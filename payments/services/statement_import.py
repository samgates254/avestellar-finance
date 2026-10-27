"""Statement import batch processing and transaction ingestion service."""

import hashlib
from decimal import Decimal
from typing import Any
from django.db import IntegrityError, transaction

from auditlog.services import record_audit_log
from automation.services.outbox import publish_outbox_event
from ledger.models import Account
from organizations.models import Organization
from payments.models import (
    StatementImportBatch,
    StatementImportSource,
    StatementImportStatus,
    StatementTransaction,
)
from payments.services.statement_parsers import (
    BankCsvParser,
    MpesaStatementParser,
    Mt940StatementParser,
    OfxStatementParser,
    StatementParseResult,
)


class DuplicateStatementFileError(Exception):
    """Raised when an identical statement file hash has already been imported for this account."""
    pass


class StatementImportError(Exception):
    """Raised when statement parsing or import fails unrecoverably."""
    pass


def compute_file_sha256(content: str | bytes) -> str:
    """Computes SHA-256 hash of file content for cryptographic deduplication."""
    if isinstance(content, str):
        data = content.encode("utf-8")
    else:
        data = content
    return hashlib.sha256(data).hexdigest()


def compute_transaction_fingerprint(
    account_id: str,
    txn_date: str,
    reference: str,
    signed_amount: Decimal,
    currency: str,
    source_transaction_id: str = "",
) -> str:
    """Creates a deterministic fingerprint for deduplicating statement lines lacking source IDs."""
    if source_transaction_id.strip():
        raw = f"{account_id}:SOURCE:{source_transaction_id.strip()}"
    else:
        raw = f"{account_id}:{txn_date}:{reference.strip()}:{signed_amount:.4f}:{currency.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_parser_for_source(
    import_source: str,
    column_mapping: dict[str, str] | None = None,
    default_currency: str = "KES",
    date_format: str | None = None,
):
    """Instantiates the appropriate parser adapter for the given source format."""
    if import_source == StatementImportSource.BANK_CSV:
        return BankCsvParser(
            column_mapping=column_mapping,
            default_currency=default_currency,
            date_format=date_format,
        )
    elif import_source == StatementImportSource.MPESA_CSV:
        return MpesaStatementParser(default_currency=default_currency)
    elif import_source == StatementImportSource.OFX:
        return OfxStatementParser(default_currency=default_currency)
    elif import_source == StatementImportSource.MT940:
        return Mt940StatementParser(default_currency=default_currency)
    else:
        raise ValueError(f"Unsupported import source: '{import_source}'")


@transaction.atomic
def import_statement_file(
    organization: Organization,
    financial_account_id: str,
    file_content: str | bytes,
    file_name: str,
    import_source: str = StatementImportSource.BANK_CSV,
    column_mapping: dict[str, str] | None = None,
    user=None,
    date_format: str | None = None,
) -> StatementImportBatch:
    """Ingests, parses, deduplicates, and persists a statement file batch.

    Invariants:
    1. Cryptographic file hash deduplication prevents duplicate batch ingestion.
    2. Zero binary floating-point numbers; all amounts Decimal.
    3. Transactions with stable upstream source IDs or matching fingerprints are deduplicated.
    4. Statement transactions are imported as reconciliation evidence, NOT auto-posted to GL.
    5. Emits 'statement.imported' outbox event atomically.
    """
    account = Account.objects.get(
        id=financial_account_id,
        organization=organization,
        is_active=True,
    )

    file_hash = compute_file_sha256(file_content)

    # 1. Deduplication check: Has this exact file been imported for this financial account?
    existing_batch = StatementImportBatch.objects.filter(
        organization=organization,
        financial_account=account,
        file_hash=file_hash,
    ).first()

    if existing_batch:
        raise DuplicateStatementFileError(
            f"File '{file_name}' (SHA-256: {file_hash[:12]}...) has already been imported for account '{account.name}'."
        )

    # 2. Parse file content
    parser = get_parser_for_source(
        import_source=import_source,
        column_mapping=column_mapping,
        default_currency=account.currency or organization.currency or "KES",
        date_format=date_format,
    )

    parse_result: StatementParseResult = parser.parse(file_content)

    # If parsing produced 0 transactions and has errors, mark batch FAILED
    is_failed = len(parse_result.transactions) == 0 and len(parse_result.errors) > 0
    status = (
        StatementImportStatus.FAILED
        if is_failed
        else (
            StatementImportStatus.PARTIAL
            if len(parse_result.errors) > 0
            else StatementImportStatus.COMPLETED
        )
    )

    # 3. Create StatementImportBatch
    batch = StatementImportBatch.objects.create(
        organization=organization,
        financial_account=account,
        import_source=import_source,
        file_name=file_name,
        file_hash=file_hash,
        parser_version="1.0.0",
        imported_by=user,
        opening_balance=parse_result.opening_balance,
        closing_balance=parse_result.closing_balance,
        total_rows=parse_result.total_rows,
        imported_count=0,
        rejected_count=parse_result.rejected_rows,
        duplicate_count=0,
        status=status,
        validation_summary={
            "total_rows_parsed": parse_result.total_rows,
            "errors_count": len(parse_result.errors),
            "currency": parse_result.currency,
        },
        error_details=parse_result.errors,
    )

    if is_failed:
        publish_outbox_event(
            event_type="statement.import_failed",
            organization=organization,
            payload={
                "batch_id": str(batch.id),
                "file_name": file_name,
                "account_id": str(account.id),
                "errors": parse_result.errors,
            },
        )
        return batch

    # 4. Ingest and deduplicate parsed transactions
    imported_count = 0
    duplicate_count = 0

    for parsed_trx in parse_result.transactions:
        fingerprint = compute_transaction_fingerprint(
            account_id=str(account.id),
            txn_date=str(parsed_trx.transaction_date),
            reference=parsed_trx.reference,
            signed_amount=parsed_trx.signed_amount,
            currency=parsed_trx.currency,
            source_transaction_id=parsed_trx.source_transaction_id,
        )

        # Check existing source_transaction_id if present
        if parsed_trx.source_transaction_id:
            exists = StatementTransaction.objects.filter(
                organization=organization,
                financial_account=account,
                source_transaction_id=parsed_trx.source_transaction_id,
            ).exists()
            if exists:
                duplicate_count += 1
                continue
        else:
            # Check fingerprint
            exists = StatementTransaction.objects.filter(
                organization=organization,
                financial_account=account,
                duplicate_fingerprint=fingerprint,
            ).exists()
            if exists:
                duplicate_count += 1
                continue

        try:
            StatementTransaction.objects.create(
                organization=organization,
                batch=batch,
                financial_account=account,
                source_transaction_id=parsed_trx.source_transaction_id,
                transaction_date=parsed_trx.transaction_date,
                value_date=parsed_trx.value_date,
                description=parsed_trx.description,
                reference=parsed_trx.reference,
                debit_amount=parsed_trx.debit_amount,
                credit_amount=parsed_trx.credit_amount,
                signed_amount=parsed_trx.signed_amount,
                currency=parsed_trx.currency,
                running_balance=parsed_trx.running_balance,
                classification=parsed_trx.classification,
                duplicate_fingerprint=fingerprint,
                raw_metadata=parsed_trx.raw_metadata,
            )
            imported_count += 1
        except IntegrityError:
            duplicate_count += 1

    batch.imported_count = imported_count
    batch.duplicate_count = duplicate_count
    batch.validation_summary["imported_count"] = imported_count
    batch.validation_summary["duplicate_count"] = duplicate_count
    batch.save(update_fields=["imported_count", "duplicate_count", "validation_summary"])

    # 5. Outbox Event
    publish_outbox_event(
        event_type="statement.imported",
        organization=organization,
        payload={
            "batch_id": str(batch.id),
            "file_name": file_name,
            "account_id": str(account.id),
            "account_name": account.name,
            "import_source": import_source,
            "imported_count": imported_count,
            "duplicate_count": duplicate_count,
            "rejected_count": batch.rejected_count,
        },
    )

    # 6. Audit Log
    record_audit_log(
        action="IMPORT_STATEMENT_BATCH",
        target_model="StatementImportBatch",
        target_id=str(batch.id),
        organization=organization,
        user=user,
        details={
            "file_name": file_name,
            "file_hash": file_hash,
            "imported_count": imported_count,
            "duplicate_count": duplicate_count,
        },
    )

    return batch

"""Deterministic Financial Reconciliation Engine and Accountant Review Services."""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from auditlog.services import record_audit_log
from automation.services.outbox import publish_outbox_event
from invoicing.models import Invoice, InvoiceStatus
from ledger.models import Account, AccountCategory, JournalEntry, JournalLine, JournalStatus
from payments.models import (
    Payment,
    PaymentMethod,
    PaymentStatus,
    PaymentType,
    ReconciliationConfidence,
    ReconciliationDecisionType,
    ReconciliationMatchDecision,
    ReconciliationSession,
    ReconciliationSessionStatus,
    StatementReconciliationStatus,
    StatementTransaction,
    StatementTransactionClassification,
)
from payments.services.settlement import record_and_confirm_customer_payment
from payments.services.verification import settle_pending_payment


@dataclass
class MatchCandidate:
    """Represents a candidate match for a statement transaction."""

    candidate_type: str  # 'PAYMENT', 'INVOICE', 'JOURNAL_ENTRY'
    candidate_id: str
    rule_applied: str
    confidence_score: int
    confidence_category: str  # HIGH, MEDIUM, LOW
    reason: str
    auto_match_permitted: bool
    evidence_fields: dict[str, Any] = field(default_factory=dict)
    candidate_summary: dict[str, Any] = field(default_factory=dict)


class ReconciliationError(Exception):
    """Base exception for reconciliation rule violations."""
    pass


class ReconciliationSafeguardError(ReconciliationError):
    """Raised when an operation would violate financial integrity invariants."""
    pass


def calculate_ledger_account_balance(
    account: Account,
    as_of_date: date,
) -> Decimal:
    """Calculates the posted general ledger balance for an account up to a given date.

    For asset accounts (Bank, Cash, Clearing), Balance = Debit - Credit.
    For liability/equity/revenue, Balance = Credit - Debit.
    """
    lines = JournalLine.objects.filter(
        account=account,
        journal_entry__organization=account.organization,
        journal_entry__status=JournalStatus.POSTED,
        journal_entry__date__lte=as_of_date,
    ).aggregate(
        total_debit=Sum("debit"),
        total_credit=Sum("credit"),
    )

    total_debit = lines["total_debit"] or Decimal("0.0000")
    total_credit = lines["total_credit"] or Decimal("0.0000")

    if account.category in (AccountCategory.ASSET, AccountCategory.EXPENSE):
        return (total_debit - total_credit).quantize(Decimal("0.0001"))
    else:
        return (total_credit - total_debit).quantize(Decimal("0.0001"))


class DeterministicReconciliationEngine:
    """Deterministic, rules-based financial matching engine."""

    def __init__(self, organization):
        self.organization = organization

    def find_candidates(
        self,
        statement_trx: StatementTransaction,
        date_window_days: int = 3,
    ) -> list[MatchCandidate]:
        """Evaluates statement transaction against candidate payments and invoices."""
        if statement_trx.reconciliation_status == StatementReconciliationStatus.RECONCILED:
            return []

        candidates: list[MatchCandidate] = []
        target_amount = abs(statement_trx.signed_amount)
        is_inflow = statement_trx.signed_amount > Decimal("0")
        ref = (statement_trx.reference or statement_trx.source_transaction_id or "").strip()

        # -------------------------------------------------------------
        # RULE 1: Strong Reference Match
        # -------------------------------------------------------------
        if ref:
            # 1A. Check Payments by exact reference or payment number
            matching_payments = Payment.objects.filter(
                organization=self.organization,
                is_reconciled=False,
            ).filter(
                Q(reference__iexact=ref) | Q(payment_number__iexact=ref)
            )

            for p in matching_payments:
                if p.currency != statement_trx.currency:
                    continue  # Currency mismatch

                if p.amount == target_amount:
                    candidates.append(
                        MatchCandidate(
                            candidate_type="PAYMENT",
                            candidate_id=str(p.id),
                            rule_applied="RULE_1_STRONG_REFERENCE",
                            confidence_score=100,
                            confidence_category=ReconciliationConfidence.HIGH,
                            reason=f"Exact match on reference '{ref}', exact amount ({target_amount} {p.currency}), and deposit account.",
                            auto_match_permitted=True,
                            evidence_fields={"reference": ref, "amount": str(p.amount), "currency": p.currency},
                            candidate_summary={
                                "payment_number": p.payment_number,
                                "amount": str(p.amount),
                                "payment_date": str(p.payment_date),
                                "status": p.status,
                                "reference": p.reference,
                            },
                        )
                    )

            # 1B. Check Invoices by invoice number matching reference (direct customer payment)
            if is_inflow:
                matching_invoices = Invoice.objects.filter(
                    organization=self.organization,
                    invoice_number__iexact=ref,
                    status__in=[InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID],
                )
                for inv in matching_invoices:
                    if inv.currency != statement_trx.currency:
                        continue
                    if inv.outstanding_balance == target_amount or inv.total_amount == target_amount:
                        candidates.append(
                            MatchCandidate(
                                candidate_type="INVOICE",
                                candidate_id=str(inv.id),
                                rule_applied="RULE_1_STRONG_REFERENCE",
                                confidence_score=95,
                                confidence_category=ReconciliationConfidence.HIGH,
                                reason=f"Exact match on Invoice Number '{inv.invoice_number}' with matching open amount.",
                                auto_match_permitted=True,
                                evidence_fields={"invoice_number": inv.invoice_number, "outstanding_balance": str(inv.outstanding_balance)},
                                candidate_summary={
                                    "invoice_number": inv.invoice_number,
                                    "customer": inv.customer.name,
                                    "total_amount": str(inv.total_amount),
                                    "outstanding_balance": str(inv.outstanding_balance),
                                },
                            )
                        )

        # -------------------------------------------------------------
        # RULE 2: Exact Amount, Direction, and Tight Date Window (±3 days)
        # -------------------------------------------------------------
        min_date = statement_trx.transaction_date - timedelta(days=date_window_days)
        max_date = statement_trx.transaction_date + timedelta(days=date_window_days)

        expected_pay_type = (
            PaymentType.CUSTOMER_PAYMENT if is_inflow else PaymentType.SUPPLIER_PAYMENT
        )

        amount_matched_payments = Payment.objects.filter(
            organization=self.organization,
            amount=target_amount,
            currency=statement_trx.currency,
            payment_type=expected_pay_type,
            payment_date__gte=min_date,
            payment_date__lte=max_date,
            is_reconciled=False,
        ).exclude(id__in=[c.candidate_id for c in candidates if c.candidate_type == "PAYMENT"])

        for p in amount_matched_payments:
            counterparty_hint = ""
            score = 80
            if p.customer and p.customer.name.lower() in statement_trx.description.lower():
                score = 88
                counterparty_hint = f" Counterparty '{p.customer.name}' matched in narrative."
            elif p.supplier and p.supplier.name.lower() in statement_trx.description.lower():
                score = 88
                counterparty_hint = f" Supplier '{p.supplier.name}' matched in narrative."

            candidates.append(
                MatchCandidate(
                    candidate_type="PAYMENT",
                    candidate_id=str(p.id),
                    rule_applied="RULE_2_EXACT_AMOUNT_DATE",
                    confidence_score=score,
                    confidence_category=ReconciliationConfidence.MEDIUM,
                    reason=f"Exact amount {target_amount} {p.currency} within ±{date_window_days} days.{counterparty_hint}",
                    auto_match_permitted=False,
                    evidence_fields={"amount": str(p.amount), "date_diff_days": abs((p.payment_date - statement_trx.transaction_date).days)},
                    candidate_summary={
                        "payment_number": p.payment_number,
                        "amount": str(p.amount),
                        "payment_date": str(p.payment_date),
                        "status": p.status,
                    },
                )
            )

        # -------------------------------------------------------------
        # RULE 3: Potential Match (Incomplete Evidence or Wider Window)
        # -------------------------------------------------------------
        wide_min = statement_trx.transaction_date - timedelta(days=14)
        wide_max = statement_trx.transaction_date + timedelta(days=14)

        wider_payments = Payment.objects.filter(
            organization=self.organization,
            amount=target_amount,
            currency=statement_trx.currency,
            payment_type=expected_pay_type,
            payment_date__gte=wide_min,
            payment_date__lte=wide_max,
            is_reconciled=False,
        ).exclude(id__in=[c.candidate_id for c in candidates if c.candidate_type == "PAYMENT"])

        for p in wider_payments:
            candidates.append(
                MatchCandidate(
                    candidate_type="PAYMENT",
                    candidate_id=str(p.id),
                    rule_applied="RULE_3_POTENTIAL_MATCH",
                    confidence_score=50,
                    confidence_category=ReconciliationConfidence.LOW,
                    reason=f"Exact amount {target_amount} {p.currency} but transaction date is outside standard window ({(p.payment_date - statement_trx.transaction_date).days} days difference).",
                    auto_match_permitted=False,
                    evidence_fields={"amount": str(p.amount), "date_diff_days": (p.payment_date - statement_trx.transaction_date).days},
                    candidate_summary={
                        "payment_number": p.payment_number,
                        "amount": str(p.amount),
                        "payment_date": str(p.payment_date),
                    },
                )
            )

        # Sort candidates by confidence score descending
        candidates.sort(key=lambda c: c.confidence_score, reverse=True)
        return candidates


@transaction.atomic
def execute_reconciliation_match(
    statement_trx_id: str,
    candidate_type: str | None = None,
    candidate_id: str | None = None,
    user=None,
    reason: str = "",
    rule_applied: str = "MANUAL_DECISION",
    confidence_score: int = 100,
    confidence_category: str = ReconciliationConfidence.MANUAL,
    session_id: str | None = None,
) -> StatementTransaction:
    """Safely executes reconciliation match between a statement transaction and financial records.

    Invariants:
    1. Statement transaction must be UNRECONCILED or MATCH_PROPOSED.
    2. Locked using select_for_update to prevent concurrent double-matching.
    3. Payment / Invoice tenant must strictly match statement transaction organization.
    4. Currencies must match.
    5. If matched to an open Invoice, calls existing record_and_confirm_customer_payment
       to ensure double-entry journal, invoice balances, and outbox events are posted atomically.
    6. If matched to existing Payment, ensures payment is marked is_reconciled=True.
    7. Creates ReconciliationMatchDecision audit log.
    8. Emits 'reconciliation.match_confirmed' outbox event.
    """
    statement_trx = (
        StatementTransaction.objects.select_for_update()
        .select_related("organization", "financial_account")
        .get(id=statement_trx_id)
    )

    if statement_trx.reconciliation_status == StatementReconciliationStatus.RECONCILED:
        raise ReconciliationSafeguardError(
            f"Statement transaction {statement_trx.id} is already reconciled and cannot be re-matched."
        )

    session = None
    if session_id:
        session = ReconciliationSession.objects.filter(
            id=session_id, organization=statement_trx.organization
        ).first()

    matched_payment = None
    matched_invoice = None
    matched_je = None
    prev_status = statement_trx.reconciliation_status

    if candidate_type == "PAYMENT" and candidate_id:
        payment = (
            Payment.objects.select_for_update()
            .get(id=candidate_id, organization=statement_trx.organization)
        )

        if payment.is_reconciled:
            raise ReconciliationSafeguardError(
                f"Payment {payment.payment_number} is already reconciled to another statement line."
            )

        if payment.currency != statement_trx.currency:
            raise ReconciliationSafeguardError(
                f"Currency mismatch: statement is in {statement_trx.currency}, payment is in {payment.currency}."
            )

        # Invariant check: amounts must match
        if payment.amount != abs(statement_trx.signed_amount):
            raise ReconciliationSafeguardError(
                f"Amount mismatch: statement amount is {abs(statement_trx.signed_amount)}, payment amount is {payment.amount}."
            )

        if payment.status == PaymentStatus.PENDING:
            if not payment.invoice_id:
                raise ReconciliationSafeguardError(
                    f"Pending payment {payment.payment_number} has no invoice to settle."
                )
            if payment.amount > payment.invoice.outstanding_balance:
                raise ReconciliationSafeguardError(
                    "Pending payment exceeds the invoice's outstanding balance."
                )
            payment = settle_pending_payment(
                pending_payment=payment,
                deposit_account=statement_trx.financial_account,
                user=user,
                evidence_type="STATEMENT_TRANSACTION",
                idempotency_key=f"reconciliation:payment:{statement_trx.id}",
            )
        payment.is_reconciled = True
        payment.status = PaymentStatus.RECONCILED
        payment.save(update_fields=["is_reconciled", "status"])

        matched_payment = payment
        matched_je = payment.journal_entry

    elif candidate_type == "INVOICE" and candidate_id:
        invoice = (
            Invoice.objects.select_for_update()
            .select_related("organization", "customer")
            .get(id=candidate_id, organization=statement_trx.organization)
        )

        if invoice.status not in (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID):
            raise ReconciliationSafeguardError(
                f"Invoice {invoice.invoice_number} cannot be settled: status is {invoice.status}."
            )

        if invoice.currency != statement_trx.currency:
            raise ReconciliationSafeguardError(
                f"Currency mismatch: statement is in {statement_trx.currency}, invoice is in {invoice.currency}."
            )

        amount_to_apply = abs(statement_trx.signed_amount)
        if amount_to_apply > invoice.outstanding_balance:
            raise ReconciliationSafeguardError(
                f"Statement amount {amount_to_apply} exceeds invoice outstanding balance {invoice.outstanding_balance}."
            )

        # Call existing payment & ledger posting service
        pay_method = (
            PaymentMethod.MPESA
            if statement_trx.batch.import_source == "MPESA_CSV"
            else PaymentMethod.BANK_TRANSFER
        )
        new_payment = record_and_confirm_customer_payment(
            invoice_id=invoice.id,
            amount=amount_to_apply,
            deposit_account_id=statement_trx.financial_account.id,
            payment_method=pay_method,
            reference=statement_trx.reference or statement_trx.source_transaction_id,
            user=user,
        )
        new_payment.is_reconciled = True
        new_payment.status = PaymentStatus.RECONCILED
        new_payment.save(update_fields=["is_reconciled", "status"])

        matched_payment = new_payment
        matched_invoice = invoice
        matched_je = new_payment.journal_entry

    # Update statement transaction state
    statement_trx.reconciliation_status = StatementReconciliationStatus.RECONCILED
    statement_trx.matched_payment = matched_payment
    statement_trx.matched_invoice = matched_invoice
    statement_trx.matched_journal_entry = matched_je
    statement_trx.reconciled_at = timezone.now()
    statement_trx.reconciled_by = user
    statement_trx.save(
        update_fields=[
            "reconciliation_status",
            "matched_payment",
            "matched_invoice",
            "matched_journal_entry",
            "reconciled_at",
            "reconciled_by",
        ]
    )

    # Record ReconciliationMatchDecision
    decision = ReconciliationMatchDecision.objects.create(
        organization=statement_trx.organization,
        session=session,
        statement_transaction=statement_trx,
        matched_payment=matched_payment,
        matched_invoice=matched_invoice,
        matched_journal_entry=matched_je,
        user=user,
        decision=ReconciliationDecisionType.MANUAL_MATCH if rule_applied == "MANUAL_DECISION" else ReconciliationDecisionType.AUTO_MATCH,
        rule_applied=rule_applied,
        confidence_score=confidence_score,
        confidence_category=confidence_category,
        reason=reason or f"Matched by {rule_applied}",
        previous_status=prev_status,
        new_status=StatementReconciliationStatus.RECONCILED,
    )

    # Emit outbox event
    publish_outbox_event(
        event_type="reconciliation.match_confirmed",
        organization=statement_trx.organization,
        payload={
            "statement_transaction_id": str(statement_trx.id),
            "payment_id": str(matched_payment.id) if matched_payment else None,
            "invoice_id": str(matched_invoice.id) if matched_invoice else None,
            "journal_entry_id": str(matched_je.id) if matched_je else None,
            "rule_applied": rule_applied,
            "confidence_score": confidence_score,
            "decision_id": str(decision.id),
        },
    )

    record_audit_log(
        action="CONFIRM_RECONCILIATION_MATCH",
        target_model="StatementTransaction",
        target_id=str(statement_trx.id),
        organization=statement_trx.organization,
        user=user,
        details={
            "rule_applied": rule_applied,
            "payment_id": str(matched_payment.id) if matched_payment else None,
            "amount": str(statement_trx.signed_amount),
        },
    )

    return statement_trx


@transaction.atomic
def mark_transaction_for_investigation(
    statement_trx_id: str,
    user=None,
    reason: str = "",
) -> StatementTransaction:
    """Flags a statement transaction as requiring accountant investigation."""
    statement_trx = (
        StatementTransaction.objects.select_for_update()
        .select_related("organization")
        .get(id=statement_trx_id)
    )

    prev_status = statement_trx.reconciliation_status
    statement_trx.reconciliation_status = StatementReconciliationStatus.INVESTIGATION
    statement_trx.save(update_fields=["reconciliation_status"])

    ReconciliationMatchDecision.objects.create(
        organization=statement_trx.organization,
        statement_transaction=statement_trx,
        user=user,
        decision=ReconciliationDecisionType.INVESTIGATE,
        rule_applied="ACCOUNTANT_EXCEPTION_FLAG",
        confidence_score=0,
        confidence_category=ReconciliationConfidence.MANUAL,
        reason=reason or "Flagged for manual investigation.",
        previous_status=prev_status,
        new_status=StatementReconciliationStatus.INVESTIGATION,
    )

    publish_outbox_event(
        event_type="reconciliation.exception",
        organization=statement_trx.organization,
        payload={
            "statement_transaction_id": str(statement_trx.id),
            "reason": reason,
            "amount": str(statement_trx.signed_amount),
            "currency": statement_trx.currency,
        },
    )

    return statement_trx


@transaction.atomic
def create_reconciliation_session(
    organization,
    financial_account_id: str,
    period_start: date,
    period_end: date,
    statement_opening_balance: Decimal,
    statement_closing_balance: Decimal,
    session_number: str | None = None,
    user=None,
    notes: str = "",
) -> ReconciliationSession:
    """Creates a new reconciliation session associating statement balances with general ledger accounts."""
    account = Account.objects.get(
        id=financial_account_id,
        organization=organization,
        is_active=True,
    )

    sess_num = session_number or f"REC-{account.code}-{period_end.strftime('%Y%m')}"

    # Calculate general ledger balance as of period_end
    ledger_bal = calculate_ledger_account_balance(account, period_end)

    # Difference between statement closing balance and general ledger balance
    diff = (statement_closing_balance - ledger_bal).quantize(Decimal("0.0001"))

    session = ReconciliationSession.objects.create(
        organization=organization,
        financial_account=account,
        session_number=sess_num,
        period_start=period_start,
        period_end=period_end,
        statement_opening_balance=statement_opening_balance,
        statement_closing_balance=statement_closing_balance,
        ledger_balance=ledger_bal,
        reconciled_balance=ledger_bal,
        difference=diff,
        status=ReconciliationSessionStatus.DRAFT,
        notes=notes,
        reconciled_by=user,
    )

    publish_outbox_event(
        event_type="reconciliation.started",
        organization=organization,
        payload={
            "session_id": str(session.id),
            "session_number": session.session_number,
            "account_id": str(account.id),
            "difference": str(diff),
        },
    )

    return session


@transaction.atomic
def finalize_reconciliation_session(
    session_id: str,
    user=None,
    force_finalize: bool = False,
) -> ReconciliationSession:
    """Finalizes a reconciliation session, locking matched transactions and making it immutable."""
    session = (
        ReconciliationSession.objects.select_for_update()
        .select_related("organization", "financial_account")
        .get(id=session_id)
    )

    if session.status == ReconciliationSessionStatus.FINALIZED:
        raise ReconciliationSafeguardError("Reconciliation session is already finalized.")

    # Re-verify ledger balance and difference
    current_ledger_bal = calculate_ledger_account_balance(
        session.financial_account, session.period_end
    )
    current_diff = (session.statement_closing_balance - current_ledger_bal).quantize(Decimal("0.0001"))

    session.ledger_balance = current_ledger_bal
    session.reconciled_balance = current_ledger_bal
    session.difference = current_diff

    # If there is an unresolved difference and not forced, raise warning or error
    if current_diff != Decimal("0.0000") and not force_finalize:
        raise ReconciliationSafeguardError(
            f"Cannot finalize reconciliation session: unexplained difference of {current_diff} {session.financial_account.currency} exists between statement and general ledger."
        )

    session.status = ReconciliationSessionStatus.FINALIZED
    session.finalized_at = timezone.now()
    session.finalized_by = user
    session.save(
        update_fields=[
            "status",
            "finalized_at",
            "finalized_by",
            "ledger_balance",
            "reconciled_balance",
            "difference",
            "updated_at",
        ]
    )

    publish_outbox_event(
        event_type="reconciliation.completed",
        organization=session.organization,
        payload={
            "session_id": str(session.id),
            "session_number": session.session_number,
            "account_id": str(session.financial_account.id),
            "difference": str(current_diff),
            "finalized_at": str(session.finalized_at),
        },
    )

    record_audit_log(
        action="FINALIZE_RECONCILIATION_SESSION",
        target_model="ReconciliationSession",
        target_id=str(session.id),
        organization=session.organization,
        user=user,
        details={
            "session_number": session.session_number,
            "difference": str(current_diff),
        },
    )

    return session


@transaction.atomic
def reopen_reconciliation_session(
    session_id: str,
    user=None,
    reason: str = "",
) -> ReconciliationSession:
    """Reopens a finalized reconciliation session under authorized control."""
    session = (
        ReconciliationSession.objects.select_for_update()
        .select_related("organization", "financial_account")
        .get(id=session_id)
    )

    if session.status != ReconciliationSessionStatus.FINALIZED:
        raise ReconciliationSafeguardError("Only finalized sessions can be reopened.")

    session.status = ReconciliationSessionStatus.REOPENED
    session.save(update_fields=["status", "updated_at"])

    record_audit_log(
        action="REOPEN_RECONCILIATION_SESSION",
        target_model="ReconciliationSession",
        target_id=str(session.id),
        organization=session.organization,
        user=user,
        details={"session_number": session.session_number, "reason": reason},
    )

    return session

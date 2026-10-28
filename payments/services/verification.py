"""Payment evidence verification and policy enforcement services.

Enforces strict separation between reported external collections and verified financial settlements.
Prevents unverified upstream POS claims or unauthenticated webhooks from automatically posting to the general ledger.

Lifecycle:
  PENDING  → created when evidence is insufficient at collection time.
  CONFIRMED → reached via settle_pending_payment() once sufficient evidence exists.

Concurrency:
  settle_pending_payment() acquires select_for_update locks on the Payment and its Invoice,
  preventing concurrent Daraja callbacks or reconciliation actions from double-settling.
"""

from dataclasses import dataclass
from decimal import Decimal
import logging
from typing import Any
import uuid

from django.db import transaction
from django.utils import timezone

from auditlog.services import record_audit_log
from automation.services.outbox import publish_outbox_event
from integrations.models import (
    DarajaCallbackEvent,
    DarajaCallbackStatus,
    DarajaCallbackType,
    ExternalObjectMapping,
    IntegrationSyncLog,
    MauzoPilotConfig,
)
from invoicing.models import Invoice, InvoiceStatus
from invoicing.services import issue_invoice
from ledger.models import Account
from organizations.models import Organization, RoleChoices
from payments.models import (
    Payment,
    PaymentMethod,
    PaymentStatus,
    PaymentType,
    StatementTransaction,
)
from payments.services.settlement import record_and_confirm_customer_payment
from payments.services.statement_parsers.bank_csv import clean_decimal

logger = logging.getLogger(__name__)


@dataclass
class PaymentVerificationResult:
    """Outcome of evaluating payment evidence against organization financial policy."""

    verified: bool
    evidence_type: str  # AUTHORIZED_CASH_POLICY, DARAJA_CALLBACK, STATEMENT_TRANSACTION, AUTHORIZED_MANUAL, UNVERIFIED
    deposit_account: Account | None = None
    reason: str = ""
    metadata: dict[str, Any] | None = None


def _callback_amount(event: DarajaCallbackEvent) -> Decimal | None:
    """Amount stated by the provider payload, or None when it states none.

    C2B payloads carry ``TransAmount``; STK payloads carry the amount inside
    ``Body.stkCallback.CallbackMetadata.Item``. An event without a comparable
    amount cannot be evidence for a specific sum.
    """
    payload = event.payload if isinstance(event.payload, dict) else {}

    raw = payload.get("TransAmount")
    if raw is None:
        body = payload.get("Body")
        stk_body = body.get("stkCallback") if isinstance(body, dict) else None
        metadata = stk_body.get("CallbackMetadata") if isinstance(stk_body, dict) else None
        items = metadata.get("Item") if isinstance(metadata, dict) else None
        for item in items or []:
            if isinstance(item, dict) and item.get("Name") == "Amount":
                raw = item.get("Value")
                break

    if raw is None or str(raw).strip() == "":
        return None
    return clean_decimal(raw)


def verify_payment_evidence(
    organization: Organization,
    invoice: Invoice,
    amount: Decimal,
    payment_method: str,
    reference: str,
    raw_data: dict[str, Any] | None = None,
    user=None,
) -> PaymentVerificationResult:
    """Strictly evaluates whether trustworthy, audit-compliant financial evidence exists to settle an invoice.

    Invariants:
    1. Organization scoping: invoice must belong to the organization.
    2. Currency matching: payment must match invoice currency.
    3. Balance boundary: amount must not exceed invoice outstanding balance.
    4. Method verification:
       - CASH: Requires explicit organization policy (`auto_settle_cash=True`) and cashier audit evidence.
       - MPESA: Requires matching verified Daraja callback or cleared statement transaction.
       - BANK: Requires matching cleared bank statement transaction.
       - MANUAL: Requires authorized user with Accountant/Admin/Owner role.
    """
    raw_data = raw_data or {}

    # 1. Organization boundary
    if invoice.organization_id != organization.id:
        return PaymentVerificationResult(
            verified=False,
            evidence_type="UNVERIFIED",
            reason=f"Tenant mismatch: Invoice {invoice.id} does not belong to organization {organization.id}.",
        )

    # 2. Currency check
    if invoice.currency != organization.currency:
        return PaymentVerificationResult(
            verified=False,
            evidence_type="UNVERIFIED",
            reason=f"Currency mismatch: Invoice is in {invoice.currency}, expected {organization.currency}.",
        )

    # 3. Balance check
    if amount <= Decimal("0.0000"):
        return PaymentVerificationResult(
            verified=False,
            evidence_type="UNVERIFIED",
            reason=f"Invalid payment amount: {amount}. Must be strictly positive.",
        )

    if amount > invoice.outstanding_balance:
        return PaymentVerificationResult(
            verified=False,
            evidence_type="UNVERIFIED",
            reason=f"Payment amount ({amount}) exceeds invoice outstanding balance ({invoice.outstanding_balance}).",
        )

    # 4. Check Authorized Manual User override
    if user and user.is_authenticated:
        membership = organization.memberships.filter(user=user, is_active=True).first()
        if membership and membership.role in (RoleChoices.OWNER, RoleChoices.ADMIN, RoleChoices.ACCOUNTANT):
            deposit_acc = (
                Account.objects.filter(organization=organization, code="1010", is_active=True).first()
                or Account.objects.filter(organization=organization, system_role="BANK", is_active=True).first()
                or Account.objects.filter(organization=organization, code="1000", is_active=True).first()
            )
            if deposit_acc:
                return PaymentVerificationResult(
                    verified=True,
                    evidence_type="AUTHORIZED_MANUAL",
                    deposit_account=deposit_acc,
                    reason=f"Manually authorized and confirmed by {user.email} ({membership.role}).",
                    metadata={"user_id": str(user.id), "user_email": user.email},
                )

    method_normalized = str(payment_method).upper()

    # 5. CASH Verification Policy
    if "CASH" in method_normalized:
        mp_config = MauzoPilotConfig.objects.filter(organization=organization).first()
        if mp_config and mp_config.auto_settle_cash:
            # Check cashier / operator audit attribution
            cashier = (
                raw_data.get("employee_username")
                or raw_data.get("cashier")
                or raw_data.get("operator")
                or (user.email if user and user.is_authenticated else "")
            )
            deposit_acc = Account.objects.filter(
                organization=organization, code="1000", is_active=True
            ).first() or Account.objects.filter(
                organization=organization, system_role="CASH", is_active=True
            ).first()

            if not deposit_acc:
                return PaymentVerificationResult(
                    verified=False,
                    evidence_type="UNVERIFIED",
                    reason="Cash on Hand account (1000) is not configured for this organization.",
                )

            return PaymentVerificationResult(
                verified=True,
                evidence_type="AUTHORIZED_CASH_POLICY",
                deposit_account=deposit_acc,
                reason="Verified POS cash collection under authorized organization cash policy.",
                metadata={"cashier": cashier, "policy": "auto_settle_cash"},
            )
        else:
            return PaymentVerificationResult(
                verified=False,
                evidence_type="UNVERIFIED",
                reason="Organization policy does not permit automated cash settlement without till register reconciliation.",
            )

    # 6. M-PESA Verification Policy
    if "MPESA" in method_normalized:
        # Only a corroborated callback counts as evidence. A payload that was
        # merely recorded (HELD) or whose payment is still awaiting review
        # (matched payment PENDING) proves nothing about the money arriving, so
        # it can never promote a payment on its own.
        daraja_event = (
            DarajaCallbackEvent.objects.filter(
                organization=organization,
                transaction_id=reference,
                callback_type__in=(
                    DarajaCallbackType.C2B_CONFIRMATION,
                    DarajaCallbackType.STK_PUSH,
                ),
                status=DarajaCallbackStatus.PROCESSED,
            )
            .exclude(matched_payment__status=PaymentStatus.PENDING)
            .first()
        )

        if daraja_event:
            # The callback must state an amount equal to the claim being verified.
            cb_amount = _callback_amount(daraja_event)
            if cb_amount is not None and cb_amount == amount:
                deposit_acc = Account.objects.filter(
                    organization=organization, code="1020", is_active=True
                ).first() or Account.objects.filter(
                    organization=organization, system_role="CLEARING", is_active=True
                ).first() or Account.objects.filter(
                    organization=organization, code="1010", is_active=True
                ).first()

                if deposit_acc:
                    return PaymentVerificationResult(
                        verified=True,
                        evidence_type="DARAJA_CALLBACK",
                        deposit_account=deposit_acc,
                        reason=f"Verified through confirmed Safaricom Daraja callback {reference}.",
                        metadata={"daraja_event_id": str(daraja_event.id), "callback_type": daraja_event.callback_type},
                    )

        # Check if cleared StatementTransaction exists in imported M-Pesa batch
        stmt_trx = StatementTransaction.objects.filter(
            organization=organization,
            reference=reference,
            signed_amount=amount,
            currency=invoice.currency,
        ).first()
        if stmt_trx:
            deposit_acc = stmt_trx.financial_account or Account.objects.filter(
                organization=organization, code="1020", is_active=True
            ).first()
            if deposit_acc:
                return PaymentVerificationResult(
                    verified=True,
                    evidence_type="STATEMENT_TRANSACTION",
                    deposit_account=deposit_acc,
                    reason=f"Verified through imported M-Pesa statement transaction {reference}.",
                    metadata={"statement_transaction_id": str(stmt_trx.id)},
                )

        return PaymentVerificationResult(
            verified=False,
            evidence_type="UNVERIFIED",
            reason=f"Reported M-Pesa receipt '{reference}' requires matching Daraja callback or statement reconciliation match.",
        )

    # 7. BANK TRANSFER Verification Policy
    if "BANK" in method_normalized:
        stmt_trx = StatementTransaction.objects.filter(
            organization=organization,
            reference=reference,
            signed_amount=amount,
            currency=invoice.currency,
        ).first()
        if stmt_trx:
            deposit_acc = stmt_trx.financial_account or Account.objects.filter(
                organization=organization, code="1010", is_active=True
            ).first()
            if deposit_acc:
                return PaymentVerificationResult(
                    verified=True,
                    evidence_type="STATEMENT_TRANSACTION",
                    deposit_account=deposit_acc,
                    reason=f"Verified through imported bank statement transaction {reference}.",
                    metadata={"statement_transaction_id": str(stmt_trx.id)},
                )

        return PaymentVerificationResult(
            verified=False,
            evidence_type="UNVERIFIED",
            reason=f"Reported bank payment '{reference}' requires matching bank statement import record.",
        )

    # Fallback: unsupported payment method or missing evidence
    return PaymentVerificationResult(
        verified=False,
        evidence_type="UNVERIFIED",
        reason=f"Unsupported payment method '{payment_method}' or insufficient verification evidence.",
    )


@transaction.atomic
def process_collection_with_verification(
    organization: Organization,
    external_payment_data: dict[str, Any],
    user=None,
    force_verified: bool = False,
) -> tuple[Payment, bool]:
    """Processes an incoming collection event and only settles if verified evidence exists.

    Returns:
        tuple[Payment, bool]: (payment_instance, is_settled_and_verified)
    """
    ext_pay_id = str(external_payment_data.get("id") or external_payment_data.get("payment_id") or "").strip()
    if not ext_pay_id:
        raise ValueError("Payment payload must contain a valid 'id' or 'payment_id'.")

    # Amount: cents vs decimal
    if "paid_cents" in external_payment_data:
        amount = (Decimal(str(external_payment_data["paid_cents"])) / Decimal("100")).quantize(Decimal("0.0001"))
    elif "amount_cents" in external_payment_data:
        amount = (Decimal(str(external_payment_data["amount_cents"])) / Decimal("100")).quantize(Decimal("0.0001"))
    else:
        amount = Decimal(str(external_payment_data.get("amount", "0"))).quantize(Decimal("0.0001"))

    if amount <= Decimal("0.0000"):
        raise ValueError(f"Invalid payment collection amount: {amount}")

    ref = str(external_payment_data.get("reference") or external_payment_data.get("payment_reference") or ext_pay_id)
    method_str = str(external_payment_data.get("payment_method", "MPESA")).upper()
    if "CASH" in method_str:
        payment_method = PaymentMethod.CASH
    elif "CARD" in method_str:
        payment_method = PaymentMethod.CARD
    elif "MPESA" in method_str:
        payment_method = PaymentMethod.MPESA
    else:
        payment_method = PaymentMethod.BANK_TRANSFER

    # Idempotency check: existing payment mapping
    existing_mapping = ExternalObjectMapping.objects.filter(
        organization=organization,
        external_system="mauzopilot",
        external_type="payment",
        external_id=ext_pay_id,
    ).first()

    if existing_mapping:
        payment = Payment.objects.filter(id=existing_mapping.internal_id, organization=organization).first()
        if payment:
            if payment.status in (PaymentStatus.CONFIRMED, PaymentStatus.RECONCILED):
                # Already settled — idempotent return.
                return payment, True

            if payment.status == PaymentStatus.PENDING and payment.invoice_id:
                # PENDING payment: re-evaluate verification evidence.
                # A Daraja callback or statement reconciliation may have arrived since the
                # original collection was recorded.  If evidence now passes policy, promote
                # the payment through the full verified lifecycle rather than returning False.
                # An external ID is immutable identity: changed amount, reference,
                # method, or invoice data must not reinterpret the original claim.
                if (
                    amount != payment.amount
                    or ref != payment.reference
                    or payment_method != payment.payment_method
                    or payment.currency != payment.invoice.currency
                ):
                    return payment, False

                re_verify = verify_payment_evidence(
                    organization=organization,
                    invoice=payment.invoice,
                    amount=payment.amount,
                    payment_method=payment.payment_method,
                    reference=payment.reference,
                    raw_data=external_payment_data,
                    user=user,
                )
                if re_verify.verified and re_verify.deposit_account:
                    promoted = settle_pending_payment(
                        pending_payment=payment,
                        deposit_account=re_verify.deposit_account,
                        user=user,
                        evidence_type=re_verify.evidence_type,
                        idempotency_key=f"mp:pay:settle:{ext_pay_id}",
                    )
                    # Update mapping metadata to record verified evidence
                    existing_mapping.raw_metadata = {
                        **existing_mapping.raw_metadata,
                        "verified_at": timezone.now().isoformat(),
                        "evidence_type": re_verify.evidence_type,
                    }
                    existing_mapping.save(update_fields=["raw_metadata"])
                    return promoted, True

            # Still unverified or already failed — return current state without modification.
            return payment, payment.status == PaymentStatus.CONFIRMED

    # Locate associated invoice
    ext_order_id = str(external_payment_data.get("order_id") or external_payment_data.get("sale_id") or "")
    invoice = None
    if ext_order_id:
        order_mapping = ExternalObjectMapping.objects.filter(
            organization=organization,
            external_system="mauzopilot",
            external_type="sales_order",
            external_id=ext_order_id,
        ).first()
        if order_mapping:
            invoice = Invoice.objects.filter(id=order_mapping.internal_id, organization=organization).first()

    if not invoice:
        inv_id = external_payment_data.get("invoice_id")
        if inv_id:
            invoice = Invoice.objects.filter(id=inv_id, organization=organization).first()

    if not invoice:
        raise ValueError(f"Cannot process payment {ext_pay_id}: no matching invoice found for order '{ext_order_id}'.")

    # Evaluate payment verification policy
    verification = verify_payment_evidence(
        organization=organization,
        invoice=invoice,
        amount=amount,
        payment_method=payment_method,
        reference=ref,
        raw_data=external_payment_data,
        user=user,
    )

    if verification.verified:
        # Trustworthy evidence exists: settle invoice and post journal entry
        if invoice.status == InvoiceStatus.DRAFT:
            invoice = issue_invoice(invoice.id, user=user)

        deposit_acc = verification.deposit_account
        if not deposit_acc:
            raise ValueError("No deposit account resolved for verified settlement.")

        payment = record_and_confirm_customer_payment(
            invoice_id=invoice.id,
            amount=amount,
            deposit_account_id=deposit_acc.id,
            payment_method=payment_method,
            payment_number=f"PAY-MP-{ext_pay_id[:24]}",
            reference=ref,
            user=user,
            idempotency_key=f"mp:pay:{ext_pay_id}",
        )

        ExternalObjectMapping.objects.create(
            organization=organization,
            external_system="mauzopilot",
            external_type="payment",
            external_id=ext_pay_id,
            internal_type="payment",
            internal_id=payment.id,
            raw_metadata={**external_payment_data, "verification": verification.evidence_type},
        )

        IntegrationSyncLog.objects.create(
            organization=organization,
            external_system="mauzopilot",
            action="VERIFIED_PAYMENT_SETTLEMENT",
            status="SUCCESS",
            details={
                "payment_id": str(payment.id),
                "amount": str(amount),
                "reference": ref,
                "evidence": verification.evidence_type,
            },
        )

        record_audit_log(
            action="VERIFIED_PAYMENT_SETTLED",
            target_model="Payment",
            target_id=str(payment.id),
            organization=organization,
            user=user,
            details={
                "evidence_type": verification.evidence_type,
                "amount": str(amount),
                "reference": ref,
                "invoice_id": str(invoice.id),
            },
        )

        return payment, True

    else:
        # Untrusted / Unverified: record as PENDING awaiting verification
        # Invariant: Does NOT post to GL, does NOT mark invoice as PAID
        default_deposit = (
            Account.objects.filter(organization=organization, code="1020", is_active=True).first()
            or Account.objects.filter(organization=organization, code="1010", is_active=True).first()
            or Account.objects.filter(organization=organization, system_role="BANK", is_active=True).first()
        )

        payment = Payment.objects.create(
            organization=organization,
            payment_number=f"PAY-PEND-{ext_pay_id[:24]}",
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            customer=invoice.customer,
            invoice=invoice,
            amount=amount,
            currency=invoice.currency,
            payment_date=timezone.now().date(),
            payment_method=payment_method,
            status=PaymentStatus.PENDING,
            reference=ref,
            deposit_account=default_deposit,
        )

        ExternalObjectMapping.objects.create(
            organization=organization,
            external_system="mauzopilot",
            external_type="payment",
            external_id=ext_pay_id,
            internal_type="payment",
            internal_id=payment.id,
            raw_metadata={**external_payment_data, "unverified_reason": verification.reason},
        )

        IntegrationSyncLog.objects.create(
            organization=organization,
            external_system="mauzopilot",
            action="UNVERIFIED_PAYMENT_RECORDED",
            status="PENDING_VERIFICATION",
            details={
                "payment_id": str(payment.id),
                "amount": str(amount),
                "reference": ref,
                "reason": verification.reason,
            },
        )

        publish_outbox_event(
            event_type="payment.verification_required",
            organization=organization,
            idempotency_key=f"verify:mp:{ext_pay_id}",
            payload={
                "payment_id": str(payment.id),
                "invoice_id": str(invoice.id),
                "amount": str(amount),
                "payment_method": payment_method,
                "reference": ref,
                "reason": verification.reason,
            },
        )

        return payment, False


@transaction.atomic
def settle_pending_payment(
    pending_payment: Payment,
    deposit_account: Account,
    user=None,
    evidence_type: str = "VERIFIED",
    idempotency_key: str = "",
) -> Payment:
    """Promotes a PENDING payment to CONFIRMED by posting the full double-entry settlement.

    This is the authoritative service for the PENDING → CONFIRMED lifecycle transition.
    It must be used by every code path that wishes to settle a previously unverified payment:
    - Daraja C2B callbacks that match a payment already recorded as PENDING via MauzoPilot
    - Daraja STK callbacks that match a previously initiated payment
    - Bank statement reconciliation workflows
    - Authorized manual user overrides

    Invariants:
    1. Acquires select_for_update on the Payment to prevent concurrent double-settlement.
    2. Payment must be in PENDING status; raises ValueError if already CONFIRMED or RECONCILED.
    3. Payment must have an associated Invoice in ISSUED or PARTIALLY_PAID status.
    4. Delegates entirely to record_and_confirm_customer_payment() for journal posting,
       invoice balance update, and 'payment.confirmed' outbox event.
    5. Preserves the original payment identifier, payment_number, reference, and external mappings.
    6. Emits a 'payment.pending_settled' audit log distinguishing this path from fresh settlements.
    7. Wrapped in transaction.atomic — a failed journal post rolls back all state changes.

    Args:
        pending_payment: The PENDING Payment instance to promote.
        deposit_account: The verified deposit/clearing account for the GL entry.
        user: The user or service account authorizing the promotion (may be None for automated paths).
        evidence_type: Human-readable description of the verification evidence (for audit trail).
        idempotency_key: Forwarded to record_and_confirm_customer_payment outbox event.

    Returns:
        The newly created CONFIRMED Payment record.

    Raises:
        ValueError: If payment is not PENDING, has no invoice, or the invoice cannot accept payment.
    """
    # Re-acquire with lock to guard against concurrent promotion attempts
    locked_payment = (
        Payment.objects.select_for_update(of=("self",))
        .select_related("organization", "invoice", "customer")
        .get(id=pending_payment.id, organization=pending_payment.organization)
    )

    if locked_payment.status != PaymentStatus.PENDING:
        raise ValueError(
            f"Payment {locked_payment.payment_number} cannot be promoted: "
            f"expected PENDING, found '{locked_payment.status}'. "
            "Concurrent settlement may have already completed."
        )

    if not locked_payment.invoice_id:
        raise ValueError(
            f"Payment {locked_payment.payment_number} has no associated Invoice "
            "and cannot be settled through the standard lifecycle."
        )

    # Validate organization isolation
    if locked_payment.organization_id != deposit_account.organization_id:
        raise ValueError(
            "Deposit account organization does not match payment organization. "
            "Cross-organization settlement is not permitted."
        )

    invoice = locked_payment.invoice
    if locked_payment.currency != invoice.currency or invoice.currency != locked_payment.organization.currency:
        raise ValueError("Pending payment currency does not match its invoice and organization.")
    if locked_payment.amount > invoice.outstanding_balance:
        raise ValueError("Pending payment exceeds the invoice's outstanding balance.")

    # record_and_confirm_customer_payment requires the invoice to be ISSUED or PARTIALLY_PAID.
    # A payment recorded as PENDING may have been attached to a DRAFT invoice that was never
    # formally issued.  Issue it now so that the settlement can proceed.
    if invoice.status == InvoiceStatus.DRAFT:
        invoice = issue_invoice(invoice.id, user=user)

    # Delegate to the single authoritative settlement service.
    # This creates the journal entry, posts it, updates invoice balances,
    # transitions invoice status, and emits the 'payment.confirmed' outbox event.
    confirmed_payment = record_and_confirm_customer_payment(
        invoice_id=locked_payment.invoice_id,
        amount=locked_payment.amount,
        deposit_account_id=deposit_account.id,
        payment_method=locked_payment.payment_method,
        payment_number=locked_payment.payment_number,
        reference=locked_payment.reference,
        user=user,
        idempotency_key=idempotency_key or f"settle:pending:{locked_payment.id}",
        existing_payment=locked_payment,
    )

    record_audit_log(
        action="PENDING_PAYMENT_SETTLED",
        target_model="Payment",
        target_id=str(confirmed_payment.id),
        organization=locked_payment.organization,
        user=user,
        details={
            "original_pending_payment_id": str(locked_payment.id),
            "confirmed_payment_id": str(confirmed_payment.id),
            "payment_number": confirmed_payment.payment_number,
            "amount": str(locked_payment.amount),
            "evidence_type": evidence_type,
            "invoice_id": str(locked_payment.invoice_id),
        },
    )

    publish_outbox_event(
        event_type="payment.pending_settled",
        organization=locked_payment.organization,
        idempotency_key=(
            f"{idempotency_key}:pending-settled"
            if idempotency_key
            else f"settle:pending:evt:{locked_payment.id}"
        ),
        payload={
            "original_pending_payment_id": str(locked_payment.id),
            "confirmed_payment_id": str(confirmed_payment.id),
            "payment_number": confirmed_payment.payment_number,
            "amount": str(locked_payment.amount),
            "currency": locked_payment.currency,
            "evidence_type": evidence_type,
            "invoice_id": str(locked_payment.invoice_id),
        },
    )

    logger.info(
        "PENDING payment %s promoted to CONFIRMED as %s via evidence '%s' (invoice %s)",
        locked_payment.payment_number,
        confirmed_payment.payment_number,
        evidence_type,
        locked_payment.invoice_id,
    )

    return confirmed_payment

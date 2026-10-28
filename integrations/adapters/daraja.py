"""Safaricom Daraja M-Pesa API and Callback Boundary Adapter.

Security model
--------------
Safaricom sends Daraja callbacks without a request signature we can verify, so an
inbound callback is treated as *untrusted input*: it is attributed to an
organization only through server-side state the business itself created (a
configured business short code, or a PENDING payment it initiated). A callback is
never, on its own, sufficient evidence to settle an invoice. It is promoted to a
CONFIRMED payment only when it corroborates an initiated payment across amount,
currency, reference and invoice state; otherwise the payload is recorded and held
for review (``DarajaCallbackStatus.HELD``).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils import timezone

from auditlog.services import record_audit_log
from automation.services.outbox import publish_outbox_event
from integrations.models import (
    DarajaCallbackEvent,
    DarajaCallbackStatus,
    DarajaCallbackType,
    DarajaConfig,
)
from invoicing.models import Invoice, InvoiceStatus
from ledger.models import Account
from organizations.models import Organization
from payments.models import Payment, PaymentMethod, PaymentStatus, PaymentType
from payments.services.statement_parsers.bank_csv import clean_decimal
from payments.services.verification import settle_pending_payment


class DarajaAdapterError(Exception):
    """Base error for Daraja operations."""
    pass


class DarajaVerificationUnavailable(DarajaAdapterError):
    """Raised when no genuine provider-status verification is available.

    Present so callers cannot quietly substitute a synthetic response for real
    provider evidence. This milestone deliberately does not implement the Daraja
    Transaction Status / reconciliation protocol.
    """
    pass


@dataclass(frozen=True)
class VerifiedPaymentResult:
    """Independently verified provider outcome for a single M-Pesa transaction.

    Only code that has actually confirmed the payment with the provider may build
    this. Nothing in the callback path does so: ``DarajaClient`` cannot verify a
    transaction, and tests build it explicitly to prove the settlement guard.
    """

    organization_id: str
    transaction_id: str
    amount: Decimal
    currency: str
    source: str


class DarajaClient:
    """Mockable Safaricom Daraja API client.

    Transaction status verification is NOT implemented. The client never returns
    a success-shaped result, because a synthetic success is indistinguishable
    from genuine confirmation once it reaches the settlement path.
    """

    def __init__(self, config: DarajaConfig | None = None):
        self.config = config

    def query_transaction_status(
        self,
        transaction_id: str,
        short_code: str | None = None,
        mock_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Reports that no provider-side verification was performed.

        ``mock_response`` exists only so tests can inject a canned payload; a
        mocked payload is never evidence and must not be passed from production
        code.
        """
        if mock_response is not None:
            return mock_response

        return {
            "ResponseCode": "1",
            "ResponseDescription": "Transaction status verification is not implemented",
            "ResultCode": "1",
            "ResultDesc": (
                "No provider verification was performed. This response must not "
                "be treated as payment evidence."
            ),
            "TransactionStatus": "UNVERIFIED",
            "Verified": False,
            "ReceiptNo": transaction_id,
        }

    def verify_payment(self, transaction_id: str) -> VerifiedPaymentResult:
        """Placeholder for genuine provider verification (not implemented)."""
        raise DarajaVerificationUnavailable(
            "Genuine Daraja transaction verification is not implemented; automatic "
            "settlement from provider status remains disabled."
        )


def _resolve_deposit_account(organization: Organization) -> Account | None:
    """Configured Daraja deposit account, else M-Pesa clearing (1020), else bank."""
    daraja_cfg = getattr(organization, "daraja_config", None)
    if daraja_cfg and daraja_cfg.default_deposit_account:
        return daraja_cfg.default_deposit_account
    return Account.objects.filter(
        organization=organization, code="1020", is_active=True
    ).first() or Account.objects.filter(
        organization=organization, is_active=True, code__in=["1010", "1000"]
    ).first()


@transaction.atomic
def process_c2b_callback(
    organization: Organization,
    payload: dict[str, Any],
    is_validation: bool = False,
) -> dict[str, Any]:
    """Processes Safaricom Daraja C2B confirmation or validation callback.

    Payload format:
    {
        "TransactionType": "Pay Bill",
        "TransID": "QKH1234567",
        "TransTime": "20260315143210",
        "TransAmount": "1500.00",
        "BusinessShortCode": "600000",
        "BillRefNumber": "INV-2026-0001",
        "InvoiceNumber": "",
        "OrgAccountBalance": "125000.00",
        "ThirdPartyTransID": "",
        "MSISDN": "254712345678",
        "FirstName": "John",
        "MiddleName": "K",
        "LastName": "Doe"
    }
    """
    callback_type = (
        DarajaCallbackType.C2B_VALIDATION
        if is_validation
        else DarajaCallbackType.C2B_CONFIRMATION
    )

    # Serialize callback processing per organization. The unique callback constraint
    # remains the durable replay guard; this lock also protects the lookup/create path.
    organization = Organization.objects.select_for_update().get(id=organization.id)

    trans_id = payload.get("TransID", "").strip()
    if not trans_id:
        return {"ResultCode": "C2B00011", "ResultDesc": "Rejected: Missing TransID"}

    # Validation callback response (validation only verifies if account exists / amount valid)
    if is_validation:
        # Check if BillRefNumber exists if required
        bill_ref = payload.get("BillRefNumber", "").strip()
        return {
            "ResultCode": "0",
            "ResultDesc": f"Accepted: Valid account reference {bill_ref}",
        }

    # Replay Protection & Idempotency: Check if this TransID was already processed for this org
    existing_event = DarajaCallbackEvent.objects.filter(
        organization=organization,
        callback_type=callback_type,
        transaction_id=trans_id,
    ).first()

    if existing_event:
        return {
            "ResultCode": "0",
            "ResultDesc": f"Duplicate confirmation ignored for TransID {trans_id}",
        }

    # Create DarajaCallbackEvent record
    event = DarajaCallbackEvent.objects.create(
        organization=organization,
        callback_type=callback_type,
        transaction_id=trans_id,
        status=DarajaCallbackStatus.PENDING,
        payload=payload,
    )

    amount_str = payload.get("TransAmount", "0")
    amount = clean_decimal(amount_str)
    bill_ref = payload.get("BillRefNumber", "").strip()
    msisdn = payload.get("MSISDN", "").strip()
    first_name = payload.get("FirstName", "").strip()
    last_name = payload.get("LastName", "").strip()
    payer_name = f"{first_name} {last_name}".strip() or msisdn

    # Resolve deposit account (configured Daraja account, M-Pesa clearing 1020, or operating bank 1010)
    daraja_cfg = getattr(organization, "daraja_config", None)
    deposit_account = (
        daraja_cfg.default_deposit_account if daraja_cfg and daraja_cfg.default_deposit_account else None
    )
    if not deposit_account:
        deposit_account = Account.objects.filter(
            organization=organization, code="1020", is_active=True
        ).first() or Account.objects.filter(
            organization=organization, is_active=True, code__in=["1010", "1000"]
        ).first()

    if not deposit_account:
        event.status = DarajaCallbackStatus.FAILED
        event.error_message = "No active deposit or clearing account configured."
        event.save(update_fields=["status", "error_message"])
        return {"ResultCode": "1", "ResultDesc": "Failed: Missing deposit account"}

    # Try matching BillRefNumber to an issued Invoice
    matched_invoice = None
    if bill_ref:
        matched_invoice = Invoice.objects.filter(
            organization=organization,
            invoice_number__iexact=bill_ref,
            status__in=[InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID],
        ).first()

    payment = None

    # Check if a PENDING payment already exists for this transaction reference
    # (e.g. recorded earlier via MauzoPilot sync but awaiting Daraja verification).
    # Promote it through the full settlement lifecycle rather than creating a duplicate.
    existing_pending = Payment.objects.filter(
        organization=organization,
        reference=trans_id,
        status=PaymentStatus.PENDING,
    ).select_related("invoice").first()

    if existing_pending:
        invoice_ok = existing_pending.invoice_id and existing_pending.invoice.status in (
            InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID
        )
        amount_ok = amount == existing_pending.amount
        method_ok = existing_pending.payment_method == PaymentMethod.MPESA
        currency_ok = existing_pending.invoice_id and (
            existing_pending.currency == existing_pending.invoice.currency
            == organization.currency
        )
        outstanding_ok = existing_pending.invoice_id and (
            existing_pending.amount <= existing_pending.invoice.outstanding_balance
        )
        if invoice_ok and amount_ok and method_ok and currency_ok and outstanding_ok:
            payment = settle_pending_payment(
                pending_payment=existing_pending,
                deposit_account=deposit_account,
                evidence_type="DARAJA_CALLBACK",
                idempotency_key=f"daraja:c2b:settle:{trans_id}",
            )
        else:
            # Reusing a transaction reference with conflicting financial details is
            # evidence for review, never a reason to create a second payment.
            event.matched_payment = existing_pending
            event.status = DarajaCallbackStatus.FAILED
            event.error_message = (
                "Callback did not match the pending payment method, amount, currency, "
                "invoice state, or outstanding balance."
            )
            event.processed_at = timezone.now()
            event.save(update_fields=["matched_payment", "status", "error_message", "processed_at"])
            record_audit_log(
                action="REJECT_DARAJA_PAYMENT_MISMATCH",
                target_model="DarajaCallbackEvent",
                target_id=str(event.id),
                organization=organization,
                details={
                    "trans_id": trans_id,
                    "callback_amount": str(amount),
                    "pending_payment_id": str(existing_pending.id),
                },
            )
            return {"ResultCode": "0", "ResultDesc": "Confirmation received for review"}

    # An unauthenticated Daraja confirmation is never sufficient on its own to
    # settle an invoice. Automatic promotion happens only above, where the
    # callback matches a PENDING payment the business itself initiated (bound by
    # reference and validated for amount, currency, method and invoice state).
    # A bare BillRefNumber match is treated as evidence for review, so we fall
    # through and hold the funds as a PENDING payment below.

    if payment is None:
        # No initiated payment corroborates this callback — record the funds as an
        # unallocated PENDING payment and hold the event for review. Nothing is
        # settled and no journal is posted.
        pay_num = f"PAY-MPESA-{trans_id}"
        payment = Payment.objects.create(
            organization=organization,
            payment_number=pay_num,
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            customer=matched_invoice.customer if matched_invoice else None,
            invoice=matched_invoice,
            amount=amount,
            currency="KES",
            payment_date=timezone.now().date(),
            payment_method=PaymentMethod.MPESA,
            status=PaymentStatus.PENDING,
            reference=trans_id,
            deposit_account=deposit_account,
        )

        publish_outbox_event(
            event_type="payment.verification_required",
            organization=organization,
            idempotency_key=f"verify:{trans_id}",
            payload={
                "payment_id": str(payment.id),
                "transaction_id": trans_id,
                "amount": str(amount),
                "bill_ref": bill_ref,
                "payer": payer_name,
                "reason": "Callback could not be independently verified against an initiated payment; held for review.",
            },
        )

        event.matched_payment = payment
        event.status = DarajaCallbackStatus.HELD
        event.error_message = (
            "Held for review: no initiated payment matched this callback."
        )
        event.processed_at = timezone.now()
        event.save(
            update_fields=["matched_payment", "status", "error_message", "processed_at"]
        )

        record_audit_log(
            action="HOLD_DARAJA_C2B_CALLBACK",
            target_model="DarajaCallbackEvent",
            target_id=str(event.id),
            organization=organization,
            details={
                "trans_id": trans_id,
                "amount": str(amount),
                "payment_id": str(payment.id),
                "reason": "no_initiated_payment",
            },
        )

        return {
            "ResultCode": "0",
            "ResultDesc": "Confirmation received and held for verification",
        }

    event.matched_payment = payment
    event.status = DarajaCallbackStatus.PROCESSED
    event.processed_at = timezone.now()
    event.save(update_fields=["matched_payment", "status", "processed_at"])

    record_audit_log(
        action="PROCESS_DARAJA_C2B_CALLBACK",
        target_model="DarajaCallbackEvent",
        target_id=str(event.id),
        organization=organization,
        details={
            "trans_id": trans_id,
            "amount": str(amount),
            "payment_id": str(payment.id) if payment else None,
        },
    )

    return {
        "ResultCode": "0",
        "ResultDesc": "Confirmation received and processed successfully",
    }


@transaction.atomic
def process_stk_callback(
    organization: Organization,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Processes Safaricom Daraja Lipa Na M-Pesa Online (STK Push) callback."""
    stk_body = payload.get("Body", {}).get("stkCallback", {})
    if not stk_body:
        return {"ResultCode": "1", "ResultDesc": "Missing Body.stkCallback"}

    result_code = stk_body.get("ResultCode")
    result_desc = stk_body.get("ResultDesc", "")
    merchant_request_id = stk_body.get("MerchantRequestID", "")
    checkout_request_id = stk_body.get("CheckoutRequestID", "")

    # Extract metadata items
    items = stk_body.get("CallbackMetadata", {}).get("Item", [])
    meta_dict = {item.get("Name"): item.get("Value") for item in items if isinstance(item, dict)}

    receipt_no = str(meta_dict.get("MpesaReceiptNumber", "") or checkout_request_id).strip()
    amount_val = meta_dict.get("Amount")
    amount = clean_decimal(amount_val) if amount_val is not None else Decimal("0.0000")

    # Replay protection
    existing_event = DarajaCallbackEvent.objects.filter(
        organization=organization,
        callback_type=DarajaCallbackType.STK_PUSH,
        transaction_id=receipt_no,
    ).first()

    if existing_event:
        return {"ResultCode": "0", "ResultDesc": f"Duplicate STK push callback ignored for {receipt_no}"}

    event = DarajaCallbackEvent.objects.create(
        organization=organization,
        callback_type=DarajaCallbackType.STK_PUSH,
        transaction_id=receipt_no,
        status=DarajaCallbackStatus.PENDING,
        payload=payload,
    )

    if result_code != 0:
        event.status = DarajaCallbackStatus.FAILED
        event.error_message = f"STK push failed with code {result_code}: {result_desc}"
        event.save(update_fields=["status", "error_message"])
        return {"ResultCode": "0", "ResultDesc": "Failure acknowledged"}

    # Success: resolve deposit account
    daraja_cfg = getattr(organization, "daraja_config", None)
    deposit_account = (
        daraja_cfg.default_deposit_account if daraja_cfg and daraja_cfg.default_deposit_account else None
    )
    if not deposit_account:
        deposit_account = Account.objects.filter(
            organization=organization, code="1020", is_active=True
        ).first() or Account.objects.filter(
            organization=organization, is_active=True, code__in=["1010", "1000"]
        ).first()

    # Look for existing pending payment with reference == merchant_request_id or checkout_request_id
    payment = Payment.objects.filter(
        organization=organization,
        reference__in=[merchant_request_id, checkout_request_id],
        status=PaymentStatus.PENDING,
    ).first()

    if payment:
        # The callback is bound to a PENDING payment this system initiated
        # (_resolve_daraja_org only returns an org for STK when such a payment
        # exists). Independently verify the callback against that payment before
        # promoting it; a success message alone is not proof.
        amount_matches = amount == payment.amount
        currency_matches = payment.currency == organization.currency
        if (
            amount_matches
            and currency_matches
            and payment.invoice_id
            and deposit_account
            and payment.invoice.status
            in (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID)
        ):
            payment = settle_pending_payment(
                pending_payment=payment,
                deposit_account=deposit_account,
                evidence_type="STK_PUSH_CALLBACK",
                idempotency_key=f"daraja:stk:settle:{receipt_no}",
            )
        else:
            # Mismatched amount/currency, or no settleable invoice: this is
            # evidence for review, never an automatic confirmation. Leave the
            # initiated payment PENDING and record why verification is required.
            event.matched_payment = payment
            event.status = DarajaCallbackStatus.FAILED
            event.error_message = (
                "STK callback did not independently match an initiated payment "
                "for settlement (amount, currency, or invoice state)."
            )
            event.processed_at = timezone.now()
            event.save(
                update_fields=["matched_payment", "status", "error_message", "processed_at"]
            )
            publish_outbox_event(
                event_type="payment.verification_required",
                organization=organization,
                idempotency_key=f"stk:verify:{receipt_no}",
                payload={
                    "payment_id": str(payment.id),
                    "receipt_no": receipt_no,
                    "amount": str(amount),
                    "reason": "STK callback did not match an initiated payment for automatic settlement.",
                },
            )
            return {"ResultCode": "0", "ResultDesc": "STK callback held for verification"}
    else:
        # No initiated payment matched, yet the callback was attributable to an
        # organization via its short code. Fail closed: never create a CONFIRMED
        # payment from an unauthenticated callback. Hold the funds as PENDING.
        pay_num = f"PAY-STK-{receipt_no}"
        payment = Payment.objects.create(
            organization=organization,
            payment_number=pay_num,
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            amount=amount,
            currency="KES",
            payment_date=timezone.now().date(),
            payment_method=PaymentMethod.MPESA,
            status=PaymentStatus.PENDING,
            reference=receipt_no,
            deposit_account=deposit_account,
        )
        event.matched_payment = payment
        event.status = DarajaCallbackStatus.HELD
        event.error_message = (
            "Held for review: STK success callback had no initiated payment to bind to."
        )
        event.processed_at = timezone.now()
        event.save(
            update_fields=["matched_payment", "status", "error_message", "processed_at"]
        )
        publish_outbox_event(
            event_type="payment.verification_required",
            organization=organization,
            idempotency_key=f"stk:verify:{receipt_no}",
            payload={
                "payment_id": str(payment.id),
                "receipt_no": receipt_no,
                "amount": str(amount),
                "reason": "STK success callback had no initiated payment to bind to.",
            },
        )
        return {"ResultCode": "0", "ResultDesc": "STK callback held for verification"}

    # Successful, independently verified settlement only.
    event.matched_payment = payment
    event.status = DarajaCallbackStatus.PROCESSED
    event.processed_at = timezone.now()
    event.save(update_fields=["matched_payment", "status", "processed_at"])

    if payment.status == PaymentStatus.CONFIRMED:
        publish_outbox_event(
            event_type="payment.confirmed",
            organization=organization,
            payload={
                "payment_id": str(payment.id),
                "payment_number": payment.payment_number,
                "receipt_no": receipt_no,
                "amount": str(amount),
            },
        )

    return {"ResultCode": "0", "ResultDesc": "STK callback processed successfully"}


@transaction.atomic
def settle_from_verified_result(
    organization: Organization,
    pending_payment: Payment,
    evidence: VerifiedPaymentResult,
) -> Payment:
    """Settle a PENDING payment from independently verified provider evidence.

    This is the reserved entry point for a future, genuine Daraja verification
    implementation (or a test-controlled adapter). It is deliberately not called
    from any callback view: until ``DarajaClient.verify_payment`` is implemented,
    automatic settlement from provider status stays disabled.

    Even with trusted evidence the settlement is re-validated against server-side
    state, so a defective or malicious caller cannot settle the wrong tenant,
    payment, amount, currency or invoice.

    Raises:
        DarajaAdapterError: if the evidence does not match the payment exactly.
    """
    if not isinstance(evidence, VerifiedPaymentResult):
        raise DarajaAdapterError(
            "Verified settlement requires VerifiedPaymentResult evidence."
        )
    if str(evidence.organization_id) != str(organization.id):
        raise DarajaAdapterError("Verified evidence belongs to another organization.")

    locked_payment = (
        Payment.objects.select_for_update(of=("self",))
        .select_related("invoice")
        .filter(organization=organization)
        .get(id=pending_payment.id)
    )

    if locked_payment.status != PaymentStatus.PENDING:
        raise DarajaAdapterError("Only a PENDING payment can be settled from verified evidence.")
    if not locked_payment.reference or evidence.transaction_id != locked_payment.reference:
        raise DarajaAdapterError("Verified evidence does not match the payment reference.")
    if evidence.amount != locked_payment.amount:
        raise DarajaAdapterError("Verified evidence amount does not match the payment.")
    if evidence.currency != locked_payment.currency:
        raise DarajaAdapterError("Verified evidence currency does not match the payment.")
    if not locked_payment.invoice_id:
        raise DarajaAdapterError("Payment has no invoice to settle.")

    invoice = locked_payment.invoice
    if invoice.organization_id != organization.id:
        raise DarajaAdapterError("Invoice belongs to another organization.")
    if invoice.currency != evidence.currency:
        raise DarajaAdapterError("Invoice currency does not match the verified evidence.")
    if invoice.status not in (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID):
        raise DarajaAdapterError("Invoice is not open for settlement.")
    if locked_payment.amount > invoice.outstanding_balance:
        raise DarajaAdapterError("Payment exceeds the invoice outstanding balance.")

    deposit_account = _resolve_deposit_account(organization)
    if not deposit_account:
        raise DarajaAdapterError("No active deposit account is configured.")

    return settle_pending_payment(
        pending_payment=locked_payment,
        deposit_account=deposit_account,
        evidence_type=f"VERIFIED_PROVIDER_RESULT:{evidence.source}",
        idempotency_key=f"daraja:verified:{evidence.transaction_id}",
    )

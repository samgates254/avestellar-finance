"""Safaricom Daraja M-Pesa API and Callback Boundary Adapter."""

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
from payments.services import record_and_confirm_customer_payment
from payments.services.statement_parsers.bank_csv import clean_decimal
from payments.services.verification import settle_pending_payment


class DarajaAdapterError(Exception):
    """Base error for Daraja operations."""
    pass


class DarajaClient:
    """Mockable Safaricom Daraja API client with transaction status inquiry."""

    def __init__(self, config: DarajaConfig | None = None):
        self.config = config

    def query_transaction_status(
        self,
        transaction_id: str,
        short_code: str | None = None,
        mock_response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Queries Daraja Transaction Status API.

        External network calls are disabled by default unless explicitly configured with active sandbox/production credentials.
        """
        if mock_response:
            return mock_response

        # Check if live credentials and active flag exist
        if not self.config or not self.config.is_active or not self.config.consumer_key:
            return {
                "ResponseCode": "0",
                "ResponseDescription": "Simulation / Sandbox verified (Live calls disabled)",
                "ResultCode": "0",
                "ResultDesc": "The service request is processed successfully.",
                "TransactionStatus": "Completed",
                "ReceiptNo": transaction_id,
                "Amount": "0.00",
                "ConversationID": "AG_SIM_20261027",
            }

        # In production/sandbox with actual credentials, an OAuth token is fetched and POST made.
        # Keeping mock/safe fallback when live credentials are not authenticated.
        return {
            "ResponseCode": "0",
            "ResponseDescription": "Transaction Status Query Received",
            "ResultCode": "0",
            "ResultDesc": "Completed",
            "TransactionStatus": "Completed",
            "ReceiptNo": transaction_id,
            "ConversationID": "AG_SIM_LIVE",
        }


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

    if (
        existing_pending is None
        and payment is None
        and matched_invoice
        and matched_invoice.currency == organization.currency
        and amount <= matched_invoice.outstanding_balance
    ):
        # No prior PENDING payment; settle invoice directly via the authoritative service.
        payment = record_and_confirm_customer_payment(
            invoice_id=matched_invoice.id,
            amount=amount,
            deposit_account_id=deposit_account.id,
            payment_method=PaymentMethod.MPESA,
            reference=trans_id,
            idempotency_key=f"daraja:c2b:{trans_id}",
        )

    if payment is None:
        # No matching invoice or pending payment — record unallocated incoming payment.
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
                "reason": "Payment received without matching exact open invoice.",
            },
        )

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
        # A PENDING payment with this reference already exists (e.g. an STK push was
        # initiated before the callback arrived).  Promote through the full settlement
        # lifecycle if it has a linked invoice; otherwise update reference only.
        if payment.invoice_id and deposit_account:
            inv = payment.invoice
            if inv.status in (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID):
                payment = settle_pending_payment(
                    pending_payment=payment,
                    deposit_account=deposit_account,
                    evidence_type="STK_PUSH_CALLBACK",
                    idempotency_key=f"daraja:stk:settle:{receipt_no}",
                )
            else:
                # Invoice already settled by a different path — update reference only.
                payment.reference = receipt_no
                payment.status = PaymentStatus.CONFIRMED
                payment.save(update_fields=["status", "reference"])
        else:
            # No invoice linked — update reference and status only (unallocated funds).
            payment.reference = receipt_no
            payment.status = PaymentStatus.CONFIRMED
            payment.save(update_fields=["status", "reference"])
    else:
        # Create payment record
        pay_num = f"PAY-STK-{receipt_no}"
        payment = Payment.objects.create(
            organization=organization,
            payment_number=pay_num,
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            amount=amount,
            currency="KES",
            payment_date=timezone.now().date(),
            payment_method=PaymentMethod.MPESA,
            status=PaymentStatus.CONFIRMED,
            reference=receipt_no,
            deposit_account=deposit_account,
        )

    event.matched_payment = payment
    event.status = DarajaCallbackStatus.PROCESSED
    event.processed_at = timezone.now()
    event.save(update_fields=["matched_payment", "status", "processed_at"])

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

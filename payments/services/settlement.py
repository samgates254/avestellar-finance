"""Payment recording and settlement services."""

import uuid
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from auditlog.services import record_audit_log
from automation.services.outbox import publish_outbox_event
from invoicing.models import Invoice, InvoiceStatus
from ledger.exceptions import AccountingError, AccountingPeriodClosedError
from ledger.models import (
    Account,
    AccountingPeriod,
    JournalEntry,
    JournalLine,
    JournalStatus,
    PeriodStatus,
)
from ledger.services.posting import post_journal_entry
from payments.models import Payment, PaymentMethod, PaymentStatus, PaymentType


@transaction.atomic
def record_and_confirm_customer_payment(
    invoice_id: uuid.UUID | str,
    amount: Decimal | str | float,
    deposit_account_id: uuid.UUID | str,
    payment_method: str = PaymentMethod.BANK_TRANSFER,
    payment_number: str | None = None,
    reference: str = "",
    user=None,
    idempotency_key: str = "",
    existing_payment: Payment | None = None,
) -> Payment:
    """Records and settles a customer payment against an issued invoice.

    Invariants:
    1. Invoice must be in ISSUED or PARTIALLY_PAID status.
    2. Payment amount must be strictly positive and <= invoice.outstanding_balance.
    3. Posts general ledger entry: Dr Deposit Account, Cr Accounts Receivable.
    4. Updates invoice paid_amount and outstanding_balance.
    5. Transitions invoice to PAID or PARTIALLY_PAID.
    6. Emits 'payment.confirmed' outbox event.
    """
    amount = Decimal(str(amount))
    if amount <= Decimal("0.0000"):
        raise ValueError("Payment amount must be strictly greater than zero.")

    invoice = (
        Invoice.objects.select_for_update(of=("self",))
        .select_related("organization", "customer")
        .get(id=invoice_id)
    )

    if invoice.status not in (InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID):
        raise ValueError(
            f"Cannot apply payment to invoice {invoice.invoice_number}: "
            f"status is '{invoice.status}', expected 'ISSUED' or 'PARTIALLY_PAID'."
        )

    if amount > invoice.outstanding_balance:
        raise ValueError(
            f"Payment amount ({amount}) exceeds outstanding invoice balance ({invoice.outstanding_balance})."
        )

    deposit_account = Account.objects.get(
        id=deposit_account_id, organization=invoice.organization, is_active=True
    )

    # Locate Accounts Receivable account
    ar_account = (
        Account.objects.filter(
            organization=invoice.organization, system_role="AR", is_active=True
        ).first()
        or Account.objects.filter(
            organization=invoice.organization, code="1200", is_active=True
        ).first()
    )
    if not ar_account:
        raise AccountingError("Accounts Receivable account not configured for this organization.")

    today = timezone.now().date()
    period = AccountingPeriod.objects.filter(
        organization=invoice.organization,
        start_date__lte=today,
        end_date__gte=today,
        status=PeriodStatus.OPEN,
    ).first() or AccountingPeriod.objects.filter(
        organization=invoice.organization, status=PeriodStatus.OPEN
    ).first()

    if not period:
        raise AccountingPeriodClosedError("No open accounting period available for payment settlement.")

    pay_num = payment_number or f"PAY-{uuid.uuid4().hex[:8].upper()}"

    # 1. Create a new Payment, or promote the existing pending record in place.
    # Keeping its primary key preserves integration mappings and callback links.
    if existing_payment is not None:
        payment = Payment.objects.select_for_update().get(
            id=existing_payment.id,
            organization=invoice.organization,
        )
        if payment.status != PaymentStatus.PENDING:
            raise ValueError(
                f"Payment {payment.payment_number} cannot be confirmed: "
                f"expected PENDING, found '{payment.status}'."
            )
        if payment.invoice_id != invoice.id:
            raise ValueError("Pending payment invoice does not match the settlement invoice.")
        if payment.amount != amount or payment.currency != invoice.currency:
            raise ValueError("Pending payment amount or currency does not match the settlement obligation.")
        if payment.payment_method != payment_method:
            raise ValueError("Pending payment method does not match the settlement method.")
        payment.customer = invoice.customer
        payment.payment_date = today
        payment.status = PaymentStatus.CONFIRMED
        payment.deposit_account = deposit_account
        payment.save(update_fields=["customer", "payment_date", "status", "deposit_account"])
    else:
        payment = Payment.objects.create(
            organization=invoice.organization,
            payment_number=pay_num,
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            customer=invoice.customer,
            invoice=invoice,
            amount=amount,
            currency=invoice.currency,
            payment_date=today,
            payment_method=payment_method,
            status=PaymentStatus.CONFIRMED,
            reference=reference,
            deposit_account=deposit_account,
        )

    # 2. Create Journal Entry
    je_number = f"JE-PAY-{pay_num}"
    journal_entry = JournalEntry.objects.create(
        organization=invoice.organization,
        period=period,
        entry_number=je_number,
        date=today,
        description=f"Receipt for invoice {invoice.invoice_number} from {invoice.customer.name}",
        reference=reference or pay_num,
        currency=invoice.currency,
        status=JournalStatus.DRAFT,
        created_by=user,
    )

    # Line 1: Dr Cash / Bank / Clearing (Deposit Account)
    JournalLine.objects.create(
        journal_entry=journal_entry,
        account=deposit_account,
        description=f"Deposit into {deposit_account.name} (Ref: {reference})",
        debit=amount,
        credit=Decimal("0.0000"),
        sequence=1,
    )

    # Line 2: Cr Accounts Receivable
    JournalLine.objects.create(
        journal_entry=journal_entry,
        account=ar_account,
        description=f"AR settlement - {invoice.customer.name} (Inv {invoice.invoice_number})",
        debit=Decimal("0.0000"),
        credit=amount,
        sequence=2,
    )

    # Post journal entry atomically
    posted_entry = post_journal_entry(journal_entry.id, user=user)
    payment.journal_entry = posted_entry
    payment.save(update_fields=["journal_entry"])

    # 3. Update Invoice Balance and Lifecycle Status
    invoice.paid_amount += amount
    invoice.outstanding_balance -= amount

    if invoice.outstanding_balance == Decimal("0.0000"):
        invoice.status = InvoiceStatus.PAID
    else:
        invoice.status = InvoiceStatus.PARTIALLY_PAID

    invoice.save(update_fields=["paid_amount", "outstanding_balance", "status", "updated_at"])

    # 4. Outbox Event
    publish_outbox_event(
        event_type="payment.confirmed",
        organization=invoice.organization,
        idempotency_key=idempotency_key,
        payload={
            "payment_id": str(payment.id),
            "payment_number": payment.payment_number,
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number,
            "customer_id": str(invoice.customer_id),
            "amount": str(payment.amount),
            "currency": payment.currency,
            "payment_method": payment.payment_method,
            "reference": payment.reference,
            "invoice_remaining_balance": str(invoice.outstanding_balance),
            "invoice_status": invoice.status,
            "journal_entry_id": str(posted_entry.id),
        },
    )

    # 5. Audit Log
    record_audit_log(
        action="CONFIRM_PAYMENT",
        target_model="Payment",
        target_id=str(payment.id),
        organization=invoice.organization,
        user=user,
        details={
            "payment_number": payment.payment_number,
            "amount": str(payment.amount),
            "invoice_number": invoice.invoice_number,
            "reference": reference,
        },
    )

    return payment

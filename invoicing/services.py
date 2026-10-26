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


@transaction.atomic
def issue_invoice(invoice_id: uuid.UUID | str, user=None) -> Invoice:
    """Transitions a draft invoice to ISSUED and posts the corresponding receivable entry to the ledger."""
    invoice = (
        Invoice.objects.select_for_update()
        .select_related("organization", "customer")
        .get(id=invoice_id)
    )

    if invoice.status != InvoiceStatus.DRAFT:
        raise ValueError(
            f"Cannot issue invoice {invoice.invoice_number}: status is '{invoice.status}', expected 'DRAFT'."
        )

    items = list(invoice.items.select_related("revenue_account").all())
    if not items:
        raise ValueError(f"Cannot issue invoice {invoice.invoice_number}: at least one line item is required.")

    # Calculate subtotal and tax
    subtotal = Decimal("0.0000")
    tax_amount = Decimal("0.0000")
    for item in items:
        line_sub = (item.quantity * item.unit_price).quantize(Decimal("0.0001"))
        line_tax = (line_sub * item.tax_rate).quantize(Decimal("0.0001"))
        subtotal += line_sub
        tax_amount += line_tax

    total_amount = (subtotal + tax_amount).quantize(Decimal("0.0001"))
    if total_amount <= Decimal("0.0000"):
        raise ValueError("Invoice total must be strictly positive to issue.")

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
        raise AccountingError("Accounts Receivable (AR) ledger account not configured for this organization.")

    # Locate Sales Tax Payable account if tax applies
    tax_account = None
    if tax_amount > Decimal("0.0000"):
        tax_account = (
            Account.objects.filter(
                organization=invoice.organization, system_role="TAX_PAYABLE", is_active=True
            ).first()
            or Account.objects.filter(
                organization=invoice.organization, code="2200", is_active=True
            ).first()
        )
        if not tax_account:
            raise AccountingError("Sales Tax / VAT Payable account not configured for this organization.")

    # Find or validate open accounting period
    period = AccountingPeriod.objects.filter(
        organization=invoice.organization,
        start_date__lte=invoice.issue_date,
        end_date__gte=invoice.issue_date,
        status=PeriodStatus.OPEN,
    ).first()
    if not period:
        # Fallback to any open period
        period = AccountingPeriod.objects.filter(
            organization=invoice.organization, status=PeriodStatus.OPEN
        ).first()
        if not period:
            raise AccountingPeriodClosedError("No open accounting period found for invoice issue date.")

    # Create Journal Entry
    je_number = f"JE-INV-{invoice.invoice_number}"
    journal_entry = JournalEntry.objects.create(
        organization=invoice.organization,
        period=period,
        entry_number=je_number,
        date=invoice.issue_date,
        description=f"Sales Invoice {invoice.invoice_number} for {invoice.customer.name}",
        reference=invoice.invoice_number,
        currency=invoice.currency,
        status=JournalStatus.DRAFT,
        created_by=user,
    )

    # 1. Debit Accounts Receivable
    JournalLine.objects.create(
        journal_entry=journal_entry,
        account=ar_account,
        description=f"AR - {invoice.customer.name} (Invoice {invoice.invoice_number})",
        debit=total_amount,
        credit=Decimal("0.0000"),
        sequence=1,
    )

    # 2. Credit Revenue Accounts
    seq = 2
    for item in items:
        item_sub = item.quantity * item.unit_price
        JournalLine.objects.create(
            journal_entry=journal_entry,
            account=item.revenue_account,
            description=f"Revenue: {item.description}",
            debit=Decimal("0.0000"),
            credit=item_sub,
            sequence=seq,
        )
        seq += 1

    # 3. Credit Tax Payable if applicable
    if tax_amount > Decimal("0.0000") and tax_account:
        JournalLine.objects.create(
            journal_entry=journal_entry,
            account=tax_account,
            description=f"VAT/Tax on Invoice {invoice.invoice_number}",
            debit=Decimal("0.0000"),
            credit=tax_amount,
            sequence=seq,
        )

    # Post journal entry to general ledger atomically
    posted_entry = post_journal_entry(journal_entry.id, user=user)

    # Update invoice state
    invoice.subtotal = subtotal
    invoice.tax_amount = tax_amount
    invoice.total_amount = total_amount
    invoice.outstanding_balance = total_amount
    invoice.status = InvoiceStatus.ISSUED
    invoice.journal_entry = posted_entry
    invoice.save(update_fields=[
        "subtotal",
        "tax_amount",
        "total_amount",
        "outstanding_balance",
        "status",
        "journal_entry",
        "updated_at",
    ])

    # Outbox event
    publish_outbox_event(
        event_type="invoice.issued",
        organization=invoice.organization,
        payload={
            "invoice_id": str(invoice.id),
            "invoice_number": invoice.invoice_number,
            "customer_id": str(invoice.customer_id),
            "customer_name": invoice.customer.name,
            "total_amount": str(invoice.total_amount),
            "currency": invoice.currency,
            "issue_date": str(invoice.issue_date),
            "due_date": str(invoice.due_date),
            "journal_entry_id": str(posted_entry.id),
        },
    )

    # Audit log
    record_audit_log(
        action="ISSUE_INVOICE",
        target_model="Invoice",
        target_id=str(invoice.id),
        organization=invoice.organization,
        user=user,
        details={
            "invoice_number": invoice.invoice_number,
            "total_amount": str(invoice.total_amount),
            "currency": invoice.currency,
        },
    )

    return invoice

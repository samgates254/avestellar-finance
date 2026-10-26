from decimal import Decimal
import pytest
from django.utils import timezone

from automation.models import OutboxEvent
from identity.models import User
from invoicing.models import Customer, Invoice, InvoiceLineItem, InvoiceStatus
from invoicing.services import issue_invoice
from ledger.models import Account, AccountingPeriod, JournalStatus
from ledger.services.initialization import initialize_organization_accounts
from organizations.models import Membership, Organization, RoleChoices
from payments.models import PaymentMethod, PaymentStatus
from payments.services import record_and_confirm_customer_payment


@pytest.mark.django_db
class TestInvoicingAndPayments:
    def setup_method(self):
        self.user = User.objects.create_user(email="finance@avestellar.com", password="Password123!")
        self.org = Organization.objects.create(name="Avestellar Commerce", slug="avestellar-commerce", currency="KES")
        Membership.objects.create(organization=self.org, user=self.user, role=RoleChoices.ACCOUNTANT)
        initialize_organization_accounts(self.org, self.user)

        self.revenue_acc = Account.objects.get(organization=self.org, code="4000")
        self.bank_acc = Account.objects.get(organization=self.org, code="1010")
        self.customer = Customer.objects.create(
            organization=self.org,
            name="Safiri Enterprises",
            email="finance@safiri.co.ke",
            phone="+254700112233",
        )

    def test_invoice_issuance_posts_receivable_and_emits_outbox_event(self):
        today = timezone.now().date()
        invoice = Invoice.objects.create(
            organization=self.org,
            customer=self.customer,
            invoice_number="INV-2026-001",
            issue_date=today,
            due_date=today,
            status=InvoiceStatus.DRAFT,
            currency="KES",
        )
        InvoiceLineItem.objects.create(
            invoice=invoice,
            revenue_account=self.revenue_acc,
            description="ERP Implementation Service",
            quantity=Decimal("1.0000"),
            unit_price=Decimal("150000.0000"),
            tax_rate=Decimal("0.1600"),  # 16% VAT = 24,000
        )

        issued = issue_invoice(invoice.id, user=self.user)

        assert issued.status == InvoiceStatus.ISSUED
        assert issued.subtotal == Decimal("150000.0000")
        assert issued.tax_amount == Decimal("24000.0000")
        assert issued.total_amount == Decimal("174000.0000")
        assert issued.outstanding_balance == Decimal("174000.0000")
        assert issued.journal_entry is not None
        assert issued.journal_entry.status == JournalStatus.POSTED

        # Check outbox event
        outbox = OutboxEvent.objects.filter(
            organization=self.org,
            event_type="invoice.issued",
            payload__invoice_number="INV-2026-001",
        ).first()
        assert outbox is not None
        assert outbox.payload["total_amount"] == "174000.0000"

    def test_partial_and_full_payment_settlement(self):
        today = timezone.now().date()
        invoice = Invoice.objects.create(
            organization=self.org,
            customer=self.customer,
            invoice_number="INV-2026-002",
            issue_date=today,
            due_date=today,
            status=InvoiceStatus.DRAFT,
            currency="KES",
        )
        InvoiceLineItem.objects.create(
            invoice=invoice,
            revenue_account=self.revenue_acc,
            description="Cloud Hosting Annual",
            quantity=Decimal("1.0000"),
            unit_price=Decimal("100000.0000"),
            tax_rate=Decimal("0.0000"),
        )
        issue_invoice(invoice.id, user=self.user)
        invoice.refresh_from_db()

        # 1. Partial payment: 40,000 KES
        p1 = record_and_confirm_customer_payment(
            invoice_id=invoice.id,
            amount=Decimal("40000.0000"),
            deposit_account_id=self.bank_acc.id,
            payment_method=PaymentMethod.BANK_TRANSFER,
            reference="FT-998811",
            user=self.user,
        )
        assert p1.status == PaymentStatus.CONFIRMED
        assert p1.journal_entry.status == JournalStatus.POSTED

        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.PARTIALLY_PAID
        assert invoice.paid_amount == Decimal("40000.0000")
        assert invoice.outstanding_balance == Decimal("60000.0000")

        # 2. Overpayment attempt: 70,000 KES (exceeds 60,000 remaining)
        with pytest.raises(ValueError, match="exceeds outstanding invoice balance"):
            record_and_confirm_customer_payment(
                invoice_id=invoice.id,
                amount=Decimal("70000.0000"),
                deposit_account_id=self.bank_acc.id,
                user=self.user,
            )

        # 3. Final settlement payment: 60,000 KES
        p2 = record_and_confirm_customer_payment(
            invoice_id=invoice.id,
            amount=Decimal("60000.0000"),
            deposit_account_id=self.bank_acc.id,
            payment_method=PaymentMethod.MPESA,
            reference="MPESA-QA12345",
            user=self.user,
        )
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.PAID
        assert invoice.paid_amount == Decimal("100000.0000")
        assert invoice.outstanding_balance == Decimal("0.0000")

        # Verify payment outbox event
        outbox = OutboxEvent.objects.filter(
            organization=self.org,
            event_type="payment.confirmed",
            payload__reference="MPESA-QA12345",
        ).first()
        assert outbox is not None
        assert outbox.payload["invoice_status"] == InvoiceStatus.PAID

"""Comprehensive tests for the Canonical Data Flywheel across MauzoPilot, AVESTELLAR FINANCE, and n8n."""

from decimal import Decimal
import uuid
import httpx
import pytest
from rest_framework.test import APIClient

from automation.models import DeliveryStatus, OutboxEvent, OutboxStatus, WebhookDelivery, WebhookSubscription
from automation.services.outbox import publish_outbox_event
from automation.services.webhooks import dispatch_outbox_event
from identity.models import User
from integrations.adapters.mauzopilot import MauzoPilotAdapter, MauzoPilotAPIError
from integrations.models import ExternalObjectMapping, MauzoPilotConfig
from invoicing.models import Customer, Invoice, InvoiceLineItem, InvoiceStatus
from ledger.models import Account, JournalEntry, JournalStatus
from ledger.services.initialization import initialize_organization_accounts
from organizations.models import Membership, Organization, RoleChoices
from payments.models import Payment, PaymentMethod, PaymentStatus
from payments.services import record_and_confirm_customer_payment


@pytest.mark.django_db
class TestCanonicalDataFlywheel:
    def setup_method(self):
        self.user = User.objects.create_user(email="flywheel@avestellar.com", password="Password123!")
        self.org = Organization.objects.create(name="Flywheel Retail Group", slug="flywheel-org", currency="KES")
        Membership.objects.create(organization=self.org, user=self.user, role=RoleChoices.ADMIN)
        initialize_organization_accounts(self.org, self.user)

        self.revenue_acc = Account.objects.get(organization=self.org, code="4000")
        self.clearing_acc = Account.objects.get(organization=self.org, code="1020")
        self.ar_acc = Account.objects.get(organization=self.org, code="1200")

        self.mp_config = MauzoPilotConfig.objects.create(
            organization=self.org,
            business_id="a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
            base_url="https://api.mauzopilot.mock/v1",
            api_key="mp-live-key-xyz",
            webhook_secret="mp-secret-flywheel",
            is_active=True,
        )
        self.adapter = MauzoPilotAdapter(organization=self.org)

    def test_full_flywheel_lifecycle_sale_to_settlement_to_outbox(self):
        """Tests the full flywheel progression:
        Operational Sale -> Draft Invoice -> Payment Collection -> Posted Journal -> Outbox Events -> Webhook Dispatch
        """
        # 1. Setup webhook subscription representing n8n listening for financial events
        subscription = WebhookSubscription.objects.create(
            organization=self.org,
            target_url="https://n8n.automation.internal/workflows/finance-hub",
            secret="n8n-shared-secret",
            events=["customer.synchronized", "sale.received", "invoice.issued", "payment.confirmed"],
            is_active=True,
        )

        # 2. Ingest Customer
        cust_payload = {
            "id": "mp-cust-flywheel-01",
            "name": "Flywheel Client Ltd",
            "email": "ops@flywheel.co.ke",
            "phone": "+254711998877",
            "credit_limit_cents": 100000000,
        }
        customer = self.adapter.sync_customer(cust_payload)
        assert customer.name == "Flywheel Client Ltd"

        cust_evt = OutboxEvent.objects.get(
            organization=self.org, event_type="customer.synchronized", payload__external_id="mp-cust-flywheel-01"
        )
        assert cust_evt.status == OutboxStatus.PENDING

        # 3. Ingest Sale Order -> Creates Draft Invoice
        order_payload = {
            "id": "SO-FLYWHEEL-9001",
            "status": "COMPLETED",
            "customer": cust_payload,
            "items": [
                {
                    "description": "High Value Equipment",
                    "quantity": "1.0000",
                    "unit_price": "75000.00",
                    "tax_rate": "0.16",
                }
            ],
        }
        invoice = self.adapter.sync_sales_order_to_draft_invoice(order_payload)
        assert invoice.status == InvoiceStatus.DRAFT
        assert invoice.total_amount == Decimal("87000.0000")

        sale_evt = OutboxEvent.objects.get(
            organization=self.org, event_type="sale.received", payload__order_id="SO-FLYWHEEL-9001"
        )
        assert sale_evt.status == OutboxStatus.PENDING

        # 4. Ingest and Confirm Payment Collection with verified Daraja callback evidence
        from integrations.models import DarajaCallbackEvent, DarajaCallbackStatus, DarajaCallbackType
        DarajaCallbackEvent.objects.create(
            organization=self.org,
            callback_type=DarajaCallbackType.C2B_CONFIRMATION,
            transaction_id="MPESA-FLY-7766",
            status=DarajaCallbackStatus.PROCESSED,
            payload={"TransAmount": "87000.00", "TransID": "MPESA-FLY-7766"},
        )

        payment_payload = {
            "id": "PAY-FLYWHEEL-001",
            "order_id": "SO-FLYWHEEL-9001",
            "amount": "87000.00",
            "payment_reference": "MPESA-FLY-7766",
            "payment_method": "MPESA",
        }
        payment = self.adapter.sync_payment_collection(payment_payload)
        assert payment.status == PaymentStatus.CONFIRMED
        assert payment.amount == Decimal("87000.0000")

        # Invariant: Invoice transitions to PAID
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.PAID
        assert invoice.outstanding_balance == Decimal("0.0000")

        # Invariant: Balanced Journal Entry posted to General Ledger
        assert payment.journal_entry is not None
        assert payment.journal_entry.status == JournalStatus.POSTED
        assert payment.journal_entry.total_debit == Decimal("87000.0000")
        assert payment.journal_entry.total_credit == Decimal("87000.0000")

        pay_evt = OutboxEvent.objects.get(
            organization=self.org, event_type="payment.confirmed", payload__payment_id=str(payment.id)
        )
        assert pay_evt.status == OutboxStatus.PENDING

        # 5. Dispatch outbox events to n8n webhook mock
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"received": True}))
        with httpx.Client(transport=transport) as mock_client:
            d_cust = dispatch_outbox_event(cust_evt, client=mock_client)
            d_sale = dispatch_outbox_event(sale_evt, client=mock_client)
            d_pay = dispatch_outbox_event(pay_evt, client=mock_client)

        assert len(d_cust) == 1 and d_cust[0].status == DeliveryStatus.SUCCESS
        assert len(d_sale) == 1 and d_sale[0].status == DeliveryStatus.SUCCESS
        assert len(d_pay) == 1 and d_pay[0].status == DeliveryStatus.SUCCESS

    def test_duplicate_sales_order_and_idempotent_invoice_creation(self):
        """Repeated sales order ingestion must never create duplicate draft invoices."""
        order_payload = {
            "id": "SO-DUP-CHECK-01",
            "status": "COMPLETED",
            "total_amount": "45000.00",
            "customer": {
                "id": "mp-cust-dup-01",
                "name": "Duplicate Check Customer",
            },
        }

        inv1 = self.adapter.sync_sales_order_to_draft_invoice(order_payload)
        inv2 = self.adapter.sync_sales_order_to_draft_invoice(order_payload)

        assert inv1.id == inv2.id
        assert Invoice.objects.filter(organization=self.org).count() == 1
        assert ExternalObjectMapping.objects.filter(
            organization=self.org, external_type="sales_order", external_id="SO-DUP-CHECK-01"
        ).count() == 1

    def test_repeated_payment_events_idempotency(self):
        """Repeated payment events with the same external ID must not double-settle or create duplicate payments."""
        order_payload = {
            "id": "SO-PAY-DUP-01",
            "status": "COMPLETED",
            "total_amount": "20000.00",
            "customer": {"id": "mp-cust-p-1", "name": "Payment Test Customer"},
        }
        invoice = self.adapter.sync_sales_order_to_draft_invoice(order_payload)

        from integrations.models import DarajaCallbackEvent, DarajaCallbackStatus, DarajaCallbackType
        DarajaCallbackEvent.objects.create(
            organization=self.org,
            callback_type=DarajaCallbackType.C2B_CONFIRMATION,
            transaction_id="REF-9988",
            status=DarajaCallbackStatus.PROCESSED,
            payload={"TransAmount": "20000.00", "TransID": "REF-9988"},
        )

        pay_payload = {
            "id": "PAY-EXT-9988",
            "order_id": "SO-PAY-DUP-01",
            "amount": "20000.00",
            "payment_reference": "REF-9988",
            "payment_method": "MPESA",
        }

        pay1 = self.adapter.sync_payment_collection(pay_payload)
        pay2 = self.adapter.sync_payment_collection(pay_payload)

        assert pay1.id == pay2.id
        assert Payment.objects.filter(organization=self.org, reference="REF-9988").count() == 1
        assert ExternalObjectMapping.objects.filter(
            organization=self.org, external_type="payment", external_id="PAY-EXT-9988"
        ).count() == 1

    def test_invalid_external_payloads_rejection(self):
        """Validates that corrupt, incomplete, or unconfirmed records are rejected cleanly."""
        # 1. Missing order identifier
        with pytest.raises(ValueError, match="Order payload must contain an 'id'"):
            self.adapter.sync_sales_order_to_draft_invoice({"status": "completed"})

        # 2. Ineligible order status (e.g. pending / draft upstream)
        with pytest.raises(ValueError, match="is in status 'pending', expected completed/confirmed"):
            self.adapter.sync_sales_order_to_draft_invoice({
                "id": "SO-PENDING-01",
                "status": "pending",
            })

        # 3. Invalid payment amount (<= 0)
        with pytest.raises(ValueError, match="Invalid payment collection amount"):
            self.adapter.sync_payment_collection({
                "id": "PAY-ZERO-01",
                "order_id": "SO-SOMETHING",
                "amount": "0.00",
            })

    def test_webhook_delivery_failure_and_redelivery(self):
        """Validates that external delivery failures do not roll back transactions, and redelivery functions cleanly."""
        sub = WebhookSubscription.objects.create(
            organization=self.org,
            target_url="https://flaky-partner.internal/webhook",
            secret="flaky-secret",
            events=["*"],
            is_active=True,
        )

        event = OutboxEvent.objects.create(
            event_id="evt-flaky-01",
            event_type="invoice.issued",
            organization=self.org,
            payload={"invoice_number": "INV-FLAKY-01"},
        )

        # 1. First attempt fails (HTTP 500)
        transport_err = httpx.MockTransport(lambda req: httpx.Response(500, text="Internal Server Error"))
        with httpx.Client(transport=transport_err) as mock_client:
            deliveries = dispatch_outbox_event(event, client=mock_client)

        assert len(deliveries) == 1
        assert deliveries[0].status == DeliveryStatus.FAILED
        assert deliveries[0].status_code == 500

        event.refresh_from_db()
        assert event.status == OutboxStatus.PROCESSED
        assert deliveries[0].status == DeliveryStatus.FAILED

        # 2. Redelivery attempt succeeds (HTTP 200)
        transport_ok = httpx.MockTransport(lambda req: httpx.Response(200, json={"ack": True}))
        with httpx.Client(transport=transport_ok) as mock_client:
            deliveries_retry = dispatch_outbox_event(event, client=mock_client)

        assert len(deliveries_retry) == 1
        assert deliveries_retry[0].status == DeliveryStatus.SUCCESS

        # Total deliveries is now 2 (1 failed, 1 succeeded)
        assert WebhookDelivery.objects.filter(outbox_event=event).count() == 2

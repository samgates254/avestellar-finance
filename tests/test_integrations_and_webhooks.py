import json
import time
from decimal import Decimal
import httpx
import pytest
from rest_framework.test import APIClient

from automation.models import DeliveryStatus, OutboxEvent, OutboxStatus, WebhookSubscription
from automation.services.webhooks import (
    dispatch_outbox_event,
    generate_webhook_signature,
    verify_webhook_signature,
)
from identity.models import User
from invoicing.models import InvoiceStatus
from integrations.adapters.mauzopilot import MauzoPilotAdapter
from integrations.models import ExternalObjectMapping, MauzoPilotConfig
from ledger.services.initialization import initialize_organization_accounts
from organizations.models import Membership, Organization, RoleChoices


@pytest.mark.django_db
class TestIntegrationsAndWebhooks:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="integrations@avestellar.com", password="Password123!")
        self.org = Organization.objects.create(name="MauzoPilot Integrations Org", slug="mp-org", currency="KES")
        Membership.objects.create(organization=self.org, user=self.user, role=RoleChoices.ADMIN)
        initialize_organization_accounts(self.org, self.user)

        self.adapter = MauzoPilotAdapter(organization=self.org)
        self.mp_config = MauzoPilotConfig.objects.create(
            organization=self.org,
            base_url="https://api.mauzopilot.mock/v1",
            api_key="test-api-key-12345",
            webhook_secret="test-webhook-secret-67890",
            is_active=True,
        )

    def test_mauzopilot_customer_sync_idempotency(self):
        payload = {
            "id": "mp-cust-101",
            "name": "Acme Retail Ltd",
            "email": "info@acme.co.ke",
            "phone": "+254711223344",
        }

        # First sync creates customer and mapping
        c1 = self.adapter.sync_customer(payload)
        assert c1.name == "Acme Retail Ltd"

        mapping = ExternalObjectMapping.objects.filter(
            organization=self.org, external_type="customer", external_id="mp-cust-101"
        ).first()
        assert mapping is not None
        assert mapping.internal_id == c1.id

        # Second sync with same ID returns identical customer without duplicating
        c2 = self.adapter.sync_customer(payload)
        assert c1.id == c2.id
        assert ExternalObjectMapping.objects.filter(external_id="mp-cust-101").count() == 1

    def test_mauzopilot_sales_order_sync_creates_draft_invoice(self):
        order_payload = {
            "id": "SO-8899",
            "status": "confirmed",
            "customer": {
                "id": "mp-cust-202",
                "name": "Bayside Merchants",
            },
            "items": [
                {
                    "description": "Inventory Goods A",
                    "quantity": "2",
                    "unit_price": "25000.00",
                    "tax_rate": "0.16",
                }
            ],
        }

        invoice = self.adapter.sync_sales_order_to_draft_invoice(order_payload)
        assert invoice.status == InvoiceStatus.DRAFT  # Safe invariant: does NOT immediately post
        assert invoice.total_amount == Decimal("58000.0000")
        assert invoice.customer.name == "Bayside Merchants"

        # Repeated sync returns existing draft invoice
        inv_repeat = self.adapter.sync_sales_order_to_draft_invoice(order_payload)
        assert inv_repeat.id == invoice.id

    def test_webhook_hmac_signature_generation_and_replay_protection(self):
        secret = "super-secret-signing-key"
        payload_bytes = b'{"event":"invoice.issued","amount":"50000.00"}'
        timestamp = str(int(time.time()))

        signature = generate_webhook_signature(secret, timestamp, payload_bytes)
        assert signature is not None

        # Valid signature & fresh timestamp passes
        assert verify_webhook_signature(secret, timestamp, payload_bytes, signature) is True

        # Tampered payload fails
        tampered = b'{"event":"invoice.issued","amount":"999999.00"}'
        assert verify_webhook_signature(secret, timestamp, tampered, signature) is False

        # Expired timestamp (> 300 seconds) fails
        stale_timestamp = str(int(time.time()) - 301)
        stale_signature = generate_webhook_signature(secret, stale_timestamp, payload_bytes)
        assert verify_webhook_signature(secret, stale_timestamp, payload_bytes, stale_signature) is False

    def test_outbox_event_dispatch_to_webhook_subscription(self):
        sub = WebhookSubscription.objects.create(
            organization=self.org,
            target_url="https://n8n.webhook.internal/test-endpoint",
            secret="n8n-secret-1234",
            events=["*"],
            is_active=True,
        )

        event = OutboxEvent.objects.create(
            event_id="evt-test-001",
            event_type="invoice.issued",
            organization=self.org,
            payload={"invoice_number": "INV-TEST-001", "total": "5000.00"},
        )

        # Mock httpx client response
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"received": True}))
        with httpx.Client(transport=transport) as mock_client:
            deliveries = dispatch_outbox_event(event, client=mock_client)

        assert len(deliveries) == 1
        assert deliveries[0].status == DeliveryStatus.SUCCESS
        assert deliveries[0].status_code == 200

        event.refresh_from_db()
        assert event.status == OutboxStatus.PROCESSED

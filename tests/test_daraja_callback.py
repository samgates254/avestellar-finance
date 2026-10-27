"""Tests for Safaricom Daraja M-Pesa callbacks, replay protection, and transaction status inquiries."""

from decimal import Decimal
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from automation.models import OutboxEvent
from identity.models import User
from integrations.adapters.daraja import DarajaClient, process_c2b_callback, process_stk_callback
from integrations.models import (
    DarajaCallbackEvent,
    DarajaCallbackStatus,
    DarajaCallbackType,
    DarajaConfig,
)
from invoicing.models import Customer, Invoice, InvoiceLineItem, InvoiceStatus
from invoicing.services import issue_invoice
from ledger.models import Account, JournalStatus
from ledger.services.initialization import initialize_organization_accounts
from organizations.models import Membership, Organization, RoleChoices
from payments.models import Payment, PaymentMethod, PaymentStatus


@pytest.mark.django_db
class TestDarajaIntegrationAndCallbacks:
    """Comprehensive test suite for Daraja payment boundary."""

    def setup_method(self):
        self.user = User.objects.create_user(email="daraja@avestellar.com", password="Password123!")
        self.org = Organization.objects.create(name="Avestellar Daraja Retail", slug="avestellar-daraja", currency="KES")
        Membership.objects.create(organization=self.org, user=self.user, role=RoleChoices.ACCOUNTANT)
        initialize_organization_accounts(self.org, self.user)

        self.clearing_acc = Account.objects.get(organization=self.org, code="1020")
        self.revenue_acc = Account.objects.get(organization=self.org, code="4000")

        # Configure Daraja for this organization
        self.daraja_config = DarajaConfig.objects.create(
            organization=self.org,
            short_code="600999",
            consumer_key="test_consumer_key",
            consumer_secret="test_consumer_secret",
            passkey="test_passkey",
            is_sandbox=True,
            is_active=True,
            default_deposit_account=self.clearing_acc,
        )

        self.customer = Customer.objects.create(
            organization=self.org,
            name="Wanjiku Enterprises",
            email="wanjiku@enterprise.co.ke",
            phone="+254711223344",
        )

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)
        self.client.defaults["HTTP_X_ORGANIZATION_ID"] = str(self.org.id)

    def test_c2b_validation_callback(self):
        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "QKH001VAL",
            "TransTime": "20260315120000",
            "TransAmount": "5000.00",
            "BusinessShortCode": "600999",
            "BillRefNumber": "ACC-101",
            "MSISDN": "254711223344",
        }
        res = self.client.post("/api/v1/integrations/daraja/c2b-validation/", payload, format="json")
        assert res.status_code == 200
        assert res.data["ResultCode"] == "0"
        assert "Accepted" in res.data["ResultDesc"]

    def test_c2b_confirmation_with_matching_open_invoice(self):
        today = timezone.now().date()
        invoice = Invoice.objects.create(
            organization=self.org,
            customer=self.customer,
            invoice_number="INV-MPESA-88",
            issue_date=today,
            due_date=today,
            status=InvoiceStatus.DRAFT,
            currency="KES",
        )
        InvoiceLineItem.objects.create(
            invoice=invoice,
            revenue_account=self.revenue_acc,
            description="Consulting Service",
            quantity=Decimal("1.0000"),
            unit_price=Decimal("12000.0000"),
            tax_rate=Decimal("0.0000"),
        )
        issued_invoice = issue_invoice(invoice.id, user=self.user)

        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "QKH888777A",
            "TransTime": "20260315143000",
            "TransAmount": "12000.00",
            "BusinessShortCode": "600999",
            "BillRefNumber": "INV-MPESA-88",
            "MSISDN": "254711223344",
            "FirstName": "Jane",
            "LastName": "Wanjiku",
        }

        res = self.client.post("/api/v1/integrations/daraja/c2b-confirmation/", payload, format="json")
        assert res.status_code == 200
        assert res.data["ResultCode"] == "0"

        # Verify database state
        issued_invoice.refresh_from_db()
        assert issued_invoice.status == InvoiceStatus.PAID
        assert issued_invoice.paid_amount == Decimal("12000.0000")
        assert issued_invoice.outstanding_balance == Decimal("0.0000")

        # Payment record created
        payment = Payment.objects.filter(
            organization=self.org, reference="QKH888777A"
        ).first()
        assert payment is not None
        assert payment.amount == Decimal("12000.0000")
        assert payment.payment_method == PaymentMethod.MPESA
        assert payment.deposit_account == self.clearing_acc
        assert payment.status == PaymentStatus.CONFIRMED

        # Outbox event
        outbox = OutboxEvent.objects.filter(
            organization=self.org,
            event_type="payment.confirmed",
            payload__reference="QKH888777A",
        ).first()
        assert outbox is not None

    def test_c2b_confirmation_replay_protection_is_idempotent(self):
        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "QKH_REPLAY_1",
            "TransTime": "20260315150000",
            "TransAmount": "3500.00",
            "BusinessShortCode": "600999",
            "BillRefNumber": "NO_MATCH",
            "MSISDN": "254711223344",
        }

        # First call
        res1 = self.client.post("/api/v1/integrations/daraja/c2b-confirmation/", payload, format="json")
        assert res1.status_code == 200
        assert res1.data["ResultCode"] == "0"

        initial_count = Payment.objects.filter(reference="QKH_REPLAY_1").count()
        assert initial_count == 1

        # Second call (replay)
        res2 = self.client.post("/api/v1/integrations/daraja/c2b-confirmation/", payload, format="json")
        assert res2.status_code == 200
        assert res2.data["ResultCode"] == "0"
        assert "Duplicate" in res2.data["ResultDesc"]

        # Ensure no second payment was created
        final_count = Payment.objects.filter(reference="QKH_REPLAY_1").count()
        assert final_count == 1

    def test_c2b_confirmation_without_invoice_emits_verification_required(self):
        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "QKH_UNREF_100",
            "TransTime": "20260315153000",
            "TransAmount": "7500.00",
            "BusinessShortCode": "600999",
            "BillRefNumber": "UNKNOWN_REF",
            "MSISDN": "254799887766",
            "FirstName": "Peter",
            "LastName": "Omondi",
        }

        res = self.client.post("/api/v1/integrations/daraja/c2b-confirmation/", payload, format="json")
        assert res.status_code == 200

        payment = Payment.objects.filter(reference="QKH_UNREF_100").first()
        assert payment is not None
        assert payment.status == PaymentStatus.PENDING

        outbox = OutboxEvent.objects.filter(
            organization=self.org,
            event_type="payment.verification_required",
            payload__transaction_id="QKH_UNREF_100",
        ).first()
        assert outbox is not None

    def test_stk_callback_success(self):
        payload = {
            "Body": {
                "stkCallback": {
                    "MerchantRequestID": "29115-34620561-1",
                    "CheckoutRequestID": "ws_CO_19122026102036404",
                    "ResultCode": 0,
                    "ResultDesc": "The service request is processed successfully.",
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": 4500.00},
                            {"Name": "MpesaReceiptNumber", "Value": "QKH_STK_SUCCESS"},
                            {"Name": "TransactionDate", "Value": 20260315154500},
                            {"Name": "PhoneNumber", "Value": 254711223344},
                        ]
                    },
                }
            }
        }

        res = self.client.post("/api/v1/integrations/daraja/stk-callback/", payload, format="json")
        assert res.status_code == 200

        callback_event = DarajaCallbackEvent.objects.filter(
            transaction_id="QKH_STK_SUCCESS"
        ).first()
        assert callback_event is not None
        assert callback_event.status == DarajaCallbackStatus.PROCESSED
        assert callback_event.matched_payment is not None
        assert callback_event.matched_payment.amount == Decimal("4500.0000")

    def test_stk_callback_user_cancelled_failure(self):
        payload = {
            "Body": {
                "stkCallback": {
                    "MerchantRequestID": "29115-34620561-2",
                    "CheckoutRequestID": "ws_CO_CANCELLED_001",
                    "ResultCode": 1032,
                    "ResultDesc": "Request cancelled by user",
                }
            }
        }

        res = self.client.post("/api/v1/integrations/daraja/stk-callback/", payload, format="json")
        assert res.status_code == 200

        event = DarajaCallbackEvent.objects.filter(
            transaction_id="ws_CO_CANCELLED_001"
        ).first()
        assert event is not None
        assert event.status == DarajaCallbackStatus.FAILED
        assert "cancelled" in event.error_message.lower()

    def test_daraja_transaction_status_inquiry_client_mock_boundary(self):
        client = DarajaClient(config=self.daraja_config)
        # Sandbox verification / safe mock response when live calls are disabled
        res = client.query_transaction_status(transaction_id="QKH_VERIFY_999")
        assert res["TransactionStatus"] == "Completed"
        assert res["ReceiptNo"] == "QKH_VERIFY_999"

        # Via API endpoint
        api_res = self.client.post(
            "/api/v1/integrations/daraja/verify-status/",
            {"transaction_id": "QKH_VERIFY_999"},
            format="json",
        )
        assert api_res.status_code == 200
        assert api_res.data["ReceiptNo"] == "QKH_VERIFY_999"

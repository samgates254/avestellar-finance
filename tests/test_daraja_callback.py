"""Tests for Safaricom Daraja M-Pesa callbacks, replay protection, and transaction status inquiries.

All provider interaction is mocked or entirely local. No request leaves the test
process: nothing here contacts Safaricom.
"""

from decimal import Decimal
import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from automation.models import OutboxEvent
from identity.models import User
from integrations.adapters.daraja import (
    DarajaAdapterError,
    DarajaClient,
    DarajaVerificationUnavailable,
    VerifiedPaymentResult,
    process_c2b_callback,
    process_stk_callback,
    settle_from_verified_result,
)
from integrations.models import (
    DarajaCallbackEvent,
    DarajaCallbackStatus,
    DarajaCallbackType,
    DarajaConfig,
)
from invoicing.models import Customer, Invoice, InvoiceLineItem, InvoiceStatus
from invoicing.services import issue_invoice
from ledger.models import Account, JournalEntry, JournalStatus
from ledger.services.initialization import initialize_organization_accounts
from organizations.models import Membership, Organization, RoleChoices
from payments.models import Payment, PaymentMethod, PaymentStatus, PaymentType
from payments.services.verification import verify_payment_evidence


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

    def test_c2b_confirmation_matched_invoice_is_held_for_verification(self):
        """A bare BillRefNumber match must NOT auto-settle.

        An unauthenticated confirmation callback is only evidence; the invoice
        stays ISSUED and the funds are held as a PENDING payment for independent
        verification, with no settlement journal entry posted.
        """
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

        # Invoice is NOT settled by an unauthenticated callback.
        issued_invoice.refresh_from_db()
        assert issued_invoice.status == InvoiceStatus.ISSUED
        assert issued_invoice.paid_amount == Decimal("0.0000")

        payment = Payment.objects.filter(
            organization=self.org, reference="QKH888777A"
        ).first()
        assert payment is not None
        assert payment.status == PaymentStatus.PENDING

        # Held for review, not confirmed.
        event = DarajaCallbackEvent.objects.get(transaction_id="QKH888777A")
        assert event.status == DarajaCallbackStatus.HELD
        assert event.matched_payment_id == payment.id

        outbox = OutboxEvent.objects.filter(
            organization=self.org,
            event_type="payment.verification_required",
            payload__transaction_id="QKH888777A",
        ).first()
        assert outbox is not None
        assert not OutboxEvent.objects.filter(
            organization=self.org,
            event_type="payment.confirmed",
            payload__reference="QKH888777A",
        ).exists()

    def _issued_invoice(self, invoice_number, amount):
        today = timezone.now().date()
        invoice = Invoice.objects.create(
            organization=self.org,
            customer=self.customer,
            invoice_number=invoice_number,
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
            unit_price=amount,
            tax_rate=Decimal("0.0000"),
        )
        return issue_invoice(invoice.id, user=self.user)

    def _initiated_pending_payment(self, invoice, amount, reference):
        return Payment.objects.create(
            organization=self.org,
            payment_number=f"PAY-INIT-{reference}",
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            customer=invoice.customer,
            invoice=invoice,
            amount=amount,
            currency="KES",
            payment_date=timezone.now().date(),
            payment_method=PaymentMethod.MPESA,
            status=PaymentStatus.PENDING,
            reference=reference,
            deposit_account=self.clearing_acc,
        )

    def _settlement_journals(self):
        """Journal entries posted by payment settlement.

        Issuing an invoice legitimately posts a `JE-INV-*` receivable entry, so
        "no journal was posted" must mean "no `JE-PAY-*` settlement entry",
        not "the ledger is empty".
        """
        return JournalEntry.objects.filter(
            organization=self.org, entry_number__startswith="JE-PAY-"
        )

    def test_c2b_confirmation_settles_bound_initiated_payment(self):
        """Only a callback that matches a business-initiated PENDING payment
        (bound by reference, with matching amount/currency/method/invoice) may
        promote to CONFIRMED and post settlement."""
        issued_invoice = self._issued_invoice("INV-BOUND-01", Decimal("12000.0000"))
        self._initiated_pending_payment(
            issued_invoice, Decimal("12000.0000"), "QKH_BOUND_001"
        )

        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "QKH_BOUND_001",
            "TransTime": "20260315143100",
            "TransAmount": "12000.00",
            "BusinessShortCode": "600999",
            "BillRefNumber": "INV-BOUND-01",
            "MSISDN": "254711223344",
        }
        res = self.client.post("/api/v1/integrations/daraja/c2b-confirmation/", payload, format="json")
        assert res.status_code == 200
        assert res.data["ResultCode"] == "0"

        payment = Payment.objects.get(reference="QKH_BOUND_001")
        assert payment.status == PaymentStatus.CONFIRMED
        issued_invoice.refresh_from_db()
        assert issued_invoice.status == InvoiceStatus.PAID
        assert issued_invoice.outstanding_balance == Decimal("0.0000")
        assert OutboxEvent.objects.filter(
            organization=self.org,
            event_type="payment.confirmed",
            payload__reference="QKH_BOUND_001",
        ).exists()

    def test_c2b_confirmation_amount_mismatch_does_not_settle(self):
        issued_invoice = self._issued_invoice("INV-MISMATCH-01", Decimal("12000.0000"))
        self._initiated_pending_payment(
            issued_invoice, Decimal("12000.0000"), "QKH_MISMATCH"
        )

        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "QKH_MISMATCH",
            "TransTime": "20260315143200",
            "TransAmount": "9000.00",
            "BusinessShortCode": "600999",
            "BillRefNumber": "INV-MISMATCH-01",
            "MSISDN": "254711223344",
        }
        res = self.client.post("/api/v1/integrations/daraja/c2b-confirmation/", payload, format="json")
        assert res.status_code == 200
        assert "review" in res.data["ResultDesc"].lower()

        payment = Payment.objects.get(reference="QKH_MISMATCH")
        assert payment.status == PaymentStatus.PENDING
        issued_invoice.refresh_from_db()
        assert issued_invoice.status != InvoiceStatus.PAID

    def test_c2b_callback_unknown_shortcode_is_rejected(self):
        """A forged callback whose short code matches no configured organization
        is unattributable and rejected — it cannot settle anything."""
        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "QKH_FORGED_001",
            "TransTime": "20260315143300",
            "TransAmount": "99999.00",
            "BusinessShortCode": "000000",
            "BillRefNumber": "INV-BOUND-01",
            "MSISDN": "254700000000",
        }
        res = self.client.post("/api/v1/integrations/daraja/c2b-confirmation/", payload, format="json")
        assert res.status_code == 400
        assert not Payment.objects.filter(reference="QKH_FORGED_001").exists()

    def test_daraja_callback_ignores_caller_supplied_organization_header(self):
        """The X-Organization-ID header is never trusted for an unauthenticated
        callback; without a short-code or initiated-payment binding the request
        is rejected even when a valid-looking header is supplied."""
        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "QKH_HDR_FORGE",
            "TransTime": "20260315143400",
            "TransAmount": "12000.00",
            "BillRefNumber": "INV-MPESA-88",
            "MSISDN": "254700000000",
        }
        res = self.client.post(
            "/api/v1/integrations/daraja/c2b-confirmation/",
            payload,
            format="json",
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        assert res.status_code == 400
        assert not Payment.objects.filter(reference="QKH_HDR_FORGE").exists()

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

    def test_c2b_confirmation_ignores_caller_query_parameter(self):
        """The org_id query parameter is not authority for an unauthenticated
        callback; without a short code or initiated payment it is rejected."""
        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "QKH_QUERY_FORGE",
            "TransTime": "20260315150400",
            "TransAmount": "12000.00",
            "BillRefNumber": "INV-QUERY",
            "MSISDN": "254700000000",
        }
        res = self.client.post(
            f"/api/v1/integrations/daraja/c2b-confirmation/?org_id={self.org.id}&organization_id={self.org.id}",
            payload,
            format="json",
        )
        assert res.status_code == 400
        assert not Payment.objects.filter(reference="QKH_QUERY_FORGE").exists()

    def test_c2b_currency_mismatch_cannot_settle(self):
        """A pending payment whose currency does not match the organization's
        cannot be settled by a callback."""
        issued_invoice = self._issued_invoice("INV-CCY-01", Decimal("1000.0000"))
        pending = Payment.objects.create(
            organization=self.org,
            payment_number="PAY-INIT-CCY",
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            customer=issued_invoice.customer,
            invoice=issued_invoice,
            amount=Decimal("1000.0000"),
            currency="USD",
            payment_date=timezone.now().date(),
            payment_method=PaymentMethod.MPESA,
            status=PaymentStatus.PENDING,
            reference="QKH_CCY_01",
            deposit_account=self.clearing_acc,
        )

        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "QKH_CCY_01",
            "TransTime": "20260315150500",
            "TransAmount": "1000.00",
            "BusinessShortCode": "600999",
            "BillRefNumber": "INV-CCY-01",
            "MSISDN": "254711223344",
        }
        res = self.client.post("/api/v1/integrations/daraja/c2b-confirmation/", payload, format="json")
        assert res.status_code == 200

        pending.refresh_from_db()
        assert pending.status == PaymentStatus.PENDING
        issued_invoice.refresh_from_db()
        assert issued_invoice.status == InvoiceStatus.ISSUED
        assert not self._settlement_journals().exists()

    def test_c2b_reference_mismatch_holds_and_posts_nothing(self):
        """A callback whose reference does not match the invoice it names is
        held: no settlement, no journal, one pending payment per reference."""
        issued_invoice = self._issued_invoice("INV-REF-01", Decimal("5000.0000"))

        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "QKH_REF_MISMATCH",
            "TransTime": "20260315150600",
            "TransAmount": "5000.00",
            "BusinessShortCode": "600999",
            "BillRefNumber": "INV-REF-01",
            "MSISDN": "254711223344",
        }
        res = self.client.post("/api/v1/integrations/daraja/c2b-confirmation/", payload, format="json")
        assert res.status_code == 200

        issued_invoice.refresh_from_db()
        assert issued_invoice.status == InvoiceStatus.ISSUED
        assert issued_invoice.paid_amount == Decimal("0.0000")
        assert not self._settlement_journals().exists()
        assert Payment.objects.get(reference="QKH_REF_MISMATCH").status == PaymentStatus.PENDING

    def test_held_callback_is_not_treated_as_trusted_evidence(self):
        """A held callback must not be able to promote a payment through the
        verification service later on."""
        issued_invoice = self._issued_invoice("INV-EVID-01", Decimal("3000.0000"))

        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "QKH_EVID_HELD",
            "TransTime": "20260315150700",
            "TransAmount": "3000.00",
            "BusinessShortCode": "600999",
            "BillRefNumber": "INV-EVID-01",
            "MSISDN": "254711223344",
        }
        self.client.post("/api/v1/integrations/daraja/c2b-confirmation/", payload, format="json")

        result = verify_payment_evidence(
            organization=self.org,
            invoice=issued_invoice,
            amount=Decimal("3000.0000"),
            payment_method="MPESA",
            reference="QKH_EVID_HELD",
        )
        assert result.verified is False
        assert result.evidence_type == "UNVERIFIED"

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

    def test_stk_callback_success_settles_initiated_payment(self):
        """A success callback for a checkout the business initiated (PENDING
        payment bound by CheckoutRequestID, matching amount) settles."""
        issued_invoice = self._issued_invoice("INV-STK-01", Decimal("4500.0000"))
        self._initiated_pending_payment(
            issued_invoice, Decimal("4500.0000"), "ws_CO_19122026102036404"
        )

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
        assert res.data["ResultCode"] == "0"

        callback_event = DarajaCallbackEvent.objects.filter(
            transaction_id="QKH_STK_SUCCESS"
        ).first()
        assert callback_event is not None
        assert callback_event.status == DarajaCallbackStatus.PROCESSED
        assert callback_event.matched_payment is not None
        assert callback_event.matched_payment.status == PaymentStatus.CONFIRMED
        assert callback_event.matched_payment.amount == Decimal("4500.0000")

    def test_stk_callback_without_initiated_payment_is_rejected(self):
        """An STK success callback with no short code and no initiated payment
        cannot be attributed to any organization and is rejected."""
        payload = {
            "Body": {
                "stkCallback": {
                    "MerchantRequestID": "99999-99999999-X",
                    "CheckoutRequestID": "ws_CO_UNKNOWN_00000",
                    "ResultCode": 0,
                    "ResultDesc": "The service request is processed successfully.",
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": 5000.00},
                            {"Name": "MpesaReceiptNumber", "Value": "QKH_STK_ORPHAN"},
                        ]
                    },
                }
            }
        }
        res = self.client.post("/api/v1/integrations/daraja/stk-callback/", payload, format="json")
        assert res.status_code == 400
        assert not Payment.objects.filter(reference="QKH_STK_ORPHAN").exists()

    def test_stk_callback_amount_mismatch_holds_for_verification(self):
        """When the callback amount disagrees with the initiated payment, the
        payment is NOT confirmed; it is held for review."""
        issued_invoice = self._issued_invoice("INV-STK-MM", Decimal("4500.0000"))
        self._initiated_pending_payment(
            issued_invoice, Decimal("4500.0000"), "ws_CO_STK_MM"
        )

        payload = {
            "Body": {
                "stkCallback": {
                    "MerchantRequestID": "29115-34620561-9",
                    "CheckoutRequestID": "ws_CO_STK_MM",
                    "ResultCode": 0,
                    "ResultDesc": "The service request is processed successfully.",
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": 9999.00},
                            {"Name": "MpesaReceiptNumber", "Value": "QKH_STK_MM"},
                        ]
                    },
                }
            }
        }
        res = self.client.post("/api/v1/integrations/daraja/stk-callback/", payload, format="json")
        assert res.status_code == 200
        assert "verification" in res.data["ResultDesc"].lower()

        payment = Payment.objects.get(reference="ws_CO_STK_MM")
        assert payment.status == PaymentStatus.PENDING
        assert not OutboxEvent.objects.filter(
            organization=self.org,
            event_type="payment.confirmed",
            payload__receipt_no="QKH_STK_MM",
        ).exists()

    def test_stk_callback_currency_mismatch_holds_for_verification(self):
        """A callback whose currency does not match the initiated payment is held."""
        issued_invoice = self._issued_invoice("INV-STK-CCY", Decimal("4500.0000"))
        pending = Payment.objects.create(
            organization=self.org,
            payment_number="PAY-INIT-STK-CCY",
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            customer=issued_invoice.customer,
            invoice=issued_invoice,
            amount=Decimal("4500.0000"),
            currency="USD",
            payment_date=timezone.now().date(),
            payment_method=PaymentMethod.MPESA,
            status=PaymentStatus.PENDING,
            reference="ws_CO_STK_CCY",
            deposit_account=self.clearing_acc,
        )

        payload = {
            "Body": {
                "stkCallback": {
                    "MerchantRequestID": "29115-34620561-10",
                    "CheckoutRequestID": "ws_CO_STK_CCY",
                    "ResultCode": 0,
                    "ResultDesc": "The service request is processed successfully.",
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": 4500.00},
                            {"Name": "MpesaReceiptNumber", "Value": "QKH_STK_CCY"},
                        ]
                    },
                }
            }
        }
        res = self.client.post("/api/v1/integrations/daraja/stk-callback/", payload, format="json")
        assert res.status_code == 200
        assert "verification" in res.data["ResultDesc"].lower()

        pending.refresh_from_db()
        assert pending.status == PaymentStatus.PENDING
        issued_invoice.refresh_from_db()
        assert issued_invoice.status == InvoiceStatus.ISSUED
        assert not self._settlement_journals().exists()

    def test_stk_callback_user_cancelled_failure(self):
        # A realistic STK flow: the business first initiates a PENDING payment
        # bound to the checkout id, so the (failed) callback is attributable.
        issued_invoice = self._issued_invoice("INV-STK-CANCEL", Decimal("1000.0000"))
        self._initiated_pending_payment(
            issued_invoice, Decimal("1000.0000"), "ws_CO_CANCELLED_001"
        )
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

        # Nothing was confirmed and no settlement was posted.
        payment = Payment.objects.get(reference="ws_CO_CANCELLED_001")
        assert payment.status == PaymentStatus.PENDING
        assert not self._settlement_journals().exists()
        assert not OutboxEvent.objects.filter(
            organization=self.org, event_type="payment.confirmed"
        ).exists()

    def test_transaction_status_inquiry_never_returns_synthetic_success(self):
        """A synthetic success is not verification, so the client reports that no
        provider check happened rather than shaping a Completed result."""
        client = DarajaClient(config=self.daraja_config)
        res = client.query_transaction_status(transaction_id="QKH_VERIFY_999")
        assert res["TransactionStatus"] == "UNVERIFIED"
        assert res["Verified"] is False
        assert res["ResultCode"] != "0"
        assert res["ReceiptNo"] == "QKH_VERIFY_999"

        # Via API endpoint
        api_res = self.client.post(
            "/api/v1/integrations/daraja/verify-status/",
            {"transaction_id": "QKH_VERIFY_999"},
            format="json",
        )
        assert api_res.status_code == 200
        assert api_res.data["ReceiptNo"] == "QKH_VERIFY_999"
        assert api_res.data["Verified"] is False

    def test_provider_verification_is_not_implemented(self):
        with pytest.raises(DarajaVerificationUnavailable):
            DarajaClient(config=self.daraja_config).verify_payment("QKH_VERIFY_999")

    def _verified_result(self, pending, **overrides):
        values = {
            "organization_id": str(self.org.id),
            "transaction_id": pending.reference,
            "amount": pending.amount,
            "currency": pending.currency,
            "source": "controlled-test-adapter",
        }
        values.update(overrides)
        return VerifiedPaymentResult(**values)

    def test_verified_provider_result_settles_matching_pending_payment(self):
        """The reserved verified-result path is the only automatic settlement
        entry point, and it still re-validates every field."""
        issued_invoice = self._issued_invoice("INV-VERIFIED-01", Decimal("2500.0000"))
        pending = self._initiated_pending_payment(
            issued_invoice, Decimal("2500.0000"), "ws_CO_VERIFIED_01"
        )

        settled = settle_from_verified_result(
            organization=self.org,
            pending_payment=pending,
            evidence=self._verified_result(pending),
        )

        assert settled.status == PaymentStatus.CONFIRMED
        issued_invoice.refresh_from_db()
        assert issued_invoice.status == InvoiceStatus.PAID
        assert self._settlement_journals().count() == 1

    @pytest.mark.parametrize(
        "overrides",
        [
            {"amount": Decimal("999999.0000")},
            {"currency": "USD"},
            {"transaction_id": "ws_CO_SOMETHING_ELSE"},
            {"organization_id": "00000000-0000-0000-0000-000000000000"},
        ],
    )
    def test_verified_result_path_rejects_mismatched_evidence(self, overrides):
        issued_invoice = self._issued_invoice("INV-VERIFIED-02", Decimal("2500.0000"))
        pending = self._initiated_pending_payment(
            issued_invoice, Decimal("2500.0000"), "ws_CO_VERIFIED_02"
        )

        with pytest.raises(DarajaAdapterError):
            settle_from_verified_result(
                organization=self.org,
                pending_payment=pending,
                evidence=self._verified_result(pending, **overrides),
            )

        pending.refresh_from_db()
        assert pending.status == PaymentStatus.PENDING
        issued_invoice.refresh_from_db()
        assert issued_invoice.status == InvoiceStatus.ISSUED
        assert not self._settlement_journals().exists()

    def test_verified_result_path_requires_the_evidence_type(self):
        issued_invoice = self._issued_invoice("INV-VERIFIED-03", Decimal("1000.0000"))
        pending = self._initiated_pending_payment(
            issued_invoice, Decimal("1000.0000"), "ws_CO_VERIFIED_03"
        )
        with pytest.raises(DarajaAdapterError):
            settle_from_verified_result(
                organization=self.org,
                pending_payment=pending,
                evidence={"amount": pending.amount},
            )

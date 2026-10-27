"""Regression tests for the payment PENDING→CONFIRMED lifecycle.

Covers:
  1. PENDING payment promoted to CONFIRMED when Daraja callback arrives later.
  2. Repeated verified callbacks cannot create a second confirmed payment.
  3. PENDING payment stays PENDING when re-verification fails (conflicting amount).
  4. settle_pending_payment() rejects non-PENDING payments (concurrent safety).
  5. settle_pending_payment() rejects cross-organization deposit account.
  6. Complete audit and outbox event chain is emitted on promotion.
  7. Balanced double-entry journal entry is posted on promotion.
  8. Invoice is settled exactly once after promotion.
  9. Repeated MauzoPilot sync of a payment already settled by Daraja is idempotent.
 10. process_collection_with_verification PENDING→CONFIRMED promotion via re-verification.
 11. Daraja C2B callback promotes an existing PENDING payment (reference match).
 12. Daraja C2B callback does not double-settle when payment is already CONFIRMED.
"""

from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from auditlog.models import AuditLog
from automation.models import OutboxEvent
from identity.models import User
from integrations.adapters.daraja import process_c2b_callback
from integrations.models import (
    DarajaCallbackEvent,
    DarajaCallbackStatus,
    DarajaCallbackType,
    ExternalObjectMapping,
    MauzoPilotConfig,
)
from invoicing.models import Customer, Invoice, InvoiceLineItem, InvoiceStatus
from invoicing.services import issue_invoice
from ledger.models import Account, JournalEntry, JournalStatus
from ledger.services.initialization import initialize_organization_accounts
from organizations.models import Membership, Organization, RoleChoices
from payments.models import Payment, PaymentMethod, PaymentStatus, PaymentType
from payments.services import record_and_confirm_customer_payment, settle_pending_payment
from payments.services.verification import process_collection_with_verification


@pytest.mark.django_db
class TestPaymentPendingToConfirmedLifecycle:
    """Regression tests for the PENDING -> CONFIRMED payment lifecycle."""

    def setup_method(self):
        self.user = User.objects.create_user(
            email="lifecycle@avestellar.com", password="Password123!"
        )
        self.org = Organization.objects.create(
            name="Lifecycle Test Org", slug="lifecycle-org", currency="KES"
        )
        Membership.objects.create(
            organization=self.org, user=self.user, role=RoleChoices.ACCOUNTANT
        )
        initialize_organization_accounts(self.org, self.user)

        self.cash_acc = Account.objects.get(organization=self.org, code="1000")
        self.clearing_acc = Account.objects.get(organization=self.org, code="1020")
        self.ar_acc = Account.objects.get(organization=self.org, code="1200")
        self.revenue_acc = Account.objects.get(organization=self.org, code="4000")

        self.customer = Customer.objects.create(
            organization=self.org,
            name="Lifecycle Customer Ltd",
            email="lc@example.co.ke",
            currency="KES",
        )
        self.mp_config = MauzoPilotConfig.objects.create(
            organization=self.org,
            business_id="lc-biz-id",
            api_key="lc-api-key",
            base_url="https://api.mauzopilot.mock/v1",
            webhook_secret="lc-secret",
            is_active=True,
            auto_settle_cash=False,
        )

    def _make_invoice(self, total="15000.00", number="INV-LC-001"):
        invoice = Invoice.objects.create(
            organization=self.org,
            customer=self.customer,
            invoice_number=number,
            issue_date=timezone.now().date(),
            due_date=timezone.now().date(),
            status=InvoiceStatus.DRAFT,
            currency="KES",
        )
        InvoiceLineItem.objects.create(
            invoice=invoice,
            revenue_account=self.revenue_acc,
            description="Service Item",
            quantity=Decimal("1.0000"),
            unit_price=Decimal(total),
            tax_rate=Decimal("0.0000"),
        )
        invoice.subtotal = Decimal(total)
        invoice.total_amount = Decimal(total)
        invoice.outstanding_balance = Decimal(total)
        invoice.save(update_fields=["subtotal", "total_amount", "outstanding_balance"])
        return invoice

    def _make_pending_payment(self, invoice, amount="15000.00",
                              reference="MPESA-PEND-001", pay_num="PAY-PEND-001"):
        return Payment.objects.create(
            organization=self.org,
            payment_number=pay_num,
            payment_type=PaymentType.CUSTOMER_PAYMENT,
            customer=self.customer,
            invoice=invoice,
            amount=Decimal(amount),
            currency="KES",
            payment_date=timezone.now().date(),
            payment_method=PaymentMethod.MPESA,
            status=PaymentStatus.PENDING,
            reference=reference,
            deposit_account=self.clearing_acc,
        )

    # -------------------------------------------------------------------------
    # 1. Happy path
    # -------------------------------------------------------------------------
    def test_settle_pending_payment_promotes_to_confirmed_and_posts_journal(self):
        """settle_pending_payment() issues the invoice, posts a balanced GL entry,
        settles the invoice, emits outbox events, leaves the PENDING record FAILED."""
        invoice = self._make_invoice("20000.00", "INV-LC-SETTLE-1")
        pending = self._make_pending_payment(invoice, "20000.00", "MPESA-SETTLE-01", "PAY-PEND-SETTLE-01")

        confirmed = settle_pending_payment(
            pending_payment=pending,
            deposit_account=self.clearing_acc,
            user=self.user,
            evidence_type="DARAJA_CALLBACK",
            idempotency_key="test:settle:001",
        )

        assert confirmed.status == PaymentStatus.CONFIRMED
        assert confirmed.journal_entry is not None
        assert confirmed.journal_entry.status == JournalStatus.POSTED
        assert confirmed.journal_entry.total_debit == Decimal("20000.0000")
        assert confirmed.journal_entry.total_credit == Decimal("20000.0000")

        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.PAID
        assert invoice.outstanding_balance == Decimal("0.0000")
        assert invoice.paid_amount == Decimal("20000.0000")

        pending.refresh_from_db()
        assert pending.status == PaymentStatus.CONFIRMED
        assert pending.id == confirmed.id
        assert Payment.objects.filter(organization=self.org, invoice=invoice).count() == 1

        assert OutboxEvent.objects.filter(
            organization=self.org,
            event_type="payment.confirmed",
            payload__payment_id=str(confirmed.id),
        ).exists()
        assert OutboxEvent.objects.filter(
            organization=self.org,
            event_type="payment.pending_settled",
            payload__original_pending_payment_id=str(pending.id),
        ).exists()

    # -------------------------------------------------------------------------
    # 2. Duplicate: second call raises, invoice settled exactly once
    # -------------------------------------------------------------------------
    def test_duplicate_settle_call_raises_on_second_attempt(self):
        invoice = self._make_invoice("10000.00", "INV-LC-DUP-1")
        pending = self._make_pending_payment(invoice, "10000.00", "MPESA-DUP-01", "PAY-PEND-DUP-01")

        settle_pending_payment(
            pending_payment=pending,
            deposit_account=self.clearing_acc,
            evidence_type="DARAJA_CALLBACK",
        )

        with pytest.raises(ValueError, match="cannot be promoted"):
            settle_pending_payment(
                pending_payment=pending,
                deposit_account=self.clearing_acc,
                evidence_type="DARAJA_CALLBACK",
            )

        assert Payment.objects.filter(
            organization=self.org, status=PaymentStatus.CONFIRMED, invoice=invoice,
        ).count() == 1
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.PAID
        assert invoice.paid_amount == Decimal("10000.0000")

    @pytest.mark.django_db(transaction=True)
    @pytest.mark.skipif(
        connection.vendor != "postgresql",
        reason="Requires PostgreSQL row locks and independent concurrent transactions.",
    )
    def test_concurrent_pending_settlement_posts_exactly_once(self):
        invoice = self._make_invoice("11000.00", "INV-LC-CONCURRENT-1")
        invoice = issue_invoice(invoice.id, user=self.user)
        pending = self._make_pending_payment(
            invoice, "11000.00", "MPESA-CONCURRENT-01", "PAY-PEND-CONCURRENT-01"
        )
        barrier = Barrier(2)

        def settle_once():
            close_old_connections()
            try:
                worker_payment = Payment.objects.get(id=pending.id, organization=self.org)
                worker_account = Account.objects.get(id=self.clearing_acc.id, organization=self.org)
                barrier.wait(timeout=10)
                try:
                    result = settle_pending_payment(
                        pending_payment=worker_payment,
                        deposit_account=worker_account,
                        evidence_type="DARAJA_CALLBACK",
                        idempotency_key="test:concurrent:pending-payment",
                    )
                    return ("settled", str(result.id))
                except ValueError:
                    return ("already_settled", None)
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _index: settle_once(), range(2)))

        assert sorted(outcome[0] for outcome in outcomes) == ["already_settled", "settled"]
        pending.refresh_from_db()
        invoice.refresh_from_db()
        assert pending.status == PaymentStatus.CONFIRMED
        assert pending.journal_entry is not None
        assert pending.journal_entry.total_debit == pending.journal_entry.total_credit
        assert invoice.status == InvoiceStatus.PAID
        assert invoice.paid_amount == Decimal("11000.0000")
        assert Payment.objects.filter(organization=self.org, invoice=invoice).count() == 1
        assert JournalEntry.objects.filter(
            organization=self.org, entry_number=f"JE-PAY-{pending.payment_number}"
        ).count() == 1

    # -------------------------------------------------------------------------
    # 3. Amount mismatch: re-verification rejects promotion
    # -------------------------------------------------------------------------
    def test_pending_not_promoted_when_daraja_amount_mismatches(self):
        """Daraja callback amount < payment amount: verification must reject promotion."""
        invoice = self._make_invoice("50000.00", "INV-LC-AMT-1")
        pending = self._make_pending_payment(invoice, "50000.00", "MPESA-AMOUNT-01", "PAY-PEND-AMT-01")

        # Daraja callback reports only 30000, not 50000
        DarajaCallbackEvent.objects.create(
            organization=self.org,
            callback_type=DarajaCallbackType.C2B_CONFIRMATION,
            transaction_id="MPESA-AMOUNT-01",
            status=DarajaCallbackStatus.PROCESSED,
            payload={"TransAmount": "30000.00", "TransID": "MPESA-AMOUNT-01"},
        )

        ExternalObjectMapping.objects.create(
            organization=self.org,
            external_system="mauzopilot",
            external_type="payment",
            external_id="EXT-PAY-AMT-01",
            internal_type="payment",
            internal_id=pending.id,
            raw_metadata={"unverified_reason": "Amount mismatch"},
        )

        _result_payment, is_settled = process_collection_with_verification(
            organization=self.org,
            external_payment_data={
                "id": "EXT-PAY-AMT-01",
                "paid_cents": 50000 * 100,
                "payment_reference": "MPESA-AMOUNT-01",
                "payment_method": "MPESA",
            },
        )

        assert is_settled is False
        pending.refresh_from_db()
        assert pending.status == PaymentStatus.PENDING
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.DRAFT

    def test_conflicting_mauzopilot_amount_does_not_promote_existing_mapping(self):
        invoice = self._make_invoice("50000.00", "INV-LC-SYNC-AMT-1")
        pending = self._make_pending_payment(
            invoice, "30000.00", "MPESA-SYNC-AMT-01", "PAY-PEND-SYNC-AMT-01"
        )
        mapping = ExternalObjectMapping.objects.create(
            organization=self.org,
            external_system="mauzopilot",
            external_type="payment",
            external_id="EXT-SYNC-AMT-01",
            internal_type="payment",
            internal_id=pending.id,
            raw_metadata={"unverified_reason": "Awaiting evidence"},
        )
        DarajaCallbackEvent.objects.create(
            organization=self.org,
            callback_type=DarajaCallbackType.C2B_CONFIRMATION,
            transaction_id="MPESA-SYNC-AMT-01",
            status=DarajaCallbackStatus.PROCESSED,
            payload={"TransAmount": "30000.00", "TransID": "MPESA-SYNC-AMT-01"},
        )

        result, settled = process_collection_with_verification(
            organization=self.org,
            external_payment_data={
                "id": "EXT-SYNC-AMT-01",
                "amount": "31000.00",
                "payment_reference": "MPESA-SYNC-AMT-01",
                "payment_method": "MPESA",
            },
        )

        assert settled is False
        assert result.id == pending.id
        pending.refresh_from_db()
        assert pending.status == PaymentStatus.PENDING
        mapping.refresh_from_db()
        assert mapping.internal_id == pending.id
        invoice.refresh_from_db()
        assert invoice.outstanding_balance == Decimal("50000.0000")
        assert Payment.objects.filter(organization=self.org, invoice=invoice).count() == 1

    # -------------------------------------------------------------------------
    # 4. Rejects non-PENDING payments (concurrent safety guard)
    # -------------------------------------------------------------------------
    def test_settle_pending_raises_if_payment_is_already_confirmed(self):
        invoice = self._make_invoice("8000.00", "INV-LC-CNFRM-1")
        issued = issue_invoice(invoice.id, user=self.user)

        confirmed = record_and_confirm_customer_payment(
            invoice_id=issued.id,
            amount=Decimal("8000.00"),
            deposit_account_id=self.clearing_acc.id,
            payment_method=PaymentMethod.MPESA,
            reference="MPESA-CONF-001",
        )
        assert confirmed.status == PaymentStatus.CONFIRMED

        with pytest.raises(ValueError, match="cannot be promoted"):
            settle_pending_payment(
                pending_payment=confirmed,
                deposit_account=self.clearing_acc,
            )

    # -------------------------------------------------------------------------
    # 5. Cross-organization deposit account rejected
    # -------------------------------------------------------------------------
    def test_settle_pending_rejects_cross_org_deposit_account(self):
        other_org = Organization.objects.create(
            name="Other Org", slug="other-org", currency="KES"
        )
        initialize_organization_accounts(other_org, self.user)
        other_clearing = Account.objects.get(organization=other_org, code="1020")

        invoice = self._make_invoice("5000.00", "INV-LC-XORG-1")
        pending = self._make_pending_payment(invoice, "5000.00", "MPESA-XORG-01", "PAY-PEND-XORG-01")

        with pytest.raises(ValueError, match="Cross-organization"):
            settle_pending_payment(
                pending_payment=pending,
                deposit_account=other_clearing,
            )

        pending.refresh_from_db()
        assert pending.status == PaymentStatus.PENDING

    # -------------------------------------------------------------------------
    # 6. Audit log records PENDING_PAYMENT_SETTLED
    # -------------------------------------------------------------------------
    def test_settle_pending_payment_emits_audit_log(self):
        invoice = self._make_invoice("12000.00", "INV-LC-AUDIT-1")
        pending = self._make_pending_payment(invoice, "12000.00", "MPESA-AUDIT-01", "PAY-PEND-AUDIT-01")

        confirmed = settle_pending_payment(
            pending_payment=pending,
            deposit_account=self.clearing_acc,
            user=self.user,
            evidence_type="DARAJA_CALLBACK",
        )

        audit = AuditLog.objects.filter(
            organization=self.org,
            action="PENDING_PAYMENT_SETTLED",
        ).first()
        assert audit is not None
        assert audit.details["confirmed_payment_id"] == str(confirmed.id)
        assert audit.details["original_pending_payment_id"] == str(pending.id)
        assert audit.details["evidence_type"] == "DARAJA_CALLBACK"

    # -------------------------------------------------------------------------
    # 7. ExternalObjectMapping updated after promotion
    # -------------------------------------------------------------------------
    def test_external_mapping_updated_after_pending_promotion(self):
        invoice = self._make_invoice("18000.00", "INV-LC-MAP-1")
        pending = self._make_pending_payment(invoice, "18000.00", "MPESA-MAP-01", "PAY-PEND-MAP-01")

        mapping = ExternalObjectMapping.objects.create(
            organization=self.org,
            external_system="mauzopilot",
            external_type="payment",
            external_id="EXT-MAP-01",
            internal_type="payment",
            internal_id=pending.id,
            raw_metadata={"unverified_reason": "Awaiting Daraja"},
        )

        confirmed = settle_pending_payment(
            pending_payment=pending,
            deposit_account=self.clearing_acc,
        )

        mapping.refresh_from_db()
        assert confirmed.id == pending.id
        assert mapping.internal_id == pending.id

    # -------------------------------------------------------------------------
    # 8. process_collection_with_verification promotes PENDING on re-evaluation
    # -------------------------------------------------------------------------
    def test_process_collection_promotes_pending_when_daraja_evidence_arrives(self):
        invoice = self._make_invoice("25000.00", "INV-LC-RECHECK-1")
        pending = self._make_pending_payment(invoice, "25000.00", "MPESA-RECHECK-01", "PAY-PEND-RECHECK-01")

        ExternalObjectMapping.objects.create(
            organization=self.org,
            external_system="mauzopilot",
            external_type="payment",
            external_id="EXT-RECHECK-01",
            internal_type="payment",
            internal_id=pending.id,
            raw_metadata={"unverified_reason": "No Daraja evidence"},
        )

        DarajaCallbackEvent.objects.create(
            organization=self.org,
            callback_type=DarajaCallbackType.C2B_CONFIRMATION,
            transaction_id="MPESA-RECHECK-01",
            status=DarajaCallbackStatus.PROCESSED,
            payload={"TransAmount": "25000.00", "TransID": "MPESA-RECHECK-01"},
        )

        result_payment, is_settled = process_collection_with_verification(
            organization=self.org,
            external_payment_data={
                "id": "EXT-RECHECK-01",
                "paid_cents": 25000 * 100,
                "payment_reference": "MPESA-RECHECK-01",
                "payment_method": "MPESA",
            },
        )

        assert is_settled is True
        assert result_payment.status == PaymentStatus.CONFIRMED
        assert result_payment.id == pending.id
        assert result_payment.journal_entry is not None
        assert result_payment.journal_entry.status == JournalStatus.POSTED

        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.PAID

        pending.refresh_from_db()
        assert pending.status == PaymentStatus.CONFIRMED

        mapping = ExternalObjectMapping.objects.get(
            organization=self.org, external_type="payment", external_id="EXT-RECHECK-01",
        )
        assert mapping.internal_id == pending.id == result_payment.id

        assert Payment.objects.filter(
            organization=self.org, status=PaymentStatus.CONFIRMED, invoice=invoice,
        ).count() == 1
        assert Payment.objects.filter(organization=self.org, invoice=invoice).count() == 1

    # -------------------------------------------------------------------------
    # 9. Repeated sync after settlement is idempotent
    # -------------------------------------------------------------------------
    def test_repeated_sync_after_daraja_settlement_is_idempotent(self):
        invoice = self._make_invoice("30000.00", "INV-LC-IDMP-1")
        pending = self._make_pending_payment(invoice, "30000.00", "MPESA-IDMP-01", "PAY-PEND-IDMP-01")

        ExternalObjectMapping.objects.create(
            organization=self.org,
            external_system="mauzopilot",
            external_type="payment",
            external_id="EXT-IDMP-01",
            internal_type="payment",
            internal_id=pending.id,
            raw_metadata={},
        )

        DarajaCallbackEvent.objects.create(
            organization=self.org,
            callback_type=DarajaCallbackType.C2B_CONFIRMATION,
            transaction_id="MPESA-IDMP-01",
            status=DarajaCallbackStatus.PROCESSED,
            payload={"TransAmount": "30000.00", "TransID": "MPESA-IDMP-01"},
        )

        p1, settled1 = process_collection_with_verification(
            organization=self.org,
            external_payment_data={
                "id": "EXT-IDMP-01",
                "paid_cents": 30000 * 100,
                "payment_reference": "MPESA-IDMP-01",
                "payment_method": "MPESA",
            },
        )
        assert settled1 is True

        p2, settled2 = process_collection_with_verification(
            organization=self.org,
            external_payment_data={
                "id": "EXT-IDMP-01",
                "paid_cents": 30000 * 100,
                "payment_reference": "MPESA-IDMP-01",
                "payment_method": "MPESA",
            },
        )
        assert settled2 is True
        assert p1.id == p2.id
        assert Payment.objects.filter(
            organization=self.org, status=PaymentStatus.CONFIRMED, invoice=invoice,
        ).count() == 1
        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.PAID
        assert invoice.paid_amount == Decimal("30000.0000")

    # -------------------------------------------------------------------------
    # 10. Daraja C2B promotes existing PENDING payment by reference
    # -------------------------------------------------------------------------
    def test_c2b_callback_promotes_existing_pending_payment_by_reference(self):
        invoice = self._make_invoice("40000.00", "INV-LC-C2B-PEND-1")
        issued_invoice = issue_invoice(invoice.id, user=self.user)

        pending = self._make_pending_payment(
            issued_invoice, "40000.00", "MPESA-C2B-PEND-01", "PAY-PEND-C2B-01"
        )
        mapping = ExternalObjectMapping.objects.create(
            organization=self.org,
            external_system="mauzopilot",
            external_type="payment",
            external_id="EXT-C2B-PEND-01",
            internal_type="payment",
            internal_id=pending.id,
            raw_metadata={"unverified_reason": "Awaiting callback"},
        )

        payload = {
            "TransactionType": "Pay Bill",
            "TransID": "MPESA-C2B-PEND-01",
            "TransTime": "20261027140000",
            "TransAmount": "40000.00",
            "BusinessShortCode": "600000",
            "BillRefNumber": "INV-LC-C2B-PEND-1",
            "MSISDN": "254711223344",
            "FirstName": "Test",
            "LastName": "Customer",
        }

        result = process_c2b_callback(organization=self.org, payload=payload)
        assert result["ResultCode"] == "0"

        issued_invoice.refresh_from_db()
        assert issued_invoice.status == InvoiceStatus.PAID
        assert issued_invoice.outstanding_balance == Decimal("0.0000")

        pending.refresh_from_db()
        assert pending.status == PaymentStatus.CONFIRMED
        assert pending.journal_entry is not None
        assert Payment.objects.filter(organization=self.org, invoice=issued_invoice).count() == 1
        callback_event = DarajaCallbackEvent.objects.get(
            organization=self.org,
            callback_type=DarajaCallbackType.C2B_CONFIRMATION,
            transaction_id="MPESA-C2B-PEND-01",
        )
        assert callback_event.status == DarajaCallbackStatus.PROCESSED
        assert callback_event.matched_payment_id == pending.id
        mapping.refresh_from_db()
        assert mapping.internal_id == pending.id
        assert pending.journal_entry.total_debit == pending.journal_entry.total_credit

        duplicate = process_c2b_callback(organization=self.org, payload=payload)
        assert "Duplicate" in duplicate["ResultDesc"]
        assert Payment.objects.filter(organization=self.org, invoice=issued_invoice).count() == 1
        assert pending.journal_entry.lines.count() == 2

    def test_c2b_conflicting_callback_amount_does_not_create_second_payment(self):
        invoice = self._make_invoice("50000.00", "INV-LC-C2B-AMT-1")
        issued_invoice = issue_invoice(invoice.id, user=self.user)
        pending = self._make_pending_payment(
            issued_invoice, "40000.00", "MPESA-C2B-AMT-01", "PAY-PEND-C2B-AMT-01"
        )

        result = process_c2b_callback(
            organization=self.org,
            payload={
                "TransID": "MPESA-C2B-AMT-01",
                "TransAmount": "30000.00",
                "BillRefNumber": "INV-LC-C2B-AMT-1",
            },
        )

        assert result["ResultCode"] == "0"
        pending.refresh_from_db()
        assert pending.status == PaymentStatus.PENDING
        assert Payment.objects.filter(organization=self.org, invoice=issued_invoice).count() == 1
        event = DarajaCallbackEvent.objects.get(
            organization=self.org,
            callback_type=DarajaCallbackType.C2B_CONFIRMATION,
            transaction_id="MPESA-C2B-AMT-01",
        )
        assert event.status == DarajaCallbackStatus.FAILED

    # -------------------------------------------------------------------------
    # 11. Daraja C2B replay protection: no double-settlement
    # -------------------------------------------------------------------------
    def test_c2b_callback_replay_does_not_double_settle(self):
        invoice = self._make_invoice("15000.00", "INV-LC-REPLAY-1")
        issued = issue_invoice(invoice.id, user=self.user)

        payload = {
            "TransID": "MPESA-REPLAY-01",
            "TransAmount": "15000.00",
            "BillRefNumber": "INV-LC-REPLAY-1",
            "MSISDN": "254711223344",
            "FirstName": "Test",
            "LastName": "Payer",
        }
        r1 = process_c2b_callback(organization=self.org, payload=payload)
        assert r1["ResultCode"] == "0"

        issued.refresh_from_db()
        assert issued.status == InvoiceStatus.PAID

        payment_count_after_first = Payment.objects.filter(
            organization=self.org, invoice=issued
        ).count()

        r2 = process_c2b_callback(organization=self.org, payload=payload)
        assert r2["ResultCode"] == "0"
        assert "Duplicate" in r2["ResultDesc"]

        assert Payment.objects.filter(
            organization=self.org, invoice=issued
        ).count() == payment_count_after_first

        issued.refresh_from_db()
        assert issued.paid_amount == Decimal("15000.0000")

    # -------------------------------------------------------------------------
    # 12. Transaction rollback leaves no partial changes
    # -------------------------------------------------------------------------
    def test_settle_pending_rollback_on_accounting_failure(self):
        """Disabling the AR account causes record_and_confirm_customer_payment to raise.
        The entire atomic transaction must roll back — PENDING payment remains PENDING,
        invoice remains DRAFT, no confirmed payment exists."""
        invoice = self._make_invoice("9000.00", "INV-LC-ROLLBACK-1")
        pending = self._make_pending_payment(invoice, "9000.00", "MPESA-ROLLBACK-01", "PAY-PEND-RB-01")

        ar_account = Account.objects.get(organization=self.org, code="1200")
        ar_account.is_active = False
        ar_account.save(update_fields=["is_active"])

        from ledger.exceptions import AccountingError
        with pytest.raises((AccountingError, Exception)):
            settle_pending_payment(
                pending_payment=pending,
                deposit_account=self.clearing_acc,
                evidence_type="DARAJA_CALLBACK",
            )

        pending.refresh_from_db()
        assert pending.status == PaymentStatus.PENDING

        invoice.refresh_from_db()
        assert invoice.status == InvoiceStatus.DRAFT
        assert invoice.outstanding_balance == Decimal("9000.0000")

        assert Payment.objects.filter(
            organization=self.org, status=PaymentStatus.CONFIRMED, invoice=invoice,
        ).count() == 0
        assert JournalEntry.objects.filter(
            organization=self.org, entry_number=f"JE-PAY-{pending.payment_number}"
        ).count() == 0
        assert not OutboxEvent.objects.filter(
            organization=self.org,
            event_type__in=("payment.confirmed", "payment.pending_settled"),
            payload__original_pending_payment_id=str(pending.id),
        ).exists()
        assert not AuditLog.objects.filter(
            organization=self.org,
            action="PENDING_PAYMENT_SETTLED",
            target_id=str(pending.id),
        ).exists()

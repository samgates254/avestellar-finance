from decimal import Decimal
from unittest.mock import patch
import pytest
from django.core.management import call_command
from rest_framework.test import APIClient

from automation.models import OutboxEvent, OutboxStatus
from identity.models import User
from invoicing.models import Customer, Invoice, InvoiceLineItem, InvoiceStatus
from invoicing.services import issue_invoice
from ledger.models import Account, AccountingPeriod, JournalEntry, JournalLine, JournalStatus
from ledger.services.initialization import initialize_organization_accounts
from organizations.models import Membership, Organization, RoleChoices


@pytest.mark.django_db
class TestRolePermissionsAndRollbacks:
    def setup_method(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(email="owner@avestellar.com", password="Password123!")
        self.viewer = User.objects.create_user(email="viewer@avestellar.com", password="Password123!")
        self.org = Organization.objects.create(name="Roles Org", slug="roles-org", currency="KES")

        Membership.objects.create(organization=self.org, user=self.owner, role=RoleChoices.OWNER)
        Membership.objects.create(organization=self.org, user=self.viewer, role=RoleChoices.VIEWER)
        initialize_organization_accounts(self.org, self.owner)

        self.cash_acc = Account.objects.get(organization=self.org, code="1000")
        self.rev_acc = Account.objects.get(organization=self.org, code="4000")
        self.period = AccountingPeriod.objects.filter(organization=self.org).first()

    def test_viewer_cannot_post_journal_entry(self):
        entry = JournalEntry.objects.create(
            organization=self.org,
            period=self.period,
            entry_number="JE-VIEWER-TEST",
            date=self.period.start_date,
            description="Unauthorized attempt",
            status=JournalStatus.DRAFT,
        )
        JournalLine.objects.create(journal_entry=entry, account=self.cash_acc, debit=Decimal("1000.0000"), credit=Decimal("0.0000"), sequence=1)
        JournalLine.objects.create(journal_entry=entry, account=self.rev_acc, debit=Decimal("0.0000"), credit=Decimal("1000.0000"), sequence=2)

        self.client.force_authenticate(user=self.viewer)
        resp = self.client.post(
            f"/api/v1/ledger/journals/{entry.id}/post/",
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        assert resp.status_code == 403
        entry.refresh_from_db()
        assert entry.status == JournalStatus.DRAFT

    def test_cannot_remove_the_only_owner(self):
        self.client.force_authenticate(user=self.owner)
        resp = self.client.delete(
            f"/api/v1/organizations/{self.org.id}/members/{self.owner.id}/",
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        assert resp.status_code == 400
        assert "Cannot remove the only Owner" in resp.data["detail"]

    def test_transaction_rollback_leaves_no_partial_records_on_error(self):
        cust = Customer.objects.create(organization=self.org, name="Rollback Customer")
        inv = Invoice.objects.create(
            organization=self.org,
            customer=cust,
            invoice_number="INV-FAIL-01",
            issue_date=self.period.start_date,
            due_date=self.period.start_date,
            status=InvoiceStatus.DRAFT,
            currency="KES",
        )
        InvoiceLineItem.objects.create(
            invoice=inv,
            revenue_account=self.rev_acc,
            description="Service with simulated crash",
            quantity=Decimal("1.0000"),
            unit_price=Decimal("10000.0000"),
        )

        initial_journal_count = JournalEntry.objects.filter(organization=self.org).count()
        initial_outbox_count = OutboxEvent.objects.filter(organization=self.org).count()

        # Simulate exception during post_journal_entry inside issue_invoice transaction
        with patch("invoicing.services.post_journal_entry", side_effect=RuntimeError("Simulated DB Crash")):
            with pytest.raises(RuntimeError, match="Simulated DB Crash"):
                issue_invoice(inv.id, user=self.owner)

        # Invariant check: all partial changes rolled back!
        inv.refresh_from_db()
        assert inv.status == InvoiceStatus.DRAFT
        assert inv.journal_entry is None
        assert JournalEntry.objects.filter(organization=self.org).count() == initial_journal_count
        assert OutboxEvent.objects.filter(organization=self.org).count() == initial_outbox_count

    def test_process_outbox_management_command(self):
        OutboxEvent.objects.create(
            event_id="evt-cmd-001",
            event_type="test.event",
            organization=self.org,
            payload={"msg": "hello"},
        )
        call_command("process_outbox")
        evt = OutboxEvent.objects.get(event_id="evt-cmd-001")
        assert evt.status == OutboxStatus.PROCESSED

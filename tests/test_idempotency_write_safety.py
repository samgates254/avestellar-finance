"""Server-enforced replay (idempotency) protection for financial writes.

These tests exercise the real Django/DRF stack against the PostgreSQL test
database (see pytest.ini ``--reuse-db`` + ``.env`` DATABASE_URL). No financial
endpoint is mocked; persisted database state and server-returned accounting
values are used to prove that duplicate/replayed requests cannot duplicate
financial effects.
"""

import threading
from decimal import Decimal

import pytest
from django.db import connection
from rest_framework.test import APIClient

from automation.models import IdempotencyRecord
from identity.models import User
from invoicing.models import Customer
from ledger.models import Account, AccountingPeriod, JournalEntry, PeriodStatus
from ledger.services.initialization import initialize_organization_accounts
from organizations.models import Membership, Organization, RoleChoices

JOURNALS_URL = "/api/v1/ledger/journals/"
INVOICES_URL = "/api/v1/invoicing/invoices/"


def make_context(slug, email, role=RoleChoices.ACCOUNTANT):
    user = User.objects.create_user(email=email, password="Password123!")
    org = Organization.objects.create(name=f"{slug} Org", slug=slug, currency="KES")
    Membership.objects.create(organization=org, user=user, role=role)
    initialize_organization_accounts(org, user)
    return user, org


def journal_payload(entry_number, org):
    cash = Account.objects.get(organization=org, code="1000")
    revenue = Account.objects.get(organization=org, code="4000")
    period = AccountingPeriod.objects.filter(organization=org, status=PeriodStatus.OPEN).first()
    return {
        "entry_number": entry_number,
        "period_id": str(period.id),
        "date": "2026-01-15",
        "description": "Idempotency regression entry",
        "reference": "IDEM-REF",
        "currency": "KES",
        "lines": [
            {"account_id": str(cash.id), "description": "Dr Cash", "debit": "500.0000", "credit": "0.0000", "sequence": 1},
            {"account_id": str(revenue.id), "description": "Cr Revenue", "debit": "0.0000", "credit": "500.0000", "sequence": 2},
        ],
    }


def invoice_payload(invoice_number, org):
    customer = Customer.objects.create(organization=org, name="Idempotency Customer", currency="KES")
    revenue = Account.objects.get(organization=org, code="4000")
    return {
        "invoice_number": invoice_number,
        "customer_id": str(customer.id),
        "issue_date": "2026-01-15",
        "due_date": "2026-02-14",
        "currency": "KES",
        "notes": "Idempotency regression invoice",
        "items": [
            {"revenue_account_id": str(revenue.id), "description": "Service", "quantity": "2.0000", "unit_price": "300.0000", "tax_rate": "0.0000"},
        ],
    }


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
class TestJournalCreationReplayProtection:
    def setup_method(self):
        self.user, self.org = make_context("idem-journal", "idem-journal@avestellar.com")

    def test_identical_retry_replays_original_and_creates_one_entry(self):
        client = client_for(self.user)
        payload = journal_payload("JE-IDEM-REPLAY", self.org)
        headers = {"HTTP_X_ORGANIZATION_ID": str(self.org.id), "HTTP_IDEMPOTENCY_KEY": "key-journal-replay"}

        r1 = client.post(JOURNALS_URL, payload, format="json", **headers)
        assert r1.status_code == 201, r1.data
        r2 = client.post(JOURNALS_URL, payload, format="json", **headers)

        # The retry is served as a replay of the original 201, not a second write.
        assert r2.status_code == 201, r2.data
        assert r2.data["id"] == r1.data["id"]
        assert JournalEntry.objects.filter(organization=self.org, entry_number="JE-IDEM-REPLAY").count() == 1

    def test_same_key_different_payload_is_rejected(self):
        client = client_for(self.user)
        headers = {"HTTP_X_ORGANIZATION_ID": str(self.org.id), "HTTP_IDEMPOTENCY_KEY": "key-journal-conflict"}
        r1 = client.post(JOURNALS_URL, journal_payload("JE-IDEM-CONFLICT-A", self.org), format="json", **headers)
        assert r1.status_code == 201
        r2 = client.post(JOURNALS_URL, journal_payload("JE-IDEM-CONFLICT-B", self.org), format="json", **headers)
        assert r2.status_code == 409
        assert "detail" in r2.data
        # Neither the second entry was created nor the first duplicated.
        assert not JournalEntry.objects.filter(entry_number="JE-IDEM-CONFLICT-B").exists()
        assert JournalEntry.objects.filter(entry_number="JE-IDEM-CONFLICT-A").count() == 1

    def test_failed_creation_does_not_leave_key_stuck(self):
        client = client_for(self.user)
        headers = {"HTTP_X_ORGANIZATION_ID": str(self.org.id), "HTTP_IDEMPOTENCY_KEY": "key-journal-fail"}
        broken = journal_payload("JE-IDEM-FAIL", self.org)
        # Unbalanced lines → serializer validation error before any write.
        broken["lines"][1]["credit"] = "400.0000"
        r1 = client.post(JOURNALS_URL, broken, format="json", **headers)
        assert r1.status_code == 400
        # The reservation must not persist for a rolled-back transaction.
        assert not IdempotencyRecord.objects.filter(organization=self.org, key="key-journal-fail").exists()

        good = journal_payload("JE-IDEM-FAIL", self.org)
        r2 = client.post(JOURNALS_URL, good, format="json", **headers)
        assert r2.status_code == 201, r2.data
        assert JournalEntry.objects.filter(entry_number="JE-IDEM-FAIL").count() == 1

    def test_request_without_key_creates_normally(self):
        client = client_for(self.user)
        payload = journal_payload("JE-IDEM-NOKEY", self.org)
        resp = client.post(JOURNALS_URL, payload, format="json", HTTP_X_ORGANIZATION_ID=str(self.org.id))
        assert resp.status_code == 201
        assert JournalEntry.objects.filter(entry_number="JE-IDEM-NOKEY").count() == 1


@pytest.mark.django_db
class TestInvoiceCreationReplayProtection:
    def setup_method(self):
        self.user, self.org = make_context("idem-invoice", "idem-invoice@avestellar.com")

    def test_identical_retry_replays_original_and_creates_one_invoice(self):
        client = client_for(self.user)
        payload = invoice_payload("INV-IDEM-REPLAY", self.org)
        headers = {"HTTP_X_ORGANIZATION_ID": str(self.org.id), "HTTP_IDEMPOTENCY_KEY": "key-invoice-replay"}

        r1 = client.post(INVOICES_URL, payload, format="json", **headers)
        assert r1.status_code == 201, r1.data
        r2 = client.post(INVOICES_URL, payload, format="json", **headers)
        assert r2.status_code == 201, r2.data
        assert r2.data["id"] == r1.data["id"]

        from invoicing.models import Invoice
        assert Invoice.objects.filter(organization=self.org, invoice_number="INV-IDEM-REPLAY").count() == 1

    def test_same_key_different_payload_is_rejected(self):
        client = client_for(self.user)
        headers = {"HTTP_X_ORGANIZATION_ID": str(self.org.id), "HTTP_IDEMPOTENCY_KEY": "key-invoice-conflict"}
        r1 = client.post(INVOICES_URL, invoice_payload("INV-IDEM-CONFLICT-A", self.org), format="json", **headers)
        assert r1.status_code == 201
        r2 = client.post(INVOICES_URL, invoice_payload("INV-IDEM-CONFLICT-B", self.org), format="json", **headers)
        assert r2.status_code == 409


@pytest.mark.django_db
class TestOrganizationAndActorScoping:
    def test_key_is_scoped_per_organization(self):
        user, org_a = make_context("idem-org-a", "idem-org-a@avestellar.com")
        _, org_b = make_context("idem-org-b", "idem-org-b@avestellar.com")
        # Same actor must belong to both orgs for a clean cross-org comparison.
        Membership.objects.create(organization=org_b, user=user, role=RoleChoices.ACCOUNTANT)
        client = client_for(user)
        key = "key-shared-across-orgs"

        ra = client.post(JOURNALS_URL, journal_payload("JE-IDEM-ORG", org_a), format="json",
                         HTTP_X_ORGANIZATION_ID=str(org_a.id), HTTP_IDEMPOTENCY_KEY=key)
        rb = client.post(JOURNALS_URL, journal_payload("JE-IDEM-ORG", org_b), format="json",
                         HTTP_X_ORGANIZATION_ID=str(org_b.id), HTTP_IDEMPOTENCY_KEY=key)
        assert ra.status_code == 201 and rb.status_code == 201
        # Different organizations → independent results, not a cross-tenant replay.
        assert ra.data["id"] != rb.data["id"]
        assert JournalEntry.objects.filter(organization=org_a, entry_number="JE-IDEM-ORG").count() == 1
        assert JournalEntry.objects.filter(organization=org_b, entry_number="JE-IDEM-ORG").count() == 1

    def test_key_is_scoped_per_actor(self):
        user_a, org = make_context("idem-actor", "idem-actor-a@avestellar.com")
        user_b = User.objects.create_user(email="idem-actor-b@avestellar.com", password="Password123!")
        Membership.objects.create(organization=org, user=user_b, role=RoleChoices.ACCOUNTANT)
        key = "key-shared-across-actors"

        r1 = client_for(user_a).post(JOURNALS_URL, journal_payload("JE-IDEM-ACTOR", org), format="json",
                                     HTTP_X_ORGANIZATION_ID=str(org.id), HTTP_IDEMPOTENCY_KEY=key)
        assert r1.status_code == 201
        # A different authenticated actor reusing the same key must be rejected,
        # never silently served another user's stored result.
        r2 = client_for(user_b).post(JOURNALS_URL, journal_payload("JE-IDEM-ACTOR-2", org), format="json",
                                     HTTP_X_ORGANIZATION_ID=str(org.id), HTTP_IDEMPOTENCY_KEY=key)
        assert r2.status_code == 409


@pytest.mark.django_db(transaction=True)
def test_concurrent_identical_journal_requests_create_exactly_one_entry():
    """Two simultaneous submissions with one key must not duplicate the ledger write.

    Relies on the PostgreSQL ``(organization, key)`` unique constraint to
    serialize the racing transactions.
    """
    user, org = make_context("idem-conc", "idem-conc@avestellar.com")
    payload = journal_payload("JE-IDEM-CONCURRENT", org)
    key = "key-journal-concurrent"
    results = {}
    barrier = threading.Barrier(2)

    def worker(name):
        thread_client = client_for(user)
        try:
            barrier.wait()
            response = thread_client.post(
                JOURNALS_URL, payload, format="json",
                HTTP_X_ORGANIZATION_ID=str(org.id), HTTP_IDEMPOTENCY_KEY=key,
            )
            results[name] = (response.status_code, getattr(response, "data", None))
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert len(results) == 2, results
    # Both callers receive a successful 201 (one real write, one replay)...
    assert all(status == 201 for status, _ in results.values()), results
    # ...but only one journal entry exists and both refer to the same record.
    ids = {data["id"] for _, data in results.values()}
    assert len(ids) == 1
    assert JournalEntry.objects.filter(organization=org, entry_number="JE-IDEM-CONCURRENT").count() == 1
    entry = JournalEntry.objects.get(organization=org, entry_number="JE-IDEM-CONCURRENT")
    # A draft is created (totals are only computed by the backend on posting), and
    # exactly one balanced pair of lines exists - never a duplicated set of lines.
    assert entry.status == "DRAFT"
    lines = list(entry.lines.order_by("sequence"))
    assert len(lines) == 2
    assert sum(line.debit for line in lines) == Decimal("500.0000")
    assert sum(line.credit for line in lines) == Decimal("500.0000")

from decimal import Decimal
import pytest
from rest_framework.test import APIClient

from analytics.services.ai_assistant import AIFinancialAssistantInterface
from analytics.services.financial_analysis import (
    detect_financial_anomalies,
    get_cash_flow_summary,
    get_receivables_aging,
)
from identity.models import User
from invoicing.models import Customer, Invoice, InvoiceLineItem
from invoicing.services import issue_invoice
from ledger.models import Account, AccountingPeriod, JournalEntry, JournalLine, JournalStatus
from ledger.services.initialization import initialize_organization_accounts
from ledger.services.posting import post_journal_entry
from organizations.models import Membership, Organization, RoleChoices


@pytest.mark.django_db
class TestAnalyticsAndSafeAI:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(email="analyst@avestellar.com", password="Password123!")
        self.org = Organization.objects.create(name="Analytics Corp", slug="analytics-corp", currency="KES")
        Membership.objects.create(organization=self.org, user=self.user, role=RoleChoices.ACCOUNTANT)
        initialize_organization_accounts(self.org, self.user)
        self.client.force_authenticate(user=self.user)

        self.cash_acc = Account.objects.get(organization=self.org, code="1000")
        self.rev_acc = Account.objects.get(organization=self.org, code="4000")
        self.period = AccountingPeriod.objects.filter(organization=self.org).first()

    def test_cash_flow_and_aging_analytics(self):
        # Post cash revenue: Dr Cash 200,000 | Cr Revenue 200,000
        je = JournalEntry.objects.create(
            organization=self.org,
            period=self.period,
            entry_number="JE-AN-1",
            date=self.period.start_date,
            description="Cash sales",
            status=JournalStatus.DRAFT,
        )
        JournalLine.objects.create(journal_entry=je, account=self.cash_acc, debit=Decimal("200000.0000"), credit=Decimal("0.0000"), sequence=1)
        JournalLine.objects.create(journal_entry=je, account=self.rev_acc, debit=Decimal("0.0000"), credit=Decimal("200000.0000"), sequence=2)
        post_journal_entry(je.id, user=self.user)

        # Issue customer invoice for 50,000
        cust = Customer.objects.create(organization=self.org, name="Aged Customer")
        inv = Invoice.objects.create(
            organization=self.org,
            customer=cust,
            invoice_number="INV-AN-100",
            issue_date=self.period.start_date,
            due_date=self.period.start_date,
            currency="KES",
        )
        InvoiceLineItem.objects.create(
            invoice=inv,
            revenue_account=self.rev_acc,
            description="Consulting",
            quantity=Decimal("1.0000"),
            unit_price=Decimal("50000.0000"),
        )
        issue_invoice(inv.id, user=self.user)

        # Test Cash Flow Summary
        cf = get_cash_flow_summary(self.org, days_forward=30)
        assert Decimal(cf["verified_current_cash"]) == Decimal("200000.0000")

        # Test Receivables Aging
        aging = get_receivables_aging(self.org)
        assert Decimal(aging["total_receivables"]) == Decimal("50000.0000")

        # Test Safe AI Assistant Interface
        ai = AIFinancialAssistantInterface(organization=self.org)
        summary = ai.generate_management_summary()
        assert "executive_summary" in summary
        assert summary["verified_facts"]["liquid_cash"] == "200000.0000"

        explanation = ai.explain_financial_statement("income_statement")
        assert "verified_totals" in explanation

    def test_analytics_api_endpoints(self):
        resp = self.client.get(
            "/api/v1/analytics/cash-flow/",
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        assert resp.status_code == 200
        assert "verified_current_cash" in resp.data

        summary_resp = self.client.get(
            "/api/v1/analytics/ai/management-summary/",
            HTTP_X_ORGANIZATION_ID=str(self.org.id),
        )
        assert summary_resp.status_code == 200
        assert "executive_summary" in summary_resp.data

    def test_health_check_endpoint(self):
        resp = self.client.get("/api/v1/health/")
        assert resp.status_code == 200
        assert resp.data["status"] == "HEALTHY"
        assert resp.data["database"] == "CONNECTED"

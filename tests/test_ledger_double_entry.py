from datetime import date
from decimal import Decimal
import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from identity.models import User
from ledger.exceptions import AccountingPeriodClosedError, UnbalancedJournalEntryError
from ledger.models import (
    Account,
    AccountCategory,
    AccountingPeriod,
    JournalEntry,
    JournalLine,
    JournalStatus,
    PeriodStatus,
)
from ledger.services.initialization import initialize_organization_accounts
from ledger.services.posting import post_journal_entry, reverse_journal_entry
from ledger.services.reporting import get_balance_sheet, get_income_statement, get_trial_balance
from organizations.models import Membership, Organization, RoleChoices


@pytest.mark.django_db
class TestDoubleEntryAccountingLedger:
    def setup_method(self):
        self.user = User.objects.create_user(email="accountant@avestellar.com", password="Password123!")
        self.org = Organization.objects.create(name="Avestellar Corp", slug="avestellar-corp", currency="KES")
        Membership.objects.create(organization=self.org, user=self.user, role=RoleChoices.ACCOUNTANT)
        initialize_organization_accounts(self.org, self.user)

        self.cash_account = Account.objects.get(organization=self.org, code="1000")
        self.bank_account = Account.objects.get(organization=self.org, code="1010")
        self.ar_account = Account.objects.get(organization=self.org, code="1200")
        self.rev_account = Account.objects.get(organization=self.org, code="4000")
        self.exp_account = Account.objects.get(organization=self.org, code="6000")

        self.period = AccountingPeriod.objects.filter(organization=self.org, status=PeriodStatus.OPEN).first()

    def test_balanced_journal_entry_posts_successfully(self):
        entry = JournalEntry.objects.create(
            organization=self.org,
            period=self.period,
            entry_number="JE-1001",
            date=timezone.now().date(),
            description="Initial cash injection",
            status=JournalStatus.DRAFT,
            created_by=self.user,
        )
        # Dr Cash 50,000 | Cr Revenue 50,000
        JournalLine.objects.create(
            journal_entry=entry, account=self.cash_account, debit=Decimal("50000.0000"), credit=Decimal("0.0000"), sequence=1
        )
        JournalLine.objects.create(
            journal_entry=entry, account=self.rev_account, debit=Decimal("0.0000"), credit=Decimal("50000.0000"), sequence=2
        )

        posted = post_journal_entry(entry.id, user=self.user)
        assert posted.status == JournalStatus.POSTED
        assert posted.total_debit == Decimal("50000.0000")
        assert posted.total_credit == Decimal("50000.0000")
        assert posted.posted_by == self.user
        assert posted.posted_at is not None

    def test_unbalanced_journal_entry_is_rejected(self):
        entry = JournalEntry.objects.create(
            organization=self.org,
            period=self.period,
            entry_number="JE-UNBALANCED",
            date=timezone.now().date(),
            description="Unbalanced attempt",
            status=JournalStatus.DRAFT,
        )
        # Dr 50,000 | Cr 40,000 (diff 10,000)
        JournalLine.objects.create(
            journal_entry=entry, account=self.cash_account, debit=Decimal("50000.0000"), credit=Decimal("0.0000"), sequence=1
        )
        JournalLine.objects.create(
            journal_entry=entry, account=self.rev_account, debit=Decimal("0.0000"), credit=Decimal("40000.0000"), sequence=2
        )

        with pytest.raises(UnbalancedJournalEntryError):
            post_journal_entry(entry.id, user=self.user)

        entry.refresh_from_db()
        assert entry.status == JournalStatus.DRAFT

    def test_posting_into_closed_accounting_period_is_rejected(self):
        # Close period
        self.period.status = PeriodStatus.CLOSED
        self.period.save(update_fields=["status"])

        entry = JournalEntry.objects.create(
            organization=self.org,
            period=self.period,
            entry_number="JE-CLOSED-PERIOD",
            date=timezone.now().date(),
            description="Attempt posting in closed period",
            status=JournalStatus.DRAFT,
        )
        JournalLine.objects.create(
            journal_entry=entry, account=self.cash_account, debit=Decimal("1000.0000"), credit=Decimal("0.0000"), sequence=1
        )
        JournalLine.objects.create(
            journal_entry=entry, account=self.rev_account, debit=Decimal("0.0000"), credit=Decimal("1000.0000"), sequence=2
        )

        with pytest.raises(AccountingPeriodClosedError):
            post_journal_entry(entry.id, user=self.user)

    def test_posted_journal_cannot_be_deleted(self):
        entry = JournalEntry.objects.create(
            organization=self.org,
            period=self.period,
            entry_number="JE-IMMUTABLE",
            date=timezone.now().date(),
            description="Test immutability",
            status=JournalStatus.DRAFT,
        )
        JournalLine.objects.create(
            journal_entry=entry, account=self.cash_account, debit=Decimal("5000.0000"), credit=Decimal("0.0000"), sequence=1
        )
        JournalLine.objects.create(
            journal_entry=entry, account=self.rev_account, debit=Decimal("0.0000"), credit=Decimal("5000.0000"), sequence=2
        )
        post_journal_entry(entry.id, user=self.user)
        entry.refresh_from_db()

        with pytest.raises(ValidationError):
            entry.delete()

    def test_journal_reversal_workflow(self):
        entry = JournalEntry.objects.create(
            organization=self.org,
            period=self.period,
            entry_number="JE-TO-REVERSE",
            date=timezone.now().date(),
            description="Erroneous posting",
            status=JournalStatus.DRAFT,
        )
        JournalLine.objects.create(
            journal_entry=entry, account=self.exp_account, debit=Decimal("12000.0000"), credit=Decimal("0.0000"), sequence=1
        )
        JournalLine.objects.create(
            journal_entry=entry, account=self.bank_account, debit=Decimal("0.0000"), credit=Decimal("12000.0000"), sequence=2
        )
        post_journal_entry(entry.id, user=self.user)

        # Reverse entry
        reversing_entry = reverse_journal_entry(entry.id, reason="Correction of duplicate expense", user=self.user)

        assert reversing_entry.status == JournalStatus.POSTED
        assert reversing_entry.entry_number == f"REV-{entry.entry_number}"

        entry.refresh_from_db()
        assert entry.status == JournalStatus.REVERSED
        assert entry.reversed_entry == reversing_entry

        # Trial balance net movement for exp and bank should be 0.00
        tb = get_trial_balance(self.org)
        assert tb["is_balanced"] is True

    def test_financial_reports_mathematical_consistency(self):
        # Post a revenue entry: Dr Cash 100,000 | Cr Revenue 100,000
        e1 = JournalEntry.objects.create(
            organization=self.org,
            period=self.period,
            entry_number="JE-R1",
            date=timezone.now().date(),
            description="Sales",
            status=JournalStatus.DRAFT,
        )
        JournalLine.objects.create(journal_entry=e1, account=self.cash_account, debit=Decimal("100000.0000"), credit=Decimal("0.0000"), sequence=1)
        JournalLine.objects.create(journal_entry=e1, account=self.rev_account, debit=Decimal("0.0000"), credit=Decimal("100000.0000"), sequence=2)
        post_journal_entry(e1.id, user=self.user)

        # Post an expense entry: Dr Operating Expense 30,000 | Cr Cash 30,000
        e2 = JournalEntry.objects.create(
            organization=self.org,
            period=self.period,
            entry_number="JE-E1",
            date=timezone.now().date(),
            description="Office rent",
            status=JournalStatus.DRAFT,
        )
        JournalLine.objects.create(journal_entry=e2, account=self.exp_account, debit=Decimal("30000.0000"), credit=Decimal("0.0000"), sequence=1)
        JournalLine.objects.create(journal_entry=e2, account=self.cash_account, debit=Decimal("0.0000"), credit=Decimal("30000.0000"), sequence=2)
        post_journal_entry(e2.id, user=self.user)

        # 1. Trial Balance
        tb = get_trial_balance(self.org)
        assert tb["is_balanced"] is True
        assert Decimal(tb["total_debit"]) == Decimal("100000.0000")
        assert Decimal(tb["total_credit"]) == Decimal("100000.0000")

        # 2. Income Statement
        pnl = get_income_statement(self.org)
        assert Decimal(pnl["total_revenue"]) == Decimal("100000.0000")
        assert Decimal(pnl["total_expense"]) == Decimal("30000.0000")
        assert Decimal(pnl["net_income"]) == Decimal("70000.0000")

        # 3. Balance Sheet
        bs = get_balance_sheet(self.org)
        assert bs["is_balanced"] is True
        # Assets: Cash = 100,000 - 30,000 = 70,000
        assert Decimal(bs["total_assets"]) == Decimal("70000.0000")
        # Liabilities = 0, Equity (Retained Earnings) = 70,000
        assert Decimal(bs["total_liabilities_and_equity"]) == Decimal("70000.0000")

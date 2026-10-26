import uuid
from decimal import Decimal
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class AccountCategory(models.TextChoices):
    ASSET = "ASSET", "Asset"
    LIABILITY = "LIABILITY", "Liability"
    EQUITY = "EQUITY", "Equity"
    REVENUE = "REVENUE", "Revenue"
    EXPENSE = "EXPENSE", "Expense"


class NormalBalance(models.TextChoices):
    DEBIT = "DEBIT", "Debit"
    CREDIT = "CREDIT", "Credit"


CATEGORY_NORMAL_BALANCE = {
    AccountCategory.ASSET: NormalBalance.DEBIT,
    AccountCategory.LIABILITY: NormalBalance.CREDIT,
    AccountCategory.EQUITY: NormalBalance.CREDIT,
    AccountCategory.REVENUE: NormalBalance.CREDIT,
    AccountCategory.EXPENSE: NormalBalance.DEBIT,
}


class PeriodStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    CLOSED = "CLOSED", "Closed"
    LOCKED = "LOCKED", "Locked"


class JournalStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    POSTED = "POSTED", "Posted"
    REVERSED = "REVERSED", "Reversed"


class Account(models.Model):
    """Chart of Accounts node representing an individual ledger account."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="accounts",
    )
    code = models.CharField(max_length=50, db_index=True)
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=20, choices=AccountCategory.choices)
    sub_category = models.CharField(max_length=100, blank=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    is_system_account = models.BooleanField(default=False)
    system_role = models.CharField(max_length=50, blank=True, db_index=True)
    currency = models.CharField(max_length=3, default="KES")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ledger_accounts"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "code"], name="unique_org_account_code"
            )
        ]
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} - {self.name} ({self.category})"

    @property
    def normal_balance(self) -> str:
        return CATEGORY_NORMAL_BALANCE.get(self.category, NormalBalance.DEBIT)


class AccountingPeriod(models.Model):
    """Fiscal period during which journal entries can be posted."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="accounting_periods",
    )
    name = models.CharField(max_length=100)
    fiscal_year = models.PositiveIntegerField()
    period_number = models.PositiveSmallIntegerField()
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(
        max_length=20, choices=PeriodStatus.choices, default=PeriodStatus.OPEN
    )
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="closed_periods",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ledger_accounting_periods"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "fiscal_year", "period_number"],
                name="unique_org_fiscal_period",
            )
        ]
        ordering = ["fiscal_year", "period_number"]

    def __str__(self) -> str:
        return f"{self.name} [{self.fiscal_year}-P{self.period_number}] ({self.status})"

    def clean(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValidationError("Period start_date must be before or equal to end_date.")


class JournalEntry(models.Model):
    """General Ledger Journal Entry."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="journal_entries",
    )
    period = models.ForeignKey(
        AccountingPeriod, on_delete=models.PROTECT, related_name="journal_entries"
    )
    entry_number = models.CharField(max_length=100, db_index=True)
    date = models.DateField(db_index=True)
    description = models.TextField()
    reference = models.CharField(max_length=255, blank=True, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=JournalStatus.choices,
        default=JournalStatus.DRAFT,
        db_index=True,
    )
    currency = models.CharField(max_length=3, default="KES")
    total_debit = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    total_credit = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_journals",
    )
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="posted_journals",
    )
    posted_at = models.DateTimeField(null=True, blank=True)
    reversed_entry = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reversal_of",
    )
    is_auto_reversal = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ledger_journal_entries"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "entry_number"],
                name="unique_org_journal_entry_number",
            )
        ]
        ordering = ["-date", "-created_at"]

    def __str__(self) -> str:
        return f"{self.entry_number} ({self.date}) - {self.status} [{self.total_debit}]"

    def delete(self, *args, **kwargs):
        if self.status in (JournalStatus.POSTED, JournalStatus.REVERSED):
            raise ValidationError(
                "Accounting Invariant Violation: Posted or reversed journal entries cannot be deleted."
            )
        return super().delete(*args, **kwargs)


class JournalLine(models.Model):
    """An individual line (debit or credit) on a Journal Entry."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    journal_entry = models.ForeignKey(
        JournalEntry, on_delete=models.CASCADE, related_name="lines"
    )
    account = models.ForeignKey(
        Account, on_delete=models.PROTECT, related_name="journal_lines"
    )
    description = models.CharField(max_length=255, blank=True)
    debit = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    credit = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    sequence = models.PositiveIntegerField(default=1)

    class Meta:
        db_table = "ledger_journal_lines"
        ordering = ["sequence", "id"]

    def __str__(self) -> str:
        return (
            f"Line {self.sequence}: {self.account.code} "
            f"Dr {self.debit} | Cr {self.credit}"
        )

    def clean(self):
        if self.debit < 0 or self.credit < 0:
            raise ValidationError("Debit and Credit amounts must be non-negative.")
        if self.debit > 0 and self.credit > 0:
            raise ValidationError("A journal line cannot have both Debit and Credit amounts.")
        if self.debit == 0 and self.credit == 0:
            raise ValidationError("A journal line must specify either a Debit or Credit amount.")

    def delete(self, *args, **kwargs):
        if self.journal_entry.status in (JournalStatus.POSTED, JournalStatus.REVERSED):
            raise ValidationError("Cannot modify or delete lines of a posted journal entry.")
        return super().delete(*args, **kwargs)

    def save(self, *args, **kwargs):
        if self.journal_entry.status in (JournalStatus.POSTED, JournalStatus.REVERSED):
            raise ValidationError("Cannot modify or add lines to a posted journal entry.")
        self.clean()
        return super().save(*args, **kwargs)

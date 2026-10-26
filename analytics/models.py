import uuid
from decimal import Decimal
from django.db import models


class FinancialClassification(models.TextChoices):
    REVENUE = "REVENUE", "Operating Revenue"
    CONTRA_REVENUE = "CONTRA_REVENUE", "Contra Revenue (Returns & Discounts)"
    COGS = "COGS", "Cost of Goods Sold"
    OPEX_GENERAL = "OPEX_GENERAL", "General Operating Expense"
    OPEX_ADMIN = "OPEX_ADMIN", "Administrative Expense"
    OPEX_SALES_MARKETING = "OPEX_SALES_MARKETING", "Sales & Marketing Expense"
    DEPRECIATION = "DEPRECIATION", "Depreciation Expense"
    AMORTIZATION = "AMORTIZATION", "Amortization Expense"
    INTEREST_EXPENSE = "INTEREST_EXPENSE", "Interest / Finance Expense"
    INTEREST_INCOME = "INTEREST_INCOME", "Interest / Finance Income"
    TAX_EXPENSE = "TAX_EXPENSE", "Income Tax Expense"
    CASH_EQUIVALENT = "CASH_EQUIVALENT", "Cash & Cash Equivalents"
    TRADE_RECEIVABLE = "TRADE_RECEIVABLE", "Trade Accounts Receivable"
    INVENTORY = "INVENTORY", "Merchandise / Materials Inventory"
    OTHER_CURRENT_ASSET = "OTHER_CURRENT_ASSET", "Other Current Assets & Prepayments"
    PPE = "PPE", "Property, Plant & Equipment (Fixed Assets)"
    INTANGIBLE_ASSET = "INTANGIBLE_ASSET", "Intangible Assets"
    TRADE_PAYABLE = "TRADE_PAYABLE", "Trade Accounts Payable"
    CURRENT_LIABILITY = "CURRENT_LIABILITY", "Other Current Liabilities & Accruals"
    SHORT_TERM_DEBT = "SHORT_TERM_DEBT", "Short-Term Debt & Overdrafts"
    LONG_TERM_DEBT = "LONG_TERM_DEBT", "Long-Term Borrowings & Notes"
    EQUITY = "EQUITY", "Share Capital & Retained Earnings"
    CAPEX_ADDITION = "CAPEX_ADDITION", "Capital Asset Additions"


class CostBehavior(models.TextChoices):
    FIXED = "FIXED", "Fixed Cost"
    VARIABLE = "VARIABLE", "Variable Cost"
    SEMI_VARIABLE = "SEMI_VARIABLE", "Semi-Variable Cost"
    UNSPECIFIED = "UNSPECIFIED", "Unspecified"


class AssetCategory(models.TextChoices):
    MACHINERY_EQUIPMENT = "MACHINERY_EQUIPMENT", "Machinery & Equipment"
    IT_INFRASTRUCTURE = "IT_INFRASTRUCTURE", "IT & Technology Infrastructure"
    BUILDINGS_FACILITIES = "BUILDINGS_FACILITIES", "Buildings & Leasehold Improvements"
    VEHICLES = "VEHICLES", "Motor Vehicles & Transport"
    FURNITURE_FIXTURES = "FURNITURE_FIXTURES", "Furniture & Office Fixtures"
    INTANGIBLE_SOFTWARE = "INTANGIBLE_SOFTWARE", "Proprietary Software & IP"
    OTHER = "OTHER", "Other Capital Assets"


class CapexStatus(models.TextChoices):
    PLANNED = "PLANNED", "Planned"
    COMMITTED = "COMMITTED", "Committed"
    INCURRED = "INCURRED", "Incurred"
    CAPITALIZED = "CAPITALIZED", "Capitalized to Fixed Assets"
    CANCELLED = "CANCELLED", "Cancelled"


class AccountClassificationMapping(models.Model):
    """Organization-specific mapping linking a chart of accounts entry to formal financial statement classifications."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="account_classifications",
    )
    account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.CASCADE,
        related_name="classifications",
    )
    classification = models.CharField(
        max_length=50,
        choices=FinancialClassification.choices,
        db_index=True,
    )
    cost_behavior = models.CharField(
        max_length=30,
        choices=CostBehavior.choices,
        default=CostBehavior.UNSPECIFIED,
        db_index=True,
    )
    notes = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "analytics_account_classification_mappings"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "account", "classification"],
                name="unique_org_account_classification",
            )
        ]
        ordering = ["account__code", "classification"]

    def __str__(self) -> str:
        return f"{self.organization.name}: {self.account.code} -> {self.classification} ({self.cost_behavior})"


class CapitalExpenditure(models.Model):
    """Explicit capital expenditure record tracking investment in physical, digital, and fixed assets."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="capital_expenditures",
    )
    project_code = models.CharField(max_length=100, db_index=True)
    name = models.CharField(max_length=255)
    asset_category = models.CharField(
        max_length=50,
        choices=AssetCategory.choices,
        default=AssetCategory.MACHINERY_EQUIPMENT,
    )
    amount = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    currency = models.CharField(max_length=3, default="KES")
    incurred_date = models.DateField(db_index=True)
    status = models.CharField(
        max_length=30,
        choices=CapexStatus.choices,
        default=CapexStatus.INCURRED,
        db_index=True,
    )
    account = models.ForeignKey(
        "ledger.Account",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="capex_records",
    )
    journal_entry = models.ForeignKey(
        "ledger.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="capex_records",
    )
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "analytics_capital_expenditures"
        ordering = ["-incurred_date", "-created_at"]

    def __str__(self) -> str:
        return f"{self.project_code} - {self.name} [{self.amount} {self.currency}] ({self.status})"


class FinancialScenario(models.Model):
    """Audit-tracked financial simulation scenario with parameter assumptions and pro-forma projections."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="financial_scenarios",
    )
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    parameters = models.JSONField(default=dict)
    projections = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "analytics_financial_scenarios"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.name} ({self.organization.name})"

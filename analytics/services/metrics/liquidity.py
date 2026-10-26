from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from django.db.models import Sum
from analytics.models import FinancialClassification
from analytics.services.metrics.account_mapping import AccountMappingService
from analytics.services.metrics.result import CalculationStatus, MetricResult, MetricUnit
from ledger.models import AccountCategory, JournalLine, JournalStatus
from organizations.models import Organization


class LiquidityService:
    """Calculates deterministic working capital, current, quick, cash ratios, and cash runway from posted ledger records."""

    def __init__(self, organization: Organization):
        self.organization = organization
        self.mapping = AccountMappingService(organization)

    def get_current_assets_and_liabilities(self, as_of_date: date | None = None) -> dict[str, Any]:
        """Resolves verified Current Assets and Current Liabilities balances."""
        cash_accs = self.mapping.get_accounts_for_classification(FinancialClassification.CASH_EQUIVALENT)
        ar_accs = self.mapping.get_accounts_for_classification(FinancialClassification.TRADE_RECEIVABLE)
        inv_accs = self.mapping.get_accounts_for_classification(FinancialClassification.INVENTORY)
        other_ca_accs = self.mapping.get_accounts_for_classification(FinancialClassification.OTHER_CURRENT_ASSET)

        cash = self.mapping.calculate_balance_for_accounts(cash_accs, as_of_date=as_of_date)
        ar = self.mapping.calculate_balance_for_accounts(ar_accs, as_of_date=as_of_date)
        inventory = self.mapping.calculate_balance_for_accounts(inv_accs, as_of_date=as_of_date)
        other_ca = self.mapping.calculate_balance_for_accounts(other_ca_accs, as_of_date=as_of_date)

        total_current_assets = cash + ar + inventory + other_ca
        quick_assets = cash + ar

        curr_liab_accs = self.mapping.get_accounts_for_classification(FinancialClassification.CURRENT_LIABILITY)
        total_current_liabilities = self.mapping.calculate_balance_for_accounts(curr_liab_accs, as_of_date=as_of_date)

        return {
            "cash": cash,
            "trade_receivables": ar,
            "inventory": inventory,
            "other_current_assets": other_ca,
            "total_current_assets": total_current_assets,
            "quick_assets": quick_assets,
            "total_current_liabilities": total_current_liabilities,
        }

    def calculate_working_capital(self, as_of_date: date | None = None) -> MetricResult:
        """Working Capital = Current Assets - Current Liabilities."""
        components = self.get_current_assets_and_liabilities(as_of_date=as_of_date)
        ca = components["total_current_assets"]
        cl = components["total_current_liabilities"]
        wc = ca - cl

        return MetricResult(
            metric_id="working_capital",
            name="Net Working Capital",
            status=CalculationStatus.AVAILABLE,
            value=wc,
            formatted_value=f"{self.organization.currency} {wc:,.2f}",
            unit=MetricUnit.CURRENCY,
            currency=self.organization.currency,
            reporting_period={"as_of_date": str(as_of_date) if as_of_date else None},
            inputs={
                "current_assets": ca,
                "current_liabilities": cl,
                "cash": components["cash"],
                "receivables": components["trade_receivables"],
                "inventory": components["inventory"],
            },
            assumptions=["Includes all liquid assets and short-term obligations maturing within 12 months."],
        )

    def calculate_current_ratio(self, as_of_date: date | None = None) -> MetricResult:
        """Current Ratio = Current Assets / Current Liabilities."""
        components = self.get_current_assets_and_liabilities(as_of_date=as_of_date)
        ca = components["total_current_assets"]
        cl = components["total_current_liabilities"]

        if cl <= Decimal("0.0000"):
            return MetricResult(
                metric_id="current_ratio",
                name="Current Ratio",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.RATIO,
                currency=self.organization.currency,
                reporting_period={"as_of_date": str(as_of_date) if as_of_date else None},
                inputs={"current_assets": ca, "current_liabilities": cl},
                warnings=["Current liabilities are zero; ratio is mathematically undefined."],
            )

        ratio = (ca / cl).quantize(Decimal("0.0100"))
        return MetricResult(
            metric_id="current_ratio",
            name="Current Ratio",
            status=CalculationStatus.AVAILABLE,
            value=ratio,
            formatted_value=f"{ratio:.2f}x",
            unit=MetricUnit.RATIO,
            currency=self.organization.currency,
            reporting_period={"as_of_date": str(as_of_date) if as_of_date else None},
            inputs={"current_assets": ca, "current_liabilities": cl},
            assumptions=["Healthy benchmark typically 1.5x - 2.0x."],
        )

    def calculate_quick_ratio(self, as_of_date: date | None = None) -> MetricResult:
        """Quick Ratio (Acid-Test) = (Current Assets - Inventory) / Current Liabilities."""
        components = self.get_current_assets_and_liabilities(as_of_date=as_of_date)
        quick_assets = components["quick_assets"]
        cl = components["total_current_liabilities"]

        if cl <= Decimal("0.0000"):
            return MetricResult(
                metric_id="quick_ratio",
                name="Quick Ratio (Acid-Test)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.RATIO,
                currency=self.organization.currency,
                reporting_period={"as_of_date": str(as_of_date) if as_of_date else None},
                inputs={"quick_assets": quick_assets, "current_liabilities": cl},
                warnings=["Current liabilities are zero; quick ratio is mathematically undefined."],
            )

        ratio = (quick_assets / cl).quantize(Decimal("0.0100"))
        return MetricResult(
            metric_id="quick_ratio",
            name="Quick Ratio (Acid-Test)",
            status=CalculationStatus.AVAILABLE,
            value=ratio,
            formatted_value=f"{ratio:.2f}x",
            unit=MetricUnit.RATIO,
            currency=self.organization.currency,
            reporting_period={"as_of_date": str(as_of_date) if as_of_date else None},
            inputs={
                "quick_assets": quick_assets,
                "inventory_excluded": components["inventory"],
                "current_liabilities": cl,
            },
            assumptions=["Excludes inventory due to delayed liquidity conversion."],
        )

    def calculate_cash_ratio(self, as_of_date: date | None = None) -> MetricResult:
        """Cash Ratio = Cash and Cash Equivalents / Current Liabilities."""
        components = self.get_current_assets_and_liabilities(as_of_date=as_of_date)
        cash = components["cash"]
        cl = components["total_current_liabilities"]

        if cl <= Decimal("0.0000"):
            return MetricResult(
                metric_id="cash_ratio",
                name="Cash Ratio",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.RATIO,
                currency=self.organization.currency,
                inputs={"cash_and_equivalents": cash, "current_liabilities": cl},
                warnings=["Current liabilities are zero; cash ratio is mathematically undefined."],
            )

        ratio = (cash / cl).quantize(Decimal("0.0100"))
        return MetricResult(
            metric_id="cash_ratio",
            name="Cash Ratio",
            status=CalculationStatus.AVAILABLE,
            value=ratio,
            formatted_value=f"{ratio:.2f}x",
            unit=MetricUnit.RATIO,
            currency=self.organization.currency,
            reporting_period={"as_of_date": str(as_of_date) if as_of_date else None},
            inputs={"cash_and_equivalents": cash, "current_liabilities": cl},
            assumptions=["Measures immediate solvency using solely verified cash and clearing accounts."],
        )

    def calculate_cash_runway(
        self,
        as_of_date: date,
        lookback_days: int = 90,
        monthly_burn_rate: Optional[Decimal] = None,
    ) -> MetricResult:
        """Estimates cash runway in months based on net cash burn or explicit burn rate."""
        components = self.get_current_assets_and_liabilities(as_of_date=as_of_date)
        cash = components["cash"]

        if monthly_burn_rate is not None and monthly_burn_rate > Decimal("0.0000"):
            monthly_burn = monthly_burn_rate
        else:
            lookback_start = as_of_date - timedelta(days=lookback_days)
            cash_accs = self.mapping.get_accounts_for_classification(FinancialClassification.CASH_EQUIVALENT)

            # Net cash movement over lookback period
            lines_qs = JournalLine.objects.filter(
                journal_entry__organization=self.organization,
                journal_entry__status=JournalStatus.POSTED,
                journal_entry__date__gte=lookback_start,
                journal_entry__date__lte=as_of_date,
                account__in=cash_accs,
            )
            agg = lines_qs.aggregate(dr=Sum("debit"), cr=Sum("credit"))
            dr = agg["dr"] or Decimal("0.0000")
            cr = agg["cr"] or Decimal("0.0000")

            # Net cash change (inflow - outflow)
            net_flow = dr - cr
            months = Decimal(str(lookback_days)) / Decimal("30.0000")
            monthly_burn = -net_flow / months if net_flow < Decimal("0.0000") else Decimal("0.0000")

        if monthly_burn <= Decimal("0.0000"):
            return MetricResult(
                metric_id="cash_runway",
                name="Cash Runway",
                status=CalculationStatus.AVAILABLE,
                value=Decimal("999.0000"),
                formatted_value="Profitable / Cash Positive",
                unit=MetricUnit.DAYS,
                currency=self.organization.currency,
                inputs={"current_cash": cash, "monthly_burn": monthly_burn},
                assumptions=["Business has net positive cash flow; runway is effectively indefinite under current burn."],
            )

        runway_months = (cash / monthly_burn).quantize(Decimal("0.0001"))
        return MetricResult(
            metric_id="cash_runway",
            name="Cash Runway",
            status=CalculationStatus.AVAILABLE,
            value=runway_months,
            formatted_value=f"{runway_months:.1f} months",
            unit=MetricUnit.DAYS,
            currency=self.organization.currency,
            inputs={"current_cash": cash, "monthly_burn": monthly_burn, "lookback_days": lookback_days},
            assumptions=[f"Assumes historical burn rate over prior {lookback_days} days persists."],
        )

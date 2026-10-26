from datetime import date
from decimal import Decimal
from typing import Any
from analytics.models import FinancialClassification
from analytics.services.metrics.account_mapping import AccountMappingService
from analytics.services.metrics.profitability import ProfitabilityService
from analytics.services.metrics.result import CalculationStatus, MetricResult, MetricUnit
from ledger.models import AccountCategory
from organizations.models import Organization


class LeverageService:
    """Calculates debt, leverage, solvency, and debt service coverage metrics."""

    def __init__(self, organization: Organization):
        self.organization = organization
        self.mapping = AccountMappingService(organization)
        self.profitability = ProfitabilityService(organization)

    def get_debt_components(self, as_of_date: date | None = None) -> dict[str, Any]:
        """Resolves verified Short-Term Debt, Long-Term Debt, Cash, and Book Equity."""
        st_debt_accs = self.mapping.get_accounts_for_classification(FinancialClassification.SHORT_TERM_DEBT)
        lt_debt_accs = self.mapping.get_accounts_for_classification(FinancialClassification.LONG_TERM_DEBT)
        cash_accs = self.mapping.get_accounts_for_classification(FinancialClassification.CASH_EQUIVALENT)
        equity_accs = self.mapping.get_accounts_for_classification(FinancialClassification.EQUITY)
        all_asset_accs = list(self.organization.accounts.filter(category=AccountCategory.ASSET, is_active=True))

        st_debt = self.mapping.calculate_balance_for_accounts(st_debt_accs, as_of_date=as_of_date)
        lt_debt = self.mapping.calculate_balance_for_accounts(lt_debt_accs, as_of_date=as_of_date)
        cash = self.mapping.calculate_balance_for_accounts(cash_accs, as_of_date=as_of_date)
        equity = self.mapping.calculate_balance_for_accounts(equity_accs, as_of_date=as_of_date)
        assets = self.mapping.calculate_balance_for_accounts(all_asset_accs, as_of_date=as_of_date)

        total_debt = st_debt + lt_debt
        net_debt = total_debt - cash

        return {
            "short_term_debt": st_debt,
            "long_term_debt": lt_debt,
            "total_debt": total_debt,
            "cash": cash,
            "net_debt": net_debt,
            "equity": equity,
            "total_assets": assets,
        }

    def calculate_total_debt(self, as_of_date: date | None = None) -> MetricResult:
        """Calculates total gross debt (short-term + long-term borrowings)."""
        comp = self.get_debt_components(as_of_date=as_of_date)
        debt = comp["total_debt"]
        return MetricResult(
            metric_id="total_debt",
            name="Total Debt",
            status=CalculationStatus.AVAILABLE,
            value=debt,
            formatted_value=f"{self.organization.currency} {debt:,.2f}",
            unit=MetricUnit.CURRENCY,
            currency=self.organization.currency,
            reporting_period={"as_of_date": str(as_of_date) if as_of_date else None},
            inputs={
                "short_term_debt": comp["short_term_debt"],
                "long_term_debt": comp["long_term_debt"],
            },
        )

    def calculate_net_debt(self, as_of_date: date | None = None) -> MetricResult:
        """Calculates Net Debt = Total Debt - Cash & Cash Equivalents."""
        comp = self.get_debt_components(as_of_date=as_of_date)
        net_debt = comp["net_debt"]
        return MetricResult(
            metric_id="net_debt",
            name="Net Debt",
            status=CalculationStatus.AVAILABLE,
            value=net_debt,
            formatted_value=f"{self.organization.currency} {net_debt:,.2f}",
            unit=MetricUnit.CURRENCY,
            currency=self.organization.currency,
            reporting_period={"as_of_date": str(as_of_date) if as_of_date else None},
            inputs={
                "total_debt": comp["total_debt"],
                "cash": comp["cash"],
            },
        )

    def calculate_debt_to_equity(self, as_of_date: date | None = None) -> MetricResult:
        """Debt-to-Equity = Total Debt / Total Book Equity."""
        comp = self.get_debt_components(as_of_date=as_of_date)
        debt = comp["total_debt"]
        equity = comp["equity"]

        if equity <= Decimal("0.0000"):
            return MetricResult(
                metric_id="debt_to_equity",
                name="Debt-to-Equity Ratio",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.RATIO,
                currency=self.organization.currency,
                inputs={"total_debt": debt, "equity": equity},
                warnings=["Book equity is zero or negative; ratio is mathematically undefined."],
            )

        ratio = (debt / equity).quantize(Decimal("0.0100"))
        return MetricResult(
            metric_id="debt_to_equity",
            name="Debt-to-Equity Ratio",
            status=CalculationStatus.AVAILABLE,
            value=ratio,
            formatted_value=f"{ratio:.2f}x",
            unit=MetricUnit.RATIO,
            currency=self.organization.currency,
            reporting_period={"as_of_date": str(as_of_date) if as_of_date else None},
            inputs={"total_debt": debt, "equity": equity},
            assumptions=["Measures financial leverage against book value of shareholder equity."],
        )

    def calculate_debt_to_assets(self, as_of_date: date | None = None) -> MetricResult:
        """Debt-to-Assets = Total Debt / Total Assets."""
        comp = self.get_debt_components(as_of_date=as_of_date)
        debt = comp["total_debt"]
        assets = comp["total_assets"]

        if assets <= Decimal("0.0000"):
            return MetricResult(
                metric_id="debt_to_assets",
                name="Debt-to-Assets Ratio",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.RATIO,
                currency=self.organization.currency,
                inputs={"total_debt": debt, "total_assets": assets},
                warnings=["Total assets are zero or negative."],
            )

        ratio = (debt / assets).quantize(Decimal("0.0100"))
        return MetricResult(
            metric_id="debt_to_assets",
            name="Debt-to-Assets Ratio",
            status=CalculationStatus.AVAILABLE,
            value=ratio,
            formatted_value=f"{ratio:.2f}x",
            unit=MetricUnit.RATIO,
            currency=self.organization.currency,
            reporting_period={"as_of_date": str(as_of_date) if as_of_date else None},
            inputs={"total_debt": debt, "total_assets": assets},
        )

    def calculate_interest_coverage(self, start_date: date, end_date: date) -> MetricResult:
        """Interest Coverage = EBIT / Interest Expense."""
        ebit_res = self.profitability.calculate_ebit(start_date, end_date)
        ebit = ebit_res.value or Decimal("0.0000")

        interest_accs = self.mapping.get_accounts_for_classification(FinancialClassification.INTEREST_EXPENSE)
        interest_exp = self.mapping.calculate_balance_for_accounts(interest_accs, start_date=start_date, end_date=end_date)

        if interest_exp <= Decimal("0.0000"):
            return MetricResult(
                metric_id="interest_coverage",
                name="Interest Coverage Ratio",
                status=CalculationStatus.NOT_APPLICABLE,
                value=None,
                formatted_value="Zero Interest Expense",
                unit=MetricUnit.RATIO,
                currency=self.organization.currency,
                inputs={"ebit": ebit, "interest_expense": interest_exp},
                assumptions=["Organization incurred no interest expense during the selected period."],
            )

        ratio = (ebit / interest_exp).quantize(Decimal("0.0100"))
        return MetricResult(
            metric_id="interest_coverage",
            name="Interest Coverage Ratio",
            status=CalculationStatus.AVAILABLE,
            value=ratio,
            formatted_value=f"{ratio:.2f}x",
            unit=MetricUnit.RATIO,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"ebit": ebit, "interest_expense": interest_exp},
            assumptions=["Operating earnings available to cover annual financing obligations."],
        )

    def calculate_net_debt_to_ebitda(self, start_date: date, end_date: date) -> MetricResult:
        """Net Debt to EBITDA = (Total Debt - Cash) / EBITDA."""
        comp = self.get_debt_components(as_of_date=end_date)
        net_debt = comp["net_debt"]

        ebitda_res = self.profitability.calculate_ebitda(start_date, end_date)
        ebitda = ebitda_res.value or Decimal("0.0000")

        if ebitda <= Decimal("0.0000"):
            return MetricResult(
                metric_id="net_debt_to_ebitda",
                name="Net Debt to EBITDA",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.MULTIPLIER,
                currency=self.organization.currency,
                inputs={"net_debt": net_debt, "ebitda": ebitda},
                warnings=["EBITDA is zero or negative."],
            )

        ratio = (net_debt / ebitda).quantize(Decimal("0.0100"))
        return MetricResult(
            metric_id="net_debt_to_ebitda",
            name="Net Debt to EBITDA",
            status=CalculationStatus.AVAILABLE,
            value=ratio,
            formatted_value=f"{ratio:.2f}x",
            unit=MetricUnit.MULTIPLIER,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"net_debt": net_debt, "ebitda": ebitda, "total_debt": comp["total_debt"], "cash": comp["cash"]},
            assumptions=["Measures leverage payback capacity from operating cash flow."],
        )

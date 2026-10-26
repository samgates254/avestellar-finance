from datetime import date
from decimal import Decimal
from typing import Any, Optional
from django.db.models import Sum
from analytics.models import AssetCategory, CapexStatus, CapitalExpenditure, CostBehavior, FinancialClassification
from analytics.services.metrics.account_mapping import AccountMappingService
from analytics.services.metrics.profitability import ProfitabilityService
from analytics.services.metrics.result import CalculationStatus, MetricResult, MetricUnit
from ledger.models import AccountCategory
from organizations.models import Organization


class CapitalAllocationService:
    """Calculates CAPEX, OPEX cost behavior, ROI, ROA, ROE, ROIC, and ROCE metrics."""

    def __init__(self, organization: Organization):
        self.organization = organization
        self.mapping = AccountMappingService(organization)
        self.profitability = ProfitabilityService(organization)

    def calculate_capex(
        self,
        start_date: date,
        end_date: date,
    ) -> MetricResult:
        """Calculates Capital Expenditure (CAPEX) incurred during the period.

        Combines:
        1. Explicit CapitalExpenditure records with status INCURRED or CAPITALIZED.
        2. Debits to PPE / Fixed Asset accounts during the period.
        """
        # 1. Explicit CAPEX records
        capex_qs = CapitalExpenditure.objects.filter(
            organization=self.organization,
            incurred_date__gte=start_date,
            incurred_date__lte=end_date,
            status__in=[CapexStatus.INCURRED, CapexStatus.CAPITALIZED],
        )
        explicit_total = capex_qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.0000")

        # Category breakdown
        category_breakdown = {}
        for cat in AssetCategory.values:
            cat_sum = capex_qs.filter(asset_category=cat).aggregate(total=Sum("amount"))["total"]
            if cat_sum and cat_sum > Decimal("0.0000"):
                category_breakdown[cat] = str(cat_sum)

        # 2. General Ledger PPE net additions (if no explicit records entered)
        ppe_accounts = self.mapping.get_accounts_for_classification(FinancialClassification.PPE)
        ledger_additions = Decimal("0.0000")
        if ppe_accounts:
            ledger_additions = self.mapping.calculate_balance_for_accounts(
                ppe_accounts, start_date=start_date, end_date=end_date
            )

        total_capex = explicit_total if explicit_total > Decimal("0.0000") else max(Decimal("0.0000"), ledger_additions)

        # Ratios: CAPEX to Revenue & CAPEX to Depreciation
        rev_data = self.profitability.calculate_revenue(start_date, end_date)
        net_rev = rev_data["net_revenue"]
        capex_to_rev = (total_capex / net_rev * Decimal("100.0000")) if net_rev > Decimal("0.0000") else None

        depr_accs = self.mapping.get_accounts_for_classification(FinancialClassification.DEPRECIATION)
        depr_amount = self.mapping.calculate_balance_for_accounts(depr_accs, start_date=start_date, end_date=end_date)
        capex_to_depr = (total_capex / depr_amount * Decimal("100.0000")) if depr_amount > Decimal("0.0000") else None

        return MetricResult(
            metric_id="capex",
            name="Capital Expenditure (CAPEX)",
            status=CalculationStatus.AVAILABLE,
            value=total_capex,
            formatted_value=f"{self.organization.currency} {total_capex:,.2f}",
            unit=MetricUnit.CURRENCY,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={
                "explicit_capex_records_total": explicit_total,
                "ledger_fixed_asset_additions": ledger_additions,
                "capex_to_revenue_pct": capex_to_rev,
                "capex_to_depreciation_pct": capex_to_depr,
            },
            source_accounts=[{"code": a.code, "name": a.name, "category": "PPE"} for a in ppe_accounts],
            assumptions=["Includes capitalized equipment, infrastructure, and leasehold investments."],
            metadata={
                "category_breakdown": category_breakdown,
                "capex_to_revenue_pct": str(capex_to_rev.quantize(Decimal("0.0100"))) if capex_to_rev is not None else None,
                "capex_to_depreciation_pct": str(capex_to_depr.quantize(Decimal("0.0100"))) if capex_to_depr is not None else None,
            },
        )

    def calculate_capex_to_revenue(self, start_date: date, end_date: date) -> MetricResult:
        """CAPEX to Revenue % = (CAPEX / Net Revenue) * 100."""
        capex_res = self.calculate_capex(start_date, end_date)
        capex_val = capex_res.value or Decimal("0.0000")
        rev_data = self.profitability.calculate_revenue(start_date, end_date)
        net_rev = rev_data["net_revenue"]
        if net_rev <= Decimal("0.0000"):
            return MetricResult(
                metric_id="capex_to_revenue",
                name="CAPEX to Revenue Ratio",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.PERCENTAGE,
                currency=self.organization.currency,
                reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
                inputs={"capex": capex_val, "net_revenue": net_rev},
                warnings=["Revenue is zero or negative; CAPEX to revenue ratio is undefined."],
            )
        ratio = (capex_val / net_rev * Decimal("100.0000")).quantize(Decimal("0.0100"))
        return MetricResult(
            metric_id="capex_to_revenue",
            name="CAPEX to Revenue Ratio",
            status=CalculationStatus.AVAILABLE,
            value=ratio,
            formatted_value=f"{ratio:.2f}%",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"capex": capex_val, "net_revenue": net_rev},
        )

    def calculate_capex_to_depreciation(self, start_date: date, end_date: date) -> MetricResult:
        """CAPEX to Depreciation % = (CAPEX / Depreciation) * 100."""
        capex_res = self.calculate_capex(start_date, end_date)
        capex_val = capex_res.value or Decimal("0.0000")
        depr_accs = self.mapping.get_accounts_for_classification(FinancialClassification.DEPRECIATION)
        depr = self.mapping.calculate_balance_for_accounts(depr_accs, start_date=start_date, end_date=end_date)
        if depr <= Decimal("0.0000"):
            return MetricResult(
                metric_id="capex_to_depreciation",
                name="CAPEX to Depreciation Ratio",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.PERCENTAGE,
                currency=self.organization.currency,
                reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
                inputs={"capex": capex_val, "depreciation": depr},
                warnings=["Depreciation is zero; CAPEX to depreciation ratio is undefined."],
            )
        ratio = (capex_val / depr * Decimal("100.0000")).quantize(Decimal("0.0100"))
        return MetricResult(
            metric_id="capex_to_depreciation",
            name="CAPEX to Depreciation Ratio",
            status=CalculationStatus.AVAILABLE,
            value=ratio,
            formatted_value=f"{ratio:.2f}%",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"capex": capex_val, "depreciation": depr},
        )

    def calculate_opex_breakdown(
        self,
        start_date: date,
        end_date: date,
    ) -> dict[str, Any]:
        """Calculates operating expense behavior distinguishing fixed vs variable costs."""
        fixed_accs = self.mapping.get_accounts_by_cost_behavior(CostBehavior.FIXED)
        var_accs = self.mapping.get_accounts_by_cost_behavior(CostBehavior.VARIABLE)

        fixed_cost = self.mapping.calculate_balance_for_accounts(fixed_accs, start_date=start_date, end_date=end_date)
        variable_cost = self.mapping.calculate_balance_for_accounts(var_accs, start_date=start_date, end_date=end_date)
        total_cost = fixed_cost + variable_cost

        rev_data = self.profitability.calculate_revenue(start_date, end_date)
        net_rev = rev_data["net_revenue"]

        fixed_pct = (fixed_cost / total_cost * Decimal("100.0000")) if total_cost > Decimal("0.0000") else Decimal("0.0000")
        var_pct = (variable_cost / total_cost * Decimal("100.0000")) if total_cost > Decimal("0.0000") else Decimal("0.0000")

        return {
            "currency": self.organization.currency,
            "total_operating_costs": str(total_cost),
            "total_opex": str(total_cost),
            "fixed_costs": str(fixed_cost),
            "fixed_opex": str(fixed_cost),
            "fixed_costs_pct": str(fixed_pct.quantize(Decimal("0.0100"))),
            "variable_costs": str(variable_cost),
            "variable_opex": str(variable_cost),
            "variable_costs_pct": str(var_pct.quantize(Decimal("0.0100"))),
            "cost_to_revenue_pct": str((total_cost / net_rev * Decimal("100.0000")).quantize(Decimal("0.0100"))) if net_rev > Decimal("0.0000") else None,
        }

    # Alias for method name flexibility
    calculate_fixed_vs_variable_opex = calculate_opex_breakdown

    def calculate_simple_roi(
        self,
        attributable_benefits: Decimal | str | float,
        investment_cost: Decimal | str | float,
        project_name: str = "Investment Project",
    ) -> MetricResult:
        """ROI = (Attributable Benefits - Investment Cost) / Investment Cost * 100."""
        benefits = Decimal(str(attributable_benefits))
        cost = Decimal(str(investment_cost))

        if cost <= Decimal("0.0000"):
            return MetricResult(
                metric_id="simple_roi",
                name=f"Simple ROI ({project_name})",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.PERCENTAGE,
                currency=self.organization.currency,
                inputs={"benefits": benefits, "cost": cost},
                warnings=["Investment cost must be strictly positive to calculate ROI."],
            )

        net_profit = benefits - cost
        roi = (net_profit / cost) * Decimal("100.0000")

        return MetricResult(
            metric_id="simple_roi",
            name=f"Simple ROI ({project_name})",
            status=CalculationStatus.AVAILABLE,
            value=roi.quantize(Decimal("0.0100")),
            formatted_value=f"{roi:.2f}%",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            inputs={
                "investment_cost": cost,
                "attributable_benefits": benefits,
                "net_profit": net_profit,
            },
            assumptions=["Attributable operating benefits measured against total initial outlay."],
        )

    def calculate_roa(self, start_date: date, end_date: date) -> MetricResult:
        """Return on Assets (ROA) = Net Income / Average Total Assets * 100."""
        ebitda_res = self.profitability.calculate_ebitda(start_date, end_date)
        net_income = ebitda_res.inputs["net_income"]

        all_asset_accounts = list(self.organization.accounts.filter(category=AccountCategory.ASSET, is_active=True))
        avg_assets = self.mapping.calculate_average_balance(all_asset_accounts, start_date, end_date)

        if avg_assets <= Decimal("0.0000"):
            return MetricResult(
                metric_id="roa",
                name="Return on Assets (ROA)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.PERCENTAGE,
                currency=self.organization.currency,
                inputs={"net_income": net_income, "average_assets": avg_assets},
                warnings=["Average assets are zero or negative."],
            )

        roa = (net_income / avg_assets) * Decimal("100.0000")
        return MetricResult(
            metric_id="roa",
            name="Return on Assets (ROA)",
            status=CalculationStatus.AVAILABLE,
            value=roa.quantize(Decimal("0.0100")),
            formatted_value=f"{roa:.2f}%",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"net_income": net_income, "average_assets": avg_assets},
            assumptions=["Calculated over average total asset base."],
        )

    def calculate_roe(self, start_date: date, end_date: date) -> MetricResult:
        """Return on Equity (ROE) = Net Income / Average Total Equity * 100."""
        ebitda_res = self.profitability.calculate_ebitda(start_date, end_date)
        net_income = ebitda_res.inputs["net_income"]

        equity_accounts = self.mapping.get_accounts_for_classification(FinancialClassification.EQUITY)
        avg_equity = self.mapping.calculate_average_balance(equity_accounts, start_date, end_date)

        warnings = []
        if avg_equity <= Decimal("0.0000"):
            return MetricResult(
                metric_id="roe",
                name="Return on Equity (ROE)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.PERCENTAGE,
                currency=self.organization.currency,
                inputs={"net_income": net_income, "average_equity": avg_equity},
                warnings=["Zero or negative average equity (insolvent or highly leveraged capital structure); ROE is unavailable or misleading."],
            )

        roe = (net_income / avg_equity) * Decimal("100.0000")
        return MetricResult(
            metric_id="roe",
            name="Return on Equity (ROE)",
            status=CalculationStatus.AVAILABLE,
            value=roe.quantize(Decimal("0.0100")),
            formatted_value=f"{roe:.2f}%",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"net_income": net_income, "average_equity": avg_equity},
            assumptions=["Shareholder return based on average book equity."],
            warnings=warnings,
        )

    def calculate_roic(
        self,
        start_date: date,
        end_date: date,
        effective_tax_rate: Decimal = Decimal("0.3000"),
    ) -> MetricResult:
        """Return on Invested Capital (ROIC) = NOPAT / Average Invested Capital * 100.

        NOPAT = EBIT * (1 - effective_tax_rate)
        Invested Capital = Total Debt + Total Equity - Cash
        """
        ebit_res = self.profitability.calculate_ebit(start_date, end_date)
        ebit = ebit_res.value or Decimal("0.0000")
        nopat = ebit * (Decimal("1.0000") - effective_tax_rate)

        # Invested capital components
        debt_accs = (
            self.mapping.get_accounts_for_classification(FinancialClassification.SHORT_TERM_DEBT)
            + self.mapping.get_accounts_for_classification(FinancialClassification.LONG_TERM_DEBT)
        )
        equity_accs = self.mapping.get_accounts_for_classification(FinancialClassification.EQUITY)
        cash_accs = self.mapping.get_accounts_for_classification(FinancialClassification.CASH_EQUIVALENT)

        avg_debt = self.mapping.calculate_average_balance(debt_accs, start_date, end_date)
        avg_equity = self.mapping.calculate_average_balance(equity_accs, start_date, end_date)
        avg_cash = self.mapping.calculate_average_balance(cash_accs, start_date, end_date)

        invested_capital = avg_debt + avg_equity - avg_cash

        if invested_capital <= Decimal("0.0000"):
            return MetricResult(
                metric_id="roic",
                name="Return on Invested Capital (ROIC)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.PERCENTAGE,
                currency=self.organization.currency,
                inputs={"nopat": nopat, "invested_capital": invested_capital},
                warnings=["Average invested capital is zero or negative."],
            )

        roic = (nopat / invested_capital) * Decimal("100.0000")
        return MetricResult(
            metric_id="roic",
            name="Return on Invested Capital (ROIC)",
            status=CalculationStatus.AVAILABLE,
            value=roic.quantize(Decimal("0.0100")),
            formatted_value=f"{roic:.2f}%",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={
                "ebit": ebit,
                "effective_tax_rate": effective_tax_rate,
                "nopat": nopat,
                "average_debt": avg_debt,
                "average_equity": avg_equity,
                "average_cash": avg_cash,
                "invested_capital": invested_capital,
            },
            assumptions=[f"Uses effective tax rate of {effective_tax_rate * 100:.1f}% to compute NOPAT."],
        )

    def calculate_roce(self, start_date: date, end_date: date) -> MetricResult:
        """Return on Capital Employed (ROCE) = EBIT / Average Capital Employed * 100.

        Capital Employed = Total Assets - Current Liabilities
        """
        ebit_res = self.profitability.calculate_ebit(start_date, end_date)
        ebit = ebit_res.value or Decimal("0.0000")

        all_asset_accs = list(self.organization.accounts.filter(category=AccountCategory.ASSET, is_active=True))
        curr_liab_accs = self.mapping.get_accounts_for_classification(FinancialClassification.CURRENT_LIABILITY)

        avg_assets = self.mapping.calculate_average_balance(all_asset_accs, start_date, end_date)
        avg_curr_liab = self.mapping.calculate_average_balance(curr_liab_accs, start_date, end_date)
        capital_employed = avg_assets - avg_curr_liab

        if capital_employed <= Decimal("0.0000"):
            return MetricResult(
                metric_id="roce",
                name="Return on Capital Employed (ROCE)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.PERCENTAGE,
                currency=self.organization.currency,
                inputs={"ebit": ebit, "capital_employed": capital_employed},
                warnings=["Average capital employed is zero or negative."],
            )

        roce = (ebit / capital_employed) * Decimal("100.0000")
        return MetricResult(
            metric_id="roce",
            name="Return on Capital Employed (ROCE)",
            status=CalculationStatus.AVAILABLE,
            value=roce.quantize(Decimal("0.0100")),
            formatted_value=f"{roce:.2f}%",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={
                "ebit": ebit,
                "average_assets": avg_assets,
                "average_current_liabilities": avg_curr_liab,
                "capital_employed": capital_employed,
            },
            assumptions=["Capital employed defined as total assets less current liabilities."],
        )

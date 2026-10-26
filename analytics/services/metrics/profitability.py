from datetime import date
from decimal import Decimal
from typing import Optional
from analytics.models import FinancialClassification
from analytics.services.metrics.account_mapping import AccountMappingService
from analytics.services.metrics.comparisons import compare_metrics
from analytics.services.metrics.result import CalculationStatus, MetricResult, MetricUnit
from organizations.models import Organization


class ProfitabilityService:
    """Calculates deterministic profitability, income statement, EBITDA, and margin metrics from posted ledger records."""

    def __init__(self, organization: Organization):
        self.organization = organization
        self.mapping = AccountMappingService(organization)

    def calculate_revenue(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        """Calculates Gross Revenue, Contra Revenue, and Net Revenue."""
        gross_accounts = self.mapping.get_accounts_for_classification(FinancialClassification.REVENUE)
        contra_accounts = self.mapping.get_accounts_for_classification(FinancialClassification.CONTRA_REVENUE)

        gross_rev = self.mapping.calculate_balance_for_accounts(gross_accounts, start_date=start_date, end_date=end_date)
        contra_rev = self.mapping.calculate_balance_for_accounts(contra_accounts, start_date=start_date, end_date=end_date)
        net_rev = gross_rev - contra_rev

        return {
            "gross_revenue": gross_rev,
            "contra_revenue": contra_rev,
            "net_revenue": net_rev,
            "source_accounts": [
                {"code": acc.code, "name": acc.name, "category": "REVENUE"} for acc in gross_accounts
            ] + [
                {"code": acc.code, "name": acc.name, "category": "CONTRA_REVENUE"} for acc in contra_accounts
            ],
        }

    def calculate_revenue_growth(
        self,
        current_start: date,
        current_end: date,
        prior_start: date,
        prior_end: date,
    ) -> MetricResult:
        """Calculates revenue growth comparing current period against a prior benchmark period."""
        current_rev = self.calculate_revenue(current_start, current_end)["net_revenue"]
        prior_rev = self.calculate_revenue(prior_start, prior_end)["net_revenue"]

        comparison = compare_metrics(current_rev, prior_rev, metric_name="Revenue", higher_is_better=True)

        status = CalculationStatus.AVAILABLE
        pct_val = Decimal(comparison["percentage_variance"]) if comparison["percentage_variance"] is not None else None
        if pct_val is None:
            status = CalculationStatus.UNDEFINED

        warnings = []
        if comparison.get("warning"):
            warnings.append(comparison["warning"])

        return MetricResult(
            metric_id="revenue_growth",
            name="Revenue Growth Rate",
            status=status,
            value=pct_val,
            formatted_value=f"{pct_val:.2f}%" if pct_val is not None else "N/A",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            reporting_period={
                "current": {"start": str(current_start), "end": str(current_end)},
                "prior": {"start": str(prior_start), "end": str(prior_end)},
            },
            inputs={
                "current_net_revenue": current_rev,
                "prior_net_revenue": prior_rev,
                "absolute_growth": Decimal(comparison["absolute_variance"]),
            },
            assumptions=["Compares net revenue across selected discrete accounting periods."],
            warnings=warnings,
            metadata={"direction": comparison["direction"]},
        )

    def calculate_gross_profit(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> MetricResult:
        """Gross Profit = Net Revenue - Cost of Goods Sold."""
        rev_data = self.calculate_revenue(start_date, end_date)
        net_rev = rev_data["net_revenue"]

        cogs_accounts = self.mapping.get_accounts_for_classification(FinancialClassification.COGS)
        cogs = self.mapping.calculate_balance_for_accounts(cogs_accounts, start_date=start_date, end_date=end_date)

        gp = net_rev - cogs

        return MetricResult(
            metric_id="gross_profit",
            name="Gross Profit",
            status=CalculationStatus.AVAILABLE,
            value=gp,
            formatted_value=f"{self.organization.currency} {gp:,.2f}",
            unit=MetricUnit.CURRENCY,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"net_revenue": net_rev, "cost_of_goods_sold": cogs},
            source_accounts=rev_data["source_accounts"] + [
                {"code": acc.code, "name": acc.name, "category": "COGS"} for acc in cogs_accounts
            ],
            assumptions=["COGS recognized from posted general ledger transactions."],
        )

    def calculate_gross_margin(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> MetricResult:
        """Gross Profit Margin % = (Gross Profit / Net Revenue) * 100."""
        gp_res = self.calculate_gross_profit(start_date, end_date)
        gp = gp_res.value or Decimal("0.0000")
        net_rev = gp_res.inputs["net_revenue"]

        if net_rev <= Decimal("0.0000"):
            return MetricResult(
                metric_id="gross_profit_margin",
                name="Gross Profit Margin",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.PERCENTAGE,
                currency=self.organization.currency,
                reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
                inputs={"gross_profit": gp, "net_revenue": net_rev},
                warnings=["Revenue is zero or negative; gross margin is mathematically undefined."],
            )

        margin = (gp / net_rev) * Decimal("100.0000")
        return MetricResult(
            metric_id="gross_profit_margin",
            name="Gross Profit Margin",
            status=CalculationStatus.AVAILABLE,
            value=margin.quantize(Decimal("0.0100")),
            formatted_value=f"{margin:.2f}%",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"gross_profit": gp, "net_revenue": net_rev},
            source_accounts=gp_res.source_accounts,
        )

    def calculate_operating_expenses(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> MetricResult:
        """Total Operating Expenses (OPEX) excluding COGS, Interest, and Income Tax."""
        opex_accounts = self.mapping.get_accounts_for_classification(FinancialClassification.OPEX_GENERAL)
        opex_total = self.mapping.calculate_balance_for_accounts(opex_accounts, start_date=start_date, end_date=end_date)

        rev_data = self.calculate_revenue(start_date, end_date)
        net_rev = rev_data["net_revenue"]

        opex_ratio = (opex_total / net_rev * Decimal("100.0000")) if net_rev > Decimal("0.0000") else None

        return MetricResult(
            metric_id="operating_expenses",
            name="Operating Expenses (OPEX)",
            status=CalculationStatus.AVAILABLE,
            value=opex_total,
            formatted_value=f"{self.organization.currency} {opex_total:,.2f}",
            unit=MetricUnit.CURRENCY,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={
                "total_opex": opex_total,
                "net_revenue": net_rev,
                "opex_ratio_pct": opex_ratio,
            },
            source_accounts=[
                {"code": acc.code, "name": acc.name, "category": "OPEX"} for acc in opex_accounts
            ],
            assumptions=["Excludes Cost of Goods Sold, financing costs, depreciation, and income taxes."],
        )

    def calculate_ebit(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> MetricResult:
        """EBIT = Net Revenue - COGS - Operating Expenses - Depreciation & Amortization."""
        gp_res = self.calculate_gross_profit(start_date, end_date)
        gp = gp_res.value or Decimal("0.0000")

        opex_res = self.calculate_operating_expenses(start_date, end_date)
        opex = opex_res.value or Decimal("0.0000")

        depr_accs = self.mapping.get_accounts_for_classification(FinancialClassification.DEPRECIATION)
        amort_accs = self.mapping.get_accounts_for_classification(FinancialClassification.AMORTIZATION)
        depr = self.mapping.calculate_balance_for_accounts(depr_accs, start_date=start_date, end_date=end_date)
        amort = self.mapping.calculate_balance_for_accounts(amort_accs, start_date=start_date, end_date=end_date)

        ebit = gp - opex - depr - amort

        return MetricResult(
            metric_id="ebit",
            name="Earnings Before Interest & Taxes (EBIT)",
            status=CalculationStatus.AVAILABLE,
            value=ebit,
            formatted_value=f"{self.organization.currency} {ebit:,.2f}",
            unit=MetricUnit.CURRENCY,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={
                "net_revenue": gp_res.inputs["net_revenue"],
                "gross_profit": gp,
                "operating_expenses": opex,
            },
            source_accounts=gp_res.source_accounts + opex_res.source_accounts,
            assumptions=["Operating profit derived prior to interest and income tax provisions."],
        )

    def calculate_ebitda(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> MetricResult:
        """Calculates EBITDA and provides a full formal reconciliation bridge from Net Income.

        Bridge:
          Net Income
          + Income Tax Expense
          + Interest / Finance Expense
          - Interest Income
          + Depreciation Expense
          + Amortization Expense
          = EBITDA
        """
        ebit_res = self.calculate_ebit(start_date, end_date)
        ebit = ebit_res.value or Decimal("0.0000")

        depr_accs = self.mapping.get_accounts_for_classification(FinancialClassification.DEPRECIATION)
        amort_accs = self.mapping.get_accounts_for_classification(FinancialClassification.AMORTIZATION)
        depr = self.mapping.calculate_balance_for_accounts(depr_accs, start_date=start_date, end_date=end_date)
        amort = self.mapping.calculate_balance_for_accounts(amort_accs, start_date=start_date, end_date=end_date)

        ebitda = ebit + depr + amort

        # Reconcile from Net Income
        interest_exp_accs = self.mapping.get_accounts_for_classification(FinancialClassification.INTEREST_EXPENSE)
        interest_inc_accs = self.mapping.get_accounts_for_classification(FinancialClassification.INTEREST_INCOME)
        tax_accs = self.mapping.get_accounts_for_classification(FinancialClassification.TAX_EXPENSE)

        interest_exp = self.mapping.calculate_balance_for_accounts(interest_exp_accs, start_date=start_date, end_date=end_date)
        interest_inc = self.mapping.calculate_balance_for_accounts(interest_inc_accs, start_date=start_date, end_date=end_date)
        tax_exp = self.mapping.calculate_balance_for_accounts(tax_accs, start_date=start_date, end_date=end_date)

        net_income = ebit - interest_exp + interest_inc - tax_exp

        # Net Revenue for margin
        net_rev = ebit_res.inputs["net_revenue"]
        ebitda_margin = (ebitda / net_rev * Decimal("100.0000")) if net_rev > Decimal("0.0000") else None

        reconciliation_bridge = {
            "reconciled": True,
            "net_income": str(net_income),
            "interest_expense": str(interest_exp),
            "interest_addback": str(interest_exp),
            "income_tax_expense": str(tax_exp),
            "tax_addback": str(tax_exp),
            "depreciation_expense": str(depr),
            "depreciation_addback": str(depr),
            "amortization_expense": str(amort),
            "amortization_addback": str(amort),
            "interest_income": str(interest_inc),
            "ebit": str(ebit),
            "ebitda": str(ebitda),
        }

        return MetricResult(
            metric_id="ebitda",
            name="EBITDA",
            status=CalculationStatus.AVAILABLE,
            value=ebitda,
            formatted_value=f"{self.organization.currency} {ebitda:,.2f}",
            unit=MetricUnit.CURRENCY,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={
                "ebit": ebit,
                "depreciation": depr,
                "amortization": amort,
                "net_income": net_income,
                "ebitda_margin_pct": ebitda_margin,
                "reconciliation_bridge": reconciliation_bridge,
            },
            source_accounts=ebit_res.source_accounts + [
                {"code": a.code, "name": a.name, "category": "DEPRECIATION"} for a in depr_accs
            ] + [
                {"code": a.code, "name": a.name, "category": "AMORTIZATION"} for a in amort_accs
            ],
            assumptions=["Adds back verified non-cash depreciation and amortization to operating profit."],
            metadata={"reconciliation_bridge": reconciliation_bridge},
        )

    def calculate_margin_summary(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        comparison_start_date: date | None = None,
        comparison_end_date: date | None = None,
    ) -> dict:
        """Returns unified profit margins summary with full MetricResult contracts for each tier."""
        rev_data = self.calculate_revenue(start_date, end_date)
        net_rev = rev_data["net_revenue"]

        revenue_metric = MetricResult(
            metric_id="revenue",
            name="Net Revenue",
            status=CalculationStatus.AVAILABLE,
            value=net_rev,
            formatted_value=f"{self.organization.currency} {net_rev:,.2f}",
            unit=MetricUnit.CURRENCY,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"gross_revenue": rev_data["gross_revenue"], "contra_revenue": rev_data["contra_revenue"]},
            source_accounts=rev_data["source_accounts"],
        )

        gp_metric = self.calculate_gross_profit(start_date, end_date)
        gm_metric = self.calculate_gross_margin(start_date, end_date)
        opex_metric = self.calculate_operating_expenses(start_date, end_date)
        ebit_metric = self.calculate_ebit(start_date, end_date)
        ebitda_metric = self.calculate_ebitda(start_date, end_date)

        # OPEX Ratio
        opex_val = opex_metric.value or Decimal("0.0000")
        opex_ratio = (opex_val / net_rev * Decimal("100.0000")) if net_rev > Decimal("0.0000") else None
        opex_ratio_metric = MetricResult(
            metric_id="opex_ratio",
            name="Operating Expense Ratio",
            status=CalculationStatus.AVAILABLE if opex_ratio is not None else CalculationStatus.UNDEFINED,
            value=opex_ratio.quantize(Decimal("0.0100")) if opex_ratio is not None else None,
            formatted_value=f"{opex_ratio:.2f}%" if opex_ratio is not None else "N/A",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"operating_expenses": opex_val, "net_revenue": net_rev},
            warnings=["Revenue is zero or negative; OPEX ratio is undefined."] if opex_ratio is None else [],
        )

        # Operating margin / EBIT margin
        ebit_val = ebit_metric.value or Decimal("0.0000")
        op_margin = (ebit_val / net_rev * Decimal("100.0000")) if net_rev > Decimal("0.0000") else None
        ebit_margin_metric = MetricResult(
            metric_id="ebit_margin",
            name="EBIT / Operating Margin",
            status=CalculationStatus.AVAILABLE if op_margin is not None else CalculationStatus.UNDEFINED,
            value=op_margin.quantize(Decimal("0.0100")) if op_margin is not None else None,
            formatted_value=f"{op_margin:.2f}%" if op_margin is not None else "N/A",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"ebit": ebit_val, "net_revenue": net_rev},
            warnings=["Revenue is zero or negative; EBIT margin is undefined."] if op_margin is None else [],
        )

        # EBITDA margin
        ebitda_val = ebitda_metric.value or Decimal("0.0000")
        ebitda_margin = (ebitda_val / net_rev * Decimal("100.0000")) if net_rev > Decimal("0.0000") else None
        ebitda_margin_metric = MetricResult(
            metric_id="ebitda_margin",
            name="EBITDA Margin",
            status=CalculationStatus.AVAILABLE if ebitda_margin is not None else CalculationStatus.UNDEFINED,
            value=ebitda_margin.quantize(Decimal("0.0100")) if ebitda_margin is not None else None,
            formatted_value=f"{ebitda_margin:.2f}%" if ebitda_margin is not None else "N/A",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"ebitda": ebitda_val, "net_revenue": net_rev},
            warnings=["Revenue is zero or negative; EBITDA margin is undefined."] if ebitda_margin is None else [],
        )

        # Net profit & pre-tax profit
        net_inc_val = ebitda_metric.inputs.get("net_income", Decimal("0.0000"))
        net_profit_metric = MetricResult(
            metric_id="net_profit",
            name="Net Profit / (Loss)",
            status=CalculationStatus.AVAILABLE,
            value=net_inc_val,
            formatted_value=f"{self.organization.currency} {net_inc_val:,.2f}",
            unit=MetricUnit.CURRENCY,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"ebit": ebit_val, "net_income": net_inc_val},
        )

        net_margin = (net_inc_val / net_rev * Decimal("100.0000")) if net_rev > Decimal("0.0000") else None
        net_margin_metric = MetricResult(
            metric_id="net_margin",
            name="Net Profit Margin",
            status=CalculationStatus.AVAILABLE if net_margin is not None else CalculationStatus.UNDEFINED,
            value=net_margin.quantize(Decimal("0.0100")) if net_margin is not None else None,
            formatted_value=f"{net_margin:.2f}%" if net_margin is not None else "N/A",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"net_income": net_inc_val, "net_revenue": net_rev},
            warnings=["Revenue is zero or negative; Net margin is undefined."] if net_margin is None else [],
        )

        # Pre-tax profit
        pre_tax_val = ebit_val  # EBT before non-operating
        pre_tax_metric = MetricResult(
            metric_id="pre_tax_profit",
            name="Pre-Tax Profit (EBT)",
            status=CalculationStatus.AVAILABLE,
            value=pre_tax_val,
            formatted_value=f"{self.organization.currency} {pre_tax_val:,.2f}",
            unit=MetricUnit.CURRENCY,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"pre_tax_profit": pre_tax_val},
        )
        pre_tax_margin_metric = MetricResult(
            metric_id="pre_tax_margin",
            name="Pre-Tax Profit Margin",
            status=CalculationStatus.AVAILABLE if op_margin is not None else CalculationStatus.UNDEFINED,
            value=op_margin.quantize(Decimal("0.0100")) if op_margin is not None else None,
            formatted_value=f"{op_margin:.2f}%" if op_margin is not None else "N/A",
            unit=MetricUnit.PERCENTAGE,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"pre_tax_profit": pre_tax_val, "net_revenue": net_rev},
        )

        # Optional comparison
        comparisons = {}
        if comparison_start_date and comparison_end_date:
            prior_summary = self.calculate_margin_summary(comparison_start_date, comparison_end_date)
            comparisons["revenue_growth"] = compare_metrics(revenue_metric, prior_summary["revenue"])
            comparisons["gross_profit_growth"] = compare_metrics(gp_metric, prior_summary["gross_profit"])
            comparisons["ebitda_growth"] = compare_metrics(ebitda_metric, prior_summary["ebitda"])
            comparisons["net_profit_growth"] = compare_metrics(net_profit_metric, prior_summary["net_profit"])

        bridge = ebitda_metric.metadata.get("reconciliation_bridge", {})

        return {
            "revenue": revenue_metric,
            "gross_profit": gp_metric,
            "gross_margin": gm_metric,
            "operating_expenses": opex_metric,
            "operating_expense_ratio": opex_ratio_metric,
            "operating_profit": ebit_metric,
            "operating_margin": ebit_margin_metric,
            "ebit": ebit_metric,
            "ebit_margin": ebit_margin_metric,
            "pre_tax_profit": pre_tax_metric,
            "pre_tax_margin": pre_tax_margin_metric,
            "net_profit": net_profit_metric,
            "net_margin": net_margin_metric,
            "ebitda": ebitda_metric,
            "ebitda_margin": ebitda_margin_metric,
            "ebitda_reconciliation": bridge,
            "comparisons": comparisons,
        }

    # Alias for flexibility
    calculate_margins_summary = calculate_margin_summary

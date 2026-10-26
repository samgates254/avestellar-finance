"""Operating efficiency and working capital cycles service.

Calculates deterministic Inventory Turnover, DIO, Receivables Turnover, DSO,
Payables Turnover, DPO, Cash Conversion Cycle (CCC), and Asset Turnover.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

from analytics.models import FinancialClassification
from analytics.services.metrics.account_mapping import AccountMappingService
from analytics.services.metrics.profitability import ProfitabilityService
from analytics.services.metrics.result import CalculationStatus, MetricResult, MetricUnit
from ledger.models import AccountCategory
from organizations.models import Organization


class EfficiencyService:
    """Calculates operational turnover, DIO, DSO, DPO, Cash Conversion Cycle, and Asset Turnover."""

    def __init__(self, organization: Organization):
        self.organization = organization
        self.mapping = AccountMappingService(organization)
        self.profitability = ProfitabilityService(organization)

    def calculate_inventory_turnover(self, start_date: date, end_date: date) -> MetricResult:
        """Inventory Turnover = Cost of Goods Sold / Average Inventory."""
        gp_res = self.profitability.calculate_gross_profit(start_date, end_date)
        cogs = gp_res.inputs.get("cost_of_goods_sold", Decimal("0.0000"))

        inv_accs = self.mapping.get_accounts_for_classification(FinancialClassification.INVENTORY)
        avg_inv = self.mapping.calculate_average_balance(inv_accs, start_date, end_date)

        if avg_inv <= Decimal("0.0000"):
            return MetricResult(
                metric_id="inventory_turnover",
                name="Inventory Turnover",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.RATIO,
                currency=self.organization.currency,
                reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
                inputs={"cogs": cogs, "average_inventory": avg_inv},
                warnings=["Average inventory is zero or negative; inventory turnover is undefined."],
            )

        turnover = (cogs / avg_inv).quantize(Decimal("0.0001"))
        return MetricResult(
            metric_id="inventory_turnover",
            name="Inventory Turnover",
            status=CalculationStatus.AVAILABLE,
            value=turnover,
            formatted_value=f"{turnover:.2f}x",
            unit=MetricUnit.RATIO,
            currency=self.organization.currency,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"cogs": cogs, "average_inventory": avg_inv},
        )

    def calculate_dio(self, start_date: date, end_date: date) -> MetricResult:
        """Days Inventory Outstanding (DIO) = (Average Inventory / COGS) * Days in Period."""
        days = Decimal(str((end_date - start_date).days + 1))
        gp_res = self.profitability.calculate_gross_profit(start_date, end_date)
        cogs = gp_res.inputs.get("cost_of_goods_sold", Decimal("0.0000"))

        inv_accs = self.mapping.get_accounts_for_classification(FinancialClassification.INVENTORY)
        avg_inv = self.mapping.calculate_average_balance(inv_accs, start_date, end_date)

        if cogs <= Decimal("0.0000"):
            return MetricResult(
                metric_id="days_inventory_outstanding",
                name="Days Inventory Outstanding (DIO)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.DAYS,
                reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
                inputs={"cogs": cogs, "average_inventory": avg_inv, "days": days},
                warnings=["COGS is zero or negative; DIO is undefined."],
            )

        dio = ((avg_inv / cogs) * days).quantize(Decimal("0.0001"))
        return MetricResult(
            metric_id="days_inventory_outstanding",
            name="Days Inventory Outstanding (DIO)",
            status=CalculationStatus.AVAILABLE,
            value=dio,
            formatted_value=f"{dio:.1f} days",
            unit=MetricUnit.DAYS,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"cogs": cogs, "average_inventory": avg_inv, "days": days},
        )

    def calculate_receivables_turnover(self, start_date: date, end_date: date) -> MetricResult:
        """Receivables Turnover = Net Revenue / Average Accounts Receivable."""
        rev_data = self.profitability.calculate_revenue(start_date, end_date)
        net_rev = rev_data["net_revenue"]

        ar_accs = self.mapping.get_accounts_for_classification(FinancialClassification.TRADE_RECEIVABLE)
        avg_ar = self.mapping.calculate_average_balance(ar_accs, start_date, end_date)

        if avg_ar <= Decimal("0.0000"):
            return MetricResult(
                metric_id="receivables_turnover",
                name="Receivables Turnover",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.RATIO,
                reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
                inputs={"net_revenue": net_rev, "average_receivables": avg_ar},
                warnings=["Average receivables is zero or negative; turnover is undefined."],
            )

        turnover = (net_rev / avg_ar).quantize(Decimal("0.0001"))
        return MetricResult(
            metric_id="receivables_turnover",
            name="Receivables Turnover",
            status=CalculationStatus.AVAILABLE,
            value=turnover,
            formatted_value=f"{turnover:.2f}x",
            unit=MetricUnit.RATIO,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"net_revenue": net_rev, "average_receivables": avg_ar},
        )

    def calculate_dso(self, start_date: date, end_date: date) -> MetricResult:
        """Days Sales Outstanding (DSO) = (Average Accounts Receivable / Net Revenue) * Days."""
        days = Decimal(str((end_date - start_date).days + 1))
        rev_data = self.profitability.calculate_revenue(start_date, end_date)
        net_rev = rev_data["net_revenue"]

        ar_accs = self.mapping.get_accounts_for_classification(FinancialClassification.TRADE_RECEIVABLE)
        avg_ar = self.mapping.calculate_average_balance(ar_accs, start_date, end_date)

        if net_rev <= Decimal("0.0000"):
            return MetricResult(
                metric_id="days_sales_outstanding",
                name="Days Sales Outstanding (DSO)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.DAYS,
                reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
                inputs={"net_revenue": net_rev, "average_receivables": avg_ar, "days": days},
                warnings=["Net revenue is zero or negative; DSO is undefined."],
            )

        dso = ((avg_ar / net_rev) * days).quantize(Decimal("0.0001"))
        return MetricResult(
            metric_id="days_sales_outstanding",
            name="Days Sales Outstanding (DSO)",
            status=CalculationStatus.AVAILABLE,
            value=dso,
            formatted_value=f"{dso:.1f} days",
            unit=MetricUnit.DAYS,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"net_revenue": net_rev, "average_receivables": avg_ar, "days": days},
        )

    def calculate_payables_turnover(self, start_date: date, end_date: date) -> MetricResult:
        """Payables Turnover = COGS / Average Accounts Payable."""
        gp_res = self.profitability.calculate_gross_profit(start_date, end_date)
        cogs = gp_res.inputs.get("cost_of_goods_sold", Decimal("0.0000"))

        ap_accs = self.mapping.get_accounts_for_classification(FinancialClassification.TRADE_PAYABLE)
        avg_ap = self.mapping.calculate_average_balance(ap_accs, start_date, end_date)

        if avg_ap <= Decimal("0.0000"):
            return MetricResult(
                metric_id="payables_turnover",
                name="Payables Turnover",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.RATIO,
                reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
                inputs={"cogs": cogs, "average_payables": avg_ap},
                warnings=["Average payables is zero or negative; turnover is undefined."],
            )

        turnover = (cogs / avg_ap).quantize(Decimal("0.0001"))
        return MetricResult(
            metric_id="payables_turnover",
            name="Payables Turnover",
            status=CalculationStatus.AVAILABLE,
            value=turnover,
            formatted_value=f"{turnover:.2f}x",
            unit=MetricUnit.RATIO,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"cogs": cogs, "average_payables": avg_ap},
        )

    def calculate_dpo(self, start_date: date, end_date: date) -> MetricResult:
        """Days Payable Outstanding (DPO) = (Average Accounts Payable / COGS) * Days."""
        days = Decimal(str((end_date - start_date).days + 1))
        gp_res = self.profitability.calculate_gross_profit(start_date, end_date)
        cogs = gp_res.inputs.get("cost_of_goods_sold", Decimal("0.0000"))

        ap_accs = self.mapping.get_accounts_for_classification(FinancialClassification.TRADE_PAYABLE)
        avg_ap = self.mapping.calculate_average_balance(ap_accs, start_date, end_date)

        if cogs <= Decimal("0.0000"):
            return MetricResult(
                metric_id="days_payable_outstanding",
                name="Days Payable Outstanding (DPO)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.DAYS,
                reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
                inputs={"cogs": cogs, "average_payables": avg_ap, "days": days},
                warnings=["COGS is zero or negative; DPO is undefined."],
            )

        dpo = ((avg_ap / cogs) * days).quantize(Decimal("0.0001"))
        return MetricResult(
            metric_id="days_payable_outstanding",
            name="Days Payable Outstanding (DPO)",
            status=CalculationStatus.AVAILABLE,
            value=dpo,
            formatted_value=f"{dpo:.1f} days",
            unit=MetricUnit.DAYS,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"cogs": cogs, "average_payables": avg_ap, "days": days},
        )

    def calculate_cash_conversion_cycle(self, start_date: date, end_date: date) -> MetricResult:
        """Cash Conversion Cycle (CCC) = DIO + DSO - DPO."""
        dio_res = self.calculate_dio(start_date, end_date)
        dso_res = self.calculate_dso(start_date, end_date)
        dpo_res = self.calculate_dpo(start_date, end_date)

        if dio_res.value is not None and dso_res.value is not None and dpo_res.value is not None:
            ccc = (dio_res.value + dso_res.value - dpo_res.value).quantize(Decimal("0.0001"))
            return MetricResult(
                metric_id="cash_conversion_cycle",
                name="Cash Conversion Cycle (CCC)",
                status=CalculationStatus.AVAILABLE,
                value=ccc,
                formatted_value=f"{ccc:.1f} days",
                unit=MetricUnit.DAYS,
                reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
                inputs={"dio": dio_res.value, "dso": dso_res.value, "dpo": dpo_res.value},
            )
        else:
            return MetricResult(
                metric_id="cash_conversion_cycle",
                name="Cash Conversion Cycle (CCC)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.DAYS,
                reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
                inputs={"dio": dio_res.value, "dso": dso_res.value, "dpo": dpo_res.value},
                warnings=["One or more cycle components (DIO, DSO, DPO) could not be calculated."],
            )

    def calculate_asset_turnover(self, start_date: date, end_date: date) -> MetricResult:
        """Total Asset Turnover = Net Revenue / Average Total Assets."""
        rev_data = self.profitability.calculate_revenue(start_date, end_date)
        net_rev = rev_data["net_revenue"]

        asset_accs = list(self.organization.accounts.filter(category=AccountCategory.ASSET, is_active=True))
        avg_assets = self.mapping.calculate_average_balance(asset_accs, start_date, end_date)

        if avg_assets <= Decimal("0.0000"):
            return MetricResult(
                metric_id="total_asset_turnover",
                name="Total Asset Turnover",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.RATIO,
                reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
                inputs={"net_revenue": net_rev, "average_assets": avg_assets},
                warnings=["Average assets are zero; asset turnover is undefined."],
            )

        turnover = (net_rev / avg_assets).quantize(Decimal("0.0001"))
        return MetricResult(
            metric_id="total_asset_turnover",
            name="Total Asset Turnover",
            status=CalculationStatus.AVAILABLE,
            value=turnover,
            formatted_value=f"{turnover:.2f}x",
            unit=MetricUnit.RATIO,
            reporting_period={"start_date": str(start_date), "end_date": str(end_date)},
            inputs={"net_revenue": net_rev, "average_assets": avg_assets},
        )

    def calculate_efficiency_metrics(self, start_date: date, end_date: date) -> dict[str, Any]:
        """Calculates all operational efficiency ratios across a designated period."""
        return {
            "inventory_turnover": self.calculate_inventory_turnover(start_date, end_date).to_dict(),
            "days_inventory_outstanding": self.calculate_dio(start_date, end_date).to_dict(),
            "receivables_turnover": self.calculate_receivables_turnover(start_date, end_date).to_dict(),
            "days_sales_outstanding": self.calculate_dso(start_date, end_date).to_dict(),
            "payables_turnover": self.calculate_payables_turnover(start_date, end_date).to_dict(),
            "days_payable_outstanding": self.calculate_dpo(start_date, end_date).to_dict(),
            "cash_conversion_cycle": self.calculate_cash_conversion_cycle(start_date, end_date).to_dict(),
            "total_asset_turnover": self.calculate_asset_turnover(start_date, end_date).to_dict(),
        }

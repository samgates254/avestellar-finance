"""Financial dashboard aggregation service.

Combines profitability, capital allocation, liquidity, leverage, and efficiency
metrics into an integrated executive summary for an organization and period.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from organizations.models import Organization

from .account_mapping import AccountMappingService
from .capital_allocation import CapitalAllocationService
from .efficiency import EfficiencyService
from .leverage import LeverageService
from .liquidity import LiquidityService
from .profitability import ProfitabilityService
from .result import CalculationStatus, MetricResult


class FinancialDashboardService:
    """Aggregates multi-dimensional financial metrics into a coherent dashboard."""

    def __init__(self, organization: Organization):
        self.organization = organization
        self.mapping_service = AccountMappingService(organization)
        self.profitability_service = ProfitabilityService(organization)
        self.capital_service = CapitalAllocationService(organization)
        self.liquidity_service = LiquidityService(organization)
        self.leverage_service = LeverageService(organization)
        self.efficiency_service = EfficiencyService(organization)

    def get_dashboard(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
        comparison_start_date: Optional[datetime.date] = None,
        comparison_end_date: Optional[datetime.date] = None,
        monthly_burn_rate: Optional[Decimal] = None,
    ) -> Dict[str, Any]:
        """Generate complete financial intelligence dashboard."""
        completeness = self.mapping_service.validate_mapping_completeness()
        
        # 1. Profitability Summary
        prof_summary = self.profitability_service.calculate_margin_summary(
            start_date, end_date, comparison_start_date, comparison_end_date
        )

        # 2. Capital Allocation & CAPEX
        capex = self.capital_service.calculate_capex(start_date, end_date)
        capex_rev = self.capital_service.calculate_capex_to_revenue(start_date, end_date)
        fixed_var = self.capital_service.calculate_fixed_vs_variable_opex(start_date, end_date)
        roa = self.capital_service.calculate_roa(start_date, end_date)
        roe = self.capital_service.calculate_roe(start_date, end_date)
        roic = self.capital_service.calculate_roic(start_date, end_date)
        roce = self.capital_service.calculate_roce(start_date, end_date)

        # 3. Liquidity & Working Capital
        working_cap = self.liquidity_service.calculate_working_capital(as_of_date=end_date)
        current_ratio = self.liquidity_service.calculate_current_ratio(as_of_date=end_date)
        quick_ratio = self.liquidity_service.calculate_quick_ratio(as_of_date=end_date)
        cash_ratio = self.liquidity_service.calculate_cash_ratio(as_of_date=end_date)
        cash_runway = self.liquidity_service.calculate_cash_runway(
            as_of_date=end_date, monthly_burn_rate=monthly_burn_rate
        )

        # 4. Leverage & Solvency
        total_debt = self.leverage_service.calculate_total_debt(as_of_date=end_date)
        net_debt = self.leverage_service.calculate_net_debt(as_of_date=end_date)
        debt_to_equity = self.leverage_service.calculate_debt_to_equity(as_of_date=end_date)
        debt_to_assets = self.leverage_service.calculate_debt_to_assets(as_of_date=end_date)
        interest_coverage = self.leverage_service.calculate_interest_coverage(start_date, end_date)
        net_debt_to_ebitda = self.leverage_service.calculate_net_debt_to_ebitda(start_date, end_date)

        # 5. Operating Efficiency
        inv_turnover = self.efficiency_service.calculate_inventory_turnover(start_date, end_date)
        dio = self.efficiency_service.calculate_dio(start_date, end_date)
        rec_turnover = self.efficiency_service.calculate_receivables_turnover(start_date, end_date)
        dso = self.efficiency_service.calculate_dso(start_date, end_date)
        pay_turnover = self.efficiency_service.calculate_payables_turnover(start_date, end_date)
        dpo = self.efficiency_service.calculate_dpo(start_date, end_date)
        ccc = self.efficiency_service.calculate_cash_conversion_cycle(start_date, end_date)
        asset_turnover = self.efficiency_service.calculate_asset_turnover(start_date, end_date)

        # Collect all metric results to audit warnings and anomalies
        all_metrics: List[MetricResult] = [
            capex, capex_rev, roa, roe, roic, roce,
            working_cap, current_ratio, quick_ratio, cash_ratio, cash_runway,
            total_debt, net_debt, debt_to_equity, debt_to_assets, interest_coverage, net_debt_to_ebitda,
            inv_turnover, dio, rec_turnover, dso, pay_turnover, dpo, ccc, asset_turnover,
        ]

        anomalies: List[Dict[str, Any]] = []
        # Identify negative margins or coverage < 1.0
        if prof_summary["ebitda_margin"].calculated_value is not None and prof_summary["ebitda_margin"].calculated_value < Decimal("0"):
            anomalies.append({
                "severity": "WARNING",
                "metric": "ebitda_margin",
                "message": f"Negative EBITDA margin ({prof_summary['ebitda_margin'].calculated_value}%). Operating losses are being incurred.",
            })
        if current_ratio.calculated_value is not None and current_ratio.calculated_value < Decimal("1.0"):
            anomalies.append({
                "severity": "WARNING",
                "metric": "current_ratio",
                "message": f"Current ratio of {current_ratio.calculated_value} is below 1.0; short-term obligations exceed current assets.",
            })
        if interest_coverage.calculated_value is not None and interest_coverage.calculated_value < Decimal("1.5"):
            anomalies.append({
                "severity": "CRITICAL" if interest_coverage.calculated_value < Decimal("1.0") else "WARNING",
                "metric": "interest_coverage",
                "message": f"Interest coverage ratio ({interest_coverage.calculated_value}x) indicates high debt distress risk.",
            })
        if roe.status == CalculationStatus.UNDEFINED:
            anomalies.append({
                "severity": "INFO",
                "metric": "roe",
                "message": f"ROE is undefined: {'; '.join(roe.warnings)}",
            })

        for m in all_metrics:
            if m.warnings:
                for w in m.warnings:
                    if not any(a.get("message") == w for a in anomalies):
                        anomalies.append({
                            "severity": "INFO" if m.status == CalculationStatus.AVAILABLE else "WARNING",
                            "metric": m.metric_id,
                            "message": w,
                        })

        return {
            "organization_id": str(self.organization.id),
            "currency": self.organization.base_currency,
            "reporting_period": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "comparison_start_date": comparison_start_date.isoformat() if comparison_start_date else None,
                "comparison_end_date": comparison_end_date.isoformat() if comparison_end_date else None,
            },
            "mapping_completeness": completeness,
            "profitability": {
                "revenue": prof_summary["revenue"].to_dict(),
                "gross_profit": prof_summary["gross_profit"].to_dict(),
                "gross_margin": prof_summary["gross_margin"].to_dict(),
                "operating_expenses": prof_summary["operating_expenses"].to_dict(),
                "operating_expense_ratio": prof_summary["operating_expense_ratio"].to_dict(),
                "operating_profit": prof_summary["operating_profit"].to_dict(),
                "operating_margin": prof_summary["operating_margin"].to_dict(),
                "ebit": prof_summary["ebit"].to_dict(),
                "ebit_margin": prof_summary["ebit_margin"].to_dict(),
                "pre_tax_profit": prof_summary["pre_tax_profit"].to_dict(),
                "pre_tax_margin": prof_summary["pre_tax_margin"].to_dict(),
                "net_profit": prof_summary["net_profit"].to_dict(),
                "net_margin": prof_summary["net_margin"].to_dict(),
                "ebitda": prof_summary["ebitda"].to_dict(),
                "ebitda_margin": prof_summary["ebitda_margin"].to_dict(),
                "ebitda_reconciliation": prof_summary["ebitda_reconciliation"],
                "comparisons": prof_summary["comparisons"],
            },
            "capital_allocation": {
                "capex": capex.to_dict(),
                "capex_to_revenue": capex_rev.to_dict(),
                "cost_behavior": fixed_var,
                "returns": {
                    "roa": roa.to_dict(),
                    "roe": roe.to_dict(),
                    "roic": roic.to_dict(),
                    "roce": roce.to_dict(),
                },
            },
            "liquidity": {
                "working_capital": working_cap.to_dict(),
                "current_ratio": current_ratio.to_dict(),
                "quick_ratio": quick_ratio.to_dict(),
                "cash_ratio": cash_ratio.to_dict(),
                "cash_runway": cash_runway.to_dict(),
            },
            "leverage": {
                "total_debt": total_debt.to_dict(),
                "net_debt": net_debt.to_dict(),
                "debt_to_equity": debt_to_equity.to_dict(),
                "debt_to_assets": debt_to_assets.to_dict(),
                "interest_coverage": interest_coverage.to_dict(),
                "net_debt_to_ebitda": net_debt_to_ebitda.to_dict(),
            },
            "efficiency": {
                "inventory_turnover": inv_turnover.to_dict(),
                "days_inventory_outstanding": dio.to_dict(),
                "receivables_turnover": rec_turnover.to_dict(),
                "days_sales_outstanding": dso.to_dict(),
                "payables_turnover": pay_turnover.to_dict(),
                "days_payable_outstanding": dpo.to_dict(),
                "cash_conversion_cycle": ccc.to_dict(),
                "asset_turnover": asset_turnover.to_dict(),
            },
            "anomalies_and_warnings": anomalies,
        }

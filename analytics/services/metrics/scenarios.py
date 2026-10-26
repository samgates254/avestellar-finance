from datetime import date
from decimal import Decimal
from typing import Any
from analytics.services.metrics.profitability import ProfitabilityService
from analytics.services.metrics.liquidity import LiquidityService
from organizations.models import Organization


class FinancialScenarioService:
    """Simulates pro-forma financial impacts under explicit macroeconomic, pricing, and operational assumptions."""

    def __init__(self, organization: Organization):
        self.organization = organization
        self.profitability = ProfitabilityService(organization)
        self.liquidity = LiquidityService(organization)

    def run_scenario(
        self,
        baseline_start: date,
        baseline_end: date,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """Calculates deterministic pro-forma statements under simulated parameter variations.

        Supported parameters:
          - revenue_growth_pct (Decimal / float / str)
          - price_change_pct
          - volume_change_pct
          - cogs_inflation_pct
          - opex_inflation_pct
          - planned_capex
          - effective_tax_rate (default: 0.30)
        """
        # Baseline historical metrics from posted ledger
        rev_data = self.profitability.calculate_revenue(baseline_start, baseline_end)
        base_rev = rev_data["net_revenue"]

        gp_res = self.profitability.calculate_gross_profit(baseline_start, baseline_end)
        base_cogs = gp_res.inputs["cost_of_goods_sold"]
        base_gp = base_rev - base_cogs

        opex_res = self.profitability.calculate_operating_expenses(baseline_start, baseline_end)
        base_opex = opex_res.value or Decimal("0.0000")

        base_ebit = base_rev - base_cogs - base_opex
        tax_rate = Decimal(str(parameters.get("effective_tax_rate", "0.3000")))
        base_tax = max(Decimal("0.0000"), base_ebit * tax_rate)
        base_net_income = base_ebit - base_tax

        # Apply parameters
        rev_growth = Decimal(str(parameters.get("revenue_growth_pct", "0.0"))) / Decimal("100.0000")
        price_change = Decimal(str(parameters.get("price_change_pct", "0.0"))) / Decimal("100.0000")
        volume_change = Decimal(str(parameters.get("volume_change_pct", "0.0"))) / Decimal("100.0000")
        cogs_inf = Decimal(str(parameters.get("cogs_inflation_pct", "0.0"))) / Decimal("100.0000")
        opex_inf = Decimal(str(parameters.get("opex_inflation_pct", "0.0"))) / Decimal("100.0000")
        planned_capex = Decimal(str(parameters.get("planned_capex", "0.0")))

        # Combined revenue factor: (1 + rev_growth) * (1 + price_change) * (1 + volume_change)
        total_rev_factor = (Decimal("1.0000") + rev_growth) * (Decimal("1.0000") + price_change) * (Decimal("1.0000") + volume_change)
        proj_rev = (base_rev * total_rev_factor).quantize(Decimal("0.0001"))

        # COGS varies with volume and inflation
        cogs_volume_factor = (Decimal("1.0000") + volume_change) * (Decimal("1.0000") + cogs_inf)
        proj_cogs = (base_cogs * cogs_volume_factor).quantize(Decimal("0.0001"))

        proj_gp = proj_rev - proj_cogs
        proj_gp_margin = (proj_gp / proj_rev * Decimal("100.0000")) if proj_rev > Decimal("0.0000") else Decimal("0.0000")

        # OPEX scales with inflation
        proj_opex = (base_opex * (Decimal("1.0000") + opex_inf)).quantize(Decimal("0.0001"))

        proj_ebit = proj_gp - proj_opex
        proj_ebit_margin = (proj_ebit / proj_rev * Decimal("100.0000")) if proj_rev > Decimal("0.0000") else Decimal("0.0000")

        proj_tax = max(Decimal("0.0000"), (proj_ebit * tax_rate).quantize(Decimal("0.0001")))
        proj_net_income = proj_ebit - proj_tax
        proj_net_margin = (proj_net_income / proj_rev * Decimal("100.0000")) if proj_rev > Decimal("0.0000") else Decimal("0.0000")

        # Net cash change estimate = Projected Net Income - Planned CAPEX
        est_net_cash_impact = proj_net_income - planned_capex

        return {
            "scenario_metadata": {
                "organization": self.organization.name,
                "currency": self.organization.currency,
                "baseline_period": {"start_date": str(baseline_start), "end_date": str(baseline_end)},
                "parameters_applied": {
                    "revenue_growth_pct": str(rev_growth * 100),
                    "price_change_pct": str(price_change * 100),
                    "volume_change_pct": str(volume_change * 100),
                    "cogs_inflation_pct": str(cogs_inf * 100),
                    "opex_inflation_pct": str(opex_inf * 100),
                    "planned_capex": str(planned_capex),
                    "effective_tax_rate": str(tax_rate),
                },
            },
            "baseline": {
                "revenue": str(base_rev),
                "cogs": str(base_cogs),
                "gross_profit": str(base_gp),
                "opex": str(base_opex),
                "ebit": str(base_ebit),
                "net_income": str(base_net_income),
            },
            "pro_forma_projection": {
                "projected_revenue": str(proj_rev),
                "projected_cogs": str(proj_cogs),
                "projected_gross_profit": str(proj_gp),
                "projected_gross_margin_pct": str(proj_gp_margin.quantize(Decimal("0.0100"))),
                "projected_opex": str(proj_opex),
                "projected_ebit": str(proj_ebit),
                "projected_operating_margin_pct": str(proj_ebit_margin.quantize(Decimal("0.0100"))),
                "projected_tax_expense": str(proj_tax),
                "projected_net_income": str(proj_net_income),
                "projected_net_margin_pct": str(proj_net_margin.quantize(Decimal("0.0100"))),
                "planned_capex": str(planned_capex),
                "estimated_cash_impact": str(est_net_cash_impact),
            },
            "variance_vs_baseline": {
                "revenue_delta": str(proj_rev - base_rev),
                "net_income_delta": str(proj_net_income - base_net_income),
            },
            "assumptions": [
                "Strictly deterministic pro-forma simulation; NO changes committed to official ledger.",
                "Tax calculated at specified effective rate on positive operating earnings.",
            ],
        }

    def simulate_scenario(
        self,
        base_start_date: date,
        base_end_date: date,
        revenue_growth_pct: Decimal = Decimal("0"),
        cogs_change_pct: Decimal = Decimal("0"),
        opex_change_pct: Decimal = Decimal("0"),
        planned_capex: Decimal = Decimal("0"),
        working_capital_buffer_pct: Decimal = Decimal("0"),
        save_scenario: bool = False,
        name: str = "Financial Scenario",
        description: str = "",
    ) -> dict[str, Any]:
        """Convenience simulation wrapper supporting direct kwargs and optional persistence."""
        from analytics.models import FinancialScenario

        params = {
            "revenue_growth_pct": revenue_growth_pct,
            "cogs_inflation_pct": cogs_change_pct,
            "opex_inflation_pct": opex_change_pct,
            "planned_capex": planned_capex,
            "working_capital_buffer_pct": working_capital_buffer_pct,
        }
        res = self.run_scenario(base_start_date, base_end_date, params)

        scenario_id = None
        if save_scenario:
            saved = FinancialScenario.objects.create(
                organization=self.organization,
                name=name or "Simulated Scenario",
                description=description,
                parameters={k: str(v) for k, v in params.items()},
                projections=res["pro_forma_projection"],
            )
            scenario_id = str(saved.id)

        res["scenario_id"] = scenario_id
        res["parameters"] = {k: str(v) for k, v in params.items()}
        return res

"""Break-even and contribution margin analysis service.

Calculates deterministic contribution margin, single-product and multi-product
break-even points, margin of safety, and target profit volume.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
from typing import Any, List, Optional, Union

from analytics.services.metrics.result import CalculationStatus, MetricResult, MetricUnit


class BreakEvenService:
    """Calculates contribution margin, break-even sales & units, margin of safety, and target profit scenarios."""

    @staticmethod
    def calculate_break_even(
        *args,
        fixed_costs: Optional[Union[Decimal, str, float]] = None,
        unit_selling_price: Optional[Union[Decimal, str, float]] = None,
        unit_variable_cost: Optional[Union[Decimal, str, float]] = None,
        revenue: Optional[Union[Decimal, str, float]] = None,
        variable_costs: Optional[Union[Decimal, str, float]] = None,
        actual_units: Optional[Union[int, float, str, Decimal]] = None,
        units_sold: Optional[Union[int, float, str, Decimal]] = None,
        target_profit: Union[Decimal, str, float] = Decimal("0.0000"),
        currency: str = "KES",
        **kwargs,
    ) -> dict[str, Any]:
        """Calculates single-product or aggregated contribution margin and break-even thresholds.

        Supports both unit-based signature:
          (fixed_costs, unit_selling_price, unit_variable_cost, [actual_units])
        and revenue/variable cost aggregate signature:
          (revenue, variable_costs, fixed_costs, [units_sold])
        """
        # Parse positional args if passed
        if len(args) >= 3:
            # Check if arg[0] is fixed_costs or revenue
            # By convention, if positional:
            # (fixed_costs, unit_price, unit_var_cost) OR (rev, var_costs, fixed_costs)
            first = Decimal(str(args[0]))
            second = Decimal(str(args[1]))
            third = Decimal(str(args[2]))

            # Disambiguate based on keywords or default to (fixed_costs, unit_price, unit_var_cost)
            if fixed_costs is None and unit_selling_price is None and unit_variable_cost is None:
                fixed_costs = first
                unit_selling_price = second
                unit_variable_cost = third
                if len(args) >= 4 and actual_units is None:
                    actual_units = args[3]
            elif revenue is None and variable_costs is None:
                revenue = first
                variable_costs = second
                if fixed_costs is None:
                    fixed_costs = third

        # Unit-based pathway
        if unit_selling_price is not None and unit_variable_cost is not None and fixed_costs is not None:
            fc = Decimal(str(fixed_costs))
            price = Decimal(str(unit_selling_price))
            vc_unit = Decimal(str(unit_variable_cost))
            units = Decimal(str(actual_units or units_sold or "0"))

            warnings: List[str] = []
            cm_unit = price - vc_unit

            if price <= Decimal("0.0000"):
                return {
                    "status": CalculationStatus.UNDEFINED.value,
                    "currency": currency,
                    "fixed_costs": str(fc),
                    "unit_selling_price": str(price),
                    "unit_variable_cost": str(vc_unit),
                    "contribution_margin_per_unit": str(cm_unit),
                    "warnings": ["Unit selling price must be greater than zero."],
                }

            if cm_unit <= Decimal("0.0000"):
                warnings.append("Selling price does not cover unit variable cost; contribution is non-positive.")
                return {
                    "status": CalculationStatus.UNDEFINED.value,
                    "currency": currency,
                    "fixed_costs": str(fc),
                    "unit_selling_price": str(price),
                    "unit_variable_cost": str(vc_unit),
                    "contribution_margin_per_unit": str(cm_unit),
                    "contribution_margin_ratio": str((cm_unit / price * Decimal("100.0000")).quantize(Decimal("0.0001"))),
                    "break_even_units": None,
                    "break_even_sales": None,
                    "warnings": warnings,
                }

            cm_ratio_pct = (cm_unit / price * Decimal("100.0000")).quantize(Decimal("0.0001"))
            cm_ratio_frac = cm_unit / price

            be_units = (fc / cm_unit).quantize(Decimal("0.0001"))
            be_sales = (fc / cm_ratio_frac).quantize(Decimal("0.0001"))

            mos_units = None
            mos_sales = None
            mos_pct = None
            if units > Decimal("0.0000"):
                actual_sales = (units * price).quantize(Decimal("0.0001"))
                mos_units = (units - be_units).quantize(Decimal("0.0001"))
                mos_sales = (actual_sales - be_sales).quantize(Decimal("0.0001"))
                mos_pct = (mos_sales / actual_sales * Decimal("100.0000")).quantize(Decimal("0.0100"))

            return {
                "status": CalculationStatus.AVAILABLE.value,
                "currency": currency,
                "fixed_costs": str(fc),
                "unit_selling_price": str(price),
                "unit_variable_cost": str(vc_unit),
                "contribution_margin_per_unit": str(cm_unit.quantize(Decimal("0.0001"))),
                "contribution_margin_ratio": str(cm_ratio_pct),
                "contribution_margin_ratio_pct": str(cm_ratio_pct),
                "break_even_units": str(be_units),
                "break_even_sales": str(be_sales),
                "actual_units": str(units) if units > Decimal("0") else None,
                "margin_of_safety_units": str(mos_units) if mos_units is not None else None,
                "margin_of_safety_sales": str(mos_sales) if mos_sales is not None else None,
                "margin_of_safety_pct": str(mos_pct) if mos_pct is not None else None,
                "warnings": warnings,
            }

        # Aggregate Revenue / Cost pathway
        rev = Decimal(str(revenue or "0"))
        vc = Decimal(str(variable_costs or "0"))
        fc = Decimal(str(fixed_costs or "0"))

        if rev <= Decimal("0.0000"):
            return {
                "status": CalculationStatus.UNDEFINED.value,
                "currency": currency,
                "revenue": str(rev),
                "variable_costs": str(vc),
                "fixed_costs": str(fc),
                "warnings": ["Revenue must be positive to determine contribution margin ratio."],
            }

        cm = rev - vc
        if cm <= Decimal("0.0000"):
            return {
                "status": CalculationStatus.UNDEFINED.value,
                "currency": currency,
                "revenue": str(rev),
                "variable_costs": str(vc),
                "fixed_costs": str(fc),
                "contribution_margin": str(cm),
                "warnings": ["Selling price does not cover unit variable cost; contribution is non-positive."],
            }

        cm_ratio_frac = cm / rev
        cm_ratio_pct = (cm_ratio_frac * Decimal("100.0000")).quantize(Decimal("0.0001"))
        be_sales = (fc / cm_ratio_frac).quantize(Decimal("0.0001"))

        return {
            "status": CalculationStatus.AVAILABLE.value,
            "currency": currency,
            "revenue": str(rev),
            "variable_costs": str(vc),
            "fixed_costs": str(fc),
            "contribution_margin": str(cm),
            "contribution_margin_ratio": str(cm_ratio_pct),
            "contribution_margin_ratio_pct": str(cm_ratio_pct),
            "break_even_sales": str(be_sales),
            "warnings": [],
        }

    @staticmethod
    def calculate_target_profit_volume(
        fixed_costs: Union[Decimal, str, float],
        unit_selling_price: Union[Decimal, str, float],
        unit_variable_cost: Union[Decimal, str, float],
        target_profit: Union[Decimal, str, float],
        currency: str = "KES",
    ) -> MetricResult:
        """Calculates volume of units required to achieve target operating profit."""
        fc = Decimal(str(fixed_costs))
        price = Decimal(str(unit_selling_price))
        vc_unit = Decimal(str(unit_variable_cost))
        tp = Decimal(str(target_profit))

        cm_unit = price - vc_unit
        if cm_unit <= Decimal("0.0000"):
            return MetricResult(
                metric_id="target_profit_units",
                name="Target Profit Volume",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.UNITS,
                warnings=["Unit contribution margin is non-positive; target profit cannot be achieved."],
            )

        required_units = ((fc + tp) / cm_unit).quantize(Decimal("1"), rounding=ROUND_CEILING)
        return MetricResult(
            metric_id="target_profit_units",
            name="Target Profit Volume",
            status=CalculationStatus.AVAILABLE,
            value=required_units,
            formatted_value=f"{required_units:,.0f} units",
            unit=MetricUnit.UNITS,
            inputs={
                "fixed_costs": fc,
                "unit_selling_price": price,
                "unit_variable_cost": vc_unit,
                "target_profit": tp,
                "unit_contribution_margin": cm_unit,
            },
        )

    @staticmethod
    def calculate_multi_product_break_even(
        *args,
        fixed_costs: Optional[Union[Decimal, str, float]] = None,
        products: Optional[List[dict[str, Any]]] = None,
        currency: str = "KES",
        **kwargs,
    ) -> dict[str, Any]:
        """Calculates composite break-even point for a multi-product sales mix.

        Accepts (fixed_costs, products) or (products, fixed_costs).
        Each product dict can have:
          - name
          - price / revenue
          - variable_cost / variable_costs
          - sales_mix_pct (percentage e.g. 60 for 60%) OR revenue weight
        """
        if len(args) == 2:
            if isinstance(args[0], list):
                products = args[0]
                fixed_costs = args[1]
            else:
                fixed_costs = args[0]
                products = args[1]

        fc = Decimal(str(fixed_costs or "0"))
        prods = products or []

        weighted_cm = Decimal("0.0000")
        weighted_revenue = Decimal("0.0000")
        summaries = []

        for p in prods:
            name = p.get("name", "Product")
            price = Decimal(str(p.get("price") or p.get("revenue") or "0"))
            vc = Decimal(str(p.get("variable_cost") or p.get("variable_costs") or "0"))
            mix_pct = Decimal(str(p.get("sales_mix_pct") or "0")) / Decimal("100.0000") if p.get("sales_mix_pct") else Decimal("1.0")

            cm = price - vc
            weighted_cm += cm * mix_pct
            weighted_revenue += price * mix_pct

            summaries.append({
                "name": name,
                "price": str(price),
                "variable_cost": str(vc),
                "contribution_margin": str(cm),
                "sales_mix_pct": str(mix_pct * 100),
            })

        if weighted_revenue <= Decimal("0.0000") or weighted_cm <= Decimal("0.0000"):
            return {
                "status": CalculationStatus.UNDEFINED.value,
                "currency": currency,
                "total_fixed_costs": str(fc),
                "warnings": ["Composite contribution margin across sales mix is zero or negative."],
            }

        cm_ratio_frac = weighted_cm / weighted_revenue
        be_revenue = (fc / cm_ratio_frac).quantize(Decimal("0.0001"))

        return {
            "status": CalculationStatus.AVAILABLE.value,
            "currency": currency,
            "fixed_costs": str(fc),
            "break_even_total_revenue": str(be_revenue),
            "break_even_sales": str(be_revenue),
            "composite_contribution_margin_ratio_pct": str((cm_ratio_frac * 100).quantize(Decimal("0.0100"))),
            "products": summaries,
        }

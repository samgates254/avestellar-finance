from decimal import Decimal, InvalidOperation
from typing import Any, Sequence
from analytics.services.metrics.result import CalculationStatus, MetricResult, MetricUnit


class InvestmentAppraisalService:
    """Deterministic, robust numerical investment appraisal calculations: NPV, IRR, Payback, and Profitability Index."""

    @staticmethod
    def calculate_npv(
        initial_investment: Decimal | str | float,
        cash_flows: Sequence[Decimal | str | float],
        discount_rate: Decimal | str | float,
        currency: str = "KES",
    ) -> MetricResult:
        """Net Present Value (NPV) = Sum(CF_t / (1 + r)^t) - Initial Investment."""
        c0 = Decimal(str(initial_investment))
        r = Decimal(str(discount_rate))

        if c0 < Decimal("0.0000"):
            return MetricResult(
                metric_id="npv",
                name="Net Present Value (NPV)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.CURRENCY,
                currency=currency,
                inputs={"initial_investment": c0, "discount_rate": r},
                warnings=["Initial investment must be non-negative."],
            )

        if r <= Decimal("-1.0000"):
            return MetricResult(
                metric_id="npv",
                name="Net Present Value (NPV)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.CURRENCY,
                currency=currency,
                inputs={"initial_investment": c0, "discount_rate": r},
                warnings=["Discount rate must be strictly greater than -100% (-1.0)."],
            )

        pv_future = Decimal("0.0000")
        for t, cf in enumerate(cash_flows, start=1):
            cf_dec = Decimal(str(cf))
            denom = (Decimal("1.0000") + r) ** t
            pv_future += cf_dec / denom

        npv = pv_future - c0

        return MetricResult(
            metric_id="npv",
            name="Net Present Value (NPV)",
            status=CalculationStatus.AVAILABLE,
            value=npv.quantize(Decimal("0.0001")),
            formatted_value=f"{currency} {npv:,.2f}",
            unit=MetricUnit.CURRENCY,
            currency=currency,
            inputs={
                "initial_investment": c0,
                "cash_flows": [Decimal(str(x)) for x in cash_flows],
                "discount_rate": r,
                "pv_of_future_cash_flows": pv_future.quantize(Decimal("0.0001")),
            },
            assumptions=["Cash flows discounted at end of each discrete period."],
            metadata={"accept_project": npv > Decimal("0.0000")},
        )

    @staticmethod
    def calculate_irr(
        initial_investment: Decimal | str | float,
        cash_flows: Sequence[Decimal | str | float],
        max_iterations: int = 200,
        tolerance: float = 1e-7,
        currency: str = "KES",
    ) -> MetricResult:
        """Internal Rate of Return (IRR) using hybrid Newton-Raphson with bisection fallback.

        Detects and cleanly handles:
        - Cash flows with no valid IRR (e.g. all positive or all negative).
        - Multiple sign changes (non-conventional cash flows resulting in multiple potential roots).
        - Non-convergence within max_iterations.
        """
        c0 = float(initial_investment)
        cfs = [float(cf) for cf in cash_flows]

        warnings: list[str] = []

        if c0 <= 0:
            return MetricResult(
                metric_id="irr",
                name="Internal Rate of Return (IRR)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.PERCENTAGE,
                currency=currency,
                inputs={"initial_investment": Decimal(str(c0))},
                warnings=["Initial investment must be strictly positive to calculate IRR."],
            )

        if not cfs or sum(cfs) <= 0:
            return MetricResult(
                metric_id="irr",
                name="Internal Rate of Return (IRR)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.PERCENTAGE,
                currency=currency,
                inputs={"initial_investment": Decimal(str(c0)), "total_inflows": sum(cfs)},
                warnings=["Total undiscounted cash inflows do not exceed initial investment; project has no positive IRR."],
            )

        # Count sign changes in sequence [-c0, cf1, cf2, ...]
        full_series = [-c0] + cfs
        sign_changes = 0
        for i in range(len(full_series) - 1):
            if (full_series[i] < 0 and full_series[i + 1] > 0) or (full_series[i] > 0 and full_series[i + 1] < 0):
                sign_changes += 1

        if sign_changes > 1:
            warnings.append(
                f"Cash flow stream exhibits {sign_changes} sign changes (non-conventional investment). "
                "Multiple mathematical IRRs may exist; root shown represents the primary operating rate."
            )

        def npv_func(rate: float) -> float:
            total = -c0
            for t, cf in enumerate(cfs, start=1):
                total += cf / ((1.0 + rate) ** t)
            return total

        def npv_deriv(rate: float) -> float:
            total = 0.0
            for t, cf in enumerate(cfs, start=1):
                total -= (t * cf) / ((1.0 + rate) ** (t + 1))
            return total

        # Attempt Newton-Raphson from initial guess of 10%
        rate = 0.10
        converged = False

        for _ in range(max_iterations):
            val = npv_func(rate)
            if abs(val) < tolerance:
                converged = True
                break
            deriv = npv_deriv(rate)
            if abs(deriv) < 1e-12:
                break
            new_rate = rate - (val / deriv)
            if new_rate <= -0.99 or new_rate > 20.0:
                # Stepped out of bounds; break to bisection
                break
            rate = new_rate

        # Bisection fallback if Newton-Raphson did not converge
        if not converged:
            low = -0.90
            high = 5.0
            npv_low = npv_func(low)
            npv_high = npv_func(high)

            if npv_low * npv_high > 0:
                return MetricResult(
                    metric_id="irr",
                    name="Internal Rate of Return (IRR)",
                    status=CalculationStatus.UNDEFINED,
                    value=None,
                    formatted_value="N/A",
                    unit=MetricUnit.PERCENTAGE,
                    currency=currency,
                    inputs={"initial_investment": Decimal(str(c0))},
                    warnings=["Numerical convergence failed: no sign bracket found for IRR root."],
                )

            for _ in range(max_iterations):
                mid = (low + high) / 2.0
                val_mid = npv_func(mid)
                if abs(val_mid) < tolerance or (high - low) / 2.0 < tolerance:
                    rate = mid
                    converged = True
                    break
                if npv_func(low) * val_mid <= 0:
                    high = mid
                else:
                    low = mid

        if not converged or rate <= -0.999:
            return MetricResult(
                metric_id="irr",
                name="Internal Rate of Return (IRR)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.PERCENTAGE,
                currency=currency,
                inputs={"initial_investment": Decimal(str(c0))},
                warnings=["Algorithm did not converge on a valid mathematical IRR."],
            )

        irr_dec = Decimal(str(rate * 100.0)).quantize(Decimal("0.0100"))

        return MetricResult(
            metric_id="irr",
            name="Internal Rate of Return (IRR)",
            status=CalculationStatus.AVAILABLE,
            value=irr_dec,
            formatted_value=f"{irr_dec:.2f}%",
            unit=MetricUnit.PERCENTAGE,
            currency=currency,
            inputs={
                "initial_investment": Decimal(str(c0)),
                "cash_flows": [Decimal(str(x)) for x in cfs],
            },
            assumptions=["Discrete annual cash flows with discount rates bounded > -100%."],
            warnings=warnings,
        )

    @staticmethod
    def calculate_payback(
        initial_investment: Decimal | str | float,
        cash_flows: Sequence[Decimal | str | float],
        discount_rate: Optional[Decimal | str | float] = None,
        currency: str = "KES",
    ) -> MetricResult:
        """Calculates Simple Payback Period or Discounted Payback Period (in years / periods)."""
        c0 = Decimal(str(initial_investment))
        r = Decimal(str(discount_rate)) if discount_rate is not None else None

        metric_id = "discounted_payback" if r is not None else "simple_payback"
        metric_name = "Discounted Payback Period" if r is not None else "Simple Payback Period"

        if c0 <= Decimal("0.0000"):
            return MetricResult(
                metric_id=metric_id,
                name=metric_name,
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.DAYS,
                currency=currency,
                inputs={"initial_investment": c0},
                warnings=["Initial investment must be strictly positive."],
            )

        # 1. Simple Payback
        cum_simple = Decimal("0.0000")
        simple_payback: Optional[Decimal] = None
        for t, cf in enumerate(cash_flows, start=1):
            cf_dec = Decimal(str(cf))
            prior = cum_simple
            cum_simple += cf_dec
            if cum_simple >= c0:
                needed = c0 - prior
                fraction = needed / cf_dec if cf_dec > Decimal("0.0000") else Decimal("0.0000")
                simple_payback = Decimal(str(t - 1)) + fraction
                break

        # 2. Discounted Payback
        disc_payback: Optional[Decimal] = None
        if r is not None:
            cum_disc = Decimal("0.0000")
            for t, cf in enumerate(cash_flows, start=1):
                cf_dec = Decimal(str(cf))
                eff_cf = cf_dec / ((Decimal("1.0000") + r) ** t)
                prior = cum_disc
                cum_disc += eff_cf
                if cum_disc >= c0:
                    needed = c0 - prior
                    fraction = needed / eff_cf if eff_cf > Decimal("0.0000") else Decimal("0.0000")
                    disc_payback = Decimal(str(t - 1)) + fraction
                    break

        if simple_payback is None:
            return MetricResult(
                metric_id="payback_period",
                name="Payback Period",
                status=CalculationStatus.NOT_APPLICABLE,
                value=None,
                formatted_value="Unrecovered",
                unit=MetricUnit.DAYS,
                currency=currency,
                inputs={"initial_investment": c0, "discount_rate": r},
                warnings=["Initial investment is not fully recovered within the evaluation horizon."],
            )

        payback_dec = simple_payback.quantize(Decimal("0.0001"))
        disc_dec = disc_payback.quantize(Decimal("0.0001")) if disc_payback is not None else None

        inputs_dict = {
            "initial_investment": c0,
            "cash_flows": [Decimal(str(x)) for x in cash_flows],
            "discount_rate": r,
            "simple_payback_period": payback_dec,
        }
        if r is not None:
            inputs_dict["discounted_payback_period"] = disc_dec

        return MetricResult(
            metric_id="payback_period",
            name="Payback Period",
            status=CalculationStatus.AVAILABLE,
            value=payback_dec,
            formatted_value=f"{payback_dec:.2f} periods",
            unit=MetricUnit.DAYS,
            currency=currency,
            inputs=inputs_dict,
            assumptions=["Cash flows received evenly throughout each period for fractional interpolation."],
        )

    # Alias for method name flexibility
    calculate_payback_period = calculate_payback

    @staticmethod
    def calculate_profitability_index(
        initial_investment: Decimal | str | float,
        cash_flows: Sequence[Decimal | str | float],
        discount_rate: Decimal | str | float,
        currency: str = "KES",
    ) -> MetricResult:
        """Profitability Index (PI) = PV of Future Cash Flows / Initial Investment."""
        npv_res = InvestmentAppraisalService.calculate_npv(initial_investment, cash_flows, discount_rate, currency)
        if npv_res.status != CalculationStatus.AVAILABLE:
            return MetricResult(
                metric_id="profitability_index",
                name="Profitability Index (PI)",
                status=npv_res.status,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.RATIO,
                currency=currency,
                warnings=npv_res.warnings,
            )

        pv_future = npv_res.inputs["pv_of_future_cash_flows"]
        c0 = npv_res.inputs["initial_investment"]

        if c0 <= Decimal("0.0000"):
            return MetricResult(
                metric_id="profitability_index",
                name="Profitability Index (PI)",
                status=CalculationStatus.UNDEFINED,
                value=None,
                formatted_value="N/A",
                unit=MetricUnit.RATIO,
                currency=currency,
                warnings=["Initial investment must be positive."],
            )

        pi = (pv_future / c0).quantize(Decimal("0.0100"))
        return MetricResult(
            metric_id="profitability_index",
            name="Profitability Index (PI)",
            status=CalculationStatus.AVAILABLE,
            value=pi,
            formatted_value=f"{pi:.2f}x",
            unit=MetricUnit.RATIO,
            currency=currency,
            inputs={"pv_of_future_cash_flows": pv_future, "initial_investment": c0},
            metadata={"accept_project": pi >= Decimal("1.0000")},
        )

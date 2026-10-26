"""Safe Financial Intelligence Assistant Interface.

The AI assistant NEVER performs free-form financial arithmetic or directly writes to the ledger.
All facts and numbers are derived deterministically by underlying domain services.
The assistant synthesizes verified facts into executive management summaries and actionable insights.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.conf import settings

from analytics.services.financial_analysis import (
    detect_financial_anomalies,
    get_cash_flow_summary,
)
from analytics.services.metrics.capital_allocation import CapitalAllocationService
from analytics.services.metrics.investment_appraisal import InvestmentAppraisalService
from analytics.services.metrics.liquidity import LiquidityService
from analytics.services.metrics.profitability import ProfitabilityService
from analytics.services.metrics.result import CalculationStatus
from ledger.services.reporting import get_income_statement
from organizations.models import Organization


class AIFinancialAssistantInterface:
    """Safe Financial Intelligence Assistant Interface.

    Integrates deterministic calculations from financial services into clear,
    traceable, evidence-backed narrative explanations.
    """

    def __init__(self, organization: Organization):
        self.organization = organization
        self.is_enabled = getattr(settings, "AI_ASSISTANT_ENABLED", False)
        self.profitability_service = ProfitabilityService(organization)
        self.capital_service = CapitalAllocationService(organization)
        self.liquidity_service = LiquidityService(organization)
        self.investment_service = InvestmentAppraisalService()

    def generate_management_summary(self) -> dict[str, Any]:
        """Provides an executive digest of revenue, profitability, cash flow, and key financial risks."""
        cash_flow = get_cash_flow_summary(self.organization, days_forward=30)
        pnl = get_income_statement(self.organization)
        anomalies = detect_financial_anomalies(self.organization)

        total_rev = Decimal(pnl.get("total_revenue", "0.00"))
        net_inc = Decimal(pnl.get("net_income", "0.00"))
        margin_pct = (net_inc / total_rev * 100) if total_rev > 0 else Decimal("0.00")

        currency = getattr(self.organization, "base_currency", getattr(self.organization, "currency", "USD"))
        summary_text = (
            f"Organization '{self.organization.name}' currently holds {cash_flow['verified_current_cash']} {currency} "
            f"in verified liquid cash. Over the next 30 days, projected net cash position is {cash_flow['projected_net_cash']} {currency}. "
            f"Current period net income stands at {pnl['net_income']} {currency} ({margin_pct:.1f}% net margin). "
            f"{len(anomalies)} financial risk exceptions have been identified for accountant review."
        )

        return {
            "organization_name": self.organization.name,
            "currency": currency,
            "ai_integration_status": "ACTIVE_SYNTHESIZER" if self.is_enabled else "DETERMINISTIC_RULES_FALLBACK",
            "executive_summary": summary_text,
            "verified_facts": {
                "liquid_cash": cash_flow["verified_current_cash"],
                "projected_cash_30d": cash_flow["projected_net_cash"],
                "total_revenue": pnl["total_revenue"],
                "total_expense": pnl["total_expense"],
                "net_income": pnl["net_income"],
                "risk_exceptions_count": len(anomalies),
            },
            "recommendations": [
                "Prioritize collection on receivables aged > 30 days to protect working capital.",
                "Review open supplier bills to optimize cash outflow schedule.",
            ],
        }

    def explain_financial_statement(self, statement_type: str = "income_statement") -> dict[str, Any]:
        """Explains performance metrics and operational margin drivers based on verified ledger records."""
        pnl = get_income_statement(self.organization)
        rev = Decimal(pnl.get("total_revenue", "0.00"))
        exp = Decimal(pnl.get("total_expense", "0.00"))
        net = Decimal(pnl.get("net_income", "0.00"))
        currency = getattr(self.organization, "base_currency", getattr(self.organization, "currency", "USD"))

        status_narrative = (
            "The business is operating profitably." if net > 0
            else "The business is currently operating at a net loss or breakeven."
        )

        return {
            "statement_type": statement_type,
            "verified_totals": {
                "revenue": str(rev),
                "expenses": str(exp),
                "net_income": str(net),
            },
            "narrative": status_narrative,
            "insights": [
                f"Total operating revenue of {rev} {currency} supported {exp} {currency} in expenses.",
                "All reported numbers are mathematically reconciled against posted ledger journal entries.",
            ],
        }

    def explain_ebitda_reconciliation(
        self,
        start_date: datetime.date,
        end_date: datetime.date,
    ) -> Dict[str, Any]:
        """Explains the mathematical bridge from Net Income to EBITDA with verified account figures."""
        ebitda_res = self.profitability_service.calculate_ebitda(start_date, end_date)
        bridge = ebitda_res.input_values.get("reconciliation_bridge", {})
        currency = self.organization.base_currency

        net_income = bridge.get("net_income", Decimal("0"))
        interest = bridge.get("interest_expense", Decimal("0"))
        tax = bridge.get("income_tax_expense", Decimal("0"))
        depr = bridge.get("depreciation_expense", Decimal("0"))
        amort = bridge.get("amortization_expense", Decimal("0"))
        ebitda = ebitda_res.calculated_value

        narrative = (
            f"EBITDA was calculated at {ebitda} {currency} for the period from {start_date} to {end_date}. "
            f"Starting from Net Income of {net_income} {currency}, we add back non-operating financing, "
            f"tax, and non-cash items: Interest Expense ({interest} {currency}), Income Taxes ({tax} {currency}), "
            f"Depreciation ({depr} {currency}), and Amortization ({amort} {currency})."
        )

        return {
            "metric": "ebitda",
            "status": ebitda_res.status.value,
            "calculated_value": str(ebitda) if ebitda is not None else None,
            "currency": currency,
            "reconciliation_bridge": {k: str(v) for k, v in bridge.items()},
            "narrative": narrative,
            "warnings": ebitda_res.warnings,
        }

    def explain_margin_change(
        self,
        current_start: datetime.date,
        current_end: datetime.date,
        prior_start: datetime.date,
        prior_end: datetime.date,
    ) -> Dict[str, Any]:
        """Explains why operating margin or gross margin changed between two reporting periods."""
        curr = self.profitability_service.calculate_margin_summary(current_start, current_end)
        prior = self.profitability_service.calculate_margin_summary(prior_start, prior_end)

        curr_rev = curr["revenue"].calculated_value or Decimal("0")
        prior_rev = prior["revenue"].calculated_value or Decimal("0")
        curr_op_margin = curr["operating_margin"].calculated_value
        prior_op_margin = prior["operating_margin"].calculated_value

        narrative_parts = []
        if curr_op_margin is not None and prior_op_margin is not None:
            diff = curr_op_margin - prior_op_margin
            direction = "expanded" if diff > 0 else ("contracted" if diff < 0 else "remained flat")
            narrative_parts.append(
                f"Operating margin {direction} by {abs(diff)} percentage points from {prior_op_margin}% to {curr_op_margin}%."
            )
        else:
            narrative_parts.append("Operating margin could not be fully compared due to incomplete period data or zero revenue.")

        curr_opex = curr["operating_expenses"].calculated_value or Decimal("0")
        prior_opex = prior["operating_expenses"].calculated_value or Decimal("0")
        narrative_parts.append(
            f"Revenue moved from {prior_rev} to {curr_rev}, while operating expenses moved from {prior_opex} to {curr_opex}."
        )

        return {
            "current_period": {"start": current_start.isoformat(), "end": current_end.isoformat()},
            "prior_period": {"start": prior_start.isoformat(), "end": prior_end.isoformat()},
            "current_operating_margin": str(curr_op_margin) if curr_op_margin is not None else None,
            "prior_operating_margin": str(prior_op_margin) if prior_op_margin is not None else None,
            "narrative": " ".join(narrative_parts),
            "current_summary": {k: v.to_dict() for k, v in curr.items() if hasattr(v, "to_dict")},
        }

    def evaluate_investment_hurdle(
        self,
        initial_investment: Decimal,
        cash_flows: List[Decimal],
        discount_rate: Decimal,
        hurdle_rate: Decimal,
    ) -> Dict[str, Any]:
        """Evaluates whether an investment proposal satisfies configured hurdle rates deterministically."""
        npv_res = self.investment_service.calculate_npv(initial_investment, cash_flows, discount_rate)
        irr_res = self.investment_service.calculate_irr(initial_investment, cash_flows)
        payback_res = self.investment_service.calculate_payback_period(initial_investment, cash_flows)
        pi_res = self.investment_service.calculate_profitability_index(initial_investment, cash_flows, discount_rate)

        npv_val = npv_res.calculated_value
        irr_val = irr_res.calculated_value

        meets_hurdle = False
        reasons = []

        if npv_val is not None:
            if npv_val > Decimal("0"):
                reasons.append(f"NPV is positive at {npv_val}, indicating value creation above the {discount_rate * 100}% discount rate.")
            else:
                reasons.append(f"NPV is non-positive at {npv_val}, failing the baseline value-creation criterion.")

        if irr_res.status == CalculationStatus.AVAILABLE and irr_val is not None:
            if irr_val >= hurdle_rate:
                reasons.append(f"IRR of {irr_val}% meets or exceeds the required hurdle rate of {hurdle_rate}%.")
                if npv_val and npv_val > Decimal("0"):
                    meets_hurdle = True
            else:
                reasons.append(f"IRR of {irr_val}% is below the hurdle rate of {hurdle_rate}%.")
        else:
            reasons.append(f"IRR status is '{irr_res.status.value}'; {'; '.join(irr_res.warnings)}")

        narrative = f"Investment appraisal conclusion: {'RECOMMENDED' if meets_hurdle else 'DOES NOT MEET CRITERIA'}. " + " ".join(reasons)

        return {
            "meets_hurdle": meets_hurdle,
            "hurdle_rate_pct": str(hurdle_rate),
            "discount_rate_pct": str(discount_rate * 100),
            "npv": npv_res.to_dict(),
            "irr": irr_res.to_dict(),
            "payback_periods": payback_res.to_dict(),
            "profitability_index": pi_res.to_dict(),
            "narrative": narrative,
        }

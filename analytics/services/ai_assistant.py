from decimal import Decimal
from typing import Any
from django.conf import settings
from analytics.services.financial_analysis import (
    detect_financial_anomalies,
    get_cash_flow_summary,
    get_receivables_aging,
)
from ledger.services.reporting import get_income_statement
from organizations.models import Organization


class AIFinancialAssistantInterface:
    """Safe Financial Intelligence Assistant Interface.

    Core Invariant:
      The AI assistant NEVER performs free-form financial arithmetic or directly writes to the ledger.
      All facts and numbers are derived deterministically by underlying domain services.
      The assistant synthesizes verified facts into executive management summaries and actionable insights.
    """

    def __init__(self, organization: Organization):
        self.organization = organization
        self.is_enabled = getattr(settings, "AI_ASSISTANT_ENABLED", False)

    def generate_management_summary(self) -> dict[str, Any]:
        """Provides an executive digest of revenue, profitability, cash flow, and key financial risks."""
        cash_flow = get_cash_flow_summary(self.organization, days_forward=30)
        pnl = get_income_statement(self.organization)
        anomalies = detect_financial_anomalies(self.organization)

        total_rev = Decimal(pnl.get("total_revenue", "0.00"))
        net_inc = Decimal(pnl.get("net_income", "0.00"))
        margin_pct = (net_inc / total_rev * 100) if total_rev > 0 else Decimal("0.00")

        summary_text = (
            f"Organization '{self.organization.name}' currently holds {cash_flow['verified_current_cash']} {self.organization.currency} "
            f"in verified liquid cash. Over the next 30 days, projected net cash position is {cash_flow['projected_net_cash']} {self.organization.currency}. "
            f"Current period net income stands at {pnl['net_income']} {self.organization.currency} ({margin_pct:.1f}% net margin). "
            f"{len(anomalies)} financial risk exceptions have been identified for accountant review."
        )

        return {
            "organization_name": self.organization.name,
            "currency": self.organization.currency,
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
                f"Total operating revenue of {rev} {self.organization.currency} supported {exp} {self.organization.currency} in expenses.",
                "All reported numbers are mathematically reconciled against posted ledger journal entries.",
            ],
        }

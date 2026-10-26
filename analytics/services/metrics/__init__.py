"""Financial Metric Calculation and Decision Engine.

Provides deterministic, reproducible, and auditable financial metrics
built upon ledger-posted transactions and verified classification mappings.
"""

from .account_mapping import AccountMappingService
from .break_even import BreakEvenService
from .capital_allocation import CapitalAllocationService
from .comparisons import compare_metrics
from .dashboard import FinancialDashboardService
from .efficiency import EfficiencyService
from .investment_appraisal import InvestmentAppraisalService
from .leverage import LeverageService
from .liquidity import LiquidityService
from .profitability import ProfitabilityService
from .registry import FinancialMetricRegistry, metric_registry
from .result import CalculationStatus, MetricResult, MetricUnit
from .scenarios import FinancialScenarioService

__all__ = [
    "CalculationStatus",
    "MetricUnit",
    "MetricResult",
    "compare_metrics",
    "AccountMappingService",
    "ProfitabilityService",
    "CapitalAllocationService",
    "InvestmentAppraisalService",
    "LiquidityService",
    "LeverageService",
    "EfficiencyService",
    "BreakEvenService",
    "FinancialScenarioService",
    "FinancialDashboardService",
    "FinancialMetricRegistry",
    "metric_registry",
]

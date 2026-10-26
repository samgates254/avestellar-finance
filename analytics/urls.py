"""URL configuration for analytics, metric calculations, and financial decision engines."""

from django.urls import path

from analytics.views import (
    AccountClassificationMappingView,
    AccountMappingCompletenessView,
    AIExplainEbitdaView,
    AIExplainPnLView,
    AIManagementSummaryView,
    BreakEvenCalculatorView,
    CapitalExpenditureView,
    CashFlowSummaryView,
    EfficiencyMetricsView,
    FinancialAnomaliesView,
    FinancialDashboardView,
    FinancialMetricCatalogView,
    FinancialScenarioView,
    InvestmentAppraisalView,
    IrrCalculatorView,
    LiquidityLeverageView,
    MultiProductBreakEvenView,
    NpvCalculatorView,
    ProfitabilityMetricsView,
    ReceivablesAgingView,
    ReturnsMetricsView,
)

urlpatterns = [
    # Dashboards and Metric Catalogs
    path("analytics/financial-dashboard/", FinancialDashboardView.as_view(), name="analytics-financial-dashboard"),
    path("analytics/metrics/", FinancialMetricCatalogView.as_view(), name="analytics-metrics-catalog"),

    # Domain Metrics
    path("analytics/profitability/", ProfitabilityMetricsView.as_view(), name="analytics-profitability"),
    path("analytics/capital-expenditure/", CapitalExpenditureView.as_view(), name="analytics-capex"),
    path("analytics/returns/", ReturnsMetricsView.as_view(), name="analytics-returns"),
    path("analytics/liquidity-leverage/", LiquidityLeverageView.as_view(), name="analytics-liquidity-leverage"),
    path("analytics/efficiency/", EfficiencyMetricsView.as_view(), name="analytics-efficiency"),

    # Calculators
    path("analytics/calculators/investment/", InvestmentAppraisalView.as_view(), name="analytics-calc-investment"),
    path("analytics/calculators/npv/", NpvCalculatorView.as_view(), name="analytics-calc-npv"),
    path("analytics/calculators/irr/", IrrCalculatorView.as_view(), name="analytics-calc-irr"),
    path("analytics/calculators/break-even/", BreakEvenCalculatorView.as_view(), name="analytics-calc-break-even"),
    path("analytics/calculators/break-even-multiproduct/", MultiProductBreakEvenView.as_view(), name="analytics-calc-break-even-multi"),

    # Scenarios
    path("analytics/scenarios/", FinancialScenarioView.as_view(), name="analytics-scenarios"),

    # Account Classification & Completeness
    path("analytics/account-mappings/", AccountClassificationMappingView.as_view(), name="analytics-account-mappings"),
    path("analytics/account-mappings/completeness/", AccountMappingCompletenessView.as_view(), name="analytics-mapping-completeness"),

    # Legacy & AI Assistant
    path("analytics/cash-flow/", CashFlowSummaryView.as_view(), name="analytics-cash-flow"),
    path("analytics/receivables-aging/", ReceivablesAgingView.as_view(), name="analytics-receivables-aging"),
    path("analytics/anomalies/", FinancialAnomaliesView.as_view(), name="analytics-anomalies"),
    path("analytics/ai/management-summary/", AIManagementSummaryView.as_view(), name="analytics-ai-summary"),
    path("analytics/ai/explain-pnl/", AIExplainPnLView.as_view(), name="analytics-ai-explain-pnl"),
    path("analytics/ai/explain-ebitda/", AIExplainEbitdaView.as_view(), name="analytics-ai-explain-ebitda"),
]

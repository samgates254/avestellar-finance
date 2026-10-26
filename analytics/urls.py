from django.urls import path
from analytics.views import (
    AIExplainPnLView,
    AIManagementSummaryView,
    CashFlowSummaryView,
    FinancialAnomaliesView,
    ReceivablesAgingView,
)

urlpatterns = [
    path("analytics/cash-flow/", CashFlowSummaryView.as_view(), name="analytics-cash-flow"),
    path("analytics/receivables-aging/", ReceivablesAgingView.as_view(), name="analytics-receivables-aging"),
    path("analytics/anomalies/", FinancialAnomaliesView.as_view(), name="analytics-anomalies"),
    path("analytics/ai/management-summary/", AIManagementSummaryView.as_view(), name="analytics-ai-summary"),
    path("analytics/ai/explain-pnl/", AIExplainPnLView.as_view(), name="analytics-ai-explain-pnl"),
]

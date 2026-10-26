from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter

from analytics.services.ai_assistant import AIFinancialAssistantInterface
from analytics.services.financial_analysis import (
    detect_financial_anomalies,
    get_cash_flow_summary,
    get_receivables_aging,
)
from organizations.permissions import (
    IsAccountantOrAbove,
    IsOrganizationMember,
    get_request_organization,
)


@extend_schema_view(
    get=extend_schema(
        summary="Cash Flow Forecast",
        description="Calculates verified liquid cash on hand and projected near-term liquidity.",
        parameters=[OpenApiParameter(name="days_forward", type=int, description="Forecast horizon in days (default: 30)")],
        responses={200: OpenApiTypes.OBJECT},
    )
)
class CashFlowSummaryView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        days = int(request.query_params.get("days_forward", 30))
        data = get_cash_flow_summary(org, days_forward=days)
        return Response(data)


@extend_schema_view(
    get=extend_schema(
        summary="Receivables Aging Report",
        description="Groups outstanding receivables into 30-day aging brackets.",
        responses={200: OpenApiTypes.OBJECT},
    )
)
class ReceivablesAgingView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        data = get_receivables_aging(org)
        return Response(data)


@extend_schema_view(
    get=extend_schema(
        summary="Financial Anomaly Detection",
        description="Scans ledger and invoices for financial and risk anomalies.",
        responses={200: OpenApiTypes.OBJECT},
    )
)
class FinancialAnomaliesView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        data = detect_financial_anomalies(org)
        return Response(data)


@extend_schema_view(
    get=extend_schema(
        summary="AI Financial Management Summary",
        description="Executive synthesis of cash, margins, and operational health.",
        responses={200: OpenApiTypes.OBJECT},
    )
)
class AIManagementSummaryView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        assistant = AIFinancialAssistantInterface(organization=org)
        data = assistant.generate_management_summary()
        return Response(data)


@extend_schema_view(
    get=extend_schema(
        summary="AI P&L Statement Explanation",
        description="Reconciled narrative explanation of revenues, expenses, and margins.",
        responses={200: OpenApiTypes.OBJECT},
    )
)
class AIExplainPnLView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        assistant = AIFinancialAssistantInterface(organization=org)
        data = assistant.explain_financial_statement(statement_type="income_statement")
        return Response(data)

"""API views for financial intelligence, metric calculations, and decision engines."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

from django.utils.dateparse import parse_date
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView

from analytics.models import AccountClassificationMapping, CapitalExpenditure, FinancialScenario
from analytics.serializers import (
    AccountClassificationMappingSerializer,
    BreakEvenCalculatorRequestSerializer,
    CapitalExpenditureSerializer,
    FinancialScenarioRequestSerializer,
    FinancialScenarioSerializer,
    InvestmentCalculatorRequestSerializer,
    IrrCalculatorRequestSerializer,
    MultiProductBreakEvenRequestSerializer,
    NpvCalculatorRequestSerializer,
)
from analytics.services.ai_assistant import AIFinancialAssistantInterface
from analytics.services.financial_analysis import (
    detect_financial_anomalies,
    get_cash_flow_summary,
    get_receivables_aging,
)
from analytics.services.metrics import (
    AccountMappingService,
    BreakEvenService,
    CapitalAllocationService,
    EfficiencyService,
    FinancialDashboardService,
    FinancialScenarioService,
    InvestmentAppraisalService,
    LeverageService,
    LiquidityService,
    ProfitabilityService,
    metric_registry,
)
from organizations.permissions import (
    IsAccountantOrAbove,
    IsOrgAdminOrOwner,
    IsOrganizationMember,
    get_request_organization,
)


def _get_dates(request) -> tuple[datetime.date, datetime.date]:
    """Helper to parse required or default start and end dates."""
    today = datetime.date.today()
    default_start = today.replace(day=1)
    
    start_str = request.query_params.get("start_date")
    end_str = request.query_params.get("end_date")

    start_date = parse_date(start_str) if start_str else default_start
    end_date = parse_date(end_str) if end_str else today

    if not start_date or not end_date:
        start_date, end_date = default_start, today
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    return start_date, end_date


# ==============================================================================
# 1. CORE DASHBOARD & CATALOG
# ==============================================================================

@extend_schema_view(
    get=extend_schema(
        summary="Unified Financial Intelligence Dashboard",
        description="Aggregates profitability, CAPEX, working capital, liquidity, leverage, efficiency, and anomaly warnings.",
        parameters=[
            OpenApiParameter(name="start_date", type=str, description="Start date (YYYY-MM-DD)"),
            OpenApiParameter(name="end_date", type=str, description="End date (YYYY-MM-DD)"),
            OpenApiParameter(name="comparison_start_date", type=str, description="Comparison period start (YYYY-MM-DD)"),
            OpenApiParameter(name="comparison_end_date", type=str, description="Comparison period end (YYYY-MM-DD)"),
            OpenApiParameter(name="monthly_burn_rate", type=float, description="Estimated monthly cash burn rate for runway calculation"),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
)
class FinancialDashboardView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        start_date, end_date = _get_dates(request)
        comp_start_str = request.query_params.get("comparison_start_date")
        comp_end_str = request.query_params.get("comparison_end_date")
        comp_start = parse_date(comp_start_str) if comp_start_str else None
        comp_end = parse_date(comp_end_str) if comp_end_str else None

        burn_str = request.query_params.get("monthly_burn_rate")
        burn_rate = Decimal(burn_str) if burn_str else None

        service = FinancialDashboardService(org)
        data = service.get_dashboard(
            start_date=start_date,
            end_date=end_date,
            comparison_start_date=comp_start,
            comparison_end_date=comp_end,
            monthly_burn_rate=burn_rate,
        )
        return Response(data)


@extend_schema_view(
    get=extend_schema(
        summary="Financial Metric Registry Catalog",
        description="Returns complete metadata, formulas, units, interpretations, and limitations for all registered metrics.",
        responses={200: OpenApiTypes.OBJECT},
    )
)
class FinancialMetricCatalogView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        return Response({
            "metrics": [m.to_dict() for m in metric_registry.list_all()],
            "count": len(metric_registry.list_all()),
        })


# ==============================================================================
# 2. METRIC DOMAIN ENDPOINTS
# ==============================================================================

@extend_schema_view(
    get=extend_schema(
        summary="Profitability & Margin Analytics",
        description="Calculates gross/net revenue, OPEX, EBIT, EBITDA with formal reconciliation bridge, and all operating margins.",
        parameters=[
            OpenApiParameter(name="start_date", type=str, description="Start date (YYYY-MM-DD)"),
            OpenApiParameter(name="end_date", type=str, description="End date (YYYY-MM-DD)"),
            OpenApiParameter(name="comparison_start_date", type=str, description="Comparison start date (YYYY-MM-DD)"),
            OpenApiParameter(name="comparison_end_date", type=str, description="Comparison end date (YYYY-MM-DD)"),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
)
class ProfitabilityMetricsView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        start_date, end_date = _get_dates(request)
        comp_start_str = request.query_params.get("comparison_start_date")
        comp_end_str = request.query_params.get("comparison_end_date")
        comp_start = parse_date(comp_start_str) if comp_start_str else None
        comp_end = parse_date(comp_end_str) if comp_end_str else None

        service = ProfitabilityService(org)
        summary = service.calculate_margin_summary(start_date, end_date, comp_start, comp_end)

        result = {k: v.to_dict() if hasattr(v, "to_dict") else v for k, v in summary.items()}
        result["organization_id"] = str(org.id)
        result["base_currency"] = org.base_currency
        result["reporting_period"] = {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
        return Response(result)


@extend_schema_view(
    get=extend_schema(
        summary="Capital Expenditure (CAPEX) & Cost Intelligence",
        description="Tracks capital additions, CAPEX-to-revenue, CAPEX-to-depreciation, and fixed vs. variable OPEX split.",
        parameters=[
            OpenApiParameter(name="start_date", type=str, description="Start date (YYYY-MM-DD)"),
            OpenApiParameter(name="end_date", type=str, description="End date (YYYY-MM-DD)"),
        ],
        responses={200: OpenApiTypes.OBJECT},
    ),
    post=extend_schema(
        summary="Record Capital Expenditure Project",
        description="Creates an auditable capital expenditure item associated with asset accounts and projects.",
        request=CapitalExpenditureSerializer,
        responses={201: CapitalExpenditureSerializer},
    ),
)
class CapitalExpenditureView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        start_date, end_date = _get_dates(request)
        service = CapitalAllocationService(org)

        capex = service.calculate_capex(start_date, end_date)
        capex_rev = service.calculate_capex_to_revenue(start_date, end_date)
        capex_dep = service.calculate_capex_to_depreciation(start_date, end_date)
        cost_behavior = service.calculate_fixed_vs_variable_opex(start_date, end_date)

        records = CapitalExpenditure.objects.filter(
            organization=org,
            incurred_date__range=(start_date, end_date)
        ).order_by("-incurred_date")
        records_serializer = CapitalExpenditureSerializer(records, many=True)

        return Response({
            "organization_id": str(org.id),
            "currency": org.base_currency,
            "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            "capex": capex.to_dict(),
            "capex_to_revenue": capex_rev.to_dict(),
            "capex_to_depreciation": capex_dep.to_dict(),
            "cost_behavior": cost_behavior,
            "capex_records": records_serializer.data,
        })

    def post(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = CapitalExpenditureSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        capex_item = serializer.save(organization=org)
        return Response(CapitalExpenditureSerializer(capex_item).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        summary="Capital Allocation & Return Metrics",
        description="Calculates Return on Assets (ROA), Return on Equity (ROE), ROIC, and ROCE based on verified ledger balances.",
        parameters=[
            OpenApiParameter(name="start_date", type=str, description="Start date (YYYY-MM-DD)"),
            OpenApiParameter(name="end_date", type=str, description="End date (YYYY-MM-DD)"),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
)
class ReturnsMetricsView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        start_date, end_date = _get_dates(request)
        service = CapitalAllocationService(org)

        roa = service.calculate_roa(start_date, end_date)
        roe = service.calculate_roe(start_date, end_date)
        roic = service.calculate_roic(start_date, end_date)
        roce = service.calculate_roce(start_date, end_date)

        return Response({
            "organization_id": str(org.id),
            "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            "roa": roa.to_dict(),
            "roe": roe.to_dict(),
            "roic": roic.to_dict(),
            "roce": roce.to_dict(),
        })


@extend_schema_view(
    get=extend_schema(
        summary="Liquidity & Leverage Ratios",
        description="Calculates Working Capital, Current Ratio, Quick Ratio, Cash Ratio, Debt Ratios, and Interest Coverage.",
        parameters=[
            OpenApiParameter(name="as_of_date", type=str, description="As of date (YYYY-MM-DD)"),
            OpenApiParameter(name="start_date", type=str, description="Period start date for flow coverage ratios (YYYY-MM-DD)"),
            OpenApiParameter(name="end_date", type=str, description="Period end date for flow coverage ratios (YYYY-MM-DD)"),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
)
class LiquidityLeverageView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        as_of_str = request.query_params.get("as_of_date")
        start_date, end_date = _get_dates(request)
        as_of_date = parse_date(as_of_str) if as_of_str else end_date

        liq_svc = LiquidityService(org)
        lev_svc = LeverageService(org)

        return Response({
            "organization_id": str(org.id),
            "as_of_date": as_of_date.isoformat(),
            "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            "liquidity": {
                "working_capital": liq_svc.calculate_working_capital(as_of_date).to_dict(),
                "current_ratio": liq_svc.calculate_current_ratio(as_of_date).to_dict(),
                "quick_ratio": liq_svc.calculate_quick_ratio(as_of_date).to_dict(),
                "cash_ratio": liq_svc.calculate_cash_ratio(as_of_date).to_dict(),
            },
            "leverage": {
                "total_debt": lev_svc.calculate_total_debt(as_of_date).to_dict(),
                "net_debt": lev_svc.calculate_net_debt(as_of_date).to_dict(),
                "debt_to_equity": lev_svc.calculate_debt_to_equity(as_of_date).to_dict(),
                "debt_to_assets": lev_svc.calculate_debt_to_assets(as_of_date).to_dict(),
                "interest_coverage": lev_svc.calculate_interest_coverage(start_date, end_date).to_dict(),
                "net_debt_to_ebitda": lev_svc.calculate_net_debt_to_ebitda(start_date, end_date).to_dict(),
            },
        })


@extend_schema_view(
    get=extend_schema(
        summary="Operating Efficiency & Working Capital Cycles",
        description="Calculates Inventory Turnover, DIO, Receivables Turnover, DSO, Payables Turnover, DPO, CCC, and Asset Turnover.",
        parameters=[
            OpenApiParameter(name="start_date", type=str, description="Start date (YYYY-MM-DD)"),
            OpenApiParameter(name="end_date", type=str, description="End date (YYYY-MM-DD)"),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
)
class EfficiencyMetricsView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        start_date, end_date = _get_dates(request)
        svc = EfficiencyService(org)

        return Response({
            "organization_id": str(org.id),
            "period": {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
            "inventory_turnover": svc.calculate_inventory_turnover(start_date, end_date).to_dict(),
            "days_inventory_outstanding": svc.calculate_dio(start_date, end_date).to_dict(),
            "receivables_turnover": svc.calculate_receivables_turnover(start_date, end_date).to_dict(),
            "days_sales_outstanding": svc.calculate_dso(start_date, end_date).to_dict(),
            "payables_turnover": svc.calculate_payables_turnover(start_date, end_date).to_dict(),
            "days_payable_outstanding": svc.calculate_dpo(start_date, end_date).to_dict(),
            "cash_conversion_cycle": svc.calculate_cash_conversion_cycle(start_date, end_date).to_dict(),
            "asset_turnover": svc.calculate_asset_turnover(start_date, end_date).to_dict(),
        })


# ==============================================================================
# 3. INTERACTIVE INVESTMENT & BREAK-EVEN CALCULATORS
# ==============================================================================

@extend_schema_view(
    post=extend_schema(
        summary="Deterministic NPV Calculator",
        description="Calculates exact Net Present Value given an initial outlay, future net cash flows, and periodic discount rate.",
        request=NpvCalculatorRequestSerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
)
class NpvCalculatorView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        serializer = NpvCalculatorRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        svc = InvestmentAppraisalService()
        result = svc.calculate_npv(
            initial_investment=serializer.validated_data["initial_investment"],
            cash_flows=serializer.validated_data["cash_flows"],
            discount_rate=serializer.validated_data["discount_rate"],
        )
        return Response(result.to_dict())


@extend_schema_view(
    post=extend_schema(
        summary="Deterministic IRR Calculator",
        description="Calculates exact Internal Rate of Return using hybrid Newton-Raphson/Bisection solver with root multiplicity guards.",
        request=IrrCalculatorRequestSerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
)
class IrrCalculatorView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        serializer = IrrCalculatorRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        svc = InvestmentAppraisalService()
        result = svc.calculate_irr(
            initial_investment=serializer.validated_data["initial_investment"],
            cash_flows=serializer.validated_data["cash_flows"],
        )
        return Response(result.to_dict())


@extend_schema_view(
    post=extend_schema(
        summary="Comprehensive Investment Appraisal Suite",
        description="Evaluates NPV, IRR, Simple Payback, Discounted Payback, and Profitability Index against a configured hurdle rate.",
        request=InvestmentCalculatorRequestSerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
)
class InvestmentAppraisalView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        serializer = InvestmentCalculatorRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        svc = InvestmentAppraisalService()

        npv = svc.calculate_npv(data["initial_investment"], data["cash_flows"], data["discount_rate"])
        irr = svc.calculate_irr(data["initial_investment"], data["cash_flows"])
        payback = svc.calculate_payback_period(data["initial_investment"], data["cash_flows"], data["discount_rate"])
        pi = svc.calculate_profitability_index(data["initial_investment"], data["cash_flows"], data["discount_rate"])

        hurdle = data.get("hurdle_rate", Decimal("0"))
        meets_hurdle = (
            irr.status.value == "available"
            and irr.calculated_value is not None
            and irr.calculated_value >= hurdle
            and npv.calculated_value is not None
            and npv.calculated_value > Decimal("0")
        )

        return Response({
            "meets_hurdle": meets_hurdle,
            "hurdle_rate_pct": str(hurdle),
            "npv": npv.to_dict(),
            "irr": irr.to_dict(),
            "payback": payback.to_dict(),
            "profitability_index": pi.to_dict(),
        })


@extend_schema_view(
    post=extend_schema(
        summary="Single-Product Contribution & Break-Even Calculator",
        description="Calculates Contribution Margin, CM Ratio, Break-Even Units & Sales, and Margin of Safety.",
        request=BreakEvenCalculatorRequestSerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
)
class BreakEvenCalculatorView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        serializer = BreakEvenCalculatorRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        svc = BreakEvenService()

        be_res = svc.calculate_break_even(
            fixed_costs=data["fixed_costs"],
            unit_selling_price=data["unit_selling_price"],
            unit_variable_cost=data["unit_variable_cost"],
            actual_units=data.get("actual_units"),
        )

        target_profit_units = None
        if data.get("target_profit") is not None:
            tp_res = svc.calculate_target_profit_volume(
                fixed_costs=data["fixed_costs"],
                unit_selling_price=data["unit_selling_price"],
                unit_variable_cost=data["unit_variable_cost"],
                target_profit=data["target_profit"],
            )
            target_profit_units = tp_res.to_dict()

        return Response({
            "break_even_analysis": be_res,
            "target_profit_volume": target_profit_units,
        })


@extend_schema_view(
    post=extend_schema(
        summary="Multi-Product Break-Even Calculator",
        description="Calculates composite weighted-average contribution margin and company-wide break-even sales revenue across a product mix.",
        request=MultiProductBreakEvenRequestSerializer,
        responses={200: OpenApiTypes.OBJECT},
    )
)
class MultiProductBreakEvenView(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def post(self, request):
        serializer = MultiProductBreakEvenRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        svc = BreakEvenService()
        result = svc.calculate_multi_product_break_even(
            fixed_costs=serializer.validated_data["fixed_costs"],
            products=serializer.validated_data["products"],
        )
        return Response(result)


# ==============================================================================
# 4. SCENARIO MODELLING & SIMULATION
# ==============================================================================

@extend_schema_view(
    get=extend_schema(
        summary="List Saved Financial Scenarios",
        description="Lists all stored financial simulation scenarios for the organization.",
        responses={200: FinancialScenarioSerializer(many=True)},
    ),
    post=extend_schema(
        summary="Run Financial Scenario Simulation",
        description="Simulates pro-forma revenue, cost, profit, and working capital changes without altering ledger accounting records.",
        request=FinancialScenarioRequestSerializer,
        responses={200: OpenApiTypes.OBJECT},
    ),
)
class FinancialScenarioView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        scenarios = FinancialScenario.objects.filter(organization=org).order_by("-created_at")
        serializer = FinancialScenarioSerializer(scenarios, many=True)
        return Response(serializer.data)

    def post(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = FinancialScenarioRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        svc = FinancialScenarioService(org)
        simulation = svc.simulate_scenario(
            base_start_date=data["base_start_date"],
            base_end_date=data["base_end_date"],
            revenue_growth_pct=data["revenue_growth_pct"],
            cogs_change_pct=data["cogs_change_pct"],
            opex_change_pct=data["opex_change_pct"],
            planned_capex=data["planned_capex"],
            working_capital_buffer_pct=data["working_capital_buffer_pct"],
            save_scenario=data["save_scenario"],
            name=data["name"],
            description=data.get("description", ""),
        )
        return Response(simulation)


# ==============================================================================
# 5. ACCOUNT CLASSIFICATION MAPPINGS
# ==============================================================================

@extend_schema_view(
    get=extend_schema(
        summary="List Account Classification Mappings",
        description="Lists all explicit organization-specific financial classification mappings for chart-of-accounts items.",
        responses={200: AccountClassificationMappingSerializer(many=True)},
    ),
    post=extend_schema(
        summary="Create or Update Account Classification Mapping",
        description="Configures custom classification (e.g. COGS, OPEX, Debt, Cash) and cost behavior for an account.",
        request=AccountClassificationMappingSerializer,
        responses={201: AccountClassificationMappingSerializer},
    ),
)
class AccountClassificationMappingView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        mappings = AccountClassificationMapping.objects.filter(organization=org).select_related("account")
        serializer = AccountClassificationMappingSerializer(mappings, many=True)
        return Response(serializer.data)

    def post(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        serializer = AccountClassificationMappingSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        mapping, created = AccountClassificationMapping.objects.update_or_create(
            organization=org,
            account=serializer.validated_data["account"],
            defaults={
                "classification": serializer.validated_data["classification"],
                "cost_behavior": serializer.validated_data.get("cost_behavior", "FIXED"),
                "notes": serializer.validated_data.get("notes", ""),
            },
        )
        out_serializer = AccountClassificationMappingSerializer(mapping)
        return Response(out_serializer.data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


@extend_schema_view(
    get=extend_schema(
        summary="Account Mapping Completeness Audit",
        description="Scans chart of accounts to identify unmapped accounts and verify classification integrity.",
        responses={200: OpenApiTypes.OBJECT},
    )
)
class AccountMappingCompletenessView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        svc = AccountMappingService(org)
        completeness = svc.validate_mapping_completeness()
        return Response(completeness)


# ==============================================================================
# 6. EXISTING & EXTENDED ANALYTICS VIEWS
# ==============================================================================

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


@extend_schema_view(
    get=extend_schema(
        summary="AI EBITDA Bridge & Reconciliation Explanation",
        description="Provides a verified, auditable explanation of the reconciliation bridge from Net Income to EBITDA.",
        parameters=[
            OpenApiParameter(name="start_date", type=str, description="Start date (YYYY-MM-DD)"),
            OpenApiParameter(name="end_date", type=str, description="End date (YYYY-MM-DD)"),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
)
class AIExplainEbitdaView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        start_date, end_date = _get_dates(request)
        assistant = AIFinancialAssistantInterface(organization=org)
        data = assistant.explain_ebitda_reconciliation(start_date, end_date)
        return Response(data)

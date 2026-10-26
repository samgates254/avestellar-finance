from datetime import date
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter

from ledger.exceptions import AccountingError
from ledger.models import (
    Account,
    AccountingPeriod,
    JournalEntry,
    JournalStatus,
    PeriodStatus,
)
from ledger.serializers import (
    AccountSerializer,
    AccountingPeriodSerializer,
    JournalEntryDetailSerializer,
    JournalEntryListSerializer,
    ReverseJournalSerializer,
)
from ledger.services.posting import post_journal_entry, reverse_journal_entry
from ledger.services.reporting import (
    get_balance_sheet,
    get_income_statement,
    get_trial_balance,
)
from organizations.permissions import (
    IsAccountantOrAbove,
    IsOrgAdminOrOwner,
    IsOrganizationMember,
    get_request_organization,
)


@extend_schema_view(
    get=extend_schema(summary="List Chart of Accounts", description="List all accounts for the active organization."),
    post=extend_schema(summary="Create Account", description="Add a new ledger account to the Chart of Accounts."),
)
class AccountListCreateView(generics.ListCreateAPIView):
    serializer_class = AccountSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Account.objects.none()
        org = get_request_organization(self.request, self)
        if not org:
            return Account.objects.none()
        return Account.objects.filter(organization=org).order_by("code")

    def perform_create(self, serializer):
        org = get_request_organization(self.request, self)
        serializer.save(organization=org)


@extend_schema_view(
    get=extend_schema(summary="Get Account Detail", description="Retrieve account metadata and classification."),
    patch=extend_schema(summary="Update Account", description="Update account description or status."),
)
class AccountDetailView(generics.RetrieveUpdateAPIView):
    serializer_class = AccountSerializer
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Account.objects.none()
        org = get_request_organization(self.request, self)
        if not org:
            return Account.objects.none()
        return Account.objects.filter(organization=org)


@extend_schema_view(
    get=extend_schema(summary="List Accounting Periods", description="List fiscal periods for the organization."),
    post=extend_schema(summary="Create Accounting Period", description="Open a new fiscal accounting period."),
)
class AccountingPeriodListCreateView(generics.ListCreateAPIView):
    serializer_class = AccountingPeriodSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return AccountingPeriod.objects.none()
        org = get_request_organization(self.request, self)
        if not org:
            return AccountingPeriod.objects.none()
        return AccountingPeriod.objects.filter(organization=org).order_by("fiscal_year", "period_number")

    def perform_create(self, serializer):
        org = get_request_organization(self.request, self)
        serializer.save(organization=org)


@extend_schema_view(
    post=extend_schema(
        summary="Close Accounting Period",
        description="Close an accounting period to prevent new postings.",
        request=None,
        responses={200: AccountingPeriodSerializer},
    )
)
class AccountingPeriodCloseView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrgAdminOrOwner)

    def post(self, request, period_id):
        org = get_request_organization(request, self)
        period = get_object_or_404(AccountingPeriod, id=period_id, organization=org)
        period.status = PeriodStatus.CLOSED
        period.closed_at = timezone.now()
        period.closed_by = request.user
        period.save(update_fields=["status", "closed_at", "closed_by"])
        return Response(AccountingPeriodSerializer(period).data)


@extend_schema_view(
    get=extend_schema(summary="List Journal Entries", description="Query general ledger journal entries with status filtering."),
    post=extend_schema(summary="Create Draft Journal Entry", description="Create a balanced draft journal entry with lines."),
)
class JournalEntryListCreateView(generics.ListCreateAPIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def get_serializer_class(self):
        if self.request.method == "POST":
            return JournalEntryDetailSerializer
        return JournalEntryListSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return JournalEntry.objects.none()
        org = get_request_organization(self.request, self)
        if not org:
            return JournalEntry.objects.none()
        qs = JournalEntry.objects.filter(organization=org).order_by("-date", "-created_at")
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    def perform_create(self, serializer):
        org = get_request_organization(self.request, self)
        serializer.save(organization=org, created_by=self.request.user)


class JournalEntryDetailView(generics.RetrieveAPIView):
    serializer_class = JournalEntryDetailSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return JournalEntry.objects.none()
        org = get_request_organization(self.request, self)
        if not org:
            return JournalEntry.objects.none()
        return JournalEntry.objects.filter(organization=org).prefetch_related("lines__account")


@extend_schema_view(
    post=extend_schema(
        summary="Post Journal Entry",
        description="Validate debits/credits and post entry to the general ledger.",
        request=None,
        responses={200: JournalEntryDetailSerializer, 400: OpenApiTypes.OBJECT},
    )
)
class JournalEntryPostView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def post(self, request, entry_id):
        org = get_request_organization(request, self)
        entry = get_object_or_404(JournalEntry, id=entry_id, organization=org)
        try:
            posted_entry = post_journal_entry(entry.id, user=request.user)
            return Response(JournalEntryDetailSerializer(posted_entry).data, status=status.HTTP_200_OK)
        except AccountingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


@extend_schema_view(
    post=extend_schema(
        summary="Reverse Journal Entry",
        description="Execute formal accounting reversal for a posted entry.",
        request=ReverseJournalSerializer,
        responses={201: JournalEntryDetailSerializer, 400: OpenApiTypes.OBJECT},
    )
)
class JournalEntryReverseView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def post(self, request, entry_id):
        org = get_request_organization(request, self)
        entry = get_object_or_404(JournalEntry, id=entry_id, organization=org)
        serializer = ReverseJournalSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reversal_date = serializer.validated_data.get("reversal_date")
        reason = serializer.validated_data.get("reason", "Correction of posted entry")
        try:
            reversing_entry = reverse_journal_entry(
                entry.id, reversal_date=reversal_date, reason=reason, user=request.user
            )
            return Response(JournalEntryDetailSerializer(reversing_entry).data, status=status.HTTP_201_CREATED)
        except AccountingError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


# Financial Reporting Views
@extend_schema_view(
    get=extend_schema(
        summary="Trial Balance Report",
        description="Generates trial balance showing debit and credit totals across all accounts up to as_of_date.",
        parameters=[OpenApiParameter(name="as_of_date", type=str, description="Filter date in YYYY-MM-DD")],
        responses={200: OpenApiTypes.OBJECT},
    )
)
class TrialBalanceView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        as_of_str = request.query_params.get("as_of_date")
        as_of_date = date.fromisoformat(as_of_str) if as_of_str else timezone.now().date()
        data = get_trial_balance(org, as_of_date=as_of_date)
        return Response(data)


@extend_schema_view(
    get=extend_schema(
        summary="Income Statement (P&L)",
        description="Generates Income Statement report calculating revenues, expenses, and net profit.",
        parameters=[
            OpenApiParameter(name="start_date", type=str, description="Start date YYYY-MM-DD"),
            OpenApiParameter(name="end_date", type=str, description="End date YYYY-MM-DD"),
        ],
        responses={200: OpenApiTypes.OBJECT},
    )
)
class IncomeStatementView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        start_str = request.query_params.get("start_date")
        end_str = request.query_params.get("end_date")
        start_d = date.fromisoformat(start_str) if start_str else None
        end_d = date.fromisoformat(end_str) if end_str else timezone.now().date()
        data = get_income_statement(org, start_date=start_d, end_date=end_d)
        return Response(data)


@extend_schema_view(
    get=extend_schema(
        summary="Balance Sheet Report",
        description="Generates Balance Sheet report verifying Assets = Liabilities + Equity.",
        parameters=[OpenApiParameter(name="as_of_date", type=str, description="Filter date in YYYY-MM-DD")],
        responses={200: OpenApiTypes.OBJECT},
    )
)
class BalanceSheetView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        as_of_str = request.query_params.get("as_of_date")
        as_of_date = date.fromisoformat(as_of_str) if as_of_str else timezone.now().date()
        data = get_balance_sheet(org, as_of_date=as_of_date)
        return Response(data)

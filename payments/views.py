from rest_framework import generics, permissions, status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view

from automation.idempotency import idempotent_action
from organizations.permissions import (
    IsAccountantOrAbove,
    IsOrgAdminOrOwner,
    IsOrganizationMember,
    get_request_organization,
)
from payments.models import (
    Payment,
    ReconciliationSession,
    StatementImportBatch,
    StatementReconciliationStatus,
    StatementTransaction,
)
from payments.serializers import (
    ConfirmMatchRequestSerializer,
    CreateReconciliationSessionSerializer,
    InvestigateRequestSerializer,
    MatchCandidateSerializer,
    PaymentSerializer,
    ReconciliationSessionSerializer,
    RecordPaymentRequestSerializer,
    ReopenSessionRequestSerializer,
    StatementImportBatchSerializer,
    StatementImportUploadSerializer,
    StatementTransactionSerializer,
)
from payments.services import record_and_confirm_customer_payment
from payments.services.reconciliation import (
    DeterministicReconciliationEngine,
    create_reconciliation_session,
    execute_reconciliation_match,
    finalize_reconciliation_session,
    mark_transaction_for_investigation,
    reopen_reconciliation_session,
)
from payments.services.statement_import import import_statement_file


@extend_schema_view(
    get=extend_schema(
        summary="List Payments",
        description="List recorded payments and receipts.",
        responses={200: PaymentSerializer(many=True)},
    ),
    post=extend_schema(
        summary="Record Customer Payment",
        description="Settle an outstanding invoice, record payment, and post cash/receivable entries to general ledger.",
        request=RecordPaymentRequestSerializer,
        responses={201: PaymentSerializer, 400: OpenApiTypes.OBJECT},
    ),
)
class PaymentListCreateView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response([], status=status.HTTP_200_OK)
        payments = (
            Payment.objects.filter(organization=org)
            .select_related("customer", "supplier", "deposit_account", "journal_entry")
            .order_by("-payment_date", "-created_at")
        )
        serializer = PaymentSerializer(payments, many=True)
        return Response(serializer.data)

    @idempotent_action
    def post(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)
        
        serializer = RecordPaymentRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            payment = record_and_confirm_customer_payment(
                invoice_id=data["invoice_id"],
                amount=data["amount"],
                deposit_account_id=data["deposit_account_id"],
                payment_method=data.get("payment_method"),
                payment_number=data.get("payment_number"),
                reference=data.get("reference", ""),
                user=request.user,
                idempotency_key=data.get("idempotency_key", ""),
            )
            return Response(PaymentSerializer(payment).data, status=status.HTTP_201_CREATED)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class PaymentDetailView(generics.RetrieveAPIView):
    serializer_class = PaymentSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Payment.objects.none()
        org = get_request_organization(self.request, self)
        if not org:
            return Payment.objects.none()
        return Payment.objects.filter(organization=org).select_related(
            "customer", "supplier", "deposit_account", "journal_entry"
        )


@extend_schema_view(
    get=extend_schema(
        summary="List Statement Import Batches",
        description="Retrieve history of uploaded bank and M-Pesa statement import batches.",
        responses={200: StatementImportBatchSerializer(many=True)},
    ),
    post=extend_schema(
        summary="Upload Statement File",
        description="Upload and ingest a Bank CSV, M-Pesa CSV, OFX, or MT940 statement file with deduplication.",
        request=StatementImportUploadSerializer,
        responses={201: StatementImportBatchSerializer, 400: OpenApiTypes.OBJECT},
    ),
)
class StatementImportListCreateView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)
    parser_classes = (MultiPartParser, FormParser, JSONParser)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response([], status=status.HTTP_200_OK)
        batches = (
            StatementImportBatch.objects.filter(organization=org)
            .select_related("financial_account", "imported_by")
            .order_by("-created_at")
        )
        return Response(StatementImportBatchSerializer(batches, many=True).data)

    def post(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = StatementImportUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        file_obj = request.FILES.get("file")
        if file_obj:
            content = file_obj.read()
            file_name = file_obj.name
        elif data.get("file_content"):
            content = data["file_content"]
            file_name = data.get("file_name", "statement.csv")
        else:
            return Response(
                {"detail": "Either 'file' upload or 'file_content' string must be provided."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            batch = import_statement_file(
                organization=org,
                financial_account_id=str(data["financial_account_id"]),
                file_content=content,
                file_name=file_name,
                import_source=data.get("import_source", "BANK_CSV"),
                column_mapping=data.get("column_mapping"),
                user=request.user,
                date_format=data.get("date_format"),
            )
            return Response(
                StatementImportBatchSerializer(batch).data,
                status=status.HTTP_201_CREATED,
            )
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class StatementImportDetailView(generics.RetrieveAPIView):
    serializer_class = StatementImportBatchSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return StatementImportBatch.objects.none()
        org = get_request_organization(self.request, self)
        if not org:
            return StatementImportBatch.objects.none()
        return StatementImportBatch.objects.filter(organization=org).select_related(
            "financial_account", "imported_by"
        ).prefetch_related("transactions")


@extend_schema_view(
    get=extend_schema(
        summary="List Unreconciled Statement Transactions",
        description="Review queue listing statement lines that remain unmatched or require investigation.",
        responses={200: StatementTransactionSerializer(many=True)},
    )
)
class ReconciliationUnreconciledListView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response([], status=status.HTTP_200_OK)

        status_filter = request.query_params.get("status")
        account_id = request.query_params.get("account_id")

        qs = StatementTransaction.objects.filter(organization=org).select_related("financial_account", "batch")
        if status_filter:
            qs = qs.filter(reconciliation_status=status_filter)
        else:
            qs = qs.filter(
                reconciliation_status__in=[
                    StatementReconciliationStatus.UNRECONCILED,
                    StatementReconciliationStatus.MATCH_PROPOSED,
                    StatementReconciliationStatus.INVESTIGATION,
                ]
            )

        if account_id:
            qs = qs.filter(financial_account_id=account_id)

        return Response(StatementTransactionSerializer(qs.order_by("-transaction_date")[:100], many=True).data)


@extend_schema_view(
    get=extend_schema(
        summary="Get Match Candidates for Statement Transaction",
        description="Run deterministic matching rules and return ranked candidate payments or invoices.",
        responses={200: MatchCandidateSerializer(many=True)},
    )
)
class ReconciliationCandidatesView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def get(self, request, id):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            trx = StatementTransaction.objects.get(id=id, organization=org)
        except StatementTransaction.DoesNotExist:
            return Response({"detail": "Statement transaction not found."}, status=status.HTTP_404_NOT_FOUND)

        engine = DeterministicReconciliationEngine(organization=org)
        candidates = engine.find_candidates(trx)

        data = [
            {
                "candidate_type": c.candidate_type,
                "candidate_id": c.candidate_id,
                "rule_applied": c.rule_applied,
                "confidence_score": c.confidence_score,
                "confidence_category": c.confidence_category,
                "reason": c.reason,
                "auto_match_permitted": c.auto_match_permitted,
                "evidence_fields": c.evidence_fields,
                "candidate_summary": c.candidate_summary,
            }
            for c in candidates
        ]
        return Response(data, status=status.HTTP_200_OK)


@extend_schema_view(
    post=extend_schema(
        summary="Execute Reconciliation Match Decision",
        description="Confirm a proposed or manual match between a statement transaction and a payment or invoice.",
        request=ConfirmMatchRequestSerializer,
        responses={200: StatementTransactionSerializer, 400: OpenApiTypes.OBJECT},
    )
)
class ReconciliationMatchView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def post(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = ConfirmMatchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            reconciled_trx = execute_reconciliation_match(
                statement_trx_id=str(data["statement_transaction_id"]),
                candidate_type=data["candidate_type"],
                candidate_id=str(data["candidate_id"]),
                user=request.user,
                reason=data.get("reason", ""),
                rule_applied=data.get("rule_applied", "MANUAL_DECISION"),
                confidence_score=data.get("confidence_score", 100),
                confidence_category=data.get("confidence_category", "MANUAL"),
                session_id=str(data["session_id"]) if data.get("session_id") else None,
            )
            return Response(StatementTransactionSerializer(reconciled_trx).data, status=status.HTTP_200_OK)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


@extend_schema_view(
    post=extend_schema(
        summary="Mark Transaction for Investigation",
        description="Flag an unreconciled statement transaction as requiring investigation with an explanatory note.",
        request=InvestigateRequestSerializer,
        responses={200: StatementTransactionSerializer, 400: OpenApiTypes.OBJECT},
    )
)
class ReconciliationInvestigateView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def post(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = InvestigateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            trx = mark_transaction_for_investigation(
                statement_trx_id=str(data["statement_transaction_id"]),
                user=request.user,
                reason=data["reason"],
            )
            return Response(StatementTransactionSerializer(trx).data, status=status.HTTP_200_OK)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


@extend_schema_view(
    get=extend_schema(
        summary="List Reconciliation Sessions",
        description="Retrieve reconciliation sessions tracking statement vs ledger balances.",
        responses={200: ReconciliationSessionSerializer(many=True)},
    ),
    post=extend_schema(
        summary="Create Reconciliation Session",
        description="Initiate an accounting period reconciliation session with automatic ledger balance calculation.",
        request=CreateReconciliationSessionSerializer,
        responses={201: ReconciliationSessionSerializer, 400: OpenApiTypes.OBJECT},
    ),
)
class ReconciliationSessionListCreateView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def get(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response([], status=status.HTTP_200_OK)
        sessions = (
            ReconciliationSession.objects.filter(organization=org)
            .select_related("financial_account", "reconciled_by", "finalized_by")
            .order_by("-period_end", "-created_at")
        )
        return Response(ReconciliationSessionSerializer(sessions, many=True).data)

    def post(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = CreateReconciliationSessionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            session = create_reconciliation_session(
                organization=org,
                financial_account_id=str(data["financial_account_id"]),
                period_start=data["period_start"],
                period_end=data["period_end"],
                statement_opening_balance=data["statement_opening_balance"],
                statement_closing_balance=data["statement_closing_balance"],
                session_number=data.get("session_number"),
                user=request.user,
                notes=data.get("notes", ""),
            )
            return Response(ReconciliationSessionSerializer(session).data, status=status.HTTP_201_CREATED)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


class ReconciliationSessionDetailView(generics.RetrieveAPIView):
    serializer_class = ReconciliationSessionSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return ReconciliationSession.objects.none()
        org = get_request_organization(self.request, self)
        if not org:
            return ReconciliationSession.objects.none()
        return ReconciliationSession.objects.filter(organization=org).select_related(
            "financial_account", "reconciled_by", "finalized_by"
        )


@extend_schema_view(
    post=extend_schema(
        summary="Finalize Reconciliation Session",
        description="Verify statement and ledger differences, lock session, and record finalization audit trail.",
        request=None,
        responses={200: ReconciliationSessionSerializer, 400: OpenApiTypes.OBJECT},
    )
)
class ReconciliationSessionFinalizeView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def post(self, request, id):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        force = request.data.get("force", False) if isinstance(request.data, dict) else False
        try:
            session = finalize_reconciliation_session(
                session_id=str(id),
                user=request.user,
                force_finalize=bool(force),
            )
            return Response(ReconciliationSessionSerializer(session).data, status=status.HTTP_200_OK)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


@extend_schema_view(
    post=extend_schema(
        summary="Reopen Finalized Reconciliation Session",
        description="Authorized reopening of a previously finalized reconciliation session.",
        request=ReopenSessionRequestSerializer,
        responses={200: ReconciliationSessionSerializer, 400: OpenApiTypes.OBJECT},
    )
)
class ReconciliationSessionReopenView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrgAdminOrOwner)

    def post(self, request, id):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = ReopenSessionRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            session = reopen_reconciliation_session(
                session_id=str(id),
                user=request.user,
                reason=serializer.validated_data["reason"],
            )
            return Response(ReconciliationSessionSerializer(session).data, status=status.HTTP_200_OK)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

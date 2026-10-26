from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view

from automation.idempotency import idempotent_action
from organizations.permissions import (
    IsAccountantOrAbove,
    IsOrganizationMember,
    get_request_organization,
)
from payments.models import Payment
from payments.serializers import PaymentSerializer, RecordPaymentRequestSerializer
from payments.services import record_and_confirm_customer_payment


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

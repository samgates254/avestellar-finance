from django.shortcuts import get_object_or_404
from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiParameter

from automation.idempotency import idempotent_action
from invoicing.models import Customer, Invoice
from invoicing.serializers import CustomerSerializer, InvoiceSerializer
from invoicing.services import issue_invoice
from organizations.permissions import (
    IsAccountantOrAbove,
    IsOrganizationMember,
    get_request_organization,
)

# Optional client-generated key for replay-safe creation. See automation.idempotency.
IDEMPOTENCY_KEY_PARAM = OpenApiParameter(
    name="Idempotency-Key",
    type=OpenApiTypes.STR,
    location=OpenApiParameter.HEADER,
    required=False,
    description=(
        "Optional key that makes this creation replay-safe. Retrying with the same "
        "key and identical payload returns the original result instead of creating a "
        "duplicate record; reusing the key for a different payload returns 409. Keys "
        "are scoped to the organization and authenticated actor."
    ),
)


@extend_schema_view(
    get=extend_schema(summary="List Customers", description="List all customers for the active organization."),
    post=extend_schema(
        summary="Create Customer",
        description="Add a new customer profile. Supports the optional Idempotency-Key header for replay-safe creation.",
        parameters=[IDEMPOTENCY_KEY_PARAM],
    ),
)
class CustomerListCreateView(generics.ListCreateAPIView):
    serializer_class = CustomerSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    @idempotent_action
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Customer.objects.none()
        org = get_request_organization(self.request, self)
        if not org:
            return Customer.objects.none()
        return Customer.objects.filter(organization=org).order_by("name")

    def perform_create(self, serializer):
        org = get_request_organization(self.request, self)
        serializer.save(organization=org)


class CustomerDetailView(generics.RetrieveUpdateAPIView):
    serializer_class = CustomerSerializer
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Customer.objects.none()
        org = get_request_organization(self.request, self)
        if not org:
            return Customer.objects.none()
        return Customer.objects.filter(organization=org)


@extend_schema_view(
    get=extend_schema(summary="List Invoices", description="List sales invoices with customer details."),
    post=extend_schema(
        summary="Create Draft Invoice",
        description="Create a new draft sales invoice with line items. Supports the optional Idempotency-Key header for replay-safe creation.",
        parameters=[IDEMPOTENCY_KEY_PARAM],
    ),
)
class InvoiceListCreateView(generics.ListCreateAPIView):
    serializer_class = InvoiceSerializer
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    @idempotent_action
    def post(self, request, *args, **kwargs):
        return super().post(request, *args, **kwargs)

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Invoice.objects.none()
        org = get_request_organization(self.request, self)
        if not org:
            return Invoice.objects.none()
        qs = Invoice.objects.filter(organization=org).select_related("customer").prefetch_related("items__revenue_account")
        status_param = self.request.query_params.get("status")
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    def perform_create(self, serializer):
        org = get_request_organization(self.request, self)
        serializer.save(organization=org)


class InvoiceDetailView(generics.RetrieveAPIView):
    serializer_class = InvoiceSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)
    lookup_field = "id"

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Invoice.objects.none()
        org = get_request_organization(self.request, self)
        if not org:
            return Invoice.objects.none()
        return Invoice.objects.filter(organization=org).select_related("customer").prefetch_related("items__revenue_account")


@extend_schema_view(
    post=extend_schema(
        summary="Issue Invoice",
        description="Issue a draft invoice and post receivables to the general ledger.",
        request=None,
        responses={200: InvoiceSerializer, 400: OpenApiTypes.OBJECT},
    )
)
class InvoiceIssueView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def post(self, request, invoice_id):
        org = get_request_organization(request, self)
        invoice = get_object_or_404(Invoice, id=invoice_id, organization=org)
        try:
            issued = issue_invoice(invoice.id, user=request.user)
            return Response(InvoiceSerializer(issued).data, status=status.HTTP_200_OK)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

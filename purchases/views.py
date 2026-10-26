from rest_framework import generics, permissions
from drf_spectacular.utils import extend_schema, extend_schema_view
from organizations.permissions import (
    IsAccountantOrAbove,
    IsOrganizationMember,
    get_request_organization,
)
from purchases.models import Supplier, SupplierBill
from purchases.serializers import SupplierBillSerializer, SupplierSerializer


@extend_schema_view(
    get=extend_schema(summary="List Suppliers", description="List vendor suppliers for accounts payable."),
    post=extend_schema(summary="Create Supplier", description="Add a new supplier profile."),
)
class SupplierListCreateView(generics.ListCreateAPIView):
    serializer_class = SupplierSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get_queryset(self):
        org = get_request_organization(self.request, self)
        if not org:
            return Supplier.objects.none()
        return Supplier.objects.filter(organization=org).order_by("name")

    def perform_create(self, serializer):
        org = get_request_organization(self.request, self)
        serializer.save(organization=org)


class SupplierDetailView(generics.RetrieveUpdateAPIView):
    serializer_class = SupplierSerializer
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)
    lookup_field = "id"

    def get_queryset(self):
        org = get_request_organization(self.request, self)
        if not org:
            return Supplier.objects.none()
        return Supplier.objects.filter(organization=org)


@extend_schema_view(
    get=extend_schema(summary="List Supplier Bills", description="List accounts payable supplier bills."),
    post=extend_schema(summary="Create Supplier Bill", description="Record a new supplier bill."),
)
class SupplierBillListCreateView(generics.ListCreateAPIView):
    serializer_class = SupplierBillSerializer
    permission_classes = (permissions.IsAuthenticated, IsAccountantOrAbove)

    def get_queryset(self):
        org = get_request_organization(self.request, self)
        if not org:
            return SupplierBill.objects.none()
        return SupplierBill.objects.filter(organization=org).select_related("supplier")

    def perform_create(self, serializer):
        org = get_request_organization(self.request, self)
        serializer.save(organization=org)


class SupplierBillDetailView(generics.RetrieveAPIView):
    serializer_class = SupplierBillSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)
    lookup_field = "id"

    def get_queryset(self):
        org = get_request_organization(self.request, self)
        if not org:
            return SupplierBill.objects.none()
        return SupplierBill.objects.filter(organization=org).select_related("supplier")

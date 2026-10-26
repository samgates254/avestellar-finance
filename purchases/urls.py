from django.urls import path
from purchases.views import (
    SupplierBillDetailView,
    SupplierBillListCreateView,
    SupplierDetailView,
    SupplierListCreateView,
)

urlpatterns = [
    path("purchases/suppliers/", SupplierListCreateView.as_view(), name="supplier-list-create"),
    path("purchases/suppliers/<uuid:id>/", SupplierDetailView.as_view(), name="supplier-detail"),
    path("purchases/bills/", SupplierBillListCreateView.as_view(), name="supplier-bill-list-create"),
    path("purchases/bills/<uuid:id>/", SupplierBillDetailView.as_view(), name="supplier-bill-detail"),
]

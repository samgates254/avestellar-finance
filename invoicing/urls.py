from django.urls import path
from invoicing.views import (
    CustomerDetailView,
    CustomerListCreateView,
    InvoiceDetailView,
    InvoiceIssueView,
    InvoiceListCreateView,
)

urlpatterns = [
    path("invoicing/customers/", CustomerListCreateView.as_view(), name="customer-list-create"),
    path("invoicing/customers/<uuid:id>/", CustomerDetailView.as_view(), name="customer-detail"),
    path("invoicing/invoices/", InvoiceListCreateView.as_view(), name="invoice-list-create"),
    path("invoicing/invoices/<uuid:id>/", InvoiceDetailView.as_view(), name="invoice-detail"),
    path("invoicing/invoices/<uuid:invoice_id>/issue/", InvoiceIssueView.as_view(), name="invoice-issue"),
]

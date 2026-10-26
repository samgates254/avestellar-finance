from django.urls import path
from payments.views import PaymentDetailView, PaymentListCreateView

urlpatterns = [
    path("payments/", PaymentListCreateView.as_view(), name="payment-list-create"),
    path("payments/<uuid:id>/", PaymentDetailView.as_view(), name="payment-detail"),
]

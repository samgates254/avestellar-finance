from django.urls import path
from payments.views import (
    PaymentDetailView,
    PaymentListCreateView,
    ReconciliationCandidatesView,
    ReconciliationInvestigateView,
    ReconciliationMatchView,
    ReconciliationSessionDetailView,
    ReconciliationSessionFinalizeView,
    ReconciliationSessionListCreateView,
    ReconciliationSessionReopenView,
    ReconciliationUnreconciledListView,
    StatementImportDetailView,
    StatementImportListCreateView,
)

urlpatterns = [
    # Payments
    path("payments/", PaymentListCreateView.as_view(), name="payment-list-create"),
    path("payments/<uuid:id>/", PaymentDetailView.as_view(), name="payment-detail"),

    # Statement Imports
    path("payments/statement-imports/", StatementImportListCreateView.as_view(), name="statement-import-list-create"),
    path("payments/statement-imports/<uuid:id>/", StatementImportDetailView.as_view(), name="statement-import-detail"),

    # Reconciliation Queue & Candidates
    path("payments/reconciliation/unreconciled/", ReconciliationUnreconciledListView.as_view(), name="reconciliation-unreconciled-list"),
    path("payments/reconciliation/candidates/<uuid:id>/", ReconciliationCandidatesView.as_view(), name="reconciliation-candidates"),
    path("payments/reconciliation/match/", ReconciliationMatchView.as_view(), name="reconciliation-match"),
    path("payments/reconciliation/investigate/", ReconciliationInvestigateView.as_view(), name="reconciliation-investigate"),

    # Reconciliation Sessions
    path("payments/reconciliation/sessions/", ReconciliationSessionListCreateView.as_view(), name="reconciliation-session-list-create"),
    path("payments/reconciliation/sessions/<uuid:id>/", ReconciliationSessionDetailView.as_view(), name="reconciliation-session-detail"),
    path("payments/reconciliation/sessions/<uuid:id>/finalize/", ReconciliationSessionFinalizeView.as_view(), name="reconciliation-session-finalize"),
    path("payments/reconciliation/sessions/<uuid:id>/reopen/", ReconciliationSessionReopenView.as_view(), name="reconciliation-session-reopen"),
]

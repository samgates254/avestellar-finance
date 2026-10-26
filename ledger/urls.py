from django.urls import path
from ledger.views import (
    AccountDetailView,
    AccountListCreateView,
    AccountingPeriodCloseView,
    AccountingPeriodListCreateView,
    BalanceSheetView,
    IncomeStatementView,
    JournalEntryDetailView,
    JournalEntryListCreateView,
    JournalEntryPostView,
    JournalEntryReverseView,
    TrialBalanceView,
)

urlpatterns = [
    # Chart of Accounts
    path("ledger/accounts/", AccountListCreateView.as_view(), name="account-list-create"),
    path("ledger/accounts/<uuid:id>/", AccountDetailView.as_view(), name="account-detail"),
    # Accounting Periods
    path("ledger/periods/", AccountingPeriodListCreateView.as_view(), name="period-list-create"),
    path("ledger/periods/<uuid:period_id>/close/", AccountingPeriodCloseView.as_view(), name="period-close"),
    # Journal Entries
    path("ledger/journals/", JournalEntryListCreateView.as_view(), name="journal-list-create"),
    path("ledger/journals/<uuid:id>/", JournalEntryDetailView.as_view(), name="journal-detail"),
    path("ledger/journals/<uuid:entry_id>/post/", JournalEntryPostView.as_view(), name="journal-post"),
    path("ledger/journals/<uuid:entry_id>/reverse/", JournalEntryReverseView.as_view(), name="journal-reverse"),
    # Financial Reports
    path("reports/trial-balance/", TrialBalanceView.as_view(), name="report-trial-balance"),
    path("reports/income-statement/", IncomeStatementView.as_view(), name="report-income-statement"),
    path("reports/balance-sheet/", BalanceSheetView.as_view(), name="report-balance-sheet"),
]

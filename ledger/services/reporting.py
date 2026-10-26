from datetime import date
from decimal import Decimal
from django.db.models import Sum
from ledger.models import (
    Account,
    AccountCategory,
    JournalLine,
    JournalStatus,
)
from organizations.models import Organization


def get_trial_balance(organization: Organization, as_of_date: date | None = None) -> dict:
    """Generates the Trial Balance for an organization up to as_of_date.

    Only includes POSTED journal entries.
    Returns:
      - accounts: list of accounts with code, name, category, debit_balance, credit_balance
      - total_debit: sum of all debit balances
      - total_credit: sum of all credit balances
      - is_balanced: boolean indicating debit_total == credit_total
    """
    lines_qs = JournalLine.objects.filter(
        journal_entry__organization=organization,
        journal_entry__status=JournalStatus.POSTED,
    )
    if as_of_date:
        lines_qs = lines_qs.filter(journal_entry__date__lte=as_of_date)

    accounts = Account.objects.filter(organization=organization, is_active=True).order_by("code")
    accounts_data = []
    total_debit = Decimal("0.0000")
    total_credit = Decimal("0.0000")

    for acc in accounts:
        acc_lines = lines_qs.filter(account=acc)
        agg = acc_lines.aggregate(sum_dr=Sum("debit"), sum_cr=Sum("credit"))
        dr = agg["sum_dr"] or Decimal("0.0000")
        cr = agg["sum_cr"] or Decimal("0.0000")

        if dr == Decimal("0.0000") and cr == Decimal("0.0000"):
            continue

        if dr >= cr:
            debit_bal = dr - cr
            credit_bal = Decimal("0.0000")
        else:
            debit_bal = Decimal("0.0000")
            credit_bal = cr - dr

        total_debit += debit_bal
        total_credit += credit_bal

        accounts_data.append({
            "account_id": str(acc.id),
            "code": acc.code,
            "name": acc.name,
            "category": acc.category,
            "debit_balance": str(debit_bal),
            "credit_balance": str(credit_bal),
        })

    return {
        "as_of_date": str(as_of_date) if as_of_date else None,
        "currency": organization.currency,
        "accounts": accounts_data,
        "total_debit": str(total_debit),
        "total_credit": str(total_credit),
        "is_balanced": total_debit == total_credit,
    }


def get_income_statement(
    organization: Organization,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict:
    """Generates the Income Statement (Profit & Loss) for a specified date range.

    Revenues (Credit - Debit)
    Expenses (Debit - Credit)
    Net Income = Revenues - Expenses
    """
    lines_qs = JournalLine.objects.filter(
        journal_entry__organization=organization,
        journal_entry__status=JournalStatus.POSTED,
    )
    if start_date:
        lines_qs = lines_qs.filter(journal_entry__date__gte=start_date)
    if end_date:
        lines_qs = lines_qs.filter(journal_entry__date__lte=end_date)

    # Calculate revenues
    revenue_accounts = Account.objects.filter(
        organization=organization, category=AccountCategory.REVENUE, is_active=True
    ).order_by("code")
    revenue_lines = []
    total_revenue = Decimal("0.0000")

    for acc in revenue_accounts:
        agg = lines_qs.filter(account=acc).aggregate(sum_dr=Sum("debit"), sum_cr=Sum("credit"))
        dr = agg["sum_dr"] or Decimal("0.0000")
        cr = agg["sum_cr"] or Decimal("0.0000")
        net = cr - dr
        if net != Decimal("0.0000"):
            revenue_lines.append({
                "account_id": str(acc.id),
                "code": acc.code,
                "name": acc.name,
                "amount": str(net),
            })
            total_revenue += net

    # Calculate expenses
    expense_accounts = Account.objects.filter(
        organization=organization, category=AccountCategory.EXPENSE, is_active=True
    ).order_by("code")
    expense_lines = []
    total_expense = Decimal("0.0000")

    for acc in expense_accounts:
        agg = lines_qs.filter(account=acc).aggregate(sum_dr=Sum("debit"), sum_cr=Sum("credit"))
        dr = agg["sum_dr"] or Decimal("0.0000")
        cr = agg["sum_cr"] or Decimal("0.0000")
        net = dr - cr
        if net != Decimal("0.0000"):
            expense_lines.append({
                "account_id": str(acc.id),
                "code": acc.code,
                "name": acc.name,
                "amount": str(net),
            })
            total_expense += net

    net_income = total_revenue - total_expense

    return {
        "start_date": str(start_date) if start_date else None,
        "end_date": str(end_date) if end_date else None,
        "currency": organization.currency,
        "revenues": revenue_lines,
        "total_revenue": str(total_revenue),
        "expenses": expense_lines,
        "total_expense": str(total_expense),
        "net_income": str(net_income),
    }


def get_balance_sheet(organization: Organization, as_of_date: date | None = None) -> dict:
    """Generates the Balance Sheet as of a specific date.

    Assets = Liabilities + Equity (including current period retained earnings)
    """
    lines_qs = JournalLine.objects.filter(
        journal_entry__organization=organization,
        journal_entry__status=JournalStatus.POSTED,
    )
    if as_of_date:
        lines_qs = lines_qs.filter(journal_entry__date__lte=as_of_date)

    # 1. Assets (Dr - Cr)
    asset_accounts = Account.objects.filter(
        organization=organization, category=AccountCategory.ASSET, is_active=True
    ).order_by("code")
    asset_lines = []
    total_assets = Decimal("0.0000")
    for acc in asset_accounts:
        agg = lines_qs.filter(account=acc).aggregate(sum_dr=Sum("debit"), sum_cr=Sum("credit"))
        dr = agg["sum_dr"] or Decimal("0.0000")
        cr = agg["sum_cr"] or Decimal("0.0000")
        net = dr - cr
        if net != Decimal("0.0000"):
            asset_lines.append({"code": acc.code, "name": acc.name, "amount": str(net)})
            total_assets += net

    # 2. Liabilities (Cr - Dr)
    liability_accounts = Account.objects.filter(
        organization=organization, category=AccountCategory.LIABILITY, is_active=True
    ).order_by("code")
    liability_lines = []
    total_liabilities = Decimal("0.0000")
    for acc in liability_accounts:
        agg = lines_qs.filter(account=acc).aggregate(sum_dr=Sum("debit"), sum_cr=Sum("credit"))
        dr = agg["sum_dr"] or Decimal("0.0000")
        cr = agg["sum_cr"] or Decimal("0.0000")
        net = cr - dr
        if net != Decimal("0.0000"):
            liability_lines.append({"code": acc.code, "name": acc.name, "amount": str(net)})
            total_liabilities += net

    # 3. Equity (Cr - Dr)
    equity_accounts = Account.objects.filter(
        organization=organization, category=AccountCategory.EQUITY, is_active=True
    ).order_by("code")
    equity_lines = []
    base_equity = Decimal("0.0000")
    for acc in equity_accounts:
        agg = lines_qs.filter(account=acc).aggregate(sum_dr=Sum("debit"), sum_cr=Sum("credit"))
        dr = agg["sum_dr"] or Decimal("0.0000")
        cr = agg["sum_cr"] or Decimal("0.0000")
        net = cr - dr
        if net != Decimal("0.0000"):
            equity_lines.append({"code": acc.code, "name": acc.name, "amount": str(net)})
            base_equity += net

    # 4. Current period retained earnings from income statement
    income_stmt = get_income_statement(organization, end_date=as_of_date)
    current_earnings = Decimal(income_stmt["net_income"])
    total_equity = base_equity + current_earnings

    total_liabilities_and_equity = total_liabilities + total_equity

    return {
        "as_of_date": str(as_of_date) if as_of_date else None,
        "currency": organization.currency,
        "assets": asset_lines,
        "total_assets": str(total_assets),
        "liabilities": liability_lines,
        "total_liabilities": str(total_liabilities),
        "equity": equity_lines,
        "current_period_earnings": str(current_earnings),
        "total_equity": str(total_equity),
        "total_liabilities_and_equity": str(total_liabilities_and_equity),
        "is_balanced": total_assets == total_liabilities_and_equity,
    }

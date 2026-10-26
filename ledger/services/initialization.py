from datetime import date
from django.utils import timezone
from ledger.models import Account, AccountCategory, AccountingPeriod, PeriodStatus
from organizations.models import Organization


STANDARD_ACCOUNTS = [
    # Assets (1000 - 1999)
    {"code": "1000", "name": "Cash on Hand", "category": AccountCategory.ASSET, "sub_category": "Current Assets", "is_system_account": True, "system_role": "CASH"},
    {"code": "1010", "name": "Operating Bank Account", "category": AccountCategory.ASSET, "sub_category": "Current Assets", "is_system_account": True, "system_role": "BANK"},
    {"code": "1020", "name": "M-Pesa Clearing Account", "category": AccountCategory.ASSET, "sub_category": "Current Assets", "is_system_account": True, "system_role": "CLEARING"},
    {"code": "1200", "name": "Accounts Receivable", "category": AccountCategory.ASSET, "sub_category": "Current Assets", "is_system_account": True, "system_role": "AR"},
    {"code": "1400", "name": "Inventory Asset", "category": AccountCategory.ASSET, "sub_category": "Current Assets", "is_system_account": True, "system_role": "INVENTORY"},
    
    # Liabilities (2000 - 2999)
    {"code": "2000", "name": "Accounts Payable", "category": AccountCategory.LIABILITY, "sub_category": "Current Liabilities", "is_system_account": True, "system_role": "AP"},
    {"code": "2200", "name": "Sales Tax / VAT Payable (16%)", "category": AccountCategory.LIABILITY, "sub_category": "Current Liabilities", "is_system_account": True, "system_role": "TAX_PAYABLE"},
    
    # Equity (3000 - 3999)
    {"code": "3000", "name": "Owner's Equity", "category": AccountCategory.EQUITY, "sub_category": "Equity", "is_system_account": True, "system_role": "EQUITY"},
    {"code": "3100", "name": "Retained Earnings", "category": AccountCategory.EQUITY, "sub_category": "Equity", "is_system_account": True, "system_role": "RETAINED_EARNINGS"},
    
    # Revenue (4000 - 4999)
    {"code": "4000", "name": "Sales & Services Revenue", "category": AccountCategory.REVENUE, "sub_category": "Operating Revenue", "is_system_account": True, "system_role": "REVENUE"},
    
    # Expenses (5000 - 6999)
    {"code": "5000", "name": "Cost of Goods Sold (COGS)", "category": AccountCategory.EXPENSE, "sub_category": "Cost of Sales", "is_system_account": True, "system_role": "COGS"},
    {"code": "6000", "name": "General & Administrative Expenses", "category": AccountCategory.EXPENSE, "sub_category": "Operating Expenses", "is_system_account": True, "system_role": "EXPENSE"},
    {"code": "6100", "name": "Bank & Payment Processing Fees", "category": AccountCategory.EXPENSE, "sub_category": "Operating Expenses", "is_system_account": True, "system_role": "FEES"},
]


def initialize_organization_accounts(organization: Organization, user=None) -> list[Account]:
    """Sets up a foundational Chart of Accounts and current Accounting Period for a new tenant."""
    created_accounts = []
    for item in STANDARD_ACCOUNTS:
        acc, _ = Account.objects.get_or_create(
            organization=organization,
            code=item["code"],
            defaults={
                "name": item["name"],
                "category": item["category"],
                "sub_category": item["sub_category"],
                "is_system_account": item.get("is_system_account", False),
                "system_role": item.get("system_role", ""),
                "currency": organization.currency,
            },
        )
        created_accounts.append(acc)

    # Create current fiscal year periods (12 monthly periods)
    today = timezone.now().date()
    current_year = today.year
    for month in range(1, 13):
        # Approximate month start and end dates
        import calendar
        _, last_day = calendar.monthrange(current_year, month)
        start_d = date(current_year, month, 1)
        end_d = date(current_year, month, last_day)

        AccountingPeriod.objects.get_or_create(
            organization=organization,
            fiscal_year=current_year,
            period_number=month,
            defaults={
                "name": f"FY {current_year} - Month {month:02d}",
                "start_date": start_d,
                "end_date": end_d,
                "status": PeriodStatus.OPEN,
            },
        )

    return created_accounts

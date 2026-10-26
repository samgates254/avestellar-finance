from datetime import date, timedelta
from decimal import Decimal
from django.db.models import Sum
from django.utils import timezone

from invoicing.models import Invoice, InvoiceStatus
from ledger.models import Account, AccountCategory, JournalLine, JournalStatus
from organizations.models import Organization
from purchases.models import BillStatus, SupplierBill


def get_cash_flow_summary(organization: Organization, days_forward: int = 30) -> dict:
    """Calculates deterministic verified cash balances and projected near-term liquidity."""
    today = timezone.now().date()
    horizon_date = today + timedelta(days=days_forward)

    # 1. Verified liquid cash/bank/clearing balances from general ledger
    liquid_accounts = Account.objects.filter(
        organization=organization,
        category=AccountCategory.ASSET,
        system_role__in=["CASH", "BANK", "CLEARING"],
        is_active=True,
    )
    lines_qs = JournalLine.objects.filter(
        journal_entry__organization=organization,
        journal_entry__status=JournalStatus.POSTED,
        account__in=liquid_accounts,
    )
    agg = lines_qs.aggregate(total_dr=Sum("debit"), total_cr=Sum("credit"))
    dr = agg["total_dr"] or Decimal("0.0000")
    cr = agg["total_cr"] or Decimal("0.0000")
    current_cash = dr - cr

    # 2. Expected Inflows: Outstanding Customer Invoices due within window
    invoices = Invoice.objects.filter(
        organization=organization,
        status__in=[InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID],
        due_date__lte=horizon_date,
    )
    inflows = invoices.aggregate(total=Sum("outstanding_balance"))["total"] or Decimal("0.0000")

    # 3. Expected Outflows: Outstanding Supplier Bills due within window
    bills = SupplierBill.objects.filter(
        organization=organization,
        status__in=[BillStatus.RECEIVED, BillStatus.PARTIALLY_PAID],
        due_date__lte=horizon_date,
    )
    outflows = bills.aggregate(total=Sum("outstanding_balance"))["total"] or Decimal("0.0000")

    projected_balance = current_cash + inflows - outflows

    return {
        "as_of_date": str(today),
        "horizon_date": str(horizon_date),
        "currency": organization.currency,
        "verified_current_cash": str(current_cash),
        "expected_receivables_inflow": str(inflows),
        "expected_payables_outflow": str(outflows),
        "projected_net_cash": str(projected_balance),
        "days_forward": days_forward,
    }


def get_receivables_aging(organization: Organization) -> dict:
    """Calculates aging buckets for all issued and partially paid customer invoices."""
    today = timezone.now().date()
    invoices = Invoice.objects.filter(
        organization=organization,
        status__in=[InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID],
    ).select_related("customer")

    current = Decimal("0.0000")
    overdue_1_30 = Decimal("0.0000")
    overdue_31_60 = Decimal("0.0000")
    overdue_61_90 = Decimal("0.0000")
    overdue_over_90 = Decimal("0.0000")

    overdue_invoices_list = []
    for inv in invoices:
        balance = inv.outstanding_balance
        if inv.due_date >= today:
            current += balance
        else:
            days = (today - inv.due_date).days
            if days <= 30:
                overdue_1_30 += balance
            elif days <= 60:
                overdue_31_60 += balance
            elif days <= 90:
                overdue_61_90 += balance
            else:
                overdue_over_90 += balance

            overdue_invoices_list.append({
                "invoice_id": str(inv.id),
                "invoice_number": inv.invoice_number,
                "customer_name": inv.customer.name,
                "due_date": str(inv.due_date),
                "days_overdue": days,
                "outstanding_balance": str(balance),
            })

    total_receivables = current + overdue_1_30 + overdue_31_60 + overdue_61_90 + overdue_over_90

    return {
        "as_of_date": str(today),
        "currency": organization.currency,
        "total_receivables": str(total_receivables),
        "buckets": {
            "current": str(current),
            "1_30_days": str(overdue_1_30),
            "31_60_days": str(overdue_31_60),
            "61_90_days": str(overdue_61_90),
            "over_90_days": str(overdue_over_90),
        },
        "overdue_invoices": sorted(overdue_invoices_list, key=lambda x: x["days_overdue"], reverse=True),
    }


def detect_financial_anomalies(organization: Organization) -> list[dict]:
    """Deterministically identifies accounting and financial risk indicators."""
    anomalies = []
    today = timezone.now().date()

    # 1. Unusually aged receivables (> 60 days overdue)
    aged_invoices = Invoice.objects.filter(
        organization=organization,
        status__in=[InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID],
        due_date__lt=today - timedelta(days=60),
    ).select_related("customer")
    for inv in aged_invoices:
        days = (today - inv.due_date).days
        anomalies.append({
            "code": "OVERDUE_RECEIVABLE_SEVERE",
            "severity": "HIGH",
            "entity": "Invoice",
            "entity_id": str(inv.id),
            "message": f"Invoice {inv.invoice_number} for {inv.customer.name} is {days} days overdue (Balance: {inv.outstanding_balance} {inv.currency}).",
        })

    # 2. Inactive accounts with non-zero balances
    inactive_accounts = Account.objects.filter(organization=organization, is_active=False)
    for acc in inactive_accounts:
        agg = JournalLine.objects.filter(
            journal_entry__organization=organization,
            journal_entry__status=JournalStatus.POSTED,
            account=acc,
        ).aggregate(dr=Sum("debit"), cr=Sum("credit"))
        dr = agg["dr"] or Decimal("0.0000")
        cr = agg["cr"] or Decimal("0.0000")
        if dr != cr:
            anomalies.append({
                "code": "INACTIVE_ACCOUNT_WITH_BALANCE",
                "severity": "MEDIUM",
                "entity": "Account",
                "entity_id": str(acc.id),
                "message": f"Deactivated account {acc.code} ({acc.name}) maintains an unliquidated balance.",
            })

    return anomalies

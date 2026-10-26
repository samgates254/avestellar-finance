from datetime import date
from decimal import Decimal
from django.db.models import Sum
from analytics.models import AccountClassificationMapping, CostBehavior, FinancialClassification
from ledger.models import Account, AccountCategory, JournalLine, JournalStatus
from organizations.models import Organization


class AccountMappingService:
    """Manages organization-specific chart-of-accounts classification and net balance resolution."""

    def __init__(self, organization: Organization):
        self.organization = organization

    def get_accounts_for_classification(self, classification: str | FinancialClassification) -> list[Account]:
        """Resolves all accounts mapped to a specific classification, checking explicit mappings then system heuristics."""
        cls_val = classification.value if isinstance(classification, FinancialClassification) else str(classification)

        # 1. Check explicit mappings
        explicit_mappings = AccountClassificationMapping.objects.filter(
            organization=self.organization,
            classification=cls_val,
            account__is_active=True,
        ).select_related("account")
        if explicit_mappings.exists():
            return [m.account for m in explicit_mappings]

        # 2. Heuristic fallback based on system roles, categories, and standard codes
        all_accounts = Account.objects.filter(organization=self.organization, is_active=True)

        if cls_val == FinancialClassification.REVENUE:
            return list(all_accounts.filter(category=AccountCategory.REVENUE).exclude(code__startswith="49"))
        elif cls_val == FinancialClassification.CONTRA_REVENUE:
            return list(all_accounts.filter(category=AccountCategory.REVENUE, code__startswith="49"))
        elif cls_val == FinancialClassification.COGS:
            return list(
                all_accounts.filter(category=AccountCategory.EXPENSE).filter(
                    system_role="COGS"
                ) | all_accounts.filter(category=AccountCategory.EXPENSE, sub_category__icontains="Cost of Sales")
                | all_accounts.filter(category=AccountCategory.EXPENSE, code__startswith="5")
            )
        elif cls_val == FinancialClassification.DEPRECIATION:
            return list(all_accounts.filter(name__icontains="depreciation"))
        elif cls_val == FinancialClassification.AMORTIZATION:
            return list(all_accounts.filter(name__icontains="amortization"))
        elif cls_val == FinancialClassification.INTEREST_EXPENSE:
            return list(all_accounts.filter(category=AccountCategory.EXPENSE).filter(
                name__icontains="interest"
            ) | all_accounts.filter(category=AccountCategory.EXPENSE, system_role="FEES"))
        elif cls_val == FinancialClassification.INTEREST_INCOME:
            return list(all_accounts.filter(category=AccountCategory.REVENUE, name__icontains="interest"))
        elif cls_val == FinancialClassification.TAX_EXPENSE:
            return list(all_accounts.filter(category=AccountCategory.EXPENSE, name__icontains="tax"))
        elif cls_val in (FinancialClassification.OPEX_GENERAL, FinancialClassification.OPEX_ADMIN):
            # OPEX = all expenses excluding COGS, Depreciation, Amortization, Interest, Tax
            excluded_accounts = set(
                self.get_accounts_for_classification(FinancialClassification.COGS)
                + self.get_accounts_for_classification(FinancialClassification.DEPRECIATION)
                + self.get_accounts_for_classification(FinancialClassification.AMORTIZATION)
                + self.get_accounts_for_classification(FinancialClassification.INTEREST_EXPENSE)
                + self.get_accounts_for_classification(FinancialClassification.TAX_EXPENSE)
            )
            all_expenses = all_accounts.filter(category=AccountCategory.EXPENSE)
            return [acc for acc in all_expenses if acc not in excluded_accounts]
        elif cls_val == FinancialClassification.CASH_EQUIVALENT:
            return list(all_accounts.filter(
                category=AccountCategory.ASSET,
                system_role__in=["CASH", "BANK", "CLEARING"],
            ) | all_accounts.filter(category=AccountCategory.ASSET, code__startswith="10"))
        elif cls_val == FinancialClassification.TRADE_RECEIVABLE:
            return list(all_accounts.filter(
                category=AccountCategory.ASSET,
                system_role="AR",
            ) | all_accounts.filter(category=AccountCategory.ASSET, code="1200"))
        elif cls_val == FinancialClassification.INVENTORY:
            return list(all_accounts.filter(
                category=AccountCategory.ASSET,
                system_role="INVENTORY",
            ) | all_accounts.filter(category=AccountCategory.ASSET, code="1400"))
        elif cls_val == FinancialClassification.OTHER_CURRENT_ASSET:
            return list(all_accounts.filter(
                category=AccountCategory.ASSET,
                code__range=("1300", "1499"),
            ).exclude(code="1400"))
        elif cls_val == FinancialClassification.PPE:
            return list(all_accounts.filter(
                category=AccountCategory.ASSET,
                code__range=("1500", "1999"),
            ))
        elif cls_val == FinancialClassification.TRADE_PAYABLE:
            return list(all_accounts.filter(
                category=AccountCategory.LIABILITY,
                system_role="AP",
            ) | all_accounts.filter(category=AccountCategory.LIABILITY, code="2000"))
        elif cls_val == FinancialClassification.CURRENT_LIABILITY:
            return list(all_accounts.filter(
                category=AccountCategory.LIABILITY,
                code__range=("2000", "2499"),
            ))
        elif cls_val == FinancialClassification.SHORT_TERM_DEBT:
            return list(all_accounts.filter(
                category=AccountCategory.LIABILITY,
                name__icontains="short-term debt",
            ) | all_accounts.filter(category=AccountCategory.LIABILITY, name__icontains="overdraft"))
        elif cls_val == FinancialClassification.LONG_TERM_DEBT:
            return list(all_accounts.filter(
                category=AccountCategory.LIABILITY,
                code__range=("2500", "2999"),
            ))
        elif cls_val == FinancialClassification.EQUITY:
            return list(all_accounts.filter(category=AccountCategory.EQUITY))

        return []

    def get_accounts_by_cost_behavior(self, cost_behavior: CostBehavior) -> list[Account]:
        """Returns expense accounts categorized by fixed vs variable cost behavior."""
        all_mapped_for_org = AccountClassificationMapping.objects.filter(
            organization=self.organization,
            account__is_active=True,
        ).select_related("account")

        explicit_for_this = [m.account for m in all_mapped_for_org if m.cost_behavior == cost_behavior]
        explicit_other_ids = {
            m.account_id for m in all_mapped_for_org
            if m.cost_behavior != cost_behavior and m.cost_behavior != CostBehavior.UNSPECIFIED
        }

        candidate_heuristics = []
        if cost_behavior == CostBehavior.VARIABLE:
            candidate_heuristics = self.get_accounts_for_classification(FinancialClassification.COGS)
        elif cost_behavior == CostBehavior.FIXED:
            candidate_heuristics = self.get_accounts_for_classification(FinancialClassification.OPEX_GENERAL)

        filtered_heuristics = [acc for acc in candidate_heuristics if acc.id not in explicit_other_ids]

        combined = {acc.id: acc for acc in (explicit_for_this + filtered_heuristics)}
        return list(combined.values())

    def calculate_balance_for_accounts(
        self,
        accounts: list[Account],
        start_date: date | None = None,
        end_date: date | None = None,
        as_of_date: date | None = None,
    ) -> Decimal:
        """Calculates net balance across accounts following their normal balance convention."""
        if not accounts:
            return Decimal("0.0000")

        lines_qs = JournalLine.objects.filter(
            journal_entry__organization=self.organization,
            journal_entry__status=JournalStatus.POSTED,
            account__in=accounts,
        )

        if as_of_date:
            lines_qs = lines_qs.filter(journal_entry__date__lte=as_of_date)
        else:
            if start_date:
                lines_qs = lines_qs.filter(journal_entry__date__gte=start_date)
            if end_date:
                lines_qs = lines_qs.filter(journal_entry__date__lte=end_date)

        total = Decimal("0.0000")
        for acc in accounts:
            acc_lines = lines_qs.filter(account=acc)
            agg = acc_lines.aggregate(dr=Sum("debit"), cr=Sum("credit"))
            dr = agg["dr"] or Decimal("0.0000")
            cr = agg["cr"] or Decimal("0.0000")

            if acc.category in (AccountCategory.ASSET, AccountCategory.EXPENSE):
                total += (dr - cr)
            else:
                total += (cr - dr)

        return total

    def calculate_average_balance(
        self,
        accounts: list[Account],
        start_date: date,
        end_date: date,
    ) -> Decimal:
        """Calculates average balance as (Opening Balance as of start_date + Closing Balance as of end_date) / 2."""
        opening = self.calculate_balance_for_accounts(accounts, as_of_date=start_date)
        closing = self.calculate_balance_for_accounts(accounts, as_of_date=end_date)
        return (opening + closing) / Decimal("2.0000")

    def get_completeness_report(self) -> dict:
        """Audits the organization's Chart of Accounts to check mapping coverage and completeness."""
        active_accounts = Account.objects.filter(organization=self.organization, is_active=True)
        mapped_account_ids = set(
            AccountClassificationMapping.objects.filter(
                organization=self.organization
            ).values_list("account_id", flat=True)
        )

        unmapped = []
        for acc in active_accounts:
            if acc.id not in mapped_account_ids:
                unmapped.append({
                    "id": str(acc.id),
                    "code": acc.code,
                    "name": acc.name,
                    "category": acc.category,
                    "system_role": acc.system_role,
                })

        # Check critical category coverage
        has_revenue = len(self.get_accounts_for_classification(FinancialClassification.REVENUE)) > 0
        has_cogs = len(self.get_accounts_for_classification(FinancialClassification.COGS)) > 0
        has_cash = len(self.get_accounts_for_classification(FinancialClassification.CASH_EQUIVALENT)) > 0
        has_ar = len(self.get_accounts_for_classification(FinancialClassification.TRADE_RECEIVABLE)) > 0
        has_ap = len(self.get_accounts_for_classification(FinancialClassification.TRADE_PAYABLE)) > 0

        is_complete = has_revenue and has_cash and has_ar and has_ap

        warnings = []
        if not has_revenue:
            warnings.append("No active Revenue accounts identified.")
        if not has_cash:
            warnings.append("No active Cash/Bank accounts identified.")
        if not has_cogs:
            warnings.append("No COGS accounts identified; gross profit equals net revenue.")

        return {
            "total_active_accounts": active_accounts.count(),
            "explicitly_mapped_count": len(mapped_account_ids),
            "unmapped_accounts_count": len(unmapped),
            "unmapped_accounts": unmapped,
            "is_complete": is_complete,
            "warnings": warnings,
        }

    # Alias for API compatibility
    validate_mapping_completeness = get_completeness_report

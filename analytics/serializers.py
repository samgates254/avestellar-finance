"""Serializers for analytics, financial metrics, and investment calculators."""

from decimal import Decimal
from rest_framework import serializers

from analytics.models import (
    AccountClassificationMapping,
    CapitalExpenditure,
    FinancialClassification,
    FinancialScenario,
)
from ledger.models import AccountCategory


class AccountClassificationMappingSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="account.code", read_only=True)
    account_name = serializers.CharField(source="account.name", read_only=True)

    class Meta:
        model = AccountClassificationMapping
        fields = [
            "id",
            "account",
            "account_code",
            "account_name",
            "classification",
            "cost_behavior",
            "notes",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        account = attrs.get("account")
        classification = attrs.get("classification")
        if account and classification:
            # Prevent illogical classification mappings (e.g. Asset account mapped to Revenue classification)
            if classification == FinancialClassification.REVENUE and account.category not in (AccountCategory.REVENUE, AccountCategory.EQUITY):
                raise serializers.ValidationError({"classification": "Revenue classification must apply to a revenue account category."})
            if classification in (
                FinancialClassification.OPEX_GENERAL,
                FinancialClassification.OPEX_ADMIN,
                FinancialClassification.OPEX_SALES_MARKETING,
            ) and account.category != AccountCategory.EXPENSE:
                raise serializers.ValidationError({"classification": "Operating expenses must apply to an expense account category."})
        return attrs


class CapitalExpenditureSerializer(serializers.ModelSerializer):
    class Meta:
        model = CapitalExpenditure
        fields = [
            "id",
            "project_code",
            "name",
            "asset_category",
            "status",
            "amount",
            "currency",
            "incurred_date",
            "account",
            "journal_entry",
            "description",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class NpvCalculatorRequestSerializer(serializers.Serializer):
    initial_investment = serializers.DecimalField(max_digits=20, decimal_places=4, min_value=Decimal("0.0001"))
    cash_flows = serializers.ListField(
        child=serializers.DecimalField(max_digits=20, decimal_places=4),
        allow_empty=False,
        help_text="Periodic net cash inflows starting at period 1."
    )
    discount_rate = serializers.DecimalField(
        max_digits=10, decimal_places=6, min_value=Decimal("-0.999999"),
        help_text="Discount rate as a decimal (e.g., 0.10 for 10%)."
    )


class IrrCalculatorRequestSerializer(serializers.Serializer):
    initial_investment = serializers.DecimalField(max_digits=20, decimal_places=4, min_value=Decimal("0.0001"))
    cash_flows = serializers.ListField(
        child=serializers.DecimalField(max_digits=20, decimal_places=4),
        allow_empty=False,
        help_text="Periodic net cash inflows starting at period 1."
    )


class InvestmentCalculatorRequestSerializer(serializers.Serializer):
    initial_investment = serializers.DecimalField(max_digits=20, decimal_places=4, min_value=Decimal("0.0001"))
    cash_flows = serializers.ListField(
        child=serializers.DecimalField(max_digits=20, decimal_places=4),
        allow_empty=False,
    )
    discount_rate = serializers.DecimalField(
        max_digits=10, decimal_places=6, min_value=Decimal("-0.999999"),
        help_text="Periodic discount rate as decimal (e.g. 0.10)"
    )
    hurdle_rate = serializers.DecimalField(
        max_digits=10, decimal_places=4, required=False, default=Decimal("0.0"),
        help_text="Hurdle rate percentage (e.g. 12.0 for 12%)"
    )


class BreakEvenCalculatorRequestSerializer(serializers.Serializer):
    fixed_costs = serializers.DecimalField(max_digits=20, decimal_places=4, min_value=Decimal("0"))
    unit_selling_price = serializers.DecimalField(max_digits=20, decimal_places=4, min_value=Decimal("0.0001"))
    unit_variable_cost = serializers.DecimalField(max_digits=20, decimal_places=4, min_value=Decimal("0"))
    actual_units = serializers.DecimalField(max_digits=20, decimal_places=4, required=False, allow_null=True)
    target_profit = serializers.DecimalField(max_digits=20, decimal_places=4, required=False, allow_null=True)


class MultiProductBreakEvenRequestSerializer(serializers.Serializer):
    fixed_costs = serializers.DecimalField(max_digits=20, decimal_places=4, min_value=Decimal("0"))
    products = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=False,
        help_text="List of products with name, price, variable_cost, and sales_mix_pct."
    )


class FinancialScenarioRequestSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    base_start_date = serializers.DateField()
    base_end_date = serializers.DateField()
    revenue_growth_pct = serializers.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    cogs_change_pct = serializers.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    opex_change_pct = serializers.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    planned_capex = serializers.DecimalField(max_digits=20, decimal_places=4, default=Decimal("0"))
    working_capital_buffer_pct = serializers.DecimalField(max_digits=10, decimal_places=4, default=Decimal("0"))
    save_scenario = serializers.BooleanField(default=False)


class FinancialScenarioSerializer(serializers.ModelSerializer):
    class Meta:
        model = FinancialScenario
        fields = [
            "id",
            "name",
            "description",
            "parameters",
            "projections",
            "created_at",
        ]
        read_only_fields = ["id", "created_at"]

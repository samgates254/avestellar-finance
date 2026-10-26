from decimal import Decimal
from rest_framework import serializers
from ledger.models import (
    Account,
    AccountCategory,
    AccountingPeriod,
    JournalEntry,
    JournalLine,
    JournalStatus,
    PeriodStatus,
)


class AccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = Account
        fields = (
            "id",
            "code",
            "name",
            "category",
            "sub_category",
            "description",
            "is_active",
            "is_system_account",
            "system_role",
            "currency",
            "normal_balance",
            "created_at",
        )
        read_only_fields = ("id", "normal_balance", "created_at")


class AccountingPeriodSerializer(serializers.ModelSerializer):
    class Meta:
        model = AccountingPeriod
        fields = (
            "id",
            "name",
            "fiscal_year",
            "period_number",
            "start_date",
            "end_date",
            "status",
            "closed_at",
            "closed_by",
            "created_at",
        )
        read_only_fields = ("id", "status", "closed_at", "closed_by", "created_at")


class JournalLineSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="account.code", read_only=True)
    account_name = serializers.CharField(source="account.name", read_only=True)
    account_id = serializers.PrimaryKeyRelatedField(
        queryset=Account.objects.all(), source="account", write_only=True
    )

    class Meta:
        model = JournalLine
        fields = (
            "id",
            "account_id",
            "account_code",
            "account_name",
            "description",
            "debit",
            "credit",
            "sequence",
        )
        read_only_fields = ("id",)


class JournalEntryListSerializer(serializers.ModelSerializer):
    class Meta:
        model = JournalEntry
        fields = (
            "id",
            "entry_number",
            "date",
            "description",
            "reference",
            "status",
            "currency",
            "total_debit",
            "total_credit",
            "posted_at",
            "created_at",
        )
        read_only_fields = fields


class JournalEntryDetailSerializer(serializers.ModelSerializer):
    lines = JournalLineSerializer(many=True)
    period_id = serializers.PrimaryKeyRelatedField(
        queryset=AccountingPeriod.objects.all(), source="period", write_only=True
    )
    period_name = serializers.CharField(source="period.name", read_only=True)

    class Meta:
        model = JournalEntry
        fields = (
            "id",
            "entry_number",
            "period_id",
            "period_name",
            "date",
            "description",
            "reference",
            "status",
            "currency",
            "total_debit",
            "total_credit",
            "posted_at",
            "posted_by",
            "lines",
            "reversed_entry",
            "is_auto_reversal",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "status",
            "total_debit",
            "total_credit",
            "posted_at",
            "posted_by",
            "reversed_entry",
            "is_auto_reversal",
            "created_at",
            "updated_at",
        )

    def validate_lines(self, value):
        if len(value) < 2:
            raise serializers.ValidationError("A journal entry must contain at least two lines.")
        total_dr = Decimal("0.0000")
        total_cr = Decimal("0.0000")
        for line in value:
            dr = line.get("debit", Decimal("0.0000"))
            cr = line.get("credit", Decimal("0.0000"))
            if dr < 0 or cr < 0:
                raise serializers.ValidationError("Line amounts must be non-negative.")
            if dr > 0 and cr > 0:
                raise serializers.ValidationError("A line cannot have both Debit and Credit amounts.")
            if dr == 0 and cr == 0:
                raise serializers.ValidationError("A line must specify a Debit or Credit amount.")
            total_dr += dr
            total_cr += cr
        if total_dr != total_cr:
            raise serializers.ValidationError(
                f"Debit total ({total_dr}) must equal credit total ({total_cr})."
            )
        return value

    def create(self, validated_data):
        lines_data = validated_data.pop("lines")
        entry = JournalEntry.objects.create(**validated_data)
        for idx, line_data in enumerate(lines_data, start=1):
            JournalLine.objects.create(
                journal_entry=entry,
                sequence=line_data.get("sequence", idx),
                **line_data,
            )
        return entry


class ReverseJournalSerializer(serializers.Serializer):
    reversal_date = serializers.DateField(required=False)
    reason = serializers.CharField(required=False, default="Correction of posted entry")

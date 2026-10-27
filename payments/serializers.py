from decimal import Decimal
from rest_framework import serializers
from ledger.models import Account
from payments.models import Payment, PaymentMethod, PaymentStatus, PaymentType


class PaymentSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    deposit_account_code = serializers.CharField(source="deposit_account.code", read_only=True)
    deposit_account_name = serializers.CharField(source="deposit_account.name", read_only=True)

    class Meta:
        model = Payment
        fields = (
            "id",
            "payment_number",
            "payment_type",
            "customer",
            "customer_name",
            "supplier",
            "invoice",
            "bill",
            "amount",
            "currency",
            "payment_date",
            "payment_method",
            "status",
            "reference",
            "deposit_account",
            "deposit_account_code",
            "deposit_account_name",
            "journal_entry",
            "is_reconciled",
            "created_at",
        )
        read_only_fields = (
            "id",
            "payment_number",
            "customer_name",
            "deposit_account_code",
            "deposit_account_name",
            "journal_entry",
            "created_at",
        )


class RecordPaymentRequestSerializer(serializers.Serializer):
    invoice_id = serializers.UUIDField(required=True)
    amount = serializers.DecimalField(max_digits=18, decimal_places=4, min_value=Decimal("0.01"))
    deposit_account_id = serializers.UUIDField(required=True)
    payment_method = serializers.ChoiceField(
        choices=PaymentMethod.choices, default=PaymentMethod.BANK_TRANSFER
    )
    payment_number = serializers.CharField(required=False, allow_blank=True)
    reference = serializers.CharField(required=False, allow_blank=True, default="")
    idempotency_key = serializers.CharField(required=False, allow_blank=True, default="")


class StatementTransactionSerializer(serializers.ModelSerializer):
    account_code = serializers.CharField(source="financial_account.code", read_only=True)
    account_name = serializers.CharField(source="financial_account.name", read_only=True)

    class Meta:
        from payments.models import StatementTransaction
        model = StatementTransaction
        fields = (
            "id",
            "batch",
            "financial_account",
            "account_code",
            "account_name",
            "source_transaction_id",
            "transaction_date",
            "value_date",
            "description",
            "reference",
            "debit_amount",
            "credit_amount",
            "signed_amount",
            "currency",
            "running_balance",
            "classification",
            "reconciliation_status",
            "matched_payment",
            "matched_invoice",
            "matched_journal_entry",
            "duplicate_fingerprint",
            "raw_metadata",
            "reconciled_at",
            "created_at",
        )
        read_only_fields = fields


class StatementImportBatchSerializer(serializers.ModelSerializer):
    financial_account_code = serializers.CharField(source="financial_account.code", read_only=True)
    financial_account_name = serializers.CharField(source="financial_account.name", read_only=True)
    imported_by_email = serializers.CharField(source="imported_by.email", read_only=True)
    transactions = StatementTransactionSerializer(many=True, read_only=True)

    class Meta:
        from payments.models import StatementImportBatch
        model = StatementImportBatch
        fields = (
            "id",
            "financial_account",
            "financial_account_code",
            "financial_account_name",
            "import_source",
            "file_name",
            "file_hash",
            "parser_version",
            "imported_by",
            "imported_by_email",
            "opening_balance",
            "closing_balance",
            "total_rows",
            "imported_count",
            "rejected_count",
            "duplicate_count",
            "status",
            "validation_summary",
            "error_details",
            "transactions",
            "created_at",
        )
        read_only_fields = fields


class StatementImportUploadSerializer(serializers.Serializer):
    financial_account_id = serializers.UUIDField(required=True)
    import_source = serializers.ChoiceField(
        choices=["BANK_CSV", "MPESA_CSV", "OFX", "MT940"],
        default="BANK_CSV",
    )
    file = serializers.FileField(required=False)
    file_content = serializers.CharField(required=False, allow_blank=True)
    file_name = serializers.CharField(required=False, default="statement.csv")
    column_mapping = serializers.DictField(required=False, default=dict)
    date_format = serializers.CharField(required=False, allow_blank=True)


class ReconciliationSessionSerializer(serializers.ModelSerializer):
    financial_account_code = serializers.CharField(source="financial_account.code", read_only=True)
    financial_account_name = serializers.CharField(source="financial_account.name", read_only=True)
    reconciled_by_email = serializers.CharField(source="reconciled_by.email", read_only=True)
    finalized_by_email = serializers.CharField(source="finalized_by.email", read_only=True)

    class Meta:
        from payments.models import ReconciliationSession
        model = ReconciliationSession
        fields = (
            "id",
            "session_number",
            "financial_account",
            "financial_account_code",
            "financial_account_name",
            "period_start",
            "period_end",
            "statement_opening_balance",
            "statement_closing_balance",
            "ledger_balance",
            "reconciled_balance",
            "difference",
            "status",
            "notes",
            "reconciled_by",
            "reconciled_by_email",
            "finalized_at",
            "finalized_by",
            "finalized_by_email",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "session_number",
            "ledger_balance",
            "reconciled_balance",
            "difference",
            "finalized_at",
            "finalized_by",
            "created_at",
            "updated_at",
        )


class CreateReconciliationSessionSerializer(serializers.Serializer):
    financial_account_id = serializers.UUIDField(required=True)
    period_start = serializers.DateField(required=True)
    period_end = serializers.DateField(required=True)
    statement_opening_balance = serializers.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    statement_closing_balance = serializers.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    session_number = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class MatchCandidateSerializer(serializers.Serializer):
    candidate_type = serializers.CharField()
    candidate_id = serializers.CharField()
    rule_applied = serializers.CharField()
    confidence_score = serializers.IntegerField()
    confidence_category = serializers.CharField()
    reason = serializers.CharField()
    auto_match_permitted = serializers.BooleanField()
    evidence_fields = serializers.DictField()
    candidate_summary = serializers.DictField()


class ConfirmMatchRequestSerializer(serializers.Serializer):
    statement_transaction_id = serializers.UUIDField(required=True)
    candidate_type = serializers.ChoiceField(choices=["PAYMENT", "INVOICE"], required=True)
    candidate_id = serializers.UUIDField(required=True)
    session_id = serializers.UUIDField(required=False, allow_null=True)
    reason = serializers.CharField(required=False, allow_blank=True, default="")
    rule_applied = serializers.CharField(required=False, default="MANUAL_DECISION")
    confidence_score = serializers.IntegerField(required=False, default=100)
    confidence_category = serializers.ChoiceField(
        choices=["HIGH", "MEDIUM", "LOW", "MANUAL"], default="MANUAL"
    )


class InvestigateRequestSerializer(serializers.Serializer):
    statement_transaction_id = serializers.UUIDField(required=True)
    reason = serializers.CharField(required=True)


class ReopenSessionRequestSerializer(serializers.Serializer):
    reason = serializers.CharField(required=True)

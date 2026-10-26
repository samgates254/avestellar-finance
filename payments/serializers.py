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

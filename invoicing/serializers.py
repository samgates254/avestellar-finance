from decimal import Decimal
from rest_framework import serializers
from invoicing.models import Customer, Invoice, InvoiceLineItem
from ledger.models import Account


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = (
            "id",
            "name",
            "email",
            "phone",
            "tax_pin",
            "address",
            "currency",
            "is_active",
            "created_at",
        )
        read_only_fields = ("id", "created_at")


class InvoiceLineItemSerializer(serializers.ModelSerializer):
    revenue_account_id = serializers.PrimaryKeyRelatedField(
        queryset=Account.objects.all(), source="revenue_account", write_only=True
    )
    revenue_account_code = serializers.CharField(source="revenue_account.code", read_only=True)

    class Meta:
        model = InvoiceLineItem
        fields = (
            "id",
            "revenue_account_id",
            "revenue_account_code",
            "description",
            "quantity",
            "unit_price",
            "tax_rate",
            "tax_amount",
            "line_total",
        )
        read_only_fields = ("id", "tax_amount", "line_total")


class InvoiceSerializer(serializers.ModelSerializer):
    customer_id = serializers.PrimaryKeyRelatedField(
        queryset=Customer.objects.all(), source="customer", write_only=True
    )
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    items = InvoiceLineItemSerializer(many=True, required=False)

    class Meta:
        model = Invoice
        fields = (
            "id",
            "invoice_number",
            "customer_id",
            "customer_name",
            "issue_date",
            "due_date",
            "status",
            "currency",
            "subtotal",
            "tax_amount",
            "total_amount",
            "paid_amount",
            "outstanding_balance",
            "journal_entry",
            "notes",
            "items",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "status",
            "subtotal",
            "tax_amount",
            "total_amount",
            "paid_amount",
            "outstanding_balance",
            "journal_entry",
            "created_at",
            "updated_at",
        )

    def create(self, validated_data):
        items_data = validated_data.pop("items", [])
        invoice = Invoice.objects.create(**validated_data)
        subtotal = Decimal("0.0000")
        tax_total = Decimal("0.0000")
        for item in items_data:
            line = InvoiceLineItem.objects.create(invoice=invoice, **item)
            subtotal += line.quantity * line.unit_price
            tax_total += line.tax_amount
        invoice.subtotal = subtotal
        invoice.tax_amount = tax_total
        invoice.total_amount = subtotal + tax_total
        invoice.outstanding_balance = invoice.total_amount
        invoice.save(update_fields=["subtotal", "tax_amount", "total_amount", "outstanding_balance"])
        return invoice

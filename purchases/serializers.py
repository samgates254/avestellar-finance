from rest_framework import serializers
from purchases.models import Supplier, SupplierBill


class SupplierSerializer(serializers.ModelSerializer):
    class Meta:
        model = Supplier
        fields = (
            "id",
            "name",
            "email",
            "phone",
            "tax_pin",
            "currency",
            "is_active",
            "created_at",
        )
        read_only_fields = ("id", "created_at")


class SupplierBillSerializer(serializers.ModelSerializer):
    supplier_id = serializers.PrimaryKeyRelatedField(
        queryset=Supplier.objects.all(), source="supplier", write_only=True
    )
    supplier_name = serializers.CharField(source="supplier.name", read_only=True)

    class Meta:
        model = SupplierBill
        fields = (
            "id",
            "bill_number",
            "supplier_id",
            "supplier_name",
            "bill_date",
            "due_date",
            "status",
            "currency",
            "total_amount",
            "paid_amount",
            "outstanding_balance",
            "journal_entry",
            "notes",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "status",
            "paid_amount",
            "outstanding_balance",
            "journal_entry",
            "created_at",
            "updated_at",
        )

    def create(self, validated_data):
        bill = SupplierBill.objects.create(**validated_data)
        bill.outstanding_balance = bill.total_amount
        bill.save(update_fields=["outstanding_balance"])
        return bill

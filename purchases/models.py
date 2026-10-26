import uuid
from decimal import Decimal
from django.db import models


class BillStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    RECEIVED = "RECEIVED", "Received"
    PARTIALLY_PAID = "PARTIALLY_PAID", "Partially Paid"
    PAID = "PAID", "Paid"
    CANCELLED = "CANCELLED", "Cancelled"


class Supplier(models.Model):
    """Vendor / Supplier entity for accounts payable."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="suppliers",
    )
    name = models.CharField(max_length=255, db_index=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    tax_pin = models.CharField(max_length=50, blank=True)
    currency = models.CharField(max_length=3, default="KES")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "purchases_suppliers"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.organization.name})"


class SupplierBill(models.Model):
    """Supplier bill / accounts payable invoice."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="supplier_bills",
    )
    supplier = models.ForeignKey(
        Supplier, on_delete=models.PROTECT, related_name="bills"
    )
    bill_number = models.CharField(max_length=100, db_index=True)
    bill_date = models.DateField(db_index=True)
    due_date = models.DateField(db_index=True)
    status = models.CharField(
        max_length=20,
        choices=BillStatus.choices,
        default=BillStatus.DRAFT,
        db_index=True,
    )
    currency = models.CharField(max_length=3, default="KES")
    total_amount = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    paid_amount = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    outstanding_balance = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    journal_entry = models.ForeignKey(
        "ledger.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="supplier_bills",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "purchases_supplier_bills"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "bill_number"],
                name="unique_org_bill_number",
            )
        ]
        ordering = ["-bill_date", "-created_at"]

    def __str__(self) -> str:
        return f"{self.bill_number} - {self.supplier.name} [{self.status}: {self.total_amount}]"

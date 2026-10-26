import uuid
from decimal import Decimal
from django.db import models


class InvoiceStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ISSUED = "ISSUED", "Issued"
    PARTIALLY_PAID = "PARTIALLY_PAID", "Partially Paid"
    PAID = "PAID", "Paid"
    CANCELLED = "CANCELLED", "Cancelled"


class Customer(models.Model):
    """Customer entity associated with sales invoices and receivables."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="customers",
    )
    name = models.CharField(max_length=255, db_index=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=50, blank=True)
    tax_pin = models.CharField(max_length=50, blank=True)
    address = models.TextField(blank=True)
    currency = models.CharField(max_length=3, default="KES")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "invoicing_customers"
        ordering = ["name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.organization.name})"


class Invoice(models.Model):
    """Sales invoice representing accounts receivable claims against customers."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="invoices",
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="invoices"
    )
    invoice_number = models.CharField(max_length=100, db_index=True)
    issue_date = models.DateField(db_index=True)
    due_date = models.DateField(db_index=True)
    status = models.CharField(
        max_length=20,
        choices=InvoiceStatus.choices,
        default=InvoiceStatus.DRAFT,
        db_index=True,
    )
    currency = models.CharField(max_length=3, default="KES")
    subtotal = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    tax_amount = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
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
        related_name="invoices",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "invoicing_invoices"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "invoice_number"],
                name="unique_org_invoice_number",
            )
        ]
        ordering = ["-issue_date", "-created_at"]

    def __str__(self) -> str:
        return f"{self.invoice_number} - {self.customer.name} [{self.status}: {self.total_amount}]"


class InvoiceLineItem(models.Model):
    """Line item on a sales invoice detailing product/service, rate, and tax."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    invoice = models.ForeignKey(
        Invoice, on_delete=models.CASCADE, related_name="items"
    )
    revenue_account = models.ForeignKey(
        "ledger.Account", on_delete=models.PROTECT, related_name="invoice_lines"
    )
    description = models.CharField(max_length=255)
    quantity = models.DecimalField(
        max_digits=12, decimal_places=4, default=Decimal("1.0000")
    )
    unit_price = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    tax_rate = models.DecimalField(
        max_digits=6, decimal_places=4, default=Decimal("0.0000")
    )  # e.g. 0.1600 for 16% VAT
    tax_amount = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    line_total = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )

    class Meta:
        db_table = "invoicing_line_items"
        ordering = ["id"]

    def save(self, *args, **kwargs):
        sub = self.quantity * self.unit_price
        self.tax_amount = sub * self.tax_rate
        self.line_total = sub + self.tax_amount
        super().save(*args, **kwargs)

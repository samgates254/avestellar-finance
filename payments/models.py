import uuid
from decimal import Decimal
from django.db import models


class PaymentType(models.TextChoices):
    CUSTOMER_PAYMENT = "CUSTOMER_PAYMENT", "Customer Payment (Inflow)"
    SUPPLIER_PAYMENT = "SUPPLIER_PAYMENT", "Supplier Payment (Outflow)"


class PaymentMethod(models.TextChoices):
    BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer"
    MPESA = "MPESA", "M-Pesa"
    CASH = "CASH", "Cash"
    CHEQUE = "CHEQUE", "Cheque"
    CARD = "CARD", "Debit / Credit Card"


class PaymentStatus(models.TextChoices):
    INITIATED = "INITIATED", "Initiated"
    PENDING = "PENDING", "Pending"
    CONFIRMED = "CONFIRMED", "Confirmed"
    FAILED = "FAILED", "Failed"
    RECONCILED = "RECONCILED", "Reconciled"


class Payment(models.Model):
    """Financial payment and receipt settlement record."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="payments",
    )
    payment_number = models.CharField(max_length=100, db_index=True)
    payment_type = models.CharField(
        max_length=30, choices=PaymentType.choices, default=PaymentType.CUSTOMER_PAYMENT
    )
    customer = models.ForeignKey(
        "invoicing.Customer",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payments",
    )
    supplier = models.ForeignKey(
        "purchases.Supplier",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payments",
    )
    invoice = models.ForeignKey(
        "invoicing.Invoice",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payments",
    )
    bill = models.ForeignKey(
        "purchases.SupplierBill",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payments",
    )
    amount = models.DecimalField(max_digits=18, decimal_places=4)
    currency = models.CharField(max_length=3, default="KES")
    payment_date = models.DateField(db_index=True)
    payment_method = models.CharField(
        max_length=30, choices=PaymentMethod.choices, default=PaymentMethod.BANK_TRANSFER
    )
    status = models.CharField(
        max_length=20,
        choices=PaymentStatus.choices,
        default=PaymentStatus.INITIATED,
        db_index=True,
    )
    reference = models.CharField(max_length=255, blank=True, db_index=True)  # e.g. M-Pesa receipt code
    deposit_account = models.ForeignKey(
        "ledger.Account", on_delete=models.PROTECT, related_name="payments"
    )
    journal_entry = models.ForeignKey(
        "ledger.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payments",
    )
    is_reconciled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payments"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "payment_number"],
                name="unique_org_payment_number",
            )
        ]
        ordering = ["-payment_date", "-created_at"]

    def __str__(self) -> str:
        return f"{self.payment_number} [{self.payment_type}: {self.amount} {self.currency}] ({self.status})"

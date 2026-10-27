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


class StatementImportSource(models.TextChoices):
    BANK_CSV = "BANK_CSV", "Bank Statement CSV"
    MPESA_CSV = "MPESA_CSV", "Safaricom M-Pesa CSV"
    OFX = "OFX", "Open Financial Exchange (OFX)"
    MT940 = "MT940", "SWIFT MT940"


class StatementImportStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PROCESSING = "PROCESSING", "Processing"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"
    PARTIAL = "PARTIAL", "Partially Completed"


class StatementTransactionClassification(models.TextChoices):
    CUSTOMER_PAYMENT = "CUSTOMER_PAYMENT", "Customer Payment / Collection"
    SUPPLIER_PAYMENT = "SUPPLIER_PAYMENT", "Supplier Payment / Disbursement"
    BANK_FEE = "BANK_FEE", "Bank Fee / Charge"
    INTEREST = "INTEREST", "Interest Receipt / Charge"
    TRANSFER = "TRANSFER", "Account Transfer"
    REFUND = "REFUND", "Refund"
    REVERSAL = "REVERSAL", "Reversal"
    UNCLASSIFIED = "UNCLASSIFIED", "Unclassified"


class StatementReconciliationStatus(models.TextChoices):
    UNRECONCILED = "UNRECONCILED", "Unreconciled"
    MATCH_PROPOSED = "MATCH_PROPOSED", "Match Proposed"
    RECONCILED = "RECONCILED", "Reconciled"
    INVESTIGATION = "INVESTIGATION", "Requires Investigation"
    IGNORED = "IGNORED", "Ignored"


class ReconciliationSessionStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    FINALIZED = "FINALIZED", "Finalized"
    REOPENED = "REOPENED", "Reopened"


class ReconciliationDecisionType(models.TextChoices):
    AUTO_MATCH = "AUTO_MATCH", "Automated Match"
    MANUAL_MATCH = "MANUAL_MATCH", "Manual Match"
    REJECT_MATCH = "REJECT_MATCH", "Reject Match"
    INVESTIGATE = "INVESTIGATE", "Mark for Investigation"
    REOPEN = "REOPEN", "Reopen Session"
    UNMATCH = "UNMATCH", "Unmatch Transaction"


class ReconciliationConfidence(models.TextChoices):
    HIGH = "HIGH", "High Confidence"
    MEDIUM = "MEDIUM", "Medium Confidence"
    LOW = "LOW", "Low Confidence"
    MANUAL = "MANUAL", "Manual Decision"


class StatementImportBatch(models.Model):
    """Batch container for imported bank or M-Pesa statement files."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="statement_batches",
    )
    financial_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="statement_batches",
    )
    import_source = models.CharField(
        max_length=20,
        choices=StatementImportSource.choices,
        default=StatementImportSource.BANK_CSV,
    )
    file_name = models.CharField(max_length=255)
    file_hash = models.CharField(max_length=64, db_index=True)
    parser_version = models.CharField(max_length=30, default="1.0.0")
    imported_by = models.ForeignKey(
        "identity.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="statement_imports",
    )
    opening_balance = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    closing_balance = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    total_rows = models.PositiveIntegerField(default=0)
    imported_count = models.PositiveIntegerField(default=0)
    rejected_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=20,
        choices=StatementImportStatus.choices,
        default=StatementImportStatus.PENDING,
    )
    validation_summary = models.JSONField(default=dict)
    error_details = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "statement_import_batches"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "financial_account", "file_hash"],
                name="unique_org_account_statement_file",
            )
        ]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.file_name} [{self.import_source}] ({self.status}) - {self.imported_count} rows"


class StatementTransaction(models.Model):
    """Normalized statement line item evidence for financial reconciliation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="statement_transactions",
    )
    batch = models.ForeignKey(
        StatementImportBatch,
        on_delete=models.CASCADE,
        related_name="transactions",
    )
    financial_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="statement_transactions",
    )
    source_transaction_id = models.CharField(
        max_length=255, blank=True, db_index=True
    )
    transaction_date = models.DateField(db_index=True)
    value_date = models.DateField(null=True, blank=True)
    description = models.TextField()
    reference = models.CharField(max_length=255, blank=True, db_index=True)
    debit_amount = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    credit_amount = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    signed_amount = models.DecimalField(
        max_digits=18, decimal_places=4, db_index=True
    )
    currency = models.CharField(max_length=3, default="KES")
    running_balance = models.DecimalField(
        max_digits=18, decimal_places=4, null=True, blank=True
    )
    classification = models.CharField(
        max_length=30,
        choices=StatementTransactionClassification.choices,
        default=StatementTransactionClassification.UNCLASSIFIED,
    )
    reconciliation_status = models.CharField(
        max_length=30,
        choices=StatementReconciliationStatus.choices,
        default=StatementReconciliationStatus.UNRECONCILED,
        db_index=True,
    )
    matched_payment = models.ForeignKey(
        Payment,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="matched_statement_transactions",
    )
    matched_journal_entry = models.ForeignKey(
        "ledger.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="matched_statement_transactions",
    )
    matched_invoice = models.ForeignKey(
        "invoicing.Invoice",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="matched_statement_transactions",
    )
    duplicate_fingerprint = models.CharField(max_length=64, db_index=True)
    raw_metadata = models.JSONField(default=dict)
    reconciled_at = models.DateTimeField(null=True, blank=True)
    reconciled_by = models.ForeignKey(
        "identity.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reconciled_transactions",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "statement_transactions"
        ordering = ["-transaction_date", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "financial_account", "source_transaction_id"],
                condition=models.Q(source_transaction_id__gt=""),
                name="unique_org_account_source_trx_id",
            )
        ]

    def __str__(self) -> str:
        return f"{self.transaction_date} | {self.reference or self.source_transaction_id} | {self.signed_amount} {self.currency} [{self.reconciliation_status}]"


class ReconciliationSession(models.Model):
    """Reconciliation session tracking account period statement vs ledger balance."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="reconciliation_sessions",
    )
    financial_account = models.ForeignKey(
        "ledger.Account",
        on_delete=models.PROTECT,
        related_name="reconciliation_sessions",
    )
    session_number = models.CharField(max_length=100, db_index=True)
    period_start = models.DateField()
    period_end = models.DateField()
    statement_opening_balance = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    statement_closing_balance = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    ledger_balance = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    reconciled_balance = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    difference = models.DecimalField(
        max_digits=18, decimal_places=4, default=Decimal("0.0000")
    )
    status = models.CharField(
        max_length=20,
        choices=ReconciliationSessionStatus.choices,
        default=ReconciliationSessionStatus.DRAFT,
    )
    notes = models.TextField(blank=True)
    reconciled_by = models.ForeignKey(
        "identity.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reconciliation_sessions_conducted",
    )
    finalized_at = models.DateTimeField(null=True, blank=True)
    finalized_by = models.ForeignKey(
        "identity.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reconciliation_sessions_finalized",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "reconciliation_sessions"
        ordering = ["-period_end", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "session_number"],
                name="unique_org_reconciliation_session_num",
            )
        ]

    def __str__(self) -> str:
        return f"{self.session_number} ({self.financial_account.name}: {self.period_start} to {self.period_end}) [{self.status}]"


class ReconciliationMatchDecision(models.Model):
    """Audit log of individual transaction matching decisions and accountant reviews."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="reconciliation_decisions",
    )
    session = models.ForeignKey(
        ReconciliationSession,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="decisions",
    )
    statement_transaction = models.ForeignKey(
        StatementTransaction,
        on_delete=models.CASCADE,
        related_name="decisions",
    )
    matched_payment = models.ForeignKey(
        Payment,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reconciliation_decisions",
    )
    matched_invoice = models.ForeignKey(
        "invoicing.Invoice",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reconciliation_decisions",
    )
    matched_journal_entry = models.ForeignKey(
        "ledger.JournalEntry",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reconciliation_decisions",
    )
    user = models.ForeignKey(
        "identity.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reconciliation_decisions_made",
    )
    decision = models.CharField(
        max_length=30,
        choices=ReconciliationDecisionType.choices,
    )
    rule_applied = models.CharField(max_length=100)
    confidence_score = models.PositiveIntegerField(default=100)
    confidence_category = models.CharField(
        max_length=20,
        choices=ReconciliationConfidence.choices,
        default=ReconciliationConfidence.MANUAL,
    )
    reason = models.TextField(blank=True)
    previous_status = models.CharField(max_length=30)
    new_status = models.CharField(max_length=30)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "reconciliation_match_decisions"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.decision} on Trx {self.statement_transaction_id} ({self.rule_applied})"

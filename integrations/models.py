import uuid
from django.db import models


class MauzoPilotConfig(models.Model):
    """Configuration and credentials for operational sync with MauzoPilot."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="mauzopilot_config",
    )
    base_url = models.URLField(default="https://api.mauzopilot.com/v1")
    business_id = models.CharField(max_length=64, blank=True)
    api_key = models.CharField(max_length=255, blank=True)
    webhook_secret = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=False)
    sync_customers = models.BooleanField(default=True)
    sync_sales = models.BooleanField(default=True)
    sync_payments = models.BooleanField(default=True)
    auto_settle_cash = models.BooleanField(
        default=False,
        help_text="Allow automatic settlement of verified POS cash collections under organization policy.",
    )
    request_timeout = models.PositiveIntegerField(default=10)
    customer_sync_cursor = models.DateTimeField(null=True, blank=True)
    sale_sync_cursor = models.DateTimeField(null=True, blank=True)
    sale_after_id = models.CharField(max_length=64, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_status = models.CharField(max_length=30, blank=True, default="IDLE")
    last_sync_error = models.TextField(blank=True, default="")
    consecutive_failure_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_mauzopilot_config"

    def __str__(self) -> str:
        return f"MauzoPilot Config [{self.organization.name}] (business: {self.business_id or 'none'}, active: {self.is_active})"


class ExternalObjectMapping(models.Model):
    """Canonical mapping table preventing duplicate ingestion across ecosystem boundaries."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="external_mappings",
    )
    external_system = models.CharField(max_length=50, default="mauzopilot", db_index=True)
    external_type = models.CharField(max_length=50, db_index=True)  # 'customer', 'sales_order', 'payment'
    external_id = models.CharField(max_length=255, db_index=True)
    internal_type = models.CharField(max_length=50, db_index=True)  # 'customer', 'invoice', 'payment'
    internal_id = models.UUIDField(db_index=True)
    raw_metadata = models.JSONField(default=dict)
    synced_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_external_mappings"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "external_system", "external_type", "external_id"],
                name="unique_org_external_object_mapping",
            )
        ]
        ordering = ["-synced_at"]

    def __str__(self) -> str:
        return f"{self.external_system}:{self.external_type}:{self.external_id} -> {self.internal_type}:{self.internal_id}"


class IntegrationSyncLog(models.Model):
    """Audit log tracking integration sync transactions and error payloads."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="sync_logs",
    )
    external_system = models.CharField(max_length=50, db_index=True)
    action = models.CharField(max_length=100)
    status = models.CharField(max_length=20, db_index=True)  # 'SUCCESS', 'FAILED', 'PARTIAL'
    details = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "integrations_sync_logs"
        ordering = ["-created_at"]


class DarajaConfig(models.Model):
    """Configuration and credentials for Safaricom Daraja M-Pesa integration."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.OneToOneField(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="daraja_config",
    )
    short_code = models.CharField(max_length=20, blank=True)
    consumer_key = models.CharField(max_length=255, blank=True)
    consumer_secret = models.CharField(max_length=255, blank=True)
    passkey = models.CharField(max_length=255, blank=True)
    initiator_name = models.CharField(max_length=100, blank=True)
    is_sandbox = models.BooleanField(default=True)
    is_active = models.BooleanField(default=False)
    default_deposit_account = models.ForeignKey(
        "ledger.Account",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="daraja_configs",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_daraja_config"

    def __str__(self) -> str:
        return f"Daraja Config [{self.organization.name}] (shortcode: {self.short_code}, active: {self.is_active})"


class DarajaCallbackType(models.TextChoices):
    C2B_VALIDATION = "C2B_VALIDATION", "C2B Validation"
    C2B_CONFIRMATION = "C2B_CONFIRMATION", "C2B Confirmation"
    STK_PUSH = "STK_PUSH", "STK Push / Lipa Na M-Pesa Online"
    TRANSACTION_STATUS = "TRANSACTION_STATUS", "Transaction Status Query"


class DarajaCallbackStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PROCESSED = "PROCESSED", "Processed"
    DUPLICATE = "DUPLICATE", "Duplicate Ignored"
    FAILED = "FAILED", "Failed"


class DarajaCallbackEvent(models.Model):
    """Audit log and replay protection store for Safaricom Daraja callback payloads."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="daraja_callback_events",
    )
    callback_type = models.CharField(
        max_length=30,
        choices=DarajaCallbackType.choices,
        db_index=True,
    )
    transaction_id = models.CharField(max_length=100, blank=True, db_index=True)  # M-Pesa TransID / Receipt
    status = models.CharField(
        max_length=20,
        choices=DarajaCallbackStatus.choices,
        default=DarajaCallbackStatus.PENDING,
        db_index=True,
    )
    payload = models.JSONField(default=dict)
    matched_payment = models.ForeignKey(
        "payments.Payment",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="daraja_callbacks",
    )
    processed_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "integrations_daraja_callback_events"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "callback_type", "transaction_id"],
                condition=models.Q(transaction_id__gt=""),
                name="unique_org_daraja_callback_trx",
            )
        ]

    def __str__(self) -> str:
        return f"{self.callback_type} [{self.transaction_id}] - {self.status}"

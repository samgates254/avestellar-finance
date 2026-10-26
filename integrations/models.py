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
    api_key = models.CharField(max_length=255, blank=True)
    webhook_secret = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=False)
    sync_customers = models.BooleanField(default=True)
    sync_sales = models.BooleanField(default=True)
    sync_payments = models.BooleanField(default=True)
    request_timeout = models.PositiveIntegerField(default=10)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "integrations_mauzopilot_config"

    def __str__(self) -> str:
        return f"MauzoPilot Config [{self.organization.name}] (active: {self.is_active})"


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

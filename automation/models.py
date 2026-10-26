import uuid
from django.db import models
from django.utils import timezone


class OutboxStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    PROCESSED = "PROCESSED", "Processed"
    FAILED = "FAILED", "Failed"


class DeliveryStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"


class OutboxEvent(models.Model):
    """Transactional outbox event committed atomically with financial changes."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_id = models.CharField(max_length=100, unique=True, default=uuid.uuid4, db_index=True)
    event_type = models.CharField(max_length=100, db_index=True)
    schema_version = models.CharField(max_length=20, default="v1")
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="outbox_events",
    )
    source_system = models.CharField(max_length=100, default="avestellar.finance")
    correlation_id = models.CharField(max_length=100, blank=True, db_index=True)
    idempotency_key = models.CharField(max_length=255, blank=True, db_index=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=20, choices=OutboxStatus.choices, default=OutboxStatus.PENDING, db_index=True
    )
    attempts = models.PositiveIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "automation_outbox_events"
        ordering = ["occurred_at"]

    def __str__(self) -> str:
        return f"{self.event_type} ({self.event_id}) [{self.status}]"


class WebhookSubscription(models.Model):
    """Configured target endpoint subscribed to financial business events (e.g. n8n)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.CASCADE,
        related_name="webhook_subscriptions",
    )
    target_url = models.URLField()
    secret = models.CharField(max_length=255)  # HMAC-SHA256 signing secret
    events = models.JSONField(default=list)  # e.g. ["*"] or ["invoice.issued", "payment.confirmed"]
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "automation_webhook_subscriptions"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.organization.name} -> {self.target_url}"


class WebhookDelivery(models.Model):
    """Record of an outbound webhook dispatch attempt."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(
        WebhookSubscription, on_delete=models.CASCADE, related_name="deliveries"
    )
    outbox_event = models.ForeignKey(
        OutboxEvent, on_delete=models.CASCADE, related_name="deliveries"
    )
    status = models.CharField(
        max_length=20, choices=DeliveryStatus.choices, default=DeliveryStatus.PENDING
    )
    status_code = models.PositiveSmallIntegerField(null=True, blank=True)
    response_body = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=0)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "automation_webhook_deliveries"
        ordering = ["-created_at"]


class IdempotencyRecord(models.Model):
    """Stores completed write responses to guarantee idempotency across network retries."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="idempotency_records"
    )
    key = models.CharField(max_length=255, db_index=True)
    request_path = models.CharField(max_length=255)
    request_hash = models.CharField(max_length=64)
    response_status = models.PositiveIntegerField()
    response_body = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "automation_idempotency_records"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "key"], name="unique_org_idempotency_key"
            )
        ]

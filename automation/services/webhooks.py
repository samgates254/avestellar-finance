import hashlib
import hmac
import json
import time
from datetime import timedelta
from typing import Any
import httpx
from django.utils import timezone
from automation.models import DeliveryStatus, OutboxEvent, OutboxStatus, WebhookDelivery, WebhookSubscription


def generate_webhook_signature(secret: str, timestamp: str, payload_bytes: bytes) -> str:
    """Computes HMAC-SHA256 hex signature over 'timestamp.payload'."""
    message = timestamp.encode("utf-8") + b"." + payload_bytes
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    secret: str, timestamp: str, payload_bytes: bytes, provided_signature: str, tolerance_seconds: int = 300
) -> bool:
    """Validates incoming webhook HMAC signature and guards against timestamp replay attacks."""
    try:
        ts = int(timestamp)
        now = int(time.time())
        if abs(now - ts) > tolerance_seconds:
            return False
    except (ValueError, TypeError):
        return False

    expected_signature = generate_webhook_signature(secret, timestamp, payload_bytes)
    return hmac.compare_digest(expected_signature, provided_signature)


def dispatch_outbox_event(event: OutboxEvent, client: httpx.Client | None = None) -> list[WebhookDelivery]:
    """Deliver an event, preserving failed events for an at-least-once retry."""
    subscriptions = WebhookSubscription.objects.filter(
        organization=event.organization, is_active=True
    )
    matching_subs = [
        sub
        for sub in subscriptions
        if ("*" in sub.events or event.event_type in sub.events)
        and not WebhookDelivery.objects.filter(
            subscription=sub,
            outbox_event=event,
            status=DeliveryStatus.SUCCESS,
        ).exists()
    ]

    deliveries = []
    payload_dict = {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "schema_version": event.schema_version,
        "occurred_at": event.occurred_at.isoformat(),
        "organization_id": str(event.organization_id),
        "source_system": event.source_system,
        "correlation_id": event.correlation_id,
        "idempotency_key": event.idempotency_key,
        "payload": event.payload,
    }
    payload_bytes = json.dumps(payload_dict, separators=(",", ":"), default=str).encode("utf-8")
    timestamp = str(int(time.time()))

    should_close_client = False
    if client is None:
        client = httpx.Client(timeout=10.0)
        should_close_client = True

    attempted_at = timezone.now()
    event.attempts += 1
    event.last_attempt_at = attempted_at
    failed = False
    try:
        for sub in matching_subs:
            signature = generate_webhook_signature(sub.secret, timestamp, payload_bytes)
            headers = {
                "Content-Type": "application/json",
                "X-Avestellar-Signature": signature,
                "X-Avestellar-Timestamp": timestamp,
                "X-Avestellar-Event-ID": event.event_id,
                "X-Avestellar-Event-Type": event.event_type,
            }

            delivery = WebhookDelivery.objects.create(
                subscription=sub,
                outbox_event=event,
                status=DeliveryStatus.PENDING,
                attempts=WebhookDelivery.objects.filter(
                    subscription=sub, outbox_event=event
                ).count() + 1,
            )

            try:
                response = client.post(sub.target_url, content=payload_bytes, headers=headers)
                delivery.status_code = response.status_code
                delivery.response_body = response.text[:1000]
                if 200 <= response.status_code < 300:
                    delivery.status = DeliveryStatus.SUCCESS
                else:
                    delivery.status = DeliveryStatus.FAILED
            except Exception as exc:
                delivery.status = DeliveryStatus.FAILED
                delivery.response_body = f"Connection error: {str(exc)}"[:1000]

            if delivery.status == DeliveryStatus.FAILED:
                failed = True
                retry_seconds = min(60 * (2 ** min(delivery.attempts - 1, 6)), 3600)
                delivery.next_retry_at = attempted_at + timedelta(seconds=retry_seconds)

            delivery.save(
                update_fields=[
                    "status",
                    "status_code",
                    "response_body",
                    "attempts",
                    "next_retry_at",
                ]
            )
            deliveries.append(delivery)

        event.status = OutboxStatus.FAILED if failed else OutboxStatus.PROCESSED
        event.processed_at = None if failed else attempted_at
        event.save(update_fields=["status", "attempts", "last_attempt_at", "processed_at"])
    finally:
        if should_close_client:
            client.close()

    return deliveries

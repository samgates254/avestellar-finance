import uuid
from typing import Any
from django.utils import timezone
from automation.models import OutboxEvent
from organizations.models import Organization


def publish_outbox_event(
    event_type: str,
    organization: Organization,
    payload: dict[str, Any],
    correlation_id: str = "",
    idempotency_key: str = "",
    source_system: str = "avestellar.finance",
    schema_version: str = "v1",
) -> OutboxEvent:
    """Emits an outbox event.

    Must be called inside the same database transaction as the financial state mutation.
    """
    event_id = str(uuid.uuid4())
    event = OutboxEvent.objects.create(
        event_id=event_id,
        event_type=event_type,
        schema_version=schema_version,
        organization=organization,
        source_system=source_system,
        correlation_id=correlation_id or str(uuid.uuid4()),
        idempotency_key=idempotency_key,
        payload=payload,
        occurred_at=timezone.now(),
    )
    return event

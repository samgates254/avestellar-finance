from rest_framework import serializers
from automation.models import OutboxEvent, WebhookDelivery, WebhookSubscription


class WebhookSubscriptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookSubscription
        fields = (
            "id",
            "target_url",
            "secret",
            "events",
            "is_active",
            "created_at",
        )
        read_only_fields = ("id", "created_at")
        extra_kwargs = {
            "secret": {"write_only": True},
        }


class WebhookDeliverySerializer(serializers.ModelSerializer):
    class Meta:
        model = WebhookDelivery
        fields = (
            "id",
            "subscription",
            "outbox_event",
            "status",
            "status_code",
            "response_body",
            "attempts",
            "created_at",
        )
        read_only_fields = fields


class OutboxEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = OutboxEvent
        fields = (
            "id",
            "event_id",
            "event_type",
            "schema_version",
            "source_system",
            "correlation_id",
            "idempotency_key",
            "payload",
            "status",
            "occurred_at",
            "processed_at",
        )
        read_only_fields = fields

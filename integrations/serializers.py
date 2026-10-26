from rest_framework import serializers
from integrations.models import ExternalObjectMapping, IntegrationSyncLog, MauzoPilotConfig


class MauzoPilotConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = MauzoPilotConfig
        fields = (
            "id",
            "base_url",
            "api_key",
            "webhook_secret",
            "is_active",
            "sync_customers",
            "sync_sales",
            "sync_payments",
            "request_timeout",
            "last_sync_at",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "last_sync_at", "created_at", "updated_at")
        extra_kwargs = {
            "api_key": {"write_only": True},
            "webhook_secret": {"write_only": True},
        }


class ExternalObjectMappingSerializer(serializers.ModelSerializer):
    class Meta:
        model = ExternalObjectMapping
        fields = (
            "id",
            "external_system",
            "external_type",
            "external_id",
            "internal_type",
            "internal_id",
            "synced_at",
        )
        read_only_fields = fields


class IntegrationSyncLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = IntegrationSyncLog
        fields = (
            "id",
            "external_system",
            "action",
            "status",
            "details",
            "created_at",
        )
        read_only_fields = fields

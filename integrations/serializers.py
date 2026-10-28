from rest_framework import serializers

from config.secret_serializers import SecretFieldProtectionMixin
from integrations.models import ExternalObjectMapping, IntegrationSyncLog, MauzoPilotConfig


class MauzoPilotConfigSerializer(SecretFieldProtectionMixin, serializers.ModelSerializer):
    # Both are provider credentials: write-only, and never wiped by a blank PATCH
    # unless the caller sends clear_secrets=true.
    secret_fields = ("api_key", "webhook_secret")

    class Meta:
        model = MauzoPilotConfig
        fields = (
            "id",
            "base_url",
            "business_id",
            "api_key",
            "webhook_secret",
            "is_active",
            "sync_customers",
            "sync_sales",
            "sync_payments",
            "auto_settle_cash",
            "request_timeout",
            "customer_sync_cursor",
            "sale_sync_cursor",
            "sale_after_id",
            "last_sync_at",
            "last_sync_status",
            "last_sync_error",
            "consecutive_failure_count",
            "created_at",
            "updated_at",
        )
        read_only_fields = (
            "id",
            "last_sync_at",
            "last_sync_status",
            "last_sync_error",
            "consecutive_failure_count",
            "created_at",
            "updated_at",
        )


class IntegrationHealthSerializer(serializers.Serializer):
    organization_id = serializers.UUIDField()
    organization_name = serializers.CharField()
    mauzopilot_enabled = serializers.BooleanField()
    mauzopilot_connected = serializers.BooleanField()
    mauzopilot_status = serializers.CharField()
    daraja_enabled = serializers.BooleanField()
    pending_outbox_events = serializers.IntegerField()
    failed_webhook_deliveries = serializers.IntegerField()
    last_sync_at = serializers.DateTimeField(allow_null=True)
    consecutive_failures = serializers.IntegerField()



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


class DarajaConfigSerializer(SecretFieldProtectionMixin, serializers.ModelSerializer):
    # consumer_key identifies the app and consumer_secret/passkey authenticate it;
    # all three are treated as credentials and are never returned by the API.
    secret_fields = ("consumer_key", "consumer_secret", "passkey")

    deposit_account_name = serializers.CharField(
        source="default_deposit_account.name", read_only=True
    )

    class Meta:
        from integrations.models import DarajaConfig
        model = DarajaConfig
        fields = (
            "id",
            "short_code",
            "consumer_key",
            "consumer_secret",
            "passkey",
            "initiator_name",
            "is_sandbox",
            "is_active",
            "default_deposit_account",
            "deposit_account_name",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "deposit_account_name", "created_at", "updated_at")


class DarajaCallbackEventSerializer(serializers.ModelSerializer):
    class Meta:
        from integrations.models import DarajaCallbackEvent
        model = DarajaCallbackEvent
        fields = (
            "id",
            "callback_type",
            "transaction_id",
            "status",
            "payload",
            "matched_payment",
            "processed_at",
            "error_message",
            "created_at",
        )
        read_only_fields = fields


class DarajaVerifyStatusRequestSerializer(serializers.Serializer):
    transaction_id = serializers.CharField(required=True)
    short_code = serializers.CharField(required=False, allow_blank=True)

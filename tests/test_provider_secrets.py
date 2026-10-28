"""Provider credentials at rest: redaction and accidental-wipe protection.

These tests use synthetic credential values only. They prove the three rules the
configuration API must uphold:

1. A stored secret is never returned by a serializer.
2. A blank or omitted submission does not erase a stored secret.
3. Clearing requires the explicit ``clear_secrets=true`` operation.
"""

import pytest
from rest_framework.test import APIClient

from automation.models import WebhookSubscription
from automation.serializers import WebhookSubscriptionSerializer
from identity.models import User
from integrations.models import DarajaConfig, MauzoPilotConfig
from integrations.serializers import DarajaConfigSerializer, MauzoPilotConfigSerializer
from ledger.services.initialization import initialize_organization_accounts
from organizations.models import Membership, Organization, RoleChoices

DARAJ_CONFIG_URL = "/api/v1/integrations/daraja/config/"
MAUZOPILOT_CONFIG_URL = "/api/v1/integrations/mauzopilot/config/"

SECRET_FIELDS = ("consumer_key", "consumer_secret", "passkey")


@pytest.mark.django_db
class TestDarajaConfigSecrets:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="secrets-admin@avestellar.test", password="Password123!"
        )
        self.org = Organization.objects.create(
            name="Provider Secrets Org", slug="provider-secrets-org", currency="KES"
        )
        Membership.objects.create(
            organization=self.org, user=self.user, role=RoleChoices.ADMIN
        )
        initialize_organization_accounts(self.org, self.user)
        self.client.force_authenticate(user=self.user)
        self.client.defaults["HTTP_X_ORGANIZATION_ID"] = str(self.org.id)

    def test_api_response_never_returns_provider_credentials(self):
        res = self.client.patch(
            DARAJ_CONFIG_URL,
            {
                "short_code": "600000",
                "consumer_key": "synthetic-consumer-key",
                "consumer_secret": "synthetic-consumer-secret",
                "passkey": "synthetic-passkey",
            },
            format="json",
        )
        assert res.status_code == 200, res.content

        body = res.json()
        for field in SECRET_FIELDS:
            assert field not in body

        get_body = self.client.get(DARAJ_CONFIG_URL)
        assert get_body.status_code == 200
        payload = get_body.content.decode()
        for secret in ("synthetic-consumer-key", "synthetic-consumer-secret", "synthetic-passkey"):
            assert secret not in payload
        # Non-secret configuration is still readable.
        assert get_body.json()["short_code"] == "600000"

    def test_blank_and_omitted_values_do_not_overwrite_stored_secrets(self):
        self.client.patch(
            DARAJ_CONFIG_URL,
            {
                "consumer_key": "synthetic-consumer-key",
                "consumer_secret": "synthetic-consumer-secret",
                "passkey": "synthetic-passkey",
            },
            format="json",
        )

        # A form-style submission that blanks every credential must be ignored.
        res = self.client.patch(
            DARAJ_CONFIG_URL,
            {"consumer_key": "", "consumer_secret": "   ", "passkey": ""},
            format="json",
        )
        assert res.status_code == 200, res.content

        config = DarajaConfig.objects.get(organization=self.org)
        assert config.consumer_key == "synthetic-consumer-key"
        assert config.consumer_secret == "synthetic-consumer-secret"
        assert config.passkey == "synthetic-passkey"

    def test_explicit_clear_operation_removes_stored_secrets(self):
        self.client.patch(
            DARAJ_CONFIG_URL,
            {
                "consumer_key": "synthetic-consumer-key",
                "consumer_secret": "synthetic-consumer-secret",
                "passkey": "synthetic-passkey",
            },
            format="json",
        )

        res = self.client.patch(DARAJ_CONFIG_URL, {"clear_secrets": True}, format="json")
        assert res.status_code == 200, res.content

        config = DarajaConfig.objects.get(organization=self.org)
        assert config.consumer_key == ""
        assert config.consumer_secret == ""
        assert config.passkey == ""

    def test_serializer_exposes_no_secret_and_keeps_values_on_partial_update(self):
        config = DarajaConfig.objects.create(
            organization=self.org,
            short_code="600001",
            consumer_key="synthetic-key",
            consumer_secret="synthetic-secret",
            passkey="synthetic-passkey",
        )

        assert "consumer_key" not in DarajaConfigSerializer(config).data

        serializer = DarajaConfigSerializer(config, data={"short_code": "600002"}, partial=True)
        assert serializer.is_valid(), serializer.errors
        serializer.save()
        config.refresh_from_db()
        assert config.short_code == "600002"
        assert config.consumer_key == "synthetic-key"


@pytest.mark.django_db
class TestMauzoPilotConfigSecrets:
    def setup_method(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            email="mp-secrets-admin@avestellar.test", password="Password123!"
        )
        self.org = Organization.objects.create(
            name="MP Secrets Org", slug="mp-secrets-org", currency="KES"
        )
        Membership.objects.create(
            organization=self.org, user=self.user, role=RoleChoices.ADMIN
        )
        initialize_organization_accounts(self.org, self.user)
        self.client.force_authenticate(user=self.user)
        self.client.defaults["HTTP_X_ORGANIZATION_ID"] = str(self.org.id)

    def test_api_response_never_returns_connector_credentials(self):
        res = self.client.patch(
            MAUZOPILOT_CONFIG_URL,
            {"api_key": "synthetic-mp-api-key", "webhook_secret": "synthetic-mp-webhook-secret"},
            format="json",
        )
        assert res.status_code == 200, res.content

        body = res.json()
        assert "api_key" not in body
        assert "webhook_secret" not in body

        payload = self.client.get(MAUZOPILOT_CONFIG_URL).content.decode()
        assert "synthetic-mp-api-key" not in payload
        assert "synthetic-mp-webhook-secret" not in payload

    def test_blank_patch_does_not_erase_connector_credentials(self):
        MauzoPilotConfig.objects.create(
            organization=self.org,
            business_id="business-1",
            api_key="synthetic-mp-api-key",
            webhook_secret="synthetic-mp-webhook-secret",
        )
        res = self.client.patch(
            MAUZOPILOT_CONFIG_URL, {"api_key": "", "webhook_secret": ""}, format="json"
        )
        assert res.status_code == 200, res.content

        config = MauzoPilotConfig.objects.get(organization=self.org)
        assert config.api_key == "synthetic-mp-api-key"
        assert config.webhook_secret == "synthetic-mp-webhook-secret"


@pytest.mark.django_db
class TestWebhookSubscriptionSecrets:
    def setup_method(self):
        self.org = Organization.objects.create(
            name="Webhook Secrets Org", slug="webhook-secrets-org", currency="KES"
        )

    def test_signing_secret_is_write_only(self):
        subscription = WebhookSubscription.objects.create(
            organization=self.org,
            target_url="https://hooks.example.test/avestellar",
            secret="synthetic-signing-secret",
            events=["payment.confirmed"],
        )
        assert "secret" not in WebhookSubscriptionSerializer(subscription).data

    def test_blank_secret_keeps_stored_value_and_clear_flag_removes_it(self):
        subscription = WebhookSubscription.objects.create(
            organization=self.org,
            target_url="https://hooks.example.test/avestellar",
            secret="synthetic-signing-secret",
            events=["payment.confirmed"],
        )

        serializer = WebhookSubscriptionSerializer(
            subscription, data={"secret": ""}, partial=True
        )
        assert serializer.is_valid(), serializer.errors
        serializer.save()
        subscription.refresh_from_db()
        assert subscription.secret == "synthetic-signing-secret"

        clearing = WebhookSubscriptionSerializer(
            subscription, data={"clear_secrets": True}, partial=True
        )
        assert clearing.is_valid(), clearing.errors
        clearing.save()
        subscription.refresh_from_db()
        assert subscription.secret == ""

    def test_secret_is_required_when_creating_a_subscription(self):
        serializer = WebhookSubscriptionSerializer(
            data={"target_url": "https://hooks.example.test/avestellar", "events": ["*"]}
        )
        assert not serializer.is_valid()
        assert "secret" in serializer.errors

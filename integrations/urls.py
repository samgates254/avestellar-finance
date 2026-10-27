from django.urls import path
from integrations.views import (
    DarajaC2BConfirmationWebhookView,
    DarajaC2BValidationWebhookView,
    DarajaConfigView,
    DarajaStkCallbackWebhookView,
    DarajaVerifyTransactionStatusView,
    IntegrationHealthOverviewView,
    MauzoPilotConfigView,
    MauzoPilotEventWebhookView,
    MauzoPilotMappingListView,
    MauzoPilotSyncTriggerView,
    MauzoPilotTestConnectionView,
    N8nInboundWebhookView,
)

urlpatterns = [
    # MauzoPilot
    path("integrations/mauzopilot/config/", MauzoPilotConfigView.as_view(), name="mauzopilot-config"),
    path("integrations/mauzopilot/test-connection/", MauzoPilotTestConnectionView.as_view(), name="mauzopilot-test-connection"),
    path("integrations/mauzopilot/sync/", MauzoPilotSyncTriggerView.as_view(), name="mauzopilot-sync-trigger"),
    path("integrations/mauzopilot/mappings/", MauzoPilotMappingListView.as_view(), name="mauzopilot-mappings"),
    path("integrations/mauzopilot/events/", MauzoPilotEventWebhookView.as_view(), name="mauzopilot-events"),

    # Integration Health and Telemetry
    path("integrations/health/", IntegrationHealthOverviewView.as_view(), name="integration-health-overview"),

    # n8n Inbound Webhook
    path("integrations/n8n/webhook/", N8nInboundWebhookView.as_view(), name="n8n-inbound-webhook"),

    # Safaricom Daraja M-Pesa Integration
    path("integrations/daraja/config/", DarajaConfigView.as_view(), name="daraja-config"),
    path("integrations/daraja/c2b-confirmation/", DarajaC2BConfirmationWebhookView.as_view(), name="daraja-c2b-confirmation"),
    path("integrations/daraja/c2b-validation/", DarajaC2BValidationWebhookView.as_view(), name="daraja-c2b-validation"),
    path("integrations/daraja/stk-callback/", DarajaStkCallbackWebhookView.as_view(), name="daraja-stk-callback"),
    path("integrations/daraja/verify-status/", DarajaVerifyTransactionStatusView.as_view(), name="daraja-verify-status"),
]

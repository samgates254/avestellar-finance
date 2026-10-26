from django.urls import path
from integrations.views import (
    MauzoPilotConfigView,
    MauzoPilotMappingListView,
    MauzoPilotTestConnectionView,
    N8nInboundWebhookView,
)

urlpatterns = [
    path("integrations/mauzopilot/config/", MauzoPilotConfigView.as_view(), name="mauzopilot-config"),
    path("integrations/mauzopilot/test-connection/", MauzoPilotTestConnectionView.as_view(), name="mauzopilot-test-connection"),
    path("integrations/mauzopilot/mappings/", MauzoPilotMappingListView.as_view(), name="mauzopilot-mappings"),
    path("integrations/n8n/webhook/", N8nInboundWebhookView.as_view(), name="n8n-inbound-webhook"),
]

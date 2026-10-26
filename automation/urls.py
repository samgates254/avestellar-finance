from django.urls import path
from automation.views import (
    OutboxEventListView,
    WebhookSubscriptionDetailView,
    WebhookSubscriptionListCreateView,
)

urlpatterns = [
    path("automation/webhooks/", WebhookSubscriptionListCreateView.as_view(), name="webhook-list-create"),
    path("automation/webhooks/<uuid:id>/", WebhookSubscriptionDetailView.as_view(), name="webhook-detail"),
    path("automation/outbox/", OutboxEventListView.as_view(), name="outbox-list"),
]

from rest_framework import generics, permissions
from drf_spectacular.utils import extend_schema, extend_schema_view
from automation.models import OutboxEvent, WebhookSubscription
from automation.serializers import OutboxEventSerializer, WebhookSubscriptionSerializer
from organizations.permissions import (
    IsAccountantOrAbove,
    IsOrgAdminOrOwner,
    IsOrganizationMember,
    get_request_organization,
)


@extend_schema_view(
    get=extend_schema(summary="List Webhook Subscriptions", description="List outbound webhook subscriptions."),
    post=extend_schema(summary="Register Webhook Subscription", description="Register a webhook target URL and HMAC secret for event notifications."),
)
class WebhookSubscriptionListCreateView(generics.ListCreateAPIView):
    serializer_class = WebhookSubscriptionSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrgAdminOrOwner)

    def get_queryset(self):
        org = get_request_organization(self.request, self)
        if not org:
            return WebhookSubscription.objects.none()
        return WebhookSubscription.objects.filter(organization=org)

    def perform_create(self, serializer):
        org = get_request_organization(self.request, self)
        serializer.save(organization=org)


class WebhookSubscriptionDetailView(generics.RetrieveDestroyAPIView):
    serializer_class = WebhookSubscriptionSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrgAdminOrOwner)
    lookup_field = "id"

    def get_queryset(self):
        org = get_request_organization(self.request, self)
        if not org:
            return WebhookSubscription.objects.none()
        return WebhookSubscription.objects.filter(organization=org)


@extend_schema_view(
    get=extend_schema(summary="List Outbox Events", description="Query transactional outbox events emitted by financial transactions.")
)
class OutboxEventListView(generics.ListAPIView):
    serializer_class = OutboxEventSerializer
    permission_classes = (permissions.IsAuthenticated, IsOrgAdminOrOwner)

    def get_queryset(self):
        org = get_request_organization(self.request, self)
        if not org:
            return OutboxEvent.objects.none()
        return OutboxEvent.objects.filter(organization=org).order_by("-occurred_at")

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view

from automation.services.webhooks import verify_webhook_signature
from integrations.adapters.mauzopilot import MauzoPilotAdapter
from integrations.models import ExternalObjectMapping, IntegrationSyncLog, MauzoPilotConfig
from integrations.serializers import (
    ExternalObjectMappingSerializer,
    IntegrationSyncLogSerializer,
    MauzoPilotConfigSerializer,
)
from organizations.models import Organization
from organizations.permissions import (
    IsOrgAdminOrOwner,
    IsOrganizationMember,
    get_request_organization,
)


@extend_schema_view(
    get=extend_schema(
        summary="Get MauzoPilot Configuration",
        description="Retrieve MauzoPilot connector configuration.",
        responses={200: MauzoPilotConfigSerializer},
    ),
    patch=extend_schema(
        summary="Update MauzoPilot Configuration",
        description="Update endpoints, credentials, and active state.",
        request=MauzoPilotConfigSerializer,
        responses={200: MauzoPilotConfigSerializer},
    ),
)
class MauzoPilotConfigView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrgAdminOrOwner)

    def get(self, request):
        org = get_request_organization(request, self)
        config, _ = MauzoPilotConfig.objects.get_or_create(organization=org)
        return Response(MauzoPilotConfigSerializer(config).data)

    def patch(self, request):
        org = get_request_organization(request, self)
        config, _ = MauzoPilotConfig.objects.get_or_create(organization=org)
        serializer = MauzoPilotConfigSerializer(config, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


@extend_schema_view(
    post=extend_schema(
        summary="Test MauzoPilot Connectivity",
        description="Run endpoint ping and check authentication health against MauzoPilot.",
        request=None,
        responses={200: OpenApiTypes.OBJECT},
    )
)
class MauzoPilotTestConnectionView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrgAdminOrOwner)

    def post(self, request):
        org = get_request_organization(request, self)
        adapter = MauzoPilotAdapter(organization=org)
        result = adapter.test_connection()
        return Response(result)


@extend_schema_view(
    get=extend_schema(
        summary="List External Object Mappings",
        description="List canonical mappings between external IDs and internal financial objects.",
        responses={200: ExternalObjectMappingSerializer(many=True)},
    )
)
class MauzoPilotMappingListView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrgAdminOrOwner)

    def get(self, request):
        org = get_request_organization(request, self)
        mappings = ExternalObjectMapping.objects.filter(organization=org).order_by("-synced_at")
        serializer = ExternalObjectMappingSerializer(mappings, many=True)
        return Response(serializer.data)


@extend_schema_view(
    post=extend_schema(
        summary="n8n Inbound Webhook Execution Endpoint",
        description="Receives automated workflow events from n8n with HMAC signature verification and routes through financial services.",
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT},
    )
)
class N8nInboundWebhookView(APIView):
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        org_id = request.headers.get("X-Organization-ID") or request.data.get("organization_id")
        if not org_id:
            return Response({"detail": "X-Organization-ID header is required."}, status=status.HTTP_400_BAD_REQUEST)

        org = Organization.objects.filter(id=org_id, is_active=True).first()
        if not org:
            return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

        cfg = MauzoPilotConfig.objects.filter(organization=org).first()
        if cfg and cfg.webhook_secret:
            sig = request.headers.get("X-Avestellar-Signature", "")
            ts = request.headers.get("X-Avestellar-Timestamp", "")
            if not sig or not ts or not verify_webhook_signature(cfg.webhook_secret, ts, request.body, sig):
                return Response({"detail": "Invalid or expired webhook signature."}, status=status.HTTP_401_UNAUTHORIZED)

        action = request.data.get("action")
        payload = request.data.get("payload", {})
        adapter = MauzoPilotAdapter(organization=org)

        try:
            if action == "sync_customer":
                customer = adapter.sync_customer(payload)
                return Response({"status": "SUCCESS", "customer_id": str(customer.id), "name": customer.name})
            elif action == "sync_sales_order":
                invoice = adapter.sync_sales_order_to_draft_invoice(payload)
                return Response({"status": "SUCCESS", "invoice_id": str(invoice.id), "invoice_number": invoice.invoice_number})
            elif action == "confirm_payment":
                payment = adapter.sync_payment_collection(payload)
                return Response({"status": "SUCCESS", "payment_id": str(payment.id), "amount": str(payment.amount)})
            else:
                return Response({"detail": f"Unrecognized action '{action}'."}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response({"status": "ERROR", "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

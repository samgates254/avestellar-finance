import hashlib
import hmac

from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone
from django.utils.dateparse import parse_datetime
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


def _verify_mauzopilot_event_signature(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """Verify MauzoPilot's raw-body HMAC and ISO timestamp contract."""
    try:
        parsed = parse_datetime(timestamp)
        if parsed is None or timezone.is_naive(parsed):
            return False
        if abs((timezone.now() - parsed).total_seconds()) > 300:
            return False
    except (TypeError, ValueError, OverflowError):
        return False

    provided = signature.removeprefix("sha256=").strip().lower()
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return bool(provided) and hmac.compare_digest(expected, provided)


class MauzoPilotEventWebhookView(APIView):
    """Receive the signed event envelope sent by MauzoPilot's webhook service."""

    permission_classes = (permissions.AllowAny,)

    @extend_schema(
        request=OpenApiTypes.OBJECT,
        responses={
            200: OpenApiTypes.OBJECT,
            400: OpenApiTypes.OBJECT,
            401: OpenApiTypes.OBJECT,
            404: OpenApiTypes.OBJECT,
            503: OpenApiTypes.OBJECT,
        },
    )
    def post(self, request):
        business_id = request.headers.get("X-Organization-ID", "")
        cfg = MauzoPilotConfig.objects.filter(business_id=business_id).select_related("organization").first()
        if not cfg or not cfg.organization.is_active:
            return Response({"detail": "MauzoPilot business is not configured."}, status=status.HTTP_404_NOT_FOUND)
        if not cfg.webhook_secret:
            return Response({"detail": "MauzoPilot webhook secret is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)

        signature = request.headers.get("X-Mauzopilot-Signature", "")
        timestamp = request.headers.get("X-Mauzopilot-Timestamp", "")
        if not _verify_mauzopilot_event_signature(cfg.webhook_secret, timestamp, request.body, signature):
            return Response({"detail": "Invalid or expired MauzoPilot signature."}, status=status.HTTP_401_UNAUTHORIZED)

        event = request.data
        if event.get("business_id") != business_id or not event.get("event_id"):
            return Response({"detail": "MauzoPilot event identity does not match its signed business."}, status=status.HTTP_400_BAD_REQUEST)
        if event.get("schema_version") != "1.0":
            return Response({"detail": "Unsupported MauzoPilot event schema version."}, status=status.HTTP_400_BAD_REQUEST)

        data = event.get("data")
        if not isinstance(data, dict):
            return Response({"detail": "MauzoPilot event data must be an object."}, status=status.HTTP_400_BAD_REQUEST)

        adapter = MauzoPilotAdapter(organization=cfg.organization)
        event_type = event.get("event_type")
        try:
            if event_type in {"customer.created", "customer.updated"}:
                customer = adapter.sync_customer(data)
                result = {"customer_id": str(customer.id)}
            elif event_type == "sale.completed":
                invoice = adapter.sync_sales_order_to_draft_invoice(data)
                result = {"invoice_id": str(invoice.id)}
                paid_cents = int(data.get("paid_cents", 0))
                if paid_cents > 0:
                    payment = adapter.sync_payment_collection(
                        {
                            "id": f"pay-{data['id']}",
                            "order_id": data["id"],
                            "paid_cents": paid_cents,
                            "payment_reference": data.get("payment_reference") or data.get("receipt_number", ""),
                            "payment_method": data.get("payment_method", "MPESA"),
                        }
                    )
                    result["payment_id"] = str(payment.id)
                    result["payment_status"] = payment.status
            else:
                return Response({"detail": f"Unsupported MauzoPilot event '{event_type}'."}, status=status.HTTP_400_BAD_REQUEST)
        except (KeyError, TypeError, ValueError) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        return Response({"status": "SUCCESS", "event_id": event["event_id"], **result})


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
        summary="Trigger MauzoPilot Incremental Synchronization",
        description="Initiates an on-demand incremental pull of updated customers and sales records from MauzoPilot.",
        request=None,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
)
class MauzoPilotSyncTriggerView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrgAdminOrOwner)

    def post(self, request):
        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        adapter = MauzoPilotAdapter(organization=org)
        summary = adapter.run_incremental_sync()
        return Response(summary.to_dict(), status=status.HTTP_200_OK)


@extend_schema_view(
    get=extend_schema(
        summary="Get Integration Health and Observability Status",
        description="Comprehensive operational telemetry for MauzoPilot connector, Daraja boundary, and transactional outbox queues.",
        responses={200: OpenApiTypes.OBJECT},
    )
)
class IntegrationHealthOverviewView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def get(self, request):
        from automation.models import OutboxEvent, OutboxStatus, WebhookDelivery, DeliveryStatus
        from integrations.models import DarajaConfig

        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        mp_cfg = MauzoPilotConfig.objects.filter(organization=org).first()
        daraja_cfg = DarajaConfig.objects.filter(organization=org).first()

        pending_outbox = OutboxEvent.objects.filter(
            organization=org, status=OutboxStatus.PENDING
        ).count()
        failed_deliveries = WebhookDelivery.objects.filter(
            subscription__organization=org, status=DeliveryStatus.FAILED
        ).count()

        mp_connected = False
        if mp_cfg and mp_cfg.is_active and mp_cfg.api_key:
            # Check fast connectivity status
            adapter = MauzoPilotAdapter(organization=org)
            conn = adapter.test_connection()
            mp_connected = bool(conn.get("connected", False))

        health_data = {
            "organization_id": str(org.id),
            "organization_name": org.name,
            "mauzopilot": {
                "enabled": bool(mp_cfg and mp_cfg.is_active),
                "connected": mp_connected,
                "business_id": mp_cfg.business_id if mp_cfg else "",
                "status": mp_cfg.last_sync_status if mp_cfg else "UNCONFIGURED",
                "last_sync_at": mp_cfg.last_sync_at.isoformat() if mp_cfg and mp_cfg.last_sync_at else None,
                "customer_sync_cursor": mp_cfg.customer_sync_cursor.isoformat() if mp_cfg and mp_cfg.customer_sync_cursor else None,
                "sale_sync_cursor": mp_cfg.sale_sync_cursor.isoformat() if mp_cfg and mp_cfg.sale_sync_cursor else None,
                "consecutive_failures": mp_cfg.consecutive_failure_count if mp_cfg else 0,
                "last_error": mp_cfg.last_sync_error if mp_cfg else "",
            },
            "daraja": {
                "enabled": bool(daraja_cfg and daraja_cfg.is_active),
                "is_sandbox": bool(daraja_cfg and daraja_cfg.is_sandbox),
                "short_code": daraja_cfg.short_code if daraja_cfg else "",
            },
            "outbox": {
                "pending_events_count": pending_outbox,
                "failed_deliveries_count": failed_deliveries,
            },
        }
        return Response(health_data, status=status.HTTP_200_OK)


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
        from invoicing.models import Invoice, InvoiceStatus
        from payments.models import StatementReconciliationStatus, StatementTransaction

        org_id = request.headers.get("X-Organization-ID") or request.data.get("organization_id")
        if not org_id:
            return Response({"detail": "X-Organization-ID header is required."}, status=status.HTTP_400_BAD_REQUEST)

        org = Organization.objects.filter(id=org_id, is_active=True).first()
        if not org:
            return Response({"detail": "Organization not found."}, status=status.HTTP_404_NOT_FOUND)

        cfg = MauzoPilotConfig.objects.filter(organization=org).first()
        if not cfg or not cfg.webhook_secret:
            return Response({"detail": "Webhook secret is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        sig = (
            request.headers.get("X-Avestellar-Signature")
            or request.headers.get("X-Webhook-Signature", "")
        )
        ts = (
            request.headers.get("X-Avestellar-Timestamp")
            or request.headers.get("X-Webhook-Timestamp", "")
        )
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
                from payments.services.verification import process_collection_with_verification
                payment, is_settled = process_collection_with_verification(
                    organization=org,
                    external_payment_data=payload,
                )
                if not is_settled:
                    return Response(
                        {
                            "status": "HELD_FOR_REVIEW",
                            "payment_id": str(payment.id),
                            "payment_status": payment.status,
                            "detail": "Payment recorded as pending verification. Ledger settlement requires verified provider reference, statement match, or authorized cash policy.",
                        },
                        status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    )
                return Response(
                    {
                        "status": "SUCCESS",
                        "payment_id": str(payment.id),
                        "amount": str(payment.amount),
                        "payment_status": payment.status,
                        "verified": True,
                    },
                    status=status.HTTP_200_OK,
                )
            elif action == "trigger_sync":
                summary = adapter.run_incremental_sync()
                return Response({"status": "SUCCESS", "summary": summary.to_dict()})
            elif action == "query_reconciliation_exceptions":
                exceptions = StatementTransaction.objects.filter(
                    organization=org,
                    reconciliation_status__in=[
                        StatementReconciliationStatus.UNRECONCILED,
                        StatementReconciliationStatus.INVESTIGATION,
                    ],
                ).values("id", "transaction_date", "description", "signed_amount", "currency", "reconciliation_status")[:50]
                return Response({"status": "SUCCESS", "exceptions": list(exceptions)})
            elif action == "query_overdue_invoices":
                today = timezone.now().date()
                overdue = Invoice.objects.filter(
                    organization=org,
                    status__in=[InvoiceStatus.ISSUED, InvoiceStatus.PARTIALLY_PAID],
                    due_date__lt=today,
                ).values("id", "invoice_number", "customer__name", "total_amount", "outstanding_balance", "due_date")[:50]
                return Response({"status": "SUCCESS", "overdue_invoices": list(overdue)})
            else:
                return Response({"detail": f"Unrecognized action '{action}'."}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response({"status": "ERROR", "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


@extend_schema_view(
    get=extend_schema(
        summary="Get Daraja M-Pesa Configuration",
        description="Retrieve Safaricom Daraja payment integration credentials and settings.",
        responses={200: OpenApiTypes.OBJECT},
    ),
    patch=extend_schema(
        summary="Update Daraja M-Pesa Configuration",
        description="Update Daraja shortcode, consumer key, passkey, and active status.",
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT},
    ),
)
class DarajaConfigView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrgAdminOrOwner)

    def get(self, request):
        from integrations.models import DarajaConfig
        from integrations.serializers import DarajaConfigSerializer

        org = get_request_organization(request, self)
        config, _ = DarajaConfig.objects.get_or_create(organization=org)
        return Response(DarajaConfigSerializer(config).data)

    def patch(self, request):
        from integrations.models import DarajaConfig
        from integrations.serializers import DarajaConfigSerializer

        org = get_request_organization(request, self)
        config, _ = DarajaConfig.objects.get_or_create(organization=org)
        serializer = DarajaConfigSerializer(config, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


def _resolve_daraja_org(request) -> Organization | None:
    """Resolve the organization a Daraja callback genuinely belongs to.

    Inbound Daraja callbacks are unauthenticated: Safaricom does not sign them
    with a request HMAC we could verify. A caller-supplied organization header
    or query parameter is therefore NOT treated as proof of ownership or
    authority (an attacker could set it to any org id). The organization must be
    derived from server-side state that the business itself created:

      - C2B: the payload's BusinessShortCode maps to that organization's active
        DarajaConfig, which only a member could configure.
      - STK Push: the CheckoutRequestID/MerchantRequestID matches a PENDING
        payment this system initiated for the organization (creating such a
        payment requires authentication and org membership).

    If neither binds, the callback is unattributable and the view rejects it.
    """
    from integrations.models import DarajaConfig
    from payments.models import Payment, PaymentStatus

    payload = request.data if isinstance(request.data, dict) else {}

    # C2B: authoritative binding via the pay-bill short code.
    short_code = payload.get("BusinessShortCode")
    if short_code:
        cfg = (
            DarajaConfig.objects.filter(
                short_code=str(short_code).strip(), organization__is_active=True
            )
            .select_related("organization")
            .first()
        )
        if cfg:
            return cfg.organization

    # STK Push: bind only to a payment the business already initiated.
    body = payload.get("Body")
    stk_body = body.get("stkCallback", {}) if isinstance(body, dict) else {}
    checkout_id = stk_body.get("CheckoutRequestID") or payload.get("CheckoutRequestID")
    merchant_id = stk_body.get("MerchantRequestID") or payload.get("MerchantRequestID")
    identifiers = [str(i).strip() for i in (checkout_id, merchant_id) if i]
    if identifiers:
        payment = (
            Payment.objects.filter(
                status=PaymentStatus.PENDING,
                reference__in=identifiers,
                organization__is_active=True,
            )
            .select_related("organization")
            .first()
        )
        if payment:
            return payment.organization

    return None


@extend_schema_view(
    post=extend_schema(
        summary="Safaricom Daraja C2B Confirmation Callback",
        description="Receives asynchronous C2B transaction confirmation callbacks from Safaricom M-Pesa with replay protection.",
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT},
    )
)
class DarajaC2BConfirmationWebhookView(APIView):
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        from integrations.adapters.daraja import process_c2b_callback

        org = _resolve_daraja_org(request)
        if not org:
            return Response(
                {"ResultCode": "1", "ResultDesc": "Organization context could not be resolved."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        resp = process_c2b_callback(organization=org, payload=request.data, is_validation=False)
        return Response(resp, status=status.HTTP_200_OK)


@extend_schema_view(
    post=extend_schema(
        summary="Safaricom Daraja C2B Validation Callback",
        description="Receives asynchronous C2B validation callbacks from Safaricom M-Pesa to validate customer accounts.",
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT},
    )
)
class DarajaC2BValidationWebhookView(APIView):
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        from integrations.adapters.daraja import process_c2b_callback

        org = _resolve_daraja_org(request)
        if not org:
            return Response(
                {"ResultCode": "C2B00012", "ResultDesc": "Invalid Organization"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        resp = process_c2b_callback(organization=org, payload=request.data, is_validation=True)
        return Response(resp, status=status.HTTP_200_OK)


@extend_schema_view(
    post=extend_schema(
        summary="Safaricom Daraja STK Push Callback",
        description="Receives asynchronous Lipa Na M-Pesa Online (STK Push) callbacks.",
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT},
    )
)
class DarajaStkCallbackWebhookView(APIView):
    permission_classes = (permissions.AllowAny,)

    def post(self, request):
        from integrations.adapters.daraja import process_stk_callback

        org = _resolve_daraja_org(request)
        if not org:
            return Response(
                {"ResultCode": "1", "ResultDesc": "Organization context could not be resolved."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        resp = process_stk_callback(organization=org, payload=request.data)
        return Response(resp, status=status.HTTP_200_OK)


@extend_schema_view(
    post=extend_schema(
        summary="Query Safaricom Daraja Transaction Status",
        description="Queries Safaricom Daraja Transaction Status API for payment verification.",
        request=OpenApiTypes.OBJECT,
        responses={200: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT},
    )
)
class DarajaVerifyTransactionStatusView(APIView):
    permission_classes = (permissions.IsAuthenticated, IsOrganizationMember)

    def post(self, request):
        from integrations.adapters.daraja import DarajaClient
        from integrations.models import DarajaConfig
        from integrations.serializers import DarajaVerifyStatusRequestSerializer

        org = get_request_organization(request, self)
        if not org:
            return Response({"detail": "Active organization context required."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = DarajaVerifyStatusRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        config = DarajaConfig.objects.filter(organization=org).first()
        client = DarajaClient(config=config)
        result = client.query_transaction_status(
            transaction_id=serializer.validated_data["transaction_id"],
            short_code=serializer.validated_data.get("short_code"),
        )
        return Response(result, status=status.HTTP_200_OK)

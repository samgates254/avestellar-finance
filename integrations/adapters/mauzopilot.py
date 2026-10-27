"""MauzoPilot Integration Adapter and Client.

Connects AVESTELLAR FINANCE to MauzoPilot's operational cloud backend for customer
and sales synchronization, verified settlement, and event-driven data flywheel integration.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
import logging
import time
from typing import Any
import uuid
import httpx
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from auditlog.services import record_audit_log
from automation.services.outbox import publish_outbox_event
from integrations.models import ExternalObjectMapping, IntegrationSyncLog, MauzoPilotConfig
from invoicing.models import Customer, Invoice, InvoiceLineItem, InvoiceStatus
from invoicing.services import issue_invoice
from ledger.models import Account, AccountCategory
from organizations.models import Organization
from payments.models import Payment, PaymentMethod, PaymentStatus
from payments.services import record_and_confirm_customer_payment

logger = logging.getLogger(__name__)


@dataclass
class SyncSummary:
    """Detailed operational metrics for a synchronization run."""

    organization_id: str
    started_at: datetime
    finished_at: datetime | None = None
    customers_retrieved: int = 0
    customers_created: int = 0
    customers_updated: int = 0
    customers_skipped: int = 0
    sales_retrieved: int = 0
    invoices_created: int = 0
    invoices_settled: int = 0
    sales_skipped: int = 0
    duplicates_detected: int = 0
    validation_failures: int = 0
    request_failures: int = 0
    errors: list[str] = field(default_factory=list)
    status: str = "SUCCESS"

    def to_dict(self) -> dict[str, Any]:
        return {
            "organization_id": self.organization_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "customers_retrieved": self.customers_retrieved,
            "customers_created": self.customers_created,
            "customers_updated": self.customers_updated,
            "customers_skipped": self.customers_skipped,
            "sales_retrieved": self.sales_retrieved,
            "invoices_created": self.invoices_created,
            "invoices_settled": self.invoices_settled,
            "sales_skipped": self.sales_skipped,
            "duplicates_detected": self.duplicates_detected,
            "validation_failures": self.validation_failures,
            "request_failures": self.request_failures,
            "errors": self.errors,
            "status": self.status,
        }


class MauzoPilotAPIError(Exception):
    """Base exception for MauzoPilot API communication failures."""
    def __init__(self, message: str, status_code: int | None = None, response_body: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class MauzoPilotRateLimitError(MauzoPilotAPIError):
    """Raised when upstream MauzoPilot API rate-limits the client (HTTP 429)."""
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message, status_code=429)
        self.retry_after = retry_after


class MauzoPilotAdapter:
    """Main connector implementing the verified MauzoPilot API integration."""

    MAX_RETRIES = 3
    RETRY_BACKOFF_FACTOR = 0.5

    def __init__(self, organization: Organization, client: httpx.Client | None = None):
        self.organization = organization
        self.client = client
        self.config = MauzoPilotConfig.objects.filter(organization=organization).first()

    def _get_headers(self, correlation_id: str | None = None) -> dict[str, str]:
        corr_id = correlation_id or f"ag-{uuid.uuid4().hex[:12]}"
        token = self.config.api_key if self.config else ""
        return {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "User-Agent": "AVESTELLAR-FINANCE-INTEGRATOR/1.0",
            "X-Client-System": "avestellar-finance",
            "X-Correlation-ID": corr_id,
        }

    def _request_with_retry(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> httpx.Response:
        """Executes an HTTP request to MauzoPilot with correlation ID and exponential backoff retry."""
        if not self.config or not self.config.is_active:
            raise MauzoPilotAPIError("MauzoPilot connector is unconfigured or disabled.")

        base_url = self.config.base_url.rstrip("/")
        url = f"{base_url}/{path.lstrip('/')}"
        headers = self._get_headers(correlation_id)

        timeout = float(self.config.request_timeout or 10)
        should_close = False
        client = self.client
        if client is None:
            client = httpx.Client(timeout=timeout)
            should_close = True

        last_exc: Exception | None = None
        for attempt in range(self.MAX_RETRIES):
            try:
                resp = client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                )

                if resp.status_code == 429:
                    retry_after_str = resp.headers.get("Retry-After", "1")
                    retry_after = int(retry_after_str) if retry_after_str.isdigit() else 1
                    if attempt < self.MAX_RETRIES - 1:
                        time.sleep(retry_after)
                        continue
                    raise MauzoPilotRateLimitError(
                        f"MauzoPilot rate limit exceeded for {url}", retry_after=retry_after
                    )

                if 500 <= resp.status_code < 600 and attempt < self.MAX_RETRIES - 1:
                    sleep_time = self.RETRY_BACKOFF_FACTOR * (2 ** attempt)
                    time.sleep(sleep_time)
                    continue

                if resp.status_code >= 400:
                    raise MauzoPilotAPIError(
                        f"MauzoPilot API returned error {resp.status_code}: {resp.text[:300]}",
                        status_code=resp.status_code,
                        response_body=resp.text,
                    )

                return resp

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_exc = exc
                if attempt < self.MAX_RETRIES - 1:
                    sleep_time = self.RETRY_BACKOFF_FACTOR * (2 ** attempt)
                    time.sleep(sleep_time)
                    continue
                break
            finally:
                if should_close and attempt == self.MAX_RETRIES - 1:
                    client.close()

        if should_close:
            client.close()

        raise MauzoPilotAPIError(f"MauzoPilot request failed after {self.MAX_RETRIES} attempts: {str(last_exc)}")

    def test_connection(self) -> dict[str, Any]:
        """Check public liveness plus authenticated access to the configured business."""
        if not self.config:
            return {
                "status": "UNCONFIGURED",
                "message": "MauzoPilot configuration has not been created for this organization.",
                "connected": False,
            }

        if not self.config.is_active:
            return {
                "status": "DISABLED",
                "message": "MauzoPilot connector is intentionally disabled until credentials are confirmed.",
                "connected": False,
            }

        if not self.config.api_key:
            return {
                "status": "MISSING_CREDENTIALS",
                "message": "MauzoPilot API key is required.",
                "connected": False,
            }
        if not self.config.business_id:
            return {
                "status": "MISSING_BUSINESS_ID",
                "message": "MauzoPilot business ID is required to validate connector access.",
                "connected": False,
            }

        base_url = self.config.base_url.rstrip("/")
        # MauzoPilot currently routes its public liveness endpoint at /api/health/.
        url = f"{base_url}/api/health/"
        headers = self._get_headers()

        should_close = False
        client = self.client
        if client is None:
            client = httpx.Client(timeout=float(self.config.request_timeout or 10))
            should_close = True

        start_time = time.monotonic()
        try:
            resp = client.get(url, headers=headers)
            if not 200 <= resp.status_code < 300:
                return {
                    "status": "UNAVAILABLE",
                    "status_code": resp.status_code,
                    "connected": False,
                    "latency_ms": round((time.monotonic() - start_time) * 1000, 2),
                    "base_url": self.config.base_url,
                    "business_id": self.config.business_id,
                }

            auth_url = f"{base_url}/api/businesses/{self.config.business_id}/customers/"
            auth_response = client.get(auth_url, headers=headers)
            latency_ms = round((time.monotonic() - start_time) * 1000, 2)
            connected = 200 <= auth_response.status_code < 300
            if connected:
                status_label = "CONNECTED"
            elif auth_response.status_code in (401, 403):
                status_label = "AUTHENTICATION_FAILED"
            else:
                status_label = "UNAVAILABLE"
            return {
                "status": status_label,
                "status_code": auth_response.status_code,
                "connected": connected,
                "latency_ms": latency_ms,
                "base_url": self.config.base_url,
                "business_id": self.config.business_id,
            }
        except Exception as exc:
            return {
                "status": "ERROR",
                "message": f"Connection failed: {str(exc)}",
                "connected": False,
                "base_url": self.config.base_url,
            }
        finally:
            if should_close:
                client.close()

    @transaction.atomic
    def sync_customer(self, external_customer_data: dict[str, Any]) -> Customer:
        """Idempotently syncs an operational customer from MauzoPilot into AVESTELLAR FINANCE.

        Guarantees that repeated ingestion under the same external ID resolves to the identical Customer record.
        """
        ext_id = str(external_customer_data.get("id") or external_customer_data.get("customer_id") or "").strip()
        if not ext_id:
            raise ValueError("External customer payload must contain a valid 'id' or 'customer_id'.")

        name = external_customer_data.get("name", "").strip() or f"MauzoPilot Customer {ext_id}"
        email = external_customer_data.get("email", "").strip()
        phone = external_customer_data.get("phone", "").strip()
        tax_pin = external_customer_data.get("tax_pin", "").strip()

        # Check existing external mapping
        mapping = ExternalObjectMapping.objects.filter(
            organization=self.organization,
            external_system="mauzopilot",
            external_type="customer",
            external_id=ext_id,
        ).first()

        if mapping:
            customer = Customer.objects.filter(id=mapping.internal_id, organization=self.organization).first()
            if customer:
                # Update details if changed
                changed = False
                if name and customer.name != name:
                    customer.name = name
                    changed = True
                if email and customer.email != email:
                    customer.email = email
                    changed = True
                if phone and customer.phone != phone:
                    customer.phone = phone
                    changed = True
                if changed:
                    customer.save(update_fields=["name", "email", "phone"])
                return customer

        # Create new Customer
        customer = Customer.objects.create(
            organization=self.organization,
            name=name,
            email=email,
            phone=phone,
            tax_pin=tax_pin,
            currency=self.organization.currency,
        )

        ExternalObjectMapping.objects.create(
            organization=self.organization,
            external_system="mauzopilot",
            external_type="customer",
            external_id=ext_id,
            internal_type="customer",
            internal_id=customer.id,
            raw_metadata=external_customer_data,
        )

        IntegrationSyncLog.objects.create(
            organization=self.organization,
            external_system="mauzopilot",
            action="SYNC_CUSTOMER",
            status="SUCCESS",
            details={"external_id": ext_id, "customer_id": str(customer.id), "name": name},
        )

        publish_outbox_event(
            event_type="customer.synchronized",
            organization=self.organization,
            idempotency_key=f"mp:cust:{ext_id}",
            payload={
                "customer_id": str(customer.id),
                "external_id": ext_id,
                "name": customer.name,
                "email": customer.email,
            },
        )

        return customer

    @transaction.atomic
    def sync_sales_order_to_draft_invoice(self, external_order_data: dict[str, Any]) -> Invoice:
        """Translates a MauzoPilot sales order or cloud sale into a validated draft sales invoice.

        Safeguards:
        1. Converts integer cents to Decimal currency ($1 KES = 100 cents).
        2. Validates order status is confirmed/fulfilled/completed.
        3. Checks external mapping to prevent duplicate invoice generation.
        4. Creates a DRAFT invoice; does NOT post to general ledger without validation.
        5. Emits 'sale.received' outbox event atomically.
        """
        ext_order_id = str(
            external_order_data.get("id")
            or external_order_data.get("order_number")
            or external_order_data.get("receipt_number")
            or ""
        ).strip()
        if not ext_order_id:
            raise ValueError("Order payload must contain an 'id', 'order_number', or 'receipt_number'.")

        order_status = str(external_order_data.get("status", "")).lower()
        if order_status not in ("confirmed", "completed", "fulfilled", "paid"):
            raise ValueError(f"Sales record {ext_order_id} is in status '{order_status}', expected completed/confirmed.")

        # Idempotency check
        existing_mapping = ExternalObjectMapping.objects.filter(
            organization=self.organization,
            external_system="mauzopilot",
            external_type="sales_order",
            external_id=ext_order_id,
        ).first()

        if existing_mapping:
            inv = Invoice.objects.filter(id=existing_mapping.internal_id, organization=self.organization).first()
            if inv:
                if external_order_data.get("total_cents") is not None:
                    reported_total = (
                        Decimal(str(external_order_data["total_cents"])) / Decimal("100")
                    ).quantize(Decimal("0.0001"))
                    if reported_total != inv.total_amount:
                        raise ValueError(
                            f"MauzoPilot sale {ext_order_id} total changed from "
                            f"{inv.total_amount} to {reported_total}."
                        )
                if external_order_data.get("subtotal_cents") is not None:
                    reported_subtotal = (
                        Decimal(str(external_order_data["subtotal_cents"])) / Decimal("100")
                    ).quantize(Decimal("0.0001"))
                    if reported_subtotal != inv.subtotal:
                        raise ValueError(
                            f"MauzoPilot sale {ext_order_id} subtotal changed from "
                            f"{inv.subtotal} to {reported_subtotal}."
                        )
                return inv

        # Resolve customer
        cust_payload = external_order_data.get("customer")
        if not cust_payload:
            cust_id = external_order_data.get("customer_id")
            cust_payload = {
                "id": cust_id or f"cust-{ext_order_id}",
                "name": external_order_data.get("customer_name") or ("Retail Customer" if not cust_id else f"Customer {cust_id}"),
            }
        customer = self.sync_customer(cust_payload)

        # Locate default revenue account
        revenue_account = (
            Account.objects.filter(
                organization=self.organization, system_role="REVENUE", is_active=True
            ).first()
            or Account.objects.filter(
                organization=self.organization, category=AccountCategory.REVENUE, is_active=True
            ).first()
        )
        if not revenue_account:
            raise ValueError("No active Revenue account found to map invoice line items.")

        today = timezone.now().date()
        invoice_number = f"INV-MP-{ext_order_id[:24]}"

        # Parse amounts: support cents (MauzoPilot CloudSale) or decimal amounts
        items_data = external_order_data.get("lines") or external_order_data.get("items", [])

        total_cents = external_order_data.get("total_cents")
        subtotal_cents = external_order_data.get("subtotal_cents")

        notes = external_order_data.get("notes") or f"Ingested from MauzoPilot Sale #{ext_order_id}"

        subtotal = Decimal("0.0000")
        tax_total = Decimal("0.0000")

        # MauzoPilot's CloudSale endpoint publishes integer KES cents and has
        # already validated line arithmetic. Re-check the snapshot at this
        # boundary before creating any financial record; a stale or malformed
        # payload must not become an invoice with a different total.
        upstream_subtotal = (
            (Decimal(str(subtotal_cents)) / Decimal("100")).quantize(Decimal("0.0001"))
            if subtotal_cents is not None
            else None
        )
        upstream_total = (
            (Decimal(str(total_cents)) / Decimal("100")).quantize(Decimal("0.0001"))
            if total_cents is not None
            else None
        )

        invoice = Invoice.objects.create(
            organization=self.organization,
            customer=customer,
            invoice_number=invoice_number,
            issue_date=today,
            due_date=today,
            status=InvoiceStatus.DRAFT,
            currency=self.organization.currency,
            notes=notes,
        )

        if items_data:
            for item in items_data:
                desc = item.get("product_name") or item.get("description", "Product Item")
                qty = Decimal(str(item.get("quantity", "1")))

                # Detect unit price in cents vs currency
                if "unit_price_cents" in item:
                    unit_price = (Decimal(str(item["unit_price_cents"])) / Decimal("100")).quantize(Decimal("0.0001"))
                    if item.get("unit_price") is not None:
                        decimal_price = Decimal(str(item["unit_price"])).quantize(Decimal("0.0001"))
                        if decimal_price != unit_price:
                            raise ValueError(f"MauzoPilot sale {ext_order_id} has inconsistent unit price fields.")
                else:
                    unit_price = Decimal(str(item.get("unit_price", "0"))).quantize(Decimal("0.0001"))

                tax_rate = Decimal(str(item.get("tax_rate", "0.0")))

                line = InvoiceLineItem.objects.create(
                    invoice=invoice,
                    revenue_account=revenue_account,
                    description=desc,
                    quantity=qty,
                    unit_price=unit_price,
                    tax_rate=tax_rate,
                )
                calculated_line_total = (line.quantity * line.unit_price).quantize(Decimal("0.0001"))
                if item.get("line_total_cents") is not None:
                    reported_line_total = (
                        Decimal(str(item["line_total_cents"])) / Decimal("100")
                    ).quantize(Decimal("0.0001"))
                    if calculated_line_total != reported_line_total:
                        raise ValueError(f"MauzoPilot sale {ext_order_id} has a line total that does not match its quantity and price.")
                subtotal += calculated_line_total
                tax_total += line.tax_amount
        else:
            # Fallback when lines are not supplied
            if total_cents is not None:
                calc_total = (Decimal(str(total_cents)) / Decimal("100")).quantize(Decimal("0.0001"))
            else:
                calc_total = Decimal(str(external_order_data.get("total_amount", "0"))).quantize(Decimal("0.0001"))

            line = InvoiceLineItem.objects.create(
                invoice=invoice,
                revenue_account=revenue_account,
                description=f"Sales Order #{ext_order_id}",
                quantity=Decimal("1.0000"),
                unit_price=calc_total,
                tax_rate=Decimal("0.0000"),
            )
            subtotal = calc_total

        if upstream_subtotal is not None and subtotal != upstream_subtotal:
            raise ValueError(
                f"MauzoPilot sale {ext_order_id} line subtotal {subtotal} does not "
                f"match upstream subtotal {upstream_subtotal}."
            )
        if upstream_total is not None and (subtotal + tax_total) != upstream_total:
            raise ValueError(
                f"MauzoPilot sale {ext_order_id} invoice total {subtotal + tax_total} "
                f"does not match upstream total {upstream_total}."
            )

        invoice.subtotal = subtotal
        invoice.tax_amount = tax_total
        invoice.total_amount = subtotal + tax_total
        invoice.outstanding_balance = invoice.total_amount
        invoice.save(update_fields=["subtotal", "tax_amount", "total_amount", "outstanding_balance"])

        ExternalObjectMapping.objects.create(
            organization=self.organization,
            external_system="mauzopilot",
            external_type="sales_order",
            external_id=ext_order_id,
            internal_type="invoice",
            internal_id=invoice.id,
            raw_metadata=external_order_data,
        )

        IntegrationSyncLog.objects.create(
            organization=self.organization,
            external_system="mauzopilot",
            action="SYNC_SALES_ORDER",
            status="SUCCESS",
            details={"order_id": ext_order_id, "invoice_id": str(invoice.id), "total": str(invoice.total_amount)},
        )

        publish_outbox_event(
            event_type="sale.received",
            organization=self.organization,
            idempotency_key=f"mp:sale:{ext_order_id}",
            payload={
                "order_id": ext_order_id,
                "invoice_id": str(invoice.id),
                "invoice_number": invoice.invoice_number,
                "customer_id": str(customer.id),
                "total_amount": str(invoice.total_amount),
            },
        )

        return invoice

    @transaction.atomic
    def sync_payment_collection(self, external_payment_data: dict[str, Any], user=None) -> Payment:
        """Processes an incoming payment collection event through the payment verification policy.

        Safeguards:
        1. Checks payment mapping to prevent duplicate payment postings.
        2. Resolves associated invoice and validates organizational context.
        3. Evaluates evidence against organization verification policy:
           - CASH: requires explicit auto_settle_cash policy and cashier attribution.
           - MPESA / BANK: requires matching verified Daraja callback or cleared statement line.
        4. Settle only when verified; otherwise records payment as PENDING awaiting verification.
        """
        from payments.services.verification import process_collection_with_verification

        payment, _ = process_collection_with_verification(
            organization=self.organization,
            external_payment_data=external_payment_data,
            user=user,
        )
        return payment

    def pull_customers_incremental(self, limit: int = 100) -> list[Customer]:
        """Queries MauzoPilot /api/businesses/<business_id>/customers/?updated_since=<cursor>."""
        if not self.config or not self.config.business_id:
            return []

        params: dict[str, Any] = {}
        if self.config.customer_sync_cursor:
            params["updated_since"] = self.config.customer_sync_cursor.isoformat()

        path = f"api/businesses/{self.config.business_id}/customers/"
        resp = self._request_with_retry("GET", path, params=params)
        data = resp.json()

        customer_list = data if isinstance(data, list) else data.get("results", [])
        synced_customers: list[Customer] = []
        latest_cursor: datetime | None = self.config.customer_sync_cursor

        # MauzoPilot's current customer endpoint returns the complete matching
        # list and has no page or after-ID cursor. Process the entire response;
        # locally truncating it at a timestamp shared by many records would
        # replay the first records forever and starve the rest.
        for item in customer_list:
            customer = self.sync_customer(item)
            synced_customers.append(customer)

            updated_at_str = item.get("updated_at_utc") or item.get("updated_at")
            if updated_at_str:
                dt = parse_datetime(updated_at_str)
                if dt and (latest_cursor is None or dt > latest_cursor):
                    latest_cursor = dt

        if latest_cursor and latest_cursor != self.config.customer_sync_cursor:
            self.config.customer_sync_cursor = latest_cursor
            self.config.save(update_fields=["customer_sync_cursor"])

        return synced_customers

    def pull_sales_incremental(self, limit: int = 100) -> list[Invoice]:
        """Queries MauzoPilot /api/businesses/<business_id>/sales/?received_since=<cursor>&after_id=<after_id>."""
        if not self.config or not self.config.business_id:
            return []

        params: dict[str, Any] = {}
        if self.config.sale_sync_cursor:
            params["received_since"] = self.config.sale_sync_cursor.isoformat()
            if self.config.sale_after_id:
                params["after_id"] = self.config.sale_after_id

        path = f"api/businesses/{self.config.business_id}/sales/"
        resp = self._request_with_retry("GET", path, params=params)
        data = resp.json()

        sales_list = data if isinstance(data, list) else data.get("results", [])
        invoices: list[Invoice] = []
        latest_cursor: datetime | None = self.config.sale_sync_cursor
        latest_after_id = self.config.sale_after_id

        for item in sales_list[:limit]:
            invoice = self.sync_sales_order_to_draft_invoice(item)
            invoices.append(invoice)

            # If sale reports collection, route through verification policy
            total_cents = item.get("total_cents", 0)
            paid_cents = item.get("paid_cents", 0)
            if paid_cents > 0:
                try:
                    payment = self.sync_payment_collection({
                        "id": f"pay-{item['id']}",
                        "order_id": item["id"],
                        "paid_cents": paid_cents,
                        "payment_reference": item.get("payment_reference") or item.get("receipt_number", ""),
                        "payment_method": item.get("payment_method", "MPESA"),
                        "employee_username": item.get("employee_username", ""),
                    })
                    if payment.status == PaymentStatus.CONFIRMED:
                        invoice.refresh_from_db()
                except Exception as exc:
                    logger.warning("Could not process collection for sale %s: %s", item.get("id"), exc)

            received_at_str = item.get("received_at_utc") or item.get("received_at")
            if received_at_str:
                dt = parse_datetime(received_at_str)
                if dt:
                    if latest_cursor is None or dt > latest_cursor:
                        latest_cursor = dt
                        latest_after_id = str(item.get("id", ""))
                    elif dt == latest_cursor:
                        latest_after_id = str(item.get("id", ""))

        if latest_cursor:
            self.config.sale_sync_cursor = latest_cursor
            self.config.sale_after_id = latest_after_id
            self.config.save(update_fields=["sale_sync_cursor", "sale_after_id"])

        return invoices

    def run_incremental_sync(self) -> SyncSummary:
        """Executes full operational synchronization across customers and sales."""
        started_at = timezone.now()
        summary = SyncSummary(
            organization_id=str(self.organization.id),
            started_at=started_at,
        )

        if not self.config or not self.config.is_active:
            summary.status = "DISABLED"
            summary.finished_at = timezone.now()
            return summary

        try:
            # 1. Sync Customers
            if self.config.sync_customers:
                custs = self.pull_customers_incremental()
                summary.customers_retrieved = len(custs)

            # 2. Sync Sales
            if self.config.sync_sales:
                invs = self.pull_sales_incremental()
                summary.sales_retrieved = len(invs)
                summary.invoices_created = len(invs)

            self.config.last_sync_at = timezone.now()
            self.config.last_sync_status = "SUCCESS"
            self.config.last_sync_error = ""
            self.config.consecutive_failure_count = 0
            self.config.save(
                update_fields=[
                    "last_sync_at",
                    "last_sync_status",
                    "last_sync_error",
                    "consecutive_failure_count",
                ]
            )

            summary.status = "SUCCESS"

        except Exception as exc:
            summary.status = "FAILED"
            summary.errors.append(str(exc))
            summary.request_failures += 1

            self.config.last_sync_status = "FAILED"
            self.config.last_sync_error = str(exc)[:500]
            self.config.consecutive_failure_count += 1
            self.config.save(
                update_fields=[
                    "last_sync_status",
                    "last_sync_error",
                    "consecutive_failure_count",
                ]
            )

        finally:
            summary.finished_at = timezone.now()
            IntegrationSyncLog.objects.create(
                organization=self.organization,
                external_system="mauzopilot",
                action="INCREMENTAL_SYNC",
                status=summary.status,
                details=summary.to_dict(),
            )
            record_audit_log(
                action="MAUZOPILOT_INCREMENTAL_SYNC",
                target_model="MauzoPilotConfig",
                target_id=str(self.config.id),
                organization=self.organization,
                details=summary.to_dict(),
            )

        return summary

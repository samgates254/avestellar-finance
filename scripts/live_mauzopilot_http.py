#!/usr/bin/env python3
"""Exercise MauzoPilot and AVESTELLAR FINANCE over real local HTTP.

The runner intentionally refuses to write unless AVESTELLAR points at a
PostgreSQL database named with the ``avestellar_live_http_`` prefix and the
configured MauzoPilot URL is loopback. Create synthetic tenants/configuration
first, start both application servers, then provide the environment variables
documented in docs/live_mauzopilot_http.md.
"""

from __future__ import annotations

import hashlib
import hmac
import httpx
import json
import os
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from unittest.mock import patch

import django

django.setup()

from django.core.management import call_command  # noqa: E402
from django.db import connection  # noqa: E402
from django.utils import timezone as django_timezone  # noqa: E402
from automation.models import (  # noqa: E402
    DeliveryStatus,
    OutboxEvent,
    WebhookDelivery,
    WebhookSubscription,
)
from automation.services.webhooks import dispatch_outbox_event  # noqa: E402
from auditlog.models import AuditLog  # noqa: E402
from integrations.models import (  # noqa: E402
    ExternalObjectMapping,
    IntegrationSyncLog,
    MauzoPilotConfig,
)
from invoicing.models import Customer, Invoice, InvoiceStatus  # noqa: E402
from ledger.models import JournalEntry, JournalLine, JournalStatus  # noqa: E402
from ledger.services.reporting import (  # noqa: E402
    get_balance_sheet,
    get_income_statement,
    get_trial_balance,
)
from organizations.models import Organization  # noqa: E402
from payments.models import Payment, PaymentStatus  # noqa: E402


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
    expected: tuple[int, ...] = (200,),
) -> tuple[int, dict | list | str]:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    req = Request(url, data=body, method=method, headers=headers or {})
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=8) as response:
            status_code = response.status
            content = response.read()
    except HTTPError as exc:
        status_code = exc.code
        content = exc.read()
    except URLError as exc:
        raise RuntimeError(f"HTTP request to {url} failed: {exc.reason}") from exc
    try:
        data = json.loads(content) if content else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = content.decode("utf-8", errors="replace")
    require(
        status_code in expected,
        f"{method} {url} returned HTTP {status_code}; expected {expected}: {str(data)[:500]}",
    )
    return status_code, data


def signed_event(
    *,
    config: MauzoPilotConfig,
    receiver_url: str,
    business_id: str,
    secret: str,
    event_type: str,
    data: dict,
    event_id: str | None = None,
    timestamp: str | None = None,
    signature_secret: str | None = None,
    mutate_after_signing: bool = False,
    omit_signature_headers: bool = False,
) -> tuple[int, dict]:
    timestamp = timestamp or django_timezone.now().isoformat()
    envelope = {
        "event_id": event_id or str(uuid.uuid4()),
        "event_type": event_type,
        "schema_version": "1.0",
        "business_id": business_id,
        "business_name": "Synthetic E2E Shop",
        "timestamp": timestamp,
        "correlation_id": str(uuid.uuid4()),
        "data": data,
    }
    body = json.dumps(envelope, separators=(",", ":"), default=str).encode()
    key = (signature_secret or secret).encode()
    signature = hmac.new(key, body, hashlib.sha256).hexdigest()
    headers = {
        "X-Organization-ID": business_id,
        "X-Mauzopilot-Timestamp": timestamp,
        "X-Mauzopilot-Signature": f"sha256={signature}",
    }
    if mutate_after_signing:
        body = body.replace(b"Synthetic", b"Tampered", 1)
    if omit_signature_headers:
        headers.pop("X-Mauzopilot-Signature")
    req = Request(
        f"{receiver_url.rstrip('/')}/api/v1/integrations/mauzopilot/events/",
        data=body,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=8) as response:
            code, content = response.status, response.read()
    except HTTPError as exc:
        code, content = exc.code, exc.read()
    try:
        response_data = json.loads(content) if content else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        response_data = content.decode("utf-8", errors="replace")[:500]
    return code, response_data


class _RetryReceiver(BaseHTTPRequestHandler):
    attempts = 0

    def do_POST(self):  # noqa: N802
        type(self).attempts += 1
        status_code = 503 if type(self).attempts == 1 else 200
        self.send_response(status_code)
        self.end_headers()
        self.wfile.write(b"synthetic receiver")

    def log_message(self, fmt, *args):
        return


def main() -> None:
    mauzo_base = os.environ.get("E2E_MAUZO_BASE_URL", "http://127.0.0.1:8101").rstrip("/")
    av_base = os.environ.get("E2E_AV_BASE_URL", "http://127.0.0.1:8100").rstrip("/")
    mauzo_token = os.environ["E2E_MAUZO_TOKEN"]
    business_id = os.environ["E2E_MAUZO_BUSINESS_ID"]
    organization_id = os.environ["E2E_AV_ORGANIZATION_ID"]
    api_key = os.environ["E2E_AV_ACCESS_TOKEN"]
    secret = os.environ["E2E_WEBHOOK_SECRET"]
    auth_mauzo = {"Authorization": f"Token {mauzo_token}"}
    auth_av = {"Authorization": f"Bearer {api_key}", "X-Organization-ID": organization_id}

    require(connection.vendor == "postgresql", "AVESTELLAR must use PostgreSQL for this test.")
    database_name = str(connection.settings_dict["NAME"])
    require(
        database_name.startswith("avestellar_live_http_"),
        "Refusing to run: AVESTELLAR DB is not an explicitly disposable avestellar_live_http_* database.",
    )
    config = MauzoPilotConfig.objects.select_related("organization").get(
        organization_id=organization_id
    )
    org = Organization.objects.get(id=organization_id)
    require(config.is_active, "Synthetic connector is disabled.")
    require(config.business_id == business_id, "MauzoPilot business context does not match config.")
    require(config.base_url.rstrip("/") == mauzo_base, "Connector URL does not point at the tested MauzoPilot server.")
    require(
        mauzo_base.startswith("http://127.0.0.1:") and av_base.startswith("http://127.0.0.1:"),
        "Only loopback HTTP URLs are allowed by this test runner.",
    )
    require(config.webhook_secret == secret, "Configured synthetic webhook secret does not match the runner secret.")

    _, mauzo_health = request("GET", f"{mauzo_base}/api/health/")
    require(mauzo_health.get("status") == "ok", "MauzoPilot health response was unexpected.")
    _, av_health = request("GET", f"{av_base}/api/v1/health/")
    require(av_health.get("status") == "HEALTHY" and av_health.get("database") == "CONNECTED", "AVESTELLAR DB health check failed.")
    print("PASS health: MauzoPilot liveness and AVESTELLAR PostgreSQL readiness")

    customers_url = f"{mauzo_base}/api/businesses/{business_id}/customers/"
    sales_url = f"{mauzo_base}/api/businesses/{business_id}/sales/"
    code, _ = request("GET", customers_url, headers={"Authorization": "Token invalid"}, expected=(401,))
    require(code == 401, "Invalid MauzoPilot token was not rejected.")
    _, cross_business = request(
        "GET",
        f"{mauzo_base}/api/businesses/{uuid.uuid4()}/customers/",
        headers=auth_mauzo,
        expected=(404,),
    )
    require("detail" in cross_business, "MauzoPilot business scoping did not reject an unknown tenant.")
    _, invalid_customer = request(
        "POST", customers_url, headers=auth_mauzo, payload={"id": str(uuid.uuid4()), "name": "   "}, expected=(400,)
    )
    require(bool(invalid_customer), "Invalid MauzoPilot customer data was not rejected.")
    _, customers_before = request("GET", customers_url, headers=auth_mauzo)
    require(isinstance(customers_before, list), "MauzoPilot customer response must be a list.")
    _, sales_before = request("GET", sales_url, headers=auth_mauzo)
    require(isinstance(sales_before, list), "MauzoPilot sales response must be a list.")
    print("PASS API contract: Token auth, business-scoped customer/sales lists, integer-cent sale payload contract")

    customer_id = str(uuid.uuid4())
    customer_payload = {
        "id": customer_id,
        "name": f"Synthetic HTTP Customer {customer_id[:8]}",
        "phone": "+254700000001",
        "email": f"customer-{customer_id[:8]}@example.invalid",
        "notes": "Synthetic HTTP integration fixture",
        "credit_limit_cents": 0,
        "is_active": True,
    }
    created_code, customer_response = request("POST", customers_url, headers=auth_mauzo, payload=customer_payload, expected=(201,))
    require(created_code == 201 and customer_response["id"] == customer_id, "Customer API create failed or changed its external identifier.")
    customer_mapping = ExternalObjectMapping.objects.get(
        organization=org, external_system="mauzopilot", external_type="customer", external_id=customer_id
    )
    customer = Customer.objects.get(id=customer_mapping.internal_id, organization=org)
    require(customer.email == customer_payload["email"] and customer.phone == customer_payload["phone"], "Customer fields did not synchronize correctly.")
    _, duplicate_customer = request("POST", customers_url, headers=auth_mauzo, payload=customer_payload, expected=(200,))
    require(duplicate_customer["id"] == customer_id, "Customer upsert did not preserve the external ID.")
    require(ExternalObjectMapping.objects.filter(organization=org, external_type="customer", external_id=customer_id).count() == 1, "Customer mapping duplicated.")
    print("PASS customer sync: live create, signed event, duplicate upsert, external ID preserved")

    product_id = str(uuid.uuid4())
    _, product = request(
        "PUT",
        f"{mauzo_base}/api/businesses/{business_id}/products/{product_id}/",
        headers=auth_mauzo,
        payload={
            "name": "Synthetic Test Widget",
            "sku": f"E2E-{product_id[:8]}",
            "category": "Synthetic",
            "selling_price_cents": 123450,
            "cost_price_cents": 25000,
            "low_stock_threshold": 0,
            "is_active": True,
        },
        expected=(201,),
    )
    require(product["id"] == product_id, "Product catalog upsert changed the product ID.")
    sale_id, line_id = str(uuid.uuid4()), str(uuid.uuid4())
    sale_reference = f"E2E-MPESA-{sale_id[:10]}"
    sale_payload = {
        "id": sale_id,
        "receipt_number": f"E2E-{sale_id[:10]}",
        "idempotency_key": f"e2e-sale-{sale_id}",
        "subtotal_cents": 123450,
        "total_cents": 123450,
        "payment_method": "mpesa_manual",
        "payment_reference": sale_reference,
        "paid_cents": 123450,
        "credit_cents": 0,
        "customer_id": customer_id,
        "employee_username": "synthetic-cashier",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "lines": [{
            "id": line_id,
            "product_id": product_id,
            "product_name": product["name"],
            "sku": product["sku"],
            "quantity": 1,
            "unit_price_cents": 123450,
            "unit_cost_cents": 25000,
            "line_total_cents": 123450,
        }],
    }
    journal_count_before_sale = JournalEntry.objects.filter(organization=org).count()
    sale_code, sale_response = request("POST", sales_url, headers=auth_mauzo, payload=sale_payload, expected=(201,))
    require(sale_code == 201 and sale_response["id"] == sale_id, "Sale API create failed or changed its external identifier.")
    require(sale_response["lines"][0]["line_total_cents"] == 123450 and sale_response["total_cents"] == 123450, "MauzoPilot returned an unexpected itemized total.")
    invoice_mapping = ExternalObjectMapping.objects.get(
        organization=org, external_system="mauzopilot", external_type="sales_order", external_id=sale_id
    )
    invoice = Invoice.objects.get(id=invoice_mapping.internal_id, organization=org)
    payment_mapping = ExternalObjectMapping.objects.get(
        organization=org, external_system="mauzopilot", external_type="payment", external_id=f"pay-{sale_id}"
    )
    payment = Payment.objects.get(id=payment_mapping.internal_id, organization=org)
    expected_amount = Decimal("1234.5000")
    require(invoice.customer_id == customer.id, "Invoice customer mapping does not match the synchronized customer.")
    require(invoice.status == InvoiceStatus.DRAFT, "Imported sale must remain a draft until issuance.")
    require(invoice.currency == org.currency == "KES" and invoice.total_amount == expected_amount, "Cents-to-Decimal invoice conversion failed.")
    require(invoice.items.count() == 1 and invoice.items.get().quantity == Decimal("1.0000") and invoice.items.get().unit_price == expected_amount, "Invoice itemization was not preserved.")
    require(payment.status == PaymentStatus.PENDING and payment.amount == expected_amount, "Reported collection without trusted evidence did not remain pending.")
    require(JournalEntry.objects.filter(organization=org).count() == journal_count_before_sale, "Sale synchronization unexpectedly changed the general ledger.")
    _, delivery_logs = request(
        "GET",
        f"{mauzo_base}/api/businesses/{business_id}/integrations/logs/",
        headers=auth_mauzo,
    )
    require(
        any(row.get("event_type") == "sale.completed" and row.get("status") == "success" and row.get("status_code") == 200 for row in delivery_logs),
        "MauzoPilot's live sale webhook delivery was not accepted by AVESTELLAR.",
    )
    print("PASS sale sync: live product and itemized sale, KES cents converted to Decimal, draft invoice, pending payment, no ledger posting")

    _, mapping_list = request("GET", f"{av_base}/api/v1/integrations/mauzopilot/mappings/", headers=auth_av)
    require(any(row["external_type"] == "sales_order" and row["external_id"] == sale_id for row in mapping_list), "AVESTELLAR mapping API omitted the sale mapping.")
    _, test_connection = request("POST", f"{av_base}/api/v1/integrations/mauzopilot/test-connection/", headers=auth_av, expected=(200,))
    require(test_connection.get("connected") is True, "Connector authenticated health probe failed.")
    _, first_sync = request("POST", f"{av_base}/api/v1/integrations/mauzopilot/sync/", headers=auth_av)
    require(first_sync.get("status") == "SUCCESS", f"Incremental sync failed: {first_sync.get('errors')}")
    _, second_sync = request("POST", f"{av_base}/api/v1/integrations/mauzopilot/sync/", headers=auth_av)
    require(second_sync.get("status") == "SUCCESS", f"Repeated incremental sync failed: {second_sync.get('errors')}")
    require(Invoice.objects.filter(organization=org, id=invoice.id).count() == 1, "Repeated synchronization duplicated the invoice.")
    require(ExternalObjectMapping.objects.filter(organization=org, external_type="sales_order", external_id=sale_id).count() == 1, "Repeated synchronization duplicated the sale mapping.")
    require(Payment.objects.filter(organization=org, id=payment.id).count() == 1, "Repeated synchronization duplicated the payment.")
    require(payment.refresh_from_db() is None and payment.status == PaymentStatus.PENDING, "Repeated sync changed pending payment state without evidence.")
    require(IntegrationSyncLog.objects.filter(organization=org, status="SUCCESS").count() >= 2, "Connector sync results were not recorded.")
    print("PASS connector: authenticated live HTTP sync repeated with no invoice, mapping, or payment duplicates")

    # Demonstrate raw-body HMAC and timestamp rejection against the live AV receiver.
    bad_id = str(uuid.uuid4())
    bad_event_data = {"id": bad_id, "name": "Should Never Be Created", "email": "invalid@example.invalid"}
    base_mapping_count = ExternalObjectMapping.objects.filter(organization=org, external_type="customer").count()
    bad_secret_code, _ = signed_event(config=config, receiver_url=av_base, business_id=business_id, secret=secret, signature_secret="wrong synthetic secret", event_type="customer.created", data=bad_event_data)
    require(bad_secret_code == 401, "Wrong webhook secret was accepted.")
    modified_code, _ = signed_event(config=config, receiver_url=av_base, business_id=business_id, secret=secret, event_type="customer.created", data=bad_event_data, mutate_after_signing=True)
    require(modified_code == 401, "Modified webhook body was accepted.")
    missing_code, _ = signed_event(config=config, receiver_url=av_base, business_id=business_id, secret=secret, event_type="customer.created", data=bad_event_data, omit_signature_headers=True)
    require(missing_code == 401, "Webhook with a missing signature header was accepted.")
    stale = (django_timezone.now() - timedelta(minutes=10)).isoformat()
    stale_code, _ = signed_event(config=config, receiver_url=av_base, business_id=business_id, secret=secret, event_type="customer.created", data=bad_event_data, timestamp=stale)
    require(stale_code == 401, "Stale webhook timestamp was accepted.")
    require(ExternalObjectMapping.objects.filter(organization=org, external_type="customer").count() == base_mapping_count, "Rejected webhook performed a customer side effect.")
    print("PASS webhook rejection: wrong secret, changed raw body, missing signature, and stale timestamp were rejected without business side effects")

    # Re-deliver the exact same signed event ID and verify the domain mapping makes
    # processing idempotent even though HTTP delivery itself is at-least-once.
    repeated_event_id = str(uuid.uuid4())
    event_code, repeated_response = signed_event(
        config=config,
        receiver_url=av_base,
        business_id=business_id,
        secret=secret,
        event_type="sale.completed",
        data=sale_response,
        event_id=repeated_event_id,
    )
    require(event_code == 200 and repeated_response.get("status") == "SUCCESS", "First manually signed duplicate-sale event was rejected.")
    duplicate_code, duplicate_response = signed_event(
        config=config,
        receiver_url=av_base,
        business_id=business_id,
        secret=secret,
        event_type="sale.completed",
        data=sale_response,
        event_id=repeated_event_id,
    )
    require(duplicate_code == 200 and duplicate_response.get("status") == "SUCCESS", "Repeated signed sale event was rejected.")
    require(Invoice.objects.filter(organization=org).count() == 1 and Payment.objects.filter(organization=org).count() == 1, "Repeated webhook duplicated the invoice/payment.")
    require(JournalEntry.objects.filter(organization=org).count() == journal_count_before_sale, "Repeated unverified webhook posted a journal entry.")
    print("PASS event replay: repeated verified HMAC event is idempotent at financial mapping boundary")

    conflicting_sale_event = dict(sale_response)
    conflicting_sale_event["paid_cents"] = 123449
    conflict_code, _ = signed_event(
        config=config,
        receiver_url=av_base,
        business_id=business_id,
        secret=secret,
        event_type="sale.completed",
        data=conflicting_sale_event,
    )
    payment.refresh_from_db()
    require(conflict_code == 200 and payment.status == PaymentStatus.PENDING and payment.amount == expected_amount, "A changed payment amount reinterpreted the existing pending payment.")
    require(Payment.objects.filter(organization=org).count() == 1, "Conflicting collection data created another payment.")
    conflicting_reference_event = dict(sale_response)
    conflicting_reference_event["payment_reference"] = f"OTHER-{sale_id[:10]}"
    reference_code, _ = signed_event(
        config=config,
        receiver_url=av_base,
        business_id=business_id,
        secret=secret,
        event_type="sale.completed",
        data=conflicting_reference_event,
    )
    payment.refresh_from_db()
    require(reference_code == 200 and payment.status == PaymentStatus.PENDING and payment.reference == sale_reference, "A changed reference reinterpreted the existing pending payment.")
    require(Payment.objects.filter(organization=org).count() == 1, "Conflicting reference created another payment.")
    print("PASS payment conflicts: changed cents and changed reference did not alter or duplicate the pending payment")

    # Issue through AVESTELLAR's authenticated domain endpoint, then verify using
    # the real Daraja callback adapter with wholly synthetic transaction data.
    _, issued = request("POST", f"{av_base}/api/v1/invoicing/invoices/{invoice.id}/issue/", headers=auth_av)
    invoice.refresh_from_db()
    require(invoice.status == InvoiceStatus.ISSUED and Decimal(issued["outstanding_balance"]) == expected_amount, "Invoice issuance failed before verification.")
    income_before = Decimal(get_income_statement(org, date.today(), date.today())["total_revenue"])
    good_callback = {
        "TransID": sale_reference,
        "TransTime": datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S"),
        "TransAmount": "1234.50",
        "BusinessShortCode": "TEST",
        "BillRefNumber": invoice.invoice_number,
        "MSISDN": "254700000001",
        "FirstName": "Synthetic",
        "LastName": "Payer",
    }
    _, callback_response = request("POST", f"{av_base}/api/v1/integrations/daraja/c2b-confirmation/", headers={"X-Organization-ID": organization_id}, payload=good_callback)
    payment.refresh_from_db()
    invoice.refresh_from_db()
    payment_mapping.refresh_from_db()
    require(callback_response.get("ResultCode") == "0", "Synthetic Daraja verification callback was rejected.")
    require(payment.status == PaymentStatus.CONFIRMED and payment.id == payment_mapping.internal_id, "Existing pending payment ID was not confirmed in place.")
    require(payment.amount == expected_amount and payment.currency == "KES" and payment.invoice_id == invoice.id, "Payment evidence did not match the obligation.")
    require(invoice.status == InvoiceStatus.PAID and invoice.outstanding_balance == Decimal("0.0000"), "Invoice was not settled exactly once.")
    payment_journals = JournalEntry.objects.filter(organization=org, entry_number=f"JE-PAY-{payment.payment_number}", status=JournalStatus.POSTED)
    require(payment_journals.count() == 1, "Expected one posted payment journal entry.")
    je = payment_journals.get()
    require(je.total_debit == expected_amount and je.total_credit == expected_amount, "Payment journal entry is not balanced.")
    require(je.lines.count() == 2, "Payment journal should contain two lines.")
    ar_line = je.lines.get(account__system_role="AR")
    cash_line = je.lines.get(account__system_role="CLEARING")
    require(ar_line.credit == expected_amount and cash_line.debit == expected_amount, "Payment journal did not debit clearing and credit receivables.")
    income_after = Decimal(get_income_statement(org, date.today(), date.today())["total_revenue"])
    require(income_after == income_before, "Payment settlement recognized revenue a second time.")
    require(AuditLog.objects.filter(organization=org, action="PENDING_PAYMENT_SETTLED", target_id=str(payment.id)).exists(), "Pending-payment settlement audit record is missing.")
    require(OutboxEvent.objects.filter(organization=org, event_type="payment.confirmed", payload__payment_id=str(payment.id)).exists(), "payment.confirmed outbox event is missing.")
    require(OutboxEvent.objects.filter(organization=org, event_type="payment.pending_settled", payload__confirmed_payment_id=str(payment.id)).exists(), "payment.pending_settled outbox event is missing.")
    print("PASS verification: amount conflict rejected; synthetic C2B callback promoted original payment and posted one balanced journal")

    duplicate_callback, duplicate_callback_body = request("POST", f"{av_base}/api/v1/integrations/daraja/c2b-confirmation/", headers={"X-Organization-ID": organization_id}, payload=good_callback)
    require(duplicate_callback == 200 and "Duplicate" in duplicate_callback_body.get("ResultDesc", ""), "Repeated Daraja callback was not replay-protected.")
    _, final_sync = request("POST", f"{av_base}/api/v1/integrations/mauzopilot/sync/", headers=auth_av)
    require(final_sync.get("status") == "SUCCESS", f"Post-settlement repeat sync failed: {final_sync.get('errors')}")
    require(Payment.objects.filter(organization=org).count() == 1 and JournalEntry.objects.filter(organization=org, entry_number=je.entry_number).count() == 1, "Repeated callback/sync duplicated payment accounting.")
    require(payment_mapping.internal_id == payment.id, "Payment mapping changed during verification.")
    print("PASS duplicate processing: repeated callback and sync did not create another payment or journal")

    trial = get_trial_balance(org, date.today())
    income = get_income_statement(org, date.today(), date.today())
    balance = get_balance_sheet(org, date.today())
    require(trial["is_balanced"], "Trial balance is not balanced.")
    require(balance["is_balanced"], "Balance sheet is not balanced.")
    trial_by_code = {row["code"]: Decimal(row["debit_balance"]) - Decimal(row["credit_balance"]) for row in trial["accounts"]}
    require(trial_by_code.get("1020") == expected_amount, "Clearing account balance does not reflect the settlement.")
    require(trial_by_code.get("1200", Decimal("0")) == Decimal("0"), "Accounts receivable did not clear.")
    require(Decimal(income["total_revenue"]) == expected_amount, "Revenue was not recognized once on invoice issuance.")
    print("PASS reporting: cash/clearing increased, receivables cleared, revenue recognized once, trial balance and balance sheet balanced")

    # Exercise at-least-once outbox delivery behavior with a local receiver that
    # fails once, then succeeds. Re-dispatching the same event must have no GL effect.
    receiver = HTTPServer(("127.0.0.1", 0), _RetryReceiver)
    receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    receiver_thread.start()
    try:
        event = OutboxEvent.objects.filter(organization=org, event_type="payment.confirmed", payload__payment_id=str(payment.id)).latest("occurred_at")
        subscription = WebhookSubscription.objects.create(
            organization=org,
            target_url=f"http://127.0.0.1:{receiver.server_port}/events/",
            secret="synthetic-outbox-secret",
            events=["payment.confirmed"],
            is_active=True,
        )
        with httpx.Client(timeout=5, trust_env=False) as local_client:
            failed = dispatch_outbox_event(event, client=local_client)
        require(len(failed) == 1 and failed[0].status == DeliveryStatus.FAILED, "Synthetic first outbox delivery did not fail as arranged.")
        event.refresh_from_db()
        require(event.status == "FAILED" and event.processed_at is None, "Failed outbox delivery was incorrectly marked processed.")
        failed[0].next_retry_at = django_timezone.now()
        failed[0].save(update_fields=["next_retry_at"])
        state_after_failure = (Payment.objects.filter(organization=org).count(), JournalEntry.objects.filter(organization=org).count(), Invoice.objects.get(id=invoice.id).outstanding_balance)
        httpx_client = httpx.Client
        with patch(
            "automation.services.webhooks.httpx.Client",
            side_effect=lambda *args, **kwargs: httpx_client(
                *args, trust_env=False, **kwargs
            ),
        ):
            call_command("process_outbox", limit=100)
        event.refresh_from_db()
        retried = WebhookDelivery.objects.filter(subscription=subscription, outbox_event=event).order_by("-created_at").first()
        require(retried is not None and retried.status == DeliveryStatus.SUCCESS, "Scheduled outbox retry did not succeed.")
        require(event.status == "PROCESSED" and event.attempts == 2, "Successful scheduled retry did not complete the outbox event.")
        require(WebhookDelivery.objects.filter(subscription=subscription, outbox_event=event).count() == 2, "Outbox retry attempts were not individually recorded.")
        require(state_after_failure == (Payment.objects.filter(organization=org).count(), JournalEntry.objects.filter(organization=org).count(), Invoice.objects.get(id=invoice.id).outstanding_balance), "Outbox retry repeated a financial side effect.")
    finally:
        receiver.shutdown()
        receiver.server_close()
        receiver_thread.join(timeout=2)
    print("PASS outbox: first HTTP attempt failed, scheduled retry succeeded, each delivery recorded, financial state unchanged")
    print("LIVE HTTP INTEGRATION PASSED")


if __name__ == "__main__":
    main()

# n8n Automation Workflows & Webhook Integration Guide

This guide details how **n8n** automation workflows interact with **AVESTELLAR FINANCE** via secure REST endpoints and HMAC-SHA256 authenticated webhooks.

---

## 1. Architectural Guardrails

1. **Zero Direct Database Access**: All actions must pass through AVESTELLAR FINANCE's domain services via `/api/v1/` REST endpoints or the `/api/v1/integrations/n8n/webhook/` endpoint.
2. **Tenant Isolation**: Every inbound request must provide an active `X-Organization-ID` header matching the authorized token context or signed organization. Cross-organization access is rejected.
3. **Double-Entry Integrity**: External workflow engines cannot directly post journal lines. Workflows interact through business operations (`issue_invoice`, `record_and_confirm_customer_payment`), ensuring every posted transaction is balanced ($\sum \text{Dr} = \sum \text{Cr}$).
4. **n8n HMAC Signature Verification & Replay Protection**:
   - Outbound and inbound payloads are signed using HMAC-SHA256 over `${timestamp}.${raw_body}`.
   - Timestamps older than 300 seconds are rejected immediately.

### Verified MauzoPilot API and event contract

The current MauzoPilot backend exposes `GET /api/health/` (liveness only),
`GET /api/businesses/{business_id}/customers/`, and
`GET /api/businesses/{business_id}/sales/`. The business-scoped routes require
an active membership and accept `Authorization: Token <key>` (also `Bearer`).
Customer and sale downloads are JSON lists, not a paginated `results` envelope.
Customer downloads accept `updated_since`; sales are ordered by
`received_at_utc` and ID, return at most 500 records, and accept the stable
`received_since` plus `after_id` cursor. Sale prices, totals, and payments are
integer KES cents. Sales include both line snapshots and normalized `items`;
the currently configured MauzoPilot accounting rules require total to equal
subtotal and do not apply tax or discounts. The sale payload does not carry a
currency field; AVESTELLAR interprets cents in the configured organization
currency, which must therefore be KES for this connector.

MauzoPilot's outbound webhook signs the exact compact JSON request body with
HMAC-SHA256 (hex digest, sent as `sha256=<digest>` in
`X-Mauzopilot-Signature`). `X-Mauzopilot-Timestamp` is an ISO-8601 timestamp;
`X-Organization-ID` carries the MauzoPilot business UUID. This differs from
the n8n action endpoint's timestamp-prefixed signature. Direct MauzoPilot
events must use `POST /api/v1/integrations/mauzopilot/events/`, which verifies
the MauzoPilot contract and resolves the AVESTELLAR organization through its
configured MauzoPilot business ID. Both endpoints require a non-empty shared
webhook secret. Duplicate customer/sale/payment effects are handled by the
existing external mappings and payment idempotency keys.

---

## 2. Inbound Webhook: Calling AVESTELLAR FINANCE from n8n

### Endpoint
- **URL**: `POST /api/v1/integrations/n8n/webhook/`
- **Headers**:
  - `Content-Type: application/json`
  - `X-Organization-ID: <ORGANIZATION_UUID>`
  - `X-Avestellar-Signature: <HMAC_SHA256_HEX>` (or `X-Webhook-Signature`)
  - `X-Avestellar-Timestamp: <UNIX_TIMESTAMP>` (or `X-Webhook-Timestamp`)

### Supported Inbound Actions

| Action | Payload Key Details | Resulting System Behavior |
| :--- | :--- | :--- |
| `sync_customer` | `{"id": "...", "name": "...", "email": "...", "phone": "..."}` | Idempotently syncs customer, creates `ExternalObjectMapping`, emits `customer.synchronized`. |
| `sync_sales_order` | `{"id": "...", "status": "completed", "items": [...]}` | Creates draft invoice with line items, emits `sale.received`. |
| `confirm_payment` | `{"id": "...", "order_id": "...", "amount": "...", "reference": "..."}` | Issues invoice if draft, settles receivable, posts GL entry, emits `payment.confirmed`. |
| `trigger_sync` | `{}` | Runs on-demand incremental pull from MauzoPilot. Returns detailed `SyncSummary`. |
| `query_reconciliation_exceptions` | `{}` | Returns unreconciled statement transactions requiring human attention. |
| `query_overdue_invoices` | `{}` | Returns list of issued/partially paid invoices where `due_date < today`. |

---

## 3. The 5 Core Automation Workflows

### Workflow A: Customer Synchronization
- **Trigger**: New customer registration in MauzoPilot or external CRM.
- **Workflow Steps**:
  1. Webhook node receives customer data.
  2. n8n HTTP Request node calls `POST /api/v1/integrations/n8n/webhook/` with action `sync_customer`.
  3. AVESTELLAR FINANCE resolves `ExternalObjectMapping`, updates/creates `Customer`, and returns canonical ID.
  4. n8n records external mapping in downstream CRM/ERP.

### Workflow B: Invoice Collection (Overdue Escalation)
- **Trigger**: Daily cron (e.g., 08:00 EAT) in n8n.
- **Workflow Steps**:
  1. n8n calls `POST /api/v1/integrations/n8n/webhook/` with action `query_overdue_invoices`.
  2. AVESTELLAR FINANCE returns overdue invoices with balance and customer details.
  3. Filter node partitions invoices by aging brackets (1–30 days, 31–60 days, >60 days).
  4. Sends automated WhatsApp reminder via Infobip/Twilio or sends finance reminder email.
  5. Records reminder event in customer audit history.

### Workflow C: Payment Reconciliation
- **Trigger**: Daraja M-Pesa C2B Confirmation or Bank webhook.
- **Workflow Steps**:
  1. Transaction arrives at AVESTELLAR FINANCE callback.
  2. If matched automatically, invoice is marked PAID and `payment.confirmed` is emitted.
  3. If unallocated or ambiguous, `payment.verification_required` is emitted to the outbox.
  4. n8n catches the outbox webhook and alerts the finance operations Slack channel with match suggestions.
  5. Operator approves match via Slack Interactive Button $\rightarrow$ n8n calls `POST /api/v1/payments/reconciliation/apply/`.

### Workflow D: Financial Exception Escalation
- **Trigger**: Outbox event `financial.exception` or reconciliation engine discrepancy.
- **Workflow Steps**:
  1. Outbound webhook fires to n8n endpoint.
  2. n8n verifies HMAC signature.
  3. Opens high-priority Jira/Linear ticket for the Senior Financial Controller.
  4. Preserves transaction reference, raw statement narrative, and discrepancy amount.

### Workflow E: Financial Intelligence & Liquidity Alerts
- **Trigger**: Scheduled weekly analytics job or outbox event `analytics.material_condition`.
- **Workflow Steps**:
  1. n8n queries `/api/v1/analytics/working-capital/` and `/api/v1/analytics/cash-flow/`.
  2. Evaluates Altman Z-Score and Quick Ratio against organization safety thresholds.
  3. If working capital drops below threshold or runway $< 60$ days, formats an Executive Briefing PDF and delivers it to the CFO.

Workflow action `confirm_payment` is a request to evaluate a reported
collection, not authority to bypass verification. The payment service can
return a pending-verification result; settlement requires the configured
provider evidence, cleared statement match, or an authorized manual/cash
policy.

### Workflow F: Payment verification exceptions and successful settlement

- Subscribe the n8n Webhook node to the AVESTELLAR outbox notification URL.
- Route `payment.verification_required` to an exception queue and alert the
  finance operator with the reference, amount, invoice ID, and correlation ID.
- Route `payment.confirmed` to the successful-payment notification flow only
  after AVESTELLAR commits the balanced journal and outbox event.
- Treat deliveries as at least once; use `event_id` and `idempotency_key` to
  deduplicate downstream notifications.

Configure these workflows with AVESTELLAR's authorized organization-scoped
API token and the shared webhook secret stored in n8n credentials. Keep all
financial writes behind AVESTELLAR APIs and services. No live n8n instance was
available in the local environment, so these are configuration examples and
were not executed against n8n.

---

## 4. Webhook Verification Example in n8n (JavaScript Code Node)

```javascript
const crypto = require('crypto');

const secret = $env.AVESTELLAR_WEBHOOK_SECRET;
const receivedSignature = $headers['x-avestellar-signature'] || $headers['x-webhook-signature'];
const timestamp = $headers['x-avestellar-timestamp'] || $headers['x-webhook-timestamp'];
const rawPayload = JSON.stringify($json);

// Check timestamp replay window (5 minutes = 300 seconds)
const now = Math.floor(Date.now() / 1000);
if (Math.abs(now - parseInt(timestamp)) > 300) {
  throw new Error("Rejected: Webhook timestamp expired (replay guard).");
}

// Compute expected HMAC-SHA256 signature
const message = `${timestamp}.${rawPayload}`;
const expectedSignature = crypto.createHmac('sha256', secret).update(message).digest('hex');

if (receivedSignature !== expectedSignature) {
  throw new Error("Rejected: Invalid HMAC signature.");
}

return [{ json: { verified: true, event: $json } }];
```

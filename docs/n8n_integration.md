# n8n Integration Guide: AVESTELLAR FINANCE

This guide explains how to connect **n8n** automation workflows to **AVESTELLAR FINANCE** using secure, signed, and idempotent HTTP webhook contracts.

---

## 1. Architecture Overview

AVESTELLAR FINANCE is designed as an API-first financial platform. External systems such as n8n interact with AVESTELLAR FINANCE strictly through **versioned REST APIs (`/api/v1/`)** and **transactional webhook events**.

**Direct database connections are prohibited.** Every mutating event passes through AVESTELLAR FINANCE's domain services, where tenant scoping, role-based authorization, idempotency, and double-entry invariants are enforced.

```
+---------------+           Outbound Webhooks (HMAC-SHA256)           +---------------+
|  AVESTELLAR   | --------------------------------------------------> |      n8n      |
|    FINANCE    |                                                     |   Workflows   |
|   (Backend)   | <-------------------------------------------------- |               |
+---------------+              Authenticated Inbound REST API         +---------------+
```

---

## 2. Inbound Webhooks: Calling AVESTELLAR FINANCE from n8n

### 2.1 Inbound Endpoint
- **URL**: `POST /api/v1/integrations/n8n/webhook/`
- **Headers**:
  - `Content-Type: application/json`
  - `X-Organization-ID: <ORGANIZATION_UUID>`
  - `X-Avestellar-Signature: <HMAC_SHA256_HEX>` *(if webhook secret is configured)*
  - `X-Avestellar-Timestamp: <UNIX_TIMESTAMP>` *(within 300s)*

### 2.2 Supported Actions & Payloads

#### Action A: Sync Customer
```json
{
  "action": "sync_customer",
  "payload": {
    "id": "ext-cust-501",
    "name": "Nairobi Solar Tech Ltd",
    "email": "billing@nairobisolar.ke",
    "phone": "+254711998877",
    "tax_pin": "P051234567Z"
  }
}
```

#### Action B: Sync Confirmed Sales Order to Draft Invoice
```json
{
  "action": "sync_sales_order",
  "payload": {
    "id": "ORD-2026-9901",
    "status": "confirmed",
    "customer": {
      "id": "ext-cust-501",
      "name": "Nairobi Solar Tech Ltd"
    },
    "items": [
      {
        "description": "5kW Solar Inverter",
        "quantity": "2",
        "unit_price": "85000.00",
        "tax_rate": "0.16"
      }
    ]
  }
}
```

#### Action C: Confirm Payment Collection
```json
{
  "action": "confirm_payment",
  "payload": {
    "id": "PAY-MPESA-QAB789",
    "order_id": "ORD-2026-9901",
    "amount": "197200.00",
    "payment_method": "MPESA",
    "reference": "QAB789XYZ"
  }
}
```

---

## 3. Outbound Webhooks: Consuming AVESTELLAR FINANCE Events in n8n

### 3.1 Subscribing to Events
Register an outbound webhook subscription via API:
```bash
POST /api/v1/automation/webhooks/
Headers:
  Authorization: Bearer <JWT_ACCESS_TOKEN>
  X-Organization-ID: <ORGANIZATION_UUID>
  Content-Type: application/json

Body:
{
  "target_url": "https://n8n.yourcompany.com/webhook/finance-events",
  "secret": "your-secure-shared-hmac-secret-32-chars",
  "events": ["invoice.issued", "payment.confirmed", "journal.posted"],
  "is_active": true
}
```

### 3.2 Canonical Event Envelope
Every event emitted by AVESTELLAR FINANCE adheres to this envelope:
```json
{
  "event_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "event_type": "payment.confirmed",
  "schema_version": "v1",
  "occurred_at": "2026-10-26T21:45:00.123456Z",
  "organization_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "source_system": "avestellar.finance",
  "correlation_id": "c73a03f2-472c-4d83-9b87-73d8ab5b4974",
  "idempotency_key": "idemp-pay-4491",
  "payload": {
    "payment_id": "0df81e1b-85d8-4f1b-88a2-2b62719a8491",
    "payment_number": "PAY-MP-8831",
    "invoice_id": "8f36a5c1-654d-4e92-bd17-91950e32aa76",
    "invoice_number": "INV-2026-001",
    "amount": "174000.0000",
    "currency": "KES",
    "payment_method": "MPESA",
    "reference": "QA12345678",
    "invoice_remaining_balance": "0.0000",
    "invoice_status": "PAID"
  }
}
```

### 3.3 Verifying Webhook Signatures in n8n (Code Node)
In your n8n workflow, insert a **Code Node (JavaScript)** directly after the Webhook Node:

```javascript
const crypto = require('crypto');

const secret = $env.AVESTELLAR_WEBHOOK_SECRET;
const receivedSignature = $headers['x-avestellar-signature'];
const timestamp = $headers['x-avestellar-timestamp'];
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

---

## 4. Planned n8n Automation Workflows

1. **Invoice Overdue Escalation**:
   - Webhook trigger: `invoice.issued` or daily scheduled poll of `/api/v1/analytics/receivables-aging/`.
   - Action: Send WhatsApp reminder or email alert if an invoice moves into the > 30 days overdue bracket.

2. **M-Pesa Payment Notification & Settlement**:
   - Webhook trigger: Payment receipt event from M-Pesa.
   - Action: Call `/api/v1/integrations/n8n/webhook/` with `confirm_payment`.
   - Result: Automatically settles the receivable, posts general ledger entries, and notifies the account manager.

3. **Customer Synchronization**:
   - MauzoPilot operational event -> n8n workflow -> AVESTELLAR FINANCE checks canonical external mapping table -> Creates Customer if new.

4. **Cash Balance Alert**:
   - Hourly/Daily n8n Cron calls `/api/v1/analytics/cash-flow/`.
   - If `projected_net_cash` drops below organization threshold, send Slack notification to CFO.

# MauzoPilot Integration Guide: AVESTELLAR FINANCE

This document specifies the integration architecture between **MauzoPilot** (the operational sales and inventory engine) and **AVESTELLAR FINANCE** (the double-entry Financial Operating System).

---

## 1. The AVESTELLAR Data Flywheel

The operational synergy follows this documented cycle:

```
MauzoPilot Operational Activity (Sales, Orders, Deliveries)
      │
      ▼
Validated Integration Events (HMAC, Timestamp, Idempotency)
      │
      ▼
Canonical Financial Records (Customers, Invoices, Payments)
      │
      ▼
Verified Ledger & Settlement (Double-Entry General Ledger Postings)
      │
      ▼
Financial Intelligence & Forecasting (Cash Flow, Aging, Margins)
      │
      ▼
Approved Insights Returned to MauzoPilot (Customer Balance, Credit Limits)
      │
      ▼
Improved Operational & Sales Decisions
```

---

## 2. Core Invariants & Boundaries

1. **Explicit Data Ownership**:
   - MauzoPilot is authoritative for operational order capture, catalog items, and point-of-sale actions.
   - AVESTELLAR FINANCE is authoritative for financial accounting, receivables recognition, tax calculations, ledger balances, and audit records.
2. **No Unvalidated Ledger Writes**:
   - A sales order received from MauzoPilot creates a **DRAFT invoice**.
   - An accounting entry is only recognized in the ledger when the invoice is validated and formally issued.
3. **Canonical Object Mapping**:
   - The `ExternalObjectMapping` table links `mauzopilot:sales_order:<id>` to internal `invoice:<uuid>`, and `mauzopilot:customer:<id>` to internal `customer:<uuid>`.
   - Repeated ingestion of the same external identifier is strictly idempotent.
4. **Credential Security**:
   - Upstream API keys and secrets are stored in protected environment variables and the `MauzoPilotConfig` model.
   - Credentials are write-only in API responses and never logged.

---

## 3. Configuration & Activating the Live Connector

By default, the live MauzoPilot connector is **intentionally disabled (`is_active = False`)** to protect ledger integrity until genuine API credentials and base URLs are provisioned.

### 3.1 Step 1: Configure Environment Variables
In `.env`:
```ini
MAUZOPILOT_ENABLED=True
MAUZOPILOT_BASE_URL=https://api.mauzopilot.com/v1
MAUZOPILOT_API_KEY=mp_live_your_actual_api_key_here
MAUZOPILOT_WEBHOOK_SECRET=mp_whsec_your_actual_webhook_secret_here
```

### 3.2 Step 2: Configure Organization Connector via REST API
```bash
PATCH /api/v1/integrations/mauzopilot/config/
Headers:
  Authorization: Bearer <JWT_ACCESS_TOKEN>
  X-Organization-ID: <ORGANIZATION_UUID>
  Content-Type: application/json

Body:
{
  "base_url": "https://api.mauzopilot.com/v1",
  "api_key": "mp_live_your_actual_api_key_here",
  "webhook_secret": "mp_whsec_your_actual_webhook_secret_here",
  "is_active": true,
  "sync_customers": true,
  "sync_sales": true,
  "sync_payments": true,
  "request_timeout": 15
}
```

### 3.3 Step 3: Run Endpoint Health Check
```bash
POST /api/v1/integrations/mauzopilot/test-connection/
Headers:
  Authorization: Bearer <JWT_ACCESS_TOKEN>
  X-Organization-ID: <ORGANIZATION_UUID>

Response:
{
  "status": "CONNECTED",
  "status_code": 200,
  "connected": true,
  "base_url": "https://api.mauzopilot.com/v1"
}
```

---

## 4. Canonical Object Mappings

| MauzoPilot Object | AVESTELLAR FINANCE Object | Mapping Invariant |
| :--- | :--- | :--- |
| Customer (`id`) | `invoicing.Customer` | Unique mapping in `integrations_external_mappings`. Duplicate requests resolve to the existing Customer. |
| Sales Order (`id`) | `invoicing.Invoice` | Creates a `DRAFT` invoice. Computes VAT/tax. Ledger receivable is not posted until formally issued. |
| Payment Collection (`id`) | `payments.Payment` | Settles matching invoice, transitions invoice to `PARTIALLY_PAID` or `PAID`, posts Dr Cash/Cr AR entry atomically. |

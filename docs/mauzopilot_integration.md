# MauzoPilot Integration Guide: AVESTELLAR FINANCE

This document specifies the integration architecture between **MauzoPilot** (the operational sales, POS, and inventory engine) and **AVESTELLAR FINANCE** (the double-entry Financial Operating System).

---

## 1. Verified Upstream MauzoPilot Architecture & Endpoints

Based on verified local source inspection of the MauzoPilot Django backend (`/home/mr-robot/Projects/mauzopilot`), the integration communicates directly with the following production endpoints:

| Endpoint | Method | Purpose | Verified Query Params / Payload |
| :--- | :--- | :--- | :--- |
| `/api/health/` | `GET` | Upstream connectivity & health ping | None. Returns `{"status": "ok"}` |
| `/api/businesses/<uuid:business_id>/customers/` | `GET` | Incremental Customer Synchronization | `updated_since=<ISO_8601>`. Returns customer array with `id`, `name`, `phone`, `email`, `credit_limit_cents`, `balance_cents`, `is_active`, `updated_at_utc`. |
| `/api/businesses/<uuid:business_id>/sales/` | `GET` | Incremental Sales Synchronization | `received_since=<ISO_8601>&after_id=<UUID>`. Returns sales array with line items, `total_cents`, `paid_cents`, `payment_method`, `lines` (`product_name`, `sku`, `unit_price_cents`, `quantity`). |
| `/api/businesses/<uuid:business_id>/cash-entries/` | `GET` | Daily till register & cash flow verification | `created_since=<ISO_8601>`. |

### Authentication Scheme
- Header: `Authorization: Token <api_key>`
- Client Identifiers: `X-Client-System: avestellar-finance`
- Correlation Tracking: `X-Correlation-ID: ag-<uuid_hex>`

---

## 2. Canonical Data Ownership & Accounting Boundaries

An explicit boundary prevents conflicting financial records or corrupted ledger state:

| Domain Entity | Authoritative System | Integration Role in AVESTELLAR FINANCE |
| :--- | :--- | :--- |
| **Point-of-Sale (POS) & Orders** | MauzoPilot | Ingested as operational sales record. Triggers creation of a **DRAFT invoice** (`InvoiceStatus.DRAFT`). |
| **Product Catalog & Stock** | MauzoPilot | Referenced on invoice line items for item description, SKU, and unit rate. |
| **Shop Customers & CRM** | MauzoPilot | Synced via `ExternalObjectMapping`. Creates or updates internal `Customer` record. |
| **General Ledger & Double-Entry** | AVESTELLAR FINANCE | Authoritative source of truth for accounts receivable, revenue recognition, sales tax (VAT), and settlement. |
| **Invoices & Receivables** | AVESTELLAR FINANCE | Validates commercial lines, applies tax rules, computes aging, and tracks payment settlement. |
| **Payment Settlement** | AVESTELLAR FINANCE | Verified through banking/M-Pesa integration (`Payment`). Posts debit to Cash/Bank/Clearing and credit to Accounts Receivable. |
| **Financial Intelligence & Analytics** | AVESTELLAR FINANCE | Calculates cash flow forecasting, Dupont analysis, Altman Z-Score, and publishes risk alerts. |

### Strict Invariants
1. **Never Invent Production Endpoints**: All API interactions map strictly to verified routes under `/api/businesses/<business_id>/`.
2. **Draft Financial Documents First**: An external sales order never immediately writes to the general ledger without validation. It is stored as a draft invoice until validated or auto-settled with proof of payment.
3. **Integer Cents to Decimal Conversion**: MauzoPilot expresses all monetary values in integer Kenya shillings cents ($1\text{ KES} = 100\text{ cents}$). AVESTELLAR FINANCE enforces high-precision `Decimal(18, 4)`:
   $$\text{Amount}_{\text{KES}} = \frac{\text{cents}}{100}$$
4. **Idempotent Ingestion**: Every external entity is recorded in `ExternalObjectMapping`. Repeated sync runs identify existing mappings and update existing records without creating duplicates.
5. **Separation of Initiation and Settlement**: Payment claims from external POS systems require matching against verified receipts or bank/clearing accounts.

---

## 3. Incremental Synchronization & Cursor Management

The connector employs persistent, organization-scoped synchronization cursors on `MauzoPilotConfig`:

- `customer_sync_cursor`: `DateTimeField` tracking `updated_at_utc` of the latest ingested customer. Sent as `?updated_since=<cursor>` on subsequent runs.
- `sale_sync_cursor`: `DateTimeField` tracking `received_at_utc` of the latest received sale.
- `sale_after_id`: `UUIDField` breaking ties when multiple sales share identical arrival timestamps. Sent as `?received_since=<cursor>&after_id=<uuid>`.

### Cursor Progression Workflow
```
[Start Sync]
    │
    ├─► GET /api/businesses/<business_id>/customers/?updated_since=<cursor>
    │     └─► Iterate records -> sync_customer() -> Advance customer_sync_cursor
    │
    └─► GET /api/businesses/<business_id>/sales/?received_since=<cursor>&after_id=<after_id>
          └─► Iterate records -> sync_sales_order_to_draft_invoice()
                ├─► If sale paid (paid_cents >= total_cents):
                │     └─► sync_payment_collection() -> Post to GL (Dr Cash/Clearing, Cr AR)
                └─► Advance sale_sync_cursor & sale_after_id
```

---

## 4. Error Handling, Rate Limiting & Safe Retries

1. **Transient Errors (HTTP 500-504)**: Retried up to 3 times with exponential backoff:
   $$T_{\text{sleep}} = 0.5 \times 2^{\text{attempt}}\text{ seconds}$$
2. **Rate Limiting (HTTP 429)**: The client inspects the `Retry-After` header. If within threshold, it sleeps and retries. If exhausted, it raises `MauzoPilotRateLimitError`.
3. **Correlation Tracking**: Every request carries an `X-Correlation-ID` header, which is preserved in `IntegrationSyncLog` and transactional outbox payloads.
4. **Failure Logging**: Any failure increments `MauzoPilotConfig.consecutive_failure_count` and updates `last_sync_error`.

---

## 5. API Endpoints in AVESTELLAR FINANCE

### 5.1 Trigger Incremental Sync
```http
POST /api/v1/integrations/mauzopilot/sync/
Headers:
  Authorization: Bearer <JWT_ACCESS_TOKEN>
  X-Organization-ID: <ORGANIZATION_UUID>

Response (200 OK):
{
  "organization_id": "113df537-e604-4f81-98e7-23131f7c1243",
  "started_at": "2026-10-27T06:37:27.949785Z",
  "finished_at": "2026-10-27T06:37:28.115432Z",
  "customers_retrieved": 12,
  "customers_created": 3,
  "customers_updated": 9,
  "customers_skipped": 0,
  "sales_retrieved": 45,
  "invoices_created": 45,
  "invoices_settled": 42,
  "sales_skipped": 0,
  "duplicates_detected": 0,
  "validation_failures": 0,
  "request_failures": 0,
  "status": "SUCCESS"
}
```

### 5.2 Test Connectivity
```http
POST /api/v1/integrations/mauzopilot/test-connection/
Headers:
  Authorization: Bearer <JWT_ACCESS_TOKEN>
  X-Organization-ID: <ORGANIZATION_UUID>

Response (200 OK):
{
  "status": "CONNECTED",
  "status_code": 200,
  "connected": true,
  "latency_ms": 42.15,
  "base_url": "https://api.mauzopilot.com",
  "business_id": "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d"
}
```

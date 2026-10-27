# Integration Operations & Failure Recovery Guide

This runbook outlines operational procedures, health monitoring, cursor management, and failure recovery for AVESTELLAR FINANCE integrations.

---

## 1. Operational Telemetry Endpoint

AVESTELLAR FINANCE exposes a centralized health and telemetry endpoint for real-time observability:

```http
GET /api/v1/integrations/health/
Headers:
  Authorization: Bearer <JWT_ACCESS_TOKEN>
  X-Organization-ID: <ORGANIZATION_UUID>

Response (200 OK):
{
  "organization_id": "113df537-e604-4f81-98e7-23131f7c1243",
  "organization_name": "Acme Retail Ltd",
  "mauzopilot": {
    "enabled": true,
    "connected": true,
    "business_id": "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
    "status": "SUCCESS",
    "last_sync_at": "2026-10-27T06:37:28.115432Z",
    "customer_sync_cursor": "2026-10-27T06:00:00Z",
    "sale_sync_cursor": "2026-10-27T06:30:00Z",
    "consecutive_failures": 0,
    "last_error": ""
  },
  "daraja": {
    "enabled": true,
    "is_sandbox": false,
    "short_code": "600999"
  },
  "outbox": {
    "pending_events_count": 0,
    "failed_deliveries_count": 0
  }
}
```

### Security Invariant: Zero Credential Leakage
Under no circumstance do API keys, webhook secrets, Daraja passkeys, or consumer secrets appear in this telemetry response or operational logs.

---

## 2. Connector Health States

| State | Indicator | Recommended Action |
| :--- | :--- | :--- |
| **UNCONFIGURED** | `enabled: false, business_id: ""` | Organization has not set up MauzoPilot credentials. |
| **DISABLED** | `enabled: false` | Integration disabled by admin. Safe mode; no sync calls run. |
| **CONNECTED** | `connected: true, status: "SUCCESS"` | Live health ping to `/api/health/` succeeded with latency $< 250\text{ms}$. |
| **UNAVAILABLE** | `connected: false` | Upstream returned 5xx error or connection timed out. |
| **DEGRADED** | `consecutive_failures > 3` | Multiple sync runs failed. Check `last_error` and inspect network / credentials. |

---

## 3. Cursors & Safe Reset Procedures

Cursors prevent re-fetching millions of historical records on every poll.

- **Customer Sync Cursor**: Timestamp of the latest modified customer in MauzoPilot.
- **Sales Sync Cursor & After ID**: Timestamp and UUID of the latest imported sale.

### Handling Cursor Drift or Missed Records
If an operational audit indicates sales were missed during a prolonged upstream outage:
1. Note the current cursor values from `GET /api/v1/integrations/health/`.
2. Admin can adjust the cursors backwards via:
   ```http
   PATCH /api/v1/integrations/mauzopilot/config/
   {
     "sale_sync_cursor": "2026-10-20T00:00:00Z",
     "sale_after_id": null
   }
   ```
3. Trigger an incremental sync: `POST /api/v1/integrations/mauzopilot/sync/`.
4. Idempotency safeguards will skip already-imported records and ingest only the missing sales without duplicating invoices.

---

## 4. Failure Recovery & Retry Policies

### 4.1 Transient Failures & Exponential Backoff
- Internal HTTP client performs up to 3 retries with backoff factors:
  - Attempt 1: immediate
  - Attempt 2: $0.5\text{s}$
  - Attempt 3: $1.0\text{s}$
- If upstream returns HTTP 429 (Rate Limit Exceeded), the client respects `Retry-After`.

### 4.2 Webhook Outbox Delivery Retries
- Outbox events transition to `PROCESSED` once dispatched.
- Failed webhook deliveries (`WebhookDelivery.status == "FAILED"`) record the HTTP status code and response snippet.
- Retries can be dispatched via automated Celery worker or administrative trigger without rolling back committed ledger data.

### 4.3 Avoiding Infinite Loops
- Synchronization jobs terminate if upstream returns fatal authentication errors (HTTP 401/403).
- Consecutive failure counters increment on error and reset to 0 upon any successful sync run.

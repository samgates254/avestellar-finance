# Canonical Data Flywheel Architecture

## 1. Executive Summary

The **AVESTELLAR Canonical Data Flywheel** connects commercial front-office transactions, double-entry financial accounting, transactional event outboxes, automated workflow triggers, and strategic business intelligence into a self-reinforcing, event-driven loop.

```
                    ┌────────────────────────┐
                    │  MauzoPilot POS / ERP   │
                    │ (Operational Commerce) │
                    └───────────┬────────────┘
                                │
                        REST / Webhooks
                                │
                                ▼
                    ┌────────────────────────┐
                    │   AVESTELLAR FINANCE   │
                    │   Integration Layer    │
                    └───────────┬────────────┘
                                │
                       Validate & Convert
                                │
                                ▼
                    ┌────────────────────────┐
                    │ Canonical Double-Entry │
                    │     General Ledger     │
                    └───────────┬────────────┘
                                │
                      Atomic Outbox Events
                                │
                                ▼
                    ┌────────────────────────┐
                    │  Transactional Outbox  │
                    │  & Webhook Dispatcher  │
                    └───────────┬────────────┘
                                │
                       HMAC-Signed Webhooks
                                │
                                ▼
                    ┌────────────────────────┐
                    │  n8n Automated Engine  │
                    │ (Collections, Alerts)  │
                    └───────────┬────────────┘
                                │
                        Enriched Insights
                                │
                                ▼
                    ┌────────────────────────┐
                    │ Executive Analytics &  │
                    │    Feedback to POS     │
                    └────────────────────────┘
```

---

## 2. Event Contracts & Schemas

All events are published atomically inside database transactions using the `publish_outbox_event()` service. Delivery is handled asynchronously without holding open database locks or allowing external HTTP failures to roll back committed financial ledger entries.

### Event Catalog

| Event Name | Schema Version | Emitted When | Payload Highlights |
| :--- | :--- | :--- | :--- |
| `customer.synchronized` | `1.0` | Customer synced/mapped from MauzoPilot or external CRM. | `customer_id`, `external_id`, `name`, `email` |
| `sale.received` | `1.0` | POS sale order converted into draft invoice. | `order_id`, `invoice_id`, `invoice_number`, `customer_id`, `total_amount` |
| `invoice.issued` | `1.0` | Draft invoice approved/issued; receivables posted to GL. | `invoice_id`, `invoice_number`, `customer_id`, `total_amount`, `due_date` |
| `payment.confirmed` | `1.0` | Verified customer payment settled into clearing/bank. | `payment_id`, `invoice_id`, `amount`, `payment_method`, `journal_entry_id` |
| `payment.verification_required` | `1.0` | M-Pesa / Bank payment arrives unallocated or ambiguous. | `payment_id`, `external_reference`, `amount`, `bill_ref_number`, `msisdn` |
| `financial.exception` | `1.0` | Unreconciled statement discrepancy or ledger imbalance. | `transaction_id`, `amount`, `source`, `reason` |

### Sample Outbox Event Envelope
```json
{
  "event_id": "evt-77b8c2d1-e94f-4ab2-811d-65a8df23b9c0",
  "event_type": "payment.confirmed",
  "schema_version": "1.0",
  "occurred_at": "2026-10-27T06:30:00.000Z",
  "organization_id": "113df537-e604-4f81-98e7-23131f7c1243",
  "correlation_id": "ag-9a8b7c6d5e4f",
  "idempotency_key": "mp:pay:pay-834cba8a-c603-455b",
  "payload": {
    "payment_id": "31b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
    "invoice_id": "a1b2c3d4-e5f6-7a8b-9c0d-1e2f3a4b5c6d",
    "amount": "87000.0000",
    "currency": "KES",
    "payment_method": "MPESA",
    "reference": "MPESA-FLY-7766",
    "journal_entry_id": "e9b8a7c6-d5f4-3e2a-1b0c-9d8e7f6a5b4c"
  }
}
```

---

## 3. Direction and Authority Rules

1. **MauzoPilot $\rightarrow$ AVESTELLAR FINANCE**:
   - Operational sales records, till collections, POS customer data.
   - Authoritative for operational commerce and receipt generation.
2. **AVESTELLAR FINANCE $\rightarrow$ General Ledger**:
   - Double-entry debits and credits, accounts receivable claims, VAT payable, bank reconciliation.
   - Authoritative for financial accounting, auditability, and fiscal reporting.
3. **AVESTELLAR FINANCE $\rightarrow$ Transactional Outbox**:
   - Reliable event log guaranteed by PostgreSQL transaction boundary.
   - Immutable historical audit record.
4. **Transactional Outbox $\rightarrow$ n8n / Downstream Webhooks**:
   - HTTP delivery signed with HMAC-SHA256 and fresh Unix timestamps.
   - At-least-once delivery semantics with exponential backoff retries.
   - Delivery failures do NOT roll back accounting transactions.

---

## 4. Idempotency & Safe Replays

- **Database Unique Constraints**: `ExternalObjectMapping` enforces unique `(organization, external_system, external_type, external_id)`.
- **Outbox Idempotency**: `OutboxEvent` enforces unique `idempotency_key` per organization. Duplicate events are silently skipped.
- **Payment Settlement Safety**: A payment cannot be settled twice against an invoice; once `outstanding_balance == 0`, subsequent attempts raise clear, recoverable errors.

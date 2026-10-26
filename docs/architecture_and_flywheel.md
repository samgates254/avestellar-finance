# Architecture & Technical Specifications: AVESTELLAR FINANCE

AVESTELLAR FINANCE is an intelligent Financial Operating System combining double-entry accounting, SaaS multi-tenancy, transactional outbox messaging, MauzoPilot integration, and deterministic AI financial analysis.

---

## 1. Architectural Strategy: The Maintainable Modular Monolith

Rather than introducing distributed microservices, Kafka clusters, or premature complexity, AVESTELLAR FINANCE is architected as a high-performance **modular monolith** on **Python 3.14, Django 5.2 LTS, and PostgreSQL**.

### App Boundaries:
- `identity`: Custom user model with UUID primary keys and JWT authentication.
- `organizations`: Multi-tenant boundary, role-based memberships (OWNER, ADMIN, ACCOUNTANT, AUDITOR, VIEWER), and tenant scoping.
- `ledger`: Double-entry accounting core (Chart of Accounts, Accounting Periods, Journal Entries, Journal Lines, Posting, Reversals, Trial Balance, Income Statement, Balance Sheet).
- `invoicing`: Customers, sales invoices, line items, and receivable recognition workflows.
- `purchases`: Suppliers, vendor bills, and accounts payable.
- `payments`: Customer and supplier payments, settlement workflows, and deposit reconciliation.
- `automation`: Transactional outbox events, webhook subscriptions, delivery tracking, and idempotency store.
- `integrations`: Upstream connector adapters (MauzoPilot), canonical external object mapping, and sync logs.
- `analytics`: Deterministic cash flow forecasting, receivables aging, anomaly detection, and safe AI assistant interfaces.
- `auditlog`: Immutable audit records of all critical financial operations.

---

## 2. Multi-Tenant SaaS Isolation

Every financial query and mutation is strictly scoped to an `Organization`.

### Invariants:
1. **Never trust client-supplied organization IDs**: Clients supply `X-Organization-ID` or `X-Organization-Slug`, but `OrganizationContextMiddleware` and `get_request_organization()` verify that the authenticated user possesses an active `Membership` in that specific organization.
2. **Database isolation**: Foreign keys link every account, period, journal, invoice, and payment directly to `organization`. Querysets are explicitly filtered by `organization`.
3. **Cross-Tenant Prevention**: Users belonging to Organization A receive `403 Forbidden` or `404 Not Found` when attempting to query or modify data belonging to Organization B.

---

## 3. Mandatory Double-Entry Accounting Invariants

1. **Debit-Credit Equality**:
   A journal entry cannot be posted unless:
   $$\sum \text{Debits} = \sum \text{Credits} > 0$$
   Any violation raises `UnbalancedJournalEntryError`.

2. **Immutability of Posted Records**:
   Once a journal entry transitions to `POSTED`, neither its amounts, dates, nor line items can be modified or deleted. Attempts to delete or alter lines raise `ValidationError`.

3. **Explicit Reversal & Adjustment**:
   Corrections must use `reverse_journal_entry()`, which creates a balanced mirror entry with swapped debits/credits, posts it, and marks the original as `REVERSED`.

4. **Accounting Periods**:
   Posting into `CLOSED` or `LOCKED` periods is strictly forbidden and raises `AccountingPeriodClosedError`.

5. **Monetary Precision**:
   All monetary fields use `DecimalField(max_digits=18, decimal_places=4)`. Floating-point binary arithmetic is prohibited.

---

## 4. Transactional Outbox Pattern

To ensure business events (`invoice.issued`, `payment.confirmed`, `journal.posted`) are never lost during network failures, events are committed to `automation_outbox_events` in the **exact same database transaction** as the financial mutation.

```
BEGIN TRANSACTION;
  UPDATE invoicing_invoices SET status = 'ISSUED'...;
  INSERT INTO ledger_journal_entries ...;
  INSERT INTO automation_outbox_events (event_type, payload, status='PENDING')...;
COMMIT;
```

A background worker or management command (`python manage.py process_outbox`) polls pending outbox events, computes HMAC-SHA256 signatures, and dispatches them with retry backoff to subscribed webhooks.

---

## 5. Safe AI Financial Intelligence

The AI assistant operates on one non-negotiable rule:
> **The AI assistant NEVER performs financial arithmetic and NEVER writes directly to the general ledger.**

All numbers are calculated deterministically by tested Python domain services (`ledger.services.reporting` and `analytics.services.financial_analysis`). The AI assistant receives these verified facts and produces structured executive summaries and explanations.

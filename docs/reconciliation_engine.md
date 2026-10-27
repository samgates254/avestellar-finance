# AVESTELLAR FINANCE — Deterministic Financial Reconciliation Engine

## 1. Architectural Overview

The AVESTELLAR FINANCE Reconciliation Engine connects external statement evidence (Bank and Safaricom M-Pesa statements) with internal general ledger records, payments, and customer invoices.

### Core Architectural Principle
> **Every match must have evidence, every financial state change must be auditable, and duplicate imports must not cause duplicate accounting entries.**

Statement transactions are strictly treated as **evidence for reconciliation**—they are never automatically posted to the general ledger upon file ingestion. Accounting entries are only generated or updated when an authorized accountant confirms a match or when a verified provider callback settles an outstanding invoice through the atomic financial service layer.

```
                           +---------------------------+
                           | Statement Import Batches  |
                           | (CSV, M-Pesa, OFX, MT940) |
                           +-------------+-------------+
                                         |
                                         v
                           +-------------+-------------+
                           |   Statement Transactions  |
                           |     (Normalized Evidence) |
                           +-------------+-------------+
                                         |
         +-------------------------------+-------------------------------+
         |                               |                               |
         v                               v                               v
+------------------+           +-------------------+           +-------------------+
| Rule 1: Strong   |           | Rule 2: Amount &  |           | Rule 3: Potential |
| Reference Match  |           | Window (±3 days)  |           | (±14 days/partial)|
+--------+---------+           +---------+---------+           +---------+---------+
         |                               |                               |
         +-------------------------------+-------------------------------+
                                         |
                                         v
                         +---------------+---------------+
                         |    Accountant Review Queue    |
                         |   Candidates / Decisions      |
                         +---------------+---------------+
                                         |
                                         v
                         +---------------+---------------+
                         | Settlement & Posting Services |
                         | (Atomic GL Dr/Cr & Invoice)   |
                         +---------------+---------------+
                                         |
                                         v
                         +---------------+---------------+
                         |   Transactional Outbox Log    |
                         |  (n8n Workflows & Webhooks)   |
                         +-------------------------------+
```

---

## 2. Deterministic Matching Engine

The matching engine (`DeterministicReconciliationEngine`) runs an ordered set of rules against unmatched statement transactions. Heuristics are explicitly documented rather than masked as probabilistic black boxes.

### Rule 1: Strong Reference Match
- **Criteria**:
  - Statement transaction reference or source ID exactly matches `Payment.reference`, `Payment.payment_number`, or `Invoice.invoice_number`.
  - Transaction amounts match exactly (`abs(signed_amount) == payment.amount` or `invoice.outstanding_balance`).
  - Currency matches exactly.
  - Same organization (tenant-scoped).
- **Confidence Score**: `100` (Payment) / `95` (Invoice).
- **Category**: `HIGH`.
- **Policy**: Automatic confirmation permitted under organization policy.

### Rule 2: Exact Amount and Compatible Details
- **Criteria**:
  - Exact monetary amount match (`abs(signed_amount) == payment.amount`).
  - Compatible transaction direction: positive signed amount (inflow) matches `CUSTOMER_PAYMENT`; negative signed amount (outflow) matches `SUPPLIER_PAYMENT`.
  - Transaction date within configurable policy window (default `±3 days`).
  - Currency matches.
  - Unreconciled payment on the same financial account.
  - Narrative similarity: Counterparty name contained in description earns confidence boost.
- **Confidence Score**: `80` (base) up to `88` (with counterparty match).
- **Category**: `MEDIUM`.
- **Policy**: Automatic confirmation disabled; flagged for accountant review.

### Rule 3: Potential Match
- **Criteria**:
  - Exact amount and currency match, but transaction date falls between `4` and `14` days difference, or partial reference tokens match.
- **Confidence Score**: `50`.
- **Category**: `LOW`.
- **Policy**: Explains available evidence and missing information to accountant.

### Rule 4: Unmatched Exceptions
Transactions with no candidates, ambiguous candidates (multiple identical amount matches without references), or currency mismatches are retained in the exception queue with explicit reasons:
- `NO_CANDIDATE_FOUND`: No matching payment or invoice found.
- `MULTIPLE_CANDIDATES`: Ambiguous candidates detected; manual accountant selection required.
- `CURRENCY_MISMATCH`: Matching reference found but currencies differ without conversion rate.
- `AMOUNT_MISMATCH`: Reference matched but amounts diverge.
- `DATE_OUTSIDE_POLICY`: Candidate payment is outside permissible date threshold.
- `ALREADY_SETTLED`: Invoice or payment is already settled or reconciled.

---

## 3. Financial Integrity Safeguards

The engine enforces strict controls at the database and service layers:
1. **Reconciliation Mutex & Row Locks**: `select_for_update()` ensures concurrent reconciliation requests cannot double-match transactions or invoices.
2. **Single Settlement Invariant**: A statement transaction cannot be reconciled more than once (`reconciliation_status == 'RECONCILED'`).
3. **Payment Re-use Guard**: A payment marked `is_reconciled=True` cannot be matched to another statement line.
4. **Tenant Isolation**: All queries enforce `organization=org` constraints. Cross-organization matching is prevented by both query filters and database foreign keys.
5. **Precision**: All amounts use `Decimal` with 4 decimal places. Binary floating-point arithmetic is prohibited.
6. **Double-Entry Journal Postings**: Reconciling against an open invoice invokes `record_and_confirm_customer_payment()`, creating balanced debits to Cash/Clearing (`1010`/`1020`) and credits to Accounts Receivable (`1200`).

---

## 4. Reconciliation Sessions

A `ReconciliationSession` represents a formal accounting reconciliation period:
- **Statement Opening / Closing Balances**: Captured from imported statement files.
- **Ledger Balance**: Real-time posted general ledger balance derived from `JournalLine`s up to `period_end`.
- **Reconciliation Difference**: Calculated as `statement_closing_balance - ledger_balance`.
- **Locking & Finalization**: Finalizing locks the session. Unexplained differences prevent finalization unless explicitly overridden by an authorized finance manager (`force=True`).
- **Audit Reopening**: Reopening a finalized session requires admin permissions (`IsOrgAdminOrOwner`) and records an audit log entry.

---

## 5. Event Contracts (Transactional Outbox)

The engine persists outbox events within the same atomic database transaction:

| Event Type | Trigger | Key Payload Attributes |
|---|---|---|
| `statement.imported` | Statement file batch successfully parsed and ingested | `batch_id`, `file_name`, `account_id`, `imported_count`, `duplicate_count` |
| `statement.import_failed` | Unrecoverable parsing error | `batch_id`, `file_name`, `account_id`, `errors` |
| `reconciliation.started` | Reconciliation session created | `session_id`, `session_number`, `account_id`, `difference` |
| `reconciliation.completed` | Reconciliation session finalized | `session_id`, `session_number`, `account_id`, `difference`, `finalized_at` |
| `reconciliation.match_confirmed` | Transaction match executed | `statement_transaction_id`, `payment_id`, `invoice_id`, `rule_applied`, `confidence_score` |
| `reconciliation.exception` | Transaction marked for investigation | `statement_transaction_id`, `reason`, `amount`, `currency` |
| `payment.verification_required` | Unallocated payment received via provider callback | `payment_id`, `transaction_id`, `amount`, `payer`, `reason` |

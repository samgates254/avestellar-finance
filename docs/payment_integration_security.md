# Payment Integration Security & Verification Boundaries

This document specifies the payment security controls and verification boundaries implemented in AVESTELLAR FINANCE for Safaricom Daraja (M-Pesa) and banking rails.

---

## 1. The Core Payment Verification Boundary

In financial systems, **an HTTP callback is an advisory notification, not conclusive evidence of financial settlement.**

```
Provider HTTP Callback (C2B / STK Push)
           │
           ▼
[Replay Guard: TransID Unique Constraint]
           │
           ├─► Already Processed? ──► Return 200 OK (Idempotent ACK, no duplicate GL write)
           │
           ▼
[Shortcode & Business Context Check]
           │
           ├─► Mismatched Shortcode? ──► Return 400 Bad Request
           │
           ▼
[Invoice Matching: BillRefNumber vs Open Invoices]
           │
           ├─► Exact Match Found & Amount Matches:
           │     └─► Settle Invoice -> Post Dr 1020 Clearing, Cr 1200 AR -> Emit payment.confirmed
           │
           └─► No Match / Amount Discrepancy:
                 └─► Create Unallocated Payment -> Emit payment.verification_required
```

---

## 2. Daraja Contract Adherence

### 2.1 Why There Is No Provider Signature Header
Unlike Stripe or GitHub, Safaricom Daraja does **not** provide an HMAC signature header (such as `X-Daraja-Signature`) on C2B or STK push callbacks. 
Therefore, AVESTELLAR FINANCE enforces defense-in-depth:
1. **Shortcode Binding**: Every callback is validated against the tenant's configured shortcode (`DarajaConfig.short_code`).
2. **Database Replay Guard**: Every incoming `TransID` is recorded in `DarajaCallbackEvent`. If a duplicate `TransID` arrives, it is rejected or acknowledged idempotently without posting a second journal entry.
3. **Transaction Status API Verification**: Where high-value transactions or disputed callbacks occur, the system initiates an asynchronous inquiry via Safaricom's Transaction Status API (`/mpesa/transactionstatus/v1/query`).
4. **IP Filtering / Ingress Guard**: In production, callback URLs are protected by reverse-proxy ingress rules allowing only Safaricom's published IP ranges (196.201.214.0/24, etc.).

---

## 3. Handling Unallocated Payments & Delayed Callbacks

### 3.1 Unallocated Payments
When an M-Pesa payment arrives with an invalid account reference (e.g., customer entered `TIRES` instead of `INV-2026-001`):
1. The payment is recorded as `PaymentStatus.CONFIRMED` with `is_reconciled = False` and `customer = None`.
2. Accounting entry:
   - Debit: `1020 M-Pesa Clearing Account`
   - Credit: `2050 Unallocated Customer Receipts (Liability)`
3. Outbox event `payment.verification_required` is published to notify finance teams.
4. An accountant can subsequently match the receipt via the Bank Reconciliation UI or API (`/api/v1/payments/reconciliation/apply/`).

### 3.2 Delayed Callbacks
If a callback is delayed by network partitions or Safaricom system maintenance:
- Customer invoice remains in `ISSUED` status with outstanding balance.
- Automated reconciliation matches the M-Pesa statement import against open invoices using receipt numbers.
- Once matched, the settlement service updates the invoice and resolves the receivable without double-counting.

---

## 4. Production Go-Live Checklist

To transition from Sandbox to Live production payment processing:

- [ ] Dedicated Safaricom Daraja Production Consumer Key & Secret provisioned in Daraja Portal.
- [ ] Production Passkey generated for STK Push.
- [ ] Public HTTPS endpoint with valid SSL/TLS certificate configured for callbacks.
- [ ] C2B URLs registered using the Register URL API (`/mpesa/c2b/v1/registerurl`).
- [ ] Operator credentials for Transaction Status API configured.
- [ ] Automated test suite verifies zero float arithmetic and strict tenant scoping.
- [ ] Webhook outbox workers active with persistent retry queues.

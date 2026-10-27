# AVESTELLAR FINANCE — Safaricom Daraja Callback Boundary Integration

## 1. Overview and Security Boundary

AVESTELLAR FINANCE provides an asynchronous provider boundary for Safaricom Daraja M-Pesa integration. The boundary is designed for zero financial data loss, strict replay resistance, and seamless integration with double-entry accounting.

### Operational Security Principles
- **No Hardcoded Credentials**: Consumer keys, secrets, passkeys, and shortcodes are stored in `DarajaConfig` per tenant organization.
- **Provider Authentication**: Inbound callbacks arrive over TLS/HTTPS. Safaricom Daraja does not transmit HMAC headers on inbound webhooks; replay resistance and deduplication are maintained by recording every incoming provider event in `DarajaCallbackEvent` and validating transaction IDs against internal tables.
- **Safe Sandbox Defaults**: External network calls to Daraja servers are disabled by default until explicit sandbox/production credentials are provided. Automated tests run against mock provider adapters.

---

## 2. Supported Daraja Products & Payloads

### A. Customer-to-Business (C2B) Confirmation
Endpoint: `POST /api/v1/integrations/daraja/c2b-confirmation/`

**Payload Contract**:
```json
{
  "TransactionType": "Pay Bill",
  "TransID": "QKH1234567",
  "TransTime": "20260315143210",
  "TransAmount": "15000.00",
  "BusinessShortCode": "600000",
  "BillRefNumber": "INV-2026-001",
  "InvoiceNumber": "",
  "OrgAccountBalance": "125000.00",
  "ThirdPartyTransID": "",
  "MSISDN": "254712345678",
  "FirstName": "John",
  "LastName": "Doe"
}
```

**Processing Logic**:
1. Record callback payload in `DarajaCallbackEvent`.
2. **Replay Protection**: If `(organization, C2B_CONFIRMATION, TransID)` already exists in `PROCESSED` state, return standard HTTP 200 acknowledgment `{"ResultCode": "0", "ResultDesc": "Duplicate confirmation ignored"}` without re-posting.
3. If `BillRefNumber` matches an outstanding `Invoice`, automatically settle via `record_and_confirm_customer_payment()`:
   - Debit: M-Pesa Clearing Account (`1020`)
   - Credit: Accounts Receivable (`1200`)
   - Status: Invoice marked `PAID` or `PARTIALLY_PAID`
   - Outbox: Emits `payment.confirmed`
4. If no open invoice matches `BillRefNumber`:
   - Create unallocated `Payment` with status `PENDING`.
   - Outbox: Emits `payment.verification_required` for accountant review.

---

### B. Customer-to-Business (C2B) Validation
Endpoint: `POST /api/v1/integrations/daraja/c2b-validation/`

Validates that an account or bill reference is eligible to receive payment:
```json
{
  "ResultCode": "0",
  "ResultDesc": "Accepted: Valid account reference INV-2026-001"
}
```

---

### C. Lipa Na M-Pesa Online (STK Push Callback)
Endpoint: `POST /api/v1/integrations/daraja/stk-callback/`

**Payload Contract**:
```json
{
  "Body": {
    "stkCallback": {
      "MerchantRequestID": "29115-34620561-1",
      "CheckoutRequestID": "ws_CO_19122026102036404",
      "ResultCode": 0,
      "ResultDesc": "The service request is processed successfully.",
      "CallbackMetadata": {
        "Item": [
          {"Name": "Amount", "Value": 4500.00},
          {"Name": "MpesaReceiptNumber", "Value": "QKH9988776"},
          {"Name": "TransactionDate", "Value": 20260315154500},
          {"Name": "PhoneNumber", "Value": 254712345678}
        ]
      }
    }
  }
}
```

**Processing Logic**:
- If `ResultCode == 0`: Updates pending payment to `CONFIRMED`, assigns M-Pesa receipt number, posts accounting journal, and emits `payment.confirmed`.
- If `ResultCode != 0` (e.g. `1032` Request cancelled by user): Marks `DarajaCallbackEvent` as `FAILED` with descriptive error, leaving invoice open.

---

### D. Transaction Status Inquiry API
Endpoint: `POST /api/v1/integrations/daraja/verify-status/`

Queries Daraja Transaction Status API for payment verification and delayed confirmation resolution:
```json
{
  "transaction_id": "QKH1234567",
  "short_code": "600000"
}
```

**Response**:
```json
{
  "ResponseCode": "0",
  "ResultCode": "0",
  "TransactionStatus": "Completed",
  "ReceiptNo": "QKH1234567",
  "Amount": "15000.00"
}
```

---

## 3. Production Readiness and Integration Status

| Component | Status | Verification Detail |
|---|---|---|
| **C2B Webhook Confirmation** | Fully Implemented & Tested | Automated tests verified against synthetic Daraja payloads, replay protection, and invoice settlement. |
| **C2B Validation** | Fully Implemented & Tested | Automated tests verify account reference validation. |
| **STK Push Callback Handler** | Fully Implemented & Tested | Handles both successful completions and user-cancelled failures. |
| **Transaction Status Query** | Mock Adapter Tested | Mocked boundary tested; live HTTP calls disabled pending official production sandbox credentials. |
| **Live Money Movement** | Disabled by Default | No real funds transferred. External credentials must be configured in `DarajaConfig`. |

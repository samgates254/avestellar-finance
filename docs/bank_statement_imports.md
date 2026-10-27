# AVESTELLAR FINANCE — Bank Statement Imports Subsystem

## 1. Supported File Formats

AVESTELLAR FINANCE provides explicit parser adapters with validated input contracts:

1. **Bank CSV (`BANK_CSV`)**: General delimiter-separated statements with configurable column mappings.
2. **Open Financial Exchange (`OFX`)**: Standard SGML and XML bank statement exports (versions 1.x and 2.x).
3. **SWIFT MT940 (`MT940`)**: Standard SWIFT customer statement format.

---

## 2. Cryptographic Batch Deduplication

Repeated file uploads must not produce duplicate statement transactions. 
- Each uploaded file is hashed using **SHA-256**:
  $$\text{hash} = \text{SHA-256}(\text{file\_content})$$
- A database constraint on `(organization, financial_account, file_hash)` rejects re-importing the identical file for the same account:
  ```json
  {
    "detail": "File 'bank_march.csv' (SHA-256: d2e81fa...) has already been imported for account 'Operating Bank Account'."
  }
  ```

---

## 3. Bank CSV Column Mapping

The CSV parser supports both automatic heuristic mapping and explicit custom column overrides.

### Column Configuration Schema
```json
{
  "financial_account_id": "8435479b-7b2b-41ec-bc44-d6b6a57abb1d",
  "import_source": "BANK_CSV",
  "file_name": "march_statement.csv",
  "column_mapping": {
    "date": "Booking Date",
    "value_date": "Value Date",
    "description": "Narrative",
    "reference": "Document Number",
    "debit": "Debit Amount",
    "credit": "Credit Amount",
    "amount": "Signed Amount",
    "balance": "Running Balance",
    "currency": "Currency"
  },
  "date_format": "%d/%m/%Y"
}
```

### Amount Resolution and Invariant Checks
1. **Separate Debit and Credit Columns**:
   - Inflow (Credit from bank perspective): `signed_amount = +credit_amount`
   - Outflow (Debit from bank perspective): `signed_amount = -debit_amount`
   - **Conflicting Row Protection**: If both `debit` and `credit` are positive on the same row, the row is rejected with an actionable error.
2. **Single Signed Amount Column**:
   - Positive indicates inflow; negative indicates outflow.
   - Zero-amount rows are flagged and rejected.

---

## 4. Monetary and Date Parsing Rules

### Monetary Formats (`clean_decimal`)
- Binary floating-point arithmetic is strictly prohibited; all values are parsed into `Decimal`.
- Strips non-numeric characters, currency prefixes (`KES`, `$`, `EUR`), and thousands commas.
- Supports accounting parentheses: `(1,500.00)` $\rightarrow$ `-1500.0000`.
- Detects European decimal comma notation: `1.234,50` $\rightarrow$ `1234.5000`.

### Date Formats (`parse_date`)
Supports standard accounting formats:
- ISO: `YYYY-MM-DD` and `YYYY-MM-DD HH:MM:SS`
- British / Commonwealth: `DD/MM/YYYY` and `DD-MM-YYYY`
- American: `MM/DD/YYYY`
- Compact: `YYYYMMDD`

---

## 5. Transaction Deduplication Strategy

When a file spans overlapping dates with an earlier import, line deduplication is applied:
1. **Upstream Source Transaction ID**: If present (e.g. FITID in OFX, M-Pesa receipt in CSV), uniqueness is enforced against `(organization, financial_account, source_transaction_id)`.
2. **Composite Fingerprint**: For bank lines without unique external IDs, a deterministic SHA-256 fingerprint is calculated:
   $$\text{Fingerprint} = \text{SHA-256}(\text{account\_id} : \text{date} : \text{reference} : \text{signed\_amount} : \text{currency})$$
3. Genuine repeated transactions of the same amount with distinct references or dates are preserved. Duplicate lines are excluded, and `duplicate_count` is incremented in the batch summary.

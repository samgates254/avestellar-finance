# AVESTELLAR FINANCE — Safaricom M-Pesa Statement Reconciliation

## 1. M-Pesa Statement Export Structure

Safaricom M-Pesa corporate statements (e.g. Paybill and Buy Goods statements) export as CSV files often containing preamble metadata lines before the primary transaction table:

```csv
Safaricom M-Pesa Statement
Account Name: AVESTELLAR CORP
Short Code: 600100
Period: 01-Mar-2026 to 31-Mar-2026

Receipt No.,Completion Time,Details,Transaction Status,Paid In,Withdrawn,Balance,Other Party Info
QKH1234567,2026-03-15 14:32:10,Pay Utility to 600100 - Inv INV-2026-001,Completed,15000.00,,115000.00,254712345678 - John Doe
QKH1234568,2026-03-15 14:32:11,Paybill Charge,Completed,,110.00,114890.00,Safaricom
QKH1234569,2026-03-16 10:15:00,Business Payment to Supplier XYZ,Completed,,50000.00,64890.00,Supplier XYZ
QKH1234570,2026-03-17 11:00:00,Reversal of QKH1234567,Completed,,15000.00,49890.00,Customer Support
```

### Automatic Header Detection
The `MpesaStatementParser` automatically scans and skips metadata preambles by locating the header row containing signature indicators: `Receipt No.`, `Completion Time`, `Paid In`, and `Withdrawn`.

---

## 2. Transaction Classification

M-Pesa exports contain a diverse mix of business flows. The parser maps each row into an explicit `StatementTransactionClassification`:

| Flow | Indicators | Classification | Financial Direction |
|---|---|---|---|
| **Customer Paybill Collection** | `Paid In > 0`, "Pay Utility", "Customer Transfer", "Merchant Payment" | `CUSTOMER_PAYMENT` | Positive signed amount (Cash Inflow) |
| **Paybill Tariff / Charge** | `Withdrawn > 0`, "Charge", "Tariff", "Fee", "Excise" | `BANK_FEE` | Negative signed amount (Fee Outflow) |
| **B2C Disbursement** | `Withdrawn > 0`, "Business Payment", "Salary", "Disbursement" | `SUPPLIER_PAYMENT` | Negative signed amount (Cash Outflow) |
| **Reversal** | "Reversal of" in details or narrative | `REVERSAL` | Direction depends on whether original transaction was inflow or outflow |
| **Refund** | "Refund" in narrative | `REFUND` | Cash Outflow or Inflow |
| **Account Transfer** | "Transfer to", "Transfer from" | `TRANSFER` | Clearing account transfer |

---

## 3. Account Hierarchy & Clearing Rules

### M-Pesa Clearing Account (`1020`)
M-Pesa receipts are initially posted to an M-Pesa Clearing Account (`1020`) rather than the operating bank account (`1010`):
1. **Direct Inflow**:
   - $\text{Dr}\quad\text{M-Pesa Clearing Account (1020)}$
   - $\text{Cr}\quad\text{Accounts Receivable (1200)}$
2. **Safaricom Paybill Fee**:
   - $\text{Dr}\quad\text{Bank and M-Pesa Fees (5200)}$
   - $\text{Cr}\quad\text{M-Pesa Clearing Account (1020)}$
3. **Settlement Sweep into Bank**:
   - When funds are swept from the M-Pesa Paybill into the commercial bank account:
   - $\text{Dr}\quad\text{Operating Bank Account (1010)}$
   - $\text{Cr}\quad\text{M-Pesa Clearing Account (1020)}$

This distinction ensures verified available funds in the bank account are never conflated with uncleared funds held in provider float.

"""Safaricom M-Pesa CSV statement export parser adapter."""

import csv
import io
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from payments.models import StatementTransactionClassification
from payments.services.statement_parsers.bank_csv import clean_decimal, parse_date
from payments.services.statement_parsers.base import (
    BaseStatementParser,
    ParsedTransaction,
    StatementParseResult,
)


class MpesaStatementParser(BaseStatementParser):
    """Parser specifically tuned for Safaricom M-Pesa corporate/paybill statement exports."""

    HEADER_INDICATORS = ["receipt no", "completion time", "transaction date", "paid in", "withdrawn"]

    def __init__(self, default_currency: str = "KES"):
        self.default_currency = default_currency

    def _find_header_line(self, lines: list[str]) -> int:
        """Finds the index of the header line, skipping preamble metadata."""
        for idx, line in enumerate(lines):
            line_lower = line.lower()
            matches = sum(1 for indicator in self.HEADER_INDICATORS if indicator in line_lower)
            if matches >= 2:
                return idx
        return 0

    def parse(self, content: str | bytes, **kwargs) -> StatementParseResult:
        if isinstance(content, bytes):
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = content.decode("latin-1")
        else:
            text = content

        raw_lines = [l for l in text.splitlines() if l.strip()]
        if not raw_lines:
            return StatementParseResult(
                opening_balance=None,
                closing_balance=None,
                currency=self.default_currency,
                errors=[{"row": 0, "error": "M-Pesa statement file is empty."}],
            )

        header_idx = self._find_header_line(raw_lines)
        csv_payload = "\n".join(raw_lines[header_idx:])

        reader = csv.DictReader(io.StringIO(csv_payload))
        if not reader.fieldnames:
            return StatementParseResult(
                opening_balance=None,
                closing_balance=None,
                currency=self.default_currency,
                errors=[{"row": 0, "error": "Unable to detect valid M-Pesa table headers."}],
            )

        # Normalize field names map
        header_map: dict[str, str] = {}
        for fn in reader.fieldnames:
            if not fn:
                continue
            fn_clean = fn.strip().lower()
            if "receipt" in fn_clean:
                header_map["receipt_no"] = fn
            elif "completion" in fn_clean or "trans time" in fn_clean or "txn date" in fn_clean:
                header_map["completion_time"] = fn
            elif "detail" in fn_clean or "narrative" in fn_clean or "description" in fn_clean:
                header_map["details"] = fn
            elif "status" in fn_clean:
                header_map["status"] = fn
            elif "paid in" in fn_clean or "amount in" in fn_clean or "credit" in fn_clean:
                header_map["paid_in"] = fn
            elif "withdrawn" in fn_clean or "amount out" in fn_clean or "debit" in fn_clean:
                header_map["withdrawn"] = fn
            elif "balance" in fn_clean and "confirmed" not in fn_clean:
                header_map["balance"] = fn
            elif "other party" in fn_clean or "counterparty" in fn_clean:
                header_map["counterparty"] = fn
            elif "reason" in fn_clean or "type" in fn_clean:
                header_map["reason_type"] = fn

        transactions: list[ParsedTransaction] = []
        errors: list[dict[str, Any]] = []
        total_rows = 0
        rejected_rows = 0

        opening_balance: Decimal | None = None
        closing_balance: Decimal | None = None

        for row_idx, row in enumerate(reader, start=1):
            if not any(v and v.strip() for v in row.values() if v is not None):
                continue

            total_rows += 1
            row_clean = {k.strip(): (v.strip() if v else "") for k, v in row.items() if k is not None}

            receipt_no = row_clean.get(header_map.get("receipt_no", ""), "").strip()
            time_str = row_clean.get(header_map.get("completion_time", ""), "").strip()
            details = row_clean.get(header_map.get("details", ""), "").strip()
            status_val = row_clean.get(header_map.get("status", ""), "").strip()
            paid_in_str = row_clean.get(header_map.get("paid_in", ""), "")
            withdrawn_str = row_clean.get(header_map.get("withdrawn", ""), "")
            balance_str = row_clean.get(header_map.get("balance", ""), "")
            counterparty = row_clean.get(header_map.get("counterparty", ""), "").strip()
            reason_type = row_clean.get(header_map.get("reason_type", ""), "").strip()

            if not receipt_no:
                errors.append({"row": row_idx, "error": "Missing M-Pesa receipt number."})
                rejected_rows += 1
                continue

            if not time_str:
                errors.append({"row": row_idx, "error": "Missing transaction completion timestamp."})
                rejected_rows += 1
                continue

            try:
                txn_date = parse_date(time_str)
            except ValueError as exc:
                errors.append({"row": row_idx, "error": f"Invalid M-Pesa date/time: {exc}"})
                rejected_rows += 1
                continue

            # In Safaricom M-Pesa statement:
            # Paid In = customer inflow (credit)
            # Withdrawn = business disbursement or fee (debit)
            paid_in = Decimal("0.0000")
            withdrawn = Decimal("0.0000")
            try:
                if paid_in_str:
                    paid_in = clean_decimal(paid_in_str)
                if withdrawn_str:
                    withdrawn = clean_decimal(withdrawn_str)
            except ValueError as exc:
                errors.append({"row": row_idx, "error": f"Invalid amount: {exc}"})
                rejected_rows += 1
                continue

            if paid_in > Decimal("0") and withdrawn > Decimal("0"):
                errors.append({
                    "row": row_idx,
                    "error": f"Conflicting M-Pesa line: both Paid In ({paid_in}) and Withdrawn ({withdrawn}) are positive."
                })
                rejected_rows += 1
                continue

            if paid_in == Decimal("0") and withdrawn == Decimal("0"):
                errors.append({"row": row_idx, "error": "Transaction amount is zero."})
                rejected_rows += 1
                continue

            if paid_in > Decimal("0"):
                signed_amount = paid_in
                debit_amt = Decimal("0.0000")
                credit_amt = paid_in
            else:
                signed_amount = -withdrawn
                debit_amt = withdrawn
                credit_amt = Decimal("0.0000")

            running_bal = None
            if balance_str:
                try:
                    running_bal = clean_decimal(balance_str)
                    if opening_balance is None:
                        opening_balance = running_bal - signed_amount
                    closing_balance = running_bal
                except ValueError:
                    pass

            # Classify transaction
            combined_text = f"{details} {reason_type}".lower()
            if "charge" in combined_text or "fee" in combined_text or "tariff" in combined_text:
                classification = StatementTransactionClassification.BANK_FEE
            elif "reversal" in combined_text:
                classification = StatementTransactionClassification.REVERSAL
            elif "refund" in combined_text:
                classification = StatementTransactionClassification.REFUND
            elif "transfer" in combined_text:
                classification = StatementTransactionClassification.TRANSFER
            elif signed_amount > Decimal("0"):
                classification = StatementTransactionClassification.CUSTOMER_PAYMENT
            else:
                classification = StatementTransactionClassification.SUPPLIER_PAYMENT

            full_desc = details or f"M-Pesa {receipt_no}"
            if counterparty and counterparty not in full_desc:
                full_desc = f"{full_desc} - {counterparty}"

            parsed_trx = ParsedTransaction(
                source_transaction_id=receipt_no,
                transaction_date=txn_date,
                value_date=txn_date,
                description=full_desc,
                reference=receipt_no,
                debit_amount=debit_amt,
                credit_amount=credit_amt,
                signed_amount=signed_amount,
                currency=self.default_currency,
                running_balance=running_bal,
                classification=classification,
                raw_metadata={
                    "receipt_no": receipt_no,
                    "completion_time": time_str,
                    "details": details,
                    "status": status_val,
                    "counterparty": counterparty,
                    "reason_type": reason_type,
                    "raw_row": row_clean,
                },
            )
            transactions.append(parsed_trx)

        return StatementParseResult(
            opening_balance=opening_balance,
            closing_balance=closing_balance,
            currency=self.default_currency,
            transactions=transactions,
            errors=errors,
            total_rows=total_rows,
            rejected_rows=rejected_rows,
        )

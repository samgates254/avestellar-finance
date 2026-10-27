"""Standard bank CSV statement parser adapter with configurable column mappings."""

import csv
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from payments.models import StatementTransactionClassification
from payments.services.statement_parsers.base import (
    BaseStatementParser,
    ParsedTransaction,
    StatementParseResult,
)


def clean_decimal(value: Any, default: Decimal = Decimal("0.0000")) -> Decimal:
    """Safely converts string or numeric input into a Decimal with 4 decimal places.

    Strips thousands separators, currency symbols, and whitespace.
    """
    if value is None:
        return default
    val_str = str(value).strip()
    if not val_str or val_str in ("-", "--", "N/A", "null"):
        return default

    # Remove currency symbols and non-numeric characters except '-', '.', and ','
    val_str = re.sub(r"[^\d.,\-+()]", "", val_str)

    # Handle parentheses for negative accounting format (e.g. (1,234.50) -> -1234.50)
    if val_str.startswith("(") and val_str.endswith(")"):
        val_str = "-" + val_str[1:-1]

    # Handle European decimal formatting where comma is decimal separator (e.g., 1.234,50)
    if "," in val_str and "." in val_str:
        if val_str.rfind(",") > val_str.rfind("."):
            val_str = val_str.replace(".", "").replace(",", ".")
        else:
            val_str = val_str.replace(",", "")
    elif "," in val_str and "." not in val_str:
        # Check if comma is used as decimal separator (e.g. 1500,50)
        parts = val_str.split(",")
        if len(parts) == 2 and len(parts[1]) <= 3:
            val_str = val_str.replace(",", ".")
        else:
            val_str = val_str.replace(",", "")

    try:
        return Decimal(val_str).quantize(Decimal("0.0001"))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid monetary format '{value}': cannot parse as Decimal.") from exc


def parse_date(date_str: str, custom_format: str | None = None) -> date:
    """Parses date string using specified or common accounting formats."""
    val = date_str.strip()
    if not val:
        raise ValueError("Transaction date cannot be empty.")

    if custom_format:
        try:
            return datetime.strptime(val, custom_format).date()
        except ValueError:
            pass

    formats = [
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%m/%d/%Y",
        "%d-%m-%Y",
        "%Y/%m/%d",
        "%Y%m%d",
        "%d.%m.%Y",
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(val, fmt).date()
        except ValueError:
            continue

    raise ValueError(f"Unable to parse date string '{val}'.")


class BankCsvParser(BaseStatementParser):
    """Configurable bank CSV statement parser."""

    DEFAULT_MAPPINGS = {
        "date": ["date", "transaction date", "txn date", "trans date", "booking date"],
        "value_date": ["value date", "val date", "effective date"],
        "description": ["description", "narrative", "details", "particulars", "memo", "remarks"],
        "reference": ["reference", "ref", "ref no", "reference number", "cheque no", "chq no", "txn id"],
        "debit": ["debit", "debit amount", "withdrawal", "withdrawals", "dr", "paid out"],
        "credit": ["credit", "credit amount", "deposit", "deposits", "cr", "paid in"],
        "amount": ["amount", "txn amount", "net amount", "signed amount"],
        "balance": ["balance", "running balance", "closing balance", "ledger balance"],
        "currency": ["currency", "ccy"],
    }

    def __init__(self, column_mapping: dict[str, str] | None = None, default_currency: str = "KES", date_format: str | None = None):
        self.column_mapping = column_mapping or {}
        self.default_currency = default_currency
        self.date_format = date_format

    def _resolve_column(self, row: dict[str, str], field_key: str) -> str | None:
        """Finds the column value based on custom mapping or known alias lists."""
        # 1. Custom explicit mapping
        if field_key in self.column_mapping:
            custom_col = self.column_mapping[field_key]
            if custom_col in row:
                return row[custom_col]
            # Case-insensitive match on custom column
            for k, v in row.items():
                if k.strip().lower() == custom_col.strip().lower():
                    return v

        # 2. Heuristic alias match
        aliases = self.DEFAULT_MAPPINGS.get(field_key, [])
        for k, v in row.items():
            if k.strip().lower() in aliases:
                return v

        return None

    def parse(self, content: str | bytes, **kwargs) -> StatementParseResult:
        if isinstance(content, bytes):
            # Detect BOM or fallback
            try:
                text = content.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = content.decode("latin-1")
        else:
            text = content

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return StatementParseResult(
                opening_balance=None,
                closing_balance=None,
                currency=self.default_currency,
                errors=[{"row": 0, "error": "CSV file contains no headers or is empty."}],
            )

        transactions: list[ParsedTransaction] = []
        errors: list[dict[str, Any]] = []
        total_rows = 0
        rejected_rows = 0

        opening_balance: Decimal | None = None
        closing_balance: Decimal | None = None

        for row_idx, row in enumerate(reader, start=1):
            # Skip completely empty rows
            if not any(v and v.strip() for v in row.values() if v is not None):
                continue

            total_rows += 1
            row_clean = {k.strip(): (v.strip() if v else "") for k, v in row.items() if k is not None}

            date_val = self._resolve_column(row_clean, "date")
            desc_val = self._resolve_column(row_clean, "description") or ""
            ref_val = self._resolve_column(row_clean, "reference") or ""
            val_date_val = self._resolve_column(row_clean, "value_date")
            curr_val = self._resolve_column(row_clean, "currency") or self.default_currency
            bal_val = self._resolve_column(row_clean, "balance")

            # Check date
            if not date_val:
                errors.append({"row": row_idx, "error": "Missing mandatory transaction date."})
                rejected_rows += 1
                continue

            try:
                txn_date = parse_date(date_val, self.date_format)
            except ValueError as exc:
                errors.append({"row": row_idx, "error": f"Invalid date: {exc}"})
                rejected_rows += 1
                continue

            val_date = None
            if val_date_val:
                try:
                    val_date = parse_date(val_date_val, self.date_format)
                except ValueError:
                    pass

            # Resolve amounts
            debit_str = self._resolve_column(row_clean, "debit")
            credit_str = self._resolve_column(row_clean, "credit")
            amount_str = self._resolve_column(row_clean, "amount")

            debit_amt = Decimal("0.0000")
            credit_amt = Decimal("0.0000")
            signed_amt = Decimal("0.0000")

            has_debit = debit_str is not None and debit_str.strip() not in ("", "-", "0", "0.00")
            has_credit = credit_str is not None and credit_str.strip() not in ("", "-", "0", "0.00")
            has_amount = amount_str is not None and amount_str.strip() not in ("", "-")

            try:
                if has_debit or has_credit:
                    if has_debit:
                        debit_amt = clean_decimal(debit_str)
                    if has_credit:
                        credit_amt = clean_decimal(credit_str)

                    # In financial cash accounting:
                    # Inflow to bank / Cash receipt = credit from bank perspective = +signed_amount
                    # Outflow from bank / Cash payment = debit from bank perspective = -signed_amount
                    if debit_amt > Decimal("0") and credit_amt > Decimal("0"):
                        # Ambiguous row: both debit and credit present
                        errors.append({
                            "row": row_idx,
                            "error": f"Conflicting amounts: both debit ({debit_amt}) and credit ({credit_amt}) are positive."
                        })
                        rejected_rows += 1
                        continue

                    if credit_amt > Decimal("0"):
                        signed_amt = credit_amt
                    elif debit_amt > Decimal("0"):
                        signed_amt = -debit_amt
                    else:
                        errors.append({"row": row_idx, "error": "Transaction amount is zero."})
                        rejected_rows += 1
                        continue

                elif has_amount:
                    parsed_amt = clean_decimal(amount_str)
                    if parsed_amt == Decimal("0"):
                        errors.append({"row": row_idx, "error": "Transaction amount cannot be zero."})
                        rejected_rows += 1
                        continue

                    signed_amt = parsed_amt
                    if signed_amt > Decimal("0"):
                        credit_amt = signed_amt
                        debit_amt = Decimal("0.0000")
                    else:
                        debit_amt = abs(signed_amt)
                        credit_amt = Decimal("0.0000")
                else:
                    errors.append({"row": row_idx, "error": "No debit, credit, or amount column found in row."})
                    rejected_rows += 1
                    continue

            except ValueError as exc:
                errors.append({"row": row_idx, "error": f"Monetary parsing error: {exc}"})
                rejected_rows += 1
                continue

            running_bal = None
            if bal_val:
                try:
                    running_bal = clean_decimal(bal_val)
                    if opening_balance is None:
                        # Estimate opening balance
                        opening_balance = running_bal - signed_amt
                    closing_balance = running_bal
                except ValueError:
                    pass

            # Classification heuristics
            classification = StatementTransactionClassification.UNCLASSIFIED
            desc_lower = desc_val.lower()
            if any(w in desc_lower for w in ["charge", "fee", "tariff", "commission", "excise", "vat on fee"]):
                classification = StatementTransactionClassification.BANK_FEE
            elif any(w in desc_lower for w in ["interest", "int paid", "int credit"]):
                classification = StatementTransactionClassification.INTEREST
            elif any(w in desc_lower for w in ["transfer to", "transfer from", "xfer", "internal trf"]):
                classification = StatementTransactionClassification.TRANSFER
            elif signed_amt > Decimal("0"):
                classification = StatementTransactionClassification.CUSTOMER_PAYMENT
            else:
                classification = StatementTransactionClassification.SUPPLIER_PAYMENT

            parsed_trx = ParsedTransaction(
                source_transaction_id="",
                transaction_date=txn_date,
                value_date=val_date,
                description=desc_val,
                reference=ref_val,
                debit_amount=debit_amt,
                credit_amount=credit_amt,
                signed_amount=signed_amt,
                currency=curr_val[:3].upper(),
                running_balance=running_bal,
                classification=classification,
                raw_metadata=row_clean,
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

"""SWIFT MT940 statement parser adapter."""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from payments.models import StatementTransactionClassification
from payments.services.statement_parsers.bank_csv import clean_decimal
from payments.services.statement_parsers.base import (
    BaseStatementParser,
    ParsedTransaction,
    StatementParseResult,
)


def parse_mt940_date(yymmdd: str) -> date:
    """Parses 6-digit MT940 date string YYMMDD (assumes 2000s century)."""
    clean = yymmdd.strip()
    if len(clean) != 6 or not clean.isdigit():
        raise ValueError(f"Invalid MT940 date format '{yymmdd}', expected YYMMDD.")
    return datetime.strptime("20" + clean, "%Y%m%d").date()


class Mt940StatementParser(BaseStatementParser):
    """Parser for SWIFT MT940 customer bank statement messages."""

    def __init__(self, default_currency: str = "KES"):
        self.default_currency = default_currency

    def _parse_balance_tag(self, tag_content: str) -> tuple[Decimal, str, date]:
        """Parses tag :60F: or :62F: -> (signed_balance, currency, date).

        Format: [C/D][YYMMDD][CURRENCY 3 chars][AMOUNT with comma]
        """
        match = re.match(r"^([CD])(\d{6})([A-Z]{3})([\d,]+)", tag_content.strip())
        if not match:
            raise ValueError(f"Invalid balance tag format: '{tag_content}'")
        dc_mark, date_str, ccy, amt_str = match.groups()
        b_date = parse_mt940_date(date_str)
        b_amt = clean_decimal(amt_str)
        if dc_mark == "D":
            b_amt = -b_amt
        return b_amt, ccy, b_date

    def parse(self, content: str | bytes, **kwargs) -> StatementParseResult:
        if isinstance(content, bytes):
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                text = content.decode("latin-1")
        else:
            text = content

        if not text.strip():
            return StatementParseResult(
                opening_balance=None,
                closing_balance=None,
                currency=self.default_currency,
                errors=[{"row": 0, "error": "MT940 content is empty."}],
            )

        opening_balance: Decimal | None = None
        closing_balance: Decimal | None = None
        currency = self.default_currency

        # Find opening balance :60F:
        match_60 = re.search(r":60[FM]:([^\r\n:]+)", text)
        if match_60:
            try:
                opening_balance, cur, _ = self._parse_balance_tag(match_60.group(1))
                currency = cur
            except ValueError:
                pass

        # Find closing balance :62F:
        match_62 = re.search(r":62[FM]:([^\r\n:]+)", text)
        if match_62:
            try:
                closing_balance, cur, _ = self._parse_balance_tag(match_62.group(1))
                currency = cur
            except ValueError:
                pass

        # Parse statement lines :61: followed optionally by :86: narrative
        # A single :61: line may have multi-line :86:
        pattern = re.compile(
            r":61:([^\r\n]+)(?:\r?\n:86:((?:(?!:(?:61|62|20|25):)[\s\S])*))?",
            re.MULTILINE,
        )

        matches = list(pattern.finditer(text))
        transactions: list[ParsedTransaction] = []
        errors: list[dict[str, Any]] = []
        total_rows = len(matches)
        rejected_rows = 0

        for idx, match in enumerate(matches, start=1):
            line_61 = match.group(1).strip()
            memo_86 = match.group(2).strip() if match.group(2) else ""
            memo_86 = re.sub(r"\r?\n", " ", memo_86).strip()

            # :61: parsing
            # Format: YYMMDD[MMDD]D/C[F]AmountN[TransType]Reference[//BankReference]
            m61 = re.match(
                r"^(\d{6})(?:\d{4})?([R]?[CD])([A-Z])?([\d,]+)(?:N[A-Z0-9]{3,4})?(.*?)(?://(.*))?$",
                line_61,
            )
            if not m61:
                errors.append({"row": idx, "error": f"Malformed :61: statement line: '{line_61}'"})
                rejected_rows += 1
                continue

            date_str, dc_ind, fund_code, amt_str, ref_str, bank_ref = m61.groups()

            try:
                txn_date = parse_mt940_date(date_str)
            except ValueError as exc:
                errors.append({"row": idx, "error": f"Invalid date in :61: {exc}"})
                rejected_rows += 1
                continue

            try:
                base_amt = clean_decimal(amt_str)
            except ValueError as exc:
                errors.append({"row": idx, "error": f"Invalid amount in :61: {exc}"})
                rejected_rows += 1
                continue

            if base_amt == Decimal("0.0000"):
                errors.append({"row": idx, "error": "Transaction amount is zero."})
                rejected_rows += 1
                continue

            is_credit = "C" in dc_ind.upper()
            if is_credit:
                signed_amount = base_amt
                credit_amt = base_amt
                debit_amt = Decimal("0.0000")
            else:
                signed_amount = -base_amt
                debit_amt = base_amt
                credit_amt = Decimal("0.0000")

            reference = (bank_ref or ref_str or "").strip()
            # If reference is NONREF, clear it
            if reference.upper() == "NONREF":
                reference = ""

            description = memo_86 or reference or f"MT940 Transaction {date_str}"

            # Classification
            desc_lower = description.lower()
            if any(w in desc_lower for w in ["charge", "fee", "commission"]):
                classification = StatementTransactionClassification.BANK_FEE
            elif "interest" in desc_lower:
                classification = StatementTransactionClassification.INTEREST
            elif "transfer" in desc_lower:
                classification = StatementTransactionClassification.TRANSFER
            elif signed_amount > Decimal("0"):
                classification = StatementTransactionClassification.CUSTOMER_PAYMENT
            else:
                classification = StatementTransactionClassification.SUPPLIER_PAYMENT

            parsed_trx = ParsedTransaction(
                source_transaction_id=bank_ref.strip() if bank_ref else "",
                transaction_date=txn_date,
                value_date=txn_date,
                description=description,
                reference=reference,
                debit_amount=debit_amt,
                credit_amount=credit_amt,
                signed_amount=signed_amount,
                currency=currency[:3].upper(),
                running_balance=None,
                classification=classification,
                raw_metadata={
                    "line_61": line_61,
                    "tag_86": memo_86,
                    "bank_reference": bank_ref or "",
                },
            )
            transactions.append(parsed_trx)

        return StatementParseResult(
            opening_balance=opening_balance,
            closing_balance=closing_balance,
            currency=currency[:3].upper(),
            transactions=transactions,
            errors=errors,
            total_rows=total_rows,
            rejected_rows=rejected_rows,
        )

"""Open Financial Exchange (OFX) statement parser adapter supporting OFX 1.x and 2.x."""

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


def parse_ofx_date(date_str: str) -> date:
    """Parses OFX date format (e.g. 20260315 or 20260315120000[-5:EST])."""
    clean_date = date_str.split("[")[0].strip()
    if len(clean_date) >= 8:
        return datetime.strptime(clean_date[:8], "%Y%m%d").date()
    raise ValueError(f"Invalid OFX date string '{date_str}'.")


class OfxStatementParser(BaseStatementParser):
    """Parser for Open Financial Exchange (OFX) bank statements."""

    def __init__(self, default_currency: str = "KES"):
        self.default_currency = default_currency

    def _extract_tag_value(self, block: str, tag: str) -> str:
        """Extracts value for a given tag from an XML or SGML block."""
        # Match XML: <TAG>value</TAG> or SGML: <TAG>value\n
        pattern = rf"<{tag}>([^<\r\n]+)(?:</{tag}>)?"
        match = re.search(pattern, block, re.IGNORECASE)
        return match.group(1).strip() if match else ""

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
                errors=[{"row": 0, "error": "OFX content is empty."}],
            )

        currency = self._extract_tag_value(text, "CURDEF") or self.default_currency

        # Closing balance from LEDGERBAL
        closing_balance: Decimal | None = None
        ledger_bal_match = re.search(r"<LEDGERBAL>.*?</LEDGERBAL>", text, re.IGNORECASE | re.DOTALL)
        if ledger_bal_match:
            bal_amt_str = self._extract_tag_value(ledger_bal_match.group(0), "BALAMT")
            if bal_amt_str:
                try:
                    closing_balance = clean_decimal(bal_amt_str)
                except ValueError:
                    pass
        else:
            # Check SGML unclosed LEDGERBAL
            bal_amt_str = self._extract_tag_value(text, "BALAMT")
            if bal_amt_str:
                try:
                    closing_balance = clean_decimal(bal_amt_str)
                except ValueError:
                    pass

        # Extract transaction blocks: <STMTTRN>...</STMTTRN> or <STMTTRN> to next tag
        trn_blocks = re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>)|(?=</CCSTMTTRN>)|$)", text, re.IGNORECASE | re.DOTALL)

        transactions: list[ParsedTransaction] = []
        errors: list[dict[str, Any]] = []
        total_rows = len(trn_blocks)
        rejected_rows = 0

        for idx, block in enumerate(trn_blocks, start=1):
            if not block.strip():
                continue

            trntype = self._extract_tag_value(block, "TRNTYPE").upper()
            dtposted = self._extract_tag_value(block, "DTPOSTED")
            trnamt_str = self._extract_tag_value(block, "TRNAMT")
            fitid = self._extract_tag_value(block, "FITID")
            name = self._extract_tag_value(block, "NAME")
            memo = self._extract_tag_value(block, "MEMO")
            checknum = self._extract_tag_value(block, "CHECKNUM")

            if not dtposted:
                errors.append({"row": idx, "error": "Missing DTPOSTED in OFX statement transaction."})
                rejected_rows += 1
                continue

            try:
                txn_date = parse_ofx_date(dtposted)
            except ValueError as exc:
                errors.append({"row": idx, "error": f"Invalid DTPOSTED: {exc}"})
                rejected_rows += 1
                continue

            if not trnamt_str:
                errors.append({"row": idx, "error": "Missing TRNAMT in OFX transaction."})
                rejected_rows += 1
                continue

            try:
                signed_amount = clean_decimal(trnamt_str)
            except ValueError as exc:
                errors.append({"row": idx, "error": f"Invalid TRNAMT: {exc}"})
                rejected_rows += 1
                continue

            if signed_amount == Decimal("0.0000"):
                errors.append({"row": idx, "error": "Transaction amount is zero."})
                rejected_rows += 1
                continue

            if signed_amount > Decimal("0"):
                credit_amt = signed_amount
                debit_amt = Decimal("0.0000")
            else:
                debit_amt = abs(signed_amount)
                credit_amt = Decimal("0.0000")

            # Classification
            if trntype in ("FEE", "SRVCHG"):
                classification = StatementTransactionClassification.BANK_FEE
            elif trntype in ("INT", "DIV"):
                classification = StatementTransactionClassification.INTEREST
            elif trntype in ("XFER",):
                classification = StatementTransactionClassification.TRANSFER
            elif signed_amount > Decimal("0"):
                classification = StatementTransactionClassification.CUSTOMER_PAYMENT
            else:
                classification = StatementTransactionClassification.SUPPLIER_PAYMENT

            desc_parts = [p for p in (name, memo) if p]
            description = " - ".join(desc_parts) if desc_parts else f"OFX Transaction {fitid}"
            reference = checknum or fitid

            parsed_trx = ParsedTransaction(
                source_transaction_id=fitid,
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
                    "trntype": trntype,
                    "fitid": fitid,
                    "name": name,
                    "memo": memo,
                    "checknum": checknum,
                },
            )
            transactions.append(parsed_trx)

        return StatementParseResult(
            opening_balance=None,
            closing_balance=closing_balance,
            currency=currency[:3].upper(),
            transactions=transactions,
            errors=errors,
            total_rows=total_rows,
            rejected_rows=rejected_rows,
        )

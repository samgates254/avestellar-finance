"""Base statement parser interfaces and data structures."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from payments.models import StatementTransactionClassification


@dataclass
class ParsedTransaction:
    """Normalized representation of a single transaction parsed from an imported statement."""

    source_transaction_id: str
    transaction_date: date
    value_date: date | None
    description: str
    reference: str
    debit_amount: Decimal
    credit_amount: Decimal
    signed_amount: Decimal
    currency: str
    running_balance: Decimal | None = None
    classification: str = StatementTransactionClassification.UNCLASSIFIED
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StatementParseResult:
    """Comprehensive outcome of a statement parsing operation."""

    opening_balance: Decimal | None
    closing_balance: Decimal | None
    currency: str
    transactions: list[ParsedTransaction] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    total_rows: int = 0
    rejected_rows: int = 0


class BaseStatementParser(ABC):
    """Abstract base class for statement file format adapters."""

    @abstractmethod
    def parse(self, content: str | bytes, **kwargs) -> StatementParseResult:
        """Parse raw statement payload into normalized StatementParseResult."""
        pass

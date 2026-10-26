from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Optional
from django.utils import timezone


class CalculationStatus(str, Enum):
    AVAILABLE = "available"
    INSUFFICIENT_DATA = "insufficient_data"
    UNDEFINED = "undefined"
    NOT_APPLICABLE = "not_applicable"
    UNSUPPORTED_CURRENCY = "unsupported_currency"


class MetricUnit(str, Enum):
    CURRENCY = "currency"
    PERCENTAGE = "percentage"
    RATIO = "ratio"
    DAYS = "days"
    UNITS = "units"
    MULTIPLIER = "multiplier"


@dataclass
class MetricResult:
    """Canonical result contract for all financial metric engine calculations."""

    metric_id: str
    name: str
    formula_version: str = "v1"
    status: CalculationStatus = CalculationStatus.AVAILABLE
    value: Optional[Decimal] = None
    formatted_value: str = ""
    unit: MetricUnit = MetricUnit.CURRENCY
    currency: Optional[str] = None
    reporting_period: Optional[dict[str, Any]] = None
    calculated_at: str = field(default_factory=lambda: timezone.now().isoformat())
    inputs: dict[str, Any] = field(default_factory=dict)
    source_accounts: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def calculated_value(self) -> Optional[Decimal]:
        return self.value

    @calculated_value.setter
    def calculated_value(self, val: Optional[Decimal]):
        self.value = val

    @property
    def input_values(self) -> dict[str, Any]:
        return self.inputs

    @input_values.setter
    def input_values(self, val: dict[str, Any]):
        self.inputs = val

    def to_dict(self) -> dict[str, Any]:
        """Serializes MetricResult to a standard JSON-compatible dictionary."""
        val_str = str(self.value) if self.value is not None else None
        serialized_inputs = {
            k: str(v) if isinstance(v, Decimal) else v
            for k, v in self.inputs.items()
        }
        return {
            "metric_id": self.metric_id,
            "name": self.name,
            "formula_version": self.formula_version,
            "status": self.status.value if isinstance(self.status, CalculationStatus) else str(self.status),
            "value": val_str,
            "calculated_value": val_str,
            "formatted_value": self.formatted_value,
            "unit": self.unit.value if isinstance(self.unit, MetricUnit) else str(self.unit),
            "currency": self.currency,
            "reporting_period": self.reporting_period,
            "calculated_at": self.calculated_at,
            "inputs": serialized_inputs,
            "input_values": serialized_inputs,
            "source_accounts": self.source_accounts,
            "assumptions": self.assumptions,
            "warnings": self.warnings,
            "metadata": self.metadata,
        }

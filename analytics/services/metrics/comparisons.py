from decimal import Decimal
from typing import Any, Optional


def compare_metrics(
    current: Decimal,
    prior: Decimal,
    metric_name: str = "",
    higher_is_better: bool = True,
) -> dict[str, Any]:
    """Computes absolute and percentage variances between two periods with zero and negative base safeguards."""
    abs_variance = current - prior

    pct_variance: Optional[Decimal] = None
    warning: Optional[str] = None

    if prior == Decimal("0.0000"):
        if current == Decimal("0.0000"):
            pct_variance = Decimal("0.0000")
        else:
            warning = f"Prior period {metric_name} is zero; percentage growth is mathematically undefined."
    elif prior < Decimal("0.0000"):
        # When prior base is negative, conventional percentage can be misleading
        pct_variance = (abs_variance / abs(prior)) * Decimal("100.0000")
        warning = f"Prior period {metric_name} is negative ({prior}); percentage change reflects growth relative to absolute baseline."
    else:
        pct_variance = (abs_variance / prior) * Decimal("100.0000")

    if abs_variance > Decimal("0.0000"):
        direction = "FAVORABLE" if higher_is_better else "UNFAVORABLE"
    elif abs_variance < Decimal("0.0000"):
        direction = "UNFAVORABLE" if higher_is_better else "FAVORABLE"
    else:
        direction = "NEUTRAL"

    return {
        "current_value": str(current),
        "prior_value": str(prior),
        "absolute_variance": str(abs_variance),
        "percentage_variance": str(pct_variance.quantize(Decimal("0.0100"))) if pct_variance is not None else None,
        "direction": direction,
        "warning": warning,
    }

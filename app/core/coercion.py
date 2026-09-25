from __future__ import annotations

from typing import Any


def safe_non_negative_int(value: Any) -> int:
    """Coerce persisted or model-produced values to a non-negative integer."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0

from __future__ import annotations

from collections.abc import Callable
from typing import Any


Validator = Callable[[dict[str, Any]], None]


def require_fields(*fields: str) -> Validator:
    """Build a validator for non-empty required object fields."""
    def validate(result: dict[str, Any]) -> None:
        missing = [
            field
            for field in fields
            if field not in result
            or result[field] is None
            or (isinstance(result[field], str) and not result[field].strip())
        ]
        if missing:
            raise ValueError(f"模型缺少必要字段：{', '.join(missing)}")

    return validate


def require_schema(
    *fields: str,
    list_fields: tuple[str, ...] = (),
    list_bounds: dict[str, tuple[int, int | None]] | None = None,
) -> Validator:
    """Build a validator for required fields and bounded list shapes."""
    base_validator = require_fields(*fields)
    bounds = list_bounds or {}

    def validate(result: dict[str, Any]) -> None:
        base_validator(result)
        for field in list_fields:
            if not isinstance(result.get(field), list):
                raise ValueError(f"字段 {field} 必须是数组")
        for field, (minimum, maximum) in bounds.items():
            value = result.get(field)
            if not isinstance(value, list):
                raise ValueError(f"字段 {field} 必须是数组")
            if len(value) < minimum or (
                maximum is not None and len(value) > maximum
            ):
                expected = (
                    str(minimum)
                    if maximum == minimum
                    else f"{minimum}-{maximum or '更多'}"
                )
                raise ValueError(
                    f"字段 {field} 应有 {expected} 项，实际 {len(value)} 项"
                )

    return validate

from __future__ import annotations

from typing import Any


def bounded_excerpt(value: Any, limit: int) -> str:
    """Keep representative beginning, middle, and ending text within a limit."""
    text = str(value or "")
    if len(text) <= limit:
        return text
    third = max(1, (limit - 80) // 3)
    middle_start = max(0, len(text) // 2 - third // 2)
    return (
        text[:third]
        + "\n[…内容过长，保留中段代表片段…]\n"
        + text[middle_start : middle_start + third]
        + "\n[…内容过长，保留结尾…]\n"
        + text[-third:]
    )

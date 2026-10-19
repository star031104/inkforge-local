from __future__ import annotations

import re

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


def relabel_ab(text: str, names: dict[str, str]) -> str:
    """Replace a judge's anonymous "A/B"(稿) labels with the real candidate names."""
    return re.sub(
        r"(?:候选|方案)?(?<![A-Za-z])([AB])(?![A-Za-z])(?:稿|版)?",
        lambda match: names.get(match.group(1), match.group(0)),
        str(text or ""),
    )

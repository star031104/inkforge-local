"""One deterministic budget for prompt compilation, preview and model requests."""

from __future__ import annotations

import re
import math
from dataclasses import asdict, dataclass
from typing import Any


class ContextBudgetError(ValueError):
    """The immutable input and requested output do not fit the configured window."""


def estimate_tokens(text: str) -> int:
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    ascii_chars = sum(ord(char) < 128 for char in text)
    other_unicode = len(text) - chinese - ascii_chars
    # CJK is no longer discounted; emoji and other Unicode are not counted
    # like ASCII. This remains a conservative heuristic, not a tokenizer.
    return math.ceil(chinese + other_unicode * 2 + ascii_chars / 3.6) + 1


def safety_margin(window: int) -> int:
    return min(900, max(300, window // 24)) if window else 0


@dataclass(frozen=True)
class RequestBudget:
    context_window: int
    input_tokens: int
    output_tokens: int
    safety_tokens: int
    available_output_tokens: int | None
    fits: bool

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "token_source": "estimated"}


def request_budget(settings: dict, messages: list, output_tokens: int) -> RequestBudget:
    window = max(0, int(settings.get("context_budget", 0) or 0))
    input_tokens = estimate_tokens("\n".join(str(m.get("content", "")) for m in messages))
    input_tokens += 8 * len(messages)
    margin = safety_margin(window)
    available = max(0, window - input_tokens - margin) if window else None
    return RequestBudget(
        window,
        input_tokens,
        max(0, output_tokens),
        margin,
        available,
        available is None or input_tokens + margin + max(0, output_tokens) <= window,
    )


def validate_request_budget(settings: dict, messages: list, output_tokens: int) -> None:
    budget = request_budget(settings, messages, output_tokens)
    if not budget.fits:
        raise ContextBudgetError(
            f"上下文预算不足：输入估算 {budget.input_tokens}，"
            f"输出预留 {budget.output_tokens}，安全余量 {budget.safety_tokens}，"
            f"设置上限 {budget.context_window}。请缩短材料或降低输出长度；"
            "只有模型实际支持时才增大上下文上限。"
        )


def fit_output_tokens(
    settings: dict,
    messages: list,
    requested: int,
    *,
    minimum: int = 256,
) -> int:
    """Reduce output allowance only; never silently remove authoritative input."""
    budget = request_budget(settings, messages, requested)
    if budget.available_output_tokens is None:
        return requested
    fitted = min(requested, budget.available_output_tokens)
    if fitted < min(minimum, requested):
        validate_request_budget(settings, messages, min(minimum, requested))
    return fitted

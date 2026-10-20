"""Provider completion receipts; punctuation alone is not a completion signal."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


class ModelOutputTruncatedError(ValueError):
    """A provider exhausted its output allowance before completing the result."""


class ModelStreamError(ValueError):
    """An error arrived inside an otherwise successful streaming HTTP response."""


@dataclass
class StreamOutcome:
    finish_reason: str = "unknown"
    terminal_received: bool = False

    @property
    def truncated(self) -> bool:
        return self.finish_reason in {"length", "max_tokens"}

    def observe(self, payload: str | dict[str, Any]) -> None:
        if payload == "[DONE]":
            self.terminal_received = True
            return
        try:
            event = json.loads(payload) if isinstance(payload, str) else payload
        except (ValueError, TypeError):
            return
        if not isinstance(event, dict):
            return
        if event.get("error"):
            raise ModelStreamError(
                "模型服务在输出期间返回错误，已收到的片段保留为候选，请重试或检查模型服务。"
            )
        choices = event.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            return
        reason = choices[0].get("finish_reason")
        if reason:
            self.finish_reason = str(reason).lower()[:60]
            self.terminal_received = True

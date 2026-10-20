"""A truncated JSON answer is retried with real headroom, not +256 tokens."""
import asyncio

import app.llm.structured_output as so
from app.llm.completion import ModelOutputTruncatedError


def test_truncated_structured_output_retries_with_double_budget(monkeypatch):
    calls = []

    async def fake(settings, messages, **kwargs):
        calls.append(kwargs.get("max_tokens"))
        if len(calls) == 1:
            raise ModelOutputTruncatedError("length")
        return '{"ok": true}'

    monkeypatch.setattr(so, "chat_once", fake)
    settings = {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "context_budget": 64000}
    result, warnings = asyncio.run(so.structured_completion(
        settings, [{"role": "user", "content": "规划"}], max_tokens=6000,
        timeout_seconds=5, temperature=0.3, token_ceiling=40000,
    ))
    assert result == {"ok": True}
    assert calls == [6000, 12000]
    assert warnings

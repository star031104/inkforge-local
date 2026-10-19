"""Local call accounting; deliberately stores no prompts, prose, URLs or credentials."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .core.model_budget import estimate_tokens as estimate

_path: Path | None = None
_active: ContextVar[dict | None] = ContextVar("model_call", default=None)


def configure(path: Path) -> None:
    global _path
    _path = path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS model_calls (id TEXT PRIMARY KEY, created_at TEXT, payload TEXT)")


def observe(data: str | dict) -> None:
    current = _active.get()
    if current is None:
        return
    try:
        event = json.loads(data) if isinstance(data, str) else data
        usage = event.get("usage")
        if isinstance(usage, dict):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if isinstance(usage.get(key), (int, float)):
                    current[key] = max(0, int(usage[key]))
            if "prompt_tokens" in current and "completion_tokens" in current:
                current["token_source"] = "provider"
        for choice in event.get("choices", []):
            if choice.get("finish_reason"):
                current["finish_reason"] = str(choice["finish_reason"])[:60]
    except (ValueError, TypeError, AttributeError):
        return


def attempt(index: int) -> None:
    if _active.get() is not None:
        _active.get()["attempts"] = index + 1


class Call:
    def __init__(self, settings: dict, messages: list):
        self.data = {"id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
                     "model": str(settings.get("model", ""))[:160],
                     "provider": str(settings.get("provider", ""))[:40],
                     "workload": str(settings.get("_workload", "prose"))[:40],
                     # Risk-gated audit: "merged" / "full" (empty for non-critic calls).
                     "critic_budget": str(settings.get("_critic_budget", "") or "")[:20],
                     "estimated_prompt_tokens": estimate("\n".join(str(m.get("content", "")) for m in messages)),
                     "token_source": "estimated", "attempts": 1, "finish_reason": "unknown"}
        self.output = ""
        self.started = time.monotonic()
        self.token = _active.set(self.data)

    def finish(self, error: BaseException | None = None) -> None:
        self.data.update(duration_seconds=round(time.monotonic() - self.started, 3),
                         estimated_completion_tokens=estimate(self.output) if self.output else 0,
                         status="failed" if error else "completed",
                         error_type=type(error).__name__ if error else "")
        _active.reset(self.token)
        if _path is None:
            return
        try:
            with sqlite3.connect(_path, timeout=2) as db:
                db.execute("INSERT INTO model_calls VALUES (?,?,?)", (self.data["id"], self.data["created_at"], json.dumps(self.data)))
                db.execute("DELETE FROM model_calls WHERE id NOT IN (SELECT id FROM model_calls ORDER BY created_at DESC LIMIT 5000)")
        except sqlite3.Error:
            pass  # Observability failure must not discard a completed manuscript.


LENGTH_ALERT_RATE = 0.2
LENGTH_ALERT_MIN_CALLS = 5
# OpenAI/llama.cpp "length", Anthropic/Gemini "max_tokens"/"MAX_TOKENS", and
# JSON output that ended without a closing brace when no reason was reported.
TRUNCATION_REASONS = frozenset({"length", "max_tokens", "json_unclosed"})


def workload_health(calls: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Per-workload truncation and failure rates.

    ``length_rate`` is the share of calls that ended because the output token
    limit was reached (``finish_reason`` in ``TRUNCATION_REASONS``).  A rate
    above 20% (with at least five calls) means outputs are routinely cut off.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        groups.setdefault(str(call.get("workload") or "unknown"), []).append(call)
    health: dict[str, dict[str, Any]] = {}
    for name, items in sorted(groups.items()):
        total = len(items)
        truncated = sum(
            str(item.get("finish_reason", "")).lower() in TRUNCATION_REASONS for item in items
        )
        failed = sum(item.get("status") == "failed" for item in items)
        filtered = sum(str(item.get("finish_reason", "")) == "sensitive" for item in items)
        rate = round(truncated / total, 3) if total else 0.0
        health[name] = {
            "calls": total,
            "length_truncated": truncated,
            "length_rate": rate,
            "failed": failed,
            "content_filtered": filtered,
            "length_alert": total >= LENGTH_ALERT_MIN_CALLS and rate > LENGTH_ALERT_RATE,
        }
    return health


def report(limit: int = 100) -> dict[str, Any]:
    if _path is None:
        return {"calls": [], "summary": {}, "workloads": {}, "alerts": []}
    with sqlite3.connect(_path, timeout=2) as db:
        rows = db.execute("SELECT payload FROM model_calls ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
    calls = [json.loads(r[0]) for r in rows]
    workloads = workload_health(calls)
    alerts = [
        f"{name} 调用中 {round(item['length_rate'] * 100)}% 因输出长度上限被截断，"
        "请缩小单次输出或拆分任务"
        for name, item in workloads.items()
        if item["length_alert"]
    ]
    return {"calls": calls, "workloads": workloads, "alerts": alerts, "summary": {"count": len(calls),
            "failed": sum(c["status"] == "failed" for c in calls),
            "retries": sum(max(0, c["attempts"] - 1) for c in calls),
            "seconds": round(sum(c["duration_seconds"] for c in calls), 1),
            "provider_usage_calls": sum(c["token_source"] == "provider" for c in calls),
            "cost": None}, "note": "仅统计最近调用；无供应商用量时显示估算，未配置单价不推算费用。"}

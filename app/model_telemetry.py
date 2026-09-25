"""Local call accounting; deliberately stores no prompts, prose, URLs or credentials."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_path: Path | None = None
_active: ContextVar[dict | None] = ContextVar("model_call", default=None)


def configure(path: Path) -> None:
    global _path
    _path = path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS model_calls (id TEXT PRIMARY KEY, created_at TEXT, payload TEXT)")


def estimate(text: str) -> int:
    chinese = len(re.findall(r"[\u3400-\u9fff]", text))
    return int(chinese / 1.25 + (len(text) - chinese) / 3.6) + 1


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


def report(limit: int = 100) -> dict[str, Any]:
    if _path is None:
        return {"calls": [], "summary": {}}
    with sqlite3.connect(_path, timeout=2) as db:
        rows = db.execute("SELECT payload FROM model_calls ORDER BY created_at DESC LIMIT ?", (max(1, min(limit, 1000)),)).fetchall()
    calls = [json.loads(r[0]) for r in rows]
    return {"calls": calls, "summary": {"count": len(calls),
            "failed": sum(c["status"] == "failed" for c in calls),
            "retries": sum(max(0, c["attempts"] - 1) for c in calls),
            "seconds": round(sum(c["duration_seconds"] for c in calls), 1),
            "provider_usage_calls": sum(c["token_source"] == "provider" for c in calls),
            "cost": None}, "note": "仅统计最近调用；无供应商用量时显示估算，未配置单价不推算费用。"}

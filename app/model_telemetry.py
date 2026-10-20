"""Local call accounting; deliberately stores no prompts, prose, URLs or credentials.

Every model call becomes one row in ``model_calls`` (latest 5000 kept) and is
also folded into ``usage_rollup`` keyed by project, chapter and workload, so
per-project totals and cost survive the row pruning.  Token numbers come from
the provider's ``usage`` (OpenAI/DeepSeek, also the final stream chunk) or from
llama.cpp ``timings``; without either they stay estimates and no cost is
derived.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .core.model_budget import estimate_tokens as estimate

_path: Path | None = None
_active: ContextVar[dict | None] = ContextVar("model_call", default=None)
# (project_id, chapter_id) of the request or director step issuing model calls.
_scope: ContextVar[tuple[str, str]] = ContextVar("model_call_scope", default=("", ""))

# "provider" is the pre-0.35 spelling of "usage" in stored rows.
USAGE_SOURCES = frozenset({"usage", "provider"})


def configure(path: Path) -> None:
    global _path
    _path = path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE IF NOT EXISTS model_calls (id TEXT PRIMARY KEY, created_at TEXT, payload TEXT)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS usage_rollup ("
            "project_id TEXT NOT NULL, chapter_id TEXT NOT NULL, workload TEXT NOT NULL, "
            "calls INTEGER DEFAULT 0, failed INTEGER DEFAULT 0, usage_calls INTEGER DEFAULT 0, "
            "prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0, "
            "cache_hit_tokens INTEGER DEFAULT 0, cache_miss_tokens INTEGER DEFAULT 0, "
            "reasoning_tokens INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, priced_calls INTEGER DEFAULT 0, "
            "first_at TEXT, last_at TEXT, PRIMARY KEY (project_id, chapter_id, workload))"
        )


# --------------------------------------------------------------------------
# Attribution scope
# --------------------------------------------------------------------------

def bind_scope(project_id: Any = None, chapter_id: Any = None) -> None:
    """Attribute later model calls in this context to a project/chapter.

    ``None`` keeps the current value.  Request handlers run in their own
    context, so binding there never leaks into other requests; a background
    task keeps the binding until it binds again.
    """
    project, chapter = _scope.get()
    if project_id is not None:
        new_project = str(project_id or "")[:80]
        if new_project != project:
            chapter = ""
        project = new_project
    if chapter_id is not None:
        chapter = str(chapter_id or "")[:80]
    _scope.set((project, chapter))


@contextmanager
def usage_scope(project_id: Any = None, chapter_id: Any = None) -> Iterator[None]:
    """Scoped variant of :func:`bind_scope` that restores the previous scope."""
    token = _scope.set(_scope.get())
    try:
        bind_scope(project_id, chapter_id)
        yield
    finally:
        _scope.reset(token)


def current_scope() -> tuple[str, str]:
    return _scope.get()


# --------------------------------------------------------------------------
# Prices (USD per 1M tokens)
# --------------------------------------------------------------------------

DEEPSEEK_PEAK_HOURS_UTC = ((1, 4), (6, 10))

# DeepSeek charges double during peak hours: 01:00–04:00 and 06:00–10:00 UTC,
# Monday to Friday.  ``None`` prices mean "family known, price not bundled":
# such calls are counted but never priced.
PRICE_TABLE: dict[str, dict[str, Any]] = {
    "deepseek-flash": {
        "label": "DeepSeek Flash",
        "off_peak": {"cache_hit": 0.003, "cache_miss": 0.15, "output": 0.6},
        "peak": {"cache_hit": 0.006, "cache_miss": 0.3, "output": 1.2},
        "peak_hours_utc": DEEPSEEK_PEAK_HOURS_UTC,
    },
    "deepseek-v4-pro": {
        "label": "DeepSeek V4 Pro",
        "off_peak": None,
        "peak": None,
        "peak_hours_utc": DEEPSEEK_PEAK_HOURS_UTC,
    },
}


def model_family(model: str, server: str = "") -> str | None:
    name = str(model or "").strip().lower()
    if "deepseek" not in name and server != "deepseek":
        return None
    if "flash" in name:
        return "deepseek-flash"
    if "pro" in name:
        return "deepseek-v4-pro"
    return None


def is_peak(moment: datetime, family: str | None = None) -> bool:
    spec = PRICE_TABLE.get(family or "deepseek-flash") or {}
    utc = moment.astimezone(timezone.utc) if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    if utc.weekday() >= 5:
        return False
    return any(start <= utc.hour < end for start, end in spec.get("peak_hours_utc", ()))


def call_cost(call: dict[str, Any]) -> float | None:
    """USD cost of one call from real usage, or ``None`` when unknown."""
    if call.get("token_source") not in USAGE_SOURCES:
        return None
    family = call.get("price_family") or model_family(call.get("model", ""), call.get("server", ""))
    spec = PRICE_TABLE.get(family or "")
    if not spec:
        return None
    try:
        moment = datetime.fromisoformat(str(call.get("created_at")))
    except (TypeError, ValueError):
        moment = datetime.now(timezone.utc)
    prices = spec["peak"] if is_peak(moment, family) else spec["off_peak"]
    if not prices:
        return None
    prompt = int(call.get("prompt_tokens") or 0)
    hit = int(call.get("cache_hit_tokens") or 0)
    miss_value = call.get("cache_miss_tokens")
    miss = int(miss_value) if isinstance(miss_value, (int, float)) else max(0, prompt - hit)
    output = int(call.get("completion_tokens") or 0)
    cost = (hit * prices["cache_hit"] + miss * prices["cache_miss"] + output * prices["output"]) / 1e6
    return round(cost, 8)


# --------------------------------------------------------------------------
# Observation
# --------------------------------------------------------------------------

def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0, int(value))


def _apply_usage(current: dict[str, Any], usage: dict[str, Any]) -> None:
    prompt = _int(usage.get("prompt_tokens"))
    completion = _int(usage.get("completion_tokens"))
    if prompt is not None:
        current["prompt_tokens"] = prompt
    if completion is not None:
        current["completion_tokens"] = completion
    total = _int(usage.get("total_tokens"))
    if total is not None:
        current["total_tokens"] = total
    hit = _int(usage.get("prompt_cache_hit_tokens"))
    miss = _int(usage.get("prompt_cache_miss_tokens"))
    details = usage.get("prompt_tokens_details")
    if hit is None and isinstance(details, dict):
        hit = _int(details.get("cached_tokens"))
    if hit is not None:
        current["cache_hit_tokens"] = hit
        if miss is None and prompt is not None:
            miss = max(0, prompt - hit)
    if miss is not None:
        current["cache_miss_tokens"] = miss
    completion_details = usage.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        reasoning = _int(completion_details.get("reasoning_tokens"))
        if reasoning is not None:
            current["reasoning_tokens"] = reasoning
    if "prompt_tokens" in current and "completion_tokens" in current:
        current["token_source"] = "usage"


def _apply_timings(current: dict[str, Any], timings: dict[str, Any]) -> None:
    """llama.cpp ``timings``: prompt_n = newly evaluated, cache_n = reused KV."""
    prompt_n = _int(timings.get("prompt_n"))
    predicted_n = _int(timings.get("predicted_n"))
    cache_n = _int(timings.get("cache_n")) or 0
    if current.get("token_source") == "usage":
        current.setdefault("cache_hit_tokens", cache_n)
        if prompt_n is not None:
            current.setdefault("cache_miss_tokens", prompt_n)
        return
    if prompt_n is None or predicted_n is None:
        return
    current["prompt_tokens"] = prompt_n + cache_n
    current["completion_tokens"] = predicted_n
    current["cache_hit_tokens"] = cache_n
    current["cache_miss_tokens"] = prompt_n
    current["token_source"] = "usage"


def observe(data: str | dict) -> None:
    current = _active.get()
    if current is None:
        return
    try:
        event = json.loads(data) if isinstance(data, str) else data
        usage = event.get("usage")
        if isinstance(usage, dict):
            _apply_usage(current, usage)
        timings = event.get("timings")
        if isinstance(timings, dict):
            _apply_timings(current, timings)
        for choice in event.get("choices") or []:
            if choice.get("finish_reason"):
                current["finish_reason"] = str(choice["finish_reason"])[:60]
    except (ValueError, TypeError, AttributeError):
        return


def has_usage() -> bool:
    """True once the active call has received real token counts."""
    current = _active.get()
    return bool(current and current.get("token_source") == "usage")


def attempt(index: int) -> None:
    if _active.get() is not None:
        _active.get()["attempts"] = index + 1


def _server_for(settings: dict) -> str:
    try:
        from .providers import resolve_capabilities

        return resolve_capabilities(settings).server
    except Exception:  # noqa: BLE001 - accounting must never break a call
        return ""


class Call:
    def __init__(self, settings: dict, messages: list):
        project_id, chapter_id = _scope.get()
        server = _server_for(settings)
        model = str(settings.get("model", ""))[:160]
        self.data = {"id": str(uuid.uuid4()), "created_at": datetime.now(timezone.utc).isoformat(),
                     "model": model,
                     "provider": str(settings.get("provider", ""))[:40],
                     "server": server[:40],
                     "workload": str(settings.get("_workload", "prose"))[:40],
                     # Risk-gated audit: "merged" / "full" (empty for non-critic calls).
                     "critic_budget": str(settings.get("_critic_budget", "") or "")[:20],
                     "project_id": project_id,
                     "chapter_id": chapter_id,
                     "price_family": model_family(model, server),
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
        self.data["cost_usd"] = call_cost(self.data)
        _active.reset(self.token)
        if _path is None:
            return
        try:
            with sqlite3.connect(_path, timeout=2) as db:
                db.execute("INSERT INTO model_calls VALUES (?,?,?)", (self.data["id"], self.data["created_at"], json.dumps(self.data)))
                db.execute("DELETE FROM model_calls WHERE id NOT IN (SELECT id FROM model_calls ORDER BY created_at DESC LIMIT 5000)")
                _rollup(db, self.data)
        except sqlite3.Error:
            pass  # Observability failure must not discard a completed manuscript.


def _call_tokens(call: dict[str, Any]) -> tuple[int, int]:
    """Real prompt/completion tokens when reported, otherwise the estimates."""
    real = call.get("token_source") in USAGE_SOURCES
    prompt = call.get("prompt_tokens") if real else None
    completion = call.get("completion_tokens") if real else None
    return (
        int(prompt if prompt is not None else call.get("estimated_prompt_tokens") or 0),
        int(completion if completion is not None else call.get("estimated_completion_tokens") or 0),
    )


def _rollup(db: sqlite3.Connection, call: dict[str, Any]) -> None:
    prompt, completion = _call_tokens(call)
    real = call.get("token_source") in USAGE_SOURCES
    cost = call.get("cost_usd")
    db.execute(
        "INSERT INTO usage_rollup (project_id, chapter_id, workload, calls, failed, usage_calls, "
        "prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens, reasoning_tokens, "
        "cost_usd, priced_calls, first_at, last_at) VALUES (?,?,?,1,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(project_id, chapter_id, workload) DO UPDATE SET "
        "calls=calls+1, failed=failed+excluded.failed, usage_calls=usage_calls+excluded.usage_calls, "
        "prompt_tokens=prompt_tokens+excluded.prompt_tokens, "
        "completion_tokens=completion_tokens+excluded.completion_tokens, "
        "cache_hit_tokens=cache_hit_tokens+excluded.cache_hit_tokens, "
        "cache_miss_tokens=cache_miss_tokens+excluded.cache_miss_tokens, "
        "reasoning_tokens=reasoning_tokens+excluded.reasoning_tokens, "
        "cost_usd=cost_usd+excluded.cost_usd, priced_calls=priced_calls+excluded.priced_calls, "
        "last_at=excluded.last_at",
        (
            str(call.get("project_id") or ""), str(call.get("chapter_id") or ""),
            str(call.get("workload") or "unknown"),
            int(call.get("status") == "failed"), int(real), prompt, completion,
            int(call.get("cache_hit_tokens") or 0) if real else 0,
            int(call.get("cache_miss_tokens") or 0) if real else 0,
            int(call.get("reasoning_tokens") or 0) if real else 0,
            float(cost or 0), int(cost is not None),
            call.get("created_at"), call.get("created_at"),
        ),
    )


# --------------------------------------------------------------------------
# Per-project aggregates
# --------------------------------------------------------------------------

_SUM_FIELDS = ("calls", "failed", "usage_calls", "prompt_tokens", "completion_tokens",
               "cache_hit_tokens", "cache_miss_tokens", "reasoning_tokens", "cost_usd", "priced_calls")


def _empty_bucket() -> dict[str, Any]:
    return {**{key: 0 for key in _SUM_FIELDS}, "last_at": ""}


def _add(bucket: dict[str, Any], row: dict[str, Any]) -> None:
    for key in _SUM_FIELDS:
        bucket[key] += row.get(key) or 0
    if str(row.get("last_at") or "") > bucket["last_at"]:
        bucket["last_at"] = str(row["last_at"])


def _finalize(bucket: dict[str, Any]) -> dict[str, Any]:
    hit, miss = bucket["cache_hit_tokens"], bucket["cache_miss_tokens"]
    result = dict(bucket)
    result["cache_hit_rate"] = round(hit / (hit + miss), 4) if hit + miss else None
    result["cost_usd"] = round(bucket["cost_usd"], 6) if bucket["priced_calls"] else None
    result["cost_complete"] = bool(bucket["calls"]) and bucket["priced_calls"] == bucket["calls"]
    result["total_tokens"] = bucket["prompt_tokens"] + bucket["completion_tokens"]
    return result


def project_usage(
    project_id: str, *, last_chapters: int = 10, chapter_id: str | None = None
) -> dict[str, Any]:
    """Totals, per-workload and per-chapter usage for one project."""
    empty = {
        "project_id": project_id, "available": _path is not None,
        "totals": _finalize(_empty_bucket()), "workloads": {}, "chapters": [],
        "recent_chapters": [], "chapter": None, "currency": "USD",
    }
    if _path is None or not project_id:
        return empty
    try:
        with sqlite3.connect(_path, timeout=2) as db:
            db.row_factory = sqlite3.Row
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM usage_rollup WHERE project_id = ?", (str(project_id),)
                ).fetchall()
            ]
    except sqlite3.Error:
        return empty
    totals = _empty_bucket()
    workloads: dict[str, dict[str, Any]] = {}
    chapters: dict[str, dict[str, Any]] = {}
    chapter_workloads: dict[str, dict[str, int]] = {}
    for row in rows:
        _add(totals, row)
        _add(workloads.setdefault(row["workload"], _empty_bucket()), row)
        if row["chapter_id"]:
            _add(chapters.setdefault(row["chapter_id"], _empty_bucket()), row)
            chapter_workloads.setdefault(row["chapter_id"], {})[row["workload"]] = row["calls"]
    chapter_list = [
        {"chapter_id": cid, **_finalize(bucket), "workloads": chapter_workloads.get(cid, {})}
        for cid, bucket in chapters.items()
    ]
    chapter_list.sort(key=lambda item: item["last_at"], reverse=True)
    limit = max(1, min(int(last_chapters or 10), 200))
    selected = None
    if chapter_id:
        selected = next((item for item in chapter_list if item["chapter_id"] == chapter_id), None)
    return {
        "project_id": project_id,
        "available": True,
        "totals": _finalize(totals),
        "workloads": {name: _finalize(bucket) for name, bucket in sorted(workloads.items())},
        "chapters": chapter_list,
        "recent_chapters": chapter_list[:limit],
        "chapter": selected,
        "currency": "USD",
        "note": "费用只按供应商返回的真实用量与内置价目计算；本地模型与价目未知的模型不计费。",
    }


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
    costs = [c.get("cost_usd") for c in calls if isinstance(c.get("cost_usd"), (int, float))]
    return {"calls": calls, "workloads": workloads, "alerts": alerts, "summary": {"count": len(calls),
            "failed": sum(c["status"] == "failed" for c in calls),
            "retries": sum(max(0, c["attempts"] - 1) for c in calls),
            "seconds": round(sum(c["duration_seconds"] for c in calls), 1),
            "provider_usage_calls": sum(c.get("token_source") in USAGE_SOURCES for c in calls),
            "cost": round(sum(costs), 6) if costs else None},
            "note": "仅统计最近调用；无供应商用量时显示估算，价目未知的模型不推算费用。"}

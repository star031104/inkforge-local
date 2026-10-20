"""Hook debt ledger (追读力): every chapter's planned ending hook is a promise.

When a chapter is accepted its planned ``ending_hook`` (route first, then the
execution plan) is recorded as an open hook.  The next ``window`` chapters'
timeline checker sees the open hooks and reports, per hook id, whether the
chapter ``paid`` it off, ``advanced`` it, or ``ignored`` it.  A hook ignored
until its window has passed becomes a *hook debt*; the most recent debts are
injected into the writing context of the following chapter.

State lives in ``memory["hook_ledger"]`` as one record per origin chapter
(keyed by chapter id; positions are re-derived from the current chapter
order) with evaluations keyed by the evaluating chapter's id, so re-settling a chapter simply replaces
its evaluation and every view "as of chapter N" is a pure fold over
evaluations from earlier chapters (the same discipline as the item/injury
state machine).
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

HOOK_STATUSES = ("paid", "advanced", "ignored")
LEDGER_LIMIT = 200
DEFAULT_WINDOW = 2
MAX_PROMPT_DEBTS = 3
# A debt expires after this many windows without being advanced or paid.
DEBT_LIFETIME = 3

# Endings that leave the reader with a question or an unresolved tension.
HOOK_MARKERS = (
    "？", "?", "……", "——", "却", "竟", "忽然", "突然", "猛地", "然而", "可是", "但是", "只是",
    "究竟", "到底", "难道", "谁", "为什么", "怎么会", "不知道", "未知", "下一刻", "就在此时",
    "就在这时", "身后", "门外", "脚步声", "等着", "明天", "明日", "今夜", "来了", "还没", "尚未",
    "不见了", "消失", "秘密", "真相", "答案",
)
_COMMON_BIGRAMS = {"一个", "自己", "没有", "什么", "他们", "我们", "你们", "这个", "那个", "已经", "时候", "本章"}


def _memory(project: dict[str, Any]) -> dict[str, Any]:
    memory = project.get("memory")
    if not isinstance(memory, dict):
        memory = {}
        project["memory"] = memory
    return memory


def _ledger(memory: dict[str, Any]) -> list[dict[str, Any]]:
    ledger = memory.get("hook_ledger")
    if not isinstance(ledger, list):
        ledger = []
        memory["hook_ledger"] = ledger
    ledger[:] = [item for item in ledger if isinstance(item, dict) and item.get("id")]
    return ledger


def planned_hook(chapter: dict[str, Any]) -> str:
    route = chapter.get("route", {}) if isinstance(chapter.get("route"), dict) else {}
    plan = chapter.get("plan", {}) if isinstance(chapter.get("plan"), dict) else {}
    return str(route.get("ending_hook") or plan.get("ending_hook") or "").strip()


def hook_id(chapter_id: str, text: str) -> str:
    digest = hashlib.sha256(f"{chapter_id}|{text}".encode("utf-8")).hexdigest()[:8]
    return f"H{digest}"


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"[^一-鿿A-Za-z0-9]", "", str(text or ""))
    return {compact[i:i + 2] for i in range(len(compact) - 1)} - _COMMON_BIGRAMS


def term_overlap(hook_text: str, draft: str) -> float:
    hook = _bigrams(hook_text)
    if not hook:
        return 0.0
    return len(hook & _bigrams(draft)) / len(hook)


def _positions(project: dict[str, Any]) -> dict[str, int]:
    """Current 1-based position of every chapter id (the ledger's source of truth)."""
    return {
        str(item.get("id", "")): number
        for number, item in enumerate(project.get("chapters", []) or [], start=1)
        if isinstance(item, dict) and str(item.get("id", ""))
    }


def _chapter_key(project: dict[str, Any], number: int) -> str:
    """Evaluation key of chapter ``number``: its id, or the number when unknown."""
    chapters = project.get("chapters", []) or []
    if 1 <= int(number) <= len(chapters) and isinstance(chapters[int(number) - 1], dict):
        chapter_id = str(chapters[int(number) - 1].get("id", ""))
        if chapter_id:
            return chapter_id
    return str(int(number))


def _resolve(key: Any, positions: dict[str, int]) -> int | None:
    text = str(key)
    if text in positions:
        return positions[text]
    return int(text) if text.isdigit() else None


def migrate_legacy_evaluations(record: dict[str, Any], project: dict[str, Any]) -> bool:
    """Re-key legacy evaluations (``{"5": "ignored"}``) by evaluating chapter id.

    Before the ledger was keyed by chapter id, evaluations were keyed by the
    evaluating chapter's *number*; after an insert or delete those numbers
    point at the wrong chapters.  The record's cached ``origin_chapter`` is the
    origin's position when the numbers were written, so every legacy number is
    shifted by ``current origin - cached origin`` and mapped to the chapter id
    now at that position.  Keys that fall outside the book (their chapter was
    deleted) are dropped; an existing id key always wins over a migrated one.
    The cached ``origin_chapter`` is refreshed, so the migration runs once.
    Returns True when the record changed.
    """
    if not isinstance(record, dict):
        return False
    positions = _positions(project)
    origin = positions.get(str(record.get("chapter_id", "")))
    if origin is None:
        return False  # origin unknown or deleted: nothing to anchor the shift on
    evaluations = record.get("evaluations")
    changed = False
    if isinstance(evaluations, dict) and any(str(key).isdigit() for key in evaluations):
        try:
            cached = int(record.get("origin_chapter", 0) or 0)
        except (TypeError, ValueError):
            cached = 0
        delta = origin - cached if cached > 0 else 0
        chapters = project.get("chapters", []) or []
        migrated: dict[str, Any] = {}
        legacy: dict[str, Any] = {}
        for key, verdict in evaluations.items():
            (legacy if str(key).isdigit() else migrated)[str(key)] = verdict
        for key, verdict in sorted(legacy.items(), key=lambda pair: int(pair[0])):
            target = int(key) + delta
            if not 1 <= target <= len(chapters):
                continue
            new_key = _chapter_key(project, target)
            migrated.setdefault(new_key, verdict)
        record["evaluations"] = migrated
        changed = True
    if record.get("origin_chapter") != origin:
        record["origin_chapter"] = origin
        changed = True
    return changed


def migrate_ledger(project: dict[str, Any]) -> int:
    """Migrate every ledger record of ``project`` in place (idempotent)."""
    memory = project.get("memory") if isinstance(project, dict) else None
    ledger = memory.get("hook_ledger") if isinstance(memory, dict) else None
    if not isinstance(ledger, list):
        return 0
    return sum(1 for record in ledger if migrate_legacy_evaluations(record, project))


def record_chapter_hook(
    project: dict[str, Any], chapter: dict[str, Any], number: int, *, window: int = DEFAULT_WINDOW
) -> dict[str, Any] | None:
    """Record (or replace) the accepted chapter's planned ending hook.

    Records are keyed by ``chapter_id``; ``origin_chapter`` is only a cached
    position, re-derived from the current chapter order on every read, so
    inserting or deleting chapters never shifts a hook onto the wrong chapter.
    """
    ledger = _ledger(_memory(project))
    migrate_ledger(project)
    chapter_id = str(chapter.get("id", ""))
    text = planned_hook(chapter)[:300]
    existing = next((item for item in ledger if item.get("chapter_id") == chapter_id), None)
    number = _positions(project).get(chapter_id, int(number))
    if not text:
        if existing is not None:
            ledger.remove(existing)
        return None
    if existing is not None and existing.get("text") == text:
        existing["origin_chapter"] = number
        existing["window"] = max(1, int(window))
        return existing
    if existing is not None:
        ledger.remove(existing)
    record = {
        "id": hook_id(chapter_id, text),
        "chapter_id": chapter_id,
        "origin_chapter": int(number),
        "text": text,
        "window": max(1, int(window)),
        "evaluations": {},
    }
    ledger.append(record)
    del ledger[:-LEDGER_LIMIT]
    return record


def _origin(record: dict[str, Any], positions: dict[str, int]) -> int | None:
    chapter_id = str(record.get("chapter_id", ""))
    if positions:
        # A hook whose chapter was deleted is no longer a promise.
        return positions.get(chapter_id)
    return int(record.get("origin_chapter", 0) or 0)


def hook_state(
    record: dict[str, Any], before_number: int, positions: dict[str, int] | None = None
) -> dict[str, Any]:
    """Fold evaluations from chapters strictly before ``before_number``.

    A debt that stays unanswered for ``DEBT_LIFETIME`` windows expires: it is
    no longer due, injected or re-evaluated, so neither the prompt nor the
    evaluation history grows without bound.
    """
    positions = positions or {}
    origin = _origin(record, positions)
    if origin is None:
        origin = int(record.get("origin_chapter", 0) or 0)
    window = max(1, int(record.get("window", DEFAULT_WINDOW) or DEFAULT_WINDOW))
    anchor = origin
    status = "open"
    closed_at = None
    evaluations = record.get("evaluations", {}) if isinstance(record.get("evaluations"), dict) else {}
    folded = sorted(
        (number, str(verdict)) for key, verdict in evaluations.items()
        if (number := _resolve(key, positions)) is not None and number > origin
    )
    for number, verdict in folded:
        if before_number and number >= before_number:
            continue
        if verdict == "paid":
            status, closed_at = "paid", number
            break
        if verdict == "advanced":
            anchor, status = number, "open"
        elif verdict == "ignored" and number - anchor >= window:
            status = "debt"
    if status == "debt" and before_number and before_number - anchor > window * DEBT_LIFETIME:
        status = "expired"
    return {
        "id": record.get("id"), "text": record.get("text", ""), "origin_chapter": origin,
        "status": status, "anchor": anchor, "window": window, "closed_at": closed_at,
    }


def due_hooks(project: dict[str, Any], number: int) -> list[dict[str, Any]]:
    """Hooks chapter ``number`` should answer: open ones in window, then debts.

    Fresh in-window hooks (usually the previous chapter's) come first so a
    backlog of old debts can never push them out of the audit's top slots.
    """
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    migrate_ledger(project)  # first touch re-keys legacy number evaluations
    positions = _positions(project)
    due = []
    for record in memory.get("hook_ledger", []) or []:
        if not isinstance(record, dict):
            continue
        origin = _origin(record, positions)
        if origin is None or origin >= number:
            continue
        state = hook_state(record, number, positions)
        if state["status"] == "debt" or (
            state["status"] == "open" and number - state["anchor"] <= state["window"]
        ):
            due.append(state)
    due.sort(key=lambda item: (item["status"] == "debt", -item["origin_chapter"]))
    return due


def outstanding_debts(project: dict[str, Any], number: int, limit: int = MAX_PROMPT_DEBTS) -> list[dict[str, Any]]:
    debts = [item for item in due_hooks(project, number) if item["status"] == "debt"]
    debts.sort(key=lambda item: -item["origin_chapter"])
    return debts[:limit]


def render_hooks_for_audit(hooks: list[dict[str, Any]]) -> str:
    if not hooks:
        return ""
    lines = [
        f"- [{item['id']}] 第{item['origin_chapter']}章钩子"
        + ("（已逾期成为钩子债）" if item["status"] == "debt" else "")
        + f"：{item['text']}"
        for item in hooks[:6]
    ]
    return (
        "上章钩子：判断本章是否承接/推进/兑现下列钩子，并在 hook_status 中逐条给出 "
        "paid（兑现）/advanced（推进）/ignored（未处理）。未处理本身不是错误，不要单独报 issue。\n"
        + "\n".join(lines)
    )


def render_hook_debts_for_prompt(project: dict[str, Any], number: int) -> str:
    debts = outstanding_debts(project, number)
    if not debts:
        return ""
    return (
        "以下前文章末钩子已多章未承接，读者仍在等待；本章应择其相关者自然推进或兑现，"
        "不得生硬罗列，也不得违背本章路线：\n"
        + "\n".join(f"- 第{item['origin_chapter']}章：{item['text']}" for item in debts)
    )


def settle_hooks(
    project: dict[str, Any],
    number: int,
    draft: str,
    hook_status: Any = None,
) -> list[dict[str, Any]]:
    """Record chapter ``number``'s verdict for every hook due to it.

    Model verdicts (``[{"id", "status"}]``) win; hooks the model did not judge
    fall back to a deterministic term-overlap check against the accepted prose.
    """
    reported: dict[str, str] = {}
    for item in hook_status or []:
        if isinstance(item, dict):
            status = str(item.get("status", "")).strip().lower()
            if status in HOOK_STATUSES:
                reported[str(item.get("id", "")).strip()] = status
    memory = _memory(project)
    due = {item["id"]: item for item in due_hooks(project, number)}
    evaluation_key = _chapter_key(project, number)
    settled = []
    for record in _ledger(memory):
        if record.get("id") not in due:
            continue
        status = reported.get(str(record["id"]))
        if status is None:
            overlap = term_overlap(str(record.get("text", "")), draft)
            status = "paid" if overlap >= 0.6 else "advanced" if overlap >= 0.3 else "ignored"
        evaluations = record.setdefault("evaluations", {})
        if not isinstance(evaluations, dict):
            evaluations = record["evaluations"] = {}
        evaluations.pop(str(int(number)), None)  # legacy number key
        evaluations[evaluation_key] = status
        settled.append({"id": record["id"], "status": status, "source": "model" if record["id"] in reported else "local"})
    return settled


def hook_ending_check(draft: str, planned: str = "", tail_share: float = 0.15) -> dict[str, Any]:
    """Warning-only check that the chapter actually ends on a hook."""
    text = str(draft or "").strip()
    if not text:
        return {"ok": True, "reason": ""}
    tail = text[-max(80, int(len(text) * tail_share)):]
    markers = [marker for marker in HOOK_MARKERS if marker in tail]
    overlap = term_overlap(planned, tail) if planned else 0.0
    ok = bool(markers) or overlap >= 0.25
    return {
        "ok": ok,
        "markers": markers[:5],
        "planned_overlap": round(overlap, 2),
        "reason": "" if ok else "章末约 15% 未见悬念、未解张力或计划钩子的关键词，结尾推动力可能不足",
    }

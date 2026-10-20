"""Whole-volume review (整卷回顾).

One structured model call reads a planning volume's chapters against the
volume plan and returns pacing per chapter, which planned turning points /
character arcs / subplots were delivered, foreshadowing opened in the volume
and never paid off, repeated scenes, character drift, and a prioritised list
of concrete repair tasks.  Repair tasks go into the project's repair queue
(source ``整卷回顾``) so the existing 精修 flow processes them.

Input sizing: a cloud endpoint receives the full chapter texts (bounded by
``FULL_TEXT_CHAR_CAP`` and the configured context window); a local endpoint
receives each chapter's summary plus its first and last ``EXCERPT_CHARS``
characters, shrunk further when the window is small.

Reports are stored at ``project["volume_reviews"][volume_id]`` with the draft
hash of every chapter so a later edit marks the report stale.
"""
from __future__ import annotations

from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Awaitable, Callable

from app.editorial_workflow import enqueue_repair, ensure_repair_queue, issue_key
from app.evidence_audit import draft_hash
from app.providers import is_local_endpoint, settings_for_workload

SOURCE = "整卷回顾"
FULL_TEXT_CHAR_CAP = 200_000
EXCERPT_CHARS = 600
MIN_EXCERPT_CHARS = 150
SUMMARY_CHARS = 400
PACES = ("拖沓", "合适", "过快")
PLAN_STATUSES = ("delivered", "partial", "missing")
PRIORITIES = ("high", "medium", "low")
DUPLICATE_RATIO = 0.75
MAX_TASKS = 30

Completion = Callable[..., Awaitable[tuple[dict[str, Any], list[str]]]]

VOLUME_REVIEW_PROMPT = """你是长篇小说的整卷责任编辑。请通读输入给出的一整卷正文（或摘要与首尾片段），对照本卷规划做整卷回顾，不改写正文。
返回严格 JSON：
{
  "summary": "本卷整体评价，80-200字",
  "pacing": [{"chapter": 章号, "pace": "拖沓|合适|过快", "note": "一句理由"}],
  "planned": [{"kind": "turning_point|character_arc|subplot|ending_state", "item": "规划条目原文或概括", "status": "delivered|partial|missing", "chapters": [兑现所在章号], "note": "依据"}],
  "unpaid_foreshadowing": [{"chapter": 埋下的章号, "item": "本卷埋下但未回收的伏笔", "note": "建议在哪里回收或是否可留给下一卷"}],
  "repetitions": [{"chapters": [章号], "pattern": "重复的场景、桥段、句式或情绪节拍"}],
  "character_drift": [{"character": "人物名", "chapters": [章号], "issue": "言行与设定或前文不一致之处"}],
  "repair_tasks": [{"chapter": 章号, "category": "节奏|规划兑现|伏笔|重复|人物一致性|其他", "priority": "high|medium|low", "instruction": "可直接执行的具体修改指令", "reason": "为什么要改"}]
}
要求：
1. pacing 必须覆盖输入中的每一章，chapter 用全书章号。
2. planned 逐条核对本卷规划中的关键转折、人物弧、支线和卷末状态；没有证据就判 missing，不得臆测。
3. repair_tasks 按优先级排序，最多 12 条；每条只针对一章，指令要具体到“改哪里、改成什么效果”，不要泛泛而谈。
4. 只评价输入里实际存在的内容；摘要模式下不要对未给出的细节下结论。
5. 只输出 JSON，不要 Markdown 或解释。"""

_INT_LIST = {"type": "array", "items": {"type": "integer"}}
VOLUME_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "pacing": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chapter": {"type": "integer"},
                    "pace": {"type": "string", "enum": list(PACES)},
                    "note": {"type": "string"},
                },
                "required": ["chapter", "pace", "note"],
            },
        },
        "planned": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string"},
                    "item": {"type": "string"},
                    "status": {"type": "string", "enum": list(PLAN_STATUSES)},
                    "chapters": _INT_LIST,
                    "note": {"type": "string"},
                },
                "required": ["kind", "item", "status"],
            },
        },
        "unpaid_foreshadowing": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"chapter": {"type": "integer"}, "item": {"type": "string"}, "note": {"type": "string"}},
                "required": ["item"],
            },
        },
        "repetitions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"chapters": _INT_LIST, "pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
        "character_drift": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"character": {"type": "string"}, "chapters": _INT_LIST, "issue": {"type": "string"}},
                "required": ["issue"],
            },
        },
        "repair_tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chapter": {"type": "integer"},
                    "category": {"type": "string"},
                    "priority": {"type": "string", "enum": list(PRIORITIES)},
                    "instruction": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["chapter", "category", "priority", "instruction"],
            },
        },
    },
    "required": ["summary", "pacing", "planned", "unpaid_foreshadowing", "repetitions", "character_drift", "repair_tasks"],
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, limit: int = 400) -> str:
    if isinstance(value, dict):
        value = "；".join(f"{key}：{item}" for key, item in value.items() if str(item).strip())
    return str(value or "").strip()[:limit]


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Volume and chapter lookup
# ---------------------------------------------------------------------------


def find_volume(project: dict[str, Any], volume_id: str) -> dict[str, Any] | None:
    for volume in project.get("planning", {}).get("volumes", []) or []:
        if isinstance(volume, dict) and str(volume.get("id", "")) == str(volume_id):
            return volume
    return None


def volume_ending_at(project: dict[str, Any], chapter_number: int) -> dict[str, Any] | None:
    for volume in project.get("planning", {}).get("volumes", []) or []:
        if isinstance(volume, dict) and _int(volume.get("chapter_end")) == int(chapter_number):
            return volume
    return None


def volume_chapters(project: dict[str, Any], volume: dict[str, Any]) -> list[tuple[int, dict[str, Any]]]:
    """``(chapter_number, chapter)`` for the volume's chapters that have text."""
    start = max(1, _int(volume.get("chapter_start")) or 1)
    end = max(start, _int(volume.get("chapter_end")) or start)
    rows = []
    chapters = project.get("chapters", []) or []
    for number in range(start, min(end, len(chapters)) + 1):
        chapter = chapters[number - 1]
        if isinstance(chapter, dict) and str(chapter.get("content", "")).strip():
            rows.append((number, chapter))
    return rows


def chapter_hashes(rows: list[tuple[int, dict[str, Any]]]) -> dict[str, str]:
    return {str(chapter.get("id", "")): draft_hash(chapter.get("content", "")) for _number, chapter in rows}


def is_stale(project: dict[str, Any], volume_id: str, entry: dict[str, Any] | None = None) -> bool:
    entry = entry if entry is not None else (project.get("volume_reviews") or {}).get(volume_id)
    volume = find_volume(project, volume_id)
    if not isinstance(entry, dict) or volume is None:
        return True
    return chapter_hashes(volume_chapters(project, volume)) != entry.get("chapter_hashes", {})


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def _plan_lines(volume: dict[str, Any]) -> str:
    lines = [f"卷名：{_text(volume.get('title'), 80)}（第 {volume.get('chapter_start')}–{volume.get('chapter_end')} 章）"]
    for key, label in (("goal", "阶段目标"), ("conflict", "主导冲突"), ("synopsis", "剧情梗概"), ("ending_state", "卷末状态")):
        value = _text(volume.get(key), 1500)
        if value:
            lines.append(f"{label}：{value}")
    for key, label in (
        ("turning_points", "关键转折"), ("character_arcs", "人物弧"), ("subplots", "支线"), ("must_keep", "必须保持"),
    ):
        items = [_text(item, 300) for item in volume.get(key, []) or [] if _text(item)]
        if items:
            lines.append(f"{label}：\n" + "\n".join(f"- {item}" for item in items[:12]))
    routes = [route for route in volume.get("chapters", []) or [] if isinstance(route, dict)]
    if routes:
        lines.append("逐章路线：\n" + "\n".join(
            f"- 第{route.get('number')}章 {_text(route.get('title'), 40)}：{_text(route.get('goal'), 120)}"
            for route in routes[:80]
        ))
    return "\n".join(lines)


def _cast_lines(project: dict[str, Any]) -> str:
    rows = []
    for item in (project.get("characters") or [])[:24]:
        if not isinstance(item, dict) or not _text(item.get("name")):
            continue
        detail = _text(item.get("personality") or item.get("description") or item.get("role"), 120)
        rows.append(f"- {_text(item.get('name'), 30)}：{detail}" if detail else f"- {_text(item.get('name'), 30)}")
    return "\n".join(rows)


def _clip_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = max(1, (limit - 20) // 2)
    return f"{text[:half]}\n……（中段省略 {len(text) - 2 * half} 字）……\n{text[-half:]}"


def input_char_budget(settings: dict[str, Any], *, local: bool, max_tokens: int, overhead: int) -> int:
    """Characters available for chapter material (CJK ≈ one token per char)."""
    window = int(settings.get("context_budget", 0) or 0)
    cap = FULL_TEXT_CHAR_CAP
    if window > 0:
        cap = min(cap, window - max_tokens - overhead - max(300, min(900, window // 24)) - 200)
    return max(2000 if local else 4000, cap)


def build_messages(
    project: dict[str, Any], volume: dict[str, Any], rows: list[tuple[int, dict[str, Any]]],
    settings: dict[str, Any], *, local: bool, max_tokens: int,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    head = f"【本卷规划】\n{_plan_lines(volume)}"
    cast = _cast_lines(project)
    if cast:
        head += f"\n\n【主要人物设定】\n{cast}"
    overhead = len(VOLUME_REVIEW_PROMPT) + len(head) + 400
    budget = input_char_budget(settings, local=local, max_tokens=max_tokens, overhead=overhead)
    count = max(1, len(rows))
    parts = []
    if local:
        summary_limit = min(SUMMARY_CHARS, max(80, budget // count // 3))
        excerpt = max(MIN_EXCERPT_CHARS, min(EXCERPT_CHARS, (budget // count - summary_limit - 60) // 2))
        for number, chapter in rows:
            content = str(chapter.get("content", "")).strip()
            summary = _text(chapter.get("summary"), summary_limit) or "（无摘要）"
            tail = content[-excerpt:] if len(content) > excerpt * 2 else content[excerpt:]
            parts.append(
                f"【第{number}章《{_text(chapter.get('title'), 60)}》 {len(content)} 字】\n摘要：{summary}\n"
                f"开头：{content[:excerpt]}\n结尾：{tail}"
            )
        mode = "summary"
    else:
        total = sum(len(str(chapter.get("content", ""))) for _n, chapter in rows)
        per_chapter = budget // count if total > budget else None
        for number, chapter in rows:
            content = str(chapter.get("content", "")).strip()
            if per_chapter is not None:
                content = _clip_middle(content, max(400, per_chapter - 40))
            parts.append(f"【第{number}章《{_text(chapter.get('title'), 60)}》】\n{content}")
        mode = "full_text" if total <= budget else "clipped_text"
    material = "\n\n".join(parts)
    numbers = [number for number, _chapter in rows]
    user = (
        f"{head}\n\n【本卷正文（{'各章摘要与首尾片段' if local else '全文'}，共 {len(rows)} 章：第 {numbers[0]}–{numbers[-1]} 章）】\n"
        f"{material}\n\n请按要求输出整卷回顾 JSON。pacing 需覆盖第 {', '.join(map(str, numbers))} 章。"
    )
    messages = [{"role": "system", "content": VOLUME_REVIEW_PROMPT}, {"role": "user", "content": user}]
    return messages, {"mode": mode, "input_chars": len(material), "budget_chars": budget}


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------


def _chapter_list(value: Any, allowed: set[int]) -> list[int]:
    items = value if isinstance(value, list) else [value]
    return sorted({number for number in map(_int, items) if number in allowed})


def normalize_report(raw: dict[str, Any], numbers: list[int]) -> dict[str, Any]:
    allowed = set(numbers)
    pacing: dict[int, dict[str, Any]] = {}
    for item in raw.get("pacing") or []:
        if not isinstance(item, dict):
            continue
        number = _int(item.get("chapter"))
        pace = str(item.get("pace", "")).strip()
        if number in allowed and pace in PACES and number not in pacing:
            pacing[number] = {"chapter": number, "pace": pace, "note": _text(item.get("note"), 200)}
    planned = []
    for item in raw.get("planned") or []:
        if isinstance(item, dict) and _text(item.get("item")):
            status = str(item.get("status", "")).strip().lower()
            planned.append({
                "kind": _text(item.get("kind"), 30) or "turning_point",
                "item": _text(item.get("item"), 300),
                "status": status if status in PLAN_STATUSES else "partial",
                "chapters": _chapter_list(item.get("chapters", []), allowed),
                "note": _text(item.get("note"), 300),
            })
    foreshadowing = [
        {"chapter": _int(item.get("chapter")) if _int(item.get("chapter")) in allowed else None,
         "item": _text(item.get("item"), 300), "note": _text(item.get("note"), 300)}
        for item in raw.get("unpaid_foreshadowing") or []
        if isinstance(item, dict) and _text(item.get("item"))
    ]
    repetitions = [
        {"chapters": _chapter_list(item.get("chapters", []), allowed), "pattern": _text(item.get("pattern"), 300)}
        for item in raw.get("repetitions") or []
        if isinstance(item, dict) and _text(item.get("pattern"))
    ]
    drift = [
        {"character": _text(item.get("character"), 40), "chapters": _chapter_list(item.get("chapters", []), allowed),
         "issue": _text(item.get("issue"), 300)}
        for item in raw.get("character_drift") or []
        if isinstance(item, dict) and _text(item.get("issue"))
    ]
    tasks = []
    for item in raw.get("repair_tasks") or []:
        if not isinstance(item, dict):
            continue
        number = _int(item.get("chapter"))
        instruction = _text(item.get("instruction"), 500)
        if number not in allowed or not instruction:
            continue
        priority = str(item.get("priority", "medium")).strip().lower()
        tasks.append({
            "chapter": number,
            "category": _text(item.get("category"), 20) or "其他",
            "priority": priority if priority in PRIORITIES else "medium",
            "instruction": instruction,
            "reason": _text(item.get("reason"), 300),
        })
    order = {"high": 0, "medium": 1, "low": 2}
    tasks.sort(key=lambda task: order[task["priority"]])
    return {
        "summary": _text(raw.get("summary"), 800),
        "pacing": [pacing[number] for number in numbers if number in pacing],
        "planned": planned[:40],
        "unpaid_foreshadowing": foreshadowing[:30],
        "repetitions": repetitions[:20],
        "character_drift": drift[:20],
        "repair_tasks": tasks[:MAX_TASKS],
    }


def make_validator(numbers: list[int]) -> Callable[[dict[str, Any]], None]:
    def validate(result: dict[str, Any]) -> None:
        if not isinstance(result, dict):
            raise ValueError("整卷回顾必须是 JSON 对象")
        for key in ("pacing", "repair_tasks"):
            if not isinstance(result.get(key), list):
                raise ValueError(f"整卷回顾缺少数组字段：{key}")
        clean = normalize_report(result, numbers)
        covered = {item["chapter"] for item in clean["pacing"]}
        if len(covered) < max(1, (len(numbers) + 1) // 2):
            raise ValueError(f"pacing 只覆盖了 {len(covered)}/{len(numbers)} 章，必须逐章给出节奏判断")
    return validate


# ---------------------------------------------------------------------------
# Repair queue
# ---------------------------------------------------------------------------


def enqueue_review_tasks(
    project: dict[str, Any], tasks: list[dict[str, Any]], volume_title: str = ""
) -> list[dict[str, Any]]:
    """Queue repair tasks (source ``整卷回顾``), skipping near-duplicates.

    A task is a duplicate of an open queue item for the same chapter whose
    message is nearly identical (any source).  An exact task the author has
    dismissed before stays dismissed.
    """
    queue = ensure_repair_queue(project)
    chapters = project.get("chapters", []) or []
    results = []
    for task in tasks:
        number = int(task["chapter"])
        chapter = chapters[number - 1] if 0 < number <= len(chapters) else None
        if not isinstance(chapter, dict):
            results.append({**task, "status": "invalid", "repair_id": ""})
            continue
        chapter_id = str(chapter.get("id", ""))
        category = f"{SOURCE}·{task['category']}"
        message = task["instruction"]
        key = issue_key(chapter_id, category, message)
        if any(item.get("issue_key") == key and item.get("status") == "dismissed" for item in queue):
            results.append({**task, "status": "dismissed", "repair_id": ""})
            continue
        duplicate = next(
            (
                item for item in queue
                if str(item.get("chapter_id", "")) == chapter_id
                and str(item.get("status", "queued")) in {"queued", "in_progress"}
                and (
                    item.get("issue_key") == key
                    or SequenceMatcher(None, str(item.get("message", "")), message).ratio() >= DUPLICATE_RATIO
                )
            ),
            None,
        )
        if duplicate is not None:
            results.append({**task, "status": "duplicate", "repair_id": str(duplicate.get("id", ""))})
            continue
        item, _existing = enqueue_repair(
            project,
            chapter,
            {
                "category": category,
                "severity": task["priority"],
                "message": message,
                "suggestion": task.get("reason", ""),
            },
            source=SOURCE,
        )
        if str(item.get("status", "")) == "resolved":
            item["status"] = "queued"
            item.pop("resolved_by", None)
        item["volume"] = volume_title
        results.append({**task, "status": "queued", "repair_id": str(item.get("id", ""))})
    return results


def repair_statuses(project: dict[str, Any], entry: dict[str, Any]) -> dict[str, str]:
    ids = {
        str(task.get("repair_id", ""))
        for task in (entry.get("report") or {}).get("repair_tasks", []) if task.get("repair_id")
    }
    return {
        str(item.get("id", "")): str(item.get("status", "queued"))
        for item in project.get("repair_queue", []) or []
        if isinstance(item, dict) and str(item.get("id", "")) in ids
    }


def public_entry(project: dict[str, Any], volume_id: str) -> dict[str, Any] | None:
    entry = (project.get("volume_reviews") or {}).get(volume_id)
    if not isinstance(entry, dict):
        return None
    return {**entry, "stale": is_stale(project, volume_id, entry), "repair_statuses": repair_statuses(project, entry)}


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


async def run_volume_review(
    project: dict[str, Any], volume_id: str, *, completion: Completion
) -> dict[str, Any]:
    """Review one volume, store the report on ``project`` and queue repairs.

    Mutates ``project`` (``volume_reviews`` and ``repair_queue``); the caller
    persists it.  Raises ``KeyError`` for an unknown volume and ``ValueError``
    when the volume has no written chapters.
    """
    volume = find_volume(project, volume_id)
    if volume is None:
        raise KeyError(volume_id)
    rows = volume_chapters(project, volume)
    if not rows:
        raise ValueError("本卷还没有已写正文的章节，无法整卷回顾")
    settings = project.get("settings", {}) or {}
    routed = settings_for_workload(settings, "critic")
    local = is_local_endpoint(routed)
    max_tokens = 3200 if local else 8000
    messages, meta = build_messages(project, volume, rows, routed, local=local, max_tokens=max_tokens)
    numbers = [number for number, _chapter in rows]
    raw, warnings = await completion(
        settings,
        messages,
        max_tokens=max_tokens,
        timeout_seconds=900 if local else 600,
        temperature=0.2,
        validate=make_validator(numbers),
        workload="critic",
        schema=VOLUME_REVIEW_SCHEMA,
    )
    report = normalize_report(raw, numbers)
    report["repair_tasks"] = enqueue_review_tasks(project, report["repair_tasks"], str(volume.get("title", "")))
    entry = {
        "volume_id": str(volume_id),
        "volume_title": str(volume.get("title", "")),
        "chapter_start": numbers[0],
        "chapter_end": numbers[-1],
        "planned_range": [_int(volume.get("chapter_start")), _int(volume.get("chapter_end"))],
        "created_at": _now(),
        "model": str(routed.get("model", "")),
        "mode": meta["mode"],
        "input_chars": meta["input_chars"],
        "warnings": [str(item) for item in warnings][:10],
        "report": report,
        "chapter_hashes": chapter_hashes(rows),
    }
    reviews = project.get("volume_reviews")
    if not isinstance(reviews, dict):
        reviews = {}
        project["volume_reviews"] = reviews
    reviews[str(volume_id)] = entry
    return entry

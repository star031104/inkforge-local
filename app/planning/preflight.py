"""开写前设定预检：设定 × 世界书年表 × 人物卡 × 分卷路线的一致性检查。

Two layers, both generic (no book, era or name is built in):

* ``deterministic_conflicts`` — no model.  Uses only the project's own data:
  chapter/route ``story_year`` against character ``born/died/enters_year``,
  ``active: false`` characters used in early routes, ``governance.era_terms``
  and author ``banned_phrases`` in route text, and years written in the
  premise / current focus against years in constant world entries that the
  first routes rely on.
* one small structured model call (``run_preflight(..., completion=...)``)
  that receives the same materials and reports only real contradictions
  between them (chronology, ability vs hard limits, knowledge, entrances).

The merged result is ``{ok, high_count, conflicts, warnings, checked_at,
volume_id}``; callers store it at ``project["planning"]["preflight"]``.
A model failure never blocks: the deterministic result is returned with a
warning.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.core.text import bounded_excerpt
from app.planning.route_validation import route_era_conflicts, route_story_year
from app.story.history import character_names, extract_years, format_year

Completion = Callable[..., Awaitable[tuple[dict[str, Any], list[str]]]]

SEVERITIES = ("high", "medium", "low")
KINDS = ("chronology", "ability", "knowledge", "character_entrance", "rule", "other")
FIX_TARGETS = ("setting", "route", "character")
MAX_CONFLICTS = 40
MODEL_MAX_CONFLICTS = 12
ROUTE_FIELDS = ("title", "goal", "conflict", "turning_point", "ending_hook")

_STRING = {"type": "string"}
PREFLIGHT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conflicts": {
            "type": "array",
            "maxItems": MODEL_MAX_CONFLICTS,
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": list(SEVERITIES)},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "where": _STRING,
                    "detail": _STRING,
                    "suggestion": _STRING,
                    "fix_target": {"type": "string", "enum": list(FIX_TARGETS)},
                },
                "required": ["severity", "kind", "where", "detail", "suggestion", "fix_target"],
            },
        }
    },
    "required": ["conflicts"],
}

PREFLIGHT_PROMPT = """你是长篇小说开写前的设定一致性检查员。你会收到作者的设定（核心构想、近期焦点、全书硬规则）、常驻世界书条目、人物卡和一卷的逐章路线。
只报告这些材料之间真实存在的矛盾，例如：
- chronology：路线依赖的事件、制度或物件按世界书年表在故事起点之后才出现；人物在故事年份已死或未生。
- ability：路线要求人物做到其人物卡“硬限制/能力边界”明确禁止或做不到的事。
- knowledge：路线让人物使用其此时不可能知道的信息。
- character_entrance：路线让人物在其登场年份或启用之前出场。
- rule：路线违反全书硬规则。
不要报告文风、节奏、好不好看、建议加强之类的意见；没有把握的推测不要报告。材料没有矛盾就返回空数组。
每条 where 写成“设定”“人物卡：某人”“世界书：条目名”或“第N章路线”；detail 用一两句话点明是哪两处材料冲突；suggestion 给出最小改法；fix_target 指出应改 setting（设定/世界书）、route（路线）还是 character（人物卡）。
severity：会让大量章节被判错或剧情无法成立的为 high；局部可修的为 medium；轻微为 low。最多 12 条。只返回 JSON：{"conflicts":[...]}"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _volumes(project: dict[str, Any]) -> list[dict[str, Any]]:
    planning = project.get("planning") if isinstance(project.get("planning"), dict) else {}
    return [item for item in planning.get("volumes", []) or [] if isinstance(item, dict)]


def _route_text(route: dict[str, Any]) -> str:
    return " ".join(str(route.get(field, "")) for field in ROUTE_FIELDS) + " " + " ".join(
        str(item) for item in route.get("must_keep", []) or []
    )


def _characters(project: dict[str, Any]) -> list[dict[str, Any]]:
    items = project.get("characters")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def select_volume(project: dict[str, Any], volume_id: str | None = None) -> dict[str, Any] | None:
    """The requested volume, else the first volume with routes and unwritten chapters."""
    volumes = _volumes(project)
    if volume_id:
        return next((item for item in volumes if str(item.get("id", "")) == str(volume_id)), None)
    chapters = project.get("chapters", []) if isinstance(project.get("chapters"), list) else []

    def unwritten(volume: dict[str, Any]) -> bool:
        start = int(volume.get("chapter_start", 1) or 1)
        end = int(volume.get("chapter_end", start) or start)
        return any(
            number > len(chapters)
            or not str((chapters[number - 1] or {}).get("content", "")).strip()
            for number in range(start, end + 1)
        )

    with_routes = [item for item in volumes if item.get("chapters")]
    return next((item for item in with_routes if unwritten(item)), None) or (
        with_routes[0] if with_routes else (volumes[0] if volumes else None)
    )


def _conflict(
    severity: str, kind: str, where: str, detail: str, suggestion: str, fix_target: str,
    source: str = "rule",
) -> dict[str, Any]:
    return {
        "severity": severity if severity in SEVERITIES else "medium",
        "kind": kind if kind in KINDS else "other",
        "where": where.strip()[:80],
        "detail": detail.strip()[:400],
        "suggestion": suggestion.strip()[:300],
        "fix_target": fix_target if fix_target in FIX_TARGETS else "route",
        "source": source,
    }


_START_CUES = ("穿越到", "穿越至", "回到", "醒来于", "故事开始", "故事始于", "开篇", "起点", "开始于", "始于")
_RULE_AFTER_YEAR = re.compile(r"^\s*(?:之前|以前|以后|之后|起|前后|左右|至今)")


def premise_start_year(project: dict[str, Any]) -> int | None:
    """The year the story starts.

    Priority: the earliest ``story_year`` the author set on a chapter; then a
    year written right after a start cue in the premise/current focus
    ("穿越到……前246年"); then the first year written there.  Other years in the
    premise (e.g. "某王前247年即位、次年改元") must not win just for being
    earlier.
    """
    chapter_years = []
    for chapter in project.get("chapters", []) or []:
        if isinstance(chapter, dict):
            try:
                if chapter.get("story_year") not in (None, ""):
                    chapter_years.append(int(chapter["story_year"]))
            except (TypeError, ValueError):
                continue
    if chapter_years:
        return min(chapter_years)
    text = f"{project.get('premise', '')}\n{project.get('current_focus', '')}"
    for cue in _START_CUES:
        index = text.find(cue)
        if index >= 0:
            years = extract_years(text[index:index + 40])
            if years:
                return years[0]
    years = extract_years(text)
    return years[0] if years else None


def _constant_entries(project: dict[str, Any]) -> list[dict[str, Any]]:
    entries = project.get("world_entries")
    return [
        item for item in (entries if isinstance(entries, list) else [])
        if isinstance(item, dict) and item.get("constant") and item.get("enabled", True) is not False
    ]


def _entry_terms(entry: dict[str, Any]) -> list[str]:
    keys = entry.get("keys", [])
    if isinstance(keys, str):
        keys = re.split(r"[,，\n]", keys)
    terms = [str(item).strip() for item in keys or [] if len(str(item).strip()) >= 2]
    title = str(entry.get("title", "")).strip()
    if 2 <= len(title) <= 12:
        terms.append(title)
    return list(dict.fromkeys(terms))


_CLAUSE_SPLIT = re.compile(r"[；;。\n]")


def dated_events(project: dict[str, Any]) -> list[tuple[int, str, str]]:
    """``(year, event clause, entry title)`` from constant world entries.

    Only clauses that state something happening in a year count
    ("前246年郑国渠开工").  Rule boundaries such as "前221年之前称王" are skipped.
    """
    events: list[tuple[int, str, str]] = []
    for entry in _constant_entries(project):
        title = str(entry.get("title", "") or "未命名条目")
        for clause in _CLAUSE_SPLIT.split(str(entry.get("content", ""))):
            for match in re.finditer(r"(?:公元前|前|公元)?\s*\d{1,4}\s*年", clause):
                years = extract_years(match.group(0))
                if not years:
                    continue
                rest = clause[match.end():]
                if _RULE_AFTER_YEAR.match(rest):
                    continue
                # The event is the whole clause around this year (the subject
                # may come first: "运河于1204年开凿完成"), cut at the next year.
                before = re.split(r"(?:公元前|前|公元)?\s*\d{1,4}\s*年", clause[:match.start()])[-1]
                after = re.split(r"(?:公元前|前|公元)?\s*\d{1,4}\s*年", rest, maxsplit=1)[0]
                event = f"{before}{match.group(0)}{after}".strip("，,：: ")
                if len(event) >= 4:
                    events.append((years[0], event, title))
    return events


def _event_terms(project: dict[str, Any], event: str) -> list[str]:
    """World-book keys/titles that occur in an event clause."""
    candidates: list[str] = []
    for entry in project.get("world_entries", []) or []:
        if isinstance(entry, dict):
            candidates.extend(_entry_terms(entry))
    # Character names and book titles are deliberately not used: a person
    # named in a dated event ("X封侯") exists before it, and a book is being
    # written before the year it is completed.
    return [term for term in dict.fromkeys(candidates) if len(term) >= 2 and term in event]


def chronology_conflicts(project: dict[str, Any], volume: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Routes set before a dated event of the world-book chronology yet relying on it.

    Conservative by construction: only "year + event" clauses are used, and a
    route is reported only when it names a specific term of that event
    (a world-book key, a character name or a 《title》) and is set before it.
    """
    if not isinstance(volume, dict):
        return []
    start = premise_start_year(project)
    routes = [item for item in volume.get("chapters", []) or [] if isinstance(item, dict)]
    issues: list[dict[str, Any]] = []
    for year, event, title in dated_events(project):
        terms = _event_terms(project, event)
        if not terms:
            continue
        using = []
        for route in routes:
            reference = route_story_year(project, route, volume)
            reference = start if reference is None else reference
            if reference is None or year <= reference:
                continue
            text = _route_text(route)
            hit = next((term for term in terms if term in text), "")
            if hit:
                using.append((int(route.get("number", 0) or 0), hit, reference))
        if not using:
            continue
        numbers = "、".join(f"第{number}章" for number, _, _ in using[:6])
        reference = using[0][2]
        # A keyword heuristic: worth a look, never a reason to block writing.
        issues.append(_conflict(
            "medium", "chronology", f"设定/世界书「{title}」",
            f"世界书「{title}」记载“{event[:40]}”，但这些章节的故事时间是"
            f"{format_year(reference)}，早于此事；{numbers}路线已经用到“{using[0][1]}”。",
            f"二选一：把故事起点改到{format_year(year)}或之后；或修改相关路线，"
            "不要在此事发生前依赖它。",
            "setting",
        ))
    return issues


def route_conflicts(project: dict[str, Any], volumes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Character years / entrance, era terms and banned phrases in route text."""
    issues: list[dict[str, Any]] = []
    all_volumes = _volumes(project)
    opening = all_volumes[0] if all_volumes else None
    start = premise_start_year(project)
    for volume in volumes:
        for route in volume.get("chapters", []) or []:
            if not isinstance(route, dict):
                continue
            where = f"第{int(route.get('number', 0) or 0)}章路线"
            # Routes of the opening volume without their own year happen at the
            # start year the author wrote in the premise.
            if volume is opening and start is not None and route_story_year(project, route, volume) is None:
                route = dict(route, story_year=start)
            for item in route_era_conflicts(project, route, volume):
                category = str(item.get("category", ""))
                message = str(item.get("message", ""))
                if category == "作者禁用语":
                    kind, fix = "rule", "route"
                elif "出场过早" in message:
                    # The plan contradicts the author's own character card.
                    kind, fix = "character_entrance", "route"
                    item = dict(item, severity="high")
                else:
                    kind, fix = "chronology", "route"
                issues.append(_conflict(
                    str(item.get("severity", "medium")), kind, where, message,
                    str(item.get("suggestion", "")) or "修改路线或核对人物卡年份", fix,
                ))
    return issues


def inactive_character_conflicts(project: dict[str, Any], volume: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Characters marked ``active: false`` used in the first routes without any year data.

    With years the route check above already decides; without them this is a
    low-severity reminder, never a blocker.
    """
    if not isinstance(volume, dict):
        return []
    issues: list[dict[str, Any]] = []
    for character in _characters(project):
        if character.get("active") is not False or character.get("enters_year") not in (None, ""):
            continue
        names = character_names(character)
        if not names:
            continue
        numbers = [
            int(route.get("number", 0) or 0)
            for route in volume.get("chapters", []) or []
            if isinstance(route, dict)
            and route_story_year(project, route, volume) is None
            and any(name in _route_text(route) for name in names)
        ]
        if numbers:
            name = str(character.get("name", "")).strip()
            issues.append(_conflict(
                "low", "character_entrance", f"人物卡：{name}",
                f"{name}的人物卡标记为尚未启用，但"
                + "、".join(f"第{n}章" for n in numbers[:6]) + "路线让其出场。",
                "确认此人此时是否应登场：应登场就启用人物卡，否则改写路线。",
                "character",
            ))
    return issues


def deterministic_conflicts(project: dict[str, Any], volume_id: str | None = None) -> list[dict[str, Any]]:
    volume = select_volume(project, volume_id)
    scope = [volume] if volume_id and volume else _volumes(project)
    return (
        chronology_conflicts(project, volume)
        + route_conflicts(project, scope)
        + inactive_character_conflicts(project, volume)
    )


def build_preflight_messages(project: dict[str, Any], volume: dict[str, Any] | None) -> list[dict[str, str]]:
    cards = [
        {
            key: item.get(key)
            for key in ("name", "role", "hard_limits", "knowledge", "born_year", "died_year", "enters_year", "active")
            if item.get(key) not in (None, "", [])
        }
        for item in _characters(project)
    ][:30]
    world = [
        {"title": str(item.get("title", "")), "content": bounded_excerpt(str(item.get("content", "")), 600)}
        for item in _constant_entries(project)
    ][:20]
    routes = [
        {
            "chapter": int(route.get("number", 0) or 0),
            **{field: str(route.get(field, "")) for field in ROUTE_FIELDS},
            **({"story_year": route["story_year"]} if route.get("story_year") is not None else {}),
        }
        for route in (volume or {}).get("chapters", []) or []
        if isinstance(route, dict)
    ][:40]
    context = f"""【核心构想】
{bounded_excerpt(str(project.get('premise', '')), 2000) or '（空）'}
【近期焦点】
{bounded_excerpt(str(project.get('current_focus', '')), 1200) or '（空）'}
【全书硬规则】
{bounded_excerpt(str(project.get('book_rules', '')), 2000) or '（空）'}
【常驻世界书】
{bounded_excerpt(json.dumps(world, ensure_ascii=False), 6000)}
【人物卡】
{bounded_excerpt(json.dumps(cards, ensure_ascii=False), 6000)}
【{(volume or {}).get('title', '本卷')}逐章路线】
{bounded_excerpt(json.dumps(routes, ensure_ascii=False), 9000)}"""
    return [
        {"role": "system", "content": PREFLIGHT_PROMPT},
        {"role": "user", "content": context},
    ]


def validate_preflight_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict) or not isinstance(payload.get("conflicts"), list):
        raise ValueError("预检结果缺少 conflicts 数组")


def normalize_model_conflicts(payload: Any) -> list[dict[str, Any]]:
    raw = payload.get("conflicts", []) if isinstance(payload, dict) else []
    clean = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not str(item.get("detail", "")).strip():
            continue
        clean.append(_conflict(
            str(item.get("severity", "medium")), str(item.get("kind", "other")),
            str(item.get("where", "")) or "设定", str(item.get("detail", "")),
            str(item.get("suggestion", "")), str(item.get("fix_target", "route")), source="model",
        ))
    return clean[:MODEL_MAX_CONFLICTS]


def merge_result(
    conflicts: list[dict[str, Any]], warnings: list[str], volume: dict[str, Any] | None
) -> dict[str, Any]:
    seen: set[tuple[str, str]] = set()
    merged = []
    for item in conflicts:
        key = (item["where"], item["detail"][:60])
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    merged.sort(key=lambda item: SEVERITIES.index(item["severity"]))
    merged = merged[:MAX_CONFLICTS]
    high = sum(1 for item in merged if item["severity"] == "high")
    return {
        "ok": high == 0,
        "high_count": high,
        "conflicts": merged,
        "warnings": [str(item) for item in warnings][:6],
        "checked_at": _now(),
        "volume_id": str((volume or {}).get("id", "")),
    }


async def run_preflight(
    project: dict[str, Any],
    volume_id: str | None = None,
    *,
    completion: Completion | None = None,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    """Deterministic checks plus one optional model pass; never raises on model failure."""
    volume = select_volume(project, volume_id)
    if volume_id and volume is None:
        raise KeyError(volume_id)
    conflicts = deterministic_conflicts(project, volume_id)
    warnings: list[str] = []
    if completion is not None and volume is not None and volume.get("chapters"):
        try:
            payload, model_warnings = await completion(
                project.get("settings", {}),
                build_preflight_messages(project, volume),
                max_tokens=1400,
                timeout_seconds=timeout_seconds,
                temperature=0.1,
                validate=validate_preflight_payload,
                workload="planning",
                schema=PREFLIGHT_SCHEMA,
            )
            conflicts += normalize_model_conflicts(payload)
            warnings.extend(str(item) for item in model_warnings or [])
        except Exception as exc:  # noqa: BLE001 - the model layer is advisory
            warnings.append(f"模型预检未完成（{bounded_excerpt(str(exc) or exc.__class__.__name__, 160)}），仅显示本地规则检查结果。")
    elif completion is not None:
        warnings.append("所选分卷还没有逐章路线，只做了本地规则检查。")
    return merge_result(conflicts, warnings, volume)


def store_preflight(project: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    planning = project.setdefault("planning", {})
    if isinstance(planning, dict):
        planning["preflight"] = result
    return project

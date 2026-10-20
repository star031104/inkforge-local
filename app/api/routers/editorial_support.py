"""Shared editorial-route pieces: proxies, result type and audit helpers."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter

from app.story.continuity_state import injury_states, item_states
from app.services.antislop import banned_phrases, detect_slop
from app.services.candidate_selection import is_key_chapter
from app.services.focused_critics import has_previous_run


class _StoreProxy:
    def __init__(self, provider: Callable[[], Any]):
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider(), name)


class _LiveCallable:
    def __init__(self, provider: Callable[[], Callable[..., Any]]):
        self._provider = provider

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._provider()(*args, **kwargs)


@dataclass(frozen=True)
class EditorialRoutes:
    router: APIRouter
    chapter_memory: Callable[..., Any]
    chapter_audit: Callable[..., Any]


class EditorialContext:
    """Attribute bag shared by the editorial route modules."""

    def __init__(self, **values: Any):
        self.__dict__.update(values)

    def register(self, *functions: Callable[..., Any]) -> None:
        for function in functions:
            setattr(self, function.__name__, function)


_NAMED_ENTITY_RES = (
    # "一个名叫阿七的小厮": introduction phrases name an entity directly.
    re.compile(r"(?:名叫|叫做|名为|唤作|自称|人称|号称)([\u4e00-\u9fff]{2,4})"),
    # "，沈鸢笑道": a clause-initial speaker tag (verified below, it over-matches).
    re.compile(r"(?:^|[。！？\n“”，])([\u4e00-\u9fff]{2,3})(?:说道|问道|答道|笑道|喝道)"),
)
_GENERIC_ROLES = {
    "少年", "少女", "老人", "男人", "女人", "众人", "那人", "此人", "对方", "掌柜", "小二", "侍卫",
    "他们", "她们", "我们", "你们", "老者", "来人", "旁人", "有人",
}
# A speaker tag starting with a pronoun is "她冷笑道 / 他沉声喝道", not a name.
_PRONOUN_STARTS = tuple("他她它我你您这那谁其众")
# Function characters that end a captured name ("阿七的小厮" → "阿七").
_NAME_STOP = re.compile(r"[的了着是在和与将把被向对从也都便就又却]")


def _clean_entity(name: str) -> str:
    return _NAME_STOP.split(name, maxsplit=1)[0]


def new_named_entities(project: dict[str, Any], draft: str, known_text: str = "") -> list[str]:
    """Names introduced by the draft that no character, lore entry or memory knows.

    Speaker tags only count when the name also recurs elsewhere in the draft
    and does not start with a pronoun, so ordinary dialogue ("她冷笑道")
    never looks like a new character.
    """
    known_parts = [known_text]
    for character in project.get("characters", []) or []:
        if isinstance(character, dict):
            known_parts.append(str(character.get("name", "")))
            known_parts.append(json.dumps(character.get("aliases", ""), ensure_ascii=False))
    for entry in project.get("world_entries", []) or []:
        if isinstance(entry, dict):
            known_parts.append(str(entry.get("title", "")) + json.dumps(entry.get("keys", ""), ensure_ascii=False))
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    known_parts.extend(str(item.get("text", "")) for item in memory.get("facts", []) if isinstance(item, dict))
    known = "\n".join(known_parts)
    found: list[str] = []
    for position, pattern in enumerate(_NAMED_ENTITY_RES):
        for match in pattern.finditer(draft):
            name = _clean_entity(match.group(1))
            if len(name) < 2 or name in found or name in known:
                continue
            if name in _GENERIC_ROLES or any(name.endswith(role) for role in _GENERIC_ROLES):
                continue
            if position == 1 and (name.startswith(_PRONOUN_STARTS) or draft.count(name) < 2):
                continue
            found.append(name)
    return found[:8]


def _audit_attempts(chapter: dict[str, Any]) -> int:
    """Persisted audit attempts (survives restarts, unlike the in-process run memory)."""
    workflow = chapter.get("workflow", {}) if isinstance(chapter.get("workflow"), dict) else {}
    stages = workflow.get("stages", {}) if isinstance(workflow.get("stages"), dict) else {}
    audit = stages.get("audit", {}) if isinstance(stages.get("audit"), dict) else {}
    return int(audit.get("attempts", 0) or 0)


def critic_risk_signals(
    project: dict[str, Any],
    index: int,
    chapter: dict[str, Any],
    draft: str,
    *,
    local_checks: dict[str, Any],
    continuity_issues: list[dict[str, Any]],
    agenda: list[dict[str, Any]],
    hooks: list[dict[str, Any]],
    audit_key: str,
) -> dict[str, Any]:
    """Cheap deterministic signals for the risk-gated critic budget."""
    number = index + 1
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    tracked = [name for name in item_states(memory, number) if name and name in draft]
    tracked += [
        str(state.get("character", ""))
        for state in injury_states(memory, number).values()
        if state.get("state") in {"fresh", "healing"} and str(state.get("character", "")) in draft
    ]
    continuity_keys = {id(item) for item in continuity_issues}
    mechanical = [
        item for item in local_checks.get("issues", []) or []
        if isinstance(item, dict) and id(item) not in continuity_keys
        and str(item.get("severity", "")).lower() in {"high", "medium"}
    ]
    slop_excess = sum(
        item["count"] - item["allow"] for item in detect_slop(draft, banned_phrases(project))
    )
    overdue_threads = sum(
        1 for item in agenda
        if isinstance(item, dict) and isinstance(item.get("_lifecycle"), dict) and item["_lifecycle"].get("stale")
    )
    execution = chapter.get("execution", {}) if isinstance(chapter.get("execution"), dict) else {}
    prior_debt = str(execution.get("status", "")) == "quality_debt" or any(
        isinstance(item, dict) and item.get("chapter_id") == chapter.get("id")
        and item.get("status") in {"queued", "in_progress"}
        for item in project.get("repair_queue", []) or []
    )
    route_text = json.dumps(
        {"route": chapter.get("route", {}), "plan": chapter.get("plan", {})}, ensure_ascii=False
    )
    return {
        "key_chapter": is_key_chapter(chapter, index, len(project.get("chapters", []))),
        "new_entities": new_named_entities(project, draft, route_text),
        "continuity_mentions": len(tracked),
        "continuity_conflicts": len(continuity_issues),
        "mechanical_issues": len(mechanical),
        "slop_excess": slop_excess,
        # Only overdue obligations add risk: every chapter has the previous
        # chapter's fresh hook in its window, which is routine, not risky.
        "obligations": overdue_threads + sum(
            1 for item in hooks if isinstance(item, dict) and item.get("status") == "debt"
        ),
        "prior_debt": prior_debt,
        "first_audit": not has_previous_run(audit_key) and _audit_attempts(chapter) <= 1,
    }

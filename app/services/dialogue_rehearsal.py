"""Dialogue rehearsal: each major character improvises in the first person.

HoLLMwood (2024) found that letting characters "act out" a scene from their
own profile was the step that most improved interest, and novel-studio keeps
characters as agents that only see their own observations.  InkForge already
keeps a per-character knowledge ledger, so each rehearsal call receives only:

* the character's profile and voice samples,
* what that character knows as of this chapter (temporal view),
* the scene beat.

The resulting intents/lines/actions are injected into the prose prompt as
"对白演出素材" — raw material, not text to copy.  A deterministic guard drops
lines that echo facts the character cannot know (author-only/private facts
not in ``known_by``, or other characters' secrets).
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from ..domain.json_schemas import REHEARSAL_SCHEMA
from ..prompts import render_epistemic_context
from ..temporal_context import chapter_context
from .craft_settings import craft_setting

Completion = Callable[..., Awaitable[tuple[dict[str, Any], list[str]]]]

REHEARSAL_SYSTEM = (
    "你现在是小说中的一个人物，用第一人称为即将发生的场景做即兴排演。"
    "你只知道下面“你确实知道的事”，绝不能说出或暗示你不知道的信息；"
    "说话方式必须符合你的性格与声纹样本。不要写旁白、不要写其他人物的台词。"
    "只输出 JSON：{\"intent\":\"此场你想达成什么（20字内）\","
    "\"lines\":[\"你可能说出口的台词，最多4句\"],\"actions\":[\"你会做的具体动作，最多3个\"],"
    "\"withholds\":\"你刻意不说出口的事（可空）\"}"
)


def _text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "；".join(str(item) for item in value if str(item).strip())
    return str(value or "").strip()


def scene_beat(chapter: dict[str, Any]) -> str:
    route = chapter.get("route", {}) if isinstance(chapter.get("route"), dict) else {}
    plan = chapter.get("plan", {}) if isinstance(chapter.get("plan"), dict) else {}
    parts = []
    for key, label in (
        ("goal", "本场目标"), ("conflict", "阻力"), ("turning_point", "转折"), ("ending_hook", "收束"),
    ):
        value = _text(route.get(key) or plan.get(key))
        if value:
            parts.append(f"{label}：{value}")
    return "\n".join(parts)


def rehearsal_cast(project: dict[str, Any], chapter: dict[str, Any], limit: int = 4) -> list[dict[str, Any]]:
    """Major characters named in this chapter's route/plan (needs at least two)."""
    route_text = " ".join(
        str(chapter.get(key, "")) for key in ("route", "plan", "scene_goal", "summary")
    )
    pov = str((chapter.get("plan") or {}).get("pov_character", "")) if isinstance(chapter.get("plan"), dict) else ""
    cast = []
    for character in project.get("characters", []):
        name = str(character.get("name", "")).strip()
        if not name or not character.get("active", True):
            continue
        major = str(character.get("importance", "supporting")) in {"main", "major"}
        if name in route_text or name == pov:
            cast.append((0 if major else 1, character))
    cast.sort(key=lambda item: item[0])
    selected = [item[1] for item in cast][:limit]
    return selected if len(selected) >= 2 else []


def forbidden_facts(view: dict[str, Any], name: str) -> list[str]:
    facts = view.get("memory", {}).get("facts", []) if isinstance(view.get("memory"), dict) else []
    forbidden = [
        str(item.get("text", "")).strip()
        for item in facts
        if isinstance(item, dict)
        and (item.get("author_only", False) or str(item.get("visibility", "objective")) == "private")
        and name not in [str(value) for value in item.get("known_by", [])]
        and str(item.get("text", "")).strip()
    ]
    for character in view.get("characters", []):
        if str(character.get("name", "")).strip() == name:
            continue
        secret = _text(character.get("secrets"))
        if secret:
            forbidden.append(secret)
    return forbidden


def _grams(text: str, size: int = 4) -> set[str]:
    compact = re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’—…（）()《》]", "", str(text or ""))
    return {compact[index:index + size] for index in range(len(compact) - size + 1)}


def leaked_secret(line: str, forbidden: list[str], allowed: str) -> str:
    """Return the forbidden fact a line echoes (≥2 shared 4-grams absent from allowed text)."""
    allowed_grams = _grams(allowed)
    line_grams = _grams(line) - allowed_grams
    if not line_grams:
        return ""
    for fact in forbidden:
        if len(line_grams & _grams(fact)) >= 2:
            return fact
    return ""


def _voice_samples(project: dict[str, Any], name: str, limit: int = 2) -> list[str]:
    samples = project.get("voice_lab", {}).get("samples", []) if isinstance(project.get("voice_lab"), dict) else []
    return [
        str(item.get("text", ""))[:160]
        for item in samples
        if isinstance(item, dict) and item.get("character") == name and item.get("polarity", "positive") == "positive"
    ][:limit]


def should_rehearse(project: dict[str, Any], *, director: bool, config: dict[str, Any] | None = None) -> bool:
    mode = craft_setting(project, "dialogue_rehearsal", config)
    return mode == "always" or (mode == "auto" and director)


async def rehearse_dialogue(
    project: dict[str, Any],
    index: int,
    settings: dict[str, Any],
    completion: Completion,
) -> dict[str, Any]:
    chapters = project.get("chapters", [])
    if not 0 <= index < len(chapters):
        return {"material": "", "cast": [], "dropped": [], "warnings": []}
    view = chapter_context(project, index)
    chapter = view["chapters"][index]
    cast = rehearsal_cast(view, chapter)
    report: dict[str, Any] = {"material": "", "cast": [], "dropped": [], "warnings": []}
    if not cast:
        return report
    beat = scene_beat(chapter)
    blocks: list[str] = []
    for character in cast:
        name = str(character.get("name", "")).strip()
        knowledge = render_epistemic_context(view, index, beat, {name})
        allowed = "\n".join([
            knowledge, beat, _text(character.get("personality")), _text(character.get("role")),
            _text(character.get("knowledge_baseline", character.get("knowledge"))),
        ])
        samples = _voice_samples(view, name)
        user = (
            f"【你是】{name}｜身份：{_text(character.get('role'))}\n"
            f"【性格】{_text(character.get('personality'))}\n"
            f"【说话方式】{_text(character.get('voice'))}\n"
            f"【绝不会做的事】{_text(character.get('hard_limits'))}\n"
            f"【此刻状态】{_text(character.get('state'))}；位置：{_text(character.get('location'))}\n"
            + (f"【声纹样本】\n" + "\n".join(f"- {item}" for item in samples) + "\n" if samples else "")
            + f"【你确实知道的事（截至本章开始）】\n{knowledge}\n"
            f"【即将发生的场景】\n{beat or '按本章路线推进。'}"
        )
        try:
            result, warnings = await completion(
                settings,
                [{"role": "system", "content": REHEARSAL_SYSTEM}, {"role": "user", "content": user}],
                temperature=0.85,
                max_tokens=420,
                timeout_seconds=90,
                workload="writing",
                schema=REHEARSAL_SCHEMA,
            )
            report["warnings"].extend(warnings)
        except Exception as exc:  # noqa: BLE001 - rehearsal is optional
            report["warnings"].append(f"{name} 的对白排演失败：{exc}")
            continue
        forbidden = forbidden_facts(view, name)
        lines = []
        for line in result.get("lines", []) if isinstance(result.get("lines"), list) else []:
            text = str(line).strip()[:120]
            if not text:
                continue
            leak = leaked_secret(text, forbidden, allowed)
            if leak:
                report["dropped"].append({"character": name, "line": text, "secret": leak[:60]})
                continue
            lines.append(text)
        actions = [
            str(item).strip()[:80]
            for item in (result.get("actions", []) if isinstance(result.get("actions"), list) else [])
            if str(item).strip() and not leaked_secret(str(item), forbidden, allowed)
        ][:3]
        intent = str(result.get("intent", "")).strip()[:40]
        withholds = str(result.get("withholds", "")).strip()[:60]
        if not (lines or actions or intent):
            continue
        report["cast"].append(name)
        block = [f"【{name}】意图：{intent or '未说明'}"]
        if lines:
            block.append("可能的台词：" + " / ".join(f"“{item.strip('“”')}”" for item in lines[:4]))
        if actions:
            block.append("动作：" + "；".join(actions))
        if withholds:
            block.append("刻意不说：" + withholds)
        blocks.append("\n".join(block))
    if len(report["cast"]) >= 2:
        report["material"] = (
            "以下是各人物按自身知情范围即兴排演出的意图、台词与动作，只作素材：保留各自的口吻差异与"
            "信息差，可删改、可重新组织，不得照抄成对白清单，也不得让人物说出其不知道的事。\n\n"
            + "\n\n".join(blocks)
        )
    return report

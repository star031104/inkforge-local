"""Item and injury state machines (pure, no I/O).

Long-form drift most often shows up as small physical contradictions: a sword
that was lost two chapters ago is drawn again, or a deep wound is forgotten by
the next scene.  ConStory-Bench calls these "fact detail" and "character
ability" errors; SCORE tracks key items as active / lost / destroyed.

State is stored as an append-only, chapter-stamped event log in
``memory.item_events`` and ``memory.injury_events``.  The current state as of
any chapter is a fold over events from earlier chapters, so chapter-relative
views (``temporal_context.chapter_context``) stay correct without extra code:
events from the current or later chapters are filtered out like other
derived memory.
"""
from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from typing import Any

ITEM_STATES = ("active", "lost", "destroyed", "transferred")
INJURY_STATES = ("fresh", "healing", "healed")

ITEM_STATE_LABELS = {
    "active": "在手可用",
    "lost": "已遗失",
    "destroyed": "已毁坏",
    "transferred": "已转交",
}
INJURY_STATE_LABELS = {"fresh": "新伤", "healing": "恢复中", "healed": "已痊愈"}

# Chapters a fresh injury normally needs before "healed" is plausible.
INJURY_MIN_HEAL_CHAPTERS = {"light": 1, "medium": 2, "severe": 4}

ITEM_STATE_ALIASES = {
    "持有": "active", "在手": "active", "获得": "active", "取回": "active", "找回": "active",
    "丢失": "lost", "遗失": "lost", "失去": "lost", "被夺": "lost", "被抢": "lost",
    "损毁": "destroyed", "毁坏": "destroyed", "折断": "destroyed", "烧毁": "destroyed", "碎裂": "destroyed",
    "转交": "transferred", "交出": "transferred", "赠与": "transferred", "上交": "transferred",
}
INJURY_STATE_ALIASES = {
    "受伤": "fresh", "新伤": "fresh", "重伤": "fresh", "负伤": "fresh",
    "包扎": "healing", "好转": "healing", "恢复": "healing", "结痂": "healing",
    "痊愈": "healed", "愈合": "healed", "康复": "healed",
}

USE_VERBS = (
    "握着", "握住", "攥着", "拿着", "拿起", "举起", "取出", "掏出", "拔出", "抽出",
    "递给", "递出", "佩着", "戴着", "挥动", "挥起", "亮出", "摸出", "按在", "握紧",
)
RECOVERY_WORDS = (
    "找回", "寻回", "夺回", "拾回", "捡回", "失而复得", "重铸", "修好", "修复",
    "赎回", "送还", "归还", "物归原主", "重新得到",
)
HEALED_WORDS = (
    "痊愈", "完全愈合", "伤口愈合", "已无大碍", "伤势全消", "毫发无损", "毫无伤痕",
    "伤好了", "不再疼", "没有一丝伤", "行动如常",
)
WINDOW = 14
# Characters around an item mention searched for a recovery/repair word.
RECOVERY_WINDOW = 30
NEGATIONS = (
    "无法", "没能", "未能", "不能", "难以", "无力", "并未", "没有", "未曾", "不曾", "从未",
    "未", "没", "不", "无",
)
# "找不回 / 修不好" never contain a recovery word (找回/修好), so only prefixes matter.


def _negated(text: str, position: int) -> bool:
    prefix = text[max(0, position - 4):position]
    return any(prefix.endswith(word) for word in NEGATIONS)


def _affirmed_recovery(text: str) -> bool:
    """A recovery/repair word in ``text`` that is not negated (无法找回、未能修复…)."""
    for word in RECOVERY_WORDS:
        start = text.find(word)
        while start >= 0:
            if not _negated(text, start):
                return True
            start = text.find(word, start + 1)
    return False


def _normalize_state(value: Any, allowed: tuple[str, ...], aliases: dict[str, str]) -> str:
    text = str(value or "").strip().lower()
    if text in allowed:
        return text
    # Earliest non-negated alias wins ("未丢失，仍在手中" is not "lost").
    best: tuple[int, str] | None = None
    for key, mapped in aliases.items():
        start = text.find(key)
        while start >= 0:
            if not _negated(text, start):
                if best is None or start < best[0]:
                    best = (start, mapped)
                break
            start = text.find(key, start + 1)
    return best[1] if best else ""


def recovered_near(text: str, name: str, window: int = RECOVERY_WINDOW) -> bool:
    """A recovery/repair word close to a mention of ``name`` in ``text``."""
    text = str(text or "")
    if not name:
        return False
    start = text.find(name)
    while start >= 0:
        around = text[max(0, start - window): start + len(name) + window]
        if _affirmed_recovery(around):
            return True
        start = text.find(name, start + 1)
    return False


def _severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in INJURY_MIN_HEAL_CHAPTERS:
        return text
    if any(word in text for word in ("重", "深", "骨折", "贯穿", "severe")):
        return "severe"
    if any(word in text for word in ("轻", "擦", "皮外", "light")):
        return "light"
    return "medium"


def apply_continuity_updates(
    memory: dict[str, Any],
    chapter: dict[str, Any],
    number: int,
    result: dict[str, Any],
    verify: Callable[[Any], tuple[str, bool]],
    prose: str | None = None,
) -> list[str]:
    """Append evidence-backed item/injury events for one accepted chapter.

    ``verify(evidence)`` returns ``(matched_text, verified)`` against the
    accepted prose.  Re-settling the same chapter replaces its events.
    """
    warnings: list[str] = []
    chapter_id = str(chapter.get("id", ""))
    for key in ("item_events", "injury_events"):
        memory[key] = [
            item for item in memory.get(key, [])
            if isinstance(item, dict) and str(item.get("source_chapter_id", "")) != chapter_id
        ]
    current = {name: str(event.get("state", "")) for name, event in item_states(memory, number).items()}
    prose = str(prose if prose is not None else chapter.get("content", "") or "")
    for raw in result.get("item_updates", []) or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("item", raw.get("name", ""))).strip()[:60]
        state = _normalize_state(raw.get("state"), ITEM_STATES, ITEM_STATE_ALIASES)
        if not name or not state:
            continue
        quote, verified = verify(raw.get("evidence"))
        if not verified:
            warnings.append(f"道具“{name}”的状态变化缺少可核验正文证据，未写入")
            continue
        previous_state = current.get(name, "")
        if (
            state == "active"
            and previous_state in {"lost", "destroyed"}
            and not recovered_near(str(quote or ""), name)
            and not recovered_near(prose, name)
        ):
            # A destroyed/lost item may only come back through an explicit
            # recovery or repair; otherwise the contradiction would become canon.
            warnings.append(
                f"道具“{name}”此前{ITEM_STATE_LABELS[previous_state]}，本章正文未交代找回或修复，"
                "未将其恢复为可用；请作者确认"
            )
            continue
        current[name] = state
        memory["item_events"].append({
            "id": str(uuid.uuid4()),
            "item": name,
            "state": state,
            "holder": str(raw.get("holder", "")).strip()[:40],
            "note": str(raw.get("note", "")).strip()[:120],
            "chapter_number": number,
            "source_chapter_id": chapter_id,
            "evidence": quote,
        })
    for raw in result.get("injury_updates", []) or []:
        if not isinstance(raw, dict):
            continue
        character = str(raw.get("character", "")).strip()[:40]
        injury = str(raw.get("injury", "")).strip()[:60]
        state = _normalize_state(raw.get("state"), INJURY_STATES, INJURY_STATE_ALIASES)
        if not character or not injury or not state:
            continue
        quote, verified = verify(raw.get("evidence"))
        if not verified:
            warnings.append(f"“{character}”的伤势变化缺少可核验正文证据，未写入")
            continue
        memory["injury_events"].append({
            "id": str(uuid.uuid4()),
            "character": character,
            "injury": injury,
            "state": state,
            "severity": _severity(raw.get("severity")),
            "chapter_number": number,
            "source_chapter_id": chapter_id,
            "evidence": quote,
        })
    memory["item_events"] = memory["item_events"][-400:]
    memory["injury_events"] = memory["injury_events"][-400:]
    return warnings


def item_states(memory: dict[str, Any], before_number: int) -> dict[str, dict[str, Any]]:
    """Fold item events from chapters strictly before ``before_number``."""
    states: dict[str, dict[str, Any]] = {}
    events = sorted(
        (item for item in memory.get("item_events", []) if isinstance(item, dict)),
        key=lambda item: int(item.get("chapter_number", 0) or 0),
    )
    for event in events:
        number = int(event.get("chapter_number", 0) or 0)
        if before_number and number >= before_number:
            continue
        states[str(event.get("item", ""))] = dict(event)
    return states


def injury_states(memory: dict[str, Any], before_number: int) -> dict[str, dict[str, Any]]:
    """Key ``character|injury`` → latest event plus the chapter it first occurred."""
    states: dict[str, dict[str, Any]] = {}
    events = sorted(
        (item for item in memory.get("injury_events", []) if isinstance(item, dict)),
        key=lambda item: int(item.get("chapter_number", 0) or 0),
    )
    for event in events:
        number = int(event.get("chapter_number", 0) or 0)
        if before_number and number >= before_number:
            continue
        key = f"{event.get('character', '')}|{event.get('injury', '')}"
        previous = states.get(key)
        merged = dict(event)
        merged["first_chapter"] = (
            previous.get("first_chapter", number) if previous and previous.get("state") != "healed" else number
        )
        if previous and event.get("state") != "fresh":
            merged["severity"] = previous.get("severity", merged.get("severity", "medium"))
        states[key] = merged
    return states


def render_continuity_state(project: dict[str, Any], current_index: int) -> str:
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    number = current_index + 1
    lines: list[str] = []
    for name, state in sorted(item_states(memory, number).items()):
        label = ITEM_STATE_LABELS.get(str(state.get("state")), str(state.get("state")))
        holder = f"，持有者：{state['holder']}" if state.get("holder") else ""
        lines.append(f"- 道具【{name}】{label}{holder}（第{state.get('chapter_number')}章）")
    for _key, state in sorted(injury_states(memory, number).items()):
        if state.get("state") == "healed":
            continue
        label = INJURY_STATE_LABELS.get(str(state.get("state")), str(state.get("state")))
        lines.append(
            f"- {state.get('character')}的{state.get('injury')}：{label}"
            f"（第{state.get('first_chapter')}章受伤）"
        )
    if not lines:
        return ""
    return (
        "以下为截至本章开始时有证据的物品与伤势状态；已遗失/已毁坏的道具不得无交代地再次使用，"
        "新伤不得无故消失。\n" + "\n".join(lines[:30])
    )


def _sentences(text: str) -> list[str]:
    return [item for item in re.split(r"(?<=[。！？!?])|\n+", str(text or "")) if item.strip()]


def check_continuity_draft(
    project: dict[str, Any], current_index: int, draft: str
) -> list[dict[str, Any]]:
    """Deterministic contradictions between a draft and item/injury state."""
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    number = current_index + 1
    issues: list[dict[str, Any]] = []
    text = str(draft or "")
    for name, state in item_states(memory, number).items():
        if state.get("state") not in {"lost", "destroyed"} or not name:
            continue
        recovered = recovered_near(text, name)
        for sentence in _sentences(text):
            position = sentence.find(name)
            if position < 0:
                continue
            window = sentence[max(0, position - WINDOW): position + len(name) + WINDOW]
            if any(verb in window for verb in USE_VERBS) and not recovered:
                label = ITEM_STATE_LABELS[str(state["state"])]
                issues.append({
                    "severity": "high" if state["state"] == "destroyed" else "medium",
                    "category": "道具连续性",
                    "message": f"“{name}”在第{state.get('chapter_number')}章{label}，本稿却在未交代找回/修复的情况下使用。",
                    "suggestion": "补写找回或修复的过程，或改用其他道具。",
                    "evidence": [{"quote": sentence.strip()[:240], "verified": True, "start": text.find(sentence)}],
                    "evidence_verified": True,
                    "source": "本地",
                })
                break
    for state in injury_states(memory, number).values():
        if state.get("state") not in {"fresh", "healing"}:
            continue
        elapsed = number - int(state.get("first_chapter", number) or number)
        needed = INJURY_MIN_HEAL_CHAPTERS.get(str(state.get("severity", "medium")), 2)
        if elapsed >= needed:
            continue
        character = str(state.get("character", ""))
        injury = str(state.get("injury", ""))
        sentences = _sentences(text)
        for position, sentence in enumerate(sentences):
            previous = sentences[position - 1] if position else ""
            about_character = (
                character in sentence
                or (injury and injury in sentence)
                # "他的伤口……" right after a sentence naming the character
                or ("伤" in sentence and character in previous)
            )
            if not about_character:
                continue
            if any(word in sentence for word in HEALED_WORDS):
                issues.append({
                    "severity": "medium",
                    "category": "伤势连续性",
                    "message": (
                        f"{character}的{state.get('injury')}在第{state.get('first_chapter')}章才受伤，"
                        f"本稿已写成痊愈，恢复过快。"
                    ),
                    "suggestion": "保留伤势带来的行动限制或疼痛，或补写合理的治疗与时间流逝。",
                    "evidence": [{"quote": sentence.strip()[:240], "verified": True, "start": text.find(sentence)}],
                    "evidence_verified": True,
                    "source": "本地",
                })
                break
    return issues

"""Debate / confrontation scenes (交锋场景).

A chapter plan or route may carry an optional ``debate`` object describing a
structured confrontation.  The prose prompt gets a compact "交锋结构" block and
the focused critics run an extra "交锋强度" checker — both only when the
chapter actually is a debate scene, so ordinary chapters pay nothing.
"""
from __future__ import annotations

from typing import Any

DEBATE_TEXT_FIELDS = (
    "topic",
    "venue_and_judge",
    "stakes",
    "protagonist_arguments",
    "protagonist_weak_points",
    "opponent_counterattack",
    "outcome_and_cost",
)
DEBATE_FIELD_LABELS = {
    "topic": "辩题",
    "venue_and_judge": "场合与裁决者",
    "stakes": "赌注",
    "opponent_strongest_arguments": "对手最强论点",
    "protagonist_arguments": "主角论点",
    "protagonist_weak_points": "主角软肋",
    "opponent_counterattack": "对手反击",
    "outcome_and_cost": "结果与代价",
}
MAX_OPPONENT_ARGUMENTS = 3
DEBATE_TYPE_MARKERS = ("辩", "论", "交锋", "廷议", "问对", "debate", "argument", "council")


def empty_debate() -> dict[str, Any]:
    return {**{key: "" for key in DEBATE_TEXT_FIELDS}, "opponent_strongest_arguments": []}


def normalize_debate(raw: Any) -> dict[str, Any]:
    """Clean a debate object; missing or malformed input becomes an empty one."""
    clean = empty_debate()
    if not isinstance(raw, dict):
        return clean
    for key in DEBATE_TEXT_FIELDS:
        value = raw.get(key, "")
        if isinstance(value, list):
            value = "；".join(str(item).strip() for item in value if str(item).strip())
        clean[key] = str(value or "").strip()[:400]
    arguments = raw.get("opponent_strongest_arguments", [])
    if isinstance(arguments, str):
        arguments = [item for item in arguments.splitlines()]
    if isinstance(arguments, list):
        clean["opponent_strongest_arguments"] = [
            str(item).strip()[:300] for item in arguments if str(item).strip()
        ][:MAX_OPPONENT_ARGUMENTS]
    return clean


def has_debate(debate: Any) -> bool:
    if not isinstance(debate, dict):
        return False
    return any(debate.get(key) for key in (*DEBATE_TEXT_FIELDS, "opponent_strongest_arguments"))


def chapter_debate(chapter: dict[str, Any]) -> dict[str, Any] | None:
    """The chapter's debate object (plan first, then route), or ``None``."""
    for holder in (chapter.get("plan"), chapter.get("route")):
        if isinstance(holder, dict) and has_debate(holder.get("debate")):
            return normalize_debate(holder["debate"])
    return None


def is_debate_chapter(chapter: dict[str, Any]) -> bool:
    if chapter_debate(chapter):
        return True
    plan = chapter.get("plan") if isinstance(chapter.get("plan"), dict) else {}
    chapter_type = str(plan.get("chapter_type", "")).lower()
    return any(marker in chapter_type for marker in DEBATE_TYPE_MARKERS)


def plan_debate_entry(route: Any, previous_plan: Any, result: Any) -> dict[str, Any]:
    """``{"debate": ...}`` for a freshly generated chapter plan, or ``{}``.

    Author-curated debate fields win: route first, then the debate the author
    edited on the previous plan, and only then the planner's own proposal.
    """
    for holder in (route, previous_plan, result):
        if isinstance(holder, dict) and has_debate(holder.get("debate")):
            return {"debate": normalize_debate(holder["debate"])}
    return {}


def render_debate(debate: dict[str, Any] | None) -> str:
    """Compact multi-line rendering of the filled debate fields."""
    if not debate:
        return ""
    lines = []
    for key in ("topic", "venue_and_judge", "stakes"):
        if debate.get(key):
            lines.append(f"{DEBATE_FIELD_LABELS[key]}：{debate[key]}")
    for number, argument in enumerate(debate.get("opponent_strongest_arguments", []), start=1):
        lines.append(f"对手最强论点{number}：{argument}")
    for key in ("protagonist_arguments", "protagonist_weak_points", "opponent_counterattack", "outcome_and_cost"):
        if debate.get(key):
            lines.append(f"{DEBATE_FIELD_LABELS[key]}：{debate[key]}")
    return "\n".join(lines)


DEBATE_PROSE_RULES = (
    "写法要求：对手每条最强论点都要以其本人立场、术语和最有力的形式完整说出，不得替对手降智；"
    "主角不能靠一句对方无法回应的现代金句取胜，论证必须在当时的知识与语境中成立；"
    "对手要抓住主角软肋反击，主角须付出可见代价；胜负由利益、权力与裁决者的考量决定，"
    "不得出现对手当场折服、纳头便拜。"
)


def render_debate_for_prompt(chapter: dict[str, Any]) -> str:
    """The "交锋结构" block for the prose prompt, or ``""`` for normal chapters."""
    debate = chapter_debate(chapter)
    if not debate:
        return ""
    return "交锋结构：\n" + render_debate(debate) + "\n" + DEBATE_PROSE_RULES


def render_debate_for_audit(chapter: dict[str, Any]) -> str:
    """Authority context of the debate checker; ``""`` keeps the checker off."""
    debate = chapter_debate(chapter)
    if debate:
        return render_debate(debate)
    if is_debate_chapter(chapter):
        plan = chapter.get("plan") if isinstance(chapter.get("plan"), dict) else {}
        return f"本章类型为交锋/论辩场景（{plan.get('chapter_type', '')}），作者未填写详细交锋结构，按通用标准检查。"
    return ""

"""Scene cards and author-approved methods; manuscript text remains canonical."""
from __future__ import annotations

from difflib import SequenceMatcher
import hashlib
import uuid
from typing import Any


def locate_scene(content: str, scene: dict[str, Any]) -> tuple[int, int] | None:
    quote = str(scene.get("excerpt", ""))
    if not quote:
        return None
    start = int(scene.get("start", -1))
    if start >= 0 and content[start:start + len(quote)] == quote:
        return start, start + len(quote)
    if content.count(quote) == 1:
        start = content.index(quote)
        return start, start + len(quote)
    return None


def save_scene(chapter: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    cards = chapter.setdefault("scenes", [])
    scene_id = str(item.get("id") or uuid.uuid4())
    previous = next((c for c in cards if c["id"] == scene_id), {})
    card = {"id": scene_id}
    for field in ("title", "pov", "time_location", "entry_state", "goal", "obstacle", "turn", "exit_state", "dependencies"):
        card[field] = str(item.get(field, previous.get(field, "")))[:2000]
    card["excerpt"] = str(item.get("excerpt", previous.get("excerpt", "")))[:50000]
    card["start"] = int(item.get("start", previous.get("start", -1)))
    if card["excerpt"]:
        span = locate_scene(str(chapter.get("content", "")), card)
        if span is None:
            raise ValueError("场景原文已变化或有多处相同文字，请重新选中正文绑定")
        for other in cards:
            other_span = locate_scene(str(chapter.get("content", "")), other)
            if other["id"] != scene_id and other_span and max(span[0], other_span[0]) < min(span[1], other_span[1]):
                raise ValueError("场景正文范围重叠，请分别选择不重叠的场景")
        card["start"], card["end"] = span
    if previous:
        cards[cards.index(previous)] = card
    else:
        if len(cards) >= 100:
            raise ValueError("每章最多保存 100 个场景")
        cards.append(card)
    return card


def scene_context(chapter: dict[str, Any], scene_id: str, selection: str | None = None) -> str:
    card = next((x for x in chapter.get("scenes", []) if x.get("id") == scene_id), None)
    if not card:
        raise ValueError("场景不存在")
    if card.get("excerpt"):
        if locate_scene(str(chapter.get("content", "")), card) is None:
            raise ValueError("场景原文已变化，请重新绑定后生成")
        if selection is not None and selection != card["excerpt"]:
            raise ValueError("选区与场景绑定不一致，请重新选择这一场")
    return "本次只处理以下场景，不扩展成整章：\n" + "\n".join(
        f"{label}：{card.get(key, '')}" for key, label in (
            ("title", "场景"), ("pov", "视角"), ("time_location", "时间地点"),
            ("entry_state", "进入状态"), ("goal", "目标"), ("obstacle", "阻力"),
            ("turn", "转折"), ("exit_state", "退出状态"), ("dependencies", "依赖事实")))


def preference_context(project: dict[str, Any]) -> str:
    preferences = project.get("author_preferences", [])
    return "\n".join(f"- {p.get('instruction', '')}" for p in preferences
                     if p.get("status") == "approved")[:4000]


def propose_preference(project: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    instruction = str(item.get("instruction", "")).strip()
    if not 6 <= len(instruction) <= 1000:
        raise ValueError("偏好说明需要 6–1000 个字符")
    preferences = project.setdefault("author_preferences", [])
    if len(preferences) >= 100:
        raise ValueError("最多保存 100 条偏好，请先清理不用的条目")
    proposal = {"id": str(uuid.uuid4()), "instruction": instruction, "status": "pending",
                "before": str(item.get("before", ""))[:2000], "after": str(item.get("after", ""))[:2000]}
    preferences.append(proposal)
    return proposal


def text_diff(before: str, after: str) -> dict[str, Any]:
    # Preserve exact whitespace and always allow lossless reconstruction.
    left, right = before.splitlines(keepends=True), after.splitlines(keepends=True)
    hunks = [{"kind": op, "before": "".join(left[a:b]), "after": "".join(right[c:d])}
             for op, a, b, c, d in SequenceMatcher(None, left, right, autojunk=False).get_opcodes()]
    return {"base_hash": hashlib.sha256(before.encode()).hexdigest(), "hunks": hunks}

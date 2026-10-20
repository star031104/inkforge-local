"""Author / reader / character knowledge boundaries rendered for one chapter."""
from __future__ import annotations

import re
from typing import Any

from app.story.memory import relevance


def _active_character_names(project: dict[str, Any], query: str) -> set[str]:
    active: set[str] = set()
    folded = query.casefold()
    for character in project.get("characters", []):
        name = str(character.get("name", "")).strip()
        aliases = character.get("aliases", [])
        if isinstance(aliases, str):
            aliases = re.split(r"[,，\n]", aliases)
        names = [name] + [str(item).strip() for item in aliases if str(item).strip()]
        if name and any(candidate.casefold() in folded for candidate in names):
            active.add(name)
    return active


def render_epistemic_context(
    project: dict[str, Any],
    current_index: int,
    query: str = "",
    active_character_names: set[str] | None = None,
) -> str:
    """Render explicit author/reader/character knowledge boundaries for one chapter."""
    chapters = project.get("chapters", [])
    current_number = current_index + 1
    current = chapters[current_index] if 0 <= current_index < len(chapters) else {}
    planned_pov = str(current.get("plan", {}).get("pov_character", "")).strip()
    active_names = set(active_character_names or _active_character_names(project, query))
    if planned_pov and planned_pov.casefold() not in {"omniscient", "全知", "auto"}:
        active_names.add(planned_pov)
    if not active_names:
        main_names = [
            str(item.get("name", "")).strip()
            for item in project.get("characters", [])
            if item.get("active", True)
            and str(item.get("importance", "supporting")) == "main"
            and str(item.get("name", "")).strip()
        ]
        active_names.update(main_names[:2])

    chapter_numbers = {
        str(chapter.get("id", "")): index + 1
        for index, chapter in enumerate(chapters)
        if isinstance(chapter, dict)
    }

    def fact_available(fact: dict[str, Any]) -> bool:
        if not fact.get("active", True):
            return False
        source_number = chapter_numbers.get(
            str(fact.get("source_chapter_id") or fact.get("chapter_id") or ""), 0
        )
        if source_number and source_number >= current_number:
            return False
        valid_from = int(fact.get("valid_from_chapter", 0) or 0)
        valid_until = int(fact.get("valid_until_chapter", 0) or 0)
        return not (
            (valid_from and current_number < valid_from)
            or (valid_until and current_number > valid_until)
        )

    facts = [
        item
        for item in project.get("memory", {}).get("facts", [])
        if isinstance(item, dict) and fact_available(item)
    ]
    ranked_reader = sorted(
        (
            item
            for item in facts
            if item.get("reader_known", False) and not item.get("author_only", False)
        ),
        key=lambda item: (
            relevance(query, str(item.get("text", ""))),
            int(item.get("importance", 3) or 3),
        ),
        reverse=True,
    )[:8]
    reader_lines = [str(item.get("text", "")).strip() for item in ranked_reader]

    character_blocks: list[str] = []
    for character in project.get("characters", []):
        name = str(character.get("name", "")).strip()
        if not name or name not in active_names or not character.get("active", True):
            continue
        confirmed: list[str] = []
        suspected: list[str] = []
        baseline = str(
            character.get("knowledge_baseline", character.get("knowledge", ""))
        ).strip()
        if baseline:
            confirmed.append(baseline)
        for item in character.get("knowledge_ledger", []):
            if not isinstance(item, dict) or not item.get("active", True):
                continue
            learned_number = int(item.get("chapter_number", 0) or 0)
            if learned_number and learned_number >= current_number:
                continue
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            if str(item.get("certainty", "confirmed")) == "suspected":
                suspected.append(text)
            else:
                confirmed.append(text)
        for fact in facts:
            if name in [str(value) for value in fact.get("known_by", [])]:
                text = str(fact.get("text", "")).strip()
                if text and text not in confirmed:
                    confirmed.append(text)
        forbidden = [
            str(item.get("text", "")).strip()
            for item in facts
            if (
                item.get("author_only", False)
                or str(item.get("visibility", "objective")) == "private"
            )
            and name not in [str(value) for value in item.get("known_by", [])]
            and (
                relevance(query, str(item.get("text", ""))) > 0
                or int(item.get("importance", 3) or 3) >= 5
            )
        ][:6]
        lines = [f"【{name}】"]
        lines.append("可作为行动依据：" + ("；".join(confirmed[-12:]) or "暂无明确记录"))
        if suspected:
            lines.append("仅可当作猜测：" + "；".join(suspected[-8:]))
        if forbidden:
            lines.append("明确不可知/不可据此行动：" + "；".join(forbidden))
        character_blocks.append("\n".join(lines))

    pov_label = planned_pov or str(project.get("narrative", {}).get("pov", "auto"))
    return (
        f"本章视角标记：{pov_label}\n"
        "分层规则：权威事实属于作者/系统层，不自动等于人物知情；"
        "读者已经看到的信息可以形成戏剧反讽，但人物仍不得无来源使用；"
        "人物只有开篇人工知识、早于本章且有证据的知情账本、或 known_by 明示内容可作为行动依据；"
        "suspected 只能写成猜测。其他人物的 secrets 字段默认不可知。\n"
        + (
            "读者截至本章前已知的相关事实：\n- " + "\n- ".join(reader_lines) + "\n"
            if reader_lines
            else "读者截至本章前已知的相关事实：暂无结构化记录。\n"
        )
        + ("\n\n".join(character_blocks) if character_blocks else "当前未锁定具体视角人物，仍须逐人物遵守知情来源。")
    )

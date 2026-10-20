"""Write the detailed whole-book outline as plain text when the JSON plan is too terse.

Cloud models such as DeepSeek flash answer the master-plan JSON with a short
``full_outline`` (≈400 Chinese characters) even when asked for ~2000, because
the same object carries a dozen other fields.  Asking twice in JSON rarely
helps.  A dedicated plain-text call, given the plan it must agree with, reliably
reaches the requested length.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

from app.llm.client import chat_once

OUTLINE_EXPANSION_PROMPT = """你是长篇小说总导演。依据给定的作品设定和已经确定的故事圣经，写出全书详细总纲。
要求：
1. 按卷分段，每段以“卷X「卷名」（时间范围，约N章）：”开头；卷数、卷名和时间与故事圣经及作者大纲一致，不得增删或挪动历史节点。
2. 每卷写清：本卷开局处境、主要事件链（按先后，至少五个具体事件）、关键交锋或转折、主角的选择与代价、卷末状态以及它如何触发下一卷。
3. 只写已经确定的方向，不新增与设定冲突的人物或事件；遵守作者硬规则和人物登场时间。
4. 只输出总纲正文，不要 JSON、不要 Markdown 标题、不要解释。"""

Chat = Callable[..., Awaitable[str]]


async def expand_full_outline(
    settings: dict[str, Any],
    context: str,
    core: dict[str, Any],
    *,
    target_chars: int,
    minimum_chars: int,
    chat: Chat | None = None,
) -> tuple[str, list[str]]:
    """Return a full outline of at least ``minimum_chars`` when possible.

    Never raises for a short answer: the longer of the existing and the new
    outline is kept and a warning explains the shortfall.
    """
    existing = str(core.get("full_outline", "")).strip()
    if len(existing) >= minimum_chars:
        return existing, []
    bible = {key: core.get(key) for key in (
        "theme", "reader_promise", "central_conflict", "story_engine", "ending_state",
        "main_plot", "theme_progression", "pacing_plan", "stakes_ladder",
        "major_character_arcs", "subplots", "historical_nodes",
    )}
    messages = [
        {"role": "system", "content": OUTLINE_EXPANSION_PROMPT},
        {"role": "user", "content": (
            f"{context}\n\n【已确定的故事圣经】\n{json.dumps(bible, ensure_ascii=False)}\n\n"
            f"【现有简要总纲】\n{existing or '（无）'}\n\n"
            f"请写出全书详细总纲，总字数 {target_chars}-{int(target_chars * 1.35)} 字。"
        )},
    ]
    call = chat or chat_once
    text = await call(
        {**settings, "_workload": "planning"}, messages,
        temperature=0.5, max_tokens=int(target_chars * 1.8) + 800, timeout_seconds=420,
    )
    expanded = str(text or "").strip()
    best = expanded if len(expanded) > len(existing) else existing
    warnings = ["故事圣经里的总纲过短，已单独扩写详细全书总纲"]
    if len(best) < minimum_chars:
        warnings.append(f"详细总纲只有 {len(best)} 字（目标 {minimum_chars} 字以上），可在全书总导演板里手动补充")
    return best, warnings

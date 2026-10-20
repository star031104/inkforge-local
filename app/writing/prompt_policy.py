from __future__ import annotations

from typing import Any


CREATIVE_FREEDOM_RULES = {
    "strict": (
        "创作自由度：严格依纲。未在权威上下文出现的过去经历、关系、能力、关键道具和"
        "世界规则一律保持未知；只可补充不改变状态的即时感官细节。"
    ),
    "balanced": (
        "创作自由度：平衡。可以补充服饰、环境、普通动作等可撤回的低风险细节，也可以让"
        "人物作符合既有人格的临场选择；不得新增会约束后文的往事、关系、能力或世界规则。"
    ),
    "exploratory": (
        "创作自由度：探索。可以提出新的场景线索、临时障碍和人物猜测，但必须在正文中保持"
        "为未证实或可撤回状态；不得改变权威事实、人物知情边界和本章必须兑现的结果。"
    ),
}


_LOCAL_REWRITE_OMISSIONS = {
    "长期作者意图",
    "近期焦点",
    "叙事与篇幅契约",
    "阶段推进硬门",
    "章节场景契约",
    "作品总纲",
    "分层导演规划",
    "全书滚动进展",
    "伏笔与暗线治理议程",
    "最近章节摘要",
}


def creative_freedom_rule(settings: dict[str, Any]) -> tuple[str, str]:
    level = str(settings.get("creative_freedom") or "balanced").strip().lower()
    if level not in CREATIVE_FREEDOM_RULES:
        level = "balanced"
    return level, CREATIVE_FREEDOM_RULES[level]


def apply_task_prompt_policy(
    sections: list[Any], *, mode: str, full_chapter_rewrite: bool = False
) -> list[Any]:
    """Shape the context around the requested writing operation.

    Local rewrites need nearby prose, active facts, voice and style. Loading the
    whole story strategy makes small revisions less precise and wastes context.
    Full chapter rewrites keep the complete chapter recipe.
    """
    if mode != "rewrite" or full_chapter_rewrite:
        return sections
    for section in sections:
        if section.name in _LOCAL_REWRITE_OMISSIONS:
            section.content = ""
            section.required = False
            section.status = "omitted"
            section.reason = "局部改写采用精简上下文配方"
    return sections

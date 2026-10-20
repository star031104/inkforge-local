"""Local, model-free fallback for idea recommendations (灵感推荐)."""
from __future__ import annotations

from typing import Any


def local_ideas(project: dict[str, Any], kind: str, reason: str) -> dict[str, Any]:
    narrative = project.get("narrative", {})
    master = project.get("planning", {}).get("master", {})
    people = [
        str(item.get("name", "")).strip()
        for item in project.get("characters", [])
        if str(item.get("name", "")).strip()
    ]
    lead = people[0] if people else "核心人物"
    counterpart = people[1] if len(people) > 1 else "既有对手"
    base_conflict = str(
        master.get("central_conflict")
        or project.get("premise")
        or narrative.get("central_question")
        or "人物目标与现实代价发生冲突"
    )
    ending = str(master.get("ending_state") or narrative.get("ending_direction", ""))
    lenses = (
        ("代价升级", f"让{lead}的方案产生一名明确受损者", "现实代价"),
        ("关系选择", f"迫使{lead}与{counterpart}在共同目标下选择不同手段", "关系裂变"),
        ("信念反证", f"用一次合理失败检验{lead}最确信的方法", "认知转向"),
    )
    options = []
    for title, turn, theme in lenses:
        options.append(
            {
                "title": title,
                "theme": theme,
                "central_conflict": base_conflict[:100],
                "story_promise": f"通过{theme}让既有主线产生新的选择与后果",
                "turning_point": turn,
                "ending_direction": ending[:100],
                "why_fit": "直接利用现有核心冲突和人物，不新增无关支线",
                "risk": "避免只讨论观点，必须改变资源、关系或行动结果",
            }
        )
    return {
        "options": options,
        "fallback": True,
        "warnings": [f"AI灵感推荐未完成，已提供本地结构化方向。原因：{reason}"],
    }

from __future__ import annotations

import re
from typing import Any

from .style_engine import render_fingerprint, style_stats


def _sentences(text: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"(?<=[。！？!?])|\n+", text)
        if item.strip()
    ]


def local_style_analysis(sample: str, reason: str) -> dict[str, Any]:
    stats = style_stats(sample)
    fingerprint = render_fingerprint(sample)
    pace = "短句推进" if stats.sentence_length < 16 else "长短句交替"
    dialogue = (
        "对话较多"
        if stats.dialogue_ratio > 0.28
        else "叙述较多"
        if stats.dialogue_ratio < 0.1
        else "叙述与对话均衡"
    )
    return {
        "name": "本地统计文风",
        "profile": (
            f"{fingerprint} 整体采用{pace}，{dialogue}。此文风卡来自本地统计，"
            "未推断原文没有直接体现的文学流派或作者意图。"
        ),
        "dos": [
            f"平均句长保持在约{stats.sentence_length:.0f}字附近并允许自然波动",
            f"对话占比参考约{stats.dialogue_ratio:.0%}",
            "保持原样文的段落疏密和叙述距离",
            "优先复现节奏与观察方式，不复制专名和句子",
        ],
        "donts": [
            "不要机械凑平均句长",
            "不要复制样文情节、人物和标志性措辞",
            "不要用空泛抒情替代具体场景",
        ],
        "fallback": True,
        "warnings": [f"AI文风分析未完成，已使用本地统计文风卡。原因：{reason}"],
    }


def local_memory_result(
    project: dict[str, Any], chapter: dict[str, Any], content: str, reason: str
) -> dict[str, Any]:
    sentences = _sentences(content)
    selected = []
    for item in sentences[:2] + sentences[-3:]:
        if item not in selected:
            selected.append(item)
    summary = "".join(selected)[:500]
    old = str(project.get("memory", {}).get("story_so_far", "")).strip()
    addition = f"{chapter.get('title', '本章')}：{summary}"
    story_so_far = "\n".join(item for item in (old, addition) if item)[-4000:]
    return {
        "summary": summary,
        "story_so_far": story_so_far,
        "character_updates": [],
        "facts": [],
        "plot_threads": [],
        "timeline": [],
        "description_updates": [],
        "continuity_notes": [
            f"AI记忆提取未完成，仅生成了保守的本地基础摘要；人物状态、事实、"
            f"伏笔和时间线未自动推断，请按需手工补充。原因：{reason}"
        ],
        "fallback": True,
        "warnings": [f"已使用本地基础记忆。原因：{reason}"],
    }


def local_audit_result(local_checks: dict[str, Any], reason: str) -> dict[str, Any]:
    issues = local_checks.get("issues", [])
    return {
        "score": int(local_checks.get("score", 0)),
        "verdict": "partial",
        "issues": [],
        "strengths": [],
        "revision_brief": (
            f"AI连续性审计未完成，本次只执行了本地长度、重复、元话语、"
            f"疑似新增往事和文风偏移检查。原因：{reason}"
        ),
        "local_checks": local_checks,
        "fallback": True,
        "audit_scope": "local_only",
        "warnings": [f"本次只有本地审计结果。原因：{reason}"],
    }


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

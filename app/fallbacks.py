from __future__ import annotations

from typing import Any

from app.style_engine import render_fingerprint, style_stats


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



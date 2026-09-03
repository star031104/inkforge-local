from __future__ import annotations

import re
from typing import Any, Sequence

from .knowledge import canonical_name, infer_active_entities


CANON_MODES = {"canon", "au", "cp", "custom"}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,，\n]", value) if item.strip()]
    if isinstance(value, list):
        return [_clean(item) for item in value if _clean(item)]
    return []


def ensure_fanfic_defaults(project: dict[str, Any]) -> dict[str, Any]:
    fanfic = project.get("fanfic")
    if not isinstance(fanfic, dict):
        fanfic = {}
        project["fanfic"] = fanfic
    fanfic.setdefault("enabled", False)
    fanfic.setdefault("mode", "canon")
    if fanfic.get("mode") not in CANON_MODES:
        fanfic["mode"] = "canon"
    fanfic.setdefault("source_universes", [])
    if not isinstance(fanfic.get("source_universes"), list):
        fanfic["source_universes"] = []
    fanfic.setdefault(
        "policy",
        {
            "preserve_identity": True,
            "preserve_core_personality": True,
            "preserve_voice": True,
            "preserve_abilities": True,
            "require_causal_character_change": True,
            "unverified_ai_inference_is_hard_canon": False,
            "audit_before_accept": True,
        },
    )
    if not isinstance(fanfic.get("policy"), dict):
        fanfic["policy"] = {}
    policy = fanfic["policy"]
    policy.setdefault("preserve_identity", True)
    policy.setdefault("preserve_core_personality", True)
    policy.setdefault("preserve_voice", True)
    policy.setdefault("preserve_abilities", True)
    policy.setdefault("require_causal_character_change", True)
    policy.setdefault("unverified_ai_inference_is_hard_canon", False)
    policy.setdefault("audit_before_accept", True)

    for character in project.get("characters", []):
        if not isinstance(character, dict):
            continue
        profile = character.get("canon_profile")
        if not isinstance(profile, dict):
            profile = {}
            character["canon_profile"] = profile
        profile.setdefault("enabled", False)
        profile.setdefault("user_verified", False)
        profile.setdefault("source_work", "")
        profile.setdefault("timeline_node", "")
        profile.setdefault("identity", "")
        profile.setdefault("appearance", character.get("appearance", ""))
        profile.setdefault("core_personality", character.get("personality", ""))
        profile.setdefault("deep_personality", "")
        profile.setdefault("values", character.get("values", ""))
        profile.setdefault("goals", character.get("goal", ""))
        profile.setdefault("fears", character.get("fears", ""))
        profile.setdefault("abilities", "")
        profile.setdefault("limitations", character.get("hard_limits", ""))
        profile.setdefault("speech_style", character.get("voice", ""))
        profile.setdefault("behavior_patterns", character.get("mannerisms", ""))
        profile.setdefault("emotional_patterns", "")
        profile.setdefault("relationship_patterns", character.get("relationships", ""))
        profile.setdefault("must_preserve", [])
        profile.setdefault("must_not", [])
        profile.setdefault("source_refs", [])
        for key in ("must_preserve", "must_not", "source_refs"):
            profile[key] = _list(profile.get(key))
    return project


def active_canon_characters(
    project: dict[str, Any], query: str, active_names: Sequence[str] = ()
) -> list[dict[str, Any]]:
    ensure_fanfic_defaults(project)
    if not project.get("fanfic", {}).get("enabled", False):
        return []
    names = {canonical_name(project, name) for name in active_names if _clean(name)}
    if not names:
        names = set(infer_active_entities(project, query))
    result: list[dict[str, Any]] = []
    for character in project.get("characters", []):
        if not isinstance(character, dict):
            continue
        profile = character.get("canon_profile", {})
        if not isinstance(profile, dict) or not profile.get("enabled", False):
            continue
        name = _clean(character.get("name"))
        if names and name not in names:
            continue
        result.append(character)
    return result


def render_canon_context(
    project: dict[str, Any], query: str, active_names: Sequence[str] = (), *, max_chars: int = 6500
) -> str:
    characters = active_canon_characters(project, query, active_names)
    if not characters:
        return ""
    mode = project.get("fanfic", {}).get("mode", "canon")
    lines = [
        "【同人正典锁 / Canon Lock】",
        f"模式：{mode}",
        "角色可以被新经历影响，但核心人格变化必须由正文中足够的因果过程造成。",
        "冲突时优先级：人工核对的原作核心设定 > 已确认正文事实 > 当前关系/状态 > 章节大纲 > 推动剧情的便利。",
        "不得为了恋爱、爽点或推进剧情让角色突然失去警惕、智力、价值观、能力限制或固有说话方式。",
        "若原作资料没有答案，保持未知或写成推测；不要把模型补全当作原作事实。",
    ]
    for character in characters:
        profile = character.get("canon_profile", {})
        verified = bool(profile.get("user_verified", False))
        lines.append(
            f"\n【{character.get('name', '')}｜{profile.get('source_work') or '原作未填'}｜"
            f"{'人工核对' if verified else '待核对档案'}】"
        )
        field_pairs = [
            ("时间节点", "timeline_node"),
            ("身份", "identity"),
            ("外貌锚点", "appearance"),
            ("核心人格", "core_personality"),
            ("深层人格", "deep_personality"),
            ("价值观", "values"),
            ("欲望/目标", "goals"),
            ("恐惧/软肋", "fears"),
            ("能力", "abilities"),
            ("能力限制", "limitations"),
            ("语言风格", "speech_style"),
            ("行为模式", "behavior_patterns"),
            ("情绪表达", "emotional_patterns"),
            ("关系模式", "relationship_patterns"),
        ]
        for label, key in field_pairs:
            value = _clean(profile.get(key))
            if value:
                lines.append(f"- {label}：{value}")
        must_preserve = _list(profile.get("must_preserve"))
        must_not = _list(profile.get("must_not"))
        if must_preserve:
            lines.append("- 必须保持：" + "；".join(must_preserve))
        if must_not:
            lines.append("- 禁止轻易发生：" + "；".join(must_not))
        if not verified:
            lines.append(
                "- 注意：此角色档案尚未人工确认；只能把与用户输入/资料片段一致的内容当硬约束，"
                "其余字段视为待核对。"
            )
    return "\n".join(lines)[:max_chars]


CANON_ANALYSIS_PROMPT = """你是同人小说的原作角色资料整理员。只根据用户提供的角色资料片段整理人物档案，不允许凭空补全。
把“资料明确写出/可直接观察”的内容与“合理推断”分开。若资料不足，字段留空或写“资料不足”，不要用模型常识填充。
返回严格 JSON：
{
  "source_work":"作品名",
  "timeline_node":"本资料对应时间节点；不确定则留空",
  "identity":"身份与关键背景",
  "appearance":"稳定外貌锚点",
  "core_personality":"表层且稳定的人格与待人方式",
  "deep_personality":"深层动机、执念与矛盾",
  "values":"价值观与底线",
  "goals":"核心欲望/目标",
  "fears":"恐惧、软肋与回避机制",
  "abilities":"能力与典型使用方式",
  "limitations":"能力边界、代价、不能做什么",
  "speech_style":"词汇、句长、礼貌/戏谑/沉默方式、称呼习惯",
  "behavior_patterns":"压力、陌生人、朋友、敌人面前的典型行为",
  "emotional_patterns":"愤怒、悲伤、害羞/动摇、认真状态下如何表现",
  "relationship_patterns":"建立信任/亲密/敌意的方式",
  "must_preserve":["6-12条写作时必须维持的核心特征"],
  "must_not":["6-12条最容易OOC的行为或写法"],
  "explicit_facts":["资料可直接支持的事实"],
  "inferences":["仅为推断、不能自动视为正典的内容"]
}
不要输出Markdown。资料如下：\n"""


CANON_AUDIT_PROMPT = """你是同人小说角色一致性审校员。对照给出的“同人正典锁”和本章候选正文，检查是否OOC。
重点检查：核心人格、价值观、说话方式、能力限制、关系推进速度、信息边界、为了剧情方便而降智/失去警惕、无铺垫的亲密或性格突变。
不要因为角色经历了合理的新事件就判OOC；变化有足够因果铺垫时应允许。
返回严格JSON：
{
  "score": 0-100,
  "verdict": "pass/revise/block",
  "issues": [{"character":"角色名","severity":"low/medium/high","type":"voice/personality/value/ability/relationship/knowledge/other","evidence":"正文短引文","reason":"为何与正典锁冲突","fix":"如何在不改核心剧情的前提下修正"}],
  "strengths": ["保持得好的角色特征"]
}
不要输出Markdown。"""


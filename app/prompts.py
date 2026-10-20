from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from app.story.canon import render_canon_context
from app.llm.budget import estimate_tokens, safety_margin
from app.story.debate import render_debate_for_prompt
from app.story.continuity_state import render_continuity_state
from app.story.history import render_story_date
from app.story.knowledge import render_knowledge_context
from app.story.lore import activate_lore
from app.review.manuscript_quality import prompt_repetition_guard
from app.story.memory import (
    MemoryHit,
    relevance,
    render_memories,
    render_thread_agenda,
    retrieve_memories,
    select_thread_agenda,
)
from app.planning.narrative_policy import guidance as narrative_guidance
from app.planning.model import render_planning_context
from app.prompt_policy import apply_task_prompt_policy, creative_freedom_rule
from app.story.references import combined_style_corpus, render_reference_context
from app.services.craft_settings import craft_setting
from app.story.hook_ledger import render_hook_debts_for_prompt
from app.story.story_systems import render_professional_context
from app.style_engine import render_fingerprint, render_style_context
from app.story.temporal_context import chapter_context
from app.writing_skills import (
    WRITING_SKILL_BOUNDARY,
    activate_writing_skills,
    render_writing_skills,
    writing_skill_blocks,
)
from app.writing_workspace import preference_context, scene_context
from app.story.epistemics import _active_character_names, render_epistemic_context

PROSE_PROMPT_VERSION = "prose-2.1.0"
PROMPT_CONTEXT_SCHEMA_VERSION = 2


SYSTEM_PROMPT = """你是“砚火”，一名严谨的中文小说合著者。
你的任务是创作可直接进入正文的文学文本，同时维护人物、世界设定、时间线和叙事视角的一致性。

优先级从高到低：
1. 作者临时要求与人工核对的同人角色正典锁。
2. 本章计划中的必须保留、必须避免，以及已确认故事事实/人物状态/时间线/世界规则。
3. 近期焦点、长期作者意图和大纲。
4. 抽象文风特征。

若大纲要求角色做出明显违背人工核对正典核心人格的行为，优先调整剧情实现方式，而不是扭曲角色。

硬规则：
1. 不复述任务，不解释创作过程，不输出“以下是正文”等元话语。
2. 不擅自改写既定事实，不用回忆、梦境或“其实”强行修补矛盾。
3. 人物只能依据其亲历、被告知或合理推断的信息行动。
4. 道具、伤势、位置、关系和时间流逝必须承接最近状态。
5. 人物的价值观、恐惧、内在矛盾和语言指纹是稳定约束；状态可以变化，但变化必须由正文事件造成。
6. 人物不得为了推进剧情突然变聪明、变愚蠢、改口吻或遗忘自身目标；如需反常行为，正文必须给出可观察诱因。
7. 稳定外貌是事实库，不是每次登场都要复述的清单；只提取与当前动作、视角和情绪有关的少量细节。
8. 不连续复用同一段环境介绍、身体反应、眼神、比喻、句式起笔或段落模板。
9. 用行动、感官、对话与具体细节呈现，不用总结代替关键场面。
10. 避免模板化网文腔、连续排比、滥用破折号、空泛抒情和同义反复。
11. 抵达本章目标即可，不抢写后续大纲，不提前回收未到时机的伏笔。
12. 不得声称“旧信、日记、过去对话、回忆或档案中早已写过/发生过”上下文未提供的信息。新线索必须在当前场景中可观察，或明确保持为人物猜测。
13. 每个场景遵循“欲望/任务→阻力→选择→代价→新状态”的因果链；转折必须改写人物下一步行动，不能只是再来一条消息。
14. 开头尽快落在正在变化的具体时刻；结尾停在选择、后果或认知已改变之后。普通章节不强造悬崖，终章不再新增未结问题。
15. 输出只包含用户要求的小说正文。"""


STYLE_ANALYSIS_PROMPT = """你是文学编辑。请分析给定样文的可迁移写作特征，不要模仿专有角色、地名、情节或独特句子。
返回严格 JSON：
{
  "name": "不超过12字的风格名",
  "profile": "150-300字，描述叙述视角、句法节奏、用词、意象、对话、情绪距离与场景组织",
  "dos": ["4-8条可执行写作规则"],
  "donts": ["3-6条应避免的偏差"]
}
不要输出 Markdown 代码块。样文如下：
"""


@dataclass
class PromptSection:
    name: str
    content: str
    priority: int
    role: str = "user"
    required: bool = False
    keep_tail: bool = False
    min_chars: int = 0
    original_tokens: int = 0
    status: str = "included"
    reason: str = ""
    atomic_units: tuple[str, ...] = ()
    atomic_prefix: str = ""


@dataclass
class PromptBuild:
    messages: list[dict[str, str]]
    activated_lore: list[dict[str, Any]]
    activated_skills: list[dict[str, Any]]
    estimated_tokens: int
    sections: list[dict[str, Any]]
    retrieved_memories: list[MemoryHit]
    budget_warnings: list[str]
    prompt_version: str = PROSE_PROMPT_VERSION
    context_schema_version: int = PROMPT_CONTEXT_SCHEMA_VERSION


def _clip_complete_units(text: str, limit: int, keep_tail: bool = False) -> str:
    """Clip on paragraph or sentence boundaries so constraints remain readable."""
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    units = [item.strip() for item in re.split(r"(?<=\n)|(?<=[。！？；])", value) if item.strip()]
    if keep_tail:
        units.reverse()
    selected: list[str] = []
    used = 0
    for unit in units:
        if selected and used + len(unit) > limit:
            break
        if not selected and len(unit) > limit:
            unit = unit[-limit:] if keep_tail else unit[:limit]
        selected.append(unit)
        used += len(unit)
    if keep_tail:
        selected.reverse()
    return "\n".join(selected).strip()


def _budget_lore(
    entries: list[dict[str, Any]], token_budget: int
) -> tuple[list[dict[str, Any]], list[str]]:
    ranked = sorted(
        entries,
        key=lambda item: (
            bool(item.get("constant", False)),
            str(item.get("canon", "soft")) == "hard",
            item.get("_activation_reason") == "direct",
            len(item.get("_matched_keys", [])),
            int(item.get("order", 100)),
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    used = 0
    omitted: list[str] = []
    for entry in ranked:
        cost = estimate_tokens(str(entry.get("content", ""))) + 12
        hard = str(entry.get("canon", "soft")) == "hard"
        if selected and used + cost > token_budget and not hard:
            omitted.append(str(entry.get("title", "未命名设定")))
            continue
        selected.append(entry)
        used += cost
    selected.sort(key=lambda item: int(item.get("order", 100)))
    warnings = []
    if omitted:
        warnings.append(
            f"世界书独立预算约 {token_budget} tokens，已省略 {len(omitted)} 条低优先级设定："
            + "、".join(omitted[:8])
        )
    if used > token_budget:
        warnings.append(
            f"权威/常驻世界规则约 {used} tokens，超过世界书预算但仍保留"
        )
    return selected, warnings


def _render_plan(current: dict[str, Any]) -> str:
    plan = current.get("plan", {}) if isinstance(current.get("plan"), dict) else {}
    route = current.get("route", {}) if isinstance(current.get("route"), dict) else {}

    def value(key: str, fallback: object = "") -> object:
        planned = plan.get(key)
        if planned not in (None, "", []):
            return planned
        routed = route.get(key)
        return routed if routed not in (None, "", []) else fallback

    values = [
        f"目标：{value('goal', current.get('scene_goal', ''))}",
        f"冲突：{value('conflict')}",
        "必须保留：\n- " + "\n- ".join(value("must_keep", [])),
        "必须避免：\n- " + "\n- ".join(value("must_avoid", [])),
        f"转折：{value('turning_point')}",
        f"结尾钩子：{value('ending_hook')}",
        f"章节功能：{plan.get('chapter_type', '')}",
        f"视角人物：{plan.get('pov_character', '')}",
        f"时间地点：{plan.get('time_location', '')}",
        f"开篇第一拍：{plan.get('opening_beat', '')}",
        "场景因果拍：\n- " + "\n- ".join(plan.get("scene_beats", [])),
        f"情绪转向：{plan.get('emotional_turn', '')}",
        "线索动作：\n- " + "\n- ".join(plan.get("thread_actions", [])),
        f"章末新状态：{plan.get('exit_state', '')}",
        f"结尾类型：{plan.get('ending_type', '')}",
        render_story_date(current),
    ]
    rendered = "\n".join(value for value in values if value.split("：", 1)[-1].strip("- \n"))
    debate = render_debate_for_prompt(current)  # "" for ordinary chapters
    return rendered + ("\n" + debate if debate else "")


def _chapter_boundary_contract(project: dict[str, Any], current_index: int) -> str:
    chapters = project.get("chapters", [])
    current = chapters[current_index] if current_index < len(chapters) else {}
    plan = current.get("plan", {}) if isinstance(current.get("plan"), dict) else {}
    target = max(1, int(project.get("narrative", {}).get("target_chapters", 30) or 30))
    number = current_index + 1
    final_chapter = number >= target
    previous_ending = ""
    if current_index > 0:
        previous = chapters[current_index - 1]
        previous_plan = previous.get("plan", {}) if isinstance(previous.get("plan"), dict) else {}
        previous_ending = str(
            previous_plan.get("exit_state")
            or previous_plan.get("ending_hook")
            or previous.get("summary", "")
        )
    opening_rule = (
        "从正在发生的具体动作、感官变化、对话压力或上一章后果切入；"
        "前10%内让读者知道谁在何处想完成什么、眼前阻力是什么。"
        "除非环境本身造成阻力，不以天气、景物全景、人物履历或主题议论热身。"
    )
    if current.get("content", "").strip():
        opening_rule = "这是续写：第一句只承接现有最后动作/感官/对话，不重新设计章节开头。"
    ending_rule = (
        "结尾必须落在本章选择已经造成的新状态上；钩子来自后果、期限、发现或关系变化，"
        "不得凭空闯入陌生人/新消息，也不得用泛泛感叹代替变化。"
    )
    if final_chapter:
        ending_rule = (
            "这是计划终章：回答核心问题，兑现主要因果与人物选择，呈现代价后的稳定状态；"
            "不再新增需要后文解释的秘密、反派、任务或悬崖。结尾类型必须是 closure 或余韵意象。"
        )
    return (
        f"章节位置：第{number}/{target}章{'（计划终章）' if final_chapter else ''}\n"
        f"上一章交付状态：{previous_ending}\n"
        f"计划开篇：{plan.get('opening_beat', '')}\n"
        f"计划章末：{plan.get('exit_state', '') or plan.get('goal', '')}\n"
        f"开头规则：{opening_rule}\n"
        f"中段规则：场景之间应存在因果或体验上的联系；具体节奏遵循所选叙事策略。\n"
        f"结尾规则：{ending_rule}"
    )


def _craft_brief(project: dict[str, Any], current_index: int) -> str:
    current = project.get("chapters", [])[current_index]
    plan = current.get("plan", {}) if isinstance(current.get("plan"), dict) else {}
    chapter_type = str(plan.get("chapter_type", "") or "推进")
    pov = str(plan.get("pov_character", "") or project.get("narrative", {}).get("pov", "auto"))
    emotional_turn = str(plan.get("emotional_turn", ""))
    return (
        f"本章功能：{chapter_type}；视角过滤器：{pov}；情绪转向：{emotional_turn}\n"
        "文笔执行：每段只承担一个主要动作（行动、观察、判断、对话或余波）；"
        "关键选择前放慢并给出可触摸的证据，过场压缩；抽象判断后立刻用动作、物件、空间距离或话语反应落地。\n"
        "对白执行：每个说话者都有当场目的；重要对白至少包含一次回避、试探、误解、交换或拒绝，"
        "不要让人物轮流解释设定。叙述不得替人物总结尚未做出的选择。\n"
        "细节执行：优先选择会被人物使用、误读、损坏或付出代价的细节；"
        "同一段不堆叠视觉、听觉、嗅觉清单，也不为“文艺”制造与人物无关的比喻。"
    )


def _continuation_anchor(text: str) -> str:
    if not text.strip():
        return "当前章节尚无正文，从本章计划中的首个动作直接开场。"
    paragraphs = [item.strip() for item in re.split(r"\n+", text) if item.strip()]
    tail = "\n\n".join(paragraphs[-2:])[-1800:]
    sentences = [
        item.strip()
        for item in re.split(r"(?<=[。！？!?])", text)
        if item.strip()
    ]
    last = sentences[-1] if sentences else text[-200:]
    return (
        f"原文最后一句：{last}\n原文最后两段：\n{tail}\n\n"
        "续写的第一句必须在动作、感官、对话或因果上直接承接此刻，"
        "不得复述最后一句，不得重新介绍场景，不得无提示跳时空或切换视角。"
    )


def _narrative_contract(project: dict[str, Any], current_index: int) -> str:
    narrative = project.get("narrative", {})
    mode = project.get("story_mode", "long")
    target = max(1, int(narrative.get("target_chapters", 30)))
    progress = min(1, (current_index + 1) / target)
    base = (
        f"作品形态：{'短篇/中短篇' if mode == 'short' else '长篇/连载'}\n"
        f"叙事视角：{narrative.get('pov', 'auto')}\n"
        f"时态：{narrative.get('tense', 'auto')}\n"
        f"总体气质：{narrative.get('tone', '')}\n"
        f"核心问题：{narrative.get('central_question', '')}\n"
        f"结局方向：{narrative.get('ending_direction', '')}\n"
        f"当前故事弧：{narrative.get('current_arc', '')}\n"
        f"计划篇幅：约 {target} 章；当前进度约 {progress:.0%}。"
    )
    if mode == "short":
        if progress >= 0.7:
            base += (
                "\n短篇已进入后段：禁止新增需要大量篇幅解释的新支线或核心设定；"
                "让既有选择产生代价，推动线索汇合、高潮与收束。"
            )
        else:
            base += (
                "\n短篇规则：围绕一个核心问题和一条主要冲突线；每个场景同时推进情节与人物，"
                "不铺设无法在计划篇幅内回收的新支线。"
            )
    else:
        base += (
            "\n长篇规则：本章只完成当前故事弧中的一步；维持主线与支线的层级，"
            "不要为了刺激连续升级力量或提前消耗终局秘密。"
        )
    return base


def _stage_progression_guard(project: dict[str, Any], current_index: int) -> str:
    """Make the current volume's unique job explicit instead of relying on a huge outline."""
    volumes = project.get("planning", {}).get("volumes", [])
    if not isinstance(volumes, list) or not volumes:
        return "本章必须造成至少一个可验证的新变化，不能只换地点或措辞重演上一章的问题。"
    chapter_number = current_index + 1
    active_index = 0
    for index, volume in enumerate(volumes):
        if not isinstance(volume, dict):
            continue
        start = int(volume.get("chapter_start", 1) or 1)
        end = int(volume.get("chapter_end", start + int(volume.get("chapter_count", 1) or 1) - 1) or start)
        if start <= chapter_number <= end:
            active_index = index
            break
    volume = volumes[active_index] if isinstance(volumes[active_index], dict) else {}
    previous = volumes[active_index - 1] if active_index > 0 and isinstance(volumes[active_index - 1], dict) else {}
    following = volumes[active_index + 1] if active_index + 1 < len(volumes) and isinstance(volumes[active_index + 1], dict) else {}
    return (
        f"当前卷：第{active_index + 1}卷《{volume.get('title', '')}》\n"
        f"本卷唯一主战场：{volume.get('primary_arena', volume.get('conflict', ''))}\n"
        f"本卷必须完成：{volume.get('goal', '')}\n"
        f"本卷不可逆变化：{volume.get('irreversible_change', volume.get('ending_state', ''))}\n"
        f"上一卷已经完成、不得重做：{previous.get('ending_state', '')}\n"
        f"下一卷才处理、不得抢写：{following.get('goal', '')}\n"
        "本章验收：必须改变信息、资源、关系、权限、风险或人物立场中的至少一项；"
        "若删去本章而后续毫无变化，说明本章无效，必须重新设计。"
    )


def _character_core(
    character: dict[str, Any], current_chapter_number: int | None = None
) -> str:
    name = str(character.get("name", "")).strip()
    ledger = [
        item
        for item in character.get("knowledge_ledger", [])
        if isinstance(item, dict)
        and item.get("active", True)
        and str(item.get("text", "")).strip()
        and (
            current_chapter_number is None
            or int(item.get("chapter_number", 0) or 0) == 0
            or int(item.get("chapter_number", 0) or 0) < current_chapter_number
        )
    ][-16:]
    ledger_text = "；".join(
        f"{item.get('text', '')}（{item.get('learned_how', '来源未标注')}，"
        f"{item.get('certainty', 'confirmed')}）"
        for item in ledger
    )
    return (
        f"【{name}】｜{character.get('importance', 'supporting')}\n"
        f"身份与不可变背景：{character.get('role', '')}；{character.get('description', '')}\n"
        f"人格内核：{character.get('personality', '')}\n"
        f"价值观与底线：{character.get('values', '')}\n"
        f"恐惧/软肋：{character.get('fears', '')}\n"
        f"内在矛盾：{character.get('contradictions', '')}\n"
        f"稳定外貌锚点：{character.get('appearance', '')}\n"
        f"当前可变外观（衣着/伤势/伪装）：{character.get('appearance_state', '')}\n"
        f"习惯动作：{character.get('mannerisms', '')}\n"
        f"绝不可写偏：{character.get('hard_limits', '')}\n"
        f"关系视角：{character.get('relationships', '')}\n"
        f"当前目标：{character.get('goal', '')}\n"
        f"当前状态：{character.get('state', '')}\n"
        f"所在位置：{character.get('location', '')}\n"
        f"开篇/人工维护的知情边界：{character.get('knowledge_baseline', character.get('knowledge', ''))}\n"
        f"有来源的新增知情：{ledger_text}\n"
        f"携带物品：{character.get('items', '')}\n"
        f"当前情绪：{character.get('emotion', '')}\n"
        f"语言指纹：{character.get('voice', '')}"
    )


def _character_context(
    project: dict[str, Any], query: str, limit: int = 10,
    current_chapter_number: int | None = None,
) -> str:
    active_names = _active_character_names(project, query)
    importance_scores = {"main": 8.0, "supporting": 3.0, "minor": 0.0}
    rendered: list[tuple[int, float, str]] = []
    for index, char in enumerate(project.get("characters", [])):
        name = str(char.get("name", "")).strip()
        if not name or not char.get("active", True):
            continue
        text = _character_core(char, current_chapter_number)
        boost = importance_scores.get(str(char.get("importance", "supporting")), 2.0)
        if name in active_names:
            boost += 20.0
        rendered.append((index, relevance(query, text) + boost, text[:4200]))
    if not rendered:
        return ""
    # Always retain one protagonist as the behavioural anchor, then add only
    # characters explicitly activated by this chapter. This prevents future
    # cast cards from leaking later arcs while keeping large-cast prompts safe.
    anchor = next((item for item in rendered if "】｜main" in item[2]), rendered[0])
    chosen = {anchor[0]}
    for item in sorted(rendered, key=lambda row: (row[1], -row[0]), reverse=True):
        character_name = str(
            project.get("characters", [])[item[0]].get("name", "")
        ).strip()
        if character_name in active_names:
            chosen.add(item[0])
        if len(chosen) >= limit:
            break
    return "\n\n".join(text for index, _, text in rendered if index in chosen)


def _character_voice_examples(project: dict[str, Any], query: str) -> str:
    if not project.get("settings", {}).get("include_dialogue_examples", True):
        return ""
    active_names = _active_character_names(project, query)
    blocks: list[str] = []
    for character in project.get("characters", []):
        name = str(character.get("name", "")).strip()
        if not name or name not in active_names:
            continue
        examples = character.get("dialogue_examples", [])
        if isinstance(examples, str):
            examples = [item.strip() for item in examples.splitlines() if item.strip()]
        examples = [str(item).strip() for item in examples if str(item).strip()][:3]
        if examples:
            blocks.append(
                f"【{name}对白范例｜只学习措辞、节奏和潜台词，不复制内容】\n- "
                + "\n- ".join(examples)
            )
    return "\n\n".join(blocks)


def _repetition_guard(
    project: dict[str, Any], current_index: int, active_names: set[str]
) -> str:
    chapters = project.get("chapters", [])
    source = "\n".join(
        str(item.get("content", ""))[-7000:]
        for item in chapters[max(0, current_index - 2) : current_index + 1]
    )
    sentence_starts: Counter[str] = Counter()
    paragraph_starts: Counter[str] = Counter()
    for sentence in re.split(r"[。！？!?]+", source):
        compact = re.sub(r"\s+", "", sentence)
        if len(compact) >= 8:
            sentence_starts[compact[:8]] += 1
    for paragraph in re.split(r"\n+", source):
        compact = re.sub(r"\s+", "", paragraph)
        if len(compact) >= 12:
            paragraph_starts[compact[:12]] += 1
    tired = [key for key, count in sentence_starts.items() if count >= 2][:8]
    tired += [key for key, count in paragraph_starts.items() if count >= 2][:6]
    ledger = [
        item
        for item in project.get("memory", {}).get("description_ledger", [])[-40:]
        if isinstance(item, dict)
        and (
            not active_names
            or str(item.get("character", "")) in active_names
        )
    ][-12:]
    ledger_lines = [
        f"- {item.get('character', '人物')}｜{item.get('aspect', '描写')}：{item.get('phrase', '')}"
        for item in ledger
        if str(item.get("phrase", "")).strip()
    ]
    global_guard = prompt_repetition_guard(
        project,
        str(project.get("chapters", [])[current_index].get("id", ""))
        if project.get("chapters") and current_index < len(project.get("chapters", []))
        else "",
    )
    return (
        "人物出场时只选择与当前动作、视角或情绪有关的1—2个外貌锚点；"
        "除非外貌发生变化，不得每次登场都复述整套衣着、眼神、伤痕或招牌动作。\n"
        "同一情绪优先通过新的选择、动作、语气和环境互动表现，不连续复用同一种身体反应或比喻。\n"
        + ("近期高频句段起笔（本次换一种句法）：" + "、".join(dict.fromkeys(tired)) + "\n" if tired else "")
        + ("最近已经使用过的显著描写（没有变化时不要原样再写）：\n" + "\n".join(ledger_lines) + "\n" if ledger_lines else "")
        + global_guard
    )


def _fit_atomic_section(section: PromptSection, token_limit: int) -> None:
    kept = []
    for unit in section.atomic_units:
        content = "\n\n".join([section.atomic_prefix, *kept, unit])
        if estimate_tokens(content) <= token_limit:
            kept.append(unit)
    section.content = "\n\n".join([section.atomic_prefix, *kept]) if kept else ""
    section.status = "trimmed" if kept else "omitted"
    section.reason = "方法步骤与示例必须完整，超出预算的 Skill 已整条省略"


def _fit_sections(
    sections: list[PromptSection], budget: int
) -> tuple[list[PromptSection], list[str]]:
    warnings: list[str] = []
    hard_caps = {
        "长期作者意图": 6000,
        "近期焦点": 4000,
        "本书不可违背规则": 8000,
        "章节场景契约": 5000,
        "伏笔与暗线治理议程": 7000,
        "文笔执行简报": 4000,
        "前置权威世界规则": 8000,
        "权威场景世界规则": 8000,
        "近端权威世界规则": 6000,
        "作品总纲": 12000,
        "分层导演规划": 10000,
        "人物权威状态": 14000,
        "人物与读者知情边界": 9000,
        "激活写作 Skills": 9000,
        "本场人物对白范例": 5000,
        "描写去重复约束": 5000,
        "全书滚动进展": 6000,
        "待确认连续性备注": 4000,
        "本章执行计划": 6000,
        "当前章节与最近正文": 18000,
        "续写边界锚点": 2400,
        "作者临时注": 4000,
    }
    for section in sections:
        section.original_tokens = estimate_tokens(section.content)
        section.status = "included"
        section.reason = ""
        cap = hard_caps.get(section.name)
        # Required authority may exceed the window, but it must never be
        # rewritten by a character cap. The final request guard will explain
        # the overflow. Recent prose intentionally retains a bounded tail.
        if section.required and not section.keep_tail:
            cap = None
        if cap and len(section.content) > cap:
            if section.atomic_units:
                _fit_atomic_section(section, estimate_tokens(section.content[:cap]))
                warnings.append(f"已整条选择写作方法：{section.name}")
                continue
            marker = "\n[…该区块过长，已保留关键部分…]\n"
            clipped = _clip_complete_units(section.content, cap, section.keep_tail)
            section.content = marker + clipped if section.keep_tail else clipped + marker
            section.status = "trimmed"
            section.reason = "超过该类上下文的安全长度上限"
            warnings.append(f"已限制超长区块：{section.name}")
    total = sum(estimate_tokens(section.content) for section in sections)
    if total <= budget:
        return sections, warnings

    for section in sorted(sections, key=lambda item: item.priority):
        if total <= budget:
            break
        if section.required or not section.content:
            continue
        if section.atomic_units:
            _fit_atomic_section(section, max(0, estimate_tokens(section.content) - (total - budget)))
            total = sum(estimate_tokens(item.content) for item in sections)
            warnings.append(f"已整条选择写作方法：{section.name}")
            continue
        original = section.content
        removable_tokens = max(
            0, estimate_tokens(section.content) - estimate_tokens(section.content[: section.min_chars])
        )
        need = total - budget
        cut_ratio = min(0.85, max(0.2, need / max(1, removable_tokens)))
        keep_chars = max(section.min_chars, int(len(section.content) * (1 - cut_ratio)))
        if keep_chars >= len(section.content):
            continue
        marker = "\n[…已按上下文预算裁剪…]\n"
        clipped = _clip_complete_units(original, keep_chars, section.keep_tail)
        section.content = marker + clipped if section.keep_tail else clipped + marker
        section.status = "trimmed"
        section.reason = "为模型输出预留上下文预算"
        total = sum(estimate_tokens(item.content) for item in sections)
        warnings.append(f"已裁剪：{section.name}")

    # The first pass respects each section's preferred minimum. In a genuinely
    # tight context window, make a second emergency pass over optional material
    # so the reserved generation space remains real rather than advisory.
    if total > budget:
        for section in sorted(sections, key=lambda item: item.priority):
            if total <= budget:
                break
            if section.required or not section.content:
                continue
            if section.atomic_units:
                _fit_atomic_section(section, max(0, estimate_tokens(section.content) - (total - budget)))
                total = sum(estimate_tokens(item.content) for item in sections)
                continue
            current_tokens = estimate_tokens(section.content)
            need = total - budget
            keep_tokens = max(0, current_tokens - need - 8)
            if keep_tokens < 30:
                section.content = ""
                section.status = "omitted"
                section.reason = "上下文预算不足，低优先级可选区块已省略"
            else:
                keep_chars = max(
                    60,
                    int(len(section.content) * keep_tokens / max(1, current_tokens)),
                )
                original = section.content
                marker = "\n[…为生成正文预留上下文，已进一步裁剪…]\n"
                clipped = _clip_complete_units(original, keep_chars, section.keep_tail)
                section.content = marker + clipped if section.keep_tail else clipped + marker
                section.status = "trimmed"
                section.reason = "上下文预算紧张，已进行第二轮裁剪"
            total = sum(estimate_tokens(item.content) for item in sections)
            warnings.append(f"已为输出空间进一步裁剪：{section.name}")

    if total > budget:
        warnings.append(f"固定上下文约 {total} tokens，超过预算 {budget}")
    return sections, warnings


def build_retrieval_query(project: dict[str, Any], request: dict[str, Any]) -> str:
    """Compile the same retrieval intent for prompt and persistent search layers."""
    instruction = str(request.get("instruction", "")).strip()
    selection = str(request.get("selection", "")).strip()
    chapter_id = request.get("chapter_id")
    chapters = project.get("chapters", [])
    current_index = next(
        (index for index, item in enumerate(chapters) if item.get("id") == chapter_id),
        max(0, len(chapters) - 1),
    )
    current = chapters[current_index] if chapters else {}
    settings = project.get("settings", {})
    recent_chars = int(settings.get("recent_chars", 12000))
    current_content = current.get("content", "")
    recent_text = current_content[-recent_chars:]
    return "\n".join(
        [
            instruction,
            selection,
            current.get("scene_goal", ""),
            _render_plan(current),
            recent_text[-3000:],
            project.get("current_focus", ""),
        ]
    )


def _experience_hint_text(project: dict[str, Any], request: dict[str, Any]) -> str:
    """Lessons from earlier audits (experience bank), rendered for one chapter."""
    if not craft_setting(project, "experience_bank"):
        return ""
    hints = request.get("_experience_hints")
    if isinstance(hints, str):
        hints = [hints]
    lines = [str(item).strip() for item in hints or [] if str(item).strip()][:3]
    if not lines:
        return ""
    return "以往审校中本模型反复出现的问题，本章写作时主动规避：\n" + "\n".join(f"- {item}" for item in lines)


def build_prompt(project: dict[str, Any], request: dict[str, Any]) -> PromptBuild:
    mode = request.get("mode", "continue")
    instruction = request.get("instruction", "").strip()
    selection = request.get("selection", "").strip()
    chapter_id = request.get("chapter_id")
    chapters = project.get("chapters", [])
    current_index = next(
        (index for index, item in enumerate(chapters) if item.get("id") == chapter_id),
        max(0, len(chapters) - 1),
    )
    project = chapter_context(project, current_index)
    chapters = project.get("chapters", [])
    current = chapters[current_index] if chapters else {}
    settings = project.get("settings", {})
    recent_chars = int(settings.get("recent_chars", 12000))
    current_content = current.get("content", "")
    recent_text = current_content[-recent_chars:]
    query = build_retrieval_query(project, request)
    thread_agenda_entries = select_thread_agenda(
        project,
        query,
        current_index,
        max(4, min(10, int(settings.get("memory_items", 12)) // 2)),
    )
    thread_agenda = render_thread_agenda(thread_agenda_entries)
    if thread_agenda:
        query += "\n" + thread_agenda
    active_character_names = _active_character_names(project, query)
    governed_names = {
        str(name).strip()
        for thread in thread_agenda_entries
        for key in ("stakeholders", "knowledge_holders")
        for name in thread.get(key, [])
        if str(name).strip()
    }
    active_character_names.update(governed_names)
    knowledge_context, knowledge_active_names = render_knowledge_context(
        project, query, sorted(active_character_names)
    )
    active_character_names.update(knowledge_active_names)
    canon_context = render_canon_context(project, query, sorted(active_character_names))
    reference_context = render_reference_context(project, query)
    lore = activate_lore(
        project,
        query,
        current_index,
        active_character_names,
        int(settings.get("lore_recursion_steps", 2)),
    )
    lore, lore_warnings = _budget_lore(
        lore, max(500, int(settings.get("lore_budget", 4500)))
    )
    memories = retrieve_memories(
        project,
        query,
        current_index,
        int(settings.get("memory_items", 12)),
        indexed_hits=request.get("_indexed_memory_hits", []),
    )
    activated_skills = activate_writing_skills(
        project,
        query,
        str(mode),
        explicit_ids=(
            request.get("skill_ids", [])
            or project.get("writing_skill_preferences", {}).get("manual_ids", [])
        ),
        user_skills=request.get("_user_writing_skills", []),
    )
    skills_context = render_writing_skills(activated_skills)

    lore_groups: dict[str, list[str]] = {
        "before": [], "after": [], "near": [],
        "before_hard": [], "after_hard": [], "near_hard": [],
    }
    for entry in lore:
        position = entry.get("position", "after")
        if position not in {"before", "after", "near"}:
            position = "after"
        group = position + ("_hard" if entry.get("canon") == "hard" else "")
        lore_groups[group].append(
            f"【{entry.get('title', '设定')}｜{entry.get('category', '世界设定')}】\n"
            f"{entry.get('content', '')}"
        )

    characters = _character_context(
        project, query, current_chapter_number=current_index + 1
    )
    epistemic_context = render_epistemic_context(
        project, current_index, query, active_character_names
    )
    character_examples = _character_voice_examples(project, query)
    professional_context = render_professional_context(project, active_character_names)
    repetition_guard = _repetition_guard(
        project, current_index, active_character_names
    )
    recent_summaries = "\n".join(
        f"- {chapter.get('title', '章节')}：{chapter.get('summary', '').strip()}"
        for chapter in chapters[max(0, current_index - 5) : current_index]
        if chapter.get("summary", "").strip()
    )
    continuity_notes = "\n".join(
        f"- {str(item.get('chapter_title', '')).strip() + '：' if str(item.get('chapter_title', '')).strip() else ''}"
        f"{item.get('text', '')}"
        for item in project.get("memory", {}).get("continuity_notes", [])
        if isinstance(item, dict)
        and not item.get("resolved", False)
        and str(item.get("text", "")).strip()
    )
    target_words = int(request.get("target_words") or settings.get("target_words", 1200))
    # Rewrite should preserve the selected passage's scale. The global chapter
    # target (often 1200-3000 chars) must not accidentally turn a 120-char
    # sentence repair into a new full chapter. "expand" intentionally keeps
    # using the explicit/global target.
    if (
        mode == "rewrite"
        and selection.strip()
        and not request.get("_full_chapter_rewrite", False)
    ):
        selected_chars = len(re.sub(r"\s+", "", selection))
        if selected_chars:
            target_words = max(100, selected_chars)
    style_payload = dict(project.get("style", {}))
    reference_style_corpus = combined_style_corpus(project, max_chars=30000)
    if reference_style_corpus:
        style_payload["sample"] = reference_style_corpus
    outline_text = str(project.get("outline", ""))
    master_outline = str(
        project.get("planning", {}).get("master", {}).get("full_outline", "")
    )
    if (
        outline_text.strip()
        and re.sub(r"\s+", "", outline_text)
        == re.sub(r"\s+", "", master_outline)
    ):
        outline_text = "已与AI详细全书大纲同步，详见“分层导演规划”区块。"
    mode_rules = {
        "continue": "从现有正文最后一句自然续写，不重复已有内容，不突然跳时空。",
        "instruction": "按照创作要求写新的正文，并与现有上下文无缝衔接。",
        "rewrite": "只重写选中文本，保留事实、人物意图和剧情功能。",
        "expand": "扩写选中文本，把概述变成完整场景，不改变既定结果。",
    }
    if mode == "rewrite" and not request.get("_full_chapter_rewrite", False):
        length_rule = (
            f"目标长度：约 {target_words} 个中文字符，尽量保持与选中文本同一量级；"
            "不要为了凑字数新增剧情。"
        )
    else:
        minimum_chars = max(80, int(target_words * 0.8))
        maximum_chars = max(minimum_chars + 40, int(target_words * 1.2))
        length_rule = (
            f"目标长度：约 {target_words} 个中文字符，建议落在 {minimum_chars}-{maximum_chars} 字。"
            f"在至少写到约 {minimum_chars} 字以前不要主动总结或提前收束；"
            "若场景尚未完成，应继续用动作、对话、环境变化和可验证的新信息推进，而不是重复描述。"
        )
    task = f"{mode_rules.get(mode, mode_rules['continue'])}\n{length_rule}"
    freedom_level, freedom_rule = creative_freedom_rule(settings)
    task += f"\n{freedom_rule}\n{narrative_guidance(project)}"
    if request.get("scene_id"):
        task += "\n" + scene_context(current, str(request["scene_id"]),
                                      request.get("selection", "") if mode in {"rewrite", "expand"} else None)
    if instruction:
        task += f"\n作者本次要求：{instruction}"
    if selection:
        task += f"\n待处理文本：\n{selection}"
    if mode in {"continue", "instruction"} and freedom_level == "strict":
        task += (
            "\n事实封闭原则：只有上方权威上下文明确写出的过去信息，才可以当作既有事实。"
            "不得新添人物或物品的来历、旧经历、旧约定、习惯、回忆、信件内容或过去时间点；"
            "若上下文没有答案，就保持未知。允许新增的事实只能是本场景此刻直接发生、"
            "被感官观察到或由角色明确说出的内容；人物猜测必须写成未证实的猜测。"
        )

    continuity_state = render_continuity_state(project, current_index)
    sections = [
        # Stable-prefix layout: the system message carries only content that is
        # byte-identical for every call in the same book (core protocol).  World
        # info is keyword-activated and therefore volatile, so it opens the user
        # message instead of breaking the cacheable prefix.
        PromptSection("核心协议", SYSTEM_PROMPT, 100, "system", True),
        PromptSection("前置权威世界规则", "\n\n".join(lore_groups["before_hard"]), 99, required=True),
        PromptSection("前置世界规则", "\n\n".join(lore_groups["before"]), 96),
        PromptSection("长期作者意图", project.get("author_intent", ""), 94, required=True),
        PromptSection("近期焦点", project.get("current_focus", ""), 98, required=True),
        PromptSection("本书不可违背规则", project.get("book_rules", ""), 99, required=True),
        PromptSection("叙事与篇幅契约", _narrative_contract(project, current_index), 98, required=True),
        PromptSection("阶段推进硬门", _stage_progression_guard(project, current_index), 100, required=True),
        PromptSection(
            "章节场景契约",
            _chapter_boundary_contract(project, current_index),
            100,
            required=True,
        ),
        PromptSection(
            "作品总纲",
            f"书名：{project.get('title', '')}\n类型：{project.get('genre', '')}\n"
            f"核心构想：{project.get('premise', '')}\n全书大纲：{outline_text}",
            70,
            min_chars=600,
        ),
        PromptSection(
            "分层导演规划",
            render_planning_context(project, current_index),
            99,
            required=True,
        ),
        PromptSection(
            "同人角色原作正典锁", canon_context, 100, required=bool(canon_context), min_chars=900
        ),
        PromptSection(
            "结构化知识约束", knowledge_context, 100, required=bool(knowledge_context), min_chars=700
        ),
        PromptSection(
            "人物与读者知情边界",
            epistemic_context,
            100,
            required=True,
        ),
        PromptSection(
            "激活写作 Skills",
            skills_context,
            91,
            required=False,
            min_chars=500,
            atomic_units=tuple(writing_skill_blocks(activated_skills)),
            atomic_prefix=WRITING_SKILL_BOUNDARY,
        ),
        PromptSection("人物权威状态", characters, 99, required=bool(characters), min_chars=500),
        PromptSection(
            "物品与伤势状态",
            continuity_state,
            97,
            required=bool(continuity_state),
        ),
        PromptSection(
            "事件溯源人物与关系状态",
            professional_context,
            100,
            required=bool(professional_context),
            min_chars=400,
        ),
        PromptSection("权威场景世界规则", "\n\n".join(lore_groups["after_hard"]), 99, required=True),
        PromptSection(
            "相关资料证据", reference_context, 88, required=False, min_chars=600
        ),
        PromptSection("激活的世界书", "\n\n".join(lore_groups["after"]), 89),
        PromptSection(
            "全书滚动进展",
            project.get("memory", {}).get("story_so_far", ""),
            93,
            min_chars=500,
        ),
        PromptSection(
            "待确认连续性备注",
            continuity_notes,
            98,
            required=bool(continuity_notes),
        ),
        PromptSection("相关长期记忆", render_memories(memories), 92, min_chars=500),
        PromptSection(
            "伏笔与暗线治理议程",
            thread_agenda,
            99,
            required=bool(thread_agenda),
        ),
        PromptSection("最近章节摘要", recent_summaries, 90, keep_tail=True, min_chars=500),
        PromptSection(
            "样本文风约束",
            render_style_context(style_payload, query, recent_text),
            73,
            min_chars=500,
        ),
        PromptSection(
            "原文局部文风指纹",
            render_fingerprint(recent_text[-5000:]),
            96,
            required=bool(recent_text),
        ),
        PromptSection(
            "文笔执行简报",
            _craft_brief(project, current_index),
            97,
            required=True,
        ),
        PromptSection(
            "本场人物对白范例",
            character_examples,
            94,
            min_chars=300,
        ),
        PromptSection(
            "对白演出素材",
            str(request.get("_rehearsal_material", "") or ""),
            93,
            min_chars=300,
        ),
        # Per-chapter (user message) blocks: they change every chapter, so they
        # must never enter the byte-stable system prefix.
        PromptSection(
            "待承接的章末钩子",
            (
                render_hook_debts_for_prompt(project, current_index + 1)
                if craft_setting(project, "hook_ledger")
                else ""
            ),
            95,
            min_chars=200,
        ),
        PromptSection(
            "跨书审校经验提示",
            _experience_hint_text(project, request),
            92,
            min_chars=200,
        ),
        PromptSection(
            "描写去重复约束",
            repetition_guard,
            98,
            required=True,
        ),
        PromptSection("作者确认的写作偏好", preference_context(project), 95, min_chars=200),
        PromptSection("本章执行计划", _render_plan(current), 99, required=True),
        PromptSection(
            "当前章节与最近正文",
            f"章节：{current.get('title', '')}\n本章摘要：{current.get('summary', '')}\n"
            f"最近正文：\n{recent_text}",
            97,
            required=True,
            keep_tail=True,
        ),
        PromptSection(
            "续写边界锚点",
            _continuation_anchor(current_content)
            if mode in {"continue", "instruction"}
            else "",
            100,
            required=True,
            keep_tail=True,
        ),
        PromptSection("近端权威世界规则", "\n\n".join(lore_groups["near_hard"]), 100, required=True),
        PromptSection("近端世界书", "\n\n".join(lore_groups["near"]), 95),
        PromptSection(
            "本章作者临时注",
            current.get("author_note", ""),
            100,
            required=True,
        ),
        PromptSection("本次任务", task, 100, required=True),
    ]
    sections = apply_task_prompt_policy(
        sections,
        mode=mode,
        full_chapter_rewrite=bool(request.get("_full_chapter_rewrite", False)),
    )
    sections = [section for section in sections if section.content.strip()]
    context_window = max(1200, int(settings.get("context_budget", 24000)))
    requested_output = max(256, int(settings.get("max_tokens", 3500)))
    margin = safety_margin(context_window)
    # llama.cpp counts prompt and generated tokens in the same context window.
    # Reserving output space here avoids late HTTP 400 errors or truncated prose.
    budget = max(800, context_window - requested_output - margin)
    heading_overhead = sum(
        estimate_tokens(section.name) + 6 for section in sections
    ) + 24
    sections, warnings = _fit_sections(
        sections, max(400, budget - heading_overhead)
    )
    warnings = lore_warnings + warnings
    requested_skills = set(request.get("skill_ids", []) or project.get("writing_skill_preferences", {}).get("manual_ids", []))
    selected_skill_ids = {s["id"] for s in activated_skills}
    if requested_skills - selected_skill_ids:
        warnings.append("部分手动 Skill 未加载：任务/题材不适用、已停用或超过数量上限")
    skill_section = next((s for s in sections if s.name == "激活写作 Skills"), None)
    for skill in activated_skills:
        block = writing_skill_blocks([skill])[0]
        skill["injected"] = bool(skill_section and block in skill_section.content)
    if skill_section and skill_section.status != "included":
        warnings.append("写作 Skills 已按预算整条取舍；已注入的方法保留完整步骤与示例")
    messages = []
    for role in ("system", "user"):
        content = "\n\n".join(
            f"## {section.name}\n{section.content.strip()}"
            for section in sections
            if section.role == role and section.content.strip()
        )
        if content:
            messages.append({"role": role, "content": content})
    tokens = estimate_tokens("\n".join(message["content"] for message in messages))
    return PromptBuild(
        messages=messages,
        activated_lore=lore,
        activated_skills=activated_skills,
        estimated_tokens=tokens,
        sections=[
            {
                "name": section.name,
                "content": section.content,
                "priority": section.priority,
                "tokens": estimate_tokens(section.content),
                "tokens_before": section.original_tokens,
                "tokens_after": estimate_tokens(section.content),
                "status": section.status,
                "selected": bool(section.content.strip()),
                "reason": section.reason,
                "role": section.role,
                "required": section.required,
            }
            for section in sections
        ],
        retrieved_memories=memories,
        budget_warnings=warnings,
    )

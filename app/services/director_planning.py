"""Model workflows that build a story from seed through chapter routes."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from ..domain.planning_validation import (
    _director_chapter_core_forbidden_terms,
    _director_chapter_forbidden_terms,
    _director_chapter_seed,
    _director_stage_event_terms,
    _director_stage_forbidden_terms,
    _director_stage_location_terms,
    _validate_director_stage_boundary,
    validate_asset_authority,
    validate_director_cast,
    validate_director_chapter_forbidden_terms,
    validate_director_chapter_seed,
    validate_director_character_card,
    validate_director_master_bible,
    validate_director_seed_brief,
    validate_director_volume_contract,
    validate_director_volume_core,
    validate_director_volume_details,
    validate_director_volume_domain,
    validate_director_world,
    validate_incubator_assets,
    validate_incubator_core_result,
    validate_route_batch,
)
from ..domain.route_validation import (
    _director_assigned_turn,
    _director_chapter_job,
    _director_historicalize_context,
    _director_job_prohibition,
    _director_outcome_reserved_by_turn,
    _director_routes_before_volume,
    _director_state_dimension,
    _route_safe_volume_synopsis,
    _route_similarity_score,
    validate_director_assigned_turn,
    validate_director_future_turns,
    validate_director_penultimate_bridge,
    validate_director_route_language,
    validate_director_route_role,
    validate_director_route_structure,
)
from ..llama_client import chat_once
from ..narrative_policy import profile as narrative_profile, route_guidance
from ..planning import (
    DIRECTOR_CHAPTER_ROUTE_PROMPT,
    DIRECTOR_MASTER_BIBLE_PROMPT,
    DIRECTOR_VOLUME_CONTRACT_PROMPT,
    DIRECTOR_VOLUME_CORE_PROMPT,
    DIRECTOR_VOLUME_DETAILS_PROMPT,
)
from ..prompts import (
    DIRECTOR_CAST_PROMPT,
    DIRECTOR_CHARACTER_CARD_PROMPT,
    DIRECTOR_SEED_BRIEF_PROMPT,
    DIRECTOR_WORLD_PROMPT,
    INCUBATOR_ASSETS_PROMPT,
    INCUBATOR_PROMPT,
)
from ..providers import settings_for_workload
from .model_errors import bounded_excerpt, planning_exception_detail
from .structured_output import parse_json_response


@dataclass(frozen=True)
class DirectorPlanningService:
    seed_brief: Callable[..., Any]
    seed_cast: Callable[..., Any]
    character_card: Callable[..., Any]
    seed_world: Callable[..., Any]
    master_bible: Callable[..., Any]
    master_contract: Callable[..., Any]
    master_volume_core: Callable[..., Any]
    master_volume_details: Callable[..., Any]
    plan_chapter_route: Callable[..., Any]
    full_outline: Callable[..., str]
    generate_incubator_core: Callable[..., Any]
    complete_incubator_option: Callable[..., Any]


def create_director_planning_service(
    structured_completion_callback: Callable[..., Any],
) -> DirectorPlanningService:
    """Bind structured generation once and expose cohesive planning operations."""
    structured_completion = structured_completion_callback

    async def director_seed_brief(
        project: dict[str, Any], config: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        target_chapters = max(3, int(config.get("target_chapters", 30) or 30))
        story_mode = "short" if config.get("story_mode") == "short" else "long"
        spine_hint = (
            "这是3-5章短篇：story_spine可压缩到约140-300字，但必须明确写出开篇、发展、"
            "关键转折/高潮和结局四个因果节点；多个节点可以落在同一章。"
            if story_mode == "short" and target_chapters <= 5
            else "story_spine必须覆盖开篇、发展、关键转折、高潮和结局，不能只写开篇阶段。"
        )
        context = f"""【作者核心灵感】
    {bounded_excerpt(config.get('seed', ''), 8000)}
    【必须保留与禁区】
    {bounded_excerpt(config.get('preferences', ''), 6000) or '没有额外补充。'}
    【作品形态】
    {'短篇/中短篇' if config.get('story_mode') == 'short' else '长篇/连载'}；计划约 {config.get('target_chapters', 30)} 章
    【故事骨架长度提示】
    {spine_hint}

    这是自动导演的第一检查点。这里只建立一套因果清楚、可继续扩写的故事骨架；详细分卷、逐章路线、人物卡和世界书会由后续独立步骤完成。"""
        return await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": DIRECTOR_SEED_BRIEF_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.58,
            max_tokens=2100,
            timeout_seconds=300,
            validate=lambda payload: validate_director_seed_brief(
                payload,
                target_chapters=target_chapters,
                story_mode=story_mode,
            ),
            token_ceiling=2400,
        )


    def _director_brief_context(
        brief: dict[str, Any], config: dict[str, Any]
    ) -> str:
        return f"""【已确认故事骨架】
    书名：{brief.get('title', '')}
    题材：{brief.get('genre', '')}
    核心构想：{brief.get('premise', '')}
    贯穿冲突：{brief.get('central_conflict', '')}
    故事发动机：{brief.get('story_engine', '')}
    故事主脊：{brief.get('story_spine', '')}
    开篇钩子：{brief.get('opening_hook', '')}
    前期故事弧：{brief.get('first_arc', '')}
    硬规则：{json.dumps(brief.get('book_rules', []), ensure_ascii=False)}
    【作者禁区】
    {bounded_excerpt(config.get('preferences', ''), 3000) or '没有额外补充。'}"""


    async def director_seed_cast(
        project: dict[str, Any], config: dict[str, Any], brief: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str]]:
        context = _director_brief_context(brief, config)
        return await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": DIRECTOR_CAST_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.55,
            max_tokens=900,
            timeout_seconds=240,
            validate=validate_director_cast,
            token_ceiling=1100,
        )


    async def director_character_card(
        project: dict[str, Any],
        config: dict[str, Any],
        brief: dict[str, Any],
        cast: list[dict[str, Any]],
        target: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        compact_cast = [
            {
                key: item.get(key, "")
                for key in ("name", "role", "narrative_function", "relationship_seed", "core_conflict")
            }
            for item in cast
        ]
        context = f"""{_director_brief_context(brief, config)}
    【全部主要人物名单】
    {json.dumps(compact_cast, ensure_ascii=False)}
    【本次只写此人】
    {json.dumps(target, ensure_ascii=False)}

    姓名和身份必须服从指定项。本次只返回这一张人物卡。"""
        def validate_card(payload: dict[str, Any]) -> None:
            validate_director_character_card(payload)
            validate_asset_authority(payload, context)

        result, warnings = await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": DIRECTOR_CHARACTER_CARD_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.58,
            max_tokens=1250,
            timeout_seconds=240,
            validate=validate_card,
            token_ceiling=1450,
        )
        # 姓名与功能由已验证的名单锁定，避免模型在扩写卡片时悄悄改名。
        result["name"] = str(target.get("name", result.get("name", ""))).strip()
        result["role"] = str(target.get("role", result.get("role", ""))).strip()
        result.setdefault("aliases", [])
        result.setdefault("dialogue_examples", [])
        return result, warnings


    async def director_seed_world(
        project: dict[str, Any],
        config: dict[str, Any],
        brief: dict[str, Any],
        cast: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[str]]:
        compact_cast = [
            {"name": item.get("name", ""), "role": item.get("role", "")}
            for item in cast
        ]
        context = f"""{_director_brief_context(brief, config)}
    【主要人物】
    {json.dumps(compact_cast, ensure_ascii=False)}

    本次只返回开篇必须保持一致的世界书条目。"""
        def validate_world(payload: dict[str, Any]) -> None:
            validate_director_world(payload)
            validate_asset_authority(payload, context)

        return await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": DIRECTOR_WORLD_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.52,
            max_tokens=1400,
            timeout_seconds=240,
            validate=validate_world,
            token_ceiling=1650,
        )


    def _director_master_context(project: dict[str, Any], instruction: str = "") -> str:
        narrative = project.get("narrative", {})
        characters = [
            {
                key: str(item.get(key, ""))[:220]
                for key in ("name", "role", "description", "personality", "values", "goal")
            }
            for item in project.get("characters", [])[:8]
        ]
        world = [
            {"title": str(item.get("title", ""))[:80], "content": str(item.get("content", ""))[:240]}
            for item in project.get("world_entries", [])[:8]
        ]
        return f"""【作品】
    书名：{project.get('title', '')}
    题材：{project.get('genre', '')}
    计划章节数：{narrative.get('target_chapters', 30)}
    核心构想：{project.get('premise', '')}
    现有故事主脊：{bounded_excerpt(project.get('outline', ''), 4200)}
    【作者控制】
    长期意图：{project.get('author_intent', '')}
    硬规则：{project.get('book_rules', '')}
    作者指定生产规格（若含编号分卷，顺序、卷名和阶段事件均高于模型自行发挥）：
    {bounded_excerpt(project.get('production_spec', ''), 12000)}
    核心问题：{narrative.get('central_question', '')}
    结局方向：{narrative.get('ending_direction', '')}
    总体气质：{narrative.get('tone', '')}
    【主要人物】
    {json.dumps(characters, ensure_ascii=False)}
    【必要世界设定】
    {json.dumps(world, ensure_ascii=False)}
    【本次补充】
    {instruction or '保持已有构想，建立可供后续分卷和拆章执行的因果结构。'}"""


    async def director_master_bible(
        project: dict[str, Any], instruction: str = ""
    ) -> tuple[dict[str, Any], list[str]]:
        return await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": DIRECTOR_MASTER_BIBLE_PROMPT},
                {"role": "user", "content": _director_master_context(project, instruction)},
            ],
            temperature=0.38,
            max_tokens=1800,
            timeout_seconds=300,
            validate=validate_director_master_bible,
            token_ceiling=2100,
        )


    async def director_master_contract(
        project: dict[str, Any],
        bible: dict[str, Any],
        specs: list[dict[str, int]],
        index: int,
        completed: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[str]]:
        spec = specs[index]
        production_spec = str(project.get("production_spec", ""))
        mandates = {
            int(number): {"title": title.strip(), "text": text.strip()}
            for number, title, text in re.findall(
                r"(?m)^\s*(\d{1,2})\.\s*《([^》]+)》([^\r\n]*)$",
                production_spec,
            )
        }
        mandate = mandates.get(index + 1, {})
        context = f"""【全书故事圣经】
    {json.dumps(bible, ensure_ascii=False)}
    【已有故事主脊】
    {bounded_excerpt(project.get('outline', ''), 2600)}
    【全书固定分卷范围】
    {json.dumps(specs, ensure_ascii=False)}
    【本次只建立此卷契约】
    {json.dumps(spec, ensure_ascii=False)}
    【已经保存的前卷契约】
    {json.dumps(completed, ensure_ascii=False)}
    【作者硬规则】
    {project.get('book_rules', '')}
    【作者指定的本卷强制蓝图】
    {json.dumps(mandate, ensure_ascii=False) if mandate else '未单独指定；服从故事圣经与前卷因果'}
    【结构位置】
    这是第 {index + 1}/{len(specs)} 卷；承接前卷但不得重复，非末卷不得提前完成终局。
    若强制蓝图非空，title 必须与其中 title 完全一致，阶段事件、不可逆变化和新问题不得挪到其他卷。"""
        messages = [
            {"role": "system", "content": DIRECTOR_VOLUME_CONTRACT_PROMPT},
            {"role": "user", "content": context},
        ]
        def validator(value: dict[str, Any]) -> None:
            validate_director_volume_contract(
                value,
                spec,
                completed,
                str(bible.get("ending_state", "")),
                index == len(specs) - 1,
            )
        def validate_with_mandate(value: dict[str, Any]) -> None:
            validator(value)
            if mandate and str(value.get("contract", {}).get("title", "")).strip() != mandate["title"]:
                raise ValueError(
                    f"第 {index + 1} 卷必须使用作者指定卷名《{mandate['title']}》"
                )
        try:
            result, warnings = await structured_completion(
                project.get("settings", {}),
                messages,
                temperature=0.44,
                max_tokens=1200,
                timeout_seconds=240,
                validate=validate_with_mandate,
                token_ceiling=1450,
            )
        except ValueError as first_error:
            # Local 8-12B models often understand the story but repeat the same
            # ending_state after a generic JSON repair. Give the contract two more
            # targeted attempts before treating this as a true structural blocker.
            repair_messages = [dict(message) for message in messages]
            repair_messages.append(
                {
                    "role": "user",
                    "content": (
                        f"前两次契约仍未通过：{planning_exception_detail(first_error)}。"
                        "这次必须先在心中对照所有前卷，另选一个此前没有使用过的主要场域、"
                        "不可逆变化和人物两难；非终卷不得出现全书终局。完整返回全部字段，"
                        "不要沿用被拒答案的句式。"
                    ),
                }
            )
            result, warnings = await structured_completion(
                project.get("settings", {}),
                repair_messages,
                temperature=0.62,
                max_tokens=1250,
                timeout_seconds=240,
                validate=validate_with_mandate,
                token_ceiling=1500,
            )
            warnings.insert(0, f"分卷契约已进入深度差异化修复：{planning_exception_detail(first_error)}")
        return result["contract"], warnings


    def _compact_volume_contracts(
        contracts: list[dict[str, Any]], current_index: int
    ) -> list[dict[str, Any]]:
        compact = []
        for index, item in enumerate(contracts):
            current = index == current_index
            compact.append(
                {
                    "number": item.get("number", index + 1),
                    "title": item.get("title", ""),
                    "goal": bounded_excerpt(str(item.get("goal", "")), 240 if current else 100),
                    "conflict": bounded_excerpt(str(item.get("conflict", "")), 260 if current else 0) if current else "",
                    "ending_state": bounded_excerpt(str(item.get("ending_state", "")), 200 if current else 80),
                    "bridge_to_next": bounded_excerpt(str(item.get("bridge_to_next", "")), 140 if current else 60),
                    "theme_test": bounded_excerpt(str(item.get("theme_test", "")), 140 if current else 0) if current else "",
                    "primary_arena": bounded_excerpt(str(item.get("primary_arena", "")), 120 if current else 60),
                    "time_span": bounded_excerpt(str(item.get("time_span", "")), 80),
                    "irreversible_change": bounded_excerpt(str(item.get("irreversible_change", "")), 160 if current else 70),
                    "character_choice": bounded_excerpt(str(item.get("character_choice", "")), 150 if current else 0) if current else "",
                    "new_story_question": bounded_excerpt(str(item.get("new_story_question", "")), 130 if current else 50),
                }
            )
        return compact


    async def director_master_volume_core(
        project: dict[str, Any],
        bible: dict[str, Any],
        contracts: list[dict[str, Any]],
        specs: list[dict[str, int]],
        index: int,
        completed: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[str]]:
        contract = contracts[index]
        spec = specs[index]
        previous = [
            {
                "number": pos + 1,
                "title": item.get("title", ""),
                "ending_state": item.get("ending_state", ""),
                "bridge_to_next": item.get("bridge_to_next", ""),
            }
            for pos, item in enumerate(completed)
        ]
        bible_core = {
            key: bible.get(key, "")
            for key in (
                "theme", "central_conflict", "story_engine", "pacing_plan",
            )
        }
        next_boundary = (
            {
                "number": contracts[index + 1].get("number", index + 2),
                "title": contracts[index + 1].get("title", ""),
            }
            if index + 1 < len(contracts)
            else {"number": "终卷", "title": "无下一卷"}
        )
        context = f"""【精简故事圣经】
    {json.dumps(bible_core, ensure_ascii=False)}
    【本次固定范围】
    {json.dumps(spec, ensure_ascii=False)}
    【本次必须扩写的契约】
    {json.dumps(_compact_volume_contracts([contract], 0)[0], ensure_ascii=False)}
    【前卷已确定的结局与衔接】
    {json.dumps(previous, ensure_ascii=False)}
    【下一卷边界；只能由 bridge_to_next 触发，不得在本卷实际展开】
    {json.dumps(next_boundary, ensure_ascii=False)}
    【作者硬规则】
    {bounded_excerpt(project.get('book_rules', ''), 1800)}
    【阶段禁入】
    本卷正文事件不得出现：{json.dumps(_director_stage_forbidden_terms(project, index + 1), ensure_ascii=False)}
    这里只写本卷十章的完整因果链，绝不能摘要全书或提前讲述后续卷。"""
        messages = [
            {"role": "system", "content": DIRECTOR_VOLUME_CORE_PROMPT},
            {"role": "user", "content": context},
        ]
        def validator(payload: dict[str, Any]) -> None:
            validate_director_volume_core(payload, spec["chapter_count"])
        def validate_core_with_stage_boundary(payload: dict[str, Any]) -> None:
            validator(payload)
            core = payload.get("volume_core", {})
            _validate_director_stage_boundary(
                project,
                index + 1,
                " ".join(str(core.get(field, "")) for field in ("synopsis", "conflict")),
            )
        try:
            result, warnings = await structured_completion(
                project.get("settings", {}),
                messages,
                temperature=0.42,
                max_tokens=850,
                timeout_seconds=210,
                validate=validate_core_with_stage_boundary,
                token_ceiling=1050,
            )
        except ValueError as first_error:
            repair_messages = [dict(message) for message in messages]
            repair_messages.append(
                {
                    "role": "user",
                    "content": (
                        f"上次分卷核心未通过：{planning_exception_detail(first_error)}。"
                        "保持作者指定卷名、阶段事件、不可逆变化和前后衔接不变；"
                        "把 synopsis 写成 220—300 字的完整因果链，明确起因、连续升级、"
                        "人物选择、可见代价、卷末结果。不得用同义反复凑字。"
                        "完整返回全部 volume_core 字段，只输出闭合 JSON。"
                    ),
                }
            )
            result, warnings = await structured_completion(
                project.get("settings", {}),
                repair_messages,
                temperature=0.52,
                max_tokens=950,
                timeout_seconds=240,
                validate=validate_core_with_stage_boundary,
                token_ceiling=1150,
            )
            warnings.insert(
                0,
                "分卷剧情核心已在长度或结构门禁后执行定向修复："
                + planning_exception_detail(first_error),
            )
        core = result["volume_core"]
        for field in ("title", "goal", "conflict", "ending_state", "bridge_to_next"):
            core[field] = str(contract.get(field, core.get(field, ""))).strip()
        core["chapter_count"] = spec["chapter_count"]
        return core, warnings


    async def director_master_volume_details(
        project: dict[str, Any],
        bible: dict[str, Any],
        contracts: list[dict[str, Any]],
        specs: list[dict[str, int]],
        index: int,
        core: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        context = f"""【全书主题；只用于检验本卷选择，不得摘要后续人物弧】
    {bounded_excerpt(str(bible.get('theme', '')), 500)}
    【本卷范围与契约】
    {json.dumps(specs[index], ensure_ascii=False)}
    {json.dumps(_compact_volume_contracts([contracts[index]], 0)[0], ensure_ascii=False)}
    【已经确定的本卷剧情核心，不得复述或改变】
    {json.dumps(core, ensure_ascii=False)}
    【作者硬规则】
    {bounded_excerpt(project.get('book_rules', ''), 1600)}
    【阶段禁入】
    本卷转折、人物弧、支线和 must_keep 不得出现：{json.dumps(_director_stage_forbidden_terms(project, index + 1), ensure_ascii=False)}
    只补本卷十章内发生的事项，不得借人物弧提前讲述后续卷。"""
        def validate_details_with_stage_boundary(payload: dict[str, Any]) -> None:
            validate_director_volume_details(payload)
            details = payload.get("volume_details", {})
            governed = []
            for field in ("turning_points", "character_arcs", "subplots", "must_keep"):
                governed.extend(details.get(field, []) if isinstance(details.get(field), list) else [])
            _validate_director_stage_boundary(
                project, index + 1, " ".join(str(item) for item in governed)
            )

        messages = [
            {"role": "system", "content": DIRECTOR_VOLUME_DETAILS_PROMPT},
            {"role": "user", "content": context},
        ]
        try:
            result, warnings = await structured_completion(
                project.get("settings", {}),
                messages,
                temperature=0.38,
                max_tokens=850,
                timeout_seconds=210,
                validate=validate_details_with_stage_boundary,
                token_ceiling=1050,
            )
        except ValueError as first_error:
            repair_messages = [dict(message) for message in messages]
            repair_messages.append(
                {
                    "role": "user",
                    "content": (
                        f"上次执行清单未通过：{planning_exception_detail(first_error)}。"
                        "废弃被拒清单，只围绕当前卷 synopsis 重新设计 3—5 个具体转折、"
                        "2—5 条本卷人物变化和本卷支线；不得使用阶段禁入词，也不得提前"
                        "讲后续卷。完整返回 volume_details，只输出闭合 JSON。"
                    ),
                }
            )
            result, warnings = await structured_completion(
                project.get("settings", {}),
                repair_messages,
                temperature=0.52,
                max_tokens=900,
                timeout_seconds=240,
                validate=validate_details_with_stage_boundary,
                token_ceiling=1100,
            )
            warnings.insert(
                0,
                "分卷执行清单已在阶段门禁后执行定向重构："
                + planning_exception_detail(first_error),
            )
        return result["volume_details"], warnings


    async def director_plan_chapter_route(
        project: dict[str, Any],
        volume: dict[str, Any],
        chapter_number: int,
        previous_routes: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[str]]:
        start, end = int(volume["chapter_start"]), int(volume["chapter_end"])
        position = chapter_number - start
        count = end - start + 1
        final = chapter_number == end
        assigned_turn = _director_assigned_turn(volume, position, count)
        chapter_seed = _director_chapter_seed(
            project, int(volume.get("number", 0) or 0), chapter_number
        )
        chapter_forbidden_terms = _director_chapter_forbidden_terms(
            project, int(volume.get("number", 0) or 0), chapter_number
        )
        chapter_core_forbidden_terms = _director_chapter_core_forbidden_terms(
            project, int(volume.get("number", 0) or 0), chapter_number
        )
        effective_job = (
            "作者章种子执行：完成种子指定的场景、行动、局部结果与代价"
            if chapter_seed else _director_chapter_job(position, count)
        )
        effective_dimension = (
            "作者章种子明确规定的局部状态"
            if chapter_seed else _director_state_dimension(position, count)
        )
        effective_prohibition = (
            "不得用通用节拍覆盖章种子，不得停在种子要求之前，也不得越过种子提前完成卷末"
            if chapter_seed else _director_job_prohibition(position, count)
        )
        if not chapter_seed and narrative_profile(project) != "legacy":
            effective_job, effective_dimension, effective_prohibition = route_guidance(project, final)
        master = project.get("planning", {}).get("master", {})
        earlier_routes = _director_routes_before_volume(project, volume)
        comparison_routes = earlier_routes + previous_routes
        volume_event_palette = {
            "primary_arena": volume.get("primary_arena", ""),
            "assigned_turn_for_this_chapter": assigned_turn or "本章没有卷级转折配额",
            "required_event_terms": _director_stage_event_terms(
                project, int(volume.get("number", 0) or 0)
            ),
            "single_scene_locations": _director_stage_location_terms(
                project, int(volume.get("number", 0) or 0)
            ),
        }
        future_turns = [
            (start + later, turn)
            for later in range(position + 1, count)
            if (turn := _director_assigned_turn(volume, later, count))
        ]
        safe_synopsis = _route_safe_volume_synopsis(
            str(volume.get("synopsis", "")), future_turns
        )
        current_volume_chain = [
            {
                "number": item.get("number"),
                "title": item.get("title", ""),
                "goal": bounded_excerpt(str(item.get("goal", "")), 90),
                "ending_hook": bounded_excerpt(str(item.get("ending_hook", "")), 80),
            }
            for item in previous_routes
        ]
        characters = [
            {"name": item.get("name", ""), "role": item.get("role", "")}
            for item in project.get("characters", [])[:8]
        ]
        context = f"""【全书方向】
    核心主题：{bounded_excerpt(master.get('theme', ''), 260)}
    全书核心冲突：{bounded_excerpt(master.get('central_conflict', ''), 360)}
    【本卷】
    卷名：{volume.get('title', '')}（第 {start}-{end} 章）
    当前章可见的本卷因果背景：{bounded_excerpt(safe_synopsis, 1300)}
    卷目标：{bounded_excerpt(volume.get('goal', ''), 400)}
    主导冲突：{bounded_excerpt(volume.get('conflict', ''), 500)}
    本卷唯一主场域：{bounded_excerpt(volume.get('primary_arena', ''), 220)}
    明确时间跨度：{bounded_excerpt(volume.get('time_span', ''), 160)}
    本卷不可逆变化：{bounded_excerpt(volume.get('irreversible_change', ''), 260)}
    核心人物不可兼得选择：{bounded_excerpt(volume.get('character_choice', ''), 260)}
    卷末状态：{bounded_excerpt(volume.get('ending_state', ''), 400)}
    下一卷触发：{bounded_excerpt(volume.get('bridge_to_next', ''), 300)}
    【本卷事件材料白名单】
    {json.dumps(volume_event_palette, ensure_ascii=False)}
    本章主要地点、机构、器物、受害者和对抗方式必须来自上述本卷材料；材料中没有的前卷专属粮仓、密档、调令、审查机构只能作为一句因果背景，不得再次成为本章主要事件或证物。
    【本卷已经保存的因果链】
    {json.dumps(current_volume_chain, ensure_ascii=False) if current_volume_chain else '本章是本卷开篇，尚无前章路线'}
    必须直接承接最后一条 ending_hook；不得把本卷已经发生的事件重新开场。must_keep 与 must_avoid 各只选本章直接相关的 1-3 条，不得复制整卷清单。
    【本次唯一任务】
    只规划第 {chapter_number} 章，这是本卷第 {position + 1}/{count} 章。
    作者指定章种子：{chapter_seed or '未指定；只按本章岗位设计局部事件'}
    若章种子非空，它高于模型自行选择的事件；必须保留其中的主场景、关键物件与局部结果，不得换成同卷其他转折。
    本章作者禁入词：{json.dumps(chapter_forbidden_terms, ensure_ascii=False) if chapter_forbidden_terms else '无额外章级禁入'}
    本章核心禁入词（可作为 ending_hook，但不得进入标题、目标、冲突或转折）：{json.dumps(chapter_core_forbidden_terms, ensure_ascii=False) if chapter_core_forbidden_terms else '无'}
    本章岗位：{effective_job}
    本章关注的变化：{effective_dimension}。goal 应由具体人物和事件承载，避免空泛重复。
    本章岗位禁令：{effective_prohibition}
    岗位与状态维度只是内部导演指令，title、goal、conflict、turning_point、ending_hook 中不得复述“形成两难、可选方案集合、主动放弃退路、外部社会后果、关系裂痕、信誉受损”等节拍术语；必须写成点名人物围绕本书题材中的具体对象采取了什么行动，章末实际改变了什么。
    本章承载转折：{assigned_turn or '本章不兑现卷级关键转折，只完成岗位规定的局部状态变化'}
    如果“本章承载转折”给出了具体事件，title、goal、conflict 或 turning_point 中必须直接保留该事件至少两个具体对象或动作（如地名、人物、器物、命令、罢工或灾情），不得换回前卷的粮册、密档、调令故事。
    这是卷末章：{'是，必须完成卷目标并形成下一卷触发' if final else '否，严禁提前兑现卷目标、卷末状态或下一卷桥梁'}
    【历史隔离】
    全书此前已有 {len(comparison_routes)} 条路线。其文本故意不提供，避免把旧卷地点、机构和证物污染到本卷；服务端会自动检查重名与同功能重复，若撞车会在修订轮只指出那一条。
    【可用人物】
    {json.dumps(characters, ensure_ascii=False)}
    以上是当前可用人物。临时配角应符合本书题材和时代；未列出的原作或历史实名人物不得擅自加入或改写身份。
    【全书硬规则】
    {bounded_excerpt(project.get('book_rules', ''), 1600)}"""
        context = _director_historicalize_context(project, context)

        forbidden = [] if final else [
            outcome
            for outcome in (
                str(volume.get("goal", "")),
                str(volume.get("ending_state", "")),
                str(volume.get("irreversible_change", "")),
                str(volume.get("bridge_to_next", "")),
            )
            if not _director_outcome_reserved_by_turn(outcome, assigned_turn)
        ]
        base_messages = [
            {"role": "system", "content": DIRECTOR_CHAPTER_ROUTE_PROMPT},
            {"role": "user", "content": context},
        ]
        last_error: Exception | None = None
        previous_candidate: dict[str, Any] | None = None
        best_candidate: dict[str, Any] | None = None
        best_issue = ""
        best_score = float("inf")

        # A malformed response is a structural failure.  A complete route rejected
        # by a quality gate is recoverable: ask the model to alter the event/state,
        # retain the most distinct candidate, and never discard earlier checkpoints.
        for attempt in range(6):
            messages = [dict(message) for message in base_messages]
            if last_error is not None:
                issue_detail = planning_exception_detail(last_error)
                future_turn_leak = "提前占用第" in issue_detail
                structural_duplicate_issue = any(
                    marker in issue_detail
                    for marker in (
                        "标题重复", "标题高度相似", "标题高度近似", "核心意象",
                        "目标高度相似", "目标高度重复", "目标重复",
                        "核心冲突高度重复", "关键转折高度重复", "章末变化高度重复",
                    )
                )
                model_issue_detail = (
                    "上一候选提前使用了只允许在后续章节出现的保留转折。"
                    "必须废弃该候选的证物、发现、制度手段和结论"
                    if future_turn_leak
                    else issue_detail
                )
                repair = (
                    f"第 {attempt} 次候选未通过检查：{model_issue_detail}。"
                    "请重新设计本章的具体事件和状态变化，不要只替换同义词。"
                    f"必须服从本章岗位“{effective_job}”与唯一状态维度"
                    f"“{effective_dimension}”，并避开已经保存路线的目标、"
                    "转折和结果。只返回完整闭合 JSON。"
                )
                if "时代语言质量问题" in issue_detail:
                    repair += (
                        "\n上轮含有现代词。请直接改用战国官署、简牍、度量、道路、"
                        "赋税和实物后果的说法；不要复述被拒候选，也不要解释你避开了哪些词。"
                    )
                if "核心意象" in issue_detail:
                    repair += (
                        "\n上轮标题沿用了本卷已经高频出现的核心名词。只改标题："
                        "改用本章新出现的具体人物动作、实物后果或地点作为意象；"
                        "标题不得再包含检查信息中点名的高频词。正文事件仍须承接前章。"
                    )
                if future_turn_leak:
                    repair += (
                        "\n不要猜测、复述或改写被保留的后续发现。本章只能从当前权限、"
                        "眼前阻力或尚未解决的危机中选一个较早的局部变化；turning_point "
                        "必须更换人物行动、对象和结果三者。"
                    )
                duplicate_match = re.search(
                    r"第\s*(\d+)\s*与第\s*(\d+)\s*条章节(?:目标|核心冲突|关键转折|章末变化)高度重复",
                    issue_detail,
                )
                if duplicate_match:
                    earlier_index = int(duplicate_match.group(1)) - 1
                    if 0 <= earlier_index < len(comparison_routes):
                        conflicting_route = comparison_routes[earlier_index]
                        repair += (
                            "\n与本候选冲突的既有路线如下。新候选必须更换事件对象、行动方式与"
                            "章末状态，不能沿用同一制度动作：\n"
                            + json.dumps(
                                {
                                    "number": conflicting_route.get("number"),
                                    "title": conflicting_route.get("title", ""),
                                    "goal": conflicting_route.get("goal", ""),
                                    "conflict": conflicting_route.get("conflict", ""),
                                    "turning_point": conflicting_route.get("turning_point", ""),
                                },
                                ensure_ascii=False,
                            )
                        )
                if (
                    previous_candidate
                    and "时代语言质量问题" not in issue_detail
                    and not future_turn_leak
                    and not structural_duplicate_issue
                ):
                    repair += (
                        "\n被拒候选如下，只用于识别问题，不得复述：\n"
                        + json.dumps(previous_candidate, ensure_ascii=False)
                    )
                messages.append({"role": "user", "content": repair})
            try:
                raw = await chat_once(
                    settings_for_workload(
                        project.get("settings", {}), "reasoning"
                    ),
                    messages,
                    temperature=min(0.86, 0.46 + attempt * 0.08),
                    max_tokens=max(2400, 850 + attempt * 40),
                    json_mode=True,
                    timeout_seconds=210,
                )
                result = parse_json_response(raw)
                candidate = validate_director_route_structure(result, chapter_number)
                previous_candidate = candidate
            except Exception as exc:
                last_error = exc
                previous_candidate = None
                continue

            try:
                validate_route_batch(
                    {"chapters": [candidate]},
                    [chapter_number],
                    comparison_routes,
                    forbidden,
                )
                if not chapter_seed:
                    if narrative_profile(project) == "legacy":
                        validate_director_route_role(candidate, position, count)
                    validate_director_penultimate_bridge(
                        candidate, volume, position, count
                    )
                validate_director_route_language(project, candidate)
                if not chapter_seed:
                    validate_director_assigned_turn(candidate, assigned_turn)
                    validate_director_future_turns(candidate, future_turns)
                if not chapter_seed:
                    validate_director_volume_domain(
                        project,
                        int(volume.get("number", 0) or 0),
                        candidate,
                    )
                validate_director_chapter_seed(candidate, chapter_seed)
                validate_director_chapter_forbidden_terms(
                    candidate, chapter_forbidden_terms
                )
                validate_director_chapter_forbidden_terms(
                    candidate, chapter_core_forbidden_terms, include_hook=False
                )
                _validate_director_stage_boundary(
                    project,
                    int(volume.get("number", 0) or 0),
                    " ".join(
                        str(candidate.get(field, ""))
                        for field in (
                            ("title", "goal", "conflict", "turning_point")
                            if position == count - 1
                            else ("title", "goal", "conflict", "turning_point", "ending_hook")
                        )
                    ),
                )
                warnings = []
                if attempt:
                    warnings.append(
                        f"章节路线第 {attempt + 1} 轮定向修复后通过质量门"
                    )
                return candidate, warnings
            except Exception as exc:
                last_error = exc
                issue = planning_exception_detail(exc)
                penalty = 0.0
                if "时代语言" in issue:
                    penalty += 0.75
                if "提前兑现" in issue:
                    penalty += 0.65
                if "岗位高度重复" in issue:
                    penalty += 0.55
                score = _route_similarity_score(candidate, comparison_routes) + penalty
                if score < best_score:
                    best_candidate = deepcopy(candidate)
                    best_issue = issue
                    best_score = score

        if best_candidate is not None:
            if any(
                marker in best_issue
                for marker in (
                    "标题重复", "标题高度相似", "目标高度相似", "目标高度重复", "目标重复",
                    "标题高度近似", "核心冲突高度重复", "关键转折高度重复", "章末变化高度重复",
                    "未来阶段串线", "提前兑现", "岗位高度重复",
                    "导演节拍术语", "核心意象", "未承载指定卷级转折",
                    "提前占用第", "时代语言质量问题",
                    "时代身份质量问题", "制度可信度问题", "章节岗位提前升级",
                    "章末变化是预告", "卷末部署过度", "卷末桥梁冲突",
                    "偏离本卷事件材料", "单章主场域过多",
                    "未承载作者指定章种子",
                    "作者章级禁入词",
                )
            ):
                candidate_summary = (
                    f"；差异度最高的被拒候选：标题《{bounded_excerpt(str(best_candidate.get('title', '')), 60)}》；"
                    f"目标：{bounded_excerpt(str(best_candidate.get('goal', '')), 180)}；"
                    f"冲突：{bounded_excerpt(str(best_candidate.get('conflict', '')), 180)}；"
                    f"转折：{bounded_excerpt(str(best_candidate.get('turning_point', '')), 160)}"
                )
                raise ValueError(
                    "章节路线连续六轮仍存在不可接受的结构重复："
                    + best_issue
                    + candidate_summary
                )
            warning = (
                "章节路线经过 6 轮定向修复仍有软质量风险，系统已选择差异度最高的"
                f"完整候选并记录规划质量债务：{best_issue}"
            )
            best_candidate["quality_warnings"] = [best_issue]
            return best_candidate, [warning]
        if last_error is not None:
            raise last_error
        raise ValueError("章节路线生成未返回任何可检查结果")


    def _director_full_outline(volumes: list[dict[str, Any]]) -> str:
        sections = []
        for index, volume in enumerate(volumes, start=1):
            turns = "；".join(str(item) for item in volume.get("turning_points", []) if str(item).strip())
            sections.append(
                f"第{index}卷《{volume.get('title', '')}》\n"
                f"阶段目标：{volume.get('goal', '')}\n"
                f"主导冲突：{volume.get('conflict', '')}\n"
                f"剧情展开：{volume.get('synopsis', '')}\n"
                f"关键转折：{turns}\n"
                f"卷末状态：{volume.get('ending_state', '')}\n"
                f"后续触发：{volume.get('bridge_to_next', '')}"
            )
        return "\n\n".join(sections)


    def _incubator_context(
        project: dict[str, Any], seed: str, preferences: str,
        story_mode: str, target: int,
    ) -> str:
        return f"""【作者的原始灵感】
    {bounded_excerpt(seed, 8000)}
    【作者补充偏好与禁区】
    {bounded_excerpt(preferences, 6000) or '没有额外要求，由总导演提供两种不同但可持续的方向。'}
    【作品形态与计划长度】
    {'短篇/中短篇' if story_mode == 'short' else '长篇/连载'}；约 {target} 章
    【当前作品里可以继承的内容】
    题材：{project.get('genre', '')}
    长期作者意图：{bounded_excerpt(project.get('author_intent', ''), 2500)}
    硬规则：{bounded_excerpt(project.get('book_rules', ''), 3500)}
    已有文风方向：{bounded_excerpt(project.get('style', {}).get('profile', ''), 1500)}

    作者只要求把灵感发展成可选择、可编辑、可继续做全书规划的开书方案。
    不要把当前作品的旧剧情和人物强行带入，除非作者在灵感或偏好中明确要求继承。"""


    async def _generate_incubator_core(
        project: dict[str, Any], seed: str, preferences: str,
        story_mode: str, target: int,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        context = _incubator_context(project, seed, preferences, story_mode, target)
        result, warnings = await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": INCUBATOR_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.64,
            max_tokens=3600,
            timeout_seconds=420,
            validate=lambda payload: validate_incubator_core_result(
                payload,
                target_chapters=target,
                story_mode=story_mode,
            ),
            token_ceiling=max(
                3900,
                min(
                    4800,
                    int(project.get("settings", {}).get("context_budget", 24000)) - 1800,
                ),
            ),
        )
        return [deepcopy(item) for item in result["options"]], list(warnings)


    async def _complete_incubator_option(
        project: dict[str, Any], raw_option: dict[str, Any], *, index: int,
        seed: str, preferences: str, target: int,
    ) -> tuple[dict[str, Any], list[str]]:
        option = deepcopy(raw_option)
        option["target_chapters"] = target
        asset_context = f"""【已经确定的候选方向；不得改写】
    {json.dumps(option, ensure_ascii=False)}
    【作者原始灵感】
    {bounded_excerpt(seed, 3500)}
    【作者偏好与禁区】
    {bounded_excerpt(preferences, 3500) or '没有额外补充。'}

    这是第 {index}/2 套候选方向。只补充这套方向的人物和世界资产。"""

        def validate_assets(payload: dict[str, Any]) -> None:
            validate_incubator_assets(payload)
            validate_asset_authority(payload, asset_context)

        assets, warnings = await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": INCUBATOR_ASSETS_PROMPT},
                {"role": "user", "content": asset_context},
            ],
            temperature=0.52,
            max_tokens=3000,
            timeout_seconds=360,
            validate=validate_assets,
            token_ceiling=3600,
        )
        option["characters"] = assets["characters"]
        option["world_entries"] = assets["world_entries"]
        return option, [f"第 {index} 套资产：{warning}" for warning in warnings]

    return DirectorPlanningService(
        seed_brief=director_seed_brief,
        seed_cast=director_seed_cast,
        character_card=director_character_card,
        seed_world=director_seed_world,
        master_bible=director_master_bible,
        master_contract=director_master_contract,
        master_volume_core=director_master_volume_core,
        master_volume_details=director_master_volume_details,
        plan_chapter_route=director_plan_chapter_route,
        full_outline=_director_full_outline,
        generate_incubator_core=_generate_incubator_core,
        complete_incubator_option=_complete_incubator_option,
    )

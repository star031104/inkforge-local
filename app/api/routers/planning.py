"""Chapter, master-story, and volume planning HTTP workflows."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from ...db import ensure_project_defaults
from ...memory import (
    render_memories,
    render_thread_agenda,
    retrieve_memories,
    select_thread_agenda,
)
from ...planning import (
    MASTER_CORE_PROMPT,
    MASTER_VOLUMES_PROMPT,
    VOLUME_PLAN_PROMPT,
    apply_volume_routes,
    fallback_chapter_plan,
    fallback_master_plan,
    find_volume,
    normalize_master_plan,
    normalize_route,
    render_planning_context,
)
from ...prompts import CHAPTER_PLAN_PROMPT, estimate_tokens, render_epistemic_context
from ...services.model_errors import (
    bounded_excerpt,
    planning_exception_detail,
    recoverable_model_error,
)
from ...services.structured_output import require_schema
from ...temporal_context import chapter_context
from ...domain.planning_validation import (
    validate_master_core,
    validate_master_result,
    validate_route_batch,
    validate_volume_blueprints,
)
from ..schemas import ApplyVolumeRequest, ChapterActionRequest, PlanningRequest


@dataclass(frozen=True)
class PlanningRoutes:
    router: APIRouter
    chapter_plan: Callable[..., Any]


def create_planning_routes(
    *,
    find_chapter_callback: Callable[..., Any],
    indexed_retrieval_callback: Callable[..., Any],
    structured_completion_callback: Callable[..., Any],
) -> PlanningRoutes:
    """Create planning routes with explicit retrieval and model dependencies."""
    router = APIRouter(tags=["planning"])
    find_chapter = find_chapter_callback
    _indexed_retrieval_hits = indexed_retrieval_callback
    structured_completion = structured_completion_callback

    @router.post("/api/chapter/plan")
    async def chapter_plan(body: ChapterActionRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        index, chapter = find_chapter(project, body.chapter_id)
        project = chapter_context(project, index)
        chapter = project["chapters"][index]
        query = "\n".join(
            [
                body.instruction,
                project.get("current_focus", ""),
                chapter.get("scene_goal", ""),
                chapter.get("content", "")[-3000:],
            ]
        )
        memories = retrieve_memories(
            project,
            query,
            index,
            int(project.get("settings", {}).get("memory_items", 12)),
            indexed_hits=_indexed_retrieval_hits(
                project,
                query,
                index,
                int(project.get("settings", {}).get("memory_items", 12)),
            ),
        )
        thread_agenda = render_thread_agenda(
            select_thread_agenda(project, query, index, limit=8)
        )
        previous = project.get("chapters", [])[max(0, index - 4) : index]
        context = f"""【长期作者意图】
    {bounded_excerpt(project.get('author_intent', ''), 2500)}
    【近期焦点】
    {bounded_excerpt(project.get('current_focus', ''), 2500)}
    【不可违背规则】
    {bounded_excerpt(project.get('book_rules', ''), 5000)}
    【人物权威状态】
    {bounded_excerpt(json.dumps(project.get('characters', []), ensure_ascii=False), 8000)}
    【人物与读者知情边界】
    {bounded_excerpt(render_epistemic_context(project, index, query), 7000)}
    【分层规划】
    {bounded_excerpt(render_planning_context(project, index), 8000)}
    【最近章节】
    {chr(10).join(f"- {item.get('title','')}：{item.get('summary','')}" for item in previous)}
    【相关记忆】
    {bounded_excerpt(render_memories(memories), 5000)}
    【伏笔与暗线治理议程】
    {bounded_excerpt(thread_agenda, 6000) or '当前没有必须处理的开放线索。'}
    【动态关系状态】
    {bounded_excerpt(json.dumps(project.get('memory', {}).get('relationships', []), ensure_ascii=False), 4000)}
    【待确认连续性备注】
    {json.dumps([
        item for item in project.get('memory', {}).get('continuity_notes', [])
        if isinstance(item, dict) and not item.get('resolved', False)
    ], ensure_ascii=False)}
    【当前章节】
    标题：{chapter.get('title','')}
    场景目标：{chapter.get('scene_goal','')}
    已有正文结尾：{chapter.get('content','')[-3000:]}
    【路线层级说明】
    当前章路线卡已经人工核对。goal、conflict、must_keep、must_avoid、turning_point、ending_hook
    只能原样服从，不得缩小、改写或用你生成的 exit_state 覆盖；你只补充视角、开篇动作和场景表现。
    【作者本次要求】
    {body.instruction or '依据近期焦点规划下一步，不抢写后续剧情。'}"""
        try:
            result, warnings = await structured_completion(
                project.get("settings", {}),
                [
                    {"role": "system", "content": CHAPTER_PLAN_PROMPT},
                    {"role": "user", "content": context},
                ],
                temperature=0.25,
                max_tokens=900,
                timeout_seconds=180,
                validate=require_schema(
                    "goal",
                    "conflict",
                    "must_keep",
                    "must_avoid",
                    "turning_point",
                    "ending_hook",
                    list_fields=("must_keep", "must_avoid"),
                ),
                workload="planning",
            )
            route = chapter.get("route", {}) if isinstance(chapter.get("route"), dict) else {}

            def route_value(key: str, fallback: Any) -> Any:
                value = route.get(key)
                return value if value not in (None, "", []) else fallback

            if route:
                scene_beats = [
                    f"进入：{route_value('goal', chapter.get('scene_goal', ''))}",
                    f"阻力：{route_value('conflict', result.get('conflict', ''))}",
                    f"转折：{route_value('turning_point', result.get('turning_point', ''))}",
                    f"退出：{route_value('ending_hook', result.get('ending_hook', ''))}",
                ]
                thread_actions: list[str] = []
            else:
                scene_beats = [
                    "→".join(str(part) for part in item if str(part).strip())
                    if isinstance(item, list)
                    else str(item)
                    for item in result.get("scene_beats", [])
                    if str(item).strip()
                ][:8] if isinstance(result.get("scene_beats"), list) else []
                thread_actions = [
                    "→".join(str(part) for part in item if str(part).strip())
                    if isinstance(item, list)
                    else str(item)
                    for item in result.get("thread_actions", [])
                    if str(item).strip()
                ][:5] if isinstance(result.get("thread_actions"), list) else []
            return {
                "goal": str(route_value("goal", result.get("goal", ""))),
                "conflict": str(route_value("conflict", result.get("conflict", ""))),
                "must_keep": [str(item) for item in route_value("must_keep", result.get("must_keep", []))][:8],
                "must_avoid": [str(item) for item in route_value("must_avoid", result.get("must_avoid", []))][:8],
                "turning_point": str(route_value("turning_point", result.get("turning_point", ""))),
                "ending_hook": str(route_value("ending_hook", result.get("ending_hook", ""))),
                "chapter_type": str(result.get("chapter_type", "")),
                "pov_character": str(result.get("pov_character", "")),
                "time_location": str(result.get("time_location", "")),
                "opening_beat": str(result.get("opening_beat", "")),
                "scene_beats": scene_beats,
                "emotional_turn": str(result.get("emotional_turn", "")),
                "thread_actions": thread_actions,
                "exit_state": str(route_value("ending_hook", result.get("exit_state", ""))),
                "ending_type": str(result.get("ending_type", "")),
                "fallback": False,
                "warnings": warnings,
            }
        except Exception as exc:
            if recoverable_model_error(exc):
                return fallback_chapter_plan(
                    project, index, chapter, planning_exception_detail(exc)
                )
            raise HTTPException(
                502, f"章节规划失败：{planning_exception_detail(exc)}"
            ) from exc


    @router.post("/api/planning/master")
    async def planning_master(body: PlanningRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        narrative = project.get("narrative", {})
        target = int(narrative.get("target_chapters", 30))
        requested_volumes = (
            1
            if target <= 6
            else min(12, max(2, math.ceil(target / 12)))
        )
        outline_target = (
            700
            if target <= 12
            else 1200
            if target <= 30
            else 1800
            if target <= 60
            else 2600
            if target <= 120
            else 3200
        )
        minimum_outline = max(500, int(outline_target * 0.7))
        characters = [
            {
                key: str(item.get(key, ""))[:240]
                for key in ("name", "role", "description", "goal")
            }
            for item in project.get("characters", [])[:12]
        ]
        world = [
            {
                "title": str(item.get("title", ""))[:80],
                "content": str(item.get("content", ""))[:300],
            }
            for item in project.get("world_entries", [])[:12]
        ]
        outline = str(project.get("outline", ""))
        if len(outline) > 4800:
            outline = outline[:3200] + "\n[…保留总纲结尾…]\n" + outline[-1600:]
        context = f"""【作品信息】
    书名：{project.get('title', '')}
    题材：{project.get('genre', '')}
    形态：{project.get('story_mode', 'long')}
    计划总章数：{target}
    必须生成分卷数：{requested_volumes}
    AI详细全书大纲目标：{outline_target}-{int(outline_target * 1.35)}字
    核心构想：{project.get('premise', '')}
    现有总纲：{outline}
    【作者控制】
    长期意图：{project.get('author_intent', '')}
    不可违背规则：{project.get('book_rules', '')}
    核心问题：{narrative.get('central_question', '')}
    结局方向：{narrative.get('ending_direction', '')}
    总体气质：{narrative.get('tone', '')}
    【人物】
    {json.dumps(characters, ensure_ascii=False)}
    【世界设定】
    {json.dumps(world, ensure_ascii=False)}
    【已发生事实】
    {project.get('memory', {}).get('story_so_far', '')}
    【作者对本次规划的补充】
    {body.instruction or '在不改变已有构想的前提下，补全可持续的全书结构。'}"""
        try:
            core_messages = [
                {"role": "system", "content": MASTER_CORE_PROMPT},
                {"role": "user", "content": context},
            ]
            safe_context = min(
                23000,
                int(project.get("settings", {}).get("context_budget", 24000)),
            )
            core_prompt_estimate = estimate_tokens(
                "\n".join(message["content"] for message in core_messages)
            )
            core_available = max(2200, safe_context - core_prompt_estimate - 900)
            core_desired = min(5200, 1100 + int(outline_target * 1.25))
            core, core_warnings = await structured_completion(
                project.get("settings", {}),
                core_messages,
                temperature=0.38,
                max_tokens=min(core_desired, core_available),
                timeout_seconds=420,
                validate=lambda result: validate_master_core(
                    result, minimum_outline
                ),
                token_ceiling=core_available,
                workload="planning",
            )

            core_for_volumes = {
                key: core.get(key)
                for key in (
                    "theme",
                    "reader_promise",
                    "central_conflict",
                    "story_engine",
                    "ending_state",
                    "full_outline",
                    "main_plot",
                    "theme_progression",
                    "pacing_plan",
                    "stakes_ladder",
                    "major_character_arcs",
                    "subplots",
                    "historical_nodes",
                )
            }
            volume_context = f"""【作品】
    书名：{project.get('title', '')}
    题材：{project.get('genre', '')}
    计划总章数：{target}
    必须生成分卷数：{requested_volumes}
    【全书故事圣经】
    {json.dumps(core_for_volumes, ensure_ascii=False)}
    【作者硬规则】
    {project.get('book_rules', '')}
    【本次补充】
    {body.instruction or '按照总纲建立连续、互不重复的分卷因果链。'}"""
            volume_messages = [
                {"role": "system", "content": MASTER_VOLUMES_PROMPT},
                {"role": "user", "content": volume_context},
            ]
            volume_prompt_estimate = estimate_tokens(
                "\n".join(message["content"] for message in volume_messages)
            )
            volume_available = max(
                2400, safe_context - volume_prompt_estimate - 900
            )
            volume_desired = min(6200, 900 + requested_volumes * 620)
            volume_result, volume_warnings = await structured_completion(
                project.get("settings", {}),
                volume_messages,
                temperature=0.34,
                max_tokens=min(volume_desired, volume_available),
                timeout_seconds=420,
                validate=lambda result: validate_volume_blueprints(
                    result, requested_volumes, target
                ),
                token_ceiling=volume_available,
                workload="planning",
            )
            result = {**core, "volumes": volume_result["volumes"]}
            validate_master_result(
                result, requested_volumes, minimum_outline, target
            )
            normalized = normalize_master_plan(result, target)
            normalized["fallback"] = False
            normalized["warnings"] = core_warnings + volume_warnings
            return normalized
        except Exception as exc:
            if recoverable_model_error(exc):
                return fallback_master_plan(
                    project, target, planning_exception_detail(exc)
                )
            raise HTTPException(
                502, f"全书规划失败：{planning_exception_detail(exc)}"
            ) from exc


    @router.post("/api/planning/volume")
    async def planning_volume(body: PlanningRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        try:
            _, volume = find_volume(project, body.volume_id)
        except KeyError:
            raise HTTPException(404, "分卷不存在") from None
        start, end = int(volume["chapter_start"]), int(volume["chapter_end"])
        written = [
            {
                "number": index + 1,
                "title": chapter.get("title", ""),
                "summary": chapter.get("summary", ""),
                "has_content": bool(chapter.get("content", "").strip()),
            }
            for index, chapter in enumerate(project.get("chapters", []))
            if start <= index + 1 <= end
            and (chapter.get("summary") or chapter.get("content"))
        ]
        master = project.get("planning", {}).get("master", {})
        base_context = f"""【全书详细总纲】
    {str(master.get('full_outline') or project.get('outline', ''))[:7000]}
    【全书主线、人物弧与主题递进】
    主线：{master.get('main_plot', '')}
    主题递进：{master.get('theme_progression', '')}
    人物弧：{json.dumps(master.get('major_character_arcs', []), ensure_ascii=False)}
    支线：{json.dumps(master.get('subplots', []), ensure_ascii=False)}
    【本卷蓝图】
    卷名：{volume.get('title', '')}（第{start}-{end}章）
    详细梗概：{volume.get('synopsis', '')}
    阶段目标：{volume.get('goal', '')}
    主导冲突：{volume.get('conflict', '')}
    关键转折：{json.dumps(volume.get('turning_points', []), ensure_ascii=False)}
    人物弧：{json.dumps(volume.get('character_arcs', []), ensure_ascii=False)}
    支线：{json.dumps(volume.get('subplots', []), ensure_ascii=False)}
    卷末状态：{volume.get('ending_state', '')}
    承接下一卷：{volume.get('bridge_to_next', '')}
    本卷必须保持：{json.dumps(volume.get('must_keep', []), ensure_ascii=False)}
    本卷必须避免：{json.dumps(volume.get('must_avoid', []), ensure_ascii=False)}
    【已有正文状态】
    {json.dumps(written, ensure_ascii=False)}
    【全书滚动进展与未结线索】
    {project.get('memory', {}).get('story_so_far', '')}
    {bounded_excerpt(json.dumps(project.get('memory', {}).get('plot_threads', []), ensure_ascii=False), 5000)}
    【作者补充】
    {body.instruction or '让每章承担不同的具体事件功能，连续造成可验证的新状态。'}"""
        routes: list[dict[str, Any]] = []
        retry_warnings: list[str] = []
        # Three chapters per call is a better reliability/latency balance for
        # 8–12B local models: each event card stays detailed, while the final
        # batch is small enough to land the promised volume ending only once.
        batch_size = 3
        total_batches = math.ceil((end - start + 1) / batch_size)
        for batch_start in range(start, end + 1, batch_size):
            batch_end = min(end, batch_start + batch_size - 1)
            batch_number = (batch_start - start) // batch_size + 1
            is_final_batch = batch_number == total_batches
            phase = (
                "起势：建立本卷独有问题、行动入口和第一次受阻"
                if batch_number == 1 and not is_final_batch
                else "升级：让既有行动产生反制、代价和方向变化"
                if not is_final_batch
                else "收束：完成本卷最终选择与代价，并触发下一卷"
            )
            turning_points = list(volume.get("turning_points", []))
            assigned_turns = [
                item
                for turn_index, item in enumerate(turning_points)
                if min(
                    total_batches - 1,
                    turn_index * total_batches // max(1, len(turning_points)),
                )
                == batch_number - 1
            ]
            expected_numbers = list(range(batch_start, batch_end + 1))
            if total_batches == 1:
                slot_roles = [
                    "建立具体局面与行动入口",
                    "让阻力升级并迫使人物改变做法",
                    "完成关键选择、支付代价并形成卷末状态",
                ]
            elif batch_number == 1:
                slot_roles = [
                    "建立本卷独有问题与主角当下处境",
                    "进行第一次具体调查或尝试并取得新证据",
                    "遭遇第一次反制，作出不可轻易撤回的选择",
                ]
            elif is_final_batch:
                slot_roles = [
                    "把此前证据、关系和代价汇入最终两难",
                    "执行最终选择并完成核心对抗",
                    "只在最后一章兑现卷末状态，同时制造下一卷的具体问题",
                ]
            elif batch_number == total_batches - 1:
                slot_roles = [
                    "让支线或人物关系与主线正面碰撞",
                    "制造一次改变力量对比的重大挫败或有限胜利",
                    "用新的代价逼出终局前无法回避的两难",
                ]
            else:
                slot_roles = [
                    "从不同渠道推进调查、试点或联盟，不重复上一批的方法",
                    "让有明确利益的对手采取具体反制行动",
                    "让计划付出可见代价，并据此修正下一阶段方向",
                ]
            slot_contract = "\n".join(
                f"- 第 {number} 章：{slot_roles[index]}"
                for index, number in enumerate(expected_numbers)
            )
            previous = routes[-3:]
            used_routes = [
                {
                    "number": item.get("number"),
                    "title": item.get("title"),
                    "goal": str(item.get("goal", ""))[:100],
                }
                for item in routes
            ]
            batch_context = f"""{base_context}
    【本批次任务】
    只生成第 {batch_start}-{batch_end} 章，共 {len(expected_numbers)} 章。
    紧邻本批次、需要直接承接的路线：
    {json.dumps(previous, ensure_ascii=False)}
    本卷此前全部已用章名与章末变化（新结果不得重复或近似复述）：
    {json.dumps(used_routes, ensure_ascii=False)}
    【本批次在整卷中的职责】
    第 {batch_number}/{total_batches} 批，阶段：{phase}
    本批应承载的转折：{json.dumps(assigned_turns, ensure_ascii=False)}
    本批每章不可互换的剧情岗位：
    {slot_contract}
    {"这是最终批次，必须到最后一章才兑现卷目标和卷末状态。" if is_final_batch else f"这不是最终批次。严禁提前兑现卷目标“{volume.get('goal', '')}”、卷末状态“{volume.get('ending_state', '')}”或下一卷桥梁；本批结尾只能形成通向下一阶段的新压力、证据、资格或代价。"}
    第{batch_start}章必须直接承接上述最后状态；第{batch_end}章要为下一批或卷末留下具体推动力。
    每章必须写不同的人物行动、具体阻力、关键选择和章末结果。"""
            batch_result: dict[str, Any] | None = None
            last_batch_error: Exception | None = None
            for batch_attempt in range(2):
                repair_instruction = ""
                if batch_attempt and last_batch_error is not None:
                    repair_instruction = f"""
    【上一轮仍未通过质量门】
    问题：{planning_exception_detail(last_batch_error)}
    这是一次全新重写，不是改几个词。四章必须使用四个互不相似、且未在本卷出现过的具体事件标题；
    每章的行动、阻力、选择和章末变化也必须彼此不同。不要复用上一轮的标题或句式。"""
                try:
                    result, batch_warnings = await structured_completion(
                        project.get("settings", {}),
                        [
                            {"role": "system", "content": VOLUME_PLAN_PROMPT},
                            {
                                "role": "user",
                                "content": batch_context + repair_instruction,
                            },
                        ],
                        temperature=0.42 if batch_attempt == 0 else 0.52,
                        max_tokens=2100,
                        timeout_seconds=240,
                        validate=lambda value,
                        numbers=expected_numbers,
                        prior=list(routes),
                        forbidden=(
                            []
                            if is_final_batch
                            else [
                                str(volume.get("goal", "")),
                                str(volume.get("ending_state", "")),
                                str(volume.get("bridge_to_next", "")),
                            ]
                        ): (
                            validate_route_batch(value, numbers, prior, forbidden)
                        ),
                        workload="planning",
                    )
                    retry_warnings.extend(batch_warnings)
                    if batch_attempt:
                        retry_warnings.append(
                            f"第 {batch_start}-{batch_end} 章首次重写仍未通过质量门，"
                            "系统已按错误原因再次生成并恢复。"
                        )
                    batch_result = result
                    break
                except Exception as exc:
                    last_batch_error = exc
                    if not recoverable_model_error(exc):
                        raise HTTPException(
                            502, f"分卷拆解失败：{planning_exception_detail(exc)}"
                        ) from exc
            if batch_result is None:
                detail = planning_exception_detail(
                    last_batch_error or ValueError("未知质量检查错误")
                )
                return {
                    "volume_id": volume["id"],
                    "chapters": routes,
                    "warnings": [
                        f"第 {batch_start}-{batch_end} 章经过自动重写后仍未通过完整性或重复度检查："
                        f"{detail}。旧路线未被替换，请重试本卷。"
                    ],
                    "complete": False,
                    "fallback": True,
                }
            for offset, item in enumerate(batch_result["chapters"]):
                routes.append(normalize_route(item, batch_start + offset))
        return {
            "volume_id": volume["id"],
            "chapters": routes,
            "warnings": retry_warnings,
            "complete": len(routes) == end - start + 1,
            "fallback": False,
        }


    @router.post("/api/planning/apply-volume")
    async def planning_apply_volume(body: ApplyVolumeRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        try:
            return ensure_project_defaults(apply_volume_routes(project, body.volume_id))
        except KeyError:
            raise HTTPException(404, "分卷不存在") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    return PlanningRoutes(router=router, chapter_plan=chapter_plan)

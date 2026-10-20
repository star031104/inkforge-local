"""Director prose generation.

Compact per-scene prompts, dialogue rehearsal, anti-slop, best-of-n drafts and
evidenced local patches.
"""
from __future__ import annotations

import asyncio
import functools
import json
import math
import re
from copy import deepcopy
from typing import Any

from app.core.clock import utc_now
from app.debate_tools import render_debate_for_prompt
from app.domain.prose import (
    _clean_repair_continuation,
    _dedupe_adjacent_sentence_blocks,
    _dedupe_exact_paragraphs,
    _dedupe_repeated_sentences,
    _effective_prose_settings,
    _ensure_final_route_closure,
    _number_phrases,
    _paragraphize_prose,
    _prose_char_count,
    _prose_looks_truncated,
    _remove_orphan_chinese_quotes,
    _sanitize_generated_prose,
    _scene_constraint_violations,
    _strip_nonfinal_scene_violations,
    _strip_unsupported_recollections,
    _trim_incomplete_prose_tail,
)
from app.history_tools import render_story_date
from app.memory import render_memories, retrieve_memories
from app.prompts import build_prompt, estimate_tokens, render_epistemic_context
from app.llm.providers import settings_for_workload
from app.services.antislop import apply_antislop, prose_stream_options
from app.services.candidate_selection import candidate_count_for, select_best_candidate
from app.services.craft_settings import craft_setting
from app.services.dialogue_rehearsal import rehearse_dialogue, should_rehearse
from app.services.editorial_policy import _targeted_refinement_findings
from app.services.hook_ledger import render_hook_debts_for_prompt
from app.services.memory_settlement import _chapter_number
from app.llm.errors import bounded_excerpt, planning_exception_detail
from app.services.prose_recovery import receive_prose
from app.services.director.context import DirectorContext


def install(ctx: DirectorContext) -> None:
    """Define the prose closures on ``ctx``."""
    store = ctx.store
    _attach_indexed_retrieval = ctx._attach_indexed_retrieval
    _persist_context_snapshot = ctx._persist_context_snapshot
    chat_stream = ctx.chat_stream
    chat_once = ctx.chat_once
    run_structured_completion = ctx.run_structured_completion
    _director_candidate_gate_failures = ctx._director_candidate_gate_failures
    _director_event = ctx._director_event
    _record_task_event = ctx._record_task_event
    _director_experience_hints = ctx._director_experience_hints

    async def _director_generate_prose_raw(
        task: dict[str, Any], project: dict[str, Any], chapter: dict[str, Any],
        *, mode: str = "instruction", instruction: str = "", selection: str = "",
        rehearsal: str = "",
    ) -> str:
        config = task["config"]
        # Generation may need a much larger output window than the saved everyday
        # model setting, especially when a 4-5k character chapter is rewritten in
        # full. Work on a copy so that the temporary token ceiling is not persisted.
        project = deepcopy(project)
        target_chars = int(config["target_words"])
        project["settings"] = _effective_prose_settings(
            project.get("settings", {}),
            target_chars,
            "revision" if mode == "rewrite" else "prose",
        )
        project["settings"]["target_words"] = target_chars
        safe_directive_markers = (
            "长度", "重复", "句式", "时代", "元话语", "疑似新增往事",
            "待确认新设定", "视角", "知识来源", "人物状态", "时间线",
            "目标偏离", "段落节奏",
        )
        adaptive = [
            str(item)
            for item in task.get("quality_directives", [])
            if str(item).strip()
            and any(marker in str(item) for marker in safe_directive_markers)
        ][-6:]
        if adaptive:
            instruction += (
                "\n【自动导演根据前章质量债生成的临时硬约束】\n- "
                + "\n- ".join(adaptive)
            )

        # Per-chapter extras (never in the stable system prefix): outstanding
        # hook debts and lessons from earlier audits of this model.
        chapter_number = next(
            (number for number, item in enumerate(project.get("chapters", []), start=1)
             if item.get("id") == chapter.get("id")),
            0,
        )
        hook_debt_text = (
            render_hook_debts_for_prompt(project, chapter_number)
            if chapter_number and craft_setting(project, "hook_ledger", config)
            else ""
        )
        experience_lines = _director_experience_hints(project, config)

        async def receive(settings: dict[str, Any], messages: list[dict[str, str]]) -> str:
            def is_running() -> bool:
                current = store.get_director_task(task["id"])
                return bool(current and current.get("status") == "running")

            def save_checkpoint(payload: dict[str, Any]) -> None:
                latest = store.get_director_task(task["id"])
                if latest:
                    previous = latest.get("prose_stream_checkpoint", {})
                    receipts = previous.get("receipts", []) if isinstance(previous, dict) and previous.get("chapter_id") == chapter["id"] else []
                    if not isinstance(receipts, list):
                        receipts = []
                    receipts = [item for item in receipts if isinstance(item, dict) and item.get("input_hash") != payload["input_hash"]]
                    # Retain completed scene receipts so resuming a later scene
                    # does not regenerate earlier scenes and change its input.
                    receipts.append(payload)
                    latest["prose_stream_checkpoint"] = {"chapter_id": chapter["id"], **payload, "receipts": receipts[-32:]}
                    saved = store.save_director_task(task["id"], latest)
                    task.clear()
                    task.update(saved)

            def notify(message: str, level: str) -> None:
                latest = store.get_director_task(task["id"])
                if latest and latest.get("status") == "running":
                    _director_event(latest, message, level)
                    saved = store.save_director_task(task["id"], latest)
                    task.clear()
                    task.update(saved)

            checkpoint = task.get("prose_stream_checkpoint", {})
            if not isinstance(checkpoint, dict) or checkpoint.get("chapter_id") != chapter["id"]:
                checkpoint = {}
            prose_settings = settings_for_workload(settings, "prose")
            # llama.cpp: bias distinctive cliché tokens down while generating.
            stream_options = await prose_stream_options(project, prose_settings, config)
            stream = (
                functools.partial(chat_stream, options=stream_options)
                if stream_options is not None
                else chat_stream
            )
            return await receive_prose(
                prose_settings, messages, stream=stream,
                checkpoint=checkpoint, save_checkpoint=save_checkpoint,
                is_running=is_running, notify=notify,
            )

        if project.get("settings", {}).get("director_scene_generation", False):
            plan = chapter.get("plan", {}) if isinstance(chapter.get("plan"), dict) else {}
            route = chapter.get("route", {}) if isinstance(chapter.get("route"), dict) else {}
            raw_contracts = route.get("scene_contracts", [])
            if isinstance(raw_contracts, list) and 3 <= len(raw_contracts) <= 8 and all(
                isinstance(item, dict) and str(item.get("boundary", "")).strip()
                for item in raw_contracts
            ):
                phases = [
                    (
                        str(item.get("job", "完成当前场景合同")).strip(),
                        str(item.get("boundary", "")).strip(),
                        [str(beat).strip() for beat in item.get("beats", []) if str(beat).strip()][:8],
                    )
                    for item in raw_contracts
                ]
            else:
                phases = [
                    (
                        "建立本章进入状态，只让人物开始当前任务",
                        str(route.get("goal") or plan.get("goal", "")).strip(),
                        [],
                    ),
                    (
                        "让主要阻力在现场具体发生，但不解决",
                        str(route.get("conflict") or plan.get("conflict", "")).strip(),
                        [],
                    ),
                    (
                        "让核验、对抗或选择推进到本章转折",
                        str(route.get("turning_point") or plan.get("turning_point", "")).strip(),
                        [],
                    ),
                    (
                        "只完成本章规定的退出状态并收束",
                        str(route.get("ending_hook") or plan.get("ending_hook", "")).strip(),
                        [],
                    ),
                ]
            ending_text = str(route.get("ending_hook") or plan.get("ending_hook", ""))
            delayed_actions = [
                action
                for action in (
                    "开仓", "署名", "下令", "交出", "烧毁", "放弃", "离开",
                    "抵达", "处死", "杀死", "获得官印", "交还官印",
                )
                if action in ending_text
            ]
            partial = ""
            scene_target = max(
                320,
                min(
                    900,
                    int(project.get("settings", {}).get("director_scene_target_chars", 850) or 850),
                ),
            )
            for scene_index, (scene_job, scene_boundary, scene_beats) in enumerate(phases, start=1):
                scene_project = deepcopy(project)
                scene_chapter = next(
                    item for item in scene_project.get("chapters", [])
                    if item.get("id") == chapter.get("id")
                )
                scene_chapter["content"] = partial
                scene_instruction = (
                    f"整章分场写作：当前是第 {scene_index}/{len(phases)} 段。{scene_job}。\n"
                    f"本段唯一交付边界：【{scene_boundary}】。\n"
                    f"本段写约 {scene_target} 个中文字符；只输出从现有末句之后发生的新正文，"
                    "不得复述前文、不得重新介绍人物任务、不得新增往事或回忆。"
                )
                if scene_index < len(phases):
                    scene_instruction += (
                        "当前不得越过本段边界，也不得猜测、提及或执行尚未提供的后段内容。"
                        "本段结束在能自然接续下一段的动作或压力上。"
                    )
                    if delayed_actions:
                        scene_instruction += (
                            "存在尚未提供的章末延迟动作；当前段不得猜测、命名或开始其物理步骤。"
                        )
                else:
                    scene_instruction += (
                        "这是末段：准确交付章末状态后立即停止，不新增陌生人、新消息、"
                        "新线索或下一章事件。"
                    )
                    if delayed_actions:
                        scene_instruction += (
                            "此前延迟的章末动作现在必须在场完成："
                            + "、".join(delayed_actions)
                            + "。"
                        )
                # Whole-chapter revision feedback can name a required ending event.
                # Broadcasting it into every isolated scene defeats the phase wall
                # and makes small models execute the ending in scene one. The route
                # remains authoritative; audit feedback is enforced by rebuilding
                # all four route phases and re-auditing the finished chapter.
                scene_request = {
                    "project": scene_project,
                    "chapter_id": chapter["id"],
                    "mode": "continue" if partial else "instruction",
                    "instruction": scene_instruction,
                    "selection": "",
                    "target_words": scene_target,
                }
                _attach_indexed_retrieval(scene_project, scene_request)
                scene_build = build_prompt(scene_project, scene_request)
                scene_authority = "\n".join([scene_boundary, *scene_beats])
                compact_query = "\n".join(
                    [scene_authority, partial[-1800:], scene_instruction]
                )
                scene_memories = retrieve_memories(
                    scene_project,
                    compact_query,
                    _chapter_number(scene_project, scene_chapter) - 1,
                    8,
                    indexed_hits=scene_request.get("_indexed_memory_hits", []),
                )
                active_names = {
                    str(item.get("name", "")).strip()
                    for item in scene_project.get("characters", [])
                    if str(item.get("name", "")).strip()
                    and str(item.get("name", "")).strip() in compact_query
                }
                main_name = next(
                    (
                        str(item.get("name", "")).strip()
                        for item in scene_project.get("characters", [])
                        if item.get("active", True)
                        and str(item.get("importance", "supporting")) == "main"
                        and str(item.get("name", "")).strip()
                    ),
                    "",
                )
                if main_name:
                    active_names.add(main_name)
                character_lines = []
                for item in scene_project.get("characters", []):
                    name = str(item.get("name", "")).strip()
                    if not name or name not in active_names or not item.get("active", True):
                        continue
                    character_lines.append(
                        f"【{name}】身份：{item.get('role', '')}；"
                        f"人格：{item.get('personality', '')}；"
                        f"不可写偏：{item.get('hard_limits', '')}；"
                        f"当前状态：{item.get('state', '')}；"
                        f"当前知情：{item.get('knowledge_baseline', item.get('knowledge', ''))}；"
                        f"语言：{item.get('voice', '')}"
                    )
                lore_lines = []
                folded_query = compact_query.casefold()
                for entry in scene_project.get("world_entries", []):
                    if not isinstance(entry, dict) or not entry.get("enabled", True):
                        continue
                    keys = entry.get("keys", [])
                    if isinstance(keys, str):
                        keys = re.split(r"[,，\n]", keys)
                    if entry.get("constant", False) or any(
                        str(key).strip().casefold() in folded_query
                        for key in keys if str(key).strip()
                    ):
                        lore_lines.append(
                            f"【{entry.get('title', '')}】{entry.get('content', '')}"
                        )
                    if len(lore_lines) >= 4:
                        break
                completed_boundaries = [
                    f"第{number}段已经完成" for number in range(1, scene_index)
                ]
                visible_route_lines = [f"当前边界：{scene_boundary}"]
                if scene_beats:
                    visible_route_lines.append(
                        "必须依次写出的动作：" + "；".join(scene_beats)
                    )
                # Chapter-level authority shared by every scene ("" for ordinary
                # chapters): the in-story date and the debate structure.  Their
                # explicit numbers are authorised, so echoing them is no violation.
                story_date_line = render_story_date(scene_chapter)
                if story_date_line:
                    visible_route_lines.append(story_date_line)
                debate_text = render_debate_for_prompt(scene_chapter)
                number_authority = "\n".join(
                    item for item in (scene_authority, story_date_line, debate_text) if item
                )
                compact_system = (
                    "你是严谨的中文历史小说作者，只续写当前分场正文。禁止解释任务、输出标题或规划标签。"
                    "只写眼前可观察的动作、物件、对话和判断；禁止用‘想起、记得、曾经、上次、"
                    "昨日见过’凭空制造往事。禁止现代技术/管理术语、全知剧透和无来源新人物。"
                    "不得复述已经完成的动作、证据、问答或结论。路线中的所有数字必须逐字保持，"
                    "不得缩写、换算、改成近似数或另造数字。禁用目光如炬、指节发白、空气凝固、"
                    "命运齿轮、无人知道、更大的风暴。第三人称限知必须服从人物知情边界。"
                    "不得新增能改变判断的证物、证词、书信、印鉴、血迹、布料、暗号或目击者；"
                    "当前分场的有效证据只有权威路线明确写出的内容。"
                )
                compact_user = f"""【当前分场可见的权威路线】
    {chr(10).join(visible_route_lines)}
    整章级 must_keep 与 must_avoid 已由导演校验，本段不重复展示，避免诱发复述或提前写出后段事件。
    本段允许使用的明确数量只有：{'、'.join(sorted(_number_phrases(number_authority))) or '无'}；不得另造日期、时刻、车数、物数，也不得提前引用其他分场的数量。{(chr(10) + chr(10) + debate_text) if debate_text else ''}
    
    【当前人物】
    {chr(10).join(character_lines) or '只使用路线中已经点名的人物。'}
    本段可出现的姓名只有：{'、'.join(sorted(active_names)) or '无'}；其他现场人物只能用已有职称，不得创造姓名。
    
    【人物知情边界】
    {render_epistemic_context(scene_project, _chapter_number(scene_project, scene_chapter) - 1, compact_query, active_names)}
    
    【相关已发生记忆】
    {bounded_excerpt(render_memories(scene_memories), 2400) or '尚无已接受章节记忆。'}
    
    【当前触发世界规则】
    {chr(10).join(lore_lines) or '无额外条目；不得自行补造制度与前史。'}
    
    {('【对白演出素材（仅作参考，只取与本段相关部分）】' + chr(10) + rehearsal[:1500] + chr(10)) if rehearsal else ''}
    {('【待承接的章末钩子】' + chr(10) + hook_debt_text + chr(10)) if hook_debt_text else ''}
    {('【审校经验提示（主动规避）】' + chr(10) + chr(10).join('- ' + item for item in experience_lines) + chr(10)) if experience_lines else ''}
    【前文最后接续锚点】
    {partial[-360:] if partial else '尚未起笔。'}
    
    【已完成、严禁换词重写】
    {'；'.join(completed_boundaries) or '无。'}
    
    【当前分场指令】
    {scene_instruction}"""
                scene_build.messages = [
                    {"role": "system", "content": compact_system},
                    {"role": "user", "content": compact_user},
                ]
                scene_build.estimated_tokens = estimate_tokens(
                    compact_system + "\n" + compact_user
                )
                scene_build.sections = [
                    {
                        "name": "导演分场紧凑上下文",
                        "content": compact_user,
                        "priority": 100,
                        "tokens": estimate_tokens(compact_user),
                        "tokens_before": estimate_tokens(compact_user),
                        "tokens_after": estimate_tokens(compact_user),
                        "status": "included",
                        "selected": True,
                        "reason": "",
                        "role": "user",
                        "required": True,
                    }
                ]
                _persist_context_snapshot(
                    scene_project,
                    scene_request,
                    scene_build,
                    reason=f"director_generation_scene_{scene_index}",
                )
                segment = await asyncio.wait_for(
                    receive(scene_project["settings"], scene_build.messages),
                    timeout=900,
                )
                violations = _scene_constraint_violations(
                    segment, route, scene_index, len(phases), delayed_actions, number_authority
                )
                if (
                    _prose_char_count(segment) < 280
                    or _prose_looks_truncated(segment)
                    or violations
                ):
                    retry_messages = [dict(item) for item in scene_build.messages]
                    retry_messages.append(
                        {
                            "role": "user",
                            "content": (
                                "上一回应未通过当前分场的硬校验，请完全重写本分场。"
                                f"校验原因：{'；'.join(violations) or '篇幅不足或句子未结束'}。"
                                "至少写到约320个中文字符；仍只执行当前分场边界，不复述前文、"
                                "不抢写后续、不解释。不得新增任何人名、数字、时刻、制度或道具。"
                            ),
                        }
                    )
                    segment = await asyncio.wait_for(
                        receive(scene_project["settings"], retry_messages),
                        timeout=900,
                    )
                segment = _strip_nonfinal_scene_violations(
                    segment, route, scene_index, len(phases), delayed_actions, number_authority
                )
                if _prose_char_count(segment) < 320:
                    top_up_messages = [dict(item) for item in scene_build.messages]
                    top_up_messages.extend(
                        [
                            {"role": "assistant", "content": segment},
                            {
                                "role": "user",
                                "content": (
                                    "只从上句之后续写当前分场约150至220个中文字符，"
                                    "补足现场动作、物件反应或简短问答。不得复述，"
                                    "不得新增姓名、数字、时刻、制度、道具或后段事件；"
                                    "写完一个完整句子立即停止。"
                                ),
                            },
                        ]
                    )
                    top_up = await asyncio.wait_for(
                        receive(scene_project["settings"], top_up_messages),
                        timeout=900,
                    )
                    top_up = _clean_repair_continuation(segment, top_up)
                    top_up = _strip_nonfinal_scene_violations(
                        top_up, route, scene_index, len(phases), delayed_actions, number_authority
                    )
                    segment = (segment.rstrip() + "\n\n" + top_up.lstrip()).strip()
                if scene_index == len(phases):
                    segment = _ensure_final_route_closure(
                        segment, route, delayed_actions
                    )
                violations = _scene_constraint_violations(
                    segment, route, scene_index, len(phases), delayed_actions, number_authority
                )
                if violations:
                    raise ValueError(
                        f"模型第 {scene_index} 段违反路线硬约束：{'；'.join(violations)}"
                    )
                segment = _clean_repair_continuation(partial, segment)
                segment_authority = "\n".join(
                    [
                        json.dumps(route, ensure_ascii=False),
                        json.dumps(plan, ensure_ascii=False),
                        "\n".join(character_lines),
                        render_memories(scene_memories),
                        str(scene_chapter.get("summary", "")),
                    ]
                )
                segment = _strip_unsupported_recollections(
                    _dedupe_adjacent_sentence_blocks(segment), segment_authority
                )
                # A scene is a semantic boundary, not an independent chapter.  Treat
                # this only as a non-empty-output guard: aggregate chapter length and
                # the later local/AI audit own development and pacing quality.  Safety
                # stripping may legitimately reduce a concise bridge scene heavily.
                minimum_scene_chars = 20
                if _prose_char_count(segment) < minimum_scene_chars:
                    raise ValueError(
                        f"模型重试并补写后第 {scene_index} 段正文仍不足 {minimum_scene_chars} 字"
                    )
                partial = (partial.rstrip() + "\n\n" + segment.lstrip()).strip()
            if len(partial) < 300:
                raise ValueError("模型返回的分场正文不足 300 字")
            return _dedupe_exact_paragraphs(
                _paragraphize_prose(
                    _remove_orphan_chinese_quotes(
                        _dedupe_repeated_sentences(
                            _dedupe_adjacent_sentence_blocks(partial)
                        )
                    )
                )
            )

        length_multiplier = max(
            1.0,
            min(
                1.6,
                float(project.get("settings", {}).get("director_length_prompt_multiplier", 1.0) or 1.0),
            ),
        )
        prompt_target = int(math.ceil(int(config["target_words"]) * length_multiplier))
        request = {
            "project": project, "chapter_id": chapter["id"], "mode": mode,
            "instruction": instruction, "selection": selection,
            "target_words": prompt_target,
            "_rehearsal_material": rehearsal,
            "_experience_hints": experience_lines,
        }
        if mode == "rewrite" and selection.strip():
            # Director revisions replace an entire chapter. Manual rewrite mode is
            # often used for a selected sentence/paragraph and intentionally keeps
            # the selection's scale; these are different operations.
            request["_full_chapter_rewrite"] = True
        _attach_indexed_retrieval(project, request)
        build = build_prompt(project, request)
        _persist_context_snapshot(project, request, build, reason="director_generation")
        text = await asyncio.wait_for(
            receive(project["settings"], build.messages), timeout=900
        )
        text, cleanup_notes = _sanitize_generated_prose(text)
        text, tail_repair_note = _trim_incomplete_prose_tail(text)
        if tail_repair_note:
            cleanup_notes.append(tail_repair_note)
        if cleanup_notes:
            latest = store.get_director_task(task["id"])
            if latest and latest.get("status") == "running":
                _director_event(
                    latest,
                    "；".join(cleanup_notes) + "，仅保留可审计的小说正文",
                    "warning",
                )
                saved = store.save_director_task(task["id"], latest)
                task.clear()
                task.update(saved)
        if _prose_char_count(text) < 100:
            raise ValueError("模型返回的正文不足 100 字")
        return text

    async def _chapter_rehearsal(
        task: dict[str, Any], project: dict[str, Any], chapter: dict[str, Any]
    ) -> str:
        """First-person dialogue rehearsal, cached per chapter in the task."""
        if not should_rehearse(project, director=True, config=task.get("config")):
            return ""
        cached = task.get("rehearsal_cache", {})
        if isinstance(cached, dict) and cached.get("chapter_id") == chapter.get("id"):
            return str(cached.get("material", ""))
        index = next(
            (number for number, item in enumerate(project.get("chapters", []))
             if item.get("id") == chapter.get("id")),
            -1,
        )
        if index < 0:
            return ""
        report = await rehearse_dialogue(
            project, index, project.get("settings", {}), run_structured_completion
        )
        latest = store.get_director_task(task["id"]) or task
        latest["rehearsal_cache"] = {
            "chapter_id": chapter.get("id", ""),
            "material": report.get("material", ""),
            "cast": report.get("cast", []),
            "dropped": report.get("dropped", [])[:12],
        }
        if report.get("cast"):
            _director_event(
                latest,
                f"对白演出：{'、'.join(report['cast'])} 已按各自知情范围排演"
                + (f"，拦截 {len(report['dropped'])} 句越界台词" if report.get("dropped") else ""),
                "info",
            )
        saved = store.save_director_task(task["id"], latest)
        task.clear()
        task.update(saved)
        return str(report.get("material", ""))

    async def _director_generate_prose(
        task: dict[str, Any], project: dict[str, Any], chapter: dict[str, Any],
        *, mode: str = "instruction", instruction: str = "", selection: str = "",
    ) -> str:
        """Generate prose, then remove AI clichés sentence-by-sentence."""
        rehearsal = ""
        if mode != "rewrite":
            try:
                rehearsal = await _chapter_rehearsal(task, project, chapter)
            except Exception as exc:  # noqa: BLE001 - rehearsal is optional
                _record_task_event(task, f"对白演出跳过：{planning_exception_detail(exc)}", "warning")
        text = await _director_generate_prose_raw(
            task, project, chapter, mode=mode, instruction=instruction,
            selection=selection, rehearsal=rehearsal,
        )
        try:
            revised, report = await apply_antislop(
                text, project, project.get("settings", {}), run_structured_completion,
                config=task.get("config"),
            )
        except Exception as exc:  # noqa: BLE001 - keep the original draft
            _record_task_event(task, f"套话局部改写跳过：{planning_exception_detail(exc)}", "warning")
            return text
        if report.get("rewritten"):
            _record_task_event(
                task,
                f"已局部改写 {len(report['rewritten'])} 句套话（"
                + "、".join(dict.fromkeys(hit["phrase"] for hit in report.get("hits", [])))[:60]
                + "），未重写整章",
                "success",
            )
        return revised

    async def _director_initial_draft(
        task: dict[str, Any], project: dict[str, Any], chapter: dict[str, Any],
        index: int, instruction: str,
    ) -> str:
        """One draft, or best-of-n with a pairwise judge for key chapters."""
        config = task.get("config", {})
        count = candidate_count_for(project, chapter, index, config)
        if count <= 1:
            return await _director_generate_prose(task, project, chapter, instruction=instruction)
        # Resume: reuse candidates (and a finished selection) checkpointed for
        # this chapter instead of paying for every draft again.
        saved = task.get("chapter_candidates")
        candidates: list[str] = []
        if isinstance(saved, dict) and saved.get("chapter_id") == chapter.get("id", ""):
            candidates = [str(item) for item in saved.get("drafts", []) or [] if str(item).strip()][:count]
            selected = saved.get("selected")
            if isinstance(selected, int) and 0 <= selected < len(candidates):
                _record_task_event(task, f"第 {index + 1} 章：沿用已比较完成的第 {selected + 1} 份候选稿")
                return candidates[selected]
            if candidates:
                _record_task_event(task, f"第 {index + 1} 章：沿用已生成的 {len(candidates)} 份候选稿")
        for number in range(len(candidates), count):
            _record_task_event(task, f"第 {index + 1} 章：正在生成候选稿 {number + 1}/{count}")
            # A distinct variant hint diversifies candidates and gives each one its
            # own stream-checkpoint identity (identical inputs would be replayed).
            variant = instruction if number == 0 else (
                instruction
                + f"\n（这是第 {number + 1} 份候选稿：采用与常规写法不同的开场切入点和动作链，"
                "仍完整执行本章路线与收束。）"
            )
            try:
                candidates.append(
                    await _director_generate_prose(task, project, chapter, instruction=variant)
                )
            except Exception as exc:  # noqa: BLE001 - keep earlier candidates
                if not candidates:
                    raise
                _record_task_event(task, f"候选稿 {number + 1} 生成失败：{planning_exception_detail(exc)}", "warning")
                break
            # Checkpoint every candidate; a resume never loses finished drafts.
            latest = store.get_director_task(task["id"]) or task
            latest["chapter_candidates"] = {
                "chapter_id": chapter.get("id", ""),
                "drafts": [item[:30000] for item in candidates],
                "saved_at": utc_now(),
            }
            saved = store.save_director_task(task["id"], latest)
            task.clear()
            task.update(saved)
        if len(candidates) == 1:
            return candidates[0]
        route = chapter.get("route", {}) if isinstance(chapter.get("route"), dict) else {}
        plan = chapter.get("plan", {}) if isinstance(chapter.get("plan"), dict) else {}
        brief = "\n".join(
            f"{label}：{route.get(key) or plan.get(key)}"
            for key, label in (("goal", "目标"), ("conflict", "阻力"), ("turning_point", "转折"), ("ending_hook", "收束"))
            if route.get(key) or plan.get(key)
        )
        selection = await select_best_candidate(
            candidates,
            project=project,
            target_chars=int(config.get("target_words", 1200)),
            plan_brief=brief,
            settings=project.get("settings", {}),
            completion=run_structured_completion,
        )
        latest = store.get_director_task(task["id"]) or task
        latest["chapter_candidates"] = {
            "chapter_id": chapter.get("id", ""),
            "drafts": [item[:30000] for item in candidates],
            "selected": selection["index"],
            "report": selection["report"],
            "saved_at": utc_now(),
        }
        reasons = [
            reason for match in selection["report"].get("matches", []) for reason in match.get("reasons", [])
        ]
        _director_event(
            latest,
            f"第 {index + 1} 章 {len(candidates)} 份候选稿比较完成，采用第 {selection['index'] + 1} 份"
            + (f"：{'；'.join(reasons[:3])}" if reasons else ""),
            "success",
        )
        saved = store.save_director_task(task["id"], latest)
        task.clear()
        task.update(saved)
        return selection["text"]

    async def _director_patch_refinement_excerpts(
        project: dict[str, Any], chapter: dict[str, Any], draft: str,
        audit: dict[str, Any], target_chars: int,
    ) -> str:
        """Repair one to three evidenced paragraphs without destabilizing the chapter."""
        findings = _targeted_refinement_findings(audit)
        if not findings:
            return draft
        patched = draft
        settings = _effective_prose_settings(
            project.get("settings", {}), min(1200, max(300, target_chars)), "revision"
        )
        for finding in findings:
            quote = finding["quote"]
            start = patched.find(quote)
            if start < 0:
                continue
            before = patched[max(0, start - 260):start]
            after = patched[start + len(quote):start + len(quote) + 260]
            messages = [
                {
                    "role": "system",
                    "content": (
                        "你是中文小说局部精修编辑。只重写给定问题段落，不改事件事实、人物、"
                        "时间顺序和叙事视角。输出一段可直接替换的小说正文；不要标题、说明、"
                        "引号包裹或修改计划。"
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        f"【章节】{chapter.get('title', '')}\n"
                        f"【问题】[{finding['category']}] {finding['message']}\n"
                        f"【修复要求】{finding['suggestion'] or '换用新的具体动作、观察与人物反应表达。'}\n"
                        f"【前文】{before}\n【必须替换的段落】{quote}\n【后文】{after}\n"
                        "只输出替换段落，保持与前后文自然衔接。"
                    ),
                },
            ]
            try:
                replacement = await chat_once(
                    settings,
                    messages,
                    temperature=0.35,
                    max_tokens=min(1200, max(320, len(quote) * 3)),
                    timeout_seconds=180,
                )
                replacement, _notes = _sanitize_generated_prose(replacement)
                replacement = replacement.strip().strip("`\n")
            except Exception:
                return draft
            replacement_chars = _prose_char_count(replacement)
            if (
                replacement_chars < max(18, int(_prose_char_count(quote) * 0.45))
                or replacement_chars > max(500, int(_prose_char_count(quote) * 2.2))
                or quote in replacement
                or _director_candidate_gate_failures(replacement, max(300, len(quote)))
            ):
                return draft
            patched = patched[:start] + replacement + patched[start + len(quote):]
        return patched

    ctx.register(
        _director_generate_prose_raw,
        _chapter_rehearsal,
        _director_generate_prose,
        _director_initial_draft,
        _director_patch_refinement_excerpts,
    )

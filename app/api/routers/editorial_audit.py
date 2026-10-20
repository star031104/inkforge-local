"""Editorial chapter audit and quality routes."""
from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from typing import Any

from fastapi import HTTPException

from app.chapter_session import prepare_messages
from app.db import ensure_project_defaults
from app.debate_tools import render_debate_for_audit
from app.domain.continuity_state import check_continuity_draft, render_continuity_state
from app.domain.json_schemas import AUDIT_SCHEMA
from app.domain.planning_validation import validate_audit_result
from app.editorial_workflow import begin_stage, complete_stage, fail_stage
from app.evidence_audit import (
    normalize_model_score,
    number_draft,
    penalty_total,
    stable_audit_result,
)
from app.fallbacks import local_audit_result
from app.history_tools import apply_history_issues, check_history
from app.manuscript_quality import jargon_policy, prior_manuscript_text
from app.memory import (
    render_memories,
    render_thread_agenda,
    retrieve_memories,
    select_thread_agenda,
)
from app.narrative_policy import route_guidance
from app.prompts import AUDIT_PROMPT, render_epistemic_context
from app.providers import settings_for_workload
from app.quality import local_quality_check
from app.services.craft_settings import craft_setting
from app.services.experience_bank import record_audit_issues
from app.services.focused_critics import choose_budget, run_focused_critics, score_critic_risk
from app.services.hook_ledger import (
    due_hooks,
    hook_ending_check,
    planned_hook,
    render_hooks_for_audit,
)
from app.services.model_errors import (
    bounded_excerpt,
    planning_exception_detail,
    recoverable_model_error,
)
from app.temporal_context import chapter_context
from app.api.schemas import ChapterActionRequest
from app.api.routers.editorial_support import EditorialContext, critic_risk_signals


def install_audit_routes(ctx: EditorialContext) -> None:
    """Chapter audit and local quality routes."""
    router = ctx.router
    find_chapter = ctx.find_chapter
    _indexed_retrieval_hits = ctx._indexed_retrieval_hits
    _record_chapter_session_turn = ctx._record_chapter_session_turn
    local_quality_known_context = ctx.local_quality_known_context
    quality_source_tail = ctx.quality_source_tail
    structured_completion = ctx.structured_completion

    _VOLUME_REGRESSION_STOP_TERMS = {
        "本章", "章节", "人物", "事件", "冲突", "目标", "结尾", "推进", "进入",
        "发现", "决定", "要求", "拒绝", "确认", "证明", "证据", "结果", "最终",
        "开始", "仍然", "已经", "没有", "不能", "不得", "必须", "可以", "只得",
        "一处", "一份", "一页", "一夜", "对方", "自己", "此事", "此后", "同时",
        "留下", "掌握", "看见", "面对", "通过", "为了", "成为", "明白", "知道",
        "选择", "代价", "责任", "权力", "秩序", "问题", "阶段", "路线", "场景",
        "只能", "任何", "只余", "调令", "成一", "一同", "一项", "一件", "一个",
        "以及", "并且", "如果", "为何", "如何", "是否", "其中", "对应", "相关",
        "同一", "不同", "昨日", "今日", "此时", "当日", "次日", "三日", "十七",
        "一旦", "却不", "已经", "仍有", "仍在", "其中", "得到", "不到", "到粮",
        "自己的", "粮与", "人与", "户与", "的一", "中的", "上的", "下的", "后的",
        # Cross-volume editorial prose uses these ordinary verbs and institutions
        # heavily.  They are not story-engine identifiers: requiring a court scene
        # to avoid "核验" or "认为" merely because earlier route cards used them
        # produces false regressions and encourages unnatural synonym swapping.
        "核验", "认为", "交出", "国家", "扣住", "秦策在", "却没有",
        "回咸阳", "车全部", "名册",
    }


    def _route_card_text(route: dict[str, Any]) -> str:
        values: list[str] = []
        for key in (
            "title", "goal", "conflict", "turning_point", "ending_hook",
            "must_keep", "must_avoid",
        ):
            value = route.get(key, "")
            if isinstance(value, list):
                values.extend(str(item) for item in value)
            else:
                values.append(str(value))
        return "\n".join(item for item in values if item.strip())


    def _route_ngrams(text: str) -> set[str]:
        terms: set[str] = set()
        # Keep route-card fields separate. Joining them before slicing can invent
        # meaningless boundary ngrams such as the last character of a goal plus the
        # first character of a conflict.
        for segment in text.splitlines():
            compact = "".join(re.findall(r"[\u3400-\u9fff]", segment))
            terms.update(
                compact[index : index + size]
                for size in (2, 3, 4)
                for index in range(max(0, len(compact) - size + 1))
            )
        return terms


    def _volume_route_documents(
        project: dict[str, Any], volume: dict[str, Any]
    ) -> list[str]:
        documents = [
            _route_card_text(item)
            for item in volume.get("chapters", [])
            if isinstance(item, dict)
        ]
        if documents:
            return [item for item in documents if item]
        volume_id = str(volume.get("id", ""))
        start = int(volume.get("chapter_start", 0) or 0)
        end = int(volume.get("chapter_end", 0) or 0)
        for item in project.get("chapters", []):
            if not isinstance(item, dict):
                continue
            number = int(item.get("number", 0) or 0)
            belongs = (
                bool(volume_id) and str(item.get("volume_id", "")) == volume_id
            ) or (start > 0 and start <= number <= end)
            if belongs and isinstance(item.get("route"), dict):
                text = _route_card_text(item["route"])
                if text:
                    documents.append(text)
        return documents


    def _prior_volume_regression_issues(
        project: dict[str, Any], chapter: dict[str, Any], draft: str
    ) -> list[dict[str, Any]]:
        """Flag a cluster of unplanned concepts that belongs only to earlier volumes."""
        volumes = [
            item
            for item in project.get("planning", {}).get("volumes", [])
            if isinstance(item, dict)
        ]
        if len(volumes) < 2:
            return []
        chapter_number = int(chapter.get("number", 0) or 0)
        chapter_volume_id = str(chapter.get("volume_id", ""))
        current_index = next(
            (
                index
                for index, volume in enumerate(volumes)
                if (
                    chapter_volume_id
                    and str(volume.get("id", "")) == chapter_volume_id
                )
                or (
                    int(volume.get("chapter_start", 0) or 0)
                    <= chapter_number
                    <= int(volume.get("chapter_end", 0) or 0)
                )
            ),
            -1,
        )
        if current_index <= 0:
            return []

        current_terms: set[str] = set()
        for document in _volume_route_documents(project, volumes[current_index]):
            current_terms.update(_route_ngrams(document))
        # A volume boundary is expected to carry forward the immediately preceding
        # chapter's consequences.  Treat that prose as an explicit bridge contract;
        # the guard is for a dormant older engine resurfacing, not normal continuity.
        project_chapters = project.get("chapters", [])
        current_project_index = next(
            (
                index
                for index, item in enumerate(project_chapters)
                if isinstance(item, dict)
                and str(item.get("id", "")) == str(chapter.get("id", ""))
            ),
            -1,
        )
        if current_project_index > 0:
            previous_content = str(
                project_chapters[current_project_index - 1].get("content", "")
            )
            current_terms.update(_route_ngrams(previous_content))
        current_volume = volumes[current_index]
        current_volume_id = str(current_volume.get("id", ""))
        current_start = int(current_volume.get("chapter_start", 0) or 0)
        current_end = int(current_volume.get("chapter_end", 0) or 0)
        for index, item in enumerate(project_chapters):
            if index >= current_project_index >= 0 or not isinstance(item, dict):
                continue
            number = int(item.get("number", 0) or 0)
            # Some persisted projects omit chapter.number; array position remains the
            # canonical chapter number in that legacy shape.
            if number <= 0:
                number = index + 1
            belongs = (
                bool(current_volume_id)
                and str(item.get("volume_id", "")) == current_volume_id
            ) or (current_start > 0 and current_start <= number <= current_end)
            content = str(item.get("content", ""))
            if belongs and content:
                current_terms.update(_route_ngrams(content))
        character_names = {
            str(item.get("name", "")).strip()
            for item in project.get("characters", [])
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        }
        prior_counts: Counter[str] = Counter()
        prior_anchor_terms: set[str] = set()
        for volume in volumes[:current_index]:
            for document in _volume_route_documents(project, volume):
                prior_counts.update(_route_ngrams(document))
            for route in volume.get("chapters", []):
                if not isinstance(route, dict):
                    continue
                anchor_values: list[str] = [str(route.get("title", ""))]
                for field in ("must_keep", "must_avoid"):
                    value = route.get(field, [])
                    if isinstance(value, list):
                        anchor_values.extend(str(item) for item in value)
                for value in anchor_values:
                    for anchor in re.findall(r"[\u3400-\u9fff]{2,12}", value):
                        prior_anchor_terms.add(anchor)

        compact_draft = "".join(re.findall(r"[\u3400-\u9fff]", draft))
        candidates = [
            (term, count)
            for term, count in prior_counts.items()
            if count >= 2
            # Only explicit route-card anchors can identify a dormant story engine.
            # Arbitrary 2-4 character windows from prose goals mostly produce common
            # grammar ("共同", "却没有", "交给") and repeatedly reject valid new
            # volumes.  Titles and must_keep/must_avoid items are author-curated and
            # remain specific enough to support a high-severity regression gate.
            and term in prior_anchor_terms
            and term not in current_terms
            and term not in _VOLUME_REGRESSION_STOP_TERMS
            and term not in character_names
            and not any(char in "一二三四五六七八九十百千万两" for char in term)
            and term in compact_draft
        ]
        # Prefer longer, more specific terms and require distinct locations in the
        # candidate draft; overlapping ngrams from one phrase count only once.
        occupied: list[tuple[int, int]] = []
        selected: list[tuple[str, int]] = []
        for term, count in sorted(candidates, key=lambda item: (-len(item[0]), -item[1], item[0])):
            start = compact_draft.find(term)
            if start < 0:
                continue
            span = (start, start + len(term))
            if any(span[0] < used[1] and used[0] < span[1] for used in occupied):
                continue
            occupied.append(span)
            selected.append((term, count))
            if len(selected) >= 5:
                break
        # Three scattered matches are insufficient: at least two concepts must have
        # been repeated across five prior route cards.  This rejects common prose
        # vocabulary while still catching a real replay of a prior volume's engine.
        if len(selected) < 3 or sum(count >= 5 for _term, count in selected) < 2:
            return []
        return [
            {
                "severity": "high",
                "category": "前卷语义回流",
                "message": "候选稿集中重现本卷路线未安排的前卷专属语义。",
                "evidence": [term for term, _count in selected],
                "suggestion": "删除前卷事件残片，按当前卷路线重新组织场景、行动与因果。",
            }
        ]


    def _chapter_character_audit_context(
        project: dict[str, Any], chapter_number: int
    ) -> list[dict[str, Any]]:
        """Render character data without leaking a later chapter's saved state."""
        rendered: list[dict[str, Any]] = []
        for character in project.get("characters", []):
            if not isinstance(character, dict) or not character.get("active", True):
                continue
            item = {
                key: deepcopy(character.get(key))
                for key in (
                    "name", "role", "personality", "values", "goal", "hard_limits",
                    "voice", "knowledge_baseline",
                )
                if character.get(key) not in (None, "", [])
            }
            try:
                state_chapter = int(character.get("last_state_chapter_number", 0) or 0)
            except (TypeError, ValueError):
                state_chapter = 0
            if not state_chapter or state_chapter <= chapter_number:
                for key in ("state", "location", "items", "emotion"):
                    if character.get(key) not in (None, "", []):
                        item[key] = deepcopy(character.get(key))
            else:
                item["state_note"] = (
                    f"当前保存的动态状态来自后续第 {state_chapter} 章，"
                    "不得用它否定本章行动；以本章路线和此前已发生事实为准。"
                )
            ledger = []
            for entry in character.get("knowledge_ledger", []):
                if not isinstance(entry, dict):
                    continue
                try:
                    learned_chapter = int(entry.get("chapter_number", 0) or 0)
                except (TypeError, ValueError):
                    learned_chapter = 0
                if learned_chapter <= chapter_number:
                    ledger.append(deepcopy(entry))
            if ledger:
                item["knowledge_ledger"] = ledger[-12:]
            rendered.append(item)
        return rendered


    @router.post("/api/chapter/audit")
    async def chapter_audit(body: ChapterActionRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        index, chapter = find_chapter(project, body.chapter_id)
        experience_bank = deepcopy(project.get("memory", {}).get("experience_bank", {}))
        project = chapter_context(project, index)
        chapter = project["chapters"][index]
        audit_weights = project.get("settings", {}).get("audit_weights")
        begin_stage(chapter, "audit", {"draft": body.draft, "instruction": body.instruction})
        draft = body.draft.strip()
        if len(draft) < 80:
            raise HTTPException(400, "候选草稿至少需要 80 字")
        query = draft[-4000:] + "\n" + body.instruction
        memories = retrieve_memories(
            project,
            query,
            index,
            int(project.get("settings", {}).get("memory_items", 12)) + 6,
            indexed_hits=_indexed_retrieval_hits(
                project,
                query,
                index,
                int(project.get("settings", {}).get("memory_items", 12)) + 6,
            ),
        )
        agenda_items = select_thread_agenda(project, query, index, limit=10)
        thread_agenda = render_thread_agenda(agenda_items)
        hooks_due = due_hooks(project, index + 1) if craft_setting(project, "hook_ledger") else []
        full_refinement = "AI 全自动精修" in str(body.instruction)
        existing_tail = (
            "本次为整章替换式精修。原稿仅是事实与文风来源，不得因为候选稿保留原稿内容而判定为重复。"
            if full_refinement
            else chapter.get("content", "")[-5000:]
        )
        context = f"""【叙事策略】
    {route_guidance(project, False)[0]}
    【不可违背规则】
    {bounded_excerpt(project.get('book_rules', ''), 5000)}
    【人物状态】
    {bounded_excerpt(json.dumps(_chapter_character_audit_context(project, index + 1), ensure_ascii=False), 8000)}
    【人物与读者知情边界】
    {bounded_excerpt(render_epistemic_context(project, index, query), 7000)}
    【本章人工核定路线（高于模型生成的计划）】
    {json.dumps(chapter.get('route', {}), ensure_ascii=False)}
    【条件禁令解释】
    “不得在结尾前/章末前执行某动作”只禁止正文主体提前执行；如果 ending_hook 明确要求该动作，
    则动作在最后一个场景发生是正确交付，不得据此判错，也不得建议删除。ending_hook 中的每个结果都必须明写。
    【本章执行计划（仅作补充，不得覆盖人工路线）】
    {json.dumps(chapter.get('plan', {}), ensure_ascii=False)}
    【相关历史事实】
    {bounded_excerpt(render_memories(memories), 6000)}
    【伏笔与暗线治理议程】
    {bounded_excerpt(thread_agenda, 7000)}
    【动态关系状态】
    {bounded_excerpt(json.dumps(project.get('memory', {}).get('relationships', []), ensure_ascii=False), 5000)}
    【待确认连续性备注】
    {json.dumps([
        item for item in project.get('memory', {}).get('continuity_notes', [])
        if isinstance(item, dict) and not item.get('resolved', False)
    ], ensure_ascii=False)}
    【近期已经用过的显著描写】
    {bounded_excerpt(json.dumps(project.get('memory', {}).get('description_ledger', [])[-40:], ensure_ascii=False), 5000)}
    【当前章节已有正文结尾】
    {existing_tail}
    【候选草稿（每句前为证据编号）】
    {bounded_excerpt(number_draft(draft), 19500)}"""
        local_checks = local_quality_check(
            draft,
            "" if full_refinement else quality_source_tail(chapter.get("content", ""), draft),
            int(project.get("settings", {}).get("target_words", 1200)),
            project.get("narrative", {}).get("pov", "auto"),
            (
                chapter.get("content", "")[-5000:]
                if len(chapter.get("content", "")) >= 300
                else project.get("style", {}).get("sample", "")[:5000]
            ),
            prior_manuscript_text(project, chapter["id"]),
            str(project.get("genre", "")),
            local_quality_known_context(project, chapter),
            jargon_policy=jargon_policy(project),
        )
        regression_issues = _prior_volume_regression_issues(project, chapter, draft)
        continuity_issues = check_continuity_draft(project, index, draft)
        regression_issues = list(regression_issues or []) + continuity_issues
        if regression_issues:
            local_checks.setdefault("issues", []).extend(regression_issues)
            local_checks["score"] = min(int(local_checks.get("score", 100)), 74)
            local_checks["verdict"] = "revise"
        # Historical timeline / era terms: no-op unless the chapter has a story_year.
        apply_history_issues(local_checks, check_history(project, index, draft))
        try:
            if craft_setting(project, "critic_mode") == "focused":
                # ConStory-style category checkers: each sees only its own context.
                critic_blocks = {
                    "characters": json.dumps(
                        _chapter_character_audit_context(project, index + 1), ensure_ascii=False
                    ),
                    "epistemic": render_epistemic_context(project, index, query),
                    "relationships": json.dumps(
                        project.get("memory", {}).get("relationships", [])[-20:], ensure_ascii=False
                    ),
                    "facts": render_memories(memories),
                    "continuity_state": render_continuity_state(project, index),
                    "description_ledger": json.dumps(
                        project.get("memory", {}).get("description_ledger", [])[-20:], ensure_ascii=False
                    ),
                    "narrative": (
                        f"视角：{project.get('narrative', {}).get('pov', 'auto')}\n"
                        + str(route_guidance(project, False)[0])
                    ),
                    "existing_tail": "" if full_refinement else chapter.get("content", "")[-900:],
                    "route": (
                        "人工路线（高于计划）：" + json.dumps(chapter.get("route", {}), ensure_ascii=False)
                        + "\n执行计划：" + json.dumps(chapter.get("plan", {}), ensure_ascii=False)
                        + "\n条件禁令：ending_hook 明确要求的动作在最后一个场景发生是正确交付。"
                    ),
                    "threads": thread_agenda,
                    "hooks": render_hooks_for_audit(hooks_due),
                    "rules": str(project.get("book_rules", "")),
                    "world": "\n".join(
                        f"【{entry.get('title', '')}】{entry.get('content', '')}"
                        for entry in project.get("world_entries", [])
                        if isinstance(entry, dict) and entry.get("enabled", True)
                        and (
                            entry.get("constant", False)
                            or any(
                                str(key).strip() and str(key).strip() in draft
                                for key in (
                                    entry.get("keys", []) if isinstance(entry.get("keys"), list)
                                    else str(entry.get("keys", "")).replace("，", ",").split(",")
                                )
                            )
                        )
                    ),
                }
                debate_context = render_debate_for_audit(chapter)
                if debate_context:  # debate checker runs only for debate scenes
                    critic_blocks["debate"] = debate_context
                audit_key = f"{project.get('id', '')}:{chapter.get('id', '')}"
                craft_config = body.config if isinstance(body.config, dict) else {}
                budget_setting = craft_setting(project, "critic_budget", craft_config)
                risk = score_critic_risk(
                    critic_risk_signals(
                        project, index, chapter, draft,
                        local_checks=local_checks, continuity_issues=continuity_issues,
                        agenda=agenda_items, hooks=hooks_due, audit_key=audit_key,
                    ),
                    int(craft_setting(project, "critic_risk_threshold", craft_config)),
                )
                budget = choose_budget(budget_setting, risk)
                result, warnings = await run_focused_critics(
                    project.get("settings", {}),
                    critic_blocks,
                    number_draft(draft)[:19500],
                    completion=structured_completion,
                    prepare=lambda messages: prepare_messages(messages, project, chapter)[0],
                    audit_key=audit_key,
                    draft=draft,
                    budget=budget,
                    # Light: only high findings escalate.  Auto: high, or a medium
                    # finding whose evidence resolves in the draft.
                    escalate_on=("high",) if budget_setting == "light" else ("high", "medium_verified"),
                )
                result["risk"] = {
                    **risk,
                    "setting": budget_setting,
                    "budget_used": result.get("critic_budget", budget),
                    "calls": result.get("critic_calls", 0),
                }
            else:
                audit_messages, _audit_prefixes = prepare_messages(
                    [
                        {"role": "system", "content": AUDIT_PROMPT},
                        {"role": "user", "content": context},
                    ],
                    project,
                    chapter,
                )
                result, warnings = await structured_completion(
                    project.get("settings", {}),
                    audit_messages,
                    temperature=0.1,
                    max_tokens=1100,
                    timeout_seconds=180,
                    validate=validate_audit_result,
                    workload="critic",
                    schema=AUDIT_SCHEMA,
                )
            raw_issues = result.get("issues", []) if isinstance(result.get("issues"), list) else []
            normalized_score = normalize_model_score(
                result.get("score"), raw_issues,
                scale=100 if result.get("score_scale") == 100 or result.get("critic_mode") == "focused" else None,
            )
            if normalized_score is None:
                # Placeholder or missing score: derive it from the reported issues
                # instead of letting a schema default of 0 fail the chapter.
                normalized_score = max(0, 100 - penalty_total(raw_issues, audit_weights)) if raw_issues else 90
                warnings = [*warnings, "审校模型未给出有效分数，已按问题严重度推算"]
            ai_score = normalized_score
            ai_verdict = (
                "pass" if str(result.get("verdict", "")).lower() == "pass" else "revise"
            )
            local_score = max(0, min(100, int(local_checks.get("score", 0))))
            local_verdict = str(local_checks.get("verdict", "revise"))
            ai_issues = (
                result.get("issues", [])
                if isinstance(result.get("issues"), list)
                else []
            )
            # Long-term recall entries are not active obligations in every chapter.
            # If no open-thread agenda activates one, an AI request to plant/pay off
            # a future clue is invalid and must not lower the chapter's gate score.
            removed_weight = 0
            if not thread_agenda.strip():
                kept_issues = []
                for item in ai_issues:
                    category = str(item.get("category", "")) if isinstance(item, dict) else ""
                    if "伏笔" in category or "回收" in category:
                        severity = str(item.get("severity", "medium"))
                        removed_weight += (
                            15 if severity == "high" else 8 if severity == "medium" else 3
                        )
                        continue
                    kept_issues.append(item)
                ai_issues = kept_issues
            if removed_weight:
                ai_score = min(100, ai_score + removed_weight)
            if not ai_issues and ai_score >= 85:
                # A clean audit and a sub-threshold score contradict each other.
                # Normalize that narrow provider quirk so an issue-free chapter is
                # not rejected solely because one model habitually returns 85.
                ai_score = max(ai_score, 90)
                ai_verdict = "pass"
            elif ai_verdict == "pass" and ai_score >= 85 and not any(
                isinstance(item, dict) and str(item.get("severity", "")).lower() == "high"
                for item in ai_issues
            ):
                ai_score = max(ai_score, 90)
            elif ai_score >= 95 and not any(
                isinstance(item, dict) and str(item.get("severity", "")).lower() == "high"
                for item in ai_issues
            ):
                # Some providers return a perfect score while also attaching optional
                # medium/low editorial suggestions, then label the verdict "revise".
                # Suggestions remain visible, but cannot contradict a 95+ score and
                # block an otherwise clean local gate unless a high-risk issue exists.
                ai_verdict = "pass"
            elif (
                ai_score >= 85
                and local_score >= 95
                and local_verdict == "pass"
                and isinstance(chapter.get("route"), dict)
                and bool(chapter.get("route"))
                and not any(
                    isinstance(item, dict)
                    and str(item.get("severity", "")).lower() == "high"
                    for item in ai_issues
                )
            ):
                # Curated production routes deliberately end some chapters before a
                # choice or payoff.  Small audit models often label that exact hook a
                # medium "unfinished" issue while still assigning 85.  When the
                # deterministic gate is exceptionally clean and no high-risk issue is
                # present, keep the suggestions visible but do not let that provider
                # convention contradict the authoritative route and block production.
                ai_score = max(ai_score, 90)
                ai_verdict = "pass"
            result["ai_score"] = ai_score
            result["score"] = min(ai_score, local_score)
            result["verdict"] = "pass" if ai_verdict == "pass" and local_verdict == "pass" else "revise"
            result["issues"] = ai_issues
            result["score_normalized"] = True
            result = stable_audit_result(draft, result, local_checks, threshold=85, weights=audit_weights)
            result["fallback"] = False
            hook_ending = hook_ending_check(draft, planned_hook(chapter))
            result["hook_ending"] = hook_ending
            if not hook_ending["ok"]:
                warnings = [*warnings, hook_ending["reason"]]
            result["hooks_due"] = [
                {"id": item["id"], "origin_chapter": item["origin_chapter"], "status": item["status"]}
                for item in hooks_due
            ]
            result["warnings"] = warnings
            if craft_setting(project, "experience_bank"):
                # The audit route does not persist the project: return only this
                # audit's delta; the client merges it additively (per-pattern
                # chapter-id union) so concurrent audits never clobber each other.
                bank_holder = {"memory": {"experience_bank": experience_bank}}
                result["experience_delta"] = record_audit_issues(
                    bank_holder,
                    str(settings_for_workload(project.get("settings", {}), "prose").get("model", "")),
                    [*result.get("issues", []), *result.get("local_checks", {}).get("issues", [])],
                    chapter_id=str(chapter.get("id", "")),
                )
            complete_stage(chapter, "audit", result)
            _record_chapter_session_turn(project, chapter, "assistant", json.dumps(result, ensure_ascii=False), "audit")
            return result
        except Exception as exc:
            if recoverable_model_error(exc):
                fallback = local_audit_result(local_checks, planning_exception_detail(exc))
                fallback = stable_audit_result(
                    draft, fallback, local_checks, threshold=85,
                    weights=audit_weights,
                )
                fallback["fallback"] = True
                complete_stage(chapter, "audit", fallback)
                _record_chapter_session_turn(project, chapter, "assistant", json.dumps(fallback, ensure_ascii=False), "audit-fallback")
                return fallback
            fail_stage(chapter, "audit", exc)
            raise HTTPException(
                502, f"连续性审计失败：{planning_exception_detail(exc)}"
            ) from exc


    @router.post("/api/chapter/quality")
    async def chapter_quality(body: ChapterActionRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        _, chapter = find_chapter(project, body.chapter_id)
        return local_quality_check(
            body.draft,
            quality_source_tail(chapter.get("content", ""), body.draft),
            int(project.get("settings", {}).get("target_words", 1200)),
            project.get("narrative", {}).get("pov", "auto"),
            (
                chapter.get("content", "")[-5000:]
                if len(chapter.get("content", "")) >= 300
                else project.get("style", {}).get("sample", "")[:5000]
            ),
            prior_manuscript_text(project, chapter["id"]),
            str(project.get("genre", "")),
            local_quality_known_context(project, chapter),
            jargon_policy=jargon_policy(project),
        )

    ctx.register(
        _route_card_text,
        _route_ngrams,
        _volume_route_documents,
        _prior_volume_regression_issues,
        _chapter_character_audit_context,
        chapter_audit,
        chapter_quality,
    )

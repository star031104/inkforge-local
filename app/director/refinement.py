"""Whole-book AI refinement: audit → targeted patch/rewrite → lock or record debt."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

from app.writing.requests import ChapterActionRequest
from app.core.clock import utc_now
from app.writing.prose import (
    _dedupe_adjacent_sentence_blocks,
    _dedupe_exact_paragraphs,
    _prose_char_count,
)
from app.review.editorial_workflow import enqueue_repair, lock_chapter
from app.review.manuscript_quality import manuscript_health_report
from app.llm.telemetry import usage_scope
from app.review.contracts import scan_contracts
from app.review.editorial_policy import (
    _audit_issues,
    _director_manuscript_gate_failures,
    _refinement_candidate_passes,
    _refinement_candidate_rank,
    _resolve_project_repairs,
    convergence_message,
    refinement_convergence,
)
from app.review.memory_settlement import _apply_director_memory
from app.llm.errors import planning_exception_detail
from app.director.common import _craft_overrides
from app.director.context import DirectorContext


def install(ctx: DirectorContext) -> None:
    """Define the refinement closures on ``ctx``."""
    store = ctx.store
    chapter_audit = ctx.chapter_audit
    chapter_memory = ctx.chapter_memory
    find_chapter = ctx.find_chapter
    _director_candidate_gate_failures = ctx._director_candidate_gate_failures
    _save_director_task = ctx._save_director_task
    _quality_debt_index = ctx._quality_debt_index
    _remove_quality_debt = ctx._remove_quality_debt
    _director_repair_memory = ctx._director_repair_memory
    _director_after_audit = ctx._director_after_audit
    _director_settle_hooks = ctx._director_settle_hooks
    _director_generate_prose = ctx._director_generate_prose
    _director_patch_refinement_excerpts = ctx._director_patch_refinement_excerpts

    async def _run_auto_refinement(
        task: dict[str, Any], project: dict[str, Any]
    ) -> None:
        """Audit and revise existing chapters without replacing failed candidates."""
        config = task["config"]
        targets = [str(item) for item in task.get("refinement_targets", []) if str(item)]
        task["total_chapters"] = len(targets)
        start = max(0, int(task.get("refine_index", 0) or 0))
        for position in range(start, len(targets)):
            latest = store.get_director_task(task["id"])
            if not latest or latest.get("status") != "running":
                raise asyncio.CancelledError()
            task.clear()
            task.update(latest)
            project = store.get(task["project_id"])
            if not project:
                raise ValueError("精修作品已被删除")
            chapter_id = targets[position]
            with usage_scope(task["project_id"], chapter_id):
                chapter_index, chapter = find_chapter(project, chapter_id)
                original = str(chapter.get("content", "")).strip()
                task["current_chapter"] = chapter_index + 1
                task["current_refine_position"] = position + 1
                task = _save_director_task(
                    task,
                    f"AI 精修 {position + 1}/{len(targets)}：正在审校《{chapter.get('title', '')}》",
                )
                if len(original) < 100:
                    issue = {
                        "severity": "high", "category": "正文缺失",
                        "message": "章节正文不足 100 字，无法在保留事实的前提下精修。",
                        "suggestion": "先让 AI 按章节计划生成完整正文。",
                    }
                    enqueue_repair(project, chapter, issue, source="auto-refine")
                    task.setdefault("quality_debts", []).append(
                        {"chapter": chapter_index + 1, "chapter_id": chapter_id,
                         "title": chapter.get("title", ""), "score": 0, "issues": [issue]}
                    )
                    project = store.save(project["id"], project, reason=f"refine-chapter-{chapter_index + 1}-skipped")
                    task["refine_index"] = position + 1
                    task["completed_chapters"] = position + 1
                    task = _save_director_task(task, f"第 {chapter_index + 1} 章正文不足，已保留并加入待办", "warning")
                    continue

                draft = original
                audit: dict[str, Any] = {}
                contract_result: dict[str, Any] = {}
                passed = False
                used_attempts = 0
                clean_original = _dedupe_exact_paragraphs(
                    _dedupe_adjacent_sentence_blocks(original)
                )
                original_chars = _prose_char_count(original)
                clean_chars = _prose_char_count(clean_original)
                if clean_chars < int(original_chars * 0.75):
                    # A loop-inflated chapter must not be asked to regenerate all of the
                    # duplicated length. Preserve the substantive first copy and allow a
                    # modest expansion for transitions and missing scene texture.
                    target_chars = max(300, min(5000, int(clean_chars * 1.25)))
                else:
                    target_chars = max(300, min(5000, original_chars))
                task["config"]["target_words"] = target_chars
                best_draft = original
                best_audit: dict[str, Any] = {}
                best_contract: dict[str, Any] = {}
                best_rank = (-1, -10_000, -1)
                original_score = 0
                for attempt in range(int(config["max_revision_attempts"]) + 1):
                    audit_project = deepcopy(project)
                    audit_project.setdefault("settings", {})["target_words"] = target_chars
                    audit = await chapter_audit(
                        ChapterActionRequest(
                            project=audit_project, chapter_id=chapter_id, draft=draft,
                            instruction="AI 全自动精修：核对连续性、章节目标、人物状态、语言质量和跨章重复。",
                            config=_craft_overrides(task),
                        )
                    )
                    _director_after_audit(task, audit, project, chapter_id)
                    contract_result = scan_contracts(project, chapter, draft, chapter_index + 1)
                    if not contract_result.get("passed", False):
                        violations = [
                            {
                                "severity": str(item.get("severity", "high")),
                                "category": "硬契约",
                                "message": str(item.get("message", "硬契约未满足")),
                                "suggestion": "严格满足硬契约，同时保留原有情节事实。",
                                "evidence": item.get("evidence", []),
                                "evidence_verified": True,
                            }
                            for item in contract_result.get("violations", [])
                            if isinstance(item, dict)
                        ]
                        audit.setdefault("issues", []).extend(violations)
                        audit["score"] = min(int(audit.get("score", 0) or 0), 74)
                        audit["verdict"] = "revise"
                    score = int(audit.get("score", 0) or 0)
                    if attempt == 0:
                        original_score = score
                    candidate_rank = _refinement_candidate_rank(
                        draft, audit, contract_result,
                        int(config["quality_threshold"]), target_chars,
                    )
                    if candidate_rank > best_rank:
                        best_rank = candidate_rank
                        best_draft = draft
                        best_audit = deepcopy(audit)
                        best_contract = deepcopy(contract_result)
                    task["current_audit"] = {
                        "chapter": chapter_index + 1, "chapter_id": chapter_id,
                        "score": score, "verdict": str(audit.get("verdict", "review")),
                        "attempt": attempt + 1, "updated_at": utc_now(),
                    }
                    task = _save_director_task(
                        task,
                        f"《{chapter.get('title', '')}》第 {attempt + 1} 次审校完成：{score} 分",
                        "success" if audit.get("verdict") == "pass" else "warning",
                    )
                    passed = _refinement_candidate_passes(
                        audit, contract_result, int(config["quality_threshold"])
                    )
                    if (
                        passed
                        and attempt == 0
                        and str(task.get("manuscript_focus", {}).get(chapter_id, "")).strip()
                    ):
                        # The chapter passes on its own but is a top contributor to a
                        # failing manuscript-level gate: give it one focused revision.
                        passed = False
                    if passed or attempt >= int(config["max_revision_attempts"]):
                        used_attempts = attempt
                        break
                    issues = _audit_issues(audit)
                    requirements = "\n".join(
                        f"{number}. [{item.get('category', '问题')}] {item.get('message', '')}；建议：{item.get('suggestion', '')}"
                        for number, item in enumerate(issues, start=1)
                    ) or str(audit.get("revision_brief", "提高本章连续性与语言质量"))
                    extra = str(config.get("instruction", "")).strip()
                    focus = str(task.get("manuscript_focus", {}).get(chapter_id, "")).strip()
                    if focus:
                        requirements = (requirements + "\n" + focus).strip()
                    task = _save_director_task(
                        task, f"《{chapter.get('title', '')}》未达 {config['quality_threshold']} 分，正在自动修订"
                    )
                    inherited = task.get("seed_candidates", {}).get(chapter_id, {})
                    inherited_draft = (
                        str(inherited.get("draft", "")).strip()
                        if isinstance(inherited, dict)
                        else ""
                    )
                    if attempt == 0 and len(inherited_draft) >= 100 and inherited_draft != original:
                        draft = inherited_draft
                        task["refinement_checkpoint"] = {
                            "chapter_id": chapter_id,
                            "position": position + 1,
                            "draft": draft[:30000],
                            "attempt": attempt + 1,
                            "saved_at": utc_now(),
                            "inherited": True,
                        }
                        used_attempts = attempt + 1
                        task = _save_director_task(
                            task,
                            f"《{chapter.get('title', '')}》已继承上次最佳候选（{inherited.get('score', 0)} 分），继续局部精修",
                            "success",
                        )
                        continue
                    revision_seed = _dedupe_exact_paragraphs(
                        _dedupe_adjacent_sentence_blocks(best_draft)
                    )
                    if revision_seed != best_draft:
                        task = _save_director_task(
                            task,
                            f"《{chapter.get('title', '')}》已先清理确定性重复，再交给 AI 补写精修",
                            "success",
                        )
                    draft = await _director_patch_refinement_excerpts(
                        project, chapter, revision_seed, best_audit or audit, target_chars
                    )
                    if draft != revision_seed:
                        task = _save_director_task(
                            task,
                            f"《{chapter.get('title', '')}》仅剩局部问题，已做定点修补并保持其余正文不变",
                            "success",
                        )
                    else:
                        draft = await _director_generate_prose(
                            task, project, chapter, mode="rewrite", selection=revision_seed,
                            instruction=(
                                "对整章做精修，只修复列出的问题。保留原文全部既定事实、人物关系、"
                                "事件顺序、叙事视角、伏笔与章末功能；禁止新增背景设定，禁止解释修改过程。\n"
                                "如果输入中有重复段落或循环句，删除重复副本后用新的现场动作、感官细节和人物反应补足篇幅；"
                                "不得换词复述已经发生的同一动作。\n"
                                + (f"作者额外要求：{extra}\n" if extra else "")
                                + requirements
                                + "\n只输出完整修订稿。"
                            ),
                        )
                    draft = _dedupe_exact_paragraphs(
                        _dedupe_adjacent_sentence_blocks(draft)
                    )
                    task["refinement_checkpoint"] = {
                        "chapter_id": chapter_id, "position": position + 1,
                        "draft": draft[:30000], "attempt": attempt + 1,
                        "saved_at": utc_now(),
                    }
                    used_attempts = attempt + 1
                    task = _save_director_task(task, f"《{chapter.get('title', '')}》第 {attempt + 1} 版修订稿已保存", "success")

                draft = best_draft
                audit = best_audit or audit
                contract_result = best_contract or contract_result
                passed = _refinement_candidate_passes(
                    audit, contract_result, int(config["quality_threshold"])
                )
                gate_failures = _director_candidate_gate_failures(draft, target_chars)
                if passed and not gate_failures:
                    if str(audit.get("verdict", "")) != "pass":
                        audit = deepcopy(audit)
                        audit["model_gate_verdict"] = str(audit.get("verdict", "revise"))
                        audit["verdict"] = "pass"
                        audit.setdefault("score_components", {})["threshold"] = int(
                            config["quality_threshold"]
                        )
                    changed = draft.strip() != original
                    chapter["content"] = draft.strip()
                    chapter["authority_state"] = "accepted"
                    lock_chapter(chapter, actor="auto-refine", audit=audit, contract_scan=contract_result)
                    _resolve_project_repairs(project, chapter_id)
                    memory_warnings: list[str] = []
                    if changed or str(chapter.get("memory_status", "")) != "committed":
                        try:
                            memory = await chapter_memory(
                                ChapterActionRequest(
                                    project=project, chapter_id=chapter_id,
                                    draft=draft, instruction="AI 精修通过后的状态与长期记忆回灌",
                                )
                            )
                            memory_warnings = list(memory.get("warnings", [])) if isinstance(memory, dict) else []
                            _apply_director_memory(project, chapter, memory)
                            await _director_repair_memory(task, project, chapter)
                            _director_settle_hooks(task, project, chapter, chapter_index, draft, audit)
                        except Exception as exc:
                            chapter["memory_status"] = "pending_rebuild"
                            memory_warnings = [f"精修正文已锁定，但记忆回灌待重试：{planning_exception_detail(exc)}"]
                            enqueue_repair(
                                project, chapter,
                                {"category": "记忆回灌", "severity": "medium", "message": memory_warnings[0]},
                                source="auto-refine",
                            )
                    _remove_quality_debt(task, chapter_id)
                    run_record = {
                        "status": "accepted", "last_run_at": utc_now(),
                        "model": str(project.get("settings", {}).get("model", "")),
                        "target_words": target_chars, "audit_score": int(audit.get("score", 0)),
                        "audit_verdict": str(audit.get("verdict", "pass")),
                        "revision_attempts": used_attempts, "issues": _audit_issues(audit),
                        "warnings": memory_warnings, "authority_state": chapter.get("authority_state", "locked"),
                        "contract_scan": contract_result, "refined_by_ai": True,
                    }
                    chapter["execution"] = run_record
                    chapter.setdefault("run_history", []).append(deepcopy(run_record))
                    chapter["run_history"] = chapter["run_history"][-10:]
                    project = store.save(project["id"], project, reason=f"auto-refine-chapter-{chapter_index + 1}")
                    task = _save_director_task(
                        task,
                        f"《{chapter.get('title', '')}》精修通过并已锁定（{audit.get('score', 0)} 分）",
                        "success",
                    )
                else:
                    issues = _audit_issues(audit)
                    if gate_failures:
                        issues.extend(
                            {"severity": "high", "category": "正文安全门", "message": item,
                             "suggestion": "重新生成完整修订稿。"}
                            for item in gate_failures
                        )
                    debt = {
                        "chapter": chapter_index + 1, "chapter_id": chapter_id,
                        "title": chapter.get("title", ""), "score": int(audit.get("score", 0) or 0),
                        "issues": issues,
                    }
                    debt_index = _quality_debt_index(task, chapter_id)
                    if debt_index >= 0:
                        task["quality_debts"][debt_index] = debt
                    else:
                        task.setdefault("quality_debts", []).append(debt)
                    for issue in issues:
                        enqueue_repair(project, chapter, issue, source="auto-refine")
                    task["last_rejected_candidate"] = {
                        **debt, "draft": draft[:30000], "rejected_at": utc_now(),
                    }
                    task.setdefault("refinement_candidates", {})[chapter_id] = {
                        **debt, "draft": draft[:30000], "saved_at": utc_now(),
                    }
                    project = store.save(project["id"], project, reason=f"auto-refine-chapter-{chapter_index + 1}-review")
                    task = _save_director_task(
                        task,
                        f"《{chapter.get('title', '')}》自动精修后仍未达标，原正文保持不变",
                        "warning",
                    )
                task.setdefault("refinement_results", []).append(
                    {
                        "chapter": chapter_index + 1,
                        "chapter_id": chapter_id,
                        "title": chapter.get("title", ""),
                        "status": "accepted" if passed and not gate_failures else "review",
                        "original_score": original_score,
                        "best_score": int(audit.get("score", 0) or 0),
                        "revision_attempts": used_attempts,
                        "updated_at": utc_now(),
                    }
                )
                task["refinement_results"] = task["refinement_results"][-300:]
                task.pop("refinement_checkpoint", None)
                task.pop("current_audit", None)
                task["refine_index"] = position + 1
                task["completed_chapters"] = position + 1
                task = store.save_director_task(task["id"], task)

        project = store.get(task["project_id"])
        final_health = manuscript_health_report(project)
        release_failures = _director_manuscript_gate_failures(
            final_health, final=True, genre=str(project.get("genre", ""))
        )
        task["latest_manuscript_health"] = final_health
        task["release_score"] = int(final_health.get("score", 0))
        task["release_failures"] = release_failures
        task["release_ready"] = not release_failures and not task.get("quality_debts")
        task["release_status"] = "ready" if task["release_ready"] else "needs_revision"
        task["phase"] = "completed"
        task["status"] = "completed"
        convergence = _refinement_convergence_for(task)
        task["convergence"] = convergence
        if convergence.get("stalled"):
            task["release_status"] = "needs_author"
            _save_director_task(task, convergence_message(convergence), "warning")
            return
        _save_director_task(
            task,
            (
                f"AI 全书精修完成，{len(targets)} 章已通过复核，可以导出"
                if task["release_ready"]
                else f"AI 全书精修完成；仍有 {len(task.get('quality_debts', []))} 章需要复核"
            ),
            "success" if task["release_ready"] else "warning",
        )

    def _refinement_history(project_id: str, exclude_id: str = "") -> list[dict[str, Any]]:
        """Earlier completed whole-book refinement rounds, oldest first."""
        lister = getattr(store, "recent_director_tasks", None)
        if not callable(lister):
            return []
        rounds = []
        for item in lister(project_id, task_type="refinement", limit=6):
            if item.get("id") == exclude_id or item.get("status") != "completed":
                continue
            if "release_score" not in item:
                continue
            rounds.append({
                "score": int(item.get("release_score", 0) or 0),
                "failures": list(item.get("release_failures", []) or []),
                "release_status": item.get("release_status", ""),
                "updated_at": item.get("updated_at", ""),
                "id": item.get("id", ""),
            })
        return list(reversed(rounds))

    def _refinement_convergence_for(task: dict[str, Any]) -> dict[str, Any]:
        if task.get("release_ready"):
            return {"stalled": False}
        history = _refinement_history(str(task.get("project_id", "")), str(task.get("id", "")))
        return refinement_convergence(
            history,
            {"score": task.get("release_score", 0), "failures": task.get("release_failures", [])},
        )

    ctx.register(
        _run_auto_refinement,
        _refinement_history,
        _refinement_convergence_for,
    )

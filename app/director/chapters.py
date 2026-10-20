"""Director chapter phase: the per-chapter write → audit → revise → settle loop."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.writing.requests import ChapterActionRequest
from app.core.clock import utc_now
from app.writing.prose import _prose_looks_truncated, _trim_incomplete_prose_tail
from app.review.editorial_workflow import enqueue_repair, lock_chapter
from app.story.memory_extraction import local_memory_result
from app.review.manuscript_quality import manuscript_health_report
from app.llm.telemetry import usage_scope
from app.review.contracts import scan_contracts
from app.review.editorial_policy import (
    _audit_issues,
    _director_manuscript_gate_failures,
    _quality_directives,
    _refinement_candidate_passes,
    _systemic_quality_issues,
)
from app.writing.craft_settings import craft_setting
from app.review.memory_settlement import _apply_director_memory
from app.review.volume_review import run_volume_review, volume_ending_at
from app.director.common import TaskStoppedError, _craft_overrides
from app.director.context import DirectorContext


def install(ctx: DirectorContext) -> None:
    """Define the chapters closures on ``ctx``."""
    store = ctx.store
    chapter_audit = ctx.chapter_audit
    chapter_plan = ctx.chapter_plan
    _director_candidate_gate_failures = ctx._director_candidate_gate_failures
    _director_event = ctx._director_event
    _save_director_task = ctx._save_director_task
    _record_planning_debt = ctx._record_planning_debt
    _quality_debt_index = ctx._quality_debt_index
    _remove_quality_debt = ctx._remove_quality_debt
    _director_repair_memory = ctx._director_repair_memory
    _director_after_audit = ctx._director_after_audit
    _director_settle_hooks = ctx._director_settle_hooks
    _director_rolling_replan = ctx._director_rolling_replan
    _director_chapter_memory = ctx._director_chapter_memory
    _director_generate_prose = ctx._director_generate_prose
    _director_initial_draft = ctx._director_initial_draft
    _director_patch_refinement_excerpts = ctx._director_patch_refinement_excerpts

    async def _director_volume_review(
        task: dict[str, Any], project: dict[str, Any], index: int
    ) -> dict[str, Any]:
        """Whole-volume review after a volume's last chapter; never stops the run."""
        volume = volume_ending_at(project, index + 1)
        if volume is None:
            return project
        title = str(volume.get("title", "")) or f"第{volume.get('number', '')}卷"
        try:
            _save_director_task(task, f"《{title}》已写完，正在整卷回顾")
            entry = await run_volume_review(
                project, str(volume.get("id", "")), completion=ctx.run_structured_completion
            )
            project = store.save(project["id"], project, reason=f"director-volume-review-{index + 1}")
            queued = sum(1 for item in entry["report"]["repair_tasks"] if item.get("status") == "queued")
            _director_event(task, f"《{title}》整卷回顾完成，新增精修任务 {queued} 项", "success")
        except TaskStoppedError:
            raise
        except Exception as exc:  # noqa: BLE001 - a review failure must not stop the director
            _director_event(task, f"《{title}》整卷回顾未完成（不影响继续写作）：{str(exc)[:200]}", "warning")
        return project

    async def _run_chapters_phase(task: dict[str, Any], config: dict[str, Any]) -> None:
        """Per chapter: plan → draft → audit/revise → lock → memory settlement."""
        project = store.get(task["project_id"])
        chapters = project.get("chapters", [])
        for index in range(int(task.get("chapter_index", 0)), len(chapters)):
            chapter = project["chapters"][index]
            with usage_scope(task["project_id"], chapter.get("id", "")):
                task["current_chapter"] = index + 1
                task = _save_director_task(task, f"第 {index + 1}/{len(chapters)} 章：正在细化《{chapter.get('title', '')}》")
                if chapter.get("content", "").strip():
                    if chapter.get("execution", {}).get("status") == "memory_pending":
                        draft = str(chapter["content"])
                        task = _save_director_task(
                            task, f"第 {index + 1} 章正文已保存，正在补全剧情记忆"
                        )
                        memory = await _director_chapter_memory(project, chapter, draft)
                        _apply_director_memory(project, chapter, memory)
                        await _director_repair_memory(task, project, chapter)
                        chapter["memory_status"] = "committed"
                        # The crash happened before the ledger saw this chapter.
                        _director_settle_hooks(task, project, chapter, index, draft, {})
                        chapter["execution"]["status"] = "accepted"
                        chapter["execution"]["warnings"] = list(memory.get("warnings", []))
                        project = store.save(
                            project["id"], project,
                            reason=f"director-chapter-{index + 1}-memory-recovered",
                        )
                        task.pop("chapter_draft_checkpoint", None)
                        task.pop("prose_stream_checkpoint", None)
                        task["completed_chapters"] = index + 1
                        task = _save_director_task(
                            task, f"第 {index + 1} 章剧情记忆已补全", "success"
                        )
                    if (
                        str(chapter.get("execution", {}).get("status", ""))
                        == "accepted"
                        and _remove_quality_debt(task, str(chapter.get("id", "")))
                    ):
                        task = _save_director_task(
                            task,
                            f"第 {index + 1} 章已通过复审，旧质量债务已清除",
                            "success",
                        )
                    task["chapter_index"] = index + 1
                    task = _save_director_task(task, "检测到已有正文，本章保持不变并跳过", "warning")
                    continue
                plan = await chapter_plan(
                    ChapterActionRequest(project=project, chapter_id=chapter["id"], instruction="自动导演模式：服从上层路线和已发生事实，只规划本章。")
                )
                if plan.get("fallback"):
                    issues = [str(item) for item in plan.get("warnings", []) if str(item).strip()]
                    _record_planning_debt(
                        task,
                        {
                            "phase": "chapter_plan", "chapter": index + 1,
                            "chapter_id": chapter.get("id", ""),
                            "title": chapter.get("title", ""),
                            "issues": issues or ["AI 单章细化失败，已使用上层章节路线继续"],
                        },
                    )
                    _director_event(
                        task,
                        f"第 {index + 1} 章 AI 细化未完成，已使用上层路线继续并记录规划债务",
                        "warning",
                    )
                chapter["plan"] = plan
                chapter.pop("plan_stale", None)
                chapter["scene_goal"] = plan.get("goal", chapter.get("scene_goal", ""))
                project = store.save(project["id"], project, reason=f"director-chapter-{index + 1}-plan")
                chapter = project["chapters"][index]
                draft_checkpoint = task.get("chapter_draft_checkpoint", {})
                if (
                    isinstance(draft_checkpoint, dict)
                    and str(draft_checkpoint.get("chapter_id", ""))
                    == str(chapter.get("id", ""))
                    and len(str(draft_checkpoint.get("draft", "")).strip()) >= 100
                ):
                    draft = str(draft_checkpoint["draft"]).strip()
                    draft, checkpoint_repair_note = _trim_incomplete_prose_tail(draft)
                    if checkpoint_repair_note:
                        task["chapter_draft_checkpoint"] = {
                            **draft_checkpoint,
                            "draft": draft[:30000],
                            "repaired_at": utc_now(),
                            "repair_note": checkpoint_repair_note,
                        }
                        task["last_rejected_candidate"] = {}
                        task = _save_director_task(
                            task,
                            f"第 {index + 1} 章检查点已自动修复：{checkpoint_repair_note}；现在重新审计",
                            "success",
                        )
                    if _prose_looks_truncated(draft):
                        task.pop("chapter_draft_checkpoint", None)
                        task["last_rejected_candidate"] = {}
                        task = _save_director_task(
                            task,
                            f"第 {index + 1} 章旧检查点无法安全补齐，已自动作废并重新生成正文",
                            "warning",
                        )
                        draft = await _director_generate_prose(
                            task,
                            project,
                            chapter,
                            instruction=(
                                "旧候选稿曾在半句中截断，已经作废。请从零生成完整本章，"
                                "严格执行本章计划并以完整句子自然收束，只输出小说正文。"
                            ),
                        )
                        task["chapter_draft_checkpoint"] = {
                            "chapter": index + 1,
                            "chapter_id": chapter.get("id", ""),
                            "draft": draft[:30000],
                            "revision_attempt": 0,
                            "saved_at": utc_now(),
                            "regenerated_from_invalid_checkpoint": True,
                        }
                        task = _save_director_task(
                            task,
                            f"第 {index + 1} 章重新生成的正文已保存到检查点，开始审计",
                            "success",
                        )
                    else:
                        task = _save_director_task(
                            task,
                            f"第 {index + 1} 章：已从正文检查点恢复，直接继续审计",
                            "success",
                        )
                else:
                    task = _save_director_task(task, f"第 {index + 1} 章：正在生成正文")
                    draft = await _director_initial_draft(
                        task, project, chapter, index,
                        "严格执行本章计划，从具体场景起笔，完成本章目标、冲突、转折和结尾推动力。只输出小说正文。",
                    )
                    task["chapter_draft_checkpoint"] = {
                        "chapter": index + 1,
                        "chapter_id": chapter.get("id", ""),
                        "draft": draft[:30000],
                        "revision_attempt": 0,
                        "saved_at": utc_now(),
                    }
                    task = _save_director_task(
                        task,
                        f"第 {index + 1} 章正文已保存到检查点，开始审计",
                        "success",
                    )
                audit: dict[str, Any] = {}
                revision_attempt_limit = min(
                    int(config["max_revision_attempts"]),
                    max(
                        0,
                        int(
                            project.get("settings", {}).get(
                                "director_revision_attempt_limit",
                                config["max_revision_attempts"],
                            )
                        ),
                    ),
                )
                for attempt in range(revision_attempt_limit + 1):
                    task = _save_director_task(task, f"第 {index + 1} 章：正在进行连续性与质量审计（第 {attempt + 1} 次）")
                    audit = await chapter_audit(
                        ChapterActionRequest(
                            project=project, chapter_id=chapter["id"], draft=draft, instruction="自动导演整章审计",
                            config=_craft_overrides(task),
                        )
                    )
                    risk_note = _director_after_audit(task, audit, project, str(chapter.get("id", "")))
                    audit_score = max(0, min(100, int(audit.get("score", 0) or 0)))
                    existing_debt = _quality_debt_index(task, str(chapter.get("id", "")))
                    if existing_debt >= 0:
                        task["quality_debts"][existing_debt] = {
                            "chapter": index + 1,
                            "chapter_id": chapter.get("id", ""),
                            "title": chapter.get("title", ""),
                            "score": audit_score,
                            "issues": _audit_issues(audit),
                        }
                    task["current_audit"] = {
                        "chapter": index + 1,
                        "chapter_id": chapter.get("id", ""),
                        "score": audit_score,
                        "verdict": str(audit.get("verdict", "review")),
                        "attempt": attempt + 1,
                        "risk": audit.get("risk"),
                        "updated_at": utc_now(),
                    }
                    task = _save_director_task(
                        task,
                        f"第 {index + 1} 章第 {attempt + 1} 次审计完成：{audit_score} 分{risk_note}",
                        "success" if audit.get("verdict") == "pass" else "warning",
                    )
                    if _refinement_candidate_passes(
                        audit, {}, int(config["quality_threshold"])
                    ):
                        _remove_quality_debt(task, str(chapter.get("id", "")))
                        break
                    if attempt >= revision_attempt_limit:
                        break
                    issues = _audit_issues(audit)
                    task["quality_directives"] = _quality_directives(audit)
                    requirements = "\n".join(
                        f"{number}. [{item.get('category', '问题')}] {item.get('message', '')}；建议：{item.get('suggestion', '')}"
                        for number, item in enumerate(issues, start=1)
                    ) or str(audit.get("revision_brief", "修复审计发现的问题"))
                    task = _save_director_task(task, f"第 {index + 1} 章：审计未通过，正在自动修订")
                    systemic = _systemic_quality_issues(audit)
                    if systemic:
                        draft = await _director_generate_prose(
                            task,
                            project,
                            chapter,
                            mode="instruction",
                            instruction=(
                                "上一候选存在结构性重复或阶段偏离，必须废弃原候选，从本章路线重新设计不同的场景动作链。"
                                "保留上层规定的章末新状态，但不得沿用原候选的段落、句式、对话问答或意象。\n"
                                + requirements
                            ),
                        )
                    else:
                        # Prefer targeted local fixes of evidenced passages; every
                        # whole-chapter rewrite risks new plot holes.
                        patched = await _director_patch_refinement_excerpts(
                            project, chapter, draft, audit, int(config["target_words"])
                        )
                        if patched != draft:
                            draft = patched
                            task = _save_director_task(
                                task,
                                f"第 {index + 1} 章：已按证据局部修订问题段落，未整章重写",
                                "success",
                            )
                        else:
                            draft = await _director_generate_prose(
                                task, project, chapter, mode="rewrite", selection=draft,
                                instruction=f"完整修订候选正文并解决以下问题：\n{requirements}\n保留未被指出的问题、人物关系、事件顺序和结尾功能，只输出完整修订稿。",
                            )
                    task["chapter_draft_checkpoint"] = {
                        "chapter": index + 1,
                        "chapter_id": chapter.get("id", ""),
                        "draft": draft[:30000],
                        "revision_attempt": attempt + 1,
                        "saved_at": utc_now(),
                    }
                    task = _save_director_task(
                        task,
                        f"第 {index + 1} 章第 {attempt + 1} 版修订稿已保存到检查点",
                        "success",
                    )
                contract_result = scan_contracts(project, chapter, draft, index + 1)
                if not contract_result.get("passed", False):
                    contract_issues = [
                        {
                            "severity": str(item.get("severity", "high")),
                            "category": "硬契约",
                            "message": str(item.get("message", "硬契约未满足")),
                            "suggestion": "按契约要求修订本章后重新扫描。",
                            "evidence": item.get("evidence", []),
                            "evidence_verified": True,
                        }
                        for item in contract_result.get("violations", [])
                        if isinstance(item, dict)
                    ]
                    audit.setdefault("issues", []).extend(contract_issues)
                    audit["score"] = min(int(audit.get("score", 0)), 74)
                    audit["verdict"] = "revise"
                passed = _refinement_candidate_passes(
                    audit, contract_result, int(config["quality_threshold"])
                )
                if not passed:
                    debt = {"chapter": index + 1, "chapter_id": chapter.get("id", ""), "title": chapter.get("title", ""), "score": int(audit.get("score", 0)), "issues": _audit_issues(audit)}
                    debts = task.setdefault("quality_debts", [])
                    debt_index = _quality_debt_index(task, str(chapter.get("id", "")))
                    same_chapter_retry = debt_index >= 0
                    if same_chapter_retry:
                        debts[debt_index] = debt
                    else:
                        debts.append(debt)
                    systemic = _systemic_quality_issues(audit)
                    if same_chapter_retry:
                        task["consecutive_quality_debts"] = 1
                        task["consecutive_systemic_debts"] = 1 if systemic else 0
                    else:
                        task["consecutive_quality_debts"] = int(task.get("consecutive_quality_debts", 0)) + 1
                        task["consecutive_systemic_debts"] = (
                            int(task.get("consecutive_systemic_debts", 0)) + 1 if systemic else 0
                        )
                    task["quality_directives"] = _quality_directives(audit)
                    task["last_rejected_candidate"] = {
                        "chapter": index + 1,
                        "chapter_id": chapter.get("id", ""),
                        "title": chapter.get("title", ""),
                        "draft": draft[:30000],
                        "audit": audit,
                        "rejected_at": utc_now(),
                    }
                    if int(task.get("consecutive_systemic_debts", 0)) >= 3 or int(task.get("consecutive_quality_debts", 0)) >= 5:
                        task["status"] = "paused"
                        task["checkpoint_message"] = (
                            f"已连续 {task.get('consecutive_quality_debts', 0)} 章未达标；"
                            "系统已触发质量熔断，当前低质量候选未写入正文。"
                        )
                        _save_director_task(
                            task,
                            "连续质量债表明规划或提示词存在系统性问题，已在写入本章前暂停；请先查看全稿体检后从检查点继续",
                            "warning",
                        )
                        return
                    if not config.get("continue_on_quality_debt", True):
                        task["status"] = "paused"
                        _save_director_task(task, f"第 {index + 1} 章连续修订后仍未达标，已暂停等待人工接管", "warning")
                        return
                else:
                    _remove_quality_debt(task, str(chapter.get("id", "")))
                    task["consecutive_quality_debts"] = 0
                    task["consecutive_systemic_debts"] = 0
                    task["quality_directives"] = []
                candidate_failures = _director_candidate_gate_failures(
                    draft, int(config["target_words"])
                )
                if candidate_failures:
                    task["status"] = "paused"
                    task["last_rejected_candidate"] = {
                        "chapter": index + 1,
                        "chapter_id": chapter.get("id", ""),
                        "title": chapter.get("title", ""),
                        "draft": draft[:30000],
                        "audit": audit,
                        "gate_failures": candidate_failures,
                        "rejected_at": utc_now(),
                    }
                    task["checkpoint_message"] = (
                        f"第 {index + 1} 章候选稿未写入正文："
                        + "；".join(candidate_failures)
                    )
                    _save_director_task(
                        task,
                        task["checkpoint_message"]
                        + "。这是正文安全门禁，需从本章检查点重新生成。",
                        "error",
                    )
                    return
                chapter["content"] = draft
                chapter["authority_state"] = "reviewed" if not passed else "accepted"
                memory: dict[str, Any] = {}
                memory_warnings: list[str] = []
                if passed:
                    if str(audit.get("verdict", "")) != "pass":
                        audit = deepcopy(audit)
                        audit["model_gate_verdict"] = str(
                            audit.get("verdict", "revise")
                        )
                        audit["verdict"] = "pass"
                        audit.setdefault("score_components", {})["threshold"] = int(
                            config["quality_threshold"]
                        )
                    lock_chapter(
                        chapter,
                        actor="auto-director",
                        audit=audit,
                        contract_scan=contract_result,
                    )
                    chapter["summary"] = local_memory_result(
                        project, chapter, draft, "等待完整记忆提取"
                    )["summary"]
                    chapter["memory_status"] = "pending"
                    chapter["execution"] = {
                        "status": "memory_pending",
                        "audit_score": int(audit.get("score", 0)),
                        "audit_verdict": str(audit.get("verdict", "pass")),
                        "contract_scan": contract_result,
                    }
                    project = store.save(
                        project["id"], project,
                        reason=f"director-chapter-{index + 1}-text-saved",
                    )
                    chapter = project["chapters"][index]
                    task = _save_director_task(
                        task, f"第 {index + 1} 章正文已写入作品，正在提取剧情记忆", "success"
                    )
                    memory = await _director_chapter_memory(project, chapter, draft)
                    memory_warnings = list(memory.get("warnings", [])) if isinstance(memory, dict) else []
                    _apply_director_memory(project, chapter, memory)
                    await _director_repair_memory(task, project, chapter)
                    chapter["memory_status"] = "committed"
                    _director_settle_hooks(task, project, chapter, index, draft, audit)
                else:
                    chapter["memory_status"] = "quarantined"
                    memory_warnings = ["未通过审校的候选稿已隔离，未回灌正式记忆"]
                    for issue in _audit_issues(audit):
                        enqueue_repair(project, chapter, issue, source="director")
                if isinstance(memory, dict) and memory.get("fallback"):
                    _record_planning_debt(
                        task,
                        {
                            "phase": "memory", "chapter": index + 1,
                            "chapter_id": chapter.get("id", ""),
                            "title": chapter.get("title", ""),
                            "issues": [str(item) for item in memory.get("warnings", []) if str(item).strip()]
                            or ["AI 记忆提取失败，已使用本地记忆回灌"],
                        },
                    )
                    _director_event(
                        task,
                        f"第 {index + 1} 章使用本地记忆回灌，正文流水线继续运行",
                        "warning",
                    )
                run_record = {
                    "status": "accepted" if passed else "quality_debt",
                    "last_run_at": utc_now(),
                    "model": str(project.get("settings", {}).get("model", "")),
                    "target_words": int(config["target_words"]),
                    "audit_score": int(audit.get("score", 0)),
                    "audit_verdict": str(audit.get("verdict", "review")),
                    "revision_attempts": int(attempt),
                    "critic_risk": audit.get("risk"),
                    "issues": _audit_issues(audit),
                    "warnings": memory_warnings,
                    "authority_state": chapter.get("authority_state", "candidate"),
                    "contract_scan": contract_result,
                }
                chapter["execution"] = run_record
                chapter.setdefault("run_history", []).append(deepcopy(run_record))
                chapter["run_history"] = chapter["run_history"][-10:]
                project = store.save(project["id"], project, reason=f"director-chapter-{index + 1}-accepted")
                task.pop("chapter_draft_checkpoint", None)
                task.pop("prose_stream_checkpoint", None)
                task.pop("current_audit", None)
                task["chapter_index"] = index + 1
                task["completed_chapters"] = index + 1
                task = _save_director_task(
                    task,
                    (
                        f"第 {index + 1} 章已锁定并完成记忆回灌"
                        if passed
                        else f"第 {index + 1} 章已进入精修队列，未污染正式记忆"
                    ),
                    "success" if passed else "warning",
                )
                if passed and index + 1 < len(chapters):
                    project = await _director_rolling_replan(task, project, index)
                if passed and craft_setting(project, "volume_review", config):
                    project = await _director_volume_review(task, project, index)
                volume_ends = {
                    int(item.get("chapter_end", 0) or 0)
                    for item in project.get("planning", {}).get("volumes", [])
                    if isinstance(item, dict)
                }
                if len(chapters) >= 10 and (
                    index + 1 in volume_ends or (index + 1) % 10 == 0
                ):
                    health = manuscript_health_report(project)
                    task.setdefault("manuscript_health_history", []).append(
                        {
                            "chapter": index + 1,
                            "score": int(health.get("score", 0)),
                            "severity": str(health.get("severity", "high")),
                            "duplicate_passage_count": int(
                                health.get("duplicate_passage_count", 0)
                            ),
                            "similar_chapter_count": len(
                                health.get("similar_chapters", [])
                            ),
                            "volume_progression_issue_count": len(
                                health.get("volume_progression_issues", [])
                            ),
                            "memory_integrity_issue_count": len(
                                health.get("memory_integrity_issues", [])
                            ),
                        }
                    )
                    task["manuscript_health_history"] = task[
                        "manuscript_health_history"
                    ][-30:]
                    hard_failures = _director_manuscript_gate_failures(
                        health,
                        final=index + 1 == len(chapters),
                        genre=str(project.get("genre", "")),
                    )
                    if hard_failures:
                        task["latest_manuscript_health"] = health
                        manuscript_debts = task.setdefault(
                            "manuscript_quality_debts", []
                        )
                        manuscript_debts.append(
                            {
                                "chapter": index + 1,
                                "score": int(health.get("score", 0)),
                                "issues": list(hard_failures),
                                "recorded_at": utc_now(),
                            }
                        )
                        task["manuscript_quality_debts"] = manuscript_debts[-30:]
                        if not config.get("continue_on_quality_debt", True):
                            task["status"] = "paused"
                            task["checkpoint_message"] = (
                                f"第 {index + 1} 章全稿门禁未通过："
                                + "；".join(hard_failures)
                                + "。本章与记忆已保留，请修订后从检查点继续。"
                            )
                            _save_director_task(
                                task,
                                task["checkpoint_message"],
                                "warning",
                            )
                            return
                        task = _save_director_task(
                            task,
                            f"截至第 {index + 1} 章的全稿检查发现："
                            + "；".join(hard_failures)
                            + (
                                "。初稿生产已完成，已进入精修队列。"
                                if index + 1 == len(chapters)
                                else "。已记录全稿质量债务，并按无人值守设置继续下一章。"
                            ),
                            "warning",
                        )
                        continue
                    task["latest_manuscript_health"] = health
                    task = _save_director_task(
                        task,
                        f"截至第 {index + 1} 章的全稿门禁通过（健康分 {health.get('score', 0)}）",
                        "success",
                    )
        final_health = manuscript_health_report(project)
        release_failures = _director_manuscript_gate_failures(
            final_health, final=True, genre=str(project.get("genre", ""))
        )
        task["latest_manuscript_health"] = final_health
        task["release_score"] = int(final_health.get("score", 0))
        task["release_failures"] = release_failures
        task["release_ready"] = not release_failures and not task.get(
            "quality_debts"
        )
        task["release_status"] = (
            "ready" if task["release_ready"] else "needs_revision"
        )
        task["phase"] = "completed"
        task["status"] = "completed"
        if task["release_ready"]:
            message = (
                f"《{project.get('title', '')}》初稿与定稿门禁均已完成，"
                f"共 {len(chapters)} 章"
            )
            kind = "success"
        else:
            message = (
                f"《{project.get('title', '')}》初稿生产完成，共 {len(chapters)} 章；"
                f"当前规则检查分 {task['release_score']}，已进入精修队列"
            )
            kind = "warning"
        _save_director_task(task, message, kind)

    ctx.register(
        _run_chapters_phase,
    )

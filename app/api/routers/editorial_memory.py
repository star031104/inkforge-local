"""Editorial routes for acceptance, contracts, repairs and memory transport."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from fastapi import HTTPException

from app.storage.schema.project import ensure_project_defaults
from app.core.clock import utc_now
from app.story.continuity_state import render_continuity_state
from app.domain.planning_validation import validate_chapter_memory_compact_result
from app.editorial_workflow import (
    begin_stage,
    complete_stage,
    enqueue_repair,
    fail_stage,
    lock_chapter,
    rebuild_repair_queue,
    reset_from_stage,
)
from app.story.evidence import draft_hash
from app.story.memory_extraction import local_memory_result
from app.manuscript_quality import manuscript_health_report
from app.story.memory import normalize_thread_status
from app.story.memory_integrity import (
    begin_memory_commit,
    get_memory_commit,
    mark_memory_commit,
    validate_memory_commit,
)
from app.must_contracts import ensure_contracts, scan_contracts
from app.prompts import CHAPTER_MEMORY_COMPACT_PROMPT
from app.services.craft_settings import craft_setting
from app.services.editorial_policy import _audit_issues
from app.story.hook_ledger import record_chapter_hook, settle_hooks
from app.story.memory_extraction import extract_staged_memory, memory_time_budget
from app.services.memory_settlement import _apply_director_memory, _chapter_number
from app.llm.errors import bounded_excerpt, planning_exception_detail, recoverable_model_error
from app.story.temporal_context import chapter_context
from app.api.schemas import (
    AcceptChapterRequest,
    ApplyMemoryRequest,
    ChapterActionRequest,
    ContractScanRequest,
    ManuscriptHealthRequest,
    MemoryCommitStatusRequest,
    ProjectRequest,
    RepairStatusRequest,
    WorkflowStageRequest,
)
from app.api.routers.editorial_support import EditorialContext


def install_memory_routes(ctx: EditorialContext) -> None:
    """Acceptance, contracts, workflow reset, memory extraction and repair routes."""
    router = ctx.router
    store = ctx.store
    find_chapter = ctx.find_chapter
    _persist_managed_project = ctx._persist_managed_project
    _latest_managed_commit_project = ctx._latest_managed_commit_project
    _record_chapter_session_turn = ctx._record_chapter_session_turn
    _require_locked_memory_source = ctx._require_locked_memory_source
    structured_completion = ctx.structured_completion
    _director_event = ctx._director_event
    _remove_quality_debt = ctx._remove_quality_debt
    repair_incomplete_memory = ctx.repair_incomplete_memory

    @router.post("/api/chapter/accept")
    async def chapter_accept(body: AcceptChapterRequest) -> dict[str, Any]:
        """Accept prose, optionally lock it, and only then open a memory commit."""
        project = ensure_project_defaults(body.project)
        index, chapter = find_chapter(project, body.chapter_id)
        if len(str(chapter.get("content", "")).strip()) < 100:
            raise HTTPException(400, "接受的章节正文至少需要 100 字")
        begin_stage(chapter, "accept", {"content": chapter.get("content", ""), "lock": body.lock})
        chapter["authority_state"] = "accepted"
        complete_stage(chapter, "accept", {"authority_state": "accepted"})
        if craft_setting(project, "hook_ledger"):
            # Hook ledger: judge earlier hooks against the accepted prose, then
            # record this chapter's own planned ending hook as a new promise.
            audit = body.audit if isinstance(body.audit, dict) else {}
            settle_hooks(project, index + 1, str(chapter.get("content", "")), audit.get("hook_status"))
            record_chapter_hook(
                project, chapter, index + 1, window=int(craft_setting(project, "hook_window"))
            )
        contract_result = body.contract_scan or scan_contracts(
            project, chapter, str(chapter.get("content", "")), index + 1
        )
        begin_stage(chapter, "contract_scan", {"content": chapter.get("content", ""), "contracts": project.get("must_contracts", [])})
        complete_stage(chapter, "contract_scan", contract_result)
        commit: dict[str, Any] = {}
        reused = False
        if body.lock:
            begin_stage(chapter, "lock", {"content": chapter.get("content", ""), "audit": body.audit, "contract_scan": contract_result})
            try:
                receipt = lock_chapter(
                    chapter,
                    actor="editor",
                    audit=body.audit,
                    contract_scan=contract_result,
                    force=body.force,
                )
            except ValueError as exc:
                fail_stage(chapter, "lock", exc)
                for violation in contract_result.get("violations", []):
                    if isinstance(violation, dict):
                        enqueue_repair(project, chapter, violation, source="contract")
                raise HTTPException(409, str(exc)) from exc
            commit, reused = begin_memory_commit(project, chapter)
        else:
            receipt = {}
        execution = chapter.get("execution", {})
        if isinstance(execution, dict) and body.audit:
            execution["audit_score"] = int(body.audit.get("score", 0) or 0)
            execution["audit_verdict"] = str(body.audit.get("verdict", "review"))
            execution["issues"] = _audit_issues(body.audit)
            execution["last_run_at"] = utc_now()
        if isinstance(execution, dict) and execution.get("status") == "quality_debt":
            execution["status"] = "accepted"
            execution["accepted_by"] = "editor"
            execution["accepted_at"] = utc_now()
        project, persisted = _persist_managed_project(
            project, "locked-pending-memory" if body.lock else "accepted-not-locked"
        )
        persisted_commit = get_memory_commit(project, str(commit.get("id", ""))) or commit if commit else {}
        # A manual editorial acceptance resolves the prose-quality debt that caused
        # a paused director checkpoint.  Without resetting these consecutive
        # counters, the next new chapter can trip the systemic breaker using stale
        # failures from chapters the editor has already replaced and accepted.
        director_task = store.latest_director_task(str(project.get("id", "")))
        if (
            not reused
            and director_task
            and director_task.get("task_type") != "incubation"
            and director_task.get("status") not in {"queued", "running"}
        ):
            director_task["consecutive_quality_debts"] = 0
            director_task["consecutive_systemic_debts"] = 0
            director_task["quality_directives"] = []
            director_task["checkpoint_message"] = ""
            _remove_quality_debt(director_task, str(chapter.get("id", "")))
            rejected = director_task.get("last_rejected_candidate", {})
            if isinstance(rejected, dict) and str(rejected.get("chapter_id", "")) == str(
                chapter.get("id", "")
            ):
                director_task["last_rejected_candidate"] = {}
            _director_event(
                director_task,
                f"第 {_chapter_number(project, chapter)} 章已由编辑人工接纳，连续质量债计数已清零",
                "success",
            )
            store.save_director_task(director_task["id"], director_task)
        return {
            "project": project,
            "commit": persisted_commit,
            "lock_receipt": receipt,
            "contract_scan": contract_result,
            "reused": reused,
            "persisted": persisted,
        }


    @router.post("/api/chapter/contracts/scan")
    async def chapter_contract_scan(body: ContractScanRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        index, chapter = find_chapter(project, body.chapter_id)
        text = body.draft.strip() or str(chapter.get("content", ""))
        result = scan_contracts(project, chapter, text, index + 1)
        begin_stage(chapter, "contract_scan", {"content": text, "contracts": project.get("must_contracts", [])})
        complete_stage(chapter, "contract_scan", result)
        return result


    @router.post("/api/project/contracts/save")
    async def project_contracts_save(body: ProjectRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        ensure_contracts(project)
        project, persisted = _persist_managed_project(project, "must-contracts-updated")
        return {"project": project, "contracts": project.get("must_contracts", []), "persisted": persisted}


    @router.post("/api/chapter/workflow/reset")
    async def chapter_workflow_reset(body: WorkflowStageRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        _, chapter = find_chapter(project, body.chapter_id)
        try:
            reset = reset_from_stage(chapter, body.stage)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        project, persisted = _persist_managed_project(project, f"workflow-reset-{body.stage}")
        return {"project": project, "chapter_id": body.chapter_id, "reset": reset, "persisted": persisted}


    def _repair_base(project: dict[str, Any], chapter_id: str, content_hash: str) -> dict[str, Any]:
        """Latest server copy of ``project`` for a memory repair (browser copy if unmanaged).

        Like ``_latest_managed_commit_project``: extraction takes minutes, so
        the repair is applied to the newest server project instead of the
        browser snapshot; the chapter's content hash guards the prose.
        """
        project_id = str(project.get("id", "")).strip()
        latest = store.get(project_id) if project_id else None
        if latest is None:
            return project
        latest = ensure_project_defaults(deepcopy(latest))
        _index, chapter = find_chapter(latest, chapter_id)
        if draft_hash(str(chapter.get("content", ""))) != content_hash:
            raise HTTPException(409, "该章正文已在服务器上更新，请刷新作品后再补提取记忆")
        return latest

    @router.post("/api/chapter/memory/repair")
    async def chapter_memory_repair(body: ChapterActionRequest) -> dict[str, Any]:
        """Re-extract only the memory stages that failed for one chapter."""
        project = ensure_project_defaults(body.project)
        _index, chapter = find_chapter(project, body.chapter_id)
        content_hash = draft_hash(str(chapter.get("content", "")))
        base = _repair_base(project, body.chapter_id, content_hash)
        recorded: dict[str, Any] = {}

        def stage_key(messages: list[dict[str, str]]) -> str:
            return str(messages[-1].get("content", "")).split("\n", 1)[0] if messages else ""

        async def recording(settings: dict[str, Any], messages: list[dict[str, str]], **kwargs: Any) -> Any:
            output = await structured_completion(settings, messages, **kwargs)
            recorded[stage_key(messages)] = deepcopy(output)
            return output

        async def replay(settings: dict[str, Any], messages: list[dict[str, str]], **kwargs: Any) -> Any:
            key = stage_key(messages)
            if key not in recorded:
                raise ValueError(f"{key or '记忆阶段'}未在本次补提取中完成")
            return deepcopy(recorded[key])

        try:
            working = deepcopy(base)
            with memory_time_budget(150):
                report = await repair_incomplete_memory(working, body.chapter_id, completion=recording)
            latest = _repair_base(project, body.chapter_id, content_hash)
            if latest is not base and str(latest.get("updated_at", "")) != str(base.get("updated_at", "")):
                # The project changed while the model was extracting: settle the
                # recorded stage outputs again onto the newest copy (no new calls).
                working = deepcopy(latest)
                report = await repair_incomplete_memory(working, body.chapter_id, completion=replay)
        except KeyError as exc:
            raise HTTPException(404, "章节不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            if recoverable_model_error(exc):
                raise HTTPException(502, f"记忆补提取失败：{planning_exception_detail(exc)}") from exc
            raise
        project, persisted = _persist_managed_project(working, "memory-stage-repair")
        return {"project": project, "chapter_id": body.chapter_id, "report": report, "persisted": persisted}


    @router.post("/api/project/repairs/rebuild")
    async def project_repairs_rebuild(body: ProjectRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        queue = rebuild_repair_queue(project)
        project, persisted = _persist_managed_project(project, "repair-queue-rebuilt")
        return {"project": project, "queue": queue, "persisted": persisted}


    @router.post("/api/project/repairs/status")
    async def project_repair_status(body: RepairStatusRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        valid = {"queued", "in_progress", "resolved", "dismissed"}
        if body.status not in valid:
            raise HTTPException(400, "精修任务状态无效")
        queue = rebuild_repair_queue(project)
        task = next((item for item in queue if str(item.get("id", "")) == body.task_id), None)
        if not task:
            raise HTTPException(404, "精修任务不存在")
        task["status"] = body.status
        task["updated_at"] = utc_now()
        if body.status == "in_progress":
            task["attempts"] = int(task.get("attempts", 0) or 0) + 1
        project, persisted = _persist_managed_project(project, f"repair-{body.status}")
        return {"project": project, "task": task, "persisted": persisted}


    @router.post("/api/chapter/memory")
    async def chapter_memory(body: ChapterActionRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        if body.commit_id:
            _, incoming_chapter = find_chapter(project, body.chapter_id)
            try:
                validate_memory_commit(project, incoming_chapter, body.commit_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        project = _latest_managed_commit_project(project, body.commit_id)
        chapter_index, chapter = find_chapter(project, body.chapter_id)
        _require_locked_memory_source(project, chapter, body.commit_id)
        begin_stage(chapter, "memory_extract", {"content": chapter.get("content", ""), "commit_id": body.commit_id})
        if body.commit_id:
            try:
                validate_memory_commit(project, chapter, body.commit_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

        def extracted(result: dict[str, Any]) -> dict[str, Any]:
            complete_stage(chapter, "memory_extract", result)
            _record_chapter_session_turn(
                project, chapter, "assistant", json.dumps(result, ensure_ascii=False), "memory-extract"
            )
            if body.commit_id:
                current = get_memory_commit(project, body.commit_id)
                if current and current.get("status") != "committed":
                    mark_memory_commit(
                        project,
                        chapter,
                        body.commit_id,
                        "settlement_extracted",
                        warnings=list(result.get("warnings", [])),
                    )
                    _persist_managed_project(project, "accepted-memory-extracted")
            return result

        content = body.draft.strip() or chapter.get("content", "").strip()
        if len(content) < 100:
            raise HTTPException(400, "章节正文至少需要 100 字")
        prior = chapter_context(project, chapter_index)
        memory = prior.get("memory", {})
        active_threads = [
            item for item in memory.get("plot_threads", [])
            if isinstance(item, dict)
            and normalize_thread_status(item.get("status")) != "closed"
        ]
        context = f"""【提取协议】
    当前是第 {chapter_index + 1} 章。下面“本章正文”是唯一事件证据；计划、旧状态和线索表只用于识别增量，绝不能当成本章已经发生。
    【已有全书滚动进展】
    {memory.get('story_so_far', '')}
    【章前人物权威状态】
    {bounded_excerpt(json.dumps(prior.get('characters', []), ensure_ascii=False), 9000)}
    【章前权威事实（更新事实时保留来源，替换时填写 supersedes_id）】
    {bounded_excerpt(json.dumps(memory.get('facts', [])[-80:], ensure_ascii=False), 7000)}
    【章前开放线索（更新时必须复用 id，不得换名复制）】
    {bounded_excerpt(json.dumps(active_threads[-50:], ensure_ascii=False), 9000)}
    【章前动态关系】
    {bounded_excerpt(json.dumps(memory.get('relationships', [])[-50:], ensure_ascii=False), 5000)}
    【当前章节】
    章节：{chapter.get('title', '')}
    已有角色：{', '.join(item.get('name','') for item in project.get('characters', []))}
    本章计划：{json.dumps(chapter.get('plan', {}), ensure_ascii=False)}
    本章正文：
    {bounded_excerpt(content, 18000)}"""
        # Staged extraction: the chapter text is an identical shared prefix and
        # each stage only receives the prior state it needs.
        shared_context = f"""【提取协议】
    当前是第 {chapter_index + 1} 章。下面“本章正文”是唯一事件证据；计划、旧状态和线索表只用于识别增量，绝不能当成本章已经发生。
    【当前章节】
    章节：{chapter.get('title', '')}
    已有角色：{', '.join(item.get('name','') for item in project.get('characters', []))}
    本章计划：{json.dumps(chapter.get('plan', {}), ensure_ascii=False)}
    本章正文：
    {bounded_excerpt(content, 18000)}"""
        state_blocks = {
            "story_so_far": f"【已有全书滚动进展】\n{memory.get('story_so_far', '')}",
            "characters": "【章前人物权威状态】\n" + bounded_excerpt(
                json.dumps(prior.get('characters', []), ensure_ascii=False), 9000
            ),
            "facts": "【章前权威事实（更新事实时保留来源，替换时填写 supersedes_id）】\n" + bounded_excerpt(
                json.dumps(memory.get('facts', [])[-80:], ensure_ascii=False), 7000
            ),
            "plot_threads": "【章前开放线索（更新时必须复用 id，不得换名复制）】\n" + bounded_excerpt(
                json.dumps(active_threads[-50:], ensure_ascii=False), 9000
            ),
            "relationships": "【章前动态关系】\n" + bounded_excerpt(
                json.dumps(memory.get('relationships', [])[-50:], ensure_ascii=False), 5000
            ),
            "objects": "【章前道具与伤势状态】\n" + (
                render_continuity_state(prior, chapter_index) or "暂无记录"
            ),
        }
        try:
            result = await extract_staged_memory(
                project.get("settings", {}),
                shared_context,
                state_blocks,
                completion=structured_completion,
            )
            return extracted(result)
        except Exception as exc:
            if recoverable_model_error(exc):
                primary_reason = planning_exception_detail(exc)
                try:
                    compact, compact_warnings = await structured_completion(
                        project.get("settings", {}),
                        [
                            {"role": "system", "content": CHAPTER_MEMORY_COMPACT_PROMPT},
                            {"role": "user", "content": context},
                        ],
                        temperature=0.1,
                        max_tokens=850,
                        timeout_seconds=180,
                        validate=validate_chapter_memory_compact_result,
                        token_ceiling=1050,
                        workload="extraction",
                    )
                    compact["summary"] = str(compact.get("summary", ""))
                    if compact.get("story_so_far"):
                        compact["story_so_far"] = str(compact["story_so_far"])[:4000]
                    else:
                        old_story = str(memory.get("story_so_far", "")).strip()
                        addition = f"{chapter.get('title', '本章')}：{compact['summary']}"
                        compact["story_so_far"] = "\n".join(
                            item for item in (old_story, addition) if item
                        )[-4000:]
                    for key in (
                        "character_updates", "facts", "plot_threads", "timeline",
                        "relationship_updates", "continuity_notes", "description_updates",
                    ):
                        if not isinstance(compact.get(key), list):
                            compact[key] = []
                    if not isinstance(compact.get("scene_settlement"), dict):
                        compact["scene_settlement"] = {}
                    compact["fallback"] = False
                    compact["memory_mode"] = "compact_recovery"
                    compact["warnings"] = [
                        f"分阶段记忆提取的核心阶段未通过，已用精简AI记忆恢复：{primary_reason}"
                    ] + compact_warnings
                    return extracted(compact)
                except Exception as compact_exc:
                    return extracted(
                        local_memory_result(
                            project,
                            chapter,
                            content,
                            f"完整结构：{primary_reason}；精简恢复：{planning_exception_detail(compact_exc)}",
                        )
                    )
            raise HTTPException(
                502, f"记忆提取失败：{planning_exception_detail(exc)}"
            ) from exc

    ctx.register(
        chapter_accept,
        chapter_contract_scan,
        project_contracts_save,
        chapter_workflow_reset,
        _repair_base,
        chapter_memory_repair,
        project_repairs_rebuild,
        project_repair_status,
        chapter_memory,
    )


def install_memory_state_routes(ctx: EditorialContext) -> None:
    """Manuscript health and memory-commit apply/degrade routes."""
    router = ctx.router
    find_chapter = ctx.find_chapter
    _persist_managed_project = ctx._persist_managed_project
    _latest_managed_commit_project = ctx._latest_managed_commit_project
    _require_locked_memory_source = ctx._require_locked_memory_source

    @router.post("/api/project/manuscript-health")
    async def project_manuscript_health(body: ManuscriptHealthRequest) -> dict[str, Any]:
        """Deterministic whole-book checks; no model call and safe for unsaved projects."""
        return manuscript_health_report(ensure_project_defaults(body.project))


    @router.post("/api/chapter/memory/apply")
    async def chapter_memory_apply(body: ApplyMemoryRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        if body.commit_id:
            _, incoming_target = find_chapter(project, body.chapter_id)
            try:
                validate_memory_commit(project, incoming_target, body.commit_id)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        project = _latest_managed_commit_project(project, body.commit_id)
        _, target = find_chapter(project, body.chapter_id)
        _require_locked_memory_source(project, target, body.commit_id)
        begin_stage(target, "memory_apply", {"commit_id": body.commit_id, "result": body.result})
        if body.commit_id:
            try:
                commit = validate_memory_commit(
                    project, target, body.commit_id
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            if commit.get("status") == "committed":
                project, persisted = _persist_managed_project(
                    project, "memory-idempotent-replay"
                )
                return {
                    "project": project,
                    "warnings": list(commit.get("warnings", [])),
                    "commit": commit,
                    "persisted": persisted,
                    "idempotent": True,
                }

        working = ensure_project_defaults(deepcopy(project))
        _, working_target = find_chapter(working, body.chapter_id)
        try:
            warnings = _apply_director_memory(working, working_target, body.result)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        complete_stage(working_target, "memory_apply", {"warnings": warnings})
        commit = None
        if body.commit_id:
            try:
                commit = mark_memory_commit(
                    working,
                    working_target,
                    body.commit_id,
                    "committed",
                    warnings=warnings,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
        working, persisted = _persist_managed_project(
            working, "accepted-with-memory"
        )
        if commit:
            commit = get_memory_commit(working, body.commit_id) or commit
        return {
            "project": ensure_project_defaults(working),
            "warnings": warnings,
            "commit": commit,
            "persisted": persisted,
            "idempotent": False,
        }


    @router.post("/api/chapter/memory/degrade")
    async def chapter_memory_degrade(
        body: MemoryCommitStatusRequest,
    ) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        _, incoming_target = find_chapter(project, body.chapter_id)
        try:
            validate_memory_commit(project, incoming_target, body.commit_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        project = _latest_managed_commit_project(project, body.commit_id)
        _, target = find_chapter(project, body.chapter_id)
        try:
            current = validate_memory_commit(project, target, body.commit_id)
            if current.get("status") != "committed":
                mark_memory_commit(
                    project,
                    target,
                    body.commit_id,
                    "state_degraded",
                    error=body.error or "记忆提取或应用失败",
                )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        project, persisted = _persist_managed_project(
            project, "accepted-state-degraded"
        )
        return {
            "project": project,
            "commit": get_memory_commit(project, body.commit_id),
            "persisted": persisted,
        }

    ctx.register(
        project_manuscript_health,
        chapter_memory_apply,
        chapter_memory_degrade,
    )

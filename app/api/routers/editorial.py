"""Chapter acceptance, audit, repair, contracts, and memory transport."""
from __future__ import annotations

import json
import re
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from ...chapter_session import prepare_messages
from ...db import ensure_project_defaults, utc_now
from ...domain.planning_validation import (
    validate_audit_result,
    validate_chapter_memory_compact_result,
    validate_chapter_memory_result,
)
from ...editorial_workflow import (
    begin_stage,
    complete_stage,
    enqueue_repair,
    fail_stage,
    lock_chapter,
    rebuild_repair_queue,
    reset_from_stage,
)
from ...services.memory_extraction import extract_staged_memory, memory_time_budget
from ...services.craft_settings import craft_setting
from ...domain.json_schemas import AUDIT_SCHEMA
from ...services.focused_critics import (
    choose_budget,
    has_previous_run,
    run_focused_critics,
    score_critic_risk,
)
from ...services.antislop import banned_phrases, detect_slop
from ...services.experience_bank import record_audit_issues
from ...providers import settings_for_workload
from ...services.candidate_selection import is_key_chapter
from ...services.hook_ledger import (
    due_hooks,
    hook_ending_check,
    planned_hook,
    record_chapter_hook,
    render_hooks_for_audit,
    settle_hooks,
)
from ...domain.continuity_state import (
    check_continuity_draft,
    injury_states,
    item_states,
    render_continuity_state,
)
from ...evidence_audit import PENALTIES, draft_hash, normalize_model_score, number_draft, stable_audit_result
from ...fallbacks import local_audit_result, local_memory_result
from ...manuscript_quality import jargon_policy, manuscript_health_report, prior_manuscript_text
from ...memory import (
    normalize_thread_status,
    render_memories,
    render_thread_agenda,
    retrieve_memories,
    select_thread_agenda,
)
from ...memory_integrity import (
    begin_memory_commit,
    get_memory_commit,
    mark_memory_commit,
    validate_memory_commit,
)
from ...must_contracts import ensure_contracts, scan_contracts
from ...narrative_policy import route_guidance
from ...prompts import (
    AUDIT_PROMPT,
    CHAPTER_MEMORY_COMPACT_PROMPT,
    CHAPTER_MEMORY_PROMPT,
    render_epistemic_context,
)
from ...quality import local_quality_check
from ...services.editorial_policy import _audit_issues
from ...services.memory_settlement import _apply_director_memory, _chapter_number, repair_incomplete_memory
from ...services.model_errors import (
    bounded_excerpt,
    planning_exception_detail,
    recoverable_model_error,
)
from ...temporal_context import chapter_context
from ..schemas import (
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


class _StoreProxy:
    def __init__(self, provider: Callable[[], Any]):
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider(), name)


class _LiveCallable:
    def __init__(self, provider: Callable[[], Callable[..., Any]]):
        self._provider = provider

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._provider()(*args, **kwargs)


@dataclass(frozen=True)
class EditorialRoutes:
    router: APIRouter
    chapter_memory: Callable[..., Any]
    chapter_audit: Callable[..., Any]


_NAMED_ENTITY_RES = (
    # "一个名叫阿七的小厮": introduction phrases name an entity directly.
    re.compile(r"(?:名叫|叫做|名为|唤作|自称|人称|号称)([\u4e00-\u9fff]{2,4})"),
    # "，沈鸢笑道": a clause-initial speaker tag (verified below, it over-matches).
    re.compile(r"(?:^|[。！？\n“”，])([\u4e00-\u9fff]{2,3})(?:说道|问道|答道|笑道|喝道)"),
)
_GENERIC_ROLES = {
    "少年", "少女", "老人", "男人", "女人", "众人", "那人", "此人", "对方", "掌柜", "小二", "侍卫",
    "他们", "她们", "我们", "你们", "老者", "来人", "旁人", "有人",
}
# A speaker tag starting with a pronoun is "她冷笑道 / 他沉声喝道", not a name.
_PRONOUN_STARTS = tuple("他她它我你您这那谁其众")
# Function characters that end a captured name ("阿七的小厮" → "阿七").
_NAME_STOP = re.compile(r"[的了着是在和与将把被向对从也都便就又却]")


def _clean_entity(name: str) -> str:
    return _NAME_STOP.split(name, maxsplit=1)[0]


def new_named_entities(project: dict[str, Any], draft: str, known_text: str = "") -> list[str]:
    """Names introduced by the draft that no character, lore entry or memory knows.

    Speaker tags only count when the name also recurs elsewhere in the draft
    and does not start with a pronoun, so ordinary dialogue ("她冷笑道")
    never looks like a new character.
    """
    known_parts = [known_text]
    for character in project.get("characters", []) or []:
        if isinstance(character, dict):
            known_parts.append(str(character.get("name", "")))
            known_parts.append(json.dumps(character.get("aliases", ""), ensure_ascii=False))
    for entry in project.get("world_entries", []) or []:
        if isinstance(entry, dict):
            known_parts.append(str(entry.get("title", "")) + json.dumps(entry.get("keys", ""), ensure_ascii=False))
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    known_parts.extend(str(item.get("text", "")) for item in memory.get("facts", []) if isinstance(item, dict))
    known = "\n".join(known_parts)
    found: list[str] = []
    for position, pattern in enumerate(_NAMED_ENTITY_RES):
        for match in pattern.finditer(draft):
            name = _clean_entity(match.group(1))
            if len(name) < 2 or name in found or name in known:
                continue
            if name in _GENERIC_ROLES or any(name.endswith(role) for role in _GENERIC_ROLES):
                continue
            if position == 1 and (name.startswith(_PRONOUN_STARTS) or draft.count(name) < 2):
                continue
            found.append(name)
    return found[:8]


def _audit_attempts(chapter: dict[str, Any]) -> int:
    """Persisted audit attempts (survives restarts, unlike the in-process run memory)."""
    workflow = chapter.get("workflow", {}) if isinstance(chapter.get("workflow"), dict) else {}
    stages = workflow.get("stages", {}) if isinstance(workflow.get("stages"), dict) else {}
    audit = stages.get("audit", {}) if isinstance(stages.get("audit"), dict) else {}
    return int(audit.get("attempts", 0) or 0)


def critic_risk_signals(
    project: dict[str, Any],
    index: int,
    chapter: dict[str, Any],
    draft: str,
    *,
    local_checks: dict[str, Any],
    continuity_issues: list[dict[str, Any]],
    agenda: list[dict[str, Any]],
    hooks: list[dict[str, Any]],
    audit_key: str,
) -> dict[str, Any]:
    """Cheap deterministic signals for the risk-gated critic budget."""
    number = index + 1
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    tracked = [name for name in item_states(memory, number) if name and name in draft]
    tracked += [
        str(state.get("character", ""))
        for state in injury_states(memory, number).values()
        if state.get("state") in {"fresh", "healing"} and str(state.get("character", "")) in draft
    ]
    continuity_keys = {id(item) for item in continuity_issues}
    mechanical = [
        item for item in local_checks.get("issues", []) or []
        if isinstance(item, dict) and id(item) not in continuity_keys
        and str(item.get("severity", "")).lower() in {"high", "medium"}
    ]
    slop_excess = sum(
        item["count"] - item["allow"] for item in detect_slop(draft, banned_phrases(project))
    )
    overdue_threads = sum(
        1 for item in agenda
        if isinstance(item, dict) and isinstance(item.get("_lifecycle"), dict) and item["_lifecycle"].get("stale")
    )
    execution = chapter.get("execution", {}) if isinstance(chapter.get("execution"), dict) else {}
    prior_debt = str(execution.get("status", "")) == "quality_debt" or any(
        isinstance(item, dict) and item.get("chapter_id") == chapter.get("id")
        and item.get("status") in {"queued", "in_progress"}
        for item in project.get("repair_queue", []) or []
    )
    route_text = json.dumps(
        {"route": chapter.get("route", {}), "plan": chapter.get("plan", {})}, ensure_ascii=False
    )
    return {
        "key_chapter": is_key_chapter(chapter, index, len(project.get("chapters", []))),
        "new_entities": new_named_entities(project, draft, route_text),
        "continuity_mentions": len(tracked),
        "continuity_conflicts": len(continuity_issues),
        "mechanical_issues": len(mechanical),
        "slop_excess": slop_excess,
        # Only overdue obligations add risk: every chapter has the previous
        # chapter's fresh hook in its window, which is routine, not risky.
        "obligations": overdue_threads + sum(
            1 for item in hooks if isinstance(item, dict) and item.get("status") == "debt"
        ),
        "prior_debt": prior_debt,
        "first_audit": not has_previous_run(audit_key) and _audit_attempts(chapter) <= 1,
    }


def create_editorial_routes(
    *,
    store_provider: Callable[[], Any],
    find_chapter_callback: Callable[..., Any],
    indexed_retrieval_callback: Callable[..., Any],
    persist_managed_project_callback: Callable[..., Any],
    latest_managed_commit_callback: Callable[..., Any],
    record_session_turn_callback: Callable[..., Any],
    require_locked_source_callback: Callable[..., Any],
    quality_context_callback: Callable[..., Any],
    quality_source_tail_callback: Callable[..., Any],
    structured_completion_callback: Callable[..., Any],
    director_event_provider: Callable[[], Callable[..., Any]],
    remove_quality_debt_provider: Callable[[], Callable[..., Any]],
) -> EditorialRoutes:
    """Create editorial routes with live access to director bookkeeping."""
    router = APIRouter(tags=["editorial"])
    store = _StoreProxy(store_provider)
    find_chapter = find_chapter_callback
    _indexed_retrieval_hits = indexed_retrieval_callback
    _persist_managed_project = persist_managed_project_callback
    _latest_managed_commit_project = latest_managed_commit_callback
    _record_chapter_session_turn = record_session_turn_callback
    _require_locked_memory_source = require_locked_source_callback
    local_quality_known_context = quality_context_callback
    quality_source_tail = quality_source_tail_callback
    structured_completion = structured_completion_callback
    _director_event = _LiveCallable(director_event_provider)
    _remove_quality_debt = _LiveCallable(remove_quality_debt_provider)

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
                normalized_score = max(
                    0,
                    100 - sum(
                        PENALTIES.get(str(item.get("severity", "medium")).lower(), 8)
                        for item in raw_issues
                        if isinstance(item, dict)
                    ),
                ) if raw_issues else 90
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
            result = stable_audit_result(draft, result, local_checks, threshold=85)
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
                fallback = stable_audit_result(draft, fallback, local_checks, threshold=85)
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

    return EditorialRoutes(
        router=router,
        chapter_memory=chapter_memory,
        chapter_audit=chapter_audit,
    )

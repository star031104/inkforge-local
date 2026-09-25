from __future__ import annotations

from . import model_telemetry
from .narrative_policy import profile as narrative_profile, route_guidance
from .workspace_api import create_workspace_router
import hashlib
import asyncio
import json
import math
import re
from contextlib import asynccontextmanager
from collections import Counter
from copy import deepcopy
from difflib import SequenceMatcher
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from .db import ProjectStore, ensure_project_defaults, utc_now
from .fallbacks import (
    local_audit_result,
    local_ideas,
    local_memory_result,
)
from .llama_client import chat_once, validate_context_budget
from .memory import (
    normalize_thread_status,
    render_memories,
    render_thread_agenda,
    retrieve_memories,
    select_thread_agenda,
)
from .planning import (
    DIRECTOR_MASTER_BIBLE_PROMPT,
    DIRECTOR_CHAPTER_ROUTE_PROMPT,
    DIRECTOR_VOLUME_CONTRACT_PROMPT,
    DIRECTOR_VOLUME_CORE_PROMPT,
    DIRECTOR_VOLUME_DETAILS_PROMPT,
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
from .prompts import (
    AUDIT_PROMPT,
    CHAPTER_MEMORY_COMPACT_PROMPT,
    CHAPTER_MEMORY_PROMPT,
    CHAPTER_PLAN_PROMPT,
    DIRECTOR_CAST_PROMPT,
    DIRECTOR_CHARACTER_CARD_PROMPT,
    DIRECTOR_SEED_BRIEF_PROMPT,
    DIRECTOR_WORLD_PROMPT,
    IDEAS_PROMPT,
    INCUBATOR_ASSETS_PROMPT,
    INCUBATOR_PROMPT,
    build_prompt,
    build_retrieval_query,
    estimate_tokens,
    render_epistemic_context,
)
from .quality import local_quality_check
from .manuscript_quality import manuscript_health_report, prior_manuscript_text
from .providers import settings_for_workload
from .memory_integrity import (
    begin_memory_commit,
    get_memory_commit,
    mark_memory_commit,
    validate_memory_commit,
)
from .professional_api import create_professional_router
from .chapter_session import add_turn, new_session, prepare_messages
from .evidence_audit import stable_audit_result
from .editorial_workflow import (
    begin_stage,
    complete_stage,
    enqueue_repair,
    fail_stage,
    lock_chapter,
    rebuild_repair_queue,
    reset_from_stage,
)
from .must_contracts import ensure_contracts, scan_contracts
from .project_service import ProjectConflictError
from .temporal_context import chapter_context
from .core.config import APP_VERSION, settings as app_settings
from .infrastructure.instance_lock import ApplicationInstanceLock
from .infrastructure.backup_location import BackupLocation
from .infrastructure.automatic_backup import AutomaticBackupService
from .api.schemas import (
    AcceptChapterRequest, ApplyMemoryRequest, ApplyVolumeRequest,
    ChapterActionRequest, ContractScanRequest,
    IdeasRequest, IncubatorRequest, ManuscriptHealthRequest,
    MemoryCommitStatusRequest, PlanningRequest, ProjectRequest,
    RepairStatusRequest, WorkflowStageRequest,
)
from .api.routers.projects import create_projects_router
from .api.routers.system import create_system_router
from .api.routers.research import create_research_router
from .api.routers.prompts import create_prompt_router
from .api.routers.chapter_sessions import create_chapter_session_router
from .api.routers.director import create_director_router
from .api.routers.generation import create_generation_router
from .services.structured_output import (
    parse_json_response,
    require_schema,
    structured_completion as run_structured_completion,
)
from .services.model_errors import (
    bounded_excerpt,
    planning_exception_detail,
    recoverable_model_error,
)
from .services.memory_settlement import _apply_director_memory, _chapter_number
from .services.editorial_policy import (
    _audit_issues,
    _refinement_candidate_passes,
)
from .services.director_runtime import DirectorDependencies, create_director_runtime
from .domain.planning_validation import (
    _director_chapter_core_forbidden_terms,
    _director_chapter_forbidden_terms,
    _director_chapter_seed,
    _director_stage_event_terms,
    _director_stage_forbidden_terms,
    _director_stage_location_terms,
    _validate_director_stage_boundary,
    validate_asset_authority,
    validate_audit_result,
    validate_chapter_memory_compact_result,
    validate_chapter_memory_result,
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
    validate_incubator_result,
    validate_master_core,
    validate_master_result,
    validate_route_batch,
    validate_volume_blueprints,
)
from .domain.prose import (
    _effective_prose_settings,
    _prose_char_count,
)
from .domain.route_validation import (
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


ROOT = app_settings.root
store = ProjectStore(app_settings.database_path, app_settings.secret_path)
backup_location = BackupLocation(
    app_settings.backup_path,
    app_settings.database_path,
    app_settings.database_path.with_name("inkforge-settings.json"),
)
model_telemetry.configure(app_settings.telemetry_path)
instance_lock = ApplicationInstanceLock(
    app_settings.database_path.with_suffix(app_settings.database_path.suffix + ".lock")
)
automatic_backups = AutomaticBackupService(
    lambda: store,
    lambda: backup_location.path,
)


@asynccontextmanager
async def application_lifespan(_app: FastAPI):
    instance_lock.acquire()
    store.clear_director_task_leases()
    backup_stop = asyncio.Event()
    backup_task = asyncio.create_task(automatic_backups.run(backup_stop))
    try:
        yield
    finally:
        backup_stop.set()
        await backup_task
        instance_lock.release()


app = FastAPI(
    title="InkForge Local API", version=APP_VERSION, lifespan=application_lifespan
)
app.mount("/assets", StaticFiles(directory=app_settings.static_path), name="assets")
app.include_router(create_workspace_router(lambda: store))
app.include_router(
    create_projects_router(
        lambda: store,
        backup_location,
        lambda: director_runners,
    )
)
app.include_router(create_research_router())


async def structured_completion(
    settings: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    timeout_seconds: float,
    temperature: float,
    validate: Callable[[dict[str, Any]], None] | None = None,
    token_ceiling: int | None = None,
    workload: str = "planning",
) -> tuple[dict[str, Any], list[str]]:
    """Compatibility facade while workflows migrate into dedicated services."""
    return await run_structured_completion(
        settings,
        messages,
        max_tokens=max_tokens,
        timeout_seconds=timeout_seconds,
        temperature=temperature,
        validate=validate,
        token_ceiling=token_ceiling,
        workload=workload,
        error_detail=planning_exception_detail,
    )


app.include_router(create_professional_router(lambda: store, structured_completion))


def find_chapter(project: dict[str, Any], chapter_id: str) -> tuple[int, dict[str, Any]]:
    chapters = project.get("chapters", [])
    for index, chapter in enumerate(chapters):
        if chapter.get("id") == chapter_id:
            return index, chapter
    raise HTTPException(404, "章节不存在")


def _get_or_create_chapter_session(
    project: dict[str, Any], chapter: dict[str, Any]
) -> dict[str, Any]:
    project_id = str(project.get("id", ""))
    existing = (
        store.get_chapter_session(project_id, str(chapter.get("id", "")))
        if project_id and store.get(project_id)
        else None
    )
    return new_session(project, chapter, existing)


app.include_router(
    create_chapter_session_router(
        lambda: store,
        find_chapter,
        _get_or_create_chapter_session,
    )
)


def _record_chapter_session_turn(
    project: dict[str, Any],
    chapter: dict[str, Any],
    role: str,
    content: str,
    kind: str,
) -> dict[str, Any]:
    session = _get_or_create_chapter_session(project, chapter)
    add_turn(session, role, content, kind)
    project_id = str(project.get("id", ""))
    if project_id and store.get(project_id):
        return store.save_chapter_session(project_id, str(chapter.get("id", "")), session)
    return session


def _require_locked_memory_source(
    project: dict[str, Any], chapter: dict[str, Any], commit_id: str = ""
) -> None:
    managed = bool(str(project.get("id", "")) and store.get(str(project.get("id", ""))))
    if not (managed or commit_id):
        return
    if str(chapter.get("authority_state", "")) != "locked":
        raise HTTPException(409, "章节尚未锁定；候选稿和待精修稿不能回灌正式记忆")
    current_hash = hashlib.sha256(str(chapter.get("content", "")).strip().encode("utf-8")).hexdigest()
    if str(chapter.get("locked_content_hash", "")) != current_hash:
        raise HTTPException(409, "正文已在锁定后发生变化，请重新审校并锁定后再回灌记忆")


def _persist_managed_project(
    project: dict[str, Any], reason: str
) -> tuple[dict[str, Any], bool]:
    """Persist when the project belongs to this store; keep unsaved API previews usable."""
    project_id = str(project.get("id", "")).strip()
    if not project_id or store.get(project_id) is None:
        return ensure_project_defaults(project), False
    director_task = store.latest_director_task(project_id)
    if (
        director_task
        and director_task.get("task_type") != "incubation"
        and director_task.get("status") in {"queued", "running"}
    ):
        raise HTTPException(
            409, "自动导演正在写入该作品，请先暂停任务再接受或结算章节"
        )
    try:
        return store.save(project_id, project, reason=reason, expected_updated_at=str(project.get("updated_at", ""))), True
    except ProjectConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


def _latest_managed_commit_project(
    project: dict[str, Any], commit_id: str
) -> dict[str, Any]:
    """Resolve an in-flight memory commit from the latest server-side project.

    Extraction can take minutes. Applying an older browser snapshot would overwrite
    edits made to other chapters during that interval. The target chapter content
    hash still guards against changing the chapter being settled.
    """
    project_id = str(project.get("id", "")).strip()
    if not project_id or not commit_id:
        return project
    latest = store.get(project_id)
    if latest is None:
        return project
    if get_memory_commit(latest, commit_id) is None:
        raise HTTPException(
            409, "服务器中的记忆提交已变化，请刷新作品后重新接受章节"
        )
    return latest


def local_quality_known_context(project: dict[str, Any], chapter: dict[str, Any]) -> str:
    """Render project authority for deterministic local quality checks.

    The local checker should not accuse the model of inventing a past event when
    that event is already present in the story bible, a character card, a world
    entry, an accepted memory fact, or the current chapter plan.  Keep this
    independent from the LLM context budget: it is local text matching only.
    """
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    payload = {
        "premise": project.get("premise", ""),
        "outline": project.get("outline", ""),
        "author_intent": project.get("author_intent", ""),
        "book_rules": project.get("book_rules", ""),
        "narrative": project.get("narrative", {}),
        "characters": project.get("characters", []),
        "world_entries": project.get("world_entries", []),
        "accepted_facts": memory.get("facts", []),
        "timeline": memory.get("timeline", []),
        "relationships": memory.get("relationships", []),
        "story_so_far": memory.get("story_so_far", ""),
        "chapter_plan": chapter.get("plan", {}),
        "chapter_scene_goal": chapter.get("scene_goal", ""),
        "chapter_author_note": chapter.get("author_note", ""),
    }
    return bounded_excerpt(json.dumps(payload, ensure_ascii=False), 60_000)


def quality_source_tail(chapter_content: Any, draft: Any, limit: int = 3000) -> str:
    """Return prior prose only when it is not the candidate's saved revision base.

    Browser and QA flows can legitimately save a generated draft before asking
    for a local check. Comparing that saved chapter to the identical candidate
    reports every sentence as copied from itself and corrupts the quality score.
    The same problem occurs after a light copy-edit, so suppress a near-identical
    *full-length* revision as well.  A short excerpt copied from a longer saved
    chapter is deliberately retained so genuine reuse detection still runs.
    """
    source = str(chapter_content or "").strip()
    candidate = str(draft or "").strip()
    if not source or source == candidate:
        return ""

    source_compact = re.sub(r"\s+", "", source)
    candidate_compact = re.sub(r"\s+", "", candidate)
    longer = max(len(source_compact), len(candidate_compact))
    shorter = min(len(source_compact), len(candidate_compact))
    if (
        shorter >= 120
        and longer > 0
        and shorter / longer >= 0.80
        and SequenceMatcher(
            None, source_compact, candidate_compact, autojunk=False
        ).ratio()
        >= 0.86
    ):
        return ""
    return source[-limit:]


def _indexed_retrieval_hits(
    project: dict[str, Any], query: str, current_index: int, limit: int
) -> list[dict[str, Any]]:
    project_id = str(project.get("id", ""))
    if not project_id or not store.get(project_id):
        return []
    return store.search_project(
        project_id,
        query,
        current_chapter_number=max(1, current_index + 1),
        limit=max(limit, limit * 2),
    )


def _attach_indexed_retrieval(
    project: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    chapters = project.get("chapters", [])
    chapter_id = str(request.get("chapter_id", ""))
    current_index = next(
        (
            index
            for index, chapter in enumerate(chapters)
            if str(chapter.get("id", "")) == chapter_id
        ),
        max(0, len(chapters) - 1),
    )
    query = build_retrieval_query(project, request)
    limit = int(project.get("settings", {}).get("memory_items", 12) or 12)
    request["_indexed_memory_hits"] = _indexed_retrieval_hits(
        project, query, current_index, limit
    )
    request["_user_writing_skills"] = store.user_writing_skills()
    return request


def _persist_context_snapshot(
    project: dict[str, Any],
    request: dict[str, Any],
    build: Any,
    reason: str,
) -> dict[str, Any] | None:
    project_id = str(project.get("id", ""))
    if not project_id or not store.get(project_id):
        return None
    safe_request = {
        key: request.get(key)
        for key in (
            "chapter_id", "mode", "instruction", "selection", "target_words", "skill_ids", "scene_id"
        )
    }
    settings = project.get("settings", {})
    diagnostics = {
        "prompt_version": build.prompt_version,
        "context_schema_version": build.context_schema_version,
        "estimated_tokens": build.estimated_tokens,
        "sections": [
            {
                key: item.get(key)
                for key in (
                    "name", "priority", "tokens_before", "tokens_after",
                    "status", "selected", "reason", "role", "required",
                )
            }
            for item in build.sections
        ],
        "activated_lore": [
            {
                "id": item.get("id", ""),
                "title": item.get("title", ""),
                "reason": item.get("_activation_reason", ""),
                "matched_keys": item.get("_matched_keys", []),
            }
            for item in build.activated_lore
        ],
        "activated_skills": [
            {
                "id": item.get("id", ""),
                "name": item.get("name", ""),
                "scope": item.get("scope", ""),
                "mode": item.get("mode", ""),
                "activation_reason": item.get("activation_reason", ""),
            }
            for item in build.activated_skills
        ],
        "retrieved_memories": [
            {
                "kind": hit.kind,
                "title": hit.title,
                "content": hit.content,
                "score": round(hit.score, 4),
                "source_id": hit.source_id,
                "origin": hit.origin,
            }
            for hit in build.retrieved_memories
        ],
        "budget_warnings": list(build.budget_warnings),
        "runtime": {
            "provider": settings.get("provider", ""),
            "base_url": settings.get("base_url", ""),
            "model": settings.get("model", ""),
            "temperature": settings.get("temperature"),
            "top_p": settings.get("top_p"),
            "max_tokens": settings.get("max_tokens"),
            "context_budget": settings.get("context_budget"),
        },
    }
    try:
        return store.create_context_snapshot(
            project_id,
            str(request.get("chapter_id", "")),
            safe_request,
            build.messages,
            diagnostics,
            reason=reason,
        )
    except KeyError:
        return None


def _prepare_prose_build(build, project, chapter):
    build.messages, prefixes = prepare_messages(build.messages, project, chapter)
    build.estimated_tokens = model_telemetry.estimate("\n".join(m["content"] for m in build.messages))
    for key, label in reversed((("project_prefix", "稳定项目前缀"), ("chapter_prefix", "稳定章节前缀"))):
        content = prefixes[key]
        tokens = model_telemetry.estimate(content)
        build.sections.insert(0, {"name": label, "content": content, "role": "system", "priority": 100,
                                  "tokens": tokens, "tokens_before": tokens, "tokens_after": tokens,
                                  "selected": True, "required": True, "status": "included"})
    try:
        validate_context_budget(project["settings"], build.messages, int(project["settings"].get("max_tokens", 3500)))
    except ValueError as exc:
        build.budget_warnings.append(str(exc))
    return prefixes


app.include_router(
    create_prompt_router(
        lambda: store,
        find_chapter,
        _attach_indexed_retrieval,
        _persist_context_snapshot,
        _prepare_prose_build,
    )
)


@app.post("/api/chapter/plan")
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


@app.post("/api/planning/master")
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


@app.post("/api/planning/volume")
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


@app.post("/api/planning/apply-volume")
async def planning_apply_volume(body: ApplyVolumeRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    try:
        return ensure_project_defaults(apply_volume_routes(project, body.volume_id))
    except KeyError:
        raise HTTPException(404, "分卷不存在") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/chapter/accept")
async def chapter_accept(body: AcceptChapterRequest) -> dict[str, Any]:
    """Accept prose, optionally lock it, and only then open a memory commit."""
    project = ensure_project_defaults(body.project)
    index, chapter = find_chapter(project, body.chapter_id)
    if len(str(chapter.get("content", "")).strip()) < 100:
        raise HTTPException(400, "接受的章节正文至少需要 100 字")
    begin_stage(chapter, "accept", {"content": chapter.get("content", ""), "lock": body.lock})
    chapter["authority_state"] = "accepted"
    complete_stage(chapter, "accept", {"authority_state": "accepted"})
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


@app.post("/api/chapter/contracts/scan")
async def chapter_contract_scan(body: ContractScanRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    index, chapter = find_chapter(project, body.chapter_id)
    text = body.draft.strip() or str(chapter.get("content", ""))
    result = scan_contracts(project, chapter, text, index + 1)
    begin_stage(chapter, "contract_scan", {"content": text, "contracts": project.get("must_contracts", [])})
    complete_stage(chapter, "contract_scan", result)
    return result


@app.post("/api/project/contracts/save")
async def project_contracts_save(body: ProjectRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    ensure_contracts(project)
    project, persisted = _persist_managed_project(project, "must-contracts-updated")
    return {"project": project, "contracts": project.get("must_contracts", []), "persisted": persisted}


@app.post("/api/chapter/workflow/reset")
async def chapter_workflow_reset(body: WorkflowStageRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    _, chapter = find_chapter(project, body.chapter_id)
    try:
        reset = reset_from_stage(chapter, body.stage)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    project, persisted = _persist_managed_project(project, f"workflow-reset-{body.stage}")
    return {"project": project, "chapter_id": body.chapter_id, "reset": reset, "persisted": persisted}


@app.post("/api/project/repairs/rebuild")
async def project_repairs_rebuild(body: ProjectRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    queue = rebuild_repair_queue(project)
    project, persisted = _persist_managed_project(project, "repair-queue-rebuilt")
    return {"project": project, "queue": queue, "persisted": persisted}


@app.post("/api/project/repairs/status")
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




@app.post("/api/chapter/memory")
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
    try:
        result, warnings = await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": CHAPTER_MEMORY_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.15,
            max_tokens=1900,
            timeout_seconds=210,
            validate=validate_chapter_memory_result,
            token_ceiling=2400,
            workload="extraction",
        )
        result["summary"] = str(result.get("summary", ""))
        result["story_so_far"] = str(result.get("story_so_far", ""))[:4000]
        for key in (
            "character_updates",
            "facts",
            "plot_threads",
            "timeline",
            "relationship_updates",
            "continuity_notes",
            "description_updates",
        ):
            if not isinstance(result.get(key), list):
                result[key] = []
        if not isinstance(result.get("scene_settlement"), dict):
            result["scene_settlement"] = {}
        result["fallback"] = False
        result["memory_mode"] = "full"
        result["warnings"] = warnings
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
                    f"完整记忆结构未通过，已用精简AI记忆恢复：{primary_reason}"
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


@app.post("/api/chapter/audit")
async def chapter_audit(body: ChapterActionRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    index, chapter = find_chapter(project, body.chapter_id)
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
    thread_agenda = render_thread_agenda(
        select_thread_agenda(project, query, index, limit=10)
    )
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
【候选草稿】
{bounded_excerpt(draft, 18000)}"""
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
    )
    regression_issues = _prior_volume_regression_issues(project, chapter, draft)
    if regression_issues:
        local_checks.setdefault("issues", []).extend(regression_issues)
        local_checks["score"] = min(int(local_checks.get("score", 100)), 74)
        local_checks["verdict"] = "revise"
    try:
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
        )
        ai_score = max(0, min(100, int(result.get("score", 0))))
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
        result = stable_audit_result(draft, result, local_checks, threshold=85)
        result["fallback"] = False
        result["warnings"] = warnings
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


@app.post("/api/chapter/quality")
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
    )


@app.post("/api/project/manuscript-health")
async def project_manuscript_health(body: ManuscriptHealthRequest) -> dict[str, Any]:
    """Deterministic whole-book checks; no model call and safe for unsaved projects."""
    return manuscript_health_report(ensure_project_defaults(body.project))


@app.post("/api/chapter/memory/apply")
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


@app.post("/api/chapter/memory/degrade")
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


@app.post("/api/incubator")
async def incubator(body: IncubatorRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    seed = body.seed.strip()
    if len(seed) < 8:
        raise HTTPException(400, "灵感至少需要 8 个字")
    target = max(3, min(300, int(body.target_chapters or 30)))
    story_mode = "short" if body.story_mode == "short" else "long"
    try:
        core_options, warnings = await _generate_incubator_core(
            project, seed, body.preferences, story_mode, target
        )
        options: list[dict[str, Any]] = []
        for index, raw_option in enumerate(core_options, start=1):
            option, asset_warnings = await _complete_incubator_option(
                project,
                raw_option,
                index=index,
                seed=seed,
                preferences=body.preferences,
                target=target,
            )
            options.append(option)
            warnings.extend(asset_warnings)
        result = {"options": options}
        validate_incubator_result(
            result, target_chapters=target, story_mode=story_mode
        )
        result["warnings"] = warnings
        result["generation_mode"] = "staged_transaction"
        result["fallback"] = False
        return result
    except Exception as exc:
        if not recoverable_model_error(exc):
            raise HTTPException(
                502, f"灵感孵化失败：{planning_exception_detail(exc)}"
            ) from exc
        raise HTTPException(
            502,
            "灵感孵化没有得到完整方案："
            f"{planning_exception_detail(exc)}。原始灵感未被修改，请重试。",
        ) from exc


@app.post("/api/incubator/start")
async def incubator_start(body: IncubatorRequest) -> dict[str, Any]:
    source = ensure_project_defaults(body.project)
    project_id = str(source.get("id", "")).strip()
    project = store.get(project_id) if project_id else None
    if not project:
        raise HTTPException(404, "请先保存当前作品，再开始灵感孵化")
    seed = body.seed.strip()
    if len(seed) < 8:
        raise HTTPException(400, "灵感至少需要 8 个字")
    latest = store.latest_director_task(project_id)
    if latest and latest.get("status") in {"queued", "running", "stopping"}:
        raise HTTPException(409, "当前已有 AI 任务运行，请先等待完成或暂停")
    config = {
        "seed": seed,
        "preferences": body.preferences.strip(),
        "story_mode": "short" if body.story_mode == "short" else "long",
        "target_chapters": max(3, min(300, int(body.target_chapters or 30))),
    }
    task = store.create_director_task(
        project_id,
        {
            "task_type": "incubation",
            "phase": "incubation",
            "incubation_step": "queued",
            "message": "灵感已保存，等待 AI 开始构思",
            "config": config,
            "events": [],
            "incubation_core_options": [],
            "incubation_options": [],
            "warnings": [],
        },
    )
    _launch_director(task["id"])
    return {"task": task}


@app.post("/api/ideas")
async def ideas(body: IdeasRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    narrative = project.get("narrative", {})
    memory = project.get("memory", {})
    context = f"""【推荐类型】
{'开书主题与核心冲突' if body.kind == 'book' else '下一阶段可选主题与冲突'}
【作品形态】
{project.get('story_mode', 'long')}，计划约 {narrative.get('target_chapters', 30)} 章
【书名与题材】
{project.get('title', '')}；{project.get('genre', '')}
【核心构想】
{project.get('premise', '')}
【作者长期意图】
{project.get('author_intent', '')}
【核心问题与结局方向】
{narrative.get('central_question', '')}
{narrative.get('ending_direction', '')}
【人物】
{bounded_excerpt(json.dumps(project.get('characters', []), ensure_ascii=False), 7000)}
【全书进展】
{memory.get('story_so_far', '')}
【未结线索】
{bounded_excerpt(json.dumps(memory.get('plot_threads', []), ensure_ascii=False), 5000)}
【作者补充】
{body.instruction}"""
    try:
        result, warnings = await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": IDEAS_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.65,
            max_tokens=1200,
            timeout_seconds=180,
            validate=require_schema(
                "options",
                list_fields=("options",),
                list_bounds={"options": (3, None)},
            ),
        )
        options = result.get("options", [])
        if not isinstance(options, list):
            options = []
        clean = []
        for option in options[:6]:
            if not isinstance(option, dict):
                continue
            clean.append(
                {
                    key: str(option.get(key, ""))
                    for key in (
                        "title",
                        "theme",
                        "central_conflict",
                        "story_promise",
                        "turning_point",
                        "ending_direction",
                        "why_fit",
                        "risk",
                    )
                }
            )
        if len(clean) < 3:
            raise ValueError(f"模型只返回 {len(clean)} 个可用方案")
        return {"options": clean[:3], "fallback": False, "warnings": warnings}
    except Exception as exc:
        if recoverable_model_error(exc):
            return local_ideas(project, body.kind, planning_exception_detail(exc))
        raise HTTPException(
            502, f"灵感推荐失败：{planning_exception_detail(exc)}"
        ) from exc


app.include_router(
    create_generation_router(
        store_provider=lambda: store,
        find_chapter=find_chapter,
        attach_indexed_retrieval=_attach_indexed_retrieval,
        prepare_prose_build=_prepare_prose_build,
        get_or_create_session=_get_or_create_chapter_session,
        record_session_turn=_record_chapter_session_turn,
        persist_context_snapshot=_persist_context_snapshot,
    )
)

director_runtime = create_director_runtime(
    store_provider=lambda: store,
    backup_directory=lambda: backup_location.path,
    dependencies=DirectorDependencies(
        attach_indexed_retrieval=lambda: _attach_indexed_retrieval,
        persist_context_snapshot=lambda: _persist_context_snapshot,
        complete_incubator_option=lambda: _complete_incubator_option,
        generate_incubator_core=lambda: _generate_incubator_core,
        director_full_outline=lambda: _director_full_outline,
        chapter_audit=lambda: chapter_audit,
        chapter_memory=lambda: chapter_memory,
        chapter_plan=lambda: chapter_plan,
        director_character_card=lambda: director_character_card,
        director_master_bible=lambda: director_master_bible,
        director_master_contract=lambda: director_master_contract,
        director_master_volume_core=lambda: director_master_volume_core,
        director_master_volume_details=lambda: director_master_volume_details,
        director_plan_chapter_route=lambda: director_plan_chapter_route,
        director_seed_brief=lambda: director_seed_brief,
        director_seed_cast=lambda: director_seed_cast,
        director_seed_world=lambda: director_seed_world,
        find_chapter=lambda: find_chapter,
        launch_director=lambda: _launch_director,
    ),
)
director_runners = director_runtime.runners
_run_auto_director = director_runtime.run_auto_director
_launch_director = director_runtime.launch_director
_director_event = director_runtime.director_event
_remove_quality_debt = director_runtime.remove_quality_debt

app.include_router(create_director_router(director_runtime))
app.include_router(
    create_system_router(
        lambda: store,
        lambda: director_runners,
        app_settings.static_path / "index.html",
    )
)

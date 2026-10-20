"""Dependency container: process-wide singletons and the late-bound callbacks
shared between routers and the automatic director.

Routers and the director receive providers such as ``lambda: container.store``
so that tests can replace a dependency with
``monkeypatch.setattr(container, "store", ...)`` (likewise ``chapter_audit``,
``chapter_plan``, ``chapter_memory``, ``_launch_director``,
``run_structured_completion`` and ``github_backup``).  The application
factory and router registration live in :mod:`app.main`.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from difflib import SequenceMatcher
from typing import Any

from fastapi import HTTPException

from app.api.routers.editorial import create_editorial_routes
from app.api.routers.planning import create_planning_routes
from app.core.config import APP_VERSION, settings as app_settings
from app.director.service import DirectorDependencies, create_director_runtime
from app.io.github_backup import GitHubBackupService
from app.llm import telemetry as model_telemetry
from app.llm.budget import request_budget
from app.llm.client import validate_context_budget
from app.llm.errors import bounded_excerpt, planning_exception_detail
from app.llm.structured_output import structured_completion as run_structured_completion
from app.planning.director_planning import create_director_planning_service
from app.storage.automatic_backup import AutomaticBackupService
from app.storage.backup_location import BackupLocation
from app.storage.instance_lock import ApplicationInstanceLock
from app.storage.project_service import ProjectConflictError
from app.storage.project_store import ProjectStore
from app.storage.schema.project import ensure_project_defaults
from app.story.memory_integrity import get_memory_commit
from app.writing.chapter_session import add_turn, new_session, prepare_messages
from app.writing.context_builder import build_retrieval_query

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
github_backup = GitHubBackupService(
    lambda: store,
    app_settings.database_path.with_name(
        app_settings.database_path.stem + "-github-backup.json"
    ),
    app_version=APP_VERSION,
)


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
    schema: dict[str, Any] | None = None,
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
        schema=schema,
    )


def find_chapter(project: dict[str, Any], chapter_id: str) -> tuple[int, dict[str, Any]]:
    chapters = project.get("chapters", [])
    for index, chapter in enumerate(chapters):
        if chapter.get("id") == chapter_id:
            # Model calls that follow are accounted to this project/chapter.
            model_telemetry.bind_scope(project.get("id"), chapter_id)
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
        "request_budget": request_budget(settings, build.messages, int(settings.get("max_tokens", 3500))).as_dict(),
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
                "injected": item.get("injected", False),
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



# Chapter workflows shared with the director (routers are registered in app.main).
planning_routes = create_planning_routes(
    find_chapter_callback=find_chapter,
    indexed_retrieval_callback=_indexed_retrieval_hits,
    structured_completion_callback=structured_completion,
)
chapter_plan = planning_routes.chapter_plan


editorial_routes = create_editorial_routes(
    store_provider=lambda: store,
    find_chapter_callback=find_chapter,
    indexed_retrieval_callback=_indexed_retrieval_hits,
    persist_managed_project_callback=_persist_managed_project,
    latest_managed_commit_callback=_latest_managed_commit_project,
    record_session_turn_callback=_record_chapter_session_turn,
    require_locked_source_callback=_require_locked_memory_source,
    quality_context_callback=local_quality_known_context,
    quality_source_tail_callback=quality_source_tail,
    structured_completion_callback=structured_completion,
    director_event_provider=lambda: _director_event,
    remove_quality_debt_provider=lambda: _remove_quality_debt,
)
chapter_memory = editorial_routes.chapter_memory
chapter_audit = editorial_routes.chapter_audit


director_planning = create_director_planning_service(structured_completion)
director_seed_brief = director_planning.seed_brief
director_seed_cast = director_planning.seed_cast
director_character_card = director_planning.character_card
director_seed_world = director_planning.seed_world
director_master_bible = director_planning.master_bible
director_master_contract = director_planning.master_contract
director_master_volume_core = director_planning.master_volume_core
director_master_volume_details = director_planning.master_volume_details
director_plan_chapter_route = director_planning.plan_chapter_route
_director_full_outline = director_planning.full_outline
_generate_incubator_core = director_planning.generate_incubator_core
_complete_incubator_option = director_planning.complete_incubator_option


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

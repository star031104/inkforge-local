"""Application factory: the FastAPI app, its lifespan and router registration.

Process-wide singletons and the callbacks shared between routers live in
:mod:`app.api.container`; this module only composes them into one app.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import container
from app.api.routers.branches import create_branch_router
from app.api.routers.calibration_review import create_calibration_review_router
from app.api.routers.chapter_sessions import create_chapter_session_router
from app.api.routers.demo import create_demo_router
from app.api.routers.director import create_director_router
from app.api.routers.evaluation import create_evaluation_router
from app.api.routers.export_cleanup import create_export_cleanup_router
from app.api.routers.generation import create_generation_router
from app.api.routers.genre_templates import create_genre_templates_router
from app.api.routers.github_backup import create_github_backup_router
from app.api.routers.incubation import create_incubation_router
from app.api.routers.manuscript_export import create_manuscript_export_router
from app.api.routers.manuscript_import import create_manuscript_import_router
from app.api.routers.professional import create_professional_router
from app.api.routers.projects import create_projects_router
from app.api.routers.prompts import create_prompt_router
from app.api.routers.research import create_research_router
from app.api.routers.story_graph import create_story_graph_router
from app.api.routers.system import create_system_router
from app.api.routers.usage import UsageScopeMiddleware, create_usage_router
from app.api.routers.workspace import create_workspace_router
from app.core.config import APP_VERSION, settings as app_settings
from app.llm import telemetry as model_telemetry


@asynccontextmanager
async def application_lifespan(_app: FastAPI):
    container.instance_lock.acquire()
    container.store.clear_director_task_leases()
    backup_stop = asyncio.Event()
    backup_task = asyncio.create_task(container.automatic_backups.run(backup_stop))
    github_task = asyncio.create_task(container.github_backup.run(backup_stop))
    try:
        yield
    finally:
        backup_stop.set()
        await backup_task
        await github_task
        container.instance_lock.release()


def register_routers(app: FastAPI) -> None:
    """Register every router; order matters where paths overlap."""
    c = container
    app.include_router(create_usage_router())
    app.include_router(create_workspace_router(lambda: c.store))
    app.include_router(
        create_projects_router(
            lambda: c.store,
            c.backup_location,
            lambda: c.director_runners,
        )
    )
    app.include_router(create_research_router())
    app.include_router(create_genre_templates_router())
    app.include_router(create_github_backup_router(lambda: c.github_backup))
    app.include_router(create_demo_router(lambda: c.store))
    app.include_router(create_professional_router(lambda: c.store, c.structured_completion))
    app.include_router(
        create_chapter_session_router(
            lambda: c.store,
            c.find_chapter,
            c._get_or_create_chapter_session,
        )
    )
    app.include_router(
        create_prompt_router(
            lambda: c.store,
            c.find_chapter,
            c._attach_indexed_retrieval,
            c._persist_context_snapshot,
            c._prepare_prose_build,
        )
    )
    app.include_router(c.planning_routes.router)
    app.include_router(c.editorial_routes.router)
    app.include_router(
        create_manuscript_import_router(
            store_provider=lambda: c.store,
            structured_completion_provider=lambda: c.structured_completion,
            chapter_memory_provider=lambda: c.chapter_memory,
        )
    )
    app.include_router(create_story_graph_router(lambda: c.store))
    app.include_router(create_manuscript_export_router(lambda: c.store))
    app.include_router(
        create_calibration_review_router(lambda: c.store, lambda: c.structured_completion)
    )
    app.include_router(create_export_cleanup_router(lambda: c.store))
    app.include_router(
        create_incubation_router(
            store_provider=lambda: c.store,
            generate_core_callback=c._generate_incubator_core,
            complete_option_callback=c._complete_incubator_option,
            structured_completion_callback=c.structured_completion,
            launch_director_provider=lambda: c._launch_director,
        )
    )
    app.include_router(
        create_generation_router(
            store_provider=lambda: c.store,
            find_chapter=c.find_chapter,
            attach_indexed_retrieval=c._attach_indexed_retrieval,
            prepare_prose_build=c._prepare_prose_build,
            get_or_create_session=c._get_or_create_chapter_session,
            record_session_turn=c._record_chapter_session_turn,
            persist_context_snapshot=c._persist_context_snapshot,
        )
    )
    app.include_router(
        create_branch_router(
            store_provider=lambda: c.store,
            find_chapter=c.find_chapter,
            attach_indexed_retrieval=c._attach_indexed_retrieval,
            prepare_prose_build=c._prepare_prose_build,
            structured_completion_provider=lambda: c.structured_completion,
            chapter_audit_provider=lambda: c.chapter_audit,
        )
    )
    app.include_router(create_director_router(c.director_runtime))
    app.include_router(
        create_evaluation_router(
            lambda: c.store,
            history_root=app_settings.database_path.parent / "evaluations",
            telemetry_path=lambda: model_telemetry._path,
            completion=c.structured_completion,
        )
    )
    app.include_router(
        create_system_router(
            lambda: c.store,
            lambda: c.director_runners,
            app_settings.static_path / "index.html",
        )
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title="InkForge Local API", version=APP_VERSION, lifespan=application_lifespan
    )
    app.mount("/assets", StaticFiles(directory=app_settings.static_path), name="assets")
    app.add_middleware(UsageScopeMiddleware)
    register_routers(app)
    return app


app = create_app()

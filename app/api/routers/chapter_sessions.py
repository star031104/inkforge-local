from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from ...chapter_session import checkpoint as session_checkpoint
from ...chapter_session import rollback as rollback_session
from ...db import ensure_project_defaults
from ..schemas import SessionRequest


def create_chapter_session_router(
    store_provider: Callable[[], Any],
    find_chapter: Callable[[dict[str, Any], str], tuple[int, dict[str, Any]]],
    get_or_create_session: Callable[
        [dict[str, Any], dict[str, Any]], dict[str, Any]
    ],
) -> APIRouter:
    """Manage resumable chapter conversations and manual checkpoints."""
    router = APIRouter(prefix="/api/chapter/session", tags=["chapter-sessions"])

    def persist(
        project: dict[str, Any], chapter_id: str, session: dict[str, Any]
    ) -> dict[str, Any]:
        store = store_provider()
        project_id = str(project.get("id", ""))
        if project_id and store.get(project_id):
            return store.save_chapter_session(project_id, chapter_id, session)
        return session

    @router.post("")
    async def chapter_session_get(body: SessionRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        _, chapter = find_chapter(project, body.chapter_id)
        session = get_or_create_session(project, chapter)
        return {"session": persist(project, body.chapter_id, session)}

    @router.post("/checkpoint")
    async def chapter_session_checkpoint(body: SessionRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        _, chapter = find_chapter(project, body.chapter_id)
        session = get_or_create_session(project, chapter)
        created = session_checkpoint(session, "manual")
        persist(project, body.chapter_id, session)
        return {"session": session, "checkpoint": created}

    @router.post("/rollback")
    async def chapter_session_rollback(body: SessionRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        _, chapter = find_chapter(project, body.chapter_id)
        session = get_or_create_session(project, chapter)
        try:
            rollback_session(session, body.checkpoint_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        persist(project, body.chapter_id, session)
        return {"session": session}

    return router

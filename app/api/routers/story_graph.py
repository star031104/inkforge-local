"""Read-only relationship graph, event timeline and foreshadowing arcs."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException

from app.story.story_graph import build_story_graph


def create_story_graph_router(store_provider: Callable[[], Any]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/projects/{project_id}/story-graph")
    async def story_graph(project_id: str, as_of: int = 0) -> dict[str, Any]:
        project = store_provider().get(project_id)
        if project is None:
            raise HTTPException(404, "作品不存在")
        return build_story_graph(project, as_of or None)

    return router

"""开写前设定预检 endpoint (``app.planning.preflight``)."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.planning.preflight import run_preflight, store_preflight
from app.storage.schema.project import ensure_project_defaults


class PreflightRequest(BaseModel):
    volume_id: str = Field(default="", max_length=80)
    # Deterministic checks only (no model call), e.g. for a quick re-check.
    local_only: bool = False


def create_preflight_router(
    store_provider: Callable[[], Any],
    structured_completion_provider: Callable[[], Callable[..., Any]],
) -> APIRouter:
    router = APIRouter(tags=["planning", "preflight"])

    def project_or_404(project_id: str) -> dict[str, Any]:
        project = store_provider().get(project_id)
        if not project:
            raise HTTPException(404, "作品不存在")
        return ensure_project_defaults(project)

    @router.post("/api/projects/{project_id}/preflight")
    async def project_preflight(
        project_id: str, body: PreflightRequest | None = None
    ) -> dict[str, Any]:
        body = body or PreflightRequest()
        project = project_or_404(project_id)
        try:
            result = await run_preflight(
                project,
                body.volume_id or None,
                completion=None if body.local_only else structured_completion_provider(),
            )
        except KeyError:
            raise HTTPException(404, "分卷不存在") from None
        # Re-read before saving: the author may have saved while the model ran.
        latest = store_preflight(project_or_404(project_id), result)
        saved = store_provider().save(project_id, latest, reason="preflight")
        return {"preflight": result, "updated_at": str(saved.get("updated_at", ""))}

    return router

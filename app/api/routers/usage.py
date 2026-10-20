"""Model usage/cost aggregates and endpoint-specific parameter presets."""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Query

from ... import model_telemetry
from ...providers import endpoint_kind, recommended_settings

_PROJECT_PATH = re.compile(r"^/api/projects/([A-Za-z0-9_-]{1,80})(?:/|$)")


class UsageScopeMiddleware:
    """Attribute model calls made under ``/api/projects/{id}/...`` to that project.

    Pure ASGI so the context variable is set in the same task that runs the
    endpoint (and is inherited by tasks it starts).  Chapter attribution is
    bound by ``find_chapter`` once a handler resolves its chapter.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            match = _PROJECT_PATH.match(str(scope.get("path") or ""))
            if match:
                model_telemetry.bind_scope(match.group(1))
        await self.app(scope, receive, send)


def create_usage_router() -> APIRouter:
    router = APIRouter(tags=["usage"])

    @router.get("/api/projects/{project_id}/usage")
    async def project_usage(
        project_id: str,
        last: int = Query(default=10, ge=1, le=200),
        chapter_id: str = Query(default="", max_length=80),
    ) -> dict[str, Any]:
        """Calls, tokens, cache hit rate and USD cost for one project."""
        return model_telemetry.project_usage(
            project_id, last_chapters=last, chapter_id=chapter_id or None
        )

    @router.post("/api/model/recommended-settings")
    async def model_recommended_settings(body: dict[str, Any]) -> dict[str, Any]:
        """Preset for the endpoint in ``settings`` plus current → recommended diff.

        Body: ``{"settings": {...}, "previous_base_url": "..."}`` or a bare
        settings object.  ``kind_changed`` is true when the previous endpoint
        was of another kind (local ↔ cloud).
        """
        settings = body.get("settings") if isinstance(body.get("settings"), dict) else body
        settings = {key: value for key, value in dict(settings).items() if key != "api_key"}
        result = recommended_settings(settings)
        previous = str(body.get("previous_base_url") or "").strip()
        if previous:
            previous_kind = endpoint_kind({"base_url": previous})
            result["previous_kind"] = previous_kind
            result["kind_changed"] = (previous_kind == "local") != (result["kind"] == "local")
        else:
            result["previous_kind"] = None
            result["kind_changed"] = False
        return result

    return router

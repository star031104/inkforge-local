"""Plain manuscript export (TXT / EPUB / DOCX) for the 导出 panel."""
from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.io import export_formats


class ExportRequest(BaseModel):
    format: str = "txt"
    options: dict[str, Any] = Field(default_factory=dict)


def create_manuscript_export_router(store_provider: Callable[[], Any]) -> APIRouter:
    router = APIRouter()

    def project_or_404(project_id: str) -> dict[str, Any]:
        project = store_provider().get(project_id)
        if project is None:
            raise HTTPException(404, "作品不存在")
        return project

    @router.post("/api/projects/{project_id}/export/manuscript")
    async def export_manuscript(project_id: str, body: ExportRequest) -> dict[str, Any]:
        project = project_or_404(project_id)
        exporter = export_formats.EXPORTERS.get(body.format)
        if exporter is None:
            raise HTTPException(422, "导出格式只支持 txt / epub / docx")

        def build() -> dict[str, Any]:
            result = exporter(project, body.options)
            content = result.pop("content")
            if isinstance(content, str):
                content = content.encode("utf-8")
            result["content_base64"] = base64.b64encode(content).decode("ascii")
            result["bytes"] = len(content)
            return result

        try:
            # Building a long EPUB/DOCX is CPU-bound; run it off the event loop.
            return await run_in_threadpool(build)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return router

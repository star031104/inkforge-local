"""导出前清理：扫描书稿问题并应用作者勾选的确定性修复。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.project_service import ProjectConflictError
from app.services import export_cleanup as cleanup


class CleanupScope(BaseModel):
    start: int = 1
    end: int = 0
    chapter_ids: list[str] = Field(default_factory=list)

    def options(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "chapter_ids": self.chapter_ids}


class NameMapping(BaseModel):
    from_: str = Field(alias="from")
    to: str

    model_config = {"populate_by_name": True}


class CleanupApplyRequest(CleanupScope):
    fixes: list[str] = Field(default_factory=list)
    name_mappings: list[NameMapping] = Field(default_factory=list)
    include_locked: bool = False
    expected_updated_at: str | None = None


def create_export_cleanup_router(store_provider: Callable[[], Any]) -> APIRouter:
    router = APIRouter(tags=["export-cleanup"])

    def project_or_404(project_id: str) -> dict[str, Any]:
        project = store_provider().get(project_id)
        if project is None:
            raise HTTPException(404, "作品不存在")
        return project

    @router.post("/api/projects/{project_id}/export-cleanup/scan")
    async def scan_manuscript(project_id: str, body: CleanupScope) -> dict[str, Any]:
        project = project_or_404(project_id)
        report = await run_in_threadpool(cleanup.scan, project, body.options())
        report["project_updated_at"] = project.get("updated_at", "")
        return report

    @router.post("/api/projects/{project_id}/export-cleanup/apply")
    async def apply_cleanup(project_id: str, body: CleanupApplyRequest) -> dict[str, Any]:
        project = project_or_404(project_id)
        expected = body.expected_updated_at
        if expected and expected != str(project.get("updated_at", "")):
            raise HTTPException(409, "作品已在别处更新，请重新检查后再应用修复。")
        mappings = [{"from": item.from_, "to": item.to} for item in body.name_mappings]
        try:
            updated, summary = await run_in_threadpool(
                cleanup.apply,
                project,
                body.fixes,
                name_mappings=mappings,
                options=body.options(),
                include_locked=body.include_locked,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not summary["changed_count"]:
            summary["project_updated_at"] = project.get("updated_at", "")
            summary["memory_stale_from"] = int(project.get("memory", {}).get("stale_from_chapter", 0) or 0)
            return summary
        try:
            # A non-autosave reason makes the store snapshot every changed
            # chapter into its version history before overwriting it.
            saved = store_provider().save(
                project_id,
                updated,
                reason=cleanup.VERSION_REASON,
                expected_updated_at=str(project.get("updated_at", "")),
            )
        except ProjectConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        summary["project_updated_at"] = saved.get("updated_at", "")
        summary["memory_stale_from"] = int(saved.get("memory", {}).get("stale_from_chapter", 0) or 0)
        return summary

    return router

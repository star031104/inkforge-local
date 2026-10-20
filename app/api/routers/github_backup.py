"""GitHub private-repository backup endpoints (global, not per project)."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from app.infrastructure.github_client import GitHubError
from app.services.github_backup import GitHubBackupError, GitHubBackupService


class GitHubBackupSettingsRequest(BaseModel):
    token: str | None = None
    clear_token: bool = False
    owner: str | None = None
    repo: str | None = None
    branch: str | None = None
    auto_sync: bool | None = None
    include_binary_exports: bool | None = None


class GitHubRestoreRequest(BaseModel):
    folder: str


def _http_error(exc: Exception) -> HTTPException:
    status = getattr(exc, "status", 0)
    if status in {401, 403}:
        return HTTPException(400, str(exc))
    return HTTPException(502 if isinstance(exc, GitHubError) else 400, str(exc))


def create_github_backup_router(
    service_provider: Callable[[], GitHubBackupService],
) -> APIRouter:
    router = APIRouter(prefix="/api/github-backup", tags=["github-backup"])

    def service() -> GitHubBackupService:
        return service_provider()

    async def call(function: Callable[..., Any], *args: Any) -> Any:
        try:
            return await asyncio.to_thread(function, *args)
        except (GitHubError, GitHubBackupError) as exc:
            raise _http_error(exc) from exc

    @router.get("/settings")
    async def github_backup_settings() -> dict[str, Any]:
        return service().describe_settings()

    @router.put("/settings")
    async def github_backup_settings_update(body: GitHubBackupSettingsRequest) -> dict[str, Any]:
        payload = body.model_dump(exclude_none=True)
        try:
            return service().update_settings(payload)
        except GitHubBackupError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/connect")
    async def github_backup_connect() -> dict[str, Any]:
        return await call(service().connect)

    @router.get("/status")
    async def github_backup_status() -> dict[str, Any]:
        return service().status()

    def _start(kind: str, project_id: str, tasks: BackgroundTasks) -> dict[str, Any]:
        current = service()
        if not current.describe_settings()["token_set"]:
            raise HTTPException(400, "尚未设置 GitHub 令牌")
        if not current.begin_job(kind):
            raise HTTPException(409, "已有备份任务正在进行，请稍候")
        tasks.add_task(current.run_job, kind, project_id)
        return {"queued": True, "job": current.job_status()}

    @router.post("/sync/{project_id}")
    async def github_backup_sync_project(project_id: str, tasks: BackgroundTasks) -> dict[str, Any]:
        if service().store_provider().get(project_id) is None:
            raise HTTPException(404, "作品不存在")
        return _start("project", project_id, tasks)

    @router.post("/sync-all")
    async def github_backup_sync_all(tasks: BackgroundTasks) -> dict[str, Any]:
        return _start("all", "", tasks)

    @router.get("/remote-books")
    async def github_backup_remote_books() -> dict[str, Any]:
        return {"books": await call(service().remote_books)}

    @router.post("/restore")
    async def github_backup_restore(body: GitHubRestoreRequest) -> dict[str, Any]:
        result = await call(service().restore, body.folder)
        project = result["project"]
        return {"id": result["id"], "title": result["title"], "chapter_count": len(project.get("chapters", []))}

    return router

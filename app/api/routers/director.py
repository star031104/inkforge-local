"""HTTP transport for the automatic director service."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ...services.director_runtime import DirectorControlError, DirectorRuntime
from ..schemas import (
    AutoDirectorStartRequest,
    AutoRefineRequest,
    ContinuePlannedWritingRequest,
)


def create_director_router(runtime: DirectorRuntime) -> APIRouter:
    router = APIRouter(tags=["automatic-director"])

    async def invoke(operation, *args) -> dict[str, Any]:
        try:
            return await operation(*args)
        except DirectorControlError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    @router.post("/api/director/start")
    async def director_start(body: AutoDirectorStartRequest) -> dict[str, Any]:
        return await invoke(runtime.start, body)

    @router.post("/api/director/projects/{project_id}/write-planned")
    async def director_write_planned(
        project_id: str, body: ContinuePlannedWritingRequest
    ) -> dict[str, Any]:
        return await invoke(runtime.write_planned, project_id, body)

    @router.post("/api/director/projects/{project_id}/refine")
    async def director_refine(
        project_id: str, body: AutoRefineRequest
    ) -> dict[str, Any]:
        return await invoke(runtime.refine, project_id, body)

    @router.get("/api/director/tasks/{task_id}")
    async def director_task(task_id: str) -> dict[str, Any]:
        return await invoke(runtime.get_task, task_id)

    @router.get("/api/director/projects/{project_id}/latest")
    async def director_latest(project_id: str) -> dict[str, Any]:
        return await invoke(runtime.latest, project_id)

    @router.post("/api/director/tasks/{task_id}/pause")
    async def director_pause(task_id: str) -> dict[str, Any]:
        return await invoke(runtime.pause, task_id)

    @router.post("/api/director/tasks/{task_id}/resume")
    async def director_resume(task_id: str) -> dict[str, Any]:
        return await invoke(runtime.resume, task_id)

    return router

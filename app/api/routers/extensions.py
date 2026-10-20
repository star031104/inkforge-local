"""HTTP routes for manuscript import, story visualization and manuscript
export.  Long-running import work is resumable and its state lives in the
extension store, never in the browser-edited project JSON."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.io.file_parsing import parse_reference_file
from app.storage.extension_store import ExtensionStore
from app.storage.project_service import ProjectConflictError
from app.io import manuscript_import as importer
from app.io import export_formats
from app.review.memory_settlement import _apply_director_memory
from app.story.story_graph import build_story_graph
from app.writing.requests import ChapterActionRequest


class ImportFileRequest(BaseModel):
    name: str
    content_base64: str
    mode: str = "auto"
    target_chars: int = 3000


class ImportCreateRequest(ImportFileRequest):
    title: str = ""
    genre: str = ""
    target_project_id: str = ""
    copy_settings_from: str = ""
    start_analysis: bool = True


class ImportReviewAccept(BaseModel):
    fields: list[str] = Field(default_factory=list)
    characters: list[str] = Field(default_factory=list)
    world_entries: list[str] = Field(default_factory=list)
    foreshadow: list[str] = Field(default_factory=list)
    overwrite: bool = False
    expected_updated_at: str | None = None
    start_settlement: bool = True


class ExportRequest(BaseModel):
    format: str = "txt"
    options: dict[str, Any] = Field(default_factory=dict)




def create_extensions_router(
    *,
    store_provider: Callable[[], Any],
    structured_completion_provider: Callable[[], Callable[..., Any]],
    chapter_memory_provider: Callable[[], Callable[..., Any]],
) -> APIRouter:
    router = APIRouter()
    running: dict[str, asyncio.Task[Any]] = {}

    def store() -> Any:
        return store_provider()

    def extensions() -> ExtensionStore:
        return ExtensionStore.for_store(store())

    def project_or_404(project_id: str) -> dict[str, Any]:
        project = store().get(project_id)
        if project is None:
            raise HTTPException(404, "作品不存在")
        return project

    def is_running(project_id: str) -> bool:
        task = running.get(project_id)
        return bool(task and not task.done())

    async def guarded(project_id: str, coroutine: Any) -> None:
        try:
            await coroutine
        except Exception as exc:  # persist unexpected failures for the UI
            job = extensions().get(project_id, importer.JOB_KEY) or {}
            job.update({"status": "failed", "error": str(exc)[:500]})
            importer.job_event(job, f"任务异常停止：{str(exc)[:200]}", "error")
            extensions().put(project_id, importer.JOB_KEY, job)
        finally:
            running.pop(project_id, None)

    async def launch(project_id: str, factory: Callable[[], Any], wait: bool) -> dict[str, Any]:
        if is_running(project_id):
            raise HTTPException(409, "这部作品已有导入任务在运行")
        if wait:
            await guarded(project_id, factory())
        else:
            running[project_id] = asyncio.create_task(guarded(project_id, factory()))
            await asyncio.sleep(0)
        return importer.public_job(extensions().get(project_id, importer.JOB_KEY), is_running(project_id))

    async def parse_upload(body: ImportFileRequest) -> dict[str, Any]:
        # Decoding and parsing a 20MB DOCX/EPUB takes seconds: keep it off
        # the event loop so streaming generation elsewhere is not frozen.
        try:
            return await run_in_threadpool(parse_reference_file, body.name, body.content_base64)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def analysis(project_id: str) -> Any:
        return importer.run_import_analysis(
            store=store(),
            extensions=extensions(),
            project_id=project_id,
            completion=structured_completion_provider(),
        )

    def settlement(project_id: str) -> Any:
        return importer.run_import_settlement(
            store=store(),
            extensions=extensions(),
            project_id=project_id,
            chapter_memory=chapter_memory_provider(),
            apply_memory=_apply_director_memory,
            request_factory=ChapterActionRequest,
        )

    # ------------------------------------------------------------- import

    @router.post("/api/import/preview")
    async def import_preview(body: ImportFileRequest) -> dict[str, Any]:
        parsed = await parse_upload(body)
        return await run_in_threadpool(
            lambda: importer.preview_split(parsed, mode=body.mode, target_chars=body.target_chars)
        )

    @router.post("/api/import/create")
    async def import_create(body: ImportCreateRequest, wait: bool = False) -> dict[str, Any]:
        parsed = await parse_upload(body)
        split, preview = await run_in_threadpool(
            lambda: (
                importer.split_manuscript(parsed["text"], mode=body.mode, target_chars=body.target_chars),
                importer.preview_split(parsed, mode=body.mode, target_chars=body.target_chars),
            )
        )
        try:
            project = importer.create_imported_project(
                store(),
                split,
                title=body.title.strip() or preview["title_guess"],
                genre=body.genre.strip(),
                source_name=body.name,
                target_project_id=body.target_project_id,
                copy_settings_from=body.copy_settings_from,
            )
        except KeyError as exc:
            raise HTTPException(404, "目标作品不存在") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        job = importer.new_job(body.name, len(project["chapters"]), split["total_chars"])
        job["warnings"] = list(split["warnings"])
        importer.job_event(job, f"已导入 {len(project['chapters'])} 章、{split['total_chars']} 字", "success")
        extensions().put(project["id"], importer.JOB_KEY, job)
        if body.start_analysis:
            job_view = await launch(project["id"], lambda: analysis(project["id"]), wait)
        else:
            job_view = importer.public_job(job)
        return {"project": store().get(project["id"]), "job": job_view}

    @router.get("/api/projects/{project_id}/import-job")
    async def import_job(project_id: str, include_review: bool = True) -> dict[str, Any]:
        project_or_404(project_id)
        view = importer.public_job(extensions().get(project_id, importer.JOB_KEY), is_running(project_id))
        if not include_review:
            view.pop("review", None)
        return view

    @router.post("/api/projects/{project_id}/import-job/analyze")
    async def import_analyze(project_id: str, wait: bool = False) -> dict[str, Any]:
        project_or_404(project_id)
        if not extensions().get(project_id, importer.JOB_KEY):
            raise HTTPException(404, "这部作品没有旧稿导入任务")
        return await launch(project_id, lambda: analysis(project_id), wait)

    @router.post("/api/projects/{project_id}/import-job/pause")
    async def import_pause(project_id: str) -> dict[str, Any]:
        if not extensions().get(project_id, importer.JOB_KEY):
            raise HTTPException(404, "这部作品没有旧稿导入任务")
        if is_running(project_id):
            # Flag only the pause field on the latest stored job, atomically:
            # writing back a stale copy could roll back settlement progress or
            # resurrect a job that just completed.
            importer.request_pause(extensions(), project_id)
        return importer.public_job(extensions().get(project_id, importer.JOB_KEY), is_running(project_id))

    @router.post("/api/projects/{project_id}/import-job/confirm")
    async def import_confirm(project_id: str, body: ImportReviewAccept, wait: bool = False) -> dict[str, Any]:
        project = project_or_404(project_id)
        job = extensions().get(project_id, importer.JOB_KEY)
        if not job or not isinstance(job.get("review"), dict):
            raise HTTPException(409, "设定反推尚未完成，暂时没有可确认的候选")
        if is_running(project_id):
            raise HTTPException(409, "导入任务正在运行，请稍后再确认")
        applied = importer.apply_review(project, job["review"], body.model_dump())
        try:
            saved = store().save(
                project_id, project, reason="manuscript-import-review",
                expected_updated_at=body.expected_updated_at,
            )
        except ProjectConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        job["review"]["status"] = "confirmed"
        job["review"]["applied"] = applied
        job.update({"status": "ready_to_settle", "phase": "settlement"})
        importer.job_event(job, f"作者已确认 {len(applied)} 项设定，准备逐章结算记忆", "success")
        extensions().put(project_id, importer.JOB_KEY, job)
        view = importer.public_job(job)
        if body.start_settlement:
            view = await launch(project_id, lambda: settlement(project_id), wait)
        return {"project": store().get(project_id) or saved, "job": view, "applied": applied}

    @router.post("/api/projects/{project_id}/import-job/settle")
    async def import_settle(project_id: str, wait: bool = False) -> dict[str, Any]:
        project_or_404(project_id)
        job = extensions().get(project_id, importer.JOB_KEY)
        if not job:
            raise HTTPException(404, "这部作品没有旧稿导入任务")
        if job.get("review", {}) and job["review"].get("status") != "confirmed":
            raise HTTPException(409, "请先确认反推出的人物和设定，再结算章节记忆")
        return await launch(project_id, lambda: settlement(project_id), wait)

    # ------------------------------------------------------- visualization

    @router.get("/api/projects/{project_id}/story-graph")
    async def story_graph(project_id: str, as_of: int = 0) -> dict[str, Any]:
        return build_story_graph(project_or_404(project_id), as_of or None)

    # ------------------------------------------------------------ export

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

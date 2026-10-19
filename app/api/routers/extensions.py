"""HTTP routes for manuscript import, story visualization, reader simulation
and web-novel publishing.  All long-running work is resumable and its state
lives in the extension store, never in the browser-edited project JSON."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ...file_parsing import parse_reference_file
from ...infrastructure.extension_store import ExtensionStore
from ...project_service import ProjectConflictError
from ...services import manuscript_import as importer
from ...services import publishing
from ...services import reader_simulation as readers
from ...services.memory_settlement import _apply_director_memory
from ...services.story_graph import build_story_graph
from ..schemas import ChapterActionRequest


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


class ReaderSimulationRequest(BaseModel):
    chapter_ids: list[str] = Field(default_factory=list)
    personas: list[str] = Field(default_factory=lambda: list(readers.DEFAULT_PERSONAS))
    compare_with_plan: bool = True


class ExportRequest(BaseModel):
    format: str = "txt"
    options: dict[str, Any] = Field(default_factory=dict)


class ScheduleRequest(BaseModel):
    schedule: dict[str, Any]


class ChapterStatusRequest(BaseModel):
    status: str
    published_at: str = ""


class BulkStatusRequest(BaseModel):
    chapter_ids: list[str]
    status: str
    published_at: str = ""


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

    # ------------------------------------------------------ reader simulation

    reader_tasks: dict[str, asyncio.Task[Any]] = {}

    def reader_running(project_id: str) -> bool:
        task = reader_tasks.get(project_id)
        return bool(task and not task.done())

    def reader_ids(project: dict[str, Any], body: ReaderSimulationRequest, limit: int) -> list[str]:
        ids = list(dict.fromkeys(item for item in body.chapter_ids if item))[:limit]
        if not ids:
            raise HTTPException(422, "请选择要模拟阅读的章节")
        known = {str(chapter.get("id")) for chapter in project["chapters"]}
        if any(chapter_id not in known for chapter_id in ids):
            raise HTTPException(404, "章节不存在")
        return ids

    def launch_reader_job(
        project_id: str, project: dict[str, Any], job: dict[str, Any], completion: Any
    ) -> None:
        async def run() -> None:
            try:
                await readers.run_reader_job(
                    project=project, extensions=extensions(), project_id=project_id,
                    job=job, completion=completion,
                )
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # persist unexpected failures for polling
                message = str(exc)[:300] or exc.__class__.__name__

                def fail(current: Any) -> Any:
                    if isinstance(current, dict) and current.get("id") == job["id"]:
                        current.update({"status": "failed"})
                        current.setdefault("errors", []).append(message)
                        return current
                    return None

                extensions().update(project_id, readers.JOB_KEY, fail)
            finally:
                reader_tasks.pop(project_id, None)

        reader_tasks[project_id] = asyncio.create_task(run())

    @router.post("/api/projects/{project_id}/reader-simulation")
    async def reader_simulation(
        project_id: str, body: ReaderSimulationRequest, background: bool = False
    ) -> dict[str, Any]:
        """Simulate readers on up to 3 chapters (or a background job of up to 12).

        Every chapter × reader call of every run in this process (all
        projects, foreground and background) shares one bounded pool of
        ``readers.DEFAULT_CONCURRENCY`` model calls; reports keep the
        requested order.
        """
        project = project_or_404(project_id)
        personas = body.personas or list(readers.DEFAULT_PERSONAS)
        completion = structured_completion_provider()
        if background:
            if reader_running(project_id):
                raise HTTPException(409, "这部作品已有读者模拟在运行")
            ids = reader_ids(project, body, readers.MAX_JOB_CHAPTERS)
            job = readers.new_reader_job(ids, personas, body.compare_with_plan)
            extensions().put(project_id, readers.JOB_KEY, job)
            launch_reader_job(project_id, project, job, completion)
            await asyncio.sleep(0)
            return readers.public_reader_job(extensions().get(project_id, readers.JOB_KEY), reader_running(project_id))
        ids = reader_ids(project, body, 3)
        positions = {str(chapter.get("id")): index for index, chapter in enumerate(project["chapters"])}
        outcomes = await readers.simulate_chapters(
            project, [positions[item] for item in ids], completion=completion,
            personas=personas, compare_with_plan=body.compare_with_plan,
        )
        reports = []
        failure: Exception | None = None
        for outcome in outcomes:
            if isinstance(outcome, ValueError):
                failure = failure or outcome
            elif isinstance(outcome, BaseException):
                raise outcome
            else:
                reports.append(readers.store_report(extensions(), project_id, outcome))
        if failure is not None:
            raise HTTPException(422, str(failure))
        return {"reports": reports}

    @router.get("/api/projects/{project_id}/reader-simulation/job")
    async def reader_job(project_id: str) -> dict[str, Any]:
        project_or_404(project_id)
        return readers.public_reader_job(extensions().get(project_id, readers.JOB_KEY), reader_running(project_id))

    @router.post("/api/projects/{project_id}/reader-simulation/job/cancel")
    async def reader_job_cancel(project_id: str) -> dict[str, Any]:
        project_or_404(project_id)
        task = reader_tasks.get(project_id)
        if task and not task.done():
            def flag(current: Any) -> Any:
                if isinstance(current, dict):
                    current["cancel_requested"] = True
                    return current
                return None

            extensions().update(project_id, readers.JOB_KEY, flag)
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        return readers.public_reader_job(extensions().get(project_id, readers.JOB_KEY), reader_running(project_id))

    @router.post("/api/projects/{project_id}/reader-simulation/job/resume")
    async def reader_job_resume(project_id: str) -> dict[str, Any]:
        """Continue an interrupted (e.g. by a restart), cancelled or failed job.

        Chapters whose reports are already saved are skipped; the rest are
        read again with the job's original personas and options.
        """
        project = project_or_404(project_id)
        if reader_running(project_id):
            raise HTTPException(409, "这部作品已有读者模拟在运行")
        current = extensions().get(project_id, readers.JOB_KEY)
        try:
            job = readers.prepare_resume(current, readers.list_reports(extensions(), project_id))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        extensions().put(project_id, readers.JOB_KEY, job)
        launch_reader_job(project_id, project, job, structured_completion_provider())
        await asyncio.sleep(0)
        return readers.public_reader_job(extensions().get(project_id, readers.JOB_KEY), reader_running(project_id))

    @router.get("/api/projects/{project_id}/reader-reports")
    async def reader_reports(project_id: str, chapter_id: str = "") -> dict[str, Any]:
        project_or_404(project_id)
        return {"reports": readers.list_reports(extensions(), project_id, chapter_id)}

    # ------------------------------------------------------------ publishing

    @router.post("/api/projects/{project_id}/export/webnovel")
    async def export_webnovel(project_id: str, body: ExportRequest) -> dict[str, Any]:
        project = project_or_404(project_id)
        exporter = publishing.EXPORTERS.get(body.format)
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

    def schedule_for(project_id: str) -> dict[str, Any]:
        return publishing.normalize_schedule(extensions().get(project_id, publishing.SCHEDULE_KEY))

    @router.get("/api/projects/{project_id}/publishing")
    async def publishing_state(project_id: str, today: str = "") -> dict[str, Any]:
        project = project_or_404(project_id)
        day = None
        if today:
            try:
                day = date.fromisoformat(today)
            except ValueError as exc:
                raise HTTPException(422, "日期格式应为 YYYY-MM-DD") from exc
        return publishing.cadence_report(project, schedule_for(project_id), day)

    @router.put("/api/projects/{project_id}/publishing/schedule")
    async def publishing_schedule(project_id: str, body: ScheduleRequest) -> dict[str, Any]:
        project = project_or_404(project_id)
        current = schedule_for(project_id)
        incoming = dict(body.schedule)
        incoming["chapter_status"] = current["chapter_status"]
        try:
            schedule = publishing.normalize_schedule({**current, **incoming})
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        schedule["updated_at"] = publishing.utc_now()
        extensions().put(project_id, publishing.SCHEDULE_KEY, schedule)
        return publishing.cadence_report(project, schedule)

    @router.put("/api/projects/{project_id}/publishing/chapters/{chapter_id}")
    async def publishing_chapter(project_id: str, chapter_id: str, body: ChapterStatusRequest) -> dict[str, Any]:
        project = project_or_404(project_id)
        if not any(str(chapter.get("id")) == chapter_id for chapter in project["chapters"]):
            raise HTTPException(404, "章节不存在")
        try:
            schedule = publishing.set_chapter_status(schedule_for(project_id), chapter_id, body.status, body.published_at)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        extensions().put(project_id, publishing.SCHEDULE_KEY, schedule)
        return publishing.cadence_report(project, schedule)

    @router.post("/api/projects/{project_id}/publishing/bulk-status")
    async def publishing_bulk(project_id: str, body: BulkStatusRequest) -> dict[str, Any]:
        project = project_or_404(project_id)
        known = {str(chapter.get("id")) for chapter in project["chapters"]}
        schedule = schedule_for(project_id)
        try:
            for chapter_id in body.chapter_ids:
                if chapter_id in known:
                    schedule = publishing.set_chapter_status(schedule, chapter_id, body.status, body.published_at)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        extensions().put(project_id, publishing.SCHEDULE_KEY, schedule)
        return publishing.cadence_report(project, schedule)

    return router

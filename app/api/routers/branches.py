"""HTTP routes for 分支试写 (plot branch trials on one chapter).

Drafting runs as one background job per project whose progress lives in the
extension store (poll ``GET .../branches/job``, cancel with ``.../cancel``).
Collect / adopt / discard are pure transforms of the browser's project: they
return the updated project and the browser saves it, so they never race the
editor's optimistic saves.  Adopting is saved with a non-autosave reason,
which makes the store keep the replaced chapter as a chapter version.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.storage.schema.project import ensure_project_defaults
from app.storage.extension_store import ExtensionStore
from app.writing import branch_trials as trials
from app.llm.errors import planning_exception_detail
from app.writing.requests import ChapterActionRequest


class BranchChapterRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str


class BranchStartRequest(BranchChapterRequest):
    directions: list[Any] = Field(default_factory=list, max_length=trials.MAX_DIRECTIONS + 2)
    audit: bool = True
    judge: bool = True
    impact: bool = True


class BranchActionRequest(BranchChapterRequest):
    branch_id: str


def create_branch_router(
    *,
    store_provider: Callable[[], Any],
    find_chapter: Callable[[dict[str, Any], str], tuple[int, dict[str, Any]]],
    attach_indexed_retrieval: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    prepare_prose_build: Callable[[Any, dict[str, Any], dict[str, Any]], Any],
    structured_completion_provider: Callable[[], Callable[..., Any]],
    chapter_audit_provider: Callable[[], Callable[..., Any]],
) -> APIRouter:
    router = APIRouter(tags=["branches"])
    tasks: dict[str, asyncio.Task[Any]] = {}

    def extensions() -> ExtensionStore:
        return ExtensionStore.for_store(store_provider())

    def running(project_id: str) -> bool:
        task = tasks.get(project_id)
        return bool(task and not task.done())

    def view(project_id: str) -> dict[str, Any]:
        return trials.public_branch_job(extensions().get(project_id, trials.JOB_KEY), running(project_id))

    def body_project(project_id: str, body: BranchChapterRequest) -> dict[str, Any]:
        if store_provider().get(project_id) is None:
            raise HTTPException(404, "作品不存在")
        project = ensure_project_defaults(body.project)
        if str(project.get("id", "")) != project_id:
            raise HTTPException(409, "提交的作品与地址不一致，请刷新后重试")
        find_chapter(project, body.chapter_id)  # 404 + telemetry scope
        return project

    @router.post("/api/projects/{project_id}/branches/suggest")
    async def suggest(project_id: str, body: BranchChapterRequest) -> dict[str, Any]:
        project = body_project(project_id, body)
        try:
            return await trials.suggest_directions(project, body.chapter_id, structured_completion_provider())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface provider errors readably
            raise HTTPException(502, f"推荐走向失败：{planning_exception_detail(exc)}") from exc

    @router.post("/api/projects/{project_id}/branches/job")
    async def start(project_id: str, body: BranchStartRequest) -> dict[str, Any]:
        project = body_project(project_id, body)
        if running(project_id):
            raise HTTPException(409, "这部作品已有分支试写在运行")
        try:
            directions = trials.normalize_directions(body.directions)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        job = trials.new_branch_job(
            project, body.chapter_id, directions, audit=body.audit, judge=body.judge, impact=body.impact,
        )
        extensions().put(project_id, trials.JOB_KEY, job)
        completion = structured_completion_provider()
        audit_route = chapter_audit_provider()

        async def generate(snapshot: dict[str, Any], chapter_id: str, direction: str) -> dict[str, Any]:
            return await trials.generate_branch_prose(
                snapshot, chapter_id, direction,
                attach_retrieval=attach_indexed_retrieval,
                prepare_build=prepare_prose_build,
                rewrite_completion=completion,
            )

        async def audit(snapshot: dict[str, Any], chapter_id: str, draft: str) -> dict[str, Any]:
            index = trials.chapter_index(snapshot, chapter_id)
            # The branch replaces the chapter: audit it against an empty chapter.
            return await audit_route(ChapterActionRequest(
                project=trials.branch_prompt_project(snapshot, index), chapter_id=chapter_id,
                draft=draft, instruction="分支试写：评估这一剧情走向的成稿质量",
            ))

        async def run() -> None:
            try:
                await trials.run_branch_job(
                    project=project, extensions=extensions(), project_id=project_id, job=job,
                    completion=completion, generate=generate, audit=audit,
                )
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - persist for polling
                message = str(exc)[:300] or exc.__class__.__name__

                def fail(current: Any) -> Any:
                    if isinstance(current, dict) and current.get("id") == job["id"]:
                        current.update(status="failed", phase="done", message=f"试写中断：{message}")
                        current.setdefault("errors", []).append(message)
                        return current
                    return None

                extensions().update(project_id, trials.JOB_KEY, fail)
            finally:
                tasks.pop(project_id, None)

        tasks[project_id] = asyncio.create_task(run())
        await asyncio.sleep(0)
        return view(project_id)

    @router.get("/api/projects/{project_id}/branches/job")
    async def job_status(project_id: str) -> dict[str, Any]:
        if store_provider().get(project_id) is None:
            raise HTTPException(404, "作品不存在")
        return view(project_id)

    @router.post("/api/projects/{project_id}/branches/job/cancel")
    async def cancel(project_id: str) -> dict[str, Any]:
        if store_provider().get(project_id) is None:
            raise HTTPException(404, "作品不存在")
        task = tasks.get(project_id)
        if task and not task.done():
            def flag(current: Any) -> Any:
                if isinstance(current, dict):
                    current["cancel_requested"] = True
                    return current
                return None

            extensions().update(project_id, trials.JOB_KEY, flag)
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        return view(project_id)

    @router.post("/api/projects/{project_id}/branches/collect")
    async def collect(project_id: str, body: BranchChapterRequest) -> dict[str, Any]:
        project = body_project(project_id, body)
        job = extensions().get(project_id, trials.JOB_KEY)
        if not isinstance(job, dict) or str(job.get("chapter_id", "")) != body.chapter_id:
            return {"project": project, "added": 0}
        updated, added = trials.merge_job_into_chapter(project, job)
        return {"project": updated, "added": added}

    @router.post("/api/projects/{project_id}/branches/adopt")
    async def adopt(project_id: str, body: BranchActionRequest) -> dict[str, Any]:
        project = body_project(project_id, body)
        try:
            updated, report = trials.adopt_branch(project, body.chapter_id, body.branch_id)
        except KeyError as exc:
            raise HTTPException(404, "分支不存在，请先把试写结果收入本章") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"project": updated, "report": report}

    @router.post("/api/projects/{project_id}/branches/discard")
    async def discard(project_id: str, body: BranchActionRequest) -> dict[str, Any]:
        project = body_project(project_id, body)
        try:
            updated = trials.discard_branch(project, body.chapter_id, body.branch_id)
        except KeyError as exc:
            raise HTTPException(404, "分支不存在") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"project": updated}

    return router

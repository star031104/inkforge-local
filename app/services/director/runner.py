"""The director task runner: lease, phase dispatch, pause/failure handling, launch."""
from __future__ import annotations

import asyncio
from contextlib import suppress

from app.llm.client import ModelContentFilteredError
from app.llm.telemetry import bind_scope
from app.llm.errors import planning_exception_detail
from app.services.director.context import DirectorContext


def install(ctx: DirectorContext) -> None:
    """Define the runner closures on ``ctx``."""
    store = ctx.store
    director_runners = ctx.director_runners
    runtime_instance_id = ctx.runtime_instance_id
    _save_director_task = ctx._save_director_task
    _run_auto_refinement = ctx._run_auto_refinement
    _run_incubation_task = ctx._run_incubation_task
    _run_incubator_phase = ctx._run_incubator_phase
    _run_master_phase = ctx._run_master_phase
    _run_volumes_phase = ctx._run_volumes_phase
    _run_chapters_phase = ctx._run_chapters_phase

    async def _run_auto_director(task_id: str) -> None:
        task = store.get_director_task(task_id)
        if not task:
            return
        if not store.claim_director_task(task_id, runtime_instance_id):
            return
        # Attribute every model call of this run (also a resumed task) to its project.
        bind_scope(task.get("project_id"), "")

        async def keep_lease_alive() -> None:
            while True:
                await asyncio.sleep(15)
                if not store.renew_director_task_lease(
                    task_id, runtime_instance_id
                ):
                    return

        lease_heartbeat = asyncio.create_task(keep_lease_alive())
        try:
            task["status"] = "running"
            task = _save_director_task(task, "自动导演已启动")
            config = task["config"]
            project = store.get(task["project_id"])
            if not project:
                raise ValueError("自动导演作品已被删除")

            if task.get("task_type") == "incubation":
                await _run_incubation_task(task, project)
                return

            if task.get("phase") == "refinement":
                await _run_auto_refinement(task, project)
                return

            if task.get("phase") == "incubator":
                task = await _run_incubator_phase(task, project, config)

            if task.get("phase") == "master":
                task = await _run_master_phase(task, config)

            if task.get("phase") == "volumes":
                task = await _run_volumes_phase(task, config)

            if task.get("phase") == "chapters":
                await _run_chapters_phase(task, config)
        except asyncio.CancelledError:
            latest = store.get_director_task(task_id)
            if latest and latest.get("status") not in {"completed", "failed"}:
                latest["status"] = "paused"
                _save_director_task(latest, "自动导演已暂停，可从当前检查点继续", "warning")
            raise
        except Exception as exc:
            latest = store.get_director_task(task_id) or task
            latest["status"] = "paused"
            latest["failure_kind"] = (
                "content_filtered" if isinstance(exc, ModelContentFilteredError) else "hard"
            )
            if isinstance(exc, ModelContentFilteredError):
                current_project = store.get(latest["project_id"])
                latest["failure_project_updated_at"] = (
                    current_project.get("updated_at", "") if current_project else ""
                )
            latest["error"] = planning_exception_detail(exc)
            _save_director_task(
                latest,
                f"自动导演已暂停：{latest['error']}",
                "error",
            )
        finally:
            lease_heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await lease_heartbeat
            store.release_director_task_lease(task_id, runtime_instance_id)
            director_runners.pop(task_id, None)


    def _runtime_launch_director(task_id: str) -> None:
        running = director_runners.get(task_id)
        if running and not running.done():
            return
        director_runners[task_id] = asyncio.create_task(_run_auto_director(task_id))

    ctx.register(
        _run_auto_director,
        _runtime_launch_director,
    )

"""HTTP-facing director control: start, planned writing, refine, poll, pause, resume."""
from __future__ import annotations

import json
from typing import Any

from ...api.schemas import (
    AutoDirectorStartRequest,
    AutoRefineRequest,
    ContinuePlannedWritingRequest,
)
from ...db import ensure_project_defaults
from ...editorial_workflow import rebuild_repair_queue
from ...manuscript_quality import manuscript_health_report
from ...planning import apply_volume_routes
from ..editorial_policy import _director_manuscript_gate_failures
from .common import (
    DirectorControlError,
    _known_audit_score,
)
from .context import DirectorContext


def install(ctx: DirectorContext) -> None:
    """Define the control closures on ``ctx``."""
    store = ctx.store
    director_runners = ctx.director_runners
    _backup_directory = ctx._backup_directory
    _launch_director = ctx._launch_director
    _save_director_task = ctx._save_director_task
    _reconcile_director_quality_debts = ctx._reconcile_director_quality_debts
    _refinement_history = ctx._refinement_history

    def _reconcile_orphaned_director_task(task: dict[str, Any]) -> dict[str, Any]:
        """Turn a restart-left task into a resumable checkpoint.

        This must happen in the serving process, not in ``ProjectStore.__init__``.
        Database readers and test processes are allowed to open the same file while
        a director is running and must never pause that live job as a side effect.
        """
        if task.get("status") not in {"queued", "running", "stopping"}:
            return task
        runner = director_runners.get(str(task.get("id", "")))
        if runner is not None and not runner.done():
            return task
        if store.director_task_lease_active(str(task.get("id", ""))):
            return task
        task["status"] = "paused"
        return _save_director_task(
            task,
            "检测到服务重启遗留任务，已安全转为暂停；可从最后检查点继续",
            "warning",
        )


    async def director_start(body: AutoDirectorStartRequest) -> dict[str, Any]:
        source = ensure_project_defaults(body.source_project)
        store.backup(_backup_directory())
        created = store.create("AI 自动导演新作")
        created["settings"] = json.loads(json.dumps(source.get("settings", {}), ensure_ascii=False))
        created["style"] = json.loads(json.dumps(source.get("style", {}), ensure_ascii=False))
        created = store.save(created["id"], created, reason="director-created")
        config = {
            "seed": body.seed.strip(), "preferences": body.preferences.strip(),
            "story_mode": "short" if body.story_mode == "short" else "long",
            "target_chapters": int(body.target_chapters), "target_words": int(body.target_words),
            "preferred_volume_count": (
                int(body.preferred_volume_count) if body.preferred_volume_count else None
            ),
            "quality_threshold": int(body.quality_threshold),
            "max_revision_attempts": int(body.max_revision_attempts),
            "continue_on_quality_debt": bool(body.continue_on_quality_debt),
        }
        if body.critic_budget:  # otherwise audits follow the project setting
            config["critic_budget"] = body.critic_budget
        task = store.create_director_task(
            created["id"],
            {"phase": "incubator", "message": "等待自动导演启动", "config": config, "events": [], "completed_chapters": 0, "quality_debts": [], "planning_debts": []},
        )
        _launch_director(task["id"])
        return {"task": task, "project": created}


    async def director_write_planned(
        project_id: str, body: ContinuePlannedWritingRequest
    ) -> dict[str, Any]:
        project = store.get(project_id)
        if not project:
            raise DirectorControlError(404, "作品不存在")
        latest = store.latest_director_task(project_id)
        if latest and latest.get("status") in {"queued", "running", "stopping"}:
            raise DirectorControlError(409, "当前已有 AI 任务运行，请先等待完成或暂停")
        project = ensure_project_defaults(project)
        volumes = [
            item
            for item in project.get("planning", {}).get("volumes", [])
            if isinstance(item, dict)
        ]
        if not volumes:
            raise DirectorControlError(400, "还没有分卷规划，请先生成全书大规划")
        incomplete: list[str] = []
        for index, volume in enumerate(volumes, start=1):
            expected = max(
                0,
                int(volume.get("chapter_end", 0) or 0)
                - int(volume.get("chapter_start", 1) or 1)
                + 1,
            )
            routes = volume.get("chapters", [])
            if not isinstance(routes, list) or len(routes) != expected or expected <= 0:
                incomplete.append(str(volume.get("title") or f"第{index}卷"))
        if incomplete:
            raise DirectorControlError(
                400,
                "这些分卷还没有完整章节路线：" + "、".join(incomplete[:8]),
            )

        store.backup(_backup_directory())
        for volume in volumes:
            project = ensure_project_defaults(
                apply_volume_routes(project, str(volume.get("id", "")))
            )
        project = store.save(project_id, project, reason="planned-writing-start")
        chapters = [
            item for item in project.get("chapters", []) if isinstance(item, dict)
        ]
        first_unwritten = next(
            (
                index
                for index, chapter in enumerate(chapters)
                if not str(chapter.get("content", "")).strip()
            ),
            len(chapters),
        )
        if first_unwritten >= len(chapters):
            raise DirectorControlError(400, "规划中的章节已经全部有正文，可使用 AI 自动精修进行总检")

        config = {
            "seed": "",
            "preferences": "使用已经审核的全书规划和逐章路线继续写作",
            "story_mode": project.get("story_mode", "long"),
            "target_chapters": len(chapters),
            "target_words": int(
                project.get("settings", {}).get("target_words", 1200) or 1200
            ),
            "quality_threshold": int(body.quality_threshold),
            "max_revision_attempts": int(body.max_revision_attempts),
            "continue_on_quality_debt": bool(body.continue_on_quality_debt),
        }
        task = store.create_director_task(
            project_id,
            {
                "task_type": "planned_production",
                "phase": "chapters",
                "message": "规划和章节已保存，等待 AI 从第一章未完成正文继续创作",
                "config": config,
                "events": [],
                "chapter_index": first_unwritten,
                "completed_chapters": first_unwritten,
                "total_chapters": len(chapters),
                "quality_debts": [],
                "planning_debts": [],
            },
        )
        _launch_director(task["id"])
        return {"task": task, "project": project}


    async def director_refine(project_id: str, body: AutoRefineRequest) -> dict[str, Any]:
        project = store.get(project_id)
        if not project:
            raise DirectorControlError(404, "作品不存在")
        latest = store.latest_director_task(project_id)
        if latest and latest.get("status") in {"queued", "running"}:
            raise DirectorControlError(409, "当前已有 AI 自动任务运行，请等待完成或先暂停")
        project = ensure_project_defaults(project)
        rebuild_repair_queue(project)
        chapters = [item for item in project.get("chapters", []) if isinstance(item, dict)]
        existing_ids = {str(item.get("id", "")) for item in chapters}
        requested = [str(item) for item in body.chapter_ids if str(item) in existing_ids]
        if requested:
            targets = requested
        elif body.scope == "all":
            targets = [str(item.get("id", "")) for item in chapters if len(str(item.get("content", "")).strip()) >= 100]
        else:
            active_repairs = {
                str(item.get("chapter_id", ""))
                for item in project.get("repair_queue", [])
                if isinstance(item, dict)
                and str(item.get("status", "queued")) in {"queued", "in_progress"}
            }
            targets = [str(item.get("id", "")) for item in chapters if str(item.get("id", "")) in active_repairs]
        health = manuscript_health_report(project)
        manuscript_failures = _director_manuscript_gate_failures(
            health, final=True, genre=str(project.get("genre", ""))
        )
        history = _refinement_history(project_id)
        if history and not getattr(body, "force", False):
            last = history[-1]
            if (
                last.get("release_status") == "needs_author"
                and str(project.get("updated_at", "")) <= str(last.get("updated_at", ""))
            ):
                raise DirectorControlError(
                    409,
                    "上次自动精修已判定为无法继续收敛，作品自那以后没有人工修改。"
                    "请先处理未通过项（"
                    + "；".join(str(item) for item in last.get("failures", []))
                    + "），或选择强制继续。",
                )
        manuscript_focus: dict[str, str] = {}
        if manuscript_failures and not requested:
            # A global gate (jargon totals, fatigue, duplicates) can only move if
            # the chapters that contribute most to it are revised.
            for row in health.get("chapter_contributions", [])[:5]:
                chapter_id = str(row.get("chapter_id", ""))
                if chapter_id not in existing_ids:
                    continue
                parts = []
                if row.get("jargon"):
                    parts.append(f"现代抽象术语约 {row['jargon']} 处（改为当时人物能看见、争论和付出代价的具体事物）")
                if row.get("fatigue"):
                    parts.append(f"全书疲劳词 {row['fatigue']} 处（换成新的动作、感官细节或直接删去）")
                if row.get("duplicates"):
                    parts.append(f"与前文重复的段落 {row['duplicates']} 处（删除重复，改写为新的推进）")
                if parts:
                    manuscript_focus[chapter_id] = "全稿门禁：本章贡献了" + "；".join(parts) + "。"
                    targets.append(chapter_id)
        targets = list(dict.fromkeys(targets))
        if not targets:
            raise DirectorControlError(400, "没有可自动精修的章节；可选择检查全部已有正文")
        store.backup(_backup_directory())
        project = store.save(project_id, project, reason="auto-refine-start")
        queue_by_chapter: dict[str, list[dict[str, Any]]] = {}
        for item in project.get("repair_queue", []):
            if isinstance(item, dict) and str(item.get("status", "queued")) in {"queued", "in_progress"}:
                queue_by_chapter.setdefault(str(item.get("chapter_id", "")), []).append(item)
        chapter_map = {str(item.get("id", "")): (index + 1, item) for index, item in enumerate(chapters)}
        debts = []
        for chapter_id in targets:
            number, chapter = chapter_map[chapter_id]
            issues = [
                {
                    "severity": str(item.get("severity", "medium")),
                    "category": str(item.get("category", "质量问题")),
                    "message": str(item.get("message", "待精修")),
                    "suggestion": str(item.get("suggestion", "")),
                    "source": str(item.get("source", "repair_queue")),
                }
                for item in queue_by_chapter.get(chapter_id, [])
            ]
            if chapter_id in manuscript_focus:
                issues.append({
                    "severity": "medium", "category": "全稿门禁",
                    "message": manuscript_focus[chapter_id],
                    "suggestion": "定点改写贡献处，保留情节事实。",
                    "source": "manuscript_gate",
                })
            debts.append(
                {"chapter": number, "chapter_id": chapter_id, "title": chapter.get("title", ""),
                 "score": _known_audit_score(chapter), "issues": issues}
            )
        config = {
            "quality_threshold": int(body.quality_threshold),
            "max_revision_attempts": int(body.max_revision_attempts),
            "continue_on_quality_debt": True,
            "target_words": int(project.get("settings", {}).get("target_words", 1200) or 1200),
            "instruction": body.instruction.strip(),
            "scope": "all" if body.scope == "all" else "repairs",
        }
        task = store.create_director_task(
            project_id,
            {
                "task_type": "refinement", "phase": "refinement",
                "message": "等待 AI 全书精修启动", "config": config,
                "events": [], "completed_chapters": 0, "total_chapters": len(targets),
                "refine_index": 0, "refinement_targets": targets,
                "manuscript_focus": manuscript_focus,
                "manuscript_failures_at_start": manuscript_failures,
                "quality_debts": debts, "planning_debts": [],
                "seed_candidates": {
                    chapter_id: candidate
                    for chapter_id, candidate in {
                        **(
                            latest.get("seed_candidates", {})
                            if isinstance(latest, dict)
                            and isinstance(latest.get("seed_candidates"), dict)
                            else {}
                        ),
                        **(
                            latest.get("refinement_candidates", {})
                            if isinstance(latest, dict)
                            and isinstance(latest.get("refinement_candidates"), dict)
                            else {}
                        ),
                        **(
                            {
                                str(latest.get("last_rejected_candidate", {}).get("chapter_id", "")):
                                latest.get("last_rejected_candidate", {})
                            }
                            if isinstance(latest, dict)
                            and isinstance(latest.get("last_rejected_candidate"), dict)
                            and str(latest.get("last_rejected_candidate", {}).get("chapter_id", ""))
                            else {}
                        ),
                    }.items()
                    if chapter_id in targets
                    and isinstance(candidate, dict)
                    and len(str(candidate.get("draft", "")).strip()) >= 100
                },
            },
        )
        _launch_director(task["id"])
        return {"task": task, "project": project}


    async def director_task(task_id: str) -> dict[str, Any]:
        task = store.get_director_task(task_id)
        if not task:
            raise DirectorControlError(404, "自动导演任务不存在")
        task = _reconcile_orphaned_director_task(task)
        return _reconcile_director_quality_debts(task)


    async def director_latest(project_id: str) -> dict[str, Any]:
        task = store.latest_director_task(project_id)
        return (
            _reconcile_director_quality_debts(
                _reconcile_orphaned_director_task(task)
            )
            if task
            else {"status": "none", "project_id": project_id}
        )


    async def director_pause(task_id: str) -> dict[str, Any]:
        task = store.get_director_task(task_id)
        if not task:
            raise DirectorControlError(404, "自动导演任务不存在")
        if task.get("status") in {"completed", "failed"}:
            return task
        task["status"] = "paused"
        task = _save_director_task(task, "正在暂停自动导演…", "warning")
        runner = director_runners.get(task_id)
        if runner and not runner.done():
            runner.cancel()
        return task


    async def director_resume(task_id: str) -> dict[str, Any]:
        task = store.get_director_task(task_id)
        if not task:
            raise DirectorControlError(404, "自动导演任务不存在")
        if task.get("status") == "completed":
            return task
        if task.get("failure_kind") == "content_filtered":
            current_project = store.get(task["project_id"])
            if current_project and current_project.get("updated_at") == task.get(
                "failure_project_updated_at"
            ):
                raise DirectorControlError(
                    409,
                    "模型服务已拦截本章正文。请先调整本章路线或更换正文模型并保存，再从检查点继续。",
                )
        task["status"] = "queued"
        task.pop("error", None)
        task.pop("failure_kind", None)
        task.pop("failure_project_updated_at", None)
        task["checkpoint_message"] = ""
        task = _save_director_task(task, "任务已进入恢复队列")
        _launch_director(task_id)
        return task

    ctx.register(
        _reconcile_orphaned_director_task,
        director_start,
        director_write_planned,
        director_refine,
        director_task,
        director_latest,
        director_pause,
        director_resume,
    )

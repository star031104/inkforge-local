"""Per-chapter bookkeeping: memory settlement/repair, experience bank, hook ledger, rolling re-plan."""
from __future__ import annotations

import asyncio
from typing import Any

from app.writing.requests import ChapterActionRequest
from app.core.clock import utc_now
from app.review.editorial_workflow import incomplete_memory_stages
from app.story.memory_extraction import local_memory_result
from app.storage.project_service import ProjectConflictError
from app.llm.providers import settings_for_workload
from app.writing.craft_settings import craft_setting
from app.review.experience_bank import experience_hints, experience_summary, record_audit_issues
from app.story.hook_ledger import outstanding_debts, record_chapter_hook, settle_hooks
from app.story.memory_extraction import memory_time_budget as _memory_time_budget
from app.llm.errors import planning_exception_detail
from app.planning.rolling_replan import apply_replan_changes, rolling_replan
from app.director.common import (
    MEMORY_REPAIR_POLL_SECONDS,
    MEMORY_REPAIR_SECONDS,
    TaskStoppedError,
    _director_memory_timeout,
    experience_summary_rows,
    run_while_running,
)
from app.director.context import DirectorContext


def install(ctx: DirectorContext) -> None:
    """Define the support closures on ``ctx``."""
    store = ctx.store
    chapter_memory = ctx.chapter_memory
    run_structured_completion = ctx.run_structured_completion
    repair_incomplete_memory = ctx.repair_incomplete_memory
    _director_event = ctx._director_event
    _task_still_running = ctx._task_still_running
    _record_task_event = ctx._record_task_event

    def _writer_model(project: dict[str, Any]) -> str:
        return str(settings_for_workload(project.get("settings", {}), "prose").get("model", ""))

    def _director_experience_hints(project: dict[str, Any], config: dict[str, Any] | None) -> list[str]:
        """Experience-bank lessons for the writing model (per-chapter prompt only)."""
        if not craft_setting(project, "experience_bank", config):
            return []
        return [
            str(item) for item in experience_hints(project, _writer_model(project), limit=3)
            if str(item).strip()
        ]

    async def _director_repair_memory(
        task: dict[str, Any], project: dict[str, Any], chapter: dict[str, Any]
    ) -> None:
        """One in-place re-extraction of memory stages that failed; never fatal.

        Runs on the director's own in-memory project (saved by the caller), so
        it cannot race a browser save; a pause/stop is honoured within
        ``MEMORY_REPAIR_POLL_SECONDS`` instead of after the whole budget.
        """
        stages = incomplete_memory_stages(chapter)
        if not stages or not _task_still_running(task):
            return
        try:
            with _memory_time_budget(MEMORY_REPAIR_SECONDS):
                report = await run_while_running(
                    repair_incomplete_memory(
                        project, str(chapter.get("id", "")), completion=run_structured_completion,
                    ),
                    lambda: _task_still_running(task),
                    timeout=MEMORY_REPAIR_SECONDS + 15,
                    poll=MEMORY_REPAIR_POLL_SECONDS,
                )
        except TaskStoppedError:
            return  # the stop is surfaced by the caller's next status check
        except Exception as exc:  # noqa: BLE001 - the repair queue keeps the item
            _director_event(
                task,
                f"《{chapter.get('title', '')}》记忆阶段补提取失败，已留在精修队列：{planning_exception_detail(exc)}",
                "warning",
            )
            return
        status = str(report.get("status", ""))
        remaining = list(report.get("remaining", []) or [])
        _director_event(
            task,
            (
                f"《{chapter.get('title', '')}》已补齐记忆阶段：{'、'.join(stages)}"
                if status == "repaired"
                else f"《{chapter.get('title', '')}》记忆阶段仍未完成：{'、'.join(remaining) or '、'.join(stages)}"
            ),
            "success" if status == "repaired" else "warning",
        )

    def _director_record_experience(
        task: dict[str, Any], project: dict[str, Any], audit: dict[str, Any], chapter_id: str
    ) -> None:
        """Feed verified AI findings and local-check issues to the experience bank."""
        if not craft_setting(project, "experience_bank", task.get("config")):
            return
        local = audit.get("local_checks", {}) if isinstance(audit.get("local_checks"), dict) else {}
        issues = [
            item for item in [*(audit.get("issues") or []), *(local.get("issues") or [])]
            if isinstance(item, dict)
        ]
        model = _writer_model(project)
        record_audit_issues(project, model, issues, chapter_id=chapter_id)
        task["experience_summary"] = experience_summary_rows(experience_summary(project, model))

    def _director_after_audit(
        task: dict[str, Any], audit: dict[str, Any], project: dict[str, Any], chapter_id: str,
    ) -> str:
        """Feed the experience bank, expose the critic risk gate; return an event suffix."""
        try:
            _director_record_experience(task, project, audit, chapter_id)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must never stop a run
            _director_event(task, f"经验库记录跳过：{exc}", "warning")
        risk = audit.get("risk") if isinstance(audit.get("risk"), dict) else None
        if not risk:
            return ""
        task["last_critic_risk"] = {
            "score": risk.get("score"), "level": risk.get("level"),
            "reasons": list(risk.get("reasons", []))[:6],
            "budget_used": risk.get("budget_used"), "calls": risk.get("calls"),
        }
        budget = {"merged": "合并审计 1 次", "full": "分类审计", "merged→full": "合并审计后升级为分类审计"}.get(
            str(risk.get("budget_used", "")), str(risk.get("budget_used", ""))
        )
        return f"（风险 {risk.get('score')}，{budget}）"

    def _director_settle_hooks(
        task: dict[str, Any], project: dict[str, Any], chapter: dict[str, Any],
        index: int, draft: str, audit: dict[str, Any],
    ) -> None:
        """Hook ledger: judge earlier hooks, record this chapter's own hook."""
        config = task.get("config")
        if not craft_setting(project, "hook_ledger", config):
            return
        settled = settle_hooks(project, index + 1, draft, audit.get("hook_status"))
        record_chapter_hook(
            project, chapter, index + 1, window=int(craft_setting(project, "hook_window", config))
        )
        debts = outstanding_debts(project, index + 2, limit=10)
        task["hook_debts"] = [
            {"id": item["id"], "origin_chapter": item["origin_chapter"], "text": item["text"]}
            for item in debts
        ]
        paid = [item for item in settled if item["status"] == "paid"]
        if paid or debts:
            _director_event(
                task,
                f"第 {index + 1} 章钩子账本：兑现 {len(paid)} 个，未承接的钩子债 {len(debts)} 个",
                "warning" if debts else "info",
            )
        ending = audit.get("hook_ending") if isinstance(audit.get("hook_ending"), dict) else {}
        if ending and not ending.get("ok", True):
            _director_event(task, f"第 {index + 1} 章：{ending.get('reason', '章末钩子偏弱')}", "warning")

    async def _director_rolling_replan(
        task: dict[str, Any], project: dict[str, Any], index: int
    ) -> dict[str, Any]:
        """Re-plan the next unwritten routes from what actually happened."""
        if not craft_setting(project, "rolling_replan", task.get("config")):
            return project
        try:
            updated, report = await rolling_replan(
                project,
                index,
                project.get("settings", {}),
                run_structured_completion,
                window=int(craft_setting(project, "rolling_replan_window", task.get("config"))),
            )
        except Exception as exc:  # noqa: BLE001 - keep original routes
            _record_task_event(task, f"滚动重规划跳过：{planning_exception_detail(exc)}", "warning")
            return project
        for warning in report.get("warnings", []):
            _record_task_event(task, str(warning), "warning")
        if not report.get("changes"):
            return project
        reason = f"director-rolling-replan-{index + 1}"
        try:
            project = store.save(
                project["id"], updated, reason=reason,
                expected_updated_at=str(project.get("updated_at", "")) or None,
            )
        except ProjectConflictError:
            # The author (or another task) saved while the planner was thinking:
            # replay only diffs whose fields are still untouched onto the latest
            # version, and never over routes the author has since edited.
            latest_project = store.get(project["id"]) or project
            merged, applied = apply_replan_changes(latest_project, report["changes"], index)
            if not applied:
                _record_task_event(task, "滚动重规划期间作品已被修改，本次路线调整已放弃", "warning")
                return latest_project
            try:
                project = store.save(
                    project["id"], merged, reason=reason,
                    expected_updated_at=str(latest_project.get("updated_at", "")) or None,
                )
            except ProjectConflictError:
                _record_task_event(task, "滚动重规划期间作品反复被修改，本次路线调整已放弃", "warning")
                return store.get(project["id"]) or latest_project
            report["changes"] = applied
        latest = store.get_director_task(task["id"]) or task
        history = latest.setdefault("replan_history", [])
        history.append({"after_chapter": index + 1, "changes": report["changes"], "at": utc_now()})
        latest["replan_history"] = history[-30:]
        _director_event(
            latest,
            "滚动重规划：已根据第 {0} 章实际结果调整 {1}".format(
                index + 1,
                "、".join(
                    f"第{item['chapter']}章（{'/'.join(item['fields'])}）" for item in report["changes"]
                ),
            ),
            "success",
        )
        saved = store.save_director_task(task["id"], latest)
        task.clear()
        task.update(saved)
        return project

    async def _director_chapter_memory(
        project: dict[str, Any], chapter: dict[str, Any], draft: str
    ) -> dict[str, Any]:
        timeout = _director_memory_timeout()
        try:
            # The inner staged extractor gets a slightly shorter budget so it
            # returns finished stages instead of being cancelled by wait_for.
            with _memory_time_budget(max(30.0, timeout - 15.0)):
                return await asyncio.wait_for(
                    chapter_memory(
                        ChapterActionRequest(
                            project=project, chapter_id=chapter["id"], draft=draft,
                            instruction="自动导演状态回灌",
                        )
                    ),
                    timeout=timeout,
                )
        except asyncio.TimeoutError:
            return local_memory_result(
                project, chapter, draft,
                f"完整记忆提取超过 {int(timeout)} 秒；已保留正文并使用本地基础摘要",
            )

    ctx.register(
        _writer_model,
        _director_experience_hints,
        _director_repair_memory,
        _director_record_experience,
        _director_after_audit,
        _director_settle_hooks,
        _director_rolling_replan,
        _director_chapter_memory,
    )

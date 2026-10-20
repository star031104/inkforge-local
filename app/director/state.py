"""Director task persistence: events, checkpoint saves, planning/quality debts."""
from __future__ import annotations

from typing import Any

from app.core.clock import utc_now
from app.director.context import DirectorContext


def install(ctx: DirectorContext) -> None:
    """Define the state closures on ``ctx``."""
    store = ctx.store
    runtime_instance_id = ctx.runtime_instance_id

    def _director_event(task: dict[str, Any], message: str, kind: str = "info") -> None:
        task.setdefault("events", []).append(
            {"time": utc_now(), "kind": kind, "message": message}
        )
        task["events"] = task["events"][-100:]
        task["message"] = message


    def _save_director_task(
        task: dict[str, Any], message: str | None = None, kind: str = "info"
    ) -> dict[str, Any]:
        if message:
            _director_event(task, message, kind)
        saved = store.save_director_task(task["id"], task)
        if saved.get("status") in {"queued", "running", "stopping"}:
            store.renew_director_task_lease(task["id"], runtime_instance_id)
        return saved


    def _record_planning_debt(
        task: dict[str, Any], debt: dict[str, Any]
    ) -> None:
        debts = task.setdefault("planning_debts", [])
        if not isinstance(debts, list):
            debts = []
            task["planning_debts"] = debts
        identity = (
            str(debt.get("phase", "")),
            str(debt.get("volume_id", "")),
            int(debt.get("chapter", 0) or 0),
        )
        debts[:] = [
            item for item in debts
            if (
                str(item.get("phase", "")),
                str(item.get("volume_id", "")),
                int(item.get("chapter", 0) or 0),
            ) != identity
        ]
        debts.append({"time": utc_now(), **debt})
        task["planning_debts"] = debts[-120:]


    def _quality_debt_index(task: dict[str, Any], chapter_id: str) -> int:
        debts = task.get("quality_debts", [])
        if not isinstance(debts, list):
            return -1
        return next(
            (
                index
                for index, item in enumerate(debts)
                if isinstance(item, dict)
                and str(item.get("chapter_id", "")) == str(chapter_id)
            ),
            -1,
        )


    def _remove_quality_debt(task: dict[str, Any], chapter_id: str) -> bool:
        debts = task.get("quality_debts", [])
        if not isinstance(debts, list):
            task["quality_debts"] = []
            return False
        kept = [
            item
            for item in debts
            if not isinstance(item, dict)
            or str(item.get("chapter_id", "")) != str(chapter_id)
        ]
        changed = len(kept) != len(debts)
        task["quality_debts"] = kept
        return changed


    def _reconcile_director_quality_debts(task: dict[str, Any]) -> dict[str, Any]:
        """Keep persisted director debt scores aligned with the latest chapter run."""
        project = store.get(str(task.get("project_id", "")))
        debts = task.get("quality_debts", [])
        if not project or not isinstance(debts, list) or not debts:
            return task
        chapters = {
            str(item.get("id", "")): item
            for item in project.get("chapters", [])
            if isinstance(item, dict)
        }
        reconciled: list[dict[str, Any]] = []
        changed = False
        seen: set[str] = set()
        for item in debts:
            if not isinstance(item, dict):
                changed = True
                continue
            chapter_id = str(item.get("chapter_id", ""))
            if chapter_id in seen:
                changed = True
                continue
            seen.add(chapter_id)
            chapter = chapters.get(chapter_id, {})
            execution = chapter.get("execution", {}) if isinstance(chapter, dict) else {}
            if not isinstance(execution, dict):
                execution = {}
            if str(execution.get("status", "")) == "accepted":
                changed = True
                continue
            current = dict(item)
            # ``audit_score`` is None until the chapter has actually been
            # audited and accepted/reviewed; treating that as 0 used to wipe
            # the real score of a debt that is still waiting for the author.
            if execution.get("audit_score") is not None and str(execution.get("status", "")) != "never_run":
                score = max(0, min(100, int(execution.get("audit_score", 0) or 0)))
                if int(current.get("score", 0) or 0) != score:
                    current["score"] = score
                    changed = True
            reconciled.append(current)
        if changed:
            task["quality_debts"] = reconciled
            # Polling a live task must remain read-only: saving a just-read payload
            # here could race the runner and overwrite a newer checkpoint.
            if task.get("status") not in {"queued", "running", "stopping"}:
                task = store.save_director_task(task["id"], task)
        return task

    def _task_still_running(task: dict[str, Any]) -> bool:
        try:
            latest = store.get_director_task(task["id"])
        except Exception:  # noqa: BLE001 - an unreadable task row never blocks the run
            return True
        return bool(latest is None or latest.get("status") == "running")

    def _record_task_event(task: dict[str, Any], message: str, level: str = "info") -> None:
        latest = store.get_director_task(task["id"])
        if latest and latest.get("status") == "running":
            _director_event(latest, message, level)
            saved = store.save_director_task(task["id"], latest)
            task.clear()
            task.update(saved)

    ctx.register(
        _director_event,
        _save_director_task,
        _record_planning_debt,
        _quality_debt_index,
        _remove_quality_debt,
        _reconcile_director_quality_debts,
        _task_still_running,
        _record_task_event,
    )

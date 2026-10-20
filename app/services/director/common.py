"""Module-level director pieces: constants, shared types and pure helpers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable

from app.services.memory_extraction import MEMORY_STAGES as _MEMORY_STAGES

# Each staged memory call may take up to 180 s; six serial stages under a
# single 180 s outer timeout used to discard every finished stage.
MEMORY_STAGE_SECONDS = 180.0
# Budget for one in-place re-extraction of failed memory stages.
MEMORY_REPAIR_SECONDS = 150.0
# How often an in-place memory repair checks whether the task was paused.
MEMORY_REPAIR_POLL_SECONDS = 5.0
MEMORY_TIMEOUT_CAP_SECONDS = 900.0


CRAFT_OVERRIDE_KEYS = ("critic_budget", "critic_risk_threshold")


def _craft_overrides(task: dict[str, Any]) -> dict[str, Any]:
    """Craft settings a director task config may override for its audits."""
    config = task.get("config", {}) if isinstance(task.get("config"), dict) else {}
    return {key: config[key] for key in CRAFT_OVERRIDE_KEYS if config.get(key) is not None}


def experience_summary_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
    """``experience_summary()`` → ``[{category, count, hint}]`` for the director UI."""
    rows = []
    for pattern in summary.get("patterns", []) or []:
        if not isinstance(pattern, dict):
            continue
        label = str(pattern.get("label", "") or pattern.get("kind", "") or "其他")
        rows.append({
            "category": label,
            "count": int(pattern.get("count", 0) or 0),
            "hint": str(pattern.get("hint", "") or ""),
        })
    return rows


def _known_audit_score(chapter: dict[str, Any]) -> int | None:
    """The chapter's real audit score, or ``None`` when it was never audited."""
    execution = chapter.get("execution", {}) if isinstance(chapter.get("execution"), dict) else {}
    value = execution.get("audit_score")
    if value is None or value == "":
        return None
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return None


def _director_memory_timeout() -> float:
    return min(MEMORY_TIMEOUT_CAP_SECONDS, MEMORY_STAGE_SECONDS * max(1, len(_MEMORY_STAGES)))


class _StoreProxy:
    """Resolve the active store on every access so tests and app swaps remain safe."""

    def __init__(self, provider: Callable[[], Any]):
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider(), name)


class _LiveDependency:
    """Resolve an explicitly typed callback lazily for tests and hot swaps."""

    def __init__(self, provider: Callable[[], Callable[..., Any]]):
        self._provider = provider

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._provider()(*args, **kwargs)


@dataclass(frozen=True)
class DirectorDependencies:
    attach_indexed_retrieval: Callable[[], Callable[..., Any]]
    persist_context_snapshot: Callable[[], Callable[..., Any]]
    complete_incubator_option: Callable[[], Callable[..., Any]]
    generate_incubator_core: Callable[[], Callable[..., Any]]
    director_full_outline: Callable[[], Callable[..., Any]]
    chapter_audit: Callable[[], Callable[..., Any]]
    chapter_memory: Callable[[], Callable[..., Any]]
    chapter_plan: Callable[[], Callable[..., Any]]
    director_character_card: Callable[[], Callable[..., Any]]
    director_master_bible: Callable[[], Callable[..., Any]]
    director_master_contract: Callable[[], Callable[..., Any]]
    director_master_volume_core: Callable[[], Callable[..., Any]]
    director_master_volume_details: Callable[[], Callable[..., Any]]
    director_plan_chapter_route: Callable[[], Callable[..., Any]]
    director_seed_brief: Callable[[], Callable[..., Any]]
    director_seed_cast: Callable[[], Callable[..., Any]]
    director_seed_world: Callable[[], Callable[..., Any]]
    find_chapter: Callable[[], Callable[..., Any]]
    launch_director: Callable[[], Callable[..., Any]]


@dataclass(frozen=True)
class DirectorRuntime:
    runners: dict[str, asyncio.Task[None]]
    run_auto_director: Callable[[str], Any]
    launch_director: Callable[[str], None]
    director_event: Callable[..., None]
    remove_quality_debt: Callable[..., bool]
    start: Callable[..., Any]
    write_planned: Callable[..., Any]
    refine: Callable[..., Any]
    get_task: Callable[..., Any]
    latest: Callable[..., Any]
    pause: Callable[..., Any]
    resume: Callable[..., Any]


class TaskStoppedError(RuntimeError):
    """The director task was paused or stopped while a step was waiting."""


async def run_while_running(
    coro: Any, is_running: Callable[[], bool], *, timeout: float, poll: float
) -> Any:
    """Await ``coro`` but cancel it as soon as ``is_running()`` turns false.

    Raises ``TaskStoppedError`` on a stop and ``asyncio.TimeoutError`` after
    ``timeout`` seconds; the inner task is always cancelled and awaited.
    The task is created inside the caller's context, so context-variable
    budgets (``memory_time_budget``) still apply.
    """
    job = asyncio.ensure_future(coro)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    try:
        while not job.done():
            left = deadline - loop.time()
            if left <= 0:
                raise asyncio.TimeoutError(f"超过 {int(timeout)} 秒")
            await asyncio.wait({job}, timeout=min(poll, left))
            if not job.done() and not is_running():
                raise TaskStoppedError("任务已暂停")
        return job.result()
    finally:
        if not job.done():
            job.cancel()
            try:
                await job
            except BaseException:  # noqa: BLE001 - cancelled on purpose
                pass


class DirectorControlError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail

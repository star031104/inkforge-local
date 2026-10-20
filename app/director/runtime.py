"""Wire the director modules into one :class:`DirectorRuntime`."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Mapping

from app.director import (
    chapters,
    control,
    incubator,
    master,
    prose,
    refinement,
    runner,
    state,
    support,
)
from app.director.common import DirectorDependencies, DirectorRuntime, _LiveDependency, _StoreProxy
from app.director.context import DirectorContext

INSTALL_ORDER = (state, support, prose, refinement, incubator, master, chapters, runner, control)

# DirectorDependencies field → name used inside the director closures.
DEPENDENCY_NAMES = {
    "attach_indexed_retrieval": "_attach_indexed_retrieval",
    "persist_context_snapshot": "_persist_context_snapshot",
    "complete_incubator_option": "_complete_incubator_option",
    "generate_incubator_core": "_generate_incubator_core",
    "director_full_outline": "_director_full_outline",
    "chapter_audit": "chapter_audit",
    "chapter_memory": "chapter_memory",
    "chapter_plan": "chapter_plan",
    "director_character_card": "director_character_card",
    "director_master_bible": "director_master_bible",
    "director_master_contract": "director_master_contract",
    "director_master_volume_core": "director_master_volume_core",
    "director_master_volume_details": "director_master_volume_details",
    "director_plan_chapter_route": "director_plan_chapter_route",
    "director_seed_brief": "director_seed_brief",
    "director_seed_cast": "director_seed_cast",
    "director_seed_world": "director_seed_world",
    "find_chapter": "find_chapter",
    "launch_director": "_launch_director",
}


def build_director_runtime(
    *,
    store_provider: Callable[[], Any],
    backup_directory: Path | Callable[[], Path],
    dependencies: DirectorDependencies,
    model_calls: Mapping[str, Callable[..., Any]],
) -> DirectorRuntime:
    """Build one runtime; ``model_calls`` are late-bound model/gate callables."""
    store = _StoreProxy(store_provider)

    def _backup_directory() -> Path:
        return backup_directory() if callable(backup_directory) else backup_directory

    ctx = DirectorContext(
        store=store,
        director_runners={},
        runtime_instance_id=str(store.instance_id),
        _backup_directory=_backup_directory,
        **{
            local: _LiveDependency(getattr(dependencies, field))
            for field, local in DEPENDENCY_NAMES.items()
        },
        **dict(model_calls),
    )
    for module in INSTALL_ORDER:
        module.install(ctx)
    runners: dict[str, asyncio.Task[None]] = ctx.director_runners
    return DirectorRuntime(
        runners=runners,
        run_auto_director=ctx._run_auto_director,
        launch_director=ctx._runtime_launch_director,
        director_event=ctx._director_event,
        remove_quality_debt=ctx._remove_quality_debt,
        start=ctx.director_start,
        write_planned=ctx.director_write_planned,
        refine=ctx.director_refine,
        get_task=ctx.director_task,
        latest=ctx.director_latest,
        pause=ctx.director_pause,
        resume=ctx.director_resume,
    )

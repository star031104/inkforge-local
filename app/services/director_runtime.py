"""Persistent automatic-director runtime and HTTP control surface.

The implementation lives in :mod:`app.services.director`; this module is the
stable public import surface.  The model/gate callables below are looked up
here on every call, so ``monkeypatch.setattr(director_runtime, "chat_stream",
...)`` (and the other names in ``_LATE_BOUND``) keeps working.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..domain.prose import _director_candidate_gate_failures
from ..llama_client import chat_once, chat_stream
from .director.common import (
    _MEMORY_STAGES,
    CRAFT_OVERRIDE_KEYS,
    MEMORY_REPAIR_POLL_SECONDS,
    MEMORY_REPAIR_SECONDS,
    MEMORY_STAGE_SECONDS,
    MEMORY_TIMEOUT_CAP_SECONDS,
    DirectorControlError,
    DirectorDependencies,
    DirectorRuntime,
    TaskStoppedError,
    _craft_overrides,
    _director_memory_timeout,
    _known_audit_score,
    _LiveDependency,
    _StoreProxy,
    experience_summary_rows,
    run_while_running,
)
from .director.runtime import build_director_runtime
from .memory_settlement import repair_incomplete_memory
from .structured_output import structured_completion as run_structured_completion

__all__ = [
    "CRAFT_OVERRIDE_KEYS",
    "MEMORY_REPAIR_POLL_SECONDS",
    "MEMORY_REPAIR_SECONDS",
    "MEMORY_STAGE_SECONDS",
    "MEMORY_TIMEOUT_CAP_SECONDS",
    "DirectorControlError",
    "DirectorDependencies",
    "DirectorRuntime",
    "TaskStoppedError",
    "_LiveDependency",
    "_MEMORY_STAGES",
    "_StoreProxy",
    "_craft_overrides",
    "_director_candidate_gate_failures",
    "_director_memory_timeout",
    "_known_audit_score",
    "chat_once",
    "chat_stream",
    "create_director_runtime",
    "experience_summary_rows",
    "repair_incomplete_memory",
    "run_structured_completion",
    "run_while_running",
]

# Resolved through this module's globals at call time (test patch points).
_LATE_BOUND = (
    "chat_stream",
    "chat_once",
    "run_structured_completion",
    "_director_candidate_gate_failures",
    "repair_incomplete_memory",
)


def _late_bound(name: str) -> _LiveDependency:
    return _LiveDependency(lambda: globals()[name])


def create_director_runtime(
    *,
    store_provider: Callable[[], Any],
    backup_directory: Path | Callable[[], Path],
    dependencies: DirectorDependencies,
) -> DirectorRuntime:
    """Build one director runtime with explicit access to application dependencies."""
    return build_director_runtime(
        store_provider=store_provider,
        backup_directory=backup_directory,
        dependencies=dependencies,
        model_calls={name: _late_bound(name) for name in _LATE_BOUND},
    )

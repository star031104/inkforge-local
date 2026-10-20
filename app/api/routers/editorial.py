"""Chapter acceptance, audit, repair, contracts, and memory transport.

The routes live in ``editorial_memory`` (acceptance, contracts, repairs,
memory extraction/apply, manuscript health) and ``editorial_audit`` (chapter
audit, quality); shared helpers in ``editorial_support``.
``repair_incomplete_memory`` is looked up on this module at call time so tests
can monkeypatch it here.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter

from app.services.memory_settlement import repair_incomplete_memory
from app.api.routers.editorial_audit import install_audit_routes
from app.api.routers.editorial_memory import install_memory_routes, install_memory_state_routes
from app.api.routers.editorial_support import (
    EditorialContext,
    EditorialRoutes,
    _LiveCallable,
    _StoreProxy,
    critic_risk_signals,
    new_named_entities,
)

__all__ = [
    "EditorialRoutes",
    "create_editorial_routes",
    "critic_risk_signals",
    "new_named_entities",
    "repair_incomplete_memory",
]


def create_editorial_routes(
    *,
    store_provider: Callable[[], Any],
    find_chapter_callback: Callable[..., Any],
    indexed_retrieval_callback: Callable[..., Any],
    persist_managed_project_callback: Callable[..., Any],
    latest_managed_commit_callback: Callable[..., Any],
    record_session_turn_callback: Callable[..., Any],
    require_locked_source_callback: Callable[..., Any],
    quality_context_callback: Callable[..., Any],
    quality_source_tail_callback: Callable[..., Any],
    structured_completion_callback: Callable[..., Any],
    director_event_provider: Callable[[], Callable[..., Any]],
    remove_quality_debt_provider: Callable[[], Callable[..., Any]],
) -> EditorialRoutes:
    """Create editorial routes with live access to director bookkeeping."""
    ctx = EditorialContext(
        router=APIRouter(tags=["editorial"]),
        store=_StoreProxy(store_provider),
        find_chapter=find_chapter_callback,
        _indexed_retrieval_hits=indexed_retrieval_callback,
        _persist_managed_project=persist_managed_project_callback,
        _latest_managed_commit_project=latest_managed_commit_callback,
        _record_chapter_session_turn=record_session_turn_callback,
        _require_locked_memory_source=require_locked_source_callback,
        local_quality_known_context=quality_context_callback,
        quality_source_tail=quality_source_tail_callback,
        structured_completion=structured_completion_callback,
        _director_event=_LiveCallable(director_event_provider),
        _remove_quality_debt=_LiveCallable(remove_quality_debt_provider),
        repair_incomplete_memory=_LiveCallable(lambda: repair_incomplete_memory),
    )
    # Registration order matches the original single-module router.
    install_memory_routes(ctx)
    install_audit_routes(ctx)
    install_memory_state_routes(ctx)
    return EditorialRoutes(
        router=ctx.router,
        chapter_memory=ctx.chapter_memory,
        chapter_audit=ctx.chapter_audit,
    )

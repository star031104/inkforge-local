"""Application shell, health, and model-provider discovery endpoints."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ...core.config import API_SCHEMA_VERSION, APP_VERSION
from ...db import DATABASE_SCHEMA_VERSION, ProjectStore
from ...llama_client import list_models
from ...providers import provider_summary, settings_for_workload


def create_system_router(
    store_provider: Callable[[], ProjectStore],
    director_tasks_provider: Callable[[], dict[str, asyncio.Task[None]]],
    index_path: Path,
) -> APIRouter:
    router = APIRouter(tags=["system"])

    @router.get("/")
    async def index() -> FileResponse:
        return FileResponse(index_path, headers={"Cache-Control": "no-store"})

    @router.get("/api/health")
    async def health() -> dict[str, Any]:
        repository = store_provider()
        return {
            "status": "ok",
            "app_version": APP_VERSION,
            "api_schema_version": API_SCHEMA_VERSION,
            "database": "ready",
            "database_schema_version": DATABASE_SCHEMA_VERSION,
            "credential_protection": repository.secret_store.protector.kind,
            "search_index": "fts5" if repository.fts_enabled else "lexical_fallback",
            "active_director_tasks": sum(
                1 for task in director_tasks_provider().values() if not task.done()
            ),
        }

    @router.post("/api/models")
    async def models(settings: dict[str, Any]) -> dict[str, Any]:
        try:
            if str(settings.get("model_routing") or "single").lower() == "dual":
                routes: dict[str, Any] = {}
                cache: dict[tuple[str, str, str, str], list[str]] = {}
                for role in ("reasoning", "prose"):
                    routed = settings_for_workload(settings, role)
                    fingerprint = tuple(
                        str(routed.get(key) or "")
                        for key in ("provider", "base_url", "model", "api_key")
                    )
                    if fingerprint not in cache:
                        cache[fingerprint] = await list_models(routed)
                    routes[role] = {
                        "models": cache[fingerprint],
                        "summary": provider_summary(routed),
                    }
                summary = provider_summary(settings)
                return {
                    "models": routes["prose"]["models"],
                    "routes": routes,
                    **{key: summary[key] for key in ("routing_mode", "effective_routing", "active_slot")},
                }
            summary = provider_summary(settings)
            return {
                "models": await list_models(settings),
                **{key: value for key, value in summary.items()
                   if key in {"routing_mode", "effective_routing", "active_slot"}},
            }
        except Exception as exc:
            raise HTTPException(502, f"无法连接模型服务：{exc}") from exc

    @router.post("/api/provider/summary")
    async def provider_info(settings: dict[str, Any]) -> dict[str, Any]:
        return provider_summary(settings)

    return router

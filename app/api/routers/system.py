"""Application shell, health, and model-provider discovery endpoints."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.core.config import API_SCHEMA_VERSION, APP_VERSION
from app.db import DATABASE_SCHEMA_VERSION, ProjectStore
from app.llama_client import ModelEndpointError, list_models, probe_endpoint
from app.providers import provider_summary


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
        """List models for the saved endpoint (used by the status bar check)."""
        try:
            summary = provider_summary(settings)
            return {
                "models": await list_models(settings),
                **{
                    key: summary[key]
                    for key in (
                        "routing_mode", "effective_routing", "writing_model",
                        "planning_model", "server", "base_url",
                    )
                },
            }
        except ModelEndpointError as exc:
            raise HTTPException(502, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"无法连接模型服务：{exc}") from exc

    @router.post("/api/models/test")
    async def test_models(body: dict[str, Any]) -> dict[str, Any]:
        """Connection test for the settings dialog; never raises for bad endpoints."""
        return await probe_endpoint(
            {
                "base_url": str(body.get("base_url") or ""),
                "api_key": str(body.get("api_key") or ""),
            }
        )

    @router.post("/api/provider/summary")
    async def provider_info(settings: dict[str, Any]) -> dict[str, Any]:
        return provider_summary(settings)

    return router

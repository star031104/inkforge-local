"""First-run onboarding: install the bundled demo novel as a normal project."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ...db import ProjectStore
from ...demo import demo_available, demo_title, fresh_demo_project, is_demo_project


class DemoInstallRequest(BaseModel):
    force: bool = False
    # Copy the model connection from this project so the demo is usable at once.
    settings_from: str = Field(default="", max_length=80)


def find_installed_demo(store: ProjectStore) -> dict[str, Any] | None:
    """Summary row of the installed demo, or ``None``.

    Projects titled like the demo are checked first; the marker (not the
    title) decides, so a renamed demo is still found.
    """
    title = demo_title()
    rows = sorted(store.list(), key=lambda row: row.get("title") != title)
    for row in rows:
        project = store.get(str(row.get("id", "")))
        if project and is_demo_project(project):
            return row
    return None


def create_demo_router(store_provider: Callable[[], ProjectStore]) -> APIRouter:
    router = APIRouter(prefix="/api/demo", tags=["onboarding"])

    @router.get("")
    async def demo_status() -> dict[str, Any]:
        installed = find_installed_demo(store_provider()) if demo_available() else None
        return {
            "available": demo_available(),
            "title": demo_title(),
            "installed": bool(installed),
            "project_id": str(installed.get("id", "")) if installed else "",
        }

    @router.post("/install")
    async def demo_install(body: DemoInstallRequest) -> dict[str, Any]:
        if not demo_available():
            raise HTTPException(404, "安装包中缺少演示作品文件")
        store = store_provider()
        existing = find_installed_demo(store)
        if existing and not body.force:
            raise HTTPException(
                409, f"演示作品《{existing.get('title') or demo_title()}》已经安装，可直接在作品列表中打开"
            )
        project = store.import_project(fresh_demo_project())
        source = store.get(body.settings_from) if body.settings_from else None
        if source and isinstance(source.get("settings"), dict) and source["settings"].get("base_url"):
            settings = deepcopy(source["settings"])
            settings["target_words"] = project["settings"].get("target_words", settings.get("target_words"))
            project["settings"] = settings
            project = store.save(project["id"], project, reason="demo-install-settings")
        return {"project": project, "project_id": project["id"], "duplicate": bool(existing)}

    return router

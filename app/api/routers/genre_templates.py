"""HTTP routes for the new-book wizard's genre templates.

Stateless: the browser creates the project, asks this router to fill it from a
template, then saves it through the normal optimistic-concurrency PUT.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import ensure_project_defaults
from app.genre_templates import apply_template, get_template, list_templates, volume_instruction


class ApplyTemplateRequest(BaseModel):
    project: dict[str, Any]
    template_id: str = "general"
    answers: dict[str, Any] = Field(default_factory=dict)
    selection: dict[str, Any] = Field(default_factory=dict)


def create_genre_templates_router() -> APIRouter:
    router = APIRouter(prefix="/api/genre-templates", tags=["new-book-wizard"])

    @router.get("")
    async def templates() -> dict[str, Any]:
        return {"templates": list_templates()}

    @router.get("/{template_id}")
    async def template(template_id: str) -> dict[str, Any]:
        found = get_template(template_id)
        if not found:
            raise HTTPException(404, "没有这个题材模板")
        return found

    @router.post("/apply")
    async def apply(body: ApplyTemplateRequest) -> dict[str, Any]:
        if not get_template(body.template_id):
            raise HTTPException(404, "没有这个题材模板")
        project, summary = apply_template(
            ensure_project_defaults(body.project), body.template_id, body.answers, body.selection
        )
        return {
            "project": project,
            "summary": summary,
            "planning_instruction": volume_instruction(body.answers, body.template_id),
        }

    return router

from __future__ import annotations

from typing import Any, Callable
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .db import ProjectStore
from .project_service import ProjectConflictError
from .narrative_policy import PROFILES
from .writing_workspace import save_scene, propose_preference, text_diff
from .model_telemetry import report
from .evaluation import evaluate_candidate


class WorkspaceMutation(BaseModel):
    expected_updated_at: str = Field(min_length=1)
    chapter_id: str = ""
    item: dict[str, Any] = Field(default_factory=dict)


class DiffRequest(BaseModel):
    before: str = Field(max_length=100_000)
    after: str = Field(max_length=100_000)


class EvaluationRequest(BaseModel):
    left: str = Field(min_length=1, max_length=20000)
    right: str = Field(min_length=1, max_length=20000)
    target_chars: int = Field(default=1200, ge=1, le=10000)
    genre: str = Field(default="", max_length=120)


def create_workspace_router(provider: Callable[[], ProjectStore]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/workspace/telemetry")
    async def telemetry(limit: int = 100):
        return report(limit)

    @router.post("/api/workspace/diff")
    async def diff(body: DiffRequest):
        return text_diff(body.before, body.after)

    @router.post("/api/workspace/evaluate")
    async def evaluate(body: EvaluationRequest):
        return {"left": evaluate_candidate(body.left, target_chars=body.target_chars, genre=body.genre),
                "right": evaluate_candidate(body.right, target_chars=body.target_chars, genre=body.genre),
                "note": "仅比较机械问题和文风统计。文学表现、事实准确性需作者审阅，不据此自动选稿。"}

    @router.post("/api/projects/{project_id}/workspace/{action}")
    async def mutate(project_id: str, action: str, body: WorkspaceMutation):
        store = provider()
        project = store.get(project_id)
        if project is None:
            raise HTTPException(404, "作品不存在")
        task = store.latest_director_task(project_id)
        if task and task.get("status") in {"queued", "running"} and task.get("task_type") != "incubation":
            raise HTTPException(409, "请先暂停自动导演再编辑作品")
        if project["updated_at"] != body.expected_updated_at:
            raise HTTPException(409, "作品已更新，请重新载入后再操作")
        item = body.item
        try:
            if action in {"scene", "delete-scene"}:
                chapter = next((c for c in project["chapters"] if c["id"] == body.chapter_id), None)
                if chapter is None:
                    raise HTTPException(404, "章节不存在")
                if action == "scene":
                    save_scene(chapter, item)
                else:
                    chapter["scenes"] = [s for s in chapter.get("scenes", []) if s["id"] != item.get("id")]
            elif action == "preference":
                propose_preference(project, item)
            elif action == "review-preference":
                preference = next((p for p in project.get("author_preferences", []) if p["id"] == item.get("id")), None)
                if preference is None:
                    raise HTTPException(404, "偏好不存在")
                status = item.get("status")
                if status not in {"approved", "rejected", "pending"}:
                    raise ValueError("无效的偏好状态")
                preference["status"] = status
            elif action == "narrative":
                key = item.get("structure_profile")
                if key not in PROFILES:
                    raise ValueError("无效的叙事策略")
                project["narrative"].update(structure_profile=key, custom_structure=str(item.get("custom_structure", ""))[:3000])
            else:
                raise HTTPException(404, "操作不存在")
            return {"project": store.save(project_id, project, reason="workspace-" + action, expected_updated_at=body.expected_updated_at)}
        except ProjectConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    return router

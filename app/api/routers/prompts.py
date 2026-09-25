from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ...db import ensure_project_defaults
from ...domain.prose import _effective_prose_settings, _resolved_prose_target
from ...llama_client import chat_stream
from ...prompts import build_prompt
from ...providers import settings_for_workload
from ..schemas import GenerateRequest, SnapshotReplayRequest


def create_prompt_router(
    store_provider: Callable[[], Any],
    find_chapter: Callable[[dict[str, Any], str], tuple[int, dict[str, Any]]],
    attach_indexed_retrieval: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    persist_context_snapshot: Callable[
        [dict[str, Any], dict[str, Any], Any, str], dict[str, Any] | None
    ],
    prepare_prose_build: Callable[[Any, dict[str, Any], dict[str, Any]], Any],
) -> APIRouter:
    """Expose prompt inspection and deterministic snapshot replay.

    The router owns HTTP concerns while the injected callbacks retain access to
    the application's retrieval index and prompt assembly diagnostics.
    """
    router = APIRouter(prefix="/api/prompt", tags=["prompt-observability"])

    @router.post("/preview")
    async def prompt_preview(body: GenerateRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        target_chars = _resolved_prose_target(project, body)
        prompt_project = deepcopy(project)
        prompt_project["settings"] = _effective_prose_settings(
            project.get("settings", {}),
            target_chars,
            "revision" if body.mode == "rewrite" else "prose",
        )
        request = body.model_dump()
        request["project"] = prompt_project
        attach_indexed_retrieval(prompt_project, request)
        try:
            build = build_prompt(prompt_project, request)
            _, active_chapter = find_chapter(prompt_project, body.chapter_id)
            prepare_prose_build(build, prompt_project, active_chapter)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "prompt_version": build.prompt_version,
            "context_schema_version": build.context_schema_version,
            "messages": build.messages,
            "sections": build.sections,
            "activated_lore": build.activated_lore,
            "activated_skills": [
                {
                    "id": item.get("id", ""),
                    "name": item.get("name", ""),
                    "scope": item.get("scope", ""),
                    "mode": item.get("mode", ""),
                    "activation_reason": item.get("activation_reason", ""),
                    "injected": item.get("injected", False),
                }
                for item in build.activated_skills
            ],
            "retrieved_memories": [
                {
                    "kind": hit.kind,
                    "title": hit.title,
                    "content": hit.content,
                    "score": round(hit.score, 2),
                    "source_id": hit.source_id,
                    "origin": hit.origin,
                }
                for hit in build.retrieved_memories
            ],
            "budget_warnings": build.budget_warnings,
            "estimated_tokens": build.estimated_tokens,
        }

    @router.post("/snapshot")
    async def prompt_snapshot(body: GenerateRequest) -> dict[str, Any]:
        store = store_provider()
        project = ensure_project_defaults(body.project)
        if not store.get(str(project.get("id", ""))):
            raise HTTPException(404, "只能为已保存的项目创建上下文快照")
        target_chars = _resolved_prose_target(project, body)
        prompt_project = deepcopy(project)
        prompt_project["settings"] = _effective_prose_settings(
            project.get("settings", {}),
            target_chars,
            "revision" if body.mode == "rewrite" else "prose",
        )
        request = body.model_dump()
        request["project"] = prompt_project
        attach_indexed_retrieval(prompt_project, request)
        try:
            build = build_prompt(prompt_project, request)
            _, active_chapter = find_chapter(prompt_project, body.chapter_id)
            prepare_prose_build(build, prompt_project, active_chapter)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        snapshot = persist_context_snapshot(
            prompt_project, request, build, "manual_preview"
        )
        if not snapshot:
            raise HTTPException(404, "项目不存在，无法保存上下文快照")
        return snapshot

    @router.get("/snapshots/{snapshot_id}")
    async def prompt_snapshot_get(snapshot_id: str) -> dict[str, Any]:
        snapshot = store_provider().get_context_snapshot(snapshot_id)
        if not snapshot:
            raise HTTPException(404, "上下文快照不存在")
        return snapshot

    @router.post("/snapshots/{snapshot_id}/replay")
    async def prompt_snapshot_replay(
        snapshot_id: str, body: SnapshotReplayRequest
    ) -> StreamingResponse:
        snapshot = store_provider().get_context_snapshot(snapshot_id)
        if not snapshot:
            raise HTTPException(404, "上下文快照不存在")
        messages = snapshot.get("messages", [])
        if not isinstance(messages, list) or not messages:
            raise HTTPException(409, "该快照没有可回放的模型消息")

        async def events():
            meta = {
                "type": "meta",
                "replay": True,
                "context_snapshot_id": snapshot_id,
                "prompt_hash": snapshot.get("prompt_hash", ""),
                "estimated_tokens": snapshot.get("diagnostics", {}).get(
                    "estimated_tokens", 0
                ),
                "prompt_version": snapshot.get("diagnostics", {}).get(
                    "prompt_version", "unknown"
                ),
                "context_schema_version": snapshot.get("diagnostics", {}).get(
                    "context_schema_version", 1
                ),
            }
            yield f"data: {json.dumps(meta, ensure_ascii=False)}\n\n"
            try:
                async for piece in chat_stream(
                    settings_for_workload(body.settings, "prose"), messages
                ):
                    payload = {"type": "token", "text": piece}
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
            except Exception as exc:
                payload = {"type": "error", "error": str(exc)}
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return router

from __future__ import annotations

import uuid
from typing import Any, Awaitable, Callable

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .db import ProjectStore, ensure_project_defaults
from .project_service import ProjectConflictError
from .story_systems import (
    SOURCE_TIERS, add_editorial_review, add_narrative_event, add_research_source,
    add_voice_sample, approve_claim, build_character_dossier, bump_authority,
    create_editorial_draft, create_revision_proposal, fetch_public_source,
    finalize_editorial, governance_report, register_asset, search_sources,
)


class Mutation(BaseModel):
    project: dict[str, Any]
    item: dict[str, Any] = Field(default_factory=dict)


class SearchRequest(BaseModel):
    project: dict[str, Any]
    query: str = Field(min_length=2, max_length=500)
    limit: int = Field(default=8, ge=1, le=20)


class ExtractRequest(BaseModel):
    project: dict[str, Any]
    source_id: str
    subject: str = Field(min_length=1, max_length=200)


StructuredCompletion = Callable[..., Awaitable[tuple[dict[str, Any], list[str]]]]


def create_professional_router(store_provider: Callable[[], ProjectStore], complete: StructuredCompletion) -> APIRouter:
    router = APIRouter()

    def save(project: dict[str, Any], reason: str) -> dict[str, Any]:
        store = store_provider()
        project = ensure_project_defaults(project)
        project_id = str(project.get("id", ""))
        if not project_id or not store.get(project_id):
            raise HTTPException(404, "项目不存在")
        task = store.latest_director_task(project_id)
        if task and task.get("status") in {"queued", "running"} and task.get("task_type") != "incubation":
            raise HTTPException(409, "请先暂停自动导演再修改作品资料")
        try:
            return store.save(project_id, project, reason=reason, expected_updated_at=str(project.get("updated_at", "")))
        except ProjectConflictError as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/api/projects/{project_id}/professional/status")
    async def status(project_id: str) -> dict[str, Any]:
        store = store_provider()
        project = store.get(project_id)
        if not project:
            raise HTTPException(404, "项目不存在")
        return {
            "governance": governance_report(project),
            "research": {key: len(project["research"][key]) for key in ("sources", "claims", "conflicts", "dossiers")},
            "narrative_events": len(project["narrative_state"]["events"]),
            "voice_samples": len(project["voice_lab"]["samples"]),
            "editorial": {key: len(project["editorial"][key]) for key in ("drafts", "reviews", "revisions", "finalizations")},
        }

    @router.post("/api/professional/governance/bump")
    async def authority_bump(body: Mutation) -> dict[str, Any]:
        kind = str(body.item.get("kind", "")).strip()
        if not kind:
            raise HTTPException(422, "缺少权威资料类型")
        result = bump_authority(body.project, kind, str(body.item.get("reason", "")))
        return {"result": result, "project": save(body.project, "authority-bump")}

    @router.post("/api/professional/assets")
    async def asset_add(body: Mutation) -> dict[str, Any]:
        item = body.item
        asset = register_asset(
            body.project, kind=str(item.get("kind", "derived")), content=item.get("content"),
            authority=str(item.get("authority", "candidate")), dependencies=item.get("dependencies", []),
            source_ids=item.get("source_ids", []),
        )
        return {"item": asset, "project": save(body.project, "asset-register")}

    @router.post("/api/research/search")
    async def research_search(body: SearchRequest) -> dict[str, Any]:
        try:
            results = await search_sources(body.project.get("settings", {}), body.query, body.limit)
        except (ValueError, httpx.HTTPError) as exc:
            raise HTTPException(502, f"联网检索失败：{exc}") from exc
        return {"query": body.query, "results": results, "source_tiers": SOURCE_TIERS}

    @router.post("/api/research/source")
    async def source_add(body: Mutation) -> dict[str, Any]:
        item = add_research_source(body.project, body.item)
        return {"item": item, "project": save(body.project, "research-source")}

    @router.post("/api/research/fetch")
    async def source_fetch(body: Mutation) -> dict[str, Any]:
        try:
            fetched = await fetch_public_source(str(body.item.get("url", "")))
            item = add_research_source(body.project, {**body.item, **fetched})
        except (ValueError, httpx.HTTPError) as exc:
            raise HTTPException(502, f"资料抓取失败：{exc}") from exc
        return {"item": item, "project": save(body.project, "research-fetch")}

    @router.post("/api/research/claims/extract")
    async def claims_extract(body: ExtractRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        source = next((item for item in project["research"]["sources"] if item.get("id") == body.source_id), None)
        if not source:
            raise HTTPException(404, "资料来源不存在")
        source_text = str(source.get("text", ""))
        prompt = (
            "从资料中提取关于指定人物的原子化事实。每条只表达一个判断；evidence 必须逐字摘自资料；"
            "推断必须标 inference，未知不得补全。输出 JSON：{\"claims\":[{\"predicate\":\"\","
            "\"value\":\"\",\"certainty\":\"explicit|inference|unknown\",\"evidence\":\"\"}]}。\n"
            f"人物：{body.subject}\n资料等级：{source.get('tier')}\n资料：\n{source_text[:30000]}"
        )
        result, warnings = await complete(
            project.get("settings", {}),
            [{"role": "system", "content": "你是严谨的小说角色考据员，只依据给定材料。"}, {"role": "user", "content": prompt}],
            max_tokens=2800, timeout_seconds=120, temperature=0.1, workload="extraction",
        )
        candidates: list[dict[str, Any]] = []
        for raw in result.get("claims", []) if isinstance(result.get("claims"), list) else []:
            if not isinstance(raw, dict):
                continue
            evidence = str(raw.get("evidence", "")).strip()
            candidate = {
                "id": str(uuid.uuid4()), "subject": body.subject,
                "predicate": str(raw.get("predicate", "")).strip(), "value": str(raw.get("value", "")).strip(),
                "certainty": str(raw.get("certainty", "unknown")), "evidence": evidence,
                "source_id": body.source_id, "source_tier": source.get("tier", "C"), "status": "candidate",
            }
            if candidate["predicate"] and candidate["value"] and evidence and evidence in source_text:
                candidates.append(candidate)
        project["research"]["claims"].extend(candidates)
        return {"claims": candidates, "warnings": warnings, "project": save(project, "research-extract")}

    @router.post("/api/research/claims/approve")
    async def claim_approve(body: Mutation) -> dict[str, Any]:
        try:
            item = approve_claim(body.project, body.item)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"item": item, "project": save(body.project, "research-claim-approve")}

    @router.post("/api/research/dossier")
    async def dossier(body: Mutation) -> dict[str, Any]:
        character = str(body.item.get("character", "")).strip()
        if not character:
            raise HTTPException(422, "缺少人物名")
        item = build_character_dossier(body.project, character)
        return {"item": item, "project": save(body.project, "research-dossier")}

    @router.post("/api/narrative/events")
    async def event_add(body: Mutation) -> dict[str, Any]:
        try:
            item = add_narrative_event(body.project, body.item)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"item": item, "state": body.project["narrative_state"], "project": save(body.project, "narrative-event")}

    @router.post("/api/voice/samples")
    async def voice_add(body: Mutation) -> dict[str, Any]:
        try:
            item = add_voice_sample(body.project, body.item)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"item": item, "profile": body.project["voice_lab"]["profiles"].get(item["character"]), "project": save(body.project, "voice-sample")}

    @router.post("/api/editorial/drafts")
    async def draft_add(body: Mutation) -> dict[str, Any]:
        try:
            item = create_editorial_draft(body.project, str(body.item.get("chapter_id", "")), str(body.item.get("content", "")), str(body.item.get("source", "generation")))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"item": item, "project": save(body.project, "editorial-draft")}

    @router.post("/api/editorial/reviews")
    async def review_add(body: Mutation) -> dict[str, Any]:
        try:
            item = add_editorial_review(body.project, str(body.item.get("draft_id", "")), body.item.get("report", {}))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"item": item, "project": save(body.project, "editorial-review")}

    @router.post("/api/editorial/revisions")
    async def revision_add(body: Mutation) -> dict[str, Any]:
        try:
            item = create_revision_proposal(body.project, str(body.item.get("draft_id", "")), str(body.item.get("content", "")), str(body.item.get("review_id", "")))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"item": item, "project": save(body.project, "editorial-revision")}

    @router.post("/api/editorial/finalize")
    async def finalize(body: Mutation) -> dict[str, Any]:
        try:
            item = finalize_editorial(body.project, str(body.item.get("draft_id", "")), str(body.item.get("revision_id", "")))
            register_asset(body.project, kind="finalized_chapter", content=item["content"], authority="approved", dependencies=list(item["authority_snapshot"]), source_ids=[item["draft_id"]])
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"item": item, "project": save(body.project, "editorial-finalize")}

    return router

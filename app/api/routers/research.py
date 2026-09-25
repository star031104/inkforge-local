"""Style references, canon analysis, and knowledge-graph endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ...api.schemas import (
    CanonAuditRequest, CanonCharacterRequest, KnowledgeMutationRequest,
    ProjectRequest, ReferenceParseRequest, StyleRequest,
)
from ...canon import CANON_ANALYSIS_PROMPT, CANON_AUDIT_PROMPT, ensure_fanfic_defaults, render_canon_context
from ...db import ensure_project_defaults
from ...fallbacks import local_style_analysis
from ...file_parsing import parse_reference_file
from ...knowledge import graph_snapshot, sync_authoritative_entities, upsert_fact, upsert_relation
from ...prompts import STYLE_ANALYSIS_PROMPT
from ...references import combined_style_corpus, source_similarity_report
from ...services.model_errors import bounded_excerpt, planning_exception_detail, recoverable_model_error
from ...services.structured_output import require_schema, structured_completion


def create_research_router() -> APIRouter:
    router = APIRouter(tags=["research"])

    @router.post("/api/style/analyze")
    async def style_analyze(body: StyleRequest) -> dict[str, Any]:
        analysis_sample = bounded_excerpt(body.sample, 18000)
        messages = [
            {"role": "system", "content": "你是只返回合法 JSON 的文学风格分析器。"},
            {"role": "user", "content": STYLE_ANALYSIS_PROMPT + analysis_sample},
        ]
        try:
            result, warnings = await structured_completion(
                body.settings,
                messages,
                max_tokens=900,
                timeout_seconds=150,
                temperature=0.2,
                validate=require_schema(
                    "name", "profile", "dos", "donts", list_fields=("dos", "donts")
                ),
                workload="extraction",
                error_detail=planning_exception_detail,
            )
            return {
                "name": str(result.get("name", "样本文风")),
                "profile": str(result.get("profile", "")),
                "dos": [str(x) for x in result.get("dos", [])],
                "donts": [str(x) for x in result.get("donts", [])],
                "fallback": False,
                "warnings": warnings,
            }
        except Exception as exc:
            if recoverable_model_error(exc):
                return local_style_analysis(body.sample, planning_exception_detail(exc))
            raise HTTPException(
                502, f"文风分析失败：{planning_exception_detail(exc)}"
            ) from exc
    
    
    @router.post("/api/reference/parse")
    async def reference_parse(body: ReferenceParseRequest) -> dict[str, Any]:
        try:
            return parse_reference_file(body.name, body.content_base64)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    
    
    @router.post("/api/style/analyze-references")
    async def style_analyze_references(body: ProjectRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        corpus = combined_style_corpus(project, max_chars=100_000)
        if len(corpus.strip()) < 100:
            raise HTTPException(400, "请先在参考资料库上传并启用至少一篇‘文风样文’。")
        result = await style_analyze(StyleRequest(settings=project.get("settings", {}), sample=corpus))
        result["source_ids"] = [
            item.get("id")
            for item in project.get("references", [])
            if isinstance(item, dict) and item.get("kind") == "style" and item.get("enabled", True) is not False
        ]
        result["sample_chars"] = len(corpus)
        return result
    
    
    @router.post("/api/canon/analyze-character")
    async def canon_analyze_character(body: CanonCharacterRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        ensure_fanfic_defaults(project)
        character = next(
            (item for item in project.get("characters", []) if isinstance(item, dict) and item.get("id") == body.character_id),
            None,
        )
        if not character:
            raise HTTPException(404, "人物不存在")
        selected_ids = set(body.reference_ids)
        refs = [
            item
            for item in project.get("references", [])
            if isinstance(item, dict)
            and item.get("kind") == "canon"
            and item.get("enabled", True) is not False
            and (not selected_ids or item.get("id") in selected_ids)
        ]
        if not refs:
            raise HTTPException(400, "请先上传至少一份‘原作/正典资料’，再生成人物正典档案。")
        character_name = str(character.get("name", "")).strip()
        direct = [item for item in refs if character_name and character_name in str(item.get("text", ""))]
        if direct:
            refs = direct + [item for item in refs if item not in direct]
        parts: list[str] = []
        total = 0
        for item in refs:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            remaining = 42_000 - total
            if remaining <= 0:
                break
            excerpt = text[:remaining]
            parts.append(
                f"\n===== 资料：{item.get('name', '未命名')}｜作品={item.get('source_work', '') or '未注明'} =====\n{excerpt}"
            )
            total += len(excerpt)
        prompt = f"角色名：{character_name}\n" + CANON_ANALYSIS_PROMPT + "".join(parts)
        try:
            result, warnings = await structured_completion(
                project.get("settings", {}),
                [
                    {"role": "system", "content": "你只输出严格合法 JSON；不得使用训练记忆补全用户未提供的原作事实。"},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2200,
                timeout_seconds=240,
                temperature=0.18,
                validate=require_schema(
                    "must_preserve", "must_not", "explicit_facts", "inferences",
                    list_fields=("must_preserve", "must_not", "explicit_facts", "inferences"),
                ),
                workload="research",
                error_detail=planning_exception_detail,
            )
        except Exception as exc:
            raise HTTPException(502, f"角色正典档案分析失败：{planning_exception_detail(exc)}") from exc
        profile = {
            key: result.get(key, []) if key in {"must_preserve", "must_not"} else str(result.get(key, ""))
            for key in (
                "source_work", "timeline_node", "identity", "appearance", "core_personality",
                "deep_personality", "values", "goals", "fears", "abilities", "limitations",
                "speech_style", "behavior_patterns", "emotional_patterns", "relationship_patterns",
                "must_preserve", "must_not",
            )
        }
        profile.update(
            {
                "enabled": True,
                "user_verified": False,
                "source_refs": [str(item.get("id")) for item in refs if item.get("id")],
            }
        )
        return {
            "profile": profile,
            "explicit_facts": [str(item) for item in result.get("explicit_facts", [])],
            "inferences": [str(item) for item in result.get("inferences", [])],
            "warnings": warnings,
        }
    
    
    @router.post("/api/canon/audit")
    async def canon_audit(body: CanonAuditRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        canon_context = render_canon_context(project, body.draft)
        copy_report = source_similarity_report(project, body.draft)
        if not canon_context:
            return {
                "score": 100,
                "verdict": "pass",
                "issues": [],
                "strengths": [],
                "copy_risk": copy_report,
                "warnings": ["当前正文没有命中已启用的同人正典角色锁。"],
            }
        try:
            result, warnings = await structured_completion(
                project.get("settings", {}),
                [
                    {"role": "system", "content": CANON_AUDIT_PROMPT},
                    {"role": "user", "content": f"{canon_context}\n\n【候选正文】\n{bounded_excerpt(body.draft, 28000)}"},
                ],
                max_tokens=1700,
                timeout_seconds=220,
                temperature=0.12,
                validate=require_schema("issues", "strengths", list_fields=("issues", "strengths")),
                workload="critic",
                error_detail=planning_exception_detail,
            )
            result["score"] = max(0, min(100, int(result.get("score", 0) or 0)))
            result["verdict"] = str(result.get("verdict") or ("pass" if result["score"] >= 85 else "revise"))
            result["copy_risk"] = copy_report
            result["warnings"] = warnings
            return result
        except Exception as exc:
            raise HTTPException(502, f"同人角色一致性审校失败：{planning_exception_detail(exc)}") from exc
    
    
    @router.post("/api/knowledge/graph")
    async def knowledge_graph(body: ProjectRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        sync_authoritative_entities(project)
        return graph_snapshot(project)
    
    
    @router.post("/api/knowledge/fact")
    async def knowledge_fact(body: KnowledgeMutationRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        try:
            item = upsert_fact(project, body.item)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"project": ensure_project_defaults(project), "item": item}
    
    
    @router.post("/api/knowledge/relation")
    async def knowledge_relation(body: KnowledgeMutationRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        try:
            item = upsert_relation(project, body.item)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"project": ensure_project_defaults(project), "item": item}
    
    
    

    return router

"""Idea generation and resumable story incubation HTTP workflows."""
from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from ...db import ensure_project_defaults
from ...domain.planning_validation import validate_incubator_result
from ...fallbacks import local_ideas
from ...prompts import IDEAS_PROMPT
from ...services.model_errors import (
    bounded_excerpt,
    planning_exception_detail,
    recoverable_model_error,
)
from ...services.structured_output import require_schema
from ..schemas import IdeasRequest, IncubatorRequest


class _StoreProxy:
    def __init__(self, provider: Callable[[], Any]):
        self._provider = provider

    def __getattr__(self, name: str) -> Any:
        return getattr(self._provider(), name)


class _LiveCallable:
    def __init__(self, provider: Callable[[], Callable[..., Any]]):
        self._provider = provider

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._provider()(*args, **kwargs)


def create_incubation_router(
    *,
    store_provider: Callable[[], Any],
    generate_core_callback: Callable[..., Any],
    complete_option_callback: Callable[..., Any],
    structured_completion_callback: Callable[..., Any],
    launch_director_provider: Callable[[], Callable[..., Any]],
) -> APIRouter:
    router = APIRouter(tags=["incubation"])
    store = _StoreProxy(store_provider)
    _generate_incubator_core = generate_core_callback
    _complete_incubator_option = complete_option_callback
    structured_completion = structured_completion_callback
    _launch_director = _LiveCallable(launch_director_provider)

    @router.post("/api/incubator")
    async def incubator(body: IncubatorRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        seed = body.seed.strip()
        if len(seed) < 8:
            raise HTTPException(400, "灵感至少需要 8 个字")
        target = max(3, min(300, int(body.target_chapters or 30)))
        story_mode = "short" if body.story_mode == "short" else "long"
        try:
            core_options, warnings = await _generate_incubator_core(
                project, seed, body.preferences, story_mode, target
            )
            options: list[dict[str, Any]] = []
            for index, raw_option in enumerate(core_options, start=1):
                option, asset_warnings = await _complete_incubator_option(
                    project,
                    raw_option,
                    index=index,
                    seed=seed,
                    preferences=body.preferences,
                    target=target,
                )
                options.append(option)
                warnings.extend(asset_warnings)
            result = {"options": options}
            validate_incubator_result(
                result, target_chapters=target, story_mode=story_mode
            )
            result["warnings"] = warnings
            result["generation_mode"] = "staged_transaction"
            result["fallback"] = False
            return result
        except Exception as exc:
            if not recoverable_model_error(exc):
                raise HTTPException(
                    502, f"灵感孵化失败：{planning_exception_detail(exc)}"
                ) from exc
            raise HTTPException(
                502,
                "灵感孵化没有得到完整方案："
                f"{planning_exception_detail(exc)}。原始灵感未被修改，请重试。",
            ) from exc


    @router.post("/api/incubator/start")
    async def incubator_start(body: IncubatorRequest) -> dict[str, Any]:
        source = ensure_project_defaults(body.project)
        project_id = str(source.get("id", "")).strip()
        project = store.get(project_id) if project_id else None
        if not project:
            raise HTTPException(404, "请先保存当前作品，再开始灵感孵化")
        seed = body.seed.strip()
        if len(seed) < 8:
            raise HTTPException(400, "灵感至少需要 8 个字")
        latest = store.latest_director_task(project_id)
        if latest and latest.get("status") in {"queued", "running", "stopping"}:
            raise HTTPException(409, "当前已有 AI 任务运行，请先等待完成或暂停")
        config = {
            "seed": seed,
            "preferences": body.preferences.strip(),
            "story_mode": "short" if body.story_mode == "short" else "long",
            "target_chapters": max(3, min(300, int(body.target_chapters or 30))),
        }
        task = store.create_director_task(
            project_id,
            {
                "task_type": "incubation",
                "phase": "incubation",
                "incubation_step": "queued",
                "message": "灵感已保存，等待 AI 开始构思",
                "config": config,
                "events": [],
                "incubation_core_options": [],
                "incubation_options": [],
                "warnings": [],
            },
        )
        _launch_director(task["id"])
        return {"task": task}


    @router.post("/api/ideas")
    async def ideas(body: IdeasRequest) -> dict[str, Any]:
        project = ensure_project_defaults(body.project)
        narrative = project.get("narrative", {})
        memory = project.get("memory", {})
        context = f"""【推荐类型】
    {'开书主题与核心冲突' if body.kind == 'book' else '下一阶段可选主题与冲突'}
    【作品形态】
    {project.get('story_mode', 'long')}，计划约 {narrative.get('target_chapters', 30)} 章
    【书名与题材】
    {project.get('title', '')}；{project.get('genre', '')}
    【核心构想】
    {project.get('premise', '')}
    【作者长期意图】
    {project.get('author_intent', '')}
    【核心问题与结局方向】
    {narrative.get('central_question', '')}
    {narrative.get('ending_direction', '')}
    【人物】
    {bounded_excerpt(json.dumps(project.get('characters', []), ensure_ascii=False), 7000)}
    【全书进展】
    {memory.get('story_so_far', '')}
    【未结线索】
    {bounded_excerpt(json.dumps(memory.get('plot_threads', []), ensure_ascii=False), 5000)}
    【作者补充】
    {body.instruction}"""
        try:
            result, warnings = await structured_completion(
                project.get("settings", {}),
                [
                    {"role": "system", "content": IDEAS_PROMPT},
                    {"role": "user", "content": context},
                ],
                temperature=0.65,
                max_tokens=1200,
                timeout_seconds=180,
                validate=require_schema(
                    "options",
                    list_fields=("options",),
                    list_bounds={"options": (3, None)},
                ),
            )
            options = result.get("options", [])
            if not isinstance(options, list):
                options = []
            clean = []
            for option in options[:6]:
                if not isinstance(option, dict):
                    continue
                clean.append(
                    {
                        key: str(option.get(key, ""))
                        for key in (
                            "title",
                            "theme",
                            "central_conflict",
                            "story_promise",
                            "turning_point",
                            "ending_direction",
                            "why_fit",
                            "risk",
                        )
                    }
                )
            if len(clean) < 3:
                raise ValueError(f"模型只返回 {len(clean)} 个可用方案")
            return {"options": clean[:3], "fallback": False, "warnings": warnings}
        except Exception as exc:
            if recoverable_model_error(exc):
                return local_ideas(project, body.kind, planning_exception_detail(exc))
            raise HTTPException(
                502, f"灵感推荐失败：{planning_exception_detail(exc)}"
            ) from exc

    return router

"""Streaming prose generation transport and conservative continuation repair."""
from __future__ import annotations

import json
import math
import re
from copy import deepcopy
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.chapter_session import checkpoint as session_checkpoint
from app.storage.schema.project import ensure_project_defaults
from app.llm.budget import ContextBudgetError, fit_output_tokens, request_budget
from app.llm.completion import StreamOutcome
from app.domain.prose import (
    _clean_repair_continuation,
    _effective_prose_settings,
    _prose_char_count,
    _prose_looks_truncated,
    _resolved_prose_target,
)
from app.review.editorial_workflow import begin_stage, complete_stage, fail_stage
from app.llm.client import chat_stream, validate_context_budget
from app.llm.errors import planning_exception_detail
from app.services.antislop import apply_antislop, prose_stream_options
from app.services.craft_settings import craft_setting
from app.review.experience_bank import experience_hints
from app.llm.providers import settings_for_workload
from app.llm.structured_output import structured_completion
from app.prompts import build_prompt
from app.api.schemas import GenerateRequest


def create_generation_router(
    *,
    store_provider: Callable[[], Any],
    find_chapter: Callable[[dict[str, Any], str], tuple[int, dict[str, Any]]],
    attach_indexed_retrieval: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    prepare_prose_build: Callable[[Any, dict[str, Any], dict[str, Any]], dict[str, str]],
    get_or_create_session: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    record_session_turn: Callable[..., dict[str, Any]],
    persist_context_snapshot: Callable[..., dict[str, Any] | None],
) -> APIRouter:
    router = APIRouter(tags=["generation"])

    @router.post("/api/generate")
    async def generate(body: GenerateRequest) -> StreamingResponse:
        store = store_provider()
        project = ensure_project_defaults(body.project)
        _, active_chapter = find_chapter(project, body.chapter_id)
        begin_stage(active_chapter, "generate", body.model_dump(exclude={"project"}))
        target_chars = _resolved_prose_target(project, body)
        prose_settings = _effective_prose_settings(
            project.get("settings", {}),
            target_chars,
            "revision" if body.mode == "rewrite" else "prose",
        )
        prompt_project = deepcopy(project)
        prompt_project["settings"] = prose_settings
        request = body.model_dump()
        request["project"] = prompt_project
        if craft_setting(project, "experience_bank"):
            # Per-chapter lessons for this writing model (never the stable prefix).
            request["_experience_hints"] = experience_hints(
                project, str(settings_for_workload(project.get("settings", {}), "prose").get("model", ""))
            )
        attach_indexed_retrieval(prompt_project, request)
        try:
            build = build_prompt(prompt_project, request)
            stable_prefix = prepare_prose_build(build, prompt_project, active_chapter)
            validate_context_budget(
                prose_settings,
                build.messages,
                int(prose_settings.get("max_tokens", 3500)),
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        session = get_or_create_session(prompt_project, active_chapter)
        session_checkpoint(session, f"{body.mode}-before-generation")
        project_id = str(prompt_project.get("id", ""))
        if project_id and store.get(project_id):
            store.save_chapter_session(
                project_id, str(active_chapter.get("id", "")), session
            )
        context_snapshot = persist_context_snapshot(
            prompt_project, request, build, reason="generation"
        )

        async def events():
            meta = {
                "type": "meta",
                "prompt_version": build.prompt_version,
                "context_schema_version": build.context_schema_version,
                "estimated_tokens": build.estimated_tokens,
                "activated_lore": [
                    item.get("title", "") for item in build.activated_lore
                ],
                "activated_skills": [
                    f"{item.get('scope', '')}:{item.get('name', '')}"
                    for item in build.activated_skills
                    if item.get("injected", True)
                ],
                "retrieved_memories": [
                    f"{hit.kind}:{hit.title}" for hit in build.retrieved_memories
                ],
                "budget_warnings": build.budget_warnings,
                "target_chars": target_chars,
                "effective_max_tokens": prose_settings.get("max_tokens", 3500),
                "request_budget": request_budget(prose_settings, build.messages, int(prose_settings.get("max_tokens", 3500))).as_dict(),
                "context_snapshot_id": (
                    context_snapshot.get("id", "") if context_snapshot else ""
                ),
                "prompt_hash": (
                    context_snapshot.get("prompt_hash", "") if context_snapshot else ""
                ),
                "project_prefix_hash": stable_prefix["project_prefix_hash"],
                "chapter_prefix_hash": stable_prefix["chapter_prefix_hash"],
            }
            yield f"data: {json.dumps(meta, ensure_ascii=False)}\n\n"
            pieces: list[str] = []
            repaired = False
            outcome = StreamOutcome()
            repair_warning = ""
            try:
                stream_options = await prose_stream_options(prompt_project, prose_settings)
                stream_kwargs = {"options": stream_options} if stream_options is not None else {}
                async for piece in chat_stream(
                    prose_settings, build.messages, outcome=outcome, **stream_kwargs
                ):
                    pieces.append(piece)
                    yield f"data: {json.dumps({'type': 'token', 'text': piece}, ensure_ascii=False)}\n\n"

                text = "".join(pieces).strip()
                actual = _prose_char_count(text)
                lower = max(
                    80,
                    int(target_chars * (0.82 if body.mode != "rewrite" else 0.62)),
                )
                for repair_pass in range(1, 3):
                    needs_length_repair = body.mode != "rewrite" and actual < lower
                    needs_closure_repair = actual >= 80 and (outcome.truncated or _prose_looks_truncated(text))
                    if not text or not (needs_length_repair or needs_closure_repair):
                        break
                    remaining = max(120, target_chars - actual)
                    chapter = next(
                        (
                            item
                            for item in prompt_project.get("chapters", [])
                            if item.get("id") == body.chapter_id
                        ),
                        {},
                    )
                    plan = chapter.get("plan", {}) if isinstance(chapter.get("plan"), dict) else {}
                    route = chapter.get("route", {}) if isinstance(chapter.get("route"), dict) else {}
                    unresolved_ending = str(
                        plan.get("ending_hook")
                        or plan.get("exit_state")
                        or route.get("ending_hook")
                        or ""
                    ).strip()
                    anchor = re.sub(r"\s+", " ", text[-180:]).strip()
                    repair_messages = [dict(message) for message in build.messages]
                    repair_messages.extend(
                        [
                            {"role": "assistant", "content": text},
                            {
                                "role": "user",
                                "content": (
                                    f"这是第 {repair_pass} 次续补。当前正文约 {actual} 个中文字符，"
                                    f"目标约 {target_chars} 字，还需要约 {remaining} 字。"
                                    "不要重写、不要回顾、不要把已经发生的动作换词再写、不要解释。"
                                    "必须从上文最后一个动作或句子之后直接继续；首句不得重新介绍"
                                    "人物、地点、任务、简牍或已经发现的证据。"
                                    f"接续锚点（只能写其后的新动作）：【{anchor}】。"
                                    + (
                                        f"尚需交付的章末状态是：【{unresolved_ending}】。"
                                        "若上文尚未到达它，就沿当前因果完成它；若已经到达，"
                                        "只写它造成的即时新状态。"
                                        if unresolved_ending
                                        else ""
                                    )
                                    + "保持人物、地点、时间、视角和既有事实不变；"
                                    "不得新增未经上下文支持的往事。继续推进当前场景，加入具体动作、"
                                    "对话或可感知细节，并自然收束在完整句子上。"
                                ),
                            },
                        ]
                    )
                    repair_settings = _effective_prose_settings(
                        prose_settings, remaining
                    )
                    repair_settings["max_tokens"] = min(
                        int(repair_settings.get("max_tokens", 3500)),
                        max(900, int(math.ceil(remaining * 1.6)) + 240),
                    )
                    try:
                        repair_settings["max_tokens"] = fit_output_tokens(
                            repair_settings, repair_messages, repair_settings["max_tokens"]
                        )
                    except ContextBudgetError:
                        repair_warning = "候选正文已保留；剩余上下文不足以安全续补，请缩小任务或调整输出长度后继续。"
                        break
                    repair_meta = {
                        "type": "repair",
                        "pass": repair_pass,
                        "reason": "short" if needs_length_repair else "truncated",
                        "current_chars": actual,
                        "target_chars": target_chars,
                    }
                    yield f"data: {json.dumps(repair_meta, ensure_ascii=False)}\n\n"
                    before = actual
                    repair_pieces: list[str] = []
                    repair_outcome = StreamOutcome()
                    async for piece in chat_stream(repair_settings, repair_messages, outcome=repair_outcome):
                        repair_pieces.append(piece)
                    continuation = _clean_repair_continuation(
                        text, "".join(repair_pieces)
                    )
                    if continuation:
                        outcome = repair_outcome
                        pieces.append(continuation)
                        yield f"data: {json.dumps({'type': 'token', 'text': continuation}, ensure_ascii=False)}\n\n"
                    repaired = True
                    text = "".join(pieces).strip()
                    actual = _prose_char_count(text)
                    if actual - before < 40:
                        break

                if actual < 80:
                    event = {
                        "type": "error",
                        "message": f"模型返回正文过短（约 {actual} 字），请重试。",
                    }
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    return

                # Sentence-level cliché repair; the candidate stays a candidate.
                antislop_report: dict[str, Any] = {}
                if body.mode in {"continue", "instruction"}:
                    try:
                        revised, antislop_report = await apply_antislop(
                            text, prompt_project, prose_settings, structured_completion
                        )
                    except Exception as exc:  # noqa: BLE001 - optional post-processing
                        revised, antislop_report = text, {"warnings": [str(exc)]}
                    if revised != text:
                        text = revised
                        actual = _prose_char_count(text)
                        replace = {
                            "type": "replace",
                            "text": text,
                            "reason": "antislop",
                            "rewritten": antislop_report.get("rewritten", []),
                        }
                        yield f"data: {json.dumps(replace, ensure_ascii=False)}\n\n"
                warning = ""
                needs_review = bool(repair_warning or outcome.truncated or _prose_looks_truncated(text))
                if body.mode != "rewrite" and actual < int(target_chars * 0.75):
                    warning = (
                        f"候选稿仍偏短：约 {actual}/{target_chars} 字，"
                        "建议重试或继续补写。"
                    )
                    needs_review = True
                if needs_review:
                    warning = "；".join(filter(None, [warning, repair_warning or (
                        "续补后仍未确认完整收束，请复核结尾。" if outcome.truncated or _prose_looks_truncated(text) else ""
                    )]))
                done = {
                    "type": "done",
                    "actual_chars": actual,
                    "target_chars": target_chars,
                    "length_repaired": repaired,
                    "warning": warning,
                    "completion_state": "needs_review" if needs_review else "complete",
                    "finish_reason": outcome.finish_reason,
                    "antislop": {
                        "rewritten": len(antislop_report.get("rewritten", [])),
                        "hits": [item.get("phrase") for item in antislop_report.get("hits", [])][:12],
                    } if antislop_report else {},
                }
                if needs_review:
                    fail_stage(active_chapter, "generate", ValueError(warning))
                else:
                    complete_stage(
                        active_chapter, "generate",
                        {"chars": actual, "content_hash": stable_prefix["chapter_prefix_hash"]},
                    )
                record_session_turn(
                    prompt_project,
                    active_chapter,
                    "assistant",
                    text,
                    "generated-draft-needs-review" if needs_review else "generated-draft",
                )
                yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
            except Exception as exc:
                fail_stage(active_chapter, "generate", exc)
                event = {"type": "error", "message": planning_exception_detail(exc)}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    return router

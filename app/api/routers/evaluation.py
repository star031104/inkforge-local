"""Long-form evaluation: deterministic project metrics and pairwise blind judging."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, HTTPException
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ...db import ProjectStore
from ...evaluation_suite import (
    DEFAULT_TERMINAL_CHARS,
    EvaluationHistory,
    evaluate_project,
    history_snapshot,
    load_telemetry_calls,
    pairwise_compare,
    validate_pairwise,
)

StructuredCompletion = Callable[..., Awaitable[tuple[dict[str, Any], list[str]]]]


class PairwiseRequest(BaseModel):
    left_text: str = Field(min_length=1, max_length=40_000)
    right_text: str = Field(min_length=1, max_length=40_000)
    left_label: str = Field(default="左稿", max_length=60)
    right_label: str = Field(default="右稿", max_length=60)
    context: str = Field(default="", max_length=3000)


def _prices(project: dict[str, Any], input_price: float | None, output_price: float | None) -> dict[str, float] | None:
    settings = project.get("settings", {}) if isinstance(project.get("settings"), dict) else {}
    prices = {
        "input_per_mtok": input_price if input_price is not None else settings.get("price_input_per_mtok"),
        "output_per_mtok": output_price if output_price is not None else settings.get("price_output_per_mtok"),
    }
    try:
        clean = {key: float(value) for key, value in prices.items() if value not in (None, "")}
    except (TypeError, ValueError):
        return None
    return clean or None


def create_evaluation_router(
    store_provider: Callable[[], ProjectStore],
    *,
    history_root: Path,
    telemetry_path: Callable[[], Path | None],
    completion: StructuredCompletion,
) -> APIRouter:
    router = APIRouter(tags=["evaluation"])
    history = EvaluationHistory(history_root)

    def load(project_id: str) -> dict[str, Any]:
        project = store_provider().get(project_id)
        if project is None:
            raise HTTPException(404, "作品不存在")
        return project

    @router.get("/api/projects/{project_id}/evaluation")
    async def project_evaluation(
        project_id: str,
        record: bool = False,
        input_price: float | None = None,
        output_price: float | None = None,
        terminal_chars: int = DEFAULT_TERMINAL_CHARS,
    ):
        project = load(project_id)

        def compute() -> dict[str, Any]:
            calls = load_telemetry_calls(
                telemetry_path(),
                since=str(project.get("created_at", "")),
                until="",
            )
            report = evaluate_project(
                project,
                calls=calls,
                prices=_prices(project, input_price, output_price),
                terminal_chars=max(1000, min(200_000, int(terminal_chars or DEFAULT_TERMINAL_CHARS))),
            )
            if record:
                history.append(project_id, history_snapshot(report))
            return report

        # Metrics scan the whole manuscript plus up to 5000 telemetry rows.
        return await run_in_threadpool(compute)

    @router.get("/api/projects/{project_id}/evaluation/history")
    async def evaluation_history(project_id: str, limit: int = 30, kind: str = ""):
        load(project_id)
        return {"items": history.list(project_id, limit=max(1, min(limit, 200)), kind=kind)}

    @router.post("/api/projects/{project_id}/evaluation/pairwise")
    async def pairwise(project_id: str, body: PairwiseRequest):
        project = load(project_id)
        settings = project.get("settings", {})

        async def judge(messages: list[dict[str, str]]) -> dict[str, Any]:
            result, _warnings = await completion(
                settings,
                messages,
                max_tokens=900,
                timeout_seconds=180,
                temperature=0.1,
                validate=validate_pairwise,
                workload="critic",
            )
            return result

        context = body.context or "\n".join(
            part for part in (str(project.get("premise", "")), str(project.get("book_rules", ""))) if part
        )
        try:
            result = await pairwise_compare(body.left_text, body.right_text, judge, context=context)
        except HTTPException:
            raise
        except Exception as exc:  # model or network failure: report, don't crash
            raise HTTPException(502, f"盲评调用失败：{exc}") from exc
        result.update(left_label=body.left_label, right_label=body.right_label)
        history.append(
            project_id,
            {
                "kind": "pairwise",
                "left_label": body.left_label,
                "right_label": body.right_label,
                "left_win_rate": result["left_win_rate"],
                "position_consistency": result["position_consistency"],
                "overall": result["overall"],
            },
        )
        return result

    return router

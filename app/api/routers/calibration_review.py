"""Audit score calibration and whole-volume review endpoints (v0.36)."""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.calibration import (
    audited_chapters,
    calibration_stats,
    ensure_calibration_defaults,
    sample_from_chapter,
    upsert_sample,
)
from app.evidence_audit import PENALTIES, normalize_audit_weights
from app.llm.errors import planning_exception_detail
from app.services.volume_review import find_volume, public_entry, run_volume_review


class IssueVerdict(BaseModel):
    issue_id: str = Field(max_length=60)
    verdict: str = Field(default="", max_length=20)
    corrected_severity: str = Field(default="", max_length=10)


class CalibrationSampleRequest(BaseModel):
    chapter_id: str = Field(max_length=80)
    human_score: int | None = Field(default=None, ge=0, le=100)
    issues: list[IssueVerdict] = Field(default_factory=list, max_length=60)


class CalibrationApplyRequest(BaseModel):
    mode: str = Field(default="suggested", pattern="^(suggested|default)$")
    scope: str = Field(default="project", pattern="^(project|all)$")


def create_calibration_review_router(
    store_provider: Callable[[], Any],
    structured_completion_provider: Callable[[], Callable[..., Any]],
) -> APIRouter:
    router = APIRouter(tags=["calibration", "volume-review"])
    reviewing: set[str] = set()

    def store() -> Any:
        return store_provider()

    def project_or_404(project_id: str) -> dict[str, Any]:
        project = store().get(project_id)
        if not project:
            raise HTTPException(404, "作品不存在")
        return project

    def ensure_director_idle(project_id: str, action: str) -> None:
        task = store().latest_director_task(project_id)
        if task and task.get("task_type") != "incubation" and task.get("status") in {"queued", "running"}:
            raise HTTPException(409, f"自动导演运行期间不能{action}，请先暂停任务")

    def all_samples() -> list[dict[str, Any]]:
        samples: list[dict[str, Any]] = []
        for row in store().list():
            project = store().get(str(row.get("id", "")))
            if project:
                samples.extend(ensure_calibration_defaults(project)["samples"])
        return samples

    def overview(project: dict[str, Any], scope: str = "project") -> dict[str, Any]:
        samples = ensure_calibration_defaults(project)["samples"] if scope == "project" else all_samples()
        weights = normalize_audit_weights(project.get("settings", {}).get("audit_weights"))
        return {
            "scope": scope,
            "candidates": audited_chapters(project),
            "stats": calibration_stats(samples, weights),
            "weights": weights,
            "defaults": {"penalties": dict(PENALTIES), "category_multipliers": {}},
        }

    @router.get("/api/projects/{project_id}/calibration")
    async def calibration_overview(project_id: str, scope: str = "project") -> dict[str, Any]:
        return overview(project_or_404(project_id), "all" if scope == "all" else "project")

    @router.get("/api/calibration/stats")
    async def calibration_all_stats() -> dict[str, Any]:
        """Agreement statistics over the samples of every project."""
        return calibration_stats(all_samples())

    @router.post("/api/projects/{project_id}/calibration/samples")
    async def calibration_record(project_id: str, body: CalibrationSampleRequest) -> dict[str, Any]:
        ensure_director_idle(project_id, "记录校准样本")
        project = project_or_404(project_id)
        try:
            sample = sample_from_chapter(
                project, body.chapter_id, human_score=body.human_score,
                verdicts=[item.model_dump() for item in body.issues],
            )
        except KeyError:
            raise HTTPException(404, "章节不存在") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if sample["human_score"] is None and not any(
            item.verdict for item in body.issues
        ):
            raise HTTPException(400, "请填写人工评分或至少标记一条审校意见")
        stored = upsert_sample(project, sample)
        saved = store().save(project_id, project, reason="calibration-sample")
        return {"sample": stored, "project": saved, **overview(saved)}

    @router.post("/api/projects/{project_id}/calibration/apply")
    async def calibration_apply(project_id: str, body: CalibrationApplyRequest) -> dict[str, Any]:
        ensure_director_idle(project_id, "修改审校权重")
        project = project_or_404(project_id)
        settings = project.setdefault("settings", {})
        if body.mode == "default":
            settings.pop("audit_weights", None)
        else:
            current = normalize_audit_weights(settings.get("audit_weights"))
            samples = ensure_calibration_defaults(project)["samples"] if body.scope == "project" else all_samples()
            stats = calibration_stats(samples, current)
            if not stats.get("suggestion"):
                raise HTTPException(400, stats.get("suggestion_note") or "样本不足，暂无建议权重")
            weights = normalize_audit_weights(stats["suggestion"]["weights"])
            if weights:
                settings["audit_weights"] = weights
            else:
                settings.pop("audit_weights", None)
        saved = store().save(project_id, project, reason=f"calibration-apply-{body.mode}")
        return {"project": saved, **overview(saved, body.scope)}

    @router.get("/api/projects/{project_id}/volumes/{volume_id}/review")
    async def volume_review_latest(project_id: str, volume_id: str) -> dict[str, Any]:
        project = project_or_404(project_id)
        if find_volume(project, volume_id) is None:
            raise HTTPException(404, "分卷不存在")
        return {"review": public_entry(project, volume_id), "running": project_id in reviewing}

    @router.post("/api/projects/{project_id}/volumes/{volume_id}/review")
    async def volume_review_run(project_id: str, volume_id: str) -> dict[str, Any]:
        ensure_director_idle(project_id, "整卷回顾")
        if project_id in reviewing:
            raise HTTPException(409, "这部作品已有整卷回顾在进行")
        project = project_or_404(project_id)
        if find_volume(project, volume_id) is None:
            raise HTTPException(404, "分卷不存在")
        reviewing.add(project_id)
        try:
            await run_volume_review(project, volume_id, completion=structured_completion_provider())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:  # model/transport failures → 502 with a readable reason
            raise HTTPException(502, f"整卷回顾失败：{planning_exception_detail(exc)}") from exc
        finally:
            reviewing.discard(project_id)
        # Re-read before saving: the author may have saved while the model ran.
        latest = project_or_404(project_id)
        latest.setdefault("volume_reviews", {})[volume_id] = project["volume_reviews"][volume_id]
        known = {str(item.get("id", "")) for item in latest.get("repair_queue", []) if isinstance(item, dict)}
        latest.setdefault("repair_queue", []).extend(
            item for item in project.get("repair_queue", [])
            if isinstance(item, dict) and str(item.get("id", "")) not in known
        )
        saved = store().save(project_id, latest, reason="volume-review")
        return {"review": public_entry(saved, volume_id), "project": saved}

    return router

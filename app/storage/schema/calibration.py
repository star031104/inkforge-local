"""Persisted shape of score-calibration samples (author vs. AI audit scores)."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from app.story.evidence import PENALTIES, issue_category, normalize_audit_weights


MAX_SAMPLES = 500
VERDICTS = {"agree", "disagree", "severity"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _score(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return int(round(max(0.0, min(100.0, number))))


def issue_identity(issue: dict[str, Any]) -> str:
    """``issue_id`` when the critic assigned one, else a content hash."""
    explicit = str(issue.get("issue_id") or issue.get("id") or "").strip()
    if explicit:
        return explicit[:40]
    payload = f"{issue_category(issue)}|{str(issue.get('message', '')).strip()}"
    return "h" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _normalize_issue(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    severity = str(raw.get("severity", "medium")).lower()
    if severity not in PENALTIES:
        severity = "medium"
    verdict = str(raw.get("verdict") or "").strip().lower()
    corrected = str(raw.get("corrected_severity") or "").strip().lower()
    if corrected not in PENALTIES:
        corrected = ""
    if verdict == "severity" and not corrected:
        verdict = ""
    if verdict not in VERDICTS:
        verdict = ""
    return {
        "issue_id": issue_identity(raw),
        "category": issue_category(raw)[:60],
        "severity": severity,
        "message": str(raw.get("message", ""))[:240],
        "counted": raw.get("counted", raw.get("evidence_verified", True)) is not False,
        "verdict": verdict,
        "corrected_severity": corrected if verdict == "severity" else "",
    }


def normalize_sample(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    chapter_id = str(raw.get("chapter_id", "")).strip()[:80]
    if not chapter_id:
        return None
    issues = [item for item in map(_normalize_issue, raw.get("issues") or []) if item][:40]
    return {
        "chapter_id": chapter_id,
        "chapter_title": str(raw.get("chapter_title", ""))[:120],
        "draft_hash": str(raw.get("draft_hash", "")).strip()[:64],
        "human_score": _score(raw.get("human_score")),
        "ai_score": _score(raw.get("ai_score")),
        "issues": issues,
        "audit_weights": normalize_audit_weights(raw.get("audit_weights")),
        "created_at": str(raw.get("created_at") or _now()),
        "updated_at": str(raw.get("updated_at") or raw.get("created_at") or _now()),
    }


def ensure_calibration_defaults(project: dict[str, Any]) -> dict[str, Any]:
    calibration = project.get("calibration")
    if not isinstance(calibration, dict):
        calibration = {}
        project["calibration"] = calibration
    samples = calibration.get("samples")
    clean = [item for item in map(normalize_sample, samples if isinstance(samples, list) else []) if item]
    calibration["samples"] = clean[-MAX_SAMPLES:]
    if not isinstance(project.get("volume_reviews"), dict):
        project["volume_reviews"] = {}
    return calibration

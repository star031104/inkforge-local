"""Audit score calibration: compare AI audit scores with the author's own.

The author records, for an audited chapter, a human score (0–100) and an
optional verdict on each AI finding (agree / disagree / wrong severity).
Samples live in ``project["calibration"]["samples"]``.

From the samples we report agreement (MAE, Pearson, Spearman), per-category
false-positive rates, and — once there are at least ``MIN_SAMPLES`` scored
samples — suggested scoring weights for ``project.settings.audit_weights``:

* severity penalties (high/medium/low) and per-category multipliers are fit
  by coordinate search over small grids, minimising the MAE between the
  re-scored AI score and the human score;
* every fitted value is shrunk toward its default in proportion to how few
  samples support it, so a handful of samples can never swing the scoring.

Re-scoring keeps everything the audit did besides the penalty sum: the
predicted score is ``ai_score + penalty(old weights) − penalty(new weights)``.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from typing import Any, Iterable

from .evidence_audit import (
    PENALTIES,
    draft_hash,
    issue_category,
    normalize_audit_weights,
    penalty_total,
)

MIN_SAMPLES = 8
MAX_SAMPLES = 500
SHRINK_K = 8.0
MIN_CATEGORY_SAMPLES = 3
VERDICTS = {"agree", "disagree", "severity"}
SEVERITIES = ("high", "medium", "low")
PENALTY_GRID = {
    "high": [float(value) for value in range(8, 32, 2)],
    "medium": [float(value) for value in range(2, 17)],
    "low": [float(value) for value in range(0, 8)],
}
MULTIPLIER_GRID = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]


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


def upsert_sample(project: dict[str, Any], sample: dict[str, Any]) -> dict[str, Any]:
    """Insert or replace the sample of one chapter draft (chapter_id + draft_hash)."""
    calibration = ensure_calibration_defaults(project)
    clean = normalize_sample(sample)
    if clean is None:
        raise ValueError("校准样本缺少章节")
    samples = calibration["samples"]
    for index, existing in enumerate(samples):
        if existing["chapter_id"] == clean["chapter_id"] and existing["draft_hash"] == clean["draft_hash"]:
            clean["created_at"] = existing.get("created_at", clean["created_at"])
            clean["updated_at"] = _now()
            samples[index] = clean
            return clean
    samples.append(clean)
    calibration["samples"] = samples[-MAX_SAMPLES:]
    return clean


# ---------------------------------------------------------------------------
# Audited chapters (calibration candidates)
# ---------------------------------------------------------------------------


def chapter_ai_issues(chapter: dict[str, Any]) -> list[dict[str, Any]]:
    execution = chapter.get("execution") if isinstance(chapter.get("execution"), dict) else {}
    issues = execution.get("issues") if isinstance(execution.get("issues"), list) else []
    return [
        item for item in issues
        if isinstance(item, dict) and str(item.get("source", "AI")) != "本地"
    ]


def audited_chapters(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Chapters with a recorded audit score, with any sample for the current text."""
    samples = ensure_calibration_defaults(project)["samples"]
    by_key = {(item["chapter_id"], item["draft_hash"]): item for item in samples}
    latest_by_chapter: dict[str, dict[str, Any]] = {}
    for item in samples:
        latest_by_chapter[item["chapter_id"]] = item
    rows = []
    for number, chapter in enumerate(project.get("chapters", []), start=1):
        if not isinstance(chapter, dict):
            continue
        execution = chapter.get("execution") if isinstance(chapter.get("execution"), dict) else {}
        ai_score = _score(execution.get("audit_score"))
        if ai_score is None or not str(chapter.get("content", "")).strip():
            continue
        chapter_id = str(chapter.get("id", ""))
        current_hash = draft_hash(chapter.get("content", ""))
        sample = by_key.get((chapter_id, current_hash))
        verdicts = {item["issue_id"]: item for item in (sample or {}).get("issues", [])}
        # Verdicts recorded from the audit card may carry a client-side id;
        # fall back to matching category + message.
        by_text = {(item["category"], item["message"]): item for item in (sample or {}).get("issues", [])}
        issues = []
        for raw in chapter_ai_issues(chapter):
            clean = _normalize_issue(raw) or {}
            judged = verdicts.get(clean.get("issue_id", "")) or by_text.get(
                (clean.get("category", ""), clean.get("message", "")), {}
            )
            clean["verdict"] = judged.get("verdict", "")
            clean["corrected_severity"] = judged.get("corrected_severity", "")
            clean["suggestion"] = str(raw.get("suggestion", ""))[:240]
            issues.append(clean)
        previous = latest_by_chapter.get(chapter_id)
        rows.append({
            "chapter_id": chapter_id,
            "number": number,
            "title": str(chapter.get("title", "")),
            "ai_score": ai_score,
            "draft_hash": current_hash,
            "human_score": sample.get("human_score") if sample else None,
            "issues": issues,
            "has_sample": bool(sample),
            "stale_sample": bool(previous and not sample),
        })
    return rows


def sample_from_chapter(
    project: dict[str, Any],
    chapter_id: str,
    *,
    human_score: Any,
    verdicts: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build a sample from the chapter's current audit plus the author's input."""
    chapter = next(
        (item for item in project.get("chapters", []) if isinstance(item, dict) and str(item.get("id", "")) == chapter_id),
        None,
    )
    if chapter is None:
        raise KeyError(chapter_id)
    execution = chapter.get("execution") if isinstance(chapter.get("execution"), dict) else {}
    ai_score = _score(execution.get("audit_score"))
    if ai_score is None:
        raise ValueError("该章还没有审计分数，无法记录校准样本")
    by_id = {
        str(item.get("issue_id", "")): item for item in verdicts
        if isinstance(item, dict) and str(item.get("issue_id", "")).strip()
    }
    issues = []
    for raw in chapter_ai_issues(chapter):
        clean = _normalize_issue(raw)
        if clean is None:
            continue
        judged = by_id.get(clean["issue_id"], {})
        issues.append({
            **clean,
            "verdict": judged.get("verdict", ""),
            "corrected_severity": judged.get("corrected_severity", ""),
        })
    return {
        "chapter_id": chapter_id,
        "chapter_title": str(chapter.get("title", "")),
        "draft_hash": draft_hash(chapter.get("content", "")),
        "human_score": human_score,
        "ai_score": ai_score,
        "issues": issues,
        "audit_weights": project.get("settings", {}).get("audit_weights") or {},
    }


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return round(cov / math.sqrt(var_x * var_y), 3)


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = average
        i = j + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    return _pearson(_ranks(xs), _ranks(ys))


def _counted(sample: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in sample.get("issues", []) if item.get("counted", True)]


def predicted_score(sample: dict[str, Any], weights: dict[str, Any] | None) -> float:
    issues = _counted(sample)
    before = penalty_total(issues, sample.get("audit_weights") or None)
    after = penalty_total(issues, weights or None)
    return max(0.0, min(100.0, float(sample["ai_score"]) + before - after))


def _mae(samples: list[dict[str, Any]], weights: dict[str, Any] | None) -> float:
    if not samples:
        return 0.0
    return sum(abs(predicted_score(item, weights) - item["human_score"]) for item in samples) / len(samples)


def category_stats(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for sample in samples:
        for issue in sample.get("issues", []):
            row = rows.setdefault(issue["category"], {
                "category": issue["category"], "issues": 0, "judged": 0,
                "agree": 0, "disagree": 0, "severity": 0, "samples": set(),
            })
            row["issues"] += 1
            row["samples"].add((sample["chapter_id"], sample["draft_hash"]))
            if issue.get("verdict") in VERDICTS:
                row["judged"] += 1
                row[issue["verdict"]] += 1
    result = []
    for row in rows.values():
        judged = row["judged"]
        result.append({
            **{key: value for key, value in row.items() if key != "samples"},
            "sample_count": len(row["samples"]),
            "false_positive_rate": round(row["disagree"] / judged, 3) if judged else None,
        })
    result.sort(key=lambda item: (-(item["false_positive_rate"] or 0), -item["judged"], item["category"]))
    return result


def _fit(samples: list[dict[str, Any]], base: dict[str, Any]) -> dict[str, Any]:
    """Coordinate search over the penalty and multiplier grids (MAE objective)."""
    penalties = {**PENALTIES, **base.get("penalties", {})}
    penalties = {key: float(value) for key, value in penalties.items()}
    support: dict[str, int] = {}
    for sample in samples:
        for category in {issue["category"] for issue in _counted(sample)}:
            support[category] = support.get(category, 0) + 1
    categories = sorted(name for name, count in support.items() if count >= MIN_CATEGORY_SAMPLES)
    multipliers = {name: float(base.get("category_multipliers", {}).get(name, 1.0)) for name in categories}

    def weights() -> dict[str, Any]:
        return {"penalties": dict(penalties), "category_multipliers": dict(multipliers)}

    def objective() -> float:
        return _mae(samples, weights())

    best = objective()
    for _round in range(4):
        improved = False
        for severity in SEVERITIES:
            current = penalties[severity]
            for value in PENALTY_GRID[severity]:
                penalties[severity] = value
                score = objective()
                # Ties go to the value closest to the default.
                if score < best - 1e-9 or (
                    abs(score - best) <= 1e-9
                    and abs(value - PENALTIES[severity]) < abs(current - PENALTIES[severity])
                ):
                    if score < best - 1e-9:
                        improved = True
                    best, current = score, value
            penalties[severity] = current
        for name in categories:
            current = multipliers[name]
            for value in MULTIPLIER_GRID:
                multipliers[name] = value
                score = objective()
                if score < best - 1e-9 or (abs(score - best) <= 1e-9 and abs(value - 1.0) < abs(current - 1.0)):
                    if score < best - 1e-9:
                        improved = True
                    best, current = score, value
            multipliers[name] = current
        if not improved:
            break
    n = len(samples)
    shrink = n / (n + SHRINK_K)
    shrunk_penalties = {
        severity: round(PENALTIES[severity] + (penalties[severity] - PENALTIES[severity]) * shrink, 1)
        for severity in SEVERITIES
    }
    shrunk_multipliers = {}
    for name in categories:
        factor = support[name] / (support[name] + SHRINK_K)
        value = round(1.0 + (multipliers[name] - 1.0) * factor, 2)
        if abs(value - 1.0) >= 0.05:
            shrunk_multipliers[name] = value
    clean: dict[str, Any] = {}
    changed_penalties = {
        key: value for key, value in shrunk_penalties.items() if abs(value - PENALTIES[key]) >= 0.5
    }
    if changed_penalties:
        clean["penalties"] = {**{key: float(PENALTIES[key]) for key in SEVERITIES}, **changed_penalties}
    if shrunk_multipliers:
        clean["category_multipliers"] = shrunk_multipliers
    return normalize_audit_weights(clean)


def weights_diff(current: dict[str, Any], suggested: dict[str, Any]) -> list[dict[str, Any]]:
    current = normalize_audit_weights(current)
    suggested = normalize_audit_weights(suggested)
    rows = []
    for severity in SEVERITIES:
        before = float(current.get("penalties", {}).get(severity, PENALTIES[severity]))
        after = float(suggested.get("penalties", {}).get(severity, PENALTIES[severity]))
        if abs(before - after) >= 0.05:
            rows.append({"kind": "penalty", "key": severity, "before": before, "after": after})
    names = sorted(set(current.get("category_multipliers", {})) | set(suggested.get("category_multipliers", {})))
    for name in names:
        before = float(current.get("category_multipliers", {}).get(name, 1.0))
        after = float(suggested.get("category_multipliers", {}).get(name, 1.0))
        if abs(before - after) >= 0.01:
            rows.append({"kind": "category", "key": name, "before": before, "after": after})
    return rows


def calibration_stats(
    samples: Iterable[dict[str, Any]], current_weights: dict[str, Any] | None = None
) -> dict[str, Any]:
    clean = [item for item in map(normalize_sample, samples) if item]
    scored = [item for item in clean if item["human_score"] is not None and item["ai_score"] is not None]
    current = normalize_audit_weights(current_weights)
    ai = [float(item["ai_score"]) for item in scored]
    human = [float(item["human_score"]) for item in scored]
    mae = round(sum(abs(a - h) for a, h in zip(ai, human)) / len(scored), 2) if scored else None
    bias = round(sum(a - h for a, h in zip(ai, human)) / len(scored), 2) if scored else None
    categories = category_stats(clean)
    result: dict[str, Any] = {
        "sample_count": len(clean),
        "scored_count": len(scored),
        "judged_issues": sum(row["judged"] for row in categories),
        "min_samples": MIN_SAMPLES,
        "mae": mae,
        "bias": bias,
        "pearson": _pearson(ai, human),
        "spearman": _spearman(ai, human),
        "categories": categories,
        "top_false_positive": [
            row for row in categories
            if row["false_positive_rate"] is not None and row["judged"] >= 2 and row["false_positive_rate"] > 0
        ][:5],
        "current_weights": current,
        "suggestion": None,
    }
    if len(scored) < MIN_SAMPLES:
        result["suggestion_note"] = f"至少需要 {MIN_SAMPLES} 个带人工评分的样本才给出权重建议（当前 {len(scored)} 个）"
        return result
    suggested = _fit(scored, current)
    mae_current = round(_mae(scored, current or None), 2)
    mae_suggested = round(_mae(scored, suggested or None), 2)
    diff = weights_diff(current, suggested)
    if mae_suggested >= mae_current - 0.05:
        result["suggestion_note"] = "当前权重与人工评分已足够接近，暂不建议调整"
        diff = []
        suggested = current
        mae_suggested = mae_current
    result["suggestion"] = {
        "weights": suggested,
        "diff": diff,
        "mae_current": mae_current,
        "mae_suggested": mae_suggested,
    }
    return result

"""Deterministic editorial gates and refinement ranking."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..domain.prose import _director_candidate_gate_failures


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _audit_issues(result: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    for source, values in (
        ("本地", result.get("local_checks", {}).get("issues", [])),
        ("AI", result.get("issues", [])),
    ):
        for item in values if isinstance(values, list) else []:
            if isinstance(item, dict):
                issues.append({**item, "source": source})
    return issues[:8]


def _systemic_quality_issues(result: dict[str, Any]) -> list[dict[str, Any]]:
    systemic_categories = {
        "跨章重复段落", "跨章重复长句", "重复段落", "重复内容",
        "剧情停滞", "章节目标偏离", "时代语汇失真", "人物状态", "时间线",
    }
    return [
        item
        for item in _audit_issues(result)
        if str(item.get("category", "")) in systemic_categories
        or any(
            marker in str(item.get("category", ""))
            for marker in ("跨章", "重复", "时间线", "阶段偏离", "目标偏离", "时代语汇")
        )
        or (
            str(item.get("severity", "")) == "high"
            and any(marker in str(item.get("category", "")) for marker in ("重复", "目标", "逻辑"))
        )
    ]


def _quality_directives(result: dict[str, Any]) -> list[str]:
    directives: list[str] = []
    issues = _audit_issues(result)
    local_issues = [item for item in issues if item.get("source") == "本地"]
    # AI audit may lower the score correctly while proposing a new future
    # foreshadowing or prop. Do not feed creative audit hallucinations into the
    # next draft when deterministic findings already give a sufficient brief.
    for issue in (local_issues or issues):
        category = str(issue.get("category", "问题"))
        suggestion = str(issue.get("suggestion", "")).strip()
        message = str(issue.get("message", "")).strip()
        directives.append(f"避免再次出现[{category}]：{suggestion or message}")
    return list(dict.fromkeys(directives))[:6]


def _director_manuscript_gate_failures(
    health: dict[str, Any], *, final: bool = False, genre: str = ""
) -> list[str]:
    """Translate the deterministic health report into production stop reasons.

    Intermediate checkpoints stop only on structural contamination that will
    become more expensive to repair later.  The final checkpoint additionally
    enforces release-level language and memory integrity requirements.
    """
    failures: list[str] = []
    if health.get("duplicate_titles"):
        failures.append("出现重复章名")
    if int(health.get("duplicate_passage_count", 0)):
        failures.append("出现跨章完全重复段落")
    if health.get("similar_chapters"):
        failures.append("出现高相似章节")
    if health.get("volume_progression_issues"):
        failures.append("分卷状态或场域发生结构性重复")
    if final:
        score = int(health.get("score", 0))
        if score < 82:
            failures.append(f"全稿健康分 {score}，低于发布线 82")
        memory_issues = health.get("memory_integrity_issues", [])
        if memory_issues:
            failures.append(f"仍有 {len(memory_issues)} 项记忆或线索完整性问题")
        historical = any(
            marker in str(genre) for marker in ("历史", "古代", "战国", "架空")
        )
        if historical:
            total_jargon = sum(
                int(item.get("count", 0) or 0)
                for item in health.get("modern_jargon", [])
                if isinstance(item, dict)
            )
            character_count = int(health.get("character_count", 0) or 0)
            jargon_limit = max(12, character_count // 1500)
            if total_jargon > jargon_limit:
                failures.append(
                    f"现代抽象术语共 {total_jargon} 次，超过发布线 {jargon_limit} 次"
                )
    return failures


def _refinement_candidate_passes(
    audit: dict[str, Any], contract_result: dict[str, Any], threshold: int
) -> bool:
    """Apply the user-selected refinement threshold without a hidden 85-point gate."""
    if audit.get("requires_review") or audit.get("verdict") == "partial":
        return False
    if int(audit.get("score", 0) or 0) < int(threshold):
        return False
    if contract_result and not contract_result.get("passed", False):
        return False
    local = audit.get("local_checks", {})
    local_issues = local.get("issues", []) if isinstance(local, dict) else []
    if not isinstance(local_issues, list):
        local_issues = []
    if isinstance(local, dict) and local.get("verdict") not in {None, "", "pass"}:
        return False
    if any(
        isinstance(item, dict)
        and str(item.get("severity", "")).lower() in {"high", "medium"}
        for item in local_issues
    ):
        return False
    # The evidence audit intentionally keeps unverified model suggestions visible.
    # Only verified high-risk AI findings are hard blockers here; their score still
    # contributes to the configured threshold.
    ai_issues = audit.get("issues", [])
    if not isinstance(ai_issues, list):
        ai_issues = []
    if any(
        isinstance(item, dict)
        and str(item.get("severity", "")).lower() == "high"
        and bool(item.get("evidence_verified", False))
        for item in ai_issues
    ):
        return False
    return True


def _refinement_candidate_rank(
    draft: str,
    audit: dict[str, Any],
    contract_result: dict[str, Any],
    threshold: int,
    target_chars: int,
) -> tuple[int, int, int]:
    """Prefer passing drafts, then fewer deterministic blockers, then score."""
    local = audit.get("local_checks", {})
    local_issues = local.get("issues", []) if isinstance(local, dict) else []
    if not isinstance(local_issues, list):
        local_issues = []
    blockers = sum(
        1
        for item in local_issues
        if isinstance(item, dict)
        and str(item.get("severity", "")).lower() in {"high", "medium"}
    )
    blockers += len(contract_result.get("violations", [])) if contract_result else 0
    blockers += len(_director_candidate_gate_failures(draft, target_chars))
    passed = _refinement_candidate_passes(audit, contract_result, threshold)
    return (1 if passed else 0, -blockers, int(audit.get("score", 0) or 0))


def _resolve_project_repairs(project: dict[str, Any], chapter_id: str) -> None:
    for item in project.get("repair_queue", []):
        if (
            isinstance(item, dict)
            and str(item.get("chapter_id", "")) == chapter_id
            and str(item.get("status", "queued")) in {"queued", "in_progress"}
        ):
            item["status"] = "resolved"
            item["resolved_by"] = "auto-refine"
            item["updated_at"] = _utc_now()


def _targeted_refinement_findings(audit: dict[str, Any]) -> list[dict[str, str]]:
    """Return a few locally verified excerpts suitable for surgical repair."""
    local = audit.get("local_checks", {})
    issues = local.get("issues", []) if isinstance(local, dict) else []
    if not isinstance(issues, list):
        return []
    blockers = [
        item for item in issues
        if isinstance(item, dict)
        and str(item.get("severity", "")).lower() in {"high", "medium"}
        and str(item.get("category", "")) != "长度"
    ]
    if not blockers or len(blockers) > 3:
        return []
    findings: list[dict[str, str]] = []
    for item in blockers:
        quotes: list[str] = []
        evidence = item.get("evidence", [])
        for entry in evidence if isinstance(evidence, list) else []:
            quote = (
                str(entry.get("quote", ""))
                if isinstance(entry, dict)
                else str(entry)
            ).strip()
            if len(quote) >= 18:
                quotes.append(quote)
        if not quotes:
            return []
        for quote in quotes[:6]:
            findings.append(
                {
                    "quote": quote,
                    "category": str(item.get("category", "局部问题")),
                    "message": str(item.get("message", "")),
                    "suggestion": str(item.get("suggestion", "")),
                }
            )
    unique: dict[str, dict[str, str]] = {}
    for item in findings:
        unique.setdefault(item["quote"], item)
    return list(unique.values())[:6]




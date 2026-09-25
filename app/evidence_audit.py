from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


PENALTIES = {"high": 18, "medium": 8, "low": 3}


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _evidence_values(issue: dict[str, Any]) -> list[str]:
    value = issue.get("evidence", issue.get("quote", []))
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item.get("quote", "") if isinstance(item, dict) else item).strip() for item in value if str(item).strip()]


def bind_evidence(text: str, issue: dict[str, Any], *, trusted: bool = False) -> dict[str, Any]:
    clean = deepcopy(issue)
    evidence: list[dict[str, Any]] = []
    compact_text = _compact(text)
    for quote in _evidence_values(clean)[:8]:
        compact_quote = _compact(quote)
        verified = bool(compact_quote and compact_quote in compact_text)
        start = text.find(quote) if quote else -1
        evidence.append({"quote": quote[:240], "verified": verified, "start": start})
    clean["evidence"] = evidence
    clean["evidence_verified"] = bool(trusted or any(item["verified"] for item in evidence))
    clean["severity"] = str(clean.get("severity", "medium")).lower()
    if clean["severity"] not in PENALTIES:
        clean["severity"] = "medium"
    return clean


def stable_audit_result(
    text: str,
    ai_result: dict[str, Any],
    local_checks: dict[str, Any],
    *,
    threshold: int = 85,
) -> dict[str, Any]:
    ai_issues = [bind_evidence(text, item) for item in ai_result.get("issues", []) if isinstance(item, dict)]
    local_issues = [bind_evidence(text, item, trusted=True) for item in local_checks.get("issues", []) if isinstance(item, dict)]
    verified_ai = [item for item in ai_issues if item.get("evidence_verified")]
    penalty = sum(PENALTIES[item["severity"]] for item in verified_ai)
    local_score = max(0, min(100, int(local_checks.get("score", 0) or 0)))
    provider_score = max(0, min(100, int(ai_result.get("score", 100) or 0)))
    evidence_score = min(provider_score, max(0, 100 - penalty))
    score = min(local_score, evidence_score)
    high = any(item.get("severity") == "high" for item in verified_ai + local_issues)
    local_pass = str(local_checks.get("verdict", "revise")) == "pass"
    unresolved = [item for item in ai_issues if not item.get("evidence_verified") and item["severity"] in {"high", "medium"}]
    if str(ai_result.get("verdict", "")) == "partial" or unresolved:
        verdict = "partial"
    else:
        verdict = "pass" if local_pass and not high and score >= threshold else "revise"
    return {
        **deepcopy(ai_result),
        "score": score,
        "verdict": verdict,
        "issues": ai_issues,
        "local_checks": {**deepcopy(local_checks), "issues": local_issues},
        "evidence_policy": "verified_quotes_only",
        "requires_review": bool(unresolved) or verdict == "partial",
        "quality_dimensions": {"mechanical": local_score, "continuity": evidence_score,
                               "literary": None, "review_complete": verdict != "partial"},
        "score_components": {
            "local_score": local_score,
            "evidence_score": evidence_score,
            "verified_ai_penalty": penalty,
            "verified_ai_issues": len(verified_ai),
            "unverified_ai_issues": len(ai_issues) - len(verified_ai),
            "threshold": threshold,
        },
    }

"""Evidence binding for model-produced audit findings.

Small critic models rarely reproduce a quotation byte-for-byte.  Production
telemetry showed that more than 90% of AI audit quotes failed exact matching,
which left almost every finding in "needs review" and stalled refinement.

The audit prompt therefore numbers the draft (``[P3-S2]`` = paragraph 3,
sentence 2) and asks the model for ``evidence_ids``.  Evidence is resolved
locally in this order:

1. ``evidence_ids`` → the exact sentence text from the numbered draft;
2. quote, exact match after whitespace compaction;
3. quote, fuzzy match (dependency-free partial ratio, threshold 0.9);
4. otherwise unverified.

The stored quote is always the *original draft text*, never the model's
paraphrase.  Every issue is bound to the SHA-256 prefix of the audited draft;
an issue that carries a different ``draft_hash`` can never be verified against
another draft.
"""

from __future__ import annotations

import hashlib
import re
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Any


PENALTIES = {"high": 18, "medium": 8, "low": 3}
FUZZY_THRESHOLD = 0.9
# Split after terminal punctuation, but never inside a run of terminators
# ("……", "？！") or before a closing quote.
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?…])(?![”’」』）)\"'…。！？!?])")
# Overlong "sentences" (no terminal punctuation) are split again at semicolons
# so one ID never covers more than a quotable span.
_LONG_SENTENCE = 120
_CLAUSE_SPLIT = re.compile(r"(?<=[；;])")
_ID_RE = re.compile(r"P(\d+)\s*-\s*S(\d+)", re.IGNORECASE)


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def draft_hash(text: Any) -> str:
    """Stable identity of a draft, insensitive to whitespace-only edits."""
    return hashlib.sha256(_compact(text).encode("utf-8")).hexdigest()[:16]


def split_draft_units(text: str) -> list[tuple[str, int, str]]:
    """Return ``(id, start_offset, sentence)`` units in draft order."""
    units: list[tuple[str, int, str]] = []
    source = str(text or "")
    paragraph_number = 0
    for match in re.finditer(r"[^\n]+", source):
        paragraph = match.group(0)
        if not paragraph.strip():
            continue
        paragraph_number += 1
        offset = match.start()
        sentence_number = 0
        pieces: list[str] = []
        for piece in _SENTENCE_SPLIT.split(paragraph):
            if len(piece.strip()) > _LONG_SENTENCE:
                pieces.extend(_CLAUSE_SPLIT.split(piece))
            else:
                pieces.append(piece)
        for piece in pieces:
            if not piece.strip():
                offset += len(piece)
                continue
            sentence_number += 1
            leading = len(piece) - len(piece.lstrip())
            units.append(
                (f"P{paragraph_number}-S{sentence_number}", offset + leading, piece.strip())
            )
            offset += len(piece)
    return units


def number_draft(text: str) -> str:
    """Render the draft with ``[P#-S#]`` markers for evidence references."""
    lines: list[str] = []
    current = ""
    buffer: list[str] = []
    for unit_id, _start, sentence in split_draft_units(text):
        paragraph = unit_id.split("-", 1)[0]
        if paragraph != current and buffer:
            lines.append("".join(buffer))
            buffer = []
        current = paragraph
        buffer.append(f"[{unit_id}]{sentence}")
    if buffer:
        lines.append("".join(buffer))
    return "\n".join(lines)


def _unit_index(text: str) -> dict[str, tuple[int, str]]:
    return {unit_id.upper(): (start, sentence) for unit_id, start, sentence in split_draft_units(text)}


def _parse_ids(value: Any) -> list[str | tuple[str, str]]:
    if isinstance(value, str):
        value = re.split(r"[,，;；\s]+", value)
    if not isinstance(value, list):
        return []
    parsed: list[str | tuple[str, str]] = []
    for item in value:
        raw = str(item or "")
        found = [f"P{int(a)}-S{int(b)}" for a, b in _ID_RE.findall(raw)]
        if len(found) >= 2 and re.search(r"[~～至到]|--|—", raw):
            parsed.append((found[0], found[-1]))
        else:
            parsed.extend(found)
    return parsed[:8]


def _resolve_ids(text: str, ids: list[str | tuple[str, str]]) -> list[dict[str, Any]]:
    index = _unit_index(text)
    order = list(index)
    resolved: list[dict[str, Any]] = []
    for item in ids:
        if isinstance(item, tuple):
            left, right = item
            if left not in index or right not in index:
                resolved.append({"id": f"{left}~{right}", "verified": False, "match": "invalid_id"})
                continue
            a, b = order.index(left), order.index(right)
            if a > b:
                a, b = b, a
            span = order[a : min(b, a + 5) + 1]
            start = index[span[0]][0]
            last_start, last_sentence = index[span[-1]]
            # Slice the original draft so separators (spaces, line breaks between
            # paragraphs) survive and the quote can be found verbatim later.
            quote = str(text or "")[start : last_start + len(last_sentence)]
            resolved.append({"id": f"{span[0]}~{span[-1]}", "quote": quote[:240], "start": start,
                             "verified": True, "match": "id", "ratio": 1.0})
        elif item in index:
            start, sentence = index[item]
            resolved.append({"id": item, "quote": sentence[:240], "start": start,
                             "verified": True, "match": "id", "ratio": 1.0})
        else:
            resolved.append({"id": item, "verified": False, "match": "invalid_id"})
    return resolved


def _compact_map(text: str) -> tuple[str, list[int]]:
    chars: list[str] = []
    positions: list[int] = []
    for index, char in enumerate(str(text or "")):
        if char.isspace():
            continue
        chars.append(char.casefold())
        positions.append(index)
    return "".join(chars), positions


def fuzzy_locate(text: str, quote: str, threshold: float = FUZZY_THRESHOLD) -> tuple[str, int, float]:
    """Find the draft span that best matches ``quote``.

    Returns ``(original_span, start, ratio)``; ``("", -1, best_ratio)`` when no
    window reaches ``threshold``.  Candidate windows are anchored on bigrams of
    the quote so the search stays linear in practice for chapter-sized drafts.
    """
    compact_text, positions = _compact_map(text)
    target = _compact(quote)
    if not target or not compact_text:
        return "", -1, 0.0
    exact = compact_text.find(target)
    if exact >= 0:
        start, end = positions[exact], positions[exact + len(target) - 1] + 1
        return str(text)[start:end], start, 1.0
    if len(target) < 6:
        return "", -1, 0.0
    size = len(target)
    anchors: set[int] = set()
    step = max(1, size // 8)
    for offset in range(0, size - 1, step):
        gram = target[offset : offset + 2]
        found = compact_text.find(gram)
        hits = 0
        while found >= 0 and hits < 64:
            anchors.add(max(0, found - offset))
            found = compact_text.find(gram, found + 1)
            hits += 1
    best = (0.0, -1, 0)
    for anchor in sorted(anchors):
        for length in {size, int(size * 0.9), int(size * 1.1)}:
            window = compact_text[anchor : anchor + max(1, length)]
            if not window:
                continue
            matcher = SequenceMatcher(None, target, window, autojunk=False)
            if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
                continue
            ratio = matcher.ratio()
            if ratio > best[0]:
                best = (ratio, anchor, len(window))
    ratio, anchor, length = best
    if ratio < threshold or anchor < 0:
        return "", -1, round(ratio, 3)
    start = positions[anchor]
    end = positions[min(len(positions) - 1, anchor + length - 1)] + 1
    return str(text)[start:end], start, round(ratio, 3)


def verify_quote(text: str, quote: Any, threshold: float = FUZZY_THRESHOLD) -> dict[str, Any]:
    """Verify a single quotation; the returned quote is the original draft span."""
    raw = str(quote or "").strip()
    if not raw:
        return {"quote": "", "verified": False, "start": -1, "match": "none", "ratio": 0.0}
    span, start, ratio = fuzzy_locate(text, raw, threshold)
    if span:
        item = {"quote": span[:240], "verified": True, "start": start,
                "match": "exact" if ratio >= 1.0 else "fuzzy", "ratio": ratio}
        if _compact(span) != _compact(raw):
            item["model_quote"] = raw[:240]
        return item
    return {"quote": raw[:240], "verified": False, "start": -1, "match": "none", "ratio": ratio}


def _evidence_values(issue: dict[str, Any]) -> list[str]:
    value = issue.get("evidence", issue.get("quote", []))
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    values: list[str] = []
    for item in value:
        if isinstance(item, dict):
            # Re-binding an already bound issue: prefer the model's own words so
            # verification is recomputed rather than trusted.
            text = str(item.get("model_quote") or item.get("quote") or "").strip()
        else:
            text = str(item).strip()
        if text:
            values.append(text)
    return values


def bind_evidence(
    text: str,
    issue: dict[str, Any],
    *,
    trusted: bool = False,
    expected_hash: str | None = None,
) -> dict[str, Any]:
    clean = deepcopy(issue)
    current_hash = draft_hash(text)
    issue_hash = str(clean.get("draft_hash") or "").strip()
    mismatch = bool(
        (issue_hash and issue_hash != current_hash)
        or (expected_hash and expected_hash != current_hash)
    )
    evidence: list[dict[str, Any]] = []
    if mismatch and not trusted:
        for quote in _evidence_values(clean)[:8]:
            evidence.append({"quote": quote[:240], "verified": False, "start": -1, "match": "draft_mismatch"})
        clean["evidence_status"] = "draft_mismatch"
    else:
        ids = _parse_ids(clean.get("evidence_ids", []))
        evidence.extend(_resolve_ids(text, ids))
        quotes = _evidence_values(clean)[:8]
        if not any(item.get("verified") for item in evidence) or not ids:
            for quote in quotes:
                evidence.append(verify_quote(text, quote))
        # Drop unresolved quote duplicates once an ID resolved the same issue.
        if any(item.get("verified") for item in evidence):
            evidence = [item for item in evidence if item.get("verified")] or evidence
    clean["evidence"] = evidence[:8]
    clean["draft_hash"] = current_hash
    clean["evidence_verified"] = bool(
        trusted or (not mismatch and any(item.get("verified") for item in evidence))
    )
    clean["severity"] = str(clean.get("severity", "medium")).lower()
    if clean["severity"] not in PENALTIES:
        clean["severity"] = "medium"
    return clean


def normalize_model_score(
    value: Any, issues: list[Any] | None = None, *, scale: int | None = None
) -> int | None:
    """Normalize a critic score to 0–100.

    * ``scale=100`` marks a score that is already on the 0–100 scale (for
      example one computed locally as ``100 - penalties``); it is never
      rescaled.  ``scale=10`` forces the 10-point reading.
    * otherwise values in (0, 10] are read as a 10-point scale and multiplied
      by 10 — unless a high-severity issue is reported, in which case a low
      score is taken literally (a real 8/100 must not become 80);
    * a literal 0 without any high-severity issue is treated as "missing"
      (``None``) because small models emit 0 as a schema placeholder;
    * everything else is clamped to 0–100.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    has_high = any(
        isinstance(item, dict) and str(item.get("severity", "")).lower() == "high"
        for item in (issues or [])
    )
    if 0 < number <= 10 and (scale == 10 or (scale is None and not has_high)):
        number *= 10
    if number <= 0:
        return 0 if has_high else None
    return int(round(max(0.0, min(100.0, number))))


def stable_audit_result(
    text: str,
    ai_result: dict[str, Any],
    local_checks: dict[str, Any],
    *,
    threshold: int = 85,
) -> dict[str, Any]:
    current_hash = draft_hash(text)
    ai_issues = [bind_evidence(text, item) for item in ai_result.get("issues", []) if isinstance(item, dict)]
    local_issues = [bind_evidence(text, item, trusted=True) for item in local_checks.get("issues", []) if isinstance(item, dict)]
    verified_ai = [item for item in ai_issues if item.get("evidence_verified")]
    penalty = sum(PENALTIES[item["severity"]] for item in verified_ai)
    local_score = max(0, min(100, int(local_checks.get("score", 0) or 0)))
    if ai_result.get("score_normalized"):
        normalized = max(0, min(100, int(ai_result.get("score", 100) or 0)))
    else:
        # Local fallbacks and locally aggregated critics already score 0–100.
        local_scale = bool(ai_result.get("fallback")) or ai_result.get("critic_mode") == "focused"
        scale = 100 if local_scale else ai_result.get("score_scale")
        normalized = normalize_model_score(
            ai_result.get("score", 100), ai_result.get("issues", []),
            scale=scale if scale in (10, 100) else None,
        )
    provider_score = 100 if normalized is None else normalized
    evidence_score = min(provider_score, max(0, 100 - penalty))
    score = min(local_score, evidence_score)
    high = any(item.get("severity") == "high" for item in verified_ai + local_issues)
    local_pass = str(local_checks.get("verdict", "revise")) == "pass"
    unresolved = [item for item in ai_issues if not item.get("evidence_verified") and item["severity"] in {"high", "medium"}]
    if str(ai_result.get("verdict", "")) == "partial" or unresolved:
        verdict = "partial"
    else:
        verdict = "pass" if local_pass and not high and score >= threshold else "revise"
    matches: dict[str, int] = {}
    for issue in ai_issues:
        for item in issue.get("evidence", []):
            key = str(item.get("match", "none"))
            matches[key] = matches.get(key, 0) + 1
    return {
        **deepcopy(ai_result),
        "score": score,
        "verdict": verdict,
        "issues": ai_issues,
        "draft_hash": current_hash,
        "local_checks": {**deepcopy(local_checks), "issues": local_issues},
        "evidence_policy": "verified_quotes_only",
        "requires_review": bool(unresolved) or verdict == "partial",
        "quality_dimensions": {"mechanical": local_score, "continuity": evidence_score,
                               "literary": None, "review_complete": verdict != "partial"},
        "score_components": {
            "local_score": local_score,
            "evidence_score": evidence_score,
            "provider_score": normalized,
            "verified_ai_penalty": penalty,
            "verified_ai_issues": len(verified_ai),
            "unverified_ai_issues": len(ai_issues) - len(verified_ai),
            "evidence_matches": matches,
            "threshold": threshold,
        },
    }

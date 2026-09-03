from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Sequence

from .memory import relevance

REFERENCE_KINDS = {"style", "canon", "background", "research"}


def _clean(value: Any) -> str:
    return str(value or "").strip()


def ensure_reference_defaults(project: dict[str, Any]) -> dict[str, Any]:
    refs = project.get("references")
    if not isinstance(refs, list):
        refs = []
        project["references"] = refs
    clean_refs: list[dict[str, Any]] = []
    for raw in refs:
        if not isinstance(raw, dict):
            continue
        text = _clean(raw.get("text"))
        name = _clean(raw.get("name")) or "未命名资料"
        if not text:
            continue
        kind = _clean(raw.get("kind")).lower() or "background"
        if kind not in REFERENCE_KINDS:
            kind = "background"
        item = dict(raw)
        item["id"] = _clean(item.get("id")) or f"ref_{uuid.uuid4().hex[:16]}"
        item["name"] = name
        item["kind"] = kind
        item["text"] = text
        item["sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        item["enabled"] = item.get("enabled", True) is not False
        item["user_verified"] = bool(item.get("user_verified", kind != "canon"))
        item["source_work"] = _clean(item.get("source_work"))
        item["notes"] = _clean(item.get("notes"))
        clean_refs.append(item)
    project["references"] = clean_refs[-80:]
    return project


def split_reference(text: str, *, max_chars: int = 1400) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n+", text) if part.strip()]
    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        candidate = f"{buffer}\n{paragraph}".strip()
        if buffer and len(candidate) > max_chars:
            chunks.append(buffer)
            buffer = paragraph
        else:
            buffer = candidate
    if buffer:
        chunks.append(buffer)
    if not chunks and text.strip():
        chunks = [text[i : i + max_chars] for i in range(0, len(text), max_chars)]
    return chunks


def relevant_reference_chunks(
    project: dict[str, Any],
    query: str,
    *,
    kinds: Sequence[str] = ("canon", "background", "research"),
    limit: int = 5,
) -> list[dict[str, Any]]:
    ensure_reference_defaults(project)
    allowed = set(kinds)
    scored: list[tuple[float, int, dict[str, Any]]] = []
    for ref_index, ref in enumerate(project.get("references", [])):
        if (
            not isinstance(ref, dict)
            or ref.get("enabled", True) is False
            or ref.get("kind") not in allowed
        ):
            continue
        for chunk_index, chunk in enumerate(split_reference(_clean(ref.get("text")))):
            score = relevance(query, chunk)
            # Canon/reference documents are useful when the query mentions the work/name
            # even if lexical retrieval is otherwise sparse.
            if _clean(ref.get("name")) and _clean(ref.get("name")) in query:
                score += 3
            if _clean(ref.get("source_work")) and _clean(ref.get("source_work")) in query:
                score += 2
            if score <= 0 and query.strip():
                continue
            scored.append(
                (
                    score,
                    -ref_index * 1000 - chunk_index,
                    {
                        "reference_id": ref.get("id"),
                        "name": ref.get("name"),
                        "kind": ref.get("kind"),
                        "source_work": ref.get("source_work", ""),
                        "user_verified": bool(ref.get("user_verified", False)),
                        "chunk": chunk,
                    },
                )
            )
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[: max(0, limit)]]


def render_reference_context(project: dict[str, Any], query: str, *, max_chars: int = 4000) -> str:
    chunks = relevant_reference_chunks(project, query)
    if not chunks:
        return ""
    lines = [
        "【相关资料检索】",
        "这些是检索到的证据片段。资料只用于核对事实和背景；不得复制其连续措辞到正文。",
    ]
    for item in chunks:
        label = f"{item['name']}｜{item['kind']}"
        if item.get("source_work"):
            label += f"｜作品={item['source_work']}"
        if item.get("kind") == "canon" and not item.get("user_verified"):
            label += "｜未人工核对"
        lines.append(f"\n[{label}]\n{item['chunk']}")
    return "\n".join(lines)[:max_chars]


def combined_style_corpus(project: dict[str, Any], *, max_chars: int = 120_000) -> str:
    ensure_reference_defaults(project)
    texts: list[str] = []
    total = 0
    for ref in project.get("references", []):
        if not isinstance(ref, dict) or ref.get("kind") != "style" or ref.get("enabled", True) is False:
            continue
        text = _clean(ref.get("text"))
        if not text:
            continue
        remaining = max_chars - total
        if remaining <= 0:
            break
        texts.append(text[:remaining])
        total += min(len(text), remaining)
    return "\n\n===== 样文分隔 =====\n\n".join(texts)


def source_similarity_report(project: dict[str, Any], draft: str) -> dict[str, Any]:
    """Detect suspicious phrase overlap with uploaded style references.

    The goal is style transfer, not source-text reproduction. This deterministic guard
    flags long exact spans before a draft is accepted.
    """
    ensure_reference_defaults(project)
    compact_draft = re.sub(r"\s+", "", _clean(draft))
    if len(compact_draft) < 40:
        return {"risk": "low", "matches": [], "max_match_chars": 0}
    matches: list[dict[str, Any]] = []
    # Sliding 24-character windows are conservative for Chinese prose: common short
    # phrases do not trigger, but copied sentences and distinctive clauses do.
    window = 24
    seen: set[str] = set()
    for ref in project.get("references", []):
        if not isinstance(ref, dict) or ref.get("kind") != "style" or ref.get("enabled", True) is False:
            continue
        source = re.sub(r"\s+", "", _clean(ref.get("text")))
        if len(source) < window:
            continue
        # Sample every 6 chars from draft; when a hit is found, extend both sides.
        for start in range(0, max(1, len(compact_draft) - window + 1), 6):
            needle = compact_draft[start : start + window]
            if len(needle) < window or needle in seen:
                continue
            source_pos = source.find(needle)
            if source_pos < 0:
                continue
            seen.add(needle)
            left = 0
            while start - left - 1 >= 0 and source_pos - left - 1 >= 0 and compact_draft[start-left-1] == source[source_pos-left-1]:
                left += 1
            right = window
            while start + right < len(compact_draft) and source_pos + right < len(source) and compact_draft[start+right] == source[source_pos+right]:
                right += 1
            span = compact_draft[start-left:start+right]
            if len(span) >= window:
                matches.append(
                    {
                        "reference_id": ref.get("id"),
                        "name": ref.get("name", "样文"),
                        "match_chars": len(span),
                        "excerpt": span[:120],
                    }
                )
    matches.sort(key=lambda item: -int(item.get("match_chars", 0)))
    dedup: list[dict[str, Any]] = []
    for item in matches:
        if any(item["excerpt"] in other["excerpt"] or other["excerpt"] in item["excerpt"] for other in dedup):
            continue
        dedup.append(item)
        if len(dedup) >= 8:
            break
    max_chars = max((int(item["match_chars"]) for item in dedup), default=0)
    risk = "high" if max_chars >= 60 else "medium" if max_chars >= 36 else "low"
    return {"risk": risk, "matches": dedup, "max_match_chars": max_chars}


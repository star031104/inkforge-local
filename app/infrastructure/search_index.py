"""Deterministic search-document compilation and snapshot redaction."""
from __future__ import annotations

import re
from typing import Any

from ..core.coercion import safe_non_negative_int as _safe_int
from ..temporal_context import chapter_context

_SEARCH_STOP_LEXEMES = {
    "一个", "一些", "这个", "那个", "他们", "她们", "我们", "你们",
    "自己", "已经", "可以", "没有", "不是", "什么", "怎么", "进行",
    "以及", "因为", "所以", "但是", "然后", "继续", "现在", "本章",
}


def _ordered_search_lexemes(text: str, *, maximum: int = 4000) -> list[str]:
    """Produce stable Chinese bigrams and Latin tokens for the FTS lexeme field."""
    raw = str(text or "").casefold()
    values: list[str] = []
    values.extend(re.findall(r"[a-z0-9_]{2,}", raw))
    for run in re.findall(r"[\u3400-\u9fff]+", raw):
        if len(run) == 1:
            values.append(run)
        else:
            values.extend(run[index : index + 2] for index in range(len(run) - 1))
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in _SEARCH_STOP_LEXEMES or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= maximum:
            break
    return result


def _fts_match_query(text: str) -> str:
    values = _ordered_search_lexemes(text, maximum=64)
    return " OR ".join(f'"{value}"' for value in values)


def _text_chunks(text: str, size: int = 900, overlap: int = 140) -> list[str]:
    clean = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if not clean:
        return []
    if len(clean) <= size:
        return [clean]
    chunks: list[str] = []
    start = 0
    step = max(1, size - overlap)
    while start < len(clean):
        end = min(len(clean), start + size)
        if end < len(clean):
            boundary = max(
                clean.rfind("\n", start + size // 2, end),
                clean.rfind("。", start + size // 2, end),
                clean.rfind("！", start + size // 2, end),
                clean.rfind("？", start + size // 2, end),
            )
            if boundary > start:
                end = boundary + 1
        chunk = clean[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(clean):
            break
        start = max(start + step, end - overlap)
    return chunks


_SNAPSHOT_SECRET_KEYS = {
    "api_key",
    "reasoning_api_key",
    "prose_api_key",
    "brave_api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
    "secret_key",
    "password",
}
_SNAPSHOT_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:sk|rk|pk)-[a-z0-9_-]{16,}\b|\bBearer\s+[a-z0-9._~+/=-]{12,}"
)


def _sanitize_snapshot_value(value: Any, key: str = "") -> Any:
    if key.casefold() in _SNAPSHOT_SECRET_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_snapshot_value(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_snapshot_value(item) for item in value]
    if isinstance(value, str):
        return _SNAPSHOT_SECRET_PATTERN.sub("[REDACTED]", value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def _project_search_documents(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Compile project truth into search documents without modifying the project."""
    project = chapter_context(project, len(project.get("chapters", [])))
    documents: list[dict[str, Any]] = []
    chapter_numbers = {
        str(chapter.get("id", "")): index + 1
        for index, chapter in enumerate(project.get("chapters", []))
        if isinstance(chapter, dict)
    }

    def add(
        *,
        source_id: str,
        kind: str,
        title: str,
        content: str,
        tags: str = "",
        chapter_number: int = 0,
        valid_from: int = 0,
        valid_until: int = 0,
        visibility: str = "objective",
    ) -> None:
        clean_content = str(content or "").strip()
        if not clean_content:
            return
        searchable = " ".join((str(title or ""), clean_content, str(tags or "")))
        lexemes = " ".join(_ordered_search_lexemes(searchable))
        if not lexemes:
            return
        documents.append(
            {
                "source_id": str(source_id),
                "kind": str(kind),
                "title": str(title or ""),
                "content": clean_content,
                "tags": str(tags or ""),
                "lexemes": lexemes,
                "chapter_number": _safe_int(chapter_number),
                "valid_from": _safe_int(valid_from),
                "valid_until": _safe_int(valid_until),
                "visibility": str(visibility or "objective"),
            }
        )

    for index, chapter in enumerate(project.get("chapters", [])):
        if not isinstance(chapter, dict):
            continue
        number = index + 1
        chapter_id = str(chapter.get("id", "") or f"chapter-{number}")
        title = str(chapter.get("title", "") or f"第{number}章")
        add(
            source_id=f"{chapter_id}:summary",
            kind="chapter",
            title=title,
            content=str(chapter.get("summary", "")),
            chapter_number=number,
        )
        for chunk_index, chunk in enumerate(_text_chunks(chapter.get("content", ""))):
            add(
                source_id=f"{chapter_id}:passage:{chunk_index}",
                kind="passage",
                title=f"{title}·正文片段{chunk_index + 1}",
                content=chunk,
                chapter_number=number,
            )

    memory = project.get("memory", {})
    for fact in memory.get("facts", []):
        if not isinstance(fact, dict) or not fact.get("active", True):
            continue
        source_chapter = str(
            fact.get("source_chapter_id") or fact.get("chapter_id") or ""
        )
        add(
            source_id=str(fact.get("id", "")),
            kind="fact",
            title="事实",
            content=str(fact.get("text", "")),
            tags=" ".join(str(item) for item in fact.get("tags", [])),
            chapter_number=chapter_numbers.get(source_chapter, 0),
            valid_from=_safe_int(fact.get("valid_from_chapter")),
            valid_until=_safe_int(fact.get("valid_until_chapter")),
            visibility=str(fact.get("visibility", "objective")),
        )
    for thread in memory.get("plot_threads", []):
        if not isinstance(thread, dict) or str(thread.get("status", "open")) == "closed":
            continue
        content = "；".join(
            str(thread.get(key, ""))
            for key in (
                "setup", "latest", "expected_payoff", "payoff_condition", "payoff"
            )
            if thread.get(key)
        )
        tags = " ".join(
            str(item)
            for key in ("stakeholders", "knowledge_holders")
            for item in thread.get(key, [])
        )
        add(
            source_id=str(thread.get("id", "")),
            kind="thread",
            title=f"未结线索：{thread.get('title', '未命名')}",
            content=content or str(thread.get("title", "")),
            tags=tags,
            chapter_number=_safe_int(thread.get("last_advanced_chapter")),
        )
    for event in memory.get("timeline", []):
        if not isinstance(event, dict):
            continue
        content = (
            f"{event.get('time', '')}｜{event.get('location', '')}："
            f"{event.get('event', '')}"
        )
        tags = " ".join(
            str(item)
            for key in ("participants", "causes", "effects")
            for item in event.get(key, [])
        )
        chapter_number = _safe_int(event.get("chapter_number")) or chapter_numbers.get(
            str(event.get("chapter_id", "")), 0
        )
        add(
            source_id=str(event.get("id", "")),
            kind="timeline",
            title="时间线",
            content=content,
            tags=tags,
            chapter_number=chapter_number,
        )
    for relation in memory.get("relationships", []):
        if not isinstance(relation, dict) or not relation.get("active", True):
            continue
        content = (
            f"{relation.get('left', '')}—{relation.get('right', '')}："
            f"{relation.get('state', '')}；张力：{relation.get('tension', '')}；"
            f"信任：{relation.get('trust', '')}；信息差：{relation.get('knowledge_gap', '')}"
        )
        add(
            source_id=str(relation.get("id", "")),
            kind="relationship",
            title="关系状态",
            content=content,
            chapter_number=_safe_int(relation.get("last_chapter_number")),
        )
    for character in project.get("characters", []):
        if not isinstance(character, dict):
            continue
        name = str(character.get("name", "") or "未命名人物")
        for knowledge in character.get("knowledge_ledger", []):
            if not isinstance(knowledge, dict) or not knowledge.get("active", True):
                continue
            add(
                source_id=str(knowledge.get("id", "")),
                kind="character_knowledge",
                title=f"{name}的已知信息",
                content=str(knowledge.get("text", "")),
                tags=str(knowledge.get("learned_how", "")),
                chapter_number=_safe_int(knowledge.get("chapter_number"))
                or chapter_numbers.get(str(knowledge.get("source_chapter_id", "")), 0),
                visibility="character",
            )
    return documents



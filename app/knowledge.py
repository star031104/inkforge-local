from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .memory import relevance


ENTITY_KINDS = {"character", "world", "location", "faction", "item", "event", "setting"}
FACT_STATUSES = {"confirmed", "candidate", "rejected", "superseded"}
RELATION_STATUSES = {"confirmed", "candidate", "rejected", "superseded"}
RELATION_DIMENSIONS = {
    "trust",
    "intimacy",
    "hostility",
    "loyalty",
    "alliance",
    "rivalry",
    "family",
    "professional",
    "other",
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _texts(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,，\n]", value) if part.strip()]
    if isinstance(value, list):
        return [_clean(item) for item in value if _clean(item)]
    return []


def _stable_id(prefix: str, *values: Any) -> str:
    raw = "\x1f".join(_clean(item).casefold() for item in values).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(raw).hexdigest()[:18]}"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:18]}"


def ensure_knowledge_defaults(project: dict[str, Any]) -> dict[str, Any]:
    knowledge = project.get("knowledge")
    if not isinstance(knowledge, dict):
        knowledge = {}
        project["knowledge"] = knowledge
    knowledge.setdefault("schema_version", 1)
    knowledge.setdefault("entities", [])
    knowledge.setdefault("facts", [])
    knowledge.setdefault("relations", [])
    knowledge.setdefault("review_queue", [])
    for key in ("entities", "facts", "relations", "review_queue"):
        if not isinstance(knowledge.get(key), list):
            knowledge[key] = []

    normalized_entities: list[dict[str, Any]] = []
    for raw in knowledge["entities"]:
        if not isinstance(raw, dict):
            continue
        name = _clean(raw.get("canonical_name") or raw.get("name"))
        if not name:
            continue
        kind = _clean(raw.get("kind")).lower() or "setting"
        if kind not in ENTITY_KINDS:
            kind = "setting"
        item = dict(raw)
        item["canonical_name"] = name
        item["id"] = _clean(item.get("id")) or _stable_id("ent", kind, name)
        item["kind"] = kind
        item["aliases"] = list(dict.fromkeys(_texts(item.get("aliases"))))
        item["source_refs"] = list(dict.fromkeys(_texts(item.get("source_refs"))))
        item["status"] = _clean(item.get("status")) or "confirmed"
        item["user_verified"] = bool(item.get("user_verified", False))
        normalized_entities.append(item)
    knowledge["entities"] = normalized_entities

    normalized_facts: list[dict[str, Any]] = []
    for raw in knowledge["facts"]:
        if not isinstance(raw, dict):
            continue
        subject = _clean(raw.get("subject"))
        predicate = _clean(raw.get("predicate"))
        obj = _clean(raw.get("object"))
        if not (subject and predicate and obj):
            continue
        item = dict(raw)
        item["id"] = _clean(item.get("id")) or _stable_id("fact", subject, predicate, obj)
        item["subject"] = subject
        item["predicate"] = predicate
        item["object"] = obj
        status = _clean(item.get("status")).lower() or "confirmed"
        item["status"] = status if status in FACT_STATUSES else "candidate"
        item["confidence"] = _clean(item.get("confidence")) or (
            "canon" if item["status"] == "confirmed" else "inferred"
        )
        item["evidence"] = _clean(item.get("evidence"))
        item["source_ref"] = _clean(item.get("source_ref"))
        item["importance"] = max(1, min(5, int(item.get("importance", 3) or 3)))
        normalized_facts.append(item)
    knowledge["facts"] = normalized_facts

    normalized_relations: list[dict[str, Any]] = []
    for raw in knowledge["relations"]:
        if not isinstance(raw, dict):
            continue
        source = _clean(raw.get("source"))
        target = _clean(raw.get("target"))
        if not source or not target or source == target:
            continue
        dimension = _clean(raw.get("dimension")).lower() or "other"
        item = dict(raw)
        item["id"] = _clean(item.get("id")) or _stable_id(
            "rel", source, target, dimension
        )
        item["source"] = source
        item["target"] = target
        item["dimension"] = dimension if dimension in RELATION_DIMENSIONS else "other"
        status = _clean(item.get("status")).lower() or "confirmed"
        item["status"] = status if status in RELATION_STATUSES else "candidate"
        item["level"] = max(-10, min(10, int(item.get("level", 0) or 0)))
        item["detail"] = _clean(item.get("detail"))
        item["evidence"] = _clean(item.get("evidence"))
        item["source_ref"] = _clean(item.get("source_ref"))
        normalized_relations.append(item)
    knowledge["relations"] = normalized_relations
    return project


def _entity_index(project: dict[str, Any]) -> dict[str, str]:
    index: dict[str, str] = {}
    for item in project.get("knowledge", {}).get("entities", []):
        if not isinstance(item, dict):
            continue
        canonical = _clean(item.get("canonical_name"))
        if not canonical:
            continue
        index[canonical.casefold()] = canonical
        for alias in _texts(item.get("aliases")):
            index[alias.casefold()] = canonical
    for character in project.get("characters", []):
        if not isinstance(character, dict):
            continue
        name = _clean(character.get("name"))
        if not name:
            continue
        index[name.casefold()] = name
        for alias in _texts(character.get("aliases")):
            index[alias.casefold()] = name
    return index


def canonical_name(project: dict[str, Any], name: str) -> str:
    cleaned = _clean(name)
    if not cleaned:
        return ""
    return _entity_index(project).get(cleaned.casefold(), cleaned)


def sync_authoritative_entities(project: dict[str, Any]) -> dict[str, Any]:
    """Project character/world files are authoritative; graph entries are projections.

    This mirrors Storydex's useful separation without copying its implementation: the
    graph never becomes the only source of truth and can be rebuilt from project cards.
    """
    ensure_knowledge_defaults(project)
    knowledge = project["knowledge"]
    existing = {item["id"]: item for item in knowledge["entities"] if isinstance(item, dict)}
    projected: list[dict[str, Any]] = []

    for character in project.get("characters", []):
        if not isinstance(character, dict):
            continue
        name = _clean(character.get("name"))
        if not name:
            continue
        entity_id = _stable_id("ent", "character", name)
        previous = existing.get(entity_id, {})
        projected.append(
            {
                **previous,
                "id": entity_id,
                "canonical_name": name,
                "aliases": list(dict.fromkeys(_texts(character.get("aliases")))),
                "kind": "character",
                "status": "confirmed",
                "user_verified": bool(
                    character.get("canon_profile", {}).get("user_verified", False)
                    if isinstance(character.get("canon_profile"), dict)
                    else False
                ),
                "source_refs": [f"character:{character.get('id', '')}"],
            }
        )

    for entry in project.get("world_entries", []):
        if not isinstance(entry, dict):
            continue
        title = _clean(entry.get("title"))
        if not title:
            continue
        category = _clean(entry.get("category")).lower()
        kind = "setting"
        if "地点" in category or "location" in category:
            kind = "location"
        elif "组织" in category or "势力" in category or "faction" in category:
            kind = "faction"
        elif "物" in category or "item" in category:
            kind = "item"
        elif "事件" in category or "event" in category:
            kind = "event"
        entity_id = _stable_id("ent", kind, title)
        previous = existing.get(entity_id, {})
        projected.append(
            {
                **previous,
                "id": entity_id,
                "canonical_name": title,
                "aliases": list(dict.fromkeys(_texts(entry.get("keys")))),
                "kind": kind,
                "status": "confirmed" if entry.get("canon") == "hard" else "candidate",
                "user_verified": bool(entry.get("canon") == "hard"),
                "source_refs": [f"world:{entry.get('id', '')}"],
            }
        )

    # Preserve manual entities which are not projections of current cards.
    projected_ids = {item["id"] for item in projected}
    projected.extend(
        item
        for item in knowledge["entities"]
        if isinstance(item, dict)
        and item.get("id") not in projected_ids
        and not any(
            str(ref).startswith(("character:", "world:"))
            for ref in item.get("source_refs", [])
        )
    )
    knowledge["entities"] = projected
    return project


def infer_active_entities(
    project: dict[str, Any], query: str, *, fallback_limit: int = 0
) -> list[str]:
    query_fold = _clean(query).casefold()
    if not query_fold:
        return []
    names: list[tuple[int, int, str]] = []
    aliases: dict[str, set[str]] = {}
    for canonical_key, canonical in _entity_index(project).items():
        aliases.setdefault(canonical, set()).add(canonical_key)
    for canonical, keys in aliases.items():
        hits = sum(query_fold.count(key) for key in keys if key)
        if hits:
            names.append((hits, len(canonical), canonical))
    names.sort(key=lambda item: (-item[0], -item[1], item[2]))
    result = [item[2] for item in names]
    if result or fallback_limit <= 0:
        return result
    candidates = [
        _clean(item.get("canonical_name"))
        for item in project.get("knowledge", {}).get("entities", [])
        if isinstance(item, dict) and _clean(item.get("canonical_name"))
    ]
    return candidates[:fallback_limit]


@dataclass(frozen=True)
class KnowledgeHit:
    kind: str
    title: str
    content: str
    score: float
    source_ref: str = ""


def relevant_facts(
    project: dict[str, Any], active_entities: Sequence[str], query: str, limit: int = 8
) -> list[dict[str, Any]]:
    active = {canonical_name(project, item) for item in active_entities if _clean(item)}
    scored: list[tuple[float, dict[str, Any]]] = []
    for fact in project.get("knowledge", {}).get("facts", []):
        if not isinstance(fact, dict) or fact.get("status") != "confirmed":
            continue
        subject = canonical_name(project, _clean(fact.get("subject")))
        obj = canonical_name(project, _clean(fact.get("object")))
        touches = subject in active or obj in active
        content = f"{subject} {_clean(fact.get('predicate'))} {obj}"
        lexical = relevance(query, content)
        if active and not touches and lexical <= 0:
            continue
        score = (8 if touches else 0) + lexical + int(fact.get("importance", 3) or 3)
        if fact.get("evidence"):
            score += 1.5
        if _clean(fact.get("confidence")).lower() in {"canon", "confirmed"}:
            score += 2
        scored.append((score, fact))
    scored.sort(key=lambda item: -item[0])
    return [item[1] for item in scored[: max(0, limit)]]


def relevant_relations(
    project: dict[str, Any], active_entities: Sequence[str], query: str, limit: int = 8
) -> list[dict[str, Any]]:
    active = {canonical_name(project, item) for item in active_entities if _clean(item)}
    scored: list[tuple[float, dict[str, Any]]] = []
    for relation in project.get("knowledge", {}).get("relations", []):
        if not isinstance(relation, dict) or relation.get("status") != "confirmed":
            continue
        source = canonical_name(project, _clean(relation.get("source")))
        target = canonical_name(project, _clean(relation.get("target")))
        touches = source in active or target in active
        content = f"{source} {relation.get('dimension', '')} {target} {relation.get('detail', '')}"
        lexical = relevance(query, content)
        if active and not touches and lexical <= 0:
            continue
        score = (9 if touches else 0) + lexical + abs(int(relation.get("level", 0) or 0)) * 0.35
        if relation.get("evidence"):
            score += 1.5
        scored.append((score, relation))
    # Legacy relationship state is also an authority source.
    for relation in project.get("memory", {}).get("relationships", []):
        if not isinstance(relation, dict) or relation.get("active", True) is False:
            continue
        source = canonical_name(project, _clean(relation.get("left")))
        target = canonical_name(project, _clean(relation.get("right")))
        if not source or not target:
            continue
        touches = source in active or target in active
        detail = "；".join(
            _clean(relation.get(key))
            for key in ("state", "tension", "trust", "knowledge_gap")
            if _clean(relation.get(key))
        )
        content = f"{source} {target} {detail}"
        lexical = relevance(query, content)
        if active and not touches and lexical <= 0:
            continue
        scored.append(
            (
                (8 if touches else 0) + lexical + 1,
                {
                    "id": _stable_id("legacyrel", source, target, detail),
                    "source": source,
                    "target": target,
                    "dimension": "other",
                    "level": 0,
                    "detail": detail,
                    "evidence": _clean(relation.get("evidence")),
                    "source_ref": f"chapter:{relation.get('source_chapter_id', '')}",
                    "status": "confirmed",
                },
            )
        )
    scored.sort(key=lambda item: -item[0])
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for _, relation in scored:
        key = (
            _clean(relation.get("source")),
            _clean(relation.get("target")),
            _clean(relation.get("dimension")),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(relation)
        if len(result) >= max(0, limit):
            break
    return result


def render_knowledge_context(
    project: dict[str, Any], query: str, active_entities: Sequence[str] = (), *, max_chars: int = 3500
) -> tuple[str, list[str]]:
    ensure_knowledge_defaults(project)
    sync_authoritative_entities(project)
    active = [canonical_name(project, item) for item in active_entities if _clean(item)]
    if not active:
        active = infer_active_entities(project, query)
    facts = relevant_facts(project, active, query, limit=8)
    relations = relevant_relations(project, active, query, limit=8)
    if not facts and not relations:
        return "", active
    lines = [
        "【结构化知识约束】",
        "只把下列已确认事实/关系当作本轮硬约束；候选推断不自动升级为事实。",
        "没有列出的内容仍可能是背景真相，但不要为填空而擅自发明。",
    ]
    if active:
        lines.append("当前活跃实体：" + "、".join(active))
    if facts:
        lines.append("\n已确认事实：")
        for fact in facts:
            line = f"- {fact.get('subject')}｜{fact.get('predicate')}｜{fact.get('object')}"
            if fact.get("source_ref"):
                line += f"｜来源={fact.get('source_ref')}"
            if fact.get("evidence"):
                line += f"｜证据={_clean(fact.get('evidence'))[:100]}"
            lines.append(line)
    if relations:
        lines.append("\n相关关系邻域：")
        for relation in relations:
            line = (
                f"- {relation.get('source')} → {relation.get('target')}｜"
                f"{relation.get('dimension', 'other')}｜level={relation.get('level', 0)}"
            )
            if relation.get("detail"):
                line += f"｜{_clean(relation.get('detail'))[:120]}"
            if relation.get("evidence"):
                line += f"｜证据={_clean(relation.get('evidence'))[:100]}"
            lines.append(line)
    text = "\n".join(lines)
    return text[:max_chars], active


def graph_snapshot(project: dict[str, Any]) -> dict[str, Any]:
    ensure_knowledge_defaults(project)
    sync_authoritative_entities(project)
    entities = {
        item["canonical_name"]: item
        for item in project["knowledge"]["entities"]
        if isinstance(item, dict) and item.get("canonical_name")
    }
    # Include endpoints that only exist in legacy relation memory.
    edges = relevant_relations(project, list(entities), "", limit=500)
    for edge in edges:
        for endpoint in (_clean(edge.get("source")), _clean(edge.get("target"))):
            if endpoint and endpoint not in entities:
                entities[endpoint] = {
                    "id": _stable_id("ent", "character", endpoint),
                    "canonical_name": endpoint,
                    "kind": "character",
                    "aliases": [],
                    "status": "confirmed",
                    "source_refs": [],
                }
    return {
        "nodes": [
            {
                "id": item.get("id"),
                "name": name,
                "kind": item.get("kind", "setting"),
                "status": item.get("status", "candidate"),
                "user_verified": bool(item.get("user_verified", False)),
                "source_refs": item.get("source_refs", []),
            }
            for name, item in entities.items()
        ],
        "edges": [
            {
                "id": edge.get("id") or _new_id("rel"),
                "source": _clean(edge.get("source")),
                "target": _clean(edge.get("target")),
                "dimension": edge.get("dimension", "other"),
                "level": int(edge.get("level", 0) or 0),
                "detail": _clean(edge.get("detail")),
                "evidence": _clean(edge.get("evidence")),
                "source_ref": _clean(edge.get("source_ref")),
                "status": edge.get("status", "confirmed"),
            }
            for edge in edges
        ],
        "facts": project["knowledge"]["facts"],
        "review_queue": project["knowledge"]["review_queue"],
    }


def upsert_fact(project: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    ensure_knowledge_defaults(project)
    subject = canonical_name(project, _clean(payload.get("subject")))
    predicate = _clean(payload.get("predicate"))
    obj = canonical_name(project, _clean(payload.get("object")))
    if not (subject and predicate and obj):
        raise ValueError("subject、predicate、object 不能为空")
    fact_id = _clean(payload.get("id")) or _stable_id("fact", subject, predicate, obj)
    item = {
        "id": fact_id,
        "subject": subject,
        "predicate": predicate,
        "object": obj,
        "status": _clean(payload.get("status")) or "confirmed",
        "confidence": _clean(payload.get("confidence")) or "confirmed",
        "evidence": _clean(payload.get("evidence")),
        "source_ref": _clean(payload.get("source_ref")),
        "importance": max(1, min(5, int(payload.get("importance", 3) or 3))),
        "visibility": _clean(payload.get("visibility")) or "objective",
        "source_memory_id": _clean(payload.get("source_memory_id")),
        "valid_from_chapter": max(
            0, int(payload.get("valid_from_chapter", 0) or 0)
        ),
        "valid_until_chapter": max(
            0, int(payload.get("valid_until_chapter", 0) or 0)
        ),
    }
    facts = project["knowledge"]["facts"]
    for index, existing in enumerate(facts):
        if isinstance(existing, dict) and existing.get("id") == fact_id:
            facts[index] = item
            break
    else:
        facts.append(item)
    return item


def upsert_relation(project: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    ensure_knowledge_defaults(project)
    source = canonical_name(project, _clean(payload.get("source")))
    target = canonical_name(project, _clean(payload.get("target")))
    if not source or not target or source == target:
        raise ValueError("source、target 必须是两个不同实体")
    dimension = _clean(payload.get("dimension")).lower() or "other"
    if dimension not in RELATION_DIMENSIONS:
        dimension = "other"
    relation_id = _clean(payload.get("id")) or _stable_id("rel", source, target, dimension)
    item = {
        "id": relation_id,
        "source": source,
        "target": target,
        "dimension": dimension,
        "status": _clean(payload.get("status")) or "confirmed",
        "level": max(-10, min(10, int(payload.get("level", 0) or 0))),
        "detail": _clean(payload.get("detail")),
        "evidence": _clean(payload.get("evidence")),
        "source_ref": _clean(payload.get("source_ref")),
    }
    relations = project["knowledge"]["relations"]
    for index, existing in enumerate(relations):
        if isinstance(existing, dict) and existing.get("id") == relation_id:
            relations[index] = item
            break
    else:
        relations.append(item)
    return item


def upsert_current_fact(
    project: dict[str, Any], payload: dict[str, Any]
) -> dict[str, Any]:
    """Insert a current-state fact and supersede older facts for the same slot.

    Long-form fiction contains mutable facts (location, condition, ownership, etc.).
    Keeping every historical value as simultaneously confirmed truth is one of the
    easiest ways to make retrieval contradict the current chapter.
    """
    ensure_knowledge_defaults(project)
    subject = canonical_name(project, _clean(payload.get("subject")))
    predicate = _clean(payload.get("predicate"))
    obj = canonical_name(project, _clean(payload.get("object")))
    if not (subject and predicate and obj):
        raise ValueError("subject、predicate、object 不能为空")
    for existing in project["knowledge"]["facts"]:
        if not isinstance(existing, dict):
            continue
        if (
            canonical_name(project, _clean(existing.get("subject"))) == subject
            and _clean(existing.get("predicate")) == predicate
            and existing.get("status") == "confirmed"
            and canonical_name(project, _clean(existing.get("object"))) != obj
        ):
            existing["status"] = "superseded"
            existing["superseded_by"] = _stable_id("fact", subject, predicate, obj)
    return upsert_fact(project, {**payload, "subject": subject, "predicate": predicate, "object": obj})


def project_accepted_memory_to_knowledge(
    project: dict[str, Any], chapter: dict[str, Any], result: dict[str, Any]
) -> list[str]:
    """Project evidence-backed accepted chapter state into the knowledge layer.

    The caller must run this only after the chapter is accepted and the legacy memory
    updater has verified evidence.  We intentionally do not parse arbitrary prose into
    graph edges: co-occurrence is not a relationship, and model guesses are not canon.
    """
    ensure_knowledge_defaults(project)
    sync_authoritative_entities(project)
    projected: list[str] = []
    chapter_id = _clean(chapter.get("id"))
    source_ref = f"chapter:{chapter_id}" if chapter_id else "accepted-chapter"
    accepted_content_key = re.sub(
        r"[\s，。！？、；：,.!?;:'\"“”‘’—…（）()]",
        "",
        str(chapter.get("content", "")),
    ).casefold()

    def verified_quote(value: Any) -> str:
        quote = _clean(value)[:240]
        quote_key = re.sub(
            r"[\s，。！？、；：,.!?;:'\"“”‘’—…（）()]",
            "",
            quote,
        ).casefold()
        return quote if quote_key and quote_key in accepted_content_key else ""

    characters = {
        _clean(item.get("name")).casefold(): _clean(item.get("name"))
        for item in project.get("characters", [])
        if isinstance(item, dict) and _clean(item.get("name"))
    }
    for update in result.get("character_updates", []):
        if not isinstance(update, dict):
            continue
        name = characters.get(_clean(update.get("name")).casefold(), "")
        evidence = verified_quote(update.get("evidence"))
        if not name or not evidence:
            continue
        for field, predicate in (("location", "当前地点"), ("state", "当前状态")):
            value = _clean(update.get(field))
            if not value:
                continue
            fact = upsert_current_fact(
                project,
                {
                    "subject": name,
                    "predicate": predicate,
                    "object": value,
                    "status": "confirmed",
                    "confidence": "confirmed",
                    "evidence": evidence,
                    "source_ref": source_ref,
                    "importance": 5 if field == "location" else 4,
                },
            )
            projected.append(str(fact.get("id", "")))

    memory_facts = [
        item
        for item in project.get("memory", {}).get("facts", [])
        if isinstance(item, dict)
        and item.get("active", True)
        and _clean(item.get("source_chapter_id")) == chapter_id
    ]
    memory_fact_by_text = {
        _clean(item.get("text")).casefold(): item
        for item in memory_facts
        if _clean(item.get("text"))
    }
    for update in result.get("facts", []):
        if not isinstance(update, dict):
            continue
        text = _clean(update.get("text"))
        if not text:
            continue
        memory_fact = memory_fact_by_text.get(text.casefold(), {})
        if not memory_fact or not memory_fact.get("evidence_verified"):
            continue
        evidence = verified_quote(memory_fact.get("evidence"))
        if not evidence:
            continue
        tags = _texts(update.get("tags"))
        subject = next(
            (
                characters[tag.casefold()]
                for tag in tags
                if tag.casefold() in characters
            ),
            tags[0] if tags else "故事事实",
        )
        fact = upsert_fact(
            project,
            {
                "subject": subject,
                "predicate": "章节事实",
                "object": text,
                "status": "confirmed",
                "confidence": _clean(update.get("confidence")) or "confirmed",
                "visibility": _clean(update.get("visibility")) or "objective",
                "evidence": evidence,
                "source_ref": source_ref,
                "source_memory_id": _clean(memory_fact.get("id")),
                "valid_from_chapter": int(
                    memory_fact.get("valid_from_chapter", 0) or 0
                ),
                "valid_until_chapter": int(
                    memory_fact.get("valid_until_chapter", 0) or 0
                ),
                "importance": int(update.get("importance", 3) or 3),
            },
        )
        projected.append(str(fact.get("id", "")))

    verified_relationships: dict[tuple[str, str], dict[str, Any]] = {}
    for item in project.get("memory", {}).get("relationships", []):
        if not isinstance(item, dict) or not item.get("evidence_verified"):
            continue
        if _clean(item.get("source_chapter_id")) != chapter_id:
            continue
        pair = tuple(
            sorted(
                (
                    _clean(item.get("left")).casefold(),
                    _clean(item.get("right")).casefold(),
                )
            )
        )
        verified_relationships[pair] = item

    for update in result.get("relationship_updates", []):
        if not isinstance(update, dict):
            continue
        left = characters.get(
            _clean(update.get("left") or update.get("from")).casefold(), ""
        )
        right = characters.get(
            _clean(update.get("right") or update.get("to")).casefold(), ""
        )
        pair = tuple(sorted((left.casefold(), right.casefold())))
        memory_relation = verified_relationships.get(pair, {})
        evidence = verified_quote(memory_relation.get("evidence"))
        if not left or not right or left == right or not memory_relation or not evidence:
            continue
        detail_parts = []
        for field, label in (("state", "状态"), ("tension", "张力"), ("trust", "信任"), ("knowledge_gap", "信息差")):
            value = _clean(
                update.get(field)
                or (update.get("change") if field == "state" else "")
            )
            if value:
                detail_parts.append(f"{label}:{value}")
        relation = upsert_relation(
            project,
            {
                "source": left,
                "target": right,
                "dimension": "other",
                "status": "confirmed",
                "level": 0,
                "detail": "；".join(detail_parts),
                "evidence": evidence,
                "source_ref": source_ref,
            },
        )
        projected.append(str(relation.get("id", "")))

    return [item for item in projected if item]

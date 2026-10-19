"""Read-only projections for the relationship graph and story timeline.

Everything here is derived from authoritative project state.  "As of chapter
N" uses the same temporal boundary as prompt construction
(``chapter_context``), so the graph never shows a relationship before the
chapter that established it.
"""

from __future__ import annotations

import re
from typing import Any

from ..temporal_context import chapter_context

POSITIVE_HINTS = ("信任", "亲密", "盟", "友", "爱", "恋", "师", "依赖", "感激", "合作", "亲")
NEGATIVE_HINTS = ("敌", "仇", "恨", "猜忌", "怀疑", "对立", "竞争", "威胁", "背叛", "提防", "冷淡")


def _key(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _polarity(*texts: Any) -> int:
    joined = " ".join(str(item or "") for item in texts)
    score = sum(1 for word in POSITIVE_HINTS if word in joined) - sum(
        1 for word in NEGATIVE_HINTS if word in joined
    )
    return max(-1, min(1, score))


def _chapter_numbers(project: dict[str, Any]) -> dict[str, int]:
    return {str(item.get("id", "")): index + 1 for index, item in enumerate(project.get("chapters", []))}


def _source_number(item: dict[str, Any], numbers: dict[str, int]) -> int:
    refs = [item.get(key) for key in ("source_chapter_id", "chapter_id", "last_chapter_id")]
    ref = str(item.get("source_ref", ""))
    if ref.startswith("chapter:"):
        refs.append(ref[8:])
    values = [numbers.get(str(value), 0) for value in refs if value]
    values += [
        int(item.get(key, 0) or 0)
        for key in ("chapter_number", "last_chapter_number", "created_chapter_number")
    ]
    return max(values) if values else 0


def build_story_graph(project: dict[str, Any], as_of: int | None = None) -> dict[str, Any]:
    chapters = project.get("chapters", [])
    total = len(chapters)
    as_of = total if as_of is None or as_of <= 0 or as_of > total else int(as_of)
    view = chapter_context(project, as_of) if as_of else project
    numbers = _chapter_numbers(project)
    readable = "\n".join(str(item.get("content", "")) for item in chapters[:as_of])

    nodes: list[dict[str, Any]] = []
    index_by_name: dict[str, str] = {}
    for character in project.get("characters", []):
        if not isinstance(character, dict) or not str(character.get("name", "")).strip():
            continue
        names = [character["name"], *[a for a in character.get("aliases", []) if str(a).strip()]]
        mentions = sum(readable.count(name) for name in names if len(str(name)) >= 2)
        node = {
            "id": str(character.get("id", "")) or f"char:{_key(character['name'])}",
            "name": character["name"],
            "role": character.get("role", ""),
            "importance": character.get("importance", "supporting"),
            "active": bool(character.get("active", True)),
            "mentions": mentions,
            "registered": True,
        }
        nodes.append(node)
        for name in names:
            index_by_name[_key(name)] = node["id"]

    def node_for(name: Any) -> str:
        key = _key(name)
        if not key:
            return ""
        if key in index_by_name:
            return index_by_name[key]
        node_id = f"name:{key}"
        nodes.append({
            "id": node_id,
            "name": str(name).strip()[:40],
            "role": "",
            "importance": "minor",
            "active": True,
            "mentions": readable.count(str(name).strip()) if len(str(name).strip()) >= 2 else 0,
            "registered": False,
        })
        index_by_name[key] = node_id
        return node_id

    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for item in view.get("memory", {}).get("relationships", []):
        if not isinstance(item, dict):
            continue
        left, right = node_for(item.get("left")), node_for(item.get("right"))
        if not left or not right or left == right:
            continue
        chapter_number = _source_number(item, numbers)
        pair = tuple(sorted((left, right)))
        current = edges.get(pair)
        if current and current["chapter"] > chapter_number:
            current["weight"] += 1
            continue
        edges[pair] = {
            "source": pair[0],
            "target": pair[1],
            "kind": "relationship",
            "label": str(item.get("state", ""))[:60],
            "tension": str(item.get("tension", ""))[:120],
            "trust": str(item.get("trust", ""))[:120],
            "knowledge_gap": str(item.get("knowledge_gap", ""))[:160],
            "polarity": _polarity(item.get("state"), item.get("trust"), item.get("tension")),
            "active": bool(item.get("active", True)),
            "chapter": chapter_number,
            "evidence": str(item.get("evidence", ""))[:240],
            "evidence_verified": bool(item.get("evidence_verified")),
            "weight": (current["weight"] + 1) if current else 1,
        }
    entity_names = {
        str(entity.get("id", "")): entity.get("name") or entity.get("title") or entity.get("id")
        for entity in project.get("knowledge", {}).get("entities", [])
        if isinstance(entity, dict)
    }
    for relation in project.get("knowledge", {}).get("relations", []):
        if not isinstance(relation, dict) or relation.get("status") != "confirmed":
            continue
        chapter_number = _source_number(relation, numbers)
        if chapter_number and chapter_number > as_of:
            continue
        left_name = entity_names.get(str(relation.get("source")), relation.get("source"))
        right_name = entity_names.get(str(relation.get("target")), relation.get("target"))
        if _key(left_name) not in index_by_name or _key(right_name) not in index_by_name:
            continue
        left, right = index_by_name[_key(left_name)], index_by_name[_key(right_name)]
        if left == right:
            continue
        pair = tuple(sorted((left, right)))
        level = int(relation.get("level", 0) or 0)
        if pair in edges:
            edges[pair]["weight"] += 1
            edges[pair].setdefault("dimensions", []).append(relation.get("dimension", "other"))
            continue
        edges[pair] = {
            "source": pair[0],
            "target": pair[1],
            "kind": "knowledge",
            "label": str(relation.get("detail") or relation.get("dimension") or "")[:60],
            "tension": "",
            "trust": "",
            "knowledge_gap": "",
            "polarity": 1 if level > 0 else -1 if level < 0 else 0,
            "active": True,
            "chapter": chapter_number,
            "evidence": str(relation.get("evidence", ""))[:240],
            "evidence_verified": bool(relation.get("evidence")),
            "weight": 1 + abs(level) // 4,
            "dimensions": [relation.get("dimension", "other")],
        }

    # Co-appearance in timeline events adds weight but never invents an edge label.
    timeline_events = []
    for item in view.get("memory", {}).get("timeline", []):
        if not isinstance(item, dict) or not str(item.get("event", "")).strip():
            continue
        chapter_number = _source_number(item, numbers)
        participants = [p for p in item.get("participants", []) if str(p).strip()]
        ids = sorted({node_for(p) for p in participants} - {""})
        for i, left in enumerate(ids):
            for right in ids[i + 1 :]:
                pair = (left, right)
                if pair in edges:
                    edges[pair]["weight"] += 1
                else:
                    edges[pair] = {
                        "source": left, "target": right, "kind": "co_appearance",
                        "label": "同场", "tension": "", "trust": "", "knowledge_gap": "",
                        "polarity": 0, "active": True, "chapter": chapter_number,
                        "evidence": "", "evidence_verified": False, "weight": 1,
                    }
        timeline_events.append({
            "id": str(item.get("id", "")),
            "chapter": chapter_number,
            "time": str(item.get("time", ""))[:40],
            "event": str(item.get("event", ""))[:200],
            "location": str(item.get("location", ""))[:60],
            "participants": participants[:8],
            "evidence_verified": bool(item.get("evidence_verified")),
        })
    timeline_events.sort(key=lambda item: (item["chapter"], item["time"]))

    threads = []
    for item in project.get("memory", {}).get("plot_threads", []):
        if not isinstance(item, dict) or not str(item.get("title", "")).strip():
            continue
        planted = int(item.get("created_chapter_number", 0) or 0) or numbers.get(
            str(item.get("chapter_id", "")), 0
        )
        if planted and planted > as_of:
            continue
        closed = int(item.get("closed_chapter_number", 0) or 0)
        status = str(item.get("status", "open"))
        open_as_of = not (status == "closed" and closed and closed <= as_of)
        threads.append({
            "id": str(item.get("id", "")),
            "title": str(item.get("title", ""))[:80],
            "type": str(item.get("type", "")),
            "planted": planted,
            "advanced": int(item.get("last_advanced_chapter", 0) or 0),
            "payoff": closed if closed and closed <= as_of else 0,
            # A thread closed after ``as_of`` was still open at that point.
            "status": "open" if status == "closed" and open_as_of else status,
            "open": open_as_of,
            "expected_payoff": str(item.get("expected_payoff", ""))[:200],
            "target_window": str(item.get("target_window", "")),
            "overdue": bool(open_as_of and planted and as_of - planted >= 15),
        })
    threads.sort(key=lambda item: (item["planted"], item["title"]))
    used = {edge["source"] for edge in edges.values()} | {edge["target"] for edge in edges.values()}
    visible_nodes = [
        node for node in nodes if node["registered"] or node["id"] in used
    ]
    return {
        "as_of": as_of,
        "chapter_count": total,
        "chapters": [
            {"number": index + 1, "title": str(item.get("title", ""))[:40]}
            for index, item in enumerate(chapters)
        ],
        "nodes": visible_nodes,
        "edges": sorted(edges.values(), key=lambda edge: (-edge["weight"], edge["source"])),
        "timeline": timeline_events,
        "threads": threads,
        "stats": {
            "open_threads": sum(1 for item in threads if item["open"]),
            "overdue_threads": sum(1 for item in threads if item["overdue"]),
            "relationships": sum(1 for edge in edges.values() if edge["kind"] != "co_appearance"),
        },
    }

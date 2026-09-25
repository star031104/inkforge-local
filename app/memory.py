from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any


@dataclass
class MemoryHit:
    kind: str
    title: str
    content: str
    score: float
    source_id: str = ""
    origin: str = "lexical"


THREAD_STATUSES = {"open", "progressing", "deferred", "ready", "closed"}
THREAD_TIMINGS = {"immediate", "near", "mid", "slow", "endgame"}


def normalize_thread_status(value: Any) -> str:
    """Normalize legacy/model thread labels without silently closing a hook."""
    status = str(value or "open").strip().casefold()
    aliases = {
        "opened": "open",
        "active": "open",
        "reopened": "progressing",
        "未结": "open",
        "待推进": "open",
        "advanced": "progressing",
        "推进": "progressing",
        "持续推进": "progressing",
        "paused": "deferred",
        "hold": "deferred",
        "延后": "deferred",
        "搁置": "deferred",
        "payoff_ready": "ready",
        "ready_for_payoff": "ready",
        "可回收": "ready",
        "resolved_with_cost": "progressing",
        "resolved_with_boundary": "closed",
        "resolved_as_process": "closed",
        "resolved": "closed",
        "close": "closed",
        "已回收": "closed",
        "已解决": "closed",
    }
    normalized = aliases.get(status, status)
    return normalized if normalized in THREAD_STATUSES else "open"


def normalize_thread_timing(value: Any) -> str:
    timing = str(value or "mid").strip().casefold().replace("-", "_")
    aliases = {
        "immediate": "immediate",
        "立即": "immediate",
        "near_term": "near",
        "近期": "near",
        "短线": "near",
        "mid_arc": "mid",
        "中程": "mid",
        "中期": "mid",
        "slow_burn": "slow",
        "慢烧": "slow",
        "长线": "slow",
        "终局": "endgame",
        "结局": "endgame",
    }
    normalized = aliases.get(timing, timing)
    return normalized if normalized in THREAD_TIMINGS else "mid"


def _chapter_number_map(project: dict[str, Any]) -> dict[str, int]:
    return {
        str(chapter.get("id", "")): index + 1
        for index, chapter in enumerate(project.get("chapters", []))
        if isinstance(chapter, dict) and chapter.get("id")
    }


def thread_lifecycle(
    project: dict[str, Any], thread: dict[str, Any], current_chapter_index: int
) -> dict[str, Any]:
    """Compute deterministic hook pressure for planning and prompt compilation."""
    chapter_numbers = _chapter_number_map(project)
    current_number = current_chapter_index + 1
    target = max(
        current_number,
        int(project.get("narrative", {}).get("target_chapters", current_number) or current_number),
    )
    created = int(thread.get("created_chapter_number", 0) or 0)
    if created <= 0:
        created = chapter_numbers.get(
            str(thread.get("chapter_id") or thread.get("source_chapter_id") or ""), 1
        )
    last_advanced = int(thread.get("last_advanced_chapter", 0) or 0)
    if last_advanced <= 0:
        last_advanced = chapter_numbers.get(
            str(thread.get("last_chapter_id") or thread.get("chapter_id") or ""),
            created,
        )
    status = normalize_thread_status(thread.get("status"))
    timing = normalize_thread_timing(thread.get("target_window"))
    age = max(0, current_number - max(1, created))
    dormancy = max(0, current_number - max(1, last_advanced))
    progress = current_number / max(1, target)
    stale_after = {
        "immediate": 2,
        "near": 4,
        "mid": 7,
        "slow": 10,
        "endgame": 12,
    }[timing]
    eligible = {
        "immediate": age >= 1,
        "near": age >= 2,
        "mid": age >= 4 and progress >= 0.3,
        "slow": age >= 8 and progress >= 0.55,
        "endgame": progress >= 0.78,
    }[timing]
    stale = status not in {"closed", "deferred"} and dormancy >= stale_after
    if status == "ready":
        action = "eligible_resolve"
    elif stale:
        action = "must_advance"
    elif status == "progressing" and eligible:
        action = "eligible_resolve"
    else:
        action = "hold_or_advance"
    pressure = (
        dormancy * 2
        + age * 0.35
        + (12 if stale else 0)
        + (8 if status == "ready" else 0)
        + (3 if status == "progressing" else 0)
    )
    return {
        "status": status,
        "timing": timing,
        "created_chapter": created,
        "last_advanced_chapter": last_advanced,
        "age": age,
        "dormancy": dormancy,
        "eligible_to_resolve": eligible or status == "ready",
        "stale": stale,
        "action": action,
        "pressure": pressure,
    }


def select_thread_agenda(
    project: dict[str, Any],
    query: str,
    current_chapter_index: int,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Select relevant hooks plus overdue debt without injecting every open hook."""
    ranked: list[tuple[float, dict[str, Any]]] = []
    for raw in project.get("memory", {}).get("plot_threads", []):
        if not isinstance(raw, dict):
            continue
        lifecycle = thread_lifecycle(project, raw, current_chapter_index)
        if lifecycle["status"] == "closed":
            continue
        text = " ".join(
            str(raw.get(key, ""))
            for key in (
                "title",
                "type",
                "setup",
                "latest",
                "expected_payoff",
                "payoff_condition",
                "payoff",
            )
        ) + " " + " ".join(str(item) for item in raw.get("stakeholders", []))
        related = relevance(query, text)
        if lifecycle["status"] == "deferred" and related <= 0:
            continue
        if related <= 0 and not lifecycle["stale"] and lifecycle["dormancy"] > 4:
            continue
        ranked.append(
            (
                float(lifecycle["pressure"]) + related * 3,
                {**raw, "_lifecycle": lifecycle, "_relevance": related},
            )
        )
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [item for _, item in ranked[: max(1, limit)]]


def render_thread_agenda(threads: list[dict[str, Any]]) -> str:
    if not threads:
        return ""
    timing_labels = {
        "immediate": "立即",
        "near": "近期",
        "mid": "中程",
        "slow": "慢烧",
        "endgame": "终局",
    }
    action_labels = {
        "must_advance": "本章必须推进、合理延后或给出不推进的可见原因",
        "eligible_resolve": "已具备回收条件，但只能用正文因果完成回收",
        "hold_or_advance": "可保持或小幅推进，不得凭空回收",
    }
    lines: list[str] = []
    for thread in threads:
        lifecycle = thread.get("_lifecycle", {})
        stakeholders = "、".join(str(item) for item in thread.get("stakeholders", []))
        holders = "、".join(str(item) for item in thread.get("knowledge_holders", []))
        lines.append(
            f"- [{thread.get('id', '无ID')}｜{lifecycle.get('status', 'open')}｜"
            f"{timing_labels.get(str(lifecycle.get('timing', 'mid')), '中程')}｜"
            f"静默{lifecycle.get('dormancy', 0)}章] {thread.get('title', '未命名线索')}"
        )
        detail = [
            f"最近推进：{thread.get('latest', thread.get('setup', ''))}",
            f"预期回收：{thread.get('expected_payoff', thread.get('payoff', ''))}",
            f"回收条件：{thread.get('payoff_condition', '')}",
            f"本章治理：{action_labels.get(str(lifecycle.get('action')), '谨慎处理')}",
        ]
        if stakeholders:
            detail.append(f"相关人物：{stakeholders}")
        if holders:
            detail.append(f"当前知情者：{holders}")
        lines.append("  " + "；".join(item for item in detail if item.split("：", 1)[-1]))
    return "\n".join(lines)


def terms(text: str) -> set[str]:
    """Dependency-free mixed Chinese/Latin lexical features."""
    text = text.casefold()
    latin = set(re.findall(r"[a-z0-9_]{2,}", text))
    chinese_runs = re.findall(r"[\u3400-\u9fff]+", text)
    chinese: set[str] = set()
    for run in chinese_runs:
        chinese.update(run)
        chinese.update(run[i : i + 2] for i in range(len(run) - 1))
    return latin | chinese


def relevance(query: str, text: str) -> float:
    q = terms(query)
    if not q:
        return 0.0
    t = terms(text)
    overlap = q & t
    if not overlap:
        return 0.0
    rare_bonus = sum(1.6 if len(term) > 1 else 0.55 for term in overlap)
    coverage = len(overlap) / max(1, len(q))
    return rare_bonus + coverage * 3


def lexical_similarity(left: str, right: str) -> float:
    """Jaccard similarity used to keep near-duplicate memories from crowding context."""
    left_terms, right_terms = terms(left), terms(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / max(1, len(left_terms | right_terms))


def retrieve_memories(
    project: dict[str, Any],
    query: str,
    current_chapter_index: int,
    limit: int = 12,
    indexed_hits: list[dict[str, Any]] | None = None,
) -> list[MemoryHit]:
    hits: list[MemoryHit] = []
    chapters = project.get("chapters", [])
    for index, chapter in enumerate(chapters[:current_chapter_index]):
        summary = chapter.get("summary", "").strip()
        if not summary:
            continue
        score = relevance(query, f"{chapter.get('title', '')} {summary}")
        recency = 1 / math.sqrt(max(1, current_chapter_index - index))
        if score > 0 or index >= current_chapter_index - 3:
            hits.append(
                MemoryHit(
                    "chapter",
                    chapter.get("title", f"第{index + 1}章"),
                    summary,
                    score + recency * 2,
                    chapter.get("id", ""),
                )
            )

    memory = project.get("memory", {})
    for fact in memory.get("facts", []):
        if not fact.get("active", True):
            continue
        valid_from = max(0, int(fact.get("valid_from_chapter", 0) or 0))
        valid_until = max(0, int(fact.get("valid_until_chapter", 0) or 0))
        current_number = current_chapter_index + 1
        if valid_from and current_number < valid_from:
            continue
        if valid_until and current_number > valid_until:
            continue
        text = fact.get("text", "")
        score = relevance(query, text + " " + " ".join(fact.get("tags", [])))
        importance = min(5, max(1, int(fact.get("importance", 3))))
        confidence = str(fact.get("confidence", "confirmed"))
        visibility = str(fact.get("visibility", "objective"))
        qualifier = ""
        if confidence == "suspected":
            qualifier += "[未证实推测，不得写成客观真相]"
            score -= 0.8
        if visibility == "private":
            qualifier += "[私密事实，不代表其他人物知情]"
        elif visibility == "rumor":
            qualifier += "[传闻，只能以传闻或猜测处理]"
            score -= 0.5
        if score > 0 or importance >= 5:
            hits.append(
                MemoryHit(
                    "fact",
                    "事实",
                    qualifier + text,
                    score + importance * 0.7,
                    fact.get("id", ""),
                )
            )

    open_threads = [
        thread
        for thread in memory.get("plot_threads", [])
        if isinstance(thread, dict)
        and normalize_thread_status(thread.get("status", "open")) != "closed"
    ]
    for thread_index, thread in enumerate(open_threads):
        lifecycle = thread_lifecycle(project, thread, current_chapter_index)
        text = "；".join(
            str(thread.get(key, ""))
            for key in (
                "title",
                "type",
                "setup",
                "latest",
                "expected_payoff",
                "payoff_condition",
                "payoff",
            )
            if thread.get(key)
        )
        stakeholders = "、".join(str(item) for item in thread.get("stakeholders", []))
        holders = "、".join(str(item) for item in thread.get("knowledge_holders", []))
        if stakeholders:
            text += f"；相关人物：{stakeholders}"
        if holders:
            text += f"；当前知情者：{holders}"
        score = relevance(query, text)
        # A long novel may carry dozens of open threads. Inject only relevant
        # ones plus the five most recently touched, otherwise old unrelated
        # threads can crowd all chapter/fact memories out of the prompt.
        recent_open = thread_index >= len(open_threads) - 5
        if score <= 0 and not recent_open and not lifecycle["stale"]:
            continue
        hits.append(
            MemoryHit(
                "thread",
                f"未结线索：{thread.get('title', '未命名')}",
                text
                + f"；状态：{lifecycle['status']}；静默：{lifecycle['dormancy']}章；"
                + f"治理：{lifecycle['action']}",
                score
                + (4.0 if lifecycle["stale"] else 0)
                + (1.8 if recent_open else 1.2),
                thread.get("id", ""),
            )
        )

    for event in memory.get("timeline", [])[-30:]:
        participants = "、".join(str(item) for item in event.get("participants", []))
        text = f"{event.get('time', '')}｜{event.get('location', '')}：{event.get('event', '')}"
        if participants:
            text += f"；参与者：{participants}"
        if event.get("effects"):
            text += "；后果：" + "、".join(str(item) for item in event.get("effects", []))
        score = relevance(query, text)
        if score > 0:
            hits.append(
                MemoryHit(
                    "timeline", "时间线", text, score + 1, event.get("id", "")
                )
            )

    for relation in memory.get("relationships", []):
        if not isinstance(relation, dict) or not relation.get("active", True):
            continue
        text = (
            f"{relation.get('left', '')}—{relation.get('right', '')}："
            f"{relation.get('state', '')}；张力：{relation.get('tension', '')}；"
            f"信任：{relation.get('trust', '')}；信息差：{relation.get('knowledge_gap', '')}"
        )
        score = relevance(query, text)
        if score > 0:
            hits.append(
                MemoryHit(
                    "relationship",
                    "关系状态",
                    text,
                    score + 2.0,
                    relation.get("id", ""),
                )
            )

    # SQLite FTS contributes paragraph-level recall from old accepted prose.
    # It is intentionally merged with (not substituted for) structured story
    # state, which remains authoritative and receives stronger base scoring.
    for item in indexed_hits or []:
        if not isinstance(item, dict):
            continue
        source_number = int(item.get("chapter_number", 0) or 0)
        stale_from = int(project.get("memory", {}).get("stale_from_chapter", 0) or 0)
        if source_number >= current_chapter_index + 1:
            continue
        if stale_from and source_number >= stale_from and item.get("kind") != "passage":
            continue
        kind = str(item.get("kind", "passage") or "passage")
        content = str(item.get("content", "")).strip()
        if not content:
            continue
        visibility = str(item.get("visibility", "objective"))
        qualifier = ""
        if visibility == "private":
            qualifier = "[私密事实，不代表其他人物知情]"
        elif visibility == "rumor":
            qualifier = "[传闻，只能以传闻或猜测处理]"
        rank = abs(float(item.get("rank", 0.0) or 0.0))
        score = relevance(
            query,
            f"{item.get('title', '')} {content} {item.get('tags', '')}",
        )
        hits.append(
            MemoryHit(
                kind,
                str(item.get("title", "历史正文片段")),
                qualifier + content,
                score + min(3.5, 0.6 + rank),
                str(item.get("source_id", "")),
                str(item.get("origin", "fts5")),
            )
        )

    hits.sort(key=lambda hit: hit.score, reverse=True)
    result: list[MemoryHit] = []
    seen: set[str] = set()
    remaining = list(hits)
    kind_caps = {
        "chapter": max(2, math.ceil(limit * 0.4)),
        "passage": max(1, math.ceil(limit * 0.3)),
        "fact": max(2, math.ceil(limit * 0.45)),
        "thread": max(1, math.ceil(limit * 0.3)),
        "timeline": max(1, math.ceil(limit * 0.2)),
        "relationship": max(1, math.ceil(limit * 0.25)),
        "character_knowledge": max(1, math.ceil(limit * 0.25)),
    }
    kind_counts: dict[str, int] = {}
    while remaining and len(result) < limit:
        candidates = [
            hit
            for hit in remaining
            if kind_counts.get(hit.kind, 0) < kind_caps.get(hit.kind, limit)
        ]
        if not candidates:
            candidates = remaining
        hit = max(
            candidates,
            key=lambda candidate: candidate.score
            - 2.2
            * max(
                (lexical_similarity(candidate.content, chosen.content) for chosen in result),
                default=0.0,
            ),
        )
        remaining.remove(hit)
        signature = re.sub(r"\s+", "", hit.content).casefold()
        if not signature or signature in seen:
            continue
        if any(lexical_similarity(hit.content, chosen.content) > 0.92 for chosen in result):
            continue
        seen.add(signature)
        result.append(hit)
        kind_counts[hit.kind] = kind_counts.get(hit.kind, 0) + 1
    return result


def render_memories(hits: list[MemoryHit]) -> str:
    if not hits:
        return ""
    return "\n".join(f"- [{hit.kind}] {hit.title}：{hit.content}" for hit in hits)

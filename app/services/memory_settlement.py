"""Evidence-bound chapter settlement and deterministic memory projection."""
from __future__ import annotations

import re
import uuid
from copy import deepcopy
from typing import Any

from app.core.clock import utc_now
from app.editorial_workflow import incomplete_memory_stages, sync_memory_repair
from app.evidence_audit import verify_quote
from app.knowledge import project_accepted_memory_to_knowledge
from app.manuscript_quality import refresh_learned_fatigue
from app.memory import normalize_thread_status, normalize_thread_timing
from app.memory_integrity import chapter_content_hash, derive_story_so_far
from app.domain.continuity_state import apply_continuity_updates
from app.temporal_context import DYNAMIC_FIELDS, finish_memory_rebuild, prepare_memory_rebuild

def _memory_key(value: Any) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’—…（）()]", "", str(value or "")).casefold()


def _memory_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _chapter_number(project: dict[str, Any], chapter: dict[str, Any]) -> int:
    return next(
        (
            index + 1
            for index, item in enumerate(project.get("chapters", []))
            if item.get("id") == chapter.get("id")
        ),
        1,
    )


def _verified_evidence(content: str, value: Any) -> tuple[str, bool]:
    """Validate a model-produced evidence quote against accepted prose.

    Exact (normalized) containment is tried first; otherwise a fuzzy partial
    match (ratio >= 0.9) locates the original sentence.  When verified, the
    returned evidence is the *accepted prose span*, never the model's
    paraphrase, so later checks keep matching the manuscript verbatim.
    """
    if isinstance(value, list):
        value = next((item for item in value if str(item or "").strip()), "")
    evidence = str(value or "").strip()[:240]
    if not evidence:
        return "", False
    if _memory_key(evidence) in _memory_key(content):
        return evidence, True
    located = verify_quote(content, evidence)
    if located.get("verified"):
        return str(located.get("quote", evidence))[:240], True
    return evidence, False


def _string_list(value: Any, limit: int = 12) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,，\n]", value)
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(str(item).strip() for item in value if str(item).strip())
    )[:limit]


def _relationship_key(left: Any, right: Any) -> str:
    pair = sorted((_memory_key(left), _memory_key(right)))
    return "|".join(pair)


def _apply_director_memory(
    project: dict[str, Any],
    chapter: dict[str, Any],
    result: dict[str, Any],
    *,
    rebuild_current_projection: bool = False,
) -> list[str]:
    prepare_memory_rebuild(project, chapter)
    warnings: list[str] = []
    number = _chapter_number(project, chapter)
    latest_settled_number = max(
        (
            index
            for index, item in enumerate(project.get("chapters", []), start=1)
            if isinstance(item, dict)
            and (
                str(item.get("memory_status", "")) == "committed"
                or bool(item.get("settlement"))
            )
        ),
        default=0,
    )
    historical_replay = (
        number < latest_settled_number and not rebuild_current_projection and not chapter.get("memory_stale")
    )
    accepted_content = str(chapter.get("content", ""))
    chapter["summary"] = str(result.get("summary") or chapter.get("summary", ""))
    if isinstance(chapter.get("route"), dict):
        chapter["route"]["status"] = "written"
    for volume in project.get("planning", {}).get("volumes", []):
        for route in volume.get("chapters", []):
            if route.get("id") == chapter.get("route_id"):
                route["status"] = "written"
    memory = project.setdefault("memory", {})
    if result.get("story_so_far") and not historical_replay:
        memory["story_digest_candidate"] = {
            "text": str(result["story_so_far"])[:4000],
            "status": "candidate",
            "source_chapter_id": chapter.get("id", ""),
            "chapter_number": number,
            "created_at": utc_now(),
        }
    for update in result.get("character_updates", []):
        if not isinstance(update, dict):
            continue
        update_name = str(update.get("name", "")).strip()
        character = next(
            (
                item
                for item in project.get("characters", [])
                if str(item.get("name", "")).strip().casefold()
                == update_name.casefold()
            ),
            None,
        )
        if not character:
            if update_name:
                warnings.append(f"未回写未知人物“{update_name}”的状态，请人工确认人物名称")
            continue
        evidence, evidence_verified = _verified_evidence(
            accepted_content, update.get("evidence")
        )
        state_fields = ("state", "location", "items", "emotion", "appearance_state")
        has_state_delta = any(str(update.get(key, "")).strip() for key in state_fields)
        if has_state_delta and not evidence:
            warnings.append(
                f"人物“{update_name}”的状态变化没有正文证据，已跳过状态回写"
            )
        elif has_state_delta and not evidence_verified:
            warnings.append(
                f"人物“{update_name}”的状态证据未在正文找到，已跳过状态回写"
            )
        elif has_state_delta:
            last_state_number = _memory_int(
                character.get("last_state_chapter_number", 0)
            )
            may_project_current_state = (
                number >= last_state_number
                if last_state_number > 0
                else not historical_replay
            )
            if not may_project_current_state:
                has_state_delta = False
        if has_state_delta and evidence_verified:
            character.setdefault("state_baseline", {key: character.get(key, "") for key in DYNAMIC_FIELDS})
            for key in state_fields:
                if update.get(key):
                    character[key] = str(update[key])
            character["last_state_chapter_number"] = number
            character["last_state_chapter_id"] = chapter.get("id", "")
        gain = str(update.get("knowledge_gain", "")).strip()
        knowledge_ledger = character.setdefault("knowledge_ledger", [])
        knowledge_keys = {
            _memory_key(item.get("text"))
            for item in knowledge_ledger
            if isinstance(item, dict) and item.get("active", True)
        }
        gains = update.get("knowledge_gains", [])
        if not isinstance(gains, list):
            gains = []
        if gain and not gains and evidence_verified:
            gains = [
                {
                    "text": gain,
                    "learned_how": "本章（兼容字段，方式未细分）",
                    "certainty": "confirmed",
                    "evidence": evidence,
                }
            ]
        elif gain and not gains:
            warnings.append(
                f"人物“{update_name}”的新增知情“{gain[:24]}”没有可核验正文证据，未写入"
            )
        for knowledge in gains:
            if not isinstance(knowledge, dict):
                continue
            text = str(knowledge.get("text", "")).strip()[:800]
            key = _memory_key(text)
            if not key or key in knowledge_keys:
                continue
            quote, verified = _verified_evidence(
                accepted_content, knowledge.get("evidence")
            )
            if not quote:
                warnings.append(
                    f"人物“{update_name}”的新增知情“{text[:24]}”没有正文证据，未写入"
                )
                continue
            if not verified:
                warnings.append(
                    f"人物“{update_name}”的新增知情“{text[:24]}”证据未在正文找到，未写入"
                )
                continue
            knowledge_ledger.append(
                {
                    "id": str(uuid.uuid4()),
                    "text": text,
                    "learned_how": str(knowledge.get("learned_how", "亲历"))[:120],
                    "certainty": (
                        "suspected"
                        if str(knowledge.get("certainty", "confirmed")).lower()
                        == "suspected"
                        else "confirmed"
                    ),
                    "source_chapter_id": chapter["id"],
                    "source_chapter_title": chapter.get("title", ""),
                    "chapter_number": number,
                    "evidence": quote,
                    "active": True,
                    "related_fact_ids": [],
                }
            )
            knowledge_keys.add(key)
        character["knowledge_ledger"] = knowledge_ledger[-200:]
    facts = memory.setdefault("facts", [])
    fact_map = {_memory_key(item.get("text")): item for item in facts if isinstance(item, dict)}
    for item in result.get("facts", []):
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            continue
        evidence, evidence_verified = _verified_evidence(
            accepted_content, item.get("evidence")
        )
        if not evidence:
            warnings.append(
                f"事实“{str(item.get('text', ''))[:28]}”没有正文证据，未写入权威事实"
            )
            continue
        if not evidence_verified:
            warnings.append(
                f"事实“{str(item.get('text', ''))[:28]}”的证据未在正文找到，未写入权威事实"
            )
            continue
        key = _memory_key(item["text"])
        supersedes_id = str(item.get("supersedes_id", "")).strip()
        if supersedes_id:
            superseded = next(
                (
                    fact for fact in facts
                    if isinstance(fact, dict) and fact.get("id") == supersedes_id
                ),
                None,
            )
            if superseded:
                superseded["active"] = False
                superseded["valid_until_chapter"] = number - 1
            else:
                warnings.append(
                    f"事实替换引用了不存在的 ID“{supersedes_id}”，旧事实未停用"
                )
        if key in fact_map:
            fact_map[key]["importance"] = min(
                5,
                max(
                    int(fact_map[key].get("importance", 3)),
                    int(item.get("importance", 3)),
                    1,
                ),
            )
            fact_map[key]["tags"] = list(
                dict.fromkeys(
                    _string_list(fact_map[key].get("tags"))
                    + _string_list(item.get("tags"))
                )
            )[:16]
            if evidence_verified:
                fact_map[key].update(
                    {
                        "evidence": evidence,
                        "evidence_verified": True,
                        "source_chapter_id": chapter["id"],
                        "source_chapter_title": chapter.get("title", ""),
                        "source_type": "accepted_chapter",
                        "reader_known": True,
                    }
                )
        else:
            added = {
                "id": str(uuid.uuid4()), "text": str(item["text"]),
                "tags": _string_list(item.get("tags"), 16),
                "importance": min(5, max(1, int(item.get("importance", 3)))), "active": True,
                "chapter_id": chapter["id"], "source_chapter_id": chapter["id"],
                "source_chapter_title": chapter.get("title", ""),
                "valid_from_chapter": number, "valid_until_chapter": 0,
                "confidence": (
                    "suspected"
                    if str(item.get("confidence", "confirmed")).lower() == "suspected"
                    else "confirmed"
                ),
                "visibility": str(item.get("visibility", "objective"))[:40],
                "known_by": [],
                "reader_known": True,
                "author_only": False,
                "evidence": evidence,
                "evidence_verified": evidence_verified,
                "source_type": "accepted_chapter",
                "supersedes_id": supersedes_id,
            }
            facts.append(added)
            fact_map[key] = added
    threads = memory.setdefault("plot_threads", [])
    for item in result.get("plot_threads", []):
        if not isinstance(item, dict) or not item.get("title"):
            continue
        raw_status = str(item.get("status", "open"))
        status = normalize_thread_status(raw_status)
        if raw_status.strip().casefold() not in {
            "open", "opened", "progressing", "reopened", "deferred", "ready", "closed",
            "resolved", "close", "已回收", "已解决", "advanced", "推进",
            "持续推进", "paused", "hold", "延后", "搁置", "payoff_ready", "可回收",
            "ready_for_payoff", "resolved_with_cost", "resolved_with_boundary",
            "resolved_as_process",
        }:
            warnings.append(f"线索“{item['title']}”返回了无效状态 {raw_status}，已改为 open")
        evidence, evidence_verified = _verified_evidence(
            accepted_content, item.get("evidence")
        )
        if not evidence:
            warnings.append(
                f"线索“{item['title']}”没有正文证据，本章线索更新已跳过"
            )
            continue
        if not evidence_verified:
            warnings.append(
                f"线索“{item['title']}”的推进证据未在正文找到，本章线索更新已跳过"
            )
            continue
        if status == "closed" and (not str(item.get("payoff", "")).strip() or not evidence_verified):
            warnings.append(
                f"线索“{item['title']}”缺少可核验回收结果，已降为 progressing"
            )
            status = "progressing"
        thread_id = str(item.get("thread_id") or item.get("id") or "").strip()
        old = next(
            (
                x for x in threads
                if (
                    thread_id and str(x.get("id", "")) == thread_id
                ) or _memory_key(x.get("title")) == _memory_key(item["title"])
            ),
            None,
        )
        if old:
            if _memory_int(old.get("last_advanced_chapter", 0)) > number:
                continue
            old.update(
                {
                    "status": status,
                    "latest": str(item.get("latest", old.get("latest", "")))[:1000],
                    "type": str(item.get("type", old.get("type", "mystery")))[:80],
                    "expected_payoff": str(
                        item.get("expected_payoff", old.get("expected_payoff", ""))
                    )[:1000],
                    "payoff_condition": str(
                        item.get("payoff_condition", old.get("payoff_condition", ""))
                    )[:1000],
                    "target_window": normalize_thread_timing(
                        item.get("target_window", old.get("target_window", "mid"))
                    ),
                    "stakeholders": list(
                        dict.fromkeys(
                            _string_list(old.get("stakeholders"))
                            + _string_list(item.get("stakeholders"))
                        )
                    )[:16],
                    "knowledge_holders": _string_list(
                        item.get("knowledge_holders", old.get("knowledge_holders", [])), 24
                    ),
                    "payoff": str(item.get("payoff", old.get("payoff", "")))[:1000],
                    "last_chapter_id": chapter["id"],
                    "last_advanced_chapter": number,
                    "evidence": evidence,
                    "evidence_verified": evidence_verified,
                }
            )
            if status == "closed":
                old["closed_chapter_number"] = number
        else:
            threads.append(
                {
                    "id": str(uuid.uuid4()),
                    "title": str(item["title"])[:200],
                    "type": str(item.get("type", "mystery"))[:80],
                    "status": status,
                    "setup": str(item.get("latest", ""))[:1000],
                    "latest": str(item.get("latest", ""))[:1000],
                    "expected_payoff": str(item.get("expected_payoff", ""))[:1000],
                    "payoff_condition": str(item.get("payoff_condition", ""))[:1000],
                    "target_window": normalize_thread_timing(item.get("target_window")),
                    "stakeholders": _string_list(item.get("stakeholders"), 16),
                    "knowledge_holders": _string_list(item.get("knowledge_holders"), 24),
                    "payoff": str(item.get("payoff", ""))[:1000],
                    "chapter_id": chapter["id"],
                    "last_chapter_id": chapter["id"],
                    "created_chapter_number": number,
                    "last_advanced_chapter": number,
                    "closed_chapter_number": number if status == "closed" else 0,
                    "evidence": evidence,
                    "evidence_verified": evidence_verified,
                }
            )
    timeline = memory.setdefault("timeline", [])
    event_keys = {f"{item.get('time')}|{item.get('event')}" for item in timeline if isinstance(item, dict)}
    for item in result.get("timeline", []):
        if not isinstance(item, dict) or not item.get("event"):
            continue
        evidence, evidence_verified = _verified_evidence(
            accepted_content, item.get("evidence")
        )
        if not evidence:
            warnings.append(
                f"时间线事件“{str(item.get('event', ''))[:28]}”没有正文证据，未写入"
            )
            continue
        if not evidence_verified:
            warnings.append(
                f"时间线事件“{str(item.get('event', ''))[:28]}”的证据未在正文找到，未写入"
            )
            continue
        key = f"{item.get('time')}|{item.get('event')}"
        if key not in event_keys:
            timeline.append(
                {
                    "id": str(uuid.uuid4()),
                    "time": str(item.get("time", "本章")),
                    "event": str(item["event"]),
                    "chapter_id": chapter["id"],
                    "chapter_number": number,
                    "participants": _string_list(item.get("participants"), 24),
                    "location": str(item.get("location", ""))[:240],
                    "causes": _string_list(item.get("causes"), 12),
                    "effects": _string_list(item.get("effects"), 12),
                    "evidence": evidence,
                    "evidence_verified": True,
                }
            )
            event_keys.add(key)
    relationships = memory.setdefault("relationships", [])
    relationship_map = {
        _relationship_key(item.get("left"), item.get("right")): item
        for item in relationships
        if isinstance(item, dict)
    }
    known_names = {
        str(item.get("name", "")).strip().casefold()
        for item in project.get("characters", [])
        if str(item.get("name", "")).strip()
    }
    for item in result.get("relationship_updates", []):
        if not isinstance(item, dict):
            continue
        left = str(item.get("left") or item.get("from") or "").strip()
        right = str(item.get("right") or item.get("to") or "").strip()
        if not left or not right or left.casefold() not in known_names or right.casefold() not in known_names:
            if left or right:
                warnings.append(f"关系更新“{left}—{right}”包含未知人物，已跳过")
            continue
        evidence, evidence_verified = _verified_evidence(
            accepted_content, item.get("evidence")
        )
        if not evidence:
            warnings.append(f"关系“{left}—{right}”没有正文证据，已跳过")
            continue
        if not evidence_verified:
            warnings.append(f"关系“{left}—{right}”的变化证据未在正文找到，已跳过")
            continue
        key = _relationship_key(left, right)
        relation = relationship_map.get(key)
        if relation and _memory_int(relation.get("last_chapter_number", 0)) > number:
            continue
        payload = {
            "left": left,
            "right": right,
            "state": str(item.get("state") or item.get("change") or "")[:1000],
            "tension": str(item.get("tension", ""))[:600],
            "trust": str(item.get("trust", ""))[:600],
            "knowledge_gap": str(item.get("knowledge_gap", ""))[:800],
            "active": True,
            "source_chapter_id": chapter["id"],
            "last_chapter_number": number,
            "evidence": evidence,
            "evidence_verified": evidence_verified,
        }
        if relation:
            relation.update(payload)
        else:
            relation = {"id": str(uuid.uuid4()), **payload}
            relationships.append(relation)
            relationship_map[key] = relation
    notes = memory.setdefault("continuity_notes", [])
    note_keys = {_memory_key(item.get("text")) for item in notes if isinstance(item, dict)}
    for value in result.get("continuity_notes", []) if not historical_replay else []:
        key = _memory_key(value)
        if key and key not in note_keys:
            notes.append({"id": str(uuid.uuid4()), "text": str(value), "resolved": False, "chapter_id": chapter["id"], "chapter_title": chapter.get("title", "")})
            note_keys.add(key)
    # Record only distinctive wording that can be verified in accepted prose.
    # This provides a precise anti-repetition memory without blacklisting normal
    # facts, names or necessary setting vocabulary.
    ledger = memory.setdefault("description_ledger", [])
    ledger_keys = {
        f"{_memory_key(item.get('character'))}|{_memory_key(item.get('aspect'))}|{_memory_key(item.get('phrase'))}"
        for item in ledger if isinstance(item, dict)
    }
    compact_content = _memory_key(chapter.get("content", ""))
    for item in result.get("description_updates", []):
        if not isinstance(item, dict):
            continue
        character = str(item.get("character", "")).strip()[:120]
        aspect = str(item.get("aspect", "")).strip()[:120]
        phrase = str(item.get("phrase", "")).strip()[:240]
        phrase_key = _memory_key(phrase)
        if len(phrase_key) < 8 or phrase_key not in compact_content:
            continue
        key = f"{_memory_key(character)}|{_memory_key(aspect)}|{phrase_key}"
        if key in ledger_keys:
            continue
        ledger.append({
            "id": str(uuid.uuid4()),
            "character": character,
            "aspect": aspect,
            "phrase": phrase,
            "chapter_id": chapter["id"],
            "chapter_title": chapter.get("title", ""),
        })
        ledger_keys.add(key)
    memory["description_ledger"] = ledger[-300:]
    # Item and injury state machines: evidence-bound, chapter-stamped events.
    warnings.extend(
        apply_continuity_updates(
            memory,
            chapter,
            number,
            result,
            lambda evidence: _verified_evidence(accepted_content, evidence),
            prose=accepted_content,
        )
    )
    # Only evidence-backed updates from an accepted chapter are projected into
    # the structured knowledge layer.  The graph remains a rebuildable index,
    # never an unreviewed model-generated source of truth.
    projected_knowledge_ids = project_accepted_memory_to_knowledge(project, chapter, result)
    memory["story_so_far"] = derive_story_so_far(project)
    scene_settlement = (
        result.get("scene_settlement")
        if isinstance(result.get("scene_settlement"), dict)
        else {}
    )
    failed_stages = [
        str(item) for item in (result.get("failed_stages") or [])
        if str(item).strip()
    ] if isinstance(result.get("failed_stages"), list) else []
    if failed_stages:
        warnings.append(
            "记忆提取未完成阶段："
            + "、".join(failed_stages)
            + "；本章记忆暂未完整结算，请重新提取本章记忆"
        )
    chapter["settlement"] = {
        # A partially extracted chapter is not a fresh, complete settlement:
        # leaving the hash empty keeps it from being treated as authoritative.
        "content_hash": "" if failed_stages else chapter_content_hash(chapter.get("content", "")),
        "incomplete_stages": failed_stages,
        "state_version": 4,
        "chapter_number": number,
        "summary": chapter.get("summary", ""),
        "goal_achieved": str(scene_settlement.get("goal_achieved", ""))[:40],
        "irreversible_changes": _string_list(
            scene_settlement.get("irreversible_changes"), 12
        ),
        "open_questions": _string_list(scene_settlement.get("open_questions"), 12),
        "closing_state": str(scene_settlement.get("closing_state", ""))[:1200],
        "character_updates": deepcopy(result.get("character_updates", []))[:40]
        if isinstance(result.get("character_updates"), list)
        else [],
        "fact_ids": [
            item.get("id", "")
            for item in facts
            if isinstance(item, dict) and item.get("source_chapter_id") == chapter.get("id")
        ],
        "thread_ids": [
            item.get("id", "")
            for item in threads
            if isinstance(item, dict) and item.get("last_chapter_id") == chapter.get("id")
        ],
        "relationship_ids": [
            item.get("id", "")
            for item in relationships
            if isinstance(item, dict) and item.get("source_chapter_id") == chapter.get("id")
        ],
        "knowledge_ids": projected_knowledge_ids,
        "warnings": list(warnings),
    }
    finish_memory_rebuild(project, chapter)
    execution = chapter.setdefault("execution", {})
    if failed_stages:
        execution["memory_needs_repair"] = failed_stages
    else:
        execution.pop("memory_needs_repair", None)
    sync_memory_repair(project, chapter)
    result["warnings"] = warnings
    if warnings:
        execution = chapter.setdefault("execution", {})
        existing_warnings = [
            str(item) for item in execution.get("warnings", []) if str(item).strip()
        ]
        execution["warnings"] = list(dict.fromkeys(existing_warnings + warnings))[-20:]
        execution["last_memory_update_at"] = utc_now()
    else:
        execution = chapter.setdefault("execution", {})
        execution["warnings"] = []
        execution["last_memory_update_at"] = utc_now()
    # Book-specific fatigue phrases are a rebuildable projection of the
    # accepted prose; refresh them whenever settled memory changes.
    refresh_learned_fatigue(project)
    return warnings


def _fact_entities(project: dict[str, Any], fact: dict[str, Any]) -> set[str]:
    """Entity keys a fact is about: its tags, else character names in its text."""
    terms = {_memory_key(tag) for tag in _string_list(fact.get("tags"), 16)}
    if not terms:
        text = _memory_key(fact.get("text"))
        terms = {
            _memory_key(item.get("name"))
            for item in project.get("characters", [])
            if isinstance(item, dict) and _memory_key(item.get("name")) and _memory_key(item.get("name")) in text
        }
    return {term for term in terms if len(term) >= 2}


def _later_chapters_touch(project: dict[str, Any], entities: set[str], number: int) -> bool:
    """True when settled state from a chapter after ``number`` concerns ``entities``."""
    if not entities:
        return True  # cannot tell what the fact is about: be conservative
    for fact in project.get("memory", {}).get("facts", []):
        if not isinstance(fact, dict) or _memory_int(fact.get("valid_from_chapter", 0)) <= number:
            continue
        text = _memory_key(fact.get("text"))
        if entities & _fact_entities(project, fact) or any(term in text for term in entities):
            return True
    for position, item in enumerate(project.get("chapters", []), start=1):
        if position <= number or not isinstance(item, dict):
            continue
        settlement = item.get("settlement") if isinstance(item.get("settlement"), dict) else {}
        for update in settlement.get("character_updates", []) or []:
            if isinstance(update, dict) and _memory_key(update.get("name")) in entities:
                return True
    return False


def _divert_facts_for_review(project: dict[str, Any], result: dict[str, Any], number: int) -> list[dict[str, Any]]:
    """Pull new repair facts about entities later chapters already settled.

    Repairing an older chapter must not make a fact that later chapters may
    have superseded current again; such facts are stored for author review
    instead of entering the authoritative (and knowledge-projected) set.
    """
    existing = {
        _memory_key(item.get("text"))
        for item in project.get("memory", {}).get("facts", [])
        if isinstance(item, dict)
    }
    kept: list[Any] = []
    diverted: list[dict[str, Any]] = []
    for item in result.get("facts", []) or []:
        if (
            isinstance(item, dict)
            and _memory_key(item.get("text"))
            and _memory_key(item.get("text")) not in existing
            and _later_chapters_touch(project, _fact_entities(project, item), number)
        ):
            diverted.append(item)
        else:
            kept.append(item)
    result["facts"] = kept
    return diverted


def _store_review_facts(
    project: dict[str, Any], chapter: dict[str, Any], number: int, items: list[dict[str, Any]]
) -> list[str]:
    facts = project.setdefault("memory", {}).setdefault("facts", [])
    content = str(chapter.get("content", ""))
    warnings: list[str] = []
    stored: list[str] = []
    for item in items:
        evidence, verified = _verified_evidence(content, item.get("evidence"))
        if not evidence or not verified:
            warnings.append(f"事实“{str(item.get('text', ''))[:28]}”的证据未在正文找到，未写入")
            continue
        fact = {
            "id": str(uuid.uuid4()), "text": str(item["text"]),
            "tags": _string_list(item.get("tags"), 16),
            "importance": min(5, max(1, _memory_int(item.get("importance", 3)) or 3)),
            "active": False, "needs_review": True,
            "review_reason": "补提取较早章节；后续章节已涉及相同人物或对象，需确认是否已被改写",
            "chapter_id": chapter["id"], "source_chapter_id": chapter["id"],
            "source_chapter_title": chapter.get("title", ""),
            "valid_from_chapter": number, "valid_until_chapter": 0,
            "confidence": "suspected", "visibility": str(item.get("visibility", "objective"))[:40],
            "known_by": [], "reader_known": True, "author_only": False,
            "evidence": evidence, "evidence_verified": True,
            "source_type": "memory_repair", "supersedes_id": "",
        }
        facts.append(fact)
        stored.append(fact["id"])
    if stored:
        chapter.setdefault("settlement", {})["review_fact_ids"] = stored
        warnings.append(
            f"补提取的 {len(stored)} 条事实涉及后续章节已结算的人物或对象，已存为待复核（未生效）"
        )
    return warnings


# Settlement keys owned by each extraction stage.  A repair run only
# re-extracts some stages; the others keep what the original pass settled.
_STAGE_SETTLEMENT_KEYS = {
    "core": ("summary", "goal_achieved", "irreversible_changes", "open_questions", "closing_state"),
    "characters": ("character_updates",),
}


async def repair_incomplete_memory(
    project: dict[str, Any],
    chapter_id: str,
    *,
    completion: Any,
    settings: dict[str, Any] | None = None,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Re-run only the extraction stages that failed for one chapter.

    The new stage output is settled through the normal path (evidence checks,
    state machines, knowledge projection); settlement fields owned by stages
    that were *not* re-run are preserved.  When every missing stage succeeds
    the flags are cleared, the repair item is resolved and the settled content
    hash is written.  Mutates ``project`` in place; the caller persists it.

    Returns ``{"status": "noop" | "repaired" | "partial", "stages": [...],
    "remaining": [...], "warnings": [...]}``.
    """
    from app.services.memory_extraction import (
        MEMORY_STAGES,
        build_stage_inputs,
        extract_staged_memory,
    )

    index, chapter = next(
        (
            (position, item) for position, item in enumerate(project.get("chapters", []))
            if isinstance(item, dict) and str(item.get("id", "")) == str(chapter_id)
        ),
        (-1, None),
    )
    if chapter is None:
        raise KeyError(chapter_id)
    stages = incomplete_memory_stages(chapter)
    execution = chapter.get("execution") if isinstance(chapter.get("execution"), dict) else {}
    if not stages and isinstance(execution.get("memory_needs_repair"), list):
        stages = [str(item) for item in execution["memory_needs_repair"] if str(item).strip()]
    if not stages:
        sync_memory_repair(project, chapter)
        return {"status": "noop", "stages": [], "remaining": [], "warnings": []}
    number = index + 1
    boundary = _memory_int(project.get("memory", {}).get("stale_from_chapter", 0))
    if chapter.get("memory_stale") or (boundary and boundary <= number):
        # The settled deltas of this chapter are invalid (its own or an
        # earlier chapter's prose changed): re-running a few stages would
        # discard the rest and mark the chapter as freshly settled.
        raise ValueError(
            f"第 {number} 章的派生记忆已因正文修改而过期，不能只补提取部分阶段；"
            f"请从第 {boundary or number} 章起按顺序完整重新结算记忆"
        )
    known = {stage.name for stage in MEMORY_STAGES}
    unknown = [name for name in stages if name not in known]
    if unknown:
        raise ValueError(
            "记忆阶段“" + "、".join(unknown) + "”已不存在，无法单独补提取；请完整重新结算本章记忆"
        )
    latest_settled_number = max(
        (
            position
            for position, item in enumerate(project.get("chapters", []), start=1)
            if isinstance(item, dict)
            and (str(item.get("memory_status", "")) == "committed" or bool(item.get("settlement")))
        ),
        default=0,
    )
    content = str(chapter.get("content", ""))
    shared_context, state_blocks = build_stage_inputs(project, index, content)
    result = await extract_staged_memory(
        settings if settings is not None else project.get("settings", {}),
        shared_context,
        state_blocks,
        completion=completion,
        deadline=deadline,
        only=stages,
    )
    previous = deepcopy(chapter.get("settlement") or {})
    previous_summary = str(chapter.get("summary", ""))
    if "core" not in stages:
        # Feed the settled core back so the settlement is rebuilt unchanged.
        result["summary"] = previous_summary
        result["story_so_far"] = ""
        result["scene_settlement"] = {
            key: previous.get(key) for key in ("goal_achieved", "irreversible_changes", "open_questions", "closing_state")
        }
    review_facts = (
        _divert_facts_for_review(project, result, number)
        if "facts" in stages and number < latest_settled_number
        else []
    )
    warnings = _apply_director_memory(project, chapter, result)
    if review_facts:
        warnings += _store_review_facts(project, chapter, number, review_facts)
    settlement = chapter.setdefault("settlement", {})
    for stage, keys in _STAGE_SETTLEMENT_KEYS.items():
        if stage in stages:
            continue
        for key in keys:
            if key in previous:
                settlement[key] = deepcopy(previous[key])
    if "characters" in stages and isinstance(previous.get("character_updates"), list):
        settlement["character_updates"] = (
            previous["character_updates"] + list(settlement.get("character_updates", []))
        )[:40]
    remaining = list(result.get("failed_stages", []))
    return {
        "status": "repaired" if not remaining else "partial",
        "stages": stages,
        "remaining": remaining,
        "warnings": warnings,
    }

"""Evidence-bound chapter settlement and deterministic memory projection."""
from __future__ import annotations

import re
import uuid
from copy import deepcopy
from typing import Any

from ..db import utc_now
from ..knowledge import project_accepted_memory_to_knowledge
from ..memory import normalize_thread_status, normalize_thread_timing
from ..memory_integrity import chapter_content_hash, derive_story_so_far
from ..temporal_context import (
    DYNAMIC_FIELDS, finish_memory_rebuild, prepare_memory_rebuild,
)

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
    """Validate a model-produced evidence quote against accepted prose."""
    evidence = str(value or "").strip()[:240]
    if not evidence:
        return "", False
    return evidence, _memory_key(evidence) in _memory_key(content)


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
    chapter["settlement"] = {
        "content_hash": chapter_content_hash(chapter.get("content", "")),
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
    return warnings



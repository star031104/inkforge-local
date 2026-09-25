from __future__ import annotations

import re
import uuid
from typing import Any

from ..canon import ensure_fanfic_defaults
from ..core.coercion import safe_non_negative_int as _safe_int
from ..knowledge import ensure_knowledge_defaults
from ..planning import empty_planning, ensure_planning_defaults
from ..references import ensure_reference_defaults
from ..story_systems import ensure_professional_defaults
from ..writing_skills import ensure_project_writing_skills


def default_project(project_id: str, title: str, now: str) -> dict[str, Any]:
    return ensure_project_defaults({
        "id": project_id,
        "title": title,
        "genre": "幻想",
        "story_mode": "long",
        "premise": "",
        "outline": "",
        "author_intent": "",
        "current_focus": "",
        "book_rules": "",
        "production_spec": "",
        "author_note": "",
        "memory": {
            "state_version": 4,
            "epistemic_schema_version": 1,
            "story_so_far": "",
            "story_digest_candidate": {},
            "facts": [],
            "plot_threads": [],
            "timeline": [],
            "relationships": [],
            "continuity_notes": [],
            "description_ledger": [],
            "commits": [],
        },
        "narrative": {
            "pov": "auto",
            "tense": "auto",
            "tone": "",
            "central_question": "",
            "ending_direction": "",
            "current_arc": "",
            "target_chapters": 30,
        },
        "planning": empty_planning(),
        "created_at": now,
        "updated_at": now,
        "settings": {
            "model_routing": "single",
            "provider": "zhipu",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "",
            "model": "glm-4.7-flash",
            "reasoning_provider": "modelscope",
            "reasoning_base_url": "https://api-inference.modelscope.cn/v1",
            "reasoning_api_key": "",
            "reasoning_model": "ZhipuAI/GLM-5.2",
            "temperature": 0.82,
            "top_p": 0.92,
            "top_k": 40,
            "min_p": 0.05,
            "repeat_penalty": 1.08,
            "enable_thinking": False,
            "thinking_budget": 0,
            "max_tokens": 3500,
            "context_budget": 24000,
            "recent_chars": 12000,
            "target_words": 1200,
            "memory_items": 12,
            "lore_budget": 4500,
            "lore_recursion_steps": 2,
            "creative_freedom": "balanced",
            "role_routes": {},
            "research": {
                "provider": "bing_rss",
                "searxng_url": "",
                "brave_api_key": "",
            },
        },
        "style": {
            "name": "默认文风",
            "sample": "",
            "profile": "",
            "dos": [],
            "donts": [],
            "source_ids": [],
        },
        "references": [],
        "knowledge": {"schema_version": 1, "entities": [], "facts": [], "relations": [], "review_queue": []},
        "fanfic": {
            "enabled": False,
            "mode": "canon",
            "source_universes": [],
            "policy": {
                "preserve_identity": True,
                "preserve_core_personality": True,
                "preserve_voice": True,
                "preserve_abilities": True,
                "require_causal_character_change": True,
                "unverified_ai_inference_is_hard_canon": False,
            },
        },
        "characters": [],
        "world_entries": [],
        "writing_skills": [],
        "chapters": [
            {
                "id": str(uuid.uuid4()),
                "title": "第一章",
                "summary": "",
                "content": "",
                "scene_goal": "",
                "author_note": "",
                "plan": {
                    "goal": "",
                    "conflict": "",
                    "must_keep": [],
                    "must_avoid": [],
                    "turning_point": "",
                    "ending_hook": "",
                    "chapter_type": "",
                    "pov_character": "",
                    "time_location": "",
                    "opening_beat": "",
                    "scene_beats": [],
                    "emotional_turn": "",
                    "thread_actions": [],
                    "exit_state": "",
                    "ending_type": "",
                },
            }
        ],
    })


def ensure_project_defaults(project: dict[str, Any]) -> dict[str, Any]:
    """Migrate older saved projects without destructive schema rewrites."""
    if not isinstance(project, dict):
        project = {}
    ensure_professional_defaults(project)
    project.setdefault("id", "")
    project.setdefault("title", "未命名故事")
    project.setdefault("genre", "")
    project.setdefault("premise", "")
    project.setdefault("outline", "")
    project.setdefault("author_intent", "")
    project.setdefault("current_focus", "")
    project.setdefault("book_rules", "")
    project.setdefault("production_spec", "")
    project.setdefault("author_note", "")
    project.setdefault("story_mode", "long")
    if not isinstance(project.get("must_contracts"), list):
        project["must_contracts"] = []
    if not isinstance(project.get("repair_queue"), list):
        project["repair_queue"] = []
    if not isinstance(project.get("memory"), dict):
        project["memory"] = {}
    memory = project["memory"]
    try:
        memory["state_version"] = max(4, int(memory.get("state_version", 0) or 0))
    except (TypeError, ValueError):
        memory["state_version"] = 4
    memory["epistemic_schema_version"] = max(
        1, _safe_int(memory.get("epistemic_schema_version"))
    )
    memory.setdefault("story_so_far", "")
    if not isinstance(memory.get("story_digest_candidate"), dict):
        memory["story_digest_candidate"] = {}
    for key in (
        "facts",
        "plot_threads",
        "timeline",
        "relationships",
        "continuity_notes",
        "description_ledger",
        "commits",
    ):
        if not isinstance(memory.get(key), list):
            memory[key] = []
    memory["facts"] = [
        (
            item
            if isinstance(item, dict)
            else {
                "id": str(uuid.uuid4()),
                "text": str(item),
                "tags": [],
                "importance": 3,
                "active": True,
            }
        )
        for item in memory["facts"]
        if isinstance(item, dict) or str(item).strip()
    ]
    for item in memory["facts"]:
        item.setdefault("id", str(uuid.uuid4()))
        item.setdefault("text", "")
        item.setdefault("tags", [])
        if isinstance(item.get("tags"), str):
            item["tags"] = [
                value.strip()
                for value in re.split(r"[,，\n]", item["tags"])
                if value.strip()
            ]
        elif not isinstance(item.get("tags"), list):
            item["tags"] = []
        item["importance"] = min(5, max(1, int(item.get("importance", 3) or 3)))
        item.setdefault("active", True)
        item.setdefault("confidence", "confirmed")
        item.setdefault("visibility", "objective")
        if not isinstance(item.get("known_by"), list):
            item["known_by"] = []
        item["known_by"] = list(
            dict.fromkeys(
                str(value).strip()
                for value in item["known_by"]
                if str(value).strip()
            )
        )[:30]
        item.setdefault(
            "reader_known",
            bool(item.get("source_chapter_id") and item.get("evidence_verified")),
        )
        item.setdefault("author_only", False)
        item.setdefault("evidence", "")
        item.setdefault("evidence_verified", False)
        item.setdefault("source_chapter_id", item.get("chapter_id", ""))
        item.setdefault("source_chapter_title", "")
        item.setdefault("valid_from_chapter", 0)
        item.setdefault("valid_until_chapter", 0)
        item.setdefault("supersedes_id", "")
        item.setdefault(
            "source_type",
            "accepted_chapter"
            if item.get("source_chapter_id") and item.get("evidence_verified")
            else "legacy",
        )
    memory["plot_threads"] = [
        (
            item
            if isinstance(item, dict)
            else {
                "id": str(uuid.uuid4()),
                "title": str(item),
                "status": "open",
                "latest": "",
            }
        )
        for item in memory["plot_threads"]
        if isinstance(item, dict) or str(item).strip()
    ]
    thread_status_aliases = {
        "resolved": "closed", "close": "closed", "已回收": "closed",
        "advanced": "progressing", "推进": "progressing", "持续推进": "progressing",
        "paused": "deferred", "hold": "deferred", "延后": "deferred",
        "payoff_ready": "ready", "可回收": "ready",
    }
    for item in memory["plot_threads"]:
        item.setdefault("id", str(uuid.uuid4()))
        item.setdefault("title", "")
        status = str(item.get("status", "open")).strip().casefold()
        status = thread_status_aliases.get(status, status)
        item["status"] = (
            status
            if status in {"open", "progressing", "deferred", "ready", "closed"}
            else "open"
        )
        item.setdefault("type", "mystery")
        item.setdefault("setup", item.get("latest", ""))
        item.setdefault("latest", item.get("setup", ""))
        item.setdefault("expected_payoff", item.get("payoff", ""))
        item.setdefault("payoff_condition", "")
        item.setdefault("payoff", "")
        item.setdefault("target_window", "mid")
        item.setdefault("stakeholders", [])
        item.setdefault("knowledge_holders", [])
        for key in ("stakeholders", "knowledge_holders"):
            if isinstance(item.get(key), str):
                item[key] = [
                    value.strip()
                    for value in re.split(r"[,，\n]", item[key])
                    if value.strip()
                ]
            elif not isinstance(item.get(key), list):
                item[key] = []
        item.setdefault("created_chapter_number", 0)
        item.setdefault("last_advanced_chapter", 0)
        item.setdefault("closed_chapter_number", 0)
        item.setdefault("chapter_id", item.get("source_chapter_id", ""))
        item.setdefault("last_chapter_id", item.get("chapter_id", ""))
        item.setdefault("evidence", "")
        item.setdefault("evidence_verified", False)
    memory["timeline"] = [
        (
            item
            if isinstance(item, dict)
            else {
                "id": str(uuid.uuid4()),
                "time": "",
                "event": str(item),
            }
        )
        for item in memory["timeline"]
        if isinstance(item, dict) or str(item).strip()
    ]
    for item in memory["timeline"]:
        item.setdefault("id", str(uuid.uuid4()))
        item.setdefault("time", "")
        item.setdefault("event", "")
        item.setdefault("chapter_id", "")
        item.setdefault("chapter_number", 0)
        item.setdefault("location", "")
        item.setdefault("evidence", "")
        item.setdefault("evidence_verified", False)
        for key in ("participants", "causes", "effects"):
            item.setdefault(key, [])
            if isinstance(item.get(key), str):
                item[key] = [
                    value.strip()
                    for value in re.split(r"[,，\n]", item[key])
                    if value.strip()
                ]
            elif not isinstance(item.get(key), list):
                item[key] = []
    memory["relationships"] = [
        item for item in memory["relationships"] if isinstance(item, dict)
    ]
    for item in memory["relationships"]:
        item.setdefault("id", str(uuid.uuid4()))
        item.setdefault("left", "")
        item.setdefault("right", "")
        item.setdefault("state", "")
        item.setdefault("tension", "")
        item.setdefault("trust", "")
        item.setdefault("knowledge_gap", "")
        item.setdefault("active", True)
        item.setdefault("source_chapter_id", "")
        item.setdefault("last_chapter_number", 0)
        item.setdefault("evidence", "")
        item.setdefault("evidence_verified", False)
    memory["continuity_notes"] = [
        (
            item
            if isinstance(item, dict)
            else {
                "id": str(uuid.uuid4()),
                "text": str(item),
                "resolved": False,
            }
        )
        for item in memory["continuity_notes"]
        if isinstance(item, dict) or str(item).strip()
    ]
    memory["description_ledger"] = [
        item for item in memory["description_ledger"] if isinstance(item, dict)
    ][-300:]
    for item in memory["description_ledger"]:
        item.setdefault("id", str(uuid.uuid4()))
        item.setdefault("character", "")
        item.setdefault("aspect", "描写")
        item.setdefault("phrase", "")
        item.setdefault("chapter_id", "")
    for note in memory["continuity_notes"]:
        note.setdefault("id", str(uuid.uuid4()))
        note.setdefault("text", "")
        note.setdefault("resolved", False)
        note.setdefault("chapter_id", "")
        note.setdefault("chapter_title", "")
    valid_commit_statuses = {
        "settlement_pending",
        "settlement_extracted",
        "committed",
        "state_degraded",
    }
    memory["commits"] = [
        item for item in memory["commits"] if isinstance(item, dict)
    ][-200:]
    for commit in memory["commits"]:
        commit.setdefault("id", str(uuid.uuid4()))
        commit.setdefault("chapter_id", "")
        commit.setdefault("chapter_title", "")
        commit.setdefault("content_hash", "")
        status = str(commit.get("status", "state_degraded"))
        commit["status"] = (
            status if status in valid_commit_statuses else "state_degraded"
        )
        commit["attempts"] = max(1, int(commit.get("attempts", 1) or 1))
        commit.setdefault("error", "")
        if not isinstance(commit.get("warnings"), list):
            commit["warnings"] = []
        commit.setdefault("created_at", "")
        commit.setdefault("updated_at", "")
        commit.setdefault("committed_at", "")
    if not isinstance(project.get("narrative"), dict):
        project["narrative"] = {}
    narrative = project["narrative"]
    narrative.setdefault("pov", "auto")
    narrative.setdefault("tense", "auto")
    narrative.setdefault("tone", "")
    narrative.setdefault("central_question", "")
    narrative.setdefault("ending_direction", "")
    narrative.setdefault("current_arc", "")
    narrative.setdefault("target_chapters", 30)
    project["planning"] = ensure_planning_defaults(project.get("planning"))
    if not isinstance(project.get("settings"), dict):
        project["settings"] = {}
    settings = project["settings"]
    raw_provider = str(settings.get("provider") or "").strip().lower()
    configured_base = str(settings.get("base_url") or "").strip()
    configured_base_lower = configured_base.lower()
    legacy_cloud = raw_provider in {"siliconflow", "xai"} or any(
        marker in configured_base_lower for marker in ("siliconflow", "api.x.ai")
    )
    if legacy_cloud:
        provider = "zhipu"
        settings["provider"] = provider
        settings["base_url"] = "https://open.bigmodel.cn/api/paas/v4"
        settings["model"] = "glm-4.7-flash"
        settings["api_key"] = ""
    elif "open.bigmodel.cn" in configured_base_lower:
        provider = "zhipu"
        settings["provider"] = provider
        settings["base_url"] = "https://open.bigmodel.cn/api/paas/v4"
    elif "api-inference.modelscope.cn" in configured_base_lower or raw_provider == "modelscope":
        provider = "modelscope"
        settings["provider"] = provider
        settings["base_url"] = "https://api-inference.modelscope.cn/v1"
    elif raw_provider == "zhipu":
        provider = "zhipu"
        settings["provider"] = provider
    elif any(marker in configured_base_lower for marker in ("127.0.0.1", "localhost")) and raw_provider != "openai_compatible":
        provider = "llama_cpp"
        settings["provider"] = provider
    elif raw_provider in {"llama_cpp", "openai_compatible", "modelscope"}:
        provider = raw_provider
        settings["provider"] = provider
    elif not raw_provider and not configured_base:
        provider = "zhipu"
        settings["provider"] = provider
    else:
        provider = "openai_compatible"
        settings["provider"] = provider
    provider_base_urls = {
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "modelscope": "https://api-inference.modelscope.cn/v1",
        "llama_cpp": "http://127.0.0.1:8080/v1",
        "openai_compatible": "http://127.0.0.1:8080/v1",
    }
    provider_models = {
        "zhipu": "glm-4.7-flash",
        "modelscope": "ZhipuAI/GLM-5.2",
        "llama_cpp": "",
        "openai_compatible": "",
    }
    settings.setdefault(
        "base_url",
        provider_base_urls.get(provider, "http://127.0.0.1:8080/v1"),
    )
    if "api_key" not in settings:
        settings["api_key"] = "no-key" if provider == "llama_cpp" else ""
    settings.setdefault("model", provider_models.get(provider, ""))
    if provider == "zhipu":
        if not str(settings.get("base_url") or "").strip():
            settings["base_url"] = provider_base_urls[provider]
        if not str(settings.get("model") or "").strip():
            settings["model"] = provider_models[provider]
        if str(settings.get("api_key") or "").strip() == "no-key":
            settings["api_key"] = ""
    routing_default = "single"
    routing = str(settings.get("model_routing") or routing_default).strip().lower()
    settings["model_routing"] = routing if routing in {"dual", "single"} else routing_default
    settings.setdefault("reasoning_provider", "modelscope")
    settings.setdefault("reasoning_base_url", "https://api-inference.modelscope.cn/v1")
    settings.setdefault("reasoning_api_key", "")
    settings.setdefault("reasoning_model", "ZhipuAI/GLM-5.2")
    settings.setdefault("temperature", 0.82)
    settings.setdefault("top_p", 0.92)
    settings.setdefault("max_tokens", 3500)
    settings.setdefault("context_budget", 24000)
    settings.setdefault("target_words", 1200)
    settings.setdefault("memory_items", 12)
    settings.setdefault("lore_budget", 4500)
    settings.setdefault("lore_recursion_steps", 2)
    creative_freedom = str(settings.get("creative_freedom") or "balanced").strip().lower()
    settings["creative_freedom"] = (
        creative_freedom
        if creative_freedom in {"strict", "balanced", "exploratory"}
        else "balanced"
    )
    settings.setdefault("recent_chars", 12000)
    settings.setdefault("top_k", 40)
    settings.setdefault("min_p", 0.05)
    settings.setdefault("repeat_penalty", 1.08)
    settings.setdefault("enable_thinking", False)
    settings.setdefault("thinking_budget", 0)
    if not isinstance(settings.get("role_routes"), dict):
        settings["role_routes"] = {}
    if not isinstance(settings.get("research"), dict):
        settings["research"] = {}
    settings["research"].setdefault("provider", "bing_rss")
    settings["research"].setdefault("searxng_url", "")
    settings["research"].setdefault("brave_api_key", "")
    if not isinstance(project.get("style"), dict):
        project["style"] = {}
    style = project["style"]
    style.setdefault("name", "默认文风")
    style.setdefault("sample", "")
    style.setdefault("profile", "")
    if not isinstance(style.get("dos"), list):
        style["dos"] = []
    if not isinstance(style.get("donts"), list):
        style["donts"] = []
    if not isinstance(style.get("source_ids"), list):
        style["source_ids"] = []
    ensure_reference_defaults(project)
    ensure_knowledge_defaults(project)
    ensure_fanfic_defaults(project)
    ensure_project_writing_skills(project)
    if not isinstance(project.get("characters"), list):
        project["characters"] = []
    if not isinstance(project.get("world_entries"), list):
        project["world_entries"] = []
    if not isinstance(project.get("chapters"), list):
        project["chapters"] = []
    project["characters"] = [
        item for item in project["characters"] if isinstance(item, dict)
    ]
    project["world_entries"] = [
        item for item in project["world_entries"] if isinstance(item, dict)
    ]
    project["chapters"] = [
        item for item in project["chapters"] if isinstance(item, dict)
    ]
    if not project["chapters"]:
        project["chapters"].append(
            {
                "id": str(uuid.uuid4()),
                "title": "第一章",
                "summary": "",
                "content": "",
                "scene_goal": "",
                "plan": {},
            }
        )
    legacy_author_note = str(project.get("author_note", "")).strip()
    for chapter in project["chapters"]:
        chapter.setdefault("id", str(uuid.uuid4()))
        chapter.setdefault("title", "未命名章节")
        chapter.setdefault("content", "")
        chapter.setdefault("summary", "")
        chapter.setdefault("scene_goal", "")
        chapter.setdefault("author_note", "")
        if not isinstance(chapter.get("settlement"), dict):
            chapter["settlement"] = {}
        chapter.setdefault(
            "memory_status",
            "committed" if chapter["settlement"] else "never_settled",
        )
        chapter.setdefault("memory_commit_id", "")
        chapter.setdefault("accepted_content_hash", "")
        legacy_locked = bool(chapter.get("accepted_content_hash")) or str(
            chapter.get("memory_status", "")
        ) == "committed"
        if str(chapter.get("authority_state", "")) not in {
            "candidate", "reviewed", "accepted", "locked"
        }:
            chapter["authority_state"] = "locked" if legacy_locked else "candidate"
        chapter.setdefault(
            "locked_content_hash",
            chapter.get("accepted_content_hash", "") if legacy_locked else "",
        )
        chapter.setdefault("locked_at", "")
        chapter.setdefault("locked_by", "")
        if not isinstance(chapter.get("lock_receipt"), dict):
            chapter["lock_receipt"] = {}
        if not isinstance(chapter.get("workflow"), dict):
            chapter["workflow"] = {"version": 1, "stages": {}}
        if not isinstance(chapter.get("execution"), dict):
            chapter["execution"] = {}
        chapter["execution"].setdefault("status", "never_run")
        chapter["execution"].setdefault("last_run_at", "")
        chapter["execution"].setdefault("model", "")
        chapter["execution"].setdefault("audit_score", None)
        chapter["execution"].setdefault("audit_verdict", "")
        chapter["execution"].setdefault("revision_attempts", 0)
        chapter["execution"].setdefault("issues", [])
        chapter["execution"].setdefault("warnings", [])
        if not isinstance(chapter.get("run_history"), list):
            chapter["run_history"] = []
        if not isinstance(chapter.get("plan"), dict):
            chapter["plan"] = {}
        plan = chapter["plan"]
        plan.setdefault("goal", "")
        plan.setdefault("conflict", "")
        plan.setdefault("must_keep", [])
        plan.setdefault("must_avoid", [])
        plan.setdefault("turning_point", "")
        plan.setdefault("ending_hook", "")
        plan.setdefault("chapter_type", "")
        plan.setdefault("pov_character", "")
        plan.setdefault("time_location", "")
        plan.setdefault("opening_beat", "")
        plan.setdefault("scene_beats", [])
        plan.setdefault("emotional_turn", "")
        plan.setdefault("thread_actions", [])
        plan.setdefault("exit_state", "")
        plan.setdefault("ending_type", "")
        if not isinstance(plan.get("scene_beats"), list):
            plan["scene_beats"] = []
        if not isinstance(plan.get("thread_actions"), list):
            plan["thread_actions"] = []
        if isinstance(chapter.get("route"), dict):
            route = chapter["route"]
            route.setdefault("id", str(uuid.uuid4()))
            route.setdefault("number", 1)
            route.setdefault("title", chapter.get("title", ""))
            route.setdefault("goal", "")
            route.setdefault("conflict", "")
            route.setdefault("turning_point", "")
            route.setdefault("ending_hook", "")
            route.setdefault("must_keep", [])
            route.setdefault("must_avoid", [])
            route.setdefault("status", "planned")
    if legacy_author_note and not any(
        str(chapter.get("author_note", "")).strip()
        for chapter in project["chapters"]
    ):
        project["chapters"][0]["author_note"] = legacy_author_note
        project["author_note"] = ""
    for character in project["characters"]:
        character.setdefault("id", str(uuid.uuid4()))
        character.setdefault("name", "")
        character.setdefault("role", "")
        character.setdefault("description", "")
        character.setdefault("goal", "")
        character.setdefault("knowledge", "")
        character.setdefault("secrets", "")
        character.setdefault("voice", "")
        character.setdefault("location", "")
        character.setdefault("items", "")
        character.setdefault("emotion", "")
        character.setdefault("state", "")
        character.setdefault("aliases", [])
        if isinstance(character.get("aliases"), str):
            character["aliases"] = [
                item.strip()
                for item in re.split(r"[,，\n]", character["aliases"])
                if item.strip()
            ]
        character.setdefault("importance", "supporting")
        character.setdefault("active", True)
        character.setdefault("personality", character.get("description", ""))
        character.setdefault("appearance", "")
        character.setdefault("appearance_state", "")
        character.setdefault("values", "")
        character.setdefault("fears", "")
        character.setdefault("contradictions", "")
        character.setdefault("mannerisms", "")
        character.setdefault("relationships", "")
        character.setdefault("arc", "")
        character.setdefault("hard_limits", "")
        character.setdefault("dialogue_examples", [])
        if isinstance(character.get("dialogue_examples"), str):
            character["dialogue_examples"] = [
                item.strip()
                for item in character["dialogue_examples"].splitlines()
                if item.strip()
            ]
        character.setdefault("knowledge_ledger", [])
        if not isinstance(character.get("knowledge_ledger"), list):
            character["knowledge_ledger"] = []
        character["knowledge_ledger"] = [
            item
            for item in character["knowledge_ledger"]
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        ][-200:]
        for knowledge in character["knowledge_ledger"]:
            knowledge.setdefault("id", str(uuid.uuid4()))
            knowledge.setdefault("text", "")
            knowledge.setdefault("learned_how", "")
            knowledge.setdefault("certainty", "confirmed")
            knowledge.setdefault("source_chapter_id", "")
            knowledge.setdefault("source_chapter_title", "")
            knowledge.setdefault("chapter_number", 0)
            knowledge.setdefault("evidence", "")
            knowledge.setdefault("active", True)
            if not isinstance(knowledge.get("related_fact_ids"), list):
                knowledge["related_fact_ids"] = []
        if "knowledge_baseline" not in character:
            ledger_texts = {
                re.sub(r"\s+", "", str(item.get("text", ""))).casefold()
                for item in character["knowledge_ledger"]
                if str(item.get("text", "")).strip()
            }
            baseline_parts = [
                value.strip()
                for value in re.split(r"[；\n]", str(character.get("knowledge", "")))
                if value.strip()
                and re.sub(r"\s+", "", value).casefold() not in ledger_texts
            ]
            character["knowledge_baseline"] = "；".join(baseline_parts)
        character.setdefault("knowledge_baseline_chapter", 0)
    for entry in project["world_entries"]:
        entry.setdefault("id", str(uuid.uuid4()))
        entry.setdefault("title", "")
        keys = entry.get("keys", [])
        if isinstance(keys, str):
            entry["keys"] = [
                item.strip()
                for item in keys.replace("，", ",").split(",")
                if item.strip()
            ]
        elif not isinstance(keys, list):
            entry["keys"] = []
        entry.setdefault("content", "")
        entry.setdefault("position", "after")
        entry.setdefault("order", 100)
        entry.setdefault("constant", False)
        entry.setdefault("enabled", True)
        entry.setdefault("match", "any")
        entry.setdefault("secondary_keys", [])
        if isinstance(entry.get("secondary_keys"), str):
            entry["secondary_keys"] = [
                item.strip()
                for item in re.split(r"[,，\n]", entry["secondary_keys"])
                if item.strip()
            ]
        entry.setdefault("selective_logic", "and_any")
        entry.setdefault("case_sensitive", False)
        entry.setdefault("category", "世界设定")
        entry.setdefault("canon", "hard" if entry.get("constant") else "soft")
        entry.setdefault("character_names", [])
        if isinstance(entry.get("character_names"), str):
            entry["character_names"] = [
                item.strip()
                for item in re.split(r"[,，\n]", entry["character_names"])
                if item.strip()
            ]
        entry.setdefault("chapter_start", 0)
        entry.setdefault("chapter_end", 0)
        entry.setdefault("inclusion_group", "")
        entry.setdefault("non_recursable", False)
        entry.setdefault("prevent_recursion", False)
        entry.setdefault("delay_until_recursion", False)
    ensure_reference_defaults(project)
    ensure_knowledge_defaults(project)
    ensure_fanfic_defaults(project)
    return project

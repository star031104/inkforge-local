"""Read-only, chapter-relative projections. Never persist a context projection."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from typing import Any

from .memory_integrity import derive_story_so_far
from .memory_integrity import chapter_content_hash
from .story_systems import rebuild_narrative_state

DYNAMIC_FIELDS = ("state", "location", "items", "emotion", "appearance_state")


def chapter_context(project: dict[str, Any], index: int) -> dict[str, Any]:
    view = deepcopy(project)
    chapters = view.get("chapters", [])
    number = index + 1
    numbers = {str(c.get("id", "")): i + 1 for i, c in enumerate(chapters)}
    stale_from = int(project.get("memory", {}).get("stale_from_chapter", 0) or 0)

    def available(item: dict[str, Any]) -> bool:
        refs = [item.get(k) for k in ("source_chapter_id", "chapter_id", "last_chapter_id")]
        ref = str(item.get("source_ref", ""))
        if ref.startswith("chapter:"):
            refs.append(ref[8:])
        if any(ref and str(ref) not in numbers for ref in refs):
            return False
        source = max([numbers.get(str(ref), 0) for ref in refs] + [
            int(item.get(k, 0) or 0) for k in ("chapter_number", "last_advanced_chapter", "last_chapter_number")
        ])
        if source >= number or (stale_from and source >= stale_from):
            return False
        start = int(item.get("valid_from_chapter", 0) or 0)
        end = int(item.get("valid_until_chapter", 0) or 0)
        return not ((start and start > number) or (end and end < index) or item.get("stale"))

    memory = view.setdefault("memory", {})
    for key in ("facts", "relationships", "timeline", "plot_threads", "continuity_notes", "description_ledger"):
        memory[key] = [x for x in memory.get(key, []) if isinstance(x, dict) and available(x)]
    for fact in memory["facts"]:
        if not fact.get("active", True) and int(fact.get("valid_until_chapter", 0) or 0) >= max(1, index):
            fact["active"] = True
            # The superseding event has not happened at the beginning of this chapter.
            fact["valid_until_chapter"] = 0
    history = [c for i, c in enumerate(chapters[:index]) if not stale_from or i + 1 < stale_from]
    memory["story_so_far"] = derive_story_so_far({"chapters": history})
    memory.pop("story_digest_candidate", None)
    for key in ("facts", "relations"):
        knowledge = view.setdefault("knowledge", {})
        knowledge[key] = [x for x in knowledge.get(key, []) if isinstance(x, dict) and available(x)]
        selected_ids = {x.get("id") for x in knowledge[key]}
        for item in knowledge[key]:
            if item.get("status") == "superseded" and item.get("superseded_by") and item["superseded_by"] not in selected_ids:
                item["status"] = "confirmed"

    for character in view.get("characters", []):
        character["knowledge_ledger"] = [x for x in character.get("knowledge_ledger", []) if available(x)]
        last = int(character.get("last_state_chapter_number", 0) or 0)
        # Older projects may not have a baseline. Unknown is safer than a future state.
        if last >= number or (stale_from and last >= stale_from):
            baseline = character.get("state_baseline", {})
            for field in DYNAMIC_FIELDS:
                character[field] = baseline.get(field, "")
            for source_chapter in history:
                for delta in source_chapter.get("settlement", {}).get("character_updates", []):
                    if str(delta.get("name", "")).casefold() != str(character.get("name", "")).casefold():
                        continue
                    evidence = delta.get("evidence", "")
                    if isinstance(evidence, list):
                        evidence = next((q if isinstance(q, str) else q.get("quote", "") for q in evidence), "")
                    if not isinstance(evidence, str) or not evidence or evidence not in source_chapter.get("content", ""):
                        continue
                    for field in DYNAMIC_FIELDS:
                        if delta.get(field):
                            character[field] = delta[field]

    state = view.setdefault("narrative_state", {})
    state["events"] = [x for x in state.get("events", []) if isinstance(x, dict) and available(x)]
    rebuild_narrative_state(view)
    for i, chapter in enumerate(chapters):
        if i > index:
            chapter["content"] = ""
        if i >= index or (stale_from and i + 1 >= stale_from):
            chapter["summary"] = ""
            chapter["settlement"] = {}
    view["_context_boundary"] = {"before_chapter": number, "stale_from_chapter": stale_from}
    return view


def invalidate_changed_manuscript(previous: dict[str, Any], current: dict[str, Any]) -> None:
    """Keep prose intact, but prevent stale derived state from becoming authority."""
    old = {str(c.get("id")): c for c in previous.get("chapters", [])}
    changed = []
    current_ids = {str(c.get("id")) for c in current.get("chapters", [])}
    old_chapters = previous.get("chapters", [])
    for i, (before, after) in enumerate(zip(old_chapters, current.get("chapters", [])), 1):
        if before.get("id") != after.get("id"):
            if any(c.get("settlement") or c.get("locked_content_hash") for c in old_chapters[i - 1:]):
                changed.append(i)
            break
    for i, chapter in enumerate(current.get("chapters", []), 1):
        before = old.get(str(chapter.get("id")), {})
        if before.get("content") != chapter.get("content") and (before.get("settlement") or before.get("locked_content_hash")):
            fresh = chapter.get("settlement", {}).get("content_hash") == chapter_content_hash(chapter.get("content", ""))
            changed.append(i + 1 if fresh else i)
    changed.extend(i for i, c in enumerate(previous.get("chapters", []), 1)
                   if str(c.get("id")) not in current_ids and c.get("content"))
    if not changed:
        return
    if min(changed) > len(current.get("chapters", [])):
        return
    memory = current.setdefault("memory", {})
    boundary = min(changed + ([int(memory["stale_from_chapter"])] if memory.get("stale_from_chapter") else []))
    memory["stale_from_chapter"] = boundary
    for chapter in current.get("chapters", [])[boundary - 1:]:
        chapter["memory_stale"] = True
        digest = hashlib.sha256(str(chapter.get("content", "")).strip().encode()).hexdigest()
        if chapter.get("locked_content_hash") != digest:
            chapter["authority_state"] = "state_degraded"
        chapter.setdefault("execution", {})["memory_warning"] = "前文已修改，请按章重新审校和结算记忆"


def prepare_memory_rebuild(project: dict[str, Any], chapter: dict[str, Any]) -> None:
    """Discard derived deltas from the invalidated suffix before replaying it."""
    memory = project.setdefault("memory", {})
    boundary = int(memory.get("stale_from_chapter", 0) or 0)
    if not boundary:
        return
    index = next(i for i, c in enumerate(project["chapters"]) if c["id"] == chapter["id"])
    if index + 1 != boundary:
        raise ValueError(f"请先从第 {boundary} 章按顺序重新结算记忆")
    view = chapter_context(project, index)
    for key in ("facts", "relationships", "timeline", "plot_threads", "continuity_notes", "description_ledger"):
        memory[key] = view["memory"][key]
    for original, projected in zip(project.get("characters", []), view.get("characters", [])):
        for field in DYNAMIC_FIELDS:
            original[field] = projected.get(field, "")
        original["knowledge_ledger"] = projected.get("knowledge_ledger", [])
        original["last_state_chapter_number"] = index
    project["knowledge"] = view["knowledge"]
    project["narrative_state"] = view["narrative_state"]


def finish_memory_rebuild(project: dict[str, Any], chapter: dict[str, Any]) -> None:
    chapter.pop("memory_stale", None)
    chapter.get("execution", {}).pop("memory_warning", None)
    pending = [i for i, c in enumerate(project.get("chapters", []), 1) if c.get("memory_stale")]
    memory = project.setdefault("memory", {})
    if pending:
        memory["stale_from_chapter"] = min(pending)
    else:
        memory.pop("stale_from_chapter", None)

"""Regressions for the memory stage-repair review findings (#1, #2, #3, #8)."""
from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest

from app.editorial_workflow import rebuild_repair_queue
from app.memory_integrity import chapter_content_hash
from app.services.memory_settlement import _apply_director_memory, repair_incomplete_memory
from app.temporal_context import invalidate_changed_manuscript

SENTENCE = "林默推开门，看见桌上那封盖着火漆的信。"
CONTENT = SENTENCE * 12
LATER = "林默把那封信丢进火盆，信很快烧成了灰。" * 12


def _result(failed: list[str], **extra) -> dict:
    base = {
        "summary": "林默看见信。", "story_so_far": "林默看见信。",
        "character_updates": [], "facts": [], "plot_threads": [], "timeline": [],
        "relationship_updates": [], "continuity_notes": [], "description_updates": [],
        "item_updates": [], "injury_updates": [],
        "scene_settlement": {"goal_achieved": "yes", "closing_state": "信在桌上"},
        "failed_stages": failed,
    }
    base.update(extra)
    return base


def _project(chapters: int = 1) -> dict:
    contents = [CONTENT, LATER]
    return {
        "chapters": [
            {"id": f"c{i + 1}", "title": f"第{i + 1}章", "content": contents[i]} for i in range(chapters)
        ],
        "characters": [{"name": "林默"}], "memory": {}, "planning": {"volumes": []}, "settings": {},
    }


def _facts_completion(facts: list[dict]):
    async def fake(settings, messages, **kwargs):
        return {"facts": facts, "description_updates": []}, []

    return fake


THREAD = {"title": "火漆信", "status": "open", "latest": "信出现", "evidence": "看见桌上那封盖着火漆的信"}


# ----------------------------------------------------------------- finding #1

def test_repair_refuses_a_stale_chapter_instead_of_dropping_its_memory():
    project = _project()
    chapter = project["chapters"][0]
    _apply_director_memory(project, chapter, _result(["facts"], plot_threads=[THREAD]))
    before = deepcopy(project)
    project["chapters"][0]["content"] = CONTENT + "新增一段。" * 5
    invalidate_changed_manuscript(before, project)
    assert project["memory"]["stale_from_chapter"] == 1
    snapshot = deepcopy(project)
    fake = _facts_completion([{"text": "信上有火漆", "evidence": "看见桌上那封盖着火漆的信"}])
    with pytest.raises(ValueError, match="完整重新结算"):
        asyncio.run(repair_incomplete_memory(project, "c1", completion=fake))
    # Nothing was discarded or marked as freshly settled.
    assert project == snapshot
    assert [t["title"] for t in project["memory"]["plot_threads"]] == ["火漆信"]
    assert project["chapters"][0]["memory_stale"] is True
    assert project["chapters"][0]["settlement"]["content_hash"] != chapter_content_hash(
        project["chapters"][0]["content"]
    )


def test_repair_refuses_when_an_earlier_chapter_made_memory_stale():
    project = _project(2)
    _apply_director_memory(project, project["chapters"][0], _result([]))
    _apply_director_memory(project, project["chapters"][1], _result(["facts"]))
    project["memory"]["stale_from_chapter"] = 1  # chapter 1 edited, not yet re-settled
    with pytest.raises(ValueError, match="第 1 章起"):
        asyncio.run(repair_incomplete_memory(project, "c2", completion=_facts_completion([])))


def test_repair_of_fresh_chapter_still_works_and_keeps_other_stages():
    project = _project()
    chapter = project["chapters"][0]
    _apply_director_memory(project, chapter, _result(["facts"], plot_threads=[THREAD]))
    fake = _facts_completion([{"text": "信上有火漆", "tags": ["信"], "evidence": "看见桌上那封盖着火漆的信"}])
    report = asyncio.run(repair_incomplete_memory(project, "c1", completion=fake))
    assert report["status"] == "repaired"
    assert [t["title"] for t in project["memory"]["plot_threads"]] == ["火漆信"]
    assert [f["text"] for f in project["memory"]["facts"] if f.get("active")] == ["信上有火漆"]
    assert chapter["settlement"]["content_hash"] == chapter_content_hash(chapter["content"])


# ----------------------------------------------------------------- finding #2

def _open_or_dismissed(project: dict) -> list[str]:
    return [item["status"] for item in project.get("repair_queue", []) if item.get("source") == "memory_repair"]


def test_dismissed_memory_repair_stays_dismissed_across_rebuilds():
    project = _project()
    _apply_director_memory(project, project["chapters"][0], _result(["facts"]))
    for _ in range(3):
        for item in rebuild_repair_queue(project):
            if item.get("source") == "memory_repair" and item["status"] == "queued":
                item["status"] = "dismissed"
    assert _open_or_dismissed(project) == ["dismissed"]


def test_new_failed_stage_set_raises_a_new_item_even_after_dismissal():
    project = _project()
    chapter = project["chapters"][0]
    _apply_director_memory(project, chapter, _result(["facts"]))
    project["repair_queue"][0]["status"] = "dismissed"
    _apply_director_memory(project, chapter, _result(["facts", "threads"]))
    assert sorted(_open_or_dismissed(project)) == ["dismissed", "queued"]


# ----------------------------------------------------------------- finding #3

def test_repairing_an_older_chapter_does_not_revive_facts_later_chapters_touched():
    project = _project(2)
    first, second = project["chapters"]
    _apply_director_memory(project, first, _result(["facts"]))
    _apply_director_memory(project, second, _result([], facts=[
        {"text": "那封信已被烧成灰", "tags": ["信"], "evidence": "信很快烧成了灰"},
    ]))
    fake = _facts_completion([
        {"text": "那封信完好地放在桌上", "tags": ["信"], "evidence": "看见桌上那封盖着火漆的信"},
        {"text": "林默推开了书房的门", "tags": ["书房门"], "evidence": "林默推开门"},
    ])
    report = asyncio.run(repair_incomplete_memory(project, "c1", completion=fake))
    assert report["status"] == "repaired"
    facts = {f["text"]: f for f in project["memory"]["facts"]}
    revived = facts["那封信完好地放在桌上"]
    assert revived["active"] is False and revived["needs_review"] is True
    assert revived["confidence"] == "suspected"
    assert first["settlement"]["review_fact_ids"] == [revived["id"]]
    # A fact about an entity no later chapter touched is settled normally.
    assert facts["林默推开了书房的门"]["active"] is True
    # The later chapter's fact stays the current one.
    assert facts["那封信已被烧成灰"]["active"] is True
    assert any("待复核" in warning for warning in report["warnings"])
    knowledge_text = str(project.get("knowledge", {}))
    assert "那封信完好地放在桌上" not in knowledge_text


# ----------------------------------------------------------------- finding #8

def test_unknown_stage_names_are_not_reported_as_repaired():
    project = _project()
    chapter = project["chapters"][0]
    _apply_director_memory(project, chapter, _result(["legacy_stage"]))
    calls: list[int] = []

    async def fake(settings, messages, **kwargs):
        calls.append(1)
        return {}, []

    with pytest.raises(ValueError, match="legacy_stage"):
        asyncio.run(repair_incomplete_memory(project, "c1", completion=fake))
    assert calls == []
    assert chapter["settlement"]["incomplete_stages"] == ["legacy_stage"]

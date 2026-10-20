"""Partial memory settlement → integrity issue → repair queue → stage-only repair."""
from __future__ import annotations

import asyncio

from app.editorial_workflow import rebuild_repair_queue
from app.manuscript_quality import memory_integrity_issues
from app.story.memory_extraction import build_stage_inputs, extract_staged_memory
from app.services.memory_settlement import _apply_director_memory, repair_incomplete_memory

SENTENCE = "林默推开门，看见桌上那封盖着火漆的信。"
CONTENT = SENTENCE * 12


def _project() -> dict:
    return {
        "chapters": [{"id": "c1", "title": "第一章", "content": CONTENT}],
        "characters": [{"name": "林默"}], "memory": {}, "planning": {"volumes": []},
        "settings": {},
    }


def _partial_result(failed: list[str]) -> dict:
    return {
        "summary": "林默看见信。", "story_so_far": "林默看见信。",
        "character_updates": [], "facts": [], "plot_threads": [], "timeline": [],
        "relationship_updates": [], "continuity_notes": [], "description_updates": [],
        "item_updates": [], "injury_updates": [],
        "scene_settlement": {"goal_achieved": "yes", "closing_state": "信在桌上"},
        "failed_stages": failed,
    }


def _open_memory_items(project: dict) -> list[dict]:
    return [
        item for item in project.get("repair_queue", [])
        if item.get("source") == "memory_repair" and item.get("status") in {"queued", "in_progress"}
    ]


def test_partial_settlement_surfaces_issue_and_enqueues_one_repair():
    project = _project()
    chapter = project["chapters"][0]
    _apply_director_memory(project, chapter, _partial_result(["facts", "threads"]))
    issues = [item for item in memory_integrity_issues(project) if item["category"] == "记忆结算不完整"]
    assert len(issues) == 1 and issues[0]["stages"] == ["facts", "threads"]
    items = _open_memory_items(project)
    assert len(items) == 1
    assert items[0]["category"] == "记忆回灌" and items[0]["memory_stages"] == ["facts", "threads"]
    rebuild_repair_queue(project)  # idempotent
    assert len(_open_memory_items(project)) == 1


def test_locked_accepted_chapter_keeps_memory_repair_item():
    project = _project()
    chapter = project["chapters"][0]
    _apply_director_memory(project, chapter, _partial_result(["facts"]))
    chapter["execution"]["status"] = "accepted"
    chapter["authority_state"] = "locked"
    rebuild_repair_queue(project)
    assert len(_open_memory_items(project)) == 1


def test_extract_only_selected_stages():
    calls: list[str] = []

    async def fake(settings, messages, **kwargs):
        calls.append(messages[-1]["content"].split("】", 1)[0])
        return {}, []

    result = asyncio.run(extract_staged_memory({}, "S", {}, completion=fake, only=["facts"]))
    assert calls == ["【当前阶段：原子事实与描写账本"]
    assert result["failed_stages"] == []


def test_build_stage_inputs_uses_chapter_text_and_prior_state():
    project = _project()
    shared, blocks = build_stage_inputs(project, 0, CONTENT)
    assert SENTENCE in shared and "第 1 章" in shared
    assert set(blocks) == {"story_so_far", "characters", "facts", "plot_threads", "relationships", "objects"}


def test_repair_reruns_only_failed_stages_and_clears_flags():
    project = _project()
    chapter = project["chapters"][0]
    _apply_director_memory(project, chapter, _partial_result(["facts"]))
    assert chapter["settlement"]["content_hash"] == ""
    before = dict(chapter["settlement"])
    calls: list[str] = []

    async def fake(settings, messages, **kwargs):
        instruction = messages[-1]["content"]
        calls.append(instruction)
        assert "原子事实" in instruction
        return {"facts": [{"text": "桌上有一封盖火漆的信", "tags": ["林默"], "importance": 3,
                           "confidence": "confirmed", "visibility": "objective",
                           "evidence": SENTENCE}]}, []

    report = asyncio.run(repair_incomplete_memory(project, "c1", completion=fake))
    assert report["status"] == "repaired" and report["stages"] == ["facts"] and report["remaining"] == []
    assert len(calls) == 1
    settlement = chapter["settlement"]
    assert settlement["content_hash"]
    assert settlement["incomplete_stages"] == []
    assert "memory_needs_repair" not in chapter["execution"]
    # Core fields settled by the first pass are preserved.
    assert settlement["goal_achieved"] == before["goal_achieved"] == "yes"
    assert settlement["closing_state"] == "信在桌上"
    assert chapter["summary"] == "林默看见信。"
    assert any("火漆" in str(item.get("text", "")) for item in project["memory"].get("facts", []))
    assert _open_memory_items(project) == []
    assert not [item for item in memory_integrity_issues(project) if item["category"] == "记忆结算不完整"]


def test_repair_that_fails_again_keeps_flags_and_queue_item():
    project = _project()
    chapter = project["chapters"][0]
    _apply_director_memory(project, chapter, _partial_result(["facts", "threads"]))

    async def fake(settings, messages, **kwargs):
        if "伏笔" in messages[-1]["content"]:
            raise ValueError("模型返回的 JSON 未闭合")
        return {"facts": []}, []

    report = asyncio.run(repair_incomplete_memory(project, "c1", completion=fake))
    assert report["status"] == "partial" and report["remaining"] == ["threads"]
    assert chapter["settlement"]["incomplete_stages"] == ["threads"]
    assert chapter["settlement"]["content_hash"] == ""
    items = _open_memory_items(project)
    assert len(items) == 1 and items[0]["memory_stages"] == ["threads"]


def test_repair_noop_for_complete_chapter():
    project = _project()
    chapter = project["chapters"][0]
    _apply_director_memory(project, chapter, _partial_result([]))

    async def never(*args, **kwargs):
        raise AssertionError("no model call expected")

    report = asyncio.run(repair_incomplete_memory(project, "c1", completion=never))
    assert report["status"] == "noop"

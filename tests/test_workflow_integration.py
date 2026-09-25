from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

import app.main as main
import app.services.director_runtime as director_runtime
from app.db import ProjectStore


def _planned_project(store: ProjectStore) -> dict:
    project = store.create("完整创作闭环")
    project["settings"]["target_words"] = 300
    project["planning"] = {
        "master": {"theme": "记忆与选择"},
        "volumes": [
            {
                "id": "volume-1",
                "number": 1,
                "title": "雨夜来信",
                "chapter_start": 1,
                "chapter_end": 1,
                "goal": "主角确认旧信来自失踪者",
                "ending_state": "主角决定追查寄信地点",
                "chapters": [
                    {
                        "id": "route-1",
                        "number": 1,
                        "title": "没有寄件人的信",
                        "goal": "在雨夜确认旧信并非伪造",
                        "conflict": "档案记录否认寄信人的存在",
                        "turning_point": "纸张夹层出现当天潮汐表",
                        "ending_hook": "潮汐表背面写着废弃码头编号",
                        "must_keep": [],
                        "must_avoid": [],
                    }
                ],
            }
        ],
    }
    return store.save(project["id"], project, reason="fixture")


def test_planned_writing_resumes_from_draft_checkpoint_and_settles_memory(
    monkeypatch, tmp_path
):
    store = ProjectStore(tmp_path / "workflow.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_launch_director", lambda _task_id: None)
    project = _planned_project(store)

    async def fake_plan(body):
        return {
            "goal": "确认旧信来源",
            "beats": ["检查纸张", "对照档案", "发现潮汐表"],
            "ending_state": "决定前往废弃码头",
        }

    audit_calls = {"count": 0}

    async def interrupted_audit(body):
        audit_calls["count"] += 1
        raise ValueError("模拟审计服务中断")

    async def passing_audit(body):
        audit_calls["count"] += 1
        return {
            "score": 96,
            "verdict": "pass",
            "requires_review": False,
            "issues": [],
            "revision_brief": "",
        }

    async def fake_memory(body):
        return {
            "summary": "许澄确认旧信与潮汐表有关，并决定前往废弃码头。",
            "story_so_far": "许澄收到旧信，查到废弃码头编号。",
            "character_updates": [],
            "facts": [],
            "plot_threads": [],
            "timeline": [],
            "relationship_updates": [],
            "continuity_notes": [],
            "description_updates": [],
            "scene_settlement": {
                "goal_achieved": "确认线索",
                "irreversible_changes": ["许澄决定前往码头"],
                "open_questions": ["寄信人是谁"],
                "closing_state": "许澄收起潮汐表出门",
            },
        }

    prose_calls = {"count": 0}
    prose = "\n\n".join(
        f"雨水沿着第{i}扇旧窗滑下，许澄核对纸纹、墨迹和日期，线索因此向码头推进。"
        for i in range(1, 36)
    ) + "她把潮汐表折好，推门走进雨里。"

    async def fake_stream(*_args, **_kwargs):
        prose_calls["count"] += 1
        yield prose

    monkeypatch.setattr(main, "chapter_plan", fake_plan)
    monkeypatch.setattr(main, "chapter_audit", interrupted_audit)
    monkeypatch.setattr(main, "chapter_memory", fake_memory)
    monkeypatch.setattr(director_runtime, "chat_stream", fake_stream)
    monkeypatch.setattr(
        director_runtime, "_director_candidate_gate_failures", lambda *_args: []
    )

    with TestClient(main.app) as client:
        response = client.post(
            f"/api/director/projects/{project['id']}/write-planned",
            json={
                "quality_threshold": 50,
                "max_revision_attempts": 0,
                "continue_on_quality_debt": False,
            },
        )
    assert response.status_code == 200
    task_id = response.json()["task"]["id"]

    asyncio.run(main._run_auto_director(task_id))
    interrupted = store.get_director_task(task_id)
    assert interrupted["status"] == "paused"
    assert interrupted["chapter_draft_checkpoint"]["draft"] == prose
    assert store.get(project["id"])["chapters"][0]["content"] == ""

    monkeypatch.setattr(main, "chapter_audit", passing_audit)
    asyncio.run(main._run_auto_director(task_id))

    completed = store.get_director_task(task_id)
    written = ProjectStore(store.path).get(project["id"])
    chapter = written["chapters"][0]
    assert completed["status"] == "completed"
    assert completed["phase"] == "completed"
    assert prose_calls["count"] == 1
    assert audit_calls["count"] == 2
    assert chapter["content"] == prose
    assert chapter["authority_state"] == "locked"
    assert chapter["settlement"]["summary"].startswith("许澄确认旧信")
    assert chapter["execution"]["status"] == "accepted"

import asyncio

from fastapi.testclient import TestClient

import app.main as main
from app.db import ProjectStore


def _core(title: str) -> dict:
    return {
        "title": title,
        "genre": "悬疑",
        "positioning": "可持续的类型故事",
        "premise": "一座城市每天都会遗忘一个人",
        "reader_promise": "逐层揭开遗忘机制",
        "central_question": "谁决定一个人是否被记住",
        "central_conflict": "记录者与遗忘机制对抗",
        "story_engine": "每章调查一名被遗忘者",
        "outline": "完整阶段推进、人物选择和因果转折。" * 40,
        "author_intent": "讨论记忆与存在",
        "current_focus": "调查第一次集体遗忘",
        "book_rules": [f"规则{i}" for i in range(6)],
        "ending_direction": "主角公开全部记录",
        "tone": "冷峻克制",
        "pov": "third_limited",
        "target_chapters": 30,
        "opening_hook": "档案里出现一个无人认识的名字",
        "first_arc": "确认遗忘并非自然现象",
    }


def test_incubation_request_survives_reopen(monkeypatch, tmp_path):
    store = ProjectStore(tmp_path / "incubation.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_launch_director", lambda _task_id: None)
    project = store.create("孵化恢复")

    with TestClient(main.app) as client:
        started = client.post(
            "/api/incubator/start",
            json={
                "project": project,
                "seed": "一座城市每天都会遗忘一个人",
                "preferences": "保持悬疑感",
                "story_mode": "long",
                "target_chapters": 30,
            },
        )
        assert started.status_code == 200
        task = started.json()["task"]

        reopened = client.get(
            f"/api/director/projects/{project['id']}/latest"
        ).json()

    assert reopened["id"] == task["id"]
    assert reopened["config"]["seed"] == "一座城市每天都会遗忘一个人"
    assert reopened["status"] == "paused"


def test_incubation_runner_checkpoints_and_persists_options(
    monkeypatch, tmp_path
):
    store = ProjectStore(tmp_path / "incubation-runner.db")
    monkeypatch.setattr(main, "store", store)
    project = store.create("后台孵化")
    task = store.create_director_task(
        project["id"],
        {
            "task_type": "incubation",
            "phase": "incubation",
            "incubation_step": "queued",
            "message": "等待启动",
            "events": [],
            "warnings": [],
            "incubation_core_options": [],
            "incubation_options": [],
            "config": {
                "seed": "一座城市每天都会遗忘一个人",
                "preferences": "保持悬疑感",
                "story_mode": "long",
                "target_chapters": 30,
            },
        },
    )

    async def fake_core(*_args, **_kwargs):
        return [_core("方案甲"), _core("方案乙")], []

    async def fake_assets(_project, raw_option, **_kwargs):
        option = dict(raw_option)
        option["characters"] = [{"name": f"人物{i}"} for i in range(3)]
        option["world_entries"] = []
        return option, []

    monkeypatch.setattr(main, "_generate_incubator_core", fake_core)
    monkeypatch.setattr(main, "_complete_incubator_option", fake_assets)

    asyncio.run(main._run_auto_director(task["id"]))
    completed = store.get_director_task(task["id"])

    assert completed["status"] == "completed"
    assert completed["incubation_step"] == "completed"
    assert [item["title"] for item in completed["result"]["options"]] == [
        "方案甲",
        "方案乙",
    ]
    assert any("第 1 套完整方案已保存" in item["message"] for item in completed["events"])


def test_planned_routes_can_start_continuous_writing(monkeypatch, tmp_path):
    store = ProjectStore(tmp_path / "planned-writing.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_launch_director", lambda _task_id: None)
    project = store.create("从规划继续写")
    project["planning"] = {
        "master": {"theme": "记忆与身份"},
        "volumes": [
            {
                "id": "volume-1",
                "number": 1,
                "title": "海堤上的决裂",
                "chapter_start": 1,
                "chapter_end": 2,
                "chapters": [
                    {
                        "id": "route-1",
                        "number": 1,
                        "title": "被遗忘的照片",
                        "goal": "发现照片异常",
                        "conflict": "家人否认照片中的人",
                        "turning_point": "照片开始褪色",
                        "ending_hook": "第二张照片出现",
                        "must_keep": [],
                        "must_avoid": [],
                    },
                    {
                        "id": "route-2",
                        "number": 2,
                        "title": "无人承认的名字",
                        "goal": "确认遗忘范围",
                        "conflict": "记录与记忆互相矛盾",
                        "turning_point": "主角也出现记忆缺口",
                        "ending_hook": "海堤广播念出名字",
                        "must_keep": [],
                        "must_avoid": [],
                    },
                ],
            }
        ],
    }
    project = store.save(project["id"], project, reason="fixture")

    with TestClient(main.app) as client:
        response = client.post(
            f"/api/director/projects/{project['id']}/write-planned",
            json={
                "quality_threshold": 82,
                "max_revision_attempts": 2,
                "continue_on_quality_debt": True,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["task"]["task_type"] == "planned_production"
    assert payload["task"]["phase"] == "chapters"
    assert payload["task"]["total_chapters"] == 2
    assert [chapter["route_id"] for chapter in payload["project"]["chapters"]] == [
        "route-1",
        "route-2",
    ]

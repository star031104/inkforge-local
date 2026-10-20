"""Round 3: director-runtime integration — candidate checkpoint resume, rolling
re-plan save-conflict replay, hook ledger settlement and risk exposure."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.services.director_runtime as director_runtime
from app.storage.project_store import ProjectStore
from app.services import rolling_replan as replan_module
from tests.test_workflow_integration import _planned_project

BODY = "\n\n".join(
    f"雨水沿着第{i}扇旧窗滑下，许澄核对纸纹、墨迹和日期，线索因此向码头推进。" for i in range(1, 36)
)


def _three_chapter_project(store: ProjectStore) -> dict:
    project = store.create("三章导演")
    project["settings"]["target_words"] = 300
    routes = [
        {
            "id": f"route-{n}", "number": n, "title": f"第{n}章标题",
            "goal": f"原目标{n}", "conflict": f"原阻力{n}", "turning_point": f"原转折{n}",
            "ending_hook": f"潮汐表背面写着码头编号{n}", "must_keep": [], "must_avoid": [],
        }
        for n in (1, 2, 3)
    ]
    project["planning"] = {
        "master": {"theme": "记忆与选择"},
        "volumes": [{
            "id": "volume-1", "number": 1, "title": "雨夜来信", "chapter_start": 1, "chapter_end": 3,
            "goal": "主角确认旧信来自失踪者", "ending_state": "主角决定追查寄信地点", "chapters": routes,
        }],
    }
    return store.save(project["id"], project, reason="fixture")


def _install_fakes(monkeypatch, stream_texts, structured):
    async def fake_stream(*_args, **kwargs):
        yield stream_texts()
        kwargs["outcome"].observe({"choices": [{"finish_reason": "stop"}]})

    async def fake_plan(body):
        return {"goal": "确认旧信来源", "beats": ["检查纸张"], "ending_state": "决定前往废弃码头"}

    async def passing_audit(body):
        return {
            "score": 96, "verdict": "pass", "requires_review": False, "issues": [], "revision_brief": "",
            "risk": {"score": 10, "level": "low", "reasons": ["首次审计（+10）"], "budget_used": "merged", "calls": 1},
            "hook_status": [],
        }

    async def fake_memory(body):
        return {"summary": "许澄决定前往码头。", "story_so_far": "许澄查到码头。", "character_updates": [],
                "facts": [], "plot_threads": [], "timeline": [], "relationship_updates": [],
                "continuity_notes": [], "description_updates": [], "scene_settlement": {}}

    monkeypatch.setattr(main, "chapter_plan", fake_plan)
    monkeypatch.setattr(main, "chapter_audit", passing_audit)
    monkeypatch.setattr(main, "chapter_memory", fake_memory)
    monkeypatch.setattr(director_runtime, "chat_stream", fake_stream)
    monkeypatch.setattr(director_runtime, "run_structured_completion", structured)
    monkeypatch.setattr(director_runtime, "_director_candidate_gate_failures", lambda *_args: [])


def _start(store, project) -> str:
    with TestClient(main.app) as client:
        response = client.post(
            f"/api/director/projects/{project['id']}/write-planned",
            json={"quality_threshold": 50, "max_revision_attempts": 0, "continue_on_quality_debt": True},
        )
    assert response.status_code == 200, response.text
    return response.json()["task"]["id"]


@pytest.fixture()
def store(monkeypatch, tmp_path):
    store = ProjectStore(tmp_path / "round3.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_launch_director", lambda _task_id: None)
    return store


def test_resume_reuses_finished_candidate_selection_without_regenerating(monkeypatch, store):
    project = _planned_project(store)
    project["settings"].update(candidate_count=2, rolling_replan=False, dialogue_rehearsal="off")
    project = store.save(project["id"], project, reason="settings")
    calls = {"stream": 0}

    def never():
        calls["stream"] += 1
        raise AssertionError("candidate drafts must not be regenerated on resume")

    async def structured(settings, messages, **kwargs):
        raise AssertionError("no judge call expected when a selection exists")

    _install_fakes(monkeypatch, never, structured)
    task_id = _start(store, project)
    chapter_id = store.get(project["id"])["chapters"][0]["id"]
    task = store.get_director_task(task_id)
    first, second = "灯芯爆了一下。" + BODY + "她推门走进雨里。", "她深吸一口气。" + BODY + "她推门走进雨里。"
    task["chapter_candidates"] = {"chapter_id": chapter_id, "drafts": [first, second], "selected": 0}
    store.save_director_task(task_id, task)

    asyncio.run(main._run_auto_director(task_id))

    task = store.get_director_task(task_id)
    assert task["status"] == "completed", task.get("message")
    assert calls["stream"] == 0
    assert store.get(project["id"])["chapters"][0]["content"].startswith("灯芯爆了一下。")
    assert any("沿用已比较完成" in event.get("message", "") for event in task["events"])


def test_resume_generates_only_missing_candidates(monkeypatch, store):
    project = _planned_project(store)
    project["settings"].update(candidate_count=2, rolling_replan=False, dialogue_rehearsal="off")
    project = store.save(project["id"], project, reason="settings")
    generated = []

    def new_candidate():
        generated.append(1)
        return "灯芯爆了一下。" + BODY + "她把潮汐表折好，推门走进雨里。"

    async def structured(settings, messages, **kwargs):
        user = messages[-1]["content"]
        if "【候选A】" in user:
            a = user.split("【候选A】")[1].split("【候选B】")[0]
            return {"winner": "A" if "灯芯" in a else "B", "reasons": ["开场更具体"]}, []
        return {"rewrites": []}, []

    _install_fakes(monkeypatch, new_candidate, structured)
    task_id = _start(store, project)
    chapter_id = store.get(project["id"])["chapters"][0]["id"]
    task = store.get_director_task(task_id)
    task["chapter_candidates"] = {
        "chapter_id": chapter_id, "drafts": ["她深吸一口气。" + BODY + "她推门走进雨里。"],
    }
    store.save_director_task(task_id, task)

    asyncio.run(main._run_auto_director(task_id))

    task = store.get_director_task(task_id)
    assert task["status"] == "completed", task.get("message")
    assert len(generated) == 1
    assert len(task["chapter_candidates"]["drafts"]) == 2
    assert task["chapter_candidates"]["selected"] == 1


def test_rolling_replan_conflict_replays_only_untouched_fields(monkeypatch, store):
    project = _three_chapter_project(store)
    project["settings"].update(
        candidate_count=1, rolling_replan=True, rolling_replan_window=2, dialogue_rehearsal="off",
        antislop_enabled=False, hook_ledger=True,
    )
    project = store.save(project["id"], project, reason="settings")
    monkeypatch.setattr(replan_module, "_audit_route_checkpoint_prefix", lambda *_a: (3, ""))
    replans = {"count": 0}

    def draft():
        return "她推开门。" + BODY + "潮汐表背面写着码头编号，她听见门外的脚步声？"

    async def structured(settings, messages, **kwargs):
        if "分卷编辑" in messages[0]["content"]:
            replans["count"] += 1
            if replans["count"] == 1:
                # The author edits chapter 3's goal while the planner is thinking.
                latest = store.get(project["id"])
                for chapter in latest["chapters"]:
                    if chapter.get("route_id") == "route-3":
                        chapter["route"]["goal"] = "作者亲改的目标"
                for route in latest["planning"]["volumes"][0]["chapters"]:
                    if route["id"] == "route-3":
                        route["goal"] = "作者亲改的目标"
                store.save(project["id"], latest, reason="author-edit")
                return {"adjustments": [
                    {"number": 2, "changes": {"goal": "追查码头编号的来历"}, "reason": "承接"},
                    {"number": 3, "changes": {"goal": "模型想改的目标"}, "reason": "承接"},
                ]}, []
            return {"adjustments": []}, []
        return {}, []

    _install_fakes(monkeypatch, draft, structured)
    task_id = _start(store, project)
    asyncio.run(main._run_auto_director(task_id))

    task = store.get_director_task(task_id)
    saved = store.get(project["id"])
    assert task["status"] == "completed", task.get("message")
    goals = {chapter["route_id"]: chapter["route"]["goal"] for chapter in saved["chapters"]}
    assert goals["route-2"] == "追查码头编号的来历"
    assert goals["route-3"] == "作者亲改的目标"
    history = task["replan_history"][0]["changes"]
    assert [item["chapter"] for item in history] == [2]
    # Hook ledger: every accepted chapter recorded its planned ending hook.
    ledger = saved["memory"]["hook_ledger"]
    assert sorted(item["origin_chapter"] for item in ledger) == [1, 2, 3]
    assert "hook_debts" in task
    assert task["last_critic_risk"]["budget_used"] == "merged"

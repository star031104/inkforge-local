"""Round 3 integration: experience bank wiring, memory-stage repair, telemetry
budget tag, director craft overrides and test-suite data isolation."""
from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

import app.main as main
import app.services.director_runtime as director_runtime
from app.api.routers import editorial as editorial_module
from app.api.schemas import ChapterActionRequest
from app.storage.project_store import ProjectStore
from app.services import focused_critics
from app.services.director_runtime import _craft_overrides, experience_summary_rows
from app.services.experience_bank import experience_hints, record_audit_issues
from tests.test_p1_craft import project_fixture
from tests.test_round3_runtime_director import BODY, _install_fakes, _start
from tests.test_workflow_integration import _planned_project

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def store(monkeypatch, tmp_path):
    store = ProjectStore(tmp_path / "integration.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_launch_director", lambda _task_id: None)
    return store


# ---------------------------------------------------------------- experience bank

def test_experience_summary_rows_match_ui_shape():
    project: dict = {}
    issue = {"category": "视角与语气", "checker": "pov", "severity": "medium", "source": "local", "message": "越界写了他人内心"}
    for chapter in ("c1", "c2"):
        record_audit_issues(project, "m", [issue], chapter_id=chapter)
    from app.services.experience_bank import experience_summary

    rows = experience_summary_rows(experience_summary(project, "m"))
    assert rows and set(rows[0]) == {"category", "count", "hint"}
    assert rows[0]["count"] == 2 and rows[0]["hint"] and not rows[0]["hint"].startswith("【")


def test_director_records_experience_and_injects_hints(monkeypatch, store):
    project = _planned_project(store)
    project["settings"].update(candidate_count=1, rolling_replan=False, dialogue_rehearsal="off", antislop_enabled=False)
    # Seed the bank so the writer already has a lesson for this model.
    model = str(project["settings"].get("model", ""))
    lesson = {"category": "视角与语气", "checker": "pov", "severity": "high", "source": "local", "message": "越界"}
    record_audit_issues(project, model, [lesson], chapter_id="old-1")
    record_audit_issues(project, model, [lesson], chapter_id="old-2")
    assert experience_hints(project, model)
    project = store.save(project["id"], project, reason="seed")
    prompts: list[str] = []

    async def fake_stream(settings, messages, **kwargs):
        prompts.append("\n".join(item["content"] for item in messages if item["role"] == "user"))
        yield "她推开门。" + BODY + "门外传来脚步声？"
        kwargs["outcome"].observe({"choices": [{"finish_reason": "stop"}]})

    async def structured(settings, messages, **kwargs):
        return {}, []

    _install_fakes(monkeypatch, lambda: "", structured)
    monkeypatch.setattr(director_runtime, "chat_stream", fake_stream)

    async def audit_with_local_issue(body):
        return {
            "score": 96, "verdict": "pass", "requires_review": False, "revision_brief": "",
            "issues": [{"category": "事实细节", "checker": "facts", "severity": "medium", "evidence_verified": True, "message": "称谓不一"}],
            "local_checks": {"issues": [{"category": "重复与套话", "severity": "low", "source": "local", "message": "句式重复"}]},
        }

    monkeypatch.setattr(main, "chapter_audit", audit_with_local_issue)
    task_id = _start(store, project)
    asyncio.run(main._run_auto_director(task_id))

    task = store.get_director_task(task_id)
    assert task["status"] == "completed", task.get("message")
    assert prompts and "跨书审校经验提示" in prompts[0]
    categories = {row["category"] for row in task["experience_summary"]}
    assert len(categories) >= 2
    bank = store.get(project["id"])["memory"]["experience_bank"]
    assert bank  # persisted with the accepted chapter


def test_manual_audit_returns_experience_delta(tmp_path, monkeypatch):
    project = project_fixture(tmp_path, 5)
    chapter = project["chapters"][2]

    async def fake(settings, messages, **kwargs):
        return {"issues": []}, []

    monkeypatch.setattr(main, "run_structured_completion", fake)
    draft = "许澄推开门，雨声灌进来，她把旧信摊在灯下，逐字核对墨迹与日期。" * 12 + "门外传来脚步声？"
    result = asyncio.run(main.chapter_audit(ChapterActionRequest(project=project, chapter_id=chapter["id"], draft=draft)))
    assert "experience_bank" not in result  # only this audit's delta goes back
    assert isinstance(result.get("experience_delta"), dict) and result["experience_delta"]["chapter_id"] == chapter["id"]


# ---------------------------------------------------------------- memory repair

def test_memory_repair_route_status_codes(tmp_path, monkeypatch):
    project = project_fixture(tmp_path, 2)

    async def missing(*_args, **_kwargs):
        raise KeyError("x")

    async def stale(*_args, **_kwargs):
        raise ValueError("正文已变化")

    async def ok(project, chapter_id, **_kwargs):
        return {"status": "repaired", "stages": ["relations"], "remaining": [], "warnings": []}

    from fastapi.testclient import TestClient

    payload = {"project": project, "chapter_id": "c2"}
    with TestClient(main.app) as client:
        for fake, code in ((missing, 404), (stale, 409)):
            monkeypatch.setattr(editorial_module, "repair_incomplete_memory", fake)
            assert client.post("/api/chapter/memory/repair", json=payload).status_code == code
        monkeypatch.setattr(editorial_module, "repair_incomplete_memory", ok)
        response = client.post("/api/chapter/memory/repair", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["report"]["status"] == "repaired" and response.json()["persisted"] is False


@pytest.mark.parametrize("fails", [False, True])
def test_director_repairs_incomplete_memory_once_and_never_crashes(monkeypatch, store, fails):
    project = _planned_project(store)
    project["settings"].update(candidate_count=1, rolling_replan=False, dialogue_rehearsal="off", antislop_enabled=False)
    project = store.save(project["id"], project, reason="settings")
    calls = []

    async def structured(settings, messages, **kwargs):
        return {}, []

    _install_fakes(monkeypatch, lambda: "她推开门。" + BODY + "门外传来脚步声？", structured)

    async def partial_memory(body):
        return {"summary": "许澄决定前往码头。", "story_so_far": "", "character_updates": [], "facts": [],
                "plot_threads": [], "timeline": [], "relationship_updates": [], "continuity_notes": [],
                "description_updates": [], "scene_settlement": {}, "failed_stages": ["relations"]}

    async def repair(project, chapter_id, **kwargs):
        calls.append(chapter_id)
        if fails:
            raise RuntimeError("模型超时")
        chapter = next(item for item in project["chapters"] if item["id"] == chapter_id)
        chapter["settlement"]["incomplete_stages"] = []
        return {"status": "repaired", "stages": ["relations"], "remaining": [], "warnings": []}

    monkeypatch.setattr(main, "chapter_memory", partial_memory)
    monkeypatch.setattr(director_runtime, "repair_incomplete_memory", repair)
    task_id = _start(store, project)
    asyncio.run(main._run_auto_director(task_id))

    task = store.get_director_task(task_id)
    messages = [event.get("message", "") for event in task["events"]]
    assert task["status"] == "completed" and len(calls) == 1
    chapter = store.get(project["id"])["chapters"][0]
    if fails:
        assert any("记忆阶段补提取失败" in item for item in messages)
        assert chapter["settlement"]["incomplete_stages"] == ["relations"]
    else:
        assert any("已补齐记忆阶段" in item for item in messages)
        assert chapter["settlement"]["incomplete_stages"] == []


# ---------------------------------------------------------------- telemetry / overrides

def test_critic_calls_carry_budget_tag_for_telemetry():
    seen: list[str] = []

    async def completion(settings, messages, **kwargs):
        seen.append(settings.get("_critic_budget"))
        return {"issues": []}, []

    focused_critics._last_runs.clear()
    blocks = {key: "x" for key in focused_critics.BLOCK_LABELS}
    asyncio.run(focused_critics.run_focused_critics({}, blocks, "甲。" * 50, completion=completion, budget="merged"))
    asyncio.run(focused_critics.run_focused_critics({}, blocks, "甲。" * 50, completion=completion, budget="full"))
    assert seen[0] == "merged" and set(seen[1:]) == {"full"}

    from app.llm.telemetry import Call

    call = Call({"_critic_budget": "merged", "_workload": "critic"}, [{"content": "x"}])
    try:
        assert call.data["critic_budget"] == "merged"
    finally:
        call.finish()


def test_task_config_overrides_critic_budget(tmp_path, monkeypatch):
    assert _craft_overrides({"config": {"critic_budget": "full", "other": 1}}) == {"critic_budget": "full"}
    project = project_fixture(tmp_path, 5)
    chapter = project["chapters"][2]
    calls: list[str] = []

    async def fake(settings, messages, **kwargs):
        calls.append("merged" if "合并审计" in messages[-1]["content"] else "focused")
        return {"issues": []}, []

    monkeypatch.setattr(main, "run_structured_completion", fake)
    draft = "许澄推开门，雨声灌进来，她把旧信摊在灯下，逐字核对墨迹与日期。" * 12 + "门外传来脚步声？"
    result = asyncio.run(main.chapter_audit(ChapterActionRequest(
        project=project, chapter_id=chapter["id"], draft=draft, config={"critic_budget": "full"},
    )))
    assert result["risk"]["setting"] == "full" and "merged" not in calls


# ---------------------------------------------------------------- data isolation

def test_test_suite_paths_never_point_into_repo_data():
    from app.core.config import settings

    data = (ROOT / "data").resolve()
    for path in (settings.database_path, settings.secret_path, settings.telemetry_path, settings.backup_path):
        assert data not in Path(path).resolve().parents and Path(path).resolve() != data

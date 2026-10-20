"""Director package split: public surface, per-chapter usage attribution and
the compact scene prompt's debate / story-date context."""
from __future__ import annotations

import asyncio

import pytest

import app.main as main
from app.api import container
import app.director.service as director_runtime
from app.llm import telemetry as model_telemetry
from app.storage.project_store import ProjectStore
from tests.test_round3_runtime_director import BODY, _install_fakes, _start, _three_chapter_project
from tests.test_workflow_integration import _planned_project

DEBATE = {
    "topic": "是否开仓赈济",
    "venue_and_judge": "县衙正堂，知县裁决",
    "opponent_strongest_arguments": ["仓粮是军需，擅开即死罪"],
    "outcome_and_cost": "开仓得准，主角担保下狱",
}


@pytest.fixture()
def store(monkeypatch, tmp_path):
    store = ProjectStore(tmp_path / "split.db")
    monkeypatch.setattr(container, "store", store)
    monkeypatch.setattr(container, "_launch_director", lambda _task_id: None)
    return store


def test_public_surface_is_reexported():
    for name in (
        "DirectorDependencies", "DirectorRuntime", "DirectorControlError", "TaskStoppedError",
        "create_director_runtime", "run_while_running", "_craft_overrides",
        "experience_summary_rows", "_known_audit_score", "_director_memory_timeout",
        "_MEMORY_STAGES", "chat_stream", "run_structured_completion",
        "_director_candidate_gate_failures", "repair_incomplete_memory",
    ):
        assert hasattr(director_runtime, name), name


def test_each_chapter_is_attributed_to_its_own_usage_scope(monkeypatch, store):
    project = _three_chapter_project(store)
    project["settings"].update(rolling_replan=False, dialogue_rehearsal="off")
    project = store.save(project["id"], project, reason="settings")
    stream_scopes: list[tuple[str, str]] = []
    audit_scopes: list[tuple[str, tuple[str, str]]] = []

    def text():
        stream_scopes.append(model_telemetry.current_scope())
        return "灯芯爆了一下。" + BODY + "她推门走进雨里。"

    async def structured(settings, messages, **kwargs):
        return {"rewrites": []}, []

    _install_fakes(monkeypatch, text, structured)

    async def scoped_audit(body):
        audit_scopes.append((body.chapter_id, model_telemetry.current_scope()))
        return {"score": 96, "verdict": "pass", "requires_review": False, "issues": [],
                "revision_brief": "", "hook_status": []}

    monkeypatch.setattr(container, "chapter_audit", scoped_audit)
    task_id = _start(store, project)
    asyncio.run(container._run_auto_director(task_id))

    assert store.get_director_task(task_id)["status"] == "completed"
    chapter_ids = [item["id"] for item in store.get(project["id"])["chapters"]]
    assert stream_scopes == [(project["id"], cid) for cid in chapter_ids]
    assert audit_scopes == [(cid, (project["id"], cid)) for cid in chapter_ids]


def test_compact_scene_prompt_includes_debate_and_story_date(monkeypatch, store):
    project = _planned_project(store)
    project["settings"].update(
        director_scene_generation=True, rolling_replan=False, dialogue_rehearsal="off",
    )
    project = store.save(project["id"], project, reason="settings")
    captured: list[str] = []

    def text():
        raise AssertionError("stream replaced below")

    async def structured(settings, messages, **kwargs):
        return {"rewrites": []}, []

    _install_fakes(monkeypatch, text, structured)

    async def plan_with_debate(body):
        return {"goal": "确认旧信来源", "ending_hook": "潮汐表背面写着废弃码头编号",
                "debate": DEBATE}

    async def capturing_stream(_settings, messages, *_args, **_kwargs):
        captured.append(messages[-1]["content"])
        raise ValueError("stop after the first scene prompt")
        yield ""  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(container, "chapter_plan", plan_with_debate)
    monkeypatch.setattr(director_runtime, "chat_stream", capturing_stream)
    task_id = _start(store, project)
    saved = store.get(project["id"])
    saved["chapters"][0]["story_date"] = "万历二十年三月"
    store.save(project["id"], saved, reason="date")

    asyncio.run(container._run_auto_director(task_id))

    assert captured, store.get_director_task(task_id).get("message")
    prompt = captured[0]
    assert "故事内时间：万历二十年三月" in prompt
    assert "交锋结构：" in prompt and "仓粮是军需，擅开即死罪" in prompt
    # the date's own numbers are authorised for the scene
    assert "二十年" in prompt.split("本段允许使用的明确数量只有：", 1)[1].split("；", 1)[0]

from copy import deepcopy
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.db import ProjectStore
from app.prompts import build_prompt
from app.writing_workspace import locate_scene, save_scene, text_diff
from app.temporal_context import prepare_memory_rebuild, finish_memory_rebuild
from app.memory_integrity import begin_memory_commit, mark_memory_commit
from app import model_telemetry


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "test.db")
    monkeypatch.setattr(main, "store", store)
    project = store.create("场景测试")
    project["chapters"][0]["content"] = "🌙雨停了。\n\n她把信放在桌上。"
    project = store.save(project["id"], project)
    with TestClient(main.app) as client:
        yield client, project, store


def test_scene_routes_guard_versions_and_leave_prose_intact(workspace):
    client, p, store = workspace
    chapter = p["chapters"][0]
    url = f"/api/projects/{p['id']}/workspace/scene"
    data = {"expected_updated_at": p["updated_at"], "chapter_id": chapter["id"],
            "item": {"title": "信", "excerpt": "她把信放在桌上。", "start": 7}}
    r = client.post(url, json=data)
    assert r.status_code == 200
    saved = r.json()["project"]
    assert saved["chapters"][0]["content"] == chapter["content"]
    assert locate_scene(chapter["content"], saved["chapters"][0]["scenes"][0]) is not None
    assert client.post(url, json=data).status_code == 409
    data["expected_updated_at"] = saved["updated_at"]
    data["item"]["excerpt"] = "正文不存在的句子"
    assert client.post(url, json=data).status_code == 422


def test_overlap_and_ambiguous_scenes():
    chapter = {"content": "甲乙甲乙"}
    with pytest.raises(ValueError):
        save_scene(chapter, {"excerpt": "甲乙", "start": -1})
    save_scene(chapter, {"excerpt": "甲乙", "start": 0})
    with pytest.raises(ValueError):
        save_scene(chapter, {"excerpt": "乙甲", "start": 1})
    assert chapter["content"] == "甲乙甲乙"


def test_only_author_approved_preferences_enter_prompt(workspace):
    client, p, store = workspace
    url = f"/api/projects/{p['id']}/workspace/"
    r = client.post(url + "preference", json={"expected_updated_at": p["updated_at"], "item": {"instruction": "让角色通过留白表达犹豫"}})
    assert r.status_code == 200
    p = r.json()["project"]
    req = {"chapter_id": p["chapters"][0]["id"], "mode": "continue"}
    assert "让角色通过留白表达犹豫" not in str(build_prompt(p, req).messages)
    r = client.post(url + "review-preference", json={"expected_updated_at": p["updated_at"], "item": {"id": p["author_preferences"][0]["id"], "status": "approved"}})
    assert r.status_code == 200
    p = r.json()["project"]
    assert "让角色通过留白表达犹豫" in str(build_prompt(p, req).messages)


def test_scene_context_reaches_generation_prompt(workspace):
    _, p, _ = workspace
    c = p["chapters"][0]
    scene = save_scene(c, {"title": "只问归期", "goal": "留下归期但不拆信"})
    prompt = build_prompt(p, {"chapter_id": c["id"], "mode": "instruction", "scene_id": scene["id"]})
    assert "留下归期但不拆信" in str(prompt.messages)


def test_scene_rewrite_rejects_wrong_selection_and_stale_binding(workspace):
    _, p, _ = workspace
    chapter = p["chapters"][0]
    scene = save_scene(chapter, {"excerpt": "她把信放在桌上。", "start": -1})
    request = {"chapter_id": chapter["id"], "scene_id": scene["id"], "mode": "rewrite", "selection": "🌙雨停了。"}
    with pytest.raises(ValueError, match="选区"):
        build_prompt(p, request)
    request["selection"] = scene["excerpt"]
    chapter["content"] = "原文已经被作者替换。"
    with pytest.raises(ValueError, match="原文已变化"):
        build_prompt(p, request)


@pytest.mark.parametrize("before,after", [("甲\n\n乙\n", "甲\n\n丙\n"), ("🌙", "雨"), ("", "新增\r\n"), ("删除", "")])
def test_diff_is_lossless(before, after):
    hunks = text_diff(before, after)["hunks"]
    assert "".join(h["before"] for h in hunks) == before
    assert "".join(h["after"] for h in hunks) == after


def test_memory_rebuild_order_and_idempotency(workspace):
    _, p, _ = workspace
    first = p["chapters"][0]
    commit, _ = begin_memory_commit(p, first)
    mark_memory_commit(p, first, commit["id"], "committed")
    assert begin_memory_commit(p, first)[1]
    first["memory_stale"] = True
    second = {"id": "c2", "content": "后文", "memory_stale": True}
    p["chapters"].append(second)
    p["memory"]["stale_from_chapter"] = 1
    fresh, reused = begin_memory_commit(p, first)
    assert not reused and fresh["id"] != commit["id"]
    with pytest.raises(ValueError):
        prepare_memory_rebuild(p, second)
    prepare_memory_rebuild(p, first)
    finish_memory_rebuild(p, first)
    assert p["memory"]["stale_from_chapter"] == 2
    prepare_memory_rebuild(p, second)
    finish_memory_rebuild(p, second)
    assert "stale_from_chapter" not in p["memory"]


def test_audit_schema_requests_missing_evidence():
    with pytest.raises(ValueError, match="evidence"):
        main.validate_audit_result({"score": 95, "verdict": "revise", "issues": [{"severity": "high"}], "strengths": [], "revision_brief": "复核"})


def test_telemetry_never_stores_credentials_or_content(tmp_path, monkeypatch):
    monkeypatch.setattr(model_telemetry, "_path", None)
    model_telemetry.configure(tmp_path / "calls.db")
    call = model_telemetry.Call({"model": "test", "api_key": "private-key", "base_url": "secret-url"}, [{"content": "秘密小说内容"}])
    call.output = "秘密输出"
    model_telemetry.observe({"usage": {"prompt_tokens": 20, "completion_tokens": 5}, "choices": [{"finish_reason": "stop"}]})
    model_telemetry.attempt(1)
    call.finish()
    result = model_telemetry.report()
    serialized = json.dumps(result, ensure_ascii=False)
    assert all(x not in serialized for x in ["private-key", "secret-url", "秘密小说内容", "秘密输出"])
    assert result["summary"]["retries"] == 1
    assert result["calls"][0]["token_source"] == "provider"


def test_director_blocks_workspace_edits(workspace):
    client, p, store = workspace
    store.create_director_task(p["id"], {"status": "running", "task_type": "production"})
    r = client.post(f"/api/projects/{p['id']}/workspace/narrative", json={"expected_updated_at": p["updated_at"], "item": {"structure_profile": "literary"}})
    assert r.status_code == 409


@pytest.mark.parametrize("endpoint", ["/api/generate", "/api/prompt/preview", "/api/prompt/snapshot"])
def test_missing_scene_returns_actionable_error(workspace, endpoint):
    client, p, _ = workspace
    response = client.post(endpoint, json={"project": p, "chapter_id": p["chapters"][0]["id"], "mode": "instruction", "scene_id": "missing"})
    assert response.status_code == 422
    assert "场景不存在" in response.json()["detail"]


def test_preview_and_snapshot_include_actual_stable_prefixes(workspace):
    client, p, _ = workspace
    body = {"project": p, "chapter_id": p["chapters"][0]["id"], "mode": "continue"}
    response = client.post("/api/prompt/preview", json=body)
    assert response.status_code == 200
    preview = response.json()
    assert "【稳定项目前缀】" in preview["messages"][0]["content"]
    assert preview["sections"][0]["name"] == "稳定项目前缀"
    assert preview["estimated_tokens"] == main.estimate_tokens("\n".join(m["content"] for m in preview["messages"]))
    snapshot = client.post("/api/prompt/snapshot", json=body)
    assert snapshot.status_code == 200
    assert snapshot.json()["messages"] == preview["messages"]

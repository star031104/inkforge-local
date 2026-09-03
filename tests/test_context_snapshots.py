from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app import main
from app.db import ProjectStore


def test_snapshot_freezes_context_and_redacts_credentials(tmp_path):
    store = ProjectStore(tmp_path / "snapshots.db")
    project = store.create("上下文审计")
    fake_secret = "sk-" + "x" * 32
    snapshot = store.create_context_snapshot(
        project["id"],
        project["chapters"][0]["id"],
        {"mode": "continue", "api_key": fake_secret, "instruction": f"不要保存 {fake_secret}"},
        [{"role": "user", "content": f"正文任务 {fake_secret}"}],
        {"estimated_tokens": 123, "runtime": {"api_key": fake_secret}},
        reason="test",
    )

    serialized = json.dumps(snapshot, ensure_ascii=False)
    assert fake_secret not in serialized
    assert serialized.count("[REDACTED]") >= 3
    assert len(snapshot["prompt_hash"]) == 64
    assert snapshot["messages"][0]["content"].endswith("[REDACTED]")

    listed = store.context_snapshots(project["id"])
    assert listed[0]["id"] == snapshot["id"]
    assert listed[0]["estimated_tokens"] == 123
    assert "messages" not in listed[0]
    assert fake_secret.encode() not in store.path.read_bytes()


def test_snapshot_retention_and_project_delete_cleanup(tmp_path):
    store = ProjectStore(tmp_path / "retention.db")
    project = store.create("快照保留")
    for index in range(4):
        store.create_context_snapshot(
            project["id"],
            project["chapters"][0]["id"],
            {"mode": "continue", "index": index},
            [{"role": "user", "content": f"任务{index}"}],
            {"estimated_tokens": index},
            keep=2,
        )
    assert len(store.context_snapshots(project["id"], 10)) == 2
    assert store.delete(project["id"]) is True
    assert store.context_snapshots(project["id"], 10) == []


def test_snapshot_api_builds_lists_and_replays_frozen_messages(tmp_path, monkeypatch):
    isolated = ProjectStore(tmp_path / "api-snapshots.db")
    monkeypatch.setattr(main, "store", isolated)
    project = isolated.create("API 快照")
    chapter = project["chapters"][0]
    chapter["scene_goal"] = "在雨夜找到失踪账册"
    isolated.save(project["id"], project, reason="test")

    client = TestClient(main.app)
    response = client.post(
        "/api/prompt/snapshot",
        json={
            "project": project,
            "chapter_id": chapter["id"],
            "mode": "instruction",
            "instruction": "写雨夜调查场景",
            "selection": "",
            "target_words": 400,
        },
    )
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["reason"] == "manual_preview"
    assert snapshot["messages"]
    assert snapshot["diagnostics"]["sections"]

    listing = client.get(f"/api/projects/{project['id']}/context-snapshots")
    assert listing.status_code == 200
    assert listing.json()[0]["prompt_hash"] == snapshot["prompt_hash"]

    captured = {}

    async def fake_stream(settings, messages):
        captured["settings"] = settings
        captured["messages"] = messages
        yield "冻结上下文回放成功。"

    monkeypatch.setattr(main, "chat_stream", fake_stream)
    replay = client.post(
        f"/api/prompt/snapshots/{snapshot['id']}/replay",
        json={"settings": {"provider": "openai_compatible", "api_key": "temporary-only"}},
    )
    assert replay.status_code == 200
    assert "冻结上下文回放成功" in replay.text
    assert captured["messages"] == snapshot["messages"]
    assert captured["settings"]["api_key"] == "temporary-only"
    stored = isolated.get_context_snapshot(snapshot["id"])
    assert "temporary-only" not in json.dumps(stored, ensure_ascii=False)

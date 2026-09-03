from fastapi.testclient import TestClient

import app.main as main
from app.db import ProjectStore
from app.memory_integrity import chapter_content_hash, derive_story_so_far


def _client(monkeypatch, tmp_path):
    local_store = ProjectStore(tmp_path / "memory-integrity.db")
    monkeypatch.setattr(main, "store", local_store)
    return TestClient(main.app), local_store


def test_accept_apply_is_persistent_idempotent_and_hash_guarded(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    project = store.create("可靠记忆")
    chapter = project["chapters"][0]
    chapter["content"] = (
        "沈砚亲眼看见仓吏把铜钥匙放入东侧木匣，随后封好匣盖。"
        "他站在门边复核封条，没有把这件事告诉门外的人。"
    ) * 3

    accepted = client.post(
        "/api/chapter/accept",
        json={"project": project, "chapter_id": chapter["id"]},
    )
    assert accepted.status_code == 200
    accepted_payload = accepted.json()
    project = accepted_payload["project"]
    commit = accepted_payload["commit"]
    assert commit["status"] == "settlement_pending"
    assert commit["content_hash"] == chapter_content_hash(chapter["content"])
    assert store.get(project["id"])["chapters"][0]["content"] == chapter["content"]

    repeated = client.post(
        "/api/chapter/accept",
        json={"project": project, "chapter_id": chapter["id"]},
    ).json()
    assert repeated["reused"] is True
    assert repeated["commit"]["id"] == commit["id"]
    assert len(repeated["project"]["memory"]["commits"]) == 1

    concurrent = store.get(project["id"])
    concurrent["chapters"].append(
        {
            "id": "concurrent-chapter",
            "title": "并行编辑章",
            "summary": "",
            "content": "这是记忆提取期间在另一章完成的编辑。" * 8,
            "scene_goal": "",
            "plan": {},
        }
    )
    store.save(project["id"], concurrent, reason="concurrent-other-chapter")

    result = {
        "summary": "沈砚目睹仓吏收存铜钥匙并复核封条。",
        "story_so_far": "模型试图直接覆盖全书摘要。",
        "facts": [
            {
                "text": "铜钥匙被仓吏放入东侧木匣",
                "importance": 5,
                "tags": ["沈砚", "铜钥匙"],
                "evidence": "仓吏把铜钥匙放入东侧木匣",
            }
        ],
    }
    applied = client.post(
        "/api/chapter/memory/apply",
        json={
            "project": project,
            "chapter_id": chapter["id"],
            "commit_id": commit["id"],
            "result": result,
        },
    )
    assert applied.status_code == 200
    settled = applied.json()
    project = settled["project"]
    assert settled["commit"]["status"] == "committed"
    assert project["memory"]["facts"][0]["evidence_verified"] is True
    assert project["memory"]["story_digest_candidate"]["text"] == result["story_so_far"]
    assert project["memory"]["story_so_far"] == derive_story_so_far(project)
    assert project["memory"]["story_so_far"] != result["story_so_far"]
    assert any(
        item["id"] == "concurrent-chapter" for item in project["chapters"]
    )

    replay = client.post(
        "/api/chapter/memory/apply",
        json={
            "project": project,
            "chapter_id": chapter["id"],
            "commit_id": commit["id"],
            "result": result,
        },
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent"] is True
    assert len(replay.json()["project"]["memory"]["facts"]) == 1

    changed = replay.json()["project"]
    changed["chapters"][0]["content"] += "正文后来被修改。"
    conflict = client.post(
        "/api/chapter/memory/apply",
        json={
            "project": changed,
            "chapter_id": chapter["id"],
            "commit_id": commit["id"],
            "result": result,
        },
    )
    assert conflict.status_code == 409


def test_failed_memory_can_be_marked_degraded_without_losing_prose(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    project = store.create("降级状态")
    chapter = project["chapters"][0]
    chapter["content"] = "正文已经由作者接纳并需要优先保存。" * 12
    accepted = client.post(
        "/api/chapter/accept",
        json={"project": project, "chapter_id": chapter["id"]},
    ).json()
    degraded = client.post(
        "/api/chapter/memory/degrade",
        json={
            "project": accepted["project"],
            "chapter_id": chapter["id"],
            "commit_id": accepted["commit"]["id"],
            "error": "模拟模型超时",
        },
    )
    assert degraded.status_code == 200
    payload = degraded.json()
    assert payload["commit"]["status"] == "state_degraded"
    persisted = store.get(project["id"])
    assert persisted["chapters"][0]["content"] == chapter["content"]
    assert persisted["chapters"][0]["memory_status"] == "state_degraded"


def test_manual_accept_resets_stale_director_quality_debt(monkeypatch, tmp_path):
    client, store = _client(monkeypatch, tmp_path)
    project = store.create("人工终审清债")
    chapter = project["chapters"][0]
    chapter["content"] = "这是经过编辑重写并确认可进入长期记忆的正式章节正文。" * 8
    task = store.create_director_task(
        project["id"],
        {
            "phase": "chapters",
            "status": "paused",
            "consecutive_quality_debts": 4,
            "consecutive_systemic_debts": 3,
            "quality_directives": ["旧候选存在重复"],
            "checkpoint_message": "旧质量熔断",
            "last_rejected_candidate": {"chapter_id": chapter["id"]},
            "events": [],
        },
    )
    task["status"] = "paused"
    store.save_director_task(task["id"], task)

    accepted = client.post(
        "/api/chapter/accept",
        json={"project": project, "chapter_id": chapter["id"]},
    )
    assert accepted.status_code == 200
    refreshed = store.get_director_task(task["id"])
    assert refreshed["consecutive_quality_debts"] == 0
    assert refreshed["consecutive_systemic_debts"] == 0
    assert refreshed["quality_directives"] == []
    assert refreshed["checkpoint_message"] == ""
    assert refreshed["last_rejected_candidate"] == {}

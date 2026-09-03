import json

from fastapi.testclient import TestClient

from app.db import default_project
from app.main import app


def test_chapter_memory_compact_ai_recovery_precedes_local_fallback(monkeypatch):
    calls = 0

    async def fake_chat_once(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            return "{未闭合"
        return json.dumps(
            {
                "summary": "沈砚核对两册粮牍，确认同旬存在差额，并得到次日继续查第三册的机会。",
                "story_so_far": "沈砚从仓曹粮账切入，以可复核的方式证明账册存在差额，暂时获得继续调查资格。",
                "facts": [
                    {
                        "text": "沈砚已确认两册粮牍在同旬存在差额",
                        "tags": ["沈砚", "粮牍"],
                        "importance": 4,
                        "confidence": "confirmed",
                        "visibility": "objective",
                        "evidence": "两册同旬有缺",
                    }
                ],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.main.chat_once", fake_chat_once)
    project = default_project("memory-recovery", "记忆恢复", "now")
    chapter = project["chapters"][0]
    chapter["content"] = (
        "沈砚将两册粮牍并排，请仓吏逐页复核。仓吏算至旬末，确认数字不同。"
        "沈砚指着木牍说：‘两册同旬有缺。’樊吏最终允许他次日继续查第三册。"
    ) * 3
    response = TestClient(app).post(
        "/api/chapter/memory",
        json={
            "project": project,
            "chapter_id": chapter["id"],
            "draft": chapter["content"],
            "instruction": "测试精简恢复",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert calls == 3
    assert payload["fallback"] is False
    assert payload["memory_mode"] == "compact_recovery"
    assert payload["facts"][0]["evidence"] == "两册同旬有缺"
    assert payload["plot_threads"] == []
    assert payload["timeline"] == []


def test_chapter_memory_minimal_recovery_does_not_require_story_digest(monkeypatch):
    calls = 0

    async def fake_chat_once(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            return "{未闭合"
        return json.dumps(
            {
                "summary": "沈砚证明两册粮牍存在同旬差额，获得继续查验第三册的机会。",
                "facts": [
                    {
                        "text": "沈砚获得继续查验第三册的机会",
                        "tags": ["沈砚", "粮牍"],
                        "importance": 4,
                        "confidence": "confirmed",
                        "visibility": "objective",
                        "evidence": "允许他次日继续查第三册",
                    }
                ],
                "character_updates": [],
                "scene_settlement": {
                    "goal_achieved": "partial",
                    "irreversible_changes": ["调查范围扩大到第三册"],
                    "open_questions": ["第三册是否存在同类差额"],
                    "closing_state": "次日继续核账",
                },
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.main.chat_once", fake_chat_once)
    project = default_project("memory-minimal", "最小记忆", "now")
    chapter = project["chapters"][0]
    chapter["content"] = (
        "沈砚将两册粮牍并排，请仓吏逐页复核。仓吏确认两册同旬有缺，"
        "樊吏最终允许他次日继续查第三册。"
    ) * 4
    response = TestClient(app).post(
        "/api/chapter/memory",
        json={
            "project": project,
            "chapter_id": chapter["id"],
            "draft": chapter["content"],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert calls == 3
    assert payload["fallback"] is False
    assert payload["memory_mode"] == "compact_recovery"
    assert payload["story_so_far"].endswith(payload["summary"])
    assert payload["facts"][0]["evidence"] == "允许他次日继续查第三册"

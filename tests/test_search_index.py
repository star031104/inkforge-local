from __future__ import annotations

import json
from copy import deepcopy

from app.db import ProjectStore


def _store(tmp_path) -> ProjectStore:
    store = ProjectStore(tmp_path / "search.db")
    assert store.fts_enabled is True
    return store


def test_fts_retrieves_old_prose_but_never_current_or_future_chapters(tmp_path):
    store = _store(tmp_path)
    project = store.create("检索边界")
    first = project["chapters"][0]
    first["title"] = "旧宅"
    first["content"] = "沈砚在银杏树下捡到一枚叶形青铜吊坠，并把它藏进左袖暗袋。"
    second = {
        "id": "chapter-2",
        "title": "当下",
        "content": "墙后藏着尚未发生的朱砂机关。",
        "summary": "",
        "scene_goal": "",
        "plan": {},
    }
    project["chapters"].append(second)
    store.save(project["id"], project, reason="test")

    old_hits = store.search_project(project["id"], "青铜吊坠", 2, 20)
    assert any(hit["kind"] == "passage" and "左袖暗袋" in hit["content"] for hit in old_hits)

    current_hits = store.search_project(project["id"], "朱砂机关", 2, 20)
    assert current_hits == []

    later_hits = store.search_project(project["id"], "朱砂机关", 3, 20)
    assert any(hit["source_id"].startswith("chapter-2:passage:") for hit in later_hits)


def test_search_index_is_replaced_on_save_and_removed_on_delete(tmp_path):
    store = _store(tmp_path)
    project = store.create("索引生命周期")
    project["chapters"][0]["content"] = "旧密码是霜鹤七号。"
    project["chapters"].append(
        {"id": "chapter-2", "title": "第二章", "content": "", "summary": "", "plan": {}}
    )
    saved = store.save(project["id"], project, reason="test")
    assert store.search_project(project["id"], "霜鹤七号", 2)

    saved["chapters"][0]["content"] = "新密码是赤鸢九号。"
    store.save(project["id"], saved, reason="test")
    assert store.search_project(project["id"], "霜鹤七号", 2) == []
    assert store.search_project(project["id"], "赤鸢九号", 2)

    assert store.delete(project["id"]) is True
    assert store.search_project(project["id"], "赤鸢九号", 2) == []


def test_search_respects_fact_validity_and_rebuild_does_not_mutate_payload(tmp_path):
    store = _store(tmp_path)
    project = store.create("事实有效期")
    project["memory"]["facts"] = [
        {
            "id": "future-fact",
            "text": "云门将在第五章公开禁令。",
            "tags": ["云门", "禁令"],
            "importance": 5,
            "active": True,
            "valid_from_chapter": 5,
            "valid_until_chapter": 0,
        },
        {
            "id": "expired-fact",
            "text": "青灯旧令只在第二章有效。",
            "tags": ["青灯", "旧令"],
            "importance": 3,
            "active": True,
            "valid_from_chapter": 1,
            "valid_until_chapter": 2,
        },
    ]
    for number in range(2, 7):
        project["chapters"].append(
            {"id": f"chapter-{number}", "title": f"第{number}章", "content": "", "summary": "", "plan": {}}
        )
    saved = store.save(project["id"], project, reason="test")
    before = json.dumps(deepcopy(store.get(project["id"])), ensure_ascii=False, sort_keys=True)

    assert store.search_project(project["id"], "云门禁令", 4) == []
    assert store.search_project(project["id"], "云门禁令", 5)
    assert store.search_project(project["id"], "青灯旧令", 2)
    assert store.search_project(project["id"], "青灯旧令", 3) == []

    assert store.rebuild_search_index(saved["id"]) == 1
    after = json.dumps(store.get(project["id"]), ensure_ascii=False, sort_keys=True)
    assert after == before


def test_fts_migrates_and_rebuilds_existing_projects(tmp_path):
    store = _store(tmp_path)
    project = store.create("迁移")
    project["chapters"][0]["content"] = "档案里记着黑曜船坞的潮汐钟。"
    project["chapters"].append(
        {"id": "next", "title": "下一章", "content": "", "summary": "", "plan": {}}
    )
    store.save(project["id"], project, reason="test")

    reopened = ProjectStore(store.path)
    assert reopened.fts_enabled is True
    hits = reopened.search_project(project["id"], "黑曜船坞", 2)
    assert any("潮汐钟" in hit["content"] for hit in hits)

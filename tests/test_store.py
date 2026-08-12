from app.db import ProjectStore, ensure_project_defaults


def test_old_project_migration_and_revisions(tmp_path):
    store = ProjectStore(tmp_path / "test.db")
    project = store.create("测试")
    project.pop("memory")
    project["chapters"][0].pop("plan")
    saved = store.save(project["id"], project)
    assert saved["memory"]["facts"] == []
    assert saved["memory"]["story_so_far"] == ""
    assert "must_keep" in saved["chapters"][0]["plan"]
    assert saved["narrative"]["target_chapters"] == 30

    saved["premise"] = "新的核心构想"
    store.save(saved["id"], saved, reason="test-change")
    revisions = store.revisions(saved["id"])
    assert revisions
    restored = store.restore(saved["id"], revisions[0]["id"])
    assert restored["id"] == saved["id"]


def test_legacy_shapes_are_normalized_without_losing_text():
    project = ensure_project_defaults(
        {
            "settings": None,
            "style": None,
            "memory": {
                "facts": ["门在午夜打开过"],
                "plot_threads": "wrong-shape",
                "timeline": ["第一夜：林夏抵达旧宅"],
            },
            "world_entries": [
                {"title": "旧宅", "keys": "旧宅，地下室", "content": "午夜停电"}
            ],
            "chapters": [],
        }
    )
    assert project["settings"]["base_url"].endswith("/v1")
    assert project["style"]["dos"] == []
    assert project["memory"]["facts"][0]["text"] == "门在午夜打开过"
    assert project["memory"]["plot_threads"] == []
    assert project["memory"]["timeline"][0]["event"] == "第一夜：林夏抵达旧宅"
    assert project["memory"]["continuity_notes"] == []
    assert project["memory"]["description_ledger"] == []
    assert project["world_entries"][0]["keys"] == ["旧宅", "地下室"]
    assert project["world_entries"][0]["selective_logic"] == "and_any"
    assert project["settings"]["lore_budget"] == 4500
    assert project["chapters"][0]["title"] == "第一章"


def test_character_card_professional_fields_are_migrated():
    project = ensure_project_defaults(
        {"characters": [{"name": "沈砚", "description": "谨慎的客卿"}]}
    )
    character = project["characters"][0]
    assert character["personality"] == "谨慎的客卿"
    assert character["importance"] == "supporting"
    assert character["dialogue_examples"] == []


def test_legacy_global_author_note_moves_to_first_chapter_once():
    project = ensure_project_defaults(
        {
            "author_note": "第一章不要提前揭示幕后人物",
            "chapters": [{"id": "c1", "title": "第一章", "content": ""}],
        }
    )
    assert project["chapters"][0]["author_note"] == "第一章不要提前揭示幕后人物"
    assert project["author_note"] == ""


def test_chapter_versions_import_and_database_backup(tmp_path):
    store = ProjectStore(tmp_path / "source.db")
    project = store.create("原作")
    chapter_id = project["chapters"][0]["id"]
    project["chapters"][0]["content"] = "第一版正文"
    project = store.save(project["id"], project, reason="manual-save")
    project["chapters"][0]["content"] = "第二版正文"
    project = store.save(project["id"], project, reason="accepted-draft")

    versions = store.chapter_versions(project["id"], chapter_id)
    assert versions and versions[0]["word_count"] == len("第一版正文")
    restored = store.restore_chapter_version(project["id"], chapter_id, versions[0]["id"])
    assert restored["chapters"][0]["content"] == "第一版正文"

    imported = store.import_project(restored)
    assert imported["id"] != restored["id"]
    assert imported["chapters"][0]["content"] == "第一版正文"

    backup = store.backup(tmp_path / "backups")
    assert backup.exists() and backup.stat().st_size > 0
    backup_store = ProjectStore(backup)
    assert len(backup_store.list()) == 2

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers.projects import create_projects_router
from app.db import DATABASE_SCHEMA_VERSION, DatabaseVersionError, ProjectStore
from app.infrastructure.backup_location import BackupLocation


def _save_title(store: ProjectStore, project: dict, title: str) -> dict:
    project["title"] = title
    project["chapters"][0]["content"] = f"{title}的正文" * 80
    return store.save(project["id"], project, reason=f"title:{title}")


def test_backup_can_be_validated_previewed_and_restored(tmp_path: Path):
    database = tmp_path / "projects.db"
    secrets = tmp_path / "secrets.db"
    backup_directory = tmp_path / "backups"
    store = ProjectStore(database, secrets)
    project = store.create("恢复前版本")
    project["settings"]["api_key"] = "keep-this-secret"
    project = _save_title(store, project, "恢复前版本")
    backup = store.backup(backup_directory)

    preview = store.inspect_backup(backup)
    assert preview["compatible"] is True
    assert preview["integrity"] == "ok"
    assert preview["counts"]["projects"] == 1
    assert preview["projects"][0]["title"] == "恢复前版本"
    assert len(preview["sha256"]) == 64
    assert preview["schema_version"] == DATABASE_SCHEMA_VERSION
    assert preview["migration_required"] is False

    project = _save_title(store, project, "当前被修改的版本")
    result = store.restore_database_backup(backup, backup_directory)

    assert result["ok"] is True
    assert result["safety_backup"].startswith("inkforge-before-restore-")
    restored = store.get(project["id"])
    assert restored["title"] == "恢复前版本"
    assert restored["settings"]["api_key"] == "keep-this-secret"
    safety = backup_directory / result["safety_backup"]
    assert store.inspect_backup(safety)["projects"][0]["title"] == "当前被修改的版本"


def test_restore_rolls_back_if_post_restore_step_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ProjectStore(tmp_path / "projects.db", tmp_path / "secrets.db")
    project = _save_title(store, store.create("旧版本"), "旧版本")
    backup = store.backup(tmp_path / "backups")
    project = _save_title(store, project, "必须保留的当前版本")
    original_replace = ProjectStore._replace_database
    calls = 0

    def fail_after_first_replace(source_path: Path, destination_path: Path) -> None:
        nonlocal calls
        calls += 1
        original_replace(source_path, destination_path)
        if calls == 1:
            raise RuntimeError("模拟恢复完成后的进程错误")

    monkeypatch.setattr(
        ProjectStore, "_replace_database", staticmethod(fail_after_first_replace)
    )
    with pytest.raises(RuntimeError, match="模拟恢复完成后的进程错误"):
        store.restore_database_backup(backup, tmp_path / "backups")

    assert calls == 2
    assert store.get(project["id"])["title"] == "必须保留的当前版本"


def test_corrupt_backup_is_rejected_without_touching_current_database(tmp_path: Path):
    store = ProjectStore(tmp_path / "projects.db", tmp_path / "secrets.db")
    project = _save_title(store, store.create("当前作品"), "当前作品")
    corrupt = tmp_path / "inkforge-corrupt.db"
    corrupt.write_bytes(b"this is not sqlite")

    preview = store.inspect_backup(corrupt)
    assert preview["compatible"] is False
    with pytest.raises(ValueError, match="不能安全恢复"):
        store.restore_database_backup(corrupt, tmp_path / "backups")
    assert store.get(project["id"])["title"] == "当前作品"


def test_backup_restore_http_contract_and_running_task_guard(tmp_path: Path):
    store = ProjectStore(tmp_path / "projects.db", tmp_path / "secrets.db")
    backups = tmp_path / "backups"
    project = _save_title(store, store.create("备份内容"), "备份内容")
    backup = store.backup(backups)
    project = _save_title(store, project, "当前内容")

    class RunningTask:
        @staticmethod
        def done() -> bool:
            return False

    runners: dict[str, object] = {"running": RunningTask()}
    app = FastAPI()
    app.include_router(
        create_projects_router(lambda: store, backups, lambda: runners)
    )
    with TestClient(app) as client:
        listing = client.get("/api/backups")
        assert listing.status_code == 200
        assert listing.json()["items"][0]["filename"] == backup.name

        preview = client.get(f"/api/backups/{backup.name}")
        assert preview.status_code == 200
        assert preview.json()["compatible"] is True

        rejected = client.post(
            f"/api/backups/{backup.name}/restore",
            json={"confirmation": "恢复数据库"},
        )
        assert rejected.status_code == 409
        assert store.get(project["id"])["title"] == "当前内容"

        runners.clear()
        wrong_phrase = client.post(
            f"/api/backups/{backup.name}/restore",
            json={"confirmation": "确认"},
        )
        assert wrong_phrase.status_code == 422
        restored = client.post(
            f"/api/backups/{backup.name}/restore",
            json={"confirmation": "恢复数据库"},
        )
        assert restored.status_code == 200
        assert restored.json()["safety_backup"].startswith(
            "inkforge-before-restore-"
        )
        assert store.get(project["id"])["title"] == "备份内容"


def test_legacy_database_is_backed_up_and_migrated(tmp_path: Path):
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as db:
        db.execute(
            "CREATE TABLE projects (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TABLE revisions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "project_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, "
            "reason TEXT NOT NULL DEFAULT 'autosave')"
        )

    ProjectStore(database, tmp_path / "secrets.db")
    with sqlite3.connect(database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == DATABASE_SCHEMA_VERSION
        columns = {row[1] for row in db.execute("PRAGMA table_info(director_tasks)")}
    assert {"owner_instance_id", "lease_expires_at"} <= columns
    assert list((tmp_path / "schema-backups").glob("legacy-before-schema-*.db"))


def test_future_database_and_backup_are_rejected(tmp_path: Path):
    store = ProjectStore(tmp_path / "current.db", tmp_path / "secrets.db")
    created = store.backup(tmp_path, keep=20)
    future = tmp_path / "inkforge-future.db"
    created.replace(future)
    with sqlite3.connect(future) as db:
        db.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION + 1}")

    preview = store.inspect_backup(future)
    assert preview["compatible"] is False
    assert "请升级砚火" in "".join(preview["warnings"])
    with pytest.raises(DatabaseVersionError):
        ProjectStore(future, tmp_path / "future-secrets.db")


def test_backup_location_can_be_changed_and_persisted(tmp_path: Path):
    database = tmp_path / "data" / "projects.db"
    store = ProjectStore(database, tmp_path / "data" / "secrets.db")
    settings_file = tmp_path / "data" / "settings.json"
    location = BackupLocation(
        tmp_path / "data" / "backups", database, settings_file
    )
    app = FastAPI()
    app.include_router(create_projects_router(lambda: store, location))
    destination = tmp_path / "external" / "novel-backups"

    with TestClient(app) as client:
        changed = client.put(
            "/api/backup-settings", json={"path": str(destination)}
        )
        assert changed.status_code == 200
        assert Path(changed.json()["path"]) == destination.resolve()
        created = client.post("/api/backup")
        assert created.status_code == 200
        assert (destination / created.json()["filename"]).is_file()

    reloaded = BackupLocation(
        tmp_path / "data" / "backups", database, settings_file
    )
    assert reloaded.path == destination.resolve()

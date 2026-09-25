from __future__ import annotations

import json
import sqlite3

from app.db import ProjectStore


def _payloads(path):
    with sqlite3.connect(path) as db:
        projects = [row[0] for row in db.execute("SELECT payload FROM projects")]
        revisions = [row[0] for row in db.execute("SELECT payload FROM revisions")]
    return projects + revisions


def test_credentials_are_outside_projects_revisions_and_backups(tmp_path):
    database = tmp_path / "projects.db"
    secrets = tmp_path / "credentials.db"
    store = ProjectStore(database, secrets)
    project = store.create("密钥隔离")
    project["settings"]["api_key"] = "secret-primary-marker"
    project["settings"]["reasoning_api_key"] = "secret-secondary-marker"
    project = store.save(project["id"], project, reason="credentials")
    project["title"] = "触发历史版本"
    project = store.save(project["id"], project, reason="manual")

    assert project["settings"]["api_key"] == "secret-primary-marker"
    assert store.get(project["id"])["settings"]["reasoning_api_key"] == "secret-secondary-marker"
    assert all("secret-" not in payload for payload in _payloads(database))
    assert b"secret-primary-marker" not in secrets.read_bytes()
    assert b"secret-secondary-marker" not in secrets.read_bytes()

    backup = store.backup(tmp_path / "backups")
    assert all("secret-" not in payload for payload in _payloads(backup))

    restored = store.restore(project["id"], store.revisions(project["id"])[0]["id"])
    assert restored["settings"]["api_key"] == "secret-primary-marker"


def test_legacy_embedded_credentials_are_migrated_and_can_be_cleared(tmp_path):
    database = tmp_path / "legacy.db"
    secrets = tmp_path / "legacy-secrets.db"
    store = ProjectStore(database, secrets)
    project = store.create("旧作品")
    with sqlite3.connect(database) as db:
        payload = json.loads(
            db.execute("SELECT payload FROM projects WHERE id = ?", (project["id"],)).fetchone()[0]
        )
        payload["settings"]["api_key"] = "legacy-secret-marker"
        rendered = json.dumps(payload, ensure_ascii=False)
        db.execute("UPDATE projects SET payload = ? WHERE id = ?", (rendered, project["id"]))
        db.execute(
            "INSERT INTO revisions(project_id, payload, created_at, reason) VALUES (?, ?, ?, ?)",
            (project["id"], rendered, project["created_at"], "legacy"),
        )

    migrated = ProjectStore(database, secrets)
    loaded = migrated.get(project["id"])
    assert loaded["settings"]["api_key"] == "legacy-secret-marker"
    assert all("legacy-secret-marker" not in payload for payload in _payloads(database))

    loaded["settings"]["api_key"] = ""
    migrated.save(project["id"], loaded, reason="clear-key")
    assert migrated.get(project["id"])["settings"]["api_key"] == ""


def test_legacy_plaintext_secret_rows_are_encrypted_in_place(tmp_path):
    secrets = tmp_path / "legacy-credentials.db"
    with sqlite3.connect(secrets) as db:
        db.execute(
            "CREATE TABLE project_secrets (project_id TEXT NOT NULL, name TEXT NOT NULL, "
            "value TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(project_id, name))"
        )
        db.execute(
            "INSERT INTO project_secrets VALUES (?, ?, ?, ?)",
            ("book", "settings.api_key", "plaintext-legacy-marker", "now"),
        )

    store = ProjectStore(tmp_path / "projects.db", secrets)
    assert store.secret_store.get("book")["settings.api_key"] == "plaintext-legacy-marker"
    assert b"plaintext-legacy-marker" not in secrets.read_bytes()

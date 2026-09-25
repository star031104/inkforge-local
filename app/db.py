from __future__ import annotations

import json
import re
import hashlib
import sqlite3
import threading
import uuid
from contextlib import contextmanager, closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .writing_skills import normalize_writing_skill
from .story_systems import bump_authority, content_hash
from .project_service import ProjectConflictError
from .temporal_context import invalidate_changed_manuscript
from .infrastructure.search_index import (
    _fts_match_query,
    _project_search_documents,
    _sanitize_snapshot_value,
)
from .infrastructure.secret_store import (
    ProjectSecretStore,
    apply_project_secrets,
    extract_project_secrets,
    strip_project_secrets,
)
from .domain.project_schema import default_project, ensure_project_defaults


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


DATABASE_SCHEMA_VERSION = 2


class DatabaseVersionError(RuntimeError):
    """Raised before touching a database created by a newer application."""


class ProjectStore:
    def __init__(self, path: Path, secret_path: Path | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.secret_store = ProjectSecretStore(
            secret_path or path.with_name(path.stem + "-secrets.db")
        )
        self.lock = threading.RLock()
        self.fts_enabled = False
        self.instance_id = str(uuid.uuid4())
        self._backup_before_schema_upgrade()
        self._init()

    def _backup_before_schema_upgrade(self) -> None:
        """Keep an untouched copy before the first migration of an old database."""
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return
        with closing(sqlite3.connect(self.path, timeout=10)) as source:
            version = int(source.execute("PRAGMA user_version").fetchone()[0])
            if version > DATABASE_SCHEMA_VERSION:
                raise DatabaseVersionError(
                    f"作品库版本 {version} 高于当前程序支持的 "
                    f"{DATABASE_SCHEMA_VERSION}，请使用更新版本的砚火打开"
                )
            tables = {
                str(row[0])
                for row in source.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            if not tables or version >= DATABASE_SCHEMA_VERSION:
                return
            directory = self.path.parent / "schema-backups"
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            destination = directory / (
                f"{self.path.stem}-before-schema-v{version}-to-v"
                f"{DATABASE_SCHEMA_VERSION}-{stamp}.db"
            )
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
                target.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a transaction-scoped SQLite connection and always close it.

        ``sqlite3.Connection``'s own context manager commits/rolls back but does
        not close the file descriptor. Long pytest/director sessions therefore
        accumulated ResourceWarnings and could exhaust handles on Windows.
        """
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _init(self) -> None:
        with self._connect() as db:
            database_version = int(db.execute("PRAGMA user_version").fetchone()[0])
            if database_version > DATABASE_SCHEMA_VERSION:
                raise DatabaseVersionError(
                    f"作品库版本 {database_version} 高于当前程序支持的 "
                    f"{DATABASE_SCHEMA_VERSION}，请使用更新版本的砚火打开"
                )
            db.execute("PRAGMA journal_mode = WAL")
            db.execute("PRAGMA synchronous = NORMAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS revisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT 'autosave'
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_revisions_project "
                "ON revisions(project_id, id DESC)"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS chapter_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT 'manual'
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_chapter_versions_chapter "
                "ON chapter_versions(project_id, chapter_id, id DESC)"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS director_tasks (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    owner_instance_id TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_director_tasks_project "
                "ON director_tasks(project_id, updated_at DESC)"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS context_snapshots (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    project_updated_at TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    messages_json TEXT NOT NULL,
                    diagnostics_json TEXT NOT NULL,
                    prompt_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_context_snapshots_project "
                "ON context_snapshots(project_id, created_at DESC)"
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS chapter_sessions (
                    project_id TEXT NOT NULL,
                    chapter_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project_id, chapter_id)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS user_writing_skills (
                    id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # This is a derived, rebuildable search index. Project JSON remains
            # the only source of truth, so an index migration can never alter
            # manuscript or canon data.
            try:
                db.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS search_documents_fts USING fts5(
                        project_id UNINDEXED,
                        source_id UNINDEXED,
                        kind UNINDEXED,
                        title,
                        content,
                        tags,
                        lexemes,
                        chapter_number UNINDEXED,
                        valid_from UNINDEXED,
                        valid_until UNINDEXED,
                        visibility UNINDEXED,
                        tokenize = 'unicode61 remove_diacritics 2'
                    )
                    """
                )
                db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_index_state (
                        project_id TEXT PRIMARY KEY,
                        project_updated_at TEXT NOT NULL,
                        indexed_at TEXT NOT NULL
                    )
                    """
                )
                self.fts_enabled = True
                rows = db.execute(
                    """
                    SELECT p.id, p.payload, p.updated_at
                    FROM projects p
                    LEFT JOIN search_index_state s ON s.project_id = p.id
                    WHERE s.project_id IS NULL OR s.project_updated_at != p.updated_at
                    """
                ).fetchall()
                for row in rows:
                    self._rebuild_search_index(
                        db,
                        ensure_project_defaults(json.loads(row["payload"])),
                        str(row["updated_at"]),
                    )
            except sqlite3.OperationalError:
                # Some vendor SQLite builds omit FTS5. Core writing continues
                # with the dependency-free lexical retriever in memory.py.
                self.fts_enabled = False
            self._migrate_schema(db, database_version)
            self._migrate_embedded_secrets(db)

    @staticmethod
    def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in db.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def _migrate_schema(
        self, db: sqlite3.Connection, database_version: int
    ) -> None:
        """Apply small, forward-only and idempotent SQLite migrations."""
        if database_version < 2:
            columns = self._table_columns(db, "director_tasks")
            if "owner_instance_id" not in columns:
                db.execute(
                    "ALTER TABLE director_tasks ADD COLUMN "
                    "owner_instance_id TEXT NOT NULL DEFAULT ''"
                )
            if "lease_expires_at" not in columns:
                db.execute(
                    "ALTER TABLE director_tasks ADD COLUMN "
                    "lease_expires_at TEXT NOT NULL DEFAULT ''"
                )
        db.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")

    def _migrate_embedded_secrets(self, db: sqlite3.Connection) -> None:
        """Move credentials from legacy JSON payloads into the isolated store."""
        rows = db.execute("SELECT id, payload FROM projects").fetchall()
        for row in rows:
            payload = json.loads(row["payload"])
            secrets = extract_project_secrets(payload)
            if not secrets:
                continue
            self.secret_store.merge(str(row["id"]), secrets)
            strip_project_secrets(payload)
            db.execute(
                "UPDATE projects SET payload = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), row["id"]),
            )
        revision_rows = db.execute("SELECT id, payload FROM revisions").fetchall()
        for row in revision_rows:
            payload = json.loads(row["payload"])
            if not extract_project_secrets(payload):
                continue
            strip_project_secrets(payload)
            db.execute(
                "UPDATE revisions SET payload = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), row["id"]),
            )

    def _for_storage(self, payload: dict[str, Any]) -> dict[str, Any]:
        return strip_project_secrets(deepcopy(payload))

    def _with_secrets(
        self, project_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return apply_project_secrets(payload, self.secret_store.get(project_id))

    def list(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, title, payload, created_at, updated_at "
                "FROM projects ORDER BY updated_at DESC"
            ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload"])
            result.append(
                {
                    "id": row["id"],
                    "title": row["title"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "chapter_count": len(payload.get("chapters", [])),
                    "genre": payload.get("genre", ""),
                }
            )
        return result

    def get(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if not row:
            return None
        payload = ensure_project_defaults(json.loads(row["payload"]))
        return self._with_secrets(project_id, payload)

    def create(self, title: str = "未命名故事") -> dict[str, Any]:
        project_id = str(uuid.uuid4())
        now = utc_now()
        payload = default_project(project_id, title, now)
        stored = self._for_storage(payload)
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO projects(id, title, payload, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, title, json.dumps(stored, ensure_ascii=False), now, now),
            )
            self._rebuild_search_index(db, stored, now)
        return payload

    def save(
        self, project_id: str, payload: dict[str, Any], reason: str = "autosave",
        *, expected_updated_at: str | None = None,
    ) -> dict[str, Any]:
        existing = self.get(project_id)
        if not existing:
            raise KeyError(project_id)
        clean = ensure_project_defaults(deepcopy(payload))
        submitted_secrets = extract_project_secrets(clean)
        invalidate_changed_manuscript(existing, clean)
        clean["id"] = project_id
        clean["created_at"] = existing.get("created_at", utc_now())
        clean["updated_at"] = utc_now()
        clean.setdefault("title", "未命名故事")
        # Finalization receipts are an append-only audit trail. A normal full
        # project save (or restoring an older snapshot) may omit them, but it
        # must never delete or rewrite a receipt that already exists.
        incoming_finalizations = {
            str(item.get("id", "")): item
            for item in clean.get("editorial", {}).get("finalizations", [])
            if isinstance(item, dict) and item.get("id")
        }
        protected_finalizations = []
        for receipt in existing.get("editorial", {}).get("finalizations", []):
            if not isinstance(receipt, dict) or not receipt.get("id"):
                continue
            receipt_id = str(receipt["id"])
            protected_finalizations.append(deepcopy(receipt))
            incoming_finalizations.pop(receipt_id, None)
        clean["editorial"]["finalizations"] = protected_finalizations + list(incoming_finalizations.values())
        authority_fields = {
            "story_bible": ("premise", "outline", "author_intent", "book_rules", "narrative"),
            "characters": ("characters",),
            "world": ("world_entries",),
            "planning": ("planning",),
            "style": ("style", "references"),
            "research": ("research",),
        }
        existing_revisions = existing.get("governance", {}).get("revisions", {})
        clean_revisions = clean.get("governance", {}).get("revisions", {})
        for authority_kind, previous_revision in existing_revisions.items():
            clean_revisions[authority_kind] = max(
                int(previous_revision or 0), int(clean_revisions.get(authority_kind, 0) or 0)
            )
        for authority_kind, fields in authority_fields.items():
            before = {field: existing.get(field) for field in fields}
            after = {field: clean.get(field) for field in fields}
            if (
                content_hash(before) != content_hash(after)
                and int(clean_revisions.get(authority_kind, 0) or 0)
                == int(existing_revisions.get(authority_kind, 0) or 0)
            ):
                bump_authority(clean, authority_kind, f"保存时检测到 {authority_kind} 资料变化")
        existing_stored = self._for_storage(existing)
        clean_stored = self._for_storage(clean)
        with self.lock, self._connect() as db:
            previous_compare = deepcopy(existing_stored)
            clean_compare = deepcopy(clean_stored)
            previous_compare.pop("updated_at", None)
            clean_compare.pop("updated_at", None)
            previous_json = json.dumps(
                previous_compare, ensure_ascii=False, sort_keys=True
            )
            new_json = json.dumps(clean_compare, ensure_ascii=False, sort_keys=True)
            if previous_json == new_json:
                latest = db.execute("SELECT updated_at FROM projects WHERE id = ?", (project_id,)).fetchone()
                expected = expected_updated_at if expected_updated_at is not None else existing["updated_at"]
                if latest is None or latest["updated_at"] != expected:
                    raise ProjectConflictError("作品已更新，请重新载入后合并。")
                self.secret_store.replace(project_id, submitted_secrets)
                return self._with_secrets(project_id, existing_stored)
            if previous_json != new_json:
                db.execute(
                    "INSERT INTO revisions(project_id, payload, created_at, reason) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        project_id,
                        json.dumps(existing_stored, ensure_ascii=False),
                        utc_now(),
                        reason,
                    ),
                )
                if reason != "autosave":
                    previous_chapters = {
                        str(item.get("id", "")): item
                        for item in existing.get("chapters", [])
                        if isinstance(item, dict) and item.get("id")
                    }
                    current_chapters = {
                        str(item.get("id", "")): item
                        for item in clean.get("chapters", [])
                        if isinstance(item, dict) and item.get("id")
                    }
                    for chapter_id, previous_chapter in previous_chapters.items():
                        current_chapter = current_chapters.get(chapter_id)
                        if current_chapter is None or json.dumps(
                            previous_chapter, ensure_ascii=False, sort_keys=True
                        ) != json.dumps(
                            current_chapter, ensure_ascii=False, sort_keys=True
                        ):
                            db.execute(
                                "INSERT INTO chapter_versions("
                                "project_id, chapter_id, title, payload, created_at, reason"
                                ") VALUES (?, ?, ?, ?, ?, ?)",
                                (
                                    project_id,
                                    chapter_id,
                                    str(previous_chapter.get("title", "")),
                                    json.dumps(previous_chapter, ensure_ascii=False),
                                    utc_now(),
                                    reason,
                                ),
                            )
                            db.execute(
                                """
                                DELETE FROM chapter_versions
                                WHERE project_id = ? AND chapter_id = ? AND id NOT IN (
                                    SELECT id FROM chapter_versions
                                    WHERE project_id = ? AND chapter_id = ?
                                    ORDER BY id DESC LIMIT 20
                                )
                                """,
                                (project_id, chapter_id, project_id, chapter_id),
                            )
            cursor = db.execute(
                "UPDATE projects SET title = ?, payload = ?, updated_at = ? WHERE id = ? AND updated_at = ?",
                (
                    clean_stored["title"],
                    json.dumps(clean_stored, ensure_ascii=False),
                    clean_stored["updated_at"],
                    project_id,
                    expected_updated_at if expected_updated_at is not None else existing["updated_at"],
                ),
            )
            if cursor.rowcount != 1:
                raise ProjectConflictError("作品已更新，本次写入已撤销；请重新载入后合并。")
            self._rebuild_search_index(db, clean_stored, clean_stored["updated_at"])
            db.execute(
                """
                DELETE FROM revisions
                WHERE project_id = ? AND id NOT IN (
                    SELECT id FROM revisions WHERE project_id = ?
                    ORDER BY id DESC LIMIT 40
                )
                """,
                (project_id, project_id),
            )
        self.secret_store.replace(project_id, submitted_secrets)
        return self._with_secrets(project_id, clean_stored)

    def delete(self, project_id: str) -> bool:
        with self.lock, self._connect() as db:
            cursor = db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            db.execute("DELETE FROM revisions WHERE project_id = ?", (project_id,))
            db.execute("DELETE FROM chapter_versions WHERE project_id = ?", (project_id,))
            db.execute("DELETE FROM director_tasks WHERE project_id = ?", (project_id,))
            db.execute("DELETE FROM context_snapshots WHERE project_id = ?", (project_id,))
            db.execute("DELETE FROM chapter_sessions WHERE project_id = ?", (project_id,))
            if self.fts_enabled:
                db.execute(
                    "DELETE FROM search_documents_fts WHERE project_id = ?",
                    (project_id,),
                )
                db.execute(
                    "DELETE FROM search_index_state WHERE project_id = ?", (project_id,)
                )
        self.secret_store.delete(project_id)
        return cursor.rowcount > 0

    def create_director_task(
        self, project_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        task_id = str(uuid.uuid4())
        now = utc_now()
        clean = deepcopy(payload)
        clean.update(
            {
                "id": task_id,
                "project_id": project_id,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
            }
        )
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO director_tasks(id, project_id, status, payload, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    project_id,
                    clean["status"],
                    json.dumps(clean, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        return clean

    def get_director_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload, status, updated_at, owner_instance_id, "
                "lease_expires_at FROM director_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["status"] = row["status"]
        payload["updated_at"] = row["updated_at"]
        payload["owner_instance_id"] = str(row["owner_instance_id"] or "")
        payload["lease_expires_at"] = str(row["lease_expires_at"] or "")
        return payload

    def claim_director_task(
        self, task_id: str, owner_instance_id: str, lease_seconds: int = 90
    ) -> bool:
        """Atomically claim a task unless another live process owns its lease."""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=max(30, int(lease_seconds)))
        with self.lock, self._connect() as db:
            cursor = db.execute(
                """
                UPDATE director_tasks
                SET owner_instance_id = ?, lease_expires_at = ?
                WHERE id = ? AND (
                    owner_instance_id = '' OR owner_instance_id = ? OR
                    lease_expires_at = '' OR lease_expires_at <= ?
                )
                """,
                (
                    owner_instance_id,
                    expires.isoformat(),
                    task_id,
                    owner_instance_id,
                    now.isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def renew_director_task_lease(
        self, task_id: str, owner_instance_id: str, lease_seconds: int = 90
    ) -> bool:
        expires = datetime.now(timezone.utc) + timedelta(
            seconds=max(30, int(lease_seconds))
        )
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE director_tasks SET lease_expires_at = ? "
                "WHERE id = ? AND owner_instance_id = ?",
                (expires.isoformat(), task_id, owner_instance_id),
            )
        return cursor.rowcount == 1

    def release_director_task_lease(
        self, task_id: str, owner_instance_id: str
    ) -> bool:
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE director_tasks SET owner_instance_id = '', "
                "lease_expires_at = '' WHERE id = ? AND owner_instance_id = ?",
                (task_id, owner_instance_id),
            )
        return cursor.rowcount == 1

    def director_task_lease_active(self, task_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT owner_instance_id, lease_expires_at FROM director_tasks "
                "WHERE id = ?",
                (task_id,),
            ).fetchone()
        if not row or not str(row["owner_instance_id"] or ""):
            return False
        try:
            expires = datetime.fromisoformat(str(row["lease_expires_at"] or ""))
        except ValueError:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > datetime.now(timezone.utc)

    def clear_director_task_leases(self) -> int:
        """Clear stale owners after this process acquired the exclusive app lock."""
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE director_tasks SET owner_instance_id = '', "
                "lease_expires_at = '' WHERE owner_instance_id != ''"
            )
        return cursor.rowcount

    def latest_director_task(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id FROM director_tasks WHERE project_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return self.get_director_task(row["id"]) if row else None

    def save_director_task(
        self, task_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        existing = self.get_director_task(task_id)
        if not existing:
            raise KeyError(task_id)
        clean = deepcopy(payload)
        clean["id"] = task_id
        clean["project_id"] = existing["project_id"]
        clean["created_at"] = existing["created_at"]
        clean["updated_at"] = utc_now()
        clean.setdefault("status", existing.get("status", "paused"))
        with self.lock, self._connect() as db:
            db.execute(
                "UPDATE director_tasks SET status = ?, payload = ?, updated_at = ? WHERE id = ?",
                (
                    clean["status"],
                    json.dumps(clean, ensure_ascii=False),
                    clean["updated_at"],
                    task_id,
                ),
            )
        return clean

    def revisions(self, project_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, created_at, reason, payload FROM revisions "
                "WHERE project_id = ? ORDER BY id DESC",
                (project_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "created_at": row["created_at"],
                "reason": row["reason"],
                "title": json.loads(row["payload"]).get("title", "未命名故事"),
            }
            for row in rows
        ]

    def restore(self, project_id: str, revision_id: int) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM revisions WHERE project_id = ? AND id = ?",
                (project_id, revision_id),
            ).fetchone()
        if not row:
            raise KeyError(revision_id)
        restored = apply_project_secrets(
            json.loads(row["payload"]), self.secret_store.get(project_id)
        )
        return self.save(project_id, restored, reason=f"restore:{revision_id}")

    def chapter_versions(
        self, project_id: str, chapter_id: str
    ) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, title, created_at, reason, payload FROM chapter_versions "
                "WHERE project_id = ? AND chapter_id = ? ORDER BY id DESC",
                (project_id, chapter_id),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "reason": row["reason"],
                "word_count": len(str(json.loads(row["payload"]).get("content", ""))),
            }
            for row in rows
        ]

    def restore_chapter_version(
        self, project_id: str, chapter_id: str, version_id: int
    ) -> dict[str, Any]:
        project = self.get(project_id)
        if not project:
            raise KeyError(project_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM chapter_versions "
                "WHERE project_id = ? AND chapter_id = ? AND id = ?",
                (project_id, chapter_id, version_id),
            ).fetchone()
        if not row:
            raise KeyError(version_id)
        restored = json.loads(row["payload"])
        for index, chapter in enumerate(project.get("chapters", [])):
            if str(chapter.get("id", "")) == chapter_id:
                project["chapters"][index] = restored
                return self.save(
                    project_id,
                    project,
                    reason=f"restore-chapter:{chapter_id}:{version_id}",
                )
        raise KeyError(chapter_id)

    def import_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        clean = ensure_project_defaults(deepcopy(payload))
        strip_project_secrets(clean)
        project_id = str(uuid.uuid4())
        now = utc_now()
        clean["id"] = project_id
        clean["created_at"] = now
        clean["updated_at"] = now
        clean["title"] = str(clean.get("title", "")).strip() or "Imported project"
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO projects(id, title, payload, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    project_id,
                    clean["title"],
                    json.dumps(clean, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            self._rebuild_search_index(db, clean, now)
        return clean

    def create_context_snapshot(
        self,
        project_id: str,
        chapter_id: str,
        request: dict[str, Any],
        messages: list[dict[str, Any]],
        diagnostics: dict[str, Any],
        reason: str = "manual",
        keep: int = 60,
    ) -> dict[str, Any]:
        """Freeze a scrubbed model context without persisting provider credentials."""
        snapshot_id = str(uuid.uuid4())
        now = utc_now()
        clean_request = _sanitize_snapshot_value(request)
        clean_messages = _sanitize_snapshot_value(messages)
        clean_diagnostics = _sanitize_snapshot_value(diagnostics)
        request_json = json.dumps(clean_request, ensure_ascii=False, sort_keys=True)
        messages_json = json.dumps(clean_messages, ensure_ascii=False, sort_keys=True)
        diagnostics_json = json.dumps(
            clean_diagnostics, ensure_ascii=False, sort_keys=True
        )
        prompt_hash = hashlib.sha256(messages_json.encode("utf-8")).hexdigest()
        with self.lock, self._connect() as db:
            row = db.execute(
                "SELECT updated_at FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
            if not row:
                raise KeyError(project_id)
            db.execute(
                """
                INSERT INTO context_snapshots(
                    id, project_id, chapter_id, project_updated_at, reason,
                    request_json, messages_json, diagnostics_json, prompt_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    project_id,
                    chapter_id,
                    str(row["updated_at"]),
                    str(reason or "manual")[:80],
                    request_json,
                    messages_json,
                    diagnostics_json,
                    prompt_hash,
                    now,
                ),
            )
            db.execute(
                """
                DELETE FROM context_snapshots
                WHERE project_id = ? AND id NOT IN (
                    SELECT id FROM context_snapshots WHERE project_id = ?
                    ORDER BY created_at DESC LIMIT ?
                )
                """,
                (project_id, project_id, min(200, max(1, int(keep or 60)))),
            )
        snapshot = self.get_context_snapshot(snapshot_id)
        if not snapshot:
            raise RuntimeError("上下文快照写入后无法读取")
        return snapshot

    def get_chapter_session(
        self, project_id: str, chapter_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT payload FROM chapter_sessions WHERE project_id = ? AND chapter_id = ?",
                (project_id, chapter_id),
            ).fetchone()
        if not row:
            return None
        value = json.loads(row["payload"])
        return value if isinstance(value, dict) else None

    def save_chapter_session(
        self, project_id: str, chapter_id: str, session: dict[str, Any]
    ) -> dict[str, Any]:
        now = utc_now()
        clean = _sanitize_snapshot_value(session)
        with self.lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO chapter_sessions(project_id, chapter_id, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(project_id, chapter_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (project_id, chapter_id, json.dumps(clean, ensure_ascii=False), now),
            )
        return clean

    def get_context_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM context_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "id": str(row["id"]),
            "project_id": str(row["project_id"]),
            "chapter_id": str(row["chapter_id"]),
            "project_updated_at": str(row["project_updated_at"]),
            "reason": str(row["reason"]),
            "request": json.loads(row["request_json"]),
            "messages": json.loads(row["messages_json"]),
            "diagnostics": json.loads(row["diagnostics_json"]),
            "prompt_hash": str(row["prompt_hash"]),
            "created_at": str(row["created_at"]),
        }

    def context_snapshots(
        self, project_id: str, limit: int = 30
    ) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT id, project_id, chapter_id, project_updated_at, reason,
                       diagnostics_json, prompt_hash, created_at
                FROM context_snapshots WHERE project_id = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (project_id, min(100, max(1, int(limit or 30)))),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            diagnostics = json.loads(row["diagnostics_json"])
            result.append(
                {
                    "id": str(row["id"]),
                    "project_id": str(row["project_id"]),
                    "chapter_id": str(row["chapter_id"]),
                    "project_updated_at": str(row["project_updated_at"]),
                    "reason": str(row["reason"]),
                    "prompt_hash": str(row["prompt_hash"]),
                    "prompt_version": str(diagnostics.get("prompt_version", "unknown")),
                    "estimated_tokens": int(diagnostics.get("estimated_tokens", 0) or 0),
                    "created_at": str(row["created_at"]),
                }
            )
        return result

    def user_writing_skills(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT payload FROM user_writing_skills ORDER BY updated_at DESC"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                result.append(
                    normalize_writing_skill(
                        json.loads(row["payload"]), scope="user", readonly=False
                    )
                )
            except (ValueError, json.JSONDecodeError):
                continue
        return result

    def upsert_user_writing_skill(self, payload: dict[str, Any]) -> dict[str, Any]:
        skill = normalize_writing_skill(payload, scope="user", readonly=False)
        now = utc_now()
        with self.lock, self._connect() as db:
            existing = db.execute(
                "SELECT created_at FROM user_writing_skills WHERE id = ?",
                (skill["id"],),
            ).fetchone()
            db.execute(
                """
                INSERT INTO user_writing_skills(id, payload, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    skill["id"],
                    json.dumps(skill, ensure_ascii=False),
                    str(existing["created_at"]) if existing else now,
                    now,
                ),
            )
        return skill

    def delete_user_writing_skill(self, skill_id: str) -> bool:
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "DELETE FROM user_writing_skills WHERE id = ?", (skill_id,)
            )
        return cursor.rowcount > 0

    def _rebuild_search_index(
        self,
        db: sqlite3.Connection,
        project: dict[str, Any],
        project_updated_at: str | None = None,
    ) -> None:
        """Replace one project's derived FTS corpus inside the caller transaction."""
        if not self.fts_enabled:
            return
        project_id = str(project.get("id", ""))
        if not project_id:
            return
        columns = ("source_id", "kind", "title", "content", "tags", "lexemes",
                   "chapter_number", "valid_from", "valid_until", "visibility")
        existing = {}
        for row in db.execute("SELECT rowid, * FROM search_documents_fts WHERE project_id = ?", (project_id,)):
            key = (str(row["source_id"]), str(row["kind"]))
            existing[key] = (row["rowid"], tuple(str(row[c]) for c in columns))
        changed = []
        for item in _project_search_documents(project):
            key = (item["source_id"], item["kind"])
            old = existing.pop(key, None)
            values = tuple(str(item[c]) for c in columns)
            if old and old[1] == values:
                continue
            if old:
                db.execute("DELETE FROM search_documents_fts WHERE rowid = ?", (old[0],))
            changed.append((project_id, *values))
        db.executemany("DELETE FROM search_documents_fts WHERE rowid = ?",
                       [(old[0],) for old in existing.values()])
        db.executemany(
            "INSERT INTO search_documents_fts(project_id, " + ",".join(columns) + ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            changed,
        )
        updated_at = str(project_updated_at or project.get("updated_at") or utc_now())
        db.execute(
            """
            INSERT INTO search_index_state(project_id, project_updated_at, indexed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                project_updated_at = excluded.project_updated_at,
                indexed_at = excluded.indexed_at
            """,
            (project_id, updated_at, utc_now()),
        )

    def rebuild_search_index(self, project_id: str | None = None) -> int:
        """Rebuild derived search data and return the number of indexed projects."""
        if not self.fts_enabled:
            return 0
        with self.lock, self._connect() as db:
            if project_id:
                rows = db.execute(
                    "SELECT payload, updated_at FROM projects WHERE id = ?", (project_id,)
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT payload, updated_at FROM projects"
                ).fetchall()
            for row in rows:
                self._rebuild_search_index(
                    db,
                    ensure_project_defaults(json.loads(row["payload"])),
                    str(row["updated_at"]),
                )
        return len(rows)

    def search_project(
        self,
        project_id: str,
        query: str,
        current_chapter_number: int,
        limit: int = 24,
    ) -> list[dict[str, Any]]:
        """Search only story state that existed before the chapter being written."""
        if not self.fts_enabled:
            return []
        match_query = _fts_match_query(query)
        if not match_query:
            return []
        current_number = max(1, int(current_chapter_number or 1))
        safe_limit = min(80, max(1, int(limit or 24)))
        try:
            with self._connect() as db:
                rows = db.execute(
                    """
                    SELECT source_id, kind, title, content, tags, chapter_number,
                           valid_from, valid_until, visibility,
                           bm25(search_documents_fts) AS rank
                    FROM search_documents_fts
                    WHERE search_documents_fts MATCH ?
                      AND project_id = ?
                      AND (CAST(chapter_number AS INTEGER) = 0
                           OR CAST(chapter_number AS INTEGER) < ?)
                      AND (CAST(valid_from AS INTEGER) = 0
                           OR CAST(valid_from AS INTEGER) <= ?)
                      AND (CAST(valid_until AS INTEGER) = 0
                           OR CAST(valid_until AS INTEGER) >= ?)
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (
                        match_query,
                        project_id,
                        current_number,
                        current_number,
                        current_number,
                        safe_limit,
                    ),
                ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [
            {
                "source_id": str(row["source_id"]),
                "kind": str(row["kind"]),
                "title": str(row["title"]),
                "content": str(row["content"]),
                "tags": str(row["tags"]),
                "chapter_number": int(row["chapter_number"] or 0),
                "valid_from": int(row["valid_from"] or 0),
                "valid_until": int(row["valid_until"] or 0),
                "visibility": str(row["visibility"] or "objective"),
                "rank": float(row["rank"] or 0.0),
                "origin": "fts5",
            }
            for row in rows
        ]

    def backup(
        self, directory: Path, keep: int = 12, *, prefix: str = "inkforge"
    ) -> Path:
        if not re.fullmatch(r"inkforge(?:-[a-z]+)?", prefix):
            raise ValueError("Invalid backup prefix")
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = directory / f"{prefix}-{stamp}.db"
        with self.lock:
            self._backup_to(destination)
        backups = sorted(
            (
                path
                for path in directory.glob(f"{prefix}-*.db")
                if not path.name.startswith("inkforge-before-restore-")
            ),
            reverse=True,
        )
        for old_backup in backups[max(1, keep):]:
            old_backup.unlink(missing_ok=True)
        return destination

    def _backup_to(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as source:
            source.execute("PRAGMA wal_checkpoint(PASSIVE)")
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
                target.commit()

    @staticmethod
    def _replace_database(source_path: Path, destination_path: Path) -> None:
        with closing(sqlite3.connect(source_path)) as source:
            source.row_factory = sqlite3.Row
            integrity = str(source.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity.lower() != "ok":
                raise ValueError(f"备份完整性校验失败：{integrity}")
            with closing(sqlite3.connect(destination_path, timeout=10)) as destination:
                destination.execute("PRAGMA busy_timeout = 10000")
                source.backup(destination)
                destination.commit()

    def inspect_backup(self, path: Path) -> dict[str, Any]:
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(path.name)
        result: dict[str, Any] = {
            "filename": path.name,
            "size_bytes": path.stat().st_size,
            "modified_at": datetime.fromtimestamp(
                path.stat().st_mtime, timezone.utc
            ).isoformat(),
            "integrity": "invalid",
            "compatible": False,
            "projects": [],
            "counts": {},
            "warnings": [],
            "schema_version": 0,
            "supported_schema_version": DATABASE_SCHEMA_VERSION,
            "migration_required": False,
        }
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        result["sha256"] = digest.hexdigest()
        try:
            with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as db:
                db.row_factory = sqlite3.Row
                integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
                result["integrity"] = integrity
                schema_version = int(db.execute("PRAGMA user_version").fetchone()[0])
                result["schema_version"] = schema_version
                result["migration_required"] = (
                    schema_version < DATABASE_SCHEMA_VERSION
                )
                if schema_version > DATABASE_SCHEMA_VERSION:
                    result["warnings"].append(
                        f"备份数据库版本为 {schema_version}，当前程序只支持到 "
                        f"{DATABASE_SCHEMA_VERSION}；请升级砚火后再恢复"
                    )
                tables = {
                    str(row["name"])
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                required = {"projects", "revisions"}
                missing = sorted(required - tables)
                if missing:
                    result["warnings"].append(
                        "缺少必要数据表：" + "、".join(missing)
                    )
                    return result
                projects = []
                invalid_payloads = 0
                for row in db.execute(
                    "SELECT id, title, payload, updated_at FROM projects "
                    "ORDER BY updated_at DESC"
                ).fetchall():
                    try:
                        payload = json.loads(row["payload"])
                        chapters = [
                            item
                            for item in payload.get("chapters", [])
                            if isinstance(item, dict)
                        ]
                        character_count = sum(
                            len(str(item.get("content", "")).strip())
                            for item in chapters
                        )
                    except (TypeError, ValueError, json.JSONDecodeError):
                        chapters = []
                        character_count = 0
                        invalid_payloads += 1
                    projects.append(
                        {
                            "id": str(row["id"]),
                            "title": str(row["title"]),
                            "updated_at": str(row["updated_at"]),
                            "chapters": len(chapters),
                            "characters": character_count,
                        }
                    )
                if invalid_payloads:
                    result["warnings"].append(
                        f"有 {invalid_payloads} 个作品数据无法解析"
                    )
                def count(table: str) -> int:
                    return int(
                        db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    )
                result["projects"] = projects
                result["counts"] = {
                    "projects": len(projects),
                    "revisions": count("revisions"),
                    "chapter_versions": count("chapter_versions")
                    if "chapter_versions" in tables
                    else 0,
                    "director_tasks": count("director_tasks")
                    if "director_tasks" in tables
                    else 0,
                }
                result["compatible"] = (
                    integrity.lower() == "ok"
                    and invalid_payloads == 0
                    and schema_version <= DATABASE_SCHEMA_VERSION
                )
        except sqlite3.DatabaseError as exc:
            result["warnings"].append(f"无法读取 SQLite 备份：{exc}")
        return result

    def restore_database_backup(
        self, source_path: Path, safety_directory: Path
    ) -> dict[str, Any]:
        source_path = source_path.resolve()
        preview = self.inspect_backup(source_path)
        if not preview.get("compatible"):
            detail = "；".join(preview.get("warnings", [])) or str(
                preview.get("integrity", "未知错误")
            )
            raise ValueError(f"该备份不能安全恢复：{detail}")
        safety_directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safety_path = safety_directory / f"inkforge-before-restore-{stamp}.db"
        with self.lock:
            self._backup_to(safety_path)
            try:
                self._replace_database(source_path, self.path)
                self._init()
                restored = self.inspect_backup(self.path)
                if not restored.get("compatible"):
                    raise ValueError("恢复后的数据库未通过完整性校验")
            except Exception:
                self._replace_database(safety_path, self.path)
                self._init()
                raise
        safety_backups = sorted(
            safety_directory.glob("inkforge-before-restore-*.db"), reverse=True
        )
        for old_backup in safety_backups[5:]:
            old_backup.unlink(missing_ok=True)
        return {
            "ok": True,
            "restored_from": source_path.name,
            "safety_backup": safety_path.name,
            "preview": restored,
        }

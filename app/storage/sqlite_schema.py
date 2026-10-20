"""SQLite connection handling, table creation and in-place schema migrations.

Mixed into :class:`app.storage.project_store.ProjectStore`; methods rely on
the store's ``path``, ``lock``, ``secret_store`` and ``_connect``."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager, closing
from datetime import datetime
from typing import Iterator

from app.storage.secret_store import (
    extract_project_secrets,
    strip_project_secrets,
)
from app.storage.schema.project import ensure_project_defaults


DATABASE_SCHEMA_VERSION = 2


class DatabaseVersionError(RuntimeError):
    """Raised before touching a database created by a newer application."""


class SqliteSchemaMixin:
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

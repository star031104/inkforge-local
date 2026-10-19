"""Auxiliary per-project records that must not ride inside the project JSON.

Reader-simulation reports, the publishing schedule, per-chapter publish status
and manuscript-import job state change on their own timeline.  Keeping them in
the project payload would make every background write conflict with the
browser's optimistic-concurrency saves.  They live in a small side table of the
same SQLite file, so database backups and restores still carry them.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable
from typing import Any

_INSTANCES: dict[str, "ExtensionStore"] = {}
_INSTANCES_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExtensionStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self._ensure()

    @classmethod
    def for_store(cls, store: Any) -> "ExtensionStore":
        """Return the extension store that shares ``store``'s database file."""
        path = Path(getattr(store, "path"))
        key = str(path.resolve())
        with _INSTANCES_LOCK:
            instance = _INSTANCES.get(key)
            if instance is None:
                instance = cls(path)
                _INSTANCES[key] = instance
            else:
                # A restore may have replaced the file; recreate the table.
                instance._ensure()
            return instance

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _ensure(self) -> None:
        with self.lock, closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS project_extensions ("
                "project_id TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, PRIMARY KEY(project_id, key))"
            )

    def get(self, project_id: str, key: str, default: Any = None) -> Any:
        with self.lock, closing(self._connect()) as db:
            try:
                row = db.execute(
                    "SELECT payload FROM project_extensions WHERE project_id = ? AND key = ?",
                    (str(project_id), str(key)),
                ).fetchone()
            except sqlite3.OperationalError:
                self._ensure()
                return default
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def put(self, project_id: str, key: str, value: Any) -> Any:
        payload = json.dumps(value, ensure_ascii=False)
        with self.lock, closing(self._connect()) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS project_extensions ("
                "project_id TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, PRIMARY KEY(project_id, key))"
            )
            db.execute(
                "INSERT INTO project_extensions(project_id, key, payload, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(project_id, key) DO UPDATE SET "
                "payload = excluded.payload, updated_at = excluded.updated_at",
                (str(project_id), str(key), payload, _now()),
            )
        return value

    def update(self, project_id: str, key: str, mutate: Callable[[Any], Any], default: Any = None) -> Any:
        """Atomically read-modify-write one record.

        ``mutate`` receives the current value (or ``default``) and returns the
        value to store; returning ``None`` leaves the record untouched.  The
        whole cycle runs under the store lock inside one immediate transaction,
        so a concurrent ``put`` can neither interleave nor be overwritten with
        a stale copy.
        """
        with self.lock, closing(self._connect()) as db:
            db.isolation_level = None
            db.execute(
                "CREATE TABLE IF NOT EXISTS project_extensions ("
                "project_id TEXT NOT NULL, key TEXT NOT NULL, payload TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, PRIMARY KEY(project_id, key))"
            )
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT payload FROM project_extensions WHERE project_id = ? AND key = ?",
                    (str(project_id), str(key)),
                ).fetchone()
                current = default
                if row:
                    try:
                        current = json.loads(row[0])
                    except json.JSONDecodeError:
                        current = default
                value = mutate(current)
                if value is not None:
                    db.execute(
                        "INSERT INTO project_extensions(project_id, key, payload, updated_at) "
                        "VALUES (?, ?, ?, ?) ON CONFLICT(project_id, key) DO UPDATE SET "
                        "payload = excluded.payload, updated_at = excluded.updated_at",
                        (str(project_id), str(key), json.dumps(value, ensure_ascii=False), _now()),
                    )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return value if value is not None else current

    def delete(self, project_id: str, key: str | None = None) -> None:
        with self.lock, closing(self._connect()) as db, db:
            if key is None:
                db.execute(
                    "DELETE FROM project_extensions WHERE project_id = ?", (str(project_id),)
                )
            else:
                db.execute(
                    "DELETE FROM project_extensions WHERE project_id = ? AND key = ?",
                    (str(project_id), str(key)),
                )

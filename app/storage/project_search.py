"""Rebuildable SQLite FTS index over project documents.

Mixed into :class:`app.storage.project_store.ProjectStore`; methods rely on
the store's ``path``, ``lock``, ``secret_store`` and ``_connect``."""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from app.storage.search_index import (
    _fts_match_query,
    _project_search_documents,
)
from app.storage.schema.project import ensure_project_defaults
from app.core.clock import utc_now


class ProjectSearchMixin:
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

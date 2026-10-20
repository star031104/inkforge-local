"""Redacted prompt-context snapshots and per-chapter conversation sessions.

Mixed into :class:`app.storage.project_store.ProjectStore`; methods rely on
the store's ``path``, ``lock``, ``secret_store`` and ``_connect``."""
from __future__ import annotations

import json
import hashlib
import uuid
from typing import Any

from app.storage.search_index import (
    _sanitize_snapshot_value,
)
from app.core.clock import utc_now


class ContextSnapshotStoreMixin:
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

"""The project repository: projects, revisions, chapter versions and user writing skills.

The SQLite concerns that are not about the project document itself live in
sibling mixin modules (schema migrations, director tasks, snapshots and
sessions, full-text search, database backups)."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from app.storage.schema.writing_skills import normalize_writing_skill
from app.story.story_systems import bump_authority, content_hash
from app.storage.project_service import ProjectConflictError
from app.story.temporal_context import invalidate_changed_manuscript
from app.storage.secret_store import (
    ProjectSecretStore,
    apply_project_secrets,
    extract_project_secrets,
    strip_project_secrets,
)
from app.storage.schema.project import default_project, ensure_project_defaults
from app.core.clock import utc_now
from app.storage.database_backup import DatabaseBackupMixin
from app.storage.director_tasks import DirectorTaskStoreMixin
from app.storage.project_search import ProjectSearchMixin
from app.storage.snapshots import ContextSnapshotStoreMixin
from app.storage.sqlite_schema import SqliteSchemaMixin


class ProjectStore(
    SqliteSchemaMixin,
    DirectorTaskStoreMixin,
    ContextSnapshotStoreMixin,
    ProjectSearchMixin,
    DatabaseBackupMixin,
):
    """SQLite repository for projects, revisions and auxiliary workflow state."""

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
                # Loading migrates old settings in memory (e.g. pre-0.34 model
                # slots, whose second key is consumed while choosing the
                # endpoint).  Replacing the secrets below drops those legacy
                # keys, so the migrated settings must be persisted with them;
                # otherwise the next load re-migrates without the key and
                # pairs the wrong endpoint with the remaining key.
                raw = db.execute("SELECT payload FROM projects WHERE id = ?", (project_id,)).fetchone()
                migrated_json = json.dumps(existing_stored, ensure_ascii=False)
                if raw is not None and raw["payload"] != migrated_json:
                    db.execute(
                        "UPDATE projects SET payload = ? WHERE id = ?",
                        (migrated_json, project_id),
                    )
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
            try:
                db.execute("DELETE FROM project_extensions WHERE project_id = ?", (project_id,))
            except sqlite3.OperationalError:
                pass
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



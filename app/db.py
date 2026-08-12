from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .planning import empty_planning, ensure_planning_defaults


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProjectStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _init(self) -> None:
        with self._connect() as db:
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
                    updated_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_director_tasks_project "
                "ON director_tasks(project_id, updated_at DESC)"
            )
            db.execute(
                "UPDATE director_tasks SET status = 'paused', "
                "updated_at = ? WHERE status IN ('queued', 'running', 'stopping')",
                (utc_now(),),
            )

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
        return ensure_project_defaults(json.loads(row["payload"])) if row else None

    def create(self, title: str = "未命名故事") -> dict[str, Any]:
        project_id = str(uuid.uuid4())
        now = utc_now()
        payload = default_project(project_id, title, now)
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO projects(id, title, payload, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (project_id, title, json.dumps(payload, ensure_ascii=False), now, now),
            )
        return payload

    def save(
        self, project_id: str, payload: dict[str, Any], reason: str = "autosave"
    ) -> dict[str, Any]:
        existing = self.get(project_id)
        if not existing:
            raise KeyError(project_id)
        clean = ensure_project_defaults(deepcopy(payload))
        clean["id"] = project_id
        clean["created_at"] = existing.get("created_at", utc_now())
        clean["updated_at"] = utc_now()
        clean.setdefault("title", "未命名故事")
        with self.lock, self._connect() as db:
            previous_compare = deepcopy(existing)
            clean_compare = deepcopy(clean)
            previous_compare.pop("updated_at", None)
            clean_compare.pop("updated_at", None)
            previous_json = json.dumps(
                previous_compare, ensure_ascii=False, sort_keys=True
            )
            new_json = json.dumps(clean_compare, ensure_ascii=False, sort_keys=True)
            if previous_json != new_json:
                db.execute(
                    "INSERT INTO revisions(project_id, payload, created_at, reason) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        project_id,
                        json.dumps(existing, ensure_ascii=False),
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
            db.execute(
                "UPDATE projects SET title = ?, payload = ?, updated_at = ? WHERE id = ?",
                (
                    clean["title"],
                    json.dumps(clean, ensure_ascii=False),
                    clean["updated_at"],
                    project_id,
                ),
            )
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
        return clean

    def delete(self, project_id: str) -> bool:
        with self.lock, self._connect() as db:
            cursor = db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            db.execute("DELETE FROM revisions WHERE project_id = ?", (project_id,))
            db.execute("DELETE FROM chapter_versions WHERE project_id = ?", (project_id,))
            db.execute("DELETE FROM director_tasks WHERE project_id = ?", (project_id,))
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
                "SELECT payload, status, updated_at FROM director_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["status"] = row["status"]
        payload["updated_at"] = row["updated_at"]
        return payload

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
        return self.save(
            project_id, json.loads(row["payload"]), reason=f"restore:{revision_id}"
        )

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
        return clean

    def backup(self, directory: Path, keep: int = 12) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = directory / f"inkforge-{stamp}.db"
        with self.lock, self._connect() as source:
            source.execute("PRAGMA wal_checkpoint(PASSIVE)")
            with sqlite3.connect(destination) as target:
                source.backup(target)
        backups = sorted(directory.glob("inkforge-*.db"), reverse=True)
        for old_backup in backups[max(1, keep):]:
            old_backup.unlink(missing_ok=True)
        return destination


def default_project(project_id: str, title: str, now: str) -> dict[str, Any]:
    return ensure_project_defaults({
        "id": project_id,
        "title": title,
        "genre": "幻想",
        "story_mode": "long",
        "premise": "",
        "outline": "",
        "author_intent": "",
        "current_focus": "",
        "book_rules": "",
        "author_note": "",
        "memory": {
            "state_version": 2,
            "story_so_far": "",
            "facts": [],
            "plot_threads": [],
            "timeline": [],
            "relationships": [],
            "continuity_notes": [],
            "description_ledger": [],
        },
        "narrative": {
            "pov": "auto",
            "tense": "auto",
            "tone": "",
            "central_question": "",
            "ending_direction": "",
            "current_arc": "",
            "target_chapters": 30,
        },
        "planning": empty_planning(),
        "created_at": now,
        "updated_at": now,
        "settings": {
            "base_url": "http://127.0.0.1:8080/v1",
            "api_key": "no-key",
            "model": "",
            "temperature": 0.82,
            "top_p": 0.92,
            "top_k": 40,
            "min_p": 0.05,
            "repeat_penalty": 1.08,
            "enable_thinking": False,
            "max_tokens": 1800,
            "context_budget": 24000,
            "recent_chars": 12000,
            "target_words": 1200,
            "memory_items": 12,
            "lore_budget": 4500,
            "lore_recursion_steps": 2,
        },
        "style": {
            "name": "默认文风",
            "sample": "",
            "profile": "",
            "dos": [],
            "donts": [],
        },
        "characters": [],
        "world_entries": [],
        "chapters": [
            {
                "id": str(uuid.uuid4()),
                "title": "第一章",
                "summary": "",
                "content": "",
                "scene_goal": "",
                "author_note": "",
                "plan": {
                    "goal": "",
                    "conflict": "",
                    "must_keep": [],
                    "must_avoid": [],
                    "turning_point": "",
                    "ending_hook": "",
                    "chapter_type": "",
                    "pov_character": "",
                    "time_location": "",
                    "opening_beat": "",
                    "scene_beats": [],
                    "emotional_turn": "",
                    "thread_actions": [],
                    "exit_state": "",
                    "ending_type": "",
                },
            }
        ],
    })


def ensure_project_defaults(project: dict[str, Any]) -> dict[str, Any]:
    """Migrate older saved projects without destructive schema rewrites."""
    if not isinstance(project, dict):
        project = {}
    project.setdefault("id", "")
    project.setdefault("title", "未命名故事")
    project.setdefault("genre", "")
    project.setdefault("premise", "")
    project.setdefault("outline", "")
    project.setdefault("author_intent", "")
    project.setdefault("current_focus", "")
    project.setdefault("book_rules", "")
    project.setdefault("author_note", "")
    project.setdefault("story_mode", "long")
    if not isinstance(project.get("memory"), dict):
        project["memory"] = {}
    memory = project["memory"]
    memory.setdefault("state_version", 2)
    memory.setdefault("story_so_far", "")
    for key in (
        "facts",
        "plot_threads",
        "timeline",
        "relationships",
        "continuity_notes",
        "description_ledger",
    ):
        if not isinstance(memory.get(key), list):
            memory[key] = []
    memory["facts"] = [
        (
            item
            if isinstance(item, dict)
            else {
                "id": str(uuid.uuid4()),
                "text": str(item),
                "tags": [],
                "importance": 3,
                "active": True,
            }
        )
        for item in memory["facts"]
        if isinstance(item, dict) or str(item).strip()
    ]
    for item in memory["facts"]:
        item.setdefault("id", str(uuid.uuid4()))
        item.setdefault("text", "")
        item.setdefault("tags", [])
        if isinstance(item.get("tags"), str):
            item["tags"] = [
                value.strip()
                for value in re.split(r"[,，\n]", item["tags"])
                if value.strip()
            ]
        elif not isinstance(item.get("tags"), list):
            item["tags"] = []
        item["importance"] = min(5, max(1, int(item.get("importance", 3) or 3)))
        item.setdefault("active", True)
        item.setdefault("confidence", "confirmed")
        item.setdefault("visibility", "objective")
        item.setdefault("evidence", "")
        item.setdefault("evidence_verified", False)
        item.setdefault("source_chapter_id", item.get("chapter_id", ""))
        item.setdefault("source_chapter_title", "")
        item.setdefault("valid_from_chapter", 0)
        item.setdefault("valid_until_chapter", 0)
        item.setdefault("supersedes_id", "")
    memory["plot_threads"] = [
        (
            item
            if isinstance(item, dict)
            else {
                "id": str(uuid.uuid4()),
                "title": str(item),
                "status": "open",
                "latest": "",
            }
        )
        for item in memory["plot_threads"]
        if isinstance(item, dict) or str(item).strip()
    ]
    thread_status_aliases = {
        "resolved": "closed", "close": "closed", "已回收": "closed",
        "advanced": "progressing", "推进": "progressing", "持续推进": "progressing",
        "paused": "deferred", "hold": "deferred", "延后": "deferred",
        "payoff_ready": "ready", "可回收": "ready",
    }
    for item in memory["plot_threads"]:
        item.setdefault("id", str(uuid.uuid4()))
        item.setdefault("title", "")
        status = str(item.get("status", "open")).strip().casefold()
        status = thread_status_aliases.get(status, status)
        item["status"] = (
            status
            if status in {"open", "progressing", "deferred", "ready", "closed"}
            else "open"
        )
        item.setdefault("type", "mystery")
        item.setdefault("setup", item.get("latest", ""))
        item.setdefault("latest", item.get("setup", ""))
        item.setdefault("expected_payoff", item.get("payoff", ""))
        item.setdefault("payoff_condition", "")
        item.setdefault("payoff", "")
        item.setdefault("target_window", "mid")
        item.setdefault("stakeholders", [])
        item.setdefault("knowledge_holders", [])
        for key in ("stakeholders", "knowledge_holders"):
            if isinstance(item.get(key), str):
                item[key] = [
                    value.strip()
                    for value in re.split(r"[,，\n]", item[key])
                    if value.strip()
                ]
            elif not isinstance(item.get(key), list):
                item[key] = []
        item.setdefault("created_chapter_number", 0)
        item.setdefault("last_advanced_chapter", 0)
        item.setdefault("closed_chapter_number", 0)
        item.setdefault("chapter_id", item.get("source_chapter_id", ""))
        item.setdefault("last_chapter_id", item.get("chapter_id", ""))
        item.setdefault("evidence", "")
        item.setdefault("evidence_verified", False)
    memory["timeline"] = [
        (
            item
            if isinstance(item, dict)
            else {
                "id": str(uuid.uuid4()),
                "time": "",
                "event": str(item),
            }
        )
        for item in memory["timeline"]
        if isinstance(item, dict) or str(item).strip()
    ]
    for item in memory["timeline"]:
        item.setdefault("id", str(uuid.uuid4()))
        item.setdefault("time", "")
        item.setdefault("event", "")
        item.setdefault("chapter_id", "")
        item.setdefault("chapter_number", 0)
        item.setdefault("location", "")
        for key in ("participants", "causes", "effects"):
            item.setdefault(key, [])
            if isinstance(item.get(key), str):
                item[key] = [
                    value.strip()
                    for value in re.split(r"[,，\n]", item[key])
                    if value.strip()
                ]
            elif not isinstance(item.get(key), list):
                item[key] = []
    memory["relationships"] = [
        item for item in memory["relationships"] if isinstance(item, dict)
    ]
    for item in memory["relationships"]:
        item.setdefault("id", str(uuid.uuid4()))
        item.setdefault("left", "")
        item.setdefault("right", "")
        item.setdefault("state", "")
        item.setdefault("tension", "")
        item.setdefault("trust", "")
        item.setdefault("knowledge_gap", "")
        item.setdefault("active", True)
        item.setdefault("source_chapter_id", "")
        item.setdefault("last_chapter_number", 0)
    memory["continuity_notes"] = [
        (
            item
            if isinstance(item, dict)
            else {
                "id": str(uuid.uuid4()),
                "text": str(item),
                "resolved": False,
            }
        )
        for item in memory["continuity_notes"]
        if isinstance(item, dict) or str(item).strip()
    ]
    memory["description_ledger"] = [
        item for item in memory["description_ledger"] if isinstance(item, dict)
    ][-300:]
    for item in memory["description_ledger"]:
        item.setdefault("id", str(uuid.uuid4()))
        item.setdefault("character", "")
        item.setdefault("aspect", "描写")
        item.setdefault("phrase", "")
        item.setdefault("chapter_id", "")
    for note in memory["continuity_notes"]:
        note.setdefault("id", str(uuid.uuid4()))
        note.setdefault("text", "")
        note.setdefault("resolved", False)
        note.setdefault("chapter_id", "")
        note.setdefault("chapter_title", "")
    if not isinstance(project.get("narrative"), dict):
        project["narrative"] = {}
    narrative = project["narrative"]
    narrative.setdefault("pov", "auto")
    narrative.setdefault("tense", "auto")
    narrative.setdefault("tone", "")
    narrative.setdefault("central_question", "")
    narrative.setdefault("ending_direction", "")
    narrative.setdefault("current_arc", "")
    narrative.setdefault("target_chapters", 30)
    project["planning"] = ensure_planning_defaults(project.get("planning"))
    if not isinstance(project.get("settings"), dict):
        project["settings"] = {}
    settings = project["settings"]
    settings.setdefault("base_url", "http://127.0.0.1:8080/v1")
    settings.setdefault("api_key", "no-key")
    settings.setdefault("model", "")
    settings.setdefault("temperature", 0.82)
    settings.setdefault("top_p", 0.92)
    settings.setdefault("max_tokens", 1800)
    settings.setdefault("context_budget", 24000)
    settings.setdefault("target_words", 1200)
    settings.setdefault("memory_items", 12)
    settings.setdefault("lore_budget", 4500)
    settings.setdefault("lore_recursion_steps", 2)
    settings.setdefault("recent_chars", 12000)
    settings.setdefault("top_k", 40)
    settings.setdefault("min_p", 0.05)
    settings.setdefault("repeat_penalty", 1.08)
    settings.setdefault("enable_thinking", False)
    if not isinstance(project.get("style"), dict):
        project["style"] = {}
    style = project["style"]
    style.setdefault("name", "默认文风")
    style.setdefault("sample", "")
    style.setdefault("profile", "")
    if not isinstance(style.get("dos"), list):
        style["dos"] = []
    if not isinstance(style.get("donts"), list):
        style["donts"] = []
    if not isinstance(project.get("characters"), list):
        project["characters"] = []
    if not isinstance(project.get("world_entries"), list):
        project["world_entries"] = []
    if not isinstance(project.get("chapters"), list):
        project["chapters"] = []
    project["characters"] = [
        item for item in project["characters"] if isinstance(item, dict)
    ]
    project["world_entries"] = [
        item for item in project["world_entries"] if isinstance(item, dict)
    ]
    project["chapters"] = [
        item for item in project["chapters"] if isinstance(item, dict)
    ]
    if not project["chapters"]:
        project["chapters"].append(
            {
                "id": str(uuid.uuid4()),
                "title": "第一章",
                "summary": "",
                "content": "",
                "scene_goal": "",
                "plan": {},
            }
        )
    legacy_author_note = str(project.get("author_note", "")).strip()
    for chapter in project["chapters"]:
        chapter.setdefault("id", str(uuid.uuid4()))
        chapter.setdefault("title", "未命名章节")
        chapter.setdefault("content", "")
        chapter.setdefault("summary", "")
        chapter.setdefault("scene_goal", "")
        chapter.setdefault("author_note", "")
        if not isinstance(chapter.get("settlement"), dict):
            chapter["settlement"] = {}
        if not isinstance(chapter.get("execution"), dict):
            chapter["execution"] = {}
        chapter["execution"].setdefault("status", "never_run")
        chapter["execution"].setdefault("last_run_at", "")
        chapter["execution"].setdefault("model", "")
        chapter["execution"].setdefault("audit_score", None)
        chapter["execution"].setdefault("audit_verdict", "")
        chapter["execution"].setdefault("revision_attempts", 0)
        chapter["execution"].setdefault("issues", [])
        chapter["execution"].setdefault("warnings", [])
        if not isinstance(chapter.get("run_history"), list):
            chapter["run_history"] = []
        if not isinstance(chapter.get("plan"), dict):
            chapter["plan"] = {}
        plan = chapter["plan"]
        plan.setdefault("goal", "")
        plan.setdefault("conflict", "")
        plan.setdefault("must_keep", [])
        plan.setdefault("must_avoid", [])
        plan.setdefault("turning_point", "")
        plan.setdefault("ending_hook", "")
        plan.setdefault("chapter_type", "")
        plan.setdefault("pov_character", "")
        plan.setdefault("time_location", "")
        plan.setdefault("opening_beat", "")
        plan.setdefault("scene_beats", [])
        plan.setdefault("emotional_turn", "")
        plan.setdefault("thread_actions", [])
        plan.setdefault("exit_state", "")
        plan.setdefault("ending_type", "")
        if not isinstance(plan.get("scene_beats"), list):
            plan["scene_beats"] = []
        if not isinstance(plan.get("thread_actions"), list):
            plan["thread_actions"] = []
        if isinstance(chapter.get("route"), dict):
            route = chapter["route"]
            route.setdefault("id", str(uuid.uuid4()))
            route.setdefault("number", 1)
            route.setdefault("title", chapter.get("title", ""))
            route.setdefault("goal", "")
            route.setdefault("conflict", "")
            route.setdefault("turning_point", "")
            route.setdefault("ending_hook", "")
            route.setdefault("must_keep", [])
            route.setdefault("must_avoid", [])
            route.setdefault("status", "planned")
    if legacy_author_note and not any(
        str(chapter.get("author_note", "")).strip()
        for chapter in project["chapters"]
    ):
        project["chapters"][0]["author_note"] = legacy_author_note
        project["author_note"] = ""
    for character in project["characters"]:
        character.setdefault("id", str(uuid.uuid4()))
        character.setdefault("name", "")
        character.setdefault("role", "")
        character.setdefault("description", "")
        character.setdefault("goal", "")
        character.setdefault("knowledge", "")
        character.setdefault("secrets", "")
        character.setdefault("voice", "")
        character.setdefault("location", "")
        character.setdefault("items", "")
        character.setdefault("emotion", "")
        character.setdefault("state", "")
        character.setdefault("aliases", [])
        if isinstance(character.get("aliases"), str):
            character["aliases"] = [
                item.strip()
                for item in re.split(r"[,，\n]", character["aliases"])
                if item.strip()
            ]
        character.setdefault("importance", "supporting")
        character.setdefault("active", True)
        character.setdefault("personality", character.get("description", ""))
        character.setdefault("appearance", "")
        character.setdefault("appearance_state", "")
        character.setdefault("values", "")
        character.setdefault("fears", "")
        character.setdefault("contradictions", "")
        character.setdefault("mannerisms", "")
        character.setdefault("relationships", "")
        character.setdefault("arc", "")
        character.setdefault("hard_limits", "")
        character.setdefault("dialogue_examples", [])
        if isinstance(character.get("dialogue_examples"), str):
            character["dialogue_examples"] = [
                item.strip()
                for item in character["dialogue_examples"].splitlines()
                if item.strip()
            ]
        character.setdefault("knowledge_ledger", [])
        if not isinstance(character.get("knowledge_ledger"), list):
            character["knowledge_ledger"] = []
        character["knowledge_ledger"] = [
            item
            for item in character["knowledge_ledger"]
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        ][-200:]
        for knowledge in character["knowledge_ledger"]:
            knowledge.setdefault("id", str(uuid.uuid4()))
            knowledge.setdefault("text", "")
            knowledge.setdefault("learned_how", "")
            knowledge.setdefault("certainty", "confirmed")
            knowledge.setdefault("source_chapter_id", "")
            knowledge.setdefault("source_chapter_title", "")
            knowledge.setdefault("chapter_number", 0)
            knowledge.setdefault("evidence", "")
            knowledge.setdefault("active", True)
    for entry in project["world_entries"]:
        entry.setdefault("id", str(uuid.uuid4()))
        entry.setdefault("title", "")
        keys = entry.get("keys", [])
        if isinstance(keys, str):
            entry["keys"] = [
                item.strip()
                for item in keys.replace("，", ",").split(",")
                if item.strip()
            ]
        elif not isinstance(keys, list):
            entry["keys"] = []
        entry.setdefault("content", "")
        entry.setdefault("position", "after")
        entry.setdefault("order", 100)
        entry.setdefault("constant", False)
        entry.setdefault("enabled", True)
        entry.setdefault("match", "any")
        entry.setdefault("secondary_keys", [])
        if isinstance(entry.get("secondary_keys"), str):
            entry["secondary_keys"] = [
                item.strip()
                for item in re.split(r"[,，\n]", entry["secondary_keys"])
                if item.strip()
            ]
        entry.setdefault("selective_logic", "and_any")
        entry.setdefault("case_sensitive", False)
        entry.setdefault("category", "世界设定")
        entry.setdefault("canon", "hard" if entry.get("constant") else "soft")
        entry.setdefault("character_names", [])
        if isinstance(entry.get("character_names"), str):
            entry["character_names"] = [
                item.strip()
                for item in re.split(r"[,，\n]", entry["character_names"])
                if item.strip()
            ]
        entry.setdefault("chapter_start", 0)
        entry.setdefault("chapter_end", 0)
        entry.setdefault("inclusion_group", "")
        entry.setdefault("non_recursable", False)
        entry.setdefault("prevent_recursion", False)
        entry.setdefault("delay_until_recursion", False)
    return project

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager, closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .planning import empty_planning, ensure_planning_defaults
from .knowledge import ensure_knowledge_defaults
from .references import ensure_reference_defaults
from .canon import ensure_fanfic_defaults
from .writing_skills import ensure_project_writing_skills, normalize_writing_skill
from .story_systems import bump_authority, content_hash, ensure_professional_defaults


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SEARCH_STOP_LEXEMES = {
    "一个", "一些", "这个", "那个", "他们", "她们", "我们", "你们",
    "自己", "已经", "可以", "没有", "不是", "什么", "怎么", "进行",
    "以及", "因为", "所以", "但是", "然后", "继续", "现在", "本章",
}


def _ordered_search_lexemes(text: str, *, maximum: int = 4000) -> list[str]:
    """Produce stable Chinese bigrams and Latin tokens for the FTS lexeme field."""
    raw = str(text or "").casefold()
    values: list[str] = []
    values.extend(re.findall(r"[a-z0-9_]{2,}", raw))
    for run in re.findall(r"[\u3400-\u9fff]+", raw):
        if len(run) == 1:
            values.append(run)
        else:
            values.extend(run[index : index + 2] for index in range(len(run) - 1))
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in _SEARCH_STOP_LEXEMES or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= maximum:
            break
    return result


def _fts_match_query(text: str) -> str:
    values = _ordered_search_lexemes(text, maximum=64)
    return " OR ".join(f'"{value}"' for value in values)


def _text_chunks(text: str, size: int = 900, overlap: int = 140) -> list[str]:
    clean = re.sub(r"[ \t]+", " ", str(text or "")).strip()
    if not clean:
        return []
    if len(clean) <= size:
        return [clean]
    chunks: list[str] = []
    start = 0
    step = max(1, size - overlap)
    while start < len(clean):
        end = min(len(clean), start + size)
        if end < len(clean):
            boundary = max(
                clean.rfind("\n", start + size // 2, end),
                clean.rfind("。", start + size // 2, end),
                clean.rfind("！", start + size // 2, end),
                clean.rfind("？", start + size // 2, end),
            )
            if boundary > start:
                end = boundary + 1
        chunk = clean[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(clean):
            break
        start = max(start + step, end - overlap)
    return chunks


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


_SNAPSHOT_SECRET_KEYS = {
    "api_key",
    "reasoning_api_key",
    "prose_api_key",
    "brave_api_key",
    "apikey",
    "authorization",
    "access_token",
    "refresh_token",
    "secret_key",
    "password",
}
_SNAPSHOT_SECRET_PATTERN = re.compile(
    r"(?i)\b(?:sk|rk|pk)-[a-z0-9_-]{16,}\b|\bBearer\s+[a-z0-9._~+/=-]{12,}"
)


def _sanitize_snapshot_value(value: Any, key: str = "") -> Any:
    if key.casefold() in _SNAPSHOT_SECRET_KEYS:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_snapshot_value(item_value, str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_snapshot_value(item) for item in value]
    if isinstance(value, str):
        return _SNAPSHOT_SECRET_PATTERN.sub("[REDACTED]", value)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def _project_search_documents(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Compile project truth into search documents without modifying the project."""
    documents: list[dict[str, Any]] = []
    chapter_numbers = {
        str(chapter.get("id", "")): index + 1
        for index, chapter in enumerate(project.get("chapters", []))
        if isinstance(chapter, dict)
    }

    def add(
        *,
        source_id: str,
        kind: str,
        title: str,
        content: str,
        tags: str = "",
        chapter_number: int = 0,
        valid_from: int = 0,
        valid_until: int = 0,
        visibility: str = "objective",
    ) -> None:
        clean_content = str(content or "").strip()
        if not clean_content:
            return
        searchable = " ".join((str(title or ""), clean_content, str(tags or "")))
        lexemes = " ".join(_ordered_search_lexemes(searchable))
        if not lexemes:
            return
        documents.append(
            {
                "source_id": str(source_id),
                "kind": str(kind),
                "title": str(title or ""),
                "content": clean_content,
                "tags": str(tags or ""),
                "lexemes": lexemes,
                "chapter_number": _safe_int(chapter_number),
                "valid_from": _safe_int(valid_from),
                "valid_until": _safe_int(valid_until),
                "visibility": str(visibility or "objective"),
            }
        )

    for index, chapter in enumerate(project.get("chapters", [])):
        if not isinstance(chapter, dict):
            continue
        number = index + 1
        chapter_id = str(chapter.get("id", "") or f"chapter-{number}")
        title = str(chapter.get("title", "") or f"第{number}章")
        add(
            source_id=f"{chapter_id}:summary",
            kind="chapter",
            title=title,
            content=str(chapter.get("summary", "")),
            chapter_number=number,
        )
        for chunk_index, chunk in enumerate(_text_chunks(chapter.get("content", ""))):
            add(
                source_id=f"{chapter_id}:passage:{chunk_index}",
                kind="passage",
                title=f"{title}·正文片段{chunk_index + 1}",
                content=chunk,
                chapter_number=number,
            )

    memory = project.get("memory", {})
    for fact in memory.get("facts", []):
        if not isinstance(fact, dict) or not fact.get("active", True):
            continue
        source_chapter = str(
            fact.get("source_chapter_id") or fact.get("chapter_id") or ""
        )
        add(
            source_id=str(fact.get("id", "")),
            kind="fact",
            title="事实",
            content=str(fact.get("text", "")),
            tags=" ".join(str(item) for item in fact.get("tags", [])),
            chapter_number=chapter_numbers.get(source_chapter, 0),
            valid_from=_safe_int(fact.get("valid_from_chapter")),
            valid_until=_safe_int(fact.get("valid_until_chapter")),
            visibility=str(fact.get("visibility", "objective")),
        )
    for thread in memory.get("plot_threads", []):
        if not isinstance(thread, dict) or str(thread.get("status", "open")) == "closed":
            continue
        content = "；".join(
            str(thread.get(key, ""))
            for key in (
                "setup", "latest", "expected_payoff", "payoff_condition", "payoff"
            )
            if thread.get(key)
        )
        tags = " ".join(
            str(item)
            for key in ("stakeholders", "knowledge_holders")
            for item in thread.get(key, [])
        )
        add(
            source_id=str(thread.get("id", "")),
            kind="thread",
            title=f"未结线索：{thread.get('title', '未命名')}",
            content=content or str(thread.get("title", "")),
            tags=tags,
            chapter_number=_safe_int(thread.get("last_advanced_chapter")),
        )
    for event in memory.get("timeline", []):
        if not isinstance(event, dict):
            continue
        content = (
            f"{event.get('time', '')}｜{event.get('location', '')}："
            f"{event.get('event', '')}"
        )
        tags = " ".join(
            str(item)
            for key in ("participants", "causes", "effects")
            for item in event.get(key, [])
        )
        chapter_number = _safe_int(event.get("chapter_number")) or chapter_numbers.get(
            str(event.get("chapter_id", "")), 0
        )
        add(
            source_id=str(event.get("id", "")),
            kind="timeline",
            title="时间线",
            content=content,
            tags=tags,
            chapter_number=chapter_number,
        )
    for relation in memory.get("relationships", []):
        if not isinstance(relation, dict) or not relation.get("active", True):
            continue
        content = (
            f"{relation.get('left', '')}—{relation.get('right', '')}："
            f"{relation.get('state', '')}；张力：{relation.get('tension', '')}；"
            f"信任：{relation.get('trust', '')}；信息差：{relation.get('knowledge_gap', '')}"
        )
        add(
            source_id=str(relation.get("id", "")),
            kind="relationship",
            title="关系状态",
            content=content,
            chapter_number=_safe_int(relation.get("last_chapter_number")),
        )
    for character in project.get("characters", []):
        if not isinstance(character, dict):
            continue
        name = str(character.get("name", "") or "未命名人物")
        for knowledge in character.get("knowledge_ledger", []):
            if not isinstance(knowledge, dict) or not knowledge.get("active", True):
                continue
            add(
                source_id=str(knowledge.get("id", "")),
                kind="character_knowledge",
                title=f"{name}的已知信息",
                content=str(knowledge.get("text", "")),
                tags=str(knowledge.get("learned_how", "")),
                chapter_number=_safe_int(knowledge.get("chapter_number"))
                or chapter_numbers.get(str(knowledge.get("source_chapter_id", "")), 0),
                visibility="character",
            )
    return documents


class ProjectStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.fts_enabled = False
        self._init()

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
            self._rebuild_search_index(db, payload, now)
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
            self._rebuild_search_index(db, clean, clean["updated_at"])
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
        db.execute(
            "DELETE FROM search_documents_fts WHERE project_id = ?", (project_id,)
        )
        documents = _project_search_documents(project)
        if documents:
            db.executemany(
                """
                INSERT INTO search_documents_fts(
                    project_id, source_id, kind, title, content, tags, lexemes,
                    chapter_number, valid_from, valid_until, visibility
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        project_id,
                        item["source_id"],
                        item["kind"],
                        item["title"],
                        item["content"],
                        item["tags"],
                        item["lexemes"],
                        item["chapter_number"],
                        item["valid_from"],
                        item["valid_until"],
                        item["visibility"],
                    )
                    for item in documents
                ],
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

    def backup(self, directory: Path, keep: int = 12) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        destination = directory / f"inkforge-{stamp}.db"
        with self.lock, self._connect() as source:
            source.execute("PRAGMA wal_checkpoint(PASSIVE)")
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
                target.commit()
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
        "production_spec": "",
        "author_note": "",
        "memory": {
            "state_version": 4,
            "epistemic_schema_version": 1,
            "story_so_far": "",
            "story_digest_candidate": {},
            "facts": [],
            "plot_threads": [],
            "timeline": [],
            "relationships": [],
            "continuity_notes": [],
            "description_ledger": [],
            "commits": [],
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
            "model_routing": "single",
            "provider": "zhipu",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "",
            "model": "glm-4.7-flash",
            "reasoning_provider": "modelscope",
            "reasoning_base_url": "https://api-inference.modelscope.cn/v1",
            "reasoning_api_key": "",
            "reasoning_model": "ZhipuAI/GLM-5.2",
            "temperature": 0.82,
            "top_p": 0.92,
            "top_k": 40,
            "min_p": 0.05,
            "repeat_penalty": 1.08,
            "enable_thinking": False,
            "thinking_budget": 0,
            "max_tokens": 3500,
            "context_budget": 24000,
            "recent_chars": 12000,
            "target_words": 1200,
            "memory_items": 12,
            "lore_budget": 4500,
            "lore_recursion_steps": 2,
            "creative_freedom": "balanced",
            "role_routes": {},
            "research": {
                "provider": "bing_rss",
                "searxng_url": "",
                "brave_api_key": "",
            },
        },
        "style": {
            "name": "默认文风",
            "sample": "",
            "profile": "",
            "dos": [],
            "donts": [],
            "source_ids": [],
        },
        "references": [],
        "knowledge": {"schema_version": 1, "entities": [], "facts": [], "relations": [], "review_queue": []},
        "fanfic": {
            "enabled": False,
            "mode": "canon",
            "source_universes": [],
            "policy": {
                "preserve_identity": True,
                "preserve_core_personality": True,
                "preserve_voice": True,
                "preserve_abilities": True,
                "require_causal_character_change": True,
                "unverified_ai_inference_is_hard_canon": False,
            },
        },
        "characters": [],
        "world_entries": [],
        "writing_skills": [],
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
    ensure_professional_defaults(project)
    project.setdefault("id", "")
    project.setdefault("title", "未命名故事")
    project.setdefault("genre", "")
    project.setdefault("premise", "")
    project.setdefault("outline", "")
    project.setdefault("author_intent", "")
    project.setdefault("current_focus", "")
    project.setdefault("book_rules", "")
    project.setdefault("production_spec", "")
    project.setdefault("author_note", "")
    project.setdefault("story_mode", "long")
    if not isinstance(project.get("must_contracts"), list):
        project["must_contracts"] = []
    if not isinstance(project.get("repair_queue"), list):
        project["repair_queue"] = []
    if not isinstance(project.get("memory"), dict):
        project["memory"] = {}
    memory = project["memory"]
    try:
        memory["state_version"] = max(4, int(memory.get("state_version", 0) or 0))
    except (TypeError, ValueError):
        memory["state_version"] = 4
    memory["epistemic_schema_version"] = max(
        1, _safe_int(memory.get("epistemic_schema_version"))
    )
    memory.setdefault("story_so_far", "")
    if not isinstance(memory.get("story_digest_candidate"), dict):
        memory["story_digest_candidate"] = {}
    for key in (
        "facts",
        "plot_threads",
        "timeline",
        "relationships",
        "continuity_notes",
        "description_ledger",
        "commits",
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
        if not isinstance(item.get("known_by"), list):
            item["known_by"] = []
        item["known_by"] = list(
            dict.fromkeys(
                str(value).strip()
                for value in item["known_by"]
                if str(value).strip()
            )
        )[:30]
        item.setdefault(
            "reader_known",
            bool(item.get("source_chapter_id") and item.get("evidence_verified")),
        )
        item.setdefault("author_only", False)
        item.setdefault("evidence", "")
        item.setdefault("evidence_verified", False)
        item.setdefault("source_chapter_id", item.get("chapter_id", ""))
        item.setdefault("source_chapter_title", "")
        item.setdefault("valid_from_chapter", 0)
        item.setdefault("valid_until_chapter", 0)
        item.setdefault("supersedes_id", "")
        item.setdefault(
            "source_type",
            "accepted_chapter"
            if item.get("source_chapter_id") and item.get("evidence_verified")
            else "legacy",
        )
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
        item.setdefault("evidence", "")
        item.setdefault("evidence_verified", False)
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
        item.setdefault("evidence", "")
        item.setdefault("evidence_verified", False)
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
    valid_commit_statuses = {
        "settlement_pending",
        "settlement_extracted",
        "committed",
        "state_degraded",
    }
    memory["commits"] = [
        item for item in memory["commits"] if isinstance(item, dict)
    ][-200:]
    for commit in memory["commits"]:
        commit.setdefault("id", str(uuid.uuid4()))
        commit.setdefault("chapter_id", "")
        commit.setdefault("chapter_title", "")
        commit.setdefault("content_hash", "")
        status = str(commit.get("status", "state_degraded"))
        commit["status"] = (
            status if status in valid_commit_statuses else "state_degraded"
        )
        commit["attempts"] = max(1, int(commit.get("attempts", 1) or 1))
        commit.setdefault("error", "")
        if not isinstance(commit.get("warnings"), list):
            commit["warnings"] = []
        commit.setdefault("created_at", "")
        commit.setdefault("updated_at", "")
        commit.setdefault("committed_at", "")
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
    raw_provider = str(settings.get("provider") or "").strip().lower()
    configured_base = str(settings.get("base_url") or "").strip()
    configured_base_lower = configured_base.lower()
    legacy_cloud = raw_provider in {"siliconflow", "xai"} or any(
        marker in configured_base_lower for marker in ("siliconflow", "api.x.ai")
    )
    if legacy_cloud:
        provider = "zhipu"
        settings["provider"] = provider
        settings["base_url"] = "https://open.bigmodel.cn/api/paas/v4"
        settings["model"] = "glm-4.7-flash"
        settings["api_key"] = ""
    elif "open.bigmodel.cn" in configured_base_lower:
        provider = "zhipu"
        settings["provider"] = provider
        settings["base_url"] = "https://open.bigmodel.cn/api/paas/v4"
    elif "api-inference.modelscope.cn" in configured_base_lower or raw_provider == "modelscope":
        provider = "modelscope"
        settings["provider"] = provider
        settings["base_url"] = "https://api-inference.modelscope.cn/v1"
    elif raw_provider == "zhipu":
        provider = "zhipu"
        settings["provider"] = provider
    elif any(marker in configured_base_lower for marker in ("127.0.0.1", "localhost")) and raw_provider != "openai_compatible":
        provider = "llama_cpp"
        settings["provider"] = provider
    elif raw_provider in {"llama_cpp", "openai_compatible", "modelscope"}:
        provider = raw_provider
        settings["provider"] = provider
    elif not raw_provider and not configured_base:
        provider = "zhipu"
        settings["provider"] = provider
    else:
        provider = "openai_compatible"
        settings["provider"] = provider
    provider_base_urls = {
        "zhipu": "https://open.bigmodel.cn/api/paas/v4",
        "modelscope": "https://api-inference.modelscope.cn/v1",
        "llama_cpp": "http://127.0.0.1:8080/v1",
        "openai_compatible": "http://127.0.0.1:8080/v1",
    }
    provider_models = {
        "zhipu": "glm-4.7-flash",
        "modelscope": "ZhipuAI/GLM-5.2",
        "llama_cpp": "",
        "openai_compatible": "",
    }
    settings.setdefault(
        "base_url",
        provider_base_urls.get(provider, "http://127.0.0.1:8080/v1"),
    )
    if "api_key" not in settings:
        settings["api_key"] = "no-key" if provider == "llama_cpp" else ""
    settings.setdefault("model", provider_models.get(provider, ""))
    if provider == "zhipu":
        if not str(settings.get("base_url") or "").strip():
            settings["base_url"] = provider_base_urls[provider]
        if not str(settings.get("model") or "").strip():
            settings["model"] = provider_models[provider]
        if str(settings.get("api_key") or "").strip() == "no-key":
            settings["api_key"] = ""
    routing_default = "single"
    routing = str(settings.get("model_routing") or routing_default).strip().lower()
    settings["model_routing"] = routing if routing in {"dual", "single"} else routing_default
    settings.setdefault("reasoning_provider", "modelscope")
    settings.setdefault("reasoning_base_url", "https://api-inference.modelscope.cn/v1")
    settings.setdefault("reasoning_api_key", "")
    settings.setdefault("reasoning_model", "ZhipuAI/GLM-5.2")
    settings.setdefault("temperature", 0.82)
    settings.setdefault("top_p", 0.92)
    settings.setdefault("max_tokens", 3500)
    settings.setdefault("context_budget", 24000)
    settings.setdefault("target_words", 1200)
    settings.setdefault("memory_items", 12)
    settings.setdefault("lore_budget", 4500)
    settings.setdefault("lore_recursion_steps", 2)
    creative_freedom = str(settings.get("creative_freedom") or "balanced").strip().lower()
    settings["creative_freedom"] = (
        creative_freedom
        if creative_freedom in {"strict", "balanced", "exploratory"}
        else "balanced"
    )
    settings.setdefault("recent_chars", 12000)
    settings.setdefault("top_k", 40)
    settings.setdefault("min_p", 0.05)
    settings.setdefault("repeat_penalty", 1.08)
    settings.setdefault("enable_thinking", False)
    settings.setdefault("thinking_budget", 0)
    if not isinstance(settings.get("role_routes"), dict):
        settings["role_routes"] = {}
    if not isinstance(settings.get("research"), dict):
        settings["research"] = {}
    settings["research"].setdefault("provider", "bing_rss")
    settings["research"].setdefault("searxng_url", "")
    settings["research"].setdefault("brave_api_key", "")
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
    if not isinstance(style.get("source_ids"), list):
        style["source_ids"] = []
    ensure_reference_defaults(project)
    ensure_knowledge_defaults(project)
    ensure_fanfic_defaults(project)
    ensure_project_writing_skills(project)
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
        chapter.setdefault(
            "memory_status",
            "committed" if chapter["settlement"] else "never_settled",
        )
        chapter.setdefault("memory_commit_id", "")
        chapter.setdefault("accepted_content_hash", "")
        legacy_locked = bool(chapter.get("accepted_content_hash")) or str(
            chapter.get("memory_status", "")
        ) == "committed"
        if str(chapter.get("authority_state", "")) not in {
            "candidate", "reviewed", "accepted", "locked"
        }:
            chapter["authority_state"] = "locked" if legacy_locked else "candidate"
        chapter.setdefault(
            "locked_content_hash",
            chapter.get("accepted_content_hash", "") if legacy_locked else "",
        )
        chapter.setdefault("locked_at", "")
        chapter.setdefault("locked_by", "")
        if not isinstance(chapter.get("lock_receipt"), dict):
            chapter["lock_receipt"] = {}
        if not isinstance(chapter.get("workflow"), dict):
            chapter["workflow"] = {"version": 1, "stages": {}}
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
            if not isinstance(knowledge.get("related_fact_ids"), list):
                knowledge["related_fact_ids"] = []
        if "knowledge_baseline" not in character:
            ledger_texts = {
                re.sub(r"\s+", "", str(item.get("text", ""))).casefold()
                for item in character["knowledge_ledger"]
                if str(item.get("text", "")).strip()
            }
            baseline_parts = [
                value.strip()
                for value in re.split(r"[；\n]", str(character.get("knowledge", "")))
                if value.strip()
                and re.sub(r"\s+", "", value).casefold() not in ledger_texts
            ]
            character["knowledge_baseline"] = "；".join(baseline_parts)
        character.setdefault("knowledge_baseline_chapter", 0)
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
    ensure_reference_defaults(project)
    ensure_knowledge_defaults(project)
    ensure_fanfic_defaults(project)
    return project

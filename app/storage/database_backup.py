"""Online database backups, backup inspection and verified restore.

Mixed into :class:`app.storage.project_store.ProjectStore`; methods rely on
the store's ``path``, ``lock``, ``secret_store`` and ``_connect``."""
from __future__ import annotations

import json
import re
import hashlib
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.storage.sqlite_schema import DATABASE_SCHEMA_VERSION


class DatabaseBackupMixin:
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

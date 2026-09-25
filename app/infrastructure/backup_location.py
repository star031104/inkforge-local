"""Persistent, user-selectable backup destination and health information."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any


class BackupLocation:
    def __init__(self, default_path: Path, database_path: Path, settings_path: Path):
        self.default_path = default_path.resolve()
        self.database_path = database_path.resolve()
        self.settings_path = settings_path.resolve()
        self._path = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> Path:
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            configured = str(payload.get("backup_path", "")).strip()
            if configured:
                return Path(configured).expanduser().resolve()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        return self.default_path

    @staticmethod
    def _write_probe(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".inkforge-write-test"
        probe.write_text("ok", encoding="ascii")
        probe.unlink(missing_ok=True)

    def set_path(self, value: str) -> dict[str, Any]:
        raw = str(value or "").strip()
        if not raw:
            candidate = self.default_path
        else:
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                raise ValueError("备份目录必须填写完整路径")
            candidate = candidate.resolve()
        if candidate == self.database_path.parent:
            raise ValueError("备份目录不能与作品数据库位于同一文件夹")
        self._write_probe(candidate)
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.settings_path.with_suffix(self.settings_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"backup_path": str(candidate)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.settings_path)
        self._path = candidate
        return self.describe()

    def describe(self) -> dict[str, Any]:
        writable = True
        error = ""
        try:
            self._write_probe(self._path)
        except OSError as exc:
            writable = False
            error = str(exc)
        same_volume = (
            self._path.drive.lower() == self.database_path.drive.lower()
            if self._path.drive and self.database_path.drive
            else self._path.anchor == self.database_path.anchor
        )
        backups = sorted(
            self._path.glob("inkforge-*.db") if self._path.is_dir() else [],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        latest = backups[0] if backups else None
        return {
            "path": str(self._path),
            "default_path": str(self.default_path),
            "writable": writable,
            "error": error,
            "same_volume_as_database": same_volume,
            "off_device_recommended": same_volume,
            "backup_count": len(backups),
            "latest_backup_at": (
                datetime.fromtimestamp(latest.stat().st_mtime, timezone.utc).isoformat()
                if latest
                else ""
            ),
        }


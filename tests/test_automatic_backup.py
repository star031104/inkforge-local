from __future__ import annotations

from pathlib import Path

from app.db import ProjectStore
from app.infrastructure.automatic_backup import AutomaticBackupService


def test_automatic_backup_runs_when_due_and_is_throttled(tmp_path: Path):
    store = ProjectStore(tmp_path / "projects.db", tmp_path / "secrets.db")
    store.create("自动备份测试")
    backup_root = tmp_path / "backups"
    service = AutomaticBackupService(
        lambda: store,
        lambda: backup_root,
        interval_seconds=60,
        keep=3,
    )

    first = service.run_once(now=1_000_000)
    assert first is not None
    assert first.name.startswith("inkforge-auto-")
    first.touch()
    first_time = first.stat().st_mtime

    assert service.run_once(now=first_time + 30) is None
    second = service.run_once(now=first_time + 61)
    assert second is not None
    assert second != first

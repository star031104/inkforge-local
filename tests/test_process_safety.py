from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from app.db import ProjectStore
from app.infrastructure.instance_lock import (
    ApplicationAlreadyRunningError,
    ApplicationInstanceLock,
)


def test_director_task_lease_has_one_live_owner(tmp_path):
    database = tmp_path / "projects.db"
    first = ProjectStore(database, tmp_path / "secrets.db")
    second = ProjectStore(database, tmp_path / "secrets.db")
    project = first.create("租约测试")
    task = first.create_director_task(project["id"], {"events": []})

    assert first.claim_director_task(task["id"], "process-a") is True
    assert second.claim_director_task(task["id"], "process-b") is False
    assert second.director_task_lease_active(task["id"]) is True

    expired = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    with sqlite3.connect(database) as db:
        db.execute(
            "UPDATE director_tasks SET lease_expires_at = ? WHERE id = ?",
            (expired, task["id"]),
        )
    assert second.claim_director_task(task["id"], "process-b") is True


def test_application_instance_lock_rejects_second_writer(tmp_path):
    path = tmp_path / "application.lock"
    first = ApplicationInstanceLock(path)
    second = ApplicationInstanceLock(path)
    first.acquire()
    try:
        with pytest.raises(ApplicationAlreadyRunningError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()

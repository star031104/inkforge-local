"""Persistent automatic-director tasks and their single-runner leases.

Mixed into :class:`app.storage.project_store.ProjectStore`; methods rely on
the store's ``path``, ``lock``, ``secret_store`` and ``_connect``."""
from __future__ import annotations

import json
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from app.core.clock import utc_now


class DirectorTaskStoreMixin:
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
                "SELECT payload, status, updated_at, owner_instance_id, "
                "lease_expires_at FROM director_tasks WHERE id = ?",
                (task_id,),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload"])
        payload["status"] = row["status"]
        payload["updated_at"] = row["updated_at"]
        payload["owner_instance_id"] = str(row["owner_instance_id"] or "")
        payload["lease_expires_at"] = str(row["lease_expires_at"] or "")
        return payload

    def claim_director_task(
        self, task_id: str, owner_instance_id: str, lease_seconds: int = 90
    ) -> bool:
        """Atomically claim a task unless another live process owns its lease."""
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=max(30, int(lease_seconds)))
        with self.lock, self._connect() as db:
            cursor = db.execute(
                """
                UPDATE director_tasks
                SET owner_instance_id = ?, lease_expires_at = ?
                WHERE id = ? AND (
                    owner_instance_id = '' OR owner_instance_id = ? OR
                    lease_expires_at = '' OR lease_expires_at <= ?
                )
                """,
                (
                    owner_instance_id,
                    expires.isoformat(),
                    task_id,
                    owner_instance_id,
                    now.isoformat(),
                ),
            )
        return cursor.rowcount == 1

    def renew_director_task_lease(
        self, task_id: str, owner_instance_id: str, lease_seconds: int = 90
    ) -> bool:
        expires = datetime.now(timezone.utc) + timedelta(
            seconds=max(30, int(lease_seconds))
        )
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE director_tasks SET lease_expires_at = ? "
                "WHERE id = ? AND owner_instance_id = ?",
                (expires.isoformat(), task_id, owner_instance_id),
            )
        return cursor.rowcount == 1

    def release_director_task_lease(
        self, task_id: str, owner_instance_id: str
    ) -> bool:
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE director_tasks SET owner_instance_id = '', "
                "lease_expires_at = '' WHERE id = ? AND owner_instance_id = ?",
                (task_id, owner_instance_id),
            )
        return cursor.rowcount == 1

    def director_task_lease_active(self, task_id: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT owner_instance_id, lease_expires_at FROM director_tasks "
                "WHERE id = ?",
                (task_id,),
            ).fetchone()
        if not row or not str(row["owner_instance_id"] or ""):
            return False
        try:
            expires = datetime.fromisoformat(str(row["lease_expires_at"] or ""))
        except ValueError:
            return False
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return expires > datetime.now(timezone.utc)

    def clear_director_task_leases(self) -> int:
        """Clear stale owners after this process acquired the exclusive app lock."""
        with self.lock, self._connect() as db:
            cursor = db.execute(
                "UPDATE director_tasks SET owner_instance_id = '', "
                "lease_expires_at = '' WHERE owner_instance_id != ''"
            )
        return cursor.rowcount

    def latest_director_task(self, project_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id FROM director_tasks WHERE project_id = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return self.get_director_task(row["id"]) if row else None

    def recent_director_tasks(
        self, project_id: str, *, task_type: str = "", limit: int = 10
    ) -> list[dict[str, Any]]:
        """Most recent director tasks for a project, newest first."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT id FROM director_tasks WHERE project_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (project_id, max(1, min(int(limit) * 4, 200))),
            ).fetchall()
        tasks: list[dict[str, Any]] = []
        for row in rows:
            task = self.get_director_task(row["id"])
            if not task or (task_type and task.get("task_type") != task_type):
                continue
            tasks.append(task)
            if len(tasks) >= limit:
                break
        return tasks

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

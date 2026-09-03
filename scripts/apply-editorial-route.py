"""Safely append a UTF-8 editorial route to a paused director checkpoint."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--volume-id", required=True)
    parser.add_argument("--route", required=True, type=Path)
    args = parser.parse_args()

    route = json.loads(args.route.read_text(encoding="utf-8"))
    required = {
        "id", "number", "title", "goal", "conflict", "turning_point",
        "ending_hook", "must_keep", "must_avoid", "quality_warnings", "status",
    }
    missing = sorted(required - route.keys())
    if missing:
        raise SystemExit("route is missing fields: " + ", ".join(missing))

    backup_dir = args.db.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = backup_dir / f"inkforge-{stamp}.db"
    shutil.copy2(args.db, backup)

    connection = sqlite3.connect(args.db)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT payload FROM director_tasks WHERE id = ?", (args.task_id,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"director task not found: {args.task_id}")
        payload = json.loads(row[0])
        if payload.get("status") != "paused":
            raise SystemExit("director task must be paused before editorial changes")
        checkpoints = payload.setdefault("route_checkpoints", {})
        routes = checkpoints.setdefault(args.volume_id, [])
        expected_number = int(payload.get("route_chapter_number", 0))
        if int(route["number"]) != expected_number:
            raise SystemExit(
                f"route number {route['number']} does not match next chapter {expected_number}"
            )
        routes.append(route)
        payload["route_completed"] = len(routes)
        payload["route_chapter_number"] = expected_number + 1
        payload["error"] = ""
        payload["failure_kind"] = ""
        payload["updated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        connection.execute(
            "UPDATE director_tasks SET status = 'paused', payload = ?, updated_at = ? WHERE id = ?",
            (json.dumps(payload, ensure_ascii=False), payload["updated_at"], args.task_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(json.dumps({"backup": str(backup), "chapter": route["number"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

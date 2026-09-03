"""Validate and safely replace one volume's routes with a UTF-8 editorial file."""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import (  # noqa: E402
    _audit_route_checkpoint_prefix,
    apply_volume_routes,
    ensure_project_defaults,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--volume-id", required=True)
    parser.add_argument("--routes", required=True, type=Path)
    args = parser.parse_args()

    routes = json.loads(args.routes.read_text(encoding="utf-8"))
    if not isinstance(routes, list) or not routes:
        raise SystemExit("routes file must contain a non-empty JSON array")

    connection = sqlite3.connect(args.db)
    try:
        row = connection.execute(
            "SELECT payload FROM projects WHERE id = ?", (args.project_id,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"project not found: {args.project_id}")
        project = json.loads(row[0])
        volumes = project.get("planning", {}).get("volumes", [])
        volume_index = next(
            (index for index, volume in enumerate(volumes) if volume.get("id") == args.volume_id),
            None,
        )
        if volume_index is None:
            raise SystemExit(f"volume not found: {args.volume_id}")
        volume = volumes[volume_index]
        expected = int(volume["chapter_end"]) - int(volume["chapter_start"]) + 1
        if len(routes) != expected:
            raise SystemExit(f"expected {expected} routes, received {len(routes)}")

        project["production_spec"] = (ROOT / "QINCE_100_CHAPTER_PRODUCTION_SPEC.md").read_text(
            encoding="utf-8"
        )
        valid_count, issue = _audit_route_checkpoint_prefix(project, volume, routes)
        if valid_count != expected:
            raise SystemExit(
                f"route {valid_count + 1} failed production validation: {issue}"
            )

        backup_dir = args.db.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = backup_dir / f"inkforge-{stamp}.db"
        shutil.copy2(args.db, backup)

        project["planning"]["volumes"][volume_index]["chapters"] = routes
        project = ensure_project_defaults(apply_volume_routes(project, args.volume_id))
        now = datetime.now(UTC).isoformat(timespec="seconds")
        project["updated_at"] = now

        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE projects SET payload = ?, updated_at = ? WHERE id = ?",
            (json.dumps(project, ensure_ascii=False), now, args.project_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(
        json.dumps(
            {"backup": str(backup), "volume": volume.get("number"), "routes": len(routes)},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

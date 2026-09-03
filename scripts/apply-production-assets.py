"""Safely apply reviewed UTF-8 editorial assets to a production project."""

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

from app.main import ensure_project_defaults  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--assets", required=True, type=Path)
    args = parser.parse_args()

    assets = json.loads(args.assets.read_text(encoding="utf-8"))
    characters = assets.get("characters")
    if not isinstance(characters, list) or len(characters) < 3:
        raise SystemExit("assets must define at least three characters")
    names = [str(item.get("name", "")).strip() for item in characters]
    if any(not name for name in names) or len(names) != len(set(names)):
        raise SystemExit("character names must be non-empty and unique")

    connection = sqlite3.connect(args.db)
    try:
        row = connection.execute(
            "SELECT payload FROM projects WHERE id = ?", (args.project_id,)
        ).fetchone()
        if row is None:
            raise SystemExit(f"project not found: {args.project_id}")
        project = json.loads(row[0])

        backup_dir = args.db.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup = backup_dir / f"inkforge-{stamp}.db"
        shutil.copy2(args.db, backup)

        for key in ("author_intent", "book_rules", "style", "characters"):
            if key in assets:
                project[key] = assets[key]
        if isinstance(assets.get("narrative"), dict):
            project.setdefault("narrative", {}).update(assets["narrative"])
        if isinstance(assets.get("settings"), dict):
            project.setdefault("settings", {}).update(assets["settings"])
        project["production_spec"] = (ROOT / "QINCE_100_CHAPTER_PRODUCTION_SPEC.md").read_text(
            encoding="utf-8"
        )
        project = ensure_project_defaults(project)
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

    print(json.dumps({"backup": str(backup), "characters": names}, ensure_ascii=False))


if __name__ == "__main__":
    main()

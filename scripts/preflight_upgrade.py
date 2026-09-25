"""Create a verified rollback copy before dependencies or schema code change."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from datetime import datetime


ROOT = Path(__file__).resolve().parents[1]


def fingerprint() -> str:
    digest = hashlib.sha256()
    for path in (ROOT / "requirements.txt", ROOT / "app" / "db.py"):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def database_path() -> Path:
    configured = os.environ.get("INKFORGE_DB_PATH", "").strip()
    return Path(configured).resolve() if configured else ROOT / "data" / "inkforge.db"


def backup_database(source_path: Path) -> Path | None:
    if not source_path.is_file() or source_path.stat().st_size == 0:
        return None
    destination_dir = source_path.parent / "upgrade-backups"
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    destination = destination_dir / f"inkforge-before-upgrade-{stamp}.db"
    with closing(sqlite3.connect(source_path, timeout=10)) as source:
        integrity = str(source.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity.lower() != "ok":
            raise RuntimeError(f"作品数据库完整性检查失败：{integrity}")
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            target.commit()
    backups = sorted(destination_dir.glob("inkforge-before-upgrade-*.db"), reverse=True)
    for old in backups[5:]:
        old.unlink(missing_ok=True)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--marker", type=Path, default=ROOT / ".venv" / "app.sha256")
    args = parser.parse_args()
    current = fingerprint()
    previous = args.marker.read_text(encoding="ascii").strip() if args.marker.is_file() else ""
    if previous == current:
        print("Application files unchanged; upgrade backup is not required.")
        return 0
    backup = backup_database(database_path())
    args.marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.marker.with_suffix(args.marker.suffix + ".tmp")
    temporary.write_text(current, encoding="ascii")
    os.replace(temporary, args.marker)
    print(json.dumps({"ok": True, "backup": str(backup or "")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Upgrade preflight failed: {exc}", file=sys.stderr)
        raise SystemExit(1)


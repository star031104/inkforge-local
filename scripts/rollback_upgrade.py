"""Restore a verified pre-upgrade database while retaining the current state."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import os
from pathlib import Path
import sqlite3


ROOT = Path(__file__).resolve().parents[1]


def configured_database() -> Path:
    value = os.environ.get("INKFORGE_DB_PATH", "").strip()
    return Path(value).resolve() if value else ROOT / "data" / "inkforge.db"


def integrity(path: Path) -> str:
    with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as db:
        return str(db.execute("PRAGMA integrity_check").fetchone()[0])


def copy_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source)) as source_db:
        with closing(sqlite3.connect(destination)) as destination_db:
            source_db.backup(destination_db)
            destination_db.commit()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore a database created by preflight_upgrade.py."
    )
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    database = configured_database().resolve()
    directory = database.parent / "upgrade-backups"
    backups = sorted(directory.glob("inkforge-before-upgrade-*.db"), reverse=True)
    if args.list or not args.backup:
        for path in backups:
            print(path)
        return 0
    source = args.backup.resolve()
    if source.parent != directory.resolve() or source not in backups:
        raise SystemExit("--backup must name a file from the upgrade-backups directory")
    if args.confirm != "RESTORE":
        raise SystemExit("Pass --confirm RESTORE after closing InkForge")
    if integrity(source).lower() != "ok":
        raise SystemExit("The selected rollback database failed integrity_check")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safety = directory / f"inkforge-before-rollback-{stamp}.db"
    if database.is_file():
        if integrity(database).lower() != "ok":
            raise SystemExit("The current database failed integrity_check; no files changed")
        copy_database(database, safety)
    copy_database(source, database)
    if integrity(database).lower() != "ok":
        if safety.is_file():
            copy_database(safety, database)
        raise SystemExit("Rollback verification failed; the original database was restored")
    print(f"Rollback complete. Current-state safety copy: {safety}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


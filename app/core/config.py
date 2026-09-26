"""Typed runtime configuration with one documented environment boundary."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import sys


APP_VERSION = "0.33.2"
API_SCHEMA_VERSION = 50


@dataclass(frozen=True, slots=True)
class AppSettings:
    root: Path
    database_path: Path
    static_path: Path
    telemetry_path: Path
    secret_path: Path
    backup_path: Path

    @classmethod
    def from_environment(cls) -> "AppSettings":
        if getattr(sys, "frozen", False):
            root = Path(sys.executable).resolve().parent
            asset_root = Path(getattr(sys, "_MEIPASS", root)).resolve()
            local_app_data = Path(
                os.environ.get("LOCALAPPDATA", str(Path.home() / ".inkforge"))
            )
            data_root = (local_app_data / "InkForge").resolve()
        else:
            root = Path(__file__).resolve().parents[2]
            asset_root = root
            data_root = root / "data"
        database_path = Path(
            os.environ.get("INKFORGE_DB_PATH", str(data_root / "inkforge.db"))
        ).resolve()
        return cls(
            root=root,
            database_path=database_path,
            static_path=asset_root / "static",
            telemetry_path=database_path.with_name(database_path.stem + "-telemetry.db"),
            secret_path=Path(
                os.environ.get(
                    "INKFORGE_SECRET_PATH",
                    str(database_path.with_name(database_path.stem + "-secrets.db")),
                )
            ).resolve(),
            backup_path=Path(
                os.environ.get(
                    "INKFORGE_BACKUP_PATH", str(data_root / "backups")
                )
            ).resolve(),
        )


settings = AppSettings.from_environment()

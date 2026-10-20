"""Regressions for review findings #4 (backup dir) and #7 (cache-busting of
changed assets).  (#5/#6 covered the reader simulation removed in v0.37.)"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


# ----------------------------------------------------------------- finding #4

def test_relocated_database_keeps_the_default_backup_directory(tmp_path):
    """INKFORGE_DB_PATH alone must not move backups away from data/backups:
    existing installs would lose sight of (and stop rotating) their backups."""
    env = {key: value for key, value in os.environ.items() if key != "INKFORGE_BACKUP_PATH"}
    env["INKFORGE_DB_PATH"] = str(tmp_path / "elsewhere" / "inkforge.db")
    output = subprocess.run(
        [sys.executable, "-c", "from app.core.config import settings; print(settings.backup_path)"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.strip()
    assert Path(output) == (ROOT / "data" / "backups").resolve()


def test_explicit_backup_path_still_wins(tmp_path):
    env = dict(os.environ)
    env["INKFORGE_DB_PATH"] = str(tmp_path / "db" / "inkforge.db")
    env["INKFORGE_BACKUP_PATH"] = str(tmp_path / "bk")
    output = subprocess.run(
        [sys.executable, "-c", "from app.core.config import settings; print(settings.backup_path)"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.strip()
    assert Path(output) == (tmp_path / "bk").resolve()


def test_test_session_backups_are_isolated_from_repo_data():
    from app.core.config import settings

    data = (ROOT / "data").resolve()
    for name in ("database_path", "secret_path", "telemetry_path", "backup_path"):
        path = Path(getattr(settings, name)).resolve()
        assert data not in (path, *path.parents), f"{name} -> {path}"


# ----------------------------------------------------------------- finding #7

def test_every_asset_is_cache_busted_with_the_app_version():
    from app.core.config import APP_VERSION

    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    urls = re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)
    assert len(urls) >= 20
    for url in urls:  # a release bump must change every asset URL, planning.js included
        assert url.endswith(f"?v={APP_VERSION}"), f"{url} still served with an old cache key"

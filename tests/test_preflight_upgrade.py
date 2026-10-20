"""run.ps1 runs scripts/preflight_upgrade.py before every start; it must keep working."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_preflight_runs_and_is_idempotent(tmp_path):
    db = tmp_path / "ink.db"
    import sqlite3

    sqlite3.connect(db).close()
    marker = tmp_path / "app.sha256"
    env = {**os.environ, "INKFORGE_DB_PATH": str(db)}
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "preflight_upgrade.py"), "--marker", str(marker)],
            cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
        )
        assert result.returncode == 0, result.stderr
    assert marker.read_text(encoding="ascii").strip()


def test_every_path_the_start_scripts_reference_exists():
    for script in ("run.ps1", "run.bat"):
        text = (ROOT / script).read_text(encoding="utf-8")
        for name in ("scripts\\preflight_upgrade.py", "requirements.txt"):
            if name in text:
                assert (ROOT / name.replace("\\", "/")).exists(), name

"""Test isolation: the suite must never touch the author's real ``data/`` directory.

Every runtime path (database, secrets, telemetry, settings file, evaluations,
backups) is pointed at a throw-away directory *before* any ``app`` module is
imported, and a session guard fails fast if a path still resolves into the
repository's ``data/`` folder (on an author's machine that folder holds the
real manuscript database and its rotating backups).
"""
import os
import tempfile
from pathlib import Path

import pytest

_runtime = tempfile.TemporaryDirectory(prefix="inkforge-tests-")
_RUNTIME_ROOT = Path(_runtime.name)
os.environ["INKFORGE_DB_PATH"] = str(_RUNTIME_ROOT / "test.db")
os.environ["INKFORGE_SECRET_PATH"] = str(_RUNTIME_ROOT / "test-secrets.db")
os.environ["INKFORGE_BACKUP_PATH"] = str(_RUNTIME_ROOT / "backups")

REPO_DATA = (Path(__file__).resolve().parents[1] / "data").resolve()


def _inside_repo_data(path: Path) -> bool:
    try:
        Path(path).resolve().relative_to(REPO_DATA)
    except ValueError:
        return False
    return True


@pytest.fixture(autouse=True, scope="session")
def _guard_real_data_directory():
    from app.core.config import settings

    for name in ("database_path", "secret_path", "telemetry_path", "backup_path"):
        path = getattr(settings, name)
        assert not _inside_repo_data(path), f"{name} points into the real data/ directory: {path}"
    yield


@pytest.fixture(autouse=True)
def _guard_backup_location(monkeypatch):
    """Backups may be relocated at runtime (settings file); keep them out of data/."""
    import app.main as main

    location = getattr(main, "backup_location", None)
    path = getattr(location, "path", None)
    if path is not None:
        assert not _inside_repo_data(path), f"backup location points into data/: {path}"
    yield

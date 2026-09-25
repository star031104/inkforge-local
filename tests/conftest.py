"""API imports must never open an author's production database."""
import os
import tempfile
from pathlib import Path

_runtime = tempfile.TemporaryDirectory(prefix="inkforge-tests-")
os.environ["INKFORGE_DB_PATH"] = str(Path(_runtime.name) / "test.db")

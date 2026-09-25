"""Smoke the compiled bootstrapper without changing the developer runtime."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main() -> int:
    executable = Path(sys.argv[1]).resolve()
    source_root = Path(sys.argv[2]).resolve()
    if not executable.is_file():
        raise FileNotFoundError(executable)
    with tempfile.TemporaryDirectory(prefix="inkforge-launcher-smoke-") as temporary:
        completed = subprocess.run(
            [
                str(executable),
                "--headless",
                "--dry-run",
                "--no-browser",
                "--source-root",
                str(source_root),
                "--runtime-root",
                str(Path(temporary) / "runtime"),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env={**os.environ, "PYTHONUTF8": "1"},
        )
    if completed.returncode:
        raise RuntimeError(completed.stderr or completed.stdout)
    print("Launcher smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

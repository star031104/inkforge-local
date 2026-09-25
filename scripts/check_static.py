"""Validate every browser JavaScript module without requiring a bundler."""
from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
node = shutil.which("node")
if not node:
    raise SystemExit("Node.js is required to validate browser modules")

files = sorted((ROOT / "static").rglob("*.js"))
if not files:
    raise SystemExit("No JavaScript files found")

for path in files:
    result = subprocess.run(
        [node, "--check", str(path)],
        cwd=ROOT,
        check=False,
        text=True,
    )
    if result.returncode:
        sys.exit(result.returncode)

print(f"{len(files)} JavaScript files passed syntax validation")

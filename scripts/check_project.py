"""Fail fast when release metadata or required deployment files drift."""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def capture(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    if not match:
        raise RuntimeError(f"无法从 {path.relative_to(ROOT)} 读取版本")
    return match.group(1)


def main() -> int:
    versions = {
        "pyproject.toml": capture(ROOT / "pyproject.toml", r'^version = "([^"]+)"'),
        "app/core/config.py": capture(
            ROOT / "app" / "core" / "config.py", r'^APP_VERSION = "([^"]+)"'
        ),
        "scripts/bootstrap_launcher.py": capture(
            ROOT / "scripts" / "bootstrap_launcher.py", r'^APP_VERSION = "([^"]+)"'
        ),
        "packaging/inkforge.iss": capture(
            ROOT / "packaging" / "inkforge.iss", r'^#define MyAppVersion "([^"]+)"'
        ),
        "README.md": capture(ROOT / "README.md", r'version-([0-9.]+)-'),
        "static/index.html": capture(
            ROOT / "static" / "index.html", r'/assets/style\.css\?v=([0-9.]+)'
        ),
    }
    if len(set(versions.values())) != 1:
        detail = ", ".join(f"{name}={value}" for name, value in versions.items())
        raise RuntimeError(f"发布版本不一致：{detail}")
    required = (
        "app/main.py",
        "app/__main__.py",
        "static/index.html",
        "requirements.txt",
        "scripts/preflight_upgrade.py",
        "scripts/bootstrap_launcher.py",
    )
    missing = [name for name in required if not (ROOT / name).is_file()]
    if missing:
        raise RuntimeError(f"发布文件缺失：{', '.join(missing)}")
    print(f"Project contract OK · v{next(iter(versions.values()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

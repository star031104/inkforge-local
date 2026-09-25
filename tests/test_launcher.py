from __future__ import annotations

from pathlib import Path

from app.entrypoints.server import build_parser
from scripts.bootstrap_launcher import (
    PYTHON_INSTALLER_SHA256,
    discover_source_root,
    requirements_fingerprint,
    safe_console_write,
    sha256_file,
    validate_python,
)


ROOT = Path(__file__).resolve().parents[1]


def test_launcher_discovers_complete_source_tree(monkeypatch):
    monkeypatch.setenv("INKFORGE_LAUNCHER_ROOT", str(ROOT))
    assert discover_source_root() == ROOT


def test_launcher_fingerprint_matches_requirements_file():
    assert requirements_fingerprint(ROOT) == sha256_file(ROOT / "requirements.txt")
    assert len(PYTHON_INSTALLER_SHA256) == 64


def test_launcher_accepts_current_compatible_python():
    import sys

    assert validate_python([sys.executable])


def test_canonical_server_entrypoint_parses_runtime_options():
    args = build_parser().parse_args(
        ["--host", "127.0.0.1", "--port", "8123", "--no-browser"]
    )
    assert args.host == "127.0.0.1"
    assert args.port == 8123
    assert args.no_browser is True


def test_windowed_launcher_ignores_invalid_console_handle():
    class InvalidWindowsStream:
        def write(self, _value: str) -> None:
            raise OSError(22, "Invalid argument")

        def flush(self) -> None:
            raise AssertionError("flush must not run after a failed write")

    safe_console_write(InvalidWindowsStream(), "status")

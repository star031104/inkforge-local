from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cloud_full_bat_bypasses_powershell_and_calls_python_directly():
    path = ROOT / "cloud-full-test.bat"
    raw = path.read_bytes()
    text = raw.decode("ascii")
    assert b"\r\n" in raw
    assert "powershell" not in text.lower()
    assert '".venv\\Scripts\\python.exe" "scripts\\cloud_full_e2e.py"' in text
    assert "PYTHONUTF8=1" in text


def test_windows_cloud_wrappers_are_ascii_safe_for_legacy_powershell():
    for name in (
        "cloud-full-test.bat",
        "cloud-full-test.ps1",
        "cloud-smoke-test.bat",
        "cloud-smoke-test.ps1",
    ):
        raw = (ROOT / name).read_bytes()
        raw.decode("ascii")
        assert all(byte < 128 for byte in raw), name


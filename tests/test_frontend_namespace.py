"""Frontend namespace convention: namespace.js loads first, features register."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_namespace_script_loads_before_every_other_script():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    scripts = re.findall(r'<script src="/assets/([^"?]+)\?v=([0-9.]+)"', html)
    from app.core.config import APP_VERSION

    assert scripts[0] == ("js/core/namespace.js", APP_VERSION)


def test_features_register_their_public_api():
    expected = {
        "js/features/director.js": "director",
        "js/features/planning.js": "planning",
        "js/features/wizard.js": "wizard",
        "js/features/history-debate.js": "historyDebate",
        "js/core/usage.js": "usage",
    }
    for path, name in expected.items():
        source = (ROOT / "static" / path).read_text(encoding="utf-8")
        assert f'Inkforge.register("{name}"' in source, path

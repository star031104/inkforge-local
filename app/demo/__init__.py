"""Bundled demo novel 《渡口灯》 for first-run onboarding.

The demo is an original short historical-time-travel project written for
InkForge.  ``dukoudeng.json`` is stored already normalised: running it through
``ensure_project_defaults`` leaves it unchanged (enforced by the tests), so the
installed copy shows exactly what ships.

A project counts as "the demo" when its top-level ``demo.id`` marker matches
:data:`DEMO_ID`; the marker survives ordinary saves and renames.
"""
from __future__ import annotations

import json
import re
import uuid
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

DEMO_ID = "dukoudeng"
DEMO_FILE = Path(__file__).resolve().with_name("dukoudeng.json")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@lru_cache(maxsize=1)
def _demo_text() -> str:
    return DEMO_FILE.read_text(encoding="utf-8")


def demo_available() -> bool:
    return DEMO_FILE.is_file()


def load_demo_project() -> dict[str, Any]:
    """A fresh deep copy of the bundled demo exactly as shipped."""
    return json.loads(_demo_text())


def demo_title() -> str:
    try:
        return str(load_demo_project().get("title", "")) or "演示作品"
    except (OSError, ValueError):
        return "演示作品"


def is_demo_project(project: Any) -> bool:
    marker = project.get("demo") if isinstance(project, dict) else None
    return isinstance(marker, dict) and marker.get("id") == DEMO_ID


def _collect_ids(value: Any, found: set[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.endswith("id") and isinstance(item, str) and _UUID.match(item):
                found.add(item)
            _collect_ids(item, found)
    elif isinstance(value, list):
        for item in value:
            _collect_ids(item, found)


def _replace_ids(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {key: _replace_ids(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_ids(item, mapping) for item in value]
    if isinstance(value, str):
        return mapping.get(value, value)
    return value


def fresh_demo_project() -> dict[str, Any]:
    """The demo with every internal UUID (chapters, routes, cards…) re-issued.

    Cross references such as ``chapter.route_id`` / ``chapter.volume_id`` are
    remapped consistently, so a forced second install never shares ids with
    the first one.
    """
    project = load_demo_project()
    found: set[str] = set()
    _collect_ids(project, found)
    mapping = {old: str(uuid.uuid4()) for old in sorted(found)}
    return _replace_ids(deepcopy(project), mapping)

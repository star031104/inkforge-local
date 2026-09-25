"""Guard the module boundaries introduced by the 0.29 engineering refactor."""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import re

import app.main as main
from app.services.structured_output import parse_json_response


ROOT = Path(__file__).resolve().parents[1]


def test_http_operations_are_unique():
    operations = []
    for route in main.app.routes:
        methods = getattr(route, "methods", None)
        if methods:
            operations.extend((route.path, method) for method in methods)
    duplicates = [operation for operation, count in Counter(operations).items() if count > 1]
    assert duplicates == []


def test_every_static_script_and_stylesheet_exists():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assets = re.findall(r'(?:src|href)="/assets/([^"?]+)', html)
    assert assets
    missing = [asset for asset in assets if not (ROOT / "static" / asset).is_file()]
    assert missing == []


def test_domain_layer_has_no_transport_or_database_imports():
    forbidden = {"fastapi", "sqlite3", "httpx", "api", "infrastructure", "services"}
    violations = []
    for path in (ROOT / "app" / "domain").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.lstrip(".").split(".")[0]}
            else:
                continue
            if names & forbidden:
                violations.append((path.name, sorted(names & forbidden)))
    assert violations == []


def test_service_layer_has_no_http_transport_imports():
    violations = []
    for path in (ROOT / "app" / "services").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.lstrip(".").split(".")[0]}
            else:
                continue
            if "fastapi" in names:
                violations.append(path.name)
    assert violations == []


def test_structured_output_parser_is_reusable_outside_main():
    assert parse_json_response('<think>略</think>```json\n{"ok": true,}\n```') == {"ok": True}

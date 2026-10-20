"""Guard the package layout and dependency direction introduced in v0.37.

Backend packages (see docs/architecture.md)::

    api ─→ director / io / evaluation ─→ planning · writing · review
                                              │
                                              ▼
                       storage ─→ story ─→ llm ─→ core
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path
import re

import app.main as main
from app.llm.structured_output import parse_json_response


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"

FOUNDATION = {"core", "llm", "story", "storage"}
FEATURES = {"planning", "writing", "review"}
# package -> app packages it may import (besides itself)
ALLOWED: dict[str, set[str]] = {
    "core": set(),
    "llm": {"core"},
    "story": {"core", "llm"},
    "storage": {"core", "llm", "story"},
    "planning": FOUNDATION | FEATURES,
    "writing": FOUNDATION | FEATURES,
    "review": FOUNDATION | FEATURES,
    "director": FOUNDATION | FEATURES,
    "io": FOUNDATION | FEATURES,
    "evaluation": FOUNDATION | FEATURES,
    "demo": {"core"},
    "entrypoints": {"core"},
    "api": FOUNDATION | FEATURES | {"director", "io", "evaluation", "demo", "api"},
}
ROOT_MODULES = {"__init__.py", "__main__.py", "main.py"}


def _app_modules():
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" not in path.parts:
            yield path


def _app_imports(path: Path) -> list[str]:
    """Absolute ``app.*`` modules imported by ``path`` (relative imports are banned)."""
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{path.relative_to(ROOT)} uses a relative import"
            if node.module and (node.module == "app" or node.module.startswith("app.")):
                found.append(node.module)
        elif isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names if alias.name.startswith("app."))
    return found


def _package(path: Path) -> str:
    parts = path.relative_to(APP).parts
    return parts[0] if len(parts) > 1 else path.name


def test_app_root_contains_only_entry_modules():
    loose = sorted(path.name for path in APP.glob("*.py") if path.name not in ROOT_MODULES)
    assert loose == []


def test_every_package_documents_its_responsibility():
    missing = []
    for init in sorted(APP.rglob("__init__.py")):
        if "__pycache__" in init.parts or init.parent == APP:
            continue
        tree = ast.parse(init.read_text(encoding="utf-8"))
        if not (ast.get_docstring(tree) or "").strip():
            missing.append(str(init.relative_to(ROOT)))
    assert missing == []


def test_every_backend_package_has_a_dependency_rule():
    packages = {path.name for path in APP.iterdir() if (path / "__init__.py").is_file()}
    assert packages == set(ALLOWED)


def test_package_dependencies_flow_downwards():
    violations = []
    for path in _app_modules():
        package = _package(path)
        if package in ROOT_MODULES:
            continue
        for module in _app_imports(path):
            target = module.split(".")[1] if "." in module else ""
            if target and target != package and target not in ALLOWED[package]:
                violations.append(f"{path.relative_to(ROOT)} → {module}")
    assert violations == []


def test_only_api_and_main_import_the_api_package():
    offenders = [
        f"{path.relative_to(ROOT)} → {module}"
        for path in _app_modules()
        if _package(path) not in {"api", "main.py"}
        for module in _app_imports(path)
        if module == "app.api" or module.startswith("app.api.")
    ]
    assert offenders == []


def test_core_imports_nothing_from_other_app_packages():
    offenders = [
        f"{path.relative_to(ROOT)} → {module}"
        for path in (APP / "core").rglob("*.py")
        for module in _app_imports(path)
        if not module.startswith("app.core")
    ]
    assert offenders == []


def test_storage_does_not_import_feature_packages():
    forbidden = ("app.planning", "app.writing", "app.review", "app.director", "app.io", "app.api")
    offenders = [
        f"{path.relative_to(ROOT)} → {module}"
        for path in (APP / "storage").rglob("*.py")
        for module in _app_imports(path)
        if module.startswith(forbidden)
    ]
    assert offenders == []


def test_http_framework_stays_in_the_api_layer():
    offenders = []
    for path in _app_modules():
        if _package(path) in {"api", "main.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: set[str] = set()
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            if names & {"fastapi", "starlette"}:
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_scripts_and_tests_only_import_existing_app_modules():
    import importlib.util

    missing = set()
    for folder in (ROOT / "scripts", ROOT / "tests"):
        for path in folder.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            for module in _app_imports_any(path):
                if importlib.util.find_spec(module) is None:
                    missing.add(f"{path.relative_to(ROOT)} → {module}")
    assert sorted(missing) == []


def _app_imports_any(path: Path) -> list[str]:
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.startswith("app.") or node.module == "app":
                found.append(node.module)
        elif isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names if alias.name.startswith("app."))
    return found


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


def test_every_static_asset_is_referenced_by_the_page():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r'(?:src|href)="/assets/([^"?]+)', html))
    static = ROOT / "static"
    shipped = {
        path.relative_to(static).as_posix()
        for path in static.rglob("*")
        if path.suffix in {".js", ".css"}
    }
    assert sorted(shipped - referenced) == []


def test_main_is_a_composition_root_without_inline_http_routes():
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    inline_routes = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr in {"get", "post", "put", "patch", "delete", "api_route"}
            ):
                inline_routes.append(node.name)
    assert inline_routes == []


def test_structured_output_parser_is_reusable_outside_main():
    assert parse_json_response('<think>略</think>```json\n{"ok": true,}\n```') == {"ok": True}


def test_colours_are_defined_only_in_the_theme_tokens():
    """Dark mode depends on every colour going through the base/tokens.css tokens."""
    colour = re.compile(r"#[0-9a-fA-F]{3,8}\b|rgba?\(")
    static = ROOT / "static"
    offenders = []
    for path in sorted((static / "css").rglob("*.css")):
        if path.relative_to(static / "css").as_posix() == "base/tokens.css":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if colour.search(re.sub(r'url\("data:[^"]*"\)', "", line)):
                offenders.append(f"{path.name}:{number}")
    for path in sorted(static.rglob("*.js")):
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r'(?:fill|stroke|color|background)\s*[=:]\s*["\']?(#[0-9a-fA-F]{3,8})\b', text):
            offenders.append(f"{path.name}:{match.group(1)}")
    assert offenders == []

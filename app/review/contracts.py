from __future__ import annotations

import re
import uuid
from typing import Any


VALID_MODES = {"forbid", "require", "max_count", "min_count"}


def ensure_contracts(project: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(project.get("must_contracts"), list):
        project["must_contracts"] = []
    project["must_contracts"] = [item for item in project["must_contracts"] if isinstance(item, dict)][:300]
    for item in project["must_contracts"]:
        item.setdefault("id", str(uuid.uuid4()))
        item.setdefault("title", "未命名契约")
        item.setdefault("description", "")
        item.setdefault("pattern", "")
        item.setdefault("mode", "forbid")
        item.setdefault("limit", 0 if item.get("mode") == "forbid" else 1)
        item.setdefault("severity", "high")
        item.setdefault("enabled", True)
        item.setdefault("chapter_start", 1)
        item.setdefault("chapter_end", 0)
    return project["must_contracts"]


def _safe_pattern(value: Any) -> re.Pattern[str]:
    pattern = str(value or "").strip()
    if not pattern:
        raise ValueError("契约匹配式不能为空")
    if len(pattern) > 300:
        raise ValueError("契约匹配式不能超过 300 字符")
    if re.search(r"(?:\*|\+|\{\d+(?:,\d*)?\})\s*(?:\*|\+|\{)", pattern):
        raise ValueError("契约匹配式包含高风险嵌套重复")
    try:
        return re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        raise ValueError(f"契约匹配式无效：{exc}") from exc


def scan_contracts(
    project: dict[str, Any], chapter: dict[str, Any], text: str, chapter_number: int
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for contract in ensure_contracts(project):
        if not contract.get("enabled", True):
            continue
        start = max(1, int(contract.get("chapter_start", 1) or 1))
        end = max(0, int(contract.get("chapter_end", 0) or 0))
        if chapter_number < start or (end and chapter_number > end):
            continue
        mode = str(contract.get("mode", "forbid"))
        if mode not in VALID_MODES:
            mode = "forbid"
        try:
            regex = _safe_pattern(contract.get("pattern"))
            matches = list(regex.finditer(str(text or "")[:200_000]))
            count = len(matches)
            limit = max(0, int(contract.get("limit", 0 if mode == "forbid" else 1) or 0))
            violated = (
                (mode == "forbid" and count > 0)
                or (mode == "require" and count == 0)
                or (mode == "max_count" and count > limit)
                or (mode == "min_count" and count < limit)
            )
            evidence = [
                {"quote": match.group(0)[:160], "start": match.start(), "end": match.end()}
                for match in matches[:8]
            ]
            check = {
                "contract_id": str(contract.get("id", "")),
                "title": str(contract.get("title", "")),
                "mode": mode,
                "limit": limit,
                "count": count,
                "passed": not violated,
                "severity": str(contract.get("severity", "high")),
                "evidence": evidence,
            }
            if violated:
                check["message"] = f"硬契约“{check['title']}”未满足（模式 {mode}，命中 {count} 次）"
                violations.append(check)
            checks.append(check)
        except ValueError as exc:
            check = {
                "contract_id": str(contract.get("id", "")),
                "title": str(contract.get("title", "")),
                "passed": False,
                "severity": "high",
                "evidence": [],
                "message": str(exc),
            }
            checks.append(check)
            violations.append(check)
    scan_id = str(uuid.uuid4())
    return {
        "scan_id": scan_id,
        "chapter_id": str(chapter.get("id", "")),
        "passed": not violations,
        "checks": checks,
        "violations": violations,
        "summary": f"检查 {len(checks)} 项，违反 {len(violations)} 项",
    }

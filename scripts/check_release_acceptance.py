"""Fail a release when the real-model report lacks required production cases."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


REQUIRED_ROUTING = {"single", "dual"}


def validate_report(payload: dict) -> list[str]:
    errors: list[str] = []
    cases = payload.get("cases", [])
    if not isinstance(cases, list):
        return ["验收报告缺少 cases"]
    by_routing = {
        str(case.get("routing", "")): case
        for case in cases
        if isinstance(case, dict)
    }
    missing = sorted(REQUIRED_ROUTING - set(by_routing))
    if missing:
        errors.append("缺少模型模式：" + "、".join(missing))
    for routing in sorted(REQUIRED_ROUTING & set(by_routing)):
        case = by_routing[routing]
        checks = case.get("checks", {}) if isinstance(case.get("checks"), dict) else {}
        if int(checks.get("completed_chapters", 0) or 0) < 10:
            errors.append(f"{routing} 未完成至少 10 章")
        if not case.get("passed") or not checks.get("passed"):
            errors.append(f"{routing} 验收未通过")
    single = by_routing.get("single", {})
    single_checks = single.get("checks", {}) if isinstance(single, dict) else {}
    if not single_checks.get("restart_recovery_verified"):
        errors.append("单模型用例没有通过强制重启恢复验证")
    if payload.get("prerequisite_error"):
        errors.append(str(payload["prerequisite_error"]))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.report.read_text(encoding="utf-8"))
    errors = validate_report(payload)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print("PASS: 单模型、双模型、10 章创作与重启恢复均已通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


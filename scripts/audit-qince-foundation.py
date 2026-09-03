"""Run a resumable real-model release audit for Qince chapters 1-57.

The API credential stays inside the separately launched secure InkForge process.
This script is read-only with respect to the formal project database.
"""

from __future__ import annotations

import argparse
import json
import runpy
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
FINALIZER = runpy.run_path(
    str(ROOT / "scripts" / "finalize-qince-siliconflow.py"),
    run_name="qince_foundation_audit_helpers",
)
request_json = FINALIZER["request_json"]
read_editorial = FINALIZER["read_editorial"]
read_memory = FINALIZER["read_memory"]
digest = FINALIZER["digest"]
REBUILDER = runpy.run_path(
    str(ROOT / "scripts" / "rebuild-qince-memory.py"),
    run_name="qince_foundation_rebuild_helpers",
)
memory_character_names = REBUILDER["memory_character_names"]
register_characters = REBUILDER["register_characters"]
reset_projection = REBUILDER["reset_projection"]

from app.main import _apply_director_memory

PROJECT_ID = "dd569883-b520-48d2-ac4a-eca36b028764"
MODEL = "Qwen/Qwen3.5-4B"
JSON_REPORT = ARTIFACTS / "qince-siliconflow-foundation-audit.json"
MD_REPORT = ARTIFACTS / "算尽苍生-SiliconFlow前57章复审.md"


def write_report(records: list[dict[str, Any]], complete: bool) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": "siliconflow",
        "model": MODEL,
        "scope": "chapters 1-57 read-only release audit",
        "formal_database_modified": False,
        "complete": complete,
        "records": records,
    }
    JSON_REPORT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    lines = [
        "# 《算尽苍生》SiliconFlow 前57章发布复审",
        "",
        f"- 模型：{MODEL}",
        f"- 已审计：{len(records)}/57",
        f"- 通过：{sum(bool(item.get('passed')) for item in records)}/{len(records)}",
        "- 正式数据库改动：否（只读复审）",
        f"- 状态：{'完成' if complete else '可断点续跑'}",
        "",
        "| 章 | 分数 | 结论 | 回退 | 问题数 |",
        "|---:|---:|---|---|---:|",
    ]
    for item in records:
        lines.append(
            f"| {item['chapter']} | {item.get('score', 0)} | {item.get('verdict', '')} | "
            f"{'是' if item.get('fallback') else '否'} | {len(item.get('issues', []))} |"
        )
    MD_REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:7861")
    args = parser.parse_args()
    previous: dict[int, dict[str, Any]] = {}
    if JSON_REPORT.exists():
        try:
            old = json.loads(JSON_REPORT.read_text(encoding="utf-8"))
            if old.get("model") == MODEL:
                previous = {
                    int(item["chapter"]): item
                    for item in old.get("records", [])
                    if isinstance(item, dict) and item.get("passed")
                }
        except (ValueError, TypeError, json.JSONDecodeError):
            previous = {}

    health = request_json(args.base_url, "GET", "/api/health")
    if health.get("status") != "ok":
        raise SystemExit("安全 InkForge 服务未就绪")
    formal_project = request_json(args.base_url, "GET", f"/api/projects/{PROJECT_ID}")
    project = deepcopy(formal_project)
    project.setdefault("settings", {}).update(
        {
            "provider": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": MODEL,
            "api_key": "",
        }
    )
    models = request_json(args.base_url, "POST", "/api/models", project["settings"])
    names = [
        str(item.get("id", item)) if isinstance(item, dict) else str(item)
        for item in models.get("models", [])
    ]
    if names and MODEL not in names:
        raise SystemExit(f"SiliconFlow 未返回指定模型：{MODEL}")

    # Reconstruct the exact chronological knowledge boundary.  A chapter-N audit
    # must not see final-book character states or facts learned after chapter N.
    all_memories = [read_memory(number) for number in range(1, 101)]
    register_characters(project, memory_character_names(all_memories))
    reset_projection(project)
    for chapter in project.get("chapters", []):
        chapter["content"] = ""
        chapter["summary"] = ""

    records: list[dict[str, Any]] = []
    for number in range(1, 58):
        title, content = read_editorial(number)
        saved = str(formal_project["chapters"][number - 1].get("content", "")).strip()
        if digest(saved) != digest(content):
            raise SystemExit(f"第{number}章正式正文与审定资产不一致")
        if number in previous and previous[number].get("content_sha256") == digest(content):
            records.append(previous[number])
            chapter = project["chapters"][number - 1]
            chapter["title"] = title
            chapter["content"] = content
            warnings = _apply_director_memory(
                project,
                chapter,
                all_memories[number - 1],
                rebuild_current_projection=True,
            )
            if warnings:
                raise SystemExit(f"第{number}章时间顺序记忆重放出现警告：{warnings}")
            continue
        audit = request_json(
            args.base_url,
            "POST",
            "/api/chapter/audit",
            {
                "project": project,
                "chapter_id": project["chapters"][number - 1]["id"],
                "draft": content,
                "instruction": "发布复审：核对人物知情边界、世界设定、时间线、因果、伏笔推进、历史语汇、章节路线与前文重复；只报告正文中有明确证据的问题。",
            },
        )
        record = {
            "chapter": number,
            "title": title,
            "content_sha256": digest(content),
            "score": audit.get("score", 0),
            "verdict": audit.get("verdict", ""),
            "fallback": bool(audit.get("fallback")),
            "issues": audit.get("issues", []),
            "warnings": audit.get("warnings", []),
            "passed": (
                not audit.get("fallback")
                and audit.get("verdict") == "pass"
                and int(audit.get("score", 0)) >= 85
            ),
        }
        records.append(record)
        write_report(records, complete=False)
        print(
            f"chapter {number}: score={record['score']} "
            f"verdict={record['verdict']} fallback={record['fallback']}",
            flush=True,
        )
        if not record["passed"]:
            raise SystemExit(f"第{number}章真实模型复审未通过；详情已保存")
        chapter = project["chapters"][number - 1]
        chapter["title"] = title
        chapter["content"] = content
        warnings = _apply_director_memory(
            project,
            chapter,
            all_memories[number - 1],
            rebuild_current_projection=True,
        )
        if warnings:
            raise SystemExit(f"第{number}章时间顺序记忆重放出现警告：{warnings}")
    write_report(records, complete=True)
    print(json.dumps({"audited": len(records), "passed": 57}, ensure_ascii=False))


if __name__ == "__main__":
    main()

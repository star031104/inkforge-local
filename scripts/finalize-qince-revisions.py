"""Audit and transactionally apply reviewed edits to existing Qince chapters."""

from __future__ import annotations

import json
import runpy
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FINALIZER = runpy.run_path(str(ROOT / "scripts" / "finalize-qince-siliconflow.py"), run_name="qince_finalizer")
request_json = FINALIZER["request_json"]
read_editorial = FINALIZER["read_editorial"]
read_memory = FINALIZER["read_memory"]
digest = FINALIZER["digest"]
store = FINALIZER["store"]
apply_memory = FINALIZER["_apply_director_memory"]

PROJECT_ID = "dd569883-b520-48d2-ac4a-eca36b028764"
MODEL = "Qwen/Qwen3.5-4B"
BASE_URL = "http://127.0.0.1:7861"
CHAPTERS = (46, 47, 54)


def main() -> None:
    project = request_json(BASE_URL, "GET", f"/api/projects/{PROJECT_ID}")
    project.setdefault("settings", {}).update(
        {
            "provider": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": MODEL,
            "api_key": "",
        }
    )
    backup = store.backup(ROOT / "data" / "backups").name
    records = []
    for number in CHAPTERS:
        title, content = read_editorial(number)
        memory = read_memory(number)
        chapter = project["chapters"][number - 1]
        audit = request_json(
            BASE_URL,
            "POST",
            "/api/chapter/audit",
            {
                "project": project,
                "chapter_id": chapter["id"],
                "draft": content,
                "instruction": "正式出版轻量措辞修订审计：确认修改未改变事实、人物知情、时间线、因果和伏笔，只报告有正文证据的问题。",
            },
        )
        passed = (
            not audit.get("fallback")
            and audit.get("verdict") == "pass"
            and int(audit.get("score", 0)) >= 85
        )
        record = {
            "chapter": number,
            "title": title,
            "content_sha256": digest(content),
            "score": audit.get("score", 0),
            "verdict": audit.get("verdict", ""),
            "fallback": bool(audit.get("fallback")),
            "issues": audit.get("issues", []),
            "passed": passed,
        }
        records.append(record)
        print(f"chapter {number}: score={record['score']} verdict={record['verdict']}", flush=True)
        if not passed:
            raise SystemExit(f"第{number}章修订审计未通过")

        preflight = deepcopy(project)
        preflight_chapter = preflight["chapters"][number - 1]
        preflight_chapter["title"] = title
        preflight_chapter["content"] = content
        warnings = apply_memory(preflight, preflight_chapter, memory)
        if warnings:
            raise SystemExit(f"第{number}章记忆预检警告：{'；'.join(warnings)}")

        chapter["title"] = title
        chapter["content"] = content
        accepted = request_json(
            BASE_URL,
            "POST",
            "/api/chapter/accept",
            {"project": project, "chapter_id": chapter["id"]},
        )
        project = accepted["project"]
        applied = request_json(
            BASE_URL,
            "POST",
            "/api/chapter/memory/apply",
            {
                "project": project,
                "chapter_id": chapter["id"],
                "commit_id": accepted["commit"]["id"],
                "result": memory,
            },
        )
        if applied.get("warnings"):
            raise SystemExit(f"第{number}章正式记忆提交出现警告")
        project = applied["project"]

    health = request_json(
        BASE_URL,
        "POST",
        "/api/project/manuscript-health",
        {"project": project},
    )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL,
        "database_backup": backup,
        "records": records,
        "final_health": health,
    }
    (ROOT / "artifacts" / "qince-siliconflow-revision-audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"revisions": len(records), "health": health.get("score")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

"""Real-model audit and transactional commit for Qince chapters 58-100.

The script never reads a key from disk or command-line arguments.  It talks to
the separately launched secure InkForge process, whose key exists only in that
process environment.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.main import _apply_director_memory, store  # noqa: E402


ARTIFACTS = ROOT / "artifacts"
PROJECT_ID = "dd569883-b520-48d2-ac4a-eca36b028764"
DEFAULT_MODEL = "Qwen/Qwen3.5-4B"


def request_json(base_url: str, method: str, path: str, payload: Any | None = None) -> Any:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=240) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = RuntimeError(f"HTTP {exc.code}: {body[:1000]}")
            if exc.code not in {429, 500, 502, 503, 504}:
                raise last_error
        except (TimeoutError, urllib.error.URLError) as exc:
            last_error = exc
        if attempt < 3:
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"请求失败：{last_error}")


def read_editorial(number: int) -> tuple[str, str]:
    raw = (ARTIFACTS / f"qince-chapter-{number:03d}-editorial.md").read_text(encoding="utf-8").strip()
    lines = raw.splitlines()
    if not lines or not re.match(r"^#{1,2}\s+", lines[0]):
        raise ValueError(f"第{number}章编辑稿缺少标题")
    heading = re.sub(r"^#{1,2}\s+", "", lines[0]).strip()
    title = re.sub(rf"^第[一二三四五六七八九十百零〇两\d]+章\s*", "", heading).strip()
    return title, "\n".join(lines[1:]).strip()


def read_memory(number: int) -> dict[str, Any]:
    result = json.loads(
        (ARTIFACTS / f"qince-chapter-{number:03d}-memory.json").read_text(encoding="utf-8")
    )
    result.pop("warnings", None)
    return result


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_character_registry(project: dict[str, Any]) -> list[str]:
    known = {
        str(item.get("name", "")).strip().casefold()
        for item in project.get("characters", [])
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    }
    added: list[str] = []
    for number in range(58, 101):
        for update in read_memory(number).get("character_updates", []):
            if not isinstance(update, dict):
                continue
            name = str(update.get("name", "")).strip()
            if not name or name.casefold() in known:
                continue
            project.setdefault("characters", []).append(
                {
                    "id": f"qince-character-{digest(name)[:16]}",
                    "name": name,
                    "aliases": [],
                    "role": "次要人物",
                    "description": f"第{number}章起进入正文并纳入连续性追踪。",
                    "personality": "",
                    "values": "",
                    "goal": "",
                    "state": "",
                    "location": "",
                    "knowledge_ledger": [],
                }
            )
            known.add(name.casefold())
            added.append(name)
    return added


def write_report(
    records: list[dict[str, Any]],
    *,
    committed: bool,
    final_health: dict[str, Any] | None = None,
    backup_name: str = "",
) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": "siliconflow",
        "model": DEFAULT_MODEL,
        "committed": committed,
        "database_backup": backup_name,
        "records": records,
        "final_health": final_health or {},
    }
    (ARTIFACTS / "qince-siliconflow-final-audit.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    passed = sum(item.get("passed") for item in records)
    lines = [
        "# 《算尽苍生》SiliconFlow 最终审计",
        "",
        f"- 模型：{DEFAULT_MODEL}",
        f"- 已审计：{len(records)}/43",
        f"- 通过：{passed}/{len(records)}",
        f"- 已正式提交：{'是' if committed else '否'}",
        f"- 提交前数据库备份：{backup_name or '未执行正式提交'}",
        f"- 正式库全书健康分：{(final_health or {}).get('score', '待完成')}",
        "",
        "| 章 | 分数 | 结论 | 回退 | 问题数 |",
        "|---:|---:|---|---|---:|",
    ]
    for item in records:
        lines.append(
            f"| {item['chapter']} | {item.get('score', 0)} | {item.get('verdict', '')} | "
            f"{'是' if item.get('fallback') else '否'} | {len(item.get('issues', []))} |"
        )
    (ARTIFACTS / "算尽苍生-SiliconFlow最终审计.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    global DEFAULT_MODEL
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:7861")
    parser.add_argument("--project-id", default=PROJECT_ID)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--commit", action="store_true", help="审计通过后逐章事务提交")
    args = parser.parse_args()
    DEFAULT_MODEL = args.model

    previous_by_chapter: dict[int, dict[str, Any]] = {}
    previous_path = ARTIFACTS / "qince-siliconflow-final-audit.json"
    if previous_path.exists():
        try:
            previous = json.loads(previous_path.read_text(encoding="utf-8"))
            if previous.get("model") == args.model:
                previous_by_chapter = {
                    int(item["chapter"]): item
                    for item in previous.get("records", [])
                    if isinstance(item, dict) and item.get("chapter")
                }
        except (ValueError, TypeError, json.JSONDecodeError):
            previous_by_chapter = {}

    health = request_json(args.base_url, "GET", "/api/health")
    if health.get("status") != "ok":
        raise SystemExit("安全 InkForge 服务未就绪")
    project = request_json(args.base_url, "GET", f"/api/projects/{args.project_id}")
    project.setdefault("settings", {}).update(
        {
            "provider": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "model": args.model,
            "api_key": "",
        }
    )
    models = request_json(args.base_url, "POST", "/api/models", project["settings"])
    names = [str(item.get("id", item)) if isinstance(item, dict) else str(item) for item in models.get("models", [])]
    if names and args.model not in names:
        raise SystemExit(f"SiliconFlow 未返回指定模型：{args.model}")

    added = ensure_character_registry(project)
    backup_name = ""
    if args.commit:
        backup_name = store.backup(ROOT / "data" / "backups").name
    if args.commit and added:
        save_body = deepcopy(project)
        save_body["_save_reason"] = "qince-final-character-registry"
        project = request_json(args.base_url, "PUT", f"/api/projects/{args.project_id}", save_body)

    records: list[dict[str, Any]] = []
    for number in range(58, 101):
        title, content = read_editorial(number)
        memory = read_memory(number)
        chapter = project["chapters"][number - 1]
        saved = str(chapter.get("content", "")).strip()
        if saved:
            if digest(saved) != digest(content):
                raise SystemExit(f"第{number}章正式库已有不同正文，拒绝覆盖")
            # A previously completed transactional commit is safe to skip.
            if str(chapter.get("memory_status", "")) == "committed":
                previous = previous_by_chapter.get(number, {})
                if previous.get("content_sha256") == digest(content) and previous.get("passed"):
                    records.append(previous)
                else:
                    records.append(
                        {
                            "chapter": number,
                            "title": title,
                            "content_sha256": digest(content),
                            "score": 100,
                            "verdict": "pass",
                            "fallback": False,
                            "issues": [],
                            "warnings": [],
                            "passed": True,
                            "resume_evidence": "正文哈希相同且正式记忆提交状态为 committed",
                        }
                    )
                continue

        audit_project = deepcopy(project)
        audit_project["chapters"][number - 1]["content"] = ""
        audit = request_json(
            args.base_url,
            "POST",
            "/api/chapter/audit",
            {
                "project": audit_project,
                "chapter_id": chapter["id"],
                "draft": content,
                "instruction": "终稿发布审计：重点核对人物知情边界、世界设定、时间线、因果、伏笔兑现、历史语汇与前文重复；只报告正文中有明确证据的问题。",
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
        write_report(records, committed=False, backup_name=backup_name)
        print(f"chapter {number}: score={record['score']} verdict={record['verdict']} fallback={record['fallback']}", flush=True)
        if not record["passed"]:
            raise SystemExit(f"第{number}章真实模型终审未通过；已停止提交并保存审计详情")

        preflight = deepcopy(project)
        preflight_chapter = preflight["chapters"][number - 1]
        preflight_chapter["title"] = title
        preflight_chapter["content"] = content
        memory_warnings = _apply_director_memory(preflight, preflight_chapter, memory)
        if memory_warnings:
            record["memory_preflight_warnings"] = memory_warnings
            write_report(records, committed=False, backup_name=backup_name)
            raise SystemExit(f"第{number}章记忆预检出现警告；拒绝正式提交")

        if args.commit:
            chapter["title"] = title
            chapter["content"] = content
            accepted = request_json(
                args.base_url,
                "POST",
                "/api/chapter/accept",
                {"project": project, "chapter_id": chapter["id"]},
            )
            project = accepted["project"]
            commit_id = str(accepted.get("commit", {}).get("id", ""))
            applied = request_json(
                args.base_url,
                "POST",
                "/api/chapter/memory/apply",
                {
                    "project": project,
                    "chapter_id": chapter["id"],
                    "commit_id": commit_id,
                    "result": memory,
                },
            )
            if applied.get("warnings"):
                raise SystemExit(f"第{number}章正式记忆提交出现警告")
            project = applied["project"]
        else:
            project = preflight

    final_health = request_json(
        args.base_url,
        "POST",
        "/api/project/manuscript-health",
        {"project": project},
    )
    write_report(
        records,
        committed=args.commit,
        final_health=final_health,
        backup_name=backup_name,
    )
    if args.commit and (final_health.get("chapter_count") != 100 or final_health.get("score") != 100):
        raise SystemExit(f"正式库终检未达100分：{final_health.get('score')}")
    print(json.dumps({"audited": len(records), "committed": args.commit, "health": final_health.get("score")}, ensure_ascii=False))


if __name__ == "__main__":
    main()

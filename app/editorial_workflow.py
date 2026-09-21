from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any


STAGES = (
    "plan",
    "generate",
    "sanitize",
    "contract_scan",
    "audit",
    "repair",
    "accept",
    "lock",
    "memory_extract",
    "memory_apply",
)
AUTHORITY_STATES = {"candidate", "reviewed", "accepted", "locked"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_hash(value: Any) -> str:
    if isinstance(value, str):
        payload = value
    else:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_chapter_workflow(chapter: dict[str, Any]) -> dict[str, Any]:
    content = str(chapter.get("content", ""))
    legacy_locked = bool(chapter.get("accepted_content_hash")) or str(
        chapter.get("memory_status", "")
    ) == "committed"
    state = str(chapter.get("authority_state", "")).strip().lower()
    if state not in AUTHORITY_STATES:
        state = "locked" if legacy_locked else ("candidate" if content.strip() else "candidate")
    chapter["authority_state"] = state
    chapter.setdefault("locked_content_hash", chapter.get("accepted_content_hash", "") if legacy_locked else "")
    chapter.setdefault("locked_at", "")
    chapter.setdefault("locked_by", "")
    if not isinstance(chapter.get("lock_receipt"), dict):
        chapter["lock_receipt"] = {}
    workflow = chapter.get("workflow")
    if not isinstance(workflow, dict):
        workflow = {}
        chapter["workflow"] = workflow
    workflow["version"] = 1
    stages = workflow.get("stages")
    if not isinstance(stages, dict):
        stages = {}
        workflow["stages"] = stages
    for stage in STAGES:
        item = stages.get(stage)
        if not isinstance(item, dict):
            item = {}
            stages[stage] = item
        item.setdefault("status", "pending")
        item.setdefault("input_hash", "")
        item.setdefault("output_hash", "")
        item.setdefault("attempts", 0)
        item.setdefault("started_at", "")
        item.setdefault("completed_at", "")
        item.setdefault("error", "")
    return workflow


def begin_stage(chapter: dict[str, Any], stage: str, payload: Any) -> tuple[dict[str, Any], bool]:
    if stage not in STAGES:
        raise ValueError(f"未知章节阶段：{stage}")
    workflow = ensure_chapter_workflow(chapter)
    item = workflow["stages"][stage]
    input_hash = stable_hash(payload)
    if item.get("status") == "completed" and item.get("input_hash") == input_hash:
        return item, True
    item.update(
        {
            "status": "running",
            "input_hash": input_hash,
            "output_hash": "",
            "attempts": int(item.get("attempts", 0) or 0) + 1,
            "started_at": now(),
            "completed_at": "",
            "error": "",
        }
    )
    return item, False


def complete_stage(chapter: dict[str, Any], stage: str, output: Any = None) -> dict[str, Any]:
    item = ensure_chapter_workflow(chapter)["stages"][stage]
    item.update(
        {
            "status": "completed",
            "output_hash": stable_hash(output if output is not None else {}),
            "completed_at": now(),
            "error": "",
        }
    )
    return item


def fail_stage(chapter: dict[str, Any], stage: str, error: Any) -> dict[str, Any]:
    item = ensure_chapter_workflow(chapter)["stages"][stage]
    item.update({"status": "failed", "completed_at": now(), "error": str(error)[:1200]})
    return item


def reset_from_stage(chapter: dict[str, Any], stage: str) -> list[str]:
    if stage not in STAGES:
        raise ValueError(f"未知章节阶段：{stage}")
    stages = ensure_chapter_workflow(chapter)["stages"]
    reset = list(STAGES[STAGES.index(stage) :])
    for name in reset:
        stages[name].update(
            {
                "status": "pending",
                "input_hash": "",
                "output_hash": "",
                "started_at": "",
                "completed_at": "",
                "error": "",
            }
        )
    if stage in {"generate", "sanitize", "contract_scan", "audit", "repair", "accept", "lock"}:
        chapter["authority_state"] = "candidate"
        chapter["locked_content_hash"] = ""
        chapter["locked_at"] = ""
        chapter["locked_by"] = ""
        chapter["lock_receipt"] = {}
    return reset


def lock_chapter(
    chapter: dict[str, Any],
    *,
    actor: str,
    audit: dict[str, Any] | None = None,
    contract_scan: dict[str, Any] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    content = str(chapter.get("content", "")).strip()
    if len(content) < 100:
        raise ValueError("锁定的章节正文至少需要 100 字")
    audit = audit if isinstance(audit, dict) else {}
    contract_scan = contract_scan if isinstance(contract_scan, dict) else {}
    failures: list[str] = []
    if audit and str(audit.get("verdict", "revise")) != "pass":
        failures.append("证据化审校未通过")
    if contract_scan and not bool(contract_scan.get("passed", False)):
        failures.append("硬契约扫描未通过")
    if failures and not force:
        raise ValueError("；".join(failures))
    digest = stable_hash(content)
    chapter.update(
        {
            "authority_state": "locked",
            "locked_content_hash": digest,
            "accepted_content_hash": digest,
            "locked_at": now(),
            "locked_by": actor or "editor",
            "lock_receipt": {
                "content_hash": digest,
                "audit_score": audit.get("score"),
                "audit_verdict": audit.get("verdict", ""),
                "contract_scan_id": contract_scan.get("scan_id", ""),
                "contract_passed": contract_scan.get("passed") if contract_scan else None,
                "forced": bool(force),
                "created_at": now(),
            },
        }
    )
    complete_stage(chapter, "lock", chapter["lock_receipt"])
    return chapter["lock_receipt"]


def ensure_repair_queue(project: dict[str, Any]) -> list[dict[str, Any]]:
    queue = project.get("repair_queue")
    if not isinstance(queue, list):
        queue = []
        project["repair_queue"] = queue
    # Keep the list object stable. Callers may hold a reference while
    # enqueue_repair() normalizes the queue again; replacing the list here
    # would make those callers return a stale snapshot.
    queue[:] = [item for item in queue if isinstance(item, dict)][-1000:]
    return queue


def issue_key(chapter_id: str, category: str, message: str) -> str:
    return stable_hash({"chapter_id": chapter_id, "category": category, "message": message})[:24]


def enqueue_repair(
    project: dict[str, Any],
    chapter: dict[str, Any],
    issue: dict[str, Any],
    *,
    source: str = "audit",
) -> tuple[dict[str, Any], bool]:
    queue = ensure_repair_queue(project)
    category = str(issue.get("category") or source or "质量问题")
    message = str(issue.get("message") or issue.get("detail") or "待精修问题").strip()
    key = issue_key(str(chapter.get("id", "")), category, message)
    existing = next((item for item in queue if item.get("issue_key") == key and item.get("status") != "dismissed"), None)
    if existing:
        existing["updated_at"] = now()
        existing["evidence"] = deepcopy(issue.get("evidence", existing.get("evidence", [])))
        return existing, True
    item = {
        "id": str(uuid.uuid4()),
        "issue_key": key,
        "chapter_id": str(chapter.get("id", "")),
        "chapter_title": str(chapter.get("title", "")),
        "category": category,
        "severity": str(issue.get("severity", "medium")).lower(),
        "status": "queued",
        "source": source,
        "message": message,
        "suggestion": str(issue.get("suggestion", "")),
        "evidence": deepcopy(issue.get("evidence", [])),
        "attempts": 0,
        "created_at": now(),
        "updated_at": now(),
    }
    queue.append(item)
    return item, False


def rebuild_repair_queue(project: dict[str, Any]) -> list[dict[str, Any]]:
    queue = ensure_repair_queue(project)
    for chapter in project.get("chapters", []):
        execution = chapter.get("execution", {}) if isinstance(chapter.get("execution"), dict) else {}
        if (
            execution.get("status") == "accepted"
            and chapter.get("authority_state") == "locked"
        ):
            # Accepted audits may retain optional AI suggestions for reference.
            # They are not quality debts and must not reappear as queued repairs
            # the next time the queue is rebuilt. Memory-rebuild failures remain
            # actionable even when the prose itself is locked.
            for item in queue:
                if (
                    str(item.get("chapter_id", "")) == str(chapter.get("id", ""))
                    and str(item.get("status", "queued")) in {"queued", "in_progress"}
                    and str(item.get("category", "")) != "记忆回灌"
                ):
                    item["status"] = "resolved"
                    item["resolved_by"] = "accepted-audit"
                    item["updated_at"] = now()
            continue
        for issue in execution.get("issues", []) if isinstance(execution.get("issues"), list) else []:
            if isinstance(issue, dict):
                enqueue_repair(project, chapter, issue, source="audit")
            elif str(issue).strip():
                enqueue_repair(project, chapter, {"message": str(issue)}, source="audit")
        if execution.get("status") == "quality_debt" and not execution.get("issues"):
            enqueue_repair(
                project,
                chapter,
                {"category": "质量债", "severity": "high", "message": "本章未通过自动审校，需要精修后重新锁定。"},
                source="director",
            )
    return queue

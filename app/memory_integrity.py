from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any


MEMORY_COMMIT_STATUSES = {
    "settlement_pending",
    "settlement_extracted",
    "committed",
    "state_degraded",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def memory_key(value: Any) -> str:
    return re.sub(
        r"[\s，。！？、；：,.!?;:'\"“”‘’—…（）()]",
        "",
        str(value or ""),
    ).casefold()


def chapter_content_hash(content: Any) -> str:
    normalized = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def derive_story_so_far(project: dict[str, Any], max_chars: int = 4000) -> str:
    """Build a trustworthy rolling digest from accepted chapter summaries.

    A model may propose each chapter summary, but it cannot overwrite the whole-book
    digest in one response. Keeping the digest as a deterministic projection prevents
    one malformed settlement from silently erasing or rewriting earlier causality.
    """
    lines: list[str] = []
    for number, chapter in enumerate(project.get("chapters", []), start=1):
        if not isinstance(chapter, dict):
            continue
        summary = str(chapter.get("summary", "")).strip()
        content = str(chapter.get("content", "")).strip()
        if not summary or not content:
            continue
        title = str(chapter.get("title", "")).strip() or f"第{number}章"
        lines.append(f"第{number}章·{title}：{summary}")
    if not lines:
        return ""
    rendered = "\n".join(lines)
    if len(rendered) <= max_chars:
        return rendered

    head = lines[:2]
    used = len("\n".join(head)) + len("\n[…中段章节摘要已按预算省略…]\n")
    tail: list[str] = []
    for line in reversed(lines[2:]):
        extra = len(line) + 1
        if tail and used + extra > max_chars:
            break
        if not tail and used + extra > max_chars:
            tail.append(line[-max(80, max_chars - used) :])
            break
        tail.append(line)
        used += extra
    tail.reverse()
    return (
        "\n".join(head)
        + "\n[…中段章节摘要已按预算省略…]\n"
        + "\n".join(tail)
    )[:max_chars]


def _commit_list(project: dict[str, Any]) -> list[dict[str, Any]]:
    memory = project.setdefault("memory", {})
    commits = memory.setdefault("commits", [])
    if not isinstance(commits, list):
        commits = []
        memory["commits"] = commits
    return commits


def get_memory_commit(
    project: dict[str, Any], commit_id: str
) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in _commit_list(project)
            if isinstance(item, dict) and str(item.get("id", "")) == commit_id
        ),
        None,
    )


def begin_memory_commit(
    project: dict[str, Any], chapter: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    content_hash = chapter_content_hash(chapter.get("content", ""))
    commits = _commit_list(project)
    existing = next(
        (
            item
            for item in reversed(commits)
            if isinstance(item, dict)
            and str(item.get("chapter_id", "")) == str(chapter.get("id", ""))
            and str(item.get("content_hash", "")) == content_hash
        ),
        None,
    )
    now = utc_now()
    if existing:
        reused = True
        if existing.get("status") == "state_degraded":
            existing["status"] = "settlement_pending"
            existing["attempts"] = max(1, int(existing.get("attempts", 1))) + 1
            existing["error"] = ""
            existing["updated_at"] = now
        chapter["memory_status"] = existing.get("status", "settlement_pending")
        chapter["memory_commit_id"] = existing.get("id", "")
        chapter["accepted_content_hash"] = content_hash
        return existing, reused

    commit = {
        "id": str(uuid.uuid4()),
        "chapter_id": str(chapter.get("id", "")),
        "chapter_title": str(chapter.get("title", "")),
        "content_hash": content_hash,
        "status": "settlement_pending",
        "attempts": 1,
        "error": "",
        "warnings": [],
        "created_at": now,
        "updated_at": now,
        "committed_at": "",
    }
    commits.append(commit)
    project["memory"]["commits"] = commits[-200:]
    chapter["memory_status"] = "settlement_pending"
    chapter["memory_commit_id"] = commit["id"]
    chapter["accepted_content_hash"] = content_hash
    return commit, False


def validate_memory_commit(
    project: dict[str, Any],
    chapter: dict[str, Any],
    commit_id: str,
) -> dict[str, Any]:
    commit = get_memory_commit(project, commit_id)
    if not commit:
        raise ValueError("记忆提交不存在或已被清理")
    if str(commit.get("chapter_id", "")) != str(chapter.get("id", "")):
        raise ValueError("记忆提交与目标章节不一致")
    if str(commit.get("content_hash", "")) != chapter_content_hash(
        chapter.get("content", "")
    ):
        raise ValueError("章节正文在结算期间发生变化，请重新接受并创建记忆提交")
    return commit


def mark_memory_commit(
    project: dict[str, Any],
    chapter: dict[str, Any],
    commit_id: str,
    status: str,
    *,
    error: str = "",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    if status not in MEMORY_COMMIT_STATUSES:
        raise ValueError(f"不支持的记忆提交状态：{status}")
    commit = validate_memory_commit(project, chapter, commit_id)
    now = utc_now()
    commit["status"] = status
    commit["updated_at"] = now
    commit["error"] = str(error or "")[:1200]
    if warnings is not None:
        commit["warnings"] = [str(item)[:500] for item in warnings if str(item).strip()][
            -30:
        ]
    if status == "committed":
        commit["committed_at"] = now
    chapter["memory_status"] = status
    chapter["memory_commit_id"] = commit_id
    return commit


from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def stable_prefixes(project: dict[str, Any], chapter: dict[str, Any]) -> dict[str, str]:
    project_payload = {
        "project_id": project.get("id", ""),
        "title": project.get("title", ""),
        "genre": project.get("genre", ""),
        "author_intent": project.get("author_intent", ""),
        "book_rules": project.get("book_rules", ""),
        "narrative": project.get("narrative", {}),
        "style": {
            key: project.get("style", {}).get(key, "")
            for key in ("name", "profile", "dos", "donts")
        },
    }
    chapter_payload = {
        "chapter_id": chapter.get("id", ""),
        "title": chapter.get("title", ""),
        "route": chapter.get("route", {}),
        "plan": chapter.get("plan", {}),
    }
    return {
        "project_prefix": _canonical(project_payload),
        "chapter_prefix": _canonical(chapter_payload),
        "project_prefix_hash": _hash(project_payload),
        "chapter_prefix_hash": _hash(chapter_payload),
    }


def prepare_messages(messages: list[dict[str, Any]], project: dict[str, Any], chapter: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    prefixes = stable_prefixes(project, chapter)
    header = (
        "【稳定项目前缀】\n" + prefixes["project_prefix"]
        + "\n【稳定章节前缀】\n" + prefixes["chapter_prefix"]
        + "\n【本次阶段指令】\n"
    )
    prepared = [dict(item) for item in messages]
    if prepared and prepared[0].get("role") == "system":
        prepared[0]["content"] = header + str(prepared[0].get("content", ""))
    else:
        prepared.insert(0, {"role": "system", "content": header})
    return prepared, prefixes


def new_session(project: dict[str, Any], chapter: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    prefixes = stable_prefixes(project, chapter)
    session = existing if isinstance(existing, dict) else {}
    if session.get("chapter_prefix_hash") != prefixes["chapter_prefix_hash"]:
        session["turns"] = []
        session["checkpoints"] = []
    session.update(
        {
            "version": 1,
            "project_id": str(project.get("id", "")),
            "chapter_id": str(chapter.get("id", "")),
            "project_prefix_hash": prefixes["project_prefix_hash"],
            "chapter_prefix_hash": prefixes["chapter_prefix_hash"],
            "updated_at": _now(),
        }
    )
    session.setdefault("turns", [])
    session.setdefault("checkpoints", [])
    return session


def add_turn(session: dict[str, Any], role: str, content: str, kind: str) -> dict[str, Any]:
    turn = {"id": _hash([role, content, kind, _now()])[:24], "role": role, "kind": kind, "content": str(content)[:60_000], "created_at": _now()}
    session.setdefault("turns", []).append(turn)
    session["turns"] = session["turns"][-40:]
    session["updated_at"] = _now()
    return turn


def checkpoint(session: dict[str, Any], label: str = "") -> dict[str, Any]:
    item = {"id": _hash([label, len(session.get("turns", [])), _now()])[:24], "label": label, "turn_count": len(session.get("turns", [])), "created_at": _now()}
    session.setdefault("checkpoints", []).append(item)
    session["checkpoints"] = session["checkpoints"][-20:]
    return item


def rollback(session: dict[str, Any], checkpoint_id: str) -> dict[str, Any]:
    item = next((value for value in session.get("checkpoints", []) if value.get("id") == checkpoint_id), None)
    if not item:
        raise ValueError("会话检查点不存在")
    session["turns"] = session.get("turns", [])[: int(item.get("turn_count", 0))]
    session["checkpoints"] = [value for value in session.get("checkpoints", []) if int(value.get("turn_count", 0)) <= int(item.get("turn_count", 0))]
    session["updated_at"] = _now()
    return session

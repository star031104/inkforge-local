"""Deterministically rebuild the formal Qince memory projection from reviewed assets."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import ensure_project_defaults, utc_now
from app.main import _apply_director_memory, store
from app.manuscript_quality import manuscript_health_report


ARTIFACTS = ROOT / "artifacts"
PROJECT_ID = "dd569883-b520-48d2-ac4a-eca36b028764"
CHAPTER_PATTERN = re.compile(r"^#{1,2}\s+(.+?)\s*$")
PROJECTED_CHARACTER_FIELDS = (
    "state",
    "location",
    "items",
    "emotion",
    "appearance_state",
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_editorial(number: int) -> tuple[str, str]:
    path = ARTIFACTS / f"qince-chapter-{number:03d}-editorial.md"
    raw = path.read_text(encoding="utf-8").strip()
    lines = raw.splitlines()
    match = CHAPTER_PATTERN.match(lines[0]) if lines else None
    if not match:
        raise ValueError(f"章节文件缺少标题：{path.name}")
    body = "\n".join(lines[1:]).strip()
    if not body:
        raise ValueError(f"章节正文为空：{path.name}")
    return match.group(1).strip(), body


def read_memory(number: int) -> dict[str, Any]:
    path = ARTIFACTS / f"qince-chapter-{number:03d}-memory.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"章节记忆不是对象：{path.name}")
    payload.pop("warnings", None)
    return payload


def memory_character_names(memories: list[dict[str, Any]]) -> list[str]:
    names: dict[str, str] = {}
    for memory in memories:
        for item in memory.get("character_updates", []):
            if isinstance(item, dict):
                name = str(item.get("name", "")).strip()
                if name:
                    names.setdefault(name.casefold(), name)
        for item in memory.get("relationship_updates", []):
            if not isinstance(item, dict):
                continue
            for field in ("left", "right", "from", "to"):
                name = str(item.get(field, "")).strip()
                if name:
                    names.setdefault(name.casefold(), name)
    return sorted(names.values(), key=lambda value: value.casefold())


def register_characters(project: dict[str, Any], names: list[str]) -> list[str]:
    known = {
        str(item.get("name", "")).strip().casefold()
        for item in project.get("characters", [])
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    }
    added: list[str] = []
    for name in names:
        if name.casefold() in known:
            continue
        project.setdefault("characters", []).append(
            {
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"inkforge:qince:character:{name}")),
                "name": name,
                "aliases": [],
                "role": "次要人物",
                "description": "",
                "personality": "",
                "values": "",
                "goal": "",
                "state": "",
                "location": "",
                "knowledge": "",
                "knowledge_ledger": [],
                "created_at": utc_now(),
            }
        )
        known.add(name.casefold())
        added.append(name)
    return added


def reset_projection(project: dict[str, Any]) -> None:
    memory = project.setdefault("memory", {})
    commits = deepcopy(memory.get("commits", []))
    memory.update(
        {
            "state_version": 4,
            "epistemic_schema_version": 1,
            "story_so_far": "",
            "story_digest_candidate": {},
            "facts": [],
            "plot_threads": [],
            "timeline": [],
            "relationships": [],
            "continuity_notes": [],
            "description_ledger": [],
            "commits": commits,
        }
    )
    project["knowledge"] = {
        "schema_version": 1,
        "entities": [],
        "facts": [],
        "relations": [],
        "review_queue": [],
    }
    for character in project.get("characters", []):
        if not isinstance(character, dict):
            continue
        for field in PROJECTED_CHARACTER_FIELDS:
            character[field] = ""
        character["last_state_chapter_number"] = 0
        character["last_state_chapter_id"] = ""
        character["knowledge_ledger"] = []
    for chapter in project.get("chapters", []):
        if not isinstance(chapter, dict):
            continue
        chapter["settlement"] = {}
        chapter.setdefault("execution", {})["warnings"] = []


def assert_formal_text(project: dict[str, Any], editorial: list[tuple[str, str]]) -> list[str]:
    chapters = project.get("chapters", [])
    if len(chapters) != 100:
        raise SystemExit(f"正式项目章节数不是100：{len(chapters)}")
    hashes: list[str] = []
    for number, ((_, expected), chapter) in enumerate(zip(editorial, chapters), 1):
        actual = str(chapter.get("content", "")).strip()
        if actual != expected:
            raise SystemExit(f"第{number}章正式正文与审定资产不一致，停止重建")
        hashes.append(digest(actual))
    return hashes


def main() -> None:
    stored = store.get(PROJECT_ID)
    if not stored:
        raise SystemExit(f"找不到正式项目：{PROJECT_ID}")
    if str(stored.get("settings", {}).get("api_key", "")).strip():
        raise SystemExit("正式项目意外保存了 API 密钥，停止重建")

    editorial = [read_editorial(number) for number in range(1, 101)]
    memories = [read_memory(number) for number in range(1, 101)]
    before_hashes = assert_formal_text(stored, editorial)
    project = ensure_project_defaults(deepcopy(stored))
    added = register_characters(project, memory_character_names(memories))
    reset_projection(project)

    replay_warnings: list[dict[str, Any]] = []
    for number, memory in enumerate(memories, 1):
        chapter = project["chapters"][number - 1]
        warnings = _apply_director_memory(
            project,
            chapter,
            memory,
            rebuild_current_projection=True,
        )
        if warnings:
            replay_warnings.append({"chapter": number, "warnings": warnings})
    if replay_warnings:
        raise SystemExit(
            "记忆重放仍有警告，未写入正式库：\n"
            + json.dumps(replay_warnings, ensure_ascii=False, indent=2)
        )

    after_hashes = assert_formal_text(project, editorial)
    if after_hashes != before_hashes:
        raise SystemExit("记忆重放改变了正文哈希，未写入正式库")
    health = manuscript_health_report(project)
    if health.get("score") != 100 or health.get("recommendations"):
        raise SystemExit(
            "重建后的全书健康检查未达100分，未写入正式库：\n"
            + json.dumps(health, ensure_ascii=False, indent=2)
        )
    execution_warning_chapters = [
        number
        for number, chapter in enumerate(project["chapters"], 1)
        if chapter.get("execution", {}).get("warnings")
    ]
    if execution_warning_chapters:
        raise SystemExit(f"仍有章节执行告警：{execution_warning_chapters}")

    backup = store.backup(ROOT / "data" / "backups", keep=24)
    saved = store.save(PROJECT_ID, project, reason="qince-formal-memory-rebuild-v2")
    saved_hashes = assert_formal_text(saved, editorial)
    if saved_hashes != before_hashes:
        raise SystemExit("正式保存后正文哈希不一致")

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_id": PROJECT_ID,
        "database_backup": backup.name,
        "chapter_count": len(saved["chapters"]),
        "body_sha256_unchanged": saved_hashes == before_hashes,
        "characters_added": added,
        "character_count": len(saved.get("characters", [])),
        "fact_count": len(saved.get("memory", {}).get("facts", [])),
        "thread_count": len(saved.get("memory", {}).get("plot_threads", [])),
        "timeline_count": len(saved.get("memory", {}).get("timeline", [])),
        "relationship_count": len(saved.get("memory", {}).get("relationships", [])),
        "knowledge_fact_count": len(saved.get("knowledge", {}).get("facts", [])),
        "knowledge_relation_count": len(saved.get("knowledge", {}).get("relations", [])),
        "replay_warning_count": 0,
        "execution_warning_chapters": [],
        "health": health,
    }
    output = ARTIFACTS / "qince-formal-memory-rebuild.json"
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

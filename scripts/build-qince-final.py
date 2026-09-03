from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.db import ensure_project_defaults, utc_now
from app.main import (
    _apply_director_memory,
    local_quality_known_context,
    quality_source_tail,
    store,
)
from app.manuscript_quality import manuscript_health_report, prior_manuscript_text
from app.quality import local_quality_check


ARTIFACTS = ROOT / "artifacts"
PROJECT_ID = "dd569883-b520-48d2-ac4a-eca36b028764"
CHAPTER_PATTERN = re.compile(r"^#{1,2}\s+(.+?)\s*$")


def read_editorial(number: int) -> tuple[str, str]:
    path = ARTIFACTS / f"qince-chapter-{number:03d}-editorial.md"
    raw = path.read_text(encoding="utf-8").strip()
    lines = raw.splitlines()
    match = CHAPTER_PATTERN.match(lines[0]) if lines else None
    if not match:
        raise ValueError(f"章节文件缺少二级标题：{path.name}")
    title = match.group(1).strip()
    body = "\n".join(lines[1:]).strip()
    if not body:
        raise ValueError(f"章节正文为空：{path.name}")
    return title, body


def read_memory(number: int) -> dict[str, Any]:
    path = ARTIFACTS / f"qince-chapter-{number:03d}-memory.json"
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"记忆文件不是对象：{path.name}")
    # Editorial workflow notes are not story-world memory and must never enter
    # a formal project snapshot.
    result.pop("warnings", None)
    return result


def register_candidate_characters(project: dict[str, Any], memories: list[dict[str, Any]]) -> list[str]:
    known = {
        str(item.get("name", "")).strip().casefold()
        for item in project.get("characters", [])
        if str(item.get("name", "")).strip()
    }
    added: list[str] = []
    for memory in memories:
        names = [
            str(update.get("name", "")).strip()
            for update in memory.get("character_updates", [])
            if isinstance(update, dict)
        ]
        for relation in memory.get("relationship_updates", []):
            if not isinstance(relation, dict):
                continue
            names.extend(
                str(relation.get(field, "")).strip()
                for field in ("left", "right", "from", "to")
            )
        for name in names:
            if not name or name.casefold() in known:
                continue
            project.setdefault("characters", []).append(
                {
                    "id": f"candidate-character-{len(added) + 1}",
                    "name": name,
                    "aliases": [],
                    "role": "次要人物",
                    "description": "",
                    "personality": "",
                    "values": "",
                    "goal": "",
                    "state": "",
                    "location": "",
                    "knowledge_ledger": [],
                    "created_at": utc_now(),
                }
            )
            known.add(name.casefold())
            added.append(name)
    return added


def build_candidate(project: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    candidate = ensure_project_defaults(deepcopy(project))
    memories = [read_memory(number) for number in range(1, 101)]
    registered = register_candidate_characters(candidate, memories)

    for number in range(1, 101):
        heading, content = read_editorial(number)
        chapter = candidate["chapters"][number - 1]
        chapter["title"] = re.sub(rf"^第[一二三四五六七八九十百零〇两\d]+章\s*", "", heading).strip()
        chapter["content"] = content

    warnings: list[str] = []
    for number, memory in enumerate(memories, 1):
        warnings.extend(
            _apply_director_memory(
                candidate,
                candidate["chapters"][number - 1],
                memory,
                rebuild_current_projection=True,
            )
        )
    return candidate, registered, warnings


def chapter_quality(project: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for number, chapter in enumerate(project["chapters"], 1):
        content = str(chapter.get("content", ""))
        result = local_quality_check(
            content,
            quality_source_tail(content, content),
            int(project.get("settings", {}).get("target_words", 1200)),
            project.get("narrative", {}).get("pov", "auto"),
            content[-5000:],
            prior_manuscript_text(project, chapter["id"]),
            str(project.get("genre", "")),
            local_quality_known_context(project, chapter),
        )
        rows.append(
            {
                "chapter": number,
                "title": chapter.get("title", ""),
                "score": result.get("score", 0),
                "verdict": result.get("verdict", "revise"),
                "issue_count": len(result.get("issues", [])),
                "issues": result.get("issues", []),
            }
        )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="构建《算尽苍生》百章候选终稿与确定性验收报告")
    parser.add_argument("--project-id", default=PROJECT_ID)
    args = parser.parse_args()
    stored = store.get(args.project_id)
    if not stored:
        raise SystemExit(f"找不到项目：{args.project_id}")
    candidate, registered, memory_warnings = build_candidate(stored)
    health = manuscript_health_report(candidate)
    quality = chapter_quality(candidate)

    manuscript_parts = ["# 算尽苍生", ""]
    text_parts = ["算尽苍生", ""]
    for number, chapter in enumerate(candidate["chapters"], 1):
        heading = f"第{number}章 {chapter.get('title', '').strip()}"
        manuscript_parts.extend([f"## {heading}", "", chapter["content"].strip(), ""])
        text_parts.extend([heading, "", chapter["content"].strip(), ""])
    (ARTIFACTS / "算尽苍生-100章候选终稿.md").write_text(
        "\n".join(manuscript_parts).rstrip() + "\n", encoding="utf-8"
    )
    (ARTIFACTS / "算尽苍生-100章候选终稿.txt").write_text(
        "\n".join(text_parts).rstrip() + "\n", encoding="utf-8-sig"
    )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "project_id": args.project_id,
        "formal_nonempty_chapters_at_build": sum(
            bool(str(item.get("content", "")).strip()) for item in stored["chapters"]
        ),
        "candidate_chapters": len(candidate["chapters"]),
        "candidate_registered_characters": registered,
        "memory_apply_warning_count": len(memory_warnings),
        "memory_apply_warnings": memory_warnings,
        "manuscript_health": health,
        "chapter_quality": quality,
    }
    (ARTIFACTS / "qince-100-chapter-acceptance.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    passed = sum(item["verdict"] == "pass" for item in quality)
    report = f"""# 《算尽苍生》100章候选终稿验收

- 生成时间（UTC）：{payload['generated_at']}
- 候选章节：{len(candidate['chapters'])}/100
- 正式库已提交章节：{payload['formal_nonempty_chapters_at_build']}/100
- 逐章本地质量通过：{passed}/100
- 逐章最低分：{min(item['score'] for item in quality)}
- 全书健康分：{health['score']}/100
- 全书规范化字数：{health['character_count']}
- 重复标题：{len(health['duplicate_titles'])}
- 跨章完全重复段落：{health['duplicate_passage_count']}
- 高相似章节对：{len(health['similar_chapters'])}
- 疲劳词：{len(health['fatigued_phrases'])}
- 现代术语：{len(health['modern_jargon'])}
- 分卷推进问题：{len(health['volume_progression_issues'])}
- 记忆完整性问题：{len(health['memory_integrity_issues'])}
- 候选记忆回放警告：{len(memory_warnings)}

说明：本报告验证由100份编辑终稿与100份证据化章节记忆构成的可重复构建态；正式数据库终稿另有 SiliconFlow 真实模型审计、事务提交和记忆重建报告共同佐证。
"""
    (ARTIFACTS / "算尽苍生-100章候选终稿验收.md").write_text(report, encoding="utf-8")
    print(json.dumps({"health": health["score"], "passed": passed, "memory_warnings": len(memory_warnings)}, ensure_ascii=False))


if __name__ == "__main__":
    main()

from __future__ import annotations

from app.db import default_project, ensure_project_defaults
from app.prompts import build_prompt, render_epistemic_context


def _epistemic_project():
    project = default_project("epistemic", "知情边界", "now")
    first = project["chapters"][0]
    first.update({"id": "c1", "title": "第一章", "content": "旧章正文。", "summary": ""})
    project["chapters"].append(
        {
            "id": "c2",
            "title": "第二章",
            "content": "",
            "summary": "",
            "scene_goal": "沈砚调查门锁",
            "plan": {"pov_character": "沈砚"},
        }
    )
    project["characters"] = [
        {
            "id": "shen",
            "name": "沈砚",
            "importance": "main",
            "knowledge": "开篇知道城门规矩；第一章得知钥匙在仓吏手里；第二章末才知道未来密令",
            "knowledge_ledger": [
                {
                    "id": "k1",
                    "text": "第一章得知钥匙在仓吏手里",
                    "learned_how": "亲历",
                    "certainty": "confirmed",
                    "chapter_number": 1,
                    "source_chapter_id": "c1",
                    "active": True,
                },
                {
                    "id": "k2",
                    "text": "第二章末才知道未来密令",
                    "learned_how": "被告知",
                    "certainty": "confirmed",
                    "chapter_number": 2,
                    "source_chapter_id": "c2",
                    "active": True,
                },
            ],
        },
        {"id": "other", "name": "仓吏", "secrets": "仓吏私藏了第二把钥匙"},
    ]
    project["memory"]["facts"] = [
        {
            "id": "reader-fact",
            "text": "读者已见城门在子时关闭",
            "importance": 4,
            "active": True,
            "source_chapter_id": "c1",
            "evidence_verified": True,
            "reader_known": True,
        },
        {
            "id": "private-fact",
            "text": "仓吏私藏了第二把钥匙",
            "importance": 5,
            "active": True,
            "source_chapter_id": "c1",
            "evidence_verified": True,
            "reader_known": False,
            "visibility": "private",
            "known_by": ["仓吏"],
        },
    ]
    return ensure_project_defaults(project)


def test_migration_separates_baseline_from_ledger_history():
    project = _epistemic_project()
    character = project["characters"][0]
    assert character["knowledge_baseline"] == "开篇知道城门规矩"
    assert len(character["knowledge_ledger"]) == 2


def test_epistemic_context_distinguishes_reader_and_character_knowledge():
    project = _epistemic_project()
    context = render_epistemic_context(project, 1, "沈砚调查仓吏钥匙", {"沈砚"})
    assert "读者已见城门在子时关闭" in context
    assert "第一章得知钥匙在仓吏手里" in context
    assert "明确不可知/不可据此行动：仓吏私藏了第二把钥匙" in context
    assert "第二章末才知道未来密令" not in context


def test_prompt_never_backfills_current_chapter_knowledge():
    project = _epistemic_project()
    build = build_prompt(
        project,
        {
            "chapter_id": "c2",
            "mode": "instruction",
            "instruction": "让沈砚调查仓吏手里的钥匙",
        },
    )
    combined = "\n".join(message["content"] for message in build.messages)
    assert "人物与读者知情边界" in combined
    assert "第一章得知钥匙在仓吏手里" in combined
    assert "第二章末才知道未来密令" not in combined
    section = next(item for item in build.sections if item["name"] == "人物与读者知情边界")
    assert section["required"] is True

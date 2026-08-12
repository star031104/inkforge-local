from app.db import default_project, ensure_project_defaults
from app.main import _apply_director_memory
from app.manuscript_quality import memory_integrity_issues
from app.memory import render_thread_agenda, select_thread_agenda, thread_lifecycle
from app.prompts import build_prompt


def _project_with_people():
    project = default_project("state-v2", "状态测试", "now")
    project["characters"] = [
        {"id": "a", "name": "沈砚", "importance": "main"},
        {"id": "b", "name": "嬴政", "importance": "supporting"},
    ]
    return ensure_project_defaults(project)


def test_state_v2_migrates_provenance_and_dynamic_fields():
    project = ensure_project_defaults(
        {
            "memory": {
                "facts": [{"text": "门已锁上"}],
                "plot_threads": [{"title": "钥匙去向", "status": "advanced"}],
                "timeline": [{"time": "夜", "event": "门被锁上"}],
            },
            "characters": [{"name": "林墨"}],
            "chapters": [{"id": "c1", "title": "第一章"}],
        }
    )
    assert project["memory"]["state_version"] == 2
    assert project["memory"]["facts"][0]["confidence"] == "confirmed"
    assert project["memory"]["plot_threads"][0]["status"] == "progressing"
    assert project["memory"]["timeline"][0]["participants"] == []
    assert project["memory"]["relationships"] == []
    assert project["characters"][0]["knowledge_ledger"] == []
    assert project["characters"][0]["appearance_state"] == ""
    assert project["chapters"][0]["settlement"] == {}


def test_memory_settlement_keeps_evidence_and_character_knowledge_source():
    project = _project_with_people()
    chapter = project["chapters"][0]
    chapter["content"] = "沈砚亲眼看见嬴政把铜钥匙交给仓吏，两人从此互不信任。"
    result = {
        "summary": "沈砚目睹钥匙转手。",
        "character_updates": [
            {
                "name": "沈砚",
                "knowledge_gains": [
                    {
                        "text": "铜钥匙在仓吏手中",
                        "learned_how": "亲历",
                        "certainty": "confirmed",
                        "evidence": "沈砚亲眼看见嬴政把铜钥匙交给仓吏",
                    }
                ],
                "evidence": "沈砚亲眼看见嬴政把铜钥匙交给仓吏",
            }
        ],
        "facts": [
            {
                "text": "铜钥匙由嬴政交给仓吏",
                "tags": ["铜钥匙", "仓吏"],
                "importance": 5,
                "evidence": "嬴政把铜钥匙交给仓吏",
            }
        ],
        "relationship_updates": [
            {
                "left": "沈砚",
                "right": "嬴政",
                "state": "互不信任",
                "evidence": "两人从此互不信任",
            }
        ],
        "scene_settlement": {
            "goal_achieved": "yes",
            "irreversible_changes": ["铜钥匙转手"],
            "open_questions": ["仓吏为何收钥匙"],
            "closing_state": "钥匙在仓吏手中",
        },
    }
    warnings = _apply_director_memory(project, chapter, result)
    assert warnings == []
    knowledge = project["characters"][0]["knowledge_ledger"][0]
    assert knowledge["source_chapter_id"] == chapter["id"]
    assert knowledge["evidence"]
    assert project["memory"]["facts"][0]["evidence_verified"] is True
    assert project["memory"]["relationships"][0]["state"] == "互不信任"
    assert chapter["settlement"]["goal_achieved"] == "yes"
    assert chapter["settlement"]["fact_ids"]


def test_unverified_delta_is_rejected_and_false_hook_close_is_downgraded():
    project = _project_with_people()
    chapter = project["chapters"][0]
    chapter["content"] = "沈砚只在门外听见一声轻响。"
    warnings = _apply_director_memory(
        project,
        chapter,
        {
            "facts": [
                {
                    "text": "嬴政已经离城",
                    "importance": 5,
                    "evidence": "嬴政骑马离城",
                }
            ],
            "plot_threads": [
                {
                    "title": "门后是谁",
                    "status": "closed",
                    "latest": "门后身份揭晓",
                    "payoff": "门后是仓吏",
                }
            ],
        },
    )
    assert not project["memory"]["facts"]
    assert project["memory"]["plot_threads"][0]["status"] == "progressing"
    assert len(warnings) == 2


def test_hook_agenda_prioritizes_stale_debt_and_prompt_exposes_contracts():
    project = _project_with_people()
    project["narrative"]["target_chapters"] = 30
    project["memory"]["plot_threads"] = [
        {
            "id": "hook-old",
            "title": "失踪木牍",
            "status": "open",
            "target_window": "near",
            "created_chapter_number": 1,
            "last_advanced_chapter": 1,
            "expected_payoff": "找出木牍去向",
        }
    ]
    for number in range(2, 8):
        project["chapters"].append(
            {
                "id": f"c{number}",
                "title": f"第{number}章",
                "summary": "别处的进展",
                "content": "别处的正文。" * 80,
                "plan": {},
            }
        )
    project = ensure_project_defaults(project)
    lifecycle = thread_lifecycle(project, project["memory"]["plot_threads"][0], 7)
    assert lifecycle["stale"] is True
    agenda = select_thread_agenda(project, "继续本章", 7)
    assert agenda[0]["id"] == "hook-old"
    assert "本章必须推进" in render_thread_agenda(agenda)

    current = project["chapters"][-1]
    prompt = build_prompt(
        project,
        {"chapter_id": current["id"], "mode": "continue", "instruction": "继续调查"},
    )
    combined = "\n".join(message["content"] for message in prompt.messages)
    assert "章节场景契约" in combined
    assert "伏笔与暗线治理议程" in combined
    assert "文笔执行简报" in combined
    assert "失踪木牍" in combined


def test_memory_health_reports_hook_debt_and_missing_payoff():
    project = _project_with_people()
    project["memory"]["plot_threads"] = [
        {"id": "closed", "title": "旧谜", "status": "closed", "payoff": ""},
        {
            "id": "open",
            "title": "长期失踪",
            "status": "open",
            "target_window": "near",
            "created_chapter_number": 1,
            "last_advanced_chapter": 1,
            "expected_payoff": "找到人",
        },
    ]
    project["chapters"] = [
        {"id": f"c{i}", "title": f"第{i}章", "content": "正文" * 80, "plan": {}}
        for i in range(1, 9)
    ]
    project = ensure_project_defaults(project)
    issues = memory_integrity_issues(project)
    categories = {item["category"] for item in issues}
    assert "线索回收缺口" in categories
    assert "线索债务" in categories

from app.db import default_project
from app.main import _apply_director_memory


def test_description_ledger_only_accepts_phrases_present_in_prose():
    project = default_project("ledger", "测试", "now")
    chapter = project["chapters"][0]
    chapter["content"] = "沈砚用指节轻敲竹简，停在那道被刮去的墨痕前。"
    result = {
        "summary": "沈砚发现墨痕。",
        "description_updates": [
            {"character": "沈砚", "aspect": "动作", "phrase": "沈砚用指节轻敲竹简"},
            {"character": "沈砚", "aspect": "眼神", "phrase": "眸光像寒星一样锐利"},
        ],
    }
    _apply_director_memory(project, chapter, result)
    ledger = project["memory"]["description_ledger"]
    assert [item["phrase"] for item in ledger] == ["沈砚用指节轻敲竹简"]
    assert ledger[0]["chapter_id"] == chapter["id"]


def test_description_ledger_deduplicates_same_verified_phrase():
    project = default_project("ledger", "测试", "now")
    chapter = project["chapters"][0]
    chapter["content"] = "沈砚用指节轻敲竹简，等待众人安静。"
    result = {
        "description_updates": [
            {"character": "沈砚", "aspect": "动作", "phrase": "沈砚用指节轻敲竹简"},
            {"character": "沈砚", "aspect": "动作", "phrase": "沈砚用指节轻敲竹简"},
        ]
    }
    _apply_director_memory(project, chapter, result)
    assert len(project["memory"]["description_ledger"]) == 1

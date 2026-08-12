from fastapi.testclient import TestClient
import pytest

from app.db import default_project
from app.main import app, validate_director_volume_contract
from app.manuscript_quality import manuscript_health_report, prompt_repetition_guard
from app.quality import local_quality_check


client = TestClient(app)


def _project_with_repetition():
    project = default_project("health-project", "全稿体检", "now")
    repeated = "王绾轻抚腰间玉印，说昔者穰侯应侯皆凭胸臆决断未闻败绩，此事不可轻改。"
    project["genre"] = "战国历史架空"
    project["chapters"] = [
        {"id": "c1", "title": "旧算", "content": repeated + "\n\n" + "柔性冗余模型需要数据参数。" * 8},
        {"id": "c2", "title": "旧算", "content": repeated + "\n\n" + "众人仍旧围绕同一问题争执。" * 10},
        {"id": "c3", "title": "再算", "content": repeated + "\n\n" + "秦策把简牍推到灯下。" * 10},
    ]
    return project


def test_manuscript_health_finds_cross_chapter_repetition_and_language_fatigue():
    report = manuscript_health_report(_project_with_repetition())
    assert report["duplicate_titles"]
    assert report["duplicate_passage_count"] >= 1
    assert report["fatigued_phrases"]
    assert {item["term"] for item in report["modern_jargon"]} >= {"模型", "数据", "参数"}
    assert report["score"] < 100


def test_health_api_accepts_unsaved_project_snapshot():
    response = client.post(
        "/api/project/manuscript-health", json={"project": _project_with_repetition()}
    )
    assert response.status_code == 200
    assert response.json()["duplicate_passage_count"] >= 1


def test_local_quality_rejects_passage_copied_from_an_earlier_chapter():
    old = "秦策把三枚算筹推到案前，命人逐户核验名籍，并把结果刻在新简上。"
    draft = old + "\n\n" + "众人沉默着接受了这一结果。" * 20
    result = local_quality_check(
        draft,
        target_words=100,
        prior_text=old,
        genre="战国历史架空",
    )
    assert any(item["category"].startswith("跨章重复") for item in result["issues"])
    assert result["verdict"] == "revise"


def test_prompt_guard_contains_global_fatigue_not_only_two_recent_chapters():
    project = _project_with_repetition()
    project["chapters"].append({"id": "c4", "title": "新局", "content": ""})
    guard = prompt_repetition_guard(project, "c4")
    assert "全书疲劳词" in guard
    assert "已经跨章复用过的句段" in guard


def _contract(number: int, **updates):
    goals = {
        1: "秦策在边关保住三日军粮，却因公开损耗数字第一次触犯丞相府权限",
        2: "户籍清查揭开地方隐户网络，嬴政必须在增税与保留地方合作之间选择",
        3: "军功审核逼迫新旧将领公开利益，朝廷因此失去一支原本可靠的边军支持",
    }
    value = {
        "number": number,
        "title": f"第{number}卷",
        "goal": goals.get(number, f"第{number}阶段产生独有的新局面"),
        "conflict": "秦策与具体反对者围绕执行权和可见代价作出不可兼得的选择",
        "ending_state": f"第{number}阶段结束后人物、资源与权力关系进入新的稳定状态",
        "bridge_to_next": "本卷结果暴露出下一层制度问题",
        "theme_test": f"用第{number}种具体事件检验效率与人命的冲突",
        "primary_arena": f"第{number}种独立故事场域",
        "time_span": f"第{number}月至第{number + 1}月",
        "irreversible_change": f"第{number}阶段造成独有且不可撤销的权力转移",
        "character_choice": f"秦策在第{number}阶段放弃一项明确利益以保住另一价值",
        "new_story_question": f"第{number}阶段完成后产生的下一层问题",
    }
    value.update(updates)
    return value


def test_volume_contract_rejects_repeated_arena_and_irreversible_change():
    first = _contract(1)
    second = _contract(
        2,
        ending_state="隐户名单公开后，地方豪强失去暗中转移赋役的渠道，廷尉接管复核",
        theme_test="一个为救灾隐瞒户数的县令被公开审判，迫使众人重估程序正义",
        primary_arena=first["primary_arena"],
        irreversible_change=first["irreversible_change"],
        character_choice="嬴政必须在立刻征税和保留灾区人口之间公开选择",
    )
    with pytest.raises(ValueError, match="主要故事场域|不可逆变化"):
        validate_director_volume_contract(
            {"contract": second},
            {"number": 2, "chapter_start": 13, "chapter_end": 24, "chapter_count": 12},
            [first],
        )


def test_nonfinal_volume_cannot_spend_the_book_ending_early():
    ending = "秦国完成统一而秦策拒绝成为永恒帝师，制度留下辉煌但危险的裂缝"
    contract = _contract(2, ending_state=ending)
    with pytest.raises(ValueError, match="提前兑现"):
        validate_director_volume_contract(
            {"contract": contract},
            {"number": 2, "chapter_start": 13, "chapter_end": 24, "chapter_count": 12},
            [_contract(1)],
            ending,
            False,
        )

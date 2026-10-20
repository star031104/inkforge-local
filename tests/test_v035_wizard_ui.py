"""v0.35 new-book wizard (genre templates) and UI polish: director progress
chip, planning volume tabs and chapter score badges."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.core.config import APP_VERSION
from app.storage.project_store import ProjectStore
from app.planning.genre_templates import (
    ARCHETYPES,
    GENRE_TEMPLATES,
    apply_template,
    list_templates,
    project_has_work,
    volume_instruction,
)

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def _read(relative: str) -> str:
    return (STATIC / relative).read_text(encoding="utf-8")


@pytest.fixture()
def store(tmp_path):
    return ProjectStore(tmp_path / "inkforge.sqlite3", tmp_path / "secret.key")


# ------------------------------------------------------------ templates

def test_required_genres_exist_with_complete_skeletons():
    names = {template["name"] for template in GENRE_TEMPLATES.values()}
    for wanted in ("历史", "悬疑推理", "言情", "玄幻仙侠", "科幻", "现实文学", "通用"):
        assert any(wanted in name for name in names), wanted
    for template_id, template in GENRE_TEMPLATES.items():
        narrative = template["narrative"]
        assert {"pov", "tense", "tone", "structure_profile"} <= set(narrative), template_id
        assert template["book_rules"], template_id
        assert template["banned_phrases"], template_id
        assert all(len(item["phrase"]) >= 2 and item["reason"] for item in template["banned_phrases"])
        assert template["world_entries"], template_id
        for entry in template["world_entries"]:
            assert entry["title"] and entry["keys"] and entry["content"].startswith("（待填写）")
            assert isinstance(entry["constant"], bool)
        assert [card["archetype"] for card in template["characters"]] == list(ARCHETYPES)
        for card in template["characters"]:
            assert all(card.get(key) for key in ("role", "hard_limits", "knowledge", "secrets", "voice"))
        settings = template["settings"]
        assert settings["target_words"] >= 300 and settings["target_chapters"] >= 1 and settings["volumes"] >= 1


def test_history_template_has_constant_entries_and_era_dependent_banned_words():
    history = GENRE_TEMPLATES["history"]
    constant = {entry["title"] for entry in history["world_entries"] if entry["constant"]}
    assert {"称谓与礼仪", "器物与生活", "编年"} <= constant
    assert "制度" in {entry["title"] for entry in history["world_entries"]}
    banned = {item["phrase"]: item for item in history["banned_phrases"]}
    # Era-dependent words are suggestions the author opts into, not defaults.
    for phrase in ("皇上", "万岁", "银两"):
        assert banned[phrase]["default"] is False
    assert any(item["default"] for item in history["banned_phrases"])


def test_list_templates_returns_copies():
    first = list_templates()
    first[0]["book_rules"].append("被篡改")
    assert "被篡改" not in list_templates()[0]["book_rules"]


# ------------------------------------------------------------ apply

def test_apply_to_fresh_project_fills_everything_and_survives_save(store):
    project = store.create("我的新故事")
    assert not project_has_work(project)
    answers = {
        "title": "账簿", "premise": "落第书生卷入科场舞弊案", "protagonist_name": "沈砚",
        "protagonist": "落第书生", "want": "洗清冤屈", "obstacle": "座师设局", "ending": "以名声换真相",
        "target_chapters": 90, "target_words": 2600, "volumes": 3, "story_mode": "long",
    }
    filled, summary = apply_template(project, "history", answers, {})
    assert summary["fresh_project"] is True
    assert summary["added"] == {"book_rules": 5, "banned_phrases": 5, "world_entries": 4, "characters": 4}
    saved = store.save(project["id"], filled, "new-book-wizard", expected_updated_at=project["updated_at"])
    assert saved["title"] == "账簿"
    assert saved["genre"] == "历史"
    assert saved["premise"].startswith("落第书生卷入科场舞弊案")
    assert saved["narrative"]["pov"] == "third_limited"
    assert saved["narrative"]["structure_profile"] == "adaptive"
    assert saved["narrative"]["ending_direction"] == "以名声换真相"
    assert saved["narrative"]["target_chapters"] == 90
    assert saved["narrative"]["preferred_volume_count"] == 3
    assert saved["settings"]["target_words"] == 2600
    assert len(saved["book_rules"].splitlines()) == 5
    assert "皇上" not in saved["governance"]["banned_phrases"]
    assert "搞定" in saved["governance"]["banned_phrases"]
    world = {entry["title"]: entry for entry in saved["world_entries"]}
    assert world["称谓与礼仪"]["constant"] is True and world["称谓与礼仪"]["canon"] == "hard"
    # Placeholders never reach the writing context until the author enables them.
    assert all(entry["enabled"] is False for entry in saved["world_entries"])
    cast = {card["wizard_archetype"]: card for card in saved["characters"]}
    hero = cast["主角"]
    assert hero["name"] == "沈砚" and hero["goal"] == "洗清冤屈" and hero["importance"] == "main"
    assert hero["hard_limits"] == "" and hero["wizard_prompts"]["hard_limits"]
    assert cast["对手"]["name"] == "" and cast["对手"]["hard_limits"].startswith("（待填写）")
    assert "座师设局" in cast["对手"]["description"]


def test_apply_never_overwrites_existing_content():
    project = {
        "title": "旧稿", "genre": "武侠", "premise": "已有构想", "book_rules": "旧规则",
        "narrative": {"pov": "first", "tone": "冷峻", "target_chapters": 12},
        "settings": {"target_words": 900},
        "governance": {"banned_phrases": ["旧词"]},
        "world_entries": [{"title": "编年", "content": "作者写的编年"}],
        "characters": [{"name": "阿青", "role": "主角"}],
        "chapters": [{"content": "正文已经开始。"}],
    }
    filled, summary = apply_template(project, "history", {"title": "新名", "premise": "新构想", "target_words": 3000}, {})
    assert summary["fresh_project"] is False
    assert filled["title"] == "旧稿" and filled["genre"] == "武侠" and filled["premise"] == "已有构想"
    assert filled["narrative"]["pov"] == "first" and filled["narrative"]["tone"] == "冷峻"
    assert filled["narrative"]["target_chapters"] == 12 and filled["settings"]["target_words"] == 900
    assert filled["book_rules"].splitlines()[0] == "旧规则"
    assert filled["governance"]["banned_phrases"][0] == "旧词"
    assert [entry for entry in filled["world_entries"] if entry["title"] == "编年"] == [{"title": "编年", "content": "作者写的编年"}]
    assert sum(1 for card in filled["characters"] if card.get("role") == "主角") == 1
    assert filled["chapters"][0]["content"] == "正文已经开始。"
    assert project["premise"] == "已有构想" and len(project["world_entries"]) == 1  # input untouched


def test_selection_controls_what_is_added_and_is_idempotent():
    template = GENRE_TEMPLATES["mystery"]
    selection = {
        "world_entries": ["案件档案"], "characters": ["主角", "对手"],
        "book_rules": [template["book_rules"][0]], "banned_phrases": [],
        "extra_banned_phrases": ["众所周知", "x"],
    }
    filled, summary = apply_template({}, "mystery", {}, selection)
    assert [entry["title"] for entry in filled["world_entries"]] == ["案件档案"]
    assert [card["wizard_archetype"] for card in filled["characters"]] == ["主角", "对手"]
    assert filled["book_rules"] == template["book_rules"][0]
    assert filled["governance"]["banned_phrases"] == ["众所周知"]
    again, second = apply_template(filled, "mystery", {}, selection)
    assert second["added"] == {"book_rules": 0, "banned_phrases": 0, "world_entries": 0, "characters": 0}
    assert len(again["world_entries"]) == 1 and len(again["characters"]) == 2


def test_unknown_template_raises():
    with pytest.raises(KeyError):
        apply_template({}, "nope", {}, {})


def test_volume_instruction_mentions_volumes():
    text = volume_instruction({"target_chapters": 60, "volumes": 3, "target_words": 2000}, "scifi")
    assert "60 章" in text and "3 卷" in text and "科幻" in text


# ------------------------------------------------------------ HTTP

def test_genre_template_endpoints():
    client = TestClient(main.app)
    listing = client.get("/api/genre-templates").json()["templates"]
    assert {"history", "mystery", "romance", "xianxia", "scifi", "realism", "general"} <= {t["id"] for t in listing}
    assert client.get("/api/genre-templates/romance").json()["genre"] == "言情"
    assert client.get("/api/genre-templates/missing").status_code == 404
    response = client.post("/api/genre-templates/apply", json={
        "project": {"title": "未命名故事"}, "template_id": "xianxia",
        "answers": {"volumes": 5, "premise": "凡人修仙"}, "selection": {},
    })
    assert response.status_code == 200
    body = response.json()
    assert body["project"]["genre"] == "玄幻仙侠"
    assert body["project"]["narrative"]["structure_profile"] == "adventure"
    assert "5 卷" in body["planning_instruction"]
    assert client.post("/api/genre-templates/apply", json={"project": {}, "template_id": "nope"}).status_code == 404


# ------------------------------------------------------------ static wiring

def test_new_assets_are_registered_with_the_cache_key():
    html = _read("index.html")
    for asset in ("js/features/wizard.js", "css/wizard.css"):
        assert f"/assets/{asset}?v={APP_VERSION}" in html
    assert html.index("features/wizard.js") < html.index("js/bootstrap.js")
    assert 'id="wizardEmptyState"' in html and 'id="directorChip"' in html


def test_new_project_button_offers_wizard_or_blank():
    bootstrap = _read("js/bootstrap.js")
    assert 'typeof newBookEntryModal==="function"?newBookEntryModal():newProjectModal()' in bootstrap
    wizard = _read("js/features/wizard.js")
    assert "使用新书向导" in wizard and "空白作品" in wizard and "newProjectModal()" in wizard
    for step in ("题材与篇幅", "核心构想", "预览设定"):
        assert step in wizard
    assert "/api/genre-templates/apply" in wizard
    assert "_expected_updated_at=target.updated_at" in wizard
    assert "wizardProjectHasWork(origin)&&!wizardProjectHasWork(project)" in wizard
    assert "newBookWizard({useCurrent:true})" in wizard  # planning-tab empty state
    assert "renderWizardEmptyState" in _read("js/core/project.js")


def test_director_chip_follows_poll_without_opening_modal():
    director = _read("js/features/director.js")
    start = director.index("function setDirectorTask")
    set_task = director[start:director.index("function directorModalShowing")]
    assert "renderDirectorChip()" in set_task
    chip = director[director.index("function renderDirectorChip"):director.index("function renderActiveTask")]
    assert '["queued","running","paused"]' in chip
    assert "directorModalShowing()" in chip
    assert ".focus(" not in chip and "showModal" not in chip and "renderActiveTask(task)" not in chip
    assert '$("#directorChip")?.addEventListener("click"' in director
    assert '$("#modal")?.addEventListener("close",renderDirectorChip)' in director


def test_planning_modal_volume_tabs_keep_all_cards_collectable():
    planning = _read("js/features/planning.js")
    assert "inkforge-planning-tab-" in planning
    assert re.search(r"try\{saved=localStorage\.getItem", planning)
    assert re.search(r"try\{localStorage\.setItem", planning)
    assert 'role="tablist"' in planning and "全书" in planning and "卷${chineseNumeral(i+1)}" in planning
    assert 'placeholder="AI 未填写" data-key="goal"' in planning
    # Hidden panes are only CSS-hidden; collectPlanningModal still walks every card.
    assert '$$(".volume-plan-card").forEach' in planning
    assert 'class="volume-plan-card plan-pane' in planning


def test_chapter_list_score_badge_thresholds_and_optional_usage():
    project_js = _read("js/core/project.js")
    assert "score>=82?\"good\":score>=60?\"fair\":\"poor\"" in project_js
    assert "未审" in project_js and "主要问题" in project_js
    assert 'typeof usage?.chapter!=="function"' in project_js
    assert "window.inkforgeUsage" in project_js
    css = _read("css/wizard.css")
    for tone in ("good", "fair", "poor"):
        assert f".chapter-score.{tone}" in css
    assert ".director-chip" in css and "position:fixed" in css

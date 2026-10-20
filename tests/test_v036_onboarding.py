"""Packaging & first-run onboarding: bundled demo novel, welcome page,
quick-start checklist and the "model not configured" hint."""
from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.api import container
from app.core.config import APP_VERSION
from app.storage.project_store import ProjectStore
from app.story.debate import DEBATE_TEXT_FIELDS
from app.demo import DEMO_FILE, DEMO_ID, fresh_demo_project, is_demo_project, load_demo_project
from app.storage.schema.project import ensure_project_defaults
from app.story.history import check_history

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _prose_length(text: str) -> int:
    return len("".join(str(text).split()))


@pytest.fixture()
def demo() -> dict:
    return load_demo_project()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "inkforge.db", tmp_path / "secrets.db")
    monkeypatch.setattr(container, "store", store)
    return TestClient(main.app), store


# ------------------------------------------------------------ demo content

def test_demo_file_lives_in_the_app_package():
    assert DEMO_FILE == ROOT / "app" / "demo" / "dukoudeng.json"
    assert DEMO_FILE.is_file()


def test_demo_is_a_fixed_point_of_ensure_project_defaults(demo):
    assert ensure_project_defaults(deepcopy(demo)) == demo
    assert is_demo_project(demo) and demo["demo"]["id"] == DEMO_ID


def test_demo_book_level_planning_is_filled(demo):
    assert demo["title"] == "渡口灯" and demo["genre"] == "历史穿越"
    for key in ("premise", "outline", "author_intent", "book_rules", "current_focus"):
        assert len(demo[key]) >= 30, key
    narrative = demo["narrative"]
    assert narrative["central_question"] and narrative["ending_direction"] and narrative["tone"]
    master = demo["planning"]["master"]
    assert all(master[key] for key in ("theme", "central_conflict", "story_engine", "ending_state", "full_outline"))
    volumes = demo["planning"]["volumes"]
    assert [volume["number"] for volume in volumes] == [1, 2]
    assert volumes[0]["chapter_start"] == 1 and volumes[0]["chapter_end"] == 8
    assert volumes[1]["chapter_start"] == 9 and volumes[1]["goal"]


def test_demo_routes_three_written_five_planned_with_one_debate(demo):
    routes = demo["planning"]["volumes"][0]["chapters"]
    assert [route["number"] for route in routes] == list(range(1, 9))
    assert all(route["goal"] and route["conflict"] and route["ending_hook"] for route in routes)
    assert all(route["story_year"] == 1621 and route["story_date"] for route in routes)
    chapters = demo["chapters"]
    assert len(chapters) == 3
    assert [chapter["route_id"] for chapter in chapters] == [route["id"] for route in routes[:3]]
    debates = [route for route in routes if "debate" in route]
    assert len(debates) == 1 and debates[0]["number"] > len(chapters)
    debate = debates[0]["debate"]
    assert all(debate[key] for key in DEBATE_TEXT_FIELDS)
    assert len(debate["opponent_strongest_arguments"]) == 3


def test_demo_chapters_are_finished_restrained_prose(demo):
    cliches = ("深吸一口气", "嘴角勾起", "不容置疑", "眸子", "嘴角微扬", "眼中闪过一丝")
    for chapter in demo["chapters"]:
        length = _prose_length(chapter["content"])
        assert 1200 <= length <= 1800, (chapter["title"], length)
        assert chapter["summary"] and chapter["plan"]["goal"] and chapter["plan"]["scene_beats"]
        assert chapter["story_date"].startswith("崇和元年") and chapter["story_year"] == 1621
        assert not any(phrase in chapter["content"] for phrase in cliches), chapter["title"]
        # Fictional era only: no real reign names or dynasties.
        assert not re.search(r"万历|天启|崇祯|嘉靖|明朝|大明|清朝", chapter["content"])
    for phrase in demo["governance"]["banned_phrases"]:
        assert all(phrase not in chapter["content"] for chapter in demo["chapters"])


def test_demo_chapter_numbers_stay_consistent(demo):
    first, second, third = (chapter["content"] for chapter in demo["chapters"])
    assert "就写二十八人" in first and "写了“二十八”" in second  # ch1 hook → ch2 opening
    assert "一百二十文" in second and "六十文" in third          # the red-string money
    assert "九斤" in third and "一百九十八文" in third            # 9 × 22 = 198
    assert "实四十一" in third and "四十一道" in second           # the night he fell


def test_demo_character_cards_are_complete(demo):
    cards = demo["characters"]
    assert len(cards) == 4
    for card in cards:
        for key in ("knowledge", "secrets", "hard_limits", "voice", "goal", "role"):
            assert card[key], (card["name"], key)
        assert len(card["dialogue_examples"]) >= 2
        assert isinstance(card["born_year"], int) and isinstance(card["enters_year"], int)
    assert {card["importance"] for card in cards} <= {"main", "supporting", "minor"}


def test_demo_world_book_and_preferences(demo):
    entries = demo["world_entries"]
    assert len(entries) == 6 and sum(entry["constant"] for entry in entries) == 2
    assert all(entry["keys"] and entry["content"] and entry["enabled"] for entry in entries)
    preferences = demo["author_preferences"]
    assert preferences and all(item["status"] == "approved" for item in preferences)


def test_demo_audit_results_drive_score_badges(demo):
    first = demo["chapters"][0]["execution"]
    assert isinstance(first["audit_score"], int) and first["audit_score"] >= 82
    assert first["audit_verdict"] == "pass" and first["status"] == "accepted" and first["issues"]
    assert demo["chapters"][2]["execution"]["audit_score"] is None  # one chapter left for the author


def test_demo_passes_the_history_timeline_check(demo):
    for index, chapter in enumerate(demo["chapters"]):
        assert check_history(demo, index, chapter["content"]) == []


def test_fresh_copy_reissues_ids_and_keeps_references(demo):
    copy = fresh_demo_project()
    old_ids = {chapter["id"] for chapter in demo["chapters"]} | {card["id"] for card in demo["characters"]}
    assert not old_ids & ({chapter["id"] for chapter in copy["chapters"]} | {card["id"] for card in copy["characters"]})
    volume = copy["planning"]["volumes"][0]
    route_ids = [route["id"] for route in volume["chapters"]]
    for index, chapter in enumerate(copy["chapters"]):
        assert chapter["volume_id"] == volume["id"]
        assert chapter["route_id"] == route_ids[index] == chapter["route"]["id"]
    assert ensure_project_defaults(deepcopy(copy)) == copy
    assert load_demo_project() == demo  # the bundled file is never mutated


# ------------------------------------------------------------ HTTP

def test_demo_install_refuses_duplicates_unless_forced(client):
    http, store = client
    status = http.get("/api/demo").json()
    assert status == {"available": True, "title": "渡口灯", "installed": False, "project_id": ""}
    response = http.post("/api/demo/install", json={})
    assert response.status_code == 200
    project_id = response.json()["project_id"]
    loaded = store.get(project_id)
    assert loaded["title"] == "渡口灯" and is_demo_project(loaded)
    assert len(loaded["chapters"]) == 3 and len(loaded["planning"]["volumes"][0]["chapters"]) == 8

    again = http.post("/api/demo/install", json={})
    assert again.status_code == 409 and "已经安装" in again.json()["detail"]
    assert len(store.list()) == 1

    # A renamed demo is still recognised by its marker.
    loaded["title"] = "我的渡口灯"
    store.save(project_id, loaded)
    assert http.get("/api/demo").json()["project_id"] == project_id
    assert http.post("/api/demo/install", json={}).status_code == 409

    forced = http.post("/api/demo/install", json={"force": True})
    assert forced.status_code == 200 and forced.json()["duplicate"] is True
    assert forced.json()["project_id"] != project_id and len(store.list()) == 2


def test_demo_install_copies_the_model_connection(client):
    http, store = client
    source = store.create("我的新故事")
    source["settings"].update(base_url="http://127.0.0.1:8080/v1", model="local-model", api_key="sk-demo-secret")
    store.save(source["id"], source)
    response = http.post("/api/demo/install", json={"settings_from": source["id"]})
    assert response.status_code == 200
    installed = store.get(response.json()["project_id"])
    assert installed["settings"]["base_url"] == "http://127.0.0.1:8080/v1"
    assert installed["settings"]["model"] == "local-model"
    assert installed["settings"]["api_key"] == "sk-demo-secret"  # via the secret store
    assert installed["settings"]["target_words"] == 1500
    # Credentials never land in the project payload table.
    assert "sk-demo-secret" not in json.dumps(response.json()["project"]["chapters"], ensure_ascii=False)


def test_demo_install_without_model_keeps_blank_settings(client):
    http, store = client
    blank = store.create("我的新故事")
    response = http.post("/api/demo/install", json={"settings_from": blank["id"]})
    assert store.get(response.json()["project_id"])["settings"]["base_url"] == ""


# ------------------------------------------------------------ static wiring

def test_onboarding_assets_are_registered_with_the_cache_key():
    html = _read("static/index.html")
    for asset in ("js/features/onboarding/onboarding.js", "css/features/onboarding.css"):
        assert f"/assets/{asset}?v={APP_VERSION}" in html
    assert html.index("features/onboarding/onboarding.js") < html.index("js/bootstrap.js")
    assert html.index("features/planning/wizard.js") < html.index("features/onboarding/onboarding.js")
    assert 'id="onboardingChecklist"' in html and 'id="onboardingGuideBtn"' in html and "上手指南" in html
    assert 'id="modelSetupHint"' in html and 'id="modelSetupHintBtn"' in html and "还没连接模型" in html


def test_welcome_offers_demo_wizard_import_and_model_settings():
    source = _read("static/js/features/onboarding/onboarding.js")
    assert 'Inkforge.register("onboarding"' in source
    for text in ("打开演示作品", "新书向导", "导入旧稿", "先配置模型"):
        assert text in source
    assert "/api/demo/install" in source and "importManuscriptModal()" in source and "settingsModal()" in source
    assert "newBookWizard({useCurrent:true})" in source
    for step in ("配置模型", "AI 分层规划", "写第一章", "审校与精修"):
        assert step in source
    # localStorage is optional (private mode, blocked storage).
    assert re.search(r"try\{return localStorage\.getItem", source)
    assert re.search(r"try\{value===null\?localStorage\.removeItem\(key\):localStorage\.setItem", source)


def test_startup_and_model_hooks_are_optional_calls():
    assert 'typeof maybeShowWelcome==="function"' in _read("static/js/bootstrap.js")
    assert 'typeof renderOnboardingChecklist==="function"' in _read("static/js/core/project.js")
    generation = _read("static/js/features/writing/generation.js")
    for state in ("missing", "ready", "offline"):
        assert f'renderModelSetupHint("{state}")' in generation


def test_onboarding_css_uses_theme_tokens_only():
    css = _read("static/css/features/onboarding.css")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", css)
    assert ".onboarding-checklist" in css and ".model-setup-hint" in css


# ------------------------------------------------------------ packaging

def test_demo_ships_in_every_package():
    assert '--add-data "app\\demo\\dukoudeng.json;app\\demo"' in _read("scripts/build_windows.ps1")
    launcher = _read("scripts/build_launcher.ps1")
    assert 'Copy-Item -LiteralPath "app"' in launcher and "app\\demo\\dukoudeng.json" in launcher
    assert "scripts\\smoke_demo.py" in launcher
    # The Inno installer takes the whole PyInstaller folder recursively.
    assert "recursesubdirs" in _read("packaging/inkforge.iss")
    assert "app/demo/dukoudeng.json" in _read("scripts/check_project.py")
    assert (ROOT / "scripts" / "smoke_demo.py").is_file()


def test_unaudited_chapter_badge_is_not_a_zero_score():
    # ensure_project_defaults stores audit_score=None; +null must not read as 0 分.
    app_js = _read("static/js/core/state.js")
    assert 'rawScore!==null&&rawScore!==undefined&&rawScore!==""' in app_js

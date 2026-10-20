"""v0.37.1: book-agnostic validators and the 开写前设定预检 (pre-writing preflight).

Every fixture here is an invented world (a canal city in invented CE years);
nothing depends on any particular book, dynasty or name list.
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.director.service as director_runtime
import app.main as main
from app.api import container
from app.planning.preflight import (
    PREFLIGHT_SCHEMA,
    deterministic_conflicts,
    run_preflight,
)
from app.planning.route_validation import (
    route_era_conflicts,
    validate_director_assigned_turn,
    validate_director_route_language,
)
from app.planning.validation import (
    character_name_bigrams,
    historical_asset_conflicts,
    validate_asset_authority,
)
from app.storage.project_store import ProjectStore
from app.storage.schema.project import ensure_project_defaults
from app.story.history import extract_years

ROOT = Path(__file__).resolve().parents[1]

# Names, places and slogans of earlier books.  None may be hard-coded in app/.
BOOK_SPECIFIC_TERMS = (
    "沈衡", "秦策", "赵高", "蒙恬", "蒙毅", "王翦", "王贲", "扶苏", "胡亥", "吕不韦",
    "嫪毐", "昌平君", "尉缭", "姚贾", "顿弱", "嬴政", "李斯", "周砚", "天下有数",
    "咸阳", "秦律", "秦统一", "秦王政", "统一六国",
)


def run(coro):
    return asyncio.run(coro)


def _route(number, goal, *, year=None, **extra):
    route = {
        "id": f"r{number}", "number": number, "title": f"第{number}章", "goal": goal,
        "conflict": "码头行会拒绝放行", "turning_point": "账房换了新锁",
        "ending_hook": "船灯在雾里熄灭", "must_keep": [], "must_avoid": [],
    }
    if year is not None:
        route["story_year"] = year
    route.update(extra)
    return route


def _world(**overrides):
    """An invented canal city reproducing the three generic failure classes."""
    project = {
        "id": "",
        "title": "雾港",
        "genre": "历史架空",
        "premise": "故事开始于1203年春，账房学徒柯迟在雾港醒来。",
        "current_focus": "",
        "book_rules": "",
        "characters": [
            {"name": "柯迟", "role": "主角", "hard_limits": "不识字，无法书写任何文书", "active": True},
            {"name": "闻笙", "role": "盟友", "enters_year": 1206, "active": False},
            {"name": "老韩", "role": "船主", "died_year": 1200, "active": True},
            {"name": "叶桥", "role": "行会主", "active": True},
        ],
        "world_entries": [
            {
                "title": "环城运河", "keys": ["环城运河"], "constant": True,
                "content": "环城运河于1204年开凿完成，此后成为货运主道。",
            },
            {"title": "雾港", "keys": ["雾港"], "constant": True, "content": "港口常年起雾。"},
        ],
        "governance": {"era_terms": [{"term": "银票", "from_year": 1210, "to_year": None}]},
        "planning": {
            "volumes": [
                {
                    "id": "v1", "number": 1, "title": "雾港初夜", "chapter_start": 1, "chapter_end": 3,
                    "chapters": [
                        _route(1, "柯迟沿环城运河押送第一船货"),
                        _route(2, "柯迟找闻笙借船", year=1203),
                        _route(3, "叶桥用银票收买柯迟", year=1203),
                    ],
                }
            ]
        },
    }
    project.update(overrides)
    return ensure_project_defaults(project)


# ------------------------------------------------------------ task 1: no book-specific rules


def test_app_has_no_hard_coded_names_from_earlier_books():
    offenders = []
    for path in (ROOT / "app").rglob("*.py"):
        relative = path.relative_to(ROOT).as_posix()
        if relative.startswith("app/demo/"):
            continue
        text = path.read_text(encoding="utf-8")
        offenders.extend(f"{relative}: {term}" for term in BOOK_SPECIFIC_TERMS if term in text)
    assert not offenders, offenders


def test_extract_years_handles_bce_ce_and_plain_years_but_not_durations():
    assert extract_years("前221年冬，公元前220年，公元25年，1203年春，过了十年，30年后") == [-221, -220, 25, 1203]


def test_route_language_blocks_dead_and_not_yet_entered_characters_from_project_data():
    project = _world()
    with pytest.raises(ValueError, match="闻笙"):
        validate_director_route_language(project, _route(2, "柯迟找闻笙借船", year=1203))
    with pytest.raises(ValueError, match="老韩"):
        validate_director_route_language(project, _route(2, "柯迟去见老韩", year=1203))
    # The same names are fine once the story reaches their years …
    validate_director_route_language(project, _route(9, "柯迟找闻笙借船", year=1207))
    # … and nothing is checked without a story year (no built-in knowledge).
    validate_director_route_language(project, _route(9, "柯迟找闻笙借船"))


def test_route_language_uses_era_terms_and_banned_phrases_not_built_in_eras():
    project = _world()
    with pytest.raises(ValueError, match="银票"):
        validate_director_route_language(project, _route(3, "叶桥用银票收买柯迟", year=1203))
    validate_director_route_language(project, _route(3, "叶桥用银票收买柯迟", year=1212))
    project["governance"]["banned_phrases"] = ["命运齿轮"]
    with pytest.raises(ValueError, match="命运齿轮"):
        validate_director_route_language(project, _route(4, "命运齿轮开始转动"))
    # A project without era data is never judged by another book's chronology.
    plain = _world(governance={}, characters=[])
    validate_director_route_language(plain, _route(1, "皇帝召见柯迟", year=-230))


def test_route_modern_jargon_follows_the_era_policy():
    project = _world()
    route = _route(5, "柯迟用算法核对货单")
    project["governance"]["pov_modern_allowed"] = False
    with pytest.raises(ValueError, match="算法"):
        validate_director_route_language(project, route)
    project["governance"]["pov_modern_allowed"] = True
    validate_director_route_language(project, route)  # a modern mind may think in a term or two
    with pytest.raises(ValueError, match="现代技术词"):
        validate_director_route_language(project, _route(5, "柯迟用算法、数据库和模型核对货单"))
    project["governance"].update(pov_modern_allowed=False, term_allowlist=["算法"])
    validate_director_route_language(project, route)
    # Non-period settings are not policed for modern vocabulary at all.
    validate_director_route_language(_world(genre="都市"), _route(5, "柯迟用算法、数据库和模型核对货单"))


def test_asset_authority_is_driven_by_era_terms_and_story_year():
    project = _world()
    project["governance"]["era_terms"] = [{"term": "皇帝", "from_year": -221, "to_year": None}]
    payload = {"title": "宫中旧制", "content": "皇帝亲临渡口"}
    assert historical_asset_conflicts(payload, project) == []  # no story year recorded yet
    project["chapters"] = [{"title": "一", "story_year": -230}]
    assert historical_asset_conflicts(payload, project) == ["皇帝"]
    with pytest.raises(ValueError, match="皇帝"):
        validate_asset_authority(payload, project)
    project["chapters"] = [{"title": "一", "story_year": -200}]
    validate_asset_authority(payload, project)
    validate_asset_authority(payload, None)


def test_name_stop_words_come_from_the_character_list():
    project = _world()
    names = character_name_bigrams(project)
    assert {"柯迟", "闻笙", "叶桥"} <= names
    # Sharing only the protagonist's name does not count as carrying the turn.
    route = _route(6, "柯迟回到账房", conflict="柯迟被拦", turning_point="柯迟沉默")
    with pytest.raises(ValueError, match="指定卷级转折"):
        validate_director_assigned_turn(route, "柯迟烧毁运河闸门图纸", names)


# ------------------------------------------------------------ task 2: preflight


def test_preflight_reproduces_the_three_generic_failure_classes():
    project = _world()
    conflicts = deterministic_conflicts(project)
    by_kind = {item["kind"]: item for item in conflicts}

    # (a) the premise starts the story before an event the first routes rely on.
    chronology = next(item for item in conflicts if item["where"].startswith("设定/世界书"))
    assert chronology["severity"] == "medium" and chronology["fix_target"] == "setting"
    assert "环城运河" in chronology["detail"] and "1204年" in chronology["detail"] and "第1章" in chronology["detail"]

    # (c) a route uses a character before the card's entrance year.
    entrance = by_kind["character_entrance"]
    assert entrance["severity"] == "high" and entrance["where"] == "第2章路线" and "闻笙" in entrance["detail"]

    # era terms from the project's own governance data
    assert any(item["where"] == "第3章路线" and "银票" in item["detail"] for item in conflicts)
    assert all(item["source"] == "rule" for item in conflicts)


def test_opening_routes_without_years_use_the_premise_start_year():
    project = _world()
    first = project["planning"]["volumes"][0]["chapters"][0]
    first["goal"] = "柯迟找闻笙借船"
    hits = [item for item in deterministic_conflicts(project) if item["where"] == "第1章路线" and "闻笙" in item["detail"]]
    assert hits and hits[0]["kind"] == "character_entrance" and hits[0]["severity"] == "high"
    # Without a premise year a year-less route is not judged.
    project["premise"] = "账房学徒柯迟在雾港醒来。"
    assert not [item for item in deterministic_conflicts(project) if item["where"] == "第1章路线" and "闻笙" in item["detail"]]


def test_preflight_chronology_is_conservative():
    # Fix the premise: the story now starts after the canal opens.
    project = _world(premise="故事开始于1205年春，账房学徒柯迟在雾港醒来。")
    assert not [item for item in deterministic_conflicts(project) if item["where"].startswith("设定/世界书")]
    # An entry that also records an earlier year describes something already in place.
    project = _world()
    project["world_entries"][0]["content"] = "环城运河于1190年开挖，1204年全线贯通。"
    assert not [item for item in deterministic_conflicts(project) if item["where"].startswith("设定/世界书")]
    # Without any years nothing is reported.
    project = _world(premise="账房学徒柯迟在雾港醒来。")
    assert not [item for item in deterministic_conflicts(project) if item["where"].startswith("设定/世界书")]


def test_inactive_character_without_years_is_only_a_low_reminder():
    project = _world()
    project["characters"][1]["enters_year"] = None
    project["planning"]["volumes"][0]["chapters"][1].pop("story_year", None)
    project = ensure_project_defaults(project)
    reminders = [item for item in deterministic_conflicts(project) if item["where"] == "人物卡：闻笙"]
    assert reminders and reminders[0]["severity"] == "low" and reminders[0]["fix_target"] == "character"


def test_preflight_merges_a_mocked_model_ability_conflict():
    project = _world()
    seen = {}

    async def fake_completion(settings, messages, **kwargs):
        seen["user"] = messages[-1]["content"]
        seen["schema"] = kwargs.get("schema")
        return {"conflicts": [{
            "severity": "high", "kind": "ability", "where": "第1章路线",
            "detail": "路线要求柯迟亲笔写契书，但人物卡写明他不识字。",
            "suggestion": "改为由他口述、他人代笔。", "fix_target": "route",
        }, {"severity": "low", "kind": "other", "where": "设定", "detail": "", "suggestion": "", "fix_target": "route"}]}, []

    result = run(run_preflight(project, "v1", completion=fake_completion))
    assert seen["schema"] is PREFLIGHT_SCHEMA
    assert "不识字" in seen["user"] and "柯迟沿环城运河" in seen["user"] and "1204年" in seen["user"]
    ability = [item for item in result["conflicts"] if item["kind"] == "ability"]
    assert ability and ability[0]["source"] == "model"
    assert not result["ok"] and result["high_count"] >= 3 and result["volume_id"] == "v1"
    assert [item["severity"] for item in result["conflicts"]] == sorted(
        (item["severity"] for item in result["conflicts"]), key=["high", "medium", "low"].index
    )


def test_preflight_model_failure_never_blocks_the_local_checks():
    async def broken(*_args, **_kwargs):
        raise TimeoutError("模型超时")

    result = run(run_preflight(_world(), None, completion=broken))
    assert result["high_count"] >= 2 and any("模型预检未完成" in item for item in result["warnings"])
    clean = _world(premise="故事开始于1205年。", characters=[], governance={})
    result = run(run_preflight(clean, None, completion=broken))
    assert result["ok"] and result["conflicts"] == []


# ------------------------------------------------------------ endpoint + director gate


@pytest.fixture()
def store(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "preflight.db", tmp_path / "secrets.db")
    monkeypatch.setattr(container, "store", store)
    monkeypatch.setattr(container, "_launch_director", lambda _task_id: None)
    return store


def _saved_world(store, **overrides):
    project = store.create("雾港")
    world = _world(**overrides)
    for key in ("premise", "genre", "characters", "world_entries", "governance", "planning"):
        project[key] = world[key]
    return store.save(project["id"], project, reason="fixture")


def test_preflight_endpoint_runs_the_mocked_model_and_stores_the_result(store, monkeypatch):
    project = _saved_world(store)
    calls = []

    async def fake_completion(settings, messages, **kwargs):
        calls.append(kwargs.get("workload"))
        return {"conflicts": [{
            "severity": "medium", "kind": "knowledge", "where": "第3章路线",
            "detail": "叶桥此时不可能知道账房换锁。", "suggestion": "改为他事后得知。", "fix_target": "route",
        }]}, []

    monkeypatch.setattr(container, "structured_completion", fake_completion)
    client = TestClient(main.app)
    response = client.post(f"/api/projects/{project['id']}/preflight", json={"volume_id": "v1"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert calls == ["planning"]
    assert data["preflight"]["high_count"] >= 2 and any(item["kind"] == "knowledge" for item in data["preflight"]["conflicts"])
    stored = store.get(project["id"])["planning"]["preflight"]
    assert stored["checked_at"] == data["preflight"]["checked_at"] and data["updated_at"] == store.get(project["id"])["updated_at"]

    local = client.post(f"/api/projects/{project['id']}/preflight", json={"local_only": True})
    assert local.status_code == 200 and calls == ["planning"]
    assert client.post(f"/api/projects/{project['id']}/preflight", json={"volume_id": "nope"}).status_code == 404
    assert client.post("/api/projects/missing/preflight", json={}).status_code == 404


def test_director_does_not_start_with_high_preflight_conflicts_until_ignored(store, monkeypatch):
    project = _saved_world(store)
    model_calls = []

    async def fake_structured(settings, messages, **kwargs):
        model_calls.append(kwargs.get("schema"))
        return {"conflicts": []}, []

    monkeypatch.setattr(director_runtime, "run_structured_completion", fake_structured)
    client = TestClient(main.app)
    url = f"/api/director/projects/{project['id']}/write-planned"
    blocked = client.post(url, json={})
    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert detail["code"] == "preflight_conflicts" and detail["preflight"]["high_count"] >= 2
    assert model_calls == [PREFLIGHT_SCHEMA]
    assert store.latest_director_task(project["id"]) is None
    saved = store.get(project["id"])
    assert saved["planning"]["preflight"]["high_count"] == detail["preflight"]["high_count"]
    assert detail["updated_at"] == saved["updated_at"]

    started = client.post(url, json={"ignore_preflight": True})
    assert started.status_code == 200, started.text
    assert started.json()["task"]["task_type"] == "planned_production"
    assert model_calls == [PREFLIGHT_SCHEMA]  # ignoring skips the check entirely


def test_director_starts_when_preflight_is_clean_even_if_the_model_fails(store, monkeypatch):
    project = _saved_world(store, premise="故事开始于1207年。", governance={}, characters=[])

    async def broken(*_args, **_kwargs):
        raise RuntimeError("模型不可用")

    monkeypatch.setattr(director_runtime, "run_structured_completion", broken)
    response = TestClient(main.app).post(f"/api/director/projects/{project['id']}/write-planned", json={})
    assert response.status_code == 200, response.text
    preflight = response.json()["project"]["planning"]["preflight"]
    assert preflight["ok"] and any("模型预检未完成" in item for item in preflight["warnings"])


def test_route_era_conflicts_are_empty_for_projects_without_data():
    plain = ensure_project_defaults({"title": "x", "characters": [{"name": "某甲"}]})
    assert route_era_conflicts(plain, _route(1, "某甲出门", year=1000)) == []


def test_frontend_wires_the_preflight_ui():
    js = (ROOT / "static/js/features/planning/preflight.js").read_text(encoding="utf-8")
    planning = (ROOT / "static/js/features/planning/planning.js").read_text(encoding="utf-8")
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    assert "/preflight" in js and "仍然开始" in js and "先去修改" in js and 'Inkforge.register("preflight"' in js
    assert "ignore_preflight" in planning and "handleWritePlannedBlocked" in planning and "runPreflight" in planning
    assert re.search(r'/assets/js/features/planning/preflight\.js\?v=', html)
    assert re.search(r'/assets/css/features/preflight\.css\?v=', html)


def test_rule_boundaries_and_side_years_are_not_chronology_conflicts():
    from app.planning.preflight import chronology_conflicts, premise_start_year

    project = _world(
        premise="王在1202年即位、次年改元；主角穿越到1203年的雾港。",
        world_entries=[
            {"title": "称谓", "keys": ["称谓", "宰辅"], "constant": True,
             "content": "1220年之前国君称王，宰辅称相；1220年起改称帝。"},
            {"title": "环城运河", "keys": ["环城运河"], "constant": True,
             "content": "环城运河于1204年开凿完成。"},
        ],
    )
    project["chapters"] = [dict(project["chapters"][0], story_year=None)]
    assert premise_start_year(project) == 1203
    volume = project["planning"]["volumes"][0]
    volume["chapters"][0]["goal"] = "柯迟拜见宰辅，又沿环城运河押货"
    conflicts = chronology_conflicts(project, volume)
    assert len(conflicts) == 1 and "环城运河" in conflicts[0]["detail"]
    project["chapters"][0]["story_year"] = 1205
    assert premise_start_year(project) == 1205

from fastapi.testclient import TestClient

from app.db import default_project
from app.main import (
    API_SCHEMA_VERSION,
    app,
    parse_json_response,
    require_fields,
    structured_completion,
    validate_route_batch,
)


client = TestClient(app)


def test_health_exposes_frontend_compatibility_version():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["api_schema_version"] == API_SCHEMA_VERSION


def test_index_disables_stale_html_cache():
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_prompt_preview_always_returns_collection_fields():
    project = default_project("preview-project", "预览测试", "now")
    project["memory"]["facts"] = ["旧格式事实也必须兼容"]
    project["world_entries"] = [
        {"title": "旧格式世界书", "keys": "白塔，钟室", "content": "只用于兼容测试"}
    ]
    chapter = project["chapters"][0]
    response = client.post(
        "/api/prompt/preview",
        json={
            "project": project,
            "chapter_id": chapter["id"],
            "mode": "continue",
            "instruction": "",
            "selection": "",
            "target_words": 800,
        },
    )
    assert response.status_code == 200
    data = response.json()
    for key in (
        "sections",
        "activated_lore",
        "retrieved_memories",
        "budget_warnings",
    ):
        assert isinstance(data[key], list)
    assert isinstance(data["estimated_tokens"], int)
    assert data["sections"]


def test_json_parser_ignores_thinking_and_trailing_comma():
    parsed = parse_json_response(
        '<think>先规划，但这里有一个无关的 {符号}</think>\n'
        '```json\n{"theme":"秩序与正义","volumes":[{"title":"入秦卷",}],}\n```'
    )
    assert parsed["theme"] == "秩序与正义"
    assert parsed["volumes"][0]["title"] == "入秦卷"


def test_json_parser_reports_truncated_output_clearly():
    try:
        parse_json_response('{"theme":"未完成","volumes":[')
    except ValueError as exc:
        assert "截断" in str(exc)
    else:
        raise AssertionError("truncated JSON should fail")


def test_json_parser_repairs_literal_newline_inside_string():
    parsed = parse_json_response('{"goal":"先核账\n再公开复核","must_keep":[]}')
    assert parsed["goal"] == "先核账\n再公开复核"


def test_structured_completion_retries_malformed_output(monkeypatch):
    calls = []
    message_sets = []

    async def fake_chat(settings, messages, **kwargs):
        calls.append(kwargs)
        message_sets.append(messages)
        if len(calls) == 1:
            return '{"goal":"未闭合"'
        return '{"goal":"完成复核","conflict":"仓吏拒绝配合"}'

    monkeypatch.setattr("app.main.chat_once", fake_chat)
    result, warnings = __import__("asyncio").run(
        structured_completion(
            {},
            [{"role": "system", "content": "只返回JSON"}],
            max_tokens=500,
            timeout_seconds=10,
            temperature=0.3,
            validate=require_fields("goal", "conflict"),
        )
    )
    assert result["goal"] == "完成复核"
    assert len(calls) == 2
    assert calls[1]["max_tokens"] > calls[0]["max_tokens"]
    assert message_sets[1][-1]["role"] == "user"
    assert "自动重试" in warnings[0]


def test_route_quality_validator_rejects_repeated_chapter_goals():
    repeated = {
        "chapters": [
            {
                "number": number,
                "title": f"第{number}次核账",
                "goal": "沈砚完成粮册复核并获得秦王政的初步信任",
                "conflict": "仓吏阻止",
                "turning_point": "发现旧账",
                "ending_hook": "等待复核",
                "must_keep": [],
                "must_avoid": [],
            }
            for number in range(1, 5)
        ]
    }
    try:
        validate_route_batch(repeated, [1, 2, 3, 4], [])
    except ValueError as exc:
        assert "重复" in str(exc)
    else:
        raise AssertionError("repeated chapter goals must be rejected")


def test_route_quality_validator_rejects_abstract_template_titles():
    route = {
        "chapters": [
            {
                "number": 1,
                "title": "初次选择",
                "goal": "沈砚当众指出三套粮册的计量口径冲突",
                "conflict": "仓吏要求他保持沉默",
                "turning_point": "嬴政命人封存原账",
                "ending_hook": "第二日公开复核",
                "must_keep": [],
                "must_avoid": [],
            }
        ]
    }
    try:
        validate_route_batch(route, [1], [])
    except ValueError as exc:
        assert "抽象节拍模板" in str(exc)
    else:
        raise AssertionError("abstract template title must be rejected")

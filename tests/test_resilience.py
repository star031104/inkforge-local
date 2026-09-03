import asyncio
import json

from fastapi.testclient import TestClient

from app.db import default_project, ensure_project_defaults
from app.main import app
from app.planning import (
    apply_volume_routes,
    fallback_volume_routes,
    normalize_master_plan,
)


client = TestClient(app)


def resilient_project():
    project = default_project("resilient", "诸子山河", "now")
    project["narrative"]["target_chapters"] = 3
    project["characters"] = [
        {"name": "沈砚", "role": "现代制度分析者", "description": "", "goal": ""}
    ]
    project["outline"] = (
        "第一卷：逐客之门（第1—3章）\n"
        "沈砚从无籍者变成能够试行粮册改革的低级计吏。"
    )
    project["planning"] = normalize_master_plan(
        {
            "theme": "秩序与正义",
            "central_conflict": "现代程序观与秦国结果主义冲突",
            "volumes": [
                {
                    "title": "逐客之门",
                    "chapter_count": 3,
                    "goal": "沈砚取得有限议政资格",
                    "ending_state": "获得粮册试行机会",
                }
            ],
        },
        3,
    )
    return ensure_project_defaults(project)


def test_volume_fallback_is_complete_and_uses_existing_chapter_title():
    project = resilient_project()
    project["chapters"][0]["title"] = "无籍之人"
    volume = project["planning"]["volumes"][0]
    result = fallback_volume_routes(project, volume, "本地模型响应超时")
    assert result["fallback"] is True
    assert result["complete"] is True
    assert len(result["chapters"]) == 3
    assert result["chapters"][0]["title"] == "无籍之人"
    assert all(item["goal"] for item in result["chapters"])


def test_all_structured_endpoints_degrade_safely_on_timeout(monkeypatch):
    async def timeout_chat(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr("app.main.chat_once", timeout_chat)
    project = resilient_project()
    volume = project["planning"]["volumes"][0]

    volume_response = client.post(
        "/api/planning/volume",
        json={"project": project, "volume_id": volume["id"], "instruction": ""},
    )
    assert volume_response.status_code == 200
    volume_result = volume_response.json()
    assert volume_result["fallback"] is True
    assert volume_result["complete"] is False
    assert volume_result["chapters"] == []
    assert "旧路线未被替换" in volume_result["warnings"][0]
    volume["chapters"] = fallback_volume_routes(
        project, volume, "测试后续结构化端点"
    )["chapters"]
    project = ensure_project_defaults(apply_volume_routes(project, volume["id"]))

    chapter = project["chapters"][0]
    plan_response = client.post(
        "/api/chapter/plan",
        json={"project": project, "chapter_id": chapter["id"], "instruction": ""},
    )
    assert plan_response.status_code == 200
    assert plan_response.json()["fallback"] is True
    assert plan_response.json()["goal"]

    sample = (
        "风从渭水方向吹来，贴着地面卷动黄尘。沈砚没有立刻起身。"
        "远处城墙压在尘雾后面，路旁的人只看了他一眼便继续赶路。"
    ) * 3
    style_response = client.post(
        "/api/style/analyze", json={"settings": project["settings"], "sample": sample}
    )
    assert style_response.status_code == 200
    assert style_response.json()["fallback"] is True
    assert style_response.json()["profile"]

    chapter["content"] = (
        "沈砚核对三套粮册，发现入仓、转运与实存使用了不同口径。"
        "他没有指控仓吏贪墨，而是要求先复核记录。仓吏拒绝交出旧牍，"
        "双方约定次日在仓门前公开清点。"
    ) * 3
    memory_response = client.post(
        "/api/chapter/memory",
        json={"project": project, "chapter_id": chapter["id"], "instruction": ""},
    )
    assert memory_response.status_code == 200
    assert memory_response.json()["fallback"] is True
    assert memory_response.json()["summary"]

    audit_response = client.post(
        "/api/chapter/audit",
        json={
            "project": project,
            "chapter_id": chapter["id"],
            "draft": chapter["content"],
            "instruction": "",
        },
    )
    assert audit_response.status_code == 200
    assert audit_response.json()["fallback"] is True
    assert audit_response.json()["verdict"] == "partial"
    assert audit_response.json()["audit_scope"] == "local_only"
    assert "local_checks" in audit_response.json()

    ideas_response = client.post(
        "/api/ideas",
        json={"project": project, "kind": "next", "instruction": ""},
    )
    assert ideas_response.status_code == 200
    assert ideas_response.json()["fallback"] is True
    assert len(ideas_response.json()["options"]) == 3


def test_chapter_plan_recovers_from_truncated_first_response(monkeypatch):
    calls = 0

    async def truncated_then_complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return '{"goal":"沈砚完成粮册复核","conflict":"仓吏拒绝'
        return """{
          "goal":"沈砚完成三册口径复核并取得试行机会",
          "conflict":"仓吏拒绝公开旧账，沈砚必须在自保与追查间选择",
          "must_keep":["沈砚不确定精确年份","改革先从核账开始"],
          "must_avoid":["秦人使用现代术语","提前引出秦王"],
          "turning_point":"旧牍上的损耗数字互相矛盾",
          "ending_hook":"仓吏限他次日公开验算"
        }"""

    monkeypatch.setattr("app.main.chat_once", truncated_then_complete)
    project = resilient_project()
    chapter = project["chapters"][0]
    response = client.post(
        "/api/chapter/plan",
        json={"project": project, "chapter_id": chapter["id"], "instruction": ""},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["fallback"] is False
    assert calls == 2
    assert "自动重试" in result["warnings"][0]
    assert result["ending_hook"]


def test_chapter_plan_cannot_override_reviewed_route(monkeypatch):
    async def conflicting_plan(*args, **kwargs):
        return json.dumps(
            {
                "goal": "错误地立即获得永久调粮权",
                "conflict": "错误冲突",
                "must_keep": ["错误事实"],
                "must_avoid": ["错误禁令"],
                "turning_point": "错误转折",
                "ending_hook": "错误结尾",
                "scene_beats": [["动作", "发现", "结果"]],
                "exit_state": "错误退出状态",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.main.chat_once", conflicting_plan)
    project = resilient_project()
    chapter = project["chapters"][0]
    chapter["route"] = {
        "goal": "只核验三份简牍",
        "conflict": "三份真简牍不能同时为真",
        "must_keep": ["三份封泥都真实"],
        "must_avoid": ["不得立即取得调粮权"],
        "turning_point": "发现今日重压细痕",
        "ending_hook": "日落前获准开仓一次",
    }
    response = client.post(
        "/api/chapter/plan",
        json={"project": project, "chapter_id": chapter["id"], "instruction": ""},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["goal"] == chapter["route"]["goal"]
    assert result["conflict"] == chapter["route"]["conflict"]
    assert result["must_keep"] == chapter["route"]["must_keep"]
    assert result["must_avoid"] == chapter["route"]["must_avoid"]
    assert result["ending_hook"] == chapter["route"]["ending_hook"]
    assert result["exit_state"] == chapter["route"]["ending_hook"]
    assert all(not item.startswith("[") for item in result["scene_beats"])


def test_ai_audit_cannot_overrule_local_high_or_medium_issue(monkeypatch):
    async def ai_passes(*args, **kwargs):
        return json.dumps(
            {
                "score": 100,
                "verdict": "pass",
                "issues": [],
                "strengths": ["衔接自然"],
                "revision_brief": "",
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr("app.main.chat_once", ai_passes)
    project = resilient_project()
    chapter = project["chapters"][0]
    short_draft = "沈砚核对三套粮册，发现损耗口径不同。" * 8
    response = client.post(
        "/api/chapter/audit",
        json={
            "project": project,
            "chapter_id": chapter["id"],
            "instruction": "",
            "draft": short_draft,
        },
    )
    assert response.status_code == 200
    result = response.json()
    assert result.get("fallback") is False, result
    assert result["ai_score"] == 100
    assert result["verdict"] == "revise"
    assert result["score"] < 100

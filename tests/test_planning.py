import json

import httpx
import pytest

from app.db import default_project, ensure_project_defaults
from app.planning import (
    apply_volume_routes,
    fallback_master_plan,
    normalize_master_plan,
    normalize_volume_routes,
    render_planning_context,
)
from app.prompts import build_prompt
from fastapi.testclient import TestClient

from app.main import app, validate_route_batch


client = TestClient(app)


def test_master_plan_covers_target_chapters_without_gaps():
    result = normalize_master_plan(
        {
            "theme": "秩序是否等于正义",
            "reader_promise": "思想交锋改变国家选择",
            "central_conflict": "林砚与嬴政对统一代价的分歧",
            "story_engine": "每次改革都制造新的受益者与反对者",
            "ending_state": "统一完成但二人道路分开",
            "volumes": [
                {"title": "入秦", "chapter_count": 6},
                {"title": "变法", "chapter_count": 12},
                {"title": "一统", "chapter_count": 12},
            ],
        },
        30,
    )
    volumes = result["volumes"]
    assert volumes[0]["chapter_start"] == 1
    assert volumes[-1]["chapter_end"] == 30
    assert all(
        left["chapter_end"] + 1 == right["chapter_start"]
        for left, right in zip(volumes, volumes[1:])
    )


def test_volume_routes_require_exact_count_and_keep_global_numbers():
    volume = {"id": "v1", "chapter_start": 7, "chapter_end": 9}
    routes, warnings = normalize_volume_routes(
        {
            "chapters": [
                {"title": "问策", "goal": "获得廷议资格"},
                {"title": "廷辩", "goal": "迫使双方公开代价"},
                {"title": "余波", "goal": "改革形成第一批反对者"},
            ]
        },
        volume,
    )
    assert [item["number"] for item in routes] == [7, 8, 9]
    assert warnings == []


def test_route_quality_gate_checks_conflicts_and_premature_volume_outcomes():
    base = {
        "must_keep": [],
        "must_avoid": [],
    }
    duplicate_conflicts = {
        "chapters": [
            {
                **base,
                "number": 1,
                "title": "仓门点验",
                "goal": "核对第一批粮袋",
                "conflict": "仓吏拒绝交出原始木牍并要求沈砚承担全部罪责",
                "turning_point": "一枚旧签押证明转运记录缺失",
                "ending_hook": "次日将公开称量",
            },
            {
                **base,
                "number": 2,
                "title": "夜取旧牍",
                "goal": "找到缺失的转运记录",
                "conflict": "仓吏拒绝交出原始木牍并要求沈砚承担全部罪责",
                "turning_point": "守门小吏承认曾替换封泥",
                "ending_hook": "主事官连夜召见",
            },
        ]
    }
    with pytest.raises(ValueError, match="核心冲突高度重复"):
        validate_route_batch(duplicate_conflicts, [1, 2], [])

    premature = {
        "chapters": [
            {
                **base,
                "number": 1,
                "title": "宫门回音",
                "goal": "秦王政否决极端逐客令，沈砚获得粮册复核权",
                "conflict": "安全疑虑与用人需求相冲突",
                "turning_point": "沈砚正式获准以试办史官身份留秦",
                "ending_hook": "新粮册进入全面试行",
            }
        ]
    }
    with pytest.raises(ValueError, match="提前兑现"):
        validate_route_batch(
            premature,
            [1],
            [],
            [
                "废除极端逐客令，确立沈砚介入行政的合法性",
                "逐客令废除，沈砚掌握粮册复核权",
            ],
        )


def test_apply_routes_does_not_overwrite_existing_prose_or_custom_title():
    project = default_project("p1", "诸子山河", "now")
    project["chapters"][0]["title"] = "雾入咸阳"
    project["chapters"][0]["content"] = "已有正文必须保留。"
    project["planning"] = normalize_master_plan(
        {"volumes": [{"title": "入秦卷", "chapter_count": 3}]}, 3
    )
    volume = project["planning"]["volumes"][0]
    volume["chapters"], _ = normalize_volume_routes(
        {
            "chapters": [
                {"title": "AI新标题", "goal": "入城"},
                {"title": "市井问法", "goal": "发现矛盾"},
                {"title": "廷前投书", "goal": "获得机会"},
            ]
        },
        volume,
    )
    updated = apply_volume_routes(project, volume["id"])
    assert updated["chapters"][0]["title"] == "雾入咸阳"
    assert updated["chapters"][0]["content"] == "已有正文必须保留。"
    assert updated["chapters"][1]["title"] == "市井问法"
    assert updated["chapters"][1]["route"]["number"] == 2


def test_prompt_receives_master_volume_and_route_with_truth_precedence():
    project = default_project("p1", "诸子山河", "now")
    project["planning"] = normalize_master_plan(
        {
            "theme": "秩序是否等于正义",
            "central_conflict": "林砚与嬴政争论统一的代价",
            "volumes": [{"title": "入秦卷", "chapter_count": 1, "goal": "取得信任"}],
        },
        1,
    )
    volume = project["planning"]["volumes"][0]
    volume["chapters"], _ = normalize_volume_routes(
        {"chapters": [{"title": "雾入咸阳", "goal": "林砚面见嬴政"}]},
        volume,
    )
    project = ensure_project_defaults(apply_volume_routes(project, volume["id"]))
    context = render_planning_context(project, 0)
    assert "全书总导演板" in context
    assert "当前分卷战略" in context
    assert "当前章路线卡" in context
    assert "已接受正文" in context
    built = build_prompt(
        project,
        {
            "chapter_id": project["chapters"][0]["id"],
            "mode": "continue",
            "instruction": "",
            "selection": "",
            "target_words": 800,
        },
    )
    section_names = [item["name"] for item in built.sections]
    assert "分层导演规划" in section_names


def test_planning_api_runs_master_then_volume_then_safe_apply(monkeypatch):
    project = default_project("api-plan", "诸子山河", "now")
    project["narrative"]["target_chapters"] = 3

    async def fake_chat_once(settings, messages, **kwargs):
        if "总导演" in messages[0]["content"]:
            return json.dumps(
                {
                    "theme": "秩序与正义",
                    "reader_promise": "思想交锋推动现实改变",
                    "central_conflict": "林砚与嬴政对统一代价的分歧",
                    "story_engine": "改革造成新选择与新代价",
                    "ending_state": "完成统一但保留制度争论",
                    "full_outline": (
                        "林砚以粮册复核取得有限信任，却发现制度效率与个人命运"
                        "始终互为代价。"
                    )
                    * 35,
                    "main_plot": "从仓曹试点到廷议问政，再以真实代价检验统一制度",
                    "theme_progression": "先质疑效率，再检验公平，最终面对统一后的权力边界",
                    "pacing_plan": "前段求生，中段试点受阻，后段廷议选择并支付代价",
                    "stakes_ladder": ["失去身份", "试点失败", "牵连同伴", "制度被滥用"],
                    "major_character_arcs": ["林砚：旁观求生→承担改革后果"],
                    "subplots": ["仓吏旧账：提供阻力并在廷议前回收"],
                    "historical_nodes": ["逐客令：允许改变执行方式，不改变政治背景"],
                    "volumes": [
                        {
                            "title": "入秦卷",
                            "chapter_count": 3,
                            "goal": "取得有限议政资格",
                            "conflict": "身份与理念双重冲突",
                            "synopsis": (
                                "林砚从无籍游士被编入仓曹，通过核对三套粮册"
                                "发现记录口径冲突。他拒绝直接指控贪墨，提出公开"
                                "复核的小规模办法，因此既获得试行机会，也招致"
                                "既得利益者反制。最终他必须在自保与承担之间选择，"
                                "并让试点结果成为下一阶段廷议的直接导火索。"
                            )
                            * 2,
                            "turning_points": ["账册互相矛盾", "公开复核遭到阻止"],
                            "character_arcs": ["林砚从旁观转为承担"],
                            "subplots": ["仓吏旧账"],
                            "must_keep": [],
                            "must_avoid": ["不可提前统一"],
                            "ending_state": "获得有限议政资格",
                            "bridge_to_next": "试点结果进入廷议并引出更大制度冲突",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        return """{"volume_id":"ignored","chapters":[
          {"number":1,"title":"雾入咸阳","goal":"林砚进入秦廷视野","conflict":"无籍身份受疑",
           "turning_point":"账册证明价值","ending_hook":"被召问策","must_keep":[],"must_avoid":[]},
          {"number":2,"title":"三套粮册","goal":"揭示征粮漏洞","conflict":"仓吏拒绝配合",
           "turning_point":"旧账互相矛盾","ending_hook":"廷议将开","must_keep":[],"must_avoid":[]},
          {"number":3,"title":"廷前问法","goal":"取得试行机会","conflict":"法家与现代治理冲突",
           "turning_point":"嬴政追问公平代价","ending_hook":"改革落地","must_keep":[],"must_avoid":[]}
        ]}"""

    monkeypatch.setattr("app.main.chat_once", fake_chat_once)
    master_response = client.post(
        "/api/planning/master", json={"project": project, "instruction": ""}
    )
    assert master_response.status_code == 200
    project["planning"] = master_response.json()
    volume_id = project["planning"]["volumes"][0]["id"]
    volume_response = client.post(
        "/api/planning/volume",
        json={"project": project, "volume_id": volume_id, "instruction": ""},
    )
    assert volume_response.status_code == 200
    assert volume_response.json()["complete"] is True
    project["planning"]["volumes"][0]["chapters"] = volume_response.json()["chapters"]
    apply_response = client.post(
        "/api/planning/apply-volume",
        json={"project": project, "volume_id": volume_id},
    )
    assert apply_response.status_code == 200
    assert [item["route"]["number"] for item in apply_response.json()["chapters"]] == [
        1,
        2,
        3,
    ]


def test_volume_batch_rewrites_once_after_quality_gate_failure(monkeypatch):
    project = default_project("batch-repair", "诸子山河", "now")
    project["narrative"]["target_chapters"] = 3
    project["planning"] = normalize_master_plan(
        {
            "theme": "秩序与正义",
            "central_conflict": "求生与制度代价",
            "volumes": [
                {
                    "title": "逐客之门",
                    "chapter_count": 3,
                    "goal": "沈砚获得粮册试办资格",
                    "ending_state": "有限试办获准",
                    "turning_points": ["账册矛盾公开", "既得利益者反制"],
                }
            ],
        },
        3,
    )
    calls = 0

    async def quality_failure_then_repair(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("章节标题重复，说明拆解没有形成独立事件")
        return (
            {
                "chapters": [
                    {
                        "number": 1,
                        "title": "渭水无籍",
                        "goal": "沈砚以无籍身份进入仓曹",
                        "conflict": "来历不明引发盘查",
                        "turning_point": "被迫以识数换取活路",
                        "ending_hook": "三套粮册送到案前",
                        "must_keep": [],
                        "must_avoid": [],
                    },
                    {
                        "number": 2,
                        "title": "三册异数",
                        "goal": "查明入仓与实存口径冲突",
                        "conflict": "旧吏拒绝交出原始木牍",
                        "turning_point": "缺失的转运签押出现",
                        "ending_hook": "复核将触及主事官",
                        "must_keep": [],
                        "must_avoid": [],
                    },
                    {
                        "number": 3,
                        "title": "仓门复核",
                        "goal": "以公开清点换取有限试办",
                        "conflict": "主事官要求沈砚承担失败罪责",
                        "turning_point": "李斯选择保留可验证的账法",
                        "ending_hook": "新账法进入三日试办",
                        "must_keep": [],
                        "must_avoid": [],
                    },
                ]
            },
            [],
        )

    monkeypatch.setattr("app.main.structured_completion", quality_failure_then_repair)
    volume_id = project["planning"]["volumes"][0]["id"]
    response = client.post(
        "/api/planning/volume",
        json={"project": project, "volume_id": volume_id, "instruction": ""},
    )
    assert response.status_code == 200
    result = response.json()
    assert calls == 2
    assert result["complete"] is True
    assert result["fallback"] is False
    assert len(result["chapters"]) == 3
    assert "再次生成并恢复" in result["warnings"][0]


def test_outline_fallback_extracts_named_volumes_and_ranges():
    project = default_project("fallback", "诸子山河", "now")
    project["outline"] = """### 第一卷：逐客之门（第1—12章）
沈砚在逐客风暴中取得有限议政资格。

### 第二卷：法、术与可计算之民（第13—24章）
改革让国家更高效，也让征发深入每户。
"""
    result = fallback_master_plan(project, 24, "本地模型响应超时")
    assert result["fallback"] is True
    assert [item["title"] for item in result["volumes"]] == [
        "逐客之门",
        "法、术与可计算之民",
    ]
    assert result["volumes"][0]["chapter_start"] == 1
    assert result["volumes"][0]["chapter_end"] == 12
    assert result["volumes"][1]["chapter_end"] == 24


def test_master_api_returns_editable_fallback_on_model_timeout(monkeypatch):
    project = default_project("timeout", "诸子山河", "now")
    project["narrative"]["target_chapters"] = 12
    project["outline"] = "### 第一卷：入秦（第1—12章）\n沈砚取得试行改革的机会。"

    async def timeout_chat(*args, **kwargs):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr("app.main.chat_once", timeout_chat)
    response = client.post(
        "/api/planning/master", json={"project": project, "instruction": ""}
    )
    assert response.status_code == 200
    assert response.json()["fallback"] is True
    assert response.json()["volumes"][0]["title"] == "入秦"
    assert "超时" in response.json()["warnings"][0]

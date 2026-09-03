import json

import pytest
from fastapi.testclient import TestClient

from app.db import default_project
from app.main import (
    app,
    historical_asset_conflicts,
    validate_asset_authority,
    validate_director_cast,
    validate_director_character_card,
    validate_director_seed_brief,
    validate_director_volume_core,
    validate_director_volume_expansion,
    validate_director_world,
    validate_incubator_assets,
    validate_incubator_core_result,
    validate_incubator_result,
    parse_json_response,
)


def test_json_parser_repairs_typographic_structural_quotes_only():
    parsed = parse_json_response(
        '{"name":"秦策","dialogue_examples":[“‘损耗须复核。’”],"voice":"避用“天命”一词"}'
    )
    assert parsed["dialogue_examples"] == ["‘损耗须复核。’"]
    assert parsed["voice"] == "避用“天命”一词"


def proposal(title: str) -> dict:
    return {
        "title": title,
        "genre": "历史架空",
        "positioning": "制度推演与人物选择并重",
        "premise": "现代研究者进入战国，以有限知识参与秦国改革。",
        "reader_promise": "每次解决现实问题都制造新的政治代价。",
        "central_question": "效率与人的尊严能否同时保全？",
        "central_conflict": "程序正义与战国生存逻辑不断冲突。",
        "story_engine": "危机、试行、反噬、修正形成持续循环。",
        "outline": "第一阶段建立身份并发现粮册问题。" * 40,
        "author_intent": "古人不降智，现代知识不等于万能答案。",
        "current_focus": "主角先取得一次有限试行的机会。",
        "book_rules": [f"硬规则{i}" for i in range(8)],
        "ending_direction": "统一完成，但制度代价仍由人物承担。",
        "tone": "冷峻克制",
        "pov": "third_limited",
        "target_chapters": 96,
        "opening_hook": "主角在运粮车中醒来并发现账目矛盾。",
        "first_arc": "从无籍者到临时计吏。",
        "characters": [
            {"name": "沈砚", "role": "主角"},
            {"name": "嬴政", "role": "秦王"},
            {"name": "李斯", "role": "政治对手与盟友"},
        ],
        "world_entries": [{"title": "秦国纪年", "keys": ["秦王政"]}],
    }


def test_incubator_requires_two_detailed_proposals():
    validate_incubator_result({"options": [proposal("方向甲"), proposal("方向乙")]})


def test_incubator_rejects_shallow_outline():
    first = proposal("方向甲")
    first["outline"] = "过短"
    with pytest.raises(ValueError, match="全书大纲过短"):
        validate_incubator_result({"options": [first, proposal("方向乙")]})


def test_director_seed_is_split_into_smaller_validated_results():
    first = proposal("单一方向")
    brief = {
        key: first[key]
        for key in (
            "title", "genre", "positioning", "premise", "reader_promise",
            "central_question", "central_conflict", "story_engine",
            "author_intent", "current_focus", "book_rules",
            "ending_direction", "tone", "pov", "opening_hook", "first_arc",
        )
    }
    brief["story_spine"] = first["outline"]
    validate_director_seed_brief(brief)
    cast = {"characters": [
        {"name": item["name"], "role": item["role"], "narrative_function": "独立功能", "relationship_seed": "初始关系", "core_conflict": "欲望与代价"}
        for item in first["characters"]
    ]}
    validate_director_cast(cast)
    validate_director_character_card({
        "name": "沈砚", "role": "主角", "aliases": [], "description": "现代研究者进入战国后的不可变背景。",
        "personality": "先核对证据再行动，受压时会缩小问题并坚持留下可复核记录。",
        "values": "程序与人的尊严", "contradictions": "想提高效率却警惕集权", "relationships": "与秦王互相利用",
        "arc": "从相信程序到承担制度代价", "hard_limits": "没有系统或超时代制造能力", "goal": "活下去",
        "knowledge": "仅有现代常识", "voice": "短句、先问证据、不说现代术语", "dialogue_examples": [],
    })
    validate_director_world({"world_entries": [
        {"title": f"设定{i}", "category": "制度", "keys": [f"词{i}"], "content": "必须保持一致的权威规则。"}
        for i in range(3)
    ]})


def test_director_seed_substeps_require_bounded_cast_and_lore():
    with pytest.raises(ValueError, match="3-5 人"):
        validate_director_cast({"characters": [{"name": "沈砚"}]})
    with pytest.raises(ValueError, match="3-5 个"):
        validate_director_world({"world_entries": []})


def test_incubator_uses_shorter_outline_floor_for_twelve_chapter_trial():
    first = proposal("十二章甲")
    second = proposal("十二章乙")
    first["outline"] = "开篇入秦并发现粮册差额；随后以旧例争取复核资格；中段试行新核账法并遭旧吏反制；高潮在责任追索与制度扩张之间选择；结尾只取得有限试点资格并留下政治代价。" * 3
    second["outline"] = "开篇从仓曹错账切入；第二阶段追查不同账册口径；第三阶段让改革触及既得利益；第四阶段由秦王决定是否承担试行风险；结尾保留制度收益与权力扩张的矛盾。" * 4
    validate_incubator_result(
        {"options": [first, second]}, target_chapters=12, story_mode="long"
    )


def test_director_three_chapter_short_accepts_compact_complete_spine():
    first = proposal("短篇")
    brief = {
        key: first[key]
        for key in (
            "title", "genre", "positioning", "premise", "reader_promise",
            "central_question", "central_conflict", "story_engine",
            "author_intent", "current_focus", "book_rules",
            "ending_direction", "tone", "pov", "opening_hook", "first_arc",
        )
    }
    brief["story_spine"] = (
        "开篇沈砚因粮册差额获得一次复核机会；发展阶段他用当时可行的算筹和双人复核暴露流程漏洞，"
        "却触发旧吏抵制；关键转折是秦王要求他证明新法不会破坏权责边界；高潮中沈砚放弃一次扩大权限的捷径，"
        "改用可追责的有限试行；结尾他只获得三日试点资格，同时承担若失败即被逐出的代价。"
    )
    validate_director_seed_brief(
        brief, target_chapters=3, story_mode="short"
    )


def test_staged_incubator_builds_two_complete_options_in_three_small_calls(monkeypatch):
    calls = []
    first = proposal("方向甲")
    second = proposal("方向乙")
    cores = []
    for item in (first, second):
        core = {
            key: value
            for key, value in item.items()
            if key not in {"characters", "world_entries"}
        }
        cores.append(core)

    def assets(prefix: str) -> dict:
        return {
            "characters": [
                {
                    "name": f"{prefix}{index}", "role": "主要人物",
                    "aliases": [], "description": "承担不可替代的剧情任务",
                    "personality": "先核对事实，再根据压力改变行动策略",
                    "values": "重视可复核的秩序", "contradictions": "想改变现实却害怕代价",
                    "relationships": "与其他人物保持有条件合作",
                    "hard_limits": "不知道尚未发生的未来", "goal": "完成开篇任务",
                    "knowledge": "只知道亲历和被明确告知的信息", "voice": "短句，先问证据",
                }
                for index in range(1, 4)
            ],
            "world_entries": [
                {
                    "title": f"{prefix}设定{index}", "category": "制度",
                    "keys": [f"触发词{index}"], "content": "开篇已经成立且必须保持一致的规则。",
                    "canon": "hard", "constant": False,
                }
                for index in range(1, 4)
            ],
        }

    async def fake_structured(_settings, _messages, **_kwargs):
        calls.append(_messages)
        if len(calls) == 1:
            return {"options": cores}, []
        return assets("甲" if len(calls) == 2 else "乙"), []

    monkeypatch.setattr("app.main.structured_completion", fake_structured)
    project = default_project("staged-incubator", "空白作品", "now")
    response = TestClient(app).post(
        "/api/incubator",
        json={
            "project": project,
            "seed": "战国粮账中的一次有限制度试验",
            "preferences": "古人不降智，秦统一前称谓准确",
            "story_mode": "short",
            "target_chapters": 3,
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert len(calls) == 3
    assert payload["generation_mode"] == "staged_transaction"
    assert len(payload["options"]) == 2
    assert all(len(option["characters"]) == 3 for option in payload["options"])
    assert all(option["target_chapters"] == 3 for option in payload["options"])


def test_incubator_stage_validators_accept_split_core_and_assets():
    first, second = proposal("方向甲"), proposal("方向乙")
    cores = {
        "options": [
            {key: value for key, value in item.items() if key not in {"characters", "world_entries"}}
            for item in (first, second)
        ]
    }
    validate_incubator_core_result(cores)
    validate_incubator_assets({
        "characters": [
            {
                "name": f"人物{i}", "role": "功能", "description": "具体背景",
                "personality": "先观察后行动", "values": "秩序", "contradictions": "选择冲突",
                "relationships": "有限合作", "hard_limits": "不能读心", "goal": "查清事实",
                "knowledge": "只知道亲历信息", "voice": "短句", "aliases": [],
            }
            for i in range(3)
        ],
        "world_entries": [
            {"title": f"设定{i}", "category": "制度", "keys": [f"词{i}"], "content": "规则"}
            for i in range(3)
        ],
    })


def test_pre_unification_assets_reject_post_unification_titles():
    authority = "故事发生在战国末期、秦统一前，主角面见秦王嬴政。"
    payload = {"name": "秦王嬴政", "aliases": ["始皇帝", "陛下"]}
    assert historical_asset_conflicts(payload, authority) == ["始皇帝", "皇帝", "陛下"]
    with pytest.raises(ValueError, match="秦统一前时间边界"):
        validate_asset_authority(payload, authority)
    validate_asset_authority(payload, "秦统一六国以后，始皇帝巡行天下。")


def test_staged_volume_uses_the_same_adaptive_synopsis_floor():
    short_synopsis = "梗" * 160
    core = {
        "volume_core": {
            "title": "短卷", "goal": "完成阶段目标", "conflict": "具体冲突",
            "synopsis": short_synopsis, "ending_state": "局势改变", "bridge_to_next": "进入后续",
        }
    }
    validate_director_volume_core(core, chapter_count=3)
    volume = {
        **core["volume_core"], "chapter_count": 3,
        "turning_points": ["一", "二", "三"], "character_arcs": [],
        "subplots": [], "must_keep": [], "must_avoid": [],
    }
    validate_director_volume_expansion({"volume": volume})
    with pytest.raises(ValueError, match="至少需要 180"):
        validate_director_volume_core(core, chapter_count=12)
    with pytest.raises(ValueError, match="至少需要 180"):
        validate_director_volume_expansion(
            {"volume": {**volume, "chapter_count": 12}}
        )

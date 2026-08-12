import pytest

from app.main import (
    validate_director_cast,
    validate_director_character_card,
    validate_director_seed_brief,
    validate_director_world,
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

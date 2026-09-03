from app.db import default_project
from app.prompts import activate_lore, build_prompt, estimate_tokens


def project():
    value = default_project("p1", "测试小说", "now")
    value["chapters"][0].update(
        {"id": "c1", "content": "林墨走进白塔，听见钟声。", "scene_goal": "找到老师"}
    )
    value["world_entries"] = [
        {
            "title": "白塔",
            "keys": ["白塔"],
            "content": "白塔没有窗。",
            "position": "near",
            "order": 200,
            "enabled": True,
        },
        {
            "title": "常驻规则",
            "keys": [],
            "content": "魔法需要代价。",
            "constant": True,
            "position": "before",
            "order": 10,
            "enabled": True,
        },
        {
            "title": "未触发",
            "keys": ["海港"],
            "content": "海港终年有雾。",
            "enabled": True,
        },
    ]
    return value


def test_lore_activation_and_order():
    lore = activate_lore(project(), "他抬头看见白塔")
    assert [item["title"] for item in lore] == ["常驻规则", "白塔"]


def test_prompt_contains_context_and_task():
    result = build_prompt(
        project(),
        {"chapter_id": "c1", "mode": "continue", "instruction": "写一场争执"},
    )
    combined = "\n".join(message["content"] for message in result.messages)
    assert "白塔没有窗" in combined
    assert "魔法需要代价" in combined
    assert "海港终年有雾" not in combined
    assert "写一场争执" in combined
    assert result.estimated_tokens > 0
    assert any(section["priority"] == 100 for section in result.sections)
    assert "续写的第一句必须" in combined
    assert "作品形态" in combined


def test_author_note_belongs_only_to_current_chapter():
    value = project()
    value["author_note"] = "不应继续使用的旧全局作者注"
    value["chapters"][0]["author_note"] = "本章压低情绪，不揭示老师身份"
    result = build_prompt(
        value,
        {"chapter_id": "c1", "mode": "instruction", "instruction": "写本章"},
    )
    combined = "\n".join(message["content"] for message in result.messages)
    assert "本章作者临时注" in combined
    assert "本章压低情绪，不揭示老师身份" in combined
    assert "不应继续使用的旧全局作者注" not in combined


def test_estimate_tokens_handles_chinese_and_latin():
    assert estimate_tokens("这是中文测试") > 1
    assert estimate_tokens("hello world") > 1


def test_long_prompt_is_budgeted_and_reports_trace():
    value = project()
    value["settings"]["context_budget"] = 1200
    value["outline"] = "很长的大纲。" * 3000
    value["style"]["sample"] = "样本文字。" * 2000
    result = build_prompt(
        value,
        {"chapter_id": "c1", "mode": "continue", "instruction": "继续调查白塔"},
    )
    assert result.budget_warnings
    assert any("裁剪" in warning for warning in result.budget_warnings)
    assert all(
        section["status"] in {"included", "trimmed", "omitted"}
        for section in result.sections
    )
    assert any(
        section["status"] in {"trimmed", "omitted"}
        and section["tokens_before"] >= section["tokens_after"]
        and section["reason"]
        for section in result.sections
    )
    combined = "\n".join(message["content"] for message in result.messages)
    assert "继续调查白塔" in combined


def test_prompt_budget_reserves_space_for_generated_prose():
    value = project()
    value["settings"]["context_budget"] = 6000
    value["settings"]["max_tokens"] = 1800
    value["outline"] = "阶段因果与人物选择。" * 3000
    value["style"]["sample"] = "样本文字与节奏。" * 2000
    result = build_prompt(
        value,
        {"chapter_id": "c1", "mode": "continue", "instruction": "继续调查白塔"},
    )
    assert result.estimated_tokens <= 3900


def test_large_cast_keeps_main_cast_and_current_relevant_character():
    value = project()
    value["characters"] = [
        {
            "name": f"人物{index}",
            "role": "配角",
            "description": "各自拥有不同立场",
        }
        for index in range(25)
    ]
    result = build_prompt(
        value,
        {
            "chapter_id": "c1",
            "mode": "continue",
            "instruction": "让人物24带着证据进入白塔",
        },
    )
    combined = "\n".join(message["content"] for message in result.messages)
    assert "【人物0】" in combined
    assert "【人物24】" in combined
    assert "【人物18】" not in combined


def test_unresolved_continuity_note_is_injected_but_resolved_note_is_not():
    value = project()
    value["memory"]["continuity_notes"] = [
        {
            "id": "n1",
            "text": "铜钥匙的持有人尚未确认",
            "chapter_title": "第二章",
            "resolved": False,
        },
        {
            "id": "n2",
            "text": "已经处理的旧备注",
            "resolved": True,
        },
    ]
    result = build_prompt(
        value,
        {"chapter_id": "c1", "mode": "continue", "instruction": "继续调查"},
    )
    combined = "\n".join(message["content"] for message in result.messages)
    assert "铜钥匙的持有人尚未确认" in combined
    assert "已经处理的旧备注" not in combined


def test_synced_detailed_outline_is_not_injected_twice():
    value = project()
    detailed = "林墨从钟声入手调查白塔，并因隐瞒付出信任代价。" * 30
    value["outline"] = detailed
    value["planning"]["master"]["full_outline"] = detailed
    result = build_prompt(
        value,
        {"chapter_id": "c1", "mode": "continue", "instruction": "继续调查"},
    )
    combined = "\n".join(message["content"] for message in result.messages)
    assert combined.count(detailed) == 0
    assert "已与AI详细全书大纲同步" in combined


def test_route_card_fills_execution_plan_before_chapter_plan_exists():
    value = project()
    value["chapters"][0]["plan"] = {}
    value["chapters"][0]["route"] = {
        "goal": "只核验三份简牍与仓门封泥",
        "conflict": "三份真简牍互相矛盾",
        "turning_point": "封泥下有今日重压细痕",
        "ending_hook": "日落前只获准开仓一次",
        "must_keep": ["三份封泥都是真的"],
        "must_avoid": ["不得决定主营与偏师取舍"],
    }
    result = build_prompt(
        value,
        {"chapter_id": "c1", "mode": "continue", "instruction": "严格按本章路线写"},
    )
    combined = "\n".join(message["content"] for message in result.messages)
    assert "只核验三份简牍与仓门封泥" in combined
    assert "三份真简牍互相矛盾" in combined
    assert "封泥下有今日重压细痕" in combined
    assert "日落前只获准开仓一次" in combined
    assert "不得决定主营与偏师取舍" in combined


def test_future_volume_terms_do_not_activate_current_chapter_lore():
    value = project()
    value["planning"]["volumes"].append(
        {
            "id": "future-volume",
            "title": "海港卷",
            "chapter_start": 2,
            "chapter_end": 2,
            "chapter_count": 1,
            "goal": "第二章才前往海港",
            "chapters": [],
        }
    )
    result = build_prompt(
        value,
        {"chapter_id": "c1", "mode": "continue", "instruction": "继续调查白塔"},
    )
    combined = "\n".join(message["content"] for message in result.messages)
    assert "白塔没有窗" in combined
    assert "海港终年有雾" not in combined

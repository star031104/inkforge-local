import asyncio

import pytest

from fastapi.testclient import TestClient

from app.db import ProjectStore, default_project
from app.planning import normalize_master_plan, normalize_route


def test_memory_apply_rejects_unknown_character_and_normalizes_thread_state():
    import app.main as main

    project = default_project("p1", "测试", "2026-01-01T00:00:00+00:00")
    project["characters"] = [{"id": "c1", "name": "沈砚"}]
    chapter = project["chapters"][0]
    chapter["content"] = "沈砚核对粮册，确认粮册已经封存。墨痕仍留在被改过的账页上。"
    result = {
        "summary": "沈砚核对粮册。",
        "character_updates": [{"name": "不存在的人", "state": "受伤"}],
        "facts": [{"text": "粮册已经封存", "importance": 99, "evidence": "粮册已经封存"}],
        "plot_threads": [{"title": "谁改了账", "status": "invalid", "latest": "留下墨痕", "evidence": "墨痕仍留在被改过的账页上"}],
        "timeline": [],
        "continuity_notes": [],
    }
    warnings = main._apply_director_memory(project, chapter, result)
    assert len(warnings) == 2
    assert project["memory"]["facts"][0]["importance"] == 5
    assert project["memory"]["plot_threads"][0]["status"] == "open"
    assert chapter["execution"]["warnings"] == warnings


def test_chapter_audit_explains_conditional_ending_rule_and_normalizes_clean_85(monkeypatch):
    import app.main as main

    project = default_project("p-audit", "测试", "2026-01-01T00:00:00+00:00")
    chapter = project["chapters"][0]
    chapter["route"] = {
        "ending_hook": "日落前获准开仓，粮囤未空。",
        "must_avoid": ["不得在结尾前打开仓门。"],
    }
    captured = {}

    async def fake_completion(_settings, messages, **_kwargs):
        captured["prompt"] = messages[-1]["content"]
        return {
            "score": 85,
            "verdict": "pass",
            "issues": [],
            "strengths": [],
            "revision_brief": "",
        }, []

    monkeypatch.setattr(main, "structured_completion", fake_completion)
    monkeypatch.setattr(
        main,
        "local_quality_check",
        lambda *_args, **_kwargs: {"score": 100, "verdict": "pass", "issues": []},
    )
    result = asyncio.run(
        main.chapter_audit(
            main.ChapterActionRequest(
                project=project,
                chapter_id=chapter["id"],
                draft="秦策核验三简与封泥。" * 20,
            )
        )
    )

    assert "本章人工核定路线" in captured["prompt"]
    assert "最后一个场景发生是正确交付" in captured["prompt"]
    assert result["score"] == 90
    assert result["verdict"] == "pass"


def test_scene_hard_constraints_reject_invented_quantity_and_ending_leak():
    import app.main as main

    route = {
        "goal": "核验三份简牍。",
        "ending_hook": "日落前获准开仓一次，粮囤未空。",
    }
    violations = main._scene_constraint_violations(
        "他在三更数到第七车，随后开仓。", route, 1, 4, ["开仓"]
    )

    assert any("未授权" in item and "三更" in item and "七车" in item for item in violations)
    assert any("提前" in item for item in violations)
    assert main._number_phrases("他站在一旁，一时没有开口。") == set()


def test_scene_hard_constraints_require_final_delayed_action():
    import app.main as main

    route = {"ending_hook": "日落前，王绾准他开仓一次；仓门后粮囤未空。"}
    violations = main._scene_constraint_violations(
        "日落前，王绾准他继续查验，粮囤似乎仍在。", route, 4, 4, ["开仓"]
    )

    assert any("遗漏" in item for item in violations)


def test_nonfinal_scene_strips_whole_unsafe_sentence_without_rewriting_number():
    import app.main as main

    route = {"goal": "核验三份简牍。", "ending_hook": "日落前开仓一次。"}
    text = "秦策把三份简牍铺开。仓吏称库中只有两百石。秦策当即开仓。暮色压上仓墙。"
    cleaned = main._strip_nonfinal_scene_violations(text, route, 1, 4, ["开仓"])

    assert cleaned == "秦策把三份简牍铺开。暮色压上仓墙。"
    assert "三百石" not in cleaned


def test_final_scene_deterministically_lands_reviewed_ending_hook():
    import app.main as main

    route = {"ending_hook": "日落前，王绾准他开仓一次；仓门后粮囤未空。"}
    closed = main._ensure_final_route_closure(
        "王绾看了看天色，没有立即回答。", route, ["开仓"]
    )

    assert closed.endswith("日落前，王绾准他开仓一次；仓门后粮囤未空。")
    assert main._scene_constraint_violations(closed, route, 4, 4, ["开仓"]) == []


def test_final_scene_strips_wrong_logistics_number_but_keeps_ending_action():
    import app.main as main

    route = {"ending_hook": "王绾准他开仓一次，仓内仍有三百石。"}
    cleaned = main._strip_nonfinal_scene_violations(
        "秦策开仓。仓吏报称只余三十九石。王绾站在门外。",
        route,
        4,
        4,
        ["开仓"],
    )

    assert cleaned == "秦策开仓。王绾站在门外。"


def test_scene_number_authority_does_not_leak_later_route_numbers():
    import app.main as main

    route = {
        "goal": "核验三份简牍。",
        "conflict": "仓简称三百石，驿简称一百二十车。",
        "ending_hook": "日落前开仓一次。",
    }
    violations = main._scene_constraint_violations(
        "秦策尚未核验，便断言仓中有三百石。",
        route,
        1,
        4,
        ["开仓"],
        route["goal"],
    )

    assert any("三百石" in item for item in violations)


def test_scene_before_opening_cannot_describe_warehouse_interior():
    import app.main as main

    route = {"goal": "在仓门外核验简牍。", "ending_hook": "日落前开仓一次。"}
    text = "秦策查看封泥。仓内粮垛排列整齐。他仍站在门外。"
    cleaned = main._strip_nonfinal_scene_violations(
        text, route, 1, 4, ["开仓"], route["goal"]
    )

    assert cleaned == "秦策查看封泥。他仍站在门外。"


def test_chapter_audit_provider_pass_85_keeps_optional_medium_note_nonblocking(monkeypatch):
    import app.main as main

    project = default_project("p-perfect", "测试", "2026-01-01T00:00:00+00:00")
    chapter = project["chapters"][0]

    async def fake_completion(_settings, _messages, **_kwargs):
        return {
            "score": 85,
            "verdict": "pass",
            "issues": [{"severity": "medium", "category": "道具", "message": "可加强象征", "suggestion": "可选"}],
            "strengths": [],
            "revision_brief": "可选润色",
        }, []

    monkeypatch.setattr(main, "structured_completion", fake_completion)
    monkeypatch.setattr(
        main,
        "local_quality_check",
        lambda *_args, **_kwargs: {"score": 100, "verdict": "pass", "issues": []},
    )
    result = asyncio.run(
        main.chapter_audit(
            main.ChapterActionRequest(
                project=project,
                chapter_id=chapter["id"],
                draft="秦策核验简牍与封泥。" * 20,
            )
        )
    )

    assert result["score"] == 90
    assert result["verdict"] == "pass"
    assert result["issues"]


def test_global_sentence_dedupe_removes_nonadjacent_model_loop():
    import app.main as main

    repeated = "秦策将清单收起，转身望向院中空荡的痕迹。"
    text = repeated + "赵明俯身查看旧辙。" + repeated + "王绾没有作声。"
    cleaned = main._dedupe_repeated_sentences(text)

    assert cleaned.count(repeated) == 1
    assert "赵明俯身查看旧辙。" in cleaned
    assert "王绾没有作声。" in cleaned


def test_global_sentence_dedupe_removes_high_similarity_paraphrase_loop():
    import app.main as main

    text = (
        "秦策将清单收起，转身望向院中空荡的车辙痕迹。"
        "赵明俯身核对泥地。"
        "秦策收起清单，转身又望向院中空荡的车辙痕迹。"
    )
    cleaned = main._dedupe_repeated_sentences(text)

    assert cleaned.count("车辙痕迹") == 1


def test_director_task_is_persistent_and_running_task_pauses_after_restart(tmp_path):
    path = tmp_path / "director.db"
    store = ProjectStore(path)
    project = store.create("测试作品")
    task = store.create_director_task(
        project["id"], {"phase": "chapters", "message": "测试", "config": {}}
    )
    task["status"] = "running"
    store.save_director_task(task["id"], task)

    restarted = ProjectStore(path)
    loaded = restarted.get_director_task(task["id"])
    assert loaded["status"] == "paused"
    assert loaded["phase"] == "chapters"


def test_auto_director_runs_all_stages_and_checkpoints(monkeypatch, tmp_path):
    import app.main as main

    director_store = ProjectStore(tmp_path / "pipeline.db")
    monkeypatch.setattr(main, "store", director_store)
    created = director_store.create("自动新作")
    task = director_store.create_director_task(
        created["id"],
        {
            "phase": "incubator",
            "message": "等待",
            "events": [],
            "completed_chapters": 0,
            "quality_debts": [],
            "config": {
                "seed": "现代研究者穿越战国参与制度改革",
                "preferences": "古人不降智",
                "story_mode": "long",
                "target_chapters": 3,
                "target_words": 500,
                "quality_threshold": 78,
                "max_revision_attempts": 1,
                "continue_on_quality_debt": True,
            },
        },
    )

    option = {
        "title": "诸子新政",
        "genre": "历史架空",
        "premise": "现代研究者以有限知识参与秦国改革。",
        "outline": "开篇立足、试行受阻、承担代价并完成阶段选择。" * 30,
        "author_intent": "不把现代制度当万能答案。",
        "current_focus": "前三章取得有限试行资格。",
        "book_rules": ["古人不降智", "没有系统"],
        "central_question": "效率与人的尊严能否兼得？",
        "central_conflict": "程序正义与战国生存逻辑冲突。",
        "ending_direction": "统一完成但权力代价保留。",
        "first_arc": "从无籍者到低级计吏。",
        "opening_hook": "主角在运粮车中醒来。",
        "pov": "third_limited",
        "tone": "冷峻克制",
        "characters": [{"name": "沈砚", "role": "主角"}],
        "world_entries": [{"title": "秦国纪年", "keys": ["秦王政"], "content": "统一前纪年。"}],
    }

    async def fake_seed_brief(_project, _config):
        return (
            {
                key: value
                for key, value in option.items()
                if key not in {"characters", "world_entries", "outline"}
            }
            | {"story_spine": option["outline"]},
            [],
        )

    cast = [
        {"name": "沈砚", "role": "主角"},
        {"name": "嬴政", "role": "秦王"},
        {"name": "李斯", "role": "盟友与对手"},
    ]

    async def fake_seed_cast(_project, _config, _brief):
        return {"characters": cast}, []

    async def fake_character_card(_project, _config, _brief, _cast, target):
        return {**target, "personality": "按证据行动", "voice": "短句"}, []

    async def fake_seed_world(_project, _config, _brief, _cast):
        return {"world_entries": [
            {"title": f"设定{i}", "keys": [f"词{i}"], "content": "规则"}
            for i in range(3)
        ]}, []

    planning = normalize_master_plan(
        {
            "theme": "秩序与代价",
            "reader_promise": "制度选择持续反噬人物",
            "central_conflict": "程序与效率冲突",
            "story_engine": "危机、试行、反制、修正",
            "ending_state": "阶段选择完成",
            "full_outline": "三章完成一次小型制度试行。" * 30,
            "main_plot": "主角取得有限试行资格",
            "theme_progression": "从效率走向权力边界",
            "pacing_plan": "逐章升级",
            "volumes": [{"title": "逐客之门", "chapter_count": 3, "goal": "取得资格", "conflict": "旧吏抵制", "ending_state": "获准试行"}],
        },
        3,
    )
    planning["fallback"] = False

    async def fake_master_bible(_project, _instruction=""):
        return {
            key: value for key, value in planning["master"].items()
            if key != "full_outline"
        }, []

    async def fake_master_contract(_project, _bible, specs, index, _completed):
        spec = specs[index]
        return {
            "number": spec["number"], "title": planning["volumes"][index]["title"],
            "goal": planning["volumes"][index]["goal"], "conflict": planning["volumes"][index]["conflict"],
            "ending_state": planning["volumes"][index]["ending_state"],
            "bridge_to_next": planning["volumes"][index]["bridge_to_next"], "theme_test": "检验秩序代价",
        }, []

    async def fake_volume_core(_project, _bible, _contracts, specs, index, _completed):
        volume = dict(planning["volumes"][index])
        volume["chapter_count"] = specs[index]["chapter_count"]
        volume["synopsis"] = "具体事件推动选择、反制、代价与阶段结果。" * 24
        return volume, []

    async def fake_volume_details(_project, _bible, _contracts, _specs, _index, _core):
        return {
            "character_arcs": ["沈砚改变", "嬴政改变"], "subplots": ["粮册支线"],
            "must_keep": ["保留事实"], "must_avoid": ["避免提前结局"],
            "turning_points": ["转折一", "转折二", "转折三"],
        }, []

    async def fake_route(_project, _volume, chapter_number, _previous):
        route = normalize_route(
            {
                "title": f"事件{chapter_number}", "goal": f"完成行动{chapter_number}",
                "conflict": f"遭遇阻力{chapter_number}", "turning_point": f"发现证据{chapter_number}",
                "ending_hook": f"形成问题{chapter_number + 1}", "must_keep": [], "must_avoid": [],
            },
            chapter_number,
        )
        if chapter_number == 2:
            route["quality_warnings"] = ["测试用软质量风险"]
        return route, []

    async def fake_plan(_body):
        return {"goal": "执行本章行动", "conflict": "对手阻止", "must_keep": [], "must_avoid": [], "turning_point": "证据改变判断", "ending_hook": "进入下一章", "fallback": False}

    async def fake_prose(_task, _project, chapter, **_kwargs):
        return (f"{chapter['title']}中，沈砚核对记录并面对阻力。" * 35)[:650]

    async def fake_audit(_body):
        return {"verdict": "pass", "score": 92, "issues": [], "local_checks": {"issues": []}}

    async def fake_memory(body):
        return {"summary": "沈砚完成一次核对并承担代价。", "story_so_far": "主角逐步取得试行资格。", "character_updates": [], "facts": [], "plot_threads": [], "timeline": [], "continuity_notes": [], "fallback": False}

    monkeypatch.setattr(main, "director_seed_brief", fake_seed_brief)
    monkeypatch.setattr(main, "director_seed_cast", fake_seed_cast)
    monkeypatch.setattr(main, "director_character_card", fake_character_card)
    monkeypatch.setattr(main, "director_seed_world", fake_seed_world)
    monkeypatch.setattr(main, "director_master_bible", fake_master_bible)
    monkeypatch.setattr(main, "director_master_contract", fake_master_contract)
    monkeypatch.setattr(main, "director_master_volume_core", fake_volume_core)
    monkeypatch.setattr(main, "director_master_volume_details", fake_volume_details)
    monkeypatch.setattr(main, "director_plan_chapter_route", fake_route)
    monkeypatch.setattr(main, "chapter_plan", fake_plan)
    monkeypatch.setattr(main, "_director_generate_prose", fake_prose)
    monkeypatch.setattr(main, "chapter_audit", fake_audit)
    monkeypatch.setattr(main, "chapter_memory", fake_memory)

    asyncio.run(main._run_auto_director(task["id"]))

    completed = director_store.get_director_task(task["id"])
    project = director_store.get(created["id"])
    assert completed["status"] == "completed"
    assert completed["phase"] == "completed"
    assert completed["completed_chapters"] == 3
    assert completed["seed_brief"]["title"] == "诸子新政"
    assert completed["seed_assets"]["characters"]
    assert len(project["chapters"]) == 3
    assert all(chapter["content"] for chapter in project["chapters"])
    assert all(chapter["summary"] for chapter in project["chapters"])
    assert completed["planning_debts"][0]["phase"] == "route"
    assert completed["planning_debts"][0]["chapter"] == 2


def test_director_resume_skips_saved_seed_brief(monkeypatch, tmp_path):
    import app.main as main

    director_store = ProjectStore(tmp_path / "seed-checkpoint.db")
    monkeypatch.setattr(main, "store", director_store)
    created = director_store.create("自动新作")
    brief = {
        "title": "诸子新政",
        "genre": "历史架空",
        "positioning": "制度推演",
        "premise": "现代研究者参与秦国改革。",
        "reader_promise": "每次改革都产生政治代价。",
        "central_question": "效率与尊严能否兼得？",
        "central_conflict": "程序正义与战国生存逻辑冲突。",
        "story_engine": "危机、试行、反噬与修正循环。",
        "story_spine": "开篇立足、改革受阻、联盟分裂、统一前夜作出终局选择。" * 12,
        "author_intent": "古人不降智。",
        "current_focus": "先取得试行资格。",
        "book_rules": [f"规则{i}" for i in range(6)],
        "ending_direction": "统一完成但留下权力代价。",
        "tone": "冷峻克制",
        "pov": "third_limited",
        "opening_hook": "主角在运粮车中醒来。",
        "first_arc": "从无籍者到临时计吏。",
    }
    cast = [
        {"name": "沈砚", "role": "主角"},
        {"name": "嬴政", "role": "秦王"},
        {"name": "李斯", "role": "盟友与对手"},
    ]
    task = director_store.create_director_task(
        created["id"],
        {
            "phase": "incubator",
            "seed_brief": brief,
            "seed_cast": {"characters": cast},
            "seed_character_cards": [
                {"name": "沈砚", "role": "主角", "personality": "按证据行动", "voice": "短句"}
            ],
            "incubator_step": "characters",
            "message": "人物资产生成前暂停",
            "events": [],
            "completed_chapters": 0,
            "quality_debts": [],
            "config": {
                "seed": "现代研究者穿越战国参与制度改革",
                "preferences": "古人不降智",
                "story_mode": "long",
                "target_chapters": 3,
                "target_words": 500,
                "quality_threshold": 78,
                "max_revision_attempts": 1,
                "continue_on_quality_debt": True,
            },
        },
    )
    brief_calls = 0

    async def should_not_regenerate_brief(*_args):
        nonlocal brief_calls
        brief_calls += 1
        raise AssertionError("saved brief must not be regenerated")

    async def should_not_regenerate_cast(*_args):
        raise AssertionError("saved cast must not be regenerated")

    generated_cards = []
    async def fake_card(_project, _config, _brief, _cast, target):
        generated_cards.append(target["name"])
        return {**target, "personality": "按证据行动", "voice": "短句"}, []

    async def fake_world(*_args):
        return {"world_entries": [
            {"title": f"设定{i}", "keys": [f"词{i}"], "content": "规则"}
            for i in range(3)
        ]}, []

    async def stop_at_master(*_args):
        raise RuntimeError("test stop after checkpoint")

    monkeypatch.setattr(main, "director_seed_brief", should_not_regenerate_brief)
    monkeypatch.setattr(main, "director_seed_cast", should_not_regenerate_cast)
    monkeypatch.setattr(main, "director_character_card", fake_card)
    monkeypatch.setattr(main, "director_seed_world", fake_world)
    monkeypatch.setattr(main, "director_master_bible", stop_at_master)
    asyncio.run(main._run_auto_director(task["id"]))
    resumed = director_store.get_director_task(task["id"])
    assert brief_calls == 0
    assert generated_cards == ["嬴政", "李斯"]
    assert resumed["phase"] == "master"
    assert len(resumed["seed_character_cards"]) == 3
    assert resumed["seed_assets"]["characters"][0]["name"] == "沈砚"


def test_master_contracts_resume_from_first_unsaved_volume(monkeypatch, tmp_path):
    import app.main as main

    director_store = ProjectStore(tmp_path / "master-contract-checkpoint.db")
    monkeypatch.setattr(main, "store", director_store)
    project = director_store.create("契约断点")
    project["narrative"]["target_chapters"] = 24
    project = director_store.save(project["id"], project, reason="test-target")
    bible = {
        "theme": "秩序与代价", "reader_promise": "改革持续反噬人物",
        "central_conflict": "程序与权力冲突", "story_engine": "试行与反制循环",
        "ending_state": "人物承担制度代价", "main_plot": "具体主线" * 50,
        "theme_progression": "逐层检验", "pacing_plan": "两卷升级",
        "stakes_ladder": ["一", "二", "三", "四"],
        "major_character_arcs": ["甲", "乙", "丙"], "subplots": [], "historical_nodes": [],
    }
    task = director_store.create_director_task(
        project["id"],
        {
            "phase": "master", "master_bible": bible, "events": [],
            "message": "等待", "completed_chapters": 0, "quality_debts": [],
            "config": {"target_chapters": 24, "continue_on_quality_debt": True},
        },
    )
    first_calls = []

    async def fail_on_second(_project, _bible, specs, index, _completed):
        first_calls.append(index)
        if index == 1:
            raise RuntimeError("第二卷模拟失败")
        return {
            "number": 1, "title": "第一卷", "goal": "完成第一阶段",
            "conflict": "第一阶段冲突", "ending_state": "进入第二阶段",
            "bridge_to_next": "第一卷结果触发第二卷", "theme_test": "检验第一次选择",
        }, []

    monkeypatch.setattr(main, "director_master_contract", fail_on_second)
    asyncio.run(main._run_auto_director(task["id"]))
    paused = director_store.get_director_task(task["id"])
    assert first_calls == [0, 1]
    assert len(paused["master_contracts"]) == 1

    resumed_calls = []

    async def finish_second(_project, _bible, specs, index, _completed):
        resumed_calls.append(index)
        return {
            "number": 2, "title": "第二卷", "goal": "完成第二阶段",
            "conflict": "第二阶段冲突", "ending_state": "完成终局选择",
            "bridge_to_next": "进入结局", "theme_test": "检验最终代价",
        }, []

    async def stop_before_expansion(*_args):
        raise RuntimeError("模拟蓝图阶段暂停")

    monkeypatch.setattr(main, "director_master_contract", finish_second)
    monkeypatch.setattr(main, "director_master_volume_core", stop_before_expansion)
    asyncio.run(main._run_auto_director(task["id"]))
    resumed = director_store.get_director_task(task["id"])
    assert resumed_calls == [1]
    assert len(resumed["master_contracts"]) == 2


def test_volume_routes_resume_from_first_unsaved_chapter(monkeypatch, tmp_path):
    import app.main as main

    director_store = ProjectStore(tmp_path / "route-checkpoint.db")
    monkeypatch.setattr(main, "store", director_store)
    project = director_store.create("逐章断点")
    project["narrative"]["target_chapters"] = 3
    project["planning"] = normalize_master_plan(
        {
            "theme": "选择与代价", "reader_promise": "逐章推进", "central_conflict": "改革冲突",
            "story_engine": "行动与反制", "ending_state": "完成阶段选择",
            "full_outline": "第一卷通过三次具体行动完成一次有限改革。" * 30,
            "main_plot": "三章完成改革", "theme_progression": "从尝试到代价", "pacing_plan": "逐章升级",
            "volumes": [{
                "title": "粮册之门", "chapter_count": 3, "goal": "取得试行资格",
                "conflict": "旧吏阻止核账", "synopsis": "调查粮册、遭遇反制并完成有限选择。" * 12,
                "turning_points": ["发现错账", "证人反口", "公开复核"],
                "character_arcs": ["沈砚改变", "旧吏改变"], "subplots": ["证人支线"],
                "must_keep": ["古人不降智"], "must_avoid": ["提前完成统一"],
                "ending_state": "获准有限试行", "bridge_to_next": "试行引发新阻力",
            }],
        },
        3,
    )
    project = director_store.save(project["id"], project, reason="test-planning")
    task = director_store.create_director_task(
        project["id"],
        {
            "phase": "volumes", "volume_index": 0, "total_volumes": 1,
            "events": [], "message": "等待", "completed_chapters": 0,
            "quality_debts": [], "config": {"continue_on_quality_debt": True},
        },
    )
    first_calls = []

    async def fail_second(_project, _volume, chapter_number, _previous):
        first_calls.append(chapter_number)
        if chapter_number == 2:
            raise RuntimeError("第二章路线模拟失败")
        return normalize_route({
            "title": "第一证词", "goal": "取得第一项可复核证据",
            "conflict": "旧吏拒绝交册", "turning_point": "发现错账的墨迹不同",
            "ending_hook": "证人被带走", "must_keep": [], "must_avoid": [],
        }, chapter_number), []

    monkeypatch.setattr(main, "director_plan_chapter_route", fail_second)
    asyncio.run(main._run_auto_director(task["id"]))
    paused = director_store.get_director_task(task["id"])
    volume_id = project["planning"]["volumes"][0]["id"]
    assert first_calls == [1, 2]
    assert len(paused["route_checkpoints"][volume_id]) == 1

    resumed_calls = []

    async def finish_routes(_project, _volume, chapter_number, _previous):
        resumed_calls.append(chapter_number)
        return normalize_route({
            "title": f"事件{chapter_number}", "goal": f"形成局面{chapter_number}",
            "conflict": f"遭遇阻力{chapter_number}", "turning_point": f"证据变化{chapter_number}",
            "ending_hook": f"留下问题{chapter_number}", "must_keep": [], "must_avoid": [],
        }, chapter_number), []

    async def stop_at_prose_stage(*_args):
        raise RuntimeError("路线测试到此结束")

    monkeypatch.setattr(main, "director_plan_chapter_route", finish_routes)
    monkeypatch.setattr(main, "chapter_plan", stop_at_prose_stage)
    asyncio.run(main._run_auto_director(task["id"]))
    resumed = director_store.get_director_task(task["id"])
    saved_project = director_store.get(project["id"])
    assert resumed_calls == [2, 3]
    assert resumed["phase"] == "chapters"
    assert len(saved_project["planning"]["volumes"][0]["chapters"]) == 3


def test_resume_reapplies_complete_routes_before_skipping_volume(monkeypatch, tmp_path):
    import app.main as main

    director_store = ProjectStore(tmp_path / "reapply-complete-routes.db")
    monkeypatch.setattr(main, "store", director_store)
    project = director_store.create("路线重同步")
    project["narrative"]["target_chapters"] = 3
    project["planning"] = normalize_master_plan(
        {"volumes": [{"title": "函谷三日", "chapter_count": 3}]}, 3
    )
    volume = project["planning"]["volumes"][0]
    volume["chapters"] = [
        normalize_route(
            {
                "title": f"新路线{i}", "goal": f"形成新的局面{i}",
                "conflict": f"具体阻力迫使秦策选择{i}",
                "turning_point": f"证据改变行动方向{i}",
                "ending_hook": f"结果触发下一步{i}",
                "must_keep": [], "must_avoid": [],
            },
            i,
        )
        for i in range(1, 4)
    ]
    while len(project["chapters"]) < 3:
        project["chapters"].append(
            {"id": f"c{len(project['chapters']) + 1}", "title": "旧章", "content": "", "scene_goal": "旧目标", "plan": {}}
        )
    for index, chapter in enumerate(project["chapters"]):
        chapter["route_id"] = f"old-{index}"
        chapter["title"] = f"旧路线{index + 1}"
        chapter["scene_goal"] = "旧目标"
    project = director_store.save(project["id"], project, reason="test-stale")
    task = director_store.create_director_task(
        project["id"],
        {
            "phase": "volumes", "volume_index": 0, "events": [],
            "message": "恢复", "completed_chapters": 0, "quality_debts": [],
            "planning_debts": [], "config": {"target_words": 500},
        },
    )

    monkeypatch.setattr(
        main, "_audit_route_checkpoint_prefix", lambda *_args: (3, "")
    )

    async def stop_at_chapter_plan(*_args, **_kwargs):
        raise RuntimeError("stop after route reapply")

    monkeypatch.setattr(main, "chapter_plan", stop_at_chapter_plan)
    asyncio.run(main._run_auto_director(task["id"]))
    saved = director_store.get(project["id"])
    assert [item["title"] for item in saved["chapters"][:3]] == [
        "新路线1", "新路线2", "新路线3"
    ]
    assert [item["scene_goal"] for item in saved["chapters"][:3]] == [
        "形成新的局面1", "形成新的局面2", "形成新的局面3"
    ]


def test_twelve_chapter_jobs_and_state_dimensions_are_distinct():
    import app.main as main

    jobs = [main._director_chapter_job(index, 12) for index in range(12)]
    dimensions = [main._director_state_dimension(index, 12) for index in range(12)]
    assert len(set(jobs)) == 12
    assert len(set(dimensions)) == 12
    assert "反常证据" in jobs[2]
    assert dimensions[2] == "证据与认知"
    assert "卷末兑现" in jobs[-1]


def test_anomaly_route_cannot_end_by_proving_old_solution_correct():
    import app.main as main

    with pytest.raises(ValueError, match="章节岗位高度重复"):
        main.validate_director_route_role(
            {
                "goal": "发现余粮临界风险，必须重新判断模型边界。",
                "turning_point": "计算最终证明原模型正确，迫使王绾承认方案有效。",
                "ending_hook": "继续推广模型。",
            },
            2,
            12,
        )


def test_route_role_rejects_internal_director_jargon():
    import app.main as main

    with pytest.raises(ValueError, match="导演节拍术语"):
        main.validate_director_route_role(
            {
                "goal": "秦策形成两个不可兼得的选择，使可选方案集合扩大",
                "turning_point": "外部社会后果迫使关系出现裂痕",
                "ending_hook": "下一章继续权衡",
            },
            7,
            10,
        )
    with pytest.raises(ValueError, match="导演节拍术语"):
        main.validate_director_route_role(
            {
                "goal": "把证据链与制度代价压缩至两难选项",
                "turning_point": "王绾要求立即决定",
                "ending_hook": "秦策必须答复",
            },
            7,
            10,
        )
    with pytest.raises(ValueError, match="导演节拍术语"):
        main.validate_director_route_role(
            {
                "goal": "秦策面临两个不可兼得的方案",
                "turning_point": "他放弃原有退路并承担责任",
                "ending_hook": "赵明与秦策出现信任裂痕",
            },
            7,
            10,
        )


def test_route_batch_rejects_title_image_fatigue():
    import app.main as main

    routes = [
        normalize_route(
            {
                "title": title,
                "goal": goal,
                "conflict": conflict,
                "turning_point": turn,
                "ending_hook": hook,
                "must_keep": [], "must_avoid": [],
            },
            index,
        )
        for index, (title, goal, conflict, turn, hook) in enumerate(
            [
                ("粮仓封泥", "秦策取得一枚旧封泥", "仓吏拒交", "封泥有缺", "夜车入仓"),
                ("粮仓夜车", "赵明截住一辆无籍粮车", "军吏拦路", "车底藏牍", "车夫逃走"),
                ("粮仓旧锁", "王绾扣下仓门旧锁", "县吏索锁", "锁孔留铜屑", "铜匠被召"),
                ("粮仓空瓮", "秦策发现一排空瓮", "守仓者阻拦", "瓮底有新谷", "夜里传来车声"),
            ],
            start=1,
        )
    ]
    with pytest.raises(ValueError, match="核心意象"):
        main.validate_route_batch({"chapters": routes}, [1, 2, 3, 4], [])


def test_route_goal_containment_detects_paraphrased_duplicate():
    import app.main as main

    routes = [
        normalize_route({
            "title": "算筹折损", "goal": "沈砚把模型精度从毫秒级调整为旬级并承认估算边界",
            "conflict": "旧吏反对修改口径", "turning_point": "损耗数据迫使他调整",
            "ending_hook": "新口径等待复核", "must_keep": [], "must_avoid": [],
        }, 1),
        normalize_route({
            "title": "模糊阈值", "goal": "沈砚将模型精度由毫秒级退化为旬级并接受估算误差",
            "conflict": "同僚质疑估算价值", "turning_point": "误差暴露新的风险",
            "ending_hook": "复核引出争论", "must_keep": [], "must_avoid": [],
        }, 2),
    ]
    with pytest.raises(ValueError, match="章节目标高度重复"):
        main.validate_route_batch({"chapters": routes}, [1, 2], [])


def test_assigned_turn_requires_concrete_event_anchors():
    import app.main as main

    assigned = "楚地道路标准实施后，地方工匠集体罢工，秦策意识到文化抵抗的深度。"
    unrelated = normalize_route({
        "title": "调令密匣", "goal": "旧调粮方案因密匣数据被篡改而失效",
        "conflict": "粮官拒绝交出旧调令", "turning_point": "密匣夹层露出残页",
        "ending_hook": "王绾要求复核", "must_keep": [], "must_avoid": [],
    }, 75)
    with pytest.raises(ValueError, match="未承载指定卷级转折"):
        main.validate_director_assigned_turn(unrelated, assigned)

    matching = normalize_route({
        "title": "楚道停锤", "goal": "楚地工匠以集体停工拒绝新道路尺度",
        "conflict": "秦策限期开工，匠首坚持旧尺", "turning_point": "工匠罢工使军道停筑",
        "ending_hook": "旧尺在夜市重新流通", "must_keep": [], "must_avoid": [],
    }, 75)
    main.validate_director_assigned_turn(matching, assigned)


def test_future_turn_cannot_be_consumed_early():
    import app.main as main

    route = normalize_route({
        "title": "燕地量器之争", "goal": "秦策强推统一度量",
        "conflict": "燕地长老以祭祀礼器激烈反对新尺",
        "turning_point": "长老聚众阻断量器发放，引发大规模冲突",
        "ending_hook": "宗庙闭门", "must_keep": [], "must_avoid": [],
    }, 71)
    with pytest.raises(ValueError, match="提前占用第 73 章指定转折"):
        main.validate_director_future_turns(
            route,
            [(73, "秦策在燕地推行统一度量时，遭遇当地长老的激烈反对，引发首次大规模冲突。")],
        )


def test_historical_route_rejects_gamified_percentages():
    import app.main as main

    project = default_project("historical-language", "古代故事", "now")
    project["genre"] = "历史权谋"
    route = normalize_route({
        "title": "赵地断粮", "goal": "信任维度下降30%",
        "conflict": "秦策挪用私粮", "turning_point": "粮仓见底",
        "ending_hook": "县吏闭门", "must_keep": [], "must_avoid": [],
    }, 74)
    with pytest.raises(ValueError, match="时代语言质量问题"):
        main.validate_director_route_language(project, route)


def test_historical_route_rejects_modern_system_control_language():
    import app.main as main

    project = default_project("historical-system-language", "古代故事", "now")
    project["genre"] = "历史权谋"
    route = normalize_route({
        "title": "近郊追索", "goal": "系统自动标记高危人口",
        "conflict": "秦策取得临时管控权", "turning_point": "村户焚牒",
        "ending_hook": "关吏封门", "must_keep": [], "must_avoid": [],
    }, 87)
    with pytest.raises(ValueError, match="时代语言质量问题"):
        main.validate_director_route_language(project, route)


def test_final_volume_route_hook_may_bridge_to_next_stage():
    import app.main as main

    project = default_project("stage-bridge", "古代故事", "now")
    project["production_spec"] = "- 第4卷禁入：中央文书网\n"
    main._validate_director_stage_boundary(
        project, 4, "秦策公开承担七名死者；赈济粮路继续保留"
    )
    with pytest.raises(ValueError, match="未来阶段串线"):
        main._validate_director_stage_boundary(
            project, 4, "秦策已经建立中央文书网"
        )


def test_historical_route_does_not_treat_must_avoid_as_story_language():
    import app.main as main

    project = default_project("historical-control-metadata", "古代故事", "now")
    project["genre"] = "历史权谋"
    route = normalize_route({
        "title": "工程簿署名", "goal": "秦策承担十日误期之责",
        "conflict": "李斯欲拘匠首", "turning_point": "王绾拒绝再作担保",
        "ending_hook": "齐地旧尺急报抵达", "must_keep": [],
        "must_avoid": ["复核官署", "背书"],
    }, 76)
    main.validate_director_route_language(project, route)


def test_director_discards_model_self_reported_quality_warnings():
    import app.main as main

    normalized = main.validate_director_route_structure({
        "route": {
            "title": "旧尺入市", "goal": "匠户重新使用旧尺",
            "conflict": "县吏封存量具", "turning_point": "夜市出现旧尺",
            "ending_hook": "买卖双方拒用新制", "must_keep": [], "must_avoid": [],
            "quality_warnings": ["未出现现代概念", "质量优秀"],
        }
    }, 76)
    assert normalized["quality_warnings"] == []


def test_volume_domain_requires_declared_material_and_one_core_location():
    import app.main as main

    project = default_project("domain", "分卷材料", "now")
    project["production_spec"] = (
        "- 第8卷事件词：度量、量器、道路、粮税、燕地、长老、楚地、工匠\n"
        "- 第8卷主场域词：燕地、赵国、楚地、齐国\n"
    )
    off_topic = normalize_route({
        "title": "邯郸新籍", "goal": "王绾颁行新粮籍",
        "conflict": "仓吏拒绝交册", "turning_point": "旧册被焚",
        "ending_hook": "粮车停在城外", "must_keep": [], "must_avoid": [],
    }, 71)
    with pytest.raises(ValueError, match="偏离本卷事件材料"):
        main.validate_director_volume_domain(project, 8, off_topic)

    multi_scene = normalize_route({
        "title": "燕地量器", "goal": "燕地改用新量器",
        "conflict": "楚地工匠拒造量器", "turning_point": "旧尺折断",
        "ending_hook": "驿道传来消息", "must_keep": [], "must_avoid": [],
    }, 71)
    with pytest.raises(ValueError, match="主场域过多"):
        main.validate_director_volume_domain(project, 8, multi_scene)


def test_chapter_seed_requires_authored_scene_anchors():
    import app.main as main

    seed = "占领区驿道总亭收到四种互不相容的里程木牍，粮车在同一岔路报出四个路程；秦策只取得十日勘校权。"
    wrong = normalize_route({
        "title": "燕地旧尺", "goal": "长老拒绝新量器",
        "conflict": "执法吏围住宗庙", "turning_point": "礼器被扣",
        "ending_hook": "民众聚集", "must_keep": [], "must_avoid": [],
    }, 71)
    with pytest.raises(ValueError, match="未承载作者指定章种子"):
        main.validate_director_chapter_seed(wrong, seed)

    matching = normalize_route({
        "title": "四牍一岔", "goal": "秦策取得十日勘校权",
        "conflict": "四种里程木牍使粮车堵在驿道岔路",
        "turning_point": "同一岔路被报成四个路程",
        "ending_hook": "待查路段延伸至下一亭", "must_keep": [], "must_avoid": [],
    }, 71)
    main.validate_director_chapter_seed(matching, seed)


def test_chapter_forbidden_terms_ignore_constraint_arrays_but_check_core():
    import app.main as main

    route = normalize_route({
        "title": "无名关口", "goal": "疏通一队粮车",
        "conflict": "新旧标木错开", "turning_point": "旧尺仍可赊欠",
        "ending_hook": "下一队车抵达", "must_keep": ["不得惊动赵国长老"],
        "must_avoid": [],
    }, 72)
    main.validate_director_chapter_forbidden_terms(route, ["赵国", "长老"])
    route["conflict"] = "赵国长老阻拦粮车"
    with pytest.raises(ValueError, match="作者章级禁入词"):
        main.validate_director_chapter_forbidden_terms(route, ["赵国", "长老"])

    route["conflict"] = "新旧标木错开"
    route["ending_hook"] = "县吏前去封门"
    main.validate_director_chapter_forbidden_terms(
        route, ["封门"], include_hook=False
    )


def test_route_planner_collects_routes_from_all_earlier_volumes():
    import app.main as main

    project = default_project("route-history", "全书路线历史", "now")
    project["planning"] = normalize_master_plan(
        {
            "volumes": [
                {"title": "卷一", "chapter_count": 2},
                {"title": "卷二", "chapter_count": 2},
            ]
        },
        4,
    )
    first, second = project["planning"]["volumes"]
    first["chapters"] = [
        normalize_route({"title": "旧事件一", "goal": "改变资源状态一"}, 1),
        normalize_route({"title": "旧事件二", "goal": "改变关系状态二"}, 2),
    ]
    assert [
        item["title"] for item in main._director_routes_before_volume(project, second)
    ] == ["旧事件一", "旧事件二"]


def test_checkpoint_reaudit_stops_before_invalid_historical_language():
    import app.main as main

    project = default_project("p", "历史书", "now")
    project["genre"] = "战国历史架空"
    volume = {
        "chapter_start": 1, "chapter_end": 3, "goal": "取得复核资格",
        "ending_state": "获准试行", "bridge_to_next": "试行引来反制",
    }
    routes = [
        normalize_route({
            "title": "仓门旧牍", "goal": "沈砚取得一卷可供比对的旧牍",
            "conflict": "仓吏拒绝交付", "turning_point": "木牍封泥有异",
            "ending_hook": "另一册账目失踪", "must_keep": [], "must_avoid": [],
        }, 1),
        normalize_route({
            "title": "算法黑盒", "goal": "沈砚用算法修正粮册的统计误差",
            "conflict": "众人不理解算法", "turning_point": "算法得到验证",
            "ending_hook": "准备推广数据库", "must_keep": [], "must_avoid": [],
        }, 2),
    ]
    valid_count, issue = main._audit_route_checkpoint_prefix(project, volume, routes)
    assert valid_count == 1
    assert "时代语言" in issue


def test_route_planner_repairs_soft_quality_failure_without_pausing(monkeypatch):
    import json
    import app.main as main

    project = default_project("p", "历史书", "now")
    project["genre"] = "战国历史架空"
    volume = {
        "chapter_start": 1, "chapter_end": 3, "title": "试行之门",
        "goal": "取得有限试行资格", "conflict": "旧吏阻止复核",
        "synopsis": "沈砚先取得旧牍，再发现反常证据，最终争取有限复核资格。",
        "ending_state": "获准有限试行", "bridge_to_next": "试行引来反制",
        "turning_points": [], "character_arcs": [], "must_keep": [], "must_avoid": [],
    }
    previous = [normalize_route({
        "title": "仓门旧牍", "goal": "沈砚取得一卷可供比对的旧牍并确认封泥异常",
        "conflict": "仓吏拒绝交付", "turning_point": "木牍封泥有异",
        "ending_hook": "另一册账目失踪", "must_keep": [], "must_avoid": [],
    }, 1)]
    candidates = [
        {"route": {
            "title": "旧牍再验", "goal": "沈砚取得旧牍并再次确认封泥异常",
            "conflict": "仓吏继续拒绝交付", "turning_point": "木牍封泥仍有异",
            "ending_hook": "继续寻找另一册", "must_keep": [], "must_avoid": [],
        }},
        {"route": {
            "title": "空仓回声", "goal": "失踪粮袋迫使沈砚放弃原先的损耗解释并转查夜间转运",
            "conflict": "车夫证词与仓门记录互相矛盾", "turning_point": "车辙表明粮车出城后折返",
            "ending_hook": "唯一知情车夫在复核前失踪", "must_keep": [], "must_avoid": [],
        }},
    ]
    calls = 0

    async def fake_chat(*_args, **_kwargs):
        nonlocal calls
        result = candidates[min(calls, 1)]
        calls += 1
        return json.dumps(result, ensure_ascii=False)

    monkeypatch.setattr(main, "chat_once", fake_chat)
    route, warnings = asyncio.run(
        main.director_plan_chapter_route(project, volume, 2, previous)
    )
    assert calls == 2
    assert route["title"] == "空仓回声"
    assert "第 2 轮" in warnings[0]
    assert not route.get("quality_warnings")


def test_route_planner_rejects_persistent_structural_duplicate(monkeypatch):
    import json
    import app.main as main

    project = default_project("p", "测试", "now")
    volume = {
        "chapter_start": 1, "chapter_end": 3, "title": "试行之门",
        "goal": "取得有限试行资格", "conflict": "旧吏阻止复核",
        "synopsis": "三章完成一次有限复核。", "ending_state": "获准有限试行",
        "bridge_to_next": "试行引来反制", "turning_points": [],
        "character_arcs": [], "must_keep": [], "must_avoid": [],
    }
    previous = [normalize_route({
        "title": "仓门旧牍", "goal": "沈砚取得旧牍并再次确认封泥异常与账目缺口",
        "conflict": "仓吏拒绝交付", "turning_point": "木牍封泥有异",
        "ending_hook": "另一册账目失踪", "must_keep": [], "must_avoid": [],
    }, 1)]
    rejected = {"route": {
        "title": "旧牍复核", "goal": "沈砚再次取得旧牍并确认封泥异常与账目缺口",
        "conflict": "仓吏继续拒绝交付", "turning_point": "木牍封泥仍有异",
        "ending_hook": "继续寻找另一册", "must_keep": [], "must_avoid": [],
    }}
    calls = 0

    async def always_complete_but_repeated(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return json.dumps(rejected, ensure_ascii=False)

    monkeypatch.setattr(main, "chat_once", always_complete_but_repeated)
    with pytest.raises(ValueError, match="不可接受的结构重复"):
        asyncio.run(main.director_plan_chapter_route(project, volume, 2, previous))
    assert calls == 6


def test_route_planner_raises_only_when_no_complete_structure_exists(monkeypatch):
    import app.main as main

    project = default_project("p", "测试", "now")
    volume = {
        "chapter_start": 1, "chapter_end": 1, "title": "独章",
        "goal": "完成选择", "conflict": "两难", "synopsis": "独章故事",
        "ending_state": "作出选择", "bridge_to_next": "", "turning_points": [],
        "character_arcs": [], "must_keep": [], "must_avoid": [],
    }
    calls = 0

    async def malformed(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return '{"route":{"title":"未闭合"'

    monkeypatch.setattr(main, "chat_once", malformed)
    with pytest.raises(ValueError, match="JSON 未闭合"):
        asyncio.run(main.director_plan_chapter_route(project, volume, 1, []))
    assert calls == 6


def test_planning_debt_updates_same_stage_chapter_instead_of_duplicating():
    import app.main as main

    task = {"planning_debts": []}
    main._record_planning_debt(task, {
        "phase": "route", "volume_id": "v1", "chapter": 3,
        "title": "旧标题", "issues": ["旧问题"],
    })
    main._record_planning_debt(task, {
        "phase": "route", "volume_id": "v1", "chapter": 3,
        "title": "新标题", "issues": ["新问题"],
    })
    assert len(task["planning_debts"]) == 1
    assert task["planning_debts"][0]["title"] == "新标题"


def test_manual_save_is_blocked_while_director_is_running(monkeypatch, tmp_path):
    import app.main as main

    director_store = ProjectStore(tmp_path / "lock.db")
    monkeypatch.setattr(main, "store", director_store)
    project = director_store.create("锁定作品")
    task = director_store.create_director_task(
        project["id"], {"phase": "chapters", "message": "运行中", "config": {}}
    )
    task["status"] = "running"
    director_store.save_director_task(task["id"], task)

    response = TestClient(main.app).put(
        f"/api/projects/{project['id']}", json=project
    )
    assert response.status_code == 409
    assert "请先暂停任务" in response.json()["detail"]


def test_manuscript_gate_adds_release_checks_only_at_final_checkpoint():
    import app.main as main

    health = {
        "score": 78,
        "character_count": 120_000,
        "duplicate_titles": [],
        "duplicate_passage_count": 0,
        "similar_chapters": [],
        "volume_progression_issues": [],
        "memory_integrity_issues": [{"category": "线索债务"}],
        "modern_jargon": [{"term": "模型", "count": 90}],
    }

    assert main._director_manuscript_gate_failures(health, final=False) == []
    failures = main._director_manuscript_gate_failures(health, final=True)
    assert any("健康分" in item for item in failures)
    assert any("记忆或线索" in item for item in failures)
    assert any("现代抽象术语" in item for item in failures)


def test_manuscript_gate_always_stops_structural_repetition():
    import app.main as main

    failures = main._director_manuscript_gate_failures(
        {
            "score": 96,
            "character_count": 20_000,
            "duplicate_titles": [{"title": "同名章"}],
            "duplicate_passage_count": 1,
            "similar_chapters": [{"left": 1, "right": 2}],
            "volume_progression_issues": [{"left": 1, "right": 2}],
            "memory_integrity_issues": [],
            "modern_jargon": [],
        }
    )
    assert len(failures) == 4


def test_volume_core_retries_with_targeted_length_repair(monkeypatch):
    import app.main as main

    project = default_project("volume-repair", "分卷修复", "now")
    project["book_rules"] = "梗概必须形成完整因果链"
    spec = {"number": 1, "chapter_start": 1, "chapter_end": 10, "chapter_count": 10}
    contract = {
        "number": 1,
        "title": "函谷三日",
        "goal": "获得限期越级调粮权",
        "conflict": "断粮与权责冲突",
        "ending_state": "王命第一次绕过丞相府",
        "bridge_to_next": "调粮权触发名籍核验",
        "theme_test": "效率是否足以正当化牺牲",
        "primary_arena": "函谷关与仓曹",
        "time_span": "三日",
        "irreversible_change": "越级权成为事实",
        "character_choice": "秦策放弃偏师",
        "new_story_question": "越级权如何不成为夺权工具",
    }
    next_contract = {
        **contract,
        "number": 2,
        "title": "名籍之外",
        "goal": "隐户全面核验与户籍粮籍联动",
    }
    next_spec = {
        "number": 2, "chapter_start": 11, "chapter_end": 20,
        "chapter_count": 10,
    }
    calls = []

    async def fake_structured(_settings, messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            raise ValueError("分卷剧情核心梗概至少需要 180 字，实际 166 字")
        return {
            "volume_core": {
                "title": "函谷三日",
                "goal": "获得限期越级调粮权",
                "conflict": "断粮与权责冲突",
                "synopsis": "具体行动、连续升级、人物选择、可见代价与卷末结果。" * 12,
                "ending_state": "王命第一次绕过丞相府",
                "bridge_to_next": "调粮权触发名籍核验",
            }
        }, []

    monkeypatch.setattr(main, "structured_completion", fake_structured)
    core, warnings = asyncio.run(
        main.director_master_volume_core(
            project,
            {"theme": "秩序与代价"},
            [contract, next_contract],
            [spec, next_spec],
            0,
            [],
        )
    )
    assert len(calls) == 2
    assert "隐户全面核验" not in calls[0][1]["content"]
    assert "220—300" in calls[1][-1]["content"]
    assert core["title"] == "函谷三日"
    assert warnings and "定向修复" in warnings[0]


def test_author_stage_guard_rejects_future_volume_leakage():
    import app.main as main

    project = default_project("stage-guard", "阶段门禁", "now")
    project["production_spec"] = (
        "## 分卷阶段防串线（机器门禁）\n"
        "- 第1卷禁入：隐户、河灾、全国名籍\n"
        "- 第2卷禁入：河灾；全国名籍\n"
    )
    assert main._director_stage_forbidden_terms(project, 1) == [
        "隐户", "河灾", "全国名籍"
    ]
    main._validate_director_stage_boundary(project, 1, "秦策核对函谷仓粮")
    with pytest.raises(ValueError, match="未来阶段串线"):
        main._validate_director_stage_boundary(
            project, 1, "第三章提前调查咸阳隐户"
        )


def test_volume_details_retry_does_not_expose_future_character_arcs(monkeypatch):
    import app.main as main

    project = default_project("details-repair", "清单修复", "now")
    project["production_spec"] = "- 第1卷禁入：隐户、河灾"
    spec = {"number": 1, "chapter_start": 1, "chapter_end": 10, "chapter_count": 10}
    contract = {
        "number": 1, "title": "函谷三日", "goal": "获得限期调粮权",
        "conflict": "断粮与权责冲突", "ending_state": "王命越过丞相府",
        "bridge_to_next": "越级权引发新的治理问题", "theme_test": "救多数的代价",
        "primary_arena": "函谷关与仓曹", "time_span": "三日",
        "irreversible_change": "越级调粮成为事实", "character_choice": "放弃偏师",
        "new_story_question": "越级权如何受约束",
    }
    core = {
        "title": "函谷三日", "goal": contract["goal"], "conflict": contract["conflict"],
        "synopsis": "秦策核验简牍并在三日内调动仓粮，最终放弃偏师换取有限权力。" * 8,
        "ending_state": contract["ending_state"], "bridge_to_next": contract["bridge_to_next"],
    }
    calls = []

    async def fake_structured(_settings, messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            raise ValueError("第 1 卷发生未来阶段串线：提前使用 隐户")
        return {"volume_details": {
            "turning_points": ["三份简牍互相冲突", "偏师失去救援", "王命授予限权"],
            "character_arcs": ["秦策由求全转为承担取舍", "王绾开始追问责任归属"],
            "subplots": ["仓吏隐瞒损耗"], "must_keep": ["三日时限"],
            "must_avoid": ["不得提前展开后卷"],
        }}, []

    monkeypatch.setattr(main, "structured_completion", fake_structured)
    details, warnings = asyncio.run(
        main.director_master_volume_details(
            project,
            {"theme": "秩序与代价", "major_character_arcs": ["秦策调查隐户"]},
            [contract], [spec], 0, core,
        )
    )
    assert len(calls) == 2
    assert "秦策调查隐户" not in calls[0][1]["content"]
    assert details["turning_points"]
    assert warnings and "定向重构" in warnings[0]


def test_prior_volume_regression_detects_cluster_but_allows_current_route_terms():
    import app.main as main

    project = default_project("volume-regression", "分卷回流门禁", "now")
    project["characters"] = [{"id": "c1", "name": "秦策"}]
    prior_route = {
        "title": "主营核验",
        "goal": "核对主营与粮车",
        "conflict": "偏师去向不明",
        "turning_point": "查清北坡粮车路径",
        "ending_hook": "放弃偏师",
        "must_keep": ["主营", "偏师", "粮车", "北坡"],
        "must_avoid": [],
    }
    current_route = {
        "title": "灞水隐户",
        "goal": "决定是否保留名籍空行",
        "conflict": "徭役与活命相冲突",
        "turning_point": "王绾扣下抽页",
        "ending_hook": "秦策在页背署名",
        "must_keep": ["灞水南岸", "抽页"],
        "must_avoid": ["不得公开揭发"],
    }
    project["planning"]["volumes"] = [
        {
            "id": "v1", "chapter_start": 1, "chapter_end": 2,
            "chapters": [dict(prior_route) for _ in range(5)],
        },
        {
            "id": "v2", "chapter_start": 3, "chapter_end": 4,
            "chapters": [dict(current_route), dict(current_route)],
        },
    ]
    chapter = {"number": 3, "volume_id": "v2", "route": current_route}
    bad = "秦策忽然重新核对主营，又追查偏师、粮车与北坡旧路。"
    issues = main._prior_volume_regression_issues(project, chapter, bad)
    assert issues and issues[0]["category"] == "前卷语义回流"

    # A concept that has already been used in an accepted chapter of the
    # current volume remains valid even when it is not in the current route.
    bridged_chapter = {"id": "current", "number": 5, "volume_id": "v2", "route": current_route}
    project["planning"]["volumes"][1]["chapter_end"] = 5
    project["chapters"] = [
        {"id": "v2-start", "number": 3, "volume_id": "v2", "content": bad},
        {"id": "v2-middle", "number": 4, "volume_id": "v2", "content": "王绾继续核对抽页。"},
        bridged_chapter,
    ]
    assert main._prior_volume_regression_issues(project, bridged_chapter, bad) == []

    numeric_bridge = "两日口粮、九十户和自己的粮与农具都要带走。"
    assert main._prior_volume_regression_issues(project, bridged_chapter, numeric_bridge) == []

    generic_court_language = "核验原牍后，有人认为应当交出旧印；国家不能扣住新的急报。"
    assert (
        main._prior_volume_regression_issues(
            project, bridged_chapter, generic_court_language
        )
        == []
    )

    generic_transit_language = "秦策在案前复算，车全部改走石梁，却没有立刻回咸阳改写名册。"
    assert (
        main._prior_volume_regression_issues(
            project, bridged_chapter, generic_transit_language
        )
        == []
    )

    project["planning"]["volumes"][1]["chapters"] = [
        {**current_route, "must_keep": ["主营", "偏师", "粮车", "北坡"]},
        {**current_route, "must_keep": ["主营", "偏师", "粮车", "北坡"]},
    ]
    assert main._prior_volume_regression_issues(project, chapter, bad) == []

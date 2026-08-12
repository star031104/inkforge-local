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
    result = {
        "summary": "沈砚核对粮册。",
        "character_updates": [{"name": "不存在的人", "state": "受伤"}],
        "facts": [{"text": "粮册已经封存", "importance": 99}],
        "plot_threads": [{"title": "谁改了账", "status": "invalid", "latest": "留下墨痕"}],
        "timeline": [],
        "continuity_notes": [],
    }
    warnings = main._apply_director_memory(project, chapter, result)
    assert len(warnings) == 2
    assert project["memory"]["facts"][0]["importance"] == 5
    assert project["memory"]["plot_threads"][0]["status"] == "open"
    assert chapter["execution"]["warnings"] == warnings


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
            "conflict": "旧吏拒绝交册", "turning_point": "发现墨迹不同",
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


def test_route_planner_keeps_best_complete_candidate_as_quality_debt(monkeypatch):
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
    route, warnings = asyncio.run(
        main.director_plan_chapter_route(project, volume, 2, previous)
    )
    assert calls == 4
    assert route["title"] == "旧牍复核"
    assert route["quality_warnings"]
    assert "规划质量债务" in warnings[0]


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
    assert calls == 4


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

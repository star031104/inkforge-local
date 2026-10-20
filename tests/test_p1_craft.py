"""P1 prose-quality features: anti-slop, best-of-n, rehearsal, focused critics,
stable prefix, constrained decoding, item/injury state, rolling re-planning."""
from __future__ import annotations

import asyncio
import json

import httpx

from app.core.model_budget import estimate_tokens
from app.db import ProjectStore, ensure_project_defaults
from app.domain.continuity_state import (
    apply_continuity_updates,
    check_continuity_draft,
    item_states,
    render_continuity_state,
)
from app.domain.json_schemas import AUDIT_SCHEMA, memory_stage_schema
from app.evidence_audit import number_draft
from app.prompts import AUDIT_PROMPT, build_prompt
from app.providers import build_chat_payload
from app.services import antislop, rolling_replan as replan_module, structured_output
from app.services.antislop import (
    BannedPhrase,
    banned_phrases,
    detect_slop,
    llama_cpp_logit_bias,
    rewrite_slop,
)
from app.services.candidate_selection import (
    candidate_count_for,
    deterministic_score,
    select_best_candidate,
)
from app.services.craft_settings import craft_setting
from app.services.dialogue_rehearsal import leaked_secret, rehearsal_cast, rehearse_dialogue
from app.services.focused_critics import CHECKERS, estimate_focused_tokens, run_focused_critics
from app.services.memory_extraction import MEMORY_STAGES
from app.services.rolling_replan import replan_targets, rolling_replan


def run(coro):
    return asyncio.run(coro)


def project_fixture(tmp_path, chapters: int = 3) -> dict:
    store = ProjectStore(tmp_path / "craft.db")
    project = ensure_project_defaults(store.create("测试"))
    while len(project["chapters"]) < chapters:
        project["chapters"].append(
            {"id": f"c{len(project['chapters']) + 1}", "title": f"第{len(project['chapters']) + 1}章",
             "summary": "", "content": "", "scene_goal": "", "plan": {}}
        )
    return project


# ---------------------------------------------------------------- settings

def test_craft_settings_defaults_and_bounds():
    assert craft_setting({}, "critic_mode") == "focused"
    assert craft_setting({"settings": {"candidate_count": 9}}, "candidate_count") == 3
    assert craft_setting({"settings": {"rolling_replan": "off"}}, "rolling_replan") is False
    assert craft_setting({"settings": {}}, "candidate_count", {"candidate_count": 2}) == 2


# ---------------------------------------------------------------- anti-slop

def test_banned_phrase_allowances_and_detection():
    project = {
        "governance": {"banned_phrases": ["命运的齿轮"]},
        "memory": {"learned_fatigue_phrases": [{"phrase": "周砚没有说话"}]},
    }
    phrases = {item.phrase: item for item in banned_phrases(project)}
    assert phrases["命运的齿轮"].allow == 0 and phrases["命运的齿轮"].source == "author"
    assert phrases["深吸一口气"].allow == 1
    assert phrases["周砚没有说话"].source == "learned"
    text = "他深吸一口气。门开了。她也深吸一口气，走了进去。命运的齿轮开始转动。"
    hits = {item["phrase"]: item for item in detect_slop(text, list(phrases.values()))}
    assert hits["深吸一口气"]["count"] == 2 and hits["深吸一口气"]["sentence_indexes"] == [2]
    assert hits["命运的齿轮"]["sentence_indexes"] == [3]


def test_rewrite_slop_only_touches_offending_sentences():
    phrases = [BannedPhrase("深吸一口气", 1, "generic"), BannedPhrase("命运的齿轮", 0, "author")]
    text = "他深吸一口气。门开了。\n她也深吸一口气，走了进去。命运的齿轮开始转动。"
    seen = {}

    async def completion(settings, messages, **kwargs):
        seen["user"] = messages[-1]["content"]
        seen["schema"] = kwargs.get("schema")
        return {"rewrites": [
            {"id": "S1", "text": "她攥紧袖口，走了进去。"},
            {"id": "S2", "text": "命运的齿轮仍在转动。"},  # still contains slop → skipped
        ]}, []

    revised, report = run(rewrite_slop(text, phrases, settings={}, completion=completion))
    assert revised == "他深吸一口气。门开了。\n她攥紧袖口，走了进去。命运的齿轮开始转动。"
    assert len(report["rewritten"]) == 1 and report["skipped"][0]["reason"] == "改写仍含套话"
    assert "门开了" in seen["user"]  # neighbouring sentence is sent as context
    assert seen["schema"]["required"] == ["rewrites"]


def test_rewrite_slop_rejects_label_echo_and_broken_quotes():
    phrases = [BannedPhrase("嘴角勾起", 0, "author")]
    text = "门开了。顾川嘴角勾起，却没有笑意：“你要去？”她没回答。林晚嘴角勾起。"

    async def completion(settings, messages, **kwargs):
        return {"rewrites": [
            {"id": "S1", "text": "顾川扯了扯嘴角，却没有笑意：“你要去？"},  # drops the closing quote
            {"id": "S2", "text": "需去除：林晚抿了抿唇。"},  # echoes the prompt label
        ]}, []

    revised, report = run(rewrite_slop(text, phrases, settings={}, completion=completion))
    assert revised == text
    reasons = sorted(item["reason"] for item in report["skipped"])
    assert reasons == ["改写夹带了提示词标签", "改写破坏了引号或括号配对"]

    async def good(settings, messages, **kwargs):
        return {"rewrites": [
            {"id": "S1", "text": "顾川扯了扯嘴角，却没有笑意：“你要去？”"},
            {"id": "S2", "text": "目标句：林晚抿了抿唇。"},  # label prefix is stripped, not rejected
        ]}, []

    revised, _ = run(rewrite_slop(text, phrases, settings={}, completion=good))
    assert revised == "门开了。顾川扯了扯嘴角，却没有笑意：“你要去？”她没回答。林晚抿了抿唇。"


def test_rewrite_failure_keeps_original_text():
    async def broken(*_args, **_kwargs):
        raise RuntimeError("down")

    text = "他深吸一口气。她深吸一口气。"
    revised, report = run(
        rewrite_slop(text, [BannedPhrase("深吸一口气", 1, "generic")], settings={}, completion=broken)
    )
    assert revised == text and report["warnings"]


def test_llama_cpp_logit_bias_skips_common_single_characters():
    antislop._TOKEN_CACHE.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/tokenize"
        return httpx.Response(200, json={"tokens": [
            {"id": 11, "piece": "深吸"}, {"id": 12, "piece": "一"}, {"id": 13, "piece": "口气"},
        ]})

    settings = {"base_url": "http://127.0.0.1:8080"}
    pairs = run(llama_cpp_logit_bias(
        settings, [BannedPhrase("深吸一口气", 1, "generic")], transport=httpx.MockTransport(handler)
    ))
    assert pairs == [[11, -2.5]]  # "口气" consists of common pieces only
    generic = run(llama_cpp_logit_bias({"base_url": "https://api.example.com/v1"}, [BannedPhrase("深吸一口气", 1, "g")]))
    assert generic == []


# ---------------------------------------------------------------- best-of-n

def test_candidate_count_applies_to_key_chapters_only(tmp_path):
    project = project_fixture(tmp_path, 5)
    project["settings"]["candidate_count"] = 2
    chapters = project["chapters"]
    chapters[2]["route"] = {"goal": "日常", "stage": "铺垫"}
    chapters[3]["route"] = {"goal": "决战前夜", "turning_point": "卷末高潮"}
    assert candidate_count_for(project, chapters[0], 0) == 2  # opening
    assert candidate_count_for(project, chapters[2], 2) == 1
    assert candidate_count_for(project, chapters[3], 3) == 2
    project["settings"]["candidate_scope"] = "all"
    assert candidate_count_for(project, chapters[2], 2) == 2


def test_select_best_candidate_uses_pairwise_judge_and_handles_swap():
    good = "他推开门，雨声灌进来。" * 20 + "她终于开口。"
    other = "他深吸一口气。" * 10 + "她终于开口。"
    calls = []

    async def judge(settings, messages, **kwargs):
        user = messages[-1]["content"]
        calls.append(kwargs.get("schema"))
        first = user.split("【候选A】")[1].split("【候选B】")[0]
        return {"winner": "A" if "雨声" in first else "B", "reasons": ["更具体"]}, []

    result = run(select_best_candidate(
        [other, good], project={}, target_chars=200, plan_brief="目标：开门",
        settings={}, completion=judge,
    ))
    assert result["text"] == good and result["report"]["matches"][0]["reasons"] == ["更具体"]
    assert calls[0]["required"] == ["winner", "reasons"]
    assert deterministic_score(other, 200, {})["slop_excess"] == 9


def test_pairwise_reasons_name_the_real_candidate_not_the_shuffled_label():
    good = "他推开门，雨声灌进来。" * 20 + "她终于开口。"
    other = "他站在门口，没有说话。" * 20 + "她终于开口。"

    async def judge(settings, messages, **kwargs):
        first = messages[-1]["content"].split("【候选A】")[1].split("【候选B】")[0]
        label = "A" if "雨声" in first else "B"
        return {"winner": label, "scores": {label: 40}, "reasons": [f"{label}稿的因果推进更清楚"]}, []

    result = run(select_best_candidate(
        [other, good], project={}, target_chars=200, plan_brief="", settings={}, completion=judge,
    ))
    match = result["report"]["matches"][0]
    assert result["index"] == 1
    assert match["reasons"] == ["第2份的因果推进更清楚"]
    assert match["scores"] == {"第2份": 40}


# ---------------------------------------------------------------- rehearsal

def rehearsal_project(tmp_path) -> dict:
    project = project_fixture(tmp_path, 2)
    project["characters"] = [
        {"name": "周砚", "importance": "main", "personality": "冷静", "voice": "短句", "active": True},
        {"name": "李斯", "importance": "main", "personality": "谨慎", "voice": "迂回",
         "secrets": "李斯暗中与赵高结盟谋夺兵符", "active": True},
    ]
    project["chapters"][1]["route"] = {"goal": "周砚向李斯求证粮册", "conflict": "李斯回避"}
    return project


def test_rehearsal_cast_and_secret_guard(tmp_path):
    project = rehearsal_project(tmp_path)
    cast = rehearsal_cast(project, project["chapters"][1])
    assert [item["name"] for item in cast] == ["周砚", "李斯"]
    assert leaked_secret("你与赵高结盟谋夺兵符的事我都知道", ["李斯暗中与赵高结盟谋夺兵符"], "粮册")
    assert not leaked_secret("粮册上的数字不对", ["李斯暗中与赵高结盟谋夺兵符"], "粮册")


def test_rehearse_dialogue_isolates_knowledge_and_drops_leaks(tmp_path):
    project = rehearsal_project(tmp_path)
    prompts = []

    async def completion(settings, messages, **kwargs):
        user = messages[-1]["content"]
        prompts.append(user)
        if "【你是】周砚" in user:
            return {"intent": "拿到实话", "lines": ["账册少了三车粮。", "你与赵高结盟谋夺兵符吧？"],
                    "actions": ["把账册推过去"]}, []
        return {"intent": "拖延", "lines": ["此事改日再议。"], "actions": []}, []

    report = run(rehearse_dialogue(project, 1, {}, completion))
    assert report["cast"] == ["周砚", "李斯"]
    assert report["dropped"] and report["dropped"][0]["character"] == "周砚"
    assert "结盟" not in report["material"] and "账册少了三车粮" in report["material"]
    assert "李斯暗中与赵高结盟" not in prompts[0]  # Zhou's call never sees Li's secret


def test_rehearsal_material_reaches_prose_prompt(tmp_path):
    project = rehearsal_project(tmp_path)
    build = build_prompt(project, {
        "chapter_id": project["chapters"][1]["id"], "mode": "instruction",
        "_rehearsal_material": "【周砚】意图：拿到实话\n可能的台词：“账册少了三车粮。”" * 3,
    })
    assert any(section["name"] == "对白演出素材" for section in build.sections)


# ---------------------------------------------------------------- focused critics

def realistic_blocks() -> dict[str, str]:
    return {
        "characters": json.dumps([{"name": f"人物{i}", "state": "状态" * 60} for i in range(12)], ensure_ascii=False),
        "epistemic": "知情边界" * 1800,
        "relationships": "关系" * 2500,
        "facts": "历史事实" * 1600,
        "continuity_state": "道具状态" * 100,
        "description_ledger": "描写" * 2500,
        "narrative": "第三人称限知" * 40,
        "existing_tail": "上一章结尾" * 1000,
        "route": "路线" * 800,
        "threads": "伏笔议程" * 1800,
        "rules": "规则" * 2500,
        "world": "世界书" * 600,
    }


def test_focused_critics_use_fewer_tokens_than_single_critic():
    blocks = realistic_blocks()
    draft = number_draft("他推开门，雨声灌进来。她没有回头。\n" * 60)
    old_caps = {"rules": 5000, "characters": 8000, "epistemic": 7000, "facts": 6000,
                "threads": 7000, "relationships": 5000, "description_ledger": 5000,
                "existing_tail": 5000, "route": 3000}
    single = AUDIT_PROMPT + "\n".join(blocks[key][:cap] for key, cap in old_caps.items()) + draft
    assert estimate_focused_tokens(blocks, draft) < estimate_tokens(single)


def test_focused_critics_merge_categories_and_share_prefix():
    blocks = {"characters": "周砚：冷静", "rules": "不得复活死者", "facts": "第1章：粮仓失火"}
    seen = []

    async def completion(settings, messages, **kwargs):
        seen.append(messages)
        focus = messages[-1]["content"]
        if "世界规则" in focus:
            return {"issues": [{"severity": "high", "message": "死者复活", "evidence_ids": ["P1-S1"]}]}, []
        return {"issues": []}, []

    result, _warnings = run(run_focused_critics({}, blocks, "[P1-S1]他活了。", completion=completion))
    assert result["verdict"] == "revise" and result["issues"][0]["category"].startswith("世界规则")
    assert result["score"] == 82
    # system + draft are identical across checkers (cacheable prefix)
    assert len({json.dumps(item[:2], ensure_ascii=False) for item in seen}) == 1
    assert len(seen) == len(CHECKERS)


def test_focused_critics_verify_prior_issues_on_a_revision():
    blocks = {"characters": "周砚：冷静", "rules": "不得复活死者", "facts": "第1章：粮仓失火"}
    calls = []

    async def completion(settings, messages, **kwargs):
        focus = messages[-1]["content"]
        calls.append(focus)
        if "世界规则" in focus and len(calls) < 6:
            return {"issues": [{"severity": "high", "message": "死者复活", "evidence_ids": ["P1-S1"]}]}, []
        return {"issues": []}, []

    draft = "周砚推开仓门，焦黑的梁木还在冒烟。" * 20
    key = "test-project:adaptive-chapter"
    first, _ = run(run_focused_critics({}, blocks, number_draft(draft), completion=completion,
                                       audit_key=key, draft=draft))
    full_round = len(calls)
    assert first["adaptive_rerun"] is None and full_round >= 3
    revised = draft.replace("还在冒烟", "已经冷了", 1)
    second, _ = run(run_focused_critics({}, blocks, number_draft(revised), completion=completion,
                                        audit_key=key, draft=revised))
    # v0.35: one verification call + one check of the changed sentence
    assert second["audit_mode"] == "verify" and second["prior_issue_ids"] == ["I1"]
    assert len(calls) == full_round + 2
    assert [item["issue_id"] for item in second["issues"]] == ["I1"]  # unanswered → still open
    # an unrelated new draft gets a full audit again
    other = "林晚收到匿名信，信纸泛黄。" * 20
    third, _ = run(run_focused_critics({}, blocks, number_draft(other), completion=completion,
                                       audit_key=key, draft=other))
    assert third["audit_mode"] == "full" and third["adaptive_rerun"] is None


# ---------------------------------------------------------------- stable prefix

def test_prose_prompt_system_message_is_stable_within_a_book(tmp_path):
    project = project_fixture(tmp_path, 2)
    project["book_rules"] = "不得复活死者。"
    project["world_entries"] = [{"title": "兵符", "keys": ["兵符"], "content": "兵符需两半合一", "enabled": True}]
    chapter_id = project["chapters"][1]["id"]
    first = build_prompt(project, {"chapter_id": chapter_id, "instruction": "写兵符交接"})
    project["chapters"][1]["content"] = "雨下了一夜。"
    second = build_prompt(project, {"chapter_id": chapter_id, "instruction": "继续"})
    assert first.messages[0]["role"] == "system"
    assert first.messages[0]["content"] == second.messages[0]["content"]
    assert "兵符需两半合一" not in first.messages[0]["content"]


# ---------------------------------------------------------------- constrained decoding

def test_structured_completion_sends_json_schema_to_llama_cpp(monkeypatch):
    sent = {}

    async def fake_chat_once(settings, messages, **kwargs):
        sent["payload"] = build_chat_payload(
            settings, messages, stream=True, json_mode=True, options=kwargs.get("options")
        )
        return json.dumps({"score": 90, "verdict": "pass", "issues": [], "strengths": [], "revision_brief": ""})

    monkeypatch.setattr(structured_output, "chat_once", fake_chat_once)
    settings = {"base_url": "http://127.0.0.1:8080", "model": "local", "context_budget": 32000}
    run(structured_output.structured_completion(
        settings, [{"role": "user", "content": "审计"}], max_tokens=200,
        timeout_seconds=5, temperature=0.1, workload="critic", schema=AUDIT_SCHEMA,
    ))
    assert sent["payload"]["json_schema"] == AUDIT_SCHEMA
    generic = {"base_url": "https://api.example.com/v1", "model": "m", "context_budget": 32000}
    run(structured_output.structured_completion(
        generic, [{"role": "user", "content": "审计"}], max_tokens=200,
        timeout_seconds=5, temperature=0.1, workload="critic", schema=AUDIT_SCHEMA,
    ))
    assert sent["payload"]["response_format"] == {"type": "json_object"}


def test_every_memory_stage_has_a_schema_and_objects_stage_exists():
    names = [stage.name for stage in MEMORY_STAGES]
    assert "objects" in names
    schema = memory_stage_schema(("item_updates", "injury_updates"))
    assert schema["properties"]["item_updates"]["type"] == "array"


# ---------------------------------------------------------------- item / injury state

def test_item_and_injury_state_machine_flags_contradictions(tmp_path):
    project = project_fixture(tmp_path, 4)
    memory = project["memory"]
    text = "青锋剑在激战中折断，碎片落入江中。周砚左臂中了一刀，血流不止。"
    project["chapters"][0]["content"] = text

    def verify(evidence):
        evidence = str(evidence or "")
        return evidence, bool(evidence) and evidence in text

    warnings = apply_continuity_updates(memory, project["chapters"][0], 1, {
        "item_updates": [
            {"item": "青锋剑", "state": "destroyed", "evidence": "青锋剑在激战中折断"},
            {"item": "玉佩", "state": "lost", "evidence": "并不存在的证据"},
        ],
        "injury_updates": [
            {"character": "周砚", "injury": "左臂刀伤", "state": "fresh", "severity": "severe", "evidence": "周砚左臂中了一刀"},
        ],
    }, verify)
    assert len(warnings) == 1 and "玉佩" in warnings[0]
    assert item_states(memory, 2)["青锋剑"]["state"] == "destroyed"
    assert item_states(memory, 1) == {}  # chapter-relative: not yet happened
    assert "已毁坏" in render_continuity_state(project, 1)
    issues = check_continuity_draft(project, 1, "周砚拔出青锋剑指向来人。他的伤口已经痊愈，行动如常。")
    categories = {item["category"] for item in issues}
    assert categories == {"道具连续性", "伤势连续性"}
    recovered = check_continuity_draft(project, 1, "工匠重铸了青锋剑，周砚握着青锋剑出门。")
    assert not [item for item in recovered if item["category"] == "道具连续性"]
    # re-settling the same chapter replaces, not duplicates, its events
    apply_continuity_updates(memory, project["chapters"][0], 1, {"item_updates": []}, verify)
    assert memory["item_events"] == []


# ---------------------------------------------------------------- rolling replan

def replan_project(tmp_path) -> dict:
    project = project_fixture(tmp_path, 5)
    volume = {"id": "v1", "title": "第一卷", "chapter_start": 1, "chapter_end": 5, "goal": "夺回粮仓",
              "chapters": []}
    for number, chapter in enumerate(project["chapters"], start=1):
        route = {"id": f"r{number}", "number": number, "title": f"第{number}章", "goal": f"原目标{number}",
                 "conflict": "原阻力", "turning_point": "原转折", "ending_hook": "原收束"}
        chapter["route"] = dict(route)
        chapter["route_id"] = route["id"]
        chapter["volume_id"] = "v1"
        volume["chapters"].append(dict(route))
    project["planning"]["volumes"] = [volume]
    project["chapters"][0]["content"] = "已经写好的正文。"
    project["chapters"][0]["summary"] = "周砚提前烧掉了账册"
    project["chapters"][2]["route"]["locked"] = True
    return project


def test_rolling_replan_adjusts_unlocked_routes_and_records_diff(tmp_path, monkeypatch):
    project = replan_project(tmp_path)
    assert replan_targets(project, 0, 3) == [1, 3]
    monkeypatch.setattr(replan_module, "_audit_route_checkpoint_prefix", lambda *_a: (5, ""))

    async def completion(settings, messages, **kwargs):
        return {"adjustments": [
            {"number": 2, "changes": {"goal": "追查账册灰烬里的残页"}, "reason": "承接烧账"},
            {"number": 3, "changes": {"goal": "不允许改动"}},
        ]}, []

    updated, report = run(rolling_replan(project, 0, {}, completion, window=3))
    assert updated["chapters"][1]["route"]["goal"] == "追查账册灰烬里的残页"
    assert updated["chapters"][2]["route"]["goal"] == "原目标3"
    assert updated["planning"]["volumes"][0]["chapters"][1]["goal"] == "追查账册灰烬里的残页"
    assert report["changes"][0]["fields"]["goal"]["before"] == "原目标2"
    assert project["chapters"][1]["route"]["goal"] == "原目标2"  # input untouched


def test_rolling_replan_reverts_when_validation_gets_worse(tmp_path, monkeypatch):
    project = replan_project(tmp_path)
    results = iter([(5, ""), (1, "第2章越界")])
    monkeypatch.setattr(replan_module, "_audit_route_checkpoint_prefix", lambda *_a: next(results))

    async def completion(settings, messages, **kwargs):
        return {"adjustments": [{"number": 2, "changes": {"goal": "提前完成卷目标"}}]}, []

    updated, report = run(rolling_replan(project, 0, {}, completion, window=3))
    assert updated is project and report["reverted"] and not report["changes"]


# ---------------------------------------------------------------- director integration

def test_director_best_of_n_selects_judged_winner(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    import app.main as main
    import app.services.director_runtime as director_runtime
    from tests.test_workflow_integration import _planned_project

    store = ProjectStore(tmp_path / "bestofn.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_launch_director", lambda _task_id: None)
    project = _planned_project(store)
    project["settings"].update(candidate_count=2, rolling_replan=False, dialogue_rehearsal="off")
    project = store.save(project["id"], project, reason="fixture-settings")

    body = "\n\n".join(
        f"雨水沿着第{i}扇旧窗滑下，许澄核对纸纹、墨迹和日期，线索因此向码头推进。" for i in range(1, 36)
    )
    first = "她深吸一口气。" + body + "她又深吸一口气，把潮汐表折好，推门走进雨里。"
    second = "灯芯爆了一下。" + body + "她把潮汐表折好，推门走进雨里。"
    drafts = iter([first, second])

    async def fake_stream(*_args, **kwargs):
        yield next(drafts)
        kwargs["outcome"].observe({"choices": [{"finish_reason": "stop"}]})

    async def fake_structured(settings, messages, **kwargs):
        user = messages[-1]["content"]
        if "【候选A】" in user:
            a = user.split("【候选A】")[1].split("【候选B】")[0]
            return {"winner": "A" if "灯芯" in a else "B", "reasons": ["开场更具体"]}, []
        if "目标句" in user:
            return {"rewrites": [{"id": "S1", "text": "她把呼吸压得很轻，把潮汐表折好，推门走进雨里。"}]}, []
        return {}, []

    async def fake_plan(body):
        return {"goal": "确认旧信来源", "beats": ["检查纸张"], "ending_state": "决定前往废弃码头"}

    async def passing_audit(body):
        return {"score": 96, "verdict": "pass", "requires_review": False, "issues": [], "revision_brief": ""}

    async def fake_memory(body):
        return {"summary": "许澄决定前往码头。", "story_so_far": "许澄查到码头。", "character_updates": [],
                "facts": [], "plot_threads": [], "timeline": [], "relationship_updates": [],
                "continuity_notes": [], "description_updates": [], "scene_settlement": {}}

    monkeypatch.setattr(main, "chapter_plan", fake_plan)
    monkeypatch.setattr(main, "chapter_audit", passing_audit)
    monkeypatch.setattr(main, "chapter_memory", fake_memory)
    monkeypatch.setattr(director_runtime, "chat_stream", fake_stream)
    monkeypatch.setattr(director_runtime, "run_structured_completion", fake_structured)
    monkeypatch.setattr(director_runtime, "_director_candidate_gate_failures", lambda *_args: [])

    with TestClient(main.app) as client:
        response = client.post(
            f"/api/director/projects/{project['id']}/write-planned",
            json={"quality_threshold": 50, "max_revision_attempts": 0, "continue_on_quality_debt": True},
        )
    task_id = response.json()["task"]["id"]
    asyncio.run(main._run_auto_director(task_id))

    task = store.get_director_task(task_id)
    chapter = store.get(project["id"])["chapters"][0]
    assert task["status"] == "completed"
    assert task["chapter_candidates"]["selected"] == 1, task["chapter_candidates"]["report"]
    assert len(task["chapter_candidates"]["drafts"]) == 2
    assert chapter["content"].startswith("灯芯爆了一下。")
    messages = [event.get("message", "") for event in task.get("events", [])]
    assert any("候选稿比较完成" in item for item in messages)

"""Regression tests for the P1 craft review findings (focused critics, anti-slop,
item state machine, rolling re-planning, pairwise judging, schema fallback)."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from app.story.continuity_state import (
    _normalize_state,
    ITEM_STATE_ALIASES,
    ITEM_STATES,
    apply_continuity_updates,
    check_continuity_draft,
    item_states,
)
from app.writing import antislop
from app.review import focused_critics
from app.planning import rolling_replan as replan_module
from app.llm import structured_output
from app.writing.antislop import (
    BannedPhrase,
    llama_cpp_logit_bias,
    prose_stream_options,
    rewrite_slop,
)
from app.writing.candidate_selection import judge_excerpt, pairwise_judge, select_best_candidate
from app.review.focused_critics import BLOCK_LABELS, run_focused_critics
from app.planning.rolling_replan import apply_replan_changes, replan_targets, rolling_replan
from tests.test_p1_craft import replan_project


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- focused critics

SENTENCES = [f"第{i}句，甲做了第{i}件事。" for i in range(80)]
BASE_DRAFT = "".join(SENTENCES)


def _revised(replacement: str) -> str:
    changed = list(SENTENCES)
    changed[40] = replacement
    return "".join(changed)


@pytest.fixture(autouse=True)
def _clear_critic_memory():
    focused_critics._last_runs.clear()
    antislop._TOKEN_CACHE.clear()
    yield
    focused_critics._last_runs.clear()
    antislop._TOKEN_CACHE.clear()


def test_clean_audit_then_edit_never_passes_with_zero_checkers():
    blocks = {key: "权威上下文" for key in BLOCK_LABELS}
    calls: list[str] = []

    async def clean(settings, messages, **kwargs):
        calls.append(messages[-1]["content"])
        return {"issues": []}, []

    run(run_focused_critics({}, blocks, BASE_DRAFT, completion=clean, audit_key="p:c", draft=BASE_DRAFT))
    first = len(calls)
    result, _ = run(run_focused_critics(
        {}, blocks, _revised("乙早已死去，却与甲对饮。"), completion=clean,
        audit_key="p:c", draft=_revised("乙早已死去，却与甲对饮。"),
    ))
    # v0.35: a revision of a clean audit is verified by checking the changed
    # sentence — one real call, never zero.
    assert len(calls) - first == 1 and "乙早已死去" in calls[-1]
    assert result["audit_mode"] == "verify" and result["critic_calls"] == 1


def test_revision_verifies_prior_issue_and_ignores_unchanged_sentences():
    blocks = {key: "" for key in BLOCK_LABELS}
    blocks["facts"] = "甲的佩剑名为青锋"
    blocks["route"] = "本章目标：乙在第三日清晨死于渡口"
    seen: list[str] = []

    async def judge(settings, messages, **kwargs):
        focus = messages[-1]["content"]
        seen.append(focus)
        if "事实细节" in focus:
            return {"issues": [{"severity": "low", "message": "称谓", "evidence_ids": ["P1-S1"]}]}, []
        return {"issues": []}, []

    run(run_focused_critics({}, blocks, BASE_DRAFT, completion=judge, audit_key="p:c", draft=BASE_DRAFT))
    seen.clear()
    draft = _revised("乙在第三日清晨死于渡口，甲却在第二日与他对饮。")
    result, warnings = run(run_focused_critics({}, blocks, draft, completion=judge, audit_key="p:c", draft=draft))
    assert result["audit_mode"] == "verify" and result["prior_issue_ids"] == ["I1"]
    assert len(seen) == 2 and "修订复核" in seen[0] and "增量审计" in seen[1]
    # the unanswered prior issue stays (evidence still in the draft); the
    # re-reported nitpick on an unchanged sentence is dropped
    assert [item["issue_id"] for item in result["issues"]] == ["I1"]
    assert result["out_of_scope_dropped"] == 1 and any("不在改动句内" in item for item in warnings)


def test_context_change_forces_full_rerun():
    blocks = {key: "上下文" for key in BLOCK_LABELS}

    async def flag_facts(settings, messages, **kwargs):
        if "事实细节" in messages[-1]["content"]:
            return {"issues": [{"severity": "low", "message": "称谓"}]}, []
        return {"issues": []}, []

    run(run_focused_critics({}, blocks, BASE_DRAFT, completion=flag_facts, audit_key="p:c", draft=BASE_DRAFT))
    changed_blocks = dict(blocks, rules="新增规则：夜间禁行")
    draft = _revised("甲在夜里离城。")
    result, _ = run(run_focused_critics(
        {}, changed_blocks, draft, completion=flag_facts, audit_key="p:c", draft=draft,
    ))
    assert result["audit_mode"] == "full" and result["adaptive_rerun"] is None
    assert all(item["status"] != "carried" for item in result["checkers"])


# ---------------------------------------------------------------- anti-slop

def test_logit_bias_skips_byte_pieces_and_cache_is_per_model():
    requests: list[str] = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"tokens": [
            {"id": 101, "piece": [229, 152]}, {"id": 102, "piece": [180]}, {"id": 103, "piece": "勾起"},
        ]})

    transport = httpx.MockTransport(handler)
    phrases = [BannedPhrase("嘴角勾起", 1, "generic")]
    settings = {"base_url": "http://127.0.0.1:8080/v1", "model": "qwen"}
    assert run(llama_cpp_logit_bias(settings, phrases, transport=transport)) == [[103, -2.5]]
    run(llama_cpp_logit_bias({**settings, "model": "llama"}, phrases, transport=transport))
    assert len(requests) == 2  # another model on the same port re-tokenises


def test_prose_stream_options_honours_task_config():
    project = {"settings": {}}
    settings = {"base_url": "http://127.0.0.1:8080/v1"}
    assert run(prose_stream_options(project, settings, {"antislop_enabled": False})) is None


@pytest.mark.parametrize(
    ("text", "rewrite"),
    [
        ('"我深吸一口气，再试一次。你看着。"她说。', "我稳住呼吸，再试一次。"),
        ("『我深吸一口气，再试一次。你看着。』", "我稳住呼吸，再试一次。』"),
    ],
)
def test_rewrite_that_drops_ascii_or_corner_quote_is_rejected(text, rewrite):
    phrases = [BannedPhrase("深吸一口气", 0, "author")]

    async def completion(settings, messages, **kwargs):
        return {"rewrites": [{"id": "S1", "text": rewrite}]}, []

    revised, report = run(rewrite_slop(text, phrases, settings={}, completion=completion))
    assert revised == text
    assert report["skipped"][0]["reason"] == "改写破坏了引号或括号配对"


# ---------------------------------------------------------------- item state machine

def _settle(memory, chapter_id, number, state, evidence="证据", prose=None):
    return apply_continuity_updates(
        memory, {"id": chapter_id, "content": prose or ""}, number,
        {"item_updates": [{"item": "青锋剑", "state": state, "evidence": evidence}]},
        lambda value: (str(value), True), prose,
    )


def test_destroyed_item_cannot_silently_become_active():
    memory: dict = {}
    _settle(memory, "c3", 3, "折断")
    warnings = _settle(memory, "c5", 5, "持有", "他拔出青锋剑。", "他拔出青锋剑。")
    assert item_states(memory, 6)["青锋剑"]["state"] == "destroyed"
    assert warnings and "未交代找回或修复" in warnings[0]
    issues = check_continuity_draft({"memory": memory}, 5, "他拔出青锋剑。")
    assert issues and issues[0]["severity"] == "high"


def test_destroyed_item_returns_with_repair_evidence():
    memory: dict = {}
    _settle(memory, "c3", 3, "destroyed")
    _settle(memory, "c5", 5, "active", "铁匠重铸了青锋剑。", "铁匠重铸了青锋剑。")
    assert item_states(memory, 6)["青锋剑"]["state"] == "active"


def test_negated_state_alias_is_not_a_loss():
    assert _normalize_state("未丢失", ITEM_STATES, ITEM_STATE_ALIASES) == ""
    assert _normalize_state("并未遗失，仍在手中", ITEM_STATES, ITEM_STATE_ALIASES) == "active"
    assert _normalize_state("被夺走", ITEM_STATES, ITEM_STATE_ALIASES) == "lost"


def test_unrelated_recovery_word_does_not_disable_item_check():
    memory: dict = {}
    _settle(memory, "c3", 3, "destroyed")
    draft = "他拔出青锋剑。" + "雨一直下。" * 20 + "随后把钱袋归还给掌柜。"
    assert check_continuity_draft({"memory": memory}, 3, draft)


# ---------------------------------------------------------------- rolling re-plan

def test_author_edited_routes_are_not_replanned(tmp_path):
    project = replan_project(tmp_path)
    project["chapters"][1]["route"]["source"] = "author"
    project["planning"]["volumes"][0]["chapters"][3]["goal"] = "作者在规划面板改过"
    assert replan_targets(project, 0, 3) == []
    assert replan_targets(project, 0, 4) == [4]


def test_replan_keeps_plan_and_marks_it_stale(tmp_path, monkeypatch):
    project = replan_project(tmp_path)
    project["chapters"][1]["plan"] = {"goal": "作者写的执行计划"}
    monkeypatch.setattr(replan_module, "_audit_route_checkpoint_prefix", lambda *_a: (5, ""))

    async def completion(settings, messages, **kwargs):
        return {"adjustments": [{"number": 2, "changes": {"goal": "追查残页"}}]}, []

    updated, report = run(rolling_replan(project, 0, {}, completion, window=3))
    chapter = updated["chapters"][1]
    assert chapter["route"]["goal"] == "追查残页"
    assert chapter["plan"] == {"goal": "作者写的执行计划"}
    assert chapter["plan_stale"]["fields"] == ["goal"]


def test_replan_change_behind_invalid_prefix_is_not_accepted(tmp_path, monkeypatch):
    project = replan_project(tmp_path)
    # Route 1 (index 0) is already invalid: the prefix validator stops there.
    monkeypatch.setattr(replan_module, "_audit_route_checkpoint_prefix", lambda *_a: (0, "第1章无效"))

    async def completion(settings, messages, **kwargs):
        return {"adjustments": [{"number": 2, "changes": {"goal": "提前完成卷目标"}}]}, []

    updated, report = run(rolling_replan(project, 0, {}, completion, window=3))
    assert updated is project and not report["changes"]
    assert any("无法校验" in item for item in report["warnings"])


def test_replayed_replan_skips_fields_changed_meanwhile(tmp_path):
    project = replan_project(tmp_path)
    change = {"chapter": 2, "fields": {"goal": {"before": "原目标2", "after": "追查残页"}}}
    project["chapters"][1]["route"]["goal"] = "作者刚改的目标"
    project["planning"]["volumes"][0]["chapters"][1]["goal"] = "作者刚改的目标"
    updated, applied = apply_replan_changes(project, [change], 0)
    assert not applied and updated["chapters"][1]["route"]["goal"] == "作者刚改的目标"


# ---------------------------------------------------------------- pairwise judge

def test_position_biased_judge_is_a_tie_and_falls_back_to_checks():
    good = "他推开门，雨声灌进来。" * 20 + "她终于开口。"
    sloppy = "他深吸一口气。" * 10 + "她终于开口。"
    calls = []

    async def always_first(settings, messages, **kwargs):
        calls.append(1)
        return {"winner": "A", "reasons": ["A更好"]}, []

    verdict = run(pairwise_judge(good, sloppy, "", {}, always_first))
    assert verdict["winner"] == "tie" and len(calls) == 2
    result = run(select_best_candidate(
        [sloppy, good], project={}, target_chars=200, plan_brief="", settings={}, completion=always_first,
    ))
    assert result["text"] == good and result["report"]["matches"][0]["tie"]


def test_judge_excerpt_keeps_the_ending_of_long_drafts():
    text = "开头。" + "中段。" * 4000 + "结尾收束。"
    excerpt = judge_excerpt(text)
    assert excerpt.startswith("开头。") and excerpt.endswith("结尾收束。") and len(excerpt) < 6100


# ---------------------------------------------------------------- schema fallback

def test_http_400_on_schema_retries_once_with_json_object(monkeypatch):
    seen = []

    async def fake_chat_once(settings, messages, **kwargs):
        seen.append(kwargs.get("options"))
        if kwargs.get("options") is not None:
            request = httpx.Request("POST", "http://127.0.0.1:8080/v1/chat/completions")
            raise httpx.HTTPStatusError(
                "bad schema", request=request, response=httpx.Response(400, request=request)
            )
        return '{"issues": []}'

    monkeypatch.setattr(structured_output, "chat_once", fake_chat_once)
    result, warnings = run(structured_output.structured_completion(
        {"base_url": "http://127.0.0.1:8080/v1", "model": "m"},
        [{"role": "user", "content": "x"}],
        max_tokens=200, timeout_seconds=10, temperature=0.1,
        schema={"type": "object", "properties": {"issues": {"type": "array"}}},
    ))
    assert result == {"issues": []}
    assert seen[0] is not None and seen[1] is None
    assert any("JSON Schema" in item for item in warnings)


# ---------------------------------------------------------------- director helpers

def test_director_memory_timeout_scales_with_stages_and_unknown_score_is_none():
    from app.director import service as director_runtime

    assert director_runtime._director_memory_timeout() == min(900.0, 180.0 * len(director_runtime._MEMORY_STAGES))
    assert director_runtime._director_memory_timeout() > 180
    assert director_runtime._known_audit_score({"execution": {}}) is None
    assert director_runtime._known_audit_score({"execution": {"audit_score": None}}) is None
    assert director_runtime._known_audit_score({"execution": {"audit_score": 0}}) == 0
    assert director_runtime._known_audit_score({"execution": {"audit_score": "88"}}) == 88

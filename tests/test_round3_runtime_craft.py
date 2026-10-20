"""Round 3: risk-gated critic budget, hook debt ledger, negated recovery."""
from __future__ import annotations

import asyncio

import pytest

from app.api.routers.editorial import critic_risk_signals, new_named_entities
from app.story.continuity_state import (
    apply_continuity_updates,
    check_continuity_draft,
    recovered_near,
)
from app.writing.context_builder import build_prompt
from app.review import focused_critics
from app.writing.craft_settings import craft_setting
from app.review.focused_critics import (
    BLOCK_LABELS,
    choose_budget,
    run_focused_critics,
    score_critic_risk,
)
from app.story.hook_ledger import (
    due_hooks,
    hook_ending_check,
    outstanding_debts,
    record_chapter_hook,
    render_hook_debts_for_prompt,
    render_hooks_for_audit,
    settle_hooks,
)
from tests.test_p1_craft import project_fixture
from app.api import container


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear():
    focused_critics._last_runs.clear()
    yield
    focused_critics._last_runs.clear()


SENTENCES = [f"第{i}句，甲做了第{i}件事。" for i in range(80)]
DRAFT = "".join(SENTENCES)
BLOCKS = {key: "权威上下文" for key in BLOCK_LABELS}


# ---------------------------------------------------------------- settings

def test_new_craft_settings_defaults_and_bounds():
    assert craft_setting({}, "critic_budget") == "auto"
    assert craft_setting({"settings": {"critic_budget": "LIGHT"}}, "critic_budget") == "light"
    assert craft_setting({"settings": {"critic_budget": "bogus"}}, "critic_budget") == "auto"
    assert craft_setting({}, "critic_risk_threshold") == 40
    assert craft_setting({}, "hook_ledger") is True
    assert craft_setting({"settings": {"hook_window": 9}}, "hook_window") == 5
    assert craft_setting({}, "experience_bank") is True


# ---------------------------------------------------------------- risk score

def test_risk_score_reasons_and_budget_choice():
    low = score_critic_risk({"first_audit": False})
    assert low["level"] == "low" and choose_budget("auto", low) == "merged"
    high = score_critic_risk({"key_chapter": True, "first_audit": True, "new_entities": ["沈鸢"]})
    assert high["score"] == 30 + 10 + 8 and high["level"] == "high"
    assert any("关键章节" in item for item in high["reasons"])
    assert choose_budget("auto", high) == "full"
    assert choose_budget("full", low) == "full" and choose_budget("light", high) == "merged"


def test_risk_signals_from_project(tmp_path):
    project = project_fixture(tmp_path, 5)
    project["characters"] = [{"name": "许澄", "aliases": []}]
    chapter = project["chapters"][2]
    draft = "许澄抬头。一个名叫沈鸢的女人推门而入。" + "雨一直下。" * 30
    signals = critic_risk_signals(
        project, 2, chapter, draft, local_checks={"issues": []}, continuity_issues=[],
        agenda=[], hooks=[{"id": "H1", "status": "debt"}, {"id": "H2", "status": "open"}], audit_key="p:c3",
    )
    assert signals["new_entities"] == ["沈鸢"]
    assert signals["obligations"] == 1  # only the overdue hook counts and signals["first_audit"] is True
    assert signals["key_chapter"] is False
    assert new_named_entities(project, "许澄说道：走。") == []


# ---------------------------------------------------------------- merged / escalation

def _completion(issue_by_scope):
    calls: list[str] = []

    async def completion(settings, messages, **kwargs):
        last = messages[-1]["content"]
        scope = "merged" if "合并审计" in last else last.split("【本轮只负责：")[1].split("】")[0]
        calls.append(scope)
        return {"issues": issue_by_scope.get(scope, [])}, []

    return completion, calls


def test_low_risk_runs_one_merged_call():
    completion, calls = _completion({})
    result, _ = run(run_focused_critics({}, BLOCKS, DRAFT, completion=completion, budget="merged"))
    assert calls == ["merged"] and result["critic_budget"] == "merged" and result["critic_calls"] == 1
    assert result["verdict"] == "pass"


def test_merged_medium_issue_escalates_to_full_set():
    medium = [{"checker": "facts", "severity": "medium", "message": "称谓不一", "evidence_ids": ["P1-S1"]}]
    completion, calls = _completion({"merged": medium})
    result, _ = run(run_focused_critics({}, BLOCKS, DRAFT, completion=completion, budget="merged"))
    assert calls[0] == "merged" and len(calls) == 6
    assert result["critic_budget"] == "merged→full"
    assert result["checkers"][0] == {"checker": "merged", "status": "escalated", "issues": 1}


def test_light_budget_escalates_only_on_high():
    low = [{"checker": "pov", "severity": "low", "message": "语气略散"}]
    completion, calls = _completion({"merged": low})
    result, _ = run(run_focused_critics(
        {}, BLOCKS, DRAFT, completion=completion, budget="merged", escalate_on=("high",),
    ))
    assert calls == ["merged"] and result["issues"][0]["category"].startswith("视角与语气")


def test_revision_of_merged_audit_runs_merged_again_and_full_stays_partial():
    completion, calls = _completion({})
    run(run_focused_critics({}, BLOCKS, DRAFT, completion=completion, audit_key="k", draft=DRAFT, budget="merged"))
    revised = DRAFT.replace("第40句，甲做了第40件事。", "第40句，甲改做了另一件事。")
    calls.clear()
    result, _ = run(run_focused_critics(
        {}, BLOCKS, revised, completion=completion, audit_key="k", draft=revised, budget="merged",
    ))
    # v0.35: a clean audit's revision is verified by one changed-sentence check.
    assert calls == ["merged"] and result["critic_budget"] == "verify"
    # Previous full audit with an issue: one verification call + one delta check.
    focused_critics._last_runs.clear()
    completion, calls = _completion({"事实细节": [{"severity": "low", "message": "x"}]})
    run(run_focused_critics({}, BLOCKS, DRAFT, completion=completion, audit_key="k", draft=DRAFT, budget="full"))
    seen: list[str] = []

    async def verifier(settings, messages, **kwargs):
        seen.append(messages[-1]["content"])
        return {"results": [{"id": "I1", "status": "resolved"}], "issues": []}, []

    result, _ = run(run_focused_critics(
        {}, BLOCKS, revised, completion=verifier, audit_key="k", draft=revised, budget="merged",
    ))
    assert result["critic_budget"] == "verify" and len(seen) == 2 and "[I1]" in seen[0]
    assert result["issues"] == [] and result["resolved_issues"][0]["issue_id"] == "I1"


def test_hook_status_comes_back_from_timeline_checker():
    async def completion(settings, messages, **kwargs):
        last = messages[-1]["content"]
        if "时间线" in last.split("\n")[0]:
            assert "上章钩子" in last
            return {"issues": [], "hook_status": [{"id": "H1", "status": "advanced"}, {"id": "H2", "status": "bogus"}]}, []
        return {"issues": []}, []

    blocks = dict(BLOCKS, hooks=render_hooks_for_audit([
        {"id": "H1", "origin_chapter": 1, "status": "open", "text": "门外有人"},
    ]))
    result, _ = run(run_focused_critics({}, blocks, DRAFT, completion=completion, budget="full"))
    assert result["hook_status"] == [{"id": "H1", "status": "advanced"}]


# ---------------------------------------------------------------- hook ledger

def _hook_project(tmp_path):
    project = project_fixture(tmp_path, 6)
    for number, chapter in enumerate(project["chapters"], start=1):
        chapter["route"] = {"ending_hook": f"黑衣人留下第{number}枚铜钱记号"}
    return project


def test_hook_becomes_debt_after_window_and_is_injected(tmp_path):
    project = _hook_project(tmp_path)
    chapters = project["chapters"]
    record_chapter_hook(project, chapters[0], 1, window=2)
    hook = project["memory"]["hook_ledger"][0]
    assert [item["id"] for item in due_hooks(project, 2)] == [hook["id"]]
    settle_hooks(project, 2, "雨一直下，众人各自回家。", [{"id": hook["id"], "status": "ignored"}])
    assert outstanding_debts(project, 3) == []  # still inside the window
    settle_hooks(project, 3, "雨一直下。", [])  # model silent → local overlap → ignored
    assert [item["id"] for item in outstanding_debts(project, 4)] == [hook["id"]]
    text = render_hook_debts_for_prompt(project, 4)
    assert "第1章：黑衣人留下第1枚铜钱记号" in text
    # Paying the debt later closes it.
    settle_hooks(project, 4, "", [{"id": hook["id"], "status": "paid"}])
    assert outstanding_debts(project, 5) == []


def test_advanced_hook_resets_window_and_resettle_is_idempotent(tmp_path):
    project = _hook_project(tmp_path)
    record_chapter_hook(project, project["chapters"][0], 1, window=1)
    hook_id = project["memory"]["hook_ledger"][0]["id"]
    settle_hooks(project, 2, "", [{"id": hook_id, "status": "advanced"}])
    settle_hooks(project, 3, "", [{"id": hook_id, "status": "ignored"}])
    assert len(outstanding_debts(project, 4)) == 1
    settle_hooks(project, 3, "", [{"id": hook_id, "status": "paid"}])  # chapter 3 re-settled
    assert outstanding_debts(project, 4) == []


def test_hook_debts_enter_the_per_chapter_prompt_not_the_system_prefix(tmp_path):
    project = _hook_project(tmp_path)
    record_chapter_hook(project, project["chapters"][0], 1, window=1)
    hook_id = project["memory"]["hook_ledger"][0]["id"]
    settle_hooks(project, 2, "", [{"id": hook_id, "status": "ignored"}])
    build = build_prompt(project, {
        "chapter_id": project["chapters"][2]["id"], "mode": "continue", "instruction": "",
        "_experience_hints": ["对白常替人物解释动机"],
    })
    system = next(item["content"] for item in build.messages if item["role"] == "system")
    user = next(item["content"] for item in build.messages if item["role"] == "user")
    assert "待承接的章末钩子" in user and "待承接的章末钩子" not in system
    assert "第1章：黑衣人留下第1枚铜钱记号" in user
    assert "对白常替人物解释动机" in user and "对白常替人物解释动机" not in system
    project["settings"]["experience_bank"] = False
    project["settings"]["hook_ledger"] = False
    build = build_prompt(project, {
        "chapter_id": project["chapters"][2]["id"], "mode": "continue", "instruction": "",
        "_experience_hints": ["对白常替人物解释动机"],
    })
    joined = "\n".join(item["content"] for item in build.messages)
    assert "待承接的章末钩子" not in joined and "对白常替人物解释动机" not in joined


def test_hook_ending_check_is_warning_only():
    flat = "他回到家，吃了饭，睡了。" * 20
    assert hook_ending_check(flat)["ok"] is False
    assert hook_ending_check(flat + "门外忽然响起敲门声？")["ok"] is True
    assert hook_ending_check(flat + "黑衣人留下一枚铜钱记号。", "黑衣人留下铜钱记号")["ok"] is True


# ---------------------------------------------------------------- negated recovery

@pytest.mark.parametrize("text", ["他无法找回青锋剑。", "他没能找回青锋剑。", "青锋剑未能修复。", "青锋剑再也找不回了。"])
def test_negated_recovery_is_not_recovery(text):
    assert recovered_near(text, "青锋剑") is False
    memory: dict = {}
    apply_continuity_updates(memory, {"id": "c3"}, 3, {"item_updates": [
        {"item": "青锋剑", "state": "destroyed", "evidence": "x"}]}, lambda value: (str(value), True))
    assert check_continuity_draft({"memory": memory}, 4, text + "他拔出青锋剑。")


def test_affirmed_recovery_still_counts():
    assert recovered_near("铁匠修复了青锋剑。", "青锋剑") is True


# ---------------------------------------------------------------- audit endpoint

def test_chapter_audit_records_risk_and_uses_one_merged_call(tmp_path, monkeypatch):
    from app.writing.requests import ChapterActionRequest

    project = project_fixture(tmp_path, 5)
    chapter = project["chapters"][2]
    chapter["route"] = {"goal": "核对旧信", "ending_hook": "门外传来脚步声"}
    calls: list[str] = []

    async def fake(settings, messages, **kwargs):
        last = messages[-1]["content"]
        calls.append("merged" if "合并审计" in last else "focused")
        return {"issues": []}, []

    monkeypatch.setattr(container, "run_structured_completion", fake)
    draft = "许澄推开门，雨声灌进来，她把旧信摊在灯下，逐字核对墨迹与日期。" * 12 + "门外传来脚步声，她屏住呼吸？"
    result = asyncio.run(container.chapter_audit(ChapterActionRequest(
        project=project, chapter_id=chapter["id"], draft=draft, instruction="测试",
    )))
    assert calls == ["merged"]
    assert result["risk"]["budget_used"] == "merged" and result["risk"]["calls"] == 1
    assert result["risk"]["level"] == "low" and result["risk"]["reasons"]
    assert result["hook_ending"]["ok"] is True

    project["settings"]["critic_budget"] = "full"
    calls.clear()
    result = asyncio.run(container.chapter_audit(ChapterActionRequest(
        # an unrelated draft (a small revision would be verified instead)
        project=project, chapter_id=chapter["id"], draft=draft.replace("许澄", "林远"), instruction="测试",
    )))
    assert calls and set(calls) == {"focused"} and result["risk"]["budget_used"] == "full"

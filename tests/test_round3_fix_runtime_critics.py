"""Round 3 review fixes: risk-gated critic budget (escalation, calibration)."""
from __future__ import annotations

import asyncio

import pytest

from app.api.routers.editorial import critic_risk_signals, new_named_entities
from app.story.evidence import number_draft
from app.quality import local_quality_check
from app.services import focused_critics
from app.services.focused_critics import (
    BLOCK_LABELS,
    DEFAULT_RISK_THRESHOLD,
    choose_budget,
    run_focused_critics,
    score_critic_risk,
)
from tests.test_p1_craft import project_fixture


@pytest.fixture(autouse=True)
def _clear():
    focused_critics._last_runs.clear()
    yield
    focused_critics._last_runs.clear()


BLOCKS = {key: "权威上下文" for key in BLOCK_LABELS}
DRAFT = "".join(f"第{i}句，林远做了第{i}件事。" for i in range(40))

# A typical dialogue-heavy, non-key chapter: known cast, speaker tags with
# pronoun + adverb ("她冷笑道"), a generic role, no tracked items or injuries.
DIALOGUE = (
    "雨停的时候，林远把伞靠在门边。"
    "“你又来晚了。”苏晚头也不抬，她冷笑道：“账本还在我这儿，你急什么？”"
    "林远在她对面坐下，他沉声问道：“码头那边怎么说？”"
    "“怎么说？”苏晚把茶推过去，“老掌柜说船明天才到，让我们别乱跑。”"
    "，老掌柜笑道：“两位客官，茶要凉了。”"
    "林远看了老掌柜一眼，没有接话。窗外的灯一盏盏亮起来，街上有人吆喝着收摊。"
    "“明天我去码头。”他说，“你守着铺子。”"
    "苏晚低声说道：“你一个人去？”"
    "“一个人去。”林远站起身，把伞重新拿在手里。"
) * 4


def _issue_completion(by_scope, fail=()):
    calls: list[str] = []

    async def completion(settings, messages, **kwargs):
        last = messages[-1]["content"]
        scope = "merged" if "合并审计" in last else last.split("【本轮只负责：")[1].split("】")[0]
        calls.append(scope)
        if scope in fail:
            raise asyncio.TimeoutError()
        return {"issues": by_scope.get(scope, []), "hook_status": by_scope.get(f"{scope}:hooks", [])}, []

    return completion, calls


# ---------------------------------------------------------------- #1 escalation keeps merged findings

def test_escalated_merged_issue_survives_when_its_checker_fails():
    high = [{"checker": "character", "severity": "high", "message": "林远说出了不该知道的密信内容",
             "evidence_ids": ["P1-S1"]}]
    completion, calls = _issue_completion(
        {"merged": high, "merged:hooks": [{"id": "Habc", "status": "paid"}]}, fail=("人物记忆与能力",),
    )
    result, warnings = asyncio.run(run_focused_critics(
        {}, BLOCKS, number_draft(DRAFT), completion=completion, budget="merged", draft=DRAFT,
    ))
    assert calls[0] == "merged" and len(calls) == 6
    assert result["verdict"] == "revise"
    assert [item.get("carried_from") for item in result["issues"]] == ["merged"]
    assert result["issues"][0]["severity"] == "high"
    assert result["hook_status"] == [{"id": "Habc", "status": "paid"}]  # timeline gave none
    assert any("沿用合并审计" in item for item in warnings)


def test_completed_checker_supersedes_merged_issue():
    high = [{"checker": "facts", "severity": "high", "message": "称谓矛盾", "evidence_ids": ["P1-S1"]}]
    completion, _calls = _issue_completion({"merged": high})
    result, _ = asyncio.run(run_focused_critics(
        {}, BLOCKS, number_draft(DRAFT), completion=completion, budget="merged", draft=DRAFT,
    ))
    assert result["issues"] == [] and result["verdict"] == "pass"


def test_all_focused_checkers_failing_falls_back_to_merged_result():
    high = [{"checker": "pov", "severity": "high", "message": "写了他人内心", "evidence_ids": ["P1-S1"]}]
    labels = tuple(checker.label for checker in focused_critics.CHECKERS)
    completion, _calls = _issue_completion({"merged": high}, fail=labels)
    result, _ = asyncio.run(run_focused_critics(
        {}, BLOCKS, number_draft(DRAFT), completion=completion, budget="merged", draft=DRAFT,
    ))
    assert result["verdict"] == "revise" and result["issues"][0]["message"] == "写了他人内心"


# ---------------------------------------------------------------- #2 medium escalation needs evidence

def test_unverified_medium_does_not_escalate_but_verified_medium_does():
    unverified = [{"checker": "facts", "severity": "medium", "message": "称谓不一", "evidence_ids": ["P99-S9"]}]
    completion, calls = _issue_completion({"merged": unverified})
    result, _ = asyncio.run(run_focused_critics(
        {}, BLOCKS, number_draft(DRAFT), completion=completion, budget="merged", draft=DRAFT,
        escalate_on=("high", "medium_verified"),
    ))
    assert calls == ["merged"] and result["critic_budget"] == "merged"
    focused_critics._last_runs.clear()
    verified = [dict(unverified[0], evidence_ids=["P1-S1"])]
    completion, calls = _issue_completion({"merged": verified})
    result, _ = asyncio.run(run_focused_critics(
        {}, BLOCKS, number_draft(DRAFT), completion=completion, budget="merged", draft=DRAFT,
        escalate_on=("high", "medium_verified"),
    ))
    assert len(calls) == 6 and result["critic_budget"] == "merged→full"


def test_dialogue_tags_are_not_new_entities():
    project = {"characters": [{"name": "林远"}, {"name": "苏晚"}]}
    assert new_named_entities(project, DIALOGUE) == []
    introduced = "一个名叫阿七的小厮跑进来。阿七喘着气，阿七说道：“船到了。”"
    assert new_named_entities(project, introduced) == ["阿七"]
    # A genuinely new speaker who recurs in the chapter is still caught.
    assert new_named_entities(project, "，沈鸢笑道：“走吧。”沈鸢转身出门。") == ["沈鸢"]


def test_typical_dialogue_chapter_scores_below_threshold(tmp_path):
    project = project_fixture(tmp_path, 6)
    project["characters"] = [{"name": "林远", "aliases": []}, {"name": "苏晚", "aliases": []}]
    chapter = project["chapters"][3]
    chapter["route"] = {"goal": "约定去码头", "ending_hook": "船明天到港"}
    local = local_quality_check(DIALOGUE, target_words=len(DIALOGUE))
    hooks = [{"id": "H1", "status": "open"}, {"id": "H2", "status": "open"}]
    signals = critic_risk_signals(
        project, 3, chapter, DIALOGUE, local_checks=local, continuity_issues=[],
        agenda=[], hooks=hooks, audit_key="p:c4",
    )
    risk = score_critic_risk(signals)
    assert signals["new_entities"] == [] and signals["obligations"] == 0
    assert risk["score"] < DEFAULT_RISK_THRESHOLD, risk
    assert choose_budget("auto", risk) == "merged"
    # Overdue hook debts and a key chapter still push it over.
    risky = score_critic_risk({**signals, "key_chapter": True, "obligations": 1})
    assert risky["level"] == "high"


def test_first_audit_uses_persisted_attempts(tmp_path):
    project = project_fixture(tmp_path, 3)
    chapter = project["chapters"][1]
    chapter["workflow"] = {"stages": {"audit": {"attempts": 3}}}
    signals = critic_risk_signals(
        project, 1, chapter, "雨停了。", local_checks={"issues": []}, continuity_issues=[],
        agenda=[], hooks=[], audit_key="after-restart",
    )
    assert signals["first_audit"] is False

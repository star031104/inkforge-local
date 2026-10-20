"""v0.35: verification re-audit, historical-fiction checks, debate workbench."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.core.config import APP_VERSION
from app.story.debate import (
    chapter_debate,
    is_debate_chapter,
    normalize_debate,
    plan_debate_entry,
    render_debate_for_audit,
    render_debate_for_prompt,
)
from app.llm.json_schemas import CHAPTER_PLAN_SCHEMA
from app.storage.schema.project import ensure_project_defaults
from app.story.history import apply_history_issues, check_history, parse_year
from app.storage.schema.planning import normalize_route
from app.planning.prompts import CHAPTER_PLAN_PROMPT
from app.writing.context_builder import _render_plan
from app.review import focused_critics
from app.review.focused_critics import (
    BLOCK_LABELS,
    MAX_VERIFY_CHAIN,
    carry_prior_issue,
    changed_units,
    run_focused_critics,
)
from tests.test_p1_craft import project_fixture
from app.api import container

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clear():
    focused_critics._last_runs.clear()
    yield
    focused_critics._last_runs.clear()


def run(coro):
    return asyncio.run(coro)


SENTENCES = [f"第{i}句，甲做了第{i}件事。" for i in range(40)]
DRAFT = "".join(SENTENCES)
BLOCKS = {key: "权威上下文" for key in BLOCK_LABELS}


def revise(index: int, text: str, base: list[str] | None = None) -> str:
    sentences = list(base or SENTENCES)
    sentences[index] = text
    return "".join(sentences)


def scripted(full=None, verify=None, delta=None, fail_verify=False):
    """Fake completion dispatching on the kind of request; records last messages."""
    seen: list[str] = []

    async def completion(settings, messages, **kwargs):
        last = messages[-1]["content"]
        seen.append(last)
        if "修订复核" in last:
            if fail_verify:
                raise asyncio.TimeoutError()
            return {"results": verify or []}, []
        if "增量审计" in last:
            return {"issues": delta or []}, []
        if "合并审计" in last:
            return {"issues": []}, []
        scope = last.split("【本轮只负责：")[1].split("】")[0]
        return {"issues": (full or {}).get(scope, [])}, []

    return completion, seen


FULL_ISSUES = {
    "事实细节": [{"severity": "high", "message": "称谓前后不一", "suggestion": "统一称谓", "evidence_ids": ["P1-S3"]}],
    "世界规则与地理": [{"severity": "medium", "message": "夜间出城违反宵禁", "evidence_ids": ["P1-S5"]}],
}


def audit(completion, draft, blocks=BLOCKS, key="p:c", budget="full"):
    return run(run_focused_critics({}, blocks, draft, completion=completion, audit_key=key, draft=draft, budget=budget))


# ---------------------------------------------------------------- 1. verification re-audit

def test_revision_is_verified_and_score_converges():
    completion, seen = scripted(full=FULL_ISSUES)
    first, _ = audit(completion, DRAFT)
    assert first["audit_mode"] == "full" and first["prior_issue_ids"] == []
    assert [item["issue_id"] for item in first["issues"]] == ["I1", "I2"] and first["score"] == 100 - 18 - 8

    draft = revise(2, "第2句，甲改正了对乙的称谓。")
    completion, seen = scripted(
        verify=[
            {"id": "I1", "status": "resolved", "note": "称谓已统一"},
            {"id": "I2", "status": "partially", "evidence_ids": ["P1-S5"]},
        ],
        delta=[
            {"checker": "facts", "severity": "low", "message": "新句语气略硬", "evidence_ids": ["P1-S3"]},
            {"checker": "pov", "severity": "high", "message": "旧句的新挑剔", "evidence_ids": ["P1-S20"]},
        ],
    )
    result, warnings = audit(completion, draft)
    assert result["audit_mode"] == "verify" and result["critic_budget"] == "verify"
    assert result["critic_calls"] == 2 and len(seen) == 2
    # the verifier sees the numbered prior issues with their old evidence
    assert "[I1]" in seen[0] and "[I2]" in seen[0] and "第2句，甲做了第2件事。" in seen[0]
    # the delta checker is told exactly which sentences changed
    assert "[P1-S3]第2句，甲改正了对乙的称谓。" in seen[1]
    assert result["prior_issue_ids"] == ["I1", "I2"]
    assert [item["issue_id"] for item in result["resolved_issues"]] == ["I1"]
    assert result["changed_sentence_ids"] == ["P1-S3"]
    by_id = {item["issue_id"]: item for item in result["issues"]}
    assert set(by_id) == {"I2", "I3"}
    assert by_id["I2"]["severity"] == "low" and by_id["I2"]["verification"] == "partially"
    assert by_id["I3"]["message"] == "新句语气略硬"
    assert result["out_of_scope_dropped"] == 1 and any("不在改动句内" in item for item in warnings)
    assert result["score"] == 100 - 3 - 3 and result["verdict"] == "pass"
    assert [item["checker"] for item in result["checkers"]] == ["verify", "delta"]


def test_unanswered_issue_stays_and_unlocatable_evidence_is_downgraded():
    order = [f"P1-S{i}" for i in range(1, 41)]
    prior = {"issue_id": "I1", "severity": "high", "message": "m", "quotes": ["第3句，甲做了第3件事。"]}
    kept = carry_prior_issue(prior, None, DRAFT, order)
    assert kept["severity"] == "high" and kept["evidence_ids"] == ["P1-S4"]
    gone = carry_prior_issue({**prior, "quotes": ["根本不存在的句子内容很长"]}, {"status": "unresolved"}, DRAFT, order)
    assert gone["severity"] == "low" and gone["evidence_unlocated"] is True
    assert carry_prior_issue(prior, {"status": "resolved"}, DRAFT, order) is None


def test_changed_units_ignore_whitespace_and_report_ids():
    draft = revise(5, "第5句，甲改了。")
    assert changed_units(DRAFT, draft) == [("P1-S6", "第5句，甲改了。")]
    assert changed_units(DRAFT, DRAFT.replace("，", " ，", 1)) == []


def test_full_audit_fallbacks():
    completion, _ = scripted(full=FULL_ISSUES)
    audit(completion, DRAFT)
    # process restart: no memory
    focused_critics._last_runs.clear()
    assert audit(completion, revise(2, "第2句，甲改了。"))[0]["audit_mode"] == "full"
    # too much changed
    big = "".join(f"第{i}句，乙另起炉灶。" if i % 2 else s for i, s in enumerate(SENTENCES))
    assert audit(completion, big)[0]["audit_mode"] == "full"
    # identical text audited again on purpose
    assert audit(completion, big)[0]["audit_mode"] == "full"


def test_verify_chain_is_bounded():
    completion, _ = scripted(full=FULL_ISSUES)
    modes = [audit(completion, DRAFT)[0]["audit_mode"]]
    sentences = list(SENTENCES)
    for round_number in range(MAX_VERIFY_CHAIN + 1):
        sentences[10 + round_number] = f"第{10 + round_number}句，甲第{round_number}次修订。"
        modes.append(audit(completion, "".join(sentences))[0]["audit_mode"])
    assert modes == ["full"] + ["verify"] * MAX_VERIFY_CHAIN + ["full"]


def test_failed_verification_falls_back_to_full_audit():
    completion, _ = scripted(full=FULL_ISSUES)
    audit(completion, DRAFT)
    completion, seen = scripted(full=FULL_ISSUES, fail_verify=True)
    result, warnings = audit(completion, revise(2, "第2句，甲改了。"))
    assert result["audit_mode"] == "full" and any("修订复核未完成" in item for item in warnings)
    assert sum("本轮只负责" in item for item in seen) == 5


def test_draft_dependent_context_does_not_block_verification_but_route_does():
    completion, _ = scripted(full=FULL_ISSUES)
    audit(completion, DRAFT)
    shifted = dict(BLOCKS, facts="按新草稿检索到的另一批记忆", world="新触发的世界书条目")
    verify_completion, _ = scripted(verify=[{"id": "I1", "status": "resolved"}, {"id": "I2", "status": "resolved"}])
    assert audit(verify_completion, revise(2, "第2句，甲改了。"), blocks=shifted)[0]["audit_mode"] == "verify"
    rerouted = dict(shifted, route="作者改了本章路线")
    assert audit(completion, revise(3, "第3句，甲也改了。"), blocks=rerouted)[0]["audit_mode"] == "full"


def test_failed_checker_run_is_never_used_as_verification_base():
    async def flaky(settings, messages, **kwargs):
        if "人物记忆" in messages[-1]["content"]:
            raise asyncio.TimeoutError()
        return {"issues": []}, []

    audit(flaky, DRAFT)
    assert audit(flaky, revise(2, "第2句，甲改了。"))[0]["audit_mode"] == "full"


# ---------------------------------------------------------------- 3. history tools

def history_project():
    return {
        "chapters": [{"id": "c1", "story_date": "秦王政十七年", "story_year": -230}],
        "characters": [
            {"name": "吕不韦", "aliases": ["文信侯"], "died_year": -235},
            {"name": "李斯", "aliases": [], "enters_year": -237},
            {"name": "韩非", "aliases": ["韩非子"], "enters_year": -228},
            {"name": "扶苏", "born_year": -225},
        ],
        "governance": {"era_terms": [{"term": "皇帝", "from_year": -221, "to_year": None, "note": "秦统一后始称皇帝"}]},
    }


def test_history_flags_dead_unborn_not_entered_and_era_terms():
    draft = "文信侯拱手道：“此事可议。”李斯沉默不语。韩非在殿外候着。扶苏奔进来。众人皆称皇帝圣明。"
    issues = check_history(history_project(), 0, draft)
    by_message = {item["message"].split("于")[0].split("生")[0].split("设定")[0]: item for item in issues}
    assert by_message["吕不韦已"]["severity"] == "high"
    assert "文信侯拱手道" in by_message["吕不韦已"]["evidence"][0]
    assert any(item["severity"] == "medium" and item["message"].startswith("韩非") for item in issues)
    assert any(item["severity"] == "high" and item["message"].startswith("扶苏") for item in issues)
    assert not any(item["message"].startswith("李斯") for item in issues)
    era = [item for item in issues if item["category"] == "称谓与官制"]
    assert len(era) == 1 and era[0]["severity"] == "medium" and "前221年" in era[0]["message"]
    assert {item["category"] for item in issues} == {"历史时间线", "称谓与官制"}


def test_history_skips_memorial_mentions_and_chapters_without_year():
    project = history_project()
    assert check_history(project, 0, "众人在吕不韦灵位前祭拜。") == []
    project["chapters"][0]["story_year"] = None
    assert check_history(project, 0, "吕不韦拱手道：“此事可议。”众人皆称皇帝。") == []
    assert parse_year("-247") == -247 and parse_year("") is None and parse_year("秦") is None


def test_apply_history_issues_penalizes_local_checks():
    local = {"score": 100, "verdict": "pass", "issues": []}
    apply_history_issues(local, [{"severity": "high", "category": "历史时间线", "message": "x"}])
    assert local["score"] == 82 and local["verdict"] == "revise" and len(local["issues"]) == 1
    untouched = {"score": 96, "verdict": "pass"}
    assert apply_history_issues(untouched, []) == {"score": 96, "verdict": "pass"}


def test_defaults_and_route_normalization_carry_new_fields(tmp_path):
    project = project_fixture(tmp_path, 2)
    project["chapters"][0].update(story_year="-230", plan={"debate": {"topic": "", "opponent_strongest_arguments": []}})
    project["characters"] = [{"name": "吕不韦", "died_year": "-235"}]
    project["governance"]["era_terms"] = [{"term": "朕", "from_year": "-221"}, {"term": ""}]
    clean = ensure_project_defaults(project)
    assert clean["chapters"][0]["story_year"] == -230 and clean["chapters"][1]["story_date"] == ""
    assert "debate" not in clean["chapters"][0]["plan"]  # empty debate is dropped
    assert clean["characters"][0]["died_year"] == -235 and clean["characters"][0]["enters_year"] is None
    assert clean["governance"]["era_terms"] == [{"term": "朕", "from_year": -221, "to_year": None, "note": ""}]
    route = normalize_route({"title": "廷议", "story_date": "秦王政十年", "story_year": -237,
                             "debate": {"topic": "逐客", "opponent_strongest_arguments": ["a", "b", "c", "d"]}}, 3)
    assert route["story_year"] == -237 and route["debate"]["opponent_strongest_arguments"] == ["a", "b", "c"]
    assert "debate" not in normalize_route({"title": "普通章"}, 4)


def test_chapter_audit_applies_history_and_debate_checks(tmp_path, monkeypatch):
    from app.writing.requests import ChapterActionRequest

    project = project_fixture(tmp_path, 3)
    project["characters"] = [{"name": "吕不韦", "aliases": [], "died_year": -235}]
    chapter = project["chapters"][1]
    chapter.update(story_year=-230, route={"goal": "廷议逐客", "ending_hook": "王未表态"})
    chapter["plan"] = {"chapter_type": "廷议", "debate": {
        "topic": "是否逐客", "opponent_strongest_arguments": ["客卿多为六国间谍，郑国渠即是明证"],
        "outcome_and_cost": "秦王暂缓逐客，李斯失去宗室信任"}}
    seen: list[str] = []

    async def fake(settings, messages, **kwargs):
        seen.append(messages[-1]["content"])
        return {"issues": []}, []

    monkeypatch.setattr(container, "run_structured_completion", fake)
    draft = "吕不韦拱手道：“客卿之用，在于其能。”宗室大臣冷笑着反驳，殿上一时无人作声。" * 4 + "王终于开口？"
    result = run(container.chapter_audit(ChapterActionRequest(
        project=project, chapter_id=chapter["id"], draft=draft, instruction="测试", config={"critic_budget": "full"},
    )))
    local = result["local_checks"]["issues"]
    assert any(item.get("category") == "历史时间线" and item["severity"] == "high" for item in local)
    assert result["verdict"] != "pass"
    assert any("交锋强度" in item and "客卿多为六国间谍" in item for item in seen)
    # an ordinary chapter never runs the debate checker
    seen.clear()
    plain = project["chapters"][2]
    plain["route"] = {"goal": "回府", "ending_hook": "门外有人"}
    run(container.chapter_audit(ChapterActionRequest(
        project=project, chapter_id=plain["id"], draft=draft.replace("吕不韦", "王翦"), instruction="测试",
        config={"critic_budget": "full"},
    )))
    assert seen and not any("交锋强度" in item for item in seen)


# ---------------------------------------------------------------- 4. debate workbench

DEBATE = {
    "topic": "法与礼孰先", "venue_and_judge": "稷下学宫，齐王裁决", "stakes": "败者离齐",
    "opponent_strongest_arguments": ["礼定名分，名分乱则法无所施", "严刑之下民免而无耻"],
    "protagonist_arguments": "以齐国新垦田税为例", "protagonist_weak_points": "不熟齐国旧俗",
    "opponent_counterattack": "指其外来者不知民情", "outcome_and_cost": "齐王采其策，但他被逐出学宫",
}


def test_debate_rendering_and_gating():
    chapter = {"plan": {"debate": DEBATE, "goal": "稷下论战"}}
    prompt = render_debate_for_prompt(chapter)
    assert prompt.startswith("交锋结构") and "对手最强论点2：严刑之下民免而无耻" in prompt and "纳头便拜" in prompt
    assert "交锋结构" in _render_plan(chapter) and "交锋结构" not in _render_plan({"plan": {"goal": "赶路"}})
    assert render_debate_for_audit({"plan": {"chapter_type": "廷议"}}).startswith("本章类型为交锋")
    assert render_debate_for_audit({"plan": {"chapter_type": "escalation"}}) == ""
    assert is_debate_chapter({"route": {"debate": DEBATE}}) and chapter_debate({"plan": {}}) is None
    assert normalize_debate("bogus")["opponent_strongest_arguments"] == []


def test_debate_checker_only_runs_with_debate_context():
    completion, seen = scripted()
    run(run_focused_critics({}, BLOCKS, DRAFT, completion=completion, budget="full"))
    assert len(seen) == 5 and not any("交锋强度" in item for item in seen)
    seen.clear()
    run(run_focused_critics({}, dict(BLOCKS, debate="辩题：法与礼"), DRAFT, completion=completion, budget="full"))
    assert len(seen) == 6 and "【本章交锋结构】\n辩题：法与礼" in seen[-1]
    seen.clear()
    run(run_focused_critics({}, dict(BLOCKS, debate="辩题：法与礼"), DRAFT, completion=completion, budget="merged"))
    assert len(seen) == 1 and "debate（交锋强度）" in seen[0]


def test_chapter_plan_schema_prompt_and_precedence():
    assert "debate" in CHAPTER_PLAN_SCHEMA["properties"] and "debate" not in CHAPTER_PLAN_SCHEMA["required"]
    assert "debate 只在本章是辩论" in CHAPTER_PLAN_PROMPT
    route_debate = {**DEBATE, "topic": "路线辩题"}
    assert plan_debate_entry({"debate": route_debate}, {"debate": DEBATE}, {"debate": DEBATE})["debate"]["topic"] == "路线辩题"
    assert plan_debate_entry({}, {"debate": DEBATE}, {"debate": route_debate})["debate"]["topic"] == "法与礼孰先"
    assert plan_debate_entry({}, {}, {"goal": "x"}) == {}


def test_frontend_assets_are_wired():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    for needle in (
        f"/assets/js/features/story/history-debate.js?v={APP_VERSION}", f"/assets/css/features/history-debate.css?v={APP_VERSION}",
        'id="chapterStoryDate"', 'id="chapterStoryYear"', 'id="debateWorkbenchBtn"', 'id="eraTermsBtn"',
    ):
        assert needle in html
    resources = (ROOT / "static" / "js" / "features" / "story" / "resources.js").read_text(encoding="utf-8")
    assert "c-died" in resources and "enters_year" in resources
    project_js = (ROOT / "static" / "js" / "core" / "project.js").read_text(encoding="utf-8")
    assert "story_year" in project_js

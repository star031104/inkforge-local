"""Experience bank: aggregation, decay, caps, hints and trust filtering."""
from __future__ import annotations

from app.services import experience_bank as bank
from app.services.experience_bank import (
    experience_hints,
    experience_summary,
    record_audit_issues,
)

LEAK = {
    "category": "人物记忆与能力/知情越界", "checker": "character", "severity": "high",
    "message": "林默说出了只有沈青知道的暗号", "evidence_verified": True,
    "evidence": [{"quote": "林默低声报出了暗号", "verified": True}],
}
REPEAT = {"category": "复用已有描写", "severity": "medium", "message": "再次使用冷铁比喻",
          "source": "local"}


def _entry(project, model="m"):
    return project["memory"]["experience_bank"][model]


def test_aggregates_per_model_and_pattern():
    project: dict = {}
    record_audit_issues(project, "m", [LEAK, REPEAT], chapter_id="c1")
    record_audit_issues(project, "m", [LEAK], chapter_id="c2")
    record_audit_issues(project, "other", [REPEAT], chapter_id="c1")
    patterns = _entry(project)["patterns"]
    assert patterns["knowledge_leak"]["count"] == 2
    assert patterns["repetition"]["count"] == 1
    assert _entry(project, "other")["patterns"]["repetition"]["count"] == 1
    assert "knowledge_leak" not in _entry(project, "other")["patterns"]


def test_same_pattern_counts_once_per_round():
    project: dict = {}
    record_audit_issues(project, "m", [LEAK, dict(LEAK, message="另一处越界")], chapter_id="c1")
    assert _entry(project)["patterns"]["knowledge_leak"]["count"] == 1


def test_ignores_unverified_model_claims():
    project: dict = {}
    unverified = dict(LEAK, evidence_verified=False)
    no_flag_model = {"category": "视角越界", "severity": "high", "message": "写了他人内心"}
    record_audit_issues(project, "m", [unverified, no_flag_model], chapter_id="c1")
    assert _entry(project)["patterns"] == {}


def test_decay_once_per_new_chapter_and_forgetting():
    project: dict = {}
    record_audit_issues(project, "m", [REPEAT], chapter_id="c1")
    record_audit_issues(project, "m", [], chapter_id="c1")  # re-audit: no decay
    assert _entry(project)["patterns"]["repetition"]["weight"] == 1.0
    record_audit_issues(project, "m", [], chapter_id="c2")
    assert _entry(project)["patterns"]["repetition"]["weight"] == round(bank.DECAY, 4)
    for number in range(3, 20):
        record_audit_issues(project, "m", [], chapter_id=f"c{number}")
    assert "repetition" not in _entry(project)["patterns"]
    assert len(_entry(project)["chapters"]) == bank.CHAPTER_WINDOW


def test_pattern_and_model_caps(monkeypatch):
    monkeypatch.setattr(bank, "MAX_PATTERNS", 3)
    monkeypatch.setattr(bank, "MAX_MODELS", 2)
    project: dict = {}
    issues = [{"category": f"自定义问题{index}", "source": "local", "severity": "low"} for index in range(6)]
    issues.append(dict(LEAK))
    record_audit_issues(project, "m", issues, chapter_id="c1")
    patterns = _entry(project)["patterns"]
    assert len(patterns) == 3
    assert "knowledge_leak" in patterns  # heaviest pattern survives the cap
    record_audit_issues(project, "a", [REPEAT], chapter_id="c1")
    record_audit_issues(project, "b", [REPEAT], chapter_id="c1")
    assert len(project["memory"]["experience_bank"]) == 2


def test_hints_need_recurrence_and_are_fixed_templates():
    project: dict = {}
    record_audit_issues(project, "m", [LEAK, REPEAT], chapter_id="c1")
    assert experience_hints(project, "m") == []
    record_audit_issues(project, "m", [LEAK, REPEAT], chapter_id="c2")
    hints = experience_hints(project, "m")
    assert hints[0].startswith("【人物知情越界】动笔前核对每个角色的知情范围")
    # Examples are UI-only: quoted prose never enters the writer prompt.
    assert "林默低声报出了暗号" not in "".join(hints)
    assert experience_summary(project, "m")["patterns"][0]["example"] == "林默低声报出了暗号"
    assert any(hint.startswith("【重复与套话】") for hint in hints)
    assert experience_hints(project, "m", limit=1) == hints[:1]
    assert experience_hints(project, "unknown") == []


def test_hints_are_deterministic_and_summary_shape():
    project: dict = {}
    for chapter in ("c1", "c2", "c3"):
        record_audit_issues(project, "m", [REPEAT, LEAK], chapter_id=chapter)
    assert experience_hints(project, "m") == experience_hints(project, "m")
    summary = experience_summary(project, "m")
    assert summary["model"] == "m" and summary["rounds"] == 3 and summary["chapters"] == 3
    assert summary["patterns"][0]["kind"] == "knowledge_leak"
    assert summary["patterns"][0]["label"] == "人物知情越界"
    assert summary["hints"] == experience_hints(project, "m")


def test_specific_subcategory_wins_over_checker_label():
    project: dict = {}
    issue = {"category": "事实细节/复用近期描写", "checker": "facts", "source": "local"}
    record_audit_issues(project, "m", [issue], chapter_id="c1")
    assert "repetition" in _entry(project)["patterns"]

"""Regression tests for defects found in the P0 review."""
from __future__ import annotations

import asyncio
import time

from app.story.evidence import (
    bind_evidence,
    normalize_model_score,
    split_draft_units,
    stable_audit_result,
)
from app.review.manuscript_quality import (
    MODERN_JARGON,
    jargon_hits,
    learn_fatigue_phrases,
    refresh_learned_fatigue,
)
import app.review.manuscript_quality as manuscript_quality
from app.llm.telemetry import workload_health
from app.review.editorial_policy import refinement_convergence
from app.story.memory_extraction import MEMORY_STAGES, extract_staged_memory, memory_time_budget
from app.review.memory_settlement import _apply_director_memory


# --- evidence numbering -----------------------------------------------------

def test_ellipsis_and_stacked_terminators_stay_in_one_sentence():
    units = split_draft_units("他沉默了……很久……终于开口。真的？！好。")
    sentences = [sentence for _id, _start, sentence in units]
    assert sentences == ["他沉默了……", "很久……", "终于开口。", "真的？！", "好。"]
    assert all(sentence.strip("…") for sentence in sentences)


def test_overlong_unpunctuated_paragraph_is_split_at_semicolons():
    paragraph = "；".join(f"第{index}个分句写得很长很长很长很长很长" for index in range(20))
    units = split_draft_units(paragraph)
    assert len(units) > 1
    assert all(len(sentence) <= 240 for _id, _start, sentence in units)


def test_range_evidence_is_a_verbatim_slice_across_paragraphs():
    text = "甲说。乙说。\n丙走了。丁来了。"
    issue = bind_evidence(text, {"evidence_ids": ["P1-S2~P2-S1"], "severity": "medium"})
    quote = issue["evidence"][0]["quote"]
    assert quote == "乙说。\n丙走了。"
    assert text.find(quote) == issue["evidence"][0]["start"]


# --- score normalization ----------------------------------------------------

def test_low_score_with_high_issue_is_not_rescaled():
    assert normalize_model_score(8, [{"severity": "high"}]) == 8
    assert normalize_model_score(8, []) == 80  # 10-point provider quirk still handled


def test_local_hundred_point_scores_are_never_rescaled():
    assert normalize_model_score(10, [], scale=100) == 10
    assert normalize_model_score(7, [{"severity": "high"}], scale=10) == 70


def test_stable_audit_does_not_inflate_focused_or_fallback_scores():
    text = "他推开门。" * 40
    local = {"score": 100, "verdict": "pass", "issues": []}
    focused = stable_audit_result(
        text, {"score": 10, "verdict": "revise", "issues": [], "critic_mode": "focused"}, local
    )
    assert focused["score_components"]["provider_score"] == 10
    fallback = stable_audit_result(
        text, {"score": 9, "verdict": "partial", "issues": [], "fallback": True}, local
    )
    assert fallback["score_components"]["provider_score"] == 9


# --- refinement convergence -------------------------------------------------

def test_shrinking_failure_count_is_progress_not_stall():
    rounds = [
        {"score": 60, "failures": ["现代抽象术语共 90 次，超过定稿线 20 次"]},
        {"score": 70, "failures": ["现代抽象术语共 60 次，超过定稿线 20 次"]},
    ]
    result = refinement_convergence(
        rounds, {"score": 80, "failures": ["现代抽象术语共 30 次，超过定稿线 20 次"]}
    )
    assert not result["stalled"]


def test_window_restarts_after_needs_author_stop():
    before_author = [
        {"score": 70, "failures": ["现代抽象术语共 83 次"]},
        {"score": 70, "failures": ["现代抽象术语共 83 次"]},
        {"score": 70, "failures": ["现代抽象术语共 83 次"], "release_status": "needs_author"},
    ]
    # First forced/edited round after the stop: not enough fresh rounds yet.
    result = refinement_convergence(before_author, {"score": 70, "failures": ["现代抽象术语共 82 次"]})
    assert not result["stalled"]
    fresh = before_author + [
        {"score": 70, "failures": ["现代抽象术语共 82 次"]},
        {"score": 70, "failures": ["现代抽象术语共 82 次"]},
    ]
    assert refinement_convergence(fresh, {"score": 70, "failures": ["现代抽象术语共 82 次"]})["stalled"]


# --- jargon weighting -------------------------------------------------------

def test_modern_pov_narration_weight_is_one_third_of_total_not_per_term_floor():
    terms = [term for term in MODERN_JARGON if not any(
        other != term and other in term for other in MODERN_JARGON
    ) and not any(other != term and term in other for other in MODERN_JARGON)][:10]
    text = "。".join(f"{term}在他脑中闪过" for term in terms) * 2
    raw = sum(count for _term, count in jargon_hits(text, None))
    weighted = sum(count for _term, count in jargon_hits(text, {"pov_modern_allowed": True}))
    assert raw == 20
    assert weighted == round(raw / 3)


# --- learned fatigue --------------------------------------------------------

def _fatigue_book(accepted: bool) -> dict:
    chapters = []
    for index in range(12):
        body = "\n".join(
            f"第{index}章第{line}段，雨声敲着窗棂，他把茶杯推到桌角，心里像压着一块冷铁。"
            for line in range(40)
        )
        chapter = {"id": f"c{index}", "title": f"第{index}章", "content": body}
        if accepted:
            chapter["settlement"] = {"content_hash": "x"}
        chapters.append(chapter)
    return {"chapters": chapters, "characters": [], "memory": {}}


def test_unaccepted_drafts_do_not_teach_fatigue_phrases():
    assert refresh_learned_fatigue(_fatigue_book(accepted=False)) == []
    assert refresh_learned_fatigue(_fatigue_book(accepted=True))


def test_learned_fatigue_is_memoized(monkeypatch):
    project = _fatigue_book(accepted=True)
    rows = [{"id": c["id"], "content": c["content"]} for c in project["chapters"]]
    first = learn_fatigue_phrases(rows, project)
    calls = []
    original = manuscript_quality._learn_fatigue_phrases
    monkeypatch.setattr(
        manuscript_quality, "_learn_fatigue_phrases",
        lambda *args, **kwargs: calls.append(1) or original(*args, **kwargs),
    )
    assert learn_fatigue_phrases(rows, project) == first
    assert calls == []
    rows[0] = {**rows[0], "content": rows[0]["content"] + "新的一段。"}
    learn_fatigue_phrases(rows, project)
    assert calls == [1]


# --- staged memory extraction -----------------------------------------------

def _stage_payload(messages):
    instruction = messages[-1]["content"]
    if "摘要与场景结算" in instruction:
        return {"summary": "摘要", "story_so_far": "进展"}
    return {}


def test_staged_memory_trims_to_caps_not_double():
    async def fake(settings, messages, **kwargs):
        if "原子事实" in messages[-1]["content"]:
            return {"facts": [{"text": str(index)} for index in range(20)]}, []
        return _stage_payload(messages), []

    result = asyncio.run(extract_staged_memory({}, "S", {}, completion=fake))
    facts_cap = next(stage for stage in MEMORY_STAGES if stage.name == "facts").caps["facts"]
    assert len(result["facts"]) == facts_cap


def test_staged_memory_returns_completed_stages_when_budget_runs_out():
    async def fake(settings, messages, **kwargs):
        if "摘要与场景结算" not in messages[-1]["content"]:
            await asyncio.sleep(5)
        return _stage_payload(messages), []

    async def run():
        with memory_time_budget(0.3):
            return await extract_staged_memory({}, "S", {}, completion=fake)

    started = time.monotonic()
    result = asyncio.run(run())
    assert time.monotonic() - started < 3
    assert result["summary"] == "摘要"
    assert result["memory_mode"] == "staged_partial"
    assert set(result["failed_stages"]) == {stage.name for stage in MEMORY_STAGES if not stage.required}


def test_failed_stage_is_reported_in_failed_stages():
    async def fake(settings, messages, **kwargs):
        if "伏笔" in messages[-1]["content"]:
            raise ValueError("模型返回的 JSON 未闭合")
        return _stage_payload(messages), []

    result = asyncio.run(extract_staged_memory({}, "S", {}, completion=fake))
    assert result["failed_stages"] == ["threads"]


def test_partial_extraction_is_not_settled_as_complete():
    content = "林默推开门，看见桌上那封信。" * 20
    project = {
        "chapters": [{"id": "c1", "title": "第一章", "content": content}],
        "characters": [], "memory": {}, "planning": {"volumes": []},
    }
    chapter = project["chapters"][0]
    result = {
        "summary": "林默看见信。", "story_so_far": "林默看见信。",
        "character_updates": [], "facts": [], "plot_threads": [], "timeline": [],
        "relationship_updates": [], "continuity_notes": [], "description_updates": [],
        "item_updates": [], "injury_updates": [], "scene_settlement": {},
        "failed_stages": ["threads"],
    }
    warnings = _apply_director_memory(project, chapter, result)
    assert chapter["settlement"]["content_hash"] == ""
    assert chapter["settlement"]["incomplete_stages"] == ["threads"]
    assert chapter["execution"]["memory_needs_repair"] == ["threads"]
    assert any("threads" in item for item in warnings)

    result["failed_stages"] = []
    _apply_director_memory(project, chapter, result, rebuild_current_projection=True)
    assert chapter["settlement"]["content_hash"]
    assert "memory_needs_repair" not in chapter["execution"]


# --- telemetry --------------------------------------------------------------

def test_unclosed_json_and_uppercase_max_tokens_count_as_truncation():
    calls = (
        [{"workload": "extraction", "finish_reason": "json_unclosed", "status": "completed"}] * 2
        + [{"workload": "extraction", "finish_reason": "MAX_TOKENS", "status": "completed"}]
        + [{"workload": "extraction", "finish_reason": "json_closed", "status": "completed"}] * 2
    )
    health = workload_health(calls)["extraction"]
    assert health["length_truncated"] == 3
    assert health["length_alert"]


def test_json_mode_without_finish_reason_marks_unclosed_output(monkeypatch):
    import app.llm.client as llama_client
    from app.llm.telemetry import Call

    seen: list[str] = []
    monkeypatch.setattr(llama_client, "validate_context_budget", lambda *args: None)
    monkeypatch.setattr(Call, "finish", lambda self, error=None: seen.append(self.data["finish_reason"]))
    for output in ('{"facts": [1, 2', '{"facts": []}'):
        async def fake(settings, messages, _output=output, **kwargs):
            return _output

        monkeypatch.setattr(llama_client, "_chat_once", fake)
        asyncio.run(llama_client.chat_once({}, [{"role": "user", "content": "x"}], json_mode=True))
    assert seen == ["json_unclosed", "json_closed"]

"""Failure boundaries that protect long-form prose and its authoritative inputs."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.api.routers.generation as generation
from app.core.model_budget import (
    ContextBudgetError,
    fit_output_tokens,
    request_budget,
    estimate_tokens,
)
from app.core.model_completion import ModelOutputTruncatedError
from app.db import ProjectStore
from app.llama_client import ModelContentFilteredError
from app.prompts import PromptSection, _fit_sections, build_prompt
from app.services import structured_output
from app.services.prose_recovery import receive_prose
from app.domain.prose import _clean_repair_continuation
from app.writing_skills import WRITING_SKILL_BOUNDARY, writing_skill_blocks


SETTINGS = {
    "model": "test",
    "context_budget": 24000,
    "max_tokens": 800,
    "director_stream_recovery_attempts": 1,
}
MESSAGES = [{"role": "user", "content": "沿当前因果完成场景，不增加新的事件。"}]
FIRST = "雨水顺着旧窗的裂纹流下，许澄逐一核对纸纹与墨迹，终于在潮汐表背面看见码头编号。"
LAST = "她折好信纸，披上外套，推门走进雨中。"


def finish(outcome, reason="stop"):
    outcome.observe({"choices": [{"finish_reason": reason}]})


def run_receiver(stream, saved, *, settings=None, messages=None, checkpoint=None, running=None):
    return asyncio.run(
        receive_prose(
            settings or SETTINGS,
            messages or MESSAGES,
            stream=stream,
            checkpoint=checkpoint,
            save_checkpoint=lambda item: saved.append(deepcopy(item)),
            is_running=running or (lambda: True),
            notify=lambda *_: None,
        )
    )


def test_partial_stream_survives_database_roundtrip_and_resumes_without_echo(tmp_path):
    saved = []

    async def broken(_settings, _messages, **_kwargs):
        yield FIRST
        raise httpx.ReadError("simulated disconnect")

    with pytest.raises(httpx.ReadError):
        run_receiver(broken, saved)
    store = ProjectStore(tmp_path / "checkpoint.db")
    project = store.create("检查点")
    task = store.create_director_task(project["id"], {})
    task["prose_stream_checkpoint"] = saved[-1]
    store.save_director_task(task["id"], task)
    checkpoint = ProjectStore(store.path).get_director_task(task["id"])["prose_stream_checkpoint"]

    async def resumed(settings, messages, *, outcome):
        assert messages[-2] == {"role": "assistant", "content": FIRST}
        assert request_budget(settings, messages, settings["max_tokens"]).fits
        yield FIRST + LAST  # Provider echoes the saved suffix; it must appear once.
        finish(outcome)

    result = run_receiver(resumed, saved, checkpoint=checkpoint)
    assert result == FIRST + "\n\n" + LAST
    assert result.count(FIRST) == 1
    assert saved[-1]["complete"] is True
    assert project["chapters"][0]["content"] == ""


def test_length_receipt_triggers_continuation_even_at_full_stop():
    calls = []
    saved = []

    async def stream(settings, messages, *, outcome):
        calls.append(messages)
        yield FIRST if len(calls) == 1 else LAST
        finish(outcome, "length" if len(calls) == 1 else "stop")

    result = run_receiver(
        stream, saved, settings={**SETTINGS, "director_stream_recovery_attempts": 2}
    )
    assert len(calls) == 2 and result.endswith(LAST)
    assert any(item["finish_reason"] == "length" and not item["complete"] for item in saved)
    assert saved[-1]["complete"]


def test_exhausted_truncation_remains_an_incomplete_checkpoint():
    saved = []

    async def stream(_settings, _messages, *, outcome):
        yield FIRST
        finish(outcome, "length")

    with pytest.raises(ModelOutputTruncatedError):
        run_receiver(stream, saved)
    assert saved[-1]["draft"] == FIRST and saved[-1]["complete"] is False


def test_stop_receipt_with_unfinished_sentence_continues_without_splitting_it():
    calls, saved = [], []

    async def stream(_settings, _messages, *, outcome):
        calls.append(1)
        yield "她终于看见" if len(calls) == 1 else "码头编号。"
        finish(outcome)

    result = run_receiver(
        stream, saved, settings={**SETTINGS, "director_stream_recovery_attempts": 2}
    )
    assert result == "她终于看见码头编号。"
    assert any(item["finish_reason"] == "incomplete_sentence" for item in saved)


def test_pause_after_last_piece_does_not_commit_completion():
    saved, state = [], {"running": True}

    async def stream(_settings, _messages, *, outcome):
        yield FIRST
        state["running"] = False
        finish(outcome)

    with pytest.raises(asyncio.CancelledError):
        run_receiver(stream, saved, running=lambda: state["running"])
    assert saved[-1]["draft"] == FIRST and not saved[-1]["complete"]


def test_missing_terminal_is_not_a_complete_checkpoint():
    saved = []

    async def stream(*_args, **_kwargs):
        yield FIRST

    with pytest.raises(httpx.ReadError, match="完成标记"):
        run_receiver(stream, saved)
    assert not saved[-1]["complete"]


def test_pause_saves_partial_sentence_then_continues_at_the_break():
    saved, state = [], {"running": True}

    async def paused(*_args, **_kwargs):
        state["running"] = False
        yield "她终于看见"

    with pytest.raises(asyncio.CancelledError):
        run_receiver(paused, saved, running=lambda: state["running"])
    assert saved[-1]["draft"] == "她终于看见"

    async def resumed(_settings, _messages, *, outcome):
        yield "码头编号。"
        finish(outcome)

    assert run_receiver(resumed, saved, checkpoint=saved[-1]) == "她终于看见码头编号。"


def test_filtered_partial_is_not_reusable():
    saved = []

    async def filtered(*_args, **_kwargs):
        yield FIRST
        raise ModelContentFilteredError("blocked")

    with pytest.raises(ModelContentFilteredError):
        run_receiver(filtered, saved)
    assert saved[-1]["draft"] == "" and saved[-1]["finish_reason"] == "content_filtered"


def test_completed_scene_receipts_reuse_only_identical_inputs():
    saved = []

    async def initial(_settings, _messages, *, outcome):
        yield FIRST
        finish(outcome)

    run_receiver(initial, saved)
    receipt = json.loads(json.dumps(saved[-1]))

    async def forbidden(*_args, **_kwargs):
        pytest.fail("identical completed input was regenerated")
        yield ""

    assert run_receiver(forbidden, [], checkpoint={"receipts": [receipt]}) == FIRST

    async def changed(_settings, messages, *, outcome):
        assert not any(item["role"] == "assistant" for item in messages)
        yield LAST
        finish(outcome)

    assert (
        run_receiver(
            changed, [], checkpoint=receipt, messages=[{"role": "user", "content": "改变后的路线"}]
        )
        == LAST
    )
    assert (
        run_receiver(
            changed, [], checkpoint=receipt, settings={**SETTINGS, "base_url": "http://other/v1"}
        )
        == LAST
    )


def test_resume_overflow_does_not_drop_saved_prose_or_call_model():
    saved = []
    settings = {**SETTINGS, "context_budget": 1400, "max_tokens": 500}

    async def broken(*_args, **_kwargs):
        yield "雨" * 2000
        raise httpx.ReadError("disconnect")

    with pytest.raises(httpx.ReadError):
        run_receiver(broken, saved, settings=settings)
    checkpoint = deepcopy(saved[-1])

    async def forbidden(*_args, **_kwargs):
        pytest.fail("over-budget continuation reached provider")
        yield ""

    with pytest.raises(ContextBudgetError):
        run_receiver(forbidden, saved, settings=settings, checkpoint=checkpoint)
    assert saved[-1] == checkpoint


def test_budget_reduction_preserves_messages_and_minimum_useful_output():
    messages = [{"role": "user", "content": "雨" * 700}]
    before = deepcopy(messages)
    settings = {"context_budget": 1400}
    fitted = fit_output_tokens(settings, messages, 1200)
    assert 256 <= fitted < 1200
    assert request_budget(settings, messages, fitted).fits and messages == before
    with pytest.raises(ContextBudgetError):
        fit_output_tokens(settings, [{"content": "雨" * 2000}], 1200)
    assert not request_budget(settings, [{"content": "雨" * 2000}], 0).fits


def test_unicode_budget_does_not_discount_chinese_or_emoji_as_ascii():
    assert estimate_tokens("雨" * 1000) >= 1000
    assert estimate_tokens("🌙" * 100) >= 200


def test_required_rule_tail_survives_caps_and_overflow(tmp_path):
    store = ProjectStore(tmp_path / "rules.db")
    project = store.create()
    project["book_rules"] = "必须服从因果。\n" * 1600 + "最终硬约束：绝不提前打开密封信。"
    project["settings"].update(context_budget=4000, max_tokens=1200)
    build = build_prompt(project, {"chapter_id": project["chapters"][0]["id"]})
    rules = next(item for item in build.sections if item["name"] == "本书不可违背规则")
    assert rules["content"] == project["book_rules"] and rules["status"] == "included"
    assert not request_budget(project["settings"], build.messages, 1200).fits


def test_skill_budget_never_injects_a_partial_method_or_example():
    skills = [
        {
            "name": "长方法",
            "instructions": "分解人物动机。" * 1800,
            "positive_example": "长方法正例末尾",
        },
        {
            "name": "短方法",
            "instructions": "用行动表达人物的犹豫。",
            "positive_example": "她松开门把手。",
        },
    ]
    blocks = writing_skill_blocks(skills)
    section = PromptSection(
        "激活写作 Skills",
        "\n\n".join([WRITING_SKILL_BOUNDARY, *blocks]),
        1,
        atomic_units=tuple(blocks),
        atomic_prefix=WRITING_SKILL_BOUNDARY,
    )
    fitted, _ = _fit_sections([section], 180)
    assert blocks[1] in fitted[0].content
    assert "长方法" not in fitted[0].content
    assert "她松开门把手。" in fitted[0].content


def test_continuation_cleanup_preserves_paragraphs_and_collapses_only_exact_loops():
    continuation = FIRST + "\n\n" + LAST
    assert _clean_repair_continuation("", continuation) == continuation
    long_block = FIRST + LAST + "门后的灯光逐渐暗了下去。"
    assert _clean_repair_continuation("", long_block + "\n\n" + long_block) == long_block


def test_structured_retry_recalculates_budget_for_repair_instruction(monkeypatch):
    calls = []

    async def fake(settings, messages, **kwargs):
        calls.append((deepcopy(messages), kwargs["max_tokens"]))
        assert request_budget(settings, messages, kwargs["max_tokens"]).fits
        return '{"broken":' if len(calls) == 1 else '{"ok": true}'

    monkeypatch.setattr(structured_output, "chat_once", fake)
    result, warnings = asyncio.run(
        structured_output.structured_completion(
            {"context_budget": 1400},
            [{"role": "user", "content": "雨" * 700}],
            max_tokens=1200,
            timeout_seconds=10,
            temperature=0.2,
        )
    )
    assert result["ok"] and len(calls) == 2
    assert calls[1][1] < calls[0][1] and warnings


def test_structured_filter_is_not_retried(monkeypatch):
    calls = []

    async def blocked(*_args, **_kwargs):
        calls.append(1)
        raise ModelContentFilteredError("blocked")

    monkeypatch.setattr(structured_output, "chat_once", blocked)
    with pytest.raises(ModelContentFilteredError):
        asyncio.run(
            structured_output.structured_completion(
                {}, MESSAGES, max_tokens=800, timeout_seconds=10, temperature=0.2
            )
        )
    assert len(calls) == 1


def test_generation_cap_at_sentence_end_continues_and_marks_unresolved_candidate(
    monkeypatch, tmp_path
):
    store = ProjectStore(tmp_path / "generate.db")
    monkeypatch.setattr(main, "store", store)
    project = store.create("未完成的候选")
    calls = []

    async def stream(_settings, messages, *, outcome):
        calls.append(messages)
        yield FIRST * 4 if len(calls) == 1 else LAST * 4
        finish(outcome, "length")

    monkeypatch.setattr(generation, "chat_stream", stream)
    with TestClient(main.app) as client:
        response = client.post(
            "/api/generate",
            json={
                "project": project,
                "chapter_id": project["chapters"][0]["id"],
                "target_words": 100,
            },
        )
    events = [
        json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")
    ]
    assert response.status_code == 200 and len(calls) >= 2
    assert events[-1]["type"] == "done" and events[-1]["completion_state"] == "needs_review"
    assert events[-1]["finish_reason"] == "length"
    assert store.get(project["id"])["chapters"][0]["content"] == ""
    assert events[0]["request_budget"]["fits"]


def test_generation_error_keeps_partial_tokens_without_done(monkeypatch, tmp_path):
    store = ProjectStore(tmp_path / "error.db")
    monkeypatch.setattr(main, "store", store)
    project = store.create()

    async def stream(*_args, **_kwargs):
        yield FIRST
        raise httpx.ReadError("provider disconnected")

    monkeypatch.setattr(generation, "chat_stream", stream)
    with TestClient(main.app) as client:
        response = client.post(
            "/api/generate",
            json={
                "project": project,
                "chapter_id": project["chapters"][0]["id"],
                "target_words": 100,
            },
        )
    events = [
        json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")
    ]
    assert any(item.get("text") == FIRST for item in events)
    assert events[-1]["type"] == "error" and not any(item["type"] == "done" for item in events)


def test_prompt_preview_exposes_final_budget_and_preserves_rule_tail(monkeypatch, tmp_path):
    store = ProjectStore(tmp_path / "preview.db")
    monkeypatch.setattr(main, "store", store)
    project = store.create()
    project["book_rules"] = "约束必须始终成立。" * 1400 + "规则末尾不可丢失"
    project["settings"].update(context_budget=4000, max_tokens=1200)
    with TestClient(main.app) as client:
        response = client.post(
            "/api/prompt/preview",
            json={
                "project": project,
                "chapter_id": project["chapters"][0]["id"],
                "target_words": 100,
            },
        )
    data = response.json()
    assert response.status_code == 200
    assert (
        not data["request_budget"]["fits"] and data["request_budget"]["token_source"] == "estimated"
    )
    assert "规则末尾不可丢失" in str(data["messages"])

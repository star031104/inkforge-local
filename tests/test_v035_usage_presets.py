"""Real usage/cost telemetry (task 6) and endpoint parameter presets (task 8)."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.llm import client as llama_client, telemetry as model_telemetry, providers
from app.core.config import APP_VERSION
from app.storage.project_store import ProjectStore

DEEPSEEK = {"model": "deepseek-flash", "base_url": "https://api.deepseek.com"}
LOCAL = {"model": "qwen3-8b", "base_url": "http://127.0.0.1:8080/v1"}
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def transport(tmp_path, monkeypatch):
    monkeypatch.setattr(model_telemetry, "_path", None)
    model_telemetry.configure(tmp_path / "calls.db")
    monkeypatch.setattr(providers, "_STREAM_OPTIONS_REJECTED", set())
    original = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(llama_client.httpx, "AsyncClient", lambda **kwargs: original(
            **kwargs, transport=httpx.MockTransport(handler)))
    return install


def _wire(events):
    return "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"


def _stream(settings, messages=None):
    async def consume():
        return "".join([p async for p in llama_client.chat_stream(settings, messages or [])])
    return asyncio.run(consume())


# ---------------------------------------------------------------- usage capture

def test_deepseek_stream_records_cache_reasoning_and_cost(transport):
    sent = []
    usage = {"prompt_tokens": 1000, "completion_tokens": 200, "prompt_cache_hit_tokens": 800,
             "prompt_cache_miss_tokens": 200, "completion_tokens_details": {"reasoning_tokens": 50}}

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text=_wire([
            {"choices": [{"delta": {"content": "雨停了。"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": usage},
        ]))
    transport(handler)
    with model_telemetry.usage_scope("p1", "c1"):
        assert _stream(DEEPSEEK) == "雨停了。"
    assert sent[0]["stream_options"] == {"include_usage": True}
    call = model_telemetry.report()["calls"][0]
    assert call["token_source"] == "usage"
    assert (call["prompt_tokens"], call["completion_tokens"]) == (1000, 200)
    assert (call["cache_hit_tokens"], call["cache_miss_tokens"], call["reasoning_tokens"]) == (800, 200, 50)
    assert (call["project_id"], call["chapter_id"]) == ("p1", "c1")
    assert call["price_family"] == "deepseek-flash" and call["cost_usd"] > 0


def test_cost_uses_peak_and_off_peak_prices():
    base = {"token_source": "usage", "model": "deepseek-flash", "prompt_tokens": 1_000_000,
            "cache_hit_tokens": 500_000, "cache_miss_tokens": 500_000, "completion_tokens": 1_000_000}
    # Monday 02:00 UTC is peak; Monday 12:00 UTC and Saturday 02:00 are off-peak.
    peak = model_telemetry.call_cost({**base, "created_at": "2026-10-19T02:00:00+00:00"})
    off = model_telemetry.call_cost({**base, "created_at": "2026-10-19T12:00:00+00:00"})
    weekend = model_telemetry.call_cost({**base, "created_at": "2026-10-24T02:00:00+00:00"})
    assert off == pytest.approx(0.5 * 0.003 + 0.5 * 0.15 + 0.6)
    assert peak == pytest.approx(0.5 * 0.006 + 0.5 * 0.3 + 1.2)
    assert weekend == off
    assert model_telemetry.is_peak(datetime(2026, 10, 20, 7, tzinfo=timezone.utc))
    assert not model_telemetry.is_peak(datetime(2026, 10, 20, 5, tzinfo=timezone.utc))
    # Estimates, unknown models and families without bundled prices are never priced.
    assert model_telemetry.call_cost({**base, "token_source": "estimated"}) is None
    assert model_telemetry.call_cost({**base, "model": "qwen3-8b"}) is None
    assert model_telemetry.call_cost({**base, "model": "deepseek-v4-pro"}) is None


def test_llama_cpp_timings_and_no_stream_options(transport):
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, text=_wire([
            {"choices": [{"delta": {"content": "夜色。"}, "finish_reason": "stop"}],
             "timings": {"prompt_n": 300, "cache_n": 700, "predicted_n": 40}},
        ]))
    transport(handler)
    assert _stream(LOCAL) == "夜色。"
    assert "stream_options" not in sent[0]
    call = model_telemetry.report()["calls"][0]
    assert call["token_source"] == "usage" and call["prompt_tokens"] == 1000
    assert (call["cache_hit_tokens"], call["cache_miss_tokens"], call["completion_tokens"]) == (700, 300, 40)
    assert call["cost_usd"] is None


def test_rejected_stream_options_is_retried_and_remembered(transport):
    sent = []

    def handler(request):
        body = json.loads(request.content)
        sent.append(body)
        if "stream_options" in body:
            return httpx.Response(400, json={"error": {"message": "unknown field: stream_options"}})
        return httpx.Response(200, text=_wire([{"choices": [{"delta": {"content": "好。"}, "finish_reason": "stop"}]}]))
    transport(handler)
    settings = {"model": "m", "base_url": "https://llm.example.com/v1", "request_retry_attempts": 1}
    assert _stream(settings) == "好。"
    assert ["stream_options" in body for body in sent] == [True, False]
    assert _stream(settings) == "好。"
    assert len(sent) == 3 and "stream_options" not in sent[2]


def test_json_mode_drains_final_usage_chunk(transport):
    transport(lambda request: httpx.Response(200, text=_wire([
        {"choices": [{"delta": {"content": '{"ok": true}'}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 90, "completion_tokens": 6, "prompt_cache_hit_tokens": 64}},
    ])))
    text = asyncio.run(llama_client.chat_once(DEEPSEEK, [{"role": "user", "content": "json"}], json_mode=True))
    assert json.loads(text) == {"ok": True}
    call = model_telemetry.report()["calls"][0]
    assert call["token_source"] == "usage" and call["cache_hit_tokens"] == 64
    assert call["cache_miss_tokens"] == 26


# ---------------------------------------------------------------- aggregates

def test_project_usage_aggregates_by_chapter_and_workload(tmp_path, monkeypatch):
    monkeypatch.setattr(model_telemetry, "_path", None)
    model_telemetry.configure(tmp_path / "calls.db")

    def record(chapter, workload, hit, miss, out):
        with model_telemetry.usage_scope("proj", chapter):
            call = model_telemetry.Call({**DEEPSEEK, "_workload": workload}, [{"content": "x"}])
            model_telemetry.observe({"usage": {"prompt_tokens": hit + miss, "completion_tokens": out,
                                               "prompt_cache_hit_tokens": hit, "prompt_cache_miss_tokens": miss}})
            call.finish()

    record("c1", "prose", 900, 100, 500)
    record("c1", "critic", 500, 500, 100)
    record("c2", "prose", 0, 1000, 500)
    with model_telemetry.usage_scope("other", "x"):
        model_telemetry.Call(LOCAL, []).finish()
    usage = model_telemetry.project_usage("proj", last_chapters=1, chapter_id="c1")
    totals = usage["totals"]
    assert totals["calls"] == 3 and totals["usage_calls"] == 3
    assert totals["cache_hit_rate"] == pytest.approx(1400 / 3000, abs=1e-4)
    assert totals["cost_usd"] > 0 and totals["cost_complete"]
    assert usage["workloads"]["prose"]["calls"] == 2 and usage["workloads"]["critic"]["calls"] == 1
    assert usage["chapter"]["chapter_id"] == "c1" and usage["chapter"]["calls"] == 2
    assert usage["chapter"]["workloads"] == {"prose": 1, "critic": 1}
    assert [c["chapter_id"] for c in usage["recent_chapters"]] == ["c2"]
    assert {c["chapter_id"] for c in usage["chapters"]} == {"c1", "c2"}
    assert model_telemetry.project_usage("missing")["totals"]["calls"] == 0


def test_scope_binding_and_reset():
    assert model_telemetry.current_scope() == ("", "")
    with model_telemetry.usage_scope("p", "c"):
        assert model_telemetry.current_scope() == ("p", "c")
        model_telemetry.bind_scope("q")  # switching project clears the chapter
        assert model_telemetry.current_scope() == ("q", "")
    assert model_telemetry.current_scope() == ("", "")


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "test.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(model_telemetry, "_path", None)
    model_telemetry.configure(tmp_path / "calls.db")
    with TestClient(main.app) as test_client:
        yield test_client, store


def test_usage_endpoint_and_find_chapter_binding(client):
    test_client, store = client
    project = store.create("用量")
    chapter_id = project["chapters"][0]["id"]
    with model_telemetry.usage_scope():
        main.find_chapter(project, chapter_id)
        assert model_telemetry.current_scope() == (project["id"], chapter_id)
        call = model_telemetry.Call({**DEEPSEEK, "_workload": "prose"}, [])
        model_telemetry.observe({"usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        call.finish()
    data = test_client.get(f"/api/projects/{project['id']}/usage", params={"chapter_id": chapter_id}).json()
    assert data["totals"]["calls"] == 1 and data["chapter"]["chapter_id"] == chapter_id
    assert data["recent_chapters"][0]["chapter_id"] == chapter_id


# ---------------------------------------------------------------- presets

def test_recommended_settings_by_endpoint_kind():
    local = providers.recommended_settings({"base_url": "http://127.0.0.1:8080/v1", "context_budget": 64000})
    assert local["kind"] == "local"
    assert local["values"]["context_budget"] == 24000 and local["values"]["candidate_count"] == 1
    assert local["values"]["enable_thinking"] is False
    change = next(c for c in local["changes"] if c["field"] == "context_budget")
    assert (change["current"], change["recommended"]) == (64000, 24000) and change["reason"]
    assert providers.endpoint_kind({"base_url": "http://localhost:11434"}) == "local"

    deepseek = providers.recommended_settings({"base_url": "https://api.deepseek.com", "model": "deepseek-flash"})
    assert deepseek["kind"] == "deepseek"
    assert deepseek["values"] == {
        "temperature": 1.0, "context_budget": 64000, "max_tokens": 8192, "lore_budget": 8000,
        "memory_items": 20, "candidate_count": 2, "critic_budget": "auto",
        "dialogue_rehearsal": "auto", "enable_thinking": False,
    }
    assert set(deepseek["explanations"]) == set(deepseek["values"])

    cloud = providers.recommended_settings({"base_url": "https://llm.example.com/v1"})
    assert cloud["kind"] == "cloud"
    assert 24000 < cloud["values"]["context_budget"] < 64000
    same = providers.recommended_settings({"base_url": "https://llm.example.com/v1", **cloud["values"]})
    assert same["changes"] == []


def test_recommended_settings_endpoint_reports_kind_change(client):
    test_client, _store = client
    body = {"settings": {"base_url": "https://api.deepseek.com", "api_key": "secret"},
            "previous_base_url": "http://127.0.0.1:8080/v1"}
    data = test_client.post("/api/model/recommended-settings", json=body).json()
    assert data["kind"] == "deepseek" and data["previous_kind"] == "local" and data["kind_changed"]
    assert "secret" not in json.dumps(data)
    same = test_client.post("/api/model/recommended-settings", json={
        "settings": {"base_url": "https://api.deepseek.com"}, "previous_base_url": "https://llm.example.com/v1"}).json()
    assert same["kind_changed"] is False


def test_ui_exposes_usage_api_and_preset_button():
    index = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert f"/assets/js/core/usage.js?v={APP_VERSION}" in index
    assert index.index("usage.js") < index.index("dialogs.js")
    usage_js = (ROOT / "static" / "js" / "core" / "usage.js").read_text(encoding="utf-8")
    assert "window.inkforgeUsage" in usage_js and "chapter(" in usage_js
    dialogs = (ROOT / "static" / "js" / "core" / "dialogs.js").read_text(encoding="utf-8")
    assert "应用推荐参数" in dialogs and "/api/model/recommended-settings" in dialogs
    assert "用量与成本" in dialogs

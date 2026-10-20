"""Single OpenAI-compatible endpoint: migration, routing, discovery, payloads."""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.llm import client as llama_client
from app.storage.project_store import ProjectStore
from app.storage.schema.project import ensure_project_defaults
from app.storage.secret_store import apply_project_secrets
from app.llm.providers import (
    PayloadOptions,
    build_chat_payload,
    migrate_model_settings,
    normalize_base_url,
    parse_models_payload,
    provider_summary,
    register_payload_extension,
    resolve_capabilities,
    resolved_api_key,
    settings_for_workload,
    unregister_payload_extension,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080/v1"),
        ("http://127.0.0.1:8080/", "http://127.0.0.1:8080/v1"),
        ("127.0.0.1:8080/v1/", "http://127.0.0.1:8080/v1"),
        ("https://api.example.com/v1/chat/completions", "https://api.example.com/v1"),
        ("https://api.example.com/v1/models", "https://api.example.com/v1"),
        ("https://open.bigmodel.cn/api/paas/v4", "https://open.bigmodel.cn/api/paas/v4"),
        ("  ", ""),
    ],
)
def test_base_url_normalisation(raw, expected):
    assert normalize_base_url(raw) == expected


def test_models_payload_formats():
    assert parse_models_payload({"data": [{"id": "a", "owned_by": "llamacpp"}, {"id": "a"}, {"x": 1}]}) == [
        {"id": "a", "owned_by": "llamacpp"}
    ]
    assert [m["id"] for m in parse_models_payload(["m1", {"id": "m2"}])] == ["m1", "m2"]
    assert [m["id"] for m in parse_models_payload({"models": [{"name": "qwen3:8b"}]})] == ["qwen3:8b"]
    assert parse_models_payload({"data": "broken"}) == []


def test_legacy_zhipu_single_slot_migrates_to_plain_url():
    settings = migrate_model_settings({"provider": "zhipu", "base_url": "", "model": "", "api_key": "no-key"})
    assert settings["provider"] == "openai_compatible"
    assert settings["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert settings["model"] == "glm-4.7-flash"
    assert settings["api_key"] == ""
    assert settings["planning_model"] == ""


def test_legacy_dual_same_endpoint_keeps_second_model_for_planning():
    settings = migrate_model_settings(
        {
            "model_routing": "dual",
            "provider": "openai_compatible",
            "base_url": "https://api.example.com/v1/",
            "model": "writer",
            "reasoning_provider": "openai_compatible",
            "reasoning_base_url": "https://api.example.com/v1",
            "reasoning_model": "planner",
            "role_routes": {},
        }
    )
    assert settings["planning_model"] == "planner"
    for legacy in ("model_routing", "reasoning_model", "reasoning_base_url", "role_routes"):
        assert legacy not in settings


def test_legacy_dual_with_only_secondary_key_adopts_secondary_endpoint():
    project = ensure_project_defaults(
        {
            "settings": {
                "model_routing": "dual",
                "provider": "zhipu",
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "model": "glm-4.7-flash",
                "reasoning_provider": "modelscope",
                "reasoning_base_url": "https://api-inference.modelscope.cn/v1",
                "reasoning_model": "ZhipuAI/GLM-5.2",
            }
        }
    )
    assert project["settings"]["model_migration"]["planning_model"] == "ZhipuAI/GLM-5.2"
    apply_project_secrets(project, {"settings.reasoning_api_key": "ms-key"})
    settings = project["settings"]
    assert settings["base_url"] == "https://api-inference.modelscope.cn/v1"
    assert settings["model"] == "ZhipuAI/GLM-5.2"
    assert settings["api_key"] == "ms-key"
    assert "reasoning_api_key" not in settings
    assert settings["model_migration"]["resolved"] is True


def test_legacy_dual_with_primary_key_keeps_primary_endpoint(tmp_path):
    store = ProjectStore(tmp_path / "p.db", tmp_path / "s.db")
    project = store.create("迁移")
    project["settings"].update(
        {
            "model_routing": "dual",
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "model": "glm-4.7-flash",
            "api_key": "primary-key",
            "reasoning_base_url": "https://api-inference.modelscope.cn/v1",
            "reasoning_model": "ZhipuAI/GLM-5.2",
            "reasoning_api_key": "secondary-key",
        }
    )
    saved = store.save(project["id"], project, reason="legacy")
    loaded = store.get(saved["id"])
    assert loaded["settings"]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
    assert loaded["settings"]["api_key"] == "primary-key"
    assert "reasoning_api_key" not in loaded["settings"]


def test_workload_routing_uses_planning_model_only_for_planning_family():
    settings = {"base_url": "http://x/v1", "model": "writer", "planning_model": "planner"}
    assert settings_for_workload(settings, "prose")["model"] == "writer"
    assert settings_for_workload(settings, "revision")["model"] == "writer"
    for role in ("planning", "critic", "extraction", "research"):
        assert settings_for_workload(settings, role)["model"] == "planner"
    single = {"base_url": "http://x/v1", "model": "writer", "planning_model": ""}
    assert settings_for_workload(single, "critic")["model"] == "writer"
    summary = provider_summary(settings)
    assert summary["routing_mode"] == "dual" and summary["planning_model"] == "planner"


def test_env_key_fallback(monkeypatch):
    monkeypatch.delenv("INKFORGE_API_KEY", raising=False)
    monkeypatch.setenv("ZHIPU_API_KEY", "legacy")
    assert resolved_api_key({"base_url": "https://open.bigmodel.cn/api/paas/v4"}) == "legacy"
    assert resolved_api_key({"base_url": "https://api.example.com/v1"}) == ""
    monkeypatch.setenv("INKFORGE_API_KEY", "generic")
    assert resolved_api_key({"base_url": "https://api.example.com/v1"}) == "generic"
    assert resolved_api_key({"base_url": "https://api.example.com/v1", "api_key": "own"}) == "own"


def test_payload_is_capability_gated():
    messages = [{"role": "user", "content": "x"}]
    generic = build_chat_payload(
        {"base_url": "https://api.example.com/v1", "model": "m", "repeat_penalty": 1.2},
        messages, stream=False, json_mode=True,
        options=PayloadOptions(json_schema={"type": "object"}, logit_bias={"深吸一口气": -10}),
    )
    assert "repeat_penalty" not in generic and "top_k" not in generic
    assert "logit_bias" not in generic and "cache_prompt" not in generic
    assert generic["response_format"] == {"type": "json_object"}

    llama_settings = {
        "base_url": "http://gpu-box:9000/v1",
        "model": "m",
        "capabilities": {"server": "llama_cpp", "detected": True, "base_url": "http://gpu-box:9000/v1",
                         "top_k": True, "min_p": True, "repeat_penalty": True, "cache_prompt": True,
                         "json_schema": True, "logit_bias": True, "chat_template_kwargs": True,
                         "reasoning_budget": True},
    }
    llama = build_chat_payload(
        llama_settings, messages, stream=True, json_mode=True,
        options=PayloadOptions(json_schema={"type": "object"}, logit_bias={"深吸一口气": -10}),
    )
    assert llama["repeat_penalty"] == 1.08 and llama["cache_prompt"] is True
    assert llama["json_schema"] == {"type": "object"} and "response_format" not in llama
    assert llama["logit_bias"] == [["深吸一口气", -10.0]]

    zhipu = build_chat_payload({"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm"}, messages, stream=False)
    assert zhipu["thinking"] == {"type": "disabled"}


def test_detected_capabilities_ignored_after_url_change():
    caps = resolve_capabilities(
        {"base_url": "https://other/v1", "capabilities": {"server": "llama_cpp", "detected": True, "base_url": "http://old/v1", "logit_bias": True}}
    )
    assert caps.server == "generic" and not caps.logit_bias


def test_payload_extension_hook():
    def mark(payload, caps, settings, options):
        payload["x_inkforge"] = caps.server

    register_payload_extension(mark)
    try:
        payload = build_chat_payload({"base_url": "https://api.example.com/v1", "model": "m"}, [], stream=False)
    finally:
        unregister_payload_extension(mark)
    assert payload["x_inkforge"] == "generic"


@pytest.fixture
def mock_http(monkeypatch):
    original = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(
            llama_client.httpx, "AsyncClient",
            lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler)),
        )

    return install


def test_probe_detects_llama_cpp_via_props(mock_http):
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/v1/models":
            assert request.headers.get("authorization") == "Bearer k"
            return httpx.Response(200, json={"data": [{"id": "qwen3-8b", "owned_by": "me"}]})
        if request.url.path == "/props":
            return httpx.Response(200, json={"default_generation_settings": {}, "total_slots": 1})
        return httpx.Response(404)

    mock_http(handler)
    result = asyncio.run(llama_client.probe_endpoint({"base_url": "http://gpu-box:9000", "api_key": "k"}))
    assert result["ok"] and result["base_url"] == "http://gpu-box:9000/v1"
    assert result["server"] == "llama_cpp" and result["capabilities"]["logit_bias"]
    assert [m["id"] for m in result["models"]] == ["qwen3-8b"]
    assert "/props" in seen


def test_probe_generic_server_and_owned_by_detection(mock_http):
    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "a", "owned_by": "vllm"}]})
        return httpx.Response(404)

    mock_http(handler)
    result = asyncio.run(llama_client.probe_endpoint({"base_url": "https://api.example.com/v1"}))
    assert result["server"] == "vllm" and result["capabilities"]["json_schema"]


@pytest.mark.parametrize(
    ("status", "kind"), [(401, "auth"), (404, "not_found"), (500, "server")]
)
def test_probe_explains_http_errors(mock_http, status, kind):
    mock_http(lambda request: httpx.Response(status, json={"error": {"message": "nope"}}))
    result = asyncio.run(llama_client.probe_endpoint({"base_url": "https://api.example.com/v1"}))
    assert not result["ok"] and result["error_kind"] == kind and result["message"]


def test_probe_explains_connection_errors(mock_http):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    mock_http(handler)
    result = asyncio.run(llama_client.probe_endpoint({"base_url": "http://127.0.0.1:1"}))
    assert not result["ok"] and result["error_kind"] == "connect" and "无法建立连接" in result["message"]


def test_probe_requires_url():
    result = asyncio.run(llama_client.probe_endpoint({"base_url": ""}))
    assert not result["ok"] and result["error_kind"] == "missing_url"


def test_models_test_endpoint(mock_http):
    import app.main as main

    mock_http(lambda request: httpx.Response(200, json={"data": [{"id": "m1"}]}))
    with TestClient(main.app) as client:
        response = client.post("/api/models/test", json={"base_url": "https://api.example.com/v1", "api_key": ""})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] and body["models"][0]["id"] == "m1"


def test_user_typed_siliconflow_or_xai_url_is_kept():
    for url in ("https://api.siliconflow.cn/v1", "https://api.x.ai/v1"):
        settings = migrate_model_settings(
            {"provider": "openai_compatible", "base_url": url, "model": "m", "api_key": "k"}
        )
        assert settings["base_url"] == url and settings["model"] == "m" and settings["api_key"] == "k"
    old_preset = migrate_model_settings({"provider": "siliconflow", "base_url": "", "model": "Qwen/Qwen3-32B"})
    assert old_preset["base_url"] == "https://api.siliconflow.cn/v1"
    assert old_preset["model"] == "Qwen/Qwen3-32B"


def test_unchanged_save_persists_legacy_migration_with_its_secrets(tmp_path):
    """A dual project whose only key was model 2's must keep that endpoint after a no-op save."""
    import json
    import sqlite3

    store = ProjectStore(tmp_path / "p.db", tmp_path / "s.db")
    project = store.create("旧两模型")
    # Write the pre-0.34 payload and secrets directly, as the old version stored them.
    legacy_settings = {
        **project["settings"],
        "provider": "zhipu",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4.7-flash",
        "api_key": "",
        "model_routing": "dual",
        "reasoning_provider": "modelscope",
        "reasoning_base_url": "https://api-inference.modelscope.cn/v1",
        "reasoning_model": "ZhipuAI/GLM-5.2",
    }
    for key in ("planning_model", "capabilities"):
        legacy_settings.pop(key, None)
    with sqlite3.connect(tmp_path / "p.db") as db:
        raw = json.loads(db.execute("SELECT payload FROM projects WHERE id = ?", (project["id"],)).fetchone()[0])
        raw["settings"] = legacy_settings
        db.execute("UPDATE projects SET payload = ? WHERE id = ?", (json.dumps(raw, ensure_ascii=False), project["id"]))
    store.secret_store.replace(project["id"], {"settings.reasoning_api_key": "ms-only"})

    loaded = ProjectStore(tmp_path / "p.db", tmp_path / "s.db").get(project["id"])
    assert loaded["settings"]["base_url"] == "https://api-inference.modelscope.cn/v1"
    store.save(loaded["id"], loaded, reason="noop", expected_updated_at=loaded["updated_at"])
    reloaded = ProjectStore(tmp_path / "p.db", tmp_path / "s.db").get(project["id"])["settings"]
    assert reloaded["base_url"] == "https://api-inference.modelscope.cn/v1"
    assert reloaded["model"] == "ZhipuAI/GLM-5.2"
    assert reloaded["api_key"] == "ms-only"

from app.providers import build_chat_payload, detect_provider, provider_summary


def test_siliconflow_qwen3_payload_uses_supported_fields_only():
    settings = {
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen3-8B",
        "enable_thinking": False,
        "min_p": 0.05,
        "top_k": 30,
        "repeat_penalty": 1.2,
    }
    payload = build_chat_payload(settings, [{"role": "user", "content": "test"}], stream=False)
    assert payload["enable_thinking"] is False
    assert payload["min_p"] == 0.05
    assert "chat_template_kwargs" not in payload
    assert "reasoning_budget" not in payload
    assert "repeat_penalty" not in payload


def test_llama_cpp_payload_keeps_local_specific_controls():
    settings = {"provider": "llama_cpp", "model": "local", "enable_thinking": False}
    payload = build_chat_payload(settings, [{"role": "user", "content": "test"}], stream=True)
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}
    assert payload["reasoning_budget"] == 0
    assert "repeat_penalty" in payload


def test_provider_detection_and_summary():
    settings = {"base_url": "https://api.siliconflow.cn/v1", "model": "Qwen/Qwen3-8B", "api_key": "secret"}
    assert detect_provider(settings) == "siliconflow"
    summary = provider_summary(settings)
    assert summary["has_api_key"] is True
    assert "secret" not in str(summary)


def test_xai_grok_profile_uses_official_endpoint_and_reasoning_control(monkeypatch):
    from app.db import ensure_project_defaults
    from app.providers import auth_headers

    monkeypatch.setenv("XAI_API_KEY", "xai-env-secret")
    project = ensure_project_defaults(
        {"settings": {"provider": "xai"}, "chapters": []}
    )
    settings = project["settings"]
    assert settings["base_url"] == "https://api.x.ai/v1"
    assert settings["model"] == "grok-4.6"
    assert detect_provider(settings) == "xai"
    assert auth_headers(settings)["Authorization"] == "Bearer xai-env-secret"
    payload = build_chat_payload(
        settings,
        [{"role": "user", "content": "写一段小说"}],
        stream=True,
    )
    assert payload["reasoning_effort"] == "low"
    assert "top_k" not in payload
    assert "enable_thinking" not in payload


def test_siliconflow_partial_settings_migrate_to_safe_cloud_defaults():
    from app.db import ensure_project_defaults
    project=ensure_project_defaults({"settings":{"provider":"siliconflow"},"chapters":[]})
    assert project["settings"]["base_url"]=="https://api.siliconflow.cn/v1"
    assert project["settings"]["model"]=="Qwen/Qwen3-8B"
    assert project["settings"]["api_key"]==""
    assert project["settings"]["max_tokens"]==3500


def test_siliconflow_api_key_can_come_from_environment(monkeypatch):
    from app.providers import auth_headers, provider_summary
    monkeypatch.setenv("INKFORGE_SILICONFLOW_API_KEY", "env-secret")
    settings={"provider":"siliconflow","base_url":"https://api.siliconflow.cn/v1","api_key":"","model":"Qwen/Qwen3-8B"}
    assert auth_headers(settings)["Authorization"]=="Bearer env-secret"
    summary=provider_summary(settings)
    assert summary["has_api_key"] is True
    assert summary["api_key_source"]=="environment"


def test_explicit_api_key_wins_over_environment(monkeypatch):
    from app.providers import auth_headers
    monkeypatch.setenv("INKFORGE_SILICONFLOW_API_KEY", "env-secret")
    settings={"provider":"siliconflow","api_key":"saved-secret"}
    assert auth_headers(settings)["Authorization"]=="Bearer saved-secret"


def test_environment_api_key_is_not_persisted_when_project_key_is_blank(monkeypatch, tmp_path):
    from app.db import ProjectStore
    monkeypatch.setenv("INKFORGE_SILICONFLOW_API_KEY", "env-only-secret")
    store = ProjectStore(tmp_path / "inkforge.db")
    project = store.create("env-key")
    project["settings"]["provider"] = "siliconflow"
    project["settings"]["api_key"] = ""
    store.save(project["id"], project, reason="test")
    raw = (tmp_path / "inkforge.db").read_bytes()
    assert b"env-only-secret" not in raw

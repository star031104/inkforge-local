from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


SILICONFLOW_DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
SILICONFLOW_DEFAULT_MODEL = "Qwen/Qwen3-8B"
LOCAL_DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
XAI_DEFAULT_BASE_URL = "https://api.x.ai/v1"
XAI_DEFAULT_MODEL = "grok-4.6"


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    base_url: str
    model: str
    supports_top_k: bool = False
    supports_min_p: bool = False
    supports_repeat_penalty: bool = False
    supports_enable_thinking: bool = False
    supports_llama_chat_template_kwargs: bool = False
    supports_reasoning_budget: bool = False
    supports_reasoning_effort: bool = False


def normalize_base_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def detect_provider(settings: dict[str, Any]) -> str:
    explicit = str(settings.get("provider") or "").strip().lower()
    if explicit in {"siliconflow", "xai", "llama_cpp", "openai_compatible"}:
        return explicit
    url = normalize_base_url(settings.get("base_url") or "")
    host = urlparse(url).netloc.lower()
    if "siliconflow" in host:
        return "siliconflow"
    if host == "api.x.ai":
        return "xai"
    if host in {"127.0.0.1:8080", "localhost:8080", "127.0.0.1", "localhost"}:
        return "llama_cpp"
    return "openai_compatible"


def provider_profile(settings: dict[str, Any]) -> ProviderProfile:
    provider = detect_provider(settings)
    base_url = normalize_base_url(settings.get("base_url"))
    model = str(settings.get("model") or "").strip()
    if provider == "siliconflow":
        return ProviderProfile(
            name=provider,
            base_url=base_url or SILICONFLOW_DEFAULT_BASE_URL,
            model=model or SILICONFLOW_DEFAULT_MODEL,
            supports_top_k=True,
            supports_min_p=True,
            supports_enable_thinking=True,
        )
    if provider == "llama_cpp":
        return ProviderProfile(
            name=provider,
            base_url=base_url or LOCAL_DEFAULT_BASE_URL,
            model=model or "local-model",
            supports_top_k=True,
            supports_min_p=True,
            supports_repeat_penalty=True,
            supports_llama_chat_template_kwargs=True,
            supports_reasoning_budget=True,
        )
    if provider == "xai":
        return ProviderProfile(
            name=provider,
            base_url=base_url or XAI_DEFAULT_BASE_URL,
            model=model or XAI_DEFAULT_MODEL,
            supports_reasoning_effort=True,
        )
    return ProviderProfile(
        name=provider,
        base_url=base_url or LOCAL_DEFAULT_BASE_URL,
        model=model or "local-model",
    )




def resolved_api_key(settings: dict[str, Any]) -> str:
    """Return an API key without forcing secrets into project persistence.

    Project settings remain the first source for backwards compatibility. For
    security-sensitive smoke/E2E runs, SiliconFlow can instead be supplied via
    ``INKFORGE_SILICONFLOW_API_KEY`` so exported JSON/SQLite never needs to
    contain the secret. OpenAI-compatible providers can use
    ``INKFORGE_OPENAI_COMPAT_API_KEY``. Local llama.cpp still needs no real key.
    """
    explicit = str(settings.get("api_key") or "").strip()
    if explicit:
        return explicit
    provider = detect_provider(settings)
    if provider == "siliconflow":
        return os.environ.get("INKFORGE_SILICONFLOW_API_KEY", "").strip()
    if provider == "xai":
        return (
            os.environ.get("XAI_API_KEY", "").strip()
            or os.environ.get("INKFORGE_XAI_API_KEY", "").strip()
        )
    if provider == "openai_compatible":
        return os.environ.get("INKFORGE_OPENAI_COMPAT_API_KEY", "").strip()
    return ""


def auth_headers(settings: dict[str, Any], *, stream: bool = False) -> dict[str, str]:
    token = resolved_api_key(settings)
    if not token and detect_provider(settings) == "llama_cpp":
        token = "no-key"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if stream:
        headers["Accept"] = "text/event-stream"
    return headers


def build_chat_payload(
    settings: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    stream: bool,
    temperature: float | None = None,
    max_tokens: int | None = None,
    json_mode: bool = False,
) -> dict[str, Any]:
    profile = provider_profile(settings)
    payload: dict[str, Any] = {
        "model": profile.model,
        "messages": messages,
        "temperature": float(
            settings.get("temperature", 0.82) if temperature is None else temperature
        ),
        "top_p": float(settings.get("top_p", 0.92)),
        "max_tokens": int(
            settings.get("max_tokens", 3500) if max_tokens is None else max_tokens
        ),
        "stream": bool(stream),
    }
    if profile.supports_top_k:
        payload["top_k"] = int(settings.get("top_k", 40))
    if profile.supports_min_p:
        payload["min_p"] = float(settings.get("min_p", 0.05))
    if profile.supports_repeat_penalty:
        payload["repeat_penalty"] = float(settings.get("repeat_penalty", 1.08))

    # The user explicitly prefers non-thinking prose generation. SiliconFlow exposes
    # enable_thinking directly, while llama.cpp generally expects chat-template kwargs.
    thinking = False if json_mode else bool(settings.get("enable_thinking", False))
    if profile.supports_enable_thinking:
        payload["enable_thinking"] = thinking
        if thinking and int(settings.get("thinking_budget", 0) or 0) >= 128:
            payload["thinking_budget"] = int(settings["thinking_budget"])
    if profile.supports_llama_chat_template_kwargs:
        payload["chat_template_kwargs"] = {"enable_thinking": thinking}
    if profile.supports_reasoning_budget and not thinking:
        payload["reasoning_budget"] = 0
    if profile.supports_reasoning_effort:
        payload["reasoning_effort"] = "low" if json_mode or not thinking else "high"
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def provider_summary(settings: dict[str, Any]) -> dict[str, Any]:
    profile = provider_profile(settings)
    return {
        "provider": profile.name,
        "base_url": profile.base_url,
        "model": profile.model,
        "has_api_key": bool(resolved_api_key(settings)),
        "api_key_source": (
            "settings"
            if str(settings.get("api_key") or "").strip()
            else "environment"
            if resolved_api_key(settings)
            else "none"
        ),
        "non_thinking": not bool(settings.get("enable_thinking", False)),
    }


def parse_sse_delta(data: str) -> str:
    """Return user-visible content only; reasoning_content is intentionally ignored."""
    try:
        event = json.loads(data)
    except json.JSONDecodeError:
        return ""
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
    return str(delta.get("content") or "") if isinstance(delta, dict) else ""

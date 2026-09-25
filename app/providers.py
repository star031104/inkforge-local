from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse


ZHIPU_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_DEFAULT_MODEL = "glm-4.7-flash"
MODELSCOPE_DEFAULT_BASE_URL = "https://api-inference.modelscope.cn/v1"
MODELSCOPE_REASONING_MODEL = "ZhipuAI/GLM-5.2"
LOCAL_DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
LEGACY_CLOUD_PROVIDERS = {"siliconflow", "xai"}


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
    supports_thinking_object: bool = False


def normalize_base_url(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def detect_provider(settings: dict[str, Any]) -> str:
    explicit = str(settings.get("provider") or "").strip().lower()
    if explicit in {"zhipu", "modelscope", "llama_cpp", "openai_compatible"}:
        return explicit
    # Removed cloud presets are migrated to the single supported cloud provider.
    if explicit in LEGACY_CLOUD_PROVIDERS:
        return "zhipu"
    url = normalize_base_url(settings.get("base_url") or "")
    host = urlparse(url).netloc.lower()
    if host == "open.bigmodel.cn":
        return "zhipu"
    if host == "api-inference.modelscope.cn":
        return "modelscope"
    if "siliconflow" in host or host == "api.x.ai":
        return "zhipu"
    if host in {"127.0.0.1:8080", "localhost:8080", "127.0.0.1", "localhost"}:
        return "llama_cpp"
    return "openai_compatible"


def provider_profile(settings: dict[str, Any]) -> ProviderProfile:
    provider = detect_provider(settings)
    base_url = normalize_base_url(settings.get("base_url"))
    model = str(settings.get("model") or "").strip()
    raw_provider = str(settings.get("provider") or "").strip().lower()
    raw_host = urlparse(base_url).netloc.lower()
    legacy_cloud = (
        raw_provider in LEGACY_CLOUD_PROVIDERS
        or "siliconflow" in raw_host
        or raw_host == "api.x.ai"
    )
    if provider == "zhipu":
        return ProviderProfile(
            name=provider,
            base_url=ZHIPU_DEFAULT_BASE_URL if legacy_cloud else base_url or ZHIPU_DEFAULT_BASE_URL,
            model=ZHIPU_DEFAULT_MODEL if legacy_cloud else model or ZHIPU_DEFAULT_MODEL,
            supports_thinking_object=True,
        )
    if provider == "modelscope":
        return ProviderProfile(
            name=provider,
            base_url=base_url or MODELSCOPE_DEFAULT_BASE_URL,
            model=model or MODELSCOPE_REASONING_MODEL,
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
    return ProviderProfile(
        name=provider,
        base_url=base_url or LOCAL_DEFAULT_BASE_URL,
        model=model or "local-model",
    )




def resolved_api_key(settings: dict[str, Any]) -> str:
    """Return an API key without forcing secrets into project persistence.

    Project settings remain the first source for backwards compatibility. For
    security-sensitive smoke/E2E runs, Zhipu can instead be supplied via
    ``ZHIPU_API_KEY`` or ``INKFORGE_ZHIPU_API_KEY`` so exported JSON/SQLite
    never needs to contain the secret. OpenAI-compatible providers can use
    ``INKFORGE_OPENAI_COMPAT_API_KEY``. Local llama.cpp still needs no real key.
    """
    explicit = str(settings.get("api_key") or "").strip()
    if explicit:
        return explicit
    provider = detect_provider(settings)
    if provider == "zhipu":
        return (
            os.environ.get("ZHIPU_API_KEY", "").strip()
            or os.environ.get("INKFORGE_ZHIPU_API_KEY", "").strip()
        )
    if provider == "modelscope":
        return (
            os.environ.get("MODELSCOPE_API_KEY", "").strip()
            or os.environ.get("INKFORGE_MODELSCOPE_API_KEY", "").strip()
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

    # The user explicitly prefers non-thinking prose generation. Zhipu uses a
    # nested thinking object, while llama.cpp generally expects template kwargs.
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
    if profile.supports_thinking_object:
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    return payload


def settings_for_workload(
    settings: dict[str, Any], workload: str
) -> dict[str, Any]:
    """Resolve one of the two user-configured model slots.

    Two-model mode only becomes effective when both slots are usable. If just
    one slot has credentials (or is a keyless local llama.cpp service), every
    workload automatically falls back to that slot.
    """
    source = dict(settings or {})
    role = str(workload or "prose").strip().lower()
    source["_workload"] = role
    family = {
        "research": "reasoning",
        "planning": "reasoning",
        "extraction": "reasoning",
        "critic": "reasoning",
        "revision": "prose",
    }.get(role, role)
    if str(source.get("model_routing") or "single").strip().lower() != "dual":
        source["model_routing"] = "single"
        return source

    primary = dict(source)
    primary["model_routing"] = "single"
    secondary = dict(source)
    secondary.update(
        {
            "model_routing": "single",
            "provider": str(source.get("reasoning_provider") or "modelscope").strip(),
            "base_url": str(source.get("reasoning_base_url") or MODELSCOPE_DEFAULT_BASE_URL).strip(),
            "model": str(source.get("reasoning_model") or MODELSCOPE_REASONING_MODEL).strip(),
            "api_key": str(source.get("reasoning_api_key") or "").strip(),
        }
    )

    primary_ready = detect_provider(primary) == "llama_cpp" or bool(resolved_api_key(primary))
    secondary_ready = detect_provider(secondary) == "llama_cpp" or bool(resolved_api_key(secondary))
    if primary_ready and secondary_ready:
        return secondary if family == "reasoning" else primary
    if secondary_ready:
        return secondary
    return primary


def _single_provider_summary(settings: dict[str, Any]) -> dict[str, Any]:
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


def provider_summary(settings: dict[str, Any]) -> dict[str, Any]:
    if str(settings.get("model_routing") or "single").strip().lower() == "dual":
        reasoning = _single_provider_summary(
            settings_for_workload(settings, "reasoning")
        )
        prose = _single_provider_summary(settings_for_workload(settings, "prose"))
        effective_dual = (
            reasoning["provider"], reasoning["base_url"], reasoning["model"]
        ) != (prose["provider"], prose["base_url"], prose["model"])
        return {
            "provider": "dual" if effective_dual else prose["provider"],
            "routing_mode": "dual",
            "effective_routing": "dual" if effective_dual else "single",
            "model": f"{reasoning['model']} / {prose['model']}",
            "has_api_key": reasoning["has_api_key"] and prose["has_api_key"] if effective_dual else prose["has_api_key"],
            "active_slot": (
                "both" if effective_dual
                else "secondary" if prose["base_url"] == str(settings.get("reasoning_base_url") or "").strip()
                else "primary"
            ),
            "routes": {"reasoning": reasoning, "prose": prose},
        }
    return {"routing_mode": "single", "effective_routing": "single", "active_slot": "primary", **_single_provider_summary(settings)}


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

"""Single OpenAI-compatible model endpoint with capability-gated extensions.

InkForge talks to exactly one user-defined OpenAI-compatible endpoint
(``base_url`` + optional ``api_key``).  The user may pick two models from that
endpoint: ``model`` writes prose and revisions, ``planning_model`` handles
planning, critique, research and memory extraction (empty = same as
``model``).

Server specific request fields are never guessed from a vendor preset.  They
are gated by :class:`ServerCapabilities`, which is detected by the connection
test (``app.llama_client.probe_endpoint``) and stored in
``settings["capabilities"]``.  When nothing has been detected yet a
conservative URL heuristic is used (e.g. a local ``:8080`` server is treated as
llama.cpp, ``open.bigmodel.cn`` needs the nested ``thinking`` switch).

Extension hook for later features
---------------------------------
``build_chat_payload(..., options=PayloadOptions(...))`` accepts optional
request features.  Each one is only emitted when the endpoint supports it:

* ``json_schema`` – constrained decoding.  llama.cpp receives the raw
  ``json_schema`` field; other servers that advertise support receive the
  OpenAI ``response_format={"type": "json_schema", ...}`` form.  Unsupported
  servers silently fall back to ``json_object`` mode.
* ``logit_bias`` – mapping of *strings* (llama.cpp tokenises them) or token
  ids to a bias.  Intended for anti-slop phrase suppression; only llama.cpp
  accepts string keys, so it is dropped elsewhere.
* ``cache_prompt`` – llama.cpp KV prefix reuse (on by default there).
* ``extra`` – raw fields merged last, for experiments.

Additional cross-cutting behaviour can be registered with
:func:`register_payload_extension`; every extension receives the payload, the
resolved capabilities and the options after the core fields are built.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse


PROVIDER_NAME = "openai_compatible"

# Base URLs of presets that existed before 0.34.  Only used to migrate old
# projects; the UI no longer offers vendor presets.
LEGACY_PROVIDER_BASE_URLS = {
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "modelscope": "https://api-inference.modelscope.cn/v1",
    "llama_cpp": "http://127.0.0.1:8080/v1",
}
LEGACY_PROVIDER_MODELS = {
    "zhipu": "glm-4.7-flash",
    "modelscope": "ZhipuAI/GLM-5.2",
    "llama_cpp": "local-model",
}
LEGACY_CLOUD_PROVIDERS = {"siliconflow", "xai"}
LEGACY_CLOUD_BASE_URLS = {
    "siliconflow": "https://api.siliconflow.cn/v1",
    "xai": "https://api.x.ai/v1",
}

# Workloads routed to the planning model. Everything else uses the writing model.
PLANNING_WORKLOADS = frozenset(
    {"planning", "critic", "extraction", "research", "reasoning", "incubation", "audit"}
)
WRITING_WORKLOADS = frozenset({"prose", "revision", "writing"})


@dataclass(frozen=True)
class ServerCapabilities:
    """What the configured endpoint accepts beyond the plain OpenAI schema."""

    server: str = "generic"  # llama_cpp | vllm | ollama | zhipu | deepseek | modelscope | generic
    top_k: bool = False
    min_p: bool = False
    repeat_penalty: bool = False
    cache_prompt: bool = False
    json_schema: bool = False
    logit_bias: bool = False
    chat_template_kwargs: bool = False
    reasoning_budget: bool = False
    thinking_object: bool = False
    detected: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def capabilities_for(server: str, *, detected: bool) -> ServerCapabilities:
    if server == "llama_cpp":
        return ServerCapabilities(
            server=server, top_k=True, min_p=True, repeat_penalty=True,
            cache_prompt=True, json_schema=True, logit_bias=True,
            chat_template_kwargs=True, reasoning_budget=True, detected=detected,
        )
    if server == "vllm":
        return ServerCapabilities(
            server=server, top_k=True, min_p=True, json_schema=True,
            chat_template_kwargs=True, detected=detected,
        )
    if server in {"zhipu", "deepseek"}:
        # Both default to thinking mode server-side; only the nested
        # ``thinking`` switch turns it off.
        return ServerCapabilities(server=server, thinking_object=True, detected=detected)
    return ServerCapabilities(server=server or "generic", detected=detected)


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    base_url: str
    model: str
    capabilities: ServerCapabilities = field(default_factory=ServerCapabilities)


@dataclass
class PayloadOptions:
    json_schema: dict[str, Any] | None = None
    json_schema_name: str = "inkforge_output"
    logit_bias: dict[str, float] | None = None
    cache_prompt: bool | None = None
    extra: dict[str, Any] = field(default_factory=dict)


PayloadExtension = Callable[
    [dict[str, Any], ServerCapabilities, dict[str, Any], "PayloadOptions | None"], None
]
_PAYLOAD_EXTENSIONS: list[PayloadExtension] = []


def register_payload_extension(extension: PayloadExtension) -> PayloadExtension:
    """Register ``extension(payload, capabilities, settings, options)``."""
    if extension not in _PAYLOAD_EXTENSIONS:
        _PAYLOAD_EXTENSIONS.append(extension)
    return extension


def unregister_payload_extension(extension: PayloadExtension) -> None:
    if extension in _PAYLOAD_EXTENSIONS:
        _PAYLOAD_EXTENSIONS.remove(extension)


# --------------------------------------------------------------------------
# URL handling
# --------------------------------------------------------------------------

def normalize_base_url(url: Any) -> str:
    """Normalise a user supplied endpoint into the ``.../v1`` style base.

    * strips whitespace and trailing slashes
    * removes pasted ``/chat/completions`` or ``/models`` suffixes
    * adds ``http://`` when the scheme is missing
    * appends ``/v1`` when the URL has no path at all (``http://host:8080``)
    Paths such as ``/api/paas/v4`` or ``/compatible-mode/v1`` are kept.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    text = text.rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if text.lower().endswith(suffix):
            text = text[: -len(suffix)].rstrip("/")
    parsed = urlparse(text)
    path = parsed.path.rstrip("/")
    if not path:
        path = "/v1"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", "")).rstrip("/")


def server_root(base_url: str) -> str:
    """Return scheme://host[:port] for llama.cpp style ``/props`` probes."""
    parsed = urlparse(normalize_base_url(base_url))
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")


def guess_server(base_url: str) -> str:
    host = urlparse(normalize_base_url(base_url)).netloc.lower()
    if host == "open.bigmodel.cn":
        return "zhipu"
    if host == "api-inference.modelscope.cn":
        return "modelscope"
    if host == "api.deepseek.com":
        return "deepseek"
    if host.endswith(":11434"):
        return "ollama"
    if host in {"127.0.0.1:8080", "localhost:8080"}:
        return "llama_cpp"
    return "generic"


def resolve_capabilities(settings: dict[str, Any]) -> ServerCapabilities:
    """Use detected capabilities when they belong to the current URL."""
    base = normalize_base_url(settings.get("base_url"))
    stored = settings.get("capabilities")
    if isinstance(stored, dict) and stored.get("detected"):
        stored_base = normalize_base_url(stored.get("base_url"))
        if not stored_base or stored_base == base:
            known = {key for key in ServerCapabilities.__dataclass_fields__}
            values = {key: stored[key] for key in known if key in stored}
            return ServerCapabilities(**values)
    return capabilities_for(guess_server(base), detected=False)


# --------------------------------------------------------------------------
# Settings migration and routing
# --------------------------------------------------------------------------

LEGACY_MODEL_KEYS = (
    "model_routing",
    "reasoning_provider",
    "reasoning_base_url",
    "reasoning_model",
    "reasoning_api_key",
    "prose_api_key",
    "role_routes",
)


def migrate_model_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Convert pre-0.34 provider/two-slot settings in place; idempotent.

    * ``provider`` presets become plain base URLs.
    * ``model_routing=dual`` with the same endpoint keeps the second model as
      ``planning_model``.  With two different endpoints the writing slot wins;
      the old planning endpoint is remembered in ``model_migration`` so
      :func:`adopt_legacy_planning_slot` can switch to it when only that slot
      had an API key.
    """
    if not isinstance(settings, dict):
        return settings
    raw_provider = str(settings.get("provider") or "").strip().lower()
    base = str(settings.get("base_url") or "").strip()
    # Old SiliconFlow/xAI presets are ordinary OpenAI-compatible endpoints now:
    # keep the author's own URL, model and key.  (Earlier versions forced them
    # to Zhipu; doing that here would also rewrite any URL a user types into
    # the new single-endpoint dialog, on every load.)
    if not base and raw_provider in LEGACY_CLOUD_PROVIDERS:
        settings["base_url"] = LEGACY_CLOUD_BASE_URLS[raw_provider]
    elif not base and raw_provider in LEGACY_PROVIDER_BASE_URLS:
        settings["base_url"] = LEGACY_PROVIDER_BASE_URLS[raw_provider]
        if not str(settings.get("model") or "").strip():
            settings["model"] = LEGACY_PROVIDER_MODELS[raw_provider]
    if str(settings.get("api_key") or "").strip() == "no-key":
        settings["api_key"] = ""
    settings["base_url"] = normalize_base_url(settings.get("base_url"))

    routing = str(settings.get("model_routing") or "").strip().lower()
    if routing == "dual":
        reasoning_provider = str(settings.get("reasoning_provider") or "").strip().lower()
        reasoning_base = normalize_base_url(
            settings.get("reasoning_base_url")
            or LEGACY_PROVIDER_BASE_URLS.get(reasoning_provider, "")
        )
        reasoning_model = str(settings.get("reasoning_model") or "").strip()
        if reasoning_model and (not reasoning_base or reasoning_base == settings["base_url"]):
            settings.setdefault("planning_model", reasoning_model)
            if not str(settings.get("planning_model") or "").strip():
                settings["planning_model"] = reasoning_model
        elif reasoning_base and reasoning_model:
            settings["model_migration"] = {
                "from": "dual",
                "planning_base_url": reasoning_base,
                "planning_model": reasoning_model,
                "note": "旧版两个模型使用了不同的接口地址；已保留模型 1 的地址。",
            }
            if not settings["base_url"]:
                settings["base_url"] = reasoning_base
                settings["model"] = reasoning_model
    for key in LEGACY_MODEL_KEYS:
        if key == "reasoning_api_key" and isinstance(settings.get("model_migration"), dict):
            # Kept until adopt_legacy_planning_slot ran with decrypted secrets.
            continue
        settings.pop(key, None)
    settings["provider"] = PROVIDER_NAME
    settings.setdefault("model", "")
    settings.setdefault("planning_model", "")
    settings.setdefault("api_key", "")
    if not isinstance(settings.get("capabilities"), dict):
        settings["capabilities"] = {}
    return settings


def adopt_legacy_planning_slot(settings: dict[str, Any], secrets: dict[str, str]) -> None:
    """Finish a dual-endpoint migration once decrypted keys are available."""
    migration = settings.get("model_migration")
    if not isinstance(migration, dict) or migration.get("resolved"):
        settings.pop("reasoning_api_key", None)
        return
    primary_key = str(settings.get("api_key") or secrets.get("settings.api_key") or "").strip()
    secondary_key = str(
        settings.get("reasoning_api_key") or secrets.get("settings.reasoning_api_key") or ""
    ).strip()
    # A keyless primary is normal for any local server (llama.cpp on any port,
    # Ollama, vLLM, LM Studio, a LAN box), so only a keyless *public generic*
    # endpoint means "the old writing slot never worked".
    import ipaddress

    primary_url = normalize_base_url(settings.get("base_url", ""))
    host = (urlparse(primary_url).hostname or "").strip("[]").lower()
    try:
        address = ipaddress.ip_address(host)
        private_host = address.is_loopback or address.is_private or address.is_link_local
    except ValueError:
        private_host = host == "localhost" or host.endswith((".local", ".localhost", ".lan"))
    # Zhipu/ModelScope are guessed too, but they are cloud services that need
    # a key, so only the local server kinds count here.
    local_primary = private_host or guess_server(primary_url) in {"llama_cpp", "ollama"}
    if not primary_key and not local_primary and secondary_key:
        settings["base_url"] = migration.get("planning_base_url", settings.get("base_url", ""))
        settings["model"] = migration.get("planning_model", settings.get("model", ""))
        settings["planning_model"] = ""
        settings["api_key"] = secondary_key
        settings["capabilities"] = {}
        migration["note"] = "旧版只有模型 2 配置了 Key；已改用模型 2 的接口地址。"
    migration["resolved"] = True
    settings.pop("reasoning_api_key", None)


def workload_family(workload: str) -> str:
    role = str(workload or "prose").strip().lower()
    return "planning" if role in PLANNING_WORKLOADS else "writing"


def settings_for_workload(settings: dict[str, Any], workload: str) -> dict[str, Any]:
    """Return a copy of ``settings`` whose ``model`` matches the workload."""
    source = dict(settings or {})
    if any(key in source for key in ("model_routing", "reasoning_model", "reasoning_base_url")):
        migrate_model_settings(source)
    role = str(workload or "prose").strip().lower()
    source["_workload"] = role
    source["provider"] = PROVIDER_NAME
    source["writing_model"] = str(source.get("model") or "").strip()
    planning = str(source.get("planning_model") or "").strip()
    if workload_family(role) == "planning" and planning:
        source["model"] = planning
    return source


# --------------------------------------------------------------------------
# Request building
# --------------------------------------------------------------------------

def provider_profile(settings: dict[str, Any]) -> ProviderProfile:
    return ProviderProfile(
        name=PROVIDER_NAME,
        base_url=normalize_base_url(settings.get("base_url")),
        model=str(settings.get("model") or "").strip(),
        capabilities=resolve_capabilities(settings),
    )


def detect_provider(settings: dict[str, Any]) -> str:
    """Compatibility shim: the only provider is the OpenAI-compatible one."""
    return PROVIDER_NAME


def resolved_api_key(settings: dict[str, Any]) -> str:
    """Return the configured key, falling back to environment variables.

    ``INKFORGE_API_KEY`` is the supported variable.  Legacy variables are still
    honoured for the matching hosts so old launch scripts keep working.
    """
    explicit = str(settings.get("api_key") or "").strip()
    if explicit and explicit != "no-key":
        return explicit
    generic = os.environ.get("INKFORGE_API_KEY", "").strip()
    if generic:
        return generic
    server = guess_server(str(settings.get("base_url") or ""))
    legacy: tuple[str, ...] = ("INKFORGE_OPENAI_COMPAT_API_KEY",)
    if server == "zhipu":
        legacy = ("ZHIPU_API_KEY", "INKFORGE_ZHIPU_API_KEY", *legacy)
    elif server == "modelscope":
        legacy = ("MODELSCOPE_API_KEY", "INKFORGE_MODELSCOPE_API_KEY", *legacy)
    for name in legacy:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def auth_headers(settings: dict[str, Any], *, stream: bool = False) -> dict[str, str]:
    token = resolved_api_key(settings)
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
    options: PayloadOptions | None = None,
) -> dict[str, Any]:
    profile = provider_profile(settings)
    caps = profile.capabilities
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
    if caps.top_k:
        payload["top_k"] = int(settings.get("top_k", 40))
    if caps.min_p:
        payload["min_p"] = float(settings.get("min_p", 0.05))
    if caps.repeat_penalty:
        payload["repeat_penalty"] = float(settings.get("repeat_penalty", 1.08))

    thinking = False if json_mode else bool(settings.get("enable_thinking", False))
    if caps.chat_template_kwargs:
        payload["chat_template_kwargs"] = {"enable_thinking": thinking}
    if caps.reasoning_budget and not thinking:
        payload["reasoning_budget"] = 0
    if caps.thinking_object:
        payload["thinking"] = {"type": "enabled" if thinking else "disabled"}

    if caps.cache_prompt:
        payload["cache_prompt"] = True if options is None or options.cache_prompt is None else bool(options.cache_prompt)

    schema = options.json_schema if options else None
    if json_mode or schema:
        if schema and caps.json_schema and caps.server == "llama_cpp":
            payload["json_schema"] = schema
        elif schema and caps.json_schema:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": options.json_schema_name if options else "inkforge_output",
                    "schema": schema,
                },
            }
        else:
            payload["response_format"] = {"type": "json_object"}
    if options and options.logit_bias and caps.logit_bias:
        # llama.cpp accepts [[string_or_token_id, bias], ...]
        payload["logit_bias"] = [[key, float(value)] for key, value in options.logit_bias.items()]
    for extension in list(_PAYLOAD_EXTENSIONS):
        extension(payload, caps, settings, options)
    if options and options.extra:
        payload.update(options.extra)
    return payload


def _endpoint_summary(settings: dict[str, Any]) -> dict[str, Any]:
    profile = provider_profile(settings)
    return {
        "provider": PROVIDER_NAME,
        "base_url": profile.base_url,
        "model": profile.model,
        "server": profile.capabilities.server,
        "capabilities": profile.capabilities.as_dict(),
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
    source = dict(settings or {})
    if any(key in source for key in ("model_routing", "reasoning_model")):
        migrate_model_settings(source)
    writing = str(source.get("model") or "").strip()
    planning = str(source.get("planning_model") or "").strip() or writing
    summary = _endpoint_summary(source)
    summary.update(
        {
            "writing_model": writing,
            "planning_model": planning,
            "routing_mode": "dual" if planning != writing else "single",
            "effective_routing": "dual" if planning != writing else "single",
            "configured": bool(summary["base_url"] and writing),
        }
    )
    return summary


def parse_models_payload(payload: Any) -> list[dict[str, str]]:
    """Accept OpenAI ``{"data": [...]}``, Ollama ``{"models": [...]}`` or lists."""
    items: Any = payload
    if isinstance(payload, dict):
        items = payload.get("data", payload.get("models", []))
    if not isinstance(items, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, str):
            model_id, owner = item, ""
        elif isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or item.get("model") or "")
            owner = str(item.get("owned_by") or "")
        else:
            continue
        model_id = model_id.strip()
        if model_id and model_id not in seen:
            seen.add(model_id)
            result.append({"id": model_id, "owned_by": owner})
    return result


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

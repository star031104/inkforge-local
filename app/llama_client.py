from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

from app.model_telemetry import Call, has_usage, observe, attempt as observe_attempt
from app.core.model_budget import validate_request_budget
from app.core.model_completion import ModelOutputTruncatedError, StreamOutcome

from app.providers import (
    PayloadOptions,
    auth_headers,
    build_chat_payload,
    guess_server,
    mark_stream_options_rejected,
    normalize_base_url,
    parse_models_payload,
    parse_sse_delta,
    provider_profile,
    rejects_stream_options,
    server_root,
    capabilities_for,
)


RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
# After a structured answer is complete, read at most this much more of the
# stream to collect the provider's final ``usage`` chunk.
USAGE_DRAIN_EVENTS = 48
USAGE_DRAIN_SECONDS = 1.5


@asynccontextmanager
async def _open_stream(
    client: httpx.AsyncClient,
    settings: dict[str, Any],
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> AsyncIterator[httpx.Response]:
    """POST a streaming request; drop ``stream_options`` once if it is rejected.

    Unknown OpenAI-compatible servers get ``stream_options`` so they report
    usage in the last chunk.  A server that answers 400 naming the field is
    remembered per base URL and the same request is re-sent without it.
    """
    async with client.stream("POST", url, json=payload, headers=headers) as response:
        if response.is_error:
            await response.aread()
        if not (
            "stream_options" in payload
            and response.is_error
            and rejects_stream_options(response.status_code, response.text)
        ):
            yield response
            return
    mark_stream_options_rejected(settings)
    payload.pop("stream_options", None)
    async with client.stream("POST", url, json=payload, headers=headers) as response:
        if response.is_error:
            await response.aread()
        yield response


class ModelContentFilteredError(ValueError):
    """The provider ended generation after applying its content filter."""


def _raise_if_content_filtered(payload: str | dict[str, Any]) -> None:
    try:
        body = json.loads(payload) if isinstance(payload, str) else payload
        reason = str(body.get("choices", [{}])[0].get("finish_reason") or "").lower()
    except (ValueError, TypeError, IndexError, AttributeError, KeyError):
        return
    if reason in {"sensitive", "content_filter"}:
        raise ModelContentFilteredError(
            "模型服务将本章内容标记为敏感并停止输出。请调整本章路线或更换正文模型后再继续；本次正文未写入。"
        )


def validate_context_budget(settings: dict, messages: list, output_tokens: int) -> None:
    """Check the final payload, including prefixes added after prompt compilation."""
    validate_request_budget(settings, messages, output_tokens)


def _request_attempts(settings: dict[str, Any]) -> int:
    """Return a bounded number of attempts for temporary provider failures."""
    profile = provider_profile(settings)
    default = 6 if profile.capabilities.server == "zhipu" else 3
    try:
        configured = int(settings.get("request_retry_attempts", default))
    except (TypeError, ValueError):
        configured = default
    return max(1, min(configured, 8))


def _retry_delay_seconds(
    settings: dict[str, Any], response: httpx.Response, retry_index: int
) -> float:
    """Respect Retry-After when present, otherwise use bounded exponential backoff."""
    try:
        maximum = float(settings.get("request_retry_max_seconds", 30))
    except (TypeError, ValueError):
        maximum = 30
    maximum = max(1, min(maximum, 60))
    retry_after = response.headers.get("Retry-After", "").strip()
    if retry_after:
        try:
            return max(0, min(float(retry_after), maximum))
        except ValueError:
            pass
    return _network_retry_delay_seconds(settings, retry_index)


def _network_retry_delay_seconds(
    settings: dict[str, Any], retry_index: int
) -> float:
    try:
        maximum = float(settings.get("request_retry_max_seconds", 30))
    except (TypeError, ValueError):
        maximum = 30
    maximum = max(1, min(maximum, 60))
    try:
        base = float(settings.get("request_retry_base_seconds", 2))
    except (TypeError, ValueError):
        base = 2
    return max(0, min(maximum, max(0.25, base) * (2**retry_index)))


def _response_error_details(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    error = payload.get("error")
    if isinstance(error, dict):
        return (
            str(error.get("code") or "").strip(),
            str(error.get("type") or "").strip(),
        )
    return str(payload.get("code") or "").strip(), ""


def _should_retry(response: httpx.Response, attempt: int, attempts: int) -> bool:
    # Zhipu code 1113 is an account balance/resource-package problem. Waiting
    # cannot repair it and would only make the UI appear frozen. Code 1305 and
    # ordinary rate-limit/server statuses are transient and should back off.
    if response.status_code == 429:
        code, error_type = _response_error_details(response)
        if code in {"1113", "insufficient_quota"} or error_type == "insufficient_quota":
            return False
    return response.status_code in RETRYABLE_HTTP_STATUSES and attempt + 1 < attempts


def complete_json_prefix(text: str) -> str | None:
    """Return the first balanced *valid* JSON object as soon as it is complete."""
    search_from = 0
    while True:
        start = text.find("{", search_from)
        if start < 0:
            return None
        depth = 0
        in_string = False
        escaped = False
        for index, char in enumerate(text[start:], start=start):
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : index + 1]
                    try:
                        parsed = json.loads(candidate)
                    except json.JSONDecodeError:
                        search_from = start + 1
                        break
                    if isinstance(parsed, dict):
                        return candidate
                    search_from = start + 1
                    break
        else:
            return None


class ModelEndpointError(ValueError):
    """Connection test failure with an author-facing Chinese explanation."""

    def __init__(self, message: str, *, kind: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def _require_endpoint(profile) -> None:
    if not profile.base_url:
        raise ModelEndpointError("尚未填写模型接口地址。请在“模型设置”中填写 OpenAI 兼容的 Base URL。", kind="missing_url")


def _explain_http_error(response: httpx.Response) -> ModelEndpointError:
    status = response.status_code
    detail = ""
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        detail = str((error or {}).get("message") if isinstance(error, dict) else error or body.get("message") or "")[:160]
    except (ValueError, TypeError, AttributeError):
        detail = response.text[:160]
    hints = {
        401: ("auth", "API Key 无效或缺失，服务拒绝了请求（401）。"),
        403: ("auth", "服务拒绝访问（403），请确认 Key 的权限或账户状态。"),
        404: ("not_found", "地址可以访问，但找不到 /models 接口（404）。请确认 Base URL 是否需要以 /v1 结尾；若服务不提供模型列表，可直接手动填写模型 ID。"),
        429: ("rate_limit", "请求过于频繁或账户额度不足（429）。"),
    }
    kind, message = hints.get(status, ("server", f"模型服务返回错误状态 {status}。"))
    if detail:
        message = f"{message} 服务说明：{detail}"
    return ModelEndpointError(message, kind=kind, status=status)


def _explain_transport_error(exc: Exception) -> ModelEndpointError:
    if isinstance(exc, httpx.TimeoutException):
        return ModelEndpointError("连接超时：服务没有在限定时间内响应。请确认服务已启动、地址与端口正确。", kind="timeout")
    if isinstance(exc, httpx.ConnectError):
        return ModelEndpointError("无法建立连接：地址不可达或服务未启动。本地模型请先启动服务，再检查端口。", kind="connect")
    if isinstance(exc, httpx.UnsupportedProtocol | httpx.InvalidURL):
        return ModelEndpointError("接口地址格式不正确，请填写类似 http://127.0.0.1:8080/v1 或 https://example.com/v1 的地址。", kind="invalid_url")
    return ModelEndpointError(f"连接失败：{exc}", kind="network")


async def _fetch_models(client: httpx.AsyncClient, settings: dict[str, Any]) -> tuple[list[dict[str, str]], httpx.Response]:
    profile = provider_profile(settings)
    _require_endpoint(profile)
    attempts = min(_request_attempts(settings), 2)
    response: httpx.Response | None = None
    for attempt in range(attempts):
        try:
            response = await client.get(f"{profile.base_url}/models", headers=auth_headers(settings))
        except httpx.HTTPError as exc:
            raise _explain_transport_error(exc) from exc
        if not _should_retry(response, attempt, attempts):
            break
        await asyncio.sleep(min(2.0, _retry_delay_seconds(settings, response, attempt)))
    assert response is not None
    if response.status_code >= 400:
        raise _explain_http_error(response)
    try:
        payload = response.json()
    except ValueError as exc:
        raise ModelEndpointError("服务返回的不是 JSON，可能填写了网页地址而不是 API 地址。", kind="not_api") from exc
    return parse_models_payload(payload), response


async def list_models(settings: dict[str, Any]) -> list[str]:
    async with httpx.AsyncClient(timeout=12) as client:
        models, _response = await _fetch_models(client, settings)
    return [item["id"] for item in models]


async def _detect_server(
    client: httpx.AsyncClient, settings: dict[str, Any], models: list[dict[str, str]], response: httpx.Response | None
) -> str:
    owners = {str(item.get("owned_by") or "").lower() for item in models}
    if "llamacpp" in owners or "llama.cpp" in owners:
        return "llama_cpp"
    if "vllm" in owners:
        return "vllm"
    if owners & {"library", "ollama"}:
        return "ollama"
    root = server_root(settings.get("base_url", ""))
    try:
        props = await client.get(f"{root}/props", headers=auth_headers(settings), timeout=4)
        if props.status_code == 200:
            body = props.json()
            if isinstance(body, dict) and (
                "default_generation_settings" in body or "total_slots" in body or "build_info" in body
            ):
                return "llama_cpp"
    except (httpx.HTTPError, ValueError):
        pass
    if response is not None and "vllm" in str(response.headers.get("server", "")).lower():
        return "vllm"
    return guess_server(settings.get("base_url", ""))


async def probe_endpoint(settings: dict[str, Any]) -> dict[str, Any]:
    """Connection test: list models, measure latency and detect capabilities.

    Returns ``ok=False`` with a Chinese ``message`` instead of raising, so the
    settings dialog can display the reason next to the button.
    """
    base_url = normalize_base_url(settings.get("base_url"))
    probe = {**settings, "base_url": base_url}
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            models, response = await _fetch_models(client, probe)
            latency_ms = round((time.perf_counter() - started) * 1000)
            server = await _detect_server(client, probe, models, response)
    except ModelEndpointError as exc:
        return {
            "ok": False,
            "base_url": base_url,
            "error_kind": exc.kind,
            "status": exc.status,
            "message": str(exc),
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "models": [],
            "capabilities": {},
        }
    capabilities = {**capabilities_for(server, detected=True).as_dict(), "base_url": base_url}
    note = "" if models else "服务已连接，但没有返回模型列表；请手动填写模型 ID。"
    return {
        "ok": True,
        "base_url": base_url,
        "latency_ms": latency_ms,
        "models": models,
        "server": server,
        "capabilities": capabilities,
        "message": note or f"已连接，发现 {len(models)} 个模型（{latency_ms} ms）。",
    }


async def _chat_once(
    settings: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 1200,
    json_mode: bool = False,
    timeout_seconds: float = 180,
    options: PayloadOptions | None = None,
) -> str:
    profile = provider_profile(settings)
    _require_endpoint(profile)
    # Structured output is streamed so a small/local model cannot append pages of prose
    # after a complete JSON object. The first valid balanced object ends the read.
    payload = build_chat_payload(
        settings,
        messages,
        stream=bool(json_mode),
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
        options=options,
    )
    attempts = _request_attempts(settings)
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        if json_mode:
            headers = auth_headers(settings, stream=True)

            async def receive_json() -> str:
                for attempt in range(attempts):
                    observe_attempt(attempt)
                    pieces: list[str] = []
                    outcome = StreamOutcome()
                    retry_delay: float | None = None
                    finished: str | None = None
                    drained, drain_deadline = 0, 0.0
                    try:
                        async with _open_stream(
                            client, settings, f"{profile.base_url}/chat/completions", payload, headers
                        ) as response:
                            if _should_retry(response, attempt, attempts):
                                retry_delay = _retry_delay_seconds(
                                    settings, response, attempt
                                )
                            else:
                                response.raise_for_status()
                                async for line in response.aiter_lines():
                                    if not line.startswith("data:"):
                                        continue
                                    data = line[5:].strip()
                                    if finished is not None:
                                        # JSON is complete; read a few more events only to
                                        # pick up the final ``usage`` chunk.
                                        drained += 1
                                        if data != "[DONE]":
                                            observe(data)
                                        if (
                                            data == "[DONE]"
                                            or has_usage()
                                            or drained > USAGE_DRAIN_EVENTS
                                            or time.monotonic() > drain_deadline
                                        ):
                                            return finished
                                        continue
                                    if data == "[DONE]":
                                        outcome.observe(data)
                                        break
                                    observe(data)
                                    _raise_if_content_filtered(data)
                                    outcome.observe(data)
                                    piece = parse_sse_delta(data)
                                    if not piece:
                                        continue
                                    pieces.append(piece)
                                    completed = complete_json_prefix("".join(pieces))
                                    if completed is not None:
                                        if has_usage() or "stream_options" not in payload:
                                            return completed
                                        finished, drained = completed, 0
                                        drain_deadline = time.monotonic() + USAGE_DRAIN_SECONDS
                                if finished is not None:
                                    return finished
                                if outcome.truncated:
                                    raise ModelOutputTruncatedError(
                                        "模型达到单次输出上限，JSON 尚未完整；请缩小本批任务或增加可用输出预算。"
                                    )
                                if not outcome.terminal_received:
                                    raise httpx.ReadError("模型连接在完成标记前结束，结构化输出尚未完成。")
                                return "".join(pieces)
                    except httpx.TransportError:
                        completed = complete_json_prefix("".join(pieces))
                        if completed is not None:
                            return completed
                        if attempt + 1 >= attempts:
                            raise
                        retry_delay = _network_retry_delay_seconds(settings, attempt)
                    if retry_delay is not None:
                        await asyncio.sleep(retry_delay)
                raise RuntimeError("模型请求重试状态异常")

            return await asyncio.wait_for(receive_json(), timeout=timeout_seconds)

        for attempt in range(attempts):
            observe_attempt(attempt)
            try:
                response = await client.post(
                    f"{profile.base_url}/chat/completions",
                    json=payload,
                    headers=auth_headers(settings),
                )
            except httpx.TransportError:
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(_network_retry_delay_seconds(settings, attempt))
                continue
            if not _should_retry(response, attempt, attempts):
                break
            await asyncio.sleep(_retry_delay_seconds(settings, response, attempt))
        response.raise_for_status()
        body = response.json()
        observe(body)
        _raise_if_content_filtered(body)
        outcome = StreamOutcome()
        outcome.observe(body)
        if outcome.truncated:
            raise ModelOutputTruncatedError("模型达到单次输出上限，结果尚未完整；请缩小本批任务或增加可用输出预算。")
        try:
            return str(body["choices"][0]["message"].get("content", ""))
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("模型返回缺少 choices[0].message.content") from exc


async def _chat_stream(
    settings: dict[str, Any], messages: list[dict[str, str]], outcome: StreamOutcome,
    options: PayloadOptions | None = None,
) -> AsyncIterator[str]:
    profile = provider_profile(settings)
    _require_endpoint(profile)
    payload = build_chat_payload(settings, messages, stream=True, options=options)
    attempts = _request_attempts(settings)
    timeout = httpx.Timeout(connect=20, read=600, write=60, pool=30)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(attempts):
            retry_delay: float | None = None
            emitted = False
            observe_attempt(attempt)
            try:
                async with _open_stream(
                    client,
                    settings,
                    f"{profile.base_url}/chat/completions",
                    payload,
                    auth_headers(settings, stream=True),
                ) as response:
                    if _should_retry(response, attempt, attempts):
                        retry_delay = _retry_delay_seconds(settings, response, attempt)
                    else:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                outcome.observe(data)
                                return
                            observe(data)
                            _raise_if_content_filtered(data)
                            outcome.observe(data)
                            text = parse_sse_delta(data)
                            if text:
                                emitted = True
                                yield text
                        if not outcome.terminal_received:
                            raise httpx.ReadError("模型连接在完成标记前结束，已收到的正文片段仍被保留。")
                        return
            except httpx.TransportError:
                # Retrying a stream after text has already been emitted would
                # duplicate the beginning of the answer for ordinary callers.
                # The auto director buffers its answer and has a continuation
                # recovery layer for this exact mid-stream case.
                if emitted or attempt + 1 >= attempts:
                    raise
                retry_delay = _network_retry_delay_seconds(settings, attempt)
            if retry_delay is not None:
                await asyncio.sleep(retry_delay)


async def chat_once(settings, messages, **kwargs) -> str:
    validate_context_budget(settings, messages, int(kwargs.get("max_tokens", 1200)))
    call = Call(settings, messages)
    error = None
    try:
        call.output = await _chat_once(settings, messages, **kwargs)
        if kwargs.get("json_mode") and call.data["finish_reason"] == "unknown":
            # Without a provider finish_reason, only an output that ends with a
            # closing brace may be labelled closed; anything else is a likely
            # truncation and must stay visible to the length-rate alarm.
            closed = str(call.output or "").rstrip().rstrip("`").rstrip().endswith("}")
            call.data["finish_reason"] = "json_closed" if closed else "json_unclosed"
        return call.output
    except BaseException as exc:
        error = exc
        raise
    finally:
        call.finish(error)


async def chat_stream(
    settings, messages, *, outcome: StreamOutcome | None = None, options: PayloadOptions | None = None
) -> AsyncIterator[str]:
    validate_context_budget(settings, messages, int(settings.get("max_tokens", 3500)))
    call = Call(settings, messages)
    error = None
    try:
        async for piece in _chat_stream(
            settings, messages, outcome if outcome is not None else StreamOutcome(), options
        ):
            call.output += piece
            yield piece
    except BaseException as exc:
        error = exc
        raise
    finally:
        call.finish(error)

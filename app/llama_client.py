from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .providers import (
    auth_headers,
    build_chat_payload,
    normalize_base_url,
    parse_sse_delta,
    provider_profile,
)


RETRYABLE_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}


def _request_attempts(settings: dict[str, Any]) -> int:
    """Return a bounded number of attempts for temporary provider failures."""
    profile = provider_profile(settings)
    default = 6 if profile.name == "zhipu" else 3
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


async def list_models(settings: dict[str, Any]) -> list[str]:
    profile = provider_profile(settings)
    attempts = _request_attempts(settings)
    async with httpx.AsyncClient(timeout=12) as client:
        for attempt in range(attempts):
            response = await client.get(
                f"{profile.base_url}/models",
                headers=auth_headers(settings),
            )
            if not _should_retry(response, attempt, attempts):
                break
            await asyncio.sleep(_retry_delay_seconds(settings, response, attempt))
        response.raise_for_status()
        data = response.json().get("data", [])
        return [str(item["id"]) for item in data if isinstance(item, dict) and item.get("id")]


async def chat_once(
    settings: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 1200,
    json_mode: bool = False,
    timeout_seconds: float = 180,
) -> str:
    profile = provider_profile(settings)
    # Structured output is streamed so a small/local model cannot append pages of prose
    # after a complete JSON object. The first valid balanced object ends the read.
    payload = build_chat_payload(
        settings,
        messages,
        stream=bool(json_mode),
        temperature=temperature,
        max_tokens=max_tokens,
        json_mode=json_mode,
    )
    attempts = _request_attempts(settings)
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        if json_mode:
            headers = auth_headers(settings, stream=True)

            async def receive_json() -> str:
                for attempt in range(attempts):
                    pieces: list[str] = []
                    retry_delay: float | None = None
                    try:
                        async with client.stream(
                            "POST",
                            f"{profile.base_url}/chat/completions",
                            json=payload,
                            headers=headers,
                        ) as response:
                            if response.is_error:
                                await response.aread()
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
                                    if data == "[DONE]":
                                        break
                                    piece = parse_sse_delta(data)
                                    if not piece:
                                        continue
                                    pieces.append(piece)
                                    completed = complete_json_prefix("".join(pieces))
                                    if completed is not None:
                                        return completed
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
        try:
            return str(body["choices"][0]["message"].get("content", ""))
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("模型返回缺少 choices[0].message.content") from exc


async def chat_stream(
    settings: dict[str, Any], messages: list[dict[str, str]]
) -> AsyncIterator[str]:
    profile = provider_profile(settings)
    payload = build_chat_payload(settings, messages, stream=True)
    attempts = _request_attempts(settings)
    timeout = httpx.Timeout(connect=20, read=600, write=60, pool=30)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(attempts):
            retry_delay: float | None = None
            emitted = False
            try:
                async with client.stream(
                    "POST",
                    f"{profile.base_url}/chat/completions",
                    json=payload,
                    headers=auth_headers(settings, stream=True),
                ) as response:
                    if response.is_error:
                        await response.aread()
                    if _should_retry(response, attempt, attempts):
                        retry_delay = _retry_delay_seconds(settings, response, attempt)
                    else:
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                return
                            text = parse_sse_delta(data)
                            if text:
                                emitted = True
                                yield text
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

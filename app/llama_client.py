from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx


def normalize_base_url(url: str) -> str:
    return url.rstrip("/")


def complete_json_prefix(text: str) -> str | None:
    """Return the first balanced *valid* JSON object as soon as it is complete.

    Some chat templates leak a short preamble containing braces even when JSON mode is
    requested.  Waiting for a valid object prevents that preamble from terminating the
    stream before the real payload arrives.
    """
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
    url = normalize_base_url(settings.get("base_url", "http://127.0.0.1:8080/v1"))
    headers = {"Authorization": f"Bearer {settings.get('api_key') or 'no-key'}"}
    async with httpx.AsyncClient(timeout=8) as client:
        response = await client.get(f"{url}/models", headers=headers)
        response.raise_for_status()
        return [str(item["id"]) for item in response.json().get("data", [])]


async def chat_once(
    settings: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.3,
    max_tokens: int = 1200,
    json_mode: bool = False,
    timeout_seconds: float = 180,
) -> str:
    url = normalize_base_url(settings.get("base_url", "http://127.0.0.1:8080/v1"))
    payload = {
        "model": settings.get("model") or "local-model",
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": bool(json_mode),
        "repeat_penalty": float(settings.get("repeat_penalty", 1.08)),
    }
    thinking = False if json_mode else bool(settings.get("enable_thinking", False))
    payload["chat_template_kwargs"] = {"enable_thinking": thinking}
    if not thinking:
        payload["reasoning_budget"] = 0
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Authorization": f"Bearer {settings.get('api_key') or 'no-key'}"}
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        if json_mode:
            headers["Accept"] = "text/event-stream"
            async def receive_json() -> str:
                pieces: list[str] = []
                async with client.stream(
                    "POST", f"{url}/chat/completions", json=payload, headers=headers
                ) as response:
                    if response.is_error:
                        await response.aread()
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                            piece = (
                                event["choices"][0]
                                .get("delta", {})
                                .get("content", "")
                            )
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
                        if not piece:
                            continue
                        pieces.append(piece)
                        completed = complete_json_prefix("".join(pieces))
                        if completed is not None:
                            return completed
                return "".join(pieces)

            return await asyncio.wait_for(receive_json(), timeout=timeout_seconds)
        response = await client.post(
            f"{url}/chat/completions", json=payload, headers=headers
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]


async def chat_stream(
    settings: dict[str, Any], messages: list[dict[str, str]]
) -> AsyncIterator[str]:
    url = normalize_base_url(settings.get("base_url", "http://127.0.0.1:8080/v1"))
    payload = {
        "model": settings.get("model") or "local-model",
        "messages": messages,
        "temperature": float(settings.get("temperature", 0.82)),
        "top_p": float(settings.get("top_p", 0.92)),
        "top_k": int(settings.get("top_k", 40)),
        "min_p": float(settings.get("min_p", 0.05)),
        "repeat_penalty": float(settings.get("repeat_penalty", 1.08)),
        "max_tokens": int(settings.get("max_tokens", 1800)),
        "stream": True,
    }
    thinking = bool(settings.get("enable_thinking", False))
    payload["chat_template_kwargs"] = {"enable_thinking": thinking}
    if not thinking:
        payload["reasoning_budget"] = 0
    headers = {
        "Authorization": f"Bearer {settings.get('api_key') or 'no-key'}",
        "Accept": "text/event-stream",
    }
    # Streaming may legitimately run for several minutes on an 8 GB GPU, but a
    # dead llama.cpp connection must eventually release the UI and director.
    timeout = httpx.Timeout(connect=15, read=600, write=60, pool=30)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST", f"{url}/chat/completions", json=payload, headers=headers
        ) as response:
            if response.is_error:
                await response.aread()
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    event = json.loads(data)
                    text = event["choices"][0].get("delta", {}).get("content", "")
                    if text:
                        yield text
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

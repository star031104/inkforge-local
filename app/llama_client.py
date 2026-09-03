from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from .providers import (
    auth_headers,
    build_chat_payload,
    detect_provider,
    normalize_base_url,
    parse_sse_delta,
    provider_profile,
)


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
    params = {"sub_type": "chat"} if detect_provider(settings) == "siliconflow" else None
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.get(
            f"{profile.base_url}/models",
            headers=auth_headers(settings),
            params=params,
        )
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
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        if json_mode:
            headers = auth_headers(settings, stream=True)

            async def receive_json() -> str:
                pieces: list[str] = []
                async with client.stream(
                    "POST",
                    f"{profile.base_url}/chat/completions",
                    json=payload,
                    headers=headers,
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
                        piece = parse_sse_delta(data)
                        if not piece:
                            continue
                        pieces.append(piece)
                        completed = complete_json_prefix("".join(pieces))
                        if completed is not None:
                            return completed
                return "".join(pieces)

            return await asyncio.wait_for(receive_json(), timeout=timeout_seconds)

        response = await client.post(
            f"{profile.base_url}/chat/completions",
            json=payload,
            headers=auth_headers(settings),
        )
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
    timeout = httpx.Timeout(connect=20, read=600, write=60, pool=30)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream(
            "POST",
            f"{profile.base_url}/chat/completions",
            json=payload,
            headers=auth_headers(settings, stream=True),
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
                text = parse_sse_delta(data)
                if text:
                    yield text

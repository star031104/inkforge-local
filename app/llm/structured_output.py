"""Validated structured model output with bounded repair.

This module owns provider-independent JSON recovery. Domain validators are
injected by callers, which keeps the transport and story rules separate.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import httpx

from app.llm.client import ModelContentFilteredError, chat_once
from app.llm.budget import ContextBudgetError, fit_output_tokens
from app.llm.providers import PayloadOptions, resolve_capabilities, settings_for_workload
from app.core.schema_validation import Validator, require_fields, require_schema

__all__ = [
    "parse_json_response",
    "require_fields",
    "require_schema",
    "structured_completion",
]


def _escape_control_chars_in_json_strings(text: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
                output.append(char)
            elif char == "\\":
                escaped = True
                output.append(char)
            elif char == '"':
                in_string = False
                output.append(char)
            elif char == "\n":
                output.append("\\n")
            elif char == "\r":
                continue
            elif char == "\t":
                output.append("\\t")
            else:
                output.append(char)
        else:
            output.append(char)
            if char == '"':
                in_string = True
    return "".join(output)


def parse_json_response(raw: str) -> dict[str, Any]:
    """Extract one complete JSON object from noisy model output."""
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.I | re.S).strip()
    cleaned = re.sub(r"^\s*```(?:json)?\s*|```\s*$", "", cleaned, flags=re.I).strip()
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("模型响应中没有 JSON 对象")
    depth = 0
    in_string = False
    escaped = False
    end = -1
    for index, char in enumerate(cleaned[start:], start=start):
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
                end = index + 1
                break
    if end < 0:
        raise ValueError("模型返回的 JSON 未闭合，通常是输出被截断，请重试")
    candidate = cleaned[start:end]
    candidate = re.sub(r"([\[{,:]\s*)[“”]", lambda match: f'{match.group(1)}"', candidate)
    candidate = re.sub(r"[“”](\s*[,}\]:])", lambda match: f'"{match.group(1)}', candidate)
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        parsed = json.loads(_escape_control_chars_in_json_strings(candidate))
    if not isinstance(parsed, dict):
        raise ValueError("模型没有返回 JSON 对象")
    return parsed


async def structured_completion(
    settings: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    timeout_seconds: float,
    temperature: float,
    validate: Validator | None = None,
    token_ceiling: int | None = None,
    workload: str = "planning",
    error_detail: Callable[[Exception], str] = str,
    schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Generate validated JSON and retry once for malformed or rejected output.

    ``schema`` enables constrained decoding when the endpoint supports it
    (llama.cpp ``json_schema`` / vLLM ``response_format``); other endpoints
    keep ``json_object`` plus the bounded repair below.
    """
    routed = settings_for_workload(settings, workload)
    routed["_workload"] = workload
    options = (
        PayloadOptions(json_schema=schema)
        if schema and resolve_capabilities(routed).json_schema
        else None
    )
    if resolve_capabilities(routed).server == "modelscope":
        max_tokens = min(4096, max(2400, max_tokens))
        token_ceiling = min(4608, max(int(token_ceiling or 0), max_tokens + 512))
    requested_tokens = max_tokens
    max_tokens = fit_output_tokens(routed, messages, max_tokens)
    warnings = []
    schema_state = {"options": options}

    async def ask(call_messages: list[dict[str, str]], **kwargs: Any) -> str:
        try:
            return await chat_once(
                routed, call_messages, json_mode=True, timeout_seconds=timeout_seconds,
                options=schema_state["options"], **kwargs,
            )
        except httpx.HTTPStatusError as exc:
            # Some servers advertise constrained decoding but reject a given
            # schema (older llama.cpp grammar converter, vLLM backends).  Drop
            # the schema once and fall back to plain json_object mode.
            if schema_state["options"] is None or exc.response is None or exc.response.status_code != 400:
                raise
            schema_state["options"] = None
            warnings.append("模型服务拒绝了 JSON Schema 约束（400），已改用普通 JSON 模式")
            return await chat_once(
                routed, call_messages, json_mode=True, timeout_seconds=timeout_seconds,
                options=None, **kwargs,
            )

    if max_tokens < requested_tokens:
        warnings.append(f"保留全部输入，按剩余上下文将本批输出预算调整为 {max_tokens} tokens")
    try:
        raw = await ask(messages, temperature=temperature, max_tokens=max_tokens)
        result = parse_json_response(raw)
        if validate:
            validate(result)
        return result, warnings
    except (ContextBudgetError, ModelContentFilteredError):
        # Neither changing JSON wording nor replaying the same request can
        # repair a fixed input overflow or a provider content filter.
        raise
    except (ValueError, json.JSONDecodeError, KeyError, TypeError) as first_error:
        quality_failure = any(
            marker in str(first_error)
            for marker in ("至少需要", "过短", "高度重复", "标题重复", "章数合计")
        )
        retry_messages = [dict(message) for message in messages]
        retry_messages.append({
            "role": "user",
            "content": (
                f"上次输出未通过检查：{first_error}。重新从头作答。"
                + ("必须补足详细程度并消除重复，严格达到长度、卷数和章数要求；" if quality_failure
                   else "适当压缩次要数组，但不能省略必填字段；")
                + "只输出一个完整闭合的 JSON 对象，不要解释，不要 Markdown。"
            ),
        })
        retry_tokens = fit_output_tokens(
            routed, retry_messages, min(token_ceiling or 8192, max_tokens + 256)
        )
        raw = await ask(
            retry_messages,
            temperature=max(0.48, temperature) if quality_failure else min(0.15, temperature),
            max_tokens=retry_tokens,
        )
        result = parse_json_response(raw)
        if validate:
            validate(result)
        return result, warnings + [f"首次结构化输出未通过检查，系统已自动重试并恢复：{error_detail(first_error)}"]

"""Bounded prose continuation with durable, input-bound recovery checkpoints."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from app.llm.budget import fit_output_tokens
from app.llm.completion import ModelOutputTruncatedError, StreamOutcome
from app.domain.prose import _clean_repair_continuation, _prose_looks_truncated
from app.llm.client import ModelContentFilteredError


def _join_prose(previous: str, incoming: str) -> str:
    continuation = _clean_repair_continuation(previous, incoming) if previous else incoming.strip()
    separator = "\n\n" if previous.rstrip().endswith(tuple("。！？!?……”’）】")) else ""
    return (
        (previous.rstrip() + separator + continuation.lstrip()).strip()
        if continuation
        else previous
    )


async def receive_prose(
    settings: dict[str, Any],
    messages: list[dict[str, str]],
    *,
    stream: Callable[..., Any],
    checkpoint: dict[str, Any] | None = None,
    save_checkpoint: Callable[[dict[str, Any]], None],
    is_running: Callable[[], bool],
    notify: Callable[[str, str], None],
) -> str:
    identity = {
        "messages": messages,
        "model": settings.get("model", ""),
        "provider": settings.get("provider", ""),
        "context_budget": settings.get("context_budget", 0),
        "max_tokens": settings.get("max_tokens", 3500),
        "generation_settings": {
            key: settings.get(key)
            for key in (
                "base_url",
                "temperature",
                "top_p",
                "top_k",
                "min_p",
                "repeat_penalty",
                "enable_thinking",
                "thinking_budget",
            )
        },
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    candidates = []
    if isinstance(checkpoint, dict):
        receipts = checkpoint.get("receipts", [])
        candidates = (receipts if isinstance(receipts, list) else []) + [checkpoint]
    prior = next(
        (
            item
            for item in reversed(candidates)
            if isinstance(item, dict) and item.get("input_hash") == fingerprint
        ),
        {},
    )
    combined = str(prior.get("draft", ""))
    if not is_running():
        raise asyncio.CancelledError()
    if combined and prior.get("complete") is True:
        return combined
    try:
        attempts = max(1, min(int(settings.get("director_stream_recovery_attempts", 3) or 3), 5))
    except (TypeError, ValueError):
        attempts = 3
    last_saved = time.monotonic()
    last_status_check = last_saved

    def persist(text: str, *, complete: bool = False, reason: str = "unknown") -> None:
        save_checkpoint(
            {
                "input_hash": fingerprint,
                "draft": text,
                "complete": complete,
                "finish_reason": reason,
            }
        )

    for attempt in range(attempts):
        if not is_running():
            raise asyncio.CancelledError()
        active_messages = [dict(item) for item in messages]
        if combined:
            anchor = re.sub(r"\s+", " ", combined[-220:]).strip()
            active_messages += [
                {"role": "assistant", "content": combined},
                {
                    "role": "user",
                    "content": (
                        "上面的 assistant 正文已保留。只从断点续写，先补完未完成的句子，绝不重写、复述或解释。"
                        f"接续锚点：【{anchor}】。保持原计划、人物、时间、地点和视角不变；"
                        "完成剩余任务后自然收束。已经交付的章末状态不得重复执行，"
                        "不得为了续补新增下一章事件、往事或世界规则。"
                    ),
                },
            ]
        routed = dict(settings)
        routed["max_tokens"] = fit_output_tokens(
            routed, active_messages, int(routed.get("max_tokens", 3500))
        )
        pieces: list[str] = []
        outcome = StreamOutcome()
        try:
            async for piece in stream(routed, active_messages, outcome=outcome):
                pieces.append(piece)
                now = time.monotonic()
                # Task payloads may contain several scene receipts. Polling
                # SQLite for every token needlessly re-parses all of them.
                if now - last_status_check >= 0.25:
                    if not is_running():
                        raise asyncio.CancelledError()
                    last_status_check = now
                if len(pieces) == 1 or now - last_saved >= 2:
                    persist(_join_prose(combined, "".join(pieces)))
                    last_saved = now
            if not is_running():
                raise asyncio.CancelledError()
            if not outcome.terminal_received:
                raise httpx.ReadError("模型连接在完成标记前结束，候选尚未确认完成。")
            combined = _join_prose(combined, "".join(pieces))
        except ModelContentFilteredError:
            # A blocked partial response must never become a resumable success.
            persist("", reason="content_filtered")
            raise
        except asyncio.CancelledError:
            persist(_join_prose(combined, "".join(pieces)))
            raise
        except httpx.TransportError:
            combined = _join_prose(combined, "".join(pieces))
            persist(combined, reason="interrupted")
            if attempt + 1 >= attempts:
                raise
            notify(
                f"模型连接中断；已保留 {len(combined)} 字候选，正在续接（{attempt + 1}/{attempts - 1}）",
                "warning",
            )
            await asyncio.sleep(min(8, 2**attempt))
            continue
        except Exception:
            persist(_join_prose(combined, "".join(pieces)), reason="failed")
            raise
        if combined and (outcome.truncated or _prose_looks_truncated(combined)):
            reason = outcome.finish_reason if outcome.truncated else "incomplete_sentence"
            persist(combined, reason=reason)
            if attempt + 1 >= attempts:
                raise ModelOutputTruncatedError(
                    "模型在有界续补后仍未确认完整收束；候选已保存到检查点，请缩小本章任务后继续。"
                )
            message = (
                "模型达到单次输出上限" if outcome.truncated else "模型停在未完成的句子或标点处"
            )
            notify(f"{message}；已保留 {len(combined)} 字候选，正在续接剩余内容", "warning")
            continue
        persist(combined, complete=True, reason=outcome.finish_reason)
        if attempt or prior:
            notify(f"已保留并续接候选正文，共 {len(combined)} 字，等待审校与记忆结算", "success")
        return combined
    raise RuntimeError("正文恢复状态异常")

"""User-facing model failure classification and safe text excerpts."""
from __future__ import annotations

import asyncio

import httpx

from ..core.text import bounded_excerpt

__all__ = ["bounded_excerpt", "planning_exception_detail", "recoverable_model_error"]

def planning_exception_detail(exc: Exception) -> str:
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "本地模型响应超时"
    if isinstance(exc, httpx.ConnectError):
        return "无法连接模型服务，请检查 API 地址、网络连接以及模型服务是否可用"
    if isinstance(exc, httpx.HTTPStatusError):
        detail = ""
        code = ""
        message = ""
        try:
            payload = exc.response.json()
            if isinstance(payload, dict):
                error_payload = payload.get("error")
                if isinstance(error_payload, dict):
                    code = str(error_payload.get("code") or "").strip()
                    message = str(error_payload.get("message") or "").strip()
                else:
                    code = str(payload.get("code") or "").strip()
                    message = str(payload.get("message") or "").strip()
                detail = str(
                    message
                    or payload.get("error")
                    or payload.get("detail")
                    or ""
                )
        except Exception:
            try:
                detail = exc.response.text.strip()[:300]
            except Exception:
                detail = ""
        if exc.response.status_code == 429:
            if code == "insufficient_quota" or "quota" in message.lower():
                return "ModelScope 测试 Token 的可用额度不足，请补充额度或更换可用 Token 后从检查点继续"
            if code == "1113" or "余额不足" in message or "资源包" in message:
                return "智谱账户余额不足或没有可用资源包（错误码 1113），请充值或更换可用账户后从检查点继续"
            if code == "1305" or "访问量过大" in message:
                return (
                    "智谱 GLM 当前访问量过大（错误码 1305）；"
                    "系统已完成多次自动退避重试，请稍后从检查点继续"
                )
            return "模型接口请求过于频繁；系统已自动退避重试，请稍后从检查点继续"
        return (
            f"模型服务返回 HTTP {exc.response.status_code}"
            + (f"：{detail}" if detail else "")
        )
    text = str(exc).strip()
    return text or exc.__class__.__name__


def recoverable_model_error(exc: Exception) -> bool:
    return isinstance(
        exc,
        (
            httpx.HTTPError,
            asyncio.TimeoutError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ),
    )


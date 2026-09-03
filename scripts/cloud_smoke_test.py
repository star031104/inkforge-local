from __future__ import annotations

import asyncio
import getpass
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.llama_client import chat_once, chat_stream, list_models


def settings() -> dict:
    key = (
        os.environ.get("INKFORGE_SILICONFLOW_API_KEY", "").strip()
        or os.environ.get("INKFORGE_SILICONFLOW_TEST_KEY", "").strip()
    )
    if not key:
        key = getpass.getpass("Temporary SiliconFlow API Key (hidden): ").strip()
    if not key:
        raise SystemExit("API Key is empty.")
    return {
        "provider": "siliconflow",
        "base_url": "https://api.siliconflow.cn/v1",
        "api_key": key,
        "model": "Qwen/Qwen3-8B",
        "temperature": 0.72,
        "top_p": 0.9,
        "top_k": 40,
        "min_p": 0.05,
        "enable_thinking": False,
        "max_tokens": 700,
    }


async def main() -> int:
    cfg = settings()
    checks: list[tuple[str, bool, str]] = []
    started = time.time()
    try:
        models = await list_models(cfg)
        checks.append(("models", "Qwen/Qwen3-8B" in models, f"{len(models)} models"))
    except Exception as exc:
        checks.append(("models", False, f"{type(exc).__name__}: {exc}"))
        models = []

    try:
        text = await chat_once(
            cfg,
            [{"role": "user", "content": "只回答：连接正常"}],
            max_tokens=32,
            timeout_seconds=60,
        )
        checks.append(("chat_once", "连接正常" in text, text[:80]))
    except Exception as exc:
        checks.append(("chat_once", False, f"{type(exc).__name__}: {exc}"))

    try:
        raw = await chat_once(
            cfg,
            [{"role": "user", "content": '只输出JSON：{"ok":true,"name":"砚火"}'}],
            max_tokens=96,
            json_mode=True,
            timeout_seconds=60,
        )
        payload = json.loads(raw)
        checks.append(("json_mode", payload.get("ok") is True, raw[:120]))
    except Exception as exc:
        checks.append(("json_mode", False, f"{type(exc).__name__}: {exc}"))

    try:
        chunks: list[str] = []
        async for chunk in chat_stream(
            cfg,
            [{"role": "user", "content": "写80到120字中文历史小说场景，只写正文。"}],
        ):
            chunks.append(chunk)
        prose = "".join(chunks).strip()
        compact = "".join(prose.split())
        checks.append(("stream", len(compact) >= 60 and "<think>" not in prose, f"{len(compact)} chars"))
        (ROOT / "data").mkdir(exist_ok=True)
        (ROOT / "data" / "cloud-smoke-prose.txt").write_text(prose, encoding="utf-8")
    except Exception as exc:
        checks.append(("stream", False, f"{type(exc).__name__}: {exc}"))

    print("\nInkForge SiliconFlow smoke test")
    print("=" * 50)
    for name, ok, detail in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    print(f"elapsed: {time.time() - started:.1f}s")
    passed = all(ok for _, ok, _ in checks)
    if passed:
        print("\nAll cloud smoke checks passed. Temporary key was only read from the process environment.")
        return 0
    print("\nOne or more cloud checks failed.")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))


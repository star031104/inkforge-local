"""Generate the same reviewed chapter with several models for editorial comparison."""

from __future__ import annotations

import argparse
import json
import re
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import httpx


MODELS = (
    "Qwen/Qwen3.5-4B",
    "Qwen/Qwen3-8B",
    "THUDM/GLM-4-9B-0414",
    "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",
)


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        line = next((item for item in block.splitlines() if item.startswith("data:")), "")
        if line:
            events.append(json.loads(line[5:].strip()))
    return events


def metrics(text: str, done: dict) -> dict:
    compact = re.sub(r"\s+", "", text)
    banned = [
        item for item in (
            "系统", "数据", "算法", "模型", "监控", "高危", "管控", "百分比",
            "A/B", "KPI", "数据库", "流程", "机制", "维度", "信息差",
        )
        if item.lower() in text.lower()
    ]
    route_labels = [
        item for item in ("本章目标", "场景目标", "转折点", "结尾钩子", "must_keep", "must_avoid")
        if item in text
    ]
    return {
        "characters": len(compact),
        "paragraphs": len([item for item in re.split(r"\n+", text) if item.strip()]),
        "dialogue_marks": text.count("“") + text.count("”"),
        "banned_terms": banned,
        "route_labels": route_labels,
        "length_repaired": bool(done.get("length_repaired")),
        "provider_calls": done.get("provider_calls"),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:7860")
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--target", type=int, default=1200)
    parser.add_argument(
        "--models",
        nargs="+",
        default=list(MODELS),
        help="One or more provider model IDs; defaults to the full comparison set.",
    )
    parser.add_argument("--output", type=Path, default=Path("artifacts/prose-benchmark"))
    args = parser.parse_args()

    args.output.mkdir(parents=True, exist_ok=True)
    run_dir = args.output / datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    client = httpx.Client(timeout=360)
    project = client.get(f"{args.base_url}/api/projects/{args.project_id}").json()
    chapter = project["chapters"][0]
    results = []

    for index, model in enumerate(args.models, start=1):
        candidate = deepcopy(project)
        candidate["settings"].update(
            {
                "model": model,
                "context_budget": 20000,
                "max_tokens": 2600,
                "enable_thinking": False,
                "thinking_budget": 0,
                "temperature": 0.72,
                "top_p": 0.9,
                "repeat_penalty": 1.1,
            }
        )
        response = client.post(
            f"{args.base_url}/api/generate",
            json={
                "project": candidate,
                "chapter_id": chapter["id"],
                "mode": "instruction",
                "instruction": (
                    "严格执行已定第1章路线，写成可直接出版的历史小说场景。"
                    "只写本章，不解释创作过程，不提前开仓或取得调粮权。"
                ),
                "selection": "",
                "target_words": args.target,
            },
        )
        if response.status_code != 200:
            results.append({"model": model, "error": response.text[:1000]})
            continue
        events = parse_sse(response.text)
        error = next((item for item in events if item.get("type") == "error"), None)
        if error:
            results.append({"model": model, "error": error.get("message", "unknown")})
            continue
        prose = "".join(
            str(item.get("text") or "") for item in events if item.get("type") == "token"
        ).strip()
        done = next((item for item in events if item.get("type") == "done"), {})
        slug = re.sub(r"[^A-Za-z0-9]+", "-", model).strip("-").lower()
        filename = f"{index:02d}-{slug}.md"
        (run_dir / filename).write_text(
            f"# {model}\n\n{prose}\n", encoding="utf-8"
        )
        results.append(
            {"model": model, "file": filename, "metrics": metrics(prose, done)}
        )

    report = {"chapter": chapter.get("title"), "target": args.target, "results": results}
    (run_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"output": str(run_dir), **report}, ensure_ascii=False))


if __name__ == "__main__":
    main()

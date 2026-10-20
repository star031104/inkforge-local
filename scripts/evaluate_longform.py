"""Long-form evaluation runner.

Examples::

    # Generate the three benchmark books against an endpoint and score them
    python scripts/evaluate_longform.py --case all --base-url http://127.0.0.1:8080/v1 \
        --model local-model --prompt-version prose-2.1.0

    # Score existing projects in the local database without any model call
    python scripts/evaluate_longform.py --offline --project-id all

    # Compare two earlier runs (directories or run.json files)
    python scripts/evaluate_longform.py --compare output/eval/A output/eval/B

    # Add pairwise blind judging when comparing benchmark runs of the same case
    python scripts/evaluate_longform.py --compare A B --pairwise --base-url ... --model ...

Outputs go to ``output/eval/<timestamp>-<label>/``: ``run.json`` (all reports),
``<case>.json`` / ``<case>.md`` per book and ``report.md``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.evaluation_suite import (  # noqa: E402
    DEFAULT_TERMINAL_CHARS,
    benchmark_project,
    compare_reports,
    evaluate_benchmark_output,
    evaluate_project,
    load_benchmark_cases,
    load_telemetry_calls,
    pairwise_manuscripts,
    report_markdown,
    validate_pairwise,
)

DEFAULT_DB = Path(os.environ.get("INKFORGE_DB_PATH", ROOT / "data" / "inkforge.db"))


# ---------------------------------------------------------------------------
# Model settings
# ---------------------------------------------------------------------------


def model_settings(args: argparse.Namespace) -> dict[str, Any]:
    settings: dict[str, Any] = {}
    if args.model_config:
        settings.update(json.loads(Path(args.model_config).read_text(encoding="utf-8")))
    for key, value in (
        ("base_url", args.base_url),
        ("model", args.model),
        ("planning_model", args.planning_model),
        ("api_key", args.api_key or os.environ.get("INKFORGE_API_KEY", "")),
    ):
        if value:
            settings[key] = value
    settings.setdefault("provider", "openai_compatible")
    settings.setdefault("temperature", 0.8)
    settings.setdefault("max_tokens", 3500)
    settings.setdefault("context_budget", 32768)
    return settings


def _summarize(text: str) -> str:
    sentences = [s for s in re.split(r"(?<=[。！？])", text) if s.strip()]
    if not sentences:
        return ""
    return (sentences[0] + ("……" + sentences[-1] if len(sentences) > 1 else ""))[:300]


async def generate_case(case: dict[str, Any], settings: dict[str, Any], *, chapters: int | None) -> dict[str, Any]:
    """Light direct path: compile the production prose prompt chapter by chapter.

    Each chapter uses the real ``build_prompt`` with the benchmark plan; the
    previous chapters' text and a deterministic summary give continuity.  The
    full director (audit, revision, memory settlement) is not run here, so the
    numbers isolate prompt + model behaviour.
    """
    from app.llm.client import chat_once
    from app.prompts import build_prompt
    from app.llm.providers import settings_for_workload

    project = benchmark_project(case, chapters=chapters)
    project["settings"].update({k: v for k, v in settings.items() if k != "api_key"})
    project["settings"]["api_key"] = settings.get("api_key", "")
    target = int(case.get("target_words", 1600))
    writing = settings_for_workload(project["settings"], "prose")
    for index, chapter in enumerate(project["chapters"]):
        build = build_prompt(
            project,
            {
                "chapter_id": chapter["id"],
                "mode": "instruction",
                "instruction": "按本章计划完成整章正文，章末到达计划的结束状态。",
                "target_words": target,
            },
        )
        text = await chat_once(
            writing,
            build.messages,
            temperature=float(settings.get("temperature", 0.8)),
            max_tokens=int(settings.get("max_tokens", 3500)),
            timeout_seconds=float(settings.get("timeout_seconds", 600)),
        )
        chapter["content"] = text.strip()
        chapter["summary"] = _summarize(chapter["content"])
        print(f"  {case['id']} 第{index + 1}章 {len(chapter['content'])} 字", flush=True)
    project["prompt_version"] = build.prompt_version if project["chapters"] else ""
    return project


# ---------------------------------------------------------------------------
# Offline scoring
# ---------------------------------------------------------------------------


def offline_projects(db_path: Path, project_ids: list[str]) -> list[dict[str, Any]]:
    from app.storage.schema.project import ensure_project_defaults

    if not db_path.is_file():
        raise SystemExit(f"找不到数据库：{db_path}")
    with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT id, payload FROM projects").fetchall()
    projects = []
    for project_id, payload in rows:
        if "all" in project_ids or project_id in project_ids:
            projects.append(ensure_project_defaults(json.loads(payload)))
    if not projects:
        raise SystemExit("没有匹配的作品 id")
    return projects


def telemetry_path(db_path: Path) -> Path:
    return db_path.with_name(db_path.stem + "-telemetry.db")


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _slug(text: str) -> str:
    return re.sub(r"[^\w.-]+", "-", text).strip("-")[:60] or "run"


def write_run(out_dir: Path, run: dict[str, Any], comparison_base: dict[str, Any] | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2, default=dict), encoding="utf-8")
    sections = [f"# 长篇评测运行 {run['label']}", "", f"- 时间：{run['created_at']}", f"- 模式：{run['mode']}", ""]
    for key, report in run["reports"].items():
        base = (comparison_base or {}).get("reports", {}).get(key)
        comparison = compare_reports(base, report) if base else None
        markdown = report_markdown(report, comparison=comparison)
        (out_dir / f"{_slug(key)}.md").write_text(markdown, encoding="utf-8")
        (out_dir / f"{_slug(key)}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=dict), encoding="utf-8"
        )
        sections.append(markdown.replace("# 评测报告", "## 评测报告"))
    (out_dir / "report.md").write_text("\n".join(sections), encoding="utf-8")
    return out_dir


def score_line(report: dict[str, Any]) -> str:
    """One console line with full-text and terminal-window numbers side by side."""
    metrics = report.get("metrics", {})
    terminal = metrics.get("terminal", {}) or {}
    constory = metrics.get("constory", {}) or {}
    machine = metrics.get("machine", {}) or {}
    window = "全文" if terminal.get("whole_text") else f"末{terminal.get('window_chars', 0)}字"
    return (
        f"冲突率 全文 {metrics.get('conflict_rate')} / 终局({window}) {terminal.get('conflict_rate')}；"
        f"一致性问题密度 全文 {constory.get('density')} / 终局 {terminal.get('constory_density')}；"
        f"套话率 {metrics.get('slop', {}).get('slop_rate')}；"
        f"4字串重复率 {machine.get('ngram_repeat', {}).get('4')}"
    )


def load_run(path: str) -> dict[str, Any]:
    target = Path(path)
    if target.is_dir():
        target = target / "run.json"
    return json.loads(target.read_text(encoding="utf-8"))


async def pairwise_runs(left: dict[str, Any], right: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    from app.llm.structured_output import structured_completion

    async def judge(messages):
        result, _ = await structured_completion(
            settings, messages, max_tokens=900, timeout_seconds=300,
            temperature=0.1, validate=validate_pairwise, workload="critic",
        )
        return result

    results = {}
    for key, report in right.get("reports", {}).items():
        base = left.get("reports", {}).get(key)
        if not base or not base.get("manuscript") or not report.get("manuscript"):
            continue
        results[key] = await pairwise_manuscripts(
            base["manuscript"], report["manuscript"], judge, context=report.get("title", "")
        )
        print(f"  盲评 {key}：左侧（对照）胜率 {results[key]['left_win_rate']}")
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="InkForge 长篇评测")
    parser.add_argument("--case", default="all", help="基准用例 id，逗号分隔或 all")
    parser.add_argument("--chapters", type=int, default=None, help="每本只生成前 N 章")
    parser.add_argument("--offline", action="store_true", help="不调用模型，只评测数据库中已有作品")
    parser.add_argument("--project-id", default="all", help="离线模式的作品 id，逗号分隔或 all")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--compare", nargs=2, metavar=("RUN_A", "RUN_B"))
    parser.add_argument("--pairwise", action="store_true", help="比较时对同一用例的正文做两两盲评")
    parser.add_argument("--model-config", help="JSON 文件，内容为模型设置")
    parser.add_argument("--base-url")
    parser.add_argument("--model")
    parser.add_argument("--planning-model")
    parser.add_argument("--api-key")
    parser.add_argument("--prompt-version", default="", help="写入报告的运行标签")
    parser.add_argument("--baseline", help="与该运行对比，写入差异表")
    parser.add_argument("--input-price", type=float, help="每百万输入 token 单价")
    parser.add_argument("--output-price", type=float, help="每百万输出 token 单价")
    parser.add_argument(
        "--terminal-chars", type=int, default=DEFAULT_TERMINAL_CHARS,
        help="终局窗口字数：只统计位于最后 N 字内的冲突/一致性问题（检测仍对照全文）",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "output" / "eval")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    prices = {
        key: value
        for key, value in (("input_per_mtok", args.input_price), ("output_per_mtok", args.output_price))
        if value is not None
    } or None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    if args.compare:
        left, right = load_run(args.compare[0]), load_run(args.compare[1])
        comparisons = {
            key: compare_reports(left["reports"][key], report)
            for key, report in right.get("reports", {}).items()
            if key in left.get("reports", {})
        }
        pairwise = asyncio.run(pairwise_runs(left, right, model_settings(args))) if args.pairwise else {}
        out = args.output / f"{stamp}-compare"
        out.mkdir(parents=True, exist_ok=True)
        payload = {"left": args.compare[0], "right": args.compare[1], "comparisons": comparisons, "pairwise": pairwise}
        (out / "compare.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=dict), encoding="utf-8")
        lines = [f"# 运行对比\n\n- 对照：{args.compare[0]}\n- 本次：{args.compare[1]}\n"]
        for key, comparison in comparisons.items():
            lines += [f"## {key}", "", "| 指标 | 对照 | 本次 | 变化 | 判断 |", "| --- | --- | --- | --- | --- |"]
            lines += [
                f"| {row['metric']} | {row['left']} | {row['right']} | {row['delta']} | {row['verdict']} |"
                for row in comparison["rows"]
            ]
            if key in pairwise:
                lines.append(f"\n盲评：对照胜率 {pairwise[key]['left_win_rate']}，位置一致率 {pairwise[key]['position_consistency']}")
            lines.append("")
        (out / "compare.md").write_text("\n".join(lines), encoding="utf-8")
        print(f"对比完成：{out}")
        return 0

    reports: dict[str, Any] = {}
    if args.offline:
        ids = [item.strip() for item in args.project_id.split(",") if item.strip()]
        for project in offline_projects(args.db, ids):
            calls = load_telemetry_calls(
                telemetry_path(args.db),
                since=str(project.get("created_at", "")),
                until=str(project.get("updated_at", "")),
            )
            report = evaluate_project(project, calls=calls, prices=prices, terminal_chars=args.terminal_chars)
            reports[f"{project.get('title', '')}-{str(project.get('id', ''))[:8]}"] = report
            print(f"  {project.get('title')}: {score_line(report)}")
        mode = "offline"
    else:
        settings = model_settings(args)
        if not settings.get("base_url") or not settings.get("model"):
            raise SystemExit("生成模式需要 --base-url 与 --model（或 --model-config），离线评测请加 --offline")
        from app.llm import telemetry as model_telemetry

        # Calls of this run go to a private telemetry file, never the author's.
        run_telemetry = args.output / f"{stamp}-telemetry.db"
        previous_telemetry = model_telemetry._path
        model_telemetry.configure(run_telemetry)
        try:
            cases = load_benchmark_cases([item.strip() for item in args.case.split(",")])
            if not cases:
                raise SystemExit(f"找不到基准用例：{args.case}")
            for case in cases:
                started = datetime.now(timezone.utc).isoformat()
                print(f"生成 {case['id']} …", flush=True)
                project = asyncio.run(generate_case(case, settings, chapters=args.chapters))
                calls = load_telemetry_calls(run_telemetry)
                calls = [call for call in calls if str(call.get("created_at", "")) >= started]
                report = evaluate_benchmark_output(
                    case, project, calls=calls, prices=prices, terminal_chars=args.terminal_chars
                )
                print(f"  {case['id']}: {score_line(report)}", flush=True)
                report["manuscript"] = [chapter["content"] for chapter in project["chapters"]]
                report["prompt_version"] = project.get("prompt_version", "")
                reports[case["id"]] = report
        finally:
            model_telemetry._path = previous_telemetry
        mode = "generate"

    run = {
        "label": args.prompt_version or mode,
        "created_at": datetime.now().isoformat(),
        "mode": mode,
        "reports": reports,
    }
    for report in reports.values():
        report["label"] = run["label"]
    base = load_run(args.baseline) if args.baseline else None
    out = write_run(args.output / f"{stamp}-{_slug(run['label'])}", run, base)
    print(f"评测完成：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

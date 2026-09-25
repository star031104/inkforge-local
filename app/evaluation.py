"""Reproducible mechanical checks, intentionally not a literary quality judge."""
from __future__ import annotations

from dataclasses import asdict
from difflib import SequenceMatcher
import hashlib
from typing import Any

from .domain.project_schema import default_project, ensure_project_defaults
from .prompts import PROSE_PROMPT_VERSION, build_prompt
from .quality import local_quality_check
from .style_engine import style_stats


def evaluate_candidate(text: str, *, target_chars: int = 1200, genre: str = "", baseline: str = "") -> dict[str, Any]:
    local = local_quality_check(text, target_words=target_chars, genre=genre)
    return {"text_hash": hashlib.sha256(text.encode()).hexdigest(),
            "characters": len("".join(text.split())), "mechanical_score": local["score"],
            "issues": local["issues"], "style": asdict(style_stats(text)),
            "change_ratio": round(1 - SequenceMatcher(None, baseline, text, autojunk=False).ratio(), 4) if baseline else None,
            "literary_score": None, "continuity_verified": False}


def evaluate_suite(cases: list[dict[str, Any]]) -> dict[str, Any]:
    results = []
    for case in cases:
        text = str(case.get("text", ""))
        result = evaluate_candidate(text, target_chars=int(case.get("target_chars", 1200)), genre=str(case.get("genre", "")))
        categories = {issue["category"] for issue in result["issues"]}
        missing = [c for c in case.get("expected_categories", []) if c not in categories]
        unexpected = [c for c in case.get("forbidden_categories", []) if c in categories]
        results.append({"id": case["id"], "passed": not missing and not unexpected,
                        "missing": missing, "unexpected": unexpected, "result": result})
    return {"schema_version": 1, "cases": results, "passed": sum(r["passed"] for r in results),
            "total": len(results), "note": "规则回归集；不代表模型文学质量，不调用在线模型。"}


def evaluate_prompt_suite(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Compile versioned prompts and verify stable contracts without a model call."""
    results: list[dict[str, Any]] = []
    for case in cases:
        project = default_project("prompt-eval", "提示词回归", "2026-01-01T00:00:00+00:00")
        project["premise"] = "一名档案员调查被城市遗忘的人。"
        project["outline"] = "她从一封无主旧信开始追查，并逐步确认集体遗忘并非自然现象。"
        project["book_rules"] = "没有超自然力量；所有判断必须有可观察证据。"
        project["current_focus"] = "确认旧信和码头之间的联系。"
        project["settings"].update(case.get("settings", {}))
        chapter = project["chapters"][0]
        chapter["content"] = str(case.get("current_content", "雨夜里，她拆开一封没有寄件人的旧信。"))
        chapter["scene_goal"] = "确认信件来源"
        chapter["plan"] = {
            "goal": "确认信件来源",
            "beats": ["检查纸张", "核对档案", "发现码头编号"],
            "ending_state": "决定前往码头",
        }
        future_marker = str(case.get("future_marker", ""))
        if future_marker:
            project["chapters"].append(
                {
                    "id": "future-chapter",
                    "title": "未来章节",
                    "content": future_marker,
                    "summary": future_marker,
                }
            )
            project["memory"]["story_so_far"] = future_marker
            project["characters"] = [
                {
                    "name": "主角",
                    "location": future_marker,
                    "last_state_chapter_number": 2,
                    "state_baseline": {"location": "档案馆"},
                }
            ]
        project = ensure_project_defaults(project)
        request = {
            "chapter_id": chapter["id"],
            "mode": str(case.get("mode", "continue")),
            "instruction": str(case.get("instruction", "继续调查")),
            "selection": str(case.get("selection", "")),
            "target_words": int(case.get("target_words", 600)),
        }
        build = build_prompt(project, request)
        rendered = "\n".join(message["content"] for message in build.messages)
        section_names = {item["name"] for item in build.sections if item.get("selected")}
        missing = [
            value for value in case.get("required_fragments", []) if value not in rendered
        ]
        leaked = [
            value for value in case.get("forbidden_fragments", []) if value in rendered
        ]
        missing_sections = [
            value for value in case.get("required_sections", []) if value not in section_names
        ]
        expected_version = str(case.get("prompt_version", PROSE_PROMPT_VERSION))
        version_ok = build.prompt_version == expected_version
        passed = not missing and not leaked and not missing_sections and version_ok
        results.append(
            {
                "id": case["id"],
                "passed": passed,
                "prompt_version": build.prompt_version,
                "missing": missing,
                "leaked": leaked,
                "missing_sections": missing_sections,
                "estimated_tokens": build.estimated_tokens,
                "budget_warnings": build.budget_warnings,
            }
        )
    return {
        "schema_version": 1,
        "prompt_version": PROSE_PROMPT_VERSION,
        "cases": results,
        "passed": sum(item["passed"] for item in results),
        "total": len(results),
        "note": "提示词结构、边界和版本回归；不调用在线模型。",
    }

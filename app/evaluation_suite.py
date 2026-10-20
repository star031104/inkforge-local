"""Long-form evaluation metrics, benchmark probes and pairwise blind judging.

Everything in this module except :func:`pairwise_compare` /
:func:`pairwise_manuscripts` is deterministic and model-free.  The metrics are
designed to answer one question across prompt versions and model settings:
"does the pipeline produce complete, consistent, less repetitive chapters at an
acceptable cost?"  They are *not* a literary score; literary preference is the
job of the pairwise judge, whose output is always reported separately.

Metric families
---------------
* conflict rate  – deterministic contradictions per 1k characters (DOME-style),
  from benchmark probes, probes derived from the project's own memory (dead
  characters, destroyed or lost items) and contradictory authoritative state.
* slop rate      – generic template phrases + the book's learned fatigue
  phrases per 1k characters.
* abandoned-thread rate – planted foreshadowing not advanced or paid off in
  time.
* telemetry      – extraction truncation rate, critic/prose token ratio,
  average prompt size and optional cost per chapter.
* style/repetition – manuscript health, dialogue ratio, sentence length spread.
* ConStory taxonomy – stored audit issues mapped onto 19 sub-categories in
  five groups (adapted from ConStory-Bench, arXiv 2603.05890).
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
import threading
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from app.manuscript_quality import (
    TEMPLATE_PHRASES,
    count_phrase_hits,
    learn_fatigue_phrases,
    manuscript_health_report,
    normalize_text,
)
from app.memory import normalize_thread_status, thread_lifecycle
from app.model_telemetry import workload_health
from app.core.text import relabel_ab as _relabel
from app.style_engine import style_stats

SCHEMA_VERSION = 1
FIXTURE_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "longform_cases"

# ---------------------------------------------------------------------------
# ConStory-style taxonomy
# ---------------------------------------------------------------------------

CONSTORY_TAXONOMY: dict[str, list[str]] = {
    "人物": ["记忆矛盾", "知情越界", "能力漂移", "技能遗忘", "性格动机突变"],
    "事实细节": ["外貌矛盾", "命名不一致", "数量数值矛盾", "物品状态矛盾"],
    "叙事风格": ["视角漂移", "语气文风突变", "时代语汇失真"],
    "时间线与情节": ["时间矛盾", "因果违背", "弃坑", "情节重复"],
    "世界观": ["规则违背", "地理位置矛盾", "未确认新设定"],
}
SUBCATEGORY_GROUP = {
    sub: group for group, subs in CONSTORY_TAXONOMY.items() for sub in subs
}
NON_CONTINUITY = "非一致性"

# Ordered (sub-category, keywords).  The first rule whose keyword appears in
# the issue category wins; if none matches the category, the message is
# searched with the same order.  Mechanical findings (length, clichés,
# duplicated paragraphs …) are kept out of the continuity taxonomy.
NON_CONTINUITY_KEYWORDS = (
    "长度", "句式", "套话", "措辞", "短句", "重复段落", "重复内容", "复用",
    "截断", "Markdown", "元话语", "疲劳", "格式", "字数",
)
CONSTORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("时代语汇失真", ("时代", "现代术语", "现代词", "术语", "时代语汇")),
    ("知情越界", ("知情", "知识", "信息差", "未来信息", "泄漏", "泄露", "不该知道")),
    ("记忆矛盾", ("记忆", "已死", "复活", "前后矛盾", "遗忘往事")),
    ("技能遗忘", ("遗忘技能", "忘记技能", "技能")),
    ("性格动机突变", ("动机", "性格", "人格", "OOC", "人设", "行为逻辑", "口吻")),
    ("能力漂移", ("能力", "实力", "武力", "伤势", "受伤", "体力")),
    ("外貌矛盾", ("外貌", "衣着", "长相", "发色")),
    ("命名不一致", ("称呼", "命名", "名字", "姓名", "名称")),
    ("数量数值矛盾", ("数量", "数字", "数值", "金额", "人数", "数目")),
    ("物品状态矛盾", ("物品", "道具", "物件", "器物")),
    ("视角漂移", ("视角", "人称", "POV")),
    ("语气文风突变", ("文风", "语气", "风格")),
    ("时间矛盾", ("时间", "时序", "日期", "季节", "时间线")),
    ("弃坑", ("线索", "伏笔", "回收", "弃坑")),
    ("情节重复", ("重复情节", "重复场景", "同功能", "雷同", "情节重复")),
    ("地理位置矛盾", ("位置", "地点", "地理", "距离", "路线")),
    ("规则违背", ("规则", "世界观", "硬规则", "契约", "禁令", "Canon", "正典")),
    ("未确认新设定", ("新设定", "新增往事", "待确认", "凭空", "无来源")),
    ("因果违背", ("逻辑", "因果", "情节", "动因")),
)


def classify_issue(issue: dict[str, Any]) -> str:
    """Map a stored audit issue to a ConStory sub-category or ``非一致性``."""
    category = str(issue.get("category", ""))
    message = str(issue.get("message", ""))
    if any(keyword in category for keyword in NON_CONTINUITY_KEYWORDS):
        return NON_CONTINUITY
    for text in (category, message):
        for sub, keywords in CONSTORY_RULES:
            if any(keyword in text for keyword in keywords):
                return sub
    return NON_CONTINUITY if not category else "因果违背"


def constory_counts(issues: Iterable[dict[str, Any]]) -> dict[str, Any]:
    by_sub: Counter[str] = Counter()
    for issue in issues:
        if isinstance(issue, dict):
            by_sub[classify_issue(issue)] += 1
    groups: Counter[str] = Counter()
    for sub, count in by_sub.items():
        if sub in SUBCATEGORY_GROUP:
            groups[SUBCATEGORY_GROUP[sub]] += count
    return {
        "subcategories": {
            sub: by_sub.get(sub, 0) for subs in CONSTORY_TAXONOMY.values() for sub in subs
        },
        "groups": {group: groups.get(group, 0) for group in CONSTORY_TAXONOMY},
        "continuity_total": sum(groups.values()),
        "non_continuity": by_sub.get(NON_CONTINUITY, 0),
    }


def stored_audit_issues(
    project: dict[str, Any], *, include_history: bool = False
) -> list[dict[str, Any]]:
    """Latest audit issues of each chapter (optionally every recorded run)."""
    issues: list[dict[str, Any]] = []
    for number, chapter in enumerate(project.get("chapters", []), start=1):
        if not isinstance(chapter, dict):
            continue
        execution = chapter.get("execution") if isinstance(chapter.get("execution"), dict) else {}
        runs = [execution] if execution else []
        if include_history:
            runs += [item for item in chapter.get("run_history", []) or [] if isinstance(item, dict)]
        for run in runs:
            for issue in run.get("issues", []) or []:
                if isinstance(issue, dict):
                    issues.append({**issue, "chapter": number})
    return issues


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?])|\n+")
DEATH_RE = re.compile(r"(死去|身亡|阵亡|殒命|毙命|气绝|已死|死了|被杀|遇害|丧命|去世|离世)")
LOSS_RE = re.compile(r"(毁|碎|熔|焚|烧毁|丢失|遗失|被夺|被抢|被盗|消磁|失落)")
ACTION_VERBS = "说|道|笑|点头|摇头|站起|走|开口|抬头|伸手|回答|喊|叫道"
REMEMBER_CONTEXT = "回忆|想起|当年|曾经|生前|梦|尸|遗体|遗物|坟|灵|照片|那天"
POSSESS_VERBS = "取出|掏出|拿出|握着|攥着|捧着|递给|递出|佩着|戴着|举起|摸出"
RECOVER_CONTEXT = "碎片|残|回忆|想起|找回|寻回|复得|重铸|新的|熔|毁|如果|假如"


def chapter_rows(project: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for number, chapter in enumerate(project.get("chapters", []), start=1):
        if isinstance(chapter, dict):
            rows.append(
                {
                    "number": number,
                    "id": str(chapter.get("id", "")),
                    "title": str(chapter.get("title", f"第{number}章")),
                    "content": str(chapter.get("content", "")),
                }
            )
    return rows


def char_count(text: str) -> int:
    return len(re.sub(r"\s", "", str(text or "")))


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in SENTENCE_SPLIT_RE.split(str(text or "")) if item and item.strip()]


def _per_k(count: float, chars: int, per: int = 1000) -> float:
    return round(count * per / chars, 4) if chars else 0.0


def chapter_spans(rows: list[dict[str, Any]]) -> dict[int, tuple[int, int]]:
    """``{chapter number: (start, end)}`` in non-whitespace characters."""
    spans: dict[int, tuple[int, int]] = {}
    position = 0
    for row in rows:
        length = char_count(row["content"])
        spans[row["number"]] = (position, position + length)
        position += length
    return spans


# ---------------------------------------------------------------------------
# Conflict probes
# ---------------------------------------------------------------------------


def run_probes(
    chapters: list[dict[str, Any]], probes: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Run ``forbidden`` regex probes; each match is one conflict.

    A probe is ``{"id", "type": "forbidden", "category", "reason", "pattern",
    "from_chapter", "until_chapter"?, "exclude_context"?}``.  Matching is done
    sentence by sentence; a sentence that also matches ``exclude_context`` (a
    memory, a dream, the corpse …) is skipped.  At most one hit per probe and
    sentence is reported.
    """
    conflicts: list[dict[str, Any]] = []
    spans = chapter_spans(chapters)
    for probe in probes:
        if not isinstance(probe, dict) or probe.get("type", "forbidden") != "forbidden":
            continue
        try:
            pattern = re.compile(str(probe.get("pattern", "")))
            exclude = re.compile(str(probe["exclude_context"])) if probe.get("exclude_context") else None
        except re.error:
            continue
        if not pattern.pattern:
            continue
        start = int(probe.get("from_chapter", 1) or 1)
        until = int(probe.get("until_chapter", 0) or 0)
        for chapter in chapters:
            number = chapter["number"]
            if number < start or (until and number > until):
                continue
            position = spans.get(number, (0, 0))[0]
            for sentence in _sentences(chapter["content"]):
                length = char_count(sentence)
                sentence_start, position = position, position + length
                if not pattern.search(sentence):
                    continue
                if exclude and exclude.search(sentence):
                    continue
                conflicts.append(
                    {
                        "probe_id": str(probe.get("id", "")),
                        "category": str(probe.get("category", "因果违背")),
                        "chapter": number,
                        "reason": str(probe.get("reason", "")),
                        "evidence": sentence[:200],
                        "source": str(probe.get("source", "probe")),
                        # Book-level position in non-whitespace characters,
                        # used by the terminal-window metrics.
                        "offset": sentence_start,
                        "end": position,
                    }
                )
    return conflicts


def payoff_threads(
    chapters: list[dict[str, Any]], foreshadow: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Evaluate benchmark foreshadowing: planted items need a later mention.

    An item is ``abandoned`` once the manuscript has reached ``payoff_by`` but
    none of ``payoff_terms`` appears after the planting chapter; ``pending``
    while the deadline is still ahead.
    """
    written = max((row["number"] for row in chapters if row["content"].strip()), default=0)
    results = []
    for item in foreshadow:
        if not isinstance(item, dict):
            continue
        planted = int(item.get("planted_chapter", 1) or 1)
        due = int(item.get("payoff_by", 0) or 0)
        terms = [str(term) for term in item.get("payoff_terms", []) if str(term)]
        hits = [
            row["number"]
            for row in chapters
            if planted < row["number"] <= (due or row["number"])
            and any(term in row["content"] for term in terms)
        ]
        if hits:
            status = "advanced"
        elif due and written >= due:
            status = "abandoned"
        else:
            status = "pending"
        results.append(
            {
                "id": str(item.get("id", "")),
                "title": str(item.get("title", "")),
                "planted_chapter": planted,
                "payoff_by": due,
                "status": status,
                "advanced_in": hits[:10],
                "source": "benchmark",
            }
        )
    return results


def _chapter_numbers(project: dict[str, Any]) -> dict[str, int]:
    return {
        str(chapter.get("id", "")): number
        for number, chapter in enumerate(project.get("chapters", []), start=1)
        if isinstance(chapter, dict)
    }


def _source_number(numbers: dict[str, int], item: dict[str, Any]) -> int:
    for key in ("chapter_number", "valid_from_chapter", "created_chapter_number"):
        try:
            value = int(item.get(key, 0) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    for key in ("source_chapter_id", "chapter_id"):
        if str(item.get(key, "")) in numbers:
            return numbers[str(item.get(key, ""))]
    ref = str(item.get("source_ref", ""))
    if ref.startswith("chapter:"):
        return numbers.get(ref.split(":", 1)[1], 0)
    return 0


def derive_project_probes(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Build forbidden-pattern probes from the project's own settled state.

    * a character whose settled state says they died must not speak or act
      in later chapters;
    * an item recorded as destroyed/lost must not be held or used later.
    Probes are conservative (they skip memories, dreams and remains) so the
    count is a lower bound of real contradictions.
    """
    numbers = _chapter_numbers(project)
    probes: list[dict[str, Any]] = []
    deaths: dict[str, int] = {}
    for number, chapter in enumerate(project.get("chapters", []), start=1):
        if not isinstance(chapter, dict):
            continue
        settlement = chapter.get("settlement") if isinstance(chapter.get("settlement"), dict) else {}
        for update in settlement.get("character_updates", []) or []:
            if not isinstance(update, dict):
                continue
            name = str(update.get("name", "")).strip()
            if name and DEATH_RE.search(str(update.get("state", ""))) and name not in deaths:
                deaths[name] = number
    knowledge = project.get("knowledge", {}) if isinstance(project.get("knowledge"), dict) else {}
    items: dict[str, int] = {}
    for fact in knowledge.get("facts", []) or []:
        if not isinstance(fact, dict) or fact.get("status") not in {None, "", "confirmed"}:
            continue
        predicate = str(fact.get("predicate", ""))
        subject = str(fact.get("subject", "")).strip()
        obj = str(fact.get("object", ""))
        number = _source_number(numbers, fact)
        if not subject or number <= 0:
            continue
        if predicate in {"生死", "存活状态"} and DEATH_RE.search(obj):
            deaths.setdefault(subject, number)
        elif "状态" in predicate and LOSS_RE.search(obj) and subject not in deaths:
            items.setdefault(subject, number)
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    # P1 stores item changes as an ordered event log (``memory.item_events``).
    # Only the latest event per item matters: an item lost and later recovered
    # must not produce a "used after loss" probe.
    latest_events: dict[str, dict[str, Any]] = {}
    for event in sorted(
        (e for e in memory.get("item_events", []) or [] if isinstance(e, dict)),
        key=lambda e: int(e.get("chapter_number", 0) or 0),
    ):
        latest_events[str(event.get("item", "")).strip()] = event
    for item in [*(memory.get("item_states", []) or []), *latest_events.values()]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("item") or item.get("name") or "").strip()
        state = str(item.get("state", "")).lower()
        number = _source_number(numbers, item)
        if name and number > 0 and state in {"destroyed", "lost", "毁坏", "丢失", "已毁", "遗失"}:
            items.setdefault(name, number)
    for name, number in deaths.items():
        probes.append(
            {
                "id": f"derived-dead:{name}",
                "type": "forbidden",
                "category": "记忆矛盾",
                "reason": f"{name}在第{number}章已死亡，之后不应再开口或行动",
                "pattern": re.escape(name) + f"(?:{ACTION_VERBS})",
                "from_chapter": number + 1,
                "exclude_context": f"({REMEMBER_CONTEXT})",
                "source": "derived",
            }
        )
    for name, number in items.items():
        if len(name) < 2:
            continue
        probes.append(
            {
                "id": f"derived-item:{name}",
                "type": "forbidden",
                "category": "物品状态矛盾",
                "reason": f"{name}在第{number}章已毁坏或遗失",
                "pattern": f"(?:{POSSESS_VERBS})[^。]{{0,8}}" + re.escape(name),
                "from_chapter": number + 1,
                "exclude_context": f"({RECOVER_CONTEXT})",
                "source": "derived",
            }
        )
    return probes


MUTABLE_PREDICATES = {"当前地点", "当前状态", "伤势", "生死", "状态", "持有者", "所在地"}


def state_contradictions(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Contradictions inside the authoritative store itself."""
    conflicts: list[dict[str, Any]] = []
    knowledge = project.get("knowledge", {}) if isinstance(project.get("knowledge"), dict) else {}
    slots: dict[tuple[str, str], set[str]] = {}
    for fact in knowledge.get("facts", []) or []:
        if not isinstance(fact, dict) or fact.get("status") != "confirmed":
            continue
        predicate = str(fact.get("predicate", ""))
        if predicate not in MUTABLE_PREDICATES:
            continue
        slot = (str(fact.get("subject", "")), predicate)
        slots.setdefault(slot, set()).add(str(fact.get("object", "")))
    for (subject, predicate), values in sorted(slots.items()):
        if len(values) > 1:
            conflicts.append(
                {
                    "probe_id": f"state:{subject}:{predicate}",
                    "category": "地理位置矛盾" if "地" in predicate else "记忆矛盾",
                    "chapter": 0,
                    "reason": f"{subject}的“{predicate}”同时存在 {len(values)} 个已确认取值",
                    "evidence": "；".join(sorted(values))[:200],
                    "source": "store",
                }
            )
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    for fact in memory.get("facts", []) or []:
        if not isinstance(fact, dict):
            continue
        try:
            start = int(fact.get("valid_from_chapter", 0) or 0)
            end = int(fact.get("valid_until_chapter", 0) or 0)
        except (TypeError, ValueError):
            continue
        if start and end and end < start:
            conflicts.append(
                {
                    "probe_id": f"validity:{fact.get('id', '')}",
                    "category": "时间矛盾",
                    "chapter": start,
                    "reason": "事实的失效章节早于生效章节",
                    "evidence": str(fact.get("text", ""))[:200],
                    "source": "store",
                }
            )
    return conflicts


# ---------------------------------------------------------------------------
# Thread abandonment
# ---------------------------------------------------------------------------


def memory_thread_status(project: dict[str, Any]) -> list[dict[str, Any]]:
    chapters = [c for c in project.get("chapters", []) if isinstance(c, dict)]
    written = sum(bool(str(c.get("content", "")).strip()) for c in chapters)
    target = int(project.get("narrative", {}).get("target_chapters", 0) or 0)
    finished = bool(written and target and written >= target)
    results = []
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    for thread in memory.get("plot_threads", []) or []:
        if not isinstance(thread, dict):
            continue
        status = normalize_thread_status(thread.get("status"))
        if status == "deferred":
            continue
        if status == "closed":
            state = "paid_off"
        else:
            lifecycle = thread_lifecycle(project, thread, max(0, written - 1))
            state = "abandoned" if lifecycle.get("stale") or finished else "pending"
        results.append(
            {
                "id": str(thread.get("id", "")),
                "title": str(thread.get("title", "")),
                "status": state,
                "source": "memory",
            }
        )
    return results


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def load_telemetry_calls(
    path: Path | str | None,
    *,
    since: str = "",
    until: str = "",
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Read model calls from the telemetry database, optionally by time window.

    Telemetry rows carry no project id (by design they store no content), so a
    project's calls are approximated by the project's active time window.
    """
    if not path or not Path(path).is_file():
        return []
    try:
        with sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=2) as db:
            query = "SELECT payload FROM model_calls"
            params: list[Any] = []
            clauses = []
            if since:
                clauses.append("created_at >= ?")
                params.append(since)
            if until:
                clauses.append("created_at <= ?")
                params.append(until)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            query += " ORDER BY created_at DESC LIMIT ?"
            params.append(max(1, int(limit)))
            rows = db.execute(query, params).fetchall()
    except sqlite3.Error:
        return []
    calls = []
    for (payload,) in rows:
        try:
            calls.append(json.loads(payload))
        except (TypeError, ValueError):
            continue
    return calls


def _tokens(call: dict[str, Any]) -> tuple[int, int]:
    prompt = call.get("prompt_tokens") or call.get("estimated_prompt_tokens") or 0
    completion = call.get("completion_tokens") or call.get("estimated_completion_tokens") or 0
    try:
        return int(prompt), int(completion)
    except (TypeError, ValueError):
        return 0, 0


def telemetry_metrics(
    calls: list[dict[str, Any]],
    *,
    chapters: int = 0,
    prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Cost and truncation metrics from model-call telemetry.

    ``prices`` = ``{"input_per_mtok": x, "output_per_mtok": y}`` in any
    currency; without prices the cost stays ``None`` rather than guessed.
    """
    if not calls:
        return {"calls": 0, "available": False}
    health = workload_health(calls)
    totals: dict[str, list[int]] = {}
    prompt_sizes = []
    provider_reported = 0
    for call in calls:
        workload = str(call.get("workload") or "unknown")
        prompt, completion = _tokens(call)
        bucket = totals.setdefault(workload, [0, 0, 0])
        bucket[0] += prompt
        bucket[1] += completion
        bucket[2] += 1
        prompt_sizes.append(prompt)
        provider_reported += call.get("token_source") in {"provider", "usage"}
    prose = sum(totals.get(name, [0, 0, 0])[0] + totals.get(name, [0, 0, 0])[1] for name in ("prose", "revision"))
    critic = sum(totals.get(name, [0, 0, 0])[0] + totals.get(name, [0, 0, 0])[1] for name in ("critic", "audit"))
    input_tokens = sum(item[0] for item in totals.values())
    output_tokens = sum(item[1] for item in totals.values())
    cost = None
    if prices and (prices.get("input_per_mtok") or prices.get("output_per_mtok")):
        cost = round(
            input_tokens / 1e6 * float(prices.get("input_per_mtok") or 0)
            + output_tokens / 1e6 * float(prices.get("output_per_mtok") or 0),
            4,
        )
    extraction = health.get("extraction", {})
    return {
        "available": True,
        "calls": len(calls),
        "failed": sum(call.get("status") == "failed" for call in calls),
        "provider_reported_share": round(provider_reported / len(calls), 3),
        "workloads": {
            name: {
                "calls": item[2],
                "prompt_tokens": item[0],
                "completion_tokens": item[1],
                **{k: v for k, v in health.get(name, {}).items() if k != "calls"},
            }
            for name, item in sorted(totals.items())
        },
        "extraction_length_rate": extraction.get("length_rate", 0.0) if extraction else None,
        "length_rate": round(
            sum(str(c.get("finish_reason", "")) in {"length", "max_tokens"} for c in calls) / len(calls), 3
        ),
        "critic_prose_token_ratio": round(critic / prose, 3) if prose else None,
        "avg_prompt_tokens": round(statistics.mean(prompt_sizes)) if prompt_sizes else 0,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": cost,
        "cost_per_chapter": round(cost / chapters, 4) if cost is not None and chapters else None,
        "tokens_per_chapter": round((input_tokens + output_tokens) / chapters) if chapters else None,
    }


# ---------------------------------------------------------------------------
# Style, slop and repetition
# ---------------------------------------------------------------------------


def slop_metrics(project: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    learned = [
        str(item.get("phrase", ""))
        for item in memory.get("learned_fatigue_phrases", []) or []
        if isinstance(item, dict) and item.get("phrase")
    ]
    if not learned:
        learned = [item["phrase"] for item in learn_fatigue_phrases(rows, project)]
    text = "\n".join(row["content"] for row in rows)
    chars = char_count(text)
    raw = count_phrase_hits(text, [*TEMPLATE_PHRASES, *learned])
    # Template clichés count every time.  A learned phrase is by definition
    # something the book repeats, so only the excess counts: one use per three
    # chapters is allowed (the same budget the prose prompt asks for).
    allowance = max(1, -(-len(rows) // 3))
    hits: Counter[str] = Counter()
    for phrase, count in raw.items():
        excess = count if phrase in TEMPLATE_PHRASES else count - allowance
        if excess > 0:
            hits[phrase] = excess
    template_hits = sum(count for phrase, count in hits.items() if phrase in TEMPLATE_PHRASES)
    learned_hits = sum(hits.values()) - template_hits
    return {
        "slop_rate": _per_k(sum(hits.values()), chars),
        "template_rate": _per_k(template_hits, chars),
        "learned_rate": _per_k(learned_hits, chars),
        "hits": sum(hits.values()),
        "top_phrases": [
            {"phrase": phrase, "count": count, "source": "template" if phrase in TEMPLATE_PHRASES else "learned"}
            for phrase, count in hits.most_common(12)
        ],
    }


def style_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    written = [row for row in rows if row["content"].strip()]
    if not written:
        return {"dialogue_ratio": 0.0, "sentence_length_mean": 0.0, "sentence_length_std": 0.0}
    dialogue = [style_stats(row["content"]).dialogue_ratio for row in written]
    lengths = [
        len(normalize_text(sentence))
        for row in written
        for sentence in re.split(r"(?<=[。！？!?])", row["content"])
        if normalize_text(sentence)
    ]
    chapter_chars = [char_count(row["content"]) for row in written]
    return {
        "dialogue_ratio": round(statistics.mean(dialogue), 4),
        "dialogue_ratio_std": round(statistics.pstdev(dialogue), 4) if len(dialogue) > 1 else 0.0,
        "sentence_length_mean": round(statistics.mean(lengths), 2) if lengths else 0.0,
        "sentence_length_std": round(statistics.pstdev(lengths), 2) if len(lengths) > 1 else 0.0,
        "chapter_chars_mean": round(statistics.mean(chapter_chars)),
        "chapter_chars_std": round(statistics.pstdev(chapter_chars)) if len(chapter_chars) > 1 else 0,
    }


def repetition_metrics(project: dict[str, Any]) -> dict[str, Any]:
    health = manuscript_health_report(project)
    return {
        "health_score": health.get("score"),
        "duplicate_passages": health.get("duplicate_passage_count", 0),
        "similar_chapters": len(health.get("similar_chapters", [])),
        "fatigued_phrases": len(health.get("fatigued_phrases", [])),
        "volume_progression_issues": len(health.get("volume_progression_issues", [])),
        "memory_integrity_issues": len(health.get("memory_integrity_issues", [])),
    }


# ---------------------------------------------------------------------------
# Terminal window (NstAgent-style "does it still hold together at the end?")
# ---------------------------------------------------------------------------

DEFAULT_TERMINAL_CHARS = 10_000


def terminal_window(rows: list[dict[str, Any]], terminal_chars: int = DEFAULT_TERMINAL_CHARS) -> dict[str, Any]:
    """The last ``terminal_chars`` non-whitespace characters of the book.

    Works shorter than the window use the whole text (``whole_text=True``).
    """
    spans = chapter_spans(rows)
    total = max((end for _start, end in spans.values()), default=0)
    size = max(1, int(terminal_chars or DEFAULT_TERMINAL_CHARS))
    start = max(0, total - size)
    first = next(
        (number for number, (_a, end) in sorted(spans.items()) if end > start and end > _a),
        0,
    )
    return {
        "configured_chars": size,
        "start_offset": start,
        "window_chars": total - start,
        "whole_text": start == 0,
        "from_chapter": first,
        "spans": spans,
    }


def _evidence_candidates(issue: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    for key in ("evidence", "quote", "excerpt", "text"):
        value = issue.get(key)
        values += value if isinstance(value, list) else [value]
    return [str(value).strip() for value in values if isinstance(value, str) and len(str(value).strip()) >= 4]


def locate_issue(issue: dict[str, Any], rows_by_number: dict[int, dict[str, Any]],
                 spans: dict[int, tuple[int, int]]) -> tuple[int, int] | None:
    """Book-level ``(start, end)`` of an audit issue's quoted evidence.

    Falls back to the whole chapter span when the quote cannot be found, and
    ``None`` for issues not tied to any chapter.
    """
    number = int(issue.get("chapter", 0) or 0)
    if number not in spans:
        return None
    chapter_start, chapter_end = spans[number]
    content = rows_by_number.get(number, {}).get("content", "")
    for quote in _evidence_candidates(issue):
        for probe in (quote, quote[:24]):
            index = content.find(probe)
            if index >= 0:
                begin = chapter_start + char_count(content[:index])
                return begin, begin + char_count(probe)
    return chapter_start, chapter_end


def terminal_metrics(
    rows: list[dict[str, Any]],
    conflicts: list[dict[str, Any]],
    issues: list[dict[str, Any]],
    *,
    terminal_chars: int = DEFAULT_TERMINAL_CHARS,
) -> dict[str, Any]:
    """Conflict rate and ConStory density counted only inside the final window.

    Detection itself still runs over the whole book (probes are derived from
    all settled state and every chapter is scanned), so an ending that
    contradicts chapter 3 is caught; only the *location* of the contradiction
    decides whether it is counted here.  Items whose location overlaps the
    window count; book-level store contradictions (no chapter) are reported
    as ``unlocated`` and excluded.
    """
    window = terminal_window(rows, terminal_chars)
    start, spans = window["start_offset"], window["spans"]
    rows_by_number = {row["number"]: row for row in rows}

    def conflict_span(item: dict[str, Any]) -> tuple[int, int] | None:
        if "offset" in item:
            return int(item["offset"]), int(item.get("end", item["offset"]))
        number = int(item.get("chapter", 0) or 0)
        return spans.get(number) if number else None

    inside: list[dict[str, Any]] = []
    unlocated = 0
    for item in conflicts:
        span = conflict_span(item)
        if span is None:
            unlocated += 1
        elif span[1] > start or span[0] >= start:
            inside.append(item)
    window_issues = []
    for issue in issues:
        span = locate_issue(issue, rows_by_number, spans)
        if span is not None and (span[1] > start or span[0] >= start):
            window_issues.append(issue)
    constory = constory_counts(window_issues)
    chars = window["window_chars"]
    return {
        "configured_chars": window["configured_chars"],
        "window_chars": chars,
        "start_offset": start,
        "whole_text": window["whole_text"],
        "from_chapter": window["from_chapter"],
        "conflicts": len(inside),
        "conflict_rate": _per_k(len(inside), chars),
        "conflict_categories": dict(Counter(item["category"] for item in inside)),
        "unlocated_conflicts": unlocated,
        "continuity_issues": constory["continuity_total"],
        "constory_density": _per_k(constory["continuity_total"], chars),
        "constory_groups": constory["groups"],
        "non_continuity": constory["non_continuity"],
    }


# ---------------------------------------------------------------------------
# "机器味" statistical signals (deterministic, model-free)
# ---------------------------------------------------------------------------

_WORD_RUN_RE = re.compile(r"[0-9A-Za-z\u3400-\u4dbf\u4e00-\u9fff]+")
TTR_WINDOW = 1000
NGRAM_WINDOW = 5000


def _windowed_mean(values: list[float]) -> float:
    return round(statistics.mean(values), 4) if values else 0.0


def machine_signals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Length-robust lexical statistics often used as AIGC tells.

    * type-token ratio of characters and character bigrams, averaged over
      consecutive 1,000-char windows (MATTR-style, so long books are not
      penalised just for being long);
    * 3/4/5-char n-gram repetition rate = share of n-gram occurrences that
      repeat an n-gram already seen in the same 5,000-char window; n-grams
      never cross punctuation;
    * sentence-length mean, standard deviation and coefficient of variation.

    These are signals for comparing runs of the same pipeline, not a
    detector: human prose can score "machine-like" and vice versa.
    """
    text = "\n".join(row["content"] for row in rows if row["content"].strip())
    runs = _WORD_RUN_RE.findall(text)
    stream = "".join(runs)
    total = len(stream)
    windows = [stream[i:i + TTR_WINDOW] for i in range(0, total, TTR_WINDOW)]
    if len(windows) > 1 and len(windows[-1]) < TTR_WINDOW // 2:
        windows.pop()  # a tiny tail window would inflate the ratio
    char_ttr = [len(set(w)) / len(w) for w in windows if w]
    bigram_ttr = []
    for window in windows:
        grams = [window[i:i + 2] for i in range(len(window) - 1)]
        if grams:
            bigram_ttr.append(len(set(grams)) / len(grams))
    repeats: dict[str, float] = {}
    for n in (3, 4, 5):
        buckets: dict[int, list[str]] = {}
        position = 0
        for run in runs:
            for i in range(len(run) - n + 1):
                buckets.setdefault((position + i) // NGRAM_WINDOW, []).append(run[i:i + n])
            position += len(run)
        rates = [1 - len(set(grams)) / len(grams) for grams in buckets.values() if len(grams) >= 50]
        if not rates and buckets:
            grams = [gram for items in buckets.values() for gram in items]
            rates = [1 - len(set(grams)) / len(grams)]
        repeats[str(n)] = _windowed_mean(rates)
    lengths = [
        len(normalize_text(sentence))
        for sentence in re.split(r"(?<=[。！？!?…])|\n+", text)
        if normalize_text(sentence)
    ]
    mean = statistics.mean(lengths) if lengths else 0.0
    std = statistics.pstdev(lengths) if len(lengths) > 1 else 0.0
    return {
        "sample_chars": total,
        "ttr_char": _windowed_mean(char_ttr),
        "ttr_bigram": _windowed_mean(bigram_ttr),
        "ngram_repeat": repeats,
        "sentence_length_mean": round(mean, 2),
        "sentence_length_std": round(std, 2),
        "sentence_length_cv": round(std / mean, 4) if mean else 0.0,
        "ttr_window": TTR_WINDOW,
        "ngram_window": NGRAM_WINDOW,
    }


# ---------------------------------------------------------------------------
# Whole-project evaluation
# ---------------------------------------------------------------------------


def evaluate_project(
    project: dict[str, Any],
    *,
    calls: list[dict[str, Any]] | None = None,
    probes: Iterable[dict[str, Any]] | None = None,
    foreshadow: Iterable[dict[str, Any]] | None = None,
    prices: dict[str, float] | None = None,
    include_derived: bool = True,
    include_audit_history: bool = False,
    llm_conflicts: list[dict[str, Any]] | None = None,
    terminal_chars: int = DEFAULT_TERMINAL_CHARS,
) -> dict[str, Any]:
    """Compute every deterministic metric for a project or benchmark output.

    ``terminal_chars`` sizes the terminal window (see :func:`terminal_metrics`).
    """
    rows = chapter_rows(project)
    written_rows = [row for row in rows if row["content"].strip()]
    chars = sum(char_count(row["content"]) for row in rows)
    probe_list = list(probes or [])
    if include_derived:
        probe_list += derive_project_probes(project)
    conflicts = run_probes(rows, probe_list) + state_contradictions(project)
    by_category = Counter(item["category"] for item in conflicts)
    threads = memory_thread_status(project) + payoff_threads(rows, foreshadow or [])
    decided = [item for item in threads if item["status"] != "pending"]
    abandoned = [item for item in threads if item["status"] == "abandoned"]
    issues = stored_audit_issues(project, include_history=include_audit_history)
    constory = constory_counts(issues)
    for conflict in conflicts:
        sub = conflict["category"]
        if sub in constory["subcategories"]:
            constory.setdefault("probe_subcategories", Counter())[sub] += 1
    if isinstance(constory.get("probe_subcategories"), Counter):
        constory["probe_subcategories"] = dict(constory["probe_subcategories"])
    telemetry = telemetry_metrics(calls or [], chapters=len(written_rows), prices=prices)
    slop = slop_metrics(project, rows)
    style = style_metrics(rows)
    repetition = repetition_metrics(project)
    terminal = terminal_metrics(rows, conflicts, issues, terminal_chars=terminal_chars)
    constory["density"] = _per_k(constory["continuity_total"], chars)
    abandoned_count = len(abandoned)
    metrics = {
        "conflict_rate": _per_k(len(conflicts), chars),
        "conflicts": len(conflicts),
        "conflict_categories": dict(by_category),
        "conflict_items": conflicts[:80],
        "llm_conflicts": list(llm_conflicts or []),
        "llm_conflict_rate": _per_k(len(llm_conflicts or []), chars) if llm_conflicts is not None else None,
        "slop": slop,
        "threads": {
            "total": len(threads),
            "abandoned": len(abandoned),
            "pending": sum(item["status"] == "pending" for item in threads),
            "abandoned_rate": round(len(abandoned) / len(decided), 3) if decided else 0.0,
            "items": threads[:60],
        },
        "telemetry": telemetry,
        "style": style,
        "repetition": repetition,
        "constory": constory,
        "terminal": terminal,
        "normalized": {
            "conflicts_per_1k": _per_k(len(conflicts), chars),
            "continuity_issues_per_1k": constory["density"],
            "non_continuity_per_1k": _per_k(constory["non_continuity"], chars),
            "slop_hits_per_1k": slop["slop_rate"],
            "duplicate_passages_per_10k": _per_k(repetition.get("duplicate_passages") or 0, chars, 10_000),
            "abandoned_threads_per_10k": _per_k(abandoned_count, chars, 10_000),
        },
        "machine": machine_signals(rows),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project_id": str(project.get("id", "")),
        "title": str(project.get("title", "")),
        "chapters": len(written_rows),
        "characters": chars,
        "metrics": metrics,
        "cards": summary_cards(metrics, chapters=len(written_rows), characters=chars),
        "notes": [
            "冲突率只统计可确定的矛盾（探针、已死人物/已毁物品、权威状态冲突），是下限而不是全部。",
            "终局窗口只统计位于最后约一万字内的问题，但检测时仍对照全书前文。",
            "调用统计按作品活跃时间窗口近似归属，不含正文或提示词。",
            "机械指标不代表文学质量；文学偏好请使用两两盲评。",
        ],
    }


def summary_cards(metrics: dict[str, Any], *, chapters: int, characters: int) -> list[dict[str, Any]]:
    telemetry = metrics.get("telemetry", {})
    terminal = metrics.get("terminal", {}) or {}

    def card(key: str, label: str, value: Any, unit: str = "", hint: str = "", lower_better: bool = True):
        return {"key": key, "label": label, "value": value, "unit": unit, "hint": hint, "lower_is_better": lower_better}

    return [
        card("conflict_rate", "冲突率", metrics["conflict_rate"], "/千字", f"共 {metrics['conflicts']} 处确定矛盾"),
        card(
            "terminal_conflict_rate", "终局冲突率", terminal.get("conflict_rate"), "/千字",
            f"最后 {terminal.get('window_chars', 0)} 字内 {terminal.get('conflicts', 0)} 处"
            + ("（全文不足窗口，按全文）" if terminal.get("whole_text") else ""),
        ),
        card("constory_density", "一致性问题密度", metrics["constory"].get("density"), "/千字", "审校记录的 ConStory 一致性问题"),
        card(
            "terminal_constory_density", "终局一致性问题密度", terminal.get("constory_density"), "/千字",
            f"最后 {terminal.get('window_chars', 0)} 字内 {terminal.get('continuity_issues', 0)} 个",
        ),
        card("slop_rate", "套话率", metrics["slop"]["slop_rate"], "/千字", f"命中 {metrics['slop']['hits']} 次"),
        card("abandoned_rate", "弃坑率", metrics["threads"]["abandoned_rate"], "", f"{metrics['threads']['abandoned']}/{metrics['threads']['total']} 条线索"),
        card("extraction_length_rate", "提取截断率", telemetry.get("extraction_length_rate"), "", "记忆提取因长度上限中断的比例"),
        card("critic_prose_token_ratio", "审校/写作 token 比", telemetry.get("critic_prose_token_ratio"), "", "越高说明审校越贵"),
        card("avg_prompt_tokens", "平均输入 tokens", telemetry.get("avg_prompt_tokens"), "", f"{telemetry.get('calls', 0)} 次调用"),
        card("cost_per_chapter", "每章成本", telemetry.get("cost_per_chapter"), "", "在评测面板填写每百万 token 单价后计算"),
        card("health_score", "全稿健康分", metrics["repetition"]["health_score"], "", "重复、相似章节与记忆完整性", lower_better=False),
        card("dialogue_ratio", "对白占比", metrics["style"]["dialogue_ratio"], "", "参考值，无好坏方向", lower_better=False),
        card("sentence_length_std", "句长离散度", metrics["style"]["sentence_length_std"], "字", "过低意味着句式单调", lower_better=False),
        card("continuity_issues", "审校一致性问题", metrics["constory"]["continuity_total"], "", f"{chapters} 章 · {characters} 字"),
    ]


# ---------------------------------------------------------------------------
# Run comparison
# ---------------------------------------------------------------------------

COMPARE_KEYS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("冲突率", ("conflict_rate",), True),
    ("终局冲突率", ("terminal", "conflict_rate"), True),
    ("一致性问题密度", ("constory", "density"), True),
    ("终局一致性问题密度", ("terminal", "constory_density"), True),
    ("套话率", ("slop", "slop_rate"), True),
    ("弃坑率", ("threads", "abandoned_rate"), True),
    ("提取截断率", ("telemetry", "extraction_length_rate"), True),
    ("审校/写作 token 比", ("telemetry", "critic_prose_token_ratio"), True),
    ("平均输入 tokens", ("telemetry", "avg_prompt_tokens"), True),
    ("每章成本", ("telemetry", "cost_per_chapter"), True),
    ("全稿健康分", ("repetition", "health_score"), False),
    ("重复段落", ("repetition", "duplicate_passages"), True),
    ("对白占比", ("style", "dialogue_ratio"), False),
    ("句长离散度", ("style", "sentence_length_std"), False),
    ("审校一致性问题", ("constory", "continuity_total"), True),
    ("字 TTR", ("machine", "ttr_char"), False),
    ("双字 TTR", ("machine", "ttr_bigram"), False),
    ("4 字串重复率", ("machine", "ngram_repeat", "4"), True),
    ("句长变异系数", ("machine", "sentence_length_cv"), False),
)


def _dig(data: dict[str, Any], path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def compare_reports(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """Metric-by-metric delta of two evaluation reports (right − left)."""
    rows = []
    for label, path, lower_better in COMPARE_KEYS:
        a = _dig(left.get("metrics", {}), path)
        b = _dig(right.get("metrics", {}), path)
        delta = round(b - a, 4) if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        if delta is None or delta == 0:
            verdict = "持平" if delta == 0 else "无数据"
        else:
            verdict = "改善" if (delta < 0) == lower_better else "变差"
        rows.append({"metric": label, "left": a, "right": b, "delta": delta, "verdict": verdict})
    return {
        "left": {"title": left.get("title"), "label": left.get("label", ""), "created_at": left.get("created_at")},
        "right": {"title": right.get("title"), "label": right.get("label", ""), "created_at": right.get("created_at")},
        "rows": rows,
        "improved": sum(row["verdict"] == "改善" for row in rows),
        "regressed": sum(row["verdict"] == "变差" for row in rows),
    }


def report_markdown(report: dict[str, Any], *, comparison: dict[str, Any] | None = None) -> str:
    lines = [f"# 评测报告：{report.get('title', '')}", ""]
    if report.get("label"):
        lines.append(f"- 运行标签：{report['label']}")
    lines += [
        f"- 生成时间：{report.get('created_at', '')}",
        f"- 章节：{report.get('chapters', 0)}　字数：{report.get('characters', 0)}",
        "",
        "| 指标 | 数值 | 说明 |",
        "| --- | --- | --- |",
    ]
    for card in report.get("cards", []):
        value = "—" if card["value"] is None else f"{card['value']}{card['unit']}"
        lines.append(f"| {card['label']} | {value} | {card['hint']} |")
    metrics = report.get("metrics", {})
    terminal = metrics.get("terminal")
    if terminal:
        scope = "全文（不足窗口）" if terminal.get("whole_text") else f"自第{terminal.get('from_chapter')}章起最后 {terminal.get('window_chars')} 字"
        lines += [
            "", "## 全文与终局窗口", "",
            f"终局窗口：{scope}（配置 {terminal.get('configured_chars')} 字；检测仍对照全书前文）", "",
            "| 指标 | 全文 | 终局窗口 |", "| --- | --- | --- |",
            f"| 确定矛盾数 | {metrics.get('conflicts', 0)} | {terminal.get('conflicts', 0)} |",
            f"| 冲突率（/千字） | {metrics.get('conflict_rate')} | {terminal.get('conflict_rate')} |",
            f"| 一致性问题数 | {metrics.get('constory', {}).get('continuity_total', 0)} | {terminal.get('continuity_issues', 0)} |",
            f"| 一致性问题密度（/千字） | {metrics.get('constory', {}).get('density')} | {terminal.get('constory_density')} |",
        ]
        if terminal.get("unlocated_conflicts"):
            lines.append(f"\n另有 {terminal['unlocated_conflicts']} 处权威状态冲突没有章节位置，只计入全文。")
    machine = metrics.get("machine")
    if machine:
        repeat = machine.get("ngram_repeat", {})
        lines += [
            "", "## 机器味信号（确定性统计，仅供同管线对比）", "",
            "| 信号 | 数值 | 方向 |", "| --- | --- | --- |",
            f"| 字 TTR（{machine.get('ttr_window')} 字窗口均值） | {machine.get('ttr_char')} | 越高用字越丰富 |",
            f"| 双字 TTR | {machine.get('ttr_bigram')} | 越高搭配越丰富 |",
            f"| 3/4/5 字串重复率 | {repeat.get('3')} / {repeat.get('4')} / {repeat.get('5')} | 越高越模板化 |",
            f"| 句长标准差 / 变异系数 | {machine.get('sentence_length_std')} / {machine.get('sentence_length_cv')} | 过低句式单调 |",
        ]
    conflicts = metrics.get("conflict_items", [])
    if conflicts:
        lines += ["", "## 确定矛盾", ""]
        for item in conflicts[:30]:
            where = f"第{item['chapter']}章" if item.get("chapter") else "权威状态"
            lines.append(f"- {where}［{item['category']}］{item['reason']}：{item['evidence']}")
    abandoned = [item for item in metrics.get("threads", {}).get("items", []) if item["status"] == "abandoned"]
    if abandoned:
        lines += ["", "## 弃坑线索", ""]
        lines += [f"- {item['title'] or item['id']}（{item['source']}）" for item in abandoned]
    top = metrics.get("slop", {}).get("top_phrases", [])
    if top:
        lines += ["", "## 高频套话", ""]
        lines.append("、".join(f"{item['phrase']}×{item['count']}" for item in top))
    constory = metrics.get("constory", {})
    if constory:
        lines += ["", "## 审校问题分类（ConStory 19 类）", "", "| 大类 | 子类 | 数量 |", "| --- | --- | --- |"]
        for group, subs in CONSTORY_TAXONOMY.items():
            for sub in subs:
                count = constory.get("subcategories", {}).get(sub, 0)
                if count:
                    lines.append(f"| {group} | {sub} | {count} |")
        lines.append(f"| — | 非一致性（机械问题） | {constory.get('non_continuity', 0)} |")
    pairwise = report.get("pairwise")
    if pairwise:
        lines += ["", "## 两两盲评", "", f"- 左侧胜率：{pairwise.get('left_win_rate')}　位置一致率：{pairwise.get('position_consistency')}"]
    if comparison:
        lines += ["", "## 与对照运行比较", "", "| 指标 | 对照 | 本次 | 变化 | 判断 |", "| --- | --- | --- | --- | --- |"]
        for row in comparison["rows"]:
            lines.append(f"| {row['metric']} | {row['left']} | {row['right']} | {row['delta']} | {row['verdict']} |")
    lines += ["", *[f"> {note}" for note in report.get("notes", [])], ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Benchmark fixtures
# ---------------------------------------------------------------------------


def load_benchmark_cases(names: Iterable[str] | str = "all", root: Path = FIXTURE_DIR) -> list[dict[str, Any]]:
    wanted = [names] if isinstance(names, str) else list(names)
    cases = []
    for path in sorted(root.glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        if "all" in wanted or case.get("id") in wanted or path.stem in wanted:
            cases.append(case)
    return cases


def benchmark_project(case: dict[str, Any], *, chapters: int | None = None) -> dict[str, Any]:
    """Turn a benchmark case into a fresh InkForge project with planned chapters."""
    from app.domain.project_schema import default_project, ensure_project_defaults

    project = default_project(f"bench-{case['id']}", case.get("title", case["id"]), "2026-01-01T00:00:00+00:00")
    for key in ("genre", "premise", "outline", "author_intent", "book_rules"):
        project[key] = case.get(key, "")
    project.setdefault("governance", {}).update(case.get("governance", {}))
    project["characters"] = [
        {
            "name": item["name"],
            "role": item.get("role", ""),
            "personality": item.get("personality", ""),
            "knowledge": item.get("knowledge", ""),
            "hard_limits": item.get("hard_limits", ""),
            "importance": "main",
            "active": True,
        }
        for item in case.get("characters", [])
    ]
    project["memory"]["facts"] = [
        {
            "id": f"bench-fact-{index}",
            "text": f"{fact['subject']}｜{fact['predicate']}：{fact['object']}",
            "valid_from_chapter": fact.get("from_chapter", 1),
            "valid_until_chapter": fact.get("until_chapter", 0),
            "importance": 3,
            "active": True,
            "author_only": True,
        }
        for index, fact in enumerate(case.get("facts", []), start=1)
    ]
    route = case.get("route", [])[: chapters or None]
    project["narrative"]["target_chapters"] = len(route)
    project["chapters"] = [
        {
            "id": f"{case['id']}-ch{index:02d}",
            "title": f"第{index}章 {item['title']}",
            "content": "",
            "summary": "",
            "scene_goal": item.get("goal", ""),
            "plan": {
                "goal": item.get("goal", ""),
                "beats": item.get("beats", []),
                "ending_state": item.get("ending_state", ""),
            },
        }
        for index, item in enumerate(route, start=1)
    ]
    return ensure_project_defaults(project)


def evaluate_benchmark_output(case: dict[str, Any], project: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    return evaluate_project(
        project,
        probes=case.get("probes", []),
        foreshadow=case.get("foreshadow", []),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Pairwise blind judging
# ---------------------------------------------------------------------------

RUBRIC: tuple[tuple[str, str, str], ...] = (
    ("causality", "因果推进", "事件是否由人物选择和前因推动，章末是否产生不可逆变化"),
    ("character", "人物可信", "行动、动机、知情范围是否符合人物设定与当前处境"),
    ("dialogue", "对白辨识度", "不看说话人标签也能分辨是谁；对白承担信息或冲突"),
    ("revelation", "信息揭示", "信息给出的时机与方式是否公平、有张力，不提前泄底"),
    ("pacing", "节奏", "场景长短与张弛是否得当，无注水、无仓促跳过"),
    ("freshness", "新鲜度", "意象和句式是否避开套话，细节是否具体独特"),
)
JudgeFn = Callable[[list[dict[str, str]]], Awaitable[dict[str, Any]]]
WINNERS = {"A", "B", "tie"}

PAIRWISE_SYSTEM = (
    "你是严格、公正的中文小说编辑，进行匿名两两比较。两份稿件分别标为 A 与 B，"
    "顺序随机，不代表优劣；长度不是优点。逐项判断哪份更好，可以判平。"
    "只返回严格 JSON："
    '{"dimensions":{"<key>":{"winner":"A|B|tie","reason":"不超过40字"}},"overall":"A|B|tie","summary":"不超过80字"}。'
)


def pairwise_messages(first: str, second: str, *, context: str = "", limit: int = 6000) -> list[dict[str, str]]:
    rubric = "\n".join(f"- {key}（{label}）：{desc}" for key, label, desc in RUBRIC)
    body = (
        (f"【作品背景】\n{context[:1500]}\n\n" if context else "")
        + f"【评判维度】\n{rubric}\n\n【稿件 A】\n{first[:limit]}\n\n【稿件 B】\n{second[:limit]}"
    )
    return [{"role": "system", "content": PAIRWISE_SYSTEM}, {"role": "user", "content": body}]


def validate_pairwise(result: dict[str, Any]) -> None:
    dims = result.get("dimensions")
    if not isinstance(dims, dict):
        raise ValueError("缺少 dimensions")
    for key, _, _ in RUBRIC:
        item = dims.get(key)
        winner = item.get("winner") if isinstance(item, dict) else item
        if str(winner) not in WINNERS:
            raise ValueError(f"维度 {key} 的 winner 无效")


def _winner(item: Any) -> str:
    value = item.get("winner") if isinstance(item, dict) else item
    value = str(value or "tie").strip()
    return value if value in WINNERS else "tie"


# The judge sees anonymous A/B in two swapped orders; reasons are translated
# back to the stable left/right names so the two orders read consistently.
FIRST_ORDER_NAMES = {"A": "左稿", "B": "右稿"}
SECOND_ORDER_NAMES = {"A": "右稿", "B": "左稿"}


async def pairwise_compare(left: str, right: str, judge: JudgeFn, *, context: str = "") -> dict[str, Any]:
    """Judge ``left`` vs ``right`` twice with swapped positions.

    A dimension is credited only when both orders agree; disagreement counts as
    a tie, which removes position bias at the cost of more ties.
    """
    first = await judge(pairwise_messages(left, right, context=context))
    second = await judge(pairwise_messages(right, left, context=context))
    dims: dict[str, Any] = {}
    score = 0.0
    consistent = 0
    for key, label, _ in RUBRIC:
        a = _winner((first.get("dimensions") or {}).get(key))
        b = _winner((second.get("dimensions") or {}).get(key))
        # Map both verdicts to left/right/tie.
        first_view = {"A": "left", "B": "right"}.get(a, "tie")
        second_view = {"A": "right", "B": "left"}.get(b, "tie")
        agreed = first_view == second_view
        consistent += agreed
        winner = first_view if agreed else "tie"
        score += 1.0 if winner == "left" else 0.5 if winner == "tie" else 0.0
        reasons = [
            _relabel(str(((first.get("dimensions") or {}).get(key) or {}).get("reason", "")), FIRST_ORDER_NAMES) if isinstance((first.get("dimensions") or {}).get(key), dict) else "",
            _relabel(str(((second.get("dimensions") or {}).get(key) or {}).get("reason", "")), SECOND_ORDER_NAMES) if isinstance((second.get("dimensions") or {}).get(key), dict) else "",
        ]
        dims[key] = {"label": label, "winner": winner, "consistent": agreed, "reasons": [r for r in reasons if r]}
    overall_first = {"A": "left", "B": "right"}.get(_winner(first.get("overall")), "tie")
    overall_second = {"A": "right", "B": "left"}.get(_winner(second.get("overall")), "tie")
    return {
        "dimensions": dims,
        "left_win_rate": round(score / len(RUBRIC), 3),
        "position_consistency": round(consistent / len(RUBRIC), 3),
        "overall": overall_first if overall_first == overall_second else "tie",
        "summaries": [
            _relabel(str(first.get("summary", "")), FIRST_ORDER_NAMES),
            _relabel(str(second.get("summary", "")), SECOND_ORDER_NAMES),
        ],
    }


async def pairwise_manuscripts(
    left_chapters: list[str], right_chapters: list[str], judge: JudgeFn, *, context: str = ""
) -> dict[str, Any]:
    """Pairwise-judge two manuscripts chapter by chapter and aggregate."""
    pairs = [
        (index, a, b)
        for index, (a, b) in enumerate(zip(left_chapters, right_chapters), start=1)
        if str(a).strip() and str(b).strip()
    ]
    results = []
    for index, a, b in pairs:
        result = await pairwise_compare(a, b, judge, context=context)
        results.append({"chapter": index, **result})
    if not results:
        return {"chapters": [], "left_win_rate": None, "position_consistency": None}
    dimension_rates = {
        key: round(
            statistics.mean(
                1.0 if item["dimensions"][key]["winner"] == "left" else 0.5 if item["dimensions"][key]["winner"] == "tie" else 0.0
                for item in results
            ),
            3,
        )
        for key, _, _ in RUBRIC
    }
    return {
        "chapters": results,
        "left_win_rate": round(statistics.mean(item["left_win_rate"] for item in results), 3),
        "position_consistency": round(statistics.mean(item["position_consistency"] for item in results), 3),
        "dimension_left_rates": dimension_rates,
        "overall": Counter(item["overall"] for item in results),
    }


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


class EvaluationHistory:
    """Append-only JSONL history per project, kept outside the project JSON so
    recording an evaluation never bumps the project version."""

    def __init__(self, root: Path, *, keep: int = 200):
        self.root = Path(root)
        self.keep = keep
        # Metrics are recorded from a worker thread, pairwise results from the
        # event loop: serialise the read-modify-write of the JSONL file.
        self._lock = threading.Lock()

    def _path(self, project_id: str) -> Path:
        safe = _SAFE_ID.sub("_", str(project_id))[:120] or "unknown"
        return self.root / f"{safe}.jsonl"

    def append(self, project_id: str, record: dict[str, Any]) -> dict[str, Any]:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(project_id)
        entry = {"created_at": datetime.now(timezone.utc).isoformat(), **record}
        with self._lock:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            lines.append(json.dumps(entry, ensure_ascii=False, default=_json_default))
            path.write_text("\n".join(lines[-self.keep :]) + "\n", encoding="utf-8")
        return entry

    def list(self, project_id: str, *, limit: int = 30, kind: str = "") -> list[dict[str, Any]]:
        path = self._path(project_id)
        if not path.exists():
            return []
        items = []
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if kind and item.get("kind") != kind:
                continue
            items.append(item)
            if len(items) >= limit:
                break
        return items


def _json_default(value: Any) -> Any:
    if isinstance(value, Counter):
        return dict(value)
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    return str(value)


def history_snapshot(report: dict[str, Any]) -> dict[str, Any]:
    """Compact record of a metrics report for the history list."""
    return {
        "kind": "metrics",
        "chapters": report.get("chapters"),
        "characters": report.get("characters"),
        "cards": [{k: card[k] for k in ("key", "label", "value", "unit")} for card in report.get("cards", [])],
    }

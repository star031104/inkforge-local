"""Simulated readers: what does a reader experience, block by block?

Each persona sees only reader-visible material: the text of the chapter and
(depending on the persona) recaps of chapters already published before it.
Author truth — plans, secrets, open-thread payoffs — is never shown to a
persona.  Only the separate comparison step reads the author's plan, to flag
twists the readers already predicted and confusion the plan never explains.
"""

from __future__ import annotations

import asyncio
import re
import uuid
import weakref
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

REPORTS_KEY = "reader_reports"
JOB_KEY = "reader_job"
DEFAULT_CONCURRENCY = 3
MAX_JOB_CHAPTERS = 12
MAX_REPORTS_PER_CHAPTER = 5
BLOCK_TARGET_CHARS = 500
MAX_BLOCKS = 12

StructuredCompletion = Callable[..., Awaitable[tuple[dict[str, Any], list[str]]]]

PERSONAS: dict[str, dict[str, Any]] = {
    "veteran": {
        "name": "追更老读者",
        "recap_chapters": 6,
        "brief": "你从第一章追更到现在，记得前文情节，最在意人物关系推进、爽点兑现和章末钩子；拖沓、复述前情和人物降智会让你想弃书。",
    },
    "newcomer": {
        "name": "新读者",
        "recap_chapters": 0,
        "brief": "你第一次读这本书，直接从这一章读起，没有任何前情。你最在意能不能看懂人物是谁、发生了什么、为什么要继续读；设定堆砌和指代不清会让你困惑。",
    },
    "critic": {
        "name": "挑剔书评人",
        "recap_chapters": 3,
        "brief": "你是挑剔的网文书评人，读过大量同类作品，对套路、AI 腔、陈词滥调、逻辑漏洞和节奏问题很敏感，打分偏严格。",
    },
}
DEFAULT_PERSONAS = ("veteran", "newcomer", "critic")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def split_blocks(text: str, target: int = BLOCK_TARGET_CHARS, max_blocks: int = MAX_BLOCKS) -> list[dict[str, Any]]:
    paragraphs = [item.strip() for item in re.split(r"\n+", str(text or "")) if item.strip()]
    if not paragraphs:
        return []
    total = sum(len(item) for item in paragraphs)
    target = max(target, -(-total // max_blocks))
    blocks: list[list[str]] = [[]]
    size = 0
    for paragraph in paragraphs:
        if blocks[-1] and size + len(paragraph) > target * 1.3:
            blocks.append([])
            size = 0
        blocks[-1].append(paragraph)
        size += len(paragraph)
    return [
        {"id": f"B{index}", "text": "\n".join(items), "chars": sum(len(p) for p in items)}
        for index, items in enumerate(blocks, start=1)
    ]


def reader_visible_recap(project: dict[str, Any], chapter_index: int, count: int) -> str:
    """Recap built only from already-published text (summaries or tails)."""
    if count <= 0 or chapter_index <= 0:
        return ""
    lines = []
    chapters = project.get("chapters", [])
    for index in range(max(0, chapter_index - count), chapter_index):
        item = chapters[index]
        summary = str(item.get("summary", "")).strip()
        if not summary:
            content = str(item.get("content", "")).strip()
            summary = ("……" + content[-300:]) if content else ""
        if summary:
            lines.append(f"第{index + 1}章《{item.get('title', '')}》：{summary[:400]}")
    return "\n".join(lines)


READER_PROMPT = """你要扮演一位真实的中文网文读者，完全沉浸地阅读下面这一章。
你只知道正文写出来的内容，不知道作者的任何计划；不要替作者解释，也不要写修改建议。
读者身份：{brief}
正文已按顺序切成 {count} 个片段（B1…B{count}）。读完后只返回严格 JSON，不要 Markdown：
{{
  "blocks": [{{"id":"B1","engagement":0-10 的整数,"note":"这一段读起来的真实感受，20 字内"}}],
  "confusion": [{{"block":"B3","question":"读到这里没看懂/想问的问题"}}],
  "predictions": [{{"prediction":"你猜接下来会发生什么","confidence":0-1 的小数}}],
  "hook": {{"works": true 或 false, "reason":"章末是否让你想立刻读下一章，为什么"}},
  "drop_off_block": "最可能想弃读的片段 id；不会弃读填 null",
  "favorite_moment": "最打动你的地方",
  "overall": 0-10 的整数,
  "summary": "一句话读后感"
}}
blocks 必须覆盖全部 {count} 个片段，顺序一致。confusion 最多 5 条，predictions 最多 4 条。"""

COMPARE_PROMPT = """你是小说责任编辑。下面是几位模拟读者读完本章后的预测和困惑，以及作者的后续计划。
请判断：
1. too_predictable：读者已经准确猜中、作者却打算当作反转或悬念的计划项；
2. unexplained：读者的困惑在作者计划里找不到解释（可能是写作疏漏），或计划会在很久以后才解释、需要先给读者一点线索。
只返回严格 JSON：
{"too_predictable":[{"prediction":"读者预测","plan_item":"被猜中的计划","note":"建议"}],
 "unexplained":[{"question":"读者困惑","planned_explanation":"计划中的解释，没有则为空","severity":"high/medium/low","note":"建议"}]}"""


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def normalize_reader_result(result: dict[str, Any], block_ids: list[str]) -> dict[str, Any]:
    raw_blocks = result.get("blocks") if isinstance(result.get("blocks"), list) else []
    by_id = {
        str(item.get("id", "")).strip().upper(): item
        for item in raw_blocks
        if isinstance(item, dict)
    }
    blocks = []
    for position, block_id in enumerate(block_ids):
        item = by_id.get(block_id)
        if item is None and position < len(raw_blocks) and isinstance(raw_blocks[position], dict):
            item = raw_blocks[position]
        item = item or {}
        blocks.append({
            "id": block_id,
            "engagement": round(_clamp(item.get("engagement"), 0, 10, 5)),
            "note": str(item.get("note", ""))[:80],
            "missing": not bool(item),
        })
    drop = result.get("drop_off_block")
    drop = str(drop).strip().upper() if drop not in (None, "", "null") else None
    hook = result.get("hook") if isinstance(result.get("hook"), dict) else {}
    return {
        "blocks": blocks,
        "confusion": [
            {"block": str(item.get("block", "")).upper()[:6], "question": str(item.get("question", ""))[:200]}
            for item in (result.get("confusion") or [])
            if isinstance(item, dict) and str(item.get("question", "")).strip()
        ][:5],
        "predictions": [
            {
                "prediction": str(item.get("prediction", ""))[:200],
                "confidence": round(_clamp(item.get("confidence"), 0, 1, 0.5), 2),
            }
            for item in (result.get("predictions") or [])
            if isinstance(item, dict) and str(item.get("prediction", "")).strip()
        ][:4],
        "hook": {"works": bool(hook.get("works")), "reason": str(hook.get("reason", ""))[:200]},
        "drop_off_block": drop if drop in block_ids else None,
        "favorite_moment": str(result.get("favorite_moment", ""))[:200],
        "overall": round(_clamp(result.get("overall"), 0, 10, 5)),
        "summary": str(result.get("summary", ""))[:200],
    }


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"[\s，。！？、；：,.!?;:\"“”‘’（）()…—]", "", str(text or ""))
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def text_overlap(left: str, right: str) -> float:
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def author_plan_items(project: dict[str, Any], chapter_index: int) -> list[str]:
    """Author-side material: this chapter's turn/hook, next chapters, open thread payoffs."""
    chapters = project.get("chapters", [])
    items: list[str] = []
    for index in range(chapter_index, min(len(chapters), chapter_index + 4)):
        chapter = chapters[index]
        plan = chapter.get("plan", {}) if isinstance(chapter.get("plan"), dict) else {}
        route = chapter.get("route", {}) if isinstance(chapter.get("route"), dict) else {}
        label = "本章" if index == chapter_index else f"第{index + 1}章"
        for key in ("turning_point", "ending_hook", "goal", "conflict", "exit_state"):
            for source in (plan, route):
                value = str(source.get(key, "")).strip()
                if value and len(value) >= 6:
                    items.append(f"{label}·{key}：{value[:200]}")
    for thread in project.get("memory", {}).get("plot_threads", []):
        if isinstance(thread, dict) and thread.get("status") != "closed":
            payoff = str(thread.get("expected_payoff", "")).strip()
            if payoff:
                items.append(f"伏笔《{thread.get('title', '')}》回收：{payoff[:200]}")
    return list(dict.fromkeys(items))[:40]


def local_plan_comparison(predictions: list[dict[str, Any]], plan_items: list[str], threshold: float = 0.45) -> list[dict[str, Any]]:
    flagged = []
    for prediction in predictions:
        best = max(plan_items, key=lambda item: text_overlap(prediction["prediction"], item), default="")
        score = text_overlap(prediction["prediction"], best) if best else 0.0
        if score >= threshold:
            flagged.append({
                "prediction": prediction["prediction"],
                "plan_item": best,
                "note": f"读者预测与计划高度重合（相似度 {score:.2f}），如这是反转，建议增加误导或换一个兑现方式",
                "source": "本地相似度",
            })
    return flagged


def aggregate(readers: list[dict[str, Any]], block_ids: list[str]) -> dict[str, Any]:
    curve = []
    for position, block_id in enumerate(block_ids):
        values = [reader["result"]["blocks"][position]["engagement"] for reader in readers]
        curve.append({"id": block_id, "engagement": round(sum(values) / len(values), 1) if values else 0})
    drops = [reader["result"]["drop_off_block"] for reader in readers if reader["result"]["drop_off_block"]]
    consensus_drop = max(set(drops), key=drops.count) if drops else None
    hook_votes = sum(1 for reader in readers if reader["result"]["hook"]["works"])
    lowest = min(curve, key=lambda item: item["engagement"]) if curve else None
    overall = round(sum(reader["result"]["overall"] for reader in readers) / max(1, len(readers)), 1)
    findings = []
    if lowest and lowest["engagement"] <= 4:
        findings.append(f"{lowest['id']} 是全章最低谷（平均 {lowest['engagement']}），建议压缩或加入冲突")
    if consensus_drop:
        findings.append(f"{drops.count(consensus_drop)} 位读者可能在 {consensus_drop} 弃读")
    if readers and hook_votes * 2 < len(readers):
        findings.append("多数读者认为章末钩子不够有力")
    sagging = 0
    for left, right in zip(curve, curve[1:]):
        sagging = sagging + 1 if right["engagement"] < left["engagement"] else 0
        if sagging >= 3:
            findings.append(f"兴趣在 {right['id']} 前连续下滑三段")
            break
    return {
        "curve": curve,
        "overall": overall,
        "hook_votes": hook_votes,
        "reader_count": len(readers),
        "consensus_drop_off": consensus_drop,
        "lowest_block": lowest,
        "findings": findings,
    }


async def simulate_chapter(
    project: dict[str, Any],
    chapter_index: int,
    *,
    completion: StructuredCompletion,
    personas: list[str] | tuple[str, ...] = DEFAULT_PERSONAS,
    compare_with_plan: bool = True,
) -> dict[str, Any]:
    chapters = project.get("chapters", [])
    if not 0 <= chapter_index < len(chapters):
        raise ValueError("章节不存在")
    chapter = chapters[chapter_index]
    blocks = split_blocks(chapter.get("content", ""))
    if not blocks or sum(block["chars"] for block in blocks) < 200:
        raise ValueError("本章正文不足 200 字，无法模拟阅读")
    block_ids = [block["id"] for block in blocks]
    numbered = "\n\n".join(f"[{block['id']}]\n{block['text']}" for block in blocks)
    settings = project.get("settings", {})
    errors: list[str] = []

    def validate(result: dict[str, Any]) -> None:
        if not isinstance(result, dict) or not isinstance(result.get("blocks"), list):
            raise ValueError("缺少 blocks")

    async def read_as(persona_id: str) -> dict[str, Any]:
        persona = PERSONAS[persona_id]
        recap = reader_visible_recap(project, chapter_index, int(persona["recap_chapters"]))
        user = (f"【你已经读过的前情（读者视角回忆）】\n{recap}\n\n" if recap else "") + (
            f"【第{chapter_index + 1}章《{chapter.get('title', '')}》】\n{numbered}"
        )
        result, _ = await completion(
            settings,
            [
                {"role": "system", "content": READER_PROMPT.format(brief=persona["brief"], count=len(blocks))},
                {"role": "user", "content": user},
            ],
            max_tokens=1800,
            timeout_seconds=240,
            temperature=0.7,
            validate=validate,
            workload="critic",
        )
        return {
            "persona": persona_id,
            "name": persona["name"],
            "result": normalize_reader_result(result, block_ids),
        }

    # Readers are independent, so they read concurrently (one request's
    # latency instead of three); results keep the persona order.
    persona_ids = [p for p in personas if p in PERSONAS][:3]
    outcomes = await asyncio.gather(*(read_as(pid) for pid in persona_ids), return_exceptions=True)
    readers: list[dict[str, Any]] = []
    for persona_id, outcome in zip(persona_ids, outcomes):
        if isinstance(outcome, BaseException):
            if isinstance(outcome, asyncio.CancelledError):
                raise outcome
            errors.append(f"{PERSONAS[persona_id]['name']}：{str(outcome)[:200]}")
        else:
            readers.append(outcome)
    if not readers:
        raise ValueError("所有模拟读者都没有返回结果：" + "；".join(errors))
    summary = aggregate(readers, block_ids)
    predictions = [
        {**prediction, "reader": reader["name"]}
        for reader in readers
        for prediction in reader["result"]["predictions"]
    ]
    confusion = [
        {**item, "reader": reader["name"]}
        for reader in readers
        for item in reader["result"]["confusion"]
    ]
    plan_items = author_plan_items(project, chapter_index)
    comparison: dict[str, Any] = {
        "plan_items": len(plan_items),
        "too_predictable": local_plan_comparison(predictions, plan_items),
        "unexplained": [],
        "method": "local",
    }
    if compare_with_plan and plan_items and (predictions or confusion):
        try:
            compared, _ = await completion(
                settings,
                [
                    {"role": "system", "content": COMPARE_PROMPT},
                    {
                        "role": "user",
                        "content": "【读者预测】\n"
                        + "\n".join(f"- {p['reader']}：{p['prediction']}（把握 {p['confidence']}）" for p in predictions)
                        + "\n\n【读者困惑】\n"
                        + "\n".join(f"- {c['reader']} 在 {c['block']}：{c['question']}" for c in confusion)
                        + "\n\n【作者计划】\n"
                        + "\n".join(f"- {item}" for item in plan_items),
                    },
                ],
                max_tokens=1200,
                timeout_seconds=180,
                temperature=0.2,
                workload="critic",
            )
            seen = {_norm(item["prediction"]) for item in comparison["too_predictable"]}
            for item in compared.get("too_predictable", []) or []:
                if isinstance(item, dict) and _norm(item.get("prediction")) not in seen:
                    comparison["too_predictable"].append({
                        "prediction": str(item.get("prediction", ""))[:200],
                        "plan_item": str(item.get("plan_item", ""))[:200],
                        "note": str(item.get("note", ""))[:200],
                        "source": "AI 对照",
                    })
            comparison["unexplained"] = [
                {
                    "question": str(item.get("question", ""))[:200],
                    "planned_explanation": str(item.get("planned_explanation", ""))[:200],
                    "severity": str(item.get("severity", "medium")) if str(item.get("severity")) in {"high", "medium", "low"} else "medium",
                    "note": str(item.get("note", ""))[:200],
                }
                for item in (compared.get("unexplained") or [])
                if isinstance(item, dict) and str(item.get("question", "")).strip()
            ][:8]
            comparison["method"] = "local+ai"
        except Exception as exc:
            errors.append(f"计划对照：{str(exc)[:200]}")
    elif not plan_items:
        comparison["unexplained"] = [
            {"question": item["question"], "planned_explanation": "", "severity": "low",
             "note": "作者尚未填写后续计划，无法判断是否会解释", "reader": item["reader"]}
            for item in confusion
        ][:8]
    return {
        "id": str(uuid.uuid4()),
        "chapter_id": str(chapter.get("id", "")),
        "chapter_number": chapter_index + 1,
        "chapter_title": str(chapter.get("title", "")),
        "content_chars": sum(block["chars"] for block in blocks),
        "blocks": [{"id": block["id"], "chars": block["chars"], "excerpt": block["text"][:60]} for block in blocks],
        "readers": readers,
        "summary": summary,
        "comparison": comparison,
        "errors": errors,
        "created_at": utc_now(),
    }


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def store_report(extensions: Any, project_id: str, report: dict[str, Any]) -> dict[str, Any]:
    def append(current: Any) -> dict[str, Any]:
        reports = current if isinstance(current, dict) else {}
        items = [item for item in reports.get(report["chapter_id"], []) if isinstance(item, dict)]
        items.append(report)
        reports[report["chapter_id"]] = items[-MAX_REPORTS_PER_CHAPTER:]
        return reports

    # Atomic read-modify-write: two simulations finishing together must not
    # drop each other's report.
    extensions.update(project_id, REPORTS_KEY, append, default={})
    return report


def list_reports(extensions: Any, project_id: str, chapter_id: str = "") -> dict[str, Any]:
    reports = extensions.get(project_id, REPORTS_KEY) or {}
    if chapter_id:
        return {chapter_id: reports.get(chapter_id, [])}
    return reports


# ---------------------------------------------------------------------------
# Multi-chapter runs: bounded concurrency + resumable background job
# ---------------------------------------------------------------------------


# One limit for every reader-simulation model call in the process (all
# projects, the synchronous endpoint and background jobs together).  Kept per
# event loop because an asyncio.Semaphore must not be shared across loops.
_SHARED_LIMITS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def shared_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _SHARED_LIMITS.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(DEFAULT_CONCURRENCY)
        _SHARED_LIMITS[loop] = semaphore
    return semaphore


def bounded_completion(
    completion: StructuredCompletion,
    semaphore: asyncio.Semaphore,
    on_done: Callable[[], None] | None = None,
) -> StructuredCompletion:
    """Wrap ``completion`` so at most ``semaphore``'s value calls run at once.

    One semaphore is shared by every chapter and reader of a run, so the
    limit is on *total* concurrent model calls, not per chapter.
    """

    async def call(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], list[str]]:
        async with semaphore:
            try:
                return await completion(*args, **kwargs)
            finally:
                if on_done is not None:
                    on_done()

    return call


async def simulate_chapters(
    project: dict[str, Any],
    chapter_indexes: list[int],
    *,
    completion: StructuredCompletion,
    personas: list[str] | tuple[str, ...] = DEFAULT_PERSONAS,
    compare_with_plan: bool = True,
    concurrency: int | None = None,
    on_call_done: Callable[[], None] | None = None,
    on_report: Callable[[int, dict[str, Any]], None] | None = None,
) -> list[dict[str, Any] | BaseException]:
    """Simulate several chapters concurrently; results keep the input order.

    Each element is the report or the exception that chapter raised (a
    ``ValueError`` for an unreadable chapter); cancellation propagates.
    """
    # ``concurrency=None`` uses the process-wide limit shared by every run;
    # an explicit value gives this run its own private limit.
    semaphore = (
        shared_semaphore() if concurrency is None
        else asyncio.Semaphore(max(1, int(concurrency or DEFAULT_CONCURRENCY)))
    )
    bounded = bounded_completion(completion, semaphore, on_call_done)

    async def one(position: int, index: int) -> dict[str, Any]:
        report = await simulate_chapter(
            project, index, completion=bounded, personas=personas, compare_with_plan=compare_with_plan,
        )
        if on_report is not None:
            on_report(position, report)
        return report

    outcomes = await asyncio.gather(
        *(one(position, index) for position, index in enumerate(chapter_indexes)), return_exceptions=True
    )
    for outcome in outcomes:
        if isinstance(outcome, asyncio.CancelledError):
            raise outcome
    return list(outcomes)


def estimated_calls(chapters: int, personas: list[str] | tuple[str, ...], compare_with_plan: bool) -> int:
    readers = len([p for p in personas if p in PERSONAS][:3]) or 1
    return chapters * (readers + (1 if compare_with_plan else 0))


def new_reader_job(chapter_ids: list[str], personas: list[str], compare_with_plan: bool) -> dict[str, Any]:
    now = utc_now()
    return {
        "id": str(uuid.uuid4()),
        "status": "running",
        "chapter_ids": list(chapter_ids),
        "personas": list(personas),
        "compare_with_plan": bool(compare_with_plan),
        "calls_total": estimated_calls(len(chapter_ids), personas, compare_with_plan),
        "calls_done": 0,
        "chapters_done": 0,
        "results": [None] * len(chapter_ids),
        "errors": [],
        "cancel_requested": False,
        "created_at": now,
        "updated_at": now,
    }


async def run_reader_job(
    *,
    project: dict[str, Any],
    extensions: Any,
    project_id: str,
    job: dict[str, Any],
    completion: StructuredCompletion,
    concurrency: int | None = None,
) -> dict[str, Any]:
    """Run a stored reader job; progress is checkpointed after every call.

    Every finished chapter report is stored immediately, so cancelling keeps
    the chapters already read.  ``job["results"][i]`` is the report id (or
    error) for ``job["chapter_ids"][i]``, preserving the requested order.
    Positions that already hold a report id are skipped, so the same call
    resumes a job prepared by ``prepare_resume``.
    """
    positions = {str(chapter.get("id")): index for index, chapter in enumerate(project.get("chapters", []))}
    job_id = job["id"]

    def patch(mutate: Callable[[dict[str, Any]], None]) -> None:
        def apply(current: Any) -> dict[str, Any] | None:
            if not isinstance(current, dict) or current.get("id") != job_id:
                return None  # a newer job replaced this one
            mutate(current)
            current["updated_at"] = utc_now()
            return current

        extensions.update(project_id, JOB_KEY, apply)

    def call_done() -> None:
        patch(lambda current: current.__setitem__("calls_done", int(current.get("calls_done", 0)) + 1))

    def report_done(slot: int, report: dict[str, Any]) -> None:
        position = pending[slot]
        report["job_id"] = job_id  # lets a resume recover a report stored just before a crash
        store_report(extensions, project_id, report)

        def mark(current: dict[str, Any]) -> None:
            current["results"][position] = {"report_id": report["id"], "chapter_id": report["chapter_id"]}
            current["chapters_done"] = int(current.get("chapters_done", 0)) + 1

        patch(mark)

    # Positions without a saved report; a resumed job skips the finished ones.
    results = job.get("results") if isinstance(job.get("results"), list) else []
    pending = [
        position for position in range(len(job["chapter_ids"]))
        if not (position < len(results) and isinstance(results[position], dict) and results[position].get("report_id"))
    ]
    indexes = [positions.get(job["chapter_ids"][position], -1) for position in pending]
    try:
        outcomes = await simulate_chapters(
            project,
            indexes,
            completion=completion,
            personas=job.get("personas") or list(DEFAULT_PERSONAS),
            compare_with_plan=bool(job.get("compare_with_plan", True)),
            concurrency=concurrency,
            on_call_done=call_done,
            on_report=report_done,
        )
    except asyncio.CancelledError:
        patch(lambda current: current.update({"status": "cancelled", "cancel_requested": False}))
        raise

    def finish(current: dict[str, Any]) -> None:
        for slot, outcome in enumerate(outcomes):
            if isinstance(outcome, BaseException):
                position = pending[slot]
                message = str(outcome)[:300] or outcome.__class__.__name__
                current["results"][position] = {"chapter_id": current["chapter_ids"][position], "error": message}
                current["errors"].append(f"第 {position + 1} 个所选章节：{message}")
        ok = sum(1 for item in current["results"] if isinstance(item, dict) and item.get("report_id"))
        current["status"] = "completed" if ok else "failed"
        # The plan comparison is skipped when readers made no predictions, so
        # the initial total is an upper bound; report the real count at the end.
        current["calls_total"] = int(current.get("calls_done", 0))

    patch(finish)
    return extensions.get(project_id, JOB_KEY) or job


RESUMABLE_STATUSES = frozenset({"running", "interrupted", "cancelled", "failed"})


def prepare_resume(job: dict[str, Any] | None, saved_reports: dict[str, Any] | None = None) -> dict[str, Any]:
    """Turn an interrupted/cancelled/failed job back into a runnable one.

    Chapters that already have a saved report (recorded in ``results`` or
    found in the report store under this job's id) are kept and skipped;
    failed chapters are retried.  The job id is preserved so the stored
    progress view continues instead of starting over.  Raises ``ValueError``
    when there is nothing to resume.
    """
    if not isinstance(job, dict) or not job.get("chapter_ids"):
        raise ValueError("没有可继续的读者模拟任务")
    if job.get("status") not in RESUMABLE_STATUSES:
        raise ValueError("该读者模拟任务已完成，无需继续")
    resumed = dict(job)
    chapter_ids = [str(item) for item in job["chapter_ids"]]
    results = list(job.get("results") or [])
    results = (results + [None] * len(chapter_ids))[: len(chapter_ids)]
    # Recover reports stored right before a crash but not yet marked.
    for position, chapter_id in enumerate(chapter_ids):
        item = results[position]
        if isinstance(item, dict) and item.get("report_id"):
            continue
        stored = (saved_reports or {}).get(chapter_id, []) if isinstance(saved_reports, dict) else []
        match = next(
            (report for report in reversed(stored)
             if isinstance(report, dict) and report.get("job_id") == job.get("id") and report.get("id")),
            None,
        )
        results[position] = {"report_id": match["id"], "chapter_id": chapter_id} if match else None
    done = sum(1 for item in results if isinstance(item, dict) and item.get("report_id"))
    if done == len(chapter_ids):
        raise ValueError("所选章节都已有读者报告，无需继续")
    personas = job.get("personas") or list(DEFAULT_PERSONAS)
    calls_done = int(job.get("calls_done", 0) or 0)
    resumed.update(
        status="running",
        results=results,
        chapters_done=done,
        errors=[],
        cancel_requested=False,
        calls_total=calls_done + estimated_calls(len(chapter_ids) - done, personas, bool(job.get("compare_with_plan", True))),
        resumed_count=int(job.get("resumed_count", 0) or 0) + 1,
        updated_at=utc_now(),
    )
    return resumed


def public_reader_job(job: dict[str, Any] | None, running: bool) -> dict[str, Any]:
    if not isinstance(job, dict):
        return {"exists": False}
    view = dict(job)
    view["exists"] = True
    view["running"] = running
    total = max(1, int(view.get("calls_total", 0) or 0))
    done = min(int(view.get("calls_done", 0) or 0), total)
    finished = view.get("status") in {"completed", "failed", "cancelled"}
    view["progress"] = 1.0 if view.get("status") == "completed" else round(min(done / total, 0.99 if not finished else 1.0), 3)
    if view.get("status") == "running" and not running:
        view["status"] = "interrupted"
    return view

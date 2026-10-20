"""Staged chapter-memory extraction.

A single "full" memory call asked a small model for seven arrays at once.
Production telemetry showed 64% of those calls ended with
``finish_reason=length``: the JSON was truncated and then repaired or
degraded, silently dropping state.

Extraction now runs as several small, independent stages.  Each stage asks
only for its own fields and receives only the prior state it needs.  The
shared chapter text is placed in an identical leading message so providers
with prefix caching can reuse it across stages.

When a stage fails to produce closed JSON (the typical symptom of
truncation) it is retried with halved item caps instead of repairing broken
JSON.  A failing optional stage contributes an empty array plus a warning; only
the ``core`` stage (summary + rolling progress) is mandatory.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.llm.budget import ContextBudgetError
from app.core.text import bounded_excerpt
from app.story.continuity_state import render_continuity_state
from app.llm.json_schemas import memory_stage_schema
from app.llm.client import ModelContentFilteredError
from app.story.memory import normalize_thread_status
from app.llm.errors import planning_exception_detail, recoverable_model_error

MEMORY_STAGE_SYSTEM_PROMPT = """你是长篇小说的状态观察员。只根据“本章正文”输出本章造成的增量，不得推测未来，不得把修辞、计划或人物猜测当成客观事实。
旧状态与计划只用于判断什么是新增，绝不能当作本章已经发生。每次只输出当前阶段要求的字段，宁少勿猜。
evidence 必须是本章正文中逐字连续出现的 8-30 字短句，不要改写；没有可靠证据的条目不要输出。
必须先保证 JSON 完整闭合，只输出一个 JSON 对象，不要 Markdown，不要解释。"""


@dataclass(frozen=True)
class MemoryStage:
    name: str
    label: str
    fields: tuple[str, ...]
    schema: str
    caps: dict[str, int]
    state_keys: tuple[str, ...]
    max_tokens: int
    required: bool = False


MEMORY_STAGES: tuple[MemoryStage, ...] = (
    MemoryStage(
        name="core",
        label="摘要与场景结算",
        fields=("summary", "story_so_far", "scene_settlement", "continuity_notes"),
        schema="""{{
  "summary":"120-220字因果摘要，包含关键选择、结果和结尾状态",
  "story_so_far":"在已有全书进展基础上更新成300-500字滚动摘要",
  "scene_settlement":{{"goal_achieved":"yes/partial/no","irreversible_changes":["最多{notes}条"],"open_questions":["最多{notes}条"],"closing_state":"不超过60字"}},
  "continuity_notes":["可能需要作者确认的矛盾，最多{notes}条"]
}}""",
        caps={"notes": 3},
        state_keys=("story_so_far",),
        max_tokens=900,
        required=True,
    ),
    MemoryStage(
        name="facts",
        label="原子事实与描写账本",
        fields=("facts", "description_updates"),
        schema="""{{
  "facts":[{{"text":"以后必须保持一致的原子事实，不超过45字","tags":["人物或地点"],"importance":1,"confidence":"confirmed或suspected","visibility":"objective/private/rumor","evidence":"正文连续原句8-30字","supersedes_id":"若替代旧事实则填旧事实ID"}}],
  "description_updates":[{{"character":"人物名","aspect":"外貌/动作/情绪/环境意象","phrase":"本章已用、后续不宜机械复述的显著描写，8-30字"}}]
}}
facts 最多{facts}项，description_updates 最多{descriptions}项；importance 为 1-5。""",
        caps={"facts": 6, "descriptions": 4},
        state_keys=("facts",),
        max_tokens=1000,
    ),
    MemoryStage(
        name="characters",
        label="人物章末状态与知情",
        fields=("character_updates",),
        schema="""{{
  "character_updates":[{{"name":"人物名","state":"章末新状态，不超过35字","location":"章末位置","knowledge_gains":[{{"text":"此人新知道的原子信息","learned_how":"亲历/被告知/推断","certainty":"confirmed或suspected","evidence":"正文连续原句8-30字"}}],"items":"获得、失去或仍持有的重要道具","emotion":"结束时情绪","appearance_state":"仅记录衣着/伤势/伪装等可变外观","evidence":"能证明状态变化的正文连续原句8-30字"}}]
}}
character_updates 最多{characters}项，每人 knowledge_gains 最多{gains}项；只写本章真正发生变化的人物。""",
        caps={"characters": 4, "gains": 2},
        state_keys=("characters",),
        max_tokens=1000,
    ),
    MemoryStage(
        name="threads",
        label="伏笔与事件时间线",
        fields=("plot_threads", "timeline"),
        schema="""{{
  "plot_threads":[{{"thread_id":"更新既有线索时必须使用输入中的ID；新线索留空","title":"稳定线索名","type":"mystery/promise/threat/relationship/goal","status":"open/progressing/deferred/ready/closed","latest":"本章推进","expected_payoff":"最终要回答或兑现什么","payoff_condition":"满足什么正文条件才可回收","target_window":"immediate/near/mid/slow/endgame","stakeholders":["相关人物"],"knowledge_holders":["当前真正知情者"],"payoff":"仅closed时写实际回收结果","evidence":"正文连续原句8-30字"}}],
  "timeline":[{{"time":"故事内时间或相对时间","event":"可验证事件","participants":["亲历人物"],"location":"地点","causes":["直接前因"],"effects":["已发生后果"],"evidence":"正文连续原句8-30字"}}]
}}
plot_threads 最多{threads}项，timeline 最多{timeline}项。既有线索只更新，不得换名复制；只有正文已经兑现 expected_payoff 且满足 payoff_condition 才能 closed。""",
        caps={"threads": 4, "timeline": 4},
        state_keys=("plot_threads",),
        max_tokens=1100,
    ),
    MemoryStage(
        name="relations",
        label="人物关系与信息差",
        fields=("relationship_updates",),
        schema="""{{
  "relationship_updates":[{{"left":"人物A","right":"人物B","state":"章末关系事实","tension":"具体张力","trust":"信任变化","knowledge_gap":"双方信息差","evidence":"正文连续原句8-30字"}}]
}}
relationship_updates 最多{relations}项，只写本章真正发生变化的关系。""",
        caps={"relations": 4},
        state_keys=("relationships",),
        max_tokens=700,
    ),
    MemoryStage(
        name="objects",
        label="道具与伤势状态",
        fields=("item_updates", "injury_updates"),
        schema="""{{
  "item_updates":[{{"item":"重要道具名（与旧状态同名）","state":"active/lost/destroyed/transferred","holder":"章末持有者，没有则留空","note":"变化原因，不超过30字","evidence":"正文连续原句8-30字"}}],
  "injury_updates":[{{"character":"人物名","injury":"伤势名称（如左臂刀伤）","state":"fresh/healing/healed","severity":"light/medium/severe","evidence":"正文连续原句8-30字"}}]
}}
item_updates 最多{items}项，injury_updates 最多{injuries}项；只写本章真正发生变化的重要道具与伤势，普通物件不要输出。""",
        caps={"items": 4, "injuries": 3},
        state_keys=("objects",),
        max_tokens=600,
    ),
)

ARRAY_FIELDS = (
    "character_updates", "facts", "plot_threads", "timeline",
    "relationship_updates", "continuity_notes", "description_updates",
    "item_updates", "injury_updates",
)

StructuredCompletion = Callable[..., Awaitable[tuple[dict[str, Any], list[str]]]]

# Absolute ``time.monotonic()`` deadline for staged extraction in the current
# context.  Callers that wrap the whole memory request in an outer timeout
# (the director) set it so that finished stages are returned instead of being
# discarded when the outer timeout fires.
_MEMORY_DEADLINE: ContextVar[float | None] = ContextVar("memory_extraction_deadline", default=None)
# A stage is not started with less time than this left.
MIN_STAGE_SECONDS = 15.0


@contextmanager
def memory_time_budget(seconds: float) -> Iterator[None]:
    """Bound staged extraction started inside this block to ``seconds``."""
    token = _MEMORY_DEADLINE.set(time.monotonic() + max(0.0, float(seconds)))
    try:
        yield
    finally:
        _MEMORY_DEADLINE.reset(token)


def _halved(caps: dict[str, int]) -> dict[str, int]:
    return {key: max(1, value // 2) for key, value in caps.items()}


def _stage_validator(stage: MemoryStage) -> Callable[[dict[str, Any]], None]:
    def validate(result: dict[str, Any]) -> None:
        if stage.required:
            for key in ("summary", "story_so_far"):
                if not str(result.get(key, "")).strip():
                    raise ValueError(f"模型缺少必要字段：{key}")
        for key in stage.fields:
            if key == "scene_settlement":
                if key in result and not isinstance(result[key], dict):
                    raise ValueError("字段 scene_settlement 必须是对象")
            elif key in ("summary", "story_so_far"):
                continue
            elif key in result and not isinstance(result[key], list):
                raise ValueError(f"字段 {key} 必须是数组")

    return validate


def _trim_to_caps(stage: MemoryStage, result: dict[str, Any], caps: dict[str, int]) -> dict[str, Any]:
    limits = {
        "facts": caps.get("facts"), "description_updates": caps.get("descriptions"),
        "character_updates": caps.get("characters"), "plot_threads": caps.get("threads"),
        "timeline": caps.get("timeline"), "relationship_updates": caps.get("relations"),
        "continuity_notes": caps.get("notes"),
        "item_updates": caps.get("items"), "injury_updates": caps.get("injuries"),
    }
    picked: dict[str, Any] = {}
    for key in stage.fields:
        if key not in result:
            continue
        value = result[key]
        limit = limits.get(key)
        if isinstance(value, list) and limit:
            value = value[: max(limit, 1)]
        picked[key] = value
    return picked


async def extract_staged_memory(
    settings: dict[str, Any],
    shared_context: str,
    state_blocks: dict[str, str],
    *,
    completion: StructuredCompletion,
    stages: tuple[MemoryStage, ...] = MEMORY_STAGES,
    deadline: float | None = None,
    only: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Run every stage and merge the results.

    ``only`` restricts extraction to the named stages (used to repair the
    stages that failed on an earlier pass).  The core stage is then mandatory
    only when it is selected.

    ``deadline`` is an absolute ``time.monotonic()`` value (defaults to the
    one set by :func:`memory_time_budget`).  Each call is bounded by the
    remaining time; optional stages that cannot start or finish in time are
    reported in ``failed_stages`` and the completed stages are returned.

    ``shared_context`` contains the protocol and chapter text and is sent as
    an identical first user message for every stage.  ``state_blocks`` maps a
    stage's ``state_keys`` to rendered prior state.

    Raises the core stage's exception when the mandatory stage cannot be
    recovered, so callers can apply their existing local fallback.
    """
    merged: dict[str, Any] = {key: [] for key in ARRAY_FIELDS}
    merged["scene_settlement"] = {}
    warnings: list[str] = []
    report: list[dict[str, Any]] = []
    if deadline is None:
        deadline = _MEMORY_DEADLINE.get()
    if only is not None:
        selected = {str(name) for name in only}
        stages = tuple(stage for stage in stages if stage.name in selected)

    def remaining() -> float | None:
        return None if deadline is None else deadline - time.monotonic()

    for stage in stages:
        caps = dict(stage.caps)
        attempts = 0
        last_error: Exception | None = None
        left = remaining()
        if left is not None and left < MIN_STAGE_SECONDS and not stage.required:
            report.append({"stage": stage.name, "status": "skipped", "attempts": 0,
                           "error": "time_budget_exhausted"})
            warnings.append(f"{stage.label}因记忆提取时间预算用尽而跳过，本阶段暂不更新")
            continue
        while attempts < 3:
            left = remaining()
            if left is not None and left <= 0 and attempts:
                last_error = asyncio.TimeoutError("记忆提取时间预算用尽")
                break
            attempts += 1
            state = "\n".join(
                state_blocks.get(key, "") for key in stage.state_keys if state_blocks.get(key)
            )
            instruction = (
                f"【当前阶段：{stage.label}】\n"
                + (f"{state}\n" if state else "")
                + "只返回以下结构的严格 JSON：\n"
                + stage.schema.format(**caps)
            )
            call_timeout = 180.0 if left is None else max(1.0, min(180.0, left))
            try:
                pending = completion(
                    settings,
                    [
                        {"role": "system", "content": MEMORY_STAGE_SYSTEM_PROMPT},
                        {"role": "user", "content": shared_context},
                        {"role": "user", "content": instruction},
                    ],
                    temperature=0.15 if attempts == 1 else 0.1,
                    max_tokens=stage.max_tokens,
                    timeout_seconds=call_timeout,
                    validate=_stage_validator(stage),
                    token_ceiling=stage.max_tokens + 300,
                    workload="extraction",
                    schema=memory_stage_schema(stage.fields),
                )
                # structured_completion retries internally; bound the whole call.
                result, stage_warnings = (
                    await pending if left is None else await asyncio.wait_for(pending, call_timeout)
                )
            except (ContextBudgetError, ModelContentFilteredError) as exc:
                last_error = exc
                break
            except Exception as exc:  # noqa: BLE001 - classified below
                if not recoverable_model_error(exc):
                    raise
                last_error = exc
                left = remaining()
                if left is not None and left < MIN_STAGE_SECONDS:
                    break
                if all(value <= 1 for value in caps.values()):
                    break
                caps = _halved(caps)
                continue
            merged.update(_trim_to_caps(stage, result, caps))
            warnings.extend(f"{stage.label}：{item}" for item in stage_warnings)
            if attempts > 1:
                warnings.append(
                    f"{stage.label}首次输出未闭合，已按减半条目上限重新提取（上限 {caps}）"
                )
            report.append({"stage": stage.name, "status": "completed", "attempts": attempts, "caps": caps})
            last_error = None
            break
        if last_error is not None:
            report.append({
                "stage": stage.name, "status": "failed", "attempts": attempts,
                "error": planning_exception_detail(last_error),
            })
            if stage.required:
                raise last_error
            warnings.append(
                f"{stage.label}提取失败，本阶段暂不更新：{planning_exception_detail(last_error)}"
            )
    for key in ARRAY_FIELDS:
        if not isinstance(merged.get(key), list):
            merged[key] = []
    if not isinstance(merged.get("scene_settlement"), dict):
        merged["scene_settlement"] = {}
    merged["summary"] = str(merged.get("summary", ""))
    merged["story_so_far"] = str(merged.get("story_so_far", ""))[:4000]
    merged["warnings"] = warnings
    merged["extraction_stages"] = report
    failed = [item["stage"] for item in report if item["status"] != "completed"]
    # Settlement must not treat the chapter as fully settled while these
    # stages' fields are missing (see memory_settlement).
    merged["failed_stages"] = failed
    merged["memory_mode"] = "staged" if not failed else "staged_partial"
    merged["fallback"] = False
    return merged


def build_stage_inputs(
    project: dict[str, Any], chapter_index: int, content: str
) -> tuple[str, dict[str, str]]:
    """Render ``(shared_context, state_blocks)`` for one chapter.

    Prior state is the chapter-local view (``chapter_context``): what was true
    before this chapter, never later chapters' state.
    """
    from app.story.temporal_context import chapter_context  # local: avoids an import cycle

    chapter = project.get("chapters", [])[chapter_index]
    prior = chapter_context(project, chapter_index)
    memory = prior.get("memory", {})
    active_threads = [
        item for item in memory.get("plot_threads", [])
        if isinstance(item, dict) and normalize_thread_status(item.get("status")) != "closed"
    ]
    people = ", ".join(str(item.get("name", "")) for item in project.get("characters", []) if isinstance(item, dict))
    shared_context = (
        "【提取协议】\n"
        f"当前是第 {chapter_index + 1} 章。下面“本章正文”是唯一事件证据；计划、旧状态和线索表只用于识别增量，绝不能当成本章已经发生。\n"
        "【当前章节】\n"
        f"章节：{chapter.get('title', '')}\n"
        f"已有角色：{people}\n"
        f"本章计划：{json.dumps(chapter.get('plan', {}), ensure_ascii=False)}\n"
        "本章正文：\n"
        f"{bounded_excerpt(content, 18000)}"
    )
    state_blocks = {
        "story_so_far": f"【已有全书滚动进展】\n{memory.get('story_so_far', '')}",
        "characters": "【章前人物权威状态】\n" + bounded_excerpt(
            json.dumps(prior.get("characters", []), ensure_ascii=False), 9000
        ),
        "facts": "【章前权威事实（更新事实时保留来源，替换时填写 supersedes_id）】\n" + bounded_excerpt(
            json.dumps(memory.get("facts", [])[-80:], ensure_ascii=False), 7000
        ),
        "plot_threads": "【章前开放线索（更新时必须复用 id，不得换名复制）】\n" + bounded_excerpt(
            json.dumps(active_threads[-50:], ensure_ascii=False), 9000
        ),
        "relationships": "【章前动态关系】\n" + bounded_excerpt(
            json.dumps(memory.get("relationships", [])[-50:], ensure_ascii=False), 5000
        ),
        "objects": "【章前道具与伤势状态】\n" + (render_continuity_state(prior, chapter_index) or "暂无记录"),
    }
    return shared_context, state_blocks


def _sentences(text: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"(?<=[。！？!?])|\n+", text)
        if item.strip()
    ]


def local_memory_result(
    project: dict[str, Any], chapter: dict[str, Any], content: str, reason: str
) -> dict[str, Any]:
    sentences = _sentences(content)
    selected = []
    for item in sentences[:2] + sentences[-3:]:
        if item not in selected:
            selected.append(item)
    summary = "".join(selected)[:500]
    old = str(project.get("memory", {}).get("story_so_far", "")).strip()
    addition = f"{chapter.get('title', '本章')}：{summary}"
    story_so_far = "\n".join(item for item in (old, addition) if item)[-4000:]
    return {
        "summary": summary,
        "story_so_far": story_so_far,
        "character_updates": [],
        "facts": [],
        "plot_threads": [],
        "timeline": [],
        "description_updates": [],
        "continuity_notes": [
            f"AI记忆提取未完成，仅生成了保守的本地基础摘要；人物状态、事实、"
            f"伏笔和时间线未自动推断，请按需手工补充。原因：{reason}"
        ],
        "fallback": True,
        "warnings": [f"已使用本地基础记忆。原因：{reason}"],
    }

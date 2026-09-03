from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from collections import Counter
from copy import deepcopy
from difflib import SequenceMatcher
from pathlib import Path
from collections.abc import Callable
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .db import ProjectStore, ensure_project_defaults, utc_now
from .fallbacks import (
    local_audit_result,
    local_ideas,
    local_memory_result,
    local_style_analysis,
)
from .llama_client import chat_once, chat_stream, list_models
from .memory import (
    normalize_thread_status,
    normalize_thread_timing,
    render_memories,
    render_thread_agenda,
    retrieve_memories,
    select_thread_agenda,
)
from .planning import (
    DIRECTOR_MASTER_BIBLE_PROMPT,
    DIRECTOR_CHAPTER_ROUTE_PROMPT,
    DIRECTOR_VOLUME_CONTRACT_PROMPT,
    DIRECTOR_VOLUME_CORE_PROMPT,
    DIRECTOR_VOLUME_DETAILS_PROMPT,
    MASTER_CORE_PROMPT,
    MASTER_VOLUMES_PROMPT,
    VOLUME_PLAN_PROMPT,
    apply_volume_routes,
    fallback_chapter_plan,
    fallback_master_plan,
    fallback_volume_routes,
    find_volume,
    normalize_master_plan,
    normalize_route,
    normalize_volume_routes,
    render_planning_context,
)
from .prompts import (
    AUDIT_PROMPT,
    CHAPTER_MEMORY_COMPACT_PROMPT,
    CHAPTER_MEMORY_PROMPT,
    CHAPTER_PLAN_PROMPT,
    DIRECTOR_CAST_PROMPT,
    DIRECTOR_CHARACTER_CARD_PROMPT,
    DIRECTOR_SEED_BRIEF_PROMPT,
    DIRECTOR_WORLD_PROMPT,
    IDEAS_PROMPT,
    INCUBATOR_ASSETS_PROMPT,
    INCUBATOR_PROMPT,
    STYLE_ANALYSIS_PROMPT,
    build_prompt,
    build_retrieval_query,
    estimate_tokens,
    render_epistemic_context,
)
from .quality import local_quality_check
from .manuscript_quality import manuscript_health_report, prior_manuscript_text
from .knowledge import (
    graph_snapshot,
    sync_authoritative_entities,
    upsert_fact,
    upsert_relation,
    project_accepted_memory_to_knowledge,
)
from .references import combined_style_corpus, source_similarity_report
from .canon import (
    CANON_ANALYSIS_PROMPT,
    CANON_AUDIT_PROMPT,
    ensure_fanfic_defaults,
    render_canon_context,
)
from .file_parsing import parse_reference_file
from .providers import provider_summary
from .memory_integrity import (
    begin_memory_commit,
    derive_story_so_far,
    get_memory_commit,
    mark_memory_commit,
    validate_memory_commit,
)
from .writing_skills import available_writing_skills, normalize_writing_skill


ROOT = Path(__file__).resolve().parent.parent
store = ProjectStore(ROOT / "data" / "inkforge.db")
APP_VERSION = "0.20.0"
API_SCHEMA_VERSION = 36
app = FastAPI(title="InkForge Local API", version=APP_VERSION)
app.mount("/assets", StaticFiles(directory=ROOT / "static"), name="assets")
director_runners: dict[str, asyncio.Task[None]] = {}


class CreateProject(BaseModel):
    title: str = Field(default="未命名故事", max_length=120)


class GenerateRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str
    mode: str = "continue"
    instruction: str = ""
    selection: str = ""
    target_words: int | None = Field(default=None, ge=100, le=10000)
    skill_ids: list[str] = Field(default_factory=list, max_length=20)


class StyleRequest(BaseModel):
    settings: dict[str, Any]
    sample: str = Field(min_length=100, max_length=100_000)


class ChapterActionRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str
    instruction: str = ""
    draft: str = ""
    commit_id: str = ""


class ApplyMemoryRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str
    result: dict[str, Any]
    commit_id: str = ""


class AcceptChapterRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str


class MemoryCommitStatusRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str
    commit_id: str
    error: str = ""


class IdeasRequest(BaseModel):
    project: dict[str, Any]
    kind: str = "next"
    instruction: str = ""


class IncubatorRequest(BaseModel):
    project: dict[str, Any]
    seed: str
    preferences: str = ""
    story_mode: str = "long"
    target_chapters: int = 30


class AutoDirectorStartRequest(BaseModel):
    source_project: dict[str, Any]
    seed: str = Field(min_length=8, max_length=20_000)
    preferences: str = Field(default="", max_length=12_000)
    story_mode: str = "long"
    target_chapters: int = Field(default=30, ge=3, le=300)
    preferred_volume_count: int | None = Field(default=None, ge=1, le=24)
    target_words: int = Field(default=1200, ge=300, le=5000)
    quality_threshold: int = Field(default=78, ge=50, le=100)
    max_revision_attempts: int = Field(default=2, ge=0, le=3)
    continue_on_quality_debt: bool = True


class PlanningRequest(BaseModel):
    project: dict[str, Any]
    instruction: str = ""
    volume_id: str = ""


class ApplyVolumeRequest(BaseModel):
    project: dict[str, Any]
    volume_id: str


class ManuscriptHealthRequest(BaseModel):
    project: dict[str, Any]


class ReferenceParseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    content_base64: str = Field(min_length=8, max_length=30_000_000)


class ProjectRequest(BaseModel):
    project: dict[str, Any]


class ProjectSearchRequest(BaseModel):
    project_id: str
    query: str = Field(min_length=1, max_length=20_000)
    chapter_id: str = ""
    current_chapter_number: int | None = Field(default=None, ge=1, le=100_000)
    limit: int = Field(default=24, ge=1, le=80)


class SnapshotReplayRequest(BaseModel):
    settings: dict[str, Any]


class SkillMutationRequest(BaseModel):
    item: dict[str, Any]


class CanonCharacterRequest(BaseModel):
    project: dict[str, Any]
    character_id: str
    reference_ids: list[str] = Field(default_factory=list)


class CanonAuditRequest(BaseModel):
    project: dict[str, Any]
    chapter_id: str = ""
    draft: str = Field(min_length=20, max_length=80_000)


class KnowledgeMutationRequest(BaseModel):
    project: dict[str, Any]
    item: dict[str, Any]


def _escape_control_chars_in_json_strings(text: str) -> str:
    """Repair literal line breaks emitted inside JSON strings by small local models."""
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
    cleaned = re.sub(r"<think>.*?</think>", "", raw, flags=re.I | re.S).strip()
    cleaned = re.sub(
        r"^\s*```(?:json)?\s*|```\s*$", "", cleaned, flags=re.I
    ).strip()
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
    # Some local chat templates occasionally use typographic double quotes as
    # JSON delimiters (especially for a dialogue string inside an array). Only
    # normalize quotes next to JSON punctuation; curly quotes inside prose stay
    # untouched and therefore cannot become accidental string terminators.
    candidate = re.sub(
        r"([\[{,:]\s*)[“”]", lambda match: f'{match.group(1)}"', candidate
    )
    candidate = re.sub(
        r"[“”](\s*[,}\]:])", lambda match: f'"{match.group(1)}', candidate
    )
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
    validate: Callable[[dict[str, Any]], None] | None = None,
    token_ceiling: int | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Generate validated JSON and retry once only for incomplete/malformed output."""
    warnings: list[str] = []
    try:
        raw = await chat_once(
            settings,
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=True,
            timeout_seconds=timeout_seconds,
        )
        result = parse_json_response(raw)
        if validate:
            validate(result)
        return result, warnings
    except (ValueError, json.JSONDecodeError, KeyError, TypeError) as first_error:
        retry_messages = [dict(message) for message in messages]
        quality_failure = any(
            marker in str(first_error)
            for marker in ("至少需要", "过短", "高度重复", "标题重复", "章数合计")
        )
        retry_messages.append(
            {
                # Qwen/Gemma templates commonly require the system message to
                # be the first and only leading system turn.
                "role": "user",
                "content": (
                    f"上次输出未通过检查：{first_error}。重新从头作答。"
                    + (
                        "必须补足详细程度并消除重复，尤其严格达到大纲/梗概长度、"
                        "卷数和章数要求；不要用抽象模板名称。"
                        if quality_failure
                        else "适当压缩次要数组，但不能省略必填字段；"
                    )
                    + "必须输出一个完整闭合的 JSON 对象，并在 token 用尽前闭合"
                    "所有字符串、数组和花括号。不要解释，不要 Markdown。"
                ),
            }
        )
        raw = await chat_once(
            settings,
            retry_messages,
            # Malformed JSON benefits from deterministic repair.  A content
            # quality failure (duplicate routes, thin outline, repeated
            # goals) needs more diversity; lowering it to 0.15 makes a local
            # model reproduce the same rejected answer almost verbatim.
            temperature=(
                max(0.48, temperature)
                if quality_failure
                else min(0.15, temperature)
            ),
            max_tokens=min(
                token_ceiling or 8192,
                max_tokens + 256,
            ),
            json_mode=True,
            timeout_seconds=timeout_seconds,
        )
        result = parse_json_response(raw)
        if validate:
            validate(result)
        warnings.append(
            f"首次结构化输出不完整，系统已自动重试并恢复：{planning_exception_detail(first_error)}"
        )
        return result, warnings


def require_fields(*fields: str) -> Callable[[dict[str, Any]], None]:
    def validate(result: dict[str, Any]) -> None:
        missing = [
            field
            for field in fields
            if field not in result
            or result[field] is None
            or (isinstance(result[field], str) and not result[field].strip())
        ]
        if missing:
            raise ValueError(f"模型缺少必要字段：{', '.join(missing)}")

    return validate


def require_schema(
    *fields: str,
    list_fields: tuple[str, ...] = (),
    list_bounds: dict[str, tuple[int, int | None]] | None = None,
) -> Callable[[dict[str, Any]], None]:
    base_validator = require_fields(*fields)
    bounds = list_bounds or {}

    def validate(result: dict[str, Any]) -> None:
        base_validator(result)
        for field in list_fields:
            if not isinstance(result.get(field), list):
                raise ValueError(f"字段 {field} 必须是数组")
        for field, (minimum, maximum) in bounds.items():
            value = result.get(field)
            if not isinstance(value, list):
                raise ValueError(f"字段 {field} 必须是数组")
            if len(value) < minimum or (maximum is not None and len(value) > maximum):
                expected = (
                    str(minimum)
                    if maximum == minimum
                    else f"{minimum}-{maximum or '更多'}"
                )
                raise ValueError(
                    f"字段 {field} 应有 {expected} 项，实际 {len(value)} 项"
                )

    return validate


def validate_master_result(
    result: dict[str, Any],
    requested_volumes: int,
    minimum_outline_chars: int,
    target_chapters: int | None = None,
) -> None:
    validate_master_core(result, minimum_outline_chars)
    validate_volume_blueprints(result, requested_volumes, target_chapters)


def _planning_similarity(left: Any, right: Any) -> float:
    a = re.sub(r"[\W_]+", "", str(left or ""))
    b = re.sub(r"[\W_]+", "", str(right or ""))
    if not a or not b:
        return 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    grams_a = {a[index : index + 2] for index in range(max(0, len(a) - 1))}
    grams_b = {b[index : index + 2] for index in range(max(0, len(b) - 1))}
    containment = len(grams_a & grams_b) / max(1, min(len(grams_a), len(grams_b)))
    return max(sequence, containment)


def validate_master_core(
    result: dict[str, Any], minimum_outline_chars: int
) -> None:
    require_schema(
        "theme",
        "reader_promise",
        "central_conflict",
        "story_engine",
        "ending_state",
        "full_outline",
        "main_plot",
        "theme_progression",
        "pacing_plan",
        "stakes_ladder",
        "major_character_arcs",
        "subplots",
        "historical_nodes",
        list_fields=(
            "stakes_ladder",
            "major_character_arcs",
            "subplots",
            "historical_nodes",
        ),
    )(result)
    if len(str(result.get("full_outline", "")).strip()) < minimum_outline_chars:
        raise ValueError(
            f"详细全书大纲至少需要 {minimum_outline_chars} 字，"
            f"模型只返回 {len(str(result.get('full_outline', '')).strip())} 字"
        )


def validate_volume_blueprints(
    result: dict[str, Any],
    requested_volumes: int,
    target_chapters: int | None = None,
) -> None:
    require_schema(
        "volumes",
        list_fields=("volumes",),
        list_bounds={"volumes": (requested_volumes, requested_volumes)},
    )(result)
    titles: list[str] = []
    goals: list[str] = []
    progression: list[dict[str, str]] = []
    chapter_total = 0
    for index, volume in enumerate(result["volumes"], start=1):
        if not isinstance(volume, dict):
            raise ValueError(f"第 {index} 卷不是有效对象")
        for field in (
            "title",
            "chapter_count",
            "goal",
            "conflict",
            "synopsis",
            "turning_points",
            "character_arcs",
            "subplots",
            "must_keep",
            "must_avoid",
            "ending_state",
            "bridge_to_next",
        ):
            if field not in volume:
                raise ValueError(f"第 {index} 卷缺少 {field}")
        for field in ("title", "goal", "conflict", "synopsis", "ending_state"):
            if not str(volume.get(field, "")).strip():
                raise ValueError(f"第 {index} 卷缺少 {field}")
        if len(str(volume.get("synopsis", "")).strip()) < 100:
            raise ValueError(f"第 {index} 卷剧情梗概过短")
        for field in (
            "turning_points",
            "character_arcs",
            "subplots",
            "must_keep",
            "must_avoid",
        ):
            if not isinstance(volume.get(field), list):
                raise ValueError(f"第 {index} 卷的 {field} 必须是数组")
        try:
            chapter_total += int(volume["chapter_count"])
        except (TypeError, ValueError):
            raise ValueError(f"第 {index} 卷 chapter_count 无效") from None
        titles.append(re.sub(r"[\W_]+", "", str(volume["title"])))
        goals.append(re.sub(r"[\W_]+", "", str(volume["goal"])))
        progression.append(
            {
                key: str(volume.get(key, ""))
                for key in ("goal", "ending_state", "theme_test", "primary_arena", "irreversible_change")
            }
        )
    if len(titles) != len(set(titles)):
        raise ValueError("分卷标题重复")
    generic_titles = {
        "初次选择",
        "阻力反制",
        "理念交锋",
        "代价显现",
        "关系转向",
        "方案受挫",
        "风险升级",
        "决断前夜",
        "核心选择",
        "卷末余波",
    }
    if any(title in generic_titles for title in titles):
        raise ValueError("分卷标题使用了抽象节拍模板，必须改为具体事件或意象")
    if target_chapters is not None and chapter_total != target_chapters:
        raise ValueError(
            f"分卷章数合计应为 {target_chapters}，实际为 {chapter_total}"
        )
    for right in range(len(goals)):
        for left in range(right):
            if (
                min(len(goals[left]), len(goals[right])) >= 15
                and SequenceMatcher(None, goals[left], goals[right]).ratio() >= 0.84
            ):
                raise ValueError(f"第 {left + 1} 卷和第 {right + 1} 卷目标高度重复")
    for right in range(len(progression)):
        for left in range(right):
            for field, label in (
                ("ending_state", "卷末状态"),
                ("theme_test", "主题检验"),
                ("primary_arena", "主要故事场域"),
                ("irreversible_change", "不可逆变化"),
            ):
                a, b = progression[left][field], progression[right][field]
                if min(len(re.sub(r"\s+", "", a)), len(re.sub(r"\s+", "", b))) >= 12 and _planning_similarity(a, b) >= 0.76:
                    raise ValueError(f"第 {left + 1} 卷和第 {right + 1} 卷{label}高度重复")


def validate_director_master_bible(result: dict[str, Any]) -> None:
    require_schema(
        "theme",
        "reader_promise",
        "central_conflict",
        "story_engine",
        "ending_state",
        "main_plot",
        "theme_progression",
        "pacing_plan",
        "stakes_ladder",
        "major_character_arcs",
        "subplots",
        "historical_nodes",
        list_fields=(
            "stakes_ladder",
            "major_character_arcs",
            "subplots",
            "historical_nodes",
        ),
    )(result)
    if len(str(result.get("main_plot", "")).strip()) < 120:
        raise ValueError("故事圣经主线推进过短，至少需要 120 字")
    if len(result.get("stakes_ladder", [])) < 4:
        raise ValueError("故事圣经至少需要 4 级具体风险")
    if len(result.get("major_character_arcs", [])) < 3:
        raise ValueError("故事圣经至少需要 3 条点名人物弧")


def _director_volume_specs(target_chapters: int, volume_count: int) -> list[dict[str, int]]:
    target = max(1, int(target_chapters))
    count = max(1, min(int(volume_count), target))
    base, remainder = divmod(target, count)
    cursor = 1
    specs = []
    for index in range(count):
        chapter_count = base + (1 if index < remainder else 0)
        end = cursor + chapter_count - 1
        specs.append({"number": index + 1, "chapter_start": cursor, "chapter_end": end, "chapter_count": chapter_count})
        cursor = end + 1
    return specs


def _director_stage_forbidden_terms(
    project: dict[str, Any], volume_number: int
) -> list[str]:
    """Read author-declared phase guards from the production specification."""
    production_spec = str(project.get("production_spec", ""))
    match = re.search(
        rf"(?m)^\s*-\s*第{int(volume_number)}卷禁入：([^\r\n]*)$",
        production_spec,
    )
    if not match:
        return []
    return list(
        dict.fromkeys(
            item.strip()
            for item in re.split(r"[、，,；;]", match.group(1))
            if item.strip()
        )
    )


def _director_stage_declared_terms(
    project: dict[str, Any], volume_number: int, label: str
) -> list[str]:
    production_spec = str(project.get("production_spec", ""))
    match = re.search(
        rf"(?m)^\s*-\s*第{int(volume_number)}卷{re.escape(label)}：([^\r\n]*)$",
        production_spec,
    )
    if not match:
        return []
    return list(
        dict.fromkeys(
            item.strip()
            for item in re.split(r"[、，,；;]", match.group(1))
            if item.strip()
        )
    )


def _director_stage_event_terms(
    project: dict[str, Any], volume_number: int
) -> list[str]:
    return _director_stage_declared_terms(project, volume_number, "事件词")


def _director_stage_location_terms(
    project: dict[str, Any], volume_number: int
) -> list[str]:
    return _director_stage_declared_terms(project, volume_number, "主场域词")


def _director_chapter_seed(
    project: dict[str, Any], volume_number: int, chapter_number: int
) -> str:
    production_spec = str(project.get("production_spec", ""))
    match = re.search(
        rf"(?m)^\s*-\s*第{int(volume_number)}卷第{int(chapter_number)}章种子：([^\r\n]*)$",
        production_spec,
    )
    return match.group(1).strip() if match else ""


def _director_chapter_forbidden_terms(
    project: dict[str, Any], volume_number: int, chapter_number: int
) -> list[str]:
    production_spec = str(project.get("production_spec", ""))
    match = re.search(
        rf"(?m)^\s*-\s*第{int(volume_number)}卷第{int(chapter_number)}章禁入：([^\r\n]*)$",
        production_spec,
    )
    if not match:
        return []
    return [
        item.strip()
        for item in re.split(r"[、，,；;]", match.group(1))
        if item.strip()
    ]


def _director_chapter_core_forbidden_terms(
    project: dict[str, Any], volume_number: int, chapter_number: int
) -> list[str]:
    production_spec = str(project.get("production_spec", ""))
    match = re.search(
        rf"(?m)^\s*-\s*第{int(volume_number)}卷第{int(chapter_number)}章核心禁入：([^\r\n]*)$",
        production_spec,
    )
    if not match:
        return []
    return [
        item.strip()
        for item in re.split(r"[、，,；;]", match.group(1))
        if item.strip()
    ]


def validate_director_chapter_forbidden_terms(
    route: dict[str, Any], terms: list[str], *, include_hook: bool = True
) -> None:
    if not terms:
        return
    fields = ["title", "goal", "conflict", "turning_point"]
    if include_hook:
        fields.append("ending_hook")
    combined = " ".join(str(route.get(field, "")) for field in fields)
    hits = [term for term in terms if term in combined]
    if hits:
        raise ValueError("章节使用作者章级禁入词：" + "、".join(hits))


def validate_director_chapter_seed(
    route: dict[str, Any], seed: str
) -> None:
    if not seed.strip():
        return
    common = {
        "秦策", "本章", "章末", "发现", "制度", "问题", "开始", "必须",
        "选择", "地方", "形成", "进行", "通过", "成为", "要求", "导致",
        "不得", "需要", "出现", "完成", "同时", "一处", "一项", "只能",
    }
    anchors = _chinese_bigrams(seed) - common
    combined = " ".join(
        str(route.get(field, ""))
        for field in ("title", "goal", "conflict", "turning_point", "ending_hook")
    )
    overlap = anchors & _chinese_bigrams(combined)
    required = min(8, max(3, (len(anchors) + 4) // 5))
    if len(overlap) < required:
        raise ValueError(
            "章节未承载作者指定章种子，必须围绕这一场景和局部结果重写："
            + bounded_excerpt(seed, 220)
        )


def validate_director_volume_domain(
    project: dict[str, Any], volume_number: int, route: dict[str, Any]
) -> None:
    core = " ".join(
        str(route.get(field, ""))
        for field in ("title", "goal", "conflict", "turning_point")
    )
    event_terms = _director_stage_event_terms(project, volume_number)
    event_hits = [term for term in event_terms if term in core]
    if event_terms and len(event_hits) < 2:
        raise ValueError(
            f"第 {volume_number} 卷路线偏离本卷事件材料：至少使用两个事件词；"
            "可用词为 " + "、".join(event_terms[:20])
        )
    location_terms = _director_stage_location_terms(project, volume_number)
    location_hits = [term for term in location_terms if term in core]
    if len(location_hits) > 1:
        raise ValueError(
            f"第 {volume_number} 卷单章主场域过多："
            + "、".join(location_hits)
            + "；跨地消息只能放在 ending_hook"
        )


def _validate_director_stage_boundary(
    project: dict[str, Any], volume_number: int, text: str
) -> None:
    hits = [
        term
        for term in _director_stage_forbidden_terms(project, volume_number)
        if term in str(text or "")
    ]
    if hits:
        raise ValueError(
            f"第 {volume_number} 卷发生未来阶段串线：提前使用 "
            + "、".join(hits[:8])
        )


def validate_director_volume_contracts(
    result: dict[str, Any], specs: list[dict[str, int]]
) -> None:
    require_schema(
        "volume_contracts",
        list_fields=("volume_contracts",),
        list_bounds={"volume_contracts": (len(specs), len(specs))},
    )(result)
    titles: list[str] = []
    goals: list[str] = []
    for index, (contract, spec) in enumerate(
        zip(result["volume_contracts"], specs), start=1
    ):
        if not isinstance(contract, dict):
            raise ValueError(f"第 {index} 卷契约不是对象")
        for field in (
            "number", "title", "goal", "conflict", "ending_state",
            "bridge_to_next", "theme_test", "primary_arena", "time_span",
            "irreversible_change", "character_choice", "new_story_question",
        ):
            if not str(contract.get(field, "")).strip():
                raise ValueError(f"第 {index} 卷契约缺少 {field}")
        if int(contract["number"]) != spec["number"]:
            raise ValueError(f"第 {index} 卷契约编号不正确")
        titles.append(re.sub(r"[\W_]+", "", str(contract["title"])))
        goals.append(re.sub(r"[\W_]+", "", str(contract["goal"])))
    if len(titles) != len(set(titles)):
        raise ValueError("分卷契约标题重复")
    for right in range(len(goals)):
        for left in range(right):
            if min(len(goals[left]), len(goals[right])) >= 15 and SequenceMatcher(
                None, goals[left], goals[right]
            ).ratio() >= 0.84:
                raise ValueError(f"第 {left + 1} 与第 {right + 1} 卷契约目标高度重复")


def validate_director_volume_contract(
    result: dict[str, Any],
    spec: dict[str, int],
    completed: list[dict[str, Any]],
    final_ending_state: str = "",
    is_final: bool = False,
) -> None:
    require_schema("contract")(result)
    contract = result["contract"]
    if not isinstance(contract, dict):
        raise ValueError("分卷契约 contract 不是对象")
    for field in (
        "number", "title", "goal", "conflict", "ending_state",
        "bridge_to_next", "theme_test", "primary_arena", "time_span",
        "irreversible_change", "character_choice", "new_story_question",
    ):
        if not str(contract.get(field, "")).strip():
            raise ValueError(f"分卷契约缺少 {field}")
    if int(contract["number"]) != spec["number"]:
        raise ValueError(
            f"分卷契约编号应为 {spec['number']}，实际为 {contract['number']}"
        )
    title = re.sub(r"[\W_]+", "", str(contract["title"]))
    goal = re.sub(r"[\W_]+", "", str(contract["goal"]))
    if not is_final and min(len(final_ending_state.strip()), len(str(contract.get("ending_state", "")).strip())) >= 12 and _planning_similarity(contract.get("ending_state", ""), final_ending_state) >= 0.72:
        raise ValueError("非终卷提前兑现了全书结局状态")
    for index, previous in enumerate(completed, start=1):
        previous_title = re.sub(r"[\W_]+", "", str(previous.get("title", "")))
        previous_goal = re.sub(r"[\W_]+", "", str(previous.get("goal", "")))
        if title == previous_title:
            raise ValueError(f"本卷与第 {index} 卷标题重复")
        if min(len(goal), len(previous_goal)) >= 15 and SequenceMatcher(
            None, goal, previous_goal
        ).ratio() >= 0.84:
            raise ValueError(f"本卷与第 {index} 卷目标高度重复")
        for field, label in (
            ("ending_state", "卷末状态"),
            ("theme_test", "主题检验"),
            ("primary_arena", "主要故事场域"),
            ("irreversible_change", "不可逆变化"),
            ("character_choice", "人物选择"),
        ):
            current_value = str(contract.get(field, ""))
            previous_value = str(previous.get(field, ""))
            if min(len(re.sub(r"\s+", "", current_value)), len(re.sub(r"\s+", "", previous_value))) >= 12 and _planning_similarity(current_value, previous_value) >= 0.76:
                raise ValueError(f"本卷与第 {index} 卷{label}高度重复")


def _director_volume_synopsis_minimum(chapter_count: Any = 0) -> int:
    """Use one synopsis contract for both staged volume generation steps."""
    try:
        chapters = max(0, int(chapter_count or 0))
    except (TypeError, ValueError):
        chapters = 0
    return 160 if 0 < chapters <= 6 else 180


def validate_director_volume_expansion(result: dict[str, Any]) -> None:
    require_schema("volume")(result)
    volume = result["volume"]
    if not isinstance(volume, dict):
        raise ValueError("分卷蓝图 volume 不是对象")
    for field in (
        "title", "goal", "conflict", "synopsis", "turning_points",
        "character_arcs", "subplots", "must_keep", "must_avoid",
        "ending_state", "bridge_to_next",
    ):
        if field not in volume:
            raise ValueError(f"分卷蓝图缺少 {field}")
    synopsis_minimum = _director_volume_synopsis_minimum(volume.get("chapter_count"))
    synopsis_length = len(str(volume.get("synopsis", "")).strip())
    if synopsis_length < synopsis_minimum:
        raise ValueError(
            f"分卷详细剧情梗概至少需要 {synopsis_minimum} 字，实际 {synopsis_length} 字"
        )
    for field in (
        "turning_points", "character_arcs", "subplots", "must_keep", "must_avoid"
    ):
        if not isinstance(volume.get(field), list):
            raise ValueError(f"分卷蓝图 {field} 必须是数组")
    if len(volume.get("turning_points", [])) < 3:
        raise ValueError("分卷蓝图至少需要 3 个具体转折")


def validate_director_volume_core(
    result: dict[str, Any], chapter_count: Any = 0
) -> None:
    require_schema("volume_core")(result)
    core = result["volume_core"]
    if not isinstance(core, dict):
        raise ValueError("分卷剧情核心 volume_core 不是对象")
    for field in (
        "title", "goal", "conflict", "synopsis", "ending_state", "bridge_to_next"
    ):
        if not str(core.get(field, "")).strip():
            raise ValueError(f"分卷剧情核心缺少 {field}")
    synopsis_length = len(str(core.get("synopsis", "")).strip())
    synopsis_minimum = _director_volume_synopsis_minimum(chapter_count)
    if synopsis_length < synopsis_minimum:
        raise ValueError(
            f"分卷剧情核心梗概至少需要 {synopsis_minimum} 字，实际 {synopsis_length} 字"
        )


def validate_director_volume_details(result: dict[str, Any]) -> None:
    require_schema("volume_details")(result)
    details = result["volume_details"]
    if not isinstance(details, dict):
        raise ValueError("分卷执行清单 volume_details 不是对象")
    for field in (
        "turning_points", "character_arcs", "subplots", "must_keep", "must_avoid"
    ):
        if not isinstance(details.get(field), list):
            raise ValueError(f"分卷执行清单 {field} 必须是数组")
    if not 3 <= len(details.get("turning_points", [])) <= 5:
        raise ValueError("分卷执行清单必须包含 3-5 个具体转折")
    if len(details.get("character_arcs", [])) < 2:
        raise ValueError("分卷执行清单至少需要 2 条点名人物弧")


def _chinese_bigrams(text: str) -> set[str]:
    runs = re.findall(r"[\u3400-\u9fff]+", text)
    return {
        run[index : index + 2]
        for run in runs
        for index in range(len(run) - 1)
    }


def validate_route_batch(
    result: dict[str, Any],
    expected_numbers: list[int],
    previous_routes: list[dict[str, Any]],
    forbidden_outcomes: list[str] | None = None,
) -> None:
    expected = len(expected_numbers)
    require_schema(
        "chapters",
        list_fields=("chapters",),
        list_bounds={"chapters": (expected, expected)},
    )(result)
    chapters = result["chapters"]
    numbers = []
    for index, route in enumerate(chapters):
        if not isinstance(route, dict):
            raise ValueError(f"本批第 {index + 1} 条路线不是对象")
        for field in (
            "number",
            "title",
            "goal",
            "conflict",
            "turning_point",
            "ending_hook",
            "must_keep",
            "must_avoid",
        ):
            if field not in route:
                raise ValueError(f"本批第 {index + 1} 条路线缺少 {field}")
        if not isinstance(route["must_keep"], list) or not isinstance(
            route["must_avoid"], list
        ):
            raise ValueError("章节必须保留和必须避免必须是数组")
        numbers.append(int(route["number"]))
    if numbers != expected_numbers:
        raise ValueError(f"章节编号应为 {expected_numbers}，实际为 {numbers}")

    combined = previous_routes + chapters
    titles = [re.sub(r"\W+", "", str(item.get("title", ""))) for item in combined]
    if len(titles) != len(set(titles)):
        raise ValueError("章节标题重复，说明拆解没有形成独立事件")
    generic_titles = {
        "初次选择",
        "阻力反制",
        "理念交锋",
        "代价显现",
        "关系转向",
        "方案受挫",
        "风险升级",
        "决断前夜",
        "核心选择",
        "卷末余波",
    }
    if any(title in generic_titles for title in titles):
        raise ValueError("章节标题使用了抽象节拍模板，必须改为具体事件或意象")
    title_bigram_documents: Counter[str] = Counter()
    title_stop_chars = set("第章卷上下中的与和及之")
    for title in titles[-12:]:
        title_bigram_documents.update(
            bigram
            for bigram in _chinese_bigrams(title)
            if not any(char in title_stop_chars for char in bigram)
        )
    tired_title_bigrams = [
        bigram
        for bigram, count in title_bigram_documents.items()
        if count >= 4
    ]
    if tired_title_bigrams:
        raise ValueError(
            "章节标题重复使用同一核心意象："
            + "、".join(tired_title_bigrams[:6])
        )
    for right in range(len(titles)):
        for left in range(right):
            if (
                min(len(titles[left]), len(titles[right])) >= 4
                and SequenceMatcher(
                    None, titles[left], titles[right]
                ).ratio()
                >= 0.88
            ):
                raise ValueError(
                    f"第 {left + 1} 与第 {right + 1} 条章节标题高度近似"
                )
    goals = [
        re.sub(r"[\W_]+", "", str(item.get("goal", "")))
        for item in combined
    ]
    for right in range(len(goals)):
        for left in range(right):
            minimum_length = min(len(goals[left]), len(goals[right]))
            left_terms = _chinese_bigrams(goals[left])
            right_terms = _chinese_bigrams(goals[right])
            containment = (
                len(left_terms & right_terms) / min(len(left_terms), len(right_terms))
                if left_terms and right_terms
                else 0.0
            )
            duplicate_goal = (
                minimum_length >= 18
                and (
                    SequenceMatcher(None, goals[left], goals[right]).ratio() >= 0.86
                    or containment >= 0.44
                )
            ) or (minimum_length >= 14 and containment >= 0.68)
            if duplicate_goal:
                raise ValueError(
                    f"第 {left + 1} 与第 {right + 1} 条章节目标高度重复"
                )
    for field, label, threshold in (
        ("conflict", "核心冲突", 0.82),
        ("turning_point", "关键转折", 0.80),
        ("ending_hook", "章末变化", 0.80),
    ):
        values = [
            re.sub(r"[\W_]+", "", str(item.get(field, "")))
            for item in combined
        ]
        for right in range(len(values)):
            for left in range(right):
                if (
                    min(len(values[left]), len(values[right])) >= 12
                    and SequenceMatcher(
                        None, values[left], values[right]
                    ).ratio()
                    >= threshold
                ):
                    raise ValueError(
                        f"第 {left + 1} 与第 {right + 1} 条章节{label}高度重复"
                    )

    # A local model can rename an event while still resolving the volume's
    # promised outcome too early.  For non-final batches, block explicit
    # realization language when it substantially overlaps the volume goal,
    # ending state or next-volume bridge.
    realization_markers = (
        "获准",
        "获得",
        "失去",
        "确立",
        "完成",
        "废除",
        "否决",
        "平息",
        "获释",
        "正式",
        "成功",
        "达成",
        "实现",
        "批准",
        "成为",
        "掌握",
    )

    outcome_term_groups = [
        terms
        for terms in (
            _chinese_bigrams(str(outcome))
            for outcome in (forbidden_outcomes or [])
        )
        if terms
    ]
    if outcome_term_groups:
        for route_index, route in enumerate(chapters):
            route_result = " ".join(
                str(route.get(field, ""))
                for field in ("goal", "turning_point", "ending_hook")
            )
            route_terms = _chinese_bigrams(route_result)
            coverage = max(
                len(outcome_terms & route_terms) / len(outcome_terms)
                for outcome_terms in outcome_term_groups
            )
            limited_markers = (
                "有限", "临时", "局部", "仅限", "只准", "试查", "试办",
                "尚未", "不得", "仍无权", "未完成",
            )
            realization_threshold = (
                0.48
                if any(marker in route_result for marker in limited_markers)
                else 0.28
            )
            if coverage >= realization_threshold and any(
                marker in route_result for marker in realization_markers
            ):
                raise ValueError(
                    f"第 {expected_numbers[route_index]} 章提前兑现了卷目标或卷末状态"
                )


def validate_audit_result(result: dict[str, Any]) -> None:
    require_schema(
        "score",
        "verdict",
        "issues",
        "strengths",
        list_fields=("issues", "strengths"),
    )(result)
    if "revision_brief" not in result:
        raise ValueError("模型缺少必要字段：revision_brief")


def validate_chapter_memory_result(result: dict[str, Any]) -> None:
    """Accept a compact but useful memory delta from smaller models.

    `summary` and `story_so_far` are the only fields that must always exist.
    All structured delta arrays are optional at parse time and normalized to
    empty arrays afterwards.  This avoids throwing away a valid Qwen3-8B
    memory result merely because it omitted an empty `timeline` or
    `continuity_notes` field.
    """
    require_fields("summary", "story_so_far")(result)
    for field in (
        "character_updates",
        "facts",
        "plot_threads",
        "timeline",
        "relationship_updates",
        "continuity_notes",
        "description_updates",
    ):
        if field in result and not isinstance(result.get(field), list):
            raise ValueError(f"字段 {field} 必须是数组")
    if "scene_settlement" in result and not isinstance(result.get("scene_settlement"), dict):
        raise ValueError("字段 scene_settlement 必须是对象")


def validate_chapter_memory_compact_result(result: dict[str, Any]) -> None:
    """Validate the deliberately tiny recovery protocol.

    The recovery call must not be forced to reproduce every field that made the
    primary response too large. Missing non-essential structures are normalized
    to empty values by the endpoint before the result can be applied.
    """
    require_fields("summary")(result)
    for field in ("facts", "character_updates"):
        if field in result and not isinstance(result.get(field), list):
            raise ValueError(f"字段 {field} 必须是数组")
    if "scene_settlement" in result and not isinstance(
        result.get("scene_settlement"), dict
    ):
        raise ValueError("字段 scene_settlement 必须是对象")


def _incubator_outline_minimum(target_chapters: int | None = None, story_mode: str | None = None) -> int:
    """Return a useful detail floor without making short books impossible for 8B models.

    The old universal 450-character floor was calibrated for 30+ chapter books.  A
    three-to-twelve chapter short/medium project can be structurally complete in a
    much smaller outline, and Qwen3-8B would otherwise spend its retry budget
    padding prose instead of preserving the JSON schema.  Long-form projects still
    keep the stricter floor.
    """
    chapters = max(0, int(target_chapters or 0))
    if story_mode == "short" or (chapters and chapters <= 6):
        return 180
    if chapters and chapters <= 12:
        return 220
    if chapters and chapters <= 24:
        return 320
    return 450


def validate_incubator_core_result(
    result: dict[str, Any],
    target_chapters: int | None = None,
    story_mode: str | None = None,
) -> None:
    """Validate two choice-sized cores before any character/world expansion."""
    require_schema(
        "options",
        list_fields=("options",),
        list_bounds={"options": (2, 2)},
    )(result)
    required = (
        "title", "genre", "positioning", "premise", "reader_promise",
        "central_question", "central_conflict", "story_engine", "outline",
        "author_intent", "current_focus", "book_rules", "ending_direction",
        "tone", "pov", "target_chapters", "opening_hook", "first_arc",
    )
    for index, option in enumerate(result["options"], start=1):
        if not isinstance(option, dict):
            raise ValueError(f"第 {index} 套开书方向不是对象")
        missing = [field for field in required if field not in option]
        if missing:
            raise ValueError(
                f"第 {index} 套开书方向缺少字段：{', '.join(missing)}"
            )
        minimum_outline = _incubator_outline_minimum(target_chapters, story_mode)
        if len(str(option.get("outline", "")).strip()) < minimum_outline:
            raise ValueError(f"第 {index} 套开书方向的全书大纲过短")
        if not isinstance(option.get("book_rules"), list) or len(
            option.get("book_rules", [])
        ) < 6:
            raise ValueError(f"第 {index} 套开书方向的硬规则不足 6 条")


def validate_incubator_assets(result: dict[str, Any]) -> None:
    require_schema(
        "characters",
        "world_entries",
        list_fields=("characters", "world_entries"),
    )(result)
    characters = result["characters"]
    if not 3 <= len(characters) <= 5:
        raise ValueError("候选方案主要人物必须为 3-5 人")
    names: list[str] = []
    for index, character in enumerate(characters, start=1):
        if not isinstance(character, dict):
            raise ValueError(f"候选人物第 {index} 项不是对象")
        for field in (
            "name", "role", "description", "personality", "values",
            "contradictions", "relationships", "hard_limits", "goal",
            "knowledge", "voice",
        ):
            if not str(character.get(field, "")).strip():
                raise ValueError(f"候选人物第 {index} 项缺少 {field}")
        name = str(character.get("name", "")).strip()
        names.append(name)
        if not isinstance(character.get("aliases", []), list):
            raise ValueError(f"候选人物第 {index} 项 aliases 必须是数组")
    if len(set(names)) != len(names):
        raise ValueError("候选方案主要人物存在重名")
    validate_director_world({"world_entries": result["world_entries"]})


def validate_incubator_result(
    result: dict[str, Any],
    target_chapters: int | None = None,
    story_mode: str | None = None,
) -> None:
    require_schema(
        "options",
        list_fields=("options",),
        list_bounds={"options": (2, 2)},
    )(result)
    required = (
        "title",
        "genre",
        "positioning",
        "premise",
        "reader_promise",
        "central_question",
        "central_conflict",
        "story_engine",
        "outline",
        "author_intent",
        "current_focus",
        "book_rules",
        "ending_direction",
        "tone",
        "pov",
        "target_chapters",
        "opening_hook",
        "first_arc",
        "characters",
        "world_entries",
    )
    for index, option in enumerate(result["options"], start=1):
        if not isinstance(option, dict):
            raise ValueError(f"第 {index} 套开书方案不是对象")
        missing = [field for field in required if field not in option]
        if missing:
            raise ValueError(
                f"第 {index} 套开书方案缺少字段：{', '.join(missing)}"
            )
        minimum_outline = _incubator_outline_minimum(target_chapters, story_mode)
        if len(str(option.get("outline", "")).strip()) < minimum_outline:
            raise ValueError(f"第 {index} 套开书方案的全书大纲过短")
        if not isinstance(option["book_rules"], list) or len(
            option["book_rules"]
        ) < 6:
            raise ValueError(f"第 {index} 套开书方案的硬规则不足 6 条")
        if not isinstance(option["characters"], list) or len(
            option["characters"]
        ) < 3:
            raise ValueError(f"第 {index} 套开书方案的主要人物不足 3 个")
        if not isinstance(option["world_entries"], list):
            raise ValueError(f"第 {index} 套开书方案的世界设定不是数组")


def _director_story_spine_minimum(target_chapters: int | None = None, story_mode: str | None = None) -> int:
    chapters = max(0, int(target_chapters or 0))
    if story_mode == "short" or (chapters and chapters <= 5):
        return 120
    if chapters and chapters <= 12:
        return 170
    return 220


def validate_director_seed_brief(
    result: dict[str, Any],
    target_chapters: int | None = None,
    story_mode: str | None = None,
) -> None:
    require_schema(
        "title",
        "genre",
        "positioning",
        "premise",
        "reader_promise",
        "central_question",
        "central_conflict",
        "story_engine",
        "story_spine",
        "author_intent",
        "current_focus",
        "book_rules",
        "ending_direction",
        "tone",
        "pov",
        "opening_hook",
        "first_arc",
        list_fields=("book_rules",),
    )(result)
    if len(str(result.get("story_spine", "")).strip()) < _director_story_spine_minimum(
        target_chapters, story_mode
    ):
        raise ValueError("自动导演故事骨架过短，至少需要覆盖开篇、发展、高潮和结局")
    if len(result.get("book_rules", [])) < 5:
        raise ValueError("自动导演硬规则不足 5 条")


def validate_director_cast(result: dict[str, Any]) -> None:
    require_schema("characters", list_fields=("characters",))(result)
    characters = result["characters"]
    if not 3 <= len(characters) <= 5:
        raise ValueError("自动导演主要人物名单必须为 3-5 人")
    names: list[str] = []
    for index, character in enumerate(characters, start=1):
        if not isinstance(character, dict):
            raise ValueError(f"人物名单第 {index} 项不是对象")
        for field in ("name", "role", "narrative_function", "relationship_seed", "core_conflict"):
            if not str(character.get(field, "")).strip():
                raise ValueError(f"人物名单第 {index} 项缺少 {field}")
        name = str(character["name"]).strip()
        if len(name) > 16 or re.search(r"[（）()/、]", name) or any(
            marker in name for marker in ("或代表", "待定", "某位")
        ):
            raise ValueError(f"人物名单第 {index} 项仍是备选或占位名：{name}")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError("自动导演人物名单存在重名")


def validate_director_character_card(result: dict[str, Any]) -> None:
    require_schema(
        "name",
        "role",
        "description",
        "personality",
        "values",
        "contradictions",
        "relationships",
        "arc",
        "hard_limits",
        "goal",
        "knowledge",
        "voice",
    )(result)
    if len(str(result.get("personality", "")).strip()) < 20:
        raise ValueError("人物卡的可观察性格过短")
    if not isinstance(result.get("aliases", []), list):
        raise ValueError("人物卡 aliases 必须是数组")
    if not isinstance(result.get("dialogue_examples", []), list):
        raise ValueError("人物卡 dialogue_examples 必须是数组")


def validate_director_world(result: dict[str, Any]) -> None:
    require_schema("world_entries", list_fields=("world_entries",))(result)
    entries = result["world_entries"]
    if not 3 <= len(entries) <= 5:
        raise ValueError("自动导演世界书必须包含 3-5 个必要条目")
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"世界书第 {index} 项不是对象")
        for field in ("title", "category", "content"):
            if not str(entry.get(field, "")).strip():
                raise ValueError(f"世界书第 {index} 项缺少 {field}")
        if not isinstance(entry.get("keys"), list) or not entry["keys"]:
            raise ValueError(f"世界书第 {index} 项缺少触发词")


_PRE_UNIFICATION_MARKERS = (
    "战国", "秦王政", "秦王嬴政", "统一六国前", "秦统一前", "尚未统一",
)
_POST_UNIFICATION_TERMS = (
    "始皇帝", "皇帝", "陛下", "龙袍", "玉玺",
)


def historical_asset_conflicts(payload: Any, authority_text: str) -> list[str]:
    """Detect a small set of high-confidence timeline leaks in authority assets.

    This is intentionally narrow. It does not pretend to replace historical
    research; it prevents a generated character card or lore entry from turning
    obvious post-unification titles and regalia into high-priority canon when the
    accepted seed explicitly places the story before unification.
    """
    authority = str(authority_text or "")
    if not any(marker in authority for marker in _PRE_UNIFICATION_MARKERS):
        return []
    rendered = json.dumps(payload, ensure_ascii=False)
    return [term for term in _POST_UNIFICATION_TERMS if term in rendered]


def validate_asset_authority(payload: Any, authority_text: str) -> None:
    conflicts = historical_asset_conflicts(payload, authority_text)
    if conflicts:
        raise ValueError(
            "前置资产违反秦统一前时间边界，删除或改写这些后世称谓/物件："
            + "、".join(conflicts)
        )


def find_chapter(project: dict[str, Any], chapter_id: str) -> tuple[int, dict[str, Any]]:
    chapters = project.get("chapters", [])
    for index, chapter in enumerate(chapters):
        if chapter.get("id") == chapter_id:
            return index, chapter
    raise HTTPException(404, "章节不存在")


def _persist_managed_project(
    project: dict[str, Any], reason: str
) -> tuple[dict[str, Any], bool]:
    """Persist when the project belongs to this store; keep unsaved API previews usable."""
    project_id = str(project.get("id", "")).strip()
    if not project_id or store.get(project_id) is None:
        return ensure_project_defaults(project), False
    director_task = store.latest_director_task(project_id)
    if director_task and director_task.get("status") in {"queued", "running"}:
        raise HTTPException(
            409, "自动导演正在写入该作品，请先暂停任务再接受或结算章节"
        )
    return store.save(project_id, project, reason=reason), True


def _latest_managed_commit_project(
    project: dict[str, Any], commit_id: str
) -> dict[str, Any]:
    """Resolve an in-flight memory commit from the latest server-side project.

    Extraction can take minutes. Applying an older browser snapshot would overwrite
    edits made to other chapters during that interval. The target chapter content
    hash still guards against changing the chapter being settled.
    """
    project_id = str(project.get("id", "")).strip()
    if not project_id or not commit_id:
        return project
    latest = store.get(project_id)
    if latest is None:
        return project
    if get_memory_commit(latest, commit_id) is None:
        raise HTTPException(
            409, "服务器中的记忆提交已变化，请刷新作品后重新接受章节"
        )
    return latest


def planning_exception_detail(exc: Exception) -> str:
    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "本地模型响应超时"
    if isinstance(exc, httpx.ConnectError):
        return "无法连接模型服务，请检查 API 地址、网络连接以及模型服务是否可用"
    if isinstance(exc, httpx.HTTPStatusError):
        detail = ""
        try:
            payload = exc.response.json()
            detail = str(payload.get("error") or payload.get("detail") or "")
        except Exception:
            try:
                detail = exc.response.text.strip()[:300]
            except Exception:
                detail = ""
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


def bounded_excerpt(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    third = max(1, (limit - 80) // 3)
    middle_start = max(0, len(text) // 2 - third // 2)
    return (
        text[:third]
        + "\n[…内容过长，保留中段代表片段…]\n"
        + text[middle_start : middle_start + third]
        + "\n[…内容过长，保留结尾…]\n"
        + text[-third:]
    )


def local_quality_known_context(project: dict[str, Any], chapter: dict[str, Any]) -> str:
    """Render project authority for deterministic local quality checks.

    The local checker should not accuse the model of inventing a past event when
    that event is already present in the story bible, a character card, a world
    entry, an accepted memory fact, or the current chapter plan.  Keep this
    independent from the LLM context budget: it is local text matching only.
    """
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    payload = {
        "premise": project.get("premise", ""),
        "outline": project.get("outline", ""),
        "author_intent": project.get("author_intent", ""),
        "book_rules": project.get("book_rules", ""),
        "narrative": project.get("narrative", {}),
        "characters": project.get("characters", []),
        "world_entries": project.get("world_entries", []),
        "accepted_facts": memory.get("facts", []),
        "timeline": memory.get("timeline", []),
        "relationships": memory.get("relationships", []),
        "story_so_far": memory.get("story_so_far", ""),
        "chapter_plan": chapter.get("plan", {}),
        "chapter_scene_goal": chapter.get("scene_goal", ""),
        "chapter_author_note": chapter.get("author_note", ""),
    }
    return bounded_excerpt(json.dumps(payload, ensure_ascii=False), 60_000)


def quality_source_tail(chapter_content: Any, draft: Any, limit: int = 3000) -> str:
    """Return prior prose only when it is not the candidate's saved revision base.

    Browser and QA flows can legitimately save a generated draft before asking
    for a local check. Comparing that saved chapter to the identical candidate
    reports every sentence as copied from itself and corrupts the quality score.
    The same problem occurs after a light copy-edit, so suppress a near-identical
    *full-length* revision as well.  A short excerpt copied from a longer saved
    chapter is deliberately retained so genuine reuse detection still runs.
    """
    source = str(chapter_content or "").strip()
    candidate = str(draft or "").strip()
    if not source or source == candidate:
        return ""

    source_compact = re.sub(r"\s+", "", source)
    candidate_compact = re.sub(r"\s+", "", candidate)
    longer = max(len(source_compact), len(candidate_compact))
    shorter = min(len(source_compact), len(candidate_compact))
    if (
        shorter >= 120
        and longer > 0
        and shorter / longer >= 0.80
        and SequenceMatcher(
            None, source_compact, candidate_compact, autojunk=False
        ).ratio()
        >= 0.86
    ):
        return ""
    return source[-limit:]


def _indexed_retrieval_hits(
    project: dict[str, Any], query: str, current_index: int, limit: int
) -> list[dict[str, Any]]:
    project_id = str(project.get("id", ""))
    if not project_id or not store.get(project_id):
        return []
    return store.search_project(
        project_id,
        query,
        current_chapter_number=max(1, current_index + 1),
        limit=max(limit, limit * 2),
    )


def _attach_indexed_retrieval(
    project: dict[str, Any], request: dict[str, Any]
) -> dict[str, Any]:
    chapters = project.get("chapters", [])
    chapter_id = str(request.get("chapter_id", ""))
    current_index = next(
        (
            index
            for index, chapter in enumerate(chapters)
            if str(chapter.get("id", "")) == chapter_id
        ),
        max(0, len(chapters) - 1),
    )
    query = build_retrieval_query(project, request)
    limit = int(project.get("settings", {}).get("memory_items", 12) or 12)
    request["_indexed_memory_hits"] = _indexed_retrieval_hits(
        project, query, current_index, limit
    )
    request["_user_writing_skills"] = store.user_writing_skills()
    return request


def _persist_context_snapshot(
    project: dict[str, Any],
    request: dict[str, Any],
    build: Any,
    reason: str,
) -> dict[str, Any] | None:
    project_id = str(project.get("id", ""))
    if not project_id or not store.get(project_id):
        return None
    safe_request = {
        key: request.get(key)
        for key in (
            "chapter_id", "mode", "instruction", "selection", "target_words", "skill_ids"
        )
    }
    settings = project.get("settings", {})
    diagnostics = {
        "estimated_tokens": build.estimated_tokens,
        "sections": [
            {
                key: item.get(key)
                for key in (
                    "name", "priority", "tokens_before", "tokens_after",
                    "status", "selected", "reason", "role", "required",
                )
            }
            for item in build.sections
        ],
        "activated_lore": [
            {
                "id": item.get("id", ""),
                "title": item.get("title", ""),
                "reason": item.get("_activation_reason", ""),
                "matched_keys": item.get("_matched_keys", []),
            }
            for item in build.activated_lore
        ],
        "activated_skills": [
            {
                "id": item.get("id", ""),
                "name": item.get("name", ""),
                "scope": item.get("scope", ""),
                "mode": item.get("mode", ""),
                "activation_reason": item.get("activation_reason", ""),
            }
            for item in build.activated_skills
        ],
        "retrieved_memories": [
            {
                "kind": hit.kind,
                "title": hit.title,
                "content": hit.content,
                "score": round(hit.score, 4),
                "source_id": hit.source_id,
                "origin": hit.origin,
            }
            for hit in build.retrieved_memories
        ],
        "budget_warnings": list(build.budget_warnings),
        "runtime": {
            "provider": settings.get("provider", ""),
            "base_url": settings.get("base_url", ""),
            "model": settings.get("model", ""),
            "temperature": settings.get("temperature"),
            "top_p": settings.get("top_p"),
            "max_tokens": settings.get("max_tokens"),
            "context_budget": settings.get("context_budget"),
        },
    }
    try:
        return store.create_context_snapshot(
            project_id,
            str(request.get("chapter_id", "")),
            safe_request,
            build.messages,
            diagnostics,
            reason=reason,
        )
    except KeyError:
        return None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(
        ROOT / "static" / "index.html",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "app_version": APP_VERSION,
        "api_schema_version": API_SCHEMA_VERSION,
        "database": "ready",
        "search_index": "fts5" if store.fts_enabled else "lexical_fallback",
        "active_director_tasks": sum(
            1 for task in director_runners.values() if not task.done()
        ),
    }


@app.get("/api/projects")
async def project_list() -> list[dict[str, Any]]:
    return store.list()


@app.post("/api/projects")
async def project_create(body: CreateProject) -> dict[str, Any]:
    return store.create(body.title)


@app.post("/api/projects/import")
async def project_import(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict) or not isinstance(body.get("chapters"), list):
        raise HTTPException(422, "导入文件不是有效的砚火项目 JSON")
    return store.import_project(body)


@app.post("/api/backup")
async def database_backup() -> dict[str, Any]:
    path = store.backup(ROOT / "data" / "backups")
    return {"ok": True, "filename": path.name}


@app.post("/api/search")
async def project_search(body: ProjectSearchRequest) -> dict[str, Any]:
    project = store.get(body.project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    if body.current_chapter_number is not None:
        chapter_number = body.current_chapter_number
    elif body.chapter_id:
        chapter_number = next(
            (
                index + 1
                for index, chapter in enumerate(project.get("chapters", []))
                if str(chapter.get("id", "")) == body.chapter_id
            ),
            len(project.get("chapters", [])) or 1,
        )
    else:
        chapter_number = len(project.get("chapters", [])) or 1
    return {
        "engine": "fts5" if store.fts_enabled else "lexical_fallback",
        "chapter_number": chapter_number,
        "hits": store.search_project(
            body.project_id, body.query, chapter_number, body.limit
        ),
    }


@app.get("/api/writing-skills")
async def writing_skills(project_id: str = "") -> dict[str, Any]:
    project: dict[str, Any] = {"writing_skills": []}
    if project_id:
        stored = store.get(project_id)
        if not stored:
            raise HTTPException(404, "项目不存在")
        project = stored
    return {
        "skills": available_writing_skills(project, store.user_writing_skills()),
        "permissions": {
            "allowed": ["prompt_instructions"],
            "denied": ["commands", "tools", "filesystem", "network"],
        },
    }


@app.post("/api/writing-skills/user")
async def writing_skill_user_upsert(body: SkillMutationRequest) -> dict[str, Any]:
    try:
        return {"item": store.upsert_user_writing_skill(body.item)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/writing-skills/user/{skill_id}")
async def writing_skill_user_delete(skill_id: str) -> dict[str, Any]:
    if skill_id.startswith("builtin-"):
        raise HTTPException(409, "内置写作 Skill 为只读")
    if not store.delete_user_writing_skill(skill_id):
        raise HTTPException(404, "个人写作 Skill 不存在")
    return {"ok": True}


@app.post("/api/projects/{project_id}/writing-skills")
async def writing_skill_project_upsert(
    project_id: str, body: SkillMutationRequest
) -> dict[str, Any]:
    project = store.get(project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    task = store.latest_director_task(project_id)
    if task and task.get("status") in {"queued", "running"}:
        raise HTTPException(409, "自动导演运行期间不能修改项目写作 Skill")
    try:
        item = normalize_writing_skill(body.item, scope="project", readonly=False)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    skills = project.setdefault("writing_skills", [])
    for index, existing in enumerate(skills):
        if isinstance(existing, dict) and existing.get("id") == item["id"]:
            skills[index] = item
            break
    else:
        skills.append(item)
    saved = store.save(project_id, project, reason="writing-skill-upsert")
    return {"item": item, "project": saved}


@app.delete("/api/projects/{project_id}/writing-skills/{skill_id}")
async def writing_skill_project_delete(project_id: str, skill_id: str) -> dict[str, Any]:
    if skill_id.startswith("builtin-"):
        raise HTTPException(409, "内置写作 Skill 为只读")
    project = store.get(project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    before = len(project.get("writing_skills", []))
    project["writing_skills"] = [
        item
        for item in project.get("writing_skills", [])
        if not isinstance(item, dict) or str(item.get("id", "")) != skill_id
    ]
    if len(project["writing_skills"]) == before:
        raise HTTPException(404, "项目写作 Skill 不存在")
    saved = store.save(project_id, project, reason="writing-skill-delete")
    return {"ok": True, "project": saved}


@app.get("/api/projects/{project_id}")
async def project_get(project_id: str) -> dict[str, Any]:
    project = store.get(project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    return project


@app.get("/api/projects/{project_id}/context-snapshots")
async def project_context_snapshots(
    project_id: str, limit: int = 30
) -> list[dict[str, Any]]:
    if not store.get(project_id):
        raise HTTPException(404, "项目不存在")
    return store.context_snapshots(project_id, limit)


@app.put("/api/projects/{project_id}")
async def project_save(project_id: str, body: dict[str, Any]) -> dict[str, Any]:
    director_task = store.latest_director_task(project_id)
    if director_task and director_task.get("status") in {"queued", "running"}:
        raise HTTPException(
            409, "自动导演正在写入该作品，请先暂停任务再手动编辑或保存"
        )
    try:
        return store.save(project_id, body, reason=str(body.pop("_save_reason", "autosave")))
    except KeyError:
        raise HTTPException(404, "项目不存在") from None


@app.delete("/api/projects/{project_id}")
async def project_delete(project_id: str) -> dict[str, Any]:
    if not store.get(project_id):
        raise HTTPException(404, "项目不存在")
    backup = store.backup(ROOT / "data" / "backups")
    store.delete(project_id)
    return {"ok": True, "backup": backup.name}


@app.get("/api/projects/{project_id}/revisions")
async def project_revisions(project_id: str) -> list[dict[str, Any]]:
    if not store.get(project_id):
        raise HTTPException(404, "项目不存在")
    return store.revisions(project_id)


@app.post("/api/projects/{project_id}/revisions/{revision_id}/restore")
async def project_restore(project_id: str, revision_id: int) -> dict[str, Any]:
    try:
        return store.restore(project_id, revision_id)
    except KeyError:
        raise HTTPException(404, "历史版本不存在") from None


@app.get("/api/projects/{project_id}/chapters/{chapter_id}/versions")
async def chapter_versions(project_id: str, chapter_id: str) -> list[dict[str, Any]]:
    project = store.get(project_id)
    if not project:
        raise HTTPException(404, "项目不存在")
    if not any(str(item.get("id", "")) == chapter_id for item in project["chapters"]):
        raise HTTPException(404, "章节不存在")
    return store.chapter_versions(project_id, chapter_id)


@app.post(
    "/api/projects/{project_id}/chapters/{chapter_id}/versions/{version_id}/restore"
)
async def chapter_version_restore(
    project_id: str, chapter_id: str, version_id: int
) -> dict[str, Any]:
    try:
        return store.restore_chapter_version(project_id, chapter_id, version_id)
    except KeyError:
        raise HTTPException(404, "章节历史版本不存在") from None


@app.post("/api/models")
async def models(settings: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"models": await list_models(settings)}
    except Exception as exc:
        raise HTTPException(502, f"无法连接模型服务：{exc}") from exc


@app.post("/api/prompt/preview")
async def prompt_preview(body: GenerateRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    target_chars = _resolved_prose_target(project, body)
    prompt_project = deepcopy(project)
    prompt_project["settings"] = _effective_prose_settings(
        project.get("settings", {}), target_chars
    )
    request = body.model_dump()
    request["project"] = prompt_project
    _attach_indexed_retrieval(prompt_project, request)
    build = build_prompt(prompt_project, request)
    return {
        "messages": build.messages,
        "sections": build.sections,
        "activated_lore": build.activated_lore,
        "activated_skills": [
            {
                "id": item.get("id", ""),
                "name": item.get("name", ""),
                "scope": item.get("scope", ""),
                "mode": item.get("mode", ""),
                "activation_reason": item.get("activation_reason", ""),
            }
            for item in build.activated_skills
        ],
        "retrieved_memories": [
            {
                "kind": hit.kind,
                "title": hit.title,
                "content": hit.content,
                "score": round(hit.score, 2),
                "source_id": hit.source_id,
                "origin": hit.origin,
            }
            for hit in build.retrieved_memories
        ],
        "budget_warnings": build.budget_warnings,
        "estimated_tokens": build.estimated_tokens,
    }


@app.post("/api/prompt/snapshot")
async def prompt_snapshot(body: GenerateRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    if not store.get(str(project.get("id", ""))):
        raise HTTPException(404, "只能为已保存的项目创建上下文快照")
    target_chars = _resolved_prose_target(project, body)
    prompt_project = deepcopy(project)
    prompt_project["settings"] = _effective_prose_settings(
        project.get("settings", {}), target_chars
    )
    request = body.model_dump()
    request["project"] = prompt_project
    _attach_indexed_retrieval(prompt_project, request)
    build = build_prompt(prompt_project, request)
    snapshot = _persist_context_snapshot(
        prompt_project, request, build, reason="manual_preview"
    )
    if not snapshot:
        raise HTTPException(404, "项目不存在，无法保存上下文快照")
    return snapshot


@app.get("/api/prompt/snapshots/{snapshot_id}")
async def prompt_snapshot_get(snapshot_id: str) -> dict[str, Any]:
    snapshot = store.get_context_snapshot(snapshot_id)
    if not snapshot:
        raise HTTPException(404, "上下文快照不存在")
    return snapshot


@app.post("/api/prompt/snapshots/{snapshot_id}/replay")
async def prompt_snapshot_replay(
    snapshot_id: str, body: SnapshotReplayRequest
) -> StreamingResponse:
    snapshot = store.get_context_snapshot(snapshot_id)
    if not snapshot:
        raise HTTPException(404, "上下文快照不存在")
    messages = snapshot.get("messages", [])
    if not isinstance(messages, list) or not messages:
        raise HTTPException(409, "该快照没有可回放的模型消息")

    async def events():
        meta = {
            "type": "meta",
            "replay": True,
            "context_snapshot_id": snapshot_id,
            "prompt_hash": snapshot.get("prompt_hash", ""),
            "estimated_tokens": snapshot.get("diagnostics", {}).get(
                "estimated_tokens", 0
            ),
        }
        yield f"data: {json.dumps(meta, ensure_ascii=False)}\n\n"
        try:
            async for piece in chat_stream(body.settings, messages):
                yield f"data: {json.dumps({'type': 'token', 'text': piece}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.post("/api/style/analyze")
async def style_analyze(body: StyleRequest) -> dict[str, Any]:
    analysis_sample = bounded_excerpt(body.sample, 18000)
    messages = [
        {"role": "system", "content": "你是只返回合法 JSON 的文学风格分析器。"},
        {"role": "user", "content": STYLE_ANALYSIS_PROMPT + analysis_sample},
    ]
    try:
        result, warnings = await structured_completion(
            body.settings,
            messages,
            max_tokens=900,
            timeout_seconds=150,
            temperature=0.2,
            validate=require_schema(
                "name", "profile", "dos", "donts", list_fields=("dos", "donts")
            ),
        )
        return {
            "name": str(result.get("name", "样本文风")),
            "profile": str(result.get("profile", "")),
            "dos": [str(x) for x in result.get("dos", [])],
            "donts": [str(x) for x in result.get("donts", [])],
            "fallback": False,
            "warnings": warnings,
        }
    except Exception as exc:
        if recoverable_model_error(exc):
            return local_style_analysis(body.sample, planning_exception_detail(exc))
        raise HTTPException(
            502, f"文风分析失败：{planning_exception_detail(exc)}"
        ) from exc


@app.post("/api/reference/parse")
async def reference_parse(body: ReferenceParseRequest) -> dict[str, Any]:
    try:
        return parse_reference_file(body.name, body.content_base64)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/style/analyze-references")
async def style_analyze_references(body: ProjectRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    corpus = combined_style_corpus(project, max_chars=100_000)
    if len(corpus.strip()) < 100:
        raise HTTPException(400, "请先在参考资料库上传并启用至少一篇‘文风样文’。")
    result = await style_analyze(StyleRequest(settings=project.get("settings", {}), sample=corpus))
    result["source_ids"] = [
        item.get("id")
        for item in project.get("references", [])
        if isinstance(item, dict) and item.get("kind") == "style" and item.get("enabled", True) is not False
    ]
    result["sample_chars"] = len(corpus)
    return result


@app.post("/api/canon/analyze-character")
async def canon_analyze_character(body: CanonCharacterRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    ensure_fanfic_defaults(project)
    character = next(
        (item for item in project.get("characters", []) if isinstance(item, dict) and item.get("id") == body.character_id),
        None,
    )
    if not character:
        raise HTTPException(404, "人物不存在")
    selected_ids = set(body.reference_ids)
    refs = [
        item
        for item in project.get("references", [])
        if isinstance(item, dict)
        and item.get("kind") == "canon"
        and item.get("enabled", True) is not False
        and (not selected_ids or item.get("id") in selected_ids)
    ]
    if not refs:
        raise HTTPException(400, "请先上传至少一份‘原作/正典资料’，再生成人物正典档案。")
    character_name = str(character.get("name", "")).strip()
    direct = [item for item in refs if character_name and character_name in str(item.get("text", ""))]
    if direct:
        refs = direct + [item for item in refs if item not in direct]
    parts: list[str] = []
    total = 0
    for item in refs:
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        remaining = 42_000 - total
        if remaining <= 0:
            break
        excerpt = text[:remaining]
        parts.append(
            f"\n===== 资料：{item.get('name', '未命名')}｜作品={item.get('source_work', '') or '未注明'} =====\n{excerpt}"
        )
        total += len(excerpt)
    prompt = f"角色名：{character_name}\n" + CANON_ANALYSIS_PROMPT + "".join(parts)
    try:
        result, warnings = await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": "你只输出严格合法 JSON；不得使用训练记忆补全用户未提供的原作事实。"},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2200,
            timeout_seconds=240,
            temperature=0.18,
            validate=require_schema(
                "must_preserve", "must_not", "explicit_facts", "inferences",
                list_fields=("must_preserve", "must_not", "explicit_facts", "inferences"),
            ),
        )
    except Exception as exc:
        raise HTTPException(502, f"角色正典档案分析失败：{planning_exception_detail(exc)}") from exc
    profile = {
        key: result.get(key, []) if key in {"must_preserve", "must_not"} else str(result.get(key, ""))
        for key in (
            "source_work", "timeline_node", "identity", "appearance", "core_personality",
            "deep_personality", "values", "goals", "fears", "abilities", "limitations",
            "speech_style", "behavior_patterns", "emotional_patterns", "relationship_patterns",
            "must_preserve", "must_not",
        )
    }
    profile.update(
        {
            "enabled": True,
            "user_verified": False,
            "source_refs": [str(item.get("id")) for item in refs if item.get("id")],
        }
    )
    return {
        "profile": profile,
        "explicit_facts": [str(item) for item in result.get("explicit_facts", [])],
        "inferences": [str(item) for item in result.get("inferences", [])],
        "warnings": warnings,
    }


@app.post("/api/canon/audit")
async def canon_audit(body: CanonAuditRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    canon_context = render_canon_context(project, body.draft)
    copy_report = source_similarity_report(project, body.draft)
    if not canon_context:
        return {
            "score": 100,
            "verdict": "pass",
            "issues": [],
            "strengths": [],
            "copy_risk": copy_report,
            "warnings": ["当前正文没有命中已启用的同人正典角色锁。"],
        }
    try:
        result, warnings = await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": CANON_AUDIT_PROMPT},
                {"role": "user", "content": f"{canon_context}\n\n【候选正文】\n{bounded_excerpt(body.draft, 28000)}"},
            ],
            max_tokens=1700,
            timeout_seconds=220,
            temperature=0.12,
            validate=require_schema("issues", "strengths", list_fields=("issues", "strengths")),
        )
        result["score"] = max(0, min(100, int(result.get("score", 0) or 0)))
        result["verdict"] = str(result.get("verdict") or ("pass" if result["score"] >= 85 else "revise"))
        result["copy_risk"] = copy_report
        result["warnings"] = warnings
        return result
    except Exception as exc:
        raise HTTPException(502, f"同人角色一致性审校失败：{planning_exception_detail(exc)}") from exc


@app.post("/api/knowledge/graph")
async def knowledge_graph(body: ProjectRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    sync_authoritative_entities(project)
    return graph_snapshot(project)


@app.post("/api/knowledge/fact")
async def knowledge_fact(body: KnowledgeMutationRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    try:
        item = upsert_fact(project, body.item)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"project": ensure_project_defaults(project), "item": item}


@app.post("/api/knowledge/relation")
async def knowledge_relation(body: KnowledgeMutationRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    try:
        item = upsert_relation(project, body.item)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"project": ensure_project_defaults(project), "item": item}


@app.post("/api/provider/summary")
async def provider_info(settings: dict[str, Any]) -> dict[str, Any]:
    return provider_summary(settings)


@app.post("/api/chapter/plan")
async def chapter_plan(body: ChapterActionRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    index, chapter = find_chapter(project, body.chapter_id)
    query = "\n".join(
        [
            body.instruction,
            project.get("current_focus", ""),
            chapter.get("scene_goal", ""),
            chapter.get("content", "")[-3000:],
        ]
    )
    memories = retrieve_memories(
        project,
        query,
        index,
        int(project.get("settings", {}).get("memory_items", 12)),
        indexed_hits=_indexed_retrieval_hits(
            project,
            query,
            index,
            int(project.get("settings", {}).get("memory_items", 12)),
        ),
    )
    thread_agenda = render_thread_agenda(
        select_thread_agenda(project, query, index, limit=8)
    )
    previous = project.get("chapters", [])[max(0, index - 4) : index]
    context = f"""【长期作者意图】
{bounded_excerpt(project.get('author_intent', ''), 2500)}
【近期焦点】
{bounded_excerpt(project.get('current_focus', ''), 2500)}
【不可违背规则】
{bounded_excerpt(project.get('book_rules', ''), 5000)}
【人物权威状态】
{bounded_excerpt(json.dumps(project.get('characters', []), ensure_ascii=False), 8000)}
【人物与读者知情边界】
{bounded_excerpt(render_epistemic_context(project, index, query), 7000)}
【分层规划】
{bounded_excerpt(render_planning_context(project, index), 8000)}
【最近章节】
{chr(10).join(f"- {item.get('title','')}：{item.get('summary','')}" for item in previous)}
【相关记忆】
{bounded_excerpt(render_memories(memories), 5000)}
【伏笔与暗线治理议程】
{bounded_excerpt(thread_agenda, 6000) or '当前没有必须处理的开放线索。'}
【动态关系状态】
{bounded_excerpt(json.dumps(project.get('memory', {}).get('relationships', []), ensure_ascii=False), 4000)}
【待确认连续性备注】
{json.dumps([
    item for item in project.get('memory', {}).get('continuity_notes', [])
    if isinstance(item, dict) and not item.get('resolved', False)
], ensure_ascii=False)}
【当前章节】
标题：{chapter.get('title','')}
场景目标：{chapter.get('scene_goal','')}
已有正文结尾：{chapter.get('content','')[-3000:]}
【路线层级说明】
当前章路线卡已经人工核对。goal、conflict、must_keep、must_avoid、turning_point、ending_hook
只能原样服从，不得缩小、改写或用你生成的 exit_state 覆盖；你只补充视角、开篇动作和场景表现。
【作者本次要求】
{body.instruction or '依据近期焦点规划下一步，不抢写后续剧情。'}"""
    try:
        result, warnings = await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": CHAPTER_PLAN_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.25,
            max_tokens=900,
            timeout_seconds=180,
            validate=require_schema(
                "goal",
                "conflict",
                "must_keep",
                "must_avoid",
                "turning_point",
                "ending_hook",
                list_fields=("must_keep", "must_avoid"),
            ),
        )
        route = chapter.get("route", {}) if isinstance(chapter.get("route"), dict) else {}

        def route_value(key: str, fallback: Any) -> Any:
            value = route.get(key)
            return value if value not in (None, "", []) else fallback

        if route:
            scene_beats = [
                f"进入：{route_value('goal', chapter.get('scene_goal', ''))}",
                f"阻力：{route_value('conflict', result.get('conflict', ''))}",
                f"转折：{route_value('turning_point', result.get('turning_point', ''))}",
                f"退出：{route_value('ending_hook', result.get('ending_hook', ''))}",
            ]
            thread_actions: list[str] = []
        else:
            scene_beats = [
                "→".join(str(part) for part in item if str(part).strip())
                if isinstance(item, list)
                else str(item)
                for item in result.get("scene_beats", [])
                if str(item).strip()
            ][:8] if isinstance(result.get("scene_beats"), list) else []
            thread_actions = [
                "→".join(str(part) for part in item if str(part).strip())
                if isinstance(item, list)
                else str(item)
                for item in result.get("thread_actions", [])
                if str(item).strip()
            ][:5] if isinstance(result.get("thread_actions"), list) else []
        return {
            "goal": str(route_value("goal", result.get("goal", ""))),
            "conflict": str(route_value("conflict", result.get("conflict", ""))),
            "must_keep": [str(item) for item in route_value("must_keep", result.get("must_keep", []))][:8],
            "must_avoid": [str(item) for item in route_value("must_avoid", result.get("must_avoid", []))][:8],
            "turning_point": str(route_value("turning_point", result.get("turning_point", ""))),
            "ending_hook": str(route_value("ending_hook", result.get("ending_hook", ""))),
            "chapter_type": str(result.get("chapter_type", "")),
            "pov_character": str(result.get("pov_character", "")),
            "time_location": str(result.get("time_location", "")),
            "opening_beat": str(result.get("opening_beat", "")),
            "scene_beats": scene_beats,
            "emotional_turn": str(result.get("emotional_turn", "")),
            "thread_actions": thread_actions,
            "exit_state": str(route_value("ending_hook", result.get("exit_state", ""))),
            "ending_type": str(result.get("ending_type", "")),
            "fallback": False,
            "warnings": warnings,
        }
    except Exception as exc:
        if recoverable_model_error(exc):
            return fallback_chapter_plan(
                project, index, chapter, planning_exception_detail(exc)
            )
        raise HTTPException(
            502, f"章节规划失败：{planning_exception_detail(exc)}"
        ) from exc


@app.post("/api/planning/master")
async def planning_master(body: PlanningRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    narrative = project.get("narrative", {})
    target = int(narrative.get("target_chapters", 30))
    requested_volumes = (
        1
        if target <= 6
        else min(12, max(2, math.ceil(target / 12)))
    )
    outline_target = (
        700
        if target <= 12
        else 1200
        if target <= 30
        else 1800
        if target <= 60
        else 2600
        if target <= 120
        else 3200
    )
    minimum_outline = max(500, int(outline_target * 0.7))
    characters = [
        {
            key: str(item.get(key, ""))[:240]
            for key in ("name", "role", "description", "goal")
        }
        for item in project.get("characters", [])[:12]
    ]
    world = [
        {
            "title": str(item.get("title", ""))[:80],
            "content": str(item.get("content", ""))[:300],
        }
        for item in project.get("world_entries", [])[:12]
    ]
    outline = str(project.get("outline", ""))
    if len(outline) > 4800:
        outline = outline[:3200] + "\n[…保留总纲结尾…]\n" + outline[-1600:]
    context = f"""【作品信息】
书名：{project.get('title', '')}
题材：{project.get('genre', '')}
形态：{project.get('story_mode', 'long')}
计划总章数：{target}
必须生成分卷数：{requested_volumes}
AI详细全书大纲目标：{outline_target}-{int(outline_target * 1.35)}字
核心构想：{project.get('premise', '')}
现有总纲：{outline}
【作者控制】
长期意图：{project.get('author_intent', '')}
不可违背规则：{project.get('book_rules', '')}
核心问题：{narrative.get('central_question', '')}
结局方向：{narrative.get('ending_direction', '')}
总体气质：{narrative.get('tone', '')}
【人物】
{json.dumps(characters, ensure_ascii=False)}
【世界设定】
{json.dumps(world, ensure_ascii=False)}
【已发生事实】
{project.get('memory', {}).get('story_so_far', '')}
【作者对本次规划的补充】
{body.instruction or '在不改变已有构想的前提下，补全可持续的全书结构。'}"""
    try:
        core_messages = [
            {"role": "system", "content": MASTER_CORE_PROMPT},
            {"role": "user", "content": context},
        ]
        safe_context = min(
            23000,
            int(project.get("settings", {}).get("context_budget", 24000)),
        )
        core_prompt_estimate = estimate_tokens(
            "\n".join(message["content"] for message in core_messages)
        )
        core_available = max(2200, safe_context - core_prompt_estimate - 900)
        core_desired = min(5200, 1100 + int(outline_target * 1.25))
        core, core_warnings = await structured_completion(
            project.get("settings", {}),
            core_messages,
            temperature=0.38,
            max_tokens=min(core_desired, core_available),
            timeout_seconds=420,
            validate=lambda result: validate_master_core(
                result, minimum_outline
            ),
            token_ceiling=core_available,
        )

        core_for_volumes = {
            key: core.get(key)
            for key in (
                "theme",
                "reader_promise",
                "central_conflict",
                "story_engine",
                "ending_state",
                "full_outline",
                "main_plot",
                "theme_progression",
                "pacing_plan",
                "stakes_ladder",
                "major_character_arcs",
                "subplots",
                "historical_nodes",
            )
        }
        volume_context = f"""【作品】
书名：{project.get('title', '')}
题材：{project.get('genre', '')}
计划总章数：{target}
必须生成分卷数：{requested_volumes}
【全书故事圣经】
{json.dumps(core_for_volumes, ensure_ascii=False)}
【作者硬规则】
{project.get('book_rules', '')}
【本次补充】
{body.instruction or '按照总纲建立连续、互不重复的分卷因果链。'}"""
        volume_messages = [
            {"role": "system", "content": MASTER_VOLUMES_PROMPT},
            {"role": "user", "content": volume_context},
        ]
        volume_prompt_estimate = estimate_tokens(
            "\n".join(message["content"] for message in volume_messages)
        )
        volume_available = max(
            2400, safe_context - volume_prompt_estimate - 900
        )
        volume_desired = min(6200, 900 + requested_volumes * 620)
        volume_result, volume_warnings = await structured_completion(
            project.get("settings", {}),
            volume_messages,
            temperature=0.34,
            max_tokens=min(volume_desired, volume_available),
            timeout_seconds=420,
            validate=lambda result: validate_volume_blueprints(
                result, requested_volumes, target
            ),
            token_ceiling=volume_available,
        )
        result = {**core, "volumes": volume_result["volumes"]}
        validate_master_result(
            result, requested_volumes, minimum_outline, target
        )
        normalized = normalize_master_plan(result, target)
        normalized["fallback"] = False
        normalized["warnings"] = core_warnings + volume_warnings
        return normalized
    except Exception as exc:
        if recoverable_model_error(exc):
            return fallback_master_plan(
                project, target, planning_exception_detail(exc)
            )
        raise HTTPException(
            502, f"全书规划失败：{planning_exception_detail(exc)}"
        ) from exc


@app.post("/api/planning/volume")
async def planning_volume(body: PlanningRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    try:
        _, volume = find_volume(project, body.volume_id)
    except KeyError:
        raise HTTPException(404, "分卷不存在") from None
    start, end = int(volume["chapter_start"]), int(volume["chapter_end"])
    written = [
        {
            "number": index + 1,
            "title": chapter.get("title", ""),
            "summary": chapter.get("summary", ""),
            "has_content": bool(chapter.get("content", "").strip()),
        }
        for index, chapter in enumerate(project.get("chapters", []))
        if start <= index + 1 <= end
        and (chapter.get("summary") or chapter.get("content"))
    ]
    master = project.get("planning", {}).get("master", {})
    base_context = f"""【全书详细总纲】
{str(master.get('full_outline') or project.get('outline', ''))[:7000]}
【全书主线、人物弧与主题递进】
主线：{master.get('main_plot', '')}
主题递进：{master.get('theme_progression', '')}
人物弧：{json.dumps(master.get('major_character_arcs', []), ensure_ascii=False)}
支线：{json.dumps(master.get('subplots', []), ensure_ascii=False)}
【本卷蓝图】
卷名：{volume.get('title', '')}（第{start}-{end}章）
详细梗概：{volume.get('synopsis', '')}
阶段目标：{volume.get('goal', '')}
主导冲突：{volume.get('conflict', '')}
关键转折：{json.dumps(volume.get('turning_points', []), ensure_ascii=False)}
人物弧：{json.dumps(volume.get('character_arcs', []), ensure_ascii=False)}
支线：{json.dumps(volume.get('subplots', []), ensure_ascii=False)}
卷末状态：{volume.get('ending_state', '')}
承接下一卷：{volume.get('bridge_to_next', '')}
本卷必须保持：{json.dumps(volume.get('must_keep', []), ensure_ascii=False)}
本卷必须避免：{json.dumps(volume.get('must_avoid', []), ensure_ascii=False)}
【已有正文状态】
{json.dumps(written, ensure_ascii=False)}
【全书滚动进展与未结线索】
{project.get('memory', {}).get('story_so_far', '')}
{bounded_excerpt(json.dumps(project.get('memory', {}).get('plot_threads', []), ensure_ascii=False), 5000)}
【作者补充】
{body.instruction or '让每章承担不同的具体事件功能，连续造成可验证的新状态。'}"""
    routes: list[dict[str, Any]] = []
    retry_warnings: list[str] = []
    # Three chapters per call is a better reliability/latency balance for
    # 8–12B local models: each event card stays detailed, while the final
    # batch is small enough to land the promised volume ending only once.
    batch_size = 3
    total_batches = math.ceil((end - start + 1) / batch_size)
    for batch_start in range(start, end + 1, batch_size):
        batch_end = min(end, batch_start + batch_size - 1)
        batch_number = (batch_start - start) // batch_size + 1
        is_final_batch = batch_number == total_batches
        phase = (
            "起势：建立本卷独有问题、行动入口和第一次受阻"
            if batch_number == 1 and not is_final_batch
            else "升级：让既有行动产生反制、代价和方向变化"
            if not is_final_batch
            else "收束：完成本卷最终选择与代价，并触发下一卷"
        )
        turning_points = list(volume.get("turning_points", []))
        assigned_turns = [
            item
            for turn_index, item in enumerate(turning_points)
            if min(
                total_batches - 1,
                turn_index * total_batches // max(1, len(turning_points)),
            )
            == batch_number - 1
        ]
        expected_numbers = list(range(batch_start, batch_end + 1))
        if total_batches == 1:
            slot_roles = [
                "建立具体局面与行动入口",
                "让阻力升级并迫使人物改变做法",
                "完成关键选择、支付代价并形成卷末状态",
            ]
        elif batch_number == 1:
            slot_roles = [
                "建立本卷独有问题与主角当下处境",
                "进行第一次具体调查或尝试并取得新证据",
                "遭遇第一次反制，作出不可轻易撤回的选择",
            ]
        elif is_final_batch:
            slot_roles = [
                "把此前证据、关系和代价汇入最终两难",
                "执行最终选择并完成核心对抗",
                "只在最后一章兑现卷末状态，同时制造下一卷的具体问题",
            ]
        elif batch_number == total_batches - 1:
            slot_roles = [
                "让支线或人物关系与主线正面碰撞",
                "制造一次改变力量对比的重大挫败或有限胜利",
                "用新的代价逼出终局前无法回避的两难",
            ]
        else:
            slot_roles = [
                "从不同渠道推进调查、试点或联盟，不重复上一批的方法",
                "让有明确利益的对手采取具体反制行动",
                "让计划付出可见代价，并据此修正下一阶段方向",
            ]
        slot_contract = "\n".join(
            f"- 第 {number} 章：{slot_roles[index]}"
            for index, number in enumerate(expected_numbers)
        )
        previous = routes[-3:]
        used_routes = [
            {
                "number": item.get("number"),
                "title": item.get("title"),
                "goal": str(item.get("goal", ""))[:100],
            }
            for item in routes
        ]
        batch_context = f"""{base_context}
【本批次任务】
只生成第 {batch_start}-{batch_end} 章，共 {len(expected_numbers)} 章。
紧邻本批次、需要直接承接的路线：
{json.dumps(previous, ensure_ascii=False)}
本卷此前全部已用章名与章末变化（新结果不得重复或近似复述）：
{json.dumps(used_routes, ensure_ascii=False)}
【本批次在整卷中的职责】
第 {batch_number}/{total_batches} 批，阶段：{phase}
本批应承载的转折：{json.dumps(assigned_turns, ensure_ascii=False)}
本批每章不可互换的剧情岗位：
{slot_contract}
{"这是最终批次，必须到最后一章才兑现卷目标和卷末状态。" if is_final_batch else f"这不是最终批次。严禁提前兑现卷目标“{volume.get('goal', '')}”、卷末状态“{volume.get('ending_state', '')}”或下一卷桥梁；本批结尾只能形成通向下一阶段的新压力、证据、资格或代价。"}
第{batch_start}章必须直接承接上述最后状态；第{batch_end}章要为下一批或卷末留下具体推动力。
每章必须写不同的人物行动、具体阻力、关键选择和章末结果。"""
        batch_result: dict[str, Any] | None = None
        last_batch_error: Exception | None = None
        for batch_attempt in range(2):
            repair_instruction = ""
            if batch_attempt and last_batch_error is not None:
                repair_instruction = f"""
【上一轮仍未通过质量门】
问题：{planning_exception_detail(last_batch_error)}
这是一次全新重写，不是改几个词。四章必须使用四个互不相似、且未在本卷出现过的具体事件标题；
每章的行动、阻力、选择和章末变化也必须彼此不同。不要复用上一轮的标题或句式。"""
            try:
                result, batch_warnings = await structured_completion(
                    project.get("settings", {}),
                    [
                        {"role": "system", "content": VOLUME_PLAN_PROMPT},
                        {
                            "role": "user",
                            "content": batch_context + repair_instruction,
                        },
                    ],
                    temperature=0.42 if batch_attempt == 0 else 0.52,
                    max_tokens=2100,
                    timeout_seconds=240,
                    validate=lambda value,
                    numbers=expected_numbers,
                    prior=list(routes),
                    forbidden=(
                        []
                        if is_final_batch
                        else [
                            str(volume.get("goal", "")),
                            str(volume.get("ending_state", "")),
                            str(volume.get("bridge_to_next", "")),
                        ]
                    ): (
                        validate_route_batch(value, numbers, prior, forbidden)
                    ),
                )
                retry_warnings.extend(batch_warnings)
                if batch_attempt:
                    retry_warnings.append(
                        f"第 {batch_start}-{batch_end} 章首次重写仍未通过质量门，"
                        "系统已按错误原因再次生成并恢复。"
                    )
                batch_result = result
                break
            except Exception as exc:
                last_batch_error = exc
                if not recoverable_model_error(exc):
                    raise HTTPException(
                        502, f"分卷拆解失败：{planning_exception_detail(exc)}"
                    ) from exc
        if batch_result is None:
            detail = planning_exception_detail(
                last_batch_error or ValueError("未知质量检查错误")
            )
            return {
                "volume_id": volume["id"],
                "chapters": routes,
                "warnings": [
                    f"第 {batch_start}-{batch_end} 章经过自动重写后仍未通过完整性或重复度检查："
                    f"{detail}。旧路线未被替换，请重试本卷。"
                ],
                "complete": False,
                "fallback": True,
            }
        for offset, item in enumerate(batch_result["chapters"]):
            routes.append(normalize_route(item, batch_start + offset))
    return {
        "volume_id": volume["id"],
        "chapters": routes,
        "warnings": retry_warnings,
        "complete": len(routes) == end - start + 1,
        "fallback": False,
    }


@app.post("/api/planning/apply-volume")
async def planning_apply_volume(body: ApplyVolumeRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    try:
        return ensure_project_defaults(apply_volume_routes(project, body.volume_id))
    except KeyError:
        raise HTTPException(404, "分卷不存在") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/chapter/accept")
async def chapter_accept(body: AcceptChapterRequest) -> dict[str, Any]:
    """Atomically persist accepted prose together with a pending settlement record."""
    project = ensure_project_defaults(body.project)
    _, chapter = find_chapter(project, body.chapter_id)
    if len(str(chapter.get("content", "")).strip()) < 100:
        raise HTTPException(400, "接受的章节正文至少需要 100 字")
    commit, reused = begin_memory_commit(project, chapter)
    project, persisted = _persist_managed_project(
        project, "accepted-pending-memory"
    )
    persisted_commit = get_memory_commit(project, str(commit.get("id", ""))) or commit
    # A manual editorial acceptance resolves the prose-quality debt that caused
    # a paused director checkpoint.  Without resetting these consecutive
    # counters, the next new chapter can trip the systemic breaker using stale
    # failures from chapters the editor has already replaced and accepted.
    director_task = store.latest_director_task(str(project.get("id", "")))
    if (
        not reused
        and director_task
        and director_task.get("status") not in {"queued", "running"}
    ):
        director_task["consecutive_quality_debts"] = 0
        director_task["consecutive_systemic_debts"] = 0
        director_task["quality_directives"] = []
        director_task["checkpoint_message"] = ""
        rejected = director_task.get("last_rejected_candidate", {})
        if isinstance(rejected, dict) and str(rejected.get("chapter_id", "")) == str(
            chapter.get("id", "")
        ):
            director_task["last_rejected_candidate"] = {}
        _director_event(
            director_task,
            f"第 {_chapter_number(project, chapter)} 章已由编辑人工接纳，连续质量债计数已清零",
            "success",
        )
        store.save_director_task(director_task["id"], director_task)
    return {
        "project": project,
        "commit": persisted_commit,
        "reused": reused,
        "persisted": persisted,
    }


@app.post("/api/chapter/memory")
async def chapter_memory(body: ChapterActionRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    if body.commit_id:
        _, incoming_chapter = find_chapter(project, body.chapter_id)
        try:
            validate_memory_commit(project, incoming_chapter, body.commit_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    project = _latest_managed_commit_project(project, body.commit_id)
    chapter_index, chapter = find_chapter(project, body.chapter_id)
    if body.commit_id:
        try:
            validate_memory_commit(project, chapter, body.commit_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    def extracted(result: dict[str, Any]) -> dict[str, Any]:
        if body.commit_id:
            current = get_memory_commit(project, body.commit_id)
            if current and current.get("status") != "committed":
                mark_memory_commit(
                    project,
                    chapter,
                    body.commit_id,
                    "settlement_extracted",
                    warnings=list(result.get("warnings", [])),
                )
                _persist_managed_project(project, "accepted-memory-extracted")
        return result

    content = body.draft.strip() or chapter.get("content", "").strip()
    if len(content) < 100:
        raise HTTPException(400, "章节正文至少需要 100 字")
    memory = project.get("memory", {})
    active_threads = [
        item for item in memory.get("plot_threads", [])
        if isinstance(item, dict)
        and normalize_thread_status(item.get("status")) != "closed"
    ]
    context = f"""【提取协议】
当前是第 {chapter_index + 1} 章。下面“本章正文”是唯一事件证据；计划、旧状态和线索表只用于识别增量，绝不能当成本章已经发生。
【已有全书滚动进展】
{project.get('memory', {}).get('story_so_far', '')}
【章前人物权威状态】
{bounded_excerpt(json.dumps(project.get('characters', []), ensure_ascii=False), 9000)}
【章前权威事实（更新事实时保留来源，替换时填写 supersedes_id）】
{bounded_excerpt(json.dumps(memory.get('facts', [])[-80:], ensure_ascii=False), 7000)}
【章前开放线索（更新时必须复用 id，不得换名复制）】
{bounded_excerpt(json.dumps(active_threads[-50:], ensure_ascii=False), 9000)}
【章前动态关系】
{bounded_excerpt(json.dumps(memory.get('relationships', [])[-50:], ensure_ascii=False), 5000)}
【当前章节】
章节：{chapter.get('title', '')}
已有角色：{', '.join(item.get('name','') for item in project.get('characters', []))}
本章计划：{json.dumps(chapter.get('plan', {}), ensure_ascii=False)}
本章正文：
{bounded_excerpt(content, 18000)}"""
    try:
        result, warnings = await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": CHAPTER_MEMORY_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.15,
            max_tokens=1900,
            timeout_seconds=210,
            validate=validate_chapter_memory_result,
            token_ceiling=2400,
        )
        result["summary"] = str(result.get("summary", ""))
        result["story_so_far"] = str(result.get("story_so_far", ""))[:4000]
        for key in (
            "character_updates",
            "facts",
            "plot_threads",
            "timeline",
            "relationship_updates",
            "continuity_notes",
            "description_updates",
        ):
            if not isinstance(result.get(key), list):
                result[key] = []
        if not isinstance(result.get("scene_settlement"), dict):
            result["scene_settlement"] = {}
        result["fallback"] = False
        result["memory_mode"] = "full"
        result["warnings"] = warnings
        return extracted(result)
    except Exception as exc:
        if recoverable_model_error(exc):
            primary_reason = planning_exception_detail(exc)
            try:
                compact, compact_warnings = await structured_completion(
                    project.get("settings", {}),
                    [
                        {"role": "system", "content": CHAPTER_MEMORY_COMPACT_PROMPT},
                        {"role": "user", "content": context},
                    ],
                    temperature=0.1,
                    max_tokens=850,
                    timeout_seconds=180,
                    validate=validate_chapter_memory_compact_result,
                    token_ceiling=1050,
                )
                compact["summary"] = str(compact.get("summary", ""))
                if compact.get("story_so_far"):
                    compact["story_so_far"] = str(compact["story_so_far"])[:4000]
                else:
                    old_story = str(memory.get("story_so_far", "")).strip()
                    addition = f"{chapter.get('title', '本章')}：{compact['summary']}"
                    compact["story_so_far"] = "\n".join(
                        item for item in (old_story, addition) if item
                    )[-4000:]
                for key in (
                    "character_updates", "facts", "plot_threads", "timeline",
                    "relationship_updates", "continuity_notes", "description_updates",
                ):
                    if not isinstance(compact.get(key), list):
                        compact[key] = []
                if not isinstance(compact.get("scene_settlement"), dict):
                    compact["scene_settlement"] = {}
                compact["fallback"] = False
                compact["memory_mode"] = "compact_recovery"
                compact["warnings"] = [
                    f"完整记忆结构未通过，已用精简AI记忆恢复：{primary_reason}"
                ] + compact_warnings
                return extracted(compact)
            except Exception as compact_exc:
                return extracted(
                    local_memory_result(
                        project,
                        chapter,
                        content,
                        f"完整结构：{primary_reason}；精简恢复：{planning_exception_detail(compact_exc)}",
                    )
                )
        raise HTTPException(
            502, f"记忆提取失败：{planning_exception_detail(exc)}"
        ) from exc


_VOLUME_REGRESSION_STOP_TERMS = {
    "本章", "章节", "人物", "事件", "冲突", "目标", "结尾", "推进", "进入",
    "发现", "决定", "要求", "拒绝", "确认", "证明", "证据", "结果", "最终",
    "开始", "仍然", "已经", "没有", "不能", "不得", "必须", "可以", "只得",
    "一处", "一份", "一页", "一夜", "对方", "自己", "此事", "此后", "同时",
    "留下", "掌握", "看见", "面对", "通过", "为了", "成为", "明白", "知道",
    "选择", "代价", "责任", "权力", "秩序", "问题", "阶段", "路线", "场景",
    "只能", "任何", "只余", "调令", "成一", "一同", "一项", "一件", "一个",
    "以及", "并且", "如果", "为何", "如何", "是否", "其中", "对应", "相关",
    "同一", "不同", "昨日", "今日", "此时", "当日", "次日", "三日", "十七",
    "一旦", "却不", "已经", "仍有", "仍在", "其中", "得到", "不到", "到粮",
    "自己的", "粮与", "人与", "户与", "的一", "中的", "上的", "下的", "后的",
    # Cross-volume editorial prose uses these ordinary verbs and institutions
    # heavily.  They are not story-engine identifiers: requiring a court scene
    # to avoid "核验" or "认为" merely because earlier route cards used them
    # produces false regressions and encourages unnatural synonym swapping.
    "核验", "认为", "交出", "国家", "扣住", "秦策在", "却没有",
    "回咸阳", "车全部", "名册",
}


def _route_card_text(route: dict[str, Any]) -> str:
    values: list[str] = []
    for key in (
        "title", "goal", "conflict", "turning_point", "ending_hook",
        "must_keep", "must_avoid",
    ):
        value = route.get(key, "")
        if isinstance(value, list):
            values.extend(str(item) for item in value)
        else:
            values.append(str(value))
    return "\n".join(item for item in values if item.strip())


def _route_ngrams(text: str) -> set[str]:
    terms: set[str] = set()
    # Keep route-card fields separate. Joining them before slicing can invent
    # meaningless boundary ngrams such as the last character of a goal plus the
    # first character of a conflict.
    for segment in text.splitlines():
        compact = "".join(re.findall(r"[\u3400-\u9fff]", segment))
        terms.update(
            compact[index : index + size]
            for size in (2, 3, 4)
            for index in range(max(0, len(compact) - size + 1))
        )
    return terms


def _volume_route_documents(
    project: dict[str, Any], volume: dict[str, Any]
) -> list[str]:
    documents = [
        _route_card_text(item)
        for item in volume.get("chapters", [])
        if isinstance(item, dict)
    ]
    if documents:
        return [item for item in documents if item]
    volume_id = str(volume.get("id", ""))
    start = int(volume.get("chapter_start", 0) or 0)
    end = int(volume.get("chapter_end", 0) or 0)
    for item in project.get("chapters", []):
        if not isinstance(item, dict):
            continue
        number = int(item.get("number", 0) or 0)
        belongs = (
            bool(volume_id) and str(item.get("volume_id", "")) == volume_id
        ) or (start > 0 and start <= number <= end)
        if belongs and isinstance(item.get("route"), dict):
            text = _route_card_text(item["route"])
            if text:
                documents.append(text)
    return documents


def _prior_volume_regression_issues(
    project: dict[str, Any], chapter: dict[str, Any], draft: str
) -> list[dict[str, Any]]:
    """Flag a cluster of unplanned concepts that belongs only to earlier volumes."""
    volumes = [
        item
        for item in project.get("planning", {}).get("volumes", [])
        if isinstance(item, dict)
    ]
    if len(volumes) < 2:
        return []
    chapter_number = int(chapter.get("number", 0) or 0)
    chapter_volume_id = str(chapter.get("volume_id", ""))
    current_index = next(
        (
            index
            for index, volume in enumerate(volumes)
            if (
                chapter_volume_id
                and str(volume.get("id", "")) == chapter_volume_id
            )
            or (
                int(volume.get("chapter_start", 0) or 0)
                <= chapter_number
                <= int(volume.get("chapter_end", 0) or 0)
            )
        ),
        -1,
    )
    if current_index <= 0:
        return []

    current_terms: set[str] = set()
    for document in _volume_route_documents(project, volumes[current_index]):
        current_terms.update(_route_ngrams(document))
    # A volume boundary is expected to carry forward the immediately preceding
    # chapter's consequences.  Treat that prose as an explicit bridge contract;
    # the guard is for a dormant older engine resurfacing, not normal continuity.
    project_chapters = project.get("chapters", [])
    current_project_index = next(
        (
            index
            for index, item in enumerate(project_chapters)
            if isinstance(item, dict)
            and str(item.get("id", "")) == str(chapter.get("id", ""))
        ),
        -1,
    )
    if current_project_index > 0:
        previous_content = str(
            project_chapters[current_project_index - 1].get("content", "")
        )
        current_terms.update(_route_ngrams(previous_content))
    current_volume = volumes[current_index]
    current_volume_id = str(current_volume.get("id", ""))
    current_start = int(current_volume.get("chapter_start", 0) or 0)
    current_end = int(current_volume.get("chapter_end", 0) or 0)
    for index, item in enumerate(project_chapters):
        if index >= current_project_index >= 0 or not isinstance(item, dict):
            continue
        number = int(item.get("number", 0) or 0)
        # Some persisted projects omit chapter.number; array position remains the
        # canonical chapter number in that legacy shape.
        if number <= 0:
            number = index + 1
        belongs = (
            bool(current_volume_id)
            and str(item.get("volume_id", "")) == current_volume_id
        ) or (current_start > 0 and current_start <= number <= current_end)
        content = str(item.get("content", ""))
        if belongs and content:
            current_terms.update(_route_ngrams(content))
    character_names = {
        str(item.get("name", "")).strip()
        for item in project.get("characters", [])
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    }
    prior_counts: Counter[str] = Counter()
    prior_anchor_terms: set[str] = set()
    for volume in volumes[:current_index]:
        for document in _volume_route_documents(project, volume):
            prior_counts.update(_route_ngrams(document))
        for route in volume.get("chapters", []):
            if not isinstance(route, dict):
                continue
            anchor_values: list[str] = [str(route.get("title", ""))]
            for field in ("must_keep", "must_avoid"):
                value = route.get(field, [])
                if isinstance(value, list):
                    anchor_values.extend(str(item) for item in value)
            for value in anchor_values:
                for anchor in re.findall(r"[\u3400-\u9fff]{2,12}", value):
                    prior_anchor_terms.add(anchor)

    compact_draft = "".join(re.findall(r"[\u3400-\u9fff]", draft))
    candidates = [
        (term, count)
        for term, count in prior_counts.items()
        if count >= 2
        # Only explicit route-card anchors can identify a dormant story engine.
        # Arbitrary 2-4 character windows from prose goals mostly produce common
        # grammar ("共同", "却没有", "交给") and repeatedly reject valid new
        # volumes.  Titles and must_keep/must_avoid items are author-curated and
        # remain specific enough to support a high-severity regression gate.
        and term in prior_anchor_terms
        and term not in current_terms
        and term not in _VOLUME_REGRESSION_STOP_TERMS
        and term not in character_names
        and not any(char in "一二三四五六七八九十百千万两" for char in term)
        and term in compact_draft
    ]
    # Prefer longer, more specific terms and require distinct locations in the
    # candidate draft; overlapping ngrams from one phrase count only once.
    occupied: list[tuple[int, int]] = []
    selected: list[tuple[str, int]] = []
    for term, count in sorted(candidates, key=lambda item: (-len(item[0]), -item[1], item[0])):
        start = compact_draft.find(term)
        if start < 0:
            continue
        span = (start, start + len(term))
        if any(span[0] < used[1] and used[0] < span[1] for used in occupied):
            continue
        occupied.append(span)
        selected.append((term, count))
        if len(selected) >= 5:
            break
    # Three scattered matches are insufficient: at least two concepts must have
    # been repeated across five prior route cards.  This rejects common prose
    # vocabulary while still catching a real replay of a prior volume's engine.
    if len(selected) < 3 or sum(count >= 5 for _term, count in selected) < 2:
        return []
    return [
        {
            "severity": "high",
            "category": "前卷语义回流",
            "message": "候选稿集中重现本卷路线未安排的前卷专属语义。",
            "evidence": [term for term, _count in selected],
            "suggestion": "删除前卷事件残片，按当前卷路线重新组织场景、行动与因果。",
        }
    ]


@app.post("/api/chapter/audit")
async def chapter_audit(body: ChapterActionRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    index, chapter = find_chapter(project, body.chapter_id)
    draft = body.draft.strip()
    if len(draft) < 80:
        raise HTTPException(400, "候选草稿至少需要 80 字")
    query = draft[-4000:] + "\n" + body.instruction
    memories = retrieve_memories(
        project,
        query,
        index,
        int(project.get("settings", {}).get("memory_items", 12)) + 6,
        indexed_hits=_indexed_retrieval_hits(
            project,
            query,
            index,
            int(project.get("settings", {}).get("memory_items", 12)) + 6,
        ),
    )
    thread_agenda = render_thread_agenda(
        select_thread_agenda(project, query, index, limit=10)
    )
    context = f"""【不可违背规则】
{bounded_excerpt(project.get('book_rules', ''), 5000)}
【人物状态】
{bounded_excerpt(json.dumps(project.get('characters', []), ensure_ascii=False), 8000)}
【人物与读者知情边界】
{bounded_excerpt(render_epistemic_context(project, index, query), 7000)}
【本章人工核定路线（高于模型生成的计划）】
{json.dumps(chapter.get('route', {}), ensure_ascii=False)}
【条件禁令解释】
“不得在结尾前/章末前执行某动作”只禁止正文主体提前执行；如果 ending_hook 明确要求该动作，
则动作在最后一个场景发生是正确交付，不得据此判错，也不得建议删除。ending_hook 中的每个结果都必须明写。
【本章执行计划（仅作补充，不得覆盖人工路线）】
{json.dumps(chapter.get('plan', {}), ensure_ascii=False)}
【相关历史事实】
{bounded_excerpt(render_memories(memories), 6000)}
【伏笔与暗线治理议程】
{bounded_excerpt(thread_agenda, 7000)}
【动态关系状态】
{bounded_excerpt(json.dumps(project.get('memory', {}).get('relationships', []), ensure_ascii=False), 5000)}
【待确认连续性备注】
{json.dumps([
    item for item in project.get('memory', {}).get('continuity_notes', [])
    if isinstance(item, dict) and not item.get('resolved', False)
], ensure_ascii=False)}
【近期已经用过的显著描写】
{bounded_excerpt(json.dumps(project.get('memory', {}).get('description_ledger', [])[-40:], ensure_ascii=False), 5000)}
【当前章节已有正文结尾】
{chapter.get('content', '')[-5000:]}
【候选草稿】
{bounded_excerpt(draft, 18000)}"""
    local_checks = local_quality_check(
        draft,
        quality_source_tail(chapter.get("content", ""), draft),
        int(project.get("settings", {}).get("target_words", 1200)),
        project.get("narrative", {}).get("pov", "auto"),
        (
            chapter.get("content", "")[-5000:]
            if len(chapter.get("content", "")) >= 300
            else project.get("style", {}).get("sample", "")[:5000]
        ),
        prior_manuscript_text(project, chapter["id"]),
        str(project.get("genre", "")),
        local_quality_known_context(project, chapter),
    )
    regression_issues = _prior_volume_regression_issues(project, chapter, draft)
    if regression_issues:
        local_checks.setdefault("issues", []).extend(regression_issues)
        local_checks["score"] = min(int(local_checks.get("score", 100)), 74)
        local_checks["verdict"] = "revise"
    try:
        result, warnings = await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": AUDIT_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.1,
            max_tokens=1100,
            timeout_seconds=180,
            validate=validate_audit_result,
        )
        ai_score = max(0, min(100, int(result.get("score", 0))))
        ai_verdict = (
            "pass" if str(result.get("verdict", "")).lower() == "pass" else "revise"
        )
        local_score = max(0, min(100, int(local_checks.get("score", 0))))
        local_verdict = str(local_checks.get("verdict", "revise"))
        ai_issues = (
            result.get("issues", [])
            if isinstance(result.get("issues"), list)
            else []
        )
        # Long-term recall entries are not active obligations in every chapter.
        # If no open-thread agenda activates one, an AI request to plant/pay off
        # a future clue is invalid and must not lower the chapter's gate score.
        removed_weight = 0
        if not thread_agenda.strip():
            kept_issues = []
            for item in ai_issues:
                category = str(item.get("category", "")) if isinstance(item, dict) else ""
                if "伏笔" in category or "回收" in category:
                    severity = str(item.get("severity", "medium"))
                    removed_weight += (
                        15 if severity == "high" else 8 if severity == "medium" else 3
                    )
                    continue
                kept_issues.append(item)
            ai_issues = kept_issues
        if removed_weight:
            ai_score = min(100, ai_score + removed_weight)
        if not ai_issues and ai_score >= 85:
            # A clean audit and a sub-threshold score contradict each other.
            # Normalize that narrow provider quirk so an issue-free chapter is
            # not rejected solely because one model habitually returns 85.
            ai_score = max(ai_score, 90)
            ai_verdict = "pass"
        elif ai_verdict == "pass" and ai_score >= 85 and not any(
            isinstance(item, dict) and str(item.get("severity", "")).lower() == "high"
            for item in ai_issues
        ):
            ai_score = max(ai_score, 90)
        elif ai_score >= 95 and not any(
            isinstance(item, dict) and str(item.get("severity", "")).lower() == "high"
            for item in ai_issues
        ):
            # Some providers return a perfect score while also attaching optional
            # medium/low editorial suggestions, then label the verdict "revise".
            # Suggestions remain visible, but cannot contradict a 95+ score and
            # block an otherwise clean local gate unless a high-risk issue exists.
            ai_verdict = "pass"
        elif (
            ai_score >= 85
            and local_score >= 95
            and local_verdict == "pass"
            and isinstance(chapter.get("route"), dict)
            and bool(chapter.get("route"))
            and not any(
                isinstance(item, dict)
                and str(item.get("severity", "")).lower() == "high"
                for item in ai_issues
            )
        ):
            # Curated production routes deliberately end some chapters before a
            # choice or payoff.  Small audit models often label that exact hook a
            # medium "unfinished" issue while still assigning 85.  When the
            # deterministic gate is exceptionally clean and no high-risk issue is
            # present, keep the suggestions visible but do not let that provider
            # convention contradict the authoritative route and block production.
            ai_score = max(ai_score, 90)
            ai_verdict = "pass"
        result["ai_score"] = ai_score
        result["score"] = min(ai_score, local_score)
        result["verdict"] = (
            "pass"
            if ai_verdict == "pass" and local_verdict == "pass"
            else "revise"
        )
        result["issues"] = ai_issues
        result["local_checks"] = local_checks
        result["fallback"] = False
        result["warnings"] = warnings
        return result
    except Exception as exc:
        if recoverable_model_error(exc):
            return local_audit_result(local_checks, planning_exception_detail(exc))
        raise HTTPException(
            502, f"连续性审计失败：{planning_exception_detail(exc)}"
        ) from exc


@app.post("/api/chapter/quality")
async def chapter_quality(body: ChapterActionRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    _, chapter = find_chapter(project, body.chapter_id)
    return local_quality_check(
        body.draft,
        quality_source_tail(chapter.get("content", ""), body.draft),
        int(project.get("settings", {}).get("target_words", 1200)),
        project.get("narrative", {}).get("pov", "auto"),
        (
            chapter.get("content", "")[-5000:]
            if len(chapter.get("content", "")) >= 300
            else project.get("style", {}).get("sample", "")[:5000]
        ),
        prior_manuscript_text(project, chapter["id"]),
        str(project.get("genre", "")),
        local_quality_known_context(project, chapter),
    )


@app.post("/api/project/manuscript-health")
async def project_manuscript_health(body: ManuscriptHealthRequest) -> dict[str, Any]:
    """Deterministic whole-book checks; no model call and safe for unsaved projects."""
    return manuscript_health_report(ensure_project_defaults(body.project))


@app.post("/api/chapter/memory/apply")
async def chapter_memory_apply(body: ApplyMemoryRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    if body.commit_id:
        _, incoming_target = find_chapter(project, body.chapter_id)
        try:
            validate_memory_commit(project, incoming_target, body.commit_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    project = _latest_managed_commit_project(project, body.commit_id)
    _, target = find_chapter(project, body.chapter_id)
    if body.commit_id:
        try:
            commit = validate_memory_commit(
                project, target, body.commit_id
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if commit.get("status") == "committed":
            project, persisted = _persist_managed_project(
                project, "memory-idempotent-replay"
            )
            return {
                "project": project,
                "warnings": list(commit.get("warnings", [])),
                "commit": commit,
                "persisted": persisted,
                "idempotent": True,
            }

    working = ensure_project_defaults(deepcopy(project))
    _, working_target = find_chapter(working, body.chapter_id)
    warnings = _apply_director_memory(working, working_target, body.result)
    commit = None
    if body.commit_id:
        try:
            commit = mark_memory_commit(
                working,
                working_target,
                body.commit_id,
                "committed",
                warnings=warnings,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    working, persisted = _persist_managed_project(
        working, "accepted-with-memory"
    )
    if commit:
        commit = get_memory_commit(working, body.commit_id) or commit
    return {
        "project": ensure_project_defaults(working),
        "warnings": warnings,
        "commit": commit,
        "persisted": persisted,
        "idempotent": False,
    }


@app.post("/api/chapter/memory/degrade")
async def chapter_memory_degrade(
    body: MemoryCommitStatusRequest,
) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    _, incoming_target = find_chapter(project, body.chapter_id)
    try:
        validate_memory_commit(project, incoming_target, body.commit_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    project = _latest_managed_commit_project(project, body.commit_id)
    _, target = find_chapter(project, body.chapter_id)
    try:
        current = validate_memory_commit(project, target, body.commit_id)
        if current.get("status") != "committed":
            mark_memory_commit(
                project,
                target,
                body.commit_id,
                "state_degraded",
                error=body.error or "记忆提取或应用失败",
            )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    project, persisted = _persist_managed_project(
        project, "accepted-state-degraded"
    )
    return {
        "project": project,
        "commit": get_memory_commit(project, body.commit_id),
        "persisted": persisted,
    }


async def director_seed_brief(
    project: dict[str, Any], config: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    target_chapters = max(3, int(config.get("target_chapters", 30) or 30))
    story_mode = "short" if config.get("story_mode") == "short" else "long"
    spine_hint = (
        "这是3-5章短篇：story_spine可压缩到约140-300字，但必须明确写出开篇、发展、"
        "关键转折/高潮和结局四个因果节点；多个节点可以落在同一章。"
        if story_mode == "short" and target_chapters <= 5
        else "story_spine必须覆盖开篇、发展、关键转折、高潮和结局，不能只写开篇阶段。"
    )
    context = f"""【作者核心灵感】
{bounded_excerpt(config.get('seed', ''), 8000)}
【必须保留与禁区】
{bounded_excerpt(config.get('preferences', ''), 6000) or '没有额外补充。'}
【作品形态】
{'短篇/中短篇' if config.get('story_mode') == 'short' else '长篇/连载'}；计划约 {config.get('target_chapters', 30)} 章
【故事骨架长度提示】
{spine_hint}

这是自动导演的第一检查点。这里只建立一套因果清楚、可继续扩写的故事骨架；详细分卷、逐章路线、人物卡和世界书会由后续独立步骤完成。"""
    return await structured_completion(
        project.get("settings", {}),
        [
            {"role": "system", "content": DIRECTOR_SEED_BRIEF_PROMPT},
            {"role": "user", "content": context},
        ],
        temperature=0.58,
        max_tokens=2100,
        timeout_seconds=300,
        validate=lambda payload: validate_director_seed_brief(
            payload,
            target_chapters=target_chapters,
            story_mode=story_mode,
        ),
        token_ceiling=2400,
    )


def _director_brief_context(
    brief: dict[str, Any], config: dict[str, Any]
) -> str:
    return f"""【已确认故事骨架】
书名：{brief.get('title', '')}
题材：{brief.get('genre', '')}
核心构想：{brief.get('premise', '')}
贯穿冲突：{brief.get('central_conflict', '')}
故事发动机：{brief.get('story_engine', '')}
故事主脊：{brief.get('story_spine', '')}
开篇钩子：{brief.get('opening_hook', '')}
前期故事弧：{brief.get('first_arc', '')}
硬规则：{json.dumps(brief.get('book_rules', []), ensure_ascii=False)}
【作者禁区】
{bounded_excerpt(config.get('preferences', ''), 3000) or '没有额外补充。'}"""


async def director_seed_cast(
    project: dict[str, Any], config: dict[str, Any], brief: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    context = _director_brief_context(brief, config)
    return await structured_completion(
        project.get("settings", {}),
        [
            {"role": "system", "content": DIRECTOR_CAST_PROMPT},
            {"role": "user", "content": context},
        ],
        temperature=0.55,
        max_tokens=900,
        timeout_seconds=240,
        validate=validate_director_cast,
        token_ceiling=1100,
    )


async def director_character_card(
    project: dict[str, Any],
    config: dict[str, Any],
    brief: dict[str, Any],
    cast: list[dict[str, Any]],
    target: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    compact_cast = [
        {
            key: item.get(key, "")
            for key in ("name", "role", "narrative_function", "relationship_seed", "core_conflict")
        }
        for item in cast
    ]
    context = f"""{_director_brief_context(brief, config)}
【全部主要人物名单】
{json.dumps(compact_cast, ensure_ascii=False)}
【本次只写此人】
{json.dumps(target, ensure_ascii=False)}

姓名和身份必须服从指定项。本次只返回这一张人物卡。"""
    def validate_card(payload: dict[str, Any]) -> None:
        validate_director_character_card(payload)
        validate_asset_authority(payload, context)

    result, warnings = await structured_completion(
        project.get("settings", {}),
        [
            {"role": "system", "content": DIRECTOR_CHARACTER_CARD_PROMPT},
            {"role": "user", "content": context},
        ],
        temperature=0.58,
        max_tokens=1250,
        timeout_seconds=240,
        validate=validate_card,
        token_ceiling=1450,
    )
    # 姓名与功能由已验证的名单锁定，避免模型在扩写卡片时悄悄改名。
    result["name"] = str(target.get("name", result.get("name", ""))).strip()
    result["role"] = str(target.get("role", result.get("role", ""))).strip()
    result.setdefault("aliases", [])
    result.setdefault("dialogue_examples", [])
    return result, warnings


async def director_seed_world(
    project: dict[str, Any],
    config: dict[str, Any],
    brief: dict[str, Any],
    cast: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    compact_cast = [
        {"name": item.get("name", ""), "role": item.get("role", "")}
        for item in cast
    ]
    context = f"""{_director_brief_context(brief, config)}
【主要人物】
{json.dumps(compact_cast, ensure_ascii=False)}

本次只返回开篇必须保持一致的世界书条目。"""
    def validate_world(payload: dict[str, Any]) -> None:
        validate_director_world(payload)
        validate_asset_authority(payload, context)

    return await structured_completion(
        project.get("settings", {}),
        [
            {"role": "system", "content": DIRECTOR_WORLD_PROMPT},
            {"role": "user", "content": context},
        ],
        temperature=0.52,
        max_tokens=1400,
        timeout_seconds=240,
        validate=validate_world,
        token_ceiling=1650,
    )


def _director_master_context(project: dict[str, Any], instruction: str = "") -> str:
    narrative = project.get("narrative", {})
    characters = [
        {
            key: str(item.get(key, ""))[:220]
            for key in ("name", "role", "description", "personality", "values", "goal")
        }
        for item in project.get("characters", [])[:8]
    ]
    world = [
        {"title": str(item.get("title", ""))[:80], "content": str(item.get("content", ""))[:240]}
        for item in project.get("world_entries", [])[:8]
    ]
    return f"""【作品】
书名：{project.get('title', '')}
题材：{project.get('genre', '')}
计划章节数：{narrative.get('target_chapters', 30)}
核心构想：{project.get('premise', '')}
现有故事主脊：{bounded_excerpt(project.get('outline', ''), 4200)}
【作者控制】
长期意图：{project.get('author_intent', '')}
硬规则：{project.get('book_rules', '')}
作者指定生产规格（若含编号分卷，顺序、卷名和阶段事件均高于模型自行发挥）：
{bounded_excerpt(project.get('production_spec', ''), 12000)}
核心问题：{narrative.get('central_question', '')}
结局方向：{narrative.get('ending_direction', '')}
总体气质：{narrative.get('tone', '')}
【主要人物】
{json.dumps(characters, ensure_ascii=False)}
【必要世界设定】
{json.dumps(world, ensure_ascii=False)}
【本次补充】
{instruction or '保持已有构想，建立可供后续分卷和拆章执行的因果结构。'}"""


async def director_master_bible(
    project: dict[str, Any], instruction: str = ""
) -> tuple[dict[str, Any], list[str]]:
    return await structured_completion(
        project.get("settings", {}),
        [
            {"role": "system", "content": DIRECTOR_MASTER_BIBLE_PROMPT},
            {"role": "user", "content": _director_master_context(project, instruction)},
        ],
        temperature=0.38,
        max_tokens=1800,
        timeout_seconds=300,
        validate=validate_director_master_bible,
        token_ceiling=2100,
    )


async def director_master_contract(
    project: dict[str, Any],
    bible: dict[str, Any],
    specs: list[dict[str, int]],
    index: int,
    completed: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    spec = specs[index]
    production_spec = str(project.get("production_spec", ""))
    mandates = {
        int(number): {"title": title.strip(), "text": text.strip()}
        for number, title, text in re.findall(
            r"(?m)^\s*(\d{1,2})\.\s*《([^》]+)》([^\r\n]*)$",
            production_spec,
        )
    }
    mandate = mandates.get(index + 1, {})
    context = f"""【全书故事圣经】
{json.dumps(bible, ensure_ascii=False)}
【已有故事主脊】
{bounded_excerpt(project.get('outline', ''), 2600)}
【全书固定分卷范围】
{json.dumps(specs, ensure_ascii=False)}
【本次只建立此卷契约】
{json.dumps(spec, ensure_ascii=False)}
【已经保存的前卷契约】
{json.dumps(completed, ensure_ascii=False)}
【作者硬规则】
{project.get('book_rules', '')}
【作者指定的本卷强制蓝图】
{json.dumps(mandate, ensure_ascii=False) if mandate else '未单独指定；服从故事圣经与前卷因果'}
【结构位置】
这是第 {index + 1}/{len(specs)} 卷；承接前卷但不得重复，非末卷不得提前完成终局。
若强制蓝图非空，title 必须与其中 title 完全一致，阶段事件、不可逆变化和新问题不得挪到其他卷。"""
    messages = [
        {"role": "system", "content": DIRECTOR_VOLUME_CONTRACT_PROMPT},
        {"role": "user", "content": context},
    ]
    validator = lambda value: validate_director_volume_contract(
        value,
        spec,
        completed,
        str(bible.get("ending_state", "")),
        index == len(specs) - 1,
    )
    def validate_with_mandate(value: dict[str, Any]) -> None:
        validator(value)
        if mandate and str(value.get("contract", {}).get("title", "")).strip() != mandate["title"]:
            raise ValueError(
                f"第 {index + 1} 卷必须使用作者指定卷名《{mandate['title']}》"
            )
    try:
        result, warnings = await structured_completion(
            project.get("settings", {}),
            messages,
            temperature=0.44,
            max_tokens=1200,
            timeout_seconds=240,
            validate=validate_with_mandate,
            token_ceiling=1450,
        )
    except ValueError as first_error:
        # Local 8-12B models often understand the story but repeat the same
        # ending_state after a generic JSON repair. Give the contract two more
        # targeted attempts before treating this as a true structural blocker.
        repair_messages = [dict(message) for message in messages]
        repair_messages.append(
            {
                "role": "user",
                "content": (
                    f"前两次契约仍未通过：{planning_exception_detail(first_error)}。"
                    "这次必须先在心中对照所有前卷，另选一个此前没有使用过的主要场域、"
                    "不可逆变化和人物两难；非终卷不得出现全书终局。完整返回全部字段，"
                    "不要沿用被拒答案的句式。"
                ),
            }
        )
        result, warnings = await structured_completion(
            project.get("settings", {}),
            repair_messages,
            temperature=0.62,
            max_tokens=1250,
            timeout_seconds=240,
            validate=validate_with_mandate,
            token_ceiling=1500,
        )
        warnings.insert(0, f"分卷契约已进入深度差异化修复：{planning_exception_detail(first_error)}")
    return result["contract"], warnings


def _compact_volume_contracts(
    contracts: list[dict[str, Any]], current_index: int
) -> list[dict[str, Any]]:
    compact = []
    for index, item in enumerate(contracts):
        current = index == current_index
        compact.append(
            {
                "number": item.get("number", index + 1),
                "title": item.get("title", ""),
                "goal": bounded_excerpt(str(item.get("goal", "")), 240 if current else 100),
                "conflict": bounded_excerpt(str(item.get("conflict", "")), 260 if current else 0) if current else "",
                "ending_state": bounded_excerpt(str(item.get("ending_state", "")), 200 if current else 80),
                "bridge_to_next": bounded_excerpt(str(item.get("bridge_to_next", "")), 140 if current else 60),
                "theme_test": bounded_excerpt(str(item.get("theme_test", "")), 140 if current else 0) if current else "",
                "primary_arena": bounded_excerpt(str(item.get("primary_arena", "")), 120 if current else 60),
                "time_span": bounded_excerpt(str(item.get("time_span", "")), 80),
                "irreversible_change": bounded_excerpt(str(item.get("irreversible_change", "")), 160 if current else 70),
                "character_choice": bounded_excerpt(str(item.get("character_choice", "")), 150 if current else 0) if current else "",
                "new_story_question": bounded_excerpt(str(item.get("new_story_question", "")), 130 if current else 50),
            }
        )
    return compact


async def director_master_volume_core(
    project: dict[str, Any],
    bible: dict[str, Any],
    contracts: list[dict[str, Any]],
    specs: list[dict[str, int]],
    index: int,
    completed: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    contract = contracts[index]
    spec = specs[index]
    previous = [
        {
            "number": pos + 1,
            "title": item.get("title", ""),
            "ending_state": item.get("ending_state", ""),
            "bridge_to_next": item.get("bridge_to_next", ""),
        }
        for pos, item in enumerate(completed)
    ]
    bible_core = {
        key: bible.get(key, "")
        for key in (
            "theme", "central_conflict", "story_engine", "pacing_plan",
        )
    }
    next_boundary = (
        {
            "number": contracts[index + 1].get("number", index + 2),
            "title": contracts[index + 1].get("title", ""),
        }
        if index + 1 < len(contracts)
        else {"number": "终卷", "title": "无下一卷"}
    )
    context = f"""【精简故事圣经】
{json.dumps(bible_core, ensure_ascii=False)}
【本次固定范围】
{json.dumps(spec, ensure_ascii=False)}
【本次必须扩写的契约】
{json.dumps(_compact_volume_contracts([contract], 0)[0], ensure_ascii=False)}
【前卷已确定的结局与衔接】
{json.dumps(previous, ensure_ascii=False)}
【下一卷边界；只能由 bridge_to_next 触发，不得在本卷实际展开】
{json.dumps(next_boundary, ensure_ascii=False)}
【作者硬规则】
{bounded_excerpt(project.get('book_rules', ''), 1800)}
【阶段禁入】
本卷正文事件不得出现：{json.dumps(_director_stage_forbidden_terms(project, index + 1), ensure_ascii=False)}
这里只写本卷十章的完整因果链，绝不能摘要全书或提前讲述后续卷。"""
    messages = [
        {"role": "system", "content": DIRECTOR_VOLUME_CORE_PROMPT},
        {"role": "user", "content": context},
    ]
    validator = lambda payload: validate_director_volume_core(
        payload, spec["chapter_count"]
    )
    def validate_core_with_stage_boundary(payload: dict[str, Any]) -> None:
        validator(payload)
        core = payload.get("volume_core", {})
        _validate_director_stage_boundary(
            project,
            index + 1,
            " ".join(str(core.get(field, "")) for field in ("synopsis", "conflict")),
        )
    try:
        result, warnings = await structured_completion(
            project.get("settings", {}),
            messages,
            temperature=0.42,
            max_tokens=850,
            timeout_seconds=210,
            validate=validate_core_with_stage_boundary,
            token_ceiling=1050,
        )
    except ValueError as first_error:
        repair_messages = [dict(message) for message in messages]
        repair_messages.append(
            {
                "role": "user",
                "content": (
                    f"上次分卷核心未通过：{planning_exception_detail(first_error)}。"
                    "保持作者指定卷名、阶段事件、不可逆变化和前后衔接不变；"
                    "把 synopsis 写成 220—300 字的完整因果链，明确起因、连续升级、"
                    "人物选择、可见代价、卷末结果。不得用同义反复凑字。"
                    "完整返回全部 volume_core 字段，只输出闭合 JSON。"
                ),
            }
        )
        result, warnings = await structured_completion(
            project.get("settings", {}),
            repair_messages,
            temperature=0.52,
            max_tokens=950,
            timeout_seconds=240,
            validate=validate_core_with_stage_boundary,
            token_ceiling=1150,
        )
        warnings.insert(
            0,
            "分卷剧情核心已在长度或结构门禁后执行定向修复："
            + planning_exception_detail(first_error),
        )
    core = result["volume_core"]
    for field in ("title", "goal", "conflict", "ending_state", "bridge_to_next"):
        core[field] = str(contract.get(field, core.get(field, ""))).strip()
    core["chapter_count"] = spec["chapter_count"]
    return core, warnings


async def director_master_volume_details(
    project: dict[str, Any],
    bible: dict[str, Any],
    contracts: list[dict[str, Any]],
    specs: list[dict[str, int]],
    index: int,
    core: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    context = f"""【全书主题；只用于检验本卷选择，不得摘要后续人物弧】
{bounded_excerpt(str(bible.get('theme', '')), 500)}
【本卷范围与契约】
{json.dumps(specs[index], ensure_ascii=False)}
{json.dumps(_compact_volume_contracts([contracts[index]], 0)[0], ensure_ascii=False)}
【已经确定的本卷剧情核心，不得复述或改变】
{json.dumps(core, ensure_ascii=False)}
【作者硬规则】
{bounded_excerpt(project.get('book_rules', ''), 1600)}
【阶段禁入】
本卷转折、人物弧、支线和 must_keep 不得出现：{json.dumps(_director_stage_forbidden_terms(project, index + 1), ensure_ascii=False)}
只补本卷十章内发生的事项，不得借人物弧提前讲述后续卷。"""
    def validate_details_with_stage_boundary(payload: dict[str, Any]) -> None:
        validate_director_volume_details(payload)
        details = payload.get("volume_details", {})
        governed = []
        for field in ("turning_points", "character_arcs", "subplots", "must_keep"):
            governed.extend(details.get(field, []) if isinstance(details.get(field), list) else [])
        _validate_director_stage_boundary(
            project, index + 1, " ".join(str(item) for item in governed)
        )

    messages = [
        {"role": "system", "content": DIRECTOR_VOLUME_DETAILS_PROMPT},
        {"role": "user", "content": context},
    ]
    try:
        result, warnings = await structured_completion(
            project.get("settings", {}),
            messages,
            temperature=0.38,
            max_tokens=850,
            timeout_seconds=210,
            validate=validate_details_with_stage_boundary,
            token_ceiling=1050,
        )
    except ValueError as first_error:
        repair_messages = [dict(message) for message in messages]
        repair_messages.append(
            {
                "role": "user",
                "content": (
                    f"上次执行清单未通过：{planning_exception_detail(first_error)}。"
                    "废弃被拒清单，只围绕当前卷 synopsis 重新设计 3—5 个具体转折、"
                    "2—5 条本卷人物变化和本卷支线；不得使用阶段禁入词，也不得提前"
                    "讲后续卷。完整返回 volume_details，只输出闭合 JSON。"
                ),
            }
        )
        result, warnings = await structured_completion(
            project.get("settings", {}),
            repair_messages,
            temperature=0.52,
            max_tokens=900,
            timeout_seconds=240,
            validate=validate_details_with_stage_boundary,
            token_ceiling=1100,
        )
        warnings.insert(
            0,
            "分卷执行清单已在阶段门禁后执行定向重构："
            + planning_exception_detail(first_error),
        )
    return result["volume_details"], warnings


def _director_chapter_job(position: int, count: int) -> str:
    jobs = (
        "危机与授权：建立本卷独有问题，让人物只取得有限行动资格，不解决核心问题",
        "首次试验：执行一个可检验的小行动，只得到局部结果并暴露一个新变量",
        "反常证据：发现与上一章成功解释相矛盾的事实，改变调查或判断方向",
        "政治阻击：有明确利益的人利用制度、身份或舆论阻止方案，改变权限关系",
        "修正与代价：人物修改办法但必须损失资源、信誉、时间或一段关系",
        "对手反制：对手学习此前做法后采取针对性行动，使原方案不能继续照用",
        "联盟裂缝：让合作者因价值、利益或风险分配产生不可忽略的关系变化",
        "外部后果：让普通人、军队或地方承受前期决策后果，产生新的事实压力",
        "合法性或资源损失：人物即使取得局部成果，也失去关键支持、权限或筹码",
        "终局选择收束：让证据、关系与代价迫使人物面对两道无法同时执行的具体命令，本章暂不决定",
        "卷末部署：人物完成最后一项具体安排，并亲手切断一种求援、撤回或推诿路径",
        "卷末兑现：完成本卷最终选择和代价，只由其具体结果触发下一卷",
    )
    bucket = round(position * (len(jobs) - 1) / max(1, count - 1))
    return jobs[min(len(jobs) - 1, max(0, bucket))]


def _director_state_dimension(position: int, count: int) -> str:
    dimensions = (
        "行动权限", "局部资源", "证据与认知", "政治权限关系",
        "已经支付的具体成本与他人信任", "旧办法是否还能继续执行", "合作条件与责任分配",
        "百姓、军队或地方已经承受的可点名后果", "合法性或关键筹码",
        "两道无法同时执行的具体命令及各自代价", "已经切断的求援、撤回或推诿路径",
        "卷末总体状态",
    )
    bucket = round(position * (len(dimensions) - 1) / max(1, count - 1))
    return dimensions[min(len(dimensions) - 1, max(0, bucket))]


def _director_job_prohibition(position: int, count: int) -> str:
    bucket = round(position * 11 / max(1, count - 1))
    prohibitions = (
        "不得在本章解决危机或取得完整信任",
        "只能获得局部试验结果，不得宣布方案全面正确",
        "不得以证明原方案正确、迫使他人承认模型有效或再次成功收尾；反常事实必须真正改变判断方向",
        "不得靠技术演示化解政治阻击，必须改变权限或关系",
        "修正必须支付可见代价，不得无损优化",
        "对手反制必须让旧办法失效，不得让对手只负责赞叹",
        "联盟变化不得用一句误会带过，必须改变合作条件",
        "外部后果不得只做背景描写，必须反过来约束决策",
        "局部成果不得抵消合法性、资源或筹码损失",
        "只能摆出两道具体命令及其不同受害者，不得提前替人物决定执行哪一道",
        "必须写明人物亲手取消哪项求援、撤回或推诿手段，不得临时获得万能后援",
        "只兑现本卷承诺，不得顺带完成后续卷任务",
    )
    return prohibitions[min(11, max(0, bucket))]


def validate_director_route_role(
    route: dict[str, Any], position: int, count: int
) -> None:
    bucket = round(position * 11 / max(1, count - 1))
    combined = " ".join(
        str(route.get(field, ""))
        for field in ("goal", "turning_point", "ending_hook")
    )
    meta_phrases = (
        "可选方案集合", "形成两个不可兼得", "形成不可兼得", "形成两难",
        "主动放弃退路", "外部社会后果", "方案因对手反制失效",
        "关系出现裂痕", "联盟出现裂痕", "信誉受损", "阶段目标",
        "状态维度", "本章岗位",
    )
    leaked = [phrase for phrase in meta_phrases if phrase in combined]
    meta_patterns = (
        r"两个不可兼得", r"不可兼得.{0,6}(选择|方案)",
        r"两难(?:选择|选项)", r"压缩至.{0,8}(?:选择|选项)",
        r"放弃.{0,8}退路", r"(?:信任|关系|联盟).{0,5}裂痕",
        r"方案.{0,8}(?:失效|被反制)",
    )
    leaked.extend(
        match.group(0)
        for pattern in meta_patterns
        if (match := re.search(pattern, combined))
    )
    if leaked:
        raise ValueError(
            "章节路线照抄了导演节拍术语，必须改写成具体事件与可观察结果："
            + "、".join(leaked)
        )
    if bucket == 2 and re.search(
        r"证明.{0,12}(正确|有效)|验证.{0,12}(正确|有效)|迫使.{0,16}(承认|认可)|再次成功|正确性",
        combined,
    ):
        raise ValueError(
            "章节岗位高度重复：反常证据章不得再次以证明方案正确或获得认可作为转折"
        )


def validate_director_route_structure(
    result: dict[str, Any], chapter_number: int
) -> dict[str, Any]:
    require_schema("route")(result)
    route = result["route"]
    if not isinstance(route, dict):
        raise ValueError("章节路线 route 不是对象")
    for field in (
        "title", "goal", "conflict", "turning_point", "ending_hook",
        "must_keep", "must_avoid",
    ):
        if field not in route:
            raise ValueError(f"章节路线缺少 {field}")
    for field in ("title", "goal", "conflict", "turning_point", "ending_hook"):
        if not str(route.get(field, "")).strip():
            raise ValueError(f"章节路线 {field} 为空")
    if not isinstance(route.get("must_keep"), list) or not isinstance(
        route.get("must_avoid"), list
    ):
        raise ValueError("章节路线 must_keep 和 must_avoid 必须是数组")
    if len(route["must_keep"]) > 3:
        raise ValueError("章节路线 must_keep 最多保留与本章直接相关的 3 条事实")
    if len(route["must_avoid"]) > 3:
        raise ValueError("章节路线 must_avoid 最多列出本章最相关的 3 条禁令")
    route["number"] = chapter_number
    normalized = normalize_route(route, chapter_number)
    # A model may add an unsolicited quality_warnings field containing its own
    # self-assessment.  Only deterministic validators may create production
    # debt; otherwise praise such as "no modern terms used" blocks release.
    normalized["quality_warnings"] = []
    return normalized


def validate_director_route_language(
    project: dict[str, Any], route: dict[str, Any]
) -> None:
    genre = str(project.get("genre", ""))
    if not any(marker in genre for marker in ("历史", "古代", "战国", "架空")):
        return
    text = " ".join(
        str(route.get(field, ""))
        for field in ("title", "goal", "conflict", "turning_point", "ending_hook")
    )
    # must_avoid is control metadata and commonly repeats the exact forbidden
    # phrase supplied by the production specification.  Judging it as story
    # language makes a compliant route impossible to save.  must_keep remains
    # checked because it is positive story context that can flow into prose.
    text += " " + " ".join(str(item) for item in route.get("must_keep", []))
    forbidden = [
        marker for marker in (
            "毫秒", "秒级", "黑盒化", "算法", "APP", "互联网", "数据库",
            "物理", "数字化", "全省", "数据", "误差率", "行政授权",
            "外交官", "标准化", "系统性", "信任度", "机构",
            "模型", "系统", "自动标记", "高危", "管控", "背锅", "背书",
            "机器逻辑", "帝国机器", "复核官署",
        )
        if marker.lower() in text.lower()
    ]
    if forbidden:
        raise ValueError(
            f"时代语言质量问题：章节路线含有不应直接出现的现代技术词 {', '.join(forbidden)}"
        )
    if re.search(
        r"\d+(?:\.\d+)?\s*%|(?:信任|粮食|资源|效率|风险|关系)维度\s*[-+]\s*\d+|A\s*/\s*B区",
        text,
        re.IGNORECASE,
    ):
        raise ValueError(
            "时代语言质量问题：古代题材路线不得使用百分比、数值维度或 A/B 分区表达"
        )


def _director_historicalize_context(
    project: dict[str, Any], text: str
) -> str:
    genre = str(project.get("genre", ""))
    if not any(marker in genre for marker in ("历史", "古代", "战国", "架空")):
        return text
    replacements = (
        ("系统性", "成片"), ("标准化", "统一尺度"), ("数字化", "改用统一簿籍"),
        ("数据包", "原始簿册"), ("数据库", "簿册库"), ("计算模型", "推算之法"),
        ("物理阻抗", "实物阻碍"), ("行政授权", "官署授命"), ("审查机构", "御史属官"),
        ("操作员", "经手吏员"), ("编码", "记号"), ("流程", "次序"),
        ("审计", "复核"), ("数据", "簿籍数目"), ("模型", "推算之法"),
        ("网络", "文书通路"), ("信息", "文书消息"), ("权限", "职权"),
        ("机制", "成法"), ("技术", "手段"), ("维度", "一项状态"),
        ("指标", "数目"), ("透明性", "可查验程度"),
    )
    result = text
    for source, target in replacements:
        result = result.replace(source, target)
    return result


def validate_director_assigned_turn(
    route: dict[str, Any], assigned_turn: str
) -> None:
    """Ensure a scheduled volume turning point is actually dramatized.

    Small local models sometimes acknowledge the assigned turn in reasoning but
    return a structurally valid route from an earlier volume.  Requiring two
    concrete Chinese bigram anchors keeps paraphrase freedom while preventing a
    completely unrelated event from occupying the scheduled chapter.
    """
    if not assigned_turn.strip():
        return
    common = {
        "秦策", "发现", "制度", "问题", "引发", "开始", "必须", "选择",
        "地方", "形成", "进行", "通过", "成为", "关键", "首次", "最终",
        "要求", "导致", "不得", "需要", "出现", "实现", "完成", "同时",
        "统一", "标准", "规则", "隐秘", "维持", "无法", "完全", "覆盖",
        "推行",
    }
    anchors = _chinese_bigrams(assigned_turn) - common
    combined = " ".join(
        str(route.get(field, ""))
        for field in ("title", "goal", "conflict", "turning_point", "ending_hook")
    )
    overlap = anchors & _chinese_bigrams(combined)
    required = min(8, max(2, (len(anchors) + 4) // 5))
    if len(overlap) < required:
        raise ValueError(
            "章节未承载指定卷级转折，必须围绕这件事重新设计："
            + bounded_excerpt(assigned_turn, 180)
        )


def validate_director_future_turns(
    route: dict[str, Any], future_turns: list[tuple[int, str]]
) -> None:
    """Prevent an early chapter from consuming a later scheduled turn."""
    common = {
        "秦策", "发现", "制度", "问题", "引发", "开始", "必须", "选择",
        "地方", "形成", "进行", "通过", "成为", "关键", "首次", "最终",
        "要求", "导致", "不得", "需要", "出现", "实现", "完成", "同时",
        "统一", "标准", "规则", "隐秘", "维持", "无法", "完全", "覆盖",
        "推行",
    }
    combined = " ".join(
        str(route.get(field, ""))
        for field in ("title", "goal", "conflict", "turning_point", "ending_hook")
    )
    route_bigrams = _chinese_bigrams(combined)
    for chapter_number, future_turn in future_turns:
        anchors = _chinese_bigrams(future_turn) - common
        # Two anchors often describe only the shared scene (for example
        # "燕地 + 度量").  Three anchors indicate that the later event's
        # actor/action has also been consumed.
        required = 3
        if len(anchors & route_bigrams) >= required:
            raise ValueError(
                f"章节提前占用第 {chapter_number} 章指定转折，当前章不得实现："
                + bounded_excerpt(future_turn, 160)
            )


def _route_similarity_score(
    route: dict[str, Any], previous_routes: list[dict[str, Any]]
) -> float:
    if not previous_routes:
        return 0.0
    fields = ("title", "goal", "conflict", "turning_point", "ending_hook")
    maximum = 0.0
    for previous in previous_routes:
        per_field = []
        for field in fields:
            left = re.sub(r"[\W_]+", "", str(route.get(field, "")))
            right = re.sub(r"[\W_]+", "", str(previous.get(field, "")))
            if left and right:
                per_field.append(SequenceMatcher(None, left, right).ratio())
        if per_field:
            maximum = max(maximum, max(per_field))
    return maximum


def _director_routes_before_volume(
    project: dict[str, Any], volume: dict[str, Any]
) -> list[dict[str, Any]]:
    current_start = int(volume.get("chapter_start", 1) or 1)
    routes: list[dict[str, Any]] = []
    for item in project.get("planning", {}).get("volumes", []):
        if not isinstance(item, dict):
            continue
        if int(item.get("chapter_end", 0) or 0) >= current_start:
            continue
        routes.extend(
            route
            for route in item.get("chapters", [])
            if isinstance(route, dict)
        )
    return routes


def _director_assigned_turn(
    volume: dict[str, Any], position: int, count: int
) -> str:
    turning_points = [str(item) for item in volume.get("turning_points", [])]
    if not turning_points:
        return ""
    turn_positions = [
        round((turn_index + 1) * (count - 1) / (len(turning_points) + 1))
        for turn_index in range(len(turning_points))
    ]
    if position not in turn_positions:
        return ""
    return turning_points[turn_positions.index(position)]


def _audit_route_checkpoint_prefix(
    project: dict[str, Any],
    volume: dict[str, Any],
    routes: list[dict[str, Any]],
) -> tuple[int, str]:
    start, end = int(volume["chapter_start"]), int(volume["chapter_end"])
    count = end - start + 1
    accepted: list[dict[str, Any]] = []
    earlier_routes = _director_routes_before_volume(project, volume)
    for index, route in enumerate(routes):
        number = start + index
        try:
            normalized = validate_director_route_structure(
                {"route": dict(route)}, number
            )
            forbidden = [] if number == end else [
                str(volume.get("goal", "")), str(volume.get("ending_state", "")),
                str(volume.get("irreversible_change", "")),
                str(volume.get("bridge_to_next", "")),
            ]
            validate_route_batch(
                {"chapters": [normalized]},
                [number],
                earlier_routes + accepted,
                forbidden,
            )
            seed = _director_chapter_seed(
                project,
                int(volume.get("number", 0) or 0),
                number,
            )
            if not seed:
                validate_director_route_role(normalized, index, count)
            validate_director_route_language(project, normalized)
            if not seed:
                validate_director_assigned_turn(
                    normalized,
                    _director_assigned_turn(volume, index, count),
                )
                validate_director_future_turns(
                    normalized,
                    [
                        (start + later, turn)
                        for later in range(index + 1, count)
                        if (turn := _director_assigned_turn(volume, later, count))
                    ],
                )
            if not seed:
                validate_director_volume_domain(
                    project,
                    int(volume.get("number", 0) or 0),
                    normalized,
                )
            validate_director_chapter_seed(
                normalized,
                seed,
            )
            validate_director_chapter_forbidden_terms(
                normalized,
                _director_chapter_forbidden_terms(
                    project, int(volume.get("number", 0) or 0), number
                ),
            )
            validate_director_chapter_forbidden_terms(
                normalized,
                _director_chapter_core_forbidden_terms(
                    project, int(volume.get("number", 0) or 0), number
                ),
                include_hook=False,
            )
            _validate_director_stage_boundary(
                project,
                int(volume.get("number", 0) or 0),
                " ".join(
                    str(normalized.get(field, ""))
                    for field in (
                        ("title", "goal", "conflict", "turning_point")
                        if number == end
                        else ("title", "goal", "conflict", "turning_point", "ending_hook")
                    )
                ),
            )
            accepted.append(normalized)
        except Exception as exc:
            return index, planning_exception_detail(exc)
    return len(routes), ""


async def director_plan_chapter_route(
    project: dict[str, Any],
    volume: dict[str, Any],
    chapter_number: int,
    previous_routes: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[str]]:
    start, end = int(volume["chapter_start"]), int(volume["chapter_end"])
    position = chapter_number - start
    count = end - start + 1
    final = chapter_number == end
    assigned_turn = _director_assigned_turn(volume, position, count)
    chapter_seed = _director_chapter_seed(
        project, int(volume.get("number", 0) or 0), chapter_number
    )
    chapter_forbidden_terms = _director_chapter_forbidden_terms(
        project, int(volume.get("number", 0) or 0), chapter_number
    )
    chapter_core_forbidden_terms = _director_chapter_core_forbidden_terms(
        project, int(volume.get("number", 0) or 0), chapter_number
    )
    effective_job = (
        "作者章种子执行：完成种子指定的场景、行动、局部结果与代价"
        if chapter_seed else _director_chapter_job(position, count)
    )
    effective_dimension = (
        "作者章种子明确规定的局部状态"
        if chapter_seed else _director_state_dimension(position, count)
    )
    effective_prohibition = (
        "不得用通用节拍覆盖章种子，不得停在种子要求之前，也不得越过种子提前完成卷末"
        if chapter_seed else _director_job_prohibition(position, count)
    )
    master = project.get("planning", {}).get("master", {})
    earlier_routes = _director_routes_before_volume(project, volume)
    comparison_routes = earlier_routes + previous_routes
    volume_event_palette = {
        "primary_arena": volume.get("primary_arena", ""),
        "assigned_turn_for_this_chapter": assigned_turn or "本章没有卷级转折配额",
        "required_event_terms": _director_stage_event_terms(
            project, int(volume.get("number", 0) or 0)
        ),
        "single_scene_locations": _director_stage_location_terms(
            project, int(volume.get("number", 0) or 0)
        ),
    }
    current_volume_chain = [
        {
            "number": item.get("number"),
            "title": item.get("title", ""),
            "goal": bounded_excerpt(str(item.get("goal", "")), 90),
            "ending_hook": bounded_excerpt(str(item.get("ending_hook", "")), 80),
        }
        for item in previous_routes
    ]
    characters = [
        {"name": item.get("name", ""), "role": item.get("role", "")}
        for item in project.get("characters", [])[:8]
    ]
    context = f"""【全书方向】
核心主题：{bounded_excerpt(master.get('theme', ''), 260)}
全书核心冲突：{bounded_excerpt(master.get('central_conflict', ''), 360)}
【本卷】
卷名：{volume.get('title', '')}（第 {start}-{end} 章）
详细梗概：{bounded_excerpt(volume.get('synopsis', ''), 1300)}
卷目标：{bounded_excerpt(volume.get('goal', ''), 400)}
主导冲突：{bounded_excerpt(volume.get('conflict', ''), 500)}
本卷唯一主场域：{bounded_excerpt(volume.get('primary_arena', ''), 220)}
明确时间跨度：{bounded_excerpt(volume.get('time_span', ''), 160)}
本卷不可逆变化：{bounded_excerpt(volume.get('irreversible_change', ''), 260)}
核心人物不可兼得选择：{bounded_excerpt(volume.get('character_choice', ''), 260)}
卷末状态：{bounded_excerpt(volume.get('ending_state', ''), 400)}
下一卷触发：{bounded_excerpt(volume.get('bridge_to_next', ''), 300)}
【本卷事件材料白名单】
{json.dumps(volume_event_palette, ensure_ascii=False)}
本章主要地点、机构、器物、受害者和对抗方式必须来自上述本卷材料；材料中没有的前卷专属粮仓、密档、调令、审查机构只能作为一句因果背景，不得再次成为本章主要事件或证物。
【本卷已经保存的因果链】
{json.dumps(current_volume_chain, ensure_ascii=False) if current_volume_chain else '本章是本卷开篇，尚无前章路线'}
必须直接承接最后一条 ending_hook；不得把本卷已经发生的事件重新开场。must_keep 与 must_avoid 各只选本章直接相关的 1-3 条，不得复制整卷清单。
【本次唯一任务】
只规划第 {chapter_number} 章，这是本卷第 {position + 1}/{count} 章。
作者指定章种子：{chapter_seed or '未指定；只按本章岗位设计局部事件'}
若章种子非空，它高于模型自行选择的事件；必须保留其中的主场景、关键物件与局部结果，不得换成同卷其他转折。
本章作者禁入词：{json.dumps(chapter_forbidden_terms, ensure_ascii=False) if chapter_forbidden_terms else '无额外章级禁入'}
本章核心禁入词（可作为 ending_hook，但不得进入标题、目标、冲突或转折）：{json.dumps(chapter_core_forbidden_terms, ensure_ascii=False) if chapter_core_forbidden_terms else '无'}
本章岗位：{effective_job}
本章唯一主要状态维度：{effective_dimension}。goal 必须只把这一维度从旧状态改成新状态；不要再次把“模型/方案得到验证”当作目标。
本章岗位禁令：{effective_prohibition}
岗位与状态维度只是内部导演指令，title、goal、conflict、turning_point、ending_hook 中不得复述“形成两难、可选方案集合、主动放弃退路、外部社会后果、关系裂痕、信誉受损”等节拍术语；必须写成点名人物围绕具体简牍、名籍、粮仓、印信或命令采取了什么行动，章末实际改变了什么。
本章承载转折：{assigned_turn or '本章不兑现卷级关键转折，只完成岗位规定的局部状态变化'}
如果“本章承载转折”给出了具体事件，title、goal、conflict 或 turning_point 中必须直接保留该事件至少两个具体对象或动作（如地名、人物、器物、命令、罢工或灾情），不得换回前卷的粮册、密档、调令故事。
这是卷末章：{'是，必须完成卷目标并形成下一卷触发' if final else '否，严禁提前兑现卷目标、卷末状态或下一卷桥梁'}
【历史隔离】
全书此前已有 {len(comparison_routes)} 条路线。其文本故意不提供，避免把旧卷地点、机构和证物污染到本卷；服务端会自动检查重名与同功能重复，若撞车会在修订轮只指出那一条。
【可用人物】
{json.dumps(characters, ensure_ascii=False)}
【全书硬规则】
{bounded_excerpt(project.get('book_rules', ''), 1600)}"""
    context = _director_historicalize_context(project, context)

    forbidden = [] if final else [
        str(volume.get("goal", "")),
        str(volume.get("ending_state", "")),
        str(volume.get("irreversible_change", "")),
        str(volume.get("bridge_to_next", "")),
    ]
    future_turns = [
        (start + later, turn)
        for later in range(position + 1, count)
        if (turn := _director_assigned_turn(volume, later, count))
    ]

    base_messages = [
        {"role": "system", "content": DIRECTOR_CHAPTER_ROUTE_PROMPT},
        {"role": "user", "content": context},
    ]
    last_error: Exception | None = None
    previous_candidate: dict[str, Any] | None = None
    best_candidate: dict[str, Any] | None = None
    best_issue = ""
    best_score = float("inf")

    # A malformed response is a structural failure.  A complete route rejected
    # by a quality gate is recoverable: ask the model to alter the event/state,
    # retain the most distinct candidate, and never discard earlier checkpoints.
    for attempt in range(6):
        messages = [dict(message) for message in base_messages]
        if last_error is not None:
            issue_detail = planning_exception_detail(last_error)
            repair = (
                f"第 {attempt} 次候选未通过检查：{issue_detail}。"
                "请重新设计本章的具体事件和状态变化，不要只替换同义词。"
                f"必须服从本章岗位“{effective_job}”与唯一状态维度"
                f"“{effective_dimension}”，并避开已经保存路线的目标、"
                "转折和结果。只返回完整闭合 JSON。"
            )
            if "时代语言质量问题" in issue_detail:
                repair += (
                    "\n上轮含有现代词。请直接改用战国官署、简牍、度量、道路、"
                    "赋税和实物后果的说法；不要复述被拒候选，也不要解释你避开了哪些词。"
                )
            duplicate_match = re.search(
                r"第\s*(\d+)\s*与第\s*(\d+)\s*条章节(?:目标|核心冲突|关键转折|章末变化)高度重复",
                issue_detail,
            )
            if duplicate_match:
                earlier_index = int(duplicate_match.group(1)) - 1
                if 0 <= earlier_index < len(comparison_routes):
                    conflicting_route = comparison_routes[earlier_index]
                    repair += (
                        "\n与本候选冲突的既有路线如下。新候选必须更换事件对象、行动方式与"
                        "章末状态，不能沿用同一制度动作：\n"
                        + json.dumps(
                            {
                                "number": conflicting_route.get("number"),
                                "title": conflicting_route.get("title", ""),
                                "goal": conflicting_route.get("goal", ""),
                                "conflict": conflicting_route.get("conflict", ""),
                                "turning_point": conflicting_route.get("turning_point", ""),
                            },
                            ensure_ascii=False,
                        )
                    )
            if previous_candidate and "时代语言质量问题" not in issue_detail:
                repair += (
                    "\n被拒候选如下，只用于识别问题，不得复述：\n"
                    + json.dumps(previous_candidate, ensure_ascii=False)
                )
            messages.append({"role": "user", "content": repair})
        try:
            raw = await chat_once(
                project.get("settings", {}),
                messages,
            temperature=min(0.86, 0.46 + attempt * 0.08),
                max_tokens=850 + attempt * 40,
                json_mode=True,
                timeout_seconds=210,
            )
            result = parse_json_response(raw)
            candidate = validate_director_route_structure(result, chapter_number)
            previous_candidate = candidate
        except Exception as exc:
            last_error = exc
            previous_candidate = None
            continue

        try:
            validate_route_batch(
                {"chapters": [candidate]},
                [chapter_number],
                comparison_routes,
                forbidden,
            )
            if not chapter_seed:
                validate_director_route_role(candidate, position, count)
            validate_director_route_language(project, candidate)
            if not chapter_seed:
                validate_director_assigned_turn(candidate, assigned_turn)
                validate_director_future_turns(candidate, future_turns)
            if not chapter_seed:
                validate_director_volume_domain(
                    project,
                    int(volume.get("number", 0) or 0),
                    candidate,
                )
            validate_director_chapter_seed(candidate, chapter_seed)
            validate_director_chapter_forbidden_terms(
                candidate, chapter_forbidden_terms
            )
            validate_director_chapter_forbidden_terms(
                candidate, chapter_core_forbidden_terms, include_hook=False
            )
            _validate_director_stage_boundary(
                project,
                int(volume.get("number", 0) or 0),
                " ".join(
                    str(candidate.get(field, ""))
                    for field in (
                        ("title", "goal", "conflict", "turning_point")
                        if position == count - 1
                        else ("title", "goal", "conflict", "turning_point", "ending_hook")
                    )
                ),
            )
            warnings = []
            if attempt:
                warnings.append(
                    f"章节路线第 {attempt + 1} 轮定向修复后通过质量门"
                )
            return candidate, warnings
        except Exception as exc:
            last_error = exc
            issue = planning_exception_detail(exc)
            penalty = 0.0
            if "时代语言" in issue:
                penalty += 0.75
            if "提前兑现" in issue:
                penalty += 0.65
            if "岗位高度重复" in issue:
                penalty += 0.55
            score = _route_similarity_score(candidate, comparison_routes) + penalty
            if score < best_score:
                best_candidate = deepcopy(candidate)
                best_issue = issue
                best_score = score

    if best_candidate is not None:
        if any(
            marker in best_issue
            for marker in (
                "标题重复", "标题高度相似", "目标高度相似", "目标高度重复", "目标重复",
                "未来阶段串线", "提前兑现", "岗位高度重复",
                "导演节拍术语", "核心意象", "未承载指定卷级转折",
                "提前占用第", "时代语言质量问题",
                "偏离本卷事件材料", "单章主场域过多",
                "未承载作者指定章种子",
                "作者章级禁入词",
            )
        ):
            candidate_summary = (
                f"；差异度最高的被拒候选：标题《{bounded_excerpt(str(best_candidate.get('title', '')), 60)}》；"
                f"目标：{bounded_excerpt(str(best_candidate.get('goal', '')), 180)}；"
                f"冲突：{bounded_excerpt(str(best_candidate.get('conflict', '')), 180)}；"
                f"转折：{bounded_excerpt(str(best_candidate.get('turning_point', '')), 160)}"
            )
            raise ValueError(
                "章节路线连续六轮仍存在不可接受的结构重复："
                + best_issue
                + candidate_summary
            )
        warning = (
            "章节路线经过 6 轮定向修复仍有软质量风险，系统已选择差异度最高的"
            f"完整候选并记录规划质量债务：{best_issue}"
        )
        best_candidate["quality_warnings"] = [best_issue]
        return best_candidate, [warning]
    if last_error is not None:
        raise last_error
    raise ValueError("章节路线生成未返回任何可检查结果")


def _director_full_outline(volumes: list[dict[str, Any]]) -> str:
    sections = []
    for index, volume in enumerate(volumes, start=1):
        turns = "；".join(str(item) for item in volume.get("turning_points", []) if str(item).strip())
        sections.append(
            f"第{index}卷《{volume.get('title', '')}》\n"
            f"阶段目标：{volume.get('goal', '')}\n"
            f"主导冲突：{volume.get('conflict', '')}\n"
            f"剧情展开：{volume.get('synopsis', '')}\n"
            f"关键转折：{turns}\n"
            f"卷末状态：{volume.get('ending_state', '')}\n"
            f"后续触发：{volume.get('bridge_to_next', '')}"
        )
    return "\n\n".join(sections)


@app.post("/api/incubator")
async def incubator(body: IncubatorRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    seed = body.seed.strip()
    if len(seed) < 8:
        raise HTTPException(400, "灵感至少需要 8 个字")
    target = max(3, min(300, int(body.target_chapters or 30)))
    story_mode = "short" if body.story_mode == "short" else "long"
    context = f"""【作者的原始灵感】
{bounded_excerpt(seed, 8000)}
【作者补充偏好与禁区】
{bounded_excerpt(body.preferences, 6000) or '没有额外要求，由总导演提供两种不同但可持续的方向。'}
【作品形态与计划长度】
{'短篇/中短篇' if story_mode == 'short' else '长篇/连载'}；约 {target} 章
【当前作品里可以继承的内容】
题材：{project.get('genre', '')}
长期作者意图：{bounded_excerpt(project.get('author_intent', ''), 2500)}
硬规则：{bounded_excerpt(project.get('book_rules', ''), 3500)}
已有文风方向：{bounded_excerpt(project.get('style', {}).get('profile', ''), 1500)}

作者只要求把灵感发展成可选择、可编辑、可继续做全书规划的开书方案。
不要把当前作品的旧剧情和人物强行带入，除非作者在灵感或偏好中明确要求继承。"""
    try:
        # Stage 1 deliberately excludes character cards and lore. Two complete
        # choice cores fit small cloud/local models much more reliably than the
        # previous monolithic response containing every asset for both books.
        result, warnings = await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": INCUBATOR_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.64,
            max_tokens=3600,
            timeout_seconds=420,
            validate=lambda payload: validate_incubator_core_result(
                payload,
                target_chapters=target,
                story_mode=story_mode,
            ),
            token_ceiling=max(
                3900,
                min(4800, int(project.get("settings", {}).get("context_budget", 24000)) - 1800),
            ),
        )
        options: list[dict[str, Any]] = []
        for index, raw_option in enumerate(result["options"], start=1):
            option = deepcopy(raw_option)
            option["target_chapters"] = target
            asset_context = f"""【已经确定的候选方向；不得改写】
{json.dumps(option, ensure_ascii=False)}
【作者原始灵感】
{bounded_excerpt(seed, 3500)}
【作者偏好与禁区】
{bounded_excerpt(body.preferences, 3500) or '没有额外补充。'}

这是第 {index}/2 套候选方向。只补充这套方向的人物和世界资产。"""

            def validate_assets(payload: dict[str, Any]) -> None:
                validate_incubator_assets(payload)
                validate_asset_authority(payload, asset_context)

            assets, asset_warnings = await structured_completion(
                project.get("settings", {}),
                [
                    {"role": "system", "content": INCUBATOR_ASSETS_PROMPT},
                    {"role": "user", "content": asset_context},
                ],
                temperature=0.52,
                max_tokens=3000,
                timeout_seconds=360,
                validate=validate_assets,
                token_ceiling=3600,
            )
            option["characters"] = assets["characters"]
            option["world_entries"] = assets["world_entries"]
            options.append(option)
            warnings.extend(
                f"第 {index} 套资产：{warning}" for warning in asset_warnings
            )
        result = {"options": options}
        validate_incubator_result(
            result, target_chapters=target, story_mode=story_mode
        )
        result["warnings"] = warnings
        result["generation_mode"] = "staged_transaction"
        result["fallback"] = False
        return result
    except Exception as exc:
        if not recoverable_model_error(exc):
            raise HTTPException(
                502, f"灵感孵化失败：{planning_exception_detail(exc)}"
            ) from exc
        raise HTTPException(
            502,
            "灵感孵化没有得到完整方案："
            f"{planning_exception_detail(exc)}。原始灵感未被修改，请重试。",
        ) from exc


@app.post("/api/ideas")
async def ideas(body: IdeasRequest) -> dict[str, Any]:
    project = ensure_project_defaults(body.project)
    narrative = project.get("narrative", {})
    memory = project.get("memory", {})
    context = f"""【推荐类型】
{'开书主题与核心冲突' if body.kind == 'book' else '下一阶段可选主题与冲突'}
【作品形态】
{project.get('story_mode', 'long')}，计划约 {narrative.get('target_chapters', 30)} 章
【书名与题材】
{project.get('title', '')}；{project.get('genre', '')}
【核心构想】
{project.get('premise', '')}
【作者长期意图】
{project.get('author_intent', '')}
【核心问题与结局方向】
{narrative.get('central_question', '')}
{narrative.get('ending_direction', '')}
【人物】
{bounded_excerpt(json.dumps(project.get('characters', []), ensure_ascii=False), 7000)}
【全书进展】
{memory.get('story_so_far', '')}
【未结线索】
{bounded_excerpt(json.dumps(memory.get('plot_threads', []), ensure_ascii=False), 5000)}
【作者补充】
{body.instruction}"""
    try:
        result, warnings = await structured_completion(
            project.get("settings", {}),
            [
                {"role": "system", "content": IDEAS_PROMPT},
                {"role": "user", "content": context},
            ],
            temperature=0.65,
            max_tokens=1200,
            timeout_seconds=180,
            validate=require_schema(
                "options",
                list_fields=("options",),
                list_bounds={"options": (3, None)},
            ),
        )
        options = result.get("options", [])
        if not isinstance(options, list):
            options = []
        clean = []
        for option in options[:6]:
            if not isinstance(option, dict):
                continue
            clean.append(
                {
                    key: str(option.get(key, ""))
                    for key in (
                        "title",
                        "theme",
                        "central_conflict",
                        "story_promise",
                        "turning_point",
                        "ending_direction",
                        "why_fit",
                        "risk",
                    )
                }
            )
        if len(clean) < 3:
            raise ValueError(f"模型只返回 {len(clean)} 个可用方案")
        return {"options": clean[:3], "fallback": False, "warnings": warnings}
    except Exception as exc:
        if recoverable_model_error(exc):
            return local_ideas(project, body.kind, planning_exception_detail(exc))
        raise HTTPException(
            502, f"灵感推荐失败：{planning_exception_detail(exc)}"
        ) from exc


def _prose_char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def _resolved_prose_target(project: dict[str, Any], body: GenerateRequest) -> int:
    target = int(body.target_words or project.get("settings", {}).get("target_words", 1200))
    if body.mode == "rewrite" and body.selection.strip():
        selected = _prose_char_count(body.selection)
        if selected:
            target = max(100, selected)
    return max(100, min(10000, target))


def _effective_prose_settings(settings: dict[str, Any], target_chars: int) -> dict[str, Any]:
    """Reserve enough output space for Chinese prose without changing saved settings.

    A model can stop before max_tokens, so this is only a ceiling. The follow-up
    repair below handles early stops. 8192 keeps requests practical for Qwen3-8B
    while still supporting chapters around 5k Chinese characters.
    """
    result = dict(settings or {})
    configured = max(256, int(result.get("max_tokens", 3500) or 3500))
    suggested = int(math.ceil(max(100, target_chars) * 1.55)) + 320
    result["max_tokens"] = min(8192, max(configured, suggested))
    return result


def _prose_looks_truncated(text: str) -> bool:
    stripped = str(text or "").rstrip()
    if not stripped:
        return True
    if stripped[-1] in "，、；：—（【“‘":
        return True
    if stripped[-1] not in "。！？!?……”’）】":
        return True
    return any(stripped.count(left) != stripped.count(right) for left, right in (("“", "”"), ("‘", "’"), ("（", "）"), ("【", "】")))


def _number_phrases(text: str) -> set[str]:
    """Return explicit Chinese/Arabic quantities, including their unit when present."""
    # Only quantities that can materially change logistics or chronology are
    # hard-gated. Generic counters such as 一份/两枚 are often harmless ways of
    # enumerating the already-authorized three documents and their seals.
    units = "石车日次刻更辆道里年月时"
    matches = set(
        re.findall(
            rf"(?:\d+(?:[{units}])?|[零〇一二两三四五六七八九十百千万]+[{units}]|[零〇一二两三四五六七八九十百千万]{{2,}})",
            str(text or ""),
        )
    )
    return matches - {"一时"}


def _scene_constraint_violations(
    segment: str,
    route: dict[str, Any],
    scene_index: int,
    scene_count: int,
    delayed_actions: list[str],
    number_authority: str = "",
) -> list[str]:
    """Catch cheap, objective route violations before accepting a model scene."""
    violations: list[str] = []
    authority = number_authority or json.dumps(route, ensure_ascii=False)
    allowed_numbers = _number_phrases(authority)
    invented_numbers = sorted(_number_phrases(segment) - allowed_numbers)
    if invented_numbers:
        violations.append("出现路线未授权的数字或时刻：" + "、".join(invented_numbers[:8]))
    hidden_terms = {"仓内", "粮囤", "粮垛", "粮袋", "门缝"} if "开仓" in delayed_actions else set()
    if scene_index < scene_count and (
        any(action in segment for action in delayed_actions)
        or any(term in segment for term in hidden_terms)
    ):
        violations.append("提前执行章末动作或描写尚不可见的内部状态")
    if scene_index == scene_count:
        missing = [action for action in delayed_actions if action not in segment]
        if missing:
            violations.append("末段遗漏路线规定的章末动作：" + "、".join(missing))
        ending = str(route.get("ending_hook", "")).strip()
        if ending and _planning_similarity(segment[-800:], ending) < 0.18:
            violations.append("末段没有充分交付 ending_hook 的结果状态")
    return violations


def _strip_nonfinal_scene_violations(
    segment: str,
    route: dict[str, Any],
    scene_index: int,
    scene_count: int,
    delayed_actions: list[str],
    number_authority: str = "",
) -> str:
    """Remove whole unsafe sentences; never rewrite a model's factual value."""
    authority_numbers = _number_phrases(
        number_authority or json.dumps(route, ensure_ascii=False)
    )
    forbidden = _number_phrases(segment) - authority_numbers
    if scene_index < scene_count:
        forbidden.update(action for action in delayed_actions if action in segment)
        if "开仓" in delayed_actions:
            forbidden.update(
                term for term in ("仓内", "粮囤", "粮垛", "粮袋", "门缝")
                if term in segment
            )
    if not forbidden:
        return str(segment or "").strip()
    units = [
        item.strip()
        for item in re.findall(r"[^。！？!?]+[。！？!?]?", str(segment or ""))
        if item.strip()
    ]
    return "".join(
        unit for unit in units if not any(term in unit for term in forbidden)
    ).strip()


def _ensure_final_route_closure(
    segment: str, route: dict[str, Any], delayed_actions: list[str]
) -> str:
    """Land a human-reviewed ending hook when a small model evades it."""
    text = str(segment or "").strip()
    ending = str(route.get("ending_hook", "")).strip()
    if not ending:
        return text
    if _normalize_prose_unit(ending) in _normalize_prose_unit(text):
        return text
    ending = ending if ending[-1:] in "。！？!?" else ending + "。"
    return (text.rstrip() + "\n\n" + ending).strip()


def _normalize_prose_unit(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _dedupe_adjacent_sentence_blocks(text: str) -> str:
    """Collapse exact adjacent sentence-block loops in a continuation response.

    Small models sometimes satisfy "continue writing" by repeating the same
    2-4 sentence ending several times.  We only remove *adjacent exact* blocks,
    so legitimate callbacks elsewhere in the chapter are untouched.
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    units = [
        item.strip()
        for item in re.findall(r"[^。！？!?]+[。！？!?]?", raw)
        if item.strip()
    ]
    if len(units) < 2:
        return raw
    index = 0
    cleaned: list[str] = []
    while index < len(units):
        best = 0
        # Provider loops can repeat an entire scene, not just a short ending.
        max_block = min(40, (len(units) - index) // 2)
        # Use the *smallest* repeated unit.  If a tiny phrase is repeated many
        # times, a larger multiple of that phrase must not accidentally satisfy
        # the long-block threshold and get collapsed.  We only remove genuinely
        # long repeated passages (the failure seen in cloud QA).
        for size in range(1, max_block + 1):
            left = [_normalize_prose_unit(item) for item in units[index : index + size]]
            right = [_normalize_prose_unit(item) for item in units[index + size : index + size * 2]]
            if left == right:
                if sum(len(item) for item in left) >= 60:
                    best = size
                break
        if best:
            cleaned.extend(units[index : index + best])
            index += best
            while index + best <= len(units):
                current = [_normalize_prose_unit(item) for item in units[index : index + best]]
                prior = [_normalize_prose_unit(item) for item in cleaned[-best:]]
                if current != prior:
                    break
                index += best
            continue
        cleaned.append(units[index])
        index += 1
    return "".join(cleaned).strip()


def _strip_unsupported_recollections(text: str, authority: str) -> str:
    """Drop explicit recollections that have no support in authoritative context."""
    markers = ("想起", "记得", "曾经", "上次", "往日", "昔日", "昨日见过")
    authority_bigrams = _chinese_bigrams(authority)
    units = re.findall(r"[^。！？!?]+[。！？!?]?", str(text or ""))
    kept: list[str] = []
    for unit in units:
        if not any(marker in unit for marker in markers):
            kept.append(unit)
            continue
        claim = unit
        for marker in markers:
            claim = claim.replace(marker, "")
        claim_bigrams = _chinese_bigrams(claim)
        overlap = len(claim_bigrams & authority_bigrams)
        coverage = overlap / max(1, min(len(claim_bigrams), len(authority_bigrams)))
        if overlap >= 6 and coverage >= 0.55:
            kept.append(unit)
    return "".join(kept).strip()


def _remove_orphan_chinese_quotes(text: str) -> str:
    """Remove unmatched quote glyphs without changing any prose words."""
    result = str(text or "")
    for opening, closing in (("“", "”"), ("‘", "’")):
        balance = 0
        chars: list[str] = []
        for char in result:
            if char == opening:
                balance += 1
                chars.append(char)
            elif char == closing:
                if balance:
                    balance -= 1
                    chars.append(char)
            else:
                chars.append(char)
        while balance:
            for index in range(len(chars) - 1, -1, -1):
                if chars[index] == opening:
                    chars.pop(index)
                    balance -= 1
                    break
        result = "".join(chars)
    return result


def _paragraphize_prose(text: str, max_chars: int = 480) -> str:
    """Split model wall-of-text paragraphs at sentence boundaries."""
    paragraphs: list[str] = []
    for raw in re.split(r"\n+", str(text or "")):
        raw = raw.strip()
        if not raw:
            continue
        units = [
            item.strip()
            for item in re.findall(r"[^。！？!?]+[。！？!?]?", raw)
            if item.strip()
        ]
        current = ""
        for unit in units:
            if current and len(current) + len(unit) > max_chars:
                paragraphs.append(current)
                current = unit
            else:
                current += unit
        if current:
            paragraphs.append(current)
    return "\n\n".join(paragraphs).strip()


def _dedupe_exact_paragraphs(text: str) -> str:
    """Keep the first occurrence of an exactly repeated substantive paragraph."""
    seen: set[str] = set()
    kept: list[str] = []
    for paragraph in re.split(r"\n+", str(text or "")):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        key = _normalize_prose_unit(paragraph)
        if len(key) > 18 and key in seen:
            continue
        seen.add(key)
        kept.append(paragraph)
    return "\n\n".join(kept).strip()


def _dedupe_repeated_sentences(text: str) -> str:
    """Remove substantive exact sentence loops even when scenes separate them."""
    seen: set[str] = set()
    seen_long: list[str] = []
    kept: list[str] = []
    for unit in re.findall(r"[^。！？!?]+[。！？!?]?", str(text or "")):
        unit = unit.strip()
        if not unit:
            continue
        key = re.sub(r"[“”‘’\"']", "", _normalize_prose_unit(unit))
        if len(key) >= 18:
            if key in seen:
                continue
            if any(
                min(len(key), len(prior)) >= 22
                and _planning_similarity(key, prior) >= 0.84
                for prior in seen_long[-80:]
            ):
                continue
        if len(key) >= 18:
            seen.add(key)
            seen_long.append(key)
        kept.append(unit)
    return "".join(kept).strip()


def _clean_repair_continuation(existing: str, continuation: str) -> str:
    """Remove echoed suffixes and immediate loops from a repair continuation."""
    base = str(existing or "").rstrip()
    extra = str(continuation or "").lstrip()
    if not extra:
        return ""

    # Strip the longest exact suffix/prefix echo.  Limit the scan so very long
    # chapters do not make repair quadratic in their entire size.
    for _ in range(3):
        max_overlap = min(len(base), len(extra), 1200)
        stripped = False
        for size in range(max_overlap, 23, -1):
            if base[-size:] == extra[:size]:
                extra = extra[size:].lstrip()
                stripped = True
                break
        if not stripped or not extra:
            break

    return _dedupe_adjacent_sentence_blocks(extra)


@app.post("/api/generate")
async def generate(body: GenerateRequest) -> StreamingResponse:
    project = ensure_project_defaults(body.project)
    target_chars = _resolved_prose_target(project, body)
    prose_settings = _effective_prose_settings(project.get("settings", {}), target_chars)
    prompt_project = deepcopy(project)
    prompt_project["settings"] = prose_settings
    request = body.model_dump()
    request["project"] = prompt_project
    _attach_indexed_retrieval(prompt_project, request)
    build = build_prompt(prompt_project, request)
    context_snapshot = _persist_context_snapshot(
        prompt_project, request, build, reason="generation"
    )

    async def events():
        meta = {
            "type": "meta",
            "estimated_tokens": build.estimated_tokens,
            "activated_lore": [item.get("title", "") for item in build.activated_lore],
            "activated_skills": [
                f"{item.get('scope', '')}:{item.get('name', '')}"
                for item in build.activated_skills
            ],
            "retrieved_memories": [
                f"{hit.kind}:{hit.title}" for hit in build.retrieved_memories
            ],
            "budget_warnings": build.budget_warnings,
            "target_chars": target_chars,
            "effective_max_tokens": prose_settings.get("max_tokens", 3500),
            "context_snapshot_id": (
                context_snapshot.get("id", "") if context_snapshot else ""
            ),
            "prompt_hash": (
                context_snapshot.get("prompt_hash", "") if context_snapshot else ""
            ),
        }
        yield f"data: {json.dumps(meta, ensure_ascii=False)}\n\n"
        pieces: list[str] = []
        repaired = False
        try:
            async for piece in chat_stream(prose_settings, build.messages):
                pieces.append(piece)
                event = {"type": "token", "text": piece}
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            text = "".join(pieces).strip()
            actual = _prose_char_count(text)
            # Keep the generator slightly stricter than the local quality gate
            # (which warns below 75%). Otherwise a draft could finish generation
            # and immediately fail its own deterministic length audit.
            lower = max(80, int(target_chars * (0.82 if body.mode != "rewrite" else 0.62)))

            # Up to two conservative continuation passes. Qwen-class small models
            # occasionally answer a 1200-char chapter request with a polished
            # 150-300-char mini-scene; one continuation can still remain too short.
            # We never discard/rewrite streamed text: every pass continues from the
            # exact accepted prefix, and we stop if the model makes negligible
            # progress to avoid loops/cost explosions.
            for repair_pass in range(1, 3):
                needs_length_repair = body.mode != "rewrite" and actual < lower
                needs_closure_repair = actual >= 80 and _prose_looks_truncated(text)
                if not text or not (needs_length_repair or needs_closure_repair):
                    break
                remaining = max(120, target_chars - actual)
                chapter = next(
                    (
                        item for item in prompt_project.get("chapters", [])
                        if item.get("id") == body.chapter_id
                    ),
                    {},
                )
                chapter_plan = (
                    chapter.get("plan", {})
                    if isinstance(chapter.get("plan"), dict)
                    else {}
                )
                chapter_route = (
                    chapter.get("route", {})
                    if isinstance(chapter.get("route"), dict)
                    else {}
                )
                unresolved_ending = str(
                    chapter_plan.get("ending_hook")
                    or chapter_plan.get("exit_state")
                    or chapter_route.get("ending_hook")
                    or ""
                ).strip()
                continuation_anchor = re.sub(r"\s+", " ", text[-180:]).strip()
                repair_messages = [dict(message) for message in build.messages]
                repair_messages.extend(
                    [
                        {"role": "assistant", "content": text},
                        {
                            "role": "user",
                            "content": (
                                f"这是第 {repair_pass} 次续补。当前正文约 {actual} 个中文字符，"
                                f"目标约 {target_chars} 字，还需要约 {remaining} 字。"
                                "不要重写、不要回顾、不要把已经发生的动作换词再写、不要解释。"
                                "必须从上文最后一个动作或句子之后直接继续；首句不得重新介绍"
                                "人物、地点、任务、简牍或已经发现的证据。"
                                f"接续锚点（只能写其后的新动作）：【{continuation_anchor}】。"
                                + (
                                    f"尚需交付的章末状态是：【{unresolved_ending}】。"
                                    "若上文尚未到达它，就沿当前因果完成它；若已经到达，"
                                    "只写它造成的即时新状态。"
                                    if unresolved_ending
                                    else ""
                                )
                                + "保持人物、地点、时间、视角和既有事实不变；"
                                "不得新增未经上下文支持的往事。继续推进当前场景，加入具体动作、"
                                "对话或可感知细节，并自然收束在完整句子上。"
                            ),
                        },
                    ]
                )
                repair_settings = _effective_prose_settings(prose_settings, remaining)
                repair_settings["max_tokens"] = min(
                    int(repair_settings.get("max_tokens", 3500)),
                    max(900, int(math.ceil(remaining * 1.6)) + 240),
                )
                repair_meta = {
                    "type": "repair",
                    "pass": repair_pass,
                    "reason": "short" if needs_length_repair else "truncated",
                    "current_chars": actual,
                    "target_chars": target_chars,
                }
                yield f"data: {json.dumps(repair_meta, ensure_ascii=False)}\n\n"
                before = actual
                repair_pieces: list[str] = []
                async for piece in chat_stream(repair_settings, repair_messages):
                    repair_pieces.append(piece)
                continuation = _clean_repair_continuation(
                    text, "".join(repair_pieces)
                )
                if continuation:
                    pieces.append(continuation)
                    event = {"type": "token", "text": continuation}
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                repaired = True
                text = "".join(pieces).strip()
                actual = _prose_char_count(text)
                if actual - before < 40:
                    break

            if actual < 80:
                event = {
                    "type": "error",
                    "message": f"模型返回正文过短（约 {actual} 字），请重试。",
                }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                return

            warning = ""
            if body.mode != "rewrite" and actual < int(target_chars * 0.75):
                warning = f"生成完成但仍偏短：约 {actual}/{target_chars} 字，建议重试或继续补写。"
            done = {
                "type": "done",
                "actual_chars": actual,
                "target_chars": target_chars,
                "length_repaired": repaired,
                "warning": warning,
            }
            yield f"data: {json.dumps(done, ensure_ascii=False)}\n\n"
        except Exception as exc:
            event = {"type": "error", "message": planning_exception_detail(exc)}
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


def _director_event(task: dict[str, Any], message: str, kind: str = "info") -> None:
    task.setdefault("events", []).append(
        {"time": utc_now(), "kind": kind, "message": message}
    )
    task["events"] = task["events"][-100:]
    task["message"] = message


def _save_director_task(
    task: dict[str, Any], message: str | None = None, kind: str = "info"
) -> dict[str, Any]:
    if message:
        _director_event(task, message, kind)
    return store.save_director_task(task["id"], task)


def _record_planning_debt(
    task: dict[str, Any], debt: dict[str, Any]
) -> None:
    debts = task.setdefault("planning_debts", [])
    if not isinstance(debts, list):
        debts = []
        task["planning_debts"] = debts
    identity = (
        str(debt.get("phase", "")),
        str(debt.get("volume_id", "")),
        int(debt.get("chapter", 0) or 0),
    )
    debts[:] = [
        item for item in debts
        if (
            str(item.get("phase", "")),
            str(item.get("volume_id", "")),
            int(item.get("chapter", 0) or 0),
        ) != identity
    ]
    debts.append({"time": utc_now(), **debt})
    task["planning_debts"] = debts[-120:]


def _director_project_from_option(
    project: dict[str, Any], option: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    project = ensure_project_defaults(project)
    project["title"] = str(option.get("title") or "AI 自动创作小说")
    project["genre"] = str(option.get("genre", ""))
    project["story_mode"] = config.get("story_mode", "long")
    project["premise"] = str(option.get("premise", ""))
    project["outline"] = str(option.get("outline", ""))
    project["author_intent"] = str(option.get("author_intent", ""))
    project["current_focus"] = str(
        option.get("current_focus") or option.get("first_arc", "")
    )
    project["book_rules"] = "\n".join(
        str(item) for item in option.get("book_rules", []) if str(item).strip()
    )
    project["production_spec"] = str(config.get("seed", "")).strip()
    narrative = project["narrative"]
    narrative.update(
        {
            "target_chapters": int(config.get("target_chapters", 30)),
            "central_question": str(option.get("central_question", "")),
            "ending_direction": str(option.get("ending_direction", "")),
            "current_arc": str(option.get("first_arc", "")),
            "pov": (
                option.get("pov")
                if option.get("pov")
                in {"auto", "first", "third_limited", "omniscient"}
                else "auto"
            ),
            "tone": str(option.get("tone", "")),
        }
    )
    project["settings"]["target_words"] = int(config.get("target_words", 1200))
    project["characters"] = []
    for character_index, item in enumerate(option.get("characters", [])):
        if not isinstance(item, dict):
            continue
        project["characters"].append(
            {
                "id": str(uuid.uuid4()),
                "name": str(item.get("name", "")),
                "role": str(item.get("role", "")),
                "aliases": item.get("aliases", []) if isinstance(item.get("aliases"), list) else [],
                "importance": "main" if character_index == 0 else "supporting",
                "active": True,
                "description": str(item.get("description", "")),
                "personality": str(item.get("personality", item.get("description", ""))),
                "appearance": str(item.get("appearance", "")),
                "values": str(item.get("values", "")),
                "fears": str(item.get("fears", "")),
                "contradictions": str(item.get("contradictions", "")),
                "mannerisms": str(item.get("mannerisms", "")),
                "relationships": str(item.get("relationships", "")),
                "arc": str(item.get("arc", "")),
                "hard_limits": str(item.get("hard_limits", "")),
                "goal": str(item.get("goal", "")),
                "state": str(item.get("state", "")),
                "knowledge": str(item.get("knowledge", "")),
                "secrets": str(item.get("secrets", "")),
                "voice": str(item.get("voice", "")),
                "dialogue_examples": item.get("dialogue_examples", []) if isinstance(item.get("dialogue_examples"), list) else [],
                "location": "",
                "items": "",
                "emotion": "",
            }
        )
    project["world_entries"] = []
    for index, item in enumerate(option.get("world_entries", [])):
        if not isinstance(item, dict):
            continue
        project["world_entries"].append(
            {
                "id": str(uuid.uuid4()),
                "title": str(item.get("title", "")),
                "category": str(item.get("category", "世界设定")),
                "keys": [str(key) for key in item.get("keys", [])],
                "secondary_keys": [],
                "selective_logic": "and_any",
                "content": str(item.get("content", "")),
                "canon": str(item.get("canon", "hard" if item.get("constant") else "soft")),
                "position": "after",
                "order": 20 + index * 10,
                "constant": bool(item.get("constant", False)),
                "enabled": True,
                "match": "any",
                "character_names": [],
                "chapter_start": 0,
                "chapter_end": 0,
                "inclusion_group": "",
                "non_recursable": False,
                "prevent_recursion": False,
                "delay_until_recursion": False,
            }
        )
    project["chapters"] = [
        {
            "id": str(uuid.uuid4()),
            "title": "第一章",
            "summary": "",
            "content": "",
            "scene_goal": str(option.get("opening_hook", "")),
            "author_note": "",
            "plan": {
                "goal": str(option.get("opening_hook", "")),
                "conflict": str(option.get("central_conflict", "")),
                "must_keep": [],
                "must_avoid": [],
                "turning_point": "",
                "ending_hook": "",
            },
        }
    ]
    project["planning"] = {"version": 1, "master": {}, "volumes": []}
    project["memory"] = {
        "state_version": 4,
        "epistemic_schema_version": 1,
        "story_so_far": "",
        "story_digest_candidate": {},
        "facts": [],
        "plot_threads": [],
        "timeline": [],
        "relationships": [],
        "continuity_notes": [],
        "description_ledger": [],
        "commits": [],
    }
    return ensure_project_defaults(project)


def _memory_key(value: Any) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’—…（）()]", "", str(value or "")).casefold()


def _memory_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _chapter_number(project: dict[str, Any], chapter: dict[str, Any]) -> int:
    return next(
        (
            index + 1
            for index, item in enumerate(project.get("chapters", []))
            if item.get("id") == chapter.get("id")
        ),
        1,
    )


def _verified_evidence(content: str, value: Any) -> tuple[str, bool]:
    """Validate a model-produced evidence quote against accepted prose."""
    evidence = str(value or "").strip()[:240]
    if not evidence:
        return "", False
    return evidence, _memory_key(evidence) in _memory_key(content)


def _string_list(value: Any, limit: int = 12) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,，\n]", value)
    if not isinstance(value, list):
        return []
    return list(
        dict.fromkeys(str(item).strip() for item in value if str(item).strip())
    )[:limit]


def _relationship_key(left: Any, right: Any) -> str:
    pair = sorted((_memory_key(left), _memory_key(right)))
    return "|".join(pair)


def _apply_director_memory(
    project: dict[str, Any],
    chapter: dict[str, Any],
    result: dict[str, Any],
    *,
    rebuild_current_projection: bool = False,
) -> list[str]:
    warnings: list[str] = []
    number = _chapter_number(project, chapter)
    latest_settled_number = max(
        (
            index
            for index, item in enumerate(project.get("chapters", []), start=1)
            if isinstance(item, dict)
            and (
                str(item.get("memory_status", "")) == "committed"
                or bool(item.get("settlement"))
            )
        ),
        default=0,
    )
    historical_replay = (
        number < latest_settled_number and not rebuild_current_projection
    )
    accepted_content = str(chapter.get("content", ""))
    chapter["summary"] = str(result.get("summary") or chapter.get("summary", ""))
    if isinstance(chapter.get("route"), dict):
        chapter["route"]["status"] = "written"
    for volume in project.get("planning", {}).get("volumes", []):
        for route in volume.get("chapters", []):
            if route.get("id") == chapter.get("route_id"):
                route["status"] = "written"
    memory = project.setdefault("memory", {})
    if result.get("story_so_far") and not historical_replay:
        memory["story_digest_candidate"] = {
            "text": str(result["story_so_far"])[:4000],
            "status": "candidate",
            "source_chapter_id": chapter.get("id", ""),
            "chapter_number": number,
            "created_at": utc_now(),
        }
    for update in result.get("character_updates", []):
        if not isinstance(update, dict):
            continue
        update_name = str(update.get("name", "")).strip()
        character = next(
            (
                item
                for item in project.get("characters", [])
                if str(item.get("name", "")).strip().casefold()
                == update_name.casefold()
            ),
            None,
        )
        if not character:
            if update_name:
                warnings.append(f"未回写未知人物“{update_name}”的状态，请人工确认人物名称")
            continue
        evidence, evidence_verified = _verified_evidence(
            accepted_content, update.get("evidence")
        )
        state_fields = ("state", "location", "items", "emotion", "appearance_state")
        has_state_delta = any(str(update.get(key, "")).strip() for key in state_fields)
        if has_state_delta and not evidence:
            warnings.append(
                f"人物“{update_name}”的状态变化没有正文证据，已跳过状态回写"
            )
        elif has_state_delta and not evidence_verified:
            warnings.append(
                f"人物“{update_name}”的状态证据未在正文找到，已跳过状态回写"
            )
        elif has_state_delta:
            last_state_number = _memory_int(
                character.get("last_state_chapter_number", 0)
            )
            may_project_current_state = (
                number >= last_state_number
                if last_state_number > 0
                else not historical_replay
            )
            if not may_project_current_state:
                has_state_delta = False
        if has_state_delta and evidence_verified:
            for key in state_fields:
                if update.get(key):
                    character[key] = str(update[key])
            character["last_state_chapter_number"] = number
            character["last_state_chapter_id"] = chapter.get("id", "")
        gain = str(update.get("knowledge_gain", "")).strip()
        knowledge_ledger = character.setdefault("knowledge_ledger", [])
        knowledge_keys = {
            _memory_key(item.get("text"))
            for item in knowledge_ledger
            if isinstance(item, dict) and item.get("active", True)
        }
        gains = update.get("knowledge_gains", [])
        if not isinstance(gains, list):
            gains = []
        if gain and not gains and evidence_verified:
            gains = [
                {
                    "text": gain,
                    "learned_how": "本章（兼容字段，方式未细分）",
                    "certainty": "confirmed",
                    "evidence": evidence,
                }
            ]
        elif gain and not gains:
            warnings.append(
                f"人物“{update_name}”的新增知情“{gain[:24]}”没有可核验正文证据，未写入"
            )
        for knowledge in gains:
            if not isinstance(knowledge, dict):
                continue
            text = str(knowledge.get("text", "")).strip()[:800]
            key = _memory_key(text)
            if not key or key in knowledge_keys:
                continue
            quote, verified = _verified_evidence(
                accepted_content, knowledge.get("evidence")
            )
            if not quote:
                warnings.append(
                    f"人物“{update_name}”的新增知情“{text[:24]}”没有正文证据，未写入"
                )
                continue
            if not verified:
                warnings.append(
                    f"人物“{update_name}”的新增知情“{text[:24]}”证据未在正文找到，未写入"
                )
                continue
            knowledge_ledger.append(
                {
                    "id": str(uuid.uuid4()),
                    "text": text,
                    "learned_how": str(knowledge.get("learned_how", "亲历"))[:120],
                    "certainty": (
                        "suspected"
                        if str(knowledge.get("certainty", "confirmed")).lower()
                        == "suspected"
                        else "confirmed"
                    ),
                    "source_chapter_id": chapter["id"],
                    "source_chapter_title": chapter.get("title", ""),
                    "chapter_number": number,
                    "evidence": quote,
                    "active": True,
                    "related_fact_ids": [],
                }
            )
            knowledge_keys.add(key)
        character["knowledge_ledger"] = knowledge_ledger[-200:]
    facts = memory.setdefault("facts", [])
    fact_map = {_memory_key(item.get("text")): item for item in facts if isinstance(item, dict)}
    for item in result.get("facts", []):
        if not isinstance(item, dict) or not str(item.get("text", "")).strip():
            continue
        evidence, evidence_verified = _verified_evidence(
            accepted_content, item.get("evidence")
        )
        if not evidence:
            warnings.append(
                f"事实“{str(item.get('text', ''))[:28]}”没有正文证据，未写入权威事实"
            )
            continue
        if not evidence_verified:
            warnings.append(
                f"事实“{str(item.get('text', ''))[:28]}”的证据未在正文找到，未写入权威事实"
            )
            continue
        key = _memory_key(item["text"])
        supersedes_id = str(item.get("supersedes_id", "")).strip()
        if supersedes_id:
            superseded = next(
                (
                    fact for fact in facts
                    if isinstance(fact, dict) and fact.get("id") == supersedes_id
                ),
                None,
            )
            if superseded:
                superseded["active"] = False
                superseded["valid_until_chapter"] = number - 1
            else:
                warnings.append(
                    f"事实替换引用了不存在的 ID“{supersedes_id}”，旧事实未停用"
                )
        if key in fact_map:
            fact_map[key]["importance"] = min(
                5,
                max(
                    int(fact_map[key].get("importance", 3)),
                    int(item.get("importance", 3)),
                    1,
                ),
            )
            fact_map[key]["tags"] = list(
                dict.fromkeys(
                    _string_list(fact_map[key].get("tags"))
                    + _string_list(item.get("tags"))
                )
            )[:16]
            if evidence_verified:
                fact_map[key].update(
                    {
                        "evidence": evidence,
                        "evidence_verified": True,
                        "source_chapter_id": chapter["id"],
                        "source_chapter_title": chapter.get("title", ""),
                        "source_type": "accepted_chapter",
                        "reader_known": True,
                    }
                )
        else:
            added = {
                "id": str(uuid.uuid4()), "text": str(item["text"]),
                "tags": _string_list(item.get("tags"), 16),
                "importance": min(5, max(1, int(item.get("importance", 3)))), "active": True,
                "chapter_id": chapter["id"], "source_chapter_id": chapter["id"],
                "source_chapter_title": chapter.get("title", ""),
                "valid_from_chapter": number, "valid_until_chapter": 0,
                "confidence": (
                    "suspected"
                    if str(item.get("confidence", "confirmed")).lower() == "suspected"
                    else "confirmed"
                ),
                "visibility": str(item.get("visibility", "objective"))[:40],
                "known_by": [],
                "reader_known": True,
                "author_only": False,
                "evidence": evidence,
                "evidence_verified": evidence_verified,
                "source_type": "accepted_chapter",
                "supersedes_id": supersedes_id,
            }
            facts.append(added)
            fact_map[key] = added
    threads = memory.setdefault("plot_threads", [])
    for item in result.get("plot_threads", []):
        if not isinstance(item, dict) or not item.get("title"):
            continue
        raw_status = str(item.get("status", "open"))
        status = normalize_thread_status(raw_status)
        if raw_status.strip().casefold() not in {
            "open", "opened", "progressing", "reopened", "deferred", "ready", "closed",
            "resolved", "close", "已回收", "已解决", "advanced", "推进",
            "持续推进", "paused", "hold", "延后", "搁置", "payoff_ready", "可回收",
            "ready_for_payoff", "resolved_with_cost", "resolved_with_boundary",
            "resolved_as_process",
        }:
            warnings.append(f"线索“{item['title']}”返回了无效状态 {raw_status}，已改为 open")
        evidence, evidence_verified = _verified_evidence(
            accepted_content, item.get("evidence")
        )
        if not evidence:
            warnings.append(
                f"线索“{item['title']}”没有正文证据，本章线索更新已跳过"
            )
            continue
        if not evidence_verified:
            warnings.append(
                f"线索“{item['title']}”的推进证据未在正文找到，本章线索更新已跳过"
            )
            continue
        if status == "closed" and (not str(item.get("payoff", "")).strip() or not evidence_verified):
            warnings.append(
                f"线索“{item['title']}”缺少可核验回收结果，已降为 progressing"
            )
            status = "progressing"
        thread_id = str(item.get("thread_id") or item.get("id") or "").strip()
        old = next(
            (
                x for x in threads
                if (
                    thread_id and str(x.get("id", "")) == thread_id
                ) or _memory_key(x.get("title")) == _memory_key(item["title"])
            ),
            None,
        )
        if old:
            if _memory_int(old.get("last_advanced_chapter", 0)) > number:
                continue
            old.update(
                {
                    "status": status,
                    "latest": str(item.get("latest", old.get("latest", "")))[:1000],
                    "type": str(item.get("type", old.get("type", "mystery")))[:80],
                    "expected_payoff": str(
                        item.get("expected_payoff", old.get("expected_payoff", ""))
                    )[:1000],
                    "payoff_condition": str(
                        item.get("payoff_condition", old.get("payoff_condition", ""))
                    )[:1000],
                    "target_window": normalize_thread_timing(
                        item.get("target_window", old.get("target_window", "mid"))
                    ),
                    "stakeholders": list(
                        dict.fromkeys(
                            _string_list(old.get("stakeholders"))
                            + _string_list(item.get("stakeholders"))
                        )
                    )[:16],
                    "knowledge_holders": _string_list(
                        item.get("knowledge_holders", old.get("knowledge_holders", [])), 24
                    ),
                    "payoff": str(item.get("payoff", old.get("payoff", "")))[:1000],
                    "last_chapter_id": chapter["id"],
                    "last_advanced_chapter": number,
                    "evidence": evidence,
                    "evidence_verified": evidence_verified,
                }
            )
            if status == "closed":
                old["closed_chapter_number"] = number
        else:
            threads.append(
                {
                    "id": str(uuid.uuid4()),
                    "title": str(item["title"])[:200],
                    "type": str(item.get("type", "mystery"))[:80],
                    "status": status,
                    "setup": str(item.get("latest", ""))[:1000],
                    "latest": str(item.get("latest", ""))[:1000],
                    "expected_payoff": str(item.get("expected_payoff", ""))[:1000],
                    "payoff_condition": str(item.get("payoff_condition", ""))[:1000],
                    "target_window": normalize_thread_timing(item.get("target_window")),
                    "stakeholders": _string_list(item.get("stakeholders"), 16),
                    "knowledge_holders": _string_list(item.get("knowledge_holders"), 24),
                    "payoff": str(item.get("payoff", ""))[:1000],
                    "chapter_id": chapter["id"],
                    "last_chapter_id": chapter["id"],
                    "created_chapter_number": number,
                    "last_advanced_chapter": number,
                    "closed_chapter_number": number if status == "closed" else 0,
                    "evidence": evidence,
                    "evidence_verified": evidence_verified,
                }
            )
    timeline = memory.setdefault("timeline", [])
    event_keys = {f"{item.get('time')}|{item.get('event')}" for item in timeline if isinstance(item, dict)}
    for item in result.get("timeline", []):
        if not isinstance(item, dict) or not item.get("event"):
            continue
        evidence, evidence_verified = _verified_evidence(
            accepted_content, item.get("evidence")
        )
        if not evidence:
            warnings.append(
                f"时间线事件“{str(item.get('event', ''))[:28]}”没有正文证据，未写入"
            )
            continue
        if not evidence_verified:
            warnings.append(
                f"时间线事件“{str(item.get('event', ''))[:28]}”的证据未在正文找到，未写入"
            )
            continue
        key = f"{item.get('time')}|{item.get('event')}"
        if key not in event_keys:
            timeline.append(
                {
                    "id": str(uuid.uuid4()),
                    "time": str(item.get("time", "本章")),
                    "event": str(item["event"]),
                    "chapter_id": chapter["id"],
                    "chapter_number": number,
                    "participants": _string_list(item.get("participants"), 24),
                    "location": str(item.get("location", ""))[:240],
                    "causes": _string_list(item.get("causes"), 12),
                    "effects": _string_list(item.get("effects"), 12),
                    "evidence": evidence,
                    "evidence_verified": True,
                }
            )
            event_keys.add(key)
    relationships = memory.setdefault("relationships", [])
    relationship_map = {
        _relationship_key(item.get("left"), item.get("right")): item
        for item in relationships
        if isinstance(item, dict)
    }
    known_names = {
        str(item.get("name", "")).strip().casefold()
        for item in project.get("characters", [])
        if str(item.get("name", "")).strip()
    }
    for item in result.get("relationship_updates", []):
        if not isinstance(item, dict):
            continue
        left = str(item.get("left") or item.get("from") or "").strip()
        right = str(item.get("right") or item.get("to") or "").strip()
        if not left or not right or left.casefold() not in known_names or right.casefold() not in known_names:
            if left or right:
                warnings.append(f"关系更新“{left}—{right}”包含未知人物，已跳过")
            continue
        evidence, evidence_verified = _verified_evidence(
            accepted_content, item.get("evidence")
        )
        if not evidence:
            warnings.append(f"关系“{left}—{right}”没有正文证据，已跳过")
            continue
        if not evidence_verified:
            warnings.append(f"关系“{left}—{right}”的变化证据未在正文找到，已跳过")
            continue
        key = _relationship_key(left, right)
        relation = relationship_map.get(key)
        if relation and _memory_int(relation.get("last_chapter_number", 0)) > number:
            continue
        payload = {
            "left": left,
            "right": right,
            "state": str(item.get("state") or item.get("change") or "")[:1000],
            "tension": str(item.get("tension", ""))[:600],
            "trust": str(item.get("trust", ""))[:600],
            "knowledge_gap": str(item.get("knowledge_gap", ""))[:800],
            "active": True,
            "source_chapter_id": chapter["id"],
            "last_chapter_number": number,
            "evidence": evidence,
            "evidence_verified": evidence_verified,
        }
        if relation:
            relation.update(payload)
        else:
            relation = {"id": str(uuid.uuid4()), **payload}
            relationships.append(relation)
            relationship_map[key] = relation
    notes = memory.setdefault("continuity_notes", [])
    note_keys = {_memory_key(item.get("text")) for item in notes if isinstance(item, dict)}
    for value in result.get("continuity_notes", []) if not historical_replay else []:
        key = _memory_key(value)
        if key and key not in note_keys:
            notes.append({"id": str(uuid.uuid4()), "text": str(value), "resolved": False, "chapter_id": chapter["id"], "chapter_title": chapter.get("title", "")})
            note_keys.add(key)
    # Record only distinctive wording that can be verified in accepted prose.
    # This provides a precise anti-repetition memory without blacklisting normal
    # facts, names or necessary setting vocabulary.
    ledger = memory.setdefault("description_ledger", [])
    ledger_keys = {
        f"{_memory_key(item.get('character'))}|{_memory_key(item.get('aspect'))}|{_memory_key(item.get('phrase'))}"
        for item in ledger if isinstance(item, dict)
    }
    compact_content = _memory_key(chapter.get("content", ""))
    for item in result.get("description_updates", []):
        if not isinstance(item, dict):
            continue
        character = str(item.get("character", "")).strip()[:120]
        aspect = str(item.get("aspect", "")).strip()[:120]
        phrase = str(item.get("phrase", "")).strip()[:240]
        phrase_key = _memory_key(phrase)
        if len(phrase_key) < 8 or phrase_key not in compact_content:
            continue
        key = f"{_memory_key(character)}|{_memory_key(aspect)}|{phrase_key}"
        if key in ledger_keys:
            continue
        ledger.append({
            "id": str(uuid.uuid4()),
            "character": character,
            "aspect": aspect,
            "phrase": phrase,
            "chapter_id": chapter["id"],
            "chapter_title": chapter.get("title", ""),
        })
        ledger_keys.add(key)
    memory["description_ledger"] = ledger[-300:]
    # Only evidence-backed updates from an accepted chapter are projected into
    # the structured knowledge layer.  The graph remains a rebuildable index,
    # never an unreviewed model-generated source of truth.
    projected_knowledge_ids = project_accepted_memory_to_knowledge(project, chapter, result)
    memory["story_so_far"] = derive_story_so_far(project)
    scene_settlement = (
        result.get("scene_settlement")
        if isinstance(result.get("scene_settlement"), dict)
        else {}
    )
    chapter["settlement"] = {
        "state_version": 4,
        "chapter_number": number,
        "summary": chapter.get("summary", ""),
        "goal_achieved": str(scene_settlement.get("goal_achieved", ""))[:40],
        "irreversible_changes": _string_list(
            scene_settlement.get("irreversible_changes"), 12
        ),
        "open_questions": _string_list(scene_settlement.get("open_questions"), 12),
        "closing_state": str(scene_settlement.get("closing_state", ""))[:1200],
        "character_updates": deepcopy(result.get("character_updates", []))[:40]
        if isinstance(result.get("character_updates"), list)
        else [],
        "fact_ids": [
            item.get("id", "")
            for item in facts
            if isinstance(item, dict) and item.get("source_chapter_id") == chapter.get("id")
        ],
        "thread_ids": [
            item.get("id", "")
            for item in threads
            if isinstance(item, dict) and item.get("last_chapter_id") == chapter.get("id")
        ],
        "relationship_ids": [
            item.get("id", "")
            for item in relationships
            if isinstance(item, dict) and item.get("source_chapter_id") == chapter.get("id")
        ],
        "knowledge_ids": projected_knowledge_ids,
        "warnings": list(warnings),
    }
    result["warnings"] = warnings
    if warnings:
        execution = chapter.setdefault("execution", {})
        existing_warnings = [
            str(item) for item in execution.get("warnings", []) if str(item).strip()
        ]
        execution["warnings"] = list(dict.fromkeys(existing_warnings + warnings))[-20:]
        execution["last_memory_update_at"] = utc_now()
    else:
        execution = chapter.setdefault("execution", {})
        execution["warnings"] = []
        execution["last_memory_update_at"] = utc_now()
    return warnings


async def _director_generate_prose(
    task: dict[str, Any], project: dict[str, Any], chapter: dict[str, Any],
    *, mode: str = "instruction", instruction: str = "", selection: str = ""
) -> str:
    config = task["config"]
    project["settings"]["target_words"] = int(config["target_words"])
    safe_directive_markers = (
        "长度", "重复", "句式", "时代", "元话语", "疑似新增往事",
        "待确认新设定", "视角", "知识来源", "人物状态", "时间线",
        "目标偏离", "段落节奏",
    )
    adaptive = [
        str(item)
        for item in task.get("quality_directives", [])
        if str(item).strip()
        and any(marker in str(item) for marker in safe_directive_markers)
    ][-6:]
    if adaptive:
        instruction += (
            "\n【自动导演根据前章质量债生成的临时硬约束】\n- "
            + "\n- ".join(adaptive)
        )

    async def receive(settings: dict[str, Any], messages: list[dict[str, str]]) -> str:
        pieces: list[str] = []
        async for piece in chat_stream(settings, messages):
            pieces.append(piece)
            current = store.get_director_task(task["id"])
            if not current or current.get("status") != "running":
                raise asyncio.CancelledError()
        return "".join(pieces).strip()

    if project.get("settings", {}).get("director_scene_generation", False):
        plan = chapter.get("plan", {}) if isinstance(chapter.get("plan"), dict) else {}
        route = chapter.get("route", {}) if isinstance(chapter.get("route"), dict) else {}
        raw_contracts = route.get("scene_contracts", [])
        if isinstance(raw_contracts, list) and 3 <= len(raw_contracts) <= 8 and all(
            isinstance(item, dict) and str(item.get("boundary", "")).strip()
            for item in raw_contracts
        ):
            phases = [
                (
                    str(item.get("job", "完成当前场景合同")).strip(),
                    str(item.get("boundary", "")).strip(),
                    [str(beat).strip() for beat in item.get("beats", []) if str(beat).strip()][:8],
                )
                for item in raw_contracts
            ]
        else:
            phases = [
                (
                    "建立本章进入状态，只让人物开始当前任务",
                    str(route.get("goal") or plan.get("goal", "")).strip(),
                    [],
                ),
                (
                    "让主要阻力在现场具体发生，但不解决",
                    str(route.get("conflict") or plan.get("conflict", "")).strip(),
                    [],
                ),
                (
                    "让核验、对抗或选择推进到本章转折",
                    str(route.get("turning_point") or plan.get("turning_point", "")).strip(),
                    [],
                ),
                (
                    "只完成本章规定的退出状态并收束",
                    str(route.get("ending_hook") or plan.get("ending_hook", "")).strip(),
                    [],
                ),
            ]
        ending_text = str(route.get("ending_hook") or plan.get("ending_hook", ""))
        delayed_actions = [
            action
            for action in (
                "开仓", "署名", "下令", "交出", "烧毁", "放弃", "离开",
                "抵达", "处死", "杀死", "获得官印", "交还官印",
            )
            if action in ending_text
        ]
        partial = ""
        scene_target = max(
            320,
            min(
                900,
                int(project.get("settings", {}).get("director_scene_target_chars", 850) or 850),
            ),
        )
        for scene_index, (scene_job, scene_boundary, scene_beats) in enumerate(phases, start=1):
            scene_project = deepcopy(project)
            scene_chapter = next(
                item for item in scene_project.get("chapters", [])
                if item.get("id") == chapter.get("id")
            )
            scene_chapter["content"] = partial
            scene_instruction = (
                f"整章分场写作：当前是第 {scene_index}/{len(phases)} 段。{scene_job}。\n"
                f"本段唯一交付边界：【{scene_boundary}】。\n"
                f"本段写约 {scene_target} 个中文字符；只输出从现有末句之后发生的新正文，"
                "不得复述前文、不得重新介绍人物任务、不得新增往事或回忆。"
            )
            if scene_index < len(phases):
                scene_instruction += (
                    "当前不得越过本段边界，也不得猜测、提及或执行尚未提供的后段内容。"
                    "本段结束在能自然接续下一段的动作或压力上。"
                )
                if delayed_actions:
                    scene_instruction += (
                        "存在尚未提供的章末延迟动作；当前段不得猜测、命名或开始其物理步骤。"
                    )
            else:
                scene_instruction += (
                    "这是末段：准确交付章末状态后立即停止，不新增陌生人、新消息、"
                    "新线索或下一章事件。"
                )
                if delayed_actions:
                    scene_instruction += (
                        "此前延迟的章末动作现在必须在场完成："
                        + "、".join(delayed_actions)
                        + "。"
                    )
            # Whole-chapter revision feedback can name a required ending event.
            # Broadcasting it into every isolated scene defeats the phase wall
            # and makes small models execute the ending in scene one. The route
            # remains authoritative; audit feedback is enforced by rebuilding
            # all four route phases and re-auditing the finished chapter.
            scene_request = {
                "project": scene_project,
                "chapter_id": chapter["id"],
                "mode": "continue" if partial else "instruction",
                "instruction": scene_instruction,
                "selection": "",
                "target_words": scene_target,
            }
            _attach_indexed_retrieval(scene_project, scene_request)
            scene_build = build_prompt(scene_project, scene_request)
            scene_authority = "\n".join([scene_boundary, *scene_beats])
            compact_query = "\n".join(
                [scene_authority, partial[-1800:], scene_instruction]
            )
            scene_memories = retrieve_memories(
                scene_project,
                compact_query,
                _chapter_number(scene_project, scene_chapter) - 1,
                8,
                indexed_hits=scene_request.get("_indexed_memory_hits", []),
            )
            active_names = {
                str(item.get("name", "")).strip()
                for item in scene_project.get("characters", [])
                if str(item.get("name", "")).strip()
                and str(item.get("name", "")).strip() in compact_query
            }
            main_name = next(
                (
                    str(item.get("name", "")).strip()
                    for item in scene_project.get("characters", [])
                    if item.get("active", True)
                    and str(item.get("importance", "supporting")) == "main"
                    and str(item.get("name", "")).strip()
                ),
                "",
            )
            if main_name:
                active_names.add(main_name)
            character_lines = []
            for item in scene_project.get("characters", []):
                name = str(item.get("name", "")).strip()
                if not name or name not in active_names or not item.get("active", True):
                    continue
                character_lines.append(
                    f"【{name}】身份：{item.get('role', '')}；"
                    f"人格：{item.get('personality', '')}；"
                    f"不可写偏：{item.get('hard_limits', '')}；"
                    f"当前状态：{item.get('state', '')}；"
                    f"当前知情：{item.get('knowledge_baseline', item.get('knowledge', ''))}；"
                    f"语言：{item.get('voice', '')}"
                )
            lore_lines = []
            folded_query = compact_query.casefold()
            for entry in scene_project.get("world_entries", []):
                if not isinstance(entry, dict) or not entry.get("enabled", True):
                    continue
                keys = entry.get("keys", [])
                if isinstance(keys, str):
                    keys = re.split(r"[,，\n]", keys)
                if entry.get("constant", False) or any(
                    str(key).strip().casefold() in folded_query
                    for key in keys if str(key).strip()
                ):
                    lore_lines.append(
                        f"【{entry.get('title', '')}】{entry.get('content', '')}"
                    )
                if len(lore_lines) >= 4:
                    break
            completed_boundaries = [
                f"第{number}段已经完成" for number in range(1, scene_index)
            ]
            visible_route_lines = [f"当前边界：{scene_boundary}"]
            if scene_beats:
                visible_route_lines.append(
                    "必须依次写出的动作：" + "；".join(scene_beats)
                )
            compact_system = (
                "你是严谨的中文历史小说作者，只续写当前分场正文。禁止解释任务、输出标题或规划标签。"
                "只写眼前可观察的动作、物件、对话和判断；禁止用‘想起、记得、曾经、上次、"
                "昨日见过’凭空制造往事。禁止现代技术/管理术语、全知剧透和无来源新人物。"
                "不得复述已经完成的动作、证据、问答或结论。路线中的所有数字必须逐字保持，"
                "不得缩写、换算、改成近似数或另造数字。禁用目光如炬、指节发白、空气凝固、"
                "命运齿轮、无人知道、更大的风暴。第三人称限知必须服从人物知情边界。"
                "不得新增能改变判断的证物、证词、书信、印鉴、血迹、布料、暗号或目击者；"
                "当前分场的有效证据只有权威路线明确写出的内容。"
            )
            compact_user = f"""【当前分场可见的权威路线】
{chr(10).join(visible_route_lines)}
整章级 must_keep 与 must_avoid 已由导演校验，本段不重复展示，避免诱发复述或提前写出后段事件。
本段允许使用的明确数量只有：{'、'.join(sorted(_number_phrases(scene_authority))) or '无'}；不得另造日期、时刻、车数、物数，也不得提前引用其他分场的数量。

【当前人物】
{chr(10).join(character_lines) or '只使用路线中已经点名的人物。'}
本段可出现的姓名只有：{'、'.join(sorted(active_names)) or '无'}；其他现场人物只能用已有职称，不得创造姓名。

【人物知情边界】
{render_epistemic_context(scene_project, _chapter_number(scene_project, scene_chapter) - 1, compact_query, active_names)}

【相关已发生记忆】
{bounded_excerpt(render_memories(scene_memories), 2400) or '尚无已接受章节记忆。'}

【当前触发世界规则】
{chr(10).join(lore_lines) or '无额外条目；不得自行补造制度与前史。'}

【前文最后接续锚点】
{partial[-360:] if partial else '尚未起笔。'}

【已完成、严禁换词重写】
{'；'.join(completed_boundaries) or '无。'}

【当前分场指令】
{scene_instruction}"""
            scene_build.messages = [
                {"role": "system", "content": compact_system},
                {"role": "user", "content": compact_user},
            ]
            scene_build.estimated_tokens = estimate_tokens(
                compact_system + "\n" + compact_user
            )
            scene_build.sections = [
                {
                    "name": "导演分场紧凑上下文",
                    "content": compact_user,
                    "priority": 100,
                    "tokens": estimate_tokens(compact_user),
                    "tokens_before": estimate_tokens(compact_user),
                    "tokens_after": estimate_tokens(compact_user),
                    "status": "included",
                    "selected": True,
                    "reason": "",
                    "role": "user",
                    "required": True,
                }
            ]
            _persist_context_snapshot(
                scene_project,
                scene_request,
                scene_build,
                reason=f"director_generation_scene_{scene_index}",
            )
            segment = await asyncio.wait_for(
                receive(scene_project["settings"], scene_build.messages),
                timeout=900,
            )
            violations = _scene_constraint_violations(
                segment, route, scene_index, len(phases), delayed_actions, scene_authority
            )
            if (
                _prose_char_count(segment) < 280
                or _prose_looks_truncated(segment)
                or violations
            ):
                retry_messages = [dict(item) for item in scene_build.messages]
                retry_messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上一回应未通过当前分场的硬校验，请完全重写本分场。"
                            f"校验原因：{'；'.join(violations) or '篇幅不足或句子未结束'}。"
                            "至少写到约320个中文字符；仍只执行当前分场边界，不复述前文、"
                            "不抢写后续、不解释。不得新增任何人名、数字、时刻、制度或道具。"
                        ),
                    }
                )
                segment = await asyncio.wait_for(
                    receive(scene_project["settings"], retry_messages),
                    timeout=900,
                )
            segment = _strip_nonfinal_scene_violations(
                segment, route, scene_index, len(phases), delayed_actions, scene_authority
            )
            if _prose_char_count(segment) < 320:
                top_up_messages = [dict(item) for item in scene_build.messages]
                top_up_messages.extend(
                    [
                        {"role": "assistant", "content": segment},
                        {
                            "role": "user",
                            "content": (
                                "只从上句之后续写当前分场约150至220个中文字符，"
                                "补足现场动作、物件反应或简短问答。不得复述，"
                                "不得新增姓名、数字、时刻、制度、道具或后段事件；"
                                "写完一个完整句子立即停止。"
                            ),
                        },
                    ]
                )
                top_up = await asyncio.wait_for(
                    receive(scene_project["settings"], top_up_messages),
                    timeout=900,
                )
                top_up = _clean_repair_continuation(segment, top_up)
                top_up = _strip_nonfinal_scene_violations(
                    top_up, route, scene_index, len(phases), delayed_actions, scene_authority
                )
                segment = (segment.rstrip() + "\n\n" + top_up.lstrip()).strip()
            if scene_index == len(phases):
                segment = _ensure_final_route_closure(
                    segment, route, delayed_actions
                )
            violations = _scene_constraint_violations(
                segment, route, scene_index, len(phases), delayed_actions, scene_authority
            )
            if violations:
                raise ValueError(
                    f"模型第 {scene_index} 段违反路线硬约束：{'；'.join(violations)}"
                )
            segment = _clean_repair_continuation(partial, segment)
            segment_authority = "\n".join(
                [
                    json.dumps(route, ensure_ascii=False),
                    json.dumps(plan, ensure_ascii=False),
                    "\n".join(character_lines),
                    render_memories(scene_memories),
                    str(scene_chapter.get("summary", "")),
                ]
            )
            segment = _strip_unsupported_recollections(
                _dedupe_adjacent_sentence_blocks(segment), segment_authority
            )
            # A scene is a semantic boundary, not an independent chapter.  Treat
            # this only as a non-empty-output guard: aggregate chapter length and
            # the later local/AI audit own development and pacing quality.  Safety
            # stripping may legitimately reduce a concise bridge scene heavily.
            minimum_scene_chars = 20
            if _prose_char_count(segment) < minimum_scene_chars:
                raise ValueError(
                    f"模型重试并补写后第 {scene_index} 段正文仍不足 {minimum_scene_chars} 字"
                )
            partial = (partial.rstrip() + "\n\n" + segment.lstrip()).strip()
        if len(partial) < 300:
            raise ValueError("模型返回的分场正文不足 300 字")
        return _dedupe_exact_paragraphs(
            _paragraphize_prose(
                _remove_orphan_chinese_quotes(
                    _dedupe_repeated_sentences(
                        _dedupe_adjacent_sentence_blocks(partial)
                    )
                )
            )
        )

    length_multiplier = max(
        1.0,
        min(
            1.6,
            float(project.get("settings", {}).get("director_length_prompt_multiplier", 1.0) or 1.0),
        ),
    )
    prompt_target = int(math.ceil(int(config["target_words"]) * length_multiplier))
    request = {
        "project": project, "chapter_id": chapter["id"], "mode": mode,
        "instruction": instruction, "selection": selection,
        "target_words": prompt_target,
    }
    if mode == "rewrite" and selection.strip():
        # Director revisions replace an entire chapter. Manual rewrite mode is
        # often used for a selected sentence/paragraph and intentionally keeps
        # the selection's scale; these are different operations.
        request["_full_chapter_rewrite"] = True
    _attach_indexed_retrieval(project, request)
    build = build_prompt(project, request)
    _persist_context_snapshot(project, request, build, reason="director_generation")
    text = await asyncio.wait_for(
        receive(project["settings"], build.messages), timeout=900
    )
    if len(text) < 100:
        raise ValueError("模型返回的正文不足 100 字")
    return text


def _audit_issues(result: dict[str, Any]) -> list[dict[str, Any]]:
    issues = []
    for source, values in (
        ("本地", result.get("local_checks", {}).get("issues", [])),
        ("AI", result.get("issues", [])),
    ):
        for item in values if isinstance(values, list) else []:
            if isinstance(item, dict):
                issues.append({**item, "source": source})
    return issues[:8]


def _systemic_quality_issues(result: dict[str, Any]) -> list[dict[str, Any]]:
    systemic_categories = {
        "跨章重复段落", "跨章重复长句", "重复段落", "重复内容",
        "剧情停滞", "章节目标偏离", "时代语汇失真", "人物状态", "时间线",
    }
    return [
        item
        for item in _audit_issues(result)
        if str(item.get("category", "")) in systemic_categories
        or any(
            marker in str(item.get("category", ""))
            for marker in ("跨章", "重复", "时间线", "阶段偏离", "目标偏离", "时代语汇")
        )
        or (
            str(item.get("severity", "")) == "high"
            and any(marker in str(item.get("category", "")) for marker in ("重复", "目标", "逻辑"))
        )
    ]


def _quality_directives(result: dict[str, Any]) -> list[str]:
    directives: list[str] = []
    issues = _audit_issues(result)
    local_issues = [item for item in issues if item.get("source") == "本地"]
    # AI audit may lower the score correctly while proposing a new future
    # foreshadowing or prop. Do not feed creative audit hallucinations into the
    # next draft when deterministic findings already give a sufficient brief.
    for issue in (local_issues or issues):
        category = str(issue.get("category", "问题"))
        suggestion = str(issue.get("suggestion", "")).strip()
        message = str(issue.get("message", "")).strip()
        directives.append(f"避免再次出现[{category}]：{suggestion or message}")
    return list(dict.fromkeys(directives))[:6]


def _director_manuscript_gate_failures(
    health: dict[str, Any], *, final: bool = False
) -> list[str]:
    """Translate the deterministic health report into production stop reasons.

    Intermediate checkpoints stop only on structural contamination that will
    become more expensive to repair later.  The final checkpoint additionally
    enforces release-level language and memory integrity requirements.
    """
    failures: list[str] = []
    if health.get("duplicate_titles"):
        failures.append("出现重复章名")
    if int(health.get("duplicate_passage_count", 0)):
        failures.append("出现跨章完全重复段落")
    if health.get("similar_chapters"):
        failures.append("出现高相似章节")
    if health.get("volume_progression_issues"):
        failures.append("分卷状态或场域发生结构性重复")
    if final:
        score = int(health.get("score", 0))
        if score < 82:
            failures.append(f"全稿健康分 {score}，低于发布线 82")
        memory_issues = health.get("memory_integrity_issues", [])
        if memory_issues:
            failures.append(f"仍有 {len(memory_issues)} 项记忆或线索完整性问题")
        total_jargon = sum(
            int(item.get("count", 0) or 0)
            for item in health.get("modern_jargon", [])
            if isinstance(item, dict)
        )
        character_count = int(health.get("character_count", 0) or 0)
        jargon_limit = max(12, character_count // 1500)
        if total_jargon > jargon_limit:
            failures.append(
                f"现代抽象术语共 {total_jargon} 次，超过发布线 {jargon_limit} 次"
            )
    return failures


async def _run_auto_director(task_id: str) -> None:
    task = store.get_director_task(task_id)
    if not task:
        return
    try:
        task["status"] = "running"
        task = _save_director_task(task, "自动导演已启动")
        config = task["config"]
        project = store.get(task["project_id"])
        if not project:
            raise ValueError("自动导演作品已被删除")

        if task.get("phase") == "incubator":
            brief = task.get("seed_brief")
            if not isinstance(brief, dict):
                task["incubator_step"] = "brief"
                task = _save_director_task(
                    task, "灵感开书 1/4：正在生成单套故事骨架"
                )
                brief, brief_warnings = await director_seed_brief(
                    project, config
                )
                task["seed_brief"] = brief
                task["incubator_step"] = "cast"
                for warning in brief_warnings:
                    _director_event(task, warning, "warning")
                task = _save_director_task(
                    task,
                    "故事骨架已保存；下一步生成精简人物名单",
                    "success",
                )
            cast_result = task.get("seed_cast")
            if not isinstance(cast_result, dict):
                task["incubator_step"] = "cast"
                task = _save_director_task(
                    task, "灵感开书 2/4：正在确定 3-5 名主要人物"
                )
                cast_result, cast_warnings = await director_seed_cast(
                    project, config, brief
                )
                task["seed_cast"] = cast_result
                task["incubator_step"] = "characters"
                for warning in cast_warnings:
                    _director_event(task, warning, "warning")
                task = _save_director_task(
                    task, "主要人物名单已保存；开始逐人建立人物卡", "success"
                )
            cast = cast_result.get("characters", [])

            cards = task.get("seed_character_cards")
            if not isinstance(cards, list):
                cards = []
            if len(cards) > len(cast):
                cards = cards[: len(cast)]
            for index in range(len(cards), len(cast)):
                target = cast[index]
                task["incubator_step"] = "characters"
                task = _save_director_task(
                    task,
                    f"灵感开书 3/4：正在生成人物卡 {index + 1}/{len(cast)}《{target.get('name', '')}》",
                )
                card, card_warnings = await director_character_card(
                    project, config, brief, cast, target
                )
                cards.append(card)
                task["seed_character_cards"] = cards
                for warning in card_warnings:
                    _director_event(task, warning, "warning")
                task = _save_director_task(
                    task,
                    f"人物卡《{card.get('name', '')}》已保存（{len(cards)}/{len(cast)}）",
                    "success",
                )

            world_result = task.get("seed_world")
            if not isinstance(world_result, dict):
                task["incubator_step"] = "world"
                task = _save_director_task(
                    task, "灵感开书 4/4：正在生成开篇必要世界书"
                )
                world_result, world_warnings = await director_seed_world(
                    project, config, brief, cast
                )
                task["seed_world"] = world_result
                for warning in world_warnings:
                    _director_event(task, warning, "warning")
                task = _save_director_task(task, "世界书已保存", "success")

            assets = {
                "characters": cards,
                "world_entries": world_result.get("world_entries", []),
            }
            task["seed_assets"] = assets
            task = _save_director_task(task)
            option = {
                **brief,
                "outline": str(brief.get("story_spine", "")),
                "target_chapters": int(config.get("target_chapters", 30)),
                "characters": assets.get("characters", []),
                "world_entries": assets.get("world_entries", []),
            }
            project = _director_project_from_option(project, option, config)
            project = store.save(project["id"], project, reason="director-incubator")
            task["phase"] = "master"
            task["incubator_step"] = "completed"
            task["selected_direction"] = {key: option.get(key, "") for key in ("title", "genre", "positioning", "premise")}
            task = _save_director_task(task, f"已采用推荐方向《{project['title']}》，开始全书规划", "success")

        if task.get("phase") == "master":
            project = store.get(task["project_id"])
            target = int(project.get("narrative", {}).get("target_chapters", config.get("target_chapters", 30)))
            configured_volumes = config.get("preferred_volume_count")
            requested_volumes = (
                min(target, int(configured_volumes))
                if configured_volumes
                else 1 if target <= 6 else min(12, max(2, math.ceil(target / 12)))
            )
            specs = _director_volume_specs(target, requested_volumes)

            bible = task.get("master_bible")
            if not isinstance(bible, dict):
                task["master_step"] = "bible"
                task = _save_director_task(task, "全书规划 1/3：正在建立故事圣经")
                bible, bible_warnings = await director_master_bible(
                    project, "自动导演模式：建立可直接分卷执行的故事圣经。"
                )
                task["master_bible"] = bible
                task["master_step"] = "contracts"
                for warning in bible_warnings:
                    _director_event(task, warning, "warning")
                task = _save_director_task(task, "全书故事圣经已保存", "success")

            contracts = task.get("master_contracts")
            if not isinstance(contracts, list):
                contracts = []
            if len(contracts) > len(specs):
                contracts = contracts[: len(specs)]
            for index in range(len(contracts), len(specs)):
                task["master_step"] = "contracts"
                task["current_volume"] = index + 1
                task["total_volumes"] = len(specs)
                task = _save_director_task(
                    task,
                    f"全书规划 2/3：正在建立第 {index + 1}/{len(specs)} 卷因果契约",
                )
                contract, contract_warnings = await director_master_contract(
                    project, bible, specs, index, contracts
                )
                contracts.append(contract)
                task["master_contracts"] = contracts
                for warning in contract_warnings:
                    _director_event(task, warning, "warning")
                task = _save_director_task(
                    task,
                    f"第 {index + 1} 卷因果契约已保存（{len(contracts)}/{len(specs)}）",
                    "success",
                )
            task["master_step"] = "volumes"
            task = _save_director_task(task, "全部分卷契约已保存；开始逐卷扩写", "success")

            expanded = task.get("master_volumes")
            if not isinstance(expanded, list):
                expanded = []
            if len(expanded) > len(specs):
                expanded = expanded[: len(specs)]
            for index in range(len(expanded), len(specs)):
                task["master_step"] = "volumes"
                task["current_volume"] = index + 1
                task["total_volumes"] = len(specs)
                saved_core = task.get("master_volume_core")
                if not isinstance(saved_core, dict) or int(saved_core.get("index", -1)) != index:
                    task.pop("master_volume_core", None)
                    task = _save_director_task(
                        task,
                        f"全书规划 3/3：正在生成第 {index + 1}/{len(specs)} 卷剧情梗概《{contracts[index].get('title', '')}》",
                    )
                    core, core_warnings = await director_master_volume_core(
                        project, bible, contracts, specs, index, expanded
                    )
                    task["master_volume_core"] = {"index": index, "data": core}
                    for warning in core_warnings:
                        _director_event(task, warning, "warning")
                    task = _save_director_task(
                        task,
                        f"第 {index + 1} 卷剧情梗概核心已保存；继续生成执行清单",
                        "success",
                    )
                else:
                    core = saved_core.get("data", {})
                task = _save_director_task(
                    task,
                    f"全书规划 3/3：正在生成第 {index + 1}/{len(specs)} 卷转折与人物弧",
                )
                details, detail_warnings = await director_master_volume_details(
                    project, bible, contracts, specs, index, core
                )
                contract_fields = {
                    key: contracts[index].get(key, "")
                    for key in (
                        "theme_test", "primary_arena", "time_span",
                        "irreversible_change", "character_choice", "new_story_question",
                    )
                }
                volume = {
                    **core,
                    **details,
                    **contract_fields,
                    "chapter_count": specs[index]["chapter_count"],
                }
                validate_director_volume_expansion({"volume": volume})
                expanded.append(volume)
                task["master_volumes"] = expanded
                task.pop("master_volume_core", None)
                for warning in detail_warnings:
                    _director_event(task, warning, "warning")
                task = _save_director_task(
                    task,
                    f"第 {index + 1} 卷详细蓝图已保存（{len(expanded)}/{len(specs)}）",
                    "success",
                )

            raw_planning = {
                **bible,
                "full_outline": _director_full_outline(expanded),
                "volumes": expanded,
            }
            outline_target = 700 if target <= 12 else 1200 if target <= 30 else 1800 if target <= 60 else 2600 if target <= 120 else 3200
            validate_master_result(
                raw_planning, requested_volumes, max(500, int(outline_target * 0.7)), target
            )
            planning = normalize_master_plan(raw_planning, target)
            planning["fallback"] = False
            planning["warnings"] = []
            project["planning"] = planning
            if planning.get("master", {}).get("full_outline"):
                project["outline"] = planning["master"]["full_outline"]
            project = store.save(project["id"], project, reason="director-master-plan")
            task["phase"] = "volumes"
            task["volume_index"] = 0
            task["total_volumes"] = len(planning.get("volumes", []))
            task["master_step"] = "completed"
            task = _save_director_task(task, f"全书规划完成，共 {task['total_volumes']} 卷", "success")

        if task.get("phase") == "volumes":
            project = store.get(task["project_id"])
            volumes = project.get("planning", {}).get("volumes", [])
            for index in range(int(task.get("volume_index", 0)), len(volumes)):
                volume = volumes[index]
                task["current_volume"] = index + 1
                expected = int(volume["chapter_end"]) - int(volume["chapter_start"]) + 1
                checkpoints = task.get("route_checkpoints")
                if not isinstance(checkpoints, dict):
                    checkpoints = {}
                existing = volume.get("chapters", [])
                if isinstance(existing, list) and len(existing) == expected:
                    valid_count, checkpoint_issue = _audit_route_checkpoint_prefix(
                        project, volume, existing
                    )
                    if valid_count == expected:
                        project = ensure_project_defaults(
                            apply_volume_routes(project, volume["id"])
                        )
                        project = store.save(
                            project["id"],
                            project,
                            reason=f"director-volume-{index + 1}-reapplied",
                        )
                        task["volume_index"] = index + 1
                        task = _save_director_task(
                            task,
                            f"检测到第 {index + 1} 卷路线完整且通过复核，已重新同步章节后继续",
                            "warning",
                        )
                        continue
                    checkpoints[volume["id"]] = existing[:valid_count]
                    project["planning"]["volumes"][index]["chapters"] = []
                    task["route_checkpoints"] = checkpoints
                    task.setdefault("route_rejected_history", []).append(
                        {
                            "time": utc_now(), "volume_id": volume["id"],
                            "from_chapter": int(volume["chapter_start"]) + valid_count,
                            "count": expected - valid_count, "reason": checkpoint_issue,
                        }
                    )
                    task["route_rejected_history"] = task["route_rejected_history"][-30:]
                    task = _save_director_task(
                        task,
                        f"第 {index + 1} 卷旧路线复核发现问题：{checkpoint_issue}；"
                        f"已保留前 {valid_count} 条并从下一条自动重建",
                        "warning",
                    )
                routes = checkpoints.get(volume["id"])
                if not isinstance(routes, list):
                    routes = []
                if len(routes) > expected:
                    routes = routes[:expected]
                valid_count, checkpoint_issue = _audit_route_checkpoint_prefix(
                    project, volume, routes
                )
                if valid_count < len(routes):
                    rejected_count = len(routes) - valid_count
                    task.setdefault("route_rejected_history", []).append(
                        {
                            "time": utc_now(), "volume_id": volume["id"],
                            "from_chapter": int(volume["chapter_start"]) + valid_count,
                            "count": rejected_count, "reason": checkpoint_issue,
                        }
                    )
                    task["route_rejected_history"] = task["route_rejected_history"][-30:]
                    routes = routes[:valid_count]
                    checkpoints[volume["id"]] = routes
                    task["route_checkpoints"] = checkpoints
                    task = _save_director_task(
                        task,
                        f"恢复前复核发现旧检查点问题：{checkpoint_issue}；已保留前 "
                        f"{valid_count} 条，从第 {valid_count + 1} 条自动重建",
                        "warning",
                    )
                for chapter_number in range(
                    int(volume["chapter_start"]) + len(routes),
                    int(volume["chapter_end"]) + 1,
                ):
                    task["route_volume_index"] = index + 1
                    task["route_chapter_number"] = chapter_number
                    task["route_completed"] = len(routes)
                    task["route_total"] = expected
                    task = _save_director_task(
                        task,
                        f"正在拆解第 {index + 1}/{len(volumes)} 卷《{volume.get('title', '')}》：第 {len(routes) + 1}/{expected} 条章节路线",
                    )
                    route, route_warnings = await director_plan_chapter_route(
                        project, volume, chapter_number, routes
                    )
                    routes.append(route)
                    checkpoints[volume["id"]] = routes
                    task["route_checkpoints"] = checkpoints
                    task["route_completed"] = len(routes)
                    route_issues = [
                        str(item) for item in route.get("quality_warnings", [])
                        if str(item).strip()
                    ]
                    if route_issues:
                        _record_planning_debt(
                            task,
                            {
                                "phase": "route", "volume_id": volume["id"],
                                "volume": index + 1, "chapter": chapter_number,
                                "title": route.get("title", ""), "issues": route_issues,
                            },
                        )
                    for warning in route_warnings:
                        _director_event(task, warning, "warning")
                    task = _save_director_task(
                        task,
                        f"第 {chapter_number} 章路线《{route.get('title', '')}》已保存（{len(routes)}/{expected}）",
                        "success",
                    )
                project["planning"]["volumes"][index]["chapters"] = routes
                project = ensure_project_defaults(apply_volume_routes(project, volume["id"]))
                project = store.save(project["id"], project, reason=f"director-volume-{index + 1}")
                checkpoints.pop(volume["id"], None)
                task["route_checkpoints"] = checkpoints
                task["volume_index"] = index + 1
                task = _save_director_task(task, f"第 {index + 1} 卷已拆解并建立章节", "success")
            task["phase"] = "chapters"
            task["chapter_index"] = int(task.get("chapter_index", 0))
            task["total_chapters"] = len(project.get("chapters", []))
            task = _save_director_task(task, f"分卷与逐章路线完成，开始创作 {task['total_chapters']} 章正文", "success")

        if task.get("phase") == "chapters":
            project = store.get(task["project_id"])
            chapters = project.get("chapters", [])
            for index in range(int(task.get("chapter_index", 0)), len(chapters)):
                chapter = project["chapters"][index]
                task["current_chapter"] = index + 1
                task = _save_director_task(task, f"第 {index + 1}/{len(chapters)} 章：正在细化《{chapter.get('title', '')}》")
                if chapter.get("content", "").strip():
                    task["chapter_index"] = index + 1
                    task = _save_director_task(task, "检测到已有正文，本章保持不变并跳过", "warning")
                    continue
                plan = await chapter_plan(
                    ChapterActionRequest(project=project, chapter_id=chapter["id"], instruction="自动导演模式：服从上层路线和已发生事实，只规划本章。")
                )
                if plan.get("fallback"):
                    issues = [str(item) for item in plan.get("warnings", []) if str(item).strip()]
                    _record_planning_debt(
                        task,
                        {
                            "phase": "chapter_plan", "chapter": index + 1,
                            "chapter_id": chapter.get("id", ""),
                            "title": chapter.get("title", ""),
                            "issues": issues or ["AI 单章细化失败，已使用上层章节路线继续"],
                        },
                    )
                    _director_event(
                        task,
                        f"第 {index + 1} 章 AI 细化未完成，已使用上层路线继续并记录规划债务",
                        "warning",
                    )
                chapter["plan"] = plan
                chapter["scene_goal"] = plan.get("goal", chapter.get("scene_goal", ""))
                project = store.save(project["id"], project, reason=f"director-chapter-{index + 1}-plan")
                chapter = project["chapters"][index]
                task = _save_director_task(task, f"第 {index + 1} 章：正在生成正文")
                draft = await _director_generate_prose(
                    task, project, chapter,
                    instruction="严格执行本章计划，从具体场景起笔，完成本章目标、冲突、转折和结尾推动力。只输出小说正文。",
                )
                audit: dict[str, Any] = {}
                revision_attempt_limit = min(
                    int(config["max_revision_attempts"]),
                    max(
                        0,
                        int(
                            project.get("settings", {}).get(
                                "director_revision_attempt_limit",
                                config["max_revision_attempts"],
                            )
                        ),
                    ),
                )
                for attempt in range(revision_attempt_limit + 1):
                    task = _save_director_task(task, f"第 {index + 1} 章：正在进行连续性与质量审计（第 {attempt + 1} 次）")
                    audit = await chapter_audit(
                        ChapterActionRequest(project=project, chapter_id=chapter["id"], draft=draft, instruction="自动导演整章审计")
                    )
                    if audit.get("verdict") == "pass" and int(audit.get("score", 0)) >= int(config["quality_threshold"]):
                        break
                    if attempt >= revision_attempt_limit:
                        break
                    issues = _audit_issues(audit)
                    task["quality_directives"] = _quality_directives(audit)
                    requirements = "\n".join(
                        f"{number}. [{item.get('category', '问题')}] {item.get('message', '')}；建议：{item.get('suggestion', '')}"
                        for number, item in enumerate(issues, start=1)
                    ) or str(audit.get("revision_brief", "修复审计发现的问题"))
                    task = _save_director_task(task, f"第 {index + 1} 章：审计未通过，正在自动修订")
                    systemic = _systemic_quality_issues(audit)
                    if systemic:
                        draft = await _director_generate_prose(
                            task,
                            project,
                            chapter,
                            mode="instruction",
                            instruction=(
                                "上一候选存在结构性重复或阶段偏离，必须废弃原候选，从本章路线重新设计不同的场景动作链。"
                                "保留上层规定的章末新状态，但不得沿用原候选的段落、句式、对话问答或意象。\n"
                                + requirements
                            ),
                        )
                    else:
                        draft = await _director_generate_prose(
                            task, project, chapter, mode="rewrite", selection=draft,
                            instruction=f"完整修订候选正文并解决以下问题：\n{requirements}\n保留未被指出的问题、人物关系、事件顺序和结尾功能，只输出完整修订稿。",
                        )
                passed = audit.get("verdict") == "pass" and int(audit.get("score", 0)) >= int(config["quality_threshold"])
                if not passed:
                    debt = {"chapter": index + 1, "chapter_id": chapter.get("id", ""), "title": chapter.get("title", ""), "score": int(audit.get("score", 0)), "issues": _audit_issues(audit)}
                    debts = task.setdefault("quality_debts", [])
                    same_chapter_retry = bool(
                        debts
                        and str(debts[-1].get("chapter_id", ""))
                        == str(chapter.get("id", ""))
                    )
                    if same_chapter_retry:
                        debts[-1] = debt
                    else:
                        debts.append(debt)
                    systemic = _systemic_quality_issues(audit)
                    if same_chapter_retry:
                        task["consecutive_quality_debts"] = 1
                        task["consecutive_systemic_debts"] = 1 if systemic else 0
                    else:
                        task["consecutive_quality_debts"] = int(task.get("consecutive_quality_debts", 0)) + 1
                        task["consecutive_systemic_debts"] = (
                            int(task.get("consecutive_systemic_debts", 0)) + 1 if systemic else 0
                        )
                    task["quality_directives"] = _quality_directives(audit)
                    task["last_rejected_candidate"] = {
                        "chapter": index + 1,
                        "chapter_id": chapter.get("id", ""),
                        "title": chapter.get("title", ""),
                        "draft": draft[:30000],
                        "audit": audit,
                        "rejected_at": utc_now(),
                    }
                    if int(task.get("consecutive_systemic_debts", 0)) >= 3 or int(task.get("consecutive_quality_debts", 0)) >= 5:
                        task["status"] = "paused"
                        task["checkpoint_message"] = (
                            f"已连续 {task.get('consecutive_quality_debts', 0)} 章未达标；"
                            "系统已触发质量熔断，当前低质量候选未写入正文。"
                        )
                        _save_director_task(
                            task,
                            "连续质量债表明规划或提示词存在系统性问题，已在写入本章前暂停；请先查看全稿体检后从检查点继续",
                            "warning",
                        )
                        return
                    if not config.get("continue_on_quality_debt", True):
                        task["status"] = "paused"
                        _save_director_task(task, f"第 {index + 1} 章连续修订后仍未达标，已暂停等待人工接管", "warning")
                        return
                else:
                    task["consecutive_quality_debts"] = 0
                    task["consecutive_systemic_debts"] = 0
                    task["quality_directives"] = []
                chapter["content"] = draft
                memory = await chapter_memory(
                    ChapterActionRequest(project=project, chapter_id=chapter["id"], draft=draft, instruction="自动导演状态回灌")
                )
                if isinstance(memory, dict) and memory.get("fallback"):
                    _record_planning_debt(
                        task,
                        {
                            "phase": "memory", "chapter": index + 1,
                            "chapter_id": chapter.get("id", ""),
                            "title": chapter.get("title", ""),
                            "issues": [str(item) for item in memory.get("warnings", []) if str(item).strip()]
                            or ["AI 记忆提取失败，已使用本地记忆回灌"],
                        },
                    )
                    _director_event(
                        task,
                        f"第 {index + 1} 章使用本地记忆回灌，正文流水线继续运行",
                        "warning",
                    )
                _apply_director_memory(project, chapter, memory)
                run_record = {
                    "status": "accepted" if passed else "quality_debt",
                    "last_run_at": utc_now(),
                    "model": str(project.get("settings", {}).get("model", "")),
                    "target_words": int(config["target_words"]),
                    "audit_score": int(audit.get("score", 0)),
                    "audit_verdict": str(audit.get("verdict", "review")),
                    "revision_attempts": int(attempt),
                    "issues": _audit_issues(audit),
                    "warnings": list(memory.get("warnings", []))
                    if isinstance(memory, dict)
                    else [],
                }
                chapter["execution"] = run_record
                chapter.setdefault("run_history", []).append(deepcopy(run_record))
                chapter["run_history"] = chapter["run_history"][-10:]
                project = store.save(project["id"], project, reason=f"director-chapter-{index + 1}-accepted")
                task["chapter_index"] = index + 1
                task["completed_chapters"] = index + 1
                task = _save_director_task(task, f"第 {index + 1} 章正文、审计与记忆回灌完成", "success" if passed else "warning")
                volume_ends = {
                    int(item.get("chapter_end", 0) or 0)
                    for item in project.get("planning", {}).get("volumes", [])
                    if isinstance(item, dict)
                }
                if len(chapters) >= 10 and (
                    index + 1 in volume_ends or (index + 1) % 10 == 0
                ):
                    health = manuscript_health_report(project)
                    task.setdefault("manuscript_health_history", []).append(
                        {
                            "chapter": index + 1,
                            "score": int(health.get("score", 0)),
                            "severity": str(health.get("severity", "high")),
                            "duplicate_passage_count": int(
                                health.get("duplicate_passage_count", 0)
                            ),
                            "similar_chapter_count": len(
                                health.get("similar_chapters", [])
                            ),
                            "volume_progression_issue_count": len(
                                health.get("volume_progression_issues", [])
                            ),
                            "memory_integrity_issue_count": len(
                                health.get("memory_integrity_issues", [])
                            ),
                        }
                    )
                    task["manuscript_health_history"] = task[
                        "manuscript_health_history"
                    ][-30:]
                    hard_failures = _director_manuscript_gate_failures(
                        health, final=index + 1 == len(chapters)
                    )
                    if hard_failures:
                        task["status"] = "paused"
                        task["checkpoint_message"] = (
                            f"第 {index + 1} 章全稿门禁未通过："
                            + "；".join(hard_failures)
                            + "。本章与记忆已保留，请修订后从检查点继续。"
                        )
                        task["latest_manuscript_health"] = health
                        _save_director_task(
                            task,
                            task["checkpoint_message"],
                            "warning",
                        )
                        return
                    task["latest_manuscript_health"] = health
                    task = _save_director_task(
                        task,
                        f"截至第 {index + 1} 章的全稿门禁通过（健康分 {health.get('score', 0)}）",
                        "success",
                    )
            task["phase"] = "completed"
            task["status"] = "completed"
            _save_director_task(task, f"《{project.get('title', '')}》全文创作完成，共 {len(chapters)} 章", "success")
    except asyncio.CancelledError:
        latest = store.get_director_task(task_id)
        if latest and latest.get("status") not in {"completed", "failed"}:
            latest["status"] = "paused"
            _save_director_task(latest, "自动导演已暂停，可从当前检查点继续", "warning")
        raise
    except Exception as exc:
        latest = store.get_director_task(task_id) or task
        latest["status"] = "paused"
        latest["failure_kind"] = "hard"
        latest["error"] = planning_exception_detail(exc)
        _save_director_task(
            latest,
            f"自动导演遇到无法安全降级的结构或连接故障并已暂停：{latest['error']}",
            "error",
        )
    finally:
        director_runners.pop(task_id, None)


def _launch_director(task_id: str) -> None:
    running = director_runners.get(task_id)
    if running and not running.done():
        return
    director_runners[task_id] = asyncio.create_task(_run_auto_director(task_id))


@app.post("/api/director/start")
async def director_start(body: AutoDirectorStartRequest) -> dict[str, Any]:
    source = ensure_project_defaults(body.source_project)
    store.backup(ROOT / "data" / "backups")
    created = store.create("AI 自动导演新作")
    created["settings"] = json.loads(json.dumps(source.get("settings", {}), ensure_ascii=False))
    created["style"] = json.loads(json.dumps(source.get("style", {}), ensure_ascii=False))
    created = store.save(created["id"], created, reason="director-created")
    config = {
        "seed": body.seed.strip(), "preferences": body.preferences.strip(),
        "story_mode": "short" if body.story_mode == "short" else "long",
        "target_chapters": int(body.target_chapters), "target_words": int(body.target_words),
        "preferred_volume_count": (
            int(body.preferred_volume_count) if body.preferred_volume_count else None
        ),
        "quality_threshold": int(body.quality_threshold),
        "max_revision_attempts": int(body.max_revision_attempts),
        "continue_on_quality_debt": bool(body.continue_on_quality_debt),
    }
    task = store.create_director_task(
        created["id"],
        {"phase": "incubator", "message": "等待自动导演启动", "config": config, "events": [], "completed_chapters": 0, "quality_debts": [], "planning_debts": []},
    )
    _launch_director(task["id"])
    return {"task": task, "project": created}


@app.get("/api/director/tasks/{task_id}")
async def director_task(task_id: str) -> dict[str, Any]:
    task = store.get_director_task(task_id)
    if not task:
        raise HTTPException(404, "自动导演任务不存在")
    return task


@app.get("/api/director/projects/{project_id}/latest")
async def director_latest(project_id: str) -> dict[str, Any]:
    task = store.latest_director_task(project_id)
    return task or {"status": "none", "project_id": project_id}


@app.post("/api/director/tasks/{task_id}/pause")
async def director_pause(task_id: str) -> dict[str, Any]:
    task = store.get_director_task(task_id)
    if not task:
        raise HTTPException(404, "自动导演任务不存在")
    if task.get("status") in {"completed", "failed"}:
        return task
    task["status"] = "paused"
    task = _save_director_task(task, "正在暂停自动导演…", "warning")
    runner = director_runners.get(task_id)
    if runner and not runner.done():
        runner.cancel()
    return task


@app.post("/api/director/tasks/{task_id}/resume")
async def director_resume(task_id: str) -> dict[str, Any]:
    task = store.get_director_task(task_id)
    if not task:
        raise HTTPException(404, "自动导演任务不存在")
    if task.get("status") == "completed":
        return task
    task["status"] = "queued"
    task.pop("error", None)
    task.pop("failure_kind", None)
    task = _save_director_task(task, "任务已进入恢复队列")
    _launch_director(task_id)
    return task

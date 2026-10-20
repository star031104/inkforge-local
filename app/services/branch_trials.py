"""分支试写: draft one key chapter along 2–3 different plot directions.

The author names the directions ("李斯当场翻脸" / "林砚选择沉默"); each branch
is a full chapter draft written through the normal chapter prompt
(``prompts.build_prompt`` + the stable-prefix preparation used by
``/api/generate``) with the direction injected as the author's instruction
for this run.  The chapter's current prose is hidden from the prompt because
a branch *replaces* the chapter rather than continuing it.

After drafting, a background job optionally audits each branch (the normal
chapter audit), runs blind pairwise judging between every pair of branches
(``candidate_selection.pairwise_judge``, both presentation orders) and makes
one structured call that maps every branch onto the remaining routes of the
volume ("对后续剧情的影响").

Job state (progress, partial branches) lives in the extension store, never in
the browser-edited project JSON.  The browser merges finished branches into
``chapter["branches"]`` (``merge_job_into_chapter``) and saves normally;
adopting a branch is always an explicit author action (``adopt_branch``).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime, timezone
from itertools import combinations
from typing import Any

from app import llama_client
from app.core.model_completion import StreamOutcome
from app.domain.json_schemas import object_schema
from app.domain.prose import (
    _clean_repair_continuation,
    _effective_prose_settings,
    _prose_char_count,
    _prose_looks_truncated,
)
from app.prompts import build_prompt
from app.services.antislop import apply_antislop, prose_stream_options
from app.services.candidate_selection import deterministic_score, judge_excerpt, pairwise_judge

JOB_KEY = "branch_job"
MIN_DIRECTIONS = 2
MAX_DIRECTIONS = 3
MAX_BRANCHES_PER_CHAPTER = 6
MAX_DIRECTION_CHARS = 300
MAX_FUTURE_ROUTES = 12
BRANCH_STATUSES = ("draft", "adopted", "discarded")
FINISHED = frozenset({"completed", "failed", "cancelled"})

Completion = Callable[..., Awaitable[tuple[dict[str, Any], list[str]]]]
GenerateBranch = Callable[[dict[str, Any], str, str], Awaitable[dict[str, Any]]]
AuditBranch = Callable[[dict[str, Any], str, str], Awaitable[dict[str, Any]]]

_STRING = {"type": "string"}
SUGGEST_SCHEMA = object_schema(
    {
        "directions": {
            "type": "array",
            "items": object_schema({"label": _STRING, "direction": _STRING, "why": _STRING}, ["label", "direction"]),
        }
    }
)
SUMMARY_SCHEMA = object_schema({"summary": _STRING}, ["summary"])
IMPACT_SCHEMA = object_schema(
    {
        "branches": {
            "type": "array",
            "items": object_schema(
                {
                    "id": _STRING,
                    "overall": _STRING,
                    "affected": {
                        "type": "array",
                        "items": object_schema(
                            {"number": {"type": "integer"}, "change": _STRING, "severity": _STRING},
                            ["number", "change"],
                        ),
                    },
                },
                ["id", "affected"],
            ),
        }
    }
)

SUGGEST_SYSTEM = (
    "你是长篇网文的剧情编辑。作者想在一个关键章节试写几种不同的剧情走向，再挑一种定稿。"
    "根据本章计划和前情，给出 3 个彼此真正不同的走向：至少一个顺着计划写、一个让主要人物做出相反选择、"
    "一个引入代价更高或更意外的结果。每个走向都要符合人物动机和世界规则，写成可以直接交给写手的一句指令。"
    "只输出 JSON：{\"directions\":[{\"label\":\"4-8字短名\",\"direction\":\"40字内的剧情走向指令\",\"why\":\"20字内看点\"}]}"
)
SUMMARY_SYSTEM = (
    "用 80 字以内概括这份章节草稿实际发生了什么：关键事件、人物选择、章末状态。只写正文里真的发生的事。"
    "只输出 JSON：{\"summary\":\"...\"}"
)
IMPACT_SYSTEM = (
    "你是长篇小说的分卷编辑。同一章试写了几个不同的剧情分支，你要判断：如果采用某个分支，"
    "本卷后面已经规划好的哪些章节路线必须修改，改什么。只列真正受影响的章节（路线里的前提、人物关系、"
    "信息知情或目标被这个分支改变了）；不受影响就不要列。severity 只能是 minor（措辞或细节调整）"
    "或 major（目标/冲突/转折必须重写）。"
    "只输出 JSON：{\"branches\":[{\"id\":\"分支编号\",\"overall\":\"30字内总体影响\","
    "\"affected\":[{\"number\":章号,\"change\":\"30字内需要怎么改\",\"severity\":\"minor或major\"}]}]}"
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def content_hash(text: Any) -> str:
    return hashlib.sha256(str(text or "").strip().encode("utf-8")).hexdigest()


def _clip(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def chapter_index(project: dict[str, Any], chapter_id: str) -> int:
    for index, chapter in enumerate(project.get("chapters", []) or []):
        if str(chapter.get("id", "")) == str(chapter_id):
            return index
    raise KeyError(chapter_id)


# ---------------------------------------------------------------- directions


def normalize_directions(directions: Any) -> list[dict[str, str]]:
    """Clean author input: 2–3 distinct, non-empty directions with labels."""
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in directions if isinstance(directions, list) else []:
        if isinstance(item, str):
            item = {"direction": item}
        if not isinstance(item, dict):
            continue
        direction = _clip(item.get("direction"), MAX_DIRECTION_CHARS)
        if not direction or direction in seen:
            continue
        seen.add(direction)
        label = _clip(item.get("label"), 20) or _clip(direction, 10)
        result.append({"label": label, "direction": direction})
    if len(result) < MIN_DIRECTIONS:
        raise ValueError("请至少填写 2 个不同的剧情走向")
    if len(result) > MAX_DIRECTIONS:
        raise ValueError("一次最多试写 3 个走向")
    return result


def branch_instruction(direction: str) -> str:
    """The direction as the highest-priority author instruction for this draft."""
    return (
        f"【分支试写·本章必须执行的剧情走向】{direction}\n"
        "这是作者为本章指定的剧情走向，优先级高于本章原计划中与之冲突的转折、结果和章末钩子；"
        "计划中不冲突的目标、人物、场景和伏笔照常完成。从本章开头写出完整一章，"
        "让这个走向由人物的选择推动并写出它带来的直接后果，不要写成梦境、假设或回忆，也不要在章末把它推翻。"
    )


def _chapter_brief(project: dict[str, Any], index: int) -> str:
    chapter = project["chapters"][index]
    plan = chapter.get("plan", {}) if isinstance(chapter.get("plan"), dict) else {}
    route = chapter.get("route", {}) if isinstance(chapter.get("route"), dict) else {}
    lines = [f"第{index + 1}章《{chapter.get('title', '')}》"]
    for label, key in (("目标", "goal"), ("冲突", "conflict"), ("转折", "turning_point"), ("章末", "ending_hook")):
        value = plan.get(key) or route.get(key)
        if value:
            lines.append(f"{label}：{_clip(value, 200)}")
    if chapter.get("scene_goal") and not (plan.get("goal") or route.get("goal")):
        lines.append(f"目标：{_clip(chapter.get('scene_goal'), 200)}")
    return "\n".join(lines)


def _recent_story(project: dict[str, Any], index: int, count: int = 3) -> str:
    chapters = project.get("chapters", [])
    return "\n".join(
        f"第{number + 1}章：{_clip(chapters[number].get('summary'), 160)}"
        for number in range(max(0, index - count), index)
        if str(chapters[number].get("summary", "")).strip()
    ) or "（无）"


async def suggest_directions(
    project: dict[str, Any], chapter_id: str, completion: Completion
) -> dict[str, Any]:
    index = chapter_index(project, chapter_id)
    user = (
        f"【作品】{project.get('title', '')}｜{project.get('genre', '')}\n"
        f"【前情】\n{_recent_story(project, index)}\n\n"
        f"【本章计划】\n{_chapter_brief(project, index)}"
    )
    result, warnings = await completion(
        project.get("settings", {}),
        [{"role": "system", "content": SUGGEST_SYSTEM}, {"role": "user", "content": user}],
        temperature=0.8,
        max_tokens=700,
        timeout_seconds=120,
        workload="planning",
        schema=SUGGEST_SCHEMA,
    )
    directions = []
    for item in result.get("directions", []) if isinstance(result.get("directions"), list) else []:
        if isinstance(item, dict) and _clip(item.get("direction"), 10):
            directions.append({
                "label": _clip(item.get("label"), 20) or _clip(item.get("direction"), 10),
                "direction": _clip(item.get("direction"), MAX_DIRECTION_CHARS),
                "why": _clip(item.get("why"), 60),
            })
    if not directions:
        raise ValueError("模型没有给出可用的剧情走向")
    return {"directions": directions[:MAX_DIRECTIONS], "warnings": list(warnings or [])}


# ---------------------------------------------------------------- generation


async def stream_prose(
    settings: dict[str, Any], messages: list[dict[str, str]], options: Any = None
) -> tuple[str, StreamOutcome]:
    """Collect one streamed completion (module-level so tests can replace it)."""
    outcome = StreamOutcome()
    kwargs = {"options": options} if options is not None else {}
    pieces = [piece async for piece in llama_client.chat_stream(settings, messages, outcome=outcome, **kwargs)]
    return "".join(pieces).strip(), outcome


def branch_prompt_project(project: dict[str, Any], index: int) -> dict[str, Any]:
    """Copy of the project in which the target chapter starts empty."""
    prompt_project = deepcopy(project)
    chapter = prompt_project["chapters"][index]
    chapter["content"] = ""
    chapter["summary"] = ""
    return prompt_project


async def generate_branch_prose(
    project: dict[str, Any],
    chapter_id: str,
    direction: str,
    *,
    attach_retrieval: Callable[[dict[str, Any], dict[str, Any]], Any],
    prepare_build: Callable[[Any, dict[str, Any], dict[str, Any]], Any],
    rewrite_completion: Completion | None = None,
) -> dict[str, Any]:
    """Write one full chapter draft for ``direction`` through the normal prompt."""
    index = chapter_index(project, chapter_id)
    prompt_project = branch_prompt_project(project, index)
    target_chars = max(100, min(10000, int(project.get("settings", {}).get("target_words", 1200) or 1200)))
    prose_settings = _effective_prose_settings(project.get("settings", {}), target_chars, "prose")
    prompt_project["settings"] = prose_settings
    request: dict[str, Any] = {
        "project": prompt_project,
        "chapter_id": chapter_id,
        "mode": "instruction",
        "instruction": branch_instruction(direction),
        "selection": "",
        "target_words": target_chars,
        "skill_ids": [],
    }
    attach_retrieval(prompt_project, request)
    build = build_prompt(prompt_project, request)
    prepare_build(build, prompt_project, prompt_project["chapters"][index])
    llama_client.validate_context_budget(prose_settings, build.messages, int(prose_settings.get("max_tokens", 3500)))
    options = await prose_stream_options(prompt_project, prose_settings)
    text, outcome = await stream_prose(prose_settings, build.messages, options)
    warnings: list[str] = []
    actual = _prose_char_count(text)
    if actual >= 80 and (actual < int(target_chars * 0.75) or outcome.truncated or _prose_looks_truncated(text)):
        # One conservative continuation, like the manual generation repair.
        remaining = max(120, target_chars - actual)
        anchor = re.sub(r"\s+", " ", text[-180:]).strip()
        messages = [dict(message) for message in build.messages] + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": (
                f"当前正文约 {actual} 字，目标约 {target_chars} 字，还需要约 {remaining} 字。"
                f"从上文最后一句【{anchor}】之后直接继续，不要重写、回顾或解释；"
                "保持本分支的剧情走向、人物、视角和既有事实不变，并收束在完整句子上。"
            )},
        ]
        repair_settings = _effective_prose_settings(prose_settings, remaining)
        try:
            more, _ = await stream_prose(repair_settings, messages)
            continuation = _clean_repair_continuation(text, more)
            if continuation:
                text = (text.rstrip() + continuation).strip()
                actual = _prose_char_count(text)
        except Exception as exc:  # noqa: BLE001 - keep the first draft
            warnings.append(f"续补失败：{str(exc)[:120]}")
    if actual < 80:
        raise ValueError(f"模型返回正文过短（约 {actual} 字）")
    if rewrite_completion is not None:
        try:
            revised, _report = await apply_antislop(text, prompt_project, prose_settings, rewrite_completion)
            if revised and _prose_char_count(revised) >= 80:
                text = revised
        except Exception as exc:  # noqa: BLE001 - optional post-processing
            warnings.append(f"套话改写跳过：{str(exc)[:120]}")
    if _prose_char_count(text) < int(target_chars * 0.75):
        warnings.append(f"分支偏短：约 {_prose_char_count(text)}/{target_chars} 字")
    return {
        "content": text,
        "chars": _prose_char_count(text),
        "target_chars": target_chars,
        "model": str(prose_settings.get("model", "")),
        "warnings": warnings,
    }


async def summarize_branch(text: str, settings: dict[str, Any], completion: Completion) -> str:
    try:
        result, _ = await completion(
            settings,
            [{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": judge_excerpt(text, 5000)}],
            temperature=0.2, max_tokens=300, timeout_seconds=90, workload="critic", schema=SUMMARY_SCHEMA,
        )
        summary = _clip(result.get("summary"), 200)
        if summary:
            return summary
    except Exception:  # noqa: BLE001 - fall back to the head/tail of the draft
        pass
    compact = re.sub(r"\s+", "", str(text or ""))
    return (compact[:60] + "……" + compact[-60:]) if len(compact) > 130 else compact


# ---------------------------------------------------------- audit and judging


def audit_digest(result: dict[str, Any]) -> dict[str, Any]:
    rank = {"critical": 4, "blocker": 4, "high": 3, "medium": 2, "low": 1}
    issues = [item for item in result.get("issues", []) or [] if isinstance(item, dict)]
    issues.sort(key=lambda item: -rank.get(str(item.get("severity", "")).lower(), 0))
    score = result.get("score")
    return {
        "score": score if isinstance(score, (int, float)) and not isinstance(score, bool) else None,
        "verdict": str(result.get("verdict", "")),
        "top_issues": [
            {"severity": str(item.get("severity", "")), "category": _clip(item.get("category"), 30),
             "message": _clip(item.get("message"), 160)}
            for item in issues[:3]
        ],
        "fallback": bool(result.get("fallback")),
    }


def judge_plan_brief(project: dict[str, Any], index: int) -> str:
    return (
        _chapter_brief(project, index)
        + "\n【评审补充】两份候选按作者指定的不同剧情走向写成，走向不同本身不是缺点；"
        "请比较执行质量，并在理由中至少用一条点评该走向对后续剧情的影响（伏笔、人物关系或卷目标）。"
    )


async def judge_branches(
    branches: list[dict[str, Any]],
    plan_brief: str,
    settings: dict[str, Any],
    completion: Completion,
    on_match: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Round-robin blind pairwise judging; each pair is judged in both orders."""
    wins = {branch["id"]: 0.0 for branch in branches}
    notes: dict[str, list[str]] = {branch["id"]: [] for branch in branches}
    matches: list[dict[str, Any]] = []
    for left, right in combinations(branches, 2):
        try:
            verdict = await pairwise_judge(
                left["content"], right["content"], plan_brief, settings, completion,
                left_name=left["label"], right_name=right["label"],
            )
        except Exception as exc:  # noqa: BLE001 - a failed match counts as no result
            matches.append({"left": left["id"], "right": right["id"], "winner": "", "error": str(exc)[:200]})
            if on_match:
                on_match()
            continue
        if verdict["winner"] == "tie":
            wins[left["id"]] += 0.5
            wins[right["id"]] += 0.5
            winner = "tie"
        else:
            winner = left["id"] if verdict["winner"] == "left" else right["id"]
            wins[winner] += 1
        for branch in (left, right):
            for reason in verdict.get("reasons", []):
                if reason not in notes[branch["id"]]:
                    notes[branch["id"]].append(reason)
        matches.append({
            "left": left["id"], "right": right["id"], "winner": winner,
            "reasons": verdict.get("reasons", []), "scores": verdict.get("scores", {}),
            "orders_judged": verdict.get("orders_judged", 0),
        })
        if on_match:
            on_match()
    return {"wins": wins, "notes": {key: value[:4] for key, value in notes.items()}, "matches": matches}


# -------------------------------------------------------------- plan impact


def future_routes(project: dict[str, Any], index: int) -> list[dict[str, Any]]:
    """Planned routes after chapter ``index`` in the same volume (chapter numbers)."""
    chapters = project.get("chapters", [])
    chapter = chapters[index]
    by_route = {
        str(item.get("route_id", "")): number
        for number, item in enumerate(chapters) if item.get("route_id")
    }
    volume = next(
        (item for item in project.get("planning", {}).get("volumes", []) or []
         if str(item.get("id", "")) == str(chapter.get("volume_id", "")) and chapter.get("volume_id")),
        None,
    )
    result: list[dict[str, Any]] = []
    if volume is not None:
        routes = [route for route in volume.get("chapters", []) or [] if isinstance(route, dict)]
        own_id = str(chapter.get("route_id", ""))
        position = next((p for p, route in enumerate(routes) if str(route.get("id", "")) == own_id), -1)
        for route in routes[position + 1:] if position >= 0 else []:
            number = by_route.get(str(route.get("id", "")))
            if number is not None and number <= index:
                continue
            result.append({
                "number": (number + 1) if number is not None else int(route.get("number", 0) or 0),
                "route_id": str(route.get("id", "")),
                "chapter_id": str(chapters[number].get("id", "")) if number is not None else "",
                "title": _clip(route.get("title"), 40),
                "goal": _clip(route.get("goal"), 120),
                "turning_point": _clip(route.get("turning_point"), 120),
                "ending_hook": _clip(route.get("ending_hook"), 120),
                "written": bool(number is not None and str(chapters[number].get("content", "")).strip()),
            })
    else:
        for number in range(index + 1, len(chapters)):
            item = chapters[number]
            route = item.get("route") if isinstance(item.get("route"), dict) else {}
            plan = item.get("plan") if isinstance(item.get("plan"), dict) else {}
            goal = route.get("goal") or plan.get("goal") or item.get("scene_goal")
            if not goal:
                continue
            result.append({
                "number": number + 1, "route_id": str(item.get("route_id", "")),
                "chapter_id": str(item.get("id", "")), "title": _clip(item.get("title"), 40),
                "goal": _clip(goal, 120),
                "turning_point": _clip(route.get("turning_point") or plan.get("turning_point"), 120),
                "ending_hook": _clip(route.get("ending_hook") or plan.get("ending_hook"), 120),
                "written": bool(str(item.get("content", "")).strip()),
            })
    return [item for item in result if item["number"] > 0][:MAX_FUTURE_ROUTES]


async def plan_impact(
    project: dict[str, Any], index: int, branches: list[dict[str, Any]], completion: Completion
) -> dict[str, Any]:
    """One structured call: which later routes each branch would force to change."""
    routes = future_routes(project, index)
    if not routes:
        return {"routes": [], "branches": {branch["id"]: {"overall": "本卷后面没有已规划的章节路线", "affected": []} for branch in branches}}
    chapter = project["chapters"][index]
    volume = next(
        (item for item in project.get("planning", {}).get("volumes", []) or []
         if str(item.get("id", "")) == str(chapter.get("volume_id", "")) and chapter.get("volume_id")),
        {},
    )
    volume_brief = {key: volume.get(key) for key in ("title", "goal", "ending_state", "irreversible_change") if volume.get(key)}
    user = (
        f"【本卷契约】\n{json.dumps(volume_brief, ensure_ascii=False) if volume_brief else '（无）'}\n\n"
        f"【本章原计划】\n{_chapter_brief(project, index)}\n\n"
        "【本卷后续路线】\n" + json.dumps(
            [{key: item[key] for key in ("number", "title", "goal", "turning_point", "ending_hook")} for item in routes],
            ensure_ascii=False, indent=1,
        )
        + "\n\n【试写分支】\n"
        + "\n".join(f"{branch['id']}（{branch['label']}）走向：{branch['direction']}\n实际内容：{branch.get('summary', '')}" for branch in branches)
    )
    result, _warnings = await completion(
        project.get("settings", {}),
        [{"role": "system", "content": IMPACT_SYSTEM}, {"role": "user", "content": user}],
        temperature=0.2, max_tokens=1200, timeout_seconds=150, workload="planning", schema=IMPACT_SCHEMA,
    )
    known = {item["number"]: item for item in routes}
    by_id: dict[str, dict[str, Any]] = {}
    labels = {branch["label"]: branch["id"] for branch in branches}
    for item in result.get("branches", []) if isinstance(result.get("branches"), list) else []:
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("id", "")).strip()
        branch_id = raw_id if raw_id in {branch["id"] for branch in branches} else labels.get(raw_id, "")
        if not branch_id:
            continue
        affected = []
        for change in item.get("affected", []) if isinstance(item.get("affected"), list) else []:
            if not isinstance(change, dict):
                continue
            try:
                number = int(change.get("number", 0))
            except (TypeError, ValueError):
                continue
            if number not in known or any(entry["number"] == number for entry in affected):
                continue
            severity = str(change.get("severity", "")).lower()
            affected.append({
                "number": number, "title": known[number]["title"], "route_id": known[number]["route_id"],
                "chapter_id": known[number]["chapter_id"], "written": known[number]["written"],
                "change": _clip(change.get("change"), 120),
                "severity": severity if severity in {"minor", "major"} else "minor",
            })
        by_id[branch_id] = {"overall": _clip(item.get("overall"), 120), "affected": affected}
    return {
        "routes": [{"number": item["number"], "title": item["title"]} for item in routes],
        "branches": {branch["id"]: by_id.get(branch["id"], {"overall": "", "affected": [], "unknown": True}) for branch in branches},
    }


# ------------------------------------------------------------------- the job


def new_branch_job(
    project: dict[str, Any], chapter_id: str, directions: list[dict[str, str]],
    *, audit: bool = True, judge: bool = True, impact: bool = True,
) -> dict[str, Any]:
    index = chapter_index(project, chapter_id)
    chapter = project["chapters"][index]
    now = utc_now()
    count = len(directions)
    steps = count * 2 + (count if audit else 0) + (count * (count - 1) // 2 if judge else 0) + (1 if impact else 0)
    return {
        "id": str(uuid.uuid4()),
        "status": "running",
        "phase": "generate",
        "message": "准备试写",
        "chapter_id": str(chapter_id),
        "chapter_number": index + 1,
        "chapter_title": str(chapter.get("title", "")),
        "base_content_hash": content_hash(chapter.get("content", "")),
        "options": {"audit": bool(audit), "judge": bool(judge), "impact": bool(impact)},
        "branches": [
            {
                "id": f"br-{uuid.uuid4().hex[:10]}", "label": item["label"], "direction": item["direction"],
                "content": "", "summary": "", "created_at": "", "model": "", "chars": 0,
                "audit": {}, "judge": {}, "impact": {}, "checks": {}, "warnings": [],
                "status": "pending", "error": "",
            }
            for item in directions
        ],
        "comparison": {},
        "steps_total": steps,
        "steps_done": 0,
        "errors": [],
        "cancel_requested": False,
        "created_at": now,
        "updated_at": now,
    }


async def run_branch_job(
    *,
    project: dict[str, Any],
    extensions: Any,
    project_id: str,
    job: dict[str, Any],
    completion: Completion,
    generate: GenerateBranch,
    audit: AuditBranch | None = None,
) -> dict[str, Any]:
    """Draft, audit, judge and compare; checkpoint after every model step.

    Branches are written one after another (one long prose stream at a time
    keeps a local llama.cpp usable).  Cancelling keeps finished branches.
    """
    job_id = job["id"]
    chapter_id = job["chapter_id"]
    index = chapter_index(project, chapter_id)
    settings = project.get("settings", {})

    def patch(mutate: Callable[[dict[str, Any]], None]) -> None:
        def apply(current: Any) -> dict[str, Any] | None:
            if not isinstance(current, dict) or current.get("id") != job_id:
                return None  # a newer job replaced this one
            mutate(current)
            current["updated_at"] = utc_now()
            return current

        extensions.update(project_id, JOB_KEY, apply)

    def step(message: str, phase: str | None = None, **branch_update: Any) -> None:
        branch_id = branch_update.pop("branch_id", "")

        def mutate(current: dict[str, Any]) -> None:
            current["steps_done"] = min(int(current.get("steps_total", 0)), int(current.get("steps_done", 0)) + 1)
            current["message"] = message
            if phase:
                current["phase"] = phase
            if branch_id:
                for branch in current["branches"]:
                    if branch["id"] == branch_id:
                        branch.update(branch_update)

        patch(mutate)

    def note(message: str, phase: str | None = None) -> None:
        def mutate(current: dict[str, Any]) -> None:
            current["message"] = message
            if phase:
                current["phase"] = phase

        patch(mutate)

    branches = [dict(item) for item in job["branches"]]
    try:
        for number, branch in enumerate(branches, 1):
            if branch.get("status") == "draft" and branch.get("content"):
                continue
            note(f"正在试写分支 {number}/{len(branches)}：{branch['label']}", "generate")
            try:
                drafted = await generate(project, chapter_id, branch["direction"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - other branches may still succeed
                message = str(exc)[:300] or exc.__class__.__name__
                branch.update(status="failed", error=message)
                step(f"分支“{branch['label']}”试写失败", branch_id=branch["id"], status="failed", error=message)
                step(f"分支“{branch['label']}”跳过摘要")
                patch(lambda current, m=message, b=branch: current["errors"].append(f"{b['label']}：{m}"))
                continue
            branch.update(
                content=drafted["content"], chars=drafted.get("chars", 0), model=drafted.get("model", ""),
                warnings=list(drafted.get("warnings", [])), created_at=utc_now(), status="draft",
                checks=deterministic_score(drafted["content"], int(drafted.get("target_chars", 0) or 1200), project),
            )
            step(
                f"分支“{branch['label']}”已写完，约 {branch['chars']} 字", branch_id=branch["id"],
                **{key: branch[key] for key in ("content", "chars", "model", "warnings", "created_at", "status", "checks")},
            )
            branch["summary"] = await summarize_branch(branch["content"], settings, completion)
            step(f"分支“{branch['label']}”已概括", branch_id=branch["id"], summary=branch["summary"])

        ready = [branch for branch in branches if branch.get("status") == "draft" and branch.get("content")]
        if not ready:
            def failed(current: dict[str, Any]) -> None:
                current.update(status="failed", phase="done", message="所有分支都没有写成")

            patch(failed)
            return extensions.get(project_id, JOB_KEY) or job

        options = job.get("options", {})
        if options.get("audit") and audit is not None:
            for branch in branches:
                if branch not in ready:
                    step("跳过审计")
                    continue
                note(f"正在审计分支“{branch['label']}”", "audit")
                try:
                    branch["audit"] = audit_digest(await audit(project, chapter_id, branch["content"]))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - audit is advisory here
                    branch["audit"] = {"score": None, "verdict": "", "top_issues": [], "error": str(exc)[:200]}
                step(f"分支“{branch['label']}”审计完成", branch_id=branch["id"], audit=branch["audit"])

        judged: dict[str, Any] = {}
        if options.get("judge") and len(ready) >= 2:
            note("正在盲评比较各分支", "judge")
            judged = await judge_branches(
                ready, judge_plan_brief(project, index), settings, completion,
                on_match=lambda: step("完成一组盲评", "judge"),
            )
            for branch in ready:
                branch["judge"] = {"wins": judged["wins"].get(branch["id"], 0), "notes": judged["notes"].get(branch["id"], [])}

        impact: dict[str, Any] = {}
        if options.get("impact"):
            note("正在分析对后续剧情的影响", "impact")
            try:
                impact = await plan_impact(project, index, ready, completion)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - comparison still useful without it
                impact = {"error": str(exc)[:200], "branches": {}}
            for branch in ready:
                branch["impact"] = impact.get("branches", {}).get(branch["id"], {"overall": "", "affected": [], "unknown": True})
            step("影响分析完成", "impact")

        comparison = build_comparison(ready, judged, impact)

        def finish(current: dict[str, Any]) -> None:
            by_id = {branch["id"]: branch for branch in branches}
            for item in current["branches"]:
                if item["id"] in by_id:
                    item.update({key: by_id[item["id"]].get(key) for key in ("audit", "judge", "impact", "summary")})
            current.update(
                status="completed", phase="done", comparison=comparison,
                steps_done=current.get("steps_total", 0),
                message=f"已完成 {len(ready)} 个分支的试写与比较，请选择采用哪一版",
            )

        patch(finish)
    except asyncio.CancelledError:
        def cancelled(current: dict[str, Any]) -> None:
            current.update(status="cancelled", cancel_requested=False, phase="done", message="已取消；已写完的分支会保留")
            for item in current["branches"]:
                if item.get("status") == "pending":
                    item["status"] = "cancelled"

        patch(cancelled)
        raise
    return extensions.get(project_id, JOB_KEY) or job


def build_comparison(
    branches: list[dict[str, Any]], judged: dict[str, Any], impact: dict[str, Any]
) -> dict[str, Any]:
    rows = []
    for branch in branches:
        audit = branch.get("audit") or {}
        branch_impact = branch.get("impact") or {}
        rows.append({
            "id": branch["id"], "label": branch["label"], "direction": branch["direction"],
            "summary": branch.get("summary", ""), "chars": branch.get("chars", 0),
            "score": audit.get("score"), "verdict": audit.get("verdict", ""),
            "top_issues": audit.get("top_issues", []),
            "wins": (judged.get("wins") or {}).get(branch["id"]),
            "judge_notes": (judged.get("notes") or {}).get(branch["id"], []),
            "impact": branch_impact,
            "affected_count": len(branch_impact.get("affected", []) or []),
        })

    def rank(row: dict[str, Any]) -> tuple[float, float]:
        return (float(row["wins"] or 0), float(row["score"] or 0))

    leader = max(rows, key=rank) if rows else None
    tied = leader is not None and sum(1 for row in rows if rank(row) == rank(leader)) > 1
    return {
        "branches": rows,
        "matches": judged.get("matches", []),
        "recommended": "" if (leader is None or tied or (leader["wins"] is None and leader["score"] is None)) else leader["id"],
        "future_routes": impact.get("routes", []),
        "impact_error": impact.get("error", ""),
        "note": "推荐只是参考：评审只看执行质量，剧情走向由作者决定。",
    }


def public_branch_job(job: dict[str, Any] | None, running: bool) -> dict[str, Any]:
    if not isinstance(job, dict):
        return {"exists": False}
    view = deepcopy(job)
    view["exists"] = True
    view["running"] = running
    total = max(1, int(view.get("steps_total", 0) or 0))
    done = min(int(view.get("steps_done", 0) or 0), total)
    finished = view.get("status") in FINISHED
    view["progress"] = 1.0 if view.get("status") == "completed" else round(min(done / total, 1.0 if finished else 0.99), 3)
    if view.get("status") == "running" and not running:
        view["status"] = "interrupted"
        view["message"] = "试写在后台中断（可能是程序重启）；已写完的分支仍可收入本章"
    return view


# ------------------------------------------------- chapter-side bookkeeping


def _chapter(project: dict[str, Any], chapter_id: str) -> dict[str, Any]:
    return project["chapters"][chapter_index(project, chapter_id)]


def prune_branches(branches: list[dict[str, Any]], limit: int = MAX_BRANCHES_PER_CHAPTER) -> list[dict[str, Any]]:
    """Keep at most ``limit``: drop the oldest discarded first, then oldest drafts."""
    kept = [item for item in branches if isinstance(item, dict)]
    for status in ("discarded", "draft"):
        while len(kept) > limit:
            victims = [item for item in kept if item.get("status") == status]
            if not victims:
                break
            oldest = min(victims, key=lambda item: str(item.get("created_at", "")))
            kept.remove(oldest)
    return kept


def _branch_verdicts(branch: dict[str, Any]) -> dict[str, Any]:
    audit = branch.get("audit") or {}
    judge = branch.get("judge") or {}
    return {
        "summary": branch.get("summary", ""),
        "audit": {key: audit.get(key) for key in ("score", "verdict", "top_issues")},
        "judge": {"wins": judge.get("wins"), "notes": judge.get("notes", [])},
        "impact": branch.get("impact") or {},
    }


def merge_job_into_chapter(project: dict[str, Any], job: dict[str, Any] | None) -> tuple[dict[str, Any], int]:
    """Add the job's finished branches to ``chapter["branches"]`` (idempotent)."""
    if not isinstance(job, dict) or not job.get("chapter_id"):
        return project, 0
    updated = deepcopy(project)
    try:
        chapter = _chapter(updated, str(job["chapter_id"]))
    except KeyError:
        return project, 0
    existing = [item for item in chapter.get("branches", []) or [] if isinstance(item, dict)]
    known = {str(item.get("id", "")) for item in existing}
    added = 0
    for branch in job.get("branches", []) or []:
        if branch.get("status") != "draft" or not str(branch.get("content", "")).strip():
            continue
        if branch["id"] in known:
            # A merge made while the job was still auditing: refresh the verdicts.
            for item in existing:
                if item.get("id") == branch["id"] and item.get("status") == "draft":
                    item.update(_branch_verdicts(branch))
            continue
        existing.append({
            "id": branch["id"], "label": branch.get("label", ""), "direction": branch.get("direction", ""),
            "content": branch["content"], "summary": branch.get("summary", ""),
            "created_at": branch.get("created_at") or utc_now(), "model": branch.get("model", ""),
            "chars": branch.get("chars", 0),
            **_branch_verdicts(branch),
            "session_id": job["id"],
            "status": "draft",
        })
        added += 1
    chapter["branches"] = prune_branches(existing)
    chapter["branch_session"] = {
        "id": job["id"],
        "base_content_hash": job.get("base_content_hash", ""),
        "created_at": job.get("created_at", ""),
        "status": job.get("status", ""),
        "comparison": deepcopy(job.get("comparison") or {}),
    }
    return updated, added


def discard_branch(project: dict[str, Any], chapter_id: str, branch_id: str) -> dict[str, Any]:
    updated = deepcopy(project)
    chapter = _chapter(updated, chapter_id)
    branch = next((item for item in chapter.get("branches", []) or [] if item.get("id") == branch_id), None)
    if branch is None:
        raise KeyError(branch_id)
    if branch.get("status") == "adopted":
        raise ValueError("已采用的分支不能丢弃；如需回退请使用章节版本")
    branch["status"] = "discarded"
    branch["discarded_at"] = utc_now()
    chapter["branches"] = prune_branches(chapter["branches"])
    return updated


def adopt_branch(project: dict[str, Any], chapter_id: str, branch_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Replace the chapter prose with a branch; never called automatically.

    The caller saves with a non-autosave reason so the store keeps the
    previous chapter as a version.  Other draft branches become discarded.
    Later routes of the same volume that the impact analysis says this
    branch changes are flagged ``plan_stale`` (the rolling re-plan flag) and
    the volume routes get ``needs_replan``.
    """
    updated = deepcopy(project)
    index = chapter_index(updated, chapter_id)
    chapter = updated["chapters"][index]
    branches = [item for item in chapter.get("branches", []) or [] if isinstance(item, dict)]
    branch = next((item for item in branches if item.get("id") == branch_id), None)
    if branch is None:
        raise KeyError(branch_id)
    if not str(branch.get("content", "")).strip():
        raise ValueError("这个分支没有正文")
    warnings: list[str] = []
    session = chapter.get("branch_session") if isinstance(chapter.get("branch_session"), dict) else {}
    if session.get("base_content_hash") and session["base_content_hash"] != content_hash(chapter.get("content", "")):
        warnings.append("试写开始后本章正文又被修改过；修改后的正文已保存为章节版本，可随时恢复")
    previous_hash = content_hash(chapter.get("content", ""))
    now = utc_now()
    chapter["content"] = branch["content"]
    if str(chapter.get("authority_state", "")) in {"reviewed", "accepted"}:
        chapter["authority_state"] = "candidate"  # the new prose has not been reviewed
    if branch.get("summary"):
        chapter["summary"] = branch["summary"]
    for item in branches:
        if item is branch:
            item["status"] = "adopted"
            item["adopted_at"] = now
        elif item.get("status") == "draft":
            item["status"] = "discarded"
            item["discarded_at"] = now
    chapter["branches"] = prune_branches(branches)
    chapter["branch_session"] = {**session, "adopted_branch_id": branch_id, "adopted_at": now,
                                 "previous_content_hash": previous_hash}
    marked: list[dict[str, Any]] = []
    impact = branch.get("impact") if isinstance(branch.get("impact"), dict) else {}
    by_route = {}
    for volume in updated.get("planning", {}).get("volumes", []) or []:
        for route in volume.get("chapters", []) or []:
            if isinstance(route, dict) and route.get("id"):
                by_route[str(route["id"])] = route
    for change in impact.get("affected", []) or []:
        flag = {
            "reason": "branch_adopted", "after_chapter": index + 1, "branch_id": branch_id,
            "branch_label": branch.get("label", ""), "change": change.get("change", ""),
            "severity": change.get("severity", "minor"), "at": now,
        }
        target = next(
            (item for item in updated["chapters"][index + 1:]
             if (change.get("chapter_id") and item.get("id") == change["chapter_id"])
             or (change.get("route_id") and item.get("route_id") == change["route_id"])),
            None,
        )
        if target is not None and str(target.get("content", "")).strip():
            warnings.append(f"第{change.get('number')}章已有正文，未标记重规划，请人工复核")
            continue
        if target is not None:
            target["plan_stale"] = {**flag, "fields": []}
        route = by_route.get(str(change.get("route_id", "")))
        if route is not None:
            route["needs_replan"] = flag
        if target is not None or route is not None:
            marked.append({"number": change.get("number"), "title": change.get("title", ""), "severity": flag["severity"]})
    report = {
        "adopted": branch_id, "label": branch.get("label", ""), "replan_marked": marked,
        "impact_known": bool(impact) and not impact.get("unknown"), "warnings": warnings,
    }
    return updated, report

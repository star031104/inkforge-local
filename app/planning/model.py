"""The planning model: master-plan normalisation, volume counts, planning
constraints, local fallback plans, volume/chapter lookup, planning context for
prompts and applying volume routes to chapters."""
from __future__ import annotations

import math
import re
import uuid
from copy import deepcopy
from typing import Any

from app.story.debate import plan_debate_entry
from app.storage.schema.planning import _strings, ensure_planning_defaults, normalize_route


def _allocate_counts(raw_volumes: list[dict[str, Any]], total: int) -> list[int]:
    count = min(max(1, len(raw_volumes)), total)
    raw_volumes = raw_volumes[:count]
    weights = []
    for item in raw_volumes:
        suggested = item.get("chapter_count")
        if not isinstance(suggested, (int, float)) or suggested <= 0:
            start, end = item.get("chapter_start"), item.get("chapter_end")
            suggested = (
                int(end) - int(start) + 1
                if isinstance(start, (int, float))
                and isinstance(end, (int, float))
                and end >= start
                else 1
            )
        weights.append(max(1.0, float(suggested)))
    remaining = total - count
    ideal_extra = [remaining * weight / sum(weights) for weight in weights]
    extras = [math.floor(value) for value in ideal_extra]
    for index in sorted(
        range(count), key=lambda i: ideal_extra[i] - extras[i], reverse=True
    )[: remaining - sum(extras)]:
        extras[index] += 1
    return [1 + value for value in extras]


def normalize_master_plan(raw: dict[str, Any], target_chapters: int) -> dict[str, Any]:
    target = min(2000, max(1, int(target_chapters or 1)))
    raw_volumes = [
        item for item in raw.get("volumes", []) if isinstance(item, dict)
    ][:20]
    if not raw_volumes:
        raise ValueError("模型没有返回可用的分卷规划")
    counts = _allocate_counts(raw_volumes, target)
    cursor = 1
    volumes = []
    for index, (item, chapter_count) in enumerate(zip(raw_volumes, counts)):
        end = cursor + chapter_count - 1
        volumes.append(
            {
                "id": str(uuid.uuid4()),
                "number": index + 1,
                "title": str(item.get("title") or f"第{index + 1}卷").strip(),
                "chapter_start": cursor,
                "chapter_end": end,
                "goal": str(item.get("goal", "")).strip(),
                "conflict": str(item.get("conflict", "")).strip(),
                "synopsis": str(item.get("synopsis", "")).strip(),
                "turning_points": _strings(item.get("turning_points")),
                "character_arcs": _strings(item.get("character_arcs")),
                "subplots": _strings(item.get("subplots")),
                "must_keep": _strings(item.get("must_keep")),
                "must_avoid": _strings(item.get("must_avoid")),
                "ending_state": str(item.get("ending_state", "")).strip(),
                "bridge_to_next": str(item.get("bridge_to_next", "")).strip(),
                "chapters": [],
            }
        )
        cursor = end + 1
    master = {
        key: str(raw.get(key, "")).strip()
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
        )
    }
    for key in (
        "stakes_ladder",
        "major_character_arcs",
        "subplots",
        "historical_nodes",
    ):
        master[key] = _strings(raw.get(key), 12)
    return {"version": 1, "master": master, "volumes": volumes}


_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_number(text: str) -> int:
    """Parse 1–99 written as Arabic digits or simple Chinese numerals."""
    text = str(text or "").strip()
    if text.isdigit():
        return int(text)
    if not text or any(ch not in _CN_DIGITS and ch != "十" for ch in text):
        return 0
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        return tens * 10 + (_CN_DIGITS.get(right, 0) if right else 0)
    return _CN_DIGITS.get(text, 0)


_VOLUME_TOTAL_RE = re.compile(r"(?:共|分为?|拆成|总计|一共)\s*([\d一二两三四五六七八九十]{1,3})\s*卷")
_VOLUME_HEADING_RE = re.compile(
    r"(?m)^\s*#{0,6}\s*(?:第([一二三四五六七八九十百\d]+)卷|卷([一二三四五六七八九十\d]+))"
)


def default_volume_count(target_chapters: int, *, local: bool = False) -> int:
    """Volumes when the author did not say: ~12 chapters for small local
    models (one call must hold a whole volume), ~30 for cloud models."""
    if target_chapters <= 6:
        return 1
    per_volume = 12 if local else 30
    return min(12, max(2, math.ceil(target_chapters / per_volume)))


def planned_volume_count(
    project: dict[str, Any],
    target_chapters: int,
    instruction: str = "",
    *,
    configured: Any = None,
    local: bool = False,
) -> int:
    """Volume count the author asked for, in priority order:

    1. an explicit setting (``narrative.volume_count`` or a task config);
    2. "共4卷" style wording in this run's planning instruction;
    3. distinct 卷一/第二卷 headings in the author's outline;
    4. a size-based default.
    """
    narrative = project.get("narrative", {}) if isinstance(project.get("narrative"), dict) else {}
    for value in (configured, narrative.get("volume_count"), narrative.get("preferred_volume_count")):
        try:
            number = int(value or 0)
        except (TypeError, ValueError):
            number = 0
        if number > 0:
            return max(1, min(24, number, target_chapters))
    match = _VOLUME_TOTAL_RE.search(str(instruction or ""))
    if match and _cn_number(match.group(1)) > 0:
        return max(1, min(24, _cn_number(match.group(1)), target_chapters))
    headings = {
        _cn_number(left or right)
        for left, right in _VOLUME_HEADING_RE.findall(str(project.get("outline", "")))
    } - {0}
    if len(headings) >= 2:
        return max(1, min(24, len(headings), target_chapters))
    return default_volume_count(target_chapters, local=local)


def planning_constraints(project: dict[str, Any], *, max_chars: int = 3600) -> str:
    """Hard limits the planner must respect: chronology and who may appear.

    Volume and route planners used to see only names and roles, so they placed
    characters before their historical entrance and moved dated events.  This
    block carries the always-on world entries (e.g. a chronology) and each
    character's hard limits, and lists characters the author has parked.
    """
    lines: list[str] = []
    for entry in project.get("world_entries", []) or []:
        if isinstance(entry, dict) and entry.get("constant") and entry.get("enabled", True):
            content = str(entry.get("content", "")).strip()
            if content:
                lines.append(f"【{entry.get('title', '设定')}】{content[:900]}")
    parked: list[str] = []
    for item in project.get("characters", []) or []:
        if not isinstance(item, dict) or not str(item.get("name", "")).strip():
            continue
        name = str(item.get("name", "")).strip()
        limits = str(item.get("hard_limits", "")).strip()
        if item.get("active", True) is False:
            parked.append(name)
        if limits:
            lines.append(f"{name}的硬边界：{limits[:220]}")
    if parked:
        lines.append(
            "作者暂未启用的人物（只能在其硬边界允许的时间之后登场，前期规划不得让他们出场）："
            + "、".join(parked)
        )
    text = "\n".join(lines)
    return text[:max_chars]


def fallback_master_plan(
    project: dict[str, Any], target_chapters: int, reason: str
) -> dict[str, Any]:
    """Build an editable plan from the author's outline when the model fails."""
    outline = str(project.get("outline", "")).strip()
    narrative = project.get("narrative", {})
    heading_pattern = re.compile(
        r"(?m)^\s*#{0,6}\s*(?:第[一二三四五六七八九十百\d]+卷|卷[一二三四五六七八九十\d]+)"
        r"[：:\s]*([^\n（(：:]+)"
        r"(?:[（(][^\n]*?第?\s*(\d+)\s*[—–-]\s*(\d+)\s*章)?"
    )
    matches = list(heading_pattern.finditer(outline))
    raw_volumes: list[dict[str, Any]] = []
    for index, match in enumerate(matches[:20]):
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(outline)
        body = outline[match.end() : body_end]
        body = re.sub(r"[#>*`|]+", " ", body)
        lines = [
            re.sub(r"^\s*[-\d.、：:]+\s*", "", line).strip()
            for line in body.splitlines()
            if line.strip()
        ]
        summary = "；".join(lines[:3])
        start = int(match.group(2)) if match.group(2) else 0
        end = int(match.group(3)) if match.group(3) else 0
        raw_volumes.append(
            {
                "title": match.group(1).strip(),
                "chapter_count": end - start + 1 if end >= start > 0 else 1,
                "goal": summary[:90] or "依据作者总纲完成本卷阶段变化",
                "conflict": (summary[90:170] or project.get("premise", ""))[:80],
                "synopsis": "；".join(lines)[:1200] or summary,
                "ending_state": (lines[-1] if lines else "进入下一阶段")[:80],
                "bridge_to_next": (
                    "由本卷结果触发下一阶段，具体因果待AI详细规划"
                ),
            }
        )
    if not raw_volumes:
        count = planned_volume_count(project, target_chapters)
        chunks = [
            item.strip()
            for item in re.split(r"\n\s*\n|(?<=[。！？])", outline)
            if item.strip()
        ]
        for index in range(count):
            chunk = chunks[index] if index < len(chunks) else ""
            raw_volumes.append(
                {
                    "title": f"第{index + 1}阶段",
                    "chapter_count": max(1, round(target_chapters / count)),
                    "goal": chunk[:90] or "待作者审核阶段目标",
                    "conflict": str(project.get("premise", ""))[:80],
                    "synopsis": chunk[:1200],
                    "ending_state": "推动故事进入下一阶段",
                    "bridge_to_next": "本阶段结果触发下一阶段",
                }
            )
    raw = {
        "theme": str(narrative.get("central_question", ""))[:80],
        "reader_promise": str(project.get("author_intent", ""))[:80],
        "central_conflict": str(project.get("premise", ""))[:80],
        "story_engine": "依据作者既有总纲逐卷推进选择、行动与代价",
        "ending_state": str(narrative.get("ending_direction", ""))[:80],
        "full_outline": outline,
        "main_plot": str(project.get("premise", ""))[:600],
        "theme_progression": str(narrative.get("central_question", ""))[:500],
        "pacing_plan": "依据分卷章数逐级升级冲突，具体节奏待AI完成",
        "stakes_ladder": [],
        "major_character_arcs": [],
        "subplots": [],
        "historical_nodes": [],
        "volumes": raw_volumes,
    }
    result = normalize_master_plan(raw, target_chapters)
    result["fallback"] = True
    result["warnings"] = [
        f"模型未能完成详细规划，已从现有总纲生成可编辑的备用结构（缺少转折、人物弧与支线，建议重试）。原因：{reason}"
    ]
    return result


def normalize_volume_routes(
    raw: dict[str, Any], volume: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    start = int(volume.get("chapter_start", 1))
    end = int(volume.get("chapter_end", start))
    expected = end - start + 1
    source = [item for item in raw.get("chapters", []) if isinstance(item, dict)]
    routes = [
        normalize_route(item, start + index) for index, item in enumerate(source[:expected])
    ]
    warnings = []
    if len(routes) != expected:
        warnings.append(
            f"模型应返回 {expected} 章，实际返回 {len(routes)} 章；"
            "未自动填充空洞，请重新拆解本卷。"
        )
    return routes, warnings


def _outline_segment_for_volume(outline: str, volume: dict[str, Any]) -> str:
    title = str(volume.get("title", "")).strip()
    if not title or title not in outline:
        return str(volume.get("goal", ""))
    start = outline.find(title)
    next_heading = re.search(
        r"(?m)^\s*#{0,6}\s*第[一二三四五六七八九十百\d]+卷[：:\s]+",
        outline[start + len(title) :],
    )
    end = (
        start + len(title) + next_heading.start()
        if next_heading
        else len(outline)
    )
    return outline[start:end].strip()


def fallback_volume_routes(
    project: dict[str, Any], volume: dict[str, Any], reason: str
) -> dict[str, Any]:
    start = int(volume.get("chapter_start", 1))
    end = int(volume.get("chapter_end", start))
    count = end - start + 1
    segment = _outline_segment_for_volume(str(project.get("outline", "")), volume)
    sentences = [
        re.sub(r"^[#>*\-\d.、\s]+", "", item).strip()
        for item in re.split(r"\n+|(?<=[。！？；])", segment)
        if re.sub(r"^[#>*\-\d.、\s]+", "", item).strip()
    ]
    protagonist = next(
        (
            str(item.get("name", "")).strip()
            for item in project.get("characters", [])
            if str(item.get("name", "")).strip()
        ),
        str(project.get("title", "本书")),
    )
    phase_labels = (
        "承接余波",
        "线索浮现",
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
    )
    existing = project.get("chapters", [])
    routes = []
    for offset in range(count):
        number = start + offset
        phase_index = min(
            len(phase_labels) - 1,
            round(offset * (len(phase_labels) - 1) / max(1, count - 1)),
        )
        source_index = min(
            len(sentences) - 1,
            round(offset * (len(sentences) - 1) / max(1, count - 1)),
        ) if sentences else 0
        source = sentences[source_index][:100] if sentences else ""
        old = existing[number - 1] if number - 1 < len(existing) else {}
        old_title = str(old.get("title", "")).strip()
        generic = bool(
            re.fullmatch(r"第[一二三四五六七八九十百千万\d]+章", old_title)
        )
        title = (
            old_title
            if old_title and not generic
            else f"{phase_labels[phase_index]}"
        )
        goal = (
            f"{protagonist}在“{source[:55]}”这一局面中完成{phase_labels[phase_index]}，"
            "并造成可验证的新变化。"
            if source
            else f"{protagonist}完成{phase_labels[phase_index]}，推动本卷目标向前一步。"
        )
        routes.append(
            normalize_route(
                {
                    "title": title,
                    "goal": goal,
                    "conflict": str(volume.get("conflict", ""))
                    or str(
                        project.get("planning", {})
                        .get("master", {})
                        .get("central_conflict", "")
                    ),
                    "turning_point": (
                        f"本章行动暴露新的代价，使下一步从“{phase_labels[phase_index]}”"
                        "转向更具体的选择。"
                    ),
                    "ending_hook": (
                        "本章结果直接触发下一章的阻力或选择。"
                        if number < end
                        else str(volume.get("ending_state", "进入下一卷的新局面"))
                    ),
                    "must_keep": list(volume.get("must_keep", []))[:3],
                    "must_avoid": list(volume.get("must_avoid", []))[:3]
                    + ["不得篡改已经写入正文和故事记忆的事实"],
                    "status": "planned",
                },
                number,
            )
        )
    return {
        "volume_id": volume.get("id", ""),
        "chapters": routes,
        "warnings": [
            f"AI未完成分卷拆解，已依据本卷目标、现有总纲和章节节奏建立"
            f"{count}条可编辑备用路线。原因：{reason}"
        ],
        "complete": True,
        "fallback": True,
    }


def fallback_chapter_plan(
    project: dict[str, Any], chapter_index: int, chapter: dict[str, Any], reason: str
) -> dict[str, Any]:
    _, planned_route = find_planning_for_chapter(project, chapter_index)
    route = chapter.get("route") if isinstance(chapter.get("route"), dict) else planned_route
    route = route or {}
    recent_facts = [
        str(item.get("text", ""))
        for item in project.get("memory", {}).get("facts", [])
        if isinstance(item, dict) and item.get("active", True) and item.get("text")
    ][-3:]
    must_keep = list(route.get("must_keep", [])) + recent_facts
    must_avoid = list(route.get("must_avoid", []))
    if not must_avoid:
        must_avoid = ["不得篡改已写正文", "不得提前完成后续章节目标", "不得新增无依据往事"]
    return {
        "goal": str(route.get("goal") or chapter.get("scene_goal", "")),
        "conflict": str(route.get("conflict", "")),
        "must_keep": list(dict.fromkeys(must_keep))[:8],
        "must_avoid": list(dict.fromkeys(must_avoid))[:8],
        "turning_point": str(route.get("turning_point", "")),
        "ending_hook": str(route.get("ending_hook", "")),
        "chapter_type": "escalation",
        "pov_character": "",
        "time_location": "承接上一章",
        "opening_beat": "直接承接上一章结尾的动作、感官或对话压力",
        "scene_beats": [
            value
            for value in (
                str(route.get("conflict", "")),
                str(route.get("turning_point", "")),
                str(route.get("goal", "")),
            )
            if value
        ],
        "emotional_turn": "",
        "thread_actions": [],
        "exit_state": str(route.get("goal", "")),
        "ending_type": "consequence",
        **plan_debate_entry(route, chapter.get("plan"), None),
        "fallback": True,
        "warnings": [
            f"AI未完成单章细化，已将上层章节路线转换为执行计划。原因：{reason}"
        ],
    }


def find_volume(
    project: dict[str, Any], volume_id: str
) -> tuple[int, dict[str, Any]]:
    planning = ensure_planning_defaults(project.get("planning"))
    project["planning"] = planning
    for index, volume in enumerate(planning["volumes"]):
        if volume.get("id") == volume_id:
            return index, volume
    raise KeyError(volume_id)


def find_planning_for_chapter(
    project: dict[str, Any], chapter_index: int
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    number = chapter_index + 1
    planning = ensure_planning_defaults(project.get("planning"))
    for volume in planning["volumes"]:
        if int(volume.get("chapter_start", 1)) <= number <= int(
            volume.get("chapter_end", 0)
        ):
            route = next(
                (
                    item
                    for item in volume.get("chapters", [])
                    if int(item.get("number", 0)) == number
                ),
                None,
            )
            return volume, route
    return None, None


def render_planning_context(project: dict[str, Any], chapter_index: int) -> str:
    planning = ensure_planning_defaults(project.get("planning"))
    master = planning["master"]
    volume, route = find_planning_for_chapter(project, chapter_index)
    parts = [
        "【全书总导演板】",
        f"主题命题：{master.get('theme', '')}",
        f"核心冲突：{master.get('central_conflict', '')}",
        f"故事驱动器：{master.get('story_engine', '')}",
        f"终局状态：{master.get('ending_state', '')}",
        # Prose generation needs the current causal boundary, not the full
        # machine-generated outline. Long master outlines and plot chains
        # contain future names/turns that smaller models copy prematurely.
    ]
    if volume:
        parts.extend(
            [
                "【当前分卷战略】",
                f"卷名：{volume.get('title', '')}（第{volume.get('chapter_start')}"
                f"-{volume.get('chapter_end')}章）",
                f"阶段目标：{volume.get('goal', '')}",
                f"主导冲突：{volume.get('conflict', '')}",
                f"卷末状态：{volume.get('ending_state', '')}",
                f"承接下一卷：{volume.get('bridge_to_next', '')}",
                f"本卷必须保持：{'；'.join(volume.get('must_keep', []))}",
                f"本卷禁止跑偏：{'；'.join(volume.get('must_avoid', []))}",
            ]
        )
    if route:
        parts.extend(
            [
                "【当前章路线卡（战略意图）】",
                f"章名：{route.get('title', '')}",
                f"阶段任务：{route.get('goal', '')}",
                f"预定冲突：{route.get('conflict', '')}",
                f"预定转折：{route.get('turning_point', '')}",
                f"预定钩子：{route.get('ending_hook', '')}",
                f"必须保持：{'；'.join(route.get('must_keep', []))}",
                f"必须避免：{'；'.join(route.get('must_avoid', []))}",
            ]
        )
    parts.append(
        "治理规则：路线卡是尚未写作的战略意图；已接受正文、故事记忆、人物状态和"
        "时间线是权威事实。若二者冲突，保留已发生事实，并对本章路线做最小适配。"
    )
    return "\n".join(parts)


def apply_volume_routes(project: dict[str, Any], volume_id: str) -> dict[str, Any]:
    result = deepcopy(project)
    _, volume = find_volume(result, volume_id)
    routes = volume.get("chapters", [])
    expected = int(volume["chapter_end"]) - int(volume["chapter_start"]) + 1
    if len(routes) != expected:
        raise ValueError("本卷路线尚不完整，请重新执行 AI 拆解后再批量建章")
    chapters = result.setdefault("chapters", [])
    while len(chapters) < int(volume["chapter_end"]):
        number = len(chapters) + 1
        chapters.append(
            {
                "id": str(uuid.uuid4()),
                "title": f"第{number}章",
                "summary": "",
                "content": "",
                "scene_goal": "",
                "plan": {},
            }
        )
    for route in routes:
        number = int(route["number"])
        chapter = chapters[number - 1]
        previous_route_id = str(chapter.get("route_id", "")).strip()
        chapter["volume_id"] = volume["id"]
        chapter["route_id"] = route["id"]
        chapter["route"] = deepcopy(route)
        generic_title = re.fullmatch(r"第[一二三四五六七八九十百千万\d]+章", str(chapter.get("title", "")))
        if not chapter.get("content") and (
            previous_route_id or not chapter.get("title") or generic_title
        ):
            chapter["title"] = route["title"]
        if not chapter.get("content") and (
            previous_route_id or not chapter.get("scene_goal")
        ):
            chapter["scene_goal"] = route["goal"]
    return result

"""Pure validators for planning, audit, memory, and incubation model output."""
from __future__ import annotations

import json
import re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any

from ..core.text import bounded_excerpt
from .schema_validation import require_fields, require_schema

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
        if count >= 5
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
    for field, label, threshold, containment_threshold in (
        ("conflict", "核心冲突", 0.82, 0.64),
        ("turning_point", "关键转折", 0.80, 0.60),
        ("ending_hook", "章末变化", 0.80, 0.45),
    ):
        values = [
            re.sub(r"[\W_]+", "", str(item.get(field, "")))
            for item in combined
        ]
        for right in range(len(values)):
            for left in range(right):
                minimum_length = min(len(values[left]), len(values[right]))
                left_terms = _chinese_bigrams(values[left])
                right_terms = _chinese_bigrams(values[right])
                if field == "ending_hook":
                    hook_stop_bigrams = {
                        "沈衡", "意识", "识到", "发现", "看着", "当场", "亲手",
                        "已经", "终于", "决定", "要求", "下令",
                    }
                    left_terms -= hook_stop_bigrams
                    right_terms -= hook_stop_bigrams
                containment = (
                    len(left_terms & right_terms)
                    / min(len(left_terms), len(right_terms))
                    if left_terms and right_terms
                    else 0.0
                )
                nearby_semantic_repeat = (
                    field == "ending_hook"
                    and right - left <= 2
                    and containment >= 0.27
                )
                if minimum_length >= 12 and (
                    SequenceMatcher(None, values[left], values[right]).ratio()
                    >= threshold
                    or containment >= containment_threshold
                    or nearby_semantic_repeat
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
        "全面",
        "彻底",
        "归零",
        "推行",
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
    for issue in result.get("issues", []):
        if not isinstance(issue, dict):
            raise ValueError("审计问题必须是对象")
        if issue.get("severity") in {"high", "medium"} and not issue.get("evidence"):
            raise ValueError("严重审计问题必须提供 evidence 草稿逐字引文；证据不足请说明待复核，不得虚构")


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
    "始皇帝", "皇帝", "陛下", "龙袍", "玉玺", "尚方宝剑",
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


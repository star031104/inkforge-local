"""Persisted shape of the planning section (master plan, volumes, chapter routes)."""
from __future__ import annotations

import uuid
from typing import Any

from app.story.debate import has_debate, normalize_debate
from app.story.history import parse_year


def empty_planning() -> dict[str, Any]:
    return {
        "version": 1,
        "master": {
            "theme": "",
            "reader_promise": "",
            "central_conflict": "",
            "story_engine": "",
            "ending_state": "",
            "full_outline": "",
            "main_plot": "",
            "theme_progression": "",
            "pacing_plan": "",
            "stakes_ladder": [],
            "major_character_arcs": [],
            "subplots": [],
            "historical_nodes": [],
        },
        "volumes": [],
    }


def ensure_planning_defaults(value: Any) -> dict[str, Any]:
    planning = value if isinstance(value, dict) else {}
    planning.setdefault("version", 1)
    if not isinstance(planning.get("master"), dict):
        planning["master"] = {}
    master = planning["master"]
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
    ):
        master.setdefault(key, "")
    for key in (
        "stakes_ladder",
        "major_character_arcs",
        "subplots",
        "historical_nodes",
    ):
        if not isinstance(master.get(key), list):
            master[key] = []
    if not isinstance(planning.get("volumes"), list):
        planning["volumes"] = []
    # Last 开写前设定预检 result (app.planning.preflight); additive, {} = never run.
    if not isinstance(planning.get("preflight"), dict):
        planning["preflight"] = {}
    clean_volumes = []
    for index, raw in enumerate(planning["volumes"]):
        if not isinstance(raw, dict):
            continue
        volume = raw
        volume.setdefault("id", str(uuid.uuid4()))
        volume.setdefault("number", index + 1)
        volume.setdefault("title", f"第{index + 1}卷")
        volume.setdefault("chapter_start", 1)
        volume.setdefault("chapter_end", volume["chapter_start"])
        for key in (
            "goal",
            "conflict",
            "synopsis",
            "ending_state",
            "bridge_to_next",
            "theme_test",
            "primary_arena",
            "time_span",
            "irreversible_change",
            "character_choice",
            "new_story_question",
        ):
            volume.setdefault(key, "")
        for key in (
            "turning_points",
            "character_arcs",
            "subplots",
            "must_keep",
            "must_avoid",
            "chapters",
        ):
            if not isinstance(volume.get(key), list):
                volume[key] = []
        volume["chapters"] = [
            normalize_route(item, int(volume["chapter_start"]) + route_index)
            for route_index, item in enumerate(volume["chapters"])
            if isinstance(item, dict)
        ]
        clean_volumes.append(volume)
    planning["volumes"] = clean_volumes
    return planning


def _strings(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()][:limit]


def normalize_route(raw: dict[str, Any], number: int) -> dict[str, Any]:
    route = {
        "id": str(raw.get("id") or uuid.uuid4()),
        "number": number,
        "title": str(raw.get("title") or f"第{number}章").strip(),
        "goal": str(raw.get("goal", "")).strip(),
        "conflict": str(raw.get("conflict", "")).strip(),
        "turning_point": str(raw.get("turning_point", "")).strip(),
        "ending_hook": str(raw.get("ending_hook", "")).strip(),
        "must_keep": _strings(raw.get("must_keep"), 6),
        "must_avoid": _strings(raw.get("must_avoid"), 6),
        "quality_warnings": _strings(raw.get("quality_warnings"), 6),
        "status": str(raw.get("status", "planned")),
        # Historical fiction: free-text in-story date plus numeric year (BCE < 0).
        "story_date": str(raw.get("story_date", "") or "").strip()[:60],
        "story_year": parse_year(raw.get("story_year")),
    }
    # Optional debate structure; kept only when filled so ordinary routes stay small.
    debate = normalize_debate(raw.get("debate"))
    if has_debate(debate):
        route["debate"] = debate
    return route

"""Rolling re-planning of the next few chapter routes.

Routes for a volume are decided before any prose exists.  DOME's dynamic
detailed outline and WriteHERE's interleaved planning both show that letting
later plans react to what was actually written keeps long stories coherent.
After each settled chapter the director re-plans the next ``window`` unwritten
chapters (default 3, as in novel-studio's sealed 3-chapter window):

* the volume contract is never touched; only route fields
  ``goal / conflict / turning_point / ending_hook`` may change;
* routes edited or locked by the author (``source=author`` and
  ``author_fingerprint`` written by the planning editor, ``locked``,
  ``author_locked``) or chapters that already have prose are skipped;
* the chapter's execution plan is kept and marked ``plan_stale``;
* the adjusted volume routes must pass the same deterministic route
  validator as before — if the change makes validation worse it is reverted;
* every change is returned as a field-level diff for the task event log.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

from app.llm.json_schemas import REPLAN_SCHEMA
from app.planning.route_validation import _audit_route_checkpoint_prefix

Completion = Callable[..., Awaitable[tuple[dict[str, Any], list[str]]]]

ADJUSTABLE_FIELDS = ("goal", "conflict", "turning_point", "ending_hook")

REPLAN_SYSTEM = (
    "你是长篇小说的分卷编辑。正文已经写到某一章，你要根据“实际发生的事”微调后面几章的路线，"
    "让它们承接真实的结果、人物状态和新出现的问题，避免重复已经完成的事件，也不要遗漏已经埋下的线索。"
    "只能修改 goal/conflict/turning_point/ending_hook 四个字段；不得提前完成卷目标，"
    "不得新增卷契约之外的大事件；如果原路线仍然合适就不要改。"
    "只输出 JSON：{\"adjustments\":[{\"number\":章号,\"changes\":{\"goal\":\"...\"},\"reason\":\"20字内\"}]}"
)


def route_fingerprint(route: dict[str, Any]) -> str:
    """Stable fingerprint of the adjustable route fields (mirrors planning.js)."""
    return "|".join(str(route.get(key, "") or "").strip() for key in ("title", *ADJUSTABLE_FIELDS))


def _locked(route: dict[str, Any], chapter: dict[str, Any]) -> bool:
    return bool(
        route.get("locked")
        or route.get("author_locked")
        or str(route.get("source", "")) in {"author", "manual"}
        or route.get("author_fingerprint")
        or route.get("author_edited_at")
        or str(chapter.get("content", "")).strip()
        or str(chapter.get("authority_state", "")) in {"locked", "accepted", "reviewed"}
        or str(route.get("status", "")) == "written"
    )


def _volume_route(project: dict[str, Any], chapter: dict[str, Any]) -> dict[str, Any] | None:
    route_id = str(chapter.get("route_id", "") or (chapter.get("route") or {}).get("id", ""))
    if not route_id:
        return None
    for volume in project.get("planning", {}).get("volumes", []) or []:
        for route in volume.get("chapters", []) or []:
            if isinstance(route, dict) and str(route.get("id", "")) == route_id:
                return route
    return None


def _route_locked(project: dict[str, Any], chapter: dict[str, Any]) -> bool:
    """Locked on the chapter copy, or edited/locked in the volume route editor."""
    route = chapter.get("route")
    if not isinstance(route, dict) or _locked(route, chapter):
        return True
    planned = _volume_route(project, chapter)
    if planned is None:
        return False
    if _locked(planned, {}):
        return True
    # The planning editor saves into the volume list; until it is applied to
    # the chapter the two differ, and the volume copy is the author's newer one.
    return route_fingerprint(planned) != route_fingerprint(route)


def replan_targets(project: dict[str, Any], index: int, window: int) -> list[int]:
    chapters = project.get("chapters", [])
    volume_id = str(chapters[index].get("volume_id", "")) if 0 <= index < len(chapters) else ""
    targets = []
    for number in range(index + 1, min(len(chapters), index + 1 + window)):
        chapter = chapters[number]
        route = chapter.get("route")
        if not isinstance(route, dict) or not route:
            continue
        if volume_id and str(chapter.get("volume_id", "")) != volume_id:
            break  # never re-plan across a volume boundary
        if _route_locked(project, chapter):
            continue
        targets.append(number)
    return targets


def _volume_for(project: dict[str, Any], chapter: dict[str, Any]) -> dict[str, Any] | None:
    for volume in project.get("planning", {}).get("volumes", []):
        if str(volume.get("id", "")) == str(chapter.get("volume_id", "")):
            return volume
    return None


def _happened(project: dict[str, Any], index: int) -> str:
    chapters = project.get("chapters", [])
    lines = []
    for number in range(max(0, index - 2), index + 1):
        chapter = chapters[number]
        settlement = chapter.get("settlement", {}) if isinstance(chapter.get("settlement"), dict) else {}
        lines.append(
            f"第{number + 1}章《{chapter.get('title', '')}》：{chapter.get('summary', '')}"
            + (f"｜章末状态：{settlement.get('closing_state')}" if settlement.get("closing_state") else "")
            + (f"｜不可逆变化：{'；'.join(settlement.get('irreversible_changes', [])[:4])}" if settlement.get("irreversible_changes") else "")
            + (f"｜悬而未决：{'；'.join(settlement.get('open_questions', [])[:4])}" if settlement.get("open_questions") else "")
        )
    return "\n".join(lines)


def _volume_brief(volume: dict[str, Any] | None) -> str:
    if not volume:
        return "（无卷契约）"
    keys = ("title", "goal", "primary_arena", "ending_state", "irreversible_change", "bridge_to_next")
    return json.dumps({key: volume.get(key, "") for key in keys if volume.get(key)}, ensure_ascii=False)


async def rolling_replan(
    project: dict[str, Any],
    index: int,
    settings: dict[str, Any],
    completion: Completion,
    *,
    window: int = 3,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(project, report)``; ``project`` is a modified copy when routes changed."""
    report: dict[str, Any] = {"targets": [], "changes": [], "reverted": False, "warnings": []}
    targets = replan_targets(project, index, window)
    report["targets"] = [number + 1 for number in targets]
    if not targets:
        return project, report
    chapters = project["chapters"]
    volume = _volume_for(project, chapters[targets[0]])
    upcoming = [
        {"number": number + 1, **{key: chapters[number]["route"].get(key, "") for key in ("title", *ADJUSTABLE_FIELDS)}}
        for number in targets
    ]
    user = (
        f"【卷契约（不可修改）】\n{_volume_brief(volume)}\n\n"
        f"【实际已经发生】\n{_happened(project, index)}\n\n"
        f"【后续待写路线】\n{json.dumps(upcoming, ensure_ascii=False, indent=1)}"
    )
    try:
        result, warnings = await completion(
            settings,
            [{"role": "system", "content": REPLAN_SYSTEM}, {"role": "user", "content": user}],
            temperature=0.3,
            max_tokens=900,
            timeout_seconds=150,
            workload="planning",
            schema=REPLAN_SCHEMA,
        )
        report["warnings"].extend(warnings)
    except Exception as exc:  # noqa: BLE001 - re-planning is best effort
        report["warnings"].append(f"滚动重规划失败，沿用原路线：{exc}")
        return project, report
    allowed_numbers = {number + 1 for number in targets}
    proposals: list[dict[str, Any]] = []
    for item in result.get("adjustments", []) if isinstance(result.get("adjustments"), list) else []:
        if not isinstance(item, dict):
            continue
        try:
            number = int(item.get("number", 0))
        except (TypeError, ValueError):
            continue
        if number not in allowed_numbers:
            continue
        changes = item.get("changes", {}) if isinstance(item.get("changes"), dict) else {}
        route = chapters[number - 1]["route"]
        diff = {}
        for key in ADJUSTABLE_FIELDS:
            value = str(changes.get(key, "") or "").strip()
            if not value or value == str(route.get(key, "")).strip() or len(value) > 400:
                continue
            diff[key] = {"before": route.get(key, ""), "after": value}
        if diff:
            proposals.append({
                "chapter": number, "title": route.get("title", ""),
                "reason": str(item.get("reason", ""))[:60], "fields": diff,
            })
    if not proposals:
        return project, report
    updated, applied = apply_replan_changes(project, proposals, index)
    if volume is not None and applied:
        new_volume = _volume_for(updated, updated["chapters"][targets[0]])
        old_routes = list(volume.get("chapters", []))
        positions = {
            str(route.get("id", "")): position for position, route in enumerate(old_routes) if route.get("id")
        }
        try:
            before_valid, _ = _audit_route_checkpoint_prefix(project, volume, old_routes)
        except Exception:  # noqa: BLE001 - an unvalidatable volume accepts no changes
            before_valid = -1
        validated = []
        for change in applied:
            route_id = str(chapters[change["chapter"] - 1].get("route_id", ""))
            position = positions.get(route_id)
            if position is None or before_valid < 0 or position >= before_valid:
                # An earlier route in this volume is already invalid, so the
                # prefix validator would never reach this one: do not accept an
                # unvalidated change.
                report["warnings"].append(
                    f"第{change['chapter']}章路线前已有未通过校验的路线，无法校验本次调整，已保留原路线"
                )
                continue
            validated.append(change)
        if len(validated) != len(applied):
            if not validated:
                return project, report
            updated, applied = apply_replan_changes(project, validated, index)
            new_volume = _volume_for(updated, updated["chapters"][targets[0]])
        try:
            after_valid, issue = _audit_route_checkpoint_prefix(
                updated, new_volume, new_volume.get("chapters", []) if new_volume else []
            )
        except Exception as exc:  # noqa: BLE001 - validator failure means keep originals
            after_valid, issue = -1, str(exc)
        if after_valid < before_valid:
            report["reverted"] = True
            report["warnings"].append(f"滚动重规划未通过路线校验，已恢复原路线：{issue}")
            return project, report
    report["changes"] = applied
    if not applied:
        return project, report
    return updated, report


def apply_replan_changes(
    project: dict[str, Any], changes: list[dict[str, Any]], index: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply field diffs to a copy of ``project``; return ``(copy, applied)``.

    A field is changed only while it still holds the ``before`` value and the
    chapter is still unwritten and not author-owned, so this is also safe to
    replay onto a newer project version after a save conflict.  The execution
    plan is kept and marked stale instead of being wiped (it may be the
    author's own).
    """
    updated = deepcopy(project)
    applied: list[dict[str, Any]] = []
    for change in changes:
        number = int(change.get("chapter", 0) or 0)
        if not 1 <= number <= len(updated.get("chapters", [])):
            continue
        chapter = updated["chapters"][number - 1]
        route = chapter.get("route")
        if not isinstance(route, dict) or _route_locked(updated, chapter):
            continue
        diff = {}
        for key, values in (change.get("fields") or {}).items():
            if key not in ADJUSTABLE_FIELDS:
                continue
            if str(route.get(key, "") or "").strip() != str(values.get("before", "") or "").strip():
                continue  # changed by someone else meanwhile
            route[key] = values["after"]
            diff[key] = values
        if not diff:
            continue
        route["replanned_after_chapter"] = index + 1
        if diff.get("goal"):
            chapter["scene_goal"] = route["goal"]
        if isinstance(chapter.get("plan"), dict) and chapter["plan"]:
            chapter["plan_stale"] = {
                "reason": "rolling_replan", "after_chapter": index + 1, "fields": sorted(diff),
            }
        applied.append({**change, "fields": diff})
    if applied:
        by_id = {
            str(chapter.get("route_id", "")): chapter["route"]
            for chapter in updated["chapters"] if chapter.get("route_id") and isinstance(chapter.get("route"), dict)
        }
        for volume in updated.get("planning", {}).get("volumes", []):
            volume["chapters"] = [
                deepcopy(by_id.get(str(route.get("id", "")), route)) if isinstance(route, dict) else route
                for route in volume.get("chapters", [])
            ]
    return updated, applied

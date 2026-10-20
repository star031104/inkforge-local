from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


def _keys(value: Any) -> list[str]:
    if isinstance(value, str):
        value = re.split(r"[,，\n]", value)
    return [str(item).strip() for item in value or [] if str(item).strip()]


def _regex_key(key: str) -> tuple[str, int] | None:
    if len(key) < 3 or not key.startswith("/"):
        return None
    end = key.rfind("/")
    if end <= 0:
        return None
    flags = re.IGNORECASE if "i" in key[end + 1 :] else 0
    return key[1:end], flags


def key_matches(key: str, text: str, case_sensitive: bool = False) -> bool:
    regex = _regex_key(key)
    if regex:
        try:
            return re.search(regex[0], text, regex[1]) is not None
        except re.error:
            return False
    if case_sensitive:
        return key in text
    return key.casefold() in text.casefold()


def _match_count(keys: list[str], text: str, case_sensitive: bool) -> int:
    return sum(key_matches(key, text, case_sensitive) for key in keys)


def _conditions_match(entry: dict[str, Any], text: str) -> tuple[bool, list[str]]:
    case_sensitive = bool(entry.get("case_sensitive", False))
    primary = _keys(entry.get("keys"))
    matched = [key for key in primary if key_matches(key, text, case_sensitive)]
    if not primary:
        return False, []
    primary_logic = str(entry.get("match", "any")).lower()
    if primary_logic == "all" and len(matched) != len(primary):
        return False, matched
    if primary_logic != "all" and not matched:
        return False, matched

    secondary = _keys(entry.get("secondary_keys"))
    if not secondary:
        return True, matched
    secondary_hits = _match_count(secondary, text, case_sensitive)
    logic = str(entry.get("selective_logic", "and_any")).lower()
    passed = {
        "and_any": secondary_hits > 0,
        "and_all": secondary_hits == len(secondary),
        "not_any": secondary_hits == 0,
        "not_all": secondary_hits < len(secondary),
    }.get(logic, secondary_hits > 0)
    return passed, matched


def _in_scope(
    entry: dict[str, Any], current_chapter: int, active_characters: set[str]
) -> bool:
    if not entry.get("enabled", True):
        return False
    start = max(0, int(entry.get("chapter_start", 0) or 0))
    end = max(0, int(entry.get("chapter_end", 0) or 0))
    if start and current_chapter < start:
        return False
    if end and current_chapter > end:
        return False
    filters = {name.casefold() for name in _keys(entry.get("character_names"))}
    if filters and not filters.intersection(active_characters):
        return False
    return True


def _resolve_groups(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    winners: dict[str, dict[str, Any]] = {}
    plain: list[dict[str, Any]] = []
    for entry in entries:
        group = str(entry.get("inclusion_group", "")).strip()
        if not group:
            plain.append(entry)
            continue
        current = winners.get(group)
        score = (
            len(entry.get("_matched_keys", [])),
            int(entry.get("order", 100)),
        )
        current_score = (
            len(current.get("_matched_keys", [])),
            int(current.get("order", 100)),
        ) if current else (-1, -1)
        if score > current_score:
            winners[group] = entry
    return plain + list(winners.values())


def activate_lore(
    project: dict[str, Any],
    scan_text: str,
    current_chapter_index: int = 0,
    active_character_names: set[str] | None = None,
    max_recursion_steps: int = 2,
) -> list[dict[str, Any]]:
    """Activate deterministic novel lore with SillyTavern-inspired controls.

    Novel generation deliberately omits random trigger probability and timed chat
    effects: reproducibility is more valuable than stochastic lore activation.
    """
    active_names = {name.casefold() for name in (active_character_names or set())}
    current_chapter = current_chapter_index + 1
    available = [
        deepcopy(entry)
        for entry in project.get("world_entries", [])
        if isinstance(entry, dict)
        and _in_scope(entry, current_chapter, active_names)
    ]
    activated: list[dict[str, Any]] = []
    activated_ids: set[str] = set()
    activated_groups: set[str] = set()

    def scan(buffer: str, depth: int) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for entry in available:
            identity = str(entry.get("id", "")) or str(id(entry))
            if identity in activated_ids:
                continue
            group = str(entry.get("inclusion_group", "")).strip()
            if group and group in activated_groups:
                continue
            if depth == 0 and entry.get("delay_until_recursion", False):
                continue
            if depth > 0 and entry.get("non_recursable", False):
                continue
            direct = bool(entry.get("constant", False)) and depth == 0
            matched: list[str] = []
            if not direct:
                direct, matched = _conditions_match(entry, buffer)
            if not direct:
                continue
            entry["_activation_depth"] = depth
            entry["_activation_reason"] = "constant" if entry.get("constant") and depth == 0 else ("direct" if depth == 0 else "recursive")
            entry["_matched_keys"] = matched
            found.append(entry)
        found = _resolve_groups(found)
        for entry in found:
            identity = str(entry.get("id", "")) or str(id(entry))
            activated_ids.add(identity)
            group = str(entry.get("inclusion_group", "")).strip()
            if group:
                activated_groups.add(group)
        return found

    initial = scan(scan_text, 0)
    activated.extend(initial)
    frontier = initial
    for depth in range(1, max(0, max_recursion_steps) + 1):
        recursive_buffer = "\n".join(
            str(entry.get("content", ""))
            for entry in frontier
            if not entry.get("prevent_recursion", False)
        )
        if not recursive_buffer.strip():
            break
        frontier = scan(recursive_buffer, depth)
        if not frontier:
            break
        activated.extend(frontier)

    return sorted(
        activated,
        key=lambda item: (
            int(item.get("order", 100)),
            int(item.get("_activation_depth", 0)),
        ),
    )

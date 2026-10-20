"""Deterministic checks for historical fiction.

Chapters may carry an in-story date (``story_date`` free text plus an optional
numeric ``story_year``; BCE years are negative, e.g. -247).  Characters may
carry ``born_year``/``died_year``/``enters_year`` and the project may list
era-bound terms in ``governance.era_terms``.  When a chapter has a
``story_year`` the draft is checked for

* characters on stage who are already dead, not yet born, or not yet entered;
* era-bound titles/terms used outside their valid year range.

Without a ``story_year`` (or without any of that data) the check returns
immediately, so ordinary projects pay nothing.
"""
from __future__ import annotations

from typing import Any

from .debate_tools import has_debate, normalize_debate
from .evidence_audit import PENALTIES, split_draft_units

TIMELINE_CATEGORY = "历史时间线"
ERA_CATEGORY = "称谓与官制"
# Sentences that only remember or mourn someone do not put them on stage.
MEMORIAL_MARKERS = (
    "已故", "亡故", "身故", "死后", "死前", "生前", "遗言", "遗命", "遗诏", "遗物", "遗书",
    "薨", "崩", "卒", "祭", "灵位", "牌位", "墓", "冢", "当年", "昔年", "昔日", "追忆", "想起", "记得",
)
MAX_EVIDENCE = 2


def render_story_date(chapter: dict[str, Any]) -> str:
    """``故事内时间：…`` prompt line for a chapter, or ``""`` when it has no date."""
    date = chapter.get("story_date", "") if isinstance(chapter, dict) else ""
    return f"故事内时间：{date}" if str(date or "").strip() else ""


def parse_year(value: Any) -> int | None:
    """Numeric year or ``None``; accepts ints and numeric strings such as "-247"."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace("－", "-").replace("−", "-")
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def format_year(year: int) -> str:
    return f"前{-year}年" if year < 0 else f"{year}年"


def _names(character: dict[str, Any]) -> list[str]:
    aliases = character.get("aliases", [])
    if isinstance(aliases, str):
        aliases = aliases.replace("，", ",").split(",")
    names = [str(character.get("name", "")), *(str(item) for item in aliases or [])]
    # Single characters ("王", "公") match far too much prose.
    return list(dict.fromkeys(name.strip() for name in names if len(name.strip()) >= 2))


def _sentences_with(sentences: list[str], terms: list[str]) -> list[str]:
    return [sentence for sentence in sentences if any(term in sentence for term in terms)]


def _on_stage(sentences: list[str]) -> list[str]:
    return [item for item in sentences if not any(marker in item for marker in MEMORIAL_MARKERS)]


def _lifespan_issue(character: dict[str, Any], story_year: int, evidence: list[str]) -> dict[str, Any] | None:
    name = str(character.get("name", "")).strip()
    died = parse_year(character.get("died_year"))
    born = parse_year(character.get("born_year"))
    enters = parse_year(character.get("enters_year"))
    when = format_year(story_year)
    if died is not None and died < story_year:
        severity, message = "high", f"{name}已于{format_year(died)}去世，本章故事时间为{when}，却在正文中登场。"
    elif born is not None and born > story_year:
        severity, message = "high", f"{name}生于{format_year(born)}，本章故事时间为{when}，此时尚未出生。"
    elif enters is not None and enters > story_year:
        severity, message = "medium", f"{name}设定于{format_year(enters)}才登场，本章故事时间为{when}，出场过早。"
    else:
        return None
    return {
        "severity": severity,
        "category": TIMELINE_CATEGORY,
        "message": message,
        "suggestion": "改为追忆/转述，或核对章节故事时间与人物生卒年",
        "evidence": evidence[:MAX_EVIDENCE],
        "source": "history_tools",
    }


def lifespan_issues(characters: list[Any], story_year: int, sentences: list[str]) -> list[dict[str, Any]]:
    issues = []
    for character in characters:
        if not isinstance(character, dict):
            continue
        if all(parse_year(character.get(key)) is None for key in ("died_year", "born_year", "enters_year")):
            continue
        names = _names(character)
        if not names:
            continue
        evidence = _on_stage(_sentences_with(sentences, names))
        if not evidence:
            continue
        issue = _lifespan_issue(character, story_year, evidence)
        if issue:
            issues.append(issue)
    return issues


def normalize_era_terms(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    clean = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("term", "")).strip():
            continue
        clean.append({
            "term": str(item["term"]).strip(),
            "from_year": parse_year(item.get("from_year")),
            "to_year": parse_year(item.get("to_year")),
            "note": str(item.get("note", "") or "").strip(),
        })
    return clean


def era_term_issues(era_terms: list[dict[str, Any]], story_year: int, sentences: list[str]) -> list[dict[str, Any]]:
    issues = []
    for entry in era_terms:
        start, end = entry.get("from_year"), entry.get("to_year")
        if (start is None or story_year >= start) and (end is None or story_year <= end):
            continue
        evidence = _sentences_with(sentences, [entry["term"]])
        if not evidence:
            continue
        span = f"{format_year(start) if start is not None else '不限'}至{format_year(end) if end is not None else '今'}"
        issues.append({
            "severity": "medium",
            "category": ERA_CATEGORY,
            "message": f"“{entry['term']}”仅适用于{span}，本章故事时间为{format_year(story_year)}。"
            + (f"{entry['note']}" if entry.get("note") else ""),
            "suggestion": "换用当时通行的称谓或官制用语",
            "evidence": evidence[:MAX_EVIDENCE],
            "source": "history_tools",
        })
    return issues


CHARACTER_YEAR_FIELDS = ("born_year", "died_year", "enters_year")


def ensure_history_defaults(project: dict[str, Any]) -> dict[str, Any]:
    """Defaults for the optional historical-fiction fields (called by the schema migration)."""
    governance = project.setdefault("governance", {})
    governance["era_terms"] = normalize_era_terms(governance.get("era_terms"))
    for chapter in project.get("chapters", []):
        if not isinstance(chapter, dict):
            continue
        chapter["story_date"] = str(chapter.get("story_date", "") or "").strip()[:60]
        chapter["story_year"] = parse_year(chapter.get("story_year"))
        for holder in (chapter.get("plan"), chapter.get("route")):
            if isinstance(holder, dict) and "debate" in holder:
                debate = normalize_debate(holder["debate"])
                if has_debate(debate):
                    holder["debate"] = debate
                else:
                    holder.pop("debate")
    for character in project.get("characters", []):
        if isinstance(character, dict):
            for key in CHARACTER_YEAR_FIELDS:
                character[key] = parse_year(character.get(key))
    return project


def check_history(project: dict[str, Any], chapter_index: int, draft: str) -> list[dict[str, Any]]:
    """Historical-timeline issues for ``draft``; ``[]`` when the chapter has no year."""
    chapters = project.get("chapters", [])
    if not (0 <= chapter_index < len(chapters)) or not str(draft or "").strip():
        return []
    story_year = parse_year(chapters[chapter_index].get("story_year"))
    if story_year is None:
        return []
    governance = project.get("governance") if isinstance(project.get("governance"), dict) else {}
    era_terms = normalize_era_terms(governance.get("era_terms"))
    characters = project.get("characters", []) if isinstance(project.get("characters"), list) else []
    if not era_terms and not any(
        isinstance(item, dict) and any(item.get(key) not in (None, "") for key in ("died_year", "born_year", "enters_year"))
        for item in characters
    ):
        return []
    sentences = [sentence for _id, _start, sentence in split_draft_units(draft)]
    return lifespan_issues(characters, story_year, sentences) + era_term_issues(era_terms, story_year, sentences)


def apply_history_issues(local_checks: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold history issues into a ``local_quality_check`` result in place."""
    if not issues:
        return local_checks
    local_checks.setdefault("issues", []).extend(issues)
    penalty = sum(PENALTIES.get(item["severity"], 8) for item in issues)
    local_checks["score"] = max(0, int(local_checks.get("score", 100) or 0) - penalty)
    if any(item["severity"] == "high" for item in issues):
        local_checks["verdict"] = "revise"
    return local_checks

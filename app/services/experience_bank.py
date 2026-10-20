"""Per-model experience bank (ConWriter-style).

Every model has habitual failure modes: one leaks what a character cannot know,
another forgets threads, another keeps reusing descriptions.  The bank keeps a
small, decaying tally of *trustworthy* audit findings per model and turns the
most persistent patterns into short corrective hints for the next generation.

Only evidence-verified AI findings and locally detected issues are counted; an
unverified model claim is never learned from.  Everything is deterministic and
stored in ``project["memory"]["experience_bank"]`` so it is rebuildable and
inspectable::

    {"<model>": {
        "chapters": ["c1", "c2", ...],          # last CHAPTER_WINDOW chapter ids
        "rounds": 7,                            # audit rounds recorded
        "patterns": {"<pattern key>": {
            "kind": "knowledge_leak", "category": "人物记忆与能力/知情越界",
            "checker": "character", "weight": 2.3, "count": 4,
            "last_chapter": "c7", "examples": ["……"],
        }},
        "updated_at": "..."}}
"""
from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from app.core.clock import utc_now

DECAY = 0.8  # applied once per new chapter
MIN_WEIGHT = 0.25  # patterns that decayed below this are forgotten
MAX_PATTERNS = 40  # per model
MAX_MODELS = 8
CHAPTER_WINDOW = 12
MAX_EXAMPLES = 3
SEVERITY_WEIGHT = {"high": 1.5, "medium": 1.0, "low": 0.5}
# Sources that are produced by deterministic local checks, not model claims.
LOCAL_SOURCES = frozenset(
    {"local", "本地", "local_check", "local_quality", "manuscript_gate", "regression", "continuity"}
)
# Focused checker → default pattern kind when the model gave no usable
# sub-category.  Keyed by checker code and by checker label (the label alone
# would otherwise keyword-match a different family, e.g. "…与弃坑" → threads).
CHECKER_KINDS = {
    "character": "character_voice", "facts": "facts", "pov": "pov",
    "timeline": "timeline", "world": "world",
}
LABEL_KINDS = {
    "人物记忆与能力": "character_voice", "事实细节": "facts", "视角与语气": "pov",
    "时间线、因果与弃坑": "timeline", "世界规则与地理": "world",
}
OTHER_LABEL = "其他反复问题"
OTHER_HINT = "动笔前对照上一轮审校意见，避免同类问题再次出现"

# (kind, keywords, label, corrective hint).  Order matters: first match wins.
PATTERN_KINDS: tuple[tuple[str, tuple[str, ...], str, str], ...] = (
    ("knowledge_leak", ("知情", "不该知道", "信息差", "知道", "泄露", "剧透"), "人物知情越界",
     "动笔前核对每个角色的知情范围，未知信息不得出现在其对白或心理中"),
    ("pov", ("视角", "人称", "内心"), "视角越界",
     "严格保持本章视角人物的限知，不写其看不见、听不到的事和他人内心"),
    ("threads", ("伏笔", "回收", "弃坑", "钩子", "线索"), "伏笔推进失误",
     "逐条对照本章应推进的线索，未满足回收条件不得提前兑现，也不得遗忘"),
    ("timeline", ("时间", "因果", "先后", "顺序"), "时间线与因果",
     "写每个场景前确认时间点与前因，事件先后和因果必须与已知时间线一致"),
    ("facts", ("外貌", "伤势", "道具", "数量", "数字", "称谓", "地名", "人名", "事实"), "事实细节矛盾",
     "涉及外貌、伤势、道具归属和称谓时先查既有记录，不得与已知事实矛盾"),
    ("world", ("世界", "规则", "设定", "地理", "距离", "制度", "能力体系", "器物"), "世界规则违背",
     "使用能力、制度和地理时遵守世界书限制，不得为剧情方便临时改规则"),
    ("era_jargon", ("时代语汇", "术语", "现代"), "时代语汇失真",
     "把抽象现代术语换成当时人物能看见、争论和付出代价的具体事物"),
    ("character_voice", ("人格", "说话方式", "ooc", "性格", "能力", "人物"), "人物言行漂移",
     "保持人物既定的人格核心与说话方式，态度和能力变化必须有铺垫"),
    ("repetition", ("套话", "疲劳", "重复", "复用", "句式", "循环", "整齐"), "重复与套话",
     "避免复用近期用过的描写和句式，换成新的动作、感官细节或直接删去"),
    ("structure", ("截断", "长度", "收束", "目标", "转折", "路线", "节奏", "结尾"), "章节结构不完整",
     "按路线完成本章目标、转折和收束，结尾必须落在明确的状态变化上"),
    ("style", ("文风", "元话语", "格式", "标点", "抽象"), "文风与格式",
     "只输出小说正文，保持本书文风，少用抽象概括，多写具体场景"),
)


def _model_key(model: Any) -> str:
    return str(model or "").strip()[:160] or "default"


def _other(base: str) -> tuple[str, str, str]:
    # The model-written category only names the bucket (UI); the prompt hint is
    # a fixed template so model text never reaches the writer prompt.
    base = re.sub(r"\s+", "", base)[:20] or "其他"
    return f"other:{base}", base, OTHER_HINT


def _match_keywords(text: str) -> tuple[str, str, str] | None:
    folded = text.casefold()
    for kind, keywords, label, hint in PATTERN_KINDS:
        if any(keyword.casefold() in folded for keyword in keywords):
            return kind, label, hint
    return None


def _known_kind(kind: str) -> tuple[str, str, str]:
    for known, _keywords, label, hint in PATTERN_KINDS:
        if known == kind:
            return known, label, hint
    raise KeyError(kind)


def _classify(category: str, checker: str) -> tuple[str, str, str]:
    """Map an issue to a stable pattern kind.

    An explicit sub-category ("人物记忆与能力/知情越界" → "知情越界") is tried
    first; then the focused checker's own family (by code or label); only then
    the whole category plus checker name.
    """
    head, _sep, sub = category.rpartition("/")
    if head and sub:
        found = _match_keywords(sub)
        if found:
            return found
    family = CHECKER_KINDS.get(checker) or LABEL_KINDS.get((head or category).split("/")[0])
    if family:
        return _known_kind(family)
    found = _match_keywords(f"{category} {checker}")
    if found:
        return found
    return _other(sub or category)


def _kind_meta(item: dict[str, Any], *, for_prompt: bool = False) -> tuple[str, str]:
    kind = str(item.get("kind", ""))
    for known, _keywords, label, hint in PATTERN_KINDS:
        if known == kind:
            return label, hint
    if for_prompt:
        return OTHER_LABEL, OTHER_HINT
    _kind, label, hint = _other(kind.removeprefix("other:"))
    return label, hint


def _trustworthy(issue: dict[str, Any]) -> bool:
    if issue.get("evidence_verified") is True:
        return True
    if issue.get("evidence_verified") is False:
        return False
    return str(issue.get("source", "")).strip().lower() in LOCAL_SOURCES


def _example(issue: dict[str, Any]) -> str:
    evidence = issue.get("evidence")
    if isinstance(evidence, list):
        for item in evidence:
            quote = item.get("quote", "") if isinstance(item, dict) else item
            if str(quote or "").strip():
                return str(quote).strip()[:40]
    text = str(issue.get("message") or issue.get("issue") or issue.get("suggestion") or "").strip()
    return text[:40]


def _bank(project: dict[str, Any]) -> dict[str, Any]:
    memory = project.setdefault("memory", {})
    bank = memory.get("experience_bank")
    if not isinstance(bank, dict):
        bank = {}
        memory["experience_bank"] = bank
    return bank


def _chapter_ids(item: dict[str, Any]) -> list[str]:
    """Distinct-chapter ids of a pattern, backfilling legacy entries.

    Banks written before per-pattern ``chapter_ids`` existed only carry a
    ``count``; recomputing ``count = len(chapter_ids)`` would silently reset
    them.  Such patterns get deterministic placeholder ids ``#legacy1`` …
    ``#legacyN`` (N = min(count, CHAPTER_WINDOW)) so the history survives and
    two bank copies (server and browser) backfill identically.
    """
    ids = [str(value) for value in item.get("chapter_ids", []) or [] if str(value)]
    if ids:
        return ids
    try:
        count = int(item.get("count", 0) or 0)
    except (TypeError, ValueError):
        count = 0
    return [f"#legacy{index}" for index in range(1, min(max(count, 0), CHAPTER_WINDOW) + 1)]


def _prune(entry: dict[str, Any], patterns: dict[str, Any]) -> dict[str, Any]:
    kept = sorted(
        (item for item in patterns.values()
         if isinstance(item, dict) and float(item.get("weight", 0.0)) >= MIN_WEIGHT),
        key=lambda item: (-float(item["weight"]), -int(item.get("count", 0)), str(item.get("kind", ""))),
    )[:MAX_PATTERNS]
    return {item["kind"]: item for item in kept}


def record_audit_issues(
    project: dict[str, Any],
    model: str,
    issues: list[dict[str, Any]],
    *,
    chapter_id: str = "",
) -> dict[str, Any]:
    """Fold one audit round's trustworthy issues into ``model``'s tally.

    Weights decay by ``DECAY`` once per *new* chapter (re-auditing the same
    chapter does not age the bank), so patterns the model stopped producing
    fade out.  ``count`` is the number of *distinct chapters* a pattern was
    found in: re-auditing one chapter (revision loops, manual re-runs) never
    turns a single bad chapter into a "recurring" lesson.

    Returns the delta of this round (``merge_experience_delta`` applies it to
    another copy of the bank, e.g. the browser's).
    """
    if not isinstance(project, dict):
        return {}
    bank = _bank(project)
    key = _model_key(model)
    entry = bank.get(key)
    if not isinstance(entry, dict):
        entry = {"chapters": [], "rounds": 0, "patterns": {}}
    patterns = entry.get("patterns") if isinstance(entry.get("patterns"), dict) else {}
    chapters = [str(item) for item in entry.get("chapters", []) if str(item)]
    rounds = int(entry.get("rounds", 0) or 0) + 1
    chapter = str(chapter_id or "")
    decay = 1.0
    if not chapter or not chapters or chapters[-1] != chapter:
        decay = DECAY
        for item in patterns.values():
            item["weight"] = round(float(item.get("weight", 0.0)) * DECAY, 4)
        if chapter:
            chapters = [item for item in chapters if item != chapter] + [chapter]
    # Without a chapter id every round is its own occurrence.
    occurrence = chapter or f"#round{rounds}"
    seen: set[str] = set()
    touched: dict[str, dict[str, Any]] = {}
    for issue in issues or []:
        if not isinstance(issue, dict) or not _trustworthy(issue):
            continue
        category = str(issue.get("category", "") or "").strip()
        checker = str(issue.get("checker", "") or "").strip()
        kind, _label, _hint = _classify(category, checker)
        if kind in seen:
            continue
        seen.add(kind)
        item = patterns.setdefault(kind, {
            "kind": kind, "category": category[:60], "checker": checker[:40],
            "weight": 0.0, "count": 0, "last_chapter": "", "examples": [], "chapter_ids": [],
        })
        seen_in = _chapter_ids(item)
        if occurrence not in seen_in:
            severity = str(issue.get("severity", "medium")).lower()
            item["weight"] = round(float(item.get("weight", 0.0)) + SEVERITY_WEIGHT.get(severity, 1.0), 4)
            seen_in = (seen_in + [occurrence])[-CHAPTER_WINDOW:]
        item["chapter_ids"] = seen_in
        item["count"] = len(seen_in)
        item["category"] = category[:60] or item.get("category", "")
        item["last_chapter"] = chapter
        example = _example(issue)
        if example:  # UI only: never rendered into a prompt
            examples = [value for value in item.get("examples", []) if value != example]
            item["examples"] = (examples + [example])[-MAX_EXAMPLES:]
        touched[kind] = item
    now = utc_now()
    entry.update(
        patterns=_prune(entry, patterns),
        chapters=chapters[-CHAPTER_WINDOW:],
        rounds=rounds,
        updated_at=now,
    )
    bank[key] = entry
    if len(bank) > MAX_MODELS:
        for stale in sorted(bank, key=lambda name: str(bank[name].get("updated_at", "")))[: len(bank) - MAX_MODELS]:
            bank.pop(stale, None)
    return {
        "model": key,
        "chapter_id": chapter,
        "decay": decay,
        "patterns": {kind: deepcopy(item) for kind, item in touched.items() if kind in entry["patterns"]},
        "updated_at": now,
    }


def merge_experience_delta(bank: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Additively apply one ``record_audit_issues`` delta to another bank copy.

    Mirrors ``mergeExperienceDelta`` in ``static/js/features/editorial.js``:
    per-pattern chapter ids are unioned, so concurrent audits of different
    chapters both survive instead of the last response clobbering the first.
    """
    if not isinstance(bank, dict) or not isinstance(delta, dict) or not delta.get("model"):
        return bank
    key = str(delta["model"])
    entry = bank.get(key) if isinstance(bank.get(key), dict) else {"chapters": [], "rounds": 0, "patterns": {}}
    patterns = entry.get("patterns") if isinstance(entry.get("patterns"), dict) else {}
    chapters = [str(item) for item in entry.get("chapters", []) if str(item)]
    chapter = str(delta.get("chapter_id", "") or "")
    decay = float(delta.get("decay", 1.0) or 1.0)
    new_chapter = not chapter or not chapters or chapters[-1] != chapter
    incoming = delta.get("patterns") if isinstance(delta.get("patterns"), dict) else {}
    for kind, item in patterns.items():
        if new_chapter and isinstance(item, dict):
            item["weight"] = round(float(item.get("weight", 0.0)) * decay, 4)
    for kind, item in incoming.items():
        if not isinstance(item, dict):
            continue
        current = patterns.get(kind)
        if not isinstance(current, dict):
            patterns[kind] = deepcopy(item)
            continue
        ids = _chapter_ids(current)
        ids += [value for value in _chapter_ids(item) if value not in ids]
        examples = [value for value in current.get("examples", []) if value not in item.get("examples", [])]
        current.update(
            weight=max(float(current.get("weight", 0.0)), float(item.get("weight", 0.0))),
            chapter_ids=ids[-CHAPTER_WINDOW:],
            count=len(ids[-CHAPTER_WINDOW:]),
            category=item.get("category", current.get("category", "")),
            last_chapter=item.get("last_chapter", current.get("last_chapter", "")),
            examples=(examples + list(item.get("examples", [])))[-MAX_EXAMPLES:],
        )
    if chapter and new_chapter:
        chapters = [item for item in chapters if item != chapter] + [chapter]
    entry.update(
        patterns=_prune(entry, patterns),
        chapters=chapters[-CHAPTER_WINDOW:],
        rounds=int(entry.get("rounds", 0) or 0) + 1,
        updated_at=str(delta.get("updated_at", "")),
    )
    bank[key] = entry
    return bank


def _ranked(project: dict[str, Any], model: str) -> list[dict[str, Any]]:
    if not isinstance(project, dict):
        return []
    bank = project.get("memory", {}).get("experience_bank", {})
    entry = bank.get(_model_key(model)) if isinstance(bank, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("patterns"), dict):
        return []
    return sorted(
        (item for item in entry["patterns"].values() if isinstance(item, dict)),
        key=lambda item: (-float(item.get("weight", 0.0)), -int(item.get("count", 0)), str(item.get("kind", ""))),
    )


def experience_hints(project: dict[str, Any], model: str, limit: int = 3) -> list[str]:
    """Top recurring patterns as short Chinese corrective hints.

    A pattern needs at least two *distinct chapters* to become a hint, so one
    bad chapter does not steer every later one.  Hints are fixed templates:
    no model-written category or quoted prose ever reaches the writer prompt
    (quoting the bad sentence would also prime the model to repeat it).
    """
    hints: list[str] = []
    for item in _ranked(project, model):
        if int(item.get("count", 0)) < 2:
            continue
        label, hint = _kind_meta(item, for_prompt=True)
        text = f"【{label}】{hint}"
        if text in hints:
            continue
        hints.append(text)
        if len(hints) >= max(0, int(limit)):
            break
    return hints


def experience_summary(project: dict[str, Any], model: str) -> dict[str, Any]:
    """Compact view of a model's bank for the UI."""
    bank = project.get("memory", {}).get("experience_bank", {}) if isinstance(project, dict) else {}
    entry = bank.get(_model_key(model), {}) if isinstance(bank, dict) else {}
    patterns = []
    for item in _ranked(project, model)[:10]:
        label, _hint = _kind_meta(item)
        _prompt_label, prompt_hint = _kind_meta(item, for_prompt=True)
        patterns.append({
            "kind": item.get("kind", ""),
            "label": label,
            "weight": round(float(item.get("weight", 0.0)), 2),
            "count": int(item.get("count", 0)),
            "last_chapter": item.get("last_chapter", ""),
            "example": (item.get("examples") or [""])[-1],
            "hint": prompt_hint if int(item.get("count", 0)) >= 2 else "",
        })
    return {
        "model": _model_key(model),
        "rounds": int(entry.get("rounds", 0)) if isinstance(entry, dict) else 0,
        "chapters": len(entry.get("chapters", [])) if isinstance(entry, dict) else 0,
        "patterns": patterns,
        "hints": experience_hints(project, model),
    }

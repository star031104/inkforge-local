"""Pure prose sizing, cleanup, continuity guards, and candidate checks."""
from __future__ import annotations

import json
import math
import re
from typing import Any

from ..providers import settings_for_workload
from .planning_validation import _chinese_bigrams, _planning_similarity

def _prose_char_count(text: str) -> int:
    return len(re.sub(r"\s+", "", str(text or "")))


def _resolved_prose_target(project: dict[str, Any], body: Any) -> int:
    target = int(body.target_words or project.get("settings", {}).get("target_words", 1200))
    if body.mode == "rewrite" and body.selection.strip():
        selected = _prose_char_count(body.selection)
        if selected:
            target = max(100, selected)
    return max(100, min(10000, target))


def _effective_prose_settings(
    settings: dict[str, Any], target_chars: int, workload: str = "prose"
) -> dict[str, Any]:
    """Reserve enough output space for Chinese prose without changing saved settings.

    A model can stop before max_tokens, so this is only a ceiling. The follow-up
    repair below handles early stops. 8192 keeps requests practical for Qwen3-8B
    while still supporting chapters around 5k Chinese characters.
    """
    result = settings_for_workload(settings, workload)
    result["_workload"] = workload
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


def _trim_incomplete_prose_tail(text: str) -> tuple[str, str]:
    """Drop only a short unfinished tail after the last balanced sentence.

    Providers sometimes finish normally at their token ceiling, so no transport
    exception is raised even though the final sentence is cut.  Reusing that
    checkpoint used to create an infinite pause/resume loop.  This repair is
    deliberately conservative: it never removes more than 12% or 600 chars.
    """
    value = str(text or "").rstrip()
    if not value or not _prose_looks_truncated(value):
        return value, ""
    maximum_cut = min(600, max(40, int(len(value) * 0.12)))
    lower_bound = max(0, len(value) - maximum_cut)
    terminals = "。！？!?……”’）】"
    for position in range(len(value) - 1, lower_bound - 1, -1):
        if value[position] not in terminals:
            continue
        candidate = value[: position + 1].rstrip()
        if len(candidate) < 100 or _prose_looks_truncated(candidate):
            continue
        removed = value[position + 1 :].strip()
        if not removed:
            return candidate, ""
        return candidate, f"已移除末尾 {len(removed)} 个未完成字符"
    return value, ""


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
        # Match the deterministic quality gate: punctuation-only differences do
        # not make a repeated prose paragraph substantively new.
        key = re.sub(
            r"[\s，。！？、；：,.!?;:'\"“”‘’—…（）()]", "", paragraph
        )
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


_PROSE_FINAL_MARKER_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:\*{0,2})?(?:"
    r"最终(?:版本|正文|稿件|成稿)|完整(?:正文|修订稿)|正文(?:如下)?|"
    r"final\s+(?:version|draft|polish(?:ed\s+draft)?)|"
    r"drafting\s+the\s+(?:final\s+)?content|proceeding\s+to\s+output"
    r")(?:\*{0,2})?\s*[:：-]?\s*$"
)

_PROSE_META_LINE_RE = re.compile(
    r"(?im)^\s*(?:#{1,6}\s*)?(?:\*{0,2})?(?:"
    r"评价|点评|审校|修正计划|修改计划|写作说明|创作说明|约束清单|"
    r"author['’]?s\s+note|key\s+constraints?|final\s+polish|"
    r"analysis|reasoning|revision\s+plan|checklist|let['’]?s\s+(?:write|revise)"
    r")(?:\*{0,2})?\s*[:：-]?.*$"
)

_PROSE_INLINE_FINAL_MARKER_RE = re.compile(
    r"(?is)(?:\*{0,2})?(?:drafting\s+the\s+(?:final\s+)?content|"
    r"proceeding\s+to\s+output|let['’]?s\s+write(?:\s+the\s+final\s+version)?)"
    r"[.。…:]*(?:\*{0,2})?\s*(?:\([^\u3400-\u9fff]{0,160}\)[.。]?\s*)?"
)

_PROSE_INLINE_META_RE = re.compile(
    r"(?is)(?:\n\s*|\s+\*\*)"
    r"(?:评价|点评|修正计划|修改计划|author['’]?s\s+note|"
    r"key\s+constraints?|final\s+polish|revision\s+plan)"
    r"\s*[:：]"
)


def _sanitize_generated_prose(text: str) -> tuple[str, list[str]]:
    """Extract the final narrative when a model leaks planning or multiple drafts.

    The cleaner is deliberately conservative: it only cuts at explicit standalone
    output/meta labels.  It never paraphrases prose or guesses which ordinary
    narrative paragraph is better.
    """
    raw = str(text or "").replace("\r\n", "\n").strip()
    if not raw:
        return "", []
    notes: list[str] = []
    without_thinking = re.sub(
        r"(?is)<think\b[^>]*>.*?</think\s*>", "", raw
    ).strip()
    if without_thinking != raw:
        notes.append("已移除模型思考过程")
    raw = without_thinking

    chosen = raw
    final_matches = list(_PROSE_FINAL_MARKER_RE.finditer(raw)) + list(
        _PROSE_INLINE_FINAL_MARKER_RE.finditer(raw)
    )
    final_matches.sort(key=lambda item: item.start())
    for match in reversed(final_matches):
        tail = raw[match.end():].strip()
        if _prose_char_count(tail) >= 100:
            chosen = tail
            notes.append("已从多稿输出中提取最后正文")
            break

    # If the response ends with an explicit critique/plan, keep only the prose
    # before it.  A short label near the beginning is not enough to trigger a cut.
    for match in _PROSE_META_LINE_RE.finditer(chosen):
        prefix = chosen[:match.start()].rstrip()
        if _prose_char_count(prefix) >= 100:
            chosen = prefix
            notes.append("已移除正文后的评价或写作说明")
            break
    for match in _PROSE_INLINE_META_RE.finditer(chosen):
        prefix = chosen[:match.start()].rstrip()
        if _prose_char_count(prefix) >= 100:
            chosen = prefix
            notes.append("已移除正文后的评价或写作说明")
            break

    chosen = re.sub(r"(?m)^\s*```(?:markdown|text|chinese|中文)?\s*$", "", chosen)
    chosen = re.sub(r"(?m)^\s*```\s*$", "", chosen)
    chosen = re.sub(
        r"(?im)^\s*(?:以下(?:是|为).{0,18}(?:正文|成稿)|下面(?:是|为).{0,18}(?:正文|成稿))\s*[:：]?\s*",
        "",
        chosen,
        count=1,
    ).strip()
    return chosen, list(dict.fromkeys(notes))


def _director_candidate_gate_failures(text: str, target_chars: int) -> list[str]:
    """Hard failures that must never be accepted as a chapter, even in debt mode."""
    value = str(text or "")
    failures: list[str] = []
    if re.search(r"(?is)<think\b|```|\b(?:analysis|reasoning)\b", value):
        failures.append("正文仍含模型思考、代码块或解释性元话语")
    if _PROSE_META_LINE_RE.search(value) or _PROSE_FINAL_MARKER_RE.search(value):
        failures.append("正文仍含评价、修订计划或多稿分隔标签")
    count = _prose_char_count(value)
    hard_max = max(int(target_chars * 3.0), target_chars + 1800)
    if count > hard_max:
        failures.append(f"正文约 {count} 字，超过目标 {target_chars} 字的安全上限")
    if _prose_looks_truncated(value):
        failures.append("正文疑似在半句中截断")
    return failures




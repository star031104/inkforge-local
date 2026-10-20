from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, OrderedDict, defaultdict
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Any, Iterable

from app.editorial_workflow import incomplete_memory_stages
from app.memory import normalize_thread_status, thread_lifecycle


PUNCTUATION_RE = re.compile(r"[\s，。！？、；：,.!?;:'\"“”‘’—…（）()《》【】\[\]·]")
SENTENCE_RE = re.compile(r"(?<=[。！？!?])|\n+")

# These words are not forbidden.  They become a problem when a historical novel
# repeatedly uses them as the narrator's default vocabulary rather than letting
# the era, characters and concrete actions carry the idea.
MODERN_JARGON = (
    "动态阈值", "柔性冗余", "系统架构", "架构师", "参数", "模型", "算法",
    "指标", "量化", "数据", "数据库", "变量", "机制", "闭环", "反馈循环",
    "容错", "迭代", "版本", "系统性风险", "资源配置", "财政定价", "透明度",
    "可视化", "绩效", "KPI", "概率", "边际", "效率最优", "成本收益",
)

# Generic, book-independent AI-prose clichés.  Book-specific repetitions are
# learned per project (see ``learn_fatigue_phrases``) instead of being
# hard-coded here.
TEMPLATE_PHRASES = (
    "空气中弥漫着", "仿佛在诉说", "打破了这份宁静", "命运的齿轮",
    "目光如炬", "一字一顿", "不容置疑", "陷入了沉思", "若有所思",
    "微微一愣", "嘴角勾起", "深吸一口气", "与此同时", "就在这时",
    "极其微弱", "具象化为", "烛火摇曳", "指节因为用力而发白",
    "声音低得几乎听不见", "居高临下地看着", "眼中闪过一丝", "心中一凛",
    "仿佛过了一个世纪", "一股寒意", "嘴角微微上扬",
    "倒吸一口凉气", "瞳孔微缩", "空气仿佛凝固", "死死地盯着",
)

FATIGUE_EDGE_CHARS = set("的一是在了着也与及或而就都将把被让向从于为有无这那其之并却又更很")


def normalize_text(text: str) -> str:
    return PUNCTUATION_RE.sub("", str(text or ""))


def _paragraphs(text: str, minimum: int = 28) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"\n+", str(text or ""))
        if len(normalize_text(item)) >= minimum
    ]


def _sentences(text: str, minimum: int = 24) -> list[str]:
    return [
        item.strip()
        for item in SENTENCE_RE.split(str(text or ""))
        if len(normalize_text(item)) >= minimum
    ]


def _chapter_rows(project: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, chapter in enumerate(project.get("chapters", []), start=1):
        if not isinstance(chapter, dict):
            continue
        rows.append(
            {
                "number": index,
                "id": str(chapter.get("id", "")),
                "title": str(chapter.get("title", f"第{index}章")),
                "content": str(chapter.get("content", "")),
                "summary": str(chapter.get("summary", "")),
                "scene_goal": str(chapter.get("scene_goal", "")),
            }
        )
    return rows


def prior_manuscript_text(
    project: dict[str, Any], chapter_id: str, *, max_chars: int = 800_000
) -> str:
    """Return chapters before chapter_id, newest first within a bounded corpus."""
    chunks: list[str] = []
    size = 0
    for chapter in project.get("chapters", []):
        if not isinstance(chapter, dict):
            continue
        if str(chapter.get("id", "")) == str(chapter_id):
            break
        content = str(chapter.get("content", "")).strip()
        if content:
            chunks.append(content)
    selected: list[str] = []
    for content in reversed(chunks):
        remaining = max_chars - size
        if remaining <= 0:
            break
        selected.append(content[-remaining:])
        size += min(len(content), remaining)
    return "\n\n".join(reversed(selected))


def cross_chapter_reuse(draft: str, prior_text: str) -> dict[str, Any]:
    prior_paragraphs = {normalize_text(item): item for item in _paragraphs(prior_text, 28)}
    prior_sentences = {normalize_text(item): item for item in _sentences(prior_text, 24)}
    paragraphs: list[str] = []
    sentences: list[str] = []
    for item in _paragraphs(draft, 28):
        key = normalize_text(item)
        if key in prior_paragraphs:
            paragraphs.append(item[:120])
    for item in _sentences(draft, 24):
        key = normalize_text(item)
        if key in prior_sentences:
            sentences.append(item[:120])
    return {
        "paragraph_count": len(dict.fromkeys(paragraphs)),
        "sentence_count": len(dict.fromkeys(sentences)),
        "paragraphs": list(dict.fromkeys(paragraphs))[:5],
        "sentences": list(dict.fromkeys(sentences))[:8],
    }


MODERN_PREMISE_MARKERS = ("穿越", "重生", "现代人", "现代灵魂", "现代知识", "来自现代", "转世", "魂穿", "系统流")
DIALOGUE_RE = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"\n]*\"")
POV_NARRATION_WEIGHT = 3


def jargon_policy(project: dict[str, Any] | None) -> dict[str, Any]:
    """Effective era-vocabulary policy for a project.

    * ``governance.term_allowlist``: terms that never count (e.g. a
      transmigrator's signature concept that the story is about).
    * ``governance.pov_modern_allowed``: ``True``/``False`` set by the author,
      or ``None`` to infer it.  It is inferred as ``True`` when the premise,
      genre, outline or author intent mentions a modern viewpoint (穿越、重生、
      现代人 …).  The stored project is never mutated by inference.
    """
    if not isinstance(project, dict):
        return {"allowlist": [], "pov_modern_allowed": False, "inferred": False}
    governance = project.get("governance", {}) if isinstance(project.get("governance"), dict) else {}
    allowlist = [
        str(item).strip() for item in governance.get("term_allowlist", []) or []
        if str(item).strip()
    ]
    stored = governance.get("pov_modern_allowed")
    if isinstance(stored, bool):
        return {"allowlist": allowlist, "pov_modern_allowed": stored, "inferred": False}
    intent = project.get("author_intent")
    haystack = " ".join(
        str(value)
        for value in (
            project.get("premise", ""), project.get("genre", ""), project.get("outline", ""),
            intent if isinstance(intent, str) else " ".join(str(v) for v in (intent or {}).values())
            if isinstance(intent, dict) else "",
        )
    )
    inferred = any(marker in haystack for marker in MODERN_PREMISE_MARKERS)
    return {"allowlist": allowlist, "pov_modern_allowed": inferred, "inferred": inferred}


def _count_terms(text: str, terms: Iterable[str]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for term in terms:
        count = text.count(term)
        if count:
            counts[term] = count
    return counts


def jargon_hits(text: str, policy: dict[str, Any] | None = None) -> list[tuple[str, int]]:
    """Count modern abstract terms, honoring the project's era policy.

    Without a policy every occurrence counts.  With ``pov_modern_allowed``
    the protagonist is a modern mind in an older world: modern concepts in
    narration / inner monologue are part of the premise and count only one
    third (``POV_NARRATION_WEIGHT``), while terms inside quoted dialogue
    count fully — an ancient character saying "算法" is still an anachronism
    (speaker attribution is not attempted, so the POV's own lines count too).
    Allowlisted terms never count.
    """
    source = str(text or "")
    allow = set((policy or {}).get("allowlist", []))
    terms = [term for term in MODERN_JARGON if term not in allow]
    if not (policy or {}).get("pov_modern_allowed"):
        counts = _count_terms(source, terms)
    else:
        dialogue = "\n".join(DIALOGUE_RE.findall(source))
        narration = DIALOGUE_RE.sub("\n", source)
        spoken = _count_terms(dialogue, terms)
        narrated = _count_terms(narration, terms)
        # Weight narration by 1/3 over the *total*, not per term: flooring each
        # term separately turned "ten terms twice each" into zero.  Floors are
        # topped up by largest remainder so the sum equals round(total / 3).
        target = round(sum(narrated.values()) / POV_NARRATION_WEIGHT)
        floors = {term: count // POV_NARRATION_WEIGHT for term, count in narrated.items()}
        spare = max(0, target - sum(floors.values()))
        for term in sorted(
            narrated, key=lambda key: (-(narrated[key] % POV_NARRATION_WEIGHT), -narrated[key], key)
        )[:spare]:
            floors[term] += 1
        counts = Counter()
        for term in set(spoken) | set(narrated):
            weighted = spoken[term] + floors.get(term, 0)
            if weighted:
                counts[term] = weighted
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def _content_ngrams(text: str, n: int = 4) -> set[str]:
    compact = normalize_text(text)
    if len(compact) < n:
        return set()
    return {compact[index : index + n] for index in range(len(compact) - n + 1)}


def _similarity(left: str, right: str) -> float:
    a, b = _content_ngrams(left), _content_ngrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _semantic_similarity(left: str, right: str) -> float:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    sequence = SequenceMatcher(None, a, b).ratio()
    grams_a, grams_b = _content_ngrams(a, 2), _content_ngrams(b, 2)
    containment = len(grams_a & grams_b) / max(1, min(len(grams_a), len(grams_b)))
    return max(sequence, containment)


def volume_progression_issues(project: dict[str, Any]) -> list[dict[str, Any]]:
    volumes = project.get("planning", {}).get("volumes", [])
    if not isinstance(volumes, list):
        return []
    fields = ("goal", "ending_state", "theme_test", "primary_arena", "irreversible_change")
    issues: list[dict[str, Any]] = []
    for right in range(len(volumes)):
        current = volumes[right] if isinstance(volumes[right], dict) else {}
        for left in range(right):
            previous = volumes[left] if isinstance(volumes[left], dict) else {}
            duplicates: list[str] = []
            peak = 0.0
            for field in fields:
                a, b = str(previous.get(field, "")), str(current.get(field, ""))
                score = _semantic_similarity(a, b)
                if min(len(normalize_text(a)), len(normalize_text(b))) >= 12 and score >= 0.76:
                    duplicates.append(field)
                    peak = max(peak, score)
            if duplicates:
                issues.append(
                    {
                        "left": left + 1,
                        "right": right + 1,
                        "fields": duplicates,
                        "similarity": round(peak, 3),
                        "message": f"第{left + 1}卷与第{right + 1}卷在{','.join(duplicates)}上高度相似",
                    }
                )
    return sorted(issues, key=lambda item: -item["similarity"])


_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")
LEARNED_MIN_LEN = 4
LEARNED_MAX_LEN = 10
# Learning needs a corpus large enough that "repeated" means a habit rather
# than coincidence; a few short chapters only produce noise.
LEARNED_MIN_CHAPTERS = 5
LEARNED_MIN_CORPUS_CHARS = 12000


def _protected_terms(project: dict[str, Any] | None) -> list[str]:
    """Names and world terms that are allowed to repeat."""
    if not isinstance(project, dict):
        return []
    terms: set[str] = set()
    for item in project.get("characters", []) or []:
        if isinstance(item, dict):
            for key in ("name", "aliases", "alias"):
                value = item.get(key)
                values = value if isinstance(value, list) else re.split(r"[,，、/\s]+", str(value or ""))
                terms.update(str(v).strip() for v in values if len(str(v).strip()) >= 2)
    for item in project.get("world_entries", []) or []:
        if isinstance(item, dict):
            terms.add(str(item.get("title", "") or item.get("name", "")).strip())
            for key in item.get("keys", []) or []:
                terms.add(str(key).strip())
    knowledge = project.get("knowledge", {}) if isinstance(project.get("knowledge"), dict) else {}
    for item in knowledge.get("entities", []) or []:
        if isinstance(item, dict):
            terms.add(str(item.get("name", "")).strip())
            aliases = item.get("aliases", [])
            if isinstance(aliases, list):
                terms.update(str(alias).strip() for alias in aliases)
    return sorted((term for term in terms if len(term) >= 2), key=len, reverse=True)


def _planning_corpus(project: dict[str, Any] | None) -> str:
    """Author-side planning text.  Phrases that also appear here are setting
    nouns (places, institutions, documents), not prose tics."""
    if not isinstance(project, dict):
        return ""
    parts: list[str] = []
    for key in ("premise", "outline", "author_intent", "book_rules", "current_focus", "genre"):
        parts.append(str(project.get(key, "") or ""))
    for item in project.get("world_entries", []) or []:
        if isinstance(item, dict):
            parts.append(str(item.get("content", "")))
    for chapter in project.get("chapters", []) or []:
        if isinstance(chapter, dict):
            parts.append(str(chapter.get("title", "")))
            for key in ("route", "plan", "scene_goal", "summary"):
                value = chapter.get(key)
                if value:
                    parts.append(value if isinstance(value, str) else str(value))
    return normalize_text("\n".join(parts))


def _planning_ngrams(planning: str) -> set[str]:
    grams: set[str] = set()
    for run in _CJK_RUN_RE.findall(planning):
        for size in range(3, min(LEARNED_MAX_LEN, len(run)) + 1):
            for index in range(len(run) - size + 1):
                grams.add(run[index : index + size])
    return grams


def _planning_overlap(phrase: str, planning: set[str]) -> int:
    """Length of the longest substring (>=3 chars) of ``phrase`` found in the planning text."""
    for size in range(len(phrase), 2, -1):
        for index in range(len(phrase) - size + 1):
            if phrase[index : index + size] in planning:
                return size
    return 0


_LEARNED_CACHE: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
_LEARNED_CACHE_SIZE = 8


def learn_fatigue_phrases(
    chapters: Iterable[dict[str, Any]],
    project: dict[str, Any] | None = None,
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Cached front of :func:`_learn_fatigue_phrases`.

    Learning scans every 4–10 character n-gram of the book (seconds for a long
    manuscript) and runs on every memory settlement and health report, so
    results are memoized by a digest of everything they depend on.
    """
    chapter_list = [item for item in chapters if isinstance(item, dict)]
    digest = hashlib.sha256()
    for item in chapter_list:
        digest.update(str(item.get("content", "")).encode("utf-8"))
        digest.update(b"\x00")
    digest.update(_planning_corpus(project).encode("utf-8"))
    digest.update(json.dumps(sorted(_protected_terms(project)), ensure_ascii=False).encode("utf-8"))
    digest.update(str(limit).encode())
    key = digest.hexdigest()
    if key in _LEARNED_CACHE:
        _LEARNED_CACHE.move_to_end(key)
        return deepcopy(_LEARNED_CACHE[key])
    learned = _learn_fatigue_phrases(chapter_list, project, limit=limit)
    _LEARNED_CACHE[key] = deepcopy(learned)
    while len(_LEARNED_CACHE) > _LEARNED_CACHE_SIZE:
        _LEARNED_CACHE.popitem(last=False)
    return learned


def _learn_fatigue_phrases(
    chapters: Iterable[dict[str, Any]],
    project: dict[str, Any] | None = None,
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Learn this book's own over-used phrases from its accepted chapters.

    Counts 4–10 character CJK n-grams (never across punctuation).  A phrase is
    "fatigued" when it appears in at least ``max(3, chapters // 8)`` chapters
    and at least 4 times in total.  Phrases that are just a character name or
    world term (or leave fewer than 3 characters once names are removed) are
    ignored.  Overlapping n-grams are collapsed to the longest informative
    phrase.  The result is deterministic and can always be rebuilt.
    """
    chapter_list = [item for item in chapters if str(item.get("content", "")).strip()]
    corpus_chars = sum(len(normalize_text(item.get("content", ""))) for item in chapter_list)
    if len(chapter_list) < LEARNED_MIN_CHAPTERS or corpus_chars < LEARNED_MIN_CORPUS_CHARS:
        return []
    protected = _protected_terms(project)
    planning = _planning_ngrams(_planning_corpus(project))
    documents: Counter[str] = Counter()
    totals: Counter[str] = Counter()
    seen_paragraphs: set[str] = set()
    for chapter in chapter_list:
        local: Counter[str] = Counter()
        # Verbatim paragraphs copied across chapters are a duplication problem
        # (reported separately); learning from every copy would turn each of
        # their n-grams into a "fatigue phrase".
        kept: list[str] = []
        for paragraph in re.split(r"\n+", str(chapter.get("content", ""))):
            key = normalize_text(paragraph)
            if len(key) >= 20:
                if key in seen_paragraphs:
                    continue
                seen_paragraphs.add(key)
            kept.append(paragraph)
        for run in _CJK_RUN_RE.findall("\n".join(kept)):
            for size in range(LEARNED_MIN_LEN, min(LEARNED_MAX_LEN, len(run)) + 1):
                for index in range(len(run) - size + 1):
                    local[run[index : index + size]] += 1
        totals.update(local)
        documents.update(local.keys())
    min_documents = max(3, len(chapter_list) // 8)
    candidates = []
    for phrase, documents_count in documents.items():
        if documents_count < min_documents or totals[phrase] < 4:
            continue
        stripped = phrase
        for term in protected:
            stripped = stripped.replace(term, "")
        # An n-gram window can cut a name in half ("默的手指" from "林默的手指");
        # drop a leading or trailing fragment of a protected name as well.
        for term in protected:
            for cut in range(1, len(term)):
                if stripped.startswith(term[cut:]):
                    stripped = stripped[len(term) - cut:]
                    break
                if stripped.endswith(term[:cut]) and len(term[:cut]) < len(stripped):
                    stripped = stripped[: len(stripped) - cut]
                    break
        # "林默的手指" minus the name is "的手指": strip function characters too,
        # so name+的+noun combinations don't count as stylistic tics.
        stripped = stripped.strip("".join(FATIGUE_EDGE_CHARS))
        if len(stripped) < 3 or any(phrase in term for term in protected):
            continue
        if planning and _planning_overlap(phrase, planning) * 2 >= len(phrase):
            continue
        if phrase[0] in FATIGUE_EDGE_CHARS or phrase[-1] in FATIGUE_EDGE_CHARS:
            continue
        candidates.append((phrase, totals[phrase], documents_count))
    candidates.sort(key=lambda item: (-(item[1] * len(item[0])), -len(item[0]), item[0]))
    selected: list[dict[str, Any]] = []
    for phrase, count, documents_count in candidates:
        if any(phrase in item["phrase"] or item["phrase"] in phrase for item in selected):
            continue
        selected.append({"phrase": phrase, "count": count, "chapters": documents_count, "source": "learned"})
        if len(selected) >= limit:
            break
    return selected


def count_phrase_hits(text: str, phrases: Iterable[str]) -> Counter[str]:
    """Count non-overlapping phrase occurrences, longest phrase first.

    "深吸一口气" and a learned "深吸一口气缓缓" in the same spot count once, and
    a phrase that is a substring of another matched phrase is not counted again.
    """
    source = str(text or "")
    covered = bytearray(len(source))
    hits: Counter[str] = Counter()
    for phrase in sorted(dict.fromkeys(p for p in phrases if p), key=len, reverse=True):
        start = source.find(phrase)
        while start >= 0:
            end = start + len(phrase)
            if not any(covered[start:end]):
                covered[start:end] = b"\x01" * len(phrase)
                hits[phrase] += 1
            start = source.find(phrase, start + 1)
    return hits


def refresh_learned_fatigue(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Rebuild ``memory.learned_fatigue_phrases`` from accepted prose.

    Only settled or author-accepted chapters count; a draft still under review
    must not teach the book its "own" tics.
    """
    accepted_ids = {
        str(chapter.get("id", ""))
        for chapter in project.get("chapters", []) or []
        if isinstance(chapter, dict)
        and (
            chapter.get("settlement")
            or chapter.get("locked_content_hash")
            or chapter.get("accepted_content_hash")
            or (
                isinstance(chapter.get("execution"), dict)
                and str(chapter["execution"].get("status", "")) == "accepted"
            )
        )
    }
    rows = [row for row in _chapter_rows(project) if row["id"] in accepted_ids]
    learned = learn_fatigue_phrases(rows, project)
    project.setdefault("memory", {})["learned_fatigue_phrases"] = learned
    return learned


def _fatigued_phrases(
    chapters: Iterable[dict[str, Any]],
    limit: int = 24,
    extra_phrases: Iterable[str] = (),
) -> list[dict[str, Any]]:
    chapter_list = list(chapters)
    extra = tuple(dict.fromkeys(str(item) for item in extra_phrases if str(item).strip()))
    document_count: Counter[str] = Counter()
    total_count: Counter[str] = Counter()
    for chapter in chapter_list:
        text = str(chapter.get("content", ""))
        local: Counter[str] = Counter()
        for clause in re.split(r"[，。！？；：,.!?;:\n]+", text):
            phrase = normalize_text(clause)
            if 6 <= len(phrase) <= 28 and all("\u4e00" <= char <= "\u9fff" for char in phrase):
                local[phrase] += 1
        for phrase in TEMPLATE_PHRASES + extra:
            if phrase in local:
                continue
            count = text.count(phrase)
            if count:
                local[phrase] += count
        total_count.update(local)
        document_count.update(local.keys())
    threshold = max(3, min(10, len(chapter_list) // 12 or 3))
    candidates = [
        (phrase, count, document_count[phrase])
        for phrase, count in total_count.items()
        if count >= threshold and document_count[phrase] >= 3
    ]
    candidates.sort(key=lambda item: (-(item[1] * len(item[0])), -item[2], item[0]))
    selected: list[dict[str, Any]] = []
    for phrase, count, documents in candidates:
        if any(
            phrase in item["phrase"]
            or item["phrase"] in phrase
            or SequenceMatcher(None, phrase, item["phrase"]).ratio() >= 0.66
            for item in selected
        ):
            continue
        selected.append({"phrase": phrase, "count": count, "chapters": documents})
        if len(selected) >= limit:
            break
    return selected


def memory_integrity_issues(project: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic checks for hook debt and provenance-aware state integrity."""
    issues: list[dict[str, Any]] = []
    stale = int(project.get("memory", {}).get("stale_from_chapter", 0) or 0)
    if stale:
        issues.append({"severity": "high", "category": "记忆待重建",
                       "message": f"第 {stale} 章起的派生记忆已过期，请按顺序重新审校和结算。"})
    chapters = project.get("chapters", [])
    for number, chapter in enumerate(chapters, start=1):
        if not isinstance(chapter, dict):
            continue
        missing = incomplete_memory_stages(chapter)
        if missing:
            issues.append({
                "severity": "medium",
                "category": "记忆结算不完整",
                "message": (
                    f"第 {number} 章《{chapter.get('title', '')}》记忆提取未完成阶段："
                    + "、".join(missing) + "；相关人物、线索或事实可能缺失，请补提取。"
                ),
                "chapter_id": str(chapter.get("id", "")),
                "stages": missing,
            })
    chapter_ids = {
        str(item.get("id", ""))
        for item in chapters
        if isinstance(item, dict) and item.get("id")
    }
    written = sum(
        bool(str(item.get("content", "")).strip())
        for item in chapters
        if isinstance(item, dict)
    )
    current_index = max(0, written)
    known_names = {
        str(item.get("name", "")).strip().casefold()
        for item in project.get("characters", [])
        if isinstance(item, dict) and str(item.get("name", "")).strip()
    }
    memory = project.get("memory", {})
    for thread in memory.get("plot_threads", []):
        if not isinstance(thread, dict):
            continue
        status = normalize_thread_status(thread.get("status"))
        title = str(thread.get("title", "未命名线索"))
        if status == "closed" and not str(thread.get("payoff", "")).strip():
            issues.append(
                {
                    "severity": "medium",
                    "category": "线索回收缺口",
                    "message": f"“{title}”标记为已回收，但没有保存实际回收结果。",
                    "source_id": thread.get("id", ""),
                }
            )
            continue
        if status in {"closed", "deferred"}:
            continue
        lifecycle = thread_lifecycle(project, thread, current_index)
        if lifecycle["stale"]:
            issues.append(
                {
                    "severity": "medium" if lifecycle["dormancy"] >= 10 else "low",
                    "category": "线索债务",
                    "message": (
                        f"“{title}”已静默 {lifecycle['dormancy']} 章；"
                        "应推进、给出合理延后原因或在满足条件后回收。"
                    ),
                    "source_id": thread.get("id", ""),
                }
            )
        if not str(thread.get("expected_payoff", "")).strip():
            issues.append(
                {
                    "severity": "low",
                    "category": "线索契约不完整",
                    "message": f"“{title}”没有预期回收内容，后续难以判断何时完成。",
                    "source_id": thread.get("id", ""),
                }
            )

    for relation in memory.get("relationships", []):
        if not isinstance(relation, dict):
            continue
        unknown = [
            str(relation.get(key, "")).strip()
            for key in ("left", "right")
            if str(relation.get(key, "")).strip().casefold() not in known_names
        ]
        if unknown:
            issues.append(
                {
                    "severity": "medium",
                    "category": "关系人物缺失",
                    "message": "动态关系引用未知人物：" + "、".join(unknown),
                    "source_id": relation.get("id", ""),
                }
            )

    for character in project.get("characters", []):
        if not isinstance(character, dict):
            continue
        for knowledge in character.get("knowledge_ledger", []):
            if not isinstance(knowledge, dict):
                continue
            source = str(knowledge.get("source_chapter_id", ""))
            if source and source not in chapter_ids:
                issues.append(
                    {
                        "severity": "medium",
                        "category": "人物知情来源缺失",
                        "message": (
                            f"{character.get('name', '人物')}的知情“"
                            f"{str(knowledge.get('text', ''))[:30]}”引用了不存在的章节。"
                        ),
                        "source_id": knowledge.get("id", ""),
                    }
                )
    return issues


def manuscript_health_report(project: dict[str, Any]) -> dict[str, Any]:
    chapters = _chapter_rows(project)
    paragraph_locations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    title_locations: dict[str, list[int]] = defaultdict(list)
    for chapter in chapters:
        title_locations[normalize_text(chapter["title"])].append(chapter["number"])
        seen_in_chapter: set[str] = set()
        for paragraph in _paragraphs(chapter["content"], 28):
            key = normalize_text(paragraph)
            if key in seen_in_chapter:
                continue
            seen_in_chapter.add(key)
            paragraph_locations[key].append(
                {"chapter": chapter["number"], "title": chapter["title"], "excerpt": paragraph[:160]}
            )

    duplicate_titles = [
        {"title": title, "chapters": locations}
        for title, locations in title_locations.items()
        if title and len(locations) > 1
    ]
    duplicate_passages = [
        {"excerpt": locations[0]["excerpt"], "occurrences": locations}
        for locations in paragraph_locations.values()
        if len(locations) > 1
    ]
    duplicate_passages.sort(key=lambda item: (-len(item["occurrences"]), -len(item["excerpt"])))

    similar_pairs: list[dict[str, Any]] = []
    usable = [chapter for chapter in chapters if len(normalize_text(chapter["content"])) >= 180]
    for right in range(len(usable)):
        for left in range(right):
            score = _similarity(usable[left]["content"], usable[right]["content"])
            if score >= 0.16:
                similar_pairs.append(
                    {
                        "left": usable[left]["number"],
                        "right": usable[right]["number"],
                        "left_title": usable[left]["title"],
                        "right_title": usable[right]["title"],
                        "similarity": round(score, 3),
                    }
                )
    similar_pairs.sort(key=lambda item: -item["similarity"])

    full_text = "\n".join(chapter["content"] for chapter in chapters)
    policy = jargon_policy(project)
    jargon = jargon_hits(full_text, policy)
    learned = learn_fatigue_phrases(chapters, project)
    fatigue = _fatigued_phrases(chapters, extra_phrases=[item["phrase"] for item in learned])
    contributions = chapter_contributions(chapters, policy, fatigue, duplicate_passages)
    volume_issues = volume_progression_issues(project)
    memory_issues = memory_integrity_issues(project)
    issue_count = (
        len(duplicate_titles) * 2
        + min(20, len(duplicate_passages))
        + min(15, len(similar_pairs))
        + min(12, len(fatigue))
        + min(12, sum(count for _, count in jargon) // 10)
        + min(20, len(volume_issues) * 2)
        + min(
            20,
            sum(
                3 if item.get("severity") == "high" else 2
                if item.get("severity") == "medium" else 1
                for item in memory_issues
            ),
        )
    )
    score = max(0, 100 - issue_count)
    severity = "high" if score < 60 else "medium" if score < 82 else "low"
    recommendations: list[str] = []
    if duplicate_passages:
        recommendations.append("先合并或重写跨章完全重复段落，再继续批量生成。")
    if similar_pairs:
        recommendations.append("对高相似章节执行“保留不可逆变化、删除同功能场景”的结构去重。")
    if volume_issues:
        recommendations.append("重做分卷契约：每卷必须更换主战场，并产生不同的不可逆状态变化。")
    if jargon:
        recommendations.append("历史题材把抽象现代术语改写为当时人物能观察、争论和承担代价的具体事物。")
    if fatigue:
        recommendations.append("将疲劳词加入后续章节禁复用清单，连续三章不得重复同一意象和句式。")
    if memory_issues:
        recommendations.append("先处理记忆完整性问题：推进过期线索、补足回收结果，并核对人物知情与关系来源。")
    return {
        "score": score,
        "severity": severity,
        "chapter_count": len(chapters),
        "character_count": len(normalize_text(full_text)),
        "duplicate_titles": duplicate_titles,
        "duplicate_passages": duplicate_passages[:30],
        "duplicate_passage_count": len(duplicate_passages),
        "similar_chapters": similar_pairs[:30],
        "fatigued_phrases": fatigue,
        "modern_jargon": [{"term": term, "count": count} for term, count in jargon],
        "jargon_policy": policy,
        "learned_fatigue_phrases": learned,
        "chapter_contributions": contributions[:40],
        "volume_progression_issues": volume_issues[:30],
        "memory_integrity_issues": memory_issues[:40],
        "recommendations": recommendations,
    }


def chapter_contributions(
    chapters: list[dict[str, Any]],
    policy: dict[str, Any] | None,
    fatigue: list[dict[str, Any]],
    duplicate_passages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rank chapters by how much they contribute to manuscript-level debts.

    Used to target refinement at the chapters that can actually move a failing
    global gate (jargon total, fatigue phrases, cross-chapter duplicates).
    """
    duplicate_counts: Counter[int] = Counter()
    for passage in duplicate_passages:
        for occurrence in passage.get("occurrences", [])[1:]:
            duplicate_counts[int(occurrence.get("chapter", 0) or 0)] += 1
    phrases = [item["phrase"] for item in fatigue]
    rows: list[dict[str, Any]] = []
    for chapter in chapters:
        text = chapter["content"]
        jargon = sum(count for _, count in jargon_hits(text, policy))
        fatigue_count = sum(text.count(phrase) for phrase in phrases)
        duplicates = duplicate_counts.get(chapter["number"], 0)
        total = jargon + fatigue_count + duplicates * 3
        if total:
            rows.append({
                "chapter": chapter["number"], "chapter_id": chapter["id"], "title": chapter["title"],
                "jargon": jargon, "fatigue": fatigue_count, "duplicates": duplicates, "total": total,
            })
    rows.sort(key=lambda item: (-item["total"], item["chapter"]))
    return rows


def prompt_repetition_guard(project: dict[str, Any], chapter_id: str, limit: int = 12) -> str:
    rows: list[dict[str, Any]] = []
    for chapter in _chapter_rows(project):
        if chapter["id"] == str(chapter_id):
            break
        if chapter["content"].strip():
            rows.append(chapter)
    if not rows:
        return ""
    learned = learn_fatigue_phrases(rows, project, limit=limit)
    fatigue = _fatigued_phrases(rows, limit=limit, extra_phrases=[item["phrase"] for item in learned])
    passage_counts: Counter[str] = Counter()
    passage_examples: dict[str, str] = {}
    for row in rows:
        for paragraph in set(_paragraphs(row["content"], 28)):
            key = normalize_text(paragraph)
            passage_counts[key] += 1
            passage_examples.setdefault(key, paragraph[:120])
    repeated = [
        passage_examples[key]
        for key, count in passage_counts.most_common()
        if count > 1
    ][:5]
    lines: list[str] = []
    if fatigue:
        lines.append("全书疲劳词（除非情节必需，本章不得原样复用）：" + "、".join(item["phrase"] for item in fatigue))
    if repeated:
        lines.append("已经跨章复用过的句段（不得再次改写或复述）：")
        lines.extend(f"- {item[:90]}" for item in repeated)
    return "\n".join(lines)

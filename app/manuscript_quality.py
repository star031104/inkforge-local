from __future__ import annotations

import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from typing import Any, Iterable

from .memory import normalize_thread_status, thread_lifecycle


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

TEMPLATE_PHRASES = (
    "空气中弥漫着", "仿佛在诉说", "打破了这份宁静", "命运的齿轮",
    "目光如炬", "一字一顿", "不容置疑", "陷入了沉思", "若有所思",
    "微微一愣", "嘴角勾起", "深吸一口气", "与此同时", "就在这时",
    "极其微弱", "具象化为", "柔性冗余", "动态阈值", "静默账本",
    "士气衰减系数", "半月之约", "半透明", "裂痕", "余生粮为祭",
    "指尖无意识地转动着一枚木质算筹", "十万将士性命皆系于你手中",
    "玄色龙纹深衣", "烛火摇曳", "墨迹未干",
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


def jargon_hits(text: str) -> list[tuple[str, int]]:
    hits = [(term, str(text or "").count(term)) for term in MODERN_JARGON]
    return sorted((item for item in hits if item[1]), key=lambda item: (-item[1], item[0]))


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


def _fatigued_phrases(chapters: Iterable[dict[str, Any]], limit: int = 24) -> list[dict[str, Any]]:
    chapter_list = list(chapters)
    document_count: Counter[str] = Counter()
    total_count: Counter[str] = Counter()
    for chapter in chapter_list:
        text = str(chapter.get("content", ""))
        local: Counter[str] = Counter()
        for clause in re.split(r"[，。！？；：,.!?;:\n]+", text):
            phrase = normalize_text(clause)
            if 6 <= len(phrase) <= 28 and all("\u4e00" <= char <= "\u9fff" for char in phrase):
                local[phrase] += 1
        for phrase in TEMPLATE_PHRASES:
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
    chapters = project.get("chapters", [])
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
    jargon = jargon_hits(full_text)
    fatigue = _fatigued_phrases(chapters)
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
        "volume_progression_issues": volume_issues[:30],
        "memory_integrity_issues": memory_issues[:40],
        "recommendations": recommendations,
    }


def prompt_repetition_guard(project: dict[str, Any], chapter_id: str, limit: int = 12) -> str:
    rows: list[dict[str, Any]] = []
    for chapter in _chapter_rows(project):
        if chapter["id"] == str(chapter_id):
            break
        if chapter["content"].strip():
            rows.append(chapter)
    if not rows:
        return ""
    fatigue = _fatigued_phrases(rows, limit=limit)
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

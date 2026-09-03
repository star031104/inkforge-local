from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .style_engine import style_stats
from .manuscript_quality import cross_chapter_reuse, jargon_hits


META_PREFIXES = (
    "好的",
    "当然",
    "以下是",
    "下面是",
    "根据你的要求",
    "作为一个",
)

AI_CLICHES = (
    "空气中弥漫着",
    "仿佛在诉说",
    "像一排排沉默的墓碑",
    "微微一滞",
    "不由得",
    "极其微弱",
    "具象化为",
    "此时竟",
    "那是由于",
    "打破了这份宁静",
    "命运的齿轮",
)

PAST_FACT_PATTERNS = (
    r"(?:\d+|[一二三四五六七八九十两几半]+)(?:天|周|个月|月|年)前",
    r"早在",
    r"曾经",
    r"上次",
    r"当年",
    r"小时候",
    r"(?:想起|记起|记得|回忆起)",
    r"明明.{0,20}(?:还在|没有|没)",
    r"以前.{0,10}(?:来过|见过|去过|住过|做过|认识|听过|读过|用过)",
)

# These words are too ambiguous to be treated as invented backstory on their own.
# “本该安静的夜” and “原来门没有锁” are perfectly ordinary scene prose.  When
# they accompany a strong past marker (for example “半年前……本该……”), the
# strong marker above still triggers the review.
AMBIGUOUS_PAST_WORDS = ("本该", "原来")
OBSERVABLE_HISTORY_CUES = (
    "写着", "刻着", "标着", "注明", "记载", "记录", "显示", "牌上",
    "铭牌", "告示", "碑上", "碑文", "竹简", "简牍", "档案", "账册",
    "公文", "文书", "题记", "标签", "印着",
)
UNCERTAINTY_CUES = (
    "似乎", "仿佛", "也许", "或许", "可能", "大概", "猜", "猜测",
    "不确定", "未必", "好像", "看起来", "像是", "据说", "传闻",
)
PERSONAL_BACKSTORY_CUES = (
    "我", "他", "她", "我们", "他们", "她们", "父亲", "母亲", "老师",
    "朋友", "曾来", "来过", "见过", "去过", "住过", "认识", "丢",
    "答应", "约定", "告诉", "听过", "读过", "用过", "拿过",
)


def normalize(text: str) -> str:
    return re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’—…（）()]", "", text)


def _char_bigrams(text: str) -> set[str]:
    compact = normalize(text)
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index:index + 2] for index in range(len(compact) - 1)}


def _claim_supported_by_context(claim: str, context: str) -> bool:
    """Heuristic evidence match for a claimed past fact.

    Exact substring wins. Otherwise compare the claim with context sentences by
    Chinese-character bigram overlap. This is intentionally conservative: it
    only suppresses a warning when the project already contains substantially
    the same statement, not merely the same time marker.
    """
    claim_norm = normalize(claim)
    if not claim_norm or not context.strip():
        return False
    context_norm = normalize(context)
    if len(claim_norm) >= 6 and claim_norm in context_norm:
        return True
    claim_bigrams = _char_bigrams(claim)
    if len(claim_bigrams) < 3:
        return False
    for sentence in re.split(r"[。！？!?\n]+", context):
        if len(normalize(sentence)) < 4:
            continue
        other = _char_bigrams(sentence)
        if not other:
            continue
        overlap = len(claim_bigrams & other)
        coverage = overlap / max(1, min(len(claim_bigrams), len(other)))
        if overlap >= 4 and coverage >= 0.58:
            return True
    return False


def local_quality_check(
    draft: str,
    source_tail: str = "",
    target_words: int = 1200,
    pov: str = "auto",
    reference_style: str = "",
    prior_text: str = "",
    genre: str = "",
    known_context: str = "",
) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    clean_length = len(re.sub(r"\s", "", draft))
    lower, upper = int(target_words * 0.75), int(target_words * 1.35)
    if clean_length < lower:
        issues.append(
            {
                "severity": "medium",
                "category": "长度",
                "message": f"草稿约 {clean_length} 字，明显短于目标 {target_words} 字。",
            }
        )
    elif clean_length > upper:
        issues.append(
            {
                "severity": "low",
                "category": "长度",
                "message": f"草稿约 {clean_length} 字，明显长于目标 {target_words} 字。",
            }
        )

    opening = draft.lstrip()[:30]
    if any(opening.startswith(prefix) for prefix in META_PREFIXES):
        issues.append(
            {
                "severity": "high",
                "category": "元话语",
                "message": "草稿以解释或应答语开头，不是可直接使用的小说正文。",
            }
        )

    paragraphs = [normalize(item) for item in re.split(r"\n+", draft) if normalize(item)]
    counts = Counter(paragraphs)
    duplicates = [text for text, count in counts.items() if count > 1 and len(text) > 18]
    if duplicates:
        issues.append(
            {
                "severity": "high",
                "category": "重复段落",
                "message": f"检测到 {len(duplicates)} 个完全重复的段落。",
            }
        )

    compact = normalize(draft)
    if len(compact) >= 120:
        ngrams = [compact[index : index + 12] for index in range(len(compact) - 11)]
        repeated = sum(count - 1 for count in Counter(ngrams).values() if count > 1)
        if repeated > max(8, len(compact) // 120):
            issues.append(
                {
                    "severity": "medium",
                    "category": "措辞重复",
                    "message": "较长的措辞片段反复出现，可能存在模型循环。",
                }
            )

    # A model loop can alternate two or more short sentences and therefore stay
    # below both the duplicate-paragraph and long-ngram thresholds.  Count
    # sentence-sized units independently so that this common degeneration is
    # visible to the deterministic gate.
    sentence_counts = Counter(
        normalize(item)
        for item in re.split(r"(?<=[。！？!?])|\n+", draft)
        if 8 <= len(normalize(item)) <= 80
    )
    repeated_sentences = [
        text for text, count in sentence_counts.items() if count > 1
    ]
    if len(repeated_sentences) >= 2:
        issues.append(
            {
                "severity": "medium",
                "category": "短句循环",
                "message": (
                    "检测到多组短句反复出现，可能是交替式模型循环："
                    + "；".join(repeated_sentences[:3])
                ),
            }
        )

    last_sentence = next(
        (
            item.strip()
            for item in reversed(re.split(r"(?<=[。！？!?])", source_tail))
            if item.strip()
        ),
        "",
    )
    if last_sentence and normalize(draft[: max(120, len(last_sentence) * 2)]).startswith(
        normalize(last_sentence)
    ):
        issues.append(
            {
                "severity": "medium",
                "category": "重复原文",
                "message": "草稿重复了原文最后一句，没有直接向下推进。",
            }
        )

    source_sentences = {
        normalize(item)
        for item in re.split(r"(?<=[。！？!?])|\n+", source_tail)
        if len(normalize(item)) >= 18
    }
    copied_sentences = {
        normalize(item)
        for item in re.split(r"(?<=[。！？!?])|\n+", draft)
        if len(normalize(item)) >= 18 and normalize(item) in source_sentences
    }
    if copied_sentences:
        issues.append(
            {
                "severity": "medium",
                "category": "复用已有描写",
                "message": f"候选稿原样复用了已有正文中的 {len(copied_sentences)} 个完整长句。",
                "suggestion": "保留事实，换观察角度、感官通道或人物反应表达。",
            }
        )

    if prior_text.strip():
        reuse = cross_chapter_reuse(draft, prior_text)
        if reuse["paragraph_count"]:
            issues.append(
                {
                    "severity": "high",
                    "category": "跨章重复段落",
                    "message": (
                        f"候选稿原样复用了全书既有正文中的 {reuse['paragraph_count']} 个长段落。"
                    ),
                    "suggestion": "保留本章必须发生的新事实，整段改换场景任务、冲突对象和感官通道。",
                    "evidence": reuse["paragraphs"],
                }
            )
        elif reuse["sentence_count"] >= 2:
            issues.append(
                {
                    "severity": "high",
                    "category": "跨章重复长句",
                    "message": (
                        f"候选稿原样复用了全书既有正文中的 {reuse['sentence_count']} 个长句。"
                    ),
                    "suggestion": "不要只替换同义词；重新设计动作链和信息揭示顺序。",
                    "evidence": reuse["sentences"],
                }
            )
        elif reuse["sentence_count"]:
            issues.append(
                {
                    "severity": "medium",
                    "category": "跨章复用句式",
                    "message": "候选稿复用了全书既有正文中的完整长句。",
                    "suggestion": "改为新的具体观察或人物反应，并确保本章产生新变化。",
                    "evidence": reuse["sentences"],
                }
            )

    historical = any(word in str(genre) for word in ("历史", "古代", "战国", "架空"))
    if historical:
        jargon = jargon_hits(draft)
        jargon_total = sum(count for _, count in jargon)
        density = jargon_total / max(1, clean_length) * 1000
        if len(jargon) >= 4 and (jargon_total >= 7 or density >= 4.5):
            issues.append(
                {
                    "severity": "medium",
                    "category": "时代语汇失真",
                    "message": (
                        "历史题材中现代管理/技术术语过密："
                        + "、".join(f"{term}×{count}" for term, count in jargon[:8])
                    ),
                    "suggestion": "将概念落实为简牍、算筹、口粮、名籍、命令、争论和可见代价；仅在主角内心少量保留现代概念。",
                }
            )

    if pov == "first" and clean_length > 300:
        third = sum(draft.count(word) for word in ("他", "她", "他们", "她们"))
        first = draft.count("我")
        if third > first * 5 + 15:
            issues.append(
                {
                    "severity": "low",
                    "category": "视角提醒",
                    "message": "第一人称项目中第三人称代词密度异常，请确认是否发生视角漂移。",
                }
            )

    cliché_hits = [phrase for phrase in AI_CLICHES if phrase in draft]
    if len(cliché_hits) >= 2 or draft.count("——") >= 3:
        issues.append(
            {
                "severity": "low",
                "category": "AI套话",
                "message": "检测到较密集的模板化修辞或破折号："
                + "、".join(cliché_hits[:4]),
            }
        )

    # Backstory guard: distinguish an unsupported asserted personal past from
    # (a) facts already present in project authority, (b) facts read from an
    # observable object in the current scene, and (c) explicitly uncertain
    # hypotheses.  The old implementation treated every “三年前/原来/本该” as a
    # medium error, which produced noisy false positives in historical fiction.
    authority_text = "\n".join(
        part for part in (source_tail, prior_text, known_context) if str(part).strip()
    )
    seen_past_spans: set[tuple[int, int]] = set()
    for pattern in PAST_FACT_PATTERNS:
        for match in re.finditer(pattern, draft):
            span = match.span()
            if span in seen_past_spans:
                continue
            seen_past_spans.add(span)
            marker = match.group(0)
            sentence_start = max(
                draft.rfind("。", 0, match.start()),
                draft.rfind("！", 0, match.start()),
                draft.rfind("？", 0, match.start()),
                draft.rfind("\n", 0, match.start()),
            ) + 1
            next_positions = [
                pos for pos in (
                    draft.find("。", match.end()),
                    draft.find("！", match.end()),
                    draft.find("？", match.end()),
                    draft.find("\n", match.end()),
                ) if pos >= 0
            ]
            sentence_end = min(next_positions) + 1 if next_positions else min(len(draft), match.end() + 80)
            claim = draft[sentence_start:sentence_end].strip()
            excerpt = claim[:120].replace("\n", "")

            if _claim_supported_by_context(claim, authority_text):
                continue
            if any(cue in claim for cue in OBSERVABLE_HISTORY_CUES):
                # A sign, ledger, inscription, etc. is current-scene evidence.
                # It may introduce a new fact, but it is not an illicit memory.
                continue

            uncertain = any(cue in claim for cue in UNCERTAINTY_CUES)
            personal = any(cue in claim for cue in PERSONAL_BACKSTORY_CUES)
            severity = "low" if uncertain or not personal else "medium"
            category = "待确认新设定" if severity == "low" else "疑似新增往事"
            message_prefix = (
                "候选稿提出尚未由现有设定确认的过去信息"
                if severity == "low"
                else "候选稿出现原文/设定未提供的个人往事"
            )
            issues.append(
                {
                    "severity": severity,
                    "category": category,
                    "message": f"{message_prefix}“{marker}”：{excerpt}",
                    "suggestion": (
                        "若这是计划新增的设定，可保留并在接受正文后写入记忆；"
                        "若人物不应知道此事，则改为可观察证据、未证实猜测或保持未知。"
                    ),
                }
            )

    reference = reference_style.strip() or source_tail.strip()
    if len(reference) >= 150 and len(draft) >= 150:
        expected = style_stats(reference)
        actual = style_stats(draft)
        sentence_gap = abs(actual.sentence_length - expected.sentence_length)
        dialogue_gap = abs(actual.dialogue_ratio - expected.dialogue_ratio)
        if sentence_gap > max(12, expected.sentence_length * 0.75) or dialogue_gap > 0.34:
            issues.append(
                {
                    "severity": "low",
                    "category": "文风偏移",
                    "message": (
                        f"草稿平均句长约 {actual.sentence_length:.1f} 字、对话占比约 "
                        f"{actual.dialogue_ratio:.0%}；参考文本约 {expected.sentence_length:.1f} "
                        f"字、{expected.dialogue_ratio:.0%}，节奏差异较大。"
                    ),
                }
            )
        if (
            clean_length >= 600
            and actual.abstract_density > max(14, expected.abstract_density * 2.1)
            and actual.sensory_density < max(1, expected.sensory_density * 0.7)
        ):
            issues.append(
                {
                    "severity": "low",
                    "category": "抽象叙述过密",
                    "message": (
                        "候选稿的判断/总结词明显多于参考文本，而可感知细节偏少；"
                        "关键场面可能被概述替代。"
                    ),
                    "suggestion": "把关键判断改写为人物动作、物件变化、空间距离或话语反应。",
                }
            )
        if (
            clean_length >= 800
            and expected.sentence_length_std >= 8
            and actual.sentence_length_std < expected.sentence_length_std * 0.35
        ):
            issues.append(
                {
                    "severity": "low",
                    "category": "句长过度整齐",
                    "message": "参考文本句长起伏明显，候选稿句长却过于均匀，节奏可能机械。",
                    "suggestion": "按动作压力自然调整句长，不要机械切成等长句。",
                }
            )

    if re.search(r"(?:^|\n)\s*(?:```|#{1,6}\s|(?:本章|总结|写作说明)[:：])", draft):
        issues.append(
            {
                "severity": "high",
                "category": "非正文格式",
                "message": "草稿中出现代码围栏、Markdown 标题或写作说明，不是可直接使用的小说正文。",
            }
        )

    quote_pairs = (("“", "”"), ("‘", "’"), ("（", "）"), ("【", "】"))
    unbalanced = [left + right for left, right in quote_pairs if draft.count(left) != draft.count(right)]
    if unbalanced:
        issues.append(
            {
                "severity": "high",
                "category": "标点未闭合",
                "message": "检测到未闭合的引号或括号：" + "、".join(unbalanced),
            }
        )

    stripped = draft.rstrip()
    if stripped and stripped[-1] in "，、；：—（【“‘":
        issues.append(
            {
                "severity": "high",
                "category": "疑似截断",
                "message": "草稿结尾停在逗号、冒号、破折号或未闭合标点处，模型输出可能被截断。",
            }
        )

    raw_paragraphs = [item.strip() for item in re.split(r"\n+", draft) if item.strip()]
    oversized = sum(1 for item in raw_paragraphs if len(normalize(item)) > 600)
    if oversized:
        issues.append(
            {
                "severity": "low",
                "category": "段落节奏",
                "message": f"有 {oversized} 个段落超过 600 字，阅读节奏和场景层次可能过于拥挤。",
            }
        )

    sentence_starts: Counter[str] = Counter()
    for sentence in re.split(r"[。！？!?]+", draft):
        start = normalize(sentence)[:4]
        if len(start) == 4:
            sentence_starts[start] += 1
    repeated_starts = [start for start, count in sentence_starts.items() if count >= 4]
    if repeated_starts:
        issues.append(
            {
                "severity": "low",
                "category": "句式疲劳",
                "message": "多个句子使用相同起笔，容易形成机械节奏："
                + "、".join(repeated_starts[:4]),
            }
        )

    score = max(
        0,
        100
        - sum(
            22
            if item["severity"] == "high"
            else 10
            if item["severity"] == "medium"
            else 4
            for item in issues
        ),
    )
    return {
        "score": score,
        "issues": issues,
        "word_count": clean_length,
        "verdict": (
            "pass"
            if not any(i["severity"] in {"high", "medium"} for i in issues)
            else "revise"
        ),
    }

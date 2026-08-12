from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from typing import Any

from .memory import relevance


@dataclass
class StyleStats:
    sentence_length: float
    sentence_length_std: float
    paragraph_length: float
    paragraph_length_std: float
    dialogue_ratio: float
    dialogue_turn_length: float
    comma_ratio: float
    short_sentence_ratio: float
    first_person_ratio: float
    sensory_density: float
    abstract_density: float


def style_stats(text: str) -> StyleStats:
    text = text.strip()
    sentences = [
        part.strip()
        for part in re.split(r"(?<=[。！？!?])", text)
        if part.strip()
    ]
    paragraphs = [part.strip() for part in re.split(r"\n+", text) if part.strip()]
    sentence_lengths = [len(re.sub(r"\s", "", item)) for item in sentences] or [0]
    paragraph_lengths = [len(re.sub(r"\s", "", item)) for item in paragraphs] or [0]
    dialogue_turns = [
        match.group(1)
        for match in re.finditer(r"[“「『\"]([^”」』\"]+)[”」』\"]", text)
    ]
    dialogue_chars = sum(len(item) for item in dialogue_turns)
    visible = max(1, len(re.sub(r"\s", "", text)))
    sensory_terms = (
        "看", "望", "瞥", "光", "暗", "声", "响", "听", "闻", "气味",
        "冷", "热", "疼", "痛", "涩", "湿", "干", "硬", "软", "粗", "滑",
    )
    abstract_terms = (
        "意义", "命运", "人性", "本质", "显然", "无疑", "似乎", "仿佛",
        "情绪", "感觉", "意识到", "明白", "认为", "觉得",
    )
    return StyleStats(
        sentence_length=sum(sentence_lengths) / len(sentence_lengths),
        sentence_length_std=(
            statistics.pstdev(sentence_lengths) if len(sentence_lengths) > 1 else 0.0
        ),
        paragraph_length=sum(map(len, paragraphs)) / max(1, len(paragraphs)),
        paragraph_length_std=(
            statistics.pstdev(paragraph_lengths) if len(paragraph_lengths) > 1 else 0.0
        ),
        dialogue_ratio=dialogue_chars / visible,
        dialogue_turn_length=(
            sum(len(item) for item in dialogue_turns) / len(dialogue_turns)
            if dialogue_turns
            else 0.0
        ),
        comma_ratio=(text.count("，") + text.count(",")) / visible,
        short_sentence_ratio=sum(length <= 12 for length in sentence_lengths)
        / len(sentence_lengths),
        first_person_ratio=(text.count("我") + text.count("我们")) / visible,
        sensory_density=sum(text.count(term) for term in sensory_terms) / visible * 1000,
        abstract_density=sum(text.count(term) for term in abstract_terms) / visible * 1000,
    )


def render_fingerprint(text: str) -> str:
    if len(text.strip()) < 80:
        return ""
    stats = style_stats(text)
    pace = (
        "短促"
        if stats.sentence_length < 15
        else "舒展"
        if stats.sentence_length > 28
        else "中等"
    )
    dialogue = (
        "对话主导"
        if stats.dialogue_ratio > 0.32
        else "叙述主导"
        if stats.dialogue_ratio < 0.08
        else "叙述与对话交替"
    )
    sentences_per_paragraph = max(1, stats.paragraph_length / max(1, stats.sentence_length))
    return (
        f"统计文风指纹：平均句长约 {stats.sentence_length:.1f} 字，"
        f"句长自然波动约 ±{stats.sentence_length_std:.1f} 字；"
        f"平均段长约 {stats.paragraph_length:.1f} 字，节奏{pace}；"
        f"{dialogue}（对话占比约 {stats.dialogue_ratio:.0%}）；"
        f"对白单轮约 {stats.dialogue_turn_length:.1f} 字，短句占比约 {stats.short_sentence_ratio:.0%}；"
        f"感官词密度约 {stats.sensory_density:.1f}/千字，抽象判断词约 {stats.abstract_density:.1f}/千字。"
        f"执行时让多数段落约含 {sentences_per_paragraph:.1f} 句，"
        "复现波动范围和叙述距离，不要逐项机械凑数。"
    )


def split_exemplars(sample: str, min_chars: int = 450, max_chars: int = 1100) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n+", sample) if part.strip()]
    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        candidate = f"{buffer}\n{paragraph}".strip()
        if buffer and len(candidate) > max_chars:
            if len(buffer) >= min_chars:
                chunks.append(buffer)
            buffer = paragraph
        else:
            buffer = candidate
    if buffer:
        chunks.append(buffer)
    if not chunks and sample.strip():
        chunks = [
            sample[index : index + max_chars]
            for index in range(0, len(sample), max_chars)
        ]
    return [chunk for chunk in chunks if len(chunk) >= min(120, len(sample.strip()))]


def select_exemplars(
    sample: str, query: str, current_text: str, limit: int = 2
) -> list[str]:
    chunks = split_exemplars(sample)
    if not chunks:
        return []
    current_stats = style_stats(current_text[-3000:] or query)
    scored = []
    for index, chunk in enumerate(chunks):
        stats = style_stats(chunk)
        scene_match = 1 - min(1, abs(stats.dialogue_ratio - current_stats.dialogue_ratio))
        length_match = math.exp(
            -abs(stats.sentence_length - current_stats.sentence_length) / 25
        )
        lexical = min(5, relevance(query, chunk)) * 0.25
        scored.append((scene_match * 2 + length_match + lexical, -index, chunk))
    scored.sort(reverse=True)
    return [item[2] for item in scored[:limit]]


def render_style_context(
    style: dict[str, Any], query: str, current_text: str
) -> str:
    chunks = []
    if style.get("profile"):
        chunks.append(style["profile"].strip())
    if style.get("dos"):
        chunks.append("应当：\n- " + "\n- ".join(style["dos"]))
    if style.get("donts"):
        chunks.append("避免：\n- " + "\n- ".join(style["donts"]))
    sample = style.get("sample", "").strip()
    if sample:
        chunks.append(render_fingerprint(sample))
        exemplars = select_exemplars(sample, query, current_text)
        if exemplars:
            rendered = "\n\n--- 样例切换 ---\n\n".join(exemplars)
            chunks.append(
                "以下片段由系统按当前场景类型选取，仅学习句法、节奏、感官密度和对话组织。"
                "严禁复制其中专名、情节、意象组合或连续措辞：\n" + rendered
            )
    return "\n\n".join(chunk for chunk in chunks if chunk)

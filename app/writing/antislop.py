"""Generation-time suppression of AI clichés ("slop").

Real manuscripts produced with flash models repeated the same tics dozens of
times (深吸一口气 ×17, 就在这时 ×16, 嘴角勾起 ×11) even though the prompt
asked for variety: negative instructions are weak.  Following the Antislop
idea (Paech et al. 2025), suppression happens while/after generating instead
of in a later whole-chapter rewrite:

* On llama.cpp the distinctive tokens of banned phrases receive a negative
  ``logit_bias`` (token ids resolved through ``/tokenize`` so single common
  characters such as 一/口 are never penalised).
* For every endpoint, the finished draft is scanned and only the offending
  sentences — each with one sentence of context on both sides — are sent in a
  single small JSON rewrite call, then spliced back.  The chapter is never
  regenerated for slop, which also avoids the plot-hole drift that LLM
  rewrites introduce (Finding Flawed Fictions, 2025).

A phrase may be legitimate once: generic and learned phrases allow one
occurrence per draft, author-banned phrases allow none.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

import httpx

from app.llm.json_schemas import REWRITE_SCHEMA
from app.review.manuscript_quality import TEMPLATE_PHRASES
from app.llm.providers import auth_headers, normalize_base_url, resolve_capabilities, server_root
from app.writing.craft_settings import craft_setting

SENTENCE_RE = re.compile(r"[^。！？!?\n]*[。！？!?]+[”’」』）)]*|[^。！？!?\n]+")
# Single CJK characters and function words that must never be biased.
_COMMON_PIECES = set("的一是在了着也与及或而就都将把被让向从于为有无这那其之并却又更很他她它们口气目光声音")


@dataclass(frozen=True)
class BannedPhrase:
    phrase: str
    allow: int
    source: str  # generic | learned | author


def banned_phrases(project: dict[str, Any]) -> list[BannedPhrase]:
    """Author list (0 allowed) + generic clichés + this book's learned tics (1 allowed)."""
    governance = project.get("governance", {}) if isinstance(project.get("governance"), dict) else {}
    author = governance.get("banned_phrases", [])
    if isinstance(author, str):
        author = re.split(r"[,，\n]", author)
    phrases: dict[str, BannedPhrase] = {}
    for item in author if isinstance(author, list) else []:
        text = str(item).strip()
        if len(text) >= 2:
            phrases[text] = BannedPhrase(text, 0, "author")
    for text in TEMPLATE_PHRASES:
        phrases.setdefault(text, BannedPhrase(text, 1, "generic"))
    memory = project.get("memory", {}) if isinstance(project.get("memory"), dict) else {}
    for item in memory.get("learned_fatigue_phrases", []) or []:
        text = str(item.get("phrase", "") if isinstance(item, dict) else item).strip()
        if len(text) >= 4:
            phrases.setdefault(text, BannedPhrase(text, 1, "learned"))
    return list(phrases.values())


def split_sentences(text: str) -> list[tuple[int, int]]:
    """Return (start, end) spans of sentences; separators between them are kept."""
    return [(match.start(), match.end()) for match in SENTENCE_RE.finditer(text) if match.group().strip()]


def detect_slop(text: str, phrases: list[BannedPhrase]) -> list[dict[str, Any]]:
    """Occurrences beyond each phrase's allowance, with the sentence index that holds them."""
    spans = split_sentences(text)
    hits: list[dict[str, Any]] = []
    for item in phrases:
        positions = [match.start() for match in re.finditer(re.escape(item.phrase), text)]
        if len(positions) <= item.allow:
            continue
        excess = positions[item.allow:]
        sentence_indexes = []
        for position in excess:
            index = next(
                (number for number, (start, end) in enumerate(spans) if start <= position < end),
                -1,
            )
            if index >= 0 and index not in sentence_indexes:
                sentence_indexes.append(index)
        hits.append({
            "phrase": item.phrase,
            "count": len(positions),
            "allow": item.allow,
            "source": item.source,
            "sentence_indexes": sentence_indexes,
        })
    return hits


RewriteCompletion = Callable[..., Awaitable[tuple[dict[str, Any], list[str]]]]

REWRITE_SYSTEM = (
    "你是中文小说的句级润色编辑。只改写给定的“目标句”，去掉其中列出的套话，"
    "换成贴合人物与场景的具体动作、感官细节或对白。不得改变事实、人物、事件顺序、"
    "视角和语气；长度与原句相近；不得新增人物、道具或信息。"
    "只输出 JSON：{\"rewrites\":[{\"id\":\"S1\",\"text\":\"改写后的目标句\"}]}。"
)


_REWRITE_LABEL_RE = re.compile(r"^(?:\[?S\d+\]?\s*)?(?:目标句|改写后?|改写句)\s*[:：]\s*")
_PROMPT_ECHO_MARKERS = ("需去除", "前句：", "后句：", "目标句：")
_QUOTE_PAIRS = (
    ("“", "”"), ("‘", "’"), ("（", "）"), ("【", "】"), ("「", "」"), ("『", "』"),
)
# Symmetric quotes cannot be balanced by counting pairs; only parity matters.
_SYMMETRIC_QUOTES = ('"',)


def _quote_balance(text: str) -> tuple[int, ...]:
    return tuple(text.count(left) - text.count(right) for left, right in _QUOTE_PAIRS) + tuple(
        text.count(mark) % 2 for mark in _SYMMETRIC_QUOTES
    )


async def rewrite_slop(
    text: str,
    phrases: list[BannedPhrase],
    *,
    settings: dict[str, Any],
    completion: RewriteCompletion,
    max_rewrites: int = 6,
) -> tuple[str, dict[str, Any]]:
    """Rewrite only offending sentences in one small call; never the whole draft."""
    hits = detect_slop(text, phrases)
    report: dict[str, Any] = {"hits": hits, "rewritten": [], "skipped": [], "warnings": []}
    if not hits or max_rewrites <= 0:
        return text, report
    spans = split_sentences(text)
    targets: list[int] = []
    for hit in hits:
        for index in hit["sentence_indexes"]:
            if index not in targets:
                targets.append(index)
    targets = sorted(targets)[:max_rewrites]
    banned_text = [item.phrase for item in phrases]
    items = []
    for number, index in enumerate(targets, start=1):
        start, end = spans[index]
        sentence = text[start:end]
        before = text[spans[index - 1][0]:spans[index - 1][1]] if index > 0 else ""
        after = text[spans[index + 1][0]:spans[index + 1][1]] if index + 1 < len(spans) else ""
        offending = [phrase for phrase in banned_text if phrase in sentence]
        items.append({
            "id": f"S{number}", "index": index, "before": before.strip(),
            "target": sentence.strip(), "after": after.strip(), "remove": offending,
        })
    user = "\n\n".join(
        f"[{item['id']}] 需去除：{'、'.join(item['remove'])}\n前句：{item['before'] or '（无）'}\n"
        f"目标句：{item['target']}\n后句：{item['after'] or '（无）'}"
        for item in items
    )
    try:
        result, warnings = await completion(
            settings,
            [{"role": "system", "content": REWRITE_SYSTEM}, {"role": "user", "content": user}],
            temperature=0.6,
            max_tokens=min(1600, 120 + 140 * len(items)),
            timeout_seconds=120,
            workload="revision",
            schema=REWRITE_SCHEMA,
        )
        report["warnings"].extend(warnings)
    except Exception as exc:  # noqa: BLE001 - slop repair is best effort
        report["warnings"].append(f"套话局部改写失败，保留原句：{exc}")
        return text, report
    replacements: dict[int, str] = {}
    rewrites = result.get("rewrites", []) if isinstance(result, dict) else []
    by_id = {
        str(item.get("id", "")).strip(): str(item.get("text", "")).strip()
        for item in rewrites if isinstance(item, dict)
    }
    for item in items:
        candidate = _REWRITE_LABEL_RE.sub("", by_id.get(item["id"], "").strip()).strip()
        original = item["target"]
        if candidate[:1] in "\"“" and original[:1] not in "\"“" and candidate.count(candidate[0]) == 1:
            # A model that wraps the whole sentence in quotes; dialogue keeps its own.
            candidate = candidate[1:].rstrip("\"”")
        reason = ""
        if not candidate:
            reason = "未返回改写"
        elif "\n" in candidate:
            reason = "改写含换行"
        elif any(label in candidate for label in _PROMPT_ECHO_MARKERS):
            reason = "改写夹带了提示词标签"
        elif any(phrase in candidate for phrase in item["remove"]):
            reason = "改写仍含套话"
        elif not (0.4 * len(original) <= len(candidate) <= 1.9 * len(original) + 6):
            reason = "改写长度异常"
        elif _quote_balance(candidate) != _quote_balance(original):
            # Splicing a sentence that opens or closes a quote differently
            # would break every later dialogue line (and the truncation gate).
            reason = "改写破坏了引号或括号配对"
        elif SequenceMatcher(None, original, candidate).ratio() < 0.2:
            reason = "改写与原句差异过大"
        if reason:
            report["skipped"].append({"id": item["id"], "target": original, "reason": reason})
            continue
        # Keep the closing punctuation of the original when the rewrite dropped it.
        if original[-1:] in "。！？!?" and candidate[-1:] not in "。！？!?”’」』":
            candidate += original[-1]
        replacements[item["index"]] = candidate
        report["rewritten"].append({"before": original, "after": candidate})
    if not replacements:
        return text, report
    pieces: list[str] = []
    cursor = 0
    for index, (start, end) in enumerate(spans):
        if index not in replacements:
            continue
        sentence = text[start:end]
        leading = sentence[: len(sentence) - len(sentence.lstrip())]
        pieces.append(text[cursor:start] + leading + replacements[index])
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces), report


# Keyed by server root *and* model: one llama.cpp port may serve several
# models (router / llama-swap), and token ids differ between vocabularies.
_TOKEN_CACHE: dict[tuple[str, str, str], list[int]] = {}


async def llama_cpp_logit_bias(
    settings: dict[str, Any],
    phrases: list[BannedPhrase],
    *,
    bias: float = -2.5,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[list[float]]:
    """Resolve distinctive token ids for banned phrases via llama.cpp ``/tokenize``.

    Only multi-character pieces are biased, so ordinary characters stay free.
    Returns ``[[token_id, bias], ...]``; empty when the endpoint is not
    llama.cpp or tokenisation fails.
    """
    caps = resolve_capabilities(settings)
    if not caps.logit_bias or caps.server != "llama_cpp":
        return []
    base = normalize_base_url(settings.get("base_url", ""))
    if not base:
        return []
    root = server_root(base)
    token_bias: dict[int, float] = {}
    try:
        async with httpx.AsyncClient(timeout=6, transport=transport) as client:
            for item in phrases:
                if len(item.phrase) < 3:
                    continue
                key = (root, str(settings.get("model", "") or ""), item.phrase)
                if key not in _TOKEN_CACHE:
                    response = await client.post(
                        f"{root}/tokenize",
                        json={"content": item.phrase, "with_pieces": True},
                        headers=auth_headers(settings),
                    )
                    response.raise_for_status()
                    tokens = response.json().get("tokens", [])
                    ids = []
                    for token in tokens:
                        if isinstance(token, dict):
                            piece = token.get("piece", "")
                            # llama.cpp returns invalid UTF-8 pieces (byte-fallback
                            # tokens shared by thousands of CJK characters) as a
                            # list of byte values; never bias those.
                            if not isinstance(piece, str):
                                continue
                            if len(piece.strip()) >= 2 and not set(piece.strip()) <= _COMMON_PIECES:
                                ids.append(int(token.get("id")))
                    _TOKEN_CACHE[key] = ids
                for token_id in _TOKEN_CACHE[key]:
                    token_bias[token_id] = bias
    except Exception:  # noqa: BLE001 - optional optimisation only
        return []
    return [[token_id, value] for token_id, value in token_bias.items()]


async def prose_stream_options(
    project: dict[str, Any], settings: dict[str, Any], config: dict[str, Any] | None = None
) -> Any:
    """PayloadOptions carrying llama.cpp anti-slop logit bias, or ``None``."""
    from app.llm.providers import PayloadOptions

    if not craft_setting(project, "antislop_enabled", config) or not craft_setting(
        project, "antislop_logit_bias", config
    ):
        return None
    phrases = [item for item in banned_phrases(project) if item.allow == 0 or item.source != "learned"]
    pairs = await llama_cpp_logit_bias(settings, phrases)
    if not pairs:
        return None
    # String keys would be re-tokenised by llama.cpp; pass ids through ``extra``.
    return PayloadOptions(extra={"logit_bias": pairs})


async def apply_antislop(
    text: str,
    project: dict[str, Any],
    settings: dict[str, Any],
    completion: RewriteCompletion,
    config: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Entry point for director and manual generation post-processing."""
    if not craft_setting(project, "antislop_enabled", config):
        return text, {"enabled": False, "hits": [], "rewritten": [], "skipped": [], "warnings": []}
    revised, report = await rewrite_slop(
        text,
        banned_phrases(project),
        settings=settings,
        completion=completion,
        max_rewrites=int(craft_setting(project, "antislop_max_rewrites", config)),
    )
    report["enabled"] = True
    report["remaining"] = detect_slop(revised, banned_phrases(project))
    return revised, report

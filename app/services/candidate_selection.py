"""Best-of-n drafting for key chapters.

Re3/DOC showed that generating several candidates and re-ranking them is
more reliable than serially revising one draft; Finding Flawed Fictions
showed that every LLM rewrite tends to add plot holes.  For chapters that
matter most (openings, climaxes, chapters the plan flags as key) the director
can write 2–3 candidates, filter them with cheap deterministic checks, and
let the planning model pick a winner in pairwise comparisons on a compact
rubric.  All candidates stay in the task checkpoint for the author.
"""
from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from ..core.text import relabel_ab
from ..domain.json_schemas import PAIRWISE_SCHEMA
from .antislop import banned_phrases, detect_slop
from .craft_settings import craft_setting

Completion = Callable[..., Awaitable[tuple[dict[str, Any], list[str]]]]

KEY_MARKERS = ("高潮", "决战", "揭晓", "反转", "climax", "转折点", "卷末", "终局", "开篇")

JUDGE_SYSTEM = (
    "你是严格的小说主编，比较同一章的两份候选稿，只依据下面五项挑出更好的一份："
    "1 因果：事件是否由人物选择推动、前后因果清楚；2 人物能动性：主要人物是否主动做出有代价的选择；"
    "3 对白辨识度：不同人物说话是否可区分、有信息差；4 新鲜度：是否少套话、少重复意象；"
    "5 计划交付：是否完成本章目标、转折与收束。篇幅长短本身不是优点。"
    "只输出 JSON：{\"winner\":\"A或B\",\"scores\":{\"A\":0-50,\"B\":0-50},\"reasons\":[\"不超过3条，每条20字内\"]}"
)


def is_key_chapter(chapter: dict[str, Any], index: int, total: int) -> bool:
    route = chapter.get("route", {}) if isinstance(chapter.get("route"), dict) else {}
    plan = chapter.get("plan", {}) if isinstance(chapter.get("plan"), dict) else {}
    if route.get("key_scene") or plan.get("key_scene") or route.get("is_key") or plan.get("is_key"):
        return True
    if index == 0:
        return True
    text = " ".join(str(route.get(key, "")) + str(plan.get(key, "")) for key in ("stage", "function", "goal", "turning_point", "title"))
    text += str(chapter.get("title", ""))
    return any(marker in text for marker in KEY_MARKERS) or (total > 3 and index == total - 1)


def candidate_count_for(
    project: dict[str, Any], chapter: dict[str, Any], index: int, config: dict[str, Any] | None = None
) -> int:
    count = int(craft_setting(project, "candidate_count", config))
    if count <= 1:
        return 1
    if craft_setting(project, "candidate_scope", config) == "all":
        return count
    total = len(project.get("chapters", []))
    return count if is_key_chapter(chapter, index, total) else 1


def deterministic_score(text: str, target_chars: int, project: dict[str, Any]) -> dict[str, Any]:
    """Cheap gate: length fit, cliché excess, repeated sentences, truncation."""
    compact = re.sub(r"\s+", "", text)
    chars = len(compact)
    ratio = chars / max(1, target_chars)
    length_penalty = 0 if 0.8 <= ratio <= 1.3 else min(30, int(abs(ratio - 1.05) * 40))
    slop = sum(item["count"] - item["allow"] for item in detect_slop(text, banned_phrases(project)))
    sentences = [item.strip() for item in re.split(r"[。！？!?\n]", text) if len(item.strip()) >= 8]
    repeats = len(sentences) - len(set(sentences))
    truncated = bool(compact) and compact[-1] not in "。！？!?”」』…）)"
    score = 100 - length_penalty - min(30, slop * 3) - min(20, repeats * 5) - (25 if truncated else 0)
    return {
        "score": max(0, score), "chars": chars, "slop_excess": slop,
        "repeated_sentences": repeats, "truncated": truncated,
    }


JUDGE_EXCERPT_CHARS = 6000


def judge_excerpt(text: str, limit: int = JUDGE_EXCERPT_CHARS) -> str:
    """Head and tail of a long draft: the ending carries the plan delivery."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    return text[:head] + "\n……（中段省略）……\n" + text[-(limit - head):]


async def _judge_once(
    first: str, second: str, plan_brief: str, settings: dict[str, Any], completion: Completion,
) -> dict[str, Any]:
    user = (
        f"【本章计划】\n{plan_brief or '未提供'}\n\n【候选A】\n{judge_excerpt(first)}"
        f"\n\n【候选B】\n{judge_excerpt(second)}"
    )
    result, _warnings = await completion(
        settings,
        [{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": user}],
        temperature=0.1,
        max_tokens=300,
        timeout_seconds=150,
        workload="critic",
        schema=PAIRWISE_SCHEMA,
    )
    label = str(result.get("winner", "")).strip().upper()[:1]
    if label not in {"A", "B"}:
        raise ValueError("评审未给出 A 或 B")
    reasons = [str(item)[:60] for item in result.get("reasons", [])][:3] if isinstance(result.get("reasons"), list) else []
    scores = result.get("scores", {}) if isinstance(result.get("scores"), dict) else {}
    return {"label": label, "reasons": reasons, "scores": scores}


async def pairwise_judge(
    left: str,
    right: str,
    plan_brief: str,
    settings: dict[str, Any],
    completion: Completion,
    *,
    left_name: str = "",
    right_name: str = "",
) -> dict[str, Any]:
    """Judge in both presentation orders; disagreement is a tie.

    Position bias is the dominant failure of LLM pairwise judges, so the pair
    is shown as (left, right) and (right, left).  Only a verdict that survives
    the swap counts; otherwise ``winner`` is ``"tie"`` and the caller falls
    back to the deterministic score.  If one order fails, the other is used.
    """
    verdicts: list[dict[str, Any]] = []
    errors: list[str] = []
    for swap in (False, True):
        first, second = (right, left) if swap else (left, right)
        try:
            verdict = await _judge_once(first, second, plan_brief, settings, completion)
        except Exception as exc:  # noqa: BLE001 - the other order may still succeed
            errors.append(str(exc)[:200])
            continue
        left_won = (verdict["label"] == "A") != swap
        names = {
            "A": (right_name if swap else left_name) or ("right" if swap else "left"),
            "B": (left_name if swap else right_name) or ("left" if swap else "right"),
        }
        reasons = verdict["reasons"]
        scores = verdict["scores"]
        if left_name and right_name:
            # Translate anonymous A/B back so "采用第 2 份：A 更清楚" never contradicts itself.
            reasons = [relabel_ab(item, names) for item in reasons]
            scores = {names.get(str(key).strip().upper(), str(key)): value for key, value in scores.items()}
        verdicts.append({"left_won": left_won, "reasons": reasons, "scores": scores, "swapped": swap})
    if not verdicts:
        raise RuntimeError("；".join(errors) or "评审调用失败")
    outcomes = {item["left_won"] for item in verdicts}
    if len(outcomes) > 1:
        winner = "tie"
    else:
        winner = "left" if outcomes.pop() else "right"
    reasons: list[str] = []
    for item in verdicts:
        for reason in item["reasons"]:
            if reason not in reasons:
                reasons.append(reason)
    collected: dict[str, list[Any]] = {}
    for item in verdicts:
        for key, value in item["scores"].items():
            collected.setdefault(str(key), []).append(value)
    scores: dict[str, Any] = {}
    for key, values in collected.items():
        numbers = [value for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
        scores[key] = (sum(numbers) / len(numbers)) if len(numbers) == len(values) else values[-1]
    return {
        "winner": winner,
        "reasons": reasons[:3],
        "scores": scores,
        "orders_judged": len(verdicts),
        "errors": errors,
    }


async def select_best_candidate(
    candidates: list[str],
    *,
    project: dict[str, Any],
    target_chars: int,
    plan_brief: str,
    settings: dict[str, Any],
    completion: Completion,
) -> dict[str, Any]:
    """Return ``{"index", "text", "report"}`` for the chosen candidate."""
    scored = [
        {"index": number, "text": text, "checks": deterministic_score(text, target_chars, project)}
        for number, text in enumerate(candidates)
        if str(text).strip()
    ]
    if not scored:
        raise ValueError("没有可比较的候选稿")
    report: dict[str, Any] = {"candidates": [
        {"index": item["index"], **item["checks"]} for item in scored
    ], "matches": []}
    # Drop candidates that fail the cheap gate badly when a clean one exists.
    viable = [item for item in scored if not item["checks"]["truncated"]] or scored
    viable.sort(key=lambda item: -item["checks"]["score"])
    champion = viable[0]
    for challenger in viable[1:]:
        try:
            verdict = await pairwise_judge(
                champion["text"], challenger["text"], plan_brief, settings, completion,
                left_name=f"第{champion['index'] + 1}份", right_name=f"第{challenger['index'] + 1}份",
            )
        except Exception as exc:  # noqa: BLE001 - fall back to the deterministic order
            report["matches"].append({
                "left": champion["index"], "right": challenger["index"],
                "winner": champion["index"], "error": str(exc)[:200],
            })
            continue
        if verdict["winner"] == "tie":
            # The judge flipped with the presentation order: no real preference.
            winner = (
                challenger
                if challenger["checks"]["score"] > champion["checks"]["score"]
                else champion
            )
        else:
            winner = champion if verdict["winner"] == "left" else challenger
        report["matches"].append({
            "left": champion["index"], "right": challenger["index"],
            "winner": winner["index"], "reasons": verdict["reasons"], "scores": verdict["scores"],
            "tie": verdict["winner"] == "tie", "orders_judged": verdict["orders_judged"],
        })
        champion = winner
    report["selected"] = champion["index"]
    return {"index": champion["index"], "text": champion["text"], "report": report}

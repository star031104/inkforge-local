"""ConStory-style focused critics.

The single critic received ~14k tokens of mixed context and returned at most
four issues, mostly unverifiable.  ConStory-Bench (ACL 2026 Findings) groups
long-form consistency errors into five families; each family needs different
evidence.  Here every family gets its own small checker that sees only the
context it needs plus the numbered draft (the same evidence-ID protocol as the
single critic), and the results are merged into the existing audit format.

The draft is sent as the first user message of every checker so endpoints
with prefix caching reuse it across the checkers.
"""
from __future__ import annotations

import hashlib
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from app.llm.budget import estimate_tokens
from app.llm.json_schemas import _ISSUE, object_schema
from app.story.evidence import (
    PENALTIES,
    _parse_ids,
    _resolve_ids,
    bind_evidence,
    downgrade_self_cleared,
    penalty_total,
    split_draft_units,
    verify_quote,
)

Completion = Callable[..., Awaitable[tuple[dict[str, Any], list[str]]]]


@dataclass(frozen=True)
class Checker:
    key: str
    label: str
    focus: str
    blocks: tuple[str, ...]


CHECKERS: tuple[Checker, ...] = (
    Checker(
        "character", "人物记忆与能力",
        "人物是否使用了不该知道的信息、忘记已知的事、说话方式或人格核心漂移、能力突然增强或丢失已掌握的技能、关系态度无铺垫突变。",
        ("characters", "epistemic", "relationships"),
    ),
    Checker(
        "facts", "事实细节",
        "外貌与可变外观、人名/称谓/地名写法、数量与数字、道具归属与状态、伤势是否与既有事实矛盾；以及是否原样复用近期显著描写。",
        ("facts", "continuity_state", "description_ledger"),
    ),
    Checker(
        "pov", "视角与语气",
        "叙事视角是否越界（限知视角写出他人内心或未见之事）、人称是否混乱、整体语气与本书叙事策略是否一致；开头是否自然承接已有正文结尾。",
        ("narrative", "existing_tail"),
    ),
    Checker(
        "timeline", "时间线、因果与弃坑",
        "事件时间先后与已知时间线是否矛盾、因果是否成立、是否完成本章路线规定的目标/转折/收束、是否提前写出后续事件；"
        "治理议程中本章应推进的线索是否被遗忘（弃坑），是否违规提前回收伏笔；若列出上章钩子，逐条判断承接情况。",
        ("route", "facts", "threads", "hooks"),
    ),
    Checker(
        "world", "世界规则与地理",
        "是否违反本书不可违背规则、世界书设定、能力体系限制、地理位置与移动距离、时代制度与器物。",
        ("rules", "world"),
    ),
)

# Runs only when the chapter is a debate / confrontation scene (the editorial
# route then fills the ``debate`` block); ordinary chapters never pay for it.
DEBATE_CHECKER = Checker(
    "debate", "交锋强度",
    "对手每条最强论点是否以完整、有力的形式出现（不得被削弱成稻草人或一句带过）；主角是否靠一句对方无法回应的现代金句、"
    "超时代概念或作者替对手失语取胜；对手是否抓住主角软肋反击；胜负是否由利益、权力与裁决者的考量决定并让主角付出代价；"
    "是否出现对手当场折服、纳头便拜、众人齐声叫好之类降智收场。",
    ("debate", "characters"),
)
ALL_CHECKERS: tuple[Checker, ...] = (*CHECKERS, DEBATE_CHECKER)


def active_checkers(blocks: dict[str, str]) -> tuple[Checker, ...]:
    """The five consistency checkers, plus the debate checker when it has context."""
    return ALL_CHECKERS if str(blocks.get("debate", "") or "").strip() else CHECKERS


# Shared by every checker so system + draft form one cacheable prefix; the
# checker's own focus and context follow in the last message.
CHECKER_SYSTEM = """你是长篇小说一致性审计组的成员。每次只负责最后一条消息指定的类别，不检查未指定的类别，不因个人文风偏好报错。
人物没有表现你期待的情绪、或采用不同但合理的写法，都不是错误。
只报告你确认存在的问题：若分析后认为并不矛盾、只是可以更好、或只想提醒后文注意，就不要输出该条。
穿越、重生题材中，穿越者本人随身带来的现代衣物器物、口音、知识和内心里的现代概念不是时代错误；只有当时的人或叙述把现代事物写成当时常见之物时才算。
严重度：high=与权威事实明确矛盾或泄露被禁止的真相；medium=有较强证据表明可能断裂；low=轻微问题。
候选草稿每句前有编号，如 [P3-S2]。每个问题必须在 evidence_ids 里给出 1-3 个真实存在的句子编号（连续多句写 "P3-S2~P3-S4"）；evidence 只可选填不超过30字的逐字摘录。
单一类别最多返回3个问题，多类别合并审计最多6个；message 不超过60字，suggestion 不超过40字。没有问题就返回空数组。
多类别合并审计时每个问题须用 checker 字段写明类别代号。若最后一条消息列出了带编号的章末钩子，用 hook_status 逐条给出 paid/advanced/ignored；没有钩子就省略。
只输出 JSON：{"issues":[{"checker":"类别代号（仅合并审计）","severity":"high/medium/low","message":"具体问题","suggestion":"最小修复建议","evidence_ids":["P3-S2"],"evidence":["可选摘录"],"authority_evidence":"对应设定或历史事实","reason":"为何冲突"}],"hook_status":[{"id":"H1a2b3c4d","status":"paid/advanced/ignored"}]}"""

BLOCK_LABELS = {
    "characters": "人物状态",
    "epistemic": "人物与读者知情边界",
    "relationships": "动态关系状态",
    "facts": "相关历史事实",
    "continuity_state": "物品与伤势状态",
    "description_ledger": "近期已经用过的显著描写",
    "narrative": "叙事策略与视角",
    "existing_tail": "当前章节已有正文结尾",
    "route": "本章路线与执行计划",
    "threads": "伏笔与暗线治理议程",
    "rules": "本书不可违背规则",
    "world": "相关世界书设定",
    "hooks": "章末钩子承接",
}
# Optional blocks kept out of BLOCK_LABELS so callers that fill every label do
# not switch on conditional checkers by accident.
OPTIONAL_BLOCK_LABELS = {"debate": "本章交锋结构"}


def _block_label(key: str) -> str:
    return BLOCK_LABELS.get(key) or OPTIONAL_BLOCK_LABELS.get(key, key)

# Per-block character caps keep every checker small.
BLOCK_CAPS = {
    "characters": 2600, "epistemic": 2400, "relationships": 1200,
    "facts": 2400, "continuity_state": 1000, "description_ledger": 1000,
    "narrative": 900, "existing_tail": 900, "route": 2600, "threads": 2200,
    "rules": 2200, "world": 1800, "hooks": 900, "debate": 1800,
}
# The merged low-risk checker sees every block at roughly half size.
MERGED_CAP_SHARE = 0.5
MERGED_MAX_ISSUES = 6


_HOOK_STATUS = {
    "type": "array",
    "items": object_schema(
        {"id": {"type": "string"}, "status": {"type": "string", "enum": ["paid", "advanced", "ignored"]}},
    ),
}
CHECKER_SCHEMA = object_schema(
    {"issues": {"type": "array", "items": _ISSUE, "maxItems": 3}, "hook_status": _HOOK_STATUS},
    ["issues"],
)
MERGED_SCHEMA = object_schema(
    {
        "issues": {
            "type": "array",
            "items": {
                **_ISSUE,
                "properties": {
                    **_ISSUE["properties"],
                    "checker": {"type": "string", "enum": [item.key for item in ALL_CHECKERS]},
                },
            },
            "maxItems": MERGED_MAX_ISSUES,
        },
        "hook_status": _HOOK_STATUS,
    },
    ["issues"],
)


def _bounded(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    return text[:head] + "\n……（中段省略）……\n" + text[-(limit - head):]


def checker_messages(
    checker: Checker, blocks: dict[str, str], numbered_draft: str
) -> list[dict[str, str]] | None:
    """Messages for one checker, or ``None`` when it has no context to check against."""
    parts = []
    for key in checker.blocks:
        content = _bounded(blocks.get(key, ""), BLOCK_CAPS.get(key, 1500))
        if content:
            parts.append(f"【{_block_label(key)}】\n{content}")
    if not parts and checker.key != "pov":
        return None
    return [
        {"role": "system", "content": CHECKER_SYSTEM},
        {"role": "user", "content": f"【候选草稿（每句前为证据编号）】\n{numbered_draft}"},
        {
            "role": "user",
            "content": (
                f"【本轮只负责：{checker.label}】\n检查：{checker.focus}\n\n"
                + ("\n\n".join(parts) or "（无额外权威上下文，仅依据草稿自身检查）")
            ),
        },
    ]


def merged_messages(blocks: dict[str, str], numbered_draft: str) -> list[dict[str, str]]:
    """One low-risk checker covering all five families with a compact context.

    System and draft messages are identical to the focused checkers', so an
    escalation to the full set reuses the cached prefix.
    """
    checkers = active_checkers(blocks)
    focus = "\n".join(f"- {checker.key}（{checker.label}）：{checker.focus}" for checker in checkers)
    parts = []
    seen: set[str] = set()
    for checker in checkers:
        for key in checker.blocks:
            if key in seen:
                continue
            seen.add(key)
            content = _bounded(blocks.get(key, ""), int(BLOCK_CAPS.get(key, 1500) * MERGED_CAP_SHARE))
            if content:
                parts.append(f"【{_block_label(key)}】\n{content}")
    return [
        {"role": "system", "content": CHECKER_SYSTEM},
        {"role": "user", "content": f"【候选草稿（每句前为证据编号）】\n{numbered_draft}"},
        {
            "role": "user",
            "content": (
                "【本轮为合并审计，负责以下全部类别】\n" + focus + "\n\n"
                + ("\n\n".join(parts) or "（无额外权威上下文，仅依据草稿自身检查）")
            ),
        },
    ]


def estimate_focused_tokens(blocks: dict[str, str], numbered_draft: str) -> int:
    total = 0
    for checker in active_checkers(blocks):
        messages = checker_messages(checker, blocks, numbered_draft)
        if messages:
            total += estimate_tokens("\n".join(item["content"] for item in messages))
    return total


def _validate_checker(result: dict[str, Any]) -> None:
    if not isinstance(result.get("issues", []), list):
        raise ValueError("字段 issues 必须是数组")
    for issue in result.get("issues", []):
        if not isinstance(issue, dict):
            raise ValueError("审计问题必须是对象")
        if issue.get("severity") in {"high", "medium"} and not (issue.get("evidence_ids") or issue.get("evidence")):
            raise ValueError("严重审计问题必须提供 evidence_ids 句子编号或逐字引文")


# ---------------------------------------------------------------------------
# Revision re-audit = verification
#
# The refinement loop audits the same chapter again after every revision.  A
# fresh full audit re-reads the whole chapter and invents new nitpicks, so the
# score never converges.  When the new draft is a small revision of the last
# audited one, the re-audit instead
#
# 1. verifies every previously reported issue (resolved/partially/unresolved)
#    in one model call, and
# 2. checks only the changed/new sentences for new problems (issues whose
#    evidence lies outside the changed sentences are dropped).
#
# The memory is process-local on purpose: after a restart, for a non-revision,
# or when too much changed, the audit is a full one again.
# ---------------------------------------------------------------------------
REVISION_SIMILARITY = 0.5
# A revision that rewrites more than this share of sentences gets a full audit.
CHANGED_SHARE_FULL_RERUN = 0.35
# After this many consecutive verification rounds the next audit is full, so
# accumulated small edits cannot drift arbitrarily far from the last full read.
MAX_VERIFY_CHAIN = 3
_RUN_MEMORY_LIMIT = 256
_last_runs: OrderedDict[str, dict[str, Any]] = OrderedDict()
# Authority blocks that do not depend on the draft text.  facts / epistemic /
# world / threads are retrieved with the draft as the query and legitimately
# shift between revisions, so they do not invalidate a verification.
STABLE_CONTEXT_KEYS = (
    "characters", "relationships", "continuity_state", "description_ledger", "narrative",
    "existing_tail", "route", "rules", "hooks", "debate",
)
VERIFY_STATUSES = ("resolved", "partially", "unresolved")
_DOWNGRADE = {"high": "medium", "medium": "low", "low": "low"}


def _draft_similarity(left: str, right: str) -> float:
    from app.manuscript_quality import _similarity

    return _similarity(left, right)


def _context_hash(blocks: dict[str, str], keys: tuple[str, ...] | None = None) -> str:
    selected = sorted(keys if keys is not None else blocks)
    canonical = "\x1f".join(f"{key}\x1e{blocks.get(key, '')}" for key in selected)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _compact_sentence(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def changed_units(old: str, new: str) -> list[tuple[str, str]]:
    """``(evidence_id, sentence)`` of sentences in ``new`` that ``old`` lacks."""
    old_set = {_compact_sentence(sentence) for _id, _start, sentence in split_draft_units(old)}
    return [
        (unit_id, sentence)
        for unit_id, _start, sentence in split_draft_units(new)
        if _compact_sentence(sentence) not in old_set
    ]


def _revision_changes(
    previous: dict[str, Any] | None, draft: str, blocks: dict[str, str] | None
) -> list[tuple[str, str]] | None:
    """Changed sentences when ``draft`` is a small revision of the last audit, else ``None``."""
    if not previous:
        return None
    if hashlib.sha256(draft.encode("utf-8")).hexdigest() == previous["draft_hash"]:
        return None  # same text audited again on purpose: give a full answer
    if blocks is not None and previous.get("stable_hash") != _context_hash(blocks, STABLE_CONTEXT_KEYS):
        return None  # route/rules/characters changed: earlier verdicts are stale
    if _draft_similarity(previous["draft"], draft) < REVISION_SIMILARITY:
        return None
    total = len(split_draft_units(draft))
    changed = changed_units(previous["draft"], draft)
    if not total or len(changed) > CHANGED_SHARE_FULL_RERUN * total:
        return None
    return changed


def plan_verification(
    audit_key: str, draft: str, blocks: dict[str, str] | None = None
) -> dict[str, Any] | None:
    """``{"previous", "changed"}`` when the re-audit can be a verification, else ``None``."""
    previous = _last_runs.get(audit_key) if audit_key else None
    if not previous or not previous.get("complete") or int(previous.get("verify_chain", 0)) >= MAX_VERIFY_CHAIN:
        return None
    changed = _revision_changes(previous, draft, blocks)
    if changed is None or (not changed and not previous.get("issues")):
        return None
    return {"previous": previous, "changed": changed}


def has_previous_run(audit_key: str) -> bool:
    return bool(audit_key) and audit_key in _last_runs


def _issue_quotes(draft: str, issue: dict[str, Any]) -> list[str]:
    quotes = [
        str(item["quote"]) for item in _resolve_ids(draft, _parse_ids(issue.get("evidence_ids", [])))
        if item.get("verified")
    ]
    raw = issue.get("evidence", [])
    for item in raw if isinstance(raw, list) else [raw]:
        text = str(item.get("quote", "") if isinstance(item, dict) else item or "").strip()
        if text:
            quotes.append(text)
    return [quote[:120] for quote in dict.fromkeys(quotes)][:3]


def _memory_issue(draft: str, issue: dict[str, Any]) -> dict[str, Any]:
    return {
        "issue_id": str(issue.get("issue_id", "")),
        "checker": str(issue.get("checker", "")),
        "category": str(issue.get("category", "")),
        "severity": str(issue.get("severity", "medium")).lower(),
        "message": str(issue.get("message", ""))[:200],
        "suggestion": str(issue.get("suggestion", ""))[:120],
        "quotes": _issue_quotes(draft, issue),
    }


def assign_issue_ids(issues: list[dict[str, Any]], start: int) -> int:
    """Give every issue without one a stable ``issue_id``; returns the next number."""
    number = start
    for issue in issues:
        if not issue.get("issue_id"):
            issue["issue_id"] = f"I{number}"
            number += 1
    return number


def remember_run(
    audit_key: str,
    draft: str,
    report: list[dict[str, Any]],
    blocks: dict[str, str] | None = None,
    mode: str = "full",
    *,
    issues: list[dict[str, Any]] | None = None,
    hook_status: list[dict[str, str]] | None = None,
    verify_chain: int = 0,
    next_issue: int = 1,
) -> None:
    if not audit_key:
        return
    flagged = [
        item["checker"] for item in report
        if item.get("status") == "failed"
        or (item.get("status") in {"completed", "carried"} and int(item.get("issues", 0) or 0) > 0)
    ]
    _last_runs[audit_key] = {
        "draft": draft[:20000],
        "draft_hash": hashlib.sha256(draft.encode("utf-8")).hexdigest(),
        "context_hash": _context_hash(blocks) if blocks is not None else "",
        "stable_hash": _context_hash(blocks, STABLE_CONTEXT_KEYS) if blocks is not None else "",
        "flagged": flagged,
        "mode": mode,
        # A run where a checker failed has blind spots: never verify against it.
        "complete": not any(item.get("status") == "failed" for item in report),
        "issues": [_memory_issue(draft, item) for item in issues or [] if isinstance(item, dict)],
        "hook_status": list(hook_status or []),
        "verify_chain": verify_chain,
        "next_issue": next_issue,
    }
    _last_runs.move_to_end(audit_key)
    while len(_last_runs) > _RUN_MEMORY_LIMIT:
        _last_runs.popitem(last=False)


VERIFY_SCHEMA = object_schema(
    {
        "results": {
            "type": "array",
            "items": object_schema(
                {
                    "id": {"type": "string"},
                    "status": {"type": "string", "enum": list(VERIFY_STATUSES)},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
                ["id", "status"],
            ),
        }
    },
    ["results"],
)

VERIFY_TASK = """【本轮为修订复核，不是重新审计】
上一轮审计对旧稿提出了下列问题（带编号）。候选草稿是据此修订后的新稿。逐条判断每个问题在新稿中的状态：
- resolved：问题已消除；
- partially：有改善但仍有残留，evidence_ids 写出残留所在句编号；
- unresolved：问题仍然存在，evidence_ids 写出新稿中的证据句编号。
只判断列出的问题，不得提出新问题，不得因措辞偏好改判；每个编号都必须给出一条结果。
本轮输出格式改为：{"results":[{"id":"I1","status":"resolved/partially/unresolved","evidence_ids":["P3-S2"],"note":"不超过30字"}]}"""

DELTA_TASK = """【本轮为修订稿增量审计（合并审计各类别，仅限改动句）】
本稿是对已审计旧稿的局部修订，未改动的句子已经审计过，不得再报。
只检查下列改动或新增的句子：
{changed}
每个问题的 evidence_ids 必须全部取自上述编号；证据落在其他句子的问题一律不要报告。最多3个问题，没有问题就返回空数组。

"""
_DELTA_CHANGED_CAP = 3000


def verification_messages(
    prior: list[dict[str, Any]], numbered_draft: str
) -> list[dict[str, str]]:
    lines = []
    for issue in prior:
        quotes = "｜".join(issue.get("quotes", [])) or "（无）"
        lines.append(
            f"[{issue['issue_id']}]（{issue.get('category') or issue.get('checker')}｜{issue.get('severity')}）"
            f"问题：{issue.get('message', '')}；建议：{issue.get('suggestion', '') or '无'}；旧稿证据：{quotes}"
        )
    return [
        {"role": "system", "content": CHECKER_SYSTEM},
        {"role": "user", "content": f"【候选草稿（每句前为证据编号）】\n{numbered_draft}"},
        {"role": "user", "content": VERIFY_TASK + "\n\n【上一轮问题清单】\n" + "\n".join(lines)},
    ]


def delta_messages(
    blocks: dict[str, str], numbered_draft: str, changed: list[tuple[str, str]]
) -> list[dict[str, str]]:
    """The merged checker restricted to the changed sentences (same cached prefix)."""
    messages = merged_messages(blocks, numbered_draft)
    listing = _bounded("\n".join(f"[{unit_id}]{sentence}" for unit_id, sentence in changed), _DELTA_CHANGED_CAP)
    body = messages[-1]["content"].replace("【本轮为合并审计，负责以下全部类别】", "【负责类别】", 1)
    messages[-1] = {"role": "user", "content": DELTA_TASK.format(changed=listing) + body}
    return messages


def _validate_verification(result: dict[str, Any]) -> None:
    if not isinstance(result.get("results", []), list):
        raise ValueError("字段 results 必须是数组")


def _unit_ids(draft: str) -> list[str]:
    return [unit_id for unit_id, _start, _sentence in split_draft_units(draft)]


def _expand_ids(raw: Any, order: list[str]) -> list[str] | None:
    """Evidence ids with ranges expanded; ``None`` when any id does not exist."""
    expanded: list[str] = []
    for item in _parse_ids(raw):
        if isinstance(item, tuple):
            if item[0] not in order or item[1] not in order:
                return None
            left, right = sorted((order.index(item[0]), order.index(item[1])))
            expanded.extend(order[left:right + 1])
        elif item in order:
            expanded.append(item)
        else:
            return None
    return expanded


def within_changed(issue: dict[str, Any], order: list[str], changed_ids: set[str]) -> bool:
    ids = _expand_ids(issue.get("evidence_ids", []), order)
    return bool(ids) and all(item in changed_ids for item in ids or [])


def _locate_quotes(draft: str, quotes: list[str]) -> list[str]:
    units = split_draft_units(draft)
    for quote in quotes:
        found = verify_quote(draft, quote)
        if found.get("verified"):
            start = int(found.get("start", 0))
            unit_id = next((uid for uid, ustart, _s in reversed(units) if ustart <= start), "")
            if unit_id:
                return [unit_id]
    return []


def carry_prior_issue(
    prior: dict[str, Any], verdict: dict[str, Any] | None, draft: str, order: list[str]
) -> dict[str, Any] | None:
    """The prior issue as it stands in the new draft, or ``None`` when resolved.

    A missing verdict counts as unresolved.  ``partially`` keeps the issue one
    severity lower; an unresolved issue whose evidence cannot be located in the
    new draft is kept as ``low`` so it stays visible without blocking.
    """
    status = str((verdict or {}).get("status", "unresolved")).lower()
    if status not in VERIFY_STATUSES:
        status = "unresolved"
    if status == "resolved":
        return None
    severity = prior.get("severity", "medium")
    if severity not in PENALTIES:
        severity = "medium"
    ids = _expand_ids((verdict or {}).get("evidence_ids", []), order) or _locate_quotes(draft, prior.get("quotes", []))
    item = {
        "issue_id": prior["issue_id"],
        "checker": prior.get("checker", ""),
        "category": prior.get("category", ""),
        "message": prior.get("message", ""),
        "suggestion": prior.get("suggestion", ""),
        "severity": _DOWNGRADE[severity] if status == "partially" else severity,
        "verification": status,
        "evidence_ids": ids[:3],
    }
    if not ids:
        item["severity"] = "low"
        item["evidence_unlocated"] = True
    if (verdict or {}).get("note"):
        item["verification_note"] = str(verdict["note"])[:60]
    return item


# ---------------------------------------------------------------------------
# Risk-gated critic budget (ConWriter): a cheap deterministic risk score picks
# between one merged checker and the five focused checkers.
# ---------------------------------------------------------------------------
RISK_WEIGHTS = {
    "key_chapter": 30,
    "continuity_conflict": 30,
    "prior_debt": 20,
    "continuity_mention": 10,
    "first_audit": 10,
}
DEFAULT_RISK_THRESHOLD = 40


def score_critic_risk(signals: dict[str, Any], threshold: int = DEFAULT_RISK_THRESHOLD) -> dict[str, Any]:
    """Turn deterministic draft signals into ``{score, level, reasons}``."""
    score = 0
    reasons: list[str] = []

    def add(points: int, reason: str) -> None:
        nonlocal score
        if points > 0:
            score += points
            reasons.append(f"{reason}（+{points}）")

    if signals.get("key_chapter"):
        add(RISK_WEIGHTS["key_chapter"], "关键章节（开篇/高潮/计划标记）")
    if int(signals.get("continuity_conflicts", 0) or 0):
        add(RISK_WEIGHTS["continuity_conflict"], "本地检出道具/伤势连续性冲突")
    elif int(signals.get("continuity_mentions", 0) or 0):
        add(RISK_WEIGHTS["continuity_mention"], "涉及被追踪的道具或伤势")
    entities = list(signals.get("new_entities", []) or [])
    if entities:
        add(min(20, 8 * len(entities)), f"新出现命名实体 {len(entities)} 个：{'、'.join(entities[:4])}")
    mechanical = int(signals.get("mechanical_issues", 0) or 0)
    if mechanical:
        add(min(20, 10 * mechanical), f"机械门禁问题 {mechanical} 项")
    if int(signals.get("slop_excess", 0) or 0):
        add(5, f"残留套话 {int(signals['slop_excess'])} 处")
    obligations = int(signals.get("obligations", 0) or 0)
    if obligations:
        add(min(20, 10 * obligations), f"本章有到期线索/钩子 {obligations} 项")
    if signals.get("prior_debt"):
        add(RISK_WEIGHTS["prior_debt"], "本章有历史质量债")
    if signals.get("first_audit"):
        add(RISK_WEIGHTS["first_audit"], "首次审计")
    return {
        "score": score,
        "threshold": int(threshold),
        "level": "high" if score >= int(threshold) else "low",
        "reasons": reasons,
    }


def choose_budget(setting: str, risk: dict[str, Any] | None) -> str:
    """``"full"`` or ``"merged"`` for a ``critic_budget`` setting and a risk score."""
    if setting == "full":
        return "full"
    if setting == "light":
        return "merged"
    return "full" if risk and risk.get("level") == "high" else "merged"


def _sort_and_score(
    issues: list[dict[str, Any]], weights: dict[str, Any] | None = None
) -> tuple[int, bool]:
    order = {"high": 0, "medium": 1, "low": 2}
    for item in issues:
        if isinstance(item, dict):
            downgrade_self_cleared(item)
    issues.sort(key=lambda item: order.get(str(item.get("severity", "medium")).lower(), 1))
    score = max(0, 100 - penalty_total(issues, weights))
    has_high = any(str(item.get("severity", "")).lower() == "high" for item in issues)
    return score, has_high


def _hook_status(result: dict[str, Any]) -> list[dict[str, str]]:
    raw = result.get("hook_status", []) if isinstance(result.get("hook_status"), list) else []
    return [
        {"id": str(item.get("id", "")).strip(), "status": str(item.get("status", "")).strip().lower()}
        for item in raw
        if isinstance(item, dict) and str(item.get("id", "")).strip()
        and str(item.get("status", "")).strip().lower() in {"paid", "advanced", "ignored"}
    ]


async def _run_checkers(
    settings: dict[str, Any],
    blocks: dict[str, str],
    numbered_draft: str,
    *,
    completion: Completion,
    prepare: Callable[[list[dict[str, str]]], list[dict[str, str]]] | None,
    only: set[str] | None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    warnings: list[str] = []
    report: list[dict[str, Any]] = []
    hook_status: list[dict[str, str]] = []
    failures = 0
    ran = 0
    for checker in active_checkers(blocks):
        if only is not None and checker.key not in only:
            report.append({"checker": checker.key, "status": "carried", "issues": 0})
            continue
        messages = checker_messages(checker, blocks, numbered_draft)
        if messages is None:
            report.append({"checker": checker.key, "status": "skipped"})
            continue
        if prepare is not None:
            messages = prepare(messages)
        ran += 1
        try:
            result, checker_warnings = await completion(
                settings,
                messages,
                temperature=0.1,
                max_tokens=600,
                timeout_seconds=150,
                validate=_validate_checker,
                schema=CHECKER_SCHEMA,
                workload="critic",
            )
        except Exception as exc:  # noqa: BLE001 - one failing checker must not sink the audit
            from app.llm.errors import recoverable_model_error

            if not recoverable_model_error(exc):
                raise
            failures += 1
            warnings.append(f"“{checker.label}”审计未完成：{exc}")
            report.append({"checker": checker.key, "status": "failed"})
            continue
        warnings.extend(f"{checker.label}：{item}" for item in checker_warnings)
        if checker.key == "timeline":
            hook_status = _hook_status(result)
        found = 0
        for issue in result.get("issues", [])[:3]:
            if not isinstance(issue, dict):
                continue
            item = dict(issue)
            item["category"] = f"{checker.label}" + (
                f"/{issue['category']}" if issue.get("category") and issue.get("category") != checker.label else ""
            )
            item["checker"] = checker.key
            issues.append(item)
            found += 1
        report.append({"checker": checker.key, "status": "completed", "issues": found})
    return {
        "issues": issues, "warnings": warnings, "report": report, "ran": ran,
        "failures": failures, "hook_status": hook_status,
    }


async def _run_merged(
    settings: dict[str, Any],
    blocks: dict[str, str],
    numbered_draft: str,
    *,
    completion: Completion,
    prepare: Callable[[list[dict[str, str]]], list[dict[str, str]]] | None,
    messages: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    messages = messages or merged_messages(blocks, numbered_draft)
    if prepare is not None:
        messages = prepare(messages)
    labels = {checker.key: checker.label for checker in active_checkers(blocks)}
    try:
        result, call_warnings = await completion(
            settings,
            messages,
            temperature=0.1,
            max_tokens=900,
            timeout_seconds=180,
            validate=_validate_checker,
            schema=MERGED_SCHEMA,
            workload="critic",
        )
    except Exception as exc:  # noqa: BLE001 - escalation to the full set handles it
        from app.llm.errors import recoverable_model_error

        if not recoverable_model_error(exc):
            raise
        return {
            "issues": [], "warnings": [f"合并审计未完成：{exc}"], "ran": 1, "failures": 1,
            "report": [{"checker": "merged", "status": "failed"}], "hook_status": [],
        }
    issues = []
    for issue in result.get("issues", [])[:MERGED_MAX_ISSUES]:
        if not isinstance(issue, dict):
            continue
        item = dict(issue)
        key = str(issue.get("checker", "")).strip()
        label = labels.get(key, "合并审计")
        item["category"] = label + (
            f"/{issue['category']}" if issue.get("category") and issue.get("category") != label else ""
        )
        item["checker"] = key if key in labels else "merged"
        issues.append(item)
    return {
        "issues": issues,
        "warnings": [f"合并审计：{item}" for item in call_warnings],
        "ran": 1,
        "failures": 0,
        "report": [{"checker": "merged", "status": "completed", "issues": len(issues)}],
        "hook_status": _hook_status(result),
    }


def _escalates(issue: dict[str, Any], escalate_on: tuple[str, ...], draft: str) -> bool:
    """Whether a merged-checker issue triggers the full checker set.

    ``"medium_verified"`` escalates on a medium issue only when its evidence
    actually resolves in the draft, so a vague model claim cannot double the
    audit cost.
    """
    severity = str(issue.get("severity", "")).lower()
    if severity in escalate_on:
        return True
    if severity == "medium" and "medium_verified" in escalate_on and draft:
        try:
            return bool(bind_evidence(draft, issue).get("evidence_verified"))
        except Exception:  # noqa: BLE001 - unverifiable evidence simply does not escalate
            return False
    return False


def _carry_merged(merged: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    """Keep merged findings the full checker set could not re-examine.

    An escalated issue must not vanish because its focused checker then
    failed: merged issues of every checker that did not complete are kept,
    as are unattributed ones when any checker failed.  When every checker
    failed the merged result stands in for the whole audit.
    """
    completed = {
        item.get("checker") for item in outcome["report"] if item.get("status") == "completed"
    }
    if outcome["ran"] and outcome["failures"] == outcome["ran"]:
        return {
            **merged,
            "warnings": [],
            "report": outcome["report"],
            "carried": len(merged["issues"]),
            "issues": [dict(item, carried_from="merged") for item in merged["issues"]],
        }
    carried = [
        dict(item, carried_from="merged")
        for item in merged["issues"]
        if (item.get("checker") != "merged" and item.get("checker") not in completed)
        or (item.get("checker") == "merged" and outcome["failures"])
    ]
    return {
        **outcome,
        "issues": [*outcome["issues"], *carried],
        "carried": len(carried),
        "hook_status": outcome["hook_status"] or merged["hook_status"],
    }


Prepare = Callable[[list[dict[str, str]]], list[dict[str, str]]] | None


async def _full_audit(
    settings: dict[str, Any],
    blocks: dict[str, str],
    numbered_draft: str,
    *,
    completion: Completion,
    prepare: Prepare,
    draft: str,
    budget: str,
    escalate_on: tuple[str, ...],
) -> dict[str, Any]:
    """Merged checker (escalating) or the full checker set over the whole draft."""
    mode = "merged" if budget == "merged" else "full"
    escalated = False
    merged_report: list[dict[str, Any]] = []
    warnings: list[str] = []
    calls = 0
    if mode == "merged":
        merged = await _run_merged({**settings, "_critic_budget": "merged"}, blocks, numbered_draft, completion=completion, prepare=prepare)
        calls += 1
        warnings.extend(merged["warnings"])
        triggers = [item for item in merged["issues"] if _escalates(item, escalate_on, draft)]
        if merged["failures"] or triggers:
            escalated = True
            merged_report = [{**merged["report"][0], "status": "escalated"}]
            mode = "full"
        else:
            outcome = merged
    if mode == "full":
        outcome = await _run_checkers(
            {**settings, "_critic_budget": "merged→full" if escalated else "full"}, blocks, numbered_draft, completion=completion, prepare=prepare, only=None,
        )
        calls += outcome["ran"]
        warnings.extend(outcome["warnings"])
        if escalated and not merged["failures"]:
            outcome = _carry_merged(merged, outcome)
            if outcome.get("carried"):
                warnings.append(f"分类审计未覆盖的类别沿用合并审计结论 {outcome['carried']} 条")
        if outcome["ran"] and outcome["failures"] == outcome["ran"]:
            raise RuntimeError("全部分类审计调用均失败")
    return {
        **outcome,
        "mode": mode,
        "warnings": warnings,
        "calls": calls,
        "report": merged_report + outcome["report"],
        "memory_report": outcome["report"],
        "budget_used": "merged→full" if escalated else mode,
    }


async def _verify_prior(
    settings: dict[str, Any],
    prior: list[dict[str, Any]],
    numbered_draft: str,
    *,
    completion: Completion,
    prepare: Prepare,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """One model call: ``{issue_id: {"status", "evidence_ids", "note"}}``."""
    messages = verification_messages(prior, numbered_draft)
    if prepare is not None:
        messages = prepare(messages)
    result, call_warnings = await completion(
        {**settings, "_critic_budget": "verify"},
        messages,
        temperature=0.1,
        max_tokens=min(1200, 200 + 90 * len(prior)),
        timeout_seconds=150,
        validate=_validate_verification,
        schema=VERIFY_SCHEMA,
        workload="critic",
    )
    raw = result.get("results", []) if isinstance(result.get("results"), list) else []
    verdicts = {
        str(item.get("id", "")).strip().upper(): item for item in raw if isinstance(item, dict)
    }
    return verdicts, [f"修订复核：{item}" for item in call_warnings]


async def _verify_audit(
    settings: dict[str, Any],
    blocks: dict[str, str],
    numbered_draft: str,
    plan: dict[str, Any],
    *,
    completion: Completion,
    prepare: Prepare,
    draft: str,
) -> dict[str, Any] | None:
    """Verification of prior issues + check of changed sentences; ``None`` → full audit."""
    prior = plan["previous"].get("issues", [])
    changed = plan["changed"]
    order = _unit_ids(draft)
    warnings: list[str] = []
    report: list[dict[str, Any]] = []
    verdicts: dict[str, dict[str, Any]] = {}
    calls = 0
    if prior:
        verdicts, call_warnings = await _verify_prior(settings, prior, numbered_draft, completion=completion, prepare=prepare)
        calls += 1
        warnings.extend(call_warnings)
    carried: list[dict[str, Any]] = []
    resolved: list[dict[str, Any]] = []
    for issue in prior:
        verdict = verdicts.get(issue["issue_id"].upper())
        item = carry_prior_issue(issue, verdict, draft, order)
        if item is not None:
            carried.append(item)
            continue
        resolved.append({
            key: issue.get(key, "") for key in ("issue_id", "checker", "category", "severity", "message")
        } | {"note": str((verdict or {}).get("note", ""))[:60]})
    if prior:
        report.append({"checker": "verify", "status": "completed", "issues": len(carried), "resolved": len(resolved)})
    new_issues: list[dict[str, Any]] = []
    dropped = 0
    if changed:
        delta = await _run_merged(
            {**settings, "_critic_budget": "verify"}, blocks, numbered_draft, completion=completion, prepare=prepare,
            messages=delta_messages(blocks, numbered_draft, changed),
        )
        calls += 1
        if delta["failures"]:
            return None
        warnings.extend(delta["warnings"])
        changed_ids = {unit_id for unit_id, _sentence in changed}
        for item in delta["issues"]:
            if within_changed(item, order, changed_ids):
                new_issues.append(item)
            else:
                dropped += 1
        report.append({"checker": "delta", "status": "completed", "issues": len(new_issues), "dropped": dropped})
    if dropped:
        warnings.append(f"增量审计有 {dropped} 条问题的证据不在改动句内，已忽略")
    return {
        "issues": carried + new_issues,
        "resolved": resolved,
        "warnings": warnings,
        "report": report,
        "calls": calls,
        "verification": [
            {"id": issue["issue_id"], "status": str((verdicts.get(issue["issue_id"].upper()) or {}).get("status", "unresolved"))}
            for issue in prior
        ],
        "changed_ids": [unit_id for unit_id, _sentence in changed],
        "dropped": dropped,
    }


def _assemble(
    issues: list[dict[str, Any]], *, ran: int, failures: int, warnings: list[str], extra: dict[str, Any],
    weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    score, has_high = _sort_and_score(issues, weights)
    verdict = "pass" if not has_high and score >= 85 and ran > failures else "revise"
    if ran == 0:
        # Nothing was actually checked: never report a clean pass.
        warnings.append("本轮没有任何分类审计实际运行，不能判定为通过")
    brief = "；".join(
        f"[{item['category']}] {item.get('suggestion') or item.get('message', '')}" for item in issues[:4]
    )[:160]
    return {
        "score": score,
        "verdict": verdict,
        "issues": issues,
        "strengths": [],
        "revision_brief": brief,
        "critic_mode": "focused",
        "adaptive_rerun": None,
        **extra,
    }


async def run_focused_critics(
    settings: dict[str, Any],
    blocks: dict[str, str],
    numbered_draft: str,
    *,
    completion: Completion,
    prepare: Prepare = None,
    audit_key: str = "",
    draft: str = "",
    budget: str = "full",
    escalate_on: tuple[str, ...] = ("high", "medium"),
) -> tuple[dict[str, Any], list[str]]:
    """Run the category checkers and merge into the audit format.

    ``budget="merged"`` runs one compact checker covering all families and
    escalates to the full set when it reports an issue whose severity is in
    ``escalate_on`` (or fails).  With ``audit_key`` and ``draft``, a small
    revision of the last audited draft is *verified* instead (see
    ``plan_verification``): ``audit_mode`` in the result says which happened.
    """
    previous = _last_runs.get(audit_key) if audit_key else None
    next_issue = int((previous or {}).get("next_issue", 1))
    warnings: list[str] = []
    # Author-calibrated scoring (``settings.audit_weights``); None → defaults.
    weights = settings.get("audit_weights") if isinstance(settings, dict) else None
    plan = plan_verification(audit_key, draft, blocks) if draft else None
    if plan is not None:
        try:
            verified = await _verify_audit(
                settings, blocks, numbered_draft, plan, completion=completion, prepare=prepare, draft=draft,
            )
        except Exception as exc:  # noqa: BLE001 - a failed verification falls back to a full audit
            from app.llm.errors import recoverable_model_error

            if not recoverable_model_error(exc):
                raise
            verified = None
            warnings.append(f"修订复核未完成，改为完整审计：{exc}")
        if verified is not None:
            issues = verified["issues"]
            next_issue = assign_issue_ids(issues, next_issue)
            hook_status = list(plan["previous"].get("hook_status", []))
            remember_run(
                audit_key, draft, verified["report"], blocks, mode=str(plan["previous"].get("mode", "full")),
                issues=issues, hook_status=hook_status,
                verify_chain=int(plan["previous"].get("verify_chain", 0)) + 1, next_issue=next_issue,
            )
            warnings.extend(verified["warnings"])
            return _assemble(issues, ran=verified["calls"], failures=0, warnings=warnings, extra={
                "critic_budget": "verify",
                "critic_calls": verified["calls"],
                "checkers": verified["report"],
                "hook_status": hook_status,
                "audit_mode": "verify",
                "prior_issue_ids": [item["issue_id"] for item in plan["previous"].get("issues", [])],
                "resolved_issues": verified["resolved"],
                "verification": verified["verification"],
                "changed_sentence_ids": verified["changed_ids"],
                "out_of_scope_dropped": verified["dropped"],
            }, weights=weights), warnings
    full = await _full_audit(
        settings, blocks, numbered_draft, completion=completion, prepare=prepare, draft=draft,
        budget=budget, escalate_on=escalate_on,
    )
    warnings.extend(full["warnings"])
    issues = full["issues"]
    next_issue = assign_issue_ids(issues, next_issue)
    if draft:
        remember_run(
            audit_key, draft, full["memory_report"], blocks, mode=full["mode"],
            issues=issues, hook_status=full["hook_status"], next_issue=next_issue,
        )
    return _assemble(issues, ran=full["ran"], failures=full["failures"], warnings=warnings, extra={
        "critic_budget": full["budget_used"],
        "critic_calls": full["calls"],
        "checkers": full["report"],
        "hook_status": full["hook_status"],
        "audit_mode": "full",
        "prior_issue_ids": [],
        "resolved_issues": [],
    }, weights=weights), warnings

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

from ..core.model_budget import estimate_tokens
from ..domain.json_schemas import _ISSUE, object_schema
from ..evidence_audit import PENALTIES, bind_evidence

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

# Shared by every checker so system + draft form one cacheable prefix; the
# checker's own focus and context follow in the last message.
CHECKER_SYSTEM = """你是长篇小说一致性审计组的成员。每次只负责最后一条消息指定的类别，不检查未指定的类别，不因个人文风偏好报错。
人物没有表现你期待的情绪、或采用不同但合理的写法，都不是错误。
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

# Per-block character caps keep every checker small.
BLOCK_CAPS = {
    "characters": 2600, "epistemic": 2400, "relationships": 1200,
    "facts": 2400, "continuity_state": 1000, "description_ledger": 1000,
    "narrative": 900, "existing_tail": 900, "route": 2600, "threads": 2200,
    "rules": 2200, "world": 1800, "hooks": 900,
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
                    "checker": {"type": "string", "enum": [item.key for item in CHECKERS]},
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
            parts.append(f"【{BLOCK_LABELS.get(key, key)}】\n{content}")
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
    focus = "\n".join(f"- {checker.key}（{checker.label}）：{checker.focus}" for checker in CHECKERS)
    parts = []
    seen: set[str] = set()
    for checker in CHECKERS:
        for key in checker.blocks:
            if key in seen:
                continue
            seen.add(key)
            content = _bounded(blocks.get(key, ""), int(BLOCK_CAPS.get(key, 1500) * MERGED_CAP_SHARE))
            if content:
                parts.append(f"【{BLOCK_LABELS.get(key, key)}】\n{content}")
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
    for checker in CHECKERS:
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
# Adaptive re-audit
#
# Five checkers per audit is affordable once, but the refinement loop audits
# the same chapter again after every revision.  When the new draft is clearly a
# revision of the last audited one, only the checkers that reported problems
# last time run again; the others carry their clean result forward.  The memory
# is process-local on purpose: after a restart every checker simply runs again.
# ---------------------------------------------------------------------------
REVISION_SIMILARITY = 0.5
_RUN_MEMORY_LIMIT = 256
_last_runs: OrderedDict[str, dict[str, Any]] = OrderedDict()


def _draft_similarity(left: str, right: str) -> float:
    from ..manuscript_quality import _similarity

    return _similarity(left, right)


# A revision that rewrites more than this share of sentences is re-audited in full.
CHANGED_SHARE_FULL_RERUN = 0.15


def _context_hash(blocks: dict[str, str]) -> str:
    canonical = "\x1f".join(f"{key}\x1e{blocks.get(key, '')}" for key in sorted(blocks))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _draft_sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"(?<=[。！？!?])|\n+", str(text or "")) if item and item.strip()]


def _grams(text: str, size: int = 3) -> set[str]:
    compact = re.sub(r"[\s，。！？、；：,.!?;:'\"“”‘’—…（）()《》【】]", "", str(text or ""))
    return {compact[index:index + size] for index in range(len(compact) - size + 1)}


def _touched_checkers(changed: list[str], blocks: dict[str, str]) -> set[str]:
    """Checkers whose authority context shares wording with the changed sentences."""
    changed_grams = _grams("".join(changed))
    touched = set()
    for checker in CHECKERS:
        context = "".join(blocks.get(key, "") for key in checker.blocks)
        if len(changed_grams & _grams(context)) >= 2:
            touched.add(checker.key)
    return touched


def _is_revision(previous: dict[str, Any] | None, draft: str, blocks: dict[str, str] | None) -> list[str] | None:
    """Changed sentences when ``draft`` is a small revision of the last audit, else ``None``."""
    if not previous:
        return None
    if hashlib.sha256(draft.encode("utf-8")).hexdigest() == previous["draft_hash"]:
        return None  # same text audited again on purpose: give a full answer
    if blocks is not None and previous.get("context_hash") != _context_hash(blocks):
        return None  # memory/world/route changed: carried verdicts are stale
    if _draft_similarity(previous["draft"], draft) < REVISION_SIMILARITY:
        return None
    old_sentences = set(_draft_sentences(previous["draft"]))
    new_sentences = _draft_sentences(draft)
    changed = [item for item in new_sentences if item not in old_sentences]
    if not new_sentences or len(changed) > CHANGED_SHARE_FULL_RERUN * len(new_sentences):
        return None
    return changed


def plan_checkers(
    audit_key: str, draft: str, blocks: dict[str, str] | None = None
) -> set[str] | None:
    """Checker keys to rerun for a revised draft, or ``None`` to run all.

    Only a small revision of the last *full* audit against unchanged context
    is re-audited partially: previously flagged checkers plus every checker
    whose context the changed sentences touch.  Anything else is a full run,
    so a new error in a previously clean category cannot be carried forward.
    """
    previous = _last_runs.get(audit_key) if audit_key else None
    if previous and previous.get("mode", "full") != "full":
        return None
    changed = _is_revision(previous, draft, blocks)
    if changed is None or previous is None:
        return None
    only = set(previous["flagged"]) | _touched_checkers(changed, blocks or {})
    return only or None


def has_previous_run(audit_key: str) -> bool:
    return bool(audit_key) and audit_key in _last_runs


def previous_mode(audit_key: str, draft: str, blocks: dict[str, str] | None = None) -> str | None:
    """Budget of the last audit when ``draft`` is a small revision of it."""
    previous = _last_runs.get(audit_key) if audit_key else None
    if _is_revision(previous, draft, blocks) is None or previous is None:
        return None
    return str(previous.get("mode", "full"))


def remember_run(
    audit_key: str,
    draft: str,
    report: list[dict[str, Any]],
    blocks: dict[str, str] | None = None,
    mode: str = "full",
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
        "flagged": flagged,
        "mode": mode,
    }
    _last_runs.move_to_end(audit_key)
    while len(_last_runs) > _RUN_MEMORY_LIMIT:
        _last_runs.popitem(last=False)


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


def _sort_and_score(issues: list[dict[str, Any]]) -> tuple[int, bool]:
    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda item: order.get(str(item.get("severity", "medium")).lower(), 1))
    score = max(0, 100 - sum(PENALTIES.get(str(item.get("severity", "medium")).lower(), 8) for item in issues))
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
    for checker in CHECKERS:
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
            from .model_errors import recoverable_model_error

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
) -> dict[str, Any]:
    messages = merged_messages(blocks, numbered_draft)
    if prepare is not None:
        messages = prepare(messages)
    labels = {checker.key: checker.label for checker in CHECKERS}
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
        from .model_errors import recoverable_model_error

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


async def run_focused_critics(
    settings: dict[str, Any],
    blocks: dict[str, str],
    numbered_draft: str,
    *,
    completion: Completion,
    prepare: Callable[[list[dict[str, str]]], list[dict[str, str]]] | None = None,
    audit_key: str = "",
    draft: str = "",
    budget: str = "full",
    escalate_on: tuple[str, ...] = ("high", "medium"),
) -> tuple[dict[str, Any], list[str]]:
    """Run the category checkers and merge into the audit format.

    ``budget="merged"`` runs one compact checker covering all five families and
    escalates to the full set when it reports an issue whose severity is in
    ``escalate_on`` (or fails).  With ``audit_key`` and ``draft`` the run is
    adaptive: a small revision of the last full audit reruns only flagged or
    touched checkers; a revision of a merged audit runs the merged checker again.
    """
    mode = "merged" if budget == "merged" else "full"
    last_mode = previous_mode(audit_key, draft, blocks) if draft else None
    only = None
    if mode == "merged" and last_mode == "full":
        # Keep the precise partial rerun of the previous full audit.
        mode = "full"
    if mode == "full" and draft:
        only = plan_checkers(audit_key, draft, blocks)
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
            only = None
        else:
            outcome = merged
    if mode == "full":
        outcome = await _run_checkers(
            {**settings, "_critic_budget": "merged→full" if escalated else "full"}, blocks, numbered_draft, completion=completion, prepare=prepare, only=only,
        )
        calls += outcome["ran"]
        warnings.extend(outcome["warnings"])
        if escalated and not merged["failures"]:
            outcome = _carry_merged(merged, outcome)
            if outcome.get("carried"):
                warnings.append(f"分类审计未覆盖的类别沿用合并审计结论 {outcome['carried']} 条")
        if outcome["ran"] and outcome["failures"] == outcome["ran"]:
            raise RuntimeError("全部分类审计调用均失败")
    issues = outcome["issues"]
    if draft:
        remember_run(audit_key, draft, outcome["report"], blocks, mode=mode)
    score, has_high = _sort_and_score(issues)
    ran, failures = outcome["ran"], outcome["failures"]
    verdict = "pass" if not has_high and score >= 85 and ran > failures else "revise"
    if ran == 0:
        # Nothing was actually checked: never report a clean pass.
        warnings.append("本轮没有任何分类审计实际运行，不能判定为通过")
    brief = "；".join(
        f"[{item['category']}] {item.get('suggestion') or item.get('message', '')}" for item in issues[:4]
    )[:160]
    budget_used = ("merged→full" if escalated else mode)
    return {
        "score": score,
        "verdict": verdict,
        "issues": issues,
        "strengths": [],
        "revision_brief": brief,
        "critic_mode": "focused",
        "critic_budget": budget_used,
        "critic_calls": calls,
        "checkers": merged_report + outcome["report"],
        "adaptive_rerun": sorted(only) if only is not None else None,
        "hook_status": outcome["hook_status"],
    }, warnings

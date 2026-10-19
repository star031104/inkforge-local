"""Defaults and lookup for the prose-quality (P1) knobs.

All knobs live in ``project.settings`` so the manual workspace, the director
and the audit router read one source.  A director task ``config`` may
override any of them for that run.

Keys (settings):
    antislop_enabled        bool  – detect and locally rewrite AI clichés (default True)
    antislop_max_rewrites   int   – max sentences rewritten per draft (default 6)
    antislop_logit_bias     bool  – on llama.cpp, also bias cliché tokens down (default True)
    candidate_count         int   – 1-3 drafts for key chapters, best one selected (default 1)
    candidate_scope         str   – "key" (openings/climaxes/plan-flagged) or "all"
    dialogue_rehearsal      str   – "auto" (director only), "always" or "off"
    critic_mode             str   – "focused" (category checkers) or "single"
    rolling_replan          bool  – re-plan the next chapters after each settlement (default True)
    rolling_replan_window   int   – how many following chapters to re-plan (default 3)
    critic_budget           str   – "auto" (risk-gated), "full" (5 checkers) or "light" (1 merged)
    critic_risk_threshold   int   – auto mode: risk score at/above which all 5 checkers run (default 40)
    hook_ledger             bool  – track planned ending hooks and hook debts (default True)
    hook_window             int   – chapters a hook may wait before it becomes a debt (default 2)
    experience_bank         bool  – inject cross-book audit lessons into writing (default True)
Keys (governance):
    banned_phrases          list  – author-defined phrases that must never appear
"""
from __future__ import annotations

from typing import Any

CRAFT_DEFAULTS: dict[str, Any] = {
    "antislop_enabled": True,
    "antislop_max_rewrites": 6,
    "antislop_logit_bias": True,
    "candidate_count": 1,
    "candidate_scope": "key",
    "dialogue_rehearsal": "auto",
    "critic_mode": "focused",
    "rolling_replan": True,
    "rolling_replan_window": 3,
    "critic_budget": "auto",
    "critic_risk_threshold": 40,
    "hook_ledger": True,
    "hook_window": 2,
    "experience_bank": True,
}


def craft_setting(
    project: dict[str, Any] | None, key: str, config: dict[str, Any] | None = None
) -> Any:
    if config and key in config and config[key] is not None:
        value = config[key]
    else:
        settings = (project or {}).get("settings", {}) if isinstance(project, dict) else {}
        value = settings.get(key, CRAFT_DEFAULTS.get(key)) if isinstance(settings, dict) else CRAFT_DEFAULTS.get(key)
    default = CRAFT_DEFAULTS.get(key)
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "off", "no", ""}
        return bool(value)
    if isinstance(default, int):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        if key == "candidate_count":
            return max(1, min(3, number))
        if key == "rolling_replan_window":
            return max(1, min(5, number))
        if key == "antislop_max_rewrites":
            return max(0, min(12, number))
        if key == "critic_risk_threshold":
            return max(0, min(100, number))
        if key == "hook_window":
            return max(1, min(5, number))
        return number
    if key == "candidate_scope":
        return "all" if str(value).strip().lower() == "all" else "key"
    if key == "dialogue_rehearsal":
        text = str(value or "").strip().lower()
        return text if text in {"auto", "always", "off"} else "auto"
    if key == "critic_budget":
        text = str(value or "").strip().lower()
        return text if text in {"auto", "full", "light"} else "auto"
    if key == "critic_mode":
        return "single" if str(value or "").strip().lower() == "single" else "focused"
    return value

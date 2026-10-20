"""Permissive JSON Schemas for constrained decoding.

When the endpoint supports it (llama.cpp ``json_schema``/GBNF, vLLM
``response_format: json_schema``) these schemas make the server emit valid
JSON of the right top-level shape, which removes most truncation-repair work
for small local models.  They are intentionally loose — required top-level
keys and container types only — so they never fight the prompt's richer
instructions; domain validators still run afterwards.
"""
from __future__ import annotations

from typing import Any

_STRING = {"type": "string"}
_STRINGS = {"type": "array", "items": {"type": "string"}}
_OBJECTS = {"type": "array", "items": {"type": "object"}}
_ISSUE = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "category": _STRING,
        "message": _STRING,
        "suggestion": _STRING,
        "evidence_ids": _STRINGS,
        "evidence": _STRINGS,
        "authority_evidence": _STRING,
        "reason": _STRING,
    },
    "required": ["severity", "message"],
}


def object_schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required if required is not None else properties.keys()),
    }


AUDIT_SCHEMA = object_schema(
    {
        "score": {"type": "integer", "minimum": 0, "maximum": 100},
        "verdict": {"type": "string", "enum": ["pass", "revise"]},
        "issues": {"type": "array", "items": _ISSUE, "maxItems": 4},
        "strengths": _STRINGS,
        "revision_brief": _STRING,
    }
)

CHECKER_SCHEMA = object_schema({"issues": {"type": "array", "items": _ISSUE, "maxItems": 3}})

PAIRWISE_SCHEMA = object_schema(
    {
        "winner": {"type": "string", "enum": ["A", "B"]},
        "scores": {"type": "object"},
        "reasons": _STRINGS,
    },
    ["winner", "reasons"],
)

REWRITE_SCHEMA = object_schema(
    {"rewrites": {"type": "array", "items": object_schema({"id": _STRING, "text": _STRING})}}
)

REHEARSAL_SCHEMA = object_schema(
    {"intent": _STRING, "lines": _STRINGS, "actions": _STRINGS, "withholds": _STRING},
    ["intent", "lines"],
)

REPLAN_SCHEMA = object_schema(
    {
        "adjustments": {
            "type": "array",
            "items": object_schema(
                {"number": {"type": "integer"}, "changes": {"type": "object"}, "reason": _STRING},
                ["number", "changes"],
            ),
        }
    }
)


CHAPTER_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": _STRING,
        "conflict": _STRING,
        "must_keep": _STRINGS,
        "must_avoid": _STRINGS,
        "turning_point": _STRING,
        "ending_hook": _STRING,
        # Optional: only debate / court-council chapters fill it.
        "debate": {
            "type": "object",
            "properties": {
                "topic": _STRING,
                "venue_and_judge": _STRING,
                "stakes": _STRING,
                "opponent_strongest_arguments": {"type": "array", "items": _STRING, "maxItems": 3},
                "protagonist_arguments": _STRING,
                "protagonist_weak_points": _STRING,
                "opponent_counterattack": _STRING,
                "outcome_and_cost": _STRING,
            },
        },
    },
    "required": ["goal", "conflict", "must_keep", "must_avoid", "turning_point", "ending_hook"],
}


def memory_stage_schema(fields: tuple[str, ...]) -> dict[str, Any]:
    """Schema for one staged-extraction call (see ``memory_extraction``)."""
    properties: dict[str, Any] = {}
    for field in fields:
        if field in {"summary", "story_so_far"}:
            properties[field] = _STRING
        elif field == "scene_settlement":
            properties[field] = {"type": "object"}
        elif field == "continuity_notes":
            properties[field] = _STRINGS
        else:
            properties[field] = _OBJECTS
    return object_schema(properties)

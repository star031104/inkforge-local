from __future__ import annotations

import json
from pathlib import Path

from app.evaluation import evaluate_prompt_suite
from app.prompts import PROSE_PROMPT_VERSION


def test_versioned_prompt_regression_suite():
    path = Path(__file__).parent / "fixtures" / "prompt_cases.json"
    report = evaluate_prompt_suite(json.loads(path.read_text(encoding="utf-8")))
    assert report["prompt_version"] == PROSE_PROMPT_VERSION
    assert report["passed"] == report["total"]

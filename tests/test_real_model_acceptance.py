from __future__ import annotations

from scripts.acceptance_real_model import acceptance_settings, manuscript_checks, redact
from scripts.check_release_acceptance import validate_report


def test_acceptance_report_redacts_nested_credentials():
    source = {
        "api_key": "primary-secret",
        "nested": {"reasoning_api_key": "secondary-secret", "model": "safe-model"},
        "items": [{"authorization": "Bearer secret"}],
    }
    cleaned = redact(source)
    assert cleaned["api_key"] == "[redacted]"
    assert cleaned["nested"]["reasoning_api_key"] == "[redacted]"
    assert cleaned["items"][0]["authorization"] == "[redacted]"
    assert cleaned["nested"]["model"] == "safe-model"


def test_dual_acceptance_settings_use_two_explicit_slots(monkeypatch):
    monkeypatch.setenv("INKFORGE_ACCEPT_PRIMARY_PROVIDER", "zhipu")
    monkeypatch.setenv("INKFORGE_ACCEPT_PRIMARY_KEY", "first")
    monkeypatch.setenv("INKFORGE_ACCEPT_SECONDARY_PROVIDER", "modelscope")
    monkeypatch.setenv("INKFORGE_ACCEPT_SECONDARY_KEY", "second")
    settings = acceptance_settings("dual")
    assert settings["model_routing"] == "dual"
    assert settings["provider"] == "zhipu"
    assert settings["reasoning_provider"] == "modelscope"
    assert settings["api_key"] == "first"
    assert settings["reasoning_api_key"] == "second"


def test_manuscript_acceptance_rejects_duplicate_or_unlocked_chapters():
    project = {
        "chapters": [
            {"content": "有效正文" * 40, "authority_state": "locked"},
            {"content": "有效正文" * 40, "authority_state": "draft"},
        ],
        "memory": {"commits": []},
    }
    result = manuscript_checks(project, 2)
    assert result["all_expected_chapters_written"] is True
    assert result["no_duplicate_full_chapters"] is False
    assert result["all_completed_chapters_locked"] is False
    assert result["memory_settled_for_expected_chapters"] is False
    assert result["all_expected_chapters_summarized"] is False
    assert result["passed"] is False


def test_release_gate_requires_both_routes_ten_chapters_and_restart():
    valid_case = {
        "passed": True,
        "checks": {
            "passed": True,
            "completed_chapters": 10,
            "restart_recovery_verified": True,
        },
    }
    report = {
        "cases": [
            {"routing": "single", **valid_case},
            {"routing": "dual", **valid_case},
        ]
    }
    assert validate_report(report) == []
    report["cases"][1]["checks"] = {
        **valid_case["checks"],
        "completed_chapters": 9,
    }
    assert "dual 未完成至少 10 章" in validate_report(report)

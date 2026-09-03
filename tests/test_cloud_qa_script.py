from __future__ import annotations

import runpy
from pathlib import Path


MODULE = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "cloud_full_e2e.py"))


def test_cloud_full_qa_settings_never_embed_api_key():
    cfg = MODULE["cloud_settings"]("https://api.siliconflow.cn/v1")
    assert cfg["provider"] == "siliconflow"
    assert cfg["model"] == "Qwen/Qwen3-8B"
    assert cfg["enable_thinking"] is False
    assert cfg["api_key"] == ""


def test_cloud_full_qa_scrubber_removes_secret_recursively():
    secret = "sk-test-secret"
    payload = {
        "text": f"Bearer {secret}",
        "nested": [{"api_key": secret}, f"prefix-{secret}-suffix"],
    }
    clean = MODULE["scrub"](payload, secret)
    assert secret not in str(clean)
    assert clean["nested"][0]["api_key"] == "***REDACTED***"


def test_cloud_full_qa_report_is_human_readable():
    report = {
        "started_at": "start",
        "finished_at": "finish",
        "model": "Qwen/Qwen3-8B",
        "base_url": "https://api.siliconflow.cn/v1",
        "overall": "PASS",
        "steps": [{"status": "PASS", "name": "生成", "detail": "900字"}],
    }
    text = MODULE["markdown_report"](report)
    assert "Cloud Full E2E" in text
    assert "Qwen/Qwen3-8B" in text
    assert "900字" in text


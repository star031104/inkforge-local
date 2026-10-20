"""v0.34.0 fixes: legacy experience-bank counts, legacy hook evaluations
and a per-run director critic budget."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.api import container
from app.storage.project_store import ProjectStore
from app.storage.extension_store import ExtensionStore
from app.director.service import _craft_overrides
from app.review.experience_bank import (
    CHAPTER_WINDOW,
    experience_hints,
    merge_experience_delta,
    record_audit_issues,
)
from app.story.hook_ledger import due_hooks, migrate_ledger, outstanding_debts, record_chapter_hook
from tests.test_p1_craft import project_fixture

ROOT = Path(__file__).resolve().parents[1]
VERIFIED = {
    "category": "人物记忆与能力/知情越界", "checker": "character", "severity": "high",
    "message": "林默说出了只有沈青知道的暗号", "evidence_verified": True,
}


# ------------------------------------------------------------ 1. legacy bank counts

def _legacy_bank(count: int) -> dict:
    return {"m": {"chapters": ["old1", "old2"], "rounds": 5, "patterns": {"knowledge_leak": {
        "kind": "knowledge_leak", "category": "人物记忆与能力/知情越界", "checker": "character",
        "weight": 3.0, "count": count, "last_chapter": "old2", "examples": [],
    }}}}


def test_legacy_pattern_count_survives_a_new_audit():
    project = {"memory": {"experience_bank": _legacy_bank(4)}}
    assert len(experience_hints(project, "m")) == 1  # count 4 already makes a hint
    record_audit_issues(project, "m", [VERIFIED], chapter_id="c9")
    item = project["memory"]["experience_bank"]["m"]["patterns"]["knowledge_leak"]
    assert item["chapter_ids"] == ["#legacy1", "#legacy2", "#legacy3", "#legacy4", "c9"]
    assert item["count"] == 5
    assert len(experience_hints(project, "m")) == 1


def test_legacy_backfill_is_capped_at_the_window_and_idempotent():
    project = {"memory": {"experience_bank": _legacy_bank(50)}}
    record_audit_issues(project, "m", [VERIFIED], chapter_id="c9")
    item = project["memory"]["experience_bank"]["m"]["patterns"]["knowledge_leak"]
    assert item["count"] == CHAPTER_WINDOW and item["chapter_ids"][-1] == "c9"
    record_audit_issues(project, "m", [VERIFIED], chapter_id="c9")  # same chapter again
    assert project["memory"]["experience_bank"]["m"]["patterns"]["knowledge_leak"]["count"] == CHAPTER_WINDOW


def test_merging_a_delta_into_a_legacy_copy_keeps_its_count():
    server = {"memory": {"experience_bank": _legacy_bank(3)}}
    client = deepcopy(server["memory"]["experience_bank"])  # browser copy, still legacy
    delta = record_audit_issues(server, "m", [VERIFIED], chapter_id="c9")
    merge_experience_delta(client, delta)
    item = client["m"]["patterns"]["knowledge_leak"]
    assert item["count"] == 4
    assert item["chapter_ids"] == ["#legacy1", "#legacy2", "#legacy3", "c9"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_merge_backfills_legacy_patterns_like_python():
    server = {"memory": {"experience_bank": _legacy_bank(3)}}
    snapshot = deepcopy(server["memory"]["experience_bank"])
    delta = record_audit_issues(server, "m", [VERIFIED], chapter_id="c9")
    expected = merge_experience_delta(deepcopy(snapshot), delta)
    source = (ROOT / "static/js/features/review/editorial.js").read_text(encoding="utf-8")
    script = (
        source[source.index("const EXPERIENCE_WINDOW"):source.index("async function auditDraft(){")]
        + f"\nconsole.log(JSON.stringify(mergeExperienceDelta({json.dumps(snapshot)},{json.dumps(delta)})));"
    )
    got = json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert got["m"]["patterns"] == expected["m"]["patterns"]


# ------------------------------------------------------------ 2. legacy hook evaluations

def _legacy_record(chapter_id: str, origin: int, evaluations: dict) -> dict:
    return {"id": f"H{chapter_id}", "chapter_id": chapter_id, "origin_chapter": origin,
            "text": "神秘人留下血字暗号", "window": 1, "evaluations": dict(evaluations)}


def test_legacy_number_keys_shift_after_an_insert(tmp_path):
    project = project_fixture(tmp_path, 4)
    origin_id = project["chapters"][0]["id"]
    project["memory"]["hook_ledger"] = [_legacy_record(origin_id, 1, {"2": "ignored", "3": "ignored"})]
    project["chapters"].insert(0, {"id": "c0", "title": "楔子", "content": "", "plan": {}})
    # Unmigrated, key "2" would now point at the hook's own chapter and be lost.
    debts = outstanding_debts(project, 5)
    record = project["memory"]["hook_ledger"][0]
    assert record["evaluations"] == {"c2": "ignored", "c3": "ignored"}
    assert record["origin_chapter"] == 2
    assert [(item["id"], item["origin_chapter"], item["status"]) for item in debts] == [(f"H{origin_id}", 2, "debt")]
    assert migrate_ledger(project) == 0  # second touch is a no-op


def test_legacy_number_keys_shift_after_a_delete_and_drop_out_of_range(tmp_path):
    project = project_fixture(tmp_path, 4)
    project["memory"]["hook_ledger"] = [_legacy_record("c2", 2, {"3": "advanced", "4": "ignored", "9": "paid"})]
    project["chapters"] = project["chapters"][1:]
    due_hooks(project, 4)
    record = project["memory"]["hook_ledger"][0]
    assert record["evaluations"] == {"c3": "advanced", "c4": "ignored"}
    assert record["origin_chapter"] == 1


def test_existing_id_keys_win_and_record_hook_migrates_first(tmp_path):
    project = project_fixture(tmp_path, 4)
    chapter = project["chapters"][0]
    chapter["route"] = {"ending_hook": "神秘人留下血字暗号"}
    project["memory"]["hook_ledger"] = [
        dict(_legacy_record(chapter["id"], 1, {"2": "ignored", "c2": "paid"}), id="Hx"),
    ]
    project["chapters"].insert(0, {"id": "c0", "title": "楔子", "content": "", "plan": {}})
    record_chapter_hook(project, chapter, 2, window=1)  # re-accepting refreshes the cache
    record = project["memory"]["hook_ledger"][0]
    # "2" (shifted by the insert → position 3 = c2) loses to the explicit c2 key.
    assert record["evaluations"] == {"c2": "paid"} and record["origin_chapter"] == 2


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "api.db")
    monkeypatch.setattr(container, "store", store)
    monkeypatch.setattr(container, "_launch_director", lambda _task_id: None)
    with TestClient(main.app) as test_client:
        yield test_client, store, monkeypatch


# ------------------------------------------------------------ 4. director critic budget

def _start_payload(store, **extra) -> dict:
    source = store.create("导演源")
    return {"source_project": source, "seed": "一个关于雨夜旧书店的悬疑故事", "target_chapters": 3, **extra}


def test_director_start_passes_critic_budget_into_task_config(client):
    test_client, store, _monkeypatch = client
    response = test_client.post("/api/director/start", json=_start_payload(store, critic_budget="light"))
    assert response.status_code == 200, response.text
    task = response.json()["task"]
    assert task["config"]["critic_budget"] == "light"
    assert _craft_overrides(task) == {"critic_budget": "light"}


def test_director_start_defaults_to_project_setting_and_rejects_bad_budget(client):
    test_client, store, _monkeypatch = client
    task = test_client.post("/api/director/start", json=_start_payload(store)).json()["task"]
    assert "critic_budget" not in task["config"] and _craft_overrides(task) == {}
    bad = test_client.post("/api/director/start", json=_start_payload(store, critic_budget="bogus"))
    assert bad.status_code == 422

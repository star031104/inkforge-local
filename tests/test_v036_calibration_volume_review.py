"""v0.36: audit score calibration and whole-volume review."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.review.calibration import (
    MIN_SAMPLES,
    audited_chapters,
    calibration_stats,
    predicted_score,
    upsert_sample,
)
from app.storage.schema.calibration import ensure_calibration_defaults, normalize_sample
from app.storage.project_store import ProjectStore
from app.storage.schema.project import ensure_project_defaults
from app.review.editorial_workflow import rebuild_repair_queue
from app.story.evidence import (
    PENALTIES,
    draft_hash,
    normalize_audit_weights,
    penalty_total,
    stable_audit_result,
)
from app.review import focused_critics
from app.writing.craft_settings import craft_setting
from app.review.volume_review import (
    SOURCE,
    VOLUME_REVIEW_PROMPT,
    build_messages,
    enqueue_review_tasks,
    is_stale,
    make_validator,
    normalize_report,
    run_volume_review,
)
from tests.test_round3_runtime_director import BODY, _install_fakes, _start, _three_chapter_project

ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(monkeypatch, tmp_path):
    store = ProjectStore(tmp_path / "v036.db")
    monkeypatch.setattr(main, "store", store)
    monkeypatch.setattr(main, "_launch_director", lambda _task_id: None)
    return store


# ---------------------------------------------------------------------------
# penalty_total weights
# ---------------------------------------------------------------------------

ISSUES = [
    {"severity": "high", "category": "人物"},
    {"severity": "medium", "category": "人物"},
    {"severity": "medium", "category": "节奏"},
    {"severity": "low", "category": "文风"},
    {"severity": "low", "category": "文风"},
]


def test_penalty_total_defaults_unchanged_and_weights_apply():
    default = penalty_total(ISSUES)
    assert default == 18 + 4 + 8 + round(3 + 1.5)
    assert penalty_total(ISSUES, None) == default
    assert penalty_total(ISSUES, {}) == default
    assert penalty_total(ISSUES, {"garbage": 1}) == default
    halved = penalty_total(ISSUES, {"category_multipliers": {"人物": 0.5}})
    assert halved == round(9 + 2 + 8 + 4.5)
    heavier = penalty_total(ISSUES, {"penalties": {"medium": 12}})
    assert heavier == round(18 + 6 + 12 + 4.5)
    # Out-of-range values are clamped, not trusted.
    assert normalize_audit_weights({"penalties": {"high": 999, "bogus": 3}, "category_multipliers": {"x": -2}}) == {
        "penalties": {"high": 40.0}, "category_multipliers": {"x": 0.0},
    }


def test_stable_audit_result_uses_weights():
    text = "第一句话在这里。第二句话在这里。第三句话在这里。" * 5
    ai = {"score": 100, "score_normalized": True, "issues": [
        {"severity": "high", "category": "人物", "message": "x", "evidence_ids": ["P1-S1"]},
    ]}
    local = {"score": 100, "verdict": "pass", "issues": []}
    base = stable_audit_result(text, ai, local)
    assert base["score"] == 100 - PENALTIES["high"]
    assert base["score_components"]["calibrated_weights"] is False
    weighted = stable_audit_result(text, ai, local, weights={"category_multipliers": {"人物": 0.5}})
    assert weighted["score"] == 100 - 9
    assert weighted["score_components"]["calibrated_weights"] is True


def test_focused_critics_score_with_settings_weights():
    focused_critics._last_runs.clear()

    async def completion(settings, messages, **kwargs):
        return {"issues": [{"severity": "high", "message": "称谓不一", "suggestion": "统一", "evidence_ids": ["P1-S1"]}]}, []

    blocks = {key: "上下文" for key in focused_critics.BLOCK_LABELS}
    plain, _ = run(focused_critics.run_focused_critics({}, blocks, "[P1-S1]甲。", completion=completion, budget="merged"))
    weighted, _ = run(focused_critics.run_focused_critics(
        {"audit_weights": {"penalties": {"high": 10}}}, blocks, "[P1-S1]甲。", completion=completion, budget="merged",
    ))
    focused_critics._last_runs.clear()
    assert plain["issues"] and plain["score"] == 100 - penalty_total(plain["issues"])
    assert weighted["score"] > plain["score"]
    assert weighted["score"] == 100 - penalty_total(weighted["issues"], {"penalties": {"high": 10}})


def test_audit_route_threads_project_weights():
    source = (ROOT / "app/api/routers/editorial_audit.py").read_text(encoding="utf-8")
    assert "weights=audit_weights" in source
    assert "penalty_total(raw_issues, audit_weights)" in source


# ---------------------------------------------------------------------------
# Calibration statistics
# ---------------------------------------------------------------------------


def _sample(index: int, human: int, issues: list[dict]) -> dict:
    ai = max(0, 100 - penalty_total(issues))
    return {"chapter_id": f"c{index}", "draft_hash": f"h{index}", "human_score": human, "ai_score": ai, "issues": issues}


def _over_penalised_samples(count: int = 12) -> list[dict]:
    """The AI over-penalises the 人物 category: the author scores ignore it."""
    samples = []
    for index in range(count):
        issues = [{"issue_id": f"i{index}a", "category": "人物", "severity": "high",
                   "verdict": "disagree" if index % 2 == 0 else ""}]
        if index % 3 == 0:
            issues.append({"issue_id": f"i{index}b", "category": "节奏", "severity": "medium", "verdict": "agree"})
        human = 100 - (8 if index % 3 == 0 else 0) - 3
        samples.append(_sample(index, human, issues))
    return samples


def test_stats_metrics_and_false_positive_rate():
    samples = _over_penalised_samples()
    stats = calibration_stats(samples)
    assert stats["scored_count"] == 12
    assert stats["mae"] > 10 and stats["bias"] < 0
    assert stats["pearson"] is not None and stats["spearman"] is not None
    people = next(row for row in stats["categories"] if row["category"] == "人物")
    assert people["judged"] == 6 and people["disagree"] == 6 and people["false_positive_rate"] == 1.0
    assert stats["top_false_positive"][0]["category"] == "人物"


def test_suggestion_requires_minimum_samples():
    stats = calibration_stats(_over_penalised_samples(MIN_SAMPLES - 1))
    assert stats["suggestion"] is None
    assert str(MIN_SAMPLES) in stats["suggestion_note"]


def test_suggested_weights_reduce_mae_and_shrink_toward_default():
    samples = _over_penalised_samples(12)
    stats = calibration_stats(samples)
    suggestion = stats["suggestion"]
    assert suggestion is not None
    assert suggestion["mae_suggested"] < suggestion["mae_current"]
    weights = suggestion["weights"]
    multiplier = weights.get("category_multipliers", {}).get("人物", 1.0)
    high = weights.get("penalties", {}).get("high", 18)
    # The 人物 high finding must weigh less, but shrinkage keeps it above zero.
    assert multiplier * high < 18
    assert multiplier > 0.25 or high > 8
    assert suggestion["diff"]
    # Applying the suggestion actually moves the predictions toward the author.
    clean = [normalize_sample(item) for item in samples]
    before = sum(abs(item["ai_score"] - item["human_score"]) for item in clean)
    after = sum(abs(predicted_score(item, weights) - item["human_score"]) for item in clean)
    assert after < before


def test_consistent_samples_suggest_no_change():
    samples = []
    for index in range(10):
        issues = [{"category": "节奏", "severity": "medium"}] if index % 2 else []
        sample = _sample(index, 0, issues)
        sample["human_score"] = sample["ai_score"]
        samples.append(sample)
    stats = calibration_stats(samples)
    assert stats["mae"] == 0
    assert stats["suggestion"]["diff"] == []


def test_sample_upsert_and_defaults():
    project = ensure_project_defaults({"id": "p", "chapters": [{"id": "c1", "content": "正文"}]})
    assert project["calibration"] == {"samples": []}
    assert project["volume_reviews"] == {}
    upsert_sample(project, {"chapter_id": "c1", "draft_hash": "h", "human_score": 70, "ai_score": 80})
    upsert_sample(project, {"chapter_id": "c1", "draft_hash": "h", "human_score": 75, "ai_score": 80})
    upsert_sample(project, {"chapter_id": "c1", "draft_hash": "h2", "human_score": 60, "ai_score": 80})
    assert [item["human_score"] for item in project["calibration"]["samples"]] == [75, 60]
    project["calibration"]["samples"].append({"bad": True})
    ensure_calibration_defaults(project)
    assert len(project["calibration"]["samples"]) == 2


def test_audited_chapters_matches_card_verdicts_by_text():
    content = "雨夜正文。" * 40
    project = ensure_project_defaults({"id": "p", "chapters": [{
        "id": "c1", "title": "一", "content": content,
        "execution": {"audit_score": 82, "issues": [
            {"source": "AI", "category": "人物", "severity": "high", "message": "称谓不一"},
            {"source": "本地", "category": "重复", "severity": "low", "message": "本地"},
        ]},
    }]})
    project["calibration"]["samples"].append({
        "chapter_id": "c1", "draft_hash": draft_hash(content), "ai_score": 82, "human_score": None,
        "issues": [{"issue_id": "cabc", "category": "人物", "severity": "high", "message": "称谓不一", "verdict": "disagree"}],
    })
    rows = audited_chapters(project)
    assert len(rows) == 1 and rows[0]["ai_score"] == 82
    assert [item["category"] for item in rows[0]["issues"]] == ["人物"]
    assert rows[0]["issues"][0]["verdict"] == "disagree"


def _audited_project(store: ProjectStore, count: int = 9) -> dict:
    project = store.create("校准")
    project["chapters"] = [{
        "id": f"c{n}", "title": f"第{n}章", "content": f"第{n}章正文。" * 30,
        "execution": {"audit_score": 82, "issues": [
            {"source": "AI", "issue_id": f"I{n}", "category": "人物", "severity": "high", "message": f"问题{n}"},
        ]},
    } for n in range(1, count + 1)]
    return store.save(project["id"], project, reason="fixture")


def test_calibration_endpoints_record_and_apply(store):
    project = _audited_project(store)
    with TestClient(main.app) as client:
        overview = client.get(f"/api/projects/{project['id']}/calibration").json()
        assert len(overview["candidates"]) == 9 and overview["weights"] == {}
        bad = client.post(f"/api/projects/{project['id']}/calibration/samples", json={"chapter_id": "c1"})
        assert bad.status_code == 400
        for n in range(1, 10):
            response = client.post(f"/api/projects/{project['id']}/calibration/samples", json={
                "chapter_id": f"c{n}", "human_score": 97,
                "issues": [{"issue_id": f"I{n}", "verdict": "disagree"}],
            })
            assert response.status_code == 200, response.text
        result = response.json()
        assert result["stats"]["scored_count"] == 9
        assert result["stats"]["suggestion"]["diff"]
        assert result["project"]["calibration"]["samples"][0]["issues"][0]["verdict"] == "disagree"
        applied = client.post(f"/api/projects/{project['id']}/calibration/apply", json={"mode": "suggested"})
        assert applied.status_code == 200, applied.text
        weights = applied.json()["project"]["settings"]["audit_weights"]
        assert weights and normalize_audit_weights(weights) == weights
        assert store.get(project["id"])["settings"]["audit_weights"] == weights
        everywhere = client.get("/api/calibration/stats").json()
        assert everywhere["scored_count"] >= 9
        reset = client.post(f"/api/projects/{project['id']}/calibration/apply", json={"mode": "default"})
        assert "audit_weights" not in reset.json()["project"]["settings"]


# ---------------------------------------------------------------------------
# Whole-volume review
# ---------------------------------------------------------------------------

REPORT = {
    "summary": "本卷推进清楚，中段略拖。",
    "pacing": [
        {"chapter": 1, "pace": "合适", "note": "开局清楚"},
        {"chapter": 2, "pace": "拖沓", "note": "重复核对纸纹"},
        {"chapter": 3, "pace": "过快", "note": "卷末决定来得突然"},
        {"chapter": 9, "pace": "合适", "note": "越界章号会被丢弃"},
    ],
    "planned": [{"kind": "turning_point", "item": "确认旧信来自失踪者", "status": "missing", "chapters": [], "note": "未出现证据"}],
    "unpaid_foreshadowing": [{"chapter": 1, "item": "潮汐表背面的编号", "note": "下一卷回收"}],
    "repetitions": [{"chapters": [1, 2], "pattern": "反复核对纸纹墨迹"}],
    "character_drift": [{"character": "许澄", "chapters": [3], "issue": "突然冒进"}],
    "repair_tasks": [
        {"chapter": 2, "category": "节奏", "priority": "medium", "instruction": "删去第二次核对纸纹的段落，改为与码头看守的交锋", "reason": "重复"},
        {"chapter": 3, "category": "规划兑现", "priority": "high", "instruction": "在卷末加入旧信落款与失踪者笔迹一致的证据", "reason": "转折未兑现"},
        {"chapter": 7, "category": "其他", "priority": "low", "instruction": "越界", "reason": ""},
    ],
}


def _volume_project(store: ProjectStore) -> dict:
    project = _three_chapter_project(store)
    project["chapters"] = [
        {"id": f"ch-{n}", "title": f"第{n}章标题", "content": f"第{n}章。" + BODY, "summary": f"第{n}章摘要"}
        for n in (1, 2, 3)
    ]
    return store.save(project["id"], project, reason="texts")


def test_normalize_report_and_validator():
    clean = normalize_report(REPORT, [1, 2, 3])
    assert [item["chapter"] for item in clean["pacing"]] == [1, 2, 3]
    assert [task["chapter"] for task in clean["repair_tasks"]] == [3, 2]  # high first, out-of-range dropped
    validate = make_validator([1, 2, 3])
    validate(REPORT)
    with pytest.raises(ValueError):
        validate({"pacing": [{"chapter": 1, "pace": "合适"}], "repair_tasks": []})
    with pytest.raises(ValueError):
        validate({"pacing": []})


def test_build_messages_full_text_for_cloud_and_excerpts_for_local(store):
    project = _volume_project(store)
    volume = project["planning"]["volumes"][0]
    rows = [(n, chapter) for n, chapter in enumerate(project["chapters"], start=1)]
    cloud, meta = build_messages(project, volume, rows, {"context_budget": 1_000_000}, local=False, max_tokens=8000)
    assert meta["mode"] == "full_text" and BODY in cloud[1]["content"]
    assert cloud[0]["content"] == VOLUME_REVIEW_PROMPT and "确认旧信来自失踪者" in cloud[1]["content"]
    local, meta = build_messages(project, volume, rows, {"context_budget": 8000}, local=True, max_tokens=3200)
    assert meta["mode"] == "summary" and BODY not in local[1]["content"]
    assert "第2章摘要" in local[1]["content"] and "开头：" in local[1]["content"]
    for _number, chapter in rows:
        chapter["content"] = BODY * 4
    tight, meta = build_messages(project, volume, rows, {"context_budget": 12000}, local=False, max_tokens=8000)
    assert meta["mode"] == "clipped_text" and "中段省略" in tight[1]["content"]


def test_run_volume_review_queues_tasks_dedupes_and_marks_stale(store):
    project = _volume_project(store)
    calls = []

    async def completion(settings, messages, **kwargs):
        calls.append(kwargs)
        kwargs["validate"](REPORT)
        return REPORT, []

    entry = run(run_volume_review(project, "volume-1", completion=completion))
    assert calls[0]["schema"]["required"]
    statuses = [task["status"] for task in entry["report"]["repair_tasks"]]
    assert statuses == ["queued", "queued"]
    queued = [item for item in project["repair_queue"] if item["source"] == SOURCE]
    assert len(queued) == 2 and {item["chapter_id"] for item in queued} == {
        project["chapters"][1]["id"], project["chapters"][2]["id"],
    }
    assert not is_stale(project, "volume-1")
    # Running again finds the open tasks and does not duplicate them.
    again = run(run_volume_review(project, "volume-1", completion=completion))
    assert [task["status"] for task in again["report"]["repair_tasks"]] == ["duplicate", "duplicate"]
    assert len([item for item in project["repair_queue"] if item["source"] == SOURCE]) == 2
    # A near-identical instruction is also a duplicate; a dismissed one stays dismissed.
    near = enqueue_review_tasks(project, [{"chapter": 2, "category": "节奏", "priority": "low",
                                           "instruction": "删去第二次核对纸纹的段落，改为与码头看守交锋", "reason": ""}])
    assert near[0]["status"] == "duplicate"
    second = next(item for item in queued if item["chapter_id"] == project["chapters"][1]["id"])
    second["status"] = "dismissed"
    dismissed = enqueue_review_tasks(project, [{"chapter": 2, "category": "节奏", "priority": "low",
                                                "instruction": second["message"], "reason": ""}])
    assert dismissed[0]["status"] == "dismissed"
    # Editing a chapter makes the stored report stale.
    project["chapters"][0]["content"] += "新增一段。"
    assert is_stale(project, "volume-1")


def test_rebuild_queue_keeps_volume_review_tasks_on_locked_chapters(store):
    project = _volume_project(store)
    chapter = project["chapters"][1]
    chapter["authority_state"] = "locked"
    chapter["execution"] = {"status": "accepted", "audit_score": 90, "issues": []}
    enqueue_review_tasks(project, [{"chapter": 2, "category": "节奏", "priority": "medium",
                                    "instruction": "压缩重复段落", "reason": ""}])
    rebuild_repair_queue(project)
    item = next(item for item in project["repair_queue"] if item["source"] == SOURCE)
    assert item["status"] == "queued"


def test_volume_review_endpoints(store, monkeypatch):
    project = _volume_project(store)

    async def completion(settings, messages, **kwargs):
        return REPORT, []

    monkeypatch.setattr(main, "structured_completion", completion)
    with TestClient(main.app) as client:
        assert client.get(f"/api/projects/{project['id']}/volumes/nope/review").status_code == 404
        empty = client.get(f"/api/projects/{project['id']}/volumes/volume-1/review").json()
        assert empty["review"] is None
        response = client.post(f"/api/projects/{project['id']}/volumes/volume-1/review")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["review"]["stale"] is False
        assert len(body["review"]["repair_statuses"]) == 2
        assert body["project"]["volume_reviews"]["volume-1"]["report"]["summary"]
        latest = client.get(f"/api/projects/{project['id']}/volumes/volume-1/review").json()
        assert latest["review"]["report"]["pacing"][1]["pace"] == "拖沓"
    saved = store.get(project["id"])
    assert len([item for item in saved["repair_queue"] if item["source"] == SOURCE]) == 2


def test_volume_review_setting_default_on():
    assert craft_setting({"settings": {}}, "volume_review") is True
    assert craft_setting({"settings": {"volume_review": False}}, "volume_review") is False
    assert craft_setting({"settings": {}}, "volume_review", {"volume_review": "off"}) is False


def _director_structured(report_or_error):
    seen = []

    async def structured(settings, messages, **kwargs):
        if messages and messages[0]["content"] == VOLUME_REVIEW_PROMPT:
            seen.append(messages)
            if isinstance(report_or_error, Exception):
                raise report_or_error
            return report_or_error, []
        return {"rewrites": []}, []

    return structured, seen


def _run_director(monkeypatch, store, structured, **settings):
    project = _three_chapter_project(store)
    project["settings"].update(rolling_replan=False, dialogue_rehearsal="off", **settings)
    project = store.save(project["id"], project, reason="settings")
    _install_fakes(monkeypatch, lambda: "灯芯爆了一下。" + BODY + "她推门走进雨里。", structured)
    task_id = _start(store, project)
    asyncio.run(main._run_auto_director(task_id))
    return store.get_director_task(task_id), store.get(project["id"])


def test_director_reviews_volume_after_its_last_chapter(monkeypatch, store):
    structured, seen = _director_structured(REPORT)
    task, project = _run_director(monkeypatch, store, structured)
    assert task["status"] == "completed"
    assert len(seen) == 1
    assert project["volume_reviews"]["volume-1"]["report"]["pacing"]
    assert [item for item in project["repair_queue"] if item.get("source") == SOURCE]
    assert any("整卷回顾完成" in event["message"] for event in task["events"])


def test_director_volume_review_failure_only_warns(monkeypatch, store):
    structured, seen = _director_structured(RuntimeError("模型超时"))
    task, project = _run_director(monkeypatch, store, structured)
    assert task["status"] == "completed"
    assert len(seen) == 1
    assert project["volume_reviews"] == {}
    assert any(event["kind"] == "warning" and "整卷回顾未完成" in event["message"] for event in task["events"])


def test_director_volume_review_can_be_disabled(monkeypatch, store):
    structured, seen = _director_structured(REPORT)
    task, _project = _run_director(monkeypatch, store, structured, volume_review=False)
    assert task["status"] == "completed"
    assert seen == []


# ---------------------------------------------------------------------------
# Frontend wiring
# ---------------------------------------------------------------------------


def test_frontend_wiring():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    for asset in ("js/features/calibration.js", "js/features/volume-review.js", "css/calibration-review.css"):
        assert f"/assets/{asset}?v=" in html
    assert 'id="calibrationBtn"' in html
    calibration = (ROOT / "static/js/features/calibration.js").read_text(encoding="utf-8")
    assert 'Inkforge.register("calibration"' in calibration
    review = (ROOT / "static/js/features/volume-review.js").read_text(encoding="utf-8")
    assert 'Inkforge.register("volumeReview"' in review and "已加入精修队列" in review
    planning = (ROOT / "static/js/features/planning.js").read_text(encoding="utf-8")
    assert "volume-review-btn" in planning and "volumeReviewModal" in planning
    editorial = (ROOT / "static/js/features/editorial.js").read_text(encoding="utf-8")
    assert "这条审校不认同" in editorial

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.main as main
from app.api.routers.evaluation import create_evaluation_router
from app.storage.project_store import ProjectStore
from app.evaluation.longform import (
    CONSTORY_TAXONOMY,
    EvaluationHistory,
    benchmark_project,
    classify_issue,
    compare_reports,
    constory_counts,
    derive_project_probes,
    evaluate_benchmark_output,
    evaluate_project,
    load_benchmark_cases,
    pairwise_compare,
    pairwise_manuscripts,
    payoff_threads,
    report_markdown,
    run_probes,
    chapter_rows,
    state_contradictions,
    telemetry_metrics,
)

ROOT = Path(__file__).resolve().parents[1]


def _case(case_id: str) -> dict:
    return next(case for case in load_benchmark_cases() if case["id"] == case_id)


def test_benchmark_cases_are_complete_and_probes_compile():
    cases = load_benchmark_cases()
    assert {case["id"] for case in cases} == {"historical_transmigration", "mystery", "relationship"}
    for case in cases:
        assert len(case["route"]) == 10
        assert case["characters"] and case["facts"] and case["foreshadow"]
        assert all(char.get("knowledge") is not None for char in case["characters"])
        for probe in case["probes"]:
            re.compile(probe["pattern"])
            if probe.get("exclude_context"):
                re.compile(probe["exclude_context"])
        project = benchmark_project(case)
        assert len(project["chapters"]) == 10
        assert project["chapters"][0]["plan"]["beats"]


def test_taxonomy_has_nineteen_subcategories():
    assert sum(len(items) for items in CONSTORY_TAXONOMY.values()) == 19


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("人物知识/逻辑", "知情越界"),
        ("时代语汇失真", "时代语汇失真"),
        ("长度", "非一致性"),
        ("句式疲劳", "非一致性"),
        ("疑似新增往事", "未确认新设定"),
        ("待确认新设定", "未确认新设定"),
        ("情节逻辑", "因果违背"),
        ("人物状态与动机", "性格动机突变"),
        ("线索债务", "弃坑"),
        ("重复段落", "非一致性"),
    ],
)
def test_classify_real_categories(category, expected):
    assert classify_issue({"category": category, "message": ""}) == expected


def test_constory_counts_groups():
    counts = constory_counts([
        {"category": "人物知识/逻辑"},
        {"category": "情节逻辑"},
        {"category": "长度"},
    ])
    assert counts["groups"]["人物"] == 1
    assert counts["groups"]["时间线与情节"] == 1
    assert counts["continuity_total"] == 2
    assert counts["non_continuity"] == 1


def _written(case_id: str, texts: dict[int, str]) -> dict:
    project = benchmark_project(_case(case_id))
    for number, text in texts.items():
        project["chapters"][number - 1]["content"] = text
    return project


def test_benchmark_probes_detect_and_exclude():
    case = _case("historical_transmigration")
    project = _written(
        "historical_transmigration",
        {
            8: "杜延说道：“账不能再查了。”顾衡想起杜延笑着说过的话，一时无言。",
            3: "顾衡用左手提笔记下数字。",
            9: "他从怀里掏出铜印，盖在文书上。",
        },
    )
    conflicts = run_probes(chapter_rows(project), case["probes"])
    ids = sorted(item["probe_id"] for item in conflicts)
    assert ids == ["burned-hand", "dead-du-yan", "seal-destroyed"]
    dead = next(item for item in conflicts if item["probe_id"] == "dead-du-yan")
    assert dead["chapter"] == 8 and "想起" not in dead["evidence"]


def test_payoff_threads_abandoned_and_pending():
    case = _case("mystery")
    project = _written("mystery", {i: "雨下了一夜。" for i in range(1, 6)})
    project["chapters"][2]["content"] = "退潮的时间不对。"  # chapter 3 mentions the tide
    results = {item["id"]: item["status"] for item in payoff_threads(chapter_rows(project), case["foreshadow"])}
    assert results["fs-tide"] == "advanced"
    assert results["fs-container-number"] == "pending"  # due chapter 8, only 5 written
    project = _written("mystery", {i: "雨下了一夜。" for i in range(1, 10)})
    results = {item["id"]: item["status"] for item in payoff_threads(chapter_rows(project), case["foreshadow"])}
    assert results["fs-lighter"] == "abandoned"


def _real_like_project() -> dict:
    project = benchmark_project(_case("relationship"), chapters=4)
    project["chapters"][0]["content"] = "老周把铜钥匙交给苏禾。"
    project["chapters"][1]["content"] = "老周倒在雨里，再也没有醒来。"
    project["chapters"][1]["settlement"] = {"character_updates": [{"name": "老周", "state": "在雨夜身亡"}]}
    project["chapters"][2]["content"] = "第二天，老周说道：“钥匙呢？”苏禾想起老周说过的话。"
    project["chapters"][3]["content"] = "苏禾从口袋里掏出铜钥匙，打开了门。"
    project["knowledge"]["facts"] = [
        {"subject": "铜钥匙", "predicate": "状态", "object": "被抢走后丢失", "status": "confirmed",
         "source_ref": f"chapter:{project['chapters'][1]['id']}"},
        {"subject": "苏禾", "predicate": "当前地点", "object": "书店", "status": "confirmed"},
        {"subject": "苏禾", "predicate": "当前地点", "object": "面馆", "status": "confirmed"},
    ]
    return project


def test_derived_probes_from_project_memory():
    project = _real_like_project()
    probes = derive_project_probes(project)
    assert {probe["id"] for probe in probes} == {"derived-dead:老周", "derived-item:铜钥匙"}
    conflicts = run_probes(chapter_rows(project), probes)
    assert sorted(item["chapter"] for item in conflicts) == [3, 4]
    store_conflicts = state_contradictions(project)
    assert len(store_conflicts) == 1 and store_conflicts[0]["category"] == "地理位置矛盾"


def test_telemetry_metrics_rates_and_cost():
    calls = [
        {"workload": "extraction", "finish_reason": "length", "estimated_prompt_tokens": 1000, "estimated_completion_tokens": 500, "status": "completed"},
        {"workload": "extraction", "finish_reason": "json_closed", "prompt_tokens": 1000, "completion_tokens": 300, "token_source": "provider", "status": "completed"},
        {"workload": "prose", "finish_reason": "stop", "prompt_tokens": 2000, "completion_tokens": 1000, "token_source": "provider", "status": "completed"},
        {"workload": "critic", "finish_reason": "json_closed", "prompt_tokens": 4000, "completion_tokens": 500, "status": "completed"},
    ]
    metrics = telemetry_metrics(calls, chapters=2, prices={"input_per_mtok": 1.0, "output_per_mtok": 2.0})
    assert metrics["extraction_length_rate"] == 0.5
    assert metrics["critic_prose_token_ratio"] == 1.5
    assert metrics["avg_prompt_tokens"] == 2000
    assert metrics["cost"] == pytest.approx((8000 * 1 + 2300 * 2) / 1e6, abs=1e-4)
    assert metrics["cost_per_chapter"] == pytest.approx(metrics["cost"] / 2, abs=1e-4)
    assert telemetry_metrics([], chapters=1)["available"] is False


def test_evaluate_project_full_report_and_markdown():
    project = _real_like_project()
    project["chapters"][3]["content"] += "她深吸一口气。就在这时，门开了。她深吸一口气。"
    project["chapters"][2]["execution"] = {"issues": [{"category": "人物知识/逻辑", "message": "x"}, {"category": "长度", "message": "y"}]}
    report = evaluate_project(project, calls=[{"workload": "prose", "prompt_tokens": 10, "completion_tokens": 5}])
    metrics = report["metrics"]
    assert metrics["conflicts"] == 3
    assert metrics["conflict_rate"] > 0
    assert metrics["slop"]["hits"] >= 3
    assert metrics["slop"]["top_phrases"][0]["phrase"] == "深吸一口气"
    assert metrics["constory"]["subcategories"]["知情越界"] == 1
    assert {card["key"] for card in report["cards"]} >= {"conflict_rate", "slop_rate", "abandoned_rate"}
    markdown = report_markdown(report)
    assert "冲突率" in markdown and "确定矛盾" in markdown
    json.dumps(report, ensure_ascii=False)


def test_benchmark_output_evaluation_uses_case_probes():
    case = _case("relationship")
    project = _written("relationship", {2: "外婆笑着说：“回来就好。”", 3: "苏禾发现他戴着助听器。"})
    report = evaluate_benchmark_output(case, project)
    probe_ids = {item["probe_id"] for item in report["metrics"]["conflict_items"]}
    assert probe_ids == {"grandma-speaks", "early-hearing-aid"}


def test_compare_reports_direction():
    left = {"metrics": {"conflict_rate": 1.0, "slop": {"slop_rate": 2.0}, "repetition": {"health_score": 70}}}
    right = {"metrics": {"conflict_rate": 0.5, "slop": {"slop_rate": 3.0}, "repetition": {"health_score": 80}}}
    rows = {row["metric"]: row for row in compare_reports(left, right)["rows"]}
    assert rows["冲突率"]["verdict"] == "改善"
    assert rows["套话率"]["verdict"] == "变差"
    assert rows["全稿健康分"]["verdict"] == "改善"
    assert rows["弃坑率"]["verdict"] == "无数据"


def _judge_preferring(marker: str):
    async def judge(messages):
        body = messages[-1]["content"]
        a = body.split("【稿件 A】", 1)[1].split("【稿件 B】", 1)[0]
        winner = "A" if marker in a else "B"
        return {"dimensions": {key: {"winner": winner, "reason": "r"} for key in
                               ("causality", "character", "dialogue", "revelation", "pacing", "freshness")},
                "overall": winner, "summary": "s"}
    return judge


async def _always_a(messages):
    return {"dimensions": {key: {"winner": "A"} for key in
                           ("causality", "character", "dialogue", "revelation", "pacing", "freshness")},
            "overall": "A"}


def test_pairwise_swaps_positions_to_cancel_bias():
    biased = asyncio.run(pairwise_compare("甲稿", "乙稿", _always_a))
    assert biased["left_win_rate"] == 0.5
    assert biased["position_consistency"] == 0.0
    assert biased["overall"] == "tie"
    real = asyncio.run(pairwise_compare("好稿", "差稿", _judge_preferring("好")))
    assert real["left_win_rate"] == 1.0 and real["overall"] == "left"
    assert real["position_consistency"] == 1.0


def test_pairwise_manuscripts_aggregates():
    result = asyncio.run(pairwise_manuscripts(["好一", "差二"], ["差一", "好二"], _judge_preferring("好")))
    assert result["left_win_rate"] == 0.5
    assert len(result["chapters"]) == 2


def test_history_keeps_latest(tmp_path):
    history = EvaluationHistory(tmp_path, keep=3)
    for index in range(5):
        history.append("p/../1", {"kind": "metrics", "index": index})
    items = history.list("p/../1")
    assert [item["index"] for item in items] == [4, 3, 2]
    assert all(path.parent == tmp_path for path in tmp_path.iterdir())
    assert history.list("p/../1", kind="pairwise") == []


def test_evaluation_api_routes(tmp_path):
    store = ProjectStore(tmp_path / "eval.db")
    project = store.create("评测")
    project["chapters"][0]["content"] = "她深吸一口气，推开门。"
    project = store.save(project["id"], project)
    telemetry = tmp_path / "eval-telemetry.db"
    with sqlite3.connect(telemetry) as db:
        db.execute("CREATE TABLE model_calls (id TEXT PRIMARY KEY, created_at TEXT, payload TEXT)")
        db.execute("INSERT INTO model_calls VALUES (?,?,?)", ("1", "2999-01-01", json.dumps(
            {"workload": "extraction", "finish_reason": "length", "estimated_prompt_tokens": 100,
             "estimated_completion_tokens": 50, "created_at": "2999-01-01"})))

    async def fake_completion(settings, messages, **kwargs):
        assert kwargs["workload"] == "critic"
        return await _judge_preferring("好")(messages), []

    app = FastAPI()
    app.include_router(create_evaluation_router(
        lambda: store, history_root=tmp_path / "history",
        telemetry_path=lambda: telemetry, completion=fake_completion,
    ))
    with TestClient(app) as client:
        report = client.get(f"/api/projects/{project['id']}/evaluation?record=true").json()
        assert report["metrics"]["telemetry"]["extraction_length_rate"] == 1.0
        assert report["metrics"]["slop"]["hits"] == 1
        result = client.post(
            f"/api/projects/{project['id']}/evaluation/pairwise",
            json={"left_text": "好的版本", "right_text": "差的版本", "left_label": "v1", "right_label": "v2"},
        ).json()
        assert result["left_win_rate"] == 1.0 and result["left_label"] == "v1"
        items = client.get(f"/api/projects/{project['id']}/evaluation/history").json()["items"]
        assert [item["kind"] for item in items] == ["pairwise", "metrics"]
        assert client.get("/api/projects/missing/evaluation").status_code == 404


def test_main_registers_evaluation_route():
    paths = {getattr(route, "path", "") for route in main.app.routes}
    assert "/api/projects/{project_id}/evaluation" in paths
    assert "/api/projects/{project_id}/evaluation/pairwise" in paths


def _load_runner():
    import importlib.util

    spec = importlib.util.spec_from_file_location("evaluate_longform", ROOT / "scripts" / "evaluate_longform.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_offline_and_compare(tmp_path):
    runner = _load_runner()
    store = ProjectStore(tmp_path / "author.db")
    project = store.create("离线作品")
    project["chapters"][0]["content"] = "就在这时，雨停了。"
    store.save(project["id"], project)
    out = tmp_path / "eval"
    assert runner.main(["--offline", "--db", str(tmp_path / "author.db"), "--output", str(out), "--prompt-version", "base"]) == 0
    assert runner.main(["--offline", "--db", str(tmp_path / "author.db"), "--output", str(out), "--prompt-version", "next"]) == 0
    runs = sorted(path for path in out.iterdir() if path.is_dir())
    assert len(runs) == 2
    assert (runs[0] / "run.json").exists() and (runs[0] / "report.md").exists()
    assert runner.main(["--compare", str(runs[0]), str(runs[1]), "--output", str(out)]) == 0
    compare = next(path for path in out.iterdir() if path.name.endswith("-compare"))
    assert "运行对比" in (compare / "compare.md").read_text(encoding="utf-8")


def test_runner_generate_with_fake_model(tmp_path, monkeypatch):
    runner = _load_runner()
    import app.llm.client as llama_client

    seen = []

    async def fake_chat_once(settings, messages, **kwargs):
        seen.append(settings["model"])
        return "顾衡数着柴垛，一共四十二捆。杜延说道：“别数了。”"

    monkeypatch.setattr(llama_client, "chat_once", fake_chat_once)
    out = tmp_path / "eval"
    code = runner.main([
        "--case", "historical_transmigration", "--chapters", "8",
        "--base-url", "http://127.0.0.1:9/v1", "--model", "writer",
        "--output", str(out), "--prompt-version", "fake",
    ])
    assert code == 0
    assert seen == ["writer"] * 8
    run_dir = next(path for path in out.iterdir() if path.is_dir())
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    report = run["reports"]["historical_transmigration"]
    assert len(report["manuscript"]) == 8
    # Chapter 8 lets the dead 杜延 speak: the benchmark probe must catch it.
    assert any(item["probe_id"] == "dead-du-yan" for item in report["metrics"]["conflict_items"])


def test_item_event_log_from_p1_drives_probes_and_recovery_clears_them():
    from app.evaluation.longform import derive_project_probes

    project = {
        "chapters": [{"id": "c1"}, {"id": "c2"}, {"id": "c3"}],
        "memory": {
            "item_events": [
                {"item": "青铜虎符", "state": "destroyed", "chapter_number": 2},
                {"item": "旧玉佩", "state": "lost", "chapter_number": 1},
                {"item": "旧玉佩", "state": "active", "chapter_number": 3},
            ]
        },
    }
    probes = derive_project_probes(project)
    ids = [probe["id"] for probe in probes]
    assert any("青铜虎符" in pid for pid in ids)
    assert not any("旧玉佩" in pid for pid in ids)


def test_pairwise_reasons_use_stable_left_right_names():
    from app.evaluation.longform import RUBRIC

    async def judge(messages):
        user = messages[-1]["content"]
        first = user.split("【稿件 A】")[1].split("【稿件 B】")[0] if "【稿件 A】" in user else user
        label = "A" if "好稿" in first else "B"
        dims = {key: {"winner": label, "reason": f"{label}稿更具体"} for key, _, _ in RUBRIC}
        return {"dimensions": dims, "overall": label, "summary": f"{label}稿胜"}

    result = asyncio.run(pairwise_compare("好稿", "差稿", judge))
    reasons = {reason for dim in result["dimensions"].values() for reason in dim["reasons"]}
    assert reasons <= {"左稿更具体", "右稿更具体"}
    assert set(result["summaries"]) <= {"左稿胜", "右稿胜"}

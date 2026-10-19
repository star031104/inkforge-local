"""Round 3: terminal-window metrics, length normalisation and 机器味 signals."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.db import ProjectStore
from app.evaluation_suite import (
    chapter_rows,
    compare_reports,
    evaluate_project,
    machine_signals,
    report_markdown,
    run_probes,
    terminal_window,
)

ROOT = Path(__file__).resolve().parents[1]
FILLER = "雨夜里的码头空无一人，灯光在水面上碎成一片。"  # 22 non-space chars


def _filler(chars: int) -> str:
    text = FILLER * (chars // len(FILLER) + 1)
    return text[:chars]


def _project(chapters: list[str]) -> dict:
    return {
        "id": "p-term",
        "title": "终局测试",
        "narrative": {"target_chapters": len(chapters)},
        "memory": {},
        "knowledge": {},
        "chapters": [
            {"id": f"c{i}", "title": f"第{i}章", "content": content} for i, content in enumerate(chapters, start=1)
        ],
    }


PROBE = [{"id": "p1", "type": "forbidden", "category": "记忆矛盾", "pattern": "老周说道", "from_chapter": 1, "reason": "x"}]


def test_probe_conflicts_carry_book_offsets():
    project = _project([_filler(100), "老周说道：走。" + _filler(50)])
    conflicts = run_probes(chapter_rows(project), PROBE)
    assert conflicts[0]["offset"] == 100 and conflicts[0]["end"] > 100


def test_terminal_window_counts_only_final_chars():
    # 5 × 3000 chars; window = last 10000 → starts inside chapter 2 (at 5000).
    chapters = [_filler(3000) for _ in range(5)]
    chapters[1] = "老周说道：走。" + _filler(2993)  # chapter 2, offset 3000 (outside)
    chapters[4] = _filler(1500) + "老周说道：留。" + _filler(1493)  # chapter 5 (inside)
    report = evaluate_project(_project(chapters), probes=PROBE, include_derived=False)
    metrics = report["metrics"]
    terminal = metrics["terminal"]
    assert metrics["conflicts"] == 2
    assert terminal["conflicts"] == 1
    assert terminal["window_chars"] == 10000 and terminal["start_offset"] == 5000
    assert terminal["from_chapter"] == 2 and terminal["whole_text"] is False
    assert terminal["conflict_rate"] == pytest.approx(0.1)  # 1 per 10k chars
    assert metrics["conflict_rate"] == pytest.approx(round(2 * 1000 / 15000, 4))


def test_terminal_window_still_checks_against_full_preceding_text():
    # The death is settled in chapter 1 (outside the window); the dead man
    # speaks at the very end.  Detection must use the whole book.
    chapters = [_filler(6000), _filler(6000), _filler(5000) + "老周说道：我回来了。"]
    project = _project(chapters)
    project["chapters"][0]["settlement"] = {"character_updates": [{"name": "老周", "state": "在雨夜身亡"}]}
    terminal = evaluate_project(project)["metrics"]["terminal"]
    assert terminal["conflicts"] == 1
    assert terminal["conflict_categories"] == {"记忆矛盾": 1}


def report_chars(project: dict) -> int:
    return sum(len("".join(c["content"].split())) for c in project["chapters"])


def test_short_work_uses_whole_text():
    project = _project(["老周说道：走。" + _filler(500), _filler(500)])
    metrics = evaluate_project(project, probes=PROBE, include_derived=False)["metrics"]
    assert metrics["terminal"]["whole_text"] is True
    assert metrics["terminal"]["conflicts"] == metrics["conflicts"] == 1
    assert metrics["terminal"]["window_chars"] == report_chars(project)
    assert metrics["terminal"]["conflict_rate"] == metrics["conflict_rate"]


def test_terminal_window_is_configurable():
    chapters = [_filler(3000) for _ in range(5)]
    chapters[1] = "老周说道：走。" + _filler(2993)
    metrics = evaluate_project(_project(chapters), probes=PROBE, include_derived=False, terminal_chars=13000)["metrics"]
    assert metrics["terminal"]["conflicts"] == 1  # window now starts at 2000


def test_audit_issues_located_by_quote_or_chapter():
    chapters = [_filler(3000) for _ in range(5)]
    chapters[1] = "她忽然知道了密码。" + _filler(2982) + "他提前说出了凶手。"  # 3000..6000
    project = _project(chapters)
    project["chapters"][1]["execution"] = {"issues": [
        {"category": "人物知识/逻辑", "message": "早", "evidence": "她忽然知道了密码"},       # offset 3000 → outside
        {"category": "人物知识/逻辑", "message": "晚", "evidence": ["他提前说出了凶手"]},     # end of ch2 → inside
    ]}
    project["chapters"][4]["execution"] = {"issues": [{"category": "时间线", "message": "无引文"}]}  # chapter span
    project["chapters"][0]["execution"] = {"issues": [{"category": "时间线", "message": "整章", "evidence": "找不到的引文内容"}]}
    metrics = evaluate_project(project, include_derived=False)["metrics"]
    assert metrics["constory"]["continuity_total"] == 4
    assert metrics["terminal"]["continuity_issues"] == 2
    total = sum(len(c) for c in chapters)
    assert metrics["constory"]["density"] == pytest.approx(round(4 * 1000 / total, 4))
    assert metrics["terminal"]["constory_density"] == pytest.approx(0.2)


def test_store_contradictions_without_chapter_are_unlocated():
    project = _project([_filler(12000)])
    project["knowledge"] = {"facts": [
        {"subject": "苏禾", "predicate": "当前地点", "object": "书店", "status": "confirmed"},
        {"subject": "苏禾", "predicate": "当前地点", "object": "面馆", "status": "confirmed"},
    ]}
    metrics = evaluate_project(project)["metrics"]
    assert metrics["conflicts"] == 1
    assert metrics["terminal"]["conflicts"] == 0 and metrics["terminal"]["unlocated_conflicts"] == 1


def test_empty_project_has_no_division_by_zero():
    report = evaluate_project(_project(["", ""]))
    metrics = report["metrics"]
    assert metrics["terminal"]["conflict_rate"] == 0.0 and metrics["terminal"]["window_chars"] == 0
    assert metrics["normalized"]["duplicate_passages_per_10k"] == 0.0
    assert metrics["machine"]["ttr_char"] == 0.0 and metrics["machine"]["ngram_repeat"]["4"] == 0.0
    assert terminal_window([], 10000)["whole_text"] is True
    json.dumps(report, ensure_ascii=False)


def test_normalized_metrics_present():
    metrics = evaluate_project(_project([_filler(4000)]), probes=PROBE)["metrics"]
    assert set(metrics["normalized"]) >= {
        "conflicts_per_1k", "continuity_issues_per_1k", "non_continuity_per_1k",
        "slop_hits_per_1k", "duplicate_passages_per_10k", "abandoned_threads_per_10k",
    }


# ------------------------------------------------------------------ 机器味


VARIED = (
    "清晨的薄雾还没散，渔船已经离港。老秦蹲在船头补网，指节粗大。"
    "远处钟楼响了七下，卖豆浆的推车吱呀拐进巷口。她数着硬币，忽然想起昨夜那封信。"
    "风从北面来，带着铁锈和海藻的味道；码头的木板吱嘎作响，一只灰猫蹿过缆桩。"
)


def _rows(text: str) -> list[dict]:
    return chapter_rows(_project([text]))


def test_machine_signals_flag_repetitive_text():
    repetitive = machine_signals(_rows("她深吸一口气，眼神坚定地看着前方。" * 300))
    varied = machine_signals(_rows("".join(VARIED[i:] + VARIED[:i] for i in range(0, 60, 7)) * 3))
    assert repetitive["ngram_repeat"]["4"] > varied["ngram_repeat"]["4"]
    assert repetitive["ttr_char"] < varied["ttr_char"]
    assert repetitive["ttr_bigram"] < varied["ttr_bigram"]
    assert repetitive["sentence_length_std"] == 0.0 and repetitive["sentence_length_cv"] == 0.0


def test_machine_signals_are_deterministic_and_length_robust():
    once = machine_signals(_rows(VARIED * 40))
    assert once == machine_signals(_rows(VARIED * 40))
    longer = machine_signals(_rows(VARIED * 160))
    # Windowed TTR does not collapse just because the book is 4× longer.
    assert abs(once["ttr_char"] - longer["ttr_char"]) < 0.05


def test_ngrams_do_not_cross_punctuation():
    signals = machine_signals(_rows("甲乙，丙丁。" * 10))
    assert signals["ngram_repeat"]["3"] == 0.0  # no 3-char run inside any segment


# ------------------------------------------------------------------ reporting


def _terminal_report() -> dict:
    chapters = [_filler(3000) for _ in range(5)]
    chapters[4] = "老周说道：留。" + _filler(2993)
    return evaluate_project(_project(chapters), probes=PROBE, include_derived=False)


def test_cards_markdown_and_comparison_show_both_scopes():
    report = _terminal_report()
    keys = {card["key"] for card in report["cards"]}
    assert {"conflict_rate", "terminal_conflict_rate", "constory_density", "terminal_constory_density"} <= keys
    markdown = report_markdown(report)
    assert "全文与终局窗口" in markdown and "| 冲突率（/千字） |" in markdown and "机器味信号" in markdown
    rows = {row["metric"]: row for row in compare_reports(report, report)["rows"]}
    assert rows["终局冲突率"]["verdict"] == "持平" and "4 字串重复率" in rows


def test_runner_score_line_prints_full_and_terminal():
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import evaluate_longform
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    line = evaluate_longform.score_line(_terminal_report())
    assert "全文" in line and "终局" in line and "一致性问题密度" in line
    assert evaluate_longform.parse_args(["--offline", "--terminal-chars", "20000"]).terminal_chars == 20000


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "eval.db")
    monkeypatch.setattr(main, "store", store)
    with TestClient(main.app) as test_client:
        yield test_client, store


def test_evaluation_api_accepts_terminal_chars(client):
    test_client, store = client
    project = store.create("终局接口")
    project["chapters"][0]["content"] = _filler(5000)
    store.save(project["id"], project)
    default = test_client.get(f"/api/projects/{project['id']}/evaluation").json()
    assert default["metrics"]["terminal"]["configured_chars"] == 10000
    assert default["metrics"]["terminal"]["whole_text"] is True
    custom = test_client.get(f"/api/projects/{project['id']}/evaluation?terminal_chars=2000").json()
    terminal = custom["metrics"]["terminal"]
    assert terminal["configured_chars"] == 2000 and terminal["window_chars"] == 2000 and not terminal["whole_text"]
    clamped = test_client.get(f"/api/projects/{project['id']}/evaluation?terminal_chars=5").json()
    assert clamped["metrics"]["terminal"]["configured_chars"] == 1000

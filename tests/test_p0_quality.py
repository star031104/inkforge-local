"""P0 quality-loop fixes: evidence IDs, staged memory, era policy, convergence."""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.db import ProjectStore
from app.evidence_audit import (
    bind_evidence,
    draft_hash,
    fuzzy_locate,
    normalize_model_score,
    number_draft,
    split_draft_units,
    stable_audit_result,
)
from app.manuscript_quality import (
    TEMPLATE_PHRASES,
    jargon_hits,
    jargon_policy,
    learn_fatigue_phrases,
    manuscript_health_report,
    prompt_repetition_guard,
)
from app.model_telemetry import workload_health
from app.services.editorial_policy import refinement_convergence
from app.services.memory_extraction import MEMORY_STAGES, extract_staged_memory
from app.services.memory_settlement import _verified_evidence

DRAFT = (
    "赵高冷笑一声，从袖中掏出一块玉佩。他把玉佩在手中抛了抛！\n"
    "周砚没有接话。殿外的雨越下越大。"
)


def test_draft_numbering_and_id_resolution():
    units = split_draft_units(DRAFT)
    assert [unit[0] for unit in units] == ["P1-S1", "P1-S2", "P2-S1", "P2-S2"]
    numbered = number_draft(DRAFT)
    assert "[P1-S2]他把玉佩在手中抛了抛！" in numbered
    assert numbered.count("\n") == 1
    issue = bind_evidence(DRAFT, {"severity": "high", "evidence_ids": ["P2-S1"]})
    assert issue["evidence_verified"]
    assert issue["evidence"][0]["quote"] == "周砚没有接话。"
    assert issue["evidence"][0]["match"] == "id"
    assert DRAFT[issue["evidence"][0]["start"]:].startswith("周砚没有接话")
    ranged = bind_evidence(DRAFT, {"evidence_ids": ["P1-S1~P1-S2"]})
    assert ranged["evidence"][0]["quote"].endswith("抛了抛！")


def test_invalid_id_falls_back_to_quote_then_unverified():
    issue = bind_evidence(DRAFT, {"severity": "medium", "evidence_ids": ["P9-S9"], "evidence": ["殿外的雨越下越大"]})
    assert issue["evidence_verified"]
    assert issue["evidence"][0]["match"] == "exact"
    missing = bind_evidence(DRAFT, {"severity": "medium", "evidence_ids": ["P9-S9"]})
    assert not missing["evidence_verified"]


def test_fuzzy_quote_returns_original_span():
    paraphrase = "赵高冷笑一声，从袖子中掏出一块玉佩"
    span, start, ratio = fuzzy_locate(DRAFT, paraphrase)
    assert ratio >= 0.9
    assert span.startswith("赵高冷笑一声") and "袖中" in span
    issue = bind_evidence(DRAFT, {"severity": "high", "evidence": [paraphrase]})
    assert issue["evidence_verified"]
    assert issue["evidence"][0]["match"] == "fuzzy"
    assert "袖中" in issue["evidence"][0]["quote"]
    assert issue["evidence"][0]["model_quote"] == paraphrase
    unrelated = bind_evidence(DRAFT, {"severity": "high", "evidence": ["秦王政拔剑直指咸阳宫门"]})
    assert not unrelated["evidence_verified"]


def test_draft_hash_binding_rejects_other_draft():
    issue = bind_evidence(DRAFT, {"severity": "high", "evidence_ids": ["P1-S1"]})
    assert issue["draft_hash"] == draft_hash(DRAFT)
    other = DRAFT.replace("殿外", "院中")
    rebound = bind_evidence(other, issue)
    assert not rebound["evidence_verified"]
    assert rebound["evidence_status"] == "draft_mismatch"
    # Whitespace-only edits keep the same identity.
    assert draft_hash(DRAFT) == draft_hash(DRAFT.replace("\n", "\n\n "))


def test_memory_evidence_fuzzy_stores_manuscript_text():
    quote, verified = _verified_evidence(DRAFT, "从袖子中掏出一块玉佩")
    assert verified
    assert "袖中" in quote
    quote, verified = _verified_evidence(DRAFT, "完全不存在的句子内容")
    assert not verified


@pytest.mark.parametrize(
    ("value", "issues", "expected"),
    [(8, [], 80), (8.5, [], 85), (92, [], 92), (140, [], 100), (0, [], None),
     (0, [{"severity": "high"}], 0), ("bad", [], None), (None, [], None)],
)
def test_score_normalization(value, issues, expected):
    assert normalize_model_score(value, issues) == expected


def test_stable_audit_normalizes_ten_point_scale_once():
    result = stable_audit_result("正文" * 50, {"score": 9, "issues": []}, {"score": 100, "verdict": "pass"})
    assert result["score"] == 90
    already = stable_audit_result(
        "正文" * 50, {"score": 8, "score_normalized": True, "issues": []}, {"score": 100, "verdict": "pass"}
    )
    assert already["score"] == 8


def _full_memory() -> dict:
    return {
        "summary": "周砚拒绝交出玉佩。", "story_so_far": "前情。",
        "scene_settlement": {"goal_achieved": "partial"},
        "facts": [{"text": "玉佩在周砚手中", "evidence": "周砚没有接话"}],
        "character_updates": [{"name": "周砚", "state": "沉默"}],
        "plot_threads": [], "timeline": [], "relationship_updates": [],
        "continuity_notes": [], "description_updates": [],
    }


def test_staged_memory_merges_stage_fields_and_shares_prefix():
    calls = []

    async def fake(settings, messages, **kwargs):
        calls.append(messages)
        return _full_memory(), []

    result = asyncio.run(extract_staged_memory({}, "SHARED", {"facts": "旧事实"}, completion=fake))
    assert len(calls) == len(MEMORY_STAGES)
    assert all(call[1]["content"] == "SHARED" for call in calls)
    assert result["summary"] == "周砚拒绝交出玉佩。"
    assert result["facts"][0]["text"] == "玉佩在周砚手中"
    assert result["memory_mode"] == "staged"
    assert all(item["status"] == "completed" for item in result["extraction_stages"])


def test_staged_memory_halves_caps_after_truncation_and_tolerates_optional_failure():
    seen = []

    async def fake(settings, messages, **kwargs):
        instruction = messages[-1]["content"]
        seen.append(instruction)
        if "原子事实" in instruction and "facts 最多6项" in instruction:
            raise ValueError("模型返回的 JSON 未闭合，通常是输出被截断，请重试")
        if "人物关系" in instruction:
            raise ValueError("模型返回的 JSON 未闭合")
        return _full_memory(), []

    result = asyncio.run(extract_staged_memory({}, "S", {}, completion=fake))
    assert any("facts 最多3项" in item for item in seen)
    assert any("减半" in item for item in result["warnings"])
    assert result["memory_mode"] == "staged_partial"
    assert result["relationship_updates"] == []
    assert any(item["stage"] == "relations" and item["status"] == "failed" for item in result["extraction_stages"])


def test_staged_memory_core_failure_raises():
    async def fake(settings, messages, **kwargs):
        raise ValueError("JSON 未闭合")

    with pytest.raises(ValueError):
        asyncio.run(extract_staged_memory({}, "S", {}, completion=fake))


def test_telemetry_length_rate_alert():
    calls = [{"workload": "extraction", "finish_reason": "length", "status": "completed"}] * 4
    calls += [{"workload": "extraction", "finish_reason": "json_closed", "status": "completed"}] * 2
    calls += [{"workload": "prose", "finish_reason": "stop", "status": "completed"}] * 6
    health = workload_health(calls)
    assert health["extraction"]["length_rate"] == pytest.approx(0.667, abs=0.001)
    assert health["extraction"]["length_alert"]
    assert not health["prose"]["length_alert"]


def test_template_phrases_are_book_independent():
    for phrase in ("玄色龙纹深衣", "十万将士性命皆系于你手中", "余生粮为祭", "半月之约", "静默账本", "柔性冗余"):
        assert phrase not in TEMPLATE_PHRASES


def test_jargon_policy_inference_allowlist_and_weighting():
    project = {"premise": "现代程序员穿越到秦朝", "genre": "历史", "governance": {"term_allowlist": ["算筹模型"]}}
    policy = jargon_policy(project)
    assert policy["pov_modern_allowed"] and policy["inferred"]
    assert "pov_modern_allowed" not in project["governance"]
    text = "他在脑海里重建数据模型，数据像潮水。数据不会说谎。“你这数据从何而来？”李斯问。"
    plain = dict(jargon_hits(text))
    weighted = dict(jargon_hits(text, policy))
    assert plain["数据"] == 4
    assert weighted["数据"] == 1 + 3 // 3
    explicit = jargon_policy({"premise": "穿越", "governance": {"pov_modern_allowed": False}})
    assert not explicit["pov_modern_allowed"]
    allow = jargon_policy({"governance": {"term_allowlist": ["数据"]}})
    assert "数据" not in dict(jargon_hits(text, allow))


def _chapters(texts):
    return [{"id": f"c{index}", "title": f"第{index}章", "content": text} for index, text in enumerate(texts, start=1)]


def test_learned_fatigue_excludes_names_and_feeds_prompt_guard():
    import random

    rng = random.Random(7)

    def filler(size: int) -> str:
        # Unique, non-repeating CJK noise so only the planted phrase repeats.
        return "".join(chr(rng.randint(0x4E00, 0x9FA5)) for _ in range(size))

    texts = [
        f"{filler(1200)}。周砚抬起眼睛看向远处的灯火。{filler(1200)}。他把那封信收进怀里，指尖冰凉得发麻。{filler(400)}"
        for _ in range(6)
    ]
    project = {"characters": [{"name": "周砚"}], "chapters": _chapters(texts) + [{"id": "c7", "title": "新章", "content": ""}]}
    learned = learn_fatigue_phrases(project["chapters"], project)
    phrases = [item["phrase"] for item in learned]
    assert phrases, "repeated narration should be learned"
    assert "周砚" not in phrases
    assert all(item["source"] == "learned" for item in learned)
    guard = prompt_repetition_guard(project, "c7")
    assert "全书疲劳词" in guard


def test_health_report_ranks_chapter_contributions():
    texts = ["他整理名籍。", "口粮按日发放。", "城门在黄昏关闭。", "算法、数据、模型、变量与机制交织成闭环。" * 6]
    project = {"genre": "历史", "chapters": _chapters(texts)}
    report = manuscript_health_report(project)
    top = report["chapter_contributions"][0]
    assert top["chapter"] == 4 and top["jargon"] > 0
    assert report["jargon_policy"]["pov_modern_allowed"] is False


def test_refinement_convergence_rules():
    stalled = refinement_convergence(
        [{"score": 78, "failures": ["全稿健康分 78，低于发布线 82"]},
         {"score": 79, "failures": ["全稿健康分 79，低于发布线 82"]}],
        {"score": 78.5, "failures": ["全稿健康分 80，低于发布线 82"]},
    )
    assert stalled["stalled"] and "发布分" in stalled["reason"]
    repeating = refinement_convergence(
        [{"score": 70, "failures": ["现代抽象术语共 83 次"]}, {"score": 75, "failures": ["现代抽象术语共 83 次"]}],
        {"score": 80, "failures": ["现代抽象术语共 81 次"]},
    )
    assert repeating["stalled"] and "同样" in repeating["reason"]
    improving = refinement_convergence(
        [{"score": 70, "failures": ["a"]}, {"score": 74, "failures": ["b"]}], {"score": 79, "failures": ["c"]}
    )
    assert not improving["stalled"]
    assert not refinement_convergence([{"score": 70, "failures": ["a"]}], {"score": 70, "failures": ["a"]})["stalled"]
    assert not refinement_convergence([{"score": 1, "failures": ["a"]}] * 2, {"score": 90, "failures": []})["stalled"]


def test_refine_refuses_after_needs_author_until_project_changes(monkeypatch, tmp_path):
    store = ProjectStore(tmp_path / "converge.db")
    monkeypatch.setattr(main, "store", store)
    launches = []
    monkeypatch.setattr(main, "_launch_director", launches.append)
    project = store.create("不收敛")
    project["chapters"][0]["content"] = "雨夜里他把信收进怀里。" * 20
    project = store.save(project["id"], project, reason="fixture")
    for score in (78, 79, 79):
        task = store.create_director_task(project["id"], {"task_type": "refinement", "phase": "completed"})
        task.update(status="completed", release_score=score, release_failures=["全稿健康分低"],
                    release_status="needs_author")
        store.save_director_task(task["id"], task)
    with TestClient(main.app) as client:
        response = client.post(f"/api/director/projects/{project['id']}/refine", json={"scope": "all"})
        assert response.status_code == 409
        assert "无法继续收敛" in response.json()["detail"]
        forced = client.post(f"/api/director/projects/{project['id']}/refine", json={"scope": "all", "force": True})
        assert forced.status_code == 200
        assert len(launches) == 1
    assert store.recent_director_tasks(project["id"], task_type="refinement", limit=2)


def test_learned_fatigue_needs_a_real_corpus_and_ignores_copied_paragraphs():
    body = "雨落在青石阶上。周砚抬起眼睛看向远处的灯火。他把那封信收进怀里，指尖冰凉得发麻。"
    tiny = {"chapters": _chapters([body + f"第{index}次。" for index in range(6)])}
    assert learn_fatigue_phrases(tiny["chapters"], tiny) == []
    copied = "这一段文字被原样复制到了每一章里面，所以它不是套话而是重复段落。" * 3
    big = {"chapters": _chapters([copied + "\n" + "".join(chr(0x4E00 + (i * 997 + j * 31) % 20000) for j in range(3000)) for i in range(6)])}
    phrases = [item["phrase"] for item in learn_fatigue_phrases(big["chapters"], big)]
    assert not any(phrase in copied for phrase in phrases)


def test_slop_counting_is_non_overlapping_and_learned_counts_only_excess():
    from app.evaluation_suite import slop_metrics
    from app.manuscript_quality import count_phrase_hits

    hits = count_phrase_hits("他深吸一口气缓缓开口。", ["深吸一口气", "深吸一口气缓缓", "一口气"])
    assert sum(hits.values()) == 1 and hits["深吸一口气缓缓"] == 1
    rows = [{"content": "灯火摇晃，他照旧推门。", "number": n} for n in range(1, 7)]
    project = {"memory": {"learned_fatigue_phrases": [{"phrase": "照旧推门"}]}}
    metrics = slop_metrics(project, rows)
    # 6 uses across 6 chapters, allowance is one per three chapters (2): excess 4.
    assert metrics["hits"] == 4


def test_learned_fatigue_skips_setting_nouns_and_name_possessives():
    from app.manuscript_quality import learn_fatigue_phrases

    body = (
        "林默站在三号站台边，林默的手指敲着栏杆。他深吸一口气缓缓吐出，雨水顺着站台的顶棚往下淌。"
        "远处有人喊了一声，他没有回头，只是把外套领子竖了起来，像要把整座城市挡在外面。"
    )
    chapters = [{"id": f"c{i}", "content": (body + f"第{i}段的新内容各不相同，编号{i}。") * 30} for i in range(6)]
    project = {
        "characters": [],
        "outline": "林默在三号站台等待一列不存在的车。",
        "chapters": chapters,
    }
    phrases = [item["phrase"] for item in learn_fatigue_phrases(chapters, project, limit=50)]
    assert not any("三号站台" in phrase for phrase in phrases)
    assert "林默的手指" not in phrases


def test_learned_fatigue_ignores_half_names():
    from app.manuscript_quality import learn_fatigue_phrases

    chapters = []
    for i in range(6):
        parts = [f"第{i}章第{j}节，风从街角吹来，林默的手指在桌面上停了一下，又说起别的事情编号{i}{j}。" for j in range(40)]
        chapters.append({"id": f"c{i}", "content": "\n".join(parts)})
    project = {"characters": [{"name": "林默"}], "chapters": chapters}
    phrases = [item["phrase"] for item in learn_fatigue_phrases(chapters, project, limit=80)]
    assert "默的手指" not in phrases and "林默的手指" not in phrases

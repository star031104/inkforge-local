from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from app.db import ProjectStore
from app.project_service import ProjectConflictError, save_project_versioned
from app.prompts import build_prompt
from app.temporal_context import chapter_context
from app.temporal_context import invalidate_changed_manuscript
from app.memory_integrity import chapter_content_hash
from app.evidence_audit import stable_audit_result
from app.writing_skills import activate_writing_skills


@pytest.fixture
def store(tmp_path):
    return ProjectStore(tmp_path / "project.db")


def test_missing_version_cannot_overwrite(store):
    old = store.create("原始标题")
    newer = deepcopy(old)
    newer["title"] = "新标题"
    store.save(old["id"], newer)
    with pytest.raises(ProjectConflictError):
        save_project_versioned(store, old["id"], old)
    assert store.get(old["id"])["title"] == "新标题"


def test_atomic_writers_and_revision_rollback(store):
    project = store.create()
    barrier = threading.Barrier(2)

    def write(title):
        other = ProjectStore(store.path)
        payload = deepcopy(project)
        payload["title"] = title
        barrier.wait()
        try:
            other.save(project["id"], payload, expected_updated_at=project["updated_at"])
            return True
        except ProjectConflictError:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(write, ("甲", "乙"))) == [False, True]
    with store._connect() as db:
        assert db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1


def test_noop_does_not_bump_version_or_reindex(store):
    project = store.create()
    assert store.save(project["id"], project)["updated_at"] == project["updated_at"]


def test_fresh_settlement_invalidates_only_downstream():
    previous = {"chapters": [{"id": "one", "content": "旧文", "settlement": {"summary": "旧结算"}}, {"id": "two", "content": "后文"}]}
    current = deepcopy(previous)
    current["chapters"][0].update(content="新文", settlement={"content_hash": chapter_content_hash("新文")})
    invalidate_changed_manuscript(previous, current)
    assert current["memory"]["stale_from_chapter"] == 2
    assert not current["chapters"][0].get("memory_stale")
    assert current["chapters"][1]["memory_stale"]


def test_temporal_projection_restores_fact_before_supersession():
    project = {"chapters": [{"id": "one"}, {"id": "two"}], "memory": {"facts": [
        {"id": "old", "content": "钥匙在林舟手中", "source_chapter_id": "one", "active": False, "valid_until_chapter": 1},
        {"id": "new", "content": "钥匙已交出", "source_chapter_id": "two", "active": True}]}}
    view = chapter_context(project, 1)
    assert [f["id"] for f in view["memory"]["facts"]] == ["old"]
    assert view["memory"]["facts"][0]["active"]
    assert [f["id"] for f in chapter_context(project, 2)["memory"]["facts"]] == ["new"]


def test_reordering_settled_chapters_invalidates_timeline():
    previous = {"chapters": [{"id": "one", "content": "第一章", "settlement": {"summary": "旧结算"}}, {"id": "two", "content": "第二章"}]}
    current = deepcopy(previous)
    current["chapters"].reverse()
    invalidate_changed_manuscript(previous, current)
    assert current["memory"]["stale_from_chapter"] == 1
    assert all(c["memory_stale"] for c in current["chapters"])


def test_orphan_evidence_cannot_enter_context():
    project = {"chapters": [{"id": "one"}], "knowledge": {"facts": [
        {"id": "orphan", "source_ref": "chapter:deleted", "status": "confirmed"}]}}
    assert chapter_context(project, 1)["knowledge"]["facts"] == []


def test_future_state_cannot_enter_any_prompt_section(store):
    project = store.create()
    first = project["chapters"][0]
    project["chapters"].append({"id": "future", "title": "终局", "content": "紫鲸落月", "summary": "紫鲸落月"})
    project["memory"]["story_so_far"] = "紫鲸落月"
    project["memory"]["relationships"] = [{"left": "主角", "right": "同伴", "state": "紫鲸落月", "source_chapter_id": "future"}]
    project["knowledge"]["facts"] = [{"subject": "主角", "predicate": "位置", "object": "紫鲸落月", "status": "confirmed", "source_ref": "chapter:future"}]
    project["characters"] = [{"name": "主角", "importance": "main", "location": "紫鲸落月", "last_state_chapter_number": 2}]
    project["narrative_state"]["events"] = [{"id": "future-event", "kind": "character", "status": "confirmed", "actors": ["主角"], "deltas": {"location": "紫鲸落月"}, "chapter_id": "future"}]
    original = deepcopy(project)
    result = build_prompt(project, {"chapter_id": first["id"], "mode": "continue", "instruction": "主角继续行动"})
    assert all("紫鲸落月" not in message["content"] for message in result.messages)
    assert project == original


def test_baseline_and_verified_past_states_survive(store):
    p = store.create()
    p["chapters"][0].update(content="他抵达旧桥。", summary="抵达旧桥", settlement={"character_updates": [{"name": "主角", "location": "旧桥", "evidence": "他抵达旧桥。"}]})
    p["chapters"].append({"id": "c2"})
    p["characters"] = [{"name": "主角", "location": "终局", "last_state_chapter_number": 9, "state_baseline": {"location": "家中"}}]
    assert chapter_context(p, 0)["characters"][0]["location"] == "家中"
    assert chapter_context(p, 1)["characters"][0]["location"] == "旧桥"


@pytest.mark.parametrize("severity", ["high", "medium"])
def test_unverified_serious_issue_requires_review(severity):
    result = stable_audit_result("玉佩已经碎了。", {"score": 95, "verdict": "revise", "issues": [{"severity": severity}]}, {"score": 100, "verdict": "pass"})
    assert result["verdict"] == "partial"
    assert result["requires_review"]
    from app.main import _refinement_candidate_passes
    assert not _refinement_candidate_passes(result, {}, 80)


def test_clean_audit_can_pass():
    result = stable_audit_result("正文", {"score": 95, "issues": []}, {"score": 100, "verdict": "pass"})
    assert result["verdict"] == "pass"


def test_verified_contradiction_blocks():
    result = stable_audit_result("玉佩已经碎了。", {"score": 99, "issues": [{"severity": "high", "evidence": ["玉佩已经碎了。"]}]}, {"score": 100, "verdict": "pass"})
    assert result["verdict"] == "revise"


def test_skill_task_and_genre_boundaries(store):
    p = store.create()
    p["writing_skills"] = [{"id": "audit-only", "name": "审计", "instructions": "检查本章因果链并输出问题清单。", "mode": "always", "tasks": ["audit"], "genres": ["悬疑"]}]
    assert "audit-only" not in {s["id"] for s in activate_writing_skills(p, "", "continue", explicit_ids=["audit-only"])}
    assert "audit-only" not in {s["id"] for s in activate_writing_skills(p, "", "audit")}
    p["genre"] = "悬疑"
    assert "audit-only" in {s["id"] for s in activate_writing_skills(p, "", "audit")}


def test_edit_invalidates_downstream_memory(store):
    p = store.create()
    p["chapters"][0].update(content="旧正文", settlement={"summary": "旧事实"})
    p = store.save(p["id"], p)
    p["chapters"][0]["content"] = "新正文"
    p = store.save(p["id"], p)
    assert p["memory"]["stale_from_chapter"] == 1
    assert p["chapters"][0]["memory_stale"]


def test_index_updates_only_changed_documents(store):
    if not store.fts_enabled:
        pytest.skip("SQLite without FTS5")
    p = store.create()
    p["chapters"][0]["content"] = "旧桥有一封信。"
    p["chapters"].append({"id": "c2", "title": "二", "content": "城外传来了钟声。"})
    p = store.save(p["id"], p)
    with store._connect() as db:
        before = dict(db.execute("SELECT source_id,rowid FROM search_documents_fts"))
    p["chapters"][0]["content"] = "旧桥藏着一把钥匙。"
    store.save(p["id"], p)
    with store._connect() as db:
        after = dict(db.execute("SELECT source_id,rowid FROM search_documents_fts"))
        assert after["c2:passage:0"] == before["c2:passage:0"]
        assert "一封信" not in str([tuple(r) for r in db.execute("SELECT content FROM search_documents_fts")])

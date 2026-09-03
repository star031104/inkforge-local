from app.db import default_project
from app.knowledge import (
    graph_snapshot,
    project_accepted_memory_to_knowledge,
    relevant_facts,
    relevant_relations,
    sync_authoritative_entities,
    upsert_current_fact,
    upsert_fact,
    upsert_relation,
)


def _project():
    project = default_project("p1", "测试项目", "2026-08-22T00:00:00Z")
    project["characters"] = [
        {"id": "c1", "name": "狂三", "aliases": ["时崎狂三"], "canon_profile": {"enabled": True, "user_verified": True}},
        {"id": "c2", "name": "花火", "aliases": [], "canon_profile": {"enabled": True, "user_verified": True}},
    ]
    sync_authoritative_entities(project)
    return project


def test_candidates_are_not_hard_context_and_alias_resolves():
    project = _project()
    upsert_fact(project, {"subject": "时崎狂三", "predicate": "当前地点", "object": "车站", "status": "confirmed"})
    upsert_fact(project, {"subject": "狂三", "predicate": "秘密", "object": "模型猜测", "status": "candidate"})
    facts = relevant_facts(project, ["时崎狂三"], "狂三在哪里", limit=10)
    assert len(facts) == 1
    assert facts[0]["subject"] == "狂三"
    assert facts[0]["object"] == "车站"


def test_current_fact_supersedes_old_value():
    project = _project()
    first = upsert_current_fact(project, {"subject": "狂三", "predicate": "当前地点", "object": "车站"})
    second = upsert_current_fact(project, {"subject": "狂三", "predicate": "当前地点", "object": "公园"})
    assert first["id"] != second["id"]
    statuses = {item["object"]: item["status"] for item in project["knowledge"]["facts"]}
    assert statuses["车站"] == "superseded"
    assert statuses["公园"] == "confirmed"


def test_graph_does_not_invent_edges_from_cooccurrence():
    project = _project()
    project["chapters"] = [{"id": "ch1", "content": "狂三和花火同时出现在车站。"}]
    graph = graph_snapshot(project)
    assert {node["name"] for node in graph["nodes"]} >= {"狂三", "花火"}
    assert graph["edges"] == []


def test_relation_neighborhood_requires_confirmed_relation():
    project = _project()
    upsert_relation(project, {"source": "狂三", "target": "花火", "dimension": "rivalry", "detail": "互相试探", "status": "candidate"})
    assert relevant_relations(project, ["狂三"], "花火", limit=10) == []
    upsert_relation(project, {"source": "狂三", "target": "花火", "dimension": "rivalry", "detail": "互相试探", "status": "confirmed", "evidence": "两人都没有先移开视线。"})
    relations = relevant_relations(project, ["狂三"], "花火", limit=10)
    assert len(relations) == 1
    assert relations[0]["dimension"] == "rivalry"


def test_accepted_evidence_backed_memory_projects_to_knowledge():
    project = _project()
    chapter = {
        "id": "ch1",
        "title": "第一章",
        "content": "狂三站在钟楼下，仍旧没有放松警惕。两人都在试探对方。",
    }
    project["memory"]["relationships"] = [
        {
            "id": "rel-memory",
            "left": "狂三",
            "right": "花火",
            "state": "互相试探",
            "source_chapter_id": "ch1",
            "evidence": "两人都在试探对方。",
            "evidence_verified": True,
        }
    ]
    result = {
        "character_updates": [{"name": "狂三", "location": "钟楼", "state": "保持警惕", "evidence": "狂三站在钟楼下，仍旧没有放松警惕。"}],
        "relationship_updates": [{"left": "狂三", "right": "花火", "state": "互相试探", "evidence": "两人都在试探对方。"}],
    }
    ids = project_accepted_memory_to_knowledge(project, chapter, result)
    assert len(ids) == 3
    assert any(item["predicate"] == "当前地点" and item["object"] == "钟楼" for item in project["knowledge"]["facts"])
    assert len(project["knowledge"]["relations"]) == 1

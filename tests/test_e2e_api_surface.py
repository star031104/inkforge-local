from __future__ import annotations

import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main
from app.db import ProjectStore, default_project


def _isolated_client(monkeypatch, tmp_path):
    local_store=ProjectStore(tmp_path/"data"/"inkforge.db")
    monkeypatch.setattr(main,"store",local_store)
    monkeypatch.setattr(main,"ROOT",tmp_path)
    return TestClient(main.app),local_store


def test_project_crud_history_import_backup_and_delete(monkeypatch,tmp_path):
    client,store=_isolated_client(monkeypatch,tmp_path)
    created=client.post("/api/projects",json={"title":"端到端作品"});assert created.status_code==200
    project=created.json();pid=project["id"];cid=project["chapters"][0]["id"]
    assert any(x["id"]==pid for x in client.get("/api/projects").json())
    assert client.get(f"/api/projects/{pid}").json()["title"]=="端到端作品"

    project["chapters"][0]["content"]="第一版正文"*30
    project["_save_reason"]="e2e-first"
    saved=client.put(f"/api/projects/{pid}",json=project);assert saved.status_code==200
    project=saved.json();project["chapters"][0]["content"]="第二版正文"*30;project["_save_reason"]="e2e-second"
    assert client.put(f"/api/projects/{pid}",json=project).status_code==200

    revisions=client.get(f"/api/projects/{pid}/revisions");assert revisions.status_code==200 and revisions.json()
    versions=client.get(f"/api/projects/{pid}/chapters/{cid}/versions");assert versions.status_code==200 and versions.json()
    restored=client.post(f"/api/projects/{pid}/chapters/{cid}/versions/{versions.json()[0]['id']}/restore")
    assert restored.status_code==200

    backup=client.post("/api/backup");assert backup.status_code==200
    assert (tmp_path/"data"/"backups"/backup.json()["filename"]).exists()

    exported=store.get(pid);exported["settings"]["api_key"]=""
    imported=client.post("/api/projects/import",json=exported);assert imported.status_code==200
    imported_id=imported.json()["id"];assert imported_id!=pid
    assert client.delete(f"/api/projects/{imported_id}").status_code==200
    assert client.get(f"/api/projects/{imported_id}").status_code==404


def test_reference_style_canon_knowledge_and_local_quality_api(monkeypatch,tmp_path):
    client,_=_isolated_client(monkeypatch,tmp_path)
    project=default_project("assets","资产测试","now")
    chapter=project["chapters"][0]

    raw="这是用于测试的参考文本。"*20
    parsed=client.post("/api/reference/parse",json={"name":"sample.txt","content_base64":base64.b64encode(raw.encode()).decode()})
    assert parsed.status_code==200 and "参考文本" in parsed.json()["text"]

    async def fake_structured(settings,messages,**kwargs):
        user="\n".join(m.get("content","") for m in messages)
        if "角色名" in user:
            return ({
                "source_work":"测试原作","timeline_node":"测试节点","identity":"观察员","appearance":"黑发",
                "core_personality":"谨慎","deep_personality":"重证据","values":"真实","goals":"查清事实","fears":"误判",
                "abilities":"观察","limitations":"不能读心","speech_style":"短句","behavior_patterns":"先确认再行动",
                "emotional_patterns":"克制","relationship_patterns":"慢热","must_preserve":["谨慎"],"must_not":["无条件信任"],
                "explicit_facts":["角色谨慎"],"inferences":["可能慢热"]
            },[])
        return ({"name":"克制文风","profile":"短句、具体动作","dos":["具体动作"],"donts":["空泛总结"]},[])
    monkeypatch.setattr(main,"structured_completion",fake_structured)

    project["references"]=[
        {"id":"style-1","name":"样文","kind":"style","text":raw,"enabled":True},
        {"id":"canon-1","name":"正典","kind":"canon","source_work":"测试原作","text":"测试角色非常谨慎，不能读心。"*10,"enabled":True},
    ]
    style=client.post("/api/style/analyze-references",json={"project":project});assert style.status_code==200
    assert style.json()["source_ids"]==["style-1"]

    project["characters"]=[{"id":"char-1","name":"测试角色","role":"主角"}]
    canon=client.post("/api/canon/analyze-character",json={"project":project,"character_id":"char-1","reference_ids":["canon-1"]})
    assert canon.status_code==200 and canon.json()["profile"]["user_verified"] is False
    assert canon.json()["profile"]["must_not"]==["无条件信任"]

    fact=client.post("/api/knowledge/fact",json={"project":project,"item":{"subject":"测试角色","predicate":"current_location","object":"白塔","status":"confirmed","evidence":"正文证据"}})
    assert fact.status_code==200;project=fact.json()["project"]
    relation=client.post("/api/knowledge/relation",json={"project":project,"item":{"source":"测试角色","target":"周衡","relation":"trust","status":"confirmed","evidence":"共同检查"}})
    assert relation.status_code==200;project=relation.json()["project"]
    graph=client.post("/api/knowledge/graph",json={"project":project});assert graph.status_code==200
    assert graph.json()["nodes"] and graph.json()["edges"]

    draft="林夏沿着旋梯走向钟室。"*50
    quality=client.post("/api/chapter/quality",json={"project":project,"chapter_id":chapter["id"],"draft":draft})
    assert quality.status_code==200 and "word_count" in quality.json()
    health=client.post("/api/project/manuscript-health",json={"project":project})
    assert health.status_code==200


def test_director_control_endpoints_are_persistent(monkeypatch,tmp_path):
    client,store=_isolated_client(monkeypatch,tmp_path)
    project=store.create("导演控制测试")
    task=store.create_director_task(project["id"],{"phase":"chapters","status":"paused","message":"暂停","events":[],"config":{}})
    get=client.get(f"/api/director/tasks/{task['id']}");assert get.status_code==200 and get.json()["id"]==task["id"]
    latest=client.get(f"/api/director/projects/{project['id']}/latest");assert latest.status_code==200 and latest.json()["id"]==task["id"]
    pause=client.post(f"/api/director/tasks/{task['id']}/pause");assert pause.status_code==200 and pause.json()["status"]=="paused"

    # Resume is wired to the persisted task and schedules the runner. Replace the
    # runner with a no-op so this test covers the HTTP/state transition only.
    async def noop(_task_id):
        return None
    monkeypatch.setattr(main,"_run_auto_director",noop)
    resume=client.post(f"/api/director/tasks/{task['id']}/resume");assert resume.status_code==200
    assert resume.json()["status"] in {"queued","running"}


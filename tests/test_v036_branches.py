"""v0.36 分支试写: plot branch trials on one key chapter."""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.api import container
from app.storage.project_store import ProjectStore
from app.writing import branch_trials as trials

ROOT = Path(__file__).resolve().parents[1]
SENTENCE = "李斯把竹简压在案上，烛火晃了一下，林砚没有退，只把袖中的半枚印递了过去。"


def _prose(marker: str, chars: int = 1300) -> str:
    body = (marker + SENTENCE) * (chars // (len(SENTENCE) + len(marker)) + 1)
    paragraphs = [body[i:i + 200] for i in range(0, len(body), 200)]
    return "\n\n".join(paragraphs).rstrip("，") + "。"


def _route(number: int, goal: str) -> dict:
    return {"id": f"r{number}", "number": number, "title": f"第{number}章", "goal": goal,
            "conflict": "", "turning_point": f"转折{number}", "ending_hook": f"钩子{number}"}


def _project_payload(project: dict) -> dict:
    routes = [_route(1, "入咸阳"), _route(2, "廷前对质"), _route(3, "李斯设局"), _route(4, "夜出函谷")]
    project["planning"] = {"volumes": [{"id": "v1", "title": "卷一", "goal": "入秦立足", "chapters": routes}]}
    project["chapters"] = [
        {"id": f"c{n}", "title": f"第{n}章", "content": _prose("旧稿") if n <= 2 else "",
         "summary": f"第{n}章摘要" if n <= 2 else "", "route_id": f"r{n}", "volume_id": "v1",
         "route": dict(routes[n - 1]), "plan": {"goal": routes[n - 1]["goal"]}}
        for n in range(1, 5)
    ]
    return project


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "branches.db")
    monkeypatch.setattr(container, "store", store)
    with TestClient(main.app) as test_client:
        yield test_client, store, monkeypatch


def _fakes(monkeypatch, *, stream_delay: float = 0.0):
    calls = {"stream": [], "judge": 0, "impact": 0, "audit": 0}

    async def fake_stream(settings, messages, options=None):
        await asyncio.sleep(stream_delay)
        task = messages[-1]["content"]
        calls["stream"].append(task)
        marker = "翻脸" if "李斯当场翻脸" in task else "沉默"
        return _prose(marker), trials.StreamOutcome()

    async def fake_completion(settings, messages, **kwargs):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        if system == trials.SUMMARY_SYSTEM:
            return {"summary": "李斯当场翻脸，林砚被押" if "翻脸" in user else "林砚沉默，李斯暗中记下"}, []
        if "严格的小说主编" in system:
            calls["judge"] += 1
            first = user.split("【候选A】", 1)[1].split("【候选B】", 1)[0]
            return {"winner": "A" if "翻脸" in first else "B", "scores": {"A": 40, "B": 30},
                    "reasons": ["翻脸版冲突更足，后续须改第3章"]}, []
        if system == trials.IMPACT_SYSTEM:
            calls["impact"] += 1
            assert "第3章" in user or '"number": 3' in user
            ids = [line.split("（", 1)[0] for line in user.split("【试写分支】", 1)[1].splitlines() if line.startswith("br-")]
            return {"branches": [
                {"id": ids[0], "overall": "李斯设局失去前提", "affected": [
                    {"number": 3, "change": "李斯已翻脸，改为公开追捕", "severity": "major"},
                    {"number": 99, "change": "越界章号应被丢弃", "severity": "major"},
                ]},
                {"id": ids[1], "overall": "基本不影响", "affected": []},
            ]}, []
        if "剧情编辑" in system:
            return {"directions": [
                {"label": "当场翻脸", "direction": "李斯当场翻脸", "why": "冲突前置"},
                {"label": "沉默", "direction": "林砚选择沉默", "why": "压抑"},
                {"label": "反咬", "direction": "林砚反咬李斯", "why": "意外"},
            ]}, []
        return {}, []

    async def fake_audit(body):
        calls["audit"] += 1
        assert body.project["chapters"][1]["content"] == ""  # branch replaces the chapter
        return {"score": 88 if "翻脸" in body.draft else 72, "verdict": "pass" if "翻脸" in body.draft else "revise",
                "issues": [{"severity": "low", "message": "小问题"}, {"severity": "high", "message": "大问题"}]}

    monkeypatch.setattr(trials, "stream_prose", fake_stream)
    monkeypatch.setattr(container, "structured_completion", fake_completion)
    monkeypatch.setattr(container, "chapter_audit", fake_audit)
    return calls


def _saved_project(store) -> dict:
    project = store.create("分支试写")
    return store.save(project["id"], _project_payload(project))


def _wait(test_client, project_id):
    for _ in range(500):
        job = test_client.get(f"/api/projects/{project_id}/branches/job").json()
        if not job["running"]:
            return job
        time.sleep(0.02)
    raise AssertionError("branch job did not finish")


DIRECTIONS = [{"label": "翻脸", "direction": "李斯当场翻脸"}, {"label": "沉默", "direction": "林砚选择沉默"}]


def test_full_branch_trial_flow_and_adopt_keeps_version(client):
    test_client, store, monkeypatch = client
    calls = _fakes(monkeypatch)
    project = _saved_project(store)
    pid = project["id"]
    started = test_client.post(f"/api/projects/{pid}/branches/job",
                               json={"project": project, "chapter_id": "c2", "directions": DIRECTIONS})
    assert started.status_code == 200, started.text
    job = _wait(test_client, pid)
    assert job["status"] == "completed" and job["progress"] == 1.0, job
    # The direction reaches the normal chapter prompt as the author's instruction.
    assert len(calls["stream"]) == 2 and all("作者本次要求：【分支试写" in task for task in calls["stream"])
    assert calls["audit"] == 2 and calls["judge"] == 2 and calls["impact"] == 1
    comparison = job["comparison"]
    rows = {row["label"]: row for row in comparison["branches"]}
    assert rows["翻脸"]["wins"] == 1 and rows["沉默"]["wins"] == 0
    assert rows["翻脸"]["score"] == 88 and rows["翻脸"]["top_issues"][0]["message"] == "大问题"
    assert comparison["recommended"] == rows["翻脸"]["id"]
    assert [item["number"] for item in rows["翻脸"]["impact"]["affected"]] == [3]
    assert rows["沉默"]["summary"].startswith("林砚沉默")

    collected = test_client.post(f"/api/projects/{pid}/branches/collect", json={"project": project, "chapter_id": "c2"}).json()
    assert collected["added"] == 2
    chapter = collected["project"]["chapters"][1]
    assert {item["status"] for item in chapter["branches"]} == {"draft"}
    assert chapter["branch_session"]["base_content_hash"] == trials.content_hash(project["chapters"][1]["content"])
    again = test_client.post(f"/api/projects/{pid}/branches/collect", json={"project": collected["project"], "chapter_id": "c2"}).json()
    assert again["added"] == 0 and len(again["project"]["chapters"][1]["branches"]) == 2
    saved = store.save(pid, collected["project"], expected_updated_at=project["updated_at"])

    winner = rows["翻脸"]["id"]
    adopted = test_client.post(f"/api/projects/{pid}/branches/adopt",
                               json={"project": saved, "chapter_id": "c2", "branch_id": winner})
    assert adopted.status_code == 200, adopted.text
    result = adopted.json()
    new_chapter = result["project"]["chapters"][1]
    assert "翻脸" in new_chapter["content"] and new_chapter["summary"].startswith("李斯当场翻脸")
    statuses = {item["id"]: item["status"] for item in new_chapter["branches"]}
    assert statuses[winner] == "adopted" and statuses[rows["沉默"]["id"]] == "discarded"
    later = result["project"]["chapters"][2]
    assert later["plan_stale"]["reason"] == "branch_adopted" and later["plan_stale"]["after_chapter"] == 2
    assert result["project"]["planning"]["volumes"][0]["chapters"][2]["needs_replan"]["branch_id"] == winner
    assert "plan_stale" not in result["project"]["chapters"][3]
    assert result["report"]["replan_marked"][0]["number"] == 3
    # Adopt is never automatic: nothing changed on the server until the browser saves.
    assert "旧稿" in store.get(pid)["chapters"][1]["content"]
    store.save(pid, result["project"], reason="branch-adopt", expected_updated_at=saved["updated_at"])
    versions = store.chapter_versions(pid, "c2")
    assert versions and versions[0]["reason"] == "branch-adopt"


def test_cancel_keeps_finished_branches(client):
    test_client, store, monkeypatch = client
    _fakes(monkeypatch, stream_delay=0.0)
    project = _saved_project(store)
    pid = project["id"]
    original = trials.stream_prose
    gate = {"n": 0}

    async def slow_second(settings, messages, options=None):
        gate["n"] += 1
        if gate["n"] >= 2:
            await asyncio.sleep(30)
        return await original(settings, messages, options)

    monkeypatch.setattr(trials, "stream_prose", slow_second)
    test_client.post(f"/api/projects/{pid}/branches/job", json={"project": project, "chapter_id": "c2", "directions": DIRECTIONS})
    for _ in range(200):
        job = test_client.get(f"/api/projects/{pid}/branches/job").json()
        if any(item["status"] == "draft" for item in job["branches"]):
            break
        time.sleep(0.02)
    busy = test_client.post(f"/api/projects/{pid}/branches/job", json={"project": project, "chapter_id": "c2", "directions": DIRECTIONS})
    assert busy.status_code == 409
    cancelled = test_client.post(f"/api/projects/{pid}/branches/job/cancel").json()
    assert cancelled["status"] == "cancelled" and cancelled["running"] is False
    assert [item["status"] for item in cancelled["branches"]] == ["draft", "cancelled"]
    merged = test_client.post(f"/api/projects/{pid}/branches/collect", json={"project": project, "chapter_id": "c2"}).json()
    assert merged["added"] == 1


def test_directions_validation_and_suggest(client):
    test_client, store, monkeypatch = client
    _fakes(monkeypatch)
    project = _saved_project(store)
    pid = project["id"]
    one = test_client.post(f"/api/projects/{pid}/branches/job",
                           json={"project": project, "chapter_id": "c2", "directions": [{"direction": "只有一个"}, {"direction": "只有一个"}]})
    assert one.status_code == 422
    missing = test_client.post(f"/api/projects/{pid}/branches/job", json={"project": project, "chapter_id": "nope", "directions": DIRECTIONS})
    assert missing.status_code == 404
    suggested = test_client.post(f"/api/projects/{pid}/branches/suggest", json={"project": project, "chapter_id": "c2"})
    assert suggested.status_code == 200, suggested.text
    assert [item["direction"] for item in suggested.json()["directions"]] == ["李斯当场翻脸", "林砚选择沉默", "林砚反咬李斯"]


def test_prune_and_discard_rules():
    branches = [{"id": f"d{i}", "status": "discarded", "created_at": f"2026-01-0{i}"} for i in range(1, 5)]
    branches += [{"id": f"k{i}", "status": "draft", "created_at": f"2026-02-0{i}"} for i in range(1, 4)]
    branches.append({"id": "a", "status": "adopted", "created_at": "2025-01-01"})
    kept = trials.prune_branches(branches)
    assert len(kept) == trials.MAX_BRANCHES_PER_CHAPTER
    assert {item["id"] for item in kept} == {"d3", "d4", "k1", "k2", "k3", "a"}
    project = {"chapters": [{"id": "c1", "content": "x", "branches": [{"id": "a", "status": "adopted", "content": "y"}]}]}
    with pytest.raises(ValueError):
        trials.discard_branch(project, "c1", "a")
    with pytest.raises(ValueError):
        trials.normalize_directions([{"direction": "a"}, {"direction": "b"}, {"direction": "c"}, {"direction": "d"}])


def test_branch_prompt_reuses_chapter_prompt_without_current_prose():
    from app.storage.schema.project import ensure_project_defaults
    from app.writing.context_builder import build_prompt

    project = ensure_project_defaults(_project_payload({"id": "", "title": "测试"}))
    project["chapters"][1]["content"] = "当前章原稿独有标记。" * 30
    prompt_project = trials.branch_prompt_project(project, 1)
    request = {"project": prompt_project, "chapter_id": "c2", "mode": "instruction",
               "instruction": trials.branch_instruction("林砚选择沉默"), "selection": ""}
    text = "\n".join(message["content"] for message in build_prompt(prompt_project, request).messages)
    assert "林砚选择沉默" in text and "【分支试写·本章必须执行的剧情走向】" in text
    assert "当前章原稿独有标记" not in text and project["chapters"][1]["content"]


def test_frontend_wiring():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'id="branchTrialBtn"' in html
    assert '/assets/js/features/branches.js?v=' in html and '/assets/css/branches.css?v=' in html
    source = (ROOT / "static" / "js" / "features" / "branches.js").read_text(encoding="utf-8")
    assert 'Inkforge.register("branches"' in source
    assert "采用这一版" in source and "丢弃" in source and "AI 推荐方向" in source

"""Round 3: reader simulation with bounded cross-chapter concurrency and background jobs."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.db import ProjectStore
from app.infrastructure.extension_store import ExtensionStore
from app.services import reader_simulation as readers

PARAGRAPH = "林晚推开旧书店的门，铜铃轻轻一响。顾川站在柜台后面，把一封没有署名的信推到她面前。"


def _content(tag: str) -> str:
    return "\n".join(f"{PARAGRAPH}{tag}" for _ in range(10))


def _project(count: int = 4) -> dict:
    return {
        "id": "p-readers",
        "settings": {},
        "chapters": [
            {"id": f"c{i}", "title": f"第{i}章", "content": _content(f"第{i}章"), "plan": {"turning_point": "顾川寄信"}}
            for i in range(1, count + 1)
        ],
    }


def _reader_result(messages) -> dict:
    count = messages[1]["content"].count("[B")
    return {"blocks": [{"id": f"B{i}", "engagement": 6} for i in range(1, count + 1)], "overall": 6}


def counting_completion(delay: float = 0.02):
    state = {"active": 0, "peak": 0, "calls": 0}

    async def fake(settings, messages, **kwargs):
        state["active"] += 1
        state["calls"] += 1
        state["peak"] = max(state["peak"], state["active"])
        try:
            await asyncio.sleep(delay)
            if "扮演" in messages[0]["content"]:
                return _reader_result(messages), []
            return {"too_predictable": [], "unexplained": []}, []
        finally:
            state["active"] -= 1

    return fake, state


def test_cross_chapter_concurrency_is_bounded_and_order_kept():
    project = _project(4)
    fake, state = counting_completion()
    outcomes = asyncio.run(readers.simulate_chapters(project, [3, 0, 2, 1], completion=fake, concurrency=3))
    assert [item["chapter_id"] for item in outcomes] == ["c4", "c1", "c3", "c2"]
    assert state["peak"] == 3  # 4 chapters × 3 readers = 12 calls, never more than 3 at once
    assert state["calls"] == 12  # no predictions → the plan comparison call is skipped


def test_concurrency_of_one_is_sequential():
    fake, state = counting_completion(0.005)
    asyncio.run(readers.simulate_chapters(_project(2), [0, 1], completion=fake, concurrency=1))
    assert state["peak"] == 1


def test_bad_chapter_is_reported_in_place():
    fake, _ = counting_completion(0)
    project = _project(2)
    project["chapters"][0]["content"] = "太短"
    outcomes = asyncio.run(readers.simulate_chapters(project, [0, 1], completion=fake))
    assert isinstance(outcomes[0], ValueError) and outcomes[1]["chapter_id"] == "c2"


def test_reader_job_progress_and_order(tmp_path):
    store = ProjectStore(tmp_path / "job.db")
    extensions = ExtensionStore.for_store(store)
    project = _project(3)
    fake, _ = counting_completion(0)
    job = readers.new_reader_job(["c3", "c1", "missing"], list(readers.DEFAULT_PERSONAS), True)
    extensions.put("p", readers.JOB_KEY, job)
    final = asyncio.run(readers.run_reader_job(project=project, extensions=extensions, project_id="p", job=job, completion=fake))
    assert final["status"] == "completed"
    assert [item["chapter_id"] for item in final["results"]] == ["c3", "c1", "missing"]
    assert final["results"][2]["error"] and final["chapters_done"] == 2
    assert final["calls_done"] == final["calls_total"] == 6
    stored = readers.list_reports(extensions, "p")
    assert set(stored) == {"c1", "c3"}
    view = readers.public_reader_job(final, running=False)
    assert view["progress"] == 1.0 and view["exists"]


def test_reader_job_cancel_keeps_finished_chapters(tmp_path):
    store = ProjectStore(tmp_path / "cancel.db")
    extensions = ExtensionStore.for_store(store)
    project = _project(2)
    release = None

    async def fake(settings, messages, **kwargs):
        if "第2章" in messages[1]["content"] and "【第2章" in messages[1]["content"]:
            await release.wait()  # chapter 2 never finishes on its own
        if "扮演" in messages[0]["content"]:
            return _reader_result(messages), []
        return {"too_predictable": [], "unexplained": []}, []

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        job = readers.new_reader_job(["c1", "c2"], list(readers.DEFAULT_PERSONAS), False)
        extensions.put("p", readers.JOB_KEY, job)
        task = asyncio.create_task(readers.run_reader_job(
            project=project, extensions=extensions, project_id="p", job=job, completion=fake, concurrency=6,
        ))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if (extensions.get("p", readers.JOB_KEY) or {}).get("chapters_done") == 1:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    job = extensions.get("p", readers.JOB_KEY)
    assert job["status"] == "cancelled"
    assert job["results"][0]["chapter_id"] == "c1" and job["results"][1] is None
    assert set(readers.list_reports(extensions, "p")) == {"c1"}
    assert readers.public_reader_job(job, running=False)["status"] == "cancelled"


def test_interrupted_job_view():
    job = readers.new_reader_job(["c1"], ["veteran"], False)
    assert readers.public_reader_job(job, running=False)["status"] == "interrupted"
    assert readers.public_reader_job(None, running=False) == {"exists": False}


# ------------------------------------------------------------------ API


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "api.db")
    monkeypatch.setattr(main, "store", store)
    with TestClient(main.app) as test_client:
        yield test_client, store, monkeypatch


def _api_project(store, count: int = 3) -> dict:
    project = store.create("读者并发")
    project["chapters"] = [
        {"id": f"c{i}", "title": f"第{i}章", "content": _content(f"第{i}章")} for i in range(1, count + 1)
    ]
    return store.save(project["id"], project)


def test_sync_endpoint_runs_chapters_concurrently_in_order(client):
    test_client, store, monkeypatch = client
    project = _api_project(store, 3)
    fake, state = counting_completion()
    monkeypatch.setattr(main, "structured_completion", fake)
    response = test_client.post(
        f"/api/projects/{project['id']}/reader-simulation",
        json={"chapter_ids": ["c3", "c1", "c2"], "compare_with_plan": False},
    )
    assert response.status_code == 200, response.text
    assert [r["chapter_id"] for r in response.json()["reports"]] == ["c3", "c1", "c2"]
    assert 1 < state["peak"] <= readers.DEFAULT_CONCURRENCY


def test_sync_endpoint_unknown_chapter_is_404(client):
    test_client, store, monkeypatch = client
    project = _api_project(store, 1)
    fake, _ = counting_completion(0)
    monkeypatch.setattr(main, "structured_completion", fake)
    response = test_client.post(f"/api/projects/{project['id']}/reader-simulation", json={"chapter_ids": ["nope"]})
    assert response.status_code == 404


def test_background_job_polling(client):
    test_client, store, monkeypatch = client
    project = _api_project(store, 3)
    fake, state = counting_completion(0.01)
    monkeypatch.setattr(main, "structured_completion", fake)
    started = test_client.post(
        f"/api/projects/{project['id']}/reader-simulation?background=true",
        json={"chapter_ids": ["c2", "c1", "c3"], "compare_with_plan": True},
    )
    assert started.status_code == 200, started.text
    assert started.json()["exists"] and started.json()["calls_total"] == 12
    for _ in range(300):
        job = test_client.get(f"/api/projects/{project['id']}/reader-simulation/job").json()
        if not job["running"]:
            break
        time.sleep(0.02)
    assert job["status"] == "completed" and job["progress"] == 1.0
    assert [item["chapter_id"] for item in job["results"]] == ["c2", "c1", "c3"]
    assert state["peak"] <= readers.DEFAULT_CONCURRENCY
    reports = test_client.get(f"/api/projects/{project['id']}/reader-reports").json()["reports"]
    assert set(reports) == {"c1", "c2", "c3"}


def test_background_job_cancel(client):
    test_client, store, monkeypatch = client
    project = _api_project(store, 2)

    async def slow(settings, messages, **kwargs):
        await asyncio.sleep(30)
        return {}, []

    monkeypatch.setattr(main, "structured_completion", slow)
    test_client.post(f"/api/projects/{project['id']}/reader-simulation?background=true", json={"chapter_ids": ["c1", "c2"]})
    busy = test_client.post(f"/api/projects/{project['id']}/reader-simulation?background=true", json={"chapter_ids": ["c1"]})
    assert busy.status_code == 409
    cancelled = test_client.post(f"/api/projects/{project['id']}/reader-simulation/job/cancel").json()
    assert cancelled["status"] == "cancelled" and cancelled["running"] is False

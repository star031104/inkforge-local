"""P3 extensions: manuscript import, story graph, reader simulation, publishing."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import zipfile
from datetime import date

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.db import ProjectStore
from app.fallbacks import local_memory_result
from app.file_parsing import _docx_text, parse_reference_file
from app.infrastructure.extension_store import ExtensionStore
from app.services import manuscript_import as importer
from app.services import publishing
from app.services import reader_simulation as readers
from app.services.story_graph import build_story_graph


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


PARAGRAPH = "林晚推开旧书店的门，铜铃轻轻一响。顾川站在柜台后面，把一封没有署名的信推到她面前。"


def manuscript(chapters: int = 3, toc: bool = True) -> str:
    lines = ["雾港旧事", "作者：测试", ""]
    if toc:
        lines += [f"第{i}章 雾港{i}" for i in range(1, chapters + 1)]
    lines.append("第一卷 雾起")
    for i in range(1, chapters + 1):
        lines.append(f"第{i}章 雾港{i}")
        lines += [f"　　{PARAGRAPH}第{i}章第{j}段。" for j in range(1, 5)]
    return "\n".join(lines)


# ---------------------------------------------------------------- splitting


def test_split_detects_headings_drops_toc_and_records_volume():
    result = importer.split_manuscript(manuscript(3))
    assert result["method"] == "headings"
    assert [c["title"] for c in result["chapters"]] == ["第1章 雾港1", "第2章 雾港2", "第3章 雾港3"]
    assert all(c["volume"] == "第一卷 雾起" for c in result["chapters"])
    assert any("目录" in w for w in result["warnings"])
    first = result["chapters"][0]["content"]
    assert first.startswith("林晚推开") and "\n\n" in first and "　" not in first


def test_split_falls_back_to_length_without_headings():
    text = "\n".join(PARAGRAPH * 3 for _ in range(40))
    result = importer.split_manuscript(text, target_chars=1000)
    assert result["method"] == "length"
    assert len(result["chapters"]) > 3
    assert result["chapters"][0]["title"] == "第1章"
    assert all(len(c["content"]) >= 300 for c in result["chapters"])


def test_heading_detection_ignores_prose_sentences():
    text = "第一章 开始\n" + PARAGRAPH + "\n第二章写得很好，他说。\n" + PARAGRAPH + "\n第二章 继续\n" + PARAGRAPH
    result = importer.split_manuscript(text)
    assert [c["title"] for c in result["chapters"]] == ["第一章 开始", "第二章 继续"]
    assert "第二章写得很好" in result["chapters"][0]["content"]


def test_epub_uses_spine_order():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>')
        archive.writestr(
            "OPS/book.opf",
            '<package><manifest><item id="a" href="c10.xhtml"/><item id="b" href="c2.xhtml"/></manifest>'
            '<spine><itemref idref="b"/><itemref idref="a"/></spine></package>',
        )
        archive.writestr("OPS/c2.xhtml", "<html><body><h1>第一章 开端</h1><p>" + PARAGRAPH + "</p></body></html>")
        archive.writestr("OPS/c10.xhtml", "<html><body><h1>第二章 后续</h1><p>" + PARAGRAPH + "</p></body></html>")
    parsed = parse_reference_file("book.epub", base64.b64encode(buffer.getvalue()).decode())
    assert parsed["text"].index("第一章") < parsed["text"].index("第二章")
    titles = [c["title"] for c in importer.split_manuscript(parsed["text"])["chapters"]]
    assert titles == ["第一章 开端", "第二章 后续"]


# ------------------------------------------------------------ import flow


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "test.db")
    monkeypatch.setattr(main, "store", store)
    with TestClient(main.app) as test_client:
        yield test_client, store, monkeypatch


def fake_completion_factory(calls: list[str]):
    async def fake(settings, messages, **kwargs):
        system = messages[0]["content"]
        calls.append(system[:20])
        if "反推设定" in system:
            return {
                "summary": "林晚收到匿名信，与顾川开始调查。",
                "characters": [
                    {"name": "林晚", "aliases": ["晚晚"], "importance": "main", "role": "书店常客",
                     "personality": "冷静", "evidence": "林晚推开旧书店的门"},
                    {"name": "顾川", "importance": "supporting", "role": "书店老板", "evidence": "顾川站在柜台后面"},
                    {"name": "不存在的人", "importance": "minor", "evidence": "完全虚构的句子"},
                ],
                "world": [{"title": "雾港", "keys": ["雾港"], "content": "常年起雾的港口城市", "evidence": "铜铃轻轻一响"}],
                "foreshadow": [{"title": "匿名信", "setup": "没有署名的信", "expected_payoff": "揭示寄信人",
                                "evidence": "把一封没有署名的信推到她面前"}],
            }, []
        if "反推这部作品的整体设定" in system:
            return {
                "genre": "悬疑", "premise": "旧书店里的匿名信牵出雾港往事", "outline": "调查匿名信",
                "author_intent": "推测：克制的悬疑", "book_rules": "", "story_so_far": "两人开始调查",
                "narrative": {"pov": "third_limited", "tense": "past", "tone": "冷峻"},
            }, []
        raise AssertionError(system[:40])

    return fake


def test_import_end_to_end_with_review_and_settlement(client):
    test_client, store, monkeypatch = client
    calls: list[str] = []
    monkeypatch.setattr(main, "structured_completion", fake_completion_factory(calls))
    settled: list[str] = []

    async def fake_memory(request):
        project = request.project
        chapter = next(c for c in project["chapters"] if c["id"] == request.chapter_id)
        settled.append(chapter["title"])
        return local_memory_result(project, chapter, request.draft, "测试")

    monkeypatch.setattr(main, "chapter_memory", fake_memory)
    preview = test_client.post("/api/import/preview", json={"name": "雾港.txt", "content_base64": b64(manuscript(3))}).json()
    assert preview["chapter_count"] == 3 and preview["title_guess"] == "雾港旧事"

    created = test_client.post(
        "/api/import/create?wait=true",
        json={"name": "雾港.txt", "content_base64": b64(manuscript(3)), "genre": "悬疑"},
    )
    assert created.status_code == 200, created.text
    project = created.json()["project"]
    job = created.json()["job"]
    assert project["title"] == "雾港旧事" and len(project["chapters"]) == 3
    assert all(c["authority_state"] == "locked" for c in project["chapters"])
    assert job["status"] == "awaiting_review"
    review = job["review"]
    names = {c["name"]: c for c in review["characters"]}
    assert names["林晚"]["evidence_verified"] and "晚晚" in names["林晚"]["aliases"]
    assert names["不存在的人"]["verified_in_text"] is False and not names["不存在的人"]["suggested"]
    assert review["fields"]["premise"].startswith("旧书店")
    # Nothing reaches the project before the author confirms.
    assert store.get(project["id"])["characters"] == []

    confirm = test_client.post(
        f"/api/projects/{project['id']}/import-job/confirm?wait=true",
        json={
            "fields": ["premise", "narrative", "story_so_far"],
            "characters": [names["林晚"]["id"], names["顾川"]["id"]],
            "world_entries": [review["world_entries"][0]["id"]],
            "foreshadow": [review["foreshadow"][0]["id"]],
        },
    )
    assert confirm.status_code == 200, confirm.text
    final = store.get(project["id"])
    assert {c["name"] for c in final["characters"]} == {"林晚", "顾川"}
    assert final["premise"].startswith("旧书店")
    assert final["narrative"]["pov"] == "third_limited"
    assert final["world_entries"][0]["title"] == "雾港"
    assert final["memory"]["plot_threads"][0]["title"] == "匿名信"
    assert settled == ["第1章 雾港1", "第2章 雾港2", "第3章 雾港3"]
    assert all(c["memory_status"] == "committed" for c in final["chapters"])
    status = test_client.get(f"/api/projects/{project['id']}/import-job").json()
    assert status["status"] == "completed" and status["progress"]["settlement"] == 1.0


def test_settlement_pauses_and_resumes_from_checkpoint(tmp_path):
    store = ProjectStore(tmp_path / "pause.db")
    extensions = ExtensionStore.for_store(store)
    split = importer.split_manuscript(manuscript(3, toc=False))
    project = importer.create_imported_project(store, split, title="暂停测试")
    job = importer.new_job("x.txt", 3, split["total_chars"])
    extensions.put(project["id"], importer.JOB_KEY, job)
    seen: list[str] = []

    async def memory(request):
        seen.append(request.chapter_id)
        if len(seen) == 1:  # author presses pause while chapter 1 is settling
            current = extensions.get(project["id"], importer.JOB_KEY)
            current["pause_requested"] = True
            extensions.put(project["id"], importer.JOB_KEY, current)
        chapter = next(c for c in request.project["chapters"] if c["id"] == request.chapter_id)
        return local_memory_result(request.project, chapter, request.draft, "测试")

    from app.api.schemas import ChapterActionRequest
    from app.services.memory_settlement import _apply_director_memory

    kwargs = dict(store=store, extensions=extensions, project_id=project["id"], chapter_memory=memory,
                  apply_memory=_apply_director_memory, request_factory=ChapterActionRequest)
    paused = asyncio.run(importer.run_import_settlement(**kwargs))
    assert paused["status"] == "paused" and paused["settle_index"] == 1
    done = asyncio.run(importer.run_import_settlement(**kwargs))
    assert done["status"] == "completed"
    assert len(seen) == 3 and len(set(seen)) == 3


def test_settlement_failure_pauses_with_error(tmp_path):
    store = ProjectStore(tmp_path / "fail.db")
    extensions = ExtensionStore.for_store(store)
    split = importer.split_manuscript(manuscript(2, toc=False))
    project = importer.create_imported_project(store, split, title="失败测试")
    extensions.put(project["id"], importer.JOB_KEY, importer.new_job("x", 2, 1))

    async def broken(_request):
        raise RuntimeError("模型连接失败")

    from app.api.schemas import ChapterActionRequest
    from app.services.memory_settlement import _apply_director_memory

    job = asyncio.run(importer.run_import_settlement(
        store=store, extensions=extensions, project_id=project["id"], chapter_memory=broken,
        apply_memory=_apply_director_memory, request_factory=ChapterActionRequest,
    ))
    assert job["status"] == "paused" and "模型连接失败" in job["error"]
    assert job["settle_index"] == 0 and job["failed_chapters"][0]["index"] == 1


def test_import_refuses_target_project_with_prose(client):
    test_client, store, _ = client
    existing = store.create("已有正文")
    existing["chapters"][0]["content"] = PARAGRAPH
    store.save(existing["id"], existing)
    response = test_client.post(
        "/api/import/create",
        json={"name": "a.txt", "content_base64": b64(manuscript(2)), "target_project_id": existing["id"],
              "start_analysis": False},
    )
    assert response.status_code == 422


def test_plan_batches_samples_long_manuscripts():
    chapters = [{"content": "字" * 6000} for _ in range(60)]
    batches = importer.plan_batches(chapters, max_batches=5)
    selected = [b for b in batches if b["status"] == "pending"]
    assert len(selected) == 5
    assert selected[0]["index"] == 0 and selected[-1]["index"] == len(batches) - 1


# ------------------------------------------------------------------ graph


def graph_project():
    chapters = [{"id": f"c{i}", "title": f"第{i}章", "content": "林晚和顾川在雾港。" * 3} for i in range(1, 6)]
    return {
        "chapters": chapters,
        "characters": [
            {"id": "a", "name": "林晚", "aliases": ["晚晚"], "importance": "main"},
            {"id": "b", "name": "顾川", "aliases": [], "importance": "supporting"},
        ],
        "knowledge": {"entities": [], "relations": []},
        "memory": {
            "relationships": [
                {"left": "林晚", "right": "顾川", "state": "互相信任的盟友", "source_chapter_id": "c2",
                 "last_chapter_number": 2, "evidence_verified": True},
                {"left": "顾川", "right": "白先生", "state": "敌对", "source_chapter_id": "c4", "last_chapter_number": 4},
            ],
            "timeline": [
                {"id": "t1", "event": "收到匿名信", "chapter_id": "c1", "chapter_number": 1, "participants": ["林晚", "顾川"]},
                {"id": "t2", "event": "码头对峙", "chapter_id": "c4", "chapter_number": 4, "participants": ["顾川", "白先生"]},
            ],
            "plot_threads": [
                {"id": "p1", "title": "匿名信", "status": "closed", "created_chapter_number": 1, "closed_chapter_number": 4},
                {"id": "p2", "title": "白先生身份", "status": "open", "created_chapter_number": 3},
                {"id": "p3", "title": "未来伏笔", "status": "open", "created_chapter_number": 5},
            ],
            "facts": [], "continuity_notes": [], "description_ledger": [],
        },
    }


def test_story_graph_respects_temporal_boundary():
    early = build_story_graph(graph_project(), 3)
    assert {n["name"] for n in early["nodes"]} == {"林晚", "顾川"}
    assert len(early["edges"]) == 1
    edge = early["edges"][0]
    assert edge["polarity"] == 1 and edge["chapter"] == 2 and edge["weight"] >= 2  # + co-appearance
    assert [e["event"] for e in early["timeline"]] == ["收到匿名信"]
    threads = {t["title"]: t for t in early["threads"]}
    assert threads["匿名信"]["open"] and threads["匿名信"]["status"] == "open"
    assert "未来伏笔" not in threads

    late = build_story_graph(graph_project(), 5)
    names = {n["name"]: n for n in late["nodes"]}
    assert "白先生" in names and names["白先生"]["registered"] is False
    assert any(e["polarity"] == -1 for e in late["edges"])
    threads = {t["title"]: t for t in late["threads"]}
    assert threads["匿名信"]["payoff"] == 4 and not threads["匿名信"]["open"]
    assert late["stats"]["open_threads"] == 2


def test_story_graph_api(client):
    test_client, store, _ = client
    project = store.create("图谱")
    data = graph_project()
    project["chapters"] = data["chapters"]
    project["characters"] = data["characters"]
    project["memory"].update(data["memory"])
    store.save(project["id"], project)
    response = test_client.get(f"/api/projects/{project['id']}/story-graph?as_of=2")
    assert response.status_code == 200
    body = response.json()
    assert body["as_of"] == 2 and body["chapter_count"] == 5


# ------------------------------------------------------- reader simulation


def test_split_blocks_and_normalization():
    text = "\n".join(PARAGRAPH * 2 for _ in range(30))
    blocks = readers.split_blocks(text)
    assert 2 <= len(blocks) <= readers.MAX_BLOCKS
    ids = [b["id"] for b in blocks]
    normalized = readers.normalize_reader_result(
        {"blocks": [{"id": "b1", "engagement": 14}], "drop_off_block": "B99", "overall": "7"}, ids
    )
    assert normalized["blocks"][0]["engagement"] == 10
    assert normalized["blocks"][1]["missing"] is True
    assert normalized["drop_off_block"] is None and normalized["overall"] == 7


def test_reader_visible_recap_never_reads_plans():
    project = {"chapters": [
        {"title": "一", "summary": "林晚收到信", "content": "x", "plan": {"turning_point": "顾川就是寄信人"}},
        {"title": "二", "content": "正文" * 10},
    ]}
    recap = readers.reader_visible_recap(project, 1, 3)
    assert "林晚收到信" in recap and "寄信人" not in recap


def test_reader_simulation_api_aggregates_and_flags_predictable_twist(client):
    test_client, store, monkeypatch = client
    project = store.create("读者")
    project["chapters"][0]["content"] = "\n".join(PARAGRAPH for _ in range(12))
    project["chapters"][0]["plan"] = {"turning_point": "顾川就是寄出匿名信的人", "ending_hook": ""}
    project = store.save(project["id"], project)
    seen_users: list[str] = []

    async def fake(settings, messages, **kwargs):
        if "扮演" in messages[0]["content"]:
            seen_users.append(messages[1]["content"])
            count = messages[1]["content"].count("[B")
            return {
                "blocks": [{"id": f"B{i}", "engagement": 8 if i < count else 2} for i in range(1, count + 1)],
                "confusion": [{"block": "B1", "question": "顾川是谁？"}],
                "predictions": [{"prediction": "顾川就是寄出匿名信的人", "confidence": 0.8}],
                "hook": {"works": False, "reason": "平淡"},
                "drop_off_block": f"B{count}",
                "overall": 6,
            }, []
        return {"too_predictable": [], "unexplained": [{"question": "顾川是谁？", "planned_explanation": "", "severity": "high"}]}, []

    monkeypatch.setattr(main, "structured_completion", fake)
    response = test_client.post(
        f"/api/projects/{project['id']}/reader-simulation",
        json={"chapter_ids": [project["chapters"][0]["id"]]},
    )
    assert response.status_code == 200, response.text
    report = response.json()["reports"][0]
    assert len(report["readers"]) == 3
    assert report["summary"]["hook_votes"] == 0
    assert any("钩子" in finding for finding in report["summary"]["findings"])
    assert report["comparison"]["too_predictable"][0]["source"] == "本地相似度"
    assert report["comparison"]["unexplained"][0]["severity"] == "high"
    assert all("寄出匿名信" not in user for user in seen_users)  # plan never shown to readers
    stored = test_client.get(f"/api/projects/{project['id']}/reader-reports").json()["reports"]
    assert len(stored[project["chapters"][0]["id"]]) == 1


# -------------------------------------------------------------- publishing


def publish_project():
    return {
        "id": "p1",
        "title": "雾港",
        "premise": "旧书店里的匿名信",
        "narrative": {"target_chapters": 10},
        "chapters": [
            {"id": "c1", "title": "第1章 雾起", "content": "# 标题\n**林晚**推开门。\n\n> 顾川抬头。", "authority_state": "locked"},
            {"id": "c2", "title": "旧信", "content": "她拆开信。", "authority_state": "locked"},
            {"id": "c3", "title": "空章", "content": ""},
        ],
    }


def test_chinese_numbers():
    assert [publishing.chinese_number(n) for n in (1, 10, 12, 20, 105, 1010, 2024)] == [
        "一", "十", "十二", "二十", "一百零五", "一千零一十", "二千零二十四"
    ]


def test_txt_export_follows_webnovel_conventions():
    result = publishing.export_txt(publish_project(), {"indent": True, "blank_line": True})
    text = result["content"]
    assert "第一章 雾起" in text and "第二章 旧信" in text and "空章" not in text
    assert "　　林晚推开门。" in text and "**" not in text and "#" not in text and ">" not in text
    assert result["chapters"][0]["words"] == count_cjk("标题林晚推开门顾川抬头")


def count_cjk(text):
    return publishing.count_words(text)


def test_epub_and_docx_exports_are_valid_archives():
    epub = publishing.export_epub(publish_project(), {"author": "测试"})
    archive = zipfile.ZipFile(io.BytesIO(epub["content"]))
    first = archive.infolist()[0]
    assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED
    assert archive.read("mimetype") == b"application/epub+zip"
    opf = archive.read("OEBPS/content.opf").decode()
    assert "chapter-0002.xhtml" in opf and "<dc:title>雾港</dc:title>" in opf
    assert "第二章 旧信" in archive.read("OEBPS/nav.xhtml").decode()
    docx = publishing.export_docx(publish_project())
    assert "第一章 雾起" in _docx_text(docx["content"])


def test_cadence_report_streak_buffer_projection():
    project = publish_project()
    schedule = publishing.normalize_schedule({"chapters_per_day": 1, "target_words": 4, "volume_end_chapter": 5})
    schedule = publishing.set_chapter_status(schedule, "c1", "published", "2026-10-18")
    schedule = publishing.set_chapter_status(schedule, "c2", "ready")
    report = publishing.cadence_report(project, schedule, today=date(2026, 10, 19))
    stats = report["stats"]
    assert stats["published"] == 1 and stats["buffer"] == 1 and stats["streak_days"] == 1
    assert stats["remaining_to_publish"] == 4
    assert stats["projected_volume_end"] == "2026-10-23"
    assert any("存稿" in w for w in report["warnings"])
    all_rest = publishing.normalize_schedule({"rest_weekdays": [0, 1, 2, 3, 4, 5, 6]})
    assert publishing.cadence_report(project, all_rest, today=date(2026, 10, 19))["stats"]["streak_days"] == 0


def test_publishing_api_and_export(client):
    test_client, store, _ = client
    project = store.create("发布")
    project["chapters"] = publish_project()["chapters"]
    project = store.save(project["id"], project)
    pid = project["id"]
    chapter_id = project["chapters"][0]["id"]
    state = test_client.put(f"/api/projects/{pid}/publishing/schedule", json={"schedule": {"chapters_per_day": 2}}).json()
    assert state["schedule"]["chapters_per_day"] == 2
    state = test_client.put(f"/api/projects/{pid}/publishing/chapters/{chapter_id}", json={"status": "published"}).json()
    assert state["stats"]["published"] == 1
    assert test_client.put(f"/api/projects/{pid}/publishing/chapters/{chapter_id}", json={"status": "bad"}).status_code == 422
    exported = test_client.post(f"/api/projects/{pid}/export/webnovel", json={"format": "epub"}).json()
    assert exported["filename"].endswith(".epub")
    assert zipfile.ZipFile(io.BytesIO(base64.b64decode(exported["content_base64"]))).testzip() is None
    assert test_client.post(f"/api/projects/{pid}/export/webnovel", json={"format": "pdf"}).status_code == 422
    # Schedule lives outside the project JSON, so browser saves never clobber it.
    assert "publishing" not in json.dumps(store.get(pid)["settings"])
    store.delete(pid)
    assert ExtensionStore.for_store(store).get(pid, publishing.SCHEDULE_KEY) is None


def test_import_merge_keeps_characters_sharing_a_generic_alias_apart():
    from app.services.manuscript_import import merge_batch_results

    chapters = [{"content": "沈少爷推门而入。顾少爷没有抬头。阿默就是沈默。"}]
    batches = [
        {"status": "done", "chapter_indexes": [0], "result": {"characters": [
            {"name": "沈默", "aliases": ["少爷"]},
            {"name": "顾川", "aliases": ["少爷"]},
        ]}},
        {"status": "done", "chapter_indexes": [0], "result": {"characters": [
            {"name": "阿默", "aliases": ["沈默"]},
        ]}},
    ]
    merged = merge_batch_results(batches, chapters)
    names = sorted(item["name"] for item in merged["characters"])
    assert names == ["沈默", "顾川"]
    shen = next(item for item in merged["characters"] if item["name"] == "沈默")
    assert "阿默" in shen["aliases"]

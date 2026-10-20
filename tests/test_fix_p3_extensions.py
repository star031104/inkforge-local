"""Regression tests for the P3 extension review (export, import, migration, races)."""

from __future__ import annotations

import asyncio
import base64
import io
import threading
import zipfile
from xml.dom import minidom

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.api.schemas import ChapterActionRequest
from app.db import ProjectStore
from app.domain.project_schema import ensure_project_defaults
from app.fallbacks import local_memory_result
from app.file_parsing import parse_reference_file
from app import file_parsing
from app.infrastructure.extension_store import ExtensionStore
from app.infrastructure.secret_store import apply_project_secrets
from app.services import manuscript_import as importer
from app.io import export_formats
from app.services.memory_settlement import _apply_director_memory

BODY = "他走了进来，看了看四周，什么也没说。" * 5
PARAGRAPH = "林晚推开旧书店的门，铜铃轻轻一响。顾川站在柜台后面，把一封没有署名的信推到她面前。"

NASTY = {
    "id": "p-nasty",
    "title": "A&B<C>\x0b😀",
    "premise": "简介\x01含控制符&",
    "chapters": [
        {
            "id": "c1",
            "title": '第一章 "引号"&<b>\x0c',
            "content": "hello & <i> \x0b \x1f 😀 \ud800 ]]> 结尾￿",
            "authority_state": "locked",
        }
    ],
}


def _xml_parts(content: bytes) -> dict[str, bytes]:
    archive = zipfile.ZipFile(io.BytesIO(content))
    return {
        name: archive.read(name)
        for name in archive.namelist()
        if name.endswith((".xml", ".xhtml", ".opf", ".rels"))
    }


# ------------------------------------------------------------------ export


@pytest.mark.parametrize("exporter", [export_formats.export_epub, export_formats.export_docx])
def test_export_strips_xml_illegal_chars_and_lone_surrogates(exporter):
    result = exporter(NASTY, {"author": "<me>&\x08"})
    parts = _xml_parts(result["content"])
    assert parts
    for name, data in parts.items():
        minidom.parseString(data)  # raises on any illegal char / bad escaping
    joined = b"".join(parts.values()).decode("utf-8")
    assert "😀" in joined and "&amp;" in joined and "&lt;" in joined
    assert "\x0b" not in joined and "\x08" not in result["filename"]


def test_epub_mimetype_first_and_stored():
    archive = zipfile.ZipFile(io.BytesIO(export_formats.export_epub(NASTY)["content"]))
    first = archive.infolist()[0]
    assert first.filename == "mimetype" and first.compress_type == zipfile.ZIP_STORED


def test_txt_export_survives_lone_surrogate_and_control_chars():
    result = export_formats.export_txt(NASTY)
    result["content"].encode("utf-8")  # used to raise UnicodeEncodeError
    assert "\x0b" not in result["content"] and "\x0b" not in result["filename"]


def test_export_heading_strips_uppercase_chinese_numeral_prefix():
    assert export_formats.chapter_heading(1, "第壹章 开端") == "第一章 开端"


# ------------------------------------------------------------------ import split


def test_split_recognises_uppercase_chinese_numerals():
    text = f"第壹章 开端\n{BODY}\n第贰章 发展\n{BODY}\n第拾壹章 转折\n{BODY}"
    result = importer.split_manuscript(text)
    assert result["method"] == "headings"
    assert [c["title"] for c in result["chapters"]] == ["第壹章 开端", "第贰章 发展", "第拾壹章 转折"]


@pytest.mark.parametrize(
    "line", ["第三节课他睡着了", "第五回合开始", "第一章节", "番外的事情不要再提了", "第二话不说就走了"]
)
def test_prose_lines_are_not_headings(line):
    assert importer._is_heading(line) is None
    text = f"第一章 开始\n{BODY}\n{line}\n{BODY}\n第二章 继续\n{BODY}"
    titles = [c["title"] for c in importer.split_manuscript(text)["chapters"]]
    assert titles == ["第一章 开始", "第二章 继续"]


@pytest.mark.parametrize(
    "line", ["第一章", "第一章 雾起", "第12话：重逢", "番外：小师妹的故事", "番外篇二", "番外之一 春", "楔子", "Chapter 3: Fog"]
)
def test_real_headings_still_detected(line):
    assert importer._is_heading(line) is not None


def test_volume_heading_requires_separator():
    text = f"第一章 a\n{BODY}\n第三部分\n{BODY}\n第二章 b\n{BODY}"
    chapters = importer.split_manuscript(text)["chapters"]
    assert "第三部分" in chapters[0]["content"]


def test_length_split_breaks_text_without_newlines():
    sentence = "他说：“走吧。”她没有回答。"
    result = importer.split_manuscript(sentence * 1000, mode="length", target_chars=1000)
    assert len(result["chapters"]) >= 7  # used to be a single 15000-char chapter
    assert all(len(c["content"]) <= 2000 for c in result["chapters"])
    # Cuts land on sentence ends, never between a full stop and its closing quote.
    assert all(c["content"].endswith(("。", "”")) for c in result["chapters"])
    assert "".join(c["content"] for c in result["chapters"]) == sentence * 1000


def test_length_split_hard_cuts_unpunctuated_text():
    result = importer.split_manuscript("字" * 9000, mode="length", target_chars=3000)
    assert [len(c["content"]) for c in result["chapters"]] == [3000, 3000, 3000]


# ------------------------------------------------------------------ zip bomb


def test_zip_bomb_rejected_before_inflating(monkeypatch):
    monkeypatch.setattr(file_parsing, "MAX_UNCOMPRESSED_BYTES", 1024 * 1024)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"<a>" + b"0" * (2 * 1024 * 1024) + b"</a>")
    payload = base64.b64encode(buffer.getvalue()).decode()
    with pytest.raises(ValueError, match="解压后超过"):
        parse_reference_file("bomb.docx", payload)


def test_corrupt_docx_is_a_value_error():
    with pytest.raises(ValueError):
        parse_reference_file("broken.docx", base64.b64encode(b"not a zip at all" * 4).decode())


# ------------------------------------------------------------------ settlement


def _imported(tmp_path, name):
    store = ProjectStore(tmp_path / f"{name}.db")
    extensions = ExtensionStore.for_store(store)
    text = "\n".join(f"第{i}章 雾港{i}\n" + "\n".join(PARAGRAPH for _ in range(4)) for i in range(1, 4))
    split = importer.split_manuscript(text)
    project = importer.create_imported_project(store, split, title=name)
    extensions.put(project["id"], importer.JOB_KEY, importer.new_job("x.txt", 3, split["total_chars"]))
    return store, extensions, project


def _settle(store, extensions, project_id, memory):
    return asyncio.run(importer.run_import_settlement(
        store=store, extensions=extensions, project_id=project_id, chapter_memory=memory,
        apply_memory=_apply_director_memory, request_factory=ChapterActionRequest,
    ))


def test_settlement_retry_uses_fresh_content(tmp_path):
    store, extensions, project = _imported(tmp_path, "retry")
    first_id = project["chapters"][0]["id"]
    requests: list[tuple[str, str]] = []

    async def memory(request):
        requests.append((request.chapter_id, request.draft))
        if len(requests) == 1:
            # The author rewrites chapter 1 while its memory is being settled
            # -> save conflict -> retry must read the new text.
            current = store.get(project["id"])
            edited = next(c for c in current["chapters"] if c["id"] == first_id)
            edited["content"] = "改写后的第一章。" + PARAGRAPH * 3
            store.save(project["id"], current)
        chapter = next(c for c in request.project["chapters"] if c["id"] == request.chapter_id)
        return local_memory_result(request.project, chapter, request.draft, "测试")

    _settle(store, extensions, project["id"], memory)
    assert requests[1] == (first_id, "改写后的第一章。" + PARAGRAPH * 3)
    settled = next(c for c in store.get(project["id"])["chapters"] if c["id"] == first_id)
    assert settled["memory_status"] == "committed"


def test_settlement_retry_locates_chapter_by_id(tmp_path):
    store, extensions, project = _imported(tmp_path, "insert")
    first_id = project["chapters"][0]["id"]
    seen: list[str] = []

    async def memory(request):
        seen.append(request.chapter_id)
        if len(seen) == 1:  # author inserts a chapter in front mid-settlement
            current = store.get(project["id"])
            current["chapters"].insert(0, {"id": "inserted", "title": "插入", "content": PARAGRAPH * 3})
            store.save(project["id"], current)
        chapter = next(c for c in request.project["chapters"] if c["id"] == request.chapter_id)
        return local_memory_result(request.project, chapter, request.draft, "测试")

    _settle(store, extensions, project["id"], memory)
    # The retry stays on the same chapter instead of silently switching to
    # whatever now sits at index 0.
    assert seen[:2] == [first_id, first_id]


def test_settlement_skips_chapter_deleted_mid_run(tmp_path):
    store, extensions, project = _imported(tmp_path, "deleted")
    first_id = project["chapters"][0]["id"]
    seen: list[str] = []

    async def memory(request):
        seen.append(request.chapter_id)
        if len(seen) == 1:
            current = store.get(project["id"])
            current["chapters"] = [c for c in current["chapters"] if c["id"] != first_id]
            store.save(project["id"], current)
        chapter = next(c for c in request.project["chapters"] if c["id"] == request.chapter_id)
        return local_memory_result(request.project, chapter, request.draft, "测试")

    job = _settle(store, extensions, project["id"], memory)
    assert job["status"] == "completed"
    remaining = [c["id"] for c in store.get(project["id"])["chapters"]]
    assert set(seen[1:]) == set(remaining) and first_id not in seen[1:]


# ------------------------------------------------------------------ atomic job/report writes


def test_pause_request_never_rolls_back_progress(tmp_path):
    store = ProjectStore(tmp_path / "pause.db")
    extensions = ExtensionStore.for_store(store)
    job = importer.new_job("x", 10, 1)
    job.update({"status": "settling", "settle_index": 5})
    extensions.put("p", importer.JOB_KEY, job)
    # Background task checkpoints the next chapter after the handler looked.
    progressed = dict(job, settle_index=6)
    extensions.put("p", importer.JOB_KEY, progressed)
    importer.request_pause(extensions, "p")
    stored = extensions.get("p", importer.JOB_KEY)
    assert stored["settle_index"] == 6 and stored["pause_requested"] is True


def test_pause_request_does_not_touch_finished_job(tmp_path):
    store = ProjectStore(tmp_path / "pause2.db")
    extensions = ExtensionStore.for_store(store)
    extensions.put("p", importer.JOB_KEY, dict(importer.new_job("x", 1, 1), status="completed"))
    importer.request_pause(extensions, "p")
    stored = extensions.get("p", importer.JOB_KEY)
    assert stored["status"] == "completed" and not stored["pause_requested"]


def test_persist_keeps_pause_flag(tmp_path):
    store = ProjectStore(tmp_path / "persist.db")
    extensions = ExtensionStore.for_store(store)
    job = dict(importer.new_job("x", 3, 1), status="settling")
    extensions.put("p", importer.JOB_KEY, job)
    importer.request_pause(extensions, "p")
    importer._persist(extensions, "p", dict(job, settle_index=2))
    stored = extensions.get("p", importer.JOB_KEY)
    assert stored["pause_requested"] and stored["settle_index"] == 2


# ------------------------------------------------------------------ migration


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:11434/v1",      # Ollama
        "http://127.0.0.1:8000/v1",       # vLLM on loopback
        "http://192.168.1.20:8081/v1",    # llama.cpp on the LAN
        "http://[::1]:1234/v1",           # LM Studio over IPv6 loopback
    ],
)
def test_keyless_local_primary_is_kept_during_dual_migration(base_url):
    project = ensure_project_defaults({"settings": {
        "model_routing": "dual",
        "base_url": base_url,
        "model": "local-writer",
        "reasoning_base_url": "https://api.deepseek.com/v1",
        "reasoning_model": "deepseek-chat",
    }})
    apply_project_secrets(project, {"settings.reasoning_api_key": "sk-cloud"})
    settings = project["settings"]
    assert settings["model"] == "local-writer"
    assert settings["base_url"].startswith(base_url.rsplit("/v1", 1)[0])
    assert settings["api_key"] != "sk-cloud"


def test_keyless_public_primary_still_adopts_secondary():
    project = ensure_project_defaults({"settings": {
        "model_routing": "dual",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt",
        "reasoning_base_url": "https://api.deepseek.com/v1",
        "reasoning_model": "deepseek-chat",
    }})
    apply_project_secrets(project, {"settings.reasoning_api_key": "sk-cloud"})
    assert project["settings"]["base_url"] == "https://api.deepseek.com/v1"
    assert project["settings"]["api_key"] == "sk-cloud"


# ------------------------------------------------------------------ API


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "api.db")
    monkeypatch.setattr(main, "store", store)
    with TestClient(main.app) as test_client:
        yield test_client, store


def test_export_api_with_nasty_content_returns_valid_epub(client):
    test_client, store = client
    project = store.create("A&B\x0b")
    project["chapters"][0]["content"] = "正文 & <b> \x0b 😀"
    store.save(project["id"], project)
    response = test_client.post(f"/api/projects/{project['id']}/export/manuscript", json={"format": "epub"})
    assert response.status_code == 200, response.text
    for data in _xml_parts(base64.b64decode(response.json()["content_base64"])).values():
        minidom.parseString(data)


def test_compact_heading_without_separator_is_accepted_but_prose_is_not():
    from app.services.manuscript_import import _is_heading

    assert _is_heading("第一章雾起").group("title") == "雾起"
    assert _is_heading("第十二回风雪山神庙")
    for prose in ("第三节课他睡着了", "第五回合开始", "第一章节", "第三章，他走了", "第二回到家时天黑了"):
        assert not _is_heading(prose), prose

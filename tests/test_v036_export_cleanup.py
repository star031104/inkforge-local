"""v0.36 导出前清理: deterministic manuscript lint & fix before export."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.api import container
from app.core.config import APP_VERSION
from app.storage.project_store import ProjectStore
from app.io import export_cleanup as ec

ROOT = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------ punctuation

def fix(content: str, *keys: str, style: str = "curly") -> str:
    return ec.apply_punctuation(content, keys or ec.PUNCTUATION_FIXES, style)[0]


def test_quotes_follow_dominant_style_and_fix_direction():
    assert fix('他说:"走吧."', "quotes") == "他说:“走吧.”"
    assert fix("“你好“她说。", "quotes") == "“你好”她说。"
    assert fix("「你好」她说。“走”", "quotes", style="corner") == "「你好」她说。「走」"
    # English apostrophes and pure-English paragraphs are left alone.
    assert fix("他读了 Tom's 的信，'别走'。", "quotes") == "他读了 Tom's 的信，‘别走’。"
    assert fix('He said "hi".', "quotes") == 'He said "hi".'


def test_dominant_quote_style_prefers_corner_brackets_when_book_uses_them():
    project = {"chapters": [{"content": "「走」「好」「行」"}, {"content": "“一”"}]}
    assert ec.dominant_quote_style(project) == "corner"
    assert ec.dominant_quote_style({"chapters": [{"content": "“一”“二”"}]}) == "curly"


def test_dashes_ellipsis_halfwidth_spaces_and_duplicates():
    assert fix("他看着她--没有说话—然后———走了", "dashes") == "他看着她——没有说话——然后——走了"
    assert fix("等等...再说。。。好…", "ellipsis") == "等等……再说……好……"
    assert fix("你好,世界!他说:来;去?好.", "halfwidth") == "你好，世界！他说：来；去？好。"
    assert fix("版本3.5和1,000不变", "halfwidth") == "版本3.5和1,000不变"
    assert fix("林砚站在门口 等待 ，雨很大", "spaces") == "林砚站在门口等待，雨很大"
    assert fix("好。。走，，吧", "duplicate_punct") == "好。走，吧"
    assert fix("　　段落内容。  ", "whitespace") == "段落内容。"
    assert fix("一\n\n\n\n二\n \n\n三\n\n", "blank_lines") == "一\n\n二\n\n三"
    # Scene separators and ASCII ranges are not dashes to normalise.
    assert fix("***\n———\nA-B", "dashes") == "***\n———\nA-B"


def test_ellipsis_runs_before_duplicate_punctuation():
    assert fix("他说。。。", "ellipsis", "duplicate_punct") == "他说……"


def test_unbalanced_quotes_respect_multi_paragraph_speech():
    content = "“第一段话没有收尾。\n“第二段才结束。”\n他说“没关上。\n平常一段。"
    issues = ec.unbalanced_quotes(content, "curly")
    assert [item["paragraph"] for item in issues] == [2]


# ------------------------------------------------------------- repetition

def test_window_repeat_reports_distinctive_words_but_not_names():
    content = "夜色很深。她把青铜灯放在桌上，青铜灯的光照着窗。窗外的雨没有停，她又看了一眼青铜灯。林砚林砚林砚。"
    hits = ec.window_repeats(content, ["林砚"])
    phrases = [item["phrase"] for item in hits]
    assert "青铜灯" in phrases
    assert not any("林砚" in phrase for phrase in phrases)
    assert all(item["paragraph"] == 0 for item in hits)


def test_same_openings_in_consecutive_paragraphs():
    content = "他慢慢地走过去。\n他慢慢地坐下。\n雨停了。\n林砚抬头。\n林砚低头。\n林砚转身。"
    issues = ec.same_openings(content)
    messages = [item["message"] for item in issues]
    assert any("他慢慢" in message for message in messages)
    assert any("连续 3 段" in message and "林砚" in message for message in messages)


def test_overused_phrases_are_reported_per_10k_chars():
    chapters = [{"id": f"c{i}", "title": f"第{i}章", "content": "他深吸一口气，推开门。" + "雨落在窗上。" * 20}
                for i in range(1, 6)]
    project = {"chapters": chapters, "governance": {"banned_phrases": ["推开门"]}}
    issues = ec.overused_phrases(project, list(enumerate(chapters, start=1)))
    by_phrase = {item["phrase"]: item for item in issues}
    assert by_phrase["深吸一口气"]["count"] == 5
    assert by_phrase["深吸一口气"]["per_10k"] > 0
    assert by_phrase["推开门"]["source"] == "author"
    assert len(by_phrase["深吸一口气"]["locations"]) == 5


# ------------------------------------------------------------------ names

def names_project(text: str) -> dict:
    return {
        "characters": [{"name": "林砚", "aliases": ["阿砚"]}, {"name": "秦王政", "aliases": ["秦王"]}],
        "chapters": [{"id": "c1", "title": "第一章", "content": text}],
    }


def test_name_variants_flag_near_miss_but_not_titles_or_common_words():
    text = "林砚走进来。林砚坐下。林砚说话。林岩看着窗外。林中很安静。秦王政与秦王对坐。"
    project = names_project(text)
    variants = ec.name_variants(project, list(enumerate(project["chapters"], start=1)))
    found = {item["variant"]: item for item in variants}
    assert "林岩" in found and found["林岩"]["name"] == "林砚"
    assert found["林岩"]["locations"][0]["chapter_id"] == "c1"
    assert "林中" not in found  # 中 is an everyday character, not a typo
    assert "秦王" not in found and "秦王政" not in found


def test_name_mapping_never_rewrites_inside_longer_known_names():
    text, counts = ec.apply_name_mappings("秦王政与秦王对坐，秦王笑了。", [("秦王", "嬴政")], ["秦王政", "秦王"])
    assert text == "秦王政与嬴政对坐，嬴政笑了。"
    assert counts["秦王→嬴政"] == 2
    with pytest.raises(ValueError):
        ec.normalize_mappings([{"from": "林", "to": "林砚"}])


# -------------------------------------------------------------- formatting

def test_formatting_numbering_duplicates_and_empty_chapters():
    project = {"chapters": [
        {"id": "a", "title": "第一章 雨夜", "content": "正文"},
        {"id": "b", "title": "第2章 雨夜", "content": "正文"},
        {"id": "c", "title": "第三章 空", "content": " "},
        {"id": "d", "title": "第五章 远行", "content": "正文"},
    ]}
    issues, style = ec.formatting_issues(project, list(enumerate(project["chapters"], start=1)))
    codes = [(item["code"], item["chapter_id"]) for item in issues]
    assert style == "cn"
    assert ("title_numbering", "b") in codes
    assert ("duplicate_title", "b") in codes
    assert ("empty_chapter", "c") in codes
    assert ("numbering_gap", "d") in codes
    assert ec.renumber_title("第12章 归来", "cn") == "第十二章 归来"
    assert ec.renumber_title("第一百零五章", "arabic") == "第105章"
    assert ec.parse_chinese_number("二〇三") == 203


def test_scan_groups_counts_and_locations():
    project = {
        "characters": [{"name": "林砚"}],
        "chapters": [
            {"id": "c1", "title": "第一章 起", "content": '林砚说:"走吧..."\n\n\n林砚走了。林砚回头。林岩愣住。'},
            {"id": "c2", "title": "第2章 承", "content": ""},
        ],
    }
    report = ec.scan(project)
    assert set(report["summary"]) == {"punctuation", "repetition", "names", "formatting"}
    assert report["summary"]["punctuation"]["codes"]["quotes"]["count"] == 2
    assert report["summary"]["names"]["count"] == 1
    fixes = {item["key"]: item for item in report["fixes"]}
    assert fixes["ellipsis"]["count"] == 1 and fixes["title_numbering"]["count"] == 1
    first = report["issues"]["punctuation"][0]
    assert first["chapter_id"] == "c1" and first["paragraph"] == 0 and first["fixable"]
    assert report["name_variants"][0]["variant"] == "林岩"


# --------------------------------------------------------------------- API

@pytest.fixture
def client(tmp_path, monkeypatch):
    store = ProjectStore(tmp_path / "cleanup.db")
    monkeypatch.setattr(container, "store", store)
    with TestClient(main.app) as test_client:
        yield test_client, store


def seeded(store: ProjectStore) -> dict:
    project = store.create("清理测试")
    locked_text = '她说:"好..."'
    project["characters"] = [{"id": "p1", "name": "林砚", "aliases": []}]
    project["chapters"] = [
        {"id": "c1", "title": "第一章 雨夜", "content": '林砚说:"走吧..."\n\n\n林砚走了。林砚回头。林岩愣住了。'},
        {"id": "c2", "title": "第2章 晴", "content": "林砚笑了,林岩也笑了。"},
        {"id": "c3", "title": "第三章 锁", "content": locked_text, "authority_state": "locked",
         "locked_content_hash": hashlib.sha256(locked_text.encode()).hexdigest()},
    ]
    return store.save(project["id"], project, reason="seed")


def test_scan_and_apply_snapshot_versions_and_skip_locked(client):
    test_client, store = client
    project = seeded(store)
    scan = test_client.post(f"/api/projects/{project['id']}/export-cleanup/scan", json={})
    assert scan.status_code == 200
    report = scan.json()
    assert report["chapters_scanned"] == 3
    assert [item["chapter_id"] for item in report["locked_chapters"]] == ["c3"]
    assert report["project_updated_at"] == project["updated_at"]

    applied = test_client.post(f"/api/projects/{project['id']}/export-cleanup/apply", json={
        "fixes": ["quotes", "ellipsis", "halfwidth", "blank_lines", "title_numbering"],
        "name_mappings": [{"from": "林岩", "to": "林砚"}],
        "expected_updated_at": project["updated_at"],
    })
    assert applied.status_code == 200, applied.text
    result = applied.json()
    assert result["changed_count"] == 2
    assert [item["chapter_id"] for item in result["skipped_locked"]] == ["c3"]
    assert result["totals"]["林岩→林砚"] == 2

    saved = store.get(project["id"])
    chapters = {chapter["id"]: chapter for chapter in saved["chapters"]}
    assert chapters["c1"]["content"] == "林砚说：“走吧……”\n\n林砚走了。林砚回头。林砚愣住了。"
    assert chapters["c2"]["title"] == "第二章 晴"
    assert chapters["c2"]["content"] == "林砚笑了，林砚也笑了。"
    assert chapters["c3"]["content"] == '她说:"好..."'  # locked: untouched
    assert chapters["c3"]["authority_state"] == "locked"

    versions = store.chapter_versions(project["id"], "c1")
    assert versions[0]["reason"] == ec.VERSION_REASON
    assert all(item["reason"] != ec.VERSION_REASON for item in store.chapter_versions(project["id"], "c3"))
    restored = test_client.post(
        f"/api/projects/{project['id']}/chapters/c1/versions/{versions[0]['id']}/restore")
    assert restored.status_code == 200
    assert store.get(project["id"])["chapters"][0]["content"].startswith('林砚说:"走吧..."')


def test_apply_can_include_locked_chapters_explicitly(client):
    test_client, store = client
    project = seeded(store)
    result = test_client.post(f"/api/projects/{project['id']}/export-cleanup/apply", json={
        "fixes": ["quotes"], "chapter_ids": ["c3"], "include_locked": True,
    }).json()
    assert result["changed_count"] == 1 and result["changed_chapters"][0]["was_locked"]
    chapter = store.get(project["id"])["chapters"][2]
    assert chapter["content"] == "她说:“好...”"
    assert chapter["authority_state"] != "locked"


def test_apply_rejects_unknown_fix_and_stale_project(client):
    test_client, store = client
    project = seeded(store)
    url = f"/api/projects/{project['id']}/export-cleanup/apply"
    assert test_client.post(url, json={"fixes": ["rewrite_everything"]}).status_code == 422
    assert test_client.post(url, json={}).status_code == 422
    stale = test_client.post(url, json={"fixes": ["quotes"], "expected_updated_at": "1999-01-01"})
    assert stale.status_code == 409
    assert test_client.post("/api/projects/nope/export-cleanup/scan", json={}).status_code == 404


def test_scan_respects_chapter_range(client):
    test_client, store = client
    project = seeded(store)
    report = test_client.post(f"/api/projects/{project['id']}/export-cleanup/scan", json={"start": 2, "end": 2}).json()
    assert report["chapters_scanned"] == 1
    assert {item["chapter_id"] for item in report["issues"]["punctuation"]} == {"c2"}


# --------------------------------------------------------------- frontend

def test_frontend_hooks_cleanup_into_export_flow():
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert f"/assets/js/features/io/export-cleanup.js?v={APP_VERSION}" in html
    assert f"/assets/css/features/export-cleanup.css?v={APP_VERSION}" in html
    assert html.index("features/io/manuscript-export.js") < html.index("features/io/export-cleanup.js") < html.index("bootstrap.js")
    source = (ROOT / "static" / "js" / "features" / "io" / "export-cleanup.js").read_text(encoding="utf-8")
    assert 'Inkforge.register("exportCleanup"' in source
    for text in ("导出前检查", "应用所选修复", "跳过检查直接导出", "/export-cleanup/scan", "/export-cleanup/apply"):
        assert text in source
    bootstrap = (ROOT / "static" / "js" / "bootstrap.js").read_text(encoding="utf-8")
    assert "exportCleanupGate(exportModal)" in bootstrap
    export_js = (ROOT / "static" / "js" / "features" / "io" / "manuscript-export.js").read_text(encoding="utf-8")
    assert "exportCleanup" in export_js and "pubCleanup" in export_js


def test_first_character_swaps_of_two_char_names_are_not_reported():
    from app.io.export_cleanup import name_variants

    project = {"characters": [{"name": "老篙", "aliases": []}, {"name": "沈二", "aliases": []}]}
    text = "老篙撑船。老篙笑了。竹篙插进水里。斤二米。沈二来了，沈二走了。"
    chapters = [(1, {"id": "c1", "title": "一", "content": text})]
    assert name_variants(project, chapters) == []

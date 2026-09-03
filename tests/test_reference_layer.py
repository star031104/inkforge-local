from app.db import default_project
from app.references import combined_style_corpus, relevant_reference_chunks, source_similarity_report


def test_reference_retrieval_prefers_relevant_chunk():
    project = default_project("p1", "测试项目", "2026-08-22T00:00:00Z")
    project["references"] = [
        {"id": "r1", "name": "角色资料", "kind": "canon", "enabled": True, "text": "狂三总是谨慎观察陌生人。\n她会先试探，再判断是否合作。"},
        {"id": "r2", "name": "地点", "kind": "background", "enabled": True, "text": "旧港口常年有雾。"},
    ]
    hits = relevant_reference_chunks(project, "狂三如何面对陌生人", limit=2)
    assert hits
    assert hits[0]["reference_id"] == "r1"


def test_style_corpus_only_uses_style_references():
    project = default_project("p1", "测试项目", "2026-08-22T00:00:00Z")
    project["references"] = [
        {"id": "s", "name": "样文", "kind": "style", "enabled": True, "text": "这是文风样本文本。"},
        {"id": "c", "name": "正典", "kind": "canon", "enabled": True, "text": "这是人物资料。"},
    ]
    corpus = combined_style_corpus(project)
    assert "文风样本" in corpus
    assert "人物资料" not in corpus


def test_copy_guard_flags_long_exact_overlap():
    phrase = "夜色沿着窗框慢慢坠下来她没有回头只是把指尖停在冰冷的玻璃上远处的列车穿过雨幕灯光在她眼底一闪而过"
    project = default_project("p1", "测试项目", "2026-08-22T00:00:00Z")
    project["references"] = [{"id": "s", "name": "样文", "kind": "style", "enabled": True, "text": "开头" + phrase + "结尾"}]
    report = source_similarity_report(project, "新的段落" + phrase + "然后故事继续")
    assert report["risk"] in {"medium", "high"}
    assert report["max_match_chars"] >= 36


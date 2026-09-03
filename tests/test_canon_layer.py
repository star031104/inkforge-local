from app.canon import render_canon_context
from app.db import default_project


def _project(verified=True):
    project = default_project("p1", "测试项目", "2026-08-22T00:00:00Z")
    project["fanfic"] = {"enabled": True, "mode": "canon", "source_universes": ["约会大作战"]}
    project["characters"] = [{
        "id": "c1",
        "name": "时崎狂三",
        "canon_profile": {
            "enabled": True,
            "user_verified": verified,
            "source_work": "约会大作战",
            "identity": "精灵",
            "core_personality": "从容、危险、善于试探",
            "speech_style": "礼貌而带玩味",
            "must_preserve": ["保持警惕与主动判断"],
            "must_not": ["无铺垫地完全信任陌生人"],
        },
    }]
    return project


def test_verified_canon_lock_is_rendered_as_high_priority_constraint():
    text = render_canon_context(_project(True), "时崎狂三走进房间")
    assert "同人正典锁" in text
    assert "人工核对" in text
    assert "推动剧情的便利" in text
    assert "无铺垫地完全信任陌生人" in text


def test_unverified_profile_is_explicitly_not_hard_canon():
    text = render_canon_context(_project(False), "时崎狂三")
    assert "待核对档案" in text
    assert "只能把与用户输入/资料片段一致的内容当硬约束" in text


def test_disabled_fanfic_has_no_canon_context():
    project = _project(True)
    project["fanfic"]["enabled"] = False
    assert render_canon_context(project, "时崎狂三") == ""


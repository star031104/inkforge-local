"""v0.35: author-specified volume counts, planning constraints, calmer scoring."""
from pathlib import Path

from app.story.evidence import downgrade_self_cleared, penalty_total
from app.planning import (
    fallback_master_plan,
    planned_volume_count,
    planning_constraints,
)
from app.llm.providers import is_local_endpoint
from app.quality import local_quality_check
from app.services.editorial_policy import _systemic_quality_issues

ROOT = Path(__file__).resolve().parents[1]
OUTLINE = "卷一「客」（前247–前238）：甲\n卷二「逐客」：乙\n卷三「一天下」：丙\n卷四「书同文」：丁"


def test_volume_count_follows_author_before_size_default():
    assert planned_volume_count({"outline": OUTLINE}, 120) == 4
    assert planned_volume_count({"outline": ""}, 120, "共4卷，每卷约30章") == 4
    assert planned_volume_count({"outline": ""}, 120, "分为六卷") == 6
    assert planned_volume_count({"outline": "", "narrative": {"volume_count": 5}}, 120, "共4卷") == 5
    assert planned_volume_count({"outline": ""}, 120) == 4  # cloud default: ~30 chapters per volume
    assert planned_volume_count({"outline": ""}, 120, local=True) == 10
    assert planned_volume_count({"outline": ""}, 5) == 1


def test_fallback_reads_juan_yi_style_headings():
    plan = fallback_master_plan({"outline": OUTLINE, "narrative": {}}, 120, "timeout")
    assert [v["chapter_end"] for v in plan["volumes"]] == [30, 60, 90, 120]
    assert "本地模型" not in plan["warnings"][0]


def test_planning_constraints_carry_chronology_and_parked_characters():
    project = {
        "world_entries": [{"title": "编年", "content": "前237年逐客令", "constant": True, "enabled": True},
                          {"title": "地理", "content": "咸阳", "constant": False}],
        "characters": [{"name": "尉缭", "active": False, "hard_limits": "卷二之前不登场"},
                       {"name": "林砚", "active": True, "hard_limits": "不会武功"}],
    }
    text = planning_constraints(project)
    assert "前237年逐客令" in text and "咸阳" not in text
    assert "尉缭的硬边界：卷二之前不登场" in text
    assert "作者暂未启用的人物" in text and "尉缭" in text.split("作者暂未启用的人物")[1]


def test_repeated_findings_in_one_category_cost_less():
    flat = [{"severity": "medium", "category": "人物记忆与能力"}] * 3
    assert penalty_total(flat) == 8 + 4 + 2
    lows = [{"severity": "low", "category": f"c{i}"} for i in range(6)]
    assert penalty_total(lows) == 9


def test_self_cleared_finding_becomes_a_note():
    issue = {"severity": "medium", "message": "顺序略倒置", "reason": "文本已说明，实际不矛盾。"}
    assert downgrade_self_cleared(issue)["severity"] == "low"
    real = {"severity": "high", "message": "出现胶底鞋", "reason": "这在当时不合理"}
    assert downgrade_self_cleared(real)["severity"] == "high"


def test_focused_timeline_family_is_not_systemic():
    audit = {"issues": [{"severity": "medium", "category": "时间线、因果与弃坑", "message": "顺序"}],
             "local_checks": {"issues": []}}
    assert _systemic_quality_issues(audit) == []
    repeated = {"issues": [], "local_checks": {"issues": [{"severity": "medium", "category": "跨章重复段落"}]}}
    assert _systemic_quality_issues(repeated)


def test_a_few_dashes_are_not_ai_slop():
    text = ("他停了一下——又走了。" + "风很大，土坡上的人排成长队。" * 40) * 3
    issues = local_quality_check(text, target_words=len(text), genre="历史")["issues"]
    assert not any(item.get("category") == "AI套话" for item in issues)
    dashed = "他停了一下——又走了——再停——" * 20
    issues = local_quality_check(dashed, target_words=len(dashed))["issues"]
    assert any("破折号×" in item.get("message", "") for item in issues)


def test_local_endpoint_detection():
    assert is_local_endpoint({"base_url": "http://127.0.0.1:8080/v1"})
    assert is_local_endpoint({"base_url": "http://192.168.1.5:11434"})
    assert not is_local_endpoint({"base_url": "https://api.deepseek.com/v1"})


def test_director_poll_does_not_reopen_a_closed_dialog():
    source = (ROOT / "static/js/features/director.js").read_text(encoding="utf-8")
    assert '$("#modal").open&&($("#directorWorkspace")' in source


def test_planning_hint_is_model_aware():
    source = (ROOT / "static/js/features/planning.js").read_text(encoding="utf-8")
    assert "本地9B" not in source and "planningWaitHint()" in source

"""Near-miss chapters don't trip the quality breaker; uncertain critic notes are not errors."""
from app.director.chapters import _near_miss
from app.review.local_quality import local_quality_check
from app.story.evidence import downgrade_self_cleared


def test_near_miss_requires_no_high_issue_and_close_score():
    assert _near_miss({"score": 78, "issues": [{"severity": "medium"}]}, 82)
    assert not _near_miss({"score": 70, "issues": []}, 82)
    assert not _near_miss({"score": 81, "issues": [{"severity": "high"}]}, 82)
    assert not _near_miss({"score": 80, "local_checks": {"issues": [{"severity": "high"}]}}, 82)


def test_uncertain_or_self_cleared_critic_notes_become_low():
    for reason in ("需确认前文是否给过不同年龄", "此处用词本身不矛盾", "可通", "此句本身无误"):
        issue = {"severity": "medium", "message": "x", "reason": reason}
        assert downgrade_self_cleared(issue)["severity"] == "low", reason


def test_gesture_like_remembering_is_not_invented_backstory():
    text = "工师走到棚口，又像想起了什么，回头看他一眼：“司空先生走时跟我说，这人先别放走。”" + "他点头。" * 200
    issues = local_quality_check(text, target_words=len(text))["issues"]
    assert not any(item.get("category") == "疑似新增往事" for item in issues)

"""v0.35: genre-aware planning prompts without book-specific overfitting."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.planning as planning_module
import app.prompts as prompts_module
from app.db import ensure_project_defaults
from app.genre_profiles import (
    PROFILES,
    batch_slot_roles,
    detect_genre_key,
    genre_profile,
    is_period_setting,
    planning_guidance,
    volume_batch_size,
    volume_batches,
)
from app.narrative_policy import route_guidance

ROOT = Path(__file__).resolve().parents[1]

# Nouns that leaked from one earlier book into every project.
BOOK_SPECIFIC = (
    "粮仓", "密档", "调令", "审查机构", "账册", "粮册", "试点",
    "周砚", "天下有数", "沈衡",
)
# Wording that bends every book toward an investigation plot.
INVESTIGATION_BIASED = ("调查", "证据", "证物", "算法")

PLANNING_PROMPT_NAMES_IN_PROMPTS = (
    "CHAPTER_PLAN_PROMPT", "IDEAS_PROMPT", "INCUBATOR_PROMPT",
    "INCUBATOR_ASSETS_PROMPT", "DIRECTOR_SEED_BRIEF_PROMPT", "DIRECTOR_CAST_PROMPT",
    "DIRECTOR_CHARACTER_CARD_PROMPT", "DIRECTOR_WORLD_PROMPT",
)


def _prompt_constants(module) -> dict[str, str]:
    return {
        name: value
        for name, value in vars(module).items()
        if name.endswith("_PROMPT") and isinstance(value, str)
    }


@pytest.mark.parametrize(
    ("genre", "premise", "expected"),
    [
        ("历史穿越·战国", "", "history_transmigration"),
        ("穿越", "", "history_transmigration"),
        ("历史权谋", "", "history_power"),
        ("宫斗", "", "history_power"),
        ("悬疑推理", "", "mystery"),
        ("历史悬疑", "", "mystery"),
        ("都市言情", "", "romance"),
        ("穿越修仙", "", "fantasy"),
        ("仙侠", "", "fantasy"),
        ("硬科幻", "", "scifi"),
        ("现实主义文学", "", "realist"),
        ("", "一名法医在雨夜发现第二具尸体，凶手留下了同一枚纽扣。", "mystery"),
        ("", "她穿越回到古代，成了落魄世家的庶女。", "history_transmigration"),
        ("", "少年在宗门大比中觉醒灵根，开始修炼。", "fantasy"),
        ("", "", "general"),
    ],
)
def test_genre_detection(genre, premise, expected):
    assert detect_genre_key({"genre": genre, "premise": premise}) == expected


def test_structure_profile_fallback_and_explicit_override():
    assert detect_genre_key({"narrative": {"structure_profile": "relationship"}}) == "romance"
    assert detect_genre_key(
        {"genre": "悬疑", "narrative": {"genre_profile": "realist"}}
    ) == "realist"


def test_required_profiles_exist_with_complete_phases():
    assert {
        "history_power", "history_transmigration", "mystery", "romance",
        "fantasy", "scifi", "realist", "general",
    } <= set(PROFILES)
    for profile in PROFILES.values():
        assert profile.guidance and len(profile.event_palette) >= 6
        for phase in ("opening", "escalation", "turn", "finale"):
            roles = profile.slot_roles[phase]
            assert len(roles) == 6 and len(set(roles)) == 6
            assert profile.phase_focus[phase]


@pytest.mark.parametrize("key", sorted(PROFILES))
@pytest.mark.parametrize("size", [1, 2, 3, 4, 5, 6, 8])
def test_slot_roles_match_batch_size(key, size):
    profile = PROFILES[key]
    for total in (1, 2, 3, 5):
        for number in range(1, total + 1):
            roles = batch_slot_roles(profile, number, total, size)
            assert len(roles) == size
            assert len(set(roles)) == size
            if number == total:
                # The final batch always ends on the volume-landing role.
                assert roles[-1] == profile.slot_roles["finale"][-1]


def test_slot_roles_use_genre_terms():
    history = genre_profile({"genre": "历史穿越·战国"})
    opening = "".join(batch_slot_roles(history, 1, 3, 3))
    assert "立场" in opening or "表态" in opening
    mystery = genre_profile({"genre": "悬疑"})
    assert "线索" in "".join(batch_slot_roles(mystery, 1, 3, 6))
    assert "误导" in "".join(batch_slot_roles(mystery, 2, 4, 6))
    romance = genre_profile({"genre": "言情"})
    joined = "".join(
        "".join(batch_slot_roles(romance, number, 4, 6)) for number in range(1, 5)
    )
    for word in ("相遇", "误解", "靠近", "抉择"):
        assert word in joined


def test_non_mystery_profiles_have_no_investigation_bias():
    for key, profile in PROFILES.items():
        text = profile.guidance + "".join(profile.event_palette) + "".join(
            "".join(roles) for roles in profile.slot_roles.values()
        ) + "".join(profile.phase_focus.values()) + profile.batch_pressure
        for word in BOOK_SPECIFIC:
            assert word not in text, (key, word)
        if key != "mystery":
            for word in INVESTIGATION_BIASED:
                assert word not in text, (key, word)


def test_prompt_constants_have_no_book_specific_nouns():
    constants = {**_prompt_constants(planning_module), **_prompt_constants(prompts_module)}
    assert constants
    for name, text in constants.items():
        for word in BOOK_SPECIFIC:
            assert word not in text, (name, word)
    planning_prompts = dict(_prompt_constants(planning_module))
    planning_prompts.update(
        {name: getattr(prompts_module, name) for name in PLANNING_PROMPT_NAMES_IN_PROMPTS}
    )
    for name, text in planning_prompts.items():
        for word in INVESTIGATION_BIASED:
            assert word not in text, (name, word)


def test_planning_prompt_builders_have_no_book_specific_text():
    for relative in (
        "app/services/director_planning.py",
        "app/api/routers/planning.py",
        "app/narrative_policy.py",
        "app/genre_profiles.py",
        "app/planning.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for word in BOOK_SPECIFIC + ("调查", "证物", "战国官署", "本卷十章"):
            assert word not in source, (relative, word)


def test_route_guidance_includes_genre_and_custom_structure_wins():
    project = {"genre": "悬疑", "narrative": {"structure_profile": "adaptive"}}
    job, _, _ = route_guidance(project, False)
    assert "悬疑/推理" in job and "线索" in job
    custom = {
        "genre": "言情",
        "narrative": {"structure_profile": "custom", "custom_structure": "每卷以书信往来推进"},
    }
    text = planning_guidance(custom)
    assert text.index("每卷以书信往来推进") < text.index("言情/关系")
    job, _, _ = route_guidance(custom, True)
    assert job.count("每卷以书信往来推进") == 1


def test_batch_size_local_vs_cloud_and_balanced_batches():
    assert volume_batch_size({"base_url": "http://127.0.0.1:8080/v1"}) == 3
    assert volume_batch_size({"base_url": "http://192.168.1.20:8000/v1"}) == 3
    assert volume_batch_size({"base_url": "https://api.deepseek.com/v1"}) == 6
    assert volume_batches(1, 7, 6) == [(1, 4), (5, 7)]
    assert volume_batches(1, 12, 6) == [(1, 6), (7, 12)]
    assert volume_batches(1, 10, 3) == [(1, 3), (4, 6), (7, 8), (9, 10)]
    assert all(
        end - start + 1 >= 2 for start, end in volume_batches(1, 13, 6)
    )


def test_period_language_gate_skips_invented_fantasy_worlds():
    assert is_period_setting({"genre": "历史穿越·战国"})
    assert is_period_setting({"genre": "架空历史"})
    assert not is_period_setting({"genre": "架空玄幻"})
    assert not is_period_setting({"genre": "都市言情"})


_POOL = "".join(chr(0x4E00 + 37 * index) for index in range(900))


def _distinct(number: int, offset: int, length: int) -> str:
    start = (number * 5 + offset) * 13
    return _POOL[start : start + length]


def _route(number: int) -> dict:
    return {
        "number": number,
        "title": _distinct(number, 0, 5),
        "goal": _distinct(number, 1, 30),
        "conflict": _distinct(number, 2, 30),
        "turning_point": _distinct(number, 3, 24),
        "ending_hook": _distinct(number, 4, 24),
        "must_keep": ["保持人物关系"],
        "must_avoid": ["不提前收束"],
    }


def _project(base_url: str) -> dict:
    return ensure_project_defaults(
        {
            "title": "测试书",
            "genre": "历史穿越·战国",
            "premise": "一名现代学者穿越到战国，在诸侯之间周旋。",
            "settings": {"base_url": base_url, "model": "deepseek-flash"},
            "planning": {
                "volumes": [
                    {
                        "id": "v1",
                        "number": 1,
                        "title": "入局",
                        "chapter_start": 1,
                        "chapter_end": 7,
                        "goal": "在列国间站稳脚跟",
                        "ending_state": "受邀入幕",
                        "turning_points": ["初次廷辩", "盟友倒戈"],
                    }
                ]
            },
        }
    )


@pytest.mark.parametrize(
    ("base_url", "expected_batches"),
    [("https://api.deepseek.com/v1", [[1, 2, 3, 4], [5, 6, 7]]),
     ("http://127.0.0.1:8080/v1", [[1, 2, 3], [4, 5], [6, 7]])],
)
def test_volume_planning_route_with_mocked_completion(monkeypatch, base_url, expected_batches):
    calls: list[dict] = []

    async def fake(settings, messages, **kwargs):
        user = messages[-1]["content"]
        numbers = [
            number for number in range(1, 8)
            if f"- 第 {number} 章：" in user
        ]
        calls.append({"numbers": numbers, "user": user, "kwargs": kwargs})
        result = {"volume_id": "v1", "chapters": [_route(number) for number in numbers]}
        validate = kwargs.get("validate")
        if validate:
            validate(result)
        return result, []

    monkeypatch.setattr(main, "run_structured_completion", fake)
    with TestClient(main.app) as client:
        response = client.post(
            "/api/planning/volume",
            json={"project": _project(base_url), "volume_id": "v1"},
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["complete"] is True and payload["fallback"] is False
    assert [route["number"] for route in payload["chapters"]] == list(range(1, 8))
    assert [call["numbers"] for call in calls] == expected_batches
    profile = PROFILES["history_transmigration"]
    first = calls[0]["user"]
    assert "穿越/历史" in first
    assert profile.phase_focus["opening"] in first
    final = calls[-1]["user"]
    assert profile.slot_roles["finale"][-1] in final
    assert "这是最终批次" in final
    for call in calls:
        for word in BOOK_SPECIFIC + ("调查", "证据"):
            assert word not in call["user"], word
        assert call["kwargs"]["max_tokens"] >= 2100

"""The master plan's outline is expanded in plain text instead of failing the plan."""
import asyncio

from app.planning.outline_expansion import expand_full_outline


def _run(core, reply, minimum=1800):
    seen = {}

    async def chat(settings, messages, **kwargs):
        seen["messages"], seen["kwargs"] = messages, kwargs
        return reply

    text, warnings = asyncio.run(expand_full_outline(
        {"base_url": "https://api.deepseek.com"}, "【作品】书同文", core,
        target_chars=2600, minimum_chars=minimum, chat=chat,
    ))
    return text, warnings, seen


def test_long_enough_outline_is_kept_without_a_call():
    text, warnings, seen = _run({"full_outline": "卷" * 2000}, "unused")
    assert len(text) == 2000 and warnings == [] and not seen


def test_short_outline_is_expanded_with_room_to_write():
    text, warnings, seen = _run({"full_outline": "很短的总纲", "theme": "秩序"}, "卷一「客」：" + "事" * 2400)
    assert len(text) > 2000
    assert seen["kwargs"]["max_tokens"] >= 2600
    assert "秩序" in seen["messages"][1]["content"]
    assert warnings and "单独扩写" in warnings[0]


def test_still_short_answer_keeps_the_longer_text_and_warns():
    text, warnings, _ = _run({"full_outline": "短" * 500}, "更短")
    assert text == "短" * 500
    assert any("手动补充" in item for item in warnings)

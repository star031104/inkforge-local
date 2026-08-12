from app.style_engine import (
    render_fingerprint,
    select_exemplars,
    split_exemplars,
    style_stats,
)


def test_style_stats_and_fingerprint():
    text = "“你来了？”她问。\n\n雨落在窗外。屋里很静。"
    stats = style_stats(text)
    assert stats.sentence_length > 0
    assert stats.dialogue_ratio > 0
    assert "平均句长" in render_fingerprint(text * 10)


def test_select_dialogue_exemplar_for_dialogue_scene():
    narration = "山路向北延伸，雾气压住远处的树林。" * 40
    dialogue = "“你确定吗？”林墨问。\n“我亲眼看见的。”苏禾说。\n" * 25
    sample = narration + "\n\n" + dialogue
    chunks = split_exemplars(sample)
    selected = select_exemplars(sample, "林墨与苏禾争论", dialogue, 1)
    assert chunks
    assert selected
    assert "林墨问" in selected[0] or "苏禾说" in selected[0]


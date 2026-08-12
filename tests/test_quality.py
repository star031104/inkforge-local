from app.quality import local_quality_check


def test_quality_detects_meta_and_duplicate_paragraph():
    paragraph = "林墨推开门，看见走廊尽头的灯忽然熄灭。" * 3
    draft = f"以下是续写内容：\n{paragraph}\n{paragraph}"
    result = local_quality_check(draft, target_words=100)
    categories = {item["category"] for item in result["issues"]}
    assert "元话语" in categories
    assert "重复段落" in categories
    assert result["verdict"] == "revise"


def test_quality_detects_repeated_source_ending():
    source = "他终于推开了那扇门。"
    draft = "他终于推开了那扇门。门后站着一个陌生人。" * 10
    result = local_quality_check(draft, source, target_words=200)
    assert any(item["category"] == "重复原文" for item in result["issues"])


def test_quality_detects_copied_long_sentence_from_source_middle():
    repeated = "他把三册竹简依次摊开，墨迹在斜照的日光里泛着褐色。"
    source = f"开场不同。{repeated}随后众人离开。"
    draft = f"另一场景开始。{repeated}但这一次无人说话。" * 3
    result = local_quality_check(draft, source, target_words=80)
    assert any(item["category"] == "复用已有描写" for item in result["issues"])


def test_quality_flags_dense_ai_cliches():
    draft = "空气中弥漫着旧纸气味，他的呼吸微微一滞，仿佛在诉说某种秘密。" * 8
    result = local_quality_check(draft, target_words=150)
    assert any(item["category"] == "AI套话" for item in result["issues"])


def test_quality_flags_unprovided_past_fact():
    result = local_quality_check(
        "那把伞是半年前丢掉的，本该躺在垃圾桶里。",
        "她看见门外有一把黑伞。",
        target_words=30,
    )
    assert any(item["category"] == "疑似新增往事" for item in result["issues"])
    assert result["verdict"] == "revise"


def test_quality_flags_invented_memory_wording():
    result = local_quality_check(
        "她记得这把伞明明还在阳台，没有带出去过。",
        "门外放着一把黑伞。",
        target_words=20,
    )
    assert any(item["category"] == "疑似新增往事" for item in result["issues"])


def test_quality_flags_truncation_unclosed_quotes_and_non_prose_format():
    draft = "# 本章说明\n“他推开门，看见灯光落在桌面，"
    result = local_quality_check(draft, target_words=20)
    categories = {item["category"] for item in result["issues"]}
    assert {"非正文格式", "标点未闭合", "疑似截断"} <= categories

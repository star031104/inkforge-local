from fastapi.testclient import TestClient

from app.db import default_project
from app.main import app, quality_source_tail
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


def test_quality_detects_two_distinct_repeated_short_sentences():
    draft = (
        "王绾把抽页压在案角。秦策没有伸手去取。"
        "王绾把抽页压在案角。秦策没有伸手去取。"
        "窗外的雨声渐渐逼近，两人仍隔案相望。"
    ) * 4
    result = local_quality_check(draft, target_words=80)
    assert any(item["category"] == "短句循环" for item in result["issues"])
    assert result["verdict"] == "revise"


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


def test_quality_does_not_flag_past_fact_read_from_current_scene_evidence():
    result = local_quality_check(
        "林夏擦去门边的灰，铭牌上写着：钟室三年前停用。她没有立刻下结论。",
        "林夏走到钟室门前。",
        target_words=25,
    )
    assert not any(item["category"] == "疑似新增往事" for item in result["issues"])


def test_quality_downgrades_uncertain_past_hypothesis_instead_of_blocking():
    result = local_quality_check(
        "她看着锁上的新划痕，猜测也许半年前就有人动过这里，但没有证据。",
        "她看见锁上有一道划痕。",
        target_words=28,
    )
    assert any(item["category"] == "待确认新设定" for item in result["issues"])
    assert not any(item["category"] == "疑似新增往事" for item in result["issues"])


def test_quality_accepts_past_fact_already_in_authoritative_context():
    result = local_quality_check(
        "钟室三年前停用，此后一直封着。",
        "她推开值班室的窗。",
        target_words=16,
        known_context="白塔钟室于三年前停用，此后按规定封闭。",
    )
    assert not any(item["category"] in {"疑似新增往事", "待确认新设定"} for item in result["issues"])


def test_quality_still_blocks_unsupported_personal_backstory():
    result = local_quality_check(
        "林夏半年前来过这里，还和周衡约定不再登塔。",
        "林夏第一次走到白塔门前。",
        target_words=22,
    )
    assert any(item["category"] == "疑似新增往事" and item["severity"] == "medium" for item in result["issues"])
    assert result["verdict"] == "revise"


def test_quality_does_not_treat_bengai_or_yuanlai_as_backstory_by_itself():
    result = local_quality_check(
        "本该紧闭的门留着一道缝。林夏伸手一推，原来锁舌没有扣上。",
        "林夏走到门前。",
        target_words=22,
    )
    assert not any(item["category"] in {"疑似新增往事", "待确认新设定"} for item in result["issues"])


def test_quality_endpoint_does_not_compare_a_saved_candidate_with_itself():
    project = default_project("quality-self", "质量自比较", "now")
    chapter = project["chapters"][0]
    draft = (
        "沈砚把三册木牍依次摆在案上，先请仓吏核对封泥。"
        "他没有提出结论，只把同旬数字逐项记在另一片木牍上。"
        "仓吏复核后允许他次日继续查验第三册。"
    ) * 8
    chapter["content"] = draft
    assert quality_source_tail(chapter["content"], draft) == ""
    response = TestClient(app).post(
        "/api/chapter/quality",
        json={"project": project, "chapter_id": chapter["id"], "draft": draft},
    )
    assert response.status_code == 200
    assert not any(
        issue["category"] in {"复用已有描写", "重复原文"}
        for issue in response.json()["issues"]
    )


def test_quality_endpoint_still_compares_a_real_revision_to_prior_prose():
    prior = "他把三册竹简依次摊开，墨迹在斜照的日光里泛着褐色。"
    draft = ("另一场景开始。" + prior + "但这一次无人说话。") * 3
    assert quality_source_tail(prior, draft) == prior


def test_quality_source_tail_ignores_a_light_full_chapter_copy_edit():
    prior = (
        "沈砚把旧案逐条写入官署版本，又命书吏核验封泥与日期。"
        "众人复核三遍，才把暂行办法送往廷尉府存档。"
    ) * 12
    draft = prior.replace("版本", "定本").replace("办法", "规程")
    assert quality_source_tail(prior, draft) == ""


def test_quality_source_tail_keeps_a_short_copied_excerpt():
    repeated = "沈砚把三册竹简依次摊开，逐字核验墨迹、封泥和日期。"
    prior = repeated * 20
    draft = repeated * 3
    assert quality_source_tail(prior, draft) == prior

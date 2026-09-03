import json

from fastapi.testclient import TestClient

from app.db import default_project
from app.main import app
from app.prompts import build_prompt


def _events(response):
    result=[]
    for block in response.text.split("\n\n"):
        line=next((x for x in block.splitlines() if x.startswith("data:")),"")
        if line:
            result.append(json.loads(line[5:].strip()))
    return result


def test_generate_auto_continues_short_prose(monkeypatch):
    calls=[]
    async def fake_stream(settings, messages):
        calls.append((dict(settings), list(messages)))
        text=("林夏沿着白塔旋梯向上，鞋底擦过潮湿石阶。"*5) if len(calls)==1 else "".join(
            f"她走过第{i}段石阶，先看栏杆上的水迹，再听钟室门后的机械声。" for i in range(1,27)
        )
        for i in range(0,len(text),17):
            yield text[i:i+17]
    monkeypatch.setattr("app.main.chat_stream", fake_stream)
    project=default_project("gen-short","生成长度测试","now")
    project["settings"]["target_words"]=900
    chapter=project["chapters"][0]
    client=TestClient(app)
    response=client.post("/api/generate",json={
        "project":project,"chapter_id":chapter["id"],"mode":"instruction",
        "instruction":"写林夏登上钟室。","selection":"","target_words":900,
    })
    assert response.status_code==200
    events=_events(response)
    tokens="".join(e.get("text","") for e in events if e.get("type")=="token")
    done=next(e for e in events if e.get("type")=="done")
    assert len(calls)==2
    assert any(e.get("type")=="repair" and e.get("reason")=="short" for e in events)
    assert done["length_repaired"] is True
    assert done["actual_chars"] >= 650
    assert len(tokens.replace("\n","")) >= 650
    assert calls[1][1][-2]["role"]=="assistant"
    assert "不要重写" in calls[1][1][-1]["content"]
    assert "接续锚点" in calls[1][1][-1]["content"]


def test_generate_does_not_repeat_call_when_long_enough(monkeypatch):
    calls=0
    async def fake_stream(settings, messages):
        nonlocal calls
        calls+=1
        text="林夏站在钟室里检查齿轮与绳索，逐一确认声音的来源。"*30
        for i in range(0,len(text),31):
            yield text[i:i+31]
    monkeypatch.setattr("app.main.chat_stream", fake_stream)
    project=default_project("gen-ok","生成长度测试","now")
    chapter=project["chapters"][0]
    response=TestClient(app).post("/api/generate",json={
        "project":project,"chapter_id":chapter["id"],"mode":"instruction",
        "instruction":"写钟室检查。","selection":"","target_words":600,
    })
    events=_events(response)
    assert calls==1
    assert not any(e.get("type")=="repair" for e in events)
    assert next(e for e in events if e.get("type")=="done")["actual_chars"] >= 430


def test_rewrite_uses_selection_scale_not_global_chapter_target():
    project=default_project("rewrite-scale","改写长度测试","now")
    project["settings"]["target_words"]=1800
    chapter=project["chapters"][0]
    selection="她停在门前，手指压住冰冷的门把，迟迟没有推门。"*3
    build=build_prompt(project,{
        "project":project,"chapter_id":chapter["id"],"mode":"rewrite",
        "instruction":"改得更克制。","selection":selection,"target_words":1800,
    })
    prompt="\n".join(x["content"] for x in build.messages)
    assert "目标长度：约 1800 个中文字符" not in prompt
    assert "目标长度：约" in prompt


def test_director_full_chapter_rewrite_keeps_configured_chapter_target():
    project=default_project("rewrite-full","整章改写长度测试","now")
    chapter=project["chapters"][0]
    selection="偏短的整章初稿。"*40
    build=build_prompt(project,{
        "project":project,"chapter_id":chapter["id"],"mode":"rewrite",
        "instruction":"按审校意见完整修订。","selection":selection,
        "target_words":1900,"_full_chapter_rewrite":True,
    })
    prompt="\n".join(x["content"] for x in build.messages)
    assert "目标长度：约 1900 个中文字符" in prompt
    assert "建议落在 1520-2280 字" in prompt


def test_default_cloud_output_ceiling_is_raised_for_new_projects():
    project=default_project("defaults","默认值","now")
    assert project["settings"]["max_tokens"]==3500


def test_generate_can_use_second_repair_pass_when_first_repair_is_still_short(monkeypatch):
    calls=[]
    async def fake_stream(settings, messages):
        calls.append((dict(settings), list(messages)))
        if len(calls) == 1:
            text = "林夏踏上旋梯，雨水从鞋沿滴下来。" * 5
        elif len(calls) == 2:
            text = "".join(f"她走到第{i}级转角，只记录台阶上的水迹。" for i in range(1,9))
        else:
            text = "".join(f"钟室门前第{i}处痕迹不同，她依次核对锁舌、锈屑和积水，没有急着下结论。" for i in range(1,21))
        for i in range(0, len(text), 19):
            yield text[i:i+19]
    monkeypatch.setattr("app.main.chat_stream", fake_stream)
    project=default_project("gen-two-repairs","二次补足测试","now")
    chapter=project["chapters"][0]
    response=TestClient(app).post("/api/generate",json={
        "project":project,"chapter_id":chapter["id"],"mode":"instruction",
        "instruction":"写林夏登上钟室。","selection":"","target_words":900,
    })
    events=_events(response)
    done=next(e for e in events if e.get("type")=="done")
    repairs=[e for e in events if e.get("type")=="repair"]
    assert len(calls)==3
    assert [e.get("pass") for e in repairs]==[1,2]
    assert done["length_repaired"] is True
    assert done["actual_chars"] >= int(900*0.75)


def test_repair_continuation_strips_echoed_suffix_and_repeated_block():
    from app.main import _clean_repair_continuation

    ending = "沈砚收起两册，转身望向院外。甲士脚步声渐远，雨声依旧。他知明日第三册必开。"
    continuation = ending + ending + "樊吏却在门外停住脚步，回身要求重新核验封泥。"
    cleaned = _clean_repair_continuation(ending, continuation)
    assert cleaned.count("沈砚收起两册") == 0
    assert cleaned.count("樊吏却在门外停住脚步") == 1


def test_repair_continuation_collapses_internal_sentence_block_loop():
    from app.main import _clean_repair_continuation

    block = "甲士脚步声渐远，廊下只剩雨水敲击瓦当的回声。沈砚没有回头，只把两册粮牍压在案角。樊吏也没有说话，目光仍停在第三册封泥上。"
    cleaned = _clean_repair_continuation("前文已经结束。", block + block + block + "他重新摊开第三册。")
    assert cleaned.count("甲士脚步声渐远") == 1
    assert cleaned.count("他重新摊开第三册") == 1


def test_long_scene_loop_and_unsupported_recollection_are_removed():
    from app.main import (
        _dedupe_adjacent_sentence_blocks,
        _dedupe_exact_paragraphs,
        _paragraphize_prose,
        _remove_orphan_chinese_quotes,
        _strip_unsupported_recollections,
    )

    block = "".join(f"秦策完成第{i}项眼前核验。" for i in range(1, 17))
    cleaned = _dedupe_adjacent_sentence_blocks(block + block + "王绾准他开仓一次。")
    assert cleaned.count("秦策完成第1项眼前核验") == 1
    assert "王绾准他开仓一次" in cleaned

    draft = "秦策想起昨日巡仓时听见密语。秦策依据眼前封泥记录细痕。"
    filtered = _strip_unsupported_recollections(draft, "本章只确认封泥细痕。")
    assert "昨日巡仓" not in filtered
    assert "眼前封泥" in filtered
    assert _remove_orphan_chinese_quotes("”孤立闭引号。") == "孤立闭引号。"
    formatted = _paragraphize_prose("秦策核验封泥。" * 100, max_chars=120)
    assert "\n\n" in formatted
    assert all(len(item) <= 130 for item in formatted.split("\n\n"))
    deduped = _dedupe_exact_paragraphs("甲段有足够长度用于判断整段是否发生完全重复。\n\n乙段。\n\n甲段有足够长度用于判断整段是否发生完全重复。")
    assert deduped.count("甲段有足够长度") == 1


def test_generate_repair_does_not_reappend_existing_ending(monkeypatch):
    calls = 0
    ending = "沈砚收起两册，转身望向院外。甲士脚步声渐远，雨声依旧。他知明日第三册必开。"

    async def fake_stream(settings, messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            text = "雨水沿着瓦当落下。沈砚核对两册粮牍，确认同旬存在差额。" + ending
        else:
            text = ending + ending + "".join(
                f"樊吏在第{i}次核验时改查不同封泥痕迹，沈砚只记录可验证结果。"
                for i in range(1, 18)
            )
        for i in range(0, len(text), 23):
            yield text[i:i+23]

    monkeypatch.setattr("app.main.chat_stream", fake_stream)
    project = default_project("gen-overlap", "续补去重测试", "now")
    chapter = project["chapters"][0]
    response = TestClient(app).post(
        "/api/generate",
        json={
            "project": project,
            "chapter_id": chapter["id"],
            "mode": "instruction",
            "instruction": "写仓曹核账。",
            "selection": "",
            "target_words": 650,
        },
    )
    events = _events(response)
    text = "".join(e.get("text", "") for e in events if e.get("type") == "token")
    assert text.count("沈砚收起两册") == 1
    assert "樊吏在第1次核验时" in text

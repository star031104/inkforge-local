from app.llama_client import complete_json_prefix


def test_complete_json_prefix_stops_after_balanced_object():
    text = '前言 {"a":{"quoted":"右花括号 } 不结束"},"items":[1,2]}   后续空白'
    assert complete_json_prefix(text) == (
        '{"a":{"quoted":"右花括号 } 不结束"},"items":[1,2]}'
    )


def test_complete_json_prefix_waits_for_closing_brace():
    assert complete_json_prefix('{"a":{"b":1}') is None


def test_complete_json_prefix_handles_escaped_quotes():
    assert complete_json_prefix('{"text":"他说：\\"继续\\""} trailing') == (
        '{"text":"他说：\\"继续\\""}'
    )


def test_complete_json_prefix_skips_invalid_braced_preamble():
    text = "先考虑 {这不是JSON} 然后输出 " + '{"goal":"完成核账"}'
    assert complete_json_prefix(text) == '{"goal":"完成核账"}'

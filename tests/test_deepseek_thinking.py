from app.providers import build_chat_payload, guess_server


def test_deepseek_official_api_gets_explicit_thinking_switch():
    settings = {"base_url": "https://api.deepseek.com", "model": "deepseek-flash", "enable_thinking": False}
    assert guess_server(settings["base_url"]) == "deepseek"
    payload = build_chat_payload(settings, [{"role": "user", "content": "hi"}], stream=False)
    assert payload["thinking"] == {"type": "disabled"}
    payload = build_chat_payload({**settings, "enable_thinking": True}, [{"role": "user", "content": "hi"}], stream=False)
    assert payload["thinking"] == {"type": "enabled"}

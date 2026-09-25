import asyncio
import json

import httpx
import pytest

from app import llama_client, model_telemetry


@pytest.fixture
def transport(tmp_path, monkeypatch):
    monkeypatch.setattr(model_telemetry, "_path", None)
    model_telemetry.configure(tmp_path / "calls.db")
    original = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(llama_client.httpx, "AsyncClient", lambda **kwargs: original(
            **kwargs, transport=httpx.MockTransport(handler)))
    return install


def test_nonstream_retry_usage_and_finish_reason(transport):
    requests = []

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, headers={"retry-after": "0"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "雨停了。"}, "finish_reason": "stop"}],
                                       "usage": {"prompt_tokens": 30, "completion_tokens": 4}})
    transport(handler)
    assert asyncio.run(llama_client.chat_once({"model": "test"}, [{"role": "user", "content": "写一句"}])) == "雨停了。"
    call = model_telemetry.report()["calls"][0]
    assert call["attempts"] == 2 and call["prompt_tokens"] == 30
    assert call["finish_reason"] == "stop" and call["status"] == "completed"


def test_stream_usage_chunk_without_choices(transport):
    events = [{"choices": [{"delta": {"content": "雨停了。"}}]},
              {"choices": [{"delta": {}, "finish_reason": "length"}]},
              {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 5}}]
    wire = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
    transport(lambda request: httpx.Response(200, text=wire))

    async def consume():
        return "".join([piece async for piece in llama_client.chat_stream({"model": "test"}, [])])
    assert asyncio.run(consume()) == "雨停了。"
    call = model_telemetry.report()["calls"][0]
    assert call["finish_reason"] == "length" and call["completion_tokens"] == 5


def test_partial_stream_never_retries_or_duplicates(transport):
    requests = []

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'
            raise httpx.ReadError("interrupted")

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=BrokenStream())
    transport(handler)
    pieces = []

    async def consume():
        async for piece in llama_client.chat_stream({"model": "test"}, []):
            pieces.append(piece)
    with pytest.raises(httpx.ReadError):
        asyncio.run(consume())
    assert pieces == ["first"] and len(requests) == 1
    assert model_telemetry.report()["calls"][0]["status"] == "failed"


def test_final_budget_guard_rejects_before_network(transport):
    def forbidden(request):
        pytest.fail("over-budget input reached network")
    transport(forbidden)
    with pytest.raises(ValueError, match="上下文预算不足"):
        asyncio.run(llama_client.chat_once({"context_budget": 1200}, [{"content": "雨" * 2000}], max_tokens=256))
    assert model_telemetry.report()["calls"] == []

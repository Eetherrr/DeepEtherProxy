"""`/alpha/generate` 客户端：流解析、请求头与错误路径。"""

import json

import httpx
import pytest

from deepether.commandcode.client import CommandCodeClient
from deepether.commandcode.protocol import build_envelope
from deepether.errors import DeepEtherError, ErrorCode


class ChunkedStream(httpx.AsyncByteStream):
    """按给定字节块逐块产出，用来测试跨 chunk 的半行。"""

    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk


class RecordingTransport(httpx.AsyncBaseTransport):
    """记录请求并返回预置响应。"""

    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return self.handler(request)


class SilentLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


def make_client(transport, key="user_test_key"):
    return CommandCodeClient(
        api_key_provider=lambda: key,
        logger=SilentLogger(),
        transport=transport,
    )


def lines(*events):
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events).encode("utf-8")


def envelope():
    return build_envelope(
        model="deepseek/deepseek-v4-flash",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        system="s",
        max_tokens=64,
        thread_id="t",
        working_dir=".",
        environment="win32-x64",
    )


@pytest.mark.asyncio
async def test_stream_is_parsed_across_chunks():
    payload = lines(
        {"type": "start"},
        {"type": "reasoning-delta", "text": "想"},
        {"type": "reasoning-delta", "text": "一下"},
        {"type": "text-delta", "text": "你"},
        {"type": "text-delta", "text": "好"},
        {"type": "tool-call", "toolCallId": "call_1", "toolName": "reply", "input": {"text": "hi"}},
        {
            "type": "finish-step",
            "finishReason": "tool-calls",
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        },
        {
            "type": "finish",
            "finishReason": "tool-calls",
            "totalUsage": {
                "inputTokens": 10,
                "inputTokenDetails": {"cacheReadTokens": 4},
                "outputTokens": 5,
                "totalTokens": 15,
            },
        },
        {"type": "provider-metadata", "providerMetadata": {"provider": "novita"}},
    )
    chunks = [payload[:7], payload[7:40], payload[40:]]
    transport = RecordingTransport(lambda request: httpx.Response(200, stream=ChunkedStream(chunks)))
    client = make_client(transport)
    try:
        result = await client.generate(envelope())
    finally:
        await client.aclose()

    assert result.content == "你好"
    assert result.reasoning == "想一下"
    assert result.tool_calls[0].call_id == "call_1"
    assert result.tool_calls[0].arguments == {"text": "hi"}
    assert result.usage.input_tokens == 10
    assert result.usage.cache_read_tokens == 4
    assert result.finish_reason == "tool-calls"
    assert result.provider_metadata == {"provider": "novita"}
    assert result.events["text-delta"] == 2


@pytest.mark.asyncio
async def test_request_headers_and_body_match_cli_shape():
    seen = {}

    def handler(request):
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, stream=ChunkedStream([lines({"type": "finish", "finishReason": "stop"})]))

    client = make_client(RecordingTransport(handler))
    try:
        await client.generate(envelope())
    finally:
        await client.aclose()

    headers = {key.lower(): value for key, value in seen["headers"].items()}
    assert headers["authorization"] == "Bearer user_test_key"
    assert headers["user-agent"] == "cli"
    assert headers["x-command-code-version"] == "1.88.0"
    assert headers["x-cli-environment"] == "production"
    assert headers["x-project-slug"] == "maibot"
    assert headers["x-taste-learning"] == "false"
    assert "x-session-id" in headers
    body = seen["body"]
    assert body["params"]["model"] == "deepseek/deepseek-v4-flash"
    assert body["threadId"] == "t"
    assert body["memory"] == ""


@pytest.mark.asyncio
async def test_http_403_model_not_in_plan_is_classified():
    body = json.dumps(
        {
            "success": False,
            "error": {
                "code": "FORBIDDEN",
                "status": 403,
                "message": "MODEL_NOT_IN_PLAN: Claude Sonnet 5.5 available in GOAT",
            },
        }
    )
    client = make_client(RecordingTransport(lambda request: httpx.Response(403, text=body)))
    try:
        with pytest.raises(DeepEtherError) as error:
            await client.generate(envelope())
    finally:
        await client.aclose()
    assert error.value.code == ErrorCode.MODEL_NOT_ALLOWED


@pytest.mark.asyncio
async def test_in_stream_error_event_raises():
    payload = lines(
        {"type": "start"},
        {"type": "error", "error": {"message": "premium_credits_exhausted", "type": "insufficient_credits"}},
    )
    client = make_client(RecordingTransport(lambda request: httpx.Response(200, stream=ChunkedStream([payload]))))
    try:
        with pytest.raises(DeepEtherError) as error:
            await client.generate(envelope())
    finally:
        await client.aclose()
    assert error.value.code == ErrorCode.BUDGET_EXHAUSTED


@pytest.mark.asyncio
async def test_missing_finish_event_is_protocol_error():
    payload = lines({"type": "text-delta", "text": "半截"})
    client = make_client(RecordingTransport(lambda request: httpx.Response(200, stream=ChunkedStream([payload]))))
    try:
        with pytest.raises(DeepEtherError) as error:
            await client.generate(envelope())
    finally:
        await client.aclose()
    assert error.value.code == ErrorCode.PROTOCOL_ERROR


@pytest.mark.asyncio
async def test_broken_tool_arguments_are_marked_truncated():
    payload = lines(
        {"type": "tool-call", "toolCallId": "c1", "toolName": "reply", "input": '{"text"'},
        {"type": "finish", "finishReason": "tool-calls", "totalUsage": {"inputTokens": 1, "outputTokens": 1}},
    )
    client = make_client(RecordingTransport(lambda request: httpx.Response(200, stream=ChunkedStream([payload]))))
    try:
        result = await client.generate(envelope())
    finally:
        await client.aclose()
    assert result.tool_calls[0].truncated is True
    assert result.tool_calls[0].arguments == {}


@pytest.mark.asyncio
async def test_read_timeout_maps_to_upstream_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = make_client(RecordingTransport(handler))
    try:
        with pytest.raises(DeepEtherError) as error:
            await client.generate(envelope())
    finally:
        await client.aclose()
    assert error.value.code == ErrorCode.UPSTREAM_TIMEOUT
    assert error.value.retryable is True


@pytest.mark.asyncio
async def test_get_json_reads_billing():
    client = make_client(
        RecordingTransport(lambda request: httpx.Response(200, json={"credits": {"monthlyCredits": 4.4}}))
    )
    try:
        payload = await client.get_json("/alpha/billing/credits")
    finally:
        await client.aclose()
    assert payload["credits"]["monthlyCredits"] == 4.4

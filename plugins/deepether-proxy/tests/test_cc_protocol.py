"""CommandCode 协议层：信封、NDJSON、用量与错误分类。"""

import json

import pytest

from deepether.commandcode.constants import MAX_EVENT_LINE_BYTES
from deepether.commandcode.protocol import (
    CcResult,
    CcToolCall,
    CcUsage,
    NdjsonSplitter,
    build_envelope,
    clean_surrogates,
    map_http_error,
    map_stream_error,
    normalize_finish_reason,
    usage_from_event,
)
from deepether.errors import DeepEtherError, ErrorCode


def _feed_all(chunks):
    splitter = NdjsonSplitter()
    events = []
    for chunk in chunks:
        events.extend(splitter.feed(chunk))
    events.extend(splitter.close())
    return events


def test_envelope_has_all_required_fields():
    envelope = build_envelope(
        model="deepseek/deepseek-v4-flash",
        messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        system="你是助手",
        max_tokens=512,
        thread_id="thread-1",
        working_dir="C:/tmp",
        environment="win32-x64",
        today="2026-10-27",
        temperature=0.7,
        reasoning_effort="high",
    )
    config = envelope["config"]
    assert config["workingDir"] == "C:/tmp"
    assert config["date"] == "2026-10-27"
    assert config["environment"] == "win32-x64"
    assert config["structure"] == []
    assert config["isGitRepo"] is False
    assert envelope["memory"] == ""
    assert envelope["permissionMode"] == "standard"
    assert envelope["threadId"] == "thread-1"
    params = envelope["params"]
    assert params["model"] == "deepseek/deepseek-v4-flash"
    assert params["system"] == "你是助手"
    assert params["max_tokens"] == 512
    assert params["stream"] is True
    assert params["temperature"] == 0.7
    assert params["reasoning_effort"] == "high"
    assert "tools" not in params


def test_envelope_clamps_max_tokens_and_includes_tools():
    envelope = build_envelope(
        model="m",
        messages=[],
        system="",
        max_tokens=999999,
        thread_id="t",
        working_dir=".",
        environment="linux-x64",
        tools=[{"name": "reply", "description": "", "input_schema": {"type": "object"}}],
    )
    assert envelope["params"]["max_tokens"] == 64000
    assert envelope["params"]["tools"][0]["name"] == "reply"
    assert "temperature" not in envelope["params"]


def test_ndjson_splitter_handles_split_chunks_crlf_and_trailing_line():
    payload = (
        b'{"type":"start"}\r\n'
        b"\r\n"
        b'{"type":"text-delta","text":"\xe4\xbd\xa0"}\n'
        b'{"type":"finish","totalUsage":{"inputTokens":3,"outputTokens":2}}'
    )
    events = _feed_all([payload[:10], payload[10:35], payload[35:]])
    assert [event["type"] for event in events] == ["start", "text-delta", "finish"]
    assert events[1]["text"] == "你"


def test_ndjson_splitter_tolerates_data_prefix():
    events = _feed_all([b'data: {"type":"start"}\n'])
    assert events == [{"type": "start"}]


def test_ndjson_splitter_rejects_invalid_json():
    splitter = NdjsonSplitter()
    with pytest.raises(DeepEtherError) as error:
        splitter.feed(b"not json\n")
    assert error.value.code == ErrorCode.PROTOCOL_ERROR


def test_ndjson_splitter_rejects_oversized_line():
    splitter = NdjsonSplitter(max_line_bytes=1024)
    with pytest.raises(DeepEtherError):
        splitter.feed(b"x" * 2048)
    # 常量本身要能兜住正常事件（8MB）
    assert MAX_EVENT_LINE_BYTES >= 1024 * 1024


def test_usage_from_event_prefers_nested_details():
    usage = usage_from_event(
        {
            "inputTokens": 46,
            "inputTokenDetails": {"noCacheTokens": 46, "cacheReadTokens": 0},
            "outputTokens": 34,
            "outputTokenDetails": {"textTokens": 2, "reasoningTokens": 32},
            "totalTokens": 80,
            "reasoningTokens": 32,
            "cachedInputTokens": 0,
        }
    )
    assert usage.input_tokens == 46
    assert usage.output_tokens == 34
    assert usage.reasoning_tokens == 32
    assert usage.total_tokens == 80
    assert usage.to_host() == {
        "prompt_tokens": 46,
        "completion_tokens": 34,
        "total_tokens": 80,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 46,
    }


def test_usage_from_event_handles_garbage():
    usage = usage_from_event({"inputTokens": "x", "outputTokenDetails": []})
    assert usage == CcUsage()


def test_normalize_finish_reason():
    assert normalize_finish_reason("TOOL_CALLS") == "tool-calls"
    assert normalize_finish_reason(None) == ""


def test_clean_surrogates_keeps_paired_emoji_and_replaces_lone_half():
    assert clean_surrogates("正常 😀") == "正常 😀"
    assert clean_surrogates("坏\ud83d") == "坏?"


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (401, '{"error":{"message":"unauthorized"}}', ErrorCode.CREDENTIAL_INVALID),
        (
            403,
            '{"error":{"message":"Your Go plan doesn\'t include API access","code":"upgrade_required"}}',
            ErrorCode.PLAN_NOT_SUPPORTED,
        ),
        (
            403,
            '{"success":false,"error":{"code":"FORBIDDEN","message":"MODEL_NOT_IN_PLAN: Claude Sonnet 5.5"}}',
            ErrorCode.MODEL_NOT_ALLOWED,
        ),
        (429, "rate limited", ErrorCode.UPSTREAM_HTTP),
        (500, "boom", ErrorCode.UPSTREAM_HTTP),
        (
            403,
            "<!DOCTYPE html><html><body>error 1020</body></html>",
            ErrorCode.UPSTREAM_BLOCKED,
        ),
        (
            400,
            '{"error":{"message":"maximum context length exceeded"}}',
            ErrorCode.CONTEXT_TOO_LONG,
        ),
        (
            400,
            '{"error":{"message":"JSON node count cannot exceed 32768"}}',
            ErrorCode.REQUEST_TOO_LARGE,
        ),
        (
            400,
            '{"error":{"message":"premium_credits_exhausted"}}',
            ErrorCode.BUDGET_EXHAUSTED,
        ),
    ],
)
def test_map_http_error(status, body, code):
    error = map_http_error(status, body)
    assert error.code == code
    assert error.message  # 中文文案必须存在


def test_map_http_error_retryable_flags():
    assert map_http_error(429, "").retryable is True
    assert map_http_error(502, "").retryable is True
    assert map_http_error(401, "").retryable is False


def test_map_stream_error_uses_status_code_when_present():
    error = map_stream_error({"error": {"message": "MODEL_NOT_IN_PLAN: x"}, "statusCode": 403})
    assert error.code in (ErrorCode.MODEL_NOT_ALLOWED, ErrorCode.UPSTREAM_BLOCKED)


def test_result_merge_accumulates_usage_and_tool_calls():
    first = CcResult(content="A", reasoning="r1", usage=CcUsage(input_tokens=10, output_tokens=5, total_tokens=15))
    first.tool_calls = [CcToolCall(call_id="c1", name="reply", arguments={"text": "x"})]
    second = CcResult(content="B", reasoning="r2", usage=CcUsage(input_tokens=4, output_tokens=2, total_tokens=6))
    second.tool_calls = [
        CcToolCall(call_id="c1", name="reply", arguments={"text": "x"}),
        CcToolCall(call_id="c2", name="reply", arguments={"text": "y"}),
    ]
    first.merge(second)
    assert first.content == "AB"
    assert first.reasoning == "r1r2"
    assert [call.call_id for call in first.tool_calls] == ["c1", "c2"]
    assert first.usage.input_tokens == 14
    assert first.usage.output_tokens == 7
    assert first.rounds == 2


def test_result_has_payload_and_truncation():
    assert CcResult(content=" ").has_payload is False
    assert CcResult(content="x").has_payload is True
    assert CcResult(finish_reason="max-tokens").truncated is True
    assert CcResult(finish_reason="stop").truncated is False
    assert CcResult(finish_reason="stop", tool_error=True).truncated is True


def test_envelope_is_json_serializable():
    envelope = build_envelope(
        model="m",
        messages=[{"role": "user", "content": [{"type": "text", "text": "😀"}]}],
        system="s",
        max_tokens=8,
        thread_id="t",
        working_dir=".",
        environment="linux-x64",
    )
    assert json.loads(json.dumps(envelope))["params"]["model"] == "m"

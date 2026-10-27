"""CommandCode 结果 → output_items。"""

import pytest

from deepether.commandcode.adapter_out import build_response
from deepether.commandcode.protocol import CcResult, CcToolCall, CcUsage
from deepether.errors import DeepEtherError, ErrorCode


def sample_result(**kwargs):
    result = CcResult(
        content="你好",
        reasoning="想一想",
        usage=CcUsage(input_tokens=10, output_tokens=4, cache_read_tokens=2, total_tokens=14),
        finish_reason="stop",
    )
    for key, value in kwargs.items():
        setattr(result, key, value)
    return result


def test_builds_reasoning_and_assistant_items_with_usage():
    response = build_response(sample_result(), model_name="cc-deepseek-v4-flash", elapsed_ms=12)
    assert response["item_schema_version"] == 1
    items = response["output_items"]
    assert [item["item_type"] for item in items] == ["ReasoningItem", "AssistantMessageItem"]
    assert items[0]["representation"] == "raw_text"
    assert items[0]["text_parts"] == ["想一想"]
    assert items[1]["parts"] == [{"type": "text", "text": "你好"}]
    assert response["usage"] == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
        "prompt_cache_hit_tokens": 2,
        "prompt_cache_miss_tokens": 8,
    }
    assert response["raw_data"]["finish_reason"] == "stop"


def test_tool_calls_become_function_call_items():
    result = sample_result()
    result.tool_calls = [CcToolCall(call_id="call_1", name="reply", arguments={"text": "hi"})]
    response = build_response(result, model_name="m", elapsed_ms=1)
    call = response["output_items"][-1]
    assert call["item_type"] == "FunctionCallItem"
    assert call["tool_call"] == {"call_id": "call_1", "func_name": "reply", "args": {"text": "hi"}}


def test_content_only_has_no_reasoning_item():
    result = sample_result()
    result.reasoning = ""
    response = build_response(result, model_name="m", elapsed_ms=1)
    assert [item["item_type"] for item in response["output_items"]] == ["AssistantMessageItem"]


def test_tool_only_response_is_valid():
    result = CcResult(finish_reason="tool-calls")
    result.tool_calls = [CcToolCall(call_id="c1", name="reply", arguments={})]
    response = build_response(result, model_name="m", elapsed_ms=1)
    assert [item["item_type"] for item in response["output_items"]] == ["FunctionCallItem"]


def test_empty_result_raises_upstream_empty():
    with pytest.raises(DeepEtherError) as error:
        build_response(CcResult(finish_reason="stop"), model_name="m", elapsed_ms=1)
    assert error.value.code == ErrorCode.UPSTREAM_EMPTY


def test_tool_error_raises_instead_of_returning_broken_arguments():
    result = sample_result(content="")
    result.tool_error = True
    result.tool_calls = [CcToolCall(call_id="c1", name="reply", arguments={}, truncated=True)]
    with pytest.raises(DeepEtherError) as error:
        build_response(result, model_name="m", elapsed_ms=1)
    assert error.value.code == ErrorCode.PROTOCOL_ERROR
    assert error.value.retryable is True


def test_truncated_call_without_tool_error_still_raises():
    result = CcResult(content="x")
    result.tool_calls = [CcToolCall(call_id="c1", name="reply", arguments={}, truncated=True)]
    with pytest.raises(DeepEtherError):
        build_response(result, model_name="m", elapsed_ms=1)

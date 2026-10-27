"""context_items → wire messages 投影。"""

import pytest

from deepether.commandcode.adapter_in import build_prompt, normalize_tool_schema
from deepether.errors import DeepEtherError, ErrorCode


def text_part(text):
    return {"type": "text", "text": text}


def image_part(data="AAAA", image_format="png"):
    return {"type": "image", "image_format": image_format, "image_base64": data}


def system_item(text):
    return {"item_type": "SystemMessageItem", "parts": [text_part(text)]}


def user_item(*parts):
    return {"item_type": "UserMessageItem", "parts": list(parts)}


def assistant_item(text):
    return {"item_type": "AssistantMessageItem", "parts": [text_part(text)]}


def reasoning_item(*texts):
    return {"item_type": "ReasoningItem", "representation": "raw_text", "text_parts": list(texts)}


def call_item(call_id, name, args):
    return {
        "item_type": "FunctionCallItem",
        "tool_call": {"call_id": call_id, "func_name": name, "args": args},
    }


def output_item(call_id, name, output, success=True):
    return {
        "item_type": "FunctionCallOutputItem",
        "call_id": call_id,
        "tool_name": name,
        "output": output,
        "success": success,
    }


def test_system_is_separate_and_user_messages_merge():
    prompt = build_prompt(
        [system_item("规则一"), system_item("规则二"), user_item(text_part("你好")), user_item(text_part("在吗"))]
    )
    assert prompt.system == "规则一\n\n规则二"
    assert len(prompt.messages) == 1
    assert prompt.messages[0]["role"] == "user"
    assert [block["text"] for block in prompt.messages[0]["content"]] == ["你好", "在吗"]


def test_assistant_and_tool_turns_keep_order():
    prompt = build_prompt(
        [
            user_item(text_part("查一下天气")),
            reasoning_item("先调用工具"),
            call_item("call_1", "reply", {"text": "hi"}),
            output_item("call_1", "reply", "ok"),
            assistant_item("已发送"),
        ]
    )
    roles = [message["role"] for message in prompt.messages]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assistant_blocks = prompt.messages[1]["content"]
    assert assistant_blocks[0] == {"type": "reasoning", "text": "先调用工具"}
    assert assistant_blocks[1]["type"] == "tool-call"
    assert assistant_blocks[1]["toolCallId"] == "call_1"
    assert assistant_blocks[1]["input"] == {"text": "hi"}
    tool_block = prompt.messages[2]["content"][0]
    assert tool_block["type"] == "tool-result"
    assert tool_block["output"] == {"type": "text", "value": "ok"}


def test_reasoning_is_replayed_before_tool_call():
    prompt = build_prompt(
        [user_item(text_part("x")), reasoning_item("思考"), call_item("c1", "reply", {"text": "y"})]
    )
    assert prompt.messages[1]["content"][0]["type"] == "reasoning"


def test_failed_tool_output_uses_error_text_and_is_truncated():
    prompt = build_prompt(
        [user_item(text_part("x")), output_item("c1", "reply", "y" * 100, success=False)],
        max_tool_output_chars=40,
    )
    block = prompt.messages[-1]["content"][0]
    assert block["output"]["type"] == "error-text"
    assert "已截断" in block["output"]["value"]
    assert prompt.tool_outputs_truncated == 1


def test_consecutive_tool_outputs_are_merged():
    prompt = build_prompt(
        [
            user_item(text_part("x")),
            call_item("c1", "a", {}),
            call_item("c2", "b", {}),
            output_item("c1", "a", "one"),
            output_item("c2", "b", "two"),
        ]
    )
    assert [message["role"] for message in prompt.messages] == ["user", "assistant", "tool"]
    assert len(prompt.messages[-1]["content"]) == 2


def test_images_are_sent_for_vision_models():
    prompt = build_prompt([user_item(text_part("看图"), image_part("QUJD", "jpg"))], vision=True)
    blocks = prompt.messages[0]["content"]
    image = [block for block in blocks if block["type"] == "image"][0]
    assert image["mimeType"] == "image/jpeg"
    assert image["image"].startswith("data:image/jpeg;base64,")
    assert prompt.images_sent == 1


def test_images_are_stripped_for_text_models():
    prompt = build_prompt([user_item(text_part("看图"), image_part())], vision=False)
    blocks = prompt.messages[0]["content"]
    assert all(block["type"] == "text" for block in blocks)
    assert "图片已省略" in blocks[-1]["text"]
    assert prompt.images_stripped == 1


def test_vision_mode_error_rejects_images():
    with pytest.raises(DeepEtherError) as error:
        build_prompt([user_item(image_part())], vision=False, vision_mode="error")
    assert error.value.code == ErrorCode.NOT_SUPPORTED


def test_oversized_image_is_rejected_in_error_mode():
    with pytest.raises(DeepEtherError):
        build_prompt(
            [user_item(image_part("A" * 100))],
            vision=True,
            vision_mode="error",
            max_image_bytes=10,
        )


def test_refusal_part_is_rendered():
    prompt = build_prompt([user_item({"type": "refusal", "refusal": "不聊这个"})])
    assert "模型拒答：不聊这个" in prompt.messages[0]["content"][0]["text"]


def test_unknown_items_are_dropped_not_fatal():
    prompt = build_prompt(
        [
            user_item(text_part("x")),
            {"item_type": "TotallyNewItem", "parts": []},
            {"item_type": "ProviderActivityItem"},
        ]
    )
    assert prompt.dropped_items == 2
    assert prompt.unknown_types == ("TotallyNewItem",)


def test_history_is_trimmed_but_last_message_survives():
    prompt = build_prompt(
        [user_item(text_part("A" * 100)), assistant_item("B" * 100), user_item(text_part("最新"))],
        max_history_chars=100,
    )
    assert prompt.history_truncated is True
    assert prompt.messages[-1]["content"][0]["text"] == "最新"


def test_empty_context_raises():
    with pytest.raises(DeepEtherError) as error:
        build_prompt([])
    assert error.value.code == ErrorCode.PROTOCOL_ERROR


def test_tool_conversion_uses_anthropic_naming():
    prompt = build_prompt(
        [user_item(text_part("x"))],
        tool_options=[
            {
                "type": "function",
                "function": {
                    "name": "reply",
                    "description": "发送回复",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            }
        ],
    )
    assert prompt.tools == [
        {
            "name": "reply",
            "description": "发送回复",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }
    ]


def test_tool_mode_off_sends_no_tools():
    prompt = build_prompt(
        [user_item(text_part("x"))],
        tool_options=[{"type": "function", "function": {"name": "reply", "parameters": {}}}],
        tool_mode="off",
    )
    assert prompt.tools == []


def test_normalize_tool_schema_cases():
    assert normalize_tool_schema(None) == {"type": "object", "properties": {}}
    assert normalize_tool_schema({}) == {"type": "object", "properties": {}}
    assert normalize_tool_schema({"type": "object", "properties": {"a": {}}}) == {
        "type": "object",
        "properties": {"a": {}},
    }
    assert normalize_tool_schema({"properties": {"a": {}}, "required": ["a"]}) == {
        "type": "object",
        "properties": {"a": {}},
        "required": ["a"],
    }
    assert normalize_tool_schema({"$ref": "#/definitions/x"}) == {
        "type": "object",
        "properties": {},
        "additionalProperties": True,
    }


def test_response_format_is_appended_to_system():
    prompt = build_prompt(
        [system_item("你是助手"), user_item(text_part("x"))],
        response_format={"format_type": "json_object", "schema": {"type": "object"}},
    )
    assert "严格 JSON" in prompt.system
    assert prompt.system.startswith("你是助手")

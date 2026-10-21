"""上游结果 → Host 响应字典。"""

from deepether.adapter_out import (
    build_legacy_response,
    build_output_items_response,
    build_response,
    strip_web_artifacts,
)


def test_legacy_response_shape():
    result = build_legacy_response(content="你好", reasoning="先想一下", total_tokens=42)
    assert result["content"] == "你好"
    assert result["reasoning_content"] == "先想一下"
    assert result["usage"] == {"total_tokens": 42}


def test_legacy_response_handles_none():
    result = build_legacy_response(content=None, reasoning=None)
    assert result["content"] == ""
    assert result["reasoning_content"] == ""


def test_output_items_with_thinking_and_content():
    result = build_output_items_response(content="正文", reasoning="思考", total_tokens=7)
    assert result["item_schema_version"] == 1
    kinds = [item["item_type"] for item in result["output_items"]]
    assert kinds == ["ReasoningItem", "AssistantMessageItem"]
    assert result["output_items"][1]["parts"] == [{"type": "text", "text": "正文"}]


def test_output_items_only_content():
    result = build_output_items_response(content="正文", reasoning="")
    assert [item["item_type"] for item in result["output_items"]] == ["AssistantMessageItem"]


def test_output_items_only_reasoning():
    result = build_output_items_response(content="", reasoning="只想不说")
    assert [item["item_type"] for item in result["output_items"]] == ["ReasoningItem"]


def test_strip_web_artifacts_removes_search_citations():
    """联网搜索实测：正文会带 [citation:1] / [reference:0] 这类网页版内部标记。"""

    raw = "论坛今天开幕 [citation:1]。另外 DeepSeek 开源了推理框架 [reference:0][reference:1]。"
    cleaned = strip_web_artifacts(raw)
    assert "citation" not in cleaned
    assert "reference" not in cleaned
    assert cleaned == "论坛今天开幕。另外 DeepSeek 开源了推理框架。"


def test_strip_web_artifacts_keeps_normal_brackets():
    assert strip_web_artifacts("数组 [0] 和 [1] 要保留") == "数组 [0] 和 [1] 要保留"
    assert strip_web_artifacts("") == ""
    assert strip_web_artifacts(None) == ""


def test_legacy_response_strips_citations():
    result = build_legacy_response(content="新闻 [citation:3] 结束", reasoning="")
    assert result["content"] == "新闻 结束"


def test_protocol_switch():
    legacy = build_response("legacy", content="a", reasoning="b")
    modern = build_response("output_items", content="a", reasoning="b")
    assert "content" in legacy and "output_items" not in legacy
    assert "output_items" in modern and "item_schema_version" in modern

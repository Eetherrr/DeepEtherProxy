"""`context_items` → prompt 投影层。"""

from deepether.adapter_in import build_messages, render_item, style_suffix


def _text_item(item_type: str, text: str) -> dict:
    return {"item_type": item_type, "meta": {}, "parts": [{"type": "text", "text": text}]}


def test_renders_three_message_types():
    items = [
        _text_item("SystemMessageItem", "你是麦麦"),
        _text_item("UserMessageItem", "在吗"),
        _text_item("AssistantMessageItem", "在的"),
    ]
    rendered = build_messages(items)
    assert [m["role"] for m in rendered.messages] == ["system", "user", "assistant"]
    assert "你是麦麦" in rendered.text
    assert "用户：在吗" in rendered.text
    assert "助手：在的" in rendered.text


def test_image_part_becomes_placeholder():
    items = [
        {
            "item_type": "UserMessageItem",
            "meta": {},
            "parts": [
                {"type": "text", "text": "看图"},
                {"type": "image", "image_format": "png", "image_base64": "AAAA"},
            ],
        }
    ]
    rendered = build_messages(items)
    assert rendered.image_count == 1
    assert "〔图片，已省略〕" in rendered.messages[0]["content"]


def test_tool_call_and_output_are_textualized():
    items = [
        {
            "item_type": "FunctionCallItem",
            "meta": {},
            "tool_call": {"call_id": "c1", "func_name": "query_memory", "args": {"q": "天气"}},
        },
        {
            "item_type": "FunctionCallOutputItem",
            "meta": {},
            "call_id": "c1",
            "tool_name": "query_memory",
            "output": "晴天 25 度",
            "success": True,
        },
    ]
    rendered = build_messages(items)
    assert rendered.tool_call_count == 1
    assert "调用工具 query_memory" in rendered.messages[0]["content"]
    assert "工具 query_memory 返回（成功）" in rendered.messages[1]["content"]


def test_reasoning_and_provider_activity_are_skipped():
    items = [
        {"item_type": "ReasoningItem", "meta": {}, "representation": "raw_text", "text_parts": ["思考"], "summary_parts": []},
        {"item_type": "ProviderActivityItem", "meta": {}, "provider_type": "web", "call_id": "x",
         "status": "ok", "display_summary": "搜索中", "action_type": "search", "source_count": 1, "details": []},
        {"item_type": "ProviderOpaqueItem", "meta": {}, "provider_type": "web", "display_summary": "未知"},
        _text_item("UserMessageItem", "继续"),
    ]
    rendered = build_messages(items)
    assert len(rendered.messages) == 1
    assert rendered.unknown_types == []


def test_unknown_item_type_never_raises():
    items = [{"item_type": "SomeFutureItem", "meta": {}, "payload": 1}, _text_item("UserMessageItem", "你好")]
    rendered = build_messages(items)
    assert rendered.unknown_types == ["SomeFutureItem"]
    assert len(rendered.messages) == 1


def test_adjacent_same_role_merged():
    items = [
        _text_item("UserMessageItem", "第一句"),
        _text_item("UserMessageItem", "第二句"),
    ]
    rendered = build_messages(items)
    assert len(rendered.messages) == 1
    assert "第一句" in rendered.messages[0]["content"]
    assert "第二句" in rendered.messages[0]["content"]


def test_budget_drops_oldest_but_keeps_system():
    items = [_text_item("SystemMessageItem", "系统提示")] + [
        _text_item("UserMessageItem", "历史" * 50) for _ in range(20)
    ]
    rendered = build_messages(items, max_history_chars=400)
    assert rendered.dropped_items > 0
    assert rendered.messages[0]["role"] == "system"
    assert rendered.messages[0]["content"] == "系统提示"
    assert len(rendered.text) < 400 + 200


def test_empty_context_returns_empty_messages():
    rendered = build_messages([])
    assert rendered.messages == []
    assert rendered.text == ""


def test_prompt_suffix_appended():
    items = [_text_item("UserMessageItem", "在吗")]
    plain = build_messages(items, prompt_suffix=style_suffix("plain"))
    replyer = build_messages(items, prompt_suffix=style_suffix("replyer"))
    raw = build_messages(items, prompt_suffix=style_suffix("raw"))
    assert "不要使用 Markdown" in plain.text
    assert "口语化" in replyer.text
    assert "口语化" not in raw.text
    assert len(raw.text) < len(replyer.text)


def test_style_suffix_unknown_falls_back_to_plain():
    assert style_suffix("nonsense") == style_suffix("plain")
    assert style_suffix("raw") == ""


def test_render_item_tolerates_non_dict():
    assert render_item("not-a-dict") is None
    assert render_item(None) is None

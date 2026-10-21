"""SSE / JSON-Patch 增量累积器。"""

import json

from deepether.upstream.sse import SseAccumulator


def _data(payload) -> str:
    """把字典包装成一行 SSE data 帧。"""

    return "data: " + json.dumps(payload, ensure_ascii=False)


def _feed(acc: SseAccumulator, *lines: str) -> None:
    for line in lines:
        acc.feed_line(line)


def test_path_value_form_separates_thinking_and_content():
    acc = SseAccumulator()
    _feed(
        acc,
        _data({"p": "response/thinking_content", "v": "让我想想"}),
        _data({"p": "response/content", "v": "你好"}),
        _data({"p": "response/content", "v": "，世界"}),
        "data: [DONE]",
    )
    snap = acc.snapshot
    assert snap.thinking == "让我想想"
    assert snap.content == "你好，世界"
    assert snap.finished is True


def test_json_patch_op_form():
    acc = SseAccumulator()
    _feed(
        acc,
        _data({"op": "replace", "path": "/response/content", "value": "A"}),
        _data({"op": "add", "path": "/response/thinking_content", "value": "B"}),
    )
    assert acc.snapshot.content == "A"
    assert acc.snapshot.thinking == "B"


def test_plain_value_delta_goes_to_content():
    acc = SseAccumulator()
    _feed(acc, _data({"v": "片段1"}), _data({"v": "片段2"}))
    assert acc.snapshot.content == "片段1片段2"


def test_nested_response_object():
    acc = SseAccumulator()
    _feed(acc, _data({"p": "response", "v": {"content": "正文", "thinking_content": "思考"}}))
    assert acc.snapshot.content == "正文"
    assert acc.snapshot.thinking == "思考"


def test_finished_status_marker():
    acc = SseAccumulator()
    _feed(acc, _data({"p": "response/status", "v": "FINISHED"}))
    assert acc.snapshot.finished is True


def test_usage_total_tokens():
    acc = SseAccumulator()
    _feed(acc, _data({"accumulated_token_usage": 123}))
    assert acc.snapshot.total_tokens == 123


def test_unknown_paths_are_ignored_without_crash():
    acc = SseAccumulator()
    _feed(
        acc,
        _data({"p": "response/some_future_field", "v": "whatever"}),
        _data({"p": "response/quasi_status", "v": "WIP"}),
        _data({"noise": 1}),
    )
    assert acc.snapshot.content == ""
    assert acc.snapshot.ignored_values >= 1


def test_real_mixed_protocol_splits_thinking_and_content():
    """2026-10-21 实测的真实帧序列：裸增量 + 状态快照 + 片段追加混在一起。"""

    acc = SseAccumulator()
    _feed(
        acc,
        _data({"request_message_id": 1, "response_message_id": 2, "model_type": "default"}),
        _data({"v": {"response": {
            "message_id": 2,
            "role": "ASSISTANT",
            "thinking_enabled": True,
            "status": "WIP",
            "accumulated_token_usage": 0,
            "fragments": [{"id": 2, "type": "THINK", "content": "我要想"}],
        }}}),
        _data({"p": "response/fragments/-1/content", "o": "APPEND", "v": "一下"}),
        _data({"v": "再"}),
        _data({"p": "response/fragments", "o": "APPEND",
               "v": [{"id": 3, "type": "RESPONSE", "content": "回答"}]}),
        _data({"v": "你好"}),
        _data({"p": "response", "o": "BATCH",
               "v": [{"p": "accumulated_token_usage", "v": 93}, {"p": "quasi_status", "v": "FINISHED"}]}),
        _data({"p": "response/status", "o": "SET", "v": "FINISHED"}),
    )
    snap = acc.snapshot
    assert snap.thinking == "我要想一下再"
    assert snap.content == "回答你好"
    assert snap.total_tokens == 93
    assert snap.finished is True


def test_tool_search_fragment_does_not_pollute_content():
    """联网搜索实测：TOOL_SEARCH 片段的 content 是 "Found 19 web pages" 这类进度文案。"""

    acc = SseAccumulator()
    _feed(
        acc,
        _data({"v": {"response": {
            "fragments": [{"id": 2, "type": "THINK", "content": "想"}],
            "status": "WIP",
        }}}),
        _data({"v": "继续"}),
        _data({"p": "response", "o": "BATCH", "v": [
            {"p": "fragments", "o": "APPEND",
             "v": [{"id": 3, "type": "TOOL_SEARCH", "status": "WIP", "content": None}]},
        ]}),
        _data({"p": "response/fragments/-1", "o": "BATCH", "v": [
            {"p": "status", "v": "FINISHED"},
            {"p": "content", "v": "Found 19 web pages"},
        ]}),
    )
    snap = acc.snapshot
    assert "Found 19 web pages" not in snap.content
    assert snap.content == ""
    assert snap.thinking == "想继续"
    assert snap.finished is False  # 片段结束 ≠ 整个响应结束


def test_bare_deltas_default_to_content_without_snapshot():
    acc = SseAccumulator()
    _feed(acc, _data({"v": "纯"}), _data({"v": "增量"}))
    assert acc.snapshot.content == "纯增量"
    assert acc.snapshot.thinking == ""


def test_snapshot_resync_does_not_duplicate():
    """上游有时发全量快照、有时发增量，混在一起不能重复累加。"""

    acc = SseAccumulator()
    _feed(acc, _data({"v": "你好"}))
    _feed(acc, _data({"v": "，世界"}))
    _feed(acc, _data({"v": {"content": "你好，世界"}}))
    assert acc.snapshot.content == "你好，世界"


def test_malformed_json_counted_not_raised():
    acc = SseAccumulator()
    _feed(acc, "data: {not json", "data: still-not-json")
    assert acc.snapshot.parse_errors == 2
    assert acc.snapshot.content == ""


def test_comments_and_events_ignored():
    acc = SseAccumulator()
    _feed(acc, ": keep-alive", "event: message", "id: 1", "", _data({"v": "ok"}))
    assert acc.snapshot.content == "ok"


def test_raw_json_line_without_data_prefix():
    acc = SseAccumulator()
    _feed(acc, json.dumps({"p": "response/content", "v": "直接给"}, ensure_ascii=False))
    assert acc.snapshot.content == "直接给"


def test_non_string_values_do_not_break_content():
    acc = SseAccumulator()
    _feed(
        acc,
        _data({"p": "response/content", "v": 123}),
        _data({"p": "response/content", "v": "正常"}),
    )
    assert acc.snapshot.content == "正常"
    assert acc.snapshot.ignored_values >= 1

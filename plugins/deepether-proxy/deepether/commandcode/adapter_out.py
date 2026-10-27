"""CommandCode 结果 → Host `output_items` 响应。

插件 Provider 的响应契约见 `.testenv/maibot/src/llm_models/model_client/plugin_client.py`：
带 `output_items` + `item_schema_version=1` 时由 Host 逐项反序列化；`FunctionCallItem` 会被
派生成 `APIResponse.tool_calls`，`planner` 依赖它。所以工具调用必须走新协议，不能退化成
旧标量协议的 `tool_calls` 字段。
"""

from __future__ import annotations

import json
from typing import Any, Final

from ..errors import DeepEtherError, ErrorCode
from .protocol import CcResult, CcToolCall

CONTEXT_ITEM_SCHEMA_VERSION: Final[int] = 1


def _tool_call_item(call: CcToolCall) -> dict[str, Any]:
    """把上游工具调用转成 `FunctionCallItem` 快照。"""

    return {
        "item_type": "FunctionCallItem",
        "tool_call": {
            "call_id": call.call_id,
            "func_name": call.name,
            "args": call.arguments,
        },
    }


def build_response(
    result: CcResult,
    *,
    model_name: str,
    elapsed_ms: int,
    dropped_items: int = 0,
    images_sent: int = 0,
    images_stripped: int = 0,
    tool_outputs_truncated: int = 0,
    history_truncated: bool = False,
) -> dict[str, Any]:
    """构造 Host 可直接反序列化的响应。

    Args:
        result: 上游累积结果。
        model_name: Host 侧模型名（写入 raw_data，便于排查）。
        elapsed_ms: 端到端耗时。
        dropped_items: 投影时丢弃的 Item 数。
        images_sent: 实际发送的图片数。
        images_stripped: 被降级成占位文本的图片数。
        tool_outputs_truncated: 被截断的工具结果数。
        history_truncated: 历史是否因超长被裁剪。

    Returns:
        dict[str, Any]: Host 响应字典。

    Raises:
        DeepEtherError: 结果为空、工具参数被截断/非法时。
    """

    if result.tool_error or any(call.truncated for call in result.tool_calls):
        raise DeepEtherError(
            ErrorCode.PROTOCOL_ERROR,
            "CommandCode 的工具参数被截断或不是合法 JSON，请提高 max_tokens 或换一个工具能力更强的模型",
            detail=f"finish={result.finish_reason} tool_error={result.tool_error}",
            retryable=True,
        )

    cleaned_content = (result.content or "").strip()
    if not cleaned_content and not result.tool_calls:
        raise DeepEtherError(
            ErrorCode.UPSTREAM_EMPTY,
            "CommandCode 返回了空结果（没有正文也没有工具调用）",
            detail=f"events={result.events} finish={result.finish_reason}",
            retryable=True,
        )

    output_items: list[dict[str, Any]] = []
    if result.reasoning.strip():
        output_items.append(
            {
                "item_type": "ReasoningItem",
                "representation": "raw_text",
                "text_parts": [result.reasoning.strip()],
                "summary_parts": [],
            }
        )
    if cleaned_content:
        output_items.append(
            {
                "item_type": "AssistantMessageItem",
                "parts": [{"type": "text", "text": cleaned_content}],
            }
        )
    for call in result.tool_calls:
        output_items.append(_tool_call_item(call))

    return {
        "item_schema_version": CONTEXT_ITEM_SCHEMA_VERSION,
        "output_items": output_items,
        "usage": result.usage.to_host(),
        "raw_data": {
            "stage": "commandcode",
            "model": model_name,
            "elapsed_ms": int(elapsed_ms),
            "finish_reason": result.finish_reason,
            "rounds": result.rounds,
            "events": dict(result.events),
            "provider_metadata": result.provider_metadata,
            "dropped_items": int(dropped_items),
            "images_sent": int(images_sent),
            "images_stripped": int(images_stripped),
            "tool_outputs_truncated": int(tool_outputs_truncated),
            "history_truncated": bool(history_truncated),
        },
    }


def tool_args_preview(call: CcToolCall, limit: int = 200) -> str:
    """返回工具参数的短预览（日志用）。"""

    try:
        text = json.dumps(call.arguments, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(call.arguments)
    return text[:limit]

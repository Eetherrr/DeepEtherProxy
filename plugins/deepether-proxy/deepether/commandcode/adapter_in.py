"""Host `context_items` → CommandCode wire messages 的投影。

Host 的 Item 协议见 `request_snapshot.py`；wire 协议见 `docs/research/COMMANDCODE-FEASIBILITY.md` §3.4。
这一层不发起请求、不读配置，所有降级策略都由显式参数传入，方便单测覆盖。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Final, Iterable, Mapping

from ..errors import DeepEtherError, ErrorCode
from .protocol import clean_surrogates_deep

__all__ = ["CcPrompt", "build_prompt", "normalize_tool_schema", "mime_from_format"]

_MIME_BY_FORMAT: Final[dict[str, str]] = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}


def mime_from_format(image_format: str) -> str:
    """把 Host 的 `image_format` 转成 data URI 用的 mime。"""

    key = str(image_format or "").strip().lower()
    return _MIME_BY_FORMAT.get(key, f"image/{key or 'png'}")


@dataclass
class CcPrompt:
    """一次请求投影后的 wire 结构。"""

    system: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    tools: list[dict[str, Any]] = field(default_factory=list)
    dropped_items: int = 0
    unknown_types: tuple[str, ...] = ()
    images_sent: int = 0
    images_stripped: int = 0
    tool_outputs_truncated: int = 0
    history_truncated: bool = False

    @property
    def message_chars(self) -> int:
        """消息正文总字符数（用于日志与预算估算）。"""

        return sum(_content_chars(message.get("content")) for message in self.messages)


def _content_chars(content: Any) -> int:
    """统计一条消息里所有文本块与工具参数的字符数。"""

    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    total = 0
    for block in content:
        if not isinstance(block, Mapping):
            continue
        for key in ("text", "value"):
            value = block.get(key)
            if isinstance(value, str):
                total += len(value)
        if isinstance(block.get("input"), Mapping):
            total += len(json.dumps(block["input"], ensure_ascii=False))
    return total


def _parts_to_blocks(
    parts: Any,
    *,
    vision: bool,
    vision_mode: str,
    max_image_bytes: int,
    images_sent: list[int],
    images_stripped: list[int],
) -> list[dict[str, Any]]:
    """把 Host 的 content parts 转成 wire content blocks。"""

    blocks: list[dict[str, Any]] = []
    if not isinstance(parts, list):
        return blocks
    for part in parts:
        if not isinstance(part, Mapping):
            continue
        part_type = str(part.get("type") or "").lower()
        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
            continue
        if part_type == "refusal":
            refusal = str(part.get("refusal") or "").strip()
            blocks.append({"type": "text", "text": f"〔模型拒答：{refusal}〕" if refusal else "〔模型拒答〕"})
            continue
        if part_type == "image":
            encoded = part.get("image_base64")
            image_format = str(part.get("image_format") or "png")
            if not isinstance(encoded, str) or not encoded:
                continue
            if not vision or len(encoded) > max_image_bytes:
                if vision_mode == "error" and vision:
                    raise DeepEtherError(
                        ErrorCode.NOT_SUPPORTED,
                        f"图片超过单张上限（{max_image_bytes} 字节，base64 编码后），已拒绝本次请求",
                    )
                if not vision and vision_mode == "error":
                    raise DeepEtherError(
                        ErrorCode.NOT_SUPPORTED,
                        "当前 CommandCode 模型不支持图像输入，请改用视觉模型或把 vision_mode 设为 strip",
                    )
                images_stripped[0] += 1
                blocks.append({"type": "text", "text": "〔图片已省略：当前模型不接收图像输入〕"})
                continue
            mime = mime_from_format(image_format)
            blocks.append(
                {
                    "type": "image",
                    "image": f"data:{mime};base64,{encoded}",
                    "mimeType": mime,
                }
            )
            images_sent[0] += 1
            continue
    return blocks


def normalize_tool_schema(schema: Any) -> dict[str, Any]:
    """把工具参数 schema 的根归一成 `type:"object"`。

    网关对根不是 object 的工具会直接 400；MaiBot 自带工具已经是 object，
    但第三方/MCP 工具可能给出空 schema、`$ref` 或 `allOf`，这里统一兜住。
    """

    if not isinstance(schema, Mapping) or not schema:
        return {"type": "object", "properties": {}}
    normalized = dict(schema)
    if normalized.get("type") == "object":
        if not isinstance(normalized.get("properties"), dict):
            normalized["properties"] = {}
        return dict(normalized)
    properties = normalized.get("properties")
    if isinstance(properties, Mapping):
        result: dict[str, Any] = {"type": "object", "properties": dict(properties)}
        required = normalized.get("required")
        if isinstance(required, list):
            result["required"] = [item for item in required if isinstance(item, str)]
        return result
    # 根是 $ref / allOf / 基本类型：包一层空 object，宁可放宽也不能整请求失败。
    return {"type": "object", "properties": {}, "additionalProperties": True}


def _convert_tools(tool_options: Any) -> tuple[list[dict[str, Any]], int]:
    """把 Host 的 OpenAI function schema 转成网关的 `{name, description, input_schema}`。"""

    if not isinstance(tool_options, list):
        return [], 0
    tools: list[dict[str, Any]] = []
    skipped = 0
    for item in tool_options:
        if not isinstance(item, Mapping):
            skipped += 1
            continue
        function = item.get("function") if isinstance(item.get("function"), Mapping) else item
        name = str(function.get("name") or "").strip()
        if not name:
            skipped += 1
            continue
        tools.append(
            {
                "name": name,
                "description": str(function.get("description") or ""),
                "input_schema": normalize_tool_schema(function.get("parameters")),
            }
        )
    return tools, skipped


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    """按字符数截断工具结果；超限时保留头尾并标注。"""

    if limit <= 0 or len(text) <= limit:
        return text, False
    head = max(1, limit * 2 // 3)
    tail = max(0, limit - head)
    marker = f"\n…（工具结果过长，已截断 {len(text) - head - tail} 字）…\n"
    return text[:head] + marker + (text[-tail:] if tail else ""), True


def _response_format_hint(response_format: Any) -> str:
    """把 Host 的 `response_format` 变成一句系统提示（网关没有原生 JSON 模式）。"""

    if not isinstance(response_format, Mapping):
        return ""
    schema = response_format.get("schema")
    format_type = str(response_format.get("format_type") or "").strip()
    if not isinstance(schema, Mapping) and not format_type:
        return ""
    parts = ["本次输出必须是严格 JSON，不要包含 Markdown 代码块或任何解释文字。"]
    if isinstance(schema, Mapping):
        parts.append("JSON Schema：")
        parts.append(json.dumps(schema, ensure_ascii=False)[:4000])
    return "\n".join(parts)


def build_prompt(
    context_items: Any,
    *,
    tool_options: Any = None,
    response_format: Any = None,
    vision: bool = False,
    vision_mode: str = "strip",
    tool_mode: str = "auto",
    max_history_chars: int = 60000,
    max_tool_output_chars: int = 20000,
    max_image_bytes: int = 4 * 1024 * 1024,
) -> CcPrompt:
    """把 Host 请求投影成 wire 结构。

    Args:
        context_items: Host 序列化后的 `context_items`。
        tool_options: Host 的 `tool_options`（OpenAI function schema 列表）。
        response_format: Host 的 `response_format`（没有原生映射，只能追加提示）。
        vision: 当前模型是否具备视觉能力（来自目录）。
        vision_mode: `strip` / `auto` / `error`。
        tool_mode: `auto` / `off`。
        max_history_chars: 历史正文保留上限（字符）。
        max_tool_output_chars: 单条工具结果上限（字符）。
        max_image_bytes: 单张图片的 base64 字符上限。

    Returns:
        CcPrompt: wire 结构 + 降级统计。

    Raises:
        DeepEtherError: `vision_mode=error` 且模型不支持视觉时。
    """

    prompt = CcPrompt()
    system_parts: list[str] = []
    pending_reasoning: list[str] = []
    images_sent = [0]
    images_stripped = [0]
    unknown: list[str] = []
    items = context_items if isinstance(context_items, list) else []

    def flush_reasoning() -> None:
        if not pending_reasoning:
            return
        text = "\n".join(part for part in pending_reasoning if part)
        pending_reasoning.clear()
        if not text:
            return
        if prompt.messages and prompt.messages[-1].get("role") == "assistant":
            prompt.messages[-1]["content"].insert(0, {"type": "reasoning", "text": text})
        else:
            prompt.messages.append({"role": "assistant", "content": [{"type": "reasoning", "text": text}]})

    for item in items:
        if not isinstance(item, Mapping):
            prompt.dropped_items += 1
            continue
        item_type = str(item.get("item_type") or "")
        if item_type == "SystemMessageItem":
            for block in _parts_to_blocks(
                item.get("parts"),
                vision=False,
                vision_mode="strip",
                max_image_bytes=max_image_bytes,
                images_sent=images_sent,
                images_stripped=images_stripped,
            ):
                if block.get("type") == "text":
                    system_parts.append(str(block.get("text") or ""))
            continue
        if item_type == "UserMessageItem":
            flush_reasoning()
            blocks = _parts_to_blocks(
                item.get("parts"),
                vision=vision,
                vision_mode=vision_mode,
                max_image_bytes=max_image_bytes,
                images_sent=images_sent,
                images_stripped=images_stripped,
            )
            if blocks:
                prompt.messages.append({"role": "user", "content": blocks})
            continue
        if item_type == "AssistantMessageItem":
            flush_reasoning()
            blocks = _parts_to_blocks(
                item.get("parts"),
                vision=False,
                vision_mode="strip",
                max_image_bytes=max_image_bytes,
                images_sent=images_sent,
                images_stripped=images_stripped,
            )
            blocks = [block for block in blocks if block.get("type") == "text"]
            if blocks:
                if prompt.messages and prompt.messages[-1].get("role") == "assistant":
                    prompt.messages[-1]["content"].extend(blocks)
                else:
                    prompt.messages.append({"role": "assistant", "content": blocks})
            continue
        if item_type == "ReasoningItem":
            for text in item.get("text_parts") or []:
                if isinstance(text, str) and text:
                    pending_reasoning.append(text)
            continue
        if item_type == "FunctionCallItem":
            flush_reasoning()
            raw_call = item.get("tool_call")
            if not isinstance(raw_call, Mapping):
                prompt.dropped_items += 1
                continue
            call_id = str(raw_call.get("call_id") or "").strip()
            name = str(raw_call.get("func_name") or raw_call.get("name") or "").strip()
            arguments = raw_call.get("args")
            if not isinstance(arguments, Mapping):
                arguments = {}
            if not name:
                prompt.dropped_items += 1
                continue
            block = {
                "type": "tool-call",
                "toolCallId": call_id or "call_unknown",
                "toolName": name,
                "input": dict(arguments),
            }
            if prompt.messages and prompt.messages[-1].get("role") == "assistant":
                prompt.messages[-1]["content"].append(block)
            else:
                prompt.messages.append({"role": "assistant", "content": [block]})
            continue
        if item_type == "FunctionCallOutputItem":
            flush_reasoning()
            call_id = str(item.get("call_id") or "").strip()
            output = item.get("output")
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            text, truncated = _truncate(text, max_tool_output_chars)
            if truncated:
                prompt.tool_outputs_truncated += 1
            success = item.get("success")
            block = {
                "type": "tool-result",
                "toolCallId": call_id or "call_unknown",
                "toolName": str(item.get("tool_name") or ""),
                "output": {
                    "type": "text" if success is not False else "error-text",
                    "value": text,
                },
            }
            if prompt.messages and prompt.messages[-1].get("role") == "tool":
                prompt.messages[-1]["content"].append(block)
            else:
                prompt.messages.append({"role": "tool", "content": [block]})
            continue
        # ProviderActivityItem / ProviderOpaqueItem 与未知类型：跳过并计数，绝不抛异常。
        if item_type not in ("ProviderActivityItem", "ProviderOpaqueItem"):
            unknown.append(item_type or "(空)")
        prompt.dropped_items += 1

    flush_reasoning()
    prompt.unknown_types = tuple(sorted(set(unknown)))
    prompt.images_sent = images_sent[0]
    prompt.images_stripped = images_stripped[0]

    prompt.messages = _merge_adjacent(prompt.messages)
    prompt.messages, prompt.history_truncated = _trim_history(prompt.messages, max_history_chars)

    system = "\n\n".join(part.strip() for part in system_parts if part.strip())
    hint = _response_format_hint(response_format)
    if hint:
        system = (system + "\n\n" + hint).strip() if system else hint
    prompt.system = system

    if str(tool_mode).lower() != "off":
        tools, skipped = _convert_tools(tool_options)
        prompt.tools = tools
        prompt.dropped_items += skipped

    prompt.system = clean_surrogates_deep(prompt.system)
    prompt.messages = clean_surrogates_deep(prompt.messages)
    prompt.tools = clean_surrogates_deep(prompt.tools)
    if not prompt.messages:
        raise DeepEtherError(
            ErrorCode.PROTOCOL_ERROR,
            "Host 请求里没有可用的对话内容，无法生成 CommandCode 请求",
            detail=f"context_items={len(items)} dropped={prompt.dropped_items}",
        )
    return prompt


def _merge_adjacent(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并相邻同角色消息（上游对连续两条 user / assistant 更敏感）。"""

    merged: list[dict[str, Any]] = []
    for message in messages:
        if merged and merged[-1].get("role") == message.get("role"):
            merged[-1]["content"].extend(message.get("content") or [])
            continue
        merged.append({"role": message.get("role"), "content": list(message.get("content") or [])})
    return merged


def _trim_history(messages: list[dict[str, Any]], limit: int) -> tuple[list[dict[str, Any]], bool]:
    """按字符上限从最旧的消息开始丢，至少保留最后一条消息。"""

    if limit <= 0:
        return messages, False
    total = sum(_content_chars(message.get("content")) for message in messages)
    if total <= limit:
        return messages, False
    trimmed = list(messages)
    while len(trimmed) > 1 and total > limit:
        removed = trimmed.pop(0)
        total -= _content_chars(removed.get("content"))
    return trimmed, True


def iter_items(items: Iterable[Any]) -> Iterable[Mapping[str, Any]]:
    """过滤出字典类型的 Item（测试与诊断用）。"""

    for item in items:
        if isinstance(item, Mapping):
            yield item

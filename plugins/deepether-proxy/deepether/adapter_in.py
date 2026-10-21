"""`context_items` → 网页版 prompt 的投影层。

MaiBot 给插件的是**无状态全量上下文**（`context_items`），而网页版要的是一段自然语言
对话记录。本模块负责把前者拍平成后者，并保证：

1. 只做纯函数转换，不联网、不依赖 MaiBot，可单测；
2. **未知 `item_type` 绝不抛异常**（官方曾新增过 `ProviderActivityItem` 等类型），
   只记录到 `unknown_types` 里；
3. 图片按占位文本降级（网页版不接受图片二进制）；
4. 超长历史按字符预算从最旧的开始丢弃，系统提示词永不丢弃。
"""

from dataclasses import dataclass, field
from typing import Any, Final

SYSTEM_ROLE: Final[str] = "system"
USER_ROLE: Final[str] = "user"
ASSISTANT_ROLE: Final[str] = "assistant"

_TOOL_OUTPUT_LIMIT: Final[int] = 2000
_TRUNCATED_SUFFIX: Final[str] = "…(已截断)"

PROMPT_STYLES: Final[tuple[str, ...]] = ("plain", "replyer", "raw")
"""提示词风格档位。"""

_STYLE_SUFFIXES: Final[dict[str, str]] = {
    # 默认档：对 replyer 与 utils 都安全（utils 常要求 JSON，这里只约束"不要包裹、不要加说明"）。
    "plain": "请直接输出结果本身，不要加任何前缀、解释或客套话；除非任务明确要求，不要使用 Markdown 语法（列表、加粗、代码块）。",
    # replyer 专用（报告 §4.5.3 细节 5）：回复后处理会 strip 与断句，必须是一段口语化纯文本。
    "replyer": "只输出一段口语化的纯文本回复，不要使用 Markdown、列表、加粗、代码块，不要带「助手：」之类前缀，不要解释你在做什么。",
    # raw：不追加任何指令，交给调用方自己的提示词。
    "raw": "",
}


def style_suffix(style: str) -> str:
    """返回某个提示词风格对应的追加指令。

    Args:
        style: 风格名，取自 :data:`PROMPT_STYLES`。

    Returns:
        str: 追加到 prompt 末尾的指令；`raw` 返回空串，未知风格按 `plain` 处理。
    """

    if style == "raw":
        return ""
    return _STYLE_SUFFIXES.get(style, _STYLE_SUFFIXES["plain"])


_PROMPT_HEADER: Final[str] = "以下是一段对话记录，请延续它："
_PROMPT_FOOTER: Final[str] = "请直接给出接下来「助手」要说的话，只输出回复正文，不要带「助手：」前缀。"


@dataclass
class RenderedContext:
    """一次请求投影后的结果。"""

    messages: list[dict[str, str]]
    """OpenAI 风格的 `[{"role", "content"}]`，供降级通道直接复用。"""

    text: str
    """拍平后的单条 prompt，供网页版使用。"""

    dropped_items: int = 0
    """因为字符预算被丢弃的历史条目数。"""

    image_count: int = 0
    """被降级为占位文本的图片数量。"""

    tool_call_count: int = 0
    """上下文里出现的工具调用条目数。"""

    unknown_types: list[str] = field(default_factory=list)
    """遇到过的未知 item_type（用于发现上游新增类型）。"""


def _render_parts(parts: Any) -> tuple[str, int]:
    """把 `parts` 渲染成纯文本。

    Args:
        parts: `context_items[].parts` 的原始值。

    Returns:
        tuple[str, int]: `(文本, 图片数量)`。
    """

    if not isinstance(parts, list):
        return "", 0

    chunks: list[str] = []
    image_count = 0
    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = str(part.get("type") or "").strip().lower()
        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
        elif part_type == "image":
            image_count += 1
            chunks.append("〔图片，已省略〕")
        elif part_type == "refusal":
            refusal = part.get("refusal")
            if isinstance(refusal, str) and refusal:
                chunks.append(f"〔模型拒答：{refusal}〕")
    return "".join(chunks), image_count


def _truncate(text: str, limit: int = _TOOL_OUTPUT_LIMIT) -> str:
    """按字符数截断长文本（用于工具输出）。"""

    if len(text) <= limit:
        return text
    return text[:limit] + _TRUNCATED_SUFFIX


def render_item(item: Any) -> tuple[str, str, int] | None:
    """把单个 context item 渲染成 `(role, text, image_count)`。

    Args:
        item: `context_items` 中的一项。

    Returns:
        tuple[str, str, int] | None: 渲染结果；返回 `None` 表示该条目被跳过
        （思考内容、上游活动、未知类型等）。
    """

    if not isinstance(item, dict):
        return None

    item_type = str(item.get("item_type") or "").strip()
    if item_type in ("SystemMessageItem", "UserMessageItem", "AssistantMessageItem"):
        text, image_count = _render_parts(item.get("parts"))
        role = {
            "SystemMessageItem": SYSTEM_ROLE,
            "UserMessageItem": USER_ROLE,
            "AssistantMessageItem": ASSISTANT_ROLE,
        }[item_type]
        if not text:
            return None
        return role, text, image_count

    if item_type == "FunctionCallItem":
        tool_call = item.get("tool_call")
        if not isinstance(tool_call, dict):
            return None
        func_name = str(tool_call.get("func_name") or "未知工具")
        args = tool_call.get("args")
        args_text = str(args) if args is not None else "{}"
        return ASSISTANT_ROLE, f"〔调用工具 {func_name}，参数 {_truncate(args_text, 500)}〕", 0

    if item_type == "FunctionCallOutputItem":
        tool_name = str(item.get("tool_name") or "未知工具")
        output = item.get("output")
        output_text = output if isinstance(output, str) else str(output)
        success = "成功" if item.get("success") else "失败"
        return USER_ROLE, f"〔工具 {tool_name} 返回（{success}）：{_truncate(output_text)}〕", 0

    # ReasoningItem：历史思考内容对后续生成价值低且很占预算，直接丢弃；
    # ProviderActivityItem / ProviderOpaqueItem 是上游侧展示信息，同样丢弃；
    # 其余未知类型一律安全跳过，绝不抛异常。
    return None


def _merge_adjacent(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """合并相邻的同角色消息，并保证 system 消息只出现在开头。"""

    merged: list[dict[str, str]] = []
    for message in messages:
        role = message["role"]
        if role == SYSTEM_ROLE:
            if merged and merged[0]["role"] == SYSTEM_ROLE:
                merged[0] = {"role": SYSTEM_ROLE, "content": merged[0]["content"] + "\n" + message["content"]}
            else:
                merged.insert(0, dict(message))
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1] = {"role": role, "content": merged[-1]["content"] + "\n" + message["content"]}
            continue
        merged.append(dict(message))
    return merged


def _apply_budget(messages: list[dict[str, str]], max_chars: int) -> tuple[list[dict[str, str]], int]:
    """按字符预算裁剪历史：保留全部 system 与最近的消息。"""

    if max_chars <= 0:
        return messages, 0

    system_messages = [m for m in messages if m["role"] == SYSTEM_ROLE]
    conversation = [m for m in messages if m["role"] != SYSTEM_ROLE]

    used = sum(len(m["content"]) for m in system_messages)
    kept: list[dict[str, str]] = []
    dropped = 0
    for message in reversed(conversation):
        cost = len(message["content"])
        if used + cost > max_chars and kept:
            dropped += 1
            continue
        kept.append(message)
        used += cost
    kept.reverse()
    return system_messages + kept, dropped


def build_messages(
    context_items: Any,
    *,
    max_history_chars: int = 60000,
    prompt_suffix: str = "",
) -> RenderedContext:
    """把 `context_items` 投影成消息列表与单条 prompt。

    Args:
        context_items: Host 请求里的 `context_items`。
        max_history_chars: 历史字符预算；`<=0` 表示不裁剪。
        prompt_suffix: 追加在 prompt 末尾的风格指令（见 :func:`style_suffix`）。

    Returns:
        RenderedContext: 投影结果。
    """

    raw_items = context_items if isinstance(context_items, list) else []
    messages: list[dict[str, str]] = []
    dropped_unknown: list[str] = []
    image_count = 0
    tool_call_count = 0

    for item in raw_items:
        rendered = render_item(item)
        if rendered is None:
            item_type = str(item.get("item_type") or "") if isinstance(item, dict) else ""
            if item_type and item_type not in (
                "ReasoningItem",
                "ProviderActivityItem",
                "ProviderOpaqueItem",
            ):
                dropped_unknown.append(item_type)
            continue
        role, text, images = rendered
        messages.append({"role": role, "content": text})
        image_count += images
        if isinstance(item, dict) and item.get("item_type") == "FunctionCallItem":
            tool_call_count += 1

    budgeted, dropped = _apply_budget(messages, max_history_chars)
    merged = _merge_adjacent(budgeted)
    return RenderedContext(
        messages=merged,
        text=render_prompt_text(merged, suffix=prompt_suffix),
        dropped_items=dropped,
        image_count=image_count,
        tool_call_count=tool_call_count,
        unknown_types=sorted(set(dropped_unknown)),
    )


def render_prompt_text(messages: list[dict[str, str]], *, suffix: str = "") -> str:
    """把消息列表拼成网页版可吃的单条 prompt。

    Args:
        messages: 已合并、已裁剪的消息列表。
        suffix: 追加在末尾的风格指令。

    Returns:
        str: 单条 prompt 文本。
    """

    system_chunks = [m["content"] for m in messages if m["role"] == SYSTEM_ROLE]
    conversation = [m for m in messages if m["role"] != SYSTEM_ROLE]

    lines: list[str] = []
    if system_chunks:
        lines.append("\n".join(system_chunks))
        lines.append("")

    if conversation:
        lines.append(_PROMPT_HEADER)
        for message in conversation:
            speaker = "用户" if message["role"] == USER_ROLE else "助手"
            lines.append(f"{speaker}：{message['content']}")
        lines.append("")
        lines.append(_PROMPT_FOOTER)

    if suffix:
        lines.append("")
        lines.append(suffix)

    return "\n".join(lines).strip()

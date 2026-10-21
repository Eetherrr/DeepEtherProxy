"""上游结果 → Host 可消费的响应字典。

一期走报告 §2.4 的旧标量协议（`content` / `reasoning_content` / `usage`），
Host 会打一条 deprecation warning，这是预期内的；二期切换 `output_items` 时
只改本模块即可。
"""

from typing import Any, Final, Literal

import re

CONTEXT_ITEM_SCHEMA_VERSION: Final[int] = 1

# 网页版联网搜索会在正文里插入它自己的引用标记，例如 "…开幕 [citation:1]。"
# 这些标记只在网页 UI 里可点，直接发到群里就是噪声，必须剥掉。
_WEB_ARTIFACT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[ \t]*\[(?:citation|reference|source)\s*[:：]\s*\d+\]",
    re.IGNORECASE,
)


def strip_web_artifacts(text: str | None) -> str:
    """清掉网页版内部标记（搜索引用角标等）。

    Args:
        text: 上游返回的正文。

    Returns:
        str: 清洗后的正文。
    """

    if not text:
        return ""
    cleaned = _WEB_ARTIFACT_PATTERN.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()
ResponseProtocol = Literal["legacy", "output_items"]


def build_legacy_response(
    *,
    content: str | None,
    reasoning: str | None,
    total_tokens: int = 0,
    raw_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造旧标量协议响应。

    Args:
        content: 正文。
        reasoning: 思考内容。
        total_tokens: 上游给的累计 token 总量。
        raw_data: 附加的原始信息（会进 `raw_data`，不影响 Host 解析）。

    Returns:
        dict[str, Any]: Host 可直接消费的响应字典。
    """

    return {
        "content": strip_web_artifacts(content),
        "reasoning_content": reasoning or "",
        "usage": {"total_tokens": int(total_tokens or 0)},
        "raw_data": raw_data or {},
    }


def build_output_items_response(
    *,
    content: str | None,
    reasoning: str | None,
    total_tokens: int = 0,
) -> dict[str, Any]:
    """构造新协议响应（`output_items`）。

    Args:
        content: 正文。
        reasoning: 思考内容。
        total_tokens: 上游给的累计 token 总量。

    Returns:
        dict[str, Any]: 带 `item_schema_version` 的响应字典。
    """

    output_items: list[dict[str, Any]] = []
    if reasoning:
        output_items.append(
            {
                "item_type": "ReasoningItem",
                "representation": "raw_text",
                "text_parts": [reasoning],
                "summary_parts": [],
            }
        )
    cleaned_content = strip_web_artifacts(content)
    if cleaned_content:
        output_items.append(
            {
                "item_type": "AssistantMessageItem",
                "parts": [{"type": "text", "text": cleaned_content}],
            }
        )
    return {
        "item_schema_version": CONTEXT_ITEM_SCHEMA_VERSION,
        "output_items": output_items,
        "usage": {"total_tokens": int(total_tokens or 0)},
    }


def build_response(
    protocol: ResponseProtocol,
    *,
    content: str | None,
    reasoning: str | None,
    total_tokens: int = 0,
    raw_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按配置选择响应协议。

    Args:
        protocol: `legacy`（一期默认）或 `output_items`（二期）。
        content: 正文。
        reasoning: 思考内容。
        total_tokens: 上游给的累计 token 总量。
        raw_data: 旧协议下附加的原始信息。

    Returns:
        dict[str, Any]: 响应字典。
    """

    if protocol == "output_items":
        return build_output_items_response(content=content, reasoning=reasoning, total_tokens=total_tokens)
    return build_legacy_response(
        content=content,
        reasoning=reasoning,
        total_tokens=total_tokens,
        raw_data=raw_data,
    )

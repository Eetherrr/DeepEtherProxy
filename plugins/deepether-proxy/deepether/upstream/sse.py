"""上游 SSE 增量累积器（按 2026-10-21 实测协议实现）。

实测的真实形态（`thinking_enabled=true`）是一锅混合帧：

    {"v": "我要"}                                     ← 裸文本增量（最多，无路径）
    {"v": {"response": {"fragments": [{"type": "THINK", "content": "我要"}], "status": "WIP", ...}}}
                                                      ← 全量状态快照（偶发，标明当前片段类型）
    {"p": "response/fragments/-1/content", "o": "APPEND", "v": "回答"}   ← 追加到最后片段
    {"p": "response/fragments", "o": "APPEND", "v": [{"id": 3, "type": "RESPONSE", "content": "…"}]}
                                                      ← 新开一个片段（THINK → RESPONSE 的分界）
    {"p": "response", "o": "BATCH", "v": [{"p": "accumulated_token_usage", "v": 93}, …]}
    {"p": "response/status", "o": "SET", "v": "FINISHED"}

因此**不能**按"路径里有没有 thinking 字样"来分类（裸增量没有路径）。正确做法是维护
"当前片段类型"这个状态：快照/片段追加会告诉我们当前是 THINK 还是 RESPONSE，
随后的裸增量就归到该类型里。合并时用"前缀重同步"策略，兼顾"全量快照"和"纯增量"两种上游。
"""

from dataclasses import dataclass
from typing import Any, Final

import json

FINISH_MARKERS: Final[frozenset[str]] = frozenset(
    {"finished", "finish", "done", "stop", "complete", "completed"}
)
_IGNORED_PREFIXES: Final[tuple[str, ...]] = ("event:", "id:", "retry:", ":")

KIND_THINKING: Final[str] = "thinking"
KIND_CONTENT: Final[str] = "content"
KIND_IGNORED: Final[str] = "ignored"
"""联网搜索 / 工具类片段的桶：只为了不污染正文，不对外暴露。"""

_THINKING_FRAGMENT_TYPES: Final[frozenset[str]] = frozenset(
    {"think", "thinking", "reason", "reasoning", "thought"}
)
_RESPONSE_FRAGMENT_TYPES: Final[frozenset[str]] = frozenset({"response", "answer", "text"})
_USAGE_KEYS: Final[tuple[str, ...]] = (
    "accumulated_token_usage",
    "accumulatedTokenUsage",
    "total_tokens",
)


def _looks_like_fragment(node: dict[str, Any]) -> bool:
    """判断一个 dict 是"片段定义"还是"嵌套 patch"。

    Args:
        node: 待判断的字典。

    Returns:
        bool: 是片段定义时返回 `True`。
    """

    return "type" in node or "fragment_type" in node or ("id" in node and "content" in node)


def _fragment_kind(fragment: dict[str, Any]) -> str:
    """判断一个片段属于思考、正文，还是应当忽略（搜索/工具类）。

    Args:
        fragment: 片段定义。

    Returns:
        str: `thinking` / `content` / `ignored`。
    """

    raw_type = str(fragment.get("type") or fragment.get("fragment_type") or "").strip().lower()
    if raw_type in _THINKING_FRAGMENT_TYPES:
        return KIND_THINKING
    if raw_type in _RESPONSE_FRAGMENT_TYPES:
        return KIND_CONTENT
    if raw_type:
        # 实测联网搜索会推 `TOOL_SEARCH` 片段，content 是 "Found 19 web pages" 这类进度文案，
        # 绝不能当正文返回，否则用户会收到一句 "Found 19 web pages"。
        return KIND_IGNORED
    if "thinking_content" in fragment or "thinking" in fragment:
        return KIND_THINKING
    return KIND_CONTENT


@dataclass
class StreamSnapshot:
    """一次流式读取的累积结果。"""

    content: str = ""
    thinking: str = ""
    finished: bool = False
    total_tokens: int = 0
    events: int = 0
    content_fragments: int = 0
    thinking_fragments: int = 0
    ignored_values: int = 0
    parse_errors: int = 0


class SseAccumulator:
    """把混合形态的 SSE 帧累积成"正文 + 思考"。"""

    def __init__(self) -> None:
        """初始化累积器。"""

        self._texts: dict[str, str] = {KIND_CONTENT: "", KIND_THINKING: "", KIND_IGNORED: ""}
        self._current_kind: str = KIND_CONTENT
        self._snapshot = StreamSnapshot()

    @property
    def snapshot(self) -> StreamSnapshot:
        """返回当前累积结果。"""

        snap = self._snapshot
        snap.content = self._texts[KIND_CONTENT]
        snap.thinking = self._texts[KIND_THINKING]
        return snap

    # ─── 入口 ───────────────────────────────────────────────────────────

    def feed_line(self, line: str) -> None:
        """喂入一行 SSE 文本。

        Args:
            line: 单行原文（可带 \\r\\n）。
        """

        text = line.strip()
        if not text or text.lower().startswith(_IGNORED_PREFIXES):
            return

        if text.startswith("data:"):
            payload_text = text[5:].strip()
        elif text.startswith(("{", "[")):
            payload_text = text
        else:
            return

        if not payload_text:
            return
        if payload_text.upper() == "[DONE]":
            self._snapshot.finished = True
            return

        try:
            payload = json.loads(payload_text)
        except ValueError:
            self._snapshot.parse_errors += 1
            return
        self.feed_json(payload)

    def feed_json(self, payload: Any) -> None:
        """喂入一个已解析的帧。

        Args:
            payload: JSON 帧（dict / list / 标量）。
        """

        if isinstance(payload, list):
            for item in payload:
                self.feed_json(item)
            return
        if not isinstance(payload, dict):
            return

        self._snapshot.events += 1
        self._consume_usage(payload)

        path = payload.get("p", payload.get("path"))
        if isinstance(path, str) and path:
            self._apply_patch(
                path,
                str(payload.get("o") or payload.get("op") or ""),
                payload.get("v", payload.get("value")),
            )
            return

        if "v" in payload:
            value = payload["v"]
            if isinstance(value, str):
                self._append_delta(value)
                return
            if isinstance(value, dict):
                self._apply_snapshot(value)
                return
            if isinstance(value, list):
                self.feed_json(value)
                return

        if any(key in payload for key in ("content", "thinking_content", "thinking", "response")):
            self._apply_snapshot(payload)
            return

        if str(payload.get("type") or "").lower() in FINISH_MARKERS:
            self._snapshot.finished = True

    # ─── 帧处理 ─────────────────────────────────────────────────────────

    def _apply_patch(self, path: str, op: str, value: Any, *, fragment_scoped: bool = False) -> None:
        """处理 `{"p": ..., "o": ..., "v": ...}` 形式的 patch 帧。

        Args:
            path: patch 路径。
            op: 操作类型（APPEND / SET / BATCH …）。
            value: 新值。
            fragment_scoped: 该 patch 是否作用在"某个片段内部"。片段内部的
                `status=FINISHED` 只表示这个片段结束（例如搜索进度），不代表整个响应结束，
                所以不能置全局 finished。
        """

        normalized = path.strip().lstrip("/").lower()

        if op.upper() == "BATCH" and isinstance(value, list):
            scoped = fragment_scoped or "fragments" in normalized
            for item in value:
                if not isinstance(item, dict):
                    continue
                if scoped:
                    self._apply_patch(
                        str(item.get("p") or item.get("path") or ""),
                        str(item.get("o") or item.get("op") or ""),
                        item.get("v"),
                        fragment_scoped=True,
                    )
                else:
                    self.feed_json(item)
            return

        if "token_usage" in normalized or normalized.endswith("total_tokens"):
            # BATCH 里的 {"p": "accumulated_token_usage", "v": 93} 走的是路径，不是键。
            try:
                self._snapshot.total_tokens = int(value or 0)
            except (TypeError, ValueError):
                pass
            return

        if "fragments" in normalized:
            if isinstance(value, list):
                for fragment in value:
                    if isinstance(fragment, dict):
                        self._apply_fragment(fragment)
                return
            if isinstance(value, str) and normalized.endswith("content"):
                # 形如 response/fragments/-1/content：追加到"当前片段"
                self._append_delta(value)
                return
            if isinstance(value, dict):
                self._apply_fragment(value)
                return
            if isinstance(value, list):
                return
            # fragments/-1/elapsed_secs 之类的元字段，忽略
            self._snapshot.ignored_values += 1
            return

        if normalized.endswith("content") or normalized.endswith("text"):
            if isinstance(value, str):
                kind = KIND_THINKING if "think" in normalized else self._current_kind
                self._merge(kind, value)
                return
            self._snapshot.ignored_values += 1
            return

        if "status" in normalized or "state" in normalized:
            if not fragment_scoped and isinstance(value, str) and value.strip().lower() in FINISH_MARKERS:
                self._snapshot.finished = True
            return

        if isinstance(value, (dict, list)):
            self.feed_json(value)
            return

        self._snapshot.ignored_values += 1

    def _apply_snapshot(self, payload: dict[str, Any]) -> None:
        """处理全量状态快照（用来判断片段类型/状态，正文靠片段与增量合并）。"""

        node = payload
        response = payload.get("response")
        if isinstance(response, dict):
            node = response

        self._consume_usage(node)

        status = node.get("status") or node.get("quasi_status")
        if isinstance(status, str) and status.strip().lower() in FINISH_MARKERS:
            self._snapshot.finished = True

        fragments = node.get("fragments")
        if isinstance(fragments, list):
            for fragment in fragments:
                if isinstance(fragment, dict):
                    self._apply_fragment(fragment)

        for key, kind in (
            ("content", KIND_CONTENT),
            ("thinking_content", KIND_THINKING),
            ("thinking", KIND_THINKING),
        ):
            value = node.get(key)
            if isinstance(value, str) and value:
                self._merge(kind, value)

    def _apply_fragment(self, fragment: dict[str, Any]) -> None:
        """处理一个片段：切换"当前类型"并合并它的完整内容。

        收到的不一定是片段定义：实测还会出现 `{"p": "content", "v": "Found 19 web pages"}`
        这类"作用于当前片段内部"的嵌套 patch，必须按 patch 处理并保持 fragment 作用域。
        """

        if not _looks_like_fragment(fragment):
            nested_path = str(fragment.get("p") or fragment.get("path") or "")
            if nested_path:
                self._apply_patch(
                    nested_path,
                    str(fragment.get("o") or fragment.get("op") or ""),
                    fragment.get("v"),
                    fragment_scoped=True,
                )
            return

        kind = _fragment_kind(fragment)
        self._current_kind = kind
        content = fragment.get("content")
        if not isinstance(content, str):
            content = fragment.get("thinking_content") if kind == KIND_THINKING else None
        if isinstance(content, str) and content:
            self._merge(kind, content)

    def _append_delta(self, text: str) -> None:
        """把一段裸增量归到"当前片段类型"。"""

        if not text:
            return
        self._merge(self._current_kind, text)

    # ─── 文本合并 ───────────────────────────────────────────────────────

    def _merge(self, kind: str, text: str) -> None:
        """把新文本合并进对应桶，兼容"全量快照"和"纯增量"两种上游。

        规则：
        - 新文本以当前文本为前缀 → 是新快照，直接替换（更完整）；
        - 当前文本以新文本为前缀 / 已包含该文本 → 重复片段，忽略；
        - 其它 → 视为增量追加。
        """

        if kind == KIND_IGNORED:
            # 搜索/工具片段（如 "Found 19 web pages"）只丢弃，不参与正文/思考。
            self._snapshot.ignored_values += 1
            return

        current = self._texts[kind]
        if not text:
            return
        if not current:
            self._texts[kind] = text
        elif text.startswith(current):
            self._texts[kind] = text
        elif current.startswith(text) or current.endswith(text):
            return
        else:
            self._texts[kind] = current + text

        if kind == KIND_CONTENT:
            self._snapshot.content_fragments += 1
        else:
            self._snapshot.thinking_fragments += 1

    def _consume_usage(self, node: dict[str, Any]) -> None:
        """读取累计 token 用量（网页版只给总量）。"""

        for key in _USAGE_KEYS:
            if key in node:
                try:
                    self._snapshot.total_tokens = int(node[key] or 0)
                except (TypeError, ValueError):
                    pass

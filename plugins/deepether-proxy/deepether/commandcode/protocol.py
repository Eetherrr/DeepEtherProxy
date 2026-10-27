"""CommandCode `/alpha/generate` 的请求信封、NDJSON 事件与错误分类。

这一层只做"协议"，不碰 Host 的 `context_items`，也不碰插件配置；两侧分别由
`adapter_in` / `adapter_out` 与 `provider` 负责，便于单测把协议错误与业务错误分开定位。
"""

from __future__ import annotations

import codecs
import json
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Iterator, Mapping

from ..errors import DeepEtherError, ErrorCode
from .constants import (
    BUDGET_MARKERS,
    CLOUDFLARE_MARKERS,
    CONTEXT_LIMIT_MARKERS,
    MAX_EVENT_LINE_BYTES,
    MAX_OUTPUT_TOKENS,
    MAX_STREAM_BYTES,
    MODEL_NOT_IN_PLAN_PREFIX,
    PERMISSION_MODE_STANDARD,
    RENEWAL_MARKERS,
    REQUEST_TOO_LARGE_MARKERS,
    TRUNCATED_FINISH_REASONS,
    UPGRADE_REQUIRED_MARKER,
)

__all__ = [
    "CcToolCall",
    "CcUsage",
    "CcResult",
    "NdjsonSplitter",
    "build_envelope",
    "detect_environment",
    "clean_surrogates",
    "map_http_error",
    "map_stream_error",
    "normalize_finish_reason",
    "usage_from_event",
]


def clean_surrogates(value: str) -> str:
    """去掉孤立 UTF-16 surrogate，保留成对 emoji。

    Python 的 `json.dumps` 会把 `\\ud83d` 这样的半字符原样输出，严格 JSON 解码器会直接拒收。
    这里只处理"编不回去"的字符：正常 emoji 仍能通过 UTF-8 编码。

    Args:
        value: 原始字符串。

    Returns:
        str: 可安全编码成 UTF-8 的字符串。
    """

    if not value:
        return value
    try:
        value.encode("utf-8")
        return value
    except UnicodeEncodeError:
        return value.encode("utf-8", "replace").decode("utf-8")


def clean_surrogates_deep(value: Any) -> Any:
    """递归清理容器里所有字符串的孤立 surrogate。"""

    if isinstance(value, str):
        return clean_surrogates(value)
    if isinstance(value, list):
        return [clean_surrogates_deep(item) for item in value]
    if isinstance(value, tuple):
        return tuple(clean_surrogates_deep(item) for item in value)
    if isinstance(value, dict):
        return {clean_surrogates_deep(key): clean_surrogates_deep(item) for key, item in value.items()}
    return value


def detect_environment() -> str:
    """按当前平台推断信封里的 `environment`（`platform-arch`）。"""

    platform = sys.platform
    machine = ""
    try:  # pragma: no cover - 取决于运行平台
        import platform as platform_module

        machine = platform_module.machine().lower()
    except Exception:  # noqa: BLE001 - 探测失败时用空串，交给默认值
        machine = ""
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if platform.startswith("win"):
        return f"win32-{arch}"
    if platform == "darwin":
        return f"darwin-{arch}"
    return f"linux-{arch}"


def build_envelope(
    *,
    model: str,
    messages: list[dict[str, Any]],
    system: str,
    max_tokens: int,
    thread_id: str,
    working_dir: str,
    environment: str,
    temperature: float | None = None,
    reasoning_effort: str = "",
    tools: list[dict[str, Any]] | None = None,
    today: str = "",
) -> dict[str, Any]:
    """构造 `/alpha/generate` 的请求信封（zod 强校验，缺字段直接 400）。"""

    params: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "system": system,
        "max_tokens": max(1, min(int(max_tokens), MAX_OUTPUT_TOKENS)),
        "stream": True,
    }
    if temperature is not None:
        params["temperature"] = float(temperature)
    if reasoning_effort:
        params["reasoning_effort"] = reasoning_effort
    if tools:
        params["tools"] = tools
    return {
        "config": {
            "workingDir": working_dir or ".",
            "date": today or date.today().isoformat(),
            "environment": environment or detect_environment(),
            "structure": [],
            "isGitRepo": False,
            "currentBranch": "",
            "mainBranch": "",
            "gitStatus": "",
            "recentCommits": [],
        },
        "memory": "",
        "taste": None,
        "skills": None,
        "permissionMode": PERMISSION_MODE_STANDARD,
        # 网关用 zod 校验 threadId 必须是 UUID；空串会直接 400（真 Key 实测）。
        "threadId": thread_id or str(uuid.uuid4()),
        "params": params,
    }


class NdjsonSplitter:
    """把字节流切成一行一个 JSON（没有 `data:` 前缀，绝不能按 SSE 解析）。

    处理跨 chunk 的半行、CRLF、空行、末尾缺换行；单行或总响应超限时按协议错误中止，
    避免异常上游把内存吃穿。
    """

    def __init__(
        self,
        *,
        max_line_bytes: int = MAX_EVENT_LINE_BYTES,
        max_total_bytes: int = MAX_STREAM_BYTES,
    ) -> None:
        """初始化解析器。

        Args:
            max_line_bytes: 单行上限（字节）。
            max_total_bytes: 整次响应上限（字节）。
        """

        self._max_line = max(1024, int(max_line_bytes))
        self._max_total = max(1024, int(max_total_bytes))
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._total = 0

    def feed(self, chunk: bytes) -> list[dict[str, Any]]:
        """喂入一段字节，返回本次能完整解析出的事件。"""

        self._total += len(chunk)
        if self._total > self._max_total:
            raise DeepEtherError(
                ErrorCode.PROTOCOL_ERROR,
                "上游响应超过插件允许的最大体积，已中止解析",
                detail=f"total>{self._max_total}",
            )
        self._buffer += self._decoder.decode(chunk)
        return self._drain(final=False)

    def close(self) -> list[dict[str, Any]]:
        """结束输入：处理最后一行没有换行符的情况。"""

        self._buffer += self._decoder.decode(b"", final=True)
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> list[dict[str, Any]]:
        """按换行切分缓冲区并解析。"""

        events: list[dict[str, Any]] = []
        while True:
            index = self._buffer.find("\n")
            if index < 0:
                break
            raw = self._buffer[:index]
            self._buffer = self._buffer[index + 1 :]
            event = self._parse_line(raw)
            if event is not None:
                events.append(event)
        if final and self._buffer:
            event = self._parse_line(self._buffer)
            self._buffer = ""
            if event is not None:
                events.append(event)
        if len(self._buffer.encode("utf-8", "replace")) > self._max_line:
            raise DeepEtherError(
                ErrorCode.PROTOCOL_ERROR,
                "上游单行事件超过插件允许的最大长度，已中止解析",
                detail=f"line>{self._max_line}",
            )
        return events

    @staticmethod
    def _parse_line(raw: str) -> dict[str, Any] | None:
        """解析一行；空行与 `data:` 前缀（若有）都要容错。"""

        line = raw.strip()
        if not line:
            return None
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
        try:
            payload = json.loads(line)
        except ValueError as exc:
            raise DeepEtherError(
                ErrorCode.PROTOCOL_ERROR,
                "上游返回了无法解析的事件行（协议可能已变更）",
                detail=str(exc),
            ) from exc
        if not isinstance(payload, dict):
            return None
        return payload


@dataclass
class CcToolCall:
    """上游给出的一个工具调用（最终 `tool-call` 事件）。"""

    call_id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    raw_arguments: Any = None
    truncated: bool = False


@dataclass
class CcUsage:
    """上游用量（`finish.totalUsage` 优先，`finish-step.usage` 兜底）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0

    def to_host(self) -> dict[str, int]:
        """转换成 Host `UsageRecord` 认识的字段名。"""

        return {
            "prompt_tokens": max(0, self.input_tokens),
            "completion_tokens": max(0, self.output_tokens),
            "total_tokens": max(0, self.total_tokens or (self.input_tokens + self.output_tokens)),
            "prompt_cache_hit_tokens": max(0, self.cache_read_tokens),
            "prompt_cache_miss_tokens": max(0, self.input_tokens - self.cache_read_tokens),
        }


def usage_from_event(payload: Mapping[str, Any] | None) -> CcUsage:
    """把上游 usage / totalUsage 对象转成 :class:`CcUsage`。

    字段缺失、类型错误都按 0 处理：用量只影响成本统计，不能因为它把一次成功请求判失败。
    """

    if not isinstance(payload, Mapping):
        return CcUsage()

    def _int(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    input_details = payload.get("inputTokenDetails")
    output_details = payload.get("outputTokenDetails")
    if not isinstance(input_details, Mapping):
        input_details = {}
    if not isinstance(output_details, Mapping):
        output_details = {}
    input_tokens = _int(payload.get("inputTokens"))
    output_tokens = _int(payload.get("outputTokens"))
    return CcUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=_int(input_details.get("cacheReadTokens", payload.get("cachedInputTokens"))),
        cache_write_tokens=_int(input_details.get("cacheWriteTokens")),
        reasoning_tokens=_int(output_details.get("reasoningTokens", payload.get("reasoningTokens"))),
        total_tokens=_int(payload.get("totalTokens")),
    )


def normalize_finish_reason(value: Any) -> str:
    """把上游 finishReason 归一成小写字符串。"""

    return str(value or "").strip().lower().replace("_", "-")


def is_truncated(finish_reason: str, *, tool_error: bool) -> bool:
    """判断本次结果是否被截断（工具参数可能不完整）。"""

    normalized = normalize_finish_reason(finish_reason)
    return tool_error or normalized in {item.replace("_", "-") for item in TRUNCATED_FINISH_REASONS}


@dataclass
class CcResult:
    """一次 `/alpha/generate` 调用累积出的结果。"""

    content: str = ""
    reasoning: str = ""
    tool_calls: list[CcToolCall] = field(default_factory=list)
    usage: CcUsage = field(default_factory=CcUsage)
    finish_reason: str = ""
    raw_finish_reason: str = ""
    provider_metadata: dict[str, Any] = field(default_factory=dict)
    events: dict[str, int] = field(default_factory=dict)
    tool_error: bool = False
    rounds: int = 1

    @property
    def truncated(self) -> bool:
        """结果是否被截断。"""

        return is_truncated(self.finish_reason, tool_error=self.tool_error)

    @property
    def has_payload(self) -> bool:
        """是否至少产出了正文或工具调用。"""

        return bool(self.content.strip() or self.tool_calls)

    def merge(self, other: "CcResult") -> None:
        """把 `pause_turn` 续跑的另一轮结果合并进来。"""

        if other.content:
            self.content += other.content
        if other.reasoning:
            self.reasoning += other.reasoning
        known = {call.call_id for call in self.tool_calls}
        for call in other.tool_calls:
            if call.call_id and call.call_id not in known:
                self.tool_calls.append(call)
                known.add(call.call_id)
        self.usage = CcUsage(
            input_tokens=self.usage.input_tokens + other.usage.input_tokens,
            output_tokens=self.usage.output_tokens + other.usage.output_tokens,
            cache_read_tokens=self.usage.cache_read_tokens + other.usage.cache_read_tokens,
            cache_write_tokens=self.usage.cache_write_tokens + other.usage.cache_write_tokens,
            reasoning_tokens=self.usage.reasoning_tokens + other.usage.reasoning_tokens,
            total_tokens=max(
                self.usage.total_tokens + other.usage.total_tokens,
                self.usage.input_tokens + other.usage.input_tokens + self.usage.output_tokens + other.usage.output_tokens,
            ),
        )
        self.finish_reason = other.finish_reason or self.finish_reason
        self.raw_finish_reason = other.raw_finish_reason or self.raw_finish_reason
        self.tool_error = self.tool_error or other.tool_error
        if other.provider_metadata:
            self.provider_metadata = other.provider_metadata
        for key, value in other.events.items():
            self.events[key] = self.events.get(key, 0) + value
        self.rounds += other.rounds


def _event_error_fields(payload: Mapping[str, Any]) -> tuple[int | None, str, str]:
    """从 HTTP 错误体或流内 error 事件里提取 `(status, code, message)`。"""

    status_raw = payload.get("statusCode", payload.get("status"))
    try:
        status = int(status_raw) if status_raw is not None else None
    except (TypeError, ValueError):
        status = None
    error = payload.get("error")
    code = ""
    message = ""
    if isinstance(error, Mapping):
        code = str(error.get("code") or error.get("type") or "")
        message = str(error.get("message") or "")
    elif isinstance(error, str):
        message = error
    if not message:
        message = str(payload.get("message") or payload.get("msg") or "")
    if not code:
        code = str(payload.get("code") or payload.get("type") or "")
    return status, code, message


def _looks_like_html(text: str) -> bool:
    """判断响应体是否是 Cloudflare / 网关的 HTML 页面。"""

    lowered = text.strip().lower()
    return any(marker in lowered for marker in CLOUDFLARE_MARKERS)


def map_http_error(status: int, body: str) -> DeepEtherError:
    """把 HTTP 错误响应映射成带中文说明的 :class:`DeepEtherError`。

    Args:
        status: HTTP 状态码。
        body: 响应体原文（可能为空）。

    Returns:
        DeepEtherError: 已分类的错误。
    """

    detail = body.strip().replace("\n", " ")[:600]
    payload: Mapping[str, Any] = {}
    if detail.startswith("{"):
        try:
            parsed = json.loads(detail)
            if isinstance(parsed, Mapping):
                payload = parsed
        except ValueError:
            payload = {}
    _, code, message = _event_error_fields(payload)
    haystack = f"{code} {message} {detail}".lower()

    if _looks_like_html(body):
        return DeepEtherError(
            ErrorCode.UPSTREAM_BLOCKED,
            "请求被上游边缘防护（Cloudflare）拦截，请检查网络环境，不要连续重试",
            detail=f"HTTP {status}",
            status=status,
        )
    if status == 401:
        return DeepEtherError(
            ErrorCode.CREDENTIAL_INVALID,
            "CommandCode Key 已失效，请在 Studio 重新生成后填入插件配置",
            detail=f"HTTP 401 {message}".strip(),
            status=status,
        )
    if UPGRADE_REQUIRED_MARKER in haystack:
        return DeepEtherError(
            ErrorCode.PLAN_NOT_SUPPORTED,
            "当前套餐不支持该调用方式（Go 套餐只能走 CLI 网关，不能打 Provider API）",
            detail=detail,
            status=status,
        )
    if MODEL_NOT_IN_PLAN_PREFIX.lower() in haystack:
        return DeepEtherError(
            ErrorCode.MODEL_NOT_ALLOWED,
            "该模型不在当前 CommandCode 套餐内，请在 WebUI 换一个已注册的 cc- 模型",
            detail=detail,
            status=status,
        )
    if any(marker in haystack for marker in RENEWAL_MARKERS):
        return DeepEtherError(
            ErrorCode.BILLING_PENDING,
            "CommandCode 订阅续费处理中，credits 到账后恢复",
            detail=detail,
            status=status,
        )
    if any(marker in haystack for marker in BUDGET_MARKERS):
        return DeepEtherError(
            ErrorCode.BUDGET_EXHAUSTED,
            "CommandCode 额度用尽，请等待窗口重置或充值",
            detail=detail,
            status=status,
        )
    if any(marker in haystack for marker in REQUEST_TOO_LARGE_MARKERS):
        return DeepEtherError(
            ErrorCode.REQUEST_TOO_LARGE,
            "请求结构过大（消息/工具/图片太多），请压缩历史或开新会话",
            detail=detail,
            status=status,
        )
    if any(marker in haystack for marker in CONTEXT_LIMIT_MARKERS):
        return DeepEtherError(
            ErrorCode.CONTEXT_TOO_LONG,
            "上下文超过模型上限，请缩短历史",
            detail=detail,
            status=status,
        )
    if status == 429:
        return DeepEtherError(
            ErrorCode.UPSTREAM_HTTP,
            "触发 CommandCode 限流，已进入冷却，请稍后再试",
            detail=detail,
            status=status,
            retryable=True,
        )
    if status >= 500:
        return DeepEtherError(
            ErrorCode.UPSTREAM_HTTP,
            "CommandCode 上游暂时不可用，请稍后再试",
            detail=f"HTTP {status} {message}".strip(),
            status=status,
            retryable=True,
        )
    if status == 403:
        return DeepEtherError(
            ErrorCode.UPSTREAM_BLOCKED,
            "CommandCode 拒绝了本次请求（403），请查看日志里的上游说明",
            detail=detail,
            status=status,
        )
    return DeepEtherError(
        ErrorCode.PROTOCOL_ERROR,
        "CommandCode 返回了未预期的错误响应",
        detail=f"HTTP {status} {detail}".strip(),
        status=status,
    )


def map_stream_error(payload: Mapping[str, Any]) -> DeepEtherError:
    """把流内 `error` 事件映射成插件错误。"""

    status, code, message = _event_error_fields(payload)
    synthetic_body = json.dumps({"error": {"code": code, "message": message}}, ensure_ascii=False)
    if status is not None:
        return map_http_error(status, synthetic_body)
    haystack = f"{code} {message}".lower()
    if MODEL_NOT_IN_PLAN_PREFIX.lower() in haystack:
        return DeepEtherError(
            ErrorCode.MODEL_NOT_ALLOWED,
            "该模型不在当前 CommandCode 套餐内，请在 WebUI 换一个已注册的 cc- 模型",
            detail=message,
        )
    if any(marker in haystack for marker in BUDGET_MARKERS):
        return DeepEtherError(
            ErrorCode.BUDGET_EXHAUSTED,
            "CommandCode 额度用尽，请等待窗口重置或充值",
            detail=message,
        )
    return DeepEtherError(
        ErrorCode.PROTOCOL_ERROR,
        "CommandCode 在流中返回了错误事件",
        detail=(code + " " + message).strip() or "(无说明)",
        retryable=bool(payload.get("isRetryable")),
    )


def iter_json_events(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """逐行解析字符串事件（测试与离线 fixture 用）。"""

    splitter = NdjsonSplitter()
    for line in lines:
        for event in splitter.feed((line + "\n").encode("utf-8")):
            yield event
    for event in splitter.close():
        yield event

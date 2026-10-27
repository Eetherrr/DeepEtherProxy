"""CommandCode `/alpha/generate` 的 HTTP 客户端与 NDJSON 流读取。"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Mapping, Final

import httpx

from ..errors import DeepEtherError, ErrorCode
from .constants import (
    CLI_ENVIRONMENT_VALUE,
    DEFAULT_BASE_URL,
    DEFAULT_CLI_VERSION,
    DEFAULT_PROJECT_SLUG,
    HEADER_AUTHORIZATION,
    HEADER_CLI_ENVIRONMENT,
    HEADER_CLI_VERSION,
    HEADER_CONTENT_TYPE,
    HEADER_PROJECT_SLUG,
    HEADER_SESSION_ID,
    HEADER_TASTE_LEARNING,
    HEADER_USER_AGENT,
    PATH_GENERATE,
    USER_AGENT_VALUE,
)
from .protocol import (
    CcResult,
    CcToolCall,
    NdjsonSplitter,
    map_http_error,
    map_stream_error,
    normalize_finish_reason,
    usage_from_event,
)

_DEFAULT_READ_TIMEOUT: Final[float] = 285.0
_DEFAULT_CONNECT_TIMEOUT: Final[float] = 15.0


def _coerce_arguments(raw: Any) -> tuple[dict[str, Any], bool]:
    """把工具参数归一成 dict；返回 `(参数, 是否失败)`。"""

    if isinstance(raw, dict):
        return raw, False
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}, True
        if isinstance(parsed, dict):
            return parsed, False
        return {}, True
    if raw is None:
        return {}, False
    return {}, True


class CommandCodeClient:
    """一个 Key、一套重试语义、一个 HTTP 连接池。

    插件 Provider 不支持 Host 侧流式，所以这里虽然逐行读取，但只在结束时返回完整结果。
    """

    def __init__(
        self,
        *,
        api_key_provider: Callable[[], str],
        logger: Any,
        base_url: str = DEFAULT_BASE_URL,
        cli_version: str = DEFAULT_CLI_VERSION,
        project_slug: str = DEFAULT_PROJECT_SLUG,
        taste_learning: bool = False,
        timeout_sec: float = _DEFAULT_READ_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        """初始化客户端。

        Args:
            api_key_provider: 每次请求时读取当前 Key（支持配置热更新）。
            logger: 插件日志器。
            base_url: 上游根地址。
            cli_version: `x-command-code-version` 的取值，随官方 CLI 升级。
            project_slug: `x-project-slug` 的取值。
            taste_learning: `x-taste-learning` 的取值（默认关闭）。
            timeout_sec: 读超时（秒），必须小于 Host 侧 `api_provider.timeout`。
            transport: 可注入的 httpx transport（测试用）。
            http_client: 可注入的完整 httpx 客户端（测试用；此时不负责关闭）。
        """

        self._api_key_provider = api_key_provider
        self._logger = logger
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._cli_version = cli_version or DEFAULT_CLI_VERSION
        self._project_slug = project_slug or DEFAULT_PROJECT_SLUG
        self._taste_learning = bool(taste_learning)
        self._timeout_sec = max(5.0, float(timeout_sec))
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                self._timeout_sec,
                connect=_DEFAULT_CONNECT_TIMEOUT,
                write=_DEFAULT_CONNECT_TIMEOUT,
                pool=_DEFAULT_CONNECT_TIMEOUT,
            ),
            transport=transport,
            follow_redirects=False,
        )

    # ─── 生命周期 ───────────────────────────────────────────────────────

    async def aclose(self) -> None:
        """关闭连接池（仅关闭自己创建的客户端）。"""

        if self._owns_client:
            await self._client.aclose()

    def update_options(
        self,
        *,
        cli_version: str,
        project_slug: str,
        taste_learning: bool,
        timeout_sec: float,
    ) -> None:
        """配置热更新时刷新可变项（Key 由 provider 在每次调用时读取）。"""

        self._cli_version = cli_version or DEFAULT_CLI_VERSION
        self._project_slug = project_slug or DEFAULT_PROJECT_SLUG
        self._taste_learning = bool(taste_learning)
        self._timeout_sec = max(5.0, float(timeout_sec))
        self._client.timeout = httpx.Timeout(
            self._timeout_sec,
            connect=_DEFAULT_CONNECT_TIMEOUT,
            write=_DEFAULT_CONNECT_TIMEOUT,
            pool=_DEFAULT_CONNECT_TIMEOUT,
        )

    # ─── 请求 ───────────────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        """构造与官方 CLI 形状一致的请求头（不含设备指纹）。"""

        key = (self._api_key_provider() or "").strip()
        return {
            HEADER_AUTHORIZATION: f"Bearer {key}",
            HEADER_CONTENT_TYPE: "application/json",
            HEADER_USER_AGENT: USER_AGENT_VALUE,
            HEADER_CLI_VERSION: self._cli_version,
            HEADER_CLI_ENVIRONMENT: CLI_ENVIRONMENT_VALUE,
            HEADER_PROJECT_SLUG: self._project_slug,
            HEADER_TASTE_LEARNING: "true" if self._taste_learning else "false",
            HEADER_SESSION_ID: str(uuid.uuid4()),
        }

    async def generate(self, envelope: Mapping[str, Any], *, timeout_sec: float | None = None) -> CcResult:
        """调用 `/alpha/generate` 并读完事件流。

        Args:
            envelope: 由 :func:`build_envelope` 构造的请求体。
            timeout_sec: 本次读超时（秒）；为空时用客户端默认值。

        Returns:
            CcResult: 累积后的正文、reasoning、工具调用与用量。

        Raises:
            DeepEtherError: 网络、HTTP 或流内错误，均带中文说明。
        """

        read_timeout = max(5.0, float(timeout_sec or self._timeout_sec))
        url = self._base_url + PATH_GENERATE
        try:
            async with self._client.stream(
                "POST",
                url,
                json=envelope,
                headers=self._headers(),
                timeout=httpx.Timeout(
                    read_timeout,
                    connect=_DEFAULT_CONNECT_TIMEOUT,
                    write=_DEFAULT_CONNECT_TIMEOUT,
                    pool=_DEFAULT_CONNECT_TIMEOUT,
                ),
            ) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", "replace")
                    raise map_http_error(response.status_code, body)
                return await self._consume_stream(response)
        except httpx.TimeoutException as exc:
            raise DeepEtherError(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"等待 CommandCode 响应超时（{read_timeout:.0f}s），可调大 timeout",
                detail=type(exc).__name__,
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise DeepEtherError(
                ErrorCode.UPSTREAM_HTTP,
                "连接 CommandCode 失败，请检查网络或代理设置",
                detail=f"{type(exc).__name__}: {exc}",
                retryable=True,
            ) from exc

    async def _consume_stream(self, response: httpx.Response) -> CcResult:
        """逐块解析 NDJSON 事件并累积结果。"""

        result = CcResult()
        splitter = NdjsonSplitter()
        finish_usage_seen = False
        async for chunk in response.aiter_bytes():
            for event in splitter.feed(chunk):
                self._handle_event(event, result)
                if event.get("type") == "finish":
                    finish_usage_seen = True
        for event in splitter.close():
            self._handle_event(event, result)
            if event.get("type") == "finish":
                finish_usage_seen = True
        if not finish_usage_seen and result.finish_reason == "":
            # 没收到 finish 说明流被截断（网关超时/连接中断），不能当成功。
            raise DeepEtherError(
                ErrorCode.PROTOCOL_ERROR,
                "CommandCode 事件流提前结束（没有收到 finish 事件）",
                detail=f"events={result.events}",
                retryable=True,
            )
        return result

    def _handle_event(self, event: Mapping[str, Any], result: CcResult) -> None:
        """处理单个事件。"""

        event_type = str(event.get("type") or "")
        result.events[event_type] = result.events.get(event_type, 0) + 1

        if event_type == "text-delta":
            text = event.get("text", event.get("delta"))
            if isinstance(text, str):
                result.content += text
            return
        if event_type == "reasoning-delta":
            text = event.get("text", event.get("delta"))
            if isinstance(text, str):
                result.reasoning += text
            return
        if event_type == "tool-call":
            call = self._parse_tool_call(event)
            if call is not None:
                # 同一个 callId 重复出现时以最后一条完整对象为准。
                result.tool_calls = [item for item in result.tool_calls if item.call_id != call.call_id]
                result.tool_calls.append(call)
            return
        if event_type == "tool-error":
            result.tool_error = True
            self._logger.warning(
                "CommandCode 工具参数异常（%s）：%s",
                event.get("toolName") or event.get("toolCallId") or "?",
                str(event.get("error") or event.get("message") or "")[:200],
            )
            return
        if event_type == "finish-step":
            if not result.usage.input_tokens and not result.usage.output_tokens:
                result.usage = usage_from_event(event.get("usage"))
            return
        if event_type == "finish":
            result.usage = usage_from_event(event.get("totalUsage") or event.get("usage"))
            result.finish_reason = normalize_finish_reason(
                event.get("rawFinishReason") or event.get("finishReason")
            )
            result.raw_finish_reason = str(
                event.get("rawFinishReason") or event.get("finishReason") or ""
            )
            return
        if event_type == "provider-metadata":
            metadata = event.get("providerMetadata")
            if isinstance(metadata, dict):
                result.provider_metadata = metadata
            return
        if event_type == "error":
            raise map_stream_error(event)

    @staticmethod
    def _parse_tool_call(event: Mapping[str, Any]) -> CcToolCall | None:
        """解析 `tool-call` 事件（兼容多种字段名）。"""

        call_id = str(event.get("toolCallId") or event.get("id") or "").strip()
        name = str(event.get("toolName") or event.get("name") or "").strip()
        if not name:
            return None
        if not call_id:
            call_id = "call_" + uuid.uuid4().hex
        raw = event.get("input", event.get("args", event.get("arguments")))
        arguments, broken = _coerce_arguments(raw)
        return CcToolCall(
            call_id=call_id,
            name=name,
            arguments=arguments,
            raw_arguments=raw,
            truncated=broken,
        )

    # ─── 只读接口（状态命令用，失败不影响主流程）────────────────────────

    async def get_json(self, path: str, *, timeout_sec: float = 20.0) -> dict[str, Any]:
        """读一个只读接口（credits / whoami）。

        Raises:
            DeepEtherError: 请求失败或返回非 JSON 对象。
        """

        url = self._base_url + path
        try:
            response = await self._client.get(url, headers=self._headers(), timeout=timeout_sec)
        except httpx.HTTPError as exc:
            raise DeepEtherError(
                ErrorCode.UPSTREAM_HTTP,
                "读取 CommandCode 账户信息失败",
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        body = response.text
        if response.status_code >= 400:
            raise map_http_error(response.status_code, body)
        try:
            payload = response.json()
        except ValueError as exc:
            raise DeepEtherError(
                ErrorCode.PROTOCOL_ERROR,
                "CommandCode 账户接口返回的不是 JSON",
                detail=body[:200],
            ) from exc
        if not isinstance(payload, dict):
            raise DeepEtherError(ErrorCode.PROTOCOL_ERROR, "CommandCode 账户接口返回结构异常")
        return payload

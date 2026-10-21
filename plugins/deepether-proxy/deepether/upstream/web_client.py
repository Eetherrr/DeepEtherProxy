"""DeepSeek 网页版上游客户端。

一次完整调用 = 建会话 → 领 PoW 挑战 → 本地求解 → POST completion 读 SSE 增量。
本模块只负责 HTTP 与流式读取，把结果交给 `SseAccumulator`；错误统一转成中文
`DeepEtherError`，方便上层决定"重试 / 降级 / 提示用户"。
"""

from pathlib import Path
from typing import Any, Callable, Final

import asyncio
import json
import time

import httpx

from ..constants import (
    BROWSER_XHR_HEADERS,
    DEFAULT_MODEL_TYPE,
    FIELD_CODE,
    FIELD_MESSAGE,
    HEADER_NAME_AUTHORIZATION,
    HEADER_NAME_ORIGIN,
    HEADER_NAME_POW_RESPONSE,
    HEADER_NAME_REFERER,
    HEADER_NAME_USER_AGENT,
    ORIGIN_VALUE,
    REFERER_VALUE,
    USER_AGENT_VALUE,
    PATH_CHAT_COMPLETION,
    PATH_CHAT_SESSION_CREATE,
    PATH_POW_CHALLENGE,
    PATH_USER_CURRENT,
    UPSTREAM_AUTH_ERROR_CODES,
    UPSTREAM_AUTH_ERROR_MARKERS,
    UPSTREAM_BASE_URL,
)
from ..antiban import BanKind, classify_failure
from ..errors import DeepEtherError, ErrorCode
from .pow import solve_pow
from .sse import SseAccumulator, StreamSnapshot

_BODY_SNIPPET_LIMIT: Final[int] = 400
_CONNECT_TIMEOUT_SEC: Final[float] = 10.0

try:  # 可选依赖：装了 h2 才能用 HTTP/2（Chrome 一律 h2，直连 h1.1 是弱指纹）
    import h2 as _h2  # noqa: F401

    _HTTP2_AVAILABLE: bool = True
except Exception:  # pragma: no cover - 取决于运行环境
    _HTTP2_AVAILABLE = False


class WebUpstreamClient:
    """网页版上游客户端（每个插件实例一个）。"""

    def __init__(
        self,
        *,
        token_provider: Callable[[], str],
        on_credential_invalid: Callable[[str], None],
        logger: Any,
        dump_dir: Path | None = None,
        verbose: bool = False,
        base_url: str = UPSTREAM_BASE_URL,
    ) -> None:
        """初始化客户端。

        Args:
            token_provider: 返回当前网页版 token 的回调。
            on_credential_invalid: 判定凭证失效时的回调（用于落盘标记）。
            logger: 插件日志器。
            dump_dir: 需要落盘原始报文时使用的目录。
            verbose: 是否打印上游各阶段耗时。
            base_url: 上游根地址。
        """

        self._token_provider = token_provider
        self._on_credential_invalid = on_credential_invalid
        self._logger = logger
        self._dump_dir = dump_dir
        self._verbose = verbose
        self._base_url = base_url.rstrip("/")
        self._client: httpx.AsyncClient | None = None
        # 会话复用：网页版正常用户是在同一个会话里连续追问，而"每轮都新建会话"
        # 会留下"一天建几百个会话"的机器行为痕迹（docs/ANTIBAN.md 措施 4）。
        self._session_id: str | None = None
        self._session_uses: int = 0
        self._session_reuse_calls: int = 8

    # ─── 生命周期 ───────────────────────────────────────────────────────

    def _ensure_client(self) -> httpx.AsyncClient:
        """返回复用的 HTTP 客户端。"""

        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                follow_redirects=True,
                http2=_HTTP2_AVAILABLE,
                timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT_SEC, read=180.0, write=30.0, pool=10.0),
            )
        return self._client

    def update_runtime_options(
        self,
        *,
        dump_dir: Path | None,
        verbose: bool,
        session_reuse_calls: int | None = None,
    ) -> None:
        """配置热更新时同步落盘开关、详细日志开关与会话复用上限。

        Args:
            dump_dir: 需要落盘原始报文时使用的目录。
            verbose: 是否打印上游各阶段耗时。
            session_reuse_calls: 同一会话最多复用多少次；None 表示不改。
        """

        self._dump_dir = dump_dir
        self._verbose = verbose
        if session_reuse_calls is not None:
            self._session_reuse_calls = max(1, int(session_reuse_calls))

    async def close(self) -> None:
        """关闭底层 HTTP 连接池。"""

        client = self._client
        self._client = None
        self.invalidate_session()
        if client is not None:
            await client.aclose()

    # ─── 请求头 ─────────────────────────────────────────────────────────

    def _require_token(self) -> str:
        """取当前 token；为空时抛出"未授权"错误。"""

        token = (self._token_provider() or "").strip()
        if not token:
            raise DeepEtherError(
                ErrorCode.CREDENTIAL_MISSING,
                "尚未配置网页版凭证：请在插件配置的 credential.web_token 填入 token（发送 /deepether-status 可看步骤）",
            )
        return token

    def _headers(self, *, accept: str = "application/json") -> dict[str, str]:
        """构造上游请求头。

        除鉴权与来源头外，这里补齐浏览器 XHR 会带的一套指纹头
        （Accept-Language / sec-ch-ua* / sec-fetch-*，见 constants.BROWSER_XHR_HEADERS）。
        只发 Authorization + User-Agent 的请求在服务端看来就是"裸脚本"，
        是风控最容易识别的一类特征（详见 docs/ANTIBAN.md）。

        Args:
            accept: Accept 头取值；读 SSE 时传 text/event-stream。

        Returns:
            dict[str, str]: 完整请求头。
        """

        headers = dict(BROWSER_XHR_HEADERS)
        headers.update(
            {
                HEADER_NAME_AUTHORIZATION: f"Bearer {self._require_token()}",
                HEADER_NAME_USER_AGENT: USER_AGENT_VALUE,
                HEADER_NAME_ORIGIN: ORIGIN_VALUE,
                HEADER_NAME_REFERER: REFERER_VALUE,
                "Accept": accept,
                "Content-Type": "application/json",
            }
        )
        return headers

    # ─── 基础请求 ───────────────────────────────────────────────────────

    def _handle_error_status(self, status: int, body_text: str, stage: str) -> None:
        """把非 2xx 响应翻译成中文错误。

        Args:
            status: HTTP 状态码。
            body_text: 响应体片段。
            stage: 出错阶段（用于日志与错误说明）。

        Raises:
            DeepEtherError: 总是抛出，便于调用方直接 `raise ... from`。
        """

        snippet = (body_text or "")[: _BODY_SNIPPET_LIMIT]
        if status in (401, 403):
            # 401 基本就是 token 过期；403 还可能是"账号被风控封禁"，两者处置完全不同：
            # 前者重新取 token 即可，后者继续重试只会加长封禁（docs/ANTIBAN.md 措施 5）。
            if status == 403 and classify_failure(status=status, body=snippet) == BanKind.BANNED:
                raise DeepEtherError(
                    ErrorCode.ACCOUNT_RISK,
                    "上游返回账号级封禁/风控提示，请停止自动重试并在浏览器里确认账号状态",
                    detail=f"{stage} HTTP {status}: {snippet}",
                )
            self._on_credential_invalid(f"上游 {stage} 返回 {status}")
            raise DeepEtherError(
                ErrorCode.CREDENTIAL_INVALID,
                "网页版 token 已失效：请从浏览器控制台重新获取后更新插件配置的 credential.web_token",
                detail=f"{stage} HTTP {status}: {snippet}",
            )
        if status == 429:
            raise DeepEtherError(
                ErrorCode.UPSTREAM_HTTP,
                "网页版接口返回 429，触发风控或频率限制，请稍后重试",
                detail=snippet,
                retryable=True,
            )
        if "40301" in snippet or "INVALID_POW" in snippet.upper():
            raise DeepEtherError(
                ErrorCode.POW_FAILED,
                "PoW 校验未通过（可能算法已升级或挑战已过期）",
                detail=snippet,
                retryable=True,
            )
        raise DeepEtherError(
            ErrorCode.UPSTREAM_HTTP,
            f"上游 {stage} 返回 HTTP {status}",
            detail=snippet,
            retryable=status >= 500,
        )

    @staticmethod
    def _is_auth_error(code: Any, message: str) -> bool:
        """判断业务错误码是否属于"凭证失效"。

        Args:
            code: 响应体里的业务错误码。
            message: 响应体里的说明文本。

        Returns:
            bool: 是否应判定为凭证失效。
        """

        if code in UPSTREAM_AUTH_ERROR_CODES:
            return True
        upper = (message or "").upper()
        return any(marker in upper for marker in UPSTREAM_AUTH_ERROR_MARKERS)

    def _raise_for_envelope(self, payload: dict[str, Any], stage: str) -> None:
        """检查上游 JSON 响应体里的业务错误码。

        上游约定：HTTP 200 也可能是失败，真正的结果在 body 的 code 字段里
        （docs/upstream-protocol.md 已记录 PoW 的 40301 就是这种形态）。
        这里统一把 code != 0 翻译成明确的中文错误，并且**只在鉴权类错误时**
        才把凭证标记为失效，避免把"协议漂移"误报成"需要重新授权"。

        Args:
            payload: 上游响应 JSON。
            stage: 出错阶段名，写进中文错误与日志。

        Raises:
            DeepEtherError: code 非 0 时总是抛出（调用方直接 raise ... from）。
        """

        code = payload.get(FIELD_CODE)
        if code is None or code == 0 or code == "0":
            return

        message = str(payload.get(FIELD_MESSAGE) or "")
        detail = json.dumps(payload, ensure_ascii=False)[: _BODY_SNIPPET_LIMIT]
        if self._is_auth_error(code, message):
            self._on_credential_invalid(f"上游 {stage} 返回 {code} {message}".strip())
            raise DeepEtherError(
                ErrorCode.CREDENTIAL_INVALID,
                "网页版 token 已失效：请从浏览器控制台重新获取后更新插件配置的 credential.web_token",
                detail=f"{stage} code={code} {message}: {detail}",
            )
        raise DeepEtherError(
            ErrorCode.PROTOCOL_ERROR,
            f"上游 {stage} 返回业务错误 code={code}：{message or '无说明'}",
            detail=detail,
        )

    async def _post_json(self, path: str, body: dict[str, Any], *, stage: str, timeout_sec: float = 30.0) -> dict[str, Any]:
        """发送一个 JSON POST 并解析响应。

        Args:
            path: 上游路径。
            body: 请求体。
            stage: 阶段名，用于错误说明。
            timeout_sec: 超时时间（秒）。

        Returns:
            dict[str, Any]: 响应 JSON。

        Raises:
            DeepEtherError: 网络失败、超时或响应非法时抛出。
        """

        client = self._ensure_client()
        started = time.monotonic()
        try:
            response = await client.post(path, json=body, headers=self._headers(), timeout=timeout_sec)
        except httpx.TimeoutException as exc:
            raise DeepEtherError(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"上游 {stage} 超时（{timeout_sec:.0f}s），可尝试调大 behavior.request_timeout_sec",
                detail=str(exc),
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise DeepEtherError(
                ErrorCode.UPSTREAM_HTTP,
                f"上游 {stage} 网络异常，请检查网络或代理",
                detail=str(exc),
                retryable=True,
            ) from exc

        if self._verbose:
            self._logger.info("上游 %s 耗时 %.0f ms（HTTP %d）", stage, (time.monotonic() - started) * 1000, response.status_code)

        if response.status_code != 200:
            self._handle_error_status(response.status_code, response.text, stage)
        try:
            payload = response.json()
        except ValueError as exc:
            raise DeepEtherError(
                ErrorCode.PROTOCOL_ERROR,
                f"上游 {stage} 返回的不是 JSON，协议可能已变更",
                detail=response.text[: _BODY_SNIPPET_LIMIT],
            ) from exc
        if not isinstance(payload, dict):
            raise DeepEtherError(
                ErrorCode.PROTOCOL_ERROR,
                f"上游 {stage} 返回结构异常",
                detail=str(payload)[: _BODY_SNIPPET_LIMIT],
            )
        self._raise_for_envelope(payload, stage)
        return payload

    # ─── 具体调用 ───────────────────────────────────────────────────────

    async def probe_user(self) -> None:
        """探活：请求当前用户信息，验证 token 是否仍有效。

        Raises:
            DeepEtherError: 凭证失效或网络失败时抛出。
        """

        client = self._ensure_client()
        try:
            response = await client.get(PATH_USER_CURRENT, headers=self._headers(), timeout=15.0)
        except httpx.HTTPError as exc:
            raise DeepEtherError(
                ErrorCode.UPSTREAM_HTTP,
                "探活请求失败，请检查网络",
                detail=str(exc),
                retryable=True,
            ) from exc
        if response.status_code != 200:
            self._handle_error_status(response.status_code, response.text, "users/current")
        # 实测：无效 token 也是 HTTP 200，body 里 code=40003。
        # 只看状态码会把"凭证已失效"判成"探活通过"（报告 §6.6.3 ①）。
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            self._raise_for_envelope(payload, "users/current")
        self._logger.info("凭证探活通过（HTTP 200）")

    async def acquire_session(self, *, reuse_limit: int | None = None) -> str:
        """返回可用的会话 id，优先复用已有会话。

        网页版真实用户是在同一个会话里连续追问；每调用一次就新建一个会话属于
        很显眼的机器行为（见 docs/ANTIBAN.md 措施 4）。所以这里复用最近一次会话，
        累计使用次数超过 reuse_limit 或上游报错后才换新会话。

        Args:
            reuse_limit: 同一会话最多复用多少次；None 表示用当前配置。

        Returns:
            str: 会话 id。

        Raises:
            DeepEtherError: 建会话失败时抛出。
        """

        limit = max(1, int(reuse_limit if reuse_limit is not None else self._session_reuse_calls))
        cached = self._session_id
        if cached and self._session_uses < limit:
            self._session_uses += 1
            return cached
        session_id = await self.create_session()
        self._session_id = session_id
        self._session_uses = 1
        return session_id

    def invalidate_session(self) -> None:
        """作废缓存的会话（上游报错、会话失效或关闭连接池时调用）。"""

        self._session_id = None
        self._session_uses = 0

    async def create_session(self) -> str:
        """创建一个网页版会话。

        Returns:
            str: `chat_session_id`。

        Raises:
            DeepEtherError: 上游未返回会话 id 时抛出。
        """

        payload = await self._post_json(PATH_CHAT_SESSION_CREATE, {}, stage="chat_session/create")
        session_id = self._extract_session_id(payload)
        if not session_id:
            raise DeepEtherError(
                ErrorCode.PROTOCOL_ERROR,
                "上游没有返回 chat_session_id，建会话接口可能已变更",
                detail=json.dumps(payload, ensure_ascii=False)[: _BODY_SNIPPET_LIMIT],
            )
        return session_id

    @staticmethod
    def _extract_session_id(payload: dict[str, Any]) -> str:
        """从建会话响应里抠出 session id。

        实测（2026-10-21）响应结构是：
        `{"code":0,"data":{"biz_code":0,"biz_data":{"chat_session":{"id":"..."}}}}`，
        所以这里做有限深度的递归，既兼容命名变化，又不会被无关的 id 字段误导。

        Args:
            payload: `chat_session/create` 的响应 JSON。

        Returns:
            str: 会话 id；找不到时返回空串。
        """

        def walk(node: Any, depth: int = 0) -> str:
            if depth > 5:
                return ""
            if isinstance(node, dict):
                for key in ("chat_session_id", "chatSessionId", "session_id"):
                    value = node.get(key)
                    if isinstance(value, str) and value:
                        return value
                for key in ("chat_session", "chatSession"):
                    nested = node.get(key)
                    if isinstance(nested, dict):
                        value = nested.get("id")
                        if isinstance(value, str) and value:
                            return value
                for value in node.values():
                    found = walk(value, depth + 1)
                    if found:
                        return found
            elif isinstance(node, list):
                for item in node:
                    found = walk(item, depth + 1)
                    if found:
                        return found
            return ""

        return walk(payload)

    async def complete(
        self,
        *,
        prompt: str,
        session_id: str,
        thinking_enabled: bool,
        search_enabled: bool,
        model_type: str = DEFAULT_MODEL_TYPE,
        timeout_sec: float = 180.0,
    ) -> StreamSnapshot:
        """发起一次对话补全并读完 SSE 流。

        Args:
            prompt: 拍平后的单条 prompt。
            session_id: 已创建的会话 id。
            thinking_enabled: 是否开启深度思考。
            search_enabled: 是否开启内置联网搜索。
            model_type: 网页版 model_type 参数。
            timeout_sec: 读超时（秒）。

        Returns:
            StreamSnapshot: 累积出的正文、思考内容与统计。

        Raises:
            DeepEtherError: 任意阶段失败时抛出。
        """

        challenge_payload = await self._post_json(
            PATH_POW_CHALLENGE,
            {"target_path": PATH_CHAT_COMPLETION},
            stage="chat/create_pow_challenge",
        )
        pow_header = await asyncio.to_thread(solve_pow, challenge_payload)
        if self._verbose:
            self._logger.info("PoW 已求解，准备发起 completion 请求")

        body = {
            "chat_session_id": session_id,
            "prompt": prompt,
            "thinking_enabled": bool(thinking_enabled),
            "search_enabled": bool(search_enabled),
            "model_type": model_type,
            "parent_message_id": None,
            "ref_file_ids": [],
        }
        headers = self._headers(accept="text/event-stream")
        headers[HEADER_NAME_POW_RESPONSE] = pow_header

        accumulator = SseAccumulator()
        raw_lines: list[str] = []
        client = self._ensure_client()
        started = time.monotonic()

        try:
            async with client.stream(
                "POST",
                PATH_CHAT_COMPLETION,
                json=body,
                headers=headers,
                timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT_SEC, read=timeout_sec, write=30.0, pool=10.0),
            ) as response:
                if response.status_code != 200:
                    body_text = (await response.aread()).decode("utf-8", errors="replace")
                    self._handle_error_status(response.status_code, body_text, "chat/completion")
                async for line in response.aiter_lines():
                    if self._dump_dir is not None:
                        raw_lines.append(line)
                    accumulator.feed_line(line)
                    if accumulator.snapshot.finished:
                        break
        except httpx.TimeoutException as exc:
            raise DeepEtherError(
                ErrorCode.UPSTREAM_TIMEOUT,
                f"等待网页版回答超时（{timeout_sec:.0f}s），深度思考耗时较长时可调大超时",
                detail=str(exc),
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise DeepEtherError(
                ErrorCode.UPSTREAM_HTTP,
                "读取网页版响应流时网络中断",
                detail=str(exc),
                retryable=True,
            ) from exc
        finally:
            if self._dump_dir is not None and raw_lines:
                self._dump_stream(raw_lines)

        snapshot = accumulator.snapshot
        if self._verbose:
            self._logger.info(
                "上游 completion 完成：耗时 %.0f ms，正文 %d 片 / 思考 %d 片，忽略值 %d，解析失败 %d",
                (time.monotonic() - started) * 1000,
                snapshot.content_fragments,
                snapshot.thinking_fragments,
                snapshot.ignored_values,
                snapshot.parse_errors,
            )
        return snapshot

    def _dump_stream(self, lines: list[str]) -> None:
        """把原始 SSE 落盘，用于协议漂移排查。

        Args:
            lines: 原始行。
        """

        if self._dump_dir is None:
            return
        try:
            self._dump_dir.mkdir(parents=True, exist_ok=True)
            path = self._dump_dir / f"sse_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            path.write_text("\n".join(lines), encoding="utf-8")
            self._logger.info("已把上游原始 SSE 写入 %s", path)
        except OSError as exc:
            self._logger.warning("写入上游 SSE 快照失败：%s", exc)

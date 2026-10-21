"""凭证回环接收服务。

只绑定 `127.0.0.1`，用最小可用的 HTTP/1.1 实现处理一件事：
把浏览器书签里的 token 收下来。刻意不引入 web 框架，减少依赖与暴露面。
"""

from typing import Any, Callable

import asyncio
import hmac
import json
import logging

from .constants import (
    CREDENTIAL_CLAIM_PATH,
    CREDENTIAL_HEALTH_PATH,
    LOOPBACK_HOST,
)
from .errors import DeepEtherError, ErrorCode

_MAX_BODY_BYTES = 8 * 1024
_MAX_HEADER_LINES = 64
_REQUEST_TIMEOUT_SEC = 10.0
_ALLOWED_REMOTE = ("127.0.0.1", "::1")

CREDENTIAL_INDEX_PATH: str = "/"

_INDEX_HTML: str = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeepEther Proxy 授权</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, "Segoe UI", "Microsoft YaHei", sans-serif; max-width: 720px; margin: 40px auto; padding: 0 16px; line-height: 1.7; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .muted { opacity: .7; font-size: 13px; }
  ol { padding-left: 22px; }
  code, textarea { font-family: ui-monospace, Consolas, monospace; }
  textarea { width: 100%; height: 90px; box-sizing: border-box; padding: 8px; border-radius: 8px; border: 1px solid #8884; }
  button { margin-top: 10px; padding: 8px 18px; border-radius: 8px; border: 1px solid #8884; cursor: pointer; font-size: 14px; }
  #result { margin-top: 14px; font-weight: 600; }
  .ok { color: #178a3a; }
  .err { color: #c0392b; }
  .box { border: 1px solid #8883; border-radius: 10px; padding: 14px 18px; margin-top: 18px; }
</style>
</head>
<body>
<h1>DeepEther Proxy 授权</h1>
<p class="muted">本页面只在 127.0.0.1 上提供，用于把 DeepSeek 网页版的登录 token 交给 MaiBot 插件。</p>

<div class="box">
  <b>方式 A（推荐，最快）：从浏览器控制台复制</b>
  <ol>
    <li>在同一个浏览器里打开并登录 <a href="https://chat.deepseek.com" target="_blank" rel="noreferrer">chat.deepseek.com</a>；</li>
    <li>按 F12 打开开发者工具 → <b>Console</b>，粘贴并回车执行：<br>
      <code>copy(JSON.parse(localStorage.getItem('userToken')).value)</code><br>
      <span class="muted">（Firefox 用 <code>console.log</code> 代替 <code>copy</code>，然后手动选中复制）</span></li>
    <li>回到本页粘贴到下面的输入框，点「提交」。</li>
  </ol>
</div>

<div class="box">
  <b>方式 B：用书签小工具</b>
  <p class="muted">把插件提供的 <code>javascript:</code> 书签在 chat.deepseek.com 页面上点一下，会自动送到本服务。</p>
</div>

<p><textarea id="token" placeholder="在这里粘贴 userToken（形如 6f1c... 的长字符串）"></textarea></p>
<button id="submit">提交</button>
<div id="result"></div>

<script>
const NONCE = "__NONCE__";
const btn = document.getElementById('submit');
const out = document.getElementById('result');
btn.addEventListener('click', async () => {
  const token = document.getElementById('token').value.trim();
  if (!token) { out.className = 'err'; out.textContent = '请先粘贴 token'; return; }
  btn.disabled = true;
  out.className = ''; out.textContent = '提交中…';
  try {
    const resp = await fetch('/credential/claim', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ nonce: NONCE, token: token })
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.message || ('HTTP ' + resp.status));
    out.className = 'ok'; out.textContent = '✅ 凭证已保存，可以关闭本页，回到 MaiBot 发送 /deepether-status 确认';
    document.getElementById('token').value = '';
  } catch (err) {
    out.className = 'err'; out.textContent = '❌ ' + err;
  } finally {
    btn.disabled = false;
  }
});
</script>
</body>
</html>
"""

_HTML_CONTENT_TYPE: str = "text/html; charset=utf-8"

_STATUS_TEXT: dict[int, str] = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Payload Too Large",
    500: "Internal Server Error",
}


class CredentialLoopbackServer:
    """接收书签小工具提交的凭证。

    Attributes:
        _port: 实际监听端口（端口传 0 时由系统分配，便于单测）。
    """

    def __init__(
        self,
        *,
        port: int,
        allowed_origin: str,
        logger: logging.Logger,
        nonce_provider: Callable[[], str],
        on_token: Callable[[str], bool],
    ) -> None:
        """初始化回环服务。

        Args:
            port: 监听端口；0 表示由系统分配。
            allowed_origin: 允许提交凭证的浏览器来源。
            logger: 插件日志器。
            nonce_provider: 返回当前口令的回调。
            on_token: 收到 token 后的处理回调，返回是否接受。
        """

        self._port = int(port)
        self._allowed_origin = allowed_origin
        self._logger = logger
        self._nonce_provider = nonce_provider
        self._on_token = on_token
        self._server: asyncio.AbstractServer | None = None

    # ─── 生命周期 ───────────────────────────────────────────────────────

    @property
    def port(self) -> int:
        """返回实际监听端口。"""

        return self._port

    @property
    def running(self) -> bool:
        """返回服务是否正在监听。"""

        return self._server is not None and self._server.is_serving()

    async def start(self) -> None:
        """启动回环服务。

        Raises:
            DeepEtherError: 端口被占用或无法绑定时抛出（中文说明）。
        """

        if self.running:
            return
        try:
            self._server = await asyncio.start_server(
                self._handle_client,
                host=LOOPBACK_HOST,
                port=self._port,
                limit=_MAX_BODY_BYTES + _MAX_HEADER_LINES * 256,
            )
        except OSError as exc:
            raise DeepEtherError(
                ErrorCode.INTERNAL,
                f"凭证接收端口 {self._port} 启动失败，请改用其他端口",
                detail=str(exc),
            ) from exc

        sockets = self._server.sockets or ()
        if sockets:
            self._port = int(sockets[0].getsockname()[1])
        self._logger.info(
            "凭证接收服务已启动：http://%s:%d%s（仅本机可访问）；授权页：http://%s:%d/",
            LOOPBACK_HOST,
            self._port,
            CREDENTIAL_CLAIM_PATH,
            LOOPBACK_HOST,
            self._port,
        )

    async def stop(self) -> None:
        """停止回环服务。"""

        server = self._server
        self._server = None
        if server is None:
            return
        server.close()
        try:
            await server.wait_closed()
        except Exception as exc:  # noqa: BLE001 - 关闭失败不应影响卸载
            self._logger.debug("关闭凭证接收服务时出现异常：%s", exc)
        self._logger.info("凭证接收服务已停止")

    # ─── 连接处理 ───────────────────────────────────────────────────────

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """处理单个回环连接。"""

        peer = writer.get_extra_info("peername")
        peer_host = str(peer[0]) if isinstance(peer, (tuple, list)) and peer else ""
        if peer_host and peer_host not in _ALLOWED_REMOTE:
            self._logger.warning("拒绝来自非本机地址的凭证提交：%s", peer_host)
            writer.close()
            return

        try:
            await asyncio.wait_for(self._serve(reader, writer), timeout=_REQUEST_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            self._logger.debug("凭证请求超时")
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:  # noqa: BLE001 - 回环端不能把异常抛给事件循环
            self._logger.warning("处理凭证请求异常：%s", exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001 - 对端先关闭时忽略
                pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """解析 HTTP 请求并分发。"""

        request_line = await reader.readline()
        if not request_line:
            return
        try:
            method, raw_target, _version = request_line.decode("latin-1").strip().split(" ", 2)
        except ValueError:
            await self._respond(writer, 400, {"ok": False, "message": "请求行格式不正确"}, origin="")
            return

        headers: dict[str, str] = {}
        for _ in range(_MAX_HEADER_LINES):
            line = await reader.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            text = line.decode("latin-1").rstrip("\r\n")
            if ":" not in text:
                continue
            name, value = text.split(":", 1)
            headers[name.strip().lower()] = value.strip()

        origin = headers.get("origin", "")
        path = raw_target.split("?", 1)[0]
        method_upper = method.upper()

        if method_upper == "OPTIONS":
            await self._respond(writer, 204, None, origin=origin, preflight=True)
            return

        if path in (CREDENTIAL_INDEX_PATH, "/index.html"):
            if method_upper != "GET":
                await self._respond(writer, 405, {"ok": False, "message": "只支持 GET"}, origin=origin)
                return
            html = _INDEX_HTML.replace("__NONCE__", self._nonce_provider())
            await self._respond_html(writer, 200, html, origin=origin)
            return

        if path == CREDENTIAL_HEALTH_PATH:
            if method_upper != "GET":
                await self._respond(writer, 405, {"ok": False, "message": "只支持 GET"}, origin=origin)
                return
            await self._respond(writer, 200, {"ok": True, "service": "deepether-proxy"}, origin=origin)
            return

        if path != CREDENTIAL_CLAIM_PATH:
            await self._respond(writer, 404, {"ok": False, "message": "未知路径"}, origin=origin)
            return

        if method_upper != "POST":
            await self._respond(writer, 405, {"ok": False, "message": "只支持 POST"}, origin=origin)
            return

        allowed_origins = self._allowed_origins()
        if origin and origin not in allowed_origins:
            self._logger.warning("拒绝来源 %s 的凭证提交（只放行 %s）", origin, "、".join(sorted(allowed_origins)))
            await self._respond(writer, 403, {"ok": False, "message": "来源不被允许"}, origin=origin)
            return

        try:
            content_length = int(headers.get("content-length", "0") or 0)
        except ValueError:
            content_length = -1
        if content_length < 0 or content_length > _MAX_BODY_BYTES:
            await self._respond(writer, 413, {"ok": False, "message": "请求体过大"}, origin=origin)
            return

        body = await reader.readexactly(content_length) if content_length else b""
        try:
            payload = json.loads(body.decode("utf-8")) if body else {}
        except (UnicodeDecodeError, ValueError):
            await self._respond(writer, 400, {"ok": False, "message": "请求体不是合法 JSON"}, origin=origin)
            return
        if not isinstance(payload, dict):
            await self._respond(writer, 400, {"ok": False, "message": "请求体必须是 JSON 对象"}, origin=origin)
            return

        submitted_nonce = str(payload.get("nonce") or "")
        expected_nonce = self._nonce_provider()
        if not expected_nonce or not hmac.compare_digest(submitted_nonce, expected_nonce):
            self._logger.warning("凭证提交的 nonce 校验失败，已拒绝（可能是书签过期，请重新复制）")
            await self._respond(writer, 403, {"ok": False, "message": "nonce 校验失败，请重新复制书签"}, origin=origin)
            return

        token = str(payload.get("token") or "").strip()
        if not token:
            await self._respond(writer, 400, {"ok": False, "message": "缺少 token"}, origin=origin)
            return

        if not self._on_token(token):
            await self._respond(writer, 500, {"ok": False, "message": "凭证保存失败，请查看 MaiBot 日志"}, origin=origin)
            return

        await self._respond(writer, 200, {"ok": True, "message": "凭证已保存"}, origin=origin)

    # ─── 响应 ───────────────────────────────────────────────────────────

    def _allowed_origins(self) -> set[str]:
        """返回允许提交凭证的来源集合。

        除了 DeepSeek 网页版本身，还放行本机授权页自己
        （`http://127.0.0.1:<port>`），这样手动粘贴 token 也能走同一个接口。

        Returns:
            set[str]: 允许的来源。
        """

        origins = {f"http://127.0.0.1:{self._port}", f"http://localhost:{self._port}"}
        if self._allowed_origin:
            origins.add(self._allowed_origin)
        return origins

    async def _respond_html(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        html: str,
        *,
        origin: str,
    ) -> None:
        """写出一个 HTML 响应。"""

        body = html.encode("utf-8")
        lines = [
            f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'OK')}",
            f"Content-Type: {_HTML_CONTENT_TYPE}",
            f"Content-Length: {len(body)}",
            "Cache-Control: no-store",
            "Connection: close",
        ]
        if origin and origin in self._allowed_origins():
            lines.append(f"Access-Control-Allow-Origin: {origin}")
            lines.append("Vary: Origin")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        writer.write(head + body)
        await writer.drain()

    async def _respond(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict[str, Any] | None,
        *,
        origin: str,
        preflight: bool = False,
    ) -> None:
        """写出一个最小 HTTP 响应。"""

        lines = [f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'OK')}"]
        body = b""
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if origin and origin == self._allowed_origin:
            lines.append(f"Access-Control-Allow-Origin: {origin}")
            lines.append("Vary: Origin")
            lines.append("Access-Control-Allow-Methods: POST, GET, OPTIONS")
            lines.append("Access-Control-Allow-Headers: content-type")
            lines.append("Access-Control-Max-Age: 600")
            if preflight:
                lines.append("Access-Control-Allow-Private-Network: true")
        if not preflight:
            lines.append("Content-Type: application/json; charset=utf-8")
            lines.append(f"Content-Length: {len(body)}")
        lines.append("Cache-Control: no-store")
        lines.append("Connection: close")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
        writer.write(head + body)
        await writer.drain()

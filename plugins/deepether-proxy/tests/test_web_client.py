"""上游客户端：请求头合法性与会话 id 提取。"""

import asyncio
import logging

import httpx
import pytest

from deepether.errors import DeepEtherError
from deepether.upstream.web_client import WebUpstreamClient


def _client() -> WebUpstreamClient:
    return WebUpstreamClient(
        token_provider=lambda: "unit-test-token",
        on_credential_invalid=lambda reason: None,
        logger=logging.getLogger("test"),
    )


def test_headers_are_valid_http_header_names():
    """回归：曾把 User-Agent/Origin/Referer 的"值"当成"头名"，导致 httpx 报 Illegal header name。"""

    headers = _client()._headers()  # noqa: SLF001
    for name in headers:
        assert name
        assert name.isascii()
        assert " " not in name
        assert all(ch.isalnum() or ch in "-_" for ch in name), name
    assert headers["Authorization"] == "Bearer unit-test-token"
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert headers["Origin"] == "https://chat.deepseek.com"
    assert headers["Referer"].startswith("https://chat.deepseek.com")


def test_extract_session_id_from_real_payload():
    """实测结构：data.biz_data.chat_session.id"""

    payload = {
        "code": 0,
        "msg": "",
        "data": {
            "biz_code": 0,
            "biz_msg": "",
            "biz_data": {
                "chat_session": {"id": "1b36f6a7-e79f-4226-b7e8-1f33b42aea4a", "title": None},
                "ttl_seconds": 259200,
            },
        },
    }
    assert WebUpstreamClient._extract_session_id(payload) == "1b36f6a7-e79f-4226-b7e8-1f33b42aea4a"


def test_extract_session_id_handles_flat_shapes():
    assert WebUpstreamClient._extract_session_id({"data": {"chat_session_id": "a"}}) == "a"
    assert WebUpstreamClient._extract_session_id({"chatSessionId": "b"}) == "b"
    assert WebUpstreamClient._extract_session_id({"data": {"biz_data": {"session_id": "c"}}}) == "c"
    assert WebUpstreamClient._extract_session_id({"data": {}}) == ""

# ─── 响应封装：HTTP 200 + 业务错误码 ─────────────────────────────────────
def _client_with_transport(handler) -> tuple[WebUpstreamClient, list[str]]:
    """构造一个把 httpx 请求交给 MockTransport 的客户端，并收集凭证失效回调。"""

    reasons: list[str] = []
    client = WebUpstreamClient(
        token_provider=lambda: "unit-test-token",
        on_credential_invalid=reasons.append,
        logger=logging.getLogger("test"),
    )
    client._client = httpx.AsyncClient(  # noqa: SLF001 - 注入测试用传输层
        transport=httpx.MockTransport(handler),
        base_url="https://chat.deepseek.com",
    )
    return client, reasons


def test_invalid_token_envelope_is_credential_invalid():
    """实测：无效 token 返回 HTTP 200 + code=40003；必须判成凭证失效而不是协议错误。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40003, "msg": "INVALID_TOKEN", "data": None})

    client, reasons = _client_with_transport(handler)
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(client.create_session())
    asyncio.run(client.close())
    assert excinfo.value.code == "credential_invalid"
    assert reasons and "40003" in reasons[0]
    assert "credential.web_token" in excinfo.value.message


def test_unknown_business_code_is_protocol_error():
    """非鉴权类业务错误不能把凭证标记成失效，否则用户会被误导去重新授权。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 50013, "msg": "SERVICE_BUSY", "data": None})

    client, reasons = _client_with_transport(handler)
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(client.create_session())
    asyncio.run(client.close())
    assert excinfo.value.code == "protocol_error"
    assert not reasons
    assert "50013" in excinfo.value.message


def test_authorization_failed_message_is_credential_invalid():
    """users/current 实测返回 {"code":40003,"msg":"Authorization Failed (invalid token)"}。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40003, "msg": "Authorization Failed (invalid token)", "data": None})

    client, reasons = _client_with_transport(handler)
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(client.probe_user())
    asyncio.run(client.close())
    assert excinfo.value.code == "credential_invalid"
    assert reasons


def test_code_zero_envelope_passes():
    """正常响应 code=0 必须原样放行，不能把正常调用判成失败。"""

    payload = {
        "code": 0,
        "msg": "",
        "data": {"biz_code": 0, "biz_data": {"chat_session": {"id": "sess-ok"}}},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client, reasons = _client_with_transport(handler)
    session_id = asyncio.run(client.create_session())
    asyncio.run(client.close())
    assert session_id == "sess-ok"
    assert not reasons
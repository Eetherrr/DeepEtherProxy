"""凭证回环服务：只在 127.0.0.1、nonce 校验、来源校验。"""

from pathlib import Path

import asyncio
import json
import logging

import httpx

from deepether.loopback import CredentialLoopbackServer

ALLOWED_ORIGIN = "https://chat.deepseek.com"
NONCE = "test-nonce-123"


def _run(coro):
    return asyncio.run(coro)


async def _with_server(handler):
    """起一个随机端口服务，跑完自动关停。"""

    accepted: list[str] = []
    server = CredentialLoopbackServer(
        port=0,
        allowed_origin=ALLOWED_ORIGIN,
        logger=logging.getLogger("test"),
        nonce_provider=lambda: NONCE,
        on_token=lambda token: accepted.append(token) or True,
    )
    await server.start()
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}", timeout=5.0, trust_env=False) as client:
            await handler(server, client, accepted)
    finally:
        await server.stop()


def test_binds_loopback_only():
    async def handler(server, client, accepted):
        hosts = {sock.getsockname()[0] for sock in (server._server.sockets or [])}  # noqa: SLF001
        assert hosts == {"127.0.0.1"}

    _run(_with_server(handler))


def test_health_endpoint():
    async def handler(server, client, accepted):
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["ok"] is True

    _run(_with_server(handler))


def test_wrong_nonce_rejected_and_not_saved():
    async def handler(server, client, accepted):
        response = await client.post(
            "/credential/claim",
            json={"nonce": "bogus", "token": "t"},
            headers={"Origin": ALLOWED_ORIGIN},
        )
        assert response.status_code == 403
        assert accepted == []

    _run(_with_server(handler))


def test_wrong_origin_rejected():
    async def handler(server, client, accepted):
        response = await client.post(
            "/credential/claim",
            json={"nonce": NONCE, "token": "t"},
            headers={"Origin": "https://evil.example.com"},
        )
        assert response.status_code == 403
        assert accepted == []

    _run(_with_server(handler))


def test_correct_claim_accepted():
    async def handler(server, client, accepted):
        response = await client.post(
            "/credential/claim",
            json={"nonce": NONCE, "token": "the-token"},
            headers={"Origin": ALLOWED_ORIGIN},
        )
        assert response.status_code == 200
        assert accepted == ["the-token"]
        assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN

    _run(_with_server(handler))


def test_missing_token_rejected():
    async def handler(server, client, accepted):
        response = await client.post(
            "/credential/claim",
            json={"nonce": NONCE},
            headers={"Origin": ALLOWED_ORIGIN},
        )
        assert response.status_code == 400
        assert accepted == []

    _run(_with_server(handler))


def test_index_page_serves_html_with_nonce():
    """本机授权页必须能打开，且内嵌当前 nonce（用户只需粘贴 token）。"""

    async def handler(server, client, accepted):
        response = await client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "DeepEther Proxy 授权" in response.text
        assert NONCE in response.text
        assert "userToken" in response.text

    _run(_with_server(handler))


def test_manual_paste_from_index_page_is_accepted():
    """从本机授权页提交（Origin 是回环地址本身）也必须放行。"""

    async def handler(server, client, accepted):
        response = await client.post(
            "/credential/claim",
            json={"nonce": NONCE, "token": "manual-token"},
            headers={"Origin": f"http://127.0.0.1:{server.port}"},
        )
        assert response.status_code == 200
        assert accepted == ["manual-token"]

    _run(_with_server(handler))


def test_unknown_path_is_404():
    async def handler(server, client, accepted):
        response = await client.get("/nope")
        assert response.status_code == 404

    _run(_with_server(handler))


def test_preflight_returns_cors_headers():
    async def handler(server, client, accepted):
        response = await client.options(
            "/credential/claim",
            headers={"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Private-Network": "true"},
        )
        assert response.status_code == 204
        assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
        assert response.headers["access-control-allow-private-network"] == "true"

    _run(_with_server(handler))


def test_failing_on_token_returns_500():
    async def handler():
        server = CredentialLoopbackServer(
            port=0,
            allowed_origin=ALLOWED_ORIGIN,
            logger=logging.getLogger("test"),
            nonce_provider=lambda: NONCE,
            on_token=lambda token: False,
        )
        await server.start()
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{server.port}", timeout=5.0, trust_env=False) as client:
                response = await client.post(
                    "/credential/claim",
                    json={"nonce": NONCE, "token": "t"},
                    headers={"Origin": ALLOWED_ORIGIN},
                )
                assert response.status_code == 500
        finally:
            await server.stop()

    _run(handler())

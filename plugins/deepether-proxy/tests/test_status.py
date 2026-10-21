"""状态文本渲染。"""

from pathlib import Path

import logging

from deepether.config import DeepEtherProxyConfig
from deepether.credential import SOURCE_BOOKMARKLET, CredentialStore
from deepether.state import RuntimeState
from deepether.status import render_status


def _render(tmp_path: Path, *, token: str | None = "tok-123456", loopback: bool = True) -> str:
    config = DeepEtherProxyConfig()
    store = CredentialStore(tmp_path, logging.getLogger("test"))
    if token:
        store.save(token, SOURCE_BOOKMARKLET)
    state = RuntimeState()
    return render_status(
        version="0.1.0",
        config=config,
        store=store,
        state=state,
        loopback_running=loopback,
        loopback_port=18765,
        nonce="the-nonce",
    )


def test_status_contains_credential_and_bookmarklet(tmp_path: Path):
    text = _render(tmp_path)
    assert "DeepEther Proxy v0.1.0" in text
    assert "凭证" in text
    assert "书签小工具" in text
    assert "javascript:" in text
    assert "the-nonce" in text
    assert "tok-123456" not in text


def test_status_reports_missing_credential(tmp_path: Path):
    text = _render(tmp_path, token=None)
    assert "未配置" in text


def test_status_reports_invalid_reason(tmp_path: Path):
    config = DeepEtherProxyConfig()
    store = CredentialStore(tmp_path, logging.getLogger("test"))
    store.save("tok-1", SOURCE_BOOKMARKLET)
    store.mark_invalid("上游返回 401")
    text = render_status(
        version="0.1.0",
        config=config,
        store=store,
        state=RuntimeState(),
        loopback_running=True,
        loopback_port=18765,
        nonce="n",
    )
    assert "已失效" in text
    assert "401" in text


def test_status_shows_manual_token_steps_when_loopback_disabled(tmp_path: Path):
    """发布版默认关闭回环，状态里必须给出"从控制台取 token 填 WebUI 配置"的步骤。"""

    config = DeepEtherProxyConfig()
    store = CredentialStore(tmp_path, logging.getLogger("test"))
    text = render_status(
        version="0.2.0",
        config=config,
        store=store,
        state=RuntimeState(),
        loopback_running=False,
        loopback_port=18765,
        nonce="n",
        loopback_enabled=False,
    )
    assert "已关闭（发布版默认）" in text
    assert "localStorage.getItem('userToken')" in text
    assert "WebUI" in text
    assert "javascript:" not in text


def test_status_shows_counts(tmp_path: Path):
    config = DeepEtherProxyConfig()
    store = CredentialStore(tmp_path, logging.getLogger("test"))
    store.save("tok-1", SOURCE_BOOKMARKLET)
    state = RuntimeState()
    state.record_success(1200)
    state.record_fallback("上游超时")
    text = render_status(
        version="0.1.0",
        config=config,
        store=store,
        state=state,
        loopback_running=True,
        loopback_port=18765,
        nonce="n",
    )
    assert "降级：1 次" in text
    assert "1200 ms" in text

def test_status_shows_24h_window_and_gate_rejections(tmp_path: Path):
    """报告 §6.6.2 / §7.1：状态里要能一眼看到 24 小时降级与闸门拒绝次数。"""

    config = DeepEtherProxyConfig()
    store = CredentialStore(tmp_path, logging.getLogger("test"))
    store.save("tok-1", SOURCE_BOOKMARKLET)
    state = RuntimeState()
    state.record_fallback("上游超时")
    state.record_fallback("并发已满")
    text = render_status(
        version="0.1.0",
        config=config,
        store=store,
        state=state,
        loopback_running=True,
        loopback_port=18765,
        nonce="n",
        gate_rejected=7,
    )
    assert "降级：2 次（最近 24 小时 2 次）" in text
    assert "闸门拒绝：7 次" in text
    assert state.fallbacks_last_24h == 2

"""状态文本渲染：只保留简洁的几行。"""

from pathlib import Path

import logging

from deepether.config import DeepEtherProxyConfig
from deepether.credential import SOURCE_BOOKMARKLET, CredentialStore
from deepether.state import RuntimeState
from deepether.status import render_status


def _store(tmp_path: Path, token: str | None = "tok-123456") -> CredentialStore:
    store = CredentialStore(tmp_path, logging.getLogger("test"))
    if token:
        store.save(token, SOURCE_BOOKMARKLET)
    return store


def _render(tmp_path: Path, **overrides) -> str:
    config = overrides.pop("config", None) or DeepEtherProxyConfig()
    store = overrides.pop("store", None) or _store(tmp_path, overrides.pop("token", "tok-123456"))
    state = overrides.pop("state", None) or RuntimeState()
    return render_status(
        version="0.5.0",
        config=config,
        store=store,
        state=state,
        **overrides,
    )


def test_status_is_short_and_readable(tmp_path: Path):
    text = _render(tmp_path, loopback_enabled=True, loopback_running=True, loopback_port=18765, nonce="n")
    lines = text.splitlines()

    assert lines[0].startswith("DeepEther v0.5.0")
    assert 3 <= len(lines) <= 6
    assert "凭证：已就绪" in text
    # 详细参数不再铺在聊天里
    assert "书签" not in text
    assert "自适应预算" not in text
    assert "夜间节律" not in text
    assert "javascript:" not in text
    # token 只出现脱敏形式
    assert "tok-123456" not in text


def test_status_reports_missing_credential(tmp_path: Path):
    text = _render(tmp_path, token=None)
    assert "未配置" in text


def test_status_reports_invalid_reason(tmp_path: Path):
    store = _store(tmp_path, "tok-1")
    store.mark_invalid("上游返回 401")
    text = _render(tmp_path, store=store)
    assert "已失效" in text
    assert "401" in text


def test_status_shows_usage_and_circuit(tmp_path: Path):
    pacing = {
        "in_cooldown": True,
        "cooldown_remaining_sec": 120.0,
        "used_last_hour": 18,
        "used_last_day": 120,
        "limits": {"per_day": 400, "per_hour": 45},
    }
    text = _render(tmp_path, pacing=pacing, circuit_state="open")
    assert "今日 120/400" in text
    assert "1 小时 18/45" in text
    assert "冷却 120s" in text
    assert "熔断 open" in text


def test_status_shows_commandcode_summary(tmp_path: Path):
    cc = {"five_hour_used": 1.2, "five_hour_limit": 3.0, "circuit_state": "closed"}
    text = _render(tmp_path, cc=cc)
    assert "CommandCode：5 小时 $1.20/$3.00" in text
    assert "熔断 closed" in text


def test_status_shows_last_error(tmp_path: Path):
    state = RuntimeState()
    state.record_failure("upstream_timeout", "上游超时")
    text = _render(tmp_path, state=state)
    assert "最近错误" in text
    assert "上游超时" in text


def test_status_hides_auth_page_when_loopback_disabled(tmp_path: Path):
    text = _render(tmp_path, loopback_enabled=False, loopback_running=False)
    assert "授权页" not in text

    text = _render(tmp_path, loopback_enabled=True, loopback_running=True, loopback_port=18765, nonce="abc")
    assert "授权页：http://127.0.0.1:18765/" in text

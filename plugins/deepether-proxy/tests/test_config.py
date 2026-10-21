"""配置模型：默认值、分区、版本节。"""

from deepether.config import DeepEtherProxyConfig


def test_defaults_are_complete():
    config = DeepEtherProxyConfig()
    assert config.plugin.enabled is True
    assert config.plugin.config_version
    assert config.credential.web_token == ""
    assert config.credential.credential_port == 18765
    assert config.behavior.mode == "web_first"
    assert config.behavior.response_protocol == "legacy"
    assert 1 <= config.concurrency.max_concurrency <= 3
    assert config.fallback.fallback_enabled is True
    assert config.debug.dump_request is False


def test_mode_literal_rejects_unknown_value():
    import pytest

    with pytest.raises(Exception):
        DeepEtherProxyConfig(behavior={"mode": "nonsense"})


def test_port_range_is_enforced():
    import pytest

    with pytest.raises(Exception):
        DeepEtherProxyConfig(credential={"credential_port": 80})


def test_fallback_model_whitelist_defaults_to_empty():
    """Host 不传任务名，白名单按模型名；默认留空表示全部允许。"""

    config = DeepEtherProxyConfig()
    assert config.fallback.fallback_for_models == []
    assert config.behavior.prompt_style == "plain"

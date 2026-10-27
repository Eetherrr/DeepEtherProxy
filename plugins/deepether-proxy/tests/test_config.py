"""配置模型：默认值、分区、版本节与三态通道语义。"""

import pytest

from deepether.config import DeepEtherProxyConfig


def test_defaults_are_complete():
    config = DeepEtherProxyConfig()
    assert config.plugin.enabled is True
    assert config.plugin.config_version == "0.5.0"
    # 0.5.0：自动注册只注册模型（不接线）+ replyer 让位模型 + 多账号字段
    assert config.credential.web_tokens == []
    assert not hasattr(config.provision, "wire_tasks")
    assert not hasattr(config.provision, "selection_strategy")
    # 0.3.2：容量旋钮（日上限提高 + 自适应预算 + 夜间节律 + 按模型配额 + 保留额度）
    assert config.antiban.max_per_day == 400
    assert config.antiban.max_per_hour == 45
    assert config.antiban.adaptive_budget is True
    assert config.antiban.quiet_hours == "01:00-07:00"
    assert config.antiban.reserve_daily_budget == 60
    assert config.antiban.reserve_models == "deepether-replyer"
    assert config.antiban.session_reuse_calls == 20
    assert config.credential.credential_port == 18765
    assert config.behavior.mode == "web_first"
    assert config.behavior.response_protocol == "legacy"
    assert config.behavior.prompt_style == "plain"
    assert 1 <= config.concurrency.max_concurrency <= 3
    assert config.debug.dump_request is False
    # 0.5.0：CommandCode 通道对用户只暴露一个 Key
    assert config.commandcode.api_key == ""
    assert not hasattr(config.commandcode, "register_models")
    assert not hasattr(config.commandcode, "five_hour_budget_usd")
    assert not hasattr(config.commandcode, "max_concurrency")


def test_fallback_section_is_gone():
    """0.3.0 起插件不再接管 fallback：FallbackConfig 与 fallback 字段都已删除。"""

    import deepether.config as config_module

    config = DeepEtherProxyConfig()
    assert hasattr(config, "fallback") is False
    assert not hasattr(config_module, "FallbackConfig")


@pytest.mark.parametrize("mode", ["web_first", "web_only", "official_only"])
def test_mode_accepts_known_values(mode):
    """web_only 保留只是为了旧配置能加载（行为等同 web_first）。"""

    assert DeepEtherProxyConfig(behavior={"mode": mode}).behavior.mode == mode


def test_mode_literal_rejects_unknown_value():
    with pytest.raises(Exception):
        DeepEtherProxyConfig(behavior={"mode": "nonsense"})


def test_port_range_is_enforced():
    with pytest.raises(Exception):
        DeepEtherProxyConfig(credential={"credential_port": 80})

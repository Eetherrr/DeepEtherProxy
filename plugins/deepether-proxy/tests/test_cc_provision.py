"""CommandCode 自动注册：只写模型清单，不接线。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

from deepether.provision import (
    CC_CLIENT_TYPE,
    CcProvisionSettings,
    ProvisionSettings,
    cc_settings_from_config,
    provision_model_config,
)

BASE_TOML = """[[models]]
model_type = "llm"
model_identifier = "deepseek-v4-flash"
name = "deepseek-v4-flash"
api_provider = "DeepSeek"

[model_task_config.replyer]
model_list = ["deepseek-v4-flash"]
selection_strategy = "random"

[model_task_config.utils]
model_list = []
selection_strategy = "random"

[[api_providers]]
name = "DeepSeek"
base_url = "https://api.deepseek.com"
client_type = "openai"
auth_type = "bearer"
"""


def _root(tmp_path: Path) -> Path:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugins" / "deepether-proxy").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "model_config.toml").write_text(BASE_TOML, encoding="utf-8")
    return tmp_path


def _run(root: Path, cc: CcProvisionSettings | None = None):
    return provision_model_config(
        settings=ProvisionSettings(),
        plugin_dir=root / "plugins" / "deepether-proxy",
        cc_settings=cc if cc is not None else CcProvisionSettings(),
    )


def _parsed(root: Path) -> dict:
    return tomllib.loads((root / "config" / "model_config.toml").read_text(encoding="utf-8"))


def _cc_models(parsed: dict) -> dict:
    return {
        item["name"]: item
        for item in parsed["models"]
        if item.get("api_provider") == "commandcode"
    }


def test_registers_provider_and_all_go_models_without_wiring(tmp_path: Path) -> None:
    root = _root(tmp_path)
    result = _run(root)

    assert result.ok and result.changed
    parsed = _parsed(root)
    providers = {item["name"]: item for item in parsed["api_providers"]}
    assert set(providers) == {"DeepSeek", "deepether", "commandcode"}
    provider = providers["commandcode"]
    assert provider["client_type"] == CC_CLIENT_TYPE
    assert provider["base_url"] == "plugin://com.ether.commandcode"
    assert provider["auth_type"] == "none"
    assert provider["timeout"] == 300
    assert provider["max_retry"] == 1

    models = _cc_models(parsed)
    assert len(models) == 56
    assert models["cc-deepseek-v4-flash"]["model_identifier"] == "deepseek/deepseek-v4-flash"
    assert models["cc-deepseek-v4-flash"]["force_stream_mode"] is False
    assert models["cc-deepseek-v4-flash"]["send_temperature"] is False
    assert models["cc-deepseek-v4-flash"]["price_in"] == 0.0
    assert models["cc-deepseek-v4-flash"]["price_out"] == 0.0
    assert models["cc-deepseek-v4-flash-vision-exp"]["visual"] is True
    assert "visual" not in models["cc-deepseek-v4-flash"]

    # 只注册、不接线：任何任务的 model_list 里都不能出现 cc-*。
    for task in parsed["model_task_config"].values():
        assert not any(str(name).startswith("cc-") for name in task["model_list"])


def test_second_run_is_idempotent(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _run(root)
    before = (root / "config" / "model_config.toml").read_text(encoding="utf-8")

    result = _run(root)

    after = (root / "config" / "model_config.toml").read_text(encoding="utf-8")
    assert result.ok
    assert result.changed is False
    assert before == after
    assert any("CommandCode 模型已是最新" in item for item in result.skipped)


def test_curated_then_none_removes_models_and_task_refs(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _run(root, CcProvisionSettings(register_models="curated"))
    parsed = _parsed(root)
    assert len(_cc_models(parsed)) == 10

    # 模拟用户已经把某个 cc 模型接进任务
    text = (root / "config" / "model_config.toml").read_text(encoding="utf-8")
    text = text.replace(
        'model_list = ["deepseek-v4-flash"]',
        'model_list = ["cc-deepseek-v4-flash", "deepseek-v4-flash"]',
    )
    (root / "config" / "model_config.toml").write_text(text, encoding="utf-8")

    result = _run(root, CcProvisionSettings(register_models="none"))
    parsed = _parsed(root)
    assert result.ok
    assert _cc_models(parsed) == {}
    # 0.5.0 起两个通道都只注册模型、不接线：这里只剩官方模型（cc 引用被清理）
    assert parsed["model_task_config"]["replyer"]["model_list"] == ["deepseek-v4-flash"]
    assert any("移除" in item for item in result.applied)


def test_name_conflict_is_skipped_not_overwritten(tmp_path: Path) -> None:
    root = _root(tmp_path)
    text = (root / "config" / "model_config.toml").read_text(encoding="utf-8")
    text += """
[[models]]
model_type = "llm"
model_identifier = "someone-else"
name = "cc-kimi-k3"
api_provider = "OtherProvider"
"""
    (root / "config" / "model_config.toml").write_text(text, encoding="utf-8")

    result = _run(root)
    parsed = _parsed(root)
    models = {item["name"]: item for item in parsed["models"]}
    assert models["cc-kimi-k3"]["api_provider"] == "OtherProvider"
    assert any("已被其他服务商占用" in item for item in result.warnings)


def test_include_and_exclude(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _run(
        root,
        CcProvisionSettings(
            register_models="curated",
            model_include="xiaomi/mimo-v2.6-pro",
            model_exclude="cc-kimi-k3",
        ),
    )
    models = _cc_models(_parsed(root))
    assert "cc-mimo-v2.6-pro" in models
    assert "cc-kimi-k3" not in models
    assert len(models) == 10  # 10 curated - 1 excluded + 1 included


def test_custom_prefix_renames_models(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _run(root, CcProvisionSettings(register_models="curated", model_prefix="cmdgo-"))
    models = _cc_models(_parsed(root))
    assert "cmdgo-deepseek-v4-flash" in models
    assert all(name.startswith("cmdgo-") for name in models)


def test_disabled_channel_is_skipped(tmp_path: Path) -> None:
    root = _root(tmp_path)
    result = _run(root, CcProvisionSettings(enabled=False))
    parsed = _parsed(root)
    assert "commandcode" not in {item["name"] for item in parsed["api_providers"]}
    assert _cc_models(parsed) == {}
    assert result.ok


def test_settings_from_config_needs_only_api_key():
    """0.5.0：CC 只暴露 api_key；其余（注册范围/前缀/路径开关）全部走默认值。"""

    config = SimpleNamespace(
        commandcode=SimpleNamespace(api_key="user_key"),
        provision=SimpleNamespace(model_config_path="D:/x/model_config.toml", keep_backup=False),
    )
    settings = cc_settings_from_config(config)
    assert settings.enabled is True
    assert settings.register_models == "all"
    assert settings.model_prefix == "cc-"
    assert settings.model_config_path == "D:/x/model_config.toml"
    assert settings.keep_backup is False

    # 没有 Key 就不注册模型
    empty = cc_settings_from_config(SimpleNamespace(commandcode=SimpleNamespace(api_key="")))
    assert empty.enabled is False

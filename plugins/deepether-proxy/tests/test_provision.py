"""自动注册 model_config.toml 的单测（0.5.0：只注册模型、不接线）。"""

from __future__ import annotations

import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepether.provision import (
    DEFAULT_PLAN,
    ProvisionSettings,
    candidate_paths,
    detect_model_config,
    provision_model_config,
    read_model_providers,
    read_task_models,
    settings_from_config,
)

BASE_TOML = """[inner]
version = 1

[[models]]
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


def _write_root(tmp_path: Path, text: str = BASE_TOML) -> Path:
    """在 tmp_path 下造一个实例根：config/model_config.toml + plugins/deepether-proxy。"""

    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugins" / "deepether-proxy").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "model_config.toml").write_text(text, encoding="utf-8")
    return tmp_path


def _provision(tmp_path: Path, settings: ProvisionSettings | None = None):
    """在实例根上跑一次自动注册。"""

    return provision_model_config(
        settings=settings or ProvisionSettings(),
        plugin_dir=tmp_path / "plugins" / "deepether-proxy",
    )


def _parsed(tmp_path: Path) -> dict:
    """读取写入后的配置。"""

    return tomllib.loads((tmp_path / "config" / "model_config.toml").read_text(encoding="utf-8"))


def test_registers_provider_and_models_without_wiring(tmp_path: Path) -> None:
    """0.5.0 核心行为：只注册模型，绝不改写任务的 model_list / selection_strategy。"""

    root = _write_root(tmp_path)
    before = _parsed(root)
    result = _provision(root)

    assert result.ok and result.changed
    assert result.path == root / "config" / "model_config.toml"

    parsed = _parsed(root)
    providers = [item["name"] for item in parsed["api_providers"]]
    assert providers == ["DeepSeek", "deepether"]
    deepether = parsed["api_providers"][1]
    assert deepether["client_type"] == "com.ether.deepether"
    assert deepether["auth_type"] == "none"
    assert deepether["timeout"] == 300
    assert deepether["max_retry"] == 1

    models = {item["name"]: item for item in parsed["models"]}
    assert set(models) == {"deepseek-v4-flash"} | {plan.name for plan in DEFAULT_PLAN}
    assert models["deepether-replyer"]["extra_params"] == {
        "deepether_style": "replyer",
        "deepether_thinking": False,
    }
    assert models["deepether-utils"]["extra_params"] == {
        "deepether_style": "plain",
        "deepether_thinking": False,
    }

    # 任务配置一个字节都不该被改（含 selection_strategy）
    assert parsed["model_task_config"] == before["model_task_config"]




def test_second_run_is_idempotent(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    _provision(root)
    snapshot = (root / "config" / "model_config.toml").read_text(encoding="utf-8")

    result = _provision(root)
    assert result.ok and result.changed is False
    assert (root / "config" / "model_config.toml").read_text(encoding="utf-8") == snapshot


def test_backup_created_on_first_write_only(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    _provision(root)
    backups = list((root / "config").glob("model_config.toml.deepether.bak*"))
    assert len(backups) == 1

    _provision(root)
    backups = list((root / "config").glob("model_config.toml.deepether.bak*"))
    assert len(backups) == 1


def test_broken_toml_is_left_untouched(tmp_path: Path) -> None:
    root = _write_root(tmp_path, "this is not toml [[[")
    original = (root / "config" / "model_config.toml").read_text(encoding="utf-8")

    result = _provision(root)
    assert result.ok is False
    assert "解析" in result.error
    assert (root / "config" / "model_config.toml").read_text(encoding="utf-8") == original


def test_missing_config_reports_hint(tmp_path: Path) -> None:
    (tmp_path / "plugins" / "deepether-proxy").mkdir(parents=True)
    result = provision_model_config(
        settings=ProvisionSettings(),
        plugin_dir=tmp_path / "plugins" / "deepether-proxy",
    )
    assert result.ok is False
    assert "没找到" in result.error
    assert any("model_config_path" in item for item in result.warnings)


def test_model_name_taken_by_other_provider_is_not_overwritten(tmp_path: Path) -> None:
    toml = BASE_TOML.replace(
        'name = "deepseek-v4-flash"\napi_provider = "DeepSeek"',
        'name = "deepseek-v4-flash"\napi_provider = "DeepSeek"\n\n[[models]]\nmodel_type = "llm"\nmodel_identifier = "x"\nname = "deepether-replyer"\napi_provider = "Other"',
    )
    root = _write_root(tmp_path, toml)
    result = _provision(root)

    models = {item["name"]: item for item in _parsed(root)["models"]}
    assert models["deepether-replyer"]["api_provider"] == "Other"
    assert any("已被其他服务商占用" in item for item in result.warnings)
    # 没被占用的那个照常注册
    assert models["deepether-utils"]["api_provider"] == "deepether"


def test_legacy_planner_is_cleaned(tmp_path: Path) -> None:
    toml = BASE_TOML.replace(
        'name = "deepseek-v4-flash"\napi_provider = "DeepSeek"',
        'name = "deepseek-v4-flash"\napi_provider = "DeepSeek"\n\n[[models]]\nmodel_type = "llm"\nmodel_identifier = "deepseek-web"\nname = "deepether-planner"\napi_provider = "deepether"',
    ).replace(
        'model_list = ["deepseek-v4-flash"]',
        'model_list = ["deepether-planner", "deepseek-v4-flash"]',
    )
    root = _write_root(tmp_path, toml)
    _provision(root)

    parsed = _parsed(root)
    names = {item["name"] for item in parsed["models"]}
    assert "deepether-planner" not in names
    assert "deepether-planner" not in parsed["model_task_config"]["replyer"]["model_list"]


def test_read_helpers(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    _provision(root)
    path = root / "config" / "model_config.toml"

    providers = read_model_providers(path)
    assert providers["deepether-replyer"] == "deepether"
    assert providers["deepseek-v4-flash"] == "DeepSeek"

    # 没有接线，所以 replyer 的 model_list 仍是原来那一条
    assert read_task_models(path, "replyer") == [("deepseek-v4-flash", "DeepSeek")]


def test_settings_from_config_maps_basic_fields() -> None:
    config = SimpleNamespace(
        provision=SimpleNamespace(
            provider_name="deepether",
            model_config_path="D:/x/model_config.toml",
            keep_backup=False,
        )
    )
    settings = settings_from_config(config)
    assert settings.provider_name == "deepether"
    assert settings.model_config_path == "D:/x/model_config.toml"
    assert settings.keep_backup is False

    # 缺配置节时用默认值
    defaults = settings_from_config(SimpleNamespace())
    assert defaults.provider_name == "deepether"
    assert defaults.keep_backup is True


def test_detect_prefers_explicit_path(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    explicit = root / "config" / "model_config.toml"
    path, _ = detect_model_config(
        settings=ProvisionSettings(model_config_path=str(explicit)),
        plugin_dir=root / "plugins" / "deepether-proxy",
    )
    assert path == explicit


def test_candidate_paths_include_instance_root(tmp_path: Path) -> None:
    root = _write_root(tmp_path)
    candidates = candidate_paths(plugin_dir=root / "plugins" / "deepether-proxy")
    assert root / "config" / "model_config.toml" in candidates

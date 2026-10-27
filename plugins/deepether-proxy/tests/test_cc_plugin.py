"""插件级：CommandCode 命令文本与双 Provider 注册。"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("maibot_sdk")

from deepether.config import DeepEtherProxyConfig  # noqa: E402

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _load_plugin_module():
    module_name = "deepether_plugin_cc_under_test"
    spec = importlib.util.spec_from_file_location(
        module_name,
        str(PLUGIN_ROOT / "plugin.py"),
        submodule_search_locations=[str(PLUGIN_ROOT)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _plugin(tmp_path: Path, **commandcode):
    module = _load_plugin_module()
    plugin = module.create_plugin()
    plugin._plugin_config_instance = DeepEtherProxyConfig(commandcode=commandcode)
    plugin._ctx = SimpleNamespace(
        logger=logging.getLogger("test.deepether.plugin"),
        paths=SimpleNamespace(runtime_dir=str(tmp_path), data_dir=str(tmp_path)),
    )
    return plugin


def test_plugin_registers_both_providers():
    module = _load_plugin_module()
    plugin = module.create_plugin()
    client_types = {item["client_type"] for item in plugin.get_llm_providers()}
    assert client_types == {"com.ether.deepether", "com.ether.commandcode"}


def test_status_text_contains_cc_summary(tmp_path):
    plugin = _plugin(tmp_path, api_key="user_secret_key_abc")
    text = plugin.build_commandcode_status_text()
    assert "CommandCode 通道状态" in text
    assert "user_secret_key_abc" not in text
    assert "56" in text


def test_models_text_lists_catalog(tmp_path):
    plugin = _plugin(tmp_path)
    text = plugin.build_commandcode_models_text()
    assert "CommandCode Go 模型：56 条" in text
    assert "cc-kimi-k3" in text

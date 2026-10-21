"""manifest 与 Provider 双声明一致性、插件加载契约。"""

from pathlib import Path

import importlib.util
import json
import sys

PLUGIN_ROOT = Path(__file__).resolve().parent.parent


def _load_plugin_module():
    """按 Runner 的方式（包 + submodule_search_locations）加载 plugin.py。"""

    module_name = "deepether_plugin_under_test"
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


def test_manifest_is_strict_valid():
    """manifest 必须能通过宿主自带的严格校验器。"""

    from src.plugin_runtime.runner.manifest_validator import ManifestValidator

    raw = json.loads((PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    validator = ManifestValidator()
    assert validator.validate(raw), validator.errors


def test_decorator_matches_manifest():
    """@LLMProvider 与 manifest 的 client_type 必须完全一致（否则插件不加载）。"""

    raw = json.loads((PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    module = _load_plugin_module()
    plugin = module.create_plugin()

    declared = {item["client_type"] for item in raw["llm_providers"]}
    collected = {item["client_type"] for item in plugin.get_llm_providers()}
    assert declared == collected == {"com.ether.deepether"}


def test_plugin_implements_lifecycle():
    """三个生命周期方法必须由插件自己实现，否则 Runner 会拒绝加载。"""

    from maibot_sdk import MaiBotPlugin

    module = _load_plugin_module()
    plugin_class = module.DeepEtherProxyPlugin
    assert issubclass(plugin_class, MaiBotPlugin)
    assert plugin_class.on_load is not MaiBotPlugin.on_load
    assert plugin_class.on_unload is not MaiBotPlugin.on_unload
    assert plugin_class.on_config_update is not MaiBotPlugin.on_config_update


def test_config_model_generates_defaults():
    """配置模型必须能生成完整默认配置（否则 Host 无法写出 config.toml）。"""

    module = _load_plugin_module()
    plugin_class = module.DeepEtherProxyPlugin
    defaults = plugin_class.build_default_config()
    assert defaults["plugin"]["config_version"]
    assert defaults["plugin"]["enabled"] is True
    assert defaults["behavior"]["mode"] == "web_first"
    assert defaults["concurrency"]["max_concurrency"] <= 3
    assert defaults["fallback"]["fallback_for_models"] == []
    assert defaults["behavior"]["prompt_style"] in ("plain", "replyer", "raw")

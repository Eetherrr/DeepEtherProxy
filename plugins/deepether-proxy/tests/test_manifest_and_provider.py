"""manifest 与 Provider 双声明一致性、插件加载契约。"""

from pathlib import Path

import importlib.util
import json
import sys

import pytest

# 这两个用例依赖 MaiBot 宿主/SDK：只装了插件仓库、没装宿主时自动跳过，而不是报错。
pytest.importorskip("maibot_sdk")

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

    ManifestValidator = pytest.importorskip(
        "src.plugin_runtime.runner.manifest_validator"
    ).ManifestValidator

    raw = json.loads((PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    validator = ManifestValidator()
    assert validator.validate(raw), validator.errors


def test_manifest_only_declares_text_capability_and_current_version():
    """插件不回调 Host 官方模型，所以不能声明 llm.generate 能力。"""

    raw = json.loads((PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    module = _load_plugin_module()

    capabilities = raw["capabilities"]
    assert capabilities == ["send.text"]
    assert "llm.generate" not in capabilities
    assert raw["version"] == module.PROVIDER_VERSION == "0.5.0"
    assert {item["version"] for item in raw["llm_providers"]} == {module.PROVIDER_VERSION}


def test_decorator_matches_manifest():
    """@LLMProvider 与 manifest 的 client_type 必须完全一致（否则插件不加载）。"""

    raw = json.loads((PLUGIN_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    module = _load_plugin_module()
    plugin = module.create_plugin()

    declared = {item["client_type"] for item in raw["llm_providers"]}
    collected = {item["client_type"] for item in plugin.get_llm_providers()}
    assert declared == collected == {"com.ether.deepether", "com.ether.commandcode"}


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
    assert defaults["plugin"]["config_version"] == "0.5.0"
    assert defaults["antiban"]["max_per_day"] == 400
    assert defaults["plugin"]["enabled"] is True
    assert defaults["behavior"]["mode"] == "web_first"
    assert defaults["concurrency"]["max_concurrency"] <= 3
    assert "fallback" not in defaults  # FallbackConfig 已随 0.3.0 删除
    assert defaults["behavior"]["prompt_style"] in ("plain", "replyer", "raw")
    # 0.4.0：CommandCode 通道默认只注册模型、不接线
    assert defaults["commandcode"] == {"api_key": ""}
    assert defaults["commandcode"]["api_key"] == ""

def test_replyer_hook_rewrites_model_when_capacity_exhausted():
    """额度不够时，hook 把本轮 model_name 改成官方模型（零失败切换的关键）。"""

    import asyncio

    module = _load_plugin_module()
    plugin = module.DeepEtherProxyPlugin()

    class StubProvider:
        def __init__(self) -> None:
            self.noted: list[str] = []

        def first_replyer_model(self):
            return ("deepether-replyer", "deepether")

        def replyer_yield_target(self) -> str:
            return "deepseek-v4-flash"

        def note_yield(self, target: str) -> None:
            self.noted.append(target)

    plugin._provider = StubProvider()  # noqa: SLF001
    result = asyncio.run(
        plugin.handle_replyer_before_request(model_name="deepether-replyer", task_name="replyer")
    )
    assert result["action"] == "continue"
    assert result["modified_kwargs"]["model_name"] == "deepseek-v4-flash"
    assert result["modified_kwargs"]["task_name"] == "replyer"
    assert plugin._provider.noted == ["deepseek-v4-flash"]  # noqa: SLF001


def test_replyer_hook_keeps_params_when_capacity_ok():
    import asyncio

    module = _load_plugin_module()
    plugin = module.DeepEtherProxyPlugin()

    class StubProvider:
        def first_replyer_model(self):
            return ("deepether-replyer", "deepether")

        def replyer_yield_target(self) -> None:
            return None

        def note_yield(self, target: str) -> None:  # pragma: no cover - 不应被调用
            raise AssertionError("有容量时不应记录让位")

    plugin._provider = StubProvider()  # noqa: SLF001
    result = asyncio.run(plugin.handle_replyer_before_request(model_name="deepether-replyer"))
    assert result["modified_kwargs"]["model_name"] == "deepether-replyer"


def test_replyer_hook_never_raises():
    """hook 内部异常必须被吞掉并原样放行，不能打断回复。"""

    import asyncio

    module = _load_plugin_module()
    plugin = module.DeepEtherProxyPlugin()

    class BrokenProvider:
        def replyer_yield_target(self) -> str:
            raise RuntimeError("boom")

    plugin._provider = BrokenProvider()  # noqa: SLF001
    result = asyncio.run(plugin.handle_replyer_before_request(model_name="deepether-replyer"))
    assert result == {"action": "continue", "modified_kwargs": {"model_name": "deepether-replyer"}}

def test_replyer_hook_covers_commandcode_budget():
    """replyer 用 cc-* 模型时，预算耗尽同样应该让位到官方模型。"""

    import asyncio

    module = _load_plugin_module()
    plugin = module.DeepEtherProxyPlugin()

    class StubProvider:
        def __init__(self) -> None:
            self.noted: list[str] = []

        def first_replyer_model(self):
            return ("cc-kimi-k3", "commandcode")

        def next_official_model(self) -> str:
            return "deepseek-v4-flash"

        def note_yield(self, target: str) -> None:
            self.noted.append(target)

    class StubCc:
        def capacity_block_reason(self) -> str:
            return "CommandCode 预算已用尽（剩余 $0.00）"

    plugin._provider = StubProvider()  # noqa: SLF001
    plugin._cc_provider = StubCc()  # noqa: SLF001
    result = asyncio.run(plugin.handle_replyer_before_request(model_name=""))

    assert result["modified_kwargs"]["model_name"] == "deepseek-v4-flash"
    assert plugin._provider.noted == ["deepseek-v4-flash"]  # noqa: SLF001


def test_replyer_hook_ignores_commandcode_when_budget_ok():
    import asyncio

    module = _load_plugin_module()
    plugin = module.DeepEtherProxyPlugin()

    class StubProvider:
        def first_replyer_model(self):
            return ("cc-kimi-k3", "commandcode")

        def next_official_model(self) -> str:  # pragma: no cover
            raise AssertionError("预算充足时不应改写")

    class StubCc:
        def capacity_block_reason(self) -> None:
            return None

    plugin._provider = StubProvider()  # noqa: SLF001
    plugin._cc_provider = StubCc()  # noqa: SLF001
    result = asyncio.run(plugin.handle_replyer_before_request(model_name=""))
    assert "model_name" not in result["modified_kwargs"] or result["modified_kwargs"]["model_name"] == ""

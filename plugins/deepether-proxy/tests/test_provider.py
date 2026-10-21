"""Provider 编排层：网页通道、失败降级、白名单与 web_only 语义。"""

from pathlib import Path
from types import SimpleNamespace

import asyncio
import logging

import pytest

from deepether.config import DeepEtherProxyConfig
from deepether.errors import DeepEtherError
from deepether.provider import DeepEtherProvider
from deepether.state import RuntimeState
from deepether.upstream.sse import StreamSnapshot


class FakeStore:
    """假凭证存储。"""

    def __init__(self, token: str | None = None):
        self.record = SimpleNamespace(token=token) if token else None
        self.invalid_reason = ""
        self.state = "present" if token else "missing"

    def mark_invalid(self, reason: str) -> None:
        self.invalid_reason = reason
        self.state = "invalid"


class FakeLLM:
    """假 ctx.llm。"""

    def __init__(self, success: bool = True):
        self.success = success
        self.calls: list[dict] = []

    async def generate(self, **kwargs):
        self.calls.append({"method": "generate", **kwargs})
        return {"success": self.success, "response": "兜底回复", "reasoning": "", "model_name": kwargs.get("model_name")}

    async def generate_with_tools(self, **kwargs):
        self.calls.append({"method": "generate_with_tools", **kwargs})
        return {"success": self.success, "response": "兜底回复", "reasoning": ""}


class FakePlugin:
    """假插件实例（鸭子类型）。"""

    def __init__(self, config: DeepEtherProxyConfig, *, token: str | None = None, llm=None, tmp_path: Path | None = None):
        self.config = config
        self.credential_store = FakeStore(token)
        self.runtime_state = RuntimeState()
        self.ctx = SimpleNamespace(
            llm=llm,
            paths=SimpleNamespace(runtime_dir=tmp_path, data_dir=tmp_path),
        )

    def get_logger(self):
        return logging.getLogger("test.provider")


class FakeClient:
    """假上游客户端。"""

    def __init__(self, *, snapshot: StreamSnapshot | None = None, error: DeepEtherError | None = None):
        self.snapshot = snapshot or StreamSnapshot(content="网页回复", thinking="网页思考")
        self.error = error
        self.complete_calls: list[dict] = []

    async def create_session(self) -> str:
        return "session-1"

    async def acquire_session(self, *, reuse_limit=None) -> str:
        self.session_reuse_limit = reuse_limit
        return "session-1"

    def invalidate_session(self) -> None:
        self.session_invalidated = True

    async def complete(self, **kwargs):
        self.complete_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.snapshot

    def update_runtime_options(self, *, dump_dir, verbose, session_reuse_calls=None) -> None:
        self.runtime_options = (dump_dir, verbose)
        self.session_reuse_calls = session_reuse_calls

    async def probe_user(self) -> None:
        return None

    async def close(self) -> None:
        return None


def _request(task: str = "utils") -> dict:
    return {
        "context_items": [
            {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "你是麦麦"}]},
            {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "在吗"}]},
        ],
        "model_info": {"name": "deepether-web", "model_type": task},
        "tool_options": [],
        "api_provider": {"timeout": 300},
    }


def _config(**fallback_overrides) -> DeepEtherProxyConfig:
    config = DeepEtherProxyConfig()
    config.fallback.fallback_model_name = fallback_overrides.get("fallback_model_name", "ds-flash")
    config.fallback.fallback_enabled = fallback_overrides.get("fallback_enabled", True)
    config.behavior.mode = fallback_overrides.get("mode", "web_first")
    return config


def _provider(config, client, *, token="tok-123", llm=None, tmp_path=None):
    plugin = FakePlugin(config, token=token, llm=llm or FakeLLM(), tmp_path=tmp_path)
    provider = DeepEtherProvider(plugin)
    provider._client = client  # noqa: SLF001 - 直接注入假客户端
    return provider, plugin


def test_web_success_returns_content(tmp_path: Path):
    provider, plugin = _provider(_config(), FakeClient(), tmp_path=tmp_path)
    result = asyncio.run(provider.get_response(_request()))
    assert result["content"] == "网页回复"
    assert result["reasoning_content"] == "网页思考"
    assert plugin.runtime_state.web_success == 1
    assert plugin.runtime_state.fallback_count == 0


def test_web_failure_falls_back(tmp_path: Path):
    error = DeepEtherError("upstream_timeout", "超时", retryable=True)
    llm = FakeLLM()
    provider, plugin = _provider(_config(), FakeClient(error=error), llm=llm, tmp_path=tmp_path)
    result = asyncio.run(provider.get_response(_request()))
    assert result["content"] == "兜底回复"
    assert plugin.runtime_state.fallback_count == 1
    assert llm.calls and llm.calls[0]["model_name"] == "ds-flash"


def test_web_only_reraises_original(tmp_path: Path):
    error = DeepEtherError("upstream_timeout", "超时", retryable=True)
    provider, plugin = _provider(_config(mode="web_only"), FakeClient(error=error), tmp_path=tmp_path)
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(provider.get_response(_request()))
    assert excinfo.value.code == "upstream_timeout"
    assert plugin.runtime_state.fallback_count == 0


def test_missing_token_falls_back(tmp_path: Path):
    llm = FakeLLM()
    provider, plugin = _provider(_config(), FakeClient(), token=None, llm=llm, tmp_path=tmp_path)
    result = asyncio.run(provider.get_response(_request()))
    assert result["content"] == "兜底回复"
    assert plugin.runtime_state.fallback_count == 1


def test_missing_token_without_fallback_model_raises(tmp_path: Path):
    config = _config()
    config.fallback.fallback_model_name = ""
    provider, plugin = _provider(config, FakeClient(), token=None, tmp_path=tmp_path)
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(provider.get_response(_request()))
    assert excinfo.value.code == "fallback_unavailable"


def test_model_not_in_whitelist_reraises(tmp_path: Path):
    """白名单按模型名生效：不在名单里的模型不会降级，直接失败让 Host 切下一个模型。"""

    error = DeepEtherError("upstream_timeout", "超时", retryable=True)
    config = _config()
    config.fallback.fallback_for_models = ["deepether-utils"]
    provider, plugin = _provider(config, FakeClient(error=error), tmp_path=tmp_path)
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(provider.get_response(_request()))
    assert excinfo.value.code == "upstream_timeout"


def test_thinking_only_response_falls_back(tmp_path: Path):
    """报告 §4.5.3 细节 2：只有思考没有正文必须判失败，不能把空正文交给 Host。"""

    llm = FakeLLM()
    thinking_only = StreamSnapshot(content="", thinking="我在想")
    provider, plugin = _provider(_config(), FakeClient(snapshot=thinking_only), llm=llm, tmp_path=tmp_path)
    result = asyncio.run(provider.get_response(_request()))
    assert result["content"] == "兜底回复"
    assert plugin.runtime_state.fallback_count == 1


def test_thinking_only_without_fallback_raises(tmp_path: Path):
    config = _config(mode="web_only")
    thinking_only = StreamSnapshot(content="", thinking="我在想")
    provider, plugin = _provider(config, FakeClient(snapshot=thinking_only), tmp_path=tmp_path)
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(provider.get_response(_request()))
    assert excinfo.value.code == "upstream_empty"


def test_prompt_style_from_model_extra_params(tmp_path: Path):
    provider, plugin = _provider(_config(), FakeClient(), tmp_path=tmp_path)
    request = _request()
    request["extra_params"] = {"deepether_style": "replyer"}
    asyncio.run(provider.get_response(request))
    prompt = provider._client.complete_calls[0]["prompt"]
    assert "口语化" in prompt


def test_thinking_flag_override_from_model_extra_params(tmp_path: Path):
    provider, plugin = _provider(_config(), FakeClient(), tmp_path=tmp_path)
    request = _request()
    request["extra_params"] = {"deepether_thinking": False}
    asyncio.run(provider.get_response(request))
    assert provider._client.complete_calls[0]["thinking_enabled"] is False


def test_empty_upstream_triggers_fallback(tmp_path: Path):
    empty = StreamSnapshot(content="", thinking="")
    llm = FakeLLM()
    provider, plugin = _provider(_config(), FakeClient(snapshot=empty), llm=llm, tmp_path=tmp_path)
    result = asyncio.run(provider.get_response(_request()))
    assert result["content"] == "兜底回复"
    assert plugin.runtime_state.fallback_count == 1


def test_empty_context_raises_protocol_error(tmp_path: Path):
    provider, plugin = _provider(_config(), FakeClient(), tmp_path=tmp_path)
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(provider.get_response({"context_items": [], "model_info": {}}))
    assert excinfo.value.code == "protocol_error"


def test_output_items_protocol(tmp_path: Path):
    config = _config()
    config.behavior.response_protocol = "output_items"
    provider, plugin = _provider(config, FakeClient(), tmp_path=tmp_path)
    result = asyncio.run(provider.get_response(_request()))
    assert result["item_schema_version"] == 1
    assert [item["item_type"] for item in result["output_items"]] == ["ReasoningItem", "AssistantMessageItem"]


def test_embedding_is_not_supported(tmp_path: Path):
    provider, plugin = _provider(_config(), FakeClient(), tmp_path=tmp_path)
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(provider.get_embedding({}))
    assert excinfo.value.code == "not_supported"


class BlockingClient(FakeClient):
    """第一次调用会阻塞，直到测试主动放行——用于制造真实的闸门争用。"""

    def __init__(self) -> None:
        super().__init__()
        self.release_event = asyncio.Event()

    async def complete(self, **kwargs):
        self.complete_calls.append(kwargs)
        await self.release_event.wait()
        return self.snapshot


def test_queue_rejection_does_not_open_circuit(tmp_path: Path):
    """并发闸门拒绝属于本地容量信号，不能计入熔断器的上游连续失败。

    否则一次群聊突发就会把熔断打开，让随后 cooldown 内的请求全部绕过网页版，
    短视频里的排队被放大成 60s 的整段降级（§6.6.1 的触发条件是上游连续失败）。
    """

    config = _config()
    config.concurrency.max_concurrency = 1
    config.concurrency.queue_wait_sec = 1
    config.concurrency.circuit_fail_threshold = 1
    llm = FakeLLM()
    client = BlockingClient()
    provider, plugin = _provider(config, client, llm=llm, tmp_path=tmp_path)

    async def scenario():
        first = asyncio.create_task(provider.get_response(_request()))
        await asyncio.sleep(0.05)  # 让第一个请求占满唯一槽位
        rejected = await provider.get_response(_request())
        client.release_event.set()
        return rejected, await first

    rejected, first = asyncio.run(scenario())

    assert rejected["content"] == "兜底回复"
    assert first["content"] == "网页回复"
    assert len(client.complete_calls) == 1
    assert plugin.runtime_state.fallback_count == 1
    # 阈值=1：如果排队拒绝被计入失败，熔断器此刻必然是 open
    assert provider.circuit_state == "closed"

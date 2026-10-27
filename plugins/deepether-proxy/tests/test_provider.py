"""Provider 编排层：网页通道、失败交回 Host 的契约、并发闸门与熔断。

0.3.0 起插件不再接管 fallback：任何网页版失败都抛 `DeepEtherError`（Runner 转成
`success=false`），由 Host 按任务 `model_list` + `selection_strategy=sequential`
切到下一个模型。因此这里的失败用例一律断言「异常原样抛出、失败已计数、
不碰 Host 的 `ctx.llm`」，而不是断言某种兜底回复。
"""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

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


class CtxWithoutLLM:
    """只提供 `paths` 的 Host 上下文。

    0.3.0 起插件不再回调 Host 官方模型，Provider 也不应该再访问 `ctx.llm`；
    一旦访问就直接让用例失败，把「插件不依赖 Host 回落」变成可执行的契约。
    """

    def __init__(self, tmp_path: Path | None) -> None:
        self.paths = SimpleNamespace(runtime_dir=tmp_path, data_dir=tmp_path)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        raise AssertionError(
            f"Provider 访问了 Host 上下文 ctx.{name}：0.3.0 起插件不再接管 fallback，"
            "失败必须直接交回 Host"
        )


class FakePlugin:
    """假插件实例（鸭子类型）。"""

    def __init__(
        self,
        config: DeepEtherProxyConfig,
        *,
        token: str | None = None,
        tmp_path: Path | None = None,
    ):
        self.config = config
        self.credential_store = FakeStore(token)
        self.runtime_state = RuntimeState()
        self.ctx = CtxWithoutLLM(tmp_path)

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


def _config(*, mode: str = "web_first") -> DeepEtherProxyConfig:
    config = DeepEtherProxyConfig()
    config.behavior.mode = mode
    # 节奏控制在 test_antiban.py 单独覆盖；这里关掉，避免每个请求真等 8~20 秒。
    config.antiban.enabled = False
    return config


def _provider(config, client, *, token="tok-123", tmp_path=None):
    plugin = FakePlugin(config, token=token, tmp_path=tmp_path)
    provider = DeepEtherProvider(plugin)
    provider._client = client  # noqa: SLF001 - 直接注入假客户端
    return provider, plugin


# ─── 成功路径 ───────────────────────────────────────────────────────────


def test_web_success_returns_content(tmp_path: Path):
    client = FakeClient()
    provider, plugin = _provider(_config(), client, tmp_path=tmp_path)

    result = asyncio.run(provider.get_response(_request()))

    assert result["content"] == "网页回复"
    assert result["reasoning_content"] == "网页思考"
    assert plugin.runtime_state.web_success == 1
    assert plugin.runtime_state.web_failure == 0
    assert plugin.runtime_state.failures_last_24h == 0
    assert len(client.complete_calls) == 1


# ─── 失败一律交回 Host ──────────────────────────────────────────────────


def test_upstream_failure_is_reraised_unchanged_and_counted(tmp_path: Path):
    """上游错误原样抛出，Host 据此判定本模型失败并切下一个；失败即已交回 Host。"""

    error = DeepEtherError("upstream_timeout", "上游超时", retryable=True)
    provider, plugin = _provider(_config(), FakeClient(error=error), tmp_path=tmp_path)

    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(provider.get_response(_request()))

    assert excinfo.value is error  # 不包装成 internal，错误码保持可检索
    assert excinfo.value.code == "upstream_timeout"
    assert plugin.runtime_state.web_failure == 1


def test_missing_token_raises_credential_missing(tmp_path: Path):
    client = FakeClient()
    provider, plugin = _provider(_config(), client, token=None, tmp_path=tmp_path)

    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(provider.get_response(_request()))

    assert excinfo.value.code == "credential_missing"
    assert plugin.runtime_state.web_failure == 1
    assert client.complete_calls == []  # 没凭证就不该打上游


def test_thinking_only_response_raises_upstream_empty(tmp_path: Path):
    """报告 §4.5.3 细节 2：只有思考没有正文必须判失败，不能把空正文当成功交回 Host。"""

    thinking_only = StreamSnapshot(content="", thinking="我在想")
    provider, plugin = _provider(_config(), FakeClient(snapshot=thinking_only), tmp_path=tmp_path)

    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(provider.get_response(_request()))

    assert excinfo.value.code == "upstream_empty"
    assert "思考" in excinfo.value.message
    assert plugin.runtime_state.web_failure == 1


def test_empty_body_raises_upstream_empty(tmp_path: Path):
    empty = StreamSnapshot(content="", thinking="")
    provider, plugin = _provider(_config(), FakeClient(snapshot=empty), tmp_path=tmp_path)

    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(provider.get_response(_request()))

    assert excinfo.value.code == "upstream_empty"
    assert "正文" in excinfo.value.message
    assert plugin.runtime_state.web_failure == 1


def test_circuit_open_raises_circuit_open(tmp_path: Path):
    """连续失败到阈值后熔断：后续请求直接失败交回 Host，且不再打上游。"""

    config = _config()
    config.concurrency.circuit_fail_threshold = 1
    error = DeepEtherError("upstream_timeout", "上游超时", retryable=True)
    client = FakeClient(error=error)
    provider, plugin = _provider(config, client, tmp_path=tmp_path)

    with pytest.raises(DeepEtherError) as first_error:
        asyncio.run(provider.get_response(_request()))
    assert first_error.value.code == "upstream_timeout"
    assert provider.circuit_state == "open"

    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(provider.get_response(_request()))

    assert excinfo.value.code == "circuit_open"
    assert plugin.runtime_state.web_failure == 2  # 两次都是「已交回 Host」
    assert len(client.complete_calls) == 1


def test_official_only_raises_channel_disabled(tmp_path: Path):
    """official_only 下插件不接管网页版：即使 Host 仍把请求送进来，也立刻交回。"""

    client = FakeClient()
    provider, plugin = _provider(_config(mode="official_only"), client, tmp_path=tmp_path)

    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(provider.get_response(_request()))

    assert excinfo.value.code == "channel_disabled"
    assert "official_only" in excinfo.value.message
    assert plugin.runtime_state.web_failure == 1
    assert client.complete_calls == []


def test_failure_is_observable_without_host(tmp_path: Path):
    """失败可见性：插件看不到 Host 是否切换成功，所以状态必须自己记录失败。

    `CtxWithoutLLM` 已经保证 Provider 不碰 `ctx.llm`；这里再断言失败计数与
    最近错误都已落到 runtime_state，供 `/deepether-status` 展示。
    """

    error = DeepEtherError("upstream_http", "上游返回 502", retryable=True)
    provider, plugin = _provider(_config(), FakeClient(error=error), tmp_path=tmp_path)

    with pytest.raises(DeepEtherError):
        asyncio.run(provider.get_response(_request()))

    state = plugin.runtime_state
    assert state.web_failure == 1
    assert state.failures_last_24h == 1
    assert state.last_error == "[upstream_http] 上游返回 502"
    assert state.last_error_at is not None


# ─── 其它协议契约 ───────────────────────────────────────────────────────


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


# ─── 并发闸门 ───────────────────────────────────────────────────────────


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
    把短视频里的排队放大成 60s 的整段交回 Host（§6.6.1 的触发条件是上游连续失败）。
    被拒绝的请求同样要抛 `concurrency_rejected`，由 Host 切下一个模型。
    """

    config = _config()
    config.concurrency.max_concurrency = 1
    config.concurrency.queue_wait_sec = 1
    config.concurrency.circuit_fail_threshold = 1
    client = BlockingClient()
    provider, plugin = _provider(config, client, tmp_path=tmp_path)

    async def scenario():
        first = asyncio.create_task(provider.get_response(_request()))
        await asyncio.sleep(0.05)  # 让第一个请求占满唯一槽位
        rejected: DeepEtherError | None = None
        try:
            await provider.get_response(_request())
        except DeepEtherError as exc:
            rejected = exc
        client.release_event.set()
        return rejected, await first

    rejected, first = asyncio.run(scenario())

    assert rejected is not None
    assert rejected.code == "concurrency_rejected"
    assert first["content"] == "网页回复"
    assert len(client.complete_calls) == 1
    assert plugin.runtime_state.web_failure == 1  # 只有被拒绝的那次失败
    assert provider.gate_rejected == 1
    # 阈值=1：如果排队拒绝被计入失败，熔断器此刻必然是 open
    assert provider.circuit_state == "closed"

def test_antiban_config_is_fully_mapped_to_pacing(tmp_path: Path):
    """契约：AntibanConfig 的每个容量字段都必须真的映射进 PacingConfig。

    这类"配置写了但没接上线"的漏接（例如只改了 config.py 却忘了 provider 的构造参数）
    单测平时跑不出来，只有逐字段比对才能挡住。
    """

    config = DeepEtherProxyConfig()
    antiban = config.antiban
    antiban.max_per_minute = 4
    antiban.max_per_hour = 51
    antiban.max_per_day = 500
    antiban.adaptive_budget = True
    antiban.min_adaptive_budget = 210
    antiban.quota_wait_max_sec = 17.0
    antiban.quiet_hours = "02:00-06:00"
    antiban.quiet_hours_ratio = 0.3
    antiban.model_daily_limits = "deepether-utils=123"
    antiban.reserve_daily_budget = 61
    antiban.reserve_models = "deepether-replyer,deepether-other"

    provider = DeepEtherProvider(FakePlugin(config, token="tok", tmp_path=tmp_path))
    pacing = provider._ensure_pacing()  # noqa: SLF001 - 直接检查构造结果
    mapped = pacing._config  # noqa: SLF001

    assert mapped.max_per_minute == 4
    assert mapped.max_per_hour == 51
    assert mapped.max_per_day == 500
    assert mapped.adaptive_budget is True
    assert mapped.min_adaptive_budget == 210
    assert mapped.quota_wait_max_sec == 17.0
    assert mapped.quiet_hours == "02:00-06:00"
    assert mapped.quiet_hours_ratio == 0.3
    assert mapped.model_daily_limits == {"deepether-utils": 123}
    assert mapped.reserve_daily_budget == 61
    assert mapped.reserve_models == ("deepether-replyer", "deepether-other")


def test_request_model_name_reads_host_model_info(tmp_path: Path):
    """反封禁按模型名分级，模型名必须从 Host 请求里如实取到。"""

    provider = DeepEtherProvider(FakePlugin(DeepEtherProxyConfig(), tmp_path=tmp_path))
    assert provider._request_model_name({"model_info": {"name": "deepether-replyer"}}) == "deepether-replyer"
    assert provider._request_model_name({"model_info": {"model_identifier": "deepwind"}}) == "deepwind"
    assert provider._request_model_name({"model_info": {}}) == ""
    assert provider._request_model_name({}) == ""

# ─── 额度让位（0.5.0）：replyer hook 的判定与目标解析 ─────────────────────


def _write_model_config(tmp_path: Path, *, replyer_list: list[str], extra_models: list[str] | None = None) -> Path:
    """写一份最小 model_config.toml，并返回路径。"""

    lines = []
    models = ["deepseek-v4-flash", "deepether-replyer", "deepether-utils"] + list(extra_models or [])
    providers = {"deepseek-v4-flash": "DeepSeek", "deepether-replyer": "deepether", "deepether-utils": "deepether"}
    for name in models:
        provider = providers.get(name, "DeepSeek")
        lines.extend(
            [
                "[[models]]",
                'model_type = "llm"',
                f'model_identifier = "{name}"',
                f'name = "{name}"',
                f'api_provider = "{provider}"',
                "",
            ]
        )
    lines.extend(
        [
            "[model_task_config.replyer]",
            "model_list = [" + ", ".join(f'"{item}"' for item in replyer_list) + "]",
            "",
        ]
    )
    path = tmp_path / "model_config.toml"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _yield_provider(tmp_path: Path, *, replyer_list: list[str], **config_overrides) -> DeepEtherProvider:
    config = DeepEtherProxyConfig()
    config.provision.model_config_path = str(_write_model_config(tmp_path, replyer_list=replyer_list))
    for key, value in config_overrides.items():
        section, _, field = key.partition(".")
        setattr(getattr(config, section), field, value)
    return DeepEtherProvider(FakePlugin(config, token="tok", tmp_path=tmp_path))


def test_capacity_ok_returns_none(tmp_path: Path) -> None:
    provider = _yield_provider(tmp_path, replyer_list=["deepether-replyer", "deepseek-v4-flash"])
    assert provider.capacity_block_reason() is None
    assert provider.replyer_yield_target() is None


def test_capacity_reports_day_quota_exhausted(tmp_path: Path) -> None:
    provider = _yield_provider(
        tmp_path,
        replyer_list=["deepether-replyer", "deepseek-v4-flash"],
        **{"antiban.max_per_day": 1, "antiban.adaptive_budget": False},
    )
    pacing = provider._ensure_pacing()  # noqa: SLF001
    asyncio.run(pacing.acquire(model_name="deepether-replyer"))

    reason = provider.capacity_block_reason()
    assert reason is not None and "日额度" in reason


def test_yield_target_prefers_official_model_in_task_list(tmp_path: Path) -> None:
    provider = _yield_provider(
        tmp_path,
        replyer_list=["deepether-replyer", "deepseek-v4-flash"],
        **{"antiban.max_per_day": 1, "antiban.adaptive_budget": False},
    )
    asyncio.run(provider._ensure_pacing().acquire(model_name="deepether-replyer"))  # noqa: SLF001

    assert provider.replyer_yield_target() == "deepseek-v4-flash"


def test_yield_target_follows_task_model_list_order(tmp_path: Path) -> None:
    """插件不持有兜底模型：返回的是你自己 model_list 里的第一个非本插件模型。"""

    provider = _yield_provider(
        tmp_path,
        replyer_list=["deepseek-v4-flash", "deepether-replyer"],
        **{"antiban.max_per_day": 1, "antiban.adaptive_budget": False},
    )
    asyncio.run(provider._ensure_pacing().acquire(model_name="deepether-replyer"))  # noqa: SLF001

    assert provider.replyer_yield_target() == "deepseek-v4-flash"

def test_yield_target_is_none_without_official_model(tmp_path: Path) -> None:
    provider = _yield_provider(
        tmp_path,
        replyer_list=["deepether-replyer"],
        **{"antiban.max_per_day": 1, "antiban.adaptive_budget": False},
    )
    asyncio.run(provider._ensure_pacing().acquire(model_name="deepether-replyer"))  # noqa: SLF001

    assert provider.capacity_block_reason() is not None
    assert provider.replyer_yield_target() is None

# ─── 多账号池（0.5.0）：轮换与隔离 ───────────────────────────────────────


def _pool_provider(tmp_path: Path, tokens: list[str], **overrides) -> DeepEtherProvider:
    """构造一个多账号 provider（不触网：客户端由用例注入）。"""

    config = DeepEtherProxyConfig()
    config.credential.web_tokens = list(tokens)
    config.antiban.max_per_day = 1
    config.antiban.adaptive_budget = False
    config.antiban.reserve_daily_budget = 0
    config.provision.model_config_path = str(
        _write_model_config(tmp_path, replyer_list=["deepether-replyer", "deepseek-v4-flash"])
    )
    for key, value in overrides.items():
        section, _, field = key.partition(".")
        setattr(getattr(config, section), field, value)
    return DeepEtherProvider(FakePlugin(config, token=None, tmp_path=tmp_path))


def test_rotation_uses_next_account_when_first_exhausted(tmp_path: Path) -> None:
    tokens = ["token-account-a", "token-account-b"]
    provider = _pool_provider(tmp_path, tokens)
    account_a = provider._account_id(tokens[0])  # noqa: SLF001
    account_b = provider._account_id(tokens[1])  # noqa: SLF001
    fake_a, fake_b = FakeClient(), FakeClient()
    provider._clients = {account_a: fake_a, account_b: fake_b}  # noqa: SLF001

    # 把 A 的日额度用掉（max_per_day=1）
    asyncio.run(provider._ensure_pacing(account_a).acquire(model_name="deepether-replyer"))  # noqa: SLF001

    result = asyncio.run(provider.get_response(_request()))

    assert result["content"] == "网页回复"
    assert fake_a.complete_calls == []
    assert len(fake_b.complete_calls) == 1


def test_all_accounts_exhausted_raises_quota(tmp_path: Path) -> None:
    tokens = ["token-account-a", "token-account-b"]
    provider = _pool_provider(tmp_path, tokens)
    provider._clients = {  # noqa: SLF001
        provider._account_id(tokens[0]): FakeClient(),  # noqa: SLF001
        provider._account_id(tokens[1]): FakeClient(),  # noqa: SLF001
    }
    for token in tokens:
        asyncio.run(provider._ensure_pacing(provider._account_id(token)).acquire())  # noqa: SLF001

    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(provider.get_response(_request()))
    assert excinfo.value.code == "pacing_quota"


def test_capacity_is_ok_when_any_account_has_room(tmp_path: Path) -> None:
    tokens = ["token-account-a", "token-account-b"]
    provider = _pool_provider(tmp_path, tokens)
    account_a = provider._account_id(tokens[0])  # noqa: SLF001
    asyncio.run(provider._ensure_pacing(account_a).acquire())  # noqa: SLF001

    assert provider.capacity_block_reason() is None

    account_b = provider._account_id(tokens[1])  # noqa: SLF001
    asyncio.run(provider._ensure_pacing(account_b).acquire())  # noqa: SLF001
    assert provider.capacity_block_reason() is not None

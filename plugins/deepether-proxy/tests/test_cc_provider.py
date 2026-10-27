"""CommandCode Provider：编排、预算、熔断与 pause_turn。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from deepether.commandcode.protocol import CcResult, CcToolCall, CcUsage
from deepether.commandcode.provider import CommandCodeProvider
from deepether.config import DeepEtherProxyConfig
from deepether.errors import DeepEtherError, ErrorCode


class SilentLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


class FakePlugin:
    def __init__(self, config, runtime_dir: Path):
        self.config = config
        self.ctx = SimpleNamespace(paths=SimpleNamespace(runtime_dir=str(runtime_dir)))

    def get_logger(self):
        return SilentLogger()


class FakeClient:
    """按顺序返回预置结果；记录收到的信封。"""

    def __init__(self, *results):
        self.results = list(results)
        self.envelopes = []

    async def generate(self, envelope, *, timeout_sec=None):
        self.envelopes.append(envelope)
        if not self.results:
            raise AssertionError("没有预置结果了")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def make_config(**commandcode):
    """0.5.0 起 CommandCode 只有 api_key 一个配置项。"""

    settings = {"api_key": "user_test_key"}
    settings.update(commandcode)
    return DeepEtherProxyConfig(commandcode=settings)


def make_request(*, model_identifier="deepseek/deepseek-v4-flash", content="你好", visual=False):
    return {
        "item_schema_version": 1,
        "request_kind": "response",
        "logical_turn_id": "turn-1",
        "temperature": 0.7,
        "max_tokens": None,
        "response_format": None,
        "extra_params": {},
        "model_info": {
            "name": "cc-model",
            "model_identifier": model_identifier,
            "api_provider": "commandcode",
            "extra_params": {},
            "force_stream_mode": False,
            "max_tokens": None,
            "model_type": "llm",
            "send_temperature": False,
            "temperature": None,
            "visual": visual,
        },
        "tool_options": [],
        "context_items": [
            {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "系统"}]},
            {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": content}]},
        ],
        "api_provider": {"timeout": 300, "api_key": ""},
    }


def make_provider(tmp_path, *, client=None, config=None):
    plugin = FakePlugin(config or make_config(), tmp_path)
    provider = CommandCodeProvider(plugin)
    if client is not None:
        provider._ensure_client = lambda: client  # type: ignore[method-assign]
    return provider


def text_result(content="好的", reasoning=""):
    return CcResult(
        content=content,
        reasoning=reasoning,
        usage=CcUsage(input_tokens=100, output_tokens=20, total_tokens=120),
        finish_reason="stop",
    )


@pytest.mark.asyncio
async def test_successful_response_builds_output_items(tmp_path):
    client = FakeClient(text_result("你好呀", "先想一想"))
    provider = make_provider(tmp_path, client=client)

    response = await provider.get_response(make_request())

    assert response["item_schema_version"] == 1
    assert [item["item_type"] for item in response["output_items"]] == [
        "ReasoningItem",
        "AssistantMessageItem",
    ]
    assert response["usage"]["prompt_tokens"] == 100
    assert provider.state.success == 1
    assert provider.budget_snapshot["entries"] == 1
    assert provider._budget.spent(30 * 24 * 3600) > 0
    envelope = client.envelopes[0]
    assert envelope["params"]["model"] == "deepseek/deepseek-v4-flash"
    assert envelope["params"]["system"] == "系统"
    assert envelope["threadId"]


@pytest.mark.asyncio
async def test_missing_key_is_reported_before_any_request(tmp_path):
    client = FakeClient(text_result())
    provider = make_provider(tmp_path, client=client, config=make_config(api_key=""))

    with pytest.raises(DeepEtherError) as error:
        await provider.get_response(make_request())

    assert error.value.code == ErrorCode.CREDENTIAL_MISSING
    assert client.envelopes == []
    assert provider.state.failure == 1




@pytest.mark.asyncio
async def test_budget_precheck_blocks_before_upstream(tmp_path):
    """预算上限已是代码内常量：用一份「已经花光」的记账文件让前置检查失败。"""

    import json
    import time

    state = {"version": 1, "entries": [{"ts": time.time(), "cost": 100.0, "model": "x"}]}
    (tmp_path / "commandcode_budget.json").write_text(json.dumps(state), encoding="utf-8")

    client = FakeClient(text_result())
    provider = make_provider(tmp_path, client=client)
    with pytest.raises(DeepEtherError) as error:
        await provider.get_response(make_request())
    assert error.value.code == ErrorCode.BUDGET_EXHAUSTED
    assert client.envelopes == []


@pytest.mark.asyncio
async def test_tool_calls_are_returned_as_function_call_items(tmp_path):
    result = CcResult(finish_reason="tool-calls")
    result.tool_calls = [CcToolCall(call_id="c1", name="reply", arguments={"text": "hi"})]
    provider = make_provider(tmp_path, client=FakeClient(result))

    response = await provider.get_response(make_request())

    call = response["output_items"][0]
    assert call["item_type"] == "FunctionCallItem"
    assert call["tool_call"]["func_name"] == "reply"


@pytest.mark.asyncio
async def test_circuit_opens_after_repeated_failures(tmp_path):
    failure = DeepEtherError(ErrorCode.PROTOCOL_ERROR, "上游异常")
    client = FakeClient(failure, failure, failure, text_result())
    provider = make_provider(tmp_path, client=client)

    for _ in range(3):
        with pytest.raises(DeepEtherError):
            await provider.get_response(make_request())
    assert provider.circuit_state == "open"

    with pytest.raises(DeepEtherError) as error:
        await provider.get_response(make_request())
    assert error.value.code == ErrorCode.CIRCUIT_OPEN
    assert len(client.envelopes) == 3


@pytest.mark.asyncio
async def test_pause_turn_is_continued_and_merged(tmp_path):
    first = CcResult(content="前半", finish_reason="pause-turn")
    second = text_result("后半")
    client = FakeClient(first, second)
    provider = make_provider(tmp_path, client=client)

    response = await provider.get_response(make_request())

    assert len(client.envelopes) == 2
    assistant = [item for item in response["output_items"] if item["item_type"] == "AssistantMessageItem"][0]
    assert assistant["parts"][0]["text"] == "前半后半"
    assert response["raw_data"]["rounds"] == 2
    # 续跑请求里带上了上一轮的 assistant 内容
    assert client.envelopes[1]["params"]["messages"][-1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_thread_id_is_reused_per_model(tmp_path):
    client = FakeClient(text_result(), text_result(), text_result())
    provider = make_provider(tmp_path, client=client)

    await provider.get_response(make_request())
    await provider.get_response(make_request())
    ids = [envelope["threadId"] for envelope in client.envelopes]
    assert ids[0] and ids[0] == ids[1]  # 同模型复用同一个 threadId（吃 prompt cache）

    await provider.get_response(make_request(model_identifier="deepseek/deepseek-v4-pro"))
    assert client.envelopes[-1]["threadId"] != ids[0]  # 换模型换 thread


@pytest.mark.asyncio
async def test_unknown_model_still_passes_identifier_through(tmp_path):
    client = FakeClient(text_result())
    provider = make_provider(tmp_path, client=client)
    await provider.get_response(make_request(model_identifier="vendor/unknown-model"))
    assert client.envelopes[0]["params"]["model"] == "vendor/unknown-model"


@pytest.mark.asyncio
async def test_embedding_and_audio_are_not_supported(tmp_path):
    provider = make_provider(tmp_path, client=FakeClient())
    with pytest.raises(DeepEtherError) as embedding:
        await provider.get_embedding({})
    assert embedding.value.code == ErrorCode.NOT_SUPPORTED
    with pytest.raises(DeepEtherError) as audio:
        await provider.get_audio_transcriptions({})
    assert audio.value.code == ErrorCode.NOT_SUPPORTED


@pytest.mark.asyncio
async def test_channel_concurrency_uses_default_limit(tmp_path):
    """并发上限不再暴露给用户：默认 2（见 commandcode 配置常量）。"""

    provider = make_provider(tmp_path, client=FakeClient(text_result()))
    await provider.get_response(make_request())
    assert provider.gate_status == (0, 2)


@pytest.mark.asyncio
async def test_concurrent_requests_are_serialized_by_gate(tmp_path):
    client = FakeClient(text_result(), text_result())
    provider = make_provider(tmp_path, client=client)

    await asyncio.gather(provider.get_response(make_request()), provider.get_response(make_request()))

    assert provider.state.success == 2
    assert len(client.envelopes) == 2

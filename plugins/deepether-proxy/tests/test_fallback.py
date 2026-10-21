"""降级执行器：白名单、递归守卫、超时裁剪。"""

import asyncio

import pytest

from deepether.config import FallbackConfig
from deepether.errors import DeepEtherError
from deepether.fallback import FallbackRunner, model_allowed, resolve_model_name


class FakeLLM:
    """记录调用参数的假 ctx.llm。"""

    def __init__(self, *, success: bool = True, response: str = "兜底回复", reasoning: str = "兜底思考"):
        self.success = success
        self.response = response
        self.reasoning = reasoning
        self.calls: list[dict] = []

    async def generate(self, **kwargs):
        self.calls.append({"method": "generate", **kwargs})
        if not self.success:
            return {"success": False, "error": "模型不可用"}
        return {
            "success": True,
            "response": self.response,
            "reasoning": self.reasoning,
            "model_name": kwargs.get("model_name"),
            "total_tokens": 5,
        }

    async def generate_with_tools(self, **kwargs):
        self.calls.append({"method": "generate_with_tools", **kwargs})
        return {"success": True, "response": self.response, "reasoning": self.reasoning, "total_tokens": 5}


def _messages():
    return [{"role": "user", "content": "你好"}]


def test_missing_model_name_raises():
    runner = FallbackRunner(llm=FakeLLM(), logger=None)
    config = FallbackConfig(fallback_model_name="")
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(
            runner.run(
                messages=_messages(),
                request={},
                config=config,
                protocol="legacy",
                remaining_ms=10000,
                reason="测试",
            )
        )
    assert excinfo.value.code == "fallback_unavailable"


def test_disabled_raises():
    runner = FallbackRunner(llm=FakeLLM(), logger=None)
    config = FallbackConfig(fallback_enabled=False, fallback_model_name="m")
    with pytest.raises(DeepEtherError):
        asyncio.run(
            runner.run(messages=_messages(), request={}, config=config, protocol="legacy", remaining_ms=0, reason="x")
        )


def test_successful_fallback_returns_content_and_caps_timeout():
    llm = FakeLLM()
    runner = FallbackRunner(llm=llm, logger=None)
    config = FallbackConfig(fallback_model_name="ds-flash", fallback_timeout_ms=120000)

    result = asyncio.run(
        runner.run(
            messages=_messages(),
            request={},
            config=config,
            protocol="legacy",
            remaining_ms=5000,
            reason="上游超时",
        )
    )
    assert result["content"] == "兜底回复"
    assert result["reasoning_content"] == "兜底思考"
    call = llm.calls[0]
    assert call["model_name"] == "ds-flash"
    assert call["timeout_ms"] == 5000  # 被剩余预算裁剪


def test_fallback_uses_tools_when_request_has_tool_options():
    llm = FakeLLM()
    runner = FallbackRunner(llm=llm, logger=None)
    config = FallbackConfig(fallback_model_name="m", fallback_with_tools=True)
    asyncio.run(
        runner.run(
            messages=_messages(),
            request={"tool_options": [{"type": "function", "function": {"name": "f"}}]},
            config=config,
            protocol="legacy",
            remaining_ms=0,
            reason="x",
        )
    )
    assert llm.calls[0]["method"] == "generate_with_tools"


def test_recursive_fallback_is_blocked():
    llm = FakeLLM()
    runner = FallbackRunner(llm=llm, logger=None)
    config = FallbackConfig(fallback_model_name="m")

    async def nested():
        return await runner.run(
            messages=_messages(), request={}, config=config, protocol="legacy", remaining_ms=0, reason="outer"
        )

    # 模拟兜底模型又指回本插件：深度守卫应直接拒绝
    from deepether import fallback as fallback_module

    token = fallback_module._FALLBACK_DEPTH.set(1)
    try:
        with pytest.raises(DeepEtherError) as excinfo:
            asyncio.run(nested())
        assert "递归" in excinfo.value.message
    finally:
        fallback_module._FALLBACK_DEPTH.reset(token)


def test_model_name_and_whitelist():
    assert resolve_model_name({"model_info": {"name": "deepether-replyer"}}) == "deepether-replyer"
    assert resolve_model_name({"model_info": {"model_type": "llm"}}) == "unknown"
    assert resolve_model_name({}) == "unknown"
    assert model_allowed("deepether-replyer", []) is True  # 空列表 = 全部允许
    assert model_allowed("deepether-replyer", ["deepether-replyer"]) is True
    assert model_allowed("deepseek-v4-flash", ["deepether-replyer"]) is False

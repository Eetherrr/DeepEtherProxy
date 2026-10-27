"""CommandCode 命令文本与节奏控制。"""

import pytest

from deepether.commandcode.catalog import load_catalog
from deepether.commandcode.pacing import CommandCodePacer
from deepether.commandcode.state import CcRuntimeState
from deepether.commandcode.status import render_commandcode_models, render_commandcode_status
from deepether.errors import DeepEtherError, ErrorCode


def test_status_text_hides_key_and_shows_budget():
    state = CcRuntimeState()
    state.record_success(123)
    text = render_commandcode_status(
        version="0.4.0",
        enabled=True,
        api_key="user_secret_key_123456",
        base_url="https://api.commandcode.ai",
        cli_version="1.88.0",
        catalog=load_catalog(),
        register_models="all",
        registered_models=56,
        state=state,
        budget={
            "five_hour_used": 0.1,
            "five_hour_limit": 3.0,
            "weekly_used": 0.2,
            "weekly_limit": 6.0,
            "monthly_used": 0.3,
            "monthly_limit": 9.0,
            "remaining": 2.7,
            "reserve": 0.5,
            "entries": 3,
        },
        circuit_state="closed",
        gate_in_flight=0,
        gate_limit=2,
        gate_rejected=0,
        last_model="deepseek/deepseek-v4-flash",
        thread_id="thread-x",
    )
    assert "user_secret_key_123456" not in text
    assert "user…(" in text or "user…" in text
    assert "56" in text and "已注册：56 条" in text
    assert "$0.1000 / $3.00" in text
    assert "成功：1 次" in text
    assert "不自动接线" in text


def test_status_text_without_budget():
    text = render_commandcode_status(
        version="0.4.0",
        enabled=False,
        api_key="",
        base_url="https://api.commandcode.ai",
        cli_version="1.88.0",
        catalog=load_catalog(),
        register_models="curated",
        registered_models=0,
        state=CcRuntimeState(),
        budget=None,
        circuit_state="closed",
        gate_in_flight=0,
        gate_limit=0,
        gate_rejected=0,
    )
    assert "已关闭" in text
    assert "暂无预算数据" in text


def test_models_text_marks_registered_and_filters():
    catalog = load_catalog()
    text = render_commandcode_models(catalog, ["cc-deepseek-v4-flash"])
    assert "CommandCode Go 模型：56 条" in text
    assert "cc-deepseek-v4-flash（deepseek/deepseek-v4-flash" in text
    assert "已注册" in text
    assert "未注册" in text

    filtered = render_commandcode_models(catalog, [], keyword="kimi")
    assert "kimi" in filtered.lower()
    assert "qwen" not in filtered.lower()

    empty = render_commandcode_models(catalog, [], keyword="不存在的模型")
    assert "没有匹配的模型" in empty


class FakeSleep:
    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.mark.asyncio
async def test_pacer_enforces_min_interval():
    clock = FakeClock()
    sleep = FakeSleep()

    async def fake_sleep(seconds):
        await sleep(seconds)
        clock.now += seconds

    pacer = CommandCodePacer(min_interval_sec=5.0, max_per_minute=10, clock=clock, sleep=fake_sleep)
    assert await pacer.acquire(budget_sec=10) == 0
    assert await pacer.acquire(budget_sec=10) == 5.0
    assert sleep.calls == [5.0]


@pytest.mark.asyncio
async def test_pacer_enforces_per_minute_and_budget():
    clock = FakeClock()

    async def fake_sleep(seconds):
        clock.now += seconds

    pacer = CommandCodePacer(min_interval_sec=0.0, max_per_minute=2, clock=clock, sleep=fake_sleep)
    await pacer.acquire(budget_sec=0)
    await pacer.acquire(budget_sec=0)
    waited = await pacer.acquire(budget_sec=120)
    assert waited == pytest.approx(60.0)

    # 新的窗口里只剩 1 次调用：max_per_minute=1 时第二次必须等待，等不起就失败。
    strict = CommandCodePacer(min_interval_sec=0.0, max_per_minute=1, clock=clock, sleep=fake_sleep)
    await strict.acquire(budget_sec=0)
    with pytest.raises(DeepEtherError) as error:
        await strict.acquire(budget_sec=1)
    assert error.value.code == ErrorCode.PACING_QUOTA

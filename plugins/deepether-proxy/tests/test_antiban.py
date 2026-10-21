"""反封禁节奏控制：间隔/配额/退避/状态落盘与风控信号分类。"""

import asyncio

import pytest

from deepether.antiban import (
    CONSECUTIVE_FAILURE_THRESHOLD,
    BanKind,
    PacingConfig,
    PacingController,
    classify_failure,
)
from deepether.errors import DeepEtherError, ErrorCode


class FakeClock:
    """可手动推进的假时钟（单调/墙上一体，够用且简单）。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class FakeSleeper:
    """假等待：只推进假时钟，不真睡。"""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.clock.now += seconds


def _controller(clock: FakeClock, sleeper: FakeSleeper, **overrides) -> PacingController:
    params = {
        "enabled": True,
        "min_interval_sec": 8.0,
        "max_interval_sec": 20.0,
        "jitter_ratio": 0.25,
        "max_per_minute": 3,
        "max_per_hour": 30,
        "max_per_day": 200,
        "cooldown_on_rate_limit_sec": 900.0,
        "cooldown_on_ban_sec": 21600.0,
        "adaptive_backoff": True,
    }
    params.update(overrides)
    config = PacingConfig(**params)
    return PacingController(config, clock=clock, sleeper=sleeper, rng=lambda: 0.5, wall_clock=clock)


def test_disabled_never_waits() -> None:
    clock, sleeper = FakeClock(), None
    sleeper = FakeSleeper(clock)
    pacing = _controller(clock, sleeper, enabled=False)
    assert asyncio.run(pacing.acquire()) == 0.0
    assert asyncio.run(pacing.acquire()) == 0.0
    assert sleeper.waits == []
    assert pacing.snapshot()["used_last_minute"] == 0


def test_second_call_waits_inside_jitter_window() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    pacing = _controller(clock, sleeper)

    assert asyncio.run(pacing.acquire()) == 0.0
    waited = asyncio.run(pacing.acquire())

    assert 8.0 <= waited <= 20.0 * 1.25
    assert sleeper.waits == [waited]
    assert pacing.snapshot()["used_last_minute"] == 2


def test_insufficient_budget_raises_without_recording() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    pacing = _controller(clock, sleeper)
    asyncio.run(pacing.acquire())

    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(pacing.acquire(budget_ms=1000))

    assert excinfo.value.code == ErrorCode.PACING_COOLDOWN
    assert excinfo.value.retryable is True
    assert sleeper.waits == []
    assert pacing.snapshot()["used_last_minute"] == 1


def test_minute_quota() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    pacing = _controller(clock, sleeper, min_interval_sec=0.0, max_interval_sec=0.0, max_per_minute=2)
    asyncio.run(pacing.acquire())
    asyncio.run(pacing.acquire())
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(pacing.acquire())
    assert excinfo.value.code == ErrorCode.PACING_QUOTA


def test_hour_and_day_quota() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    hourly = _controller(
        clock, sleeper, min_interval_sec=0.0, max_interval_sec=0.0, max_per_minute=100, max_per_hour=2
    )
    asyncio.run(hourly.acquire())
    asyncio.run(hourly.acquire())
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(hourly.acquire())
    assert excinfo.value.code == ErrorCode.PACING_QUOTA

    daily = _controller(
        clock,
        sleeper,
        min_interval_sec=0.0,
        max_interval_sec=0.0,
        max_per_minute=100,
        max_per_hour=100,
        max_per_day=2,
    )
    asyncio.run(daily.acquire())
    asyncio.run(daily.acquire())
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(daily.acquire())
    assert excinfo.value.code == ErrorCode.PACING_QUOTA


def test_quota_window_slides() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    pacing = _controller(clock, sleeper, min_interval_sec=0.0, max_interval_sec=0.0, max_per_minute=2)
    asyncio.run(pacing.acquire())
    asyncio.run(pacing.acquire())
    clock.now += 61.0
    asyncio.run(pacing.acquire())
    assert pacing.snapshot()["used_last_minute"] <= 2


def test_rate_limit_enters_cooldown_and_scales_interval() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    pacing = _controller(clock, sleeper)
    asyncio.run(pacing.acquire())

    pacing.note_failure(BanKind.RATE_LIMITED)
    snapshot = pacing.snapshot()
    assert snapshot["in_cooldown"] is True
    assert snapshot["cooldown_remaining_sec"] == pytest.approx(900.0)
    assert snapshot["interval_scale"] == pytest.approx(2.0)
    assert snapshot["effective_interval_sec"] == pytest.approx((16.0, 40.0))

    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(pacing.acquire(budget_ms=60000))
    assert excinfo.value.code == ErrorCode.PACING_COOLDOWN

    # 冷却结束之后又能调用了；此时距上次调用已远超放大后的间隔，所以不必再等。
    clock.now += 901.0
    assert asyncio.run(pacing.acquire(budget_ms=60000)) == 0.0
    pacing.note_success()
    assert pacing.snapshot()["interval_scale"] == pytest.approx(1.0)


def test_consecutive_failures_escalate_to_long_cooldown() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    pacing = _controller(clock, sleeper)
    for _ in range(CONSECUTIVE_FAILURE_THRESHOLD):
        pacing.note_failure(BanKind.RATE_LIMITED)

    snapshot = pacing.snapshot()
    assert snapshot["consecutive_failures"] == CONSECUTIVE_FAILURE_THRESHOLD
    assert snapshot["cooldown_remaining_sec"] == pytest.approx(1800.0)
    assert "连续" in snapshot["cooldown_reason"]


def test_ban_cooldown_and_recovery() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    pacing = _controller(clock, sleeper)
    pacing.note_failure(BanKind.BANNED)

    snapshot = pacing.snapshot()
    assert snapshot["suspected_ban"] is True
    assert snapshot["cooldown_remaining_sec"] == pytest.approx(21600.0)

    pacing.note_success()
    snapshot = pacing.snapshot()
    assert snapshot["suspected_ban"] is False
    assert snapshot["in_cooldown"] is False


def test_unrelated_failure_is_ignored() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    pacing = _controller(clock, sleeper)
    pacing.note_failure(None)
    assert pacing.snapshot()["in_cooldown"] is False
    assert pacing.snapshot()["interval_scale"] == pytest.approx(1.0)


def test_interval_scale_capped() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    pacing = _controller(clock, sleeper)
    for _ in range(20):
        pacing.note_failure(BanKind.RATE_LIMITED)
    snapshot = pacing.snapshot()
    assert snapshot["interval_scale"] <= 8.0
    assert snapshot["effective_interval_sec"][1] <= 120.0


def test_classify_failure_branches() -> None:
    # 技术性失败绝不能被当成风控。
    assert classify_failure(code=ErrorCode.POW_FAILED, message="PoW 校验未通过") is None
    assert classify_failure(code=ErrorCode.CREDENTIAL_INVALID) is None
    assert classify_failure(code=ErrorCode.CREDENTIAL_MISSING) is None
    assert classify_failure(message="40301 INVALID_POW_RESPONSE") is None
    assert classify_failure(message="INVALID_POW") is None
    assert classify_failure(code=ErrorCode.PROTOCOL_ERROR) is None
    assert classify_failure(code=ErrorCode.INTERNAL) is None
    assert classify_failure(status=502, code=ErrorCode.UPSTREAM_HTTP) is None

    # 封禁文案优先于状态码。
    assert classify_failure(status=403, message="账号已被封禁") == BanKind.BANNED
    assert classify_failure(message="Your account has been suspended") == BanKind.BANNED
    # 配额类。
    assert classify_failure(message="quota exceeded") == BanKind.QUOTA
    assert classify_failure(message="额度已用尽") == BanKind.QUOTA
    # 限流：状态码、文本、空正文。
    assert classify_failure(status=429) == BanKind.RATE_LIMITED
    assert classify_failure(message="网页版接口返回 429，触发风控或频率限制") == BanKind.RATE_LIMITED
    assert classify_failure(status=403, message="forbidden") == BanKind.RATE_LIMITED
    assert classify_failure(code=ErrorCode.UPSTREAM_EMPTY) == BanKind.RATE_LIMITED
    # 无法归类。
    assert classify_failure(status=500) is None
    assert classify_failure(code=ErrorCode.NOT_SUPPORTED) is None
    assert classify_failure(message="连接超时") is None


def test_dump_and_load_roundtrip() -> None:
    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    pacing = _controller(clock, sleeper)
    asyncio.run(pacing.acquire())
    asyncio.run(pacing.acquire())
    pacing.note_failure(BanKind.RATE_LIMITED)

    state = pacing.dump_state()
    assert state["version"] == 1
    assert len(state["recent_calls"]) == 2

    later = FakeClock(clock.now + 30.0)
    restored = _controller(later, FakeSleeper(later))
    restored.load_state(state)

    snapshot = restored.snapshot()
    assert snapshot["in_cooldown"] is True
    assert snapshot["cooldown_remaining_sec"] == pytest.approx(870.0)
    assert snapshot["interval_scale"] == pytest.approx(2.0)
    assert snapshot["used_last_minute"] == 2


def test_load_state_tolerates_garbage() -> None:
    clock = FakeClock()
    restored = _controller(clock, FakeSleeper(clock))
    restored.load_state(  # type: ignore[arg-type]
        {
            "cooldown_remaining_sec": "abc",
            "interval_scale": -3,
            "consecutive_failures": "x",
            "recent_calls": [None, "y", -5.0, clock.now - 10.0, clock.now + 10_000],
            "suspected_ban": "yes",
        }
    )
    snapshot = restored.snapshot()
    assert snapshot["in_cooldown"] is False
    assert snapshot["interval_scale"] == pytest.approx(1.0)


def test_concurrent_acquire_is_serialized() -> None:
    clock = FakeClock()
    in_flight = {"now": 0, "max": 0}
    waits: list[float] = []

    async def sleeper(seconds: float) -> None:
        waits.append(seconds)
        clock.now += seconds

    pacing = _controller(clock, FakeSleeper(clock), min_interval_sec=1.0, max_interval_sec=1.0, jitter_ratio=0.0)
    pacing._sleeper = sleeper  # type: ignore[attr-defined]

    async def one() -> float:
        got = await pacing.acquire()
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        in_flight["now"] -= 1
        return got

    async def main() -> list[float]:
        return await asyncio.gather(one(), one(), one())

    results = asyncio.run(main())
    assert in_flight["max"] == 1
    # 第一次不等待，后两次各至少等 1 秒（串行化的直接证据）。
    assert results[0] == 0.0
    assert results[1] >= 1.0
    assert results[2] >= 1.0
    assert len(waits) == 2

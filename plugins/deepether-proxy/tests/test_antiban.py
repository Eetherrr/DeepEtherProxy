"""反封禁节奏控制：间隔/配额/退避/状态落盘与风控信号分类。"""

import asyncio

import pytest

from deepether.antiban import (
    CONSECUTIVE_FAILURE_THRESHOLD,
    BanKind,
    PacingConfig,
    PacingController,
    classify_failure,
    parse_model_limits,
    parse_quiet_hours,
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
    # 固定到中午：夜间节律默认 01:00-07:00 生效，不注入时间会让这些用例在凌晨跑必红。
    return PacingController(
        config,
        clock=clock,
        sleeper=sleeper,
        rng=lambda: 0.5,
        wall_clock=clock,
        minute_provider=lambda: 12 * 60,
    )


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

    # 冷却期间另一个在途请求的成功，不能证明账号已经恢复（max_concurrency=2 时会遇到）。
    pacing.note_success()
    snapshot = pacing.snapshot()
    assert snapshot["suspected_ban"] is True
    assert snapshot["in_cooldown"] is True

    # 冷却结束后重新被放行并成功，才算恢复。
    clock.now += 21601.0
    asyncio.run(pacing.acquire())
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
    assert state["version"] == 2
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

# ─── 容量相关（0.3.2）：按模型配额 / 保留额度 / 夜间节律 / 自适应预算 ─────────


def _plain_config(**overrides) -> PacingConfig:
    """构造一份"没有节奏干扰"的配置，只测配额逻辑。"""

    params = {
        "enabled": True,
        "min_interval_sec": 0.0,
        "max_interval_sec": 0.0,
        "jitter_ratio": 0.0,
        "max_per_minute": 1000,
        "max_per_hour": 1000,
        "max_per_day": 1000,
        "cooldown_on_rate_limit_sec": 900.0,
        "cooldown_on_ban_sec": 21600.0,
        "adaptive_backoff": True,
        "adaptive_budget": False,
        "quiet_hours": "",
        "reserve_daily_budget": 0,
    }
    params.update(overrides)
    return PacingConfig(**params)


def _plain_controller(config: PacingConfig, **kwargs) -> PacingController:
    clock = FakeClock()
    return PacingController(
        config,
        clock=clock,
        sleeper=FakeSleeper(clock),
        rng=lambda: 0.5,
        wall_clock=clock,
        **kwargs,
    )


def test_per_model_daily_limit_blocks_only_that_model() -> None:
    """后台 utils 单独限流：打满后只拒绝 utils，replyer 照常可用。"""

    config = _plain_config(max_per_day=100, model_daily_limits={"deepether-utils": 2})
    pacing = _plain_controller(config)

    asyncio.run(pacing.acquire(model_name="deepether-utils"))
    asyncio.run(pacing.acquire(model_name="deepether-utils"))
    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(pacing.acquire(model_name="deepether-utils"))
    assert excinfo.value.code == ErrorCode.PACING_QUOTA
    assert "deepether-utils" in excinfo.value.message

    # 另一个模型不受影响
    asyncio.run(pacing.acquire(model_name="deepether-replyer"))


def test_reserve_budget_protects_priority_model() -> None:
    """日预算最后 N 次留给 replyer：utils 提前被拒，回复始终有额度。"""

    config = _plain_config(
        max_per_day=10,
        reserve_daily_budget=4,
        reserve_models=("deepether-replyer",),
    )
    pacing = _plain_controller(config)

    for _ in range(6):
        asyncio.run(pacing.acquire(model_name="deepether-utils"))

    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(pacing.acquire(model_name="deepether-utils"))
    assert excinfo.value.code == ErrorCode.PACING_QUOTA
    assert "保留" in excinfo.value.message

    # 保留额度对 replyer 有效
    asyncio.run(pacing.acquire(model_name="deepether-replyer"))


def test_unknown_model_name_is_not_blocked_by_reserve() -> None:
    """拿不到模型名时不能因为保留额度把所有请求都挡掉。"""

    config = _plain_config(max_per_day=10, reserve_daily_budget=9, reserve_models=("deepether-replyer",))
    pacing = _plain_controller(config)
    for _ in range(10):
        asyncio.run(pacing.acquire())


def test_quiet_hours_scale_minute_and_hour_limits() -> None:
    """夜间把分/时配额压低（不是完全禁止），白天恢复原值。"""

    config = _plain_config(
        max_per_minute=4,
        max_per_hour=40,
        quiet_hours="01:00-07:00",
        quiet_hours_ratio=0.25,
    )
    night = _plain_controller(config, minute_provider=lambda: 3 * 60)
    snapshot = night.snapshot()
    assert snapshot["quiet_active"] is True
    assert snapshot["limits"]["per_minute"] == 1
    assert snapshot["limits"]["per_hour"] == 10

    day = _plain_controller(config, minute_provider=lambda: 12 * 60)
    snapshot = day.snapshot()
    assert snapshot["quiet_active"] is False
    assert snapshot["limits"]["per_hour"] == 40


def test_quiet_hours_support_wraparound() -> None:
    assert parse_quiet_hours("23:00-06:00") == (23 * 60, 6 * 60)
    assert parse_quiet_hours("01:00-07:00") == (60, 7 * 60)
    assert parse_quiet_hours("bad") is None
    assert parse_quiet_hours("") is None

    config = _plain_config(quiet_hours="23:00-06:00", quiet_hours_ratio=0.5, max_per_hour=40)
    late = _plain_controller(config, minute_provider=lambda: 23 * 60)
    assert late.snapshot()["quiet_active"] is True
    early = _plain_controller(config, minute_provider=lambda: 5 * 60)
    assert early.snapshot()["quiet_active"] is True
    noon = _plain_controller(config, minute_provider=lambda: 12 * 60)
    assert noon.snapshot()["quiet_active"] is False


def test_adaptive_budget_decays_and_recovers() -> None:
    """AIMD：命中风控回撤，连续成功回补，且有下限。"""

    config = PacingConfig(
        enabled=True,
        min_interval_sec=0.0,
        max_interval_sec=0.0,
        jitter_ratio=0.0,
        max_per_minute=1000,
        max_per_hour=1000,
        max_per_day=400,
        adaptive_budget=True,
        min_adaptive_budget=200,
        quiet_hours="",
        reserve_daily_budget=0,
    )
    pacing = _plain_controller(config)
    assert pacing.snapshot()["limits"]["per_day"] == 400

    pacing.note_failure(BanKind.RATE_LIMITED)
    snapshot = pacing.snapshot()
    assert snapshot["budget_scale"] == pytest.approx(0.75)
    assert snapshot["limits"]["per_day"] == 300

    for _ in range(50):
        pacing.note_success()
    assert pacing.snapshot()["budget_scale"] == pytest.approx(0.85)
    assert pacing.snapshot()["limits"]["per_day"] == 340

    # 一直撞风控也不会掉到 0
    for _ in range(30):
        pacing.note_failure(BanKind.RATE_LIMITED)
    assert pacing.snapshot()["limits"]["per_day"] >= 200


def test_parse_model_limits_tolerates_junk() -> None:
    assert parse_model_limits("deepether-utils=180,deepether-replyer=300") == {
        "deepether-utils": 180,
        "deepether-replyer": 300,
    }
    assert parse_model_limits("a=3; b = 4") == {"a": 3, "b": 4}
    assert parse_model_limits("bad,c=x,,=5") == {}
    assert parse_model_limits("") == {}


def test_dump_state_carries_budget_and_model_names() -> None:
    clock = FakeClock()
    pacing = _plain_controller(_plain_config(adaptive_budget=True, max_per_day=400))
    asyncio.run(pacing.acquire(model_name="deepether-utils"))
    pacing.note_failure(BanKind.RATE_LIMITED)

    state = pacing.dump_state()
    assert state["version"] == 2
    assert state["budget_scale"] == pytest.approx(0.75)
    assert state["recent_calls"][0]["model"] == "deepether-utils"

    later = FakeClock(clock.now + 30.0)
    restored = PacingController(
        _plain_config(adaptive_budget=True, max_per_day=400),
        clock=later,
        sleeper=FakeSleeper(later),
        rng=lambda: 0.5,
        wall_clock=later,
    )
    restored.load_state(state)
    snapshot = restored.snapshot()
    assert snapshot["budget_scale"] == pytest.approx(0.75)
    assert snapshot["used_by_model"] == {"deepether-utils": 1}

def test_minute_quota_waits_for_priority_model() -> None:
    """分/时配额一时打满时，replyer 可以短暂等待，而不是立刻失败（上游速率不变）。"""

    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    config = _plain_config(
        max_per_minute=2,
        quota_wait_max_sec=25.0,
        reserve_models=("deepether-replyer",),
    )
    pacing = PacingController(config, clock=clock, sleeper=sleeper, rng=lambda: 0.5, wall_clock=clock)

    asyncio.run(pacing.acquire(model_name="deepether-replyer"))   # t=0
    clock.now += 55.0
    asyncio.run(pacing.acquire(model_name="deepether-replyer"))   # t=55
    clock.now += 3.0                                              # t=58：分钟窗内有 2 次

    waited = asyncio.run(pacing.acquire(model_name="deepether-replyer"))

    # 最早一条在 t=0，60 秒后滑出窗口 → 约等 2 秒，而不是直接失败
    assert waited == pytest.approx(2.05, abs=0.2)
    assert sleeper.waits, "应当真的等待过"
    # 等到 t=0 那一条滑出窗口后才放行，所以窗口里仍然是 2 条
    assert pacing.snapshot()["used_last_minute"] == 2


def test_minute_quota_does_not_wait_for_background_model() -> None:
    """后台 utils 不值得等：等下去只会拖慢回复路径，直接失败交给官方模型。"""

    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    config = _plain_config(
        max_per_minute=2,
        quota_wait_max_sec=25.0,
        reserve_models=("deepether-replyer",),
    )
    pacing = PacingController(config, clock=clock, sleeper=sleeper, rng=lambda: 0.5, wall_clock=clock)

    asyncio.run(pacing.acquire(model_name="deepether-utils"))
    clock.now += 1.0
    asyncio.run(pacing.acquire(model_name="deepether-utils"))
    clock.now += 1.0

    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(pacing.acquire(model_name="deepether-utils"))
    assert excinfo.value.code == ErrorCode.PACING_QUOTA
    assert sleeper.waits == []


def test_hard_blocked_quota_does_not_sleep_first() -> None:
    """日额度已经用完时不能先睡 8~20 秒再报错（评审 P1：白慢一次）。"""

    clock = FakeClock()
    sleeper = FakeSleeper(clock)
    config = _plain_config(
        min_interval_sec=10.0,
        max_interval_sec=10.0,
        jitter_ratio=0.0,
        max_per_day=1,
        reserve_daily_budget=0,
    )
    pacing = PacingController(config, clock=clock, sleeper=sleeper, rng=lambda: 0.5, wall_clock=clock)

    asyncio.run(pacing.acquire())
    sleeper.waits.clear()

    with pytest.raises(DeepEtherError) as excinfo:
        asyncio.run(pacing.acquire())
    assert excinfo.value.code == ErrorCode.PACING_QUOTA
    assert sleeper.waits == [], "注定失败的请求不应该先等待"


def test_quiet_hours_minute_precision() -> None:
    """夜间区间必须按分钟判断：只用整点会把 23:30-06:30 判错（评审 P0）。"""

    config = _plain_config(quiet_hours="23:30-06:30", quiet_hours_ratio=0.5, max_per_hour=40)

    def active_at(minutes: int) -> bool:
        controller = _plain_controller(config, minute_provider=lambda: minutes)
        return bool(controller.snapshot()["quiet_active"])

    assert active_at(23 * 60 + 45) is True    # 23:45 在区间内
    assert active_at(6 * 60 + 15) is True     # 06:15 仍在区间内
    assert active_at(6 * 60 + 45) is False    # 06:45 已出区间
    assert active_at(23 * 60 + 15) is False   # 23:15 还没进区间


def test_load_state_accepts_v1_timestamp_list() -> None:
    """旧版落盘（纯时间戳列表）必须能读，升级不能丢配额历史。"""

    clock = FakeClock(1000.0)
    pacing = PacingController(
        _plain_config(max_per_day=100),
        clock=clock,
        sleeper=FakeSleeper(clock),
        rng=lambda: 0.5,
        wall_clock=clock,
    )
    pacing.load_state({"version": 1, "recent_calls": [1000.0 - 10.0, 1000.0 - 20.0]})

    snapshot = pacing.snapshot()
    assert snapshot["used_last_day"] == 2
    assert snapshot["used_by_model"] == {"unknown": 2}

def test_classify_uses_http_status_for_soft_403() -> None:
    """403 + 非技术错误码 → 限流。旧版这条分支是死代码（web_client 先映射成了凭证失效）。"""

    assert classify_failure(status=403, code=ErrorCode.UPSTREAM_HTTP, message="Forbidden") == BanKind.RATE_LIMITED
    # 401 仍然算技术性失败（凭证过期，重试没意义，也不该冷却）
    assert classify_failure(status=401, code=ErrorCode.CREDENTIAL_INVALID) is None


def test_classify_recognizes_business_rate_limit_text() -> None:
    """HTTP 200 + code≠0 的"太频繁/稍后重试"必须能触发退避，而不是被当成协议漂移。"""

    assert classify_failure(code=ErrorCode.UPSTREAM_HTTP, message="请求过于频繁，请稍后重试") == BanKind.RATE_LIMITED
    assert classify_failure(message="Too many requests, slow down") == BanKind.RATE_LIMITED

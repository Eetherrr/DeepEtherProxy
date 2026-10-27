"""反封禁节奏控制（pacing / quota / risk signals）。

封禁绝大多数由**行为特征**触发，而不是单次请求内容（详见 docs/design/ANTIBAN.md）。
本模块负责"行为层"里最容易做对也最容易被检测的那一层：

1. 同一个账号绝不并发：acquire() 用 asyncio.Lock 串行化，后来的请求排队等待；
2. 间隔带随机抖动：固定间隔是最容易被周期性检测的特征；
3. 三级配额（分/时/日），日级配额可以按模型再细分；
4. 风控信号驱动退避：429 冷却 15 分钟、疑似封号冷却 6 小时，且间隔区间自动翻倍（有上限）；
5. 自适应日预算：命中限流后整体预算回撤，长时间无信号再缓慢爬回（AIMD）；
6. 夜间节律：深夜把分/时配额压低，避免"24 小时匀速不停"这种最像机器的形态；
7. 额度保留：日预算的最后一截留给 replyer，避免后台 utils 把额度吃光、导致用户等待的回复没额度。

上层（provider.py）只需要四句话：调用前 await acquire(budget_ms=..., model_name=...)，
成功 note_success()，失败把 classify_failure(...) 的结果交给 note_failure(kind)，再 dump_state() 落盘。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Final

import asyncio
import random
import time

from .errors import DeepEtherError, ErrorCode

#: 间隔翻倍的最大值（秒）：再慢下去的等待就该让上层直接失败，而不是把请求挂死。
MAX_BACKOFF_INTERVAL_SEC: Final[float] = 120.0

#: 间隔区间最多放大到基线的多少倍（8 倍 × 基线 20s = 160s，已被上一条兜住）。
MAX_INTERVAL_SCALE: Final[float] = 8.0

#: 连续失败多少次后按"限流"处理（一次偶发超时不该触发 15 分钟冷却）。
CONSECUTIVE_FAILURE_THRESHOLD: Final[int] = 3

#: 软风控（429/限额）命中一次后的回撤比例。0.75 而不是 0.6：
#: 单次 429 往往只是瞬时打得太快，一次就砍到 60% 会让用户立刻觉得"不如以前"。
BUDGET_DECAY_SOFT: Final[float] = 0.75

#: 硬风控（疑似封号）命中一次后的回撤比例；封号是账号级事故，必须显著收缩。
BUDGET_DECAY_HARD: Final[float] = 0.5

#: 日预算回撤的下限比例（相对 max_per_day）；再低就等于关掉网页版通道。
BUDGET_MIN_SCALE: Final[float] = 0.1

#: 连续成功多少次，日预算回补一档（AIMD 的加性增）。
#: 50 次 ≈ 17 条回复；从 0.75 回满需要约 50 条回复，不至于要跨越好几天。
BUDGET_RECOVER_EVERY: Final[int] = 50

#: 每回补一档的增量比例。
BUDGET_RECOVER_STEP: Final[float] = 0.1


class BanKind:
    """风控信号类别。

    这些字符串会出现在日志与状态命令里，保持稳定便于检索。
    """

    RATE_LIMITED: Final[str] = "rate_limited"
    BANNED: Final[str] = "banned"
    QUOTA: Final[str] = "quota"


#: 明确属于"账号级封禁/风控"的文案标记（小写比较）。
_BAN_MARKERS: Final[tuple[str, ...]] = (
    "banned",
    "封禁",
    "封号",
    "风控",
    "violat",
    "abuse",
    "suspend",
)

#: 明确属于"额度用尽"的文案标记（小写比较）。DeepSeek 网页版目前没有这条，
#: 但上游可能上线，提前把语义分开，避免额度问题被当成封号处理。
_QUOTA_MARKERS: Final[tuple[str, ...]] = ("quota", "额度", "usage limit", "配额")

#: 业务级"太频繁/稍后重试"文案：上游常用 HTTP 200 + code≠0 表达限流，
#: 这类响应既不是协议漂移也不该当成封号，必须能触发退避。
_RATE_MARKERS: Final[tuple[str, ...]] = (
    "频繁",
    "frequency",
    "too many",
    "slow down",
    "稍后重试",
    "rate limit",
    "请稍候",
)


def classify_failure(
    *,
    status: int | None = None,
    code: Any = None,
    message: str = "",
    body: str = "",
) -> str | None:
    """把一次失败归类成风控信号。

    这个函数是整套退避逻辑的入口判定，判错的代价很高：
    把 PoW 过期当成风控 → 自己把账号锁 6 小时；把封号当成普通错误 → 继续撞墙加长封禁。

    Args:
        status: HTTP 状态码；没有 HTTP 响应（例如超时）时传 None。
        code: 业务错误码或本插件错误码（ErrorCode 的取值）。
        message: 错误说明文本。
        body: 上游响应体裁剪片段。

    Returns:
        str | None: 命中时返回 BanKind 之一，否则返回 None（表示不是风控信号）。
    """

    text = f"{message} {body}".lower()
    code_text = "" if code is None else str(code).strip().lower()

    # 1) 技术性失败：PoW 过期/算法升级、凭证缺失或过期、服务端 5xx。
    #    这些重试是合理的，绝不能被当作"上游在风控我们"。
    if code_text in (ErrorCode.POW_FAILED, ErrorCode.CREDENTIAL_INVALID, ErrorCode.CREDENTIAL_MISSING):
        return None
    if "40301" in text or "invalid_pow" in text:
        return None
    if code_text in (ErrorCode.PROTOCOL_ERROR, ErrorCode.NOT_SUPPORTED, ErrorCode.INTERNAL):
        return None
    if status is not None and status >= 500:
        return None

    # 2) 限流状态码最先判定：限流响应体里常带"风控/频率"字样，
    #    若先匹配封禁文案，我们自己写的 429 说明会被误判成封号 6 小时。
    if status == 429 or "429" in text:
        return BanKind.RATE_LIMITED

    # 3) 明确的封禁/风控文案优先于其余状态码：403 + "封禁" 与 403 + "token 失效" 不是一回事。
    if any(marker in text for marker in _BAN_MARKERS):
        return BanKind.BANNED

    # 4) 配额/额度类。
    if any(marker in text for marker in _QUOTA_MARKERS):
        return BanKind.QUOTA

    # 5) 403 但没有封禁文案：保守按限流处理（宁可冷却 15 分钟，也不要误判成封号 6 小时）。
    if status == 403:
        return BanKind.RATE_LIMITED

    # 6) 业务级限流文案（HTTP 200 + code≠0 的"请求过于频繁/稍后重试"）。
    if any(marker in text for marker in _RATE_MARKERS):
        return BanKind.RATE_LIMITED

    # 7) 正文为空 / 只有思考：软拦截，DeepSeek 侧常见表现。
    if code_text == ErrorCode.UPSTREAM_EMPTY:
        return BanKind.RATE_LIMITED

    return None


def parse_model_limits(spec: str) -> dict[str, int]:
    """把 "模型=次数,模型=次数" 解析成字典。

    配置里用字符串而不是嵌套表，是为了让 WebUI 能直接编辑（嵌套表在配置表单里体验很差）。
    解析失败的部分直接忽略：宁可少一条限制，也不能让插件起不来。

    Args:
        spec: 形如 "deepether-utils=180,deepether-replyer=300" 的字符串。

    Returns:
        dict[str, int]: 模型名 → 每日上限；解析不出时为空字典。
    """

    result: dict[str, int] = {}
    for chunk in str(spec or "").replace(";", ",").split(","):
        item = chunk.strip()
        if not item or "=" not in item:
            continue
        name, _, raw_limit = item.partition("=")
        name = name.strip()
        try:
            limit = int(float(raw_limit.strip()))
        except ValueError:
            continue
        if name and limit > 0:
            result[name] = limit
    return result


def parse_quiet_hours(spec: str) -> tuple[int, int] | None:
    """把 "01:00-07:00" 解析成距离零点的分钟数区间。

    支持跨零点（例如 "23:00-06:00"）。

    Args:
        spec: 时间区间字符串；空串表示不启用夜间节律。

    Returns:
        tuple[int, int] | None: (起始分钟, 结束分钟)；解析失败返回 None。
    """

    text = str(spec or "").strip()
    if not text or "-" not in text:
        return None
    start_text, _, end_text = text.partition("-")
    start = _parse_hhmm(start_text)
    end = _parse_hhmm(end_text)
    if start is None or end is None or start == end:
        return None
    return start, end


def _parse_hhmm(text: str) -> int | None:
    """把 "07:30" / "07" 解析成分钟数；非法返回 None。"""

    hours_text, _, minutes_text = text.strip().partition(":")
    try:
        hours = int(hours_text)
        minutes = int(minutes_text or 0)
    except ValueError:
        return None
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return None
    return hours * 60 + minutes


@dataclass(frozen=True)
class PacingConfig:
    """节奏与配额配置。

    Attributes:
        enabled: 是否启用节奏控制；关闭后 PacingController 直接放行（只在排障时关）。
        min_interval_sec: 两次上游请求之间的最小间隔（秒）。
        max_interval_sec: 抖动上界（秒）；实际间隔在 [min, max] 内随机。
        jitter_ratio: 抖动比例，0 表示不加抖动。
        max_per_minute: 每分钟请求上限（夜间按 quiet_hours_ratio 缩放）。
        max_per_hour: 每小时请求上限（夜间按 quiet_hours_ratio 缩放）。
        max_per_day: 每天请求上限；实际生效值是它与自适应预算的较小者。
        cooldown_on_rate_limit_sec: 命中限流后的冷却时间（秒）。
        cooldown_on_ban_sec: 疑似封号后的冷却时间（秒）。
        adaptive_backoff: 是否让冷却后的间隔自动翻倍（连续命中时逐步退避）。
        adaptive_budget: 是否启用 AIMD 日预算（命中风控回撤、长期无信号回补）。
        min_adaptive_budget: AIMD 回撤后的绝对下限（次/天）；默认 200，不能比旧版硬配额更小。
        quota_wait_max_sec: 分/时配额一时打满时，高优先级模型最多等多少秒（0 表示不等待，直接失败）。
        quiet_hours: 夜间节律区间（HH:MM-HH:MM，支持跨零点）；空串表示不启用。
        quiet_hours_ratio: 夜间把分/时配额乘以这个比例（1.0 表示不降）。
        model_daily_limits: 按模型名的每日上限（例如后台 utils 单独限流）。
        reserve_daily_budget: 日预算里留给高优先级模型的次数。
        reserve_models: 可动用保留额度的模型名。
    """

    enabled: bool = True
    min_interval_sec: float = 8.0
    max_interval_sec: float = 20.0
    jitter_ratio: float = 0.25
    max_per_minute: int = 3
    max_per_hour: int = 45
    max_per_day: int = 400
    cooldown_on_rate_limit_sec: float = 900.0
    cooldown_on_ban_sec: float = 21600.0
    adaptive_backoff: bool = True
    adaptive_budget: bool = True
    min_adaptive_budget: int = 200
    quota_wait_max_sec: float = 25.0
    quiet_hours: str = "01:00-07:00"
    quiet_hours_ratio: float = 0.25
    model_daily_limits: dict[str, int] = field(default_factory=dict)
    reserve_daily_budget: int = 60
    reserve_models: tuple[str, ...] = ("deepether-replyer",)


class PacingController:
    """串行化 + 抖动间隔 + 三级配额 + 风控退避 + 自适应预算。

    一个账号一个实例。acquire() 协程安全；note_* / snapshot / dump_state / load_state
    都是纯内存操作，不需要额外加锁。
    """

    def __init__(
        self,
        config: PacingConfig,
        *,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], Any] | None = None,
        rng: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        minute_provider: Callable[[], int] | None = None,
    ) -> None:
        """初始化节奏控制器。

        Args:
            config: 节奏与配额配置。
            clock: 单调时钟（默认 time.monotonic）；测试可注入假时钟。
            sleeper: 等待函数（默认 asyncio.sleep）；测试可注入记录器，避免真等待。
            rng: 返回 [0, 1) 的随机源（默认 random.random）。
            wall_clock: 墙上时钟（默认 time.time），只用于状态落盘的时间戳换算。
            minute_provider: 当前本地时间距零点的分钟数（0~1439，默认取系统本地时间）；
                测试可注入固定值。注意必须到分钟：只用小时会把 23:30-06:30 这类区间判错。
        """

        self._config = config
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or asyncio.sleep
        self._rng = rng or random.random
        self._wall_clock = wall_clock or time.time
        self._minute_provider = minute_provider or (
            lambda: datetime.now().hour * 60 + datetime.now().minute
        )

        self._lock = asyncio.Lock()
        #: 调用记录：(单调时钟时间戳, 模型名)；模型名用于按模型统计与保留额度。
        self._calls: list[tuple[float, str]] = []
        self._cooldown_until: float = 0.0
        self._cooldown_reason: str = ""
        self._suspected_ban: bool = False
        self._consecutive_failures: int = 0
        self._interval_scale: float = 1.0
        #: AIMD 日预算比例（1.0 = 用满 max_per_day）。
        self._budget_scale: float = 1.0
        self._success_streak: int = 0
        #: 最近一次被放行（登记）的时刻，用于判断成功能否清掉冷却。
        self._last_admitted_at: float = 0.0
        #: 最近一次进入冷却的时刻。
        self._cooldown_since: float = 0.0
        self._quiet_range = parse_quiet_hours(config.quiet_hours)

    # ─── 对外：取用与回报 ───────────────────────────────────────────────

    async def acquire(self, *, budget_ms: int | None = None, model_name: str = "") -> float:
        """取得一次上游请求许可（必要时等待）。

        Args:
            budget_ms: 上层剩余预算（毫秒）。预计等待超过预算时立刻抛错，
                让上层直接失败，而不是把 Host 请求挂到超时。
            model_name: 本次调用使用的模型名（用于按模型配额与保留额度）。

        Returns:
            float: 实际等待的秒数（未等待返回 0.0）。

        Raises:
            DeepEtherError: 冷却中或配额耗尽，code 为 PACING_COOLDOWN / PACING_QUOTA。
        """

        if not self._config.enabled:
            return 0.0

        async with self._lock:
            waited = 0.0
            # 预检：日额度/按模型/保留额度已经耗尽时，先睡 8~20 秒再报错纯属浪费——
            # 这会让用户等待路径上的 replyer 白慢一次，且日志里看不出是"额度没了"。
            hard_block = self._hard_block_error(self._clock(), model_name)
            if hard_block is not None:
                raise hard_block

            wait = self._required_wait()
            if wait > 0:
                if budget_ms is not None and wait * 1000.0 > max(0, int(budget_ms)):
                    raise DeepEtherError(
                        ErrorCode.PACING_COOLDOWN,
                        self._cooldown_message(wait),
                        retryable=True,
                        detail=f"需要等待 {wait:.0f}s，剩余预算 {int(budget_ms)}ms",
                    )
                await self._sleeper(wait)
                waited = wait

            # 分/时配额打满时，高优先级模型可以"有界等待"到窗口滑出，而不是立刻失败。
            # 上游看到的请求速率完全不变（仍然串行 + 抖动），只是不再把突发整段推给付费模型。
            for _ in range(2):
                now = self._clock()
                wait_more = self._window_wait(now, model_name)
                if wait_more <= 0:
                    break
                if budget_ms is not None:
                    budget_left = max(0, int(budget_ms)) - int(waited * 1000.0)
                    if wait_more * 1000.0 > budget_left:
                        self._raise_window_quota(now)
                await self._sleeper(wait_more)
                waited += wait_more

            now = self._clock()
            self._check_quota(now, model_name)
            self._calls.append((now, model_name or ""))
            self._last_admitted_at = now
            self._trim(now)
            return waited

    def note_success(self) -> None:
        """记录一次成功：清空连续失败计数，间隔缓慢收敛回基线，日预算缓慢回补。"""

        self._consecutive_failures = 0
        # 只有"在冷却生效之后才被放行"的成功，才能证明冷却已经过期。
        # max_concurrency=2 时两个请求可能同时在途，旧逻辑会被另一个请求的成功误清冷却——
        # 表现就是"刚被限流，下一秒又满速打"。
        if self._last_admitted_at >= self._cooldown_since:
            self._cooldown_until = 0.0
            self._cooldown_reason = ""
        # 疑似封禁更保守：要等冷却真正结束后还被放行一次，才认为账号恢复正常。
        if self._last_admitted_at >= self._cooldown_until:
            self._suspected_ban = False
        if self._interval_scale > 1.0:
            # 成功一次就把之前的翻倍折半，避免一次风控之后永远慢下去。
            self._interval_scale = max(1.0, self._interval_scale / 2.0)
        if not self._config.adaptive_budget:
            return
        # AIMD 的加性增：连续成功攒够一档才回补，避免一次成功就急着提速。
        self._success_streak += 1
        if self._success_streak >= BUDGET_RECOVER_EVERY:
            self._success_streak = 0
            self._budget_scale = min(1.0, self._budget_scale + BUDGET_RECOVER_STEP)

    def note_failure(self, kind: str | None) -> None:
        """记录一次失败并施加对应退避。

        Args:
            kind: BanKind 的取值；None 表示与风控无关的失败（只记录，不冷却）。
        """

        if kind is None:
            return
        now = self._clock()
        if self._config.adaptive_budget:
            # AIMD 的乘性减：软风控小幅回撤（0.75），封禁级信号才腰斩（0.5）。
            decay = BUDGET_DECAY_HARD if kind == BanKind.BANNED else BUDGET_DECAY_SOFT
            self._budget_scale = max(BUDGET_MIN_SCALE, self._budget_scale * decay)
            self._success_streak = 0
        if kind == BanKind.BANNED:
            self._suspected_ban = True
            self._cooldown_reason = "疑似账号被风控封禁"
            self._enter_cooldown(now, self._config.cooldown_on_ban_sec)
            self._scale_up()
            return
        if kind == BanKind.QUOTA:
            self._cooldown_reason = "上游额度用尽"
            self._enter_cooldown(now, self._config.cooldown_on_rate_limit_sec)
            return
        # rate_limited：冷却 + 抖动区间翻倍；连续多次则升级成长冷却。
        self._consecutive_failures += 1
        cooldown = self._config.cooldown_on_rate_limit_sec
        if self._config.adaptive_backoff and self._consecutive_failures >= CONSECUTIVE_FAILURE_THRESHOLD:
            self._cooldown_reason = f"连续 {self._consecutive_failures} 次限流，进入长冷却"
            cooldown = min(cooldown * 2.0, self._config.cooldown_on_ban_sec)
        else:
            self._cooldown_reason = "上游返回限流信号"
        self._enter_cooldown(now, cooldown)
        self._scale_up()

    # ─── 对外：观测与持久化 ─────────────────────────────────────────────

    def snapshot(self) -> dict[str, Any]:
        """返回当前节奏状态（供 /deepether-status 展示）。"""

        now = self._clock()
        factor = self._quiet_factor()
        daily_limit = self._effective_daily_limit()
        return {
            "enabled": self._config.enabled,
            "in_cooldown": now < self._cooldown_until,
            "cooldown_remaining_sec": max(0.0, round(self._cooldown_until - now, 1)),
            "cooldown_reason": self._cooldown_reason,
            "suspected_ban": self._suspected_ban,
            "interval_scale": round(self._interval_scale, 2),
            "effective_interval_sec": (
                round(self._config.min_interval_sec * self._interval_scale, 1),
                round(min(self._config.max_interval_sec * self._interval_scale, MAX_BACKOFF_INTERVAL_SEC), 1),
            ),
            "consecutive_failures": self._consecutive_failures,
            "used_last_minute": self._count_within(now, 60.0),
            "used_last_hour": self._count_within(now, 3600.0),
            "used_last_day": self._count_within(now, 86400.0),
            "limits": {
                "per_minute": self._scaled_limit(self._config.max_per_minute, factor),
                "per_hour": self._scaled_limit(self._config.max_per_hour, factor),
                "per_day": daily_limit,
            },
            "budget_scale": round(self._budget_scale, 2),
            "success_streak": self._success_streak,
            "quiet_active": factor < 1.0,
            "quiet_hours": self._config.quiet_hours,
            "quiet_ratio": round(factor, 2),
            "used_by_model": self._used_by_model(now),
            "model_daily_limits": dict(self._config.model_daily_limits),
            "reserve_daily_budget": self._config.reserve_daily_budget,
            "reserve_models": list(self._config.reserve_models),
        }

    def dump_state(self) -> dict[str, Any]:
        """导出可落盘的节奏状态。

        持久化的意义：重启插件不该等于配额清零——否则重启刷新额度本身就是
        一个可被观测到的异常模式。自适应预算与成功连击也一起落盘，重启后不重置。
        """

        now = self._clock()
        wall_now = self._wall_clock()
        return {
            "version": 2,
            "cooldown_until_wall": wall_now + max(0.0, self._cooldown_until - now),
            "cooldown_remaining_sec": max(0.0, self._cooldown_until - now),
            "cooldown_reason": self._cooldown_reason,
            "suspected_ban": self._suspected_ban,
            "consecutive_failures": self._consecutive_failures,
            "interval_scale": self._interval_scale,
            "budget_scale": self._budget_scale,
            "success_streak": self._success_streak,
            # 用墙上时钟保存调用时间点，重启后按同一墙上时钟换算回单调时钟。
            "recent_calls": [
                {"wall": wall_now - (now - timestamp), "model": model} for timestamp, model in self._calls
            ],
        }

    def load_state(self, data: dict[str, Any]) -> None:
        """载入 dump_state 导出的状态。

        数据不可信（可能被手工改坏、也可能是旧版本），任何字段异常都按忽略该字段处理：
        宁可丢失配额历史，也不能让插件启动失败。

        Args:
            data: dump_state() 的返回值或其近似结构（兼容 version 1 的纯时间戳列表）。
        """

        if not isinstance(data, dict):
            return
        now = self._clock()
        wall_now = self._wall_clock()

        # 优先用墙上时钟的绝对结束时刻：重启时如果已经过去了 10 分钟，
        # 就只补 5 分钟，而不是把 15 分钟冷却重新算一遍（否则重启会变成加刑）。
        until_wall = _as_float(data.get("cooldown_until_wall"))
        remaining = until_wall - wall_now if until_wall is not None else _as_float(data.get("cooldown_remaining_sec"))
        if remaining is not None and remaining > 0:
            self._cooldown_until = now + min(remaining, self._config.cooldown_on_ban_sec)
            self._cooldown_reason = str(data.get("cooldown_reason") or "冷却中（来自上次运行）")
        scale = _as_float(data.get("interval_scale"))
        if scale is not None and scale >= 1.0:
            self._interval_scale = min(scale, MAX_INTERVAL_SCALE)
        budget_scale = _as_float(data.get("budget_scale"))
        if budget_scale is not None and 0.0 < budget_scale <= 1.0:
            self._budget_scale = budget_scale
        streak = _as_int(data.get("success_streak"))
        if streak is not None and streak >= 0:
            self._success_streak = streak
        failures = _as_int(data.get("consecutive_failures"))
        if failures is not None and failures >= 0:
            self._consecutive_failures = failures
        self._suspected_ban = bool(data.get("suspected_ban"))

        calls = data.get("recent_calls")
        if isinstance(calls, list):
            restored: list[tuple[float, str]] = []
            for item in calls:
                if isinstance(item, dict):
                    wall_ts = _as_float(item.get("wall"))
                    model = str(item.get("model") or "")
                else:
                    wall_ts = _as_float(item)
                    model = ""
                if wall_ts is None:
                    continue
                age = wall_now - wall_ts
                if age < 0 or age > 86400:
                    continue
                restored.append((now - age, model))
            self._calls = sorted(restored)

    # ─── 内部 ───────────────────────────────────────────────────────────

    def _required_wait(self) -> float:
        """算出这次请求必须等待多久（秒）。"""

        now = self._clock()
        if now < self._cooldown_until:
            return self._cooldown_until - now
        if not self._calls:
            return 0.0
        gap = self._random_interval()
        elapsed = now - self._calls[-1][0]
        return max(0.0, gap - elapsed)

    def _random_interval(self) -> float:
        """在 [min*scale, max*scale] 内随机取一个间隔。"""

        low = self._config.min_interval_sec * self._interval_scale
        high = min(self._config.max_interval_sec * self._interval_scale, MAX_BACKOFF_INTERVAL_SEC)
        low = min(low, high)
        span = max(0.0, high - low)
        jitter = self._config.jitter_ratio
        if jitter <= 0 or span <= 0:
            return low
        return low + span * min(1.0, max(0.0, self._rng()) * (1.0 + jitter))

    def _effective_daily_limit(self) -> int:
        """返回当前生效的日上限（自适应预算后的值，不低于 min_adaptive_budget）。"""

        raw = max(1, int(self._config.max_per_day))
        if not self._config.adaptive_budget:
            return raw
        scaled = int(round(raw * self._budget_scale))
        lower = min(raw, max(1, int(self._config.min_adaptive_budget)))
        return max(lower, min(raw, scaled))

    def _quiet_factor(self) -> float:
        """返回夜间节律系数（1.0 表示不在夜间或未启用）。"""

        quiet_range = self._quiet_range
        ratio = float(self._config.quiet_hours_ratio)
        if quiet_range is None or ratio >= 1.0:
            return 1.0
        try:
            minute_of_day = int(self._minute_provider()) % (24 * 60)
        except Exception:  # noqa: BLE001 - 取时间失败不能影响对话
            return 1.0
        start, end = quiet_range
        if start <= end:
            inside = start <= minute_of_day < end
        else:
            inside = minute_of_day >= start or minute_of_day < end
        return max(0.0, min(1.0, ratio)) if inside else 1.0

    @staticmethod
    def _scaled_limit(limit: int, factor: float) -> int:
        """按节律系数缩放一个限值（至少保留 1 次，避免夜间完全不可用）。"""

        raw = max(1, int(limit))
        if factor >= 1.0:
            return raw
        return max(1, int(round(raw * factor)))

    def _window_limits(self) -> tuple[tuple[str, float, int], ...]:
        """返回分/时两级窗口限值（已按夜间节律缩放）。"""

        factor = self._quiet_factor()
        return (
            ("分钟", 60.0, self._scaled_limit(self._config.max_per_minute, factor)),
            ("小时", 3600.0, self._scaled_limit(self._config.max_per_hour, factor)),
        )

    def _window_violation(self, now: float) -> tuple[str, float, int, int] | None:
        """返回第一个打满的分/时窗口：(label, window, limit, used)。"""

        for label, window, limit in self._window_limits():
            if limit <= 0:
                continue
            used = self._count_within(now, window)
            if used >= limit:
                return label, window, limit, used
        return None

    def _window_quota_error(self, violation: tuple[str, float, int, int]) -> DeepEtherError:
        """构造分/时配额耗尽的错误。"""

        label, window, limit, used = violation
        return DeepEtherError(
            ErrorCode.PACING_QUOTA,
            f"网页版调用额度已用完（{label}级上限 {limit} 次），本次改用 Host 官方模型",
            retryable=False,
            detail=f"window={window}s used={used} limit={limit}",
        )

    def _raise_window_quota(self, now: float) -> None:
        """分/时配额打满且不值得等待时抛错。"""

        violation = self._window_violation(now)
        if violation is not None:
            raise self._window_quota_error(violation)

    def _window_wait(self, now: float, model_name: str) -> float:
        """分/时配额打满时，返回"等到最早一条滑出窗口"需要的秒数。

        只有保留额度里的高优先级模型（默认 replyer）才值得等：后台 utils 等下去只会把
        回复路径拖慢，而且它本来就会有官方模型兜底。
        """

        violation = self._window_violation(now)
        if violation is None:
            return 0.0
        _label, window, _limit, _used = violation
        max_wait = float(self._config.quota_wait_max_sec)
        if max_wait <= 0 or not self._is_priority_model(model_name):
            self._raise_window_quota(now)
        # 等到窗口里最早的一条滑出窗口即可；多等 50ms 避免边界抖动。
        threshold = now - window
        oldest = min((timestamp for timestamp, _ in self._calls if timestamp > threshold), default=None)
        if oldest is None:
            self._raise_window_quota(now)
        wait_sec = max(0.05, (oldest + window) - now + 0.05)
        if wait_sec > max_wait:
            self._raise_window_quota(now)
        return wait_sec

    def _is_priority_model(self, model_name: str) -> bool:
        """是否属于保留额度里的高优先级模型。"""

        return bool(model_name) and model_name in set(self._config.reserve_models)

    def _hard_block_error(self, now: float, model_name: str) -> DeepEtherError | None:
        """日额度 / 按模型额度 / 保留额度这类"等也没用"的检查。"""

        daily_limit = self._effective_daily_limit()
        used = self._count_within(now, 86400.0)
        if used >= daily_limit:
            return DeepEtherError(
                ErrorCode.PACING_QUOTA,
                f"网页版调用额度已用完（天级上限 {daily_limit} 次），本次改用 Host 官方模型",
                retryable=False,
                detail=f"window=86400s used={used} limit={daily_limit}",
            )

        model_limit = self._config.model_daily_limits.get(model_name or "")
        if model_limit is not None and model_limit > 0:
            model_used = self._count_model_within(now, 86400.0, model_name)
            if model_used >= model_limit:
                return DeepEtherError(
                    ErrorCode.PACING_QUOTA,
                    f"模型 {model_name} 的网页版额度已用完（每天 {model_limit} 次），本次改用 Host 官方模型",
                    retryable=False,
                    detail=f"model={model_name} used={model_used} limit={model_limit}",
                )

        # 保留额度：日预算的最后一截只留给高优先级模型（默认 replyer）。
        # 只有"明确知道是低优先级模型"时才拦：model_name 为空时无法判断，不能把请求全挡掉。
        reserve = max(0, int(self._config.reserve_daily_budget))
        reserved_models = set(self._config.reserve_models)
        if reserve > 0 and model_name and model_name not in reserved_models:
            remaining = daily_limit - used
            if remaining <= reserve:
                names = "、".join(sorted(reserved_models)) or "高优先级模型"
                return DeepEtherError(
                    ErrorCode.PACING_QUOTA,
                    f"网页版当天剩余额度（{remaining} 次）保留给 {names}，本次改用 Host 官方模型",
                    retryable=False,
                    detail=f"remaining={remaining} reserve={reserve} model={model_name or 'unknown'}",
                )
        return None

    def _check_quota(self, now: float, model_name: str) -> None:
        """检查分/时配额、日额度、按模型配额与保留额度，打满就抛错。"""

        violation = self._window_violation(now)
        if violation is not None:
            raise self._window_quota_error(violation)
        hard_block = self._hard_block_error(now, model_name)
        if hard_block is not None:
            raise hard_block

    def _count_within(self, now: float, window: float) -> int:
        """统计最近 window 秒内的调用次数。"""

        threshold = now - window
        return sum(1 for timestamp, _ in self._calls if timestamp > threshold)

    def _count_model_within(self, now: float, window: float, model_name: str) -> int:
        """统计最近 window 秒内某个模型的调用次数。"""

        threshold = now - window
        return sum(1 for timestamp, model in self._calls if timestamp > threshold and model == model_name)

    def _used_by_model(self, now: float) -> dict[str, int]:
        """返回最近 24 小时各模型的调用次数。"""

        threshold = now - 86400.0
        counts: dict[str, int] = {}
        for timestamp, model in self._calls:
            if timestamp <= threshold:
                continue
            key = model or "unknown"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _trim(self, now: float) -> None:
        """丢掉一天以前的调用记录，避免列表无限增长。"""

        threshold = now - 86400.0
        if len(self._calls) > 4096 or (self._calls and self._calls[0][0] < threshold):
            self._calls = [item for item in self._calls if item[0] > threshold]

    def _enter_cooldown(self, now: float, seconds: float) -> None:
        """进入冷却。"""

        if seconds <= 0:
            return
        self._cooldown_until = max(self._cooldown_until, now + float(seconds))
        self._cooldown_since = now

    def _scale_up(self) -> None:
        """把间隔区间翻倍（上限 MAX_BACKOFF_INTERVAL_SEC）。"""

        if not self._config.adaptive_backoff:
            return
        self._interval_scale = min(self._interval_scale * 2.0, MAX_INTERVAL_SCALE)

    def _cooldown_message(self, wait: float) -> str:
        """生成冷却类错误的中文说明。"""

        reason = self._cooldown_reason or "上游节奏控制冷却中"
        return f"{reason}：约 {wait:.0f} 秒后再试，本次改用 Host 官方模型"


def _as_float(value: Any) -> float | None:
    """宽松转 float；失败返回 None。"""

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    """宽松转 int；失败返回 None。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return None

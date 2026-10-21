"""反封禁节奏控制（pacing / quota / risk signals）。

封禁绝大多数由**行为特征**触发，而不是单次请求内容（详见 docs/ANTIBAN.md）。
本模块负责"行为层"里最容易做对也最容易被检测的那一层：

1. 同一个账号`绝不并发`：acquire() 用 asyncio.Lock 串行化，后来的请求排队等待；
2. 间隔带`随机抖动`：固定间隔是最容易被周期性检测的特征；
3. `三级配额`（分/时/日）：任何一级打满都抛 PACING_QUOTA，由上层降级到 Host 官方模型；
4. `风控信号驱动退避`：429 冷却 15 分钟、疑似封号冷却 6 小时，且间隔区间自动翻倍（有上限）。

上层（provider.py）只需要三句话：调用前 await acquire(budget_ms=...)，
成功 note_success()，失败把 classify_failure(...) 的结果交给 note_failure(kind)。
"""

from dataclasses import dataclass
from typing import Any, Callable, Final
import asyncio
import random
import time

from .errors import DeepEtherError, ErrorCode

#: 间隔翻倍的最大值（秒）：再慢下去的等待就该让上层直接降级，而不是把请求挂死。
MAX_BACKOFF_INTERVAL_SEC: Final[float] = 120.0

#: 间隔区间最多放大到基线的多少倍（8 倍 × 基线 20s = 160s，已被上一条兜住）。
MAX_INTERVAL_SCALE: Final[float] = 8.0

#: 连续失败多少次后按"限流"处理（一次偶发超时不该触发 15 分钟冷却）。
CONSECUTIVE_FAILURE_THRESHOLD: Final[int] = 3


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


def classify_failure(
    *,
    status: int | None = None,
    code: Any = None,
    message: str = "",
    body: str = "",
) -> str | None:
    """把一次失败归类成风控信号。

    这个函数是整套退避逻辑的"入口判定"，判错的代价很高：
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

    # 6) 正文为空 / 只有思考：软拦截，DeepSeek 侧常见表现。
    if code_text == ErrorCode.UPSTREAM_EMPTY:
        return BanKind.RATE_LIMITED

    return None


@dataclass(frozen=True)
class PacingConfig:
    """节奏与配额配置。

    Attributes:
        enabled: 是否启用节奏控制；关闭后 PacingController 直接放行（只在排障时关）。
        min_interval_sec: 两次上游请求之间的最小间隔（秒）。
        max_interval_sec: 抖动上界（秒）；实际间隔在 [min, max] 内随机。
        jitter_ratio: 抖动比例，0 表示不加抖动。
        max_per_minute: 每分钟请求上限。
        max_per_hour: 每小时请求上限。
        max_per_day: 每天请求上限。
        cooldown_on_rate_limit_sec: 命中限流后的冷却时间（秒）。
        cooldown_on_ban_sec: 疑似封号后的冷却时间（秒）。
        adaptive_backoff: 是否让冷却后的间隔自动翻倍（连续命中时逐步退避）。
    """

    enabled: bool = True
    min_interval_sec: float = 8.0
    max_interval_sec: float = 20.0
    jitter_ratio: float = 0.25
    max_per_minute: int = 3
    max_per_hour: int = 30
    max_per_day: int = 200
    cooldown_on_rate_limit_sec: float = 900.0
    cooldown_on_ban_sec: float = 21600.0
    adaptive_backoff: bool = True


class PacingController:
    """串行化 + 抖动间隔 + 三级配额 + 风控退避。

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
    ) -> None:
        """初始化节奏控制器。

        Args:
            config: 节奏与配额配置。
            clock: 单调时钟（默认 time.monotonic）；测试可注入假时钟。
            sleeper: 等待函数（默认 asyncio.sleep）；测试可注入记录器，避免真等待。
            rng: 返回 [0, 1) 的随机源（默认 random.random）。
            wall_clock: 墙上时钟（默认 time.time），只用于状态落盘的时间戳换算。
        """

        self._config = config
        self._clock = clock or time.monotonic
        self._sleeper = sleeper or asyncio.sleep
        self._rng = rng or random.random
        self._wall_clock = wall_clock or time.time

        self._lock = asyncio.Lock()
        self._calls: list[float] = []          # 单调时钟下的调用时间戳（升序）
        self._cooldown_until: float = 0.0      # 单调时钟下的冷却结束时刻
        self._cooldown_reason: str = ""
        self._suspected_ban: bool = False
        self._consecutive_failures: int = 0
        self._interval_scale: float = 1.0

    # ─── 对外：取用与回报 ───────────────────────────────────────────────

    async def acquire(self, *, budget_ms: int | None = None) -> float:
        """取得一次上游请求许可（必要时等待）。

        Args:
            budget_ms: 上层剩余预算（毫秒）。预计等待超过预算时立刻抛错，
                让上层降级，而不是把 Host 请求挂到超时。

        Returns:
            float: 实际等待的秒数（未等待返回 0.0）。

        Raises:
            DeepEtherError: 冷却中或配额耗尽，code 为 PACING_COOLDOWN / PACING_QUOTA。
        """

        if not self._config.enabled:
            return 0.0

        async with self._lock:
            waited = 0.0
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

            self._check_quota()
            now = self._clock()
            self._calls.append(now)
            self._trim(now)
            return waited

    def note_success(self) -> None:
        """记录一次成功：清空连续失败计数，间隔缓慢收敛回基线。"""

        self._consecutive_failures = 0
        self._suspected_ban = False
        self._cooldown_reason = ""
        # 成功一次说明上游已经通了，不该再让残留的 cooldown_until 挂着拦后续请求。
        self._cooldown_until = 0.0
        if self._interval_scale > 1.0:
            # 成功一次就把之前的翻倍折半，避免一次风控之后永远慢下去。
            self._interval_scale = max(1.0, self._interval_scale / 2.0)

    def note_failure(self, kind: str | None) -> None:
        """记录一次失败并施加对应退避。

        Args:
            kind: BanKind 的取值；None 表示与风控无关的失败（只记录，不冷却）。
        """

        if kind is None:
            return
        now = self._clock()
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
                "per_minute": self._config.max_per_minute,
                "per_hour": self._config.max_per_hour,
                "per_day": self._config.max_per_day,
            },
        }

    def dump_state(self) -> dict[str, Any]:
        """导出可落盘的节奏状态。

        持久化的意义：重启插件不该等于"配额清零"——否则"重启刷新额度"本身就是
        一个可被观测到的异常模式。
        """

        now = self._clock()
        wall_now = self._wall_clock()
        return {
            "version": 1,
            "cooldown_until_wall": wall_now + max(0.0, self._cooldown_until - now),
            "cooldown_remaining_sec": max(0.0, self._cooldown_until - now),
            "cooldown_reason": self._cooldown_reason,
            "suspected_ban": self._suspected_ban,
            "consecutive_failures": self._consecutive_failures,
            "interval_scale": self._interval_scale,
            # 用墙上时钟保存调用时间点，重启后按同一墙上时钟换算回单调时钟。
            "recent_calls": [wall_now - (now - ts) for ts in self._calls],
        }

    def load_state(self, data: dict[str, Any]) -> None:
        """载入 dump_state 导出的状态。

        数据不可信（可能被手工改坏、也可能是旧版本），任何字段异常都按"忽略该字段"处理：
        宁可丢失配额历史，也不能让插件启动失败。

        Args:
            data: dump_state() 的返回值或其近似结构。
        """

        if not isinstance(data, dict):
            return
        now = self._clock()
        wall_now = self._wall_clock()

        # 优先用墙上时钟的绝对结束时刻：重启时如果已经过去了 10 分钟，
        # 就只补 5 分钟，而不是把 15 分钟冷却重新算一遍（否则重启会变成"加刑"）。
        until_wall = _as_float(data.get("cooldown_until_wall"))
        remaining = until_wall - wall_now if until_wall is not None else _as_float(data.get("cooldown_remaining_sec"))
        if remaining is not None and remaining > 0:
            self._cooldown_until = now + min(remaining, self._config.cooldown_on_ban_sec)
            self._cooldown_reason = str(data.get("cooldown_reason") or "冷却中（来自上次运行）")
        scale = _as_float(data.get("interval_scale"))
        if scale is not None and scale >= 1.0:
            self._interval_scale = min(scale, MAX_INTERVAL_SCALE)
        failures = _as_int(data.get("consecutive_failures"))
        if failures is not None and failures >= 0:
            self._consecutive_failures = failures
        self._suspected_ban = bool(data.get("suspected_ban"))

        calls = data.get("recent_calls")
        if isinstance(calls, list):
            restored: list[float] = []
            for item in calls:
                wall_ts = _as_float(item)
                if wall_ts is None:
                    continue
                age = wall_now - wall_ts
                if age < 0 or age > 86400:
                    continue
                restored.append(now - age)
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
        elapsed = now - self._calls[-1]
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

    def _check_quota(self) -> None:
        """检查三级配额，打满就抛错。"""

        now = self._clock()
        windows = (
            ("分钟", 60.0, self._config.max_per_minute),
            ("小时", 3600.0, self._config.max_per_hour),
            ("天", 86400.0, self._config.max_per_day),
        )
        for label, window, limit in windows:
            if limit <= 0:
                continue
            used = self._count_within(now, window)
            if used >= limit:
                raise DeepEtherError(
                    ErrorCode.PACING_QUOTA,
                    f"网页版调用额度已用完（{label}级上限 {limit} 次），本次改用 Host 官方模型",
                    retryable=False,
                    detail=f"window={window}s used={used} limit={limit}",
                )

    def _count_within(self, now: float, window: float) -> int:
        """统计最近 window 秒内的调用次数。"""

        threshold = now - window
        return sum(1 for ts in self._calls if ts > threshold)

    def _trim(self, now: float) -> None:
        """丢掉一天以前的调用记录，避免列表无限增长。"""

        threshold = now - 86400.0
        if len(self._calls) > 4096 or (self._calls and self._calls[0] < threshold):
            self._calls = [ts for ts in self._calls if ts > threshold]

    def _enter_cooldown(self, now: float, seconds: float) -> None:
        """进入冷却。"""

        if seconds <= 0:
            return
        self._cooldown_until = max(self._cooldown_until, now + float(seconds))

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

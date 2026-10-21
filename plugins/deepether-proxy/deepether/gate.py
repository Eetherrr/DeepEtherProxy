"""并发闸门与熔断器。

报告 §4.2 的容量结论：网页版账号级并发约 3，稳态够用，**瓶颈只在突发峰值**。
因此这里做两件事：

1. **并发闸门**：限制同时进行的上游请求数；排队超过预算就立刻失败（交给降级），
   而不是让 Host 撞 `hard_timeout`；
2. **熔断器**：连续失败到阈值后冷却一段时间，冷却期内直接降级，冷却后只放一个探针。
"""

from types import TracebackType
from typing import Any, Callable, Final

import asyncio
import time

from .errors import DeepEtherError, ErrorCode

STATE_CLOSED: Final[str] = "closed"
STATE_OPEN: Final[str] = "open"
STATE_HALF_OPEN: Final[str] = "half_open"


class ConcurrencyGate:
    """限制并发数量的异步闸门。"""

    def __init__(self, max_concurrency: int, queue_wait_sec: float) -> None:
        """初始化闸门。

        Args:
            max_concurrency: 同时进行上限。
            queue_wait_sec: 排队等待上限（秒），超时即失败。
        """

        self._limit = max(1, int(max_concurrency))
        self._queue_wait_sec = max(0.0, float(queue_wait_sec))
        self._semaphore = asyncio.Semaphore(self._limit)
        self._in_flight = 0
        self._rejected = 0

    @property
    def in_flight(self) -> int:
        """当前进行中的请求数。"""

        return self._in_flight

    @property
    def rejected(self) -> int:
        """因排队超预算被拒绝的请求数。"""

        return self._rejected

    @property
    def limit(self) -> int:
        """并发上限。"""

        return self._limit

    def reconfigure(self, max_concurrency: int, queue_wait_sec: float) -> None:
        """按新配置重建闸门（配置热更新时调用）。

        Args:
            max_concurrency: 新的并发上限。
            queue_wait_sec: 新的排队等待上限。
        """

        self._limit = max(1, int(max_concurrency))
        self._queue_wait_sec = max(0.0, float(queue_wait_sec))
        self._semaphore = asyncio.Semaphore(self._limit)

    async def acquire(self) -> None:
        """获取一个并发槽位。

        Raises:
            DeepEtherError: 排队超时（`concurrency_rejected`）。
        """

        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=self._queue_wait_sec)
        except asyncio.TimeoutError as exc:
            self._rejected += 1
            raise DeepEtherError(
                ErrorCode.CONCURRENCY_REJECTED,
                f"网页版并发已满（{self._limit}），排队超过 {self._queue_wait_sec:.0f}s",
                retryable=True,
            ) from exc
        self._in_flight += 1

    def release(self) -> None:
        """释放一个并发槽位。"""

        self._in_flight = max(0, self._in_flight - 1)
        self._semaphore.release()

    async def __aenter__(self) -> "ConcurrencyGate":
        """异步上下文入口。"""

        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """异步上下文出口。"""

        self.release()


class CircuitBreaker:
    """简单的三态熔断器（closed / open / half_open）。"""

    def __init__(
        self,
        *,
        fail_threshold: int,
        cooldown_sec: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化熔断器。

        Args:
            fail_threshold: 连续失败多少次后打开。
            cooldown_sec: 打开后的冷却时间（秒）。
            clock: 可注入的时钟，便于单测。
        """

        self._threshold = max(1, int(fail_threshold))
        self._cooldown_sec = max(0.1, float(cooldown_sec))
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        """返回当前状态。"""

        if self._opened_at is None:
            return STATE_CLOSED
        if self._clock() - self._opened_at >= self._cooldown_sec:
            return STATE_HALF_OPEN
        return STATE_OPEN

    @property
    def consecutive_failures(self) -> int:
        """返回连续失败次数。"""

        return self._failures

    @property
    def probe_in_flight(self) -> bool:
        """是否有一个 half_open 探针正在进行（且尚未记录结果）。"""

        return self._probe_in_flight

    def abort_probe(self) -> None:
        """归还 half_open 探针名额。

        请求被取消、超预算被闸门拒绝等情况下不会走 record_success/record_failure，
        如果不归还名额，熔断器会永久停在 half_open，之后所有请求都被强制降级——
        表现为"麦麦还能回话，但网页版再也不用了"，很难排查。
        """

        self._probe_in_flight = False

    def allow(self) -> bool:
        """判断是否放行本次请求。

        Returns:
            bool: `True` 表示可以请求上游。
        """

        state = self.state
        if state == STATE_CLOSED:
            return True
        if state == STATE_OPEN:
            return False
        # half_open：只放一个探针
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        """记录一次成功，关闭熔断。"""

        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        """记录一次失败，达到阈值则打开熔断。"""

        self._failures += 1
        self._probe_in_flight = False
        if self._failures >= self._threshold:
            self._opened_at = self._clock()

    def reconfigure(self, fail_threshold: int, cooldown_sec: float) -> None:
        """按新配置更新阈值。

        Args:
            fail_threshold: 新的失败阈值。
            cooldown_sec: 新的冷却时间。
        """

        self._threshold = max(1, int(fail_threshold))
        self._cooldown_sec = max(0.1, float(cooldown_sec))

    def describe(self) -> dict[str, Any]:
        """返回可写入日志/状态的结构化描述。"""

        return {
            "state": self.state,
            "consecutive_failures": self._failures,
            "threshold": self._threshold,
            "cooldown_sec": self._cooldown_sec,
        }

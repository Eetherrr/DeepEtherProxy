"""CommandCode 的节奏控制：串行 + 最小间隔 + 每分钟上限。

金额已经是主约束，这里只防"突发"：官方没有公布并发上限，保守起步。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Callable, Deque, Final

from ..errors import DeepEtherError, ErrorCode

_DEFAULT_MAX_PER_MINUTE: Final[int] = 6


class CommandCodePacer:
    """按最小间隔与每分钟上限放行请求。"""

    def __init__(
        self,
        *,
        min_interval_sec: float = 0.0,
        max_per_minute: int = _DEFAULT_MAX_PER_MINUTE,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], "asyncio.Future[None]"] | None = None,
    ) -> None:
        """初始化节奏控制器。

        Args:
            min_interval_sec: 两次请求之间的最小间隔（秒）。
            max_per_minute: 每分钟最多几次。
            clock: 可注入时钟。
            sleep: 可注入的异步睡眠函数（单测用）。
        """

        self._min_interval = max(0.0, float(min_interval_sec))
        self._max_per_minute = max(1, int(max_per_minute))
        self._clock = clock
        self._sleep = sleep or asyncio.sleep
        self._calls: Deque[float] = deque(maxlen=self._max_per_minute)
        self._last_started: float | None = None
        self._waited_total = 0.0

    @property
    def waited_total(self) -> float:
        """累计等待秒数（状态展示用）。"""

        return self._waited_total

    def reconfigure(self, *, min_interval_sec: float, max_per_minute: int) -> None:
        """配置热更新时刷新参数。"""

        self._min_interval = max(0.0, float(min_interval_sec))
        self._max_per_minute = max(1, int(max_per_minute))
        self._calls = deque(self._calls, maxlen=self._max_per_minute)

    def _required_wait(self) -> float:
        """计算还需要等多久。"""

        now = self._clock()
        wait = 0.0
        if self._last_started is not None:
            wait = max(wait, self._last_started + self._min_interval - now)
        recent = [stamp for stamp in self._calls if now - stamp < 60.0]
        if len(recent) >= self._max_per_minute:
            wait = max(wait, recent[0] + 60.0 - now)
        return max(0.0, wait)

    async def acquire(self, *, budget_sec: float) -> float:
        """等够节奏再放行。

        Args:
            budget_sec: 允许等待的上限（秒）；等不到就立刻失败，不把请求挂到超时。

        Returns:
            float: 实际等待秒数。

        Raises:
            DeepEtherError: 需要等待的时间超过预算（`pacing_quota`）。
        """

        wait = self._required_wait()
        if wait > max(0.0, float(budget_sec)):
            raise DeepEtherError(
                ErrorCode.PACING_QUOTA,
                "CommandCode 通道节奏限制：预计需要等待 "
                f"{wait:.1f}s，超过本次可用预算 {budget_sec:.1f}s",
                retryable=True,
            )
        if wait > 0:
            await self._sleep(wait)
            self._waited_total += wait
        now = self._clock()
        self._calls.append(now)
        self._last_started = now
        return wait

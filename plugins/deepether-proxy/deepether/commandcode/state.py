"""CommandCode 通道的运行期统计（与 DeepSeek 网页版分开计数）。"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Deque, Final

_LATENCY_SAMPLE_LIMIT: Final[int] = 50
_FAILURE_SAMPLE_LIMIT: Final[int] = 200


@dataclass
class CcRuntimeState:
    """CommandCode 通道的成功/失败与耗时统计。"""

    started_at: datetime = field(default_factory=datetime.now)
    success: int = 0
    failure: int = 0
    last_error: str = ""
    last_error_at: datetime | None = None
    latencies_ms: Deque[int] = field(default_factory=lambda: deque(maxlen=_LATENCY_SAMPLE_LIMIT))
    failure_times: Deque[datetime] = field(default_factory=lambda: deque(maxlen=_FAILURE_SAMPLE_LIMIT))

    def record_success(self, latency_ms: int) -> None:
        """记录一次成功调用。"""

        self.success += 1
        if latency_ms > 0:
            self.latencies_ms.append(int(latency_ms))

    def record_failure(self, code: str, message: str) -> None:
        """记录一次失败调用。"""

        self.failure += 1
        self.failure_times.append(datetime.now())
        self.last_error = f"[{code}] {message}"
        self.last_error_at = datetime.now()

    @property
    def failures_last_24h(self) -> int:
        """返回最近 24 小时的失败次数。"""

        cutoff = datetime.now() - timedelta(hours=24)
        return sum(1 for moment in self.failure_times if moment >= cutoff)

    @property
    def average_latency_ms(self) -> int | None:
        """返回最近成功调用的平均耗时。"""

        if not self.latencies_ms:
            return None
        return int(sum(self.latencies_ms) / len(self.latencies_ms))

    @property
    def uptime_text(self) -> str:
        """返回运行时长（中文）。"""

        seconds = int((datetime.now() - self.started_at).total_seconds())
        hours, remainder = divmod(seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours} 小时 {minutes} 分"
        if minutes:
            return f"{minutes} 分 {secs} 秒"
        return f"{secs} 秒"

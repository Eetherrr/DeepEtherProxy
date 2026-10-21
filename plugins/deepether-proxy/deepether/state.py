"""运行时状态与降级可见性统计。

报告 §6.6.2 要求"降级必须可见"：所有失败、降级、熔断都必须在这里计数，
并通过 `/deepether-status` 让用户一眼看到，避免"以为在省钱其实在烧官方额度"。
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Deque, Final

_LATENCY_SAMPLE_LIMIT: Final[int] = 50
_FALLBACK_SAMPLE_LIMIT: Final[int] = 200


@dataclass
class RuntimeState:
    """插件运行期统计。"""

    started_at: datetime = field(default_factory=datetime.now)
    web_success: int = 0
    web_failure: int = 0
    fallback_count: int = 0
    probe_ok: bool | None = None
    probe_detail: str = ""
    probe_at: datetime | None = None
    last_error: str = ""
    last_error_at: datetime | None = None
    latencies_ms: Deque[int] = field(default_factory=lambda: deque(maxlen=_LATENCY_SAMPLE_LIMIT))
    fallback_times: Deque[datetime] = field(default_factory=lambda: deque(maxlen=_FALLBACK_SAMPLE_LIMIT))

    def record_success(self, latency_ms: int) -> None:
        """记录一次网页版成功调用。

        Args:
            latency_ms: 端到端耗时（毫秒）。
        """

        self.web_success += 1
        if latency_ms > 0:
            self.latencies_ms.append(int(latency_ms))

    def record_failure(self, code: str, message: str) -> None:
        """记录一次网页版失败。

        Args:
            code: 错误码。
            message: 中文错误说明。
        """

        self.web_failure += 1
        self.last_error = f"[{code}] {message}"
        self.last_error_at = datetime.now()

    def record_fallback(self, reason: str) -> None:
        """记录一次降级。

        Args:
            reason: 中文降级原因。
        """

        self.fallback_count += 1
        self.fallback_times.append(datetime.now())
        self.last_error = f"[fallback] {reason}"
        self.last_error_at = datetime.now()

    def record_probe(self, ok: bool, detail: str = "") -> None:
        """记录一次凭证/链路探活结果。

        Args:
            ok: 探活是否通过。
            detail: 中文补充说明。
        """

        self.probe_ok = ok
        self.probe_detail = detail
        self.probe_at = datetime.now()

    @property
    def fallbacks_last_24h(self) -> int:
        """返回最近 24 小时的降级次数。

        报告 §6.6.2 要求状态命令显示「最近 24 小时降级 N 次」——
        只有累计值看不出"是不是刚刚一直在降级"，这一个窗口才是真正可观测的信号。
        """

        cutoff = datetime.now() - timedelta(hours=24)
        return sum(1 for moment in self.fallback_times if moment >= cutoff)

    @property
    def average_latency_ms(self) -> int | None:
        """返回最近若干次成功调用的平均耗时。"""

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

    def reset(self) -> None:
        """清零统计（保留启动时间）。"""

        self.web_success = 0
        self.web_failure = 0
        self.fallback_count = 0
        self.last_error = ""
        self.last_error_at = None
        self.latencies_ms.clear()
        self.fallback_times.clear()

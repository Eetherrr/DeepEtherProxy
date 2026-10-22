"""运行时状态与失败可见性统计。

报告 §6.6.2 要求"失败必须可见"：插件是一个普通模型通道，失败后由 MaiBot 的模型调度
处理，但"这条通道最近失败了没有、失败了多少次"仍然要能一眼看到，否则很容易
"以为在省钱其实在烧官方额度"。因此这里统计网页版通道的成功/失败次数、最近错误与耗时，
并通过 `/deepether-status` 展示。
"""

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Deque, Final

_LATENCY_SAMPLE_LIMIT: Final[int] = 50
_FAILURE_SAMPLE_LIMIT: Final[int] = 200


@dataclass
class RuntimeState:
    """插件运行期统计。"""

    started_at: datetime = field(default_factory=datetime.now)
    web_success: int = 0
    web_failure: int = 0
    probe_ok: bool | None = None
    probe_detail: str = ""
    probe_at: datetime | None = None
    last_error: str = ""
    last_error_at: datetime | None = None
    latencies_ms: Deque[int] = field(default_factory=lambda: deque(maxlen=_LATENCY_SAMPLE_LIMIT))
    failure_times: Deque[datetime] = field(default_factory=lambda: deque(maxlen=_FAILURE_SAMPLE_LIMIT))

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
        self.failure_times.append(datetime.now())
        self.last_error = f"[{code}] {message}"
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
    def failures_last_24h(self) -> int:
        """返回最近 24 小时的失败次数。

        报告 §6.6.2 要求状态命令显示「最近 24 小时失败 N 次」——
        只有累计值看不出"是不是刚刚一直在失败"，这一个窗口才是真正可观测的信号
        （例如网页版整体不可用时会持续增长）。
        """

        cutoff = datetime.now() - timedelta(hours=24)
        return sum(1 for moment in self.failure_times if moment >= cutoff)

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
        self.last_error = ""
        self.last_error_at = None
        self.latencies_ms.clear()
        self.failure_times.clear()

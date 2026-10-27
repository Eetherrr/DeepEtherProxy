"""CommandCode 的金额预算（5 小时 / 7 天 / 30 天滚动窗口）。

Go 套餐按 credits 计费而不是按请求次数，所以这里统计美元成本而不是次数：
- 上游窗口是 5 小时 $3 / 周 $6 / 月 $10（2026-10 口径）；
- 本插件默认更保守（3 / 6 / 9），并额外留 `reserve` 给 planner 等高价值场景；
- 状态落盘，重启不重置（否则改个配置就把额度清零了）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Final

from ..errors import DeepEtherError, ErrorCode
from .constants import (
    DEFAULT_BUDGET_RESERVE_USD,
    DEFAULT_FIVE_HOUR_BUDGET_USD,
    DEFAULT_MONTHLY_BUDGET_USD,
    DEFAULT_WEEKLY_BUDGET_USD,
)

WINDOW_FIVE_HOUR: Final[int] = 5 * 3600
WINDOW_WEEK: Final[int] = 7 * 24 * 3600
WINDOW_MONTH: Final[int] = 30 * 24 * 3600
_STATE_VERSION: Final[int] = 1
_ENTRY_TTL_SEC: Final[int] = WINDOW_MONTH + 24 * 3600


@dataclass(frozen=True)
class BudgetLimits:
    """三个窗口的美元上限与保留额度。"""

    five_hour: float = DEFAULT_FIVE_HOUR_BUDGET_USD
    weekly: float = DEFAULT_WEEKLY_BUDGET_USD
    monthly: float = DEFAULT_MONTHLY_BUDGET_USD
    reserve: float = DEFAULT_BUDGET_RESERVE_USD


class BudgetTracker:
    """滚动窗口预算；线程内单实例使用（Provider 内部串行访问）。"""

    def __init__(
        self,
        limits: BudgetLimits,
        *,
        state_path: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """初始化预算跟踪器。

        Args:
            limits: 三窗口上限与保留额度。
            state_path: 落盘路径；为空表示只在内存里统计。
            clock: 可注入的时钟（单测用）。
        """

        self._limits = limits
        self._path = state_path
        self._clock = clock
        self._entries: list[dict[str, Any]] = []

    @property
    def limits(self) -> BudgetLimits:
        """返回当前上限。"""

        return self._limits

    # ─── 读写 ───────────────────────────────────────────────────────────

    def load(self) -> None:
        """从磁盘恢复历史花费（文件不存在或损坏时按空状态继续）。"""

        if self._path is None or not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        entries = raw.get("entries") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            return
        now = self._clock()
        restored: list[dict[str, Any]] = []
        for item in entries:
            if not isinstance(item, dict):
                continue
            try:
                ts = float(item.get("ts"))
                cost = float(item.get("cost"))
            except (TypeError, ValueError):
                continue
            if now - ts > _ENTRY_TTL_SEC:
                continue
            restored.append({"ts": ts, "cost": max(0.0, cost), "model": str(item.get("model") or "")})
        self._entries = restored

    def persist(self) -> None:
        """把花费记录写回磁盘（失败时静默：只影响统计，不影响请求）。"""

        if self._path is None:
            return
        self._trim()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"version": _STATE_VERSION, "entries": self._entries}, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except OSError:
            return

    # ─── 统计 ───────────────────────────────────────────────────────────

    def spent(self, window_sec: int) -> float:
        """返回某个滚动窗口内的花费（美元）。"""

        cutoff = self._clock() - window_sec
        return sum(float(item["cost"]) for item in self._entries if float(item["ts"]) >= cutoff)

    def remaining(self) -> float:
        """返回三个窗口里最紧的剩余额度（美元）。"""

        limits = self._limits
        candidates = (
            limits.five_hour - self.spent(WINDOW_FIVE_HOUR),
            limits.weekly - self.spent(WINDOW_WEEK),
            limits.monthly - self.spent(WINDOW_MONTH),
        )
        return max(0.0, min(candidates))

    def check(self, estimated_cost: float) -> None:
        """前置检查：额度不足时直接失败，不把请求发出去再撞 429。

        Args:
            estimated_cost: 本次调用的预估成本（美元）。

        Raises:
            DeepEtherError: 剩余额度低于保留额或不足以覆盖预估成本。
        """

        remaining = self.remaining()
        usable = remaining - max(0.0, self._limits.reserve)
        if remaining <= 0:
            raise DeepEtherError(
                ErrorCode.BUDGET_EXHAUSTED,
                "CommandCode 本地预算已用尽（5 小时/周/月窗口），请等待窗口重置或在配置里调整上限",
                detail=f"remaining=${remaining:.4f}",
            )
        if usable <= 0:
            raise DeepEtherError(
                ErrorCode.BUDGET_EXHAUSTED,
                "CommandCode 本地预算只剩保留额度（留给 planner 等高价值场景），普通调用已暂停",
                detail=f"remaining=${remaining:.4f} reserve=${self._limits.reserve:.4f}",
            )
        if estimated_cost > usable:
            raise DeepEtherError(
                ErrorCode.BUDGET_EXHAUSTED,
                "CommandCode 本地预算不足以覆盖本次调用，请换更便宜的模型或等待窗口重置",
                detail=(
                    f"estimate=${estimated_cost:.4f} usable=${usable:.4f} "
                    f"(remaining=${remaining:.4f} reserve=${self._limits.reserve:.4f})"
                ),
            )

    def record(self, cost_usd: float, model: str) -> None:
        """记录一次调用的实际成本。"""

        self._entries.append(
            {"ts": self._clock(), "cost": max(0.0, float(cost_usd)), "model": str(model or "")}
        )
        self._trim()

    def snapshot(self) -> dict[str, Any]:
        """返回供状态命令展示的快照。"""

        limits = self._limits
        five = self.spent(WINDOW_FIVE_HOUR)
        week = self.spent(WINDOW_WEEK)
        month = self.spent(WINDOW_MONTH)
        return {
            "five_hour_used": round(five, 4),
            "five_hour_limit": limits.five_hour,
            "weekly_used": round(week, 4),
            "weekly_limit": limits.weekly,
            "monthly_used": round(month, 4),
            "monthly_limit": limits.monthly,
            "remaining": round(self.remaining(), 4),
            "reserve": limits.reserve,
            "entries": len(self._entries),
        }

    def _trim(self) -> None:
        """丢弃超过 30 天+1 天的记录。"""

        cutoff = self._clock() - _ENTRY_TTL_SEC
        self._entries = [item for item in self._entries if float(item["ts"]) >= cutoff]

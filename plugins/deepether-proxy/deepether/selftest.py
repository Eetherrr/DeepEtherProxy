"""启动自检与定时探活。

报告 §6.6.3 的自检清单里，只有"凭证探活"能低成本高频执行（`users/current` 很轻），
建会话 / PoW / 首增量都会消耗额度并可能触发风控，所以只放在 `/deepether-status`
的手动排查里说明，不自动高频跑。
"""

from typing import Any

import asyncio
import time

from .errors import DeepEtherError
from .state import RuntimeState
from .upstream.web_client import WebUpstreamClient


async def probe_credential(client: WebUpstreamClient, state: RuntimeState, logger: Any) -> bool:
    """探活一次网页版凭证。

    Args:
        client: 上游客户端。
        state: 运行时统计（会记录探活结果）。
        logger: 插件日志器。

    Returns:
        bool: 凭证是否仍然有效。
    """

    try:
        await client.probe_user()
    except DeepEtherError as exc:
        state.record_probe(False, f"{exc.code}: {exc.message}")
        logger.warning("凭证探活失败：%s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - 探活不允许把插件打挂
        state.record_probe(False, f"{type(exc).__name__}: {exc}")
        logger.warning("凭证探活异常：%s", exc)
        return False
    state.record_probe(True, "凭证有效")
    return True


class PeriodicProbe:
    """按间隔在后台跑凭证探活的守护任务。"""

    def __init__(
        self,
        *,
        client_factory: Any,
        state: RuntimeState,
        logger: Any,
        interval_sec: float,
    ) -> None:
        """初始化定时探活。

        Args:
            client_factory: 返回 `WebUpstreamClient` 的无参可调用对象。
            state: 运行时统计。
            logger: 插件日志器。
            interval_sec: 探活间隔（秒）。
        """

        self._client_factory = client_factory
        self._state = state
        self._logger = logger
        self._interval_sec = max(60.0, float(interval_sec))
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """启动后台探活任务（幂等）。"""

        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        """停止后台探活任务。"""

        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 取消时忽略一切
            pass

    async def _loop(self) -> None:
        """循环探活。"""

        while True:
            await asyncio.sleep(self._interval_sec)
            client = self._client_factory()
            if client is None:
                continue
            started = time.monotonic()
            ok = await probe_credential(client, self._state, self._logger)
            if self._logger is not None:
                self._logger.debug("定时探活完成：%s（%.0f ms）", ok, (time.monotonic() - started) * 1000)

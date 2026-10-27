"""`/deepether-status` 的文本渲染：只保留一眼能看懂的几行。

设计原则：默认只显示「现在能不能用、今天用了多少、有没有报错」；
详细参数（自适应预算、夜间节律、按模型用量、书签、探活…）不再铺在聊天里，
需要时去 WebUI 看配置即可。
"""

from __future__ import annotations

from typing import Any, Final

from .config import DeepEtherProxyConfig
from .credential import CredentialStore
from .state import RuntimeState

_STATE_TEXT: Final[dict[str, str]] = {
    "missing": "未配置",
    "present": "已就绪",
    "invalid": "已失效",
}


def render_status(
    *,
    version: str,
    config: DeepEtherProxyConfig,
    store: CredentialStore,
    state: RuntimeState,
    loopback_running: bool = False,
    loopback_enabled: bool = False,
    loopback_port: int = 0,
    nonce: str = "",
    circuit_state: str = "closed",
    gate_in_flight: int = 0,
    gate_limit: int = 0,
    gate_rejected: int = 0,
    pacing: dict[str, Any] | None = None,
    provision: str | None = None,
    cc: dict[str, Any] | None = None,
) -> str:
    """渲染状态命令：最多 6 行，够用为止。

    Args:
        version: 插件版本号。
        config: 当前配置。
        store: 凭证存储。
        state: 运行时统计。
        loopback_running: 授权服务是否在监听（仅在启用时显示）。
        loopback_enabled: 是否启用了授权服务。
        loopback_port: 授权服务端口。
        nonce: 书签口令（仅启用时显示）。
        circuit_state: 网页版熔断器状态。
        pacing: 节奏快照（可为空）。
        cc: CommandCode 摘要（five_hour_used / five_hour_limit / circuit_state）。
        gate_in_flight / gate_limit / gate_rejected / provision: 兼容旧调用，不再展示。

    Returns:
        str: 简体中文状态文本。
    """

    record = store.record
    tokens = list(getattr(config.credential, "web_tokens", []) or [])
    account_count = len(tokens) or (1 if record is not None else 0)

    lines: list[str] = [
        f"DeepEther v{version}｜{config.behavior.mode}｜运行 {state.uptime_text}",
    ]

    credential_line = f"凭证：{_STATE_TEXT.get(store.state, store.state)}"
    if record is not None:
        credential_line += f" {record.masked}"
    credential_line += f"（{account_count} 个账号）"
    lines.append(credential_line)

    if pacing:
        limits = pacing.get("limits") or {}
        web_line = (
            f"网页版：今日 {pacing.get('used_last_day', 0)}/{limits.get('per_day', '?')}"
            f"｜1 小时 {pacing.get('used_last_hour', 0)}/{limits.get('per_hour', '?')}"
        )
        if pacing.get("in_cooldown"):
            web_line += f"｜冷却 {int(float(pacing.get('cooldown_remaining_sec') or 0))}s"
        web_line += f"｜熔断 {circuit_state}"
        lines.append(web_line)

    if cc:
        used = cc.get("five_hour_used")
        limit = cc.get("five_hour_limit")
        if used is not None and limit is not None:
            lines.append(
                f"CommandCode：5 小时 ${float(used):.2f}/${float(limit):.2f}"
                f"｜熔断 {cc.get('circuit_state', 'closed')}"
            )

    if store.invalid_reason:
        lines.append(f"失效原因：{store.invalid_reason}")

    if state.last_error:
        stamp = state.last_error_at.strftime("%H:%M:%S") if state.last_error_at else "未知时间"
        lines.append(f"最近错误：{stamp} {state.last_error}")

    if loopback_enabled and loopback_running:
        lines.append(f"授权页：http://127.0.0.1:{loopback_port}/（nonce {nonce or '未生成'}）")

    return "\n".join(lines)

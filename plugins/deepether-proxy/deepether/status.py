"""`/deepether-status` 的文本渲染。

保持纯函数：输入全部是显式参数，输出是简体中文多行文本，便于单测断言。
"""

from typing import Any, Final

from .bookmarklet import build_bookmarklet
from .config import DeepEtherProxyConfig
from .credential import CredentialStore
from .state import RuntimeState

_STATE_TEXT: Final[dict[str, str]] = {
    "missing": "未配置（请在插件配置 credential.web_token 填入）",
    "present": "已就绪",
    "invalid": "已失效，需要重新授权",
}


def render_status(
    *,
    version: str,
    config: DeepEtherProxyConfig,
    store: CredentialStore,
    state: RuntimeState,
    loopback_running: bool,
    loopback_port: int,
    loopback_enabled: bool = True,
    nonce: str,
    circuit_state: str = "closed",
    gate_in_flight: int = 0,
    gate_limit: int = 0,
    gate_rejected: int = 0,
    pacing: dict[str, Any] | None = None,
    provision: str | None = None,
) -> str:
    """渲染状态命令的完整输出。

    Args:
        version: 插件版本号。
        config: 当前配置。
        store: 凭证存储。
        state: 运行时统计。
        loopback_running: 回环服务是否在监听。
        loopback_port: 实际监听端口。
        nonce: 当前书签口令。

    Returns:
        str: 简体中文多行状态文本。
    """

    record = store.record
    credential_state = store.state
    lines: list[str] = [
        f"DeepEther Proxy v{version} 状态",
        f"通道策略：{config.behavior.mode}｜思考：{'开' if config.behavior.thinking_enabled else '关'}"
        f"｜联网：{'开' if config.behavior.search_enabled else '关'}",
        f"运行时长：{state.uptime_text}",
        "",
        "【凭证】",
        f"状态：{_STATE_TEXT.get(credential_state, credential_state)}",
        f"来源：{record.source if record else '无'}｜更新于：{record.updated_at if record else '无'}",
        f"指纹：{record.masked if record else '(空)'}",
    ]
    if store.invalid_reason:
        lines.append(f"失效原因：{store.invalid_reason}")
    if config.credential.web_token:
        lines.append("说明：当前使用的是插件配置里的 credential.web_token（推荐方式）")

    lines.extend(
        [
            "",
            "【凭证接收服务】",
            f"状态：{'监听中' if loopback_running else ('已关闭（发布版默认）' if not loopback_enabled else '未启动')}"
            f"｜端口：{loopback_port}（仅 127.0.0.1）",
        ]
    )

    if loopback_enabled:
        lines.extend(
            [
                f"授权页：http://127.0.0.1:{loopback_port}/ （在里面粘贴 userToken 也能完成授权）",
                f"口令 nonce：{nonce or '(未生成)'}",
                "",
                "【书签小工具】",
                "把下面这一行整行复制成书签，在已登录的 chat.deepseek.com 页面上点一下即可完成授权：",
                build_bookmarklet(loopback_port, nonce) if loopback_running and nonce else "(服务未启动，暂无书签)",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "【如何更新凭证】",
                "1. 浏览器登录 chat.deepseek.com，按 F12 打开开发者工具 → Console；",
                "2. 粘贴执行：copy(JSON.parse(localStorage.getItem('userToken')).value)；",
                "3. 打开 MaiBot 的 WebUI → 插件 → DeepEther Proxy，把复制到的 token 填进凭证里的 web_token 并保存；",
                "4. 回到聊天发送 /deepether-status 确认状态变成「已就绪」。",
                "",
            ]
        )

    lines.extend(
        [
            "【统计】",
            f"网页版成功：{state.web_success} 次｜失败：{state.web_failure} 次"
            f"｜降级：{state.fallback_count} 次（最近 24 小时 {state.fallbacks_last_24h} 次）",
            f"平均耗时：{state.average_latency_ms} ms" if state.average_latency_ms else "平均耗时：暂无数据",
            f"熔断器：{circuit_state}｜进行中：{gate_in_flight}/{gate_limit or '?'}"
            f"｜闸门拒绝：{gate_rejected} 次",
        ]
    )

    if provision:
        lines.extend(["【自动注册】", provision])

    lines.extend(_render_pacing(config, pacing))

    if state.probe_at is not None:
        probe_text = "通过" if state.probe_ok else "失败"
        lines.append(f"最近探活：{probe_text}（{state.probe_at.strftime('%H:%M:%S')}）{state.probe_detail}")
    else:
        lines.append("最近探活：尚未执行")

    if state.last_error:
        stamp = state.last_error_at.strftime("%H:%M:%S") if state.last_error_at else "未知时间"
        lines.append(f"最近错误：{stamp} {state.last_error}")

    return "\n".join(lines)


def _render_pacing(config: DeepEtherProxyConfig, pacing: dict[str, Any] | None) -> list[str]:
    """渲染反封禁节奏段落。

    Args:
        config: 当前配置。
        pacing: PacingController.snapshot() 的返回值；为空时只显示配置。

    Returns:
        list[str]: 待追加的行（含前置空行）。
    """

    section = config.antiban
    lines: list[str] = ["", "【反封禁节奏】"]
    if not section.enabled:
        lines.append("状态：已关闭（仅排障时使用，关闭后账号更容易被风控）")
        return lines

    if not pacing:
        lines.append(
            f"配置：间隔 {section.min_interval_sec:.0f}~{section.max_interval_sec:.0f} 秒"
            f"｜配额 {section.max_per_minute}/分 · {section.max_per_hour}/时 · {section.max_per_day}/天"
        )
        lines.append("运行状态：暂无调用记录")
        return lines

    effective = pacing.get("effective_interval_sec") or (section.min_interval_sec, section.max_interval_sec)
    lines.append(
        f"间隔：{float(effective[0]):.1f}~{float(effective[1]):.1f} 秒"
        f"（含当前退避倍数 {pacing.get('interval_scale')}）"
    )
    limits = pacing.get("limits") or {}
    lines.append(
        f"用量：近 1 分钟 {pacing.get('used_last_minute')}/{limits.get('per_minute')}"
        f"｜1 小时 {pacing.get('used_last_hour')}/{limits.get('per_hour')}"
        f"｜1 天 {pacing.get('used_last_day')}/{limits.get('per_day')}"
    )
    if pacing.get("in_cooldown"):
        lines.append(
            f"冷却中：{pacing.get('cooldown_reason') or '上游风控'}"
            f"（约 {float(pacing.get('cooldown_remaining_sec') or 0):.0f} 秒后恢复；期间自动走官方模型）"
        )
    else:
        lines.append("冷却：无")
    if pacing.get("suspected_ban"):
        lines.append("注意：最近检测到封禁级信号；如持续出现请在浏览器里确认账号状态")
    if int(pacing.get("consecutive_failures") or 0) > 0:
        lines.append(f"连续失败：{pacing.get('consecutive_failures')} 次（达到 3 次会升级成长冷却）")
    return lines

"""`/commandcode-status` 与 `/commandcode-models` 的文本渲染。

保持纯函数：输入全部显式传入，便于单测。
"""

from __future__ import annotations

from typing import Any, Final, Iterable

from ..errors import mask_secret
from .catalog import ModelCatalog

_VISION_MARK: Final[str] = "视觉"


def render_commandcode_status(
    *,
    version: str,
    enabled: bool,
    api_key: str,
    base_url: str,
    cli_version: str,
    catalog: ModelCatalog,
    register_models: str,
    registered_models: int,
    state: Any,
    budget: dict[str, Any] | None,
    circuit_state: str,
    gate_in_flight: int,
    gate_limit: int,
    gate_rejected: int,
    last_model: str = "",
    thread_id: str = "",
    provision: str | None = None,
) -> str:
    """渲染 CommandCode 通道状态。

    Args:
        version: 插件版本。
        enabled: `[commandcode] enabled` 的当前值。
        api_key: 当前 Key（内部脱敏后展示）。
        base_url: 上游地址。
        cli_version: 上报的 CLI 版本。
        catalog: 模型目录。
        register_models: `all` / `curated` / `none`。
        registered_models: 已写入 `model_config.toml` 的 cc 模型数。
        state: `CcRuntimeState`。
        budget: `BudgetTracker.snapshot()` 的结果。
        circuit_state: 熔断器状态。
        gate_in_flight: 进行中的请求数。
        gate_limit: 并发上限。
        gate_rejected: 被闸门拒绝的次数。
        last_model: 最近一次调用的模型 id。
        thread_id: 当前复用的 threadId。
        provision: 自动注册摘要。

    Returns:
        str: 简体中文多行文本。
    """

    lines: list[str] = [
        f"CommandCode 通道状态（插件 v{version}）",
        f"开关：{'已启用' if enabled else '已关闭（[commandcode] enabled=false）'}"
        f"｜Key：{mask_secret(api_key)}｜上游：{base_url}",
        f"目录：{len(catalog)} 条 Go 模型（CLI {catalog.cli_version or cli_version} 口径）"
        f"｜注册模式：{register_models}｜已注册：{registered_models} 条",
        f"最近模型：{last_model or '(无)'}｜threadId：{thread_id or '(未创建)'}",
        "",
        "【预算（美元，本地硬闸）】",
    ]
    if budget:
        lines.extend(
            [
                f"5 小时：${budget.get('five_hour_used', 0):.4f} / ${budget.get('five_hour_limit', 0):.2f}"
                f"｜周：${budget.get('weekly_used', 0):.4f} / ${budget.get('weekly_limit', 0):.2f}"
                f"｜月：${budget.get('monthly_used', 0):.4f} / ${budget.get('monthly_limit', 0):.2f}",
                f"剩余：${budget.get('remaining', 0):.4f}（保留 ${budget.get('reserve', 0):.2f}）"
                f"｜记账条数：{budget.get('entries', 0)}",
            ]
        )
    else:
        lines.append("尚未产生调用，暂无预算数据（上限见插件配置 [commandcode]）")

    lines.extend(
        [
            "",
            "【运行统计】",
            f"成功：{state.success} 次｜失败：{state.failure} 次（最近 24 小时 {state.failures_last_24h} 次）",
            "平均耗时：" + (f"{state.average_latency_ms} ms" if state.average_latency_ms else "暂无数据"),
            f"熔断器：{circuit_state}｜进行中：{gate_in_flight}/{gate_limit or '?'}"
            f"｜闸门拒绝：{gate_rejected} 次",
        ]
    )
    if state.last_error:
        lines.append(f"最近错误：{state.last_error}")

    if provision:
        lines.extend(["", "【自动注册】", provision])

    lines.extend(
        [
            "",
            "【说明】本通道只注册模型、不自动接线：",
            "在 MaiBot WebUI → 模型配置里，把需要的 cc-* 模型加到对应任务的 model_list 即可；",
            "工具调用（planner）请只用工具能力强的模型（Kimi K3 / GLM-5.3 / DeepSeek V4 Pro / MiniMax M3）。",
        ]
    )
    return "\n".join(lines)


def render_commandcode_models(
    catalog: ModelCatalog,
    registered: Iterable[str],
    *,
    keyword: str = "",
) -> str:
    """渲染模型清单（标记是否已注册）。

    Args:
        catalog: 模型目录。
        registered: 已注册到 `model_config.toml` 的模型名。
        keyword: 过滤关键字（模型名或 id 子串，大小写不敏感）。

    Returns:
        str: 简体中文多行文本。
    """

    registered_set = {name.lower() for name in registered}
    needle = keyword.strip().lower()
    rows: list[str] = []
    for model in catalog:
        if needle and needle not in model.name.lower() and needle not in model.model_identifier.lower():
            continue
        mark = "已注册" if model.name.lower() in registered_set else "未注册"
        flags = [_VISION_MARK] if model.visual else []
        price = f"${model.price_in:g}/${model.price_out:g}"
        detail = "｜".join(
            part
            for part in (
                model.model_identifier,
                model.display_name or "",
                model.context or "",
                price,
                "/".join(flags),
                mark,
            )
            if part
        )
        rows.append(f"- {model.name}（{detail}）")
    header = f"CommandCode Go 模型：{len(catalog)} 条"
    if needle:
        header += f"（过滤：{keyword}）"
    if not rows:
        return header + "\n（没有匹配的模型）"
    return "\n".join([header, *rows])

"""降级：复用 Host 已配置的官方模型。

报告 §6.7 的结论：降级不配置 key、不配置 base_url，直接通过 `ctx.llm.generate`
回调 Host，复用 MaiBot 自己的 `api_providers`（密钥、重试、计费统计全部复用）。

两个必须守住的坑（§6.7.4）：
1. **递归**：兜底只允许直选 `model_name`，并用 `contextvars` 记深度，深度 ≥1 直接拒绝；
2. **超时叠加**：兜底超时取"外层剩余预算"与配置值的较小者。
"""

from typing import Any

import contextvars
import logging

from .adapter_out import build_response, ResponseProtocol
from .config import FallbackConfig
from .errors import DeepEtherError, ErrorCode

_FALLBACK_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("deepether_fallback_depth", default=0)
_UNKNOWN_MODEL: str = "unknown"


def resolve_model_name(request: dict[str, Any]) -> str:
    """返回本次请求使用的模型名（`model_config.toml` 里的 `name`）。

    为什么用模型名而不是任务名：Host 序列化请求时**不携带任务名**
    （`model_info.model_type` 只有 `llm` / `embedding` / `image_embedding`，
    `utils_model._build_response_request` 也只透传 `model_info`），
    所以"按任务白名单降级"在插件侧无法实现。实践上给每个用途建一个模型条目
    （如 `deepether-replyer` / `deepether-utils`）即可精确控制。

    Args:
        request: Host 请求字典。

    Returns:
        str: 模型名；取不到时返回 `unknown`。
    """

    model_info = request.get("model_info")
    if isinstance(model_info, dict):
        for key in ("name", "model_identifier"):
            value = model_info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return _UNKNOWN_MODEL


def model_allowed(model_name: str, allowed_models: list[str]) -> bool:
    """判断该模型是否允许降级。

    Args:
        model_name: 当前请求使用的模型名。
        allowed_models: 允许降级的模型名列表；**空列表表示全部允许**。

    Returns:
        bool: 是否允许降级。
    """

    if not allowed_models:
        return True
    return model_name in allowed_models


class FallbackRunner:
    """把请求转交给 Host 已配置的模型。"""

    def __init__(self, *, llm: Any, logger: Any) -> None:
        """初始化降级执行器。

        Args:
            llm: `ctx.llm` 能力代理。
            logger: 插件日志器。
        """

        self._llm = llm
        self._logger = logger or logging.getLogger("deepether.fallback")

    async def run(
        self,
        *,
        messages: list[dict[str, str]],
        request: dict[str, Any],
        config: FallbackConfig,
        protocol: ResponseProtocol,
        remaining_ms: int,
        reason: str,
    ) -> dict[str, Any]:
        """执行一次降级调用。

        Args:
            messages: 已投影好的消息列表（复用 adapter_in，不另写一套）。
            request: 原始 Host 请求（用于取 tool_options / max_tokens）。
            config: 降级配置。
            protocol: 返回协议。
            remaining_ms: 外层剩余预算（毫秒）。
            reason: 中文降级原因，写日志用。

        Returns:
            dict[str, Any]: 与网页版通道同构的响应字典。

        Raises:
            DeepEtherError: 未配置降级、递归过深或 Host 模型调用失败时抛出。
        """

        if not config.fallback_enabled:
            raise DeepEtherError(
                ErrorCode.FALLBACK_UNAVAILABLE,
                "网页版不可用，且配置里禁用了自动降级",
                detail=reason,
            )
        model_name = (config.fallback_model_name or "").strip()
        if not model_name:
            raise DeepEtherError(
                ErrorCode.FALLBACK_UNAVAILABLE,
                "网页版不可用，且没有配置 fallback.fallback_model_name，无法降级",
                detail=reason,
            )
        if self._llm is None:
            raise DeepEtherError(
                ErrorCode.FALLBACK_UNAVAILABLE,
                "网页版不可用，且当前运行环境没有 llm.generate 能力（检查 manifest capabilities）",
                detail=reason,
            )

        depth = _FALLBACK_DEPTH.get()
        if depth >= 1:
            raise DeepEtherError(
                ErrorCode.FALLBACK_UNAVAILABLE,
                "检测到降级递归（兜底模型又指回了本插件），已中止",
                detail=f"depth={depth} model={model_name}",
            )

        timeout_ms = int(config.fallback_timeout_ms)
        if remaining_ms > 0:
            timeout_ms = max(1000, min(timeout_ms, remaining_ms))

        max_tokens = request.get("max_tokens")
        tool_options = request.get("tool_options") or []
        use_tools = bool(config.fallback_with_tools) and bool(tool_options)

        self._logger.warning(
            "已降级到 Host 模型 %s（原因：%s；超时 %d ms；携带工具=%s）",
            model_name,
            reason,
            timeout_ms,
            "是" if use_tools else "否",
        )

        token = _FALLBACK_DEPTH.set(depth + 1)
        try:
            if use_tools:
                result = await self._llm.generate_with_tools(
                    prompt=messages,
                    tools=tool_options,
                    model_name=model_name,
                    max_tokens=max_tokens,
                    timeout_ms=timeout_ms,
                )
            else:
                result = await self._llm.generate(
                    prompt=messages,
                    model_name=model_name,
                    max_tokens=max_tokens,
                    timeout_ms=timeout_ms,
                )
        finally:
            _FALLBACK_DEPTH.reset(token)

        if not isinstance(result, dict) or not result.get("success", False):
            message = ""
            if isinstance(result, dict):
                message = str(result.get("error") or result.get("response") or "")
            raise DeepEtherError(
                ErrorCode.FALLBACK_UNAVAILABLE,
                "降级调用 Host 模型失败，请检查 model_config.toml 里的 fallback 模型是否可用",
                detail=f"{message[:200]}",
            )

        content = result.get("response")
        reasoning = result.get("reasoning")
        if not isinstance(content, str):
            content = ""
        if not isinstance(reasoning, str):
            reasoning = ""

        self._logger.info(
            "降级调用成功：模型=%s，正文 %d 字，思考 %d 字",
            result.get("model_name") or model_name,
            len(content),
            len(reasoning),
        )
        return build_response(
            protocol,
            content=content,
            reasoning=reasoning,
            total_tokens=int(result.get("total_tokens") or 0),
            raw_data={
                "stage": "fallback",
                "fallback_model": result.get("model_name") or model_name,
                "reason": reason,
                "used_tools": use_tools,
            },
        )

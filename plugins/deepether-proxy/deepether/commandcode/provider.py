"""CommandCode Provider：把 Host 请求翻译成 `/alpha/generate` 调用。

与 DeepSeek 网页版通道完全隔离：独立的 Key、并发闸门、熔断器、节奏与预算；
任何失败都抛带中文说明的 `DeepEtherError`，由 Host 按任务 `model_list` 切到下一个模型。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from maibot_sdk import LLMProviderBase

from ..errors import DeepEtherError, ErrorCode
from ..gate import CircuitBreaker, ConcurrencyGate
from .adapter_in import CcPrompt, build_prompt
from .adapter_out import build_response, tool_args_preview
from .budget import BudgetLimits, BudgetTracker
from .catalog import CcModel, ModelCatalog, load_catalog
from .client import CommandCodeClient
from .constants import (
    DEFAULT_CLI_VERSION,
    DEFAULT_MAX_HISTORY_CHARS,
    DEFAULT_MAX_IMAGE_BYTES,
    DEFAULT_MAX_TOOL_OUTPUT_CHARS,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROJECT_SLUG,
    DEFAULT_REQUEST_TIMEOUT_SEC,
    DEFAULT_THREAD_REUSE_CALLS,
    MAX_OUTPUT_TOKENS,
    MAX_PAUSE_TURN_ROUNDS,
    PAUSE_FINISH_REASONS,
)
from .pacing import CommandCodePacer
from .protocol import build_envelope, detect_environment, normalize_finish_reason
from .state import CcRuntimeState

_DEFAULT_OUTER_TIMEOUT_SEC: float = 120.0
_BUDGET_STATE_FILE: str = "commandcode_budget.json"
_AUTH_FILE_NAME: str = "auth.json"


def _cfg(config: Any) -> Any:
    """取 `[commandcode]` 配置节；旧配置缺节时返回 None，由调用方用默认值兜住。"""

    return getattr(config, "commandcode", None)


def _get(section: Any, name: str, default: Any) -> Any:
    """安全读取配置项。"""

    if section is None:
        return default
    value = getattr(section, name, default)
    return default if value is None else value


class CommandCodeProvider(LLMProviderBase):
    """CommandCode（Go 套餐）Provider。"""

    def __init__(self, plugin: Any) -> None:
        """初始化 Provider。

        Args:
            plugin: 插件实例（提供 `config`、`get_logger()`、`ctx.paths.runtime_dir`）。
        """

        self._plugin = plugin
        self._client: CommandCodeClient | None = None
        self._client_signature: tuple[str, str, str, bool, float] | None = None
        self._gate: ConcurrencyGate | None = None
        self._gate_signature: tuple[int, float] | None = None
        self._circuit: CircuitBreaker | None = None
        self._circuit_signature: tuple[int, float] | None = None
        self._pacer: CommandCodePacer | None = None
        self._pacer_signature: tuple[float, int] | None = None
        self._budget: BudgetTracker | None = None
        self._budget_signature: tuple[float, float, float, float] | None = None
        self._catalog: ModelCatalog | None = None
        self._state = CcRuntimeState()
        self._thread_id = ""
        self._thread_calls = 0
        self._threads: dict[str, tuple[str, int]] = {}
        self._last_model = ""
        self._key_cache = ""

    # ─── 基础设施 ───────────────────────────────────────────────────────

    @property
    def _config(self) -> Any:
        """返回插件强类型配置。"""

        return self._plugin.config

    @property
    def _section(self) -> Any:
        """返回 `[commandcode]` 配置节。"""

        return _cfg(self._config)

    @property
    def _logger(self) -> Any:
        """返回插件日志器。"""

        return self._plugin.get_logger()

    def _runtime_dir(self) -> Path | None:
        """返回运行期可写目录（不可用时返回 None）。"""

        try:
            return Path(self._plugin.ctx.paths.runtime_dir)
        except Exception:  # noqa: BLE001
            return None

    @property
    def state(self) -> CcRuntimeState:
        """返回本通道的运行统计。"""

        return self._state

    @property
    def catalog(self) -> ModelCatalog:
        """返回模型目录（懒加载）。"""

        if self._catalog is None:
            self._catalog = load_catalog()
        return self._catalog

    @property
    def circuit_state(self) -> str:
        """返回熔断器状态。"""

        return self._circuit.state if self._circuit is not None else "closed"

    @property
    def gate_status(self) -> tuple[int, int]:
        """返回 `(进行中, 并发上限)`。"""

        if self._gate is None:
            return (0, int(_get(self._section, "max_concurrency", 2)))
        return (self._gate.in_flight, self._gate.limit)

    @property
    def gate_rejected(self) -> int:
        """返回被闸门拒绝的次数。"""

        return self._gate.rejected if self._gate is not None else 0

    def capacity_block_reason(self) -> str | None:
        """返回「CommandCode 现在没有容量」的中文原因；有容量返回 None。只读。"""

        if not bool(_get(self._section, "enabled", True)):
            return "CommandCode 通道已关闭"
        try:
            budget = self._ensure_budget()
        except Exception:  # noqa: BLE001
            budget = None
        if budget is not None:
            remaining = budget.remaining()
            usable = remaining - max(0.0, budget.limits.reserve)
            if usable <= 0:
                return f"CommandCode 预算已用尽（剩余 ${remaining:.2f}）"
        circuit = self._circuit
        if circuit is not None and circuit.state == "open":
            return "CommandCode 连续失败，熔断冷却中"
        return None

    @property
    def budget_snapshot(self) -> dict[str, Any]:
        """返回预算快照（供状态命令）。"""

        try:
            budget = self._ensure_budget()
        except Exception:  # noqa: BLE001 - 状态展示失败不应影响命令
            return {}
        return budget.snapshot() if budget is not None else {}

    @property
    def last_model(self) -> str:
        """返回最近一次请求的模型 id。"""

        return self._last_model

    @property
    def thread_id(self) -> str:
        """返回当前 threadId（状态展示用）。"""

        return self._thread_id

    # ─── Key ────────────────────────────────────────────────────────────

    def _auth_file_key(self) -> str:
        """按 `auto_import_auth_file` 读取官方 CLI 的 auth.json。"""

        if not bool(_get(self._section, "auto_import_auth_file", False)):
            return ""
        base = os.environ.get("COMMANDCODE_CONFIG_DIR", "").strip()
        root = Path(base) if base else Path.home() / ".commandcode"
        path = root / _AUTH_FILE_NAME
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        if not isinstance(raw, dict):
            return ""
        for key in ("apiKey", "api_key", "apiKeyValue"):
            value = raw.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _api_key(self, request: dict[str, Any]) -> str:
        """按优先级解析 Key：配置 → 环境变量 → Host 服务商 → auth.json。"""

        configured = str(_get(self._section, "api_key", "") or "").strip()
        if configured:
            return configured
        env = os.environ.get("COMMAND_CODE_API_KEY", "").strip()
        if env:
            return env
        api_provider = request.get("api_provider")
        if isinstance(api_provider, dict):
            from_host = str(api_provider.get("api_key") or "").strip()
            if from_host:
                return from_host
        return self._auth_file_key()

    # ─── 客户端 / 闸门 / 预算 ───────────────────────────────────────────

    def _ensure_client(self) -> CommandCodeClient:
        """按当前配置构造或复用客户端。"""

        section = self._section
        signature = (
            str(_get(section, "base_url", "https://api.commandcode.ai") or ""),
            str(_get(section, "cli_version", DEFAULT_CLI_VERSION) or DEFAULT_CLI_VERSION),
            str(_get(section, "project_slug", DEFAULT_PROJECT_SLUG) or DEFAULT_PROJECT_SLUG),
            bool(_get(section, "taste_learning", False)),
            float(_get(section, "request_timeout_sec", DEFAULT_REQUEST_TIMEOUT_SEC)),
        )
        if self._client is not None and self._client_signature != signature:
            # 连接池保留，只刷新可变项；base_url 变了则要重建。
            old_base = self._client_signature[0] if self._client_signature else ""
            if signature[0] != old_base:
                stale = self._client
                self._client = None
                try:  # 旧连接池异步关闭，避免换 base_url 后泄漏
                    asyncio.get_running_loop().create_task(stale.aclose())
                except RuntimeError:
                    pass
            else:
                self._client.update_options(
                    cli_version=signature[1],
                    project_slug=signature[2],
                    taste_learning=signature[3],
                    timeout_sec=signature[4],
                )
        if self._client is None:
            self._client = CommandCodeClient(
                api_key_provider=lambda: self._current_key,
                logger=self._logger,
                base_url=signature[0],
                cli_version=signature[1],
                project_slug=signature[2],
                taste_learning=signature[3],
                timeout_sec=signature[4],
            )
        self._client_signature = signature
        return self._client

    @property
    def _current_key(self) -> str:
        """返回最近一次请求解析出的 Key（供客户端回调）。"""

        return self._key_cache

    async def aclose(self) -> None:
        """关闭连接池。"""

        client = self._client
        self._client = None
        self._client_signature = None
        if client is not None:
            await client.aclose()

    def _ensure_resilience(self) -> None:
        """按配置构造/更新并发闸门与熔断器（与本通道绑定）。

        并发/排队优先读 `[commandcode]`（通道自己的旋钮）；熔断阈值没有单独配置项，
        沿用 `[concurrency]`——它是进程级的失败判定口径，两个通道各有一个实例即可。
        """

        config = self._config
        concurrency = getattr(config, "concurrency", None)
        section = self._section
        max_concurrency = int(
            _get(section, "max_concurrency", _get(concurrency, "max_concurrency", 2))
        )
        queue_wait = float(_get(section, "queue_wait_sec", _get(concurrency, "queue_wait_sec", 8)))
        gate_signature = (max_concurrency, queue_wait)
        if self._gate is None:
            self._gate = ConcurrencyGate(max_concurrency, queue_wait, label="CommandCode")
            self._gate_signature = gate_signature
        elif self._gate_signature != gate_signature:
            self._gate.reconfigure(max_concurrency, queue_wait, label="CommandCode")
            self._gate_signature = gate_signature

        fail_threshold = int(_get(concurrency, "circuit_fail_threshold", 3))
        cooldown = float(_get(concurrency, "circuit_cooldown_sec", 60))
        circuit_signature = (fail_threshold, cooldown)
        if self._circuit is None:
            self._circuit = CircuitBreaker(fail_threshold=fail_threshold, cooldown_sec=cooldown)
            self._circuit_signature = circuit_signature
        elif self._circuit_signature != circuit_signature:
            self._circuit.reconfigure(fail_threshold, cooldown)
            self._circuit_signature = circuit_signature

    def _ensure_pacer(self) -> CommandCodePacer:
        """按配置构造/更新节奏控制器。"""

        min_interval = float(_get(self._section, "min_interval_sec", 0.0))
        per_minute = int(_get(self._section, "max_per_minute", 6))
        signature = (min_interval, per_minute)
        if self._pacer is None:
            self._pacer = CommandCodePacer(min_interval_sec=min_interval, max_per_minute=per_minute)
        elif self._pacer_signature != signature:
            self._pacer.reconfigure(min_interval_sec=min_interval, max_per_minute=per_minute)
        self._pacer_signature = signature
        return self._pacer

    def _ensure_budget(self) -> BudgetTracker:
        """按配置构造/更新预算跟踪器（窗口/上限变化时重建，保留历史记录）。"""

        limits = BudgetLimits(
            five_hour=float(_get(self._section, "five_hour_budget_usd", 3.0)),
            weekly=float(_get(self._section, "weekly_budget_usd", 6.0)),
            monthly=float(_get(self._section, "monthly_budget_usd", 9.0)),
            reserve=float(_get(self._section, "budget_reserve_usd", 0.5)),
        )
        signature = (limits.five_hour, limits.weekly, limits.monthly, limits.reserve)
        if self._budget is not None and self._budget_signature == signature:
            return self._budget
        state_path: Path | None = None
        if bool(_get(self._section, "persist_budget", True)):
            runtime_dir = self._runtime_dir()
            state_path = runtime_dir / _BUDGET_STATE_FILE if runtime_dir is not None else None
        previous = self._budget
        budget = BudgetTracker(limits, state_path=state_path)
        budget.load()
        if previous is not None:
            self._logger.info("CommandCode 预算上限变化，预算跟踪器已重建")
        self._budget = budget
        self._budget_signature = signature
        return budget

    # ─── 对外入口 ───────────────────────────────────────────────────────

    async def get_response(self, request: dict[str, Any]) -> dict[str, Any]:
        """处理 `operation="response"`。"""

        try:
            return await self._respond(request)
        except DeepEtherError as exc:
            self._state.record_failure(exc.code, exc.message)
            self._logger.warning("CommandCode 调用失败（原因：%s: %s）", exc.code, exc.message)
            raise
        except Exception as exc:  # noqa: BLE001 - 兜住一切异常，避免 Host 只看到"没回话"
            error = DeepEtherError(
                ErrorCode.INTERNAL,
                "CommandCode 通道内部异常，请把日志里的这一条发给开发者",
                detail=f"{type(exc).__name__}: {exc}",
            )
            self._state.record_failure(error.code, error.message)
            self._logger.warning("CommandCode 通道内部异常：%s", error.detail)
            raise error from exc
        finally:
            circuit = self._circuit
            if circuit is not None and circuit.probe_in_flight:
                circuit.abort_probe()

    async def get_embedding(self, request: dict[str, Any]) -> dict[str, Any]:
        """CommandCode 没有 embedding 能力。"""

        raise DeepEtherError(
            ErrorCode.NOT_SUPPORTED,
            "CommandCode 没有 embedding 接口，请把 embedding 任务指向官方模型",
        )

    async def get_audio_transcriptions(self, request: dict[str, Any]) -> dict[str, Any]:
        """CommandCode 没有语音转写能力。"""

        raise DeepEtherError(
            ErrorCode.NOT_SUPPORTED,
            "CommandCode 没有语音转写能力，请把 voice 任务指向官方模型",
        )

    # ─── 实现 ───────────────────────────────────────────────────────────

    async def _respond(self, request: dict[str, Any]) -> dict[str, Any]:
        """完整链路：配置 → 投影 → 预算/闸门/节奏 → 上游 → 预算落账。"""

        section = self._section
        if section is None or not bool(_get(section, "enabled", True)):
            raise DeepEtherError(
                ErrorCode.CHANNEL_DISABLED,
                "CommandCode 通道已关闭（插件配置 [commandcode] enabled=false）",
            )

        api_key = self._api_key(request)
        self._key_cache = api_key
        if not api_key:
            raise DeepEtherError(
                ErrorCode.CREDENTIAL_MISSING,
                "尚未配置 CommandCode Key：请在插件配置 [commandcode] api_key 填入 Studio 生成的 Key",
            )

        model_info = request.get("model_info")
        if not isinstance(model_info, dict):
            model_info = {}
        entry = self._resolve_model(model_info)
        model_id = entry.model_identifier if entry is not None else str(model_info.get("model_identifier") or "").strip()
        if not model_id:
            raise DeepEtherError(
                ErrorCode.PROTOCOL_ERROR,
                "Host 请求里没有 model_identifier，无法确定要调用哪个 CommandCode 模型",
            )
        self._last_model = model_id
        vision = bool((entry.visual if entry is not None else False) or model_info.get("visual"))

        prompt = build_prompt(
            request.get("context_items"),
            tool_options=request.get("tool_options"),
            response_format=request.get("response_format"),
            vision=vision,
            vision_mode=str(_get(section, "vision_mode", "strip")),
            tool_mode=str(_get(section, "tool_mode", "auto")),
            max_history_chars=int(_get(section, "max_history_chars", DEFAULT_MAX_HISTORY_CHARS)),
            max_tool_output_chars=int(
                _get(section, "max_tool_output_chars", DEFAULT_MAX_TOOL_OUTPUT_CHARS)
            ),
            max_image_bytes=int(_get(section, "max_image_bytes", DEFAULT_MAX_IMAGE_BYTES)),
        )

        max_tokens = self._max_tokens(model_info, section)
        self._ensure_resilience()
        budget = self._ensure_budget()
        budget.check(self._estimate_cost(entry, prompt, max_tokens))
        assert self._gate is not None and self._circuit is not None
        if not self._circuit.allow():
            raise DeepEtherError(
                ErrorCode.CIRCUIT_OPEN,
                f"CommandCode 连续失败 {self._circuit.consecutive_failures} 次，熔断冷却中",
            )

        budget_ms = self._outer_budget_ms(request)
        pacer = self._ensure_pacer()
        waited = await pacer.acquire(budget_sec=max(0.0, budget_ms / 1000.0 - 2.0))
        if waited > 0:
            self._logger.info("CommandCode 节奏控制：等待 %.1f 秒后调用上游", waited)

        started = time.monotonic()
        try:
            async with self._gate:
                result = await self._call_upstream(
                    request=request,
                    prompt=prompt,
                    model_id=model_id,
                    max_tokens=max_tokens,
                    budget_ms=budget_ms,
                    section=section,
                )
        except DeepEtherError as exc:
            if exc.code not in (
                ErrorCode.CONCURRENCY_REJECTED,
                ErrorCode.PACING_QUOTA,
                ErrorCode.BUDGET_EXHAUSTED,
                ErrorCode.CREDENTIAL_MISSING,
            ):
                self._circuit.record_failure()
            raise

        self._circuit.record_success()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        cost = self._actual_cost(entry, result)
        budget.record(cost, model_id)
        budget.persist()
        self._state.record_success(elapsed_ms)
        self._logger.info(
            "CommandCode 调用成功：模型=%s 耗时 %d ms 正文 %d 字 / 思考 %d 字 / 工具 %d 个 / "
            "tokens=%d+%d 成本≈$%.4f",
            model_id,
            elapsed_ms,
            len(result.content),
            len(result.reasoning),
            len(result.tool_calls),
            result.usage.input_tokens,
            result.usage.output_tokens,
            cost,
        )
        for call in result.tool_calls:
            self._logger.info("工具调用：%s(%s)", call.name, tool_args_preview(call))

        return build_response(
            result,
            model_name=str(model_info.get("name") or model_id),
            elapsed_ms=elapsed_ms,
            dropped_items=prompt.dropped_items,
            images_sent=prompt.images_sent,
            images_stripped=prompt.images_stripped,
            tool_outputs_truncated=prompt.tool_outputs_truncated,
            history_truncated=prompt.history_truncated,
        )

    def _resolve_model(self, model_info: dict[str, Any]) -> CcModel | None:
        """按 `model_identifier`（精确）或模型名解析目录条目。"""

        catalog = self.catalog
        identifier = str(model_info.get("model_identifier") or "").strip()
        entry = catalog.by_identifier(identifier)
        if entry is not None:
            return entry
        name = str(model_info.get("name") or "").strip()
        return catalog.by_name(name) if name else None

    def _max_tokens(self, model_info: dict[str, Any], section: Any) -> int:
        """决定本次 `max_tokens`：模型级 > 全局配置 > 默认值，并夹在网关允许范围内。"""

        extra = self._model_extra_params(model_info)
        raw = extra.get("cc_max_tokens", model_info.get("max_tokens"))
        try:
            value = int(raw) if raw is not None else int(_get(section, "max_tokens", DEFAULT_MAX_TOKENS))
        except (TypeError, ValueError):
            value = DEFAULT_MAX_TOKENS
        return max(1, min(value, MAX_OUTPUT_TOKENS))

    @staticmethod
    def _model_extra_params(model_info: dict[str, Any]) -> dict[str, Any]:
        """取模型级 extra_params。"""

        extra = model_info.get("extra_params")
        return extra if isinstance(extra, dict) else {}

    def _reasoning_effort(self, model_info: dict[str, Any], section: Any, entry: CcModel | None) -> str:
        """决定 `reasoning_effort`：模型级 `cc_effort` > 全局默认；模型不支持则不传。"""

        effort = str(
            self._model_extra_params(model_info).get("cc_effort")
            or _get(section, "default_effort", "")
            or ""
        ).strip()
        if not effort:
            return ""
        if entry is not None and entry.efforts and effort not in entry.efforts:
            self._logger.debug(
                "模型 %s 不支持 reasoning_effort=%s（可选：%s），本次不传",
                entry.model_identifier,
                effort,
                ",".join(entry.efforts),
            )
            return ""
        return effort

    def _temperature(self, request: dict[str, Any], model_info: dict[str, Any]) -> float | None:
        """只有模型配置显式开启 `send_temperature` 时才传温度。"""

        if not bool(model_info.get("send_temperature", False)):
            return None
        value = request.get("temperature", model_info.get("temperature"))
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _next_thread_id(self, section: Any, model_id: str) -> str:
        """按模型复用 threadId（同 thread 才能吃到 prompt cache）。"""

        reuse = max(1, int(_get(section, "thread_reuse_calls", DEFAULT_THREAD_REUSE_CALLS)))
        thread_id, calls = self._threads.get(model_id, ("", 0))
        if not thread_id or calls >= reuse:
            thread_id = str(uuid.uuid4())
            calls = 0
        calls += 1
        self._threads[model_id] = (thread_id, calls)
        self._thread_id = thread_id
        self._thread_calls = calls
        return thread_id

    async def _call_upstream(
        self,
        *,
        request: dict[str, Any],
        prompt: CcPrompt,
        model_id: str,
        max_tokens: int,
        budget_ms: int,
        section: Any,
    ) -> Any:
        """调用 `/alpha/generate`，处理 `pause_turn` 续跑。"""

        client = self._ensure_client()
        model_info = request.get("model_info")
        if not isinstance(model_info, dict):
            model_info = {}
        entry = self.catalog.by_identifier(model_id)
        working_dir = str(_get(section, "working_dir", "") or "").strip()
        if not working_dir:
            runtime_dir = self._runtime_dir()
            working_dir = str(runtime_dir) if runtime_dir is not None else "."
        environment = str(_get(section, "environment", "") or "").strip() or detect_environment()
        timeout_sec = float(_get(section, "request_timeout_sec", DEFAULT_REQUEST_TIMEOUT_SEC))
        if budget_ms > 0:
            timeout_sec = max(5.0, min(timeout_sec, budget_ms / 1000.0 - 1.5))

        envelope = build_envelope(
            model=model_id,
            messages=prompt.messages,
            system=prompt.system,
            max_tokens=max_tokens,
            thread_id=self._next_thread_id(section, model_id),
            working_dir=working_dir,
            environment=environment,
            temperature=self._temperature(request, model_info),
            reasoning_effort=self._reasoning_effort(model_info, section, entry),
            tools=prompt.tools,
        )
        result = await client.generate(envelope, timeout_sec=timeout_sec)

        rounds = 1
        while normalize_finish_reason(result.finish_reason) in PAUSE_FINISH_REASONS and rounds <= MAX_PAUSE_TURN_ROUNDS:
            continuation = self._continuation_envelope(envelope, result)
            if continuation is None:
                break
            self._logger.info("CommandCode 返回 pause_turn，按协议续跑第 %d 次", rounds)
            extra = await client.generate(continuation, timeout_sec=timeout_sec)
            result.merge(extra)
            envelope = continuation
            rounds += 1
        return result

    @staticmethod
    def _continuation_envelope(envelope: dict[str, Any], result: Any) -> dict[str, Any] | None:
        """把已经产出的半截结果追加进 messages，用于 `pause_turn` 续跑。"""

        blocks: list[dict[str, Any]] = []
        if result.reasoning:
            blocks.append({"type": "reasoning", "text": result.reasoning})
        if result.content:
            blocks.append({"type": "text", "text": result.content})
        for call in result.tool_calls:
            blocks.append(
                {
                    "type": "tool-call",
                    "toolCallId": call.call_id,
                    "toolName": call.name,
                    "input": call.arguments,
                }
            )
        if not blocks:
            return None
        params = dict(envelope.get("params") or {})
        messages = list(params.get("messages") or [])
        messages.append({"role": "assistant", "content": blocks})
        params["messages"] = messages
        return {**envelope, "params": params}

    def _estimate_cost(self, entry: CcModel | None, prompt: CcPrompt, max_tokens: int) -> float:
        """请求前预估成本（便宜模型几乎为 0，只用于"额度不够就别发"的前置判断）。"""

        if entry is None:
            return 0.0
        estimated_input = max(1, prompt.message_chars // 2)
        estimated_output = min(max_tokens, 1024)
        return entry.cost_usd(input_tokens=estimated_input, output_tokens=estimated_output)

    def _actual_cost(self, entry: CcModel | None, result: Any) -> float:
        """按上游 usage 核算实际成本。"""

        if entry is None:
            return 0.0
        usage = result.usage
        return entry.cost_usd(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )

    @staticmethod
    def _outer_budget_ms(request: dict[str, Any]) -> int:
        """估算 Host 给本次调用留的时间（毫秒）。"""

        api_provider = request.get("api_provider")
        timeout_sec = _DEFAULT_OUTER_TIMEOUT_SEC
        if isinstance(api_provider, dict):
            try:
                timeout_sec = float(api_provider.get("timeout") or _DEFAULT_OUTER_TIMEOUT_SEC)
            except (TypeError, ValueError):
                timeout_sec = _DEFAULT_OUTER_TIMEOUT_SEC
        return max(0, int(timeout_sec * 1000) - 1500)

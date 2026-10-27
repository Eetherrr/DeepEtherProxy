"""LLM Provider 实现：把 Host 请求翻译成网页版调用。

本类不直接依赖 MaiBot 主程序，只依赖 `maibot_sdk` 的 `LLMProviderBase`，
其余协作对象（配置、日志、凭证、状态）通过插件实例按鸭子类型获取，便于单测替换。

请求链路：
```
context_items → adapter_in → 并发闸门 → 熔断检查 → 建会话 → PoW → completion(SSE)
                            ↘ 任一环节失败/超预算 → 抛错，由 Host 按任务 model_list 切换下一个模型
```

插件只回答"网页版这条通道能不能给出有效正文"：给不出就抛错。它就是一个普通
LLM Provider——失败与其它模型失败没有区别，由 MaiBot 的模型调度按任务配置处理。
"""

from pathlib import Path
from typing import Any

import hashlib
import json
import time

from maibot_sdk import LLMProviderBase

from .adapter_in import RenderedContext, build_messages, style_suffix
from .antiban import PacingConfig, PacingController, classify_failure, parse_model_limits
from .adapter_out import build_response
from .config import DeepEtherProxyConfig
from .errors import DeepEtherError, ErrorCode
from .gate import CircuitBreaker, ConcurrencyGate
from .provision import detect_model_config, read_model_providers, read_task_models, settings_from_config
from .upstream.web_client import WebUpstreamClient

_DUMP_REQUEST_FILE_TEMPLATE: str = "last_request_{model}.json"
_DEFAULT_OUTER_TIMEOUT_SEC: float = 120.0
_DEFAULT_SESSION_REUSE_CALLS: int = 8
_PACING_STATE_FILE: str = "antiban_state.json"
_MAX_ACCOUNT_ATTEMPTS: int = 3


def _antiban_section(config: Any) -> Any:
    """取 [antiban] 配置节。

    旧配置文件里可能还没有这一节（升级前生成的 config.toml），此时返回 None，
    调用方用 getattr(..., 默认值) 兜住，保证老配置不会因为缺节而炸掉。

    Args:
        config: 当前强类型配置。

    Returns:
        Any: [antiban] 配置节，或 None。
    """

    return getattr(config, "antiban", None)


def _split_csv(spec: Any) -> tuple[str, ...]:
    """把逗号分隔的配置值拆成元组（去空白、丢空项）。"""

    return tuple(item.strip() for item in str(spec or "").split(",") if item.strip())


class DeepEtherProvider(LLMProviderBase):
    """DeepEther Proxy 的 Provider 编排层。

    Attributes:
        _plugin: 宿主插件实例；需要提供 `config` 属性、`get_logger()`、
            `credential_store`、`runtime_state` 与 `ctx.paths`。
    """

    def __init__(self, plugin: Any) -> None:
        """初始化 Provider。

        Args:
            plugin: 插件实例（由 plugin.py 传入，按鸭子类型使用）。
        """

        self._plugin = plugin
        self._clients: dict[str, WebUpstreamClient] = {}
        self._gate: ConcurrencyGate | None = None
        self._gate_signature: tuple[int, float] | None = None
        self._circuits: dict[str, CircuitBreaker] = {}
        self._circuit_signature: tuple[int, float] | None = None
        self._pacings: dict[str, PacingController] = {}
        self._pacing_config: PacingConfig | None = None
        #: 每个账号最近一次成功使用的时间（LRU 轮换用）
        self._last_used: dict[str, float] = {}
        #: replyer 让位目标缓存：(单调时间, [(模型, 服务商)], {模型: 服务商})
        self._models_cache: tuple[float, list[tuple[str, str]], dict[str, str]] | None = None
        #: 上一次让位原因（用于状态跃迁时只打一条日志）
        self._last_yield_reason: str = ""

    # ─── 基础设施 ───────────────────────────────────────────────────────

    @property
    def _config(self) -> DeepEtherProxyConfig:
        """返回当前强类型配置。"""

        return self._plugin.config  # type: ignore[no-any-return]

    @property
    def _logger(self) -> Any:
        """返回插件日志器。"""

        return self._plugin.get_logger()

    def _state(self) -> Any:
        """返回运行时统计对象。"""

        return self._plugin.runtime_state

    def _runtime_dir(self) -> Path | None:
        """返回插件运行期可写目录（不可用时返回 None）。"""

        try:
            return Path(self._plugin.ctx.paths.runtime_dir)
        except Exception:  # noqa: BLE001
            return None

    def _configured_tokens(self) -> list[str]:
        """返回可用的网页版 token 列表：配置数组优先，其次书签落盘的单条（去重、保序）。"""

        raw = list(getattr(self._config.credential, "web_tokens", []) or [])
        tokens = [str(item).strip() for item in raw if str(item).strip()]
        if not tokens:
            store = getattr(self._plugin, "credential_store", None)
            record = getattr(store, "record", None) if store is not None else None
            legacy = str(getattr(record, "token", "") or "").strip()
            if legacy:
                tokens = [legacy]
        unique: list[str] = []
        seen: set[str] = set()
        for token in tokens:
            if token not in seen:
                seen.add(token)
                unique.append(token)
        return unique

    def _effective_token(self) -> str:
        """返回生效的网页版 token（取列表第一条，兼容旧调用点）。"""

        tokens = self._configured_tokens()
        return tokens[0] if tokens else ""

    @staticmethod
    def _account_id(token: str) -> str:
        """账号标识：token 的 sha1 前 8 位（日志与状态里不出现明文）。"""

        return hashlib.sha1(token.encode("utf-8")).hexdigest()[:8] if token else "default"

    def _mark_credential_invalid(self, reason: str, account_id: str = "") -> None:
        """把某个账号的凭证标记为失效（多账号时只影响该账号）。"""

        store = getattr(self._plugin, "credential_store", None)
        if store is None:
            return
        if account_id and hasattr(store, "mark_invalid_account"):
            store.mark_invalid_account(account_id, reason)
        else:
            store.mark_invalid(reason)

    def _ensure_client(self, token: str | None = None) -> WebUpstreamClient:
        """按账号构造/复用上游客户端（每个 token 一个，会话与连接池互不干扰）。"""

        legacy = getattr(self, "_client", None)
        if legacy is not None:
            # 测试与外部注入用：固定返回同一个客户端
            legacy.update_runtime_options(
                dump_dir=self._runtime_dir() if self._config.debug.dump_response else None,
                verbose=self._config.debug.verbose_log,
            )
            return legacy

        config = self._config
        chosen = token if token is not None else self._effective_token()
        account = self._account_id(chosen)
        dump_dir = self._runtime_dir() if config.debug.dump_response else None
        antiban = _antiban_section(config)
        reuse_calls = int(getattr(antiban, "session_reuse_calls", _DEFAULT_SESSION_REUSE_CALLS))
        client = self._clients.get(account)
        if client is None:
            client = WebUpstreamClient(
                token_provider=lambda current=chosen: current,
                on_credential_invalid=lambda reason, acc=account: self._mark_credential_invalid(reason, acc),
                logger=self._logger,
                dump_dir=dump_dir,
                verbose=config.debug.verbose_log,
            )
            self._clients[account] = client
        client.update_runtime_options(
            dump_dir=dump_dir,
            verbose=config.debug.verbose_log,
            session_reuse_calls=reuse_calls,
        )
        return client

    def _ensure_resilience(self) -> None:
        """按当前配置构造/更新并发闸门与熔断器。"""

        config = self._config
        gate_signature = (config.concurrency.max_concurrency, float(config.concurrency.queue_wait_sec))
        if self._gate is None:
            self._gate = ConcurrencyGate(*gate_signature)
            self._gate_signature = gate_signature
        elif self._gate_signature != gate_signature:
            self._gate.reconfigure(*gate_signature)
            self._gate_signature = gate_signature

        self._circuit_signature = (
            config.concurrency.circuit_fail_threshold,
            float(config.concurrency.circuit_cooldown_sec),
        )

    def _circuit_for(self, account_id: str) -> CircuitBreaker:
        """返回某个账号的熔断器（按账号隔离，A 号失败不会连坐 B 号）。"""

        legacy = getattr(self, "_circuit", None)
        if legacy is not None:
            return legacy
        circuit = self._circuits.get(account_id)
        signature = (
            self._config.concurrency.circuit_fail_threshold,
            float(self._config.concurrency.circuit_cooldown_sec),
        )
        if circuit is None:
            circuit = CircuitBreaker(
                fail_threshold=int(signature[0]),
                cooldown_sec=float(signature[1]),
            )
            self._circuits[account_id] = circuit
        elif self._circuit_signature != signature:
            circuit.reconfigure(int(signature[0]), float(signature[1]))
        return circuit

    def _pacing_state_path(self, account_id: str = "") -> Path | None:
        """返回某个账号节奏状态的落盘路径（单账号沿用旧文件名）。"""

        if not bool(getattr(self._config.antiban, "persist_state", True)):
            return None
        runtime_dir = self._runtime_dir()
        if runtime_dir is None:
            return None
        if len(self._configured_tokens()) <= 1 or not account_id:
            return runtime_dir / _PACING_STATE_FILE
        return runtime_dir / f"antiban_state_{account_id}.json"

    def _load_pacing_state(self, pacing: PacingController, account_id: str = "") -> None:
        """从磁盘恢复某个账号的配额与冷却状态。"""

        path = self._pacing_state_path(account_id)
        if path is None or not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._logger.warning("读取节奏状态失败（按空状态继续）：%s", exc)
            return
        pacing.load_state(data)
        self._logger.info("已恢复上游节奏状态：%s", path)

    def _persist_pacing(self, pacing: PacingController, account_id: str = "") -> None:
        """把某个账号的节奏状态落盘（失败只告警，不影响本次请求）。"""

        path = self._pacing_state_path(account_id)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(pacing.dump_state(), ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError as exc:
            self._logger.warning("写入节奏状态失败：%s", exc)

    def _ensure_pacing(self, account_id: str = "") -> PacingController:
        """按当前配置构造/更新某个账号的节奏控制器。"""

        tokens = self._configured_tokens()
        account = account_id or (self._account_id(tokens[0]) if tokens else "default")
        section = self._config.antiban
        wanted = PacingConfig(
            enabled=bool(section.enabled),
            min_interval_sec=float(section.min_interval_sec),
            max_interval_sec=float(section.max_interval_sec),
            jitter_ratio=float(section.jitter_ratio),
            max_per_minute=int(section.max_per_minute),
            max_per_hour=int(section.max_per_hour),
            max_per_day=int(section.max_per_day),
            cooldown_on_rate_limit_sec=float(section.cooldown_on_rate_limit_sec),
            cooldown_on_ban_sec=float(section.cooldown_on_ban_sec),
            adaptive_backoff=bool(section.adaptive_backoff),
            adaptive_budget=bool(getattr(section, "adaptive_budget", True)),
            min_adaptive_budget=int(getattr(section, "min_adaptive_budget", 80)),
            quiet_hours=str(getattr(section, "quiet_hours", "") or ""),
            quiet_hours_ratio=float(getattr(section, "quiet_hours_ratio", 1.0)),
            quota_wait_max_sec=float(getattr(section, "quota_wait_max_sec", 0.0)),
            model_daily_limits=parse_model_limits(getattr(section, "model_daily_limits", "")),
            reserve_daily_budget=int(getattr(section, "reserve_daily_budget", 0)),
            reserve_models=_split_csv(getattr(section, "reserve_models", "")),
        )
        current = self._pacings.get(account)
        if current is None or self._pacing_config != wanted:
            was_running = current is not None
            current = PacingController(wanted)
            self._pacings[account] = current
            self._pacing_config = wanted
            self._load_pacing_state(current, account if account != "default" else "")
            if was_running:
                self._logger.info("反封禁配置变化，节奏控制器已重建")
        return current

    @property
    def pacing_snapshot(self) -> dict[str, Any]:
        """返回节奏控制状态（供 /deepether-status 展示）。"""

        tokens = self._configured_tokens()
        if not tokens:
            return {}
        return self._ensure_pacing(self._account_id(tokens[0])).snapshot()

    # ─── 额度让位（replyer hook 用；只读，不发请求、不睡眠） ───────────

    _MODELS_CACHE_TTL_SEC: float = 60.0

    def capacity_block_reason(self) -> str | None:
        """返回「网页版现在没有容量」的中文原因；有容量返回 None。"""

        tokens = self._configured_tokens()
        if not tokens:
            return None
        reasons: list[str] = []
        for token in tokens:
            account = self._account_id(token)
            try:
                snapshot = self._ensure_pacing(account).snapshot()
            except Exception:  # noqa: BLE001 - 判断失败不应打断回复
                return None
            if snapshot.get("in_cooldown"):
                reasons.append(str(snapshot.get("cooldown_reason") or "冷却中"))
                continue
            limits = snapshot.get("limits") or {}
            day_limit = int(limits.get("per_day") or 0)
            if day_limit > 0 and int(snapshot.get("used_last_day") or 0) >= day_limit:
                reasons.append(f"日额度已用完（{day_limit} 次）")
                continue
            hour_limit = int(limits.get("per_hour") or 0)
            if hour_limit > 0 and int(snapshot.get("used_last_hour") or 0) >= hour_limit:
                reasons.append(f"小时额度已用完（{hour_limit} 次）")
                continue
            circuit = self._circuits.get(account)
            if circuit is not None and circuit.state == "open":
                reasons.append("连续失败熔断中")
                continue
            return None
        return reasons[0] if reasons else "没有可用账号"

    def replyer_yield_target(self) -> str | None:
        """额度不够时 replyer 本轮应改用的模型名；不需要让位或找不到目标时返回 None。"""

        reason = self.capacity_block_reason()
        if reason is None:
            self._note_yield_end()
            return None
        target = self._resolve_fallback_model()
        if not target:
            return None
        self._last_yield_reason = reason
        return target

    def invalidate_model_cache(self) -> None:
        """配置或 model_config.toml 变化后清掉模型缓存（下一次 hook 重新读取）。"""

        self._models_cache = None

    def note_yield(self, target: str) -> None:
        """记录一次让位（状态跃迁时只打一条 info，避免每轮刷屏）。"""

        reason = self._last_yield_reason or "网页版没有容量"
        marker = f"{reason}|{target}"
        if marker == getattr(self, "_yield_marker", ""):
            return
        self._yield_marker = marker
        self._logger.info("网页版暂时没有容量（%s），本轮 replyer 改走 %s", reason, target)

    def _note_yield_end(self) -> None:
        """容量恢复时清掉让位标记，下次耗尽会重新记一条。"""

        if getattr(self, "_yield_marker", ""):
            self._yield_marker = ""
            self._last_yield_reason = ""
            self._logger.info("网页版容量已恢复，replyer 回到网页版优先")

    def _models_now(self) -> tuple[list[tuple[str, str]], dict[str, str]]:
        """读取（并缓存 60s）replyer 的 model_list 与全体模型的服务商映射。"""

        now = time.monotonic()
        cached = self._models_cache
        if cached is not None and now - cached[0] < self._MODELS_CACHE_TTL_SEC:
            return cached[1], cached[2]
        settings = settings_from_config(self._config)
        plugin_dir = Path(__file__).resolve().parents[1]
        path, _candidates = detect_model_config(settings=settings, plugin_dir=plugin_dir)
        task_models = read_task_models(path, "replyer") if path is not None else []
        providers = read_model_providers(path) if path is not None else {}
        self._models_cache = (now, task_models, providers)
        return task_models, providers

    def next_official_model(self) -> str | None:
        """返回 replyer 列表里第一个非本插件的模型（供 hook 让位使用）。"""

        return self._resolve_fallback_model()

    def first_replyer_model(self) -> tuple[str, str] | None:
        """返回 replyer 任务 model_list 的第一个模型 (名字, 服务商)；读不到返回 None。"""

        task_models, providers = self._models_now()
        for name, provider in task_models:
            if name in providers:
                return name, provider
        return None

    def _resolve_fallback_model(self) -> str | None:
        """返回 replyer 任务列表里、位于本插件之后的第一个非本插件模型。

        插件不持有「兜底模型」这个概念：用哪个模型完全由你自己的 model_list 顺序决定，
        这里只是把「本轮别用我」翻译成列表里的下一个模型名。列表里没有别的模型时返回 None。
        """

        task_models, providers = self._models_now()
        our_provider = settings_from_config(self._config).provider_name
        for name, provider in task_models:
            if name in providers and provider and provider != our_provider:
                return name
        return None

    def ensure_client(self) -> WebUpstreamClient:
        """返回（必要时创建）上游客户端，供自检与定时探活使用。"""

        return self._ensure_client()

    async def aclose(self) -> None:
        """关闭所有账号的上游连接池。"""

        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.close()

    @property
    def circuit_state(self) -> str:
        """返回熔断器状态（供状态命令展示）。"""

        legacy = getattr(self, "_circuit", None)
        if legacy is not None:
            return legacy.state
        tokens = self._configured_tokens()
        if not tokens:
            return "closed"
        return self._circuit_for(self._account_id(tokens[0])).state

    @property
    def gate_rejected(self) -> int:
        """返回因排队超预算被闸门拒绝的请求数（供状态命令展示）。"""

        return self._gate.rejected if self._gate is not None else 0

    @property
    def gate_status(self) -> tuple[int, int]:
        """返回 `(进行中, 并发上限)`，供状态命令展示。"""

        if self._gate is None:
            # 还没有请求时闸门尚未按配置创建，直接回落到配置值，避免状态里显示 "0/?"。
            return (0, self._config.concurrency.max_concurrency)
        return (self._gate.in_flight, self._gate.limit)

    # ─── 对外入口 ───────────────────────────────────────────────────────

    async def get_response(self, request: dict[str, Any]) -> dict[str, Any]:
        """处理 `operation="response"` 的文本生成请求。

        Args:
            request: Host 序列化后的响应请求，字段见报告 §2.3。

        Returns:
            dict[str, Any]: 一期为旧标量协议响应，二期可切 output_items。

        Raises:
            DeepEtherError: 网页版不可用时抛出带中文说明的错误（与其它模型失败一样，
                由 MaiBot 的模型调度处理）。
        """

        try:
            return await self._request_web(request)
        except DeepEtherError as exc:
            self._state().record_failure(exc.code, exc.message)
            # 只记录"这条通道这次失败"这一事实；后续调度由 MaiBot 负责。
            self._logger.warning("网页版调用失败（原因：%s: %s）", exc.code, exc.message)
            raise
        except Exception as exc:  # noqa: BLE001 - 兜住一切异常，避免 Host 只看到"没回话"
            error = DeepEtherError(
                ErrorCode.INTERNAL,
                "插件内部异常，请把日志里的这一条发给开发者",
                detail=f"{type(exc).__name__}: {exc}",
            )
            self._state().record_failure(error.code, error.message)
            self._logger.warning("网页版通道内部异常：%s", error.detail)
            raise error from exc
        finally:
            self._release_dangling_probe()

    def _release_dangling_probe(self) -> None:
        """请求没有正常结束（例如被 Host 取消）时归还 half_open 探针名额。"""

        for circuit in list(self._circuits.values()):
            if circuit.probe_in_flight:
                circuit.abort_probe()

    async def get_embedding(self, request: dict[str, Any]) -> dict[str, Any]:
        """网页版没有 embedding 接口。

        Raises:
            DeepEtherError: 总是抛出，提示改用官方模型。
        """

        raise DeepEtherError(
            ErrorCode.NOT_SUPPORTED,
            "DeepSeek 网页版没有 embedding 能力，请把 embedding 任务指向官方模型",
        )

    async def get_audio_transcriptions(self, request: dict[str, Any]) -> dict[str, Any]:
        """网页版没有语音转写接口。

        Raises:
            DeepEtherError: 总是抛出，提示改用官方模型。
        """

        raise DeepEtherError(
            ErrorCode.NOT_SUPPORTED,
            "DeepSeek 网页版没有语音转写能力，请把 voice 任务指向官方模型",
        )

    # ─── 实际实现 ───────────────────────────────────────────────────────

    def _ordered_accounts(self, tokens: list[str]) -> list[str]:
        """按「不在熔断中 → 最久未用」排序；第一个就是本次优先使用的账号。"""

        def sort_key(token: str) -> tuple[int, float]:
            account = self._account_id(token)
            circuit = self._circuits.get(account)
            cooling = 1 if circuit is not None and circuit.state == "open" else 0
            return (cooling, self._last_used.get(account, 0.0))

        return sorted(tokens, key=sort_key)

    def _request_model_name(self, request: dict[str, Any]) -> str:
        """返回本次请求使用的模型名（model_config.toml 里的 name）。

        反封禁的三个旋钮（按模型配额 / 保留额度 / 用量统计）都按模型名区分：
        Host 请求里不带任务名，但带 model_info.name，正好够用。

        Args:
            request: Host 请求字典。

        Returns:
            str: 模型名；取不到时返回空串（按最低优先级处理）。
        """

        model_info = request.get("model_info")
        if isinstance(model_info, dict):
            for key in ("name", "model_identifier"):
                value = model_info.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return ""

    async def _request_web(self, request: dict[str, Any]) -> dict[str, Any]:
        """完整链路：投影 → 闸门/熔断 → 上游；任一步失败都直接抛错。"""

        config = self._config

        if config.behavior.mode == "official_only":
            # 这条通道整体关闭：即使请求仍被送进来（旧配置还留着接线），也直接失败，
            # 避免继续拿"疑似封号"的账号发请求。放在投影之前，连 prompt 都不渲染。
            raise DeepEtherError(
                ErrorCode.CHANNEL_DISABLED,
                "通道配置为 official_only，本插件不接管网页版调用",
            )

        rendered = self._render_context(request)
        self._maybe_dump_request(request)
        budget_ms = self._outer_budget_ms(request, config)
        started = time.monotonic()

        tokens = self._configured_tokens()
        if not tokens:
            raise DeepEtherError(
                ErrorCode.CREDENTIAL_MISSING,
                "尚未配置网页版凭证，请在插件配置的 credential.web_tokens 填入至少一个 token",
            )

        self._ensure_resilience()
        assert self._gate is not None
        model_name = self._request_model_name(request)

        # 多账号：按「不在熔断中 + 最久未用」排序，逐个尝试；只在额度/冷却/凭证失效时换下一个账号。
        # 其它错误不轮换（避免把一次协议问题放大成多账号连撞）。
        last_error: DeepEtherError | None = None
        for token in self._ordered_accounts(tokens)[: _MAX_ACCOUNT_ATTEMPTS]:
            account = self._account_id(token)
            circuit = self._circuit_for(account)
            if not circuit.allow():
                last_error = DeepEtherError(
                    ErrorCode.CIRCUIT_OPEN,
                    f"网页版连续失败 {circuit.consecutive_failures} 次，熔断冷却中",
                )
                continue

            pacing = self._ensure_pacing(account)
            try:
                waited = await pacing.acquire(budget_ms=budget_ms, model_name=model_name)
            except DeepEtherError as exc:
                # 额度用完 / 冷却中：换下一个账号（不等待、也不在同一个账号上重试）
                last_error = exc
                continue
            if waited > 0:
                self._logger.info("节奏控制：等待 %.1f 秒后调用上游（账号 %s）", waited, account)

            try:
                async with self._gate:
                    snapshot = await self._call_web(
                        config, rendered, request, budget_ms, self._ensure_client(token)
                    )
            except DeepEtherError as exc:
                # 排队超预算 = 本地容量不足，不能算作熔断器的"上游连续失败"。
                if exc.code != ErrorCode.CONCURRENCY_REJECTED:
                    circuit.record_failure()
                try:
                    kind = classify_failure(
                        status=getattr(exc, "status", None),
                        code=exc.code,
                        message=exc.message,
                        body=exc.detail,
                    )
                except Exception:  # noqa: BLE001 - 分类失败不应放大成请求失败
                    kind = None
                if kind is not None:
                    pacing.note_failure(kind)
                    self._persist_pacing(pacing, account)
                    self._logger.warning("记录风控信号 %s（账号 %s，后续退避已生效）", kind, account)
                if exc.code == ErrorCode.CREDENTIAL_INVALID and len(tokens) > 1:
                    # 某个账号的 token 失效：只标记它，换下一个账号继续
                    self._mark_credential_invalid(exc.message, account)
                    last_error = exc
                    continue
                raise

            circuit.record_success()
            pacing.note_success()
            self._persist_pacing(pacing, account)
            self._last_used[account] = time.monotonic()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            self._state().record_success(elapsed_ms)
            self._state().record_probe(True, "最近一次调用成功")
            self._logger.info(
                "网页版调用成功：耗时 %d ms，正文 %d 字 / 思考 %d 字，上下文 %d 条（丢弃 %d 条，账号 %s）",
                elapsed_ms,
                len(snapshot.content),
                len(snapshot.thinking),
                len(rendered.messages),
                rendered.dropped_items,
                account,
            )
            return self._build_web_response(config, snapshot, rendered, elapsed_ms)

        if last_error is not None:
            raise last_error
        raise DeepEtherError(
            ErrorCode.CREDENTIAL_MISSING,
            "没有可用的网页版账号（全部在冷却或熔断中）",
        )

    async def _call_web(
        self,
        config: DeepEtherProxyConfig,
        rendered: RenderedContext,
        request: dict[str, Any],
        budget_ms: int,
        client: WebUpstreamClient,
    ) -> Any:
        """建会话 + 求解 PoW + 读完 SSE（使用指定账号的客户端）。"""

        timeout_sec = float(config.behavior.request_timeout_sec)
        if budget_ms > 0:
            timeout_sec = max(5.0, min(timeout_sec, budget_ms / 1000.0))
        antiban = _antiban_section(config)
        reuse_calls = int(getattr(antiban, "session_reuse_calls", _DEFAULT_SESSION_REUSE_CALLS))
        # 复用同一个会话（而不是每轮新建）：既是降低封禁风险的措施（docs/design/ANTIBAN.md 措施 4），
        # 也省掉每轮一次 chat_session/create 往返。
        session_id = await client.acquire_session(reuse_limit=reuse_calls)
        try:
            snapshot = await client.complete(
                prompt=rendered.text,
                session_id=session_id,
                thinking_enabled=self._thinking_enabled(config, request),
                search_enabled=config.behavior.search_enabled,
                model_type=config.behavior.model_type,
                timeout_sec=timeout_sec,
            )
        except DeepEtherError:
            # 失败的会话可能已经"半截"（上游有记录但没给完内容），直接换新会更像正常用户。
            client.invalidate_session()
            raise
        # 报告 §4.5.3 细节 1/2：空响应（含"只有思考、没有正文"）必须判失败，
        # 不能把空正文当成功返回，否则用户会收到一条空回复。
        if not snapshot.content.strip():
            raise DeepEtherError(
                ErrorCode.UPSTREAM_EMPTY,
                (
                    "网页版只返回了思考内容、没有正文" if snapshot.thinking.strip()
                    else "网页版没有返回任何正文，可能是上游协议变更或触发风控"
                ),
                detail=(
                    f"events={snapshot.events} content_fragments={snapshot.content_fragments} "
                    f"thinking_fragments={snapshot.thinking_fragments} "
                    f"ignored={snapshot.ignored_values} parse_errors={snapshot.parse_errors} "
                    f"thinking_len={len(snapshot.thinking)}"
                ),
                retryable=True,
            )
        return snapshot

    def _build_web_response(
        self,
        config: DeepEtherProxyConfig,
        snapshot: Any,
        rendered: RenderedContext,
        elapsed_ms: int,
    ) -> dict[str, Any]:
        """把上游快照转成 Host 响应。"""

        return build_response(
            config.behavior.response_protocol,
            content=snapshot.content,
            reasoning=snapshot.thinking,
            total_tokens=snapshot.total_tokens,
            raw_data={
                "stage": "web",
                "elapsed_ms": elapsed_ms,
                "content_fragments": snapshot.content_fragments,
                "thinking_fragments": snapshot.thinking_fragments,
                "events": snapshot.events,
                "ignored_values": snapshot.ignored_values,
                "parse_errors": snapshot.parse_errors,
                "dropped_items": rendered.dropped_items,
                "unknown_item_types": rendered.unknown_types,
            },
        )

    def _outer_budget_ms(self, request: dict[str, Any], config: DeepEtherProxyConfig) -> int:
        """估算外层（Host）给本次调用留下的时间预算。"""

        api_provider = request.get("api_provider")
        timeout_sec: float = _DEFAULT_OUTER_TIMEOUT_SEC
        if isinstance(api_provider, dict):
            try:
                timeout_sec = float(api_provider.get("timeout") or _DEFAULT_OUTER_TIMEOUT_SEC)
            except (TypeError, ValueError):
                timeout_sec = _DEFAULT_OUTER_TIMEOUT_SEC
        if timeout_sec <= 0:
            timeout_sec = float(config.behavior.request_timeout_sec)
        # 留 1.5s 余量给回程序列化与 Host 侧处理。
        return max(0, int(timeout_sec * 1000) - 1500)

    def _model_extra_params(self, request: dict[str, Any]) -> dict[str, Any]:
        """返回本次请求对应模型在 `model_config.toml` 里配置的 `extra_params`。

        Args:
            request: Host 请求字典。

        Returns:
            dict[str, Any]: 模型级额外参数（缺失时为空字典）。
        """

        extra = request.get("extra_params")
        if isinstance(extra, dict):
            return extra
        model_info = request.get("model_info")
        if isinstance(model_info, dict):
            nested = model_info.get("extra_params")
            if isinstance(nested, dict):
                return nested
        return {}

    def _prompt_style(self, request: dict[str, Any]) -> str:
        """决定本次请求的提示词风格：模型 extra_params 覆盖优先，其次全局配置。

        Args:
            request: Host 请求字典。

        Returns:
            str: `plain` / `replyer` / `raw`。
        """

        style = self._model_extra_params(request).get("deepether_style")
        if isinstance(style, str) and style in ("plain", "replyer", "raw"):
            return style
        return self._config.behavior.prompt_style

    def _thinking_enabled(self, config: DeepEtherProxyConfig, request: dict[str, Any]) -> bool:
        """决定本次请求是否开启深度思考（模型级 `deepether_thinking` 可覆盖）。

        Args:
            config: 当前配置。
            request: Host 请求字典。

        Returns:
            bool: 是否开启思考模式。
        """

        override = self._model_extra_params(request).get("deepether_thinking")
        if isinstance(override, bool):
            return override
        return config.behavior.thinking_enabled

    def _render_context(self, request: dict[str, Any]) -> RenderedContext:
        """把请求里的 `context_items` 投影成 prompt。"""

        style = self._prompt_style(request)
        rendered = build_messages(
            request.get("context_items"),
            max_history_chars=self._config.behavior.max_history_chars,
            prompt_suffix=style_suffix(style),
        )
        if not rendered.messages:
            raise DeepEtherError(
                ErrorCode.PROTOCOL_ERROR,
                "Host 请求里没有可用的上下文内容，无法生成 prompt",
                detail=f"context_items 数量={len(request.get('context_items') or [])}",
            )
        if rendered.unknown_types:
            self._logger.warning(
                "上下文里出现未知的 item_type（可能是宿主新增类型）：%s",
                ", ".join(rendered.unknown_types),
            )
        return rendered

    def _maybe_dump_request(self, request: dict[str, Any]) -> None:
        """按配置把原始请求落盘，便于固化契约测试样本。"""

        if not self._config.debug.dump_request:
            return
        runtime_dir = self._runtime_dir()
        if runtime_dir is None:
            return
        model_info = request.get("model_info")
        raw_model = str((model_info or {}).get("name") or "unknown") if isinstance(model_info, dict) else "unknown"
        safe_model = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in raw_model) or "unknown"
        try:
            runtime_dir.mkdir(parents=True, exist_ok=True)
            path = runtime_dir / _DUMP_REQUEST_FILE_TEMPLATE.format(model=safe_model)
            path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")
            self._logger.info("已把 Host 原始请求写入 %s", path)
        except (OSError, TypeError, ValueError) as exc:
            self._logger.warning("写入 Host 请求快照失败：%s", exc)

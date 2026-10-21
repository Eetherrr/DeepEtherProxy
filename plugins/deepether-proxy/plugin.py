"""DeepEther Proxy 插件入口。

把 DeepSeek 网页版包装成 MaiBot 的一个 LLM Provider（client_type = `com.ether.deepether`）。

设计约束（见 docs/FEASIBILITY.md）：
1. 只通过 `@LLMProvider` 暴露一个 client_type，不启动任何对公网服务；
2. 凭证接收服务只绑 127.0.0.1，日志中只出现脱敏 token；
3. 所有异常都在插件内兜住并转成中文说明，避免 Host 侧只表现为"麦麦不回话"；
4. 业务逻辑全部放在 `deepether/` 包里，可脱离 MaiBot 单测。
"""

from pathlib import Path
from typing import Any

import logging
import secrets

from maibot_sdk import Command, LLMProvider, MaiBotPlugin

from .deepether.bookmarklet import build_bookmarklet
from .deepether.config import DeepEtherProxyConfig
from .deepether.credential import SOURCE_BOOKMARKLET, SOURCE_CONFIG, CredentialStore
from .deepether.errors import mask_secret
from .deepether.loopback import CredentialLoopbackServer
from .deepether.provider import DeepEtherProvider
from .deepether.provision import ProvisionResult, provision_model_config, settings_from_config
from .deepether.selftest import PeriodicProbe
from .deepether.state import RuntimeState
from .deepether.status import render_status

CLIENT_TYPE: str = "com.ether.deepether"
PROVIDER_VERSION: str = "0.2.4"
NONCE_FILE_NAME: str = "nonce.txt"
BOOKMARKLET_FILE_NAME: str = "bookmarklet.txt"


class DeepEtherProxyPlugin(MaiBotPlugin):
    """DeepEther Proxy 插件主类。"""

    config_model = DeepEtherProxyConfig

    def __init__(self) -> None:
        """初始化插件状态。"""

        super().__init__()
        self._provider: DeepEtherProvider | None = None
        self._store: CredentialStore | None = None
        self._state: RuntimeState = RuntimeState()
        self._loopback: CredentialLoopbackServer | None = None
        self._nonce: str = ""
        self._data_dir: Path | None = None
        self._loopback_port: int = 0
        self._loopback_origin: str = ""
        self._probe: PeriodicProbe | None = None
        self._provision: ProvisionResult | None = None

    def get_logger(self) -> logging.Logger:
        """返回插件日志器；上下文尚未注入时回退到模块级日志器。"""

        try:
            return self.ctx.logger
        except RuntimeError:
            return logging.getLogger("deepether.proxy")

    # ─── 生命周期 ───────────────────────────────────────────────────────

    async def on_load(self) -> None:
        """插件激活后初始化运行时组件。

        这里刻意不抛异常：插件加载失败只会让 Host 把请求静默回退到下一个模型，
        用户很难定位问题。任何初始化失败都记录状态、写中文日志，让故障保持可见。
        """

        logger = self.get_logger()
        try:
            config = self.config
        except RuntimeError as exc:
            logger.warning("DeepEther Proxy 配置尚未注入，跳过初始化：%s", exc)
            return

        self._provider = DeepEtherProvider(self)
        self._state = RuntimeState()

        try:
            self._data_dir = self.ctx.paths.data_dir
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 - 目录不可用时降级为"仅内存"
            logger.error("插件数据目录不可用，凭证将无法落盘：%s", exc)
            self._data_dir = None

        if self._data_dir is not None:
            self._store = CredentialStore(self._data_dir, logger)
            self._store.load()
            self._nonce = self._load_or_create_nonce(self._data_dir, logger)

        self._sync_config_token(config, logger)
        await self._start_loopback(config, logger)
        self._write_bookmarklet_file(config, logger)
        self._start_periodic_probe(config, logger)

        logger.info(
            "DeepEther Proxy 已加载：通道=%s / 思考=%s / 联网=%s / 并发上限=%d / 凭证=%s",
            config.behavior.mode,
            "开" if config.behavior.thinking_enabled else "关",
            "开" if config.behavior.search_enabled else "关",
            config.concurrency.max_concurrency,
            self._describe_credential(),
        )

        self.run_provision()

    async def on_unload(self) -> None:
        """插件卸载前释放资源。"""

        logger = self.get_logger()
        if self._probe is not None:
            await self._probe.stop()
            self._probe = None
        if self._loopback is not None:
            await self._loopback.stop()
            self._loopback = None
        if self._provider is not None:
            await self._provider.aclose()
        self._provider = None
        logger.info("DeepEther Proxy 已卸载，Provider 已注销、凭证接收服务已停止")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """处理配置热更新。

        Args:
            scope: 配置变更范围（`self` / `bot` / `model`）。
            config_data: 最新配置数据。
            version: 配置版本号。
        """

        logger = self.get_logger()
        if scope != "self":
            logger.debug("DeepEther Proxy 收到非自身范围配置更新：scope=%s version=%s", scope, version)
            return

        try:
            config = self.config
        except RuntimeError as exc:
            logger.warning("DeepEther Proxy 配置更新后仍不可用：%s", exc)
            return

        self._sync_config_token(config, logger)

        need_restart = (
            config.credential.credential_port != self._loopback_port
            or config.credential.allowed_origin != self._loopback_origin
        )
        if need_restart and self._loopback is not None:
            await self._loopback.stop()
            self._loopback = None
        if self._loopback is None:
            await self._start_loopback(config, logger)

        if self._probe is not None:
            await self._probe.stop()
            self._probe = None
        self._start_periodic_probe(config, logger)
        self.run_provision()

        logger.info(
            "DeepEther Proxy 配置已更新：通道=%s / 并发上限=%d / 降级=%s / 凭证=%s",
            config.behavior.mode,
            config.concurrency.max_concurrency,
            "开" if config.fallback.fallback_enabled else "关",
            self._describe_credential(),
        )

    # ─── 凭证 ───────────────────────────────────────────────────────────

    @property
    def credential_store(self) -> CredentialStore | None:
        """返回凭证存储（供 Provider 使用）。"""

        return self._store

    @property
    def runtime_state(self) -> RuntimeState:
        """返回运行时统计。"""

        return self._state

    def _sync_config_token(self, config: DeepEtherProxyConfig, logger: logging.Logger) -> None:
        """把配置文件里的 token 同步进凭证存储。

        Args:
            config: 当前配置。
            logger: 插件日志器。
        """

        if self._store is None:
            return
        configured = config.credential.web_token.strip()
        if not configured:
            return
        record = self._store.record
        if record is not None and record.token == configured and not self._store.invalid_reason:
            return
        try:
            self._store.save(configured, SOURCE_CONFIG)
        except ValueError as exc:
            logger.warning("配置里的 web_token 无法保存：%s", exc)

    def _handle_incoming_token(self, token: str) -> bool:
        """处理书签小工具提交的 token。

        Args:
            token: 浏览器 localStorage 里的 userToken。

        Returns:
            bool: 是否成功接受并落盘。
        """

        logger = self.get_logger()
        if self._store is None:
            logger.error("收到凭证但插件数据目录不可用，无法落盘")
            return False
        try:
            self._store.save(token, SOURCE_BOOKMARKLET)
        except (ValueError, OSError) as exc:
            logger.error("保存书签提交的凭证失败：%s", exc)
            return False
        logger.info("已通过书签小工具接收网页版凭证，可以开始使用")
        # 启动时没有凭证就不会拉起定时探活（plugin.py 的 on_load 早有判断），
        # 因此这里是"先启动、后授权"场景下唯一能把探活补起来的地方，否则凭证
        # 过期不会按 health_check_interval_min 自动发现（报告 §6.6.3）。
        if self._probe is None:
            try:
                self._start_periodic_probe(self.config, logger)
            except RuntimeError as exc:
                logger.warning("书签授权后无法启动定时探活：%s", exc)
        return True

    def _describe_credential(self) -> str:
        """返回用于日志的凭证摘要（脱敏）。"""

        if self._store is None:
            return "不可用（数据目录异常）"
        record = self._store.record
        if record is None:
            return "未配置（等待书签送达）"
        return f"{record.masked}（{self._store.state}）"

    @staticmethod
    def _load_or_create_nonce(data_dir: Path, logger: logging.Logger) -> str:
        """读取或生成书签口令；口令落盘以便重启后书签继续有效。

        Args:
            data_dir: 插件数据目录。
            logger: 插件日志器。

        Returns:
            str: 当前的 nonce。
        """

        path = data_dir / NONCE_FILE_NAME
        try:
            if path.exists():
                existing = path.read_text(encoding="utf-8").strip()
                if existing:
                    return existing
            nonce = secrets.token_urlsafe(18)
            path.write_text(nonce, encoding="utf-8")
            logger.info("已生成新的书签口令（nonce），请用 /deepether-status 查看书签")
            return nonce
        except OSError as exc:
            logger.warning("读写书签口令失败，将使用本次启动的临时口令：%s", exc)
            return secrets.token_urlsafe(18)

    # ─── 回环服务 ───────────────────────────────────────────────────────

    async def _start_loopback(self, config: DeepEtherProxyConfig, logger: logging.Logger) -> None:
        """按配置启动凭证接收服务。

        Args:
            config: 当前配置。
            logger: 插件日志器。
        """

        if config.behavior.mode == "official_only":
            logger.info("当前通道为 official_only，不启动凭证接收服务")
            return

        if not config.credential.loopback_enabled:
            # 发布形态：不需要本地服务，token 由用户从浏览器控制台取出后填进插件配置（WebUI 可改）。
            logger.info(
                "未启用本地授权服务（默认关闭）。请在 WebUI 插件配置的 credential.web_token 填入网页版 token"
            )
            return

        server = CredentialLoopbackServer(
            port=config.credential.credential_port,
            allowed_origin=config.credential.allowed_origin,
            logger=logger,
            nonce_provider=lambda: self._nonce,
            on_token=self._handle_incoming_token,
        )
        try:
            await server.start()
        except Exception as exc:  # noqa: BLE001 - 端口占用也要让插件继续可用
            logger.error("凭证接收服务启动失败，书签小工具暂不可用：%s", exc)
            self._state.record_probe(False, f"凭证接收服务启动失败：{exc}")
            return

        self._loopback = server
        self._loopback_port = server.port
        self._loopback_origin = config.credential.allowed_origin

    def _write_bookmarklet_file(self, config: DeepEtherProxyConfig, logger: logging.Logger) -> None:
        """把书签小工具写到数据目录，方便用户直接复制（聊天窗口会折行，不方便全选）。

        Args:
            config: 当前配置。
            logger: 插件日志器。
        """

        if self._data_dir is None or not self._nonce:
            return
        if config.behavior.mode == "official_only" or not config.credential.loopback_enabled:
            return
        try:
            path = self._data_dir / BOOKMARKLET_FILE_NAME
            path.write_text(
                build_bookmarklet(config.credential.credential_port, self._nonce),
                encoding="utf-8",
            )
            logger.info("书签小工具已写入 %s（在已登录的 chat.deepseek.com 页面把它拖成书签后点一下）", path)
        except OSError as exc:
            logger.warning("写入书签小工具文件失败：%s", exc)

    def _start_periodic_probe(self, config: DeepEtherProxyConfig, logger: logging.Logger) -> None:
        """启动定时凭证探活。

        Args:
            config: 当前配置。
            logger: 插件日志器。
        """

        if config.behavior.mode == "official_only":
            return
        if self._store is None or self._store.record is None:
            logger.info("尚无网页版凭证，定时探活暂不启动（拿到凭证后请重载插件或发送 /deepether-status）")
            return

        self._probe = PeriodicProbe(
            client_factory=lambda: self._provider.ensure_client() if self._provider is not None else None,
            state=self._state,
            logger=logger,
            interval_sec=float(config.credential.health_check_interval_min) * 60.0,
        )
        try:
            self._probe.start()
            logger.info(
                "定时凭证探活已启动：每 %d 分钟一次",
                config.credential.health_check_interval_min,
            )
        except RuntimeError as exc:
            logger.warning("无法启动定时探活：%s", exc)

    # ─── 自动注册 model_config ──────────────────────────────────────────

    def run_provision(self, *, force: bool = False) -> ProvisionResult:
        """把 Provider 与三个预设模型写进 Host 的 model_config.toml。

        Args:
            force: 为真时忽略配置里的开关（供 /deepether-setup 手动重跑）。

        Returns:
            ProvisionResult: 本次结果，同时缓存供状态命令展示。
        """

        logger = self.get_logger()
        try:
            config = self.config
        except RuntimeError as exc:
            result = ProvisionResult(error="插件配置尚未注入，跳过自动注册：" + str(exc))
            self._provision = result
            return result

        section = getattr(config, "provision", None)
        if section is not None and not bool(getattr(section, "enabled", True)) and not force:
            result = ProvisionResult(ok=True)
            result.skipped.append("自动注册已在配置里关闭")
            self._provision = result
            return result

        try:
            settings = settings_from_config(config)
            result = provision_model_config(
                settings=settings,
                plugin_dir=Path(__file__).parent,
                logger=logger,
            )
        except Exception as exc:  # noqa: BLE001 - 自动注册失败绝不能影响 Provider 注册
            result = ProvisionResult(error="自动注册过程异常：" + str(exc))
            logger.warning("自动注册 model_config 异常：%s", exc)

        self._provision = result
        if not result.ok:
            logger.warning("自动注册 model_config 未完成：%s", result.error)
        elif result.changed:
            logger.info("自动注册 model_config 完成：%s", result.summary())
        else:
            logger.info("自动注册 model_config 无需改动：%s", result.summary())
        for warning in result.warnings:
            logger.info("自动注册提示：%s", warning)
        return result

    # ─── 状态命令 ───────────────────────────────────────────────────────

    @Command(
        "deepether-status",
        description="查看 DeepEther Proxy 的凭证、书签与降级统计",
        pattern=r"^/deepether-status$",
    )
    async def handle_status_command(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        """输出插件状态。

        Args:
            **kwargs: 宿主流入的命令上下文，包含 `stream_id` 等。

        Returns:
            tuple[bool, str | None, bool]: `(是否成功, 返回文本, 是否拦截后续消息)`。
        """

        text = self.build_status_text()
        stream_id = str(kwargs.get("stream_id") or "")
        if stream_id:
            try:
                await self.ctx.send.text(text, stream_id)
                return True, None, True
            except Exception as exc:  # noqa: BLE001 - 发不出去就退化成返回值
                self.get_logger().warning("状态文本发送失败，改为作为命令返回值输出：%s", exc)
        return True, text, True

    @Command(
        "deepether-setup",
        description="重新把 Provider 与预设模型写进 Host 的 model_config.toml",
        pattern=r"^/deepether-setup$",
    )
    async def handle_setup_command(self, **kwargs: Any) -> tuple[bool, str | None, bool]:
        """手动重跑自动注册，并回报结果。

        Args:
            **kwargs: 宿主流入的命令上下文，包含 `stream_id` 等。

        Returns:
            tuple[bool, str | None, bool]: `(是否成功, 返回文本, 是否拦截后续消息)`。
        """

        result = self.run_provision(force=True)
        text = "自动注册结果：\n" + result.summary()
        if not result.ok:
            text = text + "\n可手动指定 model_config_path，或在 WebUI 里手工新增模型与服务商。"
        stream_id = str(kwargs.get("stream_id") or "")
        if stream_id:
            try:
                await self.ctx.send.text(text, stream_id)
                return True, None, True
            except Exception as exc:  # noqa: BLE001 - 发不出去就退化成返回值
                self.get_logger().warning("自动注册结果发送失败，改为作为命令返回值输出：%s", exc)
        return True, text, True

    def build_status_text(self) -> str:
        """构造状态文本（同时供命令与单测使用）。"""

        logger = self.get_logger()
        try:
            config = self.config
        except RuntimeError:
            return "DeepEther Proxy 配置尚未注入，暂时无法读取状态"

        if self._store is None:
            return (
                "DeepEther Proxy 状态异常：插件数据目录不可用，凭证无法落盘。\n"
                f"通道={config.behavior.mode}｜端口={self._loopback_port}"
            )

        gate_in_flight, gate_limit = (
            self._provider.gate_status if self._provider is not None else (0, config.concurrency.max_concurrency)
        )
        return render_status(
            version=PROVIDER_VERSION,
            config=config,
            store=self._store,
            state=self._state,
            loopback_running=self._loopback is not None and self._loopback.running,
            loopback_enabled=config.credential.loopback_enabled,
            loopback_port=self._loopback_port or config.credential.credential_port,
            nonce=self._nonce,
            circuit_state=self._provider.circuit_state if self._provider is not None else "closed",
            gate_in_flight=gate_in_flight,
            gate_limit=gate_limit,
            gate_rejected=self._provider.gate_rejected if self._provider is not None else 0,
            pacing=self._provider.pacing_snapshot if self._provider is not None else None,
            provision=self._provision.summary() if self._provision is not None else None,
        )

    # ─── LLM Provider 入口 ──────────────────────────────────────────────

    @LLMProvider(
        CLIENT_TYPE,
        name="DeepEther Proxy",
        description="DeepSeek 网页版反代 Provider（网页优先、Host 模型兜底）",
        version=PROVIDER_VERSION,
    )
    async def handle_llm_request(self, operation: str, request: dict[str, Any]) -> dict[str, Any]:
        """处理 Host 转发来的 LLM 调用。

        Args:
            operation: 操作类型：`response` / `embedding` / `audio_transcription`。
            request: Host 序列化后的请求字典。

        Returns:
            dict[str, Any]: 可由 Host 恢复为 APIResponse 的响应字典。

        Raises:
            DeepEtherError: 无法给出结果时抛出带中文说明的异常。
        """

        provider = self._provider
        if provider is None:
            provider = DeepEtherProvider(self)
            self._provider = provider
        return await provider.dispatch(operation, request)


def create_plugin() -> MaiBotPlugin:
    """插件工厂函数，由 Runner 调用。"""

    return DeepEtherProxyPlugin()

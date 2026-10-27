"""把 Provider 与预设模型自动注册进 Host 的 model_config.toml。

插件加载时执行一次（可在配置里关闭），也可用 /deepether-setup 手动重跑。
Host 的配置查看器会监视 model_config.toml，写入后自动热重载，无需重启。

设计约束：
1. 只在能确认实例根时写入；确认不了就只告警，绝不猜测路径乱写；
2. 幂等：Provider 按 name / client_type 判重，模型按 name 判重，任务列表已含目标名则不重复插入；
3. 安全：先备份、再原子替换，写入前后都用 tomllib 复核；内容无变化时完全不落盘，
   避免触发 Host 的文件监视器反复热重载。
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

try:  # Host 自身依赖 tomlkit（WebUI 写配置也用它），缺失时降级为不自动写入
    import tomlkit
except ImportError:  # pragma: no cover - 仅覆盖解释器缺包时的兜底路径
    tomlkit = None  # type: ignore[assignment]

DEFAULT_BASE_URL: Final[str] = "plugin://com.ether.deepether"
DEFAULT_CLIENT_TYPE: Final[str] = "com.ether.deepether"
DEFAULT_TIMEOUT_SEC: Final[int] = 300
#: 让一次失败就轮到下一个模型：Host 会按 api_provider.max_retry 在同一模型上重试，
#: 默认值 3 意味着网页版失败后还要再撞两次（每次都可能再等 8~20s 反封禁间隔）。
DEFAULT_MAX_RETRY: Final[int] = 1
DEFAULT_MODEL_IDENTIFIER: Final[str] = "deepseek-web"
MAX_BACKUPS: Final[int] = 5
BACKUP_SUFFIX: Final[str] = ".deepether.bak"

LEGACY_PLANNER_MODEL: Final[str] = "deepether-planner"
"""0.3.0 及更早版本会自动注册这个模型；它无法承担 planner 的工具调用，升级时就地清理。"""

CC_CLIENT_TYPE: Final[str] = "com.ether.commandcode"
CC_BASE_URL: Final[str] = "plugin://com.ether.commandcode"
CC_PROVIDER_NAME: Final[str] = "commandcode"
CC_MODEL_PREFIX: Final[str] = "cc-"
"""CommandCode 通道：与 DeepSeek 通道并存，模型名统一 `cc-` 前缀。"""


@dataclass(frozen=True)
class CcProvisionSettings:
    """CommandCode 自动注册的可调项。

    只注册模型，不接任何任务：注册 ≠ 接线，具体模型放进哪个任务由用户在 WebUI 里决定。
    """

    enabled: bool = True
    provider_name: str = CC_PROVIDER_NAME
    model_config_path: str = ""
    register_models: str = "all"
    model_include: str = ""
    model_exclude: str = ""
    model_prefix: str = CC_MODEL_PREFIX
    keep_backup: bool = True


def cc_settings_from_config(config: Any) -> CcProvisionSettings:
    """从插件配置里读出 CommandCode 自动注册设置。"""

    section = getattr(config, "commandcode", None)
    provision = getattr(config, "provision", None)
    if section is None:
        return CcProvisionSettings(enabled=False)
    api_key = str(getattr(section, "api_key", "") or "").strip() or os.environ.get(
        "COMMAND_CODE_API_KEY", ""
    ).strip()
    prefix = str(getattr(section, "model_prefix", CC_MODEL_PREFIX) or CC_MODEL_PREFIX).strip()
    return CcProvisionSettings(
        enabled=bool(api_key),
        provider_name=str(getattr(section, "provider_name", "") or "").strip() or CC_PROVIDER_NAME,
        model_config_path=str(getattr(provision, "model_config_path", "") or "").strip(),
        register_models=str(getattr(section, "register_models", "all") or "all").strip().lower(),
        model_include=str(getattr(section, "model_include", "") or "").strip(),
        model_exclude=str(getattr(section, "model_exclude", "") or "").strip(),
        model_prefix=prefix or CC_MODEL_PREFIX,
        keep_backup=bool(getattr(provision, "keep_backup", True)),
    )


@dataclass(frozen=True)
class ModelPlan:
    """一个预设模型：名字、提示词风格、是否开思考。"""

    name: str
    style: str
    thinking: bool


DEFAULT_PLAN: Final[tuple[ModelPlan, ...]] = (
    ModelPlan(name="deepether-replyer", style="replyer", thinking=False),
    ModelPlan(name="deepether-utils", style="plain", thinking=False),
)
"""默认注册的模型（只注册，不接线）。

刻意**不注册** `deepether-planner`：planner 的命脉是 function calling，而网页版没有
工具调用能力；注册了只会让用户在 WebUI 里误把它配给 planner，每次先失败一次。
"""


@dataclass(frozen=True)
class ProvisionSettings:
    """自动注册的可调项，全部来自插件配置。"""

    provider_name: str = "deepether"
    model_config_path: str = ""
    keep_backup: bool = True


@dataclass
class ProvisionResult:
    """一次自动注册的结果，供日志与状态命令展示。"""

    ok: bool = False
    path: Path | None = None
    changed: bool = False
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def summary(self) -> str:
        """返回一句话中文摘要，用于日志与状态文本。"""

        if self.error:
            head = "自动注册未完成：" + self.error
        elif self.changed:
            head = "自动注册已写入 model_config.toml"
        else:
            head = "自动注册无需改动（配置已是最新）"
        parts = [head]
        if self.path is not None:
            parts.append("配置文件=" + str(self.path))
        if self.applied:
            parts.append("变更=" + "；".join(self.applied))
        if self.skipped:
            parts.append("跳过=" + "；".join(self.skipped))
        if self.warnings:
            parts.append("提示=" + "；".join(self.warnings))
        return " ｜ ".join(parts)


def settings_from_config(config: Any) -> ProvisionSettings:
    """从插件配置对象里读出自动注册设置；配置缺节时返回默认值。"""

    section = getattr(config, "provision", None)
    if section is None:
        return ProvisionSettings()
    return ProvisionSettings(
        provider_name=str(getattr(section, "provider_name", "") or "").strip() or "deepether",
        model_config_path=str(getattr(section, "model_config_path", "") or "").strip(),
        keep_backup=bool(getattr(section, "keep_backup", True)),
    )


def candidate_paths(*, explicit: str = "", plugin_dir: Path | None = None) -> list[Path]:
    """按可靠性顺序列出 model_config.toml 的候选位置（已去重）。"""

    cands: list[Path] = []

    def add(path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.expanduser()
        except (OSError, RuntimeError):
            return
        if resolved in cands:
            return
        cands.append(resolved)

    if explicit and explicit.strip():
        add(Path(explicit.strip()))
    env_file = os.environ.get("DEEPETHER_MODEL_CONFIG", "").strip()
    if env_file:
        add(Path(env_file))
    env_instance = os.environ.get("MAIBOT_INSTANCE_DIR", "").strip()
    if env_instance:
        add(Path(env_instance) / "config" / "model_config.toml")

    roots: list[Path] = []
    if plugin_dir is not None:
        roots.append(Path(plugin_dir))
    roots.append(Path(__file__).parent.parent)
    try:
        roots.append(Path.cwd())
    except OSError:  # pragma: no cover - cwd 已被删除时才会触发
        pass

    for root in roots:
        starts: list[Path] = [root]
        try:
            resolved_root = root.resolve()
        except (OSError, RuntimeError):
            resolved_root = root
        if resolved_root != root:
            starts.append(resolved_root)
        for start in starts:
            current = start
            for _ in range(5):
                add(current / "config" / "model_config.toml")
                if current.parent == current:
                    break
                current = current.parent
    return cands


def detect_model_config(*, settings: ProvisionSettings, plugin_dir: Path | None = None) -> tuple[Path | None, list[Path]]:
    """找到真实存在的 model_config.toml；返回（命中路径, 全部候选）。"""

    cands = candidate_paths(explicit=settings.model_config_path, plugin_dir=plugin_dir)
    for cand in cands:
        try:
            if cand.is_file():
                return cand, cands
        except OSError:
            continue
    return None, cands


def read_model_providers(path: Path) -> dict[str, str]:
    """读取 model_config.toml 里所有模型名 → 服务商名；读不到返回空字典。"""

    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {
        str(item.get("name") or ""): str(item.get("api_provider") or "")
        for item in parsed.get("models", [])
    }


def read_task_models(path: Path, task: str) -> list[tuple[str, str]]:
    """读取某个任务 `model_list` 里的模型名与服务商。

    Args:
        path: model_config.toml 路径。
        task: 任务名，例如 replyer。

    Returns:
        list[tuple[str, str]]: [(模型名, api_provider 名称), ...]，按 model_list 顺序；读不到返回空列表。
    """

    try:
        parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return []
    providers = {
        str(item.get("name") or ""): str(item.get("api_provider") or "")
        for item in parsed.get("models", [])
    }
    section = (parsed.get("model_task_config") or {}).get(task) or {}
    names = [str(item) for item in section.get("model_list", [])]
    return [(name, providers.get(name, "")) for name in names]


def provision_model_config(
    *,
    settings: ProvisionSettings,
    plugin_dir: Path | None = None,
    logger: logging.Logger | None = None,
    cc_settings: CcProvisionSettings | None = None,
) -> ProvisionResult:
    """把 Provider 与预设模型写进 model_config.toml（幂等、可重复调用）。

    Args:
        settings: DeepSeek 网页版通道的自动注册设置。
        plugin_dir: 插件目录（用于向上探测实例根）。
        logger: 插件日志器。
        cc_settings: CommandCode 通道的自动注册设置；为 None 或未启用时跳过。

    Returns:
        ProvisionResult: 本次结果（两个通道合并成一次写入与一份备份）。
    """

    if tomlkit is None:  # pragma: no cover - 依赖缺失时只会走到这里
        return ProvisionResult(error="当前运行环境缺少 tomlkit，无法自动写入 model_config.toml")

    path, cands = detect_model_config(settings=settings, plugin_dir=plugin_dir)
    if path is None:
        tried = "、".join(str(item) for item in cands[:6]) or "（无候选）"
        return ProvisionResult(
            error="没找到 Host 的 config/model_config.toml，已尝试：" + tried,
            warnings=["手动指定：在插件配置的自动注册小节填写 model_config_path 绝对路径"],
        )

    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ProvisionResult(path=path, error="读取 model_config.toml 失败：" + str(exc))

    try:
        doc = tomlkit.parse(original)
    except Exception as exc:  # noqa: BLE001 - tomlkit 抛的异常类型不稳定
        return ProvisionResult(path=path, error="解析 model_config.toml 失败：" + str(exc))

    result = ProvisionResult(path=path)
    blocked: set[str] = set()
    changed = _remove_legacy_planner(doc, settings=settings, result=result)
    changed = _ensure_provider(doc, settings=settings, result=result) or changed
    if result.error:
        return result
    # 只注册模型，不接线：哪个任务用哪个模型由用户在 WebUI 里自己选（与 CommandCode 一致）。
    changed = _ensure_models(doc, settings=settings, result=result, blocked=blocked) or changed
    if cc_settings is not None and cc_settings.enabled:
        changed = _provision_commandcode(doc, settings=cc_settings, result=result, blocked=blocked) or changed

    if not changed:
        result.ok = True
        return result

    try:
        text = tomlkit.dumps(doc)
    except Exception as exc:  # noqa: BLE001
        result.error = "生成新配置文本失败：" + str(exc)
        return result

    problem = _validate(text, settings=settings, blocked=blocked, cc_settings=cc_settings)
    if problem:
        result.error = problem
        return result

    backup: Path | None = None
    if settings.keep_backup:
        try:
            backup = _write_backup(path, original, result)
        except OSError as exc:
            result.warnings.append("备份失败（继续写入）：" + str(exc))

    tmp = path.with_name(path.name + ".deepether.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        result.error = "写入 model_config.toml 失败：" + str(exc)
        try:
            tmp.unlink()
        except OSError:
            pass
        return result

    result.ok = True
    result.changed = True
    if backup is not None:
        result.applied.append("已备份原文件到 " + backup.name)
    if logger is not None:
        logger.info("自动注册已写入 %s：%s", path, "；".join(result.applied) or "无明细")
    return result


def _remove_legacy_planner(doc: Any, *, settings: ProvisionSettings, result: ProvisionResult) -> bool:
    """清理 0.3.0 及更早版本注册的 `deepether-planner`；返回是否有改动。

    planner 的命脉是 function calling，网页版没有工具调用能力，这个模型条目在
    WebUI 里只会诱导用户把它配给 planner（每次先失败一次）。升级后就地移除模型定义，
    并把它从任何任务的 `model_list` 里摘掉；如果同名模型属于别的服务商，则不动。
    """

    changed = False
    models = doc.get("models")
    if isinstance(models, list):
        keep: list[Any] = []
        removed = False
        for item in models:
            if (
                str(item.get("name") or "") == LEGACY_PLANNER_MODEL
                and str(item.get("api_provider") or "") == settings.provider_name
            ):
                removed = True
                continue
            keep.append(item)
        if removed:
            rebuilt = tomlkit.aot()
            for item in keep:
                rebuilt.append(item)
            doc["models"] = rebuilt
            result.applied.append(
                "移除旧版模型 " + LEGACY_PLANNER_MODEL + "（planner 需要工具调用，网页版不支持）"
            )
            changed = True

    tasks = doc.get("model_task_config")
    if hasattr(tasks, "get"):
        for task_name in list(tasks.keys()):
            section = tasks[task_name]
            if not hasattr(section, "get"):
                continue
            model_list = section.get("model_list")
            if model_list is None:
                continue
            names = [str(item) for item in model_list]
            if LEGACY_PLANNER_MODEL not in names:
                continue
            rebuilt = tomlkit.array()
            for name in names:
                if name != LEGACY_PLANNER_MODEL:
                    rebuilt.append(name)
            section["model_list"] = rebuilt
            result.applied.append("从任务 " + task_name + " 移除 " + LEGACY_PLANNER_MODEL)
            changed = True
    return changed


def _ensure_provider(doc: Any, *, settings: ProvisionSettings, result: ProvisionResult) -> bool:
    """确保 api_providers 里有本插件 Provider；返回是否有改动。"""

    return _ensure_api_provider(
        doc,
        name=settings.provider_name,
        base_url=DEFAULT_BASE_URL,
        client_type=DEFAULT_CLIENT_TYPE,
        timeout=DEFAULT_TIMEOUT_SEC,
        max_retry=DEFAULT_MAX_RETRY,
        result=result,
    )


def _ensure_api_provider(
    doc: Any,
    *,
    name: str,
    base_url: str,
    client_type: str,
    timeout: int,
    max_retry: int,
    result: ProvisionResult,
) -> bool:
    """确保 `[[api_providers]]` 里有指定 Provider，并修正关键字段。

    Provider 条目的 `base_url` 只是占位：插件不是 HTTP 服务，真正的上游地址在插件内。
    `max_retry=1` 让这个模型失败一次就轮到下一个模型，不在同一模型上重试。
    """

    providers = doc.get("api_providers")
    if providers is None:
        providers = tomlkit.aot()
        doc["api_providers"] = providers
        result.applied.append("新建 api_providers 小节")
    elif not isinstance(providers, list):
        result.error = "model_config.toml 的 api_providers 不是 [[api_providers]] 数组，无法自动写入"
        return False

    existing = None
    for item in providers:
        if str(item.get("name") or "") == name:
            existing = item
            break
        if str(item.get("client_type") or "") == client_type:
            existing = item
            break

    expected = {
        "base_url": base_url,
        "client_type": client_type,
        "auth_type": "none",
        "api_key": "",
        "timeout": timeout,
        "max_retry": max_retry,
    }
    if existing is not None:
        changed = False
        for key, value in expected.items():
            if existing.get(key) != value:
                existing[key] = value
                changed = True
        if changed:
            result.applied.append("修正 Provider " + name + " 的字段")
        else:
            result.skipped.append("Provider " + name + " 已存在")
        return changed

    provider = tomlkit.table()
    provider["name"] = name
    provider["base_url"] = base_url
    provider["api_key"] = ""
    provider["client_type"] = client_type
    provider["auth_type"] = "none"
    provider["timeout"] = timeout
    provider["max_retry"] = max_retry
    providers.append(provider)
    result.applied.append("新增 Provider " + name)
    return True


def _ensure_models(
    doc: Any, *, settings: ProvisionSettings, result: ProvisionResult, blocked: set[str]
) -> bool:
    """确保 models 里有预设模型（只注册、不接线）；返回是否有改动。"""


    models = doc.get("models")
    if models is None:
        models = tomlkit.aot()
        doc["models"] = models
        result.applied.append("新建 models 小节")
    elif not isinstance(models, list):
        result.error = "model_config.toml 的 models 不是 [[models]] 数组，无法自动写入"
        return False

    index = {str(item.get("name") or ""): item for item in models}
    changed = False
    for plan in DEFAULT_PLAN:
        current = index.get(plan.name)
        if current is not None:
            if str(current.get("api_provider") or "") != settings.provider_name:
                blocked.add(plan.name)
                result.warnings.append(
                    "模型 " + plan.name + " 已被其他服务商占用，跳过（如需使用请改名）"
                )
            else:
                result.skipped.append("模型 " + plan.name)
            continue
        models.append(_build_model(plan, provider_name=settings.provider_name))
        index[plan.name] = None
        result.applied.append("新增模型 " + plan.name)
        changed = True
    return changed


def _build_model(plan: ModelPlan, *, provider_name: str) -> Any:
    """构造一条 [[models]] 记录。"""

    model = tomlkit.table()
    model["model_type"] = "llm"
    model["model_identifier"] = DEFAULT_MODEL_IDENTIFIER
    model["name"] = plan.name
    model["api_provider"] = provider_name
    model["price_in"] = 0.0
    model["price_out"] = 0.0
    model["send_temperature"] = False
    model["force_stream_mode"] = False
    extra = tomlkit.inline_table()
    extra["deepether_style"] = plan.style
    extra["deepether_thinking"] = plan.thinking
    model["extra_params"] = extra
    return model


def _provision_commandcode(
    doc: Any,
    *,
    settings: CcProvisionSettings,
    result: ProvisionResult,
    blocked: set[str],
) -> bool:
    """注册 CommandCode Provider 与模型（**不接任何任务**）。"""

    changed = _ensure_api_provider(
        doc,
        name=settings.provider_name,
        base_url=CC_BASE_URL,
        client_type=CC_CLIENT_TYPE,
        timeout=DEFAULT_TIMEOUT_SEC,
        max_retry=DEFAULT_MAX_RETRY,
        result=result,
    )
    if result.error:
        return changed
    selection = cc_selection(settings)
    if not selection:
        result.warnings.append(
            "CommandCode 模型清单为空（register_models="
            + settings.register_models
            + "），只保留 Provider 条目"
        )
    changed = _ensure_cc_models(doc, settings=settings, selection=selection, result=result, blocked=blocked) or changed
    if result.error:
        return changed
    changed = _remove_stale_cc_models(doc, settings=settings, selection=selection, result=result) or changed
    return changed


def cc_selection(settings: CcProvisionSettings) -> tuple[Any, ...]:
    """按注册模式与 include/exclude 选出要注册的模型。"""

    try:  # 延迟导入：没有生成目录时 provision 仍然可用
        from .commandcode.catalog import load_catalog

        return load_catalog().select(
            mode=settings.register_models,
            include=settings.model_include,
            exclude=settings.model_exclude,
        )
    except Exception:  # noqa: BLE001 - 目录损坏不应阻塞 DeepSeek 通道的自动注册
        return ()


def _cc_model_name(model: Any, prefix: str) -> str:
    """按 `model_prefix` 生成模型名（目录里默认已经是 `cc-` 开头）。"""

    name = str(getattr(model, "name", "") or "")
    if prefix and prefix != CC_MODEL_PREFIX and name.startswith(CC_MODEL_PREFIX):
        return prefix + name[len(CC_MODEL_PREFIX) :]
    return name


def _build_cc_model(model: Any, *, name: str, provider_name: str) -> Any:
    """构造一条 CommandCode 的 [[models]] 记录。"""

    entry = tomlkit.table()
    entry["model_type"] = "llm"
    entry["model_identifier"] = str(getattr(model, "model_identifier", "") or "")
    entry["name"] = name
    entry["api_provider"] = provider_name
    # 不要写美元单价：MaiBot 按人民币统计，填了会误报；本插件用 [commandcode] 的金额预算。
    entry["price_in"] = 0.0
    entry["price_out"] = 0.0
    entry["send_temperature"] = False
    entry["force_stream_mode"] = False
    if bool(getattr(model, "visual", False)):
        entry["visual"] = True
    return entry


def _ensure_cc_models(
    doc: Any,
    *,
    settings: CcProvisionSettings,
    selection: tuple[Any, ...],
    result: ProvisionResult,
    blocked: set[str],
) -> bool:
    """确保 models 里有全部 CommandCode 模型（幂等）；返回是否有改动。"""

    models = doc.get("models")
    if models is None:
        models = tomlkit.aot()
        doc["models"] = models
        result.applied.append("新建 models 小节")
    elif not isinstance(models, list):
        result.error = "model_config.toml 的 models 不是 [[models]] 数组，无法自动写入"
        return False

    index = {str(item.get("name") or ""): item for item in models}
    added: list[str] = []
    fixed = 0
    for model in selection:
        name = _cc_model_name(model, settings.model_prefix)
        if not name:
            continue
        current = index.get(name)
        if current is None:
            models.append(_build_cc_model(model, name=name, provider_name=settings.provider_name))
            index[name] = None
            added.append(name)
            continue
        if str(current.get("api_provider") or "") != settings.provider_name:
            blocked.add(name)
            result.warnings.append(
                "模型 " + name + " 已被其他服务商占用，跳过（如需使用请改 model_prefix）"
            )
            continue
        expected: dict[str, Any] = {
            "model_identifier": str(getattr(model, "model_identifier", "") or ""),
            "force_stream_mode": False,
            "send_temperature": False,
            "price_in": 0.0,
            "price_out": 0.0,
        }
        if bool(getattr(model, "visual", False)):
            expected["visual"] = True
        touched = False
        for key, value in expected.items():
            if current.get(key) != value:
                current[key] = value
                touched = True
        if touched:
            fixed += 1

    changed = False
    if added:
        result.applied.append(f"新增 CommandCode 模型 {len(added)} 条（{settings.model_prefix}*）")
        changed = True
    if fixed:
        result.applied.append(f"修正 CommandCode 模型 {fixed} 条")
        changed = True
    if not added and not fixed:
        result.skipped.append(f"CommandCode 模型已是最新（{len(selection)} 条）")
    return changed


def _remove_stale_cc_models(
    doc: Any,
    *,
    settings: CcProvisionSettings,
    selection: tuple[Any, ...],
    result: ProvisionResult,
) -> bool:
    """移除不再属于注册集合的 `cc-*` 模型，并把它们从任务 model_list 里摘掉。

    只处理 `api_provider = 本通道` 且名字带前缀的模型：用户手工加在其它服务商下的模型不动。
    """

    desired = {_cc_model_name(model, settings.model_prefix) for model in selection}
    models = doc.get("models")
    if not isinstance(models, list):
        return False
    keep: list[Any] = []
    removed: list[str] = []
    for item in models:
        name = str(item.get("name") or "")
        provider = str(item.get("api_provider") or "")
        if provider == settings.provider_name and name.startswith(settings.model_prefix) and name not in desired:
            removed.append(name)
            continue
        keep.append(item)
    if not removed:
        return False
    rebuilt = tomlkit.aot()
    for item in keep:
        rebuilt.append(item)
    doc["models"] = rebuilt
    result.applied.append(f"移除不再注册的 CommandCode 模型 {len(removed)} 条")
    detached = _detach_model_names(doc, set(removed))
    if detached:
        result.applied.append(f"从 {detached} 个任务的 model_list 移除这些模型")
    return True


def _detach_model_names(doc: Any, names: set[str]) -> int:
    """把指定模型名从所有任务 `model_list` 里移除；返回受影响的任务数。"""

    tasks = doc.get("model_task_config")
    if not hasattr(tasks, "get"):
        return 0
    touched = 0
    for task_name in list(tasks.keys()):
        section = tasks[task_name]
        if not hasattr(section, "get"):
            continue
        model_list = section.get("model_list")
        if model_list is None:
            continue
        current = [str(item) for item in model_list]
        if not any(name in names for name in current):
            continue
        rebuilt = tomlkit.array()
        for name in current:
            if name not in names:
                rebuilt.append(name)
        section["model_list"] = rebuilt
        touched += 1
    return touched


def _validate(text: str, *, settings: ProvisionSettings, blocked: set[str], cc_settings: CcProvisionSettings | None = None) -> str:
    """用 tomllib 复核生成的配置文本；返回错误说明，空串表示通过。"""

    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return "生成的配置无法被 tomllib 解析：" + str(exc)

    providers = [str(item.get("name") or "") for item in parsed.get("api_providers", [])]
    if len(providers) != len(set(providers)):
        return "生成的配置里出现重名 Provider：" + "、".join(providers)
    if settings.provider_name not in providers:
        return "生成的配置里找不到 Provider " + settings.provider_name
    for item in parsed.get("api_providers", []):
        if str(item.get("name") or "") != settings.provider_name:
            continue
        if item.get("max_retry") != DEFAULT_MAX_RETRY:
            return (
                "Provider " + settings.provider_name + " 的 max_retry 未设为 "
                + str(DEFAULT_MAX_RETRY) + "（会导致网页版失败后在同一模型上重试，无法立刻轮到下一个模型）"
            )

    models = {str(item.get("name") or ""): item for item in parsed.get("models", [])}
    for plan in DEFAULT_PLAN:
        if plan.name in blocked:
            continue
        item = models.get(plan.name)
        if item is None:
            return "生成的配置里缺少模型 " + plan.name
        if str(item.get("api_provider") or "") != settings.provider_name:
            return "模型 " + plan.name + " 的 api_provider 不是 " + settings.provider_name

    # 旧版 planner 模型必须已被清理：它不能承担工具调用，留在配置里只会误导用户。
    for task_name, section in (parsed.get("model_task_config") or {}).items():
        names = [str(item) for item in section.get("model_list", [])]
        if LEGACY_PLANNER_MODEL in names:
            return "任务 " + str(task_name) + " 仍包含已废弃模型 " + LEGACY_PLANNER_MODEL
    if any(
        str(item.get("name") or "") == LEGACY_PLANNER_MODEL
        and str(item.get("api_provider") or "") == settings.provider_name
        for item in parsed.get("models", [])
    ):
        return "生成的配置里仍包含已废弃模型 " + LEGACY_PLANNER_MODEL

    if cc_settings is not None and cc_settings.enabled:
        problem = _validate_commandcode(parsed, settings=cc_settings, blocked=blocked)
        if problem:
            return problem
    return ""


def _validate_commandcode(parsed: dict[str, Any], *, settings: CcProvisionSettings, blocked: set[str]) -> str:
    """校验生成文本里的 CommandCode Provider 与模型；返回错误说明。"""

    providers = {
        str(item.get("name") or ""): item for item in parsed.get("api_providers", [])
    }
    provider = providers.get(settings.provider_name)
    if provider is None:
        return "生成的配置里找不到 CommandCode Provider " + settings.provider_name
    if provider.get("max_retry") != DEFAULT_MAX_RETRY:
        return "CommandCode Provider 的 max_retry 未设为 " + str(DEFAULT_MAX_RETRY)
    if str(provider.get("client_type") or "") != CC_CLIENT_TYPE:
        return "CommandCode Provider 的 client_type 不是 " + CC_CLIENT_TYPE
    models = {str(item.get("name") or ""): item for item in parsed.get("models", [])}
    for model in cc_selection(settings):
        name = _cc_model_name(model, settings.model_prefix)
        if name in blocked:
            continue
        item = models.get(name)
        if item is None:
            return "生成的配置里缺少 CommandCode 模型 " + name
        if str(item.get("api_provider") or "") != settings.provider_name:
            return "CommandCode 模型 " + name + " 的 api_provider 不是 " + settings.provider_name
        if str(item.get("model_identifier") or "") != str(getattr(model, "model_identifier", "")):
            return "CommandCode 模型 " + name + " 的 model_identifier 与目录不一致"
    return ""


def _write_backup(path: Path, original: str, result: ProvisionResult) -> Path:
    """写带时间戳的备份，并只保留最近 MAX_BACKUPS 份。"""

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = path.with_name(path.name + BACKUP_SUFFIX + "-" + stamp)
    backup.write_text(original, encoding="utf-8")
    _prune_backups(path)
    return backup


def _prune_backups(path: Path) -> None:
    """清理过旧的备份文件，忽略清理失败。"""

    try:
        backups = sorted(path.parent.glob(path.name + BACKUP_SUFFIX + "-*"))
    except OSError:
        return
    for stale in backups[:-MAX_BACKUPS]:
        try:
            stale.unlink()
        except OSError:
            continue

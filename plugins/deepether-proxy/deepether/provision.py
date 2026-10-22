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
TASKS_ALL: Final[tuple[str, ...]] = (
    "replyer",
    "utils",
    "memory",
    "mid_memory",
    "fast_model",
    "learner",
)
"""允许接线的任务名：都是纯文本生成任务。

**不含 planner**（强依赖 function calling）与 **emoji**（聊天内选择需要看拼图），
这两类网页版做不到；也不含 vlm/voice/embedding/image_embedding。
"""

UNSUPPORTED_WIRE_TASKS: Final[dict[str, str]] = {
    "planner": (
        "planner 需要工具调用（function calling），网页版做不到，已忽略该任务；"
        "planner 请继续使用官方模型"
    ),
    "emoji": (
        "emoji 的聊天内选择需要看表情包拼图（视觉），网页版没有视觉能力，已忽略该任务；"
        "emoji 留空即可（会按 planner/vlm 的视觉逻辑处理）"
    ),
}
"""明确不支持的任务：出现在 `wire_tasks` 里时跳过并告警，绝不接线。"""

TASK_WIRE_CAVEATS: Final[dict[str, str]] = {
    "memory": "该任务要求严格 JSON，网页版格式不稳时本次结果会被静默丢弃（不会报错、也不会切模型）",
    "mid_memory": "该任务要求 JSON，解析失败会丢弃本次摘要（不会报错、也不会切模型）",
    "learner": "该任务是后台批量的 JSON 输出，解析失败会静默丢弃本次结果",
    "fast_model": "该任务处在回复的等待路径上（断句/表达选择），网页版 8~20 秒抖动会拖慢回复，建议谨慎开启",
}
"""接线这些"可选用"任务时给出的风险提示（不影响接线本身）。"""

LEGACY_PLANNER_MODEL: Final[str] = "deepether-planner"
"""0.3.0 及更早版本会自动注册这个模型；它无法承担 planner 的工具调用，升级时就地清理。"""


@dataclass(frozen=True)
class ModelPlan:
    """一个预设模型：名字、提示词风格、是否开思考、默认接线到哪个任务。"""

    name: str
    style: str
    thinking: bool
    task: str


DEFAULT_PLAN: Final[tuple[ModelPlan, ...]] = (
    ModelPlan(name="deepether-replyer", style="replyer", thinking=False, task="replyer"),
    ModelPlan(name="deepether-utils", style="plain", thinking=False, task="utils"),
)
"""默认注册的模型。

刻意**不注册** `deepether-planner`：planner 的命脉是 function calling，而网页版没有
工具调用能力；注册了只会让用户在 WebUI 里误把它配给 planner，每次先失败一次。
"""

TASK_MODEL_MAP: Final[dict[str, str]] = {
    "replyer": "deepether-replyer",
    "utils": "deepether-utils",
    # 可选的纯文本任务复用 plain/不思考的 utils 模型，不再为每个任务单独建模型条目。
    "memory": "deepether-utils",
    "mid_memory": "deepether-utils",
    "fast_model": "deepether-utils",
    "learner": "deepether-utils",
}
"""任务 → 用于接线的模型名（`DEFAULT_PLAN` 里只定义模型，任务与模型的对应关系在这里）。"""


@dataclass(frozen=True)
class ProvisionSettings:
    """自动注册的可调项，全部来自插件配置。"""

    provider_name: str = "deepether"
    model_config_path: str = ""
    wire_tasks: tuple[str, ...] = ("replyer", "utils")
    keep_backup: bool = True
    selection_strategy: str = "sequential"
    mode: str = "web_first"
    wire_note: str = ""
    """`wire_tasks` 里被跳过的任务说明（例如 planner），写进自动注册结果里提醒用户。"""


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


def _wire_tokens(raw: str | list[str] | tuple[str, ...] | None) -> list[str]:
    """把配置里的任务名（支持逗号/空格分隔的字符串）拆成 token 列表。"""

    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw]
    text = str(raw).replace(",", " ").replace("，", " ").replace(";", " ").replace("；", " ")
    return [part.strip() for part in text.split()]


def _unsupported_wire_note(raw: str | list[str] | tuple[str, ...] | None) -> str:
    """返回 `wire_tasks` 里被跳过的任务说明（去重后拼接）。"""

    notes: list[str] = []
    for item in _wire_tokens(raw):
        note = UNSUPPORTED_WIRE_TASKS.get(item)
        if note and note not in notes:
            notes.append(note)
    return "；".join(notes)


def parse_wire_tasks(raw: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    """把配置里的任务名解析成元组（`planner` 会被跳过，见 `UNSUPPORTED_WIRE_TASKS`）。"""

    seen: list[str] = []
    for item in _wire_tokens(raw):
        if not item or item in seen:
            continue
        if item in UNSUPPORTED_WIRE_TASKS:
            # 不报错：旧配置里带着 planner 也要能正常跑，只是不接线（由 settings 给出提示）。
            continue
        if item not in TASKS_ALL:
            raise ValueError(
                "不支持的任务名 " + item + "；可选：" + "、".join(TASKS_ALL)
            )
        seen.append(item)
    return tuple(seen)


def settings_from_config(config: Any) -> ProvisionSettings:
    """从插件配置对象里读出自动注册设置；配置缺节时返回默认值。"""

    section = getattr(config, "provision", None)
    behavior = getattr(config, "behavior", None)
    mode = str(getattr(behavior, "mode", "") or "").strip() or "web_first"
    if section is None:
        return ProvisionSettings(mode=mode)
    raw_wire = getattr(section, "wire_tasks", "")
    try:
        wire = parse_wire_tasks(raw_wire)
    except ValueError:
        wire = ProvisionSettings().wire_tasks
    provider_name = str(getattr(section, "provider_name", "") or "").strip() or "deepether"
    strategy = str(getattr(section, "selection_strategy", "") or "").strip() or "sequential"
    return ProvisionSettings(
        provider_name=provider_name,
        model_config_path=str(getattr(section, "model_config_path", "") or "").strip(),
        wire_tasks=wire,
        keep_backup=bool(getattr(section, "keep_backup", True)),
        selection_strategy=strategy,
        mode=mode,
        wire_note=_unsupported_wire_note(raw_wire),
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


def provision_model_config(
    *,
    settings: ProvisionSettings,
    plugin_dir: Path | None = None,
    logger: logging.Logger | None = None,
) -> ProvisionResult:
    """把 Provider 与预设模型写进 model_config.toml（幂等、可重复调用）。"""

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
    if settings.wire_note:
        result.warnings.append(settings.wire_note)
    blocked: set[str] = set()
    changed = _remove_legacy_planner(doc, settings=settings, result=result)
    changed = _ensure_provider(doc, settings=settings, result=result) or changed
    if result.error:
        return result
    changed = _ensure_models(doc, settings=settings, result=result, blocked=blocked) or changed
    if result.error:
        return result
    changed = _wire_tasks(doc, settings=settings, result=result, blocked=blocked) or changed

    if not changed:
        result.ok = True
        return result

    try:
        text = tomlkit.dumps(doc)
    except Exception as exc:  # noqa: BLE001
        result.error = "生成新配置文本失败：" + str(exc)
        return result

    problem = _validate(text, settings=settings, blocked=blocked)
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
        if str(item.get("name") or "") == settings.provider_name:
            existing = item
            break
        if str(item.get("client_type") or "") == DEFAULT_CLIENT_TYPE:
            existing = item
            break

    if existing is not None:
        changed = False
        expected = {
            "base_url": DEFAULT_BASE_URL,
            "client_type": DEFAULT_CLIENT_TYPE,
            "auth_type": "none",
            "api_key": "",
            "timeout": DEFAULT_TIMEOUT_SEC,
            # 关键：max_retry=1 表示这个模型失败一次就轮到下一个模型。
            # Host 默认 3，会把一次失败放大成 3 次尝试（每次重试前还要再等
            # 反封禁间隔），既慢又白白增加账号风险。
            "max_retry": DEFAULT_MAX_RETRY,
        }
        for key, value in expected.items():
            if existing.get(key) != value:
                existing[key] = value
                changed = True
        if changed:
            result.applied.append("修正 Provider " + settings.provider_name + " 的字段")
        else:
            result.skipped.append("Provider 已存在")
        return changed

    provider = tomlkit.table()
    provider["name"] = settings.provider_name
    provider["base_url"] = DEFAULT_BASE_URL
    provider["api_key"] = ""
    provider["client_type"] = DEFAULT_CLIENT_TYPE
    provider["auth_type"] = "none"
    provider["timeout"] = DEFAULT_TIMEOUT_SEC
    provider["max_retry"] = DEFAULT_MAX_RETRY
    providers.append(provider)
    result.applied.append("新增 Provider " + settings.provider_name)
    return True


def _ensure_models(
    doc: Any, *, settings: ProvisionSettings, result: ProvisionResult, blocked: set[str]
) -> bool:
    """确保 models 里有三个预设模型；返回是否有改动。"""

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


def _wire_tasks(
    doc: Any, *, settings: ProvisionSettings, result: ProvisionResult, blocked: set[str]
) -> bool:
    """按通道模式把预设模型接进（或移出）任务 model_list；返回是否有改动。

    - `web_first` / `web_only`：把预设模型插到 `model_list` 首位并固定 `sequential`，
      让网页版优先被选中；其余调度交给 MaiBot；
    - `official_only`：把本插件模型从 `model_list` 里移除，让任务直接走其它模型。
    """

    tasks = doc.get("model_task_config")
    if tasks is None:
        result.warnings.append("model_config.toml 缺少 model_task_config 小节，跳过任务接线")
        return False

    detach = settings.mode == "official_only"
    changed = False
    for task in settings.wire_tasks:
        model_name = TASK_MODEL_MAP.get(task)
        if model_name is None:
            unsupported = UNSUPPORTED_WIRE_TASKS.get(task)
            if unsupported:
                result.warnings.append("任务 " + task + " 跳过：" + unsupported)
            else:
                result.warnings.append("任务 " + task + " 没有对应的预设模型，跳过")
            continue
        if model_name in blocked:
            result.warnings.append("任务 " + task + " 的目标模型 " + model_name + " 被占用，跳过接线")
            continue
        try:
            section = tasks[task]
        except (KeyError, TypeError):
            result.warnings.append("model_config.toml 里没有任务 " + task + "，跳过")
            continue
        if not hasattr(section, "get"):
            result.warnings.append("任务 " + task + " 的配置不是表结构，跳过")
            continue
        model_list = section.get("model_list")
        if model_list is None:
            result.warnings.append("任务 " + task + " 缺少 model_list，跳过")
            continue
        names = [str(item) for item in model_list]
        if detach:
            if model_name not in names:
                result.skipped.append("任务 " + task + " 未接线")
                continue
            rebuilt = tomlkit.array()
            for name in names:
                if name != model_name:
                    rebuilt.append(name)
            section["model_list"] = rebuilt
            result.applied.append("按 official_only 从任务 " + task + " 移除 " + model_name)
            changed = True
            if len(rebuilt) == 0:
                result.warnings.append(
                    "任务 " + task + " 的 model_list 在移除 " + model_name + " 后为空，请在 official_only 下补一个官方模型"
                )
            continue
        touched = False
        if model_name not in names:
            rebuilt = tomlkit.array()
            rebuilt.append(model_name)
            for name in names:
                rebuilt.append(name)
            section["model_list"] = rebuilt
            touched = True
        if str(section.get("selection_strategy") or "") != settings.selection_strategy:
            section["selection_strategy"] = settings.selection_strategy
            touched = True
        if touched:
            result.applied.append("接线任务 " + task + " → " + model_name)
            changed = True
        else:
            result.skipped.append("任务 " + task + " 已接线")
        # 可选用任务（JSON 输出 / 等待路径）接线时给一条风险提示，不阻止接线。
        caveat = TASK_WIRE_CAVEATS.get(task)
        if caveat:
            result.warnings.append("任务 " + task + "：" + caveat)
        # 任务只有一个模型时，这个模型失败就没有其它可选模型了，必须明确告警。
        final_names = [str(item) for item in section.get("model_list") or []]
        if all(name == model_name for name in final_names):
            result.warnings.append(
                "任务 "
                + task
                + " 的 model_list 只有 "
                + model_name
                + "；该模型失败时这个任务没有其它可选模型，建议在 model_list 末尾补一个官方模型"
            )
    return changed


def _validate(text: str, *, settings: ProvisionSettings, blocked: set[str]) -> str:
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

    detach = settings.mode == "official_only"
    for task in settings.wire_tasks:
        section = parsed.get("model_task_config", {}).get(task)
        if section is None:
            continue
        names = [str(item) for item in section.get("model_list", [])]
        target = TASK_MODEL_MAP.get(task)
        if target is None or target in blocked:
            continue
        if detach:
            if target in names:
                return "任务 " + task + " 在 official_only 下仍包含 " + target
        elif target not in names:
            return "任务 " + task + " 的 model_list 未包含 " + target

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

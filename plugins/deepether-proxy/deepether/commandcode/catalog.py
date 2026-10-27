"""CommandCode Go 模型目录（由 `scripts/gen_commandcode_catalog.py` 生成）。

目录只负责"有哪些模型、叫什么名字、单价多少、是否视觉"，不负责写 Host 配置；
自动注册逻辑在 `deepether/provision.py`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Iterable

__all__ = ["CcModel", "ModelCatalog", "CURATED_IDENTIFIERS", "load_catalog"]

CATALOG_FILE_NAME: Final[str] = "models_go.json"

#: `register_models = "curated"` 时注册的模型：覆盖聊天 / 工具 / 视觉 / 最低价四类。
CURATED_IDENTIFIERS: Final[tuple[str, ...]] = (
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "deepseek/deepseek-v4-flash-vision-exp",
    "zai-org/GLM-5.3",
    "moonshotai/Kimi-K3",
    "MiniMaxAI/MiniMax-M3",
    "Qwen/Qwen3.8-Flash",
    "Qwen/Qwen3.7-Flash",
    "stepfun/Step-3.5-Flash",
    "xiaomi/mimo-v2.6-flash",
)


@dataclass(frozen=True)
class CcModel:
    """目录里的一条模型记录。"""

    name: str
    model_identifier: str
    display_name: str = ""
    category: str = ""
    context: str = ""
    efforts: tuple[str, ...] = ()
    price_in: float = 0.0
    price_out: float = 0.0
    price_cache_read: float = 0.0
    price_cache_write: float = 0.0
    min_plan: str = ""
    visual: bool = False

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CcModel":
        """从 JSON 记录构造。"""

        efforts = raw.get("efforts")
        return cls(
            name=str(raw.get("name") or ""),
            model_identifier=str(raw.get("model_identifier") or ""),
            display_name=str(raw.get("display_name") or ""),
            category=str(raw.get("category") or ""),
            context=str(raw.get("context") or ""),
            efforts=tuple(str(item) for item in efforts) if isinstance(efforts, list) else (),
            price_in=float(raw.get("price_in") or 0.0),
            price_out=float(raw.get("price_out") or 0.0),
            price_cache_read=float(raw.get("price_cache_read") or 0.0),
            price_cache_write=float(raw.get("price_cache_write") or 0.0),
            min_plan=str(raw.get("min_plan") or ""),
            visual=bool(raw.get("visual")),
        )

    def cost_usd(self, *, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> float:
        """按官方单价估算一次调用的美元成本（目录里的单价是 $/1M tokens）。"""

        billed_input = max(0, int(input_tokens) - int(cache_read_tokens))
        return (
            billed_input * self.price_in
            + max(0, int(cache_read_tokens)) * self.price_cache_read
            + max(0, int(cache_write_tokens)) * self.price_cache_write
            + max(0, int(output_tokens)) * self.price_out
        ) / 1_000_000.0


class ModelCatalog:
    """Go 模型目录。"""

    def __init__(self, models: Iterable[CcModel], *, cli_version: str = "", source: str = "") -> None:
        """初始化目录。"""

        self._models: tuple[CcModel, ...] = tuple(models)
        self.cli_version = cli_version
        self.source = source

    def __len__(self) -> int:
        """返回模型条数。"""

        return len(self._models)

    def __iter__(self) -> Any:
        """遍历模型。"""

        return iter(self._models)

    @property
    def models(self) -> tuple[CcModel, ...]:
        """返回全部模型。"""

        return self._models

    def by_identifier(self, model_identifier: str) -> CcModel | None:
        """按上游精确 id 查找（大小写敏感，id 不能改）。"""

        wanted = (model_identifier or "").strip()
        if not wanted:
            return None
        for model in self._models:
            if model.model_identifier == wanted:
                return model
        return None

    def by_name(self, name: str) -> CcModel | None:
        """按 `cc-*` 模型名查找。"""

        wanted = (name or "").strip().lower()
        if not wanted:
            return None
        for model in self._models:
            if model.name.lower() == wanted:
                return model
        return None

    def resolve(self, token: str) -> CcModel | None:
        """按模型名或上游 id 解析（`model_include/exclude` 两种写法都接受）。"""

        return self.by_name(token) or self.by_identifier(token)

    def select(
        self,
        *,
        mode: str = "all",
        include: str = "",
        exclude: str = "",
    ) -> tuple[CcModel, ...]:
        """按注册模式与 include/exclude 选出要注册的模型。

        Args:
            mode: `all` / `curated` / `none`。
            include: 逗号分隔的额外模型（名字或 id）。
            exclude: 逗号分隔的排除模型（名字或 id）。

        Returns:
            tuple[CcModel, ...]: 按目录顺序去重后的模型。
        """

        mode_key = (mode or "all").strip().lower()
        if mode_key == "none":
            base: list[CcModel] = []
        elif mode_key == "curated":
            base = [model for model in self._models if model.model_identifier in CURATED_IDENTIFIERS]
        else:
            base = list(self._models)

        seen = {model.model_identifier for model in base}
        for token in _split_tokens(include):
            model = self.resolve(token)
            if model is not None and model.model_identifier not in seen:
                base.append(model)
                seen.add(model.model_identifier)

        excluded: set[str] = set()
        for token in _split_tokens(exclude):
            model = self.resolve(token)
            if model is not None:
                excluded.add(model.model_identifier)
        return tuple(model for model in base if model.model_identifier not in excluded)


def _split_tokens(raw: str) -> list[str]:
    """拆分逗号/分号/空白分隔的配置值。"""

    text = str(raw or "").replace("，", ",").replace(";", ",").replace("；", ",")
    return [item.strip() for item in text.split(",") if item.strip()]


def load_catalog(path: Path | None = None) -> ModelCatalog:
    """读取 `models_go.json`；文件缺失或损坏时返回空目录（不阻塞插件加载）。"""

    target = path or Path(__file__).with_name(CATALOG_FILE_NAME)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ModelCatalog((), cli_version="", source=str(target))
    models_raw = raw.get("models") if isinstance(raw, dict) else None
    models = []
    if isinstance(models_raw, list):
        for item in models_raw:
            if isinstance(item, dict):
                model = CcModel.from_dict(item)
                if model.name and model.model_identifier:
                    models.append(model)
    return ModelCatalog(
        models,
        cli_version=str(raw.get("cli_version") or "") if isinstance(raw, dict) else "",
        source=str(raw.get("source") or "") if isinstance(raw, dict) else "",
    )

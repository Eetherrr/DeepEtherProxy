"""从官方 CLI 的 models.md 生成 CommandCode Go 模型目录。

输入：`docs/research/commandcode/models-<cli-version>.md`（官方 `command-code` 包内
`reference/models.md` 的原样快照，列含义：Id / Name / Context / Efforts / 单价 / Min plan / Best for）。
输出：`plugins/deepether-proxy/deepether/commandcode/models_go.json`。

只收录 `Min plan = Go and above` 的模型；其它套餐的模型 Go Key 调用会 403
（`MODEL_NOT_IN_PLAN:`），注册进来只会让用户在 WebUI 里选中一个永远失败的模型。

用法：
    python scripts/gen_commandcode_catalog.py
    python scripts/gen_commandcode_catalog.py --check   # 只校验，不写文件

`--check` 供打包/CI 使用：文件缺失、条数与 md 不一致、或内容过期时返回非 0。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "docs" / "research" / "commandcode" / "models-1.88.0.md"
DEFAULT_TARGET = ROOT / "plugins" / "deepether-proxy" / "deepether" / "commandcode" / "models_go.json"
CLI_VERSION: Final[str] = "1.88.0"

GO_PLAN: Final[str] = "Go and above"

#: 这些模型在 models.md 的描述里明确写了视觉/多模态能力，注册为 `visual=true`。
#: 宁可漏标也不要错标：错标会让 Host 把图片送进纯文本模型，整轮请求直接 400。
VISION_MODEL_IDS: Final[frozenset[str]] = frozenset(
    {
        "deepseek/deepseek-v4-flash-vision-exp",
        "deepseek/deepseek-v4.1-flash",
        "moonshotai/Kimi-K2.7-Code",
        "moonshotai/Kimi-K2.7-Code-Highspeed",
        "moonshotai/Kimi-K2.6",
        "moonshotai/Kimi-K2.5",
        "minimax/minimax-m3.1-flash",
        "MiniMaxAI/MiniMax-M3",
        "xiaomi/mimo-v2.6-pro",
        "xiaomi/mimo-v2.6-pro-ultraspeed",
        "xiaomi/mimo-v2.6-flash",
        "Qwen/Qwen3.8-Omni-Flash",
        "Qwen/Qwen3.8-27B",
        "stepfun/Step-3.7-Flash",
        "nvidia/nemotron-3.5-super-vl",
        "thinkingmachines/inkling",
        "mistral/mistral-large-4",
        "tencent/hy4-preview",
    }
)

#: 厂商前缀 → 模型名里的短别名。用于生成 WebUI 里看得懂的 `cc-*` 名字。
VENDOR_ALIASES: Final[dict[str, str]] = {
    "deepseek": "deepseek",
    "moonshotai": "kimi",
    "zai-org": "glm",
    "z-ai": "glm",
    "minimax": "minimax",
    "minimaxai": "minimax",
    "qwen": "qwen",
    "xiaomi": "mimo",
    "stepfun": "step",
    "meituan": "longcat",
    "nvidia": "nemotron",
    "thinkingmachines": "inkling",
    "inclusionai": "ling",
    "mistral": "mistral",
    "tencent": "tencent",
    "ornith": "ornith",
    "reflection": "beam",
    "poolside": "laguna",
    "stealth": "glyph",
    "openai": "gpt",
    "xai": "grok",
    "meta": "muse",
}

#: 生成名字时要先剥掉的家族词（大小写不敏感），避免 `cc-deepseek-deepseek-v4-flash`。
FAMILY_WORDS: Final[frozenset[str]] = frozenset(
    {
        "deepseek",
        "qwen",
        "glm",
        "kimi",
        "minimax",
        "mimo",
        "step",
        "longcat",
        "nemotron",
        "inkling",
        "ling",
        "hy",
        "muse",
        "beam",
        "laguna",
        "glyph",
        "ornith",
    }
)

_ROW_PATTERN: Final[re.Pattern[str]] = re.compile(r"^\|\s*`([^`]+)`\s*\|(.*)\|\s*$")
_SECTION_PATTERN: Final[re.Pattern[str]] = re.compile(r"^##\s+(.+?)\s*$")
_PRICE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\$(?P<in>[0-9.]+)\s*/\s*\$(?P<out>[0-9.]+)"
    r"(?:\s*·\s*cache\s*\$(?P<cache>[0-9.]+))?"
    r"(?:\s*\(write\s*\$(?P<write>[0-9.]+)\))?"
)


@dataclass(frozen=True)
class ModelRow:
    """models.md 里一行模型的定义。"""

    model_id: str
    display_name: str
    context: str
    efforts: tuple[str, ...]
    price_in: float
    price_out: float
    price_cache_read: float
    price_cache_write: float
    min_plan: str
    category: str


def _split_cells(raw: str) -> list[str]:
    """拆分表格行的单元格（去掉首尾空白，保留空白单元格）。"""

    return [cell.strip() for cell in raw.split("|")]


def parse_models_md(text: str) -> list[ModelRow]:
    """解析 models.md，返回全部模型行。"""

    rows: list[ModelRow] = []
    category = ""
    for line in text.splitlines():
        section = _SECTION_PATTERN.match(line)
        if section:
            category = section.group(1).strip()
            continue
        match = _ROW_PATTERN.match(line)
        if not match:
            continue
        cells = _split_cells(match.group(2))
        if len(cells) < 6:
            continue
        display_name, context, efforts, price, min_plan = cells[0], cells[1], cells[2], cells[3], cells[4]
        prices = _PRICE_PATTERN.search(price)
        if not prices:
            continue
        effort_list = tuple(
            item.strip() for item in efforts.replace("—", "").split(",") if item.strip()
        )
        rows.append(
            ModelRow(
                model_id=match.group(1).strip(),
                display_name=display_name,
                context=context if context != "—" else "",
                efforts=effort_list,
                price_in=float(prices.group("in")),
                price_out=float(prices.group("out")),
                price_cache_read=float(prices.group("cache") or 0.0),
                price_cache_write=float(prices.group("write") or 0.0),
                min_plan=min_plan,
                category=category,
            )
        )
    return rows


def model_name(model_id: str) -> str:
    """按 `cc-` 前缀生成唯一、可读的模型名。

    规则：厂商别名 + 家族词（去掉重复前缀）+ 型号；家族词与型号之间是否保留连字符，
    取决于官方 id 本身（`Qwen3.8-Max` → `qwen3.8-max`，`Kimi-K3` → `kimi-k3`）。
    """

    vendor, _, remainder = model_id.partition("/")
    alias = VENDOR_ALIASES.get(vendor.lower(), re.sub(r"[^a-z0-9]+", "", vendor.lower()) or "model")
    body = remainder or model_id
    match = re.match(r"^([A-Za-z]+)(-?)(.*)$", body)
    if match:
        family, separator, rest = match.group(1), match.group(2), match.group(3)
        family_key = family.lower()
        if family_key in FAMILY_WORDS:
            if family_key == alias:
                # 家族词与厂商别名一致：直接接型号，避免 `cc-deepseek-deepseek-*`。
                body = family_key + separator + rest
            else:
                # 家族词与厂商不同（例如 tencent/hy3-paid）：两段都保留，`tencent-hy3-paid`。
                body = alias + "-" + family_key + separator + rest
    slug = re.sub(r"[^a-z0-9.]+", "-", body.lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return "cc-" + slug


def build_catalog(rows: list[ModelRow]) -> dict[str, object]:
    """把解析结果转成要写入 JSON 的目录结构。"""

    go_rows = [row for row in rows if row.min_plan == GO_PLAN]
    models: list[dict[str, object]] = []
    seen_names: dict[str, str] = {}
    for row in go_rows:
        name = model_name(row.model_id)
        if name in seen_names:
            raise SystemExit(
                f"模型名冲突：{row.model_id} 与 {seen_names[name]} 都生成了 {name}，请调整别名表"
            )
        seen_names[name] = row.model_id
        models.append(
            {
                "name": name,
                "model_identifier": row.model_id,
                "display_name": row.display_name,
                "category": row.category,
                "context": row.context,
                "efforts": list(row.efforts),
                "price_in": row.price_in,
                "price_out": row.price_out,
                "price_cache_read": row.price_cache_read,
                "price_cache_write": row.price_cache_write,
                "min_plan": row.min_plan,
                "visual": row.model_id in VISION_MODEL_IDS,
            }
        )
    models.sort(key=lambda item: str(item["model_identifier"]))
    return {
        "source": "command-code reference/models.md",
        "cli_version": CLI_VERSION,
        "plan": GO_PLAN,
        "count": len(models),
        "models": models,
    }


def render(catalog: dict[str, object]) -> str:
    """序列化成稳定的 JSON 文本（便于 diff 与打包校验）。"""

    return json.dumps(catalog, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="生成 CommandCode Go 模型目录")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="models.md 路径")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="输出 JSON 路径")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args(argv)

    if not args.source.is_file():
        print(f"找不到模型目录源文件：{args.source}", file=sys.stderr)
        return 2
    rows = parse_models_md(args.source.read_text(encoding="utf-8"))
    catalog = build_catalog(rows)
    text = render(catalog)
    count = int(catalog["count"])

    if args.check:
        if not args.target.is_file():
            print(f"缺少生成产物：{args.target}", file=sys.stderr)
            return 3
        current = args.target.read_text(encoding="utf-8")
        if current != text:
            print(f"目录已过期：{args.target}（重新运行本脚本）", file=sys.stderr)
            return 4
        print(f"目录校验通过：{count} 条 Go 模型")
        return 0

    args.target.parent.mkdir(parents=True, exist_ok=True)
    args.target.write_text(text, encoding="utf-8")
    print(f"已写入 {args.target}：{count} 条 Go 模型")
    return 0


if __name__ == "__main__":
    sys.exit(main())

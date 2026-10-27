"""打包插件为可手动安装的 zip。

产物命名：dist/deepether-proxy-<version>.zip，顶层目录固定为 deepether-proxy/（与插件目录同名），
这样 WebUI「上传 zip 安装」解压后能直接得到插件根目录。

打包前会校验 CommandCode 模型目录：文件必须存在、能被解析、且正好包含全部 Go 模型
（数量与 `models.md` 解析结果一致），避免"忘了跑生成脚本"打出空目录的包。

用法：
    python scripts/build_package.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "deepether-proxy"
DIST_DIR = ROOT / "dist"
ARC_ROOT = "deepether-proxy"
CATALOG_JSON = PLUGIN_DIR / "deepether" / "commandcode" / "models_go.json"

# 只排除"生成物"和"开发期文件"；tests 不打进包（Host 侧不执行，且体积可观）
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", "tests", ".mypy_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def check_catalog() -> None:
    """校验 models_go.json 存在且与 models.md 解析结果一致。"""

    spec = importlib.util.spec_from_file_location(
        "gen_commandcode_catalog_for_build", ROOT / "scripts" / "gen_commandcode_catalog.py"
    )
    if spec is None or spec.loader is None:
        raise SystemExit("无法加载 scripts/gen_commandcode_catalog.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    if not CATALOG_JSON.is_file():
        raise SystemExit("缺少 CommandCode 模型目录：先运行 python scripts/gen_commandcode_catalog.py")
    payload = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
    models = payload.get("models")
    if not isinstance(models, list) or not models:
        raise SystemExit("models_go.json 里没有模型")
    rows = module.parse_models_md(module.DEFAULT_SOURCE.read_text(encoding="utf-8"))
    expected = module.render(module.build_catalog(rows))
    if CATALOG_JSON.read_text(encoding="utf-8") != expected:
        raise SystemExit("models_go.json 已过期：重新运行 python scripts/gen_commandcode_catalog.py")
    print(f"catalog  : {len(models)} Go models (cli {payload.get('cli_version')})")


def iter_files(base: Path) -> list[Path]:
    """收集需要打包的文件（相对 PLUGIN_DIR 排序，保证产物可复现）。"""

    out: list[Path] = []
    for path in sorted(PLUGIN_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(PLUGIN_DIR)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.suffix in EXCLUDE_SUFFIXES:
            continue
        out.append(rel)
    return out


def main() -> int:
    """命令行入口。"""

    check_catalog()
    manifest = json.loads((PLUGIN_DIR / "_manifest.json").read_text(encoding="utf-8"))
    version = str(manifest["version"])
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    target = DIST_DIR / f"deepether-proxy-{version}.zip"

    files = iter_files(PLUGIN_DIR)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for rel in files:
            zf.write(PLUGIN_DIR / rel, f"{ARC_ROOT}/{rel.as_posix()}")

    data = target.read_bytes()
    digest = hashlib.sha256(data).hexdigest().upper()
    print(f"version  : {version}")
    print(f"entries  : {len(files)}")
    print(f"bytes    : {len(data)}")
    print(f"sha256   : {digest}")
    print(f"path     : {target}")
    for rel in files:
        print(f"  {ARC_ROOT}/{rel.as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

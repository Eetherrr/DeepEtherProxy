"""为 GitHub Release 准备正文，并校验 tag 与 manifest 版本一致。

CI 用法（.github/workflows/release.yml）：
    python scripts/prepare_release.py --tag "$GITHUB_REF_NAME" --body-out .release-body.md

本地用法：
    python scripts/prepare_release.py --tag v0.5.0 --body-out /tmp/body.md --check-only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "plugins" / "deepether-proxy" / "_manifest.json"
CHANGELOG = ROOT / "plugins" / "deepether-proxy" / "CHANGELOG.md"
REPO_URL = "https://github.com/Eetherrr/DeepEtherProxy"

FOOTER = """
### 安装

1. 下载本页附件 `deepether-proxy-<version>.zip`
2. MaiBot WebUI → 插件页 → 上传 zip → **重启一次 MaiBot**
3. 按 [插件 README]({repo}/blob/main/plugins/deepether-proxy/README.md) 完成授权与配置
4. 发 `/deepether-status` 查看凭证与用量

### 文档

- 索引：[docs/README.md]({repo}/blob/main/docs/README.md)
- 规范（改代码前必读）：[docs/SKILL.md]({repo}/blob/main/docs/SKILL.md)
- 可行性：[docs/FEASIBILITY.md]({repo}/blob/main/docs/FEASIBILITY.md)
- 设计：[docs/design/]({repo}/tree/main/docs/design)
- 决策日志：[docs/records/DECISIONS.md]({repo}/blob/main/docs/records/DECISIONS.md)
- 坑库：[docs/pitfalls/PITFALLS.md]({repo}/blob/main/docs/pitfalls/PITFALLS.md)

> 通过逆向网页版 / CLI 私有网关提供模型能力通常违反服务条款，可能导致限流或封禁。
> 本项目仅限个人学习与技术研究，请自行评估风险；需要稳定合规的服务请用官方 API。

**Full Changelog**：[CHANGELOG.md]({repo}/blob/main/plugins/deepether-proxy/CHANGELOG.md)
"""


def read_version() -> str:
    """返回 manifest 里声明的插件版本。"""

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return str(manifest["version"]).strip()


def extract_section(version: str) -> str:
    """从 CHANGELOG 中截取指定版本的小节（不含标题行）。"""

    lines = CHANGELOG.read_text(encoding="utf-8").splitlines()
    wanted = f"## {version}"
    start = None
    for index, line in enumerate(lines):
        if line.startswith(wanted):
            start = index + 1
            break
    if start is None:
        raise SystemExit(f"CHANGELOG 里找不到版本小节：{wanted}")
    body: list[str] = []
    for line in lines[start:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip()


def main() -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="生成 Release 正文并校验版本")
    parser.add_argument("--tag", required=True, help="GitHub tag，例如 v0.5.0")
    parser.add_argument("--body-out", default="", help="正文输出路径；留空则不写文件")
    parser.add_argument("--check-only", action="store_true", help="只校验版本，不生成正文")
    args = parser.parse_args()

    tag = str(args.tag).strip()
    version = read_version()
    expected_tag = tag[1:] if tag.startswith("v") else tag
    if expected_tag != version:
        raise SystemExit(f"tag 与 manifest 版本不一致：tag={tag} manifest={version}")

    if not args.check_only:
        section = extract_section(version)
        body = f"## DeepEther Proxy v{version}\n\n{section}\n{FOOTER.format(repo=REPO_URL, version=version)}"
        if args.body_out:
            Path(args.body_out).write_text(body, encoding="utf-8")
            print(f"release body -> {args.body_out} ({len(body)} 字符)")
        else:
            print(body)
    print(f"version  : {version}（tag {tag} 一致）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""CommandCode `/alpha/generate` 真实 Key 探针（手动执行，L4 真机测试）。

用途：在发布前确认真实网关仍然接受当前的协议假设（信封字段、工具形状、图片块、事件名）。
**不打印 Key**；输出脱敏，可直接贴进 issue / 研究文档。

用法：
    set COMMAND_CODE_API_KEY=...            # Windows
    export COMMAND_CODE_API_KEY=...         # bash
    python scripts/probe_commandcode.py
    python scripts/probe_commandcode.py --key-file .testenv/commandcode/api_key.txt
    python scripts/probe_commandcode.py --model deepseek/deepseek-v4-pro --skip-vision

任何一步失败都会打印错误分类并返回非 0，不会静默通过。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = ROOT / "plugins" / "deepether-proxy"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from deepether.commandcode.catalog import load_catalog  # noqa: E402
from deepether.commandcode.client import CommandCodeClient  # noqa: E402
from deepether.commandcode.constants import PATH_CREDITS, PATH_WHOAMI  # noqa: E402
from deepether.commandcode.protocol import build_envelope  # noqa: E402
from deepether.errors import DeepEtherError  # noqa: E402

#: 1×1 粉色 PNG（P0 视觉探针用过的同一张）。
_TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

logger = logging.getLogger("probe.commandcode")


def load_key(args: argparse.Namespace) -> str:
    """从参数、环境变量或文件读取 Key。"""

    if args.key_file:
        return Path(args.key_file).read_text(encoding="utf-8").strip()
    import os

    key = os.environ.get("COMMAND_CODE_API_KEY", "").strip()
    if not key:
        raise SystemExit("缺少 Key：设置 COMMAND_CODE_API_KEY 或传 --key-file")
    return key


def text_message(text: str):
    return [{"role": "user", "content": [{"type": "text", "text": text}]}]


def describe(name: str, result) -> dict:
    """把一次调用的结果摘要成可打印的字典。"""

    return {
        "probe": name,
        "finish": result.finish_reason,
        "content": result.content.strip()[:200],
        "reasoning_chars": len(result.reasoning),
        "tool_calls": [
            {"name": call.name, "input": call.arguments, "truncated": call.truncated}
            for call in result.tool_calls
        ],
        "usage": result.usage.to_host(),
        "events": result.events,
        "provider_metadata": result.provider_metadata,
    }


async def run_probes(args: argparse.Namespace, key: str) -> int:
    """依次执行探针；返回进程退出码。"""

    catalog = load_catalog()
    client = CommandCodeClient(
        api_key_provider=lambda: key,
        logger=logger,
        project_slug=args.project_slug,
    )
    failures = 0
    try:
        for path in (PATH_WHOAMI, PATH_CREDITS):
            try:
                payload = await client.get_json(path)
                summary = {
                    "probe": path,
                    "ok": True,
                    "keys": sorted(payload.keys()),
                }
                if path == PATH_CREDITS:
                    summary["credits"] = payload.get("credits")
                    summary["windowLimits"] = payload.get("windowLimits")
                else:
                    user = payload.get("user") or {}
                    summary["user"] = {k: user.get(k) for k in ("userName", "plan", "id") if isinstance(user, dict)}
                print(json.dumps(summary, ensure_ascii=False))
            except DeepEtherError as exc:
                failures += 1
                print(json.dumps({"probe": path, "ok": False, "error": str(exc)}, ensure_ascii=False))

        base_envelope = dict(
            system="你是一个测试助手，回答尽量短。",
            max_tokens=args.max_tokens,
            thread_id="",
            working_dir=str(ROOT),
            environment="",
        )
        # 文本
        envelope = build_envelope(model=args.model, messages=text_message("只回复 OK 两个字母。"), **base_envelope)
        result = await client.generate(envelope, timeout_sec=args.timeout)
        print(json.dumps(describe("text", result), ensure_ascii=False))
        if not result.content.strip():
            failures += 1

        # 工具
        tool_envelope = build_envelope(
            model=args.model,
            messages=text_message("调用 reply 工具，把 OK 发出去。"),
            tools=[
                {
                    "name": "reply",
                    "description": "Send a chat reply",
                    "input_schema": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                }
            ],
            **base_envelope,
        )
        tool_envelope["params"]["reasoning_effort"] = "off"
        tool_result = await client.generate(tool_envelope, timeout_sec=args.timeout)
        print(json.dumps(describe("tool", tool_result), ensure_ascii=False))
        if not tool_result.tool_calls:
            failures += 1

        # 视觉（可选）
        if not args.skip_vision:
            vision_model = args.vision_model
            entry = catalog.by_identifier(vision_model)
            image_envelope = build_envelope(
                model=vision_model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "这张图是什么颜色？只答颜色。"},
                            {
                                "type": "image",
                                "image": f"data:image/png;base64,{_TINY_PNG_BASE64}",
                                "mimeType": "image/png",
                            },
                        ],
                    }
                ],
                **base_envelope,
            )
            vision_result = await client.generate(image_envelope, timeout_sec=args.timeout)
            print(
                json.dumps(
                    {**describe("vision", vision_result), "catalog_visual": bool(entry and entry.visual)},
                    ensure_ascii=False,
                )
            )
            if not vision_result.content.strip() and not vision_result.reasoning.strip():
                failures += 1

        # Provider 全链路（投影 + 预算 + 输出转换），用一个最小插件壳
        if not args.skip_provider:
            from deepether.commandcode.provider import CommandCodeProvider
            from deepether.config import DeepEtherProxyConfig

            config = DeepEtherProxyConfig(
                commandcode={
                    "api_key": key,
                    "request_timeout_sec": args.timeout,
                    "max_tokens": args.max_tokens,
                    "min_interval_sec": 0.0,
                    "max_per_minute": 60,
                }
            )
            runtime_dir = ROOT / ".testenv" / "commandcode" / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)

            class ProbePlugin:
                def __init__(self) -> None:
                    self.config = config
                    self.ctx = SimpleNamespace(paths=SimpleNamespace(runtime_dir=str(runtime_dir)))

                def get_logger(self):
                    return logger

            provider = CommandCodeProvider(ProbePlugin())
            request = {
                "item_schema_version": 1,
                "request_kind": "response",
                "logical_turn_id": "probe-turn",
                "max_tokens": None,
                "temperature": None,
                "response_format": None,
                "extra_params": {},
                "model_info": {
                    "name": "cc-probe",
                    "model_identifier": args.model,
                    "api_provider": "commandcode",
                    "extra_params": {},
                    "force_stream_mode": False,
                    "max_tokens": None,
                    "model_type": "llm",
                    "send_temperature": False,
                    "temperature": None,
                    "visual": False,
                },
                "tool_options": [],
                "context_items": [
                    {"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "你是测试助手。"}]},
                    {"item_type": "UserMessageItem", "parts": [{"type": "text", "text": "只回复 OK。"}]},
                ],
                "api_provider": {"timeout": args.timeout + 15, "api_key": ""},
            }
            try:
                response = await provider.get_response(request)
                items = [item["item_type"] for item in response.get("output_items", [])]
                print(
                    json.dumps(
                        {
                            "probe": "provider_path",
                            "ok": True,
                            "items": items,
                            "usage": response.get("usage"),
                            "budget": provider.budget_snapshot,
                        },
                        ensure_ascii=False,
                    )
                )
            except DeepEtherError as exc:
                failures += 1
                print(json.dumps({"probe": "provider_path", "ok": False, "error": str(exc)}, ensure_ascii=False))
            finally:
                await provider.aclose()
    finally:
        await client.aclose()
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    """命令行入口。"""

    parser = argparse.ArgumentParser(description="CommandCode 真实 Key 探针")
    parser.add_argument("--key-file", default="", help="存放 Key 的文件（默认读 COMMAND_CODE_API_KEY）")
    parser.add_argument("--model", default="deepseek/deepseek-v4-flash")
    parser.add_argument("--vision-model", default="deepseek/deepseek-v4-flash-vision-exp")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--project-slug", default="maibot")
    parser.add_argument("--skip-vision", action="store_true")
    parser.add_argument("--skip-provider", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    key = load_key(args)
    return asyncio.run(run_probes(args, key))


if __name__ == "__main__":
    sys.exit(main())

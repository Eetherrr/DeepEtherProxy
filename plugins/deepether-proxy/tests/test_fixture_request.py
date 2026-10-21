"""L2 契约测试：用真实 MaiBot 请求快照（golden fixture）驱动投影层。

夹具来自测试实例 `debug.dump_request = true` 落盘的原始请求，
只保留结构、已把敏感字段（api_key / nonce / token）清空。
"""

from pathlib import Path

import json

import pytest

from deepether.adapter_in import build_messages, style_suffix

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_FILES = ("request_utils.json", "request_replyer.json")


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_real_request_snapshot_renders(name: str):
    """真实请求必须能被投影成非空 prompt，且没有未知 item_type。"""

    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"缺少夹具 {name}")
    request = _load(name)

    assert request["item_schema_version"] == 1
    assert request["request_kind"] == "response"
    rendered = build_messages(request["context_items"], max_history_chars=60000)

    assert rendered.messages
    assert rendered.unknown_types == []
    assert rendered.text.strip()
    assert rendered.dropped_items == 0


@pytest.mark.parametrize("name", FIXTURE_FILES)
def test_fixture_has_no_secrets(name: str):
    """夹具不允许带任何密钥/凭证。"""

    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"缺少夹具 {name}")
    raw = path.read_text(encoding="utf-8")
    assert "sk-" not in raw
    assert "your-api-key" not in raw
    request = _load(name)
    provider = request.get("api_provider") or {}
    assert not provider.get("api_key")


def test_replyer_fixture_gets_replyer_style_suffix():
    """replyer 夹具带 deepether_style=replyer 时，prompt 末尾应有口语化约束。"""

    path = FIXTURES / "request_replyer.json"
    if not path.exists():
        pytest.skip("缺少 replyer 夹具")
    request = _load("request_replyer.json")
    style = (request.get("extra_params") or {}).get("deepether_style") or "plain"
    rendered = build_messages(request["context_items"], prompt_suffix=style_suffix(style))
    assert style_suffix(style) in rendered.text

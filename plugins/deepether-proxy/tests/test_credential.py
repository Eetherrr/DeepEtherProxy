"""凭证落盘、状态机与脱敏。"""

from pathlib import Path

import json
import logging

from deepether.credential import SOURCE_BOOKMARKLET, CredentialStore
from deepether.errors import mask_secret


def _store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(tmp_path, logging.getLogger("test"))


def test_save_and_load_roundtrip(tmp_path: Path):
    store = _store(tmp_path)
    assert store.load() is None
    assert store.state == "missing"

    store.save("token-abcdefgh", SOURCE_BOOKMARKLET)
    assert store.state == "present"

    reloaded = _store(tmp_path)
    record = reloaded.load()
    assert record is not None
    assert record.token == "token-abcdefgh"
    assert reloaded.state == "present"


def test_mark_invalid_persists(tmp_path: Path):
    store = _store(tmp_path)
    store.save("token-1", SOURCE_BOOKMARKLET)
    store.mark_invalid("上游返回 401")
    assert store.state == "invalid"

    reloaded = _store(tmp_path)
    reloaded.load()
    assert reloaded.state == "invalid"
    assert "401" in reloaded.invalid_reason


def test_save_clears_invalid_flag(tmp_path: Path):
    store = _store(tmp_path)
    store.save("token-1", SOURCE_BOOKMARKLET)
    store.mark_invalid("过期")
    store.save("token-2", SOURCE_BOOKMARKLET)
    assert store.state == "present"


def test_empty_token_rejected(tmp_path: Path):
    import pytest

    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.save("   ", SOURCE_BOOKMARKLET)


def test_corrupt_file_is_tolerated(tmp_path: Path):
    (tmp_path / "credential.json").write_text("{not json", encoding="utf-8")
    store = _store(tmp_path)
    assert store.load() is None
    assert store.state == "missing"


def test_clear_removes_file(tmp_path: Path):
    store = _store(tmp_path)
    store.save("token-1", SOURCE_BOOKMARKLET)
    store.clear()
    assert store.state == "missing"
    assert not (tmp_path / "credential.json").exists()


def test_token_never_written_in_plain_logs(tmp_path: Path, caplog):
    store = _store(tmp_path)
    token = "super-secret-token-value-1234567890"
    with caplog.at_level(logging.DEBUG):
        store.save(token, SOURCE_BOOKMARKLET)
        store.load()
    assert token not in caplog.text
    assert "supe…(35 字符)" in caplog.text


def test_mask_secret():
    assert mask_secret("") == "(空)"
    assert mask_secret("abc") == "***"
    assert mask_secret("abcdefgh") == "abcd…(8 字符)"

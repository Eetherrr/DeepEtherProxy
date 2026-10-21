"""书签小工具生成。"""

from deepether.bookmarklet import build_bookmarklet


def test_bookmarklet_is_single_line_javascript():
    bookmarklet = build_bookmarklet(18765, "abc123")
    assert bookmarklet.startswith("javascript:")
    assert "\n" not in bookmarklet
    assert "http://127.0.0.1:18765/credential/claim" in bookmarklet


def test_bookmarklet_embeds_nonce_quoted():
    bookmarklet = build_bookmarklet(18765, "abc123")
    assert '"abc123"' in bookmarklet


def test_bookmarklet_reads_local_storage_user_token():
    bookmarklet = build_bookmarklet(1234, "n")
    assert "localStorage.getItem('userToken')" in bookmarklet
    assert "JSON.parse" in bookmarklet


def test_bookmarklet_has_manual_fallback_prompt():
    bookmarklet = build_bookmarklet(1234, "n")
    assert "prompt(" in bookmarklet

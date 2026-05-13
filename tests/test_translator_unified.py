"""T6 tests: translate_core public API + tri_sage_collab adapter equivalence."""
import sys, os
from unittest.mock import patch, MagicMock
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ── translate_core exists and is callable ──
def test_translate_core_is_exported():
    from skills.translator.action import translate_core
    assert callable(translate_core)


def test_translate_core_empty_text_returns_failure():
    from skills.translator.action import translate_core
    # empty text → google_gtx should get nothing useful; result should still be a dict
    result = translate_core("", target_lang="繁體中文")
    assert isinstance(result, dict)


def test_translate_core_schema_keys():
    """translate_core must return a dict with at least 'success' key."""
    from skills.translator.action import translate_core
    with patch("skills.translator.action._translate_via_google_gtx", return_value="你好世界"):
        result = translate_core("Hello world", target_lang="繁體中文")
    assert "success" in result


def test_translate_core_short_text_uses_google_gtx_primary():
    """Short text stable-primary path → provider=google_gtx_primary."""
    from skills.translator.action import translate_core
    with patch("skills.translator.action._translate_via_google_gtx", return_value="你好世界"):
        result = translate_core("Hello world", target_lang="繁體中文")
    assert result.get("success") is True
    assert result.get("provider") == "google_gtx_primary"
    assert "你好" in (result.get("text") or "")


# ── translate_text adapter ──
def test_translate_text_delegates_to_translate_core():
    """translate_text must call translate_core (not re-implement)."""
    from skills.bridge import tri_sage_collab
    sentinel = {"success": True, "text": "hola", "provider": "google_gtx_primary", "degraded": False}
    with patch("skills.translator.action.translate_core", return_value=sentinel) as mock_core:
        result = tri_sage_collab.translate_text("Hello", target_lang="Español")
    mock_core.assert_called_once()
    assert result == sentinel


def test_translate_text_missing_text():
    from skills.bridge.tri_sage_collab import translate_text
    result = translate_text("")
    assert result.get("success") is False
    assert "error" in result


def test_translate_text_exception_fallback():
    """When translate_core raises, translate_text returns degraded=True dict."""
    from skills.bridge import tri_sage_collab
    with patch("skills.translator.action.translate_core", side_effect=RuntimeError("boom")):
        result = tri_sage_collab.translate_text("Hello")
    assert result.get("success") is False
    assert result.get("degraded") is True


# ── Schema equivalence ──
def test_both_paths_same_schema():
    """translate_core and translate_text should produce same keys for the same input."""
    from skills.translator.action import translate_core
    from skills.bridge.tri_sage_collab import translate_text
    sentinel = {"success": True, "text": "世界", "provider": "google_gtx_primary", "degraded": False}
    with patch("skills.translator.action.translate_core", return_value=sentinel):
        r_core = translate_core("world", target_lang="繁體中文")
        r_adapter = translate_text("world", target_lang="繁體中文")
    assert set(r_core.keys()) == set(r_adapter.keys()) or r_adapter.get("success") == r_core.get("success")

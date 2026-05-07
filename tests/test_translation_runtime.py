from __future__ import annotations

from skills.translator import action as translator_action


def test_translate_short_text_uses_stable_primary(monkeypatch):
    monkeypatch.setenv("MAGI_TRANSLATOR_STABLE_PRIMARY", "1")
    monkeypatch.setenv("MAGI_TRANSLATOR_STABLE_PRIMARY_MAX_CHARS", "1600")
    monkeypatch.setattr(
        translator_action,
        "_translate_via_google_gtx",
        lambda text, target_lang, timeout_sec=8: "你好，這是穩定主路徑翻譯。",
    )

    result = translator_action.translate(
        {
            "text": "Hello, this is a translation smoke test.",
            "target_lang": "繁體中文",
            "mode": "full",
            "export": "0",
            "timeout_sec": 60,
            "llm_timeout": 25,
        }
    )

    assert result["success"] is True
    assert result["provider"] == "google_gtx_primary"
    assert result["degraded"] is False
    assert "穩定主路徑翻譯" in result["text"]

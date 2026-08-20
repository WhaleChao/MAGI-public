from __future__ import annotations


def test_heavy_translation_never_uses_google_fallback(monkeypatch) -> None:
    from api.handlers import translation_handler
    from skills.bridge import melchior_client

    monkeypatch.setenv("MAGI_FILE_TRANSLATE_CHECKPOINT_ENABLE", "0")
    monkeypatch.setenv("MAGI_FILE_TRANSLATE_GTX_FALLBACK", "1")
    monkeypatch.setenv("MAGI_FILE_TRANSLATE_GTX_PRIMARY", "1")
    monkeypatch.setenv("MAGI_HEAVY_TRANSLATE_ALLOW_GTX_PRIMARY", "1")
    monkeypatch.setattr(melchior_client, "get_circuit_breaker_status", lambda: {"open": False})
    monkeypatch.setattr(
        translation_handler.InferenceGateway,
        "chat",
        lambda *_args, **_kwargs: {
            "success": False,
            "error": "nim_daily_budget_exceeded:500/500",
            "route": "nvidia_nim_required",
            "model": "",
        },
    )

    def forbidden_google(*_args, **_kwargs):
        raise AssertionError("strict heavy translation attempted Google GTX")

    monkeypatch.setattr(translation_handler.urllib.request, "urlopen", forbidden_google)
    result = translation_handler.translate_text_complete(
        "The defendant shall file a response.",
        target_lang="繁體中文",
        heavy=True,
    )

    assert result["success"] is False
    assert result["route"] == "nvidia_nim"
    assert "google" not in str(result.get("model") or "").lower()


def test_heavy_translation_does_not_retry_terminal_provider_failure(monkeypatch) -> None:
    from api.handlers import translation_handler
    from skills.bridge import melchior_client

    monkeypatch.setenv("MAGI_FILE_TRANSLATE_CHECKPOINT_ENABLE", "0")
    monkeypatch.setenv("MAGI_FILE_TRANSLATE_RETRIES", "6")
    monkeypatch.setattr(melchior_client, "get_circuit_breaker_status", lambda: {"open": False})
    calls = {"count": 0}

    def exhausted(*_args, **_kwargs):
        calls["count"] += 1
        return {
            "success": False,
            "error": "nim_daily_budget_exceeded:500/500",
            "route": "nvidia_nim_strict_failed",
            "model": "",
        }

    monkeypatch.setattr(translation_handler.InferenceGateway, "chat", exhausted)
    result = translation_handler.translate_text_complete(
        "The defendant shall file a response.",
        target_lang="繁體中文",
        heavy=True,
    )

    assert calls["count"] == 1
    assert result["success"] is False
    assert result["route"] == "nvidia_nim"
    assert result["model"] == "nvidia_nim_terminal_failure"

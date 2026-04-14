from __future__ import annotations

from skills.bridge import tri_sage_collab


def test_transcribe_auto_prefers_balthasar_before_apple(monkeypatch, tmp_path):
    audio_path = tmp_path / "sample.aiff"
    audio_path.write_bytes(b"test")

    calls = []

    monkeypatch.delenv("MAGI_TRANSCRIBE_AUTO_PREFERS_APPLE", raising=False)
    monkeypatch.setattr(
        tri_sage_collab.balthasar_bridge,
        "transcribe",
        lambda path: calls.append(("balthasar", path)) or {"success": True, "text": "ok", "provider": "balthasar"},
    )

    result = tri_sage_collab.transcribe_audio(str(audio_path))

    assert result["success"] is True
    assert result["provider"] == "balthasar"
    assert calls and calls[0][0] == "balthasar"


def test_transcribe_auto_uses_fast_cli_before_balthasar(monkeypatch, tmp_path):
    audio_path = tmp_path / "sample.aiff"
    audio_path.write_bytes(b"test")

    calls = {"cli": 0, "balthasar": 0}

    monkeypatch.delenv("MAGI_TRANSCRIBE_AUTO_PREFERS_APPLE", raising=False)
    monkeypatch.setenv("MAGI_TRANSCRIBE_AUTO_CLI_MODEL", "tiny")
    monkeypatch.setattr(
        tri_sage_collab.balthasar_bridge,
        "_transcribe_with_whisper_cli",
        lambda path, model=None: calls.__setitem__("cli", calls["cli"] + 1) or {"success": True, "text": "逐字稿", "provider": "openai_whisper_cli", "model": model or "tiny"},
    )
    monkeypatch.setattr(
        tri_sage_collab.balthasar_bridge,
        "transcribe",
        lambda path: calls.__setitem__("balthasar", calls["balthasar"] + 1) or {"success": True, "text": "slow", "provider": "balthasar"},
    )

    result = tri_sage_collab.transcribe_audio(str(audio_path))

    assert result["success"] is True
    assert result["provider"] == "openai_whisper_cli"
    assert result["model"] == "tiny"
    assert calls["cli"] == 1
    assert calls["balthasar"] == 0

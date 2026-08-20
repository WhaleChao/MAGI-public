from __future__ import annotations

from skills.bridge import tri_sage_collab
from skills.bridge.balthasar_bridge import (
    _resolve_whisper_model_dir,
    _transcribe_with_whisper_cli,
    _transcript_postprocess,
)


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


def test_transcript_postprocess_uses_taiwan_legal_term_for_evidence_motion():
    assert _transcript_postprocess("辯護人申請調查證據") == "辯護人聲請調查證據"


def test_whisper_model_dir_defaults_to_local_magi_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("MAGI_WHISPER_MODEL_DIR", raising=False)
    monkeypatch.setattr("skills.bridge.balthasar_bridge.Path.home", lambda: tmp_path)

    resolved = _resolve_whisper_model_dir()

    assert resolved == str(tmp_path / "Library" / "Caches" / "MAGI" / "whisper")
    assert (tmp_path / "Library" / "Caches" / "MAGI" / "whisper").is_dir()


def test_whisper_model_dir_honors_explicit_local_override(monkeypatch, tmp_path):
    override = tmp_path / "court-asr-cache"
    monkeypatch.setenv("MAGI_WHISPER_MODEL_DIR", str(override))

    assert _resolve_whisper_model_dir() == str(override)
    assert override.is_dir()


def test_whisper_cli_refuses_missing_local_model_before_subprocess(monkeypatch, tmp_path):
    audio = tmp_path / "hearing.wav"
    audio.write_bytes(b"fixture")
    model_dir = tmp_path / "models"
    monkeypatch.setenv("MAGI_WHISPER_MODEL_DIR", str(model_dir))
    monkeypatch.setenv("MAGI_WHISPER_BIN", "/usr/bin/true")
    monkeypatch.setattr(
        "skills.bridge.balthasar_bridge.subprocess.run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("missing model must fail before subprocess")
        ),
    )

    result = _transcribe_with_whisper_cli(str(audio), model="court-large")

    assert result["success"] is False
    assert result["error"] == "whisper_cli_preinstalled_model_missing:court-large"

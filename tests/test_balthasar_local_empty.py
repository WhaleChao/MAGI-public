from __future__ import annotations


def test_mlx_empty_transcript_is_failure(monkeypatch, tmp_path) -> None:
    from skills.hearing import balthasar_local

    audio = tmp_path / "silence.wav"
    audio.write_bytes(b"synthetic")

    class EmptyWhisper:
        @staticmethod
        def transcribe(*_args, **_kwargs):
            return {"text": "", "language": "zh", "segments": []}

    monkeypatch.setattr(balthasar_local, "_get_mlx_whisper", lambda: EmptyWhisper())
    result = balthasar_local.transcribe_audio(str(audio), language="zh")

    assert result["success"] is False
    assert result["error"] == "mlx_whisper_empty_text"

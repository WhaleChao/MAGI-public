"""
Local MLX Whisper transcription for Apple Silicon.

Lazy-imports mlx_whisper so the module can be imported even when the
dependency is not installed — callers get a clear error at *call* time
instead of a confusing ModuleNotFoundError at *import* time.
"""

import logging
import os
import sys

logger = logging.getLogger("BalthasarLocal")

# Ensure ffmpeg is in PATH (common issue on macOS via launchd/daemon)
os.environ["PATH"] += os.pathsep + "/opt/homebrew/bin" + os.pathsep + "/usr/local/bin"

# Model Configuration
DEFAULT_MODEL = (
    os.environ.get("MAGI_MLX_WHISPER_MODEL")
    or "mlx-community/whisper-large-v3-mlx"
).strip()
DEFAULT_INITIAL_PROMPT = (
    os.environ.get("MAGI_MLX_INITIAL_PROMPT")
    or "這是一段臺灣法庭錄音，請用繁體中文準確轉寫。 常見專有名詞包含：具狀、陳報、送達、"
       "告訴代理人、辯護人、公訴人、法官、書記官、被上訴人、被代位人、傳喚、期日、言詞辯論、"
       "拘役、易科罰金、上訴駁回、撤回、卷宗、認罪協商、附帶民事訴訟、訴訟代理人、追加起訴、"
       "聲請調查證據、原告、被告。"
).strip()
TAIGI_INITIAL_PROMPT = (
    os.environ.get("MAGI_MLX_TAIGI_INITIAL_PROMPT")
    or "這是一段臺灣法庭的錄音，可能包含台語（臺灣閩南語）與華語，請盡量以繁體中文準確轉寫。"
       "常見專有名詞包含：具狀、陳報、送達、告訴代理人、辯護人、公訴人、法官、書記官、"
       "被上訴人、被代位人、傳喚、期日、卷證、上訴駁回、撤回、認罪協商、原告、被告。"
).strip()
WORD_TIMESTAMPS = str(os.environ.get("MAGI_MLX_WORD_TIMESTAMPS", "1")).strip().lower() in {
    "1", "true", "yes", "on"
}


def _get_mlx_whisper():
    """Lazy-import mlx_whisper; raises ImportError with a helpful message."""
    try:
        import mlx_whisper
        return mlx_whisper
    except ImportError:
        raise ImportError(
            "mlx-whisper is not installed. Install it with: "
            "pip install mlx-whisper   (requires Apple Silicon Mac)"
        )


def _normalize_segments(raw_segments):
    out = []
    if not isinstance(raw_segments, list):
        return out
    for seg in raw_segments:
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        try:
            st = float(seg.get("start", 0.0) or 0.0)
        except Exception:
            st = 0.0
        try:
            ed = float(seg.get("end", st) or st)
        except Exception:
            ed = st
        out.append({
            "start": max(0.0, st),
            "end": max(st, ed),
            "text": text,
        })
    return out


def transcribe_audio(audio_path, model_path=DEFAULT_MODEL, language=None,
                     initial_prompt=None, taigi_hint=False):
    """
    Transcribes audio using mlx-whisper on local Apple Silicon.

    Args:
        audio_path (str): Path to the audio file.
        model_path (str): HuggingFace model path for MLX Whisper.
        language (str): Optional language code (e.g., "en", "zh"). Auto-detect if None.
        initial_prompt (str): Optional initial prompt for Whisper context.
        taigi_hint (bool): If True, use Taigi-aware initial prompt.

    Returns:
        dict: {"success": bool, "text": str, "language": str, "segments": list}
    """
    if not os.path.exists(audio_path):
        return {"success": False, "error": f"Audio file not found: {audio_path}"}

    mlx_whisper = _get_mlx_whisper()
    logger.info("👂 Listening to %s using %s ...", audio_path, model_path)

    try:
        resolved_prompt = (initial_prompt or "").strip()
        if not resolved_prompt:
            resolved_prompt = DEFAULT_INITIAL_PROMPT
        if taigi_hint and not resolved_prompt:
            resolved_prompt = TAIGI_INITIAL_PROMPT

        kwargs = {
            "path_or_hf_repo": model_path,
            "language": language,
        }
        if resolved_prompt:
            kwargs["initial_prompt"] = resolved_prompt
        if WORD_TIMESTAMPS:
            kwargs["word_timestamps"] = True
        try:
            result = mlx_whisper.transcribe(audio_path, **kwargs)
        except TypeError:
            # Backward-compatible with older mlx-whisper signatures.
            kwargs.pop("word_timestamps", None)
            result = mlx_whisper.transcribe(audio_path, **kwargs)

        text = result.get("text", "").strip()
        logger.info("✅ Transcription complete: %s...", text[:50])
        segments = _normalize_segments(result.get("segments"))

        return {
            "success": True,
            "text": text,
            "language": result.get("language", "unknown"),
            "segments": segments,
            "provider": "balthasar_local_mlx",
        }

    except Exception as e:
        logger.error("❌ Transcription failed: %s", e)
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    if len(sys.argv) > 1:
        file_path = sys.argv[1]
        print(transcribe_audio(file_path))
    else:
        print("Usage: python balthasar_local.py <audio_file>")

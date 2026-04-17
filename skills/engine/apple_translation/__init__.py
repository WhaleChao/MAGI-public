"""
Apple Translation framework adapter (macOS 15+).

The Translation framework is Swift-only; we shell out to a compiled Swift sidecar
located next to this module. Usage is stateless: one subprocess call per text.

Latency: ~800ms per call (SwiftUI bootstrap dominates). Acceptable for interactive
translations. For bulk work, chunk in parallel.

Language codes follow BCP-47: "zh-Hant", "zh-Hans", "en", "ja", "ko", etc.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).resolve().parent
_SIDECAR_BIN = _THIS_DIR / "_sidecar" / "magi_translator_sidecar"
_SIDECAR_SRC = _THIS_DIR / "_sidecar" / "main.swift"
_BUILD_SCRIPT = _THIS_DIR / "_sidecar" / "build.sh"

# Default CLI timeout (seconds). Swift bootstrap + translation + teardown.
_DEFAULT_TIMEOUT_SEC = float(os.environ.get("MAGI_APPLE_TRANSLATION_TIMEOUT_SEC", "10.0"))

# Exit code -> stable error key. Keep in sync with main.swift.
_EXIT_CODE_MAP: Dict[int, str] = {
    2: "usage_error",
    3: "empty_or_invalid_input",
    4: "translation_runtime_error",
    5: "sidecar_timeout",
    10: "language_pack_not_installed",
    11: "language_pair_unsupported",
    12: "unknown_availability",
}

# Common language name -> BCP-47 code. Non-exhaustive; caller may pass raw codes.
_LANG_ALIASES: Dict[str, str] = {
    "繁體中文": "zh-Hant",
    "繁中": "zh-Hant",
    "中文": "zh-Hant",
    "zh": "zh-Hant",
    "zh-TW": "zh-Hant",
    "zh-tw": "zh-Hant",
    "zh_tw": "zh-Hant",
    "簡體中文": "zh-Hans",
    "簡中": "zh-Hans",
    "zh-CN": "zh-Hans",
    "zh-cn": "zh-Hans",
    "zh_cn": "zh-Hans",
    "英文": "en",
    "English": "en",
    "english": "en",
    "日文": "ja",
    "日語": "ja",
    "Japanese": "ja",
    "韓文": "ko",
    "韓語": "ko",
    "Korean": "ko",
    "法文": "fr",
    "德文": "de",
    "西文": "es",
    "西班牙文": "es",
    "越南文": "vi",
    "泰文": "th",
}


def normalize_lang(code: str) -> str:
    """Map a human-readable name or common alias to a BCP-47 language code."""
    if not code:
        return ""
    code = code.strip()
    return _LANG_ALIASES.get(code, code)


def is_available() -> Tuple[bool, str]:
    """
    Report whether the sidecar binary is built and reachable.

    Returns (available, reason). `reason` is an empty string on success,
    otherwise a short stable error key.
    """
    if not _SIDECAR_BIN.exists():
        return False, "sidecar_binary_missing"
    if not os.access(str(_SIDECAR_BIN), os.X_OK):
        return False, "sidecar_binary_not_executable"
    return True, ""


def _build_sidecar_if_needed() -> bool:
    """Attempt to compile the sidecar if the binary is missing. Returns True on success."""
    if _SIDECAR_BIN.exists() and os.access(str(_SIDECAR_BIN), os.X_OK):
        return True
    if not _BUILD_SCRIPT.exists():
        logger.warning("apple_translation: build script missing at %s", _BUILD_SCRIPT)
        return False
    try:
        result = subprocess.run(
            ["bash", str(_BUILD_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.warning("apple_translation: sidecar build failed: %s", result.stderr[:500])
            return False
        return _SIDECAR_BIN.exists()
    except Exception as exc:  # noqa: BLE001
        logger.warning("apple_translation: sidecar build exception: %s", exc)
        return False


def translate(
    text: str,
    source_lang: str = "zh-Hant",
    target_lang: str = "en",
    timeout_sec: Optional[float] = None,
    auto_build: bool = False,
) -> Dict[str, object]:
    """
    Translate `text` from `source_lang` to `target_lang` using Apple Translation.

    Returns a dict:
      {
        "success": bool,
        "text": str,            # translated text (empty if failure)
        "provider": "apple_translation",
        "source_lang": <normalized>,
        "target_lang": <normalized>,
        "elapsed_ms": int,
        "error": Optional[str], # stable error key, e.g. "language_pack_not_installed"
        "stderr": Optional[str],
      }

    The function NEVER raises for normal translation failures; check `success`.
    """
    import time

    t0 = time.monotonic()
    src = normalize_lang(source_lang)
    tgt = normalize_lang(target_lang)

    if not text or not text.strip():
        return {
            "success": False,
            "text": "",
            "provider": "apple_translation",
            "source_lang": src,
            "target_lang": tgt,
            "elapsed_ms": 0,
            "error": "empty_input",
            "stderr": None,
        }

    if not src or not tgt:
        return {
            "success": False,
            "text": "",
            "provider": "apple_translation",
            "source_lang": src,
            "target_lang": tgt,
            "elapsed_ms": 0,
            "error": "lang_code_missing",
            "stderr": None,
        }

    available, reason = is_available()
    if not available:
        if auto_build and reason == "sidecar_binary_missing":
            if not _build_sidecar_if_needed():
                return {
                    "success": False,
                    "text": "",
                    "provider": "apple_translation",
                    "source_lang": src,
                    "target_lang": tgt,
                    "elapsed_ms": int((time.monotonic() - t0) * 1000),
                    "error": "sidecar_build_failed",
                    "stderr": None,
                }
        else:
            return {
                "success": False,
                "text": "",
                "provider": "apple_translation",
                "source_lang": src,
                "target_lang": tgt,
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "error": reason,
                "stderr": None,
            }

    try:
        proc = subprocess.run(
            [str(_SIDECAR_BIN), src, tgt],
            input=text.encode("utf-8"),
            capture_output=True,
            timeout=timeout_sec if timeout_sec is not None else _DEFAULT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "text": "",
            "provider": "apple_translation",
            "source_lang": src,
            "target_lang": tgt,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "error": "subprocess_timeout",
            "stderr": None,
        }
    except OSError as exc:
        return {
            "success": False,
            "text": "",
            "provider": "apple_translation",
            "source_lang": src,
            "target_lang": tgt,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "error": "subprocess_oserror",
            "stderr": str(exc),
        }

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    stderr_text = proc.stderr.decode("utf-8", errors="replace") if proc.stderr else ""
    # Filter the noisy IMKCFRunLoopWakeUpReliable warning that every headless SwiftUI run prints.
    stderr_lines = [
        line for line in stderr_text.splitlines()
        if "IMKCFRunLoopWakeUpReliable" not in line and line.strip()
    ]
    clean_stderr = "\n".join(stderr_lines) or None

    if proc.returncode == 0:
        out = proc.stdout.decode("utf-8", errors="replace")
        return {
            "success": True,
            "text": out,
            "provider": "apple_translation",
            "source_lang": src,
            "target_lang": tgt,
            "elapsed_ms": elapsed_ms,
            "error": None,
            "stderr": clean_stderr,
        }

    error_key = _EXIT_CODE_MAP.get(proc.returncode, f"exit_{proc.returncode}")
    return {
        "success": False,
        "text": "",
        "provider": "apple_translation",
        "source_lang": src,
        "target_lang": tgt,
        "elapsed_ms": elapsed_ms,
        "error": error_key,
        "stderr": clean_stderr,
    }


__all__ = ["translate", "is_available", "normalize_lang"]

# -*- coding: utf-8 -*-
"""
apple_intelligence.py
=====================
Apple on-device + Apple Intelligence integration (best-effort).

Goals (台灣用語):
- PDF 判讀：優先用 Apple 系統框架抽文字（Quartz/PDFDocument），必要時再做 OCR。
- 圖片 OCR：優先走捷徑（若使用者已建立），否則走 Vision (VNRecognizeTextRequest)。
- 摘要：目前可程式化的「Apple Intelligence 摘要」以捷徑為主（需先建立捷徑）。
- 音訊轉文字：優先走捷徑，否則走 Speech.framework（需授權）。

Important:
- Shortcuts-based Apple Intelligence requires user-created shortcuts. We do NOT attempt to auto-create shortcuts.
"""

from __future__ import annotations
import logging

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# PyObjC frameworks (installed in MAGI/venv and CODE/.venv)
try:
    import Quartz
    from Foundation import NSURL
    QUARTZ_AVAILABLE = True
except Exception:
    Quartz = None
    NSURL = None
    QUARTZ_AVAILABLE = False

try:
    import Vision
    VISION_AVAILABLE = True
except Exception:
    Vision = None
    VISION_AVAILABLE = False

try:
    import Speech
    SPEECH_AVAILABLE = True
except Exception:
    Speech = None
    SPEECH_AVAILABLE = False


DEFAULT_SHORTCUTS = {
    # Existing shortcuts (see SETUP_SHORTCUTS.md)
    "stt_record": "MAGI 語音辨識",
    "pdf_read": "MAGI 讀取 PDF",
    "ocr": "MAGI OCR 掃描",
    "screen_ocr": "MAGI 螢幕掃描",
    # New: Apple Intelligence writing tools (user must create)
    "summarize": "MAGI 摘要",
    "stt_file": "MAGI 音檔轉文字",
}

_SHORTCUT_ALIASES = {
    # Some users name shortcuts without spaces; accept both.
    "summarize": ["MAGI 摘要", "MAGI摘要"],
    "stt_file": ["MAGI 音檔轉文字", "MAGI音檔轉文字", "MAGI 音檔轉逐字稿", "MAGI音檔轉逐字稿"],
    # Keep existing ones as-is (can be extended later if needed).
    "stt_record": ["MAGI 語音辨識", "MAGI語音辨識"],
    "pdf_read": ["MAGI 讀取 PDF", "MAGI讀取 PDF", "MAGI讀取PDF"],
    "ocr": ["MAGI OCR 掃描", "MAGI OCR掃描", "MAGI OCR", "MAGI OCR掃描"],
    "screen_ocr": ["MAGI 螢幕掃描", "MAGI螢幕掃描"],
}


def _resolve_shortcut_name(key: str) -> str:
    """
    Return the best matching installed shortcut name for a logical key.
    Falls back to DEFAULT_SHORTCUTS[key] if no alias is installed.
    """
    installed = set(_run_shortcuts_list())
    for cand in _SHORTCUT_ALIASES.get(key, []):
        if cand in installed:
            return cand
    return DEFAULT_SHORTCUTS.get(key, key)


def _run_shortcuts_list() -> list[str]:
    try:
        r = subprocess.run(["shortcuts", "list"], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return []
        return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    except Exception:
        return []


def shortcuts_status() -> dict:
    installed = set(_run_shortcuts_list())
    st = {}
    for k, name in DEFAULT_SHORTCUTS.items():
        resolved = _resolve_shortcut_name(k)
        st[k] = {
            "name": name,
            "resolved": resolved,
            "installed": (resolved in installed),
        }
    return {"success": True, "shortcuts": st}


def _run_shortcut(name: str, input_value: Optional[str] = None, timeout_sec: int = 60) -> dict:
    """
    Run macOS Shortcuts via CLI.

    Important detail:
    - `shortcuts run` takes `--input-path` (a file path), not raw text.
      So when callers pass a plain text string, we materialize it into a temp .txt file
      and pass that file path as input.
    """

    def _materialize_input_path(val: Optional[str]) -> tuple[Optional[str], Optional[str], bool]:
        # returns (input_path, temp_path_to_cleanup)
        if val is None:
            return (None, None, False)
        s = str(val)
        # If it's already a real file path, pass through.
        try:
            if os.path.exists(s):
                return (s, None, False)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 129, exc_info=True)

        # Otherwise, treat as raw text and write to temp file.
        try:
            import tempfile

            fd, tmp = tempfile.mkstemp(prefix="magi_shortcuts_in_", suffix=".txt")
            os.close(fd)
            Path(tmp).write_text(s, encoding="utf-8")
            return (tmp, tmp, True)
        except Exception:
            # As a last resort, still pass string (may fail depending on shortcut)
            return (s, None, True)

    def _set_clipboard_text(s: str) -> bool:
        try:
            p = subprocess.run(["pbcopy"], input=(s or "").encode("utf-8"), timeout=3)
            return p.returncode == 0
        except Exception:
            return False

    try:
        cmd = ["shortcuts", "run", name]
        in_path, cleanup_in, input_is_text = _materialize_input_path(input_value)

        # Always capture output in a temp file (stdout can be truncated or empty depending on action types).
        import tempfile

        out_path = tempfile.mktemp(prefix="magi_shortcuts_out_", suffix=".txt")
        def _run(with_input: bool) -> subprocess.CompletedProcess:
            c = ["shortcuts", "run", name]
            if with_input and in_path is not None:
                c += ["--input-path", str(in_path)]
            c += ["--output-path", out_path, "--output-type", "public.plain-text"]
            return subprocess.run(c, capture_output=True, text=True, timeout=max(5, int(timeout_sec)))

        r = _run(with_input=True)
        err = (r.stderr or "").strip()

        out_text = ""
        try:
            if os.path.exists(out_path):
                out_text = (Path(out_path).read_text(encoding="utf-8", errors="replace") or "").strip()
        except Exception:
            out_text = ""
        if not out_text:
            out_text = (r.stdout or "").strip()

        # Fallback: some shortcuts are configured to read clipboard instead of accepting file input.
        # If CLI reports "cannot process shortcut input", retry by placing the text into clipboard and run without input.
        if r.returncode != 0 and input_is_text and ("無法處理捷徑的輸入" in err or "Unable to process shortcut input" in err):
            try:
                # Best-effort: copy the *raw* text to clipboard, not the temp file path.
                _set_clipboard_text(str(input_value or ""))
                r2 = _run(with_input=False)
                err2 = (r2.stderr or "").strip()

                out_text2 = ""
                try:
                    if os.path.exists(out_path):
                        out_text2 = (Path(out_path).read_text(encoding="utf-8", errors="replace") or "").strip()
                except Exception:
                    out_text2 = ""
                if not out_text2:
                    out_text2 = (r2.stdout or "").strip()

                if r2.returncode == 0:
                    return {"success": True, "text": out_text2, "error": ""}
                return {"success": False, "text": out_text2, "error": err2 or f"shortcut failed: rc={r2.returncode}"}
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 199, exc_info=True)

        if r.returncode == 0:
            return {"success": True, "text": out_text, "error": ""}
        return {"success": False, "text": out_text, "error": err or f"shortcut failed: rc={r.returncode}"}
    except subprocess.TimeoutExpired:
        return {"success": False, "text": "", "error": f"shortcut timeout({timeout_sec}s)"}
    except Exception as e:
        return {"success": False, "text": "", "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            if 'cleanup_in' in locals() and cleanup_in and os.path.exists(cleanup_in):
                os.remove(cleanup_in)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 213, exc_info=True)
        try:
            if 'out_path' in locals() and out_path and os.path.exists(out_path):
                os.remove(out_path)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 218, exc_info=True)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------
def extract_pdf_text_quartz(pdf_path: str, max_pages: int = 3) -> dict:
    p = str(pdf_path or "").strip()
    if not p:
        return {"success": False, "text": "", "pages": 0, "error": "missing pdf_path", "engine": "quartz"}
    if not os.path.exists(p):
        return {"success": False, "text": "", "pages": 0, "error": f"file not found: {p}", "engine": "quartz"}
    if not QUARTZ_AVAILABLE:
        return {"success": False, "text": "", "pages": 0, "error": "Quartz not available (pyobjc-framework-Quartz missing)", "engine": "quartz"}
    try:
        url = NSURL.fileURLWithPath_(p)
        doc = Quartz.PDFDocument.alloc().initWithURL_(url)
        if doc is None:
            return {"success": False, "text": "", "pages": 0, "error": "PDFDocument init failed", "engine": "quartz"}
        page_count = int(doc.pageCount() or 0)
        take = max(1, min(int(max_pages or 3), max(1, page_count)))
        parts = []
        for i in range(take):
            page = doc.pageAtIndex_(i)
            if page is None:
                continue
            s = page.string() or ""
            if s:
                parts.append(str(s))
        text = "\n\n".join(parts).strip()
        return {"success": True, "text": text, "pages": page_count, "error": "", "engine": "quartz"}
    except Exception as e:
        return {"success": False, "text": "", "pages": 0, "error": f"{type(e).__name__}: {e}", "engine": "quartz"}


def extract_pdf_text(pdf_path: str, max_pages: int = 3, engine: str = "auto") -> dict:
    eng = (engine or "auto").strip().lower()
    if eng in {"shortcut", "shortcuts", "apple_intelligence"}:
        return _run_shortcut(DEFAULT_SHORTCUTS["pdf_read"], pdf_path, timeout_sec=90) | {"engine": "shortcuts"}
    if eng in {"quartz", "pdfdocument", "pdfkit"}:
        return extract_pdf_text_quartz(pdf_path, max_pages=max_pages)

    # auto: always use Quartz (local, fast, no shortcut dependency)
    return extract_pdf_text_quartz(pdf_path, max_pages=max_pages)


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------
def ocr_image_vision(image_path: str) -> dict:
    p = str(image_path or "").strip()
    if not p:
        return {"success": False, "text": "", "error": "missing image_path", "engine": "vision"}
    if not os.path.exists(p):
        return {"success": False, "text": "", "error": f"file not found: {p}", "engine": "vision"}
    if not VISION_AVAILABLE:
        return {"success": False, "text": "", "error": "Vision not available (pyobjc-framework-Vision missing)", "engine": "vision"}

    try:
        # Prefer file-based handler (works for png/jpg/heic).
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setUsesLanguageCorrection_(True)
        # zh-Hant / zh-Hans optional; Vision uses auto if unspecified.
        try:
            req.setRecognitionLanguages_(["zh-Hant", "zh-Hans", "en-US"])
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 292, exc_info=True)

        url = NSURL.fileURLWithPath_(p)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
        ok = handler.performRequests_error_([req], None)
        if not ok:
            return {"success": False, "text": "", "error": "Vision performRequests failed", "engine": "vision"}

        obs = req.results() or []
        lines = []
        for o in obs:
            try:
                s = o.topCandidates_(1)[0].string()
            except Exception:
                s = ""
            if s:
                lines.append(str(s))
        return {"success": True, "text": "\n".join(lines).strip(), "error": "", "engine": "vision"}
    except Exception as e:
        return {"success": False, "text": "", "error": f"{type(e).__name__}: {e}", "engine": "vision"}


def ocr_image(image_path: str, engine: str = "auto") -> dict:
    eng = (engine or "auto").strip().lower()
    if eng in {"shortcut", "shortcuts", "apple_intelligence"}:
        return _run_shortcut(DEFAULT_SHORTCUTS["ocr"], image_path, timeout_sec=45) | {"engine": "shortcuts"}
    if eng in {"vision", "apple_vision"}:
        return ocr_image_vision(image_path)

    # auto: always use Vision (local, no shortcut dependency)
    return ocr_image_vision(image_path)


# ---------------------------------------------------------------------------
# Summarize (Apple Intelligence via Shortcuts)
# ---------------------------------------------------------------------------
def summarize_text_apple_intelligence(text: str, timeout_sec: int = 60) -> dict:
    # Requires user-created shortcut: "MAGI 摘要"
    st = shortcuts_status().get("shortcuts", {})
    resolved = st.get("summarize", {}).get("resolved") or _resolve_shortcut_name("summarize")
    if not st.get("summarize", {}).get("installed"):
        return {"success": False, "text": "", "error": f"missing shortcut: {resolved}", "engine": "shortcuts"}
    # Shortcuts input length limits vary; caller should chunk if needed.
    r = _run_shortcut(resolved, text, timeout_sec=max(10, int(timeout_sec)))
    r["engine"] = "shortcuts"
    return r


# ---------------------------------------------------------------------------
# Speech-to-Text
# ---------------------------------------------------------------------------
def _speech_auth(timeout_sec: int = 20) -> bool:
    if not SPEECH_AVAILABLE:
        return False
    try:
        status = Speech.SFSpeechRecognizer.authorizationStatus()
    except Exception:
        status = None

    # 3 = authorized in many SDKs; handle more safely.
    try:
        AUTH = getattr(Speech, "SFSpeechRecognizerAuthorizationStatusAuthorized")
        DENIED = getattr(Speech, "SFSpeechRecognizerAuthorizationStatusDenied")
        RESTRICTED = getattr(Speech, "SFSpeechRecognizerAuthorizationStatusRestricted")
    except Exception:
        AUTH = None
        DENIED = None
        RESTRICTED = None

    if AUTH is not None and status == AUTH:
        return True
    if (DENIED is not None and status == DENIED) or (RESTRICTED is not None and status == RESTRICTED):
        return False

    done = {"ok": False, "set": False}

    def _cb(s):
        try:
            done["ok"] = (AUTH is not None and s == AUTH) or bool(getattr(s, "intValue", lambda: 0)() == int(AUTH))
        except Exception:
            done["ok"] = False
        done["set"] = True

    try:
        Speech.SFSpeechRecognizer.requestAuthorization_(_cb)
    except Exception:
        return False

    deadline = time.time() + float(timeout_sec)
    while time.time() < deadline and not done["set"]:
        time.sleep(0.1)
    return bool(done["ok"])


def transcribe_audio_speech_framework(audio_path: str, locale: str = "zh-TW", timeout_sec: int = 120) -> dict:
    # IMPORTANT:
    # Speech.framework recognition from a plain Python process on macOS can SIGABRT
    # due to missing app-level privacy entitlements / usage descriptions.
    # We keep this implementation for future opt-in experiments, but it's disabled by default.
    if (os.environ.get("MAGI_ALLOW_SPEECH_FRAMEWORK") or "").strip().lower() not in {"1", "true", "yes", "on"}:
        return {"success": False, "text": "", "error": "Speech.framework STT disabled by default (set MAGI_ALLOW_SPEECH_FRAMEWORK=1 to opt-in)", "engine": "speech"}

    p = str(audio_path or "").strip()
    if not p:
        return {"success": False, "text": "", "error": "missing audio_path", "engine": "speech"}
    if not os.path.exists(p):
        return {"success": False, "text": "", "error": f"file not found: {p}", "engine": "speech"}
    if not SPEECH_AVAILABLE:
        return {"success": False, "text": "", "error": "Speech not available (pyobjc-framework-Speech missing)", "engine": "speech"}

    if not _speech_auth(timeout_sec=25):
        return {"success": False, "text": "", "error": "Speech authorization not granted", "engine": "speech"}

    try:
        from Foundation import NSLocale, NSRunLoop, NSDate
    except Exception as e:
        return {"success": False, "text": "", "error": f"Foundation missing: {e}", "engine": "speech"}

    try:
        recognizer = Speech.SFSpeechRecognizer.alloc().initWithLocale_(NSLocale.localeWithLocaleIdentifier_(locale))
        if recognizer is None:
            return {"success": False, "text": "", "error": "SFSpeechRecognizer init failed", "engine": "speech"}
        url = NSURL.fileURLWithPath_(p)
        req = Speech.SFSpeechURLRecognitionRequest.alloc().initWithURL_(url)
        req.setShouldReportPartialResults_(False)

        done = {"set": False, "text": "", "err": ""}

        def _handler(result, error):
            if error is not None:
                done["err"] = str(error)
                done["set"] = True
                return
            try:
                if result is not None:
                    best = result.bestTranscription()
                    if best is not None:
                        done["text"] = str(best.formattedString() or "")
                if result is not None and bool(result.isFinal()):
                    done["set"] = True
            except Exception as e:
                done["err"] = str(e)
                done["set"] = True

        _task = recognizer.recognitionTaskWithRequest_resultHandler_(req, _handler)

        deadline = time.time() + float(timeout_sec)
        rl = NSRunLoop.currentRunLoop()
        while time.time() < deadline and not done["set"]:
            rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))

        if not done["set"]:
            try:
                _task.cancel()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 451, exc_info=True)
            return {"success": False, "text": "", "error": f"timeout({timeout_sec}s)", "engine": "speech"}

        if done["err"]:
            return {"success": False, "text": "", "error": done["err"][:400], "engine": "speech"}
        return {"success": True, "text": (done["text"] or "").strip(), "error": "", "engine": "speech"}
    except Exception as e:
        return {"success": False, "text": "", "error": f"{type(e).__name__}: {e}", "engine": "speech"}


def transcribe_audio(audio_path: str, engine: str = "auto") -> dict:
    eng = (engine or "auto").strip().lower()
    st = shortcuts_status().get("shortcuts", {})
    resolved = st.get("stt_file", {}).get("resolved") or _resolve_shortcut_name("stt_file")

    if eng in {"shortcut", "shortcuts", "apple_intelligence"}:
        if st.get("stt_file", {}).get("installed"):
            r = _run_shortcut(resolved, audio_path, timeout_sec=180)
            r["engine"] = "shortcuts"
            return r
        return {"success": False, "text": "", "error": f"missing shortcut: {resolved}", "engine": "shortcuts"}

    if eng in {"speech", "speech_framework"}:
        return transcribe_audio_speech_framework(audio_path)

    # auto: go straight to Speech.framework (shortcuts unreliable without Apple Intelligence)
    return transcribe_audio_speech_framework(audio_path)


def self_test(pdf_path: str, image_path: str, audio_path: str) -> dict:
    rep = {
        "success": True,
        "shortcuts": shortcuts_status(),
        "pdf": extract_pdf_text(pdf_path, max_pages=3, engine="auto"),
        "ocr": ocr_image(image_path, engine="auto"),
        # STT: only verify shortcut availability here to avoid Speech.framework crashes.
        "stt": {"success": False, "text": "", "error": "requires shortcut: MAGI 音檔轉文字", "engine": "shortcuts"},
        "summarize": summarize_text_apple_intelligence("這是一段測試文字，請幫我摘要成三點。", timeout_sec=45),
    }
    rep["success"] = bool(rep["pdf"].get("success") and rep["ocr"].get("success"))
    return rep


def _print(obj: dict) -> int:
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return 0 if obj.get("success") else 2


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage:")
        print("  python apple_intelligence.py status")
        print("  python apple_intelligence.py pdf <pdf_path>")
        print("  python apple_intelligence.py ocr <image_path>")
        print("  python apple_intelligence.py stt <audio_path>")
        print("  python apple_intelligence.py summarize <text>")
        print("  python apple_intelligence.py self_test <pdf_path> <image_path> <audio_path>")
        return 1

    cmd = (argv[1] or "").strip().lower()
    if cmd in {"status", "check"}:
        return _print({"success": True, "shortcuts": shortcuts_status(), "pyobjc": {"quartz": QUARTZ_AVAILABLE, "vision": VISION_AVAILABLE, "speech": SPEECH_AVAILABLE}})
    if cmd == "pdf":
        return _print(extract_pdf_text(argv[2] if len(argv) > 2 else "", max_pages=3, engine="auto"))
    if cmd == "ocr":
        return _print(ocr_image(argv[2] if len(argv) > 2 else "", engine="auto"))
    if cmd == "stt":
        return _print(transcribe_audio(argv[2] if len(argv) > 2 else "", engine="auto"))
    if cmd == "summarize":
        text = argv[2] if len(argv) > 2 else ""
        return _print(summarize_text_apple_intelligence(text, timeout_sec=60))
    if cmd == "self_test":
        if len(argv) < 5:
            return _print({"success": False, "error": "need: self_test <pdf_path> <image_path> <audio_path>"})
        return _print(self_test(argv[2], argv[3], argv[4]))
    return _print({"success": False, "error": f"unknown cmd: {cmd}"})


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

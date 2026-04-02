"""
Text processing utilities extracted from Orchestrator.

Pure functions — no instance state, no side effects beyond file I/O (export_txt).
"""
import logging

import os
import re


# ── Optional output guards (fail-open if unavailable) ──────────────────────
try:
    from api.tw_output_guard import (
        normalize_output_text as _normalize_output_text,
        detect_output_guard_issues as _detect_output_guard_issues,
    )
except Exception:
    _normalize_output_text = None
    _detect_output_guard_issues = None


def sanitize_incoming_message(message: str) -> str:
    """Remove OpenClaw UI metadata wrappers so routing sees the actual user text."""
    text = str(message or "").strip()
    if not text:
        return ""
    text = re.sub(
        r"Conversation info \(untrusted metadata\):\s*\{[\s\S]*?\}\s*",
        "",
        text,
        flags=re.IGNORECASE,
    ).strip()
    if "\n" in text:
        lines = [ln.rstrip() for ln in text.splitlines()]
        text = "\n".join([ln for ln in lines if ln.strip()])
    return text


def strip_intent_prefixes(text: str, patterns: list[str]) -> str:
    """Strip routing prefixes like @MAGI and caller-supplied patterns."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    for _ in range(3):
        prev = raw
        raw = re.sub(r"^@MAGI\s*", "", raw, flags=re.IGNORECASE).strip()
        for pat in patterns:
            raw = re.sub(pat, "", raw, flags=re.IGNORECASE).strip()
        if raw == prev:
            break
    return raw.strip(" ：:，,。；;")


def redact_secrets(text: str) -> str:
    """Mask obvious secrets/tokens in output text."""
    s = (text or "").strip()
    if not s:
        return ""
    s = re.sub(r"(?i)(channel access token|access token|api[_-]?key|secret|password)\s*[:=]\s*\S+", r"\1: [REDACTED]", s)
    s = re.sub(r"[A-Za-z0-9+/=_-]{64,}", "[REDACTED_TOKEN]", s)
    return s


def apply_long_dialog_guard(text: str, platform: str = "") -> str:
    """Export very long replies to TXT, returning preview + download link."""
    s = (text or "").strip()
    if not s:
        return s

    if "|||IMAGE_PATH|||" in s or "|||FILE_PATH|||" in s:
        return s

    enabled = os.environ.get("MAGI_LONG_DIALOG_GUARD_ENABLE", "1").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return s

    try:
        threshold = int(os.environ.get("MAGI_LONG_DIALOG_EXPORT_THRESHOLD", "6500") or "6500")
    except Exception:
        threshold = 6500

    if len(s) < max(2000, threshold):
        return s

    try:
        from skills.ops.export_text import export_txt

        ex = export_txt(s, prefix="long_reply")
        if isinstance(ex, dict) and ex.get("success"):
            url = str(ex.get("url") or "").strip()
            path = str(ex.get("path") or "").strip()
            preview = s[:260].strip()
            plat = (platform or "").upper().strip()
            if url:
                return (
                    "內容較長，我已轉成 TXT 檔：\n"
                    f"{url}\n\n"
                    f"重點預覽：{preview}"
                )
            if plat in {"LINE", "DISCORD", "TELEGRAM", "OPENCLAW", "WEB"}:
                return (
                    "內容較長，我已轉成 TXT 檔（目前尚未取得公開網址）。\n"
                    f"檔案路徑：{path}\n\n"
                    f"重點預覽：{preview}"
                )
    except Exception:
        return s

    return s


def postprocess_router_reply(text: str, platform: str = "") -> str:
    """Redact secrets, normalize wording, apply long-dialog guard."""
    out = redact_secrets(text or "")
    try:
        if _normalize_output_text:
            out = _normalize_output_text(out, platform=(platform or "OPENCLAW"))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 118, exc_info=True)
    out = apply_long_dialog_guard(out, platform=platform)
    return out


def output_guard_issues(text: str, mode: str = "general") -> list[str]:
    """Detect output quality issues via tw_output_guard (returns empty list if unavailable)."""
    try:
        if _detect_output_guard_issues:
            return list(_detect_output_guard_issues(text, mode=mode) or [])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 129, exc_info=True)
    return []

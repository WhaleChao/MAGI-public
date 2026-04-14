"""
MAGI startup helpers: file I/O, captcha brokers, URL utilities,
export functions, and background initialization routines.

Extracted from server.py to reduce monolith size.
"""
from __future__ import annotations

import html as ihtml
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Once-guard: run_startup_hooks() must execute at most once per process
# ---------------------------------------------------------------------------
_STARTUP_HOOKS_DONE = False
_STARTUP_HOOKS_LOCK = threading.Lock()

# ---------------------------------------------------------------------------
# Directory / path constants
# ---------------------------------------------------------------------------
AGENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".agent"))
os.makedirs(AGENT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# LAF captcha constants
# ---------------------------------------------------------------------------
LAF_CAPTCHA_REQUEST_FILE = os.environ.get(
    "MAGI_LAF_CAPTCHA_REQUEST_FILE",
    os.path.join(AGENT_DIR, "laf_captcha_request.json"),
)
LAF_CAPTCHA_RESPONSE_FILE = os.environ.get(
    "MAGI_LAF_CAPTCHA_RESPONSE_FILE",
    os.path.join(AGENT_DIR, "laf_captcha_response.json"),
)
LAF_CAPTCHA_TTL_SECONDS = int(os.environ.get("MAGI_LAF_CAPTCHA_TTL_SECONDS", "300") or "300")

# Generic captcha constants
GEN_CAPTCHA_REQUEST_FILE = os.environ.get(
    "MAGI_CAPTCHA_REQUEST_FILE",
    os.path.join(AGENT_DIR, "captcha_request.json"),
)
GEN_CAPTCHA_RESPONSE_FILE = os.environ.get(
    "MAGI_CAPTCHA_RESPONSE_FILE",
    os.path.join(AGENT_DIR, "captcha_response.json"),
)

# LINE last-sender / callback / base-url persistence
LINE_LAST_SENDER_FILE = os.environ.get(
    "MAGI_LINE_LAST_SENDER_FILE",
    os.path.join(AGENT_DIR, "line_last_sender.json"),
)
LINE_LAST_CALLBACK_FILE = os.environ.get(
    "MAGI_LINE_LAST_CALLBACK_FILE",
    os.path.join(AGENT_DIR, "line_last_callback.json"),
)
LINE_AUTO_ADMIN_LAST_SENDER = os.environ.get(
    "MAGI_LINE_AUTO_ADMIN_LAST_SENDER", "0"
).strip().lower() in {"1", "true", "yes", "on"}

LINE_LAST_BASE_URL_FILE = os.environ.get(
    "MAGI_LINE_LAST_BASE_URL_FILE",
    os.path.join(AGENT_DIR, "line_last_base_url.json"),
)

# Export directory
EXPORTS_DIR = os.environ.get(
    "MAGI_EXPORTS_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static", "exports")),
)
EXPORT_LONG_TEXT = os.environ.get("MAGI_EXPORT_LONG_TEXT", "1").strip().lower() in {"1", "true", "yes", "on"}
EXPORT_TEXT_THRESHOLD = int(os.environ.get("MAGI_EXPORT_TEXT_THRESHOLD", "9000"))


# ============================================================================
# 1. File operation helpers
# ============================================================================

def _load_json(path: str) -> dict:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        return {}
    return {}


def _write_json_atomic(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_write_json_atomic", exc_info=True)


# ============================================================================
# 2. Captcha handlers
# ============================================================================

def _maybe_handle_laf_captcha_reply(event, user_id: str, user_text: str, *, _line_send_text=None) -> bool:
    text = (user_text or "").strip()
    # Accept "1234" or "驗證碼 1234" etc.
    m = re.search(r"(^|\D)(\d{4})(\D|$)", text)
    if not m:
        return False

    req = _load_json(LAF_CAPTCHA_REQUEST_FILE)
    if not req:
        return False

    now = int(time.time())
    requested_at = int(req.get("requested_at") or 0)
    expires_at = int(req.get("expires_at") or 0)
    if expires_at and now > expires_at:
        return False
    if requested_at and (now - requested_at) > max(30, LAF_CAPTCHA_TTL_SECONDS):
        return False

    req_id = (req.get("request_id") or "").strip()
    if not req_id:
        return False

    code = m.group(2)
    resp = {
        "request_id": req_id,
        "captcha": code,
        "received_at": now,
        "from_user_id": user_id,
    }
    _write_json_atomic(LAF_CAPTCHA_RESPONSE_FILE, resp)

    # Best-effort ack
    if _line_send_text is not None:
        _line_send_text(event, user_id, "\u2705 \u5df2\u6536\u5230\u9a57\u8b49\u78bc\uff0cCASPER \u6b63\u5728\u767b\u5165\u6cd5\u6276\uff0c\u5b8c\u6210\u5f8c\u6211\u6703\u518d\u56de\u5831\u3002", prefer_push=False)
    return True


def _maybe_handle_generic_captcha_reply(event, user_id: str, user_text: str, *, _line_send_text=None) -> bool:
    """
    Handle replies for human-in-the-loop captcha requests.
    Only triggers if a pending request exists and is not expired.
    """
    req = _load_json(GEN_CAPTCHA_REQUEST_FILE)
    if not req:
        return False

    now = int(time.time())
    expires_at = int(req.get("expires_at") or 0)
    if expires_at and now > expires_at:
        return False

    req_id = (req.get("request_id") or "").strip()
    if not req_id:
        return False

    expected_len = int(req.get("expected_len") or 0)
    text = (user_text or "").strip()

    # Extract digits; accept either exact length or a reasonable range if not specified.
    digits = re.sub(r"[^0-9]", "", text)
    if expected_len and expected_len > 0:
        if len(digits) < expected_len:
            return False
        digits = digits[:expected_len]
    else:
        if not (4 <= len(digits) <= 12):
            return False

    resp = {
        "request_id": req_id,
        "captcha": digits,
        "received_at": now,
        "from_user_id": user_id,
    }
    _write_json_atomic(GEN_CAPTCHA_RESPONSE_FILE, resp)
    if _line_send_text is not None:
        _line_send_text(event, user_id, "\u2705 \u5df2\u6536\u5230\u9a57\u8b49\u78bc\uff0cCASPER \u6b63\u5728\u7e7c\u7e8c\u8655\u7406\u3002", prefer_push=False)
    return True


# ============================================================================
# 3. URL utility functions
# ============================================================================

def _is_loopback_base_url(base: str) -> bool:
    s = (base or "").strip()
    if not s:
        return True
    if "://" not in s:
        s = "https://" + s
    try:
        host = (urlparse(s).hostname or "").strip().lower()
    except Exception:
        return True
    if not host:
        return True
    if host == "localhost" or host == "::1":
        return True
    if host.startswith("127."):
        return True
    return False


def _normalize_public_base_url(base: str) -> str:
    s = (base or "").strip().strip("'\"")
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    return s.rstrip("/") + "/"


def _base_from_webhook_url(url: str) -> str:
    s = (url or "").strip().strip("'\"")
    if not s:
        return ""
    if "://" not in s:
        s = "https://" + s
    try:
        p = urlparse(s)
        if not p.scheme or not p.netloc:
            return ""
        return f"{p.scheme}://{p.netloc}/"
    except Exception:
        return ""


def _record_last_public_base_url():
    """
    Record the public base URL from the current request so background tasks can build downloadable links.
    Respects reverse proxies via X-Forwarded-Proto / X-Forwarded-Host.
    """
    try:
        from flask import request
        proto = (request.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
        host = (request.headers.get("X-Forwarded-Host") or "").split(",")[0].strip()
        if not proto:
            proto = (request.scheme or "http").strip()
        if not host:
            host = (request.host or "").strip()
        base = _normalize_public_base_url(f"{proto}://{host}")
        if not base or _is_loopback_base_url(base):
            return
        with open(LINE_LAST_BASE_URL_FILE, "w", encoding="utf-8") as f:
            json.dump({"base_url": base, "updated_at": int(time.time())}, f, ensure_ascii=False)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_record_last_public_base_url", exc_info=True)


def _build_tailscale_base_url() -> str:
    """Build base URL from Tailscale IP if configured."""
    from skills.ops.export_text import _load_dotenv_value
    ts_ip = (
        os.environ.get("MAGI_TAILSCALE_IP")
        or _load_dotenv_value("MAGI_TAILSCALE_IP")
        or ""
    ).strip()
    if not ts_ip:
        return ""
    ts_port = (
        os.environ.get("MAGI_TAILSCALE_PORT")
        or _load_dotenv_value("MAGI_TAILSCALE_PORT")
        or "5002"
    ).strip()
    return f"http://{ts_ip}:{ts_port}/"


def _load_public_base_url() -> str:
    """
    Priority: explicit override -> Tailscale VPN -> LINE webhook -> cached base URL.
    """
    # 1. Explicit override
    env_base = _normalize_public_base_url(os.environ.get("MAGI_PUBLIC_BASE_URL") or "")
    if env_base and (not _is_loopback_base_url(env_base)):
        return env_base
    # 2. Tailscale (stable, always-on VPN)
    ts_base = _build_tailscale_base_url()
    if ts_base:
        return ts_base
    # 3. LINE webhook domain (Cloudflare tunnel, may rotate)
    webhook_base = _base_from_webhook_url(os.environ.get("MAGI_LINE_WEBHOOK_ENDPOINT") or "")
    if webhook_base and (not _is_loopback_base_url(webhook_base)):
        return webhook_base
    # 4. Cached base URL from last webhook
    try:
        if os.path.exists(LINE_LAST_BASE_URL_FILE):
            with open(LINE_LAST_BASE_URL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            base = _normalize_public_base_url(data.get("base_url") or "")
            if base and (not _is_loopback_base_url(base)):
                return base
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_load_public_base_url", exc_info=True)
    return ""


# ============================================================================
# 4. Export functions
# ============================================================================

def _export_text_to_static(text: str, prefix: str = "casper") -> dict:
    """
    Write a UTF-8 TXT file under /static/exports and return a public URL if available.
    """
    s = (text or "").strip()
    if not s:
        return {"success": False, "error": "empty text"}
    # Strip Markdown formatting -- TXT is plain text
    try:
        from api.tw_output_guard import strip_markdown_for_chat
        s = strip_markdown_for_chat(s)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_export_text_to_static", exc_info=True)
    try:
        os.makedirs(EXPORTS_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:10]
        filename = f"{prefix}_{stamp}_{token}.txt"
        path = os.path.join(EXPORTS_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(s + "\n")
        base = _load_public_base_url()
        url = (base.rstrip("/") + f"/static/exports/{filename}") if base else ""
        return {"success": True, "path": path, "filename": filename, "url": url}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _safe_export_stem(name: str, fallback: str = "document") -> str:
    raw = str(name or "").strip()
    if not raw:
        raw = fallback
    # Keep CJK characters, strip path separators and invalid filesystem chars.
    raw = re.sub(r'[\\/:*?"<>|]+', "_", raw)
    raw = re.sub(r"\s+", "_", raw).strip(" ._")
    return raw or fallback


def _export_file_meta(path: str) -> dict:
    p = os.path.abspath(path)
    filename = os.path.basename(p)
    base = _load_public_base_url().rstrip("/")
    url = f"{base}/static/exports/{filename}" if base else ""
    return {"success": True, "path": p, "filename": filename, "url": url}


def _find_chrome_binary() -> str:
    import shutil as _shutil
    candidates = [
        (os.environ.get("MAGI_CHROME_BIN") or "").strip(),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        _shutil.which("google-chrome"),
        _shutil.which("chromium"),
        _shutil.which("chromium-browser"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return ""


def _export_form_docx(preview_text: str, stem: str) -> dict:
    txt = str(preview_text or "").strip()
    if not txt:
        return {"success": False, "error": "empty_text"}
    try:
        from docx import Document  # type: ignore
    except Exception as e:
        return {"success": False, "error": f"python_docx_unavailable: {e}"}
    try:
        os.makedirs(EXPORTS_DIR, exist_ok=True)
        filename = f"{stem}.docx"
        path = os.path.join(EXPORTS_DIR, filename)
        doc = Document()
        for line in txt.splitlines():
            doc.add_paragraph(line)
        doc.save(path)
        return _export_file_meta(path)
    except Exception as e:
        return {"success": False, "error": str(e)}


def _render_form_text_to_html(title: str, text: str) -> str:
    safe_title = ihtml.escape(str(title or "OSC \u6587\u4ef6"))
    safe_text = ihtml.escape(str(text or ""))
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{safe_title}</title>"
        "<style>"
        "body{font-family:'Noto Sans TC','PingFang TC','Microsoft JhengHei',sans-serif;"
        "margin:36px;color:#111;line-height:1.6;}"
        "h1{margin:0 0 16px;font-size:24px;}"
        "pre{white-space:pre-wrap;word-wrap:break-word;font-family:inherit;font-size:15px;margin:0;}"
        "</style></head><body>"
        f"<h1>{safe_title}</h1><pre>{safe_text}</pre></body></html>"
    )


def _export_form_pdf(title: str, preview_text: str, stem: str) -> dict:
    txt = str(preview_text or "").strip()
    if not txt:
        return {"success": False, "error": "empty_text"}
    try:
        os.makedirs(EXPORTS_DIR, exist_ok=True)
        pdf_name = f"{stem}.pdf"
        pdf_path = os.path.join(EXPORTS_DIR, pdf_name)

        # Render HTML
        html_content = _render_form_text_to_html(title, txt)

        # Generate PDF using weasyprint
        import weasyprint
        weasyprint.HTML(string=html_content).write_pdf(pdf_path)

        if (not os.path.exists(pdf_path)) or os.path.getsize(pdf_path) < 64:
            return {"success": False, "error": "pdf_not_generated"}

        return _export_file_meta(pdf_path)
    except Exception as e:
        import traceback
        err_msg = traceback.format_exc()
        return {"success": False, "error": f"weasyprint_failed: {e}\n{err_msg}"}


def _export_osc_form_files(title: str, preview_text: str, suggested_filename: str = "") -> dict:
    txt = str(preview_text or "").strip()
    if not txt:
        return {"success": False, "errors": [{"type": "common", "error": "empty_text"}]}
    stamp = time.strftime("%Y%m%d_%H%M%S")
    token = uuid.uuid4().hex[:8]
    stem = _safe_export_stem(suggested_filename, fallback="osc_form")
    full_stem = f"{stem}_{stamp}_{token}"
    docx_meta = _export_form_docx(txt, full_stem)
    pdf_meta = _export_form_pdf(title, txt, full_stem)
    errors = []
    if not docx_meta.get("success"):
        errors.append({"type": "docx", "error": str(docx_meta.get("error") or "docx_failed")})
    if not pdf_meta.get("success"):
        errors.append({"type": "pdf", "error": str(pdf_meta.get("error") or "pdf_failed")})
    ok = bool(docx_meta.get("success") or pdf_meta.get("success"))
    preferred = pdf_meta if pdf_meta.get("success") else (docx_meta if docx_meta.get("success") else {"success": False})
    return {
        "success": ok,
        "export": preferred,
        "export_docx": docx_meta,
        "export_pdf": pdf_meta,
        "errors": errors,
    }


def _public_url_for_local_file(local_path: str) -> str:
    """
    Return a public URL for a local file.
    If the file is already inside /static/, return its URL directly (no copy).
    Otherwise, copy to EXPORTS_DIR and return its URL.
    """
    try:
        p = (local_path or "").strip().strip("'\"")
        if not p or (not os.path.exists(p)):
            return ""
        base = _load_public_base_url().rstrip("/")
        if not base:
            return ""
        abs_p = os.path.abspath(p)
        static_abs = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "static"))
        # If already under /static/, serve directly without copying
        if abs_p.startswith(static_abs + os.sep):
            rel = abs_p[len(static_abs) + 1:]
            return f"{base}/static/{rel}"
        # Otherwise, copy to exports
        os.makedirs(EXPORTS_DIR, exist_ok=True)
        filename = os.path.basename(abs_p)
        stem, ext = os.path.splitext(filename)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:8]
        filename = f"{stem}_{stamp}_{token}{ext}"
        import shutil
        shutil.copy2(abs_p, os.path.join(EXPORTS_DIR, filename))
        return f"{base}/static/exports/{filename}"
    except Exception:
        return ""


# ============================================================================
# 5. Cloudflared tunnel management
# ============================================================================

def _is_cloudflared_alive() -> bool:
    """Check if cloudflared tunnel process is actually running (not pgrep self-match)."""
    import subprocess
    try:
        result = subprocess.run(
            ["pgrep", "-f", "/opt/homebrew/bin/cloudflared tunnel"],
            capture_output=True, timeout=3,
        )
        return result.returncode == 0
    except Exception:
        return False


def _ensure_cloudflared():
    """Start cloudflared if not running and always register webhook with LINE."""
    import subprocess
    import re as _re
    import time as _time
    try:
        log_path = os.path.join(os.path.dirname(__file__), "..", "logs", "cloudflared.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        already_running = False

        # Count running cloudflared instances; kill all if >1 (prevent accumulation)
        try:
            result = subprocess.run(
                ["pgrep", "-f", "/opt/homebrew/bin/cloudflared tunnel"],
                capture_output=True, text=True, timeout=3,
            )
            cf_pids = [p.strip() for p in (result.stdout or "").strip().splitlines() if p.strip()]
        except Exception:
            cf_pids = []

        if len(cf_pids) > 1:
            logger.warning("cloudflared: Found %d instances, killing all to restart cleanly", len(cf_pids))
            try:
                subprocess.run(["pkill", "-f", "/opt/homebrew/bin/cloudflared tunnel"],
                               capture_output=True, timeout=3)
                _time.sleep(1)
            except Exception:
                pass
            cf_pids = []

        if len(cf_pids) == 1:
            # Check if the log still has the URL (not truncated)
            try:
                with open(log_path) as f:
                    content = f.read()
                if _re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', content):
                    logger.info("cloudflared already running (pid=%s)", cf_pids[0])
                    already_running = True
                else:
                    logger.warning("cloudflared running but log empty, restarting")
                    subprocess.run(["kill", cf_pids[0]], capture_output=True, timeout=3)
                    _time.sleep(1)
            except Exception:
                logger.info("cloudflared already running (pid=%s)", cf_pids[0])
                already_running = True

        if not already_running:
            try:
                subprocess.run(["pkill", "-f", "/opt/homebrew/bin/cloudflared tunnel"],
                               capture_output=True, timeout=3)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_ensure_cloudflared/pkill", exc_info=True)
            logger.info("Starting cloudflared tunnel...")
            _cf_log_fh = open(log_path, "w")  # kept open for cloudflared's lifetime
            _cf_proc = subprocess.Popen(
                ["/opt/homebrew/bin/cloudflared", "tunnel", "--url", "http://127.0.0.1:18790", "--no-autoupdate"],
                stdout=subprocess.DEVNULL, stderr=_cf_log_fh,
            )
            # Safety net: register atexit handler to close file handle
            import atexit
            def _atexit_close_cf_log(fh=_cf_log_fh):
                try:
                    if fh and not fh.closed:
                        fh.close()
                except Exception:
                    pass
            atexit.register(_atexit_close_cf_log)

            # Cleanup: close log file handle when cloudflared exits
            def _cleanup_cf_log(proc=_cf_proc, fh=_cf_log_fh):
                try:
                    proc.wait(timeout=3600)  # don't block forever
                except subprocess.TimeoutExpired:
                    logger.warning("cloudflared cleanup wait timed out after 1h")
                except Exception as e:
                    logger.debug("cloudflared wait error: %s", e)
                finally:
                    try:
                        if fh and not fh.closed:
                            fh.close()
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_cleanup_cf_log", exc_info=True)
            threading.Thread(target=_cleanup_cf_log, daemon=True, name="cf-log-cleanup").start()

        def _register():
            cf_url = ""
            if already_running:
                try:
                    with open(log_path) as f:
                        m = _re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', f.read())
                        if m:
                            cf_url = m.group(0)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_register/read_log", exc_info=True)
            if not cf_url:
                for _ in range(30):
                    _time.sleep(1)
                    try:
                        with open(log_path) as f:
                            m = _re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', f.read())
                            if m:
                                cf_url = m.group(0)
                                break
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_register/wait_log", exc_info=True)
            if not cf_url:
                logger.error("Could not get cloudflare tunnel URL after 30s")
                return
            webhook_url = f"{cf_url}/line/webhook"
            logger.info("Tunnel: %s", cf_url)
            # Load LINE token
            token = os.environ.get("MAGI_LINE_CHANNEL_ACCESS_TOKEN", "")
            if not token:
                env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
                try:
                    with open(env_path) as f:
                        for ln in f:
                            if ln.strip().startswith("MAGI_LINE_CHANNEL_ACCESS_TOKEN="):
                                token = ln.strip().split("=", 1)[1].strip().strip("\"'")
                                break
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_register/load_token", exc_info=True)
            if not token:
                logger.warning("No LINE token, skipping webhook registration")
                return
            import urllib.request
            import urllib.parse
            if not already_running:
                _time.sleep(3)  # Wait for new tunnel to be routable
            # Check if LINE already points to this URL
            try:
                get_req = urllib.request.Request(
                    "https://api.line.me/v2/bot/channel/webhook/endpoint",
                    method="GET",
                    headers={"Authorization": f"Bearer {token}"},
                )
                with urllib.request.urlopen(get_req, timeout=10) as resp:
                    current = json.loads(resp.read())
                if current.get("endpoint") == webhook_url:
                    logger.info("LINE webhook already correct: %s", webhook_url)
                    return
                logger.info("LINE webhook mismatch: %s -> %s", current.get("endpoint"), webhook_url)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_register/check_webhook", exc_info=True)
            data = json.dumps({"endpoint": webhook_url}).encode()
            registered = False
            for attempt in range(3):
                try:
                    req = urllib.request.Request(
                        "https://api.line.me/v2/bot/channel/webhook/endpoint",
                        data=data, method="PUT",
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        logger.info("LINE webhook registered: %s -> %s", webhook_url, resp.status)
                        registered = True
                        break
                except Exception as e:
                    logger.warning("LINE webhook registration attempt %d/3 failed: %s", attempt + 1, e)
                    _time.sleep(5)
            if not registered:
                logger.error("LINE webhook registration failed after 3 attempts")
            # Telegram webhook auto-registration
            try:
                from api.server import _load_openclaw_telegram_token, _load_telegram_webhook_secret
                tg_token = _load_openclaw_telegram_token()
                tg_secret = _load_telegram_webhook_secret()
                if tg_token:
                    tg_webhook_url = f"{cf_url}/telegram/webhook"
                    tg_data = urllib.parse.urlencode({"url": tg_webhook_url, **({"secret_token": tg_secret} if tg_secret else {})}).encode()
                    tg_req = urllib.request.Request(f"https://api.telegram.org/bot{tg_token}/setWebhook", data=tg_data)
                    with urllib.request.urlopen(tg_req, timeout=10) as tg_resp:
                        logger.info("Telegram webhook registered: %s -> %s", tg_webhook_url, tg_resp.status)
            except Exception as tg_e:
                logger.warning("Telegram webhook registration failed: %s", tg_e)
            # Save URLs
            agent_dir = os.path.join(os.path.dirname(__file__), "..", ".agent")
            os.makedirs(agent_dir, exist_ok=True)
            try:
                with open(os.path.join(agent_dir, "line_webhook_url.txt"), "w") as f:
                    f.write(webhook_url + "\n")
                with open(os.path.join(agent_dir, "cloudflare_tunnel_url.txt"), "w") as f:
                    f.write(cf_url + "\n")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_register/save_urls", exc_info=True)
        threading.Thread(target=_register, daemon=True, name="cloudflared-register").start()
    except Exception as e:
        logger.warning("cloudflared startup failed: %s", e)


# ============================================================================
# 6. Monitoring threads
# ============================================================================

def _cloudflared_watchdog():
    import time as _time
    _INTERVAL = 90
    _time.sleep(60)  # wait 60s after startup before first check
    while True:
        try:
            if not _is_cloudflared_alive():
                logger.warning("cloudflared died -- restarting...")
                _ensure_cloudflared()
        except Exception as e:
            logger.warning("cloudflared watchdog error: %s", e)
        _time.sleep(_INTERVAL)


def _preload_faiss():
    try:
        from skills.memory.mem_bridge import _get_faiss_index
        idx = _get_faiss_index()
        if idx:
            logger.info("FAISS index pre-loaded: %d vectors", getattr(idx, 'total', 0))
    except Exception as e:
        logger.warning("FAISS pre-load failed (non-fatal): %s", e)


def _warmup_omlx():
    try:
        import time as _t
        _t.sleep(5)  # let Ollama/oMLX finish startup
        from skills.bridge.http_pool import get_session
        from api.model_config import TEXT_PRIMARY_MODEL
        _model = os.environ.get("CASPER_LOCAL_MODEL", TEXT_PRIMARY_MODEL)
        _chat_url = os.environ.get("MAGI_OMLX_CHAT_URL", "http://127.0.0.1:11434")
        r = get_session().post(f"{_chat_url}/v1/chat/completions", json={
            "model": _model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1, "temperature": 0,
        }, timeout=120)
        if r.status_code == 200:
            logger.info("Local LLM (%s) warmed up", _model)
        else:
            logger.warning("LLM warmup got %d", r.status_code)
    except Exception as e:
        logger.warning("LLM warmup failed (non-fatal): %s", e)


def _start_laf_gmail_monitor():
    """Background thread: scan Gmail for LAF emails every 300s."""
    try:
        _laf_paths = [
            os.path.join(os.path.dirname(__file__), '..', 'casper_ecosystem', 'law_firm_orchestrators'),
            os.path.join(os.path.dirname(__file__), '..', 'skills', 'legal'),
        ]
        for p in _laf_paths:
            if p not in sys.path:
                sys.path.insert(0, p)
        from laf_orchestrator import LAFOrchestrator
        laf_orch = LAFOrchestrator(dry_run=False)
        laf_orch.run_monitor()  # blocking loop (interval=300s)
    except Exception as e:
        logger.warning("LAF Gmail Monitor failed to start: %s", e)


def _start_filereview_email_monitor():
    """Background thread: scan Gmail for file-review payment/ready emails every 300s.
    與法扶 Gmail monitor 獨立運行，使用不同的 Gmail token 和 credentials。"""
    _interval = int(os.environ.get("MAGI_FILEREVIEW_EMAIL_INTERVAL", "300") or "300")
    _consecutive_fails = 0
    _MAX_FAILS_BEFORE_NOTIFY = 5
    _last_notify_ts = 0.0
    logger.info("閱卷 Email Monitor 啟動（每 %d 秒掃描）", _interval)
    while True:
        try:
            time.sleep(_interval)
            # 動態 import 避免 circular
            _magi_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            _skill_path = os.path.join(_magi_root, "skills", "file-review-orchestrator")
            if _skill_path not in sys.path:
                sys.path.insert(0, _skill_path)
            from action import cmd_check_emails
            result = cmd_check_emails(notify=True, notify_empty=False)
            _consecutive_fails = 0
            logger.info("閱卷 Email Monitor: scan done, success=%s", result.get("success"))
        except Exception as e:
            _consecutive_fails += 1
            logger.warning("閱卷 Email Monitor error (%d): %s", _consecutive_fails, e)
            if _consecutive_fails >= _MAX_FAILS_BEFORE_NOTIFY and (time.time() - _last_notify_ts > 21600):
                try:
                    from skills.ops.red_phone import notify_admin
                    notify_admin(
                        f"🚨 閱卷 Email Monitor 連續 {_consecutive_fails} 次失敗\n"
                        f"最近錯誤: {str(e)[:200]}\n"
                        f"請檢查 Gmail token 或網路連線",
                        topic_key="filereview",
                    )
                    _last_notify_ts = time.time()
                except Exception:
                    pass


# ============================================================================
# 7. Main startup entry point
# ============================================================================

def run_startup_hooks(app, orchestrator):
    """
    Run all startup hooks: FAISS preload, oMLX warmup, cloudflared tunnel,
    NAS mount guard, LAF Gmail monitor, and export cleanup.

    Called from server.py after all routes and helpers are registered.
    Parameters:
        app         - the Flask app instance
        orchestrator - the main orchestrator instance
    """
    global _STARTUP_HOOKS_DONE
    with _STARTUP_HOOKS_LOCK:
        if _STARTUP_HOOKS_DONE:
            logger.warning(
                "run_startup_hooks() called again in the same process — skipping "
                "(double-import or circular-import detected; LAF monitor already running)"
            )
            return
        _STARTUP_HOOKS_DONE = True

    _startup_enabled = str(
        os.environ.get("MAGI_DISABLE_SERVER_STARTUP_HOOKS", "0")
    ).strip().lower() not in {"1", "true", "yes", "on"}

    if not _startup_enabled:
        logger.info("Server startup hooks disabled by MAGI_DISABLE_SERVER_STARTUP_HOOKS")
        return

    # Cleanup old export files (>30 days)
    try:
        from api.server import cleanup_old_exports
        _n_cleaned = cleanup_old_exports(days=30)
        if _n_cleaned:
            logger.info("Startup: cleaned %d old exports", _n_cleaned)
    except Exception:
        pass

    # Pre-load FAISS index in background
    threading.Thread(target=_preload_faiss, daemon=True, name="faiss-preload").start()

    # Warm up local LLM
    threading.Thread(target=_warmup_omlx, daemon=True, name="omlx-warmup").start()

    # Cloudflared tunnel
    try:
        _ensure_cloudflared()
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "run_startup_hooks/cloudflared", exc_info=True)

    # Cloudflared watchdog
    threading.Thread(target=_cloudflared_watchdog, daemon=True, name="cloudflared-watchdog").start()

    # NAS SMB auto-mount guard
    try:
        from api.nas_mount_guard import start_nas_mount_guard
        start_nas_mount_guard(interval=120)
    except Exception as e:
        logger.warning("NAS mount guard failed to start: %s", e)

    # LAF Gmail background monitor
    try:
        _laf_gmail_thread = threading.Thread(
            target=_start_laf_gmail_monitor,
            daemon=True,
            name="laf-gmail-monitor",
        )
        _laf_gmail_thread.start()
        logger.info("LAF Gmail Monitor background thread started")
    except Exception as e:
        logger.warning("LAF Gmail Monitor failed to start: %s", e)

    # 閱卷 Email 監控已整合進法扶 Gmail Monitor 的 poll cycle
    # （同一個信箱，每輪掃完法扶信件後順便掃閱卷信件，不另開 thread）
    logger.info("File Review Email Monitor: integrated into LAF Gmail Monitor cycle")

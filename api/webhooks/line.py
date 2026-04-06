"""LINE messaging and attachment handling.

Extracted from server.py. Functions in this module are registered
on the LINE handler object via handler.add().
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy-init module-level references (set by init_line_module)
# ---------------------------------------------------------------------------
orchestrator = None
line_bot_api = None
handler = None
_app = None

# LINE SDK message types — imported lazily on first use
TextSendMessage = None
ImageSendMessage = None
LineBotApiError = None

# Helpers imported from server.py at init time
_normalize_output_text = None
_public_url_for_local_file = None
_export_text_to_static = None
_send_telegram_text = None
_notify_admin_telegram_once = None
_telegram_send_orchestrator_response = None
_append_channel_delivery_audit = None
_maybe_handle_laf_captcha_reply = None
_maybe_handle_generic_captcha_reply = None
WEB_NOTIFICATIONS = defaultdict(lambda: deque(maxlen=200))

# Thread pools — imported from server at init time
_CHANNEL_BG_EXECUTOR = None
_ATTACHMENT_BG_EXECUTOR = None

# ---------------------------------------------------------------------------
# Path / env constants (re-derived here so module is self-contained)
# ---------------------------------------------------------------------------
_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

from api.runtime_paths import ensure_path_on_sys_path, get_orch_dir

AGENT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".agent"))
os.makedirs(AGENT_DIR, exist_ok=True)

LINE_LAST_SENDER_FILE = os.environ.get(
    "MAGI_LINE_LAST_SENDER_FILE",
    os.path.join(AGENT_DIR, "line_last_sender.json"),
)
LINE_LAST_CALLBACK_FILE = os.environ.get(
    "MAGI_LINE_LAST_CALLBACK_FILE",
    os.path.join(AGENT_DIR, "line_last_callback.json"),
)
# Safer default: OFF. Admin must be explicitly allowlisted.
LINE_AUTO_ADMIN_LAST_SENDER = os.environ.get("MAGI_LINE_AUTO_ADMIN_LAST_SENDER", "0").strip().lower() in {"1", "true", "yes", "on"}

LINE_LAST_BASE_URL_FILE = os.environ.get(
    "MAGI_LINE_LAST_BASE_URL_FILE",
    os.path.join(AGENT_DIR, "line_last_base_url.json"),
)

EXPORTS_DIR = os.environ.get(
    "MAGI_EXPORTS_DIR",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "static", "exports")),
)
EXPORT_LONG_TEXT = os.environ.get("MAGI_EXPORT_LONG_TEXT", "1").strip().lower() in {"1", "true", "yes", "on"}
EXPORT_TEXT_THRESHOLD = int(os.environ.get("MAGI_EXPORT_TEXT_THRESHOLD", "9000"))

ATTACHMENT_JOB_DIR = os.path.join(AGENT_DIR, "attachment_jobs")
ATTACHMENT_JOB_FILE_DIR = os.path.join(ATTACHMENT_JOB_DIR, "files")

# Admin allowlist
try:
    from api.admin_allowlist import get_line_admin_user_ids
except ImportError:
    def get_line_admin_user_ids():  # type: ignore
        return set()

ADMIN_LINE_USER_IDS = set(get_line_admin_user_ids() or set())
if not ADMIN_LINE_USER_IDS:
    _log.warning("⚠️ No LINE admin allowlist configured (MAGI_ADMIN_LINE_IDS/admin_allowlist.json). LINE users will default to non-admin.")

EXPECTED_MAGI_API_KEY = os.environ.get("MAGI_API_KEY", "").strip()
if not EXPECTED_MAGI_API_KEY:
    _log.warning("⚠️ MAGI_API_KEY not configured. /api/transcribe will require authenticated dashboard session.")

# Context Buffer (Simple in-memory for now)
# { user_id: { "type": "image|audio|file", "path": "/tmp/...", "timestamp": ... } }
user_context = {}
CONTEXT_TTL_SECONDS = int(os.environ.get("LINE_CONTEXT_TTL_SECONDS", "900"))

# Push budget
_LINE_PUSH_COUNTER_FILE = os.path.join(AGENT_DIR, "line_push_counter.json")
_LINE_PUSH_DAILY_LIMIT = int(os.environ.get("MAGI_LINE_PUSH_DAILY_LIMIT", "5") or "5")

# Quota / delay management
LINE_QUOTA_ALERT_FILE = os.path.join(AGENT_DIR, "line_quota_alert.json")
LINE_DELAY_QUEUE_FILE = os.path.join(AGENT_DIR, "line_delayed_queue.json")
DISCORD_LAST_CHANNEL_FILE = os.path.join(AGENT_DIR, "discord_last_channel.json")
_LINE_LAST_OUTGOING: dict[str, dict] = {}
_LINE_DELAYED_ALERT_TS: dict[str, int] = {}

# Job queue (optional SQLite-backed)
try:
    from skills.memory import job_queue as _jq
except ImportError:
    _jq = None  # type: ignore[assignment]

logger = _log  # alias used throughout the original code


# ===================================================================
# init_line_module — wires this module to the running application
# ===================================================================

def init_line_module(app, orch, bot_api, hdlr, **extras):
    """
    Inject runtime dependencies so the module functions can operate.

    Call once during server startup, *after* the Flask app, LINE SDK objects,
    and Orchestrator are initialised.

    ``extras`` may contain optional helpers from server.py:
        normalize_output_text, public_url_for_local_file,
        export_text_to_static, send_telegram_text,
        notify_admin_telegram_once, telegram_send_orchestrator_response,
        append_channel_delivery_audit, maybe_handle_laf_captcha_reply,
        maybe_handle_generic_captcha_reply, web_notifications,
        channel_bg_executor, attachment_bg_executor.
    """
    global orchestrator, line_bot_api, handler, _app
    global TextSendMessage, ImageSendMessage, LineBotApiError
    global _normalize_output_text, _public_url_for_local_file
    global _export_text_to_static, _send_telegram_text
    global _notify_admin_telegram_once, _telegram_send_orchestrator_response
    global _append_channel_delivery_audit
    global _maybe_handle_laf_captcha_reply, _maybe_handle_generic_captcha_reply
    global WEB_NOTIFICATIONS
    global _CHANNEL_BG_EXECUTOR, _ATTACHMENT_BG_EXECUTOR

    _app = app
    orchestrator = orch
    line_bot_api = bot_api
    handler = hdlr

    # LINE SDK types
    try:
        from linebot.models import (
            TextSendMessage as _TSM,
            ImageSendMessage as _ISM,
        )
        from linebot.exceptions import LineBotApiError as _LBAE
        TextSendMessage = _TSM
        ImageSendMessage = _ISM
        LineBotApiError = _LBAE
    except ImportError:
        _log.warning("linebot SDK not available; LINE messaging will fail at runtime.")

    # Optional helpers
    _normalize_output_text = extras.get("normalize_output_text")
    _public_url_for_local_file = extras.get("public_url_for_local_file")
    _export_text_to_static = extras.get("export_text_to_static")
    _send_telegram_text = extras.get("send_telegram_text")
    _notify_admin_telegram_once = extras.get("notify_admin_telegram_once")
    _telegram_send_orchestrator_response = extras.get("telegram_send_orchestrator_response")
    _append_channel_delivery_audit = extras.get("append_channel_delivery_audit")
    _maybe_handle_laf_captcha_reply = extras.get("maybe_handle_laf_captcha_reply")
    _maybe_handle_generic_captcha_reply = extras.get("maybe_handle_generic_captcha_reply")
    WEB_NOTIFICATIONS = extras.get("web_notifications", WEB_NOTIFICATIONS)

    # Thread pools
    _CHANNEL_BG_EXECUTOR = extras.get("channel_bg_executor")
    _ATTACHMENT_BG_EXECUTOR = extras.get("attachment_bg_executor")

    if _CHANNEL_BG_EXECUTOR is None or _ATTACHMENT_BG_EXECUTOR is None:
        try:
            from api.thread_pools import channel_pool, io_pool
            _CHANNEL_BG_EXECUTOR = _CHANNEL_BG_EXECUTOR or channel_pool
            _ATTACHMENT_BG_EXECUTOR = _ATTACHMENT_BG_EXECUTOR or io_pool
        except ImportError:
            _log.warning("api.thread_pools not available; creating fallback executors.")
            _CHANNEL_BG_EXECUTOR = _CHANNEL_BG_EXECUTOR or ThreadPoolExecutor(max_workers=4, thread_name_prefix="line_ch")
            _ATTACHMENT_BG_EXECUTOR = _ATTACHMENT_BG_EXECUTOR or ThreadPoolExecutor(max_workers=2, thread_name_prefix="line_att")

    # Register handler callbacks
    _register_handler_callbacks()


# ===================================================================
# Utility: JSON atomic write (duplicated here to avoid circular import)
# ===================================================================

def _write_json_atomic(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        _log.debug("silent-catch at %s:%s", __name__, "_write_json_atomic", exc_info=True)


def _load_json(path: str) -> dict:
    try:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        return {}
    return {}


# ===================================================================
# 1. LINE sender tracking
# ===================================================================

def _record_last_line_sender(event):
    try:
        src = getattr(event, "source", None)
        user_id = getattr(src, "user_id", None)
        group_id = getattr(src, "group_id", None)
        room_id = getattr(src, "room_id", None)
        payload = {
            "user_id": user_id,
            "group_id": group_id,
            "room_id": room_id,
            "updated_at": int(time.time()),
        }
        with open(LINE_LAST_SENDER_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        _log.debug("silent-catch at %s:%s", __name__, "_record_last_line_sender", exc_info=True)


def _record_last_line_callback(path: str = ""):
    try:
        _write_json_atomic(
            LINE_LAST_CALLBACK_FILE,
            {
                "updated_at": int(time.time()),
                "path": (path or "").strip() or "/callback",
            },
        )
    except Exception:
        _log.debug("silent-catch at %s:%s", __name__, "_record_last_line_callback", exc_info=True)


def _load_last_line_sender_user_id() -> str:
    try:
        if os.path.exists(LINE_LAST_SENDER_FILE):
            with open(LINE_LAST_SENDER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            return (data.get("user_id") or "").strip()
    except Exception:
        return ""
    return ""


# ===================================================================
# 2. Context management & cleanup
# ===================================================================

def _safe_remove_tmp(path: str) -> None:
    """
    Safety: never delete Synology Drive artifacts.
    For temporary files (typically /tmp), allow cleanup via safe_fs.
    """
    p = (path or "").strip()
    if not p:
        return
    try:
        ensure_path_on_sys_path(get_orch_dir())
        import safe_fs  # type: ignore
        safe_fs.safe_remove(p, reason="tmp_cleanup", allow_delete=True)
        return
    except Exception:
        _log.debug("silent-catch at %s:%s", __name__, "_safe_remove_tmp/safe_fs", exc_info=True)
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        _log.debug("silent-catch at %s:%s", __name__, "_safe_remove_tmp/os", exc_info=True)


def _cleanup_user_context():
    now = time.time()
    expired = []
    for uid, ctx in user_context.items():
        ts = float(ctx.get("timestamp", 0) or 0)
        if ts and (now - ts > CONTEXT_TTL_SECONDS):
            expired.append((uid, ctx))
    for uid, ctx in expired:
        path = ctx.get("path")
        if path and os.path.exists(path):
            _safe_remove_tmp(path)
        user_context.pop(uid, None)


def cleanup_old_exports(days: int = 30) -> int:
    """刪除 static/exports/ 中超過 N 天的檔案，回傳刪除數量。"""
    exports_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "static", "exports")
    if not os.path.isdir(exports_dir):
        return 0
    cutoff = time.time() - (days * 86400)
    removed = 0
    for fname in os.listdir(exports_dir):
        fpath = os.path.join(exports_dir, fname)
        try:
            if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                os.remove(fpath)
                removed += 1
        except Exception:
            continue
    if removed:
        _log.info("🧹 Cleaned up %d old exports (>%d days)", removed, days)
    return removed


# ===================================================================
# 3. Attachment job system
# ===================================================================

def _read_attachment_job(job_id: str) -> dict:
    if _jq:
        return _jq.read(job_id)
    # Legacy JSON fallback
    status_path = Path(ATTACHMENT_JOB_DIR) / f"attachment_{job_id}.json"
    if not status_path.exists():
        return {}
    try:
        return json.loads(status_path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}


def _write_attachment_job(job_id: str, patch: dict) -> dict:
    if _jq:
        job = _jq.read(job_id)
        status = str(patch.get("status") or "").strip()
        if status == "done":
            _jq.complete(job_id, result=str(patch.get("response_preview") or ""))
        elif status == "failed":
            _jq.fail(job_id, error=str(patch.get("error") or ""))
        elif status == "abandoned":
            _jq.abandon(job_id, reason=str(patch.get("abandon_reason") or patch.get("error") or ""))
        elif status == "running":
            _jq.claim(job_id)
        payload_patch = {}
        for key in (
            "progress",
            "progress_total",
            "progress_phase",
            "progress_message",
            "progress_current",
            "updated_at_iso",
            "finished_at",
            "success",
            "response_preview",
            "error",
        ):
            if key in patch:
                payload_patch[key] = patch.get(key)
        if payload_patch:
            job = _jq.update_payload(job_id, payload_patch)
        else:
            job = _jq.read(job_id)
        job.update(patch)
        return job
    # Legacy JSON fallback
    os.makedirs(ATTACHMENT_JOB_DIR, exist_ok=True)
    status_path = Path(ATTACHMENT_JOB_DIR) / f"attachment_{job_id}.json"
    data = _read_attachment_job(job_id)
    data.update(patch or {})
    data["job_id"] = job_id
    data["updated_at"] = datetime.now().isoformat()
    tmp_path = status_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(status_path)
    return data


def _list_attachment_job_ids() -> list[str]:
    if _jq:
        return [j["id"] for j in _jq.list_all(limit=500)]
    base = Path(ATTACHMENT_JOB_DIR)
    if not base.exists():
        return []
    files = sorted(base.glob("attachment_*.json"), key=lambda p: p.stat().st_mtime)
    return [p.stem.replace("attachment_", "", 1) for p in files]


def _persist_attachment_copy(src_path: str, filename: str = "", prefix: str = "attachment") -> str:
    src = str(src_path or "").strip()
    if not src or not os.path.exists(src):
        return ""
    try:
        src_real = os.path.realpath(src)
        dst_root = os.path.realpath(ATTACHMENT_JOB_FILE_DIR)
        if src_real.startswith(dst_root + os.sep) or src_real == dst_root:
            return src
    except Exception:
        _log.debug("silent-catch at %s:%s", __name__, "_persist_attachment_copy", exc_info=True)
    os.makedirs(ATTACHMENT_JOB_FILE_DIR, exist_ok=True)
    ext = os.path.splitext(str(filename or src).strip())[1] or os.path.splitext(src)[1] or ".bin"
    safe_prefix = re.sub(r"[^A-Za-z0-9_-]+", "_", str(prefix or "attachment"))[:48] or "attachment"
    dst = os.path.join(ATTACHMENT_JOB_FILE_DIR, f"{safe_prefix}_{uuid.uuid4().hex[:10]}{ext}")
    shutil.copy2(src, dst)
    return dst


def _persist_attachment_payload(attachment: dict | None, *, prefix: str) -> dict | None:
    if not isinstance(attachment, dict):
        return None
    src_path = str(attachment.get("path") or "").strip()
    if not src_path or not os.path.exists(src_path):
        return None
    filename = str(attachment.get("filename") or os.path.basename(src_path) or "attachment").strip()
    dst_path = _persist_attachment_copy(src_path, filename=filename, prefix=prefix)
    if not dst_path:
        return None
    out = dict(attachment)
    out["path"] = dst_path
    out["filename"] = filename
    out["timestamp"] = float(attachment.get("timestamp") or time.time())
    return out


def _line_push_orchestrator_response(user_id: str, response_text: str) -> None:
    text = str(response_text or "").strip()
    if not text:
        _line_push_text(user_id, "⚠️ 任務完成，但沒有可用輸出。")
        return
    if "|||FILE_PATH|||" in text:
        try:
            text_part, file_path = text.split("|||FILE_PATH|||", 1)
            file_url = _public_url_for_local_file((file_path or "").strip()) if _public_url_for_local_file else ""
            if file_url:
                body = (text_part or "").strip()
                msg = (body + "\n\n" if body else "") + f"📎 檔案下載：{file_url}"
                _line_push_text(user_id, msg)
                return
            _line_push_text(user_id, f"{(text_part or '').strip()}\n⚠️ 檔案已產生，但目前無法建立公開下載連結。")
            return
        except Exception as file_err:
            logger.error(f"❌ LINE push file response failed: {file_err}")
            _line_push_text(user_id, "❌ 檔案處理失敗，請稍後再試。")
            return
    _line_push_text(user_id, text)


def _deliver_attachment_job_response(job: dict, response_text: str) -> None:
    platform_name = str(job.get("platform") or "").strip().upper()
    if platform_name == "TELEGRAM":
        if _telegram_send_orchestrator_response:
            _telegram_send_orchestrator_response(
                str(job.get("chat_id") or "").strip(),
                str(response_text or ""),
                reply_to_message_id=int(job.get("reply_to_message_id") or 0) or None,
            )
        return
    if platform_name == "LINE":
        _line_push_orchestrator_response(str(job.get("user_id") or "").strip(), str(response_text or ""))
        return
    logger.warning("⚠️ Unknown attachment job platform: %s", platform_name)


def _run_attachment_job(job_id: str) -> None:
    if _jq:
        if not _jq.claim(job_id):
            return
        job = _jq.read(job_id)
    else:
        job = _read_attachment_job(job_id)
    if not job:
        return

    try:
        # SQLite stores attachment in payload dict; legacy JSON stores it flat
        _payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
        attachment = (
            _payload.get("attachment")
            if isinstance(_payload.get("attachment"), dict) else
            job.get("attachment") if isinstance(job.get("attachment"), dict) else
            None
        )

        # Progress callback — push intermediate status to user during long tasks.
        import time as _progress_time
        _last_progress = [0.0]
        def _job_progress_cb(phase, current, total, message):
            now = _progress_time.monotonic()
            if now - _last_progress[0] < 15:
                return
            _last_progress[0] = now
            try:
                total_i = max(1, int(total or 1))
                current_i = max(0, min(total_i, int(current or 0)))
                progress = int(max(0, min(100, round((current_i / total_i) * 100))))
            except Exception:
                current_i, total_i, progress = 0, 1, 0
            _write_attachment_job(
                job_id,
                {
                    "status": "running",
                    "progress": progress,
                    "progress_current": current_i,
                    "progress_total": total_i,
                    "progress_phase": str(phase or ""),
                    "progress_message": str(message or ""),
                    "updated_at_iso": datetime.now().isoformat(),
                },
            )
            try:
                _deliver_attachment_job_response(job, str(message or ""))
            except Exception:
                _log.debug("silent-catch at %s:%s", __name__, "_run_attachment_job/progress", exc_info=True)

        response_text = orchestrator.process_message(
            user_id=str(job.get("user_id") or "").strip(),
            message=str(job.get("user_text") or ""),
            platform=str(job.get("platform") or "LINE"),
            role=str(job.get("role") or "user"),
            attachment=attachment,
            progress_callback=_job_progress_cb,
        )
        if response_text:
            try:
                orchestrator.record_assistant_reply(str(job.get("user_id") or "").strip(), response_text)
            except Exception:
                _log.debug("silent-catch at %s:%s", __name__, "_run_attachment_job/record", exc_info=True)
        _deliver_attachment_job_response(job, str(response_text or ""))
        _write_attachment_job(
            job_id,
            {
                "status": "done",
                "success": True,
                "progress": 100,
                "finished_at": datetime.now().isoformat(),
                "response_preview": str(response_text or "")[:1200],
            },
        )
    except Exception as e:
        err = str(e)
        logger.error("❌ Attachment job failed job_id=%s error=%s", job_id, err)
        _write_attachment_job(
            job_id,
            {
                "status": "failed",
                "success": False,
                "progress": 100,
                "finished_at": datetime.now().isoformat(),
                "error": err,
            },
        )
        try:
            _deliver_attachment_job_response(job, f"❌ 系統處理失敗：{err}")
        except Exception:
            _log.debug("silent-catch at %s:%s", __name__, "_run_attachment_job/fail_deliver", exc_info=True)
    finally:
        pass  # SQLite job_queue handles state; no lock file to release


def _enqueue_attachment_job(
    *,
    platform_name: str,
    user_id: str,
    role: str,
    user_text: str,
    attachment: dict | None = None,
    chat_id: str = "",
    reply_to_message_id: int | None = None,
) -> dict:
    durable_attachment = _persist_attachment_payload(attachment, prefix=f"{platform_name.lower()}_att") if attachment else None
    if _jq:
        job_id = _jq.enqueue(
            job_type="attachment",
            platform=platform_name,
            user_id=user_id,
            role=role,
            user_text=user_text,
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            payload={"attachment": durable_attachment} if durable_attachment else {},
        )
    else:
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        payload = {
            "status": "queued",
            "platform": str(platform_name or "LINE"),
            "user_id": str(user_id or "").strip(),
            "role": str(role or "user"),
            "user_text": str(user_text or ""),
            "attachment": durable_attachment,
            "chat_id": str(chat_id or "").strip(),
            "reply_to_message_id": int(reply_to_message_id or 0) or None,
            "created_at": datetime.now().isoformat(),
            "worker_pid": 0,
            "attempts": 0,
        }
        _write_attachment_job(job_id, payload)
    if durable_attachment:
        try:
            orchestrator.remember_recent_attachment(
                user_id=str(user_id or "").strip(),
                platform=str(platform_name or ""),
                attachment=durable_attachment,
                source_message=str(user_text or ""),
            )
        except Exception as recent_err:
            logger.warning(f"⚠️ Failed to remember recent attachment for job {job_id}: {recent_err}")
    _ATTACHMENT_BG_EXECUTOR.submit(_run_attachment_job, job_id)
    return {"success": True, "job_id": job_id}


def _resume_pending_attachment_jobs() -> None:
    _MAX_RESUME_ATTEMPTS = int(os.environ.get("MAGI_ATTACHMENT_MAX_RESUME", "3") or "3")
    if _jq:
        resumed, abandoned = _jq.recover_stale_running(max_attempts=_MAX_RESUME_ATTEMPTS)
        # Re-submit recovered jobs to executor
        for job in _jq.list_by_status("queued"):
            _ATTACHMENT_BG_EXECUTOR.submit(_run_attachment_job, job["id"])
        # Periodic cleanup of old completed jobs
        _jq.cleanup_old(days=30)
        return
    # Legacy JSON fallback
    resumed = 0
    abandoned = 0
    for job_id in _list_attachment_job_ids():
        job = _read_attachment_job(job_id)
        status = str(job.get("status") or "").strip().lower()
        if status not in {"queued", "running"}:
            continue
        attempts = int(job.get("attempts") or 0)
        if attempts >= _MAX_RESUME_ATTEMPTS:
            _write_attachment_job(job_id, {**job, "status": "abandoned", "abandon_reason": f"exceeded {_MAX_RESUME_ATTEMPTS} attempts"})
            abandoned += 1
            continue
        _ATTACHMENT_BG_EXECUTOR.submit(_run_attachment_job, job_id)
        resumed += 1
    if resumed or abandoned:
        logger.info("♻️ Resumed %s pending attachment jobs, abandoned %s.", resumed, abandoned)


# ===================================================================
# 4. Messaging utilities
# ===================================================================

def _chunk_text_for_line(text: str, limit: int = 4200) -> list[str]:
    s = (text or "").strip()
    if not s:
        return []
    limit = max(300, int(limit))
    out = []
    i = 0
    while i < len(s):
        out.append(s[i : i + limit])
        i += limit
    return out


def _likely_long_task(user_text: str, attachment: dict | None) -> bool:
    if attachment:
        return True
    t = (user_text or "").lower()
    if re.search(r"https?://", t):
        return True
    if any(
        k in t
        for k in [
            # Reading/translation/summarization
            "翻譯", "translate", "摘要", "總結", "整理", "讀取", "分析", "文件", "檔案", "網頁", "網址",
            "全文", "整篇", "整份", "完整翻譯", "全文翻譯", "逐字稿", "時間戳", "時間碼",
            # Research/fetch
            "搜尋", "search", "抓取", "fetch", "research",
            # Heavier reasoning
            "深度思考", "deep think",
            # Media generation/processing
            "畫", "draw", "產生圖片", "generate image", "製作音樂", "生成音樂",
        ]
    ):
        return True
    if len(t) > 1200:
        return True
    return False


# ===================================================================
# 5. Push budget
# ===================================================================

def _line_push_budget_ok() -> bool:
    """Check if daily LINE push budget still has room (free plan = 200/month ≈ 6/day)."""
    today = time.strftime("%Y-%m-%d")
    try:
        if os.path.exists(_LINE_PUSH_COUNTER_FILE):
            with open(_LINE_PUSH_COUNTER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        else:
            data = {}
        return int(data.get(today, 0)) < _LINE_PUSH_DAILY_LIMIT
    except Exception:
        return True


def _line_push_budget_increment() -> None:
    """Record one push message for today's budget."""
    today = time.strftime("%Y-%m-%d")
    try:
        data = {}
        if os.path.exists(_LINE_PUSH_COUNTER_FILE):
            with open(_LINE_PUSH_COUNTER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
        # Clean old dates
        data = {k: v for k, v in data.items() if k >= time.strftime("%Y-%m", time.localtime())}
        data[today] = int(data.get(today, 0)) + 1
        with open(_LINE_PUSH_COUNTER_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        _log.debug("silent-catch at %s:%s", __name__, "_line_push_budget_increment", exc_info=True)


def _line_push_text(user_id: str, text: str, *, is_chat_reply: bool = False) -> bool:
    """
    Push a (possibly long) text message to LINE user.
    Push is used for long tasks to avoid reply_token expiry.

    Args:
        is_chat_reply: True if this is a fallback for an expired reply_token
                       (user-initiated conversation). Chat replies are never
                       budget-limited — the user should always get a response.
    """
    if not is_chat_reply and not _line_push_budget_ok():
        logger.warning("LINE push skipped — daily notification budget exhausted (%d/%d). "
                       "Message will only appear in TG.", _LINE_PUSH_DAILY_LIMIT, _LINE_PUSH_DAILY_LIMIT)
        return False
    ok_all = True
    safe_text = _normalize_line_output_text(text)
    for part in _chunk_text_for_line(safe_text, limit=4200):
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=part))
            _line_push_budget_increment()
        except Exception as e:
            logger.error(f"❌ LINE push failed: {e}")
            _handle_line_send_failure(e, user_id=user_id, phase="push")
            ok_all = False
            break
    return ok_all


# ===================================================================
# 6. Outgoing memory & last-outgoing tracking
# ===================================================================

def _remember_last_line_outgoing(user_id: str, text: str) -> None:
    try:
        uid = str(user_id or "").strip()
        if not uid:
            return
        body = str(text or "").strip()
        if not body:
            return
        _LINE_LAST_OUTGOING[uid] = {"ts": int(time.time()), "text": body[:12000]}
    except Exception:
        _log.debug("silent-catch at %s:%s", __name__, "_remember_last_line_outgoing", exc_info=True)


def _last_line_outgoing_preview(user_id: str) -> str:
    try:
        uid = str(user_id or "").strip()
        item = _LINE_LAST_OUTGOING.get(uid) or {}
        text = str(item.get("text") or "").strip()
        if not text:
            return ""
        text = re.sub(r"\s+", " ", text)
        return text[:280]
    except Exception:
        return ""


# ===================================================================
# 7. Config loading (openclaw cfg, telegram channel state)
# ===================================================================

def _load_openclaw_cfg() -> dict:
    def _raw_load() -> dict:
        try:
            p = Path.home() / ".openclaw" / "openclaw.json"
            if p.exists():
                data = json.loads(p.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}
        return {}

    cfg = _raw_load()
    try:
        channels = cfg.setdefault("channels", {})
        tg = channels.setdefault("telegram", {})
        legacy_notify = tg.get("notifyTo")
        legacy_topic_map = tg.get("topicMap")
        if isinstance(legacy_notify, list) or isinstance(legacy_topic_map, dict):
            state = _load_telegram_channel_state()
            changed_cfg = False
            changed_state = False
            if isinstance(legacy_notify, list):
                merged = [str(x).strip() for x in legacy_notify if str(x).strip()]
                for item in merged:
                    if item not in state["notifyTo"]:
                        state["notifyTo"].append(item)
                        changed_state = True
                tg.pop("notifyTo", None)
                changed_cfg = True
            if isinstance(legacy_topic_map, dict):
                for key, value in legacy_topic_map.items():
                    try:
                        tid = int(value or 0)
                    except Exception:
                        tid = 0
                    if key and tid > 0 and int(state["topicMap"].get(str(key)) or 0) != tid:
                        state["topicMap"][str(key)] = tid
                        changed_state = True
                tg.pop("topicMap", None)
                changed_cfg = True
            if changed_state:
                _save_telegram_channel_state(state)
            if changed_cfg:
                _save_openclaw_cfg(cfg)
    except Exception as e:
        logger.warning(f"⚠️ telegram channel-state migration skipped: {e}")
    return cfg


def _load_telegram_channel_state() -> dict:
    path = Path(f"{_MAGI_ROOT}/.agent/telegram_channel_state.json")
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                notify_to = data.get("notifyTo")
                topic_map = data.get("topicMap")
                return {
                    "notifyTo": [str(x).strip() for x in notify_to if str(x).strip()] if isinstance(notify_to, list) else [],
                    "topicMap": {
                        str(k): int(v)
                        for k, v in (topic_map or {}).items()
                        if str(k).strip() and str(v).strip() and int(v or 0) > 0
                    } if isinstance(topic_map, dict) else {},
                }
    except Exception:
        _log.debug("silent-catch at %s:%s", __name__, "_load_telegram_channel_state", exc_info=True)
    return {"notifyTo": [], "topicMap": {}}


def _save_telegram_channel_state(state: dict) -> bool:
    try:
        path = Path(f"{_MAGI_ROOT}/.agent/telegram_channel_state.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "notifyTo": [str(x).strip() for x in (state.get("notifyTo") or []) if str(x).strip()],
            "topicMap": {
                str(k): int(v)
                for k, v in (state.get("topicMap") or {}).items()
                if str(k).strip() and int(v or 0) > 0
            },
        }
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{int(time.time()*1000)}.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except Exception as e:
        logger.warning(f"⚠️ save telegram channel state failed: {e}")
        return False


def _save_openclaw_cfg(cfg: dict) -> bool:
    try:
        p = Path.home() / ".openclaw" / "openclaw.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.{int(time.time()*1000)}.tmp")
        tmp.write_text(
            json.dumps(cfg or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(p)
        return True
    except Exception as e:
        logger.warning(f"⚠️ save openclaw config failed: {e}")
        return False


# ===================================================================
# 8. Quota & delay management
# ===================================================================

def _enqueue_line_delayed(user_id: str, phase: str, reason: str, preview: str) -> None:
    try:
        items = []
        if os.path.exists(LINE_DELAY_QUEUE_FILE):
            raw = json.loads(Path(LINE_DELAY_QUEUE_FILE).read_text(encoding="utf-8"))
            if isinstance(raw, list):
                items = raw
        items.append(
            {
                "ts": int(time.time()),
                "user_id": str(user_id or "").strip(),
                "phase": str(phase or "").strip(),
                "reason": str(reason or "").strip(),
                "preview": str(preview or "").strip(),
                "status": "queued",
            }
        )
        if len(items) > 300:
            items = items[-300:]
        Path(LINE_DELAY_QUEUE_FILE).write_text(
            json.dumps(items, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning(f"⚠️ Failed to enqueue LINE delayed item: {e}")


def _line_quota_active(window_sec: int | None = None) -> bool:
    """
    Best-effort detector for recent LINE quota exhaustion.
    If quota alert was raised recently, skip repeated LINE send attempts and
    use cross-channel notice + delayed queue instead.
    """
    ws = int(window_sec or os.environ.get("LINE_QUOTA_ACTIVE_WINDOW_SEC", "600") or "600")
    ws = max(60, ws)
    try:
        if not os.path.exists(LINE_QUOTA_ALERT_FILE):
            return False
        obj = json.loads(Path(LINE_QUOTA_ALERT_FILE).read_text(encoding="utf-8")) or {}
        ts = int(obj.get("ts") or 0)
        if ts <= 0:
            return False
        return (int(time.time()) - ts) < ws
    except Exception:
        return False


def _should_emit_line_delay_notice(user_id: str, phase: str, dedupe_sec: int = 180) -> bool:
    key = f"{str(user_id or '').strip()}::{str(phase or '').strip()}"
    now = int(time.time())
    last = int(_LINE_DELAYED_ALERT_TS.get(key) or 0)
    if now - last < max(30, int(dedupe_sec)):
        return False
    _LINE_DELAYED_ALERT_TS[key] = now
    return True


def _fanout_line_delayed_notice(user_id: str, phase: str, preview: str) -> None:
    if not _should_emit_line_delay_notice(user_id=user_id, phase=phase, dedupe_sec=180):
        return
    body = (
        "⚠️ LINE 回覆已延遲（額度用盡，已加入佇列）\n"
        f"來源使用者：{user_id}\n"
        f"流程：{phase}\n"
        f"內容摘要：{(preview or '（空）')[:260]}"
    )
    if _send_telegram_text:
        _send_telegram_text(body)


def _handle_line_send_failure(err: Exception, user_id: str, phase: str, failed_text: str = "") -> None:
    """
    Detect quota/429 failures and alert admin on Telegram so LINE silence is explainable.
    """
    status = None
    try:
        status = int(getattr(err, "status_code", 0) or 0)
    except Exception:
        status = None
    msg = str(err or "")
    low = msg.lower()
    if status == 429 or ("monthly limit" in low):
        logger.error(f"⛔ LINE quota reached (phase={phase}, user={user_id}).")
        preview = (failed_text or "").strip() or _last_line_outgoing_preview(user_id)
        _enqueue_line_delayed(
            user_id=user_id,
            phase=phase,
            reason=f"line_quota_{status or 429}",
            preview=preview[:500],
        )
        _fanout_line_delayed_notice(user_id=user_id, phase=phase, preview=preview)
        if _notify_admin_telegram_once:
            _notify_admin_telegram_once(
                "⛔ LINE 額度已達上限（API 429），LINE 可能暫時無法回覆。請先改用 TG/DC 或更換可用 token。",
                dedupe_sec=1800,
            )


# ===================================================================
# 9. Output normalization
# ===================================================================

def _normalize_line_output_text(text: str, skip_llm: bool = False) -> str:
    s = (text or "").strip()
    if not s:
        return s
    try:
        if _normalize_output_text:
            if skip_llm:
                # Deterministic replacements only — skip slow TAIDE LLM review.
                from api.tw_output_guard import _opencc_s2twp, _replace_mainland_terms, _strip_internal_leaks, _limit_message_for_platform, strip_markdown_for_chat
                s = _opencc_s2twp(s)
                s, _ = _replace_mainland_terms(s)
                s, _ = _strip_internal_leaks(s)
                s = strip_markdown_for_chat(s)
                return _limit_message_for_platform(s, platform="LINE")
            return _normalize_output_text(s, platform="LINE")
    except Exception as e:
        logger.warning(f"⚠️ Taiwan wording guard skipped: {e}")
    return s


# ===================================================================
# 10. Orchestrator notification registration
# ===================================================================

def _register_orchestrator_notifications():
    """
    Wire Orchestrator async notifications to LINE push so long-running tasks can
    provide progress updates (without depending on reply_token).
    """
    try:
        def _cb(uid: str, msg: str, platform: str = "LINE"):
            if (platform or "").upper() == "WEB":
                WEB_NOTIFICATIONS[str(uid)].append(msg)
                return
            if (platform or "").upper() != "LINE":
                return
            _line_push_text(uid, msg)

        orchestrator.register_callback(_cb)
        logger.info("🔔 Orchestrator notifications enabled for LINE push.")
    except Exception as e:
        logger.warning(f"⚠️ Failed to register orchestrator notifications: {e}")


# ===================================================================
# 11. LINE send text / send messages (best-effort delivery)
# ===================================================================

def _line_send_text(event, user_id: str, text: str, prefer_push: bool = False, skip_llm: bool = False) -> bool:
    """
    Best-effort delivery:
    - If prefer_push or text is long: push (chunked).
    - Else: reply; if reply_token expired/invalid or any failure, fall back to push.
    This prevents "opened then no response" caused by reply_token expiry.
    """
    s = _normalize_line_output_text(text, skip_llm=skip_llm)
    if not s:
        return True
    _remember_last_line_outgoing(user_id, s)

    # If the content is too long for chat, export to TXT and send a link (best effort).
    if EXPORT_LONG_TEXT and _export_text_to_static and len(s) >= max(2000, int(EXPORT_TEXT_THRESHOLD)):
        exported = _export_text_to_static(s, prefix="casper_reply")
        if exported.get("success"):
            url = (exported.get("url") or "").strip()
            if url:
                msg = (
                    "內容比較長，我先幫你整理成 TXT 檔方便下載：\n"
                    f"{url}\n\n"
                    "如果你點不開這個連結，請把你目前對外的公開網址（例如網域/TS Funnel）貼給我，我會改用那個網址產生連結。"
                )
            else:
                msg = (
                    "內容比較長，我已輸出成 TXT 檔（目前尚未取得可公開下載的網址）。\n"
                    f"檔案位置：{exported.get('path')}\n"
                    "如果你有對外的公開網址（例如網域/TS Funnel），設定 `MAGI_PUBLIC_BASE_URL` 後我就能改用連結傳給你。"
                )
            return _line_push_text(user_id, msg, is_chat_reply=True)

    # Avoid reply_message size limit and reduce chance of token expiry.
    if prefer_push or len(s) > 4200:
        return _line_push_text(user_id, s, is_chat_reply=True)

    try:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=s))
        return True
    except Exception as e:
        # Typical failure: reply_token expired (400) or already used. Push is still allowed.
        try:
            if LineBotApiError and isinstance(e, LineBotApiError):
                logger.warning(f"⚠️ LINE reply failed (status={getattr(e, 'status_code', '?')}), fallback push: {e}")
            else:
                logger.warning(f"⚠️ LINE reply failed, fallback push: {e}")
        except Exception:
            _log.debug("silent-catch at %s:%s", __name__, "_line_send_text/warn", exc_info=True)
        _handle_line_send_failure(e, user_id=user_id, phase="reply", failed_text=s)
        return _line_push_text(user_id, s, is_chat_reply=True)


def _line_send_messages(event, user_id: str, messages, prefer_push: bool = False) -> bool:
    """
    Best-effort delivery for multi-message payloads (e.g., text + image).
    Falls back to push on reply failure.
    """
    if not messages:
        return True
    try:
        guarded = []
        text_parts = []
        for m in messages:
            if isinstance(m, TextSendMessage):
                txt = _normalize_line_output_text(getattr(m, "text", ""))
                guarded.append(TextSendMessage(text=txt))
                if txt:
                    text_parts.append(txt)
            else:
                guarded.append(m)
        messages = guarded
        if text_parts:
            _remember_last_line_outgoing(user_id, "\n".join(text_parts))
    except Exception as guard_err:
        logger.warning(f"⚠️ LINE message guard skipped: {guard_err}")
    try:
        if prefer_push:
            line_bot_api.push_message(user_id, messages)
            return True
        line_bot_api.reply_message(event.reply_token, messages)
        return True
    except Exception as e:
        logger.warning(f"⚠️ LINE send messages failed, fallback push: {e}")
        _handle_line_send_failure(e, user_id=user_id, phase="reply_messages")
        try:
            line_bot_api.push_message(user_id, messages)
            return True
        except Exception as push_err:
            logger.error(f"❌ LINE push messages failed: {push_err}")
            _handle_line_send_failure(push_err, user_id=user_id, phase="push_messages")
            return False


# ===================================================================
# 12. Message processing (process_message_async)
# ===================================================================

def process_message_async(event, user_id, user_text, attachment, role="user", long_task: bool | None = None, already_acked: bool = False, _mq_msg_id: str | None = None):
    # OBS-1: correlation ID  /  OBS-2: latency tracking
    correlation_id = f"magi-{uuid.uuid4().hex[:12]}"
    _start_ts = time.monotonic()
    # Message-queue: claim
    _mq_inst = None
    if _mq_msg_id:
        try:
            from skills.memory.message_queue import get_queue as _get_mq
            _mq_inst = _get_mq()
            _mq_inst.claim(_mq_msg_id)
        except Exception:
            _log.debug("silent-catch at %s:%s", __name__, "process_message_async/mq_claim", exc_info=True)
    try:
        if long_task is None:
            long_task = _likely_long_task(user_text, attachment)
        if long_task and not already_acked:
            # Reply quickly (within reply_token lifetime), then push final result.
            ack_msg = "⏳ 已收到，正在處理中。完成後我會用推播回覆結果。"
            if attachment and attachment.get("type") in ("file", "audio", "image"):
                try:
                    att_path = attachment.get("path", "")
                    att_size = os.path.getsize(att_path) if att_path and os.path.exists(att_path) else 0
                    att_fname = attachment.get("filename") or os.path.basename(att_path) or "附件"
                    if att_size > 0:
                        from api.orchestrator import Orchestrator
                        ack_msg = Orchestrator.estimate_file_processing_time(
                            file_size_bytes=att_size,
                            filename=att_fname,
                            prompt=user_text or "",
                            file_path=att_path,
                        )
                except Exception:
                    _log.debug("silent-catch at %s:%s", __name__, "process_message_async/ack_est", exc_info=True)
            _line_send_text(event, user_id, ack_msg, prefer_push=False)

        if attachment:
            logger.info(f"📎 Processing attachment for {user_id}: {attachment['type']} cid={correlation_id}")
            response_text = orchestrator.process_message(
                user_id,
                user_text,
                platform="LINE",
                attachment=attachment,
                role=role,
                correlation_id=correlation_id,
            )
        else:
            logger.info(f"📩 Processing text for {user_id}: {user_text} cid={correlation_id}")
            response_text = orchestrator.process_message(user_id, user_text, platform="LINE", role=role, correlation_id=correlation_id)

        if response_text:
            try:
                orchestrator.record_assistant_reply(user_id, response_text)
            except Exception as track_err:
                logger.warning(f"⚠️ Failed to track assistant reply for {user_id}: {track_err}")

            # If LINE is still in quota-limited window, avoid repeated failed sends.
            if _line_quota_active():
                preview = _normalize_line_output_text(response_text)
                _enqueue_line_delayed(
                    user_id=user_id,
                    phase="result",
                    reason="line_quota_active",
                    preview=(preview or "")[:500],
                )
                _fanout_line_delayed_notice(user_id=user_id, phase="result", preview=preview)
                return

            if "|||IMAGE_PATH|||" in response_text:
                try:
                    text_part, image_path = response_text.split("|||IMAGE_PATH|||", 1)
                    image_path = (image_path or "").strip()

                    if not image_path or not os.path.exists(image_path):
                        msg = f"{text_part}\n⚠️ Image file not found at path."
                        _line_send_text(event, user_id, msg, prefer_push=long_task)
                        return

                    # Serve image via local static files + Cloudflare Tunnel (no Imgur needed)
                    image_url = _public_url_for_local_file(image_path) if _public_url_for_local_file else ""
                    if not image_url:
                        msg = f"{text_part}\n⚠️ 無法建立圖片公開連結（tunnel 可能未啟動）。"
                        _line_send_text(event, user_id, msg, prefer_push=long_task)
                        return
                    logger.info(f"🖼️ Serving image via tunnel: {image_url}")
                    messages = [TextSendMessage(text=text_part)]
                    if image_url:
                        messages.append(
                            ImageSendMessage(
                                original_content_url=image_url,
                                preview_image_url=image_url,
                            )
                        )
                    ok = _line_send_messages(event, user_id, messages, prefer_push=long_task)
                    if not ok:
                        _line_push_text(user_id, f"{text_part}\n⚠️ 圖片傳送失敗（已嘗試 reply/push）。")
                except Exception as img_err:
                    logger.error(f"❌ Failed to send image to LINE: {img_err}")
                    msg = f"{response_text}\n(Image send failed: {img_err})"
                    _line_send_text(event, user_id, msg, prefer_push=long_task)
            elif "|||FILE_PATH|||" in response_text:
                try:
                    text_part, file_path = response_text.split("|||FILE_PATH|||", 1)
                    file_path = (file_path or "").strip()
                    file_url = _public_url_for_local_file(file_path) if _public_url_for_local_file else ""
                    if file_url:
                        body = (text_part or "").strip()
                        msg = (body + "\n\n" if body else "") + f"📎 檔案下載：{file_url}"
                        _line_send_text(event, user_id, msg, prefer_push=long_task)
                    else:
                        msg = f"{(text_part or '').strip()}\n⚠️ 檔案已產生，但目前無法建立公開下載連結。"
                        _line_send_text(event, user_id, msg, prefer_push=long_task)
                except Exception as file_err:
                    logger.error(f"❌ Failed to send file link to LINE: {file_err}")
                    _line_send_text(event, user_id, "❌ 檔案處理失敗，請稍後再試。", prefer_push=long_task)
            else:
                # Static system responses (command tables, status) don't need LLM review.
                _skip = response_text.lstrip().startswith(("🛠️", "📊", "✅ 系統", "⚡", "🔧", "📋"))
                _line_send_text(event, user_id, response_text, prefer_push=long_task, skip_llm=_skip)
        # Message-queue: mark success
        if _mq_inst and _mq_msg_id:
            try:
                _mq_inst.complete(_mq_msg_id)
            except Exception:
                _log.debug("silent-catch at %s:%s", __name__, "process_message_async/mq_complete", exc_info=True)
    except Exception as e:
        logger.error(f"❌ Async Processing Error: {e}")
        # Message-queue: mark failure (may retry)
        if _mq_inst and _mq_msg_id:
            try:
                _mq_inst.fail(_mq_msg_id, str(e))
            except Exception:
                _log.debug("silent-catch at %s:%s", __name__, "process_message_async/mq_fail", exc_info=True)
        try:
            # Best effort: reply if possible, otherwise push.
            _line_send_text(event, user_id, "❌ 系統暫時忙碌，請稍後再試。", prefer_push=False)
        except Exception:
            _log.debug("silent-catch at %s:%s", __name__, "process_message_async/err_reply", exc_info=True)
    finally:
        # Clean up attachment temp file after processing completes (success or failure)
        if attachment:
            att_path = str(attachment.get("path") or "").strip()
            if att_path and att_path.startswith("/tmp/"):
                try:
                    if os.path.exists(att_path):
                        _safe_remove_tmp(att_path)
                except Exception:
                    _log.debug("silent-catch at %s:%s", __name__, "process_message_async/att_cleanup", exc_info=True)
        # OBS-2: record processing latency
        elapsed_ms = int((time.monotonic() - _start_ts) * 1000)
        if _append_channel_delivery_audit:
            _append_channel_delivery_audit({
                "platform": "LINE",
                "kind": "latency",
                "user_id": str(user_id or ""),
                "correlation_id": correlation_id,
                "latency_ms": elapsed_ms,
            })


# ===================================================================
# 13. Handler callbacks (handle_message, handle_content)
#     Registered on the LINE handler object via _register_handler_callbacks().
# ===================================================================

def _register_handler_callbacks():
    """Register handle_message and handle_content on the LINE handler object."""
    if handler is None:
        _log.warning("LINE handler not set; skipping callback registration.")
        return

    from linebot.models import (
        MessageEvent,
        TextMessage,
        ImageMessage,
        AudioMessage,
        FileMessage,
    )

    @handler.add(MessageEvent, message=TextMessage)
    def handle_message(event):
        user_id = event.source.user_id
        user_text = event.message.text
        user_text_norm = (user_text or "").strip().lower()
        _cleanup_user_context()
        _record_last_line_sender(event)

        # Inject request context for structured logging (cleared at next request)
        from skills.ops.structured_log import set_request_context
        set_request_context(request_id=uuid.uuid4().hex[:12], user_id=user_id, platform="LINE")

        # Fast-path health probe: avoid LLM queueing for simple connectivity checks.
        if user_text_norm in {"連線測試", "連線測試。", "ping", "test", "連線", "測試連線"}:
            try:
                from datetime import datetime
                ts = datetime.now().strftime("%H:%M:%S")
                _line_send_text(event, user_id, f"✅ 連線正常（{ts}）", prefer_push=False)
            except Exception as probe_err:
                logger.warning(f"⚠️ Fast-path LINE probe reply failed: {probe_err}")
                _line_push_text(user_id, "✅ 連線正常")
            return 'OK'

        # Intercept LAF captcha replies (admin human-in-the-loop) before Orchestrator.
        try:
            if _maybe_handle_laf_captcha_reply and _maybe_handle_laf_captcha_reply(event, user_id, user_text):
                return 'OK'
        except Exception as _cap_err:
            logger.warning(f"⚠️ LAF captcha intercept failed: {_cap_err}")

        # Generic captcha broker (used by other modules, e.g. file review / transcripts)
        try:
            if _maybe_handle_generic_captcha_reply and _maybe_handle_generic_captcha_reply(event, user_id, user_text):
                return 'OK'
        except Exception as _cap2_err:
            logger.warning(f"⚠️ Generic captcha intercept failed: {_cap2_err}")

        # If quota was recently hit, queue user request immediately and notify via TG/DC.
        if _line_quota_active():
            preview = re.sub(r"\s+", " ", (user_text or "")).strip()[:500]
            _enqueue_line_delayed(
                user_id=user_id,
                phase="incoming",
                reason="line_quota_active",
                preview=preview,
            )
            _fanout_line_delayed_notice(user_id=user_id, phase="incoming", preview=preview)

        # Role Check
        role = "user"
        if user_id in ADMIN_LINE_USER_IDS:
            role = "admin"
        elif LINE_AUTO_ADMIN_LAST_SENDER and (not ADMIN_LINE_USER_IDS) and user_id and user_id == _load_last_line_sender_user_id():
            role = "admin"

        # Check Context
        attachment = user_context.get(user_id)
        if attachment:
            ts = float(attachment.get("timestamp", 0) or 0)
            if ts and (time.time() - ts > CONTEXT_TTL_SECONDS):
                stale_path = attachment.get("path")
                if stale_path and os.path.exists(stale_path):
                    _safe_remove_tmp(stale_path)
                attachment = None
            user_context.pop(user_id, None)

        recent_followup = False
        try:
            recent_followup = orchestrator.has_recent_attachment_followup(user_id, "LINE", user_text)
        except Exception as recent_err:
            logger.warning(f"⚠️ LINE recent attachment probe failed: {recent_err}")

        # If this looks like a long task, ACK synchronously before returning from the webhook.
        # This avoids "didn't respond" cases due to background-thread scheduling or reply_token expiry.
        long_task = _likely_long_task(user_text, attachment) or recent_followup
        if long_task:
            try:
                ack_msg = "⏳ 已收到，正在處理中。完成後我會用推播回覆結果。"
                if attachment and attachment.get("type") in ("file", "audio", "image"):
                    try:
                        att_path = attachment.get("path", "")
                        att_size = os.path.getsize(att_path) if att_path and os.path.exists(att_path) else 0
                        # Use original filename (from LINE event.message.file_name), fall back to basename of temp path
                        att_fname = attachment.get("filename") or os.path.basename(att_path) or "附件"
                        if att_size > 0:
                            from api.orchestrator import Orchestrator
                            ack_msg = Orchestrator.estimate_file_processing_time(
                                file_size_bytes=att_size,
                                filename=att_fname,
                                prompt=user_text or "",
                                file_path=att_path,
                            )
                    except Exception:
                        _log.debug("silent-catch at %s:%s", __name__, "handle_message/ack_est", exc_info=True)
                _line_send_text(event, user_id, ack_msg, prefer_push=False)
            except Exception as ack_err:
                logger.warning(f"⚠️ Failed to send immediate ACK: {ack_err}")

            # Ensure Orchestrator can push progress updates during long tasks.
            _register_orchestrator_notifications()

        if (attachment and attachment.get("type") in ("file", "audio")) or recent_followup:
            try:
                _enqueue_attachment_job(
                    platform_name="LINE",
                    user_id=user_id,
                    role=role,
                    user_text=user_text,
                    attachment=attachment,
                )
                if attachment:
                    att_path = str(attachment.get("path") or "").strip()
                    if att_path and os.path.exists(att_path):
                        _safe_remove_tmp(att_path)
                return 'OK'
            except Exception as enqueue_err:
                logger.error(f"❌ LINE attachment job enqueue failed: {enqueue_err}")

        # Persist to message queue before returning OK (at-least-once delivery)
        _mq_msg_id = None
        try:
            from skills.memory.message_queue import get_queue as _get_mq
            _mq = _get_mq()
            _mq_msg_id = _mq.enqueue(
                platform="LINE",
                user_id=user_id,
                user_text=user_text,
                role=role,
                attachment=json.dumps(attachment) if attachment else "{}",
            )
        except Exception as _mq_err:
            logger.warning(f"⚠️ MQ enqueue failed (non-fatal): {_mq_err}")

        # Run in bounded background pool
        _CHANNEL_BG_EXECUTOR.submit(process_message_async, event, user_id, user_text, attachment, role, long_task, True, _mq_msg_id)

        # Return immediately to avoid LINE timeout
        return 'OK'

    @handler.add(MessageEvent, message=(ImageMessage, AudioMessage, FileMessage))
    def handle_content(event):
        user_id = event.source.user_id
        message_id = event.message.id
        msg_type = event.message.type
        _record_last_line_sender(event)

        logger.info(f"📂 Received {msg_type} from {user_id}")

        # specific handling for file name if available
        file_name = getattr(event.message, 'file_name', f"{message_id}.{msg_type}")

        # Download Content
        message_content = line_bot_api.get_message_content(message_id)

        # Check message type to determine extension/path
        ext = "bin"
        if msg_type == "image": ext = "jpg"
        elif msg_type == "audio": ext = "m4a"
        elif msg_type == "file":
            # try to get extension from filename
            if "." in file_name:
                ext = file_name.split(".")[-1]

        temp_path = f"/tmp/{message_id}.{ext}"

        try:
            with open(temp_path, 'wb') as fd:
                for chunk in message_content.iter_content():
                    fd.write(chunk)
        except Exception:
            # Clean up partially-written temp file on download failure
            try:
                if os.path.exists(temp_path):
                    _safe_remove_tmp(temp_path)
            except Exception:
                _log.debug("silent-catch at %s:%s", __name__, "handle_content/download_cleanup", exc_info=True)
            raise

        logger.info(f"💾 Saved to {temp_path}")

        attachment_payload = {
            "type": msg_type,
            "path": temp_path,
            "filename": file_name,
            "timestamp": time.time(),
        }
        user_context[user_id] = attachment_payload
        if msg_type in {"image", "file"}:
            try:
                durable_recent = _persist_attachment_payload(attachment_payload, prefix=f"line_recent_{message_id}")
                if durable_recent:
                    orchestrator.remember_recent_attachment(
                        user_id=user_id,
                        platform="LINE",
                        attachment=durable_recent,
                        source_message="",
                    )
            except Exception as recent_err:
                logger.warning(f"⚠️ Failed to persist LINE recent attachment {message_id}: {recent_err}")

        # Reply asking for instruction
        reply_map = {
            "image": "📸 圖片已接收。請告訴我您想做什麼？(例如：描述這張圖、翻譯文字)",
            "file": f"DFC 檔案 ({file_name}) 已接收。請下達指令。"
        }

        if msg_type == "audio":
            # Route voice through orchestrator so we can consistently output timestamped TXT.
            _line_send_text(event, user_id, "⏳ 已收到語音，正在進行逐字稿處理（含時間戳/TXT）。完成後我會用推播回覆。", prefer_push=False)

            role = "user"
            if user_id in ADMIN_LINE_USER_IDS:
                role = "admin"
            elif LINE_AUTO_ADMIN_LAST_SENDER and (not ADMIN_LINE_USER_IDS) and user_id and user_id == _load_last_line_sender_user_id():
                role = "admin"

            voice_attachment = dict(attachment_payload)
            voice_prompt = "請轉換成逐字稿，附上時間戳記，並輸出TXT檔。"
            try:
                _enqueue_attachment_job(
                    platform_name="LINE",
                    user_id=user_id,
                    role=role,
                    user_text=voice_prompt,
                    attachment=voice_attachment,
                )
            except Exception as enqueue_err:
                logger.error(f"❌ Voice job enqueue failed: {enqueue_err}")

                def _run_voice_fallback():
                    try:
                        process_message_async(
                            event,
                            user_id,
                            voice_prompt,
                            voice_attachment,
                            role=role,
                            long_task=True,
                            already_acked=True,
                        )
                    except Exception as e:
                        logger.error(f"❌ Voice processing fallback error: {e}")
                        _line_push_text(user_id, f"❌ 語音處理錯誤: {str(e)}")
                    finally:
                        try:
                            if os.path.exists(temp_path):
                                _safe_remove_tmp(temp_path)
                        except Exception:
                            _log.debug("silent-catch at %s:%s", __name__, "handle_content/voice_cleanup", exc_info=True)
                        user_context.pop(user_id, None)

                _CHANNEL_BG_EXECUTOR.submit(_run_voice_fallback)
            else:
                try:
                    if os.path.exists(temp_path):
                        _safe_remove_tmp(temp_path)
                except Exception:
                    _log.debug("silent-catch at %s:%s", __name__, "handle_content/voice_ok_cleanup", exc_info=True)
                user_context.pop(user_id, None)
            return

        else:
            reply_text = reply_map.get(msg_type, "檔案已接收。請指示下一步。")

        _line_send_text(event, user_id, reply_text, prefer_push=False)

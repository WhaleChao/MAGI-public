"""Telegram webhook and messaging blueprint.

Extracted from server.py.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.request
import uuid
from collections import defaultdict
from pathlib import Path

from flask import Blueprint, request, jsonify

_log = logging.getLogger(__name__)

telegram_bp = Blueprint("telegram", __name__)

# ---------------------------------------------------------------------------
# Paths & directories (mirror server.py conventions)
# ---------------------------------------------------------------------------
_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_agent_dir_for_logs = os.path.join(_MAGI_ROOT, ".agent")
os.makedirs(_agent_dir_for_logs, exist_ok=True)

AGENT_DIR = _agent_dir_for_logs

logger = logging.getLogger("Server")

# ---------------------------------------------------------------------------
# Channel delivery audit (shared with LINE in server.py — here we keep our
# own copy so the blueprint is self-contained)
# ---------------------------------------------------------------------------
_CHANNEL_DELIVERY_AUDIT_FILE = os.path.join(_agent_dir_for_logs, "channel_delivery_audit.jsonl")
_channel_audit_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Telegram-specific constants & state
# ---------------------------------------------------------------------------
TELEGRAM_CONTEXT_TTL_SECONDS = int(os.environ.get("MAGI_TELEGRAM_CONTEXT_TTL_SECONDS", "300") or "300")
_TELEGRAM_SEEN_UPDATES: dict[int, int] = {}

# ---------------------------------------------------------------------------
# LINE_QUOTA_ALERT_FILE — reused by _notify_admin_telegram_once
# ---------------------------------------------------------------------------
LINE_QUOTA_ALERT_FILE = os.path.join(AGENT_DIR, "line_quota_alert.json")

# ---------------------------------------------------------------------------
# Lazy orchestrator accessor
# ---------------------------------------------------------------------------
def _get_orchestrator():
    """Lazy import to avoid circular dependency at module load time."""
    from api.server import orchestrator
    return orchestrator


# ---------------------------------------------------------------------------
# Token / ID loaders (pure env-var, no openclaw.json dependency)
# ---------------------------------------------------------------------------

def _load_openclaw_telegram_token() -> str:
    """讀取 TG bot token — 純環境變數，不再依賴 openclaw.json。"""
    return (os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN") or "").strip()


def _load_admin_telegram_ids() -> list[str]:
    """讀取 TG admin IDs — 純環境變數，不再依賴 openclaw.json。"""
    return [
        x.strip()
        for x in (os.environ.get("MAGI_ADMIN_TELEGRAM_IDS") or "").split(",")
        if x.strip()
    ]


def _load_notify_telegram_ids() -> list[str]:
    ids = [
        x.strip()
        for x in (os.environ.get("MAGI_NOTIFY_TELEGRAM_IDS") or "").split(",")
        if x.strip()
    ]
    try:
        state = _load_telegram_channel_state()
        notify_to = state.get("notifyTo") or []
        if isinstance(notify_to, list):
            ids.extend([str(x).strip() for x in notify_to if str(x).strip()])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9403, exc_info=True)
    out: list[str] = []
    seen: set[str] = set()
    for x in ids:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ---------------------------------------------------------------------------
# Webhook secret
# ---------------------------------------------------------------------------

def _load_telegram_webhook_secret() -> str:
    """讀取 TG webhook secret — 純環境變數，不再依賴 openclaw.json。"""
    return (
        os.environ.get("TELEGRAM_WEBHOOK_SECRET")
        or os.environ.get("OPENCLAW_TELEGRAM_WEBHOOK_SECRET")
        or ""
    ).strip()


def _telegram_verify_webhook_secret() -> bool:
    expected = _load_telegram_webhook_secret()
    if not expected:
        return True
    received = (request.headers.get("X-Telegram-Bot-Api-Secret-Token") or "").strip()
    return bool(received) and hmac.compare_digest(received, expected)


# ---------------------------------------------------------------------------
# Seen-update deduplication
# ---------------------------------------------------------------------------

def _telegram_mark_seen_update(update_id: int | None) -> bool:
    if update_id is None:
        return False
    now = int(time.time())
    # prune old ids
    stale_before = now - 3600
    for k, ts in list(_TELEGRAM_SEEN_UPDATES.items()):
        if ts < stale_before:
            _TELEGRAM_SEEN_UPDATES.pop(k, None)
    if len(_TELEGRAM_SEEN_UPDATES) > 5000:
        # Keep newest 2500 entries
        sorted_items = sorted(_TELEGRAM_SEEN_UPDATES.items(), key=lambda x: x[1])
        for uid, _ in sorted_items[:len(sorted_items) // 2]:
            del _TELEGRAM_SEEN_UPDATES[uid]
    if update_id in _TELEGRAM_SEEN_UPDATES:
        return True
    _TELEGRAM_SEEN_UPDATES[update_id] = now
    return False


# ---------------------------------------------------------------------------
# Telegram Bot API helper
# ---------------------------------------------------------------------------

def _telegram_api_post(token: str, method: str, payload: dict | None = None, files: dict | None = None):
    try:
        from skills.bridge.http_pool import get_session
        sess = get_session()
        url = f"https://api.telegram.org/bot{token}/{method}"
        if files:
            return sess.post(url, data=payload or {}, files=files, timeout=20)
        return sess.post(url, json=payload or {}, timeout=20)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Audit helpers
# ---------------------------------------------------------------------------

def _audit_preview(text: str, limit: int = 180) -> str:
    s = " ".join(str(text or "").strip().split())
    if len(s) <= limit:
        return s
    return s[:limit] + "..."


def _audit_sha1(text: str) -> str:
    return hashlib.sha1(str(text or "").encode("utf-8", "ignore")).hexdigest()


def _append_channel_delivery_audit(event: dict) -> None:
    try:
        payload = {"ts": time.time()}
        payload.update(event or {})
        with _channel_audit_lock:
            with open(_CHANNEL_DELIVERY_AUDIT_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            # Auto-prune: keep last 30K lines when file exceeds 5MB
            try:
                if os.path.getsize(_CHANNEL_DELIVERY_AUDIT_FILE) > 5 * 1024 * 1024:
                    with open(_CHANNEL_DELIVERY_AUDIT_FILE, "r", encoding="utf-8") as f:
                        lines = f.readlines()
                    with open(_CHANNEL_DELIVERY_AUDIT_FILE, "w", encoding="utf-8") as f:
                        f.writelines(lines[-30000:])
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7192, exc_info=True)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7194, exc_info=True)


# ---------------------------------------------------------------------------
# Text chunking (shared utility, duplicated here to keep blueprint standalone)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Send helpers
# ---------------------------------------------------------------------------

def _telegram_send_text_to(chat_id: str, text: str, reply_to_message_id: int | None = None) -> bool:
    token = _load_openclaw_telegram_token()
    if not token or not str(chat_id or "").strip():
        return False
    ok_all = True
    for chunk in _chunk_text_for_line(text, limit=3900):
        payload = {
            "chat_id": str(chat_id),
            "text": chunk,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        resp = _telegram_api_post(token, "sendMessage", payload=payload)
        ok = bool(resp and resp.status_code == 200)
        message_id = None
        try:
            data = resp.json() if resp is not None else {}
            if isinstance(data, dict):
                message_id = ((data.get("result") or {}) or {}).get("message_id")
        except Exception:
            message_id = None
        _append_channel_delivery_audit(
            {
                "platform": "telegram",
                "kind": "text",
                "chat_id": str(chat_id),
                "reply_to_message_id": int(reply_to_message_id) if reply_to_message_id else None,
                "message_id": int(message_id) if message_id else None,
                "ok": ok,
                "status_code": int(resp.status_code) if resp is not None else None,
                "text_sha1": _audit_sha1(chunk),
                "preview": _audit_preview(chunk),
            }
        )
        if not ok:
            ok_all = False
    return ok_all


def _telegram_send_document(chat_id: str, file_path: str, caption: str = "", reply_to_message_id: int | None = None) -> bool:
    token = _load_openclaw_telegram_token()
    p = (file_path or "").strip()
    if not token or not str(chat_id or "").strip() or (not p) or (not os.path.exists(p)):
        return False
    payload = {"chat_id": str(chat_id), "caption": (caption or "")[:900]}
    if reply_to_message_id:
        payload["reply_to_message_id"] = int(reply_to_message_id)
    try:
        with open(p, "rb") as f:
            files = {"document": (os.path.basename(p), f)}
            resp = _telegram_api_post(token, "sendDocument", payload=payload, files=files)
        ok = bool(resp and resp.status_code == 200)
        message_id = None
        try:
            data = resp.json() if resp is not None else {}
            if isinstance(data, dict):
                message_id = ((data.get("result") or {}) or {}).get("message_id")
        except Exception:
            message_id = None
        _append_channel_delivery_audit(
            {
                "platform": "telegram",
                "kind": "document",
                "chat_id": str(chat_id),
                "reply_to_message_id": int(reply_to_message_id) if reply_to_message_id else None,
                "message_id": int(message_id) if message_id else None,
                "ok": ok,
                "status_code": int(resp.status_code) if resp is not None else None,
                "file_path": p,
                "file_name": os.path.basename(p),
                "file_size": os.path.getsize(p),
                "caption_sha1": _audit_sha1(caption or ""),
                "preview": _audit_preview(caption or ""),
            }
        )
        return ok
    except Exception as e:
        logger.warning(f"⚠️ Telegram sendDocument failed: {e}")
        _append_channel_delivery_audit(
            {
                "platform": "telegram",
                "kind": "document",
                "chat_id": str(chat_id),
                "reply_to_message_id": int(reply_to_message_id) if reply_to_message_id else None,
                "ok": False,
                "file_path": p,
                "file_name": os.path.basename(p),
                "preview": _audit_preview(caption or ""),
                "error": str(e)[:500],
            }
        )
        return False


def _telegram_send_photo(chat_id: str, image_path: str, caption: str = "", reply_to_message_id: int | None = None) -> bool:
    token = _load_openclaw_telegram_token()
    p = (image_path or "").strip()
    if not token or not str(chat_id or "").strip() or (not p) or (not os.path.exists(p)):
        return False
    payload = {"chat_id": str(chat_id), "caption": (caption or "")[:900]}
    if reply_to_message_id:
        payload["reply_to_message_id"] = int(reply_to_message_id)
    try:
        with open(p, "rb") as f:
            files = {"photo": (os.path.basename(p), f)}
            resp = _telegram_api_post(token, "sendPhoto", payload=payload, files=files)
        ok = bool(resp and resp.status_code == 200)
        message_id = None
        try:
            data = resp.json() if resp is not None else {}
            if isinstance(data, dict):
                message_id = ((data.get("result") or {}) or {}).get("message_id")
        except Exception:
            message_id = None
        _append_channel_delivery_audit(
            {
                "platform": "telegram",
                "kind": "photo",
                "chat_id": str(chat_id),
                "reply_to_message_id": int(reply_to_message_id) if reply_to_message_id else None,
                "message_id": int(message_id) if message_id else None,
                "ok": ok,
                "status_code": int(resp.status_code) if resp is not None else None,
                "file_path": p,
                "file_name": os.path.basename(p),
                "file_size": os.path.getsize(p),
                "caption_sha1": _audit_sha1(caption or ""),
                "preview": _audit_preview(caption or ""),
            }
        )
        return ok
    except Exception as e:
        logger.warning(f"⚠️ Telegram sendPhoto failed: {e}")
        _append_channel_delivery_audit(
            {
                "platform": "telegram",
                "kind": "photo",
                "chat_id": str(chat_id),
                "reply_to_message_id": int(reply_to_message_id) if reply_to_message_id else None,
                "ok": False,
                "file_path": p,
                "file_name": os.path.basename(p),
                "preview": _audit_preview(caption or ""),
                "error": str(e)[:500],
            }
        )
        return False


# ---------------------------------------------------------------------------
# File download
# ---------------------------------------------------------------------------

def _telegram_download_file(file_id: str, suggested_name: str = "") -> str:
    token = _load_openclaw_telegram_token()
    fid = (file_id or "").strip()
    if not token or not fid:
        return ""
    out_path = ""
    try:
        from skills.bridge.http_pool import get_session
        sess = get_session()
        r = sess.get(f"https://api.telegram.org/bot{token}/getFile", params={"file_id": fid}, timeout=20)
        if r.status_code != 200:
            return ""
        obj = r.json() if r.content else {}
        if not isinstance(obj, dict) or not obj.get("ok"):
            return ""
        file_path = str((obj.get("result") or {}).get("file_path") or "").strip()
        if not file_path:
            return ""
        ext = os.path.splitext(suggested_name or file_path)[1] or ""
        out_path = f"/tmp/tg_{fid}{ext}"
        file_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        rr = sess.get(file_url, timeout=60)
        if rr.status_code != 200:
            return ""
        with open(out_path, "wb") as f:
            f.write(rr.content)
        return out_path
    except Exception as e:
        logger.warning(f"⚠️ Telegram file download failed: {e}")
        # Clean up partial temp file on failure
        if out_path:
            try:
                os.unlink(out_path)
            except OSError:
                pass
        return ""


# ---------------------------------------------------------------------------
# Output normalization
# ---------------------------------------------------------------------------

def _normalize_telegram_output_text(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return s
    try:
        from api.tw_output_guard import normalize_output_text as _normalize_output_text
    except Exception:
        _normalize_output_text = None
    try:
        if _normalize_output_text:
            return _normalize_output_text(s, platform="TELEGRAM")
    except Exception as e:
        logger.warning(f"⚠️ Taiwan wording guard skipped (Telegram): {e}")
    return s


# ---------------------------------------------------------------------------
# Orchestrator response dispatcher
# ---------------------------------------------------------------------------

def _telegram_send_orchestrator_response(chat_id: str, response_text: str, reply_to_message_id: int | None = None) -> None:
    text = _normalize_telegram_output_text(response_text)
    if not text:
        _telegram_send_text_to(chat_id, "⚠️ 任務完成，但沒有可用輸出。", reply_to_message_id=reply_to_message_id)
        return

    if "|||FILE_PATH|||" in text:
        try:
            text_part, file_path = text.split("|||FILE_PATH|||", 1)
            text_part = (text_part or "").strip()
            file_path = (file_path or "").strip()
            if _telegram_send_document(chat_id, file_path, caption=text_part, reply_to_message_id=reply_to_message_id):
                return
            _telegram_send_text_to(chat_id, f"{text_part}\n⚠️ 檔案傳送失敗：{file_path}", reply_to_message_id=reply_to_message_id)
            return
        except Exception as e:
            _telegram_send_text_to(chat_id, f"⚠️ 檔案回傳解析失敗：{e}", reply_to_message_id=reply_to_message_id)
            return

    if "|||IMAGE_PATH|||" in text:
        try:
            text_part, image_path = text.split("|||IMAGE_PATH|||", 1)
            text_part = (text_part or "").strip()
            image_path = (image_path or "").strip()
            if _telegram_send_photo(chat_id, image_path, caption=text_part, reply_to_message_id=reply_to_message_id):
                return
            _telegram_send_text_to(chat_id, f"{text_part}\n⚠️ 圖片傳送失敗：{image_path}", reply_to_message_id=reply_to_message_id)
            return
        except Exception as e:
            _telegram_send_text_to(chat_id, f"⚠️ 圖片回傳解析失敗：{e}", reply_to_message_id=reply_to_message_id)
            return

    _telegram_send_text_to(chat_id, text, reply_to_message_id=reply_to_message_id)


# ---------------------------------------------------------------------------
# Safe tmp removal (standalone version)
# ---------------------------------------------------------------------------

def _safe_remove_tmp(path: str) -> None:
    """
    Safety: never delete Synology Drive artifacts.
    For temporary files (typically /tmp), allow cleanup via safe_fs.
    """
    p = (path or "").strip()
    if not p:
        return
    try:
        from api.server import get_orch_dir, ensure_path_on_sys_path
        ensure_path_on_sys_path(get_orch_dir())
        import safe_fs  # type: ignore
        safe_fs.safe_remove(p, reason="tmp_cleanup", allow_delete=True)
        return
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8385, exc_info=True)
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8390, exc_info=True)


# ---------------------------------------------------------------------------
# Async processing
# ---------------------------------------------------------------------------

def _telegram_process_async(
    chat_id: str,
    user_id: str,
    role: str,
    user_text: str,
    attachment: dict | None = None,
    reply_to_message_id: int | None = None,
    channel_context: dict | None = None,
    _mq_msg_id: str | None = None,
) -> None:
    orchestrator = _get_orchestrator()
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
            _log.debug("silent-catch at %s:%s", __name__, "_telegram_process_async/mq_claim", exc_info=True)
    tmp_path = ""
    try:
        if attachment:
            tmp_path = str(attachment.get("path") or "").strip()
            response_text = orchestrator.process_message(
                user_id=user_id,
                message=user_text,
                platform="Telegram",
                role=role,
                attachment=attachment,
                correlation_id=correlation_id,
                channel_context=channel_context,
            )
        else:
            response_text = orchestrator.process_message(
                user_id=user_id,
                message=user_text,
                platform="Telegram",
                role=role,
                correlation_id=correlation_id,
                channel_context=channel_context,
            )
        _telegram_send_orchestrator_response(chat_id, str(response_text or ""), reply_to_message_id=reply_to_message_id)
        # Record assistant reply for conversation history (matches Discord/LINE pattern)
        if response_text:
            try:
                orchestrator.record_assistant_reply(user_id, str(response_text), channel_id=str(chat_id), platform="telegram")
            except Exception:
                _log.debug("silent-catch at %s:%s", __name__, "_telegram_process_async/record_reply", exc_info=True)
        # Message-queue: mark success
        if _mq_inst and _mq_msg_id:
            try:
                _mq_inst.complete(_mq_msg_id)
            except Exception:
                _log.debug("silent-catch at %s:%s", __name__, "_telegram_process_async/mq_complete", exc_info=True)
    except Exception as e:
        logger.error(f"❌ Telegram processing error: {e}")
        # Message-queue: mark failure (may retry)
        if _mq_inst and _mq_msg_id:
            try:
                _mq_inst.fail(_mq_msg_id, str(e))
            except Exception:
                _log.debug("silent-catch at %s:%s", __name__, "_telegram_process_async/mq_fail", exc_info=True)
        _telegram_send_text_to(chat_id, "⚠️ 系統暫時忙碌中，請稍後再試一次。")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            _safe_remove_tmp(tmp_path)
        # OBS-2: record processing latency
        elapsed_ms = int((time.monotonic() - _start_ts) * 1000)
        _append_channel_delivery_audit({
            "platform": "Telegram",
            "kind": "latency",
            "user_id": str(user_id or ""),
            "chat_id": str(chat_id or ""),
            "correlation_id": correlation_id,
            "latency_ms": elapsed_ms,
        })


# ---------------------------------------------------------------------------
# Telegram channel state persistence
# ---------------------------------------------------------------------------

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
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8934, exc_info=True)
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


# ---------------------------------------------------------------------------
# Handle update (core dispatcher)
# ---------------------------------------------------------------------------

def _telegram_handle_update(update: dict, from_poll: bool = False) -> dict:
    # Lazy imports for server globals
    from api.server import (
        _check_rate_limit,
        _record_last_public_base_url,
        _likely_long_task,
        _enqueue_attachment_job,
    )
    from api.thread_pools import channel_pool as _CHANNEL_BG_EXECUTOR

    orchestrator = _get_orchestrator()

    # Inject request context for structured logging
    from skills.ops.structured_log import set_request_context
    _tg_user = ((update or {}).get("message") or {}).get("from") or {}
    set_request_context(
        request_id=uuid.uuid4().hex[:12],
        user_id=str(_tg_user.get("id", "")),
        platform="Telegram",
    )
    if not isinstance(update, dict):
        update = {}
    update_id = update.get("update_id")
    try:
        update_id = int(update_id) if update_id is not None else None
    except Exception:
        update_id = None
    if (not from_poll) and _telegram_mark_seen_update(update_id):
        return {"ok": True, "deduped": True}

    msg = (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or update.get("edited_channel_post")
        or {}
    )
    if not isinstance(msg, dict) or not msg:
        return {"ok": True, "ignored": "no_message"}

    chat = msg.get("chat") or {}
    sender_chat = msg.get("sender_chat") or {}
    user = msg.get("from") or {}
    chat_id = str(chat.get("id") or "").strip()
    chat_type = str(chat.get("type") or "").strip().lower()
    chat_title = str(chat.get("title") or "").strip()
    user_id_raw = str(user.get("id") or "").strip()
    sender_chat_id = str(sender_chat.get("id") or "").strip()
    sender_id = user_id_raw or sender_chat_id or chat_id
    if not chat_id:
        return {"ok": True, "ignored": "missing_ids"}

    allowed_admin_ids = set(_load_admin_telegram_ids() or [])
    allowed_admin_ids |= set(_load_notify_telegram_ids() or [])
    candidate_ids = {x for x in [sender_id, chat_id, user_id_raw, sender_chat_id] if str(x or "").strip()}

    def _is_allowed() -> bool:
        return (not allowed_admin_ids) or any(cid in allowed_admin_ids for cid in candidate_ids)

    if not _is_allowed():
        # Bootstrap path: if this is a MAGI-named group/channel, bind once then re-check.
        try:
            auto_pair = str(os.environ.get("MAGI_TG_AUTO_PAIR_MAGI_GROUP", "1")).strip().lower() in {"1", "true", "yes", "on"}
            title_hit = "magi" in chat_title.lower()
            if auto_pair and chat_type in {"group", "supergroup", "channel"} and title_hit:
                _telegram_apply_group_notify_binding(chat_id=chat_id, sender_id=(user_id_raw or sender_chat_id or chat_id))
                allowed_admin_ids = set(_load_admin_telegram_ids() or [])
                allowed_admin_ids |= set(_load_notify_telegram_ids() or [])
        except Exception as pair_err:
            logger.warning(f"⚠️ Telegram auto-pair failed: {pair_err}")

    if not _is_allowed():
        logger.warning(
            "⛔ Telegram blocked by allowlist chat_id=%s chat_type=%s sender_id=%s user_id=%s sender_chat_id=%s title=%s candidates=%s",
            chat_id,
            chat_type,
            sender_id,
            user_id_raw,
            sender_chat_id,
            chat_title[:80],
            list(candidate_ids),
        )
        _telegram_send_text_to(chat_id, "⛔ 此 Telegram 帳號不在允許清單中。")
        return {"ok": True, "blocked": "allowlist"}

    role = "admin" if any(cid in allowed_admin_ids for cid in candidate_ids) else "user"
    user_id = f"telegram_{sender_id}"
    user_text = str(msg.get("text") or msg.get("caption") or "").strip()
    try:
        message_thread_id = int(msg.get("message_thread_id")) if msg.get("message_thread_id") is not None else None
    except Exception:
        message_thread_id = None
    attachment = None

    # Auto-bind: if admin talks in a group/supergroup, make this chat a notify target.
    try:
        auto_bind = str(os.environ.get("MAGI_TG_AUTO_BIND_GROUP_NOTIFY", "1")).strip().lower() in {"1", "true", "yes", "on"}
        if auto_bind and role == "admin" and chat_type in {"group", "supergroup"}:
            ok, msg_bind = _telegram_apply_group_notify_binding(chat_id=chat_id, sender_id=sender_id)
            if ok:
                logger.info(f"✅ Telegram auto-bind notify target chat_id={chat_id}")
            else:
                logger.warning(f"⚠️ Telegram auto-bind failed chat_id={chat_id}: {msg_bind}")
    except Exception as bind_err:
        logger.warning(f"⚠️ Telegram auto-bind exception: {bind_err}")

    settings_reply = _handle_telegram_settings_command(
        user_text,
        chat_id=chat_id,
        sender_id=sender_id,
        message_thread_id=message_thread_id,
        role=role,
    )
    if settings_reply is not None:
        _telegram_send_text_to(chat_id, settings_reply, reply_to_message_id=msg.get("message_id"))
        return {"ok": True, "settings_cmd": True}

    # ── Skip MAGI notification messages ──────────────────────────────
    # When LAFNotifier (or other MAGI subsystems) sends notifications via
    # TG bot API, the webhook may receive them in group chats.  Also skip
    # when a user *replies* to a notification — the reply should not be
    # treated as a conversational prompt for the AI.
    _NOTIFICATION_PREFIXES = ("📋", "💰", "📥", "⚠️ 閱卷", "✅ 閱卷", "🔔")

    def _is_notification_text(t: str) -> bool:
        if not t:
            return False
        return any(t.startswith(p) for p in _NOTIFICATION_PREFIXES)

    # Case 1: message itself is from a bot (self-message in group)
    if user.get("is_bot") and _is_notification_text(user_text):
        logger.info("🔕 Telegram: skipping bot-originated notification message")
        return {"ok": True, "ignored": "bot_notification"}

    # Case 2: user replied to a notification message
    reply_to = msg.get("reply_to_message") or {}
    reply_from = reply_to.get("from") or {}
    reply_text = str(reply_to.get("text") or reply_to.get("caption") or "").strip()
    if reply_from.get("is_bot") and _is_notification_text(reply_text):
        logger.info("🔕 Telegram: skipping reply-to-notification (replied to: %s)", reply_text[:60])
        return {"ok": True, "ignored": "reply_to_notification"}
    # ─────────────────────────────────────────────────────────────────

    try:
        if isinstance(msg.get("voice"), dict):
            voice = msg.get("voice") or {}
            file_id = str(voice.get("file_id") or "").strip()
            path = _telegram_download_file(file_id, suggested_name="voice.ogg")
            if path:
                attachment = {"type": "audio", "path": path, "filename": "voice.ogg", "timestamp": time.time()}
                if not user_text:
                    user_text = "請轉換成逐字稿，附上時間戳記，並輸出TXT檔。"
        elif isinstance(msg.get("audio"), dict):
            audio = msg.get("audio") or {}
            file_id = str(audio.get("file_id") or "").strip()
            fname = str(audio.get("file_name") or "audio.m4a").strip()
            path = _telegram_download_file(file_id, suggested_name=fname)
            if path:
                attachment = {"type": "audio", "path": path, "filename": fname, "timestamp": time.time()}
                if not user_text:
                    user_text = "請轉換成逐字稿，附上時間戳記，並輸出TXT檔。"
        elif isinstance(msg.get("photo"), list) and msg.get("photo"):
            photos = msg.get("photo") or []
            best = photos[-1] if isinstance(photos[-1], dict) else {}
            file_id = str(best.get("file_id") or "").strip()
            path = _telegram_download_file(file_id, suggested_name="photo.jpg")
            if path:
                attachment = {"type": "image", "path": path, "filename": "photo.jpg", "timestamp": time.time()}
                if not user_text:
                    user_text = "請分析這張圖片並用繁體中文回覆重點。"
        elif isinstance(msg.get("document"), dict):
            doc = msg.get("document") or {}
            file_id = str(doc.get("file_id") or "").strip()
            fname = str(doc.get("file_name") or "document.bin").strip()
            mime = str(doc.get("mime_type") or "").lower()
            path = _telegram_download_file(file_id, suggested_name=fname)
            if path:
                msg_type = "audio" if mime.startswith("audio/") else "file"
                attachment = {"type": msg_type, "path": path, "filename": fname, "timestamp": time.time()}
                if not user_text:
                    user_text = "請轉換成逐字稿，附上時間戳記，並輸出TXT檔。" if msg_type == "audio" else "請分析這個檔案並回覆重點。"
    except Exception as att_err:
        logger.warning(f"⚠️ Telegram attachment parse failed: {att_err}")

    if not user_text and not attachment:
        # Ignore service/empty updates to avoid triggering generic fallback tasks.
        return {"ok": True, "ignored": "empty_message"}
    if not user_text:
        user_text = "請協助處理這則訊息。"

    recent_followup = False
    try:
        recent_followup = orchestrator.has_recent_attachment_followup(user_id, "Telegram", user_text)
    except Exception as recent_err:
        logger.warning(f"⚠️ Telegram recent attachment probe failed: {recent_err}")

    long_task = _likely_long_task(user_text, attachment) or recent_followup
    if long_task:
        ack_msg = "⏳ 已收到，正在處理中。完成後我會回覆結果。"
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
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7685, exc_info=True)
        _telegram_send_text_to(chat_id, ack_msg, reply_to_message_id=msg.get("message_id"))

    if (attachment and attachment.get("type") in ("file", "audio")) or recent_followup:
        try:
            job = _enqueue_attachment_job(
                platform_name="Telegram",
                user_id=user_id,
                role=role,
                user_text=user_text,
                attachment=attachment,
                chat_id=chat_id,
                reply_to_message_id=msg.get("message_id"),
            )
            if attachment:
                att_path = str(attachment.get("path") or "").strip()
                if att_path and os.path.exists(att_path):
                    _safe_remove_tmp(att_path)
            return {"ok": True, "job_id": job.get("job_id")}
        except Exception as enqueue_err:
            logger.error(f"❌ Telegram attachment job enqueue failed: {enqueue_err}")

    # 2026-03-29: Build channel_context from Telegram message_thread_id
    _tg_channel_ctx = None
    try:
        _tg_topic_key = ""
        if message_thread_id is not None:
            _tg_state = _load_telegram_channel_state()
            _tg_topic_map = _tg_state.get("topicMap") or {}
            # Reverse lookup: thread_id -> topic_key
            for _tk, _tid in _tg_topic_map.items():
                if int(_tid) == int(message_thread_id):
                    _tg_topic_key = str(_tk)
                    break
        _tg_channel_ctx = {
            "topic_key": _tg_topic_key,
            "thread_id": message_thread_id,
            "channel_id": str(chat_id),
            "platform": "Telegram",
        }
    except Exception as _ctx_err:
        logger.debug(f"Telegram channel_context build skipped: {_ctx_err}")

    # Persist to message queue before returning OK (at-least-once delivery)
    _mq_msg_id = None
    try:
        from skills.memory.message_queue import get_queue as _get_mq
        _mq = _get_mq()
        _mq_msg_id = _mq.enqueue(
            platform="Telegram",
            user_id=user_id,
            user_text=user_text,
            role=role,
            chat_id=str(chat_id or ""),
            attachment=json.dumps(attachment) if attachment else "{}",
        )
    except Exception as _mq_err:
        logger.warning(f"⚠️ MQ enqueue failed (non-fatal): {_mq_err}")

    _CHANNEL_BG_EXECUTOR.submit(
        _telegram_process_async,
        chat_id,
        user_id,
        role,
        user_text,
        attachment,
        msg.get("message_id"),
        _tg_channel_ctx,
        _mq_msg_id,
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# Webhook route
# ---------------------------------------------------------------------------

@telegram_bp.route("/telegram/webhook", methods=["GET", "POST"])
def telegram_webhook():
    if request.method == "GET":
        return "OK", 200

    from api.server import _check_rate_limit, _record_last_public_base_url

    if _check_rate_limit("webhook"):
        return jsonify({"ok": False, "error": "rate limited"}), 429

    if not _telegram_verify_webhook_secret():
        return jsonify({"ok": False, "error": "invalid webhook secret"}), 401

    try:
        _record_last_public_base_url()
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7755, exc_info=True)

    update = request.get_json(silent=True) or {}
    try:
        return jsonify(_telegram_handle_update(update, from_poll=False))
    except Exception as _tg_err:
        logger.error("Telegram webhook handler exception: %s", _tg_err, exc_info=True)
        return jsonify({"ok": False, "error": "internal_error"}), 500


# ---------------------------------------------------------------------------
# Polling system (fallback when webhook URL is not publicly reachable)
# ---------------------------------------------------------------------------

TELEGRAM_POLL_OFFSET_FILE = os.path.join(_agent_dir_for_logs, "telegram_poll_offset.json")
TELEGRAM_POLLING_ENABLED = os.environ.get("MAGI_TELEGRAM_POLLING_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}
TELEGRAM_POLLING_FORCE = os.environ.get("MAGI_TELEGRAM_POLLING_FORCE", "0").strip().lower() in {"1", "true", "yes", "on"}
_TELEGRAM_POLL_STARTED = False


def _load_telegram_poll_offset() -> int:
    try:
        if os.path.exists(TELEGRAM_POLL_OFFSET_FILE):
            obj = json.loads(Path(TELEGRAM_POLL_OFFSET_FILE).read_text(encoding="utf-8")) or {}
            return int(obj.get("offset") or -1)
    except Exception:
        return -1
    return -1


def _save_telegram_poll_offset(offset: int) -> None:
    try:
        Path(TELEGRAM_POLL_OFFSET_FILE).write_text(
            json.dumps({"offset": int(offset), "updated_at": int(time.time())}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7788, exc_info=True)


def _telegram_poll_loop() -> None:
    """
    Fallback mode for Telegram command intake when webhook URL is not publicly reachable.
    If webhook mode is active, Telegram usually returns 409 for getUpdates; we back off quietly.
    """
    from skills.bridge.http_pool import get_session
    offset = _load_telegram_poll_offset()
    backoff = 5
    while True:
        token = _load_openclaw_telegram_token()
        if not token:
            time.sleep(30)
            continue
        try:
            sess = get_session()
            params = {"timeout": 25}
            if offset >= 0:
                params["offset"] = offset + 1
            resp = sess.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=35)
            if resp.status_code == 409 and (not TELEGRAM_POLLING_FORCE):
                time.sleep(30)
                continue
            if resp.status_code != 200:
                time.sleep(min(backoff, 60))
                backoff = min(backoff * 2, 60)
                continue

            obj = resp.json() if resp.content else {}
            updates = obj.get("result") if isinstance(obj, dict) else []
            if not isinstance(updates, list) or not updates:
                backoff = 5
                continue

            for up in updates:
                try:
                    uid = int((up or {}).get("update_id"))
                    if uid > offset:
                        offset = uid
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7830, exc_info=True)
                try:
                    _telegram_handle_update(up, from_poll=True)
                except Exception as e:
                    logger.warning(f"⚠️ Telegram poll update process failed: {e}")
            _save_telegram_poll_offset(offset)
            backoff = 5
        except Exception as e:
            logger.warning(f"⚠️ Telegram polling error: {e}")
            time.sleep(min(backoff, 60))
            backoff = min(backoff * 2, 60)


def _start_telegram_polling_fallback() -> None:
    global _TELEGRAM_POLL_STARTED
    if (not TELEGRAM_POLLING_ENABLED) or _TELEGRAM_POLL_STARTED:
        return
    _TELEGRAM_POLL_STARTED = True
    t = threading.Thread(target=_telegram_poll_loop, name="telegram-poll", daemon=True)
    t.start()
    logger.info("📮 Telegram polling fallback started.")


# ---------------------------------------------------------------------------
# Topic management
# ---------------------------------------------------------------------------

def _telegram_topic_key(raw: str) -> str:
    key = str(raw or "").strip().lower()
    aliases = {
        "general": "general",
        "default": "general",
        "預設": "general",
        "主串": "general",
        "一般": "general",
        "filereview": "filereview",
        "file_review": "filereview",
        "file-review": "filereview",
        "閱卷": "filereview",
        "卷宗": "filereview",
        "docket": "filereview",
        "transcript": "transcript",
        "transcripts": "transcript",
        "筆錄": "transcript",
        "laf": "laf",
        "legal_aid": "laf",
        "legal-aid": "laf",
        "法扶": "laf",
        "judgment": "judgment",
        "judgments": "judgment",
        "判決": "judgment",
        "司法院": "judgment",
        "verbatim": "verbatim",
        "逐字稿": "verbatim",
        "音訊": "verbatim",
        "translation": "translation",
        "translate": "translation",
        "翻譯": "translation",
        "summary": "summary",
        "summarize": "summary",
        "摘要": "summary",
        "market": "market",
        "stocks": "market",
        "stock": "market",
        "股票": "market",
        "股市": "market",
        "check": "check",
        "checks": "check",
        "health": "check",
        "檢查": "check",
        "巡檢": "check",
        "nightly": "nightly",
        "夜間": "nightly",
        "改善": "nightly",
        "夜間會議": "nightly",
        "alert": "alert",
        "alerts": "alert",
        "warning": "alert",
        "警報": "alert",
        "警告": "alert",
        "告警": "alert",
        "iron_dome": "alert",
        "irondome": "alert",
        "鐵穹": "alert",
    }
    return aliases.get(key, "")


def _telegram_apply_group_notify_binding(chat_id: str, sender_id: str) -> tuple[bool, str]:
    state = _load_telegram_channel_state()
    changed_state = False

    allow_from = state.get("allowFrom") if isinstance(state.get("allowFrom"), list) else []
    for cid in [str(sender_id or "").strip(), str(chat_id or "").strip()]:
        if cid and cid not in allow_from:
            allow_from.append(cid)
            changed_state = True
    state["allowFrom"] = allow_from

    notify_to = state.get("notifyTo") if isinstance(state.get("notifyTo"), list) else []
    c = str(chat_id or "").strip()
    if c and c not in notify_to:
        notify_to.append(c)
        changed_state = True
    state["notifyTo"] = notify_to

    if not changed_state:
        return True, f"✅ 本群已在通知目標中\nchat_id: {chat_id}\nnotifyTo: {notify_to}"

    if not _save_telegram_channel_state(state):
        return False, "❌ 寫入設定失敗（telegram_channel_state.json）"
    return True, f"✅ 已綁定本群為通知目標\nchat_id: {chat_id}\nnotifyTo: {notify_to}"


def _telegram_bind_topic(chat_id: str, sender_id: str, topic_raw: str, thread_id: int) -> tuple[bool, str]:
    topic_key = _telegram_topic_key(topic_raw)
    if not topic_key:
        return False, "❌ 無法辨識主題，請用：general / filereview / transcript / laf / judgment / verbatim / translation / summary / market / check / nightly / alert"

    state = _load_telegram_channel_state()
    changed_state = False

    allow_from = state.get("allowFrom") if isinstance(state.get("allowFrom"), list) else []
    for cid in [str(sender_id or "").strip(), str(chat_id or "").strip()]:
        if cid and cid not in allow_from:
            allow_from.append(cid)
            changed_state = True
    state["allowFrom"] = allow_from

    notify_to = state.get("notifyTo") if isinstance(state.get("notifyTo"), list) else []
    c = str(chat_id or "").strip()
    if c and c not in notify_to:
        notify_to.append(c)
        changed_state = True
    state["notifyTo"] = notify_to

    topic_map = state.get("topicMap") if isinstance(state.get("topicMap"), dict) else {}
    old_tid = int(topic_map.get(str(topic_key)) or 0)
    new_tid = int(thread_id)
    if old_tid != new_tid:
        topic_map[str(topic_key)] = new_tid
        changed_state = True
    state["topicMap"] = topic_map

    if not changed_state:
        return True, f"✅ 主題 `{topic_key}` 已是 thread_id {int(thread_id)}"

    if not _save_telegram_channel_state(state):
        return False, "❌ 寫入主題設定失敗（telegram_channel_state.json）"
    return True, f"✅ 已綁定主題 `{topic_key}` -> thread_id {int(thread_id)}"


def _telegram_setup_topics(chat_id: str, sender_id: str) -> tuple[bool, str]:
    token = _load_openclaw_telegram_token()
    if not token:
        return False, "❌ 找不到 Telegram bot token。"
    c = str(chat_id or "").strip()
    if not c:
        return False, "❌ 缺少 chat_id。"

    ok_bind, bind_msg = _telegram_apply_group_notify_binding(chat_id=c, sender_id=str(sender_id or "").strip())
    if not ok_bind:
        return False, bind_msg

    # Preflight: forum topics are only available in supergroup with topics enabled.
    try:
        chat_resp = _telegram_api_post(token, "getChat", payload={"chat_id": c})
        if chat_resp and int(getattr(chat_resp, "status_code", 0) or 0) == 200:
            data = chat_resp.json() if hasattr(chat_resp, "json") else {}
            chat_info = (data or {}).get("result") or {}
            chat_type = str(chat_info.get("type") or "").strip().lower()
            is_forum = bool(chat_info.get("is_forum"))
            if chat_type == "channel":
                return False, (
                    "⚠️ 這個聊天是「頻道 (channel)」，不是可開 Topic 的群組。\n"
                    "Telegram Topics 只能用在「超級群組 (supergroup)」。\n\n"
                    "請建立/使用超級群組後再執行：`建立MAGI主題`。\n"
                    "若你想先用目前頻道，通知仍可發送，但會全部在同一串。"
                )
            if chat_type == "supergroup" and not is_forum:
                return False, (
                    "⚠️ 目前是超級群組，但尚未啟用 Topics。\n"
                    "請到 Telegram 群組設定開啟「Topics/主題」，再執行：`建立MAGI主題`。"
                )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9151, exc_info=True)

    plan = [
        ("general", "一般"),
        ("filereview", "閱卷"),
        ("transcript", "筆錄"),
        ("laf", "法扶"),
        ("judgment", "判決"),
        ("judicial_api", "司法院API"),
        ("verbatim", "逐字稿"),
        ("translation", "翻譯"),
        ("summary", "摘要"),
        ("market", "股票"),
        ("check", "檢查"),
        ("nightly", "夜間"),
        ("alert", "警告"),
    ]
    state = _load_telegram_channel_state()
    existing_topic_map = state.get("topicMap") if isinstance(state.get("topicMap"), dict) else {}

    def _try_edit_topic_name(thread_id: int, topic_name: str) -> tuple[bool, str]:
        try:
            resp = _telegram_api_post(
                token,
                "editForumTopic",
                payload={
                    "chat_id": c,
                    "message_thread_id": int(thread_id),
                    "name": topic_name,
                },
            )
            if resp is None:
                return False, "no_response"
            try:
                data = resp.json()
            except Exception:
                data = {}
            if int(getattr(resp, "status_code", 0) or 0) == 200 and data.get("ok"):
                return True, ""
            desc = str((data or {}).get("description") or f"http_{getattr(resp, 'status_code', 'ERR')}")
            if "topic_not_modified" in desc.lower():
                return True, ""
            return False, desc
        except Exception as e:
            return False, type(e).__name__

    created: list[tuple[str, int]] = []
    reused: list[tuple[str, int]] = []
    skipped: list[str] = []
    failed: list[str] = []

    for topic_key, topic_name in plan:
        try:
            known_tid = int(existing_topic_map.get(topic_key) or 0)
        except Exception:
            known_tid = 0
        if known_tid > 0:
            ok_edit, why = _try_edit_topic_name(known_tid, topic_name)
            if ok_edit:
                ok_topic, _ = _telegram_bind_topic(c, sender_id, topic_key, known_tid)
                if ok_topic:
                    reused.append((topic_name, known_tid))
                    continue
                failed.append(f"{topic_name}: bind_failed")
                continue
            low_why = str(why or "").lower()
            # Stale/invalid thread mapping, fallback to creating a fresh topic.
            if ("message thread not found" not in low_why) and ("thread not found" not in low_why):
                failed.append(f"{topic_name}: reuse_failed({why or 'unknown'})")
                continue

        try:
            resp = _telegram_api_post(
                token,
                "createForumTopic",
                payload={"chat_id": c, "name": topic_name},
            )
            if resp is None:
                failed.append(f"{topic_name}: no_response")
                continue
            try:
                data = resp.json()
            except Exception:
                data = {}
            if int(getattr(resp, "status_code", 0) or 0) == 200 and data.get("ok"):
                result = data.get("result") or {}
                mtid = int(result.get("message_thread_id") or 0)
                if mtid > 0:
                    ok_topic, _ = _telegram_bind_topic(c, sender_id, topic_key, mtid)
                    if ok_topic:
                        created.append((topic_name, mtid))
                    else:
                        failed.append(f"{topic_name}: bind_failed")
                else:
                    failed.append(f"{topic_name}: no_thread_id")
                continue

            desc = str((data or {}).get("description") or "")
            low = desc.lower()
            if ("chat not found" in low) or ("forbidden" in low):
                return False, f"❌ 建立主題失敗：{desc or 'chat access denied'}"
            if ("not a forum" in low) or ("chat_not_forum" in low):
                return False, (
                    "⚠️ Telegram 回覆此聊天不是 forum。\n"
                    "可能是「頻道」或「尚未啟用 Topics 的超級群組」。\n"
                    "請改用超級群組並啟用 Topics 後重試。"
                )
            if ("topic already exists" in low) or ("already exists" in low):
                skipped.append(topic_name)
                continue
            if ("topic with this name already exists" in low) or ("topic name is already occupied" in low):
                skipped.append(topic_name)
                continue
            failed.append(f"{topic_name}: {desc or ('http_' + str(getattr(resp, 'status_code', 'ERR')))}")
        except Exception as e:
            failed.append(f"{topic_name}: {type(e).__name__}")

    lines = ["✅ 已嘗試建立 MAGI 主題並綁定通知分流。"]
    if reused:
        lines.append("已沿用：")
        for name, tid in reused:
            lines.append(f"- {name}（thread_id={tid}）")
    if created:
        lines.append("已建立：")
        for name, tid in created:
            lines.append(f"- {name}（thread_id={tid}）")
    if skipped:
        lines.append("已存在（略過）：")
        for name in skipped:
            lines.append(f"- {name}")
    if failed:
        lines.append("失敗：")
        for item in failed:
            lines.append(f"- {item}")

    # No longer need a separate "default" topic — "general" serves that role.

    lines.append("")
    lines.append(_telegram_notify_settings_text())
    return True, "\n".join(lines)


# ---------------------------------------------------------------------------
# Configuration / notification
# ---------------------------------------------------------------------------

def _telegram_notify_settings_text() -> str:
    state = _load_telegram_channel_state()
    allow_from = state.get("allowFrom") if isinstance(state.get("allowFrom"), list) else []
    notify_to = state.get("notifyTo") if isinstance(state.get("notifyTo"), list) else []
    topic_map = state.get("topicMap") if isinstance(state.get("topicMap"), dict) else {}
    lines = [
        "📮 Telegram 通知設定",
        f"allowFrom: {allow_from}",
        f"notifyTo: {notify_to}",
        "topicMap:",
    ]
    if topic_map:
        for k in sorted(topic_map.keys()):
            lines.append(f"- {k}: {topic_map.get(k)}")
    else:
        lines.append("- (empty)")
    return "\n".join(lines)


def _handle_telegram_settings_command(
    user_text: str,
    *,
    chat_id: str,
    sender_id: str,
    message_thread_id: int | None,
    role: str,
) -> str | None:
    text = str(user_text or "").strip()
    low = text.lower()
    if not text:
        return None
    if role != "admin":
        return None

    bind_group_cmds = {"綁定本群通知", "通知綁定本群", "設定通知到本群", "/bind_group_notify"}
    if text in bind_group_cmds or low in bind_group_cmds:
        ok, msg = _telegram_apply_group_notify_binding(chat_id=chat_id, sender_id=sender_id)
        return msg if ok else msg

    if text in {"通知設定", "顯示通知設定", "/notify_status"} or low in {"/notify_status"}:
        return _telegram_notify_settings_text()

    topic_val = ""
    if text.startswith("綁定主題 "):
        topic_val = text.replace("綁定主題 ", "", 1).strip()
    elif low.startswith("/bind_topic "):
        topic_val = text.split(" ", 1)[1].strip() if " " in text else ""
    if topic_val:
        if not message_thread_id:
            return "⚠️ 請在要綁定的 Topic 裡執行此指令（需要 message_thread_id）。"
        ok, msg = _telegram_bind_topic(
            chat_id=chat_id,
            sender_id=sender_id,
            topic_raw=topic_val,
            thread_id=int(message_thread_id),
        )
        return msg if ok else msg

    setup_cmds = {"建立magi主題", "自動建立magi主題", "建立主題", "/setup_topics"}
    if low in setup_cmds or text in {"建立MAGI主題", "自動建立MAGI主題"}:
        ok, msg = _telegram_setup_topics(chat_id=chat_id, sender_id=sender_id)
        return msg if ok else msg

    return None


def _send_telegram_text(text: str) -> bool:
    msg = str(text or "").strip()
    if not msg:
        return False
    try:
        from skills.ops.red_phone import send_telegram_push_with_status  # lazy import

        st = send_telegram_push_with_status(
            msg,
            severity="warning",
            source="api_server",
            topic_key="alert",
            queue_on_fail=True,
        ) or {}
        if bool(st.get("telegram")) or bool(st.get("queued")):
            return True
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9430, exc_info=True)

    token = _load_openclaw_telegram_token()
    notify_ids = _load_notify_telegram_ids()
    if not token or not notify_ids:
        return False
    payload = {"text": msg}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ok_any = False
    for chat_id in notify_ids:
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage?chat_id={chat_id}",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=8):
                pass
            ok_any = True
        except Exception as e:
            logger.warning(f"⚠️ Telegram notify failed: {e}")
    return ok_any


def _notify_admin_telegram_once(text: str, dedupe_sec: int = 1800) -> None:
    token = _load_openclaw_telegram_token()
    notify_ids = _load_notify_telegram_ids()
    if not token or not notify_ids:
        return

    now = int(time.time())
    try:
        if os.path.exists(LINE_QUOTA_ALERT_FILE):
            prev = json.loads(Path(LINE_QUOTA_ALERT_FILE).read_text(encoding="utf-8"))
            last = int(prev.get("ts") or 0)
            if now - last < dedupe_sec:
                return
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9515, exc_info=True)

    _send_telegram_text(text)

    try:
        Path(LINE_QUOTA_ALERT_FILE).write_text(
            json.dumps({"ts": now, "reason": "line_quota"}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9525, exc_info=True)

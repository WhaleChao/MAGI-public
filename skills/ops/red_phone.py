"""
RED PHONE ALERT MODULE (紅色熱線)
=================================
Sends critical alerts to Admin via Telegram.

Security:
- NEVER hardcode tokens/IDs in source.
- Prefer env/config, with safe local fallbacks (last-seen binding files).
"""

import os
from typing import Any, Dict, List, Optional, Union
import json
import logging
import re
import time
import uuid
import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError
import sys

logger = logging.getLogger("RedPhone")

try:
    _TAIPEI_TIMEZONE = ZoneInfo("Asia/Taipei")
except Exception:
    _TAIPEI_TIMEZONE = timezone(timedelta(hours=8))


def _taipei_now() -> datetime:
    return datetime.now(_TAIPEI_TIMEZONE)


def _alert_timestamp() -> str:
    return _taipei_now().strftime("%Y-%m-%d %H:%M:%S（台灣時間）")

# =============================================================================
# Configuration
# =============================================================================
# LINE Messaging API
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from api.runtime_paths import get_config_path

def _load_runtime_dotenv(environ=None, loader=None) -> bool:
    """Load the deployment-declared environment for cron subprocesses.

    A sealed V3 launchd service exposes ``MAGI_ENV_FILE`` rather than copying
    notification secrets into every plist.  ``load_dotenv()`` without an
    explicit path searches the release working directory, where no ``.env``
    exists, and therefore made scheduled notifications look unconfigured.
    Existing process variables always win over the file.
    """

    env = os.environ if environ is None else environ
    try:
        if loader is None:
            from dotenv import load_dotenv as loader
        declared = str(env.get("MAGI_ENV_FILE") or "").strip()
        if declared:
            path = os.path.abspath(os.path.expanduser(declared))
            if not os.path.isfile(path):
                logging.getLogger(__name__).warning(
                    "MAGI_ENV_FILE does not exist: %s", path
                )
                return False
            return bool(loader(dotenv_path=path, override=False))
        return bool(loader(override=False))
    except Exception:
        logging.getLogger(__name__).debug(
            "runtime dotenv load failed", exc_info=True
        )
        return False


# Load the declared static external environment before reading notification
# configuration below.  This is intentionally non-overriding.
_load_runtime_dotenv()

_AGENT_DIR = os.path.abspath(
    os.path.expanduser(os.environ.get("MAGI_AGENT_DIR", "").strip())
    or os.path.join(_PROJECT_ROOT, ".agent")
)
LINE_LAST_SENDER_FILE = os.environ.get(
    "MAGI_LINE_LAST_SENDER_FILE",
    os.path.join(_AGENT_DIR, "line_last_sender.json"),
)

# Discord (optional) - webhook preferred; bot token fallback.
DISCORD_WEBHOOK_URL = (
    os.environ.get("MAGI_DISCORD_WEBHOOK_URL")
    or os.environ.get("MAGI_DISCORD_WEBHOOK")
    or os.environ.get("DISCORD_WEBHOOK_URL")
    or ""
).strip()
DISCORD_BOT_TOKEN = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
# 通知優先走 DISCORD_NOTIFY_CHANNEL_ID；fallback 到 DISCORD_CHANNEL_ID 的第一個值
_raw_dc_ids = (os.environ.get("DISCORD_CHANNEL_ID") or "").strip()
DISCORD_CHANNEL_ID = (
    os.environ.get("DISCORD_NOTIFY_CHANNEL_ID", "").strip()
    or _raw_dc_ids.split(",")[0].strip()
)
DISCORD_LAST_CHANNEL_FILE = os.environ.get(
    "MAGI_DISCORD_LAST_CHANNEL_FILE",
    os.path.join(_AGENT_DIR, "discord_last_channel.json"),
)
RED_PHONE_OUTBOX_FILE = os.environ.get(
    "MAGI_RED_PHONE_OUTBOX_FILE",
    os.path.join(_AGENT_DIR, "red_phone_outbox.json"),
)
RED_PHONE_DELIVERY_LOG = os.environ.get(
    "MAGI_RED_PHONE_DELIVERY_LOG",
    os.path.join(_AGENT_DIR, "red_phone_delivery.jsonl"),
)
_RUNTIME_DIR = os.path.abspath(
    os.path.expanduser(os.environ.get("MAGI_RUNTIME_DIR", "").strip())
    or os.path.join(_PROJECT_ROOT, ".runtime")
)
NOTIFICATION_DELIVERY_HEALTH_FILE = os.environ.get(
    "MAGI_NOTIFICATION_DELIVERY_HEALTH_FILE",
    os.path.join(_RUNTIME_DIR, "notification_delivery_health_latest.json"),
)
RED_PHONE_RETRY_COUNT = int(os.environ.get("MAGI_NOTIFY_RETRY_COUNT", "2") or "2")
RED_PHONE_RETRY_BACKOFF_SEC = float(os.environ.get("MAGI_NOTIFY_RETRY_BACKOFF_SEC", "1.0") or "1.0")
RED_PHONE_OUTBOX_MAX_RETRIES = int(os.environ.get("MAGI_NOTIFY_OUTBOX_MAX_RETRIES", "24") or "24")
RED_PHONE_OUTBOX_INFO_MAX_AGE_SEC = float(os.environ.get("MAGI_NOTIFY_OUTBOX_INFO_MAX_AGE_SEC", "21600") or "21600")
RED_PHONE_OUTBOX_MAX_AGE_SEC = float(os.environ.get("MAGI_NOTIFY_OUTBOX_MAX_AGE_SEC", "86400") or "86400")
RED_PHONE_TOPIC_MAP_FILE = os.environ.get(
    "MAGI_TELEGRAM_TOPIC_MAP_FILE",
    os.path.join(_AGENT_DIR, "telegram_topic_map.json"),
)
TELEGRAM_CHANNEL_STATE_FILE = os.environ.get(
    "MAGI_TELEGRAM_CHANNEL_STATE_FILE",
    os.path.join(_AGENT_DIR, "telegram_channel_state.json"),
)

if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
try:
    from api.tw_output_guard import normalize_output_text as _normalize_output_text
except Exception:
    _normalize_output_text = None


def _guard_text(text: str, platform: str) -> str:
    s = (text or "").strip()
    if not s:
        return s
    try:
        if _normalize_output_text:
            prev_enabled = os.environ.get("MAGI_TW_REVIEW_ENABLED")
            try:
                # Delivery should be fast and deterministic; skip review round-trips.
                os.environ["MAGI_TW_REVIEW_ENABLED"] = "0"
                return _normalize_output_text(s, platform=platform)
            finally:
                if prev_enabled is None:
                    os.environ.pop("MAGI_TW_REVIEW_ENABLED", None)
                else:
                    os.environ["MAGI_TW_REVIEW_ENABLED"] = prev_enabled
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 103, exc_info=True)
    return s


def _preview_text(text: str, limit: int = 180) -> str:
    s = " ".join(str(text or "").strip().split())
    if len(s) <= limit:
        return s
    return s[:limit] + "..."


def _split_text_by_lines(text: str, limit: int) -> list[str]:
    """Split long notification text without dropping content."""
    safe_limit = max(200, int(limit))
    source = str(text or "")
    if len(source) <= safe_limit:
        return [source]

    chunks: list[str] = []
    current = ""
    for raw_line in source.splitlines(keepends=True):
        line = raw_line
        while len(line) > safe_limit:
            if current:
                chunks.append(current.rstrip("\n"))
                current = ""
            chunks.append(line[:safe_limit])
            line = line[safe_limit:]
        candidate = current + line
        if len(candidate) > safe_limit:
            if current:
                chunks.append(current.rstrip("\n"))
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current.rstrip("\n"))
    return chunks or [source[:safe_limit]]


def _numbered_chunks(text: str, limit: int, *, reserve: int = 18) -> list[str]:
    chunks = _split_text_by_lines(text, max(200, int(limit) - reserve))
    if len(chunks) <= 1:
        return chunks
    total = len(chunks)
    return [f"({idx}/{total})\n{chunk}" for idx, chunk in enumerate(chunks, start=1)]


def _load_runtime_config() -> dict:
    # Keep this lightweight and optional; do not import server.py here.
    candidates = [
        os.path.join(_PROJECT_ROOT, "config.json"),
        os.path.abspath(os.path.join(_PROJECT_ROOT, "..", "code", "config.json")),
        os.path.abspath(os.path.join(_PROJECT_ROOT, "..", "config.json")),
    ]
    for p in candidates:
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            continue
    return {}


_RUNTIME_CONFIG = _load_runtime_config()


def _get_line_channel_access_token() -> str:
    token = (
        os.environ.get("MAGI_LINE_CHANNEL_ACCESS_TOKEN")
        or os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
        or ""
    )
    return (token or "").strip()


def _get_line_admin_targets() -> list[str]:
    # Prefer explicit admin list; fallback to last-sender binding.
    ids = [
        x.strip()
        for x in (os.environ.get("MAGI_ADMIN_LINE_IDS") or "").split(",")
        if x.strip()
    ]
    if ids:
        return ids

    allow_fallback = (
        os.environ.get("MAGI_LINE_FALLBACK_LAST_SENDER", "1").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if not allow_fallback:
        return []

    try:
        if os.path.exists(LINE_LAST_SENDER_FILE):
            with open(LINE_LAST_SENDER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            uid = (data.get("user_id") or "").strip()
            if uid:
                return [uid]
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 169, exc_info=True)
    return []


def _get_discord_channel_id_fallback() -> str:
    if DISCORD_CHANNEL_ID:
        return DISCORD_CHANNEL_ID
    try:
        if os.path.exists(DISCORD_LAST_CHANNEL_FILE):
            with open(DISCORD_LAST_CHANNEL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            cid = (data.get("channel_id") or "").strip()
            return cid
    except Exception:
        return ""
    return ""

# =============================================================================
# LINE Messaging API (Broadcast Message)
# =============================================================================

def _send_line_push_real(message: str, user_id: str) -> bool:
    """Send a LINE push message via Messaging API."""
    token = _get_line_channel_access_token()
    if not token or not user_id:
        return False
    safe_message = _guard_text(message, platform="LINE")
    chunks = _numbered_chunks(safe_message, 4900)
    batches = [chunks[i:i + 5] for i in range(0, len(chunks), 5)]
    if not batches:
        batches = [[""]]
    ok_any = False
    try:
        for batch in batches:
            payload = {
                "to": user_id,
                "messages": [{"type": "text", "text": chunk} for chunk in batch],
            }
            req = urlrequest.Request(
                "https://api.line.me/v2/bot/message/push",
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                method="POST",
            )
            with urlrequest.urlopen(req, timeout=10) as resp:
                ok_any = getattr(resp, "status", 0) == 200 or ok_any
        return ok_any
    except Exception as e:
        logger.error(f"[RED PHONE] LINE push error: {e}")
        return False


def send_line_push(message: str, user_id: Optional[str] = None) -> bool:
    """
    Send push message via LINE Messaging API.
    Falls back to Telegram if LINE is not configured.
    """
    targets = [user_id] if user_id else _get_line_admin_targets()
    if not targets or not _get_line_channel_access_token():
        logger.info("[RED PHONE] LINE not configured, falling back to Telegram.")
        safe_message = _guard_text(message, platform="TELEGRAM")
        return send_telegram_push(safe_message)
    ok = False
    for uid in targets:
        ok = _send_line_push_real(message, uid) or ok
    return ok


def _send_discord_webhook(message: str, webhook_url: str, severity: str) -> bool:
    colors = {
        "info": 0x3498DB,
        "warning": 0xF39C12,
        "critical": 0xE74C3C,
    }
    safe_message = _guard_text(message, platform="DISCORD")
    embed = {
        "title": "MAGI ALERT",
        "description": safe_message,
        "color": colors.get(severity, 0xF39C12),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "MAGI Iron Dome"},
    }
    payload = {"embeds": [embed]}
    try:
        req = urlrequest.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=10) as resp:
            # Discord webhooks commonly return 204.
            return getattr(resp, "status", 0) in (200, 204)
    except Exception as e:
        logger.error(f"[RED PHONE] Discord webhook error: {e}")
        return False


def _send_discord_bot_message(
    message: str,
    severity: str,
    *,
    topic_key: str = "",
    source: str = "",
) -> bool:
    if not DISCORD_BOT_TOKEN:
        return False
    default_channel_id = _get_discord_channel_id_fallback()
    if not default_channel_id:
        return False

    # 使用頻道路由器選擇目標頻道
    channel_id = default_channel_id
    try:
        from api.discord_channel_router import resolve_discord_channel
        _, routed_id = resolve_discord_channel(
            message,
            topic_key=topic_key,
            source=source,
            fallback_channel_id=default_channel_id,
        )
        if routed_id == "__SILENT__":
            return False
        if routed_id:
            channel_id = routed_id
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 289, exc_info=True)

    safe_message = _guard_text(message, platform="DISCORD")
    content = f"[{severity.upper()}] {safe_message}"
    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    # Discord 上限 2000 字元；超過時分段發送（按行拆分，保持可讀性）
    chunks: list[str] = []
    if len(content) <= 1900:
        chunks = [content]
    else:
        cur = ""
        for line in content.split("\n"):
            # 單行超過 1900：按 1900 字元切片（不丟棄）
            while len(line) > 1900:
                if cur:
                    chunks.append(cur)
                    cur = ""
                chunks.append(line[:1900])
                line = line[1900:]
            candidate = (cur + "\n" + line) if cur else line
            if len(candidate) > 1900:
                if cur:
                    chunks.append(cur)
                cur = line
            else:
                cur = candidate
        if cur:
            chunks.append(cur)
    if not chunks:
        chunks = [content[:1900]]
    any_ok = False
    for chunk in chunks:
        try:
            payload = {"content": chunk}
            req = urlrequest.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                    "User-Agent": "DiscordBot (https://magi.local, 1.0)",
                },
                method="POST",
            )
            with urlrequest.urlopen(req, timeout=10) as resp:
                if getattr(resp, "status", 0) in (200, 201):
                    any_ok = True
        except Exception as e:
            logger.error(f"[RED PHONE] Discord bot send chunk error: {e}")
    return any_ok


def send_discord_bot_file(
    file_path: str,
    *,
    caption: str = "",
    topic_key: str = "",
    source: str = "",
) -> bool:
    """透過 Discord Bot API 上傳檔案到路由後的頻道。"""
    if not DISCORD_BOT_TOKEN:
        return False
    if not file_path or not os.path.exists(file_path):
        return False
    default_channel_id = _get_discord_channel_id_fallback()
    if not default_channel_id:
        return False

    channel_id = default_channel_id
    try:
        from api.discord_channel_router import resolve_discord_channel
        _, routed_id = resolve_discord_channel(
            caption or os.path.basename(file_path),
            topic_key=topic_key,
            source=source,
            fallback_channel_id=default_channel_id,
        )
        if routed_id == "__SILENT__":
            return False
        if routed_id:
            channel_id = routed_id
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 341, exc_info=True)

    url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
    try:
        import io
        boundary = f"----MAGIBoundary{uuid.uuid4().hex[:12]}"
        body = io.BytesIO()

        def _write(s: str):
            body.write(s.encode("utf-8"))

        # JSON payload part (content text)
        if caption:
            safe_caption = _guard_text(caption, platform="DISCORD")[:1900]
            _write(f"--{boundary}\r\n")
            _write('Content-Disposition: form-data; name="payload_json"\r\n')
            _write("Content-Type: application/json\r\n\r\n")
            _write(json.dumps({"content": safe_caption}))
            _write("\r\n")

        # File part
        filename = os.path.basename(file_path)
        _write(f"--{boundary}\r\n")
        _write(f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n')
        _write("Content-Type: application/octet-stream\r\n\r\n")
        with open(file_path, "rb") as f:
            body.write(f.read())
        _write(f"\r\n--{boundary}--\r\n")

        data = body.getvalue()
        req = urlrequest.Request(
            url,
            data=data,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
                "User-Agent": "DiscordBot (https://magi.local, 1.0)",
            },
            method="POST",
        )
        with urlrequest.urlopen(req, timeout=30) as resp:
            ok = getattr(resp, "status", 0) in (200, 201)
            if ok:
                logger.info("[RED PHONE] DC file sent: %s → channel %s", filename, channel_id)
                # Mirror: 同時發送到測試伺服器
                try:
                    from api.discord_channel_router import resolve_discord_channel, get_mirror_channel_id
                    _sub = ""
                    try:
                        _sub, _ = resolve_discord_channel(caption or "", topic_key=topic_key, source=source, fallback_channel_id="")
                    except Exception:
                        pass
                    _mirror_id = get_mirror_channel_id(_sub) if _sub else ""
                    if _mirror_id and str(_mirror_id) != str(channel_id):
                        _mirror_url = f"https://discord.com/api/v10/channels/{_mirror_id}/messages"
                        _mirror_body = io.BytesIO()
                        def _mw(s: str):
                            _mirror_body.write(s.encode("utf-8"))
                        _mb = f"----MAGIMirror{uuid.uuid4().hex[:12]}"
                        if caption:
                            _mc = f"🪞 {_guard_text(caption, platform='DISCORD')[:1800]}"
                            _mw(f"--{_mb}\r\n")
                            _mw('Content-Disposition: form-data; name="payload_json"\r\n')
                            _mw("Content-Type: application/json\r\n\r\n")
                            _mw(json.dumps({"content": _mc}))
                            _mw("\r\n")
                        _mw(f"--{_mb}\r\n")
                        _mw(f'Content-Disposition: form-data; name="files[0]"; filename="{filename}"\r\n')
                        _mw("Content-Type: application/octet-stream\r\n\r\n")
                        with open(file_path, "rb") as _mf:
                            _mirror_body.write(_mf.read())
                        _mw(f"\r\n--{_mb}--\r\n")
                        _mreq = urlrequest.Request(
                            _mirror_url, data=_mirror_body.getvalue(),
                            headers={"Content-Type": f"multipart/form-data; boundary={_mb}", "Authorization": f"Bot {DISCORD_BOT_TOKEN}", "User-Agent": "DiscordBot (https://magi.local, 1.0)"},
                            method="POST",
                        )
                        with urlrequest.urlopen(_mreq, timeout=15) as _mr:
                            if getattr(_mr, "status", 0) in (200, 201):
                                logger.info("[RED PHONE] DC mirror file sent: %s → channel %s", filename, _mirror_id)
                except Exception as _mirr_err:
                    logger.debug("[RED PHONE] DC mirror failed: %s", _mirr_err)
            return ok
    except Exception as e:
        logger.error("[RED PHONE] DC file upload error: %s", e)
        return False


def send_discord_alert(message: str, webhook_url: Optional[str] = None, severity: str = "warning") -> bool:
    """
    Legacy compatibility shim.
    System notifications are now TG-only, so this routes to Telegram.
    
    Args:
        message: Alert message
        webhook_url: Discord webhook URL (optional, uses env var if not provided)
        severity: "info", "warning", or "critical" (affects embed color)
    
    Returns:
        True if sent successfully
    """
    _ = webhook_url, severity
    logger.info("[RED PHONE] send_discord_alert() redirected to Telegram (TG-only policy).")
    safe_message = _guard_text(message, platform="TELEGRAM")
    return send_telegram_push(safe_message)


def _parse_csv_ids(raw: str) -> list[str]:
    return [x.strip() for x in str(raw or "").split(",") if x and x.strip()]


def _get_telegram_config() -> tuple[str, list[str]]:
    """Get Telegram bot token and notify chat IDs from env/OpenClaw config."""
    # Keep the delivery path aligned with the readiness probes.  Production
    # historically stored the same credential under one of these names; only
    # accepting OPENCLAW_TELEGRAM_BOT_TOKEN made valid deployments look
    # unconfigured after a scheduler/environment migration.
    token = (
        os.environ.get("MAGI_TELEGRAM_BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    notify_ids = _parse_csv_ids(os.environ.get("MAGI_NOTIFY_TELEGRAM_IDS") or "")
    # Strict policy: push alerts should go to notify targets (group/topic), not admin DM.
    # Primary: MAGI config.json telegram section (avoids openclaw config validation issues)
    try:
        _magi_cfg_path = str(get_config_path("config.json"))
        if os.path.exists(_magi_cfg_path):
            with open(_magi_cfg_path, "r", encoding="utf-8") as f:
                _magi_cfg = json.load(f) or {}
            _magi_tg = _magi_cfg.get("telegram") or {}
            _magi_notify = _magi_tg.get("notifyTo") or []
            if isinstance(_magi_notify, list):
                notify_ids.extend([str(x).strip() for x in _magi_notify if str(x).strip()])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 430, exc_info=True)
    try:
        oc_path = os.path.join(os.path.expanduser("~"), ".openclaw", "openclaw.json")
        if os.path.exists(oc_path):
            with open(oc_path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            tg = (cfg.get("channels") or {}).get("telegram") or {}
            if not token:
                token = str(tg.get("botToken") or "").strip()
            notify_to = tg.get("notifyTo") or []
            if isinstance(notify_to, list):
                notify_ids.extend([str(x).strip() for x in notify_to if str(x).strip()])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 443, exc_info=True)

    # MAGI_NOTIFY_TELEGRAM_IDS is the preferred group/topic destination.  An
    # older installation may only have the administrator destination that is
    # also used by the official channel smoke test.  Falling back only when no
    # notify target exists prevents a temporary config migration from silently
    # dropping business reminders while preserving the group-first policy.
    if not notify_ids:
        notify_ids.extend(
            _parse_csv_ids(os.environ.get("MAGI_ADMIN_TELEGRAM_IDS") or "")
        )

    out: list[str] = []
    seen: set[str] = set()
    for x in notify_ids:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return token, out


def _canonical_topic_key(key: str) -> str:
    k = str(key or "").strip().lower()
    if not k:
        return ""
    aliases = {
        # 一般
        "general": "general",
        "default": "general",
        "一般": "general",
        "預設": "general",
        # 閱卷（繳費）
        "filereview_payment": "filereview_payment",
        "filereview-payment": "filereview_payment",
        "閱卷繳費": "filereview_payment",
        "繳費": "filereview_payment",
        # 閱卷（下載）
        "filereview_download": "filereview_download",
        "filereview-download": "filereview_download",
        "閱卷下載": "filereview_download",
        # 閱卷（聲請）
        "filereview_apply": "filereview_apply",
        "filereview-apply": "filereview_apply",
        "閱卷聲請": "filereview_apply",
        # 閱卷（通用 fallback）
        "filereview": "filereview",
        "file_review": "filereview",
        "file-review": "filereview",
        "docket": "filereview",
        "閱卷": "filereview",
        "卷宗": "filereview",
        # 筆錄
        "transcript": "transcript",
        "transcripts": "transcript",
        "transcript_dl": "transcript",
        "transcript_sync": "transcript",
        "筆錄": "transcript",
        # 法扶
        "laf": "laf",
        "legal_aid": "laf",
        "legal-aid": "laf",
        "法扶": "laf",
        "laf_general": "laf_general",
        "laf-general": "laf_general",
        "法扶一般": "laf_general",
        "laf_dispatch": "laf_dispatch",
        "laf-dispatch": "laf_dispatch",
        "laf_go_live": "laf_go_live",
        "laf-go-live": "laf_go_live",
        "laf_closing": "laf_closing",
        "laf-closing": "laf_closing",
        "laf_fee": "laf_fee",
        "laf-fee": "laf_fee",
        "laf_inquiry": "laf_inquiry",
        "laf-inquiry": "laf_inquiry",
        "laf_condition": "laf_condition",
        "laf-condition": "laf_condition",
        "laf_progress": "laf_progress",
        "laf-progress": "laf_progress",
        # 判決
        "judgment": "judgment",
        "judgments": "judgment",
        "判決": "judgment",
        "司法院": "judgment",
        # 司法院 API（夜間拉取專用）
        "judicial_api": "judicial_api",
        "judicial-api": "judicial_api",
        "judicialapi": "judicial_api",
        "司法院api": "judicial_api",
        "夜間拉取": "judicial_api",
        # 逐字稿
        "verbatim": "verbatim",
        "逐字稿": "verbatim",
        "音訊": "verbatim",
        # 翻譯
        "translation": "translation",
        "translate": "translation",
        "翻譯": "translation",
        # 摘要
        "summary": "summary",
        "summarize": "summary",
        "摘要": "summary",
        # 案件庭期／衝庭（業務通知，TG 與 DC 同步）
        "case_schedule": "case_schedule",
        "case-schedule": "case_schedule",
        "hearing": "case_schedule",
        "hearing_conflict": "case_schedule",
        "hearing-conflict": "case_schedule",
        "庭期": "case_schedule",
        "衝庭": "case_schedule",
        # 股票
        "market": "market",
        "stock": "market",
        "stocks": "market",
        "股票": "market",
        "股市": "market",
        # 檢查
        "check": "check",
        "checks": "check",
        "health": "check",
        "autopilot": "check",
        "monitor": "check",
        "檢查": "check",
        "巡檢": "check",
        # 夜間
        "nightly": "nightly",
        "夜間": "nightly",
        "改善": "nightly",
        "夜間會議": "nightly",
        # 警告
        "warning": "alert",
        "warn": "alert",
        "critical": "alert",
        "error": "alert",
        "alarm": "alert",
        "alert": "alert",
        "security": "alert",
        "iron_dome": "alert",
        "irondome": "alert",
        "鐵穹": "alert",
        "警報": "alert",
        "警告": "alert",
        "self_repair": "alert",
        "self-repair": "alert",
        "repair": "alert",
        "quiet_cron": "check",
        "quiet-cron": "check",
    }
    return aliases.get(k, k)


def _is_unknown_business_topic_key(key: str) -> bool:
    k = str(key or "").strip().lower()
    if not k:
        return False
    known = {
        "general", "filereview", "filereview_payment", "filereview_download", "filereview_apply",
        "transcript", "laf", "laf_general", "laf_dispatch", "laf_go_live", "laf_closing",
        "laf_fee", "laf_inquiry", "laf_condition", "laf_progress", "judgment", "judicial_api",
        "verbatim", "translation", "summary", "case_schedule", "market", "check", "nightly", "alert",
        "filing", "research_daily", "research_interpretation", "research_ethno",
        "research_humanrights", "research_language", "research_eastasia",
    }
    if k in known:
        return False
    return k.startswith((
        "laf_",
        "filereview_",
        "file_review_",
        "research_",
        "transcript_",
        "verbatim_",
        "summary_",
        "translation_",
        "judgment_",
        "filing_",
        "case_schedule_",
        "pdf_",
    ))


DEFAULT_TELEGRAM_FORUM_TOPICS: dict[str, str] = {
    "check": "MAGI 巡檢",
    "alert": "MAGI 警報",
    "nightly": "MAGI 夜間",
    "filereview": "閱卷總覽",
    "filereview_payment": "閱卷繳費",
    "filereview_download": "閱卷下載",
    "filereview_apply": "閱卷聲請",
    "laf": "法扶總覽",
    "laf_general": "法扶巡檢",
    "laf_dispatch": "法扶派案",
    "laf_go_live": "法扶開辦",
    "laf_closing": "法扶報結",
    "laf_fee": "法扶費用",
    "laf_inquiry": "法扶疑義",
    "laf_condition": "法扶附條件",
    "laf_progress": "法扶進度",
    "transcript": "筆錄",
    "judgment": "判決與裁判",
    "judicial_api": "司法院 API",
    "verbatim": "逐字稿",
    "translation": "翻譯",
    "summary": "摘要",
    "filing": "歸檔",
    "case_schedule": "庭期與衝庭",
    "market": "市場",
}


def _normalize_topic_map(raw: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        ck = _canonical_topic_key(str(k or ""))
        if not ck:
            continue
        try:
            tid = int(v)
        except Exception:
            continue
        if tid > 0:
            out[ck] = tid
    return out


def _write_topic_map(topic_map: dict[str, int], *, chat_id: str = "", source: str = "ensure") -> None:
    normalized = _normalize_topic_map(topic_map)
    os.makedirs(os.path.dirname(RED_PHONE_TOPIC_MAP_FILE), exist_ok=True)
    tmp = RED_PHONE_TOPIC_MAP_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, RED_PHONE_TOPIC_MAP_FILE)

    state = {
        "version": 1,
        "updated_at": datetime.now().isoformat(),
        "source": source,
        "chat_id": str(chat_id or ""),
        "topicMap": normalized,
    }
    tmp_state = TELEGRAM_CHANNEL_STATE_FILE + ".tmp"
    with open(tmp_state, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp_state, TELEGRAM_CHANNEL_STATE_FILE)


def ensure_telegram_forum_topics(
    *,
    topic_names: Optional[dict[str, str]] = None,
    dry_run: bool = False,
) -> dict:
    """Ensure MAGI Telegram forum topics exist and persist their thread IDs.

    Telegram Bot API cannot list existing forum topics, so this helper is
    intentionally conservative: it only creates topics missing from the local
    topic map, then records the returned ``message_thread_id`` for routing.
    """

    token, chat_ids = _get_telegram_config()
    topic_names = topic_names or DEFAULT_TELEGRAM_FORUM_TOPICS
    existing = _load_topic_map()
    out = {
        "ok": False,
        "dry_run": bool(dry_run),
        "chat_id": str(chat_ids[0]) if chat_ids else "",
        "is_forum": False,
        "existing_count": len(existing),
        "created": {},
        "skipped": {},
        "errors": [],
        "topic_map_file": RED_PHONE_TOPIC_MAP_FILE,
        "state_file": TELEGRAM_CHANNEL_STATE_FILE,
    }
    if not token or not chat_ids:
        out["errors"].append("telegram token/admin ids missing")
        return out

    chat_id = str(chat_ids[0])
    try:
        req = urlrequest.Request(
            f"https://api.telegram.org/bot{token}/getChat?chat_id={chat_id}",
            method="GET",
        )
        with urlrequest.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        chat = payload.get("result") or {}
        out["is_forum"] = bool(chat.get("is_forum"))
        out["chat_title"] = str(chat.get("title") or "")
        if not out["is_forum"]:
            out["errors"].append("telegram target chat is not a forum supergroup")
            return out
    except Exception as exc:
        out["errors"].append(f"getChat failed: {type(exc).__name__}: {str(exc)[:200]}")
        return out

    merged = dict(existing)
    for raw_key, title in topic_names.items():
        key = _canonical_topic_key(raw_key)
        if not key:
            continue
        if key in merged and int(merged[key]) > 0:
            out["skipped"][key] = int(merged[key])
            continue
        if dry_run:
            out["created"][key] = {"title": title, "dry_run": True}
            continue
        body = json.dumps({"chat_id": chat_id, "name": str(title or key)[:128]}, ensure_ascii=False).encode("utf-8")
        try:
            req = urlrequest.Request(
                f"https://api.telegram.org/bot{token}/createForumTopic",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlrequest.urlopen(req, timeout=12) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            result = payload.get("result") or {}
            tid = int(result.get("message_thread_id") or 0)
            if tid <= 0:
                raise RuntimeError("missing message_thread_id")
            merged[key] = tid
            out["created"][key] = {"title": title, "message_thread_id": tid}
        except Exception as exc:
            out["errors"].append(f"{key}: createForumTopic failed: {type(exc).__name__}: {str(exc)[:240]}")

    if not dry_run:
        _write_topic_map(merged, chat_id=chat_id, source="ensure_telegram_forum_topics")
    out["topic_map"] = merged
    out["ok"] = not out["errors"] and (
        bool(dry_run) or all(_canonical_topic_key(k) in merged for k in topic_names)
    )
    return out


def _load_topic_map() -> dict[str, int]:
    merged: dict[str, int] = {}

    env_json = (
        os.environ.get("MAGI_TG_TOPIC_MAP")
        or os.environ.get("MAGI_TG_TOPIC_MAP_JSON")
        or ""
    ).strip()
    if env_json:
        try:
            merged.update(_normalize_topic_map(json.loads(env_json)))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 583, exc_info=True)

    try:
        if os.path.exists(RED_PHONE_TOPIC_MAP_FILE):
            with open(RED_PHONE_TOPIC_MAP_FILE, "r", encoding="utf-8") as f:
                merged.update(_normalize_topic_map(json.load(f) or {}))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 590, exc_info=True)
    try:
        _magi_cfg_path = str(get_config_path("config.json"))
        if os.path.exists(_magi_cfg_path):
            with open(_magi_cfg_path, "r", encoding="utf-8") as f:
                _magi_cfg = json.load(f) or {}
            _magi_tg = _magi_cfg.get("telegram") or {}
            merged.update(_normalize_topic_map(_magi_tg.get("topicMap") or {}))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 599, exc_info=True)
    try:
        oc_path = os.path.join(os.path.expanduser("~"), ".openclaw", "openclaw.json")
        if os.path.exists(oc_path):
            with open(oc_path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
            tg = (cfg.get("channels") or {}).get("telegram") or {}
            merged.update(_normalize_topic_map(tg.get("topicMap") or {}))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 608, exc_info=True)
    try:
        if os.path.exists(TELEGRAM_CHANNEL_STATE_FILE):
            with open(TELEGRAM_CHANNEL_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f) or {}
            merged.update(_normalize_topic_map((state or {}).get("topicMap") or {}))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 615, exc_info=True)

    default_tid = (
        os.environ.get("MAGI_TG_TOPIC_DEFAULT")
        or os.environ.get("MAGI_TG_THREAD_DEFAULT")
        or ""
    ).strip()
    if default_tid:
        try:
            dv = int(default_tid)
            if dv > 0:
                merged["general"] = dv
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 628, exc_info=True)

    return merged


def _infer_topic_key(message: str, source: str, severity: str) -> str:
    s = (str(source or "") + " " + str(message or "")).lower()
    src = str(source or "").strip().lower()
    if src in {
        "business_module_live_check",
        "nightly_regression",
        "mock_test",
    }:
        return "check"
    if src in {
        "nightly_distill_gemma",
        "weekend_resummary",
        "nightly_health_report",
    }:
        return "nightly"
    if src in {
        "disk_low_water_alarm",
        "backup_market_watchlist",
        "outbox",
    }:
        return "alert"
    if any(
        k in s
        for k in [
            "daily dual-db sync audit report",
            "dual-db sync",
            "db sync",
            "sync audit",
            "db_dual_sync",
            "topic 測試",
            "topic test",
            "tg topic",
            "telegram topic",
        ]
    ):
        return "check"
    if any(k in s for k in ["judicial_api", "夜間拉取", "night_pull", "api_night"]):
        return "judicial_api"
    # 法扶相關（報結、派案、開辦等）必須在「判決」之前判斷，
    # 因為報結訊息常含「判決書」字樣，會誤判到 judgment topic。
    if any(k in s for k in ["法扶", "laf", "legal aid", "legal_aid", "報結",
                             "派案", "開辦", "扶助", "laf_", "待報結",
                             "費用", "疑義", "二階段", "附條件",
                             "closing_report", "laf_closing", "laf_dispatch"]):
        if src == "laf_nightly_audit" or any(k in s for k in [
            "法扶夜間巡檢報告",
            "巡檢報告",
            "案件總數",
            "自動補填法扶案號",
            "仍待確認法扶案號",
        ]):
            return "laf_general"
        if any(k in s for k in ["進度回報", "laf_progress", "未結案件進度", "confirm_token"]):
            return "laf_progress"
        if any(k in s for k in ["新法扶派案", "派案已建立", "派案", "dispatch", "新案", "審查結果", "准予扶助"]):
            return "laf_dispatch"
        if any(k in s for k in ["go_live", "go-live", "開辦", "開辦回報", "開辦暫存"]):
            return "laf_go_live"
        if any(k in s for k in ["二階段", "附條件", "condition"]):
            return "laf_condition"
        if any(k in s for k in ["費用", "酬金", "領款", "fee"]):
            return "laf_fee"
        if any(k in s for k in ["疑義", "inquiry", "不合標準"]):
            return "laf_inquiry"
        if any(k in s for k in ["報結", "結案", "closing", "待報結", "closing_report", "laf_closing"]):
            return "laf_closing"
        return "laf"
    if any(k in s for k in ["判決", "judgment", "司法院", "裁判"]):
        return "judgment"
    if any(k in s for k in ["逐字稿", "verbatim", "音訊轉文字"]):
        return "verbatim"
    if any(k in s for k in ["翻譯", "translation", "translate", "tri_sage", "tri-sage"]):
        return "translation"
    if any(k in s for k in ["摘要", "summary", "summarize", "重點整理"]):
        return "summary"
    if src in {"hearing_conflict", "court_hearing_reminder"} or any(
        k in s for k in ["衝庭", "庭期衝突", "開庭提醒", "請假狀", "聲請改期"]
    ):
        return "case_schedule"
    file_review_download_signal = any(
        k in s
        for k in [
            "卷宗下載",
            "下載完成",
            "已下載",
            "download",
            "可下載判定",
            "可下載通知",
            "入口列表可下載",
            "法院端可下載",
            "待下載",
        ]
    )
    payment_zero_only = bool(
        re.search(r"(?:待繳費|入口列表待繳費|繳費相關信件)[：:\s]*0\s*(?:件|封)", s)
    )
    payment_positive_count = bool(
        re.search(r"(?:待繳費|入口列表待繳費|繳費相關信件)[^0-9]{0,12}[1-9]\d*\s*(?:件|封)", s)
    )
    payment_action_text = any(
        k in s
        for k in ["繳費單通知", "繳費單 pdf", "逾期未繳", "繳費憑證", "上傳繳費", "待繳費案件"]
    )
    if any(k in s for k in ["繳費", "payment"]):
        if not (file_review_download_signal and payment_zero_only and not payment_positive_count and not payment_action_text):
            return "filereview_payment"
    if file_review_download_signal:
        return "filereview_download"
    if any(k in s for k in ["閱卷", "電子卷", "file_review", "file-review", "docket", "可下載", "卷宗", "卷期", "卷下來"]):
        # 閱卷通知優先查繳費
        if any(k in s for k in ["繳費單", "待繳費", "逾期未繳"]):
            if not (payment_zero_only and not payment_positive_count and not payment_action_text):
                return "filereview_payment"
        return "filereview"
    if any(k in s for k in ["歸檔", "filing", "pdf_namer", "casper 歸檔"]):
        return "filing"
    if any(k in s for k in ["筆錄", "transcript"]):
        return "transcript"
    if any(k in s for k in ["股市", "股票", "market", "qqq", "tsla", "aapl", "vt"]):
        return "market"
    if any(k in s for k in ["巡檢", "檢查", "autopilot", "health", "monitor", "status", "診斷"]):
        return "check"
    if any(k in s for k in ["夜間", "nightly", "改善建議", "夜間會議"]):
        return "nightly"
    if any(k in s for k in ["警報", "警告", "iron dome", "iron_dome", "alert", "鐵穹"]):
        return "alert"
    # 法扶已在上方提前判斷，此處保留以防萬一（不會重複 return）
    if str(severity or "").lower() in {"critical", "warning"}:
        return "alert"
    return "general"


def _effective_notification_topic(
    message: str,
    source: str = "",
    severity: str = "",
    topic_key: str = "",
) -> tuple[str, str, str]:
    """Return (inferred_topic, requested_topic, effective_topic)."""
    inferred = _canonical_topic_key(_infer_topic_key(message, source, severity))
    requested = _canonical_topic_key(topic_key)
    effective = requested or inferred
    if requested in {"laf", "filereview"} and inferred.startswith(requested + "_"):
        effective = inferred
    return inferred, requested, effective


def _notification_source_class(source: str = "", topic_key: str = "") -> str:
    topic = _canonical_topic_key(topic_key)
    src = re.sub(r"[^a-z0-9_:-]+", "_", str(source or "").strip().lower()).strip("_")
    if topic in {"check", "alert", "nightly", "judicial_api"} or src in {
        "business_module_live_check",
        "nightly_regression",
        "mock_test",
        "nightly_health_report",
        "weekend_resummary",
        "nightly_distill_gemma",
    }:
        return "system"
    if topic.startswith("filereview") or src.startswith(("file_review", "filereview")):
        return "file_review"
    if topic.startswith("laf") or src.startswith("laf"):
        return "laf"
    if topic in {"transcript", "verbatim", "summary", "translation", "filing", "case_schedule", "judgment", "market"}:
        return topic
    return src or topic or "direct"


def _dedupe_keep_order(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        v = str(value or "").strip()
        if not v or v in seen:
            continue
        seen.add(v)
        out.append(v)
    return out


def _normalize_case_token(value: str) -> str:
    s = str(value or "").strip().lower()
    if not s:
        return ""
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    s = re.sub(r"[\s._,，、:：;；()（）【】\[\]《》<>]+", "", s)
    s = s.replace("臺", "台")
    for token in ("年度", "年", "字第", "字", "第", "號"):
        s = s.replace(token, "")
    s = re.sub(r"(?<=[^\d])0+(\d+)", r"\1", s)
    return s


_COURT_CASE_RE = re.compile(
    r"\d{2,3}\s*(?:年度|年)?\s*[\u4e00-\u9fff]{1,10}\s*(?:字)?\s*(?:第)?\s*\d{1,6}\s*(?:號)?"
)
_LAF_CASE_RE = re.compile(r"\b\d{6,7}-[A-Za-z]-\d{3}\b")
_INTERNAL_CASE_RE = re.compile(r"\b20\d{2}-\d{4}\b")
_PDF_NAME_RE = re.compile(r"[\w\u4e00-\u9fff（）()【】《》「」『』\-_.]+\.pdf", re.IGNORECASE)


def _extract_notification_identity_tokens(message: str) -> dict[str, list[str]]:
    s = str(message or "")
    court_cases = [_normalize_case_token(x.group(0)) for x in _COURT_CASE_RE.finditer(s)]
    laf_cases = [x.group(0).upper() for x in _LAF_CASE_RE.finditer(s)]
    internal_cases = [x.group(0) for x in _INTERNAL_CASE_RE.finditer(s)]
    pdf_names = []
    for match in _PDF_NAME_RE.finditer(s):
        raw_name = re.sub(r"\s+", " ", match.group(0).strip()).lower()
        raw_name = re.sub(r"^.*[\\/｜:：;；,，)）]", "", raw_name)
        raw_name = raw_name.lstrip("-•· ")
        if raw_name:
            pdf_names.append(raw_name)
    accounts = [
        re.sub(r"\D+", "", x)
        for x in re.findall(r"(?:銷帳編號|轉入帳號|繳費帳號|帳號)[^0-9]{0,10}([0-9]{6,})", s, flags=re.IGNORECASE)
    ]
    amounts = [
        x.replace(",", "")
        for x in re.findall(r"(?:NT\$|NTD|新臺幣|新台幣|應繳金額|應繳|金額|費用|\$)[^0-9]{0,8}([0-9][0-9,]*)", s, flags=re.IGNORECASE)
    ]
    return {
        "court": _dedupe_keep_order([x for x in court_cases if x]),
        "laf": _dedupe_keep_order(laf_cases),
        "internal": _dedupe_keep_order(internal_cases),
        "pdf": _dedupe_keep_order(pdf_names),
        "account": _dedupe_keep_order([x for x in accounts if x]),
        "amount": _dedupe_keep_order([x for x in amounts if x]),
    }


def _notification_event_signature(message: str, source_class: str, topic_key: str) -> str:
    topic = _canonical_topic_key(topic_key)
    tokens = _extract_notification_identity_tokens(message)

    def join_tokens(names: list[str]) -> str:
        parts: list[str] = []
        for name in names:
            values = tokens.get(name) or []
            if values:
                parts.append(f"{name}={','.join(sorted(values))}")
        return "|".join(parts)

    if _is_noop_completion_notification(message):
        quiet_topic = topic or "general"
        if quiet_topic.startswith("filereview"):
            quiet_topic = "filereview"
        return f"noop:{source_class}:{quiet_topic}"

    if topic == "filereview_payment":
        ident = join_tokens(["court", "laf", "internal", "account", "amount", "pdf"])
        return f"{topic}|{ident}" if ident else ""
    if topic == "filereview_download":
        ident = join_tokens(["court", "laf", "internal", "pdf"])
        return f"{topic}|{ident}" if ident else ""
    if topic == "filereview_apply":
        ident = join_tokens(["court", "laf", "internal"])
        return f"{topic}|{ident}" if ident else ""
    if topic == "transcript":
        ident = join_tokens(["court", "laf", "internal", "pdf"])
        return f"{topic}|{ident}" if ident else ""
    if topic == "laf_dispatch":
        ident = join_tokens(["laf", "internal", "pdf"])
        return f"{topic}|{ident}" if ident else ""
    return ""


def _normalize_notification_body_for_dedup(message: str) -> str:
    body = " ".join(str(message or "").strip().split())
    if not body:
        return ""
    body = re.sub(r"\b20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?\b", "<timestamp>", body)
    body = re.sub(r"\b\d{8}_\d{6}_[0-9a-f]{6,}\b", "<job_id>", body, flags=re.IGNORECASE)
    body = re.sub(r"\brp_\d{8}_\d{6}_[0-9a-f]{6,}\b", "<outbox_id>", body, flags=re.IGNORECASE)
    return body


def classify_notification_event(
    message: str,
    *,
    source: str = "",
    severity: str = "warning",
    topic_key: str = "",
) -> dict[str, str]:
    """Classify a notification into stable topic/source/dedup buckets."""
    inferred, requested, effective = _effective_notification_topic(
        message,
        source=source,
        severity=severity,
        topic_key=topic_key,
    )
    source_class = _notification_source_class(source, effective)
    body = _normalize_notification_body_for_dedup(message)
    event_signature = _notification_event_signature(body, source_class, effective)
    digest_basis = event_signature or f"body:{body}"
    digest = hashlib.sha256("\n".join([source_class, effective, digest_basis]).encode("utf-8", "ignore")).hexdigest()
    return {
        "source": str(source or "direct"),
        "source_class": source_class,
        "inferred_topic": inferred,
        "requested_topic": requested,
        "topic_key": effective,
        "dedup_key": f"{source_class}:{effective}:{digest}",
        "dedup_hash": digest,
        "event_signature": event_signature,
    }


_NO_GENERAL_TG_FALLBACK_TOPICS = {
    "filereview",
    "filereview_payment",
    "filereview_download",
    "filereview_apply",
    "laf",
    "laf_dispatch",
    "laf_go_live",
    "laf_closing",
    "laf_fee",
    "laf_inquiry",
    "laf_condition",
    "laf_progress",
    "transcript",
}


_NOOP_COMPLETION_MARKERS = (
    "檢查完成",
    "掃描完成",
    "判定完成",
    "巡檢完成",
    "健康檢查",
    "狀態掃描完成",
    "目前無新通知",
    "無新通知",
    "沒有新資訊",
    "沒有新資料",
    "沒有新",
    "查無筆錄",
    "無待下載",
    "無待繳費",
    "無需處理",
    "全部已處理",
)
_ACTIONABLE_ERROR_MARKERS = (
    "探測失敗",
    "登入失敗",
    "下載失敗",
    "上傳失敗",
    "發送失敗",
    "授權失敗",
    "錯誤",
    "異常",
    "invalid_grant",
    "need_interactive_oauth",
)
_COUNT_RE = re.compile(r"([0-9]+)\s*(件|封|份|案|個|筆|部|次)")


def _has_positive_action_count(message: str) -> bool:
    s = str(message or "")
    for match in _COUNT_RE.finditer(s):
        try:
            count = int(match.group(1))
        except Exception:
            continue
        if count <= 0:
            continue
        before = s[max(0, match.start() - 18): match.start()]
        after = s[match.end(): min(len(s), match.end() + 18)]
        window = before + match.group(0) + after
        if any(k in window for k in ("略過", "已歸檔", "已下載略過", "歷史/已完成", "原始")):
            continue
        if re.search(r"(?:已通知|已處理|已完成|掃描|巡檢|檢查|查無[^：:]{0,8}|無新|沒有新|總數|案件總數)[：:\s/（(]*$", before):
            continue
        if re.match(r"\s*(?:個月|列|row|rows|已通知|已處理|已完成|已略過|略過)", after, flags=re.IGNORECASE):
            continue
        return True
    return False


def _is_noop_completion_notification(message: str) -> bool:
    """True when a periodic completion report has no actionable new items."""
    s = " ".join(str(message or "").strip().split())
    if not s:
        return False
    s_lower = s.lower()
    if any(marker in s_lower for marker in _ACTIONABLE_ERROR_MARKERS):
        return False
    if _has_positive_action_count(s):
        return False
    has_completion_marker = any(marker in s for marker in _NOOP_COMPLETION_MARKERS)
    has_zero_count = bool(_COUNT_RE.search(s))
    if has_completion_marker and (has_zero_count or any(k in s for k in ("無新", "沒有新", "無需處理", "查無"))):
        return True
    if "0封" in s or "0件" in s or "0 份" in s or "0 案" in s:
        return has_completion_marker
    return False


def _quiet_suppression_status(
    message: str,
    *,
    severity: str,
    source: str,
    topic_key: str,
) -> Optional[dict]:
    sev = str(severity or "info").strip().lower()
    if sev == "critical":
        return None
    if not _is_noop_completion_notification(message):
        return None
    event = classify_notification_event(message, source=source, severity=severity, topic_key=topic_key)
    _append_delivery_log(
        {
            "event": "suppressed",
            "reason": "noop_completion",
            "source": source,
            "severity": severity,
            "topic_key": event["topic_key"],
            "preview": _preview_text(message),
        }
    )
    return {
        "telegram": False,
        "delivered": False,
        "acked": 0,
        "total": 0,
        "queued": False,
        "outbox_id": "",
        "error": "",
        "topic_key": event["topic_key"],
        "thread_id": 0,
        "suppressed": True,
        "suppressed_reason": "noop_completion",
    }


def _resolve_thread_id(message: str, source: str, severity: str, topic_key: str = "") -> tuple[str, Optional[int]]:
    event = classify_notification_event(message, source=source, severity=severity, topic_key=topic_key)
    key = event["topic_key"]
    tmap = _load_topic_map()
    if not tmap:
        return key, None
    if key in tmap:
        return key, int(tmap[key])
    # Fallback: filereview_payment → filereview, laf_dispatch → laf, judicial_api → judgment, etc.
    _TG_TOPIC_FALLBACK = {
        "filereview_payment": "filereview",
        "filereview_download": "filereview",
        "filereview_apply": "filereview",
        "laf_dispatch": "laf",
        "laf_general": "general",
        "laf_closing": "laf",
        "judicial_api": "judgment",
    }
    fb = _TG_TOPIC_FALLBACK.get(key, "")
    if fb and fb in tmap:
        return key, int(tmap[fb])
    if key in _NO_GENERAL_TG_FALLBACK_TOPICS:
        return key, None
    if _is_unknown_business_topic_key(key):
        return key, None
    if "general" in tmap:
        return (key or "general"), int(tmap["general"])
    return key, None


def _append_delivery_log(event: dict) -> None:
    try:
        os.makedirs(_AGENT_DIR, exist_ok=True)
        event = dict(event or {})
        event.setdefault("ts", datetime.now().isoformat())
        with open(RED_PHONE_DELIVERY_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 720, exc_info=True)


def _load_outbox() -> list[dict]:
    try:
        if os.path.exists(RED_PHONE_OUTBOX_FILE):
            with open(RED_PHONE_OUTBOX_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or []
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 731, exc_info=True)
    return []


def _outbox_fingerprint(message: str, severity: str = "", topic_key: str = "", source: str = "") -> str:
    sev = str(severity or "warning").strip().lower()
    event = classify_notification_event(
        message,
        source=source,
        severity=sev,
        topic_key=topic_key,
    )
    raw = "\n".join([sev, event["dedup_key"]])
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


def _outbox_entry_age_seconds(entry: dict, now_ts: float) -> float:
    created = str((entry or {}).get("created_at") or "").strip()
    if not created:
        return 0.0
    try:
        dt = datetime.fromisoformat(created)
        return max(0.0, now_ts - dt.timestamp())
    except Exception:
        return 0.0


def _outbox_entry_max_age_seconds(entry: dict) -> float:
    severity = str((entry or {}).get("severity") or "").strip().lower()
    if severity in {"info", "notice", "debug"}:
        return max(60.0, float(RED_PHONE_OUTBOX_INFO_MAX_AGE_SEC))
    return max(3600.0, float(RED_PHONE_OUTBOX_MAX_AGE_SEC))


def _is_machine_retry_entry(entry: dict) -> bool:
    """Recognize pre-policy entries that have the complete retry schema.

    rc213 created durable retry entries before ``delivery_policy`` was added.
    Treating every such row as a human decision stranded valid business
    reminders.  Only rows with a generated ID, content fingerprint, explicit
    source/topic, timestamps and retry metadata are safe to migrate.  Anything
    ambiguous remains fail-closed on ``legacy_manual_hold``.
    """

    if not isinstance(entry, dict):
        return False
    entry_id = str(entry.get("id") or "")
    fingerprint = str(entry.get("fingerprint") or "")
    source = str(entry.get("source") or "").strip()
    topic = str(entry.get("topic_key") or "").strip()
    created = str(entry.get("created_at") or "").strip()
    if not re.fullmatch(r"rp_\d{8}_\d{6}_[0-9a-f]{8}", entry_id):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        return False
    if not source or not topic or not created:
        return False
    try:
        datetime.fromisoformat(created)
        int(entry.get("attempts") or 0)
        float(entry.get("next_retry_at") or 0.0)
    except (TypeError, ValueError):
        return False
    return bool(str(entry.get("message") or "").strip())


def _delivery_policy_for_entry(entry: dict) -> str:
    declared = str((entry or {}).get("delivery_policy") or "").strip()
    if declared in {"auto_retry", "legacy_manual_hold"}:
        return declared
    return "auto_retry" if _is_machine_retry_entry(entry) else "legacy_manual_hold"


def _save_outbox(items: list[dict]) -> None:
    try:
        os.makedirs(_AGENT_DIR, exist_ok=True)
        tmp = RED_PHONE_OUTBOX_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, RED_PHONE_OUTBOX_FILE)
    except Exception as e:
        logger.warning("[RED PHONE] failed to save outbox: %s", e)


def _write_notification_delivery_health(
    items: list[dict], *, checked: int, recovered: int
) -> None:
    """Publish a PII-free delivery health artifact for the system health index."""

    now_ts = time.time()
    token, targets = _get_telegram_config()
    auto_items = [
        item
        for item in items
        if _delivery_policy_for_entry(item) == "auto_retry"
    ]
    held_items = [item for item in items if item not in auto_items]
    ages = [_outbox_entry_age_seconds(item, now_ts) for item in auto_items]
    oldest_age = max(ages, default=0.0)
    stale_pending = any(
        age > min(900.0, _outbox_entry_max_age_seconds(item))
        for item, age in zip(auto_items, ages)
    )
    config_ok = bool(token and targets)
    ok = bool(config_ok and not stale_pending)
    error_categories = sorted(
        {
            (
                "missing_credentials"
                if "missing" in str(item.get("last_error") or "").lower()
                else "delivery_error"
            )
            for item in items
            if str(item.get("last_error") or "").strip()
        }
    )
    payload = {
        "ok": ok,
        "status": "ok" if ok else ("misconfigured" if not config_ok else "retry_backlog_stale"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "has_token": bool(token),
        "target_count": len(targets),
        "checked": int(checked),
        "recovered": int(recovered),
        "remaining": len(items),
        "auto_retry_pending": len(auto_items),
        "manual_hold_pending": len(held_items),
        "oldest_pending_age_seconds": round(oldest_age, 1),
        "error_categories": error_categories,
    }
    try:
        path = os.path.abspath(os.path.expanduser(NOTIFICATION_DELIVERY_HEALTH_FILE))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temp = f"{path}.{os.getpid()}.tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp, path)
    except Exception:
        logger.debug("notification delivery health write failed", exc_info=True)


def _enqueue_outbox(
    message: str,
    severity: str,
    source: str,
    last_error: str = "",
    topic_key: str = "",
    mirror_to_discord: bool = True,
) -> str:
    entry_id = f"rp_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    now_ts = time.time()
    event = classify_notification_event(message, source=source, severity=severity, topic_key=topic_key)
    effective_topic = event["topic_key"]
    fingerprint = _outbox_fingerprint(message, severity=severity, topic_key=effective_topic, source=source)
    outbox = _load_outbox()
    for existing in outbox:
        existing_fp = str(existing.get("fingerprint") or "")
        if not existing_fp:
            existing_fp = _outbox_fingerprint(
                str(existing.get("message") or ""),
                severity=str(existing.get("severity") or ""),
                topic_key=str(existing.get("topic_key") or ""),
                source=str(existing.get("source") or ""),
            )
            existing["fingerprint"] = existing_fp
        if existing_fp == fingerprint:
            existing["updated_at"] = datetime.now().isoformat()
            existing["last_error"] = str(last_error or existing.get("last_error") or "")[:600]
            existing["mirror_to_discord"] = bool(existing.get("mirror_to_discord", True)) and bool(mirror_to_discord)
            _save_outbox(outbox)
            _append_delivery_log(
                {
                    "event": "outbox_dedup",
                    "entry_id": existing.get("id"),
                    "source": source,
                    "severity": severity,
                    "topic_key": effective_topic,
                    "preview": _preview_text(message),
                }
            )
            return str(existing.get("id") or "")
    entry = {
        "id": entry_id,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "severity": str(severity or "warning"),
        "source": str(source or "direct"),
        "topic_key": str(effective_topic or ""),
        "message": str(message or ""),
        "fingerprint": fingerprint,
        "mirror_to_discord": bool(mirror_to_discord),
        "delivery_policy": "auto_retry",
        "attempts": 0,
        "next_retry_at": now_ts,
        "last_error": str(last_error or "")[:600],
    }
    outbox.append(entry)
    _save_outbox(outbox)
    return entry_id


def _send_telegram_once(
    token: str,
    admin_ids: list[str],
    message: str,
    timeout_sec: int = 8,
    thread_id: Optional[int] = None,
) -> dict:
    acked = []
    errors = []
    chunks = _numbered_chunks(message, 3900)
    for chat_id in admin_ids:
        sent_all = True
        for chunk in chunks:
            payload_obj = {"chat_id": str(chat_id), "text": chunk}
            if thread_id and int(thread_id) > 0:
                payload_obj["message_thread_id"] = int(thread_id)
            payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")
            try:
                req = urlrequest.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlrequest.urlopen(req, timeout=max(4, int(timeout_sec))):
                    pass
            except HTTPError as e:
                body = ""
                try:
                    body = (e.read() or b"").decode("utf-8", "ignore")
                except Exception:
                    body = ""
                if bool(thread_id) and ("message thread not found" in body.lower() or "message_thread_id" in body.lower()):
                    errors.append(f"{chat_id}:invalid_thread:{thread_id}")
                else:
                    errors.append(f"{chat_id}:HTTP{getattr(e, 'code', 'ERR')}")
                sent_all = False
                break
            except URLError as e:
                errors.append(f"{chat_id}:URLError:{e.reason}")
                sent_all = False
                break
            except Exception as e:
                errors.append(f"{chat_id}:{type(e).__name__}")
                sent_all = False
                break
        if sent_all:
            acked.append(str(chat_id))
    return {
        "ok_any": bool(acked),
        "acked": acked,
        "total": len(admin_ids),
        "error": "; ".join(errors)[:800],
    }


def _flush_outbox(max_items: int = 8) -> dict:
    outbox = _load_outbox()
    if not outbox:
        _write_notification_delivery_health([], checked=0, recovered=0)
        return {"checked": 0, "recovered": 0, "remaining": 0}
    now_ts = time.time()
    recovered = 0
    checked = 0
    kept = []
    state_changed = False
    seen_fingerprints: set[str] = set()
    for entry in outbox:
        fingerprint = str(entry.get("fingerprint") or "")
        if not fingerprint:
            fingerprint = _outbox_fingerprint(
                str(entry.get("message") or ""),
                severity=str(entry.get("severity") or ""),
                topic_key=str(entry.get("topic_key") or ""),
                source=str(entry.get("source") or ""),
            )
            entry["fingerprint"] = fingerprint
        if fingerprint in seen_fingerprints:
            _append_delivery_log(
                {
                    "event": "outbox_drop",
                    "entry_id": entry.get("id"),
                    "reason": "duplicate_pending",
                    "preview": _preview_text(str(entry.get("message") or "")),
                }
            )
            continue
        seen_fingerprints.add(fingerprint)
        # Queues produced before the autonomous replay contract may contain
        # sensitive historical business notices. Preserve them for explicit
        # human disposition; never silently send or age-drop them.
        policy = _delivery_policy_for_entry(entry)
        if entry.get("delivery_policy") != policy:
            entry["delivery_policy"] = policy
            state_changed = True
        if policy != "auto_retry":
            kept.append(entry)
            continue
        age_sec = _outbox_entry_age_seconds(entry, now_ts)
        max_age_sec = _outbox_entry_max_age_seconds(entry)
        if age_sec > max_age_sec:
            _append_delivery_log(
                {
                    "event": "outbox_drop",
                    "entry_id": entry.get("id"),
                    "reason": "stale",
                    "age_seconds": round(age_sec, 1),
                    "max_age_seconds": round(max_age_sec, 1),
                    "preview": _preview_text(str(entry.get("message") or "")),
                }
            )
            continue
        if checked >= max_items:
            kept.append(entry)
            continue
        try:
            next_retry_at = float(entry.get("next_retry_at") or 0.0)
        except Exception:
            next_retry_at = 0.0
        if next_retry_at > now_ts:
            kept.append(entry)
            continue
        checked += 1
        result = send_telegram_push_with_status(
            str(entry.get("message") or ""),
            severity=str(entry.get("severity") or "warning"),
            source="outbox",
            topic_key=str(entry.get("topic_key") or ""),
            queue_on_fail=False,
            mirror_to_discord=bool(entry.get("mirror_to_discord", True)),
        )
        if result.get("telegram"):
            recovered += 1
            _append_delivery_log(
                {
                    "event": "outbox_recovered",
                    "entry_id": entry.get("id"),
                    "acked": int(result.get("acked") or 0),
                    "total": int(result.get("total") or 0),
                }
            )
            continue

        attempts = int(entry.get("attempts") or 0) + 1
        if attempts >= max(1, int(RED_PHONE_OUTBOX_MAX_RETRIES)):
            _append_delivery_log(
                {
                    "event": "outbox_drop",
                    "entry_id": entry.get("id"),
                    "attempts": attempts,
                    "error": str(result.get("error") or "")[:500],
                }
            )
            continue

        retry_delay = min(900.0, max(1.0, float(RED_PHONE_RETRY_BACKOFF_SEC)) * (2 ** min(attempts, 6)))
        entry["attempts"] = attempts
        entry["updated_at"] = datetime.now().isoformat()
        entry["last_error"] = str(result.get("error") or "")[:600]
        entry["next_retry_at"] = now_ts + retry_delay
        kept.append(entry)
    if state_changed or len(kept) != len(outbox) or checked > 0:
        _save_outbox(kept)
    _write_notification_delivery_health(kept, checked=checked, recovered=recovered)
    return {"checked": checked, "recovered": recovered, "remaining": len(kept)}


def flush_pending_alerts(max_items: int = 8) -> dict:
    """
    Public helper for schedulers/cron bridge to proactively retry queued alerts.
    """
    try:
        max_items = int(max_items)
    except Exception:
        max_items = 8
    max_items = max(1, min(max_items, 50))
    return _flush_outbox(max_items=max_items)


def _mirror_to_discord(
    message: str,
    *,
    topic_key: str = "",
    source: str = "",
    severity: str = "info",
) -> bool:
    """
    Best-effort: 將 TG 通知同步鏡像到 Discord 的對應子頻道。
    僅在 DC_MIRROR_ENABLED=1 且有 bot token 時啟用。
    失敗不影響主流程。
    """
    if not (os.environ.get("MAGI_DC_MIRROR_ENABLED", "1").strip().lower() in {"1", "true", "yes", "on"}):
        return False
    _src = str(source or "").strip().lower()
    # 系統/健康檢查只留在內部通知，不鏡像到業務 DC 頻道。
    if _src in {"business_module_live_check", "nightly_regression", "mock_test"}:
        return False
    # DC 對外開放，僅鏡像業務相關通知；系統內部（alert/check/nightly）不發 DC
    _DC_MIRROR_ALLOWED_TOPICS = {
        "filereview", "filereview_payment", "filereview_download", "filereview_apply",
        "laf", "laf_general", "laf_dispatch", "laf_go_live", "laf_closing", "laf_fee", "laf_inquiry", "laf_condition", "laf_progress",
        "transcript", "judgment",
        # "market" 已從 DC 鏡像中移除 (2026-04-20)：股票資訊不發 Discord
        "verbatim", "summary", "translation", "filing", "case_schedule",
    }
    event = classify_notification_event(message, source=source, severity=severity, topic_key=topic_key)
    _resolved_topic = event["topic_key"]
    if _resolved_topic and _resolved_topic not in _DC_MIRROR_ALLOWED_TOPICS:
        return False
    # 法扶夜巡完整報告包含下載、缺檔、案號補填等內部維運資訊；
    # DC 僅接收另行切出的進度回報提醒，避免業務頻道被一般巡檢洗版。
    if _src == "laf_nightly_audit" and _resolved_topic == "laf_general":
        return False

    # 🛑 靜默過濾：非「有新資訊」的定期報告不發 DC (TG 照發)
    if _is_noop_completion_notification(message):
        return False
    try:
        return _send_discord_bot_message(
            message, severity, topic_key=_resolved_topic, source=source
        )
    except Exception as e:
        logger.debug("[RED PHONE] DC mirror failed (non-fatal): %s", e)
        return False


def send_telegram_push_with_status(
    message: str,
    *,
    severity: str = "warning",
    source: str = "direct",
    topic_key: str = "",
    queue_on_fail: bool = True,
    mirror_to_discord: bool = True,
) -> dict:
    quiet_status = _quiet_suppression_status(
        message,
        severity=severity,
        source=source,
        topic_key=topic_key,
    )
    if quiet_status is not None:
        return quiet_status

    token, admin_ids = _get_telegram_config()
    resolved_topic, thread_id = _resolve_thread_id(message, source, severity, topic_key=topic_key)
    if not token or not admin_ids:
        err = "telegram token/admin ids missing"
        queued_id = ""
        if queue_on_fail:
            queued_id = _enqueue_outbox(
                message,
                severity=severity,
                source=source,
                last_error=err,
                topic_key=topic_key or resolved_topic,
                mirror_to_discord=mirror_to_discord,
            )
        return {
            "telegram": False,
            "delivered": False,
            "acked": 0,
            "total": len(admin_ids),
            "queued": bool(queued_id),
            "outbox_id": queued_id,
            "error": err,
            "topic_key": resolved_topic,
            "thread_id": int(thread_id) if thread_id else 0,
        }

    safe_message = _guard_text(message, platform="TELEGRAM")
    retries = max(0, min(int(RED_PHONE_RETRY_COUNT), 5))
    last_error = ""
    last_status = {"ok_any": False, "acked": [], "total": len(admin_ids), "error": ""}
    for attempt in range(retries + 1):
        last_status = _send_telegram_once(
            token,
            admin_ids,
            safe_message,
            timeout_sec=(8 + attempt * 2),
            thread_id=thread_id,
        )
        if last_status.get("ok_any"):
            _append_delivery_log(
                {
                    "event": "sent",
                    "source": source,
                    "severity": severity,
                    "preview": _preview_text(safe_message),
                    "topic_key": resolved_topic,
                    "thread_id": int(thread_id) if thread_id else 0,
                    "attempt": attempt + 1,
                    "acked": len(last_status.get("acked") or []),
                    "total": int(last_status.get("total") or 0),
                }
            )
            if mirror_to_discord:
                _mirror_to_discord(message, topic_key=topic_key or resolved_topic, source=source, severity=severity)
            return {
                "telegram": True,
                "delivered": True,
                "acked": len(last_status.get("acked") or []),
                "total": int(last_status.get("total") or 0),
                "queued": False,
                "outbox_id": "",
                "error": "",
                "topic_key": resolved_topic,
                "thread_id": int(thread_id) if thread_id else 0,
            }
        last_error = str(last_status.get("error") or "telegram_send_failed")
        if attempt < retries:
            time.sleep(max(0.2, float(RED_PHONE_RETRY_BACKOFF_SEC)) * (2 ** attempt))

    queued_id = ""
    if queue_on_fail:
        queued_id = _enqueue_outbox(
            safe_message,
            severity=severity,
            source=source,
            last_error=last_error,
            topic_key=topic_key or resolved_topic,
            mirror_to_discord=mirror_to_discord,
        )
    _append_delivery_log(
        {
            "event": "failed",
            "source": source,
            "severity": severity,
            "preview": _preview_text(safe_message),
            "topic_key": resolved_topic,
            "thread_id": int(thread_id) if thread_id else 0,
            "attempts": retries + 1,
            "queued": bool(queued_id),
            "outbox_id": queued_id,
            "error": last_error[:500],
        }
    )
    return {
        "telegram": False,
        "delivered": False,
        "acked": 0,
        "total": int(last_status.get("total") or len(admin_ids)),
        "queued": bool(queued_id),
        "outbox_id": queued_id,
        "error": last_error,
        "topic_key": resolved_topic,
        "thread_id": int(thread_id) if thread_id else 0,
    }


def send_telegram_push(message: str) -> bool:
    """
    Send push message to admin Telegram chat IDs.
    Includes content-based deduplication to prevent repeated alerts.
    """
    if not message or not message.strip():
        return False

    # --- Global Content Deduplication (24h) ---
    dedup_key = ""
    try:
        from skills.ops.dedup_db import is_done, mark_done
        # 取訊息內容的雜湊，併入當前日期，確保每天至少可發送一次相同的內容（或是跨日重啟時去重）
        # 如果使用者想要更嚴格，可以只用 msg_hash
        event = classify_notification_event(message, source="direct", severity="warning")
        date_str = _taipei_now().strftime("%Y%m%d")
        dedup_key = f"{date_str}:{event['dedup_key']}"
        
        if is_done("alert_content", dedup_key):
            logger.info("[RED PHONE] Deduplicated identical message (already sent today): %s", _preview_text(message))
            return False
        
        # 發送成功後才標記（在 send_telegram_push_with_status 內處理）
    except Exception as e:
        logger.debug("[RED PHONE] Dedup check failed: %s", e)

    status = send_telegram_push_with_status(message, severity="warning", source="direct", queue_on_fail=True)
    
    if status.get("telegram"):
        logger.info("[RED PHONE] Telegram alert sent successfully.")
        # 標記為已發送
        if dedup_key:
            try:
                mark_done("alert_content", dedup_key, metadata={"preview": _preview_text(message)})
            except Exception:
                pass
    else:
        logger.warning("[RED PHONE] Telegram send failed; queued=%s outbox_id=%s", status.get("queued"), status.get("outbox_id"))
    
    return bool(status.get("telegram"))


def alert_admin(
    message: str,
    severity: str = "warning",
    source: str = "alert_admin",
    topic_key: str = "",
) -> dict:
    """
    Send alert via all configured channels: Telegram, LINE, Discord.
    Includes content-based deduplication (24h) to prevent repeating the same alert.
    """
    if not message or not message.strip():
        return {}

    # --- Global Content Deduplication (24h) ---
    # We dedup based on the RAW message to avoid timestamp-driven variations.
    dedup_key = None
    event = classify_notification_event(message, source=source, severity=severity, topic_key=topic_key)
    try:
        from skills.ops.dedup_db import is_done, mark_done
        date_str = _taipei_now().strftime("%Y%m%d")
        dedup_key = f"{date_str}:{event['dedup_key']}"
        
        if is_done("alert_content", dedup_key):
            logger.info("[RED PHONE] alert_admin: Deduplicated identical message: %s", _preview_text(message))
            _append_delivery_log(
                {
                    "event": "deduplicated",
                    "source": source,
                    "severity": severity,
                    "topic_key": event["topic_key"],
                    "preview": _preview_text(message),
                }
            )
            return {
                "deduplicated": True,
                "telegram": False,
                "delivered": False,
                "line": False,
                "discord": False,
                "telegram_ack": 0,
                "telegram_total": 0,
                "outbox_queued": False,
                "outbox_id": "",
                "outbox_flushed": 0,
                "outbox_remaining": len(_load_outbox()),
                "topic_key": event["topic_key"],
                "thread_id": 0,
            }
    except Exception as e:
        logger.debug("[RED PHONE] alert_admin dedup check failed: %s", e)

    timestamp = _alert_timestamp()
    severity_emoji = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(severity, "⚠️")
    formatted_message = f"{severity_emoji} MAGI 警報\n{timestamp}\n\n{message}"

    try:
        flush_max = int(os.environ.get("MAGI_NOTIFY_OUTBOX_FLUSH_MAX", "8") or "8")
    except Exception:
        flush_max = 8
    flushed = _flush_outbox(max_items=max(1, min(flush_max, 30)))

    # --- Telegram ---
    status = send_telegram_push_with_status(
        formatted_message,
        severity=severity,
        source=source,
        topic_key=topic_key,
        queue_on_fail=True,
    )
    if status.get("telegram"):
        logger.info("[RED PHONE] Telegram alert sent successfully.")
        # 標記為已發送
        if dedup_key:
            try:
                mark_done("alert_content", dedup_key, metadata={"preview": _preview_text(message)})
            except Exception:
                pass
    else:
        logger.warning("[RED PHONE] Telegram send failed; queued=%s outbox_id=%s", status.get("queued"), status.get("outbox_id"))

    # --- LINE ---
    # LINE 免費額度有限（200 則/月），僅發送需要人介入處理的重要通知。
    # 一般對話和指令回覆由 webhook 處理，不經過 alert_admin。
    _LINE_IMPORTANT_TOPICS = {
        "filereview_payment",   # 有繳費單要處理
        "filereview_download",  # 閱卷下載完成
        "laf", "laf_dispatch", "laf_go_live", "laf_closing",  # 法扶業務
        "transcript",           # 筆錄下載完成
        "market",               # 股市快報
    }
    resolved_topic = event["topic_key"]
    line_ok = False
    should_line = resolved_topic in _LINE_IMPORTANT_TOPICS or severity == "critical"
    if not should_line:
        logger.debug("[RED PHONE] LINE skipped (topic=%s, severity=%s) — not important enough.", resolved_topic, severity)
    else:
        try:
            line_token = _get_line_channel_access_token()
            if line_token:
                line_targets = _get_line_admin_targets()
                for uid in line_targets:
                    line_ok = _send_line_push_real(formatted_message, uid) or line_ok
                if line_ok:
                    logger.info("[RED PHONE] LINE alert sent successfully.")
                elif line_targets:
                    logger.warning("[RED PHONE] LINE send failed for %d target(s).", len(line_targets))
            else:
                logger.debug("[RED PHONE] LINE token not configured, skipping.")
        except Exception as e:
            logger.warning("[RED PHONE] LINE alert error: %s", e)

    # --- Discord ---
    # Discord 僅用於互動指令與聊天，系統報告與定期通知不發送到 Discord。
    discord_ok = False

    pending_now = len(_load_outbox())
    results = {
        "line": line_ok,
        "discord": discord_ok,
        "telegram": bool(status.get("telegram")),
        "delivered": bool(status.get("telegram")) or bool(line_ok),
        "telegram_ack": int(status.get("acked") or 0),
        "telegram_total": int(status.get("total") or 0),
        "outbox_queued": bool(status.get("queued")),
        "outbox_id": str(status.get("outbox_id") or ""),
        "outbox_flushed": int(flushed.get("recovered") or 0),
        "outbox_remaining": int(pending_now),
        "topic_key": str(status.get("topic_key") or ""),
        "thread_id": int(status.get("thread_id") or 0),
    }
    logger.info(f"[RED PHONE] Alert results: {results}")
    return results


# =============================================================================
# Specific Alert Functions
# =============================================================================

def alert_iron_dome_violation(violation_type: str, matched_pattern: str, user_input: str):
    """Alert when Iron Dome blocks a potential attack."""
    message = f"""
**🛡️ 鐵穹防禦系統觸發 (Iron Dome Violation detected)**
**類型**: {violation_type}
**特徵**: `{matched_pattern}`
**內容預覽**: {user_input[:100]}...
"""
    return alert_admin(message, severity="warning", topic_key="alert")


def alert_system_error(error_type: str, details: str):
    """Alert on critical system errors."""
    message = f"""
**❌ 系統錯誤 (System Error)**
**類型**: {error_type}
**詳細資訊**: {details}
"""
    return alert_admin(message, severity="critical", topic_key="alert")


def alert_node_offline(node_name: str, ip: str):
    """Alert when a MAGI node goes offline."""
    message = f"""
**⚠️ 節點離線警告 (Node Offline)**
**節點名稱**: {node_name}
**IP 地址**: {ip}
**建議行動**: 請檢查網路連線或重啟該節點。
"""
    return alert_admin(message, severity="warning", topic_key="alert")


# =============================================================================
# File delivery via Telegram sendDocument / sendPhoto / sendAudio
# =============================================================================

_FILE_EXT_MAP = {
    # Images → sendPhoto
    ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".gif": "photo", ".webp": "photo",
    # Audio → sendAudio
    ".mp3": "audio", ".m4a": "audio", ".ogg": "audio", ".wav": "audio",
    # Video
    ".mp4": "video", ".mov": "video",
    # Everything else → sendDocument
}

_MAX_FILE_BYTES_TG = 50 * 1024 * 1024  # Telegram Bot API limit 50 MB


def send_file_admin(
    file_path: str,
    caption: str = "",
    reply_to_msg_id: Optional[int] = None,
    topic_key: str = "",
) -> dict:
    """
    Send a local file to all admin Telegram IDs using sendDocument / sendPhoto / sendAudio.

    Returns:
        {"ok": bool, "acked": [chat_id, ...], "errors": [...], "skipped_reason": str}
    """
    import mimetypes
    from email.mime.multipart import MIMEMultipart

    if not os.path.isfile(file_path):
        return {"ok": False, "skipped_reason": f"file_not_found: {file_path}", "acked": [], "errors": []}

    file_size = os.path.getsize(file_path)
    if file_size > _MAX_FILE_BYTES_TG:
        return {
            "ok": False,
            "skipped_reason": f"file_too_large: {file_size // 1024 // 1024}MB (max 50MB)",
            "acked": [],
            "errors": [],
        }

    token, admin_ids = _get_telegram_config()
    if not token or not admin_ids:
        return {"ok": False, "skipped_reason": "telegram_not_configured", "acked": [], "errors": []}

    # Resolve topic thread_id for correct TG topic routing
    thread_id: Optional[int] = None
    if topic_key:
        try:
            _key, thread_id = _resolve_thread_id(
                caption or os.path.basename(file_path),
                "red_phone_file",
                "info",
                topic_key=topic_key,
            )
        except Exception:
            thread_id = None

    ext = os.path.splitext(file_path)[1].lower()
    media_type = _FILE_EXT_MAP.get(ext, "document")
    endpoint = {
        "photo":    "sendPhoto",
        "audio":    "sendAudio",
        "video":    "sendVideo",
        "document": "sendDocument",
    }[media_type]
    field_name = media_type  # e.g. "document", "photo", "audio"

    mime_type, _ = mimetypes.guess_type(file_path)
    mime_type = mime_type or "application/octet-stream"
    filename = os.path.basename(file_path)
    caption_text = (caption or filename)[:1024]

    acked = []
    errors = []

    file_size = os.path.getsize(file_path)
    if file_size > 50 * 1024 * 1024:  # 50MB
        logger.warning("File too large to send: %s (%d MB)", file_path, file_size // (1024 * 1024))
        return {"ok": False, "error": f"File too large: {file_size // (1024*1024)} MB (limit 50 MB)"}

    with open(file_path, "rb") as fh:
        file_bytes = fh.read()

    for chat_id in admin_ids:
        try:
            # Build multipart/form-data manually (no external deps)
            boundary = f"MAGI{uuid.uuid4().hex}"
            body_parts: list[bytes] = []

            def _field(name: str, value: str) -> bytes:
                return (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")

            body_parts.append(_field("chat_id", str(chat_id)))
            body_parts.append(_field("caption", caption_text))
            if thread_id and int(thread_id) > 0:
                body_parts.append(_field("message_thread_id", str(thread_id)))
            if reply_to_msg_id:
                body_parts.append(_field("reply_to_message_id", str(reply_to_msg_id)))

            file_header = (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                f"Content-Type: {mime_type}\r\n\r\n"
            ).encode("utf-8")
            body_parts.append(file_header + file_bytes + b"\r\n")
            body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))

            body = b"".join(body_parts)
            req = urlrequest.Request(
                f"https://api.telegram.org/bot{token}/{endpoint}",
                data=body,
                headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                method="POST",
            )
            with urlrequest.urlopen(req, timeout=30):
                pass
            acked.append(str(chat_id))
            logger.info(f"[RED PHONE] File sent to {chat_id}: {filename}")
        except Exception as e:
            errors.append(f"{chat_id}: {e}")
            logger.error(f"[RED PHONE] File send failed to {chat_id}: {e}")

    return {"ok": bool(acked), "acked": acked, "errors": errors, "filename": filename}


# =============================================================================
# Module Test
# =============================================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger.info("RED PHONE TEST")
    result = alert_admin("This is a test alert from RED PHONE.", severity="info")
    logger.info(f"Result: {result}")

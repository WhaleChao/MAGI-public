"""
RED PHONE ALERT MODULE (紅色熱線)
=================================
Sends critical alerts to Admin via Telegram.

Security:
- NEVER hardcode tokens/IDs in source.
- Prefer env/config, with safe local fallbacks (last-seen binding files).
"""

import os
import json
import logging
import time
import uuid
from datetime import datetime
from urllib import request as urlrequest
from urllib.error import URLError, HTTPError
import sys

logger = logging.getLogger("RedPhone")

# =============================================================================
# Configuration
# =============================================================================
# LINE Messaging API
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from api.runtime_paths import get_config_path

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv()
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 37, exc_info=True)

_AGENT_DIR = os.path.join(_PROJECT_ROOT, ".agent")
LINE_LAST_SENDER_FILE = os.environ.get(
    "MAGI_LINE_LAST_SENDER_FILE",
    os.path.join(_AGENT_DIR, "line_last_sender.json"),
)

# Discord (optional) - webhook preferred; bot token fallback.
DISCORD_WEBHOOK_URL = (os.environ.get("MAGI_DISCORD_WEBHOOK") or os.environ.get("DISCORD_WEBHOOK_URL") or "").strip()
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
RED_PHONE_RETRY_COUNT = int(os.environ.get("MAGI_NOTIFY_RETRY_COUNT", "2") or "2")
RED_PHONE_RETRY_BACKOFF_SEC = float(os.environ.get("MAGI_NOTIFY_RETRY_BACKOFF_SEC", "1.0") or "1.0")
RED_PHONE_OUTBOX_MAX_RETRIES = int(os.environ.get("MAGI_NOTIFY_OUTBOX_MAX_RETRIES", "24") or "24")
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
    payload = {
        "to": user_id,
        "messages": [{"type": "text", "text": safe_message[:5000]}],
    }
    try:
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
            return getattr(resp, "status", 0) == 200
    except Exception as e:
        logger.error(f"[RED PHONE] LINE push error: {e}")
        return False


def send_line_push(message: str, user_id: str | None = None) -> bool:
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
        "timestamp": datetime.now(datetime.timezone.utc).isoformat(),
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
            return True  # 靜默：不發 DC
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
            return True  # 靜默：不發 DC
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


def send_discord_alert(message: str, webhook_url: str | None = None, severity: str = "warning") -> bool:
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
    token = (os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN") or "").strip()
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
        # 閱卷（通用 fallback → 下載頻道）
        "filereview": "filereview_download",
        "file_review": "filereview_download",
        "file-review": "filereview_download",
        "docket": "filereview_download",
        "閱卷": "filereview_download",
        "卷宗": "filereview_download",
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
    }
    return aliases.get(k, k)


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
                             "closing_report", "laf_closing", "laf_dispatch"]):
        return "laf"
    if any(k in s for k in ["判決", "judgment", "司法院", "裁判"]):
        return "judgment"
    if any(k in s for k in ["逐字稿", "verbatim", "音訊轉文字"]):
        return "verbatim"
    if any(k in s for k in ["翻譯", "translation", "translate", "tri_sage", "tri-sage"]):
        return "translation"
    if any(k in s for k in ["摘要", "summary", "summarize", "重點整理"]):
        return "summary"
    if any(k in s for k in ["歸檔", "filing", "pdf_namer", "casper 歸檔"]):
        return "filing"
    if any(k in s for k in ["繳費", "payment"]):
        return "filereview_payment"
    if any(k in s for k in ["閱卷", "電子卷", "file_review", "file-review", "docket", "可下載"]):
        return "filereview_download"
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


def _resolve_thread_id(message: str, source: str, severity: str, topic_key: str = "") -> tuple[str, int | None]:
    tmap = _load_topic_map()
    if not tmap:
        return "", None
    key = _canonical_topic_key(topic_key) if topic_key else _infer_topic_key(message, source, severity)
    if key in tmap:
        return key, int(tmap[key])
    # Fallback: filereview_payment → filereview, laf_dispatch → laf, judicial_api → judgment, etc.
    _TG_TOPIC_FALLBACK = {
        "filereview_payment": "filereview",
        "filereview_download": "filereview",
        "filereview_apply": "filereview",
        "laf_dispatch": "laf",
        "laf_closing": "laf",
        "judicial_api": "judgment",
    }
    fb = _TG_TOPIC_FALLBACK.get(key, "")
    if fb and fb in tmap:
        return key, int(tmap[fb])
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


def _save_outbox(items: list[dict]) -> None:
    try:
        os.makedirs(_AGENT_DIR, exist_ok=True)
        tmp = RED_PHONE_OUTBOX_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, RED_PHONE_OUTBOX_FILE)
    except Exception as e:
        logger.warning("[RED PHONE] failed to save outbox: %s", e)


def _enqueue_outbox(message: str, severity: str, source: str, last_error: str = "") -> str:
    entry_id = f"rp_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    now_ts = time.time()
    entry = {
        "id": entry_id,
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat(),
        "severity": str(severity or "warning"),
        "source": str(source or "direct"),
        "message": str(message or ""),
        "attempts": 0,
        "next_retry_at": now_ts,
        "last_error": str(last_error or "")[:600],
    }
    outbox = _load_outbox()
    outbox.append(entry)
    _save_outbox(outbox)
    return entry_id


def _send_telegram_once(
    token: str,
    admin_ids: list[str],
    message: str,
    timeout_sec: int = 8,
    thread_id: int | None = None,
) -> dict:
    acked = []
    errors = []
    for chat_id in admin_ids:
        payload_obj = {"chat_id": str(chat_id), "text": message}
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
            acked.append(str(chat_id))
        except HTTPError as e:
            body = ""
            try:
                body = (e.read() or b"").decode("utf-8", "ignore")
            except Exception:
                body = ""
            can_retry_without_thread = (
                bool(thread_id)
                and int(getattr(e, "code", 0) or 0) in {400, 403}
                and ("message thread not found" in body.lower() or "message_thread_id" in body.lower())
            )
            if can_retry_without_thread:
                try:
                    retry_payload = json.dumps({"chat_id": str(chat_id), "text": message}, ensure_ascii=False).encode("utf-8")
                    req2 = urlrequest.Request(
                        f"https://api.telegram.org/bot{token}/sendMessage",
                        data=retry_payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urlrequest.urlopen(req2, timeout=max(4, int(timeout_sec))):
                        pass
                    acked.append(str(chat_id))
                    continue
                except Exception as retry_e:
                    errors.append(f"{chat_id}:thread_fallback_failed:{type(retry_e).__name__}")
                    continue
            errors.append(f"{chat_id}:HTTP{getattr(e, 'code', 'ERR')}")
        except URLError as e:
            errors.append(f"{chat_id}:URLError:{e.reason}")
        except Exception as e:
            errors.append(f"{chat_id}:{type(e).__name__}")
    return {
        "ok_any": bool(acked),
        "acked": acked,
        "total": len(admin_ids),
        "error": "; ".join(errors)[:800],
    }


def _flush_outbox(max_items: int = 8) -> dict:
    outbox = _load_outbox()
    if not outbox:
        return {"checked": 0, "recovered": 0, "remaining": 0}
    now_ts = time.time()
    recovered = 0
    checked = 0
    kept = []
    for entry in outbox:
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
            queue_on_fail=False,
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
    if len(kept) != len(outbox) or checked > 0:
        _save_outbox(kept)
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
    # DC 對外開放，僅鏡像業務相關通知；系統內部（alert/check/nightly）不發 DC
    _DC_MIRROR_ALLOWED_TOPICS = {
        "filereview", "filereview_payment", "filereview_download", "filereview_apply",
        "laf", "laf_dispatch", "laf_go_live", "laf_closing",
        "transcript", "judgment", "market",
        "verbatim", "summary", "translation", "filing",
    }
    _resolved_topic = _canonical_topic_key(topic_key)
    if _resolved_topic and _resolved_topic not in _DC_MIRROR_ALLOWED_TOPICS:
        return False
    try:
        return _send_discord_bot_message(
            message, severity, topic_key=topic_key, source=source
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
) -> dict:
    token, admin_ids = _get_telegram_config()
    resolved_topic, thread_id = _resolve_thread_id(message, source, severity, topic_key=topic_key)
    if not token or not admin_ids:
        err = "telegram token/admin ids missing"
        queued_id = ""
        if queue_on_fail:
            queued_id = _enqueue_outbox(message, severity=severity, source=source, last_error=err)
        return {
            "telegram": False,
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
            # Best-effort mirror to Discord (routed channel)
            _mirror_to_discord(message, topic_key=topic_key or resolved_topic, source=source, severity=severity)
            return {
                "telegram": True,
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
        queued_id = _enqueue_outbox(safe_message, severity=severity, source=source, last_error=last_error)
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

    Returns:
        True if sent to at least one admin successfully.
    """
    status = send_telegram_push_with_status(message, severity="warning", source="direct", queue_on_fail=True)
    if status.get("telegram"):
        logger.info("[RED PHONE] Telegram alert sent successfully.")
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

    Args:
        message: Alert message
        severity: "info", "warning", or "critical"

    Returns:
        Dict with results: {"line": bool, "discord": bool, "telegram": bool, ...}
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
    resolved_topic = _canonical_topic_key(topic_key) if topic_key else _infer_topic_key(message, source, severity)
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
    reply_to_msg_id: int | None = None,
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
    thread_id: int | None = None
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

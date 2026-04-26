# -*- coding: utf-8 -*-
"""
Telegram-first Notifier Module for MAGI / LAF Automation
========================================================
Sends push messages to admin via Telegram Bot API.

Usage:
    from line_notifier import LAFNotifier
    notifier = LAFNotifier()
    notifier.notify_admin("📋 報結確認 — 蕭仁棨 ...")
"""

import os
import json
import logging
import requests
import time
import sys
from pathlib import Path
from typing import Optional, List

logger = logging.getLogger(__name__)

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import get_config_path, get_magi_root_dir

# ==============================================================================
# Constants
# ==============================================================================

LINE_PUSH_API = "https://api.line.me/v2/bot/message/push"
ENV_PATH = get_magi_root_dir() / ".env"
CONFIG_PATH = get_config_path("config.json")
ALLOWLIST_PATH = get_magi_root_dir() / ".agent" / "admin_allowlist.json"
LINE_429_STATE_PATH = get_magi_root_dir() / ".agent" / "line_429_status.json"
TG_RETRY_COUNT = int(os.environ.get("MAGI_TG_RETRY_COUNT", "2") or "2")
TG_RETRY_BACKOFF_SEC = float(os.environ.get("MAGI_TG_RETRY_BACKOFF_SEC", "1.0") or "1.0")
try:
    from api.tw_output_guard import normalize_output_text as _normalize_output_text
except Exception:
    _normalize_output_text = None


def _guard_text(text: str, platform: str = "LINE") -> str:
    s = (text or "").strip()
    if not s:
        return s
    try:
        if _normalize_output_text:
            return _normalize_output_text(s, platform=platform)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 54, exc_info=True)
    return s


def _load_env(env_path: Path = ENV_PATH) -> dict:
    """Parse .env file into a dict (no third-party deps)."""
    env = {}
    if not env_path.exists():
        return env
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    return env


def _load_config(config_path: Path = CONFIG_PATH) -> dict:
    """Load config.json."""
    if not config_path.exists():
        return {}
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def _split_csv(raw: str) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x and x.strip()]


def _load_admin_line_ids_from_allowlist(path: Path = ALLOWLIST_PATH) -> list[str]:
    try:
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8")) or {}
        arr = data.get("line_admin_user_ids") or []
        if not isinstance(arr, list):
            return []
        out = [str(x).strip() for x in arr if str(x).strip()]
        return out
    except Exception:
        return []


def _is_line_429_active_today() -> bool:
    """If today's LINE quota is already exhausted, skip repeated LINE API attempts."""
    try:
        if not LINE_429_STATE_PATH.exists():
            return False
        obj = json.loads(LINE_429_STATE_PATH.read_text(encoding="utf-8")) or {}
        last = str(obj.get("last_429_announcement_date") or "").strip()
        if not last:
            return False
        import datetime

        return last == datetime.date.today().isoformat()
    except Exception:
        return False


# ==============================================================================
# LAFNotifier
# ==============================================================================

class LAFNotifier:
    """
    Notification bridge: Telegram Bot API (TG-only).

    Reads credentials from:
    - Telegram: MAGI/.env
    - Fallback config: MAGI canonical config.json
    """

    def __init__(self, env_path: str = None, config_path: str = None):
        self._env = _load_env(Path(env_path) if env_path else ENV_PATH)
        self._config = _load_config(Path(config_path) if config_path else CONFIG_PATH)

        # LINE
        self.line_token = self._env.get("MAGI_LINE_CHANNEL_ACCESS_TOKEN", "")
        env_admin_ids = _split_csv(self._env.get("MAGI_ADMIN_LINE_IDS", ""))
        allowlist_ids = _load_admin_line_ids_from_allowlist(ALLOWLIST_PATH)
        merged = []
        seen = set()
        for uid in (env_admin_ids + allowlist_ids):
            if uid and uid not in seen:
                seen.add(uid)
                merged.append(uid)
        self.admin_line_ids = merged
        # Backward compatibility for old call sites.
        self.admin_line_id = self.admin_line_ids[0] if self.admin_line_ids else ""

        # Telegram (primary + only channel)
        self.telegram_token = self._env.get("OPENCLAW_TELEGRAM_BOT_TOKEN", "").strip()
        self.telegram_notify_ids = _split_csv(self._env.get("MAGI_NOTIFY_TELEGRAM_IDS", ""))
        if not self.telegram_token:
            tg_cfg = (self._config.get("channels", {}) or {}).get("telegram", {})
            self.telegram_token = str(tg_cfg.get("bot_token") or tg_cfg.get("botToken") or "").strip()
        if not self.telegram_notify_ids:
            tg_cfg = (self._config.get("channels", {}) or {}).get("telegram", {})
            notify_to = tg_cfg.get("notify_to") or tg_cfg.get("notifyTo") or []
            if isinstance(notify_to, list):
                self.telegram_notify_ids = [str(x).strip() for x in notify_to if str(x).strip()]
        # Backward compatibility for old call sites/attribute names.
        self.telegram_admin_ids = self.telegram_notify_ids

        # Discord — 2026-04-26 起 notify_admin 同時 push DC + TG（不再 TG-only）
        # 優先 LAF 專用 webhook，fallback 一般 webhook
        self.discord_webhook = (
            self._env.get("MAGI_DISCORD_WEBHOOK_LEGALBRIDGE_LAF", "")
            or self._env.get("MAGI_DISCORD_WEBHOOK_LEGALBRIDGE", "")
            or self._env.get("MAGI_DISCORD_WEBHOOK_URL", "")
            or self._config.get("discord_webhook_url", "")
        )

        if self.telegram_token and self.telegram_notify_ids:
            logger.info(
                "LAFNotifier: Telegram ready (targets=%d, first=%s...)",
                len(self.telegram_notify_ids),
                self.telegram_notify_ids[0][:10],
            )
        else:
            logger.warning("LAFNotifier: Telegram credentials missing, notify_admin will log locally")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def notify_admin(
        self,
        text: str,
        *,
        topic_key: str = "",
        source: str = "laf_notifier",
    ) -> bool:
        """
        Send message to admin (Telegram + Discord，2026-04-26 起雙通道).

        Returns:
            True if at least one channel succeeded.
        """
        safe_text = _guard_text(text, platform="TELEGRAM")
        tg_ok = self._push_telegram(safe_text, topic_key=topic_key, source=source)
        # DC 為次要通道：失敗不影響主回傳，但會 log；DC 文案不需 TG-specific guard
        dc_ok = False
        try:
            dc_ok = self._push_discord(text, topic_key=topic_key)
        except Exception as _dce:
            logger.error("Discord push exception (non-fatal): %s", _dce)
        if tg_ok or dc_ok:
            if not tg_ok:
                logger.warning("notify_admin: TG failed but DC sent (topic=%s)", topic_key)
            if not dc_ok:
                logger.warning("notify_admin: TG sent but DC failed (topic=%s)", topic_key)
            return True
        logger.error("notify_admin: BOTH TG and DC failed for topic=%s", topic_key)
        if safe_text:
            self._log_local(safe_text)
        return False

    def notify_admin_with_files(
        self,
        text: str,
        file_paths: List[str],
        *,
        topic_key: str = "",
        source: str = "laf_notifier",
    ) -> bool:
        """
        Send TG message and attach documents (PDF, etc.) to admin chats.

        Strategy:
        - send text first
        - send each existing file via sendDocument
        """
        safe_text = _guard_text(text, platform="TELEGRAM")

        files = [str(p).strip() for p in (file_paths or []) if str(p).strip() and os.path.exists(str(p).strip())]

        if files:
            # Send text as caption on first file — avoids duplicate message
            ok_file_any = False
            for i, fp in enumerate(files):
                caption = safe_text[:1024] if i == 0 and safe_text else ""
                if self._push_telegram_document(fp, caption=caption):
                    ok_file_any = True
            # If text was too long for caption (>1024), send remainder separately
            if safe_text and len(safe_text) > 1024:
                self._push_telegram(safe_text, topic_key=topic_key, source=source)
            if ok_file_any:
                return True
        else:
            # No files — send text only
            ok_text = self._push_telegram(safe_text, topic_key=topic_key, source=source) if safe_text else False
            if ok_text:
                return True

        if safe_text:
            logger.error("Telegram message/file failed. Logging locally.")
            self._log_local(safe_text)
        return False

    def send_closing_confirmation(self, case_name: str, case_number: str,
                                   counts: dict, warnings: list) -> bool:
        """
        Send a formatted closing report confirmation to admin.

        Args:
            case_name: Client name (e.g., 蕭仁棨)
            case_number: Case number (e.g., 1150206-A-042)
            counts: Dict with keys like meeting_count, contact_count, etc.
            warnings: List of warning strings for counts < threshold

        Returns:
            True if message sent successfully.
        """
        lines = [
            f"📋 法扶報結確認 — {case_name} ({case_number})",
            "",
            "📊 CASPER 統計結果：",
        ]

        # Portal 欄位對應：
        # 面談(meet_times) + 電話(tel_times) + 律見(inq_times) = 討論次數(disc_times)
        _meet = int(counts.get("meeting_count", 0) or 0)
        _tel = int(counts.get("contact_count", 0) or 0)
        _inq = int(counts.get("inq_count", 0) or 0)
        _disc = _meet + _tel + _inq
        _court = int(counts.get("court_count", 0) or 0)
        _review = int(counts.get("review_count", 0) or 0)
        _wc = int(counts.get("document_count", 0) or 0)

        _disc_warn = "  ⚠️" if _disc < 1 else ""
        lines.append(f"  討論次數: {_disc}{_disc_warn}（面談{_meet} + 電話{_tel} + 律見{_inq}）")
        for label, value in [
            ("開庭次數", _court),
            ("閱卷次數", _review),
            ("書狀次數", _wc),
        ]:
            flag = "  ⚠️" if value < 1 else ""
            lines.append(f"  {label}: {value}{flag}")

        if warnings:
            lines.append("")
            for w in warnings:
                lines.append(f"⚠️ {w}")

        lines.extend([
            "",
            "📌 請回覆：",
            "  「OK」或「請報結」→ 以上數字送出",
            "  修正數字 → 例如「聯繫 2」",
        ])

        return self.notify_admin("\n".join(lines))

    # ------------------------------------------------------------------
    # Private — LINE
    # ------------------------------------------------------------------

    def _push_line(self, text: str) -> bool:
        """Push text message via LINE Messaging API."""
        try:
            if _is_line_429_active_today():
                logger.warning("LINE push skipped: quota already exhausted today (cached 429 state).")
                return False

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.line_token}",
            }
            any_ok = False
            hit_429 = False
            
            for uid in self.admin_line_ids:
                body = {
                    "to": uid,
                    "messages": [{"type": "text", "text": text}],
                }
                resp = requests.post(LINE_PUSH_API, headers=headers, json=body, timeout=10)
                if resp.status_code == 200:
                    any_ok = True
                elif resp.status_code == 429:
                    hit_429 = True
                    logger.warning("LINE push HTTP 429 (Quota Exceeded to=%s...): %s", uid[:10], resp.text)
                else:
                    logger.warning("LINE push HTTP %d (to=%s...): %s", resp.status_code, uid[:10], resp.text)
            
            if hit_429:
                self._handle_429_fallback_announcement()
                
            if any_ok:
                # Quota recovered (e.g., new month) -> clear stale 429 state.
                try:
                    if LINE_429_STATE_PATH.exists():
                        LINE_429_STATE_PATH.unlink()
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 322, exc_info=True)
                logger.info("✅ LINE push sent successfully")
                return True
            return False

        except Exception as e:
            logger.error("LINE push exception: %s", e)
            return False

    def _handle_429_fallback_announcement(self) -> None:
        import datetime
        today_str = datetime.date.today().isoformat()
        
        try:
            if LINE_429_STATE_PATH.exists():
                state = json.loads(LINE_429_STATE_PATH.read_text(encoding="utf-8"))
                if state.get("last_429_announcement_date") == today_str:
                    return # Already announced today
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 341, exc_info=True)
            
        announcement = "🚨 [系統公告] LINE Push API 本月額度已達上限 (HTTP 429)。\n系統通知已統一改由 Telegram 發送。"
        if self.telegram_token and self.telegram_admin_ids:
            logger.info("Sending HTTP 429 fallback announcement to Telegram.")
            self._push_telegram(announcement)
            
        try:
            LINE_429_STATE_PATH.write_text(json.dumps({
                "last_429_announcement_date": today_str,
                "timestamp": datetime.datetime.now().isoformat()
            }), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to write line_429_status.json: {e}")

    # ------------------------------------------------------------------
    # Private — Discord
    # ------------------------------------------------------------------

    def _push_discord_via_bot(self, text: str, channel_id: str) -> bool:
        """Send via Discord bot REST API（topic_key → channel_id 路由）。"""
        bot_token = (self._env.get("DISCORD_BOT_TOKEN", "") or "").strip()
        if not bot_token or not channel_id:
            return False
        try:
            url = f"https://discord.com/api/v10/channels/{channel_id}/messages"
            headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
            payload = {"content": str(text or "")[:2000]}
            resp = requests.post(url, json=payload, headers=headers, timeout=10)
            if resp.status_code in (200, 201):
                logger.info("✅ Discord bot message sent to channel %s", channel_id)
                return True
            logger.warning("Discord bot HTTP %d to channel %s: %s", resp.status_code, channel_id, resp.text[:200])
            return False
        except Exception as e:
            logger.error("Discord bot push exception: %s", e)
            return False

    def _resolve_dc_channel_id(self, topic_key: str) -> str:
        """topic_key → DC channel id（從 env：MAGI_DC_CHANNEL_<UPPER_TOPIC>）。"""
        if not topic_key:
            return ""
        env_key = f"MAGI_DC_CHANNEL_{topic_key.upper()}"
        return (self._env.get(env_key, "") or "").strip()

    def _push_discord(self, text: str, *, topic_key: str = "") -> bool:
        """Send message to Discord：優先 bot+channel_id（topic 路由），fallback webhook。"""
        # 1) 嘗試 bot + topic-routed channel_id（精準路由）
        if topic_key:
            channel_id = self._resolve_dc_channel_id(topic_key)
            if channel_id and self._push_discord_via_bot(text, channel_id):
                return True
        # 2) Fallback 既有 webhook（一般 LAF channel）
        if not self.discord_webhook:
            return False

        try:
            # Discord max message is 2000 chars
            payload = {"content": text[:2000]}
            resp = requests.post(self.discord_webhook, json=payload, timeout=10)

            if resp.status_code in (200, 204):
                logger.info("✅ Discord message sent successfully")
                return True
            else:
                logger.warning("Discord webhook HTTP %d: %s", resp.status_code, resp.text)
                return False

        except Exception as e:
            logger.error("Discord webhook exception: %s", e)
            return False

    # ------------------------------------------------------------------
    # Private — Telegram
    # ------------------------------------------------------------------

    def _push_telegram(self, text: str, *, topic_key: str = "", source: str = "laf_notifier") -> bool:
        """Send message via Telegram Bot API."""
        try:
            from skills.ops.red_phone import send_telegram_push_with_status  # type: ignore

            status = send_telegram_push_with_status(
                str(text or ""),
                severity="info",
                source=source,
                topic_key=topic_key,
                queue_on_fail=True,
            ) or {}
            if bool(status.get("telegram")) or bool(status.get("queued")):
                return True
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 400, exc_info=True)

        if not self.telegram_token or not self.telegram_notify_ids:
            return False

        any_ok = False
        retries = max(0, min(int(TG_RETRY_COUNT), 5))
        for attempt in range(retries + 1):
            any_ok = False
            for chat_id in self.telegram_notify_ids:
                try:
                    url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
                    payload = {"chat_id": chat_id, "text": text}
                    resp = requests.post(url, json=payload, timeout=10 + attempt * 2)
                    if resp.status_code == 200:
                        any_ok = True
                    else:
                        logger.warning(
                            "Telegram HTTP %d for chat %s (attempt %d/%d): %s",
                            resp.status_code,
                            chat_id,
                            attempt + 1,
                            retries + 1,
                            resp.text,
                        )
                except Exception as e:
                    logger.error(
                        "Telegram push exception for chat %s (attempt %d/%d): %s",
                        chat_id,
                        attempt + 1,
                        retries + 1,
                        e,
                    )
            if any_ok:
                break
            if attempt < retries:
                time.sleep(max(0.2, float(TG_RETRY_BACKOFF_SEC)) * (2 ** attempt))

        if any_ok:
            logger.info("✅ Telegram message sent successfully")
            return True

        # Last fallback: queue into centralized red_phone outbox for later retry.
        try:
            from skills.ops.red_phone import send_telegram_push_with_status  # type: ignore

            status = send_telegram_push_with_status(
                text,
                severity="info",
                source=source,
                topic_key=topic_key,
                queue_on_fail=True,
            ) or {}
            queued = bool(status.get("queued"))
            if queued:
                logger.warning(
                    "Telegram immediate send failed; queued to outbox (id=%s)",
                    status.get("outbox_id"),
                )
            return bool(status.get("telegram")) or queued
        except Exception as e:
            logger.warning("Telegram outbox fallback unavailable: %s", e)
            return False

    def _push_telegram_document(self, file_path: str, caption: str = "") -> bool:
        """Send document via Telegram Bot API sendDocument."""
        if not self.telegram_token or not self.telegram_notify_ids:
            return False
        if not os.path.exists(file_path):
            return False

        any_ok = False
        for chat_id in self.telegram_notify_ids:
            retries = max(0, min(int(TG_RETRY_COUNT), 3))
            sent = False
            for attempt in range(retries + 1):
                try:
                    url = f"https://api.telegram.org/bot{self.telegram_token}/sendDocument"
                    data = {"chat_id": chat_id}
                    if caption:
                        data["caption"] = caption[:1024]
                    with open(file_path, "rb") as f:
                        files = {"document": (os.path.basename(file_path), f)}
                        resp = requests.post(url, data=data, files=files, timeout=60 + attempt * 10)
                    if resp.status_code == 200:
                        any_ok = True
                        sent = True
                        break
                    logger.warning(
                        "Telegram sendDocument HTTP %d for chat %s (attempt %d/%d): %s",
                        resp.status_code,
                        chat_id,
                        attempt + 1,
                        retries + 1,
                        resp.text,
                    )
                except Exception as e:
                    logger.error(
                        "Telegram sendDocument exception for chat %s (attempt %d/%d): %s",
                        chat_id,
                        attempt + 1,
                        retries + 1,
                        e,
                    )
                if attempt < retries:
                    time.sleep(max(0.2, float(TG_RETRY_BACKOFF_SEC)) * (2 ** attempt))
            if not sent:
                logger.warning("Telegram sendDocument failed after retries for chat %s", chat_id)

        if any_ok:
            logger.info("✅ Telegram document sent successfully: %s", os.path.basename(file_path))
        return any_ok

    # ------------------------------------------------------------------
    # Private — Local log fallback
    # ------------------------------------------------------------------

    def _log_local(self, text: str):
        """Fallback: log to local file."""
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"[{timestamp}] [NOTIFICATION-FALLBACK] {text}\n"
        print(log_line)

        log_path = Path(__file__).parent / "laf_notifications.log"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(log_line)


# ==============================================================================
# CLI / Self-Test
# ==============================================================================

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    notifier = LAFNotifier()

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        # Send test notification
        print("Sending test notification...")
        ok = notifier.notify_admin("🧪 CASPER 通知測試\n如果您看到這則訊息，表示通知系統正常運作。")
        print(f"Result: {'✅ 成功' if ok else '❌ 失敗'}")
    elif len(sys.argv) > 1 and sys.argv[1] == "test-closing":
        # Test closing confirmation format
        ok = notifier.send_closing_confirmation(
            case_name="蕭仁棨",
            case_number="1150206-A-042",
            counts={
                "meeting_count": 3,
                "contact_count": 0,
                "court_count": 2,
                "document_count": 4,
            },
            warnings=["聯繫次數為 0，日曆上可能未登記"]
        )
        print(f"Result: {'✅ 成功' if ok else '❌ 失敗'}")
    else:
        print("Usage:")
        print("  python line_notifier.py test          # 測試通知")
        print("  python line_notifier.py test-closing   # 測試報結確認格式")

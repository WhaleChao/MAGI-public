"""
MAGI Discord Bot
================
Receives Discord messages and routes them to the MAGI orchestrator just like LINE.
"""

import discord
import aiohttp
import asyncio
import hashlib
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
import sys
import signal
import logging
import json
import tempfile
import uuid
import time
import pathlib
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor

# Auto-reap zombie children (subprocess, asyncio create_subprocess, etc.)
def _sigchld_handler(_signum, _frame):
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
        except ChildProcessError:
            break
signal.signal(signal.SIGCHLD, _sigchld_handler)


def _sigterm_handler(_signum, _frame):
    """Graceful SIGTERM: log and exit cleanly so daemon knows it was intentional."""
    logging.getLogger("discord_bot").info("SIGTERM received — shutting down Discord bot")
    sys.exit(0)


signal.signal(signal.SIGTERM, _sigterm_handler)

# Load .env (shared with server.py)
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 41, exc_info=True)

# Scheduler policy:
# MAGI daemon / cron_jobs.json is the canonical scheduler source.  Keep the
# Discord bot internal scheduler off unless explicitly enabled.
INTERNAL_CRON_ENABLED = (
    os.environ.get("MAGI_INTERNAL_CRON_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
)

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("MAGI_MYSQL_USE_PURE", "1")
os.environ.setdefault("MYSQL_USE_PURE", "1")
try:
    from api.mysql_connector_guard import install_mysql_cext_blocker, patch_mysql_connector_for_stability

    install_mysql_cext_blocker()
    patch_mysql_connector_for_stability()
except Exception:
    logging.getLogger("discord_bot").debug("mysql connector early guard failed", exc_info=True)
from api.runtime_paths import ensure_path_on_sys_path, get_config_path, get_orch_dir

from api.orchestrator import Orchestrator
from api.admin_allowlist import get_discord_admin_ids
try:
    from api.tw_output_guard import normalize_output_text as _normalize_output_text
except Exception:
    _normalize_output_text = None

# Configuration
CONFIG_PATHS = [
    str(get_config_path("config.json")),
    os.path.abspath(os.path.join(os.path.dirname(__file__), '../config.json')),
]

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
# 支援多頻道：逗號分隔多個 channel ID（例如 "123,456,789"）
_RAW_CHANNEL_IDS = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
DISCORD_CHANNEL_IDS: set[str] = {
    cid.strip()
    for cid in _RAW_CHANNEL_IDS.split(",")
    if cid.strip()
}
# 通知專用頻道：優先用 DISCORD_NOTIFY_CHANNEL_ID，避免通知打擾聊天頻道
DISCORD_NOTIFY_CHANNEL_ID = os.environ.get("DISCORD_NOTIFY_CHANNEL_ID", "").strip()
# 向下相容：單一頻道 ID（供聊天回覆用）
DISCORD_CHANNEL_ID = next(iter(DISCORD_CHANNEL_IDS), "")
DISCORD_ADMIN_IDS = set()
DISCORD_ADMIN_IDS |= {
    uid.strip()
    for uid in os.environ.get("DISCORD_ADMIN_IDS", "").split(",")
    if uid.strip()
}
try:
    # Also load from .agent/admin_allowlist.json for restart-safe allowlisting.
    DISCORD_ADMIN_IDS |= set(get_discord_admin_ids() or set())
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 90, exc_info=True)
MAX_ATTACHMENT_BYTES = int(os.environ.get("DISCORD_MAX_ATTACHMENT_MB", "20")) * 1024 * 1024

logger = logging.getLogger("DiscordBot")

if not DISCORD_ADMIN_IDS:
    logger.warning("DISCORD_ADMIN_IDS is empty. No Discord users will be treated as admin (strict allowlist).")

if not DISCORD_BOT_TOKEN:
    logger.critical("No Discord Bot Token found in Config!")
    sys.exit(1)
from api.thread_pools import channel_pool as _DISCORD_BG_EXECUTOR
from api.thread_pools import cron_pool as _CRON_EXECUTOR


def _normalize_discord_output_text(text: str) -> str:
    s = (text or "").strip()
    if not s:
        return s
    try:
        if _normalize_output_text:
            return _normalize_output_text(s, platform="DISCORD")
    except Exception as e:
        logger.warning(f"⚠️ Taiwan wording guard skipped (Discord): {e}")
    return s


def _split_discord_chunks(text: str, limit: int = 1900) -> list[str]:
    """Split *text* into Discord-safe chunks (≤ *limit* chars each).

    Strategy: split on newline boundaries so no stock line / data row is ever
    cut in half.  If a single line exceeds *limit*, fall back to hard-cut on
    that line only.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    cur = ""
    for line in text.split("\n"):
        # Single line longer than limit → hard-cut it
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = (cur + "\n" + line) if cur else line
        if len(candidate) > limit:
            if cur:
                chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks or [text[:limit]]


# Persist last channel id so CASPER can proactively notify via bot-token when DISCORD_CHANNEL_ID is unset.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT_DIR = os.path.join(_PROJECT_ROOT, ".agent")
os.makedirs(_AGENT_DIR, exist_ok=True)
_CHANNEL_DELIVERY_AUDIT_FILE = os.path.join(_AGENT_DIR, "channel_delivery_audit.jsonl")
_channel_audit_lock = threading.Lock()
DISCORD_LAST_CHANNEL_FILE = os.environ.get(
    "MAGI_DISCORD_LAST_CHANNEL_FILE",
    os.path.join(_AGENT_DIR, "discord_last_channel.json"),
)


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
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 153, exc_info=True)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 155, exc_info=True)


def _save_last_channel_id(channel_id: str):
    try:
        cid = str(channel_id or "").strip()
        if not cid:
            return
        with open(DISCORD_LAST_CHANNEL_FILE, "w", encoding="utf-8") as f:
            json.dump({"channel_id": cid, "updated_at": time.time()}, f, ensure_ascii=False)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 166, exc_info=True)


def _load_last_channel_id() -> str:
    try:
        if os.path.exists(DISCORD_LAST_CHANNEL_FILE):
            with open(DISCORD_LAST_CHANNEL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            return str(data.get("channel_id") or "").strip()
    except Exception:
        return ""
    return ""

# Initialize Orchestrator
orchestrator = Orchestrator()

# Discord Client with intents
intents = discord.Intents.default()
intents.message_content = True  # Required to read message content
client = discord.Client(intents=intents)

_DISCORD_CHANNEL_FORBIDDEN_UNTIL = 0.0  # Backoff timer: skip channel ops until this timestamp
_DISCORD_CHANNEL_FORBIDDEN_BACKOFF = 300  # 5 minutes backoff after 403
_discord_forbidden_lock = threading.Lock()  # Protects cross-thread access to _DISCORD_CHANNEL_FORBIDDEN_UNTIL

_LINE_HEALTH_MONITORING_ENABLED = os.environ.get("MAGI_LINE_HEALTH_MONITORING_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
_LINE_HEALTH_LAST_ALERT_TS = 0.0
_LINE_HEALTH_FAIL_STREAK = 0
_LINE_HEALTH_ALERT_COOLDOWN_SEC = int(
    os.environ.get("MAGI_LINE_HEALTH_ALERT_COOLDOWN_SEC", "1800")
)
_LINE_HEALTH_CHECK_EVERY_LOOPS = max(1, int(os.environ.get("MAGI_LINE_HEALTH_CHECK_EVERY_LOOPS", "15") or "15"))
_LINE_HEALTH_TEST_RETRIES = max(1, int(os.environ.get("MAGI_LINE_HEALTH_TEST_RETRIES", "3") or "3"))
_LINE_HEALTH_TEST_RETRY_SEC = max(0.2, float(os.environ.get("MAGI_LINE_HEALTH_TEST_RETRY_SEC", "1.2") or "1.2"))
_LINE_HEALTH_AUTO_HEAL = os.environ.get("MAGI_LINE_HEALTH_AUTO_HEAL", "1").strip().lower() in {"1", "true", "yes", "on"}
_LINE_HEALTH_FAIL_STREAK_THRESHOLD = max(
    1, int(os.environ.get("MAGI_LINE_HEALTH_FAIL_STREAK_THRESHOLD", "3") or "3")
)
_LINE_LAST_CALLBACK_FILE = os.environ.get(
    "MAGI_LINE_LAST_CALLBACK_FILE",
    os.path.join(_AGENT_DIR, "line_last_callback.json"),
)
_LINE_HEALTH_RECENT_CALLBACK_SEC = max(
    30, int(os.environ.get("MAGI_LINE_HEALTH_RECENT_CALLBACK_SEC", "900") or "900")
)


def _load_last_line_callback_ts() -> float:
    try:
        if os.path.exists(_LINE_LAST_CALLBACK_FILE):
            with open(_LINE_LAST_CALLBACK_FILE, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            return float(data.get("updated_at") or 0.0)
    except Exception:
        return 0.0
    return 0.0


def _has_recent_line_callback() -> tuple[bool, float]:
    ts = _load_last_line_callback_ts()
    if ts <= 0:
        return False, 0.0
    age = max(0.0, time.time() - ts)
    return age <= float(_LINE_HEALTH_RECENT_CALLBACK_SEC), age


def _tailscale_bin() -> str:
    candidates = [
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/opt/homebrew/bin/tailscale",
        "tailscale",
    ]
    for c in candidates:
        if os.path.exists(c) or c == "tailscale":
            return c
    return "tailscale"


async def _run_shell(cmd: str, timeout_sec: int = 20) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 256, exc_info=True)
        return 124, "", "timeout"
    return int(proc.returncode or 0), (out or b"").decode("utf-8", "ignore"), (err or b"").decode("utf-8", "ignore")


async def _line_self_heal_funnel() -> str:
    """
    Best-effort recovery for LINE webhook connectivity.

    Prefer the configured stable public base (Tailscale Funnel / fixed HTTPS).
    Cloudflare Quick Tunnel is now opt-in only, because rotating hosts can fight
    with stable public links and leave stale calendar/share URLs.
    """
    import glob as _glob
    notes = []

    try:
        from api.startup import (
            _register_messaging_webhooks_for_base,
            _stable_webhook_base_url,
            _truthy_env,
        )

        stable_base = _stable_webhook_base_url()
        if stable_base and not _truthy_env("MAGI_ENABLE_CLOUDFLARE_WEBHOOK", "0"):
            await asyncio.to_thread(
                _register_messaging_webhooks_for_base,
                stable_base,
                source="discord_line_self_heal",
            )
            return f"stable_webhook={stable_base}"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_line_self_heal_funnel/stable", exc_info=True)

    # Check if cloudflared is already running
    code, out, _ = await _run_shell("pgrep -f 'cloudflared tunnel'", timeout_sec=5)
    if code != 0:
        # Start cloudflared
        notes.append("starting cloudflared")
        await _run_shell(
            "/opt/homebrew/bin/cloudflared tunnel --url http://127.0.0.1:18790 --no-autoupdate "
            f"2>{_MAGI_ROOT}/logs/cloudflared.log &",
            timeout_sec=5,
        )
        await asyncio.sleep(10)  # Wait for tunnel to establish

    # Extract tunnel URL from cloudflared log
    log_path = f"{_MAGI_ROOT}/logs/cloudflared.log"
    cf_url = ""
    try:
        code2, out2, _ = await _run_shell(
            f"grep -o 'https://[a-z0-9-]*\\.trycloudflare\\.com' {log_path} 2>/dev/null | head -1",
            timeout_sec=5,
        )
        cf_url = (out2 or "").strip()
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 292, exc_info=True)

    if not cf_url:
        return "cloudflared running but no URL found"

    webhook_url = f"{cf_url}/line/webhook"
    notes.append(f"tunnel={cf_url}")

    # Load LINE token
    line_token = os.environ.get("MAGI_LINE_CHANNEL_ACCESS_TOKEN", "")
    if not line_token:
        try:
            env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("MAGI_LINE_CHANNEL_ACCESS_TOKEN="):
                        line_token = line.split("=", 1)[1].strip().strip("\"'")
                        break
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 312, exc_info=True)

    if not line_token:
        notes.append("no LINE token")
        return " | ".join(notes)

    # Register webhook with LINE API
    import json as _json
    code3, out3, _ = await _run_shell(
        f'curl -s -X PUT '
        f'-H "Authorization: Bearer {line_token}" '
        f'-H "Content-Type: application/json" '
        f'-d \'{_json.dumps({"endpoint": webhook_url})}\' '
        f'https://api.line.me/v2/bot/channel/webhook/endpoint',
        timeout_sec=15,
    )
    reg_result = (out3 or "").strip()
    notes.append(f"reg={reg_result}")

    # Save URL for health monitoring
    agent_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".agent")
    os.makedirs(agent_dir, exist_ok=True)
    try:
        with open(os.path.join(agent_dir, "line_webhook_url.txt"), "w") as f:
            f.write(webhook_url + "\n")
        with open(os.path.join(agent_dir, "cloudflare_tunnel_url.txt"), "w") as f:
            f.write(cf_url + "\n")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 340, exc_info=True)

    return " | ".join(notes)

def _export_text_to_tmp(text: str, prefix: str = "casper") -> str:
    s = (text or "").strip()
    if not s:
        return ""
    try:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:10]
        name = f"{prefix}_{stamp}_{token}.txt"
        path = os.path.join(tempfile.gettempdir(), name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(s + "\n")
        return path
    except Exception:
        return ""

def _safe_remove_tmp(path: str) -> None:
    """
    Safety: never delete Synology Drive artifacts.
    For temp files (typically /tmp), allow cleanup via safe_fs.
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
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 373, exc_info=True)
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 378, exc_info=True)


def _likely_long_task(user_text: str, attachment: Optional[dict]) -> bool:
    if attachment:
        return True
    t = (user_text or "").lower()
    if not t:
        return False
    if "http://" in t or "https://" in t:
        return True
    if len(t) > 1200:
        return True
    heavy_keywords = [
        "翻譯", "translate", "摘要", "總結", "整理", "讀取", "分析", "文件", "檔案", "網頁", "網址",
        "搜尋", "search", "抓取", "fetch", "research",
        "深度思考", "deep think",
        "畫", "draw", "產生圖片", "generate image", "製作音樂", "生成音樂",
        "閱卷", "筆錄", "法扶", "爬蟲", "同步",
    ]
    return any(k in t for k in heavy_keywords)


_CRON_RUNNING_TASKS: dict[str, asyncio.Task] = {}


def _cron_max_concurrent_jobs() -> int:
    try:
        raw = int(os.environ.get("MAGI_CRON_MAX_CONCURRENT_JOBS", "3") or "3")
    except Exception:
        raw = 3
    return max(1, min(6, raw))


def _cron_job_timeout(job: dict) -> int:
    _LONG_JOBS = {
        "job_nightly_regression",
        "job_distill_train",
        "job_weekend_resummary",
        "job_pdf_namer_nightly",
        "job_reprocess_insights",
        "job_obsidian_ingest",
        "job_laf_nightly_audit",
        "job_nightly_autopilot",
        "job_judicial_api_night_pull",
        "job_judicial_api_morning",
        "job_weekly_legal_crawl",
        "job_transcript_sync",
        "job_file_review_check",
        "job_weekend_bookmark",
        "job_nightly_bookmark_regex",
        "job_market_briefing_script",
        "job_wiki_synthesizer",
        "job_knowledge_lint",
        "job_smoke_external_chat",
        "job_translator_ape_regression",
        "job_omlx_switch_night",
        "job_omlx_switch_day",
        "job_benchmark_pdf_bookmarker",
        "job_research_brief_daily",
        "job_disk_cleanup_healthcheck",
        "job_1772867062892_6cef0b",
        "job_1776221713533_0a5366",
        "job_weekly_cache_cleanup",
        "job_benchmark_pdf_namer",
        "job_case_index_sync",
        "job_self_repair_reporter",
    }
    _LONG_JOB_TIMEOUTS = {
        "job_nightly_autopilot": 28800,
        "job_weekend_bookmark": 21600,
    }
    _custom_timeout = job.get("timeout_sec")
    if isinstance(_custom_timeout, (int, float)) and _custom_timeout > 0:
        return int(_custom_timeout)
    job_id = job.get("id")
    if job_id in _LONG_JOB_TIMEOUTS:
        return _LONG_JOB_TIMEOUTS[job_id]
    if job.get("long_job") is True:
        return 7200
    return 7200 if job_id in _LONG_JOBS else 600


async def _execute_scheduled_job(job: dict, semaphore: asyncio.Semaphore) -> None:
    """Run one cron job without blocking the minute-by-minute scheduler loop."""
    async with semaphore:
        loop = asyncio.get_running_loop()
        command = str(job.get("command") or "")
        job_id = str(job.get("id") or "?")
        logger.info("⏰ Executing scheduled job %s: %s", job_id, command[:300])

        if command.startswith("@MAGI"):
            clean_cmd = command.replace("@MAGI", "").strip()
            response = await loop.run_in_executor(
                _CRON_EXECUTOR,
                lambda: orchestrator.process_message(
                    "SYSTEM_CRON",
                    clean_cmd,
                    platform="DISCORD_CRON",
                    role="admin",
                ),
            )
            if response:
                try:
                    await loop.run_in_executor(
                        _CRON_EXECUTOR,
                        lambda: orchestrator.record_assistant_reply("SYSTEM_CRON", response),
                    )
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 512, exc_info=True)
                logger.info("⏰ Cron job [%s] result (%d chars): %.200s", job_id, len(response), response)
            return

        _timeout = _cron_job_timeout(job)
        _shell_env = {"MAGI_PREFER_LOCAL_DB": "1", "MAGI_NO_DELETE": "1"}
        try:
            from api.platforms.safe_process import parse_cron_command, run as _safe_run
            from skills.ops.cron_result_policy import should_log_cron_issue

            _argv = parse_cron_command(command)
        except Exception as parse_exc:
            logger.warning(
                "⚠️ Blocked suspicious cron command for %s: %s (%s)",
                job_id,
                command[:160],
                parse_exc,
            )
            try:
                from skills.management.issue_tracker import log_issue

                log_issue(
                    command=f"cron:{job.get('name') or job_id}",
                    error_msg=f"parse_error={type(parse_exc).__name__}: {parse_exc}",
                    context=f"schedule={job.get('schedule') or job.get('cron') or ''} command={command[:240]}",
                    severity="High",
                    source="discord_bot.cron_scheduler",
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 576, exc_info=True)
            return
        try:
            _sr = await loop.run_in_executor(
                _CRON_EXECUTOR,
                lambda: _safe_run(
                    _argv,
                    timeout_sec=_timeout,
                    cwd=_MAGI_ROOT,
                    env_extra=_shell_env,
                ),
            )
            _stdout_text = _sr.stdout or ""
            _stderr_text = _sr.stderr or ""
            if _sr.timed_out:
                logger.warning("⚠️ Shell job %s timed out (%ds)", job_id, _timeout)
            if _sr.returncode != 0:
                _err_text = _stderr_text[-4000:] if len(_stderr_text) > 4000 else _stderr_text
                _out_tail = ""
                if not _err_text.strip():
                    _out_tail = _stdout_text[-1500:] if _stdout_text else ""
                if should_log_cron_issue(_sr.returncode, _stdout_text, _stderr_text):
                    try:
                        from skills.management.issue_tracker import log_issue

                        log_issue(
                            command=f"cron:{job.get('name') or job_id}",
                            error_msg=f"exit={_sr.returncode} stderr={_err_text}" + (f" stdout_tail={_out_tail}" if _out_tail else ""),
                            context=f"schedule={job.get('schedule')} command={str(job.get('command'))[:200]}",
                            severity="High",
                            source="discord_bot.cron_scheduler",
                        )
                    except Exception:
                        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 568, exc_info=True)
                    logger.warning("⚠️ Shell job %s exited %d: %s", job_id, _sr.returncode, _err_text)
                else:
                    logger.info("✅ Shell job %s returned %d but stdout indicates success; issue suppressed", job_id, _sr.returncode)
            else:
                logger.info("✅ Shell job %s completed OK", job_id)
        except Exception as _se:
            logger.warning("⚠️ Shell job %s error: %s", job_id, _se)


def _cron_task_done(job_id: str, task: asyncio.Task) -> None:
    if _CRON_RUNNING_TASKS.get(job_id) is task:
        _CRON_RUNNING_TASKS.pop(job_id, None)
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        logger.warning("⚠️ Cron job %s task was cancelled", job_id)
        return
    if exc:
        logger.warning("⚠️ Cron job %s task crashed: %s", job_id, exc)


async def bg_scheduler_loop():
    """Background loop to check for scheduled tasks every minute."""
    await client.wait_until_ready()
    
    # Simple Cron Scheduler
    from scripts.ops.background_task_locks import SCHEDULER_LOCK_NAME, acquire_lock
    from skills.ops.cron_scheduler import CronScheduler
    scheduler_owner_lock = acquire_lock(
        SCHEDULER_LOCK_NAME,
        owner="discord_internal_cron",
        kind="scheduler",
        blocking=False,
    )
    if not scheduler_owner_lock.acquired:
        logger.info(
            "⏸️ Cron Scheduler skipped; scheduler owner lock is held by %s pid=%s",
            (scheduler_owner_lock.active_owner or {}).get("owner") or "?",
            (scheduler_owner_lock.active_owner or {}).get("pid") or "?",
        )
        return
    scheduler = CronScheduler()
    logger.info("⏰ Cron Scheduler Started (owner lock=%s)", scheduler_owner_lock.path)
    loop_counter = 0
    _startup_catchup_done = False  # Fires once on 2nd iteration (~60s after start)
    _catchup_hours = int(os.environ.get("MAGI_CRON_CATCHUP_HOURS", "8"))
    _catchup_min_hour = int(os.environ.get("MAGI_CRON_CATCHUP_MIN_HOUR", "6"))
    _late_catchup_enabled = os.environ.get("MAGI_CRON_LATE_CATCHUP_ENABLED", "1").strip().lower() in {"1", "true", "on", "yes"}
    _late_catchup_every = max(1, int(os.environ.get("MAGI_CRON_LATE_CATCHUP_EVERY_LOOPS", "1") or "1"))
    cron_semaphore = asyncio.Semaphore(_cron_max_concurrent_jobs())

    while not client.is_closed():
        try:
            # 1. Check due jobs (run in dedicated cron executor to avoid blocking event loop / heartbeat)
            loop = asyncio.get_running_loop()
            due_jobs = await loop.run_in_executor(_CRON_EXECUTOR, scheduler.check_due_jobs)

            # ── Startup catch-up ─────────────────────────────────────────────────────
            # On the second loop iteration (~60s after start), scan for jobs that were
            # due while MAGI was offline (kernel panic, restart, etc.) and prepend them
            # to this iteration's due_jobs so they execute immediately.
            if not _startup_catchup_done and loop_counter == 1:
                _startup_catchup_done = True
                try:
                    _catchup_jobs = await loop.run_in_executor(
                        _CRON_EXECUTOR,
                        lambda: scheduler.get_missed_jobs(_catchup_hours, _catchup_min_hour),
                    )
                    if _catchup_jobs:
                        logger.info(
                            "🔄 [Startup Catch-up] %d missed job(s) will run now: %s",
                            len(_catchup_jobs),
                            ", ".join(j.get("id", "?") for j in _catchup_jobs),
                        )
                        existing_ids = {str(j.get("id") or "") for j in due_jobs}
                        for _job in _catchup_jobs:
                            await loop.run_in_executor(_CRON_EXECUTOR, lambda j=_job: scheduler.mark_job_run(j.get("id")))
                        due_jobs = [j for j in _catchup_jobs if str(j.get("id") or "") not in existing_ids] + list(due_jobs)
                    else:
                        logger.info(
                            "✅ [Startup Catch-up] No missed jobs (window=%dh, min_hour=%d)",
                            _catchup_hours, _catchup_min_hour,
                        )
                except Exception as _cu_err:
                    logger.warning("⚠️ [Startup Catch-up] Error during scan: %s", _cu_err)
            elif _startup_catchup_done and _late_catchup_enabled and loop_counter % _late_catchup_every == 0:
                try:
                    _catchup_jobs = await loop.run_in_executor(
                        _CRON_EXECUTOR,
                        lambda: scheduler.get_missed_jobs(_catchup_hours, _catchup_min_hour),
                    )
                    if _catchup_jobs:
                        existing_ids = {str(j.get("id") or "") for j in due_jobs}
                        _late_jobs = [j for j in _catchup_jobs if str(j.get("id") or "") not in existing_ids]
                        if _late_jobs:
                            logger.info(
                                "🔄 [Late Catch-up] %d missed job(s) will run now: %s",
                                len(_late_jobs),
                                ", ".join(j.get("id", "?") for j in _late_jobs),
                            )
                            for _job in _late_jobs:
                                await loop.run_in_executor(_CRON_EXECUTOR, lambda j=_job: scheduler.mark_job_run(j.get("id")))
                            due_jobs = list(_late_jobs) + list(due_jobs)
                except Exception as _cu_err:
                    logger.warning("⚠️ [Late Catch-up] Error during scan: %s", _cu_err)
            # ─────────────────────────────────────────────────────────────────────────

            for job in due_jobs:
                job_id = str(job.get("id") or "")
                if not job_id:
                    continue
                running = _CRON_RUNNING_TASKS.get(job_id)
                if running and not running.done():
                    logger.info("⏭️ Cron job %s already running; skip overlapping launch", job_id)
                    continue
                task = asyncio.create_task(
                    _execute_scheduled_job(job, cron_semaphore),
                    name=f"magi-cron:{job_id}",
                )
                _CRON_RUNNING_TASKS[job_id] = task
                task.add_done_callback(lambda t, jid=job_id: _cron_task_done(jid, t))
                logger.info("🚀 Cron job %s dispatched asynchronously", job_id)
            
            # Health check cadence is configurable to reduce LINE API rate-limit.
            if _LINE_HEALTH_MONITORING_ENABLED and loop_counter % _LINE_HEALTH_CHECK_EVERY_LOOPS == 0:
                await check_line_health(client)

            # Night Talk scheduling is handled by cron_jobs.json — no hardcoded trigger here.

            # Wait for next minute check
            await asyncio.sleep(60)
            loop_counter += 1
            
        except Exception as e:
            logger.error(f"Scheduler loop error: {e}")
            await asyncio.sleep(60)

async def check_line_health(client):
    """
    Periodically checks if the LINE Webhook Server and Tailscale Funnel are reachable.
    """
    global _LINE_HEALTH_LAST_ALERT_TS, _LINE_HEALTH_FAIL_STREAK

    async def _alert_once(msg: str):
        global _LINE_HEALTH_LAST_ALERT_TS
        now = time.time()
        if now - _LINE_HEALTH_LAST_ALERT_TS < max(60, _LINE_HEALTH_ALERT_COOLDOWN_SEC):
            logger.warning(f"🚨 LINE Monitoring Alert (suppressed by cooldown): {msg}")
            return
        _LINE_HEALTH_LAST_ALERT_TS = now
        logger.error(f"🚨 LINE Monitoring Alert: {msg}")
        try:
            routed_channel_id = DISCORD_CHANNEL_ID
            try:
                from api.discord_channel_router import resolve_discord_channel

                _, routed_channel_id = resolve_discord_channel(
                    msg,
                    topic_key="alert",
                    source="line_health",
                    fallback_channel_id=DISCORD_CHANNEL_ID,
                )
            except Exception:
                logging.getLogger(__name__).debug(
                    "silent-catch at %s:%s", __name__, "line_health_route", exc_info=True
                )
            if routed_channel_id == "__SILENT__":
                return
            channel = client.get_channel(int(routed_channel_id)) if routed_channel_id else None
            if channel:
                await channel.send(f"🚨 **MAGI LINE Connection Alert**: {msg}")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 576, exc_info=True)

    try:
        # 1. Check Local Server
        async with aiohttp.ClientSession() as session:
            _server_port = __import__('os').environ.get("MAGI_SERVER_PORT", "5002")
            async with session.get(f'http://localhost:{_server_port}/health', timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    raise Exception(f"Local Server (Port {_server_port}) returned {resp.status}")

        # 1.5 If we recently received real LINE callbacks, treat channel as healthy.
        recent_ok, callback_age = _has_recent_line_callback()
        if recent_ok:
            if _LINE_HEALTH_FAIL_STREAK >= _LINE_HEALTH_FAIL_STREAK_THRESHOLD:
                logger.info(
                    "✅ LINE webhook health recovered by real callback (age %.0fs).",
                    callback_age,
                )
            _LINE_HEALTH_FAIL_STREAK = 0
            logger.debug(
                "LINE health check bypassed webhook/test due to recent callback (age %.0fs).",
                callback_age,
            )
            return

        # 2. Check LINE official webhook connectivity via Messaging API.
        token = (
            os.environ.get("MAGI_LINE_CHANNEL_ACCESS_TOKEN")
            or os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
            or ""
        ).strip()
        if not token:
            await _alert_once("LINE channel access token missing in env")
            return

        endpoint = os.environ.get("MAGI_LINE_WEBHOOK_ENDPOINT", "").strip()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        async with aiohttp.ClientSession() as session:
            # Prefer current configured endpoint from LINE console.
            try:
                async with session.get(
                    "https://api.line.me/v2/bot/channel/webhook/endpoint",
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=20,
                ) as r_get:
                    if r_get.status == 200:
                        payload = await r_get.json()
                        endpoint = (payload.get("endpoint") or endpoint).strip() or endpoint
                        if not endpoint:
                            await _alert_once("LINE webhook endpoint is not configured")
                            return
                    else:
                        txt = (await r_get.text())[:240]
                        await _alert_once(f"LINE endpoint query failed: HTTP {r_get.status} {txt}")
                        return
            except Exception as e:
                await _alert_once(f"LINE endpoint query error: {e}")
                return

            async def _webhook_test_once() -> tuple[bool, str]:
                try:
                    async with session.post(
                        "https://api.line.me/v2/bot/channel/webhook/test",
                        headers=headers,
                        json={"endpoint": endpoint},
                        timeout=20,
                    ) as r_test:
                        txt = await r_test.text()
                        if r_test.status != 200:
                            return False, f"LINE webhook test failed: HTTP {r_test.status} {txt[:240]}"
                        try:
                            j = json.loads(txt)
                        except Exception:
                            return False, f"LINE webhook test non-JSON response: {txt[:240]}"
                        if bool(j.get("success")):
                            return True, "OK"
                        reason = (j.get("reason") or "UNKNOWN").strip()
                        detail = (j.get("detail") or "").strip()
                        return False, f"LINE webhook unreachable: {reason} {detail}".strip()
                except Exception as e:
                    return False, f"LINE webhook test error: {e}"

            last_err = ""
            for _ in range(_LINE_HEALTH_TEST_RETRIES):
                ok, msg = await _webhook_test_once()
                if ok:
                    if _LINE_HEALTH_FAIL_STREAK >= _LINE_HEALTH_FAIL_STREAK_THRESHOLD:
                        logger.info("✅ LINE webhook health recovered after %d consecutive failures.", _LINE_HEALTH_FAIL_STREAK)
                    _LINE_HEALTH_FAIL_STREAK = 0
                    return
                last_err = msg
                await asyncio.sleep(_LINE_HEALTH_TEST_RETRY_SEC)

            # Fallback: if LINE test API reports COULD_NOT_CONNECT but our
            # endpoint is actually reachable via direct HTTP probe, treat as
            # healthy (LINE's test infrastructure has known TLS compat issues
            # with Tailscale Funnel).
            if "COULD_NOT_CONNECT" in last_err or "negotiation failure" in last_err.lower():
                try:
                    async with aiohttp.ClientSession() as _probe_session:
                        async with _probe_session.get(endpoint, timeout=10, ssl=True) as _probe_resp:
                            if 200 <= _probe_resp.status < 500:
                                logger.info(
                                    "LINE webhook test API failed (%s) but direct HTTP probe OK (HTTP %d) — treating as healthy.",
                                    last_err[:80], _probe_resp.status,
                                )
                                _LINE_HEALTH_FAIL_STREAK = 0
                                return
                except Exception as _probe_err:
                    logger.debug("LINE direct probe also failed: %s", _probe_err)

            # Optional self-healing step for intermittent Tailscale negotiation failures.
            if _LINE_HEALTH_AUTO_HEAL and ("COULD_NOT_CONNECT" in last_err or "negotiation failure" in last_err.lower()):
                heal_note = await _line_self_heal_funnel()
                logger.warning(f"LINE auto-heal attempted: {heal_note}")
                await asyncio.sleep(1.5)
                ok, msg = await _webhook_test_once()
                if ok:
                    logger.info("LINE webhook recovered after auto-heal.")
                    _LINE_HEALTH_FAIL_STREAK = 0
                    return
                last_err = msg

            _LINE_HEALTH_FAIL_STREAK += 1
            if _LINE_HEALTH_FAIL_STREAK < _LINE_HEALTH_FAIL_STREAK_THRESHOLD:
                logger.warning(
                    "LINE health transient failure (%d/%d): %s",
                    _LINE_HEALTH_FAIL_STREAK,
                    _LINE_HEALTH_FAIL_STREAK_THRESHOLD,
                    last_err,
                )
                return
            await _alert_once(f"{last_err}（連續失敗 {_LINE_HEALTH_FAIL_STREAK} 次）")
            return
    except Exception as e:
        await _alert_once(str(e))


async def _download_discord_attachment(att):
    """
    Download a Discord attachment to /tmp and return local path.
    """
    suffix = os.path.splitext(att.filename or "")[1] or ".bin"
    if suffix in (".bin", "") and hasattr(att, "content_type") and att.content_type:
        _ct_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
                   "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-m4a": ".m4a", "audio/ogg": ".ogg",
                   "application/pdf": ".pdf"}
        suffix = _ct_map.get(att.content_type, suffix)
    temp_name = f"discord_{uuid.uuid4().hex}{suffix}"
    temp_path = os.path.join(tempfile.gettempdir(), temp_name)
    await att.save(temp_path)
    return temp_path


@client.event
async def on_ready():
    logger.info(f"🎮 Discord Bot connected as {client.user}")
    expected_bot_id = (os.environ.get("MAGI_DISCORD_EXPECTED_BOT_ID") or "").strip()
    actual_bot_id = str(getattr(client.user, "id", "") or "").strip()
    if expected_bot_id and actual_bot_id and actual_bot_id != expected_bot_id:
        logger.critical(
            "❌ Discord bot mismatch: expected id=%s, got id=%s (%s)",
            expected_bot_id,
            actual_bot_id,
            client.user,
        )
        await client.close()
        os._exit(1)
    
    # Start Scheduler (optional)
    if INTERNAL_CRON_ENABLED:
        client.loop.create_task(bg_scheduler_loop())
    else:
        logger.info("⏸️ Internal CronScheduler disabled (MAGI daemon scheduler is source of truth). Set MAGI_INTERNAL_CRON_ENABLED=1 to enable.")
    
    # Register Async Callback
    def send_async_notification(user_id, message, platform="Discord", *, topic_key="", source=""):
        """
        Callback to send messages from background threads.
        Discord 僅用於互動指令與聊天回覆，系統報告與定期通知不發送。

        Parameters:
            topic_key: 可選，用於頻道路由（如 "filereview", "laf", "transcript"）
            source: 可選，呼叫來源（如 "file_review_orchestrator", "laf_orchestrator"）
        """
        # Block system-initiated notifications; keep user-triggered async results
        if str(user_id or "").startswith("SYSTEM"):
            logger.debug("Skipping Discord system notification (user_id=%s)", user_id)
            return

        # Block fallback messages generated by tw_output_guard when background
        # task output was entirely stripped (no real content for the user).
        _msg_stripped = str(message or "").strip()
        _FALLBACK_MARKERS = (
            "⏳ 已收到，正在處理中",
            "偵測到非任務型通用回覆",
        )
        if any(m in _msg_stripped for m in _FALLBACK_MARKERS) and len(_msg_stripped) < 80:
            logger.debug("Skipping Discord fallback/guard message: %s", _msg_stripped[:60])
            return

        async def _send():
            try:
                # user_id format: "discord_123456"
                real_id = user_id.replace("discord_", "")

                # 頻道路由：依 topic_key + 訊息內容選擇目標頻道
                # 通知預設走 DISCORD_NOTIFY_CHANNEL_ID，避免打擾聊天頻道
                default_channel_id = DISCORD_NOTIFY_CHANNEL_ID or DISCORD_CHANNEL_ID or _load_last_channel_id()
                routed_channel_id = default_channel_id
                routed_sub_topic = ""
                try:
                    from api.discord_channel_router import resolve_discord_channel
                    routed_sub_topic, routed_channel_id = resolve_discord_channel(
                        message,
                        topic_key=topic_key or "",
                        source=source or "",
                        fallback_channel_id=default_channel_id,
                    )
                    if routed_channel_id == "__SILENT__":
                        logger.debug("Discord notification silenced for topic '%s'", routed_sub_topic)
                        return
                    if not routed_channel_id:
                        routed_channel_id = default_channel_id
                except Exception as _route_err:
                    logger.debug("Discord channel routing fallback: %s", _route_err)
                    routed_channel_id = default_channel_id

                channel_id = routed_channel_id
                channel = client.get_channel(int(channel_id)) if channel_id else None
                
                if channel:
                    # Determine if we should mention the user
                    safe_message = _normalize_discord_output_text(message)
                    file_path = ""
                    image_path = ""
                    text_part = safe_message
                    if "|||FILE_PATH|||" in safe_message:
                        text_part, file_path = safe_message.split("|||FILE_PATH|||", 1)
                        file_path = (file_path or "").strip()
                    elif "|||IMAGE_PATH|||" in safe_message:
                        text_part, image_path = safe_message.split("|||IMAGE_PATH|||", 1)
                        image_path = (image_path or "").strip()

                    prefix = "🤖 " if "SYSTEM" in user_id else f"<@{real_id}> "
                    prefix = prefix if text_part.strip() else ""
                    if file_path and os.path.exists(file_path):
                        sent = await channel.send(
                            content=(prefix + text_part.strip()).strip() or "📎 檔案如下：",
                            file=discord.File(file_path, filename=pathlib.Path(file_path).name),
                        )
                        _append_channel_delivery_audit(
                            {
                                "platform": "discord",
                                "kind": "document",
                                "channel_id": str(getattr(channel, "id", "") or ""),
                                "user_id": str(user_id or ""),
                                "message_id": str(getattr(sent, "id", "") or ""),
                                "file_path": file_path,
                                "file_name": pathlib.Path(file_path).name,
                                "file_size": os.path.getsize(file_path),
                                "text_sha1": _audit_sha1((prefix + text_part.strip()).strip()),
                                "preview": _audit_preview((prefix + text_part.strip()).strip()),
                                "ok": True,
                            }
                        )
                        return
                    if image_path and os.path.exists(image_path):
                        sent = await channel.send(
                            content=(prefix + text_part.strip()).strip() or "🖼️ 圖片如下：",
                            file=discord.File(image_path, filename=pathlib.Path(image_path).name),
                        )
                        _append_channel_delivery_audit(
                            {
                                "platform": "discord",
                                "kind": "image",
                                "channel_id": str(getattr(channel, "id", "") or ""),
                                "user_id": str(user_id or ""),
                                "message_id": str(getattr(sent, "id", "") or ""),
                                "file_path": image_path,
                                "file_name": pathlib.Path(image_path).name,
                                "file_size": os.path.getsize(image_path),
                                "text_sha1": _audit_sha1((prefix + text_part.strip()).strip()),
                                "preview": _audit_preview((prefix + text_part.strip()).strip()),
                                "ok": True,
                            }
                        )
                        return
                    if "SYSTEM" in user_id:
                        payload = f"🤖 {text_part.strip()}"
                    else:
                        mention = f"<@{real_id}>"
                        payload = f"{mention} {text_part.strip()}"
                    # 超過 2000 字元時分段發送（按行拆分）
                    if len(payload) <= 2000:
                        _chunks = [payload]
                    else:
                        _chunks = []
                        _cur = ""
                        for _line in payload.split("\n"):
                            _candidate = (_cur + "\n" + _line) if _cur else _line
                            if len(_candidate) > 2000:
                                if _cur:
                                    _chunks.append(_cur)
                                _cur = _line[:2000]
                            else:
                                _cur = _candidate
                        if _cur:
                            _chunks.append(_cur)
                        if not _chunks:
                            _chunks = [payload[:2000]]
                    sent = None
                    for _chunk in _chunks:
                        sent = await channel.send(_chunk)
                    _append_channel_delivery_audit(
                        {
                            "platform": "discord",
                            "kind": "text",
                            "channel_id": str(getattr(channel, "id", "") or ""),
                            "user_id": str(user_id or ""),
                            "message_id": str(getattr(sent, "id", "") or ""),
                            "text_sha1": _audit_sha1(payload),
                            "preview": _audit_preview(payload),
                            "ok": True,
                        }
                    )
                    # ── Mirror: 同時發送到測試伺服器 ──
                    try:
                        from api.discord_channel_router import get_mirror_channel_id
                        _mirror_id = get_mirror_channel_id(routed_sub_topic) if routed_sub_topic else ""
                        if _mirror_id and str(_mirror_id) != str(channel_id):
                            _mirror_ch = client.get_channel(int(_mirror_id))
                            if _mirror_ch:
                                _mirror_text = safe_message.strip() if safe_message else (str(message or "").strip())
                                _mirror_full = f"🪞 {_mirror_text}"
                                if file_path and os.path.exists(file_path):
                                    await _mirror_ch.send(content=_mirror_full[:2000], file=discord.File(file_path, filename=pathlib.Path(file_path).name))
                                elif image_path and os.path.exists(image_path):
                                    await _mirror_ch.send(content=_mirror_full[:2000], file=discord.File(image_path, filename=pathlib.Path(image_path).name))
                                else:
                                    # 分段發送 mirror（不截斷）
                                    if len(_mirror_full) <= 2000:
                                        await _mirror_ch.send(_mirror_full)
                                    else:
                                        _m_chunks = []
                                        _m_cur = ""
                                        for _m_line in _mirror_full.split("\n"):
                                            _m_cand = (_m_cur + "\n" + _m_line) if _m_cur else _m_line
                                            if len(_m_cand) > 2000:
                                                if _m_cur:
                                                    _m_chunks.append(_m_cur)
                                                _m_cur = _m_line[:2000]
                                            else:
                                                _m_cur = _m_cand
                                        if _m_cur:
                                            _m_chunks.append(_m_cur)
                                        for _m_chunk in (_m_chunks or [_mirror_full[:2000]]):
                                            await _mirror_ch.send(_m_chunk)
                    except Exception as _mirr_err:
                        logger.debug("Mirror send failed: %s", _mirr_err)

            except discord.Forbidden:
                global _DISCORD_CHANNEL_FORBIDDEN_UNTIL
                with _discord_forbidden_lock:
                    _DISCORD_CHANNEL_FORBIDDEN_UNTIL = time.time() + _DISCORD_CHANNEL_FORBIDDEN_BACKOFF
                logger.warning("⚠️ Async notification 403 — Discord channel access denied. Backing off %ds.", _DISCORD_CHANNEL_FORBIDDEN_BACKOFF)
            except Exception as e:
                logger.error(f"❌ Failed to send async notification: {e}")

        # Schedule the coroutine in the main loop
        if client.loop:
            _fut = asyncio.run_coroutine_threadsafe(_send(), client.loop)
            _fut.add_done_callback(lambda f: f.exception() and logger.warning("async notification failed: %s", f.exception()))

    orchestrator.register_callback(send_async_notification)
    logger.info("🔗 Registered Orchestrator Callback for Async Notifications")


@client.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == client.user:
        return
    # Ignore all bot / webhook messages to avoid command loops
    # (e.g. status bots posting notifications that contain workflow keywords).
    if getattr(message.author, "bot", False):
        return
    if getattr(message, "webhook_id", None):
        return

    # Allow DMs (private messages) from anyone
    _is_dm = isinstance(message.channel, discord.DMChannel)

    # In guild context: only respond in configured channel or MAGI-routed channels
    if not _is_dm:
        _current_ch_id = str(message.channel.id)
        _is_main_channel = (not DISCORD_CHANNEL_IDS) or (_current_ch_id in DISCORD_CHANNEL_IDS)
        _is_routed_channel = False
        if not _is_main_channel:
            try:
                from api.discord_channel_router import _load_all_routed_channel_ids, infer_topic_from_channel_metadata
                _routed_ids = _load_all_routed_channel_ids()
                _is_routed_channel = _current_ch_id in _routed_ids
                if not _is_routed_channel:
                    _topic_by_name = infer_topic_from_channel_metadata(
                        name=str(getattr(message.channel, "name", "") or ""),
                        topic=str(getattr(message.channel, "topic", "") or ""),
                    )
                    _is_routed_channel = bool(_topic_by_name and _topic_by_name != "general")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 915, exc_info=True)
        if not _is_main_channel and not _is_routed_channel:
            return
    
    # Get user info
    user_id = f"discord_{message.author.id}"
    user_name = message.author.display_name
    
    # Strip bot mention
    content = message.content
    if client.user.mentioned_in(message):
        content = content.replace(f'<@{client.user.id}>', '').replace(f'<@!{client.user.id}>', '').strip()
    
    user_text = content
    attachment_info = None
    temp_attachment_path = None
    
    # Skip empty messages or bot commands starting with other prefixes
    # Allow help, draw, img, start, search commands
    if not user_text and not message.attachments:
        return

    if user_text and (user_text.startswith("/") and not user_text.lower().startswith(("/help", "/draw", "/img", "/start", "/search", "/setup_channels", "/setup"))):
        return

    # ── Skip replies to MAGI notification messages ───────────────────
    # When user replies to a notification (📋 📥 💰 etc.) posted by MAGI,
    # do not treat the reply as a conversational prompt.
    _NOTIF_PREFIXES = ("📋", "💰", "📥", "⚠️ 閱卷", "✅ 閱卷", "🔔")
    if message.reference and message.reference.message_id:
        try:
            ref_msg = message.reference.resolved or await message.channel.fetch_message(message.reference.message_id)
            if ref_msg and getattr(ref_msg.author, "bot", False):
                ref_text = (ref_msg.content or "").strip()
                if any(ref_text.startswith(p) for p in _NOTIF_PREFIXES):
                    logger.info("🔕 Discord: skipping reply-to-notification (ref: %s)", ref_text[:60])
                    return
        except Exception as ref_err:
            logger.debug(f"Discord ref-message lookup failed (non-fatal): {ref_err}")
    # ─────────────────────────────────────────────────────────────────

    logger.info(f"📩 Discord message from {user_name} ({user_id}): {user_text[:50]}...")
    # Strict admin: only allow explicit allowlist via DISCORD_ADMIN_IDS.
    # Do not rely on "guild administrator" permission to avoid accidental privilege escalation.
    role = "admin" if (str(message.author.id) in DISCORD_ADMIN_IDS) else "user"

    # ── Admin commands ──────────────────────────────────────────────
    _cmd_lower = (user_text or "").strip().lower()
    if _cmd_lower in ("setup_channels", "!magi setup_channels", "/setup_channels"):
        if role != "admin":
            await message.channel.send("⛔ 此指令僅限管理員使用。")
            return
        try:
            from api.discord_channel_router import auto_setup_channels
            await message.channel.send("⏳ 正在建立 MAGI 通知子頻道…")
            guild = message.guild
            if not guild:
                await message.channel.send("❌ 此指令只能在伺服器內使用。")
                return
            channel_map = await auto_setup_channels(guild)
            lines = ["✅ **MAGI 通知子頻道建立完成！**", ""]
            for key, cid in channel_map.items():
                lines.append(f"  • `{key}` → <#{cid}>")
            lines.append("")
            lines.append("映射已儲存，通知將自動分流到對應頻道。")
            await message.channel.send("\n".join(lines))
        except Exception as setup_err:
            logger.error("setup_channels failed: %s", setup_err)
            await message.channel.send(f"❌ 建立頻道失敗: {setup_err}")
        return

    if _cmd_lower in ("show_channels", "!magi show_channels", "/show_channels"):
        try:
            from api.discord_channel_router import _load_channel_map
            cmap = _load_channel_map()
            if not cmap:
                await message.channel.send("📋 目前尚未設定頻道路由。請使用 `setup_channels` 自動建立。")
            else:
                lines = ["📋 **目前 MAGI 頻道路由映射：**", ""]
                for key, cid in cmap.items():
                    lines.append(f"  • `{key}` → <#{cid}>")
                await message.channel.send("\n".join(lines))
        except Exception as show_err:
            await message.channel.send(f"❌ 讀取頻道映射失敗: {show_err}")
        return
    # ─────────────────────────────────────────────────────────────────

    # Record last channel for proactive notifications when DISCORD_CHANNEL_ID is unset.
    # Bind on any message that we actually process (this bot can be single-tenant).
    if not DISCORD_CHANNEL_IDS:
        _save_last_channel_id(str(message.channel.id))
    
    correlation_id = ""
    _start_ts = time.monotonic()
    try:
        # Send quick ACK for likely long tasks so users don't feel the bot is stuck.
        long_task = _likely_long_task(user_text, None)
        if long_task or bool(message.attachments):
            try:
                ack_msg = "⏳ 已收到，正在處理中。完成後我會回覆結果。"
                # If file attachment present, generate time estimate
                if message.attachments:
                    att0 = message.attachments[0]
                    try:
                        # Prefer filename (has extension) over title (may lack extension)
                        real_fname = att0.filename or getattr(att0, "title", None) or ""
                        # If filename still has no extension, try to infer from content_type
                        if real_fname and not os.path.splitext(real_fname)[1]:
                            ct = getattr(att0, "content_type", "") or ""
                            _ct_ext_map = {
                                "image/png": ".png", "image/jpeg": ".jpg",
                                "image/gif": ".gif", "image/webp": ".webp",
                                "image/bmp": ".bmp", "image/heic": ".heic",
                                "audio/mpeg": ".mp3", "audio/wav": ".wav",
                                "audio/x-m4a": ".m4a", "audio/ogg": ".ogg",
                                "audio/flac": ".flac", "audio/aac": ".aac",
                                "audio/mp4": ".m4a",
                            }
                            _inferred = _ct_ext_map.get(ct.split(";")[0].strip().lower(), "")
                            if _inferred:
                                real_fname = real_fname + _inferred
                        ack_msg = orchestrator.estimate_file_processing_time(
                            file_size_bytes=getattr(att0, "size", 0) or 0,
                            filename=real_fname,
                            prompt=user_text or "",
                        )
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1024, exc_info=True)  # Fall back to generic ACK
                sent = await message.channel.send(ack_msg)
                _append_channel_delivery_audit(
                    {
                        "platform": "discord",
                        "kind": "ack",
                        "channel_id": str(getattr(message.channel, "id", "") or ""),
                        "user_id": str(user_id or ""),
                        "message_id": str(getattr(sent, "id", "") or ""),
                        "text_sha1": _audit_sha1(ack_msg),
                        "preview": _audit_preview(ack_msg),
                        "ok": True,
                    }
                )
            except Exception as ack_err:
                logger.warning(f"⚠️ Failed to send Discord immediate ACK: {ack_err}")

        # OBS-1: correlation ID  /  OBS-2: latency tracking
        correlation_id = f"magi-{uuid.uuid4().hex[:12]}"
        _start_ts = time.monotonic()

        # Show typing indicator while processing
        async with message.channel.typing():
            if message.attachments:
                att = message.attachments[0]
                if getattr(att, "size", 0) > MAX_ATTACHMENT_BYTES:
                    warn = f"⚠️ 附件過大（>{MAX_ATTACHMENT_BYTES // (1024 * 1024)}MB），請壓縮後再試。"
                    sent = await message.channel.send(warn)
                    _append_channel_delivery_audit(
                        {
                            "platform": "discord",
                            "kind": "text",
                            "channel_id": str(getattr(message.channel, "id", "") or ""),
                            "user_id": str(user_id or ""),
                            "message_id": str(getattr(sent, "id", "") or ""),
                            "text_sha1": _audit_sha1(warn),
                            "preview": _audit_preview(warn),
                            "ok": True,
                        }
                    )
                    return
                temp_attachment_path = await _download_discord_attachment(att)
                ext = (os.path.splitext(att.filename or "")[1] or "").lower()
                msg_type = "file"
                if ext in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".heic", ".heif", ".tiff", ".tif"}:
                    msg_type = "image"
                elif ext in {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"}:
                    msg_type = "audio"
                attachment_info = {
                    "type": msg_type,
                    "path": temp_attachment_path,
                    "filename": att.filename,
                }
                if not user_text:
                    if msg_type == "audio":
                        user_text = "請轉換成逐字稿，附上時間戳記，並輸出TXT檔。"
                    else:
                        user_text = "請分析這個附件並用繁體中文回覆重點。"
            # Process through Orchestrator (run in thread to avoid blocking)
            loop = asyncio.get_running_loop()
            _cid = correlation_id  # capture for lambda closure

            # Progress callback — push intermediate status to Discord during long tasks.
            _dc_channel = message.channel
            _dc_last_progress = [0.0]
            def _dc_progress_cb(phase, current, total, msg_text):
                import time as _pt
                now = _pt.monotonic()
                if now - _dc_last_progress[0] < 15:
                    return
                _dc_last_progress[0] = now
                try:
                    _fut = asyncio.run_coroutine_threadsafe(_dc_channel.send(str(msg_text or "")), loop)
                    _fut.add_done_callback(lambda f: f.exception() and logging.getLogger(__name__).warning("async progress callback failed: %s", f.exception()))
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1098, exc_info=True)

            # 2026-03-29: Build channel_context from Discord channel
            _dc_channel_ctx = None
            try:
                _dc_topic_key = ""
                _dc_ch_id = str(message.channel.id)
                try:
                    from api.discord_channel_router import _reverse_lookup_channel, infer_topic_from_channel_metadata
                    _sub_topic = _reverse_lookup_channel(_dc_ch_id)
                    if not _sub_topic:
                        _sub_topic = infer_topic_from_channel_metadata(
                            name=str(getattr(message.channel, "name", "") or ""),
                            topic=str(getattr(message.channel, "topic", "") or ""),
                        )
                    if _sub_topic:
                        # 保留完整 sub_topic 供頻道命令綁定使用
                        _dc_topic_key = _sub_topic
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1115, exc_info=True)
                _dc_channel_ctx = {
                    "topic_key": _dc_topic_key,
                    "channel_id": _dc_ch_id,
                    "thread_id": None,
                    "platform": "Discord",
                }
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1123, exc_info=True)

            # Persist to message queue (at-least-once delivery)
            _dc_mq_msg_id = None
            _dc_mq_inst = None
            try:
                from skills.memory.message_queue import get_queue as _get_mq
                _dc_mq_inst = _get_mq()
                _dc_mq_msg_id = _dc_mq_inst.enqueue(
                    platform="Discord",
                    user_id=user_id,
                    user_text=user_text,
                    role=role,
                    channel_id=str(getattr(message.channel, "id", "") or ""),
                    attachment=json.dumps(attachment_info) if attachment_info else "{}",
                )
            except Exception as _mq_err:
                logging.getLogger(__name__).warning("MQ enqueue failed (non-fatal): %s", _mq_err)

            def _dc_mq_wrapped_process():
                _mq_i = _dc_mq_inst
                _mq_id = _dc_mq_msg_id
                if _mq_i and _mq_id:
                    try:
                        _mq_i.claim(_mq_id)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "dc_mq_claim", exc_info=True)
                try:
                    result = orchestrator.process_message(
                        user_id,
                        user_text,
                        platform="Discord",
                        role=role,
                        attachment=attachment_info,
                        correlation_id=_cid,
                        progress_callback=_dc_progress_cb,
                        channel_context=_dc_channel_ctx,
                    )
                    if _mq_i and _mq_id:
                        try:
                            _mq_i.complete(_mq_id)
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "dc_mq_complete", exc_info=True)
                    return result
                except Exception as _proc_err:
                    if _mq_i and _mq_id:
                        try:
                            _mq_i.fail(_mq_id, str(_proc_err))
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "dc_mq_fail", exc_info=True)
                    raise

            response = await loop.run_in_executor(
                _DISCORD_BG_EXECUTOR,
                _dc_mq_wrapped_process,
            )
            if response:
                response = _normalize_discord_output_text(response)
                try:
                    _uid_for_reply = user_id
                    _resp_for_reply = response
                    await loop.run_in_executor(
                        _DISCORD_BG_EXECUTOR,
                        lambda: orchestrator.record_assistant_reply(_uid_for_reply, _resp_for_reply),
                    )
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1143, exc_info=True)

        if response:
            # Check for Image Path Protocol
            if "|||IMAGE_PATH|||" in response:
                try:
                    text_part, image_path = response.split("|||IMAGE_PATH|||", 1)
                    
                    if os.path.exists(image_path.strip()):
                        file_to_send = discord.File(image_path.strip())
                        sent = await message.channel.send(content=text_part, file=file_to_send)
                        _append_channel_delivery_audit(
                            {
                                "platform": "discord",
                                "kind": "image",
                                "channel_id": str(getattr(message.channel, "id", "") or ""),
                                "user_id": str(user_id or ""),
                                "message_id": str(getattr(sent, "id", "") or ""),
                                "file_path": image_path.strip(),
                                "file_name": pathlib.Path(image_path.strip()).name,
                                "file_size": os.path.getsize(image_path.strip()),
                                "text_sha1": _audit_sha1(text_part or ""),
                                "preview": _audit_preview(text_part or ""),
                                "ok": True,
                            }
                        )
                        logger.info(f"✅ Sent response with image to {user_name}")
                        return
                    else:
                        response = f"{text_part}\n⚠️ Image file not found at: {image_path}"
                except Exception as img_err:
                    logger.error(f"❌ Failed to send image: {img_err}")
                    response = f"{response.replace('|||IMAGE_PATH|||', ': ')}\n⚠️ Failed to upload image: {img_err}"
            elif "|||FILE_PATH|||" in response:
                try:
                    text_part, file_path = response.split("|||FILE_PATH|||", 1)
                    file_path = (file_path or "").strip()
                    if os.path.exists(file_path):
                        sent = await message.channel.send(
                            content=(text_part or "").strip() or "📎 檔案如下：",
                            file=discord.File(file_path, filename=pathlib.Path(file_path).name),
                        )
                        _append_channel_delivery_audit(
                            {
                                "platform": "discord",
                                "kind": "document",
                                "channel_id": str(getattr(message.channel, "id", "") or ""),
                                "user_id": str(user_id or ""),
                                "message_id": str(getattr(sent, "id", "") or ""),
                                "file_path": file_path,
                                "file_name": pathlib.Path(file_path).name,
                                "file_size": os.path.getsize(file_path),
                                "text_sha1": _audit_sha1((text_part or "").strip()),
                                "preview": _audit_preview((text_part or "").strip()),
                                "ok": True,
                            }
                        )
                        logger.info(f"✅ Sent response with file to {user_name}")
                        return
                    response = f"{(text_part or '').strip()}\n⚠️ 檔案不存在：{file_path}"
                except Exception as file_err:
                    logger.error(f"❌ Failed to send file: {file_err}")
                    response = f"{response.replace('|||FILE_PATH|||', ': ')}\n⚠️ Failed to upload file: {file_err}"

            # Split long messages (Discord limit: 2000 chars)
            if len(response) > 1900:
                chunks = _split_discord_chunks(response, 1900)
                for chunk in chunks:
                    sent = await message.channel.send(chunk)
                    _append_channel_delivery_audit(
                        {
                            "platform": "discord",
                            "kind": "text",
                            "channel_id": str(getattr(message.channel, "id", "") or ""),
                            "user_id": str(user_id or ""),
                            "message_id": str(getattr(sent, "id", "") or ""),
                            "text_sha1": _audit_sha1(chunk),
                            "preview": _audit_preview(chunk),
                            "ok": True,
                        }
                    )
            else:
                sent = await message.channel.send(response)
                _append_channel_delivery_audit(
                    {
                        "platform": "discord",
                        "kind": "text",
                        "channel_id": str(getattr(message.channel, "id", "") or ""),
                        "user_id": str(user_id or ""),
                        "message_id": str(getattr(sent, "id", "") or ""),
                        "text_sha1": _audit_sha1(response),
                        "preview": _audit_preview(response),
                        "ok": True,
                    }
                )
                
            logger.info(f"✅ Sent response to {user_name}")
            
    except Exception as e:
        logger.error(f"❌ Error processing Discord message: {e}")
        err_text = f"❌ 處理訊息時發生錯誤: {str(e)[:100]}"
        sent = await message.channel.send(err_text)
        _append_channel_delivery_audit(
            {
                "platform": "discord",
                "kind": "error",
                "channel_id": str(getattr(message.channel, "id", "") or ""),
                "user_id": str(user_id or ""),
                "message_id": str(getattr(sent, "id", "") or ""),
                "text_sha1": _audit_sha1(err_text),
                "preview": _audit_preview(err_text),
                "ok": True,
            }
        )
    finally:
        if temp_attachment_path and os.path.exists(temp_attachment_path):
            try:
                _safe_remove_tmp(temp_attachment_path)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1262, exc_info=True)
        # OBS-2: record processing latency
        try:
            elapsed_ms = int((time.monotonic() - _start_ts) * 1000)
            _append_channel_delivery_audit({
                "platform": "discord",
                "kind": "latency",
                "channel_id": str(getattr(message.channel, "id", "") or ""),
                "user_id": str(user_id or ""),
                "correlation_id": correlation_id,
                "latency_ms": elapsed_ms,
            })
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1275, exc_info=True)


def run_bot():
    """Start the Discord bot."""
    if not DISCORD_BOT_TOKEN:
        logger.error("❌ DISCORD_BOT_TOKEN not set!")
        return False
    
    logger.info("🚀 Starting Discord Bot...")
    
    try:
        client.run(DISCORD_BOT_TOKEN)
    except discord.LoginFailure:
        logger.error("❌ Invalid Discord Bot Token!")
        return False
    except Exception as e:
        logger.error(f"❌ Discord Bot Error: {e}")
        return False
    
    return True


if __name__ == "__main__":
    # Clean main block: No dotenv, no stale env vars.
    if not DISCORD_BOT_TOKEN:
        print("❌ Fatal: No Discord Bot Token found in Config!")
        sys.exit(1)

    if not run_bot():
        sys.exit(2)  # non-zero so daemon knows it's a startup failure

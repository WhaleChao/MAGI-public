import subprocess
import shutil
import threading
import time
import sys
import logging
import signal
import os
import shlex
import re
import urllib.request
import urllib.error
import json
import atexit
from pathlib import Path
from typing import Dict, Any

# Cross-platform file locking
_MAGI_ROOT = os.path.dirname(os.path.abspath(__file__))
if _MAGI_ROOT not in sys.path:
    sys.path.insert(0, _MAGI_ROOT)

# Load .env so all child processes inherit environment variables
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_MAGI_ROOT, ".env"))
except Exception:
    pass
try:
    from skills.ops.platform_utils import (
        file_lock, file_unlock, get_venv_python,
        IS_WINDOWS, IS_MACOS, get_magi_root,
    )
except ImportError:
    # Fallback if platform_utils not yet available
    import fcntl
    IS_WINDOWS = False
    IS_MACOS = sys.platform == "darwin"
    def file_lock(fh, exclusive=True, blocking=True):
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            flags |= fcntl.LOCK_NB
        fcntl.flock(fh.fileno(), flags)
    def file_unlock(fh):
        try: fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            logging.getLogger("Daemon").warning("Failed to release file lock: %s", e)
    def get_venv_python():
        return os.path.join(_MAGI_ROOT, "venv", "bin", "python3")
    def get_magi_root():
        return Path(_MAGI_ROOT)

# Global stability defaults for child processes.
os.environ.setdefault("MAGI_MYSQL_USE_PURE", "1")
os.environ.setdefault("MYSQL_USE_PURE", "1")
os.environ.setdefault("MAGI_AVOID_DISTRIBUTED", "1")
os.environ["MAGI_DAEMON"] = "1"  # Signal to child processes (server.py) to skip console StreamHandler

# Configure Logging with RotatingFileHandler
from logging.handlers import RotatingFileHandler as _RotatingFileHandler
_daemon_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".agent", "daemon.log")
os.makedirs(os.path.dirname(_daemon_log_path), exist_ok=True)
_log_fmt = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
_file_handler = _RotatingFileHandler(_daemon_log_path, maxBytes=5*1024*1024, backupCount=3)
_file_handler.setFormatter(_log_fmt)
# Under launchd, stderr is captured to /tmp/magi-daemon.log — adding a StreamHandler
# would duplicate every line (once via handler, once via launchd stderr capture).
# Only add console handler for interactive debugging.
_daemon_handlers = [_file_handler]
if sys.stderr.isatty():
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(_log_fmt)
    _daemon_handlers.append(_console_handler)
logging.basicConfig(level=logging.INFO, handlers=_daemon_handlers)
logger = logging.getLogger("Daemon")

from api.autopilot_artifacts import write_kill_reason as _store_autopilot_kill_reason

# Processes
# name -> {"proc": Popen, "command": str}
processes = {}
_processes_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════════════════
# Unified Reaper Configuration (統一殭屍清理設定)
# ─── 這是 MAGI 唯一的進程清理規則來源 ────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════

_LAST_REAP_AT = 0.0
_LAST_DEDUP_AT = 0.0
_timing_lock = threading.Lock()
_REAP_INTERVAL_SEC = int(os.environ.get("MAGI_REAP_INTERVAL_SEC", "45") or "45")
_ORPHAN_GRACE_SEC = int(os.environ.get("MAGI_ORPHAN_GRACE_SEC", "300") or "300")

# ── 永不殺清單：reaper 絕對不碰這些進程 ──
REAPER_NEVER_KILL = (
    "daemon.py",
    "api/server.py",
    "api/discord_bot.py",
    "api/line_bot.py",
    "api/telegram_bot.py",
    "skills/ops/cron_scheduler.py",
    "skills/ops/heartbeat.py",
    "rpc-server",
    "weekend_resummary.py",
    "nightly_distill_train.py",
    "worldmonitor",
    "run_nightly_guardian.sh",
    "run_db_sync.sh",
    "rename_watcher.py",
    "db_sync_to_remote.py",
    "ingest_raw_judgments.py",
    "resummary_batch.py",
)

# ── 每個 worker 的 grace period（秒）──
REAPER_GRACE_PERIODS = {
    # Judicial / crawl
    "skills/judgment-collector/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_JC_SEC", "420") or "420"),
    # File review
    "skills/file-review-orchestrator/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_FR_SEC", "1800") or "1800"),
    "skills/file-review-orchestrator/action.py --task download": int(os.environ.get("MAGI_ORPHAN_GRACE_FR_DOWNLOAD_SEC", "2400") or "2400"),
    # Transcript
    "skills/transcript-downloader/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_TR_SEC", "1800") or "1800"),
    "skills/transcript-downloader/action.py --task sync": int(os.environ.get("MAGI_ORPHAN_GRACE_TR_SYNC_SEC", "2400") or "2400"),
    "skills/transcript-downloader/action.py --task download_all": int(os.environ.get("MAGI_ORPHAN_GRACE_TR_ALL_SEC", "3000") or "3000"),
    # LAF
    "skills/laf-portal-automation/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_LAF_SEC", "2400") or "2400"),
    "skills/laf-orchestrator/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_LAF_ORCH_SEC", "2400") or "2400"),
    "skills/laf-withdrawal-report/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_LAF_WD_SEC", "1800") or "1800"),
    "skills/laf-refine-case/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_LAF_REFINE_SEC", "1200") or "1200"),
    # OSC / naming / vdb
    "skills/osc-orchestrator/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_OSC_ORCH_SEC", "2400") or "2400"),
    "skills/osc-scan-folder/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_OSC_SCAN_SEC", "1800") or "1800"),
    "skills/pdf-namer/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_PDF_NAMER_SEC", "1500") or "1500"),
    "skills/crawler-targets/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_CRAWLER_TARGETS_SEC", "1800") or "1800"),
    "skills/statutes-vdb/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_STATUTES_VDB_SEC", "1800") or "1800"),
    # P2-0 defense (2026-04-19): translator 有 self-spawn pattern，孤兒必須短時間內清掉
    # 正常翻譯 subprocess 應該 <120s，超過視為卡住或泄漏，reaper 盡早 kill 避免累積吃 RAM
    "skills/translator/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_TRANSLATOR_SEC", "180") or "180"),
    "skills/translator/action.py --task _translate_inner": int(os.environ.get("MAGI_ORPHAN_GRACE_TRANSLATOR_INNER_SEC", "120") or "120"),
    # Coordinator
    "skills/magi-autopilot/action.py": int(os.environ.get("MAGI_ORPHAN_GRACE_AUTOPILOT_SEC", "2400") or "2400"),
    # Short-lived NLP sidecars — should exit in <20s; cut grace short so orphans are reaped fast
    "chinese_nlp_sidecar.py": 30,          # pkuseg sidecar, should die in <20s
    # Background subprocesses (fire-and-forget)
    "MEMORY_ENABLE_FAISS": 300,            # FAISS rebuild — ~32萬 vectors, 通常 30-90s, 留 5min grace
    "build_from_db": 300,                   # same, alternative marker
    "weekend_resummary.py": 50400,          # weekly task — 14hr, 700+ judgments @ ~1min each
    "nightly_distill_train.py": 10800,      # LoRA training — 3hr
    "laf_nightly_audit.py": 1200,           # nightly LAF audit
    # Selenium / Chrome (LAF automation spawns headless Chrome)
    "chromedriver": 2700,                   # 45min grace — LAF automation can be slow
}

# ── 已知殭屍模式：不論 age 都直接殺 ──
REAPER_ZOMBIE_PATTERNS = (
    "test_nl_intent", "test_trans_orc",
    "/tmp/test_", "/tmp/debug_", "/tmp/scratch_",
    "quick_test", "quick_t",
)

# ── 安全工具：即使 stale 也永不殺 ──
REAPER_SAFE_UTILITIES = (
    "jedi", "pylsp", "language-server", "pyright",
    "pip", "venv", "certifi", "npm",
    # Long-running services managed by launchd / daemon — NOT stale orphans
    "omlx serve", "omlx-magi-start",       # oMLX inference servers (port 8080/8081)
    "magi_menubar.py",                       # Status Bar (macOS menu bar)
    "admin_server.py",                       # Website Admin (port 8088)
    "benchmark_",                            # benchmark scripts (may run >30min)
    "nas_pdf_ocr_worker",                    # NAS PDF OCR background worker
    "pkuseg_py311",                          # PKUSeg sidecar interpreter
)

# ── Reaper dedup: avoid retrying the same stale PID every cycle ──
_REAPER_ZOMBIE_SEEN = set()  # type: set[int]

# ── 向後相容別名（供 server.py 等 import）──
_ORPHAN_GRACE_BY_MARKER = REAPER_GRACE_PERIODS
_WORKER_MARKERS = tuple(REAPER_GRACE_PERIODS.keys())
_CORE_ALLOW_MARKERS = REAPER_NEVER_KILL
_STATE_PATH = Path(os.environ.get("MAGI_PROCESS_GUARDIAN_STATE",
                                   str(get_magi_root() / "static" / "process_guardian_state.json")))
_DAEMON_LOCK_PATH = Path(os.environ.get("MAGI_DAEMON_LOCK_PATH",
                                         str(get_magi_root() / "static" / "daemon.lock")))
_STATE: Dict[str, Any] = {
    "updated_at": "",
    "reap": {
        "total_killed": 0,
        "last_reap_at": "",
        "last_reap_killed": 0,
        "last_reap_force": False,
        "last_reaped": [],
    },
}
_DAEMON_LOCK_HANDLE = None

# ── Training lock: distill training 寫入此檔時，daemon/watchdog 跳過 oMLX 檢查 ──
TRAINING_LOCK_PATH = Path(os.environ.get(
    "MAGI_TRAINING_LOCK_PATH",
    str(get_magi_root() / "static" / "training.lock"),
))


def _is_training_locked() -> bool:
    """Check if a training job holds the lock (lock file exists and is < 6h old)."""
    try:
        if not TRAINING_LOCK_PATH.exists():
            return False
        age = time.time() - TRAINING_LOCK_PATH.stat().st_mtime
        if age > 6 * 3600:
            # Stale lock — training probably crashed; clean up
            TRAINING_LOCK_PATH.unlink(missing_ok=True)
            logger.warning("🔒 Stale training lock removed (age=%ds)", int(age))
            return False
        return True
    except Exception:
        return False


def _is_night_window() -> bool:
    """02:00-07:00 為夜間任務密集時段。"""
    import datetime
    return 2 <= datetime.datetime.now().hour < 7


# ---------------------------------------------------------------------------
# Auto-reap zombie children via SIGCHLD
# ---------------------------------------------------------------------------
def _sigchld_handler(signum, frame):
    """Reap all finished child processes to prevent zombies."""
    while True:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
        except ChildProcessError:
            break

if not IS_WINDOWS:
    signal.signal(signal.SIGCHLD, _sigchld_handler)

# ---------------------------------------------------------------------------
# Launchd-managed infrastructure services
# ---------------------------------------------------------------------------
_LAUNCHD_SERVICES = [
    {
        "label": "com.magi.omlx",
        "name": "oMLX Inference",
        "probe_url": f"http://127.0.0.1:{os.environ.get('MAGI_OMLX_PORT', '8080')}/v1/models",
        "probe_timeout": 10,    # startup 等待上限：不阻斷主啟動序列（oMLX 會自行上線）
        "kickstart_grace": 180, # kickstart 後 180s 內不再重複 probe-kickstart（防誤殺）
        "startup_probe": False, # 不在 startup 時同步等待 oMLX（E4B 需 ~120s，會卡住整個啟動）
    },
    {
        "label": "com.magi.omlx-embed",
        "name": "oMLX Embed",
        "probe_url": None,
        "probe_timeout": 15,
    },
    {
        "label": "com.magi.omlx-watchdog",
        "name": "oMLX Watchdog",
        "probe_url": None,
        "probe_timeout": 10,
    },
    # OpenClaw gateway + Caddy proxy removed (Phase 0)
]
_LAUNCHD_CHECK_INTERVAL = 45  # seconds between periodic launchd health checks
_LAST_LAUNCHD_CHECK_AT = 0.0
_prev_svc_status: dict = {}      # label -> bool(running); only log on state change
_LAST_KICKSTART_AT: dict = {}    # label -> float(timestamp); throttle repeated kickstarts


def _kill_existing_daemons() -> int:
    """
    Kill ALL other daemon.py processes before starting.
    Returns the number of processes killed.
    This is the nuclear option — ensures no zombie daemons survive.
    """
    my_pid = os.getpid()
    killed = 0
    try:
        result = subprocess.run(
            ["pgrep", "-f", "daemon\\.py"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.strip().split("\n"):
            pid_str = line.strip()
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            if pid == my_pid:
                continue
            # Verify it's actually a daemon.py process (not some other script matching the pattern)
            try:
                cmd_result = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "args="],
                    capture_output=True, text=True, timeout=3,
                )
                cmdline = cmd_result.stdout.strip()
                if "daemon.py" not in cmdline:
                    continue
                # Don't kill Claude Code or other tools that might have "daemon.py" in args
                if "claude" in cmdline.lower() or "grep" in cmdline.lower() or "pgrep" in cmdline.lower():
                    continue
            except Exception:
                continue  # Skip if we can't verify

            logger.warning("🔪 Killing existing daemon.py process PID %d", pid)
            try:
                os.kill(pid, signal.SIGTERM)
                # Give it 2 seconds to clean up gracefully
                for _ in range(20):
                    time.sleep(0.1)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                else:
                    # Still alive after 2s — force kill
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                killed += 1
            except ProcessLookupError:
                pass
            except PermissionError:
                logger.warning("⚠️ No permission to kill PID %d", pid)
    except Exception as e:
        logger.warning("⚠️ _kill_existing_daemons failed: %s", e)
    return killed


def _acquire_singleton_lock():
    """
    Ensure only one daemon instance runs at a time.
    Prevents duplicate service startup and port conflicts.

    Strategy (belt-and-suspenders):
    1. Kill ALL other daemon.py processes first (nuclear option)
    2. Acquire flock on lock file
    3. Write our PID to the lock file
    """
    global _DAEMON_LOCK_HANDLE

    # Step 1: Kill any existing daemon.py processes unconditionally
    n_killed = _kill_existing_daemons()
    if n_killed:
        logger.info("🧹 Killed %d existing daemon process(es) before starting.", n_killed)
        time.sleep(1)  # Allow ports/resources to be released

    # Step 2: Acquire file lock
    try:
        _DAEMON_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = _DAEMON_LOCK_PATH.open("w", encoding="utf-8")
        file_lock(fh, exclusive=True, blocking=False)
        fh.seek(0)
        fh.truncate(0)
        fh.write(str(os.getpid()))
        fh.flush()
        _DAEMON_LOCK_HANDLE = fh

        def _cleanup_lock() -> None:
            try:
                file_unlock(fh)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass

        atexit.register(_cleanup_lock)
        return True
    except BlockingIOError:
        # This should be extremely rare now since we killed all daemons above.
        # But handle it defensively.
        try:
            old_pid = int(_DAEMON_LOCK_PATH.read_text().strip())
            os.kill(old_pid, 0)
            logger.warning("⚠️ Another daemon instance is STILL active (PID %d) after kill attempt. Exiting.", old_pid)
            return False
        except (ProcessLookupError, ValueError, PermissionError, FileNotFoundError):
            # Stale lock — delete and retry
            logger.warning("🧹 Stale daemon lock found — cleaning up...")
            try:
                _DAEMON_LOCK_PATH.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                fh = _DAEMON_LOCK_PATH.open("w", encoding="utf-8")
                file_lock(fh, exclusive=True, blocking=False)
                fh.seek(0); fh.truncate(0); fh.write(str(os.getpid())); fh.flush()
                _DAEMON_LOCK_HANDLE = fh
                atexit.register(lambda: (file_unlock(fh), fh.close()))
                logger.info("✅ Daemon lock acquired after cleanup.")
                return True
            except Exception:
                logger.warning("⚠️ Cannot acquire lock even after cleanup. Exiting.")
                return False
    except Exception as e:
        # CRITICAL FIX: Previously this returned True, allowing duplicate daemons!
        logger.error("❌ Failed to acquire daemon singleton lock: %s — refusing to start.", e)
        return False

def _load_dotenv(dotenv_path: str, *, override: bool = True) -> None:
    """
    Load key=value pairs from a .env file into os.environ so child processes inherit.
    Keep it dependency-free (no python-dotenv).
    """
    p = Path(dotenv_path).expanduser().resolve()
    if not p.exists():
        return
    try:
        for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.lower().startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = (k or "").strip()
            if not k:
                continue
            v = (v or "").strip()
            if (len(v) >= 2) and ((v[0] == v[-1]) and v[0] in {"'", '"'}):
                v = v[1:-1]
            if (not override) and (k in os.environ):
                continue
            os.environ[k] = v
    except Exception as e:
        logger.warning(f"⚠️ Failed to load .env: {e}")

def _script_target_from_command(command: str) -> str:
    c = (command or "").strip()
    targets = [
        "api/server.py",
        "api/discord_bot.py",
        "api/tools_api.py",
        "skills/ops/heartbeat.py",
    ]
    for t in targets:
        if t in c:
            return t
    return ""


_SERVICE_PORT_MAP = {
    "Server": 5002,
    "ToolsAPI": 5003,
}

def _kill_port_occupier(port):
    """Kill any process occupying the given port."""
    try:
        result = subprocess.run(["lsof", "-ti", f":{port}"],
                                capture_output=True, text=True, timeout=5)
        pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip().isdigit()]
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        if pids:
            time.sleep(0.5)
        return bool(pids)
    except Exception:
        return False

def start_process(name, command):
    """Starts a subprocess and tracks it."""
    logger.info(f"🚀 Starting {name}...")
    try:
        # 如果是重啟（processes 裡已有此 name），先正常停掉舊的
        with _processes_lock:
            already_exists = name in processes
        if already_exists:
            stop_process(name)
            time.sleep(0.5)

        # 確認 port 沒被佔用（只檢查，不盲殺）
        port = _SERVICE_PORT_MAP.get(name)
        if port:
            try:
                result = subprocess.run(["lsof", "-ti", f":{port}"],
                                        capture_output=True, text=True, timeout=3)
                pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip().isdigit()]
                # 只殺不是我們自己子程序的
                with _processes_lock:
                    our_pids = {str(info["proc"].pid) for info in processes.values() if info.get("proc")}
                alien_pids = [p for p in pids if p not in our_pids]
                if alien_pids:
                    for pid in alien_pids:
                        try:
                            os.kill(int(pid), signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
                    time.sleep(0.5)
                    logger.info(f"🧹 Cleared port {port} (alien PIDs: {alien_pids})")
            except Exception:
                pass

        # Pre-clean same script to enforce single instance.
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from skills.ops.process_guardian import force_kill_all
            target = _script_target_from_command(command)
            if target:
                force_kill_all(target)
        except Exception as e:
            logger.debug(f"Process Guardian pre-clean skipped for {name}: {e}")

        # Avoid shell wrapper process; track the real child process for stable monitoring.
        argv = shlex.split(command) if isinstance(command, str) else command
        proc = subprocess.Popen(
            argv,
            shell=False,
            start_new_session=True,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        with _processes_lock:
            processes[name] = {"proc": proc, "command": command, "started": time.time()}
        logger.info(f"✅ {name} started with PID {proc.pid}")
    except Exception as e:
        logger.error(f"❌ Failed to start {name}: {e}")

def _write_autopilot_kill_reason(pid: int, reason: str) -> None:
    """寫入 kill reason — 同時寫入統一日誌及 per-PID 檔（供 autopilot 讀取後刪除）。"""
    try:
        _store_autopilot_kill_reason(pid, reason, root=_MAGI_ROOT)
    except Exception:
        pass

def stop_process(name):
    """Stops a tracked subprocess."""
    with _processes_lock:
        rec = processes.get(name)
        if not rec:
            return
        proc = rec["proc"]
    logger.info(f"🛑 Stopping {name}...")
    try:
        _write_autopilot_kill_reason(proc.pid, f"daemon stop_process({name})：daemon 正在關閉或重啟程序")
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            logger.info(f"ℹ️ {name} process already exited before SIGTERM")
        else:
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                logger.warning(f"⚠️ {name} did not exit after SIGTERM, sending SIGKILL")
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait(timeout=5)
                except (ProcessLookupError, Exception):
                    pass
        with _processes_lock:
            processes.pop(name, None)
    except Exception as e:
        logger.error(f"⚠️ Error stopping {name}: {e}")

_restart_failures: Dict[str, int] = {}        # consecutive failure count
_restart_last_exit: Dict[str, float] = {}     # timestamp of last exit
_BACKOFF_BASE = 10          # initial backoff seconds
_BACKOFF_MAX  = 300         # cap at 5 minutes
_HEALTHY_THRESHOLD = 60     # if process ran > 60s, reset failure count
_MAX_CONSECUTIVE_FAILURES = 10  # stop restarting after this many rapid failures

# ── CronScheduler Fallback ──
# When Discord Bot is unavailable, daemon runs CronScheduler independently.
_cron_fallback_running = False
_cron_fallback_lock = threading.Lock()

def _start_cron_fallback() -> None:
    """Start CronScheduler as a daemon thread if not already running.
    This is activated when Discord Bot fails to start after MAX_CONSECUTIVE_FAILURES."""
    global _cron_fallback_running
    with _cron_fallback_lock:
        if _cron_fallback_running:
            return
        cron_enabled = os.environ.get("MAGI_INTERNAL_CRON_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
        if not cron_enabled:
            logger.info("⏸️ CronScheduler fallback skipped (MAGI_INTERNAL_CRON_ENABLED not set)")
            return
        _cron_fallback_running = True

    def _cron_loop():
        logger.info("⏰ CronScheduler fallback starting (Discord Bot unavailable)...")
        try:
            sys.path.insert(0, os.path.join(_MAGI_ROOT))
            from skills.ops.cron_scheduler import CronScheduler
            from api.orchestrator import Orchestrator
            scheduler = CronScheduler()
            orchestrator = Orchestrator()
            logger.info("⏰ CronScheduler fallback ready — executing jobs every 60s")
        except Exception as e:
            logger.error("❌ CronScheduler fallback init failed: %s", e)
            return

        while True:
            try:
                due_jobs = scheduler.check_due_jobs()
                for job in due_jobs:
                    command = job.get("command", "")
                    job_id = job.get("id", "?")
                    logger.info("⏰ [CronFallback] Executing job: %s", job_id)
                    try:
                        if command.startswith("@MAGI"):
                            clean_cmd = command.replace("@MAGI", "").strip()
                            response = orchestrator.process_message(
                                "SYSTEM_CRON", clean_cmd,
                                platform="DAEMON_CRON", role="admin",
                            )
                            if response:
                                try:
                                    orchestrator.record_assistant_reply("SYSTEM_CRON", response)
                                except Exception:
                                    pass
                                logger.info("⏰ [CronFallback] Job %s result (%d chars): %.200s",
                                            job_id, len(response), response)
                        else:
                            _SAFE_PREFIXES = ("cd ", "/Users/", "./venv/", "python3 ", "MAGI_", "JUDICIAL_")
                            if any(command.strip().startswith(p) for p in _SAFE_PREFIXES):
                                _shell_env = {**os.environ, "MAGI_PREFER_LOCAL_DB": "0", "MAGI_NO_DELETE": "1"}
                                # ===== R2: SafeProcess 分支 =====
                                _USE_SAFE_PROCESS = os.environ.get("MAGI_USE_SAFE_PROCESS", "0").strip().lower() in {"1", "true", "on", "yes"}
                                result_returncode = -1
                                result_stdout = ""
                                result_stderr = ""
                                if _USE_SAFE_PROCESS:
                                    try:
                                        from api.platforms.safe_process import parse_cron_command, run as _safe_run
                                        argv = parse_cron_command(command)
                                        _sr = _safe_run(argv, timeout_sec=600, cwd=_MAGI_ROOT)
                                        result_returncode = _sr.returncode
                                        result_stdout = _sr.stdout
                                        result_stderr = _sr.stderr
                                    except Exception as _e:
                                        logger.error("[SafeProcess] fallback to legacy shell path: %s", _e)
                                        _USE_SAFE_PROCESS = False
                                if not _USE_SAFE_PROCESS:
                                    # --- legacy 原樣保留，不要刪 ---
                                    result = subprocess.run(
                                        command, shell=True, capture_output=True, text=True,
                                        cwd=_MAGI_ROOT, env=_shell_env, timeout=600,
                                    )
                                    result_returncode = result.returncode
                                    result_stdout = result.stdout
                                    result_stderr = result.stderr
                                # ===== R2 end =====
                                if result_returncode != 0:
                                    logger.warning("⚠️ [CronFallback] Shell job %s exited %d: %s",
                                                   job_id, result_returncode, (result_stderr or "")[:300])
                                else:
                                    logger.info("✅ [CronFallback] Shell job %s completed OK", job_id)
                            else:
                                logger.warning("⚠️ [CronFallback] Blocked suspicious command: %s", command[:80])
                    except Exception as je:
                        logger.error("⚠️ [CronFallback] Job %s failed: %s", job_id, je)
            except Exception as loop_err:
                logger.error("⚠️ [CronFallback] Loop error: %s", loop_err)
            time.sleep(60)

    t = threading.Thread(target=_cron_loop, name="CronFallback", daemon=True)
    t.start()
    logger.info("⏰ CronScheduler fallback thread started")


# ── Coordinated restart: processes sharing the same Orchestrator module ──
# When ANY of these dies/restarts, ALL must restart to avoid stale module caches.
# (Each has its own Orchestrator() instance loaded from the same .py files.)
_ORCHESTRATOR_GROUP = {"Server", "Discord", "ToolsAPI"}

_last_coordinated_restart: float = 0.0
_COORDINATED_RESTART_COOLDOWN = 30.0       # 日間 cooldown
_COORDINATED_RESTART_COOLDOWN_NIGHT = 180.0  # 夜間 cooldown（保護夜間任務）


def _clear_pycache():
    """Clear __pycache__ directories under api/ and skills/bridge/ to avoid stale .pyc."""
    import shutil
    for subdir in ("api", "api/handlers", "skills/bridge", "skills/memory",
                   "skills/ops", "casper_ecosystem/law_firm_orchestrators"):
        cache_dir = os.path.join(_MAGI_ROOT, subdir, "__pycache__")
        if os.path.isdir(cache_dir):
            try:
                shutil.rmtree(cache_dir)
            except Exception:
                pass


def _coordinated_restart(trigger_name: str):
    """Restart all Orchestrator-group processes together.

    夜間 (02:00-07:00) 行為改變：
    - cooldown 拉長到 180 秒（避免連鎖重啟干擾夜間任務）
    - 只重啟掛掉的那個服務，不連帶殺其他服務
    """
    global _last_coordinated_restart
    now = time.time()
    night = _is_night_window()
    cooldown = _COORDINATED_RESTART_COOLDOWN_NIGHT if night else _COORDINATED_RESTART_COOLDOWN

    if now - _last_coordinated_restart < cooldown:
        logger.info(f"⏳ Coordinated restart cooldown — skipping (triggered by {trigger_name})")
        return
    _last_coordinated_restart = now

    if night:
        # ── 夜間模式：只重啟掛掉的服務，不連帶殺其他 ──
        logger.info(f"🌙 Night mode: restarting only {trigger_name} (not full group)")
        with _processes_lock:
            rec = processes.get(trigger_name)
            cmd = rec.get("command") if rec else None
        if cmd:
            start_process(trigger_name, cmd)
        return

    # ── 日間模式：完整協調重啟 ──
    logger.info(f"🔄 Coordinated restart triggered by {trigger_name} — restarting {_ORCHESTRATOR_GROUP}")
    _clear_pycache()

    # Save commands BEFORE stopping (stop_process deletes from processes dict)
    saved_commands: Dict[str, str] = {}
    with _processes_lock:
        for name in _ORCHESTRATOR_GROUP:
            rec = processes.get(name)
            if rec and rec.get("command"):
                saved_commands[name] = rec["command"]

    # Stop all group members (except the trigger, which is already dead)
    for name in _ORCHESTRATOR_GROUP:
        if name == trigger_name:
            continue
        with _processes_lock:
            has_proc = name in processes and processes[name].get("proc")
        if has_proc:
            try:
                stop_process(name)
            except Exception as e:
                logger.warning(f"⚠️ Coordinated stop {name} failed: {e}")
    time.sleep(1)

    # Start all group members using saved commands
    for name in _ORCHESTRATOR_GROUP:
        cmd = saved_commands.get(name)
        if cmd:
            start_process(name, cmd)
            time.sleep(1)  # stagger to avoid port race


def monitor_processes():
    """Checks if processes are alive and restarts them with exponential backoff."""
    now = time.time()
    with _processes_lock:
        snapshot = list(processes.items())
    for name, rec in snapshot:
        proc = rec.get("proc")
        command = rec.get("command")
        if not proc:
            continue
        if proc.poll() is not None:
            if not command:
                continue

            # Calculate how long the process was alive
            started = rec.get("started", 0)
            uptime = now - started if started else 0

            with _processes_lock:
                if uptime > _HEALTHY_THRESHOLD:
                    # Process ran long enough — was healthy, reset counter
                    _restart_failures[name] = 0
                else:
                    _restart_failures[name] = _restart_failures.get(name, 0) + 1

                failures = _restart_failures[name]

            if failures >= _MAX_CONSECUTIVE_FAILURES:
                logger.critical(
                    f"🛑 {name} failed {failures} times consecutively — "
                    f"giving up auto-restart. Fix the issue and restart manually."
                )
                with _processes_lock:
                    cur = processes.get(name)
                    if cur is rec:
                        rec["proc"] = None  # stop monitoring
                # If Discord Bot gave up, activate CronScheduler fallback
                if name == "Discord":
                    try:
                        _start_cron_fallback()
                    except Exception as cron_err:
                        logger.error("❌ CronScheduler fallback activation failed: %s", cron_err)
                continue

            with _processes_lock:
                backoff = min(_BACKOFF_BASE * (2 ** (failures - 1)), _BACKOFF_MAX) if failures > 0 else 0
                last_exit = _restart_last_exit.get(name, 0)
                wait_until = last_exit + backoff
                _restart_last_exit[name] = now

            if now < wait_until:
                remaining = int(wait_until - now)
                logger.warning(
                    f"⏳ {name} died (exit={proc.returncode}, "
                    f"failures={failures}). Backoff {remaining}s remaining."
                )
                continue

            logger.warning(
                f"⚠️ {name} has died (exit={proc.returncode}, "
                f"failures={failures}, backoff={int(backoff)}s). Restarting..."
            )

            # WebsiteAdmin special guard: skip restart if port already bound
            # (orphan child from previous daemon still alive)
            if name == "WebsiteAdmin":
                import socket as _sock
                _wa_p = int(os.environ.get("WEBSITE_ADMIN_PORT", "8088"))
                try:
                    with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as _s:
                        _s.settimeout(0.3)
                        if _s.connect_ex(("127.0.0.1", _wa_p)) == 0:
                            logger.info(f"ℹ️ {name} port {_wa_p} still bound — skip restart")
                            with _processes_lock:
                                cur = processes.get(name)
                                if cur is rec:
                                    rec["proc"] = None
                            continue
                except Exception:
                    pass

            # Orchestrator-group: coordinated restart to keep modules in sync
            if name in _ORCHESTRATOR_GROUP:
                _coordinated_restart(name)
            else:
                start_process(name, command)


def _iter_ps_rows() -> list[Dict[str, Any]]:
    rows: list[Dict[str, Any]] = []

    def _parse_etime_to_sec(s: str) -> int:
        """
        macOS ps etime formats:
        - MM:SS
        - HH:MM:SS
        - DD-HH:MM:SS
        """
        t = (s or "").strip()
        if not t:
            return 0
        m = re.match(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$", t)
        if not m:
            return 0
        dd = int(m.group(1) or 0)
        hh = int(m.group(2) or 0)
        mm = int(m.group(3) or 0)
        ss = int(m.group(4) or 0)
        return (dd * 86400) + (hh * 3600) + (mm * 60) + ss

    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
        ).stdout or ""
    except Exception:
        return rows

    for raw in out.splitlines():
        line = (raw or "").strip()
        if not line:
            continue
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            etimes = _parse_etime_to_sec(parts[2])
        except Exception:
            continue
        rows.append({"pid": pid, "ppid": ppid, "etimes": etimes, "cmd": parts[3]})
    return rows


def _is_worker_cmd(cmd: str) -> bool:
    s = (cmd or "")
    return any(m in s for m in _WORKER_MARKERS)


def _is_core_allowed_cmd(cmd: str) -> bool:
    s = (cmd or "")
    return any(m in s for m in _CORE_ALLOW_MARKERS)


def _grace_for_cmd(cmd: str) -> int:
    s = (cmd or "")
    for marker, sec in _ORPHAN_GRACE_BY_MARKER.items():
        if marker in s:
            return max(60, int(sec))
    return max(60, _ORPHAN_GRACE_SEC)


def _kill_pid_tree(pid: int) -> bool:
    """Kill a process and all its descendants (important for chromedriver → Chrome trees)."""
    try:
        import psutil
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            try:
                child.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        parent.terminate()
        # Wait briefly then force kill survivors
        _, alive = psutil.wait_procs(children + [parent], timeout=3)
        for p in alive:
            try:
                p.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return True
    except ImportError:
        # Fallback: process group kill
        try:
            pgid = os.getpgid(pid)
            os.killpg(pgid, signal.SIGTERM)
            return True
        except Exception:
            pass
    except Exception:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except Exception:
        return False


def _write_state(extra: Dict[str, Any] | None = None) -> None:
    try:
        payload = dict(_STATE)
        payload["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        if extra:
            payload.update(extra)
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE_PATH.with_suffix(_STATE_PATH.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_STATE_PATH)
    except Exception:
        pass


def _snapshot_counts(rows: list[Dict[str, Any]] | None = None) -> Dict[str, int]:
    if rows is None:
        rows = _iter_ps_rows()
    core = 0
    worker = 0
    orphan = 0
    for r in rows:
        cmd = str(r.get("cmd") or "")
        ppid = int(r.get("ppid") or 0)
        if _is_core_allowed_cmd(cmd):
            core += 1
        if _is_worker_cmd(cmd):
            worker += 1
            if ppid == 1:
                orphan += 1
    return {"core_count": core, "worker_count": worker, "orphan_worker_count": orphan}


def reap_orphan_workers(*, force: bool = False, dry_run: bool = False) -> str:
    """
    統一殭屍清理（Unified Process Reaper）。
    這是 MAGI 唯一的進程清理入口（oMLX watchdog 和 port cleanup 除外）。

    清理四大類：
    1. OS-level zombie（STATUS_ZOMBIE）→ SIGKILL
    2. 已知殭屍模式（test scripts 等）→ SIGTERM
    3. 孤兒 worker（PPID=1, 超過 grace）→ kill tree
    4. Stale 非保護 Python 進程（>30min）→ SIGTERM

    Args:
        force: True = 啟動時強制掃描（不等 interval、不限 PPID）
        dry_run: True = 只報告不殺
    Returns:
        格式化報告字串
    """
    global _LAST_REAP_AT
    now = time.time()
    with _timing_lock:
        if (not force) and (not dry_run) and (now - _LAST_REAP_AT < max(10, _REAP_INTERVAL_SEC)):
            return ""
        _LAST_REAP_AT = now

    try:
        ctrl_path = Path(f"{_MAGI_ROOT}/static/guardian_control.json")
        if ctrl_path.exists():
            ctrl = json.loads(ctrl_path.read_text(encoding="utf-8"))
            if not ctrl.get("enabled", True) and not force:
                _STATE["reap"]["status"] = "已透過網頁介面停用 (Disabled)"
                _write_state(_snapshot_counts())  # no rows yet, let it fetch
                return "⏸️ Reaper 已停用"
    except Exception:
        pass

    _STATE["reap"]["status"] = "運作中 (Enabled)"

    managed_pids = {os.getpid()}
    try:
        managed_pids.add(os.getppid())
    except Exception:
        pass
    with _processes_lock:
        for rec in processes.values():
            p = rec.get("proc")
            if p and getattr(p, "pid", None):
                managed_pids.add(int(p.pid))

    killed: list[tuple[int, int, int, str, str]] = []  # (pid, ppid, age, cmd, reason)
    spared: list[str] = []

    # ── Phase 1: OS-level zombies (psutil) ──
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "status"]):
            try:
                if proc.info["status"] == psutil.STATUS_ZOMBIE:
                    zpid = proc.info["pid"]
                    if zpid in managed_pids:
                        continue
                    if not dry_run:
                        try:
                            os.kill(zpid, signal.SIGKILL)
                            killed.append((zpid, 0, 0, "(zombie)", "OS_ZOMBIE"))
                        except OSError:
                            pass
                    else:
                        spared.append(f"🧟 OS zombie PID {zpid}")
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except ImportError:
        pass

    # ── Phase 2-4: ps-based scan ──
    ps_rows = _iter_ps_rows()
    for row in ps_rows:
        pid = int(row["pid"])
        ppid = int(row["ppid"])
        etimes = int(row["etimes"])
        cmd = str(row["cmd"])

        if pid in managed_pids:
            continue

        # Never-kill check (統一白名單)
        if _is_core_allowed_cmd(cmd):
            continue

        # Phase 2: 已知殭屍模式 — 不論 age 直接殺
        if any(pat in cmd for pat in REAPER_ZOMBIE_PATTERNS):
            reason = "ZOMBIE_PATTERN"
            if not dry_run:
                _write_autopilot_kill_reason(pid, f"reaper: 匹配殭屍模式")
                if _kill_pid_tree(pid):
                    killed.append((pid, ppid, etimes, cmd, reason))
            else:
                spared.append(f"🔪 {reason} PID {pid} age={etimes}s — {cmd[:60]}")
            continue

        # Phase 3: 孤兒 worker（原有邏輯）
        if _is_worker_cmd(cmd):
            if (not force) and ppid != 1:
                continue
            if (not force) and etimes < _grace_for_cmd(cmd):
                continue
            reason = "ORPHAN_EXPIRED"
            if not dry_run:
                _write_autopilot_kill_reason(
                    pid,
                    f"reaper: 孤兒程序(PPID={ppid})已執行 {etimes}s，超過寬限期 {_grace_for_cmd(cmd)}s",
                )
                if _kill_pid_tree(pid):
                    killed.append((pid, ppid, etimes, cmd, reason))
            else:
                spared.append(f"⏰ {reason} PID {pid} age={etimes}s — {cmd[:60]}")
            continue

        # Phase 4: Stale 非保護 Python 進程（>30min, PPID=1）
        is_python = "python" in cmd.lower()
        if is_python and ppid == 1 and etimes > 1800:
            # 安全工具排除
            if any(safe in cmd for safe in REAPER_SAFE_UTILITIES):
                continue
            # Skip PIDs already attempted in a previous reaper cycle
            if pid in _REAPER_ZOMBIE_SEEN:
                continue
            reason = "STALE_UNPROTECTED"
            if not dry_run:
                _write_autopilot_kill_reason(pid, f"reaper: stale Python 進程已執行 {etimes}s")
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed.append((pid, ppid, etimes, cmd, reason))
                except OSError:
                    pass
                _REAPER_ZOMBIE_SEEN.add(pid)
            else:
                spared.append(f"⏰ {reason} PID {pid} age={etimes}s — {cmd[:60]}")

    # ── Logging & State ──
    if killed:
        for pid, ppid, etimes, cmd, reason in killed[:10]:
            logger.warning(
                "🧹 REAPER kill pid=%s reason=%s age=%ss ppid=%s cmd=%s",
                pid, reason, etimes, ppid, cmd[:220]
            )
        if len(killed) > 10:
            logger.warning("🧹 REAPER total killed=%s", len(killed))
    _STATE["reap"]["total_killed"] = int(_STATE["reap"].get("total_killed") or 0) + len(killed)
    _STATE["reap"]["last_reap_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _STATE["reap"]["last_reap_killed"] = len(killed)
    _STATE["reap"]["last_reap_force"] = bool(force)
    _STATE["reap"]["last_reaped"] = [
        {"pid": int(pid), "ppid": int(ppid), "age_sec": int(etimes), "cmd": str(cmd)[:240], "reason": reason}
        for pid, ppid, etimes, cmd, reason in killed[:20]
    ]
    _write_state(_snapshot_counts(rows=ps_rows))

    # Prune _REAPER_ZOMBIE_SEEN: remove PIDs no longer in the process table
    _live_pids = {int(r["pid"]) for r in ps_rows if isinstance(r, dict)} if ps_rows else set()
    _REAPER_ZOMBIE_SEEN.intersection_update(_live_pids)

    # ── Build report ──
    return _build_reap_report(killed, spared, dry_run)


def _build_reap_report(
    killed: list[tuple[int, int, int, str, str]],
    spared: list[str],
    dry_run: bool,
) -> str:
    """格式化 reaper 報告。"""
    if not killed and not spared:
        return "✅ **殭屍巡邏完成** — 系統乾淨。"

    lines = ["🛡️ **殭屍巡邏報告** (Unified Reaper)"]
    if dry_run:
        lines.append("⚠️ *模擬模式 — 未實際清除*\n")

    if killed:
        lines.append(f"🔪 **已清除 {len(killed)} 個程序：**")
        for pid, ppid, etimes, cmd, reason in killed[:15]:
            lines.append(f"   ⏰ [{reason}] PID {pid} (age: {etimes // 60}m) - {cmd[:60]}")

    if spared:
        lines.append(f"\n⚠️ **偵測到 {len(spared)} 個可疑程序（未清除）：**")
        for s in spared[:10]:
            lines.append(f"   {s}")

    lines.append(f"\n✅ 巡邏完成。共清除 {len(killed)} 個程序。")
    return "\n".join(lines)


def get_reap_report() -> str:
    """取得最近一次 reaper 的狀態報告。"""
    reap = _STATE.get("reap", {})
    reaped = reap.get("last_reaped", [])
    total = reap.get("total_killed", 0)
    last_at = reap.get("last_reap_at", "N/A")
    last_n = reap.get("last_reap_killed", 0)
    status = reap.get("status", "unknown")

    if last_n == 0 and not reaped:
        return f"✅ **殭屍巡邏完成** — 系統乾淨。\n狀態：{status} | 累計清除：{total} | 最後巡邏：{last_at}"

    lines = [f"🛡️ **殭屍巡邏報告** (Unified Reaper)"]
    if reaped:
        lines.append(f"🔪 **本次清除 {last_n} 個程序：**")
        for r in reaped[:10]:
            reason = r.get("reason", "UNKNOWN")
            lines.append(f"   ⏰ [{reason}] PID {r['pid']} (age: {r['age_sec'] // 60}m) - {r['cmd'][:60]}")
    lines.append(f"\n狀態：{status} | 累計清除：{total} | 最後巡邏：{last_at}")
    return "\n".join(lines)


def request_kill(marker: str, reason: str, *, max_age_sec: int = 0) -> list[int]:
    """
    供其他模組呼叫的統一 kill 介面。
    掃描匹配 marker 的進程，尊重 REAPER_NEVER_KILL。

    Args:
        marker: 在 command line 中搜尋的字串
        reason: 殺掉原因（寫入 log）
        max_age_sec: >0 時只殺超過此秒數的進程

    Returns:
        被殺的 PID 列表
    """
    killed_pids: list[int] = []
    my_pid = os.getpid()
    for row in _iter_ps_rows():
        pid = int(row["pid"])
        cmd = str(row["cmd"])
        etimes = int(row["etimes"])
        if pid == my_pid:
            continue
        if marker not in cmd:
            continue
        if _is_core_allowed_cmd(cmd):
            continue
        if max_age_sec > 0 and etimes < max_age_sec:
            continue
        _write_autopilot_kill_reason(pid, f"reaper request_kill: {reason}")
        if _kill_pid_tree(pid):
            killed_pids.append(pid)
            logger.warning(
                "🧹 REAPER kill pid=%s reason=LOCAL_REQUEST(%s) age=%ss cmd=%s",
                pid, reason, etimes, cmd[:200],
            )
    return killed_pids

def _launchd_target(label: str) -> str:
    """Build launchd target path: gui/<uid>/<label>."""
    return f"gui/{os.getuid()}/{label}"


def _is_launchd_service_running(label: str) -> bool:
    """Check if a launchd service is currently loaded and running."""
    try:
        result = subprocess.run(
            ["launchctl", "print", _launchd_target(label)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _kickstart_launchd_service(label: str) -> bool:
    """Kickstart a launchd service if it's not running."""
    try:
        result = subprocess.run(
            ["launchctl", "kickstart", "-k", _launchd_target(label)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            _LAST_KICKSTART_AT[label] = time.time()
            return True
        return False
    except Exception as e:
        logger.warning(f"⚠️ launchctl kickstart failed for {label}: {e}")
        return False


def _probe_url(url: str, timeout: float = 3.0) -> bool:
    """Quick HTTP probe — returns True if we get any 2xx/3xx response."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def _ensure_launchd_services(*, startup: bool = False) -> None:
    """
    Ensure all launchd-managed infrastructure services are running.
    On startup: kickstart + wait with health probe.
    Periodic: quick check + kickstart if needed.
    """
    global _prev_svc_status
    for svc in _LAUNCHD_SERVICES:
        label = svc["label"]
        name = svc["name"]
        probe_url = svc.get("probe_url")
        probe_timeout = svc.get("probe_timeout", 30)

        # Training lock: 訓練進行中跳過 oMLX 相關服務檢查
        if _is_training_locked() and label in ("com.magi.omlx", "com.magi.omlx-watchdog"):
            if startup:
                logger.info(f"🔒 {name} skipped — training lock active")
            continue

        running = _is_launchd_service_running(label)
        prev_running = _prev_svc_status.get(label)
        state_changed = (prev_running is None) or (prev_running != running)
        _prev_svc_status[label] = running

        if running and not startup:
            # Periodic check: service is there, quick HTTP probe if configured
            if probe_url:
                grace = svc.get("kickstart_grace", 0)
                since_kick = time.time() - _LAST_KICKSTART_AT.get(label, 0)
                if grace and since_kick < grace:
                    # Still within startup grace window — don't probe-kickstart (E4B needs ~120s)
                    logger.debug(f"⏳ {name} — in kickstart grace ({int(since_kick)}s / {grace}s), skipping probe")
                elif not _probe_url(probe_url, timeout=4.0):
                    logger.warning(f"⚠️ {name} ({label}) launchd ok but HTTP probe failed — kickstarting")
                    if not _kickstart_launchd_service(label):
                        logger.warning(f"⚠️ {name} ({label}) kickstart after probe failure also failed")
            if state_changed:
                logger.info(f"✅ {name} ({label}) recovered — now running")
            continue

        if not running:
            if state_changed or startup:
                logger.info(f"🔧 {name} ({label}) not running — kickstarting...")
            else:
                logger.debug(f"🔧 {name} ({label}) still not running — kickstarting...")
            if not _kickstart_launchd_service(label):
                logger.warning(f"⚠️ {name} ({label}) kickstart failed")
                # oMLX: 不做 direct Popen fallback（會產生 launchd 管不到的孤兒進程）
                # 讓 omlx_watchdog.sh 負責重試，daemon 只負責 launchd kickstart
                continue

        # If startup mode and there's a probe URL, wait for the service to be ready
        # startup_probe=False means: kickstart if needed but don't block startup waiting for it
        if startup and probe_url and svc.get("startup_probe", True):
            deadline = time.time() + probe_timeout
            ready = False
            while time.time() < deadline:
                if _probe_url(probe_url, timeout=3.0):
                    ready = True
                    break
                time.sleep(2)
            if ready:
                logger.info(f"✅ {name} — ready")
            else:
                logger.warning(f"⏳ {name} — not reachable after {probe_timeout}s (will keep trying in background)")
        elif startup:
            # No probe URL — just check the process is there after a short grace period
            time.sleep(2)
            if _is_launchd_service_running(label):
                logger.info(f"✅ {name} — running")
            else:
                logger.warning(f"⚠️ {name} — process not detected after kickstart")


def _periodic_launchd_check() -> None:
    """Periodically verify launchd services are still alive (called from monitor loop)."""
    global _LAST_LAUNCHD_CHECK_AT
    now = time.time()
    if now - _LAST_LAUNCHD_CHECK_AT < _LAUNCHD_CHECK_INTERVAL:
        return
    _LAST_LAUNCHD_CHECK_AT = now
    try:
        _ensure_launchd_services(startup=False)
    except Exception as e:
        logger.warning(f"⚠️ Periodic launchd check failed: {e}")


def cleanup(signum, frame):
    """Graceful shutdown."""
    logger.info("🔻 Daemon shutting down...")
    with _processes_lock:
        names = list(processes.keys())
    for name in names:
        stop_process(name)
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    if not _acquire_singleton_lock():
        sys.exit(0)
    
    logger.info("👿 MAGI Daemon Started.")

    # ── First-run detection: launch Setup Wizard if .env is missing/incomplete ──
    _env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    _needs_setup = False
    if not os.path.exists(_env_file):
        _needs_setup = True
    else:
        # Quick check: are required vars actually filled in?
        # P0-05: 只檢查核心必填，通道 credentials 不阻止啟動
        _required_keys = ["DB_HOST", "DB_USER", "DB_PASSWORD", "FLASK_SECRET_KEY"]
        with open(_env_file, encoding="utf-8") as _ef:
            _env_text = _ef.read()
        for _k in _required_keys:
            import re as _re_mod
            _m = _re_mod.search(rf'^{_k}=(.*)$', _env_text, _re_mod.MULTILINE)
            if not _m or not _m.group(1).strip() or _m.group(1).strip().startswith("your_"):
                _needs_setup = True
                break
    if _needs_setup:
        logger.info("🧙 First-time setup detected — launching Setup Wizard...")
        print("\n" + "=" * 50)
        print("  MAGI 首次啟動 — 正在開啟設定精靈...")
        print("  Opening Setup Wizard in your browser...")
        print("=" * 50 + "\n")
        _wizard_proc = subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "setup_wizard.py")],
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        try:
            _wizard_proc.wait(timeout=300)
        except subprocess.TimeoutExpired:
            logger.warning("⏰ Setup Wizard timed out after 5 minutes — killing it.")
            _wizard_proc.kill()
            _wizard_proc.wait()
        # Re-check after wizard
        if not os.path.exists(_env_file):
            logger.error("❌ Setup Wizard completed but .env not found. Cannot start MAGI.")
            sys.exit(1)
        logger.info("✅ Setup Wizard completed — continuing startup.")

    # Load MAGI .env so admin allowlist + tokens are available to all subprocesses.
    _load_dotenv(_env_file, override=True)

    # 0-pre. Resolve keychain: prefixed env vars (macOS Keychain integration)
    if IS_MACOS:
        try:
            from skills.ops.keychain_manager import resolve_env_keychain
            _kc_resolved = resolve_env_keychain()
            if _kc_resolved:
                logger.info("🔑 Keychain: resolved %d env vars", len(_kc_resolved))
        except Exception as e:
            logger.debug("Keychain resolution skipped: %s", e)

    # 0. Clean __pycache__ to ensure fresh bytecode for all processes
    _clear_pycache()
    logger.info("🧹 Cleared __pycache__ for fresh startup")

    # 0. Process Guardian: Clean up orphans before starting
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from skills.ops.process_guardian import force_kill_all
        logger.info("🛡️ Process Guardian: Cleaning up orphans...")
        force_kill_all("api/server.py")
        force_kill_all("api/discord_bot.py")
        force_kill_all("api/tools_api.py")
        # openclaw_cron_runner removed (Phase 0)
        force_kill_all("skills/ops/file_review_auto_worker.py")
        force_kill_all("skills/ops/heartbeat.py")
        # Kill orphaned FAISS rebuild subprocesses (leak from previous server runs)
        force_kill_all("MEMORY_ENABLE_FAISS")
        # Kill orphaned Selenium/Chrome sessions
        force_kill_all("chromedriver")
    except Exception as e:
        logger.warning(f"⚠️ Process Guardian cleanup failed: {e}")
    # 0.5 Runtime hygiene: reap stale/orphan worker tasks from old runs.
    try:
        reap_orphan_workers(force=True)
    except Exception as e:
        logger.warning(f"⚠️ Initial stale-worker reap failed: {e}")

    # 0.6 Message queue: recover stale messages from previous crash & cleanup old
    try:
        from skills.memory.message_queue import get_queue as _get_mq
        _mq = _get_mq()
        _mq_recovered = _mq.recover_stale(stale_seconds=300)
        if _mq_recovered:
            logger.info("Message queue: recovered %d stale messages", _mq_recovered)
        _mq.cleanup_old(days=7)
    except Exception as _mq_err:
        logger.warning(f"⚠️ Message queue startup recovery failed: {_mq_err}")

    # 0.7 Infrastructure services: ensure oMLX, OpenClaw Gateway, Caddy are alive
    if IS_MACOS:
        logger.info("🔌 Ensuring launchd infrastructure services are running...")
        try:
            _ensure_launchd_services(startup=True)
        except Exception as e:
            logger.warning(f"⚠️ Launchd infrastructure check failed (non-fatal): {e}")
    
    # Resolve venv python path dynamically (cross-platform)
    _PYTHON = get_venv_python()

    # 0.9 Port cleanup: kill any process occupying our ports before starting services.
    #     This prevents "Address already in use" cascading failures.
    _SERVICE_PORTS = [5002, 5003, 8088]
    for _port in _SERVICE_PORTS:
        try:
            result = subprocess.run(
                ["lsof", "-ti", f":{_port}"],
                capture_output=True, text=True, timeout=5,
            )
            pids = [p.strip() for p in result.stdout.strip().split("\n") if p.strip().isdigit()]
            if pids:
                logger.warning(f"🧹 Port {_port} occupied by PID(s) {pids} — killing before startup")
                for pid in pids:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                time.sleep(0.5)
        except Exception as e:
            logger.debug(f"Port {_port} cleanup skipped: {e}")

    # 0.5 DB Failover Monitor（遠端 DB 不通自動切本機）
    try:
        from api.db_failover import start_failover_monitor
        start_failover_monitor()
        logger.info("✅ DB Failover Monitor started")
    except Exception as e:
        logger.warning(f"⚠️ DB Failover Monitor not started: {e}")

    # 1. Start Server (LINE API)
    start_process("Server", f"{_PYTHON} api/server.py")

    # 2. Start Discord Bot
    start_process("Discord", f"{_PYTHON} api/discord_bot.py")

    # 2.5 Start Tools API (external routes / connections checks)
    start_process("ToolsAPI", f"{_PYTHON} api/tools_api.py")

    # 2.55 oMLX 三哲人審查員（Phi-4 + SmolLM3）日間自動啟動
    # 夜間模式由 omlx_switch_model.sh night 負責 bootout
    if not _is_night_window():
        try:
            _uid = os.getuid()
            _phi4_plist = os.path.expanduser("~/Library/LaunchAgents/com.magi.omlx-phi4.plist")
            _smol_plist = os.path.expanduser("~/Library/LaunchAgents/com.magi.omlx-smol.plist")
            for _label, _plist, _port in [
                ("com.magi.omlx-phi4", _phi4_plist, 8082),
                ("com.magi.omlx-smol", _smol_plist, 8083),
            ]:
                if not os.path.exists(_plist):
                    logger.warning("oMLX reviewer plist not found: %s", _plist)
                    continue
                # 檢查是否已在線
                import socket as _sock
                _already_up = False
                try:
                    with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as _s:
                        _s.settimeout(1)
                        _already_up = _s.connect_ex(("127.0.0.1", _port)) == 0
                except Exception:
                    pass
                if _already_up:
                    logger.info("oMLX reviewer %s already on port %d — skip", _label, _port)
                    continue
                # bootout（忽略錯誤）→ bootstrap → kickstart
                os.system(f'launchctl bootout gui/{_uid}/{_label} 2>/dev/null')
                os.system(f'launchctl bootstrap gui/{_uid} {_plist} 2>/dev/null')
                os.system(f'launchctl kickstart -kp gui/{_uid}/{_label}')
                logger.info("✅ oMLX reviewer %s kicked on port %d", _label, _port)
            logger.info("✅ 三哲人審查員啟動完成（日間模式）")
        except Exception as e:
            logger.warning("⚠️ oMLX reviewers kickstart failed: %s", e)
    else:
        logger.info("🌙 夜間模式：跳過 Phi-4/SmolLM3 啟動")

    # 2.6 OpenClaw cron bridge (排程來源在 OpenClaw；本機執行允許清單命令)
    # OpenClawCron removed (Phase 0)

    # 2.7 File review background worker (auto scan -> download -> archive)
    start_process("FileReviewAuto", f"{_PYTHON} skills/ops/file_review_auto_worker.py")

    # 2.8 Heartbeat monitor (node health + Tailscale serve guard)
    start_process("Heartbeat", f"{_PYTHON} skills/ops/heartbeat.py")

    # 2.9 Personal website admin server (port 8088, for Tailscale remote management)
    _website_admin = os.path.join(_MAGI_ROOT, "whalechao.github.io/admin/admin_server.py")
    _wa_port = int(os.environ.get("WEBSITE_ADMIN_PORT", "8088"))
    if os.path.exists(_website_admin):
        # Guard: skip if port already bound (e.g. previous daemon's child survived)
        _wa_busy = False
        try:
            import socket as _sock
            with _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM) as _s:
                _s.settimeout(0.3)
                _wa_busy = _s.connect_ex(("127.0.0.1", _wa_port)) == 0
        except Exception:
            pass
        if _wa_busy:
            logger.info(f"ℹ️ WebsiteAdmin port {_wa_port} already in use — skipping (likely surviving child)")
        else:
            start_process("WebsiteAdmin", f"{_PYTHON} {_website_admin} --port {_wa_port} --password whalelawyer")
            logger.info(f"✅ Website Admin Server started on port {_wa_port}")

    # 3. Start Keeper Sync Daemon (as background thread)
    try:
        from skills.memory.keeper_sync import start_sync_daemon
        start_sync_daemon()
        logger.info("✅ Keeper Sync Daemon thread started")
    except Exception as e:
        logger.warning(f"⚠️ Keeper Sync not started: {e}")
    
    # 3.5 FSEvents file watcher (macOS only — auto-process new PDFs in case folders)
    _fs_watcher = None
    if IS_MACOS:
        try:
            from skills.ops.fs_watcher import start_watcher as _start_fs_watcher

            def _on_new_file(event_data):
                """Callback for FSEvents: trigger pdf-namer on new PDF files."""
                path = event_data.get("path", "")
                ext = event_data.get("extension", "")
                if ext == ".pdf":
                    logger.info("FSWatcher: new PDF detected, queueing for pdf-namer: %s",
                                os.path.basename(path))
                    try:
                        from skills.ops.macos_notify import notify_pdf_processed
                        notify_pdf_processed(os.path.basename(path), "處理中...")
                    except Exception:
                        pass

            _watch_folders = []
            # NAS case folders
            _nas_cases = "/Volumes/homes/lumi63181107/01_案件"
            if os.path.isdir(_nas_cases):
                _watch_folders.append(_nas_cases)
            # Local scan staging area
            _local_scan = os.path.join(_MAGI_ROOT, "閱卷下載")
            if os.path.isdir(_local_scan):
                _watch_folders.append(_local_scan)

            if _watch_folders:
                _fs_watcher = _start_fs_watcher(_watch_folders, callback=_on_new_file)
                if _fs_watcher:
                    logger.info("✅ FSEvents watcher started for %d folders", len(_watch_folders))
                else:
                    logger.warning("⚠️ FSEvents watcher failed to start")
            else:
                logger.info("ℹ️ FSEvents watcher: no watch folders available")
        except ImportError:
            logger.info("ℹ️ FSEvents watcher: watchdog not installed (pip install watchdog)")
        except Exception as e:
            logger.warning(f"⚠️ FSEvents watcher not started: {e}")

    # Monitor Loop
    try:
        while True:
            monitor_processes()
            reap_orphan_workers(force=False)
            if IS_MACOS:
                _periodic_launchd_check()
            time.sleep(5)
    except KeyboardInterrupt:
        if _fs_watcher:
            _fs_watcher.stop()
        cleanup(None, None)

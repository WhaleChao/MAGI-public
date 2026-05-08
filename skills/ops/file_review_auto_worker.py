#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Background worker for automatic file-review pipeline.

Goals:
- No manual command required.
- Periodically run:
  1) check_emails
  2) optional download (disabled by default; bulk downloads must be explicit)
- Skip overlapping runs when another download task is active.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, Any

import sys

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import get_magi_root_dir, get_orch_dir, get_skill_python

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [file-review-auto] %(message)s",
)
logger = logging.getLogger("file-review-auto")

MAGI_ROOT = os.environ.get("MAGI_ROOT_DIR", str(get_magi_root_dir())).strip() or str(get_magi_root_dir())
CODE_DIR = os.environ.get("MAGI_CODE_DIR", str(get_orch_dir())).strip() or str(get_orch_dir())
VENV_PY = os.environ.get("MAGI_SKILL_PYTHON", str(get_skill_python())).strip()
ACTION_PY = os.path.join(_MAGI_ROOT, "skills", "file-review-orchestrator", "action.py")

LOCK_PATH = Path(os.environ.get("MAGI_FILE_REVIEW_AUTO_LOCK", os.path.join(MAGI_ROOT, "static", "file_review_auto.lock")))
LOG_STATE_PATH = Path(os.environ.get("MAGI_FILE_REVIEW_AUTO_STATE", os.path.join(MAGI_ROOT, "static", "file_review_auto_state.json")))

INTERVAL_SEC = int(os.environ.get("MAGI_FILE_REVIEW_AUTO_INTERVAL_SEC", "3600") or "3600")
CHECK_TIMEOUT_SEC = int(os.environ.get("MAGI_FILE_REVIEW_AUTO_CHECK_TIMEOUT_SEC", "600") or "600")
DOWNLOAD_TIMEOUT_SEC = int(os.environ.get("MAGI_FILE_REVIEW_AUTO_DOWNLOAD_TIMEOUT_SEC", "600") or "600")
RUN_ON_START = str(os.environ.get("MAGI_FILE_REVIEW_AUTO_RUN_ON_START", "1")).strip().lower() in {"1", "true", "yes", "on"}
AUTO_DOWNLOAD = str(os.environ.get("MAGI_FILE_REVIEW_AUTO_DOWNLOAD", "0")).strip().lower() in {"1", "true", "yes", "on"}
START_DELAY_SEC = int(os.environ.get("MAGI_FILE_REVIEW_AUTO_START_DELAY_SEC", "20") or "20")
STALE_DOWNLOAD_SEC = int(os.environ.get("MAGI_FILE_REVIEW_AUTO_STALE_DOWNLOAD_SEC", "1200") or "1200")


def _write_state(data: Dict[str, Any]) -> None:
    try:
        LOG_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_STATE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 60, exc_info=True)


def _tail(text: str, n: int = 1200) -> str:
    s = (text or "").strip()
    if len(s) <= n:
        return s
    return s[-n:]


def _parse_last_json(stdout: str) -> Dict[str, Any]:
    s = (stdout or "").strip()
    if not s:
        return {}
    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    for ln in reversed(lines):
        if ln.startswith("{") and ln.endswith("}"):
            try:
                obj = json.loads(ln)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    return {}


def _parse_etime_to_sec(raw: str) -> int:
    """
    Parse `ps etime` format:
    - MM:SS
    - HH:MM:SS
    - DD-HH:MM:SS
    """
    s = (raw or "").strip()
    if not s:
        return 0
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        try:
            days = int(d)
        except Exception:
            days = 0
    parts = s.split(":")
    try:
        if len(parts) == 3:
            h, m, sec = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            h, m, sec = 0, int(parts[0]), int(parts[1])
        elif len(parts) == 1:
            h, m, sec = 0, 0, int(parts[0])
        else:
            return 0
    except Exception:
        return 0
    return days * 86400 + h * 3600 + m * 60 + sec


def _list_download_processes() -> list[Dict[str, Any]]:
    pat = r"skills/file-review-orchestrator/action.py --task download"
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,etime=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=8,
        ).stdout or ""
    except Exception:
        return []
    me = os.getpid()
    found: list[Dict[str, Any]] = []
    for ln in out.splitlines():
        line = (ln or "").strip()
        if not line:
            continue
        m = re.match(r"^\s*(\d+)\s+(\S+)\s+(.*)$", line)
        if not m:
            continue
        pid = int(m.group(1))
        etime = m.group(2)
        cmd = m.group(3)
        if pid == me:
            continue
        if pat in cmd:
            found.append(
                {
                    "pid": pid,
                    "etime": etime,
                    "age_sec": _parse_etime_to_sec(etime),
                    "cmd": cmd,
                }
            )
    return found


def _is_download_running() -> bool:
    return len(_list_download_processes()) > 0


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _reap_stale_download_processes(max_age_sec: int) -> list[Dict[str, Any]]:
    """Kill stale download processes. Delegates to daemon unified reaper when available."""
    # Try daemon unified reaper first
    try:
        from daemon import request_kill
        killed_pids = request_kill(
            "file-review-orchestrator/action.py --task download",
            "stale download",
            max_age_sec=max_age_sec,
        )
        if killed_pids:
            logger.info("daemon reaper cleaned stale downloads: PIDs %s", killed_pids)
            return [{"pid": pid, "age_sec": 0, "cmd": "cleaned by daemon"} for pid in killed_pids]
        return []
    except ImportError:
        pass
    # Legacy fallback
    procs = _list_download_processes()
    if not procs:
        return []

    killed: list[Dict[str, Any]] = []
    for p in procs:
        age = int(p.get("age_sec") or 0)
        pid = int(p.get("pid") or 0)
        if pid <= 0 or age < max(120, int(max_age_sec)):
            continue
        try:
            logger.warning("stale download process detected: pid=%s age=%ss; terminating", pid, age)
            os.kill(pid, 15)
        except Exception:
            continue

        deadline = time.time() + 6
        while time.time() < deadline:
            if not _is_pid_alive(pid):
                break
            time.sleep(0.3)

        if _is_pid_alive(pid):
            try:
                logger.warning("stale download process still alive: pid=%s; killing", pid)
                os.kill(pid, 9)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 212, exc_info=True)

        killed.append(p)
    return killed


def _run_task(task: str, timeout_sec: int, env: Dict[str, str]) -> Dict[str, Any]:
    cmd = [VENV_PY, ACTION_PY, "--task", task]
    t0 = time.time()
    try:
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(30, int(timeout_sec)),
            env=env,
            cwd=_MAGI_ROOT,
        )
        parsed = _parse_last_json(p.stdout or "")
        ok = bool((parsed.get("success") if isinstance(parsed, dict) else None))
        if not isinstance(parsed, dict) or ("success" not in parsed):
            ok = p.returncode == 0
        return {
            "ok": bool(ok),
            "returncode": int(p.returncode),
            "elapsed_sec": int(time.time() - t0),
            "parsed": parsed if isinstance(parsed, dict) else {},
            "stdout_tail": _tail(p.stdout or ""),
            "stderr_tail": _tail(p.stderr or ""),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "returncode": 124,
            "elapsed_sec": int(time.time() - t0),
            "parsed": {},
            "stdout_tail": _tail(e.stdout or ""),
            "stderr_tail": _tail(e.stderr or ""),
            "error": "timeout",
        }
    except Exception as e:
        return {
            "ok": False,
            "returncode": 1,
            "elapsed_sec": int(time.time() - t0),
            "parsed": {},
            "stdout_tail": "",
            "stderr_tail": "",
            "error": f"{type(e).__name__}: {e}",
        }


def _run_cycle() -> Dict[str, Any]:
    stale_killed = _reap_stale_download_processes(STALE_DOWNLOAD_SEC)
    running = _list_download_processes()
    if running:
        return {
            "ok": True,
            "skipped": True,
            "reason": "download_already_running",
            "running_download_pids": [int(p.get("pid") or 0) for p in running],
            "stale_killed": [int(p.get("pid") or 0) for p in stale_killed],
        }

    env = os.environ.copy()
    env.setdefault("MAGI_CODE_DIR", CODE_DIR)
    env.setdefault("MAGI_ROOT_DIR", _MAGI_ROOT)
    env.setdefault("MAGI_NO_DELETE", "1")
    env.setdefault("MAGI_PREFER_LOCAL_DB", "0")
    env.setdefault("MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK", "0")
    env.setdefault("MAGI_CAPTCHA_DOUBLE_CHECK", "1")
    env.setdefault("MAGI_FILE_REVIEW_PROBE_WITH_GMAIL", "0")
    env.setdefault("MAGI_FILE_REVIEW_DOWNLOAD_MAX_RUNTIME_SEC", os.environ.get("MAGI_FILE_REVIEW_AUTO_MAX_RUNTIME_SEC", "600"))
    env.setdefault("MAGI_SELENIUM_PAGELOAD_TIMEOUT_SEC", os.environ.get("MAGI_FILE_REVIEW_AUTO_PAGELOAD_TIMEOUT_SEC", "35"))
    env.setdefault("MAGI_SELENIUM_SCRIPT_TIMEOUT_SEC", os.environ.get("MAGI_FILE_REVIEW_AUTO_SCRIPT_TIMEOUT_SEC", "35"))

    _write_state(
        {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "phase": "running_check_emails",
            "interval_sec": max(120, INTERVAL_SEC),
        }
    )
    check_res = _run_task('check_emails {"notify_empty": false}', CHECK_TIMEOUT_SEC, env)
    if not AUTO_DOWNLOAD:
        return {
            "ok": bool(check_res.get("ok")),
            "skipped": True,
            "reason": "auto_download_disabled",
            "check": check_res,
            "stale_killed": [int(p.get("pid") or 0) for p in stale_killed],
            "downloaded_count": 0,
        }

    _write_state(
        {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "phase": "running_download",
            "interval_sec": max(120, INTERVAL_SEC),
            "check_result_ok": bool(check_res.get("ok")),
        }
    )
    dl_res = _run_task("download", DOWNLOAD_TIMEOUT_SEC, env)
    dl_parsed = dl_res.get("parsed") if isinstance(dl_res.get("parsed"), dict) else {}
    downloaded_count = 0
    try:
        downloaded_count = int(dl_parsed.get("downloaded_count") or 0)
    except Exception:
        downloaded_count = 0

    return {
        "ok": bool(check_res.get("ok")) and bool(dl_res.get("ok")),
        "check": check_res,
        "download": dl_res,
        "stale_killed": [int(p.get("pid") or 0) for p in stale_killed],
        "downloaded_count": downloaded_count,
    }


def main() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lockf:
        try:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("another worker instance is running; exit")
            return 0

        logger.info("worker started: interval=%ss", max(120, INTERVAL_SEC))
        if START_DELAY_SEC > 0:
            time.sleep(START_DELAY_SEC)

        next_run = time.time() if RUN_ON_START else (time.time() + max(120, INTERVAL_SEC))
        while True:
            now = time.time()
            if now < next_run:
                time.sleep(min(5, max(1, int(next_run - now))))
                continue

            started_at = int(time.time())
            logger.info("cycle start")
            _write_state(
                {
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "started_at": started_at,
                    "interval_sec": max(120, INTERVAL_SEC),
                    "phase": "cycle_started",
                }
            )
            res = _run_cycle()
            logger.info(
                "cycle done: ok=%s skipped=%s downloaded=%s",
                bool(res.get("ok")),
                bool(res.get("skipped")),
                int(res.get("downloaded_count") or 0),
            )
            _write_state(
                {
                    "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "started_at": started_at,
                    "interval_sec": max(120, INTERVAL_SEC),
                    "result": res,
                }
            )
            next_run = time.time() + max(120, INTERVAL_SEC)


if __name__ == "__main__":
    raise SystemExit(main())

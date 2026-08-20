#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Background worker for automatic file-review pipeline.

Goals:
- No manual command required.
- Periodically run:
  1) the authoritative email + court-portal scan
  2) payment/download work only when that scan proves an actionable item
- Skip overlapping runs when another download task is active.
"""

from __future__ import annotations

from magi_v3 import fcntl_compat as fcntl
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Any

import sys

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import get_magi_root_dir, get_orch_dir, get_skill_python
from api.platforms import safe_process

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [file-review-auto] %(message)s",
)
logger = logging.getLogger("file-review-auto")

MAGI_ROOT = os.environ.get("MAGI_ROOT_DIR", str(get_magi_root_dir())).strip() or str(get_magi_root_dir())
CODE_DIR = os.environ.get("MAGI_CODE_DIR", str(get_orch_dir())).strip() or str(get_orch_dir())
VENV_PY = os.environ.get("MAGI_SKILL_PYTHON", str(get_skill_python())).strip()
ACTION_PY = os.path.join(_MAGI_ROOT, "skills", "file-review-orchestrator", "action.py")

MUTABLE_STATIC_DIR = (
    os.environ.get("MAGI_MUTABLE_STATIC_DIR", "").strip()
    or os.path.join(MAGI_ROOT, "static")
)
LOCK_PATH = Path(os.environ.get("MAGI_FILE_REVIEW_AUTO_LOCK", os.path.join(MUTABLE_STATIC_DIR, "file_review_auto.lock")))
LOG_STATE_PATH = Path(os.environ.get("MAGI_FILE_REVIEW_AUTO_STATE", os.path.join(MUTABLE_STATIC_DIR, "file_review_auto_state.json")))
DOWNLOAD_OWNERSHIP_PATH = Path(
    os.environ.get(
        "MAGI_FILE_REVIEW_AUTO_DOWNLOAD_OWNERSHIP",
        os.path.join(MUTABLE_STATIC_DIR, "file_review_auto_download_ownership.json"),
    )
)

_TRUTHY = {"1", "true", "yes", "on"}
# rc248 migrates the old scan-only worker to an unattended, portal-backed
# workflow.  Existing installations may still have the legacy 3600/0/0
# values in their sealed external environment, so the new policy is enabled by
# a separate kill switch and safely caps that legacy interval at ten minutes.
UNATTENDED_MODE = str(os.environ.get("MAGI_FILE_REVIEW_UNATTENDED_MODE", "1")).strip().lower() in _TRUTHY
_CONFIGURED_INTERVAL_SEC = int(os.environ.get("MAGI_FILE_REVIEW_AUTO_INTERVAL_SEC", "600") or "600")
INTERVAL_SEC = min(_CONFIGURED_INTERVAL_SEC, 600) if UNATTENDED_MODE else _CONFIGURED_INTERVAL_SEC
CHECK_TIMEOUT_SEC = int(os.environ.get("MAGI_FILE_REVIEW_AUTO_CHECK_TIMEOUT_SEC", "600") or "600")
DOWNLOAD_TIMEOUT_SEC = int(os.environ.get("MAGI_FILE_REVIEW_AUTO_DOWNLOAD_TIMEOUT_SEC", "600") or "600")
PAYMENT_TIMEOUT_SEC = int(os.environ.get("MAGI_FILE_REVIEW_AUTO_PAYMENT_TIMEOUT_SEC", "300") or "300")
RUN_ON_START = UNATTENDED_MODE or str(os.environ.get("MAGI_FILE_REVIEW_AUTO_RUN_ON_START", "1")).strip().lower() in _TRUTHY
AUTO_DOWNLOAD = UNATTENDED_MODE or str(os.environ.get("MAGI_FILE_REVIEW_AUTO_DOWNLOAD", "1")).strip().lower() in _TRUTHY
AUTO_PAYMENT_SLIPS = UNATTENDED_MODE or str(os.environ.get("MAGI_FILE_REVIEW_AUTO_PAYMENT_SLIPS", "1")).strip().lower() in _TRUTHY
# The unattended owner must claim the shared court-portal lock before startup
# health probes.  A historical 20-second grace period let the read-only
# business check win that race and delayed the first real Gmail/portal cycle
# for another ten minutes after every release handoff.
START_DELAY_SEC = int(os.environ.get("MAGI_FILE_REVIEW_AUTO_START_DELAY_SEC", "0") or "0")
STALE_DOWNLOAD_SEC = int(os.environ.get("MAGI_FILE_REVIEW_AUTO_STALE_DOWNLOAD_SEC", "1200") or "1200")
_TRACKED_PORTAL_TASKS = frozenset({"download", "scheduled_check"})
_STOP_EVENT = threading.Event()


def _handle_stop_signal(_signum: int, _frame: object) -> None:
    """Request a controlled child-tree shutdown during release handoff."""

    _STOP_EVENT.set()


def _write_state(data: Dict[str, Any]) -> None:
    """Publish worker state atomically without erasing the last completed proof.

    Phase updates used to replace the whole JSON document.  During every
    portal scan that briefly removed ``result`` and made readiness interpret
    a healthy in-flight scan as an ingestion failure.  Preserve the previous
    completed result until the new cycle finishes and identify the live worker
    so health checks can distinguish work in progress from a stuck phase.
    """
    try:
        LOG_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        previous: Dict[str, Any] = {}
        try:
            loaded = json.loads(LOG_STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        except Exception:
            previous = {}
        merged = dict(previous)
        merged.update(data)
        merged["pid"] = os.getpid()
        tmp = LOG_STATE_PATH.with_name(f".{LOG_STATE_PATH.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, LOG_STATE_PATH)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 60, exc_info=True)


def _completed_cycle_state(result: Dict[str, Any], *, started_at: int) -> Dict[str, Any]:
    """Build a terminal state update without discarding verified portal proof.

    ``download_already_running`` is only an observation that another owned
    portal job is active.  It is not a new portal scan result.  Publishing it
    as ``result`` used to erase the previous verified snapshot and made the
    readiness page report a false portal-status failure immediately after a
    healthy scheduled download started.
    """
    payload: Dict[str, Any] = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "started_at": started_at,
        "interval_sec": max(120, INTERVAL_SEC),
        "phase": "cycle_complete",
    }
    if (
        bool(result.get("ok"))
        and bool(result.get("skipped"))
        and str(result.get("reason") or "") == "download_already_running"
    ):
        payload["last_observation"] = result
    else:
        payload["result"] = result
    return payload


def _tail(text: str | bytes | None, n: int = 1200) -> str:
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    s = (text or "").strip()
    if len(s) <= n:
        return s
    return s[-n:]


def _mark_payment_scan_nonfatal(result: Dict[str, Any]) -> Dict[str, Any]:
    """Payment slip portal scan is supplemental; Gmail PDF delivery is the critical path."""
    if bool(result.get("ok")):
        return result
    normalized = dict(result)
    normalized["nonfatal"] = True
    normalized["degraded"] = True
    normalized.setdefault("degraded_reason", "supplemental_payment_slip_scan_failed")
    return normalized


def _task_ok_or_nonfatal(result: Dict[str, Any]) -> bool:
    return bool(result.get("ok")) or bool(result.get("nonfatal"))


def _parse_last_json(stdout: str) -> Dict[str, Any]:
    s = (stdout or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    decoder = json.JSONDecoder()
    for idx in [m.start() for m in re.finditer(r"\{", s)][::-1]:
        try:
            obj, _end = decoder.raw_decode(s[idx:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
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


def _write_download_ownership(run_id: str, pid: int, task: str = "download") -> None:
    """Persist evidence for a portal process started by this worker only."""
    task_name = _command_task_name(f"--task {task}") or str(task or "").strip()
    if task_name not in _TRACKED_PORTAL_TASKS:
        raise ValueError(f"untracked portal task: {task_name}")
    payload = {
        "run_id": str(run_id),
        "pid": int(pid),
        "worker_pid": os.getpid(),
        "task": task_name,
        "started_at": time.time(),
    }
    DOWNLOAD_OWNERSHIP_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = DOWNLOAD_OWNERSHIP_PATH.with_name(
        f".{DOWNLOAD_OWNERSHIP_PATH.name}.{os.getpid()}.tmp"
    )
    try:
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(DOWNLOAD_OWNERSHIP_PATH)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _load_download_ownership() -> Dict[str, Any]:
    try:
        payload = json.loads(DOWNLOAD_OWNERSHIP_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _clear_download_ownership(run_id: str) -> None:
    ownership = _load_download_ownership()
    if ownership.get("run_id") != run_id:
        return
    try:
        DOWNLOAD_OWNERSHIP_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _record_download_ownership(run_id: str, pid: int, task: str = "download") -> None:
    try:
        _write_download_ownership(run_id, pid, task)
    except Exception:
        # Missing ownership evidence is safer than leaving a child process
        # untracked because the stale reaper will refuse to touch it.
        logger.warning("could not record download ownership: run_id=%s pid=%s", run_id, pid)


def _command_task_name(command: str) -> str:
    """Return the exact orchestrator task name from a process command line."""
    try:
        tokens = shlex.split(str(command or ""))
    except ValueError:
        tokens = str(command or "").split()
    for idx, token in enumerate(tokens):
        if token == "--task" and idx + 1 < len(tokens):
            return str(tokens[idx + 1]).strip().split(None, 1)[0]
        if token.startswith("--task="):
            return token.split("=", 1)[1].strip().split(None, 1)[0]
    return ""


def _list_download_processes() -> list[Dict[str, Any]]:
    action_marker = "skills/file-review-orchestrator/action.py"
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,etime=,command="],
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
        m = re.match(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s+(.*)$", line)
        if not m:
            continue
        pid = int(m.group(1))
        ppid = int(m.group(2))
        etime = m.group(3)
        cmd = m.group(4)
        if pid == me:
            continue
        # Prefix matching used to treat ``downloadable_probe`` and
        # ``download_payment_slips`` as the long-running ``download`` task.
        # Track the complete scheduled portal cycle too: if the worker is
        # restarted while it owns Chromium, that orphan otherwise blocks all
        # payment/review scans until a human notices.
        task_name = _command_task_name(cmd)
        if action_marker in cmd and task_name in _TRACKED_PORTAL_TASKS:
            found.append(
                {
                    "pid": pid,
                    "ppid": ppid,
                    "etime": etime,
                    "age_sec": _parse_etime_to_sec(etime),
                    "task": task_name,
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


def _is_owned_download_process(process: Dict[str, Any], ownership: Dict[str, Any]) -> bool:
    """Accept only a process with current worker run-id/PID/start-time evidence."""
    try:
        owned_pid = int(ownership.get("pid") or 0)
        worker_pid = int(ownership.get("worker_pid") or 0)
        started_at = float(ownership.get("started_at") or 0)
    except (TypeError, ValueError):
        return False
    run_id = str(ownership.get("run_id") or "")
    if (
        not run_id
        or ownership.get("task") not in _TRACKED_PORTAL_TASKS
        or owned_pid <= 0
        or worker_pid <= 0
        or started_at <= 0
    ):
        return False
    if int(process.get("pid") or 0) != owned_pid:
        return False
    if str(process.get("task") or "") != str(ownership.get("task") or ""):
        return False
    age_sec = int(process.get("age_sec") or 0)
    expected_age = max(0.0, time.time() - started_at)
    # PID reuse is possible; matching the command alone is insufficient.
    return abs(expected_age - age_sec) <= max(60.0, expected_age * 0.1)


def _reap_stale_download_processes(max_age_sec: int) -> list[Dict[str, Any]]:
    """Terminate only stale downloads proven to have been started by this worker."""
    procs = _list_download_processes()
    if not procs:
        return []
    ownership = _load_download_ownership()

    killed: list[Dict[str, Any]] = []
    for p in procs:
        age = int(p.get("age_sec") or 0)
        pid = int(p.get("pid") or 0)
        if (
            pid <= 0
            or age < max(120, int(max_age_sec))
            or not _is_owned_download_process(p, ownership)
        ):
            continue
        try:
            logger.warning(
                "stale portal process detected: task=%s pid=%s age=%ss; terminating",
                p.get("task"),
                pid,
                age,
            )
            if os.getpgid(pid) != pid:
                continue
            os.killpg(pid, 15)
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
                os.killpg(pid, 9)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 212, exc_info=True)

        killed.append(p)
        _clear_download_ownership(str(ownership.get("run_id") or ""))
    return killed


def _run_task(task: str, timeout_sec: int, env: Dict[str, str]) -> Dict[str, Any]:
    cmd = [VENV_PY, ACTION_PY, "--task", task]
    t0 = time.time()
    task_name = str(task or "").strip().split(None, 1)[0]
    run_id = uuid.uuid4().hex if task_name in _TRACKED_PORTAL_TASKS else ""
    task_env = dict(env)
    if run_id:
        task_env["MAGI_FILE_REVIEW_WORKER_RUN_ID"] = run_id
    try:
        p = safe_process.run(
            cmd,
            timeout_sec=max(30, int(timeout_sec)),
            env_extra=task_env,
            cwd=_MAGI_ROOT,
            _on_started=(lambda pid: _record_download_ownership(run_id, pid, task_name)) if run_id else None,
            _cancel_event=_STOP_EVENT,
        )
        if p.timed_out:
            return {
                "ok": False,
                "returncode": 124,
                "elapsed_sec": int(time.time() - t0),
                "parsed": {},
                "stdout_tail": _tail(p.stdout or ""),
                "stderr_tail": _tail(p.stderr or ""),
                "error": "timeout",
            }
        parsed = _parse_last_json(p.stdout or "")
        if not parsed:
            ok = False
            contract_error = "missing_json_object_contract"
        elif "success" not in parsed and "ok" not in parsed:
            ok = False
            contract_error = "missing_success_or_ok_contract"
        else:
            contract_value = parsed.get("success", parsed.get("ok"))
            if type(contract_value) is not bool:
                ok = False
                contract_error = "non_boolean_success_or_ok_contract"
            else:
                ok = p.returncode == 0 and contract_value
                contract_error = ""
            if parsed.get("manual_required") or str(parsed.get("result") or "").strip().lower() == "ready":
                ok = False
                contract_error = "manual_action_required"
        return {
            "ok": bool(ok),
            "returncode": int(p.returncode),
            "elapsed_sec": int(time.time() - t0),
            "parsed": parsed if isinstance(parsed, dict) else {},
            "stdout_tail": _tail(p.stdout or ""),
            "stderr_tail": _tail(p.stderr or ""),
            "contract_error": contract_error,
        }
    except Exception as e:
        if getattr(e, "safe_process_cancelled", False):
            return {
                "ok": True,
                "returncode": 75,
                "elapsed_sec": int(time.time() - t0),
                "parsed": {},
                "stdout_tail": "",
                "stderr_tail": "",
                "cancelled": True,
                "skipped": True,
                "reason": "controlled_handoff",
            }
        return {
            "ok": False,
            "returncode": 1,
            "elapsed_sec": int(time.time() - t0),
            "parsed": {},
            "stdout_tail": "",
            "stderr_tail": "",
            "error": "process_cleanup_failed" if getattr(e, "safe_process_cleanup_failed", False) else f"{type(e).__name__}: {e}",
        }
    finally:
        if run_id:
            _clear_download_ownership(run_id)


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
    env.setdefault("MAGI_EEFILE_DOWNLOAD_FOLDER", os.path.join(MAGI_ROOT, "閱卷下載"))
    env.setdefault("MAGI_NO_DELETE", "1")
    env.setdefault("MAGI_PREFER_LOCAL_DB", "1")
    env.setdefault("MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK", "0")
    env.setdefault("MAGI_CAPTCHA_DOUBLE_CHECK", "1")
    env["MAGI_ENABLE_BACKGROUND_FILE_REVIEW_CHECK"] = "0"
    env.setdefault("MAGI_FILE_REVIEW_PROBE_WITH_GMAIL", "1")
    env["MAGI_FILE_REVIEW_PRIMARY_OWNER"] = "1"
    # Gmail classification must not open another Chromium session internally.
    # This one unattended cycle owns the portal payment/download stages below
    # and drains the durable queues immediately after the mail scan.
    env["MAGI_FILE_REVIEW_EMAIL_SCAN_WITH_PORTAL_PAYMENT"] = "0"
    # The worker must prove the live portal snapshot itself.  A successful
    # Gmail scan alone is not evidence that no court upload is waiting.
    env["MAGI_FILE_REVIEW_CHECK_WITH_PORTAL"] = "1"
    env.setdefault("MAGI_FILE_REVIEW_DOWNLOAD_MAX_RUNTIME_SEC", os.environ.get("MAGI_FILE_REVIEW_AUTO_MAX_RUNTIME_SEC", "600"))
    env.setdefault("MAGI_SELENIUM_PAGELOAD_TIMEOUT_SEC", os.environ.get("MAGI_FILE_REVIEW_AUTO_PAGELOAD_TIMEOUT_SEC", "35"))
    env.setdefault("MAGI_SELENIUM_SCRIPT_TIMEOUT_SEC", os.environ.get("MAGI_FILE_REVIEW_AUTO_SCRIPT_TIMEOUT_SEC", "35"))

    # Interactive uploads are persisted when the one court-portal owner is
    # busy.  Drain those first so a user attachment is never stranded behind
    # recurring scans.  The action command still owns the same portal lock, so
    # this cannot start a second Chromium.
    _write_state(
        {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "phase": "draining_payment_proof_queue",
            "interval_sec": max(120, INTERVAL_SEC),
        }
    )
    queued_uploads = _run_task(
        'process_payment_proof_queue {"notify": true, "max_items": 3}',
        PAYMENT_TIMEOUT_SEC,
        env,
    )
    if queued_uploads.get("cancelled"):
        return queued_uploads
    queued_parsed = queued_uploads.get("parsed") if isinstance(queued_uploads.get("parsed"), dict) else {}
    if not queued_uploads.get("ok") and int(queued_parsed.get("needs_attention_count") or 0) > 0:
        return {
            "ok": False,
            "skipped": True,
            "reason": "payment_proof_queue_needs_attention",
            "payment_proof_queue": queued_uploads,
            "stale_killed": [int(p.get("pid") or 0) for p in stale_killed],
        }

    if not AUTO_DOWNLOAD:
        _write_state(
            {
                "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "phase": "running_check_emails",
                "interval_sec": max(120, INTERVAL_SEC),
                "auto_download": False,
            }
        )
        check_res = _run_task('check_emails {"notify_empty": false}', CHECK_TIMEOUT_SEC, env)
        if check_res.get("cancelled"):
            return check_res
        return {
            "ok": bool(check_res.get("ok")),
            "degraded": False,
            "skipped": True,
            "reason": "auto_download_disabled",
            "check": check_res,
            "stale_killed": [int(p.get("pid") or 0) for p in stale_killed],
            "downloaded_count": 0,
        }

    # Use the same complete, fail-closed entrypoint as the formal cron job.
    # This avoids the former split-brain state where the worker reported a
    # healthy Gmail poll while the only download-capable job waited in a batch
    # lane for up to forty minutes.
    _write_state(
        {
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "phase": "running_scheduled_check",
            "interval_sec": max(120, INTERVAL_SEC),
            "auto_download": True,
        }
    )
    scheduled_timeout = max(
        CHECK_TIMEOUT_SEC,
        CHECK_TIMEOUT_SEC + DOWNLOAD_TIMEOUT_SEC + (PAYMENT_TIMEOUT_SEC if AUTO_PAYMENT_SLIPS else 0),
    )
    formal = _run_task('scheduled_check {"notify": true}', scheduled_timeout, env)
    if formal.get("cancelled"):
        return formal
    parsed = formal.get("parsed") if isinstance(formal.get("parsed"), dict) else {}
    steps = parsed.get("steps") if isinstance(parsed.get("steps"), dict) else {}
    email_step = steps.get("check_emails") if isinstance(steps.get("check_emails"), dict) else {}
    payment_step = steps.get("download_payment_slips") if isinstance(steps.get("download_payment_slips"), dict) else {}
    download_step = steps.get("download") if isinstance(steps.get("download"), dict) else {}
    downloaded_count = 0
    try:
        downloaded_count = int(download_step.get("downloaded_count") or 0)
    except Exception:
        downloaded_count = 0

    return {
        "ok": bool(formal.get("ok")),
        "degraded": bool(parsed.get("deferred")),
        "skipped": bool(parsed.get("skipped")),
        "reason": str(parsed.get("status") or "scheduled_check"),
        "scheduled_check": formal,
        # Preserve the public state shape consumed by readiness/Menubar while
        # making it refer to the authoritative portal-backed step.
        "check": {"ok": email_step.get("success") is not False, "parsed": email_step},
        "payment_slips": payment_step,
        "download": download_step,
        "portal_verified": bool(email_step.get("portal_probe_ok")),
        "portal_probe_deferred": bool(email_step.get("portal_probe_deferred")),
        "portal_raw_row_count": int(email_step.get("portal_raw_row_count") or 0),
        "portal_case_count": int(email_step.get("portal_case_count") or 0),
        "ready_to_download_count": int(email_step.get("ready_to_download_count") or 0),
        "stale_killed": [int(p.get("pid") or 0) for p in stale_killed],
        "downloaded_count": downloaded_count,
        "payment_proof_queue": queued_uploads,
    }


def main() -> int:
    _STOP_EVENT.clear()
    signal.signal(signal.SIGTERM, _handle_stop_signal)
    signal.signal(signal.SIGINT, _handle_stop_signal)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w", encoding="utf-8") as lockf:
        try:
            fcntl.flock(lockf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("another worker instance is running; exit")
            return 0

        logger.info("worker started: interval=%ss", max(120, INTERVAL_SEC))
        if START_DELAY_SEC > 0 and _STOP_EVENT.wait(START_DELAY_SEC):
            return 0

        next_run = time.time() if RUN_ON_START else (time.time() + max(120, INTERVAL_SEC))
        while not _STOP_EVENT.is_set():
            now = time.time()
            if now < next_run:
                _STOP_EVENT.wait(min(5, max(1, int(next_run - now))))
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
            if res.get("cancelled"):
                logger.info("cycle stopped for controlled handoff")
                _write_state(_completed_cycle_state(res, started_at=started_at))
                return 0
            logger.info(
                "cycle done: ok=%s skipped=%s downloaded=%s",
                bool(res.get("ok")),
                bool(res.get("skipped")),
                int(res.get("downloaded_count") or 0),
            )
            _write_state(_completed_cycle_state(res, started_at=started_at))
            next_run = time.time() + max(120, INTERVAL_SEC)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

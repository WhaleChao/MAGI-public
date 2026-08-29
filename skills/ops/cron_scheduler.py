# -*- coding: utf-8 -*-
"""
Cron Scheduler Skill (自動化排程)
Iron Dome Audit: ✅ SAFE — Local JSON storage, no external execution unless via Orchestrator

Provides: Job management (add/remove/list/check)
Schedules are stored in MAGI/cron_jobs.json
"""

import hashlib
import json
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import logging
import re
import subprocess
from datetime import datetime, timedelta
import time
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict

from skills.ops.cron_command_identity import (
    CronCommandIdentityError,
    command_definition_sha256,
)
from skills.ops.cron_result_policy import (
    legacy_candidate_rejection_reason,
    terminal_schedule_deferral_reason,
)
from magi_v3.cron_policy import (
    DURABLE_BACKLOG_COALESCING_JOB_IDS,
    DEFAULT_MAX_PENDING_OCCURRENCES_PER_JOB,
)

# === R3: runtime_dir 接入 ===
try:
    from api.platforms import runtime_dir as _rd
except Exception:
    _rd = None


def _align_datetime_for_comparison(value: datetime, reference: datetime) -> datetime:
    """Interpret legacy naive timestamps as local time before comparing them.

    cron_state historically mixed local naive ISO values with UTC ``Z`` values.
    Python rejects comparisons across those forms, which could crash the Discord
    scheduler during startup reconciliation.  Align to the reference's timezone
    while preserving the legacy meaning of naive values as host-local time.
    """
    if reference.tzinfo is None:
        return value.astimezone().replace(tzinfo=None) if value.tzinfo is not None else value
    if value.tzinfo is None:
        return value.astimezone(reference.tzinfo)
    return value.astimezone(reference.tzinfo)


def _use_runtime_dir() -> bool:
    if _rd is None:
        return False
    return os.environ.get("MAGI_USE_RUNTIME_DIR", "0").strip().lower() in {"1", "true", "on", "yes"}


_RUNTIME_FIELD_EXACT = {
    "command_sha256",
    "result",
    "result_evidence",
    "stdout",
    "stderr",
    "returncode",
    "timed_out",
    "duration_sec",
    "v3_pending_occurrence",
    "v3_retry",
    "v3_resource_recovery",
}
_RUNTIME_FIELD_PREFIXES = ("last_",)


def _cron_state_path() -> Path | None:
    if _rd is None:
        return None
    return _rd.cron_state()


@contextmanager
def _file_lock(path: str | Path):
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as handle:
        try:
            from magi_v3 import fcntl_compat as fcntl  # type: ignore

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            fcntl = None  # type: ignore[assignment]
        try:
            yield
        finally:
            try:
                if fcntl is not None:  # type: ignore[name-defined]
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass


def _atomic_write_json(path: str | Path, payload: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _sanitize_job_definition(job: dict) -> dict:
    clean = {}
    for key, value in dict(job or {}).items():
        key_s = str(key)
        if key_s in _RUNTIME_FIELD_EXACT:
            continue
        if any(key_s.startswith(prefix) for prefix in _RUNTIME_FIELD_PREFIXES):
            continue
        clean[key_s] = value
    return clean


def _load_cron_state() -> Dict[str, Dict[str, Any]]:
    if not _use_runtime_dir():
        return {}
    p = _cron_state_path()
    if p is None:
        return {}
    if not p.exists():
        return {}
    try:
        with _file_lock(p):
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _save_cron_state(state: Dict[str, Dict[str, Any]]) -> None:
    if not _use_runtime_dir():
        return
    p = _cron_state_path()
    if p is None:
        return
    with _file_lock(p):
        _atomic_write_json(p, state)


def _update_cron_state(job_id: str, payload: Dict[str, Any]) -> bool:
    return _update_cron_states({str(job_id or ""): payload})


def _update_cron_states(updates: Dict[str, Dict[str, Any]]) -> bool:
    """Merge one scheduler tick's runtime updates with a single durable write."""
    if not _use_runtime_dir():
        return False
    p = _cron_state_path()
    if p is None:
        return False
    pending = {
        str(job_id or "").strip(): dict(payload or {})
        for job_id, payload in dict(updates or {}).items()
        if str(job_id or "").strip()
    }
    if not pending:
        return False
    with _file_lock(p):
        try:
            state = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            if not isinstance(state, dict):
                state = {}
        except Exception:
            state = {}
        for job_id, payload in pending.items():
            previous = dict(state.get(job_id) or {})
            previous.update(payload)
            state[job_id] = previous
        _atomic_write_json(p, state)
    return True


def mark_job_result_from_evidence(
    job_id: str,
    *,
    evidence_at: datetime,
    success: bool,
    returncode: int = 0,
    error: str = "",
    provenance: str = "",
    expected_error: str = "",
) -> bool:
    """Record an independently verified result without overriding newer work.

    Long-running jobs use this when their own durable state proves completion,
    even if the scheduler process restarted before collecting the child result.
    The evidence must be at or after the current dispatch timestamp.
    """
    jid = str(job_id or "").strip()
    if not jid or not isinstance(evidence_at, datetime):
        return False

    if not _use_runtime_dir():
        return False
    state_path = _cron_state_path()
    if state_path is None:
        return False

    timestamp = evidence_at.isoformat()
    payload: Dict[str, Any] = {
        "last_complete_at": timestamp,
        "last_result_at": timestamp,
        "last_success": bool(success),
        "returncode": int(returncode),
        "timed_out": False,
        "last_returncode": int(returncode),
        "last_timed_out": False,
        "last_error": "" if success else _tail_text(error, 1200),
        "last_stdout_tail": "",
        "last_stderr_tail": "",
        "last_status": "success" if success else "failed",
    }
    if success:
        payload["last_success_at"] = timestamp
    else:
        payload["last_failure_at"] = timestamp
    if provenance:
        payload["result_evidence"] = _tail_text(provenance, 160)

    with _file_lock(state_path):
        try:
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
            if not isinstance(state, dict):
                return False
        except Exception:
            return False
        current = dict(state.get(jid) or {})
        if expected_error and str(current.get("last_error") or "") != expected_error:
            return False
        try:
            dispatched_at = datetime.fromisoformat(
                str(current.get("last_dispatch_at") or current.get("last_run") or "").replace("Z", "+00:00")
            )
        except Exception:
            return False

        comparable_evidence = _align_datetime_for_comparison(evidence_at, dispatched_at)
        if comparable_evidence < dispatched_at:
            return False

        current.update(payload)
        state[jid] = current
        _atomic_write_json(state_path, state)
    return True


_SENSITIVE_JSON_FIELD_RE = re.compile(
    r'(?i)(["\'](?:client_name|party|applicant|recipient|case_number|court_case_no|court_case_number|folder_path|local_path|path|email|phone|token|password|secret|api[_-]?key)["\']\s*:\s*)["\'][^"\'\r\n]*["\']'
)
_CASE_ID_RE = re.compile(r"\b20\d{2}-\d{4,}\b")
_COURT_CASE_RE = re.compile(r"\b1\d{2}年度[^\s,，。；;\"']{1,28}?字第\d{1,8}號")
_PHONE_RE = re.compile(r"\b09\d{2}[- ]?\d{3}[- ]?\d{3}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_CREDENTIAL_RE = re.compile(
    r"(?i)(token|password|secret|api[_-]?key)([\"':= ]+)[^\s,，。；;\"']+"
)
_LOCAL_PATH_RE = re.compile(r"(?:/Users/|/Volumes/)[^\r\n\"']+")


def _redact_runtime_text(value: Any) -> str:
    """Remove case identifiers, credentials, and local paths before state persistence."""

    text = str(value or "")
    text = _SENSITIVE_JSON_FIELD_RE.sub(r'\1"<REDACTED>"', text)
    text = _CASE_ID_RE.sub("<CASE_ID>", text)
    text = _COURT_CASE_RE.sub("<COURT_CASE_NO>", text)
    text = _PHONE_RE.sub("<PHONE>", text)
    text = _EMAIL_RE.sub("<EMAIL>", text)
    text = _CREDENTIAL_RE.sub(r"\1\2<REDACTED>", text)
    text = _LOCAL_PATH_RE.sub("<LOCAL_PATH>", text)
    return text


def _tail_text(value: Any, limit: int = 1200) -> str:
    text = _redact_runtime_text(value)
    if len(text) <= limit:
        return text
    return text[-limit:]

logger = logging.getLogger("CronScheduler")


def _nas_storage_available() -> bool:
    """Check only the local mount table; never wake or traverse a stale SMB mount."""
    try:
        result = subprocess.run(
            ["/sbin/mount"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return any(
        re.search(r" on /Volumes/homes(?:-\d+)? ", line)
        for line in result.stdout.splitlines()
    )

_DEFAULT_CATCHUP_SKIP_IDS = {
    # These jobs can scan NAS/case folders or open portal automation. Running
    # them immediately after reboot stacks IO on top of SMB remount recovery.
    "job_laf_nightly_audit",
    "job_pdf_namer_nightly",
    "job_weekend_bookmark",
    "job_nightly_bookmark_regex",
    "job_benchmark_pdf_namer",
    "job_obsidian_ingest",
    "job_osc_scan_cases",
    "job_insight_sync",
    "job_drive_case_sync_bidirectional",
}


def _catchup_skip_ids() -> set[str]:
    raw = os.environ.get("MAGI_CRON_CATCHUP_SKIP_IDS", "").strip()
    extra = {x.strip() for x in raw.split(",") if x.strip()}
    return _DEFAULT_CATCHUP_SKIP_IDS | extra

JOB_FILE = os.path.abspath(
    os.path.expanduser(os.environ.get("MAGI_CRON_JOBS_FILE", "").strip())
    or f"{_MAGI_ROOT}/cron_jobs.json"
)

class CronScheduler:
    def __init__(self):
        self.jobs = []
        self._last_file_mtime = 0.0
        self._load_jobs()

    def _new_job_id(self) -> str:
        return f"job_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

    def _normalize_jobs(self):
        normalized = []
        seen_ids = set()
        changed = False
        for raw in self.jobs:
            if not isinstance(raw, dict):
                changed = True
                continue
            job = dict(raw)
            job_id = str(job.get("id", "")).strip()
            if not job_id or job_id in seen_ids:
                job_id = self._new_job_id()
                changed = True
            seen_ids.add(job_id)
            job["id"] = job_id
            job.setdefault("cron", "0 9 * * *")
            job.setdefault("command", "")
            job.setdefault("desc", "")
            job.setdefault("channel_id", None)
            job.setdefault("last_run", None)
            job.setdefault("last_run_minute", None)
            job.setdefault("last_dispatch_at", None)
            job.setdefault("last_dispatch_minute", None)
            job.setdefault("enabled", True)
            normalized.append(job)
        self.jobs = normalized
        if changed:
            self._save_jobs()

    def _load_jobs(self):
        """Load jobs from JSON file."""
        if os.path.exists(JOB_FILE):
            try:
                self._last_file_mtime = os.path.getmtime(JOB_FILE)
                with open(JOB_FILE, 'r', encoding='utf-8') as f:
                    self.jobs = json.load(f)
                if not isinstance(self.jobs, list):
                    self.jobs = []
            except Exception as e:
                logger.error(f"Failed to load jobs: {e}")
                self.jobs = []
        else:
            self.jobs = []
        self._normalize_jobs()
        # --- R3 merge：若 cron_state.json 存在，用它覆蓋 last_run / last_run_minute ---
        if _use_runtime_dir():
            state = _load_cron_state()
            for j in self.jobs:
                jid = j.get("id")
                if jid and jid in state:
                    for key, value in state[jid].items():
                        j[key] = value

    def _hot_reload_if_changed(self):
        """Reload jobs from disk if the file was modified externally."""
        try:
            if not os.path.exists(JOB_FILE):
                return
            mtime = os.path.getmtime(JOB_FILE)
            if mtime > self._last_file_mtime:
                old_count = len(self.jobs)
                old_ids = {j["id"] for j in self.jobs}
                self._load_jobs()
                new_ids = {j["id"] for j in self.jobs}
                added = new_ids - old_ids
                if added:
                    logger.info("🔄 Hot-reloaded cron_jobs.json: %d→%d jobs (+%s)",
                                old_count, len(self.jobs), ", ".join(added))
        except Exception as e:
            logger.warning("Hot-reload check failed: %s", e)

    def _save_jobs(self):
        """Save jobs to JSON file (merge-safe: preserves externally-added jobs)."""
        # V3 deploys a hash-bound, release-rebased definition snapshot. Runtime
        # dispatch/result fields are already persisted in cron_state.json, so
        # rewriting that snapshot would both break its integrity binding and
        # make a supervised restart fail closed. Definition changes must be
        # prepared as a new deployment artifact instead.
        if os.environ.get("MAGI_CRON_DEFINITIONS_IMMUTABLE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return
        try:
            with _file_lock(JOB_FILE):
                # Merge: read disk first to preserve jobs added externally since last load.
                disk_jobs = []
                if os.path.exists(JOB_FILE):
                    try:
                        with open(JOB_FILE, 'r', encoding='utf-8') as f:
                            disk_jobs = json.load(f)
                        if not isinstance(disk_jobs, list):
                            disk_jobs = []
                    except Exception:
                        disk_jobs = []

                # Merge: start with in-memory state, then append any disk-only jobs.
                # Runtime keys may live in memory while the scheduler is running, but
                # cron_jobs.json is a definition file and is always written sanitized.
                merged = list(self.jobs)
                merged_ids = {j["id"] for j in merged if isinstance(j, dict) and j.get("id")}
                for dj in disk_jobs:
                    if not isinstance(dj, dict):
                        continue
                    djid = (dj.get("id") or "").strip()
                    if djid and djid not in merged_ids:
                        merged.append(dj)
                        merged_ids.add(djid)
                        logger.info("🔄 Preserved externally-added job: %s", djid)

                self.jobs = merged
                payload_jobs = [_sanitize_job_definition(j) for j in self.jobs if isinstance(j, dict)]
                _atomic_write_json(JOB_FILE, payload_jobs)
                try:
                    self._last_file_mtime = os.path.getmtime(JOB_FILE)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Failed to save jobs: {e}")

    def mark_job_dispatched(self, job_id: str, *, when: datetime | None = None) -> bool:
        """Record that a cron job was dispatched.

        Startup/late catch-up jobs are not discovered by ``check_due_jobs`` and
        used to execute without updating ``cron_state.json``. That made the same
        morning jobs look missed again after every daemon restart. Dispatch is
        not completion evidence; health checks must use result/success fields.
        """
        jid = str(job_id or "").strip()
        if not jid:
            return False
        self._hot_reload_if_changed()
        now = when or datetime.now()
        payload = {
            "last_run": now.isoformat(),
            "last_run_minute": now.strftime("%Y-%m-%d %H:%M"),
            "last_dispatch_at": now.isoformat(),
            "last_dispatch_minute": now.strftime("%Y-%m-%d %H:%M"),
        }
        changed = False
        for job in self.jobs:
            if str(job.get("id") or "") == jid:
                pending = job.get("v3_pending_occurrence")
                if isinstance(pending, dict):
                    pending = dict(pending)
                    pending["status"] = "running"
                    pending["claimed_at"] = now.isoformat()
                    payload["v3_pending_occurrence"] = pending
                retry = job.get("v3_retry")
                if isinstance(retry, dict) and retry.get("status") in {"queued", "running"}:
                    retry = dict(retry)
                    retry["status"] = "running"
                    retry["claimed_at"] = now.isoformat()
                    payload["v3_retry"] = retry
                job.update(payload)
                changed = True
                break
        if not changed:
            return False
        _update_cron_state(jid, payload)
        self._save_jobs()
        return True

    def mark_job_v3_pending(
        self,
        job_id: str,
        *,
        due_at: str,
        effective_at: str,
        lane: str,
    ) -> bool:
        """Durably queue one V3 occurrence before executor submission."""
        jid = str(job_id or "").strip()
        if not jid or lane not in {"light", "batch", "maintenance"}:
            return False
        try:
            due = datetime.fromisoformat(str(due_at))
            effective = datetime.fromisoformat(str(effective_at))
        except (TypeError, ValueError):
            return False
        if effective < due:
            return False
        occurrence = {
            "job_id": jid,
            "due_at": due.isoformat(),
            "effective_at": effective.isoformat(),
            "lane": lane,
            "status": "queued",
        }
        changed = False
        for job in self.jobs:
            if str(job.get("id") or "") != jid:
                continue
            current = job.get("v3_pending_occurrence")
            if isinstance(current, dict):
                try:
                    current_due = datetime.fromisoformat(str(current.get("due_at") or ""))
                except ValueError:
                    current_due = due
                if current_due > due:
                    occurrence = current
            job["v3_pending_occurrence"] = occurrence
            changed = True
            break
        if not changed:
            return False
        return _update_cron_state(jid, {"v3_pending_occurrence": occurrence})

    def recover_v3_pending_jobs(self) -> list[dict[str, Any]]:
        """Return durable queued/running V3 occurrences for idempotent recovery."""
        self._hot_reload_if_changed()
        recovered: list[tuple[datetime, dict[str, Any]]] = []
        for job in self.jobs:
            pending = job.get("v3_pending_occurrence")
            if not isinstance(pending, dict):
                continue
            job_id = str(job.get("id") or "")
            if pending.get("job_id") != job_id:
                continue
            try:
                due = datetime.fromisoformat(str(pending.get("due_at") or ""))
                datetime.fromisoformat(str(pending.get("effective_at") or ""))
            except ValueError:
                continue
            if pending.get("lane") not in {"light", "batch", "maintenance"}:
                continue
            occurrence = dict(job)
            occurrence["_magi_due_at"] = due.isoformat()
            recovered.append((due, occurrence))
        recovered.sort(key=lambda row: (row[0], str(row[1].get("id") or "")))
        return [job for _due, job in recovered]

    def schedule_job_v3_retry(
        self,
        job_id: str,
        *,
        command_sha256: str,
        occurrence_id: str = "",
        reason_code: str,
        public_reason: str,
        max_attempts: int = 3,
        delays_seconds: tuple[int, ...] = (60, 300, 900),
        source_returncode: int | None = None,
        source_timed_out: bool = False,
        when: datetime | None = None,
    ) -> dict[str, Any]:
        """Durably queue one bounded automatic retry for a business job."""

        jid = str(job_id or "").strip()
        supplied_sha = str(command_sha256 or "").strip().lower()
        if not jid or not re.fullmatch(r"[0-9a-f]{64}", supplied_sha):
            return {"scheduled": False, "exhausted": False, "reason": "invalid_identity"}
        self._hot_reload_if_changed()
        current_job = next(
            (job for job in self.jobs if str(job.get("id") or "") == jid),
            None,
        )
        if current_job is None:
            return {"scheduled": False, "exhausted": False, "reason": "unknown_job"}
        try:
            if command_definition_sha256(current_job) != supplied_sha:
                return {"scheduled": False, "exhausted": False, "reason": "definition_drift"}
        except CronCommandIdentityError:
            return {"scheduled": False, "exhausted": False, "reason": "invalid_definition"}

        state_row = dict(_load_cron_state().get(jid) or {})
        previous = state_row.get("v3_retry")
        supplied_occurrence_id = str(occurrence_id or "").strip().lower()
        if supplied_occurrence_id and not re.fullmatch(r"[0-9a-f]{64}", supplied_occurrence_id):
            return {"scheduled": False, "exhausted": False, "reason": "invalid_occurrence_id"}
        previous_occurrence_id = (
            str(previous.get("occurrence_id") or "").strip().lower()
            if isinstance(previous, dict)
            else ""
        )
        root_occurrence_id = supplied_occurrence_id or previous_occurrence_id
        if not root_occurrence_id:
            root_occurrence_id = hashlib.sha256(
                f"{jid}\0{supplied_sha}\0{(when or datetime.now()).isoformat()}".encode("utf-8")
            ).hexdigest()
        max_count = max(1, min(6, int(max_attempts or 3)))
        delays = tuple(max(15, min(3600, int(value))) for value in delays_seconds) or (60,)
        prior_attempt = 0
        if (
            isinstance(previous, dict)
            and previous.get("command_sha256") == supplied_sha
            and previous_occurrence_id == root_occurrence_id
        ):
            try:
                prior_attempt = max(0, int(previous.get("attempt") or 0))
            except (TypeError, ValueError):
                prior_attempt = 0
        attempt = prior_attempt + 1
        now = when or datetime.now()
        if attempt > max_count:
            exhausted = {
                "job_id": jid,
                "status": "exhausted",
                "attempt": prior_attempt,
                "max_attempts": max_count,
                "exhausted_at": now.isoformat(),
                "reason_code": _tail_text(reason_code, 80),
                "public_reason": _tail_text(public_reason, 240),
                "command_sha256": supplied_sha,
                "occurrence_id": root_occurrence_id,
            }
            current_job["v3_retry"] = exhausted
            _update_cron_state(jid, {"v3_retry": exhausted})
            return {"scheduled": False, "exhausted": True, **exhausted}

        delay_index = min(attempt - 1, len(delays) - 1)
        retry_at = now + timedelta(seconds=delays[delay_index])
        retry = {
            "job_id": jid,
            "status": "queued",
            "attempt": attempt,
            "max_attempts": max_count,
            "queued_at": now.isoformat(),
            "retry_at": retry_at.isoformat(),
            "reason_code": _tail_text(reason_code, 80),
            "public_reason": _tail_text(public_reason, 240),
            "command_sha256": supplied_sha,
            "occurrence_id": root_occurrence_id,
            "source_returncode": source_returncode,
            "source_timed_out": bool(source_timed_out),
        }
        current_job["v3_retry"] = retry
        if not _update_cron_state(jid, {"v3_retry": retry}):
            current_job.pop("v3_retry", None)
            return {"scheduled": False, "exhausted": False, "reason": "persistence_failed"}
        return {"scheduled": True, "exhausted": False, **retry}

    def recover_v3_retry_jobs(self) -> list[dict[str, Any]]:
        """Return queued/running automatic retries, including after restart."""

        self._hot_reload_if_changed()
        state = _load_cron_state()
        recovered: list[tuple[datetime, dict[str, Any]]] = []
        for job in self.jobs:
            jid = str(job.get("id") or "").strip()
            retry = (state.get(jid) or {}).get("v3_retry")
            if not isinstance(retry, dict) or retry.get("job_id") != jid:
                continue
            if retry.get("status") not in {"queued", "running"}:
                continue
            command_sha = str(retry.get("command_sha256") or "").strip().lower()
            try:
                current_command_sha = command_definition_sha256(job)
                if current_command_sha != command_sha:
                    # A retry belongs to one immutable command definition.
                    # After an upgrade it must neither execute the old body nor
                    # remain forever as a yellow "queued" receipt.  Preserve
                    # the failed/deferred result itself and retire only the
                    # stale retry ticket.
                    superseded = {
                        "v3_retry": None,
                        "last_retry_superseded_at": datetime.now().isoformat(),
                        "last_retry_superseded_from_sha256": command_sha,
                        "last_retry_superseded_to_sha256": current_command_sha,
                    }
                    job.update(superseded)
                    _update_cron_state(jid, superseded)
                    continue
                retry_at = datetime.fromisoformat(str(retry.get("retry_at") or ""))
            except (CronCommandIdentityError, TypeError, ValueError):
                continue
            occurrence = dict(job)
            occurrence["_magi_due_at"] = retry_at.isoformat()
            occurrence["_magi_retry"] = True
            occurrence["_magi_retry_attempt"] = int(retry.get("attempt") or 1)
            occurrence["_magi_occurrence_id"] = str(retry.get("occurrence_id") or "")
            recovered.append((retry_at, occurrence))
        recovered.sort(key=lambda row: (row[0], str(row[1].get("id") or "")))
        return [job for _retry_at, job in recovered]

    def rearm_recovered_resource_deferrals(
        self, *, now: datetime | None = None
    ) -> list[str]:
        """Re-arm exhausted storage work exactly once after SMB is available.

        Exhausted near-term retries remain immutable evidence.  A newly
        mounted NAS creates a new occurrence instead of erasing that history.
        The owning job's resource guard still controls CPU/RAM admission.
        """

        if not _nas_storage_available():
            return []
        self._hot_reload_if_changed()
        state = _load_cron_state()
        current = now or datetime.now()
        rearmed: list[str] = []
        for job in self.jobs:
            job_id = str(job.get("id") or "").strip()
            row = dict(state.get(job_id) or {})
            retry = row.get("v3_retry")
            if not isinstance(retry, dict):
                continue
            if retry.get("status") != "exhausted" or retry.get("reason_code") != "storage_unavailable":
                continue
            old_occurrence = str(retry.get("occurrence_id") or "").strip().lower()
            command_sha = str(retry.get("command_sha256") or "").strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", old_occurrence):
                continue
            previous_recovery = row.get("v3_resource_recovery")
            if (
                isinstance(previous_recovery, dict)
                and previous_recovery.get("from_occurrence_id") == old_occurrence
            ):
                continue
            try:
                if command_definition_sha256(job) != command_sha:
                    continue
            except CronCommandIdentityError:
                continue
            new_occurrence = hashlib.sha256(
                f"{old_occurrence}\0{command_sha}\0storage-recovered".encode("utf-8")
            ).hexdigest()
            queued = self.schedule_job_v3_retry(
                job_id,
                command_sha256=command_sha,
                occurrence_id=new_occurrence,
                reason_code="storage_recovered",
                public_reason="網路儲存裝置已恢復，MAGI 正在自動接續未完成工作",
                max_attempts=3,
                delays_seconds=(60, 300, 900),
                source_returncode=75,
                source_timed_out=False,
                when=current,
            )
            if not bool(queued.get("scheduled")):
                continue
            evidence = {
                "from_occurrence_id": old_occurrence,
                "new_occurrence_id": new_occurrence,
                "rearmed_at": current.isoformat(),
                "condition": "nas_mount_available",
            }
            job["v3_resource_recovery"] = evidence
            _update_cron_state(job_id, {"v3_resource_recovery": evidence})
            rearmed.append(job_id)
        return rearmed

    def mark_job_run(self, job_id: str, *, when: datetime | None = None) -> bool:
        """Backward-compatible alias for dispatch evidence."""
        return self.mark_job_dispatched(job_id, when=when)

    def mark_job_started(
        self,
        job_id: str,
        *,
        when: datetime | None = None,
        command_sha256: str = "",
    ) -> bool:
        """Atomically validate the current definition and record process start."""
        jid = str(job_id or "").strip()
        if not jid:
            return False
        # A due-job dict may have waited in an executor queue while the
        # definitions file was hot-reloaded.  Always validate against the
        # current definition immediately before the caller spawns anything.
        self._hot_reload_if_changed()
        now = when or datetime.now()
        payload = {"last_start_at": now.isoformat()}
        for job in self.jobs:
            if str(job.get("id") or "") != jid:
                continue
            try:
                current_command_sha256 = command_definition_sha256(job)
            except CronCommandIdentityError:
                return False
            supplied_command_sha256 = str(command_sha256 or "").strip()
            if (
                not supplied_command_sha256
                or supplied_command_sha256 != current_command_sha256
            ):
                return False
            payload["command_sha256"] = supplied_command_sha256
            job.update(payload)
            persisted = _update_cron_state(jid, payload)
            if _use_runtime_dir() and not persisted:
                job.pop("last_start_at", None)
                job.pop("command_sha256", None)
                return False
            return True
        # Never create runtime state for a jid removed by a hot reload.
        return False

    def mark_job_result(
        self,
        job_id: str,
        *,
        success: bool,
        returncode: int | None = None,
        timed_out: bool = False,
        error: str = "",
        stdout_tail: str = "",
        stderr_tail: str = "",
        duration_sec: float | None = None,
        when: datetime | None = None,
        status: str = "",
        terminal_deferred: bool = False,
        command_sha256: str = "",
    ) -> bool:
        """Record the actual result of a dispatched cron job.

        ``mark_job_run`` intentionally records dispatch before execution so
        catch-up does not double-launch long jobs. This method is the second
        half of that contract: it records whether the launched job actually
        finished successfully.
        """
        jid = str(job_id or "").strip()
        if not jid:
            return False
        self._hot_reload_if_changed()
        supplied_command_sha256 = str(command_sha256 or "").strip()
        state_command_sha256 = str(
            (_load_cron_state().get(jid) or {}).get("command_sha256") or ""
        ).strip()
        current_job = next(
            (
                job
                for job in self.jobs
                if str(job.get("id") or "") == jid
            ),
            None,
        )
        if current_job is None:
            return False
        try:
            current_command_sha256 = command_definition_sha256(current_job)
        except CronCommandIdentityError:
            return False
        if not state_command_sha256:
            state_command_sha256 = str(
                current_job.get("command_sha256") or ""
            ).strip()
        if (
            supplied_command_sha256
            and state_command_sha256
            and supplied_command_sha256 != state_command_sha256
        ):
            return False
        now = when or datetime.now()
        if (
            supplied_command_sha256
            and supplied_command_sha256 != current_command_sha256
        ):
            # The process genuinely started under an older definition, but its
            # completion must never become success/duration evidence for the
            # hot-reloaded command now occupying the same jid.
            if state_command_sha256 != supplied_command_sha256:
                return False
            drift_payload: Dict[str, Any] = {
                "last_status": "definition_drift",
                "last_error": (
                    "definition_drift: completion rejected after command change"
                ),
                "last_definition_drift_at": now.isoformat(),
                "last_definition_drift_from_sha256": supplied_command_sha256,
                "last_definition_drift_to_sha256": current_command_sha256,
                "last_definition_drift_success": bool(success),
                "last_definition_drift_returncode": returncode,
                "last_definition_drift_timed_out": bool(timed_out),
            }
            current_job.update(drift_payload)
            _update_cron_state(jid, drift_payload)
            return False
        result_command_sha256 = supplied_command_sha256 or state_command_sha256
        if result_command_sha256 and not re.fullmatch(
            r"[0-9a-f]{64}", result_command_sha256
        ):
            return False
        normalized_status = str(status or ("success" if success else "failed")).strip().lower()
        if normalized_status not in {"success", "failed", "deferred"}:
            normalized_status = "success" if success else "failed"
        payload: Dict[str, Any] = {
            "last_complete_at": now.isoformat(),
            "last_result_at": now.isoformat(),
            "last_success": bool(success),
            "returncode": returncode,
            "timed_out": bool(timed_out),
            "last_returncode": returncode,
            "last_timed_out": bool(timed_out),
            "last_stdout_tail": _tail_text(stdout_tail, 1200),
            "last_stderr_tail": _tail_text(stderr_tail, 1200),
            "last_status": normalized_status,
            "v3_pending_occurrence": None,
            "last_review_required": bool(
                normalized_status == "deferred"
                and str(error or "").strip().lower() == "candidate_rejected"
            ),
            "last_candidate_rejected": bool(
                normalized_status == "deferred"
                and str(error or "").strip().lower() == "candidate_rejected"
            ),
        }
        if success:
            recovery_receipt = current_job.get("v3_retry")
            if not isinstance(recovery_receipt, dict):
                recovery_receipt = (_load_cron_state().get(jid) or {}).get("v3_retry")
            if isinstance(recovery_receipt, dict) and str(
                recovery_receipt.get("status") or ""
            ).strip().lower() in {"queued", "running", "exhausted"}:
                payload.update(
                    {
                        "last_recovered_at": now.isoformat(),
                        "last_recovery_attempts": int(recovery_receipt.get("attempt") or 0),
                        "last_recovery_reason_code": _tail_text(
                            recovery_receipt.get("reason_code"), 80
                        ),
                        "last_recovery_occurrence_id": _tail_text(
                            recovery_receipt.get("occurrence_id"), 64
                        ),
                    }
                )
            payload["v3_retry"] = None
        elif normalized_status == "failed":
            # Preserve an exhausted receipt as evidence, but cancel any older
            # queued occurrence when the new result is explicitly terminal.
            existing_retry = current_job.get("v3_retry")
            if not isinstance(existing_retry, dict):
                existing_retry = (_load_cron_state().get(jid) or {}).get("v3_retry")
            if isinstance(existing_retry, dict) and existing_retry.get("status") == "exhausted":
                payload["v3_retry"] = existing_retry
            else:
                payload["v3_retry"] = None
        elif normalized_status == "deferred" and terminal_deferred:
            # Expected work-budget/off-peak waits are terminal for this cron
            # occurrence.  They resume from their checkpoint on the next
            # ordinary schedule and must clear any older timeout retry receipt
            # instead of inheriting/exhausting it into a false red light.
            payload["v3_retry"] = None
        if result_command_sha256:
            payload["command_sha256"] = result_command_sha256
        if duration_sec is not None:
            try:
                payload["last_duration_sec"] = round(float(duration_sec), 3)
            except Exception:
                pass
        if success:
            payload["last_success_at"] = now.isoformat()
            payload["last_error"] = ""
        elif normalized_status == "deferred":
            payload["last_deferred_at"] = now.isoformat()
            payload["last_error"] = _tail_text(error or "resource_guard_skipped", 1200)
        else:
            payload["last_failure_at"] = now.isoformat()
            payload["last_error"] = _tail_text(error, 1200)
        changed = False
        for job in self.jobs:
            if str(job.get("id") or "") == jid:
                job.update(payload)
                changed = True
                break

        if _update_cron_state(jid, payload):
            changed = True
        self._save_jobs()
        return changed

    def mark_job_complete(self, job_id: str, **kwargs) -> bool:
        """Preferred completion marker; kept separate from dispatch/start."""
        return self.mark_job_result(job_id, **kwargs)

    def reconcile_incomplete_jobs(self, *, now: datetime | None = None) -> list[str]:
        """Recover expired dispatches which never produced a completion record."""

        self._hot_reload_if_changed()
        current = now or datetime.now()
        reconciled: list[str] = []
        for job in list(self.jobs):
            job_id = str(job.get("id") or "").strip()
            try:
                timeout_sec = int(job.get("timeout_sec") or 0)
            except Exception:
                timeout_sec = 0
            if not job_id or timeout_sec <= 0:
                continue
            try:
                dispatch = datetime.fromisoformat(str(job.get("last_dispatch_at") or "").replace("Z", "+00:00"))
            except Exception:
                continue
            if str(job.get("last_status") or "") == "definition_drift":
                try:
                    drift_at = datetime.fromisoformat(
                        str(job.get("last_definition_drift_at") or "").replace(
                            "Z", "+00:00"
                        )
                    )
                except Exception:
                    drift_at = None
                if (
                    drift_at is not None
                    and _align_datetime_for_comparison(drift_at, dispatch)
                    >= dispatch
                ):
                    continue
            try:
                complete = datetime.fromisoformat(str(job.get("last_complete_at") or "").replace("Z", "+00:00"))
            except Exception:
                complete = None
            if complete is not None and _align_datetime_for_comparison(complete, dispatch) >= dispatch:
                continue
            compare_now = _align_datetime_for_comparison(current, dispatch)
            if compare_now <= dispatch + timedelta(seconds=max(60, timeout_sec)):
                continue
            existing_retry = job.get("v3_retry")
            existing_retry_status = (
                str(existing_retry.get("status") or "").strip().lower()
                if isinstance(existing_retry, dict)
                else ""
            )
            if existing_retry_status in {"queued", "running"}:
                # The retry attempt was already counted before dispatch.  A
                # supervisor restart must requeue that same attempt, not burn
                # another attempt or turn it into a false terminal failure.
                retry = dict(existing_retry)
                retry["status"] = "queued"
                retry["retry_at"] = current.isoformat()
                retry["recovered_after_restart_at"] = current.isoformat()
                job["v3_retry"] = retry
                _update_cron_state(job_id, {"v3_retry": retry})
                reconciled.append(job_id)
                continue

            try:
                command_sha = command_definition_sha256(job)
                pending = job.get("v3_pending_occurrence")
                due_identity = (
                    str(pending.get("due_at") or pending.get("effective_at") or "")
                    if isinstance(pending, dict)
                    else ""
                )
                due_identity = due_identity or dispatch.isoformat()
                occurrence_id = hashlib.sha256(
                    f"{job_id}\0{command_sha}\0{due_identity}".encode("utf-8")
                ).hexdigest()
                from magi_v3.business_recovery import (
                    decide_recovery,
                    retry_status_message,
                )

                decision = decide_recovery(
                    job,
                    returncode=130,
                    error="scheduler_completion_missing_after_timeout",
                    status="failed",
                    timed_out=True,
                )
                queued = (
                    self.schedule_job_v3_retry(
                        job_id,
                        command_sha256=command_sha,
                        occurrence_id=occurrence_id,
                        reason_code=decision.reason_code,
                        public_reason=decision.public_reason,
                        max_attempts=decision.max_attempts,
                        delays_seconds=decision.retry_delays_seconds,
                        source_returncode=130,
                        source_timed_out=True,
                        when=current,
                    )
                    if decision.retryable
                    else {"scheduled": False}
                )
            except Exception:
                logger.exception("failed to build restart recovery for cron job %s", job_id)
                queued = {"scheduled": False}
                decision = None

            if queued.get("scheduled") and decision is not None:
                recorded = self.mark_job_result(
                    job_id,
                    success=False,
                    returncode=75,
                    timed_out=False,
                    error=retry_status_message(
                        decision,
                        attempt=int(queued.get("attempt") or 1),
                        retry_at=str(queued.get("retry_at") or "稍後"),
                    ),
                    status="deferred",
                    when=current,
                    command_sha256=command_sha,
                )
            else:
                recorded = self.mark_job_result(
                    job_id,
                    success=False,
                    returncode=130,
                    timed_out=True,
                    error="scheduler_completion_missing_after_timeout",
                    when=current,
                )
            if recorded:
                reconciled.append(job_id)
        return reconciled

    def reconcile_terminal_schedule_deferrals(
        self, *, now: datetime | None = None
    ) -> list[str]:
        """Repair legacy false failures backed by strict deferred evidence.

        Earlier owners could retry an expected work-budget/off-peak deferral
        until the retry receipt became exhausted and the menubar turned red.
        This migration is deliberately narrow: it accepts only the same
        structured, fail-closed terminal deferral contract used for new runs;
        timeouts, non-zero errors, partial results and arbitrary text remain
        failures.
        """

        self._hot_reload_if_changed()
        state = _load_cron_state()
        current = now or datetime.now()
        reconciled: list[str] = []
        for job in self.jobs:
            job_id = str(job.get("id") or "").strip()
            row = state.get(job_id) if job_id else None
            if not isinstance(row, dict):
                continue
            status = str(row.get("last_status") or "").strip().lower()
            if status != "failed" or bool(
                row.get("last_timed_out", row.get("timed_out", False))
            ):
                continue
            try:
                returncode = int(
                    row.get("last_returncode", row.get("returncode", 0)) or 0
                )
            except (TypeError, ValueError):
                continue
            stdout_tail = str(row.get("last_stdout_tail") or "")
            stderr_tail = str(row.get("last_stderr_tail") or "").strip()
            last_error = str(row.get("last_error") or "").strip()
            combined_error = "\n".join(
                value for value in (stderr_tail, last_error) if value
            )
            reason = ""
            if returncode in {0, 75}:
                reason = terminal_schedule_deferral_reason(
                    stdout_tail, combined_error
                )
            elif returncode == 1:
                reason = legacy_candidate_rejection_reason(
                    job_id,
                    stdout_tail,
                    stderr_tail,
                    last_error,
                )
            else:
                continue
            if not reason:
                continue
            payload = {
                "last_status": "deferred",
                "last_success": False,
                "last_error": reason,
                "v3_retry": None,
                "last_terminal_deferral_reconciled_at": current.isoformat(),
                "last_review_required": reason == "candidate_rejected",
                "last_candidate_rejected": reason == "candidate_rejected",
            }
            if reason == "candidate_rejected":
                payload["last_quality_gate_reconciled_at"] = current.isoformat()
            job.update(payload)
            if _update_cron_state(job_id, payload):
                reconciled.append(job_id)
        if reconciled:
            self._save_jobs()
        return reconciled

    def _normalize_cron_expr(self, cron_expr: str):
        raw = (cron_expr or "").strip().lower()
        if not raw:
            return False, "", "❌ 缺少 cron 表達式。"

        if raw.startswith("daily"):
            try:
                if " " in raw:
                    time_part = raw.split()[1]
                    hour, minute = map(int, time_part.split(":"))
                    return True, f"{minute} {hour} * * *", ""
                return True, "0 9 * * *", ""
            except Exception:
                return False, "", "❌ 無效的時間格式。請使用 `daily HH:MM`"
        if raw == "hourly":
            return True, "0 * * * *", ""
        if raw == "every2h":
            return True, "0 */2 * * *", ""

        parts = raw.split()
        if len(parts) != 5:
            return False, "", "❌ cron 格式需為 5 欄位，例如 `0 */2 * * *`"
        return True, raw, ""

    def _field_match(self, expr: str, value: int, min_v: int, max_v: int) -> bool:
        field = (expr or "").strip()
        if field == "*":
            return True

        def _single_match(token: str) -> bool:
            tok = token.strip()
            if not tok:
                return False
            step = 1
            if "/" in tok:
                base, step_s = tok.split("/", 1)
                step = int(step_s)
                tok = base or "*"
                if step <= 0:
                    return False

            if tok == "*":
                start, end = min_v, max_v
            elif "-" in tok:
                a, b = tok.split("-", 1)
                start, end = int(a), int(b)
            else:
                single = int(tok)
                return single == value

            if value < start or value > end:
                return False
            return ((value - start) % step) == 0

        try:
            for part in field.split(","):
                if _single_match(part):
                    return True
            return False
        except Exception:
            return False

    def add_job(self, cron_expr, command, channel_id=None, description=""):
        """
        Add a new cron job.
        cron_expr example: "0 9 * * *" (Daily at 9:00)
        Supports simplified format: "daily 9:00", "hourly"
        """
        ok, cron_expr, err = self._normalize_cron_expr(str(cron_expr))
        if not ok:
            return err

        job_id = self._new_job_id()
        job = {
            "id": job_id,
            "cron": cron_expr,
            "command": command,
            "desc": description,
            "channel_id": channel_id,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True
        }
        self.jobs.append(job)
        self._save_jobs()
        return f"✅ 已新增排程: `{command}` ({cron_expr})"

    def ensure_job(self, cron_expr, command, channel_id=None, description=""):
        """
        Idempotent add/update by (command, description).
        """
        ok, cron_expr, err = self._normalize_cron_expr(str(cron_expr))
        if not ok:
            return {"success": False, "message": err}

        cmd = (command or "").strip()
        desc = (description or "").strip()
        if not cmd:
            return {"success": False, "message": "❌ command 不可空白。"}

        for job in self.jobs:
            if (job.get("command", "").strip() == cmd) and (job.get("desc", "").strip() == desc):
                job["cron"] = cron_expr
                job["channel_id"] = channel_id
                job["enabled"] = True
                self._save_jobs()
                return {
                    "success": True,
                    "action": "updated",
                    "job_id": job.get("id"),
                    "cron": cron_expr,
                    "command": cmd,
                    "desc": desc,
                }

        add_msg = self.add_job(cron_expr, cmd, channel_id=channel_id, description=desc)
        created = next((j for j in reversed(self.jobs) if j.get("command") == cmd and j.get("desc") == desc), None)
        return {
            "success": True,
            "action": "created",
            "job_id": created.get("id") if created else "",
            "cron": cron_expr,
            "command": cmd,
            "desc": desc,
            "message": add_msg,
        }

    def remove_job(self, job_id):
        """Remove a job by ID."""
        original_len = len(self.jobs)
        self.jobs = [j for j in self.jobs if j["id"] != job_id]
        if len(self.jobs) < original_len:
            self._save_jobs()
            return f"✅ 已刪除任務: `{job_id}`"
        return f"❌ 找不到任務: `{job_id}`"

    def list_jobs(self):
        """List all active jobs."""
        if not self.jobs:
            return "📭 目前沒有排程任務。"
        
        report = "📅 **自動化排程清單**\n\n"
        for j in self.jobs:
            status = "🟢" if j["enabled"] else "🔴"
            last = j["last_run"] or "Never"
            report += f"{status} **{j['desc'] or '未命名'}** (`{j['id']}`)\n"
            report += f"   - ⏰ 時間: `{j['cron']}`\n"
            report += f"   - 🤖 指令: `{j['command']}`\n"
            report += f"   - 🕒 上次執行: {last}\n\n"
        return report

    def check_due_jobs(self):
        """
        Check which jobs are due to run.
        Returns a list of due jobs.
        Updates last_run timestamp.
        """
        self._hot_reload_if_changed()
        now = datetime.now()
        due_jobs = []
        state_updates: Dict[str, Dict[str, Any]] = {}
        current_minute_str = now.strftime("%Y-%m-%d %H:%M")
        
        for job in self.jobs:
            if not job.get("enabled", True):
                continue

            # Cron parsing logic
            try:
                parts = job["cron"].split()
                if len(parts) != 5: continue
                
                min_f, hour_f, day_f, month_f, dow_f = parts
                
                cron_dow = (now.weekday() + 1) % 7  # Python Mon=0..Sun=6 -> cron Sun=0..Sat=6
                is_due = (
                    self._field_match(min_f, now.minute, 0, 59)
                    and self._field_match(hour_f, now.hour, 0, 23)
                    and self._field_match(day_f, now.day, 1, 31)
                    and self._field_match(month_f, now.month, 1, 12)
                    and self._field_match(dow_f, cron_dow, 0, 6)
                )
                
                # Check if already ran this minute
                last_run_minute = job.get("last_run_minute")
                
                if is_due and last_run_minute != current_minute_str:
                    due_jobs.append(job)
                    payload = {
                        "last_run": now.isoformat(),
                        "last_run_minute": current_minute_str,
                        "last_dispatch_at": now.isoformat(),
                        "last_dispatch_minute": current_minute_str,
                    }
                    job.update(payload)
                    job_id = str(job.get("id") or "").strip()
                    if job_id:
                        state_updates[job_id] = payload

            except Exception as e:
                logger.error(f"Error checking job {job['id']}: {e}")
                continue
        
        if due_jobs:
            _update_cron_states(state_updates)
            self._save_jobs()

        return due_jobs

    def peek_due_jobs(self):
        """Return due jobs without claiming or mutating their dispatch state.

        V3 may intentionally phase-delay a due occurrence.  Claiming here
        would create a crash window where a daemon restart loses the in-memory
        pending item but catch-up believes it already ran.  V3 claims only when
        its executor actually accepts the job.
        """
        self._hot_reload_if_changed()
        now = datetime.now()
        current_minute_str = now.strftime("%Y-%m-%d %H:%M")
        due_jobs = []
        for job in self.jobs:
            if not job.get("enabled", True):
                continue
            try:
                parts = job["cron"].split()
                if len(parts) != 5:
                    continue
                min_f, hour_f, day_f, month_f, dow_f = parts
                cron_dow = (now.weekday() + 1) % 7
                is_due = (
                    self._field_match(min_f, now.minute, 0, 59)
                    and self._field_match(hour_f, now.hour, 0, 23)
                    and self._field_match(day_f, now.day, 1, 31)
                    and self._field_match(month_f, now.month, 1, 12)
                    and self._field_match(dow_f, cron_dow, 0, 6)
                )
                last_dispatch = job.get("last_dispatch_minute") or job.get("last_run_minute")
                if is_due and last_dispatch != current_minute_str:
                    occurrence = dict(job)
                    occurrence["_magi_due_at"] = now.replace(
                        second=0, microsecond=0
                    ).isoformat()
                    due_jobs.append(occurrence)
            except Exception as exc:
                logger.error("Error peeking job %s: %s", job.get("id"), exc)
        return due_jobs

    def get_missed_jobs(
        self,
        catchup_window_hours: int = 8,
        min_hour: int = 6,
        *,
        include_default_skips: bool = False,
        end_grace_minutes: int = 2,
    ) -> list:
        """
        Return jobs that were due in the past catchup_window_hours but were NOT executed.

        Called once at startup (2nd scheduler loop, ~60s after start) to catch up jobs
        missed while MAGI was offline (kernel panic, restart, maintenance, etc.).

        Rules:
        - Looks back at most catchup_window_hours (default 8h) from now
        - Does NOT look earlier than today's min_hour:00 (default 06:00) to avoid
          re-running nightly/overnight jobs at the wrong time
        - Respects per-job ``"no_catchup": true`` flag for timing-sensitive jobs
        - Each job appears at most once (the most recent missed occurrence)
        - Skips jobs whose ``last_run_minute >= most_recent_due`` (already ran)

        Args:
            catchup_window_hours: How many hours back to search (env: MAGI_CRON_CATCHUP_HOURS)
            min_hour: Skip jobs scheduled before this hour today (env: MAGI_CRON_CATCHUP_MIN_HOUR)

        Returns:
            List of job dicts sorted chronologically (oldest missed first).
        """
        from datetime import timedelta
        self._hot_reload_if_changed()
        now = datetime.now()

        # Effective search window: [effective_start, window_end]
        # Leave a 2-minute grace at the end to avoid racing with check_due_jobs.
        window_end = now - timedelta(minutes=max(0, int(end_grace_minutes)))
        window_back = now - timedelta(hours=catchup_window_hours)
        today_floor = now.replace(hour=min_hour, minute=0, second=0, microsecond=0)
        effective_start = max(window_back, today_floor)

        if window_end <= effective_start:
            return []

        total_minutes = int((window_end - effective_start).total_seconds() / 60) + 1
        missed = []  # list of (due_datetime, job_dict)

        for job in self.jobs:
            if not job.get("enabled", True):
                continue
            if job.get("no_catchup", False):
                continue
            if not include_default_skips and str(job.get("id") or "") in _catchup_skip_ids():
                continue
            try:
                parts = job["cron"].split()
                if len(parts) != 5:
                    continue
                min_f, hour_f, day_f, month_f, dow_f = parts
            except Exception:
                continue

            # Walk backwards minute-by-minute to find the most recent occurrence.
            most_recent_due = None
            check_dt = window_end.replace(second=0, microsecond=0)
            for _ in range(total_minutes + 1):
                if check_dt < effective_start:
                    break
                cron_dow = (check_dt.weekday() + 1) % 7  # cron Sun=0..Sat=6
                if (self._field_match(min_f, check_dt.minute, 0, 59)
                        and self._field_match(hour_f, check_dt.hour, 0, 23)
                        and self._field_match(day_f, check_dt.day, 1, 31)
                        and self._field_match(month_f, check_dt.month, 1, 12)
                        and self._field_match(dow_f, cron_dow, 0, 6)):
                    most_recent_due = check_dt
                    break
                check_dt -= timedelta(minutes=1)

            if most_recent_due is None:
                continue

            # Skip if the job already ran at or after this occurrence.
            due_str = most_recent_due.strftime("%Y-%m-%d %H:%M")
            last_dispatch_minute = job.get("last_dispatch_minute") or job.get("last_run_minute")
            if last_dispatch_minute and last_dispatch_minute >= due_str:
                continue

            occurrence = dict(job)
            occurrence["_magi_due_at"] = most_recent_due.isoformat()
            missed.append((most_recent_due, occurrence))
            logger.debug(
                "🔄 Catch-up candidate: %s (due %s, last_dispatch_minute=%s)",
                job.get("id"), due_str, last_dispatch_minute,
            )

        # Sort oldest-first so jobs execute in their natural chronological order.
        missed.sort(key=lambda x: x[0])
        return [j for _, j in missed]

    def get_missed_jobs_v3(self, catchup_window_hours: int = 8) -> list:
        """Recover every unclaimed V3 occurrence, including overnight jobs."""
        return self.get_missed_jobs(
            catchup_window_hours,
            0,
            include_default_skips=True,
            end_grace_minutes=0,
        )

if __name__ == "__main__":
    s = CronScheduler()
    print(s.list_jobs())

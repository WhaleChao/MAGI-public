"""Hash-bound V3-only schedule dispatch policy.

The V2 cron definitions remain unchanged.  V3 applies a small phase offset and
three isolated lanes.  Two light slots and a two-slot shared heavy budget let
batch/maintenance work make progress without starving health or interactive
support jobs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


POLICY_PATH = Path("config/v3_schedule_dispatch_policy.json")
LANES = ("light", "batch", "maintenance")

# Only these jobs are explicitly allowed to collapse a pending occurrence to
# the newest snapshot.  Everything else must retain each distinct scheduled
# occurrence so a restart or a busy lane cannot silently lose work.
LEGACY_DURABLE_BACKLOG_COALESCING_JOB_IDS = frozenset(
    {
        "job_drive_case_sync_all_files",
        "job_legacy_judgment_resummary_quality",
    }
)
DURABLE_BACKLOG_COALESCING_JOB_IDS = frozenset(
    {
        "job_drive_case_sync_all_files",
        "job_business_module_live_check",
        "job_business_readiness_snapshot",
        "job_disk_cleanup_healthcheck",
        "job_laf_condition_dedup_scan",
        "job_laf_nightly_audit",
        "job_laf_portal_new_files_scan",
        "job_function_health_index",
        "job_tailscale_funnel_healthcheck",
        "job_worldmonitor_intel",
    }
)
PENDING_POLICY_LEGACY = "latest_occurrence_wins"
PENDING_POLICY_QUEUE_ALL = "queue_all_non_durable_except_declared_durable_latest"
DEFAULT_MAX_PENDING_OCCURRENCES_PER_JOB = 256


class CronDispatchPolicyError(RuntimeError):
    """The V3 dispatch policy is missing, stale, or unsafe."""


def _read_stable_regular_file(path: Path, *, label: str) -> bytes:
    """Read one regular file without following a final-component symlink."""

    try:
        declared = path.lstat()
    except OSError as exc:
        raise CronDispatchPolicyError(f"V3 {label} is unreadable: {exc}") from exc
    if stat.S_ISLNK(declared.st_mode) or not stat.S_ISREG(declared.st_mode):
        raise CronDispatchPolicyError(f"V3 {label} must be a non-symlink regular file")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CronDispatchPolicyError(f"V3 {label} is unreadable: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise CronDispatchPolicyError(f"V3 {label} must be a regular file")
        identity_fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
        if any(getattr(declared, field) != getattr(before, field) for field in identity_fields):
            raise CronDispatchPolicyError(f"V3 {label} changed before it was read")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
    except OSError as exc:
        raise CronDispatchPolicyError(f"V3 {label} is unreadable: {exc}") from exc
    finally:
        os.close(descriptor)

    if any(getattr(before, field) != getattr(after, field) for field in identity_fields):
        raise CronDispatchPolicyError(f"V3 {label} changed while it was being read")
    payload = b"".join(chunks)
    if len(payload) != before.st_size:
        raise CronDispatchPolicyError(f"V3 {label} changed while it was being read")
    return payload


def _bound_cron_snapshot(root: Path) -> tuple[bytes, str, str]:
    declared_path = os.environ.get("MAGI_CRON_JOBS_FILE", "")
    declared_sha = os.environ.get("MAGI_CRON_JOBS_SHA256", "")
    declared_source_sha = os.environ.get("MAGI_CRON_JOBS_SOURCE_SHA256", "")
    declared = (declared_path, declared_sha, declared_source_sha)
    if any(declared) and not all(declared):
        raise CronDispatchPolicyError("V3 external cron snapshot binding is incomplete")

    if declared_path:
        cron_path = Path(declared_path)
        if not cron_path.is_absolute():
            raise CronDispatchPolicyError("V3 external cron snapshot path must be absolute")
        if re.fullmatch(r"[0-9a-f]{64}", declared_sha) is None:
            raise CronDispatchPolicyError("V3 external cron snapshot SHA-256 is invalid")
        if re.fullmatch(r"[0-9a-f]{64}", declared_source_sha) is None:
            raise CronDispatchPolicyError("V3 cron source SHA-256 is invalid")
        payload = _read_stable_regular_file(cron_path, label="external cron snapshot")
        actual_sha = hashlib.sha256(payload).hexdigest()
        if actual_sha != declared_sha:
            raise CronDispatchPolicyError("V3 external cron snapshot SHA-256 mismatched")
        return payload, actual_sha, declared_source_sha

    payload = _read_stable_regular_file(root / "cron_jobs.json", label="cron job manifest")
    source_sha = hashlib.sha256(payload).hexdigest()
    return payload, source_sha, source_sha


@dataclass(frozen=True, slots=True)
class CronDispatchPolicy:
    lane_caps: Mapping[str, int]
    shared_caps: Mapping[str, tuple[frozenset[str], int]]
    batch_job_ids: frozenset[str]
    phase_delay_seconds: Mapping[str, int]
    policy_sha256: str
    cron_jobs_sha256: str
    coalescing_mode: str = PENDING_POLICY_LEGACY
    durable_backlog_coalescing_job_ids: frozenset[str] = (
        LEGACY_DURABLE_BACKLOG_COALESCING_JOB_IDS
    )
    max_pending_occurrences_per_job: int = 1

    @property
    def max_workers(self) -> int:
        return 4

    def can_start_lane(self, lane: str, active_lanes: list[str] | tuple[str, ...]) -> bool:
        if lane not in self.lane_caps or len(active_lanes) >= self.max_workers:
            return False
        if sum(value == lane for value in active_lanes) >= int(self.lane_caps[lane]):
            return False
        for lanes, cap in self.shared_caps.values():
            if lane in lanes and sum(value in lanes for value in active_lanes) >= cap:
                return False
        return True

    def lane_for(self, job: Mapping[str, Any]) -> str:
        job_id = str(job.get("id") or "")
        if job.get("long_job") is True or job.get("resource_guarded") is True:
            return "maintenance"
        if job_id in self.batch_job_ids:
            return "batch"
        return "light"

    def delay_for(self, job: Mapping[str, Any]) -> int:
        return int(self.phase_delay_seconds.get(str(job.get("id") or ""), 0))

    @property
    def queue_all_non_durable(self) -> bool:
        return self.coalescing_mode == PENDING_POLICY_QUEUE_ALL

    def coalesces_pending(self, job_id: str) -> bool:
        return str(job_id or "") in self.durable_backlog_coalescing_job_ids


def load_cron_dispatch_policy(release_root: Path) -> CronDispatchPolicy:
    root = release_root.expanduser().resolve()
    policy_path = root / POLICY_PATH
    try:
        policy_payload = _read_stable_regular_file(policy_path, label="cron dispatch policy")
        cron_payload, cron_sha, cron_source_sha = _bound_cron_snapshot(root)
        policy = json.loads(policy_payload.decode("utf-8"))
        jobs = json.loads(cron_payload.decode("utf-8"))
    except CronDispatchPolicyError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CronDispatchPolicyError(f"V3 cron dispatch policy is unreadable: {exc}") from exc
    if not isinstance(policy, dict) or policy.get("schema_version") != 1:
        raise CronDispatchPolicyError("V3 cron dispatch policy schema is invalid")
    if not isinstance(jobs, list) or not jobs:
        raise CronDispatchPolicyError("V3 cron job manifest is invalid")
    if policy.get("cron_jobs_sha256") != cron_source_sha:
        raise CronDispatchPolicyError("V3 cron dispatch policy binding drifted")
    if policy.get("queue_order") != "earliest_latest_start_then_scheduled_time":
        raise CronDispatchPolicyError("V3 cron queue order is unsafe")
    same_job_pending = policy.get("same_job_pending")
    if same_job_pending not in {PENDING_POLICY_LEGACY, PENDING_POLICY_QUEUE_ALL}:
        raise CronDispatchPolicyError("V3 cron same-job pending policy is unsafe")
    if policy.get("max_workers") != 4:
        raise CronDispatchPolicyError("V3 cron total worker cap must remain four")
    raw_slots = policy.get("lane_caps")
    if not isinstance(raw_slots, dict) or tuple(raw_slots) != LANES:
        raise CronDispatchPolicyError("V3 cron lane definitions are invalid")
    slots = {str(key): int(value) for key, value in raw_slots.items()}
    if slots != {"light": 2, "batch": 2, "maintenance": 2}:
        raise CronDispatchPolicyError("V3 cron lane caps drifted")
    raw_shared = policy.get("shared_caps")
    if not isinstance(raw_shared, dict) or set(raw_shared) != {"heavy"}:
        raise CronDispatchPolicyError("V3 cron shared lane cap is invalid")
    heavy = raw_shared["heavy"]
    if (
        not isinstance(heavy, dict)
        or heavy.get("lanes") != ["batch", "maintenance"]
        or heavy.get("slots") != 2
    ):
        raise CronDispatchPolicyError("V3 cron heavy shared cap drifted")
    shared = {"heavy": (frozenset({"batch", "maintenance"}), 2)}
    enabled = {
        str(job.get("id") or "")
        for job in jobs
        if isinstance(job, dict) and job.get("enabled") is True
    }
    raw_durable = policy.get("durable_backlog_coalescing_job_ids")
    if same_job_pending == PENDING_POLICY_QUEUE_ALL:
        if (
            not isinstance(raw_durable, list)
            or len(raw_durable) != len(set(raw_durable))
            or not raw_durable
        ):
            raise CronDispatchPolicyError(
                "V3 durable backlog coalescing allowlist is invalid"
            )
        durable = frozenset(str(value) for value in raw_durable)
        if not durable.issubset(enabled):
            raise CronDispatchPolicyError(
                "V3 durable backlog coalescing allowlist references unknown jobs"
            )
        raw_pending_limit = policy.get(
            "max_pending_occurrences_per_job",
            DEFAULT_MAX_PENDING_OCCURRENCES_PER_JOB,
        )
        if (
            isinstance(raw_pending_limit, bool)
            or not isinstance(raw_pending_limit, int)
            or not 2 <= raw_pending_limit <= 4096
        ):
            raise CronDispatchPolicyError("V3 pending occurrence queue bound is invalid")
        pending_limit = raw_pending_limit
    else:
        durable = frozenset(
            str(value) for value in raw_durable
        ) if isinstance(raw_durable, list) else LEGACY_DURABLE_BACKLOG_COALESCING_JOB_IDS
        if not durable.issubset(enabled):
            raise CronDispatchPolicyError(
                "V3 durable backlog coalescing allowlist references unknown jobs"
            )
        pending_limit = 1
    expected_enabled = policy.get("enabled_jobs_expected")
    if (
        type(expected_enabled) is not int
        or expected_enabled <= 0
        or "" in enabled
        or len(enabled) != expected_enabled
    ):
        raise CronDispatchPolicyError("V3 cron enabled-job inventory drifted")
    raw_batch = policy.get("batch_job_ids")
    if not isinstance(raw_batch, list) or len(raw_batch) != len(set(raw_batch)):
        raise CronDispatchPolicyError("V3 cron batch job list is invalid")
    batch = frozenset(str(value) for value in raw_batch)
    if not batch or not batch.issubset(enabled):
        raise CronDispatchPolicyError("V3 cron batch job list references unknown jobs")
    raw_delays = policy.get("phase_delay_seconds")
    if not isinstance(raw_delays, dict) or not set(raw_delays).issubset(enabled):
        raise CronDispatchPolicyError("V3 cron phase delay map references unknown jobs")
    delays: dict[str, int] = {}
    for job_id, raw in raw_delays.items():
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 6 * 3600:
            raise CronDispatchPolicyError("V3 cron phase delay is out of bounds")
        if raw:
            delays[str(job_id)] = raw
    return CronDispatchPolicy(
        lane_caps=slots,
        shared_caps=shared,
        batch_job_ids=batch,
        phase_delay_seconds=delays,
        policy_sha256=hashlib.sha256(policy_payload).hexdigest(),
        cron_jobs_sha256=cron_sha,
        coalescing_mode=str(same_job_pending),
        durable_backlog_coalescing_job_ids=durable,
        max_pending_occurrences_per_job=pending_limit,
    )

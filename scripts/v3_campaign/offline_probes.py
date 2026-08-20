"""Measured, offline-only schedule and fault probes for the V3 campaign.

The probes intentionally use temporary files and owned child processes only.
They never import the legacy scheduler, execute cron commands, open sockets, or
touch the configured V3/live state directories.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from magi_v3.dispatcher import DurableDispatcher, VerifiedCompletion
from magi_v3.errors import LedgerError
from magi_v3.ledger import JobLedger, JobSpec
from magi_v3.resource import GlobalResourceGovernor, ResourceSnapshot
from magi_v3.state import JobStatus
from magi_v3.supervisor import WorkerSpec, WorkerSupervisor

SCHEMA_VERSION = 1
SCHEDULE_WORKLOAD = "seven_day_schedule_10x_arrival_2x_duration_replay"
FAULT_WORKLOAD = "fault_injection"
_TAIPEI = ZoneInfo("Asia/Taipei")
_REPLAY_START = datetime(2026, 7, 13, tzinfo=_TAIPEI)
_REPLAY_MINUTES = 7 * 24 * 60
_ARRIVAL_MULTIPLIER = 10
_DURATION_MULTIPLIER = 2.0
_EXPECTED_CRON_JOBS = 104
_EXPECTED_ENABLED_CRON_JOBS = 94

_LIGHT_CLAIM = {
    "memory_mb": 8,
    "metal_mb": 0,
    "cpu_percent": 5,
    "disk_io": "none",
    "nas_io": "none",
    "network": "none",
    "browser_tokens": 0,
}
_MAINTENANCE_CLAIM = {
    **_LIGHT_CLAIM,
    "memory_mb": 16,
    "cpu_percent": 10,
    "disk_io": "light",
}


class OfflineProbeError(RuntimeError):
    """Raised when bound input or measured recovery evidence is invalid."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bound_cron_jobs(source_root: Path) -> tuple[list[dict[str, Any]], str]:
    """Load the hash-bound campaign cron snapshot, or the source snapshot in tests."""

    certifying = os.environ.get("MAGI_V3_OFFLINE_CERTIFICATION") == "1"
    raw_path = os.environ.get("MAGI_CRON_JOBS_FILE")
    path = Path(raw_path) if raw_path else source_root / "cron_jobs.json"
    if certifying and (not raw_path or not path.is_absolute()):
        raise OfflineProbeError("certifying replay requires an absolute cron snapshot")
    try:
        before = path.lstat()
        payload = json.loads(path.read_text(encoding="utf-8"))
        after = path.lstat()
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OfflineProbeError(f"cron snapshot unreadable: {exc}") from exc
    if path.is_symlink() or not path.is_file():
        raise OfflineProbeError("cron snapshot must be a regular non-symlink file")
    signature = lambda value: (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_mode,
    )
    if signature(before) != signature(after):
        raise OfflineProbeError("cron snapshot changed while being read")
    digest = _sha256(path)
    expected = os.environ.get("MAGI_CRON_JOBS_SHA256")
    if certifying and expected != digest:
        raise OfflineProbeError("cron snapshot SHA-256 binding mismatch")
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise OfflineProbeError("cron snapshot must contain an array of objects")
    ids = [str(item.get("id") or "").strip() for item in payload]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise OfflineProbeError("cron snapshot contains missing or duplicate ids")
    return [dict(item) for item in payload], digest


def _cron_values(field: str, minimum: int, maximum: int) -> frozenset[int]:
    values: set[int] = set()
    for raw_part in field.split(","):
        part = raw_part.strip()
        if not part:
            raise OfflineProbeError(f"empty cron field component: {field}")
        step = 1
        base = part
        if "/" in part:
            base, raw_step = part.split("/", 1)
            try:
                step = int(raw_step)
            except ValueError as exc:
                raise OfflineProbeError(f"invalid cron step: {part}") from exc
            if step < 1:
                raise OfflineProbeError(f"invalid cron step: {part}")
        if base == "*":
            start, stop = minimum, maximum
        elif "-" in base:
            raw_start, raw_stop = base.split("-", 1)
            try:
                start, stop = int(raw_start), int(raw_stop)
            except ValueError as exc:
                raise OfflineProbeError(f"invalid cron range: {part}") from exc
        else:
            try:
                start = stop = int(base)
            except ValueError as exc:
                raise OfflineProbeError(f"invalid cron value: {part}") from exc
        if start < minimum or stop > maximum or start > stop:
            raise OfflineProbeError(f"cron value outside [{minimum},{maximum}]: {part}")
        values.update(range(start, stop + 1, step))
    if not values:
        raise OfflineProbeError(f"cron field has no values: {field}")
    return frozenset(values)


def _cron_matches(expression: str, instant: datetime) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        raise OfflineProbeError(f"cron expression must contain five fields: {expression}")
    minute, hour, day, month, weekday = (
        _cron_values(fields[0], 0, 59),
        _cron_values(fields[1], 0, 23),
        _cron_values(fields[2], 1, 31),
        _cron_values(fields[3], 1, 12),
        _cron_values(fields[4], 0, 6),
    )
    cron_weekday = (instant.weekday() + 1) % 7
    day_match = instant.day in day
    weekday_match = cron_weekday in weekday
    if fields[2] != "*" and fields[4] != "*":
        calendar_match = day_match or weekday_match
    else:
        calendar_match = day_match and weekday_match
    return (
        instant.minute in minute
        and instant.hour in hour
        and instant.month in month
        and calendar_match
    )


def _worker_class(job: Mapping[str, Any]) -> str:
    return "maintenance" if job.get("long_job") is True or job.get("resource_guarded") is True else "light"


def _timeout(job: Mapping[str, Any]) -> int:
    raw = job.get("timeout_sec", 600)
    value = int(raw) if raw not in (None, "") else 600
    return max(1, min(86400, value))


def _replay_profile() -> tuple[str, datetime]:
    profile_id = os.environ.get("MAGI_V3_VALIDATION_PROFILE_ID", "default_ordinary_week")
    raw_start = os.environ.get("MAGI_V3_REPLAY_START_LOCAL")
    if raw_start is None:
        return profile_id, _REPLAY_START
    try:
        parsed = datetime.fromisoformat(raw_start)
    except ValueError as exc:
        raise OfflineProbeError("validation replay start is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise OfflineProbeError("validation replay start must include a timezone")
    normalized = parsed.astimezone(_TAIPEI)
    if normalized.second or normalized.microsecond:
        raise OfflineProbeError("validation replay start must be minute-aligned")
    return profile_id, normalized


def _base_arrivals(
    jobs: Iterable[Mapping[str, Any]], replay_start: datetime
) -> list[tuple[datetime, int, Mapping[str, Any]]]:
    enabled = [(index, job) for index, job in enumerate(jobs) if job.get("enabled") is True]
    events: list[tuple[datetime, int, Mapping[str, Any]]] = []
    for minute_offset in range(_REPLAY_MINUTES):
        instant = replay_start + timedelta(minutes=minute_offset)
        for index, job in enabled:
            expression = str(job.get("cron") or "").strip()
            if _cron_matches(expression, instant):
                events.append((instant, index, job))
    return events


def _percentile_95(values: list[float]) -> float:
    if not values:
        raise OfflineProbeError("latency calibration produced no observations")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def _calibrate_ledger(
    jobs: list[dict[str, Any]], path: Path, replay_start: datetime
) -> dict[str, float]:
    ledger = JobLedger(path)
    ledger.initialize()
    samples: dict[str, list[float]] = {"light": [], "maintenance": []}
    base = replay_start
    for index, job in enumerate(item for item in jobs if item.get("enabled") is True):
        worker_class = _worker_class(job)
        started = time.perf_counter()
        ledger.create_job(
            JobSpec(
                job_id=f"calibration-{index:03d}",
                capability="offline_schedule_replay",
                operation="calibrate",
                worker_class=worker_class,
                side_effect_class="read_only",
                input={"definition_index": index},
                scheduled_for=base,
                timeout_sec=_timeout(job),
                resource_claim=(
                    _MAINTENANCE_CLAIM if worker_class == "maintenance" else _LIGHT_CLAIM
                ),
            ),
            now=base,
        )
        lease = ledger.lease_next(
            "offline-calibration",
            worker_classes=[worker_class],
            now=base,
        )
        if lease is None:
            raise OfflineProbeError("calibration job was not leaseable")
        ledger.mark_running(
            lease.token,
            owner_id=lease.owner_id,
            attempt_number=lease.attempt_number,
            now=base,
        )
        ledger._commit_worker_result(
            lease.token,
            JobStatus.SUCCEEDED,
            owner_id=lease.owner_id,
            attempt_number=lease.attempt_number,
            result={"artifacts": [{"kind": "offline_calibration", "uri": "offline://schedule"}]},
            business_completed=True,
            now=base,
        )
        samples[worker_class].append((time.perf_counter() - started) * 1000)
    fallback = _percentile_95(samples["light"] + samples["maintenance"])
    return {
        worker_class: max(1.0, _percentile_95(values) if values else fallback)
        for worker_class, values in samples.items()
    }


def _simulate_deadlines(
    events: list[tuple[datetime, int, Mapping[str, Any]]],
    calibrated_ms: Mapping[str, float],
    replay_start: datetime,
) -> dict[str, int | float]:
    policy = GlobalResourceGovernor().policy
    slots = {
        "light": [0.0] * policy.max_light,
        "maintenance": [0.0] * policy.max_heavy,
    }
    latest_start_misses = 0
    deadline_misses = 0
    max_queue_delay = 0.0
    for instant, _index, job in events:
        arrival = (instant - replay_start).total_seconds()
        worker_class = _worker_class(job)
        heap = slots[worker_class]
        available = heapq.heappop(heap)
        started = max(arrival, available)
        duration = calibrated_ms[worker_class] / 1000 * _DURATION_MULTIPLIER
        finished = started + duration
        heapq.heappush(heap, finished)
        timeout = _timeout(job)
        latest_start = arrival + timeout
        deadline = latest_start + timeout
        latest_start_misses += int(started > latest_start)
        deadline_misses += int(finished > deadline)
        max_queue_delay = max(max_queue_delay, started - arrival)
    return {
        "latest_start_misses": latest_start_misses,
        "deadline_misses": deadline_misses,
        "max_queue_delay_ms": round(max_queue_delay * 1000, 6),
        "governor_light_slots": policy.max_light,
        "governor_heavy_slots": policy.max_heavy,
    }


def _simulate_production_job_duration_deadlines(
    events: list[tuple[datetime, int, Mapping[str, Any]]],
    profiles: Mapping[str, Mapping[str, Any]],
    replay_start: datetime,
) -> dict[str, int | float]:
    """Replay the same arrivals with each job's bound production duration.

    This remains separate from the ledger-lifecycle calibration because the
    latter measures dispatcher persistence overhead, not business job runtime.
    Sparse duration profiles are allowed as engineering input but their
    coverage metadata prevents this result from becoming certifying evidence.
    """

    policy = GlobalResourceGovernor().policy
    slots = {
        "light": [0.0] * policy.max_light,
        "maintenance": [0.0] * policy.max_heavy,
    }
    latest_start_misses = 0
    deadline_misses = 0
    max_queue_delay = 0.0
    max_scaled_duration = 0.0
    scaled_demand = {"light": 0.0, "maintenance": 0.0}
    latest_start_misses_by_job: dict[str, int] = {}
    deadline_misses_by_job: dict[str, int] = {}
    for instant, _index, job in events:
        job_id = str(job.get("id") or "")
        profile = profiles.get(job_id)
        if not isinstance(profile, Mapping):
            raise OfflineProbeError(
                f"production duration replay lacks profile for {job_id}"
            )
        raw_duration = profile.get("duration_seconds")
        if (
            isinstance(raw_duration, bool)
            or not isinstance(raw_duration, (int, float))
            or raw_duration <= 0
        ):
            raise OfflineProbeError(
                f"production duration replay has invalid profile for {job_id}"
            )
        arrival = (instant - replay_start).total_seconds()
        worker_class = _worker_class(job)
        heap = slots[worker_class]
        available = heapq.heappop(heap)
        started = max(arrival, available)
        duration = float(raw_duration) * _DURATION_MULTIPLIER
        scaled_demand[worker_class] += duration
        finished = started + duration
        heapq.heappush(heap, finished)
        timeout = _timeout(job)
        latest_start = arrival + timeout
        deadline = latest_start + timeout
        start_missed = int(started > latest_start)
        deadline_missed = int(finished > deadline)
        latest_start_misses += start_missed
        deadline_misses += deadline_missed
        latest_start_misses_by_job[job_id] = (
            latest_start_misses_by_job.get(job_id, 0) + start_missed
        )
        deadline_misses_by_job[job_id] = (
            deadline_misses_by_job.get(job_id, 0) + deadline_missed
        )
        max_queue_delay = max(max_queue_delay, started - arrival)
        max_scaled_duration = max(max_scaled_duration, duration)
    virtual_seconds = _REPLAY_MINUTES * 60
    available_slot_seconds = {
        "light": float(virtual_seconds * policy.max_light),
        "maintenance": float(virtual_seconds * policy.max_heavy),
    }
    return {
        "latest_start_misses": latest_start_misses,
        "deadline_misses": deadline_misses,
        "jobs_with_latest_start_misses": sum(
            count > 0 for count in latest_start_misses_by_job.values()
        ),
        "jobs_with_deadline_misses": sum(
            count > 0 for count in deadline_misses_by_job.values()
        ),
        "latest_start_misses_by_job": dict(
            sorted(latest_start_misses_by_job.items())
        ),
        "deadline_misses_by_job": dict(sorted(deadline_misses_by_job.items())),
        "max_queue_delay_seconds": round(max_queue_delay, 6),
        "max_scaled_job_duration_seconds": round(max_scaled_duration, 6),
        "scaled_demand_seconds_by_worker_class": {
            key: round(value, 6) for key, value in scaled_demand.items()
        },
        "available_slot_seconds_by_worker_class": available_slot_seconds,
        "demand_to_capacity_ratio_by_worker_class": {
            key: round(scaled_demand[key] / available_slot_seconds[key], 6)
            for key in scaled_demand
        },
        "governor_light_slots": policy.max_light,
        "governor_heavy_slots": policy.max_heavy,
    }


def _with_timeout_bound_duration_fallbacks(
    jobs: Iterable[Mapping[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Complete replay inputs without converting missing evidence into a p95 claim."""

    completed = {job_id: dict(profile) for job_id, profile in profiles.items()}
    fallback_profiles: list[dict[str, Any]] = []
    for job in jobs:
        if job.get("enabled") is not True:
            continue
        job_id = str(job.get("id") or "")
        if not job_id or job_id in completed:
            continue
        timeout_seconds = float(_timeout(job))
        fallback = {
            "duration_seconds": timeout_seconds,
            "duration_basis": "configured_timeout_bound_noncertifying_fallback",
            "successful_sample_count": 0,
            "certifying_p95": False,
        }
        completed[job_id] = fallback
        fallback_profiles.append({"job_id": job_id, **fallback})
    fallback_profiles.sort(key=lambda row: str(row["job_id"]))
    receipt_sha256 = hashlib.sha256(
        json.dumps(
            fallback_profiles,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return completed, {
        "policy": "configured_job_timeout_bound_noncertifying_v1",
        "certifying": False,
        "used": bool(fallback_profiles),
        "fallback_jobs": len(fallback_profiles),
        "fallback_job_ids": [row["job_id"] for row in fallback_profiles],
        "duration_multiplier_applied_after_fallback": True,
        "fallback_profiles": fallback_profiles,
        "fallback_profiles_sha256": receipt_sha256,
    }


def _production_duration_replay_certifying(
    duration_coverage: Mapping[str, Any],
    timeout_fallback: Mapping[str, Any],
    deadline_measurements: Mapping[str, Any],
) -> bool:
    """Fail closed whenever a synthetic duration fallback participated."""

    return bool(
        duration_coverage.get("certifying_p95_coverage") is True
        and timeout_fallback.get("used") is False
        and deadline_measurements.get("latest_start_misses") == 0
        and deadline_measurements.get("deadline_misses") == 0
    )


def run_schedule_replay(source_root: Path, workdir: Path) -> dict[str, Any]:
    started = time.perf_counter()
    jobs, cron_sha = bound_cron_jobs(source_root)
    profile_id, replay_start = _replay_profile()
    enabled = [job for job in jobs if job.get("enabled") is True]
    calibration = _calibrate_ledger(
        jobs, workdir / "schedule-calibration.sqlite3", replay_start
    )
    base_events = _base_arrivals(jobs, replay_start)
    replay_events = [
        event
        for event in base_events
        for _copy_index in range(_ARRIVAL_MULTIPLIER)
    ]
    deadline = _simulate_deadlines(replay_events, calibration, replay_start)
    # Import lazily to avoid the schedule-realism module's dependency on the
    # bound cron loader above.
    from scripts.v3_campaign.schedule_realism import bound_duration_replay_profiles

    duration_profiles, duration_coverage = bound_duration_replay_profiles(
        source_root, jobs, cron_sha
    )
    replay_duration_profiles, timeout_fallback = (
        _with_timeout_bound_duration_fallbacks(enabled, duration_profiles)
    )
    production_duration_deadline = _simulate_production_job_duration_deadlines(
        replay_events,
        replay_duration_profiles,
        replay_start,
    )
    duration_replay_certifying = _production_duration_replay_certifying(
        duration_coverage,
        timeout_fallback,
        production_duration_deadline,
    )

    ledger_path = workdir / "schedule-replay.sqlite3"
    ledger = JobLedger(ledger_path)
    ledger.initialize()
    for ordinal, (instant, definition_index, job) in enumerate(replay_events):
        worker_class = _worker_class(job)
        timeout = _timeout(job)
        ledger.create_job(
            JobSpec(
                job_id=f"replay-{ordinal:06d}",
                capability="offline_schedule_replay",
                operation="arrival",
                worker_class=worker_class,
                side_effect_class="none",
                input={"definition_index": definition_index},
                scheduled_for=instant,
                queue_ttl_sec=timeout,
                timeout_sec=timeout,
                resource_claim=(
                    _MAINTENANCE_CLAIM if worker_class == "maintenance" else _LIGHT_CLAIM
                ),
            ),
            now=replay_start,
        )

    with sqlite3.connect(ledger_path) as connection:
        persisted, distinct = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT job_id) FROM jobs"
        ).fetchone()
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0]).lower()
    reopened = JobLedger(ledger_path)
    recovered = reopened.ping()
    expected = len(replay_events)
    wall_seconds = time.perf_counter() - started
    measurements = {
        "cron_definitions": len(jobs),
        "enabled_cron_definitions": len(enabled),
        "validation_profile_id": profile_id,
        "replay_start_local": replay_start.isoformat(),
        "base_seven_day_arrivals": len(base_events),
        "replayed_arrivals": expected,
        "arrival_multiplier": _ARRIVAL_MULTIPLIER,
        "duration_multiplier": _DURATION_MULTIPLIER,
        "duration_basis": "measured_ledger_lifecycle_p95",
        "virtual_duration_seconds": _REPLAY_MINUTES * 60,
        "wall_duration_seconds": round(wall_seconds, 6),
        "acceleration_factor": round((_REPLAY_MINUTES * 60) / max(wall_seconds, 0.000001), 3),
        "calibrated_light_p95_ms": round(calibration["light"], 6),
        "calibrated_maintenance_p95_ms": round(calibration["maintenance"], 6),
        "persisted_jobs": int(persisted),
        "duplicate_jobs": int(persisted - distinct),
        "lost_jobs": int(expected - persisted),
        "recovered_jobs": int(persisted if recovered else 0),
        "latest_start_misses": int(deadline["latest_start_misses"]),
        "deadline_misses": int(deadline["deadline_misses"]),
        "max_queue_delay_ms": deadline["max_queue_delay_ms"],
        "governor_light_slots": deadline["governor_light_slots"],
        "governor_heavy_slots": deadline["governor_heavy_slots"],
        "journal_mode_wal": journal_mode == "wal",
        "integrity_check_ok": integrity == "ok",
        "reopen_ping_ok": recovered,
        "cron_jobs_sha256": cron_sha,
        "production_job_duration_replay": {
            "status": "passed" if duration_replay_certifying else "incomplete",
            "completion_claimed": duration_replay_certifying,
            "arrival_multiplier": _ARRIVAL_MULTIPLIER,
            "duration_multiplier": _DURATION_MULTIPLIER,
            "virtual_duration_seconds": _REPLAY_MINUTES * 60,
            "replayed_arrivals": expected,
            "duration_coverage": duration_coverage,
            "missing_duration_fallback": timeout_fallback,
            "deadline_measurements": production_duration_deadline,
            "eligible_to_clear_schedule_realism_blocker": duration_replay_certifying,
            "blocking_reasons": [
                reason
                for condition, reason in (
                    (
                        not duration_coverage["certifying_p95_coverage"],
                        "PRODUCTION_P95_SAMPLE_COVERAGE_INCOMPLETE",
                    ),
                    (
                        timeout_fallback["used"],
                        "PRODUCTION_DURATION_TIMEOUT_FALLBACK_USED",
                    ),
                    (
                        production_duration_deadline["latest_start_misses"] > 0,
                        "PRODUCTION_DURATION_LATEST_START_MISSES",
                    ),
                    (
                        production_duration_deadline["deadline_misses"] > 0,
                        "PRODUCTION_DURATION_DEADLINE_MISSES",
                    ),
                )
                if condition
            ],
        },
    }
    passed = (
        len(jobs) == _EXPECTED_CRON_JOBS
        and len(enabled) == _EXPECTED_ENABLED_CRON_JOBS
        and expected == len(base_events) * _ARRIVAL_MULTIPLIER
        and measurements["duplicate_jobs"] == 0
        and measurements["lost_jobs"] == 0
        and measurements["recovered_jobs"] == expected
        and measurements["latest_start_misses"] == 0
        and measurements["deadline_misses"] == 0
        and measurements["journal_mode_wal"] is True
        and measurements["integrity_check_ok"] is True
        and measurements["reopen_ping_ok"] is True
    )
    return _evidence(SCHEDULE_WORKLOAD, "measured_schedule_ledger_replay", measurements, passed)


def _atomic_bytes(path: Path, payload: bytes, fsync: Callable[[int], None] = os.fsync) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _sqlite_wal_fault(workdir: Path) -> dict[str, Any]:
    path = workdir / "fault-wal.sqlite3"
    ledger = JobLedger(path, busy_timeout_ms=5000)
    ledger.initialize()
    base = _REPLAY_START
    expected = 120

    def create(index: int) -> None:
        JobLedger(path, busy_timeout_ms=5000).create_job(
            JobSpec(
                job_id=f"wal-{index:03d}",
                capability="offline_fault",
                operation="wal_writer",
                worker_class="light",
                side_effect_class="none",
                input={"index": index},
                scheduled_for=base,
                timeout_sec=60,
                resource_claim=_LIGHT_CLAIM,
            ),
            now=base,
        )

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(create, range(expected)))
    with sqlite3.connect(path) as connection:
        count, distinct = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT job_id) FROM jobs"
        ).fetchone()
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        checkpoint = tuple(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
    reopened = JobLedger(path)
    passed = (
        count == expected
        and distinct == expected
        and reopened.ping()
        and journal_mode == "wal"
        and int(checkpoint[0]) == 0
    )
    return {
        "fault": "sqlite_wal_concurrent_reopen",
        "status": "passed" if passed else "failed",
        "attempted": expected,
        "committed": int(count),
        "duplicate": int(count - distinct),
        "loss": int(expected - count),
        "recovered": int(count if reopened.ping() else 0),
        "recovery_ms": round((time.perf_counter() - started) * 1000, 6),
        "journal_mode_wal": journal_mode == "wal",
        "writer_synchronous_policy": "NORMAL (JobLedger connection configuration)",
        "fault_scope": "concurrent SQLite WAL writers plus checkpoint and reopen; no process kill",
        "checkpoint_busy": int(checkpoint[0]),
    }


def _sqlite_disk_full_fault(workdir: Path) -> dict[str, Any]:
    path = workdir / "fault-disk-full.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA page_size=512")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("CREATE TABLE payloads(id INTEGER PRIMARY KEY, payload BLOB NOT NULL)")
    connection.commit()
    current_pages = int(connection.execute("PRAGMA page_count").fetchone()[0])
    connection.execute(f"PRAGMA max_page_count={current_pages + 12}")
    acknowledged = 0
    observed_full = False
    started = time.perf_counter()
    for index in range(100):
        try:
            connection.execute(
                "INSERT INTO payloads(payload) VALUES (?)",
                (bytes([index % 251]) * 1024,),
            )
            connection.commit()
            acknowledged += 1
        except sqlite3.OperationalError as exc:
            connection.rollback()
            observed_full = "full" in str(exc).lower()
            break
    connection.close()
    with sqlite3.connect(path) as reopened:
        recovered = int(reopened.execute("SELECT COUNT(*) FROM payloads").fetchone()[0])
        integrity = str(reopened.execute("PRAGMA integrity_check").fetchone()[0]).lower()
    return {
        "fault": "sqlite_bounded_disk_full",
        "status": "passed" if observed_full and recovered == acknowledged and integrity == "ok" else "failed",
        "attempted": acknowledged + int(observed_full),
        "committed": acknowledged,
        "duplicate": 0,
        "loss": int(acknowledged - recovered),
        "recovered": recovered,
        "recovery_ms": round((time.perf_counter() - started) * 1000, 6),
        "disk_full_observed": observed_full,
        "fault_scope": "SQLite max_page_count limit; not filesystem ENOSPC",
        "real_filesystem_enospc": False,
        "integrity_check_ok": integrity == "ok",
    }


def _fsync_fault(workdir: Path) -> dict[str, Any]:
    path = workdir / "fault-evidence.json"
    baseline = b'{"generation":1}\n'
    replacement = b'{"generation":2}\n'
    _atomic_bytes(path, baseline)
    calls = 0

    def fail_first(_fd: int) -> None:
        nonlocal calls
        calls += 1
        raise OSError("injected fsync failure")

    observed = False
    started = time.perf_counter()
    try:
        _atomic_bytes(path, replacement, fail_first)
    except OSError:
        observed = True
    preserved = path.read_bytes() == baseline
    _atomic_bytes(path, replacement)
    recovered = path.read_bytes() == replacement
    return {
        "fault": "atomic_fsync_failure",
        "status": "passed" if observed and preserved and recovered and calls == 1 else "failed",
        "attempted": 2,
        "committed": 1,
        "duplicate": 0,
        "loss": 0 if preserved and recovered else 1,
        "recovered": int(recovered),
        "recovery_ms": round((time.perf_counter() - started) * 1000, 6),
        "fsync_failure_observed": observed,
        "fault_scope": "injected Python fsync callback before atomic replace; not SQLite VFS",
        "sqlite_vfs_fsync_failure": False,
        "previous_generation_preserved": preserved,
    }


def _dispatcher_for(
    ledger: JobLedger,
    workdir: Path,
    *,
    code: str,
    timeout_sec: int,
) -> tuple[DurableDispatcher, WorkerSupervisor, GlobalResourceGovernor]:
    governor = GlobalResourceGovernor()
    supervisor = WorkerSupervisor(governor)

    def worker_factory(job, _lease):
        return WorkerSpec(
            job_id=job.job_id,
            worker_class="light",
            argv=(sys.executable, "-I", "-S", "-c", code),
            cwd=workdir,
            estimated_footprint_mb=8,
            timeout_sec=timeout_sec,
        )

    dispatcher = DurableDispatcher(
        ledger=ledger,
        supervisor=supervisor,
        worker_factory=worker_factory,
        completion_verifier=lambda job, _lease, _result: VerifiedCompletion(
            target=JobStatus.SUCCEEDED,
            business_completed=True,
            artifacts=({"kind": "offline_fault", "uri": f"offline://{job.job_id}"},),
        ),
        snapshot_provider=ResourceSnapshot,
        owner_id="offline-fault-dispatcher",
        lease_seconds=max(1, timeout_sec),
        capability_worker_classes={"offline_fault": "light"},
    )
    return dispatcher, supervisor, governor


def _worker_fault(workdir: Path, *, timeout: bool) -> dict[str, Any]:
    name = "timeout" if timeout else "crash"
    ledger = JobLedger(workdir / f"fault-worker-{name}.sqlite3")
    ledger.initialize()
    base = datetime.now(tz=_TAIPEI)
    timeout_sec = 1 if timeout else 5
    ledger.create_job(
        JobSpec(
            job_id=f"worker-{name}",
            capability="offline_fault",
            operation=name,
            worker_class="light",
            side_effect_class="read_only",
            input={},
            scheduled_for=base,
            timeout_sec=timeout_sec,
            resource_claim=_LIGHT_CLAIM,
        ),
        now=base,
    )
    code = "import time;time.sleep(5)" if timeout else "raise SystemExit(17)"
    dispatcher, supervisor, governor = _dispatcher_for(
        ledger,
        workdir,
        code=code,
        timeout_sec=timeout_sec,
    )
    started = time.perf_counter()
    handle = dispatcher.dispatch_next(now=base)
    if handle is None:
        raise OfflineProbeError(f"{name} worker was not dispatched")
    result = supervisor.wait(handle.lease.job.job_id, timeout=timeout_sec + 3)
    outcome = dispatcher.commit_result(handle.lease.job.job_id, result)
    expected_status = JobStatus.TIMED_OUT if timeout else JobStatus.FAILED
    clean = not supervisor.active_job_ids() and governor.active_counts()["total"] == 0
    return {
        "fault": f"worker_{name}",
        "status": "passed" if outcome.job.status is expected_status and clean else "failed",
        "attempted": 1,
        "committed": 1,
        "duplicate": 0,
        "loss": 0,
        "recovered": int(clean),
        "recovery_ms": round((time.perf_counter() - started) * 1000, 6),
        "process_group_gone": result.process_group_gone,
        "timed_out": result.timed_out,
        "terminal_status": outcome.job.status.value,
    }


def _notification_storm_fault(workdir: Path) -> dict[str, Any]:
    ledger = JobLedger(workdir / "fault-notification.sqlite3")
    ledger.initialize()
    base = _REPLAY_START
    records = 200
    duplicate_rejected = 0
    started = time.perf_counter()
    for index in range(records):
        ledger.enqueue_outbox(
            topic="offline.notification",
            payload={"index": index},
            idempotency_key=f"offline-notification-{index:03d}",
            outbox_id=f"notification-{index:03d}",
            ttl_seconds=600,
            max_attempts=3,
            now=base,
        )
        try:
            ledger.enqueue_outbox(
                topic="offline.notification",
                payload={"index": index},
                idempotency_key=f"offline-notification-{index:03d}",
                now=base,
            )
        except LedgerError:
            duplicate_rejected += 1

    for attempt in range(3):
        for _index in range(records):
            claim = ledger.claim_outbox("offline-notifier", now=base + timedelta(seconds=attempt))
            if claim is None or claim.claim_token is None:
                raise OfflineProbeError("notification storm claim unexpectedly empty")
            ledger.mark_outbox_failed(
                claim.outbox_id,
                "offline-notifier",
                claim_token=claim.claim_token,
                claim_generation=claim.claim_generation,
                error="injected provider outage",
                retry_after_seconds=0,
                now=base + timedelta(seconds=attempt),
            )
    if ledger.claim_outbox("offline-notifier", now=base + timedelta(seconds=4)) is not None:
        raise OfflineProbeError("notification storm exceeded its bounded DLQ attempts")
    with sqlite3.connect(ledger.path) as connection:
        total, distinct, dead_lettered = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT idempotency_key), "
            "SUM(CASE WHEN dead_lettered_at IS NOT NULL THEN 1 ELSE 0 END) FROM outbox"
        ).fetchone()
    passed = total == records and distinct == records and dead_lettered == records
    return {
        "fault": "notification_storm_dlq",
        "status": "passed" if passed and duplicate_rejected == records else "failed",
        "attempted": records * 2,
        "committed": int(total),
        "duplicate": 0,
        "duplicate_rejected": duplicate_rejected,
        "loss": int(records - total),
        "recovered": int(dead_lettered),
        "recovery_ms": round((time.perf_counter() - started) * 1000, 6),
        "dead_lettered": int(dead_lettered),
        "delivery_attempts": records * 3,
    }


def run_fault_campaign(workdir: Path) -> dict[str, Any]:
    started = time.perf_counter()
    matrix = [
        _sqlite_wal_fault(workdir),
        _sqlite_disk_full_fault(workdir),
        _fsync_fault(workdir),
        _worker_fault(workdir, timeout=False),
        _worker_fault(workdir, timeout=True),
        _notification_storm_fault(workdir),
    ]
    measurements = {
        "matrix": matrix,
        "faults_requested": len(matrix),
        "faults_completed": len(matrix),
        "faults_passed": sum(item["status"] == "passed" for item in matrix),
        "duplicate_total": sum(int(item["duplicate"]) for item in matrix),
        "loss_total": sum(int(item["loss"]) for item in matrix),
        "recovered_total": sum(int(item["recovered"]) for item in matrix),
        "maximum_recovery_ms": round(max(float(item["recovery_ms"]) for item in matrix), 6),
        "wall_duration_seconds": round(time.perf_counter() - started, 6),
    }
    passed = (
        measurements["faults_completed"] == 6
        and measurements["faults_passed"] == 6
        and measurements["duplicate_total"] == 0
        and measurements["loss_total"] == 0
    )
    return _evidence(FAULT_WORKLOAD, "bounded_offline_fault_matrix", measurements, passed)


def _evidence(
    workload: str,
    probe: str,
    measurements: Mapping[str, Any],
    passed: bool,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "workload": workload,
        "probe": probe,
        "status": "passed" if passed else "failed",
        "measurements": dict(measurements),
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
        "live_state_access_performed": False,
    }

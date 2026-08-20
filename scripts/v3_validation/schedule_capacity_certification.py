#!/usr/bin/env python3
"""Layered schedule-capacity evidence with explicit same-job coalescing.

The control-plane model receives every 10x delivery and proves exact delivery
deduplication plus a bounded same-job queue (one active and at most one latest
pending occurrence).  The business-body model is deliberately separate. It
applies the required 2x duration multiplier and remains non-certifying until
every enabled job has three successful, semantic, Seatbelt-isolated executions
of its real entrypoint.

Dispatcher persistence latency, ``--help`` execution, and synthetic command
latency are never accepted as business-body duration evidence.  Replay duration
uses the maximum of historical production p95 (when available) and an explicitly
labelled release-bound compressed active bounded-body p95.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.dont_write_bytecode = True

from magi_v3.resource import GlobalResourceGovernor
from magi_v3.cron_policy import CronDispatchPolicy, load_cron_dispatch_policy
from scripts.v3_campaign.offline_probes import (
    OfflineProbeError,
    _ARRIVAL_MULTIPLIER,
    _DURATION_MULTIPLIER,
    _base_arrivals,
    _replay_profile,
    _timeout,
    bound_cron_jobs,
)
from scripts.v3_campaign.schedule_realism import MIN_SUCCESSFUL_SAMPLES, bound_duration_replay_profiles
from scripts.v3_validation.schedule_body_registry import (
    REGISTRY_PATH,
    SCHEMA as BODY_REGISTRY_SCHEMA,
    ScheduleBodyRegistryError,
    run_registry_assessment,
)
from scripts.v3_validation.schedule_sample_evidence import (
    verify_sample_evidence_ledger,
)


SCHEMA = "magi.v3.schedule-capacity-certification/v1"
BLOCKER_CODE = "SCHEDULE_LOAD_REALISM_INCOMPLETE"
LIVE_ROOT = (Path.home() / "Library" / "Application Support" / "MAGI").resolve()
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DURABLE_BACKLOG_COALESCING_JOB_IDS = frozenset(
    {
        "job_drive_case_sync_all_files",
        "job_legacy_judgment_resummary_quality",
    }
)


class ScheduleCapacityError(RuntimeError):
    """The capacity model or its bound evidence failed closed."""


def classify_coalescing_safety(capacity: dict[str, Any]) -> None:
    """Annotate coalescing without hiding any skipped distinct occurrence."""

    coalesced_by_job = capacity.get("coalesced_distinct_occurrences_by_job")
    if not isinstance(coalesced_by_job, dict) or any(
        not isinstance(job_id, str)
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 1
        for job_id, count in coalesced_by_job.items()
    ):
        raise ScheduleCapacityError("coalesced occurrence ledger is invalid")
    loss_sensitive_by_job = {
        job_id: count
        for job_id, count in coalesced_by_job.items()
        if job_id not in DURABLE_BACKLOG_COALESCING_JOB_IDS
    }
    durable_by_job = {
        job_id: count
        for job_id, count in coalesced_by_job.items()
        if job_id in DURABLE_BACKLOG_COALESCING_JOB_IDS
    }
    if sum(coalesced_by_job.values()) != capacity.get(
        "coalesced_distinct_occurrences"
    ):
        raise ScheduleCapacityError("coalesced occurrence total drifted")
    capacity.update(
        {
            "durable_backlog_coalescing_job_ids": sorted(
                DURABLE_BACKLOG_COALESCING_JOB_IDS
            ),
            "durable_backlog_coalesced_occurrences": sum(durable_by_job.values()),
            "durable_backlog_coalesced_occurrences_by_job": durable_by_job,
            "loss_sensitive_coalesced_occurrences": sum(
                loss_sensitive_by_job.values()
            ),
            "loss_sensitive_coalesced_occurrences_by_job": loss_sensitive_by_job,
            "coalescing_safety_passed": not loss_sensitive_by_job,
        }
    )


@dataclass(frozen=True, slots=True)
class Occurrence:
    job_id: str
    scheduled_for: float
    worker_class: str
    timeout_seconds: int
    duration_seconds: float

    @property
    def key(self) -> tuple[str, float]:
        return self.job_id, self.scheduled_for


@dataclass(frozen=True, slots=True)
class OfferResult:
    disposition: str
    generation: int | None
    replaced: Occurrence | None = None


class SameJobCoalescer:
    """Exact delivery dedup plus one-active/one-latest-pending state.

    Distinct scheduled occurrences are never called duplicates.  If a job is
    active, the first newer occurrence becomes pending; further newer
    occurrences replace that pending occurrence under an explicit latest-wins
    policy.  Replacements are counted and exposed, never reported as executed.
    """

    def __init__(self) -> None:
        self._seen: set[tuple[str, float]] = set()
        self._active: set[str] = set()
        self._pending: dict[str, Occurrence] = {}
        self._generation: dict[str, int] = {}
        self.exact_duplicates = 0
        self.distinct_occurrences = 0
        self.coalesced_replacements = 0
        self.coalesced_replacements_by_job: Counter[str] = Counter()
        self.max_pending_jobs = 0

    def offer(self, occurrence: Occurrence) -> OfferResult:
        if occurrence.key in self._seen:
            self.exact_duplicates += 1
            return OfferResult("exact_duplicate", None)
        self._seen.add(occurrence.key)
        self.distinct_occurrences += 1
        previous = self._pending.get(occurrence.job_id)
        generation = self._generation.get(occurrence.job_id, 0) + 1
        self._generation[occurrence.job_id] = generation
        self._pending[occurrence.job_id] = occurrence
        self.max_pending_jobs = max(self.max_pending_jobs, len(self._pending))
        if previous is not None:
            self.coalesced_replacements += 1
            self.coalesced_replacements_by_job[occurrence.job_id] += 1
            return OfferResult("coalesced_latest_pending", generation, previous)
        if occurrence.job_id in self._active:
            return OfferResult("deferred_behind_active", generation)
        return OfferResult("ready", generation)

    def start(self, job_id: str, generation: int) -> Occurrence | None:
        if job_id in self._active or self._generation.get(job_id) != generation:
            return None
        occurrence = self._pending.pop(job_id, None)
        if occurrence is None:
            return None
        self._active.add(job_id)
        return occurrence

    def complete(self, job_id: str) -> tuple[Occurrence, int] | None:
        if job_id not in self._active:
            raise ScheduleCapacityError(f"completion for inactive job: {job_id}")
        self._active.remove(job_id)
        pending = self._pending.get(job_id)
        if pending is None:
            return None
        return pending, self._generation[job_id]

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def pending_count(self) -> int:
        return len(self._pending)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _persist_diagnostic_report(root: Path, name: str, value: Mapping[str, Any]) -> None:
    """Atomically retain hash-only certification evidence in the owned sandbox."""

    target = root / name
    temporary = root / f".{name}.tmp-{os.getpid()}"
    temporary.write_bytes(_canonical_json(value) + b"\n")
    os.replace(temporary, target)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_cached_registry(
    registry: Mapping[str, Any],
    *,
    source_root: Path,
    release_id: str,
    release_manifest_sha256: str,
    cron_sha256: str,
) -> None:
    unsigned = dict(registry)
    supplied = str(unsigned.pop("evidence_sha256", ""))
    binding = registry.get("release_binding")
    if (
        not HEX64.fullmatch(supplied)
        or _sha256(unsigned) != supplied
        or registry.get("schema") != BODY_REGISTRY_SCHEMA
        or registry.get("status") != "passed"
        or registry.get("completion_claimed") is not True
        or not isinstance(binding, Mapping)
        or binding.get("release_id") != release_id
        or binding.get("release_manifest_sha256") != release_manifest_sha256
        or binding.get("cron_jobs_sha256") != cron_sha256
        or binding.get("registry_sha256")
        != _sha256_file(source_root / REGISTRY_PATH)
        or binding.get("inherited_baseline_sha256")
        != _sha256_file(source_root / "config/v3_schedule_realism_baseline.json")
        or registry.get("external_network_access_performed") is not False
        or registry.get("production_database_access_performed") is not False
        or registry.get("nas_access_performed") is not False
        or registry.get("production_state_write_performed") is not False
    ):
        raise ScheduleCapacityError(
            "release-bound schedule body cache is invalid or belongs to another candidate"
        )


def _registry_assessment_with_campaign_cache(
    source_root: Path,
    workdir: Path,
    *,
    release_id: str,
    release_manifest_sha256: str,
    cron_sha256: str,
) -> tuple[dict[str, Any], bool]:
    raw_cache = os.environ.get("MAGI_V3_SCHEDULE_BODY_CACHE", "").strip()
    if not raw_cache:
        return (
            run_registry_assessment(
                source_root,
                workdir,
                release_id=release_id,
                release_manifest_sha256=release_manifest_sha256,
            ),
            False,
        )
    cache = Path(raw_cache).expanduser()
    if (
        not cache.is_absolute()
        or cache.resolve(strict=False) != cache
        or cache == LIVE_ROOT
        or _is_relative_to(cache, LIVE_ROOT)
        or cache == REPO_ROOT
        or _is_relative_to(cache, REPO_ROOT)
    ):
        raise ScheduleCapacityError("schedule body cache path is unsafe")
    if cache.exists() or cache.is_symlink():
        metadata = cache.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_nlink != 1
            or metadata.st_uid != os.getuid()
        ):
            raise ScheduleCapacityError("schedule body cache file is unsafe")
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ScheduleCapacityError("schedule body cache is invalid JSON") from exc
        if not isinstance(cached, dict):
            raise ScheduleCapacityError("schedule body cache must be a JSON object")
        _validate_cached_registry(
            cached,
            source_root=source_root,
            release_id=release_id,
            release_manifest_sha256=release_manifest_sha256,
            cron_sha256=cron_sha256,
        )
        return cached, True
    registry = run_registry_assessment(
        source_root,
        workdir,
        release_id=release_id,
        release_manifest_sha256=release_manifest_sha256,
    )
    _validate_cached_registry(
        registry,
        source_root=source_root,
        release_id=release_id,
        release_manifest_sha256=release_manifest_sha256,
        cron_sha256=cron_sha256,
    )
    cache.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = _canonical_json(registry) + b"\n"
    descriptor = os.open(
        cache,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return registry, False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _prepare_workdir(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise ScheduleCapacityError("schedule capacity workdir must not be a symlink")
    resolved = expanded.resolve()
    if resolved == LIVE_ROOT or _is_relative_to(resolved, LIVE_ROOT):
        raise ScheduleCapacityError("schedule capacity workdir must not be live MAGI state")
    if resolved == REPO_ROOT or _is_relative_to(resolved, REPO_ROOT):
        raise ScheduleCapacityError("schedule capacity workdir must not be inside the source tree")
    if resolved.exists():
        if not resolved.is_dir() or any(resolved.iterdir()):
            raise ScheduleCapacityError("schedule capacity workdir must be an empty directory")
    else:
        resolved.mkdir(parents=True)
    (resolved / ".magi-v3-schedule-capacity-sandbox").write_text(
        "owned disposable schedule-capacity state\n", encoding="utf-8"
    )
    return resolved


def _cleanup_certified_campaign_workdir(workdir: Path) -> None:
    """Remove the large disposable body sandbox after evidence is in memory.

    Failed runs intentionally retain their workdir for diagnosis.  Successful
    campaign runs must not retain one MariaDB datadir per sample: doing so can
    consume enough of the host disk to make the resource-governor workload
    fail for a condition created by the certifier itself.
    """

    temporary_text = os.environ.get("TMPDIR", "").strip()
    if not temporary_text:
        raise ScheduleCapacityError("campaign workdir cleanup requires TMPDIR")
    temporary_root = Path(temporary_text).expanduser().resolve(strict=True)
    resolved = workdir.expanduser().resolve(strict=True)
    marker = resolved / ".magi-v3-schedule-capacity-sandbox"
    if (
        resolved.parent != temporary_root
        or not resolved.name.startswith("schedule-capacity-")
        or resolved.is_symlink()
        or marker.is_symlink()
        or not marker.is_file()
        or marker.read_text(encoding="utf-8")
        != "owned disposable schedule-capacity state\n"
    ):
        raise ScheduleCapacityError("campaign workdir cleanup ownership is invalid")
    shutil.rmtree(resolved)
    if resolved.exists() or resolved.is_symlink():
        raise ScheduleCapacityError("campaign workdir cleanup did not complete")


def _occurrences(
    jobs: list[dict[str, Any]],
    profiles: Mapping[str, Mapping[str, Any]],
    replay_start: datetime,
    policy: CronDispatchPolicy,
) -> list[Occurrence]:
    result: list[Occurrence] = []
    for instant, _index, job in _base_arrivals(jobs, replay_start):
        job_id = str(job.get("id") or "")
        profile = profiles.get(job_id)
        if not isinstance(profile, Mapping):
            raise ScheduleCapacityError(f"duration profile is missing for enabled job: {job_id}")
        raw_duration = profile.get("duration_seconds")
        if (
            isinstance(raw_duration, bool)
            or not isinstance(raw_duration, (int, float))
            or raw_duration <= 0
        ):
            raise ScheduleCapacityError(f"duration profile is invalid for enabled job: {job_id}")
        effective_instant = instant.timestamp() + policy.delay_for(job)
        result.append(
            Occurrence(
                job_id=job_id,
                scheduled_for=effective_instant - replay_start.timestamp(),
                worker_class=policy.lane_for(job),
                timeout_seconds=_timeout(job),
                duration_seconds=float(raw_duration) * _DURATION_MULTIPLIER,
            )
        )
    return result


def simulate_layered_capacity(
    occurrences: Sequence[Occurrence],
    *,
    delivery_multiplier: int = _ARRIVAL_MULTIPLIER,
    slots: Mapping[str, int] | None = None,
    dispatch_policy: CronDispatchPolicy | None = None,
) -> dict[str, Any]:
    """Run all duplicate deliveries through a bounded same-job state machine."""

    if delivery_multiplier < 1:
        raise ScheduleCapacityError("delivery multiplier must be positive")
    if slots is None:
        policy = GlobalResourceGovernor().policy
        slots = {"light": policy.max_light, "maintenance": policy.max_heavy}
    normalized_slots = {key: int(value) for key, value in slots.items()}
    if not normalized_slots or any(value < 1 for value in normalized_slots.values()):
        raise ScheduleCapacityError("worker slot policy is invalid")
    if any(item.worker_class not in normalized_slots for item in occurrences):
        raise ScheduleCapacityError("occurrence references an unknown worker class")

    arrivals: list[tuple[float, int, Occurrence]] = []
    sequence = 0
    for occurrence in occurrences:
        for _copy in range(delivery_multiplier):
            arrivals.append((occurrence.scheduled_for, sequence, occurrence))
            sequence += 1
    arrivals.sort(key=lambda row: (row[0], row[1]))
    arrival_index = 0
    completions: list[tuple[float, int, str, str]] = []
    ready: dict[str, list[tuple[float, float, int, str, int]]] = {
        worker_class: [] for worker_class in normalized_slots
    }
    active_by_class = {worker_class: 0 for worker_class in normalized_slots}
    coalescer = SameJobCoalescer()
    event_sequence = sequence
    executed: list[tuple[str, float]] = []
    executed_set: set[tuple[str, float]] = set()
    latest_start_misses = 0
    deadline_misses = 0
    max_queue_delay = 0.0
    latest_start_misses_by_job: Counter[str] = Counter()
    deadline_misses_by_job: Counter[str] = Counter()
    max_queue_delay_by_job: dict[str, float] = {}
    same_job_concurrency_violations = 0
    demand = {worker_class: 0.0 for worker_class in normalized_slots}

    def can_start(worker_class: str) -> bool:
        if dispatch_policy is not None:
            active_lanes = [
                lane
                for lane, count in active_by_class.items()
                for _ in range(count)
            ]
            return dispatch_policy.can_start_lane(worker_class, active_lanes)
        return active_by_class[worker_class] < normalized_slots[worker_class]

    def enqueue(occurrence: Occurrence, generation: int) -> None:
        nonlocal event_sequence
        heapq.heappush(
            ready[occurrence.worker_class],
            (
                occurrence.scheduled_for + occurrence.timeout_seconds,
                occurrence.scheduled_for,
                event_sequence,
                occurrence.job_id,
                generation,
            ),
        )
        event_sequence += 1

    def schedule(now: float) -> None:
        nonlocal event_sequence, latest_start_misses, deadline_misses
        nonlocal max_queue_delay, same_job_concurrency_violations
        while True:
            startable = [
                (queue[0], worker_class)
                for worker_class, queue in ready.items()
                if queue and can_start(worker_class)
            ]
            if not startable:
                return
            (_key, worker_class) = min(startable, key=lambda row: (row[0], row[1]))
            queue = ready[worker_class]
            _latest_start, _scheduled, _ordinal, job_id, generation = heapq.heappop(queue)
            occurrence = coalescer.start(job_id, generation)
            if occurrence is None:
                continue
            if occurrence.worker_class != worker_class:
                raise ScheduleCapacityError("coalesced occurrence changed worker class")
            if any(active_job == job_id for _end, _seq, active_job, _class in completions):
                same_job_concurrency_violations += 1
            started = max(now, occurrence.scheduled_for)
            finished = started + occurrence.duration_seconds
            latest_start = occurrence.scheduled_for + occurrence.timeout_seconds
            deadline = latest_start + occurrence.timeout_seconds
            queue_delay = started - occurrence.scheduled_for
            latest_missed = started > latest_start
            deadline_missed = finished > deadline
            latest_start_misses += int(latest_missed)
            deadline_misses += int(deadline_missed)
            latest_start_misses_by_job[occurrence.job_id] += int(latest_missed)
            deadline_misses_by_job[occurrence.job_id] += int(deadline_missed)
            max_queue_delay = max(max_queue_delay, queue_delay)
            max_queue_delay_by_job[occurrence.job_id] = max(
                max_queue_delay_by_job.get(occurrence.job_id, 0.0), queue_delay
            )
            demand[worker_class] += occurrence.duration_seconds
            if occurrence.key in executed_set:
                raise ScheduleCapacityError("one occurrence was executed more than once")
            executed_set.add(occurrence.key)
            executed.append(occurrence.key)
            active_by_class[worker_class] += 1
            heapq.heappush(
                completions,
                (finished, event_sequence, job_id, worker_class),
            )
            event_sequence += 1

    while arrival_index < len(arrivals) or completions or any(ready.values()):
        next_arrival = arrivals[arrival_index][0] if arrival_index < len(arrivals) else float("inf")
        next_completion = completions[0][0] if completions else float("inf")
        now = min(next_arrival, next_completion)
        if now == float("inf"):
            raise ScheduleCapacityError("capacity event loop stalled")

        while completions and completions[0][0] <= now:
            _finished, _ordinal, job_id, worker_class = heapq.heappop(completions)
            active_by_class[worker_class] -= 1
            pending = coalescer.complete(job_id)
            if pending is not None:
                enqueue(*pending)

        while arrival_index < len(arrivals) and arrivals[arrival_index][0] <= now:
            _arrival, _ordinal, occurrence = arrivals[arrival_index]
            arrival_index += 1
            offered = coalescer.offer(occurrence)
            if offered.disposition in {"ready", "coalesced_latest_pending"}:
                if offered.generation is None:
                    raise ScheduleCapacityError("ready occurrence lacks a generation")
                if occurrence.job_id not in {
                    active_job for _end, _seq, active_job, _class in completions
                }:
                    enqueue(occurrence, offered.generation)
        schedule(now)

    input_deliveries = len(arrivals)
    unique_occurrences = len(occurrences)
    accounted_unique = len(executed) + coalescer.coalesced_replacements
    if coalescer.distinct_occurrences != unique_occurrences:
        raise ScheduleCapacityError("distinct occurrence accounting drifted")
    if coalescer.exact_duplicates != input_deliveries - unique_occurrences:
        raise ScheduleCapacityError("exact duplicate accounting drifted")
    if accounted_unique != unique_occurrences:
        raise ScheduleCapacityError("executed/coalesced occurrence accounting drifted")
    if coalescer.active_count or coalescer.pending_count or any(active_by_class.values()):
        raise ScheduleCapacityError("capacity simulation did not drain")

    return {
        "input_deliveries": input_deliveries,
        "distinct_scheduled_occurrences": unique_occurrences,
        "exact_duplicate_deliveries": coalescer.exact_duplicates,
        "executed_occurrences": len(executed),
        "coalesced_distinct_occurrences": coalescer.coalesced_replacements,
        "coalesced_distinct_occurrences_by_job": dict(
            sorted(
                coalescer.coalesced_replacements_by_job.items(),
                key=lambda row: (-row[1], row[0]),
            )
        ),
        "all_deliveries_accounted": input_deliveries
        == coalescer.exact_duplicates + coalescer.distinct_occurrences,
        "all_distinct_occurrences_accounted": unique_occurrences == accounted_unique,
        "same_job_concurrency_violations": same_job_concurrency_violations,
        "max_pending_jobs": coalescer.max_pending_jobs,
        "pending_per_job_limit": 1,
        "latest_start_misses": latest_start_misses,
        "deadline_misses": deadline_misses,
        "max_queue_delay_seconds": round(max_queue_delay, 6),
        "latest_start_misses_by_job": dict(
            sorted(
                ((job_id, count) for job_id, count in latest_start_misses_by_job.items() if count),
                key=lambda row: (-row[1], row[0]),
            )
        ),
        "deadline_misses_by_job": dict(
            sorted(
                ((job_id, count) for job_id, count in deadline_misses_by_job.items() if count),
                key=lambda row: (-row[1], row[0]),
            )
        ),
        "max_queue_delay_seconds_by_job": {
            job_id: round(delay, 6)
            for job_id, delay in sorted(
                max_queue_delay_by_job.items(), key=lambda row: (-row[1], row[0])
            )
        },
        "scaled_executed_demand_seconds_by_worker_class": {
            key: round(value, 6) for key, value in demand.items()
        },
        "worker_slots": normalized_slots,
        "global_worker_cap": (
            dispatch_policy.max_workers
            if dispatch_policy is not None
            else sum(normalized_slots.values())
        ),
        "shared_lane_caps": (
            {
                name: {"lanes": sorted(lanes), "slots": cap}
                for name, (lanes, cap) in dispatch_policy.shared_caps.items()
            }
            if dispatch_policy is not None
            else {}
        ),
        "delivery_multiplier": delivery_multiplier,
        "duration_multiplier": _DURATION_MULTIPLIER,
        "coalescing_policy": {
            "exact_key": ["job_id", "scheduled_for"],
            "same_job_concurrency": 1,
            "pending_occurrences_per_job": 1,
            "pending_replacement": "latest_scheduled_occurrence_wins",
            "coalesced_occurrences_reported_as_executed": False,
            "ready_queue_order": "earliest_latest_start_then_scheduled_time",
        },
    }


def _body_coverage(registry: Mapping[str, Any], enabled_ids: set[str]) -> dict[str, Any]:
    measurements = registry.get("measurements")
    body_results = registry.get("body_results")
    entries = registry.get("registry_entries")
    if (
        not isinstance(measurements, Mapping)
        or not isinstance(body_results, list)
        or not isinstance(entries, list)
    ):
        raise ScheduleCapacityError("real job-body registry evidence is malformed")
    passed_ids = {
        str(row.get("job_id") or "")
        for row in body_results
        if isinstance(row, Mapping)
        and row.get("status") == "passed"
        and row.get("semantic_success") is True
        and row.get("successful_samples") == MIN_SUCCESSFUL_SAMPLES
        and row.get("duration_sample_count") == MIN_SUCCESSFUL_SAMPLES
    }
    blocked_ids = {
        str(row.get("job_id") or "")
        for row in entries
        if isinstance(row, Mapping) and row.get("classification") == "blocked"
    }
    if (
        "" in passed_ids
        or "" in blocked_ids
        or passed_ids & blocked_ids
        or passed_ids | blocked_ids != enabled_ids
    ):
        raise ScheduleCapacityError("real job-body registry does not partition enabled jobs")
    if measurements.get("blocked_jobs") != len(blocked_ids):
        raise ScheduleCapacityError("real job-body registry blocker count drifted")
    if measurements.get("body_jobs_passed") != len(passed_ids):
        raise ScheduleCapacityError("real job-body registry pass count drifted")
    return {
        "enabled_jobs": len(enabled_ids),
        "jobs_with_three_successful_real_body_samples": len(passed_ids),
        "jobs_missing_real_body_adapter": len(blocked_ids),
        "body_adapter_coverage_complete": not blocked_ids,
        "passed_job_ids": sorted(passed_ids),
        "missing_job_ids": sorted(blocked_ids),
        "registry_evidence_sha256": str(registry.get("evidence_sha256") or ""),
    }


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ScheduleCapacityError("compressed active duration sample ledger is empty")
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def _duration_profile_hash_payload(duration_evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": "magi.v3.compressed-active-duration-profiles/v1",
        "cron_jobs_sha256": duration_evidence.get("cron_jobs_sha256"),
        "baseline_sha256": duration_evidence.get("baseline_sha256"),
        "active_body_evidence_sha256": duration_evidence.get(
            "active_body_evidence_sha256"
        ),
        "profile_bindings": duration_evidence.get("profile_bindings"),
    }


def _combined_duration_replay_profiles(
    source_root: Path,
    jobs: list[dict[str, Any]],
    cron_sha: str,
    registry: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Combine historical p95 with release-bound compressed active body p95.

    The active samples execute the production entrypoint in a bounded fixture.
    They are intentionally not described as historical production observations.
    """

    historical, historical_coverage = bound_duration_replay_profiles(
        source_root, jobs, cron_sha
    )
    registry_unsigned = dict(registry)
    registry_sha = str(registry_unsigned.pop("evidence_sha256", ""))
    if not HEX64.fullmatch(registry_sha) or _sha256(registry_unsigned) != registry_sha:
        raise ScheduleCapacityError("real body registry evidence hash is invalid")
    binding = registry.get("release_binding")
    entries = registry.get("registry_entries")
    results = registry.get("body_results")
    if (
        not isinstance(binding, Mapping)
        or binding.get("cron_jobs_sha256") != cron_sha
        or not isinstance(entries, list)
        or not isinstance(results, list)
    ):
        raise ScheduleCapacityError("real body registry duration source is malformed")

    enabled = {
        str(job.get("id") or ""): job for job in jobs if job.get("enabled") is True
    }
    entry_by_id = {
        str(row.get("job_id") or ""): row
        for row in entries
        if isinstance(row, Mapping)
    }
    result_by_id = {
        str(row.get("job_id") or ""): row
        for row in results
        if isinstance(row, Mapping)
    }
    if (
        "" in enabled
        or len(entry_by_id) != len(entries)
        or len(result_by_id) != len(results)
        or set(entry_by_id) != set(enabled)
        or not set(result_by_id).issubset(enabled)
    ):
        raise ScheduleCapacityError("real body duration jobs are missing or duplicated")

    profiles: dict[str, dict[str, Any]] = {}
    profile_bindings: list[dict[str, Any]] = []
    active_ids: list[str] = []
    sparse_ids: list[str] = []
    missing_ids: list[str] = []
    historical_p95_ids = sorted(
        job_id
        for job_id, profile in historical.items()
        if profile.get("certifying_p95") is True
    )
    for job_id, job in sorted(enabled.items()):
        entry = entry_by_id[job_id]
        result = result_by_id.get(job_id)
        historical_profile = historical.get(job_id)
        historical_p95 = (
            float(historical_profile["duration_seconds"])
            if isinstance(historical_profile, Mapping)
            and historical_profile.get("certifying_p95") is True
            else None
        )
        historical_sparse = (
            float(historical_profile["duration_seconds"])
            if isinstance(historical_profile, Mapping) and historical_p95 is None
            else None
        )
        active: dict[str, Any] | None = None
        if isinstance(result, Mapping):
            durations = result.get("duration_samples_seconds")
            p95 = result.get("duration_p95_seconds")
            hash_lists = {
                name: result.get(name)
                for name in (
                    "sandbox_profile_sha256_samples",
                    "stdout_sha256_samples",
                    "stderr_sha256_samples",
                )
            }
            actual_entrypoint = str(entry.get("actual_entrypoint") or "")
            entrypoint_path = source_root / actual_entrypoint
            command_sha = hashlib.sha256(
                str(job.get("command") or "").encode()
            ).hexdigest()
            if (
                entry.get("classification") != "safe_adapter"
                or entry.get("blockers") != []
                or entry.get("production_command_sha256") != command_sha
                or not actual_entrypoint
                or not entrypoint_path.is_file()
                or result.get("status") != "passed"
                or result.get("semantic_success") is not True
                or result.get("successful_samples") != MIN_SUCCESSFUL_SAMPLES
                or result.get("duration_sample_count") != MIN_SUCCESSFUL_SAMPLES
                or result.get("network_denied_by_seatbelt") is not True
                or result.get("notifications_disabled") is not True
                or not isinstance(durations, list)
                or len(durations) != MIN_SUCCESSFUL_SAMPLES
                or any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or value <= 0
                    for value in durations
                )
                or isinstance(p95, bool)
                or not isinstance(p95, (int, float))
                or float(p95) != round(_p95([float(value) for value in durations]), 6)
                or not verify_sample_evidence_ledger(
                    result, minimum_samples=MIN_SUCCESSFUL_SAMPLES
                )
                or result.get("entrypoint_sha256") != _sha256_file(entrypoint_path)
                or any(
                    not isinstance(values, list)
                    or len(values) != MIN_SUCCESSFUL_SAMPLES
                    or any(not HEX64.fullmatch(str(value or "")) for value in values)
                    for values in hash_lists.values()
                )
            ):
                raise ScheduleCapacityError(
                    f"compressed active duration binding is invalid: {job_id}"
                )
            active = {
                "sample_kind": "compressed_active_bounded_real_entrypoint",
                "p95_kind": "compressed_active_bounded_body_p95",
                "successful_samples": MIN_SUCCESSFUL_SAMPLES,
                "semantic_success": True,
                "duration_samples_seconds": [float(value) for value in durations],
                "duration_p95_seconds": float(p95),
                "sample_evidence": result.get("sample_evidence"),
                "sample_evidence_sha256": str(result["sample_evidence_sha256"]),
                "actual_entrypoint": actual_entrypoint,
                "entrypoint_sha256": str(result["entrypoint_sha256"]),
                "production_command_sha256": command_sha,
                "runner": str(result.get("runner") or ""),
                "adapter_mode": str(result.get("adapter_mode") or ""),
                "network_denied_by_seatbelt": True,
                "notifications_disabled": True,
                **{name: list(values) for name, values in hash_lists.items()},
            }
            active_ids.append(job_id)

        active_p95 = (
            float(active["duration_p95_seconds"]) if active is not None else None
        )
        certifying = active is not None
        if active_p95 is not None and historical_p95 is not None:
            selected = max(historical_p95, active_p95)
            basis = "max_historical_production_p95_and_compressed_active_bounded_body_p95"
        elif active_p95 is not None:
            selected = active_p95
            basis = "compressed_active_bounded_body_p95"
        elif historical_p95 is not None:
            selected = historical_p95
            basis = "historical_production_p95_missing_compressed_active_noncertifying"
        elif historical_sparse is not None:
            selected = historical_sparse
            basis = "historical_observed_max_sparse_fallback_noncertifying"
        else:
            missing_ids.append(job_id)
            continue
        if not certifying:
            sparse_ids.append(job_id)
        profiles[job_id] = {
            "duration_seconds": selected,
            "duration_basis": basis,
            "successful_sample_count": (
                MIN_SUCCESSFUL_SAMPLES if certifying else 0
            ),
            "certifying_p95": certifying,
        }
        profile_bindings.append(
            {
                "job_id": job_id,
                "selected_duration_seconds": selected,
                "selected_duration_basis": basis,
                "historical_production_p95_seconds": historical_p95,
                "historical_sparse_observed_max_seconds": historical_sparse,
                "compressed_active": active,
            }
        )

    duration_evidence: dict[str, Any] = {
        "enabled_jobs": len(enabled),
        "profiles": len(profiles),
        "p95_jobs": len(active_ids),
        "historical_production_p95_jobs": len(historical_p95_ids),
        "compressed_active_p95_jobs": len(active_ids),
        "sparse_fallback_jobs": len(sparse_ids),
        "missing_jobs": len(missing_ids),
        "minimum_successful_samples": MIN_SUCCESSFUL_SAMPLES,
        "certifying_p95_coverage": len(active_ids) == len(enabled)
        and not sparse_ids
        and not missing_ids,
        "p95_job_ids": sorted(active_ids),
        "historical_production_p95_job_ids": historical_p95_ids,
        "compressed_active_p95_job_ids": sorted(active_ids),
        "sparse_fallback_job_ids": sorted(sparse_ids),
        "missing_job_ids": sorted(missing_ids),
        "cron_jobs_sha256": cron_sha,
        "baseline_sha256": historical_coverage["baseline_sha256"],
        "active_body_evidence_sha256": registry_sha,
        "selected_duration_rule": "max(historical_production_p95, compressed_active_bounded_body_p95)",
        "active_sample_disclosure": "compressed active bounded real-entrypoint samples; not historical production observations",
        "profile_bindings": profile_bindings,
    }
    duration_evidence["duration_profiles_sha256"] = _sha256(
        _duration_profile_hash_payload(duration_evidence)
    )
    return profiles, duration_evidence


def verify_compressed_active_duration_evidence(
    duration_evidence: Mapping[str, Any],
    registry: Mapping[str, Any],
    *,
    cron_jobs_sha256: str,
    require_complete: bool,
) -> None:
    registry_unsigned = dict(registry)
    registry_sha = str(registry_unsigned.pop("evidence_sha256", ""))
    profile_bindings = duration_evidence.get("profile_bindings")
    entries = registry.get("registry_entries")
    results = registry.get("body_results")
    if (
        duration_evidence.get("cron_jobs_sha256") != cron_jobs_sha256
        or not HEX64.fullmatch(registry_sha)
        or _sha256(registry_unsigned) != registry_sha
        or duration_evidence.get("duration_profiles_sha256")
        != _sha256(_duration_profile_hash_payload(duration_evidence))
        or not isinstance(profile_bindings, list)
        or not isinstance(entries, list)
        or not isinstance(results, list)
    ):
        raise ScheduleCapacityError("compressed active duration evidence is malformed")
    if duration_evidence.get("active_body_evidence_sha256") != registry_sha:
        raise ScheduleCapacityError("compressed active duration body hash drifted")
    entry_by_id = {
        str(row.get("job_id") or ""): row
        for row in entries
        if isinstance(row, Mapping)
    }
    result_by_id = {
        str(row.get("job_id") or ""): row
        for row in results
        if isinstance(row, Mapping)
    }
    binding_by_id = {
        str(row.get("job_id") or ""): row
        for row in profile_bindings
        if isinstance(row, Mapping)
    }
    if len(binding_by_id) != len(profile_bindings) or set(binding_by_id) != set(entry_by_id):
        raise ScheduleCapacityError("compressed active duration job coverage drifted")
    active_ids: set[str] = set()
    for job_id, row in binding_by_id.items():
        active = row.get("compressed_active")
        result = result_by_id.get(job_id)
        entry = entry_by_id[job_id]
        historical_p95 = row.get("historical_production_p95_seconds")
        if active is None:
            continue
        if not isinstance(active, Mapping) or not isinstance(result, Mapping):
            raise ScheduleCapacityError("compressed active duration result is missing")
        durations = result.get("duration_samples_seconds")
        expected_active = {
            "sample_kind": "compressed_active_bounded_real_entrypoint",
            "p95_kind": "compressed_active_bounded_body_p95",
            "successful_samples": result.get("successful_samples"),
            "semantic_success": result.get("semantic_success"),
            "duration_samples_seconds": durations,
            "duration_p95_seconds": result.get("duration_p95_seconds"),
            "sample_evidence": result.get("sample_evidence"),
            "sample_evidence_sha256": result.get("sample_evidence_sha256"),
            "actual_entrypoint": entry.get("actual_entrypoint"),
            "entrypoint_sha256": result.get("entrypoint_sha256"),
            "production_command_sha256": entry.get("production_command_sha256"),
            "runner": result.get("runner"),
            "adapter_mode": result.get("adapter_mode"),
            "network_denied_by_seatbelt": result.get(
                "network_denied_by_seatbelt"
            ),
            "notifications_disabled": result.get("notifications_disabled"),
            "sandbox_profile_sha256_samples": result.get(
                "sandbox_profile_sha256_samples"
            ),
            "stdout_sha256_samples": result.get("stdout_sha256_samples"),
            "stderr_sha256_samples": result.get("stderr_sha256_samples"),
        }
        if (
            dict(active) != expected_active
            or result.get("status") != "passed"
            or result.get("semantic_success") is not True
            or result.get("successful_samples") != MIN_SUCCESSFUL_SAMPLES
            or result.get("duration_sample_count") != MIN_SUCCESSFUL_SAMPLES
            or result.get("network_denied_by_seatbelt") is not True
            or result.get("notifications_disabled") is not True
            or not isinstance(durations, list)
            or len(durations) != MIN_SUCCESSFUL_SAMPLES
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or value <= 0
                for value in durations
            )
            or float(result.get("duration_p95_seconds") or 0)
            != round(_p95([float(value) for value in durations]), 6)
            or not verify_sample_evidence_ledger(
                result, minimum_samples=MIN_SUCCESSFUL_SAMPLES
            )
            or not HEX64.fullmatch(str(result.get("entrypoint_sha256") or ""))
            or not HEX64.fullmatch(str(entry.get("production_command_sha256") or ""))
            or not str(result.get("runner") or "")
            or not str(result.get("adapter_mode") or "")
            or any(
                not isinstance(result.get(name), list)
                or len(result[name]) != MIN_SUCCESSFUL_SAMPLES
                or any(
                    not HEX64.fullmatch(str(value or ""))
                    for value in result[name]
                )
                for name in (
                    "sandbox_profile_sha256_samples",
                    "stdout_sha256_samples",
                    "stderr_sha256_samples",
                )
            )
        ):
            raise ScheduleCapacityError("compressed active duration raw binding drifted")
        active_p95 = float(active["duration_p95_seconds"])
        expected_selected = (
            max(float(historical_p95), active_p95)
            if isinstance(historical_p95, (int, float))
            and not isinstance(historical_p95, bool)
            else active_p95
        )
        if float(row.get("selected_duration_seconds") or 0) != expected_selected:
            raise ScheduleCapacityError("compressed active duration selection drifted")
        expected_basis = (
            "max_historical_production_p95_and_compressed_active_bounded_body_p95"
            if isinstance(historical_p95, (int, float))
            and not isinstance(historical_p95, bool)
            else "compressed_active_bounded_body_p95"
        )
        if row.get("selected_duration_basis") != expected_basis:
            raise ScheduleCapacityError("compressed active duration basis drifted")
        active_ids.add(job_id)
    enabled_jobs = int(duration_evidence.get("enabled_jobs") or 0)
    declared_active_ids = duration_evidence.get("compressed_active_p95_job_ids")
    declared_p95_ids = duration_evidence.get("p95_job_ids")
    declared_sparse_ids = duration_evidence.get("sparse_fallback_job_ids")
    declared_missing_ids = duration_evidence.get("missing_job_ids")
    expected_non_active = set(entry_by_id) - active_ids
    complete = (
        len(active_ids) == enabled_jobs == len(entry_by_id)
        and duration_evidence.get("profiles") == len(profile_bindings)
        and duration_evidence.get("minimum_successful_samples")
        == MIN_SUCCESSFUL_SAMPLES
        and duration_evidence.get("compressed_active_p95_jobs") == enabled_jobs
        and duration_evidence.get("p95_jobs") == enabled_jobs
        and duration_evidence.get("sparse_fallback_jobs") == 0
        and duration_evidence.get("missing_jobs") == 0
        and duration_evidence.get("certifying_p95_coverage") is True
        and declared_active_ids == sorted(active_ids)
        and declared_p95_ids == sorted(active_ids)
        and declared_sparse_ids == []
        and declared_missing_ids == []
        and not expected_non_active
        and duration_evidence.get("selected_duration_rule")
        == "max(historical_production_p95, compressed_active_bounded_body_p95)"
        and duration_evidence.get("active_sample_disclosure")
        == "compressed active bounded real-entrypoint samples; not historical production observations"
    )
    if require_complete and not complete:
        raise ScheduleCapacityError("compressed active duration coverage is incomplete")


def verify_schedule_capacity_evidence(evidence: Mapping[str, Any]) -> None:
    supplied = evidence.get("evidence_sha256")
    if not isinstance(supplied, str) or len(supplied) != 64:
        raise ScheduleCapacityError("schedule capacity evidence hash is missing")
    unsigned = dict(evidence)
    unsigned.pop("evidence_sha256", None)
    if supplied != _sha256(unsigned):
        raise ScheduleCapacityError("schedule capacity evidence hash does not match")
    if evidence.get("schema") != SCHEMA:
        raise ScheduleCapacityError("schedule capacity evidence schema is invalid")


def _run_schedule_capacity_certification_bundle(
    source_root: Path,
    workdir: Path,
    *,
    release_id: str = "unpackaged-source-capacity",
    release_manifest_sha256: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    sandbox = _prepare_workdir(workdir)
    jobs, cron_sha = bound_cron_jobs(source_root)
    enabled_ids = {str(job.get("id") or "") for job in jobs if job.get("enabled") is True}
    if "" in enabled_ids:
        raise ScheduleCapacityError("enabled cron definition lacks an id")
    profile_id, replay_start = _replay_profile()
    dispatch_policy = load_cron_dispatch_policy(source_root)
    governor_policy = GlobalResourceGovernor().policy
    if (
        dispatch_policy.lane_caps["light"] != governor_policy.max_light
        or dispatch_policy.lane_caps["maintenance"] != governor_policy.max_heavy
        or dispatch_policy.lane_caps["batch"] != governor_policy.max_heavy
    ):
        raise ScheduleCapacityError("V3 cron dispatch lanes exceed the resource governor")
    registry_path = source_root / REGISTRY_PATH
    manifest_sha = release_manifest_sha256 or _sha256(
        {
            "source_root": str(source_root.resolve()),
            "cron_jobs_sha256": cron_sha,
            "registry_sha256": _sha256_file(registry_path),
            "duration_baseline_sha256": _sha256_file(
                source_root / "config/v3_schedule_realism_baseline.json"
            ),
        }
    )
    registry, body_cache_reused = _registry_assessment_with_campaign_cache(
        source_root,
        sandbox / "real-body-registry-evidence",
        release_id=release_id,
        release_manifest_sha256=manifest_sha,
        cron_sha256=cron_sha,
    )
    _persist_diagnostic_report(sandbox, "real-body-registry-report.json", registry)
    body_coverage = _body_coverage(registry, enabled_ids)
    profiles, duration_coverage = _combined_duration_replay_profiles(
        source_root, jobs, cron_sha, registry
    )
    verify_compressed_active_duration_evidence(
        duration_coverage,
        registry,
        cron_jobs_sha256=cron_sha,
        require_complete=False,
    )
    occurrences = _occurrences(jobs, profiles, replay_start, dispatch_policy)
    capacity = simulate_layered_capacity(
        occurrences,
        slots=dispatch_policy.lane_caps,
        dispatch_policy=dispatch_policy,
    )
    classify_coalescing_safety(capacity)

    p95_complete = duration_coverage.get("certifying_p95_coverage") is True
    bodies_complete = body_coverage["body_adapter_coverage_complete"] is True
    deadlines_passed = capacity["latest_start_misses"] == capacity["deadline_misses"] == 0
    control_passed = bool(
        capacity["delivery_multiplier"] == _ARRIVAL_MULTIPLIER
        and capacity["all_deliveries_accounted"] is True
        and capacity["all_distinct_occurrences_accounted"] is True
        and capacity["same_job_concurrency_violations"] == 0
        and capacity["pending_per_job_limit"] == 1
        and capacity["coalescing_safety_passed"] is True
    )
    eligible = control_passed and p95_complete and bodies_complete and deadlines_passed
    blockers = [
        reason
        for condition, reason in (
            (
                not p95_complete,
                "RELEASE_BOUND_COMPRESSED_ACTIVE_P95_COVERAGE_INCOMPLETE",
            ),
            (not bodies_complete, "REAL_JOB_BODY_ADAPTER_COVERAGE_INCOMPLETE"),
            (capacity["latest_start_misses"] > 0, "PRODUCTION_DURATION_LATEST_START_MISSES"),
            (capacity["deadline_misses"] > 0, "PRODUCTION_DURATION_DEADLINE_MISSES"),
            (
                capacity["loss_sensitive_coalesced_occurrences"] > 0,
                "LOSS_SENSITIVE_SCHEDULE_OCCURRENCES_COALESCED",
            ),
        )
        if condition
    ]
    evidence: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "certified" if eligible else "incomplete",
        "workload": "seven_day_schedule_10x_arrival_2x_duration_replay",
        "validation_profile_id": profile_id,
        "release_binding": {
            "certifier_script_sha256": _sha256_file(SCRIPT_PATH),
            "cron_jobs_sha256": cron_sha,
            "duration_baseline_sha256": duration_coverage["baseline_sha256"],
            "duration_profiles_sha256": duration_coverage["duration_profiles_sha256"],
            "release_id": release_id,
            "release_manifest_sha256": manifest_sha,
            "real_job_body_registry_sha256": _sha256_file(registry_path),
            "real_job_body_registry_script_sha256": _sha256_file(
                source_root / "scripts/v3_validation/schedule_body_registry.py"
            ),
            "real_job_body_evidence_sha256": body_coverage["registry_evidence_sha256"],
            "dispatch_policy_sha256": dispatch_policy.policy_sha256,
        },
        "layers": {
            "control_plane": {
                "status": "passed" if control_passed else "failed",
                "uses_dispatcher_latency_as_body_duration": False,
                "measurements": capacity,
            },
            "business_body_plane": {
                "status": "passed"
                if p95_complete and bodies_complete and deadlines_passed
                else "incomplete",
                "arrival_multiplier": _ARRIVAL_MULTIPLIER,
                "duration_multiplier": _DURATION_MULTIPLIER,
                "duration_evidence": duration_coverage,
                "body_evidence": body_coverage,
                "deadline_measurements": {
                    "latest_start_misses": capacity["latest_start_misses"],
                    "deadline_misses": capacity["deadline_misses"],
                    "max_queue_delay_seconds": capacity["max_queue_delay_seconds"],
                },
                "dispatcher_or_help_latency_substituted": False,
            },
        },
        "gate": {
            "blocker_code": BLOCKER_CODE,
            "eligible_to_clear_schedule_realism_blocker": eligible,
            "decision": "clear" if eligible else "blocker_retained",
            "blocking_reasons": blockers,
        },
        "safety": {
            "live_state_accessed": False,
            "production_service_started": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "body_network_access_performed": registry.get("network_access_performed"),
            "body_external_network_access_performed": registry.get(
                "external_network_access_performed"
            ),
            "body_nas_access_performed": registry.get("nas_access_performed"),
            "body_production_database_access_performed": registry.get(
                "production_database_access_performed"
            ),
            "sandbox_writes_only": not registry.get("production_state_write_performed"),
            "dispatcher_module_modified_by_certifier": False,
            "release_bound_body_cache_reused": body_cache_reused,
        },
        "hash_scheme": "sha256(canonical-json-without-evidence_sha256)",
    }
    evidence["evidence_sha256"] = _sha256(evidence)
    verify_schedule_capacity_evidence(evidence)
    _persist_diagnostic_report(sandbox, "schedule-capacity-report.json", evidence)
    return evidence, registry


def run_schedule_capacity_certification(
    source_root: Path,
    workdir: Path,
    *,
    release_id: str = "unpackaged-source-capacity",
    release_manifest_sha256: str | None = None,
) -> dict[str, Any]:
    """Return the stable public capacity report used by existing callers."""

    evidence, _registry = _run_schedule_capacity_certification_bundle(
        source_root,
        workdir,
        release_id=release_id,
        release_manifest_sha256=release_manifest_sha256,
    )
    return evidence


def run_schedule_capacity_campaign_bundle(
    source_root: Path,
    workdir: Path,
    *,
    release_id: str,
    release_manifest_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return both raw reports needed by the G11 release-gate derivation."""

    return _run_schedule_capacity_certification_bundle(
        source_root,
        workdir,
        release_id=release_id,
        release_manifest_sha256=release_manifest_sha256,
    )


def campaign_evidence(
    report: Mapping[str, Any], body_evidence: Mapping[str, Any]
) -> dict[str, Any]:
    report_gate = report.get("gate")
    body_measurements = body_evidence.get("measurements")
    report_certified = (
        report.get("status") == "certified"
        and isinstance(report_gate, Mapping)
        and report_gate.get("eligible_to_clear_schedule_realism_blocker") is True
        and report_gate.get("blocking_reasons") == []
    )
    body_certified = (
        body_evidence.get("status") == "passed"
        and body_evidence.get("completion_claimed") is True
        and isinstance(body_measurements, Mapping)
        and body_measurements.get("all_safe_bodies_passed") is True
    )
    control = report.get("layers", {}).get("control_plane", {})
    control_measurements = (
        control.get("measurements") if isinstance(control, Mapping) else None
    )
    body_network = body_evidence.get("network_access_performed")
    return {
        "schema_version": 1,
        "workload": "seven_day_schedule_10x_arrival_2x_duration_replay",
        "status": "passed" if report_certified and body_certified else "failed",
        "measurements": {
            "validation_profile_id": report.get("validation_profile_id"),
            "enabled_jobs": (
                body_measurements.get("enabled_jobs")
                if isinstance(body_measurements, Mapping)
                else None
            ),
            "covered_jobs": (
                body_measurements.get("safe_adapter_coverage_jobs")
                if isinstance(body_measurements, Mapping)
                else None
            ),
            "passed_jobs": (
                body_measurements.get("body_jobs_passed")
                if isinstance(body_measurements, Mapping)
                else None
            ),
            "blocked_jobs": (
                body_measurements.get("blocked_jobs")
                if isinstance(body_measurements, Mapping)
                else None
            ),
            "latest_start_misses": (
                control_measurements.get("latest_start_misses")
                if isinstance(control_measurements, Mapping)
                else None
            ),
            "deadline_misses": (
                control_measurements.get("deadline_misses")
                if isinstance(control_measurements, Mapping)
                else None
            ),
        },
        "report": dict(report),
        "body_evidence": dict(body_evidence),
        # Loopback fixture providers are real socket activity.  Preserve that
        # fact while separately proving that no external network was reached.
        "network_access_performed": body_network,
        "external_network_access_performed": body_evidence.get(
            "external_network_access_performed"
        ),
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
        "live_state_access_performed": False,
    }


def _campaign_inputs() -> tuple[Path, str, str]:
    if os.environ.get("MAGI_V3_OFFLINE_CERTIFICATION") != "1":
        raise ScheduleCapacityError("campaign evidence requires offline certification mode")
    temporary = os.environ.get("TMPDIR", "").strip()
    profile_id = os.environ.get("MAGI_V3_VALIDATION_PROFILE_ID", "").strip()
    release_id = os.environ.get("MAGI_V3_RELEASE_ID", "").strip()
    manifest_sha = os.environ.get("MAGI_V3_RELEASE_MANIFEST_SHA256", "").strip()
    if not temporary or not profile_id or not release_id or not re.fullmatch(
        r"[0-9a-f]{64}", manifest_sha
    ):
        raise ScheduleCapacityError("campaign schedule environment binding is incomplete")
    temporary_root = Path(temporary).expanduser().resolve()
    temporary_root.mkdir(parents=True, exist_ok=True)
    workdir = Path(
        tempfile.mkdtemp(prefix=f"schedule-capacity-{profile_id}-", dir=temporary_root)
    )
    return workdir, release_id, manifest_sha


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--release-id", default="unpackaged-source-capacity")
    parser.add_argument("--release-manifest-sha256")
    parser.add_argument("--campaign-evidence", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.campaign_evidence:
            if args.workdir is not None or args.output is not None:
                raise ScheduleCapacityError(
                    "campaign evidence owns its workdir and cannot write an output path"
                )
            workdir, release_id, manifest_sha = _campaign_inputs()
            evidence, body_evidence = run_schedule_capacity_campaign_bundle(
                args.source_root.expanduser().resolve(),
                workdir,
                release_id=release_id,
                release_manifest_sha256=manifest_sha,
            )
        else:
            if args.workdir is None:
                raise ScheduleCapacityError("--workdir is required")
            evidence = run_schedule_capacity_certification(
                args.source_root.expanduser().resolve(),
                args.workdir,
                release_id=args.release_id,
                release_manifest_sha256=args.release_manifest_sha256,
            )
            body_evidence = None
        encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if args.output:
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(output.suffix + f".tmp-{os.getpid()}")
            temporary.write_text(encoded + "\n", encoding="utf-8")
            os.replace(temporary, output)
        if args.campaign_evidence:
            outer_evidence = campaign_evidence(evidence, body_evidence or {})
            outer = json.dumps(
                outer_evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if evidence.get("status") == "certified":
                _cleanup_certified_campaign_workdir(workdir)
            print("MAGI_V3_OFFLINE_EVIDENCE=" + outer)
        else:
            print(encoded)
        return 0 if evidence.get("status") == "certified" else 2
    except (ScheduleCapacityError, ScheduleBodyRegistryError, OfflineProbeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Synchronous durable lease dispatcher; callers decide when to tick it."""

from __future__ import annotations

import importlib
import json
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .errors import LeaseConflict, SupervisorError
from .ledger import (
    HEAVY_WORKER_CLASSES,
    PRIORITY_WEIGHTS,
    SAFE_PREEMPTION_SIDE_EFFECT_CLASSES,
    WORKER_CLASSES,
    JobLease,
    JobLedger,
    JobRecord,
)
from .resource import ResourceSnapshot
from .state import JobStatus
from .supervisor import WorkerResult, WorkerSpec, WorkerSupervisor

WorkerFactory = Callable[[JobRecord, JobLease], WorkerSpec]
SnapshotProvider = Callable[[], ResourceSnapshot]
_IO_RANK = {"none": 0, "light": 1, "heavy": 2}


def load_capability_worker_classes(path: Path | None = None) -> dict[str, str]:
    manifest_path = path or (
        Path(__file__).resolve().parents[1] / "config" / "v3_capability_manifest.json"
    )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to load capability manifest: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("capability manifest schema_version must equal 1")
    rows = payload.get("capabilities")
    if not isinstance(rows, list) or not rows:
        raise ValueError("capability manifest must contain capabilities")
    mapping: dict[str, str] = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ValueError(f"capability manifest row {index} must be an object")
        capability = row.get("id")
        worker_class = row.get("worker_class")
        if not isinstance(capability, str) or not capability.strip():
            raise ValueError(f"capability manifest row {index} has an invalid id")
        if worker_class not in WORKER_CLASSES:
            raise ValueError(f"capability manifest row {index} has an invalid worker_class")
        if capability in mapping:
            raise ValueError(f"capability manifest contains duplicate id: {capability}")
        mapping[capability] = worker_class
    return mapping


def _validated_capability_mapping(mapping: Mapping[str, str]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for capability, worker_class in mapping.items():
        if not isinstance(capability, str) or not capability.strip():
            raise ValueError("capability mapping contains an invalid id")
        if worker_class not in WORKER_CLASSES:
            raise ValueError(f"capability mapping has invalid worker class for {capability}")
        normalized[capability] = worker_class
    return normalized


@dataclass(frozen=True, slots=True)
class VerifiedCompletion:
    target: JobStatus
    business_completed: bool
    result: Any = None
    error: Mapping[str, Any] | None = None
    artifacts: tuple[Mapping[str, Any], ...] = ()
    side_effect_receipts: tuple[Mapping[str, Any], ...] = ()

    def validate(self, job: JobRecord | None = None) -> None:
        if self.target not in {
            JobStatus.SUCCEEDED,
            JobStatus.DEGRADED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.TIMED_OUT,
            JobStatus.WAITING_CHILDREN,
            JobStatus.AWAITING_INPUT,
            JobStatus.NEEDS_CONFIRMATION,
            JobStatus.DEFERRED,
        }:
            raise ValueError(f"unsupported verified completion target: {self.target.value}")
        if self.target is JobStatus.SUCCEEDED:
            if not self.business_completed:
                raise ValueError("succeeded completion requires business_completed=true")
            if not self.artifacts and not self.side_effect_receipts:
                raise ValueError("succeeded completion requires artifact or side-effect receipt evidence")
            if (
                job is not None
                and job.side_effect_class in {"external_commit", "destructive"}
                and not self.side_effect_receipts
            ):
                raise ValueError("dangerous successful completion requires a side-effect receipt")
        elif self.business_completed:
            raise ValueError("non-success completion cannot claim business completion")
        if self.target in {JobStatus.FAILED, JobStatus.TIMED_OUT} and self.error is None:
            raise ValueError("failed or timed-out completion requires an error")


CompletionVerifier = Callable[[JobRecord, JobLease, WorkerResult], VerifiedCompletion]


def load_capability_worker_adapter(
    capability: str,
    path: Path | None = None,
) -> tuple[WorkerFactory, CompletionVerifier]:
    """Resolve one explicitly declared V3 capability adapter from the manifest."""

    manifest_path = path or (
        Path(__file__).resolve().parents[1] / "config" / "v3_capability_manifest.json"
    )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to load capability manifest: {exc}") from exc
    rows = payload.get("capabilities") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(rows, list)
    ):
        raise ValueError("capability manifest is invalid")
    row = next(
        (item for item in rows if isinstance(item, dict) and item.get("id") == capability),
        None,
    )
    if row is None:
        raise ValueError(f"unknown capability: {capability}")
    adapter = row.get("v3_worker_adapter")
    if not isinstance(adapter, dict):
        raise ValueError(f"capability has no V3 worker adapter: {capability}")
    module_name = adapter.get("module")
    factory_name = adapter.get("worker_factory")
    verifier_name = adapter.get("completion_verifier")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (module_name, factory_name, verifier_name)
    ):
        raise ValueError(f"capability has an invalid V3 worker adapter: {capability}")
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, factory_name)
        verifier = getattr(module, verifier_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"unable to load V3 worker adapter for {capability}: {exc}") from exc
    if not callable(factory) or not callable(verifier):
        raise ValueError(f"V3 worker adapter is not callable for {capability}")
    return factory, verifier


@dataclass(frozen=True, slots=True)
class DispatchHandle:
    lease: JobLease
    worker_pid: int
    preemptions: tuple["PreemptionOutcome", ...] = ()


@dataclass(frozen=True, slots=True)
class PreemptionOutcome:
    job: JobRecord
    worker_result: WorkerResult
    incoming_priority_class: str


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    job: JobRecord
    worker_result: WorkerResult | None
    fenced: bool


class DurableDispatcher:
    """The only runtime component that translates worker exit into ledger state."""

    def __init__(
        self,
        *,
        ledger: JobLedger,
        supervisor: WorkerSupervisor,
        worker_factory: WorkerFactory,
        completion_verifier: CompletionVerifier,
        snapshot_provider: SnapshotProvider,
        owner_id: str,
        lease_seconds: int = 60,
        preemption_grace_sec: float = 0.5,
        capability_worker_classes: Mapping[str, str] | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("dispatcher owner_id is required")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        if (
            isinstance(preemption_grace_sec, bool)
            or not isinstance(preemption_grace_sec, (int, float))
            or not 0.0 <= float(preemption_grace_sec) <= 30.0
        ):
            raise ValueError("preemption_grace_sec must be in [0, 30]")
        self.ledger = ledger
        self.supervisor = supervisor
        self.worker_factory = worker_factory
        self.completion_verifier = completion_verifier
        self.snapshot_provider = snapshot_provider
        self.owner_id = owner_id
        self.lease_seconds = lease_seconds
        self.preemption_grace_sec = float(preemption_grace_sec)
        self.capability_worker_classes = _validated_capability_mapping(
            load_capability_worker_classes()
            if capability_worker_classes is None
            else capability_worker_classes
        )
        self._lock = threading.RLock()
        self._active: dict[str, DispatchHandle] = {}

    def dispatch_next(
        self,
        *,
        now: datetime | None = None,
        interactive: bool = False,
    ) -> DispatchHandle | None:
        instant = now or datetime.now(timezone.utc)
        with self._lock:
            lease = self.ledger.lease_next(
                self.owner_id,
                worker_classes=("light",) if interactive else None,
                priority_classes=("P0", "P1") if interactive else None,
                lease_seconds=self.lease_seconds,
                now=instant,
            )
            if lease is None:
                return None
            try:
                expected_worker_class = self.capability_worker_classes.get(lease.job.capability)
                if expected_worker_class is None:
                    raise ValueError(
                        f"unknown capability in capability manifest: {lease.job.capability}"
                    )
                if expected_worker_class != lease.job.worker_class:
                    raise ValueError(
                        "ledger worker_class conflicts with capability manifest: "
                        f"{lease.job.capability} requires {expected_worker_class}"
                    )
                requested = self.worker_factory(lease.job, lease)
                requested.validate()
                spec = self._bind_worker_spec(
                    requested,
                    lease,
                    interactive=interactive,
                )
            except BaseException as exc:
                self._settle_unstarted_failure(
                    lease,
                    error={"code": "worker_factory_failed", "message": str(exc)},
                    now=now or datetime.now(timezone.utc),
                )
                raise
            try:
                preemptions = self._preempt_for_interactive(
                    lease,
                    now=instant,
                ) if interactive else ()
            except BaseException as exc:
                self._settle_unstarted_failure(
                    lease,
                    error={
                        "code": "interactive_preemption_failed",
                        "message": str(exc),
                    },
                    now=now or datetime.now(timezone.utc),
                )
                raise
            try:
                self.ledger.mark_running(
                    lease.token,
                    owner_id=lease.owner_id,
                    attempt_number=lease.attempt_number,
                    worker_pid=None,
                    now=instant,
                )
            except BaseException as exc:
                self._settle_unstarted_failure(
                    lease,
                    error={"code": "mark_running_failed", "message": str(exc)},
                    now=now or datetime.now(timezone.utc),
                )
                raise
            try:
                pid = self.supervisor.start(spec, self.snapshot_provider())
            except BaseException as exc:
                failure_instant = now or datetime.now(timezone.utc)
                try:
                    if interactive:
                        self.ledger.suspend_lease(
                            lease.token,
                            JobStatus.DEFERRED,
                            owner_id=lease.owner_id,
                            attempt_number=lease.attempt_number,
                            error={
                                "code": "interactive_worker_start_recovery_required",
                                "message": str(exc),
                            },
                            now=failure_instant,
                        )
                    else:
                        self.ledger._commit_worker_result(
                            lease.token,
                            JobStatus.FAILED,
                            owner_id=lease.owner_id,
                            attempt_number=lease.attempt_number,
                            error={"code": "worker_start_failed", "message": str(exc)},
                            now=failure_instant,
                        )
                except LeaseConflict:
                    fence = self.ledger.fence_lease(
                        lease.token,
                        owner_id=lease.owner_id,
                        attempt_number=lease.attempt_number,
                        now=failure_instant,
                    )
                    self.ledger.resolve_fenced_lease(
                        lease.token,
                        owner_id=lease.owner_id,
                        attempt_number=lease.attempt_number,
                        fence_generation=fence.fence_generation,
                        process_group_gone=True,
                        now=failure_instant,
                    )
                raise
            try:
                bind_instant = now or datetime.now(timezone.utc)
                self.ledger.bind_worker_pid(
                    lease.token,
                    owner_id=lease.owner_id,
                    attempt_number=lease.attempt_number,
                    worker_pid=pid,
                    now=bind_instant,
                )
            except BaseException:
                result = self.supervisor.terminate(lease.job.job_id, grace_sec=0.2)
                try:
                    self.ledger._commit_worker_result(
                        lease.token,
                        JobStatus.FAILED,
                        owner_id=lease.owner_id,
                        attempt_number=lease.attempt_number,
                        error={"code": "worker_pid_bind_failed"},
                        now=bind_instant,
                    )
                except LeaseConflict:
                    fence = self.ledger.fence_lease(
                        lease.token,
                        owner_id=lease.owner_id,
                        attempt_number=lease.attempt_number,
                        now=bind_instant,
                    )
                    self.ledger.resolve_fenced_lease(
                        lease.token,
                        owner_id=lease.owner_id,
                        attempt_number=lease.attempt_number,
                        fence_generation=fence.fence_generation,
                        process_group_gone=result.process_group_gone,
                        now=bind_instant,
                    )
                raise
            handle = DispatchHandle(
                lease=lease,
                worker_pid=pid,
                preemptions=preemptions,
            )
            self._active[lease.job.job_id] = handle
            return handle

    @staticmethod
    def _bind_worker_spec(
        requested: WorkerSpec,
        lease: JobLease,
        *,
        interactive: bool = False,
    ) -> WorkerSpec:
        job = lease.job
        if requested.job_id != job.job_id:
            raise ValueError("worker factory returned a different job_id")
        if requested.worker_class != job.worker_class:
            raise ValueError("worker factory returned a different worker_class")
        if requested.inherit_environment:
            raise ValueError("durable dispatcher workers cannot inherit the ambient environment")
        if requested.interactive:
            raise ValueError("worker factory cannot enable interactive admission")
        if requested.priority_class != job.priority_class:
            raise ValueError("worker factory priority_class must match the ledger")
        claim = job.resource_claim
        maximums = {
            "estimated_footprint_mb": claim["memory_mb"],
            "estimated_metal_mb": claim["metal_mb"],
            "cpu_percent": claim["cpu_percent"],
            "browser_tokens": claim["browser_tokens"],
        }
        for field_name, maximum in maximums.items():
            if getattr(requested, field_name) > maximum:
                raise ValueError(f"worker factory {field_name} exceeds ledger resource_claim")
        for field_name in ("disk_io", "nas_io", "network"):
            if _IO_RANK[getattr(requested, field_name)] > _IO_RANK[claim[field_name]]:
                raise ValueError(f"worker factory {field_name} exceeds ledger resource_claim")
        if requested.timeout_sec > job.timeout_sec:
            raise ValueError("worker factory timeout_sec exceeds ledger timeout_sec")
        if job.worker_class == "browser" and claim["browser_tokens"] != 1:
            raise ValueError("browser jobs must reserve one browser token")
        return replace(
            requested,
            estimated_footprint_mb=claim["memory_mb"],
            estimated_metal_mb=claim["metal_mb"],
            cpu_percent=claim["cpu_percent"],
            disk_io=claim["disk_io"],
            nas_io=claim["nas_io"],
            network=claim["network"],
            browser_tokens=claim["browser_tokens"],
            priority_class=job.priority_class,
            interactive=interactive,
            timeout_sec=job.timeout_sec,
            attempt_number=lease.attempt_number,
            lease_token=lease.token,
        )

    def _preempt_for_interactive(
        self,
        incoming: JobLease,
        *,
        now: datetime,
    ) -> tuple[PreemptionOutcome, ...]:
        if incoming.job.worker_class != "light" or incoming.job.priority_class not in {
            "P0",
            "P1",
        }:
            raise LeaseConflict("interactive dispatch requires a P0/P1 light job")
        victims = sorted(
            (
                handle
                for handle in self._active.values()
                if handle.lease.owner_id == self.owner_id
                and handle.lease.job.worker_class in HEAVY_WORKER_CLASSES
                and handle.lease.job.preemptible
                and handle.lease.job.side_effect_class
                in SAFE_PREEMPTION_SIDE_EFFECT_CLASSES
                and PRIORITY_WEIGHTS[handle.lease.job.priority_class]
                < PRIORITY_WEIGHTS[incoming.job.priority_class]
            ),
            key=lambda handle: (
                PRIORITY_WEIGHTS[handle.lease.job.priority_class],
                handle.lease.job.created_at,
                handle.lease.job.job_id,
            ),
        )
        outcomes: list[PreemptionOutcome] = []
        for victim in victims:
            job_id = victim.lease.job.job_id
            if not self.supervisor.owns_worker(
                job_id,
                attempt_number=victim.lease.attempt_number,
                lease_token=victim.lease.token,
            ):
                raise SupervisorError(
                    f"dispatcher does not own the preemption target: {job_id}"
                )
            fence = self.ledger.fence_preemptible_lease(
                victim.lease.token,
                owner_id=victim.lease.owner_id,
                attempt_number=victim.lease.attempt_number,
                incoming_priority_class=incoming.job.priority_class,
                now=now,
            )
            try:
                result = self.supervisor.terminate(
                    job_id,
                    grace_sec=self.preemption_grace_sec,
                )
            except BaseException:
                # No PGID-gone proof exists, so the only safe recovery is to
                # restore the still-owned running lease and let normal polling
                # settle any TERM-induced exit.
                self.ledger.cancel_preemption_fence(
                    victim.lease.token,
                    owner_id=victim.lease.owner_id,
                    attempt_number=victim.lease.attempt_number,
                    fence_generation=fence.fence_generation,
                )
                raise
            if (
                result.job_id != job_id
                or result.pid != victim.worker_pid
                or result.attempt_number != victim.lease.attempt_number
                or result.lease_token != victim.lease.token
                or not result.process_group_gone
            ):
                if (
                    result.job_id == job_id
                    and result.pid == victim.worker_pid
                    and result.process_group_gone
                ):
                    self.ledger.mark_preemption_recovery_required(
                        victim.lease.token,
                        owner_id=victim.lease.owner_id,
                        attempt_number=victim.lease.attempt_number,
                        fence_generation=fence.fence_generation,
                        process_group_gone=True,
                        error={
                            "code": "preemption_result_identity_recovery_required"
                        },
                        now=now,
                    )
                    self._active.pop(job_id, None)
                else:
                    self.ledger.cancel_preemption_fence(
                        victim.lease.token,
                        owner_id=victim.lease.owner_id,
                        attempt_number=victim.lease.attempt_number,
                        fence_generation=fence.fence_generation,
                    )
                raise LeaseConflict(
                    "preemption result does not prove the owned process group is gone"
                )
            try:
                requeued = self.ledger.resolve_preempted_lease(
                    victim.lease.token,
                    owner_id=victim.lease.owner_id,
                    attempt_number=victim.lease.attempt_number,
                    fence_generation=fence.fence_generation,
                    process_group_gone=result.process_group_gone,
                    incoming_priority_class=incoming.job.priority_class,
                    now=now,
                )
            except BaseException as exc:
                recovery = self.ledger.mark_preemption_recovery_required(
                    victim.lease.token,
                    owner_id=victim.lease.owner_id,
                    attempt_number=victim.lease.attempt_number,
                    fence_generation=fence.fence_generation,
                    process_group_gone=result.process_group_gone,
                    error={
                        "code": "preemption_requeue_recovery_required",
                        "message": str(exc),
                    },
                    now=now,
                )
                self._active.pop(job_id, None)
                raise SupervisorError(
                    "preempted worker was drained but automatic requeue failed; "
                    f"job is {recovery.status.value}"
                ) from exc
            self._active.pop(job_id, None)
            outcomes.append(
                PreemptionOutcome(
                    job=requeued,
                    worker_result=result,
                    incoming_priority_class=incoming.job.priority_class,
                )
            )
        return tuple(outcomes)

    def heartbeat(self, job_id: str, *, now: datetime | None = None) -> str:
        instant = now or datetime.now(timezone.utc)
        with self._lock:
            handle = self._require_active(job_id)
            expires_at = self.ledger.heartbeat_lease(
                handle.lease.token,
                owner_id=handle.lease.owner_id,
                attempt_number=handle.lease.attempt_number,
                extend_seconds=self.lease_seconds,
                now=instant,
            )
            self._active[job_id] = replace(
                handle,
                lease=replace(handle.lease, expires_at=expires_at),
            )
            return expires_at

    def poll(self, job_id: str, *, now: datetime | None = None) -> DispatchOutcome | None:
        instant = now or datetime.now(timezone.utc)
        with self._lock:
            handle = self._require_active(job_id)
            result = self.supervisor.poll(job_id)
            if result is not None:
                return self.commit_result(job_id, result, now=now)
            if _as_utc(handle.lease.expires_at) <= _as_utc(instant):
                return self.expire(job_id, now=instant)
            return None

    def commit_result(
        self,
        job_id: str,
        result: WorkerResult,
        *,
        now: datetime | None = None,
    ) -> DispatchOutcome:
        """Commit a result only when its durable lease identity is still current."""

        with self._lock:
            handle = self._require_active(job_id)
            if (
                result.job_id != job_id
                or result.attempt_number != handle.lease.attempt_number
                or result.lease_token != handle.lease.token
                or result.pid != handle.worker_pid
                or not result.process_group_gone
            ):
                raise LeaseConflict("worker result does not match the active lease generation")
            if result.timed_out:
                completion = VerifiedCompletion(
                    target=JobStatus.TIMED_OUT,
                    business_completed=False,
                    error={"code": "worker_timed_out", "returncode": result.returncode},
                )
            elif result.killed:
                completion = VerifiedCompletion(
                    target=JobStatus.FAILED,
                    business_completed=False,
                    error={"code": "worker_process_group_forced_closed"},
                )
            elif result.returncode != 0:
                completion = VerifiedCompletion(
                    target=JobStatus.FAILED,
                    business_completed=False,
                    error={"code": "worker_failed", "returncode": result.returncode},
                )
            else:
                try:
                    completion = self.completion_verifier(handle.lease.job, handle.lease, result)
                    completion.validate(handle.lease.job)
                except BaseException as exc:
                    completion = VerifiedCompletion(
                        target=JobStatus.FAILED,
                        business_completed=False,
                        error={"code": "completion_verification_failed", "message": str(exc)},
                    )
            evidence_result = {
                "worker_returncode": result.returncode,
                "result": completion.result,
                "artifacts": [dict(item) for item in completion.artifacts],
                "side_effect_receipts": [
                    dict(item) for item in completion.side_effect_receipts
                ],
            }
            commit_instant = now or datetime.now(timezone.utc)
            try:
                if completion.target in {
                    JobStatus.WAITING_CHILDREN,
                    JobStatus.AWAITING_INPUT,
                    JobStatus.NEEDS_CONFIRMATION,
                    JobStatus.DEFERRED,
                }:
                    job = self.ledger.suspend_lease(
                        handle.lease.token,
                        completion.target,
                        owner_id=handle.lease.owner_id,
                        attempt_number=handle.lease.attempt_number,
                        result=evidence_result,
                        error=completion.error,
                        now=commit_instant,
                    )
                else:
                    job = self.ledger._commit_worker_result(
                        handle.lease.token,
                        completion.target,
                        owner_id=handle.lease.owner_id,
                        attempt_number=handle.lease.attempt_number,
                        result=evidence_result,
                        error=completion.error,
                        metrics={"duration_ms": round(result.duration_sec * 1000)},
                        business_completed=completion.business_completed,
                        now=commit_instant,
                    )
            except LeaseConflict:
                return self._resolve_expired(handle, result=result, now=commit_instant)
            self._active.pop(job_id, None)
            return DispatchOutcome(job=job, worker_result=result, fenced=False)

    def expire(self, job_id: str, *, now: datetime | None = None) -> DispatchOutcome:
        instant = now or datetime.now(timezone.utc)
        with self._lock:
            handle = self._require_active(job_id)
            return self._resolve_expired(handle, result=None, now=instant)

    def active_job_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._active)

    def _resolve_expired(
        self,
        handle: DispatchHandle,
        *,
        result: WorkerResult | None,
        now: datetime,
    ) -> DispatchOutcome:
        fence = self.ledger.fence_lease(
            handle.lease.token,
            owner_id=handle.lease.owner_id,
            attempt_number=handle.lease.attempt_number,
            now=now,
        )
        if result is None:
            if not self.supervisor.owns_worker(
                handle.lease.job.job_id,
                attempt_number=handle.lease.attempt_number,
                lease_token=handle.lease.token,
            ):
                raise SupervisorError("dispatcher no longer owns the fenced worker")
            result = self.supervisor.terminate(handle.lease.job.job_id, grace_sec=0.2)
        if (
            result.attempt_number != handle.lease.attempt_number
            or result.lease_token != handle.lease.token
            or result.pid != handle.worker_pid
            or not result.process_group_gone
        ):
            raise LeaseConflict("cannot resolve fence without matching process-group evidence")
        job = self.ledger.resolve_fenced_lease(
            handle.lease.token,
            owner_id=handle.lease.owner_id,
            attempt_number=handle.lease.attempt_number,
            fence_generation=fence.fence_generation,
            process_group_gone=result.process_group_gone,
            now=now,
        )
        self._active.pop(handle.lease.job.job_id, None)
        return DispatchOutcome(job=job, worker_result=result, fenced=True)

    def _require_active(self, job_id: str) -> DispatchHandle:
        handle = self._active.get(job_id)
        if handle is None:
            raise LeaseConflict(f"no active dispatch for job: {job_id}")
        return handle

    def _settle_unstarted_failure(
        self,
        lease: JobLease,
        *,
        error: Mapping[str, Any],
        now: datetime,
    ) -> None:
        try:
            self.ledger.abandon_unstarted_lease(
                lease.token,
                owner_id=lease.owner_id,
                attempt_number=lease.attempt_number,
                error=error,
                now=now,
            )
        except LeaseConflict:
            fence = self.ledger.fence_lease(
                lease.token,
                owner_id=lease.owner_id,
                attempt_number=lease.attempt_number,
                now=now,
            )
            self.ledger.resolve_fenced_lease(
                lease.token,
                owner_id=lease.owner_id,
                attempt_number=lease.attempt_number,
                fence_generation=fence.fence_generation,
                process_group_gone=True,
                now=now,
            )


def _as_utc(value: datetime | str) -> datetime:
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

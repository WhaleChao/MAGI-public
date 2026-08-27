"""Discord-independent scheduled-job owner for MAGI V3.

The module is side-effect free at import time.  Legacy scheduler, orchestrator,
thread-pool, lock and subprocess code are loaded only from :meth:`CronService.run`.
"""

from __future__ import annotations

import importlib
import hashlib
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol

from magi_v3.cron_policy import CronDispatchPolicy, load_cron_dispatch_policy
from magi_v3.faiss_maintenance import (
    INTERNAL_REBUILD_JOB_ID,
    REBUILD_LANE,
    FaissRebuildCoordinator,
)
from magi_v3.cron_macros import resolve_exact_cron_macro
from skills.ops.cron_command_identity import command_definition_sha256

LOGGER = logging.getLogger("magi_v3.cron")

# Cron definitions are sealed and hash-bound deployment inputs.  Their child
# processes still need the provider/database configuration loaded by the V3
# launcher, so use an explicit cron-only allowlist instead of broadening
# SafeProcess for every caller.
CRON_ENV_WHITELIST_PREFIXES = (
    "MAGI_",
    "JUDICIAL_",
    "OSC_",
    "NVIDIA_",
    "GOOGLE_",
    "GMAIL_",
    "OPENAI_",
    "GEMINI_",
    "GROQ_",
    "OPENCLAW_",
    "DISCORD_",
    "TELEGRAM_",
    "LINE_",
    "CLOUDFLARE_",
    "TAILSCALE_",
    "APPLE_",
    "MLX_",
    "WHISPER_",
    "LAF_",
    "DB_",
    "MYSQL_",
    "PATH",
    "HOME",
    "USER",
    "PYTHONPATH",
    "LANG",
    "LC_",
    "TZ",
)

# The bi-hourly deterministic autopilot executes several bounded diagnostics in
# sequence.  The sealed schedule historically carried the generic 600-second
# timeout, which is shorter than the operation's own safe completion window and
# caused a SIGTERM to be recorded as a product failure.  Keep the schedule
# binding intact, but enforce a narrow floor at the execution boundary.  This
# is deliberately not a broad timeout increase for arbitrary cron jobs.
CRON_TIMEOUT_FLOORS_SECONDS = {
    "job_1770948489644_c5a469": 1800,
}


def _cron_occurrence_id(job: dict[str, Any], command_sha256: str) -> str:
    supplied = str(job.get("_magi_occurrence_id") or "").strip().lower()
    if len(supplied) == 64 and all(char in "0123456789abcdef" for char in supplied):
        return supplied
    due_at = str(job.get("_magi_due_at") or "").strip()
    if not due_at:
        # A due occurrence normally carries _magi_due_at.  This fallback is
        # used only by direct execution adapters and is propagated into every
        # retry before the first process exits.
        due_at = datetime.now().isoformat(timespec="minutes")
    raw = "\0".join((str(job.get("id") or ""), command_sha256, due_at))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cron_timeout_seconds(runtime_policy: Any, job: dict[str, Any]) -> int:
    """Return the sealed policy timeout with narrowly-approved safety floors."""

    configured = int(runtime_policy.cron_job_timeout(job))
    floor = int(CRON_TIMEOUT_FLOORS_SECONDS.get(str(job.get("id") or ""), 0))
    return max(configured, floor)


def _load_bound_cron_environment() -> None:
    """Load the hash-bound V3 environment before any scheduled child starts."""

    raw = os.environ.get("MAGI_ENV_FILE", "").strip()
    if not raw:
        return
    path = Path(raw).expanduser()
    if (
        not path.is_absolute()
        or path.is_symlink()
        or not path.is_file()
    ):
        raise CronServiceError("MAGI_ENV_FILE is not a safe regular file")
    expected = os.environ.get("MAGI_ENV_FILE_SHA256", "").strip().lower()
    if expected:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected:
            raise CronServiceError("MAGI_ENV_FILE SHA-256 mismatch")
    try:
        from dotenv import load_dotenv
    except ImportError as exc:
        raise CronServiceError("python-dotenv is required for MAGI_ENV_FILE") from exc
    if load_dotenv(path, override=False) is False:
        raise CronServiceError("MAGI_ENV_FILE could not be loaded")


class CronServiceError(RuntimeError):
    """The dedicated scheduler cannot safely acquire or retain ownership."""


class Scheduler(Protocol):
    def reconcile_incomplete_jobs(self) -> list[str]: ...

    def reconcile_terminal_schedule_deferrals(self) -> list[str]: ...
    def rearm_recovered_resource_deferrals(self) -> list[str]: ...
    def peek_due_jobs(self) -> list[dict[str, Any]]: ...
    def get_missed_jobs_v3(self, hours: int) -> list[dict[str, Any]]: ...
    def recover_v3_pending_jobs(self) -> list[dict[str, Any]]: ...
    def mark_job_v3_pending(
        self, job_id: str, *, due_at: str, effective_at: str, lane: str
    ) -> Any: ...
    def mark_job_dispatched(self, job_id: str) -> Any: ...
    def mark_job_started(self, job_id: str, **kwargs: Any) -> Any: ...
    def mark_job_result(self, job_id: str, **kwargs: Any) -> Any: ...


class OwnerLock(Protocol):
    acquired: bool
    path: Path
    active_owner: dict[str, Any] | None

    def release(self) -> None: ...


@dataclass(frozen=True, slots=True)
class CronServiceConfig:
    release_root: Path
    poll_interval_seconds: float = 60.0
    max_workers: int = 4
    catchup_hours: int = 8
    catchup_min_hour: int = 6
    notification_flush_interval_seconds: float = 300.0

    def validated(self) -> "CronServiceConfig":
        root = self.release_root.expanduser().resolve()
        if not (root / "skills" / "ops" / "cron_scheduler.py").is_file():
            raise CronServiceError("release root does not contain the cron scheduler")
        if self.poll_interval_seconds <= 0:
            raise CronServiceError("cron poll interval must be positive")
        return CronServiceConfig(
            release_root=root,
            poll_interval_seconds=float(self.poll_interval_seconds),
            max_workers=max(1, min(6, int(self.max_workers))),
            catchup_hours=max(0, min(48, int(self.catchup_hours))),
            catchup_min_hour=max(0, min(23, int(self.catchup_min_hour))),
            notification_flush_interval_seconds=max(
                30.0, min(3600.0, float(self.notification_flush_interval_seconds))
            ),
        )


@dataclass(frozen=True, slots=True)
class PendingCronJob:
    job: dict[str, Any]
    lane: str
    scheduled_at: float
    not_before: float
    latest_start_at: float
    sequence: int


def _scheduler_factory() -> Scheduler:
    return importlib.import_module("skills.ops.cron_scheduler").CronScheduler()


def _owner_lock_factory() -> OwnerLock:
    locks = importlib.import_module("scripts.ops.background_task_locks")
    return locks.acquire_lock(
        locks.SCHEDULER_LOCK_NAME,
        owner="v3_dedicated_cron",
        kind="scheduler",
        blocking=False,
    )


def _notification_outbox_flusher() -> dict[str, Any]:
    """Retry durable business notifications without waiting for a new alert."""

    red_phone = importlib.import_module("skills.ops.red_phone")
    return red_phone.flush_pending_alerts(max_items=8)


class CronService:
    def __init__(
        self,
        config: CronServiceConfig,
        *,
        scheduler_factory: Callable[[], Scheduler] = _scheduler_factory,
        owner_lock_factory: Callable[[], OwnerLock] = _owner_lock_factory,
        process_runner: Callable[..., Any] | None = None,
        orchestrator_factory: Callable[[], Any] | None = None,
        executor_factory: Callable[[int], Any] | None = None,
        dispatch_policy: CronDispatchPolicy | None = None,
        faiss_coordinator: FaissRebuildCoordinator | None = None,
        notification_flusher: Callable[[], Any] = _notification_outbox_flusher,
    ) -> None:
        _load_bound_cron_environment()
        self.config = config.validated()
        self.scheduler_factory = scheduler_factory
        self.owner_lock_factory = owner_lock_factory
        self.process_runner = process_runner
        self.orchestrator_factory = orchestrator_factory
        self.executor_factory = executor_factory or (
            lambda workers: ThreadPoolExecutor(max_workers=workers, thread_name_prefix="magi-v3-cron")
        )
        self.dispatch_policy = dispatch_policy or load_cron_dispatch_policy(
            self.config.release_root
        )
        self.faiss_coordinator = faiss_coordinator or FaissRebuildCoordinator(
            self.config.release_root
        )
        self.notification_flusher = notification_flusher
        if self.dispatch_policy.max_workers != self.config.max_workers:
            raise CronServiceError(
                "cron worker count must match the hash-bound three-lane dispatch policy"
            )
        self._orchestrator: Any | None = None
        self._orchestrator_lock = threading.Lock()
        self._stop_event: threading.Event | None = None

    def _get_orchestrator(self) -> Any:
        if self._orchestrator is not None:
            return self._orchestrator
        with self._orchestrator_lock:
            if self._orchestrator is None:
                factory = self.orchestrator_factory
                if factory is None:
                    factory = importlib.import_module("api.orchestrator").Orchestrator
                self._orchestrator = factory()
        return self._orchestrator

    def _record(
        self,
        scheduler: Scheduler,
        job_id: str,
        started_at: float,
        command_sha256: str,
        *,
        success: bool,
        returncode: int | None,
        timed_out: bool = False,
        error: str = "",
        stdout: str = "",
        stderr: str = "",
        status: str = "",
        terminal_deferred: bool = False,
    ) -> None:
        recorded = scheduler.mark_job_result(
            job_id,
            success=success,
            returncode=returncode,
            timed_out=timed_out,
            error=error[-1200:],
            stdout_tail=stdout[-1200:],
            stderr_tail=stderr[-1200:],
            duration_sec=max(0.0, time.time() - started_at),
            status=status,
            terminal_deferred=terminal_deferred,
            command_sha256=command_sha256,
        )
        if recorded is not True:
            LOGGER.warning(
                "cron completion marker rejected for %s "
                "(command_sha256=%s, status=%s)",
                job_id,
                command_sha256,
                status or ("success" if success else "failed"),
            )

    def _record_with_recovery(
        self,
        scheduler: Scheduler,
        job: dict[str, Any],
        started_at: float,
        command_sha256: str,
        *,
        success: bool,
        returncode: int | None,
        timed_out: bool = False,
        error: str = "",
        stdout: str = "",
        stderr: str = "",
        status: str = "",
    ) -> None:
        """Persist success or convert a recoverable failure into durable retry."""

        job_id = str(job.get("id") or "").strip()
        occurrence_id = _cron_occurrence_id(job, command_sha256)
        if success:
            self._record(
                scheduler,
                job_id,
                started_at,
                command_sha256,
                success=True,
                returncode=returncode,
                timed_out=timed_out,
                stdout=stdout,
                stderr=stderr,
                status=status,
            )
            return

        result_policy = importlib.import_module("skills.ops.cron_result_policy")
        terminal_deferred_reason = result_policy.terminal_schedule_deferral_reason(
            stdout, error
        )
        if str(status or "").strip().lower() == "deferred" and terminal_deferred_reason:
            self._record(
                scheduler,
                job_id,
                started_at,
                command_sha256,
                success=False,
                returncode=75,
                timed_out=False,
                error=terminal_deferred_reason,
                stdout=stdout,
                stderr=stderr,
                status="deferred",
                terminal_deferred=True,
            )
            return

        schedule_retry = getattr(scheduler, "schedule_job_v3_retry", None)
        # Compatibility for isolated tests and older read-only scheduler
        # adapters.  Production V3 always provides the durable retry method.
        if not callable(schedule_retry):
            self._record(
                scheduler,
                job_id,
                started_at,
                command_sha256,
                success=False,
                returncode=returncode,
                timed_out=timed_out,
                error=error,
                stdout=stdout,
                stderr=stderr,
                status=status,
            )
            return

        recovery = importlib.import_module("magi_v3.business_recovery")
        decision = recovery.decide_recovery(
            job,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            error=error,
            status=status,
            timed_out=timed_out,
        )
        if decision.retryable:
            retry = schedule_retry(
                job_id,
                command_sha256=command_sha256,
                occurrence_id=occurrence_id,
                reason_code=decision.reason_code,
                public_reason=decision.public_reason,
                max_attempts=decision.max_attempts,
                delays_seconds=decision.retry_delays_seconds,
                source_returncode=returncode,
                source_timed_out=timed_out,
            )
            if bool(retry.get("scheduled")):
                retry_message = recovery.retry_status_message(
                    decision,
                    attempt=int(retry.get("attempt") or 1),
                    retry_at=str(retry.get("retry_at") or "稍後"),
                )
                self._record(
                    scheduler,
                    job_id,
                    started_at,
                    command_sha256,
                    success=False,
                    # The durable occurrence is now a retry deferral.  Keep
                    # the original rc/timeout inside v3_retry evidence while
                    # recording EX_TEMPFAIL as the public scheduler outcome.
                    returncode=75,
                    timed_out=False,
                    error=retry_message,
                    stdout=stdout,
                    stderr=stderr,
                    status="deferred",
                )
                return
            if bool(retry.get("exhausted")):
                error = recovery.exhausted_status_message(decision)
            else:
                error = (
                    "MAGI 無法建立持久化自動重試，已保留本輪證據；"
                    f"原因：{decision.public_reason}。"
                )
        elif decision.human_required:
            error = recovery.human_status_message(decision)
        elif not error:
            error = f"MAGI 已保留失敗證據；原因：{decision.public_reason}。"

        # A structured deferral is a completed, fail-closed wait state. It may
        # receive bounded near-term retries, but exhausting those retries must
        # not rewrite the original wait into a product failure. The next
        # ordinary schedule remains responsible for continuing the work.
        deferred_wait = str(status or "").strip().lower() == "deferred"
        self._record(
            scheduler,
            job_id,
            started_at,
            command_sha256,
            success=False,
            returncode=75 if deferred_wait else returncode,
            timed_out=False if deferred_wait else timed_out,
            error=error,
            stdout=stdout,
            stderr=stderr,
            status="deferred" if deferred_wait else "failed",
        )

    def _execute(self, scheduler: Scheduler, job: dict[str, Any]) -> None:
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            return
        command = str(job.get("command") or "")
        command_sha256 = command_definition_sha256(command)
        started_at = time.time()
        try:
            start_confirmed = (
                scheduler.mark_job_started(
                    job_id, command_sha256=command_sha256
                )
                is True
            )
        except Exception:
            LOGGER.exception(
                "scheduled job %s blocked before execution: "
                "definition_drift_or_start_validation_failed "
                "(queued_command_sha256=%s)",
                job_id,
                command_sha256,
            )
            return
        if not start_confirmed:
            LOGGER.error(
                "scheduled job %s blocked before execution: "
                "definition_drift: queued command identity was rejected "
                "by the current scheduler definition "
                "(queued_command_sha256=%s)",
                job_id,
                command_sha256,
            )
            # A normal completion record would be attached to the current
            # definition under this jid and could falsely make the hot-reloaded
            # command look executed.  The rejection log is the drift telemetry.
            return
        try:
            result_policy = importlib.import_module("skills.ops.cron_result_policy")
            macro_entrypoint = None
            if command.startswith("@MAGI"):
                prompt = command.removeprefix("@MAGI").strip()
                macro_entrypoint = resolve_exact_cron_macro(prompt)
                if macro_entrypoint is None:
                    orchestrator = self._get_orchestrator()
                    response = orchestrator.process_message(
                        "SYSTEM_CRON", prompt, platform="V3_CRON", role="admin"
                    )
                    if response:
                        try:
                            orchestrator.record_assistant_reply("SYSTEM_CRON", response)
                        except Exception:
                            LOGGER.debug("cron assistant reply persistence failed", exc_info=True)
                    text = (
                        json.dumps(response, ensure_ascii=False, default=str)
                        if isinstance(response, dict)
                        else str(response or "")
                    )
                    classification = result_policy.classify_cron_result(
                        0, text, "", macro_response=True
                    )
                    self._record_with_recovery(
                        scheduler,
                        job,
                        started_at,
                        command_sha256,
                        success=bool(classification.success),
                        returncode=int(classification.returncode or 0),
                        error=str(classification.error or "") if not classification.success else "",
                        stdout=text,
                        status=str(classification.status or ""),
                    )
                    return

            safe_process = importlib.import_module("api.platforms.safe_process")
            runtime_policy = importlib.import_module("skills.ops.cron_runtime_policy")
            argv = (
                macro_entrypoint.argv(self.config.release_root, sys.executable)
                if macro_entrypoint is not None
                else safe_process.parse_cron_command(command)
            )
            runner = self.process_runner or safe_process.run
            env_extra = {"MAGI_PREFER_LOCAL_DB": "1", "MAGI_NO_DELETE": "1"}
            recovery = importlib.import_module("magi_v3.business_recovery")
            business_domain, _contract = recovery.contract_for_job(job_id)
            occurrence_id = _cron_occurrence_id(job, command_sha256)
            env_extra.update(
                {
                    "MAGI_CRON_JOB_ID": job_id,
                    "MAGI_CRON_OCCURRENCE_ID": occurrence_id,
                    "MAGI_CRON_RECOVERY_MODE": "retry" if bool(job.get("_magi_retry")) else "normal",
                    "MAGI_CRON_RETRY_ATTEMPT": str(int(job.get("_magi_retry_attempt") or 0)),
                    "MAGI_IDEMPOTENCY_KEY": f"cron:{occurrence_id}",
                }
            )
            if business_domain:
                env_extra["MAGI_BUSINESS_DOMAIN"] = business_domain
            if self.faiss_coordinator.is_source_job(job_id):
                env_extra.update(
                    self.faiss_coordinator.low_memory_environment(job_id)
                )
            run_kwargs = {
                "timeout_sec": _cron_timeout_seconds(runtime_policy, job),
                "cwd": str(self.config.release_root),
                "env_extra": env_extra,
            }
            if self.process_runner is None:
                run_kwargs["env_whitelist_prefixes"] = CRON_ENV_WHITELIST_PREFIXES
                run_kwargs["_cancel_event"] = self._stop_event
            result = runner(argv, **run_kwargs)
            stdout = str(result.stdout or "")
            stderr = str(result.stderr or "")
            classification = result_policy.classify_cron_result(
                result.returncode,
                stdout,
                stderr,
                timed_out=bool(getattr(result, "timed_out", False)),
            )
            if (
                classification.success
                and self.faiss_coordinator.is_source_job(job_id)
            ):
                # Publish the durable rebuild request only after the source
                # transaction/process has completed successfully.  Marking it
                # before execution lets the rebuild snapshot stale DB state.
                self.faiss_coordinator.mark_required(job_id)
            self._record_with_recovery(
                scheduler,
                job,
                started_at,
                command_sha256,
                success=bool(classification.success),
                returncode=int(result.returncode or 0),
                timed_out=bool(getattr(result, "timed_out", False)),
                error=str(classification.error or "") if not classification.success else "",
                stdout=stdout,
                stderr=stderr,
                status=str(classification.status or ""),
            )
        except Exception as exc:
            if getattr(exc, "safe_process_cancelled", False):
                # mark_job_started already persisted a recoverable occurrence.
                # Leaving it incomplete lets reconcile_incomplete_jobs resume
                # the same checkpoint after the new release owns the scheduler.
                LOGGER.info("scheduled job stopped for controlled handoff: %s", job_id)
                return
            LOGGER.exception("scheduled job failed: %s", job_id)
            try:
                self._record_with_recovery(
                    scheduler,
                    job,
                    started_at,
                    command_sha256,
                    success=False,
                    returncode=1,
                    error=f"{type(exc).__name__}: {exc}",
                    stderr=f"{type(exc).__name__}: {exc}",
                    status="failed",
                )
            except Exception:
                LOGGER.exception("scheduled job result persistence failed: %s", job_id)

    def _execute_claimed(self, scheduler: Scheduler, job: dict[str, Any]) -> None:
        job_id = str(job.get("id") or "").strip()
        if not job_id or scheduler.mark_job_dispatched(job_id) is not True:
            raise CronServiceError(f"failed to claim durable scheduled job: {job_id}")
        self._execute(scheduler, job)

    @staticmethod
    def _prune(
        running: dict[str, Future[Any]], running_lanes: dict[str, str]
    ) -> PendingCronJob | None:
        for job_id, future in tuple(running.items()):
            if future.done():
                running.pop(job_id, None)
                running_lanes.pop(job_id, None)

    @staticmethod
    def _timeout_seconds(job: dict[str, Any]) -> int:
        try:
            value = int(job.get("timeout_sec") or 600)
        except (TypeError, ValueError):
            value = 600
        return max(1, min(86400, value))

    def _enqueue(
        self,
        pending: dict[str, Any],
        job: dict[str, Any],
        *,
        now: float,
        sequence: int,
        running: dict[str, Future[Any]] | None = None,
    ) -> PendingCronJob | None:
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            return None
        if (
            running is not None
            and job_id in running
            and bool(job.get("_magi_retry"))
        ):
            # ``recover_v3_retry_jobs`` deliberately returns receipts in both
            # queued and running states so a restarted scheduler can resume a
            # lost child.  Inside one live owner, however, the in-memory
            # future is authoritative.  Never turn the same running receipt
            # into another pending occurrence on the next poll.
            return None
        raw_due = str(job.get("_magi_due_at") or "").strip()
        try:
            due_at = datetime.fromisoformat(raw_due).timestamp() if raw_due else now
        except ValueError:
            due_at = now
        delay = 0.0 if bool(job.get("_magi_retry")) else self.dispatch_policy.delay_for(job)
        effective = due_at + delay
        item = PendingCronJob(
            job=dict(job),
            lane=self.dispatch_policy.lane_for(job),
            scheduled_at=effective,
            not_before=effective,
            latest_start_at=effective + self._timeout_seconds(job),
            sequence=sequence,
        )
        existing_value = pending.get(job_id)
        existing_items = (
            list(existing_value)
            if isinstance(existing_value, list)
            else ([existing_value] if isinstance(existing_value, PendingCronJob) else [])
        )

        def identity(value: PendingCronJob) -> tuple[str, bool]:
            return (
                str(
                    value.job.get("_magi_occurrence_id")
                    or value.job.get("_magi_due_at")
                    or value.scheduled_at
                ),
                bool(value.job.get("_magi_retry")),
            )

        # ``peek_due_jobs`` is deliberately non-claiming while a phase delay is
        # active.  It can therefore return the same minute more than once;
        # retain one copy of that occurrence instead of creating a duplicate.
        if identity(item) in {identity(value) for value in existing_items}:
            return existing_items[0]

        if self.dispatch_policy.queue_all_non_durable and not self.dispatch_policy.coalesces_pending(job_id):
            max_pending = int(self.dispatch_policy.max_pending_occurrences_per_job)
            if len(existing_items) >= max_pending:
                raise CronServiceError(
                    "loss-sensitive pending occurrence queue bound exceeded: "
                    f"{job_id}"
                )
            items = [*existing_items, item]
            items.sort(key=lambda value: (value.latest_start_at, value.scheduled_at, value.sequence))
            # Preserve the old single-item shape until a second occurrence is
            # actually present; this keeps compatibility adapters simple.
            pending[job_id] = items if len(items) > 1 else items[0]
            return item

        if self.dispatch_policy.queue_all_non_durable and self.dispatch_policy.coalesces_pending(job_id):
            active_items = [
                value
                for value in existing_items
                if str(value.job.get("_magi_pending_status") or "") == "running"
            ]
            recovered_active = str(item.job.get("_magi_pending_status") or "") == "running"
            if active_items or recovered_active:
                active = active_items or [item]
                queued = [
                    value
                    for value in existing_items
                    if str(value.job.get("_magi_pending_status") or "") != "running"
                ]
                if not recovered_active:
                    queued.append(item)
                if queued:
                    pending[job_id] = [
                        *active,
                        max(
                            queued,
                            key=lambda value: (
                                value.scheduled_at,
                                value.sequence,
                            ),
                        ),
                    ]
                else:
                    pending[job_id] = active[0]
                return item

        if existing_items and (
            bool(existing_items[0].job.get("_magi_retry"))
            or bool(item.job.get("_magi_retry"))
        ):
            # A recovery occurrence must not be pushed behind the next regular
            # schedule.  Coalesce to the earliest retry/scheduled execution.
            selected = min(
                (*existing_items, item),
                key=lambda value: (value.scheduled_at, value.sequence),
            )
        elif existing_items and self.dispatch_policy.coalesces_pending(job_id):
            # Explicitly declared snapshot jobs are latest-wins while their
            # work is pending.  This is the only path allowed to replace work.
            selected = item
        else:
            selected = item
        pending[job_id] = selected
        return selected

    @staticmethod
    def _pending_items(pending: dict[str, Any]) -> list[PendingCronJob]:
        items: list[PendingCronJob] = []
        for value in pending.values():
            if isinstance(value, PendingCronJob):
                items.append(value)
            elif isinstance(value, list):
                items.extend(
                    item for item in value if isinstance(item, PendingCronJob)
                )
        return items

    @staticmethod
    def _remove_pending_item(
        pending: dict[str, Any], item: PendingCronJob
    ) -> None:
        job_id = str(item.job.get("id") or "")
        current = pending.get(job_id)
        if isinstance(current, PendingCronJob):
            if current.sequence == item.sequence:
                pending.pop(job_id, None)
            return
        if not isinstance(current, list):
            return
        remaining = [
            value for value in current
            if not isinstance(value, PendingCronJob)
            or value.sequence != item.sequence
        ]
        if not remaining:
            pending.pop(job_id, None)
        elif len(remaining) == 1:
            pending[job_id] = remaining[0]
        else:
            pending[job_id] = remaining

    def _dispatch_ready(
        self,
        scheduler: Scheduler,
        executor: Any,
        pending: dict[str, Any],
        running: dict[str, Future[Any]],
        running_lanes: dict[str, str],
    ) -> None:
        while True:
            self._prune(running, running_lanes)
            # Defence in depth for pending data created before the current
            # owner observed a running future.  A stale duplicate must not run
            # immediately after the genuine occurrence finishes.
            if not self.dispatch_policy.queue_all_non_durable:
                for running_job_id in tuple(running):
                    pending.pop(running_job_id, None)
            now = time.time()
            active_lanes = list(running_lanes.values())
            eligible = [
                item
                for item in self._pending_items(pending)
                if item.not_before <= now
                and str(item.job.get("id") or "") not in running
                and self.dispatch_policy.can_start_lane(item.lane, active_lanes)
            ]
            if not eligible:
                return
            item = min(
                eligible,
                key=lambda value: (
                    value.latest_start_at,
                    value.scheduled_at,
                    value.sequence,
                    str(value.job.get("id") or ""),
                ),
            )
            job_id = str(item.job.get("id") or "")
            future = executor.submit(self._execute_claimed, scheduler, item.job)
            self._remove_pending_item(pending, item)
            running[job_id] = future
            running_lanes[job_id] = item.lane

    def _dispatch_faiss_rebuild(
        self,
        executor: Any,
        running: dict[str, Future[Any]],
        running_lanes: dict[str, str],
    ) -> None:
        self._prune(running, running_lanes)
        if INTERNAL_REBUILD_JOB_ID in running or not self.faiss_coordinator.ready():
            return
        if not self.dispatch_policy.can_start_lane(
            REBUILD_LANE, list(running_lanes.values())
        ):
            return
        safe_process = importlib.import_module("api.platforms.safe_process")
        runner = self.process_runner or safe_process.run
        running[INTERNAL_REBUILD_JOB_ID] = executor.submit(
            self.faiss_coordinator.run_rebuild, runner
        )
        running_lanes[INTERNAL_REBUILD_JOB_ID] = REBUILD_LANE

    def run(self, stop_event: threading.Event) -> None:
        owner_lock = self.owner_lock_factory()
        if not owner_lock.acquired:
            active = owner_lock.active_owner or {}
            raise CronServiceError(
                f"scheduler owner already active: {active.get('owner') or '?'} "
                f"pid={active.get('pid') or '?'}"
            )
        scheduler: Scheduler | None = None
        executor: Any | None = None
        self._stop_event = stop_event
        try:
            scheduler = self.scheduler_factory()
            reconciled = scheduler.reconcile_incomplete_jobs()
            if reconciled:
                LOGGER.warning("reconciled incomplete scheduled jobs: %s", ", ".join(reconciled))
            deferral_reconciler = getattr(
                scheduler, "reconcile_terminal_schedule_deferrals", None
            )
            if callable(deferral_reconciler):
                reconciled_deferrals = list(deferral_reconciler())
                if reconciled_deferrals:
                    LOGGER.info(
                        "reconciled terminal schedule deferrals: %s",
                        ", ".join(reconciled_deferrals),
                    )
            executor = self.executor_factory(self.config.max_workers)
            running: dict[str, Future[Any]] = {}
            running_lanes: dict[str, str] = {}
            pending: dict[str, PendingCronJob] = {}
            sequence = 0
            catchup_pending = True
            notification_future: Future[Any] | None = None
            next_notification_flush = (
                time.monotonic()
                + min(
                    self.config.poll_interval_seconds,
                    self.config.notification_flush_interval_seconds,
                )
            )
            next_resource_recovery_check = 0.0
            while not stop_event.is_set():
                self._prune(running, running_lanes)
                resource_rearm = getattr(
                    scheduler, "rearm_recovered_resource_deferrals", None
                )
                if callable(resource_rearm):
                    resource_check_now = time.monotonic()
                    if resource_check_now >= next_resource_recovery_check:
                        next_resource_recovery_check = resource_check_now + 300.0
                        try:
                            rearmed = list(resource_rearm())
                            if rearmed:
                                LOGGER.info(
                                    "re-armed jobs after resource recovery: %s",
                                    ", ".join(rearmed),
                                )
                        except Exception:
                            # Mount-table observation must never take the
                            # scheduler owner down. The next five-minute poll
                            # retries without modifying the old receipt.
                            LOGGER.exception("resource recovery observation failed")
                if notification_future is not None and notification_future.done():
                    try:
                        result = notification_future.result()
                        if isinstance(result, dict) and int(result.get("recovered") or 0):
                            LOGGER.info("notification outbox recovered: %s", result)
                    except Exception:
                        LOGGER.exception("notification outbox replay failed")
                    notification_future = None
                    next_notification_flush = (
                        time.monotonic()
                        + self.config.notification_flush_interval_seconds
                    )
                due_jobs = list(scheduler.peek_due_jobs())
                retry_loader = getattr(scheduler, "recover_v3_retry_jobs", None)
                if callable(retry_loader):
                    retry_jobs = list(retry_loader())
                    # Keep both when a regular occurrence and a retry share an
                    # id; _enqueue will retain the earliest recovery-safe one.
                    due_jobs = [*retry_jobs, *due_jobs]
                if catchup_pending:
                    catchup_pending = False
                    recovered = scheduler.recover_v3_pending_jobs()
                    missed = scheduler.get_missed_jobs_v3(self.config.catchup_hours)
                    due_ids = {str(job.get("id") or "") for job in due_jobs}
                    for job in [*recovered, *missed]:
                        job_id = str(job.get("id") or "")
                        if job_id and job_id not in due_ids:
                            due_jobs.insert(0, job)
                            due_ids.add(job_id)
                for job in due_jobs:
                    job_id = str(job.get("id") or "").strip()
                    if not job_id:
                        continue
                    sequence += 1
                    item = self._enqueue(
                        pending,
                        job,
                        now=time.time(),
                        sequence=sequence,
                        running=running,
                    )
                    if item is None:
                        continue
                    item_delay = (
                        0.0
                        if bool(item.job.get("_magi_retry"))
                        else self.dispatch_policy.delay_for(item.job)
                    )
                    due_at = datetime.fromtimestamp(item.scheduled_at - item_delay)
                    effective_at = datetime.fromtimestamp(item.scheduled_at)
                    if scheduler.mark_job_v3_pending(
                        job_id,
                        due_at=due_at.isoformat(),
                        effective_at=effective_at.isoformat(),
                        lane=item.lane,
                    ) is not True:
                        raise CronServiceError(
                            f"failed to persist scheduled pending occurrence: {job_id}"
                        )
                self._dispatch_faiss_rebuild(executor, running, running_lanes)
                self._dispatch_ready(
                    scheduler, executor, pending, running, running_lanes
                )
                # A notification retry must not delay a due business job.  It
                # uses one existing bounded worker only while no scheduled job
                # is active, and is never duplicated while a prior replay is
                # still running.
                if (
                    notification_future is None
                    and not running
                    and time.monotonic() >= next_notification_flush
                ):
                    notification_future = executor.submit(self.notification_flusher)
                wait_seconds = self.config.poll_interval_seconds
                future_not_before = [
                    item.not_before
                    for item in pending.values()
                    if item.not_before > time.time()
                ]
                if future_not_before:
                    wait_seconds = min(
                        wait_seconds,
                        max(0.01, min(future_not_before) - time.time()),
                    )
                if stop_event.wait(wait_seconds):
                    break
        finally:
            if executor is not None:
                # SafeProcess observes this same stop_event and reaps only its
                # verified child tree.  Wait for that bounded cleanup before
                # releasing scheduler ownership so the next release cannot run
                # concurrently with an orphan from the previous one.
                executor.shutdown(wait=True, cancel_futures=True)
            self._stop_event = None
            owner_lock.release()


def run_cron_component(stop_event: threading.Event, release_root: Path) -> None:
    # V3's sealed three-lane dispatch policy is the authoritative concurrency
    # binding.  ``MAGI_CRON_MAX_CONCURRENT_JOBS`` remains available to legacy
    # schedulers in the shared dotenv, but must not override the hash-bound V3
    # policy or make the required background service enter a restart loop.
    dispatch_policy = load_cron_dispatch_policy(release_root)
    config = CronServiceConfig(
        release_root=release_root,
        poll_interval_seconds=float(os.environ.get("MAGI_CRON_POLL_SECONDS", "60") or "60"),
        max_workers=dispatch_policy.max_workers,
        catchup_hours=int(os.environ.get("MAGI_CRON_CATCHUP_HOURS", "8") or "8"),
        catchup_min_hour=int(os.environ.get("MAGI_CRON_CATCHUP_MIN_HOUR", "6") or "6"),
        notification_flush_interval_seconds=float(
            os.environ.get("MAGI_NOTIFY_OUTBOX_FLUSH_INTERVAL_SEC", "300") or "300"
        ),
    )
    CronService(config, dispatch_policy=dispatch_policy).run(stop_event)

"""Owned process-group worker supervisor with no background daemon thread."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .errors import SupervisorError, WorkerAlreadyRunning
from .process_compat import group_exists as _portable_group_exists
from .process_compat import process_group as _process_group
from .process_compat import signal_group as _signal_group
from .resource import (
    AdmissionRequest,
    GlobalResourceGovernor,
    ResourceLease,
    ResourceSnapshot,
)

_SAFE_ENV_KEYS = ("HOME", "LANG", "LC_ALL", "PATH", "TMPDIR")


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    job_id: str
    worker_class: str
    argv: tuple[str, ...]
    estimated_footprint_mb: float
    estimated_metal_mb: float = 0.0
    cpu_percent: int = 0
    disk_io: str = "none"
    nas_io: str = "none"
    network: str = "none"
    browser_tokens: int = 0
    interactive: bool = False
    priority_class: str = "P3"
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    inherit_environment: bool = False
    timeout_sec: float = 600.0
    attempt_number: int = 0
    lease_token: str | None = None

    def validate(self) -> None:
        if not self.job_id.strip():
            raise ValueError("job_id is required")
        if not self.argv or not self.argv[0]:
            raise ValueError("worker argv cannot be empty")
        if (
            isinstance(self.timeout_sec, bool)
            or not isinstance(self.timeout_sec, (int, float))
            or not math.isfinite(self.timeout_sec)
            or self.timeout_sec <= 0
        ):
            raise ValueError("timeout_sec must be finite and positive")
        if (
            isinstance(self.estimated_footprint_mb, bool)
            or not isinstance(self.estimated_footprint_mb, (int, float))
            or not math.isfinite(self.estimated_footprint_mb)
            or self.estimated_footprint_mb < 0
        ):
            raise ValueError("estimated_footprint_mb must be finite and non-negative")
        if (
            isinstance(self.estimated_metal_mb, bool)
            or not isinstance(self.estimated_metal_mb, (int, float))
            or not math.isfinite(self.estimated_metal_mb)
            or self.estimated_metal_mb < 0
        ):
            raise ValueError("estimated_metal_mb must be finite and non-negative")
        if type(self.cpu_percent) is not int or not 0 <= self.cpu_percent <= 1000:
            raise ValueError("cpu_percent must be an integer in [0, 1000]")
        if self.disk_io not in {"none", "light", "heavy"}:
            raise ValueError("disk_io has an unsupported class")
        if self.nas_io not in {"none", "light", "heavy"}:
            raise ValueError("nas_io has an unsupported class")
        if self.network not in {"none", "light", "heavy"}:
            raise ValueError("network has an unsupported class")
        if type(self.browser_tokens) is not int or not 0 <= self.browser_tokens <= 1:
            raise ValueError("browser_tokens must be an integer in [0, 1]")
        if self.attempt_number < 0:
            raise ValueError("attempt_number cannot be negative")
        if self.cwd is not None and not self.cwd.is_dir():
            raise ValueError(f"worker cwd is not a directory: {self.cwd}")
        if any("\x00" in part for part in self.argv):
            raise ValueError("worker argv contains a NUL byte")


@dataclass(frozen=True, slots=True)
class WorkerResult:
    job_id: str
    pid: int
    returncode: int
    duration_sec: float
    timed_out: bool
    killed: bool
    attempt_number: int
    lease_token: str | None
    process_group_gone: bool


@dataclass(slots=True)
class _WorkerHandle:
    spec: WorkerSpec
    process: subprocess.Popen[bytes]
    resource_lease: ResourceLease
    started_monotonic: float
    deadline_monotonic: float
    process_group: int
    killed: bool = False
    timed_out: bool = False


class WorkerSupervisor:
    """Starts only explicitly requested workers and tracks only owned PIDs."""

    def __init__(self, governor: GlobalResourceGovernor) -> None:
        self.governor = governor
        self._lock = threading.RLock()
        self._workers: dict[str, _WorkerHandle] = {}

    def start(self, spec: WorkerSpec, snapshot: ResourceSnapshot) -> int:
        """Admit and start one worker in a new POSIX session/process group."""

        spec.validate()
        request = AdmissionRequest(
            worker_class=spec.worker_class,
            estimated_footprint_mb=spec.estimated_footprint_mb,
            estimated_metal_mb=spec.estimated_metal_mb,
            cpu_percent=spec.cpu_percent,
            disk_io=spec.disk_io,
            nas_io=spec.nas_io,
            network=spec.network,
            browser_tokens=spec.browser_tokens,
            interactive=spec.interactive,
            priority_class=spec.priority_class,
            job_id=spec.job_id,
        )
        with self._lock:
            existing = self._workers.get(spec.job_id)
            if existing is not None and existing.process.poll() is None:
                raise WorkerAlreadyRunning(spec.job_id)
            if existing is not None:
                self._finalize_locked(spec.job_id, existing)
            lease = self.governor.acquire(request, snapshot)
            try:
                process = subprocess.Popen(
                    list(spec.argv),
                    cwd=str(spec.cwd) if spec.cwd is not None else None,
                    env=self._worker_env(spec),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    shell=False,
                    close_fds=True,
                    start_new_session=True,
                )
            except BaseException:
                lease.release()
                raise
            started = time.monotonic()
            self._workers[spec.job_id] = _WorkerHandle(
                spec=spec,
                process=process,
                resource_lease=lease,
                started_monotonic=started,
                deadline_monotonic=started + spec.timeout_sec,
                process_group=process.pid,
            )
            return process.pid

    def poll(self, job_id: str) -> WorkerResult | None:
        """Return a result only after exit; enforce the configured deadline."""

        with self._lock:
            handle = self._workers.get(job_id)
            if handle is None:
                return None
            returncode = handle.process.poll()
            if returncode is not None:
                return self._finalize_locked(job_id, handle)
            if time.monotonic() >= handle.deadline_monotonic:
                handle.timed_out = True
        if handle.timed_out:
            return self.terminate(job_id, grace_sec=2.0)
        return None

    def wait(self, job_id: str, *, timeout: float | None = None) -> WorkerResult:
        with self._lock:
            handle = self._workers.get(job_id)
            if handle is None:
                raise SupervisorError(f"unknown worker: {job_id}")
            remaining = max(0.0, handle.deadline_monotonic - time.monotonic())
            wait_for = remaining if timeout is None else min(remaining, max(0.0, timeout))
        try:
            handle.process.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            with self._lock:
                handle.timed_out = time.monotonic() >= handle.deadline_monotonic
            if handle.timed_out:
                return self.terminate(job_id, grace_sec=2.0)
            raise
        with self._lock:
            return self._finalize_locked(job_id, handle)

    def terminate(self, job_id: str, *, grace_sec: float = 5.0) -> WorkerResult:
        """SIGTERM then SIGKILL only the owned process group, and always reap."""

        with self._lock:
            handle = self._workers.get(job_id)
            if handle is None:
                raise SupervisorError(f"unknown worker: {job_id}")
        if handle.process.poll() is None:
            self._signal_owned_group(handle, signal.SIGTERM)
            try:
                handle.process.wait(timeout=max(0.0, grace_sec))
            except subprocess.TimeoutExpired:
                handle.killed = True
                self._signal_owned_group(handle, signal.SIGKILL)
                try:
                    handle.process.wait(timeout=5.0)
                except subprocess.TimeoutExpired as exc:
                    raise SupervisorError(
                        f"owned worker group could not be reaped: {job_id}"
                    ) from exc
        with self._lock:
            return self._finalize_locked(job_id, handle)

    def reap_finished(self) -> list[WorkerResult]:
        results: list[WorkerResult] = []
        with self._lock:
            finished = [
                (job_id, handle)
                for job_id, handle in self._workers.items()
                if handle.process.poll() is not None
            ]
            for job_id, handle in finished:
                results.append(self._finalize_locked(job_id, handle))
        return results

    def shutdown(self, *, grace_sec: float = 5.0) -> list[WorkerResult]:
        with self._lock:
            job_ids = list(self._workers)
        results: list[WorkerResult] = []
        for job_id in job_ids:
            with self._lock:
                handle = self._workers.get(job_id)
            if handle is None:
                continue
            if handle.process.poll() is None:
                results.append(self.terminate(job_id, grace_sec=grace_sec))
            else:
                with self._lock:
                    results.append(self._finalize_locked(job_id, handle))
        return results

    def active_job_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(
                job_id
                for job_id, handle in self._workers.items()
                if handle.process.poll() is None
            )

    def owns_worker(self, job_id: str, *, attempt_number: int, lease_token: str) -> bool:
        with self._lock:
            handle = self._workers.get(job_id)
            return bool(
                handle is not None
                and handle.spec.attempt_number == attempt_number
                and handle.spec.lease_token == lease_token
            )

    @staticmethod
    def _signal_owned_group(handle: _WorkerHandle, signum: signal.Signals) -> None:
        try:
            current_group = _process_group(handle.process.pid)
        except ProcessLookupError:
            current_group = handle.process_group
        if current_group != handle.process_group:
            raise SupervisorError(
                f"refusing to signal unexpected process group {current_group} for pid {handle.process.pid}"
            )
        try:
            _signal_group(handle.process_group, signum)
        except ProcessLookupError:
            return

    @staticmethod
    def _group_exists(process_group: int) -> bool:
        return _portable_group_exists(process_group)

    def _drain_owned_group(self, handle: _WorkerHandle, *, grace_sec: float) -> None:
        if not self._group_exists(handle.process_group):
            return
        handle.killed = True
        self._signal_owned_group(handle, signal.SIGTERM)
        deadline = time.monotonic() + grace_sec
        while self._group_exists(handle.process_group) and time.monotonic() < deadline:
            time.sleep(0.01)
        if self._group_exists(handle.process_group):
            self._signal_owned_group(handle, signal.SIGKILL)
            deadline = time.monotonic() + 5.0
            while self._group_exists(handle.process_group) and time.monotonic() < deadline:
                time.sleep(0.01)
        if self._group_exists(handle.process_group):
            raise SupervisorError(
                f"owned worker process group could not be drained: {handle.process_group}"
            )

    @staticmethod
    def _worker_env(spec: WorkerSpec) -> dict[str, str]:
        if spec.inherit_environment:
            env = dict(os.environ)
        else:
            env = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
        env.update({str(key): str(value) for key, value in spec.env.items()})
        return env

    def _finalize_locked(self, job_id: str, handle: _WorkerHandle) -> WorkerResult:
        returncode = handle.process.poll()
        if returncode is None:
            raise SupervisorError(f"cannot finalize running worker: {job_id}")
        if self._group_exists(handle.process_group):
            self._drain_owned_group(handle, grace_sec=0.2)
        if self._workers.get(job_id) is handle:
            self._workers.pop(job_id, None)
        handle.resource_lease.release()
        return WorkerResult(
            job_id=job_id,
            pid=handle.process.pid,
            returncode=int(returncode),
            duration_sec=max(0.0, time.monotonic() - handle.started_monotonic),
            timed_out=handle.timed_out,
            killed=handle.killed,
            attempt_number=handle.spec.attempt_number,
            lease_token=handle.spec.lease_token,
            process_group_gone=not self._group_exists(handle.process_group),
        )

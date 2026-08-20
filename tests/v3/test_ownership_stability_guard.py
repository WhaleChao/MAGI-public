from __future__ import annotations

from pathlib import Path

import pytest

from magi_v3.service_runtime import (
    PeriodicOwnershipGuard,
    ServiceIdentity,
    ServiceRuntimeError,
    is_transient_ownership_probe_failure,
)
from magi_v3.supervisor_service import SupervisorService


class SequenceProbe:
    def __init__(self, outcomes: list[BaseException | None]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def assert_exclusive(self, _release_root: Path) -> None:
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            raise outcome


def test_periodic_guard_defers_one_listener_timeout_and_resets_on_success() -> None:
    clock = [100.0]
    probe = SequenceProbe(
        [
            ServiceRuntimeError(
                "listener ownership probe timed out for 5002 after 3 attempts"
            ),
            None,
        ]
    )
    guard = PeriodicOwnershipGuard(
        probe,
        Path("/release"),
        monotonic=lambda: clock[0],
    )

    clock[0] = 109.0
    assert guard.check() is False
    assert guard.consecutive_transient_failures == 1
    clock[0] = 114.0
    assert guard.check() is True
    assert guard.consecutive_transient_failures == 0
    assert guard.last_success == 114.0


def test_periodic_guard_never_defers_confirmed_foreign_owner() -> None:
    error = ServiceRuntimeError(
        "production port 5002 has a foreign or unclassified listener pid=42"
    )
    probe = SequenceProbe([error])
    guard = PeriodicOwnershipGuard(probe, Path("/release"))

    with pytest.raises(ServiceRuntimeError, match="foreign or unclassified"):
        guard.check()


def test_periodic_guard_fails_closed_after_consecutive_timeout_limit() -> None:
    timeout = ServiceRuntimeError(
        "process ownership probe timed out after 3 attempts"
    )
    probe = SequenceProbe([timeout, timeout, timeout])
    guard = PeriodicOwnershipGuard(
        probe,
        Path("/release"),
        max_consecutive_transient_failures=2,
    )

    assert guard.check() is False
    assert guard.check() is False
    with pytest.raises(ServiceRuntimeError, match="timed out"):
        guard.check()


def test_periodic_guard_fails_closed_after_grace_window() -> None:
    clock = [10.0]
    timeout = ServiceRuntimeError(
        "listener ownership probe timed out for 5003 after 3 attempts"
    )
    probe = SequenceProbe([timeout])
    guard = PeriodicOwnershipGuard(
        probe,
        Path("/release"),
        monotonic=lambda: clock[0],
        transient_grace_sec=30.0,
    )

    clock[0] = 41.0
    with pytest.raises(ServiceRuntimeError, match="timed out"):
        guard.check()


def test_transient_classifier_is_narrow() -> None:
    assert is_transient_ownership_probe_failure(
        ServiceRuntimeError("process ownership probe timed out after 3 attempts")
    )
    assert not is_transient_ownership_probe_failure(
        ServiceRuntimeError("active release marker is unavailable")
    )
    assert not is_transient_ownership_probe_failure(TimeoutError("timed out"))


def test_supervisor_keeps_children_running_after_one_periodic_probe_timeout(
    tmp_path: Path,
) -> None:
    class Processes:
        started = False
        stopped = False
        ticks = 0

        def start_all(self) -> None:
            self.started = True

        def shutdown(self) -> None:
            self.stopped = True

        def tick(self) -> None:
            self.ticks += 1

    class Lease:
        acquired = False
        released = False

        def acquire(self) -> None:
            self.acquired = True

        def release(self) -> None:
            self.released = True

    class TwoStepStop:
        calls = 0

        def wait(self, _timeout: float) -> bool:
            self.calls += 1
            return self.calls > 1

        def set(self) -> None:
            return

    release = tmp_path / "release"
    release.mkdir()
    manifest = release / "release-manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    identity = ServiceIdentity(
        role="supervisor",
        release_id="v3-test",
        release_root=release,
        release_manifest=manifest,
        release_manifest_sha256="0" * 64,
        runtime_root=tmp_path / "runtime",
        pid_file=tmp_path / "runtime" / "supervisor.pid",
    )
    timeout = ServiceRuntimeError(
        "listener ownership probe timed out for 5002 after 3 attempts"
    )
    probe = SequenceProbe([None, timeout])
    processes = Processes()
    lease = Lease()
    service = SupervisorService(
        identity=identity,
        role_lease=lease,  # type: ignore[arg-type]
        ownership_probe=probe,
        process_supervisor=processes,  # type: ignore[arg-type]
        control_owner=lambda: True,
        poll_interval_sec=0,
        ownership_probe_interval_sec=0,
        monotonic=lambda: 10.0,
    )
    service.stop_event = TwoStepStop()  # type: ignore[assignment]

    assert service.run() == 0
    assert probe.calls == 2
    assert processes.ticks == 1
    assert processes.started and processes.stopped
    assert lease.acquired and lease.released

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from magi_v3.errors import WorkerAlreadyRunning
from magi_v3.resource import GlobalResourceGovernor, ResourceSnapshot
from magi_v3.supervisor import WorkerSpec, WorkerSupervisor


def worker_spec(job_id: str, code: str, *, timeout: float = 5.0) -> WorkerSpec:
    return WorkerSpec(
        job_id=job_id,
        worker_class="light",
        argv=(sys.executable, "-c", code),
        estimated_footprint_mb=32,
        timeout_sec=timeout,
    )


def test_worker_runs_in_owned_process_group_and_releases_slot() -> None:
    governor = GlobalResourceGovernor()
    supervisor = WorkerSupervisor(governor)
    pid = supervisor.start(worker_spec("fast", "pass"), ResourceSnapshot())
    assert os.getpgid(pid) == pid
    result = supervisor.wait("fast")
    assert result.returncode == 0
    assert result.timed_out is False
    assert governor.active_counts()["total"] == 0


def test_duplicate_job_is_rejected_and_group_is_terminated() -> None:
    governor = GlobalResourceGovernor()
    supervisor = WorkerSupervisor(governor)
    spec = worker_spec("long", "import time; time.sleep(60)")
    pid = supervisor.start(spec, ResourceSnapshot())
    with pytest.raises(WorkerAlreadyRunning):
        supervisor.start(spec, ResourceSnapshot())

    result = supervisor.terminate("long", grace_sec=0.1)
    assert result.pid == pid
    assert result.returncode != 0
    assert supervisor.active_job_ids() == ()
    assert governor.active_counts()["total"] == 0


def test_deadline_terminates_and_reaps_worker() -> None:
    governor = GlobalResourceGovernor()
    supervisor = WorkerSupervisor(governor)
    supervisor.start(
        worker_spec("timeout", "import time; time.sleep(60)", timeout=0.1),
        ResourceSnapshot(),
    )
    result = supervisor.wait("timeout")
    assert result.timed_out is True
    assert result.returncode != 0
    assert governor.active_counts()["total"] == 0


def test_default_worker_environment_does_not_copy_arbitrary_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MAGI_TEST_SECRET", "must-not-leak")
    result_path = tmp_path / "env-result"
    code = (
        "import os,pathlib;"
        f"pathlib.Path({str(result_path)!r}).write_text(os.getenv('MAGI_TEST_SECRET','missing'))"
    )
    supervisor = WorkerSupervisor(GlobalResourceGovernor())
    supervisor.start(worker_spec("env", code), ResourceSnapshot())
    result = supervisor.wait("env")
    assert result.returncode == 0
    assert result_path.read_text() == "missing"


def test_leader_exit_does_not_leave_unaccounted_descendant(tmp_path: Path) -> None:
    child_pid_path = tmp_path / "child.pid"
    code = (
        "import pathlib,subprocess,sys;"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        f"pathlib.Path({str(child_pid_path)!r}).write_text(str(child.pid))"
    )
    governor = GlobalResourceGovernor()
    supervisor = WorkerSupervisor(governor)
    supervisor.start(worker_spec("descendant", code), ResourceSnapshot())

    result = supervisor.wait("descendant")

    child_pid = int(child_pid_path.read_text())
    assert result.returncode == 0
    assert result.killed is True
    assert governor.active_counts()["total"] == 0
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)

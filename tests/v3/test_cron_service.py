from __future__ import annotations

import os
import hashlib
import subprocess
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace

import pytest

import magi_v3.cron_service as cron_service_module
import magi_v3.faiss_maintenance as faiss_maintenance_module
from magi_v3.cron_policy import CronDispatchPolicy
from magi_v3.cron_service import (
    CRON_ENV_WHITELIST_PREFIXES,
    CRON_TIMEOUT_FLOORS_SECONDS,
    CronService,
    CronServiceConfig,
    CronServiceError,
    _load_bound_cron_environment,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _bind_source_tree_python(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scheduler unit tests do not depend on a source-tree bundled venv."""

    original = faiss_maintenance_module._python_command

    def resolve(root: Path) -> Path:
        if root.expanduser().resolve() == ROOT.resolve():
            return Path(sys.executable).resolve()
        return original(root)

    monkeypatch.setattr(faiss_maintenance_module, "_python_command", resolve)


def test_run_component_uses_hash_bound_worker_count_not_legacy_env(
    monkeypatch,
) -> None:
    stop = threading.Event()
    policy = _policy()
    captured: dict[str, object] = {}

    class StubService:
        def __init__(self, config, *, dispatch_policy):
            captured["config"] = config
            captured["policy"] = dispatch_policy

        def run(self, received_stop):
            captured["stop"] = received_stop

    monkeypatch.setenv("MAGI_CRON_MAX_CONCURRENT_JOBS", "2")
    monkeypatch.setattr(
        cron_service_module,
        "load_cron_dispatch_policy",
        lambda release_root: policy,
    )
    monkeypatch.setattr(cron_service_module, "CronService", StubService)

    cron_service_module.run_cron_component(stop, ROOT)

    assert captured["config"].max_workers == policy.max_workers == 4
    assert captured["policy"] is policy
    assert captured["stop"] is stop


def test_bound_cron_environment_loads_hash_verified_file(
    tmp_path: Path, monkeypatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MAGI_CRON_ENV_PROBE=loaded\n", encoding="utf-8")
    monkeypatch.setenv("MAGI_ENV_FILE", str(env_file))
    monkeypatch.setenv(
        "MAGI_ENV_FILE_SHA256", hashlib.sha256(env_file.read_bytes()).hexdigest()
    )
    monkeypatch.delenv("MAGI_CRON_ENV_PROBE", raising=False)

    _load_bound_cron_environment()

    assert os.environ["MAGI_CRON_ENV_PROBE"] == "loaded"


def test_bound_cron_environment_rejects_hash_drift(
    tmp_path: Path, monkeypatch
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("MAGI_CRON_ENV_PROBE=loaded\n", encoding="utf-8")
    monkeypatch.setenv("MAGI_ENV_FILE", str(env_file))
    monkeypatch.setenv("MAGI_ENV_FILE_SHA256", "0" * 64)
    with pytest.raises(CronServiceError, match="SHA-256 mismatch"):
        _load_bound_cron_environment()


def test_import_is_side_effect_free() -> None:
    code = """
import subprocess, threading
subprocess.Popen = lambda *a, **k: (_ for _ in ()).throw(AssertionError('process'))
threading.Thread.start = lambda *a, **k: (_ for _ in ()).throw(AssertionError('thread'))
import magi_v3.cron_service
assert 'skills.ops.cron_scheduler' not in __import__('sys').modules
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


class Lock:
    acquired = True
    path = Path("/tmp/test-cron-owner")
    active_owner = None

    def __init__(self) -> None:
        self.released = False

    def release(self) -> None:
        self.released = True


class ImmediateExecutor:
    def __init__(self) -> None:
        self.shutdown_calls: list[tuple[bool, bool]] = []

    def submit(self, function, *args):
        future: Future[object] = Future()
        try:
            future.set_result(function(*args))
        except Exception as exc:  # pragma: no cover - production catches job errors
            future.set_exception(exc)
        return future

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        self.shutdown_calls.append((wait, cancel_futures))


class ControlledExecutor:
    def __init__(self) -> None:
        self.submissions = []
        self.futures = {}

    def submit(self, function, scheduler, job):
        future: Future[object] = Future()
        job_id = job["id"]
        self.submissions.append(job_id)
        self.futures[job_id] = future
        return future

    def shutdown(self, *, wait, cancel_futures):
        return None


class RaisingExecutor:
    def submit(self, function, *args):
        raise RuntimeError("deterministic submit failure")

    def shutdown(self, *, wait, cancel_futures):
        return None


class Scheduler:
    def __init__(self, stop: threading.Event) -> None:
        self.stop = stop
        self.started: list[str] = []
        self.results: list[tuple[str, dict]] = []
        self.marked: list[str] = []
        self.queued: list[str] = []

    def reconcile_incomplete_jobs(self):
        return []

    def reconcile_terminal_schedule_deferrals(self):
        return []

    def peek_due_jobs(self):
        self.stop.set()
        return [{"id": "due", "command": "python3 scripts/ops/example.py"}]

    def get_missed_jobs_v3(self, hours):
        assert hours == 8
        return [{"id": "missed", "command": "python3 scripts/ops/missed.py"}]

    def recover_v3_pending_jobs(self):
        return []

    def mark_job_v3_pending(self, job_id, **kwargs):
        self.queued.append(job_id)
        return True

    def mark_job_dispatched(self, job_id):
        self.marked.append(job_id)
        return True

    def mark_job_started(self, job_id, **kwargs):
        assert len(kwargs["command_sha256"]) == 64
        self.started.append(job_id)
        return True

    def mark_job_result(self, job_id, **kwargs):
        self.results.append((job_id, kwargs))
        return True


def test_dedicated_scheduler_owns_lock_catches_up_and_records_jobs() -> None:
    stop = threading.Event()
    scheduler = Scheduler(stop)
    lock = Lock()
    executor = ImmediateExecutor()
    calls: list[tuple[list[str], dict]] = []

    def run_process(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="completed", stderr="", timed_out=False)

    service = CronService(
        CronServiceConfig(ROOT, poll_interval_seconds=0.01),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=lambda: lock,
        process_runner=run_process,
        executor_factory=lambda workers: executor,
        # This test exercises scheduler ownership/catch-up semantics.  Keep it
        # independent of a mutable repository-root cron_jobs.json so it also
        # runs from the immutable release bundle, where the real schedule is a
        # separately hash-bound deployment input.
        dispatch_policy=_policy(),
    )

    service.run(stop)

    assert scheduler.marked == ["missed", "due"]
    assert scheduler.started == ["missed", "due"]
    assert [job_id for job_id, _ in scheduler.results] == ["missed", "due"]
    assert all(payload["success"] is True for _, payload in scheduler.results)
    assert all(call[1]["cwd"] == str(ROOT) for call in calls)
    assert lock.released is True
    assert executor.shutdown_calls == [(True, True)]


def test_running_retry_is_not_enqueued_or_dispatched_twice() -> None:
    stop = threading.Event()
    scheduler = Scheduler(stop)
    executor = ControlledExecutor()
    service = CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        executor_factory=lambda _workers: executor,
        dispatch_policy=_policy(),
    )
    job = {
        "id": "job_business",
        "command": "python3 scripts/ops/example.py",
        "_magi_retry": True,
    }
    active: Future[object] = Future()
    running = {"job_business": active}
    running_lanes = {"job_business": "business"}
    pending = {}

    assert service._enqueue(
        pending,
        job,
        now=2_000_000_000.0,
        sequence=1,
        running=running,
    ) is None
    assert pending == {}

    # Also reject a stale duplicate which predates the current running map.
    service._enqueue(pending, job, now=2_000_000_000.0, sequence=2)
    assert "job_business" in pending
    service._dispatch_ready(
        scheduler, executor, pending, running, running_lanes
    )
    assert pending == {}
    assert executor.submissions == []


def test_production_cron_runner_preserves_explicit_provider_environment(
    monkeypatch,
) -> None:
    from api.platforms import safe_process

    stop = threading.Event()
    scheduler = Scheduler(stop)
    calls: list[dict[str, object]] = []

    def run_process(_argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout='{"ok":true,"success":true}',
            stderr="",
            timed_out=False,
        )

    monkeypatch.setattr(safe_process, "run", run_process)
    CronService(
        CronServiceConfig(ROOT, poll_interval_seconds=0.01),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        executor_factory=lambda workers: ImmediateExecutor(),
        dispatch_policy=_policy(),
    ).run(stop)

    assert calls
    assert all(
        call["env_whitelist_prefixes"] == CRON_ENV_WHITELIST_PREFIXES
        for call in calls
    )
    assert all(call["_cancel_event"] is stop for call in calls)
    assert {"OSC_", "NVIDIA_", "GOOGLE_", "MAGI_", "OPENCLAW_", "DB_", "MYSQL_", "APPLE_"}.issubset(
        set(CRON_ENV_WHITELIST_PREFIXES)
    )
    assert all(len(call["env_extra"]["MAGI_CRON_OCCURRENCE_ID"]) == 64 for call in calls)
    assert all(call["env_extra"]["MAGI_IDEMPOTENCY_KEY"].startswith("cron:") for call in calls)


def test_business_owner_receives_domain_and_stable_idempotency_environment() -> None:
    scheduler = Scheduler(threading.Event())
    calls: list[dict[str, object]] = []

    def run_process(_argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout='{"ok":true,"success":true}',
            stderr="",
            timed_out=False,
        )

    CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        process_runner=run_process,
        dispatch_policy=_policy(),
    )._execute(
        scheduler,
        {
            "id": "job_file_review_check",
            "command": "python3 scripts/ops/example.py",
            "_magi_due_at": "2033-05-18T03:33:20",
        },
    )

    env = calls[0]["env_extra"]
    assert env["MAGI_BUSINESS_DOMAIN"] == "file_review_payment"
    assert env["MAGI_CRON_RECOVERY_MODE"] == "normal"
    assert env["MAGI_CRON_RETRY_ATTEMPT"] == "0"
    assert env["MAGI_IDEMPOTENCY_KEY"] == f"cron:{env['MAGI_CRON_OCCURRENCE_ID']}"


def test_idle_scheduler_replays_notification_outbox_once(monkeypatch) -> None:
    stop = threading.Event()
    scheduler = Scheduler(stop)
    scheduler.peek_due_jobs = lambda: (stop.set() or [])  # type: ignore[method-assign]
    scheduler.get_missed_jobs_v3 = lambda hours: []  # type: ignore[method-assign]
    monotonic = iter([100.0, 101.0])
    monkeypatch.setattr(cron_service_module.time, "monotonic", lambda: next(monotonic))
    calls: list[str] = []

    CronService(
        CronServiceConfig(
            ROOT,
            poll_interval_seconds=0.01,
            notification_flush_interval_seconds=30,
        ),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        executor_factory=lambda workers: ImmediateExecutor(),
        dispatch_policy=_policy(),
        notification_flusher=lambda: (
            calls.append("flush") or {"checked": 0, "recovered": 0, "remaining": 0}
        ),
    ).run(stop)

    assert calls == ["flush"]


def test_autopilot_timeout_floor_prevents_generic_schedule_sigterm() -> None:
    scheduler = Scheduler(threading.Event())
    calls: list[dict[str, object]] = []

    def run_process(_argv, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout='{"ok":true,"success":true}',
            stderr="",
            timed_out=False,
        )

    service = CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        process_runner=run_process,
        dispatch_policy=_policy(),
    )
    service._execute(
        scheduler,
        {
            "id": "job_1770948489644_c5a469",
            "command": "@MAGI 自動巡檢",
            "timeout_sec": 600,
        },
    )

    assert CRON_TIMEOUT_FLOORS_SECONDS["job_1770948489644_c5a469"] == 1800
    assert calls[0]["timeout_sec"] == 1800


def test_structured_apple_unavailable_is_deferred_not_failed() -> None:
    scheduler = Scheduler(threading.Event())

    def run_process(_argv, **_kwargs):
        return SimpleNamespace(
            returncode=75,
            stdout=(
                '{"success":false,"ok":false,"status":"deferred",'
                '"deferred":true,"error":"apple_unavailable: sidecar_not_responding"}'
            ),
            stderr="",
            timed_out=False,
        )

    service = CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        process_runner=run_process,
        dispatch_policy=_policy(),
    )
    service._execute(
        scheduler,
        {"id": "job_translator_ape_regression", "command": "python3 benchmark.py"},
    )

    result = scheduler.results[0][1]
    assert result["success"] is False
    assert result["status"] == "deferred"
    assert result["error"] == "apple_unavailable: sidecar_not_responding"


def _policy(*, delays=None, batches=()):
    return CronDispatchPolicy(
        lane_caps={"light": 2, "batch": 1, "maintenance": 1},
        shared_caps={"heavy": (frozenset({"batch", "maintenance"}), 1)},
        batch_job_ids=frozenset(batches),
        phase_delay_seconds=dict(delays or {}),
        policy_sha256="a" * 64,
        cron_jobs_sha256="b" * 64,
    )


def test_phase_delayed_job_is_unclaimed_until_submit_and_recovers_once_after_restart(
    monkeypatch,
) -> None:
    from magi_v3 import cron_service as cron_module

    base = 2_000_000_000.0
    due_at = __import__("datetime").datetime.fromtimestamp(base).isoformat()
    job = {
        "id": "delayed",
        "command": "python3 scripts/ops/example.py",
        "timeout_sec": 300,
        "_magi_due_at": due_at,
    }
    calls = []

    class RecoveryScheduler(Scheduler):
        def __init__(self, stop, *, due, missed):
            super().__init__(stop)
            self.due = due
            self.missed = missed

        def peek_due_jobs(self):
            self.stop.set()
            return list(self.due)

        def get_missed_jobs_v3(self, hours):
            assert hours == 8
            return list(self.missed)

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="", timed_out=False)

    monkeypatch.setattr(cron_module.time, "time", lambda: base)
    first_stop = threading.Event()
    first = RecoveryScheduler(first_stop, due=[job], missed=[])
    CronService(
        CronServiceConfig(ROOT, poll_interval_seconds=0.01),
        scheduler_factory=lambda: first,
        owner_lock_factory=Lock,
        process_runner=runner,
        executor_factory=lambda workers: ImmediateExecutor(),
        dispatch_policy=_policy(delays={"delayed": 60}),
    ).run(first_stop)
    assert first.marked == []
    assert calls == []

    monkeypatch.setattr(cron_module.time, "time", lambda: base + 61)
    second_stop = threading.Event()
    second = RecoveryScheduler(second_stop, due=[], missed=[job])
    CronService(
        CronServiceConfig(ROOT, poll_interval_seconds=0.01),
        scheduler_factory=lambda: second,
        owner_lock_factory=Lock,
        process_runner=runner,
        executor_factory=lambda workers: ImmediateExecutor(),
        dispatch_policy=_policy(delays={"delayed": 60}),
    ).run(second_stop)

    assert second.marked == ["delayed"]
    assert second.started == ["delayed"]
    assert len(calls) == 1


def test_submit_failure_keeps_durable_occurrence_unclaimed_for_restart(monkeypatch) -> None:
    from magi_v3 import cron_service as cron_module

    now = 2_000_000_000.0
    monkeypatch.setattr(cron_module.time, "time", lambda: now)
    job = {"id": "submit-fails", "command": "python3 scripts/ops/example.py"}
    first_stop = threading.Event()
    first = Scheduler(first_stop)
    first.peek_due_jobs = lambda: (first_stop.set() or [job])  # type: ignore[method-assign]
    first.get_missed_jobs_v3 = lambda hours: []  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="submit failure"):
        CronService(
            CronServiceConfig(ROOT, poll_interval_seconds=0.01),
            scheduler_factory=lambda: first,
            owner_lock_factory=Lock,
            executor_factory=lambda workers: RaisingExecutor(),
            dispatch_policy=_policy(),
        ).run(first_stop)
    assert first.queued == ["submit-fails"]
    assert first.marked == []
    assert first.started == []

    calls = []
    second_stop = threading.Event()
    second = Scheduler(second_stop)
    recovered = dict(job, _magi_due_at=__import__("datetime").datetime.fromtimestamp(now).isoformat())
    second.peek_due_jobs = lambda: (second_stop.set() or [])  # type: ignore[method-assign]
    second.recover_v3_pending_jobs = lambda: [recovered]  # type: ignore[method-assign]
    second.get_missed_jobs_v3 = lambda hours: []  # type: ignore[method-assign]

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="", timed_out=False)

    CronService(
        CronServiceConfig(ROOT, poll_interval_seconds=0.01),
        scheduler_factory=lambda: second,
        owner_lock_factory=Lock,
        process_runner=runner,
        executor_factory=lambda workers: ImmediateExecutor(),
        dispatch_policy=_policy(),
    ).run(second_stop)
    assert second.marked == ["submit-fails"]
    assert second.started == ["submit-fails"]
    assert len(calls) == 1


def test_three_lane_edf_shares_one_heavy_slot_without_starvation(monkeypatch) -> None:
    from magi_v3 import cron_service as cron_module

    now = 2_000_000_000.0
    monkeypatch.setattr(cron_module.time, "time", lambda: now)
    scheduler = Scheduler(threading.Event())
    executor = ControlledExecutor()
    service = CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        executor_factory=lambda workers: executor,
        dispatch_policy=_policy(batches={"batch"}),
    )
    pending = {}
    running = {}
    running_lanes = {}
    jobs = [
        {"id": "batch", "command": "noop", "timeout_sec": 100},
        {"id": "maintenance", "command": "noop", "timeout_sec": 200, "resource_guarded": True},
        {"id": "light-a", "command": "noop", "timeout_sec": 300},
        {"id": "light-b", "command": "noop", "timeout_sec": 400},
    ]
    for sequence, job in enumerate(jobs, 1):
        service._enqueue(pending, job, now=now, sequence=sequence)

    service._dispatch_ready(scheduler, executor, pending, running, running_lanes)

    assert executor.submissions == ["batch", "light-a", "light-b"]
    assert running_lanes == {"batch": "batch", "light-a": "light", "light-b": "light"}
    assert set(pending) == {"maintenance"}
    executor.futures["batch"].set_result(None)
    service._dispatch_ready(scheduler, executor, pending, running, running_lanes)
    assert executor.submissions[-1] == "maintenance"
    assert not pending


def test_same_job_pending_coalesces_to_latest_occurrence(monkeypatch) -> None:
    from magi_v3 import cron_service as cron_module

    monkeypatch.setattr(cron_module.time, "time", lambda: 2_000_000_000.0)
    service = CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: Scheduler(threading.Event()),
        owner_lock_factory=Lock,
        dispatch_policy=_policy(),
    )
    pending = {}
    earlier = {
        "id": "same",
        "command": "noop",
        "_magi_due_at": "2033-05-18T03:33:20",
    }
    later = dict(earlier, _magi_due_at="2033-05-18T03:43:20")
    service._enqueue(pending, earlier, now=2_000_000_000.0, sequence=1)
    first = pending["same"]
    service._enqueue(pending, later, now=2_000_000_600.0, sequence=2)

    assert len(pending) == 1
    assert pending["same"].scheduled_at > first.scheduled_at
    assert pending["same"].sequence == 2


def test_runtime_and_capacity_replay_share_lane_admission_decision(monkeypatch) -> None:
    from magi_v3 import cron_service as cron_module
    from scripts.v3_validation.schedule_capacity_certification import (
        Occurrence,
        simulate_layered_capacity,
    )

    class CountingPolicy:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.calls = 0

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def can_start_lane(self, lane, active_lanes):
            self.calls += 1
            return self.wrapped.can_start_lane(lane, active_lanes)

    spy = CountingPolicy(_policy(batches={"batch"}))
    simulate_layered_capacity(
        [
            Occurrence("light", 0, "light", 600, 1),
            Occurrence("batch", 0, "batch", 600, 1),
            Occurrence("maintenance", 0, "maintenance", 600, 1),
        ],
        delivery_multiplier=1,
        slots=spy.lane_caps,
        dispatch_policy=spy,
    )
    replay_calls = spy.calls
    assert replay_calls > 0

    now = 2_000_000_000.0
    monkeypatch.setattr(cron_module.time, "time", lambda: now)
    scheduler = Scheduler(threading.Event())
    service = CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        executor_factory=lambda workers: ControlledExecutor(),
        dispatch_policy=spy,
    )
    pending = {}
    service._enqueue(
        pending,
        {"id": "light-runtime", "command": "noop"},
        now=now,
        sequence=1,
    )
    service._dispatch_ready(scheduler, ControlledExecutor(), pending, {}, {})
    assert spy.calls > replay_calls


def test_macro_orchestrator_is_lazy_and_result_is_persisted() -> None:
    stop = threading.Event()
    scheduler = Scheduler(stop)
    scheduler.peek_due_jobs = lambda: []  # type: ignore[method-assign]
    replies: list[tuple[str, object]] = []

    class Orchestrator:
        def process_message(self, *args, **kwargs):
            return {"ok": True, "result": "done"}

        def record_assistant_reply(self, user, response):
            replies.append((user, response))

    service = CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        orchestrator_factory=Orchestrator,
        dispatch_policy=_policy(),
    )
    service._execute(scheduler, {"id": "macro", "command": "@MAGI run health check"})

    assert scheduler.started == ["macro"]
    assert scheduler.results[0][1]["success"] is True
    assert replies and replies[0][0] == "SYSTEM_CRON"


@pytest.mark.parametrize(
    ("prompt", "entrypoint", "tail"),
    [
        ("系統狀態", "scripts/ops/system_diagnostic_report.py", ()),
        ("自動巡檢", "scripts/ops/system_diagnostic_report.py", ()),
    ],
)
def test_reviewed_exact_macro_uses_deterministic_real_entrypoint(
    prompt: str, entrypoint: str, tail: tuple[str, ...]
) -> None:
    scheduler = Scheduler(threading.Event())
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def run_process(argv, **kwargs):
        calls.append((tuple(argv), kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout='{"ok":true,"success":true}',
            stderr="",
            timed_out=False,
        )

    def forbidden_orchestrator():
        raise AssertionError("reviewed macro must not construct the orchestrator")

    service = CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        process_runner=run_process,
        orchestrator_factory=forbidden_orchestrator,
        dispatch_policy=_policy(),
    )
    service._execute(scheduler, {"id": "macro", "command": f"@MAGI {prompt}"})

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == (sys.executable, str(ROOT / entrypoint), *tail)
    assert kwargs["cwd"] == str(ROOT)
    assert scheduler.results[0][1]["success"] is True


def test_v3_cron_persists_resource_guard_deferral_as_non_red_status() -> None:
    stop = threading.Event()
    scheduler = Scheduler(stop)

    def run_process(_argv, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout="MAGI resource guard skipped job_pdf_repair: resource_level>=throttle:critical",
            stderr="",
            timed_out=False,
        )

    service = CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        process_runner=run_process,
        dispatch_policy=_policy(),
    )
    service._execute(
        scheduler,
        {"id": "guarded", "command": "python3 scripts/ops/resource_guarded_run.py"},
    )

    result = scheduler.results[0][1]
    assert result["success"] is False
    assert result["status"] == "deferred"
    assert result["error"] == "resource_guard_skipped"
    assert result["terminal_deferred"] is True


@pytest.mark.parametrize(
    "reason",
    ["large_files_waiting_for_offpeak_window", "regex_budget_exhausted"],
)
def test_expected_batch_deferral_clears_old_retry_without_retrying(reason: str) -> None:
    class NoRetryScheduler(Scheduler):
        def schedule_job_v3_retry(self, *_args, **_kwargs):
            raise AssertionError("expected schedule deferral must not be retried")

    scheduler = NoRetryScheduler(threading.Event())

    def deferred_process(_argv, **_kwargs):
        return SimpleNamespace(
            returncode=75,
            stdout=(
                '{"ok":false,"success":false,"status":"deferred",'
                f'"deferred":true,"partial":false,"reason":"{reason}"'
                ',"failed":0,"errors":0}'
            ),
            stderr="",
            timed_out=False,
        )

    CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        process_runner=deferred_process,
        dispatch_policy=_policy(),
    )._execute(
        scheduler,
        {"id": "job_business", "command": "python3 scripts/ops/example.py"},
    )

    result = scheduler.results[0][1]
    assert result["status"] == "deferred"
    assert result["success"] is False
    assert result["returncode"] == 75
    assert result["timed_out"] is False
    assert result["error"] == reason
    assert result["terminal_deferred"] is True


def test_production_scheduler_converts_transient_failure_to_durable_retry() -> None:
    stop = threading.Event()

    class RetryScheduler(Scheduler):
        def __init__(self, stop):
            super().__init__(stop)
            self.retries = []

        def schedule_job_v3_retry(self, job_id, **kwargs):
            self.retries.append((job_id, kwargs))
            return {
                "scheduled": True,
                "exhausted": False,
                "attempt": 1,
                "max_attempts": 3,
                "retry_at": "2033-05-18T03:34:20",
            }

    scheduler = RetryScheduler(stop)

    def run_process(_argv, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="connection reset by upstream",
            timed_out=False,
        )

    CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        process_runner=run_process,
        dispatch_policy=_policy(),
    )._execute(
        scheduler,
        {"id": "job_business", "command": "python3 scripts/ops/example.py"},
    )

    assert len(scheduler.retries) == 1
    result = scheduler.results[0][1]
    assert result["status"] == "deferred"
    assert result["success"] is False
    assert "MAGI 已自動接手" in result["error"]
    assert "connection reset" not in result["error"]


def test_production_scheduler_does_not_retry_explicit_human_requirement() -> None:
    # Exhausting bounded retries for a structured wait must keep it deferred;
    # the next ordinary schedule will continue it without a false red light.
    class ExhaustedScheduler(Scheduler):
        def schedule_job_v3_retry(self, job_id, **kwargs):
            return {"scheduled": False, "exhausted": True, "attempt": 3, "max_attempts": 3}

    exhausted_scheduler = ExhaustedScheduler(threading.Event())

    def deferred_process(_argv, **_kwargs):
        return SimpleNamespace(
            returncode=75,
            stdout=(
                '{"success":false,"status":"deferred","deferred":true,'
                '"partial":false,"reason":"storage_unavailable"}'
            ),
            stderr="",
            timed_out=False,
        )

    CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: exhausted_scheduler,
        owner_lock_factory=Lock,
        process_runner=deferred_process,
        dispatch_policy=_policy(),
    )._execute(
        exhausted_scheduler,
        {"id": "job_business", "command": "python3 scripts/ops/example.py"},
    )
    deferred_result = exhausted_scheduler.results[0][1]
    assert deferred_result["status"] == "deferred"
    assert deferred_result["success"] is False
    assert deferred_result["returncode"] == 75
    assert deferred_result["timed_out"] is False

    stop = threading.Event()

    class HumanScheduler(Scheduler):
        def schedule_job_v3_retry(self, *_args, **_kwargs):
            raise AssertionError("human-required work must not be blindly retried")

    scheduler = HumanScheduler(stop)

    def run_process(_argv, **_kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout='{"success":false,"action_required":true,"reason":"missing required document"}',
            stderr="",
            timed_out=False,
        )

    CronService(
        CronServiceConfig(ROOT),
        scheduler_factory=lambda: scheduler,
        owner_lock_factory=Lock,
        process_runner=run_process,
        dispatch_policy=_policy(),
    )._execute(
        scheduler,
        {"id": "job_business", "command": "python3 scripts/ops/example.py"},
    )

    result = scheduler.results[0][1]
    assert result["status"] == "failed"
    assert "需要人類提供資料、登入或作決定" in result["error"]
    assert "missing required document" not in result["error"]

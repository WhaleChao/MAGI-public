#!/usr/bin/env python3
"""Produce release-bound partial evidence for offline resource/performance gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from magi_v3.macos_resources import MacOSResourceSampler
from magi_v3.dispatcher import DurableDispatcher, VerifiedCompletion
from magi_v3.ledger import JobLedger, JobSpec
from magi_v3.resource import GlobalResourceGovernor, ResourceSnapshot
from magi_v3.state import JobStatus
from magi_v3.supervisor import WorkerSpec, WorkerSupervisor
from scripts.v3_validation.perf_compat import run_benchmark
from scripts.v3_validation.perf_certification import run_performance_certification
from scripts.v3_validation.isolated_resource_window import verify_report as verify_isolated_window
from scripts.v3_validation.g8_isolated_smb import (
    G8SMBBlocked,
    extract_bound_matched_performance_report,
)
from scripts.v3_validation.resource_performance_evidence import (
    EXPECTED_GAPS,
    ResourcePerformanceEvidenceError,
    SCHEMA,
    WORKLOAD,
    derive_metrics,
    sha256_json,
    summarize_report,
    verify_g8_transport_composition_receipt,
)


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE_PREFIX = "MAGI_V3_OFFLINE_EVIDENCE="
SEATBELT_CHILD_ENV = "MAGI_V3_RESOURCE_PERF_SEATBELT_CHILD"


class ResourcePerformanceCertificationError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ResourcePerformanceCertificationError(f"JSON object required: {path}")
    return value


def _release_files() -> tuple[dict[str, Any], dict[str, str], str]:
    manifest_path = Path(os.environ.get("MAGI_V3_RELEASE_MANIFEST", "")).resolve(
        strict=True
    )
    manifest = _load_object(manifest_path)
    observed = _sha256(manifest_path)
    if observed != os.environ.get("MAGI_V3_RELEASE_MANIFEST_SHA256"):
        raise ResourcePerformanceCertificationError("release manifest SHA-256 mismatch")
    rows = manifest.get("files")
    if not isinstance(rows, list):
        raise ResourcePerformanceCertificationError("release file inventory is missing")
    files = {
        str(row.get("path")): str(row.get("sha256"))
        for row in rows
        if isinstance(row, dict)
    }
    return manifest, files, observed


def _quoted(path: Path) -> str:
    return json.dumps(str(path.resolve()))


def _isolated_roots(workspace: Path) -> tuple[Path, ...]:
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    live_root = (real_home / "Library/Application Support/MAGI").resolve()
    home = Path(os.environ.get("HOME", "")).resolve(strict=True)
    temporary = Path(os.environ.get("TMPDIR", "")).resolve(strict=True)
    resolved = workspace.resolve(strict=True)
    if home == real_home or home == live_root or home.is_relative_to(live_root):
        raise ResourcePerformanceCertificationError("producer HOME overlaps live MAGI")
    if not resolved.is_relative_to(temporary):
        raise ResourcePerformanceCertificationError("producer workspace is outside TMPDIR")
    return tuple(dict.fromkeys((resolved, temporary, home)))


def _seatbelt_profile(workspace: Path) -> str:
    if sys.platform != "darwin" or shutil.which("sandbox-exec") != "/usr/bin/sandbox-exec":
        raise ResourcePerformanceCertificationError("macOS Seatbelt is unavailable")
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    protected = (
        real_home / "Library/Application Support/MAGI",
        real_home / "Library/CloudStorage",
        real_home / "Library/Keychains",
        real_home / ".ssh",
        Path("/Volumes"),
        Path("/opt/homebrew/var/mysql"),
    )
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        '(allow file-write* (literal "/dev/null"))',
        *(
            f"(allow file-write* (subpath {_quoted(path)}))"
            for path in _isolated_roots(workspace)
        ),
        *(f"(deny file-read* (subpath {_quoted(path)}))" for path in protected),
    ]
    return "".join(rules)


def _runtime_binding() -> tuple[str, Path]:
    expected = os.environ.get("MAGI_V3_PYTHON_RUNTIME_SHA256", "")
    runtime = Path(sys.executable)
    realpath = runtime.resolve(strict=True)
    expected_realpath = Path(
        os.environ.get("MAGI_V3_PYTHON_RUNTIME_REALPATH", "")
    ).resolve(strict=True)
    if (
        len(expected) != 64
        or _sha256(runtime) != expected
        or _sha256(realpath) != expected
        or realpath != expected_realpath
    ):
        raise ResourcePerformanceCertificationError(
            "producer is not running in the hash-bound release Python"
        )
    return expected, realpath


def _isolated_window_report(
    manifest: Mapping[str, Any], manifest_sha: str, release_files: Mapping[str, str]
) -> dict[str, Any] | None:
    raw_path = os.environ.get("MAGI_V3_ISOLATED_RESOURCE_WINDOW_REPORT", "")
    expected_sha = os.environ.get("MAGI_V3_ISOLATED_RESOURCE_WINDOW_REPORT_SHA256", "")
    if not raw_path and not expected_sha:
        return None
    if not raw_path or len(expected_sha) != 64:
        raise ResourcePerformanceCertificationError(
            "isolated resource window path and SHA-256 must be supplied together"
        )
    path = Path(raw_path).resolve(strict=True)
    live_root = (Path(pwd.getpwuid(os.getuid()).pw_dir) / "Library/Application Support/MAGI").resolve()
    if path == live_root or path.is_relative_to(live_root) or _sha256(path) != expected_sha:
        raise ResourcePerformanceCertificationError(
            "isolated resource window report is live-state or hash-mismatched"
        )
    report = _load_object(path)
    metrics = verify_isolated_window(
        report,
        expected_release_id=str(manifest.get("release_id") or ""),
        expected_release_manifest_sha256=manifest_sha,
    )
    binding = report.get("release_binding", {})
    if (
        binding.get("release_snapshot_sha256")
        != manifest.get("source_snapshot_sha256")
        or binding.get("resource_policy_sha256")
        != release_files.get("config/v3_resource_policy.json")
        or report.get("execution_binding", {}).get("collector_source_sha256")
        != release_files.get(
            "scripts/v3_validation/isolated_resource_window_collector.py"
        )
        or metrics.get("g8", {}).get("model_tokens_per_second_measured") is not True
        or metrics.get("g9", {}).get("all_budgets_passed") is not True
        or metrics.get("g25", {}).get("metal_returned_to_baseline") is not True
    ):
        raise ResourcePerformanceCertificationError(
            "isolated resource window is not bound to this release/policy"
        )
    return report


def _g8_smb_report(
    manifest: Mapping[str, Any],
    manifest_sha: str,
) -> dict[str, Any] | None:
    """Load, but never create, an authorized isolated remote-SMB artifact."""

    raw_path = os.environ.get("MAGI_V3_G8_SMB_REPORT", "")
    expected_sha = os.environ.get("MAGI_V3_G8_SMB_REPORT_SHA256", "")
    if not raw_path and not expected_sha:
        return None
    if not raw_path or len(expected_sha) != 64:
        raise ResourcePerformanceCertificationError(
            "G8 SMB report path and SHA-256 must be supplied together"
        )
    raw = Path(raw_path).expanduser()
    try:
        path = raw.resolve(strict=True)
        metadata = raw.lstat()
    except OSError as exc:
        raise ResourcePerformanceCertificationError(
            f"G8 SMB report is unavailable: {exc}"
        ) from exc
    live_root = (
        Path(pwd.getpwuid(os.getuid()).pw_dir) / "Library/Application Support/MAGI"
    ).resolve()
    if (
        not raw.is_absolute()
        or path != raw
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or path == ROOT
        or path.is_relative_to(ROOT)
        or path == live_root
        or path.is_relative_to(live_root)
        or _sha256(path) != expected_sha
    ):
        raise ResourcePerformanceCertificationError(
            "G8 SMB report is live-state or hash-mismatched"
        )
    report = _load_object(path)
    plan = report.get("plan")
    matched_sha = (
        plan.get("matched_performance_report", {}).get("evidence_sha256")
        if isinstance(plan, dict)
        else None
    )
    if not isinstance(matched_sha, str):
        raise ResourcePerformanceCertificationError(
            "G8 SMB report omitted its matched-performance SHA binding"
        )
    try:
        verify_g8_transport_composition_receipt(
            report,
            matched_performance_sha256=matched_sha,
            release_binding={
                "release_id": manifest.get("release_id"),
                "release_manifest_sha256": manifest_sha,
            },
        )
    except ResourcePerformanceEvidenceError as exc:
        raise ResourcePerformanceCertificationError(
            f"G8 SMB report failed verification: {exc}"
        ) from exc
    return report


def _resource_probe(observation_seconds: float = 0.25) -> dict[str, Any]:
    before = MacOSResourceSampler(footprint_pids=(os.getpid(),)).sample()
    started = time.monotonic()
    time.sleep(observation_seconds)
    elapsed = time.monotonic() - started
    after = MacOSResourceSampler(footprint_pids=(os.getpid(),)).sample()
    before_swap = before.vm_stat.swapouts_total_mb if before.vm_stat else None
    after_swap = after.vm_stat.swapouts_total_mb if after.vm_stat else None
    if before_swap is None or after_swap is None:
        raise ResourcePerformanceCertificationError("swapout counters are unavailable")
    return {
        "owned_probe_pid": os.getpid(),
        "owned_probe_physical_footprint_mb": after.magi_physical_footprint_mb,
        "physical_footprint_source": "/usr/bin/footprint --noCategories -f bytes",
        "swapout_before_mb": before_swap,
        "swapout_after_mb": after_swap,
        "swapout_growth_mb": max(0.0, after_swap - before_swap),
        "observation_seconds": elapsed,
        "required_idle_observation_seconds": 1800,
        "complete_budget_profiles_measured": False,
        "model_loaded": False,
        "production_state_accessed": False,
        "sample_errors": [*before.errors, *after.errors],
    }


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ResourcePerformanceCertificationError("preemption sample is empty")
    return ordered[min(len(ordered) - 1, max(0, (95 * len(ordered) + 99) // 100 - 1))]


def _pid_gone(pid: int, timeout_sec: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.005)
    return False


def _preemption_probe(workspace: Path, sample_count: int = 4) -> dict[str, Any]:
    if sample_count < 4:
        raise ResourcePerformanceCertificationError(
            "preemption certification requires at least four samples"
        )
    governor = GlobalResourceGovernor()
    supervisor = WorkerSupervisor(governor)
    ledger = JobLedger(workspace / "preemption-ledger.sqlite3")
    ledger.initialize()
    marker_root = workspace / "preemption-markers"
    marker_root.mkdir()
    samples: list[dict[str, Any]] = []

    def factory(job, lease):
        cycle = int(job.input["cycle"])
        if job.worker_class == "integration" and lease.attempt_number == 1:
            ready = marker_root / f"heavy-{cycle}.ready"
            descendant = marker_root / f"heavy-{cycle}.descendant"
            code = (
                "import pathlib,signal,subprocess,sys,time;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                "child=subprocess.Popen([sys.executable,'-I','-S','-c',"
                "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)']);"
                f"pathlib.Path({str(descendant)!r}).write_text(str(child.pid));"
                f"pathlib.Path({str(ready)!r}).write_text('ready');"
                "time.sleep(30)"
            )
        elif job.worker_class == "integration":
            completed = marker_root / f"heavy-{cycle}.completed"
            code = f"import pathlib;pathlib.Path({str(completed)!r}).write_text('once')"
        else:
            started = marker_root / f"interactive-{cycle}.started"
            code = f"import pathlib;pathlib.Path({str(started)!r}).write_text('started')"
        return WorkerSpec(
            job_id=job.job_id,
            worker_class=job.worker_class,
            argv=(sys.executable, "-I", "-S", "-c", code),
            cwd=workspace,
            estimated_footprint_mb=16,
            cpu_percent=10,
            priority_class=job.priority_class,
            timeout_sec=5,
        )

    def verified(job, _lease, _result):
        return VerifiedCompletion(
            target=JobStatus.SUCCEEDED,
            business_completed=True,
            result={"cycle": job.input["cycle"]},
            artifacts=(
                {"kind": "fixture_receipt", "uri": f"fixture://{job.job_id}"},
            ),
        )

    dispatcher = DurableDispatcher(
        ledger=ledger,
        supervisor=supervisor,
        worker_factory=factory,
        completion_verifier=verified,
        snapshot_provider=ResourceSnapshot,
        owner_id="resource-performance-certifier",
        lease_seconds=5,
        preemption_grace_sec=0.05,
        capability_worker_classes={
            "fixture_heavy": "integration",
            "fixture_interactive": "light",
        },
    )
    base = datetime.now(timezone.utc)
    try:
        for cycle in range(1, sample_count + 1):
            priority = "P0" if cycle % 2 else "P1"
            heavy_id = f"cert-heavy-{cycle}"
            interactive_id = f"cert-interactive-{cycle}"
            ledger.create_job(
                JobSpec(
                    job_id=heavy_id,
                    capability="fixture_heavy",
                    operation="owned-heavy",
                    worker_class="integration",
                    side_effect_class="read_only",
                    priority_class="P3",
                    input={"cycle": cycle},
                    scheduled_for=base,
                    max_attempts=1,
                    timeout_sec=5,
                    resource_claim={
                        "memory_mb": 16,
                        "metal_mb": 0,
                        "cpu_percent": 10,
                        "disk_io": "none",
                        "nas_io": "none",
                        "network": "none",
                        "browser_tokens": 0,
                    },
                ),
                now=base,
            )
            heavy = dispatcher.dispatch_next(now=base)
            if heavy is None or heavy.lease.job.job_id != heavy_id:
                raise ResourcePerformanceCertificationError("heavy fixture did not start")
            ready = marker_root / f"heavy-{cycle}.ready"
            deadline = time.monotonic() + 1.0
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            descendant_path = marker_root / f"heavy-{cycle}.descendant"
            if not ready.exists() or not descendant_path.exists():
                raise ResourcePerformanceCertificationError("heavy fixture was not ready")
            descendant_pid = int(descendant_path.read_text())
            arrival = base + timedelta(milliseconds=cycle)
            # This is the real latest-start budget encoded on the P0/P1 job
            # below, not a relaxed benchmark-only allowance.
            queue_budget_ms = 1000.0
            ledger.create_job(
                JobSpec(
                    job_id=interactive_id,
                    capability="fixture_interactive",
                    operation="browser-ingress-light",
                    worker_class="light",
                    side_effect_class="read_only",
                    priority_class=priority,
                    input={"cycle": cycle, "queue_class": "interactive_browser"},
                    scheduled_for=arrival,
                    latest_start_at=arrival + timedelta(seconds=1),
                    deadline_at=arrival + timedelta(seconds=6),
                    queue_ttl_sec=1,
                    timeout_sec=5,
                    resource_claim={
                        "memory_mb": 16,
                        "metal_mb": 0,
                        "cpu_percent": 10,
                        "disk_io": "none",
                        "nas_io": "none",
                        "network": "none",
                        "browser_tokens": 0,
                    },
                ),
                now=base,
            )
            started = time.monotonic()
            interactive = dispatcher.dispatch_next(
                now=arrival,
                interactive=True,
            )
            queue_ms = (time.monotonic() - started) * 1000
            if interactive is None or interactive.lease.job.job_id != interactive_id:
                raise ResourcePerformanceCertificationError(
                    "interactive fixture did not start"
                )
            if len(interactive.preemptions) != 1:
                raise ResourcePerformanceCertificationError(
                    "automatic dispatcher preemption was not observed exactly once"
                )
            preemption = interactive.preemptions[0]
            interactive_result = supervisor.wait(interactive_id)
            dispatcher.commit_result(interactive_id, interactive_result, now=arrival)
            retry = dispatcher.dispatch_next(now=arrival + timedelta(milliseconds=1))
            if retry is None or retry.lease.job.job_id != heavy_id:
                raise ResourcePerformanceCertificationError("preempted heavy was not requeued")
            retry_result = supervisor.wait(heavy_id)
            completion = dispatcher.commit_result(
                heavy_id,
                retry_result,
                now=arrival + timedelta(milliseconds=2),
            )
            with sqlite3.connect(ledger.path) as conn:
                attempts = conn.execute(
                    "SELECT attempt_number,status FROM attempts WHERE job_id=? "
                    "ORDER BY attempt_number",
                    (heavy_id,),
                ).fetchall()
                leases = conn.execute("SELECT COUNT(*) FROM leases").fetchone()[0]
            leader_gone = _pid_gone(heavy.worker_pid)
            descendant_gone = _pid_gone(descendant_pid)
            retry_marker = marker_root / f"heavy-{cycle}.completed"
            sample = {
                "cycle": cycle,
                "incoming_priority_class": priority,
                "queue_class": "interactive_browser",
                "heavy_job_id": heavy_id,
                "interactive_job_id": interactive_id,
                "heavy_worker_pid": heavy.worker_pid,
                "heavy_descendant_pid": descendant_pid,
                "automatic_preemption_count": len(interactive.preemptions),
                "preemption_source": "dispatch_handle.preemptions",
                "manual_terminate_invoked": False,
                "worker_killed_after_bounded_grace": preemption.worker_result.killed,
                "process_group_gone": preemption.worker_result.process_group_gone,
                "leader_pid_gone": leader_gone,
                "descendant_pid_gone": descendant_gone,
                "heavy_requeued": preemption.job.status is JobStatus.QUEUED,
                "attempts": [[int(number), str(status)] for number, status in attempts],
                "retry_attempt_number": retry.lease.attempt_number,
                "retry_completed_once": (
                    completion.job.status is JobStatus.SUCCEEDED
                    and retry_marker.is_file()
                    and retry_marker.read_text() == "once"
                ),
                "active_leases_after_completion": int(leases),
                "interactive_queue_ms": queue_ms,
                "deadline_budget_ms": queue_budget_ms,
                "deadline_missed": queue_ms > queue_budget_ms,
                "orphan_process_groups": int(not (leader_gone and descendant_gone)),
                "duplicate_completions": max(
                    0, sum(status == "succeeded" for _number, status in attempts) - 1
                ),
                "lost_jobs": int(completion.job.status is not JobStatus.SUCCEEDED),
            }
            samples.append(sample)
        queue_values = [float(row["interactive_queue_ms"]) for row in samples]
        browser_values = [
            float(row["interactive_queue_ms"]) / 1000
            for row in samples
            if row["incoming_priority_class"] == "P1"
        ]
        return {
            "probe_version": "durable_dispatcher_automatic_preemption_v1",
            "candidate_launcher": str(Path(sys.executable).resolve(strict=True)),
            "seatbelt_child": os.environ.get(SEATBELT_CHILD_ENV) == "1",
            "sample_count": len(samples),
            "samples": samples,
            "automatic_preemption_observed": all(
                row["automatic_preemption_count"] == 1 for row in samples
            ),
            "manual_owned_cleanup_performed": False,
            "p0_p1_deadline_misses": sum(
                bool(row["deadline_missed"]) for row in samples
            ),
            "interactive_queue_p95_ms": _p95(queue_values),
            "p1_browser_queue_p95_seconds": _p95(browser_values),
            "orphan_process_groups": sum(
                int(row["orphan_process_groups"]) for row in samples
            ),
            "duplicate_completions": sum(
                int(row["duplicate_completions"]) for row in samples
            ),
            "lost_jobs": sum(int(row["lost_jobs"]) for row in samples),
            "preempted_jobs_requeued": sum(
                bool(row["heavy_requeued"]) for row in samples
            ),
            "attempt_two_unique_completions": sum(
                bool(row["retry_completed_once"]) for row in samples
            ),
            "production_state_accessed": False,
        }
    finally:
        # This is failure cleanup only. Successful evidence above requires the
        # automatic dispatch path to have left no active worker.
        supervisor.shutdown(grace_sec=0.1)


def _worker_footprint_probe(
    workspace: Path,
    *,
    sample_count: int = 3,
    return_budget_seconds: float = 30.0,
) -> dict[str, Any]:
    """Measure owned worker-group RSS/physical-footprint return without Metal claims."""

    if sample_count < 3:
        raise ResourcePerformanceCertificationError(
            "worker footprint certification requires at least three samples"
        )
    if return_budget_seconds != 30.0:
        raise ResourcePerformanceCertificationError(
            "worker footprint certification must use the release 30-second budget"
        )
    marker_root = workspace / "worker-footprint-markers"
    marker_root.mkdir()
    supervisor = WorkerSupervisor(GlobalResourceGovernor())
    samples: list[dict[str, Any]] = []
    try:
        for cycle in range(1, sample_count + 1):
            leader_ready = marker_root / f"leader-{cycle}.ready"
            descendant_ready = marker_root / f"descendant-{cycle}.ready"
            rusage_prelude = """import ctypes,json,os,pathlib,time
class RUsageInfoV4(ctypes.Structure):
    _fields_=[('uuid',ctypes.c_uint8*16)]+[(name,ctypes.c_uint64) for name in ('user','system','idle','interrupt','pageins','wired','resident','phys','start','exit','child_user','child_system','child_idle','child_interrupt','child_pageins','child_elapsed','disk_read','disk_write','qos_default','qos_maintenance','qos_background','qos_utility','qos_legacy','qos_user_init','qos_user_inter','billed','serviced','logical','lifetime','instructions','cycles','billed_energy','serviced_energy','interval','runnable')]
def self_rusage():
    value=RUsageInfoV4()
    library=ctypes.CDLL('/usr/lib/libproc.dylib',use_errno=True)
    function=library.proc_pid_rusage
    function.argtypes=[ctypes.c_int,ctypes.c_int,ctypes.c_void_p]
    function.restype=ctypes.c_int
    if function(os.getpid(),4,ctypes.byref(value)) != 0:
        raise OSError(ctypes.get_errno(),'proc_pid_rusage failed')
    return {'pid':os.getpid(),'resident_bytes':value.resident,'physical_footprint_bytes':value.phys,'proc_start_abstime':value.start}
"""
            descendant_code = rusage_prelude + """
payload=bytearray(16*1024*1024)
payload[::4096]=b'x'*len(payload[::4096])
pathlib.Path(os.environ['DESCENDANT_READY']).write_text(json.dumps(self_rusage(),sort_keys=True))
time.sleep(3)
"""
            leader_code = rusage_prelude + (
                "import subprocess,sys\n"
                "payload=bytearray(16*1024*1024)\n"
                "payload[::4096]=b'x'*len(payload[::4096])\n"
                f"child=subprocess.Popen([sys.executable,'-I','-S','-c',{descendant_code!r}])\n"
                "deadline=time.monotonic()+2\n"
                "ready=pathlib.Path(os.environ['DESCENDANT_READY'])\n"
                "while not ready.exists() and time.monotonic()<deadline: time.sleep(0.005)\n"
                "if not ready.exists(): child.kill(); child.wait(); raise SystemExit(2)\n"
                "pathlib.Path(os.environ['LEADER_READY']).write_text(json.dumps(self_rusage(),sort_keys=True))\n"
                "raise SystemExit(child.wait())\n"
            )
            job_id = f"cert-worker-footprint-{cycle}"
            leader_pid = supervisor.start(
                WorkerSpec(
                    job_id=job_id,
                    worker_class="integration",
                    argv=(sys.executable, "-I", "-S", "-c", leader_code),
                    cwd=workspace,
                    env={
                        "LEADER_READY": str(leader_ready),
                        "DESCENDANT_READY": str(descendant_ready),
                    },
                    estimated_footprint_mb=64,
                    cpu_percent=10,
                    priority_class="P4",
                    timeout_sec=10,
                ),
                ResourceSnapshot(),
            )
            ready_deadline = time.monotonic() + 2.5
            while (
                not leader_ready.exists() or not descendant_ready.exists()
            ) and time.monotonic() < ready_deadline:
                time.sleep(0.005)
            if not leader_ready.exists() or not descendant_ready.exists():
                raise ResourcePerformanceCertificationError(
                    "worker footprint fixture did not become ready"
                )
            leader_observed = _load_object(leader_ready)
            descendant_observed = _load_object(descendant_ready)
            descendant_pid = int(descendant_observed.get("pid", 0))
            if (
                leader_observed.get("pid") != leader_pid
                or descendant_pid <= 0
                or descendant_pid == leader_pid
                or any(
                    type(row.get(field)) is not int or int(row[field]) <= 0
                    for row in (leader_observed, descendant_observed)
                    for field in (
                        "resident_bytes",
                        "physical_footprint_bytes",
                        "proc_start_abstime",
                    )
                )
            ):
                raise ResourcePerformanceCertificationError(
                    "owned worker self-rusage measurement is invalid"
                )
            group_rss_mb = sum(
                int(row["resident_bytes"])
                for row in (leader_observed, descendant_observed)
            ) / (1024 * 1024)
            physical_footprint_mb = sum(
                int(row["physical_footprint_bytes"])
                for row in (leader_observed, descendant_observed)
            ) / (1024 * 1024)
            return_started = time.monotonic()
            result = supervisor.wait(job_id)
            leader_gone = _pid_gone(leader_pid, timeout_sec=return_budget_seconds)
            descendant_gone = _pid_gone(
                descendant_pid, timeout_sec=return_budget_seconds
            )
            return_seconds = time.monotonic() - return_started
            samples.append(
                {
                    "cycle": cycle,
                    "job_id": job_id,
                    "leader_pid": leader_pid,
                    "descendant_pid": descendant_pid,
                    "observed_group_rss_mb": group_rss_mb,
                    "observed_group_physical_footprint_mb": physical_footprint_mb,
                    "rss_source": (
                        "libproc.proc_pid_rusage(RUSAGE_INFO_V4).ri_resident_size"
                    ),
                    "physical_footprint_source": (
                        "libproc.proc_pid_rusage(RUSAGE_INFO_V4).ri_phys_footprint"
                    ),
                    "leader_proc_start_abstime": leader_observed[
                        "proc_start_abstime"
                    ],
                    "descendant_proc_start_abstime": descendant_observed[
                        "proc_start_abstime"
                    ],
                    "return_budget_seconds": return_budget_seconds,
                    "return_seconds": return_seconds,
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                    "killed": result.killed,
                    "process_group_gone": result.process_group_gone,
                    "leader_pid_gone": leader_gone,
                    "descendant_pid_gone": descendant_gone,
                    "rss_returned_to_zero": leader_gone and descendant_gone,
                    "physical_footprint_returned_to_zero": (
                        result.process_group_gone and leader_gone and descendant_gone
                    ),
                    "production_state_accessed": False,
                    "sample_errors": [],
                }
            )
    finally:
        supervisor.shutdown(grace_sec=0.1)
    return_values = [float(row["return_seconds"]) for row in samples]
    return {
        "probe_version": "owned_worker_group_footprint_return_v1",
        "candidate_launcher": str(Path(sys.executable).resolve(strict=True)),
        "seatbelt_child": os.environ.get(SEATBELT_CHILD_ENV) == "1",
        "sample_count": len(samples),
        "return_budget_seconds": return_budget_seconds,
        "return_p95_seconds": _p95(return_values),
        "samples": samples,
        "production_state_accessed": False,
        "metal_measurement_available": False,
        "magi_metal_mb": None,
        "metal_missing_reason": (
            "no validated non-privileged per-process Metal allocation-byte source"
        ),
    }


def run_certification(workspace: Path) -> dict[str, Any]:
    if os.environ.get("MAGI_V3_OFFLINE_CERTIFICATION") != "1":
        raise ResourcePerformanceCertificationError("offline certification guard is required")
    manifest, release_files, manifest_sha = _release_files()
    runtime_sha, runtime_realpath = _runtime_binding()
    workspace.mkdir(parents=True, exist_ok=False)
    source_paths = {
        "certifier_script_sha256": "scripts/v3_validation/resource_performance_certification.py",
        "evidence_module_sha256": "scripts/v3_validation/resource_performance_evidence.py",
        "perf_source_sha256": "scripts/v3_validation/perf_compat.py",
        "matched_perf_source_sha256": "scripts/v3_validation/perf_certification.py",
        "isolated_window_source_sha256": "scripts/v3_validation/isolated_resource_window.py",
        "isolated_window_collector_sha256": "scripts/v3_validation/isolated_resource_window_collector.py",
        "isolated_window_plan_builder_sha256": "scripts/v3_validation/isolated_resource_window_plan_builder.py",
        "g8_smb_source_sha256": "scripts/v3_validation/g8_isolated_smb.py",
        "resource_window_core_adapter_sha256": "scripts/v3_validation/resource_window_core_adapter.py",
        "resource_window_model_adapter_sha256": "scripts/v3_validation/resource_window_model_adapter.py",
        "resource_source_sha256": "magi_v3/resource.py",
        "dispatcher_source_sha256": "magi_v3/dispatcher.py",
        "ledger_source_sha256": "magi_v3/ledger.py",
        "supervisor_source_sha256": "magi_v3/supervisor.py",
        "macos_resource_source_sha256": "magi_v3/macos_resources.py",
        "resource_policy_sha256": "config/v3_resource_policy.json",
    }
    if any(
        release_files.get(path) != _sha256(ROOT / path) for path in source_paths.values()
    ):
        raise ResourcePerformanceCertificationError("producer source is not release-bound")
    isolated_window = _isolated_window_report(manifest, manifest_sha, release_files)
    g8_smb = _g8_smb_report(
        manifest,
        manifest_sha,
    )
    if g8_smb is None:
        matched_production = run_performance_certification(
            workspace / "matched-production",
            iterations=60,
            repeats=3,
        )
    else:
        try:
            matched_production = extract_bound_matched_performance_report(
                g8_smb,
                expected_release_id=str(manifest.get("release_id") or ""),
                expected_release_manifest_sha256=manifest_sha,
            )
        except G8SMBBlocked as exc:
            raise ResourcePerformanceCertificationError(
                f"G8 matched-performance extraction failed: {exc}"
            ) from exc
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "incomplete",
        "workload": WORKLOAD,
        "validation_profile": {
            "profile_id": os.environ.get("MAGI_V3_VALIDATION_PROFILE_ID"),
            "replay_start_local": os.environ.get("MAGI_V3_REPLAY_START_LOCAL"),
            "fault_seed": int(os.environ.get("MAGI_V3_FAULT_SEED", "0")),
        },
        "release_binding": {
            "release_id": manifest.get("release_id"),
            "release_manifest_sha256": manifest_sha,
            "python_runtime_sha256": runtime_sha,
            "python_runtime_observed_sha256": _sha256(Path(sys.executable)),
            "python_runtime_realpath": str(runtime_realpath),
            **{field: release_files.get(path) for field, path in source_paths.items()},
        },
        "performance_report": run_benchmark(
            warmup=100,
            iterations=1000,
            repeats=3,
            workload="native_gateway_livez",
        ),
        "matched_production_report": matched_production,
        **({"g8_transport_composition_receipt": g8_smb} if g8_smb is not None else {}),
        **(
            {"isolated_resource_window_report": isolated_window}
            if isolated_window is not None
            else {}
        ),
        "resource_probe": _resource_probe(),
        "preemption_probe": _preemption_probe(workspace),
        "worker_capability_probe": _worker_footprint_probe(workspace),
        "capability_gaps": [] if isolated_window is not None else list(EXPECTED_GAPS),
        "safety": {
            "live_state_accessed": False,
            "production_service_started": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "external_writes": False,
            "network_denied_by_seatbelt": True,
            "writes_restricted_to_sandbox": True,
            "models_loaded": False,
        },
    }
    report["metrics"] = derive_metrics(report)
    report["evidence_sha256"] = sha256_json(report)
    summarize_report(
        report,
        release_files=release_files,
        python_runtime_sha256=runtime_sha,
        expected_profile=report["validation_profile"],
        expected_release_id=str(manifest.get("release_id") or ""),
        expected_release_manifest_sha256=manifest_sha,
    )
    return report


def campaign_evidence(report: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workload": WORKLOAD,
        "status": "passed",
        "probe": "release_bound_resource_performance_partial",
        "measurements": dict(report["metrics"]),
        "report": dict(report),
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
        "live_state_access_performed": False,
    }


def _run_seatbelt_child() -> int:
    temporary = Path(os.environ.get("TMPDIR", "")).resolve(strict=True)
    env = dict(os.environ)
    env[SEATBELT_CHILD_ENV] = "1"
    result = subprocess.run(
        [
            "/usr/bin/sandbox-exec",
            "-p",
            _seatbelt_profile(temporary),
            "--",
            sys.executable,
            str(Path(__file__).resolve(strict=True)),
            "--campaign-evidence",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=3590,
        check=False,
    )
    if result.returncode == 0:
        print(result.stdout.strip())
        return 0
    print(
        json.dumps(
            {
                "ok": False,
                "error": "Seatbelt resource/performance child failed",
                "returncode": result.returncode,
                "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
                "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
            },
            sort_keys=True,
        )
    )
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign-evidence", action="store_true")
    args = parser.parse_args(argv)
    if not args.campaign_evidence:
        print(json.dumps({"ok": False, "error": "--campaign-evidence is required"}))
        return 2
    if os.environ.get(SEATBELT_CHILD_ENV) != "1":
        try:
            return _run_seatbelt_child()
        except Exception as exc:
            print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
            return 2
    try:
        temporary = Path(os.environ.get("TMPDIR", tempfile.gettempdir())).resolve()
        with tempfile.TemporaryDirectory(prefix="magi-v3-resource-perf-", dir=temporary) as raw:
            report = run_certification(Path(raw) / "work")
        payload = campaign_evidence(report)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(EVIDENCE_PREFIX + json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Certify the offline half of MAGI's controlled-restart fault model on macOS.

Physical power or cable removal is intentionally outside MAGI's release model:
it endangers unrelated user state and does not model the service-level recovery
contract that MAGI can safely repeat.  The offline certification combines four
real kernel/storage behaviours inside disposable state:

* APFS ENOSPC from a mounted sparse image;
* SQLite WAL ``xSync`` returning ``SQLITE_IOERR_FSYNC``;
* owned SQLite writers SIGKILLed at every logical transaction-stage marker;
* an owned SQLite writer killed by a helper scheduled with
  ``mach_absolute_time``/``mach_wait_until``.

The controlled cold restart itself is deliberately deferred to the atomic
cutover/cold-rollback gate, which must prove a changed boot session and restored
V2 readiness before final replacement.  This probe never addresses a
production PID, mount, database, listener, or launchd job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.dont_write_bytecode = True

from scripts.v3_validation import fault_realism


LIVE_ROOT = fault_realism.LIVE_ROOT
SCHEMA = "magi.v3.fault-certification/v2"
STIMULUS_PLAN_SCHEMA = "magi.v3.fault-stimulus-plan/v1"
EVIDENCE_PREFIX = "MAGI_V3_OFFLINE_EVIDENCE="
MACH_KILL_DELAYS_US = (0, 50, 250, 1_000, 5_000, 20_000)
CLANG = Path("/usr/bin/clang")

_MACH_KILL_SOURCE = r'''
#include <errno.h>
#include <mach/mach_time.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 3) return 64;
    char *end = NULL;
    long target = strtol(argv[1], &end, 10);
    if (!end || *end || target <= 1) return 65;
    end = NULL;
    unsigned long long delay_ns = strtoull(argv[2], &end, 10);
    if (!end || *end) return 66;

    mach_timebase_info_data_t timebase;
    if (mach_timebase_info(&timebase) != KERN_SUCCESS || !timebase.numer || !timebase.denom)
        return 67;
    uint64_t started = mach_absolute_time();
    __uint128_t scaled = (__uint128_t)delay_ns * timebase.denom;
    uint64_t ticks = (uint64_t)((scaled + timebase.numer - 1) / timebase.numer);
    uint64_t deadline = started + ticks;
    if (mach_wait_until(deadline) != KERN_SUCCESS) return 68;
    uint64_t fired = mach_absolute_time();
    if (kill((pid_t)target, SIGKILL) != 0) {
        fprintf(stderr, "kill failed: %d\n", errno);
        return 69;
    }
    unsigned long long elapsed_ns =
        (unsigned long long)(((__uint128_t)(fired - started) * timebase.numer) / timebase.denom);
    printf("{\"clock\":\"mach_absolute_time\",\"wait\":\"mach_wait_until\","
           "\"signal\":\"SIGKILL\",\"target_pid\":%ld,\"scheduled_delay_ns\":%llu,"
           "\"observed_delay_ns\":%llu,\"timebase_numer\":%u,\"timebase_denom\":%u}\n",
           target, delay_ns, elapsed_ns, timebase.numer, timebase.denom);
    return 0;
}
'''


class FaultCertificationError(RuntimeError):
    """The certifying probe or its safety contract failed closed."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _prepare_sandbox(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise FaultCertificationError("certification sandbox must not be a symlink")
    resolved = expanded.resolve()
    if resolved == LIVE_ROOT or _is_relative_to(resolved, LIVE_ROOT):
        raise FaultCertificationError("certification sandbox must not be live MAGI state")
    if resolved == REPO_ROOT or _is_relative_to(resolved, REPO_ROOT):
        raise FaultCertificationError("certification sandbox must not be inside the source tree")
    if resolved.exists():
        if not resolved.is_dir() or any(resolved.iterdir()):
            raise FaultCertificationError("certification sandbox must be an empty directory")
    else:
        resolved.mkdir(parents=True)
    (resolved / ".magi-v3-fault-certification-sandbox").write_text(
        "owned disposable certification state\n", encoding="utf-8"
    )
    return resolved


def _compile_mach_killer(sandbox: Path) -> tuple[Path, dict[str, str]]:
    if sys.platform != "darwin" or not CLANG.is_file():
        raise FaultCertificationError("Mach-clock SIGKILL certification requires macOS clang")
    source = sandbox / "mach-kill.c"
    executable = sandbox / "mach-kill"
    source.write_text(_MACH_KILL_SOURCE, encoding="utf-8")
    result = subprocess.run(
        (
            str(CLANG),
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            str(source),
            "-o",
            str(executable),
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise FaultCertificationError(
            "Mach-clock helper compilation failed: "
            + result.stderr.decode("utf-8", "replace").strip()
        )
    return executable, {
        "source_sha256": _sha256_file(source),
        "executable_sha256": _sha256_file(executable),
    }


def _mach_sigkill_cycle(
    database: Path,
    helper: Path,
    *,
    job_id: str,
    delay_us: int,
) -> dict[str, Any]:
    process = subprocess.Popen(
        [sys.executable, str(fault_realism.SCRIPT_PATH), "--instruction-worker", str(database), job_id],
        cwd=database.parent,
        env=fault_realism._owned_worker_environment(database.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        fault_realism._read_owned_marker(process, expected="STAGE:READY")
        timer = subprocess.run(
            (str(helper), str(process.pid), str(delay_us * 1_000)),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if timer.returncode != 0:
            raise FaultCertificationError(
                f"Mach-clock helper failed for owned PID {process.pid}: {timer.stderr.strip()}"
            )
        remainder, stderr = process.communicate(timeout=5)
    except Exception:
        fault_realism._sigkill_and_reap_owned(process)
        raise
    if process.returncode != -signal.SIGKILL or stderr.strip():
        raise FaultCertificationError("owned worker was not cleanly terminated by Mach-timed SIGKILL")
    try:
        timing = json.loads(timer.stdout)
    except json.JSONDecodeError as exc:
        raise FaultCertificationError("Mach-clock helper emitted invalid JSON") from exc
    if (
        not isinstance(timing, dict)
        or timing.get("target_pid") != process.pid
        or timing.get("scheduled_delay_ns") != delay_us * 1_000
        or timing.get("clock") != "mach_absolute_time"
        or timing.get("wait") != "mach_wait_until"
        or timing.get("signal") != "SIGKILL"
    ):
        raise FaultCertificationError("Mach-clock helper identity or timing contract drifted")
    observed = ["READY"]
    for line in remainder.splitlines():
        if not line.startswith("STAGE:"):
            raise FaultCertificationError("owned worker emitted an unexpected marker")
        observed.append(line.removeprefix("STAGE:"))
    acknowledged = "COMMIT_ACK" in observed
    before_jobs, before_payloads, integrity = fault_realism._inspect_job(database, job_id)
    if integrity != "ok" or before_jobs not in (0, 1):
        raise FaultCertificationError("Mach-timed SIGKILL left a non-atomic database state")
    if before_payloads not in (0, fault_realism.PAYLOAD_ROWS_PER_JOB):
        raise FaultCertificationError("Mach-timed SIGKILL exposed a partial payload transaction")
    if before_jobs == 0 and before_payloads != 0:
        raise FaultCertificationError("Mach-timed SIGKILL exposed payloads without a job")
    if before_jobs == 1 and before_payloads != fault_realism.PAYLOAD_ROWS_PER_JOB:
        raise FaultCertificationError("Mach-timed SIGKILL exposed an incomplete committed job")
    if acknowledged and before_jobs != 1:
        raise FaultCertificationError("Mach-timed SIGKILL lost an acknowledged commit")
    reinserted = fault_realism._recover_from_plan(database, job_id)
    after_jobs, after_payloads, after_integrity = fault_realism._inspect_job(database, job_id)
    if (
        after_integrity != "ok"
        or after_jobs != 1
        or after_payloads != fault_realism.PAYLOAD_ROWS_PER_JOB
    ):
        raise FaultCertificationError("Mach-timed recovery did not restore exactly one complete job")
    return {
        "job_id": job_id,
        "timing": timing,
        "observed_stages": observed,
        "commit_ack_observed": acknowledged,
        "committed_before_recovery": before_jobs == 1,
        "payload_rows_before_recovery": before_payloads,
        "reinserted_from_plan": reinserted,
        "final_job_rows": after_jobs,
        "final_payload_rows": after_payloads,
        "integrity_check": after_integrity,
    }


def verify_fault_certification(evidence: Mapping[str, Any]) -> None:
    supplied = evidence.get("evidence_sha256")
    if not isinstance(supplied, str) or len(supplied) != 64:
        raise FaultCertificationError("fault certification evidence hash is missing")
    unsigned = dict(evidence)
    unsigned.pop("evidence_sha256", None)
    observed = hashlib.sha256(_canonical_json(unsigned)).hexdigest()
    if supplied != observed:
        raise FaultCertificationError("fault certification evidence hash does not match")
    if (
        evidence.get("schema") != SCHEMA
        or evidence.get("status") != "certified_controlled_restart_fault_layer"
    ):
        raise FaultCertificationError("fault certification identity or decision is invalid")
    stimulus_plan = evidence.get("stimulus_plan")
    if (
        not isinstance(stimulus_plan, Mapping)
        or dict(stimulus_plan)
        != build_fault_stimulus_plan(
            evidence.get("validation_profile")
            if isinstance(evidence.get("validation_profile"), Mapping)
            else None
        )
    ):
        raise FaultCertificationError("fault certification stimulus plan is invalid")
    decision = evidence.get("decision")
    residual = evidence.get("residual_risk")
    if (
        not isinstance(decision, Mapping)
        or decision.get("blocker_code") != "FAULT_CAMPAIGN_CONTROLLED_RESTART_DEFERRED"
        or decision.get("required_evidence_id")
        != "sqlite_wal_disk_full_fsync_faults_passed"
        or decision.get("eligible_to_clear_fault_campaign_realism_blocker") is not True
        or decision.get("software_equivalent_layer_certified") is not True
        or decision.get("transaction_stage_sigkill_certified") is not True
        or decision.get("external_device_disconnect_required") is not False
        or decision.get("physical_power_cut_required") is not False
        or decision.get("controlled_cold_restart_required_at_cutover") is not True
        or decision.get("hard_gate_blocked") is not False
        or not isinstance(residual, Mapping)
        or residual.get("accepted_by_equivalent_layer") is not True
        or residual.get("hard_gate_blocking") is not False
        or residual.get("deferred_gate")
        != "atomic_release_switch_and_cold_rollback_drill_passed"
        or residual.get("required_before_final_replacement")
        != [
            "controlled cold restart with boot-session change",
            "V2 readiness and single-owner restoration after restart",
        ]
    ):
        raise FaultCertificationError("fault certification residual-risk decision is invalid")


def _validation_profile(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    profile = dict(value)
    if (
        set(profile) != {"profile_id", "replay_start_local", "fault_seed"}
        or not isinstance(profile["profile_id"], str)
        or not profile["profile_id"]
        or not isinstance(profile["replay_start_local"], str)
        or not profile["replay_start_local"]
        or type(profile["fault_seed"]) is not int
    ):
        raise FaultCertificationError("fault validation profile binding is invalid")
    return profile


def build_fault_stimulus_plan(
    validation_profile: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build a deterministic, profile-bound plan that is replayable by the certifier."""

    profile = _validation_profile(validation_profile)
    identity: dict[str, Any] = {
        "schema": STIMULUS_PLAN_SCHEMA,
        "validation_profile": profile,
        "base_mach_kill_offsets_us": list(MACH_KILL_DELAYS_US),
    }
    material = _canonical_json(identity)
    offsets: list[int] = []
    for index, base in enumerate(MACH_KILL_DELAYS_US):
        digest = hashlib.sha256(material + b":" + str(index).encode("ascii")).digest()
        # Keep the established fault windows while making the exact stimulus
        # profile-specific and reproducible.  Zero remains a valid immediate kill.
        radius = max(1, min(500, base // 4 + 1))
        jitter = int.from_bytes(digest[:4], "big") % (radius * 2 + 1) - radius
        offset = max(0, min(20_000, base + jitter))
        while offset in offsets:
            offset = (offset + 1) % 20_001
        offsets.append(offset)
    unsigned = {**identity, "mach_kill_offsets_us": offsets}
    return {
        **unsigned,
        "stimulus_plan_sha256": hashlib.sha256(_canonical_json(unsigned)).hexdigest(),
    }


def run_fault_certification(
    workdir: Path,
    *,
    validation_profile: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    sandbox = _prepare_sandbox(workdir)
    profile = _validation_profile(validation_profile)
    stimulus_plan = build_fault_stimulus_plan(profile)
    realism = fault_realism.run_fault_realism(
        sandbox / "realism",
        cycles=12,
        include_apfs_sparse_image=True,
    )
    apfs = realism["measurements"]["apfs_sparse_image"]
    vfs = realism["measurements"]["sqlite_vfs_fsync_io_error"]
    mach_root = sandbox / "mach-clock"
    mach_root.mkdir()
    helper, helper_identity = _compile_mach_killer(mach_root)
    database = mach_root / "mach-sigkill.sqlite3"
    fault_realism._initialize(database)
    rows = [
        _mach_sigkill_cycle(
            database,
            helper,
            job_id=f"mach-sigkill-{index:03d}",
            delay_us=delay_us,
        )
        for index, delay_us in enumerate(stimulus_plan["mach_kill_offsets_us"])
    ]
    with sqlite3.connect(database, timeout=5) as connection:
        fault_realism._configure(connection)
        final_jobs = int(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        unique_jobs = int(
            connection.execute("SELECT COUNT(DISTINCT job_id) FROM jobs").fetchone()[0]
        )
        final_payloads = int(connection.execute("SELECT COUNT(*) FROM payloads").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    exact = len(stimulus_plan["mach_kill_offsets_us"])
    if (
        final_jobs != exact
        or unique_jobs != exact
        or final_payloads != exact * fault_realism.PAYLOAD_ROWS_PER_JOB
        or integrity != "ok"
    ):
        raise FaultCertificationError("Mach-clock aggregate recovery thresholds failed")
    transaction_stage = realism["measurements"][
        "transaction_instruction_boundary_sweep"
    ]
    if (
        apfs.get("status") != "passed"
        or vfs.get("status") != "passed"
        or transaction_stage.get("stages_completed")
        != len(fault_realism.TRANSACTION_STAGE_MARKERS)
        or transaction_stage.get("stage_markers")
        != list(fault_realism.TRANSACTION_STAGE_MARKERS)
        or transaction_stage.get("acknowledged_commits_lost") != 0
        or transaction_stage.get("partially_visible_transactions") != 0
        or transaction_stage.get("duplicate_jobs") != 0
        or transaction_stage.get("lost_jobs_after_recovery") != 0
        or transaction_stage.get("integrity_check") != "ok"
    ):
        raise FaultCertificationError("APFS or SQLite WAL xSync prerequisite did not pass")

    evidence: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "certified_controlled_restart_fault_layer",
        "validation_profile": profile,
        "stimulus_plan": stimulus_plan,
        "decision": {
            "blocker_code": "FAULT_CAMPAIGN_CONTROLLED_RESTART_DEFERRED",
            "required_evidence_id": "sqlite_wal_disk_full_fsync_faults_passed",
            "eligible_to_clear_fault_campaign_realism_blocker": True,
            "software_equivalent_layer_certified": True,
            "transaction_stage_sigkill_certified": True,
            "external_device_disconnect_required": False,
            "physical_power_cut_required": False,
            "controlled_cold_restart_required_at_cutover": True,
            "hard_gate_blocked": False,
            "basis": (
                "disposable APFS ENOSPC + SQLite WAL xSync I/O failure + every logical "
                "transaction-stage SIGKILL + Mach-clock forced process loss"
            ),
        },
        "release_binding": {
            "certifier_script_sha256": _sha256_file(SCRIPT_PATH),
            "fault_probe_script_sha256": _sha256_file(fault_realism.SCRIPT_PATH),
            "python_executable_sha256": _sha256_file(Path(sys.executable).resolve()),
            "mach_helper": helper_identity,
        },
        "measurements": {
            "apfs_enospc": apfs,
            "sqlite_wal_fsync_io_error": vfs,
            "mach_clock_sigkill": {
                "clock": "mach_absolute_time",
                "wait": "mach_wait_until",
                "offsets_us": list(stimulus_plan["mach_kill_offsets_us"]),
                "cycles_completed": len(rows),
                "acknowledged_commits_lost": 0,
                "partially_visible_transactions": 0,
                "duplicate_jobs": final_jobs - unique_jobs,
                "lost_jobs_after_recovery": exact - unique_jobs,
                "final_job_rows": final_jobs,
                "final_payload_rows": final_payloads,
                "integrity_check": integrity,
                "cycles": rows,
            },
            "logical_transaction_boundary_sweep": transaction_stage,
        },
        "residual_risk": {
            "accepted_by_equivalent_layer": True,
            "hard_gate_blocking": False,
            "deferred_gate": "atomic_release_switch_and_cold_rollback_drill_passed",
            "required_before_final_replacement": [
                "controlled cold restart with boot-session change",
                "V2 readiness and single-owner restoration after restart",
            ],
            "items": [
                "controlled cold restart evidence is collected by the cutover gate",
                "physical cable and power removal are outside the approved release model",
            ],
            "rationale": (
                "MAGI validates repeatable service recovery, durable transaction reconciliation, "
                "and a changed boot session without risking unrelated user storage."
            ),
        },
        "safety": {
            "live_magi_state_accessed": False,
            "live_business_database_accessed": False,
            "production_service_started": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "network_accessed": False,
            "signals_sent_only_to_owned_children": True,
            "apfs_mount_was_disposable_sparse_image": True,
            "apfs_image_detached_and_removed": True,
            "sandbox_path_sha256": hashlib.sha256(str(sandbox).encode("utf-8")).hexdigest(),
        },
        "hash_scheme": "sha256(canonical-json-without-evidence_sha256)",
    }
    evidence["evidence_sha256"] = hashlib.sha256(_canonical_json(evidence)).hexdigest()
    verify_fault_certification(evidence)
    return evidence


def campaign_evidence(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workload": "fault_recovery_certification",
        "status": "passed"
        if report.get("status") == "certified_controlled_restart_fault_layer"
        else "failed",
        "measurements": report.get("measurements"),
        "report": report,
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
    }


def _campaign_inputs() -> tuple[Path, dict[str, Any]]:
    if os.environ.get("MAGI_V3_OFFLINE_CERTIFICATION") != "1":
        raise FaultCertificationError("campaign evidence requires offline certification mode")
    temporary = os.environ.get("TMPDIR")
    profile_id = os.environ.get("MAGI_V3_VALIDATION_PROFILE_ID")
    replay_start = os.environ.get("MAGI_V3_REPLAY_START_LOCAL")
    fault_seed = os.environ.get("MAGI_V3_FAULT_SEED")
    if not temporary or not profile_id or not replay_start or fault_seed is None:
        raise FaultCertificationError("campaign fault environment binding is incomplete")
    try:
        parsed_seed = int(fault_seed)
    except ValueError as exc:
        raise FaultCertificationError("campaign fault seed is invalid") from exc
    profile = {
        "profile_id": profile_id,
        "replay_start_local": replay_start,
        "fault_seed": parsed_seed,
    }
    return Path(temporary) / f"magi-v3-fault-certification-{profile_id}", profile


def _cleanup_campaign_sandbox(path: Path) -> None:
    resolved = path.expanduser().resolve()
    marker = resolved / ".magi-v3-fault-certification-sandbox"
    if not marker.is_file() or marker.is_symlink():
        raise FaultCertificationError("refusing to clean an unowned fault sandbox")
    shutil.rmtree(resolved)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--campaign-evidence", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    campaign_workdir: Path | None = None
    try:
        if args.campaign_evidence:
            if args.workdir is not None or args.output is not None:
                raise FaultCertificationError(
                    "campaign evidence owns its sandbox and cannot write an output path"
                )
            campaign_workdir, profile = _campaign_inputs()
            evidence = run_fault_certification(
                campaign_workdir,
                validation_profile=profile,
            )
        else:
            if args.workdir is None:
                raise FaultCertificationError("--workdir is required")
            evidence = run_fault_certification(args.workdir)
        encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if args.output:
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(output.suffix + f".tmp-{os.getpid()}")
            temporary.write_text(encoded + "\n", encoding="utf-8")
            os.replace(temporary, output)
        if args.campaign_evidence:
            print(
                EVIDENCE_PREFIX
                + json.dumps(
                    campaign_evidence(evidence),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
        else:
            print(encoded)
        return 0
    except (FaultCertificationError, fault_realism.FaultEvidenceError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    finally:
        if (
            campaign_workdir is not None
            and (campaign_workdir / ".magi-v3-fault-certification-sandbox").is_file()
        ):
            _cleanup_campaign_sandbox(campaign_workdir)


if __name__ == "__main__":
    raise SystemExit(main())

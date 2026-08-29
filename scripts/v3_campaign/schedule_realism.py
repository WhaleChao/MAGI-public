"""Fail-closed production-duration and real-job-body schedule evidence.

This module never reads live scheduler state.  It consumes a checked-in,
hash-bound, redacted duration snapshot and executes only exact cron commands
whose real self-test bodies are allowlisted.  On macOS each body runs under a
Seatbelt profile which denies network, production runtime/agent/NAS/database
reads, and writes outside the caller-owned temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from scripts.v3_campaign.offline_probes import OfflineProbeError, bound_cron_jobs
from scripts.v3_validation.schedule_sample_evidence import (
    build_sample_evidence,
    canonical_sha256 as _sample_evidence_sha256,
)
from skills.ops.cron_command_identity import (
    CronCommandIdentityError,
    canonical_command_tokens,
    command_definition_sha256,
)


BASELINE_PATH = Path("config/v3_schedule_realism_baseline.json")
REALISM_WORKLOAD = "production_duration_and_representative_job_body_sandbox"
_ADAPTER_MODE = "real_entrypoint_dry_run_v1"
_ADAPTER_REQUIRED = {
    "mode": _ADAPTER_MODE,
    "dry_run": True,
    "fixture_root_required": True,
    "network": "deny_seatbelt",
    "notifications": "deny",
    "writes": "fixture_root_only",
}
MIN_SUCCESSFUL_SAMPLES = 3
DIAGNOSTIC_STREAM_LIMIT = 128 * 1024
BASELINE_SCHEMA_VERSION = 2
COMMAND_CHANGE_INVALIDATION_REASON = "COMMAND_DEFINITION_CHANGED_AFTER_OBSERVATION"
OBSERVATION_COMMAND_BINDING = "canonical_argv_sha256_v1"
SOURCE_EVIDENCE_RECEIPT_FIELD = "runtime_source_evidence_receipt_sha256"


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _source_evidence_receipt_sha256(source: Mapping[str, Any]) -> str:
    """Hash canonical source metadata, excluding the receipt itself."""

    payload = {
        str(key): value
        for key, value in source.items()
        if str(key) != SOURCE_EVIDENCE_RECEIPT_FIELD
    }
    return _sha256_bytes(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _fixture_inventory(root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    symlinks = 0
    files = 0
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            symlinks += 1
            rows.append({"relative": relative, "kind": "symlink"})
        elif path.is_file():
            files += 1
            rows.append(
                {
                    "relative": relative,
                    "kind": "file",
                    "sha256": _sha256_bytes(path.read_bytes()),
                }
            )
        elif path.is_dir():
            rows.append({"relative": relative, "kind": "directory"})
    return {
        "sha256": _sample_evidence_sha256(rows),
        "files": files,
        "no_symlinks": symlinks == 0,
    }


def _diagnostic_text(text: str, *, source_root: Path, sample_root: Path) -> str:
    normalized = str(text or "").replace(str(sample_root), "<SAMPLE_ROOT>")
    normalized = normalized.replace(str(source_root), "<SOURCE_ROOT>")
    normalized = normalized.replace(str(Path.home()), "<HOME>")
    encoded = normalized.encode("utf-8", errors="replace")
    if len(encoded) <= DIAGNOSTIC_STREAM_LIMIT:
        return normalized
    clipped = encoded[:DIAGNOSTIC_STREAM_LIMIT].decode("utf-8", errors="replace")
    return clipped + "\n<TRUNCATED>\n"


def _write_execution_diagnostic(
    sample_root: Path,
    *,
    source_root: Path,
    job_id: str,
    returncode: int | None,
    stdout: str,
    stderr: str,
    semantic_success: bool,
    fixture_binding_sha256: str,
) -> dict[str, str]:
    relative = Path("diagnostics") / "execution.json"
    target = sample_root / relative
    target.parent.mkdir(parents=True, exist_ok=False)
    payload = {
        "schema": "magi.v3.schedule-execution-diagnostic/v1",
        "job_id": job_id,
        "returncode": returncode,
        "semantic_success": semantic_success,
        "fixture_binding_sha256": fixture_binding_sha256,
        "dependency_evidence": {},
        "stdout": _diagnostic_text(
            stdout, source_root=source_root, sample_root=sample_root
        ),
        "stderr": _diagnostic_text(
            stderr, source_root=source_root, sample_root=sample_root
        ),
    }
    target.write_text(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "diagnostic_evidence_relative_path": relative.as_posix(),
        "diagnostic_evidence_sha256": _sha256_bytes(target.read_bytes()),
        "fixture_binding_sha256": fixture_binding_sha256,
    }


def _p95(values: list[float]) -> float:
    if not values:
        raise OfflineProbeError("duration percentile requires at least one sample")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _logical_definition_sha256(jobs: list[dict[str, Any]]) -> str:
    """Hash schedule semantics while ignoring runtime state and path rebasing."""

    normalized: list[dict[str, Any]] = []
    for raw in jobs:
        job = {
            key: value
            for key, value in raw.items()
            if not key.startswith("last_") and key != "result_evidence"
        }
        try:
            command = canonical_command_tokens(str(job.pop("command", "")))
        except CronCommandIdentityError as exc:
            raise OfflineProbeError(f"cron command is not parseable: {exc}") from exc
        job["command_tokens"] = command
        normalized.append(job)
    normalized.sort(key=lambda item: str(item.get("id") or ""))
    return _sha256_bytes(
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _command_definition_sha256(job: Mapping[str, Any]) -> str:
    """Bind an observation to canonical argv, not a release-root spelling."""
    try:
        return command_definition_sha256(job)
    except CronCommandIdentityError as exc:
        raise OfflineProbeError(f"cron command is not parseable: {exc}") from exc


def _load_baseline(source_root: Path) -> dict[str, Any]:
    path = source_root / BASELINE_PATH
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OfflineProbeError(f"schedule realism baseline unreadable: {exc}") from exc
    if (
        not isinstance(baseline, dict)
        or baseline.get("schema_version") != BASELINE_SCHEMA_VERSION
    ):
        raise OfflineProbeError(
            f"schedule realism baseline schema_version must be {BASELINE_SCHEMA_VERSION}"
        )
    if baseline.get("status") != "incomplete":
        raise OfflineProbeError("schedule realism baseline must not claim completion")
    return baseline


def _validate_baseline(
    baseline: Mapping[str, Any],
    jobs: list[dict[str, Any]],
    cron_sha: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    source = baseline.get("source_evidence")
    coverage = baseline.get("coverage")
    observations = baseline.get("observations")
    if not isinstance(source, dict):
        raise OfflineProbeError("duration baseline source evidence is malformed")
    for field in (
        "job_definitions_sha256",
        "logical_definition_sha256",
        "runtime_state_sha256",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(source.get(field) or "")):
            raise OfflineProbeError("duration baseline source hash is malformed")
    receipt_sha256 = str(source.get(SOURCE_EVIDENCE_RECEIPT_FIELD) or "")
    if not re.fullmatch(r"[0-9a-f]{64}", receipt_sha256):
        raise OfflineProbeError("duration baseline source receipt is malformed")
    if receipt_sha256 != _source_evidence_receipt_sha256(source):
        raise OfflineProbeError("duration baseline source receipt does not match")
    if (
        source.get("runtime_state_read_only_snapshot") is not True
        or source.get("raw_runtime_state_included") is not False
        or source.get("legacy_naive_timestamp_timezone") != "Asia/Taipei"
        or source.get("observation_timestamps_normalized_to_utc") is not True
    ):
        raise OfflineProbeError(
            "duration baseline runtime state source evidence is not fail-closed"
        )
    if source.get("observation_command_binding") != OBSERVATION_COMMAND_BINDING:
        raise OfflineProbeError("duration baseline command binding is unsupported")
    logical_sha = _logical_definition_sha256(jobs)
    if (
        source.get("job_definitions_sha256") != cron_sha
        and source.get("logical_definition_sha256") != logical_sha
    ):
        raise OfflineProbeError("duration baseline is not bound to the cron definitions")
    if not isinstance(coverage, dict) or not isinstance(observations, list):
        raise OfflineProbeError("duration baseline coverage/observations are malformed")

    enabled_ids = {
        str(job.get("id") or "") for job in jobs if job.get("enabled") is True
    }
    enabled_by_id = {
        str(job.get("id") or ""): job
        for job in jobs
        if job.get("enabled") is True
    }
    if coverage.get("job_definitions") != len(jobs):
        raise OfflineProbeError("duration baseline job-definition count mismatch")
    if coverage.get("enabled_job_definitions") != len(enabled_ids):
        raise OfflineProbeError("duration baseline enabled-job count mismatch")

    by_id: dict[str, dict[str, Any]] = {}
    for raw in observations:
        if not isinstance(raw, dict):
            raise OfflineProbeError("duration observation must be an object")
        job_id = str(raw.get("job_id") or "")
        duration = raw.get("duration_seconds")
        sample_count = raw.get("sample_count")
        if job_id not in enabled_ids or job_id in by_id:
            raise OfflineProbeError("duration observation references unknown or duplicate job")
        if raw.get("command_sha256") != _command_definition_sha256(
            enabled_by_id[job_id]
        ):
            raise OfflineProbeError(
                f"duration observation command binding drifted: {job_id}"
            )
        sample_count_valid = isinstance(sample_count, int) and not isinstance(sample_count, bool) and sample_count >= 1
        if (
            not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or duration <= 0
            or not sample_count_valid
            or raw.get("successful") is not True
        ):
            raise OfflineProbeError("duration observation is not valid successful evidence")
        samples = raw.get("samples")
        if samples is not None:
            if not isinstance(samples, list) or len(samples) != sample_count:
                raise OfflineProbeError("duration observation sample ledger is malformed")
            observed_timestamps: set[str] = set()
            for sample in samples:
                if not isinstance(sample, dict):
                    raise OfflineProbeError("duration sample must be an object")
                sample_duration = sample.get("duration_seconds")
                sample_at = str(sample.get("observed_at") or "").strip()
                if (
                    not isinstance(sample_duration, (int, float))
                    or isinstance(sample_duration, bool)
                    or sample_duration <= 0
                    or not sample_at
                    or sample_at in observed_timestamps
                ):
                    raise OfflineProbeError("duration sample is invalid or duplicated")
                observed_timestamps.add(sample_at)
        if sample_count >= MIN_SUCCESSFUL_SAMPLES:
            p95 = raw.get("duration_p95_seconds")
            if not isinstance(p95, (int, float)) or isinstance(p95, bool) or p95 <= 0:
                raise OfflineProbeError("multi-sample duration evidence lacks p95")
            if samples is None:
                raise OfflineProbeError("multi-sample duration evidence lacks its sample ledger")
            calculated_p95 = _p95(
                [float(sample["duration_seconds"]) for sample in samples]
            )
            if not math.isclose(float(p95), calculated_p95, rel_tol=0, abs_tol=1e-9):
                raise OfflineProbeError("duration p95 is inconsistent with its sample ledger")
        by_id[job_id] = dict(raw)

    duration_gaps = sorted(enabled_ids - set(by_id))
    invalidated = baseline.get("invalidated_observations")
    if not isinstance(invalidated, list):
        raise OfflineProbeError("duration baseline invalidation ledger is malformed")
    invalidated_ids: set[str] = set()
    for raw in invalidated:
        if not isinstance(raw, dict):
            raise OfflineProbeError("duration invalidation must be an object")
        job_id = str(raw.get("job_id") or "")
        observed_command_sha256 = str(raw.get("observed_command_sha256") or "")
        current_command_sha256 = str(raw.get("current_command_sha256") or "")
        invalidated_sample_count = raw.get("invalidated_sample_count")
        last_observed_at = str(raw.get("last_observed_at") or "").strip()
        if (
            job_id not in enabled_ids
            or job_id in by_id
            or job_id in invalidated_ids
            or raw.get("reason") != COMMAND_CHANGE_INVALIDATION_REASON
            or not re.fullmatch(r"[0-9a-f]{64}", observed_command_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", current_command_sha256)
            or observed_command_sha256 == current_command_sha256
            or current_command_sha256
            != _command_definition_sha256(enabled_by_id[job_id])
            or not isinstance(invalidated_sample_count, int)
            or isinstance(invalidated_sample_count, bool)
            or invalidated_sample_count < 1
            or not last_observed_at
        ):
            raise OfflineProbeError(
                f"duration command-change invalidation is invalid: {job_id or '<missing>'}"
            )
        invalidated_ids.add(job_id)
    if not invalidated_ids.issubset(duration_gaps):
        raise OfflineProbeError("duration invalidation ledger does not describe gaps")
    if coverage.get("enabled_jobs_with_successful_duration") != len(by_id):
        raise OfflineProbeError("duration baseline observed-job count mismatch")
    if coverage.get("enabled_jobs_without_successful_duration") != len(duration_gaps):
        raise OfflineProbeError("duration baseline gap count mismatch")
    jobs_meeting_samples = sum(
        int(item.get("sample_count") or 0) >= MIN_SUCCESSFUL_SAMPLES
        for item in by_id.values()
    )
    if coverage.get("jobs_meeting_minimum_samples") != jobs_meeting_samples:
        raise OfflineProbeError("duration baseline sample-depth count mismatch")
    global_available = not duration_gaps and jobs_meeting_samples == len(enabled_ids)
    if coverage.get("global_duration_percentile_available") is not global_available:
        raise OfflineProbeError("duration baseline global percentile claim is inconsistent")
    return by_id, duration_gaps


def bound_duration_replay_profiles(
    source_root: Path,
    jobs: list[dict[str, Any]],
    cron_sha: str,
    *,
    baseline_jobs: list[dict[str, Any]] | None = None,
    baseline_cron_sha: str | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Build honest per-job duration inputs for an accelerated replay.

    A job with three or more successful production observations may use its
    measured p95.  A sparse job uses only its largest observed success and is
    explicitly marked as an uncertifiable fallback.  The fallback keeps the
    engineering replay useful while preventing it from being presented as a
    production-p95 certification.
    """

    baseline_path = source_root / BASELINE_PATH
    baseline = _load_baseline(source_root)
    validation_jobs = baseline_jobs if baseline_jobs is not None else jobs
    validation_cron_sha = baseline_cron_sha or cron_sha
    observations, missing = _validate_baseline(
        baseline, validation_jobs, validation_cron_sha
    )
    enabled_ids = sorted(
        str(job.get("id") or "") for job in jobs if job.get("enabled") is True
    )
    profiles: dict[str, dict[str, Any]] = {}
    for job_id in enabled_ids:
        observation = observations.get(job_id)
        if observation is None:
            continue
        samples = observation.get("samples")
        if not isinstance(samples, list) or not samples:
            samples = [
                {
                    "duration_seconds": observation["duration_seconds"],
                    "observed_at": observation.get("observed_at"),
                }
            ]
        sample_count = int(observation["sample_count"])
        durations = [float(sample["duration_seconds"]) for sample in samples]
        deep = sample_count >= MIN_SUCCESSFUL_SAMPLES
        profiles[job_id] = {
            "duration_seconds": (
                float(observation["duration_p95_seconds"])
                if deep
                else max(durations)
            ),
            "duration_basis": (
                "production_success_p95"
                if deep
                else "production_success_observed_max_sparse_fallback"
            ),
            "successful_sample_count": sample_count,
            "certifying_p95": deep,
        }

    p95_jobs = sorted(
        job_id for job_id, row in profiles.items() if row["certifying_p95"] is True
    )
    sparse_jobs = sorted(set(enabled_ids) - set(p95_jobs) - set(missing))
    normalized = {
        "cron_jobs_sha256": cron_sha,
        "baseline_sha256": _sha256_bytes(baseline_path.read_bytes()),
        "profiles": profiles,
    }
    profile_sha = _sha256_bytes(
        json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return profiles, {
        "enabled_jobs": len(enabled_ids),
        "profiles": len(profiles),
        "p95_jobs": len(p95_jobs),
        "sparse_fallback_jobs": len(sparse_jobs),
        "missing_jobs": len(missing),
        "minimum_successful_samples": MIN_SUCCESSFUL_SAMPLES,
        "certifying_p95_coverage": not missing and not sparse_jobs,
        "p95_job_ids": p95_jobs,
        "sparse_fallback_job_ids": sparse_jobs,
        "missing_job_ids": sorted(missing),
        "cron_jobs_sha256": cron_sha,
        "baseline_sha256": normalized["baseline_sha256"],
        "duration_profiles_sha256": profile_sha,
    }


def _command_for_allowlist(
    source_root: Path,
    job: Mapping[str, Any],
    allow: Mapping[str, Any],
) -> list[str]:
    try:
        parsed = shlex.split(str(job.get("command") or ""), posix=True)
    except ValueError as exc:
        raise OfflineProbeError(f"allowlisted cron command is not parseable: {exc}") from exc
    interpreter = os.environ.get("MAGI_V3_PYTHON_RUNTIME") or str(source_root / "venv/bin/python3")
    expected_tail = [
        str(source_root / str(allow.get("entrypoint") or "")),
        *[str(item) for item in allow.get("arguments", [])],
    ]
    legacy_source_interpreters = {
        str(source_root / "venv/bin/python3"),
        str(source_root / "venv/bin/python"),
        str(source_root / "venv/Scripts/python.exe"),
    }
    if (
        len(parsed) != len(expected_tail) + 1
        or parsed[1:] != expected_tail
        or parsed[0] not in ({str(interpreter)} | legacy_source_interpreters)
    ):
        raise OfflineProbeError(
            f"allowlisted cron command changed for {allow.get('job_id')}; review required"
        )
    # Source schedules retain a rebaseable virtualenv anchor, while modern V3
    # diagnostics use the hash-bound centralized runtime. Execute the reviewed
    # body with that explicit runtime after proving every non-interpreter token
    # still exactly matches the allowlist.
    return [str(interpreter), *expected_tail]


def _seatbelt_profile(source_root: Path, workdir: Path) -> str:
    def quoted(path: Path) -> str:
        return json.dumps(str(path.resolve()))

    denied_reads = (
        source_root / ".runtime",
        source_root / ".agent",
        Path("/Volumes"),
        Path.home() / ".magi_mounts",
        Path("/opt/homebrew/var/mysql"),
    )
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny network*)",
        "(deny file-write*)",
        f"(allow file-write* (subpath {quoted(workdir)}))",
    ]
    rules.extend(f"(deny file-read* (subpath {quoted(path)}))" for path in denied_reads)
    return "".join(rules)


def _prepare_adapter_fixture(job_id: str, fixture_root: Path) -> dict[str, Any]:
    fixture_root.mkdir(parents=True, exist_ok=False)
    marker = fixture_root / ".magi-v3-schedule-fixture"
    marker.write_text(job_id + "\n", encoding="utf-8")
    prepared: list[str] = [marker.name]
    if job_id == "job_debug_cleanup":
        captures = fixture_root / "debug-captures"
        captures.mkdir()
        old = captures / "old-debug.json"
        old.write_text('{"fixture":true}\n', encoding="utf-8")
        os.utime(old, (1, 1))
        prepared.append(old.relative_to(fixture_root).as_posix())
    elif job_id == "job_weekly_cache_cleanup":
        cache = fixture_root / "weekly-cache"
        cache.mkdir()
        old = cache / "old-cache.bin"
        old.write_bytes(b"offline schedule fixture\n")
        os.utime(old, (1, 1))
        prepared.append(old.relative_to(fixture_root).as_posix())
    elif job_id == "job_disk_low_water_alarm":
        probe = fixture_root / "disk-probe"
        probe.mkdir()
        prepared.append(probe.relative_to(fixture_root).as_posix())
    elif job_id == "job_benchmark_osc_todos":
        output = fixture_root / "benchmark-output"
        output.mkdir()
        prepared.append(output.relative_to(fixture_root).as_posix())
    elif job_id == "job_transcript_self_test":
        config = fixture_root / "transcript-config.json"
        config.write_text('{"classification":"synthetic_non_sensitive"}\n', encoding="utf-8")
        prepared.append(config.relative_to(fixture_root).as_posix())
    return {
        "fixture_files": sorted(prepared),
        "fixture_manifest_sha256": _sha256_bytes(
            json.dumps(sorted(prepared), separators=(",", ":")).encode("utf-8")
        ),
    }


def _validate_adapter(allow: Mapping[str, Any]) -> Mapping[str, Any]:
    adapter = allow.get("adapter")
    if not isinstance(adapter, dict) or any(
        adapter.get(key) != value for key, value in _ADAPTER_REQUIRED.items()
    ):
        raise OfflineProbeError(
            f"allowlisted job {allow.get('job_id')} lacks the exact dry-run adapter contract"
        )
    if set(adapter) != set(_ADAPTER_REQUIRED):
        raise OfflineProbeError(
            f"allowlisted job {allow.get('job_id')} adapter contract has unreviewed fields"
        )
    return adapter


def _extract_json_object(stdout: str, required_field: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(stdout):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and required_field in value:
            return value
    return {}


def _execute_allowlisted_body(
    source_root: Path,
    workdir: Path,
    job: Mapping[str, Any],
    allow: Mapping[str, Any],
) -> dict[str, Any]:
    execution_nonce_sha256 = hashlib.sha256(os.urandom(32)).hexdigest()
    sandbox_exec = shutil.which("sandbox-exec")
    if sys.platform != "darwin" or not sandbox_exec:
        return {
            "job_id": allow["job_id"],
            "status": "gap",
            "gap_code": "OS_NETWORK_FILESYSTEM_SANDBOX_UNAVAILABLE",
            "executed": False,
            "execution_nonce_sha256": execution_nonce_sha256,
        }

    adapter = _validate_adapter(allow)
    command = _command_for_allowlist(source_root, job, allow)
    job_dir = workdir / str(allow["job_id"])
    temp_dir = job_dir / "tmp"
    home_dir = job_dir / "home"
    temp_dir.mkdir(parents=True, exist_ok=False)
    home_dir.mkdir(parents=True, exist_ok=False)
    fixture_root = job_dir / "fixture"
    fixture_evidence = _prepare_adapter_fixture(str(allow["job_id"]), fixture_root)
    fixture_binding_sha256 = _sample_evidence_sha256(
        {
            "schema": "magi.v3.schedule-fixture-bindings/v1",
            "adapter": dict(adapter),
            "fixture_manifest_sha256": fixture_evidence[
                "fixture_manifest_sha256"
            ],
        }
    )
    initial_inventory = _fixture_inventory(fixture_root)
    profile = _seatbelt_profile(source_root, job_dir)
    env = {
        "HOME": str(home_dir),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "MAGI_V3_REALISM_SANDBOX": "1",
        "MAGI_V3_SCHEDULE_ADAPTER": str(adapter["mode"]),
        "MAGI_V3_SCHEDULE_DRY_RUN": "1",
        "MAGI_V3_SCHEDULE_FIXTURE_ROOT": str(fixture_root),
        "MAGI_V3_SCHEDULE_NO_NETWORK": "1",
        "MAGI_V3_SCHEDULE_NO_NOTIFY": "1",
        "MAGI_DISABLE_NOTIFICATIONS": "1",
        "MAGI_ROOT": str(source_root),
        "MAGI_ROOT_DIR": str(source_root),
        "MAGI_RUNTIME_DIR": str(job_dir / "runtime"),
        "MAGI_AGENT_DIR": str(job_dir / "agent"),
        "MAGI_EXPORTS_DIR": str(job_dir / "exports"),
        "MAGI_METRICS_DIR": str(job_dir / "metrics"),
        "MAGI_AUTOPILOT_RUNS_DIR": str(job_dir / "autopilot-runs"),
        "NO_PROXY": "*",
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(temp_dir),
    }
    started = time.perf_counter()
    try:
        result = subprocess.run(
            [sandbox_exec, "-p", profile, "--", *command],
            cwd=job_dir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        diagnostic = _write_execution_diagnostic(
            job_dir,
            source_root=source_root,
            job_id=str(allow["job_id"]),
            returncode=None,
            stdout=(
                exc.stdout.decode("utf-8", errors="replace")
                if isinstance(exc.stdout, bytes)
                else str(exc.stdout or "")
            ),
            stderr=(
                exc.stderr.decode("utf-8", errors="replace")
                if isinstance(exc.stderr, bytes)
                else str(exc.stderr or "")
            ),
            semantic_success=False,
            fixture_binding_sha256=fixture_binding_sha256,
        )
        return {
            "job_id": allow["job_id"],
            "status": "failed",
            "gap_code": "SANDBOX_BODY_TIMEOUT",
            "executed": True,
            "execution_nonce_sha256": execution_nonce_sha256,
            "duration_seconds": round(time.perf_counter() - started, 6),
            "stdout_sha256": _sha256_bytes((exc.stdout or b"") if isinstance(exc.stdout, bytes) else str(exc.stdout or "").encode()),
            "stderr_sha256": _sha256_bytes((exc.stderr or b"") if isinstance(exc.stderr, bytes) else str(exc.stderr or "").encode()),
            **diagnostic,
        }

    success_field = str(allow.get("expected_success_field") or "success")
    expected_value = allow.get("expected_success_value", True)
    payload = _extract_json_object(result.stdout, success_field)
    semantic_success = payload.get(success_field) == expected_value
    final_inventory = _fixture_inventory(fixture_root)
    passed = (
        result.returncode == 0
        and semantic_success
        and initial_inventory["no_symlinks"] is True
        and final_inventory["no_symlinks"] is True
    )
    diagnostic = _write_execution_diagnostic(
        job_dir,
        source_root=source_root,
        job_id=str(allow["job_id"]),
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        semantic_success=semantic_success,
        fixture_binding_sha256=fixture_binding_sha256,
    )
    return {
        "job_id": allow["job_id"],
        "status": "passed" if passed else "failed",
        "gap_code": None if passed else "REAL_JOB_BODY_SANDBOX_FAILED",
        "executed": True,
        "execution_nonce_sha256": execution_nonce_sha256,
        "duration_seconds": round(time.perf_counter() - started, 6),
        "returncode": result.returncode,
        "semantic_success": semantic_success,
        "no_fixture_symlinks": final_inventory["no_symlinks"],
        "fixture_initial_inventory_sha256": initial_inventory["sha256"],
        "fixture_final_inventory_sha256": final_inventory["sha256"],
        "fixture_final_file_count": final_inventory["files"],
        "success_contract_evidence": {
            "checks": {
                "returncode_zero": result.returncode == 0,
                "expected_success_field_matched": semantic_success,
                "fixture_remained_symlink_free": final_inventory[
                    "no_symlinks"
                ],
            },
            "payload_sha256": _sample_evidence_sha256(payload),
            "fixture_manifest_sha256": fixture_evidence[
                "fixture_manifest_sha256"
            ],
        },
        "dependency_evidence": {
            "kind": "none",
            "request_count": 0,
            "request_counts": {},
            "expected_requests_satisfied": True,
            "transcript_sha256": _sample_evidence_sha256([]),
            "postcondition_count": 0,
            "passed_postcondition_count": 0,
            "postconditions_passed": True,
            "postconditions_sha256": _sample_evidence_sha256([]),
        },
        "stdout_sha256": _sha256_bytes(result.stdout.encode("utf-8")),
        "stderr_sha256": _sha256_bytes(result.stderr.encode("utf-8")),
        "sandbox_profile_sha256": _sha256_bytes(profile.encode("utf-8")),
        "adapter_mode": adapter["mode"],
        "adapter_dry_run": True,
        "adapter_fixture_root_sha256": _sha256_bytes(str(fixture_root).encode("utf-8")),
        "adapter_fixture_manifest_sha256": fixture_evidence["fixture_manifest_sha256"],
        "adapter_fixture_files": fixture_evidence["fixture_files"],
        "network_denied_by_seatbelt": True,
        "notifications_disabled": True,
        **diagnostic,
    }


def _execute_body_samples(
    source_root: Path,
    workdir: Path,
    job: Mapping[str, Any],
    allow: Mapping[str, Any],
) -> dict[str, Any]:
    """Require three independent successful executions of one real dry-run body."""

    samples = [
        _execute_allowlisted_body(
            source_root,
            workdir / f"sample-{index:03d}",
            job,
            allow,
        )
        for index in range(1, MIN_SUCCESSFUL_SAMPLES + 1)
    ]
    passed_samples = [row for row in samples if row.get("status") == "passed"]
    durations = [
        float(row["duration_seconds"])
        for row in passed_samples
        if isinstance(row.get("duration_seconds"), (int, float))
        and not isinstance(row.get("duration_seconds"), bool)
        and row["duration_seconds"] > 0
    ]
    passed = (
        len(passed_samples) == MIN_SUCCESSFUL_SAMPLES
        and len(durations) == MIN_SUCCESSFUL_SAMPLES
        and all(row.get("semantic_success") is True for row in samples)
    )
    first = samples[0]
    entrypoint_sha256 = _sha256_bytes(
        (source_root / str(allow["entrypoint"])).read_bytes()
    )
    sample_evidence = [
        build_sample_evidence(
            row,
            sample_index=index,
            execution_kind="inherited_real_entrypoint_dry_run_v1",
            entrypoint_sha256=entrypoint_sha256,
        )
        for index, row in enumerate(samples, 1)
    ]
    result = {
        "job_id": allow["job_id"],
        "status": "passed" if passed else "failed",
        "gap_code": None if passed else "INSUFFICIENT_SUCCESSFUL_SANDBOX_BODY_SAMPLES",
        "executed": all(row.get("executed") is True for row in samples),
        "semantic_success": passed,
        "samples_requested": MIN_SUCCESSFUL_SAMPLES,
        "successful_samples": len(passed_samples),
        "duration_sample_count": len(durations),
        "duration_samples_seconds": durations,
        "duration_p95_seconds": round(_p95(durations), 6) if passed else None,
        "sample_evidence": sample_evidence,
        "sample_evidence_sha256": _sample_evidence_sha256(sample_evidence),
        "entrypoint_sha256": entrypoint_sha256,
        "sample_statuses": [str(row.get("status") or "") for row in samples],
        "sandbox_profile_sha256_samples": [
            str(row.get("sandbox_profile_sha256") or "") for row in samples
        ],
        "stdout_sha256_samples": [str(row.get("stdout_sha256") or "") for row in samples],
        "stderr_sha256_samples": [str(row.get("stderr_sha256") or "") for row in samples],
        # Keep the single-value fields for the campaign parser while binding all
        # three samples above.  Fixture manifests are content based and must be
        # identical between independent executions.
        "sandbox_profile_sha256": first.get("sandbox_profile_sha256"),
        "adapter_mode": first.get("adapter_mode"),
        "adapter_dry_run": first.get("adapter_dry_run"),
        "adapter_fixture_manifest_sha256": first.get(
            "adapter_fixture_manifest_sha256"
        ),
        "adapter_fixture_files": first.get("adapter_fixture_files"),
        "network_denied_by_seatbelt": all(
            row.get("network_denied_by_seatbelt") is True for row in samples
        ),
        "notifications_disabled": all(
            row.get("notifications_disabled") is True for row in samples
        ),
    }
    return result


def _gap_reasons(job: Mapping[str, Any]) -> list[str]:
    command = str(job.get("command") or "").lower()
    reasons = ["NOT_EXACTLY_ALLOWLISTED_FOR_OFFLINE_BODY_EXECUTION"]
    if command.startswith("@magi"):
        reasons.append("ORCHESTRATOR_OR_LIVE_SERVICE_REQUIRED")
    if any(
        token in command
        for token in ("http", "gmail", "google", "drive", "portal", "worldmonitor", "judicial", "transcript-downloader")
    ):
        reasons.append("NETWORK_OR_EXTERNAL_SERVICE_RISK")
    if any(token in command for token in ("/volumes", "nas_", "synology", ".magi_mounts")):
        reasons.append("NAS_OR_EXTERNAL_STORAGE_RISK")
    if any(token in command for token in ("mariadb", "mysql", "accounting", "cortex", "vector", "index_cases", "reindex")):
        reasons.append("PRODUCTION_DATABASE_OR_INDEX_RISK")
    if any(token in command for token in ("--apply", "--commit", "cleanup", "purge", "repair", "rename", "backup")):
        reasons.append("PRODUCTION_MUTATION_RISK")
    if any(token in command for token in ("live", "omlx", "model", "translation", "external_chat")):
        reasons.append("LIVE_MODEL_OR_SERVICE_RISK")
    return reasons


def run_schedule_realism_assessment(source_root: Path, workdir: Path) -> dict[str, Any]:
    """Run exact safe bodies and enumerate every unmeasured/unexecuted job gap."""

    baseline = _load_baseline(source_root)
    jobs, cron_sha = bound_cron_jobs(source_root)
    observations, duration_gap_ids = _validate_baseline(baseline, jobs, cron_sha)
    by_id = {str(job["id"]): job for job in jobs}
    enabled_ids = sorted(job_id for job_id, job in by_id.items() if job.get("enabled") is True)

    raw_allowlist = baseline.get("representative_body_allowlist")
    if not isinstance(raw_allowlist, list):
        raise OfflineProbeError("representative body allowlist is malformed")
    allowlist = {str(item.get("job_id") or ""): item for item in raw_allowlist if isinstance(item, dict)}
    if not allowlist or "" in allowlist or not set(allowlist).issubset(enabled_ids):
        raise OfflineProbeError("representative body allowlist references invalid jobs")

    body_results = [
        _execute_body_samples(source_root, workdir, by_id[job_id], allowlist[job_id])
        for job_id in sorted(allowlist)
    ]
    body_gap_ids = sorted(set(enabled_ids) - set(allowlist))
    duration_sample_depth_gap_ids = sorted(
        job_id
        for job_id, observation in observations.items()
        if int(observation.get("sample_count") or 0) < MIN_SUCCESSFUL_SAMPLES
    )
    gaps: list[dict[str, Any]] = [
        {
            "job_id": job_id,
            "gap_type": "production_duration",
            "reasons": ["NO_SUCCESSFUL_DURATION_OBSERVATION"],
        }
        for job_id in duration_gap_ids
    ]
    gaps.extend(
        {
            "job_id": job_id,
            "gap_type": "production_duration_sample_depth",
            "reasons": [
                "FEWER_THAN_THREE_INDEPENDENT_SUCCESSFUL_PRODUCTION_OBSERVATIONS"
            ],
        }
        for job_id in duration_sample_depth_gap_ids
    )
    gaps.extend(
        {
            "job_id": job_id,
            "gap_type": "representative_job_body",
            "reasons": _gap_reasons(by_id[job_id]),
        }
        for job_id in body_gap_ids
    )
    gaps.extend(
        {
            "job_id": str(result["job_id"]),
            "gap_type": "representative_job_body_execution",
            "reasons": [str(result.get("gap_code") or "UNKNOWN_SANDBOX_FAILURE")],
        }
        for result in body_results
        if result["status"] != "passed"
    )

    all_bodies_passed = all(result["status"] == "passed" for result in body_results)
    full_duration_p95 = not duration_gap_ids and not duration_sample_depth_gap_ids
    full_body_coverage = not body_gap_ids and all_bodies_passed
    complete = full_duration_p95 and full_body_coverage
    release_binding = {
        "release_id": os.environ.get("MAGI_V3_RELEASE_ID"),
        "release_manifest_sha256": os.environ.get(
            "MAGI_V3_RELEASE_MANIFEST_SHA256"
        ),
        "cron_jobs_sha256": cron_sha,
        "baseline_sha256": _sha256_bytes(
            (source_root / BASELINE_PATH).read_bytes()
        ),
    }
    evidence = {
        "schema_version": 1,
        "workload": REALISM_WORKLOAD,
        "status": "passed" if complete else "incomplete",
        "completion_claimed": complete,
        "release_binding": release_binding,
        "blocker": {
            "code": "SCHEDULE_LOAD_REALISM_INCOMPLETE",
            "eligible_to_clear": complete,
            "decision": "clear" if complete else "blocker_retained",
            "reasons": [
                reason
                for condition, reason in (
                    (
                        bool(duration_gap_ids),
                        "MISSING_SUCCESSFUL_PRODUCTION_DURATION",
                    ),
                    (
                        bool(duration_sample_depth_gap_ids),
                        "PRODUCTION_P95_SAMPLE_COVERAGE_INCOMPLETE",
                    ),
                    (
                        bool(body_gap_ids),
                        "REAL_JOB_BODY_ADAPTER_COVERAGE_INCOMPLETE",
                    ),
                    (
                        not all_bodies_passed,
                        "REAL_JOB_BODY_SANDBOX_SAMPLES_FAILED",
                    ),
                )
                if condition
            ],
        },
        "measurements": {
            "cron_definitions": len(jobs),
            "enabled_cron_definitions": len(enabled_ids),
            "production_duration_observations": len(observations),
            "production_duration_gap_jobs": len(duration_gap_ids),
            "production_duration_p95_jobs": len(enabled_ids)
            - len(duration_gap_ids)
            - len(duration_sample_depth_gap_ids),
            "production_duration_sparse_sample_jobs": len(
                duration_sample_depth_gap_ids
            ),
            "minimum_successful_duration_samples": MIN_SUCCESSFUL_SAMPLES,
            "production_duration_percentile_available": bool(
                baseline["coverage"].get("global_duration_percentile_available")
            ),
            "representative_bodies_allowlisted": len(allowlist),
            "representative_bodies_passed": sum(result["status"] == "passed" for result in body_results),
            "representative_body_gap_jobs": len(body_gap_ids),
            "representative_body_minimum_samples": MIN_SUCCESSFUL_SAMPLES,
            "representative_body_jobs_meeting_minimum_samples": sum(
                int(result.get("successful_samples") or 0)
                >= MIN_SUCCESSFUL_SAMPLES
                for result in body_results
            ),
            "all_allowlisted_bodies_passed": all_bodies_passed,
            "cron_jobs_sha256": cron_sha,
        },
        "duration_observations": [observations[job_id] for job_id in sorted(observations)],
        "body_results": body_results,
        "gaps": gaps,
        "network_access_performed": False,
        "production_database_access_performed": False,
        "nas_access_performed": False,
        "live_service_access_performed": False,
        "production_state_write_performed": False,
        "sandbox_writes_only": True,
    }
    evidence["evidence_sha256"] = _sha256_bytes(
        json.dumps(
            evidence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return evidence

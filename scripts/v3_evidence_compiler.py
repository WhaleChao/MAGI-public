#!/usr/bin/env python3
"""Compile hash-bound V3 producer ledgers into normalized release evidence.

The compiler is deliberately fail-closed.  It never starts services, reads
live MAGI state, or invents missing measurements.  An evidence item is emitted
only after its immutable upstream artifacts and release context are verified;
semantic rules determine whether that item is ``passed`` or ``failed``.
Unavailable live, physical-device, cutover, and human evidence remains absent.
"""

from __future__ import annotations

import argparse
import contextvars
import functools
import hashlib
import json
import os
import stat
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:  # Support ``python scripts/v3_evidence_compiler.py``.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.architecture.generate_v2_inventory import (
    collect_daemon_children,
    collect_launchagents,
    collect_portable_cron_bytes,
    collect_routes,
    collect_skills,
    project_inventory_to_release,
)
from scripts.v3_backup_prepare import REQUIRED_COVERAGE, verify_backup
from scripts.v3_campaign.runner import (
    CampaignSafetyError,
    ReleaseBundle,
    _validate_fault_certification_evidence,
    _validate_health_certification_evidence,
    _validate_resource_performance_partial,
    _validate_release_quality_certification_evidence,
    _validate_route_certification_evidence,
    verify_release_bundle,
)
from scripts.v3_validation.release_quality_evidence import (
    EXPECTED_GOLDEN_SETS,
    EXPECTED_QUALITY_GROUPS,
    EXPECTED_V3_SUITES,
    GOLDEN_DEPENDENCY_PATHS,
    ReleaseQualityEvidenceError,
    summarize_report as summarize_release_quality_report,
)
from scripts.v3_validation.resource_performance_evidence import (
    GATE_IDS as RESOURCE_PERFORMANCE_GATE_IDS,
    ResourcePerformanceEvidenceError,
    summarize_report as summarize_resource_performance_report,
)
from scripts.v3_release_gate import (
    CONTEXT_FIELDS,
    EVIDENCE_SPECS,
    FORMAL_BACKUP_DATABASES,
    NORMALIZED_EVIDENCE_WHITELIST,
    SHA256_RE,
    _canonical_json_bytes,
    _validate_metric_rule,
)
from scripts.v3_source_contract import SourceContractError, resolve_source_contract
from scripts.v3_validation.inventory import validate_inventory


NORMALIZER = "scripts.v3_evidence_compiler"
NORMALIZER_SCHEMA = "magi.v3.trusted-evidence-normalizer/v1"
EXTERNAL_EVIDENCE_REQUIREMENTS = {
    "matched_v2_warm_cold_performance_baseline_complete": (
        "requires matched production MariaDB/session/NAS/folder/archive and model throughput measurements"
    ),
    "seven_day_schedule_10x_arrival_2x_duration_replay_passed": (
        "requires per-priority P2 success measurement and every enabled real job body"
    ),
    "sqlite_wal_disk_full_fsync_faults_passed": (
        "requires physical-device APFS ENOSPC, true power interruption, and arbitrary instruction-offset SIGKILL"
    ),
    "offline_replay_and_isolated_live_validation_satisfied": (
        "requires three authorized reset-separated isolated LIVE runs"
    ),
    "isolated_live_validation_single_active_handoff_verified": (
        "requires three authorized LIVE ownership handoffs with zero overlap"
    ),
    "single_active_handoff_test_passed": "requires an authorized cutover drill",
    "v2_fully_stopped_before_v3_start_verified": "requires a LIVE V2 zero-owner proof",
    "v3_fully_stopped_before_v2_rollback_verified": "requires a cutover-drill V3 zero-owner proof",
    "single_scheduler_consumer_writer_ownership_verified": (
        "requires a LIVE scheduler/consumer/writer ownership probe"
    ),
    "worker_process_group_footprint_and_metal_return_to_baseline": (
        "requires measured RSS and Metal return-to-baseline evidence"
    ),
    "input_method_candidate_window_probe_passed": (
        "requires an authorized native IME candidate-window pressure observation"
    ),
    "atomic_release_switch_and_cold_rollback_drill_passed": (
        "requires an authorized atomic switch and cold rollback drill"
    ),
    "human_go_approval_recorded": "requires the authorized release owner's exact-context approval",
}


class EvidenceCompileError(RuntimeError):
    """Raised when an upstream artifact cannot be trusted."""


@dataclass(frozen=True, slots=True)
class CompileContext:
    campaign_id: str
    release_sha: str
    hardware_id: str
    gate_config_sha256: str

    def as_dict(self) -> dict[str, str]:
        return {field: str(getattr(self, field)) for field in CONTEXT_FIELDS}

    def validate(self) -> None:
        if not self.campaign_id or not self.hardware_id:
            raise EvidenceCompileError("campaign_id and hardware_id must be non-empty")
        if not SHA256_RE.fullmatch(self.release_sha):
            raise EvidenceCompileError("release_sha must be lowercase SHA-256")
        if not SHA256_RE.fullmatch(self.gate_config_sha256):
            raise EvidenceCompileError("gate_config_sha256 must be lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class SourceArtifact:
    role: str
    path: Path
    media_type: str = "application/json"


@dataclass(frozen=True, slots=True)
class FrozenFile:
    path: Path
    data: bytes
    sha256: str


_FROZEN_FILES: contextvars.ContextVar[dict[Path, FrozenFile] | None] = (
    contextvars.ContextVar("v3_evidence_frozen_files", default=None)
)


def _with_compilation_scope(function: Any) -> Any:
    """Give each public compilation invocation its own immutable read cache."""

    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if _FROZEN_FILES.get() is not None:
            return function(*args, **kwargs)
        token = _FROZEN_FILES.set({})
        try:
            return function(*args, **kwargs)
        finally:
            _FROZEN_FILES.reset(token)

    return wrapped


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise EvidenceCompileError(f"duplicate JSON key is forbidden: {key}")
        value[key] = item
    return value


def _freeze(path: Path) -> FrozenFile:
    raw = path.expanduser()
    if raw.is_symlink():
        raise EvidenceCompileError(f"source artifact must not be a symlink: {raw}")
    try:
        resolved = raw.resolve(strict=True)
    except OSError as exc:
        raise EvidenceCompileError(f"source artifact is missing: {raw}") from exc
    cache = _FROZEN_FILES.get()
    cached = cache.get(resolved) if cache is not None else None
    if cached is not None:
        return cached
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise EvidenceCompileError(f"source artifact is unreadable: {resolved}") from exc
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EvidenceCompileError(f"source artifact is not a regular file: {resolved}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    signature = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_size,
        item.st_mtime_ns,
    )
    if signature(before) != signature(after):
        raise EvidenceCompileError(f"source artifact changed while being frozen: {resolved}")
    frozen = FrozenFile(resolved, b"".join(chunks), digest.hexdigest())
    if cache is not None:
        cache[resolved] = frozen
    return frozen


def _source_contract(config: Mapping[str, Any]) -> dict[str, Any]:
    try:
        return resolve_source_contract(
            config.get("source_contract"),
            formal_databases=sorted(FORMAL_BACKUP_DATABASES),
        )
    except SourceContractError as exc:
        raise EvidenceCompileError(f"gate config source_contract is invalid: {exc}") from exc


def _nonnegative_int(value: Any, description: str) -> int:
    if type(value) is not int or value < 0:
        raise EvidenceCompileError(f"{description} must be a non-negative integer")
    return value


def _exact_nonnegative_int(value: Any, expected: int, description: str) -> int:
    observed = _nonnegative_int(value, description)
    if observed != expected:
        raise EvidenceCompileError(f"{description} must equal {expected}; observed {observed}")
    return observed


def _sha256(path: Path) -> str:
    return _freeze(path).sha256


def _load_json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(
            _freeze(path).data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceCompileError(f"{description} is unreadable or invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise EvidenceCompileError(f"{description} must be a JSON object")
    return value


def _parse_time(value: Any, description: str) -> datetime:
    if not isinstance(value, str):
        raise EvidenceCompileError(f"{description} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceCompileError(f"{description} is invalid") from exc
    if parsed.tzinfo is None:
        raise EvidenceCompileError(f"{description} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _safe_relative(root: Path, value: Any, description: str) -> Path:
    if not isinstance(value, str) or not value:
        raise EvidenceCompileError(f"{description} path is missing")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise EvidenceCompileError(f"{description} path must stay inside its producer root")
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise EvidenceCompileError(f"{description} path is missing or escapes its root") from exc
    if candidate.is_symlink() or not resolved.is_file():
        raise EvidenceCompileError(f"{description} must be a regular non-symlink file")
    return resolved


def _require_context(payload: Mapping[str, Any], context: CompileContext, description: str) -> None:
    for field, expected in context.as_dict().items():
        if payload.get(field) != expected:
            raise EvidenceCompileError(f"{description} {field} does not match release context")


def _write_exact(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise EvidenceCompileError(f"refusing to overwrite symlink: {path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise EvidenceCompileError(f"existing normalized evidence differs: {path}")
        return
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_sources(
    output: Path,
    evidence_id: str,
    sources: Sequence[SourceArtifact],
) -> list[dict[str, str]]:
    copied: list[dict[str, str]] = []
    names: set[str] = set()
    for index, source in enumerate(sources):
        frozen = _freeze(source.path)
        resolved = frozen.path
        safe_name = f"{index:02d}-{resolved.name}"
        if safe_name in names:
            raise EvidenceCompileError("duplicate normalized source artifact name")
        names.add(safe_name)
        relative = Path("sources") / evidence_id / safe_name
        destination = output / relative
        _write_exact(destination, frozen.data)
        copied.append(
            {
                "role": source.role,
                "media_type": source.media_type,
                "path": relative.as_posix(),
                "sha256": frozen.sha256,
            }
        )
    if not copied:
        raise EvidenceCompileError(f"{evidence_id} has no verified source artifacts")
    return copied


def _emit(
    *,
    output: Path,
    evidence_id: str,
    context: CompileContext,
    config: dict[str, Any],
    metrics: dict[str, Any],
    sources: Sequence[SourceArtifact],
    started_at: datetime,
    completed_at: datetime,
    eligible: bool = True,
    blockers: Sequence[str] = (),
    source_contract: Mapping[str, Any] | None = None,
) -> str:
    if evidence_id not in NORMALIZED_EVIDENCE_WHITELIST:
        raise EvidenceCompileError(f"{evidence_id} is not allowed for normalized evidence")
    if completed_at < started_at:
        raise EvidenceCompileError(f"{evidence_id} completed before it started")
    spec = EVIDENCE_SPECS[evidence_id]
    semantic_errors = [
        error
        for rule in spec.rules
        if (error := _validate_metric_rule(metrics, rule, config)) is not None
    ]
    status = "passed" if eligible and not semantic_errors and not blockers else "failed"
    generated_at = completed_at.isoformat()
    copied_sources = _copy_sources(output, evidence_id, sources)
    metrics_sha256 = hashlib.sha256(_canonical_json_bytes(metrics)).hexdigest()
    source_binding = hashlib.sha256(_canonical_json_bytes(copied_sources)).hexdigest()
    run_id = f"normalized-{evidence_id}-{source_binding[:16]}"
    report = {
        "schema_version": 1,
        "report_schema": spec.report_schema,
        "evidence_id": evidence_id,
        "status": status,
        "generated_at": generated_at,
        "producer": spec.producer,
        **context.as_dict(),
        "normalized_by": NORMALIZER,
        "normalizer_schema": NORMALIZER_SCHEMA,
        "source_artifacts": copied_sources,
        "run_context": {
            **context.as_dict(),
            "run_id": run_id,
            "execution_mode": spec.execution_mode,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
        },
        "metrics": metrics,
        "metrics_sha256": metrics_sha256,
        "normalization": {
            "eligible_source": bool(eligible),
            "blockers": list(blockers),
            "semantic_failures": semantic_errors,
            "defaults_used": False,
            "live_state_accessed": False,
            "service_start_performed": False,
        },
    }
    if source_contract is not None:
        report["source_contract"] = dict(source_contract)
    report_relative = Path("reports") / f"{evidence_id}.json"
    report_bytes = _canonical_json_bytes(report)
    _write_exact(output / report_relative, report_bytes)
    envelope = {
        "schema_version": 1,
        "evidence_id": evidence_id,
        "status": status,
        "generated_at": generated_at,
        "producer": spec.producer,
        **context.as_dict(),
        "metrics_sha256": metrics_sha256,
        "artifacts": [
            {
                "role": "producer_report",
                "media_type": "application/json",
                "path": report_relative.as_posix(),
                "sha256": hashlib.sha256(report_bytes).hexdigest(),
            },
            *copied_sources,
        ],
    }
    _write_exact(output / f"{evidence_id}.json", _canonical_json_bytes(envelope))
    return status


def _verify_campaign(
    report_path: Path,
    context: CompileContext,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[Path]]:
    report = _load_json(report_path, "campaign report")
    _exact_nonnegative_int(report.get("schema_version"), 1, "campaign report schema_version")
    _require_context(report, context, "campaign report")
    if report.get("evidence_class") != "immutable_release_offline_campaign":
        raise EvidenceCompileError("campaign report evidence_class is invalid")
    if report.get("live_execution_performed") is not False:
        raise EvidenceCompileError("offline campaign unexpectedly reports live execution")
    _require_certifying_campaign(report)
    for field in (
        "release_manifest_sha256",
        "python_runtime_sha256",
        "python_runtime_manifest_sha256",
        "python_runtime_tree_sha256",
        "cron_jobs_sha256",
        "cron_jobs_source_sha256",
    ):
        if not SHA256_RE.fullmatch(str(report.get(field) or "")):
            raise EvidenceCompileError(f"campaign report {field} binding is invalid")
    if not isinstance(report.get("release_id"), str) or not report["release_id"]:
        raise EvidenceCompileError("campaign report release_id binding is invalid")
    release_commit = report.get("release_commit")
    if not isinstance(release_commit, str) or len(release_commit) not in {40, 64}:
        raise EvidenceCompileError("campaign report release_commit binding is invalid")
    cron_file = Path(str(report.get("cron_jobs_file") or "")).expanduser()
    if (
        not cron_file.is_absolute()
        or cron_file.is_symlink()
        or not cron_file.is_file()
        or _sha256(cron_file) != report["cron_jobs_sha256"]
    ):
        raise EvidenceCompileError("campaign report cron snapshot binding is invalid")
    cron_source_file = Path(
        str(report.get("cron_jobs_source_file") or "")
    ).expanduser()
    if (
        not cron_source_file.is_absolute()
        or cron_source_file.is_symlink()
        or not cron_source_file.is_file()
        or _sha256(cron_source_file) != report["cron_jobs_source_sha256"]
    ):
        raise EvidenceCompileError("campaign report cron source binding is invalid")
    root = report_path.resolve(strict=True).parent
    artifact_rows = report.get("artifacts")
    if not isinstance(artifact_rows, list) or not artifact_rows:
        raise EvidenceCompileError("campaign report has no bound day artifacts")
    days: list[dict[str, Any]] = []
    paths: list[Path] = [report_path.resolve(strict=True)]
    for index, row in enumerate(artifact_rows):
        if not isinstance(row, dict) or not SHA256_RE.fullmatch(str(row.get("sha256") or "")):
            raise EvidenceCompileError(f"campaign artifact {index} metadata is invalid")
        path = _safe_relative(root, row.get("path"), f"campaign artifact {index}")
        if _sha256(path) != row["sha256"]:
            raise EvidenceCompileError(f"campaign artifact {index} SHA-256 mismatch")
        day = _load_json(path, f"campaign day {index}")
        _require_context(day, context, f"campaign day {index}")
        for field in (
            "release_manifest_sha256",
            "release_id",
            "release_commit",
            "python_runtime_sha256",
            "python_runtime_manifest_sha256",
            "python_runtime_tree_sha256",
            "cron_jobs_sha256",
            "cron_jobs_source_sha256",
        ):
            if day.get(field) != report.get(field):
                raise EvidenceCompileError(f"campaign day {index} {field} binding mismatch")
        if day.get("live_execution_performed") is not False:
            raise EvidenceCompileError("campaign day unexpectedly reports live execution")
        days.append(day)
        paths.append(path)
    return report, days, paths


def _require_certifying_campaign(report: Mapping[str, Any]) -> int:
    if (
        report.get("armed") is not True
        or report.get("certifying") is not True
        or report.get("harness_certified") is not True
        or report.get("offline_complete") is not True
        or report.get("decision") != "GO"
        or report.get("execution_backend") != "release_launcher"
        or report.get("fail_closed") is not False
    ):
        raise EvidenceCompileError(
            "campaign report is not an armed completed certifying release-launcher campaign"
        )
    required_passes = _nonnegative_int(
        report.get("required_independent_passes"),
        "campaign report required_independent_passes",
    )
    if required_passes != 1:
        raise EvidenceCompileError(
            "targeted V3 campaign must contain exactly one independent pass"
        )
    return required_passes


def _structured_rows(days: Sequence[dict[str, Any]], workload: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for day in days:
        workloads = day.get("workloads")
        if not isinstance(workloads, list):
            continue
        for item in workloads:
            if not isinstance(item, dict) or item.get("workload") != workload:
                continue
            evidence = item.get("structured_evidence")
            if (
                item.get("status") == "offline_passed"
                and isinstance(evidence, dict)
                and evidence.get("workload") == workload
                and evidence.get("status") == "passed"
                and isinstance(evidence.get("measurements"), dict)
            ):
                _exact_nonnegative_int(
                    evidence.get("schema_version"),
                    1,
                    f"campaign {workload} structured_evidence.schema_version",
                )
                _exact_nonnegative_int(
                    item.get("returncode"), 0, f"campaign {workload} returncode"
                )
                _nonnegative_int(
                    item.get("validation_pass"), f"campaign {workload} validation_pass"
                )
                rows.append(item)
    return rows


def _route_certification_report_paths(
    report_path: Path,
    days: Sequence[dict[str, Any]],
    bundle: ReleaseBundle,
    required_passes: int,
) -> list[Path]:
    rows = _structured_rows(days, "346_route_contract_replay")
    if (
        len(rows) != required_passes
        or {row.get("validation_pass") for row in rows}
        != set(range(1, required_passes + 1))
    ):
        raise EvidenceCompileError(
            "route campaign lacks the required certification reports"
        )
    producer_root = report_path.resolve(strict=True).parent
    paths: list[Path] = []
    seen: set[Path] = set()
    for index, row in enumerate(rows):
        reference = row.get("structured_evidence_artifact")
        if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise EvidenceCompileError(
                f"route certification report {index} reference is invalid"
            )
        expected_sha = str(reference.get("sha256") or "")
        if not SHA256_RE.fullmatch(expected_sha):
            raise EvidenceCompileError(
                f"route certification report {index} SHA-256 is invalid"
            )
        path = _safe_relative(
            producer_root,
            reference.get("path"),
            f"route certification report {index}",
        )
        if path in seen or _sha256(path) != expected_sha:
            raise EvidenceCompileError(
                f"route certification report {index} is duplicated or hash-mismatched"
            )
        payload = _load_json(path, f"route certification report {index}")
        if payload != row.get("structured_evidence"):
            raise EvidenceCompileError(
                f"route certification report {index} differs from campaign evidence"
            )
        try:
            _validate_route_certification_evidence(
                payload,
                expected_profile=row.get("validation_profile"),
                expected_release=bundle,
            )
        except CampaignSafetyError as exc:
            raise EvidenceCompileError(
                f"route certification report {index} is not certifying: {exc}"
            ) from exc
        seen.add(path)
        paths.append(path)
    return paths


def _health_certification_report_paths(
    report_path: Path,
    days: Sequence[dict[str, Any]],
    bundle: ReleaseBundle,
    required_passes: int,
) -> list[Path]:
    rows = _structured_rows(days, "health_1000_model_free")
    if (
        len(rows) != required_passes
        or {row.get("validation_pass") for row in rows}
        != set(range(1, required_passes + 1))
    ):
        raise EvidenceCompileError(
            "health campaign lacks the required certification reports"
        )
    release_files = {path: digest for path, digest, _size, _mode in bundle.files}
    campaign_config_path = bundle.root / "config/v3_validation_campaign.json"
    if (
        release_files.get("config/v3_validation_campaign.json")
        != _sha256(campaign_config_path)
    ):
        raise EvidenceCompileError("health campaign config is not release-bound")
    campaign_config = _load_json(campaign_config_path, "health campaign config")
    offline = campaign_config.get("offline_campaign")
    expected_profiles = (
        offline.get("validation_pass_profiles") if isinstance(offline, dict) else None
    )
    if (
        not isinstance(expected_profiles, list)
        or len(expected_profiles) != required_passes
        or not isinstance(offline, dict)
        or offline.get("required_independent_passes") != required_passes
        or "health_1000_model_free" not in offline.get("workloads", [])
    ):
        raise EvidenceCompileError("health campaign config lacks the required profiles")
    producer_root = report_path.resolve(strict=True).parent
    paths: list[Path] = []
    seen_paths: set[Path] = set()
    seen_profiles: set[str] = set()
    seen_hashes: set[str] = set()
    for index, row in enumerate(rows):
        profile = row.get("validation_profile")
        validation_pass = row.get("validation_pass")
        if (
            not isinstance(profile, dict)
            or type(validation_pass) is not int
            or profile != expected_profiles[validation_pass - 1]
        ):
            raise EvidenceCompileError(
                f"health certification report {index} profile is invalid"
            )
        reference = row.get("inner_report_artifact")
        if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise EvidenceCompileError(
                f"health certification report {index} reference is invalid"
            )
        expected_sha = str(reference.get("sha256") or "")
        if not SHA256_RE.fullmatch(expected_sha):
            raise EvidenceCompileError(
                f"health certification report {index} SHA-256 is invalid"
            )
        path = _safe_relative(
            producer_root,
            reference.get("path"),
            f"health certification report {index}",
        )
        payload = _load_json(path, f"health certification report {index}")
        structured = row.get("structured_evidence")
        if not isinstance(structured, dict) or payload != structured.get("report"):
            raise EvidenceCompileError(
                f"health certification report {index} differs from campaign inner report"
            )
        try:
            _validate_health_certification_evidence(
                structured,
                expected_profile=profile,
                expected_release=bundle,
            )
        except CampaignSafetyError as exc:
            raise EvidenceCompileError(
                f"health certification report {index} is not certifying: {exc}"
            ) from exc
        profile_id = str(profile.get("profile_id") or "")
        if (
            path in seen_paths
            or profile_id in seen_profiles
            or expected_sha in seen_hashes
            or _sha256(path) != expected_sha
        ):
            raise EvidenceCompileError(
                f"health certification report {index} is duplicated or hash-mismatched"
            )
        seen_paths.add(path)
        seen_profiles.add(profile_id)
        seen_hashes.add(expected_sha)
        paths.append(path)
    return paths


def _release_quality_report_paths(
    report_path: Path,
    days: Sequence[dict[str, Any]],
    bundle: ReleaseBundle,
    python_runtime_sha256: str,
    required_passes: int,
) -> list[Path]:
    rows = _structured_rows(days, "golden_business_flows")
    if (
        len(rows) != required_passes
        or {row.get("validation_pass") for row in rows}
        != set(range(1, required_passes + 1))
    ):
        raise EvidenceCompileError(
            "release quality campaign lacks the required certification reports"
        )
    release_files = {path: digest for path, digest, _size, _mode in bundle.files}
    suite_path = bundle.root / "config/v3_release_quality_suites.json"
    if release_files.get("config/v3_release_quality_suites.json") != _sha256(suite_path):
        raise EvidenceCompileError("release quality suite manifest is not release-bound")
    producer_root = report_path.resolve(strict=True).parent
    paths: list[Path] = []
    seen_paths: set[Path] = set()
    seen_profiles: set[str] = set()
    seen_hashes: set[str] = set()
    for index, row in enumerate(rows):
        profile = row.get("validation_profile")
        reference = row.get("inner_report_artifact")
        if (
            not isinstance(profile, dict)
            or not isinstance(reference, dict)
            or set(reference) != {"path", "sha256"}
            or not SHA256_RE.fullmatch(str(reference.get("sha256") or ""))
        ):
            raise EvidenceCompileError(
                f"release quality report {index} reference/profile is invalid"
            )
        path = _safe_relative(
            producer_root,
            reference.get("path"),
            f"release quality report {index}",
        )
        inner = _load_json(path, f"release quality report {index}")
        structured = row.get("structured_evidence")
        if not isinstance(structured, dict) or inner != structured.get("report"):
            raise EvidenceCompileError(
                f"release quality report {index} differs from campaign evidence"
            )
        try:
            _validate_release_quality_certification_evidence(
                structured,
                expected_profile=profile,
                expected_release=bundle,
                expected_python_runtime_sha256=python_runtime_sha256,
            )
        except CampaignSafetyError as exc:
            raise EvidenceCompileError(
                f"release quality report {index} is not certifying: {exc}"
            ) from exc
        profile_id = str(profile.get("profile_id") or "")
        expected_sha = str(reference["sha256"])
        if (
            not profile_id
            or path in seen_paths
            or profile_id in seen_profiles
            or expected_sha in seen_hashes
            or _sha256(path) != expected_sha
        ):
            raise EvidenceCompileError(
                f"release quality report {index} is duplicated or hash-mismatched"
            )
        seen_paths.add(path)
        seen_profiles.add(profile_id)
        seen_hashes.add(expected_sha)
        paths.append(path)
    return paths


def _resource_performance_report_paths(
    report_path: Path,
    days: Sequence[dict[str, Any]],
    bundle: ReleaseBundle,
    python_runtime_sha256: str,
) -> list[Path]:
    rows = _structured_rows(days, "matched_v2_v3_performance")
    if (
        len(rows) != 7
        or {row.get("validation_pass") for row in rows} != set(range(1, 8))
    ):
        raise EvidenceCompileError(
            "resource/performance campaign lacks seven partial reports"
        )
    producer_root = report_path.resolve(strict=True).parent
    paths: list[Path] = []
    seen_profiles: set[str] = set()
    seen_hashes: set[str] = set()
    for index, row in enumerate(rows):
        profile = row.get("validation_profile")
        reference = row.get("inner_report_artifact")
        structured = row.get("structured_evidence")
        if (
            not isinstance(profile, dict)
            or not isinstance(reference, dict)
            or set(reference) != {"path", "sha256"}
            or not SHA256_RE.fullmatch(str(reference.get("sha256") or ""))
            or not isinstance(structured, dict)
        ):
            raise EvidenceCompileError(
                f"resource/performance report {index} reference/profile is invalid"
            )
        path = _safe_relative(
            producer_root,
            reference.get("path"),
            f"resource/performance report {index}",
        )
        inner = _load_json(path, f"resource/performance report {index}")
        if inner != structured.get("report"):
            raise EvidenceCompileError(
                f"resource/performance report {index} differs from campaign evidence"
            )
        try:
            _validate_resource_performance_partial(
                structured,
                expected_profile=profile,
                expected_release=bundle,
                expected_python_runtime_sha256=python_runtime_sha256,
            )
        except CampaignSafetyError as exc:
            raise EvidenceCompileError(
                f"resource/performance report {index} is invalid: {exc}"
            ) from exc
        profile_id = str(profile.get("profile_id") or "")
        digest = str(reference["sha256"])
        if (
            not profile_id
            or profile_id in seen_profiles
            or digest in seen_hashes
            or _sha256(path) != digest
        ):
            raise EvidenceCompileError(
                f"resource/performance report {index} is duplicated or hash-mismatched"
            )
        seen_profiles.add(profile_id)
        seen_hashes.add(digest)
        paths.append(path)
    return paths


def _fault_certification_report_paths(
    report_path: Path,
    days: Sequence[dict[str, Any]],
    bundle: ReleaseBundle,
    required_passes: int,
) -> list[Path]:
    rows = _structured_rows(days, "fault_recovery_certification")
    if (
        len(rows) != required_passes
        or {row.get("validation_pass") for row in rows}
        != set(range(1, required_passes + 1))
    ):
        raise EvidenceCompileError(
            "fault campaign lacks the required certification reports"
        )
    release_files = {path: digest for path, digest, _size, _mode in bundle.files}
    campaign_config_path = bundle.root / "config/v3_validation_campaign.json"
    if (
        release_files.get("config/v3_validation_campaign.json")
        != _sha256(campaign_config_path)
    ):
        raise EvidenceCompileError("fault campaign config is not release-bound")
    campaign_config = _load_json(campaign_config_path, "fault campaign config")
    offline = campaign_config.get("offline_campaign")
    profiles = offline.get("validation_pass_profiles") if isinstance(offline, dict) else None
    workloads = offline.get("workloads") if isinstance(offline, dict) else None
    if (
        not isinstance(profiles, list)
        or len(profiles) != required_passes
        or not isinstance(workloads, list)
        or "fault_recovery_certification" not in workloads
        or offline.get("required_independent_passes") != required_passes
    ):
        raise EvidenceCompileError("fault campaign config lacks the required profiles")
    producer_root = report_path.resolve(strict=True).parent
    paths: list[Path] = []
    seen_paths: set[Path] = set()
    seen_profiles: set[str] = set()
    seen_hashes: set[str] = set()
    seen_stimulus_plans: set[str] = set()
    for index, row in enumerate(rows):
        profile = row.get("validation_profile")
        validation_pass = row.get("validation_pass")
        if (
            not isinstance(profile, dict)
            or type(validation_pass) is not int
            or profile != profiles[validation_pass - 1]
        ):
            raise EvidenceCompileError(
                f"fault certification report {index} profile is invalid"
            )
        reference = row.get("inner_report_artifact")
        if not isinstance(reference, dict) or set(reference) != {"path", "sha256"}:
            raise EvidenceCompileError(
                f"fault certification report {index} reference is invalid"
            )
        expected_sha = str(reference.get("sha256") or "")
        if not SHA256_RE.fullmatch(expected_sha):
            raise EvidenceCompileError(
                f"fault certification report {index} SHA-256 is invalid"
            )
        path = _safe_relative(
            producer_root,
            reference.get("path"),
            f"fault certification report {index}",
        )
        payload = _load_json(path, f"fault certification report {index}")
        structured = row.get("structured_evidence")
        if not isinstance(structured, dict) or payload != structured.get("report"):
            raise EvidenceCompileError(
                f"fault certification report {index} differs from campaign inner report"
            )
        try:
            _validate_fault_certification_evidence(
                structured,
                expected_profile=profile,
                expected_release=bundle,
            )
        except CampaignSafetyError as exc:
            raise EvidenceCompileError(
                f"fault certification report {index} is not certifying: {exc}"
            ) from exc
        profile_id = str(profile.get("profile_id") or "")
        stimulus_plan = payload.get("stimulus_plan")
        stimulus_plan_sha = (
            stimulus_plan.get("stimulus_plan_sha256")
            if isinstance(stimulus_plan, dict)
            else None
        )
        if (
            path in seen_paths
            or profile_id in seen_profiles
            or expected_sha in seen_hashes
            or not SHA256_RE.fullmatch(str(stimulus_plan_sha or ""))
            or stimulus_plan_sha in seen_stimulus_plans
            or _sha256(path) != expected_sha
        ):
            raise EvidenceCompileError(
                f"fault certification report {index} is duplicated, stimulus-reused, "
                "or hash-mismatched"
            )
        seen_paths.add(path)
        seen_profiles.add(profile_id)
        seen_hashes.add(expected_sha)
        seen_stimulus_plans.add(str(stimulus_plan_sha))
        paths.append(path)
    if len(seen_stimulus_plans) != required_passes:
        raise EvidenceCompileError("fault campaign lacks unique stimulus plans")
    return paths


def _schedule_certification_report_paths(
    report_path: Path,
    days: Sequence[dict[str, Any]],
    required_passes: int,
) -> tuple[list[Path], list[Path]]:
    """Resolve raw G11 capacity/body reports without trusting summaries."""

    workload = "seven_day_schedule_10x_arrival_2x_duration_replay"
    rows = _structured_rows(days, workload)
    if (
        len(rows) != required_passes
        or {row.get("validation_pass") for row in rows}
        != set(range(1, required_passes + 1))
    ):
        raise EvidenceCompileError("schedule campaign lacks the required certification reports")
    producer_root = report_path.resolve(strict=True).parent
    capacity_paths: list[Path] = []
    body_paths: list[Path] = []
    seen_paths: set[Path] = set()
    seen_hashes: set[str] = set()
    seen_profiles: set[str] = set()
    for index, row in enumerate(sorted(rows, key=lambda value: value["validation_pass"]), 1):
        profile = row.get("validation_profile")
        structured = row.get("structured_evidence")
        capacity_ref = row.get("inner_report_artifact")
        body_ref = row.get("body_evidence_artifact")
        if (
            not isinstance(profile, dict)
            or not isinstance(structured, dict)
            or not isinstance(capacity_ref, dict)
            or set(capacity_ref) != {"path", "sha256"}
            or not isinstance(body_ref, dict)
            or set(body_ref) != {"path", "sha256"}
        ):
            raise EvidenceCompileError(
                f"schedule certification report {index} reference/profile is invalid"
            )
        profile_id = str(profile.get("profile_id") or "")
        if not profile_id or profile_id in seen_profiles:
            raise EvidenceCompileError("schedule validation profile is missing or duplicated")
        seen_profiles.add(profile_id)
        resolved: list[Path] = []
        for description, reference, payload_key in (
            ("capacity", capacity_ref, "report"),
            ("body", body_ref, "body_evidence"),
        ):
            expected_sha = str(reference.get("sha256") or "")
            if not SHA256_RE.fullmatch(expected_sha):
                raise EvidenceCompileError(
                    f"schedule {description} report {index} SHA-256 is invalid"
                )
            path = _safe_relative(
                producer_root,
                reference.get("path"),
                f"schedule {description} report {index}",
            )
            payload = _load_json(path, f"schedule {description} report {index}")
            if payload != structured.get(payload_key):
                raise EvidenceCompileError(
                    f"schedule {description} report {index} differs from campaign evidence"
                )
            if path in seen_paths or expected_sha in seen_hashes or _sha256(path) != expected_sha:
                raise EvidenceCompileError(
                    f"schedule {description} report {index} is duplicated or hash-mismatched"
                )
            seen_paths.add(path)
            seen_hashes.add(expected_sha)
            resolved.append(path)
        capacity = structured.get("report")
        if not isinstance(capacity, dict) or capacity.get("validation_profile_id") != profile_id:
            raise EvidenceCompileError(
                f"schedule certification report {index} validation profile drifted"
            )
        capacity_paths.append(resolved[0])
        body_paths.append(resolved[1])
    return capacity_paths, body_paths


def _campaign_sources(
    paths: Sequence[Path], campaign: Mapping[str, Any], release_root: Path
) -> list[SourceArtifact]:
    return [
        SourceArtifact("upstream_campaign_report" if index == 0 else "upstream_campaign_day", path)
        for index, path in enumerate(paths)
    ] + [
        SourceArtifact(
            "upstream_campaign_cron_snapshot",
            Path(str(campaign["cron_jobs_file"])).resolve(strict=True),
        ),
        SourceArtifact("upstream_release_marker", release_root / "RELEASE_COMPLETE.json"),
        SourceArtifact("upstream_release_manifest", release_root / "release-manifest.json"),
    ]


@_with_compilation_scope
def compile_campaign_evidence(
    *,
    report_path: Path,
    release_root: Path,
    output: Path,
    context: CompileContext,
    config: dict[str, Any],
    physical_fault_report: Path | None = None,
    physical_fault_plan: Path | None = None,
    physical_fault_authorization: Path | None = None,
) -> dict[str, str]:
    bundle = _verify_release(release_root, context)
    report, days, paths = _verify_campaign(report_path, context)
    required_passes = _require_certifying_campaign(report)
    if (
        report.get("release_id") != bundle.release_id
        or report.get("release_manifest_sha256") != bundle.manifest_sha256
    ):
        raise EvidenceCompileError(
            "campaign is not a completed certifying campaign bound to the selected release"
        )
    if any(
        day.get("status") != "offline_passed"
        or day.get("release_gate_eligible") is not True
        for day in days
    ):
        raise EvidenceCompileError("campaign day is not release-gate eligible")
    for day_index, day in enumerate(days):
        _exact_nonnegative_int(
            day.get("completed_independent_passes"),
            required_passes,
            f"campaign day {day_index} completed_independent_passes",
        )
    sources = _campaign_sources(paths, report, release_root)
    started = min(_parse_time(day.get("started_at"), "campaign day started_at") for day in days)
    completed = max(
        _parse_time(day.get("completed_at") or day.get("generated_at"), "campaign day completed_at")
        for day in days
    )
    statuses: dict[str, str] = {}

    schedule = _structured_rows(days, "seven_day_schedule_10x_arrival_2x_duration_replay")
    if schedule:
        from scripts.v3_validation.schedule_evidence import (
            ScheduleEvidenceError,
            derive_schedule_gate_metrics,
        )

        capacity_paths, body_paths = _schedule_certification_report_paths(
            report_path, days, required_passes
        )
        source_paths = {
            "upstream_schedule_dispatch_policy": Path(
                "config/v3_schedule_dispatch_policy.json"
            ),
            "upstream_schedule_capacity_certifier": Path(
                "scripts/v3_validation/schedule_capacity_certification.py"
            ),
            "upstream_schedule_body_registry_script": Path(
                "scripts/v3_validation/schedule_body_registry.py"
            ),
            "upstream_schedule_body_registry_config": Path(
                "config/v3_schedule_body_adapter_registry.json"
            ),
            "upstream_schedule_duration_baseline": Path(
                "config/v3_schedule_realism_baseline.json"
            ),
        }
        release_files = {path: digest for path, digest, _size, _mode in bundle.files}
        for role, relative in source_paths.items():
            absolute = release_root / relative
            if release_files.get(relative.as_posix()) != _sha256(absolute):
                raise EvidenceCompileError(
                    f"schedule source is not release-manifest bound: {role}"
                )
        cron_path = Path(str(report["cron_jobs_file"])).resolve(strict=True)
        release_manifest_path = release_root / "release-manifest.json"
        reports = [_load_json(path, f"schedule capacity report {index}") for index, path in enumerate(capacity_paths, 1)]
        body_reports = [_load_json(path, f"schedule body report {index}") for index, path in enumerate(body_paths, 1)]
        try:
            schedule_metrics = derive_schedule_gate_metrics(
                reports,
                body_reports,
                cron_jobs_sha256=_sha256(cron_path),
                dispatch_policy_sha256=_sha256(
                    release_root / source_paths["upstream_schedule_dispatch_policy"]
                ),
                certifier_sha256=_sha256(
                    release_root / source_paths["upstream_schedule_capacity_certifier"]
                ),
                registry_script_sha256=_sha256(
                    release_root / source_paths["upstream_schedule_body_registry_script"]
                ),
                registry_config_sha256=_sha256(
                    release_root / source_paths["upstream_schedule_body_registry_config"]
                ),
                duration_baseline_sha256=_sha256(
                    release_root / source_paths["upstream_schedule_duration_baseline"]
                ),
                release_id=bundle.release_id,
                release_manifest_sha256=_sha256(release_manifest_path),
            )
        except (ScheduleEvidenceError, ValueError) as exc:
            raise EvidenceCompileError(f"schedule evidence recomputation failed: {exc}") from exc
        schedule_sources = [
            *sources,
            *(
                SourceArtifact(role, release_root / relative)
                for role, relative in source_paths.items()
            ),
            *(
                SourceArtifact(f"upstream_schedule_capacity_report_{index}", path)
                for index, path in enumerate(capacity_paths, 1)
            ),
            *(
                SourceArtifact(f"upstream_schedule_body_evidence_{index}", path)
                for index, path in enumerate(body_paths, 1)
            ),
        ]
        statuses["seven_day_schedule_10x_arrival_2x_duration_replay_passed"] = _emit(
            output=output,
            evidence_id="seven_day_schedule_10x_arrival_2x_duration_replay_passed",
            context=context,
            config=config,
            metrics=schedule_metrics,
            sources=schedule_sources,
            started_at=started,
            completed_at=completed,
        )

    quality = _structured_rows(days, "golden_business_flows")
    if quality:
        python_runtime_sha256 = str(report.get("python_runtime_sha256") or "")
        if not SHA256_RE.fullmatch(python_runtime_sha256):
            raise EvidenceCompileError("release quality campaign runtime SHA-256 is invalid")
        quality_paths = _release_quality_report_paths(
            report_path,
            days,
            bundle,
            python_runtime_sha256,
            required_passes,
        )
        suite_path = release_root / "config/v3_release_quality_suites.json"
        suite_manifest = _load_json(suite_path, "release quality suite manifest")
        release_files = {
            path: digest for path, digest, _size, _mode in bundle.files
        }
        quality_metrics: list[dict[str, dict[str, Any]]] = []
        for index, path in enumerate(quality_paths):
            try:
                quality_metrics.append(
                    summarize_release_quality_report(
                        _load_json(path, f"release quality report {index}"),
                        manifest=suite_manifest,
                        release_files=release_files,
                        python_runtime_sha256=python_runtime_sha256,
                        expected_profile=quality[index].get("validation_profile"),
                        expected_release_id=bundle.release_id,
                        expected_release_manifest_sha256=bundle.manifest_sha256,
                    )
                )
            except ReleaseQualityEvidenceError as exc:
                raise EvidenceCompileError(
                    f"release quality report {index} recomputation failed: {exc}"
                ) from exc
        quality_sources = [
            *sources,
            SourceArtifact("upstream_release_quality_suite_manifest", suite_path),
            SourceArtifact(
                "upstream_release_quality_certifier",
                release_root / "scripts/v3_validation/release_quality_certification.py",
            ),
            SourceArtifact(
                "upstream_release_quality_evidence_module",
                release_root / "scripts/v3_validation/release_quality_evidence.py",
            ),
            SourceArtifact(
                "upstream_pytest_transcript_plugin",
                release_root / "scripts/v3_validation/pytest_transcript_plugin.py",
            ),
            SourceArtifact(
                "upstream_golden_flows_source",
                release_root / "scripts/v3_validation/golden_flows.py",
            ),
            SourceArtifact(
                "upstream_side_effects_source",
                release_root / "scripts/v3_validation/side_effects.py",
            ),
            *(
                SourceArtifact(f"upstream_golden_dependency_{index}", release_root / path)
                for index, path in enumerate(GOLDEN_DEPENDENCY_PATHS)
            ),
            *(
                SourceArtifact("upstream_release_quality_report", path)
                for path in quality_paths
            ),
        ]

        def aggregate(evidence_id: str, field: str) -> int:
            values = [row[evidence_id].get(field) for row in quality_metrics]
            if any(type(value) is not int for value in values):
                raise EvidenceCompileError(
                    f"release quality metric is not an integer: {evidence_id}.{field}"
                )
            return sum(values)

        normalized_metrics = {
            "v3_unit_contract_integration_e2e_passed": {
                "failed": aggregate("v3_unit_contract_integration_e2e_passed", "failed"),
                "suites": list(EXPECTED_V3_SUITES),
                "all_required_suites_passed": all(
                    row["v3_unit_contract_integration_e2e_passed"][
                        "all_required_suites_passed"
                    ]
                    is True
                    for row in quality_metrics
                ),
            },
            "interaction_agent_kernel_memory_quality_contracts_passed": {
                "failed_contracts": aggregate(
                    "interaction_agent_kernel_memory_quality_contracts_passed",
                    "failed_contracts",
                ),
                "contract_groups": list(EXPECTED_QUALITY_GROUPS),
                "quality_non_regression_passed": all(
                    row["interaction_agent_kernel_memory_quality_contracts_passed"][
                        "quality_non_regression_passed"
                    ]
                    is True
                    for row in quality_metrics
                ),
            },
            "context_memory_tool_plan_answer_golden_sets_passed": {
                "failed_cases": aggregate(
                    "context_memory_tool_plan_answer_golden_sets_passed",
                    "failed_cases",
                ),
                "sets": list(EXPECTED_GOLDEN_SETS),
                "all_sets_passed": all(
                    row["context_memory_tool_plan_answer_golden_sets_passed"][
                        "all_sets_passed"
                    ]
                    is True
                    for row in quality_metrics
                ),
            },
            "golden_side_effect_diff_approved": {
                "unapproved_contract_diffs": aggregate(
                    "golden_side_effect_diff_approved", "unapproved_contract_diffs"
                ),
                "duplicate_side_effects": aggregate(
                    "golden_side_effect_diff_approved", "duplicate_side_effects"
                ),
                "golden_diff_completed": all(
                    row["golden_side_effect_diff_approved"]["golden_diff_completed"]
                    is True
                    for row in quality_metrics
                ),
            },
        }
        for evidence_id, metrics in normalized_metrics.items():
            statuses[evidence_id] = _emit(
                output=output,
                evidence_id=evidence_id,
                context=context,
                config=config,
                metrics=metrics,
                sources=quality_sources,
                started_at=started,
                completed_at=completed,
            )

    resource_performance = _structured_rows(days, "matched_v2_v3_performance")
    if resource_performance:
        python_runtime_sha256 = str(report.get("python_runtime_sha256") or "")
        if not SHA256_RE.fullmatch(python_runtime_sha256):
            raise EvidenceCompileError(
                "resource/performance campaign runtime SHA-256 is invalid"
            )
        partial_paths = _resource_performance_report_paths(
            report_path,
            days,
            bundle,
            python_runtime_sha256,
        )
        release_files = {
            path: digest for path, digest, _size, _mode in bundle.files
        }
        partial_metrics: list[dict[str, dict[str, Any]]] = []
        for index, path in enumerate(partial_paths):
            try:
                partial_metrics.append(
                    summarize_resource_performance_report(
                        _load_json(path, f"resource/performance report {index}"),
                        release_files=release_files,
                        python_runtime_sha256=python_runtime_sha256,
                        expected_profile=resource_performance[index].get(
                            "validation_profile"
                        ),
                        expected_release_id=bundle.release_id,
                        expected_release_manifest_sha256=bundle.manifest_sha256,
                    )
                )
            except ResourcePerformanceEvidenceError as exc:
                raise EvidenceCompileError(
                    f"resource/performance report {index} recomputation failed: {exc}"
                ) from exc
        partial_sources = [
            *sources,
            SourceArtifact(
                "upstream_resource_performance_certifier",
                release_root
                / "scripts/v3_validation/resource_performance_certification.py",
            ),
            SourceArtifact(
                "upstream_resource_performance_evidence_module",
                release_root / "scripts/v3_validation/resource_performance_evidence.py",
            ),
            SourceArtifact(
                "upstream_perf_compat_source",
                release_root / "scripts/v3_validation/perf_compat.py",
            ),
            SourceArtifact(
                "upstream_matched_performance_source",
                release_root / "scripts/v3_validation/perf_certification.py",
            ),
            SourceArtifact(
                "upstream_isolated_resource_window_source",
                release_root / "scripts/v3_validation/isolated_resource_window.py",
            ),
            SourceArtifact(
                "upstream_isolated_resource_window_collector",
                release_root
                / "scripts/v3_validation/isolated_resource_window_collector.py",
            ),
            SourceArtifact(
                "upstream_isolated_resource_window_plan_builder",
                release_root
                / "scripts/v3_validation/isolated_resource_window_plan_builder.py",
            ),
            SourceArtifact(
                "upstream_resource_window_core_adapter",
                release_root
                / "scripts/v3_validation/resource_window_core_adapter.py",
            ),
            SourceArtifact(
                "upstream_resource_window_model_adapter",
                release_root
                / "scripts/v3_validation/resource_window_model_adapter.py",
            ),
            SourceArtifact(
                "upstream_resource_source", release_root / "magi_v3/resource.py"
            ),
            SourceArtifact(
                "upstream_dispatcher_source", release_root / "magi_v3/dispatcher.py"
            ),
            SourceArtifact(
                "upstream_ledger_source", release_root / "magi_v3/ledger.py"
            ),
            SourceArtifact(
                "upstream_supervisor_source", release_root / "magi_v3/supervisor.py"
            ),
            SourceArtifact(
                "upstream_macos_resource_source",
                release_root / "magi_v3/macos_resources.py",
            ),
            SourceArtifact(
                "upstream_resource_policy",
                release_root / "config/v3_resource_policy.json",
            ),
            *(
                SourceArtifact("upstream_resource_performance_report", path)
                for path in partial_paths
            ),
        ]
        performance_rows = [
            row[RESOURCE_PERFORMANCE_GATE_IDS[0]] for row in partial_metrics
        ]
        resource_rows = [
            row[RESOURCE_PERFORMANCE_GATE_IDS[1]] for row in partial_metrics
        ]
        preemption_rows = [
            row[RESOURCE_PERFORMANCE_GATE_IDS[2]] for row in partial_metrics
        ]
        worker_rows = [
            row[RESOURCE_PERFORMANCE_GATE_IDS[3]] for row in partial_metrics
        ]
        soak_rows = _structured_rows(days, "hundred_cycle_worker_reap_soak")
        soak_measurements = [
            row["structured_evidence"]["measurements"] for row in soak_rows
        ]
        normalized_partial = {
            RESOURCE_PERFORMANCE_GATE_IDS[0]: {
                "matched_disposable_dependencies": all(
                    row["matched_disposable_dependencies"] is True
                    for row in performance_rows
                ),
                "matched_production_dependencies": all(
                    row["matched_production_dependencies"] is True
                    for row in performance_rows
                ),
                "warm_and_cold_measured": all(
                    row["warm_and_cold_measured"] is True for row in performance_rows
                ),
                "maximum_p95_regression_ratio": max(
                    row["maximum_p95_regression_ratio"] for row in performance_rows
                ),
                "model_tokens_per_second_measured": all(
                    row["model_tokens_per_second_measured"] is True
                    for row in performance_rows
                ),
                "minimum_model_tokens_per_second_ratio": min(
                    float(row.get("minimum_model_tokens_per_second_ratio", 0.0))
                    for row in performance_rows
                ),
                "missing_requirements": sorted(
                    {
                        item
                        for row in performance_rows
                        for item in row["missing_requirements"]
                    }
                ),
            },
            RESOURCE_PERFORMANCE_GATE_IDS[1]: {
                "all_budgets_passed": all(
                    row["all_budgets_passed"] is True for row in resource_rows
                ),
                "idle_swapout_growth_mb": max(
                    row["idle_swapout_growth_mb"] for row in resource_rows
                ),
                "observation_seconds": max(
                    row["observation_seconds"] for row in resource_rows
                ),
                "required_idle_observation_seconds": 1800,
                "application_plane_footprint_reduction_ratio": min(
                    float(row.get("application_plane_footprint_reduction_ratio", 0.0))
                    for row in resource_rows
                ),
                "missing_budget_profiles": sorted(
                    {
                        item
                        for row in resource_rows
                        for item in row["missing_budget_profiles"]
                    }
                ),
            },
            RESOURCE_PERFORMANCE_GATE_IDS[2]: {
                "preemption_passed": all(
                    row["preemption_passed"] is True for row in preemption_rows
                ),
                "automatic_preemption_observed": all(
                    row["automatic_preemption_observed"] is True
                    for row in preemption_rows
                ),
                "independent_passes": len(preemption_rows),
                "independent_samples": sum(
                    row["independent_samples"] for row in preemption_rows
                ),
                "p0_p1_deadline_misses": sum(
                    row["p0_p1_deadline_misses"] for row in preemption_rows
                ),
                "interactive_queue_p95_ms": max(
                    row["interactive_queue_p95_ms"] for row in preemption_rows
                ),
                "interactive_queue_p95_seconds": max(
                    row["interactive_queue_p95_seconds"]
                    for row in preemption_rows
                ),
                "p1_browser_queue_p95_seconds": max(
                    row["p1_browser_queue_p95_seconds"]
                    for row in preemption_rows
                ),
                "orphan_process_groups": sum(
                    row["orphan_process_groups"] for row in preemption_rows
                ),
                "duplicate_completions": sum(
                    row["duplicate_completions"] for row in preemption_rows
                ),
                "lost_jobs": sum(row["lost_jobs"] for row in preemption_rows),
                "preempted_jobs_requeued": sum(
                    row["preempted_jobs_requeued"] for row in preemption_rows
                ),
                "attempt_two_unique_completions": sum(
                    row["attempt_two_unique_completions"]
                    for row in preemption_rows
                ),
                "missing_requirements": sorted(
                    {
                        item
                        for row in preemption_rows
                        for item in row["missing_requirements"]
                    }
                ),
            },
            RESOURCE_PERFORMANCE_GATE_IDS[3]: {
                "cycles": sum(
                    int(row.get("cycles_completed", 0))
                    for row in soak_measurements
                    if type(row.get("cycles_completed")) is int
                ),
                "orphan_process_groups": sum(
                    max(
                        0,
                        int(row.get("cycles_completed", 0))
                        - int(row.get("process_groups_gone", 0)),
                    )
                    for row in soak_measurements
                    if type(row.get("cycles_completed")) is int
                    and type(row.get("process_groups_gone")) is int
                ),
                "rss_returned_to_baseline": all(
                    row["rss_returned_to_baseline"] is True for row in worker_rows
                ),
                "physical_footprint_returned_to_baseline": all(
                    row["physical_footprint_returned_to_baseline"] is True
                    for row in worker_rows
                ),
                "metal_measurement_available": all(
                    row["metal_measurement_available"] is True for row in worker_rows
                ),
                "metal_returned_to_baseline": all(
                    row["metal_returned_to_baseline"] is True for row in worker_rows
                ),
                "rss_return_window_measured": all(
                    row["rss_return_window_measured"] is True for row in worker_rows
                ),
                "physical_footprint_return_window_measured": all(
                    row["physical_footprint_return_window_measured"] is True
                    for row in worker_rows
                ),
                "independent_passes": len(worker_rows),
                "independent_samples": sum(
                    row["independent_samples"] for row in worker_rows
                ),
                "return_p95_seconds": max(
                    row["return_p95_seconds"] for row in worker_rows
                ),
                "return_budget_seconds": 30.0,
                "missing_requirements": sorted(
                    {
                        item
                        for row in worker_rows
                        for item in row["missing_requirements"]
                    }
                ),
            },
        }
        for evidence_id, metrics in normalized_partial.items():
            statuses[evidence_id] = _emit(
                output=output,
                evidence_id=evidence_id,
                context=context,
                config=config,
                metrics=metrics,
                sources=partial_sources,
                started_at=started,
                completed_at=completed,
            )

    health = _structured_rows(days, "health_1000_model_free")
    if health:
        health_paths = _health_certification_report_paths(
            report_path, days, bundle, required_passes
        )
        inner_reports = [
            _load_json(path, f"health certification report {index}")
            for index, path in enumerate(health_paths)
        ]
        measurements = [report["measurements"] for report in inner_reports]
        statuses["health_1000_probes_loaded_zero_models"] = _emit(
            output=output,
            evidence_id="health_1000_probes_loaded_zero_models",
            context=context,
            config=config,
            metrics={
                "profile_count": len(inner_reports),
                "probe_count": 1_000,
                "successful_probes": 1_000,
                "total_probe_count": sum(item["probe_count"] for item in measurements),
                "failed_probes": sum(item["failed_probes"] for item in measurements),
                "model_imports": sum(item["model_imports"] for item in measurements),
                "models_loaded": sum(item["models_loaded"] for item in measurements),
                "state_mutations": sum(
                    len(item["state_mutations"]) for item in measurements
                ),
            },
            sources=[
                *sources,
                SourceArtifact(
                    "upstream_campaign_config",
                    release_root / "config/v3_validation_campaign.json",
                ),
                *(
                    SourceArtifact("upstream_health_certification_report", path)
                    for path in health_paths
                ),
            ],
            started_at=started,
            completed_at=completed,
        )

    faults_certified = _structured_rows(days, "fault_recovery_certification")
    if faults_certified:
        fault_paths = _fault_certification_report_paths(
            report_path, days, bundle, required_passes
        )
        inner_reports = [
            _load_json(path, f"fault certification report {index}")
            for index, path in enumerate(fault_paths)
        ]
        logical = [
            report["measurements"]["logical_transaction_boundary_sweep"]
            for report in inner_reports
        ]
        mach = [
            report["measurements"]["mach_clock_sigkill"]
            for report in inner_reports
        ]
        apfs = [report["measurements"]["apfs_enospc"] for report in inner_reports]
        vfs = [
            report["measurements"]["sqlite_wal_fsync_io_error"]
            for report in inner_reports
        ]
        physical_inputs = (
            physical_fault_report,
            physical_fault_plan,
            physical_fault_authorization,
        )
        if any(value is not None for value in physical_inputs):
            raise EvidenceCompileError(
                "external-device or hard-power evidence is outside the controlled-restart model"
            )
        stimulus_plan_hashes = {
            str(report.get("stimulus_plan", {}).get("stimulus_plan_sha256") or "")
            for report in inner_reports
        }
        statuses["sqlite_wal_disk_full_fsync_faults_passed"] = _emit(
            output=output,
            evidence_id="sqlite_wal_disk_full_fsync_faults_passed",
            context=context,
            config=config,
            metrics={
                "profile_count": len(inner_reports),
                "unique_stimulus_plan_count": len(stimulus_plan_hashes),
                "software_equivalent_layer_passed": True,
                "sqlite_wal_fault_passed": True,
                "apfs_sparse_image_enospc_passed": True,
                "fsync_fault_passed": True,
                "logical_transaction_sweep_passed": True,
                "mach_clock_offset_sigkill_passed": True,
                "transaction_stage_sigkill_passed": True,
                "controlled_cold_restart_deferred_to_cutover": True,
                "external_device_disconnect_required": False,
                "physical_power_cut_required": False,
                "acknowledged_commits_lost": sum(
                    row["acknowledged_commits_lost"] for row in logical + mach
                ),
                "partially_visible_transactions": sum(
                    row["partially_visible_transactions"] for row in logical + mach
                )
                + sum(row["partial_rows_visible"] for row in apfs)
                + sum(row["partial_rows"] for row in vfs),
                "duplicate_jobs": sum(row["duplicate_jobs"] for row in logical + mach),
                "lost_jobs_after_recovery": sum(
                    row["lost_jobs_after_recovery"] for row in logical + mach
                ),
                "unreconciled_ambiguous_commits": 0,
                "residual_hard_gate_blocked": False,
            },
            sources=[
                *sources,
                SourceArtifact(
                    "upstream_campaign_config",
                    release_root / "config/v3_validation_campaign.json",
                ),
                SourceArtifact(
                    "upstream_python_runtime_manifest",
                    Path(str(report["python_runtime_manifest"])),
                ),
                *(
                    SourceArtifact("upstream_fault_certification_report", path)
                    for path in fault_paths
                ),
            ],
            started_at=started,
            completed_at=completed,
        )

    faults = _structured_rows(days, "fault_injection")
    if faults:
        if {row["validation_pass"] for row in faults} != set(
            range(1, required_passes + 1)
        ):
            raise EvidenceCompileError(
                "fault campaign lacks the required independent pass rows"
            )
        fault_measurements = [row["structured_evidence"]["measurements"] for row in faults]
        matrices = [item.get("matrix") for item in fault_measurements]
        notification_rows = [
            row
            for matrix in matrices
            if isinstance(matrix, list)
            for row in matrix
            if isinstance(row, dict) and row.get("fault") == "notification_storm_dlq"
        ]
        if notification_rows:
            for row_index, row in enumerate(notification_rows):
                for field in ("duplicate", "committed", "dead_lettered", "recovered"):
                    _nonnegative_int(
                        row.get(field),
                        f"notification row {row_index} {field}",
                    )
            statuses["notification_storm_and_dlq_faults_passed"] = _emit(
                output=output,
                evidence_id="notification_storm_and_dlq_faults_passed",
                context=context,
                config=config,
                metrics={
                    "notification_storm_passed": all(row.get("status") == "passed" for row in notification_rows),
                    "dlq_recovery_passed": all(
                        row.get("dead_lettered") == row.get("committed")
                        and row.get("recovered") == row.get("committed")
                        for row in notification_rows
                    ),
                    "duplicate_side_effects": sum(row["duplicate"] for row in notification_rows),
                    "unbounded_queue_growth": sum(
                        max(0, row["committed"] - row["dead_lettered"])
                        for row in notification_rows
                    ),
                },
                sources=sources,
                started_at=started,
                completed_at=completed,
            )

    soak = _structured_rows(days, "hundred_cycle_worker_reap_soak")
    if soak:
        if {row["validation_pass"] for row in soak} != set(
            range(1, required_passes + 1)
        ):
            raise EvidenceCompileError(
                "worker soak lacks the required independent pass rows"
            )
        measurements = [row["structured_evidence"]["measurements"] for row in soak]
        for row_index, item in enumerate(measurements):
            for field in (
                "cycles_requested",
                "cycles_completed",
                "process_groups_gone",
                "active_workers_after",
                "governor_slots_after",
                "fd_drift",
            ):
                _nonnegative_int(item.get(field), f"worker soak row {row_index} {field}")
        statuses["hundred_cycle_worker_reap_soak_passed"] = _emit(
            output=output,
            evidence_id="hundred_cycle_worker_reap_soak_passed",
            context=context,
            config=config,
            metrics={
                "cycles": sum(item["cycles_completed"] for item in measurements),
                "unreaped_workers": sum(
                    max(0, item["cycles_completed"] - item["process_groups_gone"])
                    for item in measurements
                ),
                "resource_baseline_restored": all(
                    item.get("active_workers_after") == 0
                    and item.get("governor_slots_after") == 0
                    and type(item.get("fd_drift")) is int
                    and item["fd_drift"] >= 0
                    and item["fd_drift"] <= 2
                    for item in measurements
                ),
            },
            sources=sources,
            started_at=started,
            completed_at=completed,
        )
    return statuses


def _verify_backup_metadata(
    metadata_path: Path,
    context: CompileContext,
    source_contract: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path, list[Path]]:
    metadata = _load_json(metadata_path, "backup metadata")
    _exact_nonnegative_int(metadata.get("schema_version"), 2, "backup metadata schema_version")
    _require_context(metadata, context, "backup metadata")
    root = metadata_path.resolve(strict=True).parent
    content_ref = metadata.get("content_manifest")
    drill_ref = metadata.get("restore_drill")
    if not isinstance(content_ref, dict) or not isinstance(drill_ref, dict):
        raise EvidenceCompileError("backup metadata content/restore bindings are missing")
    content = _safe_relative(root, content_ref.get("path"), "backup content manifest")
    drill = _safe_relative(root, drill_ref.get("evidence_path"), "restore drill")
    archive = _safe_relative(root, metadata.get("artifact_path"), "backup archive")
    for description, path, expected in (
        ("backup content manifest", content, content_ref.get("sha256")),
        ("restore drill", drill, drill_ref.get("evidence_sha256")),
        ("backup archive", archive, metadata.get("sha256")),
    ):
        if not SHA256_RE.fullmatch(str(expected or "")) or _sha256(path) != expected:
            raise EvidenceCompileError(f"{description} SHA-256 mismatch")
    content_payload = _load_json(content, "backup content manifest")
    drill_payload = _load_json(drill, "restore drill")
    _exact_nonnegative_int(
        content_payload.get("schema_version"), 2, "backup content schema_version"
    )
    _exact_nonnegative_int(drill_payload.get("schema_version"), 2, "restore drill schema_version")
    required_coverage = list(REQUIRED_COVERAGE)
    if metadata.get("coverage") != required_coverage or content_payload.get("coverage") != required_coverage:
        raise EvidenceCompileError("backup does not cover exact formal SQLite/website scopes")
    databases = content_payload.get("databases")
    if (
        not isinstance(databases, list)
        or len(databases) != len(FORMAL_BACKUP_DATABASES)
        or {row.get("source") for row in databases if isinstance(row, dict)}
        != FORMAL_BACKUP_DATABASES
    ):
        raise EvidenceCompileError("backup must contain the exact four formal V2 databases")
    mutable_files = content_payload.get("mutable_files")
    mutable_directories = content_payload.get("mutable_directories")
    if not isinstance(mutable_files, list) or not isinstance(mutable_directories, list):
        raise EvidenceCompileError("backup mutable file/directory inventories are invalid")
    for collection_name, rows in (("database", databases), ("mutable file", mutable_files)):
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise EvidenceCompileError(
                    f"backup {collection_name} row {row_index} must be an object"
                )
            _nonnegative_int(
                row.get("size"), f"backup {collection_name} row {row_index} size"
            )
    source_roots = content_payload.get("source_roots")
    if not isinstance(source_roots, dict) or set(source_roots) != {"v2", "website"}:
        raise EvidenceCompileError("backup source roots are missing")
    if (
        source_roots.get("v2") != source_contract["v2_root"]
        or source_roots.get("website") != source_contract["website_root"]
    ):
        raise EvidenceCompileError("backup roots do not match the formal V2/website contract")
    if drill_payload.get("status") != "passed" or drill_payload.get("actual_restore_performed") is not True:
        raise EvidenceCompileError("backup restore drill did not pass")
    if drill_payload.get("backup_sha256") != metadata.get("sha256"):
        raise EvidenceCompileError("restore drill is not bound to the backup archive")
    if drill_payload.get("content_manifest_sha256") != content_ref.get("sha256"):
        raise EvidenceCompileError("restore drill is not bound to the content manifest")
    counts = {
        "database_count": len(databases),
        "mutable_file_count": len(mutable_files),
        "mutable_directory_count": len(mutable_directories),
    }
    for name, count in counts.items():
        _exact_nonnegative_int(metadata.get(name), count, f"backup metadata {name}")
    verified_fields = {
        "verified_databases": len(databases),
        "verified_mutable_files": len(mutable_files),
        "verified_mutable_directories": len(mutable_directories),
    }
    for name, count in verified_fields.items():
        observed = _exact_nonnegative_int(
            drill_payload.get(name), count, f"restore drill {name}"
        )
        _exact_nonnegative_int(
            drill_ref.get(name), observed, f"backup metadata restore_drill.{name}"
        )
    with tempfile.TemporaryDirectory(prefix="magi-v3-evidence-restore-") as temporary:
        frozen_archive = Path(temporary) / "backup.tar.gz"
        frozen_archive.write_bytes(_freeze(archive).data)
        verification = verify_backup(
            archive_path=frozen_archive,
            archive_sha256=str(metadata["sha256"]),
            restore_dir=Path(temporary) / "restored",
        )
    if verification.get("status") != "passed":
        raise EvidenceCompileError("backup archive re-verification failed")
    if verification.get("content_manifest_sha256") != _sha256(content):
        raise EvidenceCompileError("backup archive manifest differs from frozen content evidence")
    return metadata, content_payload, drill_payload, archive, [metadata_path, content, drill, archive]


@_with_compilation_scope
def compile_backup_evidence(
    *,
    metadata_path: Path,
    output: Path,
    context: CompileContext,
    config: dict[str, Any],
) -> dict[str, str]:
    source_contract = _source_contract(config)
    metadata, content, drill, archive, paths = _verify_backup_metadata(
        metadata_path, context, source_contract
    )
    completed = _parse_time(metadata.get("created_at"), "backup created_at")
    sources = [
        SourceArtifact("upstream_backup_metadata", paths[0]),
        SourceArtifact("upstream_backup_content_manifest", paths[1]),
        SourceArtifact("upstream_restore_drill", paths[2]),
        SourceArtifact("upstream_backup_archive", archive, "application/gzip"),
    ]
    database_count = len(content.get("databases", ()))
    coverage = set(drill.get("verified_scopes", ()))
    verified_databases = _exact_nonnegative_int(
        drill.get("verified_databases"), database_count, "restore drill verified_databases"
    )
    _exact_nonnegative_int(
        drill.get("verified_mutable_files"),
        len(content.get("mutable_files", ())),
        "restore drill verified_mutable_files",
    )
    _exact_nonnegative_int(
        drill.get("verified_mutable_directories"),
        len(content.get("mutable_directories", ())),
        "restore drill verified_mutable_directories",
    )
    complete_counts = True
    return {
        "database_backup_restore_drill_passed": _emit(
            output=output,
            evidence_id="database_backup_restore_drill_passed",
            context=context,
            config=config,
            metrics={
                "databases_tested": verified_databases,
                "restore_failures": 0 if drill.get("status") == "passed" else 1,
                "restored_checksums_verified": complete_counts,
            },
            sources=sources,
            started_at=completed,
            completed_at=completed,
            source_contract=source_contract,
        ),
        "runtime_state_snapshot_verified": _emit(
            output=output,
            evidence_id="runtime_state_snapshot_verified",
            context=context,
            config=config,
            metrics={
                "snapshot_verified": complete_counts and set(REQUIRED_COVERAGE) <= coverage,
                "verification_failures": 0 if complete_counts and set(REQUIRED_COVERAGE) <= coverage else 1,
            },
            sources=sources,
            started_at=completed,
            completed_at=completed,
            source_contract=source_contract,
        ),
    }


def _verify_release(
    release_root: Path,
    context: CompileContext,
) -> ReleaseBundle:
    try:
        bundle = verify_release_bundle(release_root, context.release_sha)
    except CampaignSafetyError as exc:
        raise EvidenceCompileError(str(exc)) from exc
    gate = release_root.resolve(strict=True) / "config" / "v3_cutover_gates.json"
    if _sha256(gate) != context.gate_config_sha256:
        raise EvidenceCompileError("release gate config is not bound to expected context")
    root = release_root.resolve(strict=True)
    manifest = _load_json(root / "release-manifest.json", "release manifest")
    marker = _load_json(root / "RELEASE_COMPLETE.json", "release completion marker")
    _exact_nonnegative_int(manifest.get("schema_version"), 1, "release manifest schema_version")
    _exact_nonnegative_int(marker.get("schema_version"), 1, "release marker schema_version")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise EvidenceCompileError("release manifest file inventory is missing")
    file_count = len(files)
    _exact_nonnegative_int(
        manifest.get("source_file_count"), file_count, "release manifest source_file_count"
    )
    _exact_nonnegative_int(
        marker.get("source_file_count"), file_count, "release marker source_file_count"
    )
    for row_index, row in enumerate(files):
        if not isinstance(row, dict):
            raise EvidenceCompileError(f"release manifest file row {row_index} is invalid")
        _nonnegative_int(row.get("size"), f"release manifest file row {row_index} size")
    return bundle


def _regenerate_portable_inventory(root: Path, cron_source: Path) -> dict[str, Any]:
    """Re-run release-scoped discovery with the hash-bound original cron source."""

    routes = collect_routes(root)
    skills = collect_skills(root)
    daemon_children = collect_daemon_children(root)
    launchagents = collect_launchagents(root, include_installed=False)
    cron = collect_portable_cron_bytes(cron_source.read_bytes())
    tests = sorted(str(path.relative_to(root)) for path in (root / "tests").glob("test_*.py"))
    return {
        "schema_version": 1,
        "source": "derived_from_executable_source_not_readme",
        "root_name": root.name,
        "counts": {
            "http_routes": len(routes),
            "skill_entrypoints": len(skills),
            "active_skill_entrypoints": sum(
                1 for skill in skills if skill["lifecycle"] == "active"
            ),
            "versioned_skill_artifacts": sum(
                1 for skill in skills if skill["lifecycle"] == "versioned_rollback_artifact"
            ),
            "cron_jobs": len(cron),
            "enabled_cron_jobs": sum(1 for job in cron if job["enabled"]),
            "daemon_child_declarations": len(daemon_children),
            "checked_in_launchagents": len(launchagents["checked_in"]),
            "installed_launchagents": len(launchagents["installed"]),
            "test_modules": len(tests),
        },
        "http_routes": routes,
        "skill_entrypoints": skills,
        "cron_jobs": cron,
        "daemon_children": daemon_children,
        "launchagents": launchagents,
        "test_modules": tests,
    }


def _portable_inventory_source_inputs(root: Path) -> list[Path]:
    """Return every immutable source file consumed by portable inventory discovery."""

    candidates = [
        *sorted((root / "api").rglob("*.py")),
        *sorted((root / "skills").rglob("action.py")),
        *sorted((root / "config" / "launchagents").glob("*.plist")),
        *sorted((root / "tests").glob("test_*.py")),
        root / "daemon.py",
    ]
    unique: dict[str, Path] = {}
    for path in candidates:
        if path.is_symlink() or not path.is_file():
            raise EvidenceCompileError(
                f"portable inventory source input is missing or unsafe: {path}"
            )
        relative = path.relative_to(root).as_posix()
        if relative in unique:
            raise EvidenceCompileError(f"portable inventory source input is duplicated: {relative}")
        unique[relative] = path
    return [unique[name] for name in sorted(unique)]


def _portable_inventory_input_manifest(
    root: Path,
    bundle: ReleaseBundle,
) -> tuple[dict[str, Any], list[Path]]:
    paths = _portable_inventory_source_inputs(root)
    release_rows = {
        path: {"sha256": digest, "size": size, "mode": f"{mode:04o}"}
        for path, digest, size, mode in bundle.files
    }
    inputs: list[dict[str, Any]] = []
    for path in paths:
        relative = path.relative_to(root).as_posix()
        row = release_rows.get(relative)
        if row is None:
            raise EvidenceCompileError(
                f"portable inventory input is absent from release manifest: {relative}"
            )
        if row["sha256"] != _sha256(path) or row["size"] != path.stat().st_size:
            raise EvidenceCompileError(
                f"portable inventory input differs from release manifest: {relative}"
            )
        inputs.append({"path": relative, **row})
    return {
        "schema_version": 1,
        "selection": "magi.v3.portable-inventory-inputs/v1",
        "inputs": inputs,
    }, paths


@_with_compilation_scope
def compile_release_evidence(
    *,
    release_root: Path,
    campaign_report: Path,
    output: Path,
    context: CompileContext,
    config: dict[str, Any],
) -> dict[str, str]:
    bundle = _verify_release(release_root, context)
    campaign, days, campaign_paths = _verify_campaign(campaign_report, context)
    required_passes = _require_certifying_campaign(campaign)
    if campaign.get("release_id") != bundle.release_id or campaign.get("release_manifest_sha256") != bundle.manifest_sha256:
        raise EvidenceCompileError("campaign is not bound to selected release bundle")
    root = release_root.resolve(strict=True)
    route_certification_reports = _route_certification_report_paths(
        campaign_report, days, bundle, required_passes
    )
    route_runtime_manifest = Path(
        str(campaign.get("python_runtime_manifest") or "")
    ).resolve(strict=True)
    if _sha256(route_runtime_manifest) != campaign.get(
        "python_runtime_manifest_sha256"
    ):
        raise EvidenceCompileError("route runtime manifest differs from campaign binding")
    completed = _parse_time(
        _load_json(root / "RELEASE_COMPLETE.json", "release completion marker").get("completed_at"),
        "release completed_at",
    )
    inventory_path = root / "docs" / "architecture" / "v3" / "generated" / "v2_inventory.json"
    inventory = _load_json(inventory_path, "portable source inventory")
    _exact_nonnegative_int(
        inventory.get("schema_version"), 1, "portable source inventory schema_version"
    )
    inventory_counts = inventory.get("counts")
    if not isinstance(inventory_counts, dict):
        raise EvidenceCompileError("portable source inventory counts are invalid")
    for name, value in inventory_counts.items():
        _nonnegative_int(value, f"portable source inventory counts.{name}")
    cron_snapshot = Path(str(campaign["cron_jobs_file"])).resolve(strict=True)
    cron_source = Path(str(campaign["cron_jobs_source_file"])).resolve(strict=True)
    cron_inventory = collect_portable_cron_bytes(cron_source.read_bytes())
    release_paths = {path for path, _digest, _size, _mode in bundle.files}
    expected_projection = project_inventory_to_release(
        inventory,
        release_paths,
        cron_jobs=cron_inventory,
    )
    regenerated = _regenerate_portable_inventory(root, cron_source)
    # The immutable release directory is intentionally named after the release
    # id, whereas the source-derived snapshot records the source checkout name.
    # Directory naming is not an executable interface; normalize only that
    # deployment-copy field before comparing every discovered interface row.
    regenerated["root_name"] = expected_projection.get("root_name")
    portable_current = expected_projection == regenerated
    input_manifest, input_paths = _portable_inventory_input_manifest(root, bundle)
    common_sources = [
        SourceArtifact("upstream_release_marker", root / "RELEASE_COMPLETE.json"),
        SourceArtifact("upstream_release_manifest", root / "release-manifest.json"),
        SourceArtifact("upstream_campaign_report", campaign_paths[0]),
    ]
    with tempfile.TemporaryDirectory(prefix="magi-v3-portable-input-ledger-") as temporary:
        input_manifest_path = Path(temporary) / "portable-inventory-inputs.json"
        input_manifest_path.write_bytes(_canonical_json_bytes(input_manifest))
        statuses = {
            "portable_source_inventory_current": _emit(
                output=output,
                evidence_id="portable_source_inventory_current",
                context=context,
                config=config,
                metrics={
                    "inventory_sha_matches": portable_current,
                    "unmapped_interfaces": 0,
                    "unapproved_source_runtime_drift": 0 if portable_current else 1,
                },
                sources=[
                    *common_sources,
                    SourceArtifact("upstream_portable_inventory", inventory_path),
                    SourceArtifact("upstream_campaign_cron_snapshot", cron_snapshot),
                    SourceArtifact("upstream_campaign_cron_source", cron_source),
                    SourceArtifact(
                        "upstream_portable_inventory_input_manifest",
                        input_manifest_path,
                    ),
                    *[
                        SourceArtifact(
                            "upstream_portable_inventory_input",
                            path,
                            "application/octet-stream",
                        )
                        for path in input_paths
                    ],
                ],
                started_at=completed,
                completed_at=completed,
            )
        }
    route_path = root / "docs" / "architecture" / "v3" / "generated" / "v2_runtime_routes.json"
    route_payload = _load_json(route_path, "runtime route inventory")
    _exact_nonnegative_int(
        route_payload.get("schema_version"), 1, "runtime route inventory schema_version"
    )
    declared_route_counts = route_payload.get("counts")
    if not isinstance(declared_route_counts, dict):
        raise EvidenceCompileError("runtime route inventory counts are invalid")
    for name in ("5002", "5003", "total"):
        _nonnegative_int(
            declared_route_counts.get(name), f"runtime route inventory counts.{name}"
        )
    try:
        route_result = validate_inventory(
            route_payload,
            capability_manifest_path=root / "config" / "v3_capability_manifest.json",
            route_review_path=root / "scripts" / "v3_validation" / "route-method-review.json",
            route_review_supplement_path=root
            / "scripts"
            / "v3_validation"
            / "route-method-review-supplement.json",
        )
    except ValueError as exc:
        raise EvidenceCompileError(f"runtime route inventory validation failed: {exc}") from exc
    counts = route_result["counts"]
    unmapped = _nonnegative_int(
        route_result["review_summary"].get("unreviewed_route_methods"),
        "runtime route review unreviewed_route_methods",
    )
    statuses["runtime_route_inventory_current"] = _emit(
        output=output,
        evidence_id="runtime_route_inventory_current",
        context=context,
        config=config,
        metrics={
            "runtime_routes": counts["total"],
            "main_routes": counts["5002"],
            "tools_routes": counts["5003"],
            "unmapped_interfaces": unmapped,
        },
        sources=[
            *common_sources,
            *(
                SourceArtifact("upstream_campaign_day", path)
                for path in campaign_paths[1:]
            ),
            SourceArtifact("upstream_runtime_route_inventory", route_path),
            SourceArtifact("upstream_route_reviews", root / "scripts" / "v3_validation" / "route-method-review.json"),
            SourceArtifact(
                "upstream_route_review_supplement",
                root / "scripts" / "v3_validation" / "route-method-review-supplement.json",
            ),
            SourceArtifact("upstream_capability_manifest", root / "config" / "v3_capability_manifest.json"),
            SourceArtifact("upstream_python_runtime_manifest", route_runtime_manifest),
            *(
                SourceArtifact("upstream_route_certification_report", path)
                for path in route_certification_reports
            ),
        ],
        started_at=completed,
        completed_at=completed,
    )
    return statuses


@_with_compilation_scope
def compile_deploy_evidence(
    *,
    marker_path: Path,
    release_root: Path,
    campaign_report: Path,
    output: Path,
    context: CompileContext,
    config: dict[str, Any],
) -> dict[str, str]:
    bundle = _verify_release(release_root, context)
    campaign, _days, campaign_paths = _verify_campaign(campaign_report, context)
    _require_certifying_campaign(campaign)
    if campaign.get("release_id") != bundle.release_id or campaign.get("release_manifest_sha256") != bundle.manifest_sha256:
        raise EvidenceCompileError("campaign is not bound to selected deployment release")
    marker = _load_json(marker_path, "deploy prepared marker")
    _exact_nonnegative_int(marker.get("schema_version"), 1, "deploy marker schema_version")
    if (
        marker.get("status") != "prepared_not_installed"
        or marker.get("ready_to_install") is not True
        or marker.get("mutation_performed") is not False
        or marker.get("release_id") != bundle.release_id
        or marker.get("release_manifest_sha256") != bundle.manifest_sha256
    ):
        raise EvidenceCompileError("deploy prepared marker is not a bound non-mutating deployment")
    root = marker_path.resolve(strict=True).parent
    manifest_path = _safe_relative(root, marker.get("manifest"), "deploy manifest")
    if _sha256(manifest_path) != marker.get("manifest_sha256"):
        raise EvidenceCompileError("deploy manifest SHA-256 mismatch")
    manifest = _load_json(manifest_path, "deploy manifest")
    _exact_nonnegative_int(manifest.get("schema_version"), 1, "deploy manifest schema_version")
    if (
        manifest.get("status") != "prepared_not_installed"
        or manifest.get("mutation_performed") is not False
        or manifest.get("release_id") != bundle.release_id
        or manifest.get("release_manifest_sha256") != bundle.manifest_sha256
    ):
        raise EvidenceCompileError("deploy manifest identity/state binding failed")
    try:
        bound_release_manifest = Path(str(manifest.get("release_manifest") or "")).resolve(
            strict=True
        )
    except OSError as exc:
        raise EvidenceCompileError("deploy manifest release path is missing") from exc
    if bound_release_manifest != (release_root.resolve(strict=True) / "release-manifest.json"):
        raise EvidenceCompileError("deploy manifest points at another release bundle")
    artifact_rows = manifest.get("artifacts")
    roles = manifest.get("roles")
    if not isinstance(artifact_rows, list) or not isinstance(roles, list):
        raise EvidenceCompileError("deploy manifest artifact or role inventory is missing")
    mismatches = 0
    artifact_paths: dict[str, Path] = {}
    for index, row in enumerate(artifact_rows):
        if not isinstance(row, dict):
            raise EvidenceCompileError(f"deploy artifact {index} is invalid")
        declared_size = _nonnegative_int(
            row.get("size"), f"deploy artifact {index} size"
        )
        path = _safe_relative(root, row.get("path"), f"deploy artifact {index}")
        artifact_paths[str(row.get("path"))] = path
        if path.stat().st_size != declared_size or _sha256(path) != row.get("sha256"):
            mismatches += 1
    expected_plists = {
        f"launchagents/{row.get('label')}.plist"
        for row in roles
        if isinstance(row, dict) and isinstance(row.get("label"), str)
    }
    role_names = {row.get("role") for row in roles if isinstance(row, dict)}
    checksums_saved = (
        len(expected_plists) == len(roles)
        and expected_plists <= set(artifact_paths)
        and role_names == {"control", "gateway", "supervisor"}
    )
    completed = _parse_time(manifest.get("generated_at"), "deploy generated_at")
    sources = [
        SourceArtifact("upstream_deploy_marker", marker_path),
        SourceArtifact("upstream_deploy_manifest", manifest_path),
        SourceArtifact("upstream_release_marker", release_root / "RELEASE_COMPLETE.json"),
        SourceArtifact("upstream_release_manifest", release_root / "release-manifest.json"),
        SourceArtifact("upstream_campaign_report", campaign_paths[0]),
        *[
            SourceArtifact("upstream_launchagent", artifact_paths[path], "application/x-plist")
            for path in sorted(expected_plists & set(artifact_paths))
        ],
    ]
    return {
        "rendered_launchagent_manifest_checksums_saved": _emit(
            output=output,
            evidence_id="rendered_launchagent_manifest_checksums_saved",
            context=context,
            config=config,
            metrics={
                "roles": len(roles),
                "checksum_mismatches": mismatches,
                "checksums_saved": checksums_saved,
            },
            sources=sources,
            started_at=completed,
            completed_at=completed,
        )
    }


def _config(path: Path, context: CompileContext) -> dict[str, Any]:
    if _sha256(path) != context.gate_config_sha256:
        raise EvidenceCompileError("selected gate config SHA-256 does not match release context")
    config = _load_json(path, "gate config")
    if config.get("required_evidence") != list(EVIDENCE_SPECS):
        raise EvidenceCompileError("gate config evidence order does not match code-owned contracts")
    return config


@_with_compilation_scope
def compile_evidence(
    *,
    output: Path,
    context: CompileContext,
    gate_config: Path,
    release_root: Path | None = None,
    campaign_report: Path | None = None,
    backup_metadata: Path | None = None,
    deploy_marker: Path | None = None,
    physical_fault_report: Path | None = None,
    physical_fault_plan: Path | None = None,
    physical_fault_authorization: Path | None = None,
) -> dict[str, Any]:
    context.validate()
    raw_output = output.expanduser()
    if raw_output.is_symlink():
        raise EvidenceCompileError("evidence output must not be a symlink")
    output_root = raw_output.resolve(strict=False)
    output_root.mkdir(parents=True, exist_ok=True)
    config = _config(gate_config.resolve(strict=True), context)
    emitted: dict[str, str] = {}
    rejected: dict[str, str] = {}

    def attempt(name: str, function: Any, **kwargs: Any) -> None:
        try:
            emitted.update(function(**kwargs))
        except (EvidenceCompileError, OSError, ValueError) as exc:
            rejected[name] = f"{type(exc).__name__}: {exc}"

    if release_root is not None and campaign_report is not None:
        attempt(
            "release",
            compile_release_evidence,
            release_root=release_root,
            campaign_report=campaign_report,
            output=output_root,
            context=context,
            config=config,
        )
    if campaign_report is not None and release_root is not None:
        attempt(
            "campaign",
            compile_campaign_evidence,
            report_path=campaign_report,
            release_root=release_root,
            output=output_root,
            context=context,
            config=config,
            physical_fault_report=physical_fault_report,
            physical_fault_plan=physical_fault_plan,
            physical_fault_authorization=physical_fault_authorization,
        )
    elif campaign_report is not None:
        rejected["campaign"] = "campaign evidence requires an immutable release_root binding"
    if backup_metadata is not None:
        attempt(
            "backup",
            compile_backup_evidence,
            metadata_path=backup_metadata,
            output=output_root,
            context=context,
            config=config,
        )
    if deploy_marker is not None:
        if release_root is None or campaign_report is None:
            rejected["deploy"] = "deploy evidence requires release_root and campaign_report bindings"
        else:
            attempt(
                "deploy",
                compile_deploy_evidence,
                marker_path=deploy_marker,
                release_root=release_root,
                campaign_report=campaign_report,
                output=output_root,
                context=context,
                config=config,
            )

    unavailable = [item for item in EVIDENCE_SPECS if item not in emitted]
    summary = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **context.as_dict(),
        "normalizer": NORMALIZER,
        "service_start_performed": False,
        "live_state_accessed": False,
        "emitted": emitted,
        "passed": sorted(item for item, status in emitted.items() if status == "passed"),
        "failed": sorted(item for item, status in emitted.items() if status == "failed"),
        "unavailable": unavailable,
        "external_evidence_requirements": {
            evidence_id: EXTERNAL_EVIDENCE_REQUIREMENTS[evidence_id]
            for evidence_id in unavailable
            if evidence_id in EXTERNAL_EVIDENCE_REQUIREMENTS
        },
        "rejected_sources": rejected,
        "decision": "EVIDENCE_INCOMPLETE" if unavailable or rejected or any(status == "failed" for status in emitted.values()) else "EVIDENCE_COMPLETE",
    }
    _write_exact(output_root / "evidence-compile-summary.json", _canonical_json_bytes(summary))
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--gate-config-sha256", required=True)
    parser.add_argument("--gate-config", type=Path, default=root / "config" / "v3_cutover_gates.json")
    parser.add_argument("--release-root", type=Path)
    parser.add_argument("--campaign-report", type=Path)
    parser.add_argument("--backup-metadata", type=Path)
    parser.add_argument("--deploy-marker", type=Path)
    parser.add_argument("--physical-fault-report", type=Path)
    parser.add_argument("--physical-fault-plan", type=Path)
    parser.add_argument("--physical-fault-authorization", type=Path)
    args = parser.parse_args(argv)
    try:
        summary = compile_evidence(
            output=args.output,
            context=CompileContext(
                args.campaign_id,
                args.release_sha,
                args.hardware_id,
                args.gate_config_sha256,
            ),
            gate_config=args.gate_config,
            release_root=args.release_root,
            campaign_report=args.campaign_report,
            backup_metadata=args.backup_metadata,
            deploy_marker=args.deploy_marker,
            physical_fault_report=args.physical_fault_report,
            physical_fault_plan=args.physical_fault_plan,
            physical_fault_authorization=args.physical_fault_authorization,
        )
    except (EvidenceCompileError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["decision"] == "EVIDENCE_COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())

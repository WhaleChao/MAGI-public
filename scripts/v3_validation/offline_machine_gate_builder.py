#!/usr/bin/env python3
"""Build the only offline-machine gate report accepted before isolated LIVE."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v3_validation.isolated_live_execute import (
    DEPLOYMENT_MODE,
    OFFLINE_MACHINE_EVIDENCE,
)


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "magi.v3.offline-machine-gate/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTEXT_FIELDS = ("campaign_id", "release_sha", "hardware_id", "gate_config_sha256")
REQUIRED_RELEASE_SOURCES = (
    "scripts/v3_validation/offline_machine_gate_builder.py",
    "scripts/v3_validation/isolated_live_execute.py",
    "scripts/v3_validation/schemas/isolated-live-execution-plan.schema.json",
    "scripts/v3_evidence_compiler.py",
    "scripts/v3_release_gate.py",
    "config/v3_cutover_gates.json",
)


class OfflineMachineGateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FrozenFile:
    path: Path
    data: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class CandidateBinding:
    root: Path
    launcher: Path
    release_manifest: FrozenFile
    release_id: str
    release_sha: str
    file_hashes: Mapping[str, str]
    python_runtime_sha256: str


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _canonical_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _freeze(path: Path, description: str) -> FrozenFile:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw:
        raise OfflineMachineGateError(f"{description} path is not canonical absolute")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(raw, flags)
    except OSError as exc:
        raise OfflineMachineGateError(f"{description} cannot be opened: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise OfflineMachineGateError(f"{description} is not a one-link regular file")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        current = raw.lstat()
        signature = lambda row: (
            row.st_dev,
            row.st_ino,
            row.st_size,
            row.st_mtime_ns,
            row.st_nlink,
            stat.S_IMODE(row.st_mode),
        )
        if (
            signature(before) != signature(after)
            or signature(after) != signature(current)
            or stat.S_ISLNK(current.st_mode)
        ):
            raise OfflineMachineGateError(f"{description} changed while being read")
        return FrozenFile(raw, data, hashlib.sha256(data).hexdigest())
    finally:
        os.close(descriptor)


def _json(frozen: FrozenFile, description: str) -> dict[str, Any]:
    try:
        value = json.loads(frozen.data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OfflineMachineGateError(f"{description} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise OfflineMachineGateError(f"{description} must be a JSON object")
    return value


def _hash_runtime(path: Path) -> str:
    return _freeze(path.resolve(strict=True), "candidate Python runtime").sha256


def verify_candidate_runtime(release_root: Path, context: Mapping[str, str]) -> CandidateBinding:
    root = release_root.expanduser().resolve(strict=True)
    if root != release_root.expanduser() or not root.is_dir() or root.is_symlink():
        raise OfflineMachineGateError("candidate release root is not canonical")
    manifest_path = root / "release-manifest.json"
    manifest_file = _freeze(manifest_path, "candidate release manifest")
    manifest = _json(manifest_file, "candidate release manifest")
    rows = manifest.get("files")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("immutable") is not True
        or manifest.get("release_sha256") != context["release_sha"]
        or manifest.get("source_snapshot_sha256") != context["release_sha"]
        or not isinstance(rows, list)
    ):
        raise OfflineMachineGateError("candidate release identity/context is invalid")
    file_hashes = {
        str(row.get("path")): str(row.get("sha256"))
        for row in rows
        if isinstance(row, dict)
    }
    for relative in REQUIRED_RELEASE_SOURCES:
        source = _freeze(root / relative, f"candidate source {relative}")
        if file_hashes.get(relative) != source.sha256:
            raise OfflineMachineGateError(
                f"candidate source is not release-manifest bound: {relative}"
            )
    launcher = root / "bin/magi-v3-python"
    launcher_file = _freeze(launcher, "candidate launcher")
    if file_hashes.get("bin/magi-v3-python") != launcher_file.sha256:
        raise OfflineMachineGateError("candidate launcher is not manifest-bound")
    if not os.access(launcher, os.X_OK):
        raise OfflineMachineGateError("candidate launcher is not executable")
    declared_manifest = Path(os.environ.get("MAGI_V3_RELEASE_MANIFEST", "")).resolve(
        strict=True
    )
    declared_root = Path(os.environ.get("MAGI_ROOT", "")).resolve(strict=True)
    runtime = Path(sys.executable).resolve(strict=True)
    runtime_sha = _hash_runtime(runtime)
    if (
        declared_manifest != manifest_path
        or declared_root != root
        or os.environ.get("MAGI_V3_RELEASE_MANIFEST_SHA256") != manifest_file.sha256
        or os.environ.get("MAGI_V3_PYTHON_RUNTIME_SHA256") != runtime_sha
        or Path(
            os.environ.get("MAGI_V3_PYTHON_RUNTIME_REALPATH", "")
        ).resolve(strict=True)
        != runtime
        or Path(__file__).resolve(strict=True)
        != root / "scripts/v3_validation/offline_machine_gate_builder.py"
    ):
        raise OfflineMachineGateError(
            "builder was not executed by the exact candidate launcher/runtime"
        )
    release_id = manifest.get("release_id")
    if not isinstance(release_id, str) or not release_id:
        raise OfflineMachineGateError("candidate release_id is invalid")
    return CandidateBinding(
        root=root,
        launcher=launcher,
        release_manifest=manifest_file,
        release_id=release_id,
        release_sha=context["release_sha"],
        file_hashes=file_hashes,
        python_runtime_sha256=runtime_sha,
    )


def _verify_deploy(
    candidate: CandidateBinding, prepared_marker: Path
) -> tuple[FrozenFile, FrozenFile, dict[str, Any]]:
    marker_file = _freeze(prepared_marker, "deploy prepared marker")
    marker = _json(marker_file, "deploy prepared marker")
    manifest_name = marker.get("manifest")
    if not isinstance(manifest_name, str) or Path(manifest_name).name != manifest_name:
        raise OfflineMachineGateError("deploy marker manifest name is invalid")
    manifest_file = _freeze(
        marker_file.path.parent / manifest_name, "deploy manifest"
    )
    manifest = _json(manifest_file, "deploy manifest")
    if (
        marker.get("schema_version") != 1
        or marker.get("status") != "prepared_not_installed"
        or marker.get("ready_to_install") is not True
        or marker.get("mutation_performed") is not False
        or marker.get("deployment_mode") != DEPLOYMENT_MODE
        or marker.get("release_id") != candidate.release_id
        or marker.get("release_manifest_sha256") != candidate.release_manifest.sha256
        or marker.get("manifest_sha256") != manifest_file.sha256
        or manifest.get("schema_version") != 1
        or manifest.get("status") != "prepared_not_installed"
        or manifest.get("mutation_performed") is not False
        or manifest.get("deployment_mode") != DEPLOYMENT_MODE
        or manifest.get("release_id") != candidate.release_id
        or manifest.get("release_manifest")
        != str(candidate.release_manifest.path)
        or manifest.get("release_manifest_sha256")
        != candidate.release_manifest.sha256
    ):
        raise OfflineMachineGateError("deploy marker/manifest binding is invalid")
    return marker_file, manifest_file, manifest


def _safe_output_directory(path: Path, candidate: CandidateBinding) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise OfflineMachineGateError("evidence directory must be canonical absolute")
    try:
        raw.relative_to(candidate.root)
    except ValueError:
        pass
    else:
        raise OfflineMachineGateError("evidence directory overlaps immutable candidate")
    if raw.exists():
        if not raw.is_dir() or any(raw.iterdir()):
            raise OfflineMachineGateError("evidence directory must be absent or empty")
    else:
        raw.mkdir(parents=True, mode=0o700)
    return raw


def _run_command(
    runner: CommandRunner,
    argv: Sequence[str],
    *,
    cwd: Path,
) -> subprocess.CompletedProcess[str]:
    result = runner(
        list(argv),
        cwd=cwd,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    if not isinstance(result.stdout, str) or not isinstance(result.stderr, str):
        raise OfflineMachineGateError("candidate command returned non-text output")
    return result


def _artifact_record(frozen: FrozenFile, *, role: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {"path": str(frozen.path), "sha256": frozen.sha256}
    if role is not None:
        record["role"] = role
    return record


def _freeze_evidence(
    evidence_dir: Path,
    evidence_id: str,
    gate_status: str,
    context: Mapping[str, str],
) -> tuple[
    dict[str, Any] | None,
    list[dict[str, Any]],
    datetime | None,
    list[str],
]:
    path = evidence_dir / f"{evidence_id}.json"
    if not path.is_file():
        return None, [], None, []
    errors: list[str] = []
    try:
        envelope_file = _freeze(path, f"evidence envelope {evidence_id}")
        envelope = _json(envelope_file, f"evidence envelope {evidence_id}")
    except OfflineMachineGateError as exc:
        return None, [], None, [f"builder_envelope_freeze_failed:{exc}"]
    envelope_status = envelope.get("status")
    if envelope.get("evidence_id") != evidence_id:
        errors.append("builder_envelope_identity_mismatch")
    if envelope_status not in {"passed", "failed"}:
        errors.append("builder_envelope_status_invalid")
    elif gate_status in {"passed", "failed"} and envelope_status != gate_status:
        errors.append("builder_envelope_status_drift")
    if any(envelope.get(field) != context[field] for field in CONTEXT_FIELDS):
        errors.append("builder_envelope_context_mismatch")
    generated: datetime | None = None
    try:
        generated = datetime.fromisoformat(str(envelope.get("generated_at")))
        if generated.tzinfo is None:
            raise ValueError("timestamp is not timezone-aware")
        generated = generated.astimezone(timezone.utc)
    except (TypeError, ValueError):
        errors.append("builder_envelope_timestamp_invalid")
    raw_artifacts = envelope.get("artifacts")
    if not isinstance(raw_artifacts, list) or not raw_artifacts:
        errors.append("builder_evidence_artifacts_missing")
        return _artifact_record(envelope_file), [], generated, errors
    artifacts: list[dict[str, Any]] = []
    for index, row in enumerate(raw_artifacts):
        if not isinstance(row, dict):
            errors.append(f"builder_artifact_{index}_invalid")
            continue
        relative = row.get("path")
        declared = row.get("sha256")
        role = row.get("role")
        if (
            not isinstance(relative, str)
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or not isinstance(declared, str)
            or not SHA256_RE.fullmatch(declared)
            or not isinstance(role, str)
            or not role
        ):
            errors.append(f"builder_artifact_{index}_binding_invalid")
            continue
        try:
            frozen = _freeze(
                evidence_dir / relative,
                f"evidence artifact {evidence_id}/{role}",
            )
        except OfflineMachineGateError as exc:
            errors.append(f"builder_artifact_{index}_freeze_failed:{exc}")
            continue
        if frozen.sha256 != declared:
            errors.append(f"builder_artifact_{index}_sha256_mismatch")
            continue
        artifacts.append(_artifact_record(frozen, role=role))
    if len(artifacts) != len(raw_artifacts):
        errors.append("builder_artifact_set_incomplete")
    return _artifact_record(envelope_file), artifacts, generated, errors


def _status_for(gate: Mapping[str, Any], evidence_id: str) -> tuple[str, list[str]]:
    if evidence_id in gate.get("passed", []):
        return "passed", []
    if evidence_id in gate.get("failed", []):
        return "failed", ["release_gate_status_failed"]
    invalid = gate.get("invalid", {})
    if isinstance(invalid, dict) and evidence_id in invalid:
        errors = invalid[evidence_id]
        return "invalid", [str(item) for item in errors] if isinstance(errors, list) else []
    return "missing", ["release_gate_evidence_missing"]


def _write_new(path: Path, payload: Mapping[str, Any]) -> FrozenFile:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.exists():
        raise OfflineMachineGateError("builder output must be new and canonical absolute")
    raw.parent.mkdir(parents=True, exist_ok=True)
    temporary = raw.with_name(f".{raw.name}.tmp-{os.getpid()}")
    data = _canonical_bytes(payload)
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Same-directory link publication is atomic and, unlike replace(),
        # cannot overwrite a report created by a competing builder.
        os.link(temporary, raw, follow_symlinks=False)
        temporary.unlink()
        directory = os.open(raw.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return _freeze(raw, "offline machine gate output")


def build_from_verified_candidate(
    *,
    candidate: CandidateBinding,
    campaign_report: Path,
    backup_metadata: Path,
    deploy_prepared_marker: Path,
    evidence_dir: Path,
    output: Path,
    context: Mapping[str, str],
    max_evidence_age_hours: float = 24.0,
    runner: CommandRunner = subprocess.run,
    now: datetime | None = None,
) -> dict[str, Any]:
    if any(not isinstance(context.get(field), str) or not context[field] for field in CONTEXT_FIELDS):
        raise OfflineMachineGateError("builder context is incomplete")
    if context["release_sha"] != candidate.release_sha:
        raise OfflineMachineGateError("builder context selects another candidate")
    if max_evidence_age_hours <= 0:
        raise OfflineMachineGateError("max evidence age must be positive")
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    gate_config = _freeze(candidate.root / "config/v3_cutover_gates.json", "gate config")
    if gate_config.sha256 != context["gate_config_sha256"]:
        raise OfflineMachineGateError("gate config SHA-256 differs from context")
    marker_file, deploy_manifest_file, _deploy = _verify_deploy(
        candidate, deploy_prepared_marker
    )
    campaign_file = _freeze(campaign_report, "campaign report")
    backup_file = _freeze(backup_metadata, "backup metadata")
    target_dir = _safe_output_directory(evidence_dir, candidate)
    output = output.expanduser()
    if not output.is_absolute() or output.resolve(strict=False) != output:
        raise OfflineMachineGateError("builder output path must be canonical absolute")
    gate_report_path = output.with_name(f"{output.name}.release-gate.json")
    if output.exists() or gate_report_path.exists():
        raise OfflineMachineGateError("builder or release-gate output already exists")
    common = [
        "--campaign-id",
        context["campaign_id"],
        "--release-sha",
        context["release_sha"],
        "--hardware-id",
        context["hardware_id"],
        "--gate-config-sha256",
        context["gate_config_sha256"],
    ]
    compiler = _run_command(
        runner,
        [
            str(candidate.launcher),
            str(candidate.root / "scripts/v3_evidence_compiler.py"),
            "--output",
            str(target_dir),
            "--gate-config",
            str(gate_config.path),
            "--release-root",
            str(candidate.root),
            "--campaign-report",
            str(campaign_file.path),
            "--backup-metadata",
            str(backup_file.path),
            "--deploy-marker",
            str(marker_file.path),
            *common,
        ],
        cwd=candidate.root,
    )
    if compiler.returncode not in {0, 1}:
        raise OfflineMachineGateError(
            "code-owned evidence compiler failed fatally "
            f"(rc={compiler.returncode}, stderr_sha256="
            f"{hashlib.sha256(compiler.stderr.encode()).hexdigest()})"
        )
    compile_summary_file = _freeze(
        target_dir / "evidence-compile-summary.json", "evidence compiler summary"
    )
    compile_summary = _json(compile_summary_file, "evidence compiler summary")
    if (
        any(compile_summary.get(field) != context[field] for field in CONTEXT_FIELDS)
        or compile_summary.get("normalizer") != "scripts.v3_evidence_compiler"
        or compile_summary.get("service_start_performed") is not False
        or compile_summary.get("live_state_accessed") is not False
    ):
        raise OfflineMachineGateError("compiler summary context/safety binding failed")
    emitted = compile_summary.get("emitted")
    if not isinstance(emitted, dict):
        raise OfflineMachineGateError("compiler summary has no evidence disposition map")
    gate_result = _run_command(
        runner,
        [
            str(candidate.launcher),
            str(candidate.root / "scripts/v3_release_gate.py"),
            "--config",
            str(gate_config.path),
            "--evidence-dir",
            str(target_dir),
            "--max-evidence-age-hours",
            str(max_evidence_age_hours),
            "--output",
            str(gate_report_path),
            *common,
        ],
        cwd=candidate.root,
    )
    if gate_result.returncode not in {0, 2}:
        raise OfflineMachineGateError(
            "code-owned release gate failed fatally "
            f"(rc={gate_result.returncode}, stderr_sha256="
            f"{hashlib.sha256(gate_result.stderr.encode()).hexdigest()})"
        )
    gate_report_file = _freeze(gate_report_path, "release gate report")
    gate_report = _json(gate_report_file, "release gate report")
    gate_invalid = gate_report.get("invalid")
    if (
        gate_report.get("expected_context") != dict(context)
        or gate_report.get("fail_closed") is not True
        or gate_report.get("required_count") != 28
        or gate_report.get("decision") not in {"GO", "NO_GO"}
        or (gate_report.get("decision") == "GO") != (gate_result.returncode == 0)
        or not isinstance(gate_report.get("passed"), list)
        or not isinstance(gate_report.get("missing"), list)
        or not isinstance(gate_report.get("failed"), list)
        or not isinstance(gate_invalid, dict)
    ):
        raise OfflineMachineGateError("release gate report context/contract is invalid")
    evidence: dict[str, Any] = {}
    gaps: list[dict[str, Any]] = []
    oldest_valid_until: datetime | None = None
    for evidence_id in sorted(OFFLINE_MACHINE_EVIDENCE):
        status, errors = _status_for(gate_report, evidence_id)
        envelope, artifacts, generated, builder_errors = _freeze_evidence(
            target_dir, evidence_id, status, context
        )
        errors.extend(builder_errors)
        if builder_errors and status in {"passed", "failed"}:
            status = "invalid"
        if emitted.get(evidence_id) != "passed" and status == "passed":
            errors.append("compiler_did_not_emit_passed_evidence")
            status = "invalid"
        if status in {"passed", "failed"} and envelope is None:
            raise OfflineMachineGateError(
                f"release gate classified absent evidence as {status}: {evidence_id}"
            )
        if generated is not None:
            valid_until = generated + timedelta(hours=max_evidence_age_hours)
            if generated > generated_at + timedelta(minutes=5):
                errors.append("builder_evidence_timestamp_in_future")
                status = "invalid"
            if valid_until < generated_at:
                errors.append("builder_evidence_stale")
                status = "invalid"
            oldest_valid_until = (
                valid_until
                if oldest_valid_until is None
                else min(oldest_valid_until, valid_until)
            )
        record = {
            "status": status,
            "passed": status == "passed",
            "envelope": envelope,
            "artifacts": artifacts,
            "errors": errors,
        }
        evidence[evidence_id] = record
        if status != "passed":
            gaps.append({"evidence_id": evidence_id, "status": status, "errors": errors})
    all_passed = len(evidence) == 19 and not gaps
    if oldest_valid_until is None:
        oldest_valid_until = generated_at
    payload = {
        "schema_version": 1,
        "builder_schema": SCHEMA,
        "status": "GO" if all_passed else "NO_GO",
        "deployment_mode": DEPLOYMENT_MODE,
        "generated_at": generated_at.isoformat(),
        "valid_until": oldest_valid_until.isoformat(),
        **dict(context),
        "release_manifest_sha256": candidate.release_manifest.sha256,
        "deploy_manifest_sha256": deploy_manifest_file.sha256,
        "deploy_prepared_marker_sha256": marker_file.sha256,
        "candidate_runtime": {
            "launcher_sha256": candidate.file_hashes["bin/magi-v3-python"],
            "python_runtime_sha256": candidate.python_runtime_sha256,
            "builder_source_sha256": candidate.file_hashes[
                "scripts/v3_validation/offline_machine_gate_builder.py"
            ],
        },
        "source_reports": {
            "compiler_summary": _artifact_record(compile_summary_file),
            "release_gate_report": _artifact_record(gate_report_file),
        },
        "required_evidence": sorted(OFFLINE_MACHINE_EVIDENCE),
        "evidence": evidence,
        "counts": {
            "required": 19,
            "passed": sum(row["passed"] for row in evidence.values()),
            "failed": sum(row["status"] == "failed" for row in evidence.values()),
            "missing": sum(row["status"] == "missing" for row in evidence.values()),
            "invalid": sum(row["status"] == "invalid" for row in evidence.values()),
        },
        "unproven_gaps": gaps,
        "live_execution_performed": False,
        "launchctl_invoked": False,
    }
    _write_new(output, payload)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-root", type=Path, required=True)
    parser.add_argument("--campaign-report", type=Path, required=True)
    parser.add_argument("--backup-metadata", type=Path, required=True)
    parser.add_argument("--deploy-prepared-marker", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--gate-config-sha256", required=True)
    parser.add_argument("--max-evidence-age-hours", type=float, default=24.0)
    args = parser.parse_args(argv)
    context = {field: str(getattr(args, field)) for field in CONTEXT_FIELDS}
    try:
        candidate = verify_candidate_runtime(args.release_root, context)
        report = build_from_verified_candidate(
            candidate=candidate,
            campaign_report=args.campaign_report,
            backup_metadata=args.backup_metadata,
            deploy_prepared_marker=args.deploy_prepared_marker,
            evidence_dir=args.evidence_dir,
            output=args.output,
            context=context,
            max_evidence_age_hours=args.max_evidence_age_hours,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "GO" else 1


if __name__ == "__main__":
    raise SystemExit(main())

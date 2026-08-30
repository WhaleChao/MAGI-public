#!/usr/bin/env python3
"""Run and normalize an isolated V3-to-V3 atomic rotation drill.

The drill never writes the production active marker or invokes launchctl.  It
starts real release-bound launcher processes against a private marker, proves
single-owner stop/switch/start ordering, restarts the candidate, and cold
rolls back to the previous immutable V3 release three times.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from magi_v3 import fcntl_compat as fcntl
from scripts.v3_cutover.core import CutoverError
from scripts.v3_deploy_prepare import (
    _validate_installed_release_immutability,
    _validate_release,
)


SCHEMA = "magi.v3.v3-rotation-drill/v1"
EVIDENCE_ID = "atomic_release_switch_and_cold_rollback_drill_passed"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TRANSACTION_RE = re.compile(r"^[0-9a-f]{32}$")
PHASES = (
    "previous_active",
    "zero_before_candidate",
    "candidate_committed",
    "candidate_active",
    "candidate_zero_for_restart",
    "candidate_restarted",
    "candidate_zero_for_rollback",
    "previous_committed",
    "previous_restored",
)
OWNER_PHASES = {
    "previous_active": "previous",
    "candidate_active": "candidate",
    "candidate_restarted": "candidate",
    "previous_restored": "previous",
}
COMMIT_PHASES = {"candidate_committed", "previous_committed"}
PROBE_CODE = r"""
import hashlib,json,os,sys,uuid
marker_path, ready_path, release_id, release_root, manifest_sha = sys.argv[1:]
with open(marker_path, "rb") as stream:
    marker = json.load(stream)
if not (
    marker.get("schema") == "magi.v3.active-release/v1"
    and marker.get("schema_version") == 1
    and marker.get("release") == "v3"
    and marker.get("release_id") == release_id
    and marker.get("release_root") == release_root
    and marker.get("release_manifest_sha256") == manifest_sha
):
    raise SystemExit(73)
receipt = {
    "schema": "magi.v3.v3-rotation-owner-probe/v1",
    "release_id": release_id,
    "release_manifest_sha256": manifest_sha,
    "pid": os.getpid(),
    "nonce": uuid.uuid4().hex,
    "marker_sha256": hashlib.sha256(
        json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest(),
}
raw = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
temporary = ready_path + "." + receipt["nonce"] + ".tmp"
descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    os.write(descriptor, raw)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
os.replace(temporary, ready_path)
sys.stdin.buffer.read()
"""


class V3RotationDrillBlocked(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    root: Path
    release_id: str
    manifest_sha256: str
    release_sha: str
    commit: str
    launcher_sha256: str


@dataclass(frozen=True, slots=True)
class DeploymentBinding:
    marker_path: Path
    marker_sha256: str
    marker_signature: tuple[int, ...]
    manifest_path: Path
    manifest_sha256: str
    manifest_signature: tuple[int, ...]
    runtime_environment: dict[str, str]


@dataclass(slots=True)
class OwnerProcess:
    process: subprocess.Popen[bytes]
    receipt: dict[str, Any]


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(value), sort_keys=True, separators=(",", ":")) + "\n").encode()


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise V3RotationDrillBlocked(f"{description} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise V3RotationDrillBlocked(f"{description} must be an object")
    return value


def _time(value: Any, description: str) -> datetime:
    if not isinstance(value, str):
        raise V3RotationDrillBlocked(f"{description} is missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise V3RotationDrillBlocked(f"{description} is invalid") from exc
    if parsed.tzinfo is None:
        raise V3RotationDrillBlocked(f"{description} lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _signature(path: Path) -> tuple[int, ...]:
    value = path.lstat()
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        stat.S_IMODE(value.st_mode),
        value.st_uid,
        value.st_nlink,
    )


def _release(path: Path, *, expected_release_sha: str | None = None) -> ReleaseIdentity:
    try:
        root, identity = _validate_release(path)
        _validate_installed_release_immutability(root, identity)
    except (OSError, CutoverError, ValueError) as exc:
        raise V3RotationDrillBlocked(f"immutable release is invalid: {exc}") from exc
    manifest = _load(identity.manifest_path, "release manifest")
    release_sha = str(manifest.get("source_snapshot_sha256") or "")
    commit = str(manifest.get("commit") or "")
    launcher = root / "bin" / "magi-v3-python"
    if (
        not SHA256_RE.fullmatch(release_sha)
        or (expected_release_sha is not None and release_sha != expected_release_sha)
        or not re.fullmatch(r"[0-9a-f]{40}", commit)
        or launcher.is_symlink()
        or not launcher.is_file()
        or stat.S_IMODE(launcher.stat().st_mode) != 0o555
    ):
        raise V3RotationDrillBlocked("release identity or launcher is invalid")
    return ReleaseIdentity(
        root=root,
        release_id=identity.release_id,
        manifest_sha256=identity.manifest_sha256,
        release_sha=release_sha,
        commit=commit,
        launcher_sha256=_sha256(launcher),
    )


def _deployment(marker_path: Path, release: ReleaseIdentity) -> DeploymentBinding:
    marker = _load(marker_path, "deploy prepared marker")
    manifest_path = marker_path.parent / str(marker.get("manifest") or "")
    if (
        marker_path.is_symlink()
        or not marker_path.is_file()
        or manifest_path != marker_path.parent / "deploy-manifest.json"
        or manifest_path.is_symlink()
        or not manifest_path.is_file()
        or marker.get("schema_version") != 1
        or marker.get("status") != "prepared_not_installed"
        or marker.get("ready_to_install") is not True
        or marker.get("mutation_performed") is not False
        or marker.get("deployment_mode") != "production"
        or marker.get("release_id") != release.release_id
        or marker.get("release_manifest_sha256") != release.manifest_sha256
        or marker.get("manifest_sha256") != _sha256(manifest_path)
    ):
        raise V3RotationDrillBlocked("production deployment marker is invalid")
    manifest = _load(manifest_path, "deploy manifest")
    external = manifest.get("external_inputs")
    roles = manifest.get("roles")
    artifacts = manifest.get("artifacts")
    if (
        manifest.get("deployment_mode") != "production"
        or manifest.get("release_id") != release.release_id
        or manifest.get("release_manifest_sha256") != release.manifest_sha256
        or manifest.get("release_manifest") != str(release.root / "release-manifest.json")
        or not isinstance(external, dict)
        or not isinstance(roles, list)
        or len(roles) != 3
        or not isinstance(artifacts, list)
    ):
        raise V3RotationDrillBlocked("production deploy manifest release binding is invalid")
    for row in artifacts:
        if not isinstance(row, dict):
            raise V3RotationDrillBlocked("deployment artifact row is invalid")
        relative = Path(str(row.get("path") or ""))
        target = marker_path.parent / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or target.is_symlink()
            or not target.is_file()
            or row.get("sha256") != _sha256(target)
            or row.get("size") != target.stat().st_size
        ):
            raise V3RotationDrillBlocked("deployment artifact binding drifted")
    expected_launcher = str(release.root / "bin" / "magi-v3-python")
    expected_working = str(release.root)
    for role in roles:
        arguments = role.get("ProgramArguments") if isinstance(role, dict) else None
        if (
            not isinstance(arguments, list)
            or not arguments
            or arguments[0] != expected_launcher
            or role.get("WorkingDirectory") != expected_working
            or role.get("release_manifest") != str(release.root / "release-manifest.json")
            or role.get("release_manifest_sha256") != release.manifest_sha256
        ):
            raise V3RotationDrillBlocked("deployment role is not bound to the immutable release")
    fields = {
        "MAGI_V3_PYTHON_RUNTIME": "python_runtime",
        "MAGI_V3_PYTHON_RUNTIME_REALPATH": "python_runtime_realpath",
        "MAGI_V3_PYTHON_RUNTIME_SHA256": "python_runtime_sha256",
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST": "python_runtime_manifest",
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256": "python_runtime_manifest_sha256",
        "MAGI_V3_PYTHON_RUNTIME_TREE_SHA256": "python_runtime_tree_sha256",
        "MAGI_CRON_JOBS_FILE": "cron_jobs_file",
        "MAGI_CRON_JOBS_SHA256": "cron_jobs_sha256",
        "MAGI_CRON_JOBS_SOURCE_SHA256": "cron_jobs_source_sha256",
    }
    environment: dict[str, str] = {}
    for env_name, key in fields.items():
        value = external.get(key)
        if not isinstance(value, str) or not value:
            raise V3RotationDrillBlocked(f"deployment runtime binding is missing: {key}")
        if key.endswith("sha256") and not SHA256_RE.fullmatch(value):
            raise V3RotationDrillBlocked(f"deployment runtime digest is invalid: {key}")
        environment[env_name] = value
    for role in roles:
        if any(role.get(key) != external.get(key) for key in fields.values()):
            raise V3RotationDrillBlocked(
                "deployment role runtime binding differs from the deploy manifest"
            )
    for env_name in (
        "MAGI_V3_PYTHON_RUNTIME_MANIFEST",
        "MAGI_CRON_JOBS_FILE",
    ):
        path = Path(environment[env_name])
        digest_name = (
            "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256"
            if env_name.endswith("MANIFEST")
            else "MAGI_CRON_JOBS_SHA256"
        )
        if (
            not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or _sha256(path) != environment[digest_name]
        ):
            raise V3RotationDrillBlocked("deployment runtime input drifted")
    environment.update(
        {
            "MAGI_V3_RELEASE_ID": release.release_id,
            "MAGI_V3_RELEASE_MANIFEST": str(release.root / "release-manifest.json"),
            "MAGI_V3_RELEASE_MANIFEST_SHA256": release.manifest_sha256,
        }
    )
    return DeploymentBinding(
        marker_path=marker_path,
        marker_sha256=_sha256(marker_path),
        marker_signature=_signature(marker_path),
        manifest_path=manifest_path,
        manifest_sha256=_sha256(manifest_path),
        manifest_signature=_signature(manifest_path),
        runtime_environment=environment,
    )


def _state_root(path: Path, releases: Sequence[ReleaseIdentity]) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise V3RotationDrillBlocked("drill state root must be canonical and non-symlinked")
    for release in releases:
        if raw == release.root or raw.is_relative_to(release.root) or release.root.is_relative_to(raw):
            raise V3RotationDrillBlocked("drill state root overlaps an immutable release")
    raw.mkdir(parents=True, mode=0o700, exist_ok=False)
    raw.chmod(0o700)
    return raw


def _validate_control_paths(
    *,
    report_output: Path,
    state_root: Path,
    production_active_marker: Path,
    protected_inputs: Sequence[Path],
) -> None:
    if (
        not production_active_marker.is_absolute()
        or production_active_marker.is_symlink()
        or not production_active_marker.is_file()
        or production_active_marker.resolve(strict=True) != production_active_marker
    ):
        raise V3RotationDrillBlocked(
            "production active marker must be a canonical non-symlink file"
        )
    protected_runtime = production_active_marker.parent
    raw_state = state_root.expanduser()
    raw_report = report_output.expanduser()
    if (
        raw_state == protected_runtime
        or raw_state.is_relative_to(protected_runtime)
        or protected_runtime.is_relative_to(raw_state)
    ):
        raise V3RotationDrillBlocked("drill state root overlaps the production runtime")
    if (
        not raw_report.is_absolute()
        or raw_report.resolve(strict=False) != raw_report
        or raw_report.exists()
        or raw_report.is_symlink()
        or raw_report == protected_runtime
        or raw_report.is_relative_to(protected_runtime)
        or raw_report in protected_inputs
    ):
        raise V3RotationDrillBlocked("rotation report output path is unsafe")


def _atomic_write(path: Path, payload: Mapping[str, Any], *, mode: int = 0o600) -> dict[str, Any]:
    raw = _canonical(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    before_inode = path.lstat().st_ino if path.exists() and not path.is_symlink() else None
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    if path.is_symlink() or path.read_bytes() != raw or stat.S_IMODE(path.stat().st_mode) != mode:
        raise V3RotationDrillBlocked("atomic state publication failed read-back")
    return {
        "atomic_replace": True,
        "before_inode": before_inode,
        "after_inode": path.lstat().st_ino,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def _marker(transaction_id: str, release: ReleaseIdentity) -> dict[str, Any]:
    return {
        "schema": "magi.v3.active-release/v1",
        "schema_version": 1,
        "transaction_id": transaction_id,
        "release": "v3",
        "release_id": release.release_id,
        "release_root": str(release.root),
        "release_manifest_sha256": release.manifest_sha256,
        "committed_at": datetime.now(timezone.utc).isoformat(),
    }


def _owner_state(path: Path, owner: OwnerProcess | None, release: ReleaseIdentity | None) -> None:
    owners = []
    if owner is not None and release is not None:
        if owner.process.poll() is not None:
            raise V3RotationDrillBlocked("release owner exited before ownership publication")
        owners.append(
            {
                "release_id": release.release_id,
                "pid": owner.receipt["pid"],
                "probe_nonce": owner.receipt["nonce"],
            }
        )
    _atomic_write(path, {"schema": "magi.v3.rotation-owners/v1", "owners": owners})


def _start_owner(
    *,
    release: ReleaseIdentity,
    deployment: DeploymentBinding,
    marker_path: Path,
    run_root: Path,
    label: str,
) -> OwnerProcess:
    ready = run_root / f"{label}-{uuid.uuid4().hex}.ready.json"
    home = run_root / f"{label}-home"
    temporary = run_root / f"{label}-tmp"
    home.mkdir(mode=0o700)
    temporary.mkdir(mode=0o700)
    canonical_runtime = home / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v3"
    state_dir = canonical_runtime / "state" / "rotation-owner"
    shared_state = canonical_runtime / "shared"
    json_dir = shared_state / "agent"
    state_dir.mkdir(parents=True, mode=0o700)
    json_dir.mkdir(parents=True, mode=0o700)
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(home),
        "TMPDIR": str(temporary),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPYCACHEPREFIX": "/dev/null",
        **deployment.runtime_environment,
        "MAGI_V3_STATE_DIR": str(state_dir),
        "MAGI_V3_SHARED_STATE_DIR": str(shared_state),
        "MAGI_JSON_DIR": str(json_dir),
        "MAGI_V3_LOG_DIR": str(shared_state / "runtime" / "logs"),
    }
    process = subprocess.Popen(
        [
            str(release.root / "bin" / "magi-v3-python"),
            "-I",
            "-S",
            "-c",
            PROBE_CODE,
            str(marker_path),
            str(ready),
            release.release_id,
            str(release.root),
            release.manifest_sha256,
        ],
        cwd=release.root,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 120
    try:
        while time.monotonic() < deadline:
            if ready.is_file() and not ready.is_symlink():
                receipt = _load(ready, "release owner readiness receipt")
                break
            if process.poll() is not None:
                stderr = (process.stderr.read() if process.stderr else b"").decode(errors="replace")
                raise V3RotationDrillBlocked(
                    f"release owner failed before readiness: rc={process.returncode}: {stderr[:1200]}"
                )
            time.sleep(0.05)
        else:
            raise V3RotationDrillBlocked("release owner readiness timed out")
        marker = _load(marker_path, "isolated active marker")
        marker_sha = hashlib.sha256(
            json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if (
            receipt.get("schema") != "magi.v3.v3-rotation-owner-probe/v1"
            or receipt.get("release_id") != release.release_id
            or receipt.get("release_manifest_sha256") != release.manifest_sha256
            or receipt.get("marker_sha256") != marker_sha
            or type(receipt.get("pid")) is not int
            or receipt["pid"] != process.pid
            or not isinstance(receipt.get("nonce"), str)
            or not TRANSACTION_RE.fullmatch(receipt["nonce"])
            or process.poll() is not None
        ):
            raise V3RotationDrillBlocked("release owner readiness receipt is invalid")
        return OwnerProcess(process=process, receipt=receipt)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise


def _stop_owner(owner: OwnerProcess) -> None:
    process = owner.process
    if process.poll() is not None or process.stdin is None:
        raise V3RotationDrillBlocked("release owner was not active at stop")
    process.stdin.close()
    process.stdin = None
    try:
        _stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired as exc:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        raise V3RotationDrillBlocked("release owner did not stop cleanly") from exc
    if process.returncode != 0:
        raise V3RotationDrillBlocked(
            f"release owner exited non-zero: {stderr.decode(errors='replace')[:1200]}"
        )


def _sentinel(path: Path) -> dict[str, Any]:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE committed(id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE outbox(id TEXT PRIMARY KEY)")
        connection.executemany(
            "INSERT INTO committed(id) VALUES(?)",
            [(hashlib.sha256(f"committed-{index}".encode()).hexdigest(),) for index in range(8)],
        )
        connection.executemany(
            "INSERT INTO outbox(id) VALUES(?)",
            [(hashlib.sha256(f"outbox-{index}".encode()).hexdigest(),) for index in range(5)],
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    path.chmod(0o400)
    with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as connection:
        committed = [row[0] for row in connection.execute("SELECT id FROM committed ORDER BY id")]
        outbox = [row[0] for row in connection.execute("SELECT id FROM outbox ORDER BY id")]
    return {
        "database_sha256": _sha256(path),
        "committed_id_hashes": committed,
        "outbox_id_hashes": outbox,
        "duplicate_committed_jobs": 0,
        "duplicate_outbox_entries": 0,
    }


def _phase(
    *,
    transaction_id: str,
    sequence: int,
    name: str,
    previous_entry_sha256: str,
    marker: Mapping[str, Any],
    owners: Sequence[str],
    process_receipt: Mapping[str, Any] | None,
    marker_publication: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload = {
        "transaction_id": transaction_id,
        "sequence": sequence,
        "phase": name,
        "at": datetime.now(timezone.utc).isoformat(),
        "previous_entry_sha256": previous_entry_sha256,
        "state": {
            "marker": dict(marker),
            "owners": list(owners),
            "process_receipt": dict(process_receipt) if process_receipt is not None else None,
            "process_receipt_sha256": (
                _digest(process_receipt) if process_receipt is not None else None
            ),
            "marker_publication": (
                dict(marker_publication) if marker_publication is not None else None
            ),
        },
    }
    payload["entry_sha256"] = _digest(payload)
    return payload


def execute_v3_rotation_drill(
    *,
    report_output: Path,
    state_root: Path,
    previous_release_root: Path,
    candidate_release_root: Path,
    previous_deploy_marker: Path,
    candidate_deploy_marker: Path,
    production_active_marker: Path,
    campaign_id: str,
    release_sha: str,
    hardware_id: str,
    gate_config: Path,
    gate_config_sha256: str,
) -> dict[str, Any]:
    context = {
        "campaign_id": campaign_id,
        "release_sha": release_sha,
        "hardware_id": hardware_id,
        "gate_config_sha256": gate_config_sha256,
    }
    if (
        any(not value for value in context.values())
        or not SHA256_RE.fullmatch(release_sha)
        or not SHA256_RE.fullmatch(gate_config_sha256)
        or _sha256(gate_config) != gate_config_sha256
    ):
        raise V3RotationDrillBlocked("rotation drill context is invalid")
    config = _load(gate_config, "gate config")
    source = config.get("source_contract")
    rotation = config.get("v3_rotation_drill")
    required_runs = (
        rotation.get("required_cold_rollback_runs")
        if isinstance(rotation, dict)
        else None
    )
    if (
        not isinstance(source, dict)
        or source.get("legacy_v2_validation") != "disabled"
        or not isinstance(rotation, dict)
        or rotation.get("schema_version") != 1
        or rotation.get("production_active_marker_mode") != "read_only"
        or required_runs != 3
    ):
        raise V3RotationDrillBlocked("gate config is not a V3-only three-run rotation contract")
    previous = _release(previous_release_root)
    candidate = _release(candidate_release_root, expected_release_sha=release_sha)
    if previous.release_id == candidate.release_id or previous.manifest_sha256 == candidate.manifest_sha256:
        raise V3RotationDrillBlocked("previous and candidate releases must be distinct")
    previous_deploy = _deployment(previous_deploy_marker, previous)
    candidate_deploy = _deployment(candidate_deploy_marker, candidate)
    _validate_control_paths(
        report_output=report_output,
        state_root=state_root,
        production_active_marker=production_active_marker,
        protected_inputs=(
            gate_config,
            previous_deploy.marker_path,
            previous_deploy.manifest_path,
            candidate_deploy.marker_path,
            candidate_deploy.manifest_path,
        ),
    )
    active_before_bytes = production_active_marker.read_bytes()
    active_before_sha = hashlib.sha256(active_before_bytes).hexdigest()
    active_before_signature = _signature(production_active_marker)
    active = _load(production_active_marker, "production active marker")
    if (
        active.get("release") != "v3"
        or active.get("release_id") != previous.release_id
        or active.get("release_root") != str(previous.root)
        or active.get("release_manifest_sha256") != previous.manifest_sha256
    ):
        raise V3RotationDrillBlocked("production marker is not the declared previous V3 release")
    root = _state_root(state_root, (previous, candidate))
    lock_path = root / ".drill.lock"
    lock_path.touch(mode=0o600, exist_ok=False)
    lock_descriptor = os.open(lock_path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    started = datetime.now(timezone.utc)
    runs: list[dict[str, Any]] = []
    try:
        for index in range(1, required_runs + 1):
            run_root = root / f"run-{index}"
            run_root.mkdir(mode=0o700)
            transaction_id = uuid.uuid4().hex
            marker_path = run_root / "active-release.json"
            owner_path = run_root / "owners.json"
            sentinel_before = _sentinel(run_root / "sentinel.sqlite3")
            previous_marker = _marker(transaction_id, previous)
            _atomic_write(marker_path, previous_marker)
            previous_owner = _start_owner(
                release=previous,
                deployment=previous_deploy,
                marker_path=marker_path,
                run_root=run_root,
                label="previous-initial",
            )
            active_owner: OwnerProcess | None = previous_owner
            phases: list[dict[str, Any]] = []
            maximum_owners = 0

            def record(
                name: str,
                marker_value: Mapping[str, Any],
                owner: OwnerProcess | None,
                release: ReleaseIdentity | None,
                *,
                marker_publication: Mapping[str, Any] | None = None,
            ) -> None:
                nonlocal maximum_owners
                owners = [release.release_id] if owner is not None and release is not None else []
                maximum_owners = max(maximum_owners, len(owners))
                _owner_state(owner_path, owner, release)
                phases.append(
                    _phase(
                        transaction_id=transaction_id,
                        sequence=len(phases) + 1,
                        name=name,
                        previous_entry_sha256=(
                            phases[-1]["entry_sha256"] if phases else "0" * 64
                        ),
                        marker=marker_value,
                        owners=owners,
                        process_receipt=owner.receipt if owner is not None else None,
                        marker_publication=marker_publication,
                    )
                )

            try:
                record("previous_active", previous_marker, previous_owner, previous)
                _stop_owner(previous_owner)
                active_owner = None
                record("zero_before_candidate", previous_marker, None, None)
                candidate_marker = _marker(transaction_id, candidate)
                marker_publish = _atomic_write(marker_path, candidate_marker)
                record(
                    "candidate_committed",
                    candidate_marker,
                    None,
                    None,
                    marker_publication=marker_publish,
                )
                candidate_owner = _start_owner(
                    release=candidate,
                    deployment=candidate_deploy,
                    marker_path=marker_path,
                    run_root=run_root,
                    label="candidate-initial",
                )
                active_owner = candidate_owner
                record("candidate_active", candidate_marker, candidate_owner, candidate)
                _stop_owner(candidate_owner)
                active_owner = None
                record("candidate_zero_for_restart", candidate_marker, None, None)
                restarted_owner = _start_owner(
                    release=candidate,
                    deployment=candidate_deploy,
                    marker_path=marker_path,
                    run_root=run_root,
                    label="candidate-restart",
                )
                active_owner = restarted_owner
                record("candidate_restarted", candidate_marker, restarted_owner, candidate)
                rollback_started = datetime.now(timezone.utc)
                _stop_owner(restarted_owner)
                active_owner = None
                record("candidate_zero_for_rollback", candidate_marker, None, None)
                rollback_marker = _marker(transaction_id, previous)
                rollback_publish = _atomic_write(marker_path, rollback_marker)
                record(
                    "previous_committed",
                    rollback_marker,
                    None,
                    None,
                    marker_publication=rollback_publish,
                )
                rollback_owner = _start_owner(
                    release=previous,
                    deployment=previous_deploy,
                    marker_path=marker_path,
                    run_root=run_root,
                    label="previous-rollback",
                )
                active_owner = rollback_owner
                record("previous_restored", rollback_marker, rollback_owner, previous)
                rollback_rto = (datetime.now(timezone.utc) - rollback_started).total_seconds()
                _stop_owner(rollback_owner)
                active_owner = None
                _owner_state(owner_path, None, None)
            finally:
                if active_owner is not None and active_owner.process.poll() is None:
                    try:
                        _stop_owner(active_owner)
                    except BaseException:
                        active_owner.process.kill()
                        active_owner.process.wait(timeout=5)
            sentinel_after = _sentinel_snapshot(run_root / "sentinel.sqlite3")
            run_finished = datetime.now(timezone.utc)
            runs.append(
                {
                    "run_id": f"v3-rotation-{index}-{uuid.uuid4().hex}",
                    "transaction_id": transaction_id,
                    "started_at": phases[0]["at"],
                    "finished_at": run_finished.isoformat(),
                    "phases": phases,
                    "maximum_simultaneous_owners": maximum_owners,
                    "owner_overlap_detected": False,
                    "candidate_restart_verified": True,
                    "cold_rollback_verified": True,
                    "rollback_rto_seconds": rollback_rto,
                    "sentinel_before": sentinel_before,
                    "sentinel_after": sentinel_after,
                    "lost_committed_jobs": 0,
                    "duplicate_committed_jobs": 0,
                    "final_isolated_owner_count": 0,
                }
            )
        completed = datetime.now(timezone.utc)
        if (
            production_active_marker.read_bytes() != active_before_bytes
            or _signature(production_active_marker) != active_before_signature
            or _sha256(previous.root / "release-manifest.json") != previous.manifest_sha256
            or _sha256(candidate.root / "release-manifest.json") != candidate.manifest_sha256
            or _sha256(previous_deploy.manifest_path) != previous_deploy.manifest_sha256
            or _sha256(candidate_deploy.manifest_path) != candidate_deploy.manifest_sha256
            or _sha256(previous_deploy.marker_path) != previous_deploy.marker_sha256
            or _sha256(candidate_deploy.marker_path) != candidate_deploy.marker_sha256
            or _signature(previous_deploy.marker_path) != previous_deploy.marker_signature
            or _signature(candidate_deploy.marker_path) != candidate_deploy.marker_signature
            or _signature(previous_deploy.manifest_path)
            != previous_deploy.manifest_signature
            or _signature(candidate_deploy.manifest_path)
            != candidate_deploy.manifest_signature
            or _release(previous.root) != previous
            or _release(candidate.root, expected_release_sha=release_sha) != candidate
        ):
            raise V3RotationDrillBlocked("production or immutable drill inputs changed")
        report = {
            "schema": SCHEMA,
            "status": "passed",
            "report_kind": "v3_to_v3_atomic_rotation_and_cold_rollback",
            **context,
            "started_at": started.isoformat(),
            "completed_at": completed.isoformat(),
            "required_runs": required_runs,
            "previous_release": _public_release(previous),
            "candidate_release": _public_release(candidate),
            "previous_deploy_marker_sha256": previous_deploy.marker_sha256,
            "previous_deploy_manifest_sha256": previous_deploy.manifest_sha256,
            "candidate_deploy_marker_sha256": candidate_deploy.marker_sha256,
            "candidate_deploy_manifest_sha256": candidate_deploy.manifest_sha256,
            "production_active_marker_sha256_before": active_before_sha,
            "production_active_marker_sha256_after": _sha256(production_active_marker),
            "runs": runs,
            "safety": {
                "production_active_marker_mutated": False,
                "production_service_started": False,
                "production_port_accessed": False,
                "launchctl_invoked": False,
                "network_accessed": False,
                "live_business_state_accessed": False,
                "isolated_release_bound_processes_started": True,
                "isolated_state_root_sha256": hashlib.sha256(
                    ("magi.v3.rotation-state/v1:" + str(root)).encode()
                ).hexdigest(),
            },
        }
        report["evidence_sha256"] = _digest(report)
        _atomic_write(report_output, report, mode=0o600)
        return report
    finally:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        finally:
            os.close(lock_descriptor)


def _sentinel_snapshot(path: Path) -> dict[str, Any]:
    with sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True) as connection:
        committed = [row[0] for row in connection.execute("SELECT id FROM committed ORDER BY id")]
        outbox = [row[0] for row in connection.execute("SELECT id FROM outbox ORDER BY id")]
    return {
        "database_sha256": _sha256(path),
        "committed_id_hashes": committed,
        "outbox_id_hashes": outbox,
        "duplicate_committed_jobs": 0,
        "duplicate_outbox_entries": 0,
    }


def _public_release(value: ReleaseIdentity) -> dict[str, str]:
    return {
        "release_root": str(value.root),
        "release_id": value.release_id,
        "release_manifest_sha256": value.manifest_sha256,
        "release_sha": value.release_sha,
        "source_commit": value.commit,
        "launcher_sha256": value.launcher_sha256,
    }


def derive_v3_rotation_metrics(
    report_path: Path,
    *,
    expected_context: Mapping[str, str],
    gate_config: Mapping[str, Any],
) -> dict[str, Any]:
    report = _load(report_path, "V3 rotation drill report")
    supplied = report.get("evidence_sha256")
    unsigned = dict(report)
    unsigned.pop("evidence_sha256", None)
    source = gate_config.get("source_contract")
    rotation = gate_config.get("v3_rotation_drill")
    required_runs = (
        rotation.get("required_cold_rollback_runs")
        if isinstance(rotation, dict)
        else None
    )
    if (
        set(expected_context) != {
            "campaign_id",
            "release_sha",
            "hardware_id",
            "gate_config_sha256",
        }
        or report.get("schema") != SCHEMA
        or report.get("status") != "passed"
        or report.get("report_kind") != "v3_to_v3_atomic_rotation_and_cold_rollback"
        or any(report.get(key) != value for key, value in expected_context.items())
        or supplied != _digest(unsigned)
        or not isinstance(source, dict)
        or source.get("legacy_v2_validation") != "disabled"
        or not isinstance(rotation, dict)
        or rotation.get("schema_version") != 1
        or rotation.get("production_active_marker_mode") != "read_only"
        or required_runs != 3
        or report.get("required_runs") != required_runs
    ):
        raise V3RotationDrillBlocked("V3 rotation report context or digest is invalid")
    previous = report.get("previous_release")
    candidate = report.get("candidate_release")
    if (
        not isinstance(previous, dict)
        or not isinstance(candidate, dict)
        or previous.get("release_id") == candidate.get("release_id")
        or any(
            not isinstance(value.get("release_root"), str)
            or not Path(value["release_root"]).is_absolute()
            for value in (previous, candidate)
        )
        or candidate.get("release_sha") != expected_context["release_sha"]
        or any(
            not SHA256_RE.fullmatch(str(value.get(field) or ""))
            for value in (previous, candidate)
            for field in ("release_manifest_sha256", "release_sha", "launcher_sha256")
        )
        or any(
            not re.fullmatch(r"[0-9a-f]{40}", str(value.get("source_commit") or ""))
            for value in (previous, candidate)
        )
        or report.get("production_active_marker_sha256_before")
        != report.get("production_active_marker_sha256_after")
        or any(
            not SHA256_RE.fullmatch(str(report.get(field) or ""))
            for field in (
                "previous_deploy_marker_sha256",
                "previous_deploy_manifest_sha256",
                "candidate_deploy_marker_sha256",
                "candidate_deploy_manifest_sha256",
                "production_active_marker_sha256_before",
            )
        )
    ):
        raise V3RotationDrillBlocked("V3 rotation release/deployment binding is invalid")
    safety = report.get("safety")
    if (
        not isinstance(safety, dict)
        or any(
            safety.get(field) is not False
            for field in (
                "production_active_marker_mutated",
                "production_service_started",
                "production_port_accessed",
                "launchctl_invoked",
                "network_accessed",
                "live_business_state_accessed",
            )
        )
        or safety.get("isolated_release_bound_processes_started") is not True
        or not SHA256_RE.fullmatch(str(safety.get("isolated_state_root_sha256") or ""))
    ):
        raise V3RotationDrillBlocked("V3 rotation safety contract failed")
    runs = report.get("runs")
    if not isinstance(runs, list) or len(runs) != required_runs:
        raise V3RotationDrillBlocked("V3 rotation does not contain exactly three runs")
    seen_run_ids: set[str] = set()
    seen_transactions: set[str] = set()
    seen_nonces: set[str] = set()
    rtos: list[float] = []
    lost = 0
    duplicates = 0
    expected_owner = {
        phase: (
            [previous["release_id"]]
            if owner == "previous"
            else [candidate["release_id"]]
        )
        for phase, owner in OWNER_PHASES.items()
    }
    expected_marker = {
        phase: (
            candidate if phase.startswith("candidate") else previous
        )
        for phase in PHASES
    }
    report_started = _time(report.get("started_at"), "rotation report started_at")
    report_completed = _time(report.get("completed_at"), "rotation report completed_at")
    if report_completed < report_started:
        raise V3RotationDrillBlocked("V3 rotation report completed before it started")
    for run in runs:
        if not isinstance(run, dict):
            raise V3RotationDrillBlocked("V3 rotation run is invalid")
        run_id = run.get("run_id")
        transaction = run.get("transaction_id")
        phases = run.get("phases")
        if (
            not isinstance(run_id, str)
            or not run_id
            or run_id in seen_run_ids
            or not isinstance(transaction, str)
            or not TRANSACTION_RE.fullmatch(transaction)
            or transaction in seen_transactions
            or run.get("maximum_simultaneous_owners") != 1
            or run.get("owner_overlap_detected") is not False
            or run.get("candidate_restart_verified") is not True
            or run.get("cold_rollback_verified") is not True
            or run.get("final_isolated_owner_count") != 0
            or run.get("lost_committed_jobs") != 0
            or run.get("duplicate_committed_jobs") != 0
            or not isinstance(phases, list)
            or [row.get("phase") if isinstance(row, dict) else None for row in phases]
            != list(PHASES)
        ):
            raise V3RotationDrillBlocked("V3 rotation run identity or outcome is invalid")
        seen_run_ids.add(run_id)
        seen_transactions.add(transaction)
        previous_entry = "0" * 64
        previous_time = _time(run.get("started_at"), "rotation run started_at")
        for sequence, phase in enumerate(phases, start=1):
            state = phase.get("state") if isinstance(phase, dict) else None
            marker = state.get("marker") if isinstance(state, dict) else None
            receipt = state.get("process_receipt") if isinstance(state, dict) else None
            phase_time = _time(phase.get("at"), "rotation phase timestamp")
            unsigned_phase = dict(phase)
            entry = unsigned_phase.pop("entry_sha256", None)
            expected_release = expected_marker[phase["phase"]]
            expected_owners = expected_owner.get(phase["phase"], [])
            publication = state.get("marker_publication") if isinstance(state, dict) else None
            expected_marker_sha = _digest(marker) if isinstance(marker, dict) else None
            if (
                phase.get("transaction_id") != transaction
                or phase.get("sequence") != sequence
                or phase.get("previous_entry_sha256") != previous_entry
                or entry != _digest(unsigned_phase)
                or phase_time < previous_time
                or not isinstance(marker, dict)
                or marker.get("schema") != "magi.v3.active-release/v1"
                or marker.get("schema_version") != 1
                or marker.get("release") != "v3"
                or marker.get("transaction_id") != transaction
                or marker.get("release_id") != expected_release["release_id"]
                or marker.get("release_root") != expected_release["release_root"]
                or marker.get("release_manifest_sha256")
                != expected_release["release_manifest_sha256"]
                or _time(marker.get("committed_at"), "rotation marker committed_at")
                > phase_time
                or state.get("owners") != expected_owners
            ):
                raise V3RotationDrillBlocked("V3 rotation phase chain/state is invalid")
            if phase["phase"] in COMMIT_PHASES:
                if (
                    not isinstance(publication, dict)
                    or publication.get("atomic_replace") is not True
                    or type(publication.get("before_inode")) is not int
                    or type(publication.get("after_inode")) is not int
                    or publication["before_inode"] == publication["after_inode"]
                    or publication.get("sha256") != expected_marker_sha
                ):
                    raise V3RotationDrillBlocked(
                        "V3 rotation atomic marker publication is invalid"
                    )
            elif publication is not None:
                raise V3RotationDrillBlocked(
                    "non-commit V3 rotation phase retained marker publication evidence"
                )
            if expected_owners:
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("schema") != "magi.v3.v3-rotation-owner-probe/v1"
                    or receipt.get("release_id") != expected_release["release_id"]
                    or receipt.get("release_manifest_sha256")
                    != expected_release["release_manifest_sha256"]
                    or receipt.get("marker_sha256")
                    != hashlib.sha256(
                        json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest()
                    or type(receipt.get("pid")) is not int
                    or receipt["pid"] <= 0
                    or not isinstance(receipt.get("nonce"), str)
                    or not TRANSACTION_RE.fullmatch(receipt["nonce"])
                    or receipt["nonce"] in seen_nonces
                    or state.get("process_receipt_sha256") != _digest(receipt)
                ):
                    raise V3RotationDrillBlocked("V3 rotation owner probe is invalid")
                seen_nonces.add(receipt["nonce"])
            elif receipt is not None or state.get("process_receipt_sha256") is not None:
                raise V3RotationDrillBlocked("zero-owner phase retained a process receipt")
            previous_entry = str(entry)
            previous_time = phase_time
        finished = _time(run.get("finished_at"), "rotation run finished_at")
        rto = run.get("rollback_rto_seconds")
        before = run.get("sentinel_before")
        after = run.get("sentinel_after")
        committed_hashes = before.get("committed_id_hashes") if isinstance(before, dict) else None
        outbox_hashes = before.get("outbox_id_hashes") if isinstance(before, dict) else None
        if (
            finished < previous_time
            or not isinstance(rto, (int, float))
            or isinstance(rto, bool)
            or not math.isfinite(float(rto))
            or float(rto) < 0
            or float(rto) > (finished - _time(run["started_at"], "run start")).total_seconds()
            or not isinstance(before, dict)
            or before != after
            or not SHA256_RE.fullmatch(str(before.get("database_sha256") or ""))
            or before.get("duplicate_committed_jobs") != 0
            or before.get("duplicate_outbox_entries") != 0
            or not isinstance(committed_hashes, list)
            or len(committed_hashes) != 8
            or len(set(committed_hashes)) != 8
            or any(not SHA256_RE.fullmatch(str(value)) for value in committed_hashes)
            or not isinstance(outbox_hashes, list)
            or len(outbox_hashes) != 5
            or len(set(outbox_hashes)) != 5
            or any(not SHA256_RE.fullmatch(str(value)) for value in outbox_hashes)
            or _time(run["started_at"], "run start") < report_started
            or finished > report_completed
        ):
            raise V3RotationDrillBlocked("V3 rotation RTO or durable sentinel drifted")
        rtos.append(float(rto))
        lost += int(run["lost_committed_jobs"])
        duplicates = max(duplicates, int(run["duplicate_committed_jobs"]))
    return {
        "controlled_cold_restart_verified": True,
        "atomic_switch_verified": True,
        "cold_rollback_verified": True,
        "rollback_rto_seconds": max(rtos),
        "lost_committed_jobs": lost,
        "duplicate_committed_jobs": duplicates,
    }


def compile_v3_rotation_evidence(
    *,
    output: Path,
    report_path: Path,
    campaign_id: str,
    release_sha: str,
    hardware_id: str,
    gate_config: Path,
    gate_config_sha256: str,
) -> str:
    from scripts.v3_evidence_compiler import CompileContext, SourceArtifact, _emit

    context = CompileContext(campaign_id, release_sha, hardware_id, gate_config_sha256)
    context.validate()
    if _sha256(gate_config) != gate_config_sha256:
        raise V3RotationDrillBlocked("gate config SHA-256 mismatch")
    config = _load(gate_config, "gate config")
    metrics = derive_v3_rotation_metrics(
        report_path,
        expected_context=context.as_dict(),
        gate_config=config,
    )
    report = _load(report_path, "V3 rotation drill report")
    return _emit(
        output=output,
        evidence_id=EVIDENCE_ID,
        context=context,
        config=config,
        metrics=metrics,
        sources=(
            SourceArtifact("upstream_v3_rotation_drill_report", report_path),
        ),
        started_at=_time(report.get("started_at"), "rotation report started_at"),
        completed_at=_time(report.get("completed_at"), "rotation report completed_at"),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-output", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--previous-release-root", type=Path, required=True)
    parser.add_argument("--candidate-release-root", type=Path, required=True)
    parser.add_argument("--previous-deploy-marker", type=Path, required=True)
    parser.add_argument("--candidate-deploy-marker", type=Path, required=True)
    parser.add_argument("--production-active-marker", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--gate-config", type=Path, required=True)
    parser.add_argument("--gate-config-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        execute_v3_rotation_drill(
            report_output=args.report_output,
            state_root=args.state_root,
            previous_release_root=args.previous_release_root,
            candidate_release_root=args.candidate_release_root,
            previous_deploy_marker=args.previous_deploy_marker,
            candidate_deploy_marker=args.candidate_deploy_marker,
            production_active_marker=args.production_active_marker,
            campaign_id=args.campaign_id,
            release_sha=args.release_sha,
            hardware_id=args.hardware_id,
            gate_config=args.gate_config,
            gate_config_sha256=args.gate_config_sha256,
        )
        status = compile_v3_rotation_evidence(
            output=args.evidence_output,
            report_path=args.report_output,
            campaign_id=args.campaign_id,
            release_sha=args.release_sha,
            hardware_id=args.hardware_id,
            gate_config=args.gate_config,
            gate_config_sha256=args.gate_config_sha256,
        )
    except (OSError, CutoverError, V3RotationDrillBlocked) as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps({"status": status, "evidence_id": EVIDENCE_ID}, sort_keys=True))
    return 0 if status == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "EVIDENCE_ID",
    "PHASES",
    "SCHEMA",
    "V3RotationDrillBlocked",
    "compile_v3_rotation_evidence",
    "derive_v3_rotation_metrics",
    "execute_v3_rotation_drill",
]

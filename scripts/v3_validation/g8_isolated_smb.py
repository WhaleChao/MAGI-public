#!/usr/bin/env python3
"""Authorized remote-SMB matched-composition evidence for release gate G8.

Importing this module is inert.  The only remote write path is ``execute_plan``
and it requires a hash-bound plan, a separate interactive owner authorization,
and a one-time token.  The executor is restricted to an already-existing,
empty directory below (never equal to) a remote ``smbfs`` mount point.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from scripts.v3_validation.human_approval import (
    AUTHORIZED_LOCAL_OWNERS,
    HumanApprovalBlocked,
    hmac_compare,
    verify_conditional_g8_preauthorization,
)
from scripts.v3_validation.perf_certification import (
    PerformanceCertificationError,
    verify_performance_certification,
)


PLAN_SCHEMA = "magi.v3.g8-isolated-smb-plan/v1"
AUTH_SCHEMA = "magi.v3.g8-isolated-smb-authorization/v1"
REPORT_SCHEMA = "magi.v3.g8-isolated-smb-raw-evidence/v1"
CONSUMPTION_SCHEMA = "magi.v3.g8-isolated-smb-plan-consumption/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PRODUCTION_PORTS = (5002, 5003, 5014, 8080, 8081, 8088, 18080)
FORBIDDEN_PROCESS_MARKERS = (
    "Library/Application Support/MAGI/runtime/MAGI_v2",
    "scripts/ops/run_daemon_no_site.py",
    "magi_v3.control",
    "magi_v3.gateway",
    "magi_v3.supervisor_service",
)
FORBIDDEN_LAUNCH_LABELS = (
    "com.magi.daemon",
    "com.magi.v3.control",
    "com.magi.v3.gateway",
    "com.magi.v3.supervisor",
)
WORKLOAD_SCHEMA = "magi.v3.g8-owned-smb-file-roundtrip/v1"
DEFAULT_SAMPLES_PER_ARM = 100
DEFAULT_SAMPLES_PER_BLOCK = 5
DEFAULT_MAX_V3_TO_V2_P95_RATIO = 1.05
BALANCED_ARM_ORDER = ("v2", "v3", "v3", "v2")
ALLOWED_SERVICE_MANIFEST_PATHS = frozenset(
    {
        "config/v3_service_manifest.json",
        "config/v3_live_validation_service_manifest.json",
    }
)


class G8SMBBlocked(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_file(path: Path, data: bytes, mode: int) -> str:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise G8SMBBlocked("artifact output must be canonical, absolute, and non-symlinked")
    raw.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        raw,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(raw.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return hashlib.sha256(data).hexdigest()


def _regular(path: Path, description: str) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise G8SMBBlocked(f"{description} must be canonical and absolute")
    metadata = raw.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise G8SMBBlocked(f"{description} must be a one-link regular file")
    return raw


def _json_file(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(_regular(path, description).read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise G8SMBBlocked(f"{description} is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise G8SMBBlocked(f"{description} must be a JSON object")
    return value


def _empty_target(path: Path) -> Path:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise G8SMBBlocked("dedicated SMB validation directory must be canonical and absolute")
    metadata = raw.lstat()
    if not stat.S_ISDIR(metadata.st_mode):
        raise G8SMBBlocked("dedicated SMB validation directory must already exist")
    if tuple(os.scandir(raw)):
        raise G8SMBBlocked("dedicated SMB validation directory must be empty")
    return raw


def _mount_unescape(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\134", "\\")
    )


def _verify_command_receipt(
    receipt: Mapping[str, Any], expected_argv: Sequence[str], *, returncodes: set[int] = {0}
) -> str:
    stdout = receipt.get("stdout")
    stderr = receipt.get("stderr")
    if (
        receipt.get("argv") != list(expected_argv)
        or receipt.get("returncode") not in returncodes
        or not isinstance(stdout, str)
        or not isinstance(stderr, str)
        or receipt.get("stdout_sha256") != hashlib.sha256(stdout.encode()).hexdigest()
        or receipt.get("stderr_sha256") != hashlib.sha256(stderr.encode()).hexdigest()
    ):
        raise G8SMBBlocked("raw system command receipt is invalid")
    return stdout


def _mount_identity(receipt: Mapping[str, Any], target: Path) -> dict[str, str]:
    stdout = _verify_command_receipt(receipt, ("/sbin/mount",))
    matches: list[tuple[str, str, str]] = []
    for raw_line in stdout.splitlines():
        match = re.fullmatch(r"(.+?) on (.+?) \(([^)]*)\)", raw_line)
        if not match:
            continue
        device, mount_raw, options = match.groups()
        mount = Path(_mount_unescape(mount_raw))
        try:
            within = target.is_relative_to(mount)
        except ValueError:
            within = False
        if within and mount != target and "smbfs" in {part.strip() for part in options.split(",")}:
            matches.append((device, str(mount), raw_line))
    if len(matches) != 1:
        raise G8SMBBlocked("target is not below exactly one raw-command-proven smbfs mount")
    device, mount_point, selected = matches[0]
    device_match = re.fullmatch(r"//([^/]+)/([^/]+)", device)
    if not device_match:
        raise G8SMBBlocked("SMB mount device/share identity is ambiguous")
    authority, share = device_match.groups()
    endpoint = authority.rsplit("@", 1)[-1].lower()
    return {
        "filesystem": "smbfs",
        "mount_point": mount_point,
        "mount_point_sha256": hashlib.sha256(mount_point.encode()).hexdigest(),
        "remote_endpoint_sha256": hashlib.sha256(endpoint.encode()).hexdigest(),
        "share_sha256": hashlib.sha256(_mount_unescape(share).encode()).hexdigest(),
        "selected_raw_line": selected,
        "selected_raw_line_sha256": hashlib.sha256(selected.encode()).hexdigest(),
    }


def _run(argv: Sequence[str], *, allowed_returncodes: set[int] = {0}) -> dict[str, Any]:
    result = subprocess.run(tuple(argv), capture_output=True, timeout=20, check=False, text=True)
    receipt = {
        "argv": list(argv),
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
    }
    if result.returncode not in allowed_returncodes:
        raise G8SMBBlocked(f"system command failed: {argv[0]}")
    return receipt


class SMBAdapter(Protocol):
    def mount_receipt(self, target: Path) -> Mapping[str, Any]: ...
    def state_snapshot(self) -> Mapping[str, Any]: ...
    def run_arm(
        self,
        target: Path,
        arm: str,
        samples: int,
        payload: bytes,
        *,
        sample_offset: int = 0,
    ) -> Mapping[str, Any]: ...
    def cleanup_owned(self, target: Path) -> Mapping[str, Any]: ...
    def entries(self, target: Path) -> Sequence[str]: ...


class HostSMBAdapter:
    """Allowlisted host adapter; it never resolves, lists, or mutates a parent."""

    def mount_receipt(self, target: Path) -> Mapping[str, Any]:
        return _run(("/sbin/mount",))

    def state_snapshot(self) -> Mapping[str, Any]:
        return {
            "ps": _run(("/bin/ps", "-axo", "pid=,command=")),
            "lsof": _run(
                ("/usr/sbin/lsof", "-nP", "-iTCP", "-sTCP:LISTEN"),
                allowed_returncodes={0, 1},
            ),
            "launchctl": _run(("/bin/launchctl", "list")),
        }

    def entries(self, target: Path) -> Sequence[str]:
        return tuple(sorted(entry.name for entry in os.scandir(target)))

    @staticmethod
    def _close_preserving_primary(descriptor: int) -> None:
        """Close a descriptor without hiding the syscall that already failed."""

        primary = sys.exception()
        try:
            os.close(descriptor)
        except OSError as close_error:
            if primary is None:
                raise
            primary.add_note(f"secondary descriptor close failed: {close_error}")

    def run_arm(
        self,
        target: Path,
        arm: str,
        samples: int,
        payload: bytes,
        *,
        sample_offset: int = 0,
    ) -> Mapping[str, Any]:
        if arm not in {"v2", "v3"}:
            raise G8SMBBlocked("SMB workload arm is invalid")
        if type(sample_offset) is not int or sample_offset < 0:
            raise G8SMBBlocked("SMB workload sample offset is invalid")
        directory_fd = os.open(target, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        rows: list[dict[str, Any]] = []
        try:
            for sample in range(sample_offset, sample_offset + samples):
                filename = f".magi-g8-probe-{arm}-{sample:04d}-{uuid.uuid4().hex}"
                transcript: list[dict[str, Any]] = [
                    {"op": "scandir", "path": ".", "entries": list(self.entries(target))}
                ]
                started = time.monotonic_ns()
                descriptor: int | None = None
                try:
                    descriptor = os.open(
                        filename,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                        dir_fd=directory_fd,
                    )
                    transcript.append({"op": "open_exclusive", "path": filename})
                    written = os.write(descriptor, payload)
                    transcript.append(
                        {"op": "write", "path": filename, "bytes": written, "sha256": hashlib.sha256(payload).hexdigest()}
                    )
                    os.fsync(descriptor)
                    transcript.append({"op": "fsync_file", "path": filename})
                    os.close(descriptor)
                    descriptor = None
                    transcript.append({"op": "close_write", "path": filename})
                    descriptor = os.open(filename, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
                    transcript.append({"op": "open_readonly", "path": filename})
                    observed = os.read(descriptor, len(payload) + 1)
                    transcript.append(
                        {"op": "read", "path": filename, "bytes": len(observed), "sha256": hashlib.sha256(observed).hexdigest()}
                    )
                    os.close(descriptor)
                    descriptor = None
                    transcript.append({"op": "close_read", "path": filename})
                    os.unlink(filename, dir_fd=directory_fd)
                    transcript.append({"op": "unlink", "path": filename})
                    os.fsync(directory_fd)
                    transcript.append({"op": "fsync_directory", "path": "."})
                finally:
                    if descriptor is not None:
                        self._close_preserving_primary(descriptor)
                    try:
                        os.unlink(filename, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                transcript.append(
                    {"op": "scandir", "path": ".", "entries": list(self.entries(target))}
                )
                rows.append(
                    {
                        "sample": sample,
                        "duration_ns": time.monotonic_ns() - started,
                        "filename": filename,
                        "transcript": transcript,
                    }
                )
        finally:
            self._close_preserving_primary(directory_fd)
        return {"arm": arm, "samples": rows}

    def cleanup_owned(self, target: Path) -> Mapping[str, Any]:
        removed: list[str] = []
        directory_fd = os.open(target, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            for name in self.entries(target):
                if not name.startswith(".magi-g8-probe-") or "/" in name:
                    continue
                os.unlink(name, dir_fd=directory_fd)
                removed.append(name)
            os.fsync(directory_fd)
        finally:
            self._close_preserving_primary(directory_fd)
        return {"removed_owned_names": removed, "entries_after": list(self.entries(target))}


def _artifact_binding(path: Path, description: str) -> dict[str, str]:
    frozen = _regular(path, description)
    return {"path": str(frozen), "sha256": _sha(frozen)}


def _release_identity(
    release_manifest: Path, service_manifest: Path
) -> tuple[str, str, str, str]:
    release = _json_file(release_manifest, "release manifest")
    release_root = release_manifest.parent
    service = _regular(service_manifest, "service manifest")
    try:
        service_relative = service.relative_to(release_root).as_posix()
    except ValueError as exc:
        raise G8SMBBlocked("service manifest is outside the sealed release") from exc
    if (
        service_relative not in ALLOWED_SERVICE_MANIFEST_PATHS
        or service != release_root / service_relative
    ):
        raise G8SMBBlocked("service manifest is not an allowlisted release member")
    files = {
        str(row.get("path")): str(row.get("sha256"))
        for row in release.get("files", [])
        if isinstance(row, dict)
    }
    release_id = release.get("release_id")
    release_sha = release.get("release_sha256")
    service_sha = _sha(service)
    if (
        not isinstance(release_id, str)
        or not release_id
        or not isinstance(release_sha, str)
        or not SHA256_RE.fullmatch(release_sha)
        or files.get(service_relative) != service_sha
    ):
        raise G8SMBBlocked("release/service manifest binding is invalid")
    return release_id, release_sha, service_sha, service_relative


def _matched_performance_binding(
    path: Path, release_manifest: Path
) -> dict[str, str]:
    source = _regular(path, "matched V2/V3 service performance evidence")
    report = _json_file(source, "matched V2/V3 service performance evidence")
    try:
        verify_performance_certification(report)
    except PerformanceCertificationError as exc:
        raise G8SMBBlocked(f"matched service performance evidence failed: {exc}") from exc
    release = _json_file(release_manifest, "release manifest")
    files = {
        str(row.get("path")): str(row.get("sha256"))
        for row in release.get("files", [])
        if isinstance(row, dict)
    }
    binding = report.get("release_binding")
    if (
        report.get("schema") != "magi.v3.matched-production-performance/v1"
        or report.get("status") != "certified"
        or report.get("workload") != "matched_v2_v3_performance"
        or not isinstance(binding, dict)
        or binding.get("certifier_script_sha256")
        != files.get("scripts/v3_validation/perf_certification.py")
    ):
        raise G8SMBBlocked(
            "matched evidence does not prove the release-bound handler/MariaDB/session workload"
        )
    evidence_sha = report.get("evidence_sha256")
    if not isinstance(evidence_sha, str) or not SHA256_RE.fullmatch(evidence_sha):
        raise G8SMBBlocked("matched service performance evidence SHA-256 is missing")
    return {
        "path": str(source),
        "sha256": _sha(source),
        "evidence_sha256": evidence_sha,
    }


def prepare_plan(
    *,
    target: Path,
    release_manifest: Path,
    service_manifest: Path,
    ownership_manifest: Path,
    matched_performance_report: Path,
    output: Path,
    token_output: Path,
    approval_context: Mapping[str, str] | None = None,
    adapter: SMBAdapter | None = None,
    samples_per_arm: int = DEFAULT_SAMPLES_PER_ARM,
    samples_per_block: int = DEFAULT_SAMPLES_PER_BLOCK,
    max_v3_to_v2_p95_ratio: float = DEFAULT_MAX_V3_TO_V2_P95_RATIO,
    now: datetime | None = None,
) -> dict[str, Any]:
    if (
        type(samples_per_arm) is not int
        or samples_per_arm < 10
        or type(samples_per_block) is not int
        or samples_per_block < 1
        or samples_per_arm % (2 * samples_per_block) != 0
    ):
        raise G8SMBBlocked(
            "at least ten SMB samples per arm, divisible into complete ABBA microblock cycles, are required"
        )
    if not math.isfinite(max_v3_to_v2_p95_ratio) or not 1 <= max_v3_to_v2_p95_ratio <= 1.05:
        raise G8SMBBlocked("G8 p95 threshold must preserve performance within five percent")
    target = _empty_target(target)
    for artifact in (output, token_output):
        if artifact.expanduser().resolve(strict=False).is_relative_to(target):
            raise G8SMBBlocked("plan/token artifacts must be outside the SMB target")
    adapter = adapter or HostSMBAdapter()
    raw_mount = dict(adapter.mount_receipt(target))
    mount = _mount_identity(raw_mount, target)
    release_id, release_sha, service_sha, service_relative = _release_identity(
        release_manifest, service_manifest
    )
    normalized_context: dict[str, str] | None = None
    if approval_context is not None:
        normalized_context = {
            "campaign_id": str(approval_context.get("campaign_id") or ""),
            "release_sha": str(approval_context.get("release_sha") or ""),
            "hardware_id": str(approval_context.get("hardware_id") or ""),
            "gate_config_sha256": str(approval_context.get("gate_config_sha256") or ""),
        }
        if (
            not normalized_context["campaign_id"]
            or not normalized_context["hardware_id"]
            or not SHA256_RE.fullmatch(normalized_context["release_sha"])
            or not SHA256_RE.fullmatch(normalized_context["gate_config_sha256"])
            or normalized_context["release_sha"] != release_sha
        ):
            raise G8SMBBlocked("G8 approval context is incomplete or not bound to the sealed release")
    ownership = _json_file(ownership_manifest, "ownership manifest")
    ownership_sha = _sha(ownership_manifest)
    if (
        ownership.get("release_id") != release_id
        or ownership.get("service_manifest_sha256") != service_sha
        or ownership.get("service_manifest") != str(service_manifest)
    ):
        raise G8SMBBlocked("ownership manifest is not bound to release/service composition")
    token = secrets.token_urlsafe(48)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cycle_count = samples_per_arm // (2 * samples_per_block)
    arm_order = list(BALANCED_ARM_ORDER) * cycle_count
    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "plan_id": uuid.uuid4().hex,
        "status": "prepared_not_authorized_not_executed",
        "prepared_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=2)).isoformat(),
        "target": str(target),
        "target_sha256": hashlib.sha256(str(target).encode()).hexdigest(),
        "mount_identity": mount,
        "mount_command": raw_mount,
        "release_id": release_id,
        "approval_context": normalized_context,
        "release_manifest": _artifact_binding(release_manifest, "release manifest"),
        "service_manifest": {
            **_artifact_binding(service_manifest, "service manifest"),
            "release_path": service_relative,
        },
        "ownership_manifest": _artifact_binding(ownership_manifest, "ownership manifest"),
        "matched_performance_report": _matched_performance_binding(
            matched_performance_report, release_manifest
        ),
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "consumption_receipt_path": str(output.parent / f".{output.name}.consumed.json"),
        "workload": {
            "schema": WORKLOAD_SCHEMA,
            "samples_per_arm": samples_per_arm,
            "samples_per_block": samples_per_block,
            "abba_cycle_count": cycle_count,
            "payload_bytes": 65536,
            "payload_sha256": hashlib.sha256(b"M" * 65536).hexdigest(),
            "arm_order": arm_order,
            "order_policy": "repeated_balanced_abba_microblocks",
            "max_v3_to_v2_p95_ratio": max_v3_to_v2_p95_ratio,
            "scope": "owned_remote_smb_transport_roundtrip_only",
            "service_handler_executed": False,
            "composition_benchmark_complete_without_matched_evidence": False,
        },
        "required_zero_owner": {
            "process_markers": list(FORBIDDEN_PROCESS_MARKERS),
            "launch_labels": list(FORBIDDEN_LAUNCH_LABELS),
            "listener_ports": list(PRODUCTION_PORTS),
        },
        "mutation_performed": False,
    }
    plan["plan_sha256"] = sha256_json(plan)
    data = _canonical(plan)
    plan_sha = _new_file(output, data, 0o400)
    _new_file(token_output, token.encode() + b"\n", 0o600)
    return {
        "status": "prepared_not_authorized_not_executed",
        "plan": str(output),
        "plan_file_sha256": plan_sha,
        "plan_semantic_sha256": plan["plan_sha256"],
        "token": str(token_output),
        "release_id": release_id,
    }


def _time(value: Any, description: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise G8SMBBlocked(f"{description} is invalid") from exc
    if parsed.tzinfo is None:
        raise G8SMBBlocked(f"{description} lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _load_plan(path: Path, expected_sha256: str) -> tuple[dict[str, Any], str]:
    plan_path = _regular(path, "G8 plan")
    data = plan_path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise G8SMBBlocked("G8 plan file hash mismatch")
    plan = json.loads(data)
    unsigned = dict(plan)
    supplied = unsigned.pop("plan_sha256", None)
    if (
        not isinstance(plan, dict)
        or plan.get("schema") != PLAN_SCHEMA
        or plan.get("mutation_performed") is not False
        or supplied != sha256_json(unsigned)
    ):
        raise G8SMBBlocked("G8 plan identity is invalid")
    return plan, str(supplied)


def authorize_plan(
    *,
    plan_path: Path,
    plan_file_sha256: str,
    output: Path,
    input_reader: Callable[[str], str] = input,
    local_uid: int | None = None,
    local_user: str | None = None,
    tty_name: str | None = None,
    conditional_request_path: Path | None = None,
    conditional_receipt_path: Path | None = None,
    isatty: Callable[[], bool] = lambda: sys.stdin.isatty(),
    now: datetime | None = None,
) -> dict[str, Any]:
    plan, semantic = _load_plan(plan_path, plan_file_sha256)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    conditional = conditional_request_path is not None or conditional_receipt_path is not None
    binding: dict[str, Any] | None = None
    if conditional:
        if conditional_request_path is None or conditional_receipt_path is None:
            raise G8SMBBlocked("conditional G8 approval requires both request and receipt")
        try:
            approval_context = plan.get("approval_context")
            if not isinstance(approval_context, dict):
                raise G8SMBBlocked("conditional G8 plan is missing its exact approval context")
            binding = verify_conditional_g8_preauthorization(
                request_path=conditional_request_path,
                receipt_path=conditional_receipt_path,
                expected_context=approval_context,
                target_sha256=str(plan["target_sha256"]),
                release_manifest_sha256=str(plan["release_manifest"]["sha256"]),
                release_id=str(plan["release_id"]),
                plan_id=str(plan["plan_id"]),
                plan_file_sha256=plan_file_sha256,
                plan_semantic_sha256=semantic,
                now=now,
            )
        except HumanApprovalBlocked as exc:
            raise G8SMBBlocked("conditional G8 approval is invalid") from exc
        # This path is intentionally unattended: the original receipt, not a
        # second stdin prompt, proves the allowlisted interactive decision.
        uid = int(binding["approver_uid"])
        user = str(binding["approver_user"])
        tty_hash = str(binding["tty_session_sha256"])
        method = "conditional_allowlisted_interactive_receipt"
    else:
        uid = os.getuid() if local_uid is None else local_uid
        user = getpass.getuser() if local_user is None else local_user
        if (uid, user) not in AUTHORIZED_LOCAL_OWNERS:
            raise G8SMBBlocked("G8 approval requires an allowlisted local owner")
        terminal = tty_name or os.ttyname(0)
        phrase = f"AUTHORIZE MAGI G8 SMB {plan['plan_id']} {plan['target_sha256']}"
        if not hmac_compare(input_reader(f"Type exactly: {phrase}\n"), phrase):
            raise G8SMBBlocked("G8 interactive approval phrase mismatch")
        tty_hash = hashlib.sha256(terminal.encode()).hexdigest()
        method = "allowlisted_local_owner_interactive_tty"
    auth = {
        "schema": AUTH_SCHEMA,
        "status": "authorized",
        "plan_id": plan["plan_id"],
        "plan_file_sha256": plan_file_sha256,
        "plan_semantic_sha256": semantic,
        "release_id": plan["release_id"],
        "target_sha256": plan["target_sha256"],
        "approver_uid": uid,
        "approver_user": user,
        "auth_method": method,
        "tty_session_sha256": tty_hash,
        "human_interaction_performed": True,
        "new_human_interaction_performed": not conditional,
        "preauthorization_human_interaction_performed": conditional,
        "authorized_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=30)).isoformat(),
    }
    if conditional:
        assert binding is not None
        usage_path = Path(str(binding["usage_receipt_path"])).expanduser()
        usage = {
            "schema": "magi.v3.conditional-g8-smb-usage/v1",
            "status": "authorized_for_g8_only",
            "plan_id": plan["plan_id"],
            "plan_file_sha256": plan_file_sha256,
            "plan_semantic_sha256": semantic,
            "target_sha256": plan["target_sha256"],
            "release_id": plan["release_id"],
            "release_manifest_sha256": plan["release_manifest"]["sha256"],
            "approval_context": binding["context"],
            "conditional_request_sha256": binding["request_sha256"],
            "conditional_receipt_sha256": binding["receipt_sha256"],
            "conditional_request_path": str(conditional_request_path),
            "conditional_receipt_path": str(conditional_receipt_path),
            "request_id": binding["request_id"],
            "cutover_window": binding["cutover_window"],
            "created_at": now.isoformat(),
            "final_cutover_consumption_performed": False,
        }
        auth["authorization_mode"] = "conditional_daytime_preauthorization"
        auth["conditional_g8_usage"] = {
            "path": str(usage_path),
            "sha256": _new_file(usage_path, _canonical(usage), 0o400),
        }
    return {
        "status": "authorized_not_executed",
        "authorization": str(output),
        "authorization_sha256": _new_file(output, _canonical(auth), 0o400),
    }


def _consume(plan: Mapping[str, Any], token_file: Path) -> tuple[dict[str, Any], str]:
    token_path = _regular(token_file, "G8 one-time token")
    if stat.S_IMODE(token_path.stat().st_mode) != 0o600 or token_path.stat().st_uid != os.getuid():
        raise G8SMBBlocked("G8 token must be owner-only mode 0600")
    token = token_path.read_bytes().rstrip(b"\r\n")
    if not token or hashlib.sha256(token).hexdigest() != plan.get("token_sha256"):
        raise G8SMBBlocked("G8 one-time token mismatch")
    receipt = {
        "schema": CONSUMPTION_SCHEMA,
        "plan_id": plan["plan_id"],
        "plan_semantic_sha256": plan["plan_sha256"],
        "token_sha256": plan["token_sha256"],
        "consumer_pid": os.getpid(),
        "consumed_monotonic_ns": time.monotonic_ns(),
    }
    path = Path(str(plan["consumption_receipt_path"]))
    digest = _new_file(path, _canonical(receipt), 0o400)
    return receipt, digest


def _authorization(
    path: Path,
    expected_sha256: str,
    plan: Mapping[str, Any],
    plan_file_sha256: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    source = _regular(path, "G8 authorization")
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise G8SMBBlocked("G8 authorization hash mismatch")
    auth = json.loads(data)
    if (
        not isinstance(auth, dict)
        or auth.get("schema") != AUTH_SCHEMA
        or auth.get("status") != "authorized"
        or auth.get("plan_id") != plan.get("plan_id")
        or auth.get("plan_file_sha256") != plan_file_sha256
        or auth.get("plan_semantic_sha256") != plan.get("plan_sha256")
        or auth.get("release_id") != plan.get("release_id")
        or auth.get("target_sha256") != plan.get("target_sha256")
        or auth.get("human_interaction_performed") is not True
        or (auth.get("approver_uid"), auth.get("approver_user")) not in AUTHORIZED_LOCAL_OWNERS
        or auth.get("auth_method") not in {
            "allowlisted_local_owner_interactive_tty",
            "conditional_allowlisted_interactive_receipt",
        }
        or (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        > _time(auth.get("expires_at"), "authorization expiry")
    ):
        raise G8SMBBlocked("G8 authorization is invalid or expired")
    mode = auth.get("authorization_mode")
    if mode is not None:
        usage_binding = auth.get("conditional_g8_usage")
        if mode != "conditional_daytime_preauthorization" or not isinstance(usage_binding, dict):
            raise G8SMBBlocked("conditional G8 authorization mode is invalid")
        usage_path = Path(str(usage_binding.get("path", "")))
        if _sha(usage_path) != usage_binding.get("sha256"):
            raise G8SMBBlocked("conditional G8 usage receipt drifted")
        usage = _json_file(usage_path, "conditional G8 usage receipt")
        if (
            usage.get("schema") != "magi.v3.conditional-g8-smb-usage/v1"
            or usage.get("status") != "authorized_for_g8_only"
            or usage.get("plan_id") != plan.get("plan_id")
            or usage.get("plan_file_sha256") != plan_file_sha256
            or usage.get("plan_semantic_sha256") != plan.get("plan_sha256")
            or usage.get("target_sha256") != plan.get("target_sha256")
            or usage.get("release_id") != plan.get("release_id")
            or usage.get("release_manifest_sha256") != plan.get("release_manifest", {}).get("sha256")
            or usage.get("approval_context") != plan.get("approval_context")
            or usage.get("final_cutover_consumption_performed") is not False
            or not SHA256_RE.fullmatch(str(usage.get("conditional_request_sha256") or ""))
            or not SHA256_RE.fullmatch(str(usage.get("conditional_receipt_sha256") or ""))
        ):
            raise G8SMBBlocked("conditional G8 usage receipt is not bound to this plan")
        try:
            verified = verify_conditional_g8_preauthorization(
                request_path=Path(str(usage.get("conditional_request_path", ""))),
                receipt_path=Path(str(usage.get("conditional_receipt_path", ""))),
                expected_context=plan.get("approval_context", {}),
                target_sha256=str(plan["target_sha256"]),
                release_manifest_sha256=str(plan["release_manifest"]["sha256"]),
                release_id=str(plan["release_id"]),
                plan_id=str(plan["plan_id"]),
                plan_file_sha256=plan_file_sha256,
                plan_semantic_sha256=str(plan["plan_sha256"]),
                now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc),
            )
        except (HumanApprovalBlocked, OSError) as exc:
            raise G8SMBBlocked("conditional G8 receipt cannot be reverified") from exc
        if (
            verified["request_sha256"] != usage["conditional_request_sha256"]
            or verified["receipt_sha256"] != usage["conditional_receipt_sha256"]
        ):
            raise G8SMBBlocked("conditional G8 receipt binding drifted")
    return auth


def _snapshot(snapshot: Mapping[str, Any], plan: Mapping[str, Any]) -> None:
    ps = _verify_command_receipt(snapshot.get("ps", {}), ("/bin/ps", "-axo", "pid=,command="))
    lsof = _verify_command_receipt(
        snapshot.get("lsof", {}),
        ("/usr/sbin/lsof", "-nP", "-iTCP", "-sTCP:LISTEN"),
        returncodes={0, 1},
    )
    launch = _verify_command_receipt(snapshot.get("launchctl", {}), ("/bin/launchctl", "list"))
    zero = plan.get("required_zero_owner", {})
    for marker in zero.get("process_markers", []):
        if not isinstance(marker, str) or marker.lower() in ps.lower():
            raise G8SMBBlocked("V2/V3 process owner was present in raw ps evidence")
    loaded = {line.split()[-1] for line in launch.splitlines() if line.split()}
    if any(label in loaded for label in zero.get("launch_labels", [])):
        raise G8SMBBlocked("V2/V3 service was present in raw launchctl evidence")
    for port in zero.get("listener_ports", []):
        if re.search(rf"(?:[:.]){int(port)}(?:\s|->|\()", lsof) and "LISTEN" in lsof:
            raise G8SMBBlocked("production listener was present in raw lsof evidence")


def _p95(values: Sequence[int]) -> float:
    if len(values) < 10 or any(type(value) is not int or value <= 0 for value in values):
        raise G8SMBBlocked("SMB timing samples are incomplete")
    ordered = sorted(values)
    return float(ordered[math.ceil(0.95 * len(ordered)) - 1])


def _arm(raw: Mapping[str, Any], arm: str, plan: Mapping[str, Any]) -> float:
    workload = plan["workload"]
    samples = raw.get("samples")
    if raw.get("arm") != arm or not isinstance(samples, list) or len(samples) != workload["samples_per_arm"]:
        raise G8SMBBlocked("SMB arm/sample identity is invalid")
    durations: list[int] = []
    payload_sha = workload["payload_sha256"]
    payload_bytes = workload["payload_bytes"]
    expected_ops = [
        "scandir", "open_exclusive", "write", "fsync_file", "close_write",
        "open_readonly", "read", "close_read", "unlink", "fsync_directory", "scandir",
    ]
    for index, row in enumerate(samples):
        if not isinstance(row, dict) or row.get("sample") != index:
            raise G8SMBBlocked("SMB sample sequence is invalid")
        filename = row.get("filename")
        transcript = row.get("transcript")
        if (
            not isinstance(filename, str)
            or not filename.startswith(f".magi-g8-probe-{arm}-{index:04d}-")
            or "/" in filename
            or ".." in filename
            or not isinstance(transcript, list)
            or [entry.get("op") for entry in transcript if isinstance(entry, dict)] != expected_ops
            or any(not isinstance(entry, dict) for entry in transcript)
            or transcript[0] != {"op": "scandir", "path": ".", "entries": []}
            or transcript[-1] != {"op": "scandir", "path": ".", "entries": []}
        ):
            raise G8SMBBlocked("SMB syscall transcript is invalid")
        for entry in transcript[1:-1]:
            if entry.get("path") not in {filename, "."}:
                raise G8SMBBlocked("SMB syscall escaped the owned target")
        write, read = transcript[2], transcript[6]
        if (
            write.get("bytes") != payload_bytes
            or write.get("sha256") != payload_sha
            or read.get("bytes") != payload_bytes
            or read.get("sha256") != payload_sha
        ):
            raise G8SMBBlocked("SMB workload payload roundtrip drifted")
        durations.append(row.get("duration_ns"))
    return _p95(durations)


def _raw_artifacts(plan: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in (
        "release_manifest",
        "service_manifest",
        "ownership_manifest",
        "matched_performance_report",
    ):
        binding = plan[name]
        path = _regular(Path(binding["path"]), name)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != binding["sha256"]:
            raise G8SMBBlocked(f"{name} changed after plan creation")
        result[name] = base64.b64encode(data).decode()
    return result


def execute_plan(
    *,
    plan_path: Path,
    plan_file_sha256: str,
    authorization_path: Path,
    authorization_sha256: str,
    token_file: Path,
    output: Path,
    adapter: SMBAdapter | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    plan, _semantic = _load_plan(plan_path, plan_file_sha256)
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if instant > _time(plan.get("expires_at"), "plan expiry"):
        raise G8SMBBlocked("G8 plan expired")
    auth = _authorization(
        authorization_path,
        authorization_sha256,
        plan,
        plan_file_sha256,
        now=instant,
    )
    target = _empty_target(Path(plan["target"]))
    if output.expanduser().resolve(strict=False).is_relative_to(target):
        raise G8SMBBlocked("G8 evidence output must be outside the SMB target")
    adapter = adapter or HostSMBAdapter()
    raw_artifacts = _raw_artifacts(plan)
    mount_before = dict(adapter.mount_receipt(target))
    if {
        key: value for key, value in _mount_identity(mount_before, target).items()
        if key not in {"selected_raw_line", "selected_raw_line_sha256"}
    } != {
        key: value for key, value in plan["mount_identity"].items()
        if key not in {"selected_raw_line", "selected_raw_line_sha256"}
    }:
        raise G8SMBBlocked("remote SMB mount endpoint/share identity drifted")
    before = dict(adapter.state_snapshot())
    _snapshot(before, plan)
    consumption, consumption_sha = _consume(plan, token_file)
    payload = b"M" * plan["workload"]["payload_bytes"]
    cleanup: Mapping[str, Any] = {}
    raw_snapshots: dict[str, Mapping[str, Any]] = {"before": before}
    raw_arms: dict[str, dict[str, Any]] = {
        "v2": {"arm": "v2", "samples": []},
        "v3": {"arm": "v3", "samples": []},
    }
    raw_block_order: list[dict[str, Any]] = []
    offsets = {"v2": 0, "v3": 0}
    primary_error: BaseException | None = None
    try:
        for block, arm in enumerate(plan["workload"]["arm_order"], start=1):
            sample_start = offsets[arm]
            samples = int(plan["workload"]["samples_per_block"])
            arm_report = dict(
                adapter.run_arm(
                    target,
                    arm,
                    samples,
                    payload,
                    sample_offset=sample_start,
                )
            )
            arm_samples = arm_report.get("samples")
            if arm_report.get("arm") != arm or not isinstance(arm_samples, list):
                raise G8SMBBlocked("SMB balanced block result is invalid")
            raw_arms[arm]["samples"].extend(arm_samples)
            raw_block_order.append(
                {
                    "block": block,
                    "arm": arm,
                    "sample_start": sample_start,
                    "sample_count": samples,
                }
            )
            offsets[arm] += samples
            snapshot = dict(adapter.state_snapshot())
            _snapshot(snapshot, plan)
            raw_snapshots[f"after_block_{block}"] = snapshot
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            cleanup = dict(adapter.cleanup_owned(target))
        except BaseException as cleanup_error:
            if primary_error is None:
                raise
            primary_error.add_note(f"secondary owned-file cleanup failed: {cleanup_error}")
    if list(adapter.entries(target)) or cleanup.get("entries_after") != []:
        raise G8SMBBlocked("dedicated SMB validation directory did not return to empty")
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "status": "passed",
        "plan": plan,
        "plan_file_sha256": plan_file_sha256,
        "authorization": auth,
        "authorization_file_sha256": authorization_sha256,
        "consumption_receipt": consumption,
        "consumption_receipt_sha256": consumption_sha,
        "raw_artifacts_b64": raw_artifacts,
        "raw_mount_before": mount_before,
        "raw_snapshots": raw_snapshots,
        "raw_block_order": raw_block_order,
        "raw_arms": raw_arms,
        "raw_cleanup": cleanup,
    }
    try:
        derived = verify_report(
            report,
            expected_release_id=plan["release_id"],
            expected_release_manifest_sha256=plan["release_manifest"]["sha256"],
            expected_matched_performance_sha256=plan["matched_performance_report"][
                "evidence_sha256"
            ],
        )
    except G8SMBBlocked as exc:
        diagnostic = {
            **report,
            "status": "failed",
            "failure": {
                "type": type(exc).__name__,
                "message": str(exc),
                "formal_gate_cleared": False,
            },
        }
        diagnostic["evidence_sha256"] = sha256_json(diagnostic)
        failure_output = output.with_name(
            f"{output.stem}-failed-diagnostic{output.suffix}"
        )
        _new_file(failure_output, _canonical(diagnostic), 0o400)
        raise G8SMBBlocked(
            f"{exc}; failed diagnostic persisted at {failure_output}"
        ) from exc
    report["derived"] = derived
    report["evidence_sha256"] = sha256_json(report)
    _new_file(output, _canonical(report), 0o400)
    return report


def verify_report(
    report: Mapping[str, Any],
    *,
    expected_release_id: str,
    expected_release_manifest_sha256: str,
    expected_matched_performance_sha256: str | None = None,
) -> dict[str, Any]:
    unsigned_report = dict(report)
    supplied_evidence = unsigned_report.pop("evidence_sha256", None)
    supplied_derived = unsigned_report.pop("derived", None)
    if supplied_evidence is not None and supplied_evidence != sha256_json({**unsigned_report, "derived": supplied_derived}):
        raise G8SMBBlocked("G8 raw evidence hash mismatch")
    plan = report.get("plan")
    if not isinstance(plan, dict):
        raise G8SMBBlocked("G8 raw plan is missing")
    unsigned_plan = dict(plan)
    semantic = unsigned_plan.pop("plan_sha256", None)
    if (
        report.get("schema") != REPORT_SCHEMA
        or report.get("status") != "passed"
        or plan.get("schema") != PLAN_SCHEMA
        or semantic != sha256_json(unsigned_plan)
        or plan.get("release_id") != expected_release_id
        or plan.get("release_manifest", {}).get("sha256") != expected_release_manifest_sha256
        or (
            expected_matched_performance_sha256 is not None
            and plan.get("matched_performance_report", {}).get("evidence_sha256")
            != expected_matched_performance_sha256
        )
        or report.get("plan_file_sha256") != hashlib.sha256(_canonical(plan)).hexdigest()
    ):
        raise G8SMBBlocked("G8 raw evidence/plan binding is invalid")
    auth = report.get("authorization")
    consumption = report.get("consumption_receipt")
    if (
        not isinstance(auth, dict)
        or auth.get("schema") != AUTH_SCHEMA
        or auth.get("plan_id") != plan.get("plan_id")
        or auth.get("plan_semantic_sha256") != semantic
        or auth.get("human_interaction_performed") is not True
        or (auth.get("approver_uid"), auth.get("approver_user")) not in AUTHORIZED_LOCAL_OWNERS
        or report.get("authorization_file_sha256") != hashlib.sha256(_canonical(auth)).hexdigest()
        or not isinstance(consumption, dict)
        or consumption.get("schema") != CONSUMPTION_SCHEMA
        or consumption.get("plan_id") != plan.get("plan_id")
        or consumption.get("plan_semantic_sha256") != semantic
        or consumption.get("token_sha256") != plan.get("token_sha256")
        or report.get("consumption_receipt_sha256") != hashlib.sha256(_canonical(consumption)).hexdigest()
    ):
        raise G8SMBBlocked("G8 approval/one-time consumption evidence is invalid")
    mount = _mount_identity(report.get("raw_mount_before", {}), Path(plan["target"]))
    if any(mount.get(key) != plan.get("mount_identity", {}).get(key) for key in (
        "filesystem", "mount_point", "mount_point_sha256", "remote_endpoint_sha256", "share_sha256"
    )):
        raise G8SMBBlocked("G8 raw SMB mount identity differs from its plan")
    snapshots = report.get("raw_snapshots", {})
    if not isinstance(snapshots, dict):
        raise G8SMBBlocked("G8 zero-owner snapshots are missing")
    for snapshot in snapshots.values():
        if not isinstance(snapshot, dict):
            raise G8SMBBlocked("G8 raw zero-owner snapshot is missing")
        _snapshot(snapshot, plan)
    planned_workload = plan.get("workload")
    planned_arm_order = (
        planned_workload.get("arm_order")
        if isinstance(planned_workload, dict)
        else None
    )
    if (
        not isinstance(planned_arm_order, list)
        or not planned_arm_order
        or len(planned_arm_order) > 1000
    ):
        raise G8SMBBlocked("G8 planned ABBA microblock order is invalid")
    expected_snapshot_phases = {"before"} | {
        f"after_block_{index}"
        for index in range(1, len(planned_arm_order) + 1)
    }
    if set(snapshots) != expected_snapshot_phases:
        raise G8SMBBlocked("G8 zero-owner snapshot phases are incomplete")
    artifacts = report.get("raw_artifacts_b64")
    if not isinstance(artifacts, dict):
        raise G8SMBBlocked("G8 raw release/composition artifacts are missing")
    decoded: dict[str, bytes] = {}
    for name in (
        "release_manifest",
        "service_manifest",
        "ownership_manifest",
        "matched_performance_report",
    ):
        try:
            decoded[name] = base64.b64decode(artifacts[name], validate=True)
        except (KeyError, ValueError) as exc:
            raise G8SMBBlocked("G8 raw artifact encoding is invalid") from exc
        if hashlib.sha256(decoded[name]).hexdigest() != plan[name]["sha256"]:
            raise G8SMBBlocked("G8 raw release/composition artifact hash mismatch")
    try:
        release = json.loads(decoded["release_manifest"])
        ownership = json.loads(decoded["ownership_manifest"])
        matched_performance = json.loads(decoded["matched_performance_report"])
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise G8SMBBlocked("G8 raw release/composition artifact JSON is invalid") from exc
    release_files = {
        str(row.get("path")): str(row.get("sha256"))
        for row in release.get("files", []) if isinstance(row, dict)
    }
    service_relative = plan.get("service_manifest", {}).get("release_path")
    if (
        release.get("release_id") != expected_release_id
        or service_relative not in ALLOWED_SERVICE_MANIFEST_PATHS
        or release_files.get(service_relative) != plan["service_manifest"]["sha256"]
        or ownership.get("release_id") != expected_release_id
        or ownership.get("service_manifest_sha256") != plan["service_manifest"]["sha256"]
        or ownership.get("service_manifest") != plan["service_manifest"]["path"]
    ):
        raise G8SMBBlocked("G8 exact release/service/ownership composition is invalid")
    try:
        verify_performance_certification(matched_performance)
    except PerformanceCertificationError as exc:
        raise G8SMBBlocked(f"G8 matched service performance evidence failed: {exc}") from exc
    matched_binding = matched_performance.get("release_binding")
    if (
        not isinstance(matched_binding, dict)
        or not SHA256_RE.fullmatch(
            str(plan.get("matched_performance_report", {}).get("evidence_sha256") or "")
        )
        or matched_performance.get("evidence_sha256")
        != plan.get("matched_performance_report", {}).get("evidence_sha256")
        or matched_binding.get("certifier_script_sha256")
        != release_files.get("scripts/v3_validation/perf_certification.py")
        or matched_performance.get("status") != "certified"
        or matched_performance.get("workload") != "matched_v2_v3_performance"
    ):
        raise G8SMBBlocked("G8 matched service evidence is not bound to this release")
    arms = report.get("raw_arms")
    workload = plan.get("workload", {})
    samples_per_arm = workload.get("samples_per_arm")
    samples_per_block = workload.get("samples_per_block")
    abba_cycle_count = workload.get("abba_cycle_count")
    valid_cycle_shape = (
        type(samples_per_arm) is int
        and samples_per_arm >= 10
        and type(samples_per_block) is int
        and samples_per_block >= 1
        and samples_per_arm % (2 * samples_per_block) == 0
    )
    expected_cycle_count = (
        samples_per_arm // (2 * samples_per_block)
        if valid_cycle_shape
        else None
    )
    expected_arm_order = (
        list(BALANCED_ARM_ORDER) * expected_cycle_count
        if expected_cycle_count is not None
        else []
    )
    if (
        not isinstance(arms, dict)
        or not valid_cycle_shape
        or list(workload.get("arm_order", [])) != expected_arm_order
        or workload.get("order_policy") != "repeated_balanced_abba_microblocks"
        or abba_cycle_count != expected_cycle_count
        or workload.get("scope") != "owned_remote_smb_transport_roundtrip_only"
        or workload.get("service_handler_executed") is not False
        or workload.get("composition_benchmark_complete_without_matched_evidence") is not False
    ):
        raise G8SMBBlocked("G8 sequential arm evidence is missing")
    raw_block_order = report.get("raw_block_order")
    expected_block_order: list[dict[str, Any]] = []
    offsets = {"v2": 0, "v3": 0}
    for block, arm in enumerate(expected_arm_order, start=1):
        expected_block_order.append(
            {
                "block": block,
                "arm": arm,
                "sample_start": offsets[arm],
                "sample_count": samples_per_block,
            }
        )
        offsets[arm] += samples_per_block
    if raw_block_order != expected_block_order:
        raise G8SMBBlocked("G8 balanced ABBA block order is invalid")
    v2_p95 = _arm(arms.get("v2", {}), "v2", plan)
    v3_p95 = _arm(arms.get("v3", {}), "v3", plan)
    ratio = v3_p95 / v2_p95
    cleanup = report.get("raw_cleanup")
    if not isinstance(cleanup, dict) or cleanup.get("entries_after") != []:
        raise G8SMBBlocked("G8 cleanup did not prove the target returned to empty")
    derived = {
        "mount_is_remote_smb": True,
        "dedicated_non_share_root": True,
        "v2_zero_owner_snapshots": len(expected_snapshot_phases),
        "balanced_abba_blocks": True,
        "repeated_abba_microblocks": True,
        "abba_cycle_count": expected_cycle_count,
        "candidate_service_started": False,
        "production_listener_started": False,
        "client_content_read": False,
        "external_writes_outside_dedicated_target": False,
        "samples_per_arm": plan["workload"]["samples_per_arm"],
        "v2_p95_ns": v2_p95,
        "v3_p95_ns": v3_p95,
        "v3_to_v2_p95_ratio": ratio,
        "maximum_v3_to_v2_p95_ratio": plan["workload"]["max_v3_to_v2_p95_ratio"],
        "threshold_passed": ratio <= plan["workload"]["max_v3_to_v2_p95_ratio"],
        "cleanup_verified_empty": True,
        "remote_probe_scope": "owned_remote_smb_transport_roundtrip_only",
        "remote_probe_is_service_benchmark": False,
        "matched_service_performance_evidence_sha256": plan[
            "matched_performance_report"
        ]["evidence_sha256"],
        "decomposed_service_plus_remote_smb_evidence_complete": True,
    }
    if not derived["threshold_passed"]:
        raise G8SMBBlocked("G8 V3 remote-SMB p95 exceeded the bound V2 threshold")
    if supplied_derived is not None and supplied_derived != derived:
        raise G8SMBBlocked("G8 summary differs from authoritative raw recomputation")
    return derived


def extract_bound_matched_performance_report(
    report: Mapping[str, Any],
    *,
    expected_release_id: str,
    expected_release_manifest_sha256: str,
) -> dict[str, Any]:
    """Return only the raw matched artifact after the whole receipt verifies."""

    plan = report.get("plan")
    expected = (
        plan.get("matched_performance_report", {}).get("evidence_sha256")
        if isinstance(plan, Mapping)
        else None
    )
    if not isinstance(expected, str):
        raise G8SMBBlocked("G8 matched performance binding is missing")
    verify_report(
        report,
        expected_release_id=expected_release_id,
        expected_release_manifest_sha256=expected_release_manifest_sha256,
        expected_matched_performance_sha256=expected,
    )
    try:
        value = json.loads(
            base64.b64decode(
                report["raw_artifacts_b64"]["matched_performance_report"],
                validate=True,
            )
        )
    except (KeyError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise G8SMBBlocked("G8 matched performance artifact cannot be extracted") from exc
    if not isinstance(value, dict):
        raise G8SMBBlocked("G8 matched performance artifact is not an object")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare/authorize/execute/verify isolated G8 SMB evidence")
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--target", type=Path, required=True)
    prepare.add_argument("--release-manifest", type=Path, required=True)
    prepare.add_argument("--service-manifest", type=Path, required=True)
    prepare.add_argument("--ownership-manifest", type=Path, required=True)
    prepare.add_argument("--matched-performance-report", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--token-output", type=Path, required=True)
    prepare.add_argument("--campaign-id")
    prepare.add_argument("--release-sha")
    prepare.add_argument("--hardware-id")
    prepare.add_argument("--gate-config-sha256")
    authorize = sub.add_parser("authorize")
    authorize.add_argument("--plan", type=Path, required=True)
    authorize.add_argument("--plan-sha256", required=True)
    authorize.add_argument("--output", type=Path, required=True)
    authorize.add_argument("--conditional-request", type=Path)
    authorize.add_argument("--conditional-receipt", type=Path)
    execute = sub.add_parser("execute")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--plan-sha256", required=True)
    execute.add_argument("--authorization", type=Path, required=True)
    execute.add_argument("--authorization-sha256", required=True)
    execute.add_argument("--token-file", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--report", type=Path, required=True)
    verify.add_argument("--release-id", required=True)
    verify.add_argument("--release-manifest-sha256", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "prepare":
        context_values = (
            args.campaign_id,
            args.release_sha,
            args.hardware_id,
            args.gate_config_sha256,
        )
        if any(value is not None for value in context_values) and not all(
            value is not None for value in context_values
        ):
            raise G8SMBBlocked("G8 prepare approval context arguments are all-or-nothing")
        result = prepare_plan(
            target=args.target, release_manifest=args.release_manifest,
            service_manifest=args.service_manifest, ownership_manifest=args.ownership_manifest,
            matched_performance_report=args.matched_performance_report,
            output=args.output, token_output=args.token_output,
            approval_context=(
                {
                    "campaign_id": args.campaign_id,
                    "release_sha": args.release_sha,
                    "hardware_id": args.hardware_id,
                    "gate_config_sha256": args.gate_config_sha256,
                }
                if all(value is not None for value in context_values)
                else None
            ),
        )
    elif args.command == "authorize":
        result = authorize_plan(
            plan_path=args.plan, plan_file_sha256=args.plan_sha256, output=args.output,
            conditional_request_path=args.conditional_request,
            conditional_receipt_path=args.conditional_receipt,
        )
    elif args.command == "execute":
        result = execute_plan(
            plan_path=args.plan, plan_file_sha256=args.plan_sha256,
            authorization_path=args.authorization, authorization_sha256=args.authorization_sha256,
            token_file=args.token_file, output=args.output,
        )
    else:
        report = _json_file(args.report, "G8 report")
        result = verify_report(
            report, expected_release_id=args.release_id,
            expected_release_manifest_sha256=args.release_manifest_sha256,
        )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AUTH_SCHEMA", "CONSUMPTION_SCHEMA", "G8SMBBlocked", "HostSMBAdapter",
    "PLAN_SCHEMA", "REPORT_SCHEMA", "SMBAdapter", "authorize_plan", "execute_plan",
    "extract_bound_matched_performance_report", "prepare_plan", "sha256_json", "verify_report",
]

#!/usr/bin/env python3
"""Execute one fail-closed V3-to-V3 production rotation.

This is deliberately separate from the historical V2-to-V3 executor.  It
recomputes the final release gate, persists an exact pre-mutation rollback
snapshot, proves zero ownership, atomically installs one sealed candidate, and
restores an independently prepared predecessor deployment on any failure.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v3_cutover.activation import (
    V3RotationTransaction,
    acquire_active_release_admission,
    active_release_marker,
)
from scripts.v3_cutover.core import CutoverError
from scripts.v3_release_gate import evaluate_evidence


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ROLE_ORDER = ("control", "gateway", "supervisor")
STOP_ORDER = tuple(reversed(ROLE_ORDER))
LABEL_BY_ROLE = {
    "control": "com.magi.v3.control",
    "gateway": "com.magi.v3.gateway",
    "supervisor": "com.magi.v3.supervisor",
}
DEFAULT_READINESS_URLS = (
    "http://127.0.0.1:5002/readyz",
    "http://127.0.0.1:5003/readyz",
    "http://127.0.0.1:8088/readyz",
    "http://127.0.0.1:5002/login",
    "http://127.0.0.1:5002/osc",
)
MAX_JSON_BYTES = 32 * 1024 * 1024
DEFAULT_OWNERSHIP_OBSERVATION_TIMEOUT_SECONDS = 45.0
DEFAULT_OWNERSHIP_OBSERVATION_INTERVAL_SECONDS = 0.25
DEFAULT_OWNERSHIP_STABLE_OBSERVATIONS = 2


Runner = Callable[[Sequence[str]], Any]
Observer = Callable[["BoundDeployment | None"], Mapping[str, Any]]
RoleObserver = Callable[["BoundDeployment", Sequence[str]], Mapping[str, Any]]
ReadinessProbe = Callable[[Sequence[str]], tuple[bool, Mapping[str, Any]]]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _safe_absolute_directory(path: Path, description: str) -> Path:
    raw = path.expanduser()
    if (
        not raw.is_absolute()
        or raw.resolve(strict=True) != raw
        or raw.is_symlink()
        or not raw.is_dir()
    ):
        raise CutoverError(f"{description} is unsafe")
    return raw


def _safe_file_bytes(
    path: Path,
    description: str,
    *,
    maximum: int = MAX_JSON_BYTES,
    allow_empty: bool = False,
) -> bytes:
    try:
        before = path.lstat()
    except OSError as exc:
        raise CutoverError(f"{description} is unavailable") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or (before.st_size < 1 and not allow_empty)
        or before.st_size > maximum
    ):
        raise CutoverError(f"{description} is unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise CutoverError(f"{description} could not be read safely") from exc
    if (
        len(raw) > maximum
        or (before.st_dev, before.st_ino, before.st_size)
        != (after.st_dev, after.st_ino, after.st_size)
    ):
        raise CutoverError(f"{description} changed while being read")
    return raw


def _load_json_bytes(raw: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CutoverError(f"{description} is invalid JSON") from exc
    if type(value) is not dict:
        raise CutoverError(f"{description} must be a JSON object")
    return value


@dataclass(frozen=True, slots=True)
class BoundFile:
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class BoundDeployment:
    root: Path
    marker: BoundFile
    manifest: BoundFile
    release_id: str
    release_root: Path
    release_manifest: BoundFile
    release_sha: str
    ownership_source: BoundFile
    ownership_target: Path
    plists: Mapping[str, BoundFile]
    ports_by_role: Mapping[str, tuple[int, ...]]


def _verify_release_inventory(
    release_root: Path,
    manifest: Mapping[str, Any],
) -> None:
    rows = manifest.get("files")
    if type(rows) is not list or not rows:
        raise CutoverError("release file inventory is missing")
    normalized: list[dict[str, Any]] = []
    paths: set[str] = set()
    for row in rows:
        if type(row) is not dict or set(row) != {"path", "sha256", "size", "mode"}:
            raise CutoverError("release file inventory row is invalid")
        relative_raw = row.get("path")
        digest = row.get("sha256")
        size = row.get("size")
        mode = row.get("mode")
        relative = PurePosixPath(relative_raw) if type(relative_raw) is str else None
        if (
            relative is None
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() in paths
            or type(digest) is not str
            or not SHA256_RE.fullmatch(digest)
            or type(size) is not int
            or isinstance(size, bool)
            or size < 0
            or type(mode) is not str
            or not re.fullmatch(r"0[0-7]{3}", mode)
        ):
            raise CutoverError("release file inventory row is invalid")
        paths.add(relative.as_posix())
        normalized.append(
            {"path": relative.as_posix(), "sha256": digest, "size": size, "mode": mode}
        )
    if [row["path"] for row in normalized] != sorted(paths):
        raise CutoverError("release file inventory is not sorted")
    inventory_sha = _sha256_bytes(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    if (
        manifest.get("source_snapshot_sha256") != inventory_sha
        or manifest.get("release_sha256") != inventory_sha
    ):
        raise CutoverError("release file inventory digest drifted")
    for row in normalized:
        relative = PurePosixPath(row["path"])
        path = release_root.joinpath(*relative.parts)
        raw = _safe_file_bytes(
            path,
            f"release member {relative}",
            maximum=max(row["size"], 1),
            allow_empty=True,
        )
        if (
            len(raw) != row["size"]
            or _sha256_bytes(raw) != row["sha256"]
            or f"0{stat.S_IMODE(path.stat().st_mode):03o}" != row["mode"]
        ):
            raise CutoverError(f"immutable release member drifted: {relative}")


def _artifact_inventory(root: Path, manifest: Mapping[str, Any]) -> dict[str, BoundFile]:
    rows = manifest.get("artifacts")
    if type(rows) is not list or not rows:
        raise CutoverError("deployment artifact inventory is missing")
    inventory: dict[str, BoundFile] = {}
    for row in rows:
        if type(row) is not dict or set(row) != {"path", "sha256", "size"}:
            raise CutoverError("deployment artifact inventory row is invalid")
        relative_raw = row.get("path")
        digest = row.get("sha256")
        size = row.get("size")
        relative = PurePosixPath(relative_raw) if type(relative_raw) is str else None
        if (
            relative is None
            or relative.is_absolute()
            or ".." in relative.parts
            or relative.as_posix() in inventory
            or type(digest) is not str
            or not SHA256_RE.fullmatch(digest)
            or type(size) is not int
            or isinstance(size, bool)
            or size < 1
        ):
            raise CutoverError("deployment artifact inventory row is invalid")
        path = root.joinpath(*relative.parts)
        raw = _safe_file_bytes(path, f"deployment artifact {relative}", maximum=max(size, 1))
        if len(raw) != size or _sha256_bytes(raw) != digest:
            raise CutoverError(f"deployment artifact drifted: {relative}")
        inventory[relative.as_posix()] = BoundFile(path, digest, size)
    return inventory


def load_bound_deployment(root: Path) -> BoundDeployment:
    """Load and re-hash every artifact in one prepared deployment."""

    deployment_root = _safe_absolute_directory(root, "prepared deployment")
    manifest_path = deployment_root / "deploy-manifest.json"
    marker_path = deployment_root / "DEPLOY_PREPARED.json"
    manifest_raw = _safe_file_bytes(manifest_path, "deployment manifest")
    marker_raw = _safe_file_bytes(marker_path, "deployment prepared marker")
    manifest = _load_json_bytes(manifest_raw, "deployment manifest")
    marker = _load_json_bytes(marker_raw, "deployment prepared marker")
    manifest_sha = _sha256_bytes(manifest_raw)
    release_id = manifest.get("release_id")
    release_manifest_value = manifest.get("release_manifest")
    release_manifest_sha = manifest.get("release_manifest_sha256")
    ownership_target_value = manifest.get("ownership_manifest")
    ownership_sha = manifest.get("ownership_manifest_sha256")
    if (
        marker.get("schema_version") != 1
        or marker.get("status") != "prepared_not_installed"
        or marker.get("ready_to_install") is not True
        or marker.get("mutation_performed") is not False
        or marker.get("deployment_mode") != "production"
        or marker.get("manifest") != "deploy-manifest.json"
        or marker.get("manifest_sha256") != manifest_sha
        or manifest.get("deployment_mode") != "production"
        or manifest.get("mutation_performed") is not False
        or type(release_id) is not str
        or not RELEASE_ID_RE.fullmatch(release_id)
        or marker.get("release_id") != release_id
        or type(release_manifest_value) is not str
        or not Path(release_manifest_value).is_absolute()
        or type(release_manifest_sha) is not str
        or not SHA256_RE.fullmatch(release_manifest_sha)
        or marker.get("release_manifest_sha256") != release_manifest_sha
        or type(ownership_target_value) is not str
        or not Path(ownership_target_value).is_absolute()
        or type(ownership_sha) is not str
        or not SHA256_RE.fullmatch(ownership_sha)
        or marker.get("ownership_manifest_sha256") != ownership_sha
    ):
        raise CutoverError("prepared deployment identity is invalid")
    inventory = _artifact_inventory(deployment_root, manifest)
    ownership = inventory.get("ownership/ownership-manifest.json")
    if ownership is None or ownership.sha256 != ownership_sha:
        raise CutoverError("prepared deployment ownership artifact is invalid")
    release_manifest_path = Path(release_manifest_value)
    release_root = release_manifest_path.parent
    if (
        release_manifest_path.name != "release-manifest.json"
        or release_root.resolve(strict=True) != release_root
        or release_root.is_symlink()
    ):
        raise CutoverError("prepared deployment release root is unsafe")
    release_raw = _safe_file_bytes(release_manifest_path, "release manifest", maximum=16 * 1024 * 1024)
    release = _load_json_bytes(release_raw, "release manifest")
    if (
        _sha256_bytes(release_raw) != release_manifest_sha
        or release.get("schema_version") != 1
        or release.get("immutable") is not True
        or release.get("release_id") != release_id
        or type(release.get("release_sha256")) is not str
        or not SHA256_RE.fullmatch(release["release_sha256"])
    ):
        raise CutoverError("prepared deployment release manifest drifted")
    _verify_release_inventory(release_root, release)
    roles = manifest.get("roles")
    if type(roles) is not list or len(roles) != len(ROLE_ORDER):
        raise CutoverError("prepared deployment role inventory is invalid")
    role_rows: dict[str, dict[str, Any]] = {}
    for row in roles:
        if type(row) is not dict or row.get("role") in role_rows:
            raise CutoverError("prepared deployment role inventory is invalid")
        role_rows[str(row.get("role"))] = row
    if set(role_rows) != set(ROLE_ORDER):
        raise CutoverError("prepared deployment role inventory is incomplete")
    plists: dict[str, BoundFile] = {}
    ports_by_role: dict[str, tuple[int, ...]] = {}
    for role in ROLE_ORDER:
        row = role_rows[role]
        label = LABEL_BY_ROLE[role]
        if row.get("label") != label:
            raise CutoverError(f"prepared deployment label mismatch: {role}")
        artifact = inventory.get(f"launchagents/{label}.plist")
        if artifact is None:
            raise CutoverError(f"prepared deployment plist missing: {label}")
        try:
            plist = plistlib.loads(artifact.path.read_bytes())
        except Exception as exc:
            raise CutoverError(f"prepared deployment plist is invalid: {label}") from exc
        ports = row.get("ports")
        if (
            type(plist) is not dict
            or plist.get("Label") != label
            or plist.get("ProgramArguments") != row.get("ProgramArguments")
            or plist.get("WorkingDirectory") != row.get("WorkingDirectory")
            or type(ports) is not list
            or any(type(port) is not int or not 1 <= port <= 65535 for port in ports)
        ):
            raise CutoverError(f"prepared deployment plist/role binding failed: {label}")
        plists[role] = artifact
        ports_by_role[role] = tuple(ports)
    return BoundDeployment(
        root=deployment_root,
        marker=BoundFile(marker_path, _sha256_bytes(marker_raw), len(marker_raw)),
        manifest=BoundFile(manifest_path, manifest_sha, len(manifest_raw)),
        release_id=release_id,
        release_root=release_root,
        release_manifest=BoundFile(
            release_manifest_path, release_manifest_sha, len(release_raw)
        ),
        release_sha=release["release_sha256"],
        ownership_source=ownership,
        ownership_target=Path(ownership_target_value),
        plists=plists,
        ports_by_role=ports_by_role,
    )


def _atomic_replace(path: Path, data: bytes, *, mode: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)
    return _sha256_bytes(data)


def _exclusive(path: Path, data: bytes, *, mode: int) -> str:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    return _sha256_bytes(data)


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )


def _default_observation_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a read-only ownership probe with a short, retryable timeout."""

    return subprocess.run(
        list(argv),
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )


def _default_readiness(urls: Sequence[str]) -> tuple[bool, Mapping[str, Any]]:
    deadline = time.monotonic() + 45.0
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        all_ready = True
        observed: dict[str, Any] = {}
        for url in urls:
            try:
                request = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(request, timeout=3.0) as response:
                    status = int(response.status)
                ok = 200 <= status < 400
                observed[url] = {"status": status, "ok": ok}
            except (OSError, urllib.error.URLError, ValueError) as exc:
                observed[url] = {"status": None, "ok": False, "error_type": type(exc).__name__}
                ok = False
            all_ready = all_ready and ok
        last = observed
        if all_ready:
            return True, observed
        time.sleep(0.5)
    return False, last


class V3RotationExecutor:
    def __init__(
        self,
        *,
        previous_deploy_root: Path,
        candidate_deploy_root: Path,
        rollback_deploy_root: Path,
        state_parent: Path,
        launchagents_directory: Path,
        evidence_dir: Path,
        gate_config: Path,
        campaign_id: str,
        hardware_id: str,
        gate_config_sha256: str,
        rollback_snapshot_directory: Path,
        report_output: Path,
        runner: Runner = _default_runner,
        observation_runner: Runner | None = None,
        observer: Observer | None = None,
        role_observer: RoleObserver | None = None,
        readiness_probe: ReadinessProbe = _default_readiness,
        clock: Callable[[], datetime] = _now,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        ownership_observation_timeout_seconds: float = (
            DEFAULT_OWNERSHIP_OBSERVATION_TIMEOUT_SECONDS
        ),
        ownership_observation_interval_seconds: float = (
            DEFAULT_OWNERSHIP_OBSERVATION_INTERVAL_SECONDS
        ),
        ownership_stable_observations: int = DEFAULT_OWNERSHIP_STABLE_OBSERVATIONS,
        uid: int | None = None,
    ) -> None:
        self.previous_root = previous_deploy_root
        self.candidate_root = candidate_deploy_root
        self.rollback_root = rollback_deploy_root
        self.state_parent = state_parent
        self.launchagents_directory = launchagents_directory
        self.evidence_dir = evidence_dir
        self.gate_config = gate_config
        self.campaign_id = campaign_id
        self.hardware_id = hardware_id
        self.gate_config_sha256 = gate_config_sha256
        self.snapshot_directory = rollback_snapshot_directory
        self.report_output = report_output
        self.runner = runner
        self.observation_runner = (
            observation_runner
            if observation_runner is not None
            else (_default_observation_runner if runner is _default_runner else runner)
        )
        self.observer = observer or self._observe
        self.role_observer = role_observer or self._observe_roles
        self.readiness_probe = readiness_probe
        self.clock = clock
        self.monotonic = monotonic
        self.sleeper = sleeper
        if ownership_observation_timeout_seconds <= 0:
            raise ValueError("ownership observation timeout must be positive")
        if ownership_observation_interval_seconds <= 0:
            raise ValueError("ownership observation interval must be positive")
        if ownership_stable_observations < 1:
            raise ValueError("ownership stable observation count must be positive")
        self.ownership_observation_timeout_seconds = float(
            ownership_observation_timeout_seconds
        )
        self.ownership_observation_interval_seconds = float(
            ownership_observation_interval_seconds
        )
        self.ownership_stable_observations = int(ownership_stable_observations)
        self.uid = os.getuid() if uid is None else uid
        self.events: list[dict[str, Any]] = []
        self.mutation_started = False

    def _event(self, action: str, **detail: Any) -> None:
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "at": self.clock().astimezone(timezone.utc).isoformat(),
                "action": action,
                **detail,
            }
        )

    def _command(self, argv: Sequence[str], *, tolerate_missing: bool = False) -> Any:
        result = self.runner(tuple(argv))
        returncode = int(getattr(result, "returncode", -1))
        self._event("command", argv=list(argv), returncode=returncode)
        if returncode != 0 and not tolerate_missing:
            raise CutoverError(f"command failed closed: {' '.join(argv[:3])}")
        return result

    def _observe(self, deployment: BoundDeployment | None) -> Mapping[str, Any]:
        labels: dict[str, Any] = {}
        pids: set[int] = set()
        for role in ROLE_ORDER:
            label = LABEL_BY_ROLE[role]
            result = self.observation_runner(
                ("/bin/launchctl", "print", f"gui/{self.uid}/{label}")
            )
            returncode = int(getattr(result, "returncode", -1))
            stdout = str(getattr(result, "stdout", "") or "")
            match = re.search(r"(?:^|\n)\s*pid\s*=\s*([1-9][0-9]*)\s*(?:\n|$)", stdout)
            pid = int(match.group(1)) if match else None
            loaded = returncode == 0
            if pid is not None:
                pids.add(pid)
            labels[role] = {"label": label, "loaded": loaded, "pid": pid}
            if deployment is None:
                if loaded:
                    return {"ok": False, "expected": "zero", "labels": labels}
            elif not loaded or pid is None or str(deployment.release_root) not in stdout:
                return {
                    "ok": False,
                    "expected": deployment.release_id,
                    "labels": labels,
                }
        ports: dict[str, list[int]] = {}
        expected_ports = sorted(
            {port for values in (deployment.ports_by_role.values() if deployment else ()) for port in values}
        )
        probe_ports = sorted(set(expected_ports) | {5002, 5003, 8088})
        for port in probe_ports:
            result = self.observation_runner(
                ("/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t")
            )
            raw = str(getattr(result, "stdout", "") or "")
            listeners = sorted({int(value) for value in raw.split() if value.isdigit()})
            ports[str(port)] = listeners
            if deployment is None and listeners:
                return {"ok": False, "expected": "zero", "labels": labels, "ports": ports}
            if deployment is not None and port in expected_ports and (
                not listeners or not set(listeners).issubset(pids)
            ):
                return {
                    "ok": False,
                    "expected": deployment.release_id,
                    "labels": labels,
                    "ports": ports,
                }
        return {
            "ok": True,
            "expected": "zero" if deployment is None else deployment.release_id,
            "owner_count": 0 if deployment is None else len(ROLE_ORDER),
            "labels": labels,
            "ports": ports,
        }

    def _observe_roles(
        self,
        deployment: BoundDeployment,
        roles: Sequence[str],
    ) -> Mapping[str, Any]:
        """Prove a dependency stage before starting roles that depend on it."""

        selected = tuple(roles)
        if not selected or any(role not in ROLE_ORDER for role in selected):
            raise CutoverError("startup role stage is invalid")
        labels: dict[str, Any] = {}
        pids: set[int] = set()
        for role in selected:
            label = LABEL_BY_ROLE[role]
            result = self.observation_runner(
                ("/bin/launchctl", "print", f"gui/{self.uid}/{label}")
            )
            returncode = int(getattr(result, "returncode", -1))
            stdout = str(getattr(result, "stdout", "") or "")
            match = re.search(r"(?:^|\n)\s*pid\s*=\s*([1-9][0-9]*)\s*(?:\n|$)", stdout)
            pid = int(match.group(1)) if match else None
            loaded = returncode == 0
            labels[role] = {"label": label, "loaded": loaded, "pid": pid}
            if not loaded or pid is None or str(deployment.release_root) not in stdout:
                return {
                    "ok": False,
                    "expected": deployment.release_id,
                    "roles": list(selected),
                    "owner_count": len(pids),
                    "labels": labels,
                }
            pids.add(pid)
        ports: dict[str, list[int]] = {}
        expected_ports = sorted(
            {
                port
                for role in selected
                for port in deployment.ports_by_role.get(role, ())
            }
        )
        for port in expected_ports:
            result = self.observation_runner(
                ("/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t")
            )
            raw = str(getattr(result, "stdout", "") or "")
            listeners = sorted({int(value) for value in raw.split() if value.isdigit()})
            ports[str(port)] = listeners
            if not listeners or not set(listeners).issubset(pids):
                return {
                    "ok": False,
                    "expected": deployment.release_id,
                    "roles": list(selected),
                    "owner_count": len(pids),
                    "labels": labels,
                    "ports": ports,
                }
        return {
            "ok": True,
            "expected": deployment.release_id,
            "roles": list(selected),
            "owner_count": len(selected),
            "labels": labels,
            "ports": ports,
        }

    def _require_startup_stage(
        self,
        deployment: BoundDeployment,
        roles: Sequence[str],
    ) -> Mapping[str, Any]:
        selected = tuple(roles)
        started = self.monotonic()
        deadline = started + self.ownership_observation_timeout_seconds
        attempts = 0
        consecutive_successes = 0
        last: dict[str, Any] = {}
        while True:
            attempts += 1
            try:
                value = dict(self.role_observer(deployment, selected))
            except (subprocess.TimeoutExpired, TimeoutError) as exc:
                value = {
                    "ok": False,
                    "expected": deployment.release_id,
                    "roles": list(selected),
                    "owner_count": None,
                    "error_type": type(exc).__name__,
                }
            last = value
            exact = (
                value.get("ok") is True
                and value.get("expected") == deployment.release_id
                and value.get("roles") == list(selected)
                and value.get("owner_count") == len(selected)
            )
            consecutive_successes = consecutive_successes + 1 if exact else 0
            if consecutive_successes >= self.ownership_stable_observations:
                self._event(
                    "startup_stage",
                    expected=deployment.release_id,
                    roles=list(selected),
                    observation=value,
                    attempts=attempts,
                    stable_observations=consecutive_successes,
                    elapsed_seconds=round(max(0.0, self.monotonic() - started), 6),
                )
                return value
            now = self.monotonic()
            if now >= deadline:
                self._event(
                    "startup_stage_timeout",
                    expected=deployment.release_id,
                    roles=list(selected),
                    attempts=attempts,
                    elapsed_seconds=round(max(0.0, now - started), 6),
                    last_observation=last,
                )
                raise CutoverError(
                    f"startup dependency stage timed out: {deployment.release_id}:{','.join(selected)}"
                )
            self.sleeper(
                min(
                    self.ownership_observation_interval_seconds,
                    max(0.0, deadline - now),
                )
            )

    def _require_observation(self, deployment: BoundDeployment | None) -> Mapping[str, Any]:
        expected = "zero" if deployment is None else deployment.release_id
        expected_count = 0 if deployment is None else len(ROLE_ORDER)
        started = self.monotonic()
        deadline = started + self.ownership_observation_timeout_seconds
        attempts = 0
        consecutive_successes = 0
        last: dict[str, Any] = {}
        while True:
            attempts += 1
            try:
                value = dict(self.observer(deployment))
            except (subprocess.TimeoutExpired, TimeoutError) as exc:
                value = {
                    "ok": False,
                    "expected": expected,
                    "owner_count": None,
                    "error_type": type(exc).__name__,
                }
                if isinstance(exc, subprocess.TimeoutExpired):
                    value["command"] = (
                        list(exc.cmd) if not isinstance(exc.cmd, str) else exc.cmd
                    )
                    value["timeout_seconds"] = exc.timeout
            last = value
            exact = (
                value.get("ok") is True
                and value.get("expected") == expected
                and value.get("owner_count") == expected_count
            )
            consecutive_successes = consecutive_successes + 1 if exact else 0
            if consecutive_successes >= self.ownership_stable_observations:
                self._event(
                    "ownership",
                    expected=expected,
                    observation=value,
                    attempts=attempts,
                    stable_observations=consecutive_successes,
                    elapsed_seconds=round(max(0.0, self.monotonic() - started), 6),
                )
                return value
            now = self.monotonic()
            if now >= deadline:
                self._event(
                    "ownership_timeout",
                    expected=expected,
                    attempts=attempts,
                    elapsed_seconds=round(max(0.0, now - started), 6),
                    last_observation=last,
                )
                raise CutoverError(f"exclusive ownership proof timed out: {expected}")
            self.sleeper(
                min(
                    self.ownership_observation_interval_seconds,
                    max(0.0, deadline - now),
                )
            )

    def _verify_gate(self, candidate: BoundDeployment) -> Mapping[str, Any]:
        config_raw = _safe_file_bytes(self.gate_config, "release gate configuration")
        if _sha256_bytes(config_raw) != self.gate_config_sha256:
            raise CutoverError("release gate configuration SHA-256 drifted")
        config = _load_json_bytes(config_raw, "release gate configuration")
        context = {
            "campaign_id": self.campaign_id,
            "release_sha": candidate.release_sha,
            "hardware_id": self.hardware_id,
            "gate_config_sha256": self.gate_config_sha256,
        }
        try:
            report = evaluate_evidence(
                config,
                self.evidence_dir,
                expected_context=context,
                now=self.clock(),
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise CutoverError("final release gate recomputation failed closed") from exc
        required = config.get("required_evidence")
        if (
            report.get("decision") != "GO"
            or type(required) is not list
            or report.get("required_count") != len(required)
            or report.get("passed_count") != len(required)
            or report.get("missing") != []
            or report.get("failed") != []
            or report.get("invalid") != {}
            or "human_go_approval_recorded" not in report.get("passed", [])
        ):
            raise CutoverError("final release gate is not exact GO")
        self._event("release_gate", decision="GO", passed_count=len(required))
        return report

    def _verify_deployment_relationships(
        self,
        previous: BoundDeployment,
        candidate: BoundDeployment,
        rollback: BoundDeployment,
    ) -> None:
        if (
            previous.release_id == candidate.release_id
            or rollback.release_id != previous.release_id
            or rollback.release_root != previous.release_root
            or rollback.release_manifest.sha256 != previous.release_manifest.sha256
            or candidate.release_root == previous.release_root
            or len({item.ownership_target for item in (previous, candidate, rollback)}) != 1
        ):
            raise CutoverError("candidate/predecessor/rollback relationship is invalid")

    def _verify_current_install(self, previous: BoundDeployment) -> None:
        _safe_absolute_directory(self.launchagents_directory, "canonical LaunchAgents directory")
        for role in ROLE_ORDER:
            target = self.launchagents_directory / f"{LABEL_BY_ROLE[role]}.plist"
            if _sha256(target) != previous.plists[role].sha256:
                raise CutoverError(f"installed predecessor plist drifted: {role}")
        if _sha256(previous.ownership_target) != previous.ownership_source.sha256:
            raise CutoverError("installed predecessor ownership manifest drifted")

    def _persist_snapshot(
        self,
        *,
        marker_path: Path,
        journal_path: Path,
        previous: BoundDeployment,
        candidate: BoundDeployment,
        rollback: BoundDeployment,
    ) -> tuple[Mapping[str, Any], Mapping[str, bytes]]:
        root = self.snapshot_directory.expanduser()
        if not root.is_absolute() or root.resolve(strict=False) != root or root.exists() or root.is_symlink():
            raise CutoverError("rollback snapshot directory must be a new canonical absolute path")
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.mkdir(mode=0o700)
        sources = {
            "active-release.json": _safe_file_bytes(marker_path, "active release marker"),
            "cutover-activation.json": _safe_file_bytes(journal_path, "activation journal"),
            "ownership-manifest.json": _safe_file_bytes(
                previous.ownership_target, "installed ownership manifest"
            ),
            **{
                f"{LABEL_BY_ROLE[role]}.plist": _safe_file_bytes(
                    self.launchagents_directory / f"{LABEL_BY_ROLE[role]}.plist",
                    f"installed predecessor plist {role}",
                )
                for role in ROLE_ORDER
            },
        }
        rows = []
        for name, raw in sources.items():
            _exclusive(root / name, raw, mode=0o600)
            rows.append({"name": name, "sha256": _sha256_bytes(raw), "size": len(raw)})
        manifest = {
            "schema": "magi.v3.rotation-rollback-snapshot/v1",
            "status": "persisted_before_mutation",
            "created_at": self.clock().astimezone(timezone.utc).isoformat(),
            "previous_release_id": previous.release_id,
            "previous_deploy_manifest_sha256": previous.manifest.sha256,
            "candidate_release_id": candidate.release_id,
            "candidate_deploy_manifest_sha256": candidate.manifest.sha256,
            "rollback_release_id": rollback.release_id,
            "rollback_deploy_manifest_sha256": rollback.manifest.sha256,
            "files": sorted(rows, key=lambda row: row["name"]),
            "mutation_performed": False,
        }
        _exclusive(root / "snapshot-manifest.json", _canonical_json(manifest), mode=0o600)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        self._event(
            "rollback_snapshot",
            manifest_sha256=_sha256(root / "snapshot-manifest.json"),
            file_count=len(rows),
        )
        return manifest, sources

    def _install(self, deployment: BoundDeployment) -> None:
        _atomic_replace(
            deployment.ownership_target,
            deployment.ownership_source.path.read_bytes(),
            mode=0o600,
        )
        for role in ROLE_ORDER:
            target = self.launchagents_directory / f"{LABEL_BY_ROLE[role]}.plist"
            _atomic_replace(target, deployment.plists[role].path.read_bytes(), mode=0o644)
        for role in ROLE_ORDER:
            target = self.launchagents_directory / f"{LABEL_BY_ROLE[role]}.plist"
            if _sha256(target) != deployment.plists[role].sha256:
                raise CutoverError(f"installed deployment plist failed verification: {role}")
        if _sha256(deployment.ownership_target) != deployment.ownership_source.sha256:
            raise CutoverError("installed deployment ownership failed verification")
        self._event("install", release_id=deployment.release_id)

    def _stop_all(self) -> None:
        self.mutation_started = True
        for role in STOP_ORDER:
            self._command(
                ("/bin/launchctl", "bootout", f"gui/{self.uid}/{LABEL_BY_ROLE[role]}"),
                tolerate_missing=True,
            )

    def _start_all(self, deployment: BoundDeployment) -> None:
        # Control owns the global release lock and 8088.  Gateway and
        # supervisor both fail closed unless that exact same-release owner is
        # already active.  Bootstrapping all three back-to-back creates a real
        # launchd race in which each role can reject a sibling's listener and
        # enter a KeepAlive restart loop.  Establish control first, then start
        # the dependent roles.
        control = "control"
        target = self.launchagents_directory / f"{LABEL_BY_ROLE[control]}.plist"
        self._command(("/bin/launchctl", "bootstrap", f"gui/{self.uid}", str(target)))
        self._require_startup_stage(deployment, (control,))
        for role in ROLE_ORDER:
            if role == control:
                continue
            target = self.launchagents_directory / f"{LABEL_BY_ROLE[role]}.plist"
            self._command(("/bin/launchctl", "bootstrap", f"gui/{self.uid}", str(target)))

    def _write_report(self, report: Mapping[str, Any]) -> None:
        _atomic_replace(self.report_output, _canonical_json(report), mode=0o600)

    def _rollback(
        self,
        *,
        rollback: BoundDeployment,
        marker_path: Path,
        journal_path: Path,
        snapshot_bytes: Mapping[str, bytes],
    ) -> Mapping[str, Any]:
        self._stop_all()
        self._require_observation(None)
        self._install(rollback)
        _atomic_replace(marker_path, snapshot_bytes["active-release.json"], mode=0o600)
        _atomic_replace(journal_path, snapshot_bytes["cutover-activation.json"], mode=0o600)
        self._start_all(rollback)
        observation = self._require_observation(rollback)
        ready, detail = self.readiness_probe(DEFAULT_READINESS_URLS)
        self._event("rollback_readiness", ok=bool(ready), detail=dict(detail))
        if not ready:
            raise CutoverError("r59 rollback readiness failed")
        return observation

    def execute(self) -> dict[str, Any]:
        started_at = self.clock().astimezone(timezone.utc).isoformat()
        previous = load_bound_deployment(self.previous_root)
        candidate = load_bound_deployment(self.candidate_root)
        rollback = load_bound_deployment(self.rollback_root)
        self._verify_deployment_relationships(previous, candidate, rollback)
        state_parent = _safe_absolute_directory(self.state_parent, "activation state parent")
        marker_path = state_parent / "active-release.json"
        journal_path = state_parent / "cutover-activation.json"
        marker = active_release_marker(
            marker_path,
            expected_release="v3",
            expected_release_id=previous.release_id,
            expected_release_root=previous.release_root,
            expected_manifest_sha256=previous.release_manifest.sha256,
        )
        del marker
        self._verify_gate(candidate)
        self._verify_current_install(previous)
        self._require_observation(previous)
        snapshot_bytes: Mapping[str, bytes] = {}
        with acquire_active_release_admission(state_parent):
            # Recheck every mutable and immutable boundary under the shared
            # admission lock before the first service stop.
            previous = load_bound_deployment(self.previous_root)
            candidate = load_bound_deployment(self.candidate_root)
            rollback = load_bound_deployment(self.rollback_root)
            self._verify_deployment_relationships(previous, candidate, rollback)
            self._verify_gate(candidate)
            self._verify_current_install(previous)
            self._require_observation(previous)
            _snapshot, snapshot_bytes = self._persist_snapshot(
                marker_path=marker_path,
                journal_path=journal_path,
                previous=previous,
                candidate=candidate,
                rollback=rollback,
            )
            plan_sha256 = _sha256(self.snapshot_directory / "snapshot-manifest.json")
            try:
                self._stop_all()
                zero = self._require_observation(None)
                transaction = V3RotationTransaction.begin(
                    state_parent=state_parent,
                    plan_sha256=plan_sha256,
                    previous_marker_sha256=_sha256_bytes(snapshot_bytes["active-release.json"]),
                    previous_journal_sha256=_sha256_bytes(snapshot_bytes["cutover-activation.json"]),
                    previous_release_id=previous.release_id,
                    previous_release_root=previous.release_root,
                    previous_release_manifest_sha256=previous.release_manifest.sha256,
                    candidate_release_id=candidate.release_id,
                    candidate_release_root=candidate.release_root,
                    candidate_release_manifest_sha256=candidate.release_manifest.sha256,
                    candidate_deployment_manifest_sha256=candidate.manifest.sha256,
                    rollback_deployment_manifest_sha256=rollback.manifest.sha256,
                    reconciliation_before=zero,
                    clock=lambda: self.clock().astimezone(timezone.utc).isoformat(),
                )
                transaction.advance("previous_v3_zero", observation=zero)
                # Close the stop-to-install TOCTOU gap without re-running the
                # formal campaign: re-hash candidate and recompute final GO.
                candidate = load_bound_deployment(self.candidate_root)
                self._verify_gate(candidate)
                self._require_observation(None)
                self._install(candidate)
                transaction.advance(
                    "candidate_files_installed",
                    candidate_deployment_manifest_sha256=candidate.manifest.sha256,
                )
                commit = transaction.commit_candidate()
                self._start_all(candidate)
                after = self._require_observation(candidate)
                ready, detail = self.readiness_probe(DEFAULT_READINESS_URLS)
                self._event("candidate_readiness", ok=bool(ready), detail=dict(detail))
                if not ready:
                    raise CutoverError("candidate readiness verification failed")
                transaction.mark_active(reconciliation_after=after)
            except BaseException as exc:
                try:
                    rollback_observation = self._rollback(
                        rollback=rollback,
                        marker_path=marker_path,
                        journal_path=journal_path,
                        snapshot_bytes=snapshot_bytes,
                    )
                except BaseException as rollback_exc:
                    report = {
                        "schema": "magi.v3.rotation-execution/v1",
                        "status": "rollback_failed",
                        "ok": False,
                        "mutation_performed": self.mutation_started,
                        "rollback_performed": True,
                        "error_type": type(exc).__name__,
                        "rollback_error_type": type(rollback_exc).__name__,
                        "previous_release_id": previous.release_id,
                        "candidate_release_id": candidate.release_id,
                        "events": self.events,
                        "started_at": started_at,
                        "finished_at": self.clock().astimezone(timezone.utc).isoformat(),
                    }
                    self._write_report(report)
                    raise CutoverError("candidate failed and r59 rollback failed closed") from rollback_exc
                report = {
                    "schema": "magi.v3.rotation-execution/v1",
                    "status": "rolled_back_to_previous",
                    "ok": False,
                    "mutation_performed": True,
                    "rollback_performed": True,
                    "error_type": type(exc).__name__,
                    "previous_release_id": previous.release_id,
                    "candidate_release_id": candidate.release_id,
                    "rollback_deploy_manifest_sha256": rollback.manifest.sha256,
                    "rollback_observation": rollback_observation,
                    "rollback_snapshot_manifest_sha256": plan_sha256,
                    "events": self.events,
                    "started_at": started_at,
                    "finished_at": self.clock().astimezone(timezone.utc).isoformat(),
                }
                self._write_report(report)
                return report
        report = {
            "schema": "magi.v3.rotation-execution/v1",
            "status": "candidate_active",
            "ok": True,
            "mutation_performed": True,
            "rollback_performed": False,
            "previous_release_id": previous.release_id,
            "candidate_release_id": candidate.release_id,
            "candidate_release_manifest_sha256": candidate.release_manifest.sha256,
            "candidate_deploy_manifest_sha256": candidate.manifest.sha256,
            "rollback_release_id": rollback.release_id,
            "rollback_deploy_manifest_sha256": rollback.manifest.sha256,
            "rollback_snapshot_manifest_sha256": plan_sha256,
            "activation_transaction_id": transaction.transaction_id,
            "active_release_marker_sha256": commit["active_release_marker_sha256"],
            "events": self.events,
            "started_at": started_at,
            "finished_at": self.clock().astimezone(timezone.utc).isoformat(),
        }
        self._write_report(report)
        return report


def recover_previous_from_snapshot(
    *,
    rollback_deploy_root: Path,
    state_parent: Path,
    launchagents_directory: Path,
    rollback_snapshot_directory: Path,
    expected_snapshot_manifest_sha256: str,
    report_output: Path,
    runner: Runner = _default_runner,
    observer: Observer | None = None,
    role_observer: RoleObserver | None = None,
    readiness_probe: ReadinessProbe = _default_readiness,
    clock: Callable[[], datetime] = _now,
    uid: int | None = None,
) -> dict[str, Any]:
    """Recover the predecessor after an uncatchable interrupted rotation."""

    if not SHA256_RE.fullmatch(expected_snapshot_manifest_sha256):
        raise CutoverError("emergency rollback snapshot SHA-256 is invalid")
    snapshot_root = _safe_absolute_directory(
        rollback_snapshot_directory, "emergency rollback snapshot"
    )
    snapshot_manifest_path = snapshot_root / "snapshot-manifest.json"
    snapshot_manifest_raw = _safe_file_bytes(
        snapshot_manifest_path, "emergency rollback snapshot manifest"
    )
    if _sha256_bytes(snapshot_manifest_raw) != expected_snapshot_manifest_sha256:
        raise CutoverError("emergency rollback snapshot manifest drifted")
    snapshot_manifest = _load_json_bytes(
        snapshot_manifest_raw, "emergency rollback snapshot manifest"
    )
    rollback = load_bound_deployment(rollback_deploy_root)
    rows = snapshot_manifest.get("files")
    expected_names = {
        "active-release.json",
        "cutover-activation.json",
        "ownership-manifest.json",
        *(f"{label}.plist" for label in LABEL_BY_ROLE.values()),
    }
    if (
        snapshot_manifest.get("schema") != "magi.v3.rotation-rollback-snapshot/v1"
        or snapshot_manifest.get("status") != "persisted_before_mutation"
        or snapshot_manifest.get("mutation_performed") is not False
        or snapshot_manifest.get("previous_release_id") != rollback.release_id
        or snapshot_manifest.get("rollback_release_id") != rollback.release_id
        or snapshot_manifest.get("rollback_deploy_manifest_sha256")
        != rollback.manifest.sha256
        or type(rows) is not list
        or {row.get("name") for row in rows if type(row) is dict} != expected_names
        or len(rows) != len(expected_names)
    ):
        raise CutoverError("emergency rollback snapshot identity is invalid")
    snapshot_bytes: dict[str, bytes] = {}
    for row in rows:
        if (
            type(row) is not dict
            or set(row) != {"name", "sha256", "size"}
            or type(row.get("name")) is not str
            or type(row.get("sha256")) is not str
            or not SHA256_RE.fullmatch(row["sha256"])
            or type(row.get("size")) is not int
            or isinstance(row.get("size"), bool)
            or row["size"] < 1
        ):
            raise CutoverError("emergency rollback snapshot file binding is invalid")
        raw = _safe_file_bytes(
            snapshot_root / row["name"],
            f"emergency rollback snapshot file {row['name']}",
            maximum=row["size"],
        )
        if len(raw) != row["size"] or _sha256_bytes(raw) != row["sha256"]:
            raise CutoverError("emergency rollback snapshot file drifted")
        snapshot_bytes[row["name"]] = raw
    state = _safe_absolute_directory(state_parent, "activation state parent")
    launchagents = _safe_absolute_directory(
        launchagents_directory, "canonical LaunchAgents directory"
    )
    executor = V3RotationExecutor(
        previous_deploy_root=rollback_deploy_root,
        candidate_deploy_root=rollback_deploy_root,
        rollback_deploy_root=rollback_deploy_root,
        state_parent=state,
        launchagents_directory=launchagents,
        evidence_dir=snapshot_root,
        gate_config=snapshot_manifest_path,
        campaign_id="emergency-recovery",
        hardware_id="local-host",
        gate_config_sha256=expected_snapshot_manifest_sha256,
        rollback_snapshot_directory=snapshot_root,
        report_output=report_output,
        runner=runner,
        observer=observer,
        role_observer=role_observer,
        readiness_probe=readiness_probe,
        clock=clock,
        uid=uid,
    )
    started_at = clock().astimezone(timezone.utc).isoformat()
    with acquire_active_release_admission(state):
        executor._stop_all()
        executor._require_observation(None)
        executor._install(rollback)
        marker_path = state / "active-release.json"
        journal_path = state / "cutover-activation.json"
        _atomic_replace(marker_path, snapshot_bytes["active-release.json"], mode=0o600)
        _atomic_replace(
            journal_path, snapshot_bytes["cutover-activation.json"], mode=0o600
        )
        active_release_marker(
            marker_path,
            expected_release="v3",
            expected_release_id=rollback.release_id,
            expected_release_root=rollback.release_root,
            expected_manifest_sha256=rollback.release_manifest.sha256,
        )
        executor._start_all(rollback)
        observation = executor._require_observation(rollback)
        ready, detail = readiness_probe(DEFAULT_READINESS_URLS)
        executor._event(
            "emergency_rollback_readiness", ok=bool(ready), detail=dict(detail)
        )
        if not ready:
            raise CutoverError("emergency r59 rollback readiness failed")
    report = {
        "schema": "magi.v3.rotation-recovery/v1",
        "status": "previous_recovered",
        "ok": True,
        "rollback_performed": True,
        "release_id": rollback.release_id,
        "rollback_deploy_manifest_sha256": rollback.manifest.sha256,
        "rollback_snapshot_manifest_sha256": expected_snapshot_manifest_sha256,
        "observation": observation,
        "events": executor.events,
        "started_at": started_at,
        "finished_at": clock().astimezone(timezone.utc).isoformat(),
    }
    executor._write_report(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-deploy", type=Path, required=True)
    parser.add_argument("--candidate-deploy", type=Path, required=True)
    parser.add_argument("--rollback-deploy", type=Path, required=True)
    parser.add_argument("--state-parent", type=Path, required=True)
    parser.add_argument("--launchagents-directory", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--gate-config", type=Path, required=True)
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--gate-config-sha256", required=True)
    parser.add_argument("--rollback-snapshot-directory", type=Path, required=True)
    parser.add_argument("--report-output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = V3RotationExecutor(
            previous_deploy_root=args.previous_deploy,
            candidate_deploy_root=args.candidate_deploy,
            rollback_deploy_root=args.rollback_deploy,
            state_parent=args.state_parent,
            launchagents_directory=args.launchagents_directory,
            evidence_dir=args.evidence_dir,
            gate_config=args.gate_config,
            campaign_id=args.campaign_id,
            hardware_id=args.hardware_id,
            gate_config_sha256=args.gate_config_sha256,
            rollback_snapshot_directory=args.rollback_snapshot_directory,
            report_output=args.report_output,
        ).execute()
    except (CutoverError, OSError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__}))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ok") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BoundDeployment",
    "DEFAULT_READINESS_URLS",
    "LABEL_BY_ROLE",
    "ROLE_ORDER",
    "STOP_ORDER",
    "V3RotationExecutor",
    "load_bound_deployment",
    "recover_previous_from_snapshot",
]

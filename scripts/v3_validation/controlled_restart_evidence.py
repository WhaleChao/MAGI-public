#!/usr/bin/env python3
"""Prepare and verify a candidate-bound controlled macOS restart drill.

The certifier is split across two invocations. ``prepare`` records the current
boot session, V2 readiness/port ownership, and a FULL/WAL SQLite sentinel.
After the operator performs a normal macOS restart, ``finalize`` proves a new
boot session, restored V2 single ownership, continued V3 absence, and durable
sentinel integrity.  This module never invokes reboot/shutdown and never reads
MAGI business data, NAS content, or customer files.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


PLAN_SCHEMA = "magi.v3.controlled-restart-plan/v1"
REPORT_SCHEMA = "magi.v3.controlled-restart-evidence/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
BOOT_UUID_RE = re.compile(
    r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$"
)
BOOT_TIME_RE = re.compile(r"sec\s*=\s*(\d+),\s*usec\s*=\s*(\d+)")
REQUIRED_ENDPOINTS = (
    (5002, "/readyz"),
    (5003, "/health"),
    (5014, "/health"),
    (8088, "/health"),
)
REQUIRED_PORTS = tuple(port for port, _path in REQUIRED_ENDPOINTS)
V3_LABELS = (
    "com.magi.v3.control",
    "com.magi.v3.gateway",
    "com.magi.v3.supervisor",
)


class ControlledRestartBlocked(RuntimeError):
    pass


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(value), sort_keys=True, separators=(",", ":")) + "\n").encode()


def _semantic(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(dict(value), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ControlledRestartBlocked(f"{description} is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlledRestartBlocked(f"{description} must be an object")
    return value


def _write_new(path: Path, value: Mapping[str, Any], mode: int) -> str:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise ControlledRestartBlocked("controlled-restart output path is unsafe")
    raw.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = _canonical(value)
    descriptor = os.open(
        raw,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(raw.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return hashlib.sha256(payload).hexdigest()


def _context(value: Mapping[str, Any]) -> dict[str, str]:
    result = {
        key: value.get(key)
        for key in ("campaign_id", "release_sha", "hardware_id", "gate_config_sha256")
    }
    if (
        any(not isinstance(item, str) or not item for item in result.values())
        or not SHA256_RE.fullmatch(str(result["release_sha"]))
        or not SHA256_RE.fullmatch(str(result["gate_config_sha256"]))
    ):
        raise ControlledRestartBlocked("controlled-restart context is incomplete")
    return {key: str(item) for key, item in result.items()}


def _time(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ControlledRestartBlocked("controlled-restart timestamp is invalid") from exc
    if result.tzinfo is None:
        raise ControlledRestartBlocked("controlled-restart timestamp lacks timezone")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class HostObservation:
    boot_session_uuid: str
    boot_time_epoch: float
    endpoints: tuple[dict[str, Any], ...]
    listener_pids: dict[int, tuple[int, ...]]
    v3_loaded_labels: tuple[str, ...]
    v3_process_pids: tuple[int, ...]

    def public(self) -> dict[str, Any]:
        return {
            "boot_session_uuid": self.boot_session_uuid,
            "boot_time_epoch": self.boot_time_epoch,
            "endpoints": [dict(item) for item in self.endpoints],
            "listener_pids": {
                str(port): list(self.listener_pids[port]) for port in REQUIRED_PORTS
            },
            "v3_loaded_labels": list(self.v3_loaded_labels),
            "v3_process_pids": list(self.v3_process_pids),
        }


class ObservationBackend(Protocol):
    def observe(self, release_root: Path) -> HostObservation: ...


class HostBackend:
    @staticmethod
    def _run(argv: Sequence[str], timeout: int = 10) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        )

    @classmethod
    def _boot_identity(cls) -> tuple[str, float]:
        session = cls._run(("/usr/sbin/sysctl", "-n", "kern.bootsessionuuid"))
        boottime = cls._run(("/usr/sbin/sysctl", "-n", "kern.boottime"))
        uuid_value = session.stdout.strip().upper()
        match = BOOT_TIME_RE.search(boottime.stdout)
        if (
            session.returncode != 0
            or boottime.returncode != 0
            or not BOOT_UUID_RE.fullmatch(uuid_value)
            or match is None
        ):
            raise ControlledRestartBlocked("macOS boot identity probe failed")
        return uuid_value, int(match.group(1)) + int(match.group(2)) / 1_000_000

    @staticmethod
    def _endpoints() -> tuple[dict[str, Any], ...]:
        rows: list[dict[str, Any]] = []
        for port, path in REQUIRED_ENDPOINTS:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                body = response.read(1024 * 1024)
            except OSError as exc:
                raise ControlledRestartBlocked(
                    f"V2 endpoint unavailable after controlled restart: {port}{path}: {exc}"
                ) from exc
            finally:
                connection.close()
            rows.append(
                {
                    "port": port,
                    "path": path,
                    "status": response.status,
                    "body_sha256": hashlib.sha256(body).hexdigest(),
                }
            )
        return tuple(rows)

    @classmethod
    def _listeners(cls) -> dict[int, tuple[int, ...]]:
        result = cls._run(
            ("/usr/sbin/lsof", "-b", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"),
            timeout=15,
        )
        if result.returncode not in {0, 1}:
            raise ControlledRestartBlocked("listener inventory failed")
        current: int | None = None
        rows = {port: set() for port in REQUIRED_PORTS}
        for line in result.stdout.splitlines():
            if re.fullmatch(r"p\d+", line):
                current = int(line[1:])
                continue
            if current is None or not line.startswith("n"):
                continue
            match = re.search(r":(\d+)$", line[1:])
            if match and int(match.group(1)) in rows:
                rows[int(match.group(1))].add(current)
        return {port: tuple(sorted(rows[port])) for port in REQUIRED_PORTS}

    @classmethod
    def _v3_labels(cls) -> tuple[str, ...]:
        loaded: list[str] = []
        for label in V3_LABELS:
            result = cls._run(("/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"))
            if result.returncode == 0:
                loaded.append(label)
            elif result.returncode != 113 and "Could not find service" not in result.stderr:
                raise ControlledRestartBlocked(f"launchd status failed for {label}")
        return tuple(loaded)

    @classmethod
    def _v3_processes(cls, release_root: Path) -> tuple[int, ...]:
        result = cls._run(("/bin/ps", "-axo", "pid=,ppid=,command="))
        if result.returncode != 0:
            raise ControlledRestartBlocked("process inventory failed")
        root = str(release_root)
        rows: dict[int, tuple[int, str]] = {}
        for line in result.stdout.splitlines():
            match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)$", line)
            if match:
                rows[int(match.group(1))] = (int(match.group(2)), match.group(3))

        # The certifier itself necessarily runs from the candidate release root.
        # Exclude its complete observer process tree, but no unrelated process.
        observer: set[int] = {os.getpid()}
        cursor = os.getpid()
        while cursor in rows:
            parent = rows[cursor][0]
            if parent <= 1 or parent in observer:
                break
            observer.add(parent)
            cursor = parent
        changed = True
        while changed:
            changed = False
            for pid, (parent, _command) in rows.items():
                if parent in observer and pid not in observer:
                    observer.add(pid)
                    changed = True
        return tuple(
            sorted(
                pid
                for pid, (_parent, command) in rows.items()
                if pid not in observer and root in command
            )
        )

    def observe(self, release_root: Path) -> HostObservation:
        session, boottime = self._boot_identity()
        return HostObservation(
            boot_session_uuid=session,
            boot_time_epoch=boottime,
            endpoints=self._endpoints(),
            listener_pids=self._listeners(),
            v3_loaded_labels=self._v3_labels(),
            v3_process_pids=self._v3_processes(release_root),
        )


def _verify_observation(value: HostObservation) -> None:
    if (
        not BOOT_UUID_RE.fullmatch(value.boot_session_uuid)
        or value.boot_time_epoch <= 0
        or tuple((row.get("port"), row.get("path")) for row in value.endpoints)
        != REQUIRED_ENDPOINTS
        or any(
            row.get("status") != 200
            or not SHA256_RE.fullmatch(str(row.get("body_sha256") or ""))
            for row in value.endpoints
        )
        or set(value.listener_pids) != set(REQUIRED_PORTS)
        or any(len(value.listener_pids[port]) != 1 for port in REQUIRED_PORTS)
        or value.v3_loaded_labels
        or value.v3_process_pids
    ):
        raise ControlledRestartBlocked("V2 single-owner/V3-absent observation failed")


def _release_binding(release_root: Path, manifest_sha256: str) -> dict[str, Any]:
    root = release_root.expanduser().resolve(strict=True)
    manifest_path = root / "release-manifest.json"
    manifest = _json(manifest_path, "release manifest")
    files = {
        str(row.get("path")): str(row.get("sha256"))
        for row in manifest.get("files", [])
        if isinstance(row, dict)
    }
    source = root / "scripts/v3_validation/controlled_restart_evidence.py"
    if (
        not root.is_dir()
        or not SHA256_RE.fullmatch(manifest_sha256)
        or _sha256(manifest_path) != manifest_sha256
        or manifest.get("immutable") is not True
        or files.get("scripts/v3_validation/controlled_restart_evidence.py")
        != _sha256(source)
    ):
        raise ControlledRestartBlocked("controlled-restart release binding failed")
    return {
        "release_root": str(root),
        "release_manifest_path": str(manifest_path),
        "release_manifest_sha256": manifest_sha256,
        "certifier_sha256": _sha256(source),
    }


def _create_sentinel(path: Path, restart_id: str) -> dict[str, Any]:
    if path.exists() or path.is_symlink():
        raise ControlledRestartBlocked("controlled-restart sentinel path must be new")
    with sqlite3.connect(path, timeout=5) as connection:
        mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("CREATE TABLE restart_sentinel(restart_id TEXT PRIMARY KEY, value TEXT)")
        connection.execute(
            "INSERT INTO restart_sentinel VALUES (?, 'durable-before-controlled-restart')",
            (restart_id,),
        )
        connection.commit()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    if mode != "wal" or integrity != "ok":
        raise ControlledRestartBlocked("controlled-restart sentinel initialization failed")
    return {"path": str(path), "restart_id": restart_id, "journal_mode": mode}


def _verify_sentinel(
    binding: Mapping[str, Any], *, path_override: Path | None = None
) -> dict[str, Any]:
    try:
        path = (
            path_override.expanduser().resolve(strict=True)
            if path_override is not None
            else Path(str(binding.get("path") or "")).resolve(strict=True)
        )
        restart_id = str(binding.get("restart_id") or "")
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5) as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            rows = connection.execute(
                "SELECT restart_id, value FROM restart_sentinel"
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        raise ControlledRestartBlocked(
            "controlled-restart sentinel is unreadable"
        ) from exc
    expected = [(restart_id, "durable-before-controlled-restart")]
    if integrity != "ok" or rows != expected:
        raise ControlledRestartBlocked("controlled-restart sentinel did not survive exactly")
    return {
        "integrity_check": integrity,
        "row_count": len(rows),
        "restart_id_sha256": hashlib.sha256(restart_id.encode()).hexdigest(),
        "database_sha256": _sha256(path),
    }


def prepare_plan(
    *,
    release_root: Path,
    release_manifest_sha256: str,
    output: Path,
    workdir: Path,
    authorization_statement_sha256: str,
    expected_context: Mapping[str, Any],
    backend: ObservationBackend | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    context = _context(expected_context)
    if not SHA256_RE.fullmatch(authorization_statement_sha256):
        raise ControlledRestartBlocked("authorization statement SHA-256 is invalid")
    root = workdir.expanduser()
    if not root.is_absolute() or root.resolve(strict=False) != root or root.exists() or root.is_symlink():
        raise ControlledRestartBlocked("controlled-restart workdir must be new and canonical")
    root.mkdir(parents=True, mode=0o700)
    release = _release_binding(release_root, release_manifest_sha256)
    observer = backend or HostBackend()
    before = observer.observe(Path(release["release_root"]))
    _verify_observation(before)
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    restart_id = uuid.uuid4().hex
    sentinel = _create_sentinel(root / "restart-sentinel.sqlite3", restart_id)
    unsigned = {
        "schema": PLAN_SCHEMA,
        "schema_version": 1,
        "plan_id": uuid.uuid4().hex,
        **context,
        "prepared_at": instant.isoformat(),
        "expires_at": (instant + timedelta(hours=24)).isoformat(),
        "authorization_statement_sha256": authorization_statement_sha256,
        "release_binding": release,
        "workdir": str(root),
        "sentinel": sentinel,
        "before": before.public(),
        "required_endpoints": [list(item) for item in REQUIRED_ENDPOINTS],
        "required_v3_absent_labels": list(V3_LABELS),
        "reboot_invoked_by_certifier": False,
        "external_device_required": False,
        "hard_power_removal_required": False,
    }
    plan = {**unsigned, "plan_sha256": _semantic(unsigned)}
    plan_file_sha256 = _write_new(output, plan, 0o400)
    return {
        "status": "prepared_waiting_for_controlled_restart",
        "plan": str(output),
        "plan_file_sha256": plan_file_sha256,
        "before_boot_session_uuid": before.boot_session_uuid,
    }


def finalize_plan(
    *,
    plan_path: Path,
    plan_sha256: str,
    output: Path,
    backend: ObservationBackend | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not SHA256_RE.fullmatch(plan_sha256) or _sha256(plan_path) != plan_sha256:
        raise ControlledRestartBlocked("controlled-restart plan file SHA-256 mismatch")
    plan = _json(plan_path, "controlled-restart plan")
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    verify_plan(plan, plan_sha256=plan_sha256, verify_release_binding=True)
    if instant > _time(plan.get("expires_at")):
        raise ControlledRestartBlocked("controlled-restart plan expired")
    release = plan.get("release_binding")
    before = plan.get("before")
    if not isinstance(release, dict) or not isinstance(before, dict):
        raise ControlledRestartBlocked("controlled-restart plan bindings are missing")
    _release_binding(
        Path(str(release.get("release_root") or "")),
        str(release.get("release_manifest_sha256") or ""),
    )
    after = (backend or HostBackend()).observe(Path(str(release["release_root"])))
    _verify_observation(after)
    before_session = str(before.get("boot_session_uuid") or "")
    before_boot = before.get("boot_time_epoch")
    if (
        not BOOT_UUID_RE.fullmatch(before_session)
        or isinstance(before_boot, bool)
        or not isinstance(before_boot, (int, float))
        or after.boot_session_uuid == before_session
        or after.boot_time_epoch <= float(before_boot)
        or after.boot_time_epoch <= _time(plan.get("prepared_at")).timestamp()
    ):
        raise ControlledRestartBlocked("a later macOS boot session was not proven")
    sentinel = _verify_sentinel(plan.get("sentinel", {}))
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "schema_version": 1,
        "status": "passed",
        **_context(plan),
        "generated_at": instant.isoformat(),
        "plan_file_sha256": plan_sha256,
        "authorization_statement_sha256": plan["authorization_statement_sha256"],
        "release_binding": release,
        "before": before,
        "after": after.public(),
        "sentinel": sentinel,
        "claims": {
            "controlled_cold_restart_verified": True,
            "boot_session_changed": True,
            "boot_time_advanced": True,
            "v2_readiness_restored": True,
            "v2_single_listener_owner_per_port": True,
            "v3_absent_after_restart": True,
            "durable_sentinel_survived": True,
        },
        "safety": {
            "reboot_invoked_by_certifier": False,
            "external_device_required": False,
            "hard_power_removal_required": False,
            "business_state_accessed": False,
            "nas_state_accessed": False,
            "customer_content_accessed": False,
            "read_only_health_and_ownership_probes_only": True,
        },
    }
    report["evidence_sha256"] = _semantic(report)
    verify_report(report, plan=plan, plan_sha256=plan_sha256)
    _write_new(output, report, 0o400)
    return report


def verify_report(
    report: Mapping[str, Any],
    *,
    plan: Mapping[str, Any],
    plan_sha256: str,
    sentinel_path_override: Path | None = None,
) -> None:
    verify_plan(plan, plan_sha256=plan_sha256, verify_release_binding=True)
    unsigned = dict(report)
    supplied = unsigned.pop("evidence_sha256", None)
    claims = report.get("claims")
    safety = report.get("safety")
    before = report.get("before")
    after = report.get("after")
    try:
        before_observation = _observation_from_public(before)
        after_observation = _observation_from_public(after)
        _verify_observation(before_observation)
        _verify_observation(after_observation)
        prepared_at = _time(plan.get("prepared_at"))
        generated_at = _time(report.get("generated_at"))
    except (ControlledRestartBlocked, TypeError, ValueError) as exc:
        raise ControlledRestartBlocked(
            "controlled-restart report observation is invalid"
        ) from exc
    sentinel = report.get("sentinel")
    observed_sentinel = _verify_sentinel(
        plan.get("sentinel", {}), path_override=sentinel_path_override
    )
    if (
        report.get("schema") != REPORT_SCHEMA
        or report.get("schema_version") != 1
        or report.get("status") != "passed"
        or supplied != _semantic(unsigned)
        or report.get("plan_file_sha256") != plan_sha256
        or _context(report) != _context(plan)
        or report.get("authorization_statement_sha256")
        != plan.get("authorization_statement_sha256")
        or report.get("release_binding") != plan.get("release_binding")
        or before != plan.get("before")
        or after_observation.boot_session_uuid == before_observation.boot_session_uuid
        or after_observation.boot_time_epoch <= before_observation.boot_time_epoch
        or after_observation.boot_time_epoch <= prepared_at.timestamp()
        or generated_at < datetime.fromtimestamp(
            after_observation.boot_time_epoch, tz=timezone.utc
        )
        or sentinel != observed_sentinel
        or claims
        != {
            "controlled_cold_restart_verified": True,
            "boot_session_changed": True,
            "boot_time_advanced": True,
            "v2_readiness_restored": True,
            "v2_single_listener_owner_per_port": True,
            "v3_absent_after_restart": True,
            "durable_sentinel_survived": True,
        }
        or safety
        != {
            "reboot_invoked_by_certifier": False,
            "external_device_required": False,
            "hard_power_removal_required": False,
            "business_state_accessed": False,
            "nas_state_accessed": False,
            "customer_content_accessed": False,
            "read_only_health_and_ownership_probes_only": True,
        }
    ):
        raise ControlledRestartBlocked("controlled-restart report is not certifying")


def _observation_from_public(value: Any) -> HostObservation:
    if not isinstance(value, dict):
        raise ControlledRestartBlocked("controlled-restart observation is missing")
    endpoints = value.get("endpoints")
    listeners = value.get("listener_pids")
    labels = value.get("v3_loaded_labels")
    pids = value.get("v3_process_pids")
    if (
        not isinstance(endpoints, list)
        or not isinstance(listeners, dict)
        or not isinstance(labels, list)
        or not isinstance(pids, list)
        or any(type(pid) is not int or pid <= 0 for pid in pids)
        or any(not isinstance(label, str) for label in labels)
    ):
        raise ControlledRestartBlocked("controlled-restart observation is malformed")
    listener_pids: dict[int, tuple[int, ...]] = {}
    for port in REQUIRED_PORTS:
        rows = listeners.get(str(port))
        if (
            not isinstance(rows, list)
            or any(type(pid) is not int or pid <= 0 for pid in rows)
        ):
            raise ControlledRestartBlocked("controlled-restart listeners are malformed")
        listener_pids[port] = tuple(rows)
    return HostObservation(
        boot_session_uuid=str(value.get("boot_session_uuid") or ""),
        boot_time_epoch=float(value.get("boot_time_epoch") or 0),
        endpoints=tuple(dict(row) for row in endpoints if isinstance(row, dict)),
        listener_pids=listener_pids,
        v3_loaded_labels=tuple(labels),
        v3_process_pids=tuple(pids),
    )


def verify_plan(
    plan: Mapping[str, Any],
    *,
    plan_sha256: str,
    verify_release_binding: bool,
) -> None:
    unsigned = dict(plan)
    supplied = unsigned.pop("plan_sha256", None)
    release = plan.get("release_binding")
    before = plan.get("before")
    sentinel = plan.get("sentinel")
    try:
        prepared_at = _time(plan.get("prepared_at"))
        expires_at = _time(plan.get("expires_at"))
        workdir = Path(str(plan.get("workdir") or "")).expanduser()
        sentinel_path = Path(str((sentinel or {}).get("path") or "")).expanduser()
    except (ControlledRestartBlocked, TypeError, ValueError) as exc:
        raise ControlledRestartBlocked(
            "controlled-restart plan paths/timestamps are invalid"
        ) from exc
    if (
        not SHA256_RE.fullmatch(plan_sha256)
        or plan.get("schema") != PLAN_SCHEMA
        or plan.get("schema_version") != 1
        or supplied != _semantic(unsigned)
        or plan.get("reboot_invoked_by_certifier") is not False
        or plan.get("external_device_required") is not False
        or plan.get("hard_power_removal_required") is not False
        or plan.get("required_endpoints") != [list(item) for item in REQUIRED_ENDPOINTS]
        or plan.get("required_v3_absent_labels") != list(V3_LABELS)
        or not isinstance(release, dict)
        or not isinstance(before, dict)
        or not isinstance(sentinel, dict)
        or not re.fullmatch(r"[0-9a-f]{32}", str(plan.get("plan_id") or ""))
        or not SHA256_RE.fullmatch(
            str(plan.get("authorization_statement_sha256") or "")
        )
        or expires_at - prepared_at != timedelta(hours=24)
        or not workdir.is_absolute()
        or workdir.resolve(strict=False) != workdir
        or not sentinel_path.is_absolute()
        or sentinel_path.resolve(strict=False) != sentinel_path
        or sentinel_path.parent != workdir
        or sentinel.get("journal_mode") != "wal"
        or not re.fullmatch(r"[0-9a-f]{32}", str(sentinel.get("restart_id") or ""))
    ):
        raise ControlledRestartBlocked("controlled-restart plan identity is invalid")
    _context(plan)
    before_observation = _observation_from_public(before)
    _verify_observation(before_observation)
    if before_observation.boot_time_epoch > prepared_at.timestamp():
        raise ControlledRestartBlocked("controlled-restart preflight boot time is invalid")
    if verify_release_binding:
        rebound = _release_binding(
            Path(str(release.get("release_root") or "")),
            str(release.get("release_manifest_sha256") or ""),
        )
        if rebound != release:
            raise ControlledRestartBlocked("controlled-restart release binding drifted")


def _add_context(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--gate-config-sha256", required=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--release-root", type=Path, required=True)
    prepare.add_argument("--release-manifest-sha256", required=True)
    prepare.add_argument("--workdir", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--authorization-statement-sha256", required=True)
    _add_context(prepare)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--plan", type=Path, required=True)
    finalize.add_argument("--plan-sha256", required=True)
    finalize.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare_plan(
                release_root=args.release_root,
                release_manifest_sha256=args.release_manifest_sha256,
                output=args.output,
                workdir=args.workdir,
                authorization_statement_sha256=args.authorization_statement_sha256,
                expected_context={
                    "campaign_id": args.campaign_id,
                    "release_sha": args.release_sha,
                    "hardware_id": args.hardware_id,
                    "gate_config_sha256": args.gate_config_sha256,
                },
            )
        else:
            result = finalize_plan(
                plan_path=args.plan,
                plan_sha256=args.plan_sha256,
                output=args.output,
            )
    except (ControlledRestartBlocked, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ControlledRestartBlocked",
    "HostObservation",
    "prepare_plan",
    "finalize_plan",
    "verify_plan",
    "verify_report",
]

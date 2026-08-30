#!/usr/bin/env python3
"""Explicitly authorized physical APFS/device-loss/SIGKILL fault drill.

Importing this module is inert.  The mutation path is restricted to a newly
authorized, empty, external physical APFS volume whose parent disk differs
from the system disk.  The executor never calls ``diskutil unmount/eject``;
the operator must physically remove power/cable and reconnect the same UUID.
"""

from __future__ import annotations

import argparse
import errno
import getpass
import hashlib
import json
import os
import plistlib
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.v3_validation import fault_realism
from scripts.v3_validation.human_approval import AUTHORIZED_LOCAL_OWNERS, hmac_compare


PLAN_SCHEMA = "magi.v3.physical-fault-plan/v2"
AUTH_SCHEMA = "magi.v3.physical-fault-authorization/v2"
REPORT_SCHEMA = "magi.v3.physical-fault-drill/v2"
SHA256_RE = __import__("re").compile(r"^[0-9a-f]{64}$")
AUTHORIZED_ACTIONS = [
    "fill_external_apfs_until_enospc",
    "physically_disconnect_and_remount_external_device_only",
    "sigkill_owned_fault_children_at_random_transaction_stage_markers",
]
POWER_WRITER_PAYLOAD_BYTES = 1024 * 1024
MINIMUM_ACKNOWLEDGED_POWER_COMMITS = 8
MINIMUM_PHYSICAL_VOLUME_BYTES = 64 * 1024 * 1024
MAXIMUM_PHYSICAL_VOLUME_BYTES = 2 * 1024 * 1024 * 1024
ALLOWED_EMPTY_VOLUME_METADATA = frozenset(
    {".DocumentRevisions-V100", ".Spotlight-V100", ".TemporaryItems", ".Trashes", ".fseventsd"}
)


class PhysicalFaultBlocked(RuntimeError):
    pass


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _semantic(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(dict(value))).hexdigest()


def _json(path: Path, description: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PhysicalFaultBlocked(f"{description} is invalid: {exc}") from exc
    if not isinstance(value, dict):
        raise PhysicalFaultBlocked(f"{description} must be an object")
    return value


def _write_new(path: Path, value: Mapping[str, Any], mode: int) -> str:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise PhysicalFaultBlocked("physical fault output path is unsafe")
    raw.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = _canonical(dict(value)) + b"\n"
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


def _context(value: Mapping[str, Any]) -> dict[str, str]:
    fields = ("campaign_id", "release_sha", "hardware_id", "gate_config_sha256")
    result = {field: value.get(field) for field in fields}
    if (
        any(not isinstance(item, str) or not item for item in result.values())
        or not SHA256_RE.fullmatch(str(result["release_sha"]))
        or not SHA256_RE.fullmatch(str(result["gate_config_sha256"]))
    ):
        raise PhysicalFaultBlocked("physical fault context is incomplete")
    return {key: str(item) for key, item in result.items()}


def _time(value: Any) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PhysicalFaultBlocked("physical fault timestamp is invalid") from exc
    if result.tzinfo is None:
        raise PhysicalFaultBlocked("physical fault timestamp lacks timezone")
    return result.astimezone(timezone.utc)


def _selected_device(info: Mapping[str, Any], system: Mapping[str, Any]) -> dict[str, Any]:
    mount = info.get("MountPoint")
    filesystem = str(
        info.get("FilesystemType")
        or info.get("FilesystemName")
        or info.get("FilesystemPersonality")
        or ""
    ).lower()
    device = str(info.get("DeviceIdentifier") or "")
    parent = str(info.get("ParentWholeDisk") or "")
    system_parent = str(system.get("ParentWholeDisk") or system.get("DeviceIdentifier") or "")
    volume_uuid = str(info.get("VolumeUUID") or "")
    total_size = info.get("TotalSize")
    free_space = info.get("FreeSpace")
    physical = str(info.get("VirtualOrPhysical") or "").lower() == "physical"
    external = (
        info.get("Internal") is False
        and str(info.get("DeviceLocation") or "").lower() == "external"
        and physical
    )
    if (
        not isinstance(mount, str)
        or not mount.startswith("/Volumes/")
        or "apfs" not in filesystem
        or not device.startswith("disk")
        or not parent.startswith("disk")
        or not system_parent.startswith("disk")
        or parent == system_parent
        or not volume_uuid
        or not external
        or info.get("DiskImage") is True
        or type(total_size) is not int
        or type(free_space) is not int
        or not MINIMUM_PHYSICAL_VOLUME_BYTES <= total_size <= MAXIMUM_PHYSICAL_VOLUME_BYTES
        or not MINIMUM_PHYSICAL_VOLUME_BYTES <= free_space <= total_size
    ):
        raise PhysicalFaultBlocked(
            "target must be a dedicated 64 MiB-2 GiB external physical non-system APFS "
            "volume, never a disk image"
        )
    return {
        "volume_uuid": volume_uuid,
        "media_uuid": str(info.get("MediaUUID") or ""),
        "device_identifier": device,
        "parent_whole_disk": parent,
        "mount_point": mount,
        "filesystem": "apfs",
        "internal": False,
        "device_location": "External",
        "virtual_or_physical": "Physical",
        "system_parent_whole_disk": system_parent,
        "total_size_bytes": total_size,
        "free_space_bytes_at_plan": free_space,
    }


def _same_device_identity(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    stable_left = dict(left)
    stable_right = dict(right)
    stable_left.pop("free_space_bytes_at_plan", None)
    stable_right.pop("free_space_bytes_at_plan", None)
    return stable_left == stable_right


class DiskInfoReader(Protocol):
    def __call__(self, target: str) -> Mapping[str, Any]: ...


def _disk_info(target: str) -> Mapping[str, Any]:
    result = subprocess.run(
        ("/usr/sbin/diskutil", "info", "-plist", target),
        capture_output=True,
        timeout=15,
        check=False,
    )
    if result.returncode != 0:
        raise PhysicalFaultBlocked(f"diskutil info failed for {target}")
    value = plistlib.loads(result.stdout)
    if not isinstance(value, dict):
        raise PhysicalFaultBlocked("diskutil info did not return a dictionary")
    return value


def _unexpected_volume_entries(mount: Path) -> list[str]:
    return sorted(
        entry.name for entry in mount.iterdir() if entry.name not in ALLOWED_EMPTY_VOLUME_METADATA
    )


def prepare_plan(
    *,
    volume: Path,
    output: Path,
    token_output: Path,
    expected_context: Mapping[str, str],
    disk_info: DiskInfoReader = _disk_info,
) -> dict[str, Any]:
    context = _context(expected_context)
    mount = volume.expanduser().resolve(strict=True)
    if not mount.is_dir() or mount.is_symlink() or not os.path.ismount(mount):
        raise PhysicalFaultBlocked("external APFS target must currently be a mounted volume root")
    if _unexpected_volume_entries(mount):
        raise PhysicalFaultBlocked("physical fault volume must be empty and dedicated")
    if any(
        candidate.expanduser().resolve(strict=False).is_relative_to(mount)
        for candidate in (output, token_output)
    ):
        raise PhysicalFaultBlocked("plan and token must be stored outside the fault volume")
    device = _selected_device(disk_info(str(mount)), disk_info("/"))
    if Path(device["mount_point"]).resolve(strict=True) != mount:
        raise PhysicalFaultBlocked("diskutil mount identity differs from selected volume")
    token = secrets.token_urlsafe(48)
    instant = datetime.now(timezone.utc)
    unsigned = {
        "schema": PLAN_SCHEMA,
        "schema_version": 2,
        "plan_id": uuid.uuid4().hex,
        **context,
        "device": device,
        "owned_workdir": str(mount / f".magi-v3-physical-fault-{uuid.uuid4().hex}"),
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "authorized_actions": AUTHORIZED_ACTIONS,
        "minimum_sigkill_cycles": 64,
        "prepared_at": instant.isoformat(),
        "expires_at": (instant + timedelta(hours=2)).isoformat(),
        "mutation_performed": False,
    }
    plan = {**unsigned, "plan_sha256": _semantic(unsigned)}
    plan_sha = _write_new(output, plan, 0o400)
    _write_new(token_output, {"token": token, "plan_file_sha256": plan_sha}, 0o600)
    return {
        "status": "prepared_not_authorized",
        "plan": str(output),
        "plan_file_sha256": plan_sha,
        "token": str(token_output),
        "device": device,
    }


def authorize_plan(
    *,
    plan_path: Path,
    output: Path,
    input_reader: Callable[[str], str] = input,
    isatty: Callable[[], bool] = lambda: sys.stdin.isatty(),
    uid: int | None = None,
    user: str | None = None,
    tty_name: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    plan = _json(plan_path, "physical fault plan")
    local_uid = os.getuid() if uid is None else uid
    local_user = getpass.getuser() if user is None else user
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if (
        plan.get("schema") != PLAN_SCHEMA
        or plan.get("schema_version") != 2
        or plan.get("authorized_actions") != AUTHORIZED_ACTIONS
        or (local_uid, local_user) not in AUTHORIZED_LOCAL_OWNERS
        or not isatty()
        or instant > _time(plan.get("expires_at"))
    ):
        raise PhysicalFaultBlocked("fresh interactive physical-fault authorization is required")
    if output.expanduser().resolve(strict=False).is_relative_to(
        Path(str(plan["device"]["mount_point"]))
    ):
        raise PhysicalFaultBlocked("authorization must be stored outside the fault volume")
    terminal = tty_name or os.ttyname(0)
    phrase = (
        f"AUTHORIZE PHYSICAL FAULT {plan['plan_id']} "
        f"{plan['device']['volume_uuid']}"
    )
    if not hmac_compare(input_reader(f"Type exactly: {phrase}\n"), phrase):
        raise PhysicalFaultBlocked("physical-fault authorization phrase mismatch")
    receipt = {
        "schema": AUTH_SCHEMA,
        "schema_version": 2,
        "status": "authorized",
        **_context(plan),
        "plan_id": plan["plan_id"],
        "plan_file_sha256": _sha(plan_path),
        "device": plan["device"],
        "authorized_actions": AUTHORIZED_ACTIONS,
        "approver_uid": local_uid,
        "approver_user": local_user,
        "auth_method": "allowlisted_local_owner_interactive_tty",
        "tty_session_sha256": hashlib.sha256(str(terminal).encode()).hexdigest(),
        "authorized_at": instant.isoformat(),
        "expires_at": (instant + timedelta(minutes=30)).isoformat(),
        "human_interaction_performed": True,
    }
    return {
        "status": "authorized",
        "authorization": str(output),
        "authorization_sha256": _write_new(output, receipt, 0o400),
    }


class PhysicalBackend(Protocol):
    def revalidate(self, device: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def enospc(self, workdir: Path) -> Mapping[str, Any]: ...
    def random_transaction_stage_sigkill(self, workdir: Path, cycles: int) -> Mapping[str, Any]: ...
    def external_device_disconnect(self, workdir: Path, device: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def cleanup(self, workdir: Path) -> Mapping[str, Any]: ...


def _emit_child_event(event: Mapping[str, Any]) -> None:
    print(json.dumps(dict(event), sort_keys=True, separators=(",", ":")), flush=True)


def _power_writer_child(database: Path) -> int:
    """Continuously commit FULL/WAL transactions until physical I/O disappears."""

    try:
        capability_fd = int(os.environ["MAGI_PHYSICAL_WRITER_CAPABILITY_FD"])
        expected_capability = os.environ["MAGI_PHYSICAL_WRITER_CAPABILITY_SHA256"]
        capability = os.read(capability_fd, 256)
        os.close(capability_fd)
    except (KeyError, ValueError, OSError) as exc:
        raise PhysicalFaultBlocked("owned power writer lacks its inherited capability") from exc
    if (
        len(capability) < 32
        or not hmac_compare(hashlib.sha256(capability).hexdigest(), expected_capability)
    ):
        raise PhysicalFaultBlocked("owned power writer capability is invalid")
    resolved_database = database.expanduser().resolve(strict=False)
    workdir = resolved_database.parent
    mount = workdir.parent
    if (
        resolved_database.name != "power.sqlite3"
        or not workdir.name.startswith(".magi-v3-physical-fault-")
        or mount.parent != Path("/Volumes")
        or not os.path.ismount(mount)
    ):
        raise PhysicalFaultBlocked("owned power writer is restricted to an external volume workdir")

    payload = b"P" * POWER_WRITER_PAYLOAD_BYTES
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(database, timeout=1)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA wal_autocheckpoint=1")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS jobs("
            "sequence INTEGER PRIMARY KEY, job_id TEXT UNIQUE NOT NULL, payload BLOB NOT NULL)"
        )
        connection.commit()
        _emit_child_event({"event": "writer_ready", "pid": os.getpid()})
        sequence = 0
        while True:
            job_id = f"power-{sequence:012d}"
            _emit_child_event(
                {"event": "transaction_begin", "sequence": sequence, "job_id": job_id}
            )
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO jobs(sequence,job_id,payload) VALUES(?,?,?)",
                (sequence, job_id, payload),
            )
            _emit_child_event(
                {"event": "transaction_write_active", "sequence": sequence, "job_id": job_id}
            )
            connection.commit()
            # synchronous=FULL makes COMMIT_ACK a durable SQLite acknowledgement.
            _emit_child_event(
                {"event": "durable_commit_ack", "sequence": sequence, "job_id": job_id}
            )
            sequence += 1
    except (OSError, sqlite3.Error) as exc:
        _emit_child_event(
            {
                "event": "writer_io_failure",
                "error_type": type(exc).__name__,
                "error_sha256": hashlib.sha256(str(exc).encode()).hexdigest(),
            }
        )
        return 74
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass


@dataclass
class CommandAudit:
    """Allowlisted backend command runner with raw argv/return-code receipts."""

    entries: list[dict[str, Any]]

    def _entry(self, argv: Sequence[str], command_class: str) -> dict[str, Any]:
        raw = [str(value) for value in argv]
        allowed = (
            command_class == "diskutil_info"
            and len(raw) == 4
            and raw[:3] == ["/usr/sbin/diskutil", "info", "-plist"]
        ) or (
            command_class == "owned_power_writer"
            and len(raw) == 4
            and raw[0] == sys.executable
            and Path(raw[1]).resolve() == Path(__file__).resolve()
            and raw[2] == "power-writer-child"
        )
        entry = {
            "sequence": len(self.entries),
            "command_class": command_class,
            "argv": raw,
            "argv_sha256": hashlib.sha256(_canonical(raw)).hexdigest(),
            "allowed_by_backend_policy": allowed,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "returncode": None,
        }
        self.entries.append(entry)
        if not allowed:
            raise PhysicalFaultBlocked("backend command is outside the physical-fault allowlist")
        return entry

    def run_disk_info(self, target: str) -> tuple[int, bytes, bytes]:
        argv = ["/usr/sbin/diskutil", "info", "-plist", target]
        entry = self._entry(argv, "diskutil_info")
        result = subprocess.run(argv, capture_output=True, timeout=15, check=False)
        entry.update(
            {
                "returncode": result.returncode,
                "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
                "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return result.returncode, result.stdout, result.stderr

    def spawn_writer(self, database: Path) -> tuple[subprocess.Popen[str], dict[str, Any]]:
        argv = [sys.executable, str(Path(__file__).resolve()), "power-writer-child", str(database)]
        entry = self._entry(argv, "owned_power_writer")
        read_fd, write_fd = os.pipe()
        capability = secrets.token_bytes(48)
        environment = os.environ.copy()
        environment["MAGI_PHYSICAL_WRITER_CAPABILITY_FD"] = str(read_fd)
        environment["MAGI_PHYSICAL_WRITER_CAPABILITY_SHA256"] = hashlib.sha256(
            capability
        ).hexdigest()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                pass_fds=(read_fd,),
                env=environment,
            )
            os.close(read_fd)
            read_fd = -1
            os.write(write_fd, capability)
        finally:
            if read_fd >= 0:
                os.close(read_fd)
            os.close(write_fd)
        entry["owned_pid"] = process.pid
        return process, entry


def _command_audit_metrics(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = [dict(row) for row in entries]
    forbidden = 0
    disk_info = 0
    writers = 0
    for index, row in enumerate(rows):
        argv = row.get("argv")
        command_class = row.get("command_class")
        if (
            not isinstance(argv, list)
            or row.get("sequence") != index
            or row.get("allowed_by_backend_policy") is not True
            or row.get("argv_sha256") != hashlib.sha256(_canonical(argv)).hexdigest()
            or not isinstance(row.get("started_at"), str)
            or not isinstance(row.get("completed_at"), str)
            or type(row.get("returncode")) is not int
        ):
            forbidden += 1
            continue
        lowered = [str(value).lower() for value in argv]
        if any(value in {"unmount", "unmountdisk", "eject"} for value in lowered):
            forbidden += 1
        if command_class == "diskutil_info":
            disk_info += 1
            if len(argv) != 4 or argv[:3] != ["/usr/sbin/diskutil", "info", "-plist"]:
                forbidden += 1
        elif command_class == "owned_power_writer":
            writers += 1
            if (
                len(argv) != 4
                or argv[2] != "power-writer-child"
                or type(row.get("owned_pid")) is not int
                or row["owned_pid"] <= 1
                or row.get("returncode") == 0
            ):
                forbidden += 1
        else:
            forbidden += 1
    return {
        "raw_commands": rows,
        "raw_commands_sha256": hashlib.sha256(_canonical(rows)).hexdigest(),
        "forbidden_command_count": forbidden,
        "diskutil_info_command_count": disk_info,
        "owned_power_writer_command_count": writers,
        "diskutil_unmount_or_eject_invoked": forbidden > 0,
    }


class HostPhysicalBackend:
    """Real external-device backend.  It is reachable only after all guards."""

    def __init__(self, control_root: Path) -> None:
        self.control_root = control_root
        self.audit = CommandAudit([])

    def _info(self, target: str) -> Mapping[str, Any]:
        returncode, stdout, _stderr = self.audit.run_disk_info(target)
        if returncode != 0:
            raise PhysicalFaultBlocked(f"diskutil info failed for {target}")
        value = plistlib.loads(stdout)
        if not isinstance(value, dict):
            raise PhysicalFaultBlocked("diskutil info did not return a dictionary")
        return value

    def revalidate(self, device: Mapping[str, Any]) -> Mapping[str, Any]:
        observed = _selected_device(self._info(str(device["mount_point"])), self._info("/"))
        if not _same_device_identity(observed, device):
            raise PhysicalFaultBlocked("external device identity changed after authorization")
        root = Path(str(observed["mount_point"]))
        return {
            "ok": True,
            "device": dict(device),
            "observed_total_size_bytes": observed["total_size_bytes"],
            "observed_free_space_bytes": observed["free_space_bytes_at_plan"],
            "mount_is_mounted": os.path.ismount(root),
            "mount_root_empty_before_workdir": not _unexpected_volume_entries(root),
        }

    def enospc(self, workdir: Path) -> Mapping[str, Any]:
        database = workdir / "enospc.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, payload BLOB)")
            connection.execute("INSERT INTO jobs VALUES('baseline',X'01')")
        filler = workdir / "enospc.filler"
        observed = False
        written = 0
        descriptor = os.open(filler, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            chunk = b"\0" * (8 * 1024 * 1024)
            while True:
                try:
                    written += os.write(descriptor, chunk)
                except OSError as exc:
                    if exc.errno != errno.ENOSPC:
                        raise
                    observed = True
                    break
            try:
                os.fsync(descriptor)
            except OSError as exc:
                if exc.errno != errno.ENOSPC:
                    raise
                observed = True
            sqlite_full = False
            sqlite_error = None
            try:
                with sqlite3.connect(database) as connection:
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("PRAGMA synchronous=FULL")
                    connection.execute(
                        "INSERT INTO jobs VALUES('must_not_partial',zeroblob(8388608))"
                    )
            except sqlite3.OperationalError as exc:
                sqlite_error = getattr(exc, "sqlite_errorcode", None)
                sqlite_full = sqlite_error == sqlite3.SQLITE_FULL or "full" in str(exc).lower()
        finally:
            os.close(descriptor)
            filler.unlink(missing_ok=True)
        with sqlite3.connect(database) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            partial = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE id='must_not_partial'"
            ).fetchone()[0]
            baseline = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE id='baseline'"
            ).fetchone()[0]
        return {
            "status": "passed" if observed and sqlite_full and integrity == "ok" and partial == 0 else "failed",
            "filesystem_enospc_observed": observed,
            "sqlite_full_observed": sqlite_full,
            "sqlite_error_code": sqlite_error,
            "filler_bytes_before_enospc": written,
            "baseline_rows": baseline,
            "partial_rows": partial,
            "integrity_check": integrity,
            "filler_removed": not filler.exists(),
        }

    def random_transaction_stage_sigkill(self, workdir: Path, cycles: int) -> Mapping[str, Any]:
        database = workdir / "sigkill.sqlite3"
        fault_realism._initialize(database)
        rows = []
        for index in range(cycles):
            stage = secrets.choice(tuple(fault_realism.TRANSACTION_STAGE_MARKERS))
            row = fault_realism._run_instruction_boundary_cycle(
                database,
                target_stage=stage,
                job_id=f"physical-random-{index:03d}",
            )
            rows.append({**row, "random_target_stage": stage})
        distinct = len({row["random_target_stage"] for row in rows})
        passed = all(
            row.get("signal") == "SIGKILL"
            and row.get("integrity_check") == "ok"
            and row.get("final_job_rows") == 1
            and row.get("final_payload_rows") == fault_realism.PAYLOAD_ROWS_PER_JOB
            for row in rows
        )
        return {
            "status": "passed" if passed and distinct >= 16 else "failed",
            "cycles": cycles,
            "offset_source": "secrets.choice(transaction_stage_markers)",
            "distinct_transaction_stages": distinct,
            "acknowledged_commits_lost": 0,
            "partially_visible_transactions": 0,
            "duplicate_jobs": 0,
            "lost_jobs_after_recovery": 0,
            "cycle_receipt_sha256": hashlib.sha256(_canonical(rows)).hexdigest(),
        }

    def external_device_disconnect(self, workdir: Path, device: Mapping[str, Any]) -> Mapping[str, Any]:
        database = workdir / "power.sqlite3"
        self.control_root.mkdir(mode=0o700)
        ledger = self.control_root / "durable-parent-ack-ledger.jsonl"
        ledger_descriptor = os.open(
            ledger,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        events: list[dict[str, Any]] = []
        events_lock = threading.Lock()
        # The audit ledger is intentionally created before the child, so a
        # successful drill always has a parent-side durable acknowledgement
        # trail.  If the child cannot be spawned, close that descriptor here:
        # otherwise a failed preflight could leak an FD and later look like a
        # live writer to the operator.  No signal is sent on this path.
        try:
            process, process_audit = self.audit.spawn_writer(database)
        except BaseException:
            os.close(ledger_descriptor)
            raise
        reader_error: list[str] = []

        def collect() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        reader_error.append("invalid_child_event")
                        continue
                    if not isinstance(event, dict):
                        reader_error.append("non_object_child_event")
                        continue
                    record = {
                        **event,
                        "parent_observed_at": datetime.now(timezone.utc).isoformat(),
                    }
                    encoded = _canonical(record) + b"\n"
                    os.write(ledger_descriptor, encoded)
                    if event.get("event") == "durable_commit_ack":
                        os.fsync(ledger_descriptor)
                    with events_lock:
                        events.append(record)
            except OSError as exc:
                reader_error.append(type(exc).__name__)

        reader = threading.Thread(target=collect, name="magi-physical-ack-reader", daemon=True)
        reader.start()
        forced_cleanup = False
        try:
            deadline = time.monotonic() + 60
            ready = False
            acknowledgements: list[dict[str, Any]] = []
            markers: set[str] = set()
            while time.monotonic() < deadline:
                with events_lock:
                    snapshot = list(events)
                ready = any(row.get("event") == "writer_ready" for row in snapshot)
                acknowledgements = [
                    row for row in snapshot if row.get("event") == "durable_commit_ack"
                ]
                markers = {str(row.get("event") or "") for row in snapshot}
                if (
                    ready
                    and len(acknowledgements) >= MINIMUM_ACKNOWLEDGED_POWER_COMMITS
                    and {"transaction_begin", "transaction_write_active", "durable_commit_ack"}
                    <= markers
                ):
                    break
                if process.poll() is not None:
                    raise PhysicalFaultBlocked("owned power writer stopped before disconnect window")
                time.sleep(0.05)
            else:
                raise PhysicalFaultBlocked("owned power writer never reached the active commit window")

            writer_pid = process.pid
            writer_active_at_prompt = process.poll() is None
            if not writer_active_at_prompt:
                raise PhysicalFaultBlocked("owned writer is not active at physical-disconnect prompt")
            input(
                "The owned child is continuously committing FULL/WAL transactions. "
                "Physically remove the authorized external device cable/power now; "
                "never use Finder or diskutil unmount/eject. Press RETURN after removal: "
            )
            disappeared_at = ""
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                returncode, _stdout, _stderr = self.audit.run_disk_info(
                    str(device["volume_uuid"])
                )
                if returncode != 0 and not os.path.ismount(str(device["mount_point"])):
                    disappeared_at = datetime.now(timezone.utc).isoformat()
                    break
                time.sleep(0.5)
            if not disappeared_at:
                raise PhysicalFaultBlocked(
                    "external device did not physically disappear within 300 seconds"
                )

            try:
                writer_returncode = process.wait(timeout=60)
            except subprocess.TimeoutExpired as exc:
                raise PhysicalFaultBlocked(
                    "owned writer did not surface an I/O failure after physical disappearance"
                ) from exc
            process_audit.update(
                {
                    "returncode": writer_returncode,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            reader.join(timeout=10)
            if reader.is_alive():
                raise PhysicalFaultBlocked("owned writer event stream did not close")
            with events_lock:
                final_events = list(events)
            child_io_failure = any(
                row.get("event") == "writer_io_failure" for row in final_events
            )
            transaction_events = [
                str(row.get("event"))
                for row in final_events
                if row.get("event")
                in {"transaction_begin", "transaction_write_active", "durable_commit_ack"}
            ]
            last_transaction_stage = transaction_events[-1] if transaction_events else ""
            if writer_returncode == 0 or not child_io_failure:
                raise PhysicalFaultBlocked(
                    "physical disappearance did not cause the owned writer I/O failure"
                )

            input("Reconnect the same authorized external device. Press RETURN after reconnect: ")
            remounted = None
            remounted_free_space = None
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                try:
                    candidate = _selected_device(
                        self._info(str(device["volume_uuid"])), self._info("/")
                    )
                except (PhysicalFaultBlocked, plistlib.InvalidFileException):
                    time.sleep(0.5)
                    continue
                if _same_device_identity(candidate, device) and os.path.ismount(
                    candidate["mount_point"]
                ):
                    remounted = dict(device)
                    remounted_free_space = candidate["free_space_bytes_at_plan"]
                    break
                time.sleep(0.5)
            if remounted is None:
                raise PhysicalFaultBlocked(
                    "same external APFS UUID did not remount within 300 seconds"
                )

            os.fsync(ledger_descriptor)
            os.close(ledger_descriptor)
            ledger_descriptor = -1
            acknowledged_ids = {
                str(row.get("job_id"))
                for row in final_events
                if row.get("event") == "durable_commit_ack"
                and isinstance(row.get("job_id"), str)
            }
            with sqlite3.connect(database) as connection:
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                database_ids = {
                    str(row[0]) for row in connection.execute("SELECT job_id FROM jobs")
                }
                partial = connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE length(payload) != ?",
                    (POWER_WRITER_PAYLOAD_BYTES,),
                ).fetchone()[0]
                total = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                distinct = connection.execute(
                    "SELECT COUNT(DISTINCT job_id) FROM jobs"
                ).fetchone()[0]
            lost = len(acknowledged_ids - database_ids)
            duplicate = total - distinct
            command_audit = _command_audit_metrics(self.audit.entries)
            ledger_sha = _sha(ledger)
            passed = (
                integrity == "ok"
                and len(acknowledged_ids) >= MINIMUM_ACKNOWLEDGED_POWER_COMMITS
                and lost == 0
                and partial == 0
                and duplicate == 0
                and child_io_failure
                and writer_returncode != 0
                and last_transaction_stage
                in {"transaction_begin", "transaction_write_active"}
                and not reader_error
                and command_audit["forbidden_command_count"] == 0
                and command_audit["owned_power_writer_command_count"] == 1
            )
            return {
                "status": "passed" if passed else "failed",
                "physical_disappearance_observed": True,
                "device_node_absent_observed": True,
                "mount_absent_observed": True,
                "disappeared_at": disappeared_at,
                "same_uuid_remounted": True,
                "remounted_device": remounted,
                "remounted_free_space_bytes": remounted_free_space,
                "writer_pid": writer_pid,
                "writer_active_at_disconnect_prompt": writer_active_at_prompt,
                "transaction_window_markers_observed": sorted(markers),
                "last_transaction_stage_before_disappearance": last_transaction_stage,
                "child_io_failure_after_disappearance": child_io_failure,
                "writer_returncode": writer_returncode,
                "parent_or_child_sigkill_used_for_power_loss": False,
                "forced_process_cleanup": forced_cleanup,
                "acknowledged_commit_count": len(acknowledged_ids),
                "durable_parent_ack_ledger_sha256": ledger_sha,
                "durable_parent_ack_ids_sha256": hashlib.sha256(
                    _canonical(sorted(acknowledged_ids))
                ).hexdigest(),
                "command_audit": command_audit,
                "acknowledged_commits_lost": lost,
                "partially_visible_transactions": partial,
                "duplicate_jobs": duplicate,
                "integrity_check": integrity,
            }
        finally:
            if ledger_descriptor >= 0:
                os.fsync(ledger_descriptor)
                os.close(ledger_descriptor)
            if process.poll() is None:
                forced_cleanup = True
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    # A passing receipt is impossible on this path. Avoid SIGKILL so it
                    # can never be mistaken for the physical-loss stimulus.
                    raise PhysicalFaultBlocked("owned writer could not be safely terminated")

    def cleanup(self, workdir: Path) -> Mapping[str, Any]:
        shutil.rmtree(workdir)
        shutil.rmtree(self.control_root, ignore_errors=True)
        return {
            "ok": not workdir.exists() and not self.control_root.exists(),
            "owned_workdir_removed": not workdir.exists(),
            "owned_control_root_removed": not self.control_root.exists(),
        }


def _consume_token(path: Path, expected_plan_sha: str, expected_token_sha: str) -> None:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise PhysicalFaultBlocked("physical fault token path is unsafe")
    metadata = raw.lstat()
    if stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
        raise PhysicalFaultBlocked("physical fault token must be owner-only 0600")
    value = _json(raw, "physical fault token")
    token = value.get("token")
    if (
        value.get("plan_file_sha256") != expected_plan_sha
        or not isinstance(token, str)
        or hashlib.sha256(token.encode()).hexdigest() != expected_token_sha
    ):
        raise PhysicalFaultBlocked("physical fault token binding mismatch")
    raw.unlink()
    directory = os.open(raw.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def execute_plan(
    *,
    plan_path: Path,
    token_path: Path,
    authorization_path: Path,
    output: Path,
    backend: PhysicalBackend | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    plan = _json(plan_path, "physical fault plan")
    authorization = _json(authorization_path, "physical fault authorization")
    unsigned = dict(plan)
    supplied = unsigned.pop("plan_sha256", None)
    instant = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if (
        plan.get("schema") != PLAN_SCHEMA
        or plan.get("schema_version") != 2
        or supplied != _semantic(unsigned)
        or plan.get("authorized_actions") != AUTHORIZED_ACTIONS
        or authorization.get("schema") != AUTH_SCHEMA
        or authorization.get("schema_version") != 2
        or authorization.get("status") != "authorized"
        or authorization.get("plan_id") != plan.get("plan_id")
        or authorization.get("plan_file_sha256") != _sha(plan_path)
        or authorization.get("device") != plan.get("device")
        or authorization.get("authorized_actions") != AUTHORIZED_ACTIONS
        or authorization.get("human_interaction_performed") is not True
        or (authorization.get("approver_uid"), authorization.get("approver_user"))
        not in AUTHORIZED_LOCAL_OWNERS
        or instant > _time(plan.get("expires_at"))
        or instant > _time(authorization.get("expires_at"))
    ):
        raise PhysicalFaultBlocked("physical fault plan/authorization is invalid or expired")
    _consume_token(token_path, _sha(plan_path), str(plan["token_sha256"]))
    raw_output = output.expanduser()
    if (
        not raw_output.is_absolute()
        or raw_output.resolve(strict=False) != raw_output
        or raw_output.is_symlink()
    ):
        raise PhysicalFaultBlocked("physical fault report output path is unsafe")
    if raw_output.is_relative_to(Path(str(plan["device"]["mount_point"]))):
        raise PhysicalFaultBlocked("physical fault report must be stored outside the fault volume")
    raw_output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    control_root = raw_output.parent / f".magi-v3-physical-control-{plan['plan_id']}"
    if control_root.exists() or control_root.is_symlink():
        raise PhysicalFaultBlocked("physical fault control root must be new")
    active = backend or HostPhysicalBackend(control_root)
    workdir = Path(str(plan["owned_workdir"]))
    if workdir.exists() or workdir.is_symlink() or workdir.parent != Path(plan["device"]["mount_point"]):
        raise PhysicalFaultBlocked("physical fault workdir must be a new direct child of the authorized mount")
    preflight = dict(active.revalidate(plan["device"]))
    if (
        preflight.get("ok") is not True
        or preflight.get("device") != plan["device"]
        or preflight.get("mount_is_mounted") is not True
        or preflight.get("mount_root_empty_before_workdir") is not True
    ):
        raise PhysicalFaultBlocked("physical external device revalidation failed")
    workdir.mkdir(mode=0o700)
    started = instant
    cleanup: Mapping[str, Any] = {"ok": False}
    try:
        enospc = dict(active.enospc(workdir))
        sigkill = dict(
            active.random_transaction_stage_sigkill(
                workdir, int(plan["minimum_sigkill_cycles"])
            )
        )
        disconnect = dict(active.external_device_disconnect(workdir, plan["device"]))
    finally:
        cleanup = active.cleanup(workdir)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "schema_version": 2,
        "status": "passed",
        **_context(plan),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started.isoformat(),
        "plan_file_sha256": _sha(plan_path),
        "authorization_sha256": _sha(authorization_path),
        "device": plan["device"],
        "preflight": preflight,
        "measurements": {
            "physical_apfs_enospc": enospc,
            "external_device_power_disconnect": disconnect,
            "random_transaction_stage_sigkill": sigkill,
        },
        "claims": {
            "physical_apfs_enospc_certified": enospc.get("status") == "passed",
            "external_device_power_disconnect_certified": disconnect.get("status")
            == "passed",
            "whole_machine_power_cut_certified": False,
            "random_transaction_stage_sigkill_certified": sigkill.get("status")
            == "passed",
            "arbitrary_machine_instruction_sigkill_certified": False,
        },
        "reconciliation": {
            "acknowledged_commits_lost": disconnect.get("acknowledged_commits_lost", 1)
            + sigkill.get("acknowledged_commits_lost", 1),
            "partially_visible_transactions": disconnect.get("partially_visible_transactions", 1)
            + sigkill.get("partially_visible_transactions", 1),
            "duplicate_jobs": disconnect.get("duplicate_jobs", 1)
            + sigkill.get("duplicate_jobs", 1),
            "lost_jobs_after_recovery": sigkill.get("lost_jobs_after_recovery", 1),
        },
        "safety": {
            "external_physical_non_system_apfs_only": True,
            "disk_image_or_sparse_image_used": False,
            "diskutil_unmount_or_eject_invoked": disconnect.get("command_audit", {}).get(
                "diskutil_unmount_or_eject_invoked"
            ),
            "live_magi_state_accessed": False,
            "system_disk_touched": False,
            "signals_sent_only_to_owned_children": True,
            "owned_workdir_removed": cleanup.get("owned_workdir_removed") is True,
        },
    }
    report["evidence_sha256"] = _semantic(report)
    verify_report(report)
    _write_new(raw_output, report, 0o400)
    return report


def verify_report(report: Mapping[str, Any], expected_context: Mapping[str, str] | None = None) -> None:
    unsigned = dict(report)
    supplied = unsigned.pop("evidence_sha256", None)
    measurements = report.get("measurements")
    reconciliation = report.get("reconciliation")
    claims = report.get("claims")
    safety = report.get("safety")
    device = report.get("device")
    preflight = report.get("preflight")
    enospc = measurements.get("physical_apfs_enospc") if isinstance(measurements, dict) else None
    disconnect = measurements.get("external_device_power_disconnect") if isinstance(measurements, dict) else None
    sigkill = measurements.get("random_transaction_stage_sigkill") if isinstance(measurements, dict) else None
    command_audit = disconnect.get("command_audit") if isinstance(disconnect, dict) else None
    raw_commands = command_audit.get("raw_commands") if isinstance(command_audit, dict) else None
    derived_command_audit = (
        _command_audit_metrics(raw_commands) if isinstance(raw_commands, list) else None
    )
    if (
        report.get("schema") != REPORT_SCHEMA
        or report.get("schema_version") != 2
        or report.get("status") != "passed"
        or supplied != _semantic(unsigned)
        or (expected_context is not None and _context(report) != _context(expected_context))
        or not isinstance(device, dict)
        or device.get("internal") is not False
        or device.get("device_location") != "External"
        or device.get("virtual_or_physical") != "Physical"
        or device.get("filesystem") != "apfs"
        or not str(device.get("device_identifier") or "").startswith("disk")
        or not str(device.get("parent_whole_disk") or "").startswith("disk")
        or not str(device.get("system_parent_whole_disk") or "").startswith("disk")
        or not str(device.get("mount_point") or "").startswith("/Volumes/")
        or not str(device.get("volume_uuid") or "")
        or device.get("parent_whole_disk") == device.get("system_parent_whole_disk")
        or type(device.get("total_size_bytes")) is not int
        or type(device.get("free_space_bytes_at_plan")) is not int
        or not MINIMUM_PHYSICAL_VOLUME_BYTES
        <= device["total_size_bytes"]
        <= MAXIMUM_PHYSICAL_VOLUME_BYTES
        or not MINIMUM_PHYSICAL_VOLUME_BYTES
        <= device["free_space_bytes_at_plan"]
        <= device["total_size_bytes"]
        or not isinstance(preflight, dict)
        or preflight.get("ok") is not True
        or preflight.get("device") != device
        or preflight.get("observed_total_size_bytes") != device["total_size_bytes"]
        or type(preflight.get("observed_free_space_bytes")) is not int
        or not MINIMUM_PHYSICAL_VOLUME_BYTES
        <= preflight["observed_free_space_bytes"]
        <= device["total_size_bytes"]
        or preflight.get("mount_is_mounted") is not True
        or preflight.get("mount_root_empty_before_workdir") is not True
        or not isinstance(enospc, dict)
        or enospc.get("status") != "passed"
        or enospc.get("filesystem_enospc_observed") is not True
        or enospc.get("sqlite_full_observed") is not True
        or enospc.get("baseline_rows") != 1
        or enospc.get("partial_rows") != 0
        or enospc.get("integrity_check") != "ok"
        or enospc.get("filler_removed") is not True
        or not isinstance(disconnect, dict)
        or disconnect.get("status") != "passed"
        or disconnect.get("physical_disappearance_observed") is not True
        or disconnect.get("device_node_absent_observed") is not True
        or disconnect.get("mount_absent_observed") is not True
        or disconnect.get("same_uuid_remounted") is not True
        or disconnect.get("remounted_device") != device
        or type(disconnect.get("remounted_free_space_bytes")) is not int
        or not 0 <= disconnect["remounted_free_space_bytes"] <= device["total_size_bytes"]
        or type(disconnect.get("writer_pid")) is not int
        or disconnect["writer_pid"] <= 1
        or disconnect.get("writer_active_at_disconnect_prompt") is not True
        or not isinstance(disconnect.get("transaction_window_markers_observed"), list)
        or not {
            "transaction_begin", "transaction_write_active", "durable_commit_ack"
        }.issubset(set(disconnect["transaction_window_markers_observed"]))
        or disconnect.get("last_transaction_stage_before_disappearance")
        not in {"transaction_begin", "transaction_write_active"}
        or disconnect.get("child_io_failure_after_disappearance") is not True
        or type(disconnect.get("writer_returncode")) is not int
        or disconnect["writer_returncode"] == 0
        or disconnect.get("parent_or_child_sigkill_used_for_power_loss") is not False
        or disconnect.get("forced_process_cleanup") is not False
        or type(disconnect.get("acknowledged_commit_count")) is not int
        or disconnect["acknowledged_commit_count"] < MINIMUM_ACKNOWLEDGED_POWER_COMMITS
        or not SHA256_RE.fullmatch(
            str(disconnect.get("durable_parent_ack_ledger_sha256") or "")
        )
        or not SHA256_RE.fullmatch(
            str(disconnect.get("durable_parent_ack_ids_sha256") or "")
        )
        or not isinstance(command_audit, dict)
        or derived_command_audit != command_audit
        or command_audit.get("forbidden_command_count") != 0
        or command_audit.get("owned_power_writer_command_count") != 1
        or type(command_audit.get("diskutil_info_command_count")) is not int
        or command_audit["diskutil_info_command_count"] < 1
        or command_audit.get("diskutil_unmount_or_eject_invoked") is not False
        or disconnect.get("integrity_check") != "ok"
        or not isinstance(sigkill, dict)
        or sigkill.get("status") != "passed"
        or type(sigkill.get("cycles")) is not int
        or sigkill["cycles"] < 64
        or sigkill.get("offset_source") != "secrets.choice(transaction_stage_markers)"
        or type(sigkill.get("distinct_transaction_stages")) is not int
        or sigkill["distinct_transaction_stages"] < 16
        or not SHA256_RE.fullmatch(str(sigkill.get("cycle_receipt_sha256") or ""))
        or not isinstance(claims, dict)
        or claims
        != {
            "physical_apfs_enospc_certified": True,
            "external_device_power_disconnect_certified": True,
            "whole_machine_power_cut_certified": False,
            "random_transaction_stage_sigkill_certified": True,
            "arbitrary_machine_instruction_sigkill_certified": False,
        }
        or not isinstance(reconciliation, dict)
        or any(reconciliation.get(field) != 0 for field in (
            "acknowledged_commits_lost", "partially_visible_transactions",
            "duplicate_jobs", "lost_jobs_after_recovery"
        ))
        or not isinstance(safety, dict)
        or safety.get("external_physical_non_system_apfs_only") is not True
        or safety.get("disk_image_or_sparse_image_used") is not False
        or safety.get("diskutil_unmount_or_eject_invoked") is not False
        or safety.get("live_magi_state_accessed") is not False
        or safety.get("system_disk_touched") is not False
        or safety.get("signals_sent_only_to_owned_children") is not True
        or safety.get("owned_workdir_removed") is not True
    ):
        raise PhysicalFaultBlocked("physical fault report does not prove the required real-device drill")


def verify_artifact_chain(
    *,
    report: Mapping[str, Any],
    report_sha256: str,
    plan: Mapping[str, Any],
    plan_sha256: str,
    authorization: Mapping[str, Any],
    authorization_sha256: str,
    expected_context: Mapping[str, str],
) -> None:
    verify_report(report, expected_context)
    unsigned = dict(plan)
    supplied = unsigned.pop("plan_sha256", None)
    workdir = Path(str(plan.get("owned_workdir") or ""))
    mount = Path(str(plan.get("device", {}).get("mount_point") or ""))
    disconnect = report.get("measurements", {}).get(
        "external_device_power_disconnect", {}
    )
    raw_commands = disconnect.get("command_audit", {}).get("raw_commands", [])
    writer_commands = [
        row
        for row in raw_commands
        if isinstance(row, dict) and row.get("command_class") == "owned_power_writer"
    ]
    if (
        report_sha256 != hashlib.sha256(_canonical(dict(report)) + b"\n").hexdigest()
        or plan_sha256 != hashlib.sha256(_canonical(dict(plan)) + b"\n").hexdigest()
        or authorization_sha256
        != hashlib.sha256(_canonical(dict(authorization)) + b"\n").hexdigest()
        or plan.get("schema") != PLAN_SCHEMA
        or plan.get("schema_version") != 2
        or supplied != _semantic(unsigned)
        or _context(plan) != _context(expected_context)
        or plan.get("authorized_actions") != AUTHORIZED_ACTIONS
        or plan.get("mutation_performed") is not False
        or type(plan.get("minimum_sigkill_cycles")) is not int
        or plan["minimum_sigkill_cycles"] < 64
        or not SHA256_RE.fullmatch(str(plan.get("token_sha256") or ""))
        or not isinstance(plan.get("plan_id"), str)
        or not plan["plan_id"]
        or not workdir.is_absolute()
        or workdir.parent != mount
        or not workdir.name.startswith(".magi-v3-physical-fault-")
        or len(writer_commands) != 1
        or Path(str(writer_commands[0].get("argv", ["", "", "", ""])[3])).parent
        != workdir
        or authorization.get("schema") != AUTH_SCHEMA
        or authorization.get("schema_version") != 2
        or authorization.get("status") != "authorized"
        or authorization.get("auth_method")
        != "allowlisted_local_owner_interactive_tty"
        or _context(authorization) != _context(expected_context)
        or authorization.get("plan_id") != plan.get("plan_id")
        or authorization.get("plan_file_sha256") != plan_sha256
        or authorization.get("device") != plan.get("device")
        or authorization.get("authorized_actions") != AUTHORIZED_ACTIONS
        or (authorization.get("approver_uid"), authorization.get("approver_user"))
        not in AUTHORIZED_LOCAL_OWNERS
        or authorization.get("human_interaction_performed") is not True
        or not SHA256_RE.fullmatch(
            str(authorization.get("tty_session_sha256") or "")
        )
        or report.get("plan_file_sha256") != plan_sha256
        or report.get("authorization_sha256") != authorization_sha256
        or report.get("device") != plan.get("device")
        or _time(plan.get("prepared_at")) > _time(authorization.get("authorized_at"))
        or _time(authorization.get("authorized_at")) > _time(plan.get("expires_at"))
        or _time(authorization.get("authorized_at")) > _time(report.get("started_at"))
        or _time(report.get("started_at")) > _time(authorization.get("expires_at"))
        or _time(report.get("started_at")) > _time(plan.get("expires_at"))
        or _time(report.get("started_at")) > _time(report.get("generated_at"))
    ):
        raise PhysicalFaultBlocked("physical fault report plan/authorization chain is invalid")


def _add_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--campaign-id", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--hardware-id", required=True)
    parser.add_argument("--gate-config-sha256", required=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare-plan")
    prepare.add_argument("--volume", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--token-output", type=Path, required=True)
    _add_context_arguments(prepare)
    authorize = commands.add_parser("authorize")
    authorize.add_argument("--plan", type=Path, required=True)
    authorize.add_argument("--output", type=Path, required=True)
    execute = commands.add_parser("execute")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--token", type=Path, required=True)
    execute.add_argument("--authorization", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--confirm-volume-uuid", required=True)
    execute.add_argument(
        "--execute-physical-fault-drill",
        action="store_true",
        required=True,
        help="required explicit mutation acknowledgement",
    )
    child = commands.add_parser("power-writer-child", help=argparse.SUPPRESS)
    child.add_argument("database", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "power-writer-child":
            return _power_writer_child(args.database)
        if args.command == "prepare-plan":
            result = prepare_plan(
                volume=args.volume,
                output=args.output,
                token_output=args.token_output,
                expected_context={
                    "campaign_id": args.campaign_id,
                    "release_sha": args.release_sha,
                    "hardware_id": args.hardware_id,
                    "gate_config_sha256": args.gate_config_sha256,
                },
            )
        elif args.command == "authorize":
            result = authorize_plan(plan_path=args.plan, output=args.output)
        else:
            plan = _json(args.plan, "physical fault plan")
            if args.confirm_volume_uuid != plan.get("device", {}).get("volume_uuid"):
                raise PhysicalFaultBlocked("explicit volume UUID confirmation mismatch")
            result = execute_plan(
                plan_path=args.plan,
                token_path=args.token,
                authorization_path=args.authorization,
                output=args.output,
            )
    except (PhysicalFaultBlocked, OSError, sqlite3.Error, ValueError) as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True))
        return 2
    print(json.dumps({"ok": True, "result": result}, sort_keys=True))
    return 0


__all__ = [
    "PhysicalFaultBlocked",
    "prepare_plan",
    "authorize_plan",
    "execute_plan",
    "verify_report",
    "verify_artifact_chain",
]


if __name__ == "__main__":
    raise SystemExit(main())

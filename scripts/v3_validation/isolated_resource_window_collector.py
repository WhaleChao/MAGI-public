#!/usr/bin/env python3
"""Code-owned collector for the V2-stopped G8/G9/G25 resource window.

The collector never stops V2.  It starts only after a read-only preflight has
proved that V2, production port owners, and unrelated local model workloads
are absent.  It then executes immutable, hash-bound commands sequentially,
observes the full 1800-second deep-idle interval, samples libproc/AGX/vm_stat,
and emits the raw report consumed by :mod:`isolated_resource_window`.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from magi_v3.external_inputs import (
    NAMED_MUTABLE_STATE_BINDINGS,
    named_mutable_state_paths,
)
from magi_v3.macos_resources import parse_memory_pressure, parse_vm_stat
from scripts.v3_validation.isolated_resource_window import (
    AGX_SOURCE,
    ATTRIBUTION_METHOD,
    PER_PROCESS_GPU_SOURCE,
    RAW_SOURCE_COMMANDS,
    SCHEMA as REPORT_SCHEMA,
    parse_powermetrics_process_gpu,
    sha256_json,
    verify_report,
)


PLAN_SCHEMA = "magi.v3.isolated-resource-window-plan/v1"
MODEL_RESULT_PREFIX = "MAGI_V3_MODEL_TPS="
LIVE_V2_MARKER = "Library/Application Support/MAGI/runtime/MAGI_v2"
MODEL_WORKLOAD_RE = re.compile(r"(?:omlx\s+serve|mlx_lm|whisper|llama)", re.I)
SHELL_NAMES = {"sh", "bash", "zsh", "fish", "dash"}
REQUIRED_STOPPED_LABELS = (
    "com.magi.daemon",
    "com.magi.omlx",
    "com.magi.omlx-embed",
    "com.magi.omlx-phi4",
    "com.magi.omlx-smol",
    "com.magi.mlx-mtp",
    "com.magi.omlx-nemotron-parse",
)
REQUIRED_MODEL_OWNER_PATTERNS = (
    "omlx serve",
    "mlx_lm.server",
    "mlx_vlm.server",
    "whisper",
    "llama",
)
REQUIRED_OBSERVED_PORTS = (5002, 5003, 5014, 8080, 8081, 8088, 18080)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CollectorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Handle:
    pid: int
    pgid: int
    proc_start_abstime: int
    process: Any
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Snapshot:
    monotonic_ns: int
    physical_footprint_mb: float
    cpu_percent: float
    available_mb: float
    swapouts_mb: float
    agx_bytes: int
    pids: tuple[int, ...]
    python_processes: int
    model_processes: int
    nonowned_model_processes: tuple[dict[str, Any], ...]
    live_v2_owner_pids: tuple[int, ...] = ()
    production_listener_pids: tuple[int, ...] = ()
    unexpected_listener_pids: tuple[int, ...] = ()
    candidate_gpu_processes: tuple[dict[str, Any], ...] = ()
    noncandidate_gpu_processes: tuple[dict[str, Any], ...] = ()
    per_process_gpu_permission: bool = False
    raw_sources: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


class Backend(Protocol):
    def now_ns(self) -> int: ...
    def sleep(self, seconds: float) -> None: ...
    def preflight(
        self,
        ports: Sequence[int],
        owner_markers: Sequence[str],
        pidfiles: Sequence[Path],
        launch_labels: Sequence[str],
        stopped_labels: Sequence[str],
    ) -> dict[str, Any]: ...
    def start(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> Handle: ...
    def snapshot(self, handles: Sequence[Handle]) -> Snapshot: ...
    def poll(self, handle: Handle) -> int | None: ...
    def finish(self, handle: Handle, timeout: float) -> tuple[int, str, str]: ...
    def stop(self, handles: Sequence[Handle], grace_seconds: float) -> None: ...
    def group_gone(self, handle: Handle) -> bool: ...
    def configure_scope(
        self, ports: Sequence[int], owner_markers: Sequence[str]
    ) -> None: ...
    def isolation_probe(
        self,
        profile: Path,
        workdir: Path,
        runtime: Path,
    ) -> dict[str, Any]: ...


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _external_website_binding(
    external: Mapping[str, Any],
    *,
    release_root: Path,
) -> tuple[Path, Path, str]:
    if not {"website_root", "website_admin_sha256"}.issubset(external):
        raise CollectorError("external website binding is incomplete")
    raw_root = external.get("website_root")
    expected = external.get("website_admin_sha256")
    if (
        not isinstance(raw_root, str)
        or not isinstance(expected, str)
        or not SHA256_RE.fullmatch(expected)
    ):
        raise CollectorError("external website path/hash binding is invalid")
    website_root = Path(raw_root)
    if not website_root.is_absolute() or website_root.resolve(strict=False) != website_root:
        raise CollectorError("external website root must be canonical and absolute")
    try:
        resolved_root = website_root.resolve(strict=True)
    except OSError as exc:
        raise CollectorError(f"external website root is missing: {exc}") from exc
    if (
        resolved_root != website_root
        or website_root.is_symlink()
        or not website_root.is_dir()
        or website_root.is_relative_to(release_root)
    ):
        raise CollectorError(
            "external website root must be a non-symlink directory outside the release"
        )
    admin = website_root / "admin" / "admin_server.py"
    try:
        resolved_admin = admin.resolve(strict=True)
    except OSError as exc:
        raise CollectorError(f"external Website Admin source is missing: {exc}") from exc
    if (
        resolved_admin != admin
        or admin.is_symlink()
        or not admin.is_file()
        or _sha256_file(admin) != expected
    ):
        raise CollectorError("external Website Admin source/hash is invalid")
    return website_root, admin, expected


def _external_runtime_bindings(
    external: Mapping[str, Any],
    *,
    release_root: Path,
) -> dict[str, Path]:
    pairs = {
        "laf_config_file": "laf_config_sha256",
        "google_credentials_file": "google_credentials_sha256",
        "google_calendar_token_source_file": "google_calendar_token_source_sha256",
        "laf_gmail_token_source_file": "laf_gmail_token_source_sha256",
        "file_review_token_source_file": "file_review_token_source_sha256",
    }
    result: dict[str, Path] = {}
    for path_name, digest_name in pairs.items():
        raw, expected = external.get(path_name), external.get(digest_name)
        if not isinstance(raw, str) or not isinstance(expected, str) or not SHA256_RE.fullmatch(expected):
            raise CollectorError(f"external {path_name} binding is invalid")
        path = Path(raw)
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise CollectorError(f"external {path_name} is missing: {exc}") from exc
        if (
            not path.is_absolute()
            or path.is_symlink()
            or resolved != path
            or not path.is_file()
            or path.is_relative_to(release_root)
            or _sha256_file(path) != expected
        ):
            raise CollectorError(f"external {path_name} path/hash is unsafe")
        result[path_name] = path
    for path_name, mode_name in (
        ("laf_config_file", "laf_config_mode"),
        ("google_credentials_file", "google_credentials_mode"),
    ):
        if external.get(mode_name) != "0600" or stat.S_IMODE(result[path_name].stat().st_mode) != 0o600:
            raise CollectorError(f"external {path_name} mode is unsafe")
    return result


def _python_runtime_binding(
    binding: Mapping[str, Any], *, release_root: Path
) -> tuple[Path, dict[str, str]]:
    raw = binding.get("python_runtime_binding")
    if not isinstance(raw, dict):
        raise CollectorError("Python runtime binding is missing")
    runtime = Path(str(binding.get("python_runtime") or "")).resolve(strict=True)
    normalized = {str(key): str(value) for key, value in raw.items()}
    if (
        normalized.get("path") != str(runtime)
        or normalized.get("realpath") != str(runtime)
        or normalized.get("sha256") != binding.get("python_runtime_sha256")
        or not SHA256_RE.fullmatch(normalized.get("sha256", ""))
        or _sha256_file(runtime) != normalized["sha256"]
        or runtime.is_symlink()
        or not runtime.is_file()
        or not os.access(runtime, os.X_OK)
    ):
        raise CollectorError("Python runtime path/hash binding is invalid")
    kind = normalized.get("kind")
    if kind == "release_member":
        if (
            not runtime.is_relative_to(release_root)
            or normalized.get("launcher_path") != str(runtime)
            or any(normalized.get(name) for name in (
                "manifest", "manifest_sha256", "tree_sha256"
            ))
        ):
            raise CollectorError("release-member Python runtime binding is invalid")
        return runtime, normalized
    if kind != "manifest_bound_external" or runtime.is_relative_to(release_root):
        raise CollectorError("external Python runtime binding kind/path is invalid")
    launcher_path = Path(normalized.get("launcher_path", ""))
    manifest = Path(normalized.get("manifest", ""))
    try:
        launcher_realpath = launcher_path.resolve(strict=True)
        manifest_realpath = manifest.resolve(strict=True)
    except OSError as exc:
        raise CollectorError(f"external Python runtime binding is missing: {exc}") from exc
    if (
        not launcher_path.is_absolute()
        or launcher_realpath != runtime
        or not manifest.is_absolute()
        or manifest_realpath != manifest
        or manifest.is_symlink()
        or not manifest.is_file()
        or not SHA256_RE.fullmatch(normalized.get("manifest_sha256", ""))
        or _sha256_file(manifest) != normalized["manifest_sha256"]
        or not SHA256_RE.fullmatch(normalized.get("tree_sha256", ""))
    ):
        raise CollectorError("external Python runtime manifest binding is invalid")
    return runtime, normalized


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    count = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise CollectorError("model tree contains a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        count += 1
    if count == 0:
        raise CollectorError("model tree is empty")
    return digest.hexdigest()


class _RUsageInfoV4(ctypes.Structure):
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "user",
            "system",
            "idle",
            "interrupt",
            "pageins",
            "wired",
            "resident",
            "phys",
            "start",
            "exit",
            "child_user",
            "child_system",
            "child_idle",
            "child_interrupt",
            "child_pageins",
            "child_elapsed",
            "disk_read",
            "disk_write",
            "qos_default",
            "qos_maintenance",
            "qos_background",
            "qos_utility",
            "qos_legacy",
            "qos_user_init",
            "qos_user_inter",
            "billed",
            "serviced",
            "logical",
            "lifetime",
            "instructions",
            "cycles",
            "billed_energy",
            "serviced_energy",
            "interval",
            "runnable",
        )
    ]


def _rusage(pid: int) -> tuple[int, int]:
    value = _RUsageInfoV4()
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    function = library.proc_pid_rusage
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    function.restype = ctypes.c_int
    if function(pid, 4, ctypes.byref(value)) != 0:
        raise OSError(ctypes.get_errno(), f"proc_pid_rusage failed for {pid}")
    return int(value.phys), int(value.start)


def parse_agx_bytes(text: str) -> int:
    values = [
        int(value)
        for value in re.findall(r'"In use system memory"\s*=\s*(\d+)', text)
    ]
    if len(values) != 1 or values[0] < 0:
        raise CollectorError("AGX In use system memory counter is missing/ambiguous")
    return values[0]


def _ps_rows(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([0-9.]+)\s+(.+)$", line)
        if not match:
            continue
        rows.append(
            {
                "pid": int(match.group(1)),
                "uid": int(match.group(2)),
                "ppid": int(match.group(3)),
                "pgid": int(match.group(4)),
                "cpu_percent": float(match.group(5)),
                "command": match.group(6),
            }
        )
    if not rows:
        raise CollectorError("process inventory is empty")
    return rows


def _raw_command(argv: Sequence[str], *, timeout: float = 10) -> dict[str, Any]:
    result = subprocess.run(
        tuple(argv), capture_output=True, timeout=timeout, check=False
    )
    stdout_bytes = bytes(result.stdout or b"")
    stderr_bytes = bytes(result.stderr or b"")
    return {
        "argv": list(argv),
        "returncode": int(result.returncode),
        "stdout": stdout_bytes.decode("utf-8", errors="replace"),
        "stderr": stderr_bytes.decode("utf-8", errors="replace"),
        "stdout_sha256": hashlib.sha256(stdout_bytes.decode("utf-8", errors="replace").encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(stderr_bytes.decode("utf-8", errors="replace").encode()).hexdigest(),
        "_stdout_bytes": stdout_bytes,
    }


class HostBackend:
    def __init__(self) -> None:
        self._ports: tuple[int, ...] = ()
        self._owner_markers: tuple[str, ...] = ()
        self._sandbox_profile: Path | None = None
        self._sandbox_workdir: Path | None = None
        self._seen_descendant_pgids: dict[int, set[int]] = {}
        self._seen_descendant_pids: dict[int, set[int]] = {}

    def configure_scope(
        self, ports: Sequence[int], owner_markers: Sequence[str]
    ) -> None:
        self._ports = tuple(int(port) for port in ports)
        self._owner_markers = tuple(str(marker) for marker in owner_markers)

    @staticmethod
    def _sandbox_prefix(profile: Path, workdir: Path) -> list[str]:
        real_home = Path(os.path.expanduser("~")).resolve(strict=True)
        return [
            "/usr/bin/sandbox-exec",
            "-D",
            f"WORKDIR={workdir}",
            "-D",
            f"LIVE_MAGI_ROOT={real_home / 'Library/Application Support/MAGI'}",
            "-D",
            f"CLOUD_STORAGE_ROOT={real_home / 'Library/CloudStorage'}",
            "-D",
            "VOLUMES_ROOT=/Volumes",
            "-D",
            f"KEYCHAINS_ROOT={real_home / 'Library/Keychains'}",
            "-D",
            f"SSH_ROOT={real_home / '.ssh'}",
            "-f",
            str(profile),
        ]

    def isolation_probe(
        self,
        profile: Path,
        workdir: Path,
        runtime: Path,
    ) -> dict[str, Any]:
        if not Path("/usr/bin/sandbox-exec").is_file():
            raise CollectorError("macOS Seatbelt sandbox-exec is unavailable")
        real_home = Path(os.path.expanduser("~")).resolve(strict=True)
        self._sandbox_profile = profile
        self._sandbox_workdir = workdir
        live_root = real_home / "Library/Application Support/MAGI"
        code = (
            "import errno,json,os,socket,sys;"
            "out={};"
            "s=socket.socket();s.settimeout(.2);"
            "\ntry:s.connect(('1.1.1.1',443));out['network']={'attempted':True,'denied':False,'errno':0}\n"
            "except OSError as e:out['network']={'attempted':True,'denied':e.errno in (errno.EPERM,errno.EACCES),'errno':e.errno}\n"
            "finally:s.close()\n"
            "try:os.listdir(sys.argv[1]);out['live']={'attempted':True,'denied':False,'errno':0}\n"
            "except OSError as e:out['live']={'attempted':True,'denied':e.errno in (errno.EPERM,errno.EACCES),'errno':e.errno}\n"
            "print('MAGI_V3_SEATBELT='+json.dumps(out,sort_keys=True,separators=(',',':')))"
        )
        argv = [
            *self._sandbox_prefix(profile, workdir),
            str(runtime),
            "-I",
            "-c",
            code,
            str(live_root),
        ]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10, check=False)
        markers = [
            line[len("MAGI_V3_SEATBELT=") :]
            for line in result.stdout.splitlines()
            if line.startswith("MAGI_V3_SEATBELT=")
        ]
        if result.returncode != 0 or len(markers) != 1:
            raise CollectorError("Seatbelt denial probe did not complete")
        payload = json.loads(markers[0])
        network = payload.get("network", {})
        live = payload.get("live", {})
        if network.get("denied") is not True or live.get("denied") is not True:
            raise CollectorError("Seatbelt did not deny network and live MAGI state")
        return {
            "network_probe": {
                "attempted": True,
                "denied_by_seatbelt": True,
                "errno": network.get("errno"),
            },
            "live_state_probe": {
                "attempted": True,
                "denied_by_seatbelt": True,
                "errno": live.get("errno"),
            },
            "probe_argv_sha256": hashlib.sha256(
                json.dumps(argv, separators=(",", ":")).encode()
            ).hexdigest(),
        }

    def now_ns(self) -> int:
        return time.monotonic_ns()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def _ps_raw() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        value = _raw_command(
            ("/bin/ps", "-axo", "pid=,uid=,ppid=,pgid=,%cpu=,command=")
        )
        if value["returncode"] != 0:
            raise CollectorError("ps inventory failed")
        return value, _ps_rows(str(value["stdout"]))

    @staticmethod
    def _agx() -> int:
        result = subprocess.run(
            ("/usr/sbin/ioreg", "-r", "-c", "AGXAccelerator"),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise CollectorError("ioreg AGX sample failed")
        return parse_agx_bytes(result.stdout)

    @staticmethod
    def _listener_raw() -> dict[str, Any]:
        command = (
            "/usr/sbin/lsof",
            "-b",
            "-nP",
            "-a",
            "-iTCP",
            "-sTCP:LISTEN",
            "-Fpn",
        )
        value = _raw_command(command)
        if value["returncode"] not in {0, 1}:
            raise CollectorError("single-pass nonblocking lsof listener inventory failed")
        return value

    @staticmethod
    def _ioreg_raw() -> dict[str, Any]:
        value = _raw_command(("/usr/sbin/ioreg", "-r", "-c", "AGXAccelerator"))
        if value["returncode"] != 0:
            raise CollectorError("ioreg AGX sample failed")
        parse_agx_bytes(str(value["stdout"]))
        return value

    @staticmethod
    def _powermetrics_raw() -> tuple[dict[str, Any], tuple[dict[str, int], ...]]:
        if os.geteuid() == 0:
            raise CollectorError("the resource-window collector must not run as root")
        measurement_argv = (
            "/usr/bin/powermetrics",
            "--format",
            "plist",
            "--sample-count",
            "1",
            "--sample-rate",
            "100",
            "--samplers",
            "tasks,gpu_power",
            "--show-process-gpu",
        )
        invoker_argv = ("/usr/bin/sudo", "-n", "--", *measurement_argv)
        value = _raw_command(invoker_argv, timeout=15)
        value["invoker_argv"] = value["argv"]
        value["argv"] = list(measurement_argv)
        value["privilege_receipt"] = {
            "schema": "magi.v3.fixed-powermetrics-privilege/v1",
            "collector_euid": os.geteuid(),
            "collector_ran_as_root": False,
            "invoker": "/usr/bin/sudo",
            "noninteractive": True,
            "fixed_measurement_argv_sha256": hashlib.sha256(
                json.dumps(list(measurement_argv), separators=(",", ":")).encode()
            ).hexdigest(),
        }
        if value["returncode"] != 0:
            raise CollectorError(
                "per-process GPU evidence unavailable; fixed passwordless read-only powermetrics permission is required"
            )
        rows = parse_powermetrics_process_gpu(value["_stdout_bytes"])
        return value, rows

    @staticmethod
    def _public_raw(value: Mapping[str, Any]) -> dict[str, Any]:
        return {key: item for key, item in value.items() if not key.startswith("_")}

    @staticmethod
    def _listener_pids(text: str, ports: Sequence[int]) -> set[int]:
        current_pid: int | None = None
        wanted = set(ports)
        result: set[int] = set()
        for line in text.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                current_pid = int(line[1:])
            elif line.startswith("n") and current_pid is not None:
                match = re.search(r":(\d+)(?:\s|$)", line[1:])
                if match and int(match.group(1)) in wanted:
                    result.add(current_pid)
        return result

    def preflight(
        self,
        ports: Sequence[int],
        owner_markers: Sequence[str],
        pidfiles: Sequence[Path],
        launch_labels: Sequence[str],
        stopped_labels: Sequence[str],
    ) -> dict[str, Any]:
        ps_raw, rows = self._ps_raw()
        raw = str(ps_raw["stdout"])
        own_pid = os.getpid()
        parent_pid = os.getppid()
        v2 = [row for row in rows if LIVE_V2_MARKER in row["command"]]
        v3_pids = {
            row["pid"]
            for row in rows
            if row["pid"] not in {own_pid, parent_pid}
            and any(marker in row["command"] for marker in owner_markers)
        }
        for pidfile in pidfiles:
            if pidfile.is_file():
                value = pidfile.read_text(encoding="utf-8", errors="replace").strip()
                if not value.isdigit():
                    raise CollectorError(f"V3 pidfile is malformed: {pidfile}")
                v3_pids.add(int(value))
        for label in launch_labels:
            result = subprocess.run(
                ("/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                matches = re.findall(r"^\s*pid\s*=\s*(\d+)\s*$", result.stdout, re.MULTILINE)
                if len(matches) > 1:
                    raise CollectorError(f"launchd label PID is ambiguous: {label}")
                v3_pids.update(int(value) for value in matches)
        stopped_states: list[dict[str, Any]] = []
        for label in stopped_labels:
            result = subprocess.run(
                ("/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"),
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            loaded = result.returncode == 0
            stopped_states.append(
                {
                    "label": label,
                    "loaded": loaded,
                    "returncode": result.returncode,
                    "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
                    "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
                }
            )
        if any(row["loaded"] for row in stopped_states):
            raise CollectorError("outer executor left a required V2/model launchd label loaded")
        models = [
            row
            for row in rows
            if row["pid"] not in {own_pid, parent_pid}
            and MODEL_WORKLOAD_RE.search(row["command"])
        ]
        listener_raw = self._listener_raw()
        ioreg_raw = self._ioreg_raw()
        power_raw, _gpu_rows = self._powermetrics_raw()
        port_pids = self._listener_pids(str(listener_raw["stdout"]), ports)
        return {
            "v2_fully_stopped": not v2,
            "candidate_not_started_at_baseline": not models,
            "production_ingress_quiesced": not port_pids,
            "v2_owner_pids": sorted(row["pid"] for row in v2),
            "v3_owner_pids_before_start": sorted(v3_pids),
            "production_port_owner_pids": sorted(port_pids),
            "noncandidate_user_metal_processes": [
                {"pid": row["pid"], "command_sha256": hashlib.sha256(row["command"].encode()).hexdigest()}
                for row in models
            ],
            "process_inventory_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "per_process_gpu_permission": True,
            "raw_source_coverage": sorted(RAW_SOURCE_COMMANDS),
            "raw_sources": {
                "ps": self._public_raw(ps_raw),
                "lsof": self._public_raw(listener_raw),
                "ioreg": self._public_raw(ioreg_raw),
                "powermetrics": self._public_raw(power_raw),
            },
            "required_stopped_launchd_labels": list(stopped_labels),
            "stopped_launchd_states": stopped_states,
        }

    def start(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> Handle:
        if self._sandbox_profile is None or self._sandbox_workdir is None:
            raise CollectorError("Seatbelt isolation probe was not completed before start")
        command = [
            *self._sandbox_prefix(self._sandbox_profile, self._sandbox_workdir),
            *argv,
        ]
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise CollectorError(
                    f"owned command exited during identity capture: {stdout[-200:]} {stderr[-200:]}"
                )
            try:
                _phys, start = _rusage(process.pid)
                return Handle(process.pid, os.getpgid(process.pid), start, process, tuple(argv))
            except OSError:
                time.sleep(0.01)
        process.kill()
        process.wait()
        raise CollectorError("owned command identity could not be captured")

    def snapshot(self, handles: Sequence[Handle]) -> Snapshot:
        ps_raw_value, rows = self._ps_raw()
        ps_raw_text = str(ps_raw_value["stdout"])
        listener_raw = self._listener_raw()
        ioreg_raw = self._ioreg_raw()
        power_raw, gpu_rows = self._powermetrics_raw()
        pgids = {handle.pgid for handle in handles}
        roots = {handle.pid for handle in handles}
        owned_pids = set(roots)
        changed = True
        while changed:
            changed = False
            for row in rows:
                if row["pid"] in owned_pids or row["ppid"] not in owned_pids:
                    continue
                owned_pids.add(row["pid"])
                changed = True
        owned = [
            row for row in rows if row["pgid"] in pgids or row["pid"] in owned_pids
        ]
        for handle in handles:
            descendants = {handle.pid}
            changed = True
            while changed:
                changed = False
                for row in owned:
                    if row["pid"] in descendants or row["ppid"] not in descendants:
                        continue
                    descendants.add(row["pid"])
                    changed = True
            self._seen_descendant_pids.setdefault(handle.pid, set()).update(descendants)
            self._seen_descendant_pgids.setdefault(handle.pid, set()).update(
                row["pgid"] for row in owned if row["pid"] in descendants
            )
        pids = tuple(sorted(row["pid"] for row in owned))
        physical = 0
        for pid in pids:
            value, _start = _rusage(pid)
            physical += value
        memory = subprocess.run(
            ("/usr/bin/memory_pressure", "-Q"),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        vm = subprocess.run(
            ("/usr/bin/vm_stat",), capture_output=True, text=True, timeout=10, check=False
        )
        if memory.returncode != 0 or vm.returncode != 0:
            raise CollectorError("memory_pressure/vm_stat sample failed")
        pressure = parse_memory_pressure(memory.stdout)
        vm_stat = parse_vm_stat(vm.stdout)
        other_models = [
            row
            for row in rows
            if row["pid"] not in {item["pid"] for item in owned}
            and MODEL_WORKLOAD_RE.search(row["command"])
        ]
        live_v2 = tuple(
            sorted(row["pid"] for row in rows if LIVE_V2_MARKER in row["command"])
        )
        listener_pids = tuple(
            sorted(self._listener_pids(str(listener_raw["stdout"]), self._ports))
        )
        unexpected = tuple(sorted(set(listener_pids) - set(pids)))
        gpu_candidates = tuple(
            {**row, "candidate": True}
            for row in gpu_rows
            if row["pid"] in set(pids)
        )
        gpu_non_candidates = tuple(
            {**row, "candidate": False}
            for row in gpu_rows
            if row["pid"] not in set(pids) and row["gpu_time_ns"] > 0
        )
        return Snapshot(
            monotonic_ns=self.now_ns(),
            physical_footprint_mb=physical / 1024**2,
            cpu_percent=sum(row["cpu_percent"] for row in owned),
            available_mb=pressure.total_memory_bytes / 1024**2 * pressure.free_percent / 100,
            swapouts_mb=vm_stat.swapouts_total_mb,
            agx_bytes=parse_agx_bytes(str(ioreg_raw["stdout"])),
            pids=pids,
            python_processes=sum("python" in row["command"].lower() for row in owned),
            model_processes=sum(bool(MODEL_WORKLOAD_RE.search(row["command"])) for row in owned),
            nonowned_model_processes=tuple(
                {"pid": row["pid"], "command_sha256": hashlib.sha256(row["command"].encode()).hexdigest()}
                for row in other_models
            ),
            live_v2_owner_pids=live_v2,
            production_listener_pids=listener_pids,
            unexpected_listener_pids=unexpected,
            candidate_gpu_processes=gpu_candidates,
            noncandidate_gpu_processes=gpu_non_candidates,
            per_process_gpu_permission=True,
            raw_sources={
                "ps": self._public_raw(ps_raw_value),
                "lsof": self._public_raw(listener_raw),
                "ioreg": self._public_raw(ioreg_raw),
                "powermetrics": self._public_raw(power_raw),
            },
        )

    def poll(self, handle: Handle) -> int | None:
        return handle.process.poll()

    def finish(self, handle: Handle, timeout: float) -> tuple[int, str, str]:
        stdout, stderr = handle.process.communicate(timeout=timeout)
        return int(handle.process.returncode), stdout, stderr

    def stop(self, handles: Sequence[Handle], grace_seconds: float) -> None:
        groups = {
            group
            for handle in handles
            for group in {
                handle.pgid,
                *self._seen_descendant_pgids.get(handle.pid, set()),
            }
        }
        for group in groups:
            try:
                os.killpg(group, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if all(handle.process.poll() is not None for handle in handles):
                break
            time.sleep(0.05)
        for group in groups:
            try:
                os.killpg(group, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                raise CollectorError(f"owned process group cannot be inspected: {group}")
            else:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        for handle in handles:
            handle.process.wait(timeout=5)
        remaining = [handle.pgid for handle in handles if not self.group_gone(handle)]
        if remaining:
            raise CollectorError(f"owned process groups remained after stop: {remaining}")

    def group_gone(self, handle: Handle) -> bool:
        try:
            os.killpg(handle.pgid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            return False
        else:
            return False
        try:
            _physical, observed_start = _rusage(handle.pid)
        except OSError:
            observed_start = -1
        if observed_start == handle.proc_start_abstime:
            return False
        for pid in self._seen_descendant_pids.get(handle.pid, set()) - {handle.pid}:
            try:
                _physical, _start = _rusage(pid)
            except OSError:
                continue
            return False
        return True


def _validate_plan(plan: Mapping[str, Any], token: str) -> dict[str, Any]:
    supplied = plan.get("plan_sha256")
    unsigned = dict(plan)
    unsigned.pop("plan_sha256", None)
    if supplied != sha256_json(unsigned) or plan.get("schema") != PLAN_SCHEMA:
        raise CollectorError("immutable resource-window plan hash/schema is invalid")
    if hashlib.sha256(token.encode()).hexdigest() != plan.get("approval_token_sha256"):
        raise CollectorError("resource-window approval token mismatch")
    binding = plan.get("release_binding")
    orchestration = plan.get("orchestration_binding")
    commands = plan.get("commands")
    durations = plan.get("durations")
    thresholds = plan.get("thresholds")
    policy = plan.get("policy_binding")
    workload = plan.get("workload_binding")
    outer_owner = plan.get("outer_owner_contract")
    external = plan.get("external_inputs")
    if not all(
        isinstance(value, dict)
        for value in (
            binding,
            orchestration,
            commands,
            durations,
            thresholds,
            policy,
            workload,
            outer_owner,
            external,
        )
    ):
        raise CollectorError("resource-window plan sections are missing")
    assert isinstance(binding, dict) and isinstance(commands, dict)
    assert isinstance(orchestration, dict)
    assert isinstance(durations, dict) and isinstance(thresholds, dict)
    if durations.get("negative_control_seconds", 0) < 30:
        raise CollectorError("negative control must run for at least 30 seconds")
    if durations.get("v2_reference_seconds", 0) < 60:
        raise CollectorError("V2 reference must run for at least 60 seconds")
    if durations.get("v3_deep_idle_seconds") != 1800:
        raise CollectorError("V3 deep-idle observation must be exactly 1800 seconds")
    if not 1 <= durations.get("sample_interval_seconds", 0) <= 10:
        raise CollectorError("sample interval must be within 1..10 seconds")
    if commands.get("model_repeats") != 3:
        raise CollectorError("model benchmark requires exactly three repeats")
    phase_token = os.environ.get("MAGI_V3_ISOLATED_LIVE_ZERO_OWNER_PHASE_TOKEN", "")
    if (
        orchestration.get("caller")
        != "scripts.v3_validation.isolated_live_execute"
        or orchestration.get("phase") != "resource_window_after_v2_zero_owner"
        or orchestration.get("v2_restore_owner")
        != "outer_isolated_live_executor_finally"
        or orchestration.get("collector_may_stop_or_restore_v2") is not False
        or not phase_token
        or hashlib.sha256(phase_token.encode()).hexdigest()
        != orchestration.get("zero_owner_phase_token_sha256")
        or not isinstance(orchestration.get("outer_plan_sha256"), str)
        or len(orchestration["outer_plan_sha256"]) != 64
        or orchestration.get("outer_plan_file_sha256")
        != orchestration.get("outer_plan_sha256")
        or not isinstance(orchestration.get("outer_plan_semantic_sha256"), str)
        or len(orchestration["outer_plan_semantic_sha256"]) != 64
        or not isinstance(orchestration.get("provisional_gate_sha256"), str)
        or len(orchestration["provisional_gate_sha256"]) != 64
        or not isinstance(orchestration.get("provisional_gate_context"), dict)
    ):
        raise CollectorError(
            "collector must be invoked by the outer isolated-LIVE zero-owner phase"
        )
    command_rows = [
        *commands.get("v2_core", []),
        *commands.get("v3_core", []),
        commands.get("v2_model"),
        commands.get("v3_model"),
    ]
    if not command_rows or any(not isinstance(row, list) or not row for row in command_rows):
        raise CollectorError("resource-window command argv is invalid")
    release_root = Path(str(binding.get("release_root") or "")).resolve(strict=True)
    runtime, _runtime_binding = _python_runtime_binding(
        binding, release_root=release_root
    )
    for argv in command_rows:
        executable = Path(str(argv[0])).resolve(strict=True)
        if not executable.is_file() or executable.name in SHELL_NAMES:
            raise CollectorError("shell/non-file executables are forbidden")
        if executable != runtime and not executable.is_relative_to(release_root):
            raise CollectorError("command executable is outside the release/runtime")
        if executable == runtime:
            if len(argv) < 2:
                raise CollectorError("Python command is missing its release script")
            script = Path(str(argv[1])).resolve(strict=True)
            if not script.is_file() or not script.is_relative_to(release_root):
                raise CollectorError("Python command script is outside the sealed release")
        for argument in argv[1:]:
            if not isinstance(argument, str):
                raise CollectorError("command argument is not a string")
    core_adapter = release_root / "scripts/v3_validation/resource_window_core_adapter.py"
    model_adapter = release_root / "scripts/v3_validation/resource_window_model_adapter.py"
    expected_v2_core = [[
        str(runtime), str(core_adapter), "--arm", "v2", "--role", "application",
        "--release-root", str(release_root),
    ]]
    expected_v3_core = [
        [
            str(runtime), str(core_adapter), "--arm", "v3", "--role", role,
            "--release-root", str(release_root),
        ]
        for role in ("control", "supervisor", "gateway")
    ]
    v2_model = commands.get("v2_model")
    v3_model = commands.get("v3_model")
    if (
        commands.get("v2_core") != expected_v2_core
        or commands.get("v3_core") != expected_v3_core
        or not isinstance(v2_model, list)
        or not isinstance(v3_model, list)
        or len(v2_model) != 16
        or len(v3_model) != 16
        or v2_model[:2] != [str(runtime), str(model_adapter)]
        or v3_model[:2] != [str(runtime), str(model_adapter)]
        or v2_model[2:] != [
            "--arm", "v2-reference", "--backend", v2_model[5],
            "--model", str(binding.get("model_root")),
            "--prompt", str(binding.get("prompt_path")),
            "--max-tokens", "256", "--model-port", "18080",
            "--arm-endpoint", "http://127.0.0.1:5003/collab/chat",
        ]
        or v3_model[2:] != [
            "--arm", "v3-candidate", "--backend", v2_model[5],
            "--model", str(binding.get("model_root")),
            "--prompt", str(binding.get("prompt_path")),
            "--max-tokens", "256", "--model-port", "18080",
            "--arm-endpoint", "http://127.0.0.1:5003/collab/chat",
        ]
        or v2_model[5] not in {"mlx_lm", "mlx_vlm"}
    ):
        raise CollectorError("resource commands are not the exact production composition")
    assert (
        isinstance(policy, dict)
        and isinstance(workload, dict)
        and isinstance(external, dict)
    )
    raw_policy = policy.get("policy_raw_json")
    resolved = policy.get("resolved_thresholds")
    request = workload.get("request")
    composition = workload.get("composition")
    if (
        not isinstance(raw_policy, str)
        or hashlib.sha256(raw_policy.encode()).hexdigest()
        != binding.get("resource_policy_sha256")
        or not isinstance(resolved, dict)
        or policy.get("resolved_thresholds_sha256") != sha256_json(resolved)
        or dict(resolved) != dict(thresholds)
        or not isinstance(request, dict)
        or workload.get("request_sha256") != sha256_json(request)
        or not isinstance(workload.get("http_request_sha256"), str)
        or len(workload["http_request_sha256"]) != 64
        or request.get("corpus_sha256") != binding.get("prompt_sha256")
        or request.get("model_tree_sha256") != binding.get("model_tree_sha256")
        or not isinstance(composition, dict)
        or composition.get("arm_transport") != "arm_owned_production_process"
        or composition.get("shared_direct_backend") is not False
        or composition.get("external_inputs") != external
        or composition.get("composition_sha256")
        != sha256_json(
            {key: value for key, value in composition.items() if key != "composition_sha256"}
        )
    ):
        raise CollectorError("policy/workload/composition binding failed")
    _external_website_binding(external, release_root=release_root)
    _external_runtime_bindings(external, release_root=release_root)
    assert isinstance(outer_owner, dict)
    if (
        outer_owner.get("required_stopped_launchd_labels")
        != list(REQUIRED_STOPPED_LABELS)
        or outer_owner.get("required_absent_process_patterns")
        != list(REQUIRED_MODEL_OWNER_PATTERNS)
        or outer_owner.get("zero_owner_snapshot_required_coverage")
        != ["launchd", "ownership", "pidfile", "port", "process"]
        or outer_owner.get("outer_must_capture_initial_label_state") is not True
        or outer_owner.get("outer_finally_restore_initial_label_state_exactly") is not True
        or outer_owner.get("restore_proof_owner")
        != "outer_isolated_live_executor_finally"
        or not isinstance(outer_owner.get("outer_restore_readiness"), dict)
    ):
        raise CollectorError("outer stop/restore owner contract is weakened")
    receipt_path = Path(str(plan.get("consumption_receipt_path") or "")).resolve()
    workdir = Path(str(plan.get("workdir") or "")).resolve()
    if receipt_path.parent != workdir.parent or receipt_path == workdir:
        raise CollectorError("one-time consumption receipt path is outside the plan domain")
    return {
        "binding": binding,
        "commands": commands,
        "durations": durations,
        "thresholds": thresholds,
        "orchestration": orchestration,
        "policy": policy,
        "workload": workload,
        "external_inputs": external,
        "outer_owner": outer_owner,
        "consumption_receipt_path": receipt_path,
        "plan_sha256": supplied,
    }


def _observe(
    backend: Backend,
    handles: Sequence[Handle],
    seconds: float,
    interval: float,
) -> tuple[list[Snapshot], float]:
    started = backend.now_ns()
    deadline = started + int(seconds * 1_000_000_000)
    samples: list[Snapshot] = []
    while backend.now_ns() < deadline:
        if any(backend.poll(handle) is not None for handle in handles):
            raise CollectorError("owned service exited during observation")
        sample = backend.snapshot(handles)
        _assert_snapshot(sample)
        samples.append(sample)
        backend.sleep(min(interval, max(0.0, (deadline - backend.now_ns()) / 1e9)))
    elapsed = (backend.now_ns() - started) / 1e9
    if elapsed < seconds:
        raise CollectorError("observation clock completed early")
    samples.append(backend.snapshot(handles))
    _assert_snapshot(samples[-1])
    return samples, elapsed


def _assert_snapshot(sample: Snapshot) -> None:
    if sample.live_v2_owner_pids:
        raise CollectorError("live V2 owner reappeared during isolated resource window")
    if sample.unexpected_listener_pids:
        raise CollectorError("foreign production listener appeared during resource window")
    if sample.nonowned_model_processes:
        raise CollectorError("unrelated model workload appeared")
    if sample.per_process_gpu_permission is not True:
        raise CollectorError("per-process GPU evidence permission was lost")
    if set(sample.raw_sources) != set(RAW_SOURCE_COMMANDS):
        raise CollectorError("raw ps/lsof/ioreg/powermetrics sample is incomplete")


def _raw_sample_row(sample: Snapshot, phase: str, sequence: int) -> dict[str, Any]:
    _assert_snapshot(sample)
    sources = {key: dict(value) for key, value in sample.raw_sources.items()}
    return {
        "sequence": sequence,
        "phase": phase,
        "monotonic_ns": sample.monotonic_ns,
        "live_v2_owner_pids": list(sample.live_v2_owner_pids),
        "owned_process_pids": list(sample.pids),
        "production_listener_pids": list(sample.production_listener_pids),
        "unexpected_listener_pids": list(sample.unexpected_listener_pids),
        "candidate_gpu_processes": [dict(row) for row in sample.candidate_gpu_processes],
        "noncandidate_gpu_processes": [
            dict(row) for row in sample.noncandidate_gpu_processes
        ],
        "per_process_gpu_permission": sample.per_process_gpu_permission,
        "system_agx_bytes": sample.agx_bytes,
        "ps_inventory_sha256": sources["ps"]["stdout_sha256"],
        "listener_inventory_sha256": sources["lsof"]["stdout_sha256"],
        "ioreg_inventory_sha256": sources["ioreg"]["stdout_sha256"],
        "powermetrics_inventory_sha256": sources["powermetrics"]["stdout_sha256"],
        **sources,
    }


def _p95(values: Sequence[float]) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, (95 * len(ordered) + 99) // 100 - 1))]


def _consume_once(
    validated: Mapping[str, Any], plan: Mapping[str, Any]
) -> tuple[Path, dict[str, Any], str]:
    path = Path(validated["consumption_receipt_path"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = {
        "schema": "magi.v3.resource-window-plan-consumption/v1",
        "plan_sha256": validated["plan_sha256"],
        "approval_token_sha256": plan["approval_token_sha256"],
        "outer_plan_sha256": validated["orchestration"]["outer_plan_sha256"],
        "outer_plan_semantic_sha256": validated["orchestration"][
            "outer_plan_semantic_sha256"
        ],
        "provisional_gate_sha256": validated["orchestration"][
            "provisional_gate_sha256"
        ],
        "zero_owner_phase_token_sha256": validated["orchestration"][
            "zero_owner_phase_token_sha256"
        ],
        "consumer_pid": os.getpid(),
        "consumed_monotonic_ns": time.monotonic_ns(),
    }
    data = (json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return path, receipt, hashlib.sha256(data).hexdigest()


def _write_owned(path: Path, data: bytes, mode: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return hashlib.sha256(data).hexdigest()


def _prepare_v3_production_environment(
    *,
    workdir: Path,
    release_root: Path,
    runtime: Path,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    external_inputs: Mapping[str, Any],
) -> tuple[dict[str, str], dict[str, Any]]:
    runtime_root = (
        workdir
        / "home"
        / "Library"
        / "Application Support"
        / "MAGI"
        / "runtime"
        / "MAGI_v3"
    )
    if (
        runtime_root == release_root
        or runtime_root.is_relative_to(release_root)
        or release_root.is_relative_to(runtime_root)
    ):
        raise CollectorError("resource-window runtime must not overlap the sealed release")
    runtime_root.mkdir(parents=True)
    env_file = workdir / "inputs" / "resource-window.env"
    cron_file = workdir / "inputs" / "cron_jobs.json"
    env_sha = _write_owned(env_file, b"# isolated resource window\n", 0o600)
    cron_sha = _write_owned(cron_file, b"[]\n", 0o600)
    service_manifest = release_root / "config/v3_service_manifest.json"
    service_sha = _sha256_file(service_manifest)
    role_source = json.loads(
        (release_root / "config/v3_launchagent_roles.json").read_text(encoding="utf-8")
    )
    role_rows = role_source.get("roles")
    if not isinstance(role_rows, list) or len(role_rows) != 3:
        raise CollectorError("production V3 role composition is incomplete")
    ownership_path = runtime_root / "ownership" / "ownership-manifest.json"
    release_id = str(manifest.get("release_id") or "")
    named_paths = named_mutable_state_paths(runtime_root)
    named_environment = {
        env_name: named_paths[binding_name]
        for env_name, (binding_name, _relative) in NAMED_MUTABLE_STATE_BINDINGS.items()
    }
    roles = []
    for raw in role_rows:
        if not isinstance(raw, dict):
            raise CollectorError("production V3 role binding is invalid")
        roles.append(
            {
                "role": raw["role"],
                "label": raw["label"],
                "ports": raw["ports"],
                "ownership_domains": raw["ownership_domains"],
                "release_id": release_id,
                "release_manifest_sha256": manifest_sha256,
                "ownership_manifest": str(ownership_path),
                **named_paths,
            }
        )
    ownership = {
        "schema_version": 1,
        "status": "isolated_resource_window",
        "release_id": release_id,
        "release_manifest": str(release_root / "release-manifest.json"),
        "release_manifest_sha256": manifest_sha256,
        "deployment_mode": "production",
        "service_manifest": str(service_manifest),
        "service_manifest_sha256": service_sha,
        "runtime_root": str(runtime_root),
        "external_inputs": named_paths,
        "roles": roles,
    }
    ownership_data = (
        json.dumps(ownership, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode()
    ownership_sha = _write_owned(ownership_path, ownership_data, 0o600)
    website_root, _website_admin, website_admin_sha256 = _external_website_binding(
        external_inputs,
        release_root=release_root,
    )
    shared = runtime_root / "shared"
    runtime_bindings = _external_runtime_bindings(
        external_inputs,
        release_root=release_root,
    )
    secrets_root = shared / "secrets"
    mutable_targets = {
        "MAGI_GOOGLE_CALENDAR_TOKEN_PATH": (
            secrets_root / "google_calendar_token.json",
            runtime_bindings["google_calendar_token_source_file"],
        ),
        "MAGI_LAF_GMAIL_TOKEN_PATH": (
            secrets_root / "laf_gmail_token.pickle",
            runtime_bindings["laf_gmail_token_source_file"],
        ),
        "MAGI_FILE_REVIEW_TOKEN_PATH": (
            secrets_root / "filereview_token.pickle",
            runtime_bindings["file_review_token_source_file"],
        ),
    }
    token_receipts: list[dict[str, str]] = []
    for env_name, (target, source) in mutable_targets.items():
        source_sha = _sha256_file(source)
        target_sha = _write_owned(target, source.read_bytes(), 0o600)
        if target_sha != source_sha:
            raise CollectorError(f"resource-window mutable token copy drifted: {env_name}")
        token_receipts.append(
            {"environment": env_name, "source_sha256": source_sha, "target_sha256": target_sha}
        )
    result = {
        "MAGI_V3_RESOURCE_RUNTIME_ROOT": str(runtime_root),
        "MAGI_V3_RELEASE_ID": release_id,
        "MAGI_V3_EXECUTABLE_PATH": str(runtime),
        "MAGI_V3_EXECUTABLE_SHA256": _sha256_file(runtime),
        "MAGI_V3_DEPLOYMENT_MODE": "production",
        "MAGI_V3_SERVICE_MANIFEST": str(service_manifest),
        "MAGI_V3_SERVICE_MANIFEST_SHA256": service_sha,
        "MAGI_V3_LIVE_VALIDATION": "0",
        "MAGI_V3_EXTERNAL_WRITES_ENABLED": "1",
        "MAGI_V3_NOTIFICATIONS_ENABLED": "1",
        "MAGI_V3_SCHEDULER_ENABLED": "1",
        "MAGI_V3_SHARED_STATE_DIR": str(shared),
        "MAGI_SHARED_STATE_DIR": str(shared),
        "MAGI_RUNTIME_DIR": str(shared / "runtime"),
        "MAGI_V3_OWNERSHIP_MANIFEST": str(ownership_path),
        "MAGI_V3_OWNERSHIP_MANIFEST_SHA256": ownership_sha,
        "MAGI_V3_REQUIRE_ACTIVE_MARKER": "0",
        "MAGI_ENV_FILE": str(env_file),
        "MAGI_ENV_FILE_SHA256": env_sha,
        "MAGI_CRON_JOBS_FILE": str(cron_file),
        "MAGI_CRON_JOBS_SHA256": cron_sha,
        "MAGI_CRON_JOBS_SOURCE_SHA256": cron_sha,
        "MAGI_WEBSITE_ROOT": str(website_root),
        "MAGI_WEBSITE_ADMIN_SHA256": website_admin_sha256,
        "MAGI_V3_EXTERNAL_INPUT_CONTRACT": "1",
        "MAGI_CONFIG_PATH": str(runtime_bindings["laf_config_file"]),
        "MAGI_CONFIG_SHA256": str(external_inputs["laf_config_sha256"]),
        "MAGI_CONFIG_MODE": "0600",
        "MAGI_LAF_CONFIG_FILE": str(runtime_bindings["laf_config_file"]),
        "MAGI_LAF_CONFIG_SHA256": str(external_inputs["laf_config_sha256"]),
        "MAGI_JSON_DIR": str(runtime_bindings["laf_config_file"].parent),
        "MAGI_PUBLIC_SOURCE_ROOT_DIR": str(release_root),
        "OSC_CONFIG_PATH": str(runtime_bindings["laf_config_file"]),
        "MAGI_GOOGLE_CREDENTIALS_PATH": str(runtime_bindings["google_credentials_file"]),
        "MAGI_GOOGLE_CREDENTIALS_SHA256": str(external_inputs["google_credentials_sha256"]),
        "MAGI_GOOGLE_CREDENTIALS_MODE": "0600",
        "MAGI_GMAIL_CREDENTIALS_PATH": str(runtime_bindings["google_credentials_file"]),
        "MAGI_GOOGLE_CALENDAR_TOKEN_PATH": str(mutable_targets["MAGI_GOOGLE_CALENDAR_TOKEN_PATH"][0]),
        "MAGI_LAF_GMAIL_TOKEN_PATH": str(mutable_targets["MAGI_LAF_GMAIL_TOKEN_PATH"][0]),
        "MAGI_FILE_REVIEW_TOKEN_PATH": str(mutable_targets["MAGI_FILE_REVIEW_TOKEN_PATH"][0]),
        "MAGI_GMAIL_COMPOSE_TOKEN_PATH": str(secrets_root / "gmail_compose_token.json"),
        "MAGI_V3_OPTIONAL_DEGRADED_INPUTS": "gmail_compose_token",
        "MAGI_USE_RUNTIME_DIR": "1",
        "MAGI_CRON_DEFINITIONS_IMMUTABLE": "1",
        **named_environment,
    }
    receipt = {
        "schema": "magi.v3.resource-window-production-environment/v1",
        "release_id": release_id,
        "runtime_root": str(runtime_root),
        "service_manifest_sha256": service_sha,
        "ownership_manifest_sha256": ownership_sha,
        "environment_file_sha256": env_sha,
        "cron_jobs_sha256": cron_sha,
        "cron_jobs_source_sha256": cron_sha,
        "python_runtime_sha256": _sha256_file(runtime),
        "website_root": str(website_root),
        "website_admin_sha256": website_admin_sha256,
        "mutable_token_handoff": token_receipts,
        "named_mutable_state_bindings": named_environment,
        "optional_degraded_inputs": ["gmail_compose_token"],
    }
    receipt["receipt_sha256"] = sha256_json(receipt)
    return result, receipt


def collect(plan: Mapping[str, Any], token: str, *, backend: Backend | None = None) -> dict[str, Any]:
    validated = _validate_plan(plan, token)
    binding = validated["binding"]
    commands = validated["commands"]
    durations = validated["durations"]
    thresholds = validated["thresholds"]
    backend = backend or HostBackend()
    _receipt_path, consumption_receipt, consumption_receipt_sha = _consume_once(
        validated, plan
    )
    release_root = Path(binding["release_root"]).resolve(strict=True)
    workdir = Path(str(plan.get("workdir") or "")).resolve()
    if workdir.exists() and (not workdir.is_dir() or any(workdir.iterdir())):
        raise CollectorError("collector workdir must be empty")
    workdir.mkdir(parents=True, exist_ok=True)
    marker = workdir / ".magi-v3-resource-window-owned"
    marker.write_text(validated["plan_sha256"], encoding="utf-8")
    manifest_path = release_root / "release-manifest.json"
    policy_path = release_root / "config/v3_resource_policy.json"
    runtime_path, runtime_binding = _python_runtime_binding(
        binding, release_root=release_root
    )
    model_root = Path(binding["model_root"]).resolve(strict=True)
    model_backend = Path(binding["model_backend"]).resolve(strict=True)
    prompt_path = Path(binding["prompt_path"]).resolve(strict=True)
    sandbox_profile = Path(binding["sandbox_profile"]).resolve(strict=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_files = {
        str(row.get("path")): str(row.get("sha256"))
        for row in manifest.get("files", [])
        if isinstance(row, dict)
    }
    if (
        _sha256_file(manifest_path) != binding.get("release_manifest_sha256")
        or manifest.get("schema_version") != 1
        or manifest.get("immutable") is not True
        or manifest.get("release_id") != binding.get("release_id")
        or manifest.get("source_snapshot_sha256") != binding.get("release_snapshot_sha256")
        or manifest.get("release_sha256") != binding.get("release_snapshot_sha256")
        or _sha256_file(policy_path) != binding.get("resource_policy_sha256")
        or _sha256_file(runtime_path) != binding.get("python_runtime_sha256")
        or _sha256_file(model_backend) != binding.get("model_backend_sha256")
        or _sha256_file(prompt_path) != binding.get("prompt_sha256")
        or _sha256_file(sandbox_profile) != binding.get("sandbox_profile_sha256")
        or _tree_sha256(model_root) != binding.get("model_tree_sha256")
    ):
        raise CollectorError("release/runtime/model/prompt binding failed")
    if runtime_binding["kind"] == "manifest_bound_external":
        verifier = release_root / "scripts/v3_python_runtime_snapshot.py"
        verifier_relative = verifier.relative_to(release_root).as_posix()
        if manifest_files.get(verifier_relative) != _sha256_file(verifier):
            raise CollectorError("Python runtime verifier is not release-bound")
        verified = subprocess.run(
            [
                "/usr/bin/python3",
                "-I",
                "-S",
                str(verifier),
                "verify",
                "--manifest",
                runtime_binding["manifest"],
                "--expected-tree-sha256",
                runtime_binding["tree_sha256"],
                "--expected-python-runtime",
                runtime_binding["launcher_path"],
                "--expected-python-realpath",
                runtime_binding["realpath"],
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if verified.returncode != 0:
            raise CollectorError("external Python runtime tree verification failed")
    for argv in (
        *commands["v2_core"],
        *commands["v3_core"],
        commands["v2_model"],
        commands["v3_model"],
    ):
        executable = Path(argv[0]).resolve(strict=True)
        owned_path = Path(argv[1]).resolve(strict=True) if executable == runtime_path else executable
        relative = owned_path.relative_to(release_root).as_posix()
        if manifest_files.get(relative) != _sha256_file(owned_path):
            raise CollectorError("owned command is not manifest-bound")
    ports = plan.get("production_ports")
    if ports != list(REQUIRED_OBSERVED_PORTS):
        raise CollectorError("production port inventory is invalid")
    owner_markers = plan.get("v3_owner_markers")
    raw_pidfiles = plan.get("v3_pidfiles")
    launch_labels = plan.get("v3_launch_labels")
    if (
        not isinstance(owner_markers, list)
        or not owner_markers
        or not isinstance(raw_pidfiles, list)
        or not isinstance(launch_labels, list)
        or not launch_labels
    ):
        raise CollectorError("V3 owner markers/pidfiles/launch labels are incomplete")
    if str(release_root) not in owner_markers or str(runtime_path) not in owner_markers:
        raise CollectorError("V3 owner markers omit the release/runtime root")
    pidfiles = [Path(value).resolve() for value in raw_pidfiles]
    if any(not path.is_relative_to(workdir) for path in pidfiles):
        raise CollectorError("V3 pidfile is outside the owned workdir")
    backend.configure_scope(ports, owner_markers)
    preflight = backend.preflight(
        ports,
        owner_markers,
        pidfiles,
        launch_labels,
        REQUIRED_STOPPED_LABELS,
    )
    if (
        preflight.get("v2_fully_stopped") is not True
        or preflight.get("candidate_not_started_at_baseline") is not True
        or preflight.get("production_ingress_quiesced") is not True
        or preflight.get("v2_owner_pids") != []
        or preflight.get("production_port_owner_pids") != []
        or preflight.get("noncandidate_user_metal_processes") != []
        or preflight.get("v3_owner_pids_before_start") != []
        or preflight.get("required_stopped_launchd_labels")
        != list(REQUIRED_STOPPED_LABELS)
        or not isinstance(preflight.get("stopped_launchd_states"), list)
        or len(preflight["stopped_launchd_states"]) != len(REQUIRED_STOPPED_LABELS)
        or any(
            not isinstance(row, dict) or row.get("loaded") is not False
            for row in preflight.get("stopped_launchd_states", [])
        )
    ):
        raise CollectorError("V2-stopped exclusive preflight failed")
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(workdir / "home"),
        "TMPDIR": str(workdir / "tmp"),
        "PYTHONPATH": str(release_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "MAGI_V3_ISOLATED_RESOURCE_WINDOW": "1",
        "MAGI_V3_RELEASE_MANIFEST": str(manifest_path),
        "MAGI_V3_RELEASE_MANIFEST_SHA256": str(binding["release_manifest_sha256"]),
    }
    Path(env["HOME"]).mkdir()
    Path(env["TMPDIR"]).mkdir()
    production_env, production_composition_receipt = _prepare_v3_production_environment(
        workdir=workdir,
        release_root=release_root,
        runtime=runtime_path,
        manifest=manifest,
        manifest_sha256=str(binding["release_manifest_sha256"]),
        external_inputs=validated["external_inputs"],
    )
    env.update(production_env)
    env.update(
        {
            "MAGI_EXTERNAL_API_KEY": "magi-v3-isolated-resource-window-only",
            "MAGI_EXTERNAL_API_KEY_REQUIRED": "1",
            "INFERENCE_LOCAL_OLLAMA_BASE": "http://127.0.0.1:18080",
            "MAGI_V3_RESOURCE_MODEL_TREE_SHA256": str(binding["model_tree_sha256"]),
            "MAGI_V3_RESOURCE_HTTP_REQUEST_SHA256": str(
                validated["workload"]["http_request_sha256"]
            ),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_DATASETS_OFFLINE": "1",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    isolation_proof = backend.isolation_probe(sandbox_profile, workdir, runtime_path)
    baseline = backend.snapshot([])
    _assert_snapshot(baseline)
    backend.sleep(float(durations["negative_control_seconds"]))
    control = backend.snapshot([])
    _assert_snapshot(control)
    tolerance = int(thresholds["agx_drift_tolerance_bytes"])
    if (
        baseline.nonowned_model_processes
        or control.nonowned_model_processes
        or abs(control.agx_bytes - baseline.agx_bytes) > tolerance
    ):
        raise CollectorError("AGX negative control was not stable/exclusive")
    raw_observations: list[tuple[str, Snapshot]] = [
        ("baseline", baseline),
        ("negative_control", control),
    ]

    v2_handles: list[Handle] = []
    v3_handles: list[Handle] = []
    all_owned: list[Handle] = []
    arm_rows: list[dict[str, Any]] = []
    v3_peak_samples: list[Snapshot] = []

    def run_model_arm(
        arm_name: str,
        argv: Sequence[str],
        core_handles: Sequence[Handle],
    ) -> None:
        handle = backend.start(argv, workdir, env)
        all_owned.append(handle)
        started = backend.now_ns()
        samples: list[Snapshot] = []
        deadline = started + int(float(durations["model_timeout_seconds"]) * 1e9)
        while backend.poll(handle) is None and backend.now_ns() < deadline:
            sample = backend.snapshot([*core_handles, handle])
            _assert_snapshot(sample)
            samples.append(sample)
            backend.sleep(float(durations["model_sample_interval_seconds"]))
        if backend.poll(handle) is None:
            backend.stop([handle], 0.1)
            raise CollectorError("model arm timed out")
        returncode, stdout, _stderr = backend.finish(handle, 1)
        completed = backend.now_ns()
        markers = [line for line in stdout.splitlines() if line.startswith(MODEL_RESULT_PREFIX)]
        if returncode != 0 or len(markers) != 1 or not samples:
            raise CollectorError("model arm result/measurements are incomplete")
        payload = json.loads(markers[0][len(MODEL_RESULT_PREFIX) :])
        expected_arm = "v2-reference" if arm_name == "v2_reference" else "v3-candidate"
        if (
            payload.get("arm") != expected_arm
            or payload.get("request_sha256")
            != validated["workload"]["http_request_sha256"]
            or payload.get("transport") != "arm_owned_production_process_http"
            or type(payload.get("owned_model_server_pid")) is not int
            or payload["owned_model_server_pid"] <= 0
        ):
            raise CollectorError("model arm did not traverse its production HTTP composition")
        tokens = int(payload["generated_tokens"])
        seconds = float(payload["generation_seconds"])
        workload = validated["workload"]
        composition = workload["composition"]
        row = {
            "arm": arm_name,
            "generated_tokens": tokens,
            "generation_seconds": seconds,
            "tokens_per_second": tokens / seconds,
            "pid": handle.pid,
            "pgid": handle.pgid,
            "proc_start_abstime": handle.proc_start_abstime,
            "started_monotonic_ns": started,
            "completed_monotonic_ns": completed,
            "returncode": returncode,
            "timed_out": False,
            "process_group_gone": backend.group_gone(handle),
            "network_accessed": False,
            "production_state_accessed": False,
            "prompt_sha256": binding["prompt_sha256"],
            "model_tree_sha256": binding["model_tree_sha256"],
            "model_backend_sha256": binding["model_backend_sha256"],
            "python_runtime_sha256": binding["python_runtime_sha256"],
            "request_sha256": workload["request_sha256"],
            "http_request_sha256": workload["http_request_sha256"],
            "response_sha256": payload.get("response_sha256"),
            "owned_model_server_pid": payload["owned_model_server_pid"],
            "composition_sha256": composition["composition_sha256"],
            "transport": "arm_owned_production_process_http",
            "shared_direct_backend": False,
            "seatbelt_network_denied": True,
            "seatbelt_live_state_denied": True,
        }
        arm_rows.append(row)
        raw_observations.extend(
            (
                "v2_model" if arm_name == "v2_reference" else "v3_model",
                sample,
            )
            for sample in samples
        )
        if arm_name == "v3_candidate":
            v3_peak_samples.append(max(samples, key=lambda sample: sample.agx_bytes))

    try:
        v2_handles = [backend.start(argv, workdir, env) for argv in commands["v2_core"]]
        all_owned.extend(v2_handles)
        v2_samples, _v2_elapsed = _observe(
            backend,
            v2_handles,
            float(durations["v2_reference_seconds"]),
            float(durations["sample_interval_seconds"]),
        )
        raw_observations.extend(("v2_reference", sample) for sample in v2_samples)
        for _repeat in range(3):
            run_model_arm("v2_reference", commands["v2_model"], v2_handles)
        backend.stop(v2_handles, float(durations["stop_grace_seconds"]))
        v2_handles = []
        v3_handles = [backend.start(argv, workdir, env) for argv in commands["v3_core"]]
        all_owned.extend(v3_handles)
        idle_samples, idle_elapsed = _observe(
            backend,
            v3_handles,
            1800.0,
            float(durations["sample_interval_seconds"]),
        )
        raw_observations.extend(("v3_deep_idle", sample) for sample in idle_samples)
        if any(sample.model_processes for sample in idle_samples):
            raise CollectorError("deep-idle profile loaded a model")
        for _repeat in range(3):
            run_model_arm("v3_candidate", commands["v3_model"], v3_handles)
        backend.stop(v3_handles, float(durations["stop_grace_seconds"]))
        v3_handles = []
        returned_started = backend.now_ns()
        returned = backend.snapshot([])
        return_budget = float(thresholds["worker_metal_return_to_baseline_seconds"])
        while (
            abs(returned.agx_bytes - control.agx_bytes) > tolerance
            and (backend.now_ns() - returned_started) / 1e9 <= return_budget
        ):
            backend.sleep(1)
            returned = backend.snapshot([])
        return_seconds = (backend.now_ns() - returned_started) / 1e9
        _assert_snapshot(returned)
        raw_observations.append(("returned", returned))
    finally:
        backend.stop([*v2_handles, *v3_handles], float(durations["stop_grace_seconds"]))

    v2_footprint = max(sample.physical_footprint_mb for sample in v2_samples)
    idle_footprint = max(sample.physical_footprint_mb for sample in idle_samples)
    idle_cpu = [sample.cpu_percent for sample in idle_samples]
    peak_v3 = max(v3_peak_samples, key=lambda sample: sample.agx_bytes)
    v2_tps = [row["tokens_per_second"] for row in arm_rows if row["arm"] == "v2_reference"]
    v3_tps = [row["tokens_per_second"] for row in arm_rows if row["arm"] == "v3_candidate"]
    ratio = min(v3_tps) / max(v2_tps)
    raw_samples = [
        {
            "phase": phase,
            "system_agx_bytes": sample.agx_bytes,
            "source": AGX_SOURCE,
            "v2_owner_pids": [],
            "noncandidate_user_metal_processes": [],
        }
        for phase, sample in (
            ("baseline", baseline),
            ("negative_control", control),
            ("candidate_peak", peak_v3),
            ("returned", returned),
        )
    ]
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "status": "passed",
        "mode": "v2_fully_stopped_isolated_window",
        "release_binding": {
            key: binding[key]
            for key in (
                "release_id",
                "release_manifest_sha256",
                "release_snapshot_sha256",
                "python_runtime_sha256",
                "resource_policy_sha256",
                "model_tree_sha256",
                "model_backend_sha256",
                "prompt_sha256",
                "sandbox_profile_sha256",
            )
        },
        "external_inputs": dict(validated["external_inputs"]),
        "execution_binding": {
            "plan_sha256": validated["plan_sha256"],
            "approval_token_sha256": plan["approval_token_sha256"],
            "collector_source_sha256": _sha256_file(Path(__file__).resolve()),
            "owned_workdir_marker_sha256": _sha256_file(marker),
            "token_consumption_receipt_sha256": consumption_receipt_sha,
            "token_consumption_receipt": consumption_receipt,
            "plan_consumed_once": True,
            "outer_plan_sha256": validated["orchestration"]["outer_plan_sha256"],
            "outer_plan_semantic_sha256": validated["orchestration"][
                "outer_plan_semantic_sha256"
            ],
            "provisional_gate_sha256": validated["orchestration"][
                "provisional_gate_sha256"
            ],
            "provisional_gate_status": "provisional_16_of_19_passed",
            "provisional_gate_counts": {
                "required": 16,
                "passed": 16,
                "failed": 0,
                "missing": 0,
                "invalid": 0,
            },
            "formal_live_eligible_before_window": False,
            "production_composition_receipt": production_composition_receipt,
            "outer_owner_contract": validated["outer_owner"],
            "observed_listener_ports": list(ports),
            "outer_executor": validated["orchestration"]["caller"],
            "outer_executor_phase": validated["orchestration"]["phase"],
            "v2_restore_owner": validated["orchestration"]["v2_restore_owner"],
        },
        "thresholds": thresholds,
        "policy_binding": validated["policy"],
        "workload_binding": validated["workload"],
        "seatbelt_isolation": {
            "profile_raw": sandbox_profile.read_text(encoding="utf-8"),
            "sandbox_exec": "/usr/bin/sandbox-exec",
            "sandbox_applied_to_every_owned_command": True,
            **isolation_proof,
            "network_accessed": False,
            "live_state_accessed": False,
        },
        "preflight": preflight,
        "raw_host_samples": [
            _raw_sample_row(sample, phase, index)
            for index, (phase, sample) in enumerate(raw_observations, 1)
        ],
        "model_benchmark": {
            "arms": arm_rows,
            "same_model_prompt_backend_runtime": True,
            "same_corpus_model_request": True,
            "separate_arm_owned_production_compositions": True,
            "shared_direct_backend": False,
            "maximum_simultaneous_arms": 1,
            "minimum_v3_over_v2_ratio": ratio,
        },
        "resource_profiles": {
            "release_core_idle": {
                "max_footprint_mb": idle_footprint,
                "average_cpu_percent": sum(idle_cpu) / len(idle_cpu),
                "p95_cpu_percent": _p95(idle_cpu),
                "heavy_framework_imports": 0,
            },
            "total_magi_deep_idle": {
                "observation_seconds": idle_elapsed,
                "swapout_growth_mb": max(0.0, idle_samples[-1].swapouts_mb - idle_samples[0].swapouts_mb),
                "max_footprint_mb": idle_footprint,
                "loaded_models": 0,
                "python_service_processes": max(sample.python_processes for sample in idle_samples),
                "background_heavy_workers": 0,
            },
            "interactive_session": {
                "loaded_primary_models": 1,
                "background_heavy_workers": 0,
                "browser_workers": 0,
                "foreground_memory_reserve_mb": min(sample.available_mb for sample in v3_peak_samples),
                "attributed_metal_mb": max(0, peak_v3.agx_bytes - control.agx_bytes) / 1024**2,
            },
            "total_magi_active": {
                "matched_v2_application_plane_footprint_mb": v2_footprint,
                "v3_application_plane_footprint_mb": idle_footprint,
                "physical_footprint_mb": peak_v3.physical_footprint_mb,
                "attributed_metal_mb": max(0, peak_v3.agx_bytes - control.agx_bytes) / 1024**2,
                "matched_workload": True,
            },
        },
        "metal_attribution": {
            "source": AGX_SOURCE,
            "per_process_gpu_source": PER_PROCESS_GPU_SOURCE,
            "attribution_method": ATTRIBUTION_METHOD,
            "per_process_gpu_permission": True,
            "per_process_gpu_available": True,
            "per_process_metal_bytes_available": False,
            "system_wide_bytes_relabelled_as_per_process": False,
            "v2_fully_stopped_for_all_samples": True,
            "production_ingress_quiesced_for_all_samples": True,
            "noncandidate_user_metal_processes": [],
            "candidate_process_group_gone": all(
                backend.group_gone(handle) for handle in all_owned
            ),
            "negative_control_passed": True,
            "candidate_processes": sorted(
                {
                    row["owned_model_server_pid"]
                    for row in arm_rows
                    if row["arm"] == "v3_candidate"
                }
            ),
            "per_process_gpu_samples": [
                {
                    **gpu,
                    "raw_powermetrics_sha256": sample.raw_sources["powermetrics"][
                        "stdout_sha256"
                    ],
                }
                for sample in v3_peak_samples
                for gpu in sample.candidate_gpu_processes
            ],
            "negative_control_noncandidate_gpu_time_ns": sum(
                int(row["gpu_time_ns"])
                for row in control.noncandidate_gpu_processes
            ),
            "candidate_peak_noncandidate_gpu_time_ns": sum(
                int(row["gpu_time_ns"])
                for row in peak_v3.noncandidate_gpu_processes
            ),
            "noncandidate_gpu_time_drift_tolerance_ns": 5_000_000,
            "baseline_system_agx_bytes": baseline.agx_bytes,
            "negative_control_system_agx_bytes": control.agx_bytes,
            "candidate_peak_system_agx_bytes": peak_v3.agx_bytes,
            "returned_system_agx_bytes": returned.agx_bytes,
            "drift_tolerance_bytes": tolerance,
            "return_seconds": return_seconds,
            "raw_samples": raw_samples,
        },
    }
    report["evidence_sha256"] = sha256_json(report)
    verify_report(
        report,
        expected_release_id=str(binding["release_id"]),
        expected_release_manifest_sha256=str(binding["release_manifest_sha256"]),
    )
    return report


def load_plan(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if resolved.stat().st_mode & 0o222:
        raise CollectorError("resource-window plan must be read-only")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CollectorError("resource-window plan must be a JSON object")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        token = os.environ.get("MAGI_V3_RESOURCE_WINDOW_APPROVAL_TOKEN", "")
        if not token:
            raise CollectorError("resource-window approval token is missing")
        report = collect(load_plan(args.plan), token)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        if args.output.exists():
            raise CollectorError("resource-window output already exists")
        args.output.write_bytes(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n")
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps({"ok": True, "output": str(args.output), "evidence_sha256": report["evidence_sha256"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

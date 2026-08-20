"""Shared fail-closed ownership primitives for V3 production service roles."""

from __future__ import annotations

from . import fcntl_compat as fcntl
import hashlib
import json
import os
import platform
import re
import shlex
import signal
import socket
import stat
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Callable, Mapping, Protocol, Sequence

from .errors import CoreError
from .process_compat import process_group as _process_group

PRODUCTION_PORTS = (5002, 5003, 8088)
ROLES = frozenset({"gateway", "control", "supervisor"})
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ServiceRuntimeError(CoreError):
    """A production role cannot safely acquire or retain ownership."""


def _canonical_runtime_root() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "MAGI"
        / "runtime"
        / "MAGI_v3"
    ).resolve(strict=False)


@dataclass(frozen=True, slots=True)
class ProcessRecord:
    pid: int
    process_group: int
    command: str


class OwnershipProbe(Protocol):
    def assert_exclusive(self, release_root: Path) -> None: ...


_TRANSIENT_OWNERSHIP_PROBE_MARKERS = (
    "process ownership probe timed out",
    "listener ownership probe timed out",
)


def is_transient_ownership_probe_failure(exc: BaseException) -> bool:
    """Return true only for inconclusive read-only OS probe timeouts.

    A timeout is not evidence of a foreign owner.  macOS ``ps``/``lsof`` may
    temporarily stall while an SMB or File Provider mount reconnects.  Actual
    ownership conflicts and malformed evidence remain hard failures.
    """

    return isinstance(exc, ServiceRuntimeError) and any(
        marker in str(exc) for marker in _TRANSIENT_OWNERSHIP_PROBE_MARKERS
    )


class PeriodicOwnershipGuard:
    """Debounce transient periodic ownership-probe timeouts.

    Startup remains strict: callers must perform one successful
    ``assert_exclusive`` before constructing this guard.  During steady-state
    operation, a small bounded number of inconclusive OS read timeouts may be
    deferred.  A confirmed conflict, an overlong outage, or too many
    consecutive timeouts still fails closed.
    """

    def __init__(
        self,
        probe: OwnershipProbe,
        release_root: Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        transient_grace_sec: float = 120.0,
        max_consecutive_transient_failures: int = 3,
    ) -> None:
        if transient_grace_sec <= 0:
            raise ValueError("transient ownership grace must be positive")
        if max_consecutive_transient_failures < 1:
            raise ValueError("transient ownership failure limit must be positive")
        self.probe = probe
        self.release_root = release_root
        self.monotonic = monotonic
        self.transient_grace_sec = float(transient_grace_sec)
        self.max_consecutive_transient_failures = int(
            max_consecutive_transient_failures
        )
        self.last_success = self.monotonic()
        self.consecutive_transient_failures = 0

    def check(self) -> bool:
        """Return false when a transient timeout was safely deferred."""

        try:
            self.probe.assert_exclusive(self.release_root)
        except ServiceRuntimeError as exc:
            if not is_transient_ownership_probe_failure(exc):
                raise
            self.consecutive_transient_failures += 1
            if (
                self.consecutive_transient_failures
                > self.max_consecutive_transient_failures
                or self.monotonic() - self.last_success
                > self.transient_grace_sec
            ):
                raise
            return False
        self.last_success = self.monotonic()
        self.consecutive_transient_failures = 0
        return True


def _default_process_reader() -> tuple[ProcessRecord, ...]:
    command = ["/bin/ps", "-axo", "pid=,pgid=,command="]
    result: subprocess.CompletedProcess[str] | None = None
    last_timeout: subprocess.TimeoutExpired | None = None
    for timeout_seconds in (5, 8, 12):
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            break
        except subprocess.TimeoutExpired as exc:
            last_timeout = exc
    if result is None:
        raise ServiceRuntimeError(
            "process ownership probe timed out after 3 attempts"
        ) from last_timeout
    if result.returncode != 0:
        raise ServiceRuntimeError(f"process ownership probe failed: {(result.stderr or '').strip()}")
    records: list[ProcessRecord] = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)$", line)
        if match:
            records.append(ProcessRecord(int(match.group(1)), int(match.group(2)), match.group(3)))
    if not records:
        raise ServiceRuntimeError("process ownership probe returned no processes")
    return tuple(records)


def _default_listener_reader(
    port: int,
    candidate_pids: frozenset[int] | None = None,
) -> frozenset[int]:
    command = [
        "/usr/sbin/lsof",
        "-b",
        "-nP",
    ]
    if candidate_pids is not None:
        if not candidate_pids:
            return frozenset()
        # A global lsof walk touches the mount table and can block for several
        # seconds when an unrelated SMB/File Provider mount is reconnecting.
        # Ownership checks already have a fail-closed process snapshot, so
        # inspect only release-classified PIDs and never traverse unrelated
        # file descriptors during normal service operation.
        command.extend(("-a", "-p", ",".join(str(pid) for pid in sorted(candidate_pids))))
    command.extend((f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"))
    result: subprocess.CompletedProcess[str] | None = None
    last_timeout: subprocess.TimeoutExpired | None = None
    for _attempt in range(3):
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
            break
        except subprocess.TimeoutExpired as exc:
            last_timeout = exc
    if result is None:
        raise ServiceRuntimeError(
            f"listener ownership probe timed out for {port} after 3 attempts"
        ) from last_timeout
    if result.returncode not in {0, 1}:
        raise ServiceRuntimeError(
            f"listener ownership probe failed for {port}: {(result.stderr or '').strip()}"
        )
    return frozenset(
        int(line[1:]) for line in result.stdout.splitlines() if re.fullmatch(r"p\d+", line)
    )


def _default_port_is_available(port: int) -> bool:
    """Return whether IPv4 loopback can be exclusively bound right now.

    This is the fail-closed complement to the scoped lsof query.  If no
    release-classified PID owns the listener but the bind is unavailable, a
    foreign/unclassified process owns the production port.
    """

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", int(port)))
    except OSError as exc:
        if exc.errno in {48, 98}:  # macOS/Linux EADDRINUSE
            return False
        raise ServiceRuntimeError(
            f"loopback bind ownership probe failed for {port}: {exc}"
        ) from exc
    finally:
        probe.close()
    return True


def _default_v2_launchagent_loaded() -> bool:
    if platform.system() != "Darwin":
        return False
    result = subprocess.run(
        ["launchctl", "print", f"gui/{os.getuid()}/com.magi.daemon"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 113 and "Could not find service" in (result.stderr or ""):
        return False
    raise ServiceRuntimeError(
        f"V2 launchd ownership probe failed rc={result.returncode}: {(result.stderr or '').strip()}"
    )


def _default_role_owner_reader(release_root: Path) -> frozenset[int]:
    try:
        manifest = json.loads(
            (release_root / "release-manifest.json").read_text(encoding="utf-8")
        )
        release_id = manifest["release_id"]
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ServiceRuntimeError(
            f"same-release role ownership manifest is unavailable: {exc}"
        ) from exc
    if not isinstance(release_id, str) or not release_id:
        raise ServiceRuntimeError("same-release role identity is invalid")
    pid_root = _canonical_runtime_root() / "pids"
    owners: set[int] = set()
    for role in ROLES:
        pid_file = pid_root / f"{role}.pid"
        if not verify_role_owner(
            pid_file,
            role=role,
            release_id=release_id,
            release_root=release_root,
        ):
            continue
        try:
            payload = json.loads(pid_file.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ServiceRuntimeError(
                f"verified same-release role PID receipt became unreadable: {role}"
            ) from exc
        owners.add(pid)
    return frozenset(owners)


def _command_mentions_root(command: str, root: Path) -> bool:
    # Process command lines are untrusted observations.  Resolving every
    # absolute token can synchronously touch an unavailable SMB mount and
    # freeze all three ownership guards.  The release/V2 roots are already
    # canonicalized by the caller, so lexical containment is sufficient here
    # and deliberately performs no filesystem I/O.
    root_text = os.path.normpath(str(root))
    try:
        tokens = shlex.split(command)
    except ValueError:
        return root_text in command
    for token in tokens:
        if not os.path.isabs(token):
            continue
        try:
            if os.path.commonpath((os.path.normpath(token), root_text)) == root_text:
                return True
        except ValueError:
            continue
    return False


class DefaultOwnershipProbe:
    """Read-only V2/process/port fence; never kills or starts another release."""

    def __init__(
        self,
        *,
        v2_root: Path | None = None,
        process_reader: Callable[[], Sequence[ProcessRecord]] = _default_process_reader,
        listener_reader: Callable[[int], frozenset[int]] = _default_listener_reader,
        v2_launchagent_loaded: Callable[[], bool] = _default_v2_launchagent_loaded,
        current_pid: Callable[[], int] = os.getpid,
        role_owner_reader: Callable[[Path], frozenset[int]] = _default_role_owner_reader,
        port_is_available: Callable[[int], bool] = _default_port_is_available,
        active_release_marker: Path | None = None,
        require_active_release_marker: bool | None = None,
    ) -> None:
        self.v2_root = (
            v2_root
            or Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v2"
        ).expanduser().resolve(strict=False)
        self.process_reader = process_reader
        self.listener_reader = listener_reader
        self.v2_launchagent_loaded = v2_launchagent_loaded
        self.current_pid = current_pid
        self.role_owner_reader = role_owner_reader
        self.port_is_available = port_is_available
        self.active_release_marker = (
            active_release_marker
            or Path.home()
            / "Library"
            / "Application Support"
            / "MAGI"
            / "runtime"
            / "active-release.json"
        ).expanduser().resolve(strict=False)
        self.require_active_release_marker = (
            os.environ.get("MAGI_V3_REQUIRE_ACTIVE_MARKER", "0") == "1"
            if require_active_release_marker is None
            else require_active_release_marker
        )

    def _assert_active_marker(self, release_root: Path) -> None:
        if not self.require_active_release_marker:
            return
        marker = self.active_release_marker
        try:
            metadata = marker.lstat()
            payload = json.loads(marker.read_text(encoding="utf-8"))
            manifest = release_root / "release-manifest.json"
            release = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ServiceRuntimeError(f"active release marker is unavailable: {exc}") from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or not isinstance(payload, dict)
            or payload.get("schema") != "magi.v3.active-release/v1"
            or payload.get("schema_version") != 1
            or payload.get("release") != "v3"
            or payload.get("release_id") != release.get("release_id")
            or payload.get("release_root") != str(release_root)
            or payload.get("release_manifest_sha256")
            != hashlib.sha256(manifest.read_bytes()).hexdigest()
            or not isinstance(payload.get("transaction_id"), str)
            or not payload["transaction_id"]
        ):
            raise ServiceRuntimeError("active release marker does not commit this V3 release")

    def assert_exclusive(self, release_root: Path) -> None:
        root = release_root.resolve(strict=True)
        self._assert_active_marker(root)
        processes = tuple(self.process_reader())
        by_pid = {record.pid: record for record in processes}
        if self.v2_launchagent_loaded():
            raise ServiceRuntimeError("V2 launchagent com.magi.daemon is still loaded")
        v2 = [record.pid for record in processes if _command_mentions_root(record.command, self.v2_root)]
        if v2:
            raise ServiceRuntimeError(f"V2 release process is still active: {sorted(v2)}")
        release_process_groups = frozenset(
            record.process_group
            for record in processes
            if _command_mentions_root(record.command, root)
        )
        same_release_role_owners = self.role_owner_reader(root)
        allowed_listener_pids = frozenset(
            record.pid
            for record in processes
            if (
                record.pid == self.current_pid()
                or record.pid in same_release_role_owners
                or _command_mentions_root(record.command, root)
                or record.process_group in release_process_groups
            )
        )
        for port in PRODUCTION_PORTS:
            if self.listener_reader is _default_listener_reader:
                scoped_owners = _default_listener_reader(port, allowed_listener_pids)
                if scoped_owners:
                    continue
                if not self.port_is_available(port):
                    raise ServiceRuntimeError(
                        f"production port {port} has a foreign or unclassified listener"
                    )
                continue
            for pid in self.listener_reader(port):
                record = by_pid.get(pid)
                if record is None or (
                    pid != self.current_pid()
                    and pid not in same_release_role_owners
                    and not _command_mentions_root(record.command, root)
                    and record.process_group not in release_process_groups
                ):
                    raise ServiceRuntimeError(
                        f"production port {port} has a foreign or unclassified listener pid={pid}"
                    )


@dataclass(frozen=True, slots=True)
class ServiceIdentity:
    role: str
    release_id: str
    release_root: Path
    release_manifest: Path
    release_manifest_sha256: str
    runtime_root: Path
    pid_file: Path
    executable_path: Path | None = None
    release_files: Mapping[str, str] = field(default_factory=dict)
    env_file: Path | None = None
    env_file_sha256: str | None = None

    @classmethod
    def from_environment(
        cls,
        role: str,
        environ: Mapping[str, str] | None = None,
        *,
        canonical_runtime_root: Path | None = None,
    ) -> "ServiceIdentity":
        if role not in ROLES:
            raise ServiceRuntimeError(f"unsupported V3 role: {role}")
        env = os.environ if environ is None else environ

        def required(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                raise ServiceRuntimeError(f"required service environment is missing: {name}")
            return value

        if required("MAGI_V3_ROLE") != role:
            raise ServiceRuntimeError("launch role does not match MAGI_V3_ROLE")
        release_id = required("MAGI_V3_RELEASE_ID")
        manifest = Path(required("MAGI_V3_RELEASE_MANIFEST")).expanduser()
        digest = required("MAGI_V3_RELEASE_MANIFEST_SHA256")
        pid_file = Path(required("MAGI_V3_PID_FILE")).expanduser()
        state_dir = Path(required("MAGI_V3_STATE_DIR")).expanduser()
        executable = Path(required("MAGI_V3_EXECUTABLE_PATH")).expanduser()
        env_file = Path(required("MAGI_ENV_FILE")).expanduser()
        env_file_digest = required("MAGI_ENV_FILE_SHA256")
        if not SHA256_RE.fullmatch(digest):
            raise ServiceRuntimeError("release manifest SHA-256 is invalid")
        if not SHA256_RE.fullmatch(env_file_digest):
            raise ServiceRuntimeError("environment file SHA-256 is invalid")
        if any(
            not path.is_absolute()
            for path in (manifest, pid_file, state_dir, executable, env_file)
        ):
            raise ServiceRuntimeError(
                "release manifest, executable, environment, state, and PID paths must be absolute"
            )
        manifest = manifest.resolve(strict=True)
        release_root = manifest.parent
        if hashlib.sha256(manifest.read_bytes()).hexdigest() != digest:
            raise ServiceRuntimeError("release manifest SHA-256 mismatch")
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ServiceRuntimeError(f"release manifest is unreadable: {exc}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("immutable") is not True
            or payload.get("release_id") != release_id
        ):
            raise ServiceRuntimeError("release manifest identity mismatch")
        rows = payload.get("files")
        if not isinstance(rows, list) or not rows:
            raise ServiceRuntimeError("release manifest has no immutable file inventory")
        release_files: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ServiceRuntimeError("release manifest file inventory is invalid")
            relative = row.get("path")
            file_digest = row.get("sha256")
            if (
                not isinstance(relative, str)
                or not relative
                or Path(relative).is_absolute()
                or ".." in Path(relative).parts
                or not isinstance(file_digest, str)
                or not SHA256_RE.fullmatch(file_digest)
                or relative in release_files
            ):
                raise ServiceRuntimeError("release manifest file inventory is invalid")
            release_files[relative] = file_digest
        runtime_root = state_dir.resolve(strict=False).parent.parent
        canonical_runtime = (
            canonical_runtime_root or _canonical_runtime_root()
        ).expanduser().resolve(strict=False)
        if runtime_root != canonical_runtime:
            raise ServiceRuntimeError(
                f"runtime root must equal canonical V3 runtime: {canonical_runtime}"
            )
        expected_pid = runtime_root / "pids" / f"{role}.pid"
        if pid_file.resolve(strict=False) != expected_pid:
            raise ServiceRuntimeError("role PID file is outside the bound runtime layout")
        executable = executable.resolve(strict=True)
        env_file = verify_environment_file(env_file, env_file_digest)
        result = cls(
            role=role,
            release_id=release_id,
            release_root=release_root,
            release_manifest=manifest,
            release_manifest_sha256=digest,
            runtime_root=runtime_root,
            pid_file=expected_pid,
            executable_path=executable,
            release_files=release_files,
            env_file=env_file,
            env_file_sha256=env_file_digest,
        )
        verify_release_member(result, executable, description="service executable")
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ServiceRuntimeError("service executable is not executable")
        if str(env.get("MAGI_V3_EXTERNAL_INPUT_CONTRACT") or "").strip() == "1":
            from .external_inputs import ExternalInputError, verify_sealed_runtime_inputs

            try:
                verify_sealed_runtime_inputs(release_root, env)
            except ExternalInputError as exc:
                raise ServiceRuntimeError(
                    f"sealed runtime input verification failed: {exc}"
                ) from exc
        return result


def verify_environment_file(path: Path, expected_sha256: str) -> Path:
    """Verify the separately managed, immutable-at-start credential input."""

    raw = path.expanduser()
    if not raw.is_absolute():
        raise ServiceRuntimeError("environment file path must be absolute")
    if not SHA256_RE.fullmatch(expected_sha256):
        raise ServiceRuntimeError("environment file SHA-256 is invalid")
    try:
        if raw.is_symlink():
            raise ServiceRuntimeError("environment file must not be a symlink")
        descriptor = os.open(raw, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except ServiceRuntimeError:
        raise
    except OSError as exc:
        raise ServiceRuntimeError(f"environment file is unavailable: {exc}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ServiceRuntimeError("environment file must be a regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ServiceRuntimeError("environment file permissions must be exactly 0600")
        digest = hashlib.sha256()
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest() != expected_sha256:
            raise ServiceRuntimeError("environment file SHA-256 mismatch")
    finally:
        os.close(descriptor)
    try:
        return raw.resolve(strict=True)
    except OSError as exc:
        raise ServiceRuntimeError(f"environment file is unavailable: {exc}") from exc


def verify_release_member(
    identity: ServiceIdentity,
    path: Path,
    *,
    description: str,
) -> Path:
    if path.expanduser().is_symlink():
        raise ServiceRuntimeError(f"{description} must not be a symlink")
    try:
        resolved = path.expanduser().resolve(strict=True)
        relative = resolved.relative_to(identity.release_root).as_posix()
    except (OSError, ValueError) as exc:
        raise ServiceRuntimeError(f"{description} must be inside the immutable release") from exc
    expected = identity.release_files.get(relative)
    if expected is None:
        raise ServiceRuntimeError(f"{description} is absent from the release manifest: {relative}")
    if hashlib.sha256(resolved.read_bytes()).hexdigest() != expected:
        raise ServiceRuntimeError(f"{description} hash does not match the release manifest: {relative}")
    return resolved


class RoleLease:
    """Role-local flock plus atomic PID/PGID identity record."""

    def __init__(
        self,
        identity: ServiceIdentity,
        *,
        pid: Callable[[], int] = os.getpid,
        process_group: Callable[[int], int] = _process_group,
    ) -> None:
        self.identity = identity
        self._pid = pid
        self._process_group = process_group
        self.lock_path = identity.pid_file.with_suffix(".lock")
        self._lock_fd: int | None = None

    @property
    def acquired(self) -> bool:
        return self._lock_fd is not None

    def acquire(self) -> None:
        if self.acquired:
            return
        self.identity.pid_file.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise ServiceRuntimeError(f"V3 role is already active: {self.identity.role}") from exc
        owner_pid = self._pid()
        payload = {
            "schema_version": 1,
            "role": self.identity.role,
            "release_id": self.identity.release_id,
            "release_root": str(self.identity.release_root),
            "pid": owner_pid,
            "process_group": self._process_group(owner_pid),
        }
        temporary = self.identity.pid_file.with_name(
            f".{self.identity.pid_file.name}.tmp-{owner_pid}"
        )
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.identity.pid_file)
            self._lock_fd = descriptor
        except BaseException:
            temporary.unlink(missing_ok=True)
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            raise

    def release(self) -> None:
        if self._lock_fd is None:
            return
        descriptor, self._lock_fd = self._lock_fd, None
        try:
            try:
                payload = json.loads(self.identity.pid_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if payload.get("pid") == self._pid() and payload.get("role") == self.identity.role:
                self.identity.pid_file.unlink(missing_ok=True)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def __enter__(self) -> "RoleLease":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def verify_role_owner(
    pid_file: Path,
    *,
    role: str,
    release_id: str,
    release_root: Path,
    pid_alive: Callable[[int], bool] | None = None,
    process_group: Callable[[int], int] = _process_group,
) -> bool:
    """Verify a same-release role holds its lock and PID/PGID still match."""

    alive = pid_alive or _pid_alive
    lock_path = pid_file.with_suffix(".lock")
    try:
        descriptor = os.open(lock_path, os.O_RDONLY)
    except OSError:
        return False
    locked = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            locked = True
        else:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
    if not locked:
        return False
    try:
        payload = json.loads(pid_file.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        pgid = int(payload["process_group"])
        return bool(
            payload.get("role") == role
            and payload.get("release_id") == release_id
            and Path(payload.get("release_root", "")).resolve(strict=False)
            == release_root.resolve(strict=True)
            and alive(pid)
            and process_group(pid) == pgid
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


SignalRegistrar = Callable[[int, Callable[[int, FrameType | None], None]], object]


def install_shutdown_handlers(
    stop: Callable[[], None],
    *,
    register: SignalRegistrar = signal.signal,
) -> Callable[[], None]:
    previous: dict[int, object] = {}

    def handler(_signum: int, _frame: FrameType | None) -> None:
        stop()

    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = register(signum, handler)

    def restore() -> None:
        for signum, old_handler in previous.items():
            register(signum, old_handler)  # type: ignore[arg-type]

    return restore

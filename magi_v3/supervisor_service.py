"""Production V3 supervisor role for manifest-owned non-HTTP child services."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol

from .errors import ConfigurationError
from .process_compat import group_exists as _portable_group_exists
from .process_compat import process_group as _process_group
from .process_compat import signal_group as _signal_group
from .service_manifest import (
    ServiceDefinition,
    ServiceManifest,
    load_bound_service_manifest,
    load_service_manifest,
)
from .service_runtime import (
    DefaultOwnershipProbe,
    OwnershipProbe,
    PeriodicOwnershipGuard,
    RoleLease,
    ServiceIdentity,
    ServiceRuntimeError,
    SignalRegistrar,
    install_shutdown_handlers,
    verify_environment_file,
    verify_release_member,
    verify_role_owner,
)


class ProcessLike(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...


ProcessFactory = Callable[..., ProcessLike]
DotenvReader = Callable[..., Mapping[str, str | None]]


@dataclass(slots=True)
class ManagedChild:
    service: ServiceDefinition
    process: ProcessLike
    process_group: int
    pid_file: Path
    started_monotonic: float


def _group_exists(process_group: int) -> bool:
    return _portable_group_exists(process_group)


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


def _process_argv(pid: int) -> tuple[str, ...]:
    """Return a process argv without parsing the lossy, space-delimited ps output."""

    try:
        import psutil

        return tuple(psutil.Process(pid).cmdline())
    except (ImportError, OSError, ValueError):
        return ()
    except Exception as exc:  # pragma: no cover - psutil's platform errors vary
        if exc.__class__.__module__.startswith("psutil"):
            return ()
        raise


def load_supervisor_environment(
    identity: ServiceIdentity,
    *,
    base_environ: Mapping[str, str] | None = None,
    dotenv_reader: DotenvReader | None = None,
) -> dict[str, str]:
    """Build child process environment from the hash-bound deployment dotenv."""

    if identity.env_file is None or identity.env_file_sha256 is None:
        raise ServiceRuntimeError("supervisor identity has no bound environment file")
    env_file = verify_environment_file(identity.env_file, identity.env_file_sha256)
    if dotenv_reader is None:
        try:
            from dotenv import dotenv_values
        except ImportError as exc:
            raise ServiceRuntimeError("python-dotenv is unavailable") from exc
        dotenv_reader = dotenv_values
    try:
        loaded = dotenv_reader(dotenv_path=env_file, encoding="utf-8", interpolate=True)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ServiceRuntimeError(f"environment file could not be loaded: {exc}") from exc
    if not isinstance(loaded, Mapping):
        raise ServiceRuntimeError("dotenv loader returned an invalid environment")
    child_environment: dict[str, str] = {}
    for key, value in loaded.items():
        if not isinstance(key, str) or not key or "\x00" in key:
            raise ServiceRuntimeError("environment file contains an invalid variable name")
        if value is None:
            continue
        if not isinstance(value, str) or "\x00" in value:
            raise ServiceRuntimeError(f"environment variable {key} has an invalid value")
        child_environment[key] = value
    # Match load_dotenv's safe default: launchd's explicit release bindings win.
    child_environment.update(os.environ if base_environ is None else base_environ)
    return child_environment


class ManifestProcessSupervisor:
    """Start and fence only process services assigned to the supervisor role."""

    def __init__(
        self,
        *,
        manifest: ServiceManifest,
        identity: ServiceIdentity,
        python_executable: Path,
        process_factory: ProcessFactory = subprocess.Popen,
        process_group: Callable[[int], int] = _process_group,
        signal_process: Callable[[int, int], None] = os.kill,
        signal_group: Callable[[int, int], None] = _signal_group,
        group_exists: Callable[[int], bool] = _group_exists,
        pid_alive: Callable[[int], bool] = _pid_alive,
        process_argv: Callable[[int], tuple[str, ...]] = _process_argv,
        monotonic: Callable[[], float] = time.monotonic,
        environ: Mapping[str, str] | None = None,
        restart_base_sec: float = 1.0,
        restart_max_sec: float = 60.0,
    ) -> None:
        services = manifest.for_role("supervisor")
        if not services or any(service.kind != "process" or service.port is not None for service in services):
            raise ServiceRuntimeError("supervisor role may own only non-HTTP process services")
        if identity.role != "supervisor":
            raise ServiceRuntimeError("supervisor service received a different role identity")
        executable = verify_release_member(
            identity,
            python_executable,
            description="supervisor Python",
        )
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ServiceRuntimeError("supervisor release Python is not executable")
        self.manifest = manifest
        self.identity = identity
        self.python_executable = executable
        self.process_factory = process_factory
        self.process_group = process_group
        self.signal_process = signal_process
        self.signal_group = signal_group
        self.group_exists = group_exists
        self.pid_alive = pid_alive
        self.process_argv = process_argv
        self.monotonic = monotonic
        self.environ = dict(os.environ if environ is None else environ)
        self.restart_base_sec = restart_base_sec
        self.restart_max_sec = restart_max_sec
        self.children: dict[str, ManagedChild] = {}
        self._restart_due: dict[str, float] = {}
        self._failures: dict[str, int] = {}

    def _restart_delay(self, failures: int) -> float:
        exponent = min(max(0, failures - 1), 16)
        return min(self.restart_max_sec, self.restart_base_sec * (2**exponent))

    def _argv(self, service: ServiceDefinition) -> tuple[str, ...]:
        if not service.argv or service.argv[0] != "{python}":
            raise ServiceRuntimeError(f"service {service.service_id} lacks release Python argv")
        script_relative = Path(service.argv[1])
        script = self.identity.release_root / script_relative
        if not script.is_file():
            raise ServiceRuntimeError(
                f"required supervisor entrypoint is missing: {script_relative.as_posix()}"
            )
        script = verify_release_member(
            self.identity,
            script,
            description=f"supervisor entrypoint {service.service_id}",
        )
        if script.name in self.manifest.forbidden_release_processes:
            raise ServiceRuntimeError(f"forbidden release process requested: {script.name}")
        return (str(self.python_executable), str(script), *service.argv[2:])

    def _pid_file(self, service: ServiceDefinition) -> Path:
        return self.identity.runtime_root / "pids" / f"service-{service.service_id}.pid"

    def _reclaim_bound_orphan(
        self,
        service: ServiceDefinition,
        path: Path,
        payload: Mapping[str, object],
        *,
        pid: int,
        pgid: int,
    ) -> None:
        expected_script = str((self.identity.release_root / Path(service.argv[1])).resolve())
        expected = {
            "schema_version": 1,
            "service_id": service.service_id,
            "release_id": self.identity.release_id,
            "release_root": str(self.identity.release_root),
            "pid": pid,
            "process_group": pgid,
        }
        if any(payload.get(key) != value for key, value in expected.items()):
            raise ServiceRuntimeError(
                f"refusing unbound orphan child pid={pid} pgid={pgid}: {path.name}"
            )
        try:
            current_group = self.process_group(pid)
        except ProcessLookupError as exc:
            raise ServiceRuntimeError(
                f"refusing leaderless orphan process group pgid={pgid}: {path.name}"
            ) from exc
        if pgid != pid or current_group != pgid:
            raise ServiceRuntimeError(
                f"refusing changed orphan process group pid={pid} pgid={current_group}: {path.name}"
            )
        if expected_script not in self.process_argv(pid):
            raise ServiceRuntimeError(
                f"refusing orphan child with mismatched command pid={pid}: {path.name}"
            )

        # launchctl kickstart -k can terminate the supervisor before its final
        # two children are reaped.  A cryptographically bound same-release PID
        # receipt plus exact argv and process-group ownership is sufficient to
        # recycle only that orphan.  Anything ambiguous remains fail-closed.
        try:
            self.signal_group(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        for _ in range(50):
            if not self.group_exists(pgid):
                break
            time.sleep(0.02)
        if self.group_exists(pgid):
            try:
                self.signal_group(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for _ in range(250):
                if not self.group_exists(pgid):
                    break
                time.sleep(0.02)
        if self.group_exists(pgid):
            raise ServiceRuntimeError(
                f"same-release orphan process group was not fenced: {path.name}"
            )
        path.unlink(missing_ok=True)

    def _assert_pid_slot_available(self, service: ServiceDefinition, path: Path) -> None:
        if not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            pid = int(payload["pid"])
            pgid = int(payload["process_group"])
        except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ServiceRuntimeError(f"managed child PID file is invalid: {path}") from exc
        pid_is_alive = self.pid_alive(pid)
        group_is_alive = self.group_exists(pgid)
        if pid_is_alive or group_is_alive:
            if not (pid_is_alive and group_is_alive):
                raise ServiceRuntimeError(
                    f"refusing incomplete orphan child pid={pid} pgid={pgid}: {path.name}"
                )
            self._reclaim_bound_orphan(
                service,
                path,
                payload,
                pid=pid,
                pgid=pgid,
            )
            return
        path.unlink()

    def _write_pid_file(self, child: ManagedChild) -> None:
        child.pid_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "service_id": child.service.service_id,
            "release_id": self.identity.release_id,
            "release_root": str(self.identity.release_root),
            "pid": child.process.pid,
            "process_group": child.process_group,
        }
        temporary = child.pid_file.with_name(f".{child.pid_file.name}.tmp-{child.process.pid}")
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, child.pid_file)

    def _remove_pid_file(self, child: ManagedChild) -> None:
        try:
            payload = json.loads(child.pid_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("pid") == child.process.pid and payload.get("service_id") == child.service.service_id:
            child.pid_file.unlink(missing_ok=True)

    def _start(self, service: ServiceDefinition) -> ManagedChild:
        pid_file = self._pid_file(service)
        self._assert_pid_slot_available(service, pid_file)
        child_environ = dict(self.environ)
        # Scheduling is owned by the V3 legacy-background component.  Disable
        # Discord's embedded copy so one release can never have two schedulers.
        if service.service_id == "discord":
            child_environ["MAGI_INTERNAL_CRON_ENABLED"] = "0"
        process = self.process_factory(
            list(self._argv(service)),
            cwd=str(self.identity.release_root),
            env=child_environ,
            stdin=subprocess.DEVNULL,
            stdout=None,
            stderr=None,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
        process_group = self.process_group(process.pid)
        if process_group != process.pid:
            try:
                self.signal_process(process.pid, signal.SIGTERM)
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                self.signal_process(process.pid, signal.SIGKILL)
                process.wait(timeout=5.0)
            raise ServiceRuntimeError(
                f"managed child did not create an owned process group: {service.service_id}"
            )
        child = ManagedChild(service, process, process_group, pid_file, self.monotonic())
        try:
            self._write_pid_file(child)
        except BaseException:
            self._terminate(child, grace_sec=0.2)
            raise
        self.children[service.service_id] = child
        return child

    def start_all(self) -> None:
        try:
            for service in self.manifest.for_role("supervisor"):
                self._start(service)
        except BaseException:
            self.shutdown(grace_sec=0.2)
            raise

    def tick(self) -> None:
        now = self.monotonic()
        for service_id, child in tuple(self.children.items()):
            if child.process.poll() is None:
                if now - child.started_monotonic >= 60:
                    self._failures[service_id] = 0
                continue
            self._terminate(child, grace_sec=0.2)
            self.children.pop(service_id, None)
            failures = self._failures.get(service_id, 0) + 1
            self._failures[service_id] = failures
            self._restart_due[service_id] = now + self._restart_delay(failures)
        for service_id, due_at in tuple(self._restart_due.items()):
            if now < due_at:
                continue
            service = self.manifest.service(service_id)
            try:
                self._start(service)
            except (OSError, ServiceRuntimeError):
                failures = self._failures.get(service_id, 0) + 1
                self._failures[service_id] = failures
                self._restart_due[service_id] = now + self._restart_delay(failures)
            else:
                self._restart_due.pop(service_id, None)

    def _signal_owned_group(self, child: ManagedChild, signum: int) -> None:
        try:
            current_group = self.process_group(child.process.pid)
        except ProcessLookupError:
            current_group = child.process_group
        if current_group != child.process_group:
            raise ServiceRuntimeError(
                f"refusing to signal changed process group for {child.service.service_id}: "
                f"expected={child.process_group} observed={current_group}"
            )
        try:
            self.signal_group(child.process_group, signum)
        except ProcessLookupError:
            return

    def _terminate(self, child: ManagedChild, *, grace_sec: float) -> None:
        if child.process.poll() is None or self.group_exists(child.process_group):
            self._signal_owned_group(child, signal.SIGTERM)
            try:
                child.process.wait(timeout=max(0.0, grace_sec))
            except subprocess.TimeoutExpired:
                self._signal_owned_group(child, signal.SIGKILL)
                child.process.wait(timeout=5.0)
        deadline = self.monotonic() + max(0.0, grace_sec)
        while self.group_exists(child.process_group) and self.monotonic() < deadline:
            time.sleep(0.01)
        if self.group_exists(child.process_group):
            self._signal_owned_group(child, signal.SIGKILL)
            deadline = self.monotonic() + 5.0
            while self.group_exists(child.process_group) and self.monotonic() < deadline:
                time.sleep(0.01)
        if self.group_exists(child.process_group):
            raise ServiceRuntimeError(
                f"managed child process group was not fenced: {child.service.service_id}"
            )
        self._remove_pid_file(child)

    def shutdown(self, *, grace_sec: float = 5.0) -> None:
        errors: list[BaseException] = []
        for service_id, child in tuple(self.children.items()):
            try:
                self._terminate(child, grace_sec=grace_sec)
            except BaseException as exc:
                errors.append(exc)
            else:
                self.children.pop(service_id, None)
        self._restart_due.clear()
        if errors:
            raise ServiceRuntimeError(f"one or more managed child groups could not be fenced: {errors[0]}")


class SupervisorService:
    def __init__(
        self,
        *,
        identity: ServiceIdentity,
        role_lease: RoleLease,
        ownership_probe: OwnershipProbe,
        process_supervisor: ManifestProcessSupervisor,
        control_owner: Callable[[], bool],
        signal_registrar: SignalRegistrar | None = None,
        poll_interval_sec: float = 0.25,
        ownership_probe_interval_sec: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.identity = identity
        self.role_lease = role_lease
        self.ownership_probe = ownership_probe
        self.process_supervisor = process_supervisor
        self.control_owner = control_owner
        self.signal_registrar = signal_registrar
        self.poll_interval_sec = poll_interval_sec
        self.ownership_probe_interval_sec = ownership_probe_interval_sec
        self.monotonic = monotonic
        self.stop_event = threading.Event()

    def stop(self) -> None:
        self.stop_event.set()

    def run(self) -> int:
        self.ownership_probe.assert_exclusive(self.identity.release_root)
        periodic_ownership = PeriodicOwnershipGuard(
            self.ownership_probe,
            self.identity.release_root,
            monotonic=self.monotonic,
        )
        if not self.control_owner():
            raise ServiceRuntimeError("same-release control owner is not active")
        self.role_lease.acquire()
        def restore_signals() -> None:
            return

        try:
            restore_signals = install_shutdown_handlers(
                self.stop,
                **({"register": self.signal_registrar} if self.signal_registrar else {}),
            )
            self.process_supervisor.start_all()
            next_ownership_probe = self.monotonic() + self.ownership_probe_interval_sec
            while not self.stop_event.wait(self.poll_interval_sec):
                if self.monotonic() >= next_ownership_probe:
                    if not periodic_ownership.check():
                        logging.getLogger(__name__).warning(
                            "transient periodic ownership probe timeout deferred; "
                            "managed children remain fenced and will be rechecked"
                        )
                    next_ownership_probe = self.monotonic() + self.ownership_probe_interval_sec
                if not self.control_owner():
                    raise ServiceRuntimeError("same-release control owner was lost")
                self.process_supervisor.tick()
            return 0
        finally:
            try:
                self.process_supervisor.shutdown()
            finally:
                restore_signals()
                self.role_lease.release()


def _identity_executable(identity: ServiceIdentity) -> Path:
    if identity.executable_path is None:
        raise ServiceRuntimeError("MAGI_V3_EXECUTABLE_PATH is required for supervisor startup")
    return identity.executable_path


def build_supervisor_service(
    identity: ServiceIdentity,
    *,
    manifest_path: Path | None = None,
    ownership_probe: OwnershipProbe | None = None,
    python_executable: Path | None = None,
    environ: Mapping[str, str] | None = None,
    dotenv_reader: DotenvReader | None = None,
) -> SupervisorService:
    if manifest_path is None:
        _, manifest = load_bound_service_manifest(
            identity,
            os.environ if environ is None else environ,
        )
    else:
        manifest = load_service_manifest(manifest_path)
    process_supervisor = ManifestProcessSupervisor(
        manifest=manifest,
        identity=identity,
        python_executable=python_executable or _identity_executable(identity),
        environ=load_supervisor_environment(
            identity,
            base_environ=environ,
            dotenv_reader=dotenv_reader,
        ),
    )
    control_pid = identity.runtime_root / "pids" / "control.pid"
    return SupervisorService(
        identity=identity,
        role_lease=RoleLease(identity),
        ownership_probe=ownership_probe or DefaultOwnershipProbe(),
        process_supervisor=process_supervisor,
        control_owner=lambda: verify_role_owner(
            control_pid,
            role="control",
            release_id=identity.release_id,
            release_root=identity.release_root,
        ),
    )


def main() -> int:
    try:
        identity = ServiceIdentity.from_environment("supervisor")
        return build_supervisor_service(identity).run()
    except (ConfigurationError, OSError, ServiceRuntimeError) as exc:
        print(
            json.dumps({"status": "blocked", "role": "supervisor", "error": str(exc)}),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

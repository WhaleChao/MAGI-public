"""Production WSGI gateway for the V3 5002/5003 ingress role.

Importing this module only defines composition and lifecycle primitives.  App
factories, Waitress, role locks, files, threads, and sockets are all deferred
until explicit construction/startup.
"""

from __future__ import annotations

import importlib
import hashlib
import json
import os
import re
import signal
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any, Callable, Iterable, Mapping, Protocol

from .config import load_settings
from .errors import ConfigurationError, CoreError
from .runtime import CoreRuntime
from .service_manifest import (
    ServiceDefinition,
    load_bound_service_manifest,
    load_service_manifest,
)
from .service_runtime import (
    DefaultOwnershipProbe,
    OwnershipProbe,
    RoleLease,
    ServiceIdentity,
    install_shutdown_handlers,
    verify_role_owner,
)

_RELEASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GATEWAY_DOMAINS = frozenset({"production_ingress", "webhook_consumers", "external_api"})
_CONTROL_DOMAINS = frozenset({"control_plane", "scheduler", "durable_ledger_writer"})

WSGIApp = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]
AppFactory = Callable[[], WSGIApp]


class GatewayConfigurationError(ConfigurationError):
    """The gateway cannot safely claim its declared production ingress."""


class GatewayRuntimeError(CoreError):
    """A configured gateway server failed while serving."""


class RoleGuard(Protocol):
    """Role-local ownership primitive supplied by ``service_runtime``."""

    @property
    def acquired(self) -> bool: ...

    def acquire(self) -> None: ...

    def release(self) -> None: ...


class ServerHandle(Protocol):
    """Minimal stoppable server contract used by the gateway lifecycle."""

    def run(self) -> None: ...

    def close(self) -> None: ...


ServerFactory = Callable[[WSGIApp, ServiceDefinition, "GatewayConfig"], ServerHandle]
RoleGuardFactory = Callable[[Mapping[str, str], "ReleaseOwnership"], RoleGuard]
ControlOwner = Callable[[], bool]
ControlOwnerFactory = Callable[[Mapping[str, str], "ReleaseOwnership"], ControlOwner]
DependencyProbe = Callable[[], tuple[bool, dict[str, object]]]
DependencyProbeFactory = Callable[
    [Mapping[str, str], "ReleaseOwnership", Path], DependencyProbe
]


@dataclass(frozen=True, slots=True)
class ReleaseOwnership:
    release_id: str
    manifest_path: Path
    release_manifest_sha256: str
    gateway_binding: Mapping[str, Any]
    control_binding: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    host: str = "127.0.0.1"
    threads: int = 8
    channel_timeout: int = 120
    shutdown_grace_sec: float = 10.0
    ownership_check_interval_sec: float = 5.0
    ownership_loss_confirmations: int = 3

    @classmethod
    def from_environ(cls, environ: Mapping[str, str]) -> "GatewayConfig":
        host = environ.get("MAGI_V3_GATEWAY_HOST", "127.0.0.1").strip()
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise GatewayConfigurationError("gateway host must remain loopback")
        return cls(
            host=host,
            threads=_bounded_int(environ, "MAGI_V3_GATEWAY_THREADS", 8, 2, 32),
            channel_timeout=_bounded_int(
                environ, "MAGI_V3_GATEWAY_CHANNEL_TIMEOUT", 120, 30, 3600
            ),
            shutdown_grace_sec=_bounded_float(
                environ, "MAGI_V3_GATEWAY_SHUTDOWN_GRACE_SEC", 10.0, 0.0, 60.0
            ),
            ownership_check_interval_sec=_bounded_float(
                environ, "MAGI_V3_GATEWAY_OWNERSHIP_CHECK_INTERVAL_SEC", 5.0, 0.05, 60.0
            ),
            ownership_loss_confirmations=_bounded_int(
                environ, "MAGI_V3_GATEWAY_OWNERSHIP_LOSS_CONFIRMATIONS", 3, 1, 12
            ),
        )


def _bounded_int(
    environ: Mapping[str, str], name: str, default: int, minimum: int, maximum: int
) -> int:
    try:
        value = int(environ.get(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise GatewayConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise GatewayConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _bounded_float(
    environ: Mapping[str, str], name: str, default: float, minimum: float, maximum: float
) -> float:
    try:
        value = float(environ.get(name, str(default)))
    except (TypeError, ValueError) as exc:
        raise GatewayConfigurationError(f"{name} must be numeric") from exc
    if not minimum <= value <= maximum:
        raise GatewayConfigurationError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def _json_object(path: Path, expected_sha256: str) -> dict[str, Any]:
    if not _SHA256.fullmatch(expected_sha256):
        raise GatewayConfigurationError("ownership manifest SHA-256 is missing or invalid")
    try:
        if path.is_symlink() or path.resolve(strict=True) != path:
            raise GatewayConfigurationError("ownership manifest path is symlinked or non-canonical")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except GatewayConfigurationError:
        raise
    except OSError as exc:
        raise GatewayConfigurationError(f"ownership manifest is unreadable: {exc}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise GatewayConfigurationError("ownership manifest must be a regular file")
        if before.st_size > 16 * 1024 * 1024:
            raise GatewayConfigurationError("ownership manifest is unreasonably large")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read()
        after = os.fstat(descriptor)
        current = path.lstat()
        signature = lambda value: (  # noqa: E731 - compact stable-FD identity tuple
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            stat.S_IMODE(value.st_mode),
        )
        if (
            signature(before) != signature(after)
            or (after.st_dev, after.st_ino) != (current.st_dev, current.st_ino)
            or stat.S_ISLNK(current.st_mode)
        ):
            raise GatewayConfigurationError("ownership manifest changed while being verified")
    finally:
        os.close(descriptor)
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise GatewayConfigurationError("ownership manifest SHA-256 mismatch")
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GatewayConfigurationError(f"ownership manifest is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise GatewayConfigurationError("ownership manifest must be a JSON object")
    return payload


def _string_set(value: object, *, name: str) -> frozenset[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise GatewayConfigurationError(f"{name} must be a list of non-empty strings")
    return frozenset(value)


def validate_release_ownership(environ: Mapping[str, str]) -> ReleaseOwnership:
    """Fail closed unless gateway and control share one declared release identity."""

    if environ.get("MAGI_V3_ROLE", "").strip() != "gateway":
        raise GatewayConfigurationError("MAGI_V3_ROLE must equal gateway")
    release_id = environ.get("MAGI_V3_RELEASE_ID", "").strip()
    if not _RELEASE_ID.fullmatch(release_id):
        raise GatewayConfigurationError("MAGI_V3_RELEASE_ID is missing or invalid")
    raw_path = environ.get("MAGI_V3_OWNERSHIP_MANIFEST", "").strip()
    if not raw_path:
        raise GatewayConfigurationError("MAGI_V3_OWNERSHIP_MANIFEST is required")
    manifest_path = Path(raw_path).expanduser()
    if not manifest_path.is_absolute():
        raise GatewayConfigurationError("MAGI_V3_OWNERSHIP_MANIFEST must be absolute")
    ownership_sha = environ.get("MAGI_V3_OWNERSHIP_MANIFEST_SHA256", "").strip()
    payload = _json_object(manifest_path, ownership_sha)
    if payload.get("schema_version") != 1 or payload.get("release_id") != release_id:
        raise GatewayConfigurationError("ownership manifest release identity mismatch")

    release_sha = payload.get("release_manifest_sha256")
    env_release_sha = environ.get("MAGI_V3_RELEASE_MANIFEST_SHA256", "").strip()
    if not isinstance(release_sha, str) or not _SHA256.fullmatch(release_sha):
        raise GatewayConfigurationError("ownership manifest release hash is invalid")
    if env_release_sha != release_sha:
        raise GatewayConfigurationError("environment and ownership release hashes differ")

    rows = payload.get("roles")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise GatewayConfigurationError("ownership manifest roles must be a list of objects")
    by_role: dict[str, dict[str, Any]] = {}
    for row in rows:
        role = row.get("role")
        if not isinstance(role, str) or role in by_role:
            raise GatewayConfigurationError("ownership manifest contains invalid or duplicate roles")
        by_role[role] = row
    if "gateway" not in by_role or "control" not in by_role:
        raise GatewayConfigurationError("ownership manifest must declare gateway and control roles")
    gateway = by_role["gateway"]
    control = by_role["control"]

    expected = (
        (gateway, "gateway", "com.magi.v3.gateway", [5002, 5003], _GATEWAY_DOMAINS),
        (control, "control", "com.magi.v3.control", [8088], _CONTROL_DOMAINS),
    )
    for binding, role, label, ports, domains in expected:
        if binding.get("release_id") != release_id or binding.get("label") != label:
            raise GatewayConfigurationError(f"{role} ownership does not match the declared release")
        if binding.get("release_manifest_sha256") != release_sha:
            raise GatewayConfigurationError(f"{role} ownership release hash mismatch")
        if binding.get("ports") != ports:
            raise GatewayConfigurationError(f"{role} ownership ports mismatch")
        if not domains <= _string_set(binding.get("ownership_domains"), name=f"{role} ownership_domains"):
            raise GatewayConfigurationError(f"{role} ownership domains are incomplete")
        declared_manifest = binding.get("ownership_manifest")
        if not isinstance(declared_manifest, str) or Path(declared_manifest).resolve() != manifest_path:
            raise GatewayConfigurationError(f"{role} ownership manifest path mismatch")

    if environ.get("MAGI_V3_PORTS", "").strip() != "5002,5003":
        raise GatewayConfigurationError("gateway environment must declare ports 5002,5003")
    env_domains = frozenset(
        item.strip()
        for item in environ.get("MAGI_V3_OWNERSHIP_DOMAINS", "").split(",")
        if item.strip()
    )
    if not _GATEWAY_DOMAINS <= env_domains:
        raise GatewayConfigurationError("gateway environment ownership domains are incomplete")
    return ReleaseOwnership(release_id, manifest_path, release_sha, gateway, control)


class LazyAppFactory:
    """Resolve a configured app factory once, only during explicit startup."""

    def __init__(self, reference: str) -> None:
        self.reference = reference
        self._factory: AppFactory | None = None

    def __call__(self) -> WSGIApp:
        if self._factory is None:
            module_name, separator, attribute = self.reference.partition(":")
            if not separator:
                raise GatewayConfigurationError(f"invalid app factory reference: {self.reference}")
            try:
                module = importlib.import_module(module_name)
                candidate = getattr(module, attribute)
            except (ImportError, AttributeError) as exc:
                raise GatewayConfigurationError(
                    f"cannot resolve app factory {self.reference}: {exc}"
                ) from exc
            if not callable(candidate):
                raise GatewayConfigurationError(f"app factory is not callable: {self.reference}")
            self._factory = candidate
        app = self._factory()
        if not callable(app):
            raise GatewayConfigurationError(f"app factory did not return a WSGI app: {self.reference}")
        return app


class _WaitressHandle:
    def __init__(self, server: Any, *, grace_sec: float) -> None:
        self._server = server
        self._grace_sec = grace_sec
        self._closed = False

    def run(self) -> None:
        self._server.run()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.close()
        self._server.task_dispatcher.shutdown(
            cancel_pending=False,
            timeout=self._grace_sec,
        )


def create_waitress_server(
    app: WSGIApp, service: ServiceDefinition, config: GatewayConfig
) -> ServerHandle:
    """Create one bounded production server; importing Waitress is deferred."""

    from waitress import create_server

    server = create_server(
        app,
        host=config.host,
        port=int(service.port or 0),
        threads=config.threads,
        channel_timeout=config.channel_timeout,
        cleanup_interval=15,
        clear_untrusted_proxy_headers=True,
        ident=f"MAGI-v3-{service.service_id}",
    )
    return _WaitressHandle(server, grace_sec=config.shutdown_grace_sec)


class _ProbeMiddleware:
    def __init__(self, app: WSGIApp, gateway: "Gateway") -> None:
        self.app = app
        self.gateway = gateway

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        path = str(environ.get("PATH_INFO", ""))
        if method in {"GET", "HEAD"} and path in {"/livez", "/readyz"}:
            payload = self.gateway.liveness() if path == "/livez" else self.gateway.readiness()
            body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            status = "200 OK" if payload["ready"] else "503 Service Unavailable"
            start_response(
                status,
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                    ("Cache-Control", "no-store"),
                    ("X-Content-Type-Options", "nosniff"),
                ],
            )
            return [b"" if method == "HEAD" else body]
        return self.app(environ, start_response)


class Gateway:
    """Own both gateway listeners under one verified release and role lock."""

    def __init__(
        self,
        *,
        runtime: CoreRuntime,
        ownership: ReleaseOwnership,
        role_guard: RoleGuard,
        control_owner: ControlOwner,
        services: tuple[tuple[ServiceDefinition, AppFactory], ...],
        config: GatewayConfig,
        dependency_probe: DependencyProbe | None = None,
        server_factory: ServerFactory = create_waitress_server,
    ) -> None:
        if [(service.service_id, service.port) for service, _ in services] != [
            ("main_http", 5002),
            ("tools_http", 5003),
        ]:
            raise GatewayConfigurationError("gateway must assemble main_http:5002 and tools_http:5003")
        self.runtime = runtime
        self.ownership = ownership
        self.role_guard = role_guard
        self.control_owner = control_owner
        self.dependency_probe = dependency_probe or (
            lambda: (True, {"role_owner": True, "children": {}})
        )
        self.services = services
        self.config = config
        self.server_factory = server_factory
        self._servers: list[tuple[ServiceDefinition, ServerHandle]] = []
        self._threads: list[threading.Thread] = []
        self._errors: list[tuple[str, BaseException]] = []
        self._shutdown_requested = threading.Event()
        self._state_lock = threading.RLock()
        self._serving = False
        self._stopping = False
        self._started_once = False

    def liveness(self) -> dict[str, Any]:
        report = self.runtime.health.liveness().to_dict()
        report["scope"] = "gateway_process"
        report["components"]["model_probe_performed"] = False
        return report

    def readiness(self) -> dict[str, Any]:
        control_active = self._control_owner_active()
        try:
            dependencies_ready, dependency_details = self.dependency_probe()
        except Exception:
            dependencies_ready = False
            dependency_details = {"error": "dependency_probe_failed"}
        with self._state_lock:
            thread_states = {
                service.service_id: (
                    "serving"
                    if index < len(self._threads) and self._threads[index].is_alive() and not self._errors
                    else "not_serving"
                )
                for index, (service, _) in enumerate(self.services)
            }
            ready = bool(
                self._serving
                and not self._stopping
                and self.role_guard.acquired
                and control_active
                and dependencies_ready
                and not self._errors
                and all(state == "serving" for state in thread_states.values())
            )
        return {
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "scope": "gateway_ingress",
            "release_id": self.ownership.release_id,
            "components": {
                "release_ownership": "verified",
                "control_ownership": "active" if control_active else "inactive",
                "supervisor": dependency_details,
                "global_active_release_lock": "delegated_to_control",
                "gateway_role_lock": "owned" if self.role_guard.acquired else "not_owned",
                "services": thread_states,
                "resource_governor": "available",
                "active_resources": self.runtime.governor.active_counts(),
                "model_probe_performed": False,
            },
        }

    def start(self) -> None:
        with self._state_lock:
            if self._started_once:
                raise GatewayRuntimeError("gateway instances cannot be restarted")
            self._started_once = True
        try:
            if not self._control_owner_active():
                raise GatewayRuntimeError("same-release control role is not active")
            # This is deliberately the role-local guard, never CoreRuntime.active_guard.
            self.role_guard.acquire()
            previous_handlers = (
                {signum: signal.getsignal(signum) for signum in (signal.SIGTERM, signal.SIGINT)}
                if threading.current_thread() is threading.main_thread()
                else {}
            )
            try:
                apps = [(service, factory()) for service, factory in self.services]
                for _, app in apps:
                    loader = getattr(app, "load", None)
                    if callable(loader):
                        loader()
            finally:
                # Legacy apps install their own process handlers at import time.
                # They are WSGI children here, so gateway ownership must win.
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
            for service, app in apps:
                wrapped = _ProbeMiddleware(app, self)
                self._servers.append((service, self.server_factory(wrapped, service, self.config)))
            for service, server in self._servers:
                thread = threading.Thread(
                    target=self._run_server,
                    args=(service, server),
                    name=f"magi-v3-{service.service_id}",
                    daemon=False,
                )
                self._threads.append(thread)
                thread.start()
            with self._state_lock:
                self._serving = True
        except BaseException:
            self.shutdown()
            raise

    def _run_server(self, service: ServiceDefinition, server: ServerHandle) -> None:
        try:
            server.run()
            with self._state_lock:
                stopping = self._stopping
            if not stopping:
                raise GatewayRuntimeError(f"{service.service_id} server stopped unexpectedly")
        except BaseException as exc:
            with self._state_lock:
                if not self._stopping:
                    self._errors.append((service.service_id, exc))
            self._shutdown_requested.set()

    def request_shutdown(self, _signum: int | None = None, _frame: FrameType | None = None) -> None:
        self._shutdown_requested.set()

    def serve_forever(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise GatewayRuntimeError("signal handlers require the main thread")
        restore_signals: Callable[[], None] = lambda: None
        try:
            self.start()
            restore_signals = install_shutdown_handlers(self._shutdown_requested.set)
            ownership_losses = 0
            while not self._shutdown_requested.wait(self.config.ownership_check_interval_sec):
                if self._control_owner_active():
                    ownership_losses = 0
                    continue
                ownership_losses += 1
                if ownership_losses >= self.config.ownership_loss_confirmations:
                    with self._state_lock:
                        self._errors.append(
                            ("control", GatewayRuntimeError("same-release control role lost ownership"))
                        )
                    self._shutdown_requested.set()
        finally:
            self.shutdown()
            restore_signals()
        if self._errors:
            service, error = self._errors[0]
            raise GatewayRuntimeError(f"{service} failed: {error}") from error

    def _control_owner_active(self) -> bool:
        try:
            return self.control_owner() is True
        except Exception:
            return False

    def shutdown(self) -> None:
        with self._state_lock:
            if self._stopping:
                return
            self._stopping = True
            self._serving = False
        self._shutdown_requested.set()
        for _, server in reversed(self._servers):
            try:
                server.close()
            except Exception:
                pass
        current = threading.current_thread()
        for thread in self._threads:
            if thread is not current and thread.is_alive():
                thread.join(timeout=self.config.shutdown_grace_sec + 1.0)
        try:
            self.runtime.close()
        finally:
            self.role_guard.release()


def build_gateway(
    environ: Mapping[str, str] | None = None,
    *,
    service_manifest_path: Path | None = None,
    app_factories: Mapping[str, AppFactory] | None = None,
    server_factory: ServerFactory = create_waitress_server,
    role_guard_factory: RoleGuardFactory,
    control_owner_factory: ControlOwnerFactory,
    dependency_probe_factory: DependencyProbeFactory | None = None,
    runtime: CoreRuntime | None = None,
) -> Gateway:
    """Validate declarations, then compose without importing apps or opening sockets."""

    env = os.environ if environ is None else environ
    ownership = validate_release_ownership(env)
    if service_manifest_path is None:
        manifest_path, manifest = load_bound_service_manifest(
            ServiceIdentity.from_environment("gateway", env),
            env,
        )
    else:
        manifest_path = service_manifest_path.expanduser().resolve()
        manifest = load_service_manifest(manifest_path)
    definitions = tuple(manifest.for_role("gateway"))
    if [(row.service_id, row.port, row.kind, row.required) for row in definitions] != [
        ("main_http", 5002, "wsgi", True),
        ("tools_http", 5003, "wsgi", True),
    ]:
        raise GatewayConfigurationError("service manifest gateway declaration is incomplete")
    supplied = app_factories or {}
    unknown = set(supplied) - {row.service_id for row in definitions}
    if unknown:
        raise GatewayConfigurationError(f"unknown injected gateway factories: {sorted(unknown)}")
    services = tuple(
        (
            row,
            supplied[row.service_id]
            if row.service_id in supplied
            else LazyAppFactory(str(row.factory)),
        )
        for row in definitions
    )
    core = runtime or CoreRuntime.build(load_settings(env))
    role_guard = role_guard_factory(env, ownership)
    control_owner = control_owner_factory(env, ownership)
    dependency_probe = (
        dependency_probe_factory(env, ownership, manifest_path)
        if dependency_probe_factory is not None
        else None
    )
    return Gateway(
        runtime=core,
        ownership=ownership,
        role_guard=role_guard,
        control_owner=control_owner,
        dependency_probe=dependency_probe,
        services=services,
        config=GatewayConfig.from_environ(env),
        server_factory=server_factory,
    )


class _ExclusiveGatewayRoleGuard:
    """Run the read-only release fence immediately before taking the role lease."""

    def __init__(self, identity: ServiceIdentity, probe: OwnershipProbe) -> None:
        self.identity = identity
        self.probe = probe
        self.lease = RoleLease(identity)

    @property
    def acquired(self) -> bool:
        return self.lease.acquired

    def acquire(self) -> None:
        self.probe.assert_exclusive(self.identity.release_root)
        self.lease.acquire()

    def release(self) -> None:
        self.lease.release()


def create_gateway_role_guard(
    environ: Mapping[str, str],
    ownership: ReleaseOwnership,
    *,
    ownership_probe: OwnershipProbe | None = None,
) -> RoleGuard:
    """Compose the shared role-local lease with a fail-closed ownership probe."""

    identity = ServiceIdentity.from_environment("gateway", environ)
    if identity.release_id != ownership.release_id:
        raise GatewayConfigurationError("service identity and ownership release IDs differ")
    declared_release_manifest = ownership.gateway_binding.get("release_manifest")
    if (
        not isinstance(declared_release_manifest, str)
        or Path(declared_release_manifest).resolve() != identity.release_manifest
    ):
        raise GatewayConfigurationError("gateway ownership release manifest path mismatch")
    return _ExclusiveGatewayRoleGuard(identity, ownership_probe or DefaultOwnershipProbe())


def create_control_owner(
    environ: Mapping[str, str],
    ownership: ReleaseOwnership,
) -> ControlOwner:
    """Build the production same-release control PID/flock/PGID verifier."""

    identity = ServiceIdentity.from_environment("gateway", environ)
    if identity.release_id != ownership.release_id:
        raise GatewayConfigurationError("control verifier release identity mismatch")
    control_pid_file = identity.runtime_root / "pids" / "control.pid"

    def active() -> bool:
        return verify_role_owner(
            control_pid_file,
            role="control",
            release_id=identity.release_id,
            release_root=identity.release_root,
        )

    return active


def create_supervisor_dependency_probe(
    environ: Mapping[str, str],
    _ownership: ReleaseOwnership,
    manifest_path: Path,
) -> DependencyProbe:
    """Bind public readiness to the same supervisor proof used by Control."""

    from .control import build_supervisor_dependency_probe

    identity = ServiceIdentity.from_environment("gateway", environ)
    manifest = load_service_manifest(manifest_path)
    return build_supervisor_dependency_probe(identity, manifest)


def main(argv: list[str] | None = None) -> int:
    if argv:
        print("magi_v3.gateway accepts configuration through its release-bound environment", file=sys.stderr)
        return 2
    try:
        gateway = build_gateway(
            role_guard_factory=create_gateway_role_guard,
            control_owner_factory=create_control_owner,
            dependency_probe_factory=create_supervisor_dependency_probe,
        )
        gateway.serve_forever()
    except CoreError as exc:
        print(f"gateway blocked: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

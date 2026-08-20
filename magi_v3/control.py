"""Production V3 control role with loopback 8088 health and readiness."""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .config import load_settings
from .errors import ConfigurationError, CoreError
from .process_compat import group_exists as _portable_group_exists
from .process_compat import process_group as _process_group
from .runtime import CoreRuntime
from .service_manifest import (
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
    verify_role_owner,
)

CONTROL_PORT = 8088


class HTTPServerLike(Protocol):
    timeout: float

    def handle_request(self) -> None: ...

    def server_close(self) -> None: ...


AdminServerFactory = Callable[..., HTTPServerLike]
FactoryResolver = Callable[[str], AdminServerFactory]
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DependencyProbe = Callable[[], tuple[bool, dict[str, object]]]


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_group_exists(process_group: int) -> bool:
    return _portable_group_exists(process_group)


def build_supervisor_dependency_probe(
    identity: ServiceIdentity,
    manifest: ServiceManifest,
    *,
    process_exists: Callable[[int], bool] = _process_exists,
    process_group: Callable[[int], int] = _process_group,
    process_group_exists: Callable[[int], bool] = _process_group_exists,
) -> DependencyProbe:
    """Return a cheap fail-closed probe for the supervisor and every child."""

    supervisor_pid = identity.runtime_root / "pids" / "supervisor.pid"
    required = tuple(service.service_id for service in manifest.for_role("supervisor"))

    def probe() -> tuple[bool, dict[str, object]]:
        details: dict[str, object] = {"role_owner": False, "children": {}}
        if not verify_role_owner(
            supervisor_pid,
            role="supervisor",
            release_id=identity.release_id,
            release_root=identity.release_root,
        ):
            return False, details
        details["role_owner"] = True
        child_details: dict[str, bool] = {}
        for service_id in required:
            path = identity.runtime_root / "pids" / f"service-{service_id}.pid"
            valid = False
            try:
                if path.is_symlink():
                    raise ValueError("linked child PID file")
                payload: Any = json.loads(path.read_text(encoding="utf-8"))
                pid = int(payload["pid"])
                group = int(payload["process_group"])
                valid = bool(
                    payload.get("schema_version") == 1
                    and payload.get("service_id") == service_id
                    and payload.get("release_id") == identity.release_id
                    and Path(str(payload.get("release_root") or "")).resolve()
                    == identity.release_root
                    and pid > 1
                    and group == pid
                    and process_exists(pid)
                    and process_group(pid) == group
                    and process_group_exists(group)
                )
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError, ProcessLookupError):
                valid = False
            child_details[service_id] = valid
        details["children"] = child_details
        return bool(required) and all(child_details.values()), details

    return probe


class ControlHealthApplication:
    """Pure routing facade around cheap V3 core health reports."""

    def __init__(
        self,
        runtime: CoreRuntime,
        dependency_probe: DependencyProbe | None = None,
    ) -> None:
        self.runtime = runtime
        self.dependency_probe = dependency_probe or (
            lambda: (True, {"role_owner": True, "children": {}})
        )

    def response(self, path: str) -> tuple[int, dict[str, object]]:
        route = path.partition("?")[0]
        if route == "/livez":
            report = self.runtime.health.liveness()
            return 200, report.to_dict()
        if route in {"/readyz", "/health"}:
            report = self.runtime.health.readiness()
            payload = report.to_dict()
            dependency_ready, dependency = self.dependency_probe()
            ready = bool(report.ready and dependency_ready)
            payload["ready"] = ready
            payload["status"] = "ready" if ready else "not_ready"
            components = payload.setdefault("components", {})
            if isinstance(components, dict):
                components["supervisor"] = dependency
            return (200 if ready else 503), payload
        return 404, {"status": "not_found", "ready": False}


def _resolve_factory(reference: str) -> AdminServerFactory:
    module_name, separator, attribute = reference.partition(":")
    if not separator:
        raise ServiceRuntimeError("control service factory reference is invalid")
    try:
        module = importlib.import_module(module_name)
        factory = getattr(module, attribute)
    except (AttributeError, ImportError) as exc:
        raise ServiceRuntimeError(f"control service factory is unavailable: {reference}") from exc
    if not callable(factory):
        raise ServiceRuntimeError(f"control service factory is not callable: {reference}")
    return factory


def _website_configuration(
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, str]:
    env = os.environ if environ is None else environ
    raw = env.get("MAGI_WEBSITE_ROOT", "").strip()
    expected_hash = env.get("MAGI_WEBSITE_ADMIN_SHA256", "").strip()
    if not raw:
        raise ServiceRuntimeError("MAGI_WEBSITE_ROOT is required for Website Admin parity")
    path = Path(raw).expanduser()
    if not path.is_absolute() or path.is_symlink():
        raise ServiceRuntimeError("MAGI_WEBSITE_ROOT must be an absolute non-symlink path")
    try:
        path = path.resolve(strict=True)
    except OSError as exc:
        raise ServiceRuntimeError(f"MAGI_WEBSITE_ROOT is unavailable: {exc}") from exc
    if not _SHA256_RE.fullmatch(expected_hash):
        raise ServiceRuntimeError("MAGI_WEBSITE_ADMIN_SHA256 is required and must be lowercase SHA-256")
    for relative in ("admin", "data", "assets", "admin/admin_server.py"):
        candidate = path / relative
        if candidate.is_symlink() or not candidate.exists():
            raise ServiceRuntimeError(f"Website Admin root is incomplete or unsafe: {relative}")
    return path, expected_hash


class ControlService:
    """Own the global release lock and control HTTP socket until shutdown."""

    def __init__(
        self,
        *,
        runtime: CoreRuntime,
        manifest: ServiceManifest,
        identity: ServiceIdentity,
        role_lease: RoleLease,
        ownership_probe: OwnershipProbe,
        website_root: Path,
        website_admin_sha256: str,
        dependency_probe: DependencyProbe | None = None,
        server_factory: AdminServerFactory | None = None,
        factory_resolver: FactoryResolver = _resolve_factory,
        signal_registrar: SignalRegistrar | None = None,
        request_timeout_sec: float = 0.25,
        ownership_probe_interval_sec: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        services = manifest.for_role("control")
        if len(services) != 1 or services[0].port != CONTROL_PORT:
            raise ServiceRuntimeError("control role must exclusively own production port 8088")
        if services[0].kind != "http_server":
            raise ServiceRuntimeError("control 8088 service must be an http_server")
        if identity.role != "control":
            raise ServiceRuntimeError("control service received a different role identity")
        if not services[0].factory:
            raise ServiceRuntimeError("control 8088 service has no compatibility factory")
        self.runtime = runtime
        self.service_definition = services[0]
        self.identity = identity
        self.role_lease = role_lease
        self.ownership_probe = ownership_probe
        self.server_factory = server_factory
        self.factory_resolver = factory_resolver
        self.website_root = website_root
        if not _SHA256_RE.fullmatch(website_admin_sha256):
            raise ServiceRuntimeError("Website Admin source SHA-256 is invalid")
        self.website_admin_sha256 = website_admin_sha256
        self.dependency_probe = dependency_probe or (
            lambda: (True, {"role_owner": True, "children": {}})
        )
        self.signal_registrar = signal_registrar
        self.request_timeout_sec = request_timeout_sec
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
        self.runtime.activate()  # The control role is the sole global active-lock owner.
        server: HTTPServerLike | None = None
        request_thread: threading.Thread | None = None
        request_errors: list[BaseException] = []

        def restore_signals() -> None:
            return

        try:
            application = ControlHealthApplication(self.runtime, self.dependency_probe)
            factory = self.server_factory or self.factory_resolver(
                str(self.service_definition.factory)
            )
            server = factory(
                server_address=("127.0.0.1", CONTROL_PORT),
                health_application=application,
                website_root=self.website_root,
                website_admin_sha256=self.website_admin_sha256,
            )
            server.timeout = self.request_timeout_sec
            self.role_lease.acquire()
            restore_signals = install_shutdown_handlers(
                self.stop,
                **({"register": self.signal_registrar} if self.signal_registrar else {}),
            )

            # Keep the loopback admin/health listener responsive while the
            # fail-closed ownership audit inspects macOS process/listener
            # state.  ``ps``/``lsof`` can occasionally take several seconds
            # when a File Provider or SMB mount is unhealthy.  Running that
            # audit in the same accept loop caused harmless health polling to
            # queue behind it and report 8088 as down.
            def serve_requests() -> None:
                assert server is not None
                while not self.stop_event.is_set():
                    try:
                        server.handle_request()
                    except BaseException as exc:  # fail closed in the owner thread
                        if self.stop_event.is_set():
                            return
                        request_errors.append(exc)
                        self.stop_event.set()
                        return

            request_thread = threading.Thread(
                target=serve_requests,
                name="magi-v3-control-http",
                daemon=True,
            )
            request_thread.start()
            while not self.stop_event.wait(self.ownership_probe_interval_sec):
                if not periodic_ownership.check():
                    logging.getLogger(__name__).warning(
                        "transient periodic ownership probe timeout deferred; "
                        "control listener remains bound and will retry"
                    )
            if request_errors:
                raise ServiceRuntimeError(
                    f"control HTTP listener failed: {type(request_errors[0]).__name__}"
                ) from request_errors[0]
            return 0
        finally:
            self.stop_event.set()
            restore_signals()
            self.role_lease.release()
            if server is not None:
                server.server_close()
            if request_thread is not None:
                request_thread.join(timeout=max(1.0, self.request_timeout_sec * 4))
            self.runtime.close()


def build_control_service(
    identity: ServiceIdentity,
    *,
    manifest_path: Path | None = None,
    ownership_probe: OwnershipProbe | None = None,
    environ: Mapping[str, str] | None = None,
) -> ControlService:
    env = os.environ if environ is None else environ
    if manifest_path is None:
        _, manifest = load_bound_service_manifest(identity, env)
    else:
        manifest = load_service_manifest(manifest_path)
    runtime = CoreRuntime.build(load_settings(env))
    website_root, website_admin_sha256 = _website_configuration(env)
    return ControlService(
        runtime=runtime,
        manifest=manifest,
        identity=identity,
        role_lease=RoleLease(identity),
        ownership_probe=ownership_probe or DefaultOwnershipProbe(),
        website_root=website_root,
        website_admin_sha256=website_admin_sha256,
        dependency_probe=build_supervisor_dependency_probe(identity, manifest),
    )


def main() -> int:
    try:
        identity = ServiceIdentity.from_environment("control")
        return build_control_service(identity).run()
    except (ConfigurationError, CoreError, OSError, ServiceRuntimeError) as exc:
        print(json.dumps({"status": "blocked", "role": "control", "error": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

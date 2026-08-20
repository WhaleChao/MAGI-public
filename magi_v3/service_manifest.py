"""Strict loader for the single-owner V3 production service manifest."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigurationError

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FACTORY = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*$"
)
_ROLES = frozenset({"gateway", "control", "supervisor"})
_KINDS = frozenset({"wsgi", "http_server", "process"})
_EXPECTED_PORTS = {5002: "gateway", 5003: "gateway", 8088: "control"}
_PYTHON_PLACEHOLDER = "{python}"
_DEPLOYMENT_MODES = frozenset({"production", "isolated_live_validation"})
_MODE_ENV = "MAGI_V3_DEPLOYMENT_MODE"
_MANIFEST_ENV = "MAGI_V3_SERVICE_MANIFEST"
_MANIFEST_SHA_ENV = "MAGI_V3_SERVICE_MANIFEST_SHA256"
_SAFETY_ENV = {
    "production": {
        "MAGI_V3_LIVE_VALIDATION": "0",
        "MAGI_V3_EXTERNAL_WRITES_ENABLED": "1",
        "MAGI_V3_NOTIFICATIONS_ENABLED": "1",
        "MAGI_V3_SCHEDULER_ENABLED": "1",
    },
    "isolated_live_validation": {
        "MAGI_V3_LIVE_VALIDATION": "1",
        "MAGI_V3_EXTERNAL_WRITES_ENABLED": "0",
        "MAGI_V3_NOTIFICATIONS_ENABLED": "0",
        "MAGI_V3_SCHEDULER_ENABLED": "0",
    },
}


def assert_deployment_safety(
    deployment_mode: str,
    environ: Mapping[str, str],
) -> None:
    """Require every code-owned safety switch for the selected mode."""

    from .service_runtime import ServiceRuntimeError

    if deployment_mode not in _DEPLOYMENT_MODES:
        raise ServiceRuntimeError(f"{_MODE_ENV} is missing or invalid")
    if environ.get(_MODE_ENV, "").strip() != deployment_mode:
        raise ServiceRuntimeError(f"{_MODE_ENV} does not match the selected mode")
    for name, expected in _SAFETY_ENV[deployment_mode].items():
        if environ.get(name, "").strip() != expected:
            raise ServiceRuntimeError(
                f"deployment safety binding mismatch: {name} must equal {expected}"
            )


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    service_id: str
    role: str
    kind: str
    required: bool
    port: int | None = None
    factory: str | None = None
    argv: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ServiceManifest:
    deployment_mode: str
    services: tuple[ServiceDefinition, ...]
    host_singletons: tuple[str, ...]
    forbidden_release_processes: tuple[str, ...]

    def for_role(self, role: str) -> tuple[ServiceDefinition, ...]:
        if role not in _ROLES:
            raise ConfigurationError(f"unsupported service role: {role}")
        return tuple(service for service in self.services if service.role == role)

    def service(self, service_id: str) -> ServiceDefinition:
        for service in self.services:
            if service.service_id == service_id:
                return service
        raise ConfigurationError(f"unknown V3 service: {service_id}")


def _object(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"service manifest is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("service manifest must be a JSON object")
    return payload


def _identifiers(value: Any, *, name: str, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ConfigurationError(f"{name} must be a {'possibly empty ' if allow_empty else 'non-empty '}list")
    normalized = tuple(value)
    if any(not isinstance(item, str) or not _IDENTIFIER.fullmatch(item) for item in normalized):
        raise ConfigurationError(f"{name} contains an invalid identifier")
    if len(normalized) != len(set(normalized)):
        raise ConfigurationError(f"{name} contains duplicates")
    return normalized


def _safe_argv(value: Any, *, service_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) < 2 or any(
        not isinstance(item, str) or not item or "\x00" in item for item in value
    ):
        raise ConfigurationError(f"process service {service_id} requires safe argv")
    argv = tuple(value)
    if argv[0] != _PYTHON_PLACEHOLDER:
        raise ConfigurationError(f"process service {service_id} must use the release Python placeholder")
    script = Path(argv[1])
    if script.is_absolute() or ".." in script.parts or script.suffix != ".py":
        raise ConfigurationError(f"process service {service_id} script escapes the release")
    options = argv[2:]
    if options and options != ("--legacy-root", "."):
        raise ConfigurationError(f"process service {service_id} has unsupported arguments")
    for argument in argv[1:2]:
        path = Path(argument)
        if path.is_absolute() or ".." in path.parts:
            raise ConfigurationError(f"process service {service_id} argv escapes the release")
    return argv


def load_service_manifest(path: Path) -> ServiceManifest:
    """Load and validate exact service/port ownership without importing services."""

    payload = _object(path)
    if payload.get("schema_version") != 1:
        raise ConfigurationError("service manifest schema_version must equal 1")
    if payload.get("release_mode") != "single_active_replacement":
        raise ConfigurationError("service manifest must require single_active_replacement")
    deployment_mode = payload.get("deployment_mode")
    if deployment_mode not in _DEPLOYMENT_MODES:
        raise ConfigurationError("service manifest has an invalid deployment_mode")
    rows = payload.get("services")
    if not isinstance(rows, list) or not rows:
        raise ConfigurationError("service manifest services must be a non-empty list")

    services: list[ServiceDefinition] = []
    seen_ids: set[str] = set()
    seen_ports: set[int] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ConfigurationError(f"service {index} must be an object")
        service_id = row.get("id")
        role = row.get("role")
        kind = row.get("kind")
        required = row.get("required")
        if not isinstance(service_id, str) or not _IDENTIFIER.fullmatch(service_id):
            raise ConfigurationError(f"service {index} has an invalid id")
        if service_id in seen_ids:
            raise ConfigurationError(f"duplicate service id: {service_id}")
        if role not in _ROLES or kind not in _KINDS or type(required) is not bool:
            raise ConfigurationError(f"service {service_id} has invalid role, kind, or required flag")
        seen_ids.add(service_id)

        if kind in {"wsgi", "http_server"}:
            port = row.get("port")
            factory = row.get("factory")
            if type(port) is not int or port not in _EXPECTED_PORTS:
                raise ConfigurationError(f"service {service_id} has an unsupported production port")
            if _EXPECTED_PORTS[port] != role:
                raise ConfigurationError(f"service {service_id} assigns port {port} to the wrong role")
            if port in seen_ports:
                raise ConfigurationError(f"duplicate production port: {port}")
            if not isinstance(factory, str) or not _FACTORY.fullmatch(factory):
                raise ConfigurationError(f"service {service_id} has an invalid factory")
            if row.get("argv") not in (None, []):
                raise ConfigurationError(f"HTTP service {service_id} cannot declare argv")
            seen_ports.add(port)
            services.append(ServiceDefinition(service_id, role, kind, required, port, factory))
            continue

        if row.get("port") is not None or row.get("factory") is not None:
            raise ConfigurationError(f"process service {service_id} cannot own an HTTP factory or port")
        services.append(
            ServiceDefinition(service_id, role, kind, required, argv=_safe_argv(row.get("argv"), service_id=service_id))
        )

    if seen_ports != set(_EXPECTED_PORTS):
        missing = sorted(set(_EXPECTED_PORTS) - seen_ports)
        raise ConfigurationError(f"service manifest is missing required production ports: {missing}")
    if {service.role for service in services} != set(_ROLES):
        raise ConfigurationError("service manifest must assign work to all production roles")
    host_singletons = _identifiers(payload.get("host_singletons"), name="host_singletons")
    forbidden = _identifiers(
        [Path(item).stem for item in payload.get("forbidden_release_processes", [])]
        if isinstance(payload.get("forbidden_release_processes"), list)
        else payload.get("forbidden_release_processes"),
        name="forbidden_release_processes",
    )
    raw_forbidden = payload.get("forbidden_release_processes")
    if not isinstance(raw_forbidden, list) or any(
        not isinstance(item, str) or Path(item).name != item or not item.endswith(".py")
        for item in raw_forbidden
    ):
        raise ConfigurationError("forbidden_release_processes must contain plain Python filenames")
    return ServiceManifest(
        str(deployment_mode),
        tuple(services),
        host_singletons,
        tuple(raw_forbidden),
    )


def load_bound_service_manifest(
    identity: Any,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, ServiceManifest]:
    """Load the one hash-bound manifest selected by a launchd role.

    The loose ``load_service_manifest`` function remains useful for pure unit
    composition.  Production entrypoints use this stricter loader so an
    environment override can never point outside the immutable release or
    silently change the safety mode.
    """

    import hashlib
    import os

    from .service_runtime import ServiceRuntimeError, verify_release_member

    env = os.environ if environ is None else environ
    mode = env.get(_MODE_ENV, "").strip()
    assert_deployment_safety(mode, env)
    raw_path = env.get(_MANIFEST_ENV, "").strip()
    expected_sha = env.get(_MANIFEST_SHA_ENV, "").strip()
    if not raw_path:
        raise ServiceRuntimeError(f"{_MANIFEST_ENV} is required")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        raise ServiceRuntimeError(f"{_MANIFEST_SHA_ENV} must be lowercase SHA-256")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        raise ServiceRuntimeError("service manifest path must be absolute")
    bound = verify_release_member(identity, path, description="service manifest")
    actual_sha = hashlib.sha256(bound.read_bytes()).hexdigest()
    if actual_sha != expected_sha:
        raise ServiceRuntimeError("service manifest SHA-256 mismatch")
    manifest = load_service_manifest(bound)
    if manifest.deployment_mode != mode:
        raise ServiceRuntimeError("service manifest deployment mode mismatch")
    return bound, manifest

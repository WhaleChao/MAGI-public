#!/usr/bin/env python3
"""Verify that V3 production factories expose the exact pinned V2 route surface."""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

# This verifier is intentionally run against an immutable release tree.  The
# interpreter flag must be set before the first ``magi_v3`` import; exporting
# PYTHONDONTWRITEBYTECODE after Python startup would be too late.
sys.dont_write_bytecode = True

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from magi_v3.service_manifest import ServiceDefinition, load_service_manifest

_HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})


@dataclass(frozen=True, order=True, slots=True)
class Route:
    service: str
    rule: str
    methods: tuple[str, ...]
    endpoint: str

    @classmethod
    def from_mapping(cls, service: str, row: dict[str, Any]) -> "Route":
        return cls(
            service=service,
            rule=str(row.get("rule") or ""),
            methods=tuple(sorted(str(method).upper() for method in row.get("methods", []))),
            endpoint=str(row.get("endpoint") or ""),
        )


def collect_routes(app: Any, *, service: str) -> tuple[Route, ...]:
    """Collect normalized Flask-compatible rules without sending a request."""

    url_map = getattr(app, "url_map", None)
    if url_map is None and callable(getattr(app, "load", None)):
        app = app.load()
        url_map = getattr(app, "url_map", None)
    iterator = getattr(url_map, "iter_rules", None)
    if not callable(iterator):
        raise ValueError(f"factory for {service} did not return a Flask-compatible app")
    rows: list[Route] = []
    for rule in iterator():
        if str(getattr(rule, "endpoint", "")) == "static":
            continue
        methods = tuple(sorted(_HTTP_METHODS.intersection(set(getattr(rule, "methods", ()) or ()))))
        if not methods:
            continue
        rows.append(
            Route(
                service=service,
                rule=str(getattr(rule, "rule", "")),
                methods=methods,
                endpoint=str(getattr(rule, "endpoint", "")),
            )
        )
    normalized = tuple(sorted(rows))
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"factory for {service} contains duplicate route signatures")
    return normalized


def load_expected(path: Path) -> dict[str, tuple[Route, ...]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"runtime route inventory is unreadable: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("runtime route inventory schema_version must equal 1")
    services = payload.get("services")
    if not isinstance(services, dict) or set(services) != {"5002", "5003"}:
        raise ValueError("runtime route inventory must contain exactly 5002 and 5003")
    expected: dict[str, tuple[Route, ...]] = {}
    for service, rows in services.items():
        if not isinstance(rows, list):
            raise ValueError(f"runtime route inventory service {service} must be a list")
        expected[service] = tuple(sorted(Route.from_mapping(service, row) for row in rows))
    return expected


def resolve_factory(reference: str) -> Callable[[], Any]:
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"invalid factory reference: {reference}")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute, None)
    if not callable(factory):
        raise ValueError(f"factory is missing or not callable: {reference}")
    return factory


def _http_services(manifest_path: Path) -> dict[str, ServiceDefinition]:
    manifest = load_service_manifest(manifest_path)
    by_port = {
        str(service.port): service
        for service in manifest.services
        if service.kind == "wsgi" and service.port is not None
    }
    if set(by_port) != {"5002", "5003"}:
        raise ValueError("service manifest must define WSGI factories for 5002 and 5003")
    return by_port


def verify_route_parity(
    *,
    manifest_path: Path,
    inventory_path: Path,
    factory_resolver: Callable[[str], Callable[[], Any]] = resolve_factory,
) -> dict[str, Any]:
    expected = load_expected(inventory_path)
    definitions = _http_services(manifest_path)
    services: dict[str, Any] = {}
    ready = True
    for service in ("5002", "5003"):
        definition = definitions[service]
        assert definition.factory is not None
        actual = collect_routes(factory_resolver(definition.factory)(), service=service)
        expected_set = set(expected[service])
        actual_set = set(actual)
        missing = sorted(expected_set - actual_set)
        extra = sorted(actual_set - expected_set)
        exact = not missing and not extra and len(actual) == len(expected[service])
        ready = ready and exact
        services[service] = {
            "factory": definition.factory,
            "expected": len(expected[service]),
            "actual": len(actual),
            "exact": exact,
            "missing": [asdict(route) for route in missing],
            "extra": [asdict(route) for route in extra],
        }
    return {
        "schema_version": 1,
        "ready": ready,
        "mutation_performed": False,
        "network_access_performed": False,
        "services": services,
        "total_expected": sum(len(rows) for rows in expected.values()),
        "total_actual": sum(row["actual"] for row in services.values()),
    }


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=root / "config" / "v3_service_manifest.json")
    parser.add_argument(
        "--inventory",
        type=Path,
        default=root / "docs" / "architecture" / "v3" / "generated" / "v2_runtime_routes.json",
    )
    args = parser.parse_args(argv)
    os.environ.setdefault("MAGI_DISABLE_SERVER_STARTUP_HOOKS", "1")
    try:
        report = verify_route_parity(manifest_path=args.manifest, inventory_path=args.inventory)
    except Exception as exc:
        report = {
            "schema_version": 1,
            "ready": False,
            "mutation_performed": False,
            "network_access_performed": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ready") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())

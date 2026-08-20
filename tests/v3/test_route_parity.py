from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scripts import v3_route_parity as parity


class FakeMap:
    def __init__(self, rules):
        self._rules = rules

    def iter_rules(self):
        return iter(self._rules)


class FakeApp:
    def __init__(self, *rules):
        self.url_map = FakeMap(rules)


def _write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    manifest = {
        "schema_version": 1,
        "release_mode": "single_active_replacement",
        "deployment_mode": "production",
        "services": [
            {"id": "main", "role": "gateway", "kind": "wsgi", "required": True, "port": 5002, "factory": "fake:main"},
            {"id": "tools", "role": "gateway", "kind": "wsgi", "required": True, "port": 5003, "factory": "fake:tools"},
            {"id": "admin", "role": "control", "kind": "http_server", "required": True, "port": 8088, "factory": "fake:admin"},
            {"id": "worker", "role": "supervisor", "kind": "process", "required": True, "argv": ["{python}", "worker.py"]},
        ],
        "host_singletons": ["rpc"],
        "forbidden_release_processes": ["daemon.py"],
    }
    inventory = {
        "schema_version": 1,
        "services": {
            "5002": [{"service": "5002", "rule": "/", "methods": ["GET"], "endpoint": "index"}],
            "5003": [{"service": "5003", "rule": "/health", "methods": ["GET"], "endpoint": "health"}],
        },
    }
    manifest_path = tmp_path / "manifest.json"
    inventory_path = tmp_path / "inventory.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    return manifest_path, inventory_path


def test_exact_factory_route_surface_passes_without_requests(tmp_path: Path) -> None:
    manifest, inventory = _write_inputs(tmp_path)
    factories = {
        "fake:main": lambda: FakeApp(SimpleNamespace(rule="/", endpoint="index", methods={"GET", "HEAD", "OPTIONS"})),
        "fake:tools": lambda: FakeApp(SimpleNamespace(rule="/health", endpoint="health", methods={"GET"})),
    }

    report = parity.verify_route_parity(
        manifest_path=manifest,
        inventory_path=inventory,
        factory_resolver=factories.__getitem__,
    )

    assert report["ready"] is True
    assert report["total_expected"] == report["total_actual"] == 2
    assert report["mutation_performed"] is False
    assert report["network_access_performed"] is False


def test_missing_and_extra_routes_fail_closed(tmp_path: Path) -> None:
    manifest, inventory = _write_inputs(tmp_path)
    factories = {
        "fake:main": lambda: FakeApp(SimpleNamespace(rule="/unexpected", endpoint="wrong", methods={"POST"})),
        "fake:tools": lambda: FakeApp(SimpleNamespace(rule="/health", endpoint="health", methods={"GET"})),
    }

    report = parity.verify_route_parity(
        manifest_path=manifest,
        inventory_path=inventory,
        factory_resolver=factories.__getitem__,
    )

    assert report["ready"] is False
    assert report["services"]["5002"]["missing"]
    assert report["services"]["5002"]["extra"]


def test_lazy_factory_is_explicitly_loaded_for_surface_inspection() -> None:
    app = FakeApp(SimpleNamespace(rule="/health", endpoint="health", methods={"GET"}))
    lazy = SimpleNamespace(load=lambda: app)

    assert parity.collect_routes(lazy, service="5002") == (
        parity.Route("5002", "/health", ("GET",), "health"),
    )

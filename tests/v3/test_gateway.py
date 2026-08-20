from __future__ import annotations

import hashlib
import json
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import pytest

from magi_v3.config import load_settings
from magi_v3.gateway import (
    GatewayConfigurationError,
    GatewayConfig,
    GatewayRuntimeError,
    build_gateway,
    create_control_owner,
    create_gateway_role_guard,
    create_waitress_server,
    main as gateway_main,
    validate_release_ownership,
)
from magi_v3.runtime import CoreRuntime
from magi_v3.service_manifest import load_service_manifest
from magi_v3.service_runtime import RoleLease, ServiceIdentity


ROOT = Path(__file__).resolve().parents[2]
SERVICE_MANIFEST = ROOT / "config" / "v3_service_manifest.json"
EXECUTABLE_BYTES = b"#!/bin/sh\nexit 0\n"
EXECUTABLE_SHA = hashlib.sha256(EXECUTABLE_BYTES).hexdigest()
RELEASE_MANIFEST_BYTES = (
    json.dumps(
        {
            "schema_version": 1,
            "immutable": True,
            "release_id": "v3-gateway-test",
            "files": [{"path": "bin/python", "sha256": EXECUTABLE_SHA}],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    + "\n"
).encode()
RELEASE_SHA = hashlib.sha256(RELEASE_MANIFEST_BYTES).hexdigest()


class FakeGuard:
    def __init__(self) -> None:
        self.acquired = False
        self.acquire_calls = 0
        self.release_calls = 0

    def acquire(self) -> None:
        self.acquire_calls += 1
        if self.acquired:
            raise RuntimeError("already acquired")
        self.acquired = True

    def release(self) -> None:
        self.release_calls += 1
        self.acquired = False


class FakeServer:
    def __init__(self, app: Any, service_id: str, port: int) -> None:
        self.app = app
        self.service_id = service_id
        self.port = port
        self.closed = threading.Event()
        self.run_entered = threading.Event()
        self.close_calls = 0

    def run(self) -> None:
        self.run_entered.set()
        self.closed.wait(timeout=5)

    def close(self) -> None:
        self.close_calls += 1
        self.closed.set()


def _ownership(tmp_path: Path) -> tuple[dict[str, str], Path]:
    release_root = (tmp_path / "release").resolve()
    release_root.mkdir()
    executable = release_root / "bin" / "python"
    executable.parent.mkdir()
    executable.write_bytes(EXECUTABLE_BYTES)
    executable.chmod(0o700)
    release_manifest = release_root / "release-manifest.json"
    release_manifest.write_bytes(RELEASE_MANIFEST_BYTES)
    env_file = (tmp_path / "production.env").resolve()
    env_file.write_text("MAGI_TEST=1\n", encoding="utf-8")
    env_file.chmod(0o600)
    website_root = (tmp_path / "website").resolve()
    (website_root / "admin").mkdir(parents=True)
    (website_root / "data").mkdir()
    (website_root / "assets").mkdir()
    admin_source = website_root / "admin" / "admin_server.py"
    admin_source.write_text("class AdminHandler: pass\n", encoding="utf-8")
    runtime_root = (tmp_path / "runtime").resolve()
    path = runtime_root / "ownership" / "ownership-manifest.json"
    path.parent.mkdir(parents=True)

    def binding(
        role: str,
        label: str,
        ports: list[int],
        domains: list[str],
    ) -> dict[str, Any]:
        return {
            "role": role,
            "label": label,
            "release_id": "v3-gateway-test",
            "release_manifest_sha256": RELEASE_SHA,
            "release_manifest": str(release_manifest),
            "ports": ports,
            "ownership_domains": domains,
            "ownership_manifest": str(path),
        }

    payload = {
        "schema_version": 1,
        "release_id": "v3-gateway-test",
        "release_manifest_sha256": RELEASE_SHA,
        "roles": [
            binding(
                "gateway",
                "com.magi.v3.gateway",
                [5002, 5003],
                ["production_ingress", "webhook_consumers", "external_api"],
            ),
            binding(
                "control",
                "com.magi.v3.control",
                [8088],
                ["control_plane", "scheduler", "durable_ledger_writer"],
            ),
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    env = {
        "MAGI_V3_ROLE": "gateway",
        "MAGI_V3_RELEASE_ID": "v3-gateway-test",
        "MAGI_V3_RELEASE_MANIFEST_SHA256": RELEASE_SHA,
        "MAGI_V3_RELEASE_MANIFEST": str(release_manifest),
        "MAGI_V3_EXECUTABLE_PATH": str(executable),
        "MAGI_V3_OWNERSHIP_MANIFEST": str(path),
        "MAGI_V3_OWNERSHIP_MANIFEST_SHA256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "MAGI_V3_PORTS": "5002,5003",
        "MAGI_V3_OWNERSHIP_DOMAINS": "production_ingress,webhook_consumers,external_api",
        "MAGI_V3_STATE_DIR": str(runtime_root / "state" / "gateway"),
        "MAGI_V3_PID_FILE": str(runtime_root / "pids" / "gateway.pid"),
        "MAGI_V3_HOST_ACTIVE_LOCK_PATH": str((tmp_path / "global-active.lock").resolve()),
        "MAGI_ENV_FILE": str(env_file),
        "MAGI_ENV_FILE_SHA256": hashlib.sha256(env_file.read_bytes()).hexdigest(),
        "MAGI_WEBSITE_ROOT": str(website_root),
        "MAGI_WEBSITE_ADMIN_SHA256": hashlib.sha256(admin_source.read_bytes()).hexdigest(),
    }
    return env, path


def _runtime(env: dict[str, str]) -> CoreRuntime:
    return CoreRuntime.build(load_settings(env))


def _fallback_app(environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
    body = f"fallback:{environ.get('PATH_INFO', '')}".encode()
    start_response("200 OK", [("Content-Length", str(len(body)))])
    return [body]


def _request(app: Any, path: str, method: str = "GET") -> tuple[str, dict[str, str], bytes]:
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = dict(headers)

    body = b"".join(app({"PATH_INFO": path, "REQUEST_METHOD": method}, start_response))
    return captured["status"], captured["headers"], body


def _gateway(
    tmp_path: Path,
    *,
    factories: dict[str, Any] | None = None,
    server_factory: Any | None = None,
    control_owner: Any | None = None,
    dependency_probe: Any | None = None,
    env_overrides: dict[str, str] | None = None,
) -> tuple[Any, FakeGuard, list[FakeServer], CoreRuntime, dict[str, str]]:
    env, _ = _ownership(tmp_path)
    env.update(env_overrides or {})
    guard = FakeGuard()
    servers: list[FakeServer] = []
    runtime = _runtime(env)

    def make_server(app: Any, service: Any, _config: Any) -> FakeServer:
        server = FakeServer(app, service.service_id, service.port)
        servers.append(server)
        return server

    gateway = build_gateway(
        env,
        service_manifest_path=SERVICE_MANIFEST,
        app_factories=factories
        or {"main_http": lambda: _fallback_app, "tools_http": lambda: _fallback_app},
        server_factory=server_factory or make_server,
        role_guard_factory=lambda _env, _ownership: guard,
        control_owner_factory=lambda _env, _ownership: control_owner or (lambda: True),
        dependency_probe_factory=(
            lambda _env, _ownership, _manifest: dependency_probe
        ) if dependency_probe is not None else None,
        runtime=runtime,
    )
    return gateway, guard, servers, runtime, env


def test_import_is_side_effect_free_and_does_not_load_apps_waitress_ml_or_ocr(tmp_path: Path) -> None:
    state = tmp_path / "never-created"
    code = (
        "import os,sys;sys.dont_write_bytecode=True;"
        f"os.environ['MAGI_V3_STATE_DIR']={str(state)!r};"
        f"sys.path.insert(0,{str(ROOT)!r});"
        "import magi_v3.gateway;"
        "bad=[n for n in sys.modules if n in {'waitress','flask','api.server','api.tools_api'} "
        "or n.split('.')[0] in {'mlx','torch','pymupdf','fitz'}];"
        "print(','.join(sorted(bad)))"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", code],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert proc.stdout.strip() == ""
    assert not state.exists()


def test_release_ownership_requires_matching_gateway_and_control_declarations(tmp_path: Path) -> None:
    env, path = _ownership(tmp_path)
    ownership = validate_release_ownership(env)
    assert ownership.release_id == "v3-gateway-test"
    assert ownership.manifest_path == path
    assert ownership.gateway_binding["ports"] == [5002, 5003]
    assert ownership.control_binding["ports"] == [8088]


def test_main_fails_closed_without_release_bound_environment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("MAGI_V3_ROLE", raising=False)
    assert gateway_main([]) == 2
    assert "gateway blocked:" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("missing_release", "RELEASE_ID"),
        ("release_mismatch", "release identity mismatch"),
        ("control_release_mismatch", "control ownership"),
        ("control_missing", "gateway and control"),
        ("gateway_ports", "gateway ownership ports"),
    ],
)
def test_release_ownership_fails_closed_before_factory_or_socket(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    env, path = _ownership(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "missing_release":
        env.pop("MAGI_V3_RELEASE_ID")
    elif mutation == "release_mismatch":
        payload["release_id"] = "other"
    elif mutation == "control_release_mismatch":
        payload["roles"][1]["release_id"] = "other"
    elif mutation == "control_missing":
        payload["roles"] = payload["roles"][:1]
    elif mutation == "gateway_ports":
        payload["roles"][0]["ports"] = [5002]
    path.write_text(json.dumps(payload), encoding="utf-8")
    env["MAGI_V3_OWNERSHIP_MANIFEST_SHA256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    factory_called = False
    server_called = False

    def app_factory() -> Any:
        nonlocal factory_called
        factory_called = True
        return _fallback_app

    def server_factory(*_args: Any) -> Any:
        nonlocal server_called
        server_called = True
        raise AssertionError("must not create a server")

    with pytest.raises(GatewayConfigurationError, match=message):
        build_gateway(
            env,
            service_manifest_path=SERVICE_MANIFEST,
            app_factories={"main_http": app_factory, "tools_http": app_factory},
            server_factory=server_factory,
            role_guard_factory=lambda *_args: FakeGuard(),
            control_owner_factory=lambda *_args: lambda: True,
        )
    assert factory_called is False
    assert server_called is False


def test_build_gateway_rejects_missing_or_hash_drifted_installed_ownership_before_start(
    tmp_path: Path,
) -> None:
    env, path = _ownership(tmp_path)
    path.unlink()
    with pytest.raises(GatewayConfigurationError, match="ownership manifest is unreadable"):
        build_gateway(
            env,
            service_manifest_path=SERVICE_MANIFEST,
            app_factories={"main_http": lambda: _fallback_app, "tools_http": lambda: _fallback_app},
            server_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("server must not be created")
            ),
            role_guard_factory=lambda *_args: FakeGuard(),
            control_owner_factory=lambda *_args: lambda: True,
        )

    drift_root = tmp_path / "drift"
    drift_root.mkdir()
    env, path = _ownership(drift_root)
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(GatewayConfigurationError, match="SHA-256 mismatch"):
        build_gateway(
            env,
            service_manifest_path=SERVICE_MANIFEST,
            app_factories={"main_http": lambda: _fallback_app, "tools_http": lambda: _fallback_app},
            server_factory=lambda *_args: (_ for _ in ()).throw(
                AssertionError("server must not be created")
            ),
            role_guard_factory=lambda *_args: FakeGuard(),
            control_owner_factory=lambda *_args: lambda: True,
        )


def test_build_is_non_binding_and_factories_are_called_once_only_at_start(tmp_path: Path) -> None:
    calls: list[str] = []

    def factory(name: str) -> Any:
        def create() -> Any:
            calls.append(name)
            return _fallback_app

        return create

    gateway, guard, servers, runtime, env = _gateway(
        tmp_path,
        factories={"main_http": factory("main"), "tools_http": factory("tools")},
    )
    assert calls == []
    assert servers == []
    assert not Path(env["MAGI_V3_STATE_DIR"]).exists()
    assert runtime.active_guard.acquired is False

    gateway.start()
    assert calls == ["main", "tools"]
    assert [(server.service_id, server.port) for server in servers] == [
        ("main_http", 5002),
        ("tools_http", 5003),
    ]
    assert all(server.run_entered.wait(timeout=1) for server in servers)
    assert guard.acquired is True
    assert runtime.active_guard.acquired is False

    gateway.shutdown()
    assert guard.acquired is False
    assert all(server.close_calls == 1 for server in servers)
    assert calls == ["main", "tools"]


def test_start_fails_before_guard_factory_or_socket_when_control_is_not_active(
    tmp_path: Path,
) -> None:
    factory_calls = 0

    def factory() -> Any:
        nonlocal factory_calls
        factory_calls += 1
        return _fallback_app

    gateway, guard, servers, _runtime, _env = _gateway(
        tmp_path,
        factories={"main_http": factory, "tools_http": factory},
        control_owner=lambda: False,
    )
    with pytest.raises(GatewayRuntimeError, match="control role is not active"):
        gateway.start()
    assert guard.acquire_calls == 0
    assert factory_calls == 0
    assert servers == []


def test_factory_preflight_loads_each_compatibility_app_exactly_once(tmp_path: Path) -> None:
    loads: list[str] = []

    class LoadableApp:
        def __init__(self, name: str) -> None:
            self.name = name

        def load(self) -> Any:
            loads.append(self.name)
            return self

        def __call__(self, environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
            return _fallback_app(environ, start_response)

    gateway, _guard, servers, _runtime, _env = _gateway(
        tmp_path,
        factories={
            "main_http": lambda: LoadableApp("main"),
            "tools_http": lambda: LoadableApp("tools"),
        },
    )
    gateway.start()
    assert loads == ["main", "tools"]
    _request(servers[0].app, "/application-route")
    assert loads == ["main", "tools"]
    gateway.shutdown()


def test_legacy_factory_cannot_replace_gateway_process_signal_handlers(tmp_path: Path) -> None:
    original = signal.getsignal(signal.SIGTERM)

    class SignalMutatingApp:
        def load(self) -> Any:
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            return self

        def __call__(self, environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
            return _fallback_app(environ, start_response)

    gateway, _guard, _servers, _runtime, _env = _gateway(
        tmp_path,
        factories={"main_http": SignalMutatingApp, "tools_http": SignalMutatingApp},
    )
    gateway.start()
    try:
        assert signal.getsignal(signal.SIGTERM) is original
    finally:
        gateway.shutdown()


def test_shared_role_lease_and_injected_ownership_probe_are_used_without_global_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _ = _ownership(tmp_path)
    from magi_v3 import service_runtime

    monkeypatch.setattr(
        service_runtime,
        "_canonical_runtime_root",
        lambda: Path(env["MAGI_V3_STATE_DIR"]).parents[1],
    )
    ownership = validate_release_ownership(env)
    probed: list[Path] = []

    class Probe:
        def assert_exclusive(self, release_root: Path) -> None:
            probed.append(release_root)

    guard = create_gateway_role_guard(env, ownership, ownership_probe=Probe())
    runtime = _runtime(env)
    assert not Path(env["MAGI_V3_PID_FILE"]).exists()
    guard.acquire()
    try:
        record = json.loads(Path(env["MAGI_V3_PID_FILE"]).read_text(encoding="utf-8"))
        assert probed == [Path(env["MAGI_V3_RELEASE_MANIFEST"]).parent]
        assert record["role"] == "gateway"
        assert record["release_id"] == ownership.release_id
        assert runtime.active_guard.acquired is False
        assert not Path(env["MAGI_V3_HOST_ACTIVE_LOCK_PATH"]).exists()
    finally:
        guard.release()
    assert not Path(env["MAGI_V3_PID_FILE"]).exists()


def test_production_control_owner_verifies_same_release_pid_lock_and_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env, _ = _ownership(tmp_path)
    runtime_root = Path(env["MAGI_V3_STATE_DIR"]).parents[1]
    from magi_v3 import service_runtime

    monkeypatch.setattr(service_runtime, "_canonical_runtime_root", lambda: runtime_root)
    ownership = validate_release_ownership(env)
    gateway_identity = ServiceIdentity.from_environment("gateway", env)
    control_identity = ServiceIdentity(
        role="control",
        release_id=gateway_identity.release_id,
        release_root=gateway_identity.release_root,
        release_manifest=gateway_identity.release_manifest,
        release_manifest_sha256=gateway_identity.release_manifest_sha256,
        runtime_root=gateway_identity.runtime_root,
        pid_file=gateway_identity.runtime_root / "pids" / "control.pid",
        executable_path=gateway_identity.executable_path,
        release_files=gateway_identity.release_files,
    )
    lease = RoleLease(control_identity)
    owner = create_control_owner(env, ownership)
    assert owner() is False
    lease.acquire()
    try:
        assert owner() is True
    finally:
        lease.release()
    assert owner() is False


def test_livez_and_readyz_are_cheap_gateway_probes_and_other_paths_reach_app(tmp_path: Path) -> None:
    gateway, _guard, servers, runtime, _env = _gateway(tmp_path)
    gateway.start()
    assert all(server.run_entered.wait(timeout=1) for server in servers)

    for server in servers:
        status, headers, body = _request(server.app, "/livez")
        live = json.loads(body)
        assert status == "200 OK"
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert live["scope"] == "gateway_process"
        assert live["components"]["model_probe_performed"] is False

        status, headers, body = _request(server.app, "/readyz")
        ready = json.loads(body)
        assert status == "200 OK"
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert ready["scope"] == "gateway_ingress"
        assert ready["components"]["global_active_release_lock"] == "delegated_to_control"
        assert ready["components"]["services"] == {
            "main_http": "serving",
            "tools_http": "serving",
        }
        assert ready["components"]["model_probe_performed"] is False

        status, headers, body = _request(server.app, "/readyz", method="HEAD")
        assert status == "200 OK"
        assert body == b""
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"

        status, _, body = _request(server.app, "/application-route")
        assert status == "200 OK"
        assert body == b"fallback:/application-route"

    assert runtime.active_guard.acquired is False
    gateway.shutdown()


def test_readyz_turns_503_immediately_when_control_owner_is_lost(tmp_path: Path) -> None:
    control = {"active": True}
    gateway, _guard, servers, _runtime, _env = _gateway(
        tmp_path,
        control_owner=lambda: control["active"],
    )
    gateway.start()
    assert all(server.run_entered.wait(timeout=1) for server in servers)
    assert _request(servers[0].app, "/readyz")[0] == "200 OK"

    control["active"] = False
    status, headers, body = _request(servers[0].app, "/readyz")
    assert status == "503 Service Unavailable"
    assert headers["Cache-Control"] == "no-store"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert json.loads(body)["components"]["control_ownership"] == "inactive"
    gateway.shutdown()


def test_readyz_turns_503_when_any_supervised_dependency_is_unhealthy(tmp_path: Path) -> None:
    dependency = {"ready": True}
    gateway, _guard, servers, _runtime, _env = _gateway(
        tmp_path,
        dependency_probe=lambda: (
            dependency["ready"],
            {"children": {"cron_scheduler": dependency["ready"]}},
        ),
    )
    gateway.start()
    assert all(server.run_entered.wait(timeout=1) for server in servers)
    assert _request(servers[0].app, "/readyz")[0] == "200 OK"

    dependency["ready"] = False
    status, _, body = _request(servers[0].app, "/readyz")
    assert status == "503 Service Unavailable"
    assert json.loads(body)["components"]["supervisor"]["children"]["cron_scheduler"] is False
    gateway.shutdown()


def test_serve_loop_stops_both_listeners_when_control_owner_is_lost(tmp_path: Path) -> None:
    checks = iter((True, False))
    gateway, guard, servers, _runtime, _env = _gateway(
        tmp_path,
        control_owner=lambda: next(checks, False),
        env_overrides={
            "MAGI_V3_GATEWAY_OWNERSHIP_CHECK_INTERVAL_SEC": "0.05",
            "MAGI_V3_GATEWAY_OWNERSHIP_LOSS_CONFIRMATIONS": "1",
        },
    )
    with pytest.raises(GatewayRuntimeError, match="control failed: same-release control role lost ownership"):
        gateway.serve_forever()
    assert guard.acquired is False
    assert all(server.close_calls == 1 for server in servers)


def test_serve_loop_tolerates_transient_control_owner_probe_loss(tmp_path: Path) -> None:
    checks = iter((True, False, True))
    gateway, guard, servers, _runtime, _env = _gateway(
        tmp_path,
        control_owner=lambda: next(checks, True),
        env_overrides={
            "MAGI_V3_GATEWAY_OWNERSHIP_CHECK_INTERVAL_SEC": "0.05",
            "MAGI_V3_GATEWAY_OWNERSHIP_LOSS_CONFIRMATIONS": "3",
        },
    )
    shutdown = threading.Timer(0.2, gateway.request_shutdown)
    shutdown.start()
    gateway.serve_forever()
    shutdown.join(timeout=1)
    assert all(server.run_entered.is_set() for server in servers)
    assert guard.acquired is False
    assert all(server.close_calls == 1 for server in servers)


def test_app_factory_failure_closes_role_guard_without_creating_any_socket(tmp_path: Path) -> None:
    server_calls = 0

    def fail() -> Any:
        raise RuntimeError("factory failed")

    def server_factory(*_args: Any) -> Any:
        nonlocal server_calls
        server_calls += 1
        raise AssertionError("server factory must not run")

    gateway, guard, _servers, runtime, _env = _gateway(
        tmp_path,
        factories={"main_http": lambda: _fallback_app, "tools_http": fail},
        server_factory=server_factory,
    )
    with pytest.raises(RuntimeError, match="factory failed"):
        gateway.start()
    assert server_calls == 0
    assert guard.acquired is False
    assert runtime.active_guard.acquired is False


def test_waitress_adapter_uses_bounded_production_settings_and_graceful_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class Dispatcher:
        def shutdown(self, **kwargs: Any) -> None:
            seen["shutdown"] = kwargs

    class RawServer:
        task_dispatcher = Dispatcher()

        def run(self) -> None:
            seen["ran"] = True

        def close(self) -> None:
            seen["closed"] = seen.get("closed", 0) + 1

    def create_server(app: Any, **kwargs: Any) -> RawServer:
        seen["app"] = app
        seen["settings"] = kwargs
        return RawServer()

    monkeypatch.setitem(sys.modules, "waitress", SimpleNamespace(create_server=create_server))
    service = load_service_manifest(SERVICE_MANIFEST).service("main_http")
    config = GatewayConfig(threads=6, channel_timeout=90, shutdown_grace_sec=7)
    handle = create_waitress_server(_fallback_app, service, config)
    handle.run()
    handle.close()
    handle.close()

    assert seen["ran"] is True
    assert seen["closed"] == 1
    assert seen["shutdown"] == {"cancel_pending": False, "timeout": 7}
    assert seen["settings"] == {
        "host": "127.0.0.1",
        "port": 5002,
        "threads": 6,
        "channel_timeout": 90,
        "cleanup_interval": 15,
        "clear_untrusted_proxy_headers": True,
        "ident": "MAGI-v3-main_http",
    }


def test_signal_style_shutdown_drains_both_servers_and_restores_handlers(tmp_path: Path) -> None:
    gateway, guard, servers, _runtime, _env = _gateway(tmp_path)
    original_term = signal.getsignal(signal.SIGTERM)
    timer = threading.Timer(0.05, gateway.request_shutdown)
    timer.start()
    try:
        gateway.serve_forever()
    finally:
        timer.join(timeout=1)
    assert guard.acquired is False
    assert all(server.close_calls == 1 for server in servers)
    assert signal.getsignal(signal.SIGTERM) is original_term


def test_server_failure_requests_full_shutdown_and_is_reported(tmp_path: Path) -> None:
    class FailedServer(FakeServer):
        def run(self) -> None:
            self.run_entered.set()
            raise RuntimeError("listener died")

    servers: list[FakeServer] = []

    def server_factory(app: Any, service: Any, _config: Any) -> FakeServer:
        server = (
            FailedServer(app, service.service_id, service.port)
            if service.service_id == "main_http"
            else FakeServer(app, service.service_id, service.port)
        )
        servers.append(server)
        return server

    gateway, guard, _ignored, _runtime, _env = _gateway(tmp_path, server_factory=server_factory)
    with pytest.raises(GatewayRuntimeError, match="main_http failed: listener died"):
        gateway.serve_forever()
    assert guard.acquired is False
    assert all(server.close_calls == 1 for server in servers)

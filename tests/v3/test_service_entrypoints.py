from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from email.message import Message
from pathlib import Path

import pytest

from magi_v3.compat.admin import create_admin_server
from magi_v3.control import (
    ControlHealthApplication,
    ControlService,
    build_supervisor_dependency_probe,
)
from magi_v3.health import HealthReport
from magi_v3.service_manifest import load_service_manifest
from magi_v3.service_runtime import (
    DefaultOwnershipProbe,
    ProcessRecord,
    RoleLease,
    ServiceIdentity,
    ServiceRuntimeError,
    _default_listener_reader,
    _default_process_reader,
    _default_role_owner_reader,
    _command_mentions_root,
    verify_role_owner,
)
from magi_v3.supervisor_service import (
    ManifestProcessSupervisor,
    SupervisorService,
    build_supervisor_service,
    load_supervisor_environment,
)

ROOT = Path(__file__).resolve().parents[2]
SERVICE_MANIFEST = ROOT / "config" / "v3_service_manifest.json"


def identity(tmp_path: Path, role: str, release: Path | None = None) -> ServiceIdentity:
    release_root = release or tmp_path / "release"
    release_root.mkdir(parents=True, exist_ok=True)
    manifest = release_root / "release-manifest.json"
    manifest.touch(exist_ok=True)
    runtime = tmp_path / "runtime"
    release_files = {
        path.relative_to(release_root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in release_root.rglob("*")
        if path.is_file()
    }
    executable = release_root / "bin" / "python3"
    env_file = tmp_path / "inputs" / "magi.env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    if not env_file.exists():
        env_file.write_text("DISCORD_TOKEN=test-token\n", encoding="utf-8")
        env_file.chmod(0o600)
    return ServiceIdentity(
        role=role,
        release_id="v3-test",
        release_root=release_root,
        release_manifest=manifest,
        release_manifest_sha256="0" * 64,
        runtime_root=runtime,
        pid_file=runtime / "pids" / f"{role}.pid",
        executable_path=executable if executable.is_file() else None,
        release_files=release_files,
        env_file=env_file,
        env_file_sha256=hashlib.sha256(env_file.read_bytes()).hexdigest(),
    )


def test_importing_production_entrypoints_has_no_runtime_side_effects(tmp_path: Path) -> None:
    home = tmp_path / "empty-home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(ROOT),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import magi_v3.control, magi_v3.supervisor_service, magi_v3.service_runtime",
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert list(home.rglob("*")) == []


def test_default_ownership_probe_rejects_v2_launchd_process_and_foreign_ports(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    v2 = tmp_path / "MAGI_v2"
    v2.mkdir()
    v3_record = ProcessRecord(100, 100, f"{release}/bin/python -m magi_v3.control")
    listeners = {5002: frozenset(), 5003: frozenset(), 8088: frozenset({100})}
    probe = DefaultOwnershipProbe(
        v2_root=v2,
        process_reader=lambda: (v3_record,),
        listener_reader=lambda port: listeners[port],
        v2_launchagent_loaded=lambda: False,
        role_owner_reader=lambda _root: frozenset(),
    )
    probe.assert_exclusive(release)

    with pytest.raises(ServiceRuntimeError, match="V2 launchagent"):
        DefaultOwnershipProbe(
            v2_root=v2,
            process_reader=lambda: (v3_record,),
            listener_reader=lambda _port: frozenset(),
            v2_launchagent_loaded=lambda: True,
            role_owner_reader=lambda _root: frozenset(),
        ).assert_exclusive(release)
    with pytest.raises(ServiceRuntimeError, match="V2 release process"):
        DefaultOwnershipProbe(
            v2_root=v2,
            process_reader=lambda: (ProcessRecord(20, 20, f"python {v2}/daemon.py"),),
            listener_reader=lambda _port: frozenset(),
            v2_launchagent_loaded=lambda: False,
            role_owner_reader=lambda _root: frozenset(),
        ).assert_exclusive(release)
    with pytest.raises(ServiceRuntimeError, match="foreign or unclassified"):
        DefaultOwnershipProbe(
            v2_root=v2,
            process_reader=lambda: (v3_record,),
            listener_reader=lambda port: frozenset({999}) if port == 5002 else frozenset(),
            v2_launchagent_loaded=lambda: False,
            role_owner_reader=lambda _root: frozenset(),
        ).assert_exclusive(release)


def test_command_root_matching_never_resolves_process_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path("/opt/magi/releases/v3-test")

    def reject_resolve(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("process token matching must not touch the filesystem")

    monkeypatch.setattr(Path, "resolve", reject_resolve)

    assert _command_mentions_root(
        "python /opt/magi/releases/v3-test/app.py "
        "/mnt/offline-nas/case.pdf",
        root,
    )
    assert not _command_mentions_root(
        "python /mnt/offline-nas/case.pdf",
        root,
    )
    assert not _command_mentions_root(
        "python /opt/magi/releases/v3-test-old/app.py",
        root,
    )


def test_default_ownership_probe_accepts_release_managed_listener_process_group(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    v2 = tmp_path / "MAGI_v2"
    v2.mkdir()
    launcher = ProcessRecord(100, 700, f"{release}/bin/python -m magi_v3.gateway")
    managed_child = ProcessRecord(101, 700, "/opt/homebrew/bin/caddy run")
    probe = DefaultOwnershipProbe(
        v2_root=v2,
        process_reader=lambda: (launcher, managed_child),
        listener_reader=lambda port: frozenset({101}) if port == 8088 else frozenset(),
        v2_launchagent_loaded=lambda: False,
        role_owner_reader=lambda _root: frozenset(),
    )

    probe.assert_exclusive(release)

    with pytest.raises(ServiceRuntimeError, match="foreign or unclassified"):
        DefaultOwnershipProbe(
            v2_root=v2,
            process_reader=lambda: (launcher, ProcessRecord(102, 702, managed_child.command)),
            listener_reader=lambda port: frozenset({102}) if port == 8088 else frozenset(),
            v2_launchagent_loaded=lambda: False,
            role_owner_reader=lambda _root: frozenset(),
        ).assert_exclusive(release)


def test_default_ownership_probe_accepts_its_own_bound_listener(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    v2 = tmp_path / "MAGI_v2"
    v2.mkdir()
    own_process = ProcessRecord(321, 321, "/external/runtime/python -c service")

    DefaultOwnershipProbe(
        v2_root=v2,
        process_reader=lambda: (own_process,),
        listener_reader=lambda port: frozenset({321}) if port == 8088 else frozenset(),
        v2_launchagent_loaded=lambda: False,
        current_pid=lambda: 321,
        role_owner_reader=lambda _root: frozenset(),
    ).assert_exclusive(release)


def test_default_ownership_probe_accepts_verified_same_release_role_listener(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    v2 = tmp_path / "MAGI_v2"
    v2.mkdir()
    sibling = ProcessRecord(654, 654, "/external/runtime/python -c service")

    DefaultOwnershipProbe(
        v2_root=v2,
        process_reader=lambda: (sibling,),
        listener_reader=lambda port: frozenset({654}) if port == 8088 else frozenset(),
        v2_launchagent_loaded=lambda: False,
        current_pid=lambda: 321,
        role_owner_reader=lambda root: (
            frozenset({654}) if root == release.resolve() else frozenset()
        ),
    ).assert_exclusive(release)


def test_default_role_owner_reader_requires_live_matching_role_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "release-manifest.json").write_text(
        json.dumps({"release_id": "v3-test"}),
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    service_identity = ServiceIdentity(
        role="control",
        release_id="v3-test",
        release_root=release,
        release_manifest=release / "release-manifest.json",
        release_manifest_sha256="0" * 64,
        runtime_root=runtime,
        pid_file=runtime / "pids/control.pid",
    )
    monkeypatch.setattr(
        "magi_v3.service_runtime._canonical_runtime_root",
        lambda: runtime,
    )
    lease = RoleLease(service_identity)
    lease.acquire()
    try:
        assert _default_role_owner_reader(release) == frozenset({os.getpid()})
    finally:
        lease.release()
    assert _default_role_owner_reader(release) == frozenset()


def test_default_listener_probe_uses_nonblocking_lsof_and_retries_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], float]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, float(kwargs["timeout"])))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, "p123\np456\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert _default_listener_reader(5002) == frozenset({123, 456})
    assert len(calls) == 2
    assert calls[0][0][:3] == ["/usr/sbin/lsof", "-b", "-nP"]
    assert calls[0][1] == 3


def test_default_listener_probe_scopes_lsof_to_release_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, "p123\n", "")

    monkeypatch.setattr(subprocess, "run", run)

    assert _default_listener_reader(5002, frozenset({456, 123})) == frozenset({123})
    assert commands == [[
        "/usr/sbin/lsof",
        "-b",
        "-nP",
        "-a",
        "-p",
        "123,456",
        "-iTCP:5002",
        "-sTCP:LISTEN",
        "-Fp",
    ]]


def test_default_listener_probe_converts_repeated_timeout_to_fail_closed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ServiceRuntimeError, match="timed out for 5003 after 3 attempts"):
        _default_listener_reader(5003)
    assert calls == 3


def test_default_process_probe_retries_transient_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(float(kwargs["timeout"]))
        if len(calls) < 3:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(command, 0, "  41   41 /usr/bin/example\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    assert _default_process_reader() == (ProcessRecord(41, 41, "/usr/bin/example"),)
    assert calls == [5, 8, 12]


def test_default_process_probe_converts_repeated_timeout_to_fail_closed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(float(kwargs["timeout"]))
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ServiceRuntimeError, match="timed out after 3 attempts"):
        _default_process_reader()
    assert calls == [5, 8, 12]


def test_default_process_probe_tolerates_unrelated_non_utf8_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["text"] is True
        assert kwargs["errors"] == "replace"
        return subprocess.CompletedProcess(
            command,
            0,
            "  41   41 /usr/bin/example-\ufffd\n",
            "",
        )

    monkeypatch.setattr(subprocess, "run", run)

    assert _default_process_reader() == (
        ProcessRecord(41, 41, "/usr/bin/example-\ufffd"),
    )


def test_role_lease_records_and_verifies_same_release_pid_process_group(
    tmp_path: Path,
) -> None:
    service_identity = identity(tmp_path, "control")
    lease = RoleLease(service_identity, pid=lambda: 41, process_group=lambda _pid: 410)
    lease.acquire()
    payload = json.loads(service_identity.pid_file.read_text(encoding="utf-8"))

    assert payload["pid"] == 41
    assert payload["process_group"] == 410
    assert verify_role_owner(
        service_identity.pid_file,
        role="control",
        release_id="v3-test",
        release_root=service_identity.release_root,
        pid_alive=lambda pid: pid == 41,
        process_group=lambda _pid: 410,
    )
    with pytest.raises(ServiceRuntimeError, match="already active"):
        RoleLease(service_identity, pid=lambda: 42).acquire()
    assert not verify_role_owner(
        service_identity.pid_file,
        role="control",
        release_id="another-release",
        release_root=service_identity.release_root,
        pid_alive=lambda _pid: True,
        process_group=lambda _pid: 410,
    )

    lease.release()
    assert not service_identity.pid_file.exists()


def test_service_identity_requires_manifest_bound_release_executable(tmp_path: Path) -> None:
    release = tmp_path / "release"
    executable = release / "bin" / "python3"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    files = [
        {
            "path": "bin/python3",
            "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        }
    ]
    release_manifest = release / "release-manifest.json"
    release_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "immutable": True,
                "release_id": "v3-test",
                "files": files,
            }
        ),
        encoding="utf-8",
    )
    runtime = tmp_path / "runtime"
    env_file = tmp_path / "inputs" / "magi.env"
    env_file.parent.mkdir()
    env_file.write_text("DISCORD_TOKEN=bound-token\n", encoding="utf-8")
    env_file.chmod(0o600)
    env = {
        "MAGI_V3_ROLE": "supervisor",
        "MAGI_V3_RELEASE_ID": "v3-test",
        "MAGI_V3_RELEASE_MANIFEST": str(release_manifest),
        "MAGI_V3_RELEASE_MANIFEST_SHA256": hashlib.sha256(
            release_manifest.read_bytes()
        ).hexdigest(),
        "MAGI_V3_EXECUTABLE_PATH": str(executable),
        "MAGI_V3_STATE_DIR": str(runtime / "state" / "supervisor"),
        "MAGI_V3_PID_FILE": str(runtime / "pids" / "supervisor.pid"),
        "MAGI_ENV_FILE": str(env_file),
        "MAGI_ENV_FILE_SHA256": hashlib.sha256(env_file.read_bytes()).hexdigest(),
    }

    resolved = ServiceIdentity.from_environment(
        "supervisor",
        env,
        canonical_runtime_root=runtime,
    )
    assert resolved.executable_path == executable
    assert resolved.env_file == env_file

    external = tmp_path / "external-python"
    external.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    external.chmod(0o755)
    with pytest.raises(ServiceRuntimeError, match="inside the immutable release"):
        ServiceIdentity.from_environment(
            "supervisor",
            {**env, "MAGI_V3_EXECUTABLE_PATH": str(external)},
            canonical_runtime_root=runtime,
        )

    env_file.chmod(0o644)
    with pytest.raises(ServiceRuntimeError, match="exactly 0600"):
        ServiceIdentity.from_environment(
            "supervisor",
            env,
            canonical_runtime_root=runtime,
        )
    env_file.chmod(0o600)
    with pytest.raises(ServiceRuntimeError, match="SHA-256 mismatch"):
        ServiceIdentity.from_environment(
            "supervisor",
            {**env, "MAGI_ENV_FILE_SHA256": "f" * 64},
            canonical_runtime_root=runtime,
        )
    symlink = tmp_path / "inputs" / "linked.env"
    symlink.symlink_to(env_file)
    with pytest.raises(ServiceRuntimeError, match="must not be a symlink"):
        ServiceIdentity.from_environment(
            "supervisor",
            {**env, "MAGI_ENV_FILE": str(symlink)},
            canonical_runtime_root=runtime,
        )


class FakeHealth:
    @staticmethod
    def liveness() -> HealthReport:
        return HealthReport("live", True, "now", {"process": "ok"})

    @staticmethod
    def readiness() -> HealthReport:
        return HealthReport("not_ready", False, "now", {"ledger": "unavailable"})


class FakeRuntime:
    def __init__(self) -> None:
        self.health = FakeHealth()
        self.activated = False
        self.closed = False

    def activate(self) -> None:
        self.activated = True

    def close(self) -> None:
        self.closed = True


class FakeLease:
    def __init__(self) -> None:
        self.acquired = False
        self.released = False

    def acquire(self) -> None:
        self.acquired = True

    def release(self) -> None:
        self.released = True


class FakeProbe:
    def __init__(self) -> None:
        self.calls = 0

    def assert_exclusive(self, _release_root: Path) -> None:
        self.calls += 1


class FakeHTTPServer:
    timeout = 0.0

    def __init__(self) -> None:
        self.closed = False
        self.on_request = lambda: None

    def handle_request(self) -> None:
        self.on_request()

    def server_close(self) -> None:
        self.closed = True


def test_control_owns_global_runtime_and_exposes_8088_health_ready_without_socket(
    tmp_path: Path,
) -> None:
    manifest = load_service_manifest(SERVICE_MANIFEST)
    runtime = FakeRuntime()
    application = ControlHealthApplication(runtime)  # type: ignore[arg-type]
    assert application.response("/livez")[0] == 200
    assert application.response("/readyz")[0] == 503
    assert application.response("/health")[0] == 503
    assert application.response("/admin")[0] == 404

    lease = FakeLease()
    probe = FakeProbe()
    server = FakeHTTPServer()
    website_root = tmp_path / "website"
    (website_root / "admin").mkdir(parents=True)
    (website_root / "data").mkdir()
    (website_root / "assets").mkdir()
    (website_root / "admin" / "admin_server.py").write_text("pass\n", encoding="utf-8")
    registered: dict[int, object] = {}
    factory_calls: list[dict[str, object]] = []
    resolved_factories: list[str] = []

    def server_factory(**kwargs):
        factory_calls.append(kwargs)
        return server

    def factory_resolver(reference: str):
        resolved_factories.append(reference)
        return server_factory

    def register(signum: int, handler):
        previous = registered.get(signum, signal.SIG_DFL)
        registered[signum] = handler
        return previous

    service = ControlService(
        runtime=runtime,  # type: ignore[arg-type]
        manifest=manifest,
        identity=identity(tmp_path, "control"),
        role_lease=lease,  # type: ignore[arg-type]
        ownership_probe=probe,
        website_root=website_root,
        website_admin_sha256="0" * 64,
        factory_resolver=factory_resolver,
        signal_registrar=register,
    )
    def request_shutdown() -> None:
        handler = registered[signal.SIGTERM]
        assert callable(handler)
        handler(signal.SIGTERM, None)

    server.on_request = request_shutdown

    assert service.run() == 0
    assert runtime.activated and runtime.closed
    assert lease.acquired and lease.released
    assert server.closed
    assert probe.calls == 1
    assert factory_calls[0]["server_address"] == ("127.0.0.1", 8088)
    assert factory_calls[0]["website_root"] == website_root
    assert factory_calls[0]["website_admin_sha256"] == "0" * 64
    assert resolved_factories == ["magi_v3.compat:create_admin_server"]


def test_control_readiness_requires_supervisor_role_and_every_managed_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_service_manifest(SERVICE_MANIFEST)
    service_identity = identity(tmp_path, "control")
    monkeypatch.setattr("magi_v3.control.verify_role_owner", lambda *args, **kwargs: True)
    for index, service in enumerate(manifest.for_role("supervisor"), start=100):
        path = service_identity.runtime_root / "pids" / f"service-{service.service_id}.pid"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "service_id": service.service_id,
                    "release_id": service_identity.release_id,
                    "release_root": str(service_identity.release_root),
                    "pid": index,
                    "process_group": index,
                }
            ),
            encoding="utf-8",
        )
    dependency = build_supervisor_dependency_probe(
        service_identity,
        manifest,
        process_exists=lambda pid: True,
        process_group=lambda pid: pid,
        process_group_exists=lambda group: True,
    )

    ready, details = dependency()
    assert ready is True
    assert all(details["children"].values())

    missing = service_identity.runtime_root / "pids" / "service-legacy_background.pid"
    missing.unlink()
    ready, details = dependency()
    assert ready is False
    assert details["children"]["legacy_background"] is False


def test_admin_overlay_preserves_legacy_session_multipart_and_mutable_data_roots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    website_root = Path(
        os.environ.get("MAGI_WEBSITE_ROOT", str(ROOT / "whalechao.github.io"))
    ).expanduser().resolve()
    source = website_root / "admin" / "admin_server.py"
    if not source.is_file():
        pytest.skip("external Website Admin checkout is not available to this release test")
    monkeypatch.setenv("MAGI_WEBSITE_ADMIN_SHA256", hashlib.sha256(source.read_bytes()).hexdigest())

    class Server:
        def __init__(self, address, handler):
            self.address = address
            self.handler = handler

    server = create_admin_server(
        server_address=("127.0.0.1", 8088),
        health_application=ControlHealthApplication(FakeRuntime()),  # type: ignore[arg-type]
        website_root=website_root,
        server_factory=Server,
    )
    legacy_handler = server.handler.__mro__[1]
    legacy_globals = legacy_handler.do_GET.__globals__

    assert server.handler.do_POST is legacy_handler.do_POST
    assert server.handler.handle_photo_upload is legacy_handler.handle_photo_upload
    assert legacy_globals["REPO_ROOT"] == website_root
    assert legacy_globals["DATA_FILE"] == website_root / "data" / "site-data.json"
    assert legacy_globals["CONTENT_FILE"] == website_root / "data" / "content.json"
    assert legacy_globals["ASSETS_DIR"] == website_root / "assets"

    token = "test-session-token"
    legacy_globals["VALID_TOKENS"].add(token)
    request = legacy_handler.__new__(legacy_handler)
    request.headers = Message()
    request.headers["Cookie"] = f"session={token}"
    assert request.check_auth() is True

    boundary = "v3-boundary"
    photo = b"\x00\xffphoto-bytes"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="photo"; filename="proof.jpg"\r\n'
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode() + photo + f"\r\n--{boundary}--\r\n".encode()
    assert legacy_globals["parse_multipart"](f"multipart/form-data; boundary={boundary}", body) == (
        "proof.jpg",
        photo,
    )


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


def _supervisor_fixture(tmp_path: Path):
    release = tmp_path / "release"
    executable = release / "bin" / "python3"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    for relative in (
        "api/discord_bot.py",
        "skills/ops/file_review_auto_worker.py",
        "skills/ops/heartbeat.py",
        "magi_v3/legacy_background_service.py",
        "scripts/ops/osc_shell_nas_helper.py",
        "gui/magi_menubar.py",
        "daemon.py",
    ):
        path = release / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n", encoding="utf-8")
    service_identity = identity(tmp_path, "supervisor", release)
    manifest = load_service_manifest(SERVICE_MANIFEST)
    processes: dict[int, FakeProcess] = {}
    calls: list[tuple[list[str], dict[str, object]]] = []
    commands: dict[int, tuple[str, ...]] = {}
    groups: dict[int, int] = {}
    next_pid = 700

    def factory(argv, **kwargs):
        nonlocal next_pid
        next_pid += 1
        process = FakeProcess(next_pid)
        processes[next_pid] = process
        groups[next_pid] = next_pid
        commands[next_pid] = tuple(argv)
        calls.append((argv, kwargs))
        return process

    signals: list[tuple[int, int]] = []

    def signal_group(pgid: int, signum: int) -> None:
        signals.append((pgid, signum))
        processes[pgid].returncode = -signum

    clock = [0.0]
    supervisor = ManifestProcessSupervisor(
        manifest=manifest,
        identity=service_identity,
        python_executable=executable,
        process_factory=factory,
        process_group=lambda pid: groups[pid],
        signal_group=signal_group,
        group_exists=lambda pgid: processes[pgid].returncode is None,
        pid_alive=lambda pid: pid in processes and processes[pid].returncode is None,
        process_argv=lambda pid: commands.get(pid, ()),
        monotonic=lambda: clock[0],
        environ={"LANG": "C", "DISCORD_TOKEN": "bound-token"},
    )
    return supervisor, processes, calls, groups, signals, clock, service_identity


def test_supervisor_loads_hash_bound_dotenv_into_child_environment_without_global_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    *_, service_identity = _supervisor_fixture(tmp_path)
    observed: dict[str, object] = {}

    def dotenv_reader(**kwargs):
        observed.update(kwargs)
        return {"DISCORD_TOKEN": "from-file", "FILE_ONLY": "loaded", "UNSET": None}

    monkeypatch.delenv("FILE_ONLY", raising=False)
    child_environment = load_supervisor_environment(
        service_identity,
        base_environ={"DISCORD_TOKEN": "from-launchd", "PATH": "/bound/bin"},
        dotenv_reader=dotenv_reader,
    )

    assert child_environment == {
        "DISCORD_TOKEN": "from-launchd",
        "FILE_ONLY": "loaded",
        "PATH": "/bound/bin",
    }
    assert observed["dotenv_path"] == service_identity.env_file
    assert observed["encoding"] == "utf-8"
    assert observed["interpolate"] is True
    assert "FILE_ONLY" not in os.environ

    service = build_supervisor_service(
        service_identity,
        manifest_path=SERVICE_MANIFEST,
        ownership_probe=FakeProbe(),
        python_executable=service_identity.executable_path,
        environ={"DISCORD_TOKEN": "from-launchd", "PATH": "/bound/bin"},
        dotenv_reader=dotenv_reader,
    )
    assert service.process_supervisor.environ["FILE_ONLY"] == "loaded"
    assert service.process_supervisor.environ["DISCORD_TOKEN"] == "from-launchd"


def test_supervisor_starts_only_manifest_non_http_release_children_and_restarts(
    tmp_path: Path,
) -> None:
    supervisor, processes, calls, _groups, signals, clock, service_identity = _supervisor_fixture(
        tmp_path
    )
    supervisor.start_all()

    assert set(supervisor.children) == {
        "discord",
        "file_review_auto",
        "heartbeat",
        "legacy_background",
        "osc_shell_nas_helper",
        "menubar",
    }
    assert len(calls) == 6
    for argv, kwargs in calls:
        assert argv[0].startswith(str(service_identity.release_root))
        assert Path(argv[1]).resolve().is_relative_to(service_identity.release_root)
        assert kwargs["cwd"] == str(service_identity.release_root)
        expected_env = {"LANG": "C", "DISCORD_TOKEN": "bound-token"}
        if Path(argv[1]).name == "discord_bot.py":
            expected_env["MAGI_INTERNAL_CRON_ENABLED"] = "0"
        assert kwargs["env"] == expected_env
        assert kwargs["shell"] is False
        assert kwargs["start_new_session"] is True
    assert not any("server.py" == Path(argv[1]).name for argv, _kwargs in calls)
    assert not any(Path(argv[1]).name == "daemon.py" for argv, _kwargs in calls)

    old_pid = supervisor.children["discord"].process.pid
    processes[old_pid].returncode = 1
    supervisor.tick()
    assert "discord" not in supervisor.children
    clock[0] = 1.0
    supervisor.tick()
    assert supervisor.children["discord"].process.pid != old_pid

    supervisor.shutdown(grace_sec=0.1)
    assert not supervisor.children
    assert all(signum == signal.SIGTERM for _pgid, signum in signals)
    assert not list((service_identity.runtime_root / "pids").glob("service-*.pid"))


def test_supervisor_recycles_only_exact_same_release_orphans_after_forced_restart(
    tmp_path: Path,
) -> None:
    supervisor, processes, calls, groups, signals, clock, service_identity = _supervisor_fixture(
        tmp_path
    )
    supervisor.start_all()
    old_pids = {service_id: child.process.pid for service_id, child in supervisor.children.items()}

    restarted = ManifestProcessSupervisor(
        manifest=supervisor.manifest,
        identity=service_identity,
        python_executable=supervisor.python_executable,
        process_factory=supervisor.process_factory,
        process_group=supervisor.process_group,
        signal_process=supervisor.signal_process,
        signal_group=supervisor.signal_group,
        group_exists=supervisor.group_exists,
        pid_alive=supervisor.pid_alive,
        process_argv=supervisor.process_argv,
        monotonic=lambda: clock[0],
        environ=supervisor.environ,
    )
    restarted.start_all()

    assert set(restarted.children) == set(old_pids)
    assert all(processes[pid].returncode == -signal.SIGTERM for pid in old_pids.values())
    assert all(restarted.children[key].process.pid != pid for key, pid in old_pids.items())
    assert {(pid, signal.SIGTERM) for pid in old_pids.values()} <= set(signals)
    restarted.shutdown(grace_sec=0.1)


def test_supervisor_refuses_orphan_when_bound_receipt_command_does_not_match(
    tmp_path: Path,
) -> None:
    supervisor, processes, _calls, _groups, signals, _clock, service_identity = _supervisor_fixture(
        tmp_path
    )
    supervisor.start_all()
    first = next(iter(supervisor.children.values()))
    guarded = ManifestProcessSupervisor(
        manifest=supervisor.manifest,
        identity=service_identity,
        python_executable=supervisor.python_executable,
        process_factory=supervisor.process_factory,
        process_group=supervisor.process_group,
        signal_process=supervisor.signal_process,
        signal_group=supervisor.signal_group,
        group_exists=supervisor.group_exists,
        pid_alive=supervisor.pid_alive,
        process_argv=lambda _pid: ("/usr/bin/python3", "/tmp/not-magi.py"),
        environ=supervisor.environ,
    )

    with pytest.raises(ServiceRuntimeError, match="mismatched command"):
        guarded.start_all()
    assert processes[first.process.pid].returncode is None
    assert (first.process_group, signal.SIGTERM) not in signals
    supervisor.shutdown(grace_sec=0.1)


def test_supervisor_refuses_changed_process_group_and_requires_control_owner(
    tmp_path: Path,
) -> None:
    supervisor, _processes, _calls, groups, signals, _clock, service_identity = _supervisor_fixture(
        tmp_path
    )
    supervisor.start_all()
    target = supervisor.children["discord"].process.pid
    groups[target] = target + 1000
    with pytest.raises(ServiceRuntimeError, match="could not be fenced"):
        supervisor.shutdown(grace_sec=0.1)
    assert not any(pgid in {target, target + 1000} for pgid, _signum in signals)

    class NeverStart:
        started = False

        def start_all(self) -> None:
            self.started = True

        def shutdown(self) -> None:
            return

        def tick(self) -> None:
            return

    never = NeverStart()
    lease = FakeLease()
    with pytest.raises(ServiceRuntimeError, match="control owner"):
        SupervisorService(
            identity=service_identity,
            role_lease=lease,  # type: ignore[arg-type]
            ownership_probe=FakeProbe(),
            process_supervisor=never,  # type: ignore[arg-type]
            control_owner=lambda: False,
        ).run()
    assert not lease.acquired
    assert not never.started


def test_supervisor_refuses_orphan_process_group_from_stale_pid_record(tmp_path: Path) -> None:
    supervisor, _processes, calls, _groups, _signals, _clock, service_identity = (
        _supervisor_fixture(tmp_path)
    )
    pid_file = service_identity.runtime_root / "pids" / "service-discord.pid"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text(
        json.dumps({"pid": 777, "process_group": 888}),
        encoding="utf-8",
    )
    supervisor.group_exists = lambda pgid: pgid == 888

    with pytest.raises(ServiceRuntimeError, match="orphan child"):
        supervisor.start_all()

    assert calls == []
    assert pid_file.exists()


def test_supervisor_signal_handler_gracefully_stops_children_and_releases_role(
    tmp_path: Path,
) -> None:
    class Processes:
        started = False
        stopped = False

        def start_all(self) -> None:
            self.started = True

        def shutdown(self) -> None:
            self.stopped = True

        def tick(self) -> None:
            return

    processes = Processes()
    lease = FakeLease()
    handlers: dict[int, object] = {}

    def register(signum: int, handler):
        previous = handlers.get(signum, signal.SIG_DFL)
        handlers[signum] = handler
        if signum == signal.SIGTERM and callable(handler):
            handler(signum, None)
        return previous

    service = SupervisorService(
        identity=identity(tmp_path, "supervisor"),
        role_lease=lease,  # type: ignore[arg-type]
        ownership_probe=FakeProbe(),
        process_supervisor=processes,  # type: ignore[arg-type]
        control_owner=lambda: True,
        signal_registrar=register,
    )

    assert service.run() == 0
    assert processes.started and processes.stopped
    assert lease.acquired and lease.released

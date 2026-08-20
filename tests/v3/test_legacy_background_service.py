from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from magi_v3 import legacy_background_service as service

ROOT = Path(__file__).resolve().parents[2]


def test_import_does_not_start_thread_socket_or_process() -> None:
    code = """
import socket
import subprocess
import sys
import threading

def blocked(*args, **kwargs):
    raise AssertionError("import attempted a runtime side effect")

socket.socket = blocked
socket.create_connection = blocked
subprocess.Popen = blocked
threading.Thread.start = blocked
import magi_v3.legacy_background_service
assert "api.startup" not in sys.modules
assert "api.nas_mount_guard" not in sys.modules
assert "api.db_failover" not in sys.modules
assert "skills.memory.keeper_sync" not in sys.modules
"""
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr


def test_default_plan_owns_required_components_and_keeps_preloads_off(monkeypatch: pytest.MonkeyPatch) -> None:
    config = service.ServiceConfig(ROOT).validated()
    monkeypatch.setattr(service.importlib, "import_module", lambda name: pytest.fail(f"eager import: {name}"))

    specs = service.build_component_specs(config)

    assert tuple(spec.name for spec in specs) == service.REQUIRED_COMPONENTS
    assert not config.enable_faiss_preload
    assert not config.enable_omlx_warmup
    assert all(spec.name not in service.OPTIONAL_PRELOADS for spec in specs)


def test_preload_components_are_strictly_opt_in() -> None:
    specs = service.build_component_specs(
        service.ServiceConfig(ROOT, enable_faiss_preload=True, enable_omlx_warmup=True)
    )

    assert tuple(spec.name for spec in specs[-2:]) == service.OPTIONAL_PRELOADS
    assert all(spec.one_shot for spec in specs[-2:])


def test_periodic_loop_runs_immediately_and_stop_event_interrupts_wait() -> None:
    event = threading.Event()
    calls: list[str] = []

    def action() -> None:
        calls.append("tick")
        event.set()

    service._periodic(event, action, name="test", interval=60)

    assert calls == ["tick"]


def test_service_starts_one_shot_and_gracefully_stops_managed_loop() -> None:
    entered = threading.Event()
    exited = threading.Event()
    one_shot_calls: list[str] = []

    def one_shot(_stop: threading.Event) -> None:
        one_shot_calls.append("recovered")

    def loop(stop: threading.Event) -> None:
        entered.set()
        stop.wait()
        exited.set()

    manager = service.LegacyBackgroundService(
        (
            service.ComponentSpec("queue_recovery", one_shot, one_shot=True),
            service.ComponentSpec("watchdog", loop),
        ),
        join_timeout_seconds=2,
    )

    manager.start()
    assert entered.wait(1)
    for _ in range(100):
        if manager.status()["components"]["queue_recovery"]["status"] == "completed":
            break
        threading.Event().wait(0.005)
    manager.stop()

    report = manager.status()
    assert one_shot_calls == ["recovered"]
    assert exited.is_set()
    assert report["stop_requested"] is True
    assert report["components"]["queue_recovery"]["status"] == "completed"
    assert report["components"]["watchdog"]["status"] == "stopped"
    assert report["unhealthy"] == []


def test_component_failure_is_machine_visible_and_unhealthy() -> None:
    def failed(_stop: threading.Event) -> None:
        raise RuntimeError("boom")

    manager = service.LegacyBackgroundService((service.ComponentSpec("required", failed),))
    manager.start()
    manager._threads["required"].join(timeout=1)

    report = manager.status()

    assert report["unhealthy"] == ["required"]
    assert report["components"]["required"]["status"] == "failed"
    assert report["components"]["required"]["error"] == "RuntimeError: boom"


def test_queue_recovery_is_one_exact_legacy_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object]] = []
    queue = SimpleNamespace(
        recover_stale=lambda **kwargs: calls.append(("recover", kwargs)) or 2,
        cleanup_old=lambda **kwargs: calls.append(("cleanup", kwargs)) or 3,
    )
    module = SimpleNamespace(get_queue=lambda: queue)
    monkeypatch.setattr(service.importlib, "import_module", lambda name: module)

    service._queue_recovery(threading.Event())

    assert calls == [("recover", {"stale_seconds": 300}), ("cleanup", {"days": 7})]


def test_cloudflared_runner_reuses_single_action_without_legacy_infinite_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = threading.Event()
    calls: list[str] = []

    def ensure() -> None:
        calls.append("ensure")
        event.set()

    startup = SimpleNamespace(
        _stable_webhook_base_url=lambda: "",
        _truthy_env=lambda *args: False,
        _is_cloudflared_alive=lambda: False,
        _ensure_cloudflared=ensure,
    )
    monkeypatch.setattr(service.importlib, "import_module", lambda name: startup)
    config = service.ServiceConfig(
        ROOT,
        cloudflared_interval_seconds=60,
        cloudflared_initial_delay_seconds=0,
    )

    service._cloudflared_runner(config)(event)

    assert calls == ["ensure"]


def test_legacy_mail_monitor_defers_to_durable_business_owners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = threading.Event()
    monkeypatch.setenv("MAGI_ENABLE_BACKGROUND_FILE_REVIEW_CHECK", "1")
    monkeypatch.setenv("MAGI_ENABLE_BACKGROUND_LAF_CHECK", "1")
    monkeypatch.setenv("MAGI_LAF_PORTAL_RETRY_ON_START", "1")

    # An already-requested stop lets this unit verify that the compatibility
    # component remains supervised without importing or starting Gmail work.
    event.set()
    monkeypatch.setattr(
        service.importlib,
        "import_module",
        lambda name: pytest.fail(f"legacy mail owner must stay idle: {name}"),
    )

    service._laf_email_runner(service.ServiceConfig(ROOT))(event)

    assert os.environ["MAGI_ENABLE_BACKGROUND_FILE_REVIEW_CHECK"] == "0"
    assert os.environ["MAGI_ENABLE_BACKGROUND_LAF_CHECK"] == "0"
    assert os.environ["MAGI_LAF_PORTAL_RETRY_ON_START"] == "0"


def test_file_watcher_handle_is_stopped_on_service_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = threading.Event()
    event.set()
    calls: list[object] = []

    class Watcher:
        def stop(self) -> None:
            calls.append("stop")

    watcher = Watcher()

    def start_watcher(folders, *, callback):
        calls.append(tuple(folders))
        calls.append(callback)
        return watcher

    monkeypatch.setattr(
        service.importlib,
        "import_module",
        lambda name: SimpleNamespace(start_watcher=start_watcher),
    )

    release_root = tmp_path / "release"
    (release_root / "api").mkdir(parents=True)
    (release_root / "api" / "startup.py").write_text("", encoding="utf-8")
    (release_root / "daemon.py").write_text("", encoding="utf-8")
    (release_root / "閱卷下載").mkdir()
    service._file_watcher_runner(service.ServiceConfig(release_root))(event)

    assert calls[0] == (str(release_root / "閱卷下載"),)
    assert callable(calls[1])
    assert calls[2] == "stop"


def test_v3_background_monitor_skips_laf_and_file_review_business_work(monkeypatch: pytest.MonkeyPatch) -> None:
    from skills.legal import laf as laf_module
    from skills.legal.laf import LAFGmailMonitor

    monitor = LAFGmailMonitor.__new__(LAFGmailMonitor)
    monitor._running = True
    monitor.callback = None
    monitor.processed_exists_func = None
    monitor._reauth_if_needed = lambda: True
    monitor._write_monitor_state = lambda *_args, **_kwargs: None
    monitor._close_service = lambda: None
    monitor.check_general_emails = lambda *_args, **_kwargs: None
    monitor.check_emails = lambda **_kwargs: pytest.fail(
        "the V3 background loop must not own LAF mail"
    )
    observed: list[str] = []

    def check_file_review() -> None:
        observed.append("compatibility_poll")
        monitor._running = False

    monitor._check_filereview_emails = check_file_review
    monkeypatch.setenv("MAGI_ENABLE_BACKGROUND_LAF_CHECK", "0")
    monkeypatch.setenv("MAGI_ENABLE_BACKGROUND_FILE_REVIEW_CHECK", "0")
    monkeypatch.setattr(laf_module.time, "sleep", lambda _seconds: None)

    monitor._monitor_loop(300, True)

    # The compatibility loop may poll its disabled branch, but neither LAF nor
    # FileReview business work is performed by it.
    assert observed == ["compatibility_poll"]


def test_signal_handlers_only_request_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: dict[int, object] = {}
    event = threading.Event()
    monkeypatch.setattr(service.signal, "signal", lambda number, handler: installed.setdefault(number, handler))

    service.install_signal_handlers(event)
    installed[signal.SIGTERM](signal.SIGTERM, None)

    assert set(installed) == {signal.SIGTERM, signal.SIGINT}
    assert event.is_set()


def test_cli_requires_explicit_legacy_root_and_documents_preload_opt_in() -> None:
    help_text = service._parser().format_help()

    assert "--legacy-root" in help_text
    assert "--enable-faiss-preload" in help_text
    assert "--enable-omlx-warmup" in help_text
    with pytest.raises(SystemExit):
        service._parser().parse_args([])

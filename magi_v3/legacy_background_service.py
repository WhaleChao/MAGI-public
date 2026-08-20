"""Supervisor-owned legacy background responsibilities for MAGI V3.

This module is intentionally side-effect free at import time.  Legacy modules,
threads, sockets, subprocesses, filesystem watchers, and signal handlers are
only touched after :func:`main` or :func:`run_service` is called.
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import math
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

LOGGER = logging.getLogger("magi_v3.legacy_background")
REQUIRED_COMPONENTS = (
    "queue_recovery",
    "cron_scheduler",
    "cloudflared_watchdog",
    "paperclip_watchdog",
    "nas_mount_guard",
    "laf_file_review_email_monitor",
    "db_failover",
    "keeper_sync",
    "file_watcher",
)
OPTIONAL_PRELOADS = ("faiss_preload", "omlx_warmup")


class LegacyBackgroundError(RuntimeError):
    """The background service cannot safely start or remain healthy."""


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    legacy_root: Path
    cloudflared_interval_seconds: float = 90.0
    cloudflared_initial_delay_seconds: float = 60.0
    paperclip_interval_seconds: float = 120.0
    paperclip_initial_delay_seconds: float = 15.0
    nas_interval_seconds: float = 120.0
    db_interval_seconds: float = 600.0
    keeper_interval_seconds: float = 300.0
    join_timeout_seconds: float = 10.0
    enable_faiss_preload: bool = False
    enable_omlx_warmup: bool = False
    disabled_components: frozenset[str] = frozenset()

    def validated(self) -> "ServiceConfig":
        root = self.legacy_root.expanduser().resolve()
        if not (root / "api" / "startup.py").is_file() or not (root / "daemon.py").is_file():
            raise LegacyBackgroundError("legacy root must contain api/startup.py and daemon.py")
        intervals = {
            "cloudflared_interval_seconds": self.cloudflared_interval_seconds,
            "paperclip_interval_seconds": self.paperclip_interval_seconds,
            "nas_interval_seconds": self.nas_interval_seconds,
            "db_interval_seconds": self.db_interval_seconds,
            "keeper_interval_seconds": self.keeper_interval_seconds,
            "join_timeout_seconds": self.join_timeout_seconds,
        }
        invalid = [name for name, value in intervals.items() if not math.isfinite(value) or value <= 0]
        if invalid:
            raise LegacyBackgroundError(f"intervals must be positive: {', '.join(invalid)}")
        unknown = sorted(set(self.disabled_components) - set(REQUIRED_COMPONENTS))
        if unknown:
            raise LegacyBackgroundError(f"unknown disabled components: {', '.join(unknown)}")
        return ServiceConfig(
            legacy_root=root,
            cloudflared_interval_seconds=float(self.cloudflared_interval_seconds),
            cloudflared_initial_delay_seconds=max(0.0, float(self.cloudflared_initial_delay_seconds)),
            paperclip_interval_seconds=float(self.paperclip_interval_seconds),
            paperclip_initial_delay_seconds=max(0.0, float(self.paperclip_initial_delay_seconds)),
            nas_interval_seconds=float(self.nas_interval_seconds),
            db_interval_seconds=float(self.db_interval_seconds),
            keeper_interval_seconds=float(self.keeper_interval_seconds),
            join_timeout_seconds=float(self.join_timeout_seconds),
            enable_faiss_preload=self.enable_faiss_preload,
            enable_omlx_warmup=self.enable_omlx_warmup,
            disabled_components=frozenset(self.disabled_components),
        )


@dataclass(frozen=True, slots=True)
class ComponentSpec:
    name: str
    target: Callable[[threading.Event], None]
    one_shot: bool = False


@dataclass(slots=True)
class ComponentState:
    status: str = "pending"
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


def _module_origin_within(module: Any, root: Path) -> bool:
    origin = getattr(module, "__file__", None)
    if not origin:
        return False
    try:
        Path(origin).resolve().relative_to(root)
        return True
    except (OSError, ValueError):
        return False


def bind_legacy_root(root: Path) -> Path:
    """Bind imports to one explicit legacy source tree, failing on ambiguity."""

    resolved = root.expanduser().resolve()
    if not (resolved / "api" / "startup.py").is_file() or not (resolved / "daemon.py").is_file():
        raise LegacyBackgroundError("legacy root must contain api/startup.py and daemon.py")
    for package in ("api", "skills"):
        loaded = sys.modules.get(package)
        if loaded is not None and not _module_origin_within(loaded, resolved):
            raise LegacyBackgroundError(f"preloaded {package} package is outside selected legacy root")
    root_text = str(resolved)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    for relative in ("casper_ecosystem/law_firm_orchestrators", "skills/legal"):
        path = str(resolved / relative)
        if path not in sys.path:
            sys.path.insert(0, path)
    return resolved


def _periodic(
    stop_event: threading.Event,
    action: Callable[[], Any],
    *,
    name: str,
    interval: float,
    initial_delay: float = 0.0,
) -> None:
    if initial_delay and stop_event.wait(initial_delay):
        return
    while not stop_event.is_set():
        try:
            action()
        except Exception:
            LOGGER.exception("%s iteration failed", name)
        if stop_event.wait(interval):
            return


def _queue_recovery(_: threading.Event) -> None:
    queue = importlib.import_module("skills.memory.message_queue").get_queue()
    recovered = queue.recover_stale(stale_seconds=300)
    cleaned = queue.cleanup_old(days=7)
    LOGGER.info("queue recovery complete recovered=%s cleaned=%s", recovered, cleaned)


def _cron_runner(config: ServiceConfig) -> Callable[[threading.Event], None]:
    def run(stop_event: threading.Event) -> None:
        cron = importlib.import_module("magi_v3.cron_service")
        cron.run_cron_component(stop_event, config.legacy_root)

    return run


def _cloudflared_runner(config: ServiceConfig) -> Callable[[threading.Event], None]:
    def run(stop_event: threading.Event) -> None:
        startup = importlib.import_module("api.startup")

        def watchdog_check() -> None:
            stable = startup._stable_webhook_base_url()
            enabled = startup._truthy_env("MAGI_ENABLE_CLOUDFLARE_WEBHOOK", "0")
            if stable and not enabled:
                return
            if not startup._is_cloudflared_alive():
                startup._ensure_cloudflared()

        try:
            startup._ensure_cloudflared()
        except Exception:
            LOGGER.exception("cloudflared initial ensure failed")
        if stop_event.wait(config.cloudflared_initial_delay_seconds):
            return
        _periodic(
            stop_event,
            watchdog_check,
            name="cloudflared_watchdog",
            interval=config.cloudflared_interval_seconds,
        )

    return run


def _paperclip_runner(config: ServiceConfig) -> Callable[[threading.Event], None]:
    def run(stop_event: threading.Event) -> None:
        startup = importlib.import_module("api.startup")
        _periodic(
            stop_event,
            startup._ensure_paperclip_share_tunnel,
            name="paperclip_watchdog",
            interval=config.paperclip_interval_seconds,
            initial_delay=config.paperclip_initial_delay_seconds,
        )

    return run


def _nas_runner(config: ServiceConfig) -> Callable[[threading.Event], None]:
    def run(stop_event: threading.Event) -> None:
        guard = importlib.import_module("api.nas_mount_guard")
        _periodic(
            stop_event,
            guard.ensure_nas_mounts,
            name="nas_mount_guard",
            interval=config.nas_interval_seconds,
        )

    return run


def _db_failover_runner(config: ServiceConfig) -> Callable[[threading.Event], None]:
    def run(stop_event: threading.Event) -> None:
        failover = importlib.import_module("api.db_failover")
        _periodic(
            stop_event,
            failover._do_check,
            name="db_failover",
            interval=config.db_interval_seconds,
        )

    return run


def _keeper_runner(config: ServiceConfig) -> Callable[[threading.Event], None]:
    def run(stop_event: threading.Event) -> None:
        keeper = importlib.import_module("skills.memory.keeper_sync")

        def sync_once() -> None:
            if keeper.check_keeper_online():
                keeper.sync_to_keeper()

        _periodic(
            stop_event,
            sync_once,
            name="keeper_sync",
            interval=config.keeper_interval_seconds,
        )

    return run


def _laf_email_runner(_: ServiceConfig) -> Callable[[threading.Event], None]:
    def run(stop_event: threading.Event) -> None:
        # Mail and portal work each have one durable, observable owner.  LAF is
        # handled by laf_gmail_dispatch_scan.py; FileReview is handled by the
        # supervised file_review_auto worker.  This compatibility loop must not
        # race either owner or write a processed marker before its receipt.
        # These are ownership switches, not user preferences.  Older sealed
        # environments may still contain ``=1`` from the V2 compatibility
        # topology; setdefault would preserve that stale value and re-enable a
        # second Gmail/Chromium owner.  V3 has dedicated durable owners, so the
        # compatibility service must override the legacy settings explicitly.
        os.environ["MAGI_ENABLE_BACKGROUND_FILE_REVIEW_CHECK"] = "0"
        os.environ["MAGI_ENABLE_BACKGROUND_LAF_CHECK"] = "0"
        os.environ["MAGI_LAF_PORTAL_RETRY_ON_START"] = "0"
        # Do not instantiate the legacy Gmail monitor at all.  Even with both
        # business branches disabled it still authenticated, polled every five
        # minutes and wrote the same health artifact as the supervised scanner.
        # That created a second Gmail transport and a cross-process state-file
        # race while contributing no business work.  Keep the compatibility
        # component alive as an explicit idle owner so supervisor topology and
        # shutdown semantics remain stable.
        LOGGER.info(
            "legacy LAF/FileReview mail component idle; durable scheduled owners active"
        )
        stop_event.wait()

    return run


def _watch_folders(config: ServiceConfig) -> list[str]:
    folders: list[str] = []
    local_scan = config.legacy_root / "閱卷下載"
    if local_scan.is_dir():
        folders.append(str(local_scan))
    nas_enabled = os.environ.get("MAGI_ENABLE_NAS_FSWATCHER", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    nas_user = (
        os.environ.get("MAGI_NAS_HOME_USER") or os.environ.get("MAGI_NAS_USER") or "home"
    ).strip().strip("/\\") or "home"
    nas_volume_root = (os.environ.get("MAGI_NAS_HOME_VOLUME_ROOT") or "").strip()
    nas_cases = Path(nas_volume_root).expanduser() / nas_user / "01_案件" if nas_volume_root else None
    if nas_enabled and nas_cases is not None and nas_cases.is_dir():
        folders.append(str(nas_cases))
    return folders


def _file_watcher_runner(config: ServiceConfig) -> Callable[[threading.Event], None]:
    def run(stop_event: threading.Event) -> None:
        folders = _watch_folders(config)
        if not folders:
            LOGGER.info("file watcher idle: no configured folders are available")
            stop_event.wait()
            return
        watcher_module = importlib.import_module("skills.ops.fs_watcher")

        def on_new_file(event: dict[str, Any]) -> None:
            path = str(event.get("path") or "")
            if str(event.get("extension") or "").lower() != ".pdf":
                return
            LOGGER.info("new PDF observed: %s", os.path.basename(path))
            try:
                notify = importlib.import_module("skills.ops.macos_notify").notify_pdf_processed
                notify(os.path.basename(path), "處理中...")
            except Exception:
                LOGGER.debug("PDF notification unavailable", exc_info=True)

        watcher = watcher_module.start_watcher(folders, callback=on_new_file)
        if watcher is None:
            raise LegacyBackgroundError("configured file watcher could not start")
        try:
            stop_event.wait()
        finally:
            watcher.stop()

    return run


def _faiss_preload(_: threading.Event) -> None:
    importlib.import_module("api.startup")._preload_faiss()


def _omlx_warmup(_: threading.Event) -> None:
    importlib.import_module("api.startup")._warmup_omlx()


def build_component_specs(config: ServiceConfig) -> tuple[ComponentSpec, ...]:
    config = config.validated()
    available = (
        ComponentSpec("queue_recovery", _queue_recovery, one_shot=True),
        ComponentSpec("cron_scheduler", _cron_runner(config)),
        ComponentSpec("cloudflared_watchdog", _cloudflared_runner(config)),
        ComponentSpec("paperclip_watchdog", _paperclip_runner(config)),
        ComponentSpec("nas_mount_guard", _nas_runner(config)),
        ComponentSpec("laf_file_review_email_monitor", _laf_email_runner(config)),
        ComponentSpec("db_failover", _db_failover_runner(config)),
        ComponentSpec("keeper_sync", _keeper_runner(config)),
        ComponentSpec("file_watcher", _file_watcher_runner(config)),
    )
    selected = [spec for spec in available if spec.name not in config.disabled_components]
    if config.enable_faiss_preload:
        selected.append(ComponentSpec("faiss_preload", _faiss_preload, one_shot=True))
    if config.enable_omlx_warmup:
        selected.append(ComponentSpec("omlx_warmup", _omlx_warmup, one_shot=True))
    return tuple(selected)


class LegacyBackgroundService:
    def __init__(
        self,
        specs: Iterable[ComponentSpec],
        *,
        stop_event: threading.Event | None = None,
        join_timeout_seconds: float = 10.0,
    ) -> None:
        self.specs = tuple(specs)
        if not self.specs or len({spec.name for spec in self.specs}) != len(self.specs):
            raise LegacyBackgroundError("component specs must be non-empty with unique names")
        self.stop_event = stop_event or threading.Event()
        self.join_timeout_seconds = max(0.1, float(join_timeout_seconds))
        self._threads: dict[str, threading.Thread] = {}
        self._states = {spec.name: ComponentState() for spec in self.specs}
        self._state_lock = threading.Lock()
        self._started = False

    def _execute(self, spec: ComponentSpec) -> None:
        with self._state_lock:
            state = self._states[spec.name]
            state.status = "running"
            state.started_at = time.time()
        try:
            spec.target(self.stop_event)
        except Exception as exc:
            LOGGER.exception("legacy background component failed: %s", spec.name)
            with self._state_lock:
                state.status = "failed"
                state.error = f"{type(exc).__name__}: {exc}"
        else:
            with self._state_lock:
                state.status = "completed" if spec.one_shot else "stopped"
        finally:
            with self._state_lock:
                state.finished_at = time.time()

    def start(self) -> None:
        if self._started:
            raise LegacyBackgroundError("legacy background service already started")
        self._started = True
        for spec in self.specs:
            thread = threading.Thread(
                target=self._execute,
                args=(spec,),
                name=f"magi-v3-{spec.name}",
                daemon=True,
            )
            self._threads[spec.name] = thread
            thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        deadline = time.monotonic() + self.join_timeout_seconds
        for thread in self._threads.values():
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)

    def unhealthy_components(self) -> list[str]:
        unhealthy: list[str] = []
        for spec in self.specs:
            state = self._states[spec.name]
            if state.status == "failed":
                unhealthy.append(spec.name)
            elif self._started and not spec.one_shot and not self.stop_event.is_set():
                thread = self._threads.get(spec.name)
                if thread is not None and not thread.is_alive():
                    unhealthy.append(spec.name)
        return unhealthy

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            components = {name: state.to_dict() for name, state in self._states.items()}
        return {
            "schema_version": 1,
            "running": self._started and not self.stop_event.is_set(),
            "stop_requested": self.stop_event.is_set(),
            "components": components,
            "unhealthy": self.unhealthy_components(),
        }


def install_signal_handlers(stop_event: threading.Event) -> None:
    def request_stop(signum: int, _frame: Any) -> None:
        LOGGER.info("stop requested by signal %s", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)


def run_service(
    config: ServiceConfig,
    *,
    stop_event: threading.Event | None = None,
    specs: Iterable[ComponentSpec] | None = None,
) -> int:
    config = config.validated()
    bind_legacy_root(config.legacy_root)
    event = stop_event or threading.Event()
    service = LegacyBackgroundService(
        specs if specs is not None else build_component_specs(config),
        stop_event=event,
        join_timeout_seconds=config.join_timeout_seconds,
    )
    service.start()
    try:
        while not event.wait(0.5):
            unhealthy = service.unhealthy_components()
            if unhealthy:
                LOGGER.error("required background components unhealthy: %s", ", ".join(unhealthy))
                return 2
        return 0
    finally:
        service.stop()
        LOGGER.info("legacy background service stopped: %s", json.dumps(service.status(), sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--disable-component", action="append", choices=REQUIRED_COMPONENTS, default=[])
    parser.add_argument("--enable-faiss-preload", action="store_true")
    parser.add_argument("--enable-omlx-warmup", action="store_true")
    parser.add_argument("--cloudflared-interval", type=float, default=90.0)
    parser.add_argument("--paperclip-interval", type=float, default=120.0)
    parser.add_argument("--nas-interval", type=float, default=120.0)
    parser.add_argument("--db-interval", type=float, default=600.0)
    parser.add_argument("--keeper-interval", type=float, default=300.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    event = threading.Event()
    install_signal_handlers(event)
    config = ServiceConfig(
        legacy_root=args.legacy_root,
        cloudflared_interval_seconds=args.cloudflared_interval,
        paperclip_interval_seconds=args.paperclip_interval,
        nas_interval_seconds=args.nas_interval,
        db_interval_seconds=args.db_interval,
        keeper_interval_seconds=args.keeper_interval,
        enable_faiss_preload=args.enable_faiss_preload,
        enable_omlx_warmup=args.enable_omlx_warmup,
        disabled_components=frozenset(args.disable_component),
    )
    try:
        return run_service(config, stop_event=event)
    except LegacyBackgroundError as exc:
        LOGGER.error("legacy background service refused startup: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

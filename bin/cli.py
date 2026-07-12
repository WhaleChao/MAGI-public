from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from bin._runtime import resolve_python, resolve_release_root, root_error_message, runtime_env


SERVICE_NAME = "com.magi.daemon"
DEFAULT_DASHBOARD_URL = "http://127.0.0.1:5002"
TOOLS_API_HEALTH_URL = "http://127.0.0.1:5003/health"


def _require_root() -> Path | None:
    root = resolve_release_root()
    if root is None:
        print(f"[ERROR] {root_error_message()}", file=sys.stderr)
        return None
    return root


def _activate_root(root: Path) -> dict[str, str]:
    env = runtime_env(root)
    os.environ.update({"MAGI_ROOT": str(root), "MAGI_ROOT_DIR": str(root)})
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return env


def _get_service_manager():
    from skills.ops.platform_utils import get_service_manager

    return get_service_manager()


def _service_installed(manager: Any, name: str = SERVICE_NAME) -> bool:
    for attr in ("_plist_path", "_service_path"):
        path_factory = getattr(manager, attr, None)
        if callable(path_factory):
            try:
                return Path(path_factory(name)).exists()
            except Exception:
                return False
    return False


def _service_running(manager: Any, name: str = SERVICE_NAME) -> bool:
    try:
        return bool(manager.is_running(name))
    except Exception:
        return False


def _daemon_running() -> bool:
    try:
        from skills.ops.process_guardian import is_daemon_running

        return bool(is_daemon_running())
    except Exception:
        return False


def _kill_daemon() -> str:
    from skills.ops.process_guardian import force_kill_all

    return str(force_kill_all("daemon.py"))


def _health_probe(url: str, timeout: float = 1.5) -> dict[str, Any]:
    try:
        with urlopen(url, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            return {"ok": 200 <= status < 400, "status": status}
    except URLError as exc:
        return {"ok": False, "error": str(exc.reason if hasattr(exc, "reason") else exc)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _daemon_command(root: Path, extra_args: list[str] | None = None) -> list[str]:
    return [str(resolve_python(root)), str(root / "daemon.py"), *(extra_args or [])]


def _command_string(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def _run_doctor(root: Path, doctor_args: list[str]) -> int:
    script = root / "scripts" / "magi_doctor.py"
    if not script.exists():
        print(f"[ERROR] Missing doctor script: {script}", file=sys.stderr)
        return 1
    return subprocess.call(
        [str(resolve_python(root)), str(script), *doctor_args],
        cwd=root,
        env=runtime_env(root),
    )


def _start_background(root: Path, daemon_args: list[str]) -> int:
    daemon_script = root / "daemon.py"
    if not daemon_script.exists():
        print(f"[ERROR] Missing daemon script: {daemon_script}", file=sys.stderr)
        return 1

    if _daemon_running():
        print("MAGI daemon is already running.")
        return 0

    log_path = root / ".runtime" / "magi-daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = _daemon_command(root, daemon_args)
    with log_path.open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            command,
            cwd=root,
            env=runtime_env(root),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name != "nt"),
        )
    print(f"Started MAGI daemon (pid {proc.pid}).")
    print(f"Log: {log_path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = _require_root()
    if root is None:
        return 1
    _activate_root(root)

    manager = _get_service_manager()
    service_installed = _service_installed(manager)
    service_running = _service_running(manager)
    daemon_running = _daemon_running()
    probes = {
        "dashboard": _health_probe(f"{DEFAULT_DASHBOARD_URL}/health"),
        "tools_api": _health_probe(TOOLS_API_HEALTH_URL),
    }
    report = {
        "root": str(root),
        "python": str(resolve_python(root)),
        "service": {
            "name": SERVICE_NAME,
            "installed": service_installed,
            "running": service_running,
        },
        "daemon": {"running": daemon_running},
        "probes": probes,
        "running": service_running or daemon_running or any(item.get("ok") for item in probes.values()),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        state = "running" if report["running"] else "stopped"
        print(f"MAGI status: {state}")
        print(f"Root: {root}")
        print(f"Python: {report['python']}")
        print(f"Service: {SERVICE_NAME} installed={service_installed} running={service_running}")
        print(f"Daemon process: running={daemon_running}")
        for name, probe in probes.items():
            detail = probe.get("status") if probe.get("ok") else probe.get("error", "not responding")
            print(f"{name}: {'ok' if probe.get('ok') else 'warn'} ({detail})")
    return 0 if report["running"] else 3


def cmd_doctor(args: argparse.Namespace) -> int:
    root = _require_root()
    if root is None:
        return 1
    _activate_root(root)

    doctor_args: list[str] = []
    if args.json:
        doctor_args.append("--json")
    if args.no_live:
        doctor_args.append("--no-live")
    if args.output:
        doctor_args.extend(["--output", str(args.output)])
    return _run_doctor(root, doctor_args)


def cmd_open(args: argparse.Namespace) -> int:
    url = args.url or os.environ.get("MAGI_DASHBOARD_URL") or DEFAULT_DASHBOARD_URL
    if webbrowser.open(url):
        print(f"Opened {url}")
        return 0
    print(f"[ERROR] Could not open {url}", file=sys.stderr)
    return 1


def cmd_start(args: argparse.Namespace) -> int:
    root = _require_root()
    if root is None:
        return 1
    _activate_root(root)

    manager = _get_service_manager()
    if _service_installed(manager):
        if _service_running(manager):
            print("MAGI service is already running.")
            return 0
        if manager.start(SERVICE_NAME):
            print(f"Started MAGI service: {SERVICE_NAME}")
            return 0
        print(f"[ERROR] Failed to start service: {SERVICE_NAME}", file=sys.stderr)
        return 1

    if args.foreground:
        from bin.start import main as legacy_start

        return legacy_start(args.daemon_args)
    return _start_background(root, args.daemon_args)


def cmd_stop(args: argparse.Namespace) -> int:
    del args
    root = _require_root()
    if root is None:
        return 1
    _activate_root(root)

    manager = _get_service_manager()
    if _service_installed(manager) and _service_running(manager):
        if manager.stop(SERVICE_NAME):
            print(f"Stopped MAGI service: {SERVICE_NAME}")
            return 0
        print(f"[ERROR] Failed to stop service: {SERVICE_NAME}", file=sys.stderr)
        return 1

    if _daemon_running():
        print(_kill_daemon())
        return 0

    print("MAGI is not running.")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    stop_rc = cmd_stop(args)
    if stop_rc != 0:
        return stop_rc
    return cmd_start(args)


def cmd_service_install(args: argparse.Namespace) -> int:
    root = _require_root()
    if root is None:
        return 1
    _activate_root(root)

    manager = _get_service_manager()
    command = _daemon_command(root)
    payload = {
        "service": SERVICE_NAME,
        "root": str(root),
        "command": command,
        "command_string": _command_string(command),
        "dry_run": args.dry_run,
    }

    if args.dry_run:
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("MAGI service install dry-run")
            print(f"Service: {SERVICE_NAME}")
            print(f"Root: {root}")
            print(f"Command: {payload['command_string']}")
        return 0

    if not manager.install(
        SERVICE_NAME,
        payload["command_string"],
        description="MAGI Daemon - Multi-Agent Governance Infrastructure",
    ):
        print(f"[ERROR] Failed to install service: {SERVICE_NAME}", file=sys.stderr)
        return 1
    print(f"Installed MAGI service: {SERVICE_NAME}")

    if args.no_start:
        return 0
    if manager.start(SERVICE_NAME):
        print(f"Started MAGI service: {SERVICE_NAME}")
        return 0
    print(f"[WARN] Installed service but failed to start: {SERVICE_NAME}", file=sys.stderr)
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="magi", description="MAGI command line interface.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    status = subparsers.add_parser("status", help="show daemon and service status")
    status.add_argument("--json", action="store_true", help="print machine-readable JSON")
    status.set_defaults(func=cmd_status)

    doctor = subparsers.add_parser("doctor", help="run MAGI doctor checks")
    doctor.add_argument("--json", action="store_true", help="print doctor JSON")
    doctor.add_argument("--no-live", action="store_true", help="skip localhost service probes")
    doctor.add_argument("--output", type=Path, help="write doctor JSON report to a file")
    doctor.set_defaults(func=cmd_doctor)

    open_cmd = subparsers.add_parser("open", help="open the MAGI dashboard")
    open_cmd.add_argument("url", nargs="?", help="URL to open instead of the default dashboard")
    open_cmd.set_defaults(func=cmd_open)

    start = subparsers.add_parser("start", help="start MAGI")
    start.add_argument("--foreground", action="store_true", help="run through the legacy foreground starter")
    start.add_argument("daemon_args", nargs=argparse.REMAINDER, help="extra arguments passed to daemon.py")
    start.set_defaults(func=cmd_start)

    stop = subparsers.add_parser("stop", help="stop MAGI")
    stop.set_defaults(func=cmd_stop)

    restart = subparsers.add_parser("restart", help="restart MAGI")
    restart.add_argument("--foreground", action="store_true", help="restart through the legacy foreground starter")
    restart.add_argument("daemon_args", nargs=argparse.REMAINDER, help="extra arguments passed to daemon.py")
    restart.set_defaults(func=cmd_restart)

    service = subparsers.add_parser("service", help="manage the OS service")
    service_subparsers = service.add_subparsers(dest="service_command", required=True)
    install = service_subparsers.add_parser("install", help="install the MAGI service")
    install.add_argument("--dry-run", action="store_true", help="show the install plan without changing the system")
    install.add_argument("--json", action="store_true", help="print install plan as JSON")
    install.add_argument("--no-start", action="store_true", help="install without starting the service")
    install.set_defaults(func=cmd_service_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        if getattr(args, "command", "") in {"start", "restart"}:
            args.daemon_args.extend(unknown)
        else:
            parser.error(f"unrecognized arguments: {' '.join(unknown)}")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only process, pidfile, port, launchd, and ownership inventory."""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from .core import Owner, Snapshot

DEFAULT_PORTS = (5002, 5003, 5014, 50052, 5102, 5103, 8188, 8080, 8081, 8082, 8083, 8088, 8090)
HOST_SINGLETON_LAUNCHD_LABELS = {
    "com.magi.db-proxy": "db_proxy",
    "com.magi.input-method-watchdog": "host_watchdog",
    "com.magi.memory-watchdog": "host_watchdog",
    "com.magi.mlx-mtp": "model_host_8090",
    "com.magi.omlx": "model_host_8080",
    "com.magi.omlx-embed": "model_host_8081",
    "com.magi.omlx-phi4": "model_host_8082",
    "com.magi.omlx-smol": "model_host_8083",
    "com.magi.omlx-watchdog": "model_host_supervisor",
    "com.magi.paperclip-share-gateway": "share_gateway",
    "com.magi.paperclip-share-tunnel": "share_tunnel",
    "com.magi.rpc": "ingress",
    "com.magi.smb-reconnect": "nas_host",
}
HOST_SINGLETON_SCRIPT_IDENTITIES = {
    "scripts/ops/memory_watchdog.py": "host_watchdog",
    "scripts/ops/input_method_watchdog.py": "host_watchdog",
    "scripts/serve_mlx_mtp.py": "model_host_8090",
    "scripts/share_gateway.py": "share_gateway",
    "scripts/share_tunnel_supervisor.py": "share_tunnel",
    "bin/omlx_watchdog.sh": "model_host_supervisor",
    "rpc_server.py": "ingress",
}
ACTIVE_RELEASE_SERVICE_DOMAINS = {
    "memory-watchdog": "host_watchdog",
    "mlx-mtp": "model_host_8090",
    "paperclip-share-gateway": "share_gateway",
    "paperclip-share-tunnel": "share_tunnel",
}
ACTIVE_RELEASE_SERVICE_LAUNCHER_SUFFIX = Path("bin/magi-active-release-service.py")
MAGI_APPLICATION_ROOT = Path.home() / "Library" / "Application Support" / "MAGI"
OMLX_EXECUTABLE_IDENTITIES = frozenset({"/opt/homebrew/bin/omlx", "/opt/homebrew/opt/omlx/bin/omlx"})
OMLX_INSTANCE_IDENTITIES = {
    "8080": ".omlx",
    "8081": ".omlx-embed",
    "8082": ".omlx-phi4",
    "8083": ".omlx-smol",
}
HOST_SINGLETON_PIDFILES = frozenset({"rpc_server.pid"})
HOST_SINGLETON_EXECUTABLE_IDENTITIES = {
    str((Path.home() / "Library" / "Application Support" / "MAGI" / "rpc-bin" / "rpc-server").resolve()): "ingress"
}
PS_EXECUTABLE = "/bin/ps"
LSOF_EXECUTABLE = "/usr/sbin/lsof"
LAUNCHCTL_EXECUTABLE = "/bin/launchctl"
ProbeCommandObserver = Callable[[tuple[str, ...], object | None, BaseException | None], None]
_COMMAND_OBSERVER: ContextVar[ProbeCommandObserver | None] = ContextVar(
    "magi_v3_probe_command_observer", default=None
)


@contextmanager
def observe_probe_commands(observer: ProbeCommandObserver) -> Iterator[None]:
    """Record exact read-only probe subprocesses in the current context only."""

    token = _COMMAND_OBSERVER.set(observer)
    try:
        yield
    finally:
        _COMMAND_OBSERVER.reset(token)


def _run_probe_command(argv: Sequence[str], **kwargs: Any) -> object:
    exact = tuple(str(item) for item in argv)
    observer = _COMMAND_OBSERVER.get()
    try:
        result = subprocess.run(list(exact), **kwargs)
    except BaseException as exc:
        if observer is not None:
            observer(exact, None, exc)
        raise
    if observer is not None:
        observer(exact, result, None)
    return result


@dataclass(frozen=True)
class ReleaseSpec:
    name: str
    root: Path
    namespace: str
    pidfiles: tuple[Path, ...] = ()
    launchd_labels: tuple[str, ...] = ()
    launchd_plists: dict[str, Path] = field(default_factory=dict)
    ownership_files: tuple[Path, ...] = ()
    probe_errors: tuple[str, ...] = ()
    pidfiles_required: bool = True
    launchd_labels_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "root": str(self.root),
            "namespace": self.namespace,
            "pidfiles": [str(item) for item in self.pidfiles],
            "launchd_labels": list(self.launchd_labels),
            "launchd_plists": {key: str(value) for key, value in self.launchd_plists.items()},
            "ownership_files": [str(item) for item in self.ownership_files],
            "probe_errors": list(self.probe_errors),
            "pidfiles_required": self.pidfiles_required,
            "launchd_labels_required": self.launchd_labels_required,
        }


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    ppid: int
    command: str
    cwd: str = ""


def _all_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _all_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _all_strings(item)


def _domain_for_text(text: str, *, default: str = "release") -> str:
    lowered = text.lower()
    if "discord" in lowered and any(word in lowered for word in ("consumer", "listen", "bot", "worker")):
        return "discord_consumer"
    if "webhook" in lowered:
        return "webhook"
    if any(word in lowered for word in ("file_watcher", "file-watcher", "watchfiles", "watchdog.observer")):
        return "file_watcher"
    if any(word in lowered for word in ("notification_sender", "notification-sender", "outbox_sender")):
        return "notification_sender"
    if "gateway" in lowered:
        return "gateway"
    if any(word in lowered for word in ("ingress", "rpc_server", "listener")):
        return "ingress"
    if any(word in lowered for word in ("cron", "scheduler")):
        return "scheduler"
    if any(word in lowered for word in ("writer", "outbox", "commit_worker")):
        return "writer"
    if any(word in lowered for word in ("playwright", "browser", "portal", "chromium", "chrome")):
        return "browser"
    if any(word in lowered for word in ("omlx", "model", "mlx", "ollama")):
        return "model"
    return default


def _host_singleton_process_identity(text: str) -> tuple[str, str] | None:
    """Return an exact allowlisted host identity; never use substring suppression."""

    for executable, domain in HOST_SINGLETON_EXECUTABLE_IDENTITIES.items():
        if text == executable or text.startswith(executable + " "):
            return domain, executable
    # ``ps ... command`` does not quote paths containing spaces.  Match only
    # exact scripts below MAGI's canonical host/release roots before falling
    # back to shlex for commands whose paths are shell-safe.
    launcher = str(MAGI_APPLICATION_ROOT / ACTIVE_RELEASE_SERVICE_LAUNCHER_SUFFIX)
    for service, domain in ACTIVE_RELEASE_SERVICE_DOMAINS.items():
        match = re.search(
            rf"(?<!\S){re.escape(launcher)}\s+{re.escape(service)}(?=\s|$)",
            text,
        )
        if match:
            return domain, f"{launcher}:{service}"
    release_prefix = re.escape(str(MAGI_APPLICATION_ROOT / "releases"))
    for suffix, domain in HOST_SINGLETON_SCRIPT_IDENTITIES.items():
        match = re.search(
            rf"(?<!\S)({release_prefix}/v3-[A-Za-z0-9._-]+/{re.escape(suffix)})(?=\s|$)",
            text,
        )
        if match:
            return domain, match.group(1)
    try:
        argv = shlex.split(text)
    except ValueError:
        return None
    normalized = [str(Path(token).expanduser()) for token in argv]
    if normalized and normalized[0] in HOST_SINGLETON_EXECUTABLE_IDENTITIES:
        return HOST_SINGLETON_EXECUTABLE_IDENTITIES[normalized[0]], normalized[0]
    for index, token in enumerate(normalized[:-1]):
        path = Path(token)
        if (
            path.is_absolute()
            and len(path.parts) >= len(ACTIVE_RELEASE_SERVICE_LAUNCHER_SUFFIX.parts)
            and path.parts[-len(ACTIVE_RELEASE_SERVICE_LAUNCHER_SUFFIX.parts) :]
            == ACTIVE_RELEASE_SERVICE_LAUNCHER_SUFFIX.parts
        ):
            service = argv[index + 1]
            domain = ACTIVE_RELEASE_SERVICE_DOMAINS.get(service)
            if domain:
                return domain, f"{path}:{service}"
    for token in normalized:
        for suffix, domain in HOST_SINGLETON_SCRIPT_IDENTITIES.items():
            path = Path(token)
            suffix_path = Path(suffix)
            if (
                path.is_absolute()
                and len(path.parts) >= len(suffix_path.parts)
                and path.parts[-len(suffix_path.parts) :] == suffix_path.parts
            ):
                return domain, str(path)
    for index, token in enumerate(normalized[:-1]):
        if token not in OMLX_EXECUTABLE_IDENTITIES or argv[index + 1] != "serve":
            continue
        try:
            port = argv[argv.index("--port", index + 2) + 1]
            base_path = Path(argv[argv.index("--base-path", index + 2) + 1]).expanduser().resolve()
        except (ValueError, IndexError):
            return None
        expected_base = OMLX_INSTANCE_IDENTITIES.get(port)
        if expected_base and base_path == (Path.home() / expected_base).resolve():
            return f"model_host_{port}", f"{token}:serve:{port}:{base_path}"
    return None


def _looks_like_unapproved_model_process(text: str) -> bool:
    try:
        argv = shlex.split(text)
    except ValueError:
        return True
    return any(Path(token).name in {"omlx", "ollama", "serve_mlx_mtp.py"} for token in argv)


def _path_within(path: str | Path, root: Path) -> bool:
    candidate = Path(path).expanduser()
    if not str(path) or not candidate.is_absolute():
        return False
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _text_mentions_root(text: str, root: Path) -> bool:
    if _path_within(text, root):
        return True
    try:
        return any(_path_within(token, root) for token in shlex.split(text))
    except ValueError:
        return False


def _read_pid(path: Path) -> int:
    text = path.read_text(encoding="utf-8").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        raw = payload.get("pid") or payload.get("owner_pid")
    else:
        match = re.search(r"\b(\d{1,10})\b", text)
        raw = match.group(1) if match else 0
    return int(raw or 0)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _read_processes() -> tuple[list[ProcessInfo], list[str]]:
    try:
        result = _run_probe_command(
            [PS_EXECUTABLE, "-axo", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return [], [f"process inventory failed: {exc}"]
    if result.returncode != 0:
        return [], [f"process inventory failed rc={result.returncode}: {(result.stderr or '').strip()[:200]}"]
    cwd_by_pid, cwd_errors = _read_all_process_cwds()
    processes: list[ProcessInfo] = []
    for line in result.stdout.splitlines():
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)$", line)
        if match:
            pid = int(match.group(1))
            processes.append(ProcessInfo(pid, int(match.group(2)), match.group(3), cwd_by_pid.get(pid, "")))
    return processes, cwd_errors


def _without_observer_processes(processes: Iterable[ProcessInfo]) -> tuple[list[ProcessInfo], tuple[int, ...]]:
    """Exclude only this read-only probe, its ancestors, and its own children.

    Command lines for the preflight necessarily contain both release paths.
    Treating the observer itself as a release owner creates a false dual-active
    result.  Sibling processes are intentionally retained.
    """

    rows = list(processes)
    by_pid = {row.pid: row for row in rows}
    current = os.getpid()
    excluded: set[int] = {current}
    cursor = current
    while cursor in by_pid:
        parent = by_pid[cursor].ppid
        if parent <= 0 or parent in excluded:
            break
        excluded.add(parent)
        cursor = parent

    descendants = {current}
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row.pid not in descendants and row.ppid in descendants:
                descendants.add(row.pid)
                changed = True
    excluded.update(descendants)
    return [row for row in rows if row.pid not in excluded], tuple(sorted(excluded & set(by_pid)))


def _read_all_process_cwds() -> tuple[dict[int, str], list[str]]:
    """Read cwd for the full process inventory in one read-only lsof pass."""

    lsof = shutil.which("lsof")
    if lsof != LSOF_EXECUTABLE or not Path(LSOF_EXECUTABLE).exists():
        return {}, ["full process cwd inventory failed: lsof unavailable"]
    try:
        result = _run_probe_command(
            [LSOF_EXECUTABLE, "-n", "-d", "cwd", "-Fpn"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as exc:
        return {}, [f"full process cwd inventory failed: {exc}"]
    if result.returncode not in {0, 1}:
        return {}, [f"full process cwd inventory failed rc={result.returncode}: {(result.stderr or '').strip()[:200]}"]
    current_pid: int | None = None
    result_map: dict[int, str] = {}
    for line in result.stdout.splitlines():
        if re.fullmatch(r"p\d+", line):
            current_pid = int(line[1:])
        elif current_pid is not None and line.startswith("n/"):
            result_map[current_pid] = line[1:]
    if not result_map:
        return {}, ["full process cwd inventory returned no process directories"]
    return result_map, []


def _listener_pid_map(ports: Sequence[int]) -> tuple[dict[int, set[int]], list[str]]:
    """Read every requested listening TCP owner in one bounded ``lsof`` pass.

    Calling ``lsof`` once per port made a 13-port ownership snapshot wait up
    to 65 seconds when a mounted network volume was slow.  ``-b`` prevents
    filesystem ``stat``/``readlink`` calls that are irrelevant to socket
    ownership, and the field output lets us group the one process inventory by
    numeric port without exposing command lines or environments.
    """

    requested = tuple(dict.fromkeys(int(port) for port in ports))
    result_map = {port: set() for port in requested}
    lsof = shutil.which("lsof")
    if lsof != LSOF_EXECUTABLE or not Path(LSOF_EXECUTABLE).exists():
        return result_map, ["listener inventory failed: lsof unavailable"]
    try:
        result = _run_probe_command(
            [LSOF_EXECUTABLE, "-b", "-nP", "-iTCP", "-sTCP:LISTEN", "-Fpn"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return result_map, [f"listener inventory failed: {exc}"]
    if result.returncode not in {0, 1}:
        return result_map, [
            "listener inventory failed "
            f"rc={result.returncode}: {(result.stderr or '').strip()[:200]}"
        ]

    current_pid: int | None = None
    for line in result.stdout.splitlines():
        if re.fullmatch(r"p\d+", line):
            current_pid = int(line[1:])
            continue
        if current_pid is None or not line.startswith("n"):
            continue
        match = re.search(r":(\d+)$", line[1:])
        if match is None:
            continue
        port = int(match.group(1))
        if port in result_map:
            result_map[port].add(current_pid)
    return result_map, []


def _listener_pids(port: int) -> tuple[set[int], str | None]:
    """Compatibility wrapper for callers that need one port."""

    result, errors = _listener_pid_map((port,))
    return result[int(port)], errors[0] if errors else None


def _process_cwd(pid: int) -> tuple[str, str | None]:
    """Return a PID's cwd without reading or exposing its environment."""

    lsof = shutil.which("lsof")
    if lsof != LSOF_EXECUTABLE or not Path(LSOF_EXECUTABLE).exists():
        return "", "lsof unavailable for cwd probe"
    try:
        result = _run_probe_command(
            [LSOF_EXECUTABLE, "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return "", f"cwd probe failed for pid {pid}: {exc}"
    if result.returncode != 0:
        return "", f"cwd probe failed for pid {pid} rc={result.returncode}"
    paths = [line[1:] for line in result.stdout.splitlines() if line.startswith("n/")]
    return (paths[-1] if paths else ""), None


def _launchd_status(label: str) -> tuple[dict[str, Any], str | None]:
    if platform.system() != "Darwin" or shutil.which("launchctl") != LAUNCHCTL_EXECUTABLE:
        return {}, "launchctl unavailable"
    target = f"gui/{os.getuid()}/{label}"
    try:
        result = _run_probe_command(
            [LAUNCHCTL_EXECUTABLE, "print", target],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return {}, f"launchctl probe failed for {label}: {exc}"
    stderr = (result.stderr or "").strip()
    if result.returncode == 113 and "Could not find service" in stderr:
        return {"loaded": False}, None
    if result.returncode != 0:
        return {}, f"launchctl print failed for {label} rc={result.returncode}: {stderr[:200]}"
    pid_match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, flags=re.MULTILINE)
    state_match = re.search(r"^\s*state = ([^\n]+)$", result.stdout, flags=re.MULTILINE)
    if not pid_match and not state_match:
        return {}, f"launchctl output unparseable for {label}"
    return {
        "loaded": True,
        "pid": int(pid_match.group(1)) if pid_match else None,
        "state": state_match.group(1).strip() if state_match else "",
    }, None


def discover_release_spec(
    name: str,
    root: Path,
    namespace: str,
    *,
    runtime_root: Path | None = None,
    pidfiles_required: bool = True,
    launchd_labels_required: bool = True,
) -> ReleaseSpec:
    """Discover identity evidence without importing either release."""

    root = root.expanduser().resolve()
    runtime = (
        runtime_root.expanduser().resolve()
        if runtime_root is not None
        else (
            Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v3"
            if name == "v3"
            else root
        ).resolve()
    )
    pidfiles: list[Path] = []
    ownership_files: list[Path] = []
    if root.exists():
        for base in (root / ".runtime", root / ".agent"):
            if base.exists():
                pidfiles.extend(
                    path for path in sorted(base.glob("*.pid")) if path.name not in HOST_SINGLETON_PIDFILES
                )
        locks = root / ".runtime" / "locks"
        if locks.exists():
            ownership_files.extend(sorted(locks.glob("*.lock.json")))
    # V3 deliberately keeps mutable PID state outside its immutable release
    # tree.  Discovering only ``release/.runtime`` made the real cutover probe
    # blind to every V3 role even though the rendered launchd deployment writes
    # them below ``runtime_root/pids``.
    runtime_pids = runtime / "pids"
    if runtime_pids.exists():
        pidfiles.extend(sorted(runtime_pids.glob("*.pid")))
    runtime_locks = runtime / "locks"
    if runtime_locks.exists():
        ownership_files.extend(sorted(runtime_locks.glob("*.lock.json")))

    labels: list[str] = []
    plists: dict[str, Path] = {}
    errors: list[str] = []
    launch_dir = Path.home() / "Library" / "LaunchAgents"
    for path in sorted(launch_dir.glob("com.magi*.plist")) if launch_dir.exists() else []:
        try:
            with path.open("rb") as handle:
                payload = plistlib.load(handle)
        except Exception as exc:
            errors.append(f"launchd plist unreadable {path}: {exc}")
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("Label"), str) or not payload["Label"]:
            errors.append(f"launchd plist invalid {path}: non-empty Label is required")
            continue
        label = payload["Label"]
        if label in HOST_SINGLETON_LAUNCHD_LABELS:
            continue
        if any(_text_mentions_root(item, root) for item in _all_strings(payload)):
            labels.append(label)
            plists[label] = path
    return ReleaseSpec(
        name=name,
        root=root,
        namespace=namespace,
        pidfiles=tuple(dict.fromkeys(pidfiles)),
        launchd_labels=tuple(dict.fromkeys(labels)),
        launchd_plists=plists,
        ownership_files=tuple(dict.fromkeys(ownership_files)),
        probe_errors=tuple(dict.fromkeys(errors)),
        pidfiles_required=pidfiles_required,
        launchd_labels_required=launchd_labels_required,
    )


def _release_for_process(
    proc: ProcessInfo,
    specs: tuple[ReleaseSpec, ...],
    pidfile_map: dict[int, str],
    known_by_pid: dict[int, str] | None = None,
) -> str | None:
    if known_by_pid and proc.pid in known_by_pid:
        return known_by_pid[proc.pid]
    if _host_singleton_process_identity(proc.command):
        return None
    by_pidfile = pidfile_map.get(proc.pid)
    root_matches = [
        spec.name
        for spec in specs
        if _path_within(proc.cwd, spec.root) or any(_path_within(token, spec.root) for token in proc.command.split())
    ]
    namespace_matches = [spec.name for spec in specs if spec.namespace and spec.namespace in proc.command]
    candidates = set(root_matches) | set(namespace_matches)
    if by_pidfile:
        candidates.add(by_pidfile)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _release_process_tree(
    processes: Iterable[ProcessInfo],
    specs: tuple[ReleaseSpec, ...],
    pidfile_map: dict[int, str],
) -> tuple[dict[int, str], dict[int, int]]:
    """Attribute relative-command children from an identity-bound ancestor."""

    process_list = tuple(processes)
    releases: dict[int, str] = {}
    anchors: dict[int, int] = {}
    for proc in process_list:
        release = _release_for_process(proc, specs, pidfile_map)
        if release:
            releases[proc.pid] = release
            anchors[proc.pid] = proc.pid

    changed = True
    while changed:
        changed = False
        for proc in process_list:
            if proc.pid in releases or proc.ppid not in releases:
                continue
            releases[proc.pid] = releases[proc.ppid]
            anchors[proc.pid] = anchors[proc.ppid]
            changed = True

    # A root/cwd match can make every Playwright/Chrome descendant look like a
    # separate owner even though they form one browser session.  Collapse only
    # adjacent processes that belong to the same release *and* the same
    # singleton domain.  Independent browser roots remain independent owners.
    by_pid = {proc.pid: proc for proc in process_list}
    changed = True
    while changed:
        changed = False
        for proc in process_list:
            parent = by_pid.get(proc.ppid)
            if not parent or releases.get(proc.pid) != releases.get(parent.pid):
                continue
            if _domain_for_text(proc.command) != _domain_for_text(parent.command):
                continue
            parent_anchor = anchors.get(parent.pid, parent.pid)
            if anchors.get(proc.pid, proc.pid) != parent_anchor:
                anchors[proc.pid] = parent_anchor
                changed = True
    return releases, anchors


def collect_snapshot(specs: Iterable[ReleaseSpec], *, ports: Iterable[int] = DEFAULT_PORTS) -> Snapshot:
    """Collect a complete read-only ownership snapshot."""

    release_specs = tuple(specs)
    errors: list[str] = []
    owners: list[Owner] = []
    processes, process_errors = _read_processes()
    processes, observer_processes = _without_observer_processes(processes)
    errors.extend(process_errors)
    process_map = {proc.pid: proc for proc in processes}
    pidfile_map: dict[int, str] = {}
    stale_pidfiles: list[str] = []

    roots = [str(spec.root) for spec in release_specs]
    namespaces = [spec.namespace for spec in release_specs if spec.namespace]
    if len(set(roots)) != len(roots):
        errors.append("release roots must be unique")
    if len(set(namespaces)) != len(namespaces):
        errors.append("release namespaces must be unique")

    for spec in release_specs:
        errors.extend(spec.probe_errors)
        if not spec.root.exists():
            errors.append(f"{spec.name} root missing: {spec.root}")
        if not spec.namespace:
            errors.append(f"{spec.name} namespace missing")
        if spec.pidfiles_required and not spec.pidfiles:
            errors.append(f"{spec.name} pidfile coverage missing")
        if spec.launchd_labels_required and not spec.launchd_labels:
            errors.append(f"{spec.name} launchd label coverage missing")
        for path in spec.pidfiles:
            try:
                pid = _read_pid(path)
            except Exception as exc:
                errors.append(f"pidfile unreadable {path}: {exc}")
                continue
            if not _pid_alive(pid):
                stale_pidfiles.append(str(path))
                errors.append(f"stale pidfile {path}: pid={pid}")
                continue
            pidfile_map[pid] = spec.name
            proc = process_map.get(pid)
            root_ok = bool(
                proc
                and (
                    _path_within(proc.cwd, spec.root)
                    or any(_path_within(token, spec.root) for token in proc.command.split())
                )
            )
            namespace_ok = bool(proc and spec.namespace in proc.command)
            if proc and not (root_ok or namespace_ok):
                cwd, cwd_error = _process_cwd(pid)
                if cwd_error:
                    errors.append(cwd_error)
                root_ok = bool(cwd and _path_within(cwd, spec.root))
            if not root_ok and not namespace_ok:
                errors.append(f"pidfile identity mismatch {path}: pid={pid}")
            owners.append(
                Owner(
                    release=spec.name if spec.name in {"v2", "v3"} else None,
                    domain=_domain_for_text(f"{path} {proc.command if proc else ''}"),
                    owner_id=f"pid:{pid}",
                    source=f"pidfile:{path}",
                    pid=pid,
                    root=str(spec.root),
                    namespace=spec.namespace,
                    ambiguous=not (root_ok or namespace_ok),
                )
            )

    launchd_details: dict[str, dict[str, Any]] = {}
    for spec in release_specs:
        for label in spec.launchd_labels:
            status, error = _launchd_status(label)
            if error:
                errors.append(error)
                continue
            launchd_details[label] = status
            if not status.get("loaded"):
                continue
            launchd_pid = status.get("pid")
            if launchd_pid:
                claimed = pidfile_map.get(int(launchd_pid))
                if claimed and claimed != spec.name:
                    errors.append(
                        f"launchd/pidfile release conflict label={label} pid={launchd_pid}: {claimed} vs {spec.name}"
                    )
                else:
                    pidfile_map[int(launchd_pid)] = spec.name
            owners.append(
                Owner(
                    release=spec.name if spec.name in {"v2", "v3"} else None,
                    domain=_domain_for_text(label),
                    owner_id=f"launchd:{label}",
                    source="launchd",
                    pid=launchd_pid,
                    root=str(spec.root),
                    namespace=spec.namespace,
                )
            )

    launch_dir = Path.home() / "Library" / "LaunchAgents"
    for label, domain in HOST_SINGLETON_LAUNCHD_LABELS.items():
        plist_path = launch_dir / f"{label}.plist"
        if not plist_path.exists():
            continue
        try:
            with plist_path.open("rb") as handle:
                payload = plistlib.load(handle)
        except Exception as exc:
            errors.append(f"launchd plist unreadable {plist_path}: {exc}")
            continue
        if payload.get("Label") != label:
            errors.append(f"launchd plist identity mismatch {plist_path}: expected Label {label}")
            continue
        status, error = _launchd_status(label)
        if error:
            errors.append(error)
            continue
        launchd_details[label] = status
        if status.get("loaded"):
            owners.append(
                Owner(
                    release=None,
                    domain=domain,
                    owner_id=f"launchd:{label}",
                    source="launchd:host-singleton",
                    pid=status.get("pid"),
                    ambiguous=False,
                    detail=f"exact allowlist label={label}",
                )
            )

    release_by_pid, anchor_by_pid = _release_process_tree(processes, release_specs, pidfile_map)
    shared_processes: list[dict[str, Any]] = []
    for proc in processes:
        host_identity = _host_singleton_process_identity(proc.command)
        if host_identity:
            domain, identity = host_identity
            shared_processes.append(
                {"pid": proc.pid, "ppid": proc.ppid, "domain": domain, "identity": identity, "command": proc.command[:500]}
            )
            owners.append(
                Owner(
                    release=None,
                    domain=domain,
                    owner_id=f"host:{identity}:pid:{proc.pid}",
                    source="process:host-singleton",
                    pid=proc.pid,
                    ambiguous=False,
                    detail=f"exact allowlist identity={identity}",
                )
            )
            continue
        release = _release_for_process(proc, release_specs, pidfile_map, release_by_pid)
        matching_specs = [
            spec
            for spec in release_specs
            if _path_within(proc.cwd, spec.root)
            or any(_path_within(token, spec.root) for token in proc.command.split())
            or (spec.namespace and spec.namespace in proc.command)
        ]
        if release:
            spec = next(item for item in release_specs if item.name == release)
            owners.append(
                Owner(
                    release=release if release in {"v2", "v3"} else None,
                    domain=_domain_for_text(proc.command),
                    owner_id=f"tree:{anchor_by_pid.get(proc.pid, proc.pid)}",
                    source="process",
                    pid=proc.pid,
                    root=str(spec.root),
                    namespace=spec.namespace,
                    ambiguous=False,
                    detail=proc.command[:500],
                )
            )
        elif len(matching_specs) > 1:
            owners.append(
                Owner(None, _domain_for_text(proc.command), f"pid:{proc.pid}", "process", proc.pid, ambiguous=True)
            )
        elif _looks_like_unapproved_model_process(proc.command):
            owners.append(
                Owner(
                    None,
                    "model",
                    f"pid:{proc.pid}",
                    "process:unapproved-model-identity",
                    proc.pid,
                    ambiguous=True,
                    detail=proc.command[:500],
                )
            )

    port_details: dict[str, list[int]] = {}
    listener_map, listener_errors = _listener_pid_map(ports)
    errors.extend(listener_errors)
    for port in ports:
        pids = listener_map.get(int(port), set())
        port_details[str(port)] = sorted(pids)
        for pid in pids:
            proc = process_map.get(pid, ProcessInfo(pid, 0, ""))
            release = _release_for_process(proc, release_specs, pidfile_map, release_by_pid)
            host_identity = _host_singleton_process_identity(proc.command)
            cwd_error = None
            if release is None:
                cwd, cwd_error = _process_cwd(pid)
                if cwd:
                    proc = ProcessInfo(proc.pid, proc.ppid, proc.command, cwd)
                    release = _release_for_process(proc, release_specs, pidfile_map, release_by_pid)
            if cwd_error:
                errors.append(cwd_error)
            owners.append(
                Owner(
                    release=release if release in {"v2", "v3"} else None,
                    domain=host_identity[0] if host_identity else "port",
                    owner_id=(
                        f"host:{host_identity[1]}:pid:{pid}"
                        if host_identity
                        else f"port:{port}:pid:{pid}"
                    ),
                    source=f"listener:{port}",
                    pid=pid,
                    ambiguous=release is None and host_identity is None,
                    detail=proc.command[:500],
                )
            )

    for spec in release_specs:
        for path in spec.ownership_files:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                pid = int(payload.get("pid") or 0)
            except Exception as exc:
                errors.append(f"ownership metadata unreadable {path}: {exc}")
                continue
            if not _pid_alive(pid):
                errors.append(f"stale ownership metadata {path}: pid={pid}")
                continue
            proc = process_map.get(pid)
            identity_ok = release_by_pid.get(pid) == spec.name or bool(
                proc
                and (
                    _path_within(proc.cwd, spec.root)
                    or any(_path_within(token, spec.root) for token in proc.command.split())
                    or spec.namespace in proc.command
                )
            )
            owners.append(
                Owner(
                    release=spec.name if spec.name in {"v2", "v3"} else None,
                    domain=_domain_for_text(str(payload.get("domain") or path.stem), default="writer"),
                    owner_id=f"lock:{payload.get('domain') or path.stem}:pid:{pid}",
                    source=f"ownership:{path}",
                    pid=pid,
                    root=str(spec.root),
                    namespace=spec.namespace,
                    ambiguous=not identity_ok,
                    detail=str(payload.get("owner") or ""),
                )
            )

    return Snapshot(
        owners=tuple(owners),
        probe_errors=tuple(dict.fromkeys(errors)),
        coverage=frozenset({"process", "pidfile", "port", "launchd", "ownership"}),
        observed_at=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        metadata={
            "ports": port_details,
            "launchd": launchd_details,
            "stale_pidfiles": stale_pidfiles,
            "shared_host_singleton_processes": shared_processes,
            "release_specs": [spec.to_dict() for spec in release_specs],
            "observer_processes_excluded": list(observer_processes),
        },
    )

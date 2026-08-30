#!/usr/bin/env python3
"""Production-safe macOS host adapter and inert-by-default CLI for isolated LIVE.

Without ``--execute-isolated-live`` the CLI performs only immutable artifact
verification and read-only host inspection.  The mutation path is delegated to
the fail-closed executor after a second set of explicit context, token, window,
platform, and host-layout checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.v3_cutover.core import CutoverError, Snapshot, assess_cutover_window, assess_snapshot
from scripts.v3_cutover.probe import (
    DEFAULT_PORTS,
    HOST_SINGLETON_LAUNCHD_LABELS,
    ReleaseSpec,
    collect_snapshot,
    observe_probe_commands,
)
from scripts.v3_validation.isolated_live_execute import (
    DEPLOYMENT_MODE,
    ROLE_LABELS,
    SHA256_RE,
    START_ORDER,
    BoundArtifact,
    IsolatedLiveBlocked,
    IsolatedLiveMachine,
    ProbeSpec,
    ValidationRole,
    VerifiedDeployment,
    _sha256_file,
    _release_cutover_window,
    _validate_probe,
    _verify_bound,
    execute_isolated_live_validation,
    load_isolated_live_plan,
    verify_static_plan,
)
from scripts.v3_validation.isolated_resource_window_collector import (
    REQUIRED_MODEL_OWNER_PATTERNS,
    REQUIRED_OBSERVED_PORTS,
    REQUIRED_STOPPED_LABELS,
)


LAUNCHCTL = "/bin/launchctl"
LABEL_RE = re.compile(r"^com\.magi\.[A-Za-z0-9._-]+$")
V2_REQUIRED_LABEL = "com.magi.daemon"
V2_READINESS_URLS = (
    "http://127.0.0.1:5002/readyz",
    # The V2 Tools API exposes its dependency-aware readiness contract at
    # /health.  /readyz belongs to the V3 validation service and is a 404 on
    # the production V2 process, so using it here makes every otherwise-safe
    # V2 restore fail after the isolated handoff has already run.
    "http://127.0.0.1:5003/health",
    "http://127.0.0.1:5014/health",
    "http://127.0.0.1:8088/health",
)
MAX_HTTP_BODY_BYTES = 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 15.0
STATE_WAIT_SECONDS = 30.0
RESOURCE_WINDOW_RECEIPT_SCHEMA = "magi.v3.resource-window-host-receipt/v1"
RESOURCE_V2_READINESS_URLS = (
    "http://127.0.0.1:5002/health",
    "http://127.0.0.1:5003/health",
    "http://127.0.0.1:8088/health",
)
RESOURCE_MODEL_READINESS = {
    "com.magi.omlx": "http://127.0.0.1:8080/v1/models",
    "com.magi.omlx-embed": "http://127.0.0.1:8081/v1/models",
}


class HTTPResponse(Protocol):
    status: int
    headers: Mapping[str, str]

    def read(self, amount: int = -1) -> bytes: ...

    def __enter__(self) -> "HTTPResponse": ...

    def __exit__(self, *_args: object) -> None: ...


Runner = Callable[..., object]
HTTPOpener = Callable[[Request, float], HTTPResponse]
SnapshotCollector = Callable[[Sequence[ReleaseSpec], Sequence[int]], Snapshot]
Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]


@dataclass(frozen=True, slots=True)
class HostLaunchAgent:
    label: str
    plist: BoundArtifact


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _canonical_v2_root() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "MAGI"
        / "runtime"
        / "MAGI_v2"
    ).resolve(strict=False)


def _canonical_v3_runtime() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "MAGI"
        / "runtime"
        / "MAGI_v3"
    ).resolve(strict=False)


def _canonical_launchagents() -> Path:
    return (Path.home() / "Library" / "LaunchAgents").resolve(strict=False)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _receipt(operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    value = {
        "schema": RESOURCE_WINDOW_RECEIPT_SCHEMA,
        "operation": operation,
        **dict(payload),
    }
    unsigned = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    value["receipt_sha256"] = _sha256_bytes(unsigned)
    return value


def _verify_resource_capture(capture: Mapping[str, Any]) -> str:
    unsigned = dict(capture)
    supplied = unsigned.pop("receipt_sha256", None)
    if (
        capture.get("schema") != RESOURCE_WINDOW_RECEIPT_SCHEMA
        or capture.get("operation") != "capture_initial_state"
        or capture.get("ok") is not True
        or capture.get("labels") != list(REQUIRED_STOPPED_LABELS)
        or supplied
        != _sha256_bytes(
            (json.dumps(unsigned, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
    ):
        raise IsolatedLiveBlocked("resource-window capture is invalid or hash-mismatched")
    return str(supplied)


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _all_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key)
            yield from _all_strings(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _all_strings(child)


def _mentions_root(value: Any, root: Path) -> bool:
    for text in _all_strings(value):
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            continue
        try:
            candidate.resolve(strict=False).relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _copy_no_clobber(source: BoundArtifact, target: Path, *, mode: int = 0o600) -> None:
    data = _verify_bound(source, description=f"host install source {source.path.name}")
    if target.exists() or target.is_symlink():
        raise IsolatedLiveBlocked(f"host install target already exists: {target}")
    temporary = target.parent / f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        _write_exclusive(temporary, data, mode=mode)
        try:
            os.link(temporary, target, follow_symlinks=False)
        except OSError as exc:
            raise IsolatedLiveBlocked(f"host install no-clobber publish failed: {target}") from exc
        if _sha256_file(target) != source.sha256:
            raise IsolatedLiveBlocked(f"host install target hash mismatch: {target}")
        _fsync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


class _NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


_DIRECT_LOOPBACK_OPENER = build_opener(ProxyHandler({}), _NoRedirects())


def _default_http_opener(request: Request, timeout: float) -> HTTPResponse:
    # Proxies and redirects are disabled: an allowlisted loopback probe may not
    # escape to an external origin even if a local service is misconfigured.
    return _DIRECT_LOOPBACK_OPENER.open(request, timeout=timeout)  # type: ignore[return-value]


def _default_snapshot_collector(
    specs: Sequence[ReleaseSpec], ports: Sequence[int]
) -> Snapshot:
    return collect_snapshot(specs, ports=ports)


class MacOSIsolatedLiveMachine(IsolatedLiveMachine):
    """Concrete macOS adapter with exact launchd and release ownership rules."""

    def __init__(
        self,
        deployment: VerifiedDeployment,
        *,
        artifact_directory: Path,
        runner: Runner = subprocess.run,
        http_opener: HTTPOpener = _default_http_opener,
        snapshot_collector: SnapshotCollector = _default_snapshot_collector,
        clock: Clock | None = None,
        sleeper: Sleeper = time.sleep,
        uid: int | None = None,
        platform_system: Callable[[], str] = platform.system,
        v2_root: Path | None = None,
        launchagents_directory: Path | None = None,
        expected_runtime_root: Path | None = None,
        command_timeout_seconds: float = COMMAND_TIMEOUT_SECONDS,
        state_wait_seconds: float = STATE_WAIT_SECONDS,
    ) -> None:
        if platform_system() != "Darwin":
            raise IsolatedLiveBlocked("macOS isolated LIVE adapter requires Darwin")
        self.deployment = deployment
        self.runner = runner
        self.http_opener = http_opener
        self.snapshot_collector = snapshot_collector
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.sleeper = sleeper
        self.uid = os.getuid() if uid is None else uid
        self.command_timeout_seconds = command_timeout_seconds
        self.state_wait_seconds = state_wait_seconds
        if self.uid <= 0 or command_timeout_seconds <= 0 or state_wait_seconds <= 0:
            raise IsolatedLiveBlocked("host adapter UID and timeouts must be positive")

        self.v2_root = (v2_root or _canonical_v2_root()).expanduser().resolve(strict=False)
        self.launchagents_directory = (
            launchagents_directory or _canonical_launchagents()
        ).expanduser().resolve(strict=False)
        expected_runtime = (
            expected_runtime_root or _canonical_v3_runtime()
        ).expanduser().resolve(strict=False)
        if deployment.runtime_root != expected_runtime:
            raise IsolatedLiveBlocked("deployment runtime root is not the canonical V3 runtime")
        if not self.v2_root.is_dir() or self.v2_root.is_symlink():
            raise IsolatedLiveBlocked("canonical V2 runtime root is missing or symlinked")
        if not self.launchagents_directory.is_dir() or self.launchagents_directory.is_symlink():
            raise IsolatedLiveBlocked("canonical user LaunchAgents directory is missing or symlinked")

        artifact = artifact_directory.expanduser()
        if not artifact.is_absolute() or artifact.resolve(strict=False) != artifact or artifact.is_symlink():
            raise IsolatedLiveBlocked("host artifact directory must be canonical and absolute")
        if artifact.exists():
            raise IsolatedLiveBlocked("host artifact directory must not already exist")
        if any(
            _inside(artifact, root) or _inside(root, artifact)
            for root in (
                self.v2_root,
                deployment.release_root,
                deployment.deployment_root,
                deployment.runtime_root,
                deployment.validation_input_root,
                self.launchagents_directory,
            )
        ):
            raise IsolatedLiveBlocked("host artifact directory overlaps an executable or runtime domain")
        artifact.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifact.mkdir(mode=0o700)
        self.artifact_directory = artifact
        self._artifact_sequence = 0
        self.v2_agents = self._discover_v2_agents()
        v2_pidfiles = tuple(
            path
            for base in (self.v2_root / ".runtime", self.v2_root / ".agent")
            if base.is_dir()
            for path in sorted(base.glob("*.pid"))
        )
        self._v2_spec = ReleaseSpec(
            name="v2",
            root=self.v2_root,
            namespace="MAGI_v2",
            pidfiles=v2_pidfiles,
            launchd_labels=tuple(agent.label for agent in self.v2_agents),
            launchd_plists={agent.label: agent.plist.path for agent in self.v2_agents},
            ownership_files=(),
            pidfiles_required=False,
            launchd_labels_required=True,
        )
        self._initially_loaded_v2 = frozenset(
            agent.label for agent in self.v2_agents if self._launchd_status(agent.label)["loaded"]
        )
        self._record(
            "v2-launchagent-inventory",
            {
                "schema_version": 1,
                "v2_root": str(self.v2_root),
                "agents": [
                    {
                        "label": agent.label,
                        "plist": str(agent.plist.path),
                        "sha256": agent.plist.sha256,
                        "initially_loaded": agent.label in self._initially_loaded_v2,
                    }
                    for agent in self.v2_agents
                ],
            },
        )
        self._started_roles: list[str] = []
        self._runtime_created = False
        self._installed_v3_labels: set[str] = set()
        self._resource_window_capture: dict[str, Any] | None = None

    def _record(self, name: str, payload: Mapping[str, Any]) -> Path:
        self._artifact_sequence += 1
        safe = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "artifact"
        path = self.artifact_directory / f"{self._artifact_sequence:04d}-{safe}.json"
        data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
        _write_exclusive(path, data)
        return path

    def _discover_v2_agents(self) -> tuple[HostLaunchAgent, ...]:
        agents: list[HostLaunchAgent] = []
        for plist_path in sorted(self.launchagents_directory.glob("com.magi*.plist")):
            if plist_path.is_symlink() or not plist_path.is_file():
                raise IsolatedLiveBlocked(f"unsafe MAGI launchagent path: {plist_path}")
            try:
                payload = plistlib.loads(plist_path.read_bytes())
            except Exception as exc:
                raise IsolatedLiveBlocked(f"MAGI launchagent is unreadable: {plist_path}") from exc
            label = payload.get("Label") if isinstance(payload, dict) else None
            if not isinstance(label, str) or not LABEL_RE.fullmatch(label):
                raise IsolatedLiveBlocked(f"MAGI launchagent has an invalid label: {plist_path}")
            if plist_path.name != f"{label}.plist":
                raise IsolatedLiveBlocked(f"MAGI launchagent filename/label mismatch: {plist_path}")
            if label in HOST_SINGLETON_LAUNCHD_LABELS or label in ROLE_LABELS.values():
                continue
            if not _mentions_root(payload, self.v2_root):
                continue
            agents.append(HostLaunchAgent(label, BoundArtifact(plist_path, _sha256_file(plist_path))))
        labels = [agent.label for agent in agents]
        if V2_REQUIRED_LABEL not in labels or len(labels) != len(set(labels)):
            raise IsolatedLiveBlocked("V2 launchagent inventory lacks the exact daemon or has duplicates")
        priority = {V2_REQUIRED_LABEL: 0, "com.magi.menubar": 1}
        return tuple(sorted(agents, key=lambda item: (priority.get(item.label, 2), item.label)))

    def _assert_agent_unchanged(self, agent: HostLaunchAgent) -> None:
        _verify_bound(agent.plist, description=f"V2 launchagent {agent.label}")
        payload = plistlib.loads(agent.plist.path.read_bytes())
        if payload.get("Label") != agent.label or not _mentions_root(payload, self.v2_root):
            raise IsolatedLiveBlocked(f"V2 launchagent identity drifted: {agent.label}")

    def _allowed_launchctl_argv(self, argv: tuple[str, ...]) -> bool:
        allowed_labels = (
            {agent.label for agent in self.v2_agents}
            | set(ROLE_LABELS.values())
            | set(REQUIRED_STOPPED_LABELS)
        )
        if len(argv) == 3 and argv[:2] == (LAUNCHCTL, "print"):
            return argv[2] in {f"gui/{self.uid}/{label}" for label in allowed_labels}
        if len(argv) == 3 and argv[:2] == (LAUNCHCTL, "bootout"):
            return argv[2] in {f"gui/{self.uid}/{label}" for label in allowed_labels}
        if len(argv) == 4 and argv[:3] == (LAUNCHCTL, "bootstrap", f"gui/{self.uid}"):
            candidate = Path(argv[3]).resolve(strict=False)
            allowed_paths = {agent.plist.path for agent in self.v2_agents} | {
                self.launchagents_directory / f"{label}.plist" for label in ROLE_LABELS.values()
            } | {
                self.launchagents_directory / f"{label}.plist"
                for label in REQUIRED_STOPPED_LABELS
            }
            return candidate in allowed_paths
        return False

    def _run_launchctl(self, argv: tuple[str, ...], *, action: str) -> CommandResult:
        if not self._allowed_launchctl_argv(argv):
            raise IsolatedLiveBlocked(f"launchctl argv is outside the code-owned allowlist: {argv}")
        started = self.clock().astimezone(timezone.utc).isoformat()
        timed_out = False
        try:
            raw = self.runner(
                list(argv),
                capture_output=True,
                text=True,
                timeout=self.command_timeout_seconds,
                check=False,
            )
            result = CommandResult(
                argv,
                int(getattr(raw, "returncode")),
                str(getattr(raw, "stdout", "") or ""),
                str(getattr(raw, "stderr", "") or ""),
            )
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            result = CommandResult(
                argv,
                124,
                str(exc.stdout or ""),
                str(exc.stderr or ""),
                timed_out=True,
            )
        artifact = self._record(
            f"launchctl-{action}",
            {
                "schema_version": 1,
                "started_at": started,
                "finished_at": self.clock().astimezone(timezone.utc).isoformat(),
                "argv": list(result.argv),
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": result.timed_out,
            },
        )
        if timed_out:
            raise IsolatedLiveBlocked(f"launchctl timed out; artifact={artifact}")
        return result

    @staticmethod
    def _known_missing(result: CommandResult) -> bool:
        lowered = result.stderr.lower()
        return result.returncode in {3, 113} and any(
            phrase in lowered
            for phrase in ("could not find service", "no such process", "service not found")
        )

    def _launchd_status(self, label: str) -> dict[str, Any]:
        result = self._run_launchctl(
            (LAUNCHCTL, "print", f"gui/{self.uid}/{label}"), action=f"print-{label}"
        )
        command_receipt = {
            "argv": list(result.argv),
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
        }
        command_sha = _sha256_bytes(
            (json.dumps(command_receipt, sort_keys=True, separators=(",", ":")) + "\n").encode()
        )
        if self._known_missing(result):
            return {
                "loaded": False,
                "pid": None,
                "launchctl_receipt": command_receipt,
                "launchctl_receipt_sha256": command_sha,
            }
        if result.returncode != 0:
            raise IsolatedLiveBlocked(f"launchctl print failed for exact label {label}")
        pid = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, flags=re.MULTILINE)
        state = re.search(r"^\s*state = ([^\n]+)$", result.stdout, flags=re.MULTILINE)
        if pid is None and state is None:
            raise IsolatedLiveBlocked(f"launchctl print output is unparseable for {label}")
        return {
            "loaded": True,
            "pid": int(pid.group(1)) if pid else None,
            "state": state.group(1).strip() if state else "",
            "launchctl_receipt": command_receipt,
            "launchctl_receipt_sha256": command_sha,
        }

    def _wait_loaded(self, label: str, expected: bool) -> dict[str, Any]:
        deadline = time.monotonic() + self.state_wait_seconds
        last: dict[str, Any] = {}
        while True:
            last = self._launchd_status(label)
            if bool(last.get("loaded")) is expected:
                return last
            if time.monotonic() >= deadline:
                raise IsolatedLiveBlocked(
                    f"launchd state timeout label={label} expected_loaded={expected}"
                )
            self.sleeper(0.1)

    def _bootout(self, label: str) -> None:
        status = self._launchd_status(label)
        if not status["loaded"]:
            return
        result = self._run_launchctl(
            (LAUNCHCTL, "bootout", f"gui/{self.uid}/{label}"),
            action=f"bootout-{label}",
        )
        if result.returncode != 0 and not self._known_missing(result):
            raise IsolatedLiveBlocked(f"launchctl bootout failed for exact label {label}")
        self._wait_loaded(label, False)

    def _bootstrap(self, agent: HostLaunchAgent, *, allow_already_loaded: bool) -> None:
        status = self._launchd_status(agent.label)
        if status["loaded"]:
            if allow_already_loaded:
                return
            raise IsolatedLiveBlocked(f"launchd label unexpectedly loaded before bootstrap: {agent.label}")
        _verify_bound(agent.plist, description=f"bootstrap plist {agent.label}")
        result = self._run_launchctl(
            (LAUNCHCTL, "bootstrap", f"gui/{self.uid}", str(agent.plist.path)),
            action=f"bootstrap-{agent.label}",
        )
        if result.returncode != 0:
            raise IsolatedLiveBlocked(f"launchctl bootstrap failed for exact label {agent.label}")
        self._wait_loaded(agent.label, True)

    def _v3_agents(self) -> tuple[HostLaunchAgent, ...]:
        by_role = {role.role: role for role in self.deployment.roles}
        return tuple(
            HostLaunchAgent(
                ROLE_LABELS[role],
                BoundArtifact(
                    self.launchagents_directory / f"{ROLE_LABELS[role]}.plist",
                    by_role[role].plist.sha256,
                ),
            )
            for role in START_ORDER
        )

    def _assert_deployment(self, deployment: VerifiedDeployment) -> None:
        if (
            deployment.release_id != self.deployment.release_id
            or deployment.release_root != self.deployment.release_root
            or deployment.deployment_root != self.deployment.deployment_root
            or deployment.runtime_root != self.deployment.runtime_root
            or deployment.service_manifest != self.deployment.service_manifest
            or deployment.ownership_manifest != self.deployment.ownership_manifest
            or deployment.roles != self.deployment.roles
        ):
            raise IsolatedLiveBlocked("host adapter received a different verified deployment")

    def activate_maintenance_blackout(self) -> Mapping[str, Any]:
        # V2 has no scheduler-aware blackout primitive.  Quiescing every exact
        # release-owned launchagent is the only truthful way to block new jobs,
        # notifications, and portal writes before the isolated handoff.
        for agent in reversed(self.v2_agents):
            self._assert_agent_unchanged(agent)
            self._bootout(agent.label)
        return {
            "ok": True,
            "active": True,
            "blocked_priorities": ["P2", "P3", "P4"],
            "portal_write_and_destructive_catchup": False,
            "quiesced_v2_labels": [agent.label for agent in self.v2_agents],
        }

    def deactivate_maintenance_blackout(self) -> Mapping[str, Any]:
        # Restoration is performed separately; this receipt only proves that no
        # adapter-level blackout state remains latched.
        return {"ok": True, "active": False}

    @staticmethod
    def _live_pidfile(path: Path) -> bool:
        try:
            text = path.read_text(encoding="utf-8")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            raw = payload.get("pid") if isinstance(payload, dict) else re.search(r"\b(\d+)\b", text)
            pid = int(raw or 0) if not hasattr(raw, "group") else int(raw.group(1))
            if pid <= 1:
                return False
            os.kill(pid, 0)
            return True
        except (OSError, TypeError, ValueError):
            return False

    def _release_specs(self) -> tuple[ReleaseSpec, ReleaseSpec]:
        live_v2_pidfiles = tuple(path for path in self._v2_spec.pidfiles if self._live_pidfile(path))
        v2 = ReleaseSpec(
            name="v2",
            root=self.v2_root,
            namespace="MAGI_v2",
            pidfiles=live_v2_pidfiles,
            launchd_labels=tuple(agent.label for agent in self.v2_agents),
            launchd_plists={agent.label: agent.plist.path for agent in self.v2_agents},
            ownership_files=(),
            pidfiles_required=False,
            launchd_labels_required=True,
        )
        v3_pid_root = self.deployment.runtime_root / "pids"
        v3_pidfiles = (
            tuple(path for path in sorted(v3_pid_root.glob("*.pid")) if self._live_pidfile(path))
            if v3_pid_root.is_dir()
            else ()
        )
        v3 = ReleaseSpec(
            name="v3",
            root=self.deployment.release_root,
            namespace="magi_v3",
            pidfiles=v3_pidfiles,
            launchd_labels=tuple(ROLE_LABELS[role] for role in START_ORDER),
            launchd_plists={agent.label: agent.plist.path for agent in self._v3_agents()},
            ownership_files=(),
            pidfiles_required=False,
            launchd_labels_required=True,
        )
        return v2, v3

    def collect_ownership_snapshot(self) -> Snapshot:
        with observe_probe_commands(self._record_probe_subprocess):
            snapshot = self.snapshot_collector(self._release_specs(), tuple(DEFAULT_PORTS))
        if not isinstance(snapshot, Snapshot):
            raise IsolatedLiveBlocked("ownership collector returned an invalid snapshot")
        self._record(
            "ownership-snapshot",
            {
                "schema_version": 1,
                "collector": "release-bound ps/lsof/pidfile/launchd/ownership",
                "snapshot": snapshot.to_dict(),
            },
        )
        return snapshot

    def _record_probe_subprocess(
        self,
        argv: tuple[str, ...],
        result: object | None,
        error: BaseException | None,
    ) -> None:
        stdout = getattr(result, "stdout", "") if result is not None else getattr(error, "stdout", "")
        stderr = getattr(result, "stderr", "") if result is not None else getattr(error, "stderr", "")
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        self._record(
            f"ownership-command-{Path(argv[0]).name}",
            {
                "schema_version": 1,
                "argv": list(argv),
                "returncode": (
                    int(getattr(result, "returncode"))
                    if result is not None and hasattr(result, "returncode")
                    else None
                ),
                "stdout": str(stdout or ""),
                "stderr": str(stderr or ""),
                "error_type": type(error).__name__ if error is not None else None,
                "error": str(error) if error is not None else "",
            },
        )

    def stop_v2(self) -> Mapping[str, Any]:
        for agent in reversed(self.v2_agents):
            self._assert_agent_unchanged(agent)
            self._bootout(agent.label)
        return {"ok": True, "stopped_labels": [agent.label for agent in self.v2_agents]}

    def _resource_agent(
        self, label: str, state: Mapping[str, Any]
    ) -> HostLaunchAgent:
        for agent in self.v2_agents:
            if agent.label == label:
                _verify_bound(agent.plist, description=f"resource-window plist {label}")
                return agent
        raw_path = state.get("plist")
        digest = state.get("plist_sha256")
        if not isinstance(raw_path, str) or not raw_path or not SHA256_RE.fullmatch(
            str(digest or "")
        ):
            raise IsolatedLiveBlocked(f"loaded resource label has no restorable plist: {label}")
        path = Path(raw_path).resolve(strict=False)
        expected = self.launchagents_directory / f"{label}.plist"
        if path != expected:
            raise IsolatedLiveBlocked(f"resource label plist path drifted: {label}")
        agent = HostLaunchAgent(label, BoundArtifact(path, str(digest)))
        _verify_bound(agent.plist, description=f"resource-window plist {label}")
        payload = plistlib.loads(path.read_bytes())
        if not isinstance(payload, dict) or payload.get("Label") != label:
            raise IsolatedLiveBlocked(f"resource label plist identity drifted: {label}")
        return agent

    def capture_resource_window_host_state(
        self, labels: Sequence[str]
    ) -> Mapping[str, Any]:
        if tuple(labels) != REQUIRED_STOPPED_LABELS:
            raise IsolatedLiveBlocked("resource window requested a different stopped-label set")
        if self._resource_window_capture is not None:
            raise IsolatedLiveBlocked("resource-window initial state was already captured")
        states: list[dict[str, Any]] = []
        for label in labels:
            status = self._launchd_status(label)
            plist_path = self.launchagents_directory / f"{label}.plist"
            plist_sha = ""
            plist_value = ""
            if plist_path.exists() or plist_path.is_symlink():
                if plist_path.is_symlink() or not plist_path.is_file():
                    raise IsolatedLiveBlocked(f"resource label plist is unsafe: {label}")
                payload = plistlib.loads(plist_path.read_bytes())
                if not isinstance(payload, dict) or payload.get("Label") != label:
                    raise IsolatedLiveBlocked(f"resource label plist identity is invalid: {label}")
                plist_sha = _sha256_file(plist_path)
                plist_value = str(plist_path)
            if status["loaded"] and not plist_sha:
                raise IsolatedLiveBlocked(f"loaded resource label cannot be restored: {label}")
            states.append(
                {
                    "label": label,
                    "loaded": bool(status["loaded"]),
                    "pid": status.get("pid"),
                    "state": status.get("state", ""),
                    "plist": plist_value,
                    "plist_sha256": plist_sha,
                    "launchctl_receipt": status["launchctl_receipt"],
                    "launchctl_receipt_sha256": status[
                        "launchctl_receipt_sha256"
                    ],
                }
            )
        capture = _receipt(
            "capture_initial_state",
            {
                "ok": True,
                "labels": list(labels),
                "states": states,
                "captured_at": self.clock().astimezone(timezone.utc).isoformat(),
            },
        )
        self._resource_window_capture = capture
        self._record("resource-window-capture", capture)
        return capture

    def _assert_resource_capture(self, capture: Mapping[str, Any]) -> str:
        digest = _verify_resource_capture(capture)
        if (
            self._resource_window_capture is None
            or self._resource_window_capture.get("receipt_sha256") != digest
        ):
            raise IsolatedLiveBlocked("resource-window capture differs from host capture")
        return digest

    def stop_resource_window_labels(
        self, capture: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        capture_sha = self._assert_resource_capture(capture)
        for label in reversed(REQUIRED_STOPPED_LABELS):
            self._bootout(label)
        states = [
            {"label": label, **self._launchd_status(label)}
            for label in REQUIRED_STOPPED_LABELS
        ]
        ok = all(status.get("loaded") is False for status in states)
        receipt = _receipt(
            "stop_required_labels",
            {
                "ok": ok,
                "capture_receipt_sha256": capture_sha,
                "labels": list(REQUIRED_STOPPED_LABELS),
                "states": states,
            },
        )
        self._record("resource-window-stopped-labels", receipt)
        return receipt

    def _resource_probe_command(
        self, argv: tuple[str, ...], *, name: str, allowed_returncodes: set[int]
    ) -> dict[str, Any]:
        started = self.clock().astimezone(timezone.utc).isoformat()
        try:
            raw = self.runner(
                list(argv),
                capture_output=True,
                text=True,
                timeout=self.command_timeout_seconds,
                check=False,
            )
            result = {
                "argv": list(argv),
                "returncode": int(getattr(raw, "returncode")),
                "stdout": str(getattr(raw, "stdout", "") or ""),
                "stderr": str(getattr(raw, "stderr", "") or ""),
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as exc:
            result = {
                "argv": list(argv),
                "returncode": 124,
                "stdout": str(exc.stdout or ""),
                "stderr": str(exc.stderr or ""),
                "timed_out": True,
            }
        result["started_at"] = started
        result["finished_at"] = self.clock().astimezone(timezone.utc).isoformat()
        result["receipt_sha256"] = _sha256_bytes(
            (
                json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
        )
        self._record(f"resource-window-{name}", result)
        if result["timed_out"] or result["returncode"] not in allowed_returncodes:
            raise IsolatedLiveBlocked(f"resource-window {name} probe failed")
        return result

    @staticmethod
    def _resource_listener_pids(text: str) -> set[int]:
        current: int | None = None
        result: set[int] = set()
        wanted = set(REQUIRED_OBSERVED_PORTS)
        for line in text.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                current = int(line[1:])
            elif line.startswith("n") and current is not None:
                match = re.search(r":(\d+)(?:\s|$)", line[1:])
                if match and int(match.group(1)) in wanted:
                    result.add(current)
        return result

    def collect_resource_window_zero_receipt(
        self, capture: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        capture_sha = self._assert_resource_capture(capture)
        launchd = [
            {"label": label, **self._launchd_status(label)}
            for label in REQUIRED_STOPPED_LABELS
        ]
        ps = self._resource_probe_command(
            ("/bin/ps", "-axo", "pid=,uid=,ppid=,pgid=,command="),
            name="zero-ps",
            allowed_returncodes={0},
        )
        lsof = self._resource_probe_command(
            (
                "/usr/sbin/lsof",
                "-b",
                "-nP",
                "-a",
                "-iTCP",
                "-sTCP:LISTEN",
                "-Fpn",
            ),
            name="zero-lsof",
            allowed_returncodes={0, 1},
        )
        process_rows: list[dict[str, Any]] = []
        ps_lines = [line for line in str(ps["stdout"]).splitlines() if line.strip()]
        unparsed_ps_rows: list[str] = []
        for line in ps_lines:
            match = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(.+)$", line)
            if match:
                process_rows.append(
                    {
                        "pid": int(match.group(1)),
                        "command": match.group(5),
                    }
                )
            else:
                unparsed_ps_rows.append(line)
        v2 = [
            row for row in process_rows if str(self.v2_root) in row["command"]
        ]
        models = [
            row
            for row in process_rows
            if any(pattern.lower() in row["command"].lower() for pattern in REQUIRED_MODEL_OWNER_PATTERNS)
        ]
        listener_pids = sorted(self._resource_listener_pids(str(lsof["stdout"])))
        ok = (
            all(status.get("loaded") is False for status in launchd)
            and bool(process_rows)
            and not unparsed_ps_rows
            and ps.get("returncode") == 0
            and ps.get("stderr") == ""
            and lsof.get("returncode") in {0, 1}
            and lsof.get("stderr") == ""
            and not (
                lsof.get("returncode") == 1 and lsof.get("stdout") != ""
            )
            and not v2
            and not models
            and not listener_pids
        )
        receipt = _receipt(
            "prove_zero_ownership",
            {
                "ok": ok,
                "capture_receipt_sha256": capture_sha,
                "labels": list(REQUIRED_STOPPED_LABELS),
                "launchd": launchd,
                "observed_ports": list(REQUIRED_OBSERVED_PORTS),
                "v2_processes": v2,
                "model_processes": models,
                "listener_pids": listener_pids,
                "unparsed_ps_rows": unparsed_ps_rows,
                "ps_receipt": ps,
                "lsof_receipt": lsof,
                "coverage": ["launchd", "ownership", "pidfile", "port", "process"],
            },
        )
        self._record("resource-window-zero-proof", receipt)
        return receipt

    def restore_resource_window_labels(
        self, capture: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        capture_sha = self._assert_resource_capture(capture)
        rows = {
            str(row["label"]): row
            for row in capture.get("states", [])
            if isinstance(row, dict) and isinstance(row.get("label"), str)
        }
        errors: list[str] = []
        for label in (*REQUIRED_STOPPED_LABELS[1:], REQUIRED_STOPPED_LABELS[0]):
            row = rows.get(label)
            if row is None:
                errors.append(f"{label}: capture row missing")
                continue
            try:
                status = self._launchd_status(label)
                if row.get("loaded") is True:
                    agent = self._resource_agent(label, row)
                    self._bootstrap(agent, allow_already_loaded=True)
                elif status.get("loaded") is True:
                    self._bootout(label)
            except BaseException as exc:
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
        states: list[dict[str, Any]] = []
        for label in REQUIRED_STOPPED_LABELS:
            try:
                states.append({"label": label, **self._launchd_status(label)})
            except BaseException as exc:
                errors.append(f"{label}: verify: {type(exc).__name__}: {exc}")
                states.append(
                    {
                        "label": label,
                        "loaded": None,
                        "pid": None,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
        restored = all(
            bool(states[index].get("loaded"))
            is bool(rows.get(label, {}).get("loaded"))
            for index, label in enumerate(REQUIRED_STOPPED_LABELS)
        ) and not errors
        receipt = _receipt(
            "restore_initial_state",
            {
                "ok": restored,
                "capture_receipt_sha256": capture_sha,
                "labels": list(REQUIRED_STOPPED_LABELS),
                "states": states,
                "errors": errors,
            },
        )
        self._record("resource-window-restore", receipt)
        return receipt

    def verify_resource_window_readiness(
        self, capture: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        capture_sha = self._assert_resource_capture(capture)
        rows = {
            str(row["label"]): row
            for row in capture.get("states", [])
            if isinstance(row, dict) and isinstance(row.get("label"), str)
        }
        states = [
            {"label": label, **self._launchd_status(label)}
            for label in REQUIRED_STOPPED_LABELS
        ]
        exact = all(
            bool(states[index].get("loaded")) is bool(rows[label].get("loaded"))
            for index, label in enumerate(REQUIRED_STOPPED_LABELS)
        )
        urls: list[str] = []
        if rows.get(V2_REQUIRED_LABEL, {}).get("loaded") is True:
            urls.extend(RESOURCE_V2_READINESS_URLS)
        for label, url in RESOURCE_MODEL_READINESS.items():
            if rows.get(label, {}).get("loaded") is True:
                urls.append(url)
        readiness: dict[str, Any] = {}
        for url in urls:
            result = self._http("GET", url, name=f"resource-restore-{url.rsplit(':', 1)[-1]}")
            readiness[url] = {
                "ok": result.get("ok") is True and result.get("status_code") == 200,
                "status_code": result.get("status_code"),
                "body_sha256": result.get("body_sha256"),
                "artifact": result.get("artifact"),
            }
        ready = all(row["ok"] is True for row in readiness.values())
        receipt = _receipt(
            "verify_restored_readiness",
            {
                "ok": exact and ready,
                "capture_receipt_sha256": capture_sha,
                "labels": list(REQUIRED_STOPPED_LABELS),
                "states": states,
                "readiness": readiness,
                "required_urls": urls,
                "originally_inactive_not_started": sorted(
                    label
                    for label in REQUIRED_STOPPED_LABELS
                    if rows.get(label, {}).get("loaded") is False
                    and states[REQUIRED_STOPPED_LABELS.index(label)].get("loaded") is False
                ),
            },
        )
        self._record("resource-window-readiness", receipt)
        return receipt

    def install_validation(self, deployment: VerifiedDeployment) -> Mapping[str, Any]:
        self._assert_deployment(deployment)
        if deployment.runtime_root.exists() or deployment.runtime_root.is_symlink():
            raise IsolatedLiveBlocked("canonical V3 runtime must be absent before validation install")
        for agent in self._v3_agents():
            if agent.plist.path.exists() or agent.plist.path.is_symlink():
                raise IsolatedLiveBlocked(f"V3 validation plist target already exists: {agent.label}")
        deployment.runtime_root.mkdir(parents=True, mode=0o700)
        self._runtime_created = True
        for row in deployment.payload["roles"]:
            for key in ("state_dir", "log_dir"):
                path = Path(str(row[key])).resolve(strict=False)
                if not _inside(path, deployment.runtime_root):
                    raise IsolatedLiveBlocked(f"validation role {key} escapes runtime")
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
            pid_parent = Path(str(row["pid_file"])).resolve(strict=False).parent
            if not _inside(pid_parent, deployment.runtime_root):
                raise IsolatedLiveBlocked("validation role PID path escapes runtime")
            pid_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        ownership_target = deployment.runtime_root / "ownership" / "ownership-manifest.json"
        _copy_no_clobber(deployment.ownership_manifest, ownership_target)
        by_role = {role.role: role for role in deployment.roles}
        installed: dict[str, str] = {}
        for role in START_ORDER:
            validation_role = by_role[role]
            target = self.launchagents_directory / f"{validation_role.label}.plist"
            _copy_no_clobber(validation_role.plist, target, mode=0o644)
            self._installed_v3_labels.add(validation_role.label)
            installed[validation_role.label] = validation_role.plist.sha256
        marker = deployment.runtime_root / "isolated-live-host-install.json"
        _write_exclusive(
            marker,
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "deployment_mode": DEPLOYMENT_MODE,
                        "release_id": deployment.release_id,
                        "service_manifest_sha256": deployment.service_manifest.sha256,
                        "ownership_manifest_sha256": deployment.ownership_manifest.sha256,
                        "plist_sha256": installed,
                    },
                    sort_keys=True,
                )
                + "\n"
            ).encode(),
        )
        return {
            "ok": True,
            "deployment_mode": DEPLOYMENT_MODE,
            "ownership_manifest_sha256": deployment.ownership_manifest.sha256,
            "plist_sha256": installed,
            "host_install_marker_sha256": _sha256_file(marker),
        }

    def start_v3_role(self, role: ValidationRole) -> Mapping[str, Any]:
        expected_index = len(self._started_roles)
        if expected_index >= len(START_ORDER) or role.role != START_ORDER[expected_index]:
            raise IsolatedLiveBlocked("V3 roles must start in control/gateway/supervisor order")
        expected = next(item for item in self.deployment.roles if item.role == role.role)
        if role != expected:
            raise IsolatedLiveBlocked("V3 start role is not the release-bound role")
        agent = next(item for item in self._v3_agents() if item.label == role.label)
        self._bootstrap(agent, allow_already_loaded=False)
        self._started_roles.append(role.role)
        status = self._launchd_status(role.label)
        return {"ok": True, "role": role.role, "label": role.label, "pid": status.get("pid")}

    def _http(self, method: str, url: str, *, name: str) -> Mapping[str, Any]:
        request = Request(url, method=method, headers={"Accept": "application/json, text/plain"})
        started = self.clock().astimezone(timezone.utc).isoformat()
        status = 0
        headers: dict[str, str] = {}
        body = b""
        error = ""
        try:
            with self.http_opener(request, self.command_timeout_seconds) as response:
                status = int(response.status)
                headers = {str(key): str(value) for key, value in response.headers.items()}
                body = response.read(MAX_HTTP_BODY_BYTES + 1)
        except HTTPError as exc:
            status = int(exc.code)
            headers = {str(key): str(value) for key, value in exc.headers.items()}
            body = exc.read(MAX_HTTP_BODY_BYTES + 1)
            error = str(exc)
        except (OSError, URLError, TimeoutError) as exc:
            error = str(exc)
        if len(body) > MAX_HTTP_BODY_BYTES:
            raise IsolatedLiveBlocked(f"HTTP response exceeds the 1 MiB validation bound: {url}")
        body_path = self.artifact_directory / (
            f"{self._artifact_sequence + 1:04d}-{re.sub(r'[^A-Za-z0-9._-]+', '-', name)}.body"
        )
        _write_exclusive(body_path, body)
        payload: Any = None
        if body:
            try:
                payload = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
        self._record(
            f"http-{name}",
            {
                "schema_version": 1,
                "started_at": started,
                "finished_at": self.clock().astimezone(timezone.utc).isoformat(),
                "method": method,
                "url": url,
                "status_code": status,
                "headers": headers,
                "body_artifact": str(body_path),
                "body_sha256": _sha256_bytes(body),
                "body_size": len(body),
                "error": error,
            },
        )
        return {
            "ok": not error and 200 <= status < 300,
            "status_code": status,
            "headers": headers,
            "body_sha256": _sha256_bytes(body),
            "json": payload,
            "artifact": str(body_path),
        }

    def probe(self, probe: ProbeSpec) -> Mapping[str, Any]:
        _validate_probe(probe)
        return self._http(
            probe.method,
            probe.url,
            name=f"v3-{probe.method.lower()}-{probe.url.split('127.0.0.1:', 1)[-1]}",
        )

    def run_native_ime_candidate_probe(
        self, deployment: VerifiedDeployment
    ) -> Mapping[str, Any]:
        """Run the native UI probe through the exact immutable candidate."""

        self._assert_deployment(deployment)
        manifest_path = deployment.release_root / "release-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(files, list):
            raise IsolatedLiveBlocked("candidate release manifest has no file inventory")
        inventory = {
            str(row.get("path")): str(row.get("sha256"))
            for row in files
            if isinstance(row, dict)
            and isinstance(row.get("path"), str)
            and isinstance(row.get("sha256"), str)
        }
        launcher = deployment.release_root / "bin/magi-v3-python"
        source = deployment.release_root / "scripts/v3_validation/ime_candidate_probe.py"
        launcher_sha = inventory.get("bin/magi-v3-python", "")
        source_sha = inventory.get("scripts/v3_validation/ime_candidate_probe.py", "")
        if (
            manifest.get("release_id") != deployment.release_id
            or not SHA256_RE.fullmatch(launcher_sha)
            or not SHA256_RE.fullmatch(source_sha)
            or _sha256_file(launcher) != launcher_sha
            or _sha256_file(source) != source_sha
            or not os.access(launcher, os.X_OK)
        ):
            raise IsolatedLiveBlocked("native IME candidate launcher/source binding failed")
        evidence_path = self.artifact_directory / (
            f"{self._artifact_sequence + 1:04d}-native-ime-candidate-probe.json"
        )
        argv = (
            str(launcher),
            str(source),
            "--cycles",
            "3",
            "--pressure-mb",
            "256",
            "--timeout-sec",
            "2",
            "--evidence",
            str(evidence_path),
        )
        from scripts.v3_validation.ime_candidate_probe import (
            _activate_process,
            _close_isolated_document,
            _current_input_source_id,
            _frontmost_application,
            _quit_textedit,
            _select_input_source,
            _textedit_document_count,
            _textedit_running,
            _wait_for_textedit_stopped,
        )

        input_source_before = _current_input_source_id()
        frontmost_before = _frontmost_application()
        textedit_running_before = _textedit_running()
        textedit_documents_before = (
            _textedit_document_count() if textedit_running_before else 0
        )
        if not input_source_before or not frontmost_before:
            raise IsolatedLiveBlocked(
                "native IME host state is not observable before candidate execution"
            )
        started = self.clock().astimezone(timezone.utc).isoformat()
        try:
            raw = self.runner(
                list(argv),
                cwd=str(deployment.release_root),
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
            returncode = int(getattr(raw, "returncode"))
            stdout = str(getattr(raw, "stdout", "") or "")
            stderr = str(getattr(raw, "stderr", "") or "")
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            returncode = 124
            stdout = str(exc.stdout or "")
            stderr = str(exc.stderr or "")
            timed_out = True
        cleanup_errors: list[str] = []
        try:
            for _attempt in range(5):
                running = _textedit_running()
                documents = _textedit_document_count() if running else 0
                if documents <= textedit_documents_before:
                    break
                if not _close_isolated_document():
                    cleanup_errors.append("close_unsaved_textedit_document_failed")
                    break
            running = _textedit_running()
            documents = _textedit_document_count() if running else 0
            if documents != textedit_documents_before:
                cleanup_errors.append("textedit_document_baseline_not_restored")
            if not textedit_running_before and running:
                _quit_textedit()
                _wait_for_textedit_stopped()
            if _current_input_source_id() != input_source_before:
                if not _select_input_source(input_source_before):
                    cleanup_errors.append("input_source_restore_rejected")
            if _current_input_source_id() != input_source_before:
                cleanup_errors.append("input_source_not_restored")
            _activate_process(frontmost_before)
            self.sleeper(0.2)
            if _frontmost_application() != frontmost_before:
                cleanup_errors.append("frontmost_application_not_restored")
            if _textedit_running() != textedit_running_before:
                cleanup_errors.append("textedit_process_state_not_restored")
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            cleanup_errors.append(f"host_cleanup_exception:{type(exc).__name__}")
        host_cleanup = {
            "input_source_restored": "input_source_not_restored" not in cleanup_errors
            and "input_source_restore_rejected" not in cleanup_errors,
            "frontmost_application_restored": "frontmost_application_not_restored"
            not in cleanup_errors,
            "textedit_document_baseline_restored": not any(
                item
                in {
                    "close_unsaved_textedit_document_failed",
                    "textedit_document_baseline_not_restored",
                    "textedit_process_state_not_restored",
                }
                for item in cleanup_errors
            ),
            "errors": cleanup_errors,
        }
        command_receipt = {
            "schema_version": 1,
            "started_at": started,
            "finished_at": self.clock().astimezone(timezone.utc).isoformat(),
            "argv_sha256": _sha256_bytes("\0".join(argv).encode()),
            "launcher_sha256": launcher_sha,
            "probe_source_sha256": source_sha,
            "returncode": returncode,
            "stdout_sha256": _sha256_bytes(stdout.encode()),
            "stderr_sha256": _sha256_bytes(stderr.encode()),
            "timed_out": timed_out,
            "host_cleanup": host_cleanup,
        }
        command_artifact = self._record("native-ime-candidate-command", command_receipt)
        if returncode != 0 or timed_out or cleanup_errors:
            raise IsolatedLiveBlocked(
                f"native IME candidate probe failed; artifact={command_artifact}"
            )
        if not evidence_path.is_file() or evidence_path.is_symlink():
            raise IsolatedLiveBlocked("native IME candidate probe produced no safe evidence file")
        try:
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IsolatedLiveBlocked("native IME candidate evidence is invalid JSON") from exc
        if not isinstance(evidence, dict):
            raise IsolatedLiveBlocked("native IME candidate evidence must be an object")
        evidence_sha = _sha256_bytes(
            json.dumps(
                evidence,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        )
        return {
            "ok": evidence.get("status") == "passed",
            "candidate_release_id": deployment.release_id,
            "candidate_launcher_verified": True,
            "candidate_probe_source_verified": True,
            "launcher_sha256": launcher_sha,
            "probe_source_sha256": source_sha,
            "command_receipt": command_receipt,
            "command_artifact": str(command_artifact),
            "command_artifact_sha256": _sha256_file(command_artifact),
            "evidence_artifact": str(evidence_path),
            "evidence_sha256": evidence_sha,
            "evidence": evidence,
        }

    def stop_v3_role(self, role: ValidationRole) -> Mapping[str, Any]:
        expected = next(item for item in self.deployment.roles if item.role == role.role)
        if role != expected:
            raise IsolatedLiveBlocked("V3 stop role is not the release-bound role")
        if self._started_roles and role.role in self._started_roles and self._started_roles[-1] != role.role:
            raise IsolatedLiveBlocked("V3 roles must stop in supervisor/gateway/control order")
        self._bootout(role.label)
        if role.role in self._started_roles:
            self._started_roles.remove(role.role)
        return {"ok": True, "role": role.role, "label": role.label, "loaded": False}

    def _preserve_runtime_logs(self) -> list[dict[str, Any]]:
        root = self.deployment.runtime_root
        if not root.exists():
            return []
        output_root = self.artifact_directory / "service-stdout-stderr"
        preserved: list[dict[str, Any]] = []
        for source in sorted(root.rglob("*")):
            if source.is_symlink():
                raise IsolatedLiveBlocked(f"validation runtime contains a symlink: {source}")
            if not source.is_file():
                continue
            if source.name not in {"stdout.log", "stderr.log"}:
                continue
            relative = source.relative_to(root)
            target = output_root / relative
            data = source.read_bytes()
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_file() or _sha256_file(target) != _sha256_bytes(data):
                    raise IsolatedLiveBlocked(
                        f"preserved service log artifact identity drifted: {target}"
                    )
            else:
                _write_exclusive(target, data)
            preserved.append(
                {
                    "source": str(source),
                    "artifact": str(target),
                    "sha256": _sha256_bytes(data),
                    "size": len(data),
                }
            )
        self._record("service-stdout-stderr-index", {"schema_version": 1, "files": preserved})
        return preserved

    def remove_validation(self, deployment: VerifiedDeployment) -> Mapping[str, Any]:
        self._assert_deployment(deployment)
        if self._runtime_created:
            self._preserve_runtime_logs()
        removed = 0
        for agent in reversed(self._v3_agents()):
            target = agent.plist.path
            if agent.label in self._installed_v3_labels and (target.exists() or target.is_symlink()):
                _verify_bound(agent.plist, description=f"installed V3 plist {agent.label}")
                target.unlink()
                self._installed_v3_labels.remove(agent.label)
                removed += 1
        _fsync_directory(self.launchagents_directory)
        runtime = deployment.runtime_root
        if self._runtime_created and (runtime.exists() or runtime.is_symlink()):
            if runtime.is_symlink() or runtime.resolve(strict=False) != runtime:
                raise IsolatedLiveBlocked("validation runtime identity is unsafe during cleanup")
            ownership = runtime / "ownership" / "ownership-manifest.json"
            if ownership.exists() or ownership.is_symlink():
                _verify_bound(
                    BoundArtifact(ownership, deployment.ownership_manifest.sha256),
                    description="installed validation ownership manifest",
                )
            for candidate in runtime.rglob("*"):
                if candidate.is_symlink():
                    raise IsolatedLiveBlocked(f"validation runtime cleanup found symlink: {candidate}")
            shutil.rmtree(runtime)
            _fsync_directory(runtime.parent)
            self._runtime_created = False
        remaining = sum(
            int(agent.plist.path.exists() or agent.plist.path.is_symlink())
            for agent in self._v3_agents()
        ) + int(runtime.exists() or runtime.is_symlink())
        return {
            "ok": remaining == 0,
            "validation_artifacts_removed": remaining == 0,
            "runtime_ownership_removed": not runtime.exists(),
            "remaining_validation_artifacts": remaining,
            "removed_plists": removed,
        }

    def restore_v2(self) -> Mapping[str, Any]:
        restored: list[str] = []
        errors: list[str] = []
        for agent in self.v2_agents:
            try:
                self._assert_agent_unchanged(agent)
                before = self._launchd_status(agent.label)
                should_be_loaded = agent.label in self._initially_loaded_v2
                if should_be_loaded:
                    self._bootstrap(agent, allow_already_loaded=True)
                    if not before["loaded"]:
                        restored.append(agent.label)
                elif before["loaded"]:
                    self._bootout(agent.label)
            except BaseException as exc:
                errors.append(f"{agent.label}: {type(exc).__name__}: {exc}")
        return {
            "ok": not errors,
            "restored_labels": restored,
            "initially_loaded_labels": sorted(self._initially_loaded_v2),
            "errors": errors,
        }

    def verify_v2_readiness_integrity(self) -> Mapping[str, Any]:
        label_status: dict[str, Any] = {}
        integrity_ok = self.v2_root.is_dir() and not self.v2_root.is_symlink()
        for agent in self.v2_agents:
            try:
                self._assert_agent_unchanged(agent)
                label_status[agent.label] = self._launchd_status(agent.label)
                label_status[agent.label]["plist_sha256"] = agent.plist.sha256
                integrity_ok = integrity_ok and (
                    bool(label_status[agent.label]["loaded"])
                    is (agent.label in self._initially_loaded_v2)
                )
            except BaseException as exc:
                integrity_ok = False
                label_status[agent.label] = {
                    "loaded": False,
                    "plist_sha256": agent.plist.sha256,
                    "error": str(exc),
                }
        readiness: dict[str, Any] = {}
        ready = True
        for url in V2_READINESS_URLS:
            result = self._http("GET", url, name=f"v2-readiness-{url.rsplit(':', 1)[-1]}")
            readiness[url] = {
                "ok": result.get("ok"),
                "status_code": result.get("status_code"),
                "body_sha256": result.get("body_sha256"),
                "artifact": result.get("artifact"),
            }
            ready = ready and result.get("ok") is True and result.get("status_code") == 200
        return {
            "ok": integrity_ok and ready,
            "ready": ready,
            "integrity_ok": integrity_ok,
            "launchd": label_status,
            "readiness": readiness,
        }

    def inspect(self) -> dict[str, Any]:
        snapshot = self.collect_ownership_snapshot()
        assessment = assess_snapshot(snapshot)
        v2_readiness = self.verify_v2_readiness_integrity()
        return {
            "schema_version": 1,
            "mode": "inspect_dry_run",
            "mutation_performed": False,
            "execution_authorized": False,
            "deployment_mode": DEPLOYMENT_MODE,
            "release_id": self.deployment.release_id,
            "release_manifest_sha256": str(
                self.deployment.payload.get("release_manifest_sha256", "")
            ),
            "service_manifest_sha256": self.deployment.service_manifest.sha256,
            "v2_launchagents": [
                {
                    "label": agent.label,
                    "plist": str(agent.plist.path),
                    "sha256": agent.plist.sha256,
                    "initially_loaded": agent.label in self._initially_loaded_v2,
                }
                for agent in self.v2_agents
            ],
            "v3_launchagent_targets": [str(agent.plist.path) for agent in self._v3_agents()],
            "v3_runtime_exists": self.deployment.runtime_root.exists(),
            "ownership": assessment.to_dict(),
            "v2_readiness": v2_readiness,
            "snapshot_artifacts": str(self.artifact_directory),
            "planned_start_order": list(START_ORDER),
            "planned_stop_order": list(reversed(START_ORDER)),
        }


AdapterFactory = Callable[..., MacOSIsolatedLiveMachine]


def _validate_execute_context(
    args: argparse.Namespace,
    plan: Any,
    deployment: VerifiedDeployment,
    *,
    now: datetime,
) -> None:
    expected = {
        "--expected-release-manifest-sha256": (
            args.expected_release_manifest_sha256,
            plan.release_manifest.sha256,
        ),
        "--expected-deploy-manifest-sha256": (
            args.expected_deploy_manifest_sha256,
            plan.deploy_manifest.sha256,
        ),
        "--expected-offline-gate-sha256": (
            args.expected_offline_gate_sha256,
            plan.offline_gate_report.sha256,
        ),
    }
    for flag, (provided, actual) in expected.items():
        if provided is None or provided != actual:
            raise IsolatedLiveBlocked(f"execute requires exact matching {flag}")
    if args.token_file is None or args.report_output is None:
        raise IsolatedLiveBlocked("execute requires --token-file and --report-output")
    token = args.token_file.expanduser()
    report_output = args.report_output.expanduser()
    if (
        not token.is_absolute()
        or token.resolve(strict=False) != token
        or not report_output.is_absolute()
        or report_output.resolve(strict=False) != report_output
    ):
        raise IsolatedLiveBlocked("execute token and report output must be canonical absolute paths")
    protected_roots = (
        deployment.release_root,
        deployment.deployment_root,
        deployment.runtime_root,
        deployment.validation_input_root,
        _canonical_v2_root(),
        _canonical_launchagents(),
    )
    if any(
        _inside(path, root) or path == root
        for path in (token, report_output)
        for root in protected_roots
    ):
        raise IsolatedLiveBlocked(
            "execute token and report output must not overlap release or runtime domains"
        )
    try:
        metadata = token.lstat()
    except OSError as exc:
        raise IsolatedLiveBlocked(f"execute token is unavailable: {exc}") from exc
    if (
        token.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
    ):
        raise IsolatedLiveBlocked("execute token must be owner-only 0600 with one link")
    window = _release_cutover_window(plan, deployment, now=now)
    if window["within_window"] is not True:
        raise IsolatedLiveBlocked("execute refused outside the release-bound Asia/Taipei window")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--artifact-directory", type=Path, required=True)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--report-output", type=Path)
    parser.add_argument("--expected-release-manifest-sha256")
    parser.add_argument("--expected-deploy-manifest-sha256")
    parser.add_argument("--expected-offline-gate-sha256")
    parser.add_argument(
        "--execute-isolated-live",
        action="store_true",
        help="perform one armed isolated validation; omission is inspect-only",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    adapter_factory: AdapterFactory = MacOSIsolatedLiveMachine,
    clock: Clock | None = None,
    platform_system: Callable[[], str] = platform.system,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    now = (clock or (lambda: datetime.now(timezone.utc)))()
    execution_started = False
    try:
        plan = load_isolated_live_plan(args.plan, args.plan_sha256)
        deployment = verify_static_plan(plan)
        if args.execute_isolated_live:
            if platform_system() != "Darwin":
                raise IsolatedLiveBlocked("execute is available only on macOS Darwin")
            _validate_execute_context(args, plan, deployment, now=now)
        adapter = adapter_factory(
            deployment,
            artifact_directory=args.artifact_directory,
            clock=clock,
            platform_system=platform_system,
        )
        if not args.execute_isolated_live:
            print(json.dumps(adapter.inspect(), ensure_ascii=False, indent=2, sort_keys=True))
            return 0
        # Refuse before the one-time token is consumed or any launchd mutation
        # when the exact V2 state that must be restored is already unhealthy.
        # This also verifies the real host routes instead of trusting a mocked
        # plan contract that may not match the currently deployed V2 services.
        v2_preflight = adapter.verify_v2_readiness_integrity()
        if (
            not isinstance(v2_preflight, Mapping)
            or v2_preflight.get("ok") is not True
            or v2_preflight.get("ready") is not True
            or v2_preflight.get("integrity_ok") is not True
        ):
            raise IsolatedLiveBlocked("preflight V2 readiness/integrity is incomplete")
        execution_started = True
        report = execute_isolated_live_validation(
            plan_path=args.plan,
            plan_sha256=args.plan_sha256,
            token_file=args.token_file,
            report_output=args.report_output,
            machine=adapter,
            clock=clock,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report.get("ok") is True else 2
    except (CutoverError, OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "blocked",
                    "mutation_performed": execution_started,
                    "mutation_state_unknown": execution_started,
                    "error": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "HostLaunchAgent",
    "LAUNCHCTL",
    "MacOSIsolatedLiveMachine",
    "V2_READINESS_URLS",
    "build_parser",
    "main",
]

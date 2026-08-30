#!/usr/bin/env python3
"""Fail-closed outer owner for the provisional 16/19 resource window.

This executor never starts the production V3 release.  It only stops V2,
proves zero ownership, invokes the separately sealed disposable collector, and
always restores V2 in ``finally``.  A successful receipt is raw input to the
later 19/19 offline recompilation; it is not release evidence by itself.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import signal
import stat
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from scripts.v3_cutover.core import assess_absolute_window, assess_cutover_window
from scripts.v3_validation.isolated_live_execute import (
    OFFLINE_MACHINE_EVIDENCE,
    IsolatedLiveBlocked,
    IsolatedLiveMachine,
    _require_receipt_ok,
    _snapshot_summary,
)
from scripts.v3_validation.isolated_resource_window import sha256_json
from scripts.v3_validation.isolated_resource_window_collector import (
    PLAN_SCHEMA as INNER_PLAN_SCHEMA,
    REQUIRED_MODEL_OWNER_PATTERNS,
    REQUIRED_OBSERVED_PORTS,
    REQUIRED_STOPPED_LABELS,
    collect as collect_resource_window,
)


SCHEMA = "magi.v3.provisional-resource-window-plan/v1"
REPORT_SCHEMA = "magi.v3.provisional-resource-window-execution/v1"
PROVISIONAL_GATE_SCHEMA = "magi.v3.provisional-resource-window-gate/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RESOURCE_EVIDENCE = frozenset(
    {
        "matched_v2_warm_cold_performance_baseline_complete",
        "resource_policy_all_budgets_passed",
        "worker_process_group_footprint_and_metal_return_to_baseline",
    }
)
PROVISIONAL_EVIDENCE = OFFLINE_MACHINE_EVIDENCE - RESOURCE_EVIDENCE


Collector = Callable[[Mapping[str, Any], str, str], Mapping[str, Any]]
Clock = Callable[[], datetime]
RESTORE_RESERVE_SECONDS = 120
MINIMUM_COLLECTOR_SECONDS = 2100
ZERO_PS_ARGV = ["/bin/ps", "-axo", "pid=,uid=,ppid=,pgid=,command="]
ZERO_LSOF_ARGV = [
    "/usr/sbin/lsof",
    "-b",
    "-nP",
    "-a",
    "-iTCP",
    "-sTCP:LISTEN",
    "-Fpn",
]


class ResourceWindowMachine(IsolatedLiveMachine, Protocol):
    def capture_resource_window_host_state(
        self, labels: Sequence[str]
    ) -> Mapping[str, Any]: ...

    def stop_resource_window_labels(
        self, capture: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def collect_resource_window_zero_receipt(
        self, capture: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def restore_resource_window_labels(
        self, capture: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...

    def verify_resource_window_readiness(
        self, capture: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


class ResourceWindowCollectorTimeout(IsolatedLiveBlocked):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(dict(value), sort_keys=True, separators=(",", ":")) + "\n").encode()


def _semantic_receipt(value: Mapping[str, Any], *, operation: str) -> dict[str, Any]:
    receipt = dict(value)
    supplied = receipt.pop("receipt_sha256", None)
    if (
        receipt.get("schema") != "magi.v3.resource-window-host-receipt/v1"
        or receipt.get("operation") != operation
        or supplied != hashlib.sha256(_canonical(receipt)).hexdigest()
        or receipt.get("ok") is not True
    ):
        raise IsolatedLiveBlocked(f"{operation} receipt is invalid or hash-mismatched")
    receipt["receipt_sha256"] = supplied
    return receipt


def _capture_sha(capture: Mapping[str, Any]) -> str:
    receipt = _semantic_receipt(capture, operation="capture_initial_state")
    labels = receipt.get("labels")
    states = receipt.get("states")
    if (
        labels != list(REQUIRED_STOPPED_LABELS)
        or not isinstance(states, list)
        or len(states) != len(REQUIRED_STOPPED_LABELS)
        or any(
            not isinstance(row, dict)
            or row.get("label") != REQUIRED_STOPPED_LABELS[index]
            or not isinstance(row.get("loaded"), bool)
            or not isinstance(row.get("launchctl_receipt"), dict)
            or not SHA256_RE.fullmatch(str(row.get("launchctl_receipt_sha256") or ""))
            or hashlib.sha256(_canonical(row["launchctl_receipt"])).hexdigest()
            != row.get("launchctl_receipt_sha256")
            or not _launchd_status_receipt_valid(
                row, label=REQUIRED_STOPPED_LABELS[index]
            )
            or (
                row.get("loaded") is True
                and not SHA256_RE.fullmatch(str(row.get("plist_sha256") or ""))
            )
            for index, row in enumerate(states)
        )
    ):
        raise IsolatedLiveBlocked("initial seven-label state receipt is incomplete")
    return str(receipt["receipt_sha256"])


def _bound_host_receipt(
    value: Mapping[str, Any], *, operation: str, capture_sha256: str
) -> dict[str, Any]:
    receipt = _semantic_receipt(value, operation=operation)
    if receipt.get("capture_receipt_sha256") != capture_sha256:
        raise IsolatedLiveBlocked(f"{operation} receipt is bound to another capture")
    return receipt


def _raw_subprocess_receipt_valid(
    value: Any,
    *,
    expected_argv: Sequence[str],
    allowed_returncodes: set[int],
) -> bool:
    if not isinstance(value, dict):
        return False
    unsigned = dict(value)
    supplied = unsigned.pop("receipt_sha256", None)
    return bool(
        SHA256_RE.fullmatch(str(supplied or ""))
        and supplied == hashlib.sha256(_canonical(unsigned)).hexdigest()
        and value.get("argv") == list(expected_argv)
        and type(value.get("returncode")) is int
        and value.get("returncode") in allowed_returncodes
        and isinstance(value.get("stdout"), str)
        and isinstance(value.get("stderr"), str)
        and value.get("stderr") == ""
        and value.get("timed_out") is False
    )


def _launchd_status_receipt_valid(
    value: Any, *, label: str, expected_argv: Sequence[str] | None = None
) -> bool:
    if not isinstance(value, dict) or value.get("label") != label:
        return False
    raw = value.get("launchctl_receipt")
    supplied = value.get("launchctl_receipt_sha256")
    argv = raw.get("argv") if isinstance(raw, dict) else None
    argv_valid = bool(
        isinstance(argv, list)
        and len(argv) == 3
        and argv[:2] == ["/bin/launchctl", "print"]
        and re.fullmatch(rf"gui/\d+/{re.escape(label)}", str(argv[2]))
        and (expected_argv is None or argv == list(expected_argv))
    )
    return bool(
        isinstance(raw, dict)
        and SHA256_RE.fullmatch(str(supplied or ""))
        and supplied == hashlib.sha256(_canonical(raw)).hexdigest()
        and argv_valid
        and type(raw.get("returncode")) is int
        and isinstance(raw.get("stdout"), str)
        and isinstance(raw.get("stderr"), str)
        and raw.get("timed_out") is False
    )


def _raw_zero_derivations(receipt: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[int]]:
    ps = receipt.get("ps_receipt")
    lsof = receipt.get("lsof_receipt")
    if not isinstance(ps, dict) or not isinstance(lsof, dict):
        raise IsolatedLiveBlocked("zero-ownership raw probes are missing")
    process_rows: list[dict[str, Any]] = []
    for line in str(ps.get("stdout") or "").splitlines():
        if not line.strip():
            continue
        match = re.match(r"\s*(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(.+)$", line)
        if match is None:
            raise IsolatedLiveBlocked("zero-ownership ps evidence is unparseable")
        process_rows.append({"pid": int(match.group(1)), "command": match.group(5)})
    if not process_rows:
        raise IsolatedLiveBlocked("zero-ownership ps evidence is empty")
    models = [
        row
        for row in process_rows
        if any(
            pattern.lower() in row["command"].lower()
            for pattern in REQUIRED_MODEL_OWNER_PATTERNS
        )
    ]
    listener_pids: set[int] = set()
    current_pid: int | None = None
    wanted = set(REQUIRED_OBSERVED_PORTS)
    for line in str(lsof.get("stdout") or "").splitlines():
        if not line:
            continue
        if line.startswith("p") and line[1:].isdigit():
            current_pid = int(line[1:])
        elif line.startswith("f"):
            continue
        elif line.startswith("n") and current_pid is not None:
            match = re.search(r":(\d+)(?:\s|$)", line[1:])
            if match and int(match.group(1)) in wanted:
                listener_pids.add(current_pid)
        else:
            raise IsolatedLiveBlocked("zero-ownership lsof evidence is unparseable")
    return models, sorted(listener_pids)


def _validate_host_receipt(
    receipt: Mapping[str, Any],
    *,
    operation: str,
    capture: Mapping[str, Any],
) -> None:
    states = receipt.get("states")
    if receipt.get("labels") != list(REQUIRED_STOPPED_LABELS):
        raise IsolatedLiveBlocked(f"{operation} receipt label set drifted")
    if operation in {"stop_required_labels", "restore_initial_state", "verify_restored_readiness"}:
        if not isinstance(states, list) or len(states) != len(REQUIRED_STOPPED_LABELS):
            raise IsolatedLiveBlocked(f"{operation} label states are incomplete")
        initial = {
            str(row["label"]): bool(row["loaded"])
            for row in capture.get("states", [])
            if isinstance(row, dict) and "label" in row and "loaded" in row
        }
        for index, row in enumerate(states):
            capture_states = capture.get("states", [])
            expected_argv = capture_states[index]["launchctl_receipt"]["argv"]
            if (
                not _launchd_status_receipt_valid(
                    row,
                    label=REQUIRED_STOPPED_LABELS[index],
                    expected_argv=expected_argv,
                )
            ):
                raise IsolatedLiveBlocked(f"{operation} launchctl receipt is invalid")
            if operation == "stop_required_labels" and row.get("loaded") is not False:
                raise IsolatedLiveBlocked("required launchd label remained loaded")
            if operation in {"restore_initial_state", "verify_restored_readiness"}:
                if bool(row.get("loaded")) is not initial[REQUIRED_STOPPED_LABELS[index]]:
                    raise IsolatedLiveBlocked("restored launchd label state drifted")
    if operation == "prove_zero_ownership":
        launchd = receipt.get("launchd")
        raw_models, raw_listener_pids = _raw_zero_derivations(receipt)
        if (
            receipt.get("coverage") != ["launchd", "ownership", "pidfile", "port", "process"]
            or receipt.get("observed_ports") != list(REQUIRED_OBSERVED_PORTS)
            or receipt.get("v2_processes") != []
            or receipt.get("model_processes") != []
            or receipt.get("listener_pids") != []
            or receipt.get("unparsed_ps_rows") != []
            or raw_models != receipt.get("model_processes")
            or raw_listener_pids != receipt.get("listener_pids")
            or not isinstance(launchd, list)
            or len(launchd) != len(REQUIRED_STOPPED_LABELS)
            or any(
                not _launchd_status_receipt_valid(
                    row,
                    label=REQUIRED_STOPPED_LABELS[index],
                    expected_argv=capture.get("states", [])[index][
                        "launchctl_receipt"
                    ]["argv"],
                )
                or row.get("loaded") is not False
                for index, row in enumerate(launchd)
            )
            or not _raw_subprocess_receipt_valid(
                receipt.get("ps_receipt"),
                expected_argv=ZERO_PS_ARGV,
                allowed_returncodes={0},
            )
            or not _raw_subprocess_receipt_valid(
                receipt.get("lsof_receipt"),
                expected_argv=ZERO_LSOF_ARGV,
                allowed_returncodes={0, 1},
            )
            or (
                receipt.get("lsof_receipt", {}).get("returncode") == 1
                and receipt.get("lsof_receipt", {}).get("stdout") != ""
            )
        ):
            raise IsolatedLiveBlocked("zero-ownership raw receipt is incomplete")
    if operation == "verify_restored_readiness":
        initial = {
            str(row["label"]): bool(row["loaded"])
            for row in capture.get("states", [])
            if isinstance(row, dict) and "label" in row and "loaded" in row
        }
        expected_urls = []
        if initial.get("com.magi.daemon"):
            expected_urls.extend(
                [
                    "http://127.0.0.1:5002/health",
                    "http://127.0.0.1:5003/health",
                    "http://127.0.0.1:8088/health",
                ]
            )
        if initial.get("com.magi.omlx"):
            expected_urls.append("http://127.0.0.1:8080/v1/models")
        if initial.get("com.magi.omlx-embed"):
            expected_urls.append("http://127.0.0.1:8081/v1/models")
        readiness = receipt.get("readiness")
        if (
            receipt.get("required_urls") != expected_urls
            or not isinstance(readiness, dict)
            or set(readiness) != set(expected_urls)
            or any(
                not isinstance(row, dict)
                or row.get("ok") is not True
                or row.get("status_code") != 200
                or not SHA256_RE.fullmatch(str(row.get("body_sha256") or ""))
                for row in readiness.values()
            )
        ):
            raise IsolatedLiveBlocked("restored readiness receipt is incomplete")


def _json(path: Path, description: str) -> dict[str, Any]:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise IsolatedLiveBlocked(f"{description} path is unsafe")
    try:
        metadata = raw.lstat()
        value = json.loads(raw.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise IsolatedLiveBlocked(f"{description} is unreadable: {exc}") from exc
    if not stat.S_ISREG(metadata.st_mode) or not isinstance(value, dict):
        raise IsolatedLiveBlocked(f"{description} is not a JSON object file")
    return value


def _binding(value: Any, description: str) -> tuple[Path, str]:
    if not isinstance(value, dict):
        raise IsolatedLiveBlocked(f"{description} binding is missing")
    raw, digest = value.get("path"), value.get("sha256")
    if not isinstance(raw, str) or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
        raise IsolatedLiveBlocked(f"{description} binding is invalid")
    path = Path(raw)
    if _sha256(path) != digest:
        raise IsolatedLiveBlocked(f"{description} SHA-256 mismatch")
    return path, digest


def _consume(path: Path, description: str) -> bytes:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise IsolatedLiveBlocked(f"{description} must be a canonical absolute file")
    descriptor = os.open(raw, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            raise IsolatedLiveBlocked(f"{description} must be owner-only 0600")
        data = os.read(descriptor, 65537)
        if not data or len(data) > 65536:
            raise IsolatedLiveBlocked(f"{description} is empty or too large")
        current = raw.lstat()
        if (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns) != (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        ):
            raise IsolatedLiveBlocked(f"{description} changed while consumed")
        raw.unlink()
        directory = os.open(raw.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return data
    finally:
        os.close(descriptor)


def _write_new(path: Path, value: Mapping[str, Any]) -> str:
    raw = path.expanduser()
    if not raw.is_absolute() or raw.resolve(strict=False) != raw or raw.is_symlink():
        raise IsolatedLiveBlocked("resource-window output path is unsafe")
    raw.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = _canonical(value)
    descriptor = os.open(
        raw,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o400,
    )
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(raw.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return hashlib.sha256(data).hexdigest()


def verify_plan(
    outer_plan_path: Path,
    outer_plan_sha256: str,
    inner_plan_path: Path,
    inner_plan_sha256: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if (
        not SHA256_RE.fullmatch(outer_plan_sha256)
        or not SHA256_RE.fullmatch(inner_plan_sha256)
        or _sha256(outer_plan_path) != outer_plan_sha256
        or _sha256(inner_plan_path) != inner_plan_sha256
    ):
        raise IsolatedLiveBlocked("provisional resource-window plan hash mismatch")
    outer = _json(outer_plan_path, "outer provisional plan")
    inner = _json(inner_plan_path, "inner resource-window plan")
    unsigned = dict(outer)
    supplied = unsigned.pop("plan_sha256", None)
    if (
        outer.get("schema") != SCHEMA
        or outer.get("operation") != "isolated_resource_window_validation"
        or supplied != hashlib.sha256(_canonical(unsigned)).hexdigest()
        or inner.get("schema") != INNER_PLAN_SCHEMA
        or inner.get("plan_sha256")
        != sha256_json(
            {key: value for key, value in inner.items() if key != "plan_sha256"}
        )
    ):
        raise IsolatedLiveBlocked("provisional resource-window plan identity is invalid")
    release_path, release_sha = _binding(outer.get("release_manifest"), "release manifest")
    gate_path, _gate_sha = _binding(outer.get("provisional_gate_report"), "provisional gate")
    gate = _json(gate_path, "provisional resource-window gate")
    release_binding = inner.get("release_binding")
    orchestration = inner.get("orchestration_binding")
    owner_contract = inner.get("outer_owner_contract")
    context = outer.get("context")
    if (
        not isinstance(release_binding, dict)
        or not isinstance(orchestration, dict)
        or not isinstance(owner_contract, dict)
        or not isinstance(context, dict)
        or gate.get("schema") != PROVISIONAL_GATE_SCHEMA
        or gate.get("status") != "provisional_16_of_19_passed"
        or gate.get("formal_live_eligible") is not False
        or gate.get("required_evidence") != sorted(PROVISIONAL_EVIDENCE)
        or gate.get("excluded_resource_evidence") != sorted(RESOURCE_EVIDENCE)
        or gate.get("counts")
        != {"required": 16, "passed": 16, "failed": 0, "missing": 0, "invalid": 0}
        or any(gate.get(field) != context.get(field) for field in (
            "campaign_id", "release_sha", "hardware_id", "gate_config_sha256"
        ))
        or gate.get("release_manifest_sha256") != release_sha
        or release_binding.get("release_manifest_sha256") != release_sha
        or Path(str(release_binding.get("release_root") or "")).resolve(strict=True)
        != release_path.parent.resolve(strict=True)
        or orchestration.get("outer_plan_sha256") != outer_plan_sha256
        or orchestration.get("phase") != "resource_window_after_v2_zero_owner"
        or orchestration.get("v2_restore_owner") != "outer_isolated_live_executor_finally"
        or outer.get("token_sha256") is None
        or owner_contract.get("required_stopped_launchd_labels")
        != list(REQUIRED_STOPPED_LABELS)
        or owner_contract.get("required_absent_process_patterns")
        != list(REQUIRED_MODEL_OWNER_PATTERNS)
        or owner_contract.get("zero_owner_snapshot_required_coverage")
        != ["launchd", "ownership", "pidfile", "port", "process"]
        or owner_contract.get("outer_must_capture_initial_label_state") is not True
        or owner_contract.get("outer_finally_restore_initial_label_state_exactly") is not True
        or owner_contract.get("restore_proof_owner")
        != "outer_isolated_live_executor_finally"
        or owner_contract.get("outer_restore_readiness")
        != {
            "v2": [
                "http://127.0.0.1:5002/health",
                "http://127.0.0.1:5003/health",
                "http://127.0.0.1:5014/health",
                "http://127.0.0.1:8088/health",
            ],
            "model_hosts_if_initially_active": [
                "http://127.0.0.1:8080/v1/models",
                "http://127.0.0.1:8081/v1/models",
            ],
        }
    ):
        raise IsolatedLiveBlocked("provisional 16/19 gate or inner/outer binding is invalid")
    return outer, inner


def _release_resource_window(
    outer: Mapping[str, Any],
    inner: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Return the release-bound resource validation window.

    A provisional run is allowed to use a conditional daytime window only when
    the *same* immutable gate configuration was bound into both its outer gate
    and its sealed plan.  This keeps a white-hours run from becoming an
    unbounded alternative to the legacy 02:00--04:00 maintenance policy.
    """

    release_path, _release_sha = _binding(outer.get("release_manifest"), "release manifest")
    context = outer.get("context")
    binding = inner.get("release_binding")
    if not isinstance(context, dict) or not isinstance(binding, dict):
        raise IsolatedLiveBlocked("resource-window release window bindings are invalid")
    gate_path = release_path.parent / "config" / "v3_cutover_gates.json"
    try:
        gate_path = gate_path.resolve(strict=True)
    except OSError as exc:
        raise IsolatedLiveBlocked("resource-window release cutover gate is unavailable") from exc
    if not gate_path.is_file() or gate_path.is_symlink():
        raise IsolatedLiveBlocked("resource-window release cutover gate is unsafe")
    gate_sha256 = _sha256(gate_path)
    if (
        context.get("gate_config_sha256") != gate_sha256
        or binding.get("gate_config_sha256") != gate_sha256
    ):
        raise IsolatedLiveBlocked("resource-window cutover gate configuration SHA drifted")
    gate = _json(gate_path, "resource-window release cutover gate")
    if gate.get("schema_version") != 1 or gate.get("timezone") != "Asia/Taipei":
        raise IsolatedLiveBlocked("resource-window release cutover gate timezone or schema is invalid")
    conditional = gate.get("conditional_daytime_window")
    try:
        if conditional is not None:
            if not isinstance(conditional, dict):
                raise IsolatedLiveBlocked("resource-window conditional daytime window is invalid")
            return assess_absolute_window(conditional, now=now)
        legacy = gate.get("window")
        if not isinstance(legacy, dict):
            raise IsolatedLiveBlocked("resource-window legacy window is invalid")
        return assess_cutover_window(legacy, timezone_name="Asia/Taipei", now=now)
    except Exception as exc:
        if isinstance(exc, IsolatedLiveBlocked):
            raise
        raise IsolatedLiveBlocked(f"resource-window release window is invalid: {exc}") from exc


def _default_collector(plan: Mapping[str, Any], approval: str, phase_token: str) -> Mapping[str, Any]:
    name = "MAGI_V3_ISOLATED_LIVE_ZERO_OWNER_PHASE_TOKEN"
    previous = os.environ.get(name)
    os.environ[name] = phase_token
    try:
        return collect_resource_window(plan, approval)
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


class ProvisionalResourceWindowExecutor:
    def __init__(
        self,
        *,
        outer_plan_path: Path,
        outer_plan_sha256: str,
        inner_plan_path: Path,
        inner_plan_sha256: str,
        outer_token_file: Path,
        inner_token_file: Path,
        collector_output: Path,
        machine: ResourceWindowMachine,
        collector: Collector = _default_collector,
        clock: Clock | None = None,
    ) -> None:
        self.outer_plan_path = outer_plan_path
        self.outer_plan_sha256 = outer_plan_sha256
        self.inner_plan_path = inner_plan_path
        self.inner_plan_sha256 = inner_plan_sha256
        self.outer_token_file = outer_token_file
        self.inner_token_file = inner_token_file
        self.collector_output = collector_output
        self.machine = machine
        self.collector = collector
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.events: list[dict[str, Any]] = []
        self.blackout = False
        self.blackout_attempted = False
        self.stop_attempted = False
        self.resource_stop_attempted = False
        self.initial_capture: dict[str, Any] | None = None
        self.initial_capture_sha256 = ""
        self._outer: dict[str, Any] | None = None
        self._inner: dict[str, Any] | None = None

    def _event(self, action: str, **detail: Any) -> None:
        self.events.append(
            {
                "sequence": len(self.events) + 1,
                "at": self.clock().astimezone(timezone.utc).isoformat(),
                "action": action,
                **detail,
            }
        )

    def _snapshot(self, expected: str) -> None:
        summary = _snapshot_summary(self.machine.collect_ownership_snapshot(), expected)
        self._event("ownership_snapshot", expected=expected, snapshot=summary)
        if summary["assessment"]["go"] is not True:
            raise IsolatedLiveBlocked(f"resource window did not prove {expected} ownership")

    def _window(self, action: str) -> dict[str, Any]:
        if self._outer is None or self._inner is None:
            raise IsolatedLiveBlocked("resource-window release window is unavailable before plan verification")
        result = _release_resource_window(self._outer, self._inner, now=self.clock())
        self._event("verify_resource_window", stage=action, window=result)
        if result.get("within_window") is not True:
            raise IsolatedLiveBlocked("outside release-bound resource validation window")
        return result

    def _host_receipt(self, operation: str) -> dict[str, Any]:
        if self.initial_capture is None or not self.initial_capture_sha256:
            raise IsolatedLiveBlocked("resource-window initial state was not captured")
        method = {
            "stop_required_labels": self.machine.stop_resource_window_labels,
            "prove_zero_ownership": self.machine.collect_resource_window_zero_receipt,
            "restore_initial_state": self.machine.restore_resource_window_labels,
            "verify_restored_readiness": self.machine.verify_resource_window_readiness,
        }[operation]
        receipt = _bound_host_receipt(
            method(self.initial_capture),
            operation=operation,
            capture_sha256=self.initial_capture_sha256,
        )
        _validate_host_receipt(
            receipt,
            operation=operation,
            capture=self.initial_capture,
        )
        self._event(operation, receipt=receipt)
        return receipt

    def _collector_timeout_seconds(self) -> float:
        from zoneinfo import ZoneInfo

        local = self.clock().astimezone(ZoneInfo("Asia/Taipei"))
        window = self._window("collector_timeout")
        if window.get("kind") == "conditional_daytime":
            try:
                end = datetime.fromisoformat(str(window["ends_at"]))
            except ValueError as exc:
                raise IsolatedLiveBlocked("conditional resource window end is invalid") from exc
        else:
            end = local.replace(
                hour=int(str(window["end"]).split(":")[0]),
                minute=int(str(window["end"]).split(":")[1]),
                second=0,
                microsecond=0,
            )
            if end <= local:
                end += timedelta(days=1)
        remaining = (end - local).total_seconds() - RESTORE_RESERVE_SECONDS
        if remaining < MINIMUM_COLLECTOR_SECONDS:
            raise IsolatedLiveBlocked(
                "insufficient maintenance-window time for 1800-second resource collection and restore"
            )
        return remaining

    def _collect_with_timeout(
        self, inner: Mapping[str, Any], approval: str, phase_token: str
    ) -> Mapping[str, Any]:
        timeout = self._collector_timeout_seconds()
        if threading.current_thread() is not threading.main_thread():
            raise IsolatedLiveBlocked("resource collector timeout requires the main execution thread")
        previous_handler = signal.getsignal(signal.SIGALRM)

        def timed_out(_signum: int, _frame: object) -> None:
            raise ResourceWindowCollectorTimeout(
                f"resource collector exceeded {timeout:.0f}s maintenance deadline"
            )

        signal.signal(signal.SIGALRM, timed_out)
        previous_timer = signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            return self.collector(inner, approval, phase_token)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
            if previous_timer != (0.0, 0.0):
                signal.setitimer(signal.ITIMER_REAL, *previous_timer)

    def execute(self) -> dict[str, Any]:
        started = self.clock().astimezone(timezone.utc)
        error = ""
        collector_sha = ""
        restored = False
        try:
            outer, inner = verify_plan(
                self.outer_plan_path,
                self.outer_plan_sha256,
                self.inner_plan_path,
                self.inner_plan_sha256,
            )
            self._outer, self._inner = outer, inner
            self._event("verify_static_artifacts")
            self._window("before_initial_state_capture")
            capture = _semantic_receipt(
                self.machine.capture_resource_window_host_state(REQUIRED_STOPPED_LABELS),
                operation="capture_initial_state",
            )
            self.initial_capture_sha256 = _capture_sha(capture)
            self.initial_capture = capture
            self._event("capture_initial_state", receipt=capture)
            self._snapshot("v2")
            outer_token = _consume(self.outer_token_file, "outer one-time token").rstrip(b"\r\n")
            if not hmac.compare_digest(
                hashlib.sha256(outer_token).hexdigest(), str(outer.get("token_sha256") or "")
            ):
                raise IsolatedLiveBlocked("outer one-time token mismatch")
            try:
                inner_tokens = json.loads(_consume(self.inner_token_file, "inner one-time tokens"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise IsolatedLiveBlocked("inner one-time tokens are invalid") from exc
            approval = inner_tokens.get("approval_token")
            phase_token = inner_tokens.get("zero_owner_phase_token")
            orchestration = inner.get("orchestration_binding", {})
            if (
                not isinstance(approval, str)
                or not isinstance(phase_token, str)
                or hashlib.sha256(approval.encode()).hexdigest()
                != inner.get("approval_token_sha256")
                or hashlib.sha256(phase_token.encode()).hexdigest()
                != orchestration.get("zero_owner_phase_token_sha256")
                or inner_tokens.get("outer_plan_sha256") != self.outer_plan_sha256
                or inner_tokens.get("release_manifest_sha256")
                != inner.get("release_binding", {}).get("release_manifest_sha256")
            ):
                raise IsolatedLiveBlocked("inner one-time token bindings are invalid")
            self._event("consume_one_time_tokens", consumed_once=True)
            self.blackout_attempted = True
            self._window("before_v2_stop")
            receipt = self.machine.activate_maintenance_blackout()
            _require_receipt_ok(receipt, action="activate_maintenance_blackout")
            self.blackout = True
            self._event("activate_maintenance_blackout", receipt=receipt)
            self.stop_attempted = True
            self._window("immediately_before_v2_stop")
            receipt = self.machine.stop_v2()
            _require_receipt_ok(receipt, action="stop_v2")
            self._event("stop_v2", receipt=receipt)
            self.resource_stop_attempted = True
            self._window("before_resource_labels_stop")
            self._host_receipt("stop_required_labels")
            self._snapshot("zero")
            self._host_receipt("prove_zero_ownership")
            raw_report = dict(self._collect_with_timeout(inner, approval, phase_token))
            if (
                raw_report.get("schema") != "magi.v3.isolated-resource-window/v1"
                or raw_report.get("status") != "passed"
            ):
                raise IsolatedLiveBlocked("resource collector returned the wrong raw schema")
            collector_sha = _write_new(self.collector_output, raw_report)
            self._event(
                "collect_resource_window",
                collector_output_sha256=collector_sha,
                reported_status=raw_report.get("status"),
            )
            self._snapshot("zero")
            self._host_receipt("prove_zero_ownership")
        except BaseException as exc:
            error = str(exc)
        finally:
            mutation_attempted = (
                self.blackout_attempted
                or self.stop_attempted
                or self.resource_stop_attempted
            )
            if mutation_attempted:
                restore_errors: list[str] = []

                def restore_step(name: str, action: Callable[[], None]) -> None:
                    try:
                        action()
                    except BaseException as exc:
                        restore_errors.append(f"{name}: {type(exc).__name__}: {exc}")

                def restore_v2() -> None:
                    receipt = self.machine.restore_v2()
                    _require_receipt_ok(receipt, action="restore_v2")
                    self._event("restore_v2", receipt=receipt)

                def verify_v2() -> None:
                    ready = self.machine.verify_v2_readiness_integrity()
                    _require_receipt_ok(ready, action="verify_v2_readiness_integrity")
                    self._event("verify_v2_readiness_integrity", receipt=ready)

                # Each step is independent: one broken plist/readiness probe
                # must not prevent all remaining labels from being restored.
                if self.initial_capture is not None:
                    restore_step(
                        "restore_initial_state",
                        lambda: self._host_receipt("restore_initial_state"),
                    )
                # The exact-label adapter restores model hosts first and the
                # daemon last.  Only then restore the remaining V2 agents so
                # the daemon cannot resume work against absent model hosts.
                restore_step("restore_v2", restore_v2)
                restore_step("snapshot_v2", lambda: self._snapshot("v2"))
                restore_step("verify_v2_readiness_integrity", verify_v2)
                if self.initial_capture is not None:
                    restore_step(
                        "verify_restored_readiness",
                        lambda: self._host_receipt("verify_restored_readiness"),
                    )
                restored = not restore_errors
                if restore_errors:
                    error = (
                        f"{error}; exact host restore failed: "
                        + " | ".join(restore_errors)
                    ).strip("; ")
            elif self.initial_capture is not None:
                try:
                    # Token/static failures happen before mutation.  Prove the
                    # captured state remained exact without bootstrapping an
                    # originally inactive label.
                    self._host_receipt("verify_restored_readiness")
                    restored = True
                except BaseException as exc:
                    error = f"{error}; initial state preservation failed: {exc}".strip("; ")
            if self.blackout_attempted:
                try:
                    receipt = self.machine.deactivate_maintenance_blackout()
                    _require_receipt_ok(receipt, action="deactivate_maintenance_blackout")
                    self._event("deactivate_maintenance_blackout", receipt=receipt)
                except BaseException as exc:
                    error = f"{error}; blackout cleanup failed: {exc}".strip("; ")
        finished = self.clock().astimezone(timezone.utc)
        ok = not error and restored and bool(collector_sha)
        return {
            "schema": REPORT_SCHEMA,
            "schema_version": 1,
            "report_id": uuid.uuid4().hex,
            "status": "window_completed_v2_restored" if ok else "blocked",
            "ok": ok,
            "formal_live_eligible": False,
            "mutation_performed": (
                self.blackout_attempted
                or self.stop_attempted
                or self.resource_stop_attempted
            ),
            "v3_production_started": False,
            "v2_restored": restored,
            "outer_plan_sha256": self.outer_plan_sha256,
            "inner_plan_sha256": self.inner_plan_sha256,
            "initial_host_capture_sha256": self.initial_capture_sha256,
            "initial_host_state_restored": restored,
            "collector_output": str(self.collector_output) if collector_sha else "",
            "collector_output_sha256": collector_sha,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "error": error,
            "events": self.events,
        }


__all__ = [
    "PROVISIONAL_EVIDENCE",
    "RESOURCE_EVIDENCE",
    "ProvisionalResourceWindowExecutor",
    "verify_plan",
]

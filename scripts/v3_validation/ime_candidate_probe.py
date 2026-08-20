"""Bounded native McBopomofo candidate-window pressure probe for macOS.

The probe creates only unsaved TextEdit documents, never types into an existing
document, and restores the previously frontmost application and input source.
It is deliberately separate from the minute watchdog: users are allowed to
choose U.S. input intentionally, while a validation campaign needs positive
evidence that a real candidate window can render under bounded memory load.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mmap
import os
import platform
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from scripts.ops.input_method_watchdog import (
    TARGET_INPUT_SOURCE_ID,
    _current_input_source_id,
    _select_input_source,
    _text_services_healthy,
)


EVIDENCE_PREFIX = "MAGI_V3_OFFLINE_EVIDENCE="
WORKLOAD = "ime_candidate_window_pressure_probe"
PROBE = "native_mcbopomofo_candidate_window_pressure"
_PAGE_SIZE = 16 * 1024
_TEXTEDIT_READINESS_TIMEOUT_SEC = 3.0
_TEXTEDIT_READINESS_POLL_SEC = 0.05
_NATIVE_STATE_TIMEOUT_SEC = 2.0
_NATIVE_STATE_POLL_SEC = 0.05

_TEXTEDIT_READINESS_SCRIPT = r'''
tell application "System Events"
  if not (exists process "TextEdit") then return "process-not-running"
  tell process "TextEdit"
    if frontmost is false then return "not-frontmost"
    if (count of windows) is 0 then return "no-window"
  end tell
end tell
tell application "TextEdit"
  if (count of documents) is 0 then return "no-document"
end tell
return "ready"
'''.strip()

_TYPE_PROBE_SCRIPT = r'''
tell application "TextEdit"
  if (count of documents) is 0 then error "TextEdit front document is not ready"
end tell
tell application "System Events"
  if not (exists process "TextEdit") then error "TextEdit front document is not ready"
  tell process "TextEdit"
    if frontmost is false then error "TextEdit front document is not ready"
    if (count of windows) is 0 then error "TextEdit front document is not ready"
    keystroke "su3"
    key code 49
  end tell
end tell
'''.strip()


class ImeProbeError(RuntimeError):
    """Raised when native UI evidence cannot be collected safely."""


def _run_osascript(*lines: str, timeout: float = 10.0) -> str:
    argv = ["/usr/bin/osascript"]
    for line in lines:
        argv.extend(("-e", line))
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode != 0:
        raise ImeProbeError((result.stderr or result.stdout or "AppleScript failed").strip())
    return (result.stdout or "").strip()


def _frontmost_application() -> str:
    return _run_osascript(
        'tell application "System Events" to get name of first process whose frontmost is true'
    )


def _activate_process(process_name: str) -> None:
    script = """
on run argv
  tell application "System Events"
    if exists process (item 1 of argv) then set frontmost of process (item 1 of argv) to true
  end tell
end run
""".strip()
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", script, "--", str(process_name)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise ImeProbeError((result.stderr or result.stdout or "frontmost restore failed").strip())


def _wait_for_frontmost_application(
    expected: str,
    *,
    timeout_sec: float = _NATIVE_STATE_TIMEOUT_SEC,
    frontmost_reader: Callable[[], str] = _frontmost_application,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Observe the asynchronous AppKit activation postcondition."""
    deadline = monotonic() + max(0.2, min(float(timeout_sec), 10.0))
    while True:
        try:
            if str(frontmost_reader() or "") == expected:
                return True
        except (ImeProbeError, OSError, subprocess.SubprocessError):
            pass
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleeper(min(_NATIVE_STATE_POLL_SEC, remaining))


def _wait_for_input_source_id(
    expected: str,
    *,
    timeout_sec: float = _NATIVE_STATE_TIMEOUT_SEC,
    source_reader: Callable[[], str | None] = _current_input_source_id,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Observe the asynchronous TIS selection postcondition."""
    deadline = monotonic() + max(0.2, min(float(timeout_sec), 10.0))
    while True:
        try:
            if str(source_reader() or "") == expected:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleeper(min(_NATIVE_STATE_POLL_SEC, remaining))


def _textedit_readiness_state() -> str:
    return _run_osascript(_TEXTEDIT_READINESS_SCRIPT)


def _wait_for_textedit_ready(
    *,
    timeout_sec: float = _TEXTEDIT_READINESS_TIMEOUT_SEC,
    readiness_reader: Callable[[], str] = _textedit_readiness_state,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> float:
    """Wait until TextEdit owns a front document and window without fixed sleeps."""
    if not 0.2 <= float(timeout_sec) <= 10.0:
        raise ImeProbeError("TextEdit readiness timeout must be between 0.2 and 10 seconds")
    started = monotonic()
    deadline = started + float(timeout_sec)
    last_state = "not-queried"
    while True:
        try:
            last_state = str(readiness_reader() or "empty-readiness-state").strip()
        except (ImeProbeError, OSError, subprocess.SubprocessError):
            # Apple Events can be temporarily unavailable while a new window is attached.
            last_state = "readiness-query-unavailable"
        now = monotonic()
        if last_state == "ready":
            return round((now - started) * 1000.0, 3)
        if now >= deadline:
            raise ImeProbeError(f"TextEdit front document was not ready ({last_state})")
        sleeper(min(_TEXTEDIT_READINESS_POLL_SEC, deadline - now))


def _activate_and_wait_for_textedit_ready() -> float:
    _run_osascript('tell application "TextEdit" to activate')
    return _wait_for_textedit_ready()


def _open_isolated_document() -> float:
    before = _textedit_document_count() if _textedit_running() else 0
    _run_osascript(
        'tell application "TextEdit" to make new document',
    )
    readiness_ms = _activate_and_wait_for_textedit_ready()
    after = _textedit_document_count()
    if after <= before:
        raise ImeProbeError("TextEdit did not create an isolated document")
    # A cold TextEdit launch can create its own blank document in addition to
    # the document requested above. Both are newer than ``before`` and are
    # removed by _restore_probe_document_count without touching any document
    # that existed before the probe.
    return readiness_ms


def _prepare_isolated_document_for_probe() -> float:
    """Select McBopomofo before TextEdit creates its NSTextInputContext.

    Selecting the source after a document already exists can leave the first
    editor context attached to the previous keyboard layout.  The global TIS
    selection then looks correct while the first probe keystrokes are handled
    by the stale context.  A newly created isolated document must inherit the
    already-confirmed source instead.
    """
    if not _select_input_source(TARGET_INPUT_SOURCE_ID):
        raise ImeProbeError("TISSelectInputSource rejected McBopomofo")
    if _current_input_source_id() != TARGET_INPUT_SOURCE_ID:
        raise ImeProbeError("McBopomofo selection did not become active")
    documents_before = _textedit_document_count() if _textedit_running() else 0
    open_readiness_ms = _open_isolated_document()
    focus_readiness_ms = _activate_and_wait_for_textedit_ready()

    # macOS may restore an app-specific keyboard layout when TextEdit becomes
    # frontmost, after the global preselection above.  If that happens, merely
    # selecting McBopomofo again is insufficient because the first
    # NSTextInputContext can remain attached to the restored layout.  Close
    # only our unsaved warm-up document, confirm the source while TextEdit is
    # active, then create a fresh editor context.
    if _current_input_source_id() != TARGET_INPUT_SOURCE_ID:
        if not _select_input_source(TARGET_INPUT_SOURCE_ID):
            raise ImeProbeError("TextEdit activation overrode McBopomofo selection")
        if not _wait_for_input_source_id(TARGET_INPUT_SOURCE_ID):
            raise ImeProbeError("McBopomofo did not become active inside TextEdit")
        if not _restore_probe_document_count(documents_before):
            raise ImeProbeError("stale TextEdit input context could not be closed")
        open_readiness_ms = max(open_readiness_ms, _open_isolated_document())
        focus_readiness_ms = max(
            focus_readiness_ms,
            _activate_and_wait_for_textedit_ready(),
        )
    if _current_input_source_id() != TARGET_INPUT_SOURCE_ID:
        raise ImeProbeError("TextEdit editor context is not bound to McBopomofo")
    return max(open_readiness_ms, focus_readiness_ms)


def _textedit_running() -> bool:
    result = subprocess.run(
        ["/usr/bin/pgrep", "-x", "TextEdit"], capture_output=True, timeout=5, check=False
    )
    return result.returncode == 0


def _quit_textedit() -> None:
    try:
        _run_osascript('tell application "TextEdit" to quit')
    except ImeProbeError:
        pass


def _wait_for_textedit_stopped(
    *,
    timeout_sec: float = 3.0,
    running_reader: Callable[[], bool] = _textedit_running,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    deadline = monotonic() + max(0.2, min(float(timeout_sec), 10.0))
    while running_reader():
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleeper(min(0.05, remaining))
    return True


def _close_isolated_document() -> bool:
    try:
        _run_osascript(
            'tell application "System Events" to tell process "TextEdit" to key code 53',
            'delay 0.1',
            'tell application "System Events" to tell process "TextEdit" to key code 53',
        )
        # Let the input method finish dismissing its composition/candidate UI
        # before asking TextEdit to close the unsaved document.
        time.sleep(0.1)
        _run_osascript('tell application "TextEdit" to close front document saving no')
        return True
    except (ImeProbeError, subprocess.SubprocessError):
        # Cleanup is retried by the caller's finalizer; never send global keys.
        return False


def _close_probe_document_if_present(documents_before: int) -> bool:
    """Close only a document created beyond the observed pre-probe count."""
    try:
        if not _textedit_running() or _textedit_document_count() <= documents_before:
            return True
    except (ImeProbeError, OSError, subprocess.SubprocessError):
        return False
    return _close_isolated_document()


def _restore_probe_document_count(
    documents_before: int,
    *,
    max_closes: int = 8,
) -> bool:
    """Remove all probe-created TextEdit documents without crossing baseline.

    TextEdit may create one automatic blank document during a cold launch and
    another document for the explicit AppleScript request. Re-read the
    observable count before every close and never close when it is at or below
    the pre-probe baseline.
    """
    if documents_before < 0 or max_closes < 1:
        return False
    for _attempt in range(max_closes):
        try:
            if not _textedit_running():
                return documents_before == 0
            current = _textedit_document_count()
        except (ImeProbeError, OSError, subprocess.SubprocessError):
            return False
        if current == documents_before:
            return True
        if current < documents_before:
            return False
        if not _close_isolated_document():
            return False
    try:
        return _textedit_running() and _textedit_document_count() == documents_before
    except (ImeProbeError, OSError, subprocess.SubprocessError):
        return False


def _textedit_document_count() -> int:
    raw = _run_osascript(
        'tell application "System Events" to if not (exists process "TextEdit") then return "0"',
        'tell application "TextEdit" to return (count of documents) as text',
    )
    try:
        count = int(raw)
    except ValueError as exc:
        raise ImeProbeError("TextEdit document count was not numeric") from exc
    if count < 0:
        raise ImeProbeError("TextEdit document count was negative")
    return count


def _wait_for_textedit_document_count(
    expected: int,
    *,
    timeout_sec: float = 2.0,
    count_reader: Callable[[], int] = _textedit_document_count,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    deadline = monotonic() + max(0.2, min(float(timeout_sec), 10.0))
    while count_reader() != expected:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleeper(min(0.05, remaining))
    return True


def _type_probe_sequence() -> None:
    # The focus guard and keystrokes run in one AppleScript transaction.  A late
    # focus handoff therefore fails closed instead of typing into another UI.
    _run_osascript(_TYPE_PROBE_SCRIPT)


def _candidate_windows(ime_pids: set[int]) -> list[dict[str, Any]]:
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGNullWindowID,
            kCGWindowListOptionOnScreenOnly,
        )
    except ImportError as exc:
        raise ImeProbeError("Quartz is required for native candidate-window evidence") from exc

    found = []
    for window in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID):
        owner = str(window.get("kCGWindowOwnerName") or "")
        owner_pid = int(window.get("kCGWindowOwnerPID") or 0)
        window_id = int(window.get("kCGWindowNumber") or 0)
        layer = int(window.get("kCGWindowLayer") or 0)
        raw_bounds = window.get("kCGWindowBounds")
        bounds = raw_bounds if isinstance(raw_bounds, Mapping) else {}
        width = int(bounds.get("Width") or 0)
        height = int(bounds.get("Height") or 0)
        if window_id <= 0 or layer <= 0 or width <= 0 or height <= 0:
            continue
        if owner_pid not in ime_pids and owner not in {"小麥注音", "McBopomofo"}:
            continue
        found.append(
            {
                "owner": "mcbopomofo",
                "window_id": window_id,
                "layer": layer,
                "width": width,
                "height": height,
            }
        )
    return found


def _wait_for_candidate_windows_gone(
    ime_pids: set[int],
    *,
    timeout_sec: float = _NATIVE_STATE_TIMEOUT_SEC,
    window_reader: Callable[[set[int]], list[dict[str, Any]]] = _candidate_windows,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait for the prior composition UI to disappear before a new cycle.

    McBopomofo may reuse one CGWindow id between compositions.  Starting the
    next cycle while the previous candidate window is still on-screen makes a
    healthy reused window look "preexisting" and therefore undetectable.  The
    probe must establish an empty baseline instead of relying on window ids to
    change between cycles.
    """
    deadline = monotonic() + max(0.2, min(float(timeout_sec), 10.0))
    while True:
        try:
            if not window_reader(ime_pids):
                return True
        except (ImeProbeError, OSError, subprocess.SubprocessError):
            pass
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleeper(min(_NATIVE_STATE_POLL_SEC, remaining))


def _ime_pids() -> set[int]:
    """Find the installed IME independent of campaign HOME isolation."""
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5, check=False
    )
    suffix = "/McBopomofo.app/Contents/MacOS/McBopomofo"
    found: set[int] = set()
    for raw in (result.stdout or "").splitlines():
        parts = raw.strip().split(None, 1)
        if len(parts) != 2 or not parts[1].endswith(suffix):
            continue
        try:
            found.add(int(parts[0]))
        except ValueError:
            continue
    return found


def _memory_free_percent() -> float | None:
    result = subprocess.run(
        ["/usr/bin/memory_pressure"], capture_output=True, text=True, timeout=10, check=False
    )
    for line in (result.stdout or "").splitlines():
        if "System-wide memory free percentage:" not in line:
            continue
        try:
            return float(line.rsplit(":", 1)[1].strip().rstrip("%"))
        except ValueError:
            return None
    return None


def _allocate_pressure(megabytes: int) -> mmap.mmap | None:
    if megabytes <= 0:
        return None
    size = int(megabytes) * 1024 * 1024
    region = mmap.mmap(-1, size)
    for offset in range(0, size, _PAGE_SIZE):
        region[offset] = 1
    return region


def _percentile95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def build_evidence(
    observations: list[dict[str, Any]],
    *,
    pressure_mb: int,
    memory_free_before: float | None,
    memory_free_during: float | None,
    services_healthy: bool,
    pressure_touched_bytes: int | None = None,
    cleanup_verified: bool = True,
    input_source_restored: bool = True,
    frontmost_application_restored: bool = True,
    textedit_state_restored: bool = True,
) -> dict[str, Any]:
    detected = [item for item in observations if item.get("detected") is True]
    latencies = [float(item["latency_ms"]) for item in detected]
    readiness_latencies = [
        float(item["readiness_ms"])
        for item in observations
        if isinstance(item.get("readiness_ms"), (int, float))
        and not isinstance(item.get("readiness_ms"), bool)
    ]
    failures = len(observations) - len(detected)
    healthy = bool(
        observations
        and failures == 0
        and services_healthy
        and pressure_mb > 0
        and cleanup_verified
        and input_source_restored
        and frontmost_application_restored
        and textedit_state_restored
    )
    evidence: dict[str, Any] = {
        "schema_version": 1,
        "workload": WORKLOAD,
        "probe": PROBE,
        "status": "passed" if healthy else "failed",
        "observations": observations,
        "measurements": {
            "cycles_requested": len(observations),
            "cycles_completed": len(observations),
            "candidate_windows_detected": len(detected),
            "candidate_window_failures": failures,
            "candidate_latency_p95_ms": round(_percentile95(latencies), 3),
            "candidate_latency_max_ms": round(max(latencies, default=0.0), 3),
            "document_readiness_p95_ms": round(_percentile95(readiness_latencies), 3),
            "document_readiness_max_ms": round(max(readiness_latencies, default=0.0), 3),
            "pressure_allocated_mb": int(pressure_mb),
            "pressure_touched_bytes": int(
                pressure_touched_bytes
                if pressure_touched_bytes is not None
                else max(0, int(pressure_mb)) * 1024 * 1024
            ),
            "memory_free_percent_before": memory_free_before,
            "memory_free_percent_during": memory_free_during,
            "text_services_healthy": bool(services_healthy),
            "input_source_id_sha256": hashlib.sha256(TARGET_INPUT_SOURCE_ID.encode()).hexdigest(),
        },
        "network_access_performed": False,
        "service_start_performed": False,
        "production_port_access_performed": False,
        "launchctl_performed": False,
        "external_write_performed": False,
        "live_magi_state_access_performed": False,
        "temporary_native_ui_performed": True,
        "unsaved_document_cleanup_performed": bool(cleanup_verified),
        "unsaved_documents_remaining": 0 if cleanup_verified else 1,
        "input_source_restored": bool(input_source_restored),
        "frontmost_application_restored": bool(frontmost_application_restored),
        "textedit_state_restored": bool(textedit_state_restored),
    }
    return evidence


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def run_probe(
    *,
    cycles: int = 3,
    pressure_mb: int = 256,
    timeout_sec: float = 2.0,
    window_reader: Callable[[set[int]], list[dict[str, Any]]] = _candidate_windows,
) -> dict[str, Any]:
    if platform.system() != "Darwin":
        raise ImeProbeError("native IME probe requires macOS")
    if not 1 <= int(cycles) <= 20:
        raise ImeProbeError("cycles must be between 1 and 20")
    if not 0 <= int(pressure_mb) <= 1024:
        raise ImeProbeError("pressure_mb must be between 0 and 1024")
    if not 0.2 <= float(timeout_sec) <= 10.0:
        raise ImeProbeError("timeout_sec must be between 0.2 and 10")

    previous_app = _frontmost_application()
    previous_source = _current_input_source_id()
    if not previous_app or not previous_source:
        raise ImeProbeError("frontmost application and input source must be observable")
    textedit_was_running = _textedit_running()
    documents_before = _textedit_document_count() if textedit_was_running else 0
    services_healthy = _text_services_healthy()
    ime_pids = _ime_pids()
    if not services_healthy or not ime_pids:
        raise ImeProbeError("McBopomofo or candidate-window services are unavailable")

    memory_free_before = _memory_free_percent()
    pressure = _allocate_pressure(int(pressure_mb))
    memory_free_during = _memory_free_percent()
    observations: list[dict[str, Any]] = []
    cleanup_results: list[bool] = []
    input_source_restored = False
    frontmost_application_restored = False
    textedit_state_restored = False
    probe_error: BaseException | None = None
    try:
        for cycle in range(1, int(cycles) + 1):
            try:
                if not _wait_for_candidate_windows_gone(
                    ime_pids,
                    timeout_sec=float(timeout_sec),
                    window_reader=window_reader,
                ):
                    raise ImeProbeError(
                        "previous McBopomofo candidate window did not disappear"
                    )
                readiness_ms = _prepare_isolated_document_for_probe()
                before_windows = window_reader(ime_pids)
                before_ids = {
                    int(item.get("window_id") or 0)
                    for item in before_windows
                    if int(item.get("window_id") or 0) > 0
                }
                started = time.monotonic()
                _type_probe_sequence()
                windows: list[dict[str, Any]] = []
                new_windows: list[dict[str, Any]] = []
                deadline = started + float(timeout_sec)
                while time.monotonic() < deadline:
                    windows = window_reader(ime_pids)
                    new_windows = [
                        item
                        for item in windows
                        if int(item.get("window_id") or 0) > 0
                        and int(item["window_id"]) not in before_ids
                    ]
                    if new_windows:
                        break
                    time.sleep(0.05)
                observations.append(
                    {
                        "cycle": cycle,
                        "detected": bool(new_windows),
                        "latency_ms": round((time.monotonic() - started) * 1000.0, 3),
                        "window_count": len(windows),
                        "preexisting_window_count": len(before_windows),
                        "preexisting_window_ids": sorted(before_ids),
                        "observed_candidate_windows": windows,
                        "new_candidate_windows": new_windows,
                        "readiness_ms": readiness_ms,
                    }
                )
            finally:
                # A cold TextEdit launch may contribute an automatic blank
                # document in addition to the explicit probe document. Remove
                # every document above the observed baseline, never a baseline
                # document owned by the user.
                document_closed = _restore_probe_document_count(documents_before)
                candidate_window_closed = _wait_for_candidate_windows_gone(
                    ime_pids,
                    timeout_sec=float(timeout_sec),
                    window_reader=window_reader,
                )
                cleanup_results.append(document_closed and candidate_window_closed)
    except BaseException as exc:
        probe_error = exc
    finally:
        if pressure is not None:
            pressure.close()
        try:
            _restore_probe_document_count(documents_before)
        finally:
            if previous_app:
                _activate_process(previous_app)
                frontmost_application_restored = _wait_for_frontmost_application(
                    previous_app
                )
            if previous_source:
                _select_input_source(previous_source)
                input_source_restored = _wait_for_input_source_id(previous_source)
            if not textedit_was_running:
                _quit_textedit()
                textedit_state_restored = _wait_for_textedit_stopped()
            else:
                textedit_state_restored = _wait_for_textedit_document_count(
                    documents_before
                )
    if probe_error is not None:
        raise probe_error
    return build_evidence(
        observations,
        pressure_mb=int(pressure_mb),
        memory_free_before=memory_free_before,
        memory_free_during=memory_free_during,
        services_healthy=services_healthy,
        pressure_touched_bytes=int(pressure_mb) * 1024 * 1024,
        cleanup_verified=bool(cleanup_results) and all(cleanup_results),
        input_source_restored=input_source_restored,
        frontmost_application_restored=frontmost_application_restored,
        textedit_state_restored=textedit_state_restored,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure a real McBopomofo candidate window under bounded pressure")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--pressure-mb", type=int, default=256)
    parser.add_argument("--timeout-sec", type=float, default=2.0)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args(argv)
    try:
        evidence = run_probe(cycles=args.cycles, pressure_mb=args.pressure_mb, timeout_sec=args.timeout_sec)
    except (ImeProbeError, OSError, subprocess.SubprocessError) as exc:
        evidence = {
            "schema_version": 1,
            "workload": WORKLOAD,
            "probe": PROBE,
            "status": "failed",
            "error_category": type(exc).__name__,
            "network_access_performed": False,
            "service_start_performed": False,
            "production_port_access_performed": False,
            "launchctl_performed": False,
            "external_write_performed": False,
            "live_magi_state_access_performed": False,
            "temporary_native_ui_performed": True,
        }
    if args.evidence:
        _write_evidence(args.evidence.expanduser().resolve(), evidence)
    print(EVIDENCE_PREFIX + json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if evidence.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

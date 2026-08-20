#!/usr/bin/env python3
"""Lightweight McBopomofo runaway-process guard for macOS."""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path


APP_PATH = Path.home() / "Library" / "Input Methods" / "McBopomofo.app"
EXECUTABLE = str(APP_PATH / "Contents" / "MacOS" / "McBopomofo")
STATE_PATH = Path.home() / "Library" / "Application Support" / "MAGI" / "state" / "input_method_watchdog.json"
# TIS reports the bundle identifier and input-mode identifier joined together.
# McBopomofo's input-mode identifier already contains the bundle prefix, hence
# the repeated ``McBopomofo`` segment here.  HIToolbox defaults show the shorter
# identifier, but TISSelectInputSource requires this exact runtime value.
TARGET_INPUT_SOURCE_ID = "org.openvanilla.inputmethod.McBopomofo.McBopomofo.PlainBopomofo"
_CARBON_PATH = "/System/Library/Frameworks/Carbon.framework/Carbon"
_CORE_FOUNDATION_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_CF_STRING_ENCODING_UTF8 = 0x08000100


def _load_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _processes() -> list[dict]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,rss=,%cpu=,command="],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    found = []
    for raw in (result.stdout or "").splitlines():
        parts = raw.strip().split(None, 3)
        if len(parts) != 4 or parts[3] != EXECUTABLE:
            continue
        try:
            found.append({"pid": int(parts[0]), "rss_kb": int(parts[1]), "cpu": float(parts[2])})
        except ValueError:
            continue
    return found


def _text_services_healthy() -> bool:
    """Candidate windows require both InputMethodKit agents, not just the IME process."""
    result = subprocess.run(
        ["ps", "-axo", "command="],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    commands = result.stdout or ""
    return "TextInputMenuAgent.app/Contents/MacOS/TextInputMenuAgent" in commands and "/imklaunchagent" in commands


def _tis_libraries():
    """Load the small Carbon TIS surface without adding a PyObjC dependency."""
    carbon = ctypes.CDLL(_CARBON_PATH)
    core = ctypes.CDLL(_CORE_FOUNDATION_PATH)

    carbon.TISCopyCurrentKeyboardInputSource.argtypes = []
    carbon.TISCopyCurrentKeyboardInputSource.restype = ctypes.c_void_p
    carbon.TISCreateInputSourceList.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    carbon.TISCreateInputSourceList.restype = ctypes.c_void_p
    carbon.TISGetInputSourceProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    carbon.TISGetInputSourceProperty.restype = ctypes.c_void_p
    carbon.TISSelectInputSource.argtypes = [ctypes.c_void_p]
    carbon.TISSelectInputSource.restype = ctypes.c_int32

    core.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    core.CFArrayGetCount.restype = ctypes.c_long
    core.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    core.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    core.CFStringGetLength.argtypes = [ctypes.c_void_p]
    core.CFStringGetLength.restype = ctypes.c_long
    core.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint32]
    core.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
    core.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    core.CFStringGetCString.restype = ctypes.c_bool
    core.CFRelease.argtypes = [ctypes.c_void_p]
    core.CFRelease.restype = None
    property_id = ctypes.c_void_p.in_dll(carbon, "kTISPropertyInputSourceID").value
    return carbon, core, property_id


def _cf_string(core, value: int | None) -> str:
    if not value:
        return ""
    length = core.CFStringGetLength(value)
    size = core.CFStringGetMaximumSizeForEncoding(length, _CF_STRING_ENCODING_UTF8) + 1
    if size <= 1:
        return ""
    buffer = ctypes.create_string_buffer(size)
    if not core.CFStringGetCString(value, buffer, size, _CF_STRING_ENCODING_UTF8):
        return ""
    return buffer.value.decode("utf-8", errors="replace")


def _current_input_source_id() -> str:
    """Return the actual TIS source, not the often-stale HIToolbox defaults value."""
    source = None
    try:
        carbon, core, property_id = _tis_libraries()
        source = carbon.TISCopyCurrentKeyboardInputSource()
        return _cf_string(core, carbon.TISGetInputSourceProperty(source, property_id))
    except (OSError, AttributeError, ValueError):
        return ""
    finally:
        if source:
            try:
                core.CFRelease(source)
            except (NameError, OSError, AttributeError):
                pass


def _select_input_source(source_id: str = TARGET_INPUT_SOURCE_ID) -> bool:
    """Select one enabled/installed input source through the supported TIS API."""
    source_list = None
    try:
        carbon, core, property_id = _tis_libraries()
        source_list = carbon.TISCreateInputSourceList(None, True)
        if not source_list:
            return False
        for index in range(core.CFArrayGetCount(source_list)):
            source = core.CFArrayGetValueAtIndex(source_list, index)
            current_id = _cf_string(core, carbon.TISGetInputSourceProperty(source, property_id))
            if current_id == source_id:
                return carbon.TISSelectInputSource(source) == 0
        return False
    except (OSError, AttributeError, ValueError):
        return False
    finally:
        if source_list:
            try:
                core.CFRelease(source_list)
            except (NameError, OSError, AttributeError):
                pass


def _restart_input_stack(*, reset_text_services: bool) -> None:
    if reset_text_services:
        for process_name in ("TextInputMenuAgent", "imklaunchagent"):
            subprocess.run(["killall", process_name], timeout=5, check=False)
    subprocess.run(["open", "-gj", str(APP_PATH)], timeout=10, check=False)


def _wait_for_exit(pid: int, timeout: float = 4.0) -> None:
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)


def check_once(
    *,
    state_path: Path = STATE_PATH,
    rss_limit_mb: int = 512,
    cpu_limit: float = 85.0,
    strikes_required: int = 3,
    processes: list[dict] | None = None,
    text_services_ok: bool | None = None,
    input_source_id: str | None = None,
    restart: bool = True,
) -> dict:
    previous = _load_state(state_path)
    records = _processes() if processes is None else processes
    now = datetime.now().isoformat(timespec="seconds")
    restart_count = int(previous.get("restart_count") or 0)

    services_ok = (
        _text_services_healthy()
        if text_services_ok is None and processes is None
        else True if text_services_ok is None else bool(text_services_ok)
    )
    active_source_id = (
        _current_input_source_id()
        if processes is None and input_source_id is None
        else str(input_source_id or "")
    )
    bopomofo_selected = active_source_id.startswith("org.openvanilla.inputmethod.McBopomofo.")

    if not records:
        strikes = int(previous.get("strikes") or 0) + 1
        payload = {
            "ok": True,
            "status": "watching",
            "checked_at": now,
            "reason": "input_method_process_missing",
            "strikes": strikes,
            "restart_count": restart_count,
        }
        if active_source_id:
            payload.update(
                input_source_id=active_source_id,
                bopomofo_selected=bopomofo_selected,
                candidate_window_expected=bopomofo_selected and services_ok,
            )
        if strikes >= max(2, int(strikes_required)) and restart:
            try:
                _restart_input_stack(reset_text_services=not services_ok)
                payload.update(
                    status="restarted",
                    strikes=0,
                    restart_count=restart_count + 1,
                    restarted_at=now,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                payload.update(ok=False, status="restart_failed", error=f"{type(exc).__name__}: {exc}")
        _write_state(state_path, payload)
        return payload

    process = max(records, key=lambda item: (int(item.get("rss_kb") or 0), float(item.get("cpu") or 0)))
    rss_mb = round(int(process.get("rss_kb") or 0) / 1024, 1)
    cpu = round(float(process.get("cpu") or 0), 1)
    reasons = []
    if rss_mb >= max(64, int(rss_limit_mb)):
        reasons.append(f"rss_{rss_mb}mb")
    if cpu >= max(10.0, float(cpu_limit)):
        reasons.append(f"cpu_{cpu}pct")
    if not services_ok:
        reasons.append("candidate_window_services_missing")

    strikes = int(previous.get("strikes") or 0) + 1 if reasons else 0
    payload = {
        "ok": True,
        "status": "watching" if reasons else "healthy",
        "checked_at": now,
        "pid": int(process.get("pid") or 0),
        "rss_mb": rss_mb,
        "cpu_pct": cpu,
        "reason": ",".join(reasons),
        "strikes": strikes,
        "restart_count": restart_count,
    }
    if active_source_id:
        payload.update(
            input_source_id=active_source_id,
            bopomofo_selected=bopomofo_selected,
            candidate_window_expected=bopomofo_selected and services_ok,
        )

    if reasons and strikes >= max(2, int(strikes_required)) and restart:
        pid = int(process.get("pid") or 0)
        if pid > 1:
            try:
                os.kill(pid, signal.SIGTERM)
                _wait_for_exit(pid)
                _restart_input_stack(reset_text_services=not services_ok)
                payload.update(
                    status="restarted",
                    strikes=0,
                    restart_count=restart_count + 1,
                    restarted_at=now,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                payload.update(ok=False, status="restart_failed", error=f"{type(exc).__name__}: {exc}")

    _write_state(state_path, payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Guard McBopomofo against sustained CPU/RSS runaway")
    parser.add_argument("--rss-limit-mb", type=int, default=512)
    parser.add_argument("--cpu-limit", type=float, default=85.0)
    parser.add_argument("--strikes", type=int, default=3)
    parser.add_argument("--no-restart", action="store_true")
    parser.add_argument(
        "--select-bopomofo-once",
        action="store_true",
        help="select McBopomofo once before checking; never used by the periodic watchdog",
    )
    args = parser.parse_args(argv)
    if args.select_bopomofo_once:
        if not _select_input_source():
            print(json.dumps({"ok": False, "status": "selection_failed"}, ensure_ascii=False))
            return 1
        time.sleep(0.5)
    result = check_once(
        rss_limit_mb=args.rss_limit_mb,
        cpu_limit=args.cpu_limit,
        strikes_required=args.strikes,
        restart=not args.no_restart,
    )
    if args.select_bopomofo_once:
        result["selection_requested"] = TARGET_INPUT_SOURCE_ID
        if result.get("input_source_id") != TARGET_INPUT_SOURCE_ID:
            result.update(ok=False, status="selection_not_active")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

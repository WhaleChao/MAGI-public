"""Pytest plugin for release-bound, offline route success-path tracing.

The plugin observes Flask's real request dispatch.  It deliberately records
only responses below HTTP 400 and rejects authentication redirects, so a
validation guard can never be promoted to representative-success evidence.
Observations are published only for tests whose setup, call, and teardown all
pass.  The parent certifier separately binds this trace to an immutable
release and enforces filesystem/network isolation.
"""

from __future__ import annotations

import json
import inspect
import linecache
import os
import re
import sys
import traceback
from collections import Counter
from collections import defaultdict
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

import pytest


_LOCK = Lock()
_CURRENT_NODEID = ""
_OBSERVATIONS: dict[str, list[dict[str, Any]]] = defaultdict(list)
_REPORTS: dict[str, dict[str, str]] = defaultdict(dict)
_ORIGINAL_OPEN: Any = None
_ORIGINAL_STATUS_CODE: Any = None
_RESPONSE_OBSERVATIONS: dict[int, dict[str, Any]] = {}
_ISOLATION_ATTEMPTS: Counter[str] = Counter()
_ISOLATION_DETAILS: list[dict[str, str]] = []
_UNSAFE_NODEIDS: set[str] = set()
_EXTERNAL_STORAGE_ROOTS: tuple[Path, ...] = ()
EXTERNAL_STORAGE_ACCESS_EVENT = "external_storage_access"


def _trace_path() -> Path:
    value = os.environ.get("MAGI_V3_ROUTE_TRACE_FILE", "").strip()
    if not value:
        raise pytest.UsageError("MAGI_V3_ROUTE_TRACE_FILE is required")
    return Path(value).expanduser().resolve()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_lexically_within(path: Path, root: Path) -> bool:
    """Compare absolute paths without touching a potentially stalled mount."""

    try:
        absolute_path = Path(os.path.abspath(os.fspath(path.expanduser())))
        absolute_root = Path(os.path.abspath(os.fspath(root.expanduser())))
        absolute_path.relative_to(absolute_root)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _external_storage_roots(live_root: Path) -> tuple[Path, ...]:
    try:
        account_home = live_root.parents[2]
    except IndexError as exc:
        raise pytest.UsageError("MAGI V3 route trace live root has no account home") from exc
    return (
        Path("/Volumes"),
        account_home / "Library" / "CloudStorage",
        account_home / ".magi_mounts",
        account_home / "SynologyDrive",
    )


def _install_isolation_guard() -> None:
    sandbox_value = os.environ.get("MAGI_V3_ROUTE_TRACE_SANDBOX", "").strip()
    live_value = os.environ.get("MAGI_V3_ROUTE_TRACE_LIVE_ROOT", "").strip()
    if not sandbox_value or not live_value:
        raise pytest.UsageError(
            "MAGI_V3_ROUTE_TRACE_SANDBOX and MAGI_V3_ROUTE_TRACE_LIVE_ROOT are required"
        )
    sandbox = Path(sandbox_value).expanduser().resolve(strict=True)
    live_root = Path(live_value).expanduser().resolve()
    immutable_live_read_roots = (
        live_root / "releases",
        live_root / "runtimes",
    )
    external_storage_roots = _external_storage_roots(live_root)
    global _EXTERNAL_STORAGE_ROOTS
    _EXTERNAL_STORAGE_ROOTS = external_storage_roots
    write_mask = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

    def block(event: str, detail: str) -> None:
        source = " > ".join(
            f"{Path(frame.filename).name}:{frame.lineno}"
            for frame in traceback.extract_stack(limit=7)[:-1]
        )
        with _LOCK:
            _ISOLATION_ATTEMPTS[event] += 1
            if _CURRENT_NODEID:
                _UNSAFE_NODEIDS.add(_CURRENT_NODEID)
            if len(_ISOLATION_DETAILS) < 20:
                _ISOLATION_DETAILS.append(
                    {
                        "event": event,
                        "detail": detail,
                        "test_nodeid": _CURRENT_NODEID,
                        "stack": source,
                    }
                )
        raise RuntimeError(f"route certification blocked {event}: {detail}; stack={source}")

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event == "socket.bind":
            address = args[1] if len(args) > 1 else None
            if isinstance(address, (str, bytes, os.PathLike)) and _is_within(
                Path(os.fsdecode(address)), sandbox
            ):
                return
            if (
                isinstance(address, tuple)
                and len(address) >= 2
                and address[0] in {"127.0.0.1", "::1"}
                and address[1] == 0
            ):
                return
            block(event, f"external process/network boundary address={address!r}")
        if event in {"socket.connect", "subprocess.Popen", "os.system"}:
            block(event, "external process/network boundary")
        if event in {"sqlite3.connect", "os.chdir"} and args and isinstance(
            args[0], (str, bytes, os.PathLike)
        ):
            path = Path(os.fsdecode(args[0])).expanduser()
            if any(_is_lexically_within(path, root) for root in external_storage_roots):
                block(EXTERNAL_STORAGE_ACCESS_EVENT, str(path))
            if _is_lexically_within(path, live_root) and not any(
                _is_lexically_within(path, root)
                for root in immutable_live_read_roots
            ):
                block("live_state_access", str(path))
        if event in {"open", "os.listdir", "os.scandir"} and args and isinstance(
            args[0], (str, bytes, os.PathLike)
        ):
            path = Path(os.fsdecode(args[0])).expanduser()
            if any(
                _is_lexically_within(path, root)
                for root in external_storage_roots
            ):
                block(EXTERNAL_STORAGE_ACCESS_EVENT, str(path))
            if _is_lexically_within(path, live_root) and not any(
                _is_lexically_within(path, root)
                for root in immutable_live_read_roots
            ):
                block("live_state_access", str(path))
            if event != "open":
                return
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            writes = (isinstance(mode, str) and any(token in mode for token in "wax+")) or (
                isinstance(flags, int) and bool(flags & write_mask)
            )
            if writes and path != Path("/dev/null") and not _is_within(path, sandbox):
                block("write_outside_sandbox", str(path))
        if event in {"os.remove", "os.rename", "os.rmdir", "os.mkdir"} and args:
            path = Path(os.fsdecode(args[0])).expanduser()
            if not _is_within(path, sandbox):
                block("mutation_outside_sandbox", str(path))

    sys.addaudithook(audit)


def _auth_redirect(response: Any) -> bool:
    if int(response.status_code) not in {301, 302, 303, 307, 308}:
        return False
    location = urlsplit(str(response.headers.get("Location") or "")).path.rstrip("/")
    return location in {"/login", "/auth/login"}


_VARIABLE = re.compile(r"<(?:(?P<converter>[a-zA-Z_][\w()]*):)?[^>]+>")


def _rule_matches(rule: str, path: str) -> bool:
    cursor = 0
    parts: list[str] = ["^"]
    for match in _VARIABLE.finditer(rule):
        parts.append(re.escape(rule[cursor : match.start()]))
        converter = str(match.group("converter") or "string")
        if converter.startswith("path"):
            parts.append(".+")
        elif converter.startswith(("int", "float")):
            parts.append(r"[0-9]+(?:\.[0-9]+)?")
        else:
            parts.append("[^/]+")
        cursor = match.end()
    parts.extend((re.escape(rule[cursor:]), "$"))
    return re.fullmatch("".join(parts), path) is not None


def _recording_open(client: Any, *args: Any, **kwargs: Any) -> Any:
    response = _ORIGINAL_OPEN(client, *args, **kwargs)
    method = str(kwargs.get("method") or "GET").upper()
    raw_path = kwargs.get("path")
    if raw_path is None and args:
        raw_path = args[0]
    path = urlsplit(str(raw_path or "/")).path
    try:
        endpoint, _values = client.application.url_map.bind("localhost").match(
            path,
            method=method,
        )
        rule = next(
            str(item.rule)
            for item in client.application.url_map.iter_rules(endpoint)
            if method in item.methods and _rule_matches(str(item.rule), path)
        )
    except Exception:
        return response
    status = int(response.status_code)
    if status >= 400 or status in {401, 403, 404} or _auth_redirect(response):
        return response
    with _LOCK:
        if _CURRENT_NODEID:
            observation = {
                    "rule": rule,
                    "method": method,
                    "endpoint": str(endpoint),
                    "status": status,
                    "content_type": str(response.headers.get("Content-Type") or ""),
                    "location_path": urlsplit(
                        str(response.headers.get("Location") or "")
                    ).path,
                    "success_assertion_lines": [],
                }
            _OBSERVATIONS[_CURRENT_NODEID].append(observation)
            _RESPONSE_OBSERVATIONS[id(response)] = observation
    return response


def _recording_status_code(response: Any) -> int:
    value = int(_ORIGINAL_STATUS_CODE.fget(response))
    observation = _RESPONSE_OBSERVATIONS.get(id(response))
    if observation is None:
        return value
    frame = inspect.currentframe()
    caller = frame.f_back if frame is not None else None
    try:
        if caller is None:
            return value
        filename = str(caller.f_code.co_filename)
        line_number = int(caller.f_lineno)
        line = linecache.getline(filename, line_number).strip()
        if "/tests/" in filename.replace("\\", "/") and line.startswith("assert"):
            proof = f"{Path(filename).resolve()}:{line_number}"
            with _LOCK:
                lines = observation["success_assertion_lines"]
                if proof not in lines:
                    lines.append(proof)
    finally:
        del frame
        del caller
    return value


def pytest_configure(config: pytest.Config) -> None:
    del config
    global _ORIGINAL_OPEN, _ORIGINAL_STATUS_CODE
    from flask.testing import FlaskClient
    from werkzeug.sansio.response import Response

    _install_isolation_guard()
    if _ORIGINAL_OPEN is not None:
        raise pytest.UsageError("route success trace plugin was configured twice")
    _ORIGINAL_OPEN = FlaskClient.open
    FlaskClient.open = _recording_open
    _ORIGINAL_STATUS_CODE = Response.status_code
    Response.status_code = property(
        _recording_status_code,
        _ORIGINAL_STATUS_CODE.fset,
        _ORIGINAL_STATUS_CODE.fdel,
        _ORIGINAL_STATUS_CODE.__doc__,
    )


def pytest_unconfigure(config: pytest.Config) -> None:
    del config
    global _ORIGINAL_OPEN, _ORIGINAL_STATUS_CODE
    if _ORIGINAL_OPEN is None:
        return
    from flask.testing import FlaskClient
    from werkzeug.sansio.response import Response

    FlaskClient.open = _ORIGINAL_OPEN
    Response.status_code = _ORIGINAL_STATUS_CODE
    _ORIGINAL_OPEN = None
    _ORIGINAL_STATUS_CODE = None


def pytest_runtest_setup(item: pytest.Item) -> None:
    global _CURRENT_NODEID
    with _LOCK:
        _CURRENT_NODEID = item.nodeid


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]):
    outcome = yield
    report = outcome.get_result()
    with _LOCK:
        _REPORTS[item.nodeid][report.when] = report.outcome


def pytest_runtest_teardown(item: pytest.Item) -> None:
    del item
    global _CURRENT_NODEID
    with _LOCK:
        _CURRENT_NODEID = ""


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del session
    passed: list[dict[str, Any]] = []
    with _LOCK:
        for nodeid, observations in _OBSERVATIONS.items():
            reports = _REPORTS.get(nodeid, {})
            if reports.get("setup") != "passed" or reports.get("call") != "passed":
                continue
            if reports.get("teardown", "passed") != "passed":
                continue
            passed.extend(
                {**row, "test_nodeid": nodeid}
                for row in observations
                if row.get("success_assertion_lines")
            )
    target = _trace_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "pytest_exit_status": int(exitstatus),
                "observations": passed,
                "isolation_attempts": dict(sorted(_ISOLATION_ATTEMPTS.items())),
                "isolation_attempt_details": list(_ISOLATION_DETAILS),
                "unsafe_test_nodeids": sorted(_UNSAFE_NODEIDS),
                "external_storage_roots": [
                    str(root) for root in _EXTERNAL_STORAGE_ROOTS
                ],
                "external_storage_access_attempts": int(
                    _ISOLATION_ATTEMPTS.get(EXTERNAL_STORAGE_ACCESS_EVENT, 0)
                ),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )

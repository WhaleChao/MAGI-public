#!/usr/bin/env python3
"""Matched, same-host V2/V3 performance certification on disposable state.

The orchestrator starts one private MariaDB through a Unix socket with TCP
disabled, then executes the V2 and V3 arms in blocking child processes.  Both
arms receive the same release-bound request plan and independent copies of the
same synthetic corpus.  The plan covers authenticated session handling,
MariaDB GET/POST, a case-folder creation request, and a closed-case archive
request.  All filesystem paths are under the caller-owned sandbox.

The comparator fails closed when either implementation omits a requested side
effect.  In particular, it does not relabel a 501 response or a synthetic
SQLite measurement as production-shaped MariaDB/NAS evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.dont_write_bytecode = True

from flask import Flask
from flask_login import LoginManager, UserMixin
from werkzeug.test import Client
from werkzeug.wrappers import Response


SCHEMA = "magi.v3.matched-production-performance/v1"
BLOCKER_CODE = "MATCHED_PRODUCTION_PERFORMANCE_NOT_IMPLEMENTED"
LIVE_ROOT = (Path.home() / "Library" / "Application Support" / "MAGI").resolve()
ARM_NAMES = ("v2", "v3")
DEFAULT_ITERATIONS = 60
DEFAULT_REPEATS = 3
SYNTHETIC_USER_ID = "42"
SESSION_SECRET = "magi-v3-disposable-perf-session-secret"
REQUEST_PLAN: tuple[dict[str, Any], ...] = (
    {
        "id": "unauthorized_get",
        "scope": "session",
        "method": "GET",
        "path": "/api/osc/cases?q=MAGI-PERF&status_scope=open&limit=25",
        "authenticated": False,
    },
    {
        "id": "authenticated_get",
        "scope": "mariadb_session",
        "method": "GET",
        "path": "/api/osc/cases?q=MAGI-PERF&status_scope=open&limit=25",
        "authenticated": True,
    },
    {
        "id": "idempotent_upsert",
        "scope": "mariadb_session",
        "method": "POST",
        "path": "/api/osc/cases",
        "authenticated": True,
        "body": {
            "id": "perf-upsert",
            "case_number": "2099-9001",
            "client_name": "MAGI-PERF-UPSERT",
            "case_category": "一般案件",
            "case_type": "民事",
            "case_reason": "離線效能驗證",
            "lawyer": "離線效能律師",
            "status": "進行中",
            "notes": "matched-release-bound-plan",
            "auto_create_folder": False,
        },
    },
    {
        "id": "create_case_folder",
        "scope": "nas_folder",
        "method": "POST",
        "path": "/api/osc/cases",
        "authenticated": True,
        "body": {
            "id": "perf-folder",
            "case_number": "2099-9002",
            "client_name": "MAGI-PERF-FOLDER",
            "case_category": "一般案件",
            "case_type": "民事",
            "case_stage": "一審",
            "case_reason": "離線效能驗證",
            "lawyer": "離線效能律師",
            "status": "進行中",
            "notes": "disposable-folder-only",
            "auto_create_folder": True,
        },
    },
    {
        "id": "archive_closed_case",
        "scope": "nas_archive",
        "method": "POST",
        "path": "/api/osc/cases",
        "authenticated": True,
        "body": {
            "id": "perf-archive",
            "case_number": "2099-9003",
            "client_name": "MAGI-PERF-ARCHIVE",
            "case_category": "一般案件",
            "case_type": "民事",
            "case_reason": "離線效能驗證",
            "lawyer": "離線效能律師",
            "status": "已結案",
            "notes": "disposable-archive-only",
            "auto_create_folder": False,
        },
    },
)

MARIADB_CASES_SCHEMA = """
CREATE TABLE cases (
    id VARCHAR(64) PRIMARY KEY,
    case_number VARCHAR(64) NOT NULL UNIQUE,
    client_name TEXT NOT NULL,
    client_phone TEXT,
    client_email TEXT,
    client_id_number TEXT,
    case_category TEXT,
    case_type TEXT,
    case_stage TEXT,
    case_reason TEXT,
    laf_case_no TEXT,
    application_no TEXT,
    court_name TEXT,
    court_case_no TEXT,
    court_case_number TEXT,
    court_division TEXT,
    legal_aid_status TEXT,
    lawyer TEXT,
    status TEXT,
    manual_status_lock TINYINT NOT NULL DEFAULT 0,
    manual_status_source TEXT,
    manual_status_at DATETIME NULL,
    notes TEXT,
    folder_path TEXT,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    created_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_case_number(case_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
"""


class PerformanceCertificationError(RuntimeError):
    """The benchmark could not produce trustworthy matched evidence."""


class _PerfUser(UserMixin):
    def __init__(self, user_id: str) -> None:
        self.id = user_id


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request_plan_sha256() -> str:
    return _sha256(REQUEST_PLAN)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _prepare_sandbox(path: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_symlink():
        raise PerformanceCertificationError("performance sandbox must not be a symlink")
    resolved = expanded.resolve()
    if resolved == LIVE_ROOT or _is_relative_to(resolved, LIVE_ROOT):
        raise PerformanceCertificationError("performance sandbox must not be live MAGI state")
    if resolved == REPO_ROOT or _is_relative_to(resolved, REPO_ROOT):
        raise PerformanceCertificationError("performance sandbox must not be inside the source tree")
    if resolved.exists():
        if not resolved.is_dir() or any(resolved.iterdir()):
            raise PerformanceCertificationError("performance sandbox must be an empty directory")
    else:
        resolved.mkdir(parents=True)
    (resolved / ".magi-v3-matched-performance-sandbox").write_text(
        "owned disposable matched-performance state\n", encoding="utf-8"
    )
    return resolved


def _find_executable(*names: str) -> Path:
    for name in names:
        located = shutil.which(name)
        if located:
            return Path(located).resolve()
    raise PerformanceCertificationError(f"required executable is unavailable: {names[0]}")


class DisposableMariaDB:
    """One private, no-TCP MariaDB instance owned by the certification run."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.datadir = root / "data"
        self.socket = root / "mariadb.sock"
        self.pid_file = root / "mariadb.pid"
        self.error_log = root / "mariadb.err"
        self.install_log = root / "mariadb-install.log"
        self.install = _find_executable("mariadb-install-db", "mysql_install_db")
        self.server = _find_executable("mariadbd", "mysqld")
        self.process: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        self.root.mkdir()
        install = subprocess.run(
            (
                str(self.install),
                "--no-defaults",
                f"--datadir={self.datadir}",
                "--auth-root-authentication-method=normal",
                "--skip-test-db",
            ),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
        )
        self.install_log.write_bytes(install.stdout + install.stderr)
        if install.returncode != 0:
            raise PerformanceCertificationError("disposable MariaDB initialization failed")
        self.process = subprocess.Popen(
            (
                str(self.server),
                "--no-defaults",
                f"--datadir={self.datadir}",
                f"--socket={self.socket}",
                f"--pid-file={self.pid_file}",
                f"--log-error={self.error_log}",
                "--skip-networking",
                "--innodb-flush-log-at-trx-commit=1",
                "--sync-binlog=1",
            ),
            cwd=self.root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                detail = self.error_log.read_text("utf-8", errors="replace") if self.error_log.exists() else ""
                raise PerformanceCertificationError(
                    "disposable MariaDB exited during startup: " + detail[-500:]
                )
            if self.socket.is_socket():
                try:
                    connection = _mysql_connect(self.socket)
                except Exception:
                    time.sleep(0.05)
                    continue
                connection.close()
                return
            time.sleep(0.05)
        raise PerformanceCertificationError("disposable MariaDB socket did not become ready")

    def stop(self) -> None:
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        self.process = None
        if self.socket.exists():
            raise PerformanceCertificationError("disposable MariaDB socket remained after shutdown")

    def __enter__(self) -> "DisposableMariaDB":
        self.start()
        return self

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.stop()


def _mysql_connect(socket_path: Path, database: str | None = None) -> Any:
    import mysql.connector

    kwargs: dict[str, Any] = {
        "user": "root",
        "unix_socket": str(socket_path),
        "connection_timeout": 10,
        "use_pure": True,
    }
    if database:
        kwargs["database"] = database
    return mysql.connector.connect(**kwargs)


def _create_database(socket_path: Path, name: str) -> None:
    if not name.replace("_", "").isalnum():
        raise PerformanceCertificationError("invalid disposable database name")
    connection = _mysql_connect(socket_path)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(f"DROP DATABASE IF EXISTS `{name}`")
            cursor.execute(
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        finally:
            cursor.close()
    finally:
        connection.close()


def _initialize_arm_database(socket_path: Path, database: str, archive_source: Path) -> None:
    connection = _mysql_connect(socket_path, database)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute(
                "CREATE TABLE users (id VARCHAR(64) PRIMARY KEY, username TEXT, role TEXT, "
                "is_active TINYINT NOT NULL DEFAULT 1) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
            )
            cursor.execute(MARIADB_CASES_SCHEMA)
            cursor.execute(
                "INSERT INTO users(id,username,role,is_active) VALUES (%s,%s,%s,1)",
                (SYNTHETIC_USER_ID, "offline-perf", "admin"),
            )
            rows = (
                (
                    "perf-upsert",
                    "2099-9001",
                    "MAGI-PERF-UPSERT",
                    "進行中",
                    "before",
                    "",
                ),
                (
                    "perf-archive",
                    "2099-9003",
                    "MAGI-PERF-ARCHIVE",
                    "進行中",
                    "archive-source",
                    str(archive_source),
                ),
            )
            cursor.executemany(
                "INSERT INTO cases(id,case_number,client_name,case_category,case_type,case_stage,"
                "case_reason,lawyer,status,notes,folder_path,updated_at,created_date) "
                "VALUES (%s,%s,%s,'一般案件','民事','一審','離線效能驗證','離線效能律師',"
                "%s,%s,%s,NOW(),NOW())",
                rows,
            )
            connection.commit()
        finally:
            cursor.close()
    finally:
        connection.close()


def _v2_exec_factory(socket_path: Path, database: str) -> Callable[..., tuple[Any, dict[str, str]]]:
    def execute(
        sql: str,
        params: tuple[Any, ...] = (),
        *,
        fetch: str = "none",
    ) -> tuple[Any, dict[str, str]]:
        connection = _mysql_connect(socket_path, database)
        try:
            cursor = connection.cursor(dictionary=True)
            try:
                cursor.execute(sql, params)
                if fetch == "one":
                    row = cursor.fetchone()
                    connection.rollback()
                    return (dict(row) if row is not None else None), {"backend": "mariadb"}
                if fetch == "all":
                    rows = [dict(row) for row in (cursor.fetchall() or [])]
                    connection.rollback()
                    return rows, {"backend": "mariadb"}
                if fetch != "none":
                    raise PerformanceCertificationError("V2 requested an invalid MariaDB fetch mode")
                connection.commit()
                return {
                    "rowcount": int(cursor.rowcount),
                    "lastrowid": getattr(cursor, "lastrowid", None),
                }, {"backend": "mariadb"}
            except BaseException:
                connection.rollback()
                raise
            finally:
                cursor.close()
        finally:
            connection.close()

    return execute


class _V2SandboxWSGI:
    def __init__(self, app: Flask, module: Any, socket_path: Path, database: str, arm_root: Path) -> None:
        self.app = app
        self.module = module
        self.execute = _v2_exec_factory(socket_path, database)
        self.arm_root = arm_root
        self.active_root = arm_root / "01_案件"
        self.archive_root = arm_root / "10_結案"

    def __call__(self, environ: dict[str, Any], start_response: Any) -> Any:
        def local_candidates(value: str, **_kwargs: Any) -> list[str]:
            return [str(value)] if value else []

        def resolve_existing(value: str, **_kwargs: Any) -> str:
            return str(value) if value and Path(value).exists() else ""

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.module, "_osc_exec", side_effect=self.execute))
            stack.enter_context(
                patch.object(
                    self.module,
                    "_get_translate_local_path_to_canonical",
                    return_value=lambda value: str(value),
                )
            )
            stack.enter_context(
                patch.object(self.module, "_osc_norm_path", side_effect=lambda value: str(value or ""))
            )
            stack.enter_context(patch.object(self.module, "_CASE_MANUAL_STATUS_SCHEMA_READY", True))
            stack.enter_context(
                patch.object(
                    self.module,
                    "_osc_select_case_creation_root",
                    return_value={"ok": True, "root": str(self.active_root)},
                )
            )
            stack.enter_context(
                patch.object(self.module, "_osc_case_folder_creation_guard", return_value={"ok": True})
            )
            stack.enter_context(
                patch.object(
                    self.module,
                    "_osc_try_create_drive_case_folder",
                    return_value={"ok": True, "skipped": True, "reason": "offline"},
                )
            )
            stack.enter_context(
                patch.object(self.module, "_osc_local_path_candidates", side_effect=local_candidates)
            )
            stack.enter_context(
                patch.object(self.module, "_osc_resolve_existing_local_path", side_effect=resolve_existing)
            )
            stack.enter_context(
                patch.object(self.module, "_osc_get_closed_archive_base", return_value=str(self.archive_root))
            )
            return self.app(environ, start_response)


def _build_v2_app(socket_path: Path, database: str, arm_root: Path) -> Any:
    import api.blueprints.osc_cases as osc_module

    app = Flask("magi_v3_matched_perf_v2")
    app.config.update(TESTING=True, SECRET_KEY=SESSION_SECRET, PROPAGATE_EXCEPTIONS=True)
    login = LoginManager()
    login.init_app(app)

    @login.user_loader
    def load_user(user_id: str) -> _PerfUser | None:
        if user_id != SYNTHETIC_USER_ID:
            return None
        connection = _mysql_connect(socket_path, database)
        try:
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT id FROM users WHERE id=%s AND is_active=1", (user_id,))
                return _PerfUser(user_id) if cursor.fetchone() else None
            finally:
                cursor.close()
        finally:
            connection.close()

    app.register_blueprint(osc_module.osc_bp)
    return _V2SandboxWSGI(app, osc_module, socket_path, database, arm_root)


def _not_found(environ: Mapping[str, Any], start_response: Callable[..., Any]) -> list[bytes]:
    start_response("404 Not Found", [("Content-Type", "text/plain")])
    return [b"not found"]


def _build_v3_app(socket_path: Path, database: str, arm_root: Path) -> Any:
    from magi_v3.osc_main import create_main_app

    def connection_factory() -> Any:
        return _mysql_connect(socket_path, database)

    return create_main_app(
        connection_factory=connection_factory,
        fallback_factory=lambda: _not_found,
        environ={
            "FLASK_SECRET_KEY": SESSION_SECRET,
            "MAGI_CSRF_ALLOW_CLI": "1",
            "MAGI_DEFAULT_LAWYER": "離線效能律師",
            "MAGI_V3_CASE_ROOT": str(arm_root / "01_案件"),
            "MAGI_V3_ARCHIVE_ROOT": str(arm_root / "10_結案"),
            "MAGI_V3_DISPOSABLE_NAS_ROOT": str(arm_root),
        },
        path_mappings=((str(arm_root), str(arm_root)),),
    )


def _signed_session_cookie() -> str:
    signer = Flask("magi_v3_matched_perf_session")
    signer.secret_key = SESSION_SECRET
    with signer.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = SYNTHETIC_USER_ID
            session["_fresh"] = True
        cookie = client.get_cookie("session")
        if cookie is None:
            raise PerformanceCertificationError("failed to create disposable signed session cookie")
        return cookie.value


def _percentile(values: Sequence[int], percentile: float) -> float:
    if not values:
        raise PerformanceCertificationError("latency sample set is empty")
    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return float(ordered[lower] * (1 - fraction) + ordered[upper] * fraction)


def _filesystem_inventory(root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise PerformanceCertificationError("filesystem transcript contains a symlink")
        if path.is_file():
            row = {
                "path": relative,
                "kind": "file",
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
            if path.name == ".gitkeep":
                parent = path.parent.name
                content = path.read_text(encoding="utf-8")
                normalized = _folder_marker_projection(relative, content)
                row.update(
                    raw_content=content,
                    raw_content_sha256=hashlib.sha256(content.encode()).hexdigest(),
                    normalized_content=normalized,
                    normalized_content_sha256=hashlib.sha256(
                        normalized.encode()
                    ).hexdigest(),
                )
            rows.append(row)
        elif path.is_dir():
            rows.append({"path": relative, "kind": "directory"})
    return {"entries": rows, "sha256": _sha256(rows), "count": len(rows)}


def _folder_marker_projection(path: str, content: str) -> str:
    marker = Path(path)
    if marker.name != ".gitkeep" or len(marker.parts) < 2:
        raise PerformanceCertificationError(
            "only a nested .gitkeep folder marker can be normalized"
        )
    parent = marker.parent.name
    match = re.fullmatch(
        rf"# {re.escape(parent)} - 建立於 (\d{{4}}-\d{{2}}-\d{{2}} \d{{2}}:\d{{2}}:\d{{2}})",
        content,
    )
    if match is None:
        raise PerformanceCertificationError(
            "folder marker has wrong parent or extra/non-timestamp content"
        )
    try:
        parsed = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise PerformanceCertificationError("folder marker timestamp is not a real datetime") from exc
    if parsed.strftime("%Y-%m-%d %H:%M:%S") != match.group(1):
        raise PerformanceCertificationError("folder marker timestamp is not canonical")
    return f"# {parent} - 建立於 <YYYY-MM-DD HH:MM:SS>"


def _filesystem_semantic_projection(inventory: Mapping[str, Any]) -> list[dict[str, Any]]:
    entries = inventory.get("entries")
    if not isinstance(entries, list):
        raise PerformanceCertificationError("filesystem inventory entries are missing")
    projected: list[dict[str, Any]] = []
    for row in entries:
        if not isinstance(row, dict):
            raise PerformanceCertificationError("filesystem inventory row is invalid")
        item = dict(row)
        path = str(item.get("path") or "")
        volatile_fields = {
            "raw_content",
            "raw_content_sha256",
            "normalized_content",
            "normalized_content_sha256",
            "volatile_content_contract",
        }
        if volatile_fields.intersection(item) and not (
            item.get("kind") == "file" and Path(path).name == ".gitkeep"
        ):
            raise PerformanceCertificationError(
                "non-.gitkeep inventory row attempts volatile normalization"
            )
        if item.get("kind") == "file" and Path(path).name == ".gitkeep":
            raw = item.pop("raw_content", None)
            raw_sha = item.pop("raw_content_sha256", None)
            normalized = item.pop("normalized_content", None)
            normalized_sha = item.pop("normalized_content_sha256", None)
            if not isinstance(raw, str) or raw_sha != hashlib.sha256(raw.encode()).hexdigest():
                raise PerformanceCertificationError(
                    "folder marker raw content is missing or hash-mismatched"
                )
            expected_contract = _folder_marker_projection(path, raw)
            if item.get("size") != len(raw.encode("utf-8")):
                raise PerformanceCertificationError(
                    "folder marker size differs from its timestamp-only contract"
                )
            if item.get("sha256") != raw_sha:
                raise PerformanceCertificationError(
                    "folder marker inventory hash is not bound to raw content"
                )
            item.pop("sha256", None)
            # The legacy self-asserted field is forbidden in formal evidence.
            if item.pop("volatile_content_contract", None) is not None:
                raise PerformanceCertificationError(
                    "legacy folder marker volatile contract is not authoritative"
                )
            if (
                normalized != expected_contract
                or normalized_sha != hashlib.sha256(expected_contract.encode()).hexdigest()
            ):
                raise PerformanceCertificationError(
                    "folder marker normalized digest is not code-owned"
                )
            item["normalized_content_sha256"] = normalized_sha
        projected.append(item)
    return projected


def _request(client: Client, row: Mapping[str, Any], *, authenticated: bool) -> Any:
    if authenticated:
        client.set_cookie("session", _signed_session_cookie())
    else:
        client.delete_cookie("session")
    headers = {"X-MAGI-Client": "cli", "X-MAGI-CLI": "1"}
    if row["method"] == "GET":
        return client.get(str(row["path"]), headers=headers)
    return client.post(str(row["path"]), json=row.get("body"), headers=headers)


def _project_response(request_id: str, response: Any) -> dict[str, Any]:
    payload = response.get_json(silent=True)
    result: dict[str, Any] = {"status": int(response.status_code)}
    if request_id == "unauthorized_get":
        result["unauthorized"] = response.status_code in {401, 403}
    elif request_id == "authenticated_get":
        items = payload.get("items", []) if isinstance(payload, dict) else []
        result.update(
            {
                "ok": bool(isinstance(payload, dict) and payload.get("ok") is True),
                "case_numbers": sorted(str(item.get("case_number")) for item in items),
            }
        )
    else:
        result.update(
            {
                "ok": bool(isinstance(payload, dict) and payload.get("ok") is True),
                "id": str(payload.get("id") or "") if isinstance(payload, dict) else "",
                "case_number": str(payload.get("case_number") or "") if isinstance(payload, dict) else "",
                "mode": str(payload.get("mode") or "") if isinstance(payload, dict) else "",
                "folder_ok": bool(
                    isinstance(payload, dict)
                    and isinstance(payload.get("folder"), dict)
                    and payload["folder"].get("ok") is True
                ),
                "archive_ok": bool(
                    isinstance(payload, dict)
                    and isinstance(payload.get("archive"), dict)
                    and payload["archive"].get("ok") is True
                ),
                "folder_reason": str(
                    payload.get("folder", {}).get("reason") or payload.get("folder", {}).get("error") or ""
                )
                if isinstance(payload, dict) and isinstance(payload.get("folder"), dict)
                else "",
                "archive_reason": str(
                    payload.get("archive", {}).get("reason")
                    or payload.get("archive", {}).get("error")
                    or ""
                )
                if isinstance(payload, dict) and isinstance(payload.get("archive"), dict)
                else "",
                "error": str(payload.get("error") or "") if isinstance(payload, dict) else "",
            }
        )
    return result


def _query_case_state(socket_path: Path, database: str) -> list[dict[str, Any]]:
    connection = _mysql_connect(socket_path, database)
    try:
        cursor = connection.cursor(dictionary=True)
        try:
            cursor.execute(
                "SELECT id,case_number,status,notes,folder_path FROM cases "
                "WHERE id IN ('perf-upsert','perf-folder','perf-archive') ORDER BY id"
            )
            return [
                {
                    "id": str(row.get("id") or ""),
                    "case_number": str(row.get("case_number") or ""),
                    "status": str(row.get("status") or ""),
                    "notes": str(row.get("notes") or ""),
                    "folder_location": (
                        "archive"
                        if "10_結案" in str(row.get("folder_path") or "")
                        else "active"
                        if str(row.get("folder_path") or "")
                        else "empty"
                    ),
                }
                for row in (cursor.fetchall() or [])
            ]
        finally:
            cursor.close()
    finally:
        connection.close()


def run_arm(
    arm: str,
    *,
    sandbox: Path,
    socket_path: Path,
    database: str,
    iterations: int,
) -> dict[str, Any]:
    if arm not in ARM_NAMES:
        raise PerformanceCertificationError("invalid benchmark arm")
    if not 10 <= iterations <= 10_000:
        raise PerformanceCertificationError("iterations must be between 10 and 10000")
    arm_root = sandbox / f"{arm}-nas"
    active = arm_root / "01_案件" / "一般案件" / "民事"
    archive = arm_root / "10_結案"
    archive_source = active / "2099-9003-MAGI-PERF-ARCHIVE"
    archive_source.mkdir(parents=True)
    archive.mkdir(parents=True)
    (archive_source / "payload.txt").write_text("disposable archive payload\n", encoding="utf-8")
    _initialize_arm_database(socket_path, database, archive_source)
    application = (
        _build_v2_app(socket_path, database, arm_root)
        if arm == "v2"
        else _build_v3_app(socket_path, database, arm_root)
    )
    client = Client(application, Response)
    responses: dict[str, Any] = {}
    scenario_latency_us: dict[str, int] = {}
    for row in REQUEST_PLAN:
        started = time.perf_counter_ns()
        response = _request(client, row, authenticated=bool(row["authenticated"]))
        scenario_latency_us[str(row["id"])] = max(1, (time.perf_counter_ns() - started) // 1_000)
        responses[str(row["id"])] = _project_response(str(row["id"]), response)

    warm_latencies: list[int] = []
    perf_rows = REQUEST_PLAN[1:3]
    for index in range(iterations):
        row = perf_rows[index % len(perf_rows)]
        started = time.perf_counter_ns()
        response = _request(client, row, authenticated=True)
        elapsed = max(1, (time.perf_counter_ns() - started) // 1_000)
        projection = _project_response(str(row["id"]), response)
        if response.status_code != 200 or projection.get("ok") is not True:
            raise PerformanceCertificationError(
                f"{arm} warm request failed: {row['id']} status={response.status_code} "
                f"projection={json.dumps(projection, ensure_ascii=False, sort_keys=True)}"
            )
        warm_latencies.append(elapsed)

    from api.blueprints.osc_cases import osc_cases_api as v2_handler
    from magi_v3.osc_cases import OscCasesApplication, OscCasesService, MariaDBCaseTransaction

    v3_handler_source = "\n".join(
        (
            __import__("inspect").getsource(OscCasesApplication.__call__),
            __import__("inspect").getsource(OscCasesService.create_case),
            __import__("inspect").getsource(MariaDBCaseTransaction.update_case),
        )
    )
    report = {
        "schema": "magi.v3.matched-production-performance-arm/v1",
        "arm": arm,
        "pid": os.getpid(),
        "parent_pid": os.getppid(),
        "started_and_completed_in_one_process": True,
        "request_plan_sha256": request_plan_sha256(),
        "release_binding": {
            "script_sha256": _sha256_file(SCRIPT_PATH),
            "v2_handler_sha256": hashlib.sha256(
                __import__("inspect").getsource(v2_handler).encode("utf-8")
            ).hexdigest(),
            "v3_handler_sha256": hashlib.sha256(v3_handler_source.encode("utf-8")).hexdigest(),
            "python_executable_sha256": _sha256_file(Path(sys.executable).resolve()),
        },
        "backend": {
            "engine": "MariaDB",
            "transport": "unix_socket",
            "tcp_networking": False,
            "database": database,
            "innodb_flush_log_at_trx_commit": 1,
            "sync_binlog": 1,
        },
        "parameters": {"iterations": iterations},
        "responses": responses,
        "scenario_latency_us": scenario_latency_us,
        "warm": {
            "samples": len(warm_latencies),
            "p50_us": _percentile(warm_latencies, 0.50),
            "p95_us": _percentile(warm_latencies, 0.95),
            "p99_us": _percentile(warm_latencies, 0.99),
        },
        "filesystem": _filesystem_inventory(arm_root),
        "database_state": _query_case_state(socket_path, database),
        "safety": {
            "live_state_accessed": False,
            "production_service_started": False,
            "listener_started": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "database_transport_was_unix_socket": True,
            "database_tcp_networking_disabled": True,
            "filesystem_root_was_disposable": True,
        },
    }
    report["evidence_sha256"] = _sha256(report)
    return report


def _load_threshold() -> float:
    config = json.loads((REPO_ROOT / "config" / "v3_cutover_gates.json").read_text("utf-8"))
    threshold = float(config["promotion_thresholds"]["max_p95_regression_ratio"])
    if not 0 <= threshold <= 1:
        raise PerformanceCertificationError("configured p95 regression threshold is invalid")
    return threshold


def compare_arm_reports(
    v2_reports: Sequence[Mapping[str, Any]],
    v3_reports: Sequence[Mapping[str, Any]],
    *,
    p95_regression_limit: float,
) -> dict[str, Any]:
    if not v2_reports or len(v2_reports) != len(v3_reports):
        raise PerformanceCertificationError("matched arm repeat counts differ")
    all_reports = list(v2_reports) + list(v3_reports)
    if {row.get("request_plan_sha256") for row in all_reports} != {request_plan_sha256()}:
        raise PerformanceCertificationError("matched request-plan binding drifted")
    runtime_hashes = {
        row.get("release_binding", {}).get("python_executable_sha256") for row in all_reports
    }
    script_hashes = {row.get("release_binding", {}).get("script_sha256") for row in all_reports}
    if len(runtime_hashes) != 1 or len(script_hashes) != 1:
        raise PerformanceCertificationError("runtime or certifier release binding drifted")
    if any(row.get("backend", {}).get("engine") != "MariaDB" for row in all_reports):
        raise PerformanceCertificationError("an arm did not use disposable MariaDB")
    if any(row.get("backend", {}).get("tcp_networking") is not False for row in all_reports):
        raise PerformanceCertificationError("an arm enabled MariaDB TCP networking")

    v2 = v2_reports[-1]
    v3 = v3_reports[-1]
    response_checks = {
        request_id: v2["responses"].get(request_id) == v3["responses"].get(request_id)
        for request_id in ("unauthorized_get", "authenticated_get", "idempotent_upsert")
    }
    session_passed = all(
        row["responses"]["unauthorized_get"].get("unauthorized") is True
        and row["responses"]["authenticated_get"].get("status") == 200
        for row in all_reports
    )
    folder_passed = all(
        row["responses"]["create_case_folder"].get("status") == 200
        and row["responses"]["create_case_folder"].get("folder_ok") is True
        for row in all_reports
    )
    archive_passed = all(
        row["responses"]["archive_closed_case"].get("status") == 200
        and row["responses"]["archive_closed_case"].get("archive_ok") is True
        for row in all_reports
    )
    v2_p95 = statistics.median(float(row["warm"]["p95_us"]) for row in v2_reports)
    v3_p95 = statistics.median(float(row["warm"]["p95_us"]) for row in v3_reports)
    ratio = v3_p95 / v2_p95 if v2_p95 else float("inf")
    scenario_comparable = {
        "authenticated_get": response_checks["authenticated_get"] and session_passed,
        "idempotent_upsert": response_checks["idempotent_upsert"] and session_passed,
        "create_case_folder": folder_passed,
        "archive_closed_case": archive_passed,
    }
    scenario_ratios: dict[str, dict[str, Any]] = {}
    for request_id in (
        "authenticated_get",
        "idempotent_upsert",
        "create_case_folder",
        "archive_closed_case",
    ):
        v2_scenario = statistics.median(
            float(row.get("scenario_latency_us", {}).get(request_id, 1)) for row in v2_reports
        )
        v3_scenario = statistics.median(
            float(row.get("scenario_latency_us", {}).get(request_id, 1)) for row in v3_reports
        )
        scenario_ratio = v3_scenario / v2_scenario if v2_scenario else float("inf")
        scenario_ratios[request_id] = {
            "v2_median_us": v2_scenario,
            "v3_median_us": v3_scenario,
            "v3_over_v2_ratio": scenario_ratio,
            "maximum_ratio": 1 + p95_regression_limit,
            "comparable": scenario_comparable[request_id],
            "passed": scenario_comparable[request_id]
            and scenario_ratio <= 1 + p95_regression_limit,
        }
    filesystem_equivalent = all(
        _filesystem_semantic_projection(v2_reports[index].get("filesystem", {}))
        == _filesystem_semantic_projection(v3_reports[index].get("filesystem", {}))
        for index in range(len(v2_reports))
    )
    database_state_equivalent = all(
        v2_reports[index].get("database_state") == v3_reports[index].get("database_state")
        for index in range(len(v2_reports))
    )
    performance_passed = ratio <= 1 + p95_regression_limit and all(
        row["passed"] for row in scenario_ratios.values()
    )
    semantic_passed = (
        all(response_checks.values())
        and session_passed
        and folder_passed
        and archive_passed
        and filesystem_equivalent
        and database_state_equivalent
    )
    gaps: list[str] = []
    if not all(response_checks.values()):
        gaps.append("MariaDB GET/POST response projection drift")
    if not session_passed:
        gaps.append("signed session authorization did not match")
    if not folder_passed:
        gaps.append("native V3 case-folder side effect is absent or differs")
    if not archive_passed:
        gaps.append("native V3 closed-case archive side effect is absent or differs")
    if not filesystem_equivalent:
        gaps.append("disposable NAS filesystem transcript differs")
    if not database_state_equivalent:
        gaps.append("post-plan MariaDB state differs")
    if not performance_passed:
        if not all(scenario_comparable.values()):
            gaps.append("matched performance is incomplete because workload scenarios did not complete")
        else:
            gaps.append("matched p95 regression exceeded the release threshold")
    return {
        "same_request_plan": True,
        "same_python_runtime": True,
        "same_certifier_release": True,
        "same_host_sequential": True,
        "mariadb_backend": True,
        "session_passed": session_passed,
        "response_checks": response_checks,
        "folder_passed": folder_passed,
        "archive_passed": archive_passed,
        "filesystem_transcript_equivalent": filesystem_equivalent,
        "database_state_equivalent": database_state_equivalent,
        "semantic_equivalence_passed": semantic_passed,
        "performance": {
            "v2_median_p95_us": v2_p95,
            "v3_median_p95_us": v3_p95,
            "v3_over_v2_ratio": ratio,
            "maximum_ratio": 1 + p95_regression_limit,
            "passed": performance_passed,
            "scenario_ratios": scenario_ratios,
        },
        "eligible_to_clear_full_v2_v3_performance_blocker": semantic_passed
        and performance_passed,
        "gaps": gaps,
    }


def verify_performance_certification(evidence: Mapping[str, Any]) -> None:
    supplied = evidence.get("evidence_sha256")
    if not isinstance(supplied, str) or len(supplied) != 64:
        raise PerformanceCertificationError("performance certification hash is missing")
    unsigned = dict(evidence)
    unsigned.pop("evidence_sha256", None)
    if supplied != _sha256(unsigned):
        raise PerformanceCertificationError("performance certification hash does not match")
    if evidence.get("schema") != SCHEMA:
        raise PerformanceCertificationError("performance certification schema is invalid")
    if evidence.get("status") != "certified":
        return
    if (
        evidence.get("workload") != "matched_v2_v3_performance"
        or evidence.get("probe")
        != "sequential_release_bound_mariadb_session_nas_folder_archive"
        or evidence.get("request_plan") != list(REQUEST_PLAN)
    ):
        raise PerformanceCertificationError(
            "certified performance workload/request plan is invalid"
        )
    parameters = evidence.get("parameters")
    reports = evidence.get("reports")
    binding = evidence.get("release_binding")
    order = evidence.get("execution_order")
    proof = evidence.get("sequential_process_proof")
    safety = evidence.get("safety")
    gate = evidence.get("gate")
    comparison = evidence.get("comparison")
    if (
        not isinstance(parameters, dict)
        or type(parameters.get("iterations")) is not int
        or not 10 <= parameters["iterations"] <= 10_000
        or type(parameters.get("repeats")) is not int
        or not 1 <= parameters["repeats"] <= 9
        or not isinstance(reports, dict)
        or not isinstance(binding, dict)
        or not isinstance(order, list)
        or not isinstance(proof, dict)
        or not isinstance(safety, dict)
        or not isinstance(gate, dict)
        or not isinstance(comparison, dict)
    ):
        raise PerformanceCertificationError(
            "certified performance report structure is invalid"
        )
    v2_reports = reports.get("v2")
    v3_reports = reports.get("v3")
    repeats = parameters["repeats"]
    if (
        not isinstance(v2_reports, list)
        or not isinstance(v3_reports, list)
        or len(v2_reports) != repeats
        or len(v3_reports) != repeats
        or len(order) != repeats * 2
    ):
        raise PerformanceCertificationError(
            "certified performance arm/repeat coverage is invalid"
        )
    expected_script = binding.get("certifier_script_sha256")
    expected_runtime = binding.get("python_executable_sha256")
    if (
        binding.get("request_plan_sha256") != request_plan_sha256()
        or not isinstance(expected_script, str)
        or len(expected_script) != 64
        or not isinstance(expected_runtime, str)
        or len(expected_runtime) != 64
    ):
        raise PerformanceCertificationError(
            "certified performance release binding is invalid"
        )
    all_reports = [*v2_reports, *v3_reports]
    for arm_report in all_reports:
        if not isinstance(arm_report, dict):
            raise PerformanceCertificationError("performance arm report is not an object")
        arm_hash = arm_report.get("evidence_sha256")
        unsigned_arm = dict(arm_report)
        unsigned_arm.pop("evidence_sha256", None)
        arm_safety = arm_report.get("safety")
        arm_binding = arm_report.get("release_binding")
        backend = arm_report.get("backend")
        if (
            arm_hash != _sha256(unsigned_arm)
            or arm_report.get("schema")
            != "magi.v3.matched-production-performance-arm/v1"
            or arm_report.get("request_plan_sha256") != request_plan_sha256()
            or arm_report.get("started_and_completed_in_one_process") is not True
            or not isinstance(arm_binding, dict)
            or arm_binding.get("script_sha256") != expected_script
            or arm_binding.get("python_executable_sha256") != expected_runtime
            or not isinstance(backend, dict)
            or backend.get("engine") != "MariaDB"
            or backend.get("transport") != "unix_socket"
            or backend.get("tcp_networking") is not False
            or backend.get("innodb_flush_log_at_trx_commit") != 1
            or backend.get("sync_binlog") != 1
            or not isinstance(arm_safety, dict)
            or arm_safety.get("live_state_accessed") is not False
            or arm_safety.get("production_service_started") is not False
            or arm_safety.get("listener_started") is not False
            or arm_safety.get("production_port_accessed") is not False
            or arm_safety.get("launchctl_invoked") is not False
            or arm_safety.get("database_transport_was_unix_socket") is not True
            or arm_safety.get("database_tcp_networking_disabled") is not True
            or arm_safety.get("filesystem_root_was_disposable") is not True
        ):
            raise PerformanceCertificationError(
                "performance arm hash/release/backend/safety proof is invalid"
            )
    recomputed = compare_arm_reports(
        v2_reports,
        v3_reports,
        p95_regression_limit=_load_threshold(),
    )
    pids = [row.get("pid") for row in order if isinstance(row, dict)]
    intervals_valid = all(
        isinstance(left, dict)
        and isinstance(right, dict)
        and type(left.get("completed_monotonic_ns")) is int
        and type(right.get("started_monotonic_ns")) is int
        and left["completed_monotonic_ns"] <= right["started_monotonic_ns"]
        for left, right in zip(order, order[1:])
    )
    if (
        comparison != recomputed
        or recomputed.get("eligible_to_clear_full_v2_v3_performance_blocker")
        is not True
        or gate
        != {
            "blocker_code": BLOCKER_CODE,
            "eligible_to_clear_full_v2_v3_performance_blocker": True,
            "decision": "clear",
            "gaps": [],
        }
        or proof.get("maximum_simultaneous_version_arms") != 1
        or proof.get("blocking_subprocess_run_used") is not True
        or proof.get("distinct_child_pid_per_arm") is not True
        or proof.get("intervals_non_overlapping") is not True
        or not intervals_valid
        or any(type(pid) is not int or pid <= 0 for pid in pids)
        or len(set(pids)) != len(pids)
        or safety.get("live_state_accessed") is not False
        or safety.get("live_business_database_accessed") is not False
        or safety.get("production_service_started") is not False
        or safety.get("production_port_accessed") is not False
        or safety.get("launchctl_invoked") is not False
        or safety.get("version_arms_ran_concurrently") is not False
        or safety.get("mariadb_tcp_networking_disabled") is not True
        or safety.get("mariadb_unix_socket_removed_after_shutdown") is not True
    ):
        raise PerformanceCertificationError(
            "performance aggregate/sequential/safety proof is invalid"
        )


def _run_arm_child(
    arm: str,
    *,
    sandbox: Path,
    socket_path: Path,
    database: str,
    iterations: int,
) -> dict[str, Any]:
    command = (
        sys.executable,
        str(SCRIPT_PATH),
        "--arm",
        arm,
        "--sandbox",
        str(sandbox),
        "--socket",
        str(socket_path),
        "--database",
        database,
        "--iterations",
        str(iterations),
    )
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONPATH": str(REPO_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPYCACHEPREFIX": "/dev/null",
            "PYTHONHASHSEED": "0",
            "HOME": str(sandbox),
            "TMPDIR": str(sandbox),
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if result.returncode != 0:
        raise PerformanceCertificationError(
            f"{arm} matched-performance arm failed: {result.stderr.strip()[-1000:]}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PerformanceCertificationError(f"{arm} arm emitted invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("arm") != arm:
        raise PerformanceCertificationError(f"{arm} arm identity drifted")
    return payload


def run_performance_certification(
    workdir: Path,
    *,
    iterations: int = DEFAULT_ITERATIONS,
    repeats: int = DEFAULT_REPEATS,
) -> dict[str, Any]:
    if not 1 <= repeats <= 9:
        raise PerformanceCertificationError("repeats must be between 1 and 9")
    sandbox = _prepare_sandbox(workdir)
    execution_order: list[dict[str, Any]] = []
    reports: dict[str, list[dict[str, Any]]] = {"v2": [], "v3": []}
    server = DisposableMariaDB(sandbox / "mariadb")
    with server:
        for repeat in range(repeats):
            order = ARM_NAMES if repeat % 2 == 0 else tuple(reversed(ARM_NAMES))
            for arm in order:
                database = f"magi_perf_{repeat}_{arm}"
                _create_database(server.socket, database)
                arm_sandbox = sandbox / f"repeat-{repeat}-{arm}"
                arm_sandbox.mkdir()
                started_ns = time.monotonic_ns()
                report = _run_arm_child(
                    arm,
                    sandbox=arm_sandbox,
                    socket_path=server.socket,
                    database=database,
                    iterations=iterations,
                )
                completed_ns = time.monotonic_ns()
                reports[arm].append(report)
                execution_order.append(
                    {
                        "ordinal": len(execution_order),
                        "repeat": repeat,
                        "arm": arm,
                        "pid": report["pid"],
                        "started_monotonic_ns": started_ns,
                        "completed_monotonic_ns": completed_ns,
                    }
                )
    comparison = compare_arm_reports(
        reports["v2"], reports["v3"], p95_regression_limit=_load_threshold()
    )
    eligible = comparison["eligible_to_clear_full_v2_v3_performance_blocker"]
    evidence: dict[str, Any] = {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "certified" if eligible else "blocked",
        "workload": "matched_v2_v3_performance",
        "probe": "sequential_release_bound_mariadb_session_nas_folder_archive",
        "release_binding": {
            "request_plan_sha256": request_plan_sha256(),
            "certifier_script_sha256": _sha256_file(SCRIPT_PATH),
            "python_executable_sha256": _sha256_file(Path(sys.executable).resolve()),
            "v2_handler_sha256": reports["v2"][0]["release_binding"]["v2_handler_sha256"],
            "v3_handler_sha256": reports["v3"][0]["release_binding"]["v3_handler_sha256"],
        },
        "request_plan": list(REQUEST_PLAN),
        "parameters": {"iterations": iterations, "repeats": repeats},
        "execution_order": execution_order,
        "sequential_process_proof": {
            "maximum_simultaneous_version_arms": 1,
            "blocking_subprocess_run_used": True,
            "distinct_child_pid_per_arm": len({row["pid"] for row in execution_order})
            == len(execution_order),
            "intervals_non_overlapping": all(
                left["completed_monotonic_ns"] <= right["started_monotonic_ns"]
                for left, right in zip(execution_order, execution_order[1:])
            ),
        },
        "reports": reports,
        "comparison": comparison,
        "gate": {
            "blocker_code": BLOCKER_CODE,
            "eligible_to_clear_full_v2_v3_performance_blocker": eligible,
            "decision": "clear" if eligible else "blocker_retained",
            "gaps": comparison["gaps"],
        },
        "safety": {
            "live_state_accessed": False,
            "live_business_database_accessed": False,
            "production_service_started": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "version_arms_ran_concurrently": False,
            "mariadb_tcp_networking_disabled": True,
            "mariadb_unix_socket_removed_after_shutdown": not server.socket.exists(),
            "sandbox_path_sha256": hashlib.sha256(str(sandbox).encode("utf-8")).hexdigest(),
        },
        "limitations": [
            "The NAS surface is a disposable same-host filesystem root, not a remote SMB transport.",
            "No production service lifecycle, listener, LIVE database, LIVE NAS path, or external provider was used.",
            "A blocked folder/archive result is preserved as a release blocker and is never imputed from V2.",
        ],
        "hash_scheme": "sha256(canonical-json-without-evidence_sha256)",
    }
    evidence["evidence_sha256"] = _sha256(evidence)
    verify_performance_certification(evidence)
    return evidence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--arm", choices=ARM_NAMES, help=argparse.SUPPRESS)
    parser.add_argument("--sandbox", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--socket", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--database", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.arm:
            if args.sandbox is None or args.socket is None or not args.database:
                raise PerformanceCertificationError("arm mode requires sandbox, socket, and database")
            payload = run_arm(
                args.arm,
                sandbox=args.sandbox.resolve(),
                socket_path=args.socket.resolve(),
                database=args.database,
                iterations=args.iterations,
            )
        else:
            if args.workdir is None:
                raise PerformanceCertificationError("--workdir is required")
            payload = run_performance_certification(
                args.workdir,
                iterations=args.iterations,
                repeats=args.repeats,
            )
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if args.output:
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(output.suffix + f".tmp-{os.getpid()}")
            temporary.write_text(encoded + "\n", encoding="utf-8")
            os.replace(temporary, output)
        print(encoded)
        return 0
    except (PerformanceCertificationError, OSError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

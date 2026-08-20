#!/usr/bin/env python3
"""Fail-closed offline actual-handler replay coverage for the pinned V2 surface.

The report accounts for every pinned route and route-method.  It executes only
reviewed handlers for which dependencies can be replaced with deterministic
in-memory fixtures.  A 401/404 dispatch result is never accepted as proof that
an actual handler ran.  Unreviewed, externally committing, destructive, and
fixture-incomplete methods remain explicit machine-readable gaps.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import io
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from collections import Counter
from contextlib import ExitStack, contextmanager, redirect_stdout
from importlib.machinery import PathFinder
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, Mapping, Sequence
from unittest.mock import patch
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parents[2]
while str(REPO_ROOT) in sys.path:
    sys.path.remove(str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT))
sys.dont_write_bytecode = True
for _module_name, _module in list(sys.modules.items()):
    if _module_name == "api" or _module_name.startswith("api."):
        _module_file = str(getattr(_module, "__file__", "") or "")
        if _module_file and not _module_file.startswith(str(REPO_ROOT)):
            sys.modules.pop(_module_name, None)

from scripts.v3_python_runtime_snapshot import (
    PythonRuntimeBlocked,
    verify_runtime_manifest,
)


FORMAL_RUNTIME_ENVIRONMENT_KEYS = (
    "MAGI_V3_PYTHON_RUNTIME",
    "MAGI_V3_PYTHON_RUNTIME_MANIFEST",
    "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256",
    "MAGI_V3_PYTHON_RUNTIME_REALPATH",
    "MAGI_V3_PYTHON_RUNTIME_SHA256",
    "MAGI_V3_PYTHON_RUNTIME_TREE_SHA256",
    "MAGI_V3_ROUTE_CERTIFYING",
)


class _FormalRuntimeBindingError(RuntimeError):
    pass


def _verify_formal_runtime_binding() -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Re-attest the sealed runtime before importing third-party handler code."""

    values = {name: os.environ.get(name, "").strip() for name in FORMAL_RUNTIME_ENVIRONMENT_KEYS}
    if values["MAGI_V3_ROUTE_CERTIFYING"] != "1" or any(
        not values[name]
        for name in FORMAL_RUNTIME_ENVIRONMENT_KEYS
        if name != "MAGI_V3_ROUTE_CERTIFYING"
    ):
        raise _FormalRuntimeBindingError("formal route runtime binding is incomplete")
    declared = Path(values["MAGI_V3_PYTHON_RUNTIME"])
    realpath = Path(values["MAGI_V3_PYTHON_RUNTIME_REALPATH"])
    manifest = Path(values["MAGI_V3_PYTHON_RUNTIME_MANIFEST"])
    if not all(path.is_absolute() for path in (declared, realpath, manifest)):
        raise _FormalRuntimeBindingError("formal route runtime paths must be absolute")
    try:
        manifest_bytes = manifest.read_bytes()
        if manifest.is_symlink() or hashlib.sha256(manifest_bytes).hexdigest() != values[
            "MAGI_V3_PYTHON_RUNTIME_MANIFEST_SHA256"
        ]:
            raise _FormalRuntimeBindingError(
                "formal route runtime manifest SHA-256 drifted"
            )
        payload = json.loads(manifest_bytes)
        report = verify_runtime_manifest(
            manifest,
            expected_tree_sha256=values["MAGI_V3_PYTHON_RUNTIME_TREE_SHA256"],
            expected_python_runtime=declared,
            expected_python_realpath=realpath,
        )
        if manifest.read_bytes() != manifest_bytes:
            raise _FormalRuntimeBindingError(
                "formal route runtime manifest changed during verification"
            )
        observed_python = Path(sys.executable).resolve(strict=True)
    except _FormalRuntimeBindingError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, PythonRuntimeBlocked) as exc:
        raise _FormalRuntimeBindingError(
            f"formal route runtime verification failed: {exc}"
        ) from exc
    if (
        observed_python != realpath.resolve(strict=True)
        or payload.get("python_runtime_sha256")
        != values["MAGI_V3_PYTHON_RUNTIME_SHA256"]
        or report.get("tree_sha256")
        != values["MAGI_V3_PYTHON_RUNTIME_TREE_SHA256"]
    ):
        raise _FormalRuntimeBindingError(
            "formal route Python executable or tree binding drifted"
        )
    return declared.absolute(), payload, report


if os.environ.get("MAGI_V3_ROUTE_CERTIFYING", "").strip() == "1":
    # Do this before Flask or any handler dependency can be imported from the
    # candidate runtime.  The nested worker repeats the same attestation.
    _verify_formal_runtime_binding()
    if __name__ == "__main__":
        # The certification CLI emits one machine-readable JSON document on
        # stdout.  Legacy NAS import probes can log expected offline warnings
        # to stderr even though Seatbelt blocks all network access; suppress
        # logging only in the isolated producer process so stderr remains an
        # unambiguous failure channel without muting the parent pytest run.
        logging.disable(logging.CRITICAL)

from flask import Flask

from magi_v3.compat.gateway import RouteInventory, verify_loaded_surface
from scripts.v3_validation.golden_flows import (
    BEHAVIOR_FIXTURE_ROOT,
    run_operational_golden_flows,
    run_osc_file_golden_flow,
)
from scripts.v3_validation.inventory import load_and_validate_runtime_inventory
from scripts.v3_validation.route_reviews import (
    RouteMethodKey,
    load_route_method_reviews,
)
from scripts.v3_validation.schema import ContractValidationError
from scripts.v3_source_contract import account_home


SCHEMA_VERSION = 1
SCRIPT_PATH = Path(__file__).resolve()
FIXTURE_ONLY_KEYS = {
    RouteMethodKey("5002", "/api/osc/chat", "POST", "web_runtime.osc_chat_api"),
    RouteMethodKey("5003", "/shortcut/pdf_text", "POST", "api_shortcut_pdf_text"),
}
BRANCH_CLASSES = frozenset({"validation_guard_only", "representative_success_path"})
GOLDEN_DOMAINS_REQUIRED = (
    "osc_file_preview_download",
    "tools_read_only_operations_and_audit",
    "nas_file_workflows",
    "office_document_workflows",
    "provider_and_session_integrations",
)
EXTERNAL_STORAGE_ACCESS_EVENT = "external_storage_access"


class ReplayIsolationError(ContractValidationError):
    """An offline replay attempted an operation outside its sandbox."""


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _route_key(value: Mapping[str, Any]) -> RouteMethodKey:
    return RouteMethodKey(
        str(value["service"]),
        str(value["rule"]),
        str(value["method"]),
        str(value["endpoint"]),
    )


def _key_dict(key: RouteMethodKey) -> dict[str, str]:
    return {
        "service": key.service,
        "rule": key.rule,
        "method": key.method,
        "endpoint": key.endpoint,
    }


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _is_lexically_within(path: Path, root: Path) -> bool:
    try:
        absolute_path = Path(os.path.abspath(os.fspath(path.expanduser())))
        absolute_root = Path(os.path.abspath(os.fspath(root.expanduser())))
        absolute_path.relative_to(absolute_root)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _external_storage_roots() -> tuple[Path, ...]:
    home = account_home()
    return (
        Path("/Volumes"),
        home / "Library" / "CloudStorage",
        home / ".magi_mounts",
        home / "SynologyDrive",
    )


def _install_worker_audit_guard(
    sandbox: Path,
    live_root: Path,
    *,
    allowed_live_read_roots: Sequence[Path] = (),
) -> dict[str, int]:
    attempts: Counter[str] = Counter()
    external_storage_roots = _external_storage_roots()
    write_mask = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event in {"socket.connect", "socket.bind", "subprocess.Popen", "os.system"}:
            attempts[event] += 1
            raise ReplayIsolationError(f"offline actual-handler replay blocked {event}")
        if event in {"sqlite3.connect", "os.chdir"} and args and isinstance(
            args[0], (str, bytes, os.PathLike)
        ):
            path = Path(os.fsdecode(args[0])).expanduser()
            if any(_is_lexically_within(path, root) for root in external_storage_roots):
                attempts[EXTERNAL_STORAGE_ACCESS_EVENT] += 1
                raise ReplayIsolationError(
                    f"offline actual-handler replay blocked external storage access: {path}"
                )
        if event in {"open", "os.listdir", "os.scandir"} and args and isinstance(
            args[0], (str, bytes, os.PathLike)
        ):
            path = Path(os.fsdecode(args[0])).expanduser()
            if any(_is_lexically_within(path, root) for root in external_storage_roots):
                attempts[EXTERNAL_STORAGE_ACCESS_EVENT] += 1
                raise ReplayIsolationError(
                    f"offline actual-handler replay blocked external storage access: {path}"
                )
            if event != "open":
                return
            mode = args[1] if len(args) > 1 else None
            flags = args[2] if len(args) > 2 else 0
            writes = (isinstance(mode, str) and any(token in mode for token in "wax+")) or (
                isinstance(flags, int) and bool(flags & write_mask)
            )
            if _is_within(path, live_root):
                if not writes and any(
                    _is_within(path, allowed_root)
                    for allowed_root in allowed_live_read_roots
                ):
                    return
                attempts["live_read_or_write"] += 1
                source = " > ".join(
                    f"{Path(frame.filename).name}:{frame.lineno}"
                    for frame in traceback.extract_stack(limit=7)[:-1]
                )
                raise ReplayIsolationError(
                    f"offline actual-handler replay blocked live MAGI state access: {path}; "
                    f"event={event}; stack={source}"
                )
            if writes and not _is_within(path, sandbox):
                attempts["write_outside_sandbox"] += 1
                raise ReplayIsolationError(f"offline actual-handler replay blocked write outside sandbox: {path}")
        if event in {"os.remove", "os.rename", "os.rmdir", "os.mkdir"} and args:
            path = Path(os.fsdecode(args[0])).expanduser()
            if not _is_within(path, sandbox):
                attempts["mutation_outside_sandbox"] += 1
                source = " > ".join(
                    f"{Path(frame.filename).name}:{frame.lineno}" for frame in traceback.extract_stack(limit=7)[:-1]
                )
                raise ReplayIsolationError(
                    f"offline actual-handler replay blocked {event} outside sandbox: {path}; stack={source}"
                )

    sys.addaudithook(audit)
    return attempts


def _blocked_operation(name: str, attempts: Counter[str]):
    def blocked(*_args: Any, **_kwargs: Any) -> None:
        attempts[name] += 1
        raise ReplayIsolationError(f"offline actual-handler replay blocked {name}")

    return blocked


def _isolation_attempt_snapshot(
    audit_attempts: Mapping[str, int],
    blocked_attempts: Mapping[str, int],
) -> dict[str, int]:
    return {
        "network_attempts": sum(
            count for name, count in blocked_attempts.items() if name.startswith("socket.")
        )
        + int(audit_attempts.get("socket.connect", 0))
        + int(audit_attempts.get("socket.bind", 0)),
        "subprocess_attempts": int(blocked_attempts.get("subprocess.Popen", 0))
        + int(audit_attempts.get("subprocess.Popen", 0)),
        "live_state_attempts": int(audit_attempts.get("live_read_or_write", 0)),
        "writes_outside_sandbox": int(audit_attempts.get("write_outside_sandbox", 0)),
        "mutations_outside_sandbox": int(audit_attempts.get("mutation_outside_sandbox", 0)),
        "external_storage_access_attempts": int(
            audit_attempts.get(EXTERNAL_STORAGE_ACCESS_EVENT, 0)
        ),
    }


def _fake_module(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


class _FakeCursor:
    def __init__(self) -> None:
        self.executions: list[tuple[str, Any]] = []

    def execute(self, statement: str, params: Any = None) -> None:
        self.executions.append((statement, params))

    def fetchall(self) -> list[dict[str, Any]]:
        return []


@contextmanager
def _fake_cursor(**_kwargs: Any) -> Iterator[tuple[object, _FakeCursor]]:
    yield object(), _FakeCursor()


def _json_exact(expected: Any):
    def validate(response: Any) -> tuple[bool, dict[str, Any]]:
        observed = response.get_json(silent=True)
        return observed == expected, {"kind": "json_exact", "expected": expected, "observed": observed}

    return validate


def _json_subset(expected: Mapping[str, Any]):
    def validate(response: Any) -> tuple[bool, dict[str, Any]]:
        observed = response.get_json(silent=True)
        passed = isinstance(observed, dict) and all(observed.get(key) == value for key, value in expected.items())
        return passed, {"kind": "json_subset", "expected": dict(expected), "observed": observed}

    return validate


def _json_deep_subset(expected: Mapping[str, Any]):
    def contains(observed: Any, wanted: Any) -> bool:
        if isinstance(wanted, Mapping):
            return isinstance(observed, Mapping) and all(
                key in observed and contains(observed[key], value)
                for key, value in wanted.items()
            )
        return observed == wanted

    def validate(response: Any) -> tuple[bool, dict[str, Any]]:
        observed = response.get_json(silent=True)
        return contains(observed, expected), {
            "kind": "json_deep_subset",
            "expected": dict(expected),
            "observed": observed,
        }

    return validate


def _body_contains(*needles: str):
    def validate(response: Any) -> tuple[bool, dict[str, Any]]:
        text = response.get_data(as_text=True)
        passed = all(needle in text for needle in needles)
        return passed, {"kind": "body_contains", "expected": list(needles), "observed_length": len(text)}

    return validate


def _redirect_exact(location: str):
    def validate(response: Any) -> tuple[bool, dict[str, Any]]:
        observed = str(response.headers.get("Location") or "")
        return observed == location, {"kind": "redirect_exact", "expected": location, "observed": observed}

    return validate


def _definitions_contract(response: Any) -> tuple[bool, dict[str, Any]]:
    observed = response.get_json(silent=True)
    passed = (
        isinstance(observed, dict)
        and set(observed) == {"_meta", "tools"}
        and isinstance(observed["tools"], (dict, list))
        and bool(observed["tools"])
    )
    return passed, {
        "kind": "definitions_shape",
        "expected_top_level_keys": ["_meta", "tools"],
        "observed_top_level_keys": sorted(observed) if isinstance(observed, dict) else None,
        "observed_tool_count": len(observed.get("tools", {})) if isinstance(observed, dict) else None,
    }


def _livez_contract(response: Any) -> tuple[bool, dict[str, Any]]:
    observed = response.get_json(silent=True)
    passed = (
        isinstance(observed, dict)
        and observed.get("ok") is True
        and observed.get("status") == "live"
        and observed.get("probe") == "liveness"
        and observed.get("readiness_checked") is False
        and isinstance(observed.get("timestamp"), (int, float))
        and isinstance(observed.get("uptime_seconds"), (int, float))
        and "checks" not in observed
    )
    return passed, {
        "kind": "admin_livez_shape",
        "expected": "process-only liveness payload without readiness checks",
        "observed_keys": sorted(observed) if isinstance(observed, dict) else None,
    }


def _invoke_case(
    app: Flask,
    *,
    service: str,
    rule: str,
    method: str,
    endpoint: str,
    path: str,
    expected_status: int,
    validator: Any,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    data: Any = None,
    content_type: str | None = None,
    branch_class: str | None = None,
) -> dict[str, Any]:
    if json_body is not None and data is not None:
        raise ContractValidationError("actual route replay case cannot send both JSON and form data")
    request_path = urlsplit(path).path
    matched_endpoint, _values = app.url_map.bind("localhost").match(request_path, method=method)
    if matched_endpoint != endpoint:
        raise ContractValidationError(
            f"actual route dispatch drift for {service} {method} {path}: expected={endpoint} observed={matched_endpoint}"
        )
    response = app.test_client().open(
        path,
        method=method,
        headers=dict(headers or {}),
        json=json_body,
        data=data,
        content_type=content_type,
    )
    contract_passed, projection = validator(response)
    status_dispatch_proof = response.status_code not in {401, 404}
    passed = response.status_code == expected_status and contract_passed and status_dispatch_proof
    body = response.get_data()
    resolved_branch_class = branch_class or (
        "validation_guard_only" if expected_status >= 400 else "representative_success_path"
    )
    if resolved_branch_class not in BRANCH_CLASSES:
        raise ContractValidationError(
            f"actual route replay case has invalid branch class: {resolved_branch_class}"
        )
    return {
        "service": service,
        "rule": rule,
        "method": method,
        "endpoint": endpoint,
        "request_path": path,
        "actual_handler_dispatched": matched_endpoint == endpoint,
        "auth_or_not_found_status_used_as_proof": response.status_code in {401, 404},
        "expected_status": expected_status,
        "observed_status": response.status_code,
        "content_type": str(response.headers.get("Content-Type") or ""),
        "response_body_sha256": _digest(body),
        "contract": projection,
        "branch_class": resolved_branch_class,
        "representative_success_path": resolved_branch_class == "representative_success_path",
        "validation_guard_only": resolved_branch_class == "validation_guard_only",
        "passed": passed,
    }


def _admin_livez_case(sandbox: Path) -> dict[str, Any]:
    from api.blueprints.admin_runtime import create_admin_runtime_blueprint

    app = Flask("v3-actual-route-replay-admin")
    app.config.update(TESTING=True, SECRET_KEY="offline-route-replay")
    app.register_blueprint(
        create_admin_runtime_blueprint(
            logger=app.logger,
            orchestrator=object(),
            require_json_auth=lambda admin=False: None,
            list_skill_docs=lambda: [],
            nerv_skill_interview_user_id=lambda: "nerv:offline",
            extract_interview_skill_name=lambda _message: "",
            skill_doc_path=lambda name: sandbox / "skills" / name / "SKILL.md",
            skill_action_path=lambda name: sandbox / "skills" / name / "action.py",
            skill_summary=lambda content: str(content or "").strip(),
            nerv_product_runtime_payload=lambda: {"ok": True},
            nerv_product_names=(),
            update_product_runtime=lambda product, **updates: updates,
            cloudflared_alive=lambda: False,
            server_start_time=1_699_999_990.0,
            attachment_job_queue=None,
            list_attachment_job_ids=lambda: [],
            read_attachment_job=lambda _job_id: {},
            expected_magi_api_key="offline-route-replay",
            db_config={"host": "blocked.invalid", "user": "offline", "password": "offline"},
            mysql_connector=object(),
            safe_remove_tmp=lambda _path: None,
            magi_root=sandbox,
        )
    )
    return _invoke_case(
        app,
        service="5002",
        rule="/livez",
        method="GET",
        endpoint="admin_runtime.livez",
        path="/livez",
        expected_status=200,
        validator=_livez_contract,
    )


def _admin_in_memory_cases(
    sandbox: Path,
    *,
    audit_attempts: Mapping[str, int],
    blocked_attempts: Mapping[str, int],
) -> list[dict[str, Any]]:
    import time
    from flask_login import AnonymousUserMixin, LoginManager

    from api.blueprints.admin_runtime import create_admin_runtime_blueprint

    class _OfflineAdminOrchestrator:
        def get_skill_interview_state(self, _user_id: str, _scope: str) -> dict[str, Any]:
            return {"active": False, "fixture": "in-memory"}

    class _FalseAdminFlag:
        def __call__(self) -> bool:
            return False

        def __bool__(self) -> bool:
            return False

    class _OfflineAnonymousUser(AnonymousUserMixin):
        is_admin = _FalseAdminFlag()

    status_fixture = {"ok": True, "status": "offline-fixture"}
    (sandbox / "static").mkdir(parents=True, exist_ok=True)
    agent_fixture_dir = Path(
        os.environ.get("MAGI_AGENT_DIR", "").strip() or sandbox / ".agent"
    ).expanduser()
    agent_fixture_dir.mkdir(parents=True, exist_ok=True)
    (sandbox / "static" / "magi_status.json").write_text(json.dumps(status_fixture), encoding="utf-8")
    (agent_fixture_dir / "server.log").write_text(
        "offline line one\noffline line two\n",
        encoding="utf-8",
    )

    app = Flask("v3-actual-route-replay-admin-in-memory")
    app.config.update(
        TESTING=True,
        SECRET_KEY="offline-route-replay",
        LOGIN_DISABLED=True,
    )
    login_manager = LoginManager(app)
    login_manager.user_loader(lambda _user_id: None)
    login_manager.anonymous_user = _OfflineAnonymousUser
    app.register_blueprint(
        create_admin_runtime_blueprint(
            logger=app.logger,
            orchestrator=_OfflineAdminOrchestrator(),
            require_json_auth=lambda admin=False: None,
            list_skill_docs=lambda: [],
            nerv_skill_interview_user_id=lambda: "nerv:offline",
            extract_interview_skill_name=lambda _message: "",
            skill_doc_path=lambda name: sandbox / "skills" / name / "SKILL.md",
            skill_action_path=lambda name: sandbox / "skills" / name / "action.py",
            skill_summary=lambda content: str(content or "").strip(),
            nerv_product_runtime_payload=lambda: {"ok": True, "fixture": "in-memory"},
            nerv_product_names=(),
            update_product_runtime=lambda product, **updates: updates,
            cloudflared_alive=lambda: False,
            server_start_time=1_699_999_990.0,
            attachment_job_queue=None,
            list_attachment_job_ids=lambda: [],
            read_attachment_job=lambda _job_id: {},
            expected_magi_api_key="offline-route-replay",
            db_config={"host": "blocked.invalid", "user": "offline", "password": "offline"},
            mysql_connector=object(),
            safe_remove_tmp=lambda _path: None,
            magi_root=sandbox,
        )
    )
    health_view = app.view_functions["admin_runtime.nerv_api_health"]
    health_handler = getattr(health_view, "__wrapped__", health_view)
    health_cells = dict(zip(health_handler.__code__.co_freevars, health_handler.__closure__ or ()))
    health_cache = health_cells["nerv_health_cache"].cell_contents
    health_cache.clear()
    health_cache.update(
        {
            "ts": time.monotonic(),
            "payload": {
                "status": "offline-fixture",
                "probe": "nerv_health",
                "services": {"fixture": {"status": "offline"}},
            },
        }
    )
    remote_view = app.view_functions["admin_runtime.api_nerv_remote_access"]
    remote_cells = dict(zip(remote_view.__code__.co_freevars, remote_view.__closure__ or ()))
    remote_cells["_remote_access_payload"].cell_contents = lambda: {
        "ok": True,
        "hostname": "offline-host",
        "google_remote_desktop": {"status": "offline", "configured": False},
        "tailscale": {"status": "offline", "ip": "", "dns_name": ""},
        "screen_sharing": {"status": "manual", "running": False, "vnc_url": ""},
        "cloudflare": {"status": "offline", "url": ""},
        "policy": {"public_vnc_exposed": False, "message": "offline synthetic fixture"},
    }
    live_validation_view = app.view_functions["admin_runtime.api_live_validation"]
    live_validation_handler = getattr(
        live_validation_view,
        "__wrapped__",
        live_validation_view,
    )
    live_validation_cells = dict(
        zip(
            live_validation_handler.__code__.co_freevars,
            live_validation_handler.__closure__ or (),
        )
    )
    live_validation_cells["_collect_process_markers"].cell_contents = lambda: {
        "daemon": {"ok": True, "markers": ["offline-daemon"]},
        "server": {"ok": True, "markers": ["offline-server"], "pid": 1},
    }
    for collector_name, payload in {
        "_collect_tools_api_status": {"ok": True, "status": "ok", "count": 0},
        "_collect_nas_mount_status": {"ok": True, "mounts": {}},
        "_collect_drive_sync_status": {"ok": True, "status": "idle"},
        "_collect_db_status": {"ok": True},
        "_collect_model_status": {"ok": True, "status": "ok", "port": 8080},
    }.items():
        live_validation_cells[collector_name].cell_contents = (
            lambda value=dict(payload): dict(value)
        )
    live_validation_cells["_attach_runtime_diagnostics"].cell_contents = (
        lambda _name, payload: dict(payload)
    )
    specs = (
        (
            "/api/nerv/skills",
            "admin_runtime.api_nerv_skills",
            "/api/nerv/skills",
            {"ok": True, "skills": []},
        ),
        (
            "/api/nerv/product-runtime",
            "admin_runtime.api_nerv_product_runtime",
            "/api/nerv/product-runtime",
            {"ok": True, "fixture": "in-memory"},
        ),
        (
            "/api/nerv/skill-interview",
            "admin_runtime.api_nerv_skill_interview_status",
            "/api/nerv/skill-interview",
            {
                "ok": True,
                "can_edit": False,
                "interview": {"active": False, "fixture": "in-memory"},
            },
        ),
        (
            "/api/skills/interview-history",
            "admin_runtime.api_skill_interview_history",
            "/api/skills/interview-history?limit=7",
            {"ok": True, "history": []},
        ),
        (
            "/api/skills/<skill_name>/versions",
            "admin_runtime.api_skill_versions",
            "/api/skills/offline-skill/versions",
            {"ok": True, "versions": [{"id": "offline-v1", "fixture": True}]},
        ),
        (
            "/api/codex-distributed/status",
            "admin_runtime.api_codex_distributed_status",
            "/api/codex-distributed/status",
            {"status": {"mode": "offline", "fixture": True}, "can_toggle": False},
        ),
        (
            "/api/drive-case-exclusions",
            "admin_runtime.api_drive_case_exclusions_list",
            "/api/drive-case-exclusions",
            {"ok": True, "count": 1, "relative_paths": ["offline/case"]},
        ),
        (
            "/api/live-log",
            "admin_runtime.api_live_log",
            "/api/live-log?limit=1",
            {"lines": ["offline line two"]},
        ),
        (
            "/api/nerv/heavy-runtime",
            "admin_runtime.api_nerv_heavy_runtime",
            "/api/nerv/heavy-runtime",
            {
                "ok": True,
                "can_edit": False,
                "env_path": str(Path(os.environ.get("MAGI_ENV_FILE") or sandbox / ".env")),
                "enabled": False,
                "configured": False,
                "masked": "",
                "env_key": "NVIDIA_NIM_API_KEY",
                "enable_key": "NVIDIA_NIM_ENABLE",
                "command_prefixes": ["@heavy", "@重型"],
                "description": "HEAVY 任務固定使用 NVIDIA NIM API；未啟用或 API 不可用時明確失敗，不會改由本機模型代跑。",
            },
        ),
        (
            "/api/nerv/skills/<skill_name>",
            "admin_runtime.api_nerv_skill_detail",
            "/api/nerv/skills/offline-missing",
            {
                "ok": True,
                "skill": {
                    "name": "offline-missing",
                    "content": "",
                    "has_skill_doc": False,
                    "has_action": False,
                    "updated_at": "",
                    "summary": "",
                },
            },
        ),
        ("/api/status", "admin_runtime.api_status", "/api/status", status_fixture),
        (
            "/health",
            "admin_runtime.health",
            "/health",
            _json_subset(
                {
                    "ok": True,
                    "status": "live",
                    "probe": "liveness",
                    "readiness": "/readyz",
                }
            ),
        ),
        (
            "/readyz",
            "admin_runtime.readyz",
            "/readyz?scope=saas",
            _json_subset({"ok": True, "status": "ready", "probe": "saas_readiness"}),
        ),
        (
            "/saas-readyz",
            "admin_runtime.saas_readyz",
            "/saas-readyz",
            _json_subset({"ok": True, "status": "ready", "probe": "saas_readiness"}),
        ),
        (
            "/dashboard/nerv/api/health",
            "admin_runtime.nerv_api_health",
            "/dashboard/nerv/api/health",
            _json_deep_subset(
                {
                    "status": "offline-fixture",
                    "probe": "nerv_health",
                    "services": {"fixture": {"status": "offline"}},
                    "cached": True,
                }
            ),
        ),
        (
            "/status/api/health",
            "admin_runtime.nerv_api_health",
            "/status/api/health",
            _json_deep_subset(
                {
                    "status": "offline-fixture",
                    "probe": "nerv_health",
                    "services": {"fixture": {"status": "offline"}},
                    "cached": True,
                }
            ),
        ),
        (
            "/api/nerv/remote-access",
            "admin_runtime.api_nerv_remote_access",
            "/api/nerv/remote-access",
            {
                "ok": True,
                "hostname": "offline-host",
                "google_remote_desktop": {"status": "offline", "configured": False},
                "tailscale": {"status": "offline", "ip": "", "dns_name": ""},
                "screen_sharing": {"status": "manual", "running": False, "vnc_url": ""},
                "cloudflare": {"status": "offline", "url": ""},
                "policy": {"public_vnc_exposed": False, "message": "offline synthetic fixture"},
            },
        ),
        (
            "/api/live-validation",
            "admin_runtime.api_live_validation",
            "/api/live-validation",
            _json_deep_subset(
                {
                    "daemon": {"ok": True},
                    "server": {"ok": True},
                    "tools_api": {"ok": True},
                    "nas": {"ok": True},
                    "drive": {"ok": True},
                    "db": {"ok": True},
                    "model": {"ok": True},
                    "summary": {"ok": True, "status": "operational", "issues": []},
                    "status": "operational",
                }
            ),
        ),
    )

    cases: list[dict[str, Any]] = []
    admin_fake_modules = {
        "skills.bridge.llm_direct": _fake_module(
            "skills.bridge.llm_direct",
            public_status_report=lambda: {"mode": "offline", "fixture": True},
        ),
        "api.osc.drive_case_sync": _fake_module(
            "api.osc.drive_case_sync",
            load_case_exclusion_payload=lambda **_kwargs: {"relative_paths": ["offline/case"]},
        ),
        "api.saas_readiness": _fake_module(
            "api.saas_readiness",
            build_saas_readiness=lambda **_kwargs: {
                "ok": True,
                "status": "ready",
                "fixture": "saas-readiness",
            },
        ),
    }
    with patch.dict(sys.modules, admin_fake_modules):
        for rule, endpoint, path, expected in specs:
            before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            case = _invoke_case(
                app,
                service="5002",
                rule=rule,
                method="GET",
                endpoint=endpoint,
                path=path,
                expected_status=200,
                validator=expected if callable(expected) else _json_exact(expected),
            )
            after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            delta = {key: after[key] - before[key] for key in before}
            if any(delta.values()):
                raise ReplayIsolationError(f"admin in-memory replay crossed an isolation boundary for {endpoint}: {delta}")
            case["side_effect_guard"] = {
                "fixture_database_calls": 0,
                "statement_kinds": [],
                "database_mutations": 0,
                "fixture": "in_memory_dependencies",
                **delta,
            }
            cases.append(case)
    return cases


def _dashboard_projection_cases(
    dashboard_pages: ModuleType,
    *,
    audit_attempts: Mapping[str, int],
    blocked_attempts: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Dispatch pure dashboard projections with all dynamic inputs bound in memory."""

    from flask_login import LoginManager

    app = Flask("v3-actual-route-replay-dashboard-projections")
    app.config.update(TESTING=True, SECRET_KEY="offline-route-replay", LOGIN_DISABLED=True)
    login_manager = LoginManager(app)
    login_manager.user_loader(lambda _user_id: None)
    app.register_blueprint(dashboard_pages.dashboard_pages_bp)

    mobile_config = {
        "routes": [
            {"label": "Home", "path": "/mobile", "kind": "core"},
            {"label": "Admin", "path": "/mobile-admin", "kind": "admin"},
        ]
    }
    mobile_manifest = {
        "name": "MAGI Mobile",
        "short_name": "MAGI",
        "description": "可直接對話、查詢與執行工作的 MAGI 行動助理",
        "id": "/mobile",
        "start_url": "/mobile",
        "scope": "/mobile",
        "display": "standalone",
        "orientation": "portrait",
        "theme_color": "#07141f",
        "background_color": "#07111b",
        "icons": [
            {
                "src": "/static/mobile/magi-mobile.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
        "shortcuts": [
            {"name": "Home", "url": "/mobile"},
            {"name": "Admin", "url": "/mobile-admin"},
        ],
    }
    specs = (
        ("/static/worldmonitor_reports", "dashboard_pages.worldmonitor_reports_redirect", 302, _redirect_exact("/intel")),
        ("/static/worldmonitor_reports/", "dashboard_pages.worldmonitor_reports_redirect", 302, _redirect_exact("/intel")),
        ("/worldmonitor", "dashboard_pages.worldmonitor_entry", 302, _redirect_exact("/intel")),
        ("/worldmonitor/", "dashboard_pages.worldmonitor_entry", 302, _redirect_exact("/intel")),
        ("/dashboard", "dashboard_pages.dashboard", 302, _redirect_exact("/dashboard/golem")),
        ("/dashboard/legacy", "dashboard_pages.dashboard_legacy", 302, _redirect_exact("/dashboard/golem")),
        (
            "/research/judgment-classifier",
            "dashboard_pages.research_judgment_classifier",
            200,
            _body_contains("offline-template:research_judgment_classifier.html"),
        ),
        ("/dashboard/nerv", "dashboard_pages.magi_adjust", 200, _body_contains("offline-template:dashboard_nerv.html")),
        ("/nerv", "dashboard_pages.magi_adjust", 200, _body_contains("offline-template:dashboard_nerv.html")),
        ("/magi-adjust", "dashboard_pages.magi_adjust", 200, _body_contains("offline-template:dashboard_nerv.html")),
        ("/magi-settings", "dashboard_pages.magi_adjust", 200, _body_contains("offline-template:dashboard_nerv.html")),
        ("/golem", "dashboard_pages.golem_console", 200, _body_contains("offline-template:golem_console.html")),
        (
            "/dashboard/golem",
            "dashboard_pages.golem_console",
            200,
            _body_contains("offline-template:golem_console.html"),
        ),
        ("/mobile/manifest.webmanifest", "dashboard_pages.mobile_manifest", 200, _json_exact(mobile_manifest)),
        ("/mobile/sw.js", "dashboard_pages.mobile_service_worker", 200, _body_contains("MAGI_MOBILE_CACHE", "shouldSkipCache")),
        ("/app", "dashboard_pages.mobile_home", 200, _body_contains("offline-template:mobile_home.html")),
        ("/mobile", "dashboard_pages.mobile_home", 200, _body_contains("offline-template:mobile_home.html")),
        ("/app-admin", "dashboard_pages.mobile_admin", 200, _body_contains("offline-template:mobile_admin.html")),
        ("/mobile-admin", "dashboard_pages.mobile_admin", 200, _body_contains("offline-template:mobile_admin.html")),
        (
            "/dashboard/beginner",
            "dashboard_pages.dashboard_beginner",
            200,
            _body_contains("offline-template:dashboard_beginner.html"),
        ),
        ("/start", "dashboard_pages.dashboard_beginner", 200, _body_contains("offline-template:dashboard_beginner.html")),
        (
            "/dashboard/status",
            "dashboard_pages.status_center",
            200,
            _body_contains("offline-template:dashboard_beginner.html"),
        ),
        ("/status", "dashboard_pages.status_center", 200, _body_contains("offline-template:dashboard_beginner.html")),
        (
            "/dashboard/website",
            "dashboard_pages.dashboard_website",
            200,
            _body_contains("offline-template:dashboard_website.html"),
        ),
        ("/magi-research", "dashboard_pages.research_panel", 200, _body_contains("offline-template:research.html")),
        ("/research", "dashboard_pages.research_panel", 200, _body_contains("offline-template:research.html")),
        ("/intel", "dashboard_pages.intel_panel", 200, _body_contains("offline-template:intel.html")),
        ("/mobile/config.json", "dashboard_pages.mobile_config_json", 200, _json_exact(mobile_config)),
        (
            "/research/rss-preview",
            "dashboard_pages.research_rss_preview",
            200,
            _body_contains("offline-template:rss_preview.html"),
        ),
    )

    cases: list[dict[str, Any]] = []
    with patch.object(
        dashboard_pages,
        "render_template",
        lambda template, **_kwargs: f"offline-template:{template}",
    ), patch.object(
        dashboard_pages,
        "_build_mobile_app_config",
        lambda: mobile_config,
    ), patch.object(
        dashboard_pages,
        "_build_beginner_dashboard",
        lambda: {"fixture": "beginner"},
    ), patch.object(
        dashboard_pages,
        "_build_status_dashboard",
        lambda: {"fixture": "status"},
    ), patch.object(
        dashboard_pages,
        "_load_research_dashboard",
        lambda: {
            "fixture": "research",
            "namespaces": [
                {
                    "sources": [
                        {"url": "https://offline.invalid/feed.xml", "is_feed": True},
                    ]
                }
            ],
        },
    ), patch.object(
        dashboard_pages,
        "_fetch_research_feed",
        lambda url: {"title": "Offline Feed", "source_url": url, "items": []},
    ), patch.object(
        dashboard_pages,
        "_iter_worldmonitor_reports",
        lambda: [],
    ):
        for rule, endpoint, expected_status, validator in specs:
            before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            case = _invoke_case(
                app,
                service="5002",
                rule=rule,
                method="GET",
                endpoint=endpoint,
                path=(
                    "/research/rss-preview?url=https%3A%2F%2Foffline.invalid%2Ffeed.xml"
                    if rule == "/research/rss-preview"
                    else rule
                ),
                expected_status=expected_status,
                validator=validator,
            )
            after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            delta = {key: after[key] - before[key] for key in before}
            if any(delta.values()):
                raise ReplayIsolationError(
                    f"dashboard projection replay crossed an isolation boundary for {endpoint}: {delta}"
                )
            case["side_effect_guard"] = {
                "fixture_database_calls": 0,
                "statement_kinds": [],
                "database_mutations": 0,
                "fixture": "in_memory_dependencies",
                **delta,
            }
            cases.append(case)
    proxy_calls: list[dict[str, Any]] = []

    class _OfflineProxyRaw:
        headers = {"Content-Type": "text/plain; charset=utf-8", "X-Offline-Fixture": "1"}

    class _OfflineProxyResponse:
        def __init__(self, body: bytes) -> None:
            self.content = body
            self.status_code = 200
            self.raw = _OfflineProxyRaw()

    def _proxy_get(url: str, **kwargs: Any) -> _OfflineProxyResponse:
        proxy_calls.append({"method": "GET", "url": url, "kwargs": sorted(kwargs)})
        return _OfflineProxyResponse(f"offline-website:GET:{url}".encode("utf-8"))

    def _proxy_post(url: str, **kwargs: Any) -> _OfflineProxyResponse:
        proxy_calls.append({"method": "POST", "url": url, "kwargs": sorted(kwargs)})
        return _OfflineProxyResponse(f"offline-website:POST:{url}".encode("utf-8"))

    with patch.object(dashboard_pages._requests, "get", _proxy_get), patch.object(
        dashboard_pages._requests,
        "post",
        _proxy_post,
    ):
        proxy_specs = (
            ("/wa/", "GET", "/wa/", "offline-website:GET:http://127.0.0.1:8088/"),
            (
                "/wa/<path:path>",
                "GET",
                "/wa/offline-path",
                "offline-website:GET:http://127.0.0.1:8088/offline-path",
            ),
            (
                "/wa/<path:path>",
                "POST",
                "/wa/offline-path",
                "offline-website:POST:http://127.0.0.1:8088/offline-path",
            ),
        )
        for rule, method, path, expected_body in proxy_specs:
            before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            call_count = len(proxy_calls)
            case = _invoke_case(
                app,
                service="5002",
                rule=rule,
                method=method,
                endpoint="dashboard_pages.website_admin_proxy",
                path=path,
                expected_status=200,
                validator=_body_contains(expected_body),
                json_body={"fixture": True} if method == "POST" else None,
            )
            after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            delta = {key: after[key] - before[key] for key in before}
            if any(delta.values()) or len(proxy_calls) != call_count + 1:
                raise ReplayIsolationError(
                    f"website proxy replay escaped its in-memory upstream for {method} {rule}: {delta}"
                )
            case["side_effect_guard"] = {
                "fixture_database_calls": 0,
                "statement_kinds": [],
                "database_mutations": 0,
                "fixture": "in_memory_http_upstream",
                "upstream_method": method,
                "upstream_url": proxy_calls[-1]["url"],
                **delta,
            }
            cases.append(case)
    return cases


def _web_runtime_isolated_cases(
    web_runtime: ModuleType,
    sandbox: Path,
    *,
    audit_attempts: Mapping[str, int],
    blocked_attempts: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Dispatch web-runtime GETs against sandbox-only roots and in-memory state."""

    from flask_login import AnonymousUserMixin, LoginManager

    class _OfflineWebUser(AnonymousUserMixin):
        id = "offline-user"
        role = "operator"

    class _OfflineWebOrchestrator:
        def process_message(
            self,
            *,
            user_id: str,
            message: str,
            platform: str,
            role: str,
        ) -> str:
            if (user_id, message, platform, role) != (
                "offline-user",
                "offline fixture ping",
                "WEB",
                "operator",
            ):
                raise ContractValidationError("web chat fixture request binding drifted")
            return "offline fixture reply"

    notifications = {"offline-user": [{"kind": "fixture", "message": "offline"}]}
    app = Flask("v3-actual-route-replay-web-runtime-isolated")
    app.config.update(TESTING=True, SECRET_KEY="offline-route-replay", LOGIN_DISABLED=True)
    login_manager = LoginManager(app)
    login_manager.user_loader(lambda _user_id: None)
    login_manager.anonymous_user = _OfflineWebUser
    app.register_blueprint(
        web_runtime.create_web_runtime_blueprint(
            orchestrator=_OfflineWebOrchestrator(),
            logger=app.logger,
            web_notifications=notifications,
            normalize_output_text=lambda value, **_kwargs: str(value or ""),
            magi_root=sandbox,
        )
    )
    process_fixture = {
        "ok": True,
        "ts": "2026-07-15T00:00:00+00:00",
        "summary": {
            "core_count": 0,
            "worker_count": 0,
            "orphan_count": 0,
            "duplicate_groups": 0,
        },
        "core": [],
        "workers": [],
        "orphans": [],
        "duplicates": [],
        "guardian_state": {},
    }
    specs = (
        (
            "/api/memory/stats",
            "web_runtime.api_memory_stats",
            {
                "doc_count": 0,
                "source_count": 0,
                "last_ingest": None,
                "obsidian": {},
                "faiss_size": 0,
            },
        ),
        (
            "/api/osc/judgments_legacy",
            "web_runtime.osc_judgments_api",
            [],
        ),
        (
            "/api/osc/poll",
            "web_runtime.osc_poll_api",
            {"messages": [{"kind": "fixture", "message": "offline"}]},
        ),
        (
            "/api/ops/process-monitor",
            "web_runtime.process_monitor_api",
            {**process_fixture, "guardian_control_enabled": True},
        ),
    )
    cases: list[dict[str, Any]] = []
    with patch.object(
        web_runtime,
        "_collect_process_monitor",
        lambda **_kwargs: dict(process_fixture),
    ), patch.object(
        web_runtime,
        "render_template",
        lambda template, **_kwargs: f"offline-template:{template}",
    ):
        for rule, endpoint, expected in specs:
            before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            case = _invoke_case(
                app,
                service="5002",
                rule=rule,
                method="GET",
                endpoint=endpoint,
                path=rule,
                expected_status=200,
                validator=_json_exact(expected),
            )
            after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            delta = {key: after[key] - before[key] for key in before}
            if any(delta.values()):
                raise ReplayIsolationError(
                    f"web-runtime replay crossed an isolation boundary for {endpoint}: {delta}"
                )
            case["side_effect_guard"] = {
                "fixture_database_calls": 0,
                "statement_kinds": [],
                "database_mutations": 0,
                "fixture": "in_memory_dependencies",
                **delta,
            }
            cases.append(case)
        before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
        page_case = _invoke_case(
            app,
            service="5002",
            rule="/ops/process-monitor",
            method="GET",
            endpoint="web_runtime.process_monitor_page",
            path="/ops/process-monitor",
            expected_status=200,
            validator=_body_contains("offline-template:process_monitor.html"),
        )
        after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
        delta = {key: after[key] - before[key] for key in before}
        if any(delta.values()):
            raise ReplayIsolationError(
                "web-runtime replay crossed an isolation boundary for process_monitor_page: "
                f"{delta}"
            )
        page_case["side_effect_guard"] = {
            "fixture_database_calls": 0,
            "statement_kinds": [],
            "database_mutations": 0,
            "fixture": "in_memory_dependencies",
            **delta,
        }
        cases.append(page_case)
        before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
        chat_case = _invoke_case(
            app,
            service="5002",
            rule="/api/osc/chat",
            method="POST",
            endpoint="web_runtime.osc_chat_api",
            path="/api/osc/chat",
            expected_status=200,
            validator=_json_deep_subset(
                {
                    "reply": "offline fixture reply",
                    "reply_html": '<div class="web-reply"><p>offline fixture reply</p></div>',
                    "artifacts": [],
                }
            ),
            json_body={"message": "offline fixture ping"},
            branch_class="representative_success_path",
        )
        after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
        delta = {key: after[key] - before[key] for key in before}
        if any(delta.values()):
            raise ReplayIsolationError(
                "web chat fixture crossed an isolation boundary: " f"{delta}"
            )
        chat_case["side_effect_guard"] = {
            "fixture_database_calls": 0,
            "statement_kinds": [],
            "database_mutations": 0,
            "fixture": "in_memory_orchestrator",
            **delta,
        }
        cases.append(chat_case)
    return cases


def _iron_dome_read_only_cases(
    iron_dome_sync: ModuleType,
    *,
    audit_attempts: Mapping[str, int],
    blocked_attempts: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Dispatch Iron Dome GET projections with every dynamic source in memory."""

    app = Flask("v3-actual-route-replay-iron-dome")
    app.config.update(TESTING=True, SECRET_KEY="offline-route-replay")
    iron_dome_sync.register_iron_dome_routes(app)
    specs = (
        (
            "/api/iron_dome/hash",
            "iron_dome_hash",
            {"hash": "offline-pattern-hash", "node": "offline-node"},
        ),
        (
            "/api/iron_dome/patterns",
            "iron_dome_patterns",
            {
                "version": 1,
                "node": "offline-node",
                "prompt_injection": ["offline-injection"],
                "dangerous_commands": ["offline-danger"],
            },
        ),
        (
            "/api/iron_dome/status",
            "iron_dome_status",
            {
                "local_node": "offline-node",
                "local_hash": "offline-pattern-hash",
                "nodes": {},
            },
        ),
    )
    cases: list[dict[str, Any]] = []
    with patch.object(iron_dome_sync, "CURRENT_NODE", "offline-node"), patch.object(
        iron_dome_sync,
        "get_patterns_hash",
        lambda: "offline-pattern-hash",
    ), patch.object(
        iron_dome_sync,
        "export_patterns",
        lambda: dict(specs[1][2]),
    ), patch.object(
        iron_dome_sync,
        "get_sync_status",
        lambda: dict(specs[2][2]),
    ):
        for rule, endpoint, expected in specs:
            before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            case = _invoke_case(
                app,
                service="5002",
                rule=rule,
                method="GET",
                endpoint=endpoint,
                path=rule,
                expected_status=200,
                validator=_json_exact(expected),
            )
            after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            delta = {key: after[key] - before[key] for key in before}
            if any(delta.values()):
                raise ReplayIsolationError(
                    f"Iron Dome replay crossed an isolation boundary for {endpoint}: {delta}"
                )
            case["side_effect_guard"] = {
                "fixture_database_calls": 0,
                "statement_kinds": [],
                "database_mutations": 0,
                "fixture": "in_memory_dependencies",
                **delta,
            }
            cases.append(case)
    return cases


def _osc_file_root_cases(
    osc_files: ModuleType,
    sandbox: Path,
    *,
    audit_attempts: Mapping[str, int],
    blocked_attempts: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Dispatch the OSC business-root projection over sandbox-only paths."""

    from flask_login import AnonymousUserMixin, LoginManager

    class _OfflineFileUser(AnonymousUserMixin):
        id = "offline-user"

    active = sandbox / "case-roots" / "active"
    closed = sandbox / "case-roots" / "closed"
    active.mkdir(parents=True, exist_ok=True)
    closed.mkdir(parents=True, exist_ok=True)
    app = Flask("v3-actual-route-replay-osc-file-roots")
    app.config.update(TESTING=True, SECRET_KEY="offline-route-replay", LOGIN_DISABLED=True)
    login_manager = LoginManager(app)
    login_manager.user_loader(lambda _user_id: None)
    login_manager.anonymous_user = _OfflineFileUser
    app.register_blueprint(osc_files.osc_files_bp)
    case_path_mapper = _fake_module(
        "api.case_path_mapper",
        preferred_case_roots=lambda **_kwargs: [str(active), str(closed)],
        default_case_roots=lambda **_kwargs: [str(active), str(closed)],
    )
    expected = {
        "ok": True,
        "items": [
            {
                "id": "active",
                "label": "進行中案件",
                "folder_name": "01_案件",
                "path": str(active),
                "hint": "依案件種類分類的目前案件資料夾",
                "local_path": str(active),
                "exists": True,
                "children": [],
            },
            {
                "id": "closed",
                "label": "已結案案件",
                "folder_name": "03_工作資料 / 10_結案",
                "path": str(closed),
                "hint": "已結案或歸檔案件資料夾",
                "local_path": str(closed),
                "exists": True,
                "children": [],
            },
        ],
    }
    with patch.dict(sys.modules, {"api.case_path_mapper": case_path_mapper}), patch.object(
        osc_files,
        "_resolve_target_dir",
        lambda value: str(value) if value in {str(active), str(closed)} else "",
    ), patch.object(
        osc_files,
        "_root_child_dirs",
        lambda _value: [],
    ):
        before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
        case = _invoke_case(
            app,
            service="5002",
            rule="/api/osc/folders/roots",
            method="GET",
            endpoint="osc_files.osc_folder_roots_api",
            path="/api/osc/folders/roots",
            expected_status=200,
            validator=_json_exact(expected),
        )
        after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
        delta = {key: after[key] - before[key] for key in before}
        if any(delta.values()):
            raise ReplayIsolationError(
                f"OSC root replay crossed an isolation boundary: {delta}"
            )
        case["side_effect_guard"] = {
            "fixture_database_calls": 0,
            "statement_kinds": [],
            "database_mutations": 0,
            "fixture": "sandbox_paths",
            **delta,
        }
    return [case]


def _osc_public_share_case(
    osc_files: ModuleType,
    sandbox: Path,
    *,
    audit_attempts: Mapping[str, int],
    blocked_attempts: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Download a real public-share fixture and persist its counter in sandbox."""

    token = "offlinePublicShareToken1234567890"
    token_hash = osc_files._share_token_hash(token)
    body = b"offline OSC public share fixture\n"
    cache_dir = sandbox / "osc-share-cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = cache_dir / "offline-proof.txt"
    cached.write_bytes(body)
    store_path = Path(osc_files._SHARE_STORE_PATH).resolve()
    if not _is_within(store_path, sandbox):
        raise ReplayIsolationError(f"OSC share store escaped worker sandbox: {store_path}")
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store_path.write_text(
        json.dumps(
            {
                "shares": {
                    token_hash: {
                        "path": str(cached),
                        "raw_path": str(cached),
                        "name": cached.name,
                        "size": len(body),
                        "expires_at": 4_102_444_800,
                        "downloads": 0,
                        "staged_path": str(cached),
                        "staged_size": len(body),
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    app = Flask("v3-actual-route-replay-osc-public-share")
    app.config.update(TESTING=True, SECRET_KEY="offline-route-replay")
    app.register_blueprint(osc_files.osc_files_bp)
    audit_events: list[dict[str, Any]] = []
    before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
    with patch.object(
        osc_files,
        "_audit_file_event",
        lambda action, path, **kwargs: audit_events.append(
            {"action": action, "path": path, **kwargs}
        ),
    ):
        case = _invoke_case(
            app,
            service="5002",
            rule="/s/<token>",
            method="GET",
            endpoint="osc_files.osc_files_public_share_api",
            path=f"/s/{token}",
            expected_status=200,
            validator=lambda response: (
                response.get_data() == body,
                {
                    "kind": "body_exact_sha256",
                    "expected_sha256": _digest(body),
                    "observed_sha256": _digest(response.get_data()),
                },
            ),
        )
    after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
    delta = {key: after[key] - before[key] for key in before}
    if any(delta.values()):
        raise ReplayIsolationError(
            f"OSC public-share replay crossed an isolation boundary: {delta}"
        )
    observed = json.loads(store_path.read_text(encoding="utf-8"))
    observed_row = observed.get("shares", {}).get(token_hash, {})
    if observed_row.get("downloads") != 1:
        raise ContractValidationError("OSC public-share replay did not persist one download")
    if [event.get("action") for event in audit_events] != ["file.share.download"]:
        raise ContractValidationError("OSC public-share replay emitted unexpected audit events")
    case["side_effect_guard"] = {
        "fixture_database_calls": 0,
        "statement_kinds": [],
        "database_mutations": 0,
        "fixture": "sandbox_share_store_reversible_write",
        "sandbox_download_counter_before": 0,
        "sandbox_download_counter_after": 1,
        "audit_events": ["file.share.download"],
        **delta,
    }
    return [case]


def _raziel_read_only_cases(
    raziel: ModuleType,
    sandbox: Path,
    *,
    audit_attempts: Mapping[str, int],
    blocked_attempts: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Dispatch Raziel status and file delivery from sandbox-only artifacts."""

    from flask_login import AnonymousUserMixin, LoginManager

    class _OfflineRazielUser(AnonymousUserMixin):
        id = "offline-user"

    root = sandbox / "raziel"
    delivery = root / "delivery"
    delivery.mkdir(parents=True, exist_ok=True)
    script_path = root / "scripts" / "complete_interpreter_dataset.py"
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text("# offline fixture\n", encoding="utf-8")
    result_file = root / "result.json"
    result_body = b'{"fixture":"raziel-result"}\n'
    result_file.write_bytes(result_body)
    delivery_file = delivery / "offline-delivery.zip"
    delivery_body = b"PK\x03\x04offline-raziel-delivery"
    delivery_file.write_bytes(delivery_body)
    result_paths = {"result": str(result_file)}
    config = {"keyword": "offline", "court": "fixture"}
    app = Flask("v3-actual-route-replay-raziel")
    app.config.update(TESTING=True, SECRET_KEY="offline-route-replay", LOGIN_DISABLED=True)
    login_manager = LoginManager(app)
    login_manager.user_loader(lambda _user_id: None)
    login_manager.anonymous_user = _OfflineRazielUser
    app.register_blueprint(raziel.raziel_bp)
    cases: list[dict[str, Any]] = []
    with patch.object(raziel, "_raziel_root", lambda: root), patch.object(
        raziel,
        "_load_config",
        lambda: dict(config),
    ), patch.object(
        raziel,
        "_result_paths",
        lambda: dict(result_paths),
    ), patch.object(
        raziel,
        "_script_path",
        lambda: script_path,
    ), patch.object(
        raziel,
        "_script_supported_modes",
        lambda _path: {"status", "preview"},
    ), patch.object(
        raziel,
        "_script_uses_runtime_root",
        lambda _path: True,
    ), patch.object(
        raziel,
        "_public_config",
        lambda value: dict(value),
    ), patch.object(
        raziel,
        "tlr_health",
        lambda: {"ok": True, "fixture": "offline"},
    ), patch.object(
        raziel,
        "_safe_delivery_name",
        lambda value: value if value == delivery_file.name else "",
    ), patch.object(
        raziel,
        "_delivery_dir",
        lambda: delivery,
    ):
        specs = (
            (
                "/api/osc/raziel/status",
                "raziel.raziel_status_api",
                "/api/osc/raziel/status",
                _json_exact(
                    {
                        "ok": True,
                        "root": str(root),
                        "script_path": str(script_path),
                        "script_exists": True,
                        "supported_modes": ["preview", "status"],
                        "uses_runtime_root": True,
                        "configured_root": "",
                        "status_message": "判決捕捉與分類器已連線。",
                        "config": config,
                        "tlr": {"ok": True, "fixture": "offline"},
                        "files": {
                            "result": {"path": str(result_file), "exists": True},
                        },
                    }
                ),
            ),
            (
                "/api/osc/raziel/delivery/<path:name>",
                "raziel.raziel_delivery_file_api",
                f"/api/osc/raziel/delivery/{delivery_file.name}",
                lambda response: (
                    response.get_data() == delivery_body,
                    {
                        "kind": "body_exact_sha256",
                        "expected_sha256": _digest(delivery_body),
                        "observed_sha256": _digest(response.get_data()),
                    },
                ),
            ),
            (
                "/api/osc/raziel/file/<kind>",
                "raziel.raziel_file_api",
                "/api/osc/raziel/file/result",
                lambda response: (
                    response.get_data() == result_body,
                    {
                        "kind": "body_exact_sha256",
                        "expected_sha256": _digest(result_body),
                        "observed_sha256": _digest(response.get_data()),
                    },
                ),
            ),
        )
        for rule, endpoint, path, validator in specs:
            before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            case = _invoke_case(
                app,
                service="5002",
                rule=rule,
                method="GET",
                endpoint=endpoint,
                path=path,
                expected_status=200,
                validator=validator,
            )
            after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            delta = {key: after[key] - before[key] for key in before}
            if any(delta.values()):
                raise ReplayIsolationError(
                    f"Raziel replay crossed an isolation boundary for {endpoint}: {delta}"
                )
            case["side_effect_guard"] = {
                "fixture_database_calls": 0,
                "statement_kinds": [],
                "database_mutations": 0,
                "fixture": "sandbox_files",
                **delta,
            }
            cases.append(case)
    return cases


def _telegram_get_probe_case(
    telegram: ModuleType,
    *,
    audit_attempts: Mapping[str, int],
    blocked_attempts: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Dispatch the real side-effect-free Telegram GET health probe."""

    app = Flask("v3-actual-route-replay-telegram-get")
    app.config.update(TESTING=True, SECRET_KEY="offline-route-replay")
    app.register_blueprint(telegram.telegram_bp)
    before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
    case = _invoke_case(
        app,
        service="5002",
        rule="/telegram/webhook",
        method="GET",
        endpoint="telegram.telegram_webhook",
        path="/telegram/webhook",
        expected_status=200,
        validator=_body_contains("OK"),
    )
    after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
    delta = {key: after[key] - before[key] for key in before}
    if any(delta.values()):
        raise ReplayIsolationError(
            f"Telegram GET replay crossed an isolation boundary: {delta}"
        )
    case["side_effect_guard"] = {
        "fixture_database_calls": 0,
        "statement_kinds": [],
        "database_mutations": 0,
        "fixture": "real_get_branch_no_dependencies",
        **delta,
    }
    return [case]


def _server_projection_cases(
    server: ModuleType,
    lottery: ModuleType,
    sandbox: Path,
    *,
    audit_attempts: Mapping[str, int],
    blocked_attempts: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Dispatch server-owned pure pages and GET validation boundaries."""

    export_body = b"offline sandbox export\n"
    export_dir = sandbox / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / "offline.txt").write_bytes(export_body)
    server.app.config.update(
        TESTING=True,
        SECRET_KEY="offline-route-replay",
        LOGIN_DISABLED=True,
        WTF_CSRF_ENABLED=False,
    )
    specs = (
        ("/", "index", 302, _redirect_exact(server.DEFAULT_POST_LOGIN_TARGET)),
        ("/favicon.ico", "favicon", 204, _body_contains()),
        ("/login", "login", 200, _body_contains("offline-template:login.html")),
        ("/logout", "logout", 302, _redirect_exact("/login")),
        ("/register", "register", 200, _body_contains("offline-template:register.html")),
        (
            "/mobile-app",
            "mobile_app_entry",
            302,
            _redirect_exact("/login?next=/mobile&mobile_app=1"),
        ),
        ("/osc", "osc_interface", 200, _body_contains("offline-template:osc.html")),
        ("/osc/debt", "osc_debt_interface", 200, _body_contains("offline-template:osc_debt.html")),
        ("/lottery", "lottery.lottery_page", 200, _body_contains("offline-template:lottery.html")),
        ("/callback", "callback", 200, _body_contains("OK")),
        ("/line/webhook", "callback", 200, _body_contains("OK")),
    )
    cases: list[dict[str, Any]] = []
    with patch.object(
        server,
        "render_template",
        lambda template, **_kwargs: f"offline-template:{template}",
    ), patch.object(
        lottery,
        "render_template",
        lambda template, **_kwargs: f"offline-template:{template}",
    ), patch.object(
        server,
        "_MAGI_ROOT",
        str(sandbox),
    ):
        for rule, endpoint, expected_status, validator in specs:
            before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            case = _invoke_case(
                server.app,
                service="5002",
                rule=rule,
                method="GET",
                endpoint=endpoint,
                path=rule,
                expected_status=expected_status,
                validator=validator,
            )
            after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            delta = {key: after[key] - before[key] for key in before}
            if any(delta.values()):
                raise ReplayIsolationError(
                    f"server projection replay crossed an isolation boundary for {endpoint}: {delta}"
                )
            case["side_effect_guard"] = {
                "fixture_database_calls": 0,
                "statement_kinds": [],
                "database_mutations": 0,
                "fixture": "in_memory_dependencies",
                **delta,
            }
            cases.append(case)
        before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
        case = _invoke_case(
            server.app,
            service="5002",
            rule="/exports/<path:filename>",
            method="GET",
            endpoint="serve_exports",
            path="/exports/offline.txt",
            expected_status=200,
            validator=lambda response: (
                response.get_data() == export_body,
                {
                    "kind": "body_exact_sha256",
                    "expected_sha256": _digest(export_body),
                    "observed_sha256": _digest(response.get_data()),
                },
            ),
        )
        after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
        delta = {key: after[key] - before[key] for key in before}
        if any(delta.values()):
            raise ReplayIsolationError(
                f"server projection replay crossed an isolation boundary for serve_exports: {delta}"
            )
        case["side_effect_guard"] = {
            "fixture_database_calls": 0,
            "statement_kinds": [],
            "database_mutations": 0,
            "fixture": "sandbox_file",
            **delta,
        }
        cases.append(case)
    return cases


def _toolsapi_proxy_cases(
    server: ModuleType,
    *,
    audit_attempts: Mapping[str, int],
    blocked_attempts: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Dispatch every tools-api compatibility proxy method to an in-memory upstream."""

    app = Flask("v3-actual-route-replay-toolsapi-proxy")
    app.config.update(TESTING=True, SECRET_KEY="offline-route-replay")
    app.add_url_rule(
        "/toolsapi/<path:subpath>",
        endpoint="toolsapi_compat_proxy",
        view_func=server.toolsapi_compat_proxy,
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )

    class _OfflineHeaders(dict[str, str]):
        pass

    class _OfflineUrlResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body
            self.status = 200
            self.headers = _OfflineHeaders(
                {"Content-Type": "application/json; charset=utf-8"}
            )

        def __enter__(self) -> "_OfflineUrlResponse":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return self._body

    upstream_calls: list[dict[str, Any]] = []

    def _urlopen(request_object: Any, *, timeout: int) -> _OfflineUrlResponse:
        method = str(request_object.get_method()).upper()
        upstream_calls.append(
            {
                "method": method,
                "url": request_object.full_url,
                "body_sha256": _digest(request_object.data or b""),
                "timeout": timeout,
            }
        )
        return _OfflineUrlResponse(_canonical({"ok": True, "upstream_method": method}))

    fake_registry = _fake_module(
        "api.routing.service_registry",
        get_service_url=lambda name: (
            "http://offline-tools.invalid" if name == "tools_api" else ""
        ),
    )
    cases: list[dict[str, Any]] = []
    with patch.dict(sys.modules, {"api.routing.service_registry": fake_registry}), patch.object(
        server.urllib.request,
        "urlopen",
        _urlopen,
    ):
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            call_count = len(upstream_calls)
            case = _invoke_case(
                app,
                service="5002",
                rule="/toolsapi/<path:subpath>",
                method=method,
                endpoint="toolsapi_compat_proxy",
                path="/toolsapi/health?probe=offline",
                expected_status=200,
                validator=_json_exact({"ok": True, "upstream_method": method}),
                json_body={"fixture": True} if method != "GET" else None,
            )
            after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            delta = {key: after[key] - before[key] for key in before}
            if any(delta.values()) or len(upstream_calls) != call_count + 1:
                raise ReplayIsolationError(
                    f"tools-api proxy replay escaped its in-memory upstream for {method}: {delta}"
                )
            observed = upstream_calls[-1]
            if observed["method"] != method or observed["url"] != (
                "http://offline-tools.invalid/health?probe=offline"
            ):
                raise ContractValidationError(
                    f"tools-api proxy target drift for {method}: {observed}"
                )
            case["side_effect_guard"] = {
                "fixture_database_calls": 0,
                "statement_kinds": [],
                "database_mutations": 0,
                "fixture": "in_memory_http_upstream",
                "upstream_method": method,
                "upstream_url": observed["url"],
                **delta,
            }
            cases.append(case)
    return cases


def _golem_in_memory_cases(
    golem_console: ModuleType,
    *,
    sandbox: Path,
    audit_attempts: Mapping[str, int],
    blocked_attempts: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Dispatch Golem console projections with every path and provider sandbox-bound."""

    from flask_login import LoginManager

    root = sandbox / "golem"
    static_dir = root / "static"
    exports_dir = static_dir / "exports"
    agent_dir = root / ".agent"
    skills_dir = root / "skills"
    exports_dir.mkdir(parents=True, exist_ok=True)
    agent_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "server.log").write_text("offline server\n", encoding="utf-8")
    (agent_dir / "daemon.log").write_text("offline daemon\n", encoding="utf-8")
    definitions = {
        "tools": [
            {
                "name": "offline-tool",
                "sage": "shared",
                "method": "GET",
                "endpoint": "/offline",
                "description": "fixture",
            }
        ],
        "_meta": {"runtime_filter": {"tools_exposed": 1}},
    }
    definitions_path = skills_dir / "definitions.json"
    definitions_path.write_text(json.dumps(definitions), encoding="utf-8")

    app = Flask("v3-actual-route-replay-golem")
    app.config.update(TESTING=True, SECRET_KEY="offline-route-replay", LOGIN_DISABLED=True)
    LoginManager(app)
    app.register_blueprint(golem_console.golem_console_bp)
    api_key_item = {
        "id": "nvidia_nim",
        "label": "NVIDIA NIM",
        "env_key": "NVIDIA_NIM_API_KEY",
        "enable_key": "NVIDIA_NIM_ENABLE",
        "configured": False,
        "masked": "",
        "enabled": False,
        "updated_from": str(root / ".env"),
    }
    skill_item = {
        "name": "offline-tool",
        "sage": "shared",
        "method": "GET",
        "endpoint": "/offline",
        "description": "fixture",
    }
    specs = (
        ("GET", "/api/golem/api-keys", "golem_console.golem_api_keys_api", 200, _json_exact({"ok": True, "items": [api_key_item]}), None),
        (
            "GET",
            "/api/golem/skills",
            "golem_console.golem_skills_api",
            200,
            _json_exact({"ok": True, "items": [skill_item], "meta": definitions["_meta"], "count": 1}),
            None,
        ),
        (
            "GET",
            "/api/golem/logs",
            "golem_console.golem_logs_api",
            200,
            _json_exact({"ok": True, "server": ["offline server"], "daemon": ["offline daemon"], "market": []}),
            None,
        ),
        (
            "GET",
            "/api/golem/status",
            "golem_console.golem_status_api",
            200,
            _json_deep_subset(
                {
                    "ok": True,
                    "root": str(root),
                    "process": {"ok": True, "fixture": "process-monitor"},
                    "skills": {"items": [skill_item], "count": 1},
                    "memory": {"doc_count": 0, "source_count": 0, "faiss_bytes": 0},
                    "exports": [],
                    "market_reports": [],
                    "api_keys": [api_key_item],
                }
            ),
            None,
        ),
        (
            "POST",
            "/api/golem/command",
            "golem_console.golem_command_api",
            400,
            _json_exact({"ok": False, "error": "empty_command"}),
            {},
        ),
    )

    cases: list[dict[str, Any]] = []
    patches = (
        # LOGIN_DISABLED bypasses Flask-Login authentication, but the product's
        # Golem endpoints also enforce an explicit administrator role.  This
        # offline replay is intentionally exercising the authorised handler
        # path, so provide an in-memory administrator identity without
        # weakening the production authorisation check.
        patch.object(golem_console, "_is_admin_user", lambda: True),
        patch.object(golem_console, "_MAGI_ROOT", root),
        patch.object(golem_console, "_STATIC_DIR", static_dir),
        patch.object(golem_console, "_EXPORTS_DIR", exports_dir),
        patch.object(golem_console, "_AGENT_DIR", agent_dir),
        patch.object(golem_console, "_SKILLS_DEFINITIONS", definitions_path),
        patch.object(golem_console, "_GUARDIAN_STATE", static_dir / "process_guardian_state.json"),
        patch.object(golem_console, "_ENV_PATH", root / ".env"),
        patch.object(
            golem_console,
            "_collect_process_monitor",
            lambda **_kwargs: {"ok": True, "fixture": "process-monitor"},
        ),
    )
    with ExitStack() as stack:
        for fixture_patch in patches:
            stack.enter_context(fixture_patch)
        for method, rule, endpoint, expected_status, validator, json_body in specs:
            before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            case = _invoke_case(
                app,
                service="5002",
                rule=rule,
                method=method,
                endpoint=endpoint,
                path=rule,
                expected_status=expected_status,
                validator=validator,
                json_body=json_body,
            )
            after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            delta = {key: after[key] - before[key] for key in before}
            if any(delta.values()):
                raise ReplayIsolationError(f"Golem replay crossed an isolation boundary for {endpoint}: {delta}")
            case["side_effect_guard"] = {
                "fixture_database_calls": 0,
                "statement_kinds": [],
                "database_mutations": 0,
                "fixture": "in_memory_dependencies",
                **delta,
            }
            cases.append(case)
    return cases


def _osc_read_only_cases(
    osc_cases: ModuleType,
    osc_settings: ModuleType,
    osc_accounting: ModuleType,
    osc_debt: ModuleType,
    osc_gcal: ModuleType,
    osc_pdf: ModuleType,
    debt_generator: ModuleType,
    osc_utils: ModuleType,
    *,
    sandbox: Path,
    audit_attempts: Mapping[str, int],
    blocked_attempts: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Dispatch reviewed OSC GET handlers against a SELECT-only fixture."""

    from flask_login import LoginManager

    statements: list[dict[str, Any]] = []
    write_db = sqlite3.connect(":memory:")
    write_db.execute(
        """
        CREATE TABLE operation_journal (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            statement_kind TEXT NOT NULL,
            target_table TEXT NOT NULL,
            params_json TEXT NOT NULL
        )
        """
    )
    transactional_fixture_state: dict[str, Any] = {"one": None, "all": []}
    fixture_state: dict[str, Any] = {"one": None, "insights": []}
    bonus_body = b"offline monthly bonus xlsx fixture\n"
    bonus_path = sandbox / "fixtures" / "monthly-bonus.xlsx"
    bonus_path.parent.mkdir(parents=True, exist_ok=True)
    bonus_path.write_bytes(bonus_body)
    backup_dir = sandbox / "osc-backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    class _SheetsAuthorizationRequired(Exception):
        pass

    accounting_sheet_fixture = _fake_module(
        "api.osc.accounting_sheet_import",
        DEFAULT_ACCOUNT_HINT="offline-account",
        DEFAULT_GID=0,
        DEFAULT_SPREADSHEET_ID="offline-sheet",
        SheetsAuthorizationRequired=_SheetsAuthorizationRequired,
        run_import=lambda **kwargs: {
            "ok": True,
            "fixture": "sheet-import",
            "dry_run": bool(kwargs.get("dry_run")),
        },
    )
    accounting_bonus_fixture = _fake_module(
        "api.osc.accounting_bonus",
        calculate_monthly_bonus=lambda **kwargs: {
            "ok": True,
            "fixture": "monthly-bonus",
            "month": kwargs.get("month") or "2026-07",
            "commit": bool(kwargs.get("commit")),
            "rows": [],
        },
        export_monthly_bonus_xlsx=lambda _result: str(bonus_path),
        record_monthly_bonus_xlsx_path=lambda *_args: None,
        write_temp_xlsx=lambda _result: str(bonus_path),
    )

    def read_only_osc_exec(statement: str, params: Any = None, *, fetch: str = "none") -> tuple[Any, dict[str, Any]]:
        normalized = " ".join(str(statement or "").split())
        statement_kind = normalized.split(" ", 1)[0].upper() if normalized else ""
        if statement_kind != "SELECT" or fetch not in {"all", "one"}:
            raise ReplayIsolationError(
                f"OSC read-only replay fixture rejected database operation: kind={statement_kind!r} fetch={fetch!r}"
            )
        statements.append(
            {
                "statement_kind": statement_kind,
                "fetch": fetch,
                "parameter_count": len(params or ()),
            }
        )
        return (fixture_state["one"] if fetch == "one" else []), {"fixture": "read_only_select"}

    def transactional_osc_exec(
        statement: str,
        params: Any = None,
        *,
        fetch: str = "none",
    ) -> tuple[Any, dict[str, Any]]:
        """Journal actual handler SQL in a real SQLite in-memory transaction fixture."""

        normalized = " ".join(str(statement or "").split())
        tokens = normalized.replace("(", " ").split()
        statement_kind = tokens[0].upper() if tokens else ""
        if statement_kind == "INSERT" and len(tokens) > 2 and tokens[1].upper() == "INTO":
            target_table = tokens[2]
        elif statement_kind == "UPDATE" and len(tokens) > 1:
            target_table = tokens[1]
        elif statement_kind == "DELETE" and len(tokens) > 2 and tokens[1].upper() == "FROM":
            target_table = tokens[2]
        elif statement_kind == "SELECT" and "FROM" in [token.upper() for token in tokens]:
            from_index = [token.upper() for token in tokens].index("FROM")
            target_table = tokens[from_index + 1]
        else:
            raise ReplayIsolationError(
                f"OSC transactional fixture rejected SQL shape: {normalized!r}"
            )
        if statement_kind not in {"SELECT", "INSERT", "UPDATE", "DELETE"}:
            raise ReplayIsolationError(
                f"OSC transactional fixture rejected SQL kind: {statement_kind!r}"
            )
        cursor = write_db.execute(
            "INSERT INTO operation_journal (statement_kind, target_table, params_json) "
            "VALUES (?, ?, ?)",
            (
                statement_kind,
                target_table,
                json.dumps(params or (), ensure_ascii=False, default=str),
            ),
        )
        write_db.commit()
        sequence = int(cursor.lastrowid)
        if statement_kind == "SELECT":
            selected = (
                transactional_fixture_state["one"]
                if fetch == "one"
                else transactional_fixture_state["all"]
            )
            return selected, {
                "fixture": "transactional_sqlite_operation_journal"
            }
        return {"rowcount": 1, "lastrowid": sequence}, {
            "fixture": "transactional_sqlite_operation_journal"
        }

    def osc_text(value: Any) -> str:
        return str(value or "").strip()

    class _MetaCursor:
        def __init__(self) -> None:
            self.last_statement = ""

        def execute(self, statement: str, params: Any = None) -> None:
            normalized = " ".join(str(statement or "").split())
            statement_kind = normalized.split(" ", 1)[0].upper() if normalized else ""
            if statement_kind != "SELECT":
                raise ReplayIsolationError(
                    f"OSC meta fixture rejected database operation: kind={statement_kind!r}"
                )
            statements.append(
                {
                    "statement_kind": statement_kind,
                    "fetch": "one",
                    "parameter_count": len(params or ()),
                }
            )
            self.last_statement = normalized

        def fetchone(self) -> dict[str, Any]:
            if "CURRENT_USER()" in self.last_statement:
                return {"current_user_name": "offline@localhost"}
            return {"c": 0}

        def close(self) -> None:
            return None

    class _MetaConnection:
        def cursor(self, *, dictionary: bool = False) -> _MetaCursor:
            if not dictionary:
                raise ReplayIsolationError("OSC meta fixture requires a dictionary cursor")
            return _MetaCursor()

        def close(self) -> None:
            return None

    meta_db_config = {
        "host": "offline.invalid",
        "port": 3306,
        "database": "offline_osc",
        "user": "offline",
    }

    app = Flask("v3-actual-route-replay-osc-read-only")
    app.config.update(
        TESTING=True,
        SECRET_KEY="offline-route-replay",
        LOGIN_DISABLED=True,
    )
    LoginManager(app)
    app.register_blueprint(osc_cases.osc_bp)
    app.register_blueprint(osc_settings.osc_settings_bp)
    app.register_blueprint(osc_accounting.osc_accounting_bp)
    app.register_blueprint(osc_debt.osc_debt_bp)
    app.register_blueprint(osc_gcal.osc_gcal_bp)
    app.register_blueprint(osc_pdf.osc_pdf_bp)

    specs = (
        ("/api/osc/settings", "osc_settings.osc_settings_api", "/api/osc/settings?limit=7", {"ok": True, "items": []}),
        (
            "/api/osc/settings/<path:setting_key>",
            "osc_settings.osc_setting_detail_api",
            "/api/osc/settings/offline-key",
            {"ok": True, "item": None},
        ),
        ("/api/osc/courts", "osc_settings.osc_courts_api", "/api/osc/courts?limit=7", {"ok": True, "items": []}),
        (
            "/api/osc/legal-aid-branches",
            "osc_settings.osc_legal_aid_branches_api",
            "/api/osc/legal-aid-branches?limit=7",
            {"ok": True, "items": []},
        ),
        (
            "/api/osc/case-reason-templates",
            "osc_cases.osc_case_reason_templates_api",
            "/api/osc/case-reason-templates?limit=7",
            {"ok": True, "items": []},
        ),
        (
            "/api/osc/activity-logs",
            "osc_cases.osc_activity_logs_api",
            "/api/osc/activity-logs?limit=7",
            {"ok": True, "items": []},
        ),
        (
            "/api/osc/user-settings",
            "osc_cases.osc_user_settings_api",
            "/api/osc/user-settings?limit=7",
            {"ok": True, "items": []},
        ),
        (
            "/api/osc/memory-keywords",
            "osc_cases.osc_memory_keywords_api",
            "/api/osc/memory-keywords?limit=7",
            {"ok": True, "items": []},
        ),
        (
            "/api/osc/opponents",
            "osc_cases.osc_opponents_api",
            "/api/osc/opponents?limit=7",
            {"ok": True, "items": []},
        ),
        (
            "/api/osc/pdf-generation-log",
            "osc_cases.osc_pdf_generation_log_api",
            "/api/osc/pdf-generation-log?limit=7",
            {"ok": True, "items": []},
        ),
        (
            "/api/osc/document-templates",
            "osc_cases.osc_document_templates_api",
            "/api/osc/document-templates?limit=7",
            {"ok": True, "items": []},
        ),
        (
            "/api/osc/document-keywords",
            "osc_cases.osc_document_keywords_api",
            "/api/osc/document-keywords?limit=7",
            {"ok": True, "items": []},
        ),
        (
            "/api/osc/document-replacements",
            "osc_cases.osc_document_replacements_api",
            "/api/osc/document-replacements?limit=7",
            {"ok": True, "items": []},
        ),
        (
            "/api/osc/quotation-templates",
            "osc_cases.osc_quotation_templates_api",
            "/api/osc/quotation-templates?limit=7",
            {"ok": True, "items": []},
        ),
        ("/api/osc/clients", "osc_cases.osc_clients_api", "/api/osc/clients?limit=7", {"ok": True, "items": []}),
        (
            "/api/osc/meetings",
            "osc_cases.osc_meetings_api",
            "/api/osc/meetings?limit=7",
            {"ok": True, "items": []},
        ),
        ("/api/osc/todos", "osc_cases.osc_todos_api", "/api/osc/todos?limit=7", {"ok": True, "items": []}),
    )
    detail_specs = (
        (
            "/api/osc/accounting/transactions/<int:row_id>",
            "osc_accounting.osc_accounting_transaction_detail_api",
            "/api/osc/accounting/transactions/7",
        ),
        (
            "/api/osc/accounting/defaults/<int:row_id>",
            "osc_accounting.osc_accounting_default_detail_api",
            "/api/osc/accounting/defaults/7",
        ),
        (
            "/api/osc/accounting/recurring/<int:row_id>",
            "osc_accounting.osc_accounting_recurring_detail_api",
            "/api/osc/accounting/recurring/7",
        ),
        (
            "/api/osc/courts/<int:row_id>",
            "osc_settings.osc_court_detail_api",
            "/api/osc/courts/7",
        ),
        (
            "/api/osc/legal-aid-branches/<int:row_id>",
            "osc_settings.osc_legal_aid_branch_detail_api",
            "/api/osc/legal-aid-branches/7",
        ),
        (
            "/api/osc/activity-logs/<int:row_id>",
            "osc_cases.osc_activity_log_detail_api",
            "/api/osc/activity-logs/7",
        ),
        (
            "/api/osc/case-reason-templates/<int:row_id>",
            "osc_cases.osc_case_reason_template_detail_api",
            "/api/osc/case-reason-templates/7",
        ),
        (
            "/api/osc/calendar/events/<int:row_id>",
            "osc_cases.osc_calendar_event_detail_api",
            "/api/osc/calendar/events/7",
        ),
        (
            "/api/osc/clients/<row_id>",
            "osc_cases.osc_client_detail_api",
            "/api/osc/clients/offline-client",
        ),
        (
            "/api/osc/document-keywords/<int:row_id>",
            "osc_cases.osc_document_keyword_detail_api",
            "/api/osc/document-keywords/7",
        ),
        (
            "/api/osc/document-replacements/<int:row_id>",
            "osc_cases.osc_document_replacement_detail_api",
            "/api/osc/document-replacements/7",
        ),
        (
            "/api/osc/document-templates/<int:row_id>",
            "osc_cases.osc_document_template_detail_api",
            "/api/osc/document-templates/7",
        ),
        (
            "/api/osc/meetings/<int:row_id>",
            "osc_cases.osc_meeting_detail_api",
            "/api/osc/meetings/7",
        ),
        (
            "/api/osc/memory-keywords/<path:case_number>/<path:hotkey>",
            "osc_cases.osc_memory_keyword_detail_api",
            "/api/osc/memory-keywords/offline-case/offline-key",
        ),
        (
            "/api/osc/opponents/<int:row_id>",
            "osc_cases.osc_opponent_detail_api",
            "/api/osc/opponents/7",
        ),
        (
            "/api/osc/pdf-generation-log/<int:row_id>",
            "osc_cases.osc_pdf_generation_log_detail_api",
            "/api/osc/pdf-generation-log/7",
        ),
        (
            "/api/osc/quotation-templates/<int:row_id>",
            "osc_cases.osc_quotation_template_detail_api",
            "/api/osc/quotation-templates/7",
        ),
        (
            "/api/osc/quotations/<row_id>",
            "osc_cases.osc_quotation_detail_api",
            "/api/osc/quotations/offline-quotation",
        ),
        (
            "/api/osc/todos/<int:row_id>",
            "osc_cases.osc_todo_detail_api",
            "/api/osc/todos/7",
        ),
        (
            "/api/osc/user-settings/<int:row_id>",
            "osc_cases.osc_user_setting_detail_api",
            "/api/osc/user-settings/7",
        ),
    )
    archive_job = {
        "id": "offline-job",
        "status": "done",
        "created_at": "2026-07-14T00:00:00",
        "result": {"ok": True, "fixture": "in-memory"},
    }
    draft_meta = {
        "ok": True,
        "meta": {
            "enabled": False,
            "provider": "casper",
            "effective_provider": "casper",
            "ollama_model": "offline-model",
            "ollama_url": "http://127.0.0.1:11434",
            "allow_cloud_models": False,
            "template_source": "default",
            "has_custom_template": False,
            "template_length": len(osc_cases._OSC_DRAFT_PROMPT_TEMPLATE),
        },
        "doc_types": osc_cases._OSC_DRAFT_DOC_TYPES,
    }
    third_batch_specs = (
        (
            "/api/osc/accounting/transactions",
            "osc_accounting.osc_accounting_transactions_api",
            "/api/osc/accounting/transactions?limit=7",
            200,
            {"ok": True, "items": []},
            1,
        ),
        (
            "/api/osc/accounting/defaults",
            "osc_accounting.osc_accounting_defaults_api",
            "/api/osc/accounting/defaults?limit=7",
            200,
            {"ok": True, "items": []},
            1,
        ),
        (
            "/api/osc/accounting/recurring",
            "osc_accounting.osc_accounting_recurring_api",
            "/api/osc/accounting/recurring?limit=7",
            200,
            {"ok": True, "items": []},
            1,
        ),
        (
            "/api/osc/accounting/summary",
            "osc_accounting.osc_accounting_summary_api",
            "/api/osc/accounting/summary",
            200,
            {"ok": True, "totals": {}, "by_category": []},
            2,
        ),
        (
            "/api/osc/quotations",
            "osc_cases.osc_quotations_api",
            "/api/osc/quotations?limit=7",
            200,
            {"ok": True, "items": []},
            1,
        ),
        (
            "/api/osc/calendar/events",
            "osc_cases.osc_calendar_events_api",
            "/api/osc/calendar/events?limit=7&include_todos=0",
            200,
            {
                "ok": True,
                "items": [],
                "source_counts": {"calendar_events": 0, "gcal_import": 0, "calendar_todo": 0},
                "source_policy": "calendar_events + open case_todos classified by api.osc.calendar_sources",
            },
            1,
        ),
        (
            "/api/osc/laf",
            "osc_cases.osc_laf_api",
            "/api/osc/laf?limit=7",
            200,
            {
                "ok": True,
                "items": {"checklist": [], "lifecycle": [], "emails": []},
                "counts": {"checklist": 0, "lifecycle": 0, "emails": 0},
            },
            3,
        ),
        (
            "/api/osc/laf/cases",
            "osc_cases.osc_laf_cases_api",
            "/api/osc/laf/cases?limit=7",
            200,
            {"ok": True, "items": []},
            1,
        ),
        (
            "/api/osc/checklists/case",
            "osc_cases.osc_case_checklist_get",
            "/api/osc/checklists/case?case_number=offline-case",
            200,
            {"ok": True, "items": []},
            1,
        ),
        (
            "/api/osc/checklists/legal-aid",
            "osc_cases.osc_laf_checklist_get",
            "/api/osc/checklists/legal-aid?case_number=offline-case",
            200,
            {"ok": True, "items": []},
            1,
        ),
        (
            "/api/osc/checklists/debt-required",
            "osc_cases.osc_laf_debt_required_get",
            "/api/osc/checklists/debt-required",
            400,
            {"ok": False, "error": "case_number 必填"},
            0,
        ),
        (
            "/api/osc/drafts/feedback",
            "osc_cases.osc_drafts_feedback_recent_api",
            "/api/osc/drafts/feedback?limit=7",
            200,
            {"ok": True, "summary": {"count": 0, "fixture": True}, "items": []},
            0,
        ),
        (
            "/api/osc/drafts/meta",
            "osc_cases.osc_drafts_meta_api",
            "/api/osc/drafts/meta",
            200,
            draft_meta,
            0,
        ),
        (
            "/api/osc/archive-jobs/<job_id>",
            "osc_cases.osc_archive_job_status_api",
            "/api/osc/archive-jobs/offline-job",
            200,
            {"ok": True, "job": archive_job},
            0,
        ),
        (
            "/api/osc/files/text",
            "osc_cases.osc_file_text_api",
            "/api/osc/files/text",
            400,
            {"ok": False, "error": "path required"},
            0,
        ),
        (
            "/api/osc/debt/forms",
            "osc_debt.debt_forms_list",
            "/api/osc/debt/forms",
            200,
            {"ok": True, "forms": debt_generator.get_all_form_types()},
            0,
        ),
        (
            "/api/osc/debt/courts",
            "osc_debt.debt_courts_list",
            "/api/osc/debt/courts",
            200,
            {"ok": True, "courts": debt_generator.COURT_OPTIONS},
            0,
        ),
        (
            "/api/osc/debt/expense-reference",
            "osc_debt.debt_expense_reference",
            "/api/osc/debt/expense-reference",
            200,
            {"ok": True, "reference": debt_generator.STATUTORY_EXPENSE_REFERENCES},
            0,
        ),
        (
            "/api/osc/debt/schema/<form_type>",
            "osc_debt.debt_form_schema",
            "/api/osc/debt/schema/application",
            200,
            {"ok": True, "schema": debt_generator.get_form_schema("application")},
            0,
        ),
        (
            "/api/osc/pdf/info",
            "osc_pdf.osc_pdf_info_api",
            "/api/osc/pdf/info",
            400,
            {"ok": False, "error": "請先指定 PDF 路徑"},
            0,
        ),
    )
    intelligence_for_query = {"ok": True, "cases": [], "fixture": "in-memory-intelligence"}
    intelligence_for_case = {
        "ok": True,
        "cases": [{"id": "offline-case"}],
        "fixture": "in-memory-intelligence",
    }
    client_row = {"id": "offline-client", "name": "Fixture Client"}
    fourth_batch_specs = (
        (
            "/api/osc/saas/overview",
            "osc_cases.osc_saas_overview_api",
            "/api/osc/saas/overview?case_number=offline-case",
            {"ok": True, "fixture": "overview"},
            0,
            None,
        ),
        (
            "/api/osc/saas/timeline",
            "osc_cases.osc_saas_timeline_api",
            "/api/osc/saas/timeline?case_number=offline-case",
            {"ok": True, "timeline": [], "fixture": "timeline"},
            0,
            None,
        ),
        (
            "/api/osc/saas/task-boards",
            "osc_cases.osc_saas_task_boards_api",
            "/api/osc/saas/task-boards?case_number=offline-case",
            {"ok": True, "boards": [], "fixture": "task-boards"},
            0,
            None,
        ),
        (
            "/api/osc/saas/onboarding",
            "osc_cases.osc_saas_onboarding_api",
            "/api/osc/saas/onboarding",
            {"ok": True, "steps": [], "fixture": "onboarding"},
            0,
            None,
        ),
        (
            "/api/osc/saas/notification-prefs",
            "osc_cases.osc_saas_notification_prefs_api",
            "/api/osc/saas/notification-prefs",
            {"ok": True, "preferences": {}, "fixture": "notification-prefs"},
            0,
            None,
        ),
        (
            "/api/osc/saas/workflow-templates",
            "osc_cases.osc_saas_workflow_templates_api",
            "/api/osc/saas/workflow-templates",
            {"ok": True, "templates": [], "fixture": "workflow-templates"},
            0,
            None,
        ),
        (
            "/api/osc/saas/ai-governance",
            "osc_cases.osc_saas_ai_governance_api",
            "/api/osc/saas/ai-governance",
            {"ok": True, "policies": [], "fixture": "ai-governance"},
            0,
            None,
        ),
        (
            "/api/osc/saas/operations-report",
            "osc_cases.osc_saas_operations_report_api",
            "/api/osc/saas/operations-report",
            {"ok": True, "report": "offline", "fixture": "operations-report"},
            0,
            None,
        ),
        (
            "/api/osc/saas/diagnostic-pack",
            "osc_cases.osc_saas_diagnostic_pack_api",
            "/api/osc/saas/diagnostic-pack",
            {"ok": True, "fixture": "diagnostic-pack"},
            0,
            None,
        ),
        (
            "/api/osc/documents",
            "osc_cases.osc_documents_api",
            "/api/osc/documents?limit=7",
            {"ok": True, "items": []},
            2,
            None,
        ),
        (
            "/api/osc/judgments",
            "osc_cases.osc_judgments_compat_api",
            "/api/osc/judgments",
            [],
            0,
            None,
        ),
        (
            "/api/osc/case-intelligence",
            "osc_cases.osc_case_intelligence_api",
            "/api/osc/case-intelligence?q=offline",
            intelligence_for_query,
            0,
            None,
        ),
        (
            "/api/osc/cases/<row_id>/intelligence-snapshot",
            "osc_cases.osc_case_intelligence_for_case_api",
            "/api/osc/cases/offline-case/intelligence-snapshot",
            intelligence_for_case,
            0,
            None,
        ),
        (
            "/api/osc/clients/<row_id>/workbench",
            "osc_cases.osc_client_workbench_api",
            "/api/osc/clients/offline-client/workbench",
            {
                "ok": True,
                "client": client_row,
                "cases": [],
                "todos": [],
                "meetings": [],
                "legal_aid_checklist": [],
                "case_checklist": [],
                "laf_progress": [],
                "opponents": [],
                "pdf_generation_log": [],
            },
            2,
            client_row,
        ),
        (
            "/api/osc/debt/cases",
            "osc_debt.debt_cases_list",
            "/api/osc/debt/cases",
            {"ok": True, "cases": []},
            1,
            None,
        ),
    )
    fifth_batch_specs = (
        (
            "/api/osc/quotations",
            "POST",
            "osc_cases.osc_quotations_api",
            {"ok": False, "error": "client_name/project_name required"},
        ),
        (
            "/api/osc/checklists/case",
            "POST",
            "osc_cases.osc_case_checklist_post",
            {"ok": False, "error": "case_number 與 item_label 必填"},
        ),
        (
            "/api/osc/checklists/legal-aid",
            "POST",
            "osc_cases.osc_laf_checklist_post",
            {"ok": False, "error": "case_number 必填"},
        ),
        (
            "/api/osc/checklists/debt-required/save",
            "POST",
            "osc_cases.osc_laf_debt_required_save",
            {"ok": False, "error": "case_number 必填"},
        ),
        (
            "/api/osc/forms/preview",
            "POST",
            "osc_cases.osc_forms_preview_api",
            {"ok": False, "error": "form_type required"},
        ),
        (
            "/api/osc/pdf/upload",
            "POST",
            "osc_pdf.osc_pdf_upload_api",
            {"ok": False, "error": "請選擇要上傳的 PDF"},
        ),
        (
            "/api/osc/pdf/action",
            "POST",
            "osc_pdf.osc_pdf_action_api",
            {"ok": False, "error": "請先指定 PDF 路徑"},
        ),
    )

    cases: list[dict[str, Any]] = []
    fixture_patches = (
        patch.object(osc_cases, "_osc_exec", read_only_osc_exec),
        patch.object(osc_cases, "_osc_web_connect", lambda: (_MetaConnection(), meta_db_config)),
        patch.object(
            osc_settings,
            "_get_osc_helpers",
            lambda: (read_only_osc_exec, osc_text, lambda *_args, **_kwargs: None),
        ),
        patch.object(
            osc_accounting,
            "_get_osc_helpers",
            lambda: (
                read_only_osc_exec,
                osc_text,
                lambda *_args, **_kwargs: None,
                lambda value: value,
                lambda value, default=0: int(value or default),
            ),
        ),
        patch.object(osc_cases, "draft_learning_summary", lambda: {"count": 0, "fixture": True}),
        patch.object(osc_cases, "recent_draft_feedback", lambda _limit: []),
        patch.object(osc_cases, "_get_text_primary_model", lambda: "offline-model"),
        patch.object(osc_cases, "_osc_get_setting_value", lambda _key, default="": default),
        patch.object(osc_cases, "_osc_draft_enabled_flag", lambda: False),
        patch.object(osc_cases, "_osc_ensure_case_manual_status_columns", lambda: None),
        patch.object(osc_cases, "_get_translate_local_path_to_canonical", lambda: (lambda value: value)),
        patch.object(osc_cases, "_osc_cleanup_non_extractable_legal_insights", lambda: 0),
        patch.object(osc_cases, "_osc_template_folder_candidates", lambda _path: []),
        patch.object(osc_cases, "_osc_smb_candidates", lambda _path: []),
        patch.object(
            osc_cases,
            "_osc_effective_case_folder_for_row",
            lambda _row, update_db=False: {
                "folder_path": "",
                "local_folder": "",
                "source": "fixture",
                "updated": False,
                "pending_archive": False,
            },
        ),
        patch.object(osc_cases, "_osc_guess_case_folder", lambda _case_number: ""),
        patch.object(osc_cases, "_osc_build_quotation_pdf", lambda _row: b"offline quotation pdf\n"),
        patch.object(osc_cases, "_osc_accounting_window", lambda: ("2026-07-01", "2026-07-31")),
        patch.object(
            osc_cases,
            "load_accounting_summary",
            lambda *_args, **_kwargs: {"totals": {"income_total": 0, "expense_total": 0}},
        ),
        patch.object(osc_cases, "_osc_backup_dir", lambda: backup_dir),
        patch.object(
            osc_cases,
            "_osc_build_archive_preview",
            lambda **_kwargs: {"ok": True, "items": [], "fixture": "archive-preview"},
        ),
        patch.dict(osc_cases._OSC_ARCHIVE_JOBS, {"offline-job": archive_job}, clear=True),
        patch.object(osc_cases, "build_saas_overview", lambda *_args, **_kwargs: {"ok": True, "fixture": "overview"}),
        patch.object(
            osc_cases,
            "build_document_timeline",
            lambda *_args, **_kwargs: {"timeline": [], "fixture": "timeline"},
        ),
        patch.object(
            osc_cases,
            "build_task_boards",
            lambda *_args, **_kwargs: {"ok": True, "boards": [], "fixture": "task-boards"},
        ),
        patch.object(
            osc_cases,
            "build_onboarding_status",
            lambda: {"ok": True, "steps": [], "fixture": "onboarding"},
        ),
        patch.object(
            osc_cases,
            "build_notification_preferences",
            lambda: {"ok": True, "preferences": {}, "fixture": "notification-prefs"},
        ),
        patch.object(
            osc_cases,
            "build_workflow_templates",
            lambda: {"ok": True, "templates": [], "fixture": "workflow-templates"},
        ),
        patch.object(
            osc_cases,
            "build_ai_governance",
            lambda: {"ok": True, "policies": [], "fixture": "ai-governance"},
        ),
        patch.object(
            osc_cases,
            "render_operations_report_text",
            lambda *_args, **_kwargs: {"ok": True, "report": "offline", "fixture": "operations-report"},
        ),
        patch.object(
            osc_cases,
            "build_diagnostic_pack",
            lambda *_args, **_kwargs: {"ok": True, "fixture": "diagnostic-pack"},
        ),
        patch.object(osc_cases, "_osc_collect_insights", lambda: list(fixture_state["insights"])),
        patch.object(
            osc_cases,
            "build_case_intelligence_snapshot",
            lambda _exec, **kwargs: (
                intelligence_for_case if kwargs.get("row_id") else intelligence_for_query
            ),
        ),
        patch.object(osc_utils, "_osc_exec", read_only_osc_exec),
        patch.object(debt_generator, "get_robot_source_status", lambda: {"ok": True, "fixture": "robot-source"}),
        patch.object(
            debt_generator,
            "get_address_options",
            lambda: {"courts": [], "creditors": [], "fixture": "address-options"},
        ),
        patch.object(
            osc_debt,
            "_debt_import_candidates",
            lambda: {"items": [], "fixture": "debt-import"},
        ),
        patch.object(
            osc_gcal,
            "_calendar_token_health",
            lambda refresh=False: {
                "ok": False,
                "status": "missing_token",
                "message": "offline fixture",
                "path": str(sandbox / "missing-token.json"),
                "expires_at": None,
                "expires_in_hours": None,
                "refresh_token_present": False,
                "scopes_ok": False,
                "account_check_status": "not_checked",
                "required": True,
                "next_action": "authorize",
            },
        ),
        patch.object(osc_gcal, "google_token_connected", lambda _health: False),
        patch.object(
            osc_gcal,
            "_get_setting",
            lambda key: {
                "gcal_calendar_id": "offline-calendar",
                "gcal_import_calendar_ids": "offline-imports",
            }.get(key),
        ),
        patch.object(osc_gcal, "TOKEN_PATH", sandbox / "missing-token.json"),
        patch.dict(
            sys.modules,
            {
                "api.osc.accounting_sheet_import": accounting_sheet_fixture,
                "api.osc.accounting_bonus": accounting_bonus_fixture,
                "api.db_failover": _fake_module(
                    "api.db_failover",
                    get_failover_status=lambda: {
                        "failover_active": False,
                        "syncing": False,
                        "remote_ok": None,
                        "active_host": "offline.invalid",
                        "active_port": 3306,
                    },
                ),
            },
        ),
    )
    with ExitStack() as stack:
        for fixture_patch in fixture_patches:
            stack.enter_context(fixture_patch)

        def dispatch_read_only_case(
            rule: str,
            endpoint: str,
            path: str,
            expected: Any,
            *,
            one_row: Mapping[str, Any] | None = None,
            insights: Sequence[Mapping[str, Any]] | None = None,
            expected_status: int = 200,
            expected_database_calls: int = 1,
            method: str = "GET",
            json_body: Any = None,
            validator: Any = None,
        ) -> None:
            fixture_state["one"] = dict(one_row) if one_row is not None else None
            fixture_state["insights"] = [dict(item) for item in (insights or [])]
            before = len(statements)
            isolation_before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            case = _invoke_case(
                app,
                service="5002",
                rule=rule,
                method=method,
                endpoint=endpoint,
                path=path,
                expected_status=expected_status,
                validator=validator or _json_exact(expected),
                json_body=json_body,
            )
            observed_statements = statements[before:]
            isolation_after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            isolation_delta = {
                key: isolation_after[key] - isolation_before[key]
                for key in isolation_before
            }
            if len(observed_statements) != expected_database_calls:
                raise ContractValidationError(
                    "OSC read-only replay fixture SELECT count drift for "
                    f"{endpoint}: expected={expected_database_calls} observed={len(observed_statements)}"
                )
            if any(isolation_delta.values()):
                raise ReplayIsolationError(
                    f"OSC read-only replay crossed an isolation boundary for {endpoint}: {isolation_delta}"
                )
            case["side_effect_guard"] = {
                "fixture_database_calls": len(observed_statements),
                "statement_kinds": [row["statement_kind"] for row in observed_statements],
                "database_mutations": 0,
                "fixture": "select_only_in_memory",
                **isolation_delta,
            }
            cases.append(case)

        def dispatch_transactional_case(
            *,
            rule: str,
            method: str,
            endpoint: str,
            path: str,
            json_body: Mapping[str, Any] | None,
            expected_response: Mapping[str, Any],
            expected_statement_kinds: Sequence[str],
            expected_tables: Sequence[str],
            select_one: Mapping[str, Any] | None = None,
            select_all: Sequence[Mapping[str, Any]] = (),
            extra_patches: Sequence[Any] = (),
        ) -> None:
            transactional_fixture_state["one"] = (
                dict(select_one) if select_one is not None else None
            )
            transactional_fixture_state["all"] = [dict(row) for row in select_all]
            isolation_before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            before_sequence = int(
                write_db.execute(
                    "SELECT COALESCE(MAX(sequence), 0) FROM operation_journal"
                ).fetchone()[0]
            )
            before_changes = write_db.total_changes
            with ExitStack() as transactional_stack:
                transactional_stack.enter_context(
                    patch.object(osc_cases, "_osc_exec", transactional_osc_exec)
                )
                transactional_stack.enter_context(
                    patch.object(osc_utils, "_osc_exec", transactional_osc_exec)
                )
                for extra_patch in extra_patches:
                    transactional_stack.enter_context(extra_patch)
                case = _invoke_case(
                    app,
                    service="5002",
                    rule=rule,
                    method=method,
                    endpoint=endpoint,
                    path=path,
                    expected_status=200,
                    validator=_json_deep_subset(expected_response),
                    json_body=dict(json_body) if json_body is not None else None,
                )
            observed = write_db.execute(
                "SELECT statement_kind, target_table FROM operation_journal "
                "WHERE sequence > ? ORDER BY sequence",
                (before_sequence,),
            ).fetchall()
            observed_kinds = [str(row[0]) for row in observed]
            observed_tables = [str(row[1]) for row in observed]
            isolation_after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            isolation_delta = {
                key: isolation_after[key] - isolation_before[key]
                for key in isolation_before
            }
            if observed_kinds != list(expected_statement_kinds):
                raise ContractValidationError(
                    f"OSC transactional SQL-kind drift for {endpoint} {method}: "
                    f"expected={list(expected_statement_kinds)} observed={observed_kinds}"
                )
            if observed_tables != list(expected_tables):
                raise ContractValidationError(
                    f"OSC transactional table drift for {endpoint} {method}: "
                    f"expected={list(expected_tables)} observed={observed_tables}"
                )
            if write_db.total_changes - before_changes != len(observed):
                raise ContractValidationError(
                    f"OSC transactional SQLite journal did not persist every SQL operation: {endpoint}"
                )
            if any(isolation_delta.values()):
                raise ReplayIsolationError(
                    f"OSC transactional replay crossed an isolation boundary for {endpoint}: "
                    f"{isolation_delta}"
                )
            case["side_effect_guard"] = {
                "fixture_database_calls": len(observed),
                "statement_kinds": observed_kinds,
                "statement_tables": observed_tables,
                "database_mutations": sum(kind != "SELECT" for kind in observed_kinds),
                "sqlite_operation_journal_rows": len(observed),
                "fixture": "transactional_sqlite_operation_journal",
                **isolation_delta,
            }
            cases.append(case)

        def dispatch_file_transcript_case(
            *,
            rule: str,
            endpoint: str,
            path: str,
            tracked_root: Path,
            expected_response: Mapping[str, Any],
            expected_added: int = 0,
            expected_removed: int = 0,
            expected_changed: int = 0,
            json_body: Mapping[str, Any] | None = None,
            data: Any = None,
            content_type: str | None = None,
            extra_patches: Sequence[Any] = (),
        ) -> None:
            tracked_root.mkdir(parents=True, exist_ok=True)

            def snapshot() -> dict[str, str]:
                return {
                    str(file.relative_to(tracked_root)): hashlib.sha256(file.read_bytes()).hexdigest()
                    for file in sorted(tracked_root.rglob("*"))
                    if file.is_file()
                }

            before_files = snapshot()
            isolation_before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            with ExitStack() as file_stack:
                for extra_patch in extra_patches:
                    file_stack.enter_context(extra_patch)
                case = _invoke_case(
                    app,
                    service="5002",
                    rule=rule,
                    method="POST",
                    endpoint=endpoint,
                    path=path,
                    expected_status=200,
                    validator=_json_deep_subset(expected_response),
                    json_body=dict(json_body) if json_body is not None else None,
                    data=data,
                    content_type=content_type,
                )
            after_files = snapshot()
            isolation_after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            isolation_delta = {
                key: isolation_after[key] - isolation_before[key]
                for key in isolation_before
            }
            added = sorted(set(after_files) - set(before_files))
            removed = sorted(set(before_files) - set(after_files))
            changed = sorted(
                name
                for name in set(before_files) & set(after_files)
                if before_files[name] != after_files[name]
            )
            if (len(added), len(removed), len(changed)) != (
                expected_added,
                expected_removed,
                expected_changed,
            ):
                raise ContractValidationError(
                    f"sandbox file transcript drift for {endpoint}: "
                    f"added={added} removed={removed} changed={changed}"
                )
            if any(isolation_delta.values()):
                raise ReplayIsolationError(
                    f"sandbox file replay crossed an isolation boundary for {endpoint}: "
                    f"{isolation_delta}"
                )
            case["side_effect_guard"] = {
                "fixture_database_calls": 0,
                "statement_kinds": [],
                "database_mutations": 0,
                "fixture": "sandbox_file_transcript",
                "file_transcript": {
                    "root_sha256": _digest(str(tracked_root).encode("utf-8")),
                    "added": added,
                    "removed": removed,
                    "changed": changed,
                    "before_sha256": before_files,
                    "after_sha256": after_files,
                },
                **isolation_delta,
            }
            cases.append(case)

        for rule, endpoint, path, expected in specs:
            dispatch_read_only_case(rule, endpoint, path, expected)
        dispatch_read_only_case(
            "/api/osc/meta",
            "osc_cases.osc_meta_api",
            "/api/osc/meta",
            {
                "ok": True,
                "db": {
                    **meta_db_config,
                    "current_user": "offline@localhost",
                },
                "failover": {
                    "failover_active": False,
                    "syncing": False,
                    "remote_ok": None,
                    "active_host": "offline.invalid",
                    "active_port": 3306,
                },
            },
            expected_database_calls=20,
            validator=_json_deep_subset(
                {
                    "ok": True,
                    "db": {
                        **meta_db_config,
                        "current_user": "offline@localhost",
                    },
                    "failover": {
                        "failover_active": False,
                        "syncing": False,
                        "remote_ok": None,
                        "active_host": "offline.invalid",
                        "active_port": 3306,
                    },
                }
            ),
        )
        for rule, endpoint, path in detail_specs:
            row = {"id": "offline-row", "fixture": "select-only", "endpoint": endpoint}
            dispatch_read_only_case(
                rule,
                endpoint,
                path,
                {"ok": True, "item": row},
                one_row=row,
            )
        for rule, endpoint, path, expected_status, expected, expected_database_calls in third_batch_specs:
            dispatch_read_only_case(
                rule,
                endpoint,
                path,
                expected,
                expected_status=expected_status,
                expected_database_calls=expected_database_calls,
            )
        for rule, endpoint, path, expected, expected_database_calls, one_row in fourth_batch_specs:
            dispatch_read_only_case(
                rule,
                endpoint,
                path,
                expected,
                expected_database_calls=expected_database_calls,
                one_row=one_row,
            )
        for rule, method, endpoint, expected in fifth_batch_specs:
            dispatch_read_only_case(
                rule,
                endpoint,
                rule,
                expected,
                expected_status=400,
                expected_database_calls=0,
                method=method,
                json_body={},
            )
        transactional_collection_specs = (
            (
                "/api/osc/case-reason-templates",
                "osc_cases.osc_case_reason_templates_api",
                {"case_type": "民事", "reason": "離線成功路徑", "is_common": True},
                {"ok": True, "result": {"rowcount": 1}},
                ("INSERT", "INSERT"),
                ("case_reason_templates", "activity_logs"),
            ),
            (
                "/api/osc/activity-logs",
                "osc_cases.osc_activity_logs_api",
                {
                    "action": "offline:create",
                    "entity_type": "fixture",
                    "entity_id": "offline-1",
                    "details": "transactional replay",
                    "user": "offline",
                },
                {"ok": True, "result": {"rowcount": 1}},
                ("INSERT",),
                ("activity_logs",),
            ),
            (
                "/api/osc/user-settings",
                "osc_cases.osc_user_settings_api",
                {"hostname": "offline-host", "setting_key": "theme", "setting_value": "dark"},
                {"ok": True, "result": {"rowcount": 1}},
                ("INSERT", "INSERT"),
                ("user_settings", "activity_logs"),
            ),
            (
                "/api/osc/memory-keywords",
                "osc_cases.osc_memory_keywords_api",
                {
                    "case_number": "OFF-001",
                    "hotkey": "party",
                    "name": "當事人",
                    "value": "離線值",
                },
                {"ok": True, "result": {"rowcount": 1}},
                ("INSERT", "INSERT"),
                ("memory_keywords", "activity_logs"),
            ),
            (
                "/api/osc/opponents",
                "osc_cases.osc_opponents_api",
                {"case_number": "OFF-001", "name": "離線對造", "address": "測試地址"},
                {"ok": True, "result": {"rowcount": 1}},
                ("INSERT", "INSERT"),
                ("opponents", "activity_logs"),
            ),
            (
                "/api/osc/document-keywords",
                "osc_cases.osc_document_keywords_api",
                {
                    "keyword_name": "離線關鍵字",
                    "keyword_content": "離線內容",
                    "category": "fixture",
                },
                {"ok": True, "mode": "insert", "result": {"rowcount": 1}},
                ("INSERT",),
                ("document_keywords",),
            ),
            (
                "/api/osc/quotation-templates",
                "osc_cases.osc_quotation_templates_api",
                {"name": "離線報價模板", "items": [{"name": "項目", "amount": 1}]},
                {"ok": True, "result": {"rowcount": 1}},
                ("INSERT",),
                ("quotation_templates",),
            ),
            (
                "/api/osc/calendar/events",
                "osc_cases.osc_calendar_events_api",
                {
                    "event_id": "offline-event",
                    "title": "離線庭期",
                    "start_date": "2026-07-20 09:00:00",
                    "end_date": "2026-07-20 10:00:00",
                },
                {"ok": True, "event_id": "offline-event", "result": {"rowcount": 1}},
                ("INSERT",),
                ("calendar_events",),
            ),
            (
                "/api/osc/clients",
                "osc_cases.osc_clients_api",
                {"id": "C9001", "name": "離線客戶", "phone": "09" + "00000000"},
                {"ok": True, "id": "C9001", "result": {"rowcount": 1}},
                ("INSERT",),
                ("clients",),
            ),
            (
                "/api/osc/meetings",
                "osc_cases.osc_meetings_api",
                {
                    "case_number": "OFF-001",
                    "client_name": "離線客戶",
                    "type": "會議",
                    "datetime": "2026-07-21T14:00:00",
                },
                {"ok": True, "result": {"rowcount": 1}},
                ("INSERT",),
                ("meetings",),
            ),
            (
                "/api/osc/todos",
                "osc_cases.osc_todos_api",
                {
                    "case_number": "OFF-001",
                    "todo_type": "遞狀",
                    "todo_date": "2026-07-22",
                    "description": "離線待辦",
                },
                {"ok": True, "result": {"rowcount": 1}},
                ("SELECT", "INSERT"),
                ("case_todos", "case_todos"),
            ),
        )
        for rule, endpoint, body, expected, kinds, tables in transactional_collection_specs:
            dispatch_transactional_case(
                rule=rule,
                method="POST",
                endpoint=endpoint,
                path=rule,
                json_body=body,
                expected_response=expected,
                expected_statement_kinds=kinds,
                expected_tables=tables,
            )

        transactional_detail_specs = (
            (
                "/api/osc/case-reason-templates/<int:row_id>",
                "osc_cases.osc_case_reason_template_detail_api",
                "/api/osc/case-reason-templates/7",
                {"reason": "更新後理由", "is_common": False},
                "case_reason_templates",
                True,
            ),
            (
                "/api/osc/user-settings/<int:row_id>",
                "osc_cases.osc_user_setting_detail_api",
                "/api/osc/user-settings/7",
                {"setting_value": "light"},
                "user_settings",
                True,
            ),
            (
                "/api/osc/memory-keywords/<path:case_number>/<path:hotkey>",
                "osc_cases.osc_memory_keyword_detail_api",
                "/api/osc/memory-keywords/OFF-001/party",
                {"name": "更新當事人", "value": "更新值"},
                "memory_keywords",
                True,
            ),
            (
                "/api/osc/opponents/<int:row_id>",
                "osc_cases.osc_opponent_detail_api",
                "/api/osc/opponents/7",
                {"name": "更新對造", "is_active": True},
                "opponents",
                True,
            ),
            (
                "/api/osc/document-keywords/<int:row_id>",
                "osc_cases.osc_document_keyword_detail_api",
                "/api/osc/document-keywords/7",
                {"keyword_content": "更新內容", "usage_count": 2},
                "document_keywords",
                False,
            ),
            (
                "/api/osc/quotation-templates/<int:row_id>",
                "osc_cases.osc_quotation_template_detail_api",
                "/api/osc/quotation-templates/7",
                {"description": "更新說明", "is_default": True},
                "quotation_templates",
                False,
            ),
            (
                "/api/osc/calendar/events/<int:row_id>",
                "osc_cases.osc_calendar_event_detail_api",
                "/api/osc/calendar/events/7",
                {"title": "更新庭期", "reminder_minutes": 30},
                "calendar_events",
                False,
            ),
            (
                "/api/osc/clients/<row_id>",
                "osc_cases.osc_client_detail_api",
                "/api/osc/clients/C9001",
                {"name": "更新客戶", "status": "進行中"},
                "clients",
                False,
            ),
            (
                "/api/osc/meetings/<int:row_id>",
                "osc_cases.osc_meeting_detail_api",
                "/api/osc/meetings/7",
                {"datetime": "2026-07-21T15:00:00", "status": "confirmed"},
                "meetings",
                False,
            ),
            (
                "/api/osc/todos/<int:row_id>",
                "osc_cases.osc_todo_detail_api",
                "/api/osc/todos/7",
                {"description": "更新待辦", "status": "已完成"},
                "case_todos",
                False,
            ),
        )
        for rule, endpoint, path, body, table, activity_log in transactional_detail_specs:
            tables = (table, "activity_logs") if activity_log else (table,)
            kinds = ("UPDATE", "INSERT") if activity_log else ("UPDATE",)
            put_tables = tables
            put_kinds = kinds
            if endpoint in {
                "osc_cases.osc_calendar_event_detail_api",
                "osc_cases.osc_todo_detail_api",
            }:
                put_kinds = (*put_kinds, "SELECT")
                put_tables = (*put_tables, table)
            if endpoint == "osc_cases.osc_todo_detail_api":
                # A completed todo first reads its case number to emit the
                # evidence event, then reloads the row for the hearing-
                # conflict candidate.  Both reads are intentional and must
                # remain covered by the replay contract.
                put_kinds = (*put_kinds, "SELECT")
                put_tables = (*put_tables, table)
            dispatch_transactional_case(
                rule=rule,
                method="PUT",
                endpoint=endpoint,
                path=path,
                json_body=body,
                expected_response={"ok": True, "result": {"rowcount": 1}},
                expected_statement_kinds=put_kinds,
                expected_tables=put_tables,
            )
            delete_kinds = ("DELETE", "INSERT") if activity_log else ("DELETE",)
            dispatch_transactional_case(
                rule=rule,
                method="DELETE",
                endpoint=endpoint,
                path=path,
                json_body=None,
                expected_response={"ok": True, "result": {"rowcount": 1}},
                expected_statement_kinds=delete_kinds,
                expected_tables=tables,
            )
        dispatch_transactional_case(
            rule="/api/osc/activity-logs/<int:row_id>",
            method="DELETE",
            endpoint="osc_cases.osc_activity_log_detail_api",
            path="/api/osc/activity-logs/7",
            json_body=None,
            expected_response={"ok": True, "result": {"rowcount": 1}},
            expected_statement_kinds=("DELETE",),
            expected_tables=("activity_logs",),
        )

        settings_helpers = lambda: (
            transactional_osc_exec,
            osc_text,
            osc_utils._osc_log_activity,
        )
        settings_collection_specs = (
            (
                "/api/osc/settings",
                "osc_settings.osc_settings_api",
                {"key": "offline.setting", "value": "enabled", "description": "fixture"},
                {"ok": True, "key": "offline.setting", "result": {"rowcount": 1}},
                "settings",
            ),
            (
                "/api/osc/courts",
                "osc_settings.osc_courts_api",
                {"name": "離線法院", "address": "測試路一號", "type": "地方法院"},
                {"ok": True, "result": {"rowcount": 1}},
                "courts",
            ),
            (
                "/api/osc/legal-aid-branches",
                "osc_settings.osc_legal_aid_branches_api",
                {"name": "離線法扶分會", "address": "測試路二號"},
                {"ok": True, "result": {"rowcount": 1}},
                "legal_aid_branches",
            ),
        )
        for rule, endpoint, body, expected, table in settings_collection_specs:
            dispatch_transactional_case(
                rule=rule,
                method="POST",
                endpoint=endpoint,
                path=rule,
                json_body=body,
                expected_response=expected,
                expected_statement_kinds=("INSERT", "INSERT"),
                expected_tables=(table, "activity_logs"),
                extra_patches=(patch.object(osc_settings, "_get_osc_helpers", settings_helpers),),
            )
        settings_detail_specs = (
            (
                "/api/osc/settings/<path:setting_key>",
                "osc_settings.osc_setting_detail_api",
                "/api/osc/settings/offline.setting",
                {"value": "disabled", "description": "updated fixture"},
                "settings",
            ),
            (
                "/api/osc/courts/<int:row_id>",
                "osc_settings.osc_court_detail_api",
                "/api/osc/courts/7",
                {"name": "更新法院", "address": "更新地址"},
                "courts",
            ),
            (
                "/api/osc/legal-aid-branches/<int:row_id>",
                "osc_settings.osc_legal_aid_branch_detail_api",
                "/api/osc/legal-aid-branches/7",
                {"name": "更新分會", "address": "更新地址"},
                "legal_aid_branches",
            ),
        )
        for rule, endpoint, path, body, table in settings_detail_specs:
            for method, kinds in (("PUT", ("UPDATE", "INSERT")), ("DELETE", ("DELETE", "INSERT"))):
                dispatch_transactional_case(
                    rule=rule,
                    method=method,
                    endpoint=endpoint,
                    path=path,
                    json_body=body if method == "PUT" else None,
                    expected_response={"ok": True, "result": {"rowcount": 1}},
                    expected_statement_kinds=kinds,
                    expected_tables=(table, "activity_logs"),
                    extra_patches=(patch.object(osc_settings, "_get_osc_helpers", settings_helpers),),
                )

        accounting_helpers = lambda: (
            transactional_osc_exec,
            osc_text,
            osc_utils._osc_log_activity,
            lambda value: value,
            lambda value, default=0: int(value or default),
        )
        accounting_collection_specs = (
            (
                "/api/osc/accounting/transactions",
                "osc_accounting.osc_accounting_transactions_api",
                {"date": "2026-07-20", "type": "收入", "category": "委任費", "amount": 1200},
                "case_transactions",
            ),
            (
                "/api/osc/accounting/defaults",
                "osc_accounting.osc_accounting_defaults_api",
                {"category": "郵資", "default_description": "離線預設", "default_amount": 80},
                "expense_defaults",
            ),
            (
                "/api/osc/accounting/recurring",
                "osc_accounting.osc_accounting_recurring_api",
                {"category": "租金", "description": "離線固定支出", "amount": 30000, "day_of_month": 5, "is_active": 1},
                "recurring_expenses",
            ),
        )
        for rule, endpoint, body, table in accounting_collection_specs:
            dispatch_transactional_case(
                rule=rule,
                method="POST",
                endpoint=endpoint,
                path=rule,
                json_body=body,
                expected_response={"ok": True, "result": {"rowcount": 1}},
                expected_statement_kinds=("INSERT",),
                expected_tables=(table,),
                extra_patches=(patch.object(osc_accounting, "_get_osc_helpers", accounting_helpers),),
            )
        accounting_detail_specs = (
            (
                "/api/osc/accounting/transactions/<int:row_id>",
                "osc_accounting.osc_accounting_transaction_detail_api",
                "/api/osc/accounting/transactions/7",
                {"description": "更新交易", "amount": 1300},
                "case_transactions",
            ),
            (
                "/api/osc/accounting/defaults/<int:row_id>",
                "osc_accounting.osc_accounting_default_detail_api",
                "/api/osc/accounting/defaults/7",
                {"default_description": "更新預設", "default_amount": 90},
                "expense_defaults",
            ),
            (
                "/api/osc/accounting/recurring/<int:row_id>",
                "osc_accounting.osc_accounting_recurring_detail_api",
                "/api/osc/accounting/recurring/7",
                {"description": "更新固定支出", "amount": 31000},
                "recurring_expenses",
            ),
        )
        for rule, endpoint, path, body, table in accounting_detail_specs:
            for method, kind in (("PUT", "UPDATE"), ("DELETE", "DELETE")):
                dispatch_transactional_case(
                    rule=rule,
                    method=method,
                    endpoint=endpoint,
                    path=path,
                    json_body=body if method == "PUT" else None,
                    expected_response={"ok": True, "result": {"rowcount": 1}},
                    expected_statement_kinds=(kind,),
                    expected_tables=(table,),
                    extra_patches=(patch.object(osc_accounting, "_get_osc_helpers", accounting_helpers),),
                )
        recurring_row = {
            "id": 7,
            "category": "租金",
            "sub_type": "辦公室",
            "description": "月租",
            "amount": 30000,
        }
        dispatch_transactional_case(
            rule="/api/osc/accounting/recurring/<int:row_id>/sync-generated",
            method="POST",
            endpoint="osc_accounting.osc_accounting_recurring_sync_generated_api",
            path="/api/osc/accounting/recurring/7/sync-generated",
            json_body={"start_date": "2026-01-01", "end_date": "2026-07-31"},
            expected_response={"ok": True, "row_id": 7, "updated_count": 1, "items": []},
            expected_statement_kinds=("SELECT", "UPDATE", "SELECT"),
            expected_tables=("recurring_expenses", "case_transactions", "case_transactions"),
            select_one=recurring_row,
            extra_patches=(patch.object(osc_accounting, "_get_osc_helpers", accounting_helpers),),
        )
        dispatch_transactional_case(
            rule="/api/osc/accounting/import/google-sheet",
            method="POST",
            endpoint="osc_accounting.osc_accounting_google_sheet_import_api",
            path="/api/osc/accounting/import/google-sheet",
            json_body={"month": "2026-07", "commit": False, "auth": False},
            expected_response={"ok": True, "fixture": "sheet-import", "dry_run": True},
            expected_statement_kinds=(),
            expected_tables=(),
        )
        dispatch_transactional_case(
            rule="/api/osc/accounting/monthly-bonus",
            method="POST",
            endpoint="osc_accounting.osc_accounting_monthly_bonus_api",
            path="/api/osc/accounting/monthly-bonus",
            json_body={"month": "2026-07", "commit": False, "refresh_import": False},
            expected_response={"ok": True, "fixture": "monthly-bonus", "commit": False},
            expected_statement_kinds=(),
            expected_tables=(),
        )

        dispatch_transactional_case(
            rule="/api/osc/debt/supplement-checklist",
            method="POST",
            endpoint="osc_debt.debt_supplement_checklist",
            path="/api/osc/debt/supplement-checklist",
            json_body={
                "case_number": "OFF-001",
                "items": ["補正戶籍謄本", {"name": "補正財產資料", "notes": "離線"}],
            },
            expected_response={"ok": True, "case_number": "OFF-001", "synced": 2, "skipped": 0},
            expected_statement_kinds=("INSERT", "INSERT"),
            expected_tables=("case_checklists", "case_checklists"),
            extra_patches=(patch.object(osc_debt, "_debt_osc_exec", transactional_osc_exec),),
        )
        dispatch_transactional_case(
            rule="/api/osc/debt/validate",
            method="POST",
            endpoint="osc_debt.debt_validate",
            path="/api/osc/debt/validate",
            json_body={
                "form_type": "application",
                "data": {"name": "離線聲請人", "address": "測試地址", "asset_total": 0, "debt_total": 1000},
            },
            expected_response={"ok": True, "valid": True, "errors": {}},
            expected_statement_kinds=(),
            expected_tables=(),
        )

        class _OfflineOAuthFlow:
            @classmethod
            def from_client_config(cls, *_args: Any, **_kwargs: Any) -> "_OfflineOAuthFlow":
                return cls()

            def authorization_url(self, **_kwargs: Any) -> tuple[str, str]:
                return "https://accounts.invalid/offline-consent", "offline-oauth-state"

        oauth_flow_module = _fake_module("google_auth_oauthlib.flow", Flow=_OfflineOAuthFlow)
        oauth_package = _fake_module("google_auth_oauthlib", flow=oauth_flow_module)
        dispatch_transactional_case(
            rule="/api/osc/gcal/auth/start",
            method="POST",
            endpoint="osc_gcal.gcal_auth_start",
            path="/api/osc/gcal/auth/start",
            json_body={},
            expected_response={
                "ok": True,
                "auth_url": "https://accounts.invalid/offline-consent",
                "state": "offline-oauth-state",
            },
            expected_statement_kinds=(),
            expected_tables=(),
            extra_patches=(
                patch.object(
                    osc_gcal,
                    "_get_setting",
                    lambda key: {"gcal_client_id": "offline-client", "gcal_client_secret": "offline-secret"}.get(key),
                ),
                patch.object(osc_gcal, "_build_redirect_uri", lambda: "http://localhost/offline-callback"),
                patch.dict(
                    sys.modules,
                    {"google_auth_oauthlib": oauth_package, "google_auth_oauthlib.flow": oauth_flow_module},
                ),
            ),
        )
        dispatch_transactional_case(
            rule="/api/osc/gcal/sync",
            method="POST",
            endpoint="osc_gcal.gcal_sync",
            path="/api/osc/gcal/sync",
            json_body={"apply": False, "limit": 12},
            expected_response={
                "ok": True,
                "synced": 0,
                "dry_run": True,
                "apply": False,
                "safety": "apply_required_for_writes",
            },
            expected_statement_kinds=(),
            expected_tables=(),
            extra_patches=(
                patch.object(osc_gcal, "_load_creds", lambda: type("Creds", (), {"valid": True})()),
                patch.object(osc_gcal, "_run_current_gcal_sync", lambda _options: {"ok": True, "synced": 0}),
            ),
        )

        debt_address_root = sandbox / "debt-write-address"

        def save_fixture_address(name: str, address: str, *_args: Any) -> bool:
            debt_address_root.mkdir(parents=True, exist_ok=True)
            (debt_address_root / "addresses.csv").write_text(
                f"{name},{address}\n",
                encoding="utf-8",
            )
            return True

        dispatch_file_transcript_case(
            rule="/api/osc/debt/address-data",
            endpoint="osc_debt.debt_address_data",
            path="/api/osc/debt/address-data",
            tracked_root=debt_address_root,
            json_body={"name": "離線債權人", "address": "測試路三號"},
            expected_response={"ok": True, "message": "已儲存 離線債權人 的地址"},
            expected_added=1,
            extra_patches=(patch.object(debt_generator, "save_address_to_csv", save_fixture_address),),
        )

        class _OfflineDebtDocument:
            def save(self, path: str) -> None:
                Path(path).write_bytes(b"offline debt docx fixture\n")

        debt_generate_root = sandbox / "debt-write-generate"
        dispatch_file_transcript_case(
            rule="/api/osc/debt/generate",
            endpoint="osc_debt.debt_generate_document",
            path="/api/osc/debt/generate",
            tracked_root=debt_generate_root,
            json_body={
                "form_type": "application",
                "data": {"name": "離線聲請人", "lawyer_name": "離線測試律師"},
            },
            expected_response={"ok": True, "form_type": "application", "message": "已產生 01_消費者債務清理聲請狀"},
            expected_added=1,
            extra_patches=(
                patch.object(debt_generator, "generate_application", lambda _data: _OfflineDebtDocument()),
                patch.object(osc_debt, "_export_dir", lambda: str(debt_generate_root)),
                patch.object(osc_debt, "_file_meta", lambda path: {"url": f"/offline/{Path(path).name}"}),
            ),
        )

        debt_batch_root = sandbox / "debt-write-batch"
        dispatch_file_transcript_case(
            rule="/api/osc/debt/batch-generate",
            endpoint="osc_debt.debt_batch_generate",
            path="/api/osc/debt/batch-generate",
            tracked_root=debt_batch_root,
            json_body={
                "types": ["application"],
                "data": {"name": "離線批次聲請人", "lawyer_name": "離線測試律師"},
            },
            expected_response={"ok": True, "errors": [], "saved_addresses": 0, "message": "已產生 1 份文件"},
            expected_added=1,
            extra_patches=(
                patch.object(debt_generator, "generate_application", lambda _data: _OfflineDebtDocument()),
                patch.object(osc_debt, "_export_dir", lambda: str(debt_batch_root)),
                patch.object(osc_debt, "_file_meta", lambda path: {"url": f"/offline/{Path(path).name}"}),
            ),
        )

        debt_import_root = sandbox / "debt-write-auto-import"
        debt_import_root.mkdir(parents=True, exist_ok=True)
        asset_doc = debt_import_root / "asset-statement.docx"
        asset_doc.write_bytes(b"offline asset statement fixture\n")
        dispatch_file_transcript_case(
            rule="/api/osc/debt/auto-import",
            endpoint="osc_debt.debt_auto_import",
            path="/api/osc/debt/auto-import",
            tracked_root=debt_import_root,
            json_body={"asset_doc_path": str(asset_doc)},
            expected_response={
                "ok": True,
                "fixture": "auto-import",
                "imported_from": [asset_doc.name],
            },
            extra_patches=(
                patch.object(osc_debt, "_debt_allowed_import_roots", lambda: [str(debt_import_root)]),
                patch.object(
                    debt_generator,
                    "auto_import_from_docs",
                    lambda **_kwargs: {"fixture": "auto-import", "fields": {"name": "離線聲請人"}},
                ),
            ),
        )

        debt_merge_root = sandbox / "debt-write-merge"

        def merge_fixture_pdfs(_paths: Sequence[str], *, add_bookmarks: bool = True) -> str:
            debt_merge_root.mkdir(parents=True, exist_ok=True)
            output = debt_merge_root / "merged.pdf"
            output.write_bytes(b"%PDF-1.4\noffline merged fixture\n%%EOF\n")
            return str(output)

        def debt_merge_tempdir(*_args: Any, **_kwargs: Any) -> str:
            upload_root = debt_merge_root / "uploads"
            upload_root.mkdir(parents=True, exist_ok=True)
            return str(upload_root)

        merge_ephemeral_removals: list[dict[str, str]] = []

        def debt_merge_cleanup(path: str, *_args: Any, **_kwargs: Any) -> None:
            cleanup_root = Path(path).resolve()
            if not _is_within(cleanup_root, debt_merge_root):
                raise ReplayIsolationError(f"debt merge cleanup escaped sandbox: {cleanup_root}")
            for file in sorted(cleanup_root.rglob("*"), reverse=True):
                if file.is_file():
                    merge_ephemeral_removals.append(
                        {
                            "path": str(file.relative_to(debt_merge_root)),
                            "sha256": hashlib.sha256(file.read_bytes()).hexdigest(),
                        }
                    )
                    file.unlink()
                elif file.is_dir():
                    file.rmdir()
            cleanup_root.rmdir()

        dispatch_file_transcript_case(
            rule="/api/osc/debt/merge-pdf",
            endpoint="osc_debt.debt_merge_pdf",
            path="/api/osc/debt/merge-pdf",
            tracked_root=debt_merge_root,
            data={
                "files": (io.BytesIO(b"%PDF-1.4\noffline input\n%%EOF\n"), "input.pdf"),
                "add_bookmarks": "true",
            },
            content_type="multipart/form-data",
            expected_response={"ok": True, "filename": "merged.pdf", "message": "已合併 1 個檔案"},
            expected_added=1,
            extra_patches=(
                patch.object(debt_generator, "merge_debt_pdfs", merge_fixture_pdfs),
                patch.object(osc_debt, "_file_meta", lambda path: {"url": f"/offline/{Path(path).name}"}),
                patch.object(osc_debt.tempfile, "mkdtemp", debt_merge_tempdir),
                patch.object(osc_debt.shutil, "rmtree", debt_merge_cleanup),
            ),
        )
        if len(merge_ephemeral_removals) != 1:
            raise ContractValidationError(
                f"debt merge upload cleanup transcript drifted: {merge_ephemeral_removals}"
            )
        cases[-1]["side_effect_guard"]["file_transcript"]["ephemeral_removed"] = list(
            merge_ephemeral_removals
        )

        gcal_disconnect_root = sandbox / "gcal-disconnect"
        gcal_disconnect_root.mkdir(parents=True, exist_ok=True)
        gcal_token = gcal_disconnect_root / "token.json"
        gcal_token.write_text('{"fixture":"offline-token"}\n', encoding="utf-8")
        dispatch_file_transcript_case(
            rule="/api/osc/gcal/disconnect",
            endpoint="osc_gcal.gcal_disconnect",
            path="/api/osc/gcal/disconnect",
            tracked_root=gcal_disconnect_root,
            json_body={},
            expected_response={"ok": True},
            expected_removed=1,
            extra_patches=(patch.object(osc_gcal, "TOKEN_PATH", gcal_token),),
        )
        dispatch_read_only_case(
            "/api/osc/cases",
            "osc_cases.osc_cases_api",
            "/api/osc/cases?limit=7",
            {"ok": True, "items": []},
        )
        dispatch_read_only_case(
            "/api/osc/cases",
            "osc_cases.osc_cases_api",
            "/api/osc/cases",
            {"ok": False, "error": "client_name required"},
            expected_status=400,
            expected_database_calls=0,
            method="POST",
            json_body={},
        )
        dispatch_read_only_case(
            "/api/osc/insights",
            "osc_cases.osc_insights_api",
            "/api/osc/insights?limit=7",
            {"ok": True, "items": []},
            expected_database_calls=0,
        )
        dispatch_read_only_case(
            "/api/osc/insights",
            "osc_cases.osc_insights_api",
            "/api/osc/insights",
            {"ok": False, "error": "insight_text required"},
            expected_status=400,
            expected_database_calls=0,
            method="POST",
            json_body={},
        )
        insight_fixture = {"id": "offline-insight", "title": "Offline", "summary": "Fixture"}
        dispatch_read_only_case(
            "/api/osc/insights/<insight_id>",
            "osc_cases.osc_insight_detail_api",
            "/api/osc/insights/offline-insight",
            {"ok": True, "item": insight_fixture},
            insights=[insight_fixture],
            expected_database_calls=0,
        )
        dispatch_read_only_case(
            "/api/osc/archive-wizard/preview",
            "osc_cases.osc_archive_wizard_preview_api",
            "/api/osc/archive-wizard/preview?limit=7",
            {"ok": True, "items": [], "fixture": "archive-preview"},
            expected_database_calls=0,
        )
        dispatch_read_only_case(
            "/api/osc/archive-wizard/execute",
            "osc_cases.osc_archive_wizard_execute_api",
            "/api/osc/archive-wizard/execute",
            {"ok": False, "error": "confirm_required"},
            expected_status=400,
            expected_database_calls=0,
            method="POST",
            json_body={},
        )
        dispatch_read_only_case(
            "/api/osc/backups",
            "osc_cases.osc_backup_list",
            "/api/osc/backups",
            {"ok": True, "items": []},
            expected_database_calls=0,
        )
        dispatch_read_only_case(
            "/api/osc/backups/<filename>/restore",
            "osc_cases.osc_backup_restore",
            "/api/osc/backups/not-valid/restore",
            {"ok": False, "error": "Invalid filename"},
            expected_status=400,
            expected_database_calls=0,
            method="POST",
            json_body={},
        )
        dispatch_read_only_case(
            "/api/osc/template-folder",
            "osc_cases.osc_template_folder_api",
            "/api/osc/template-folder",
            {
                "ok": True,
                "folder_path": "",
                "local_folder": "",
                "exists": False,
                "candidates": [],
                "smb_candidates": [],
                "case": None,
                "base_path": "",
                "current_path": "",
                "current_relative_path": "",
                "parent_relative_path": "",
                "entries": [],
            },
        )
        dispatch_read_only_case(
            "/api/osc/debt/source-status",
            "osc_debt.debt_source_status",
            "/api/osc/debt/source-status",
            {"ok": True, "fixture": "robot-source"},
            expected_database_calls=0,
        )
        dispatch_read_only_case(
            "/api/osc/debt/import-candidates",
            "osc_debt.debt_import_candidates",
            "/api/osc/debt/import-candidates",
            {"ok": True, "items": [], "fixture": "debt-import"},
            expected_database_calls=0,
        )
        dispatch_read_only_case(
            "/api/osc/debt/address-data",
            "osc_debt.debt_address_data",
            "/api/osc/debt/address-data",
            {"ok": True, "courts": [], "creditors": [], "fixture": "address-options"},
            expected_database_calls=0,
        )
        scan_row = {"id": "offline-case", "client_name": "Fixture Client", "folder_path": ""}
        dispatch_read_only_case(
            "/api/osc/debt/scan-evidence/<case_id>",
            "osc_debt.debt_scan_evidence",
            "/api/osc/debt/scan-evidence/offline-case",
            {
                "ok": False,
                "error": "此案件尚未設定資料夾路徑",
                "case": {"id": "offline-case", "client_name": "Fixture Client"},
            },
            one_row=scan_row,
            expected_status=400,
        )
        dispatch_read_only_case(
            "/api/osc/accounting/import/google-sheet",
            "osc_accounting.osc_accounting_google_sheet_import_api",
            "/api/osc/accounting/import/google-sheet?month=2026-07",
            {"ok": True, "fixture": "sheet-import", "dry_run": True},
            expected_database_calls=0,
        )
        monthly_bonus = {
            "ok": True,
            "fixture": "monthly-bonus",
            "month": "2026-07",
            "commit": False,
            "rows": [],
        }
        dispatch_read_only_case(
            "/api/osc/accounting/monthly-bonus",
            "osc_accounting.osc_accounting_monthly_bonus_api",
            "/api/osc/accounting/monthly-bonus?month=2026-07&refresh_import=0",
            monthly_bonus,
            expected_database_calls=0,
        )
        dispatch_read_only_case(
            "/api/osc/accounting/monthly-bonus/xlsx",
            "osc_accounting.osc_accounting_monthly_bonus_xlsx_api",
            "/api/osc/accounting/monthly-bonus/xlsx?month=2026-07",
            None,
            expected_database_calls=0,
            validator=lambda response: (
                response.get_data() == bonus_body,
                {
                    "kind": "body_exact_sha256",
                    "expected_sha256": _digest(bonus_body),
                    "observed_sha256": _digest(response.get_data()),
                },
            ),
        )
        binary_xlsx_validator = lambda response: (
            response.get_data().startswith(b"PK") and len(response.get_data()) > 200,
            {
                "kind": "xlsx_zip_shape",
                "expected_prefix": "PK",
                "observed_prefix_hex": response.get_data()[:2].hex(),
                "observed_length": len(response.get_data()),
            },
        )
        dispatch_read_only_case(
            "/api/osc/accounting/transactions/xlsx",
            "osc_accounting.osc_accounting_transactions_xlsx_api",
            "/api/osc/accounting/transactions/xlsx?limit=7",
            None,
            validator=binary_xlsx_validator,
        )
        case_row = {
            "id": "offline-case",
            "case_number": "OFF-001",
            "client_name": "Fixture Client",
            "folder_path": "",
            "status": "進行中",
        }
        dispatch_read_only_case(
            "/api/osc/cases/<row_id>",
            "osc_cases.osc_case_detail_api",
            "/api/osc/cases/offline-case",
            None,
            one_row=case_row,
            validator=_json_deep_subset({"ok": True, "item": {"id": "offline-case", "case_number": "OFF-001"}}),
        )
        dispatch_read_only_case(
            "/api/osc/cases/export-csv",
            "osc_cases.osc_cases_export_csv_api",
            "/api/osc/cases/export-csv",
            None,
            validator=_body_contains("案件編號", "當事人"),
        )
        dispatch_read_only_case(
            "/api/osc/cases/export-xlsx",
            "osc_cases.osc_cases_export_xlsx_api",
            "/api/osc/cases/export-xlsx",
            None,
            validator=binary_xlsx_validator,
        )
        dispatch_read_only_case(
            "/api/osc/clients/export-csv",
            "osc_cases.osc_clients_export_csv_api",
            "/api/osc/clients/export-csv",
            None,
            validator=_body_contains("姓名", "電話", "地址"),
        )
        dashboard_expected = {
            "ok": True,
            "window": {"start_date": "2026-07-01", "end_date": "2026-07-31"},
            "stats": {
                "active_cases": 0,
                "legal_aid_cases": 0,
                "monthly_revenue": 0.0,
                "monthly_expense": 0.0,
                "closed_regular": 0,
                "closed_legal_aid": 0,
            },
            "recent_cases": [],
            "pending_todos": [],
            "pending_osc_todos": [],
            "pending_calendar_todos": [],
            "upcoming_calendar": [],
            "recent_activity": [],
            "recent_pdf_logs": [],
        }
        dispatch_read_only_case(
            "/api/osc/dashboard",
            "osc_cases.osc_dashboard_api",
            "/api/osc/dashboard",
            dashboard_expected,
            one_row={"c": 0},
            expected_database_calls=11,
        )
        gcal_health = {
            "status": "missing_token",
            "message": "offline fixture",
            "path": str(sandbox / "missing-token.json"),
            "expires_at": None,
            "expires_in_hours": None,
            "refresh_token_present": False,
            "scopes_ok": False,
            "account_check_status": "not_checked",
            "required": True,
            "next_action": "authorize",
        }
        dispatch_read_only_case(
            "/api/osc/gcal/status",
            "osc_gcal.gcal_status",
            "/api/osc/gcal/status",
            {
                "ok": False,
                "connected": False,
                "healthy": False,
                "reason": "missing_token",
                "error": "offline fixture",
                "next_action": "authorize",
                "token_health": gcal_health,
                "calendar_id": "offline-calendar",
                "import_calendar_ids": "offline-imports",
            },
            expected_database_calls=0,
        )
        dispatch_read_only_case(
            "/api/osc/cases/<row_id>/folder-path",
            "osc_cases.osc_case_folder_path_api",
            "/api/osc/cases/offline-case/folder-path",
            {
                "ok": False,
                "error_kind": "folder_path_empty",
                "message": "案件未設定資料夾路徑，請先用「建立資料夾」按鈕建立預設結構。",
                "case": {"id": "offline-case", "case_number": "OFF-001", "client_name": "Fixture Client"},
            },
            one_row=case_row,
        )
        dispatch_read_only_case(
            "/api/osc/cases/<row_id>/file-search",
            "osc_cases.osc_case_file_search_api",
            "/api/osc/cases/offline-case/file-search?q=fixture",
            None,
            one_row=case_row,
            validator=_json_deep_subset(
                {
                    "ok": False,
                    "error": "folder_path_empty",
                    "items": [],
                    "case": {"id": "offline-case", "case_number": "OFF-001"},
                }
            ),
        )
        dispatch_read_only_case(
            "/api/osc/cases/<row_id>/folder-browser",
            "osc_cases.osc_case_folder_browser_api",
            "/api/osc/cases/offline-case/folder-browser",
            {"ok": False, "error": "folder_path_empty"},
            one_row=case_row,
            expected_status=400,
        )
        dispatch_read_only_case(
            "/api/osc/cases/<row_id>/workbench",
            "osc_cases.osc_case_workbench_api",
            "/api/osc/cases/offline-case/workbench",
            None,
            one_row=case_row,
            expected_database_calls=10,
            validator=_json_deep_subset(
                {
                    "ok": True,
                    "case": {"id": "offline-case", "case_number": "OFF-001"},
                    "stats": {
                        "todo_total": 0,
                        "meeting_total": 0,
                        "docs_indexed": 0,
                        "opponents_total": 0,
                        "pdf_logs_total": 0,
                    },
                    "todos": [],
                    "meetings": [],
                    "documents": [],
                    "opponents": [],
                    "pdf_generation_log": [],
                }
            ),
        )
        dispatch_read_only_case(
            "/api/osc/cases/<row_id>/address-label",
            "osc_cases.osc_case_address_label",
            "/api/osc/cases/offline-case/address-label",
            {"ok": False, "error": "請選擇法院、對造或法扶分會。"},
            expected_status=400,
            expected_database_calls=0,
        )
        quotation_body = b"offline quotation pdf\n"
        dispatch_read_only_case(
            "/api/osc/quotations/<row_id>/export-pdf",
            "osc_cases.osc_quotation_export_pdf",
            "/api/osc/quotations/offline-quotation/export-pdf",
            None,
            one_row={"id": "offline-quotation", "client_name": "Fixture Client"},
            validator=lambda response: (
                response.get_data() == quotation_body,
                {
                    "kind": "body_exact_sha256",
                    "expected_sha256": _digest(quotation_body),
                    "observed_sha256": _digest(response.get_data()),
                },
            ),
        )
        dispatch_read_only_case(
            "/api/osc/cases/import-csv",
            "osc_cases.osc_cases_import_csv_api",
            "/api/osc/cases/import-csv",
            {"ok": False, "error": "file required"},
            expected_status=400,
            expected_database_calls=0,
            method="POST",
        )
        dispatch_read_only_case(
            "/api/osc/clients/import-csv",
            "osc_cases.osc_clients_import_csv_api",
            "/api/osc/clients/import-csv",
            {"ok": False, "error": "file required"},
            expected_status=400,
            expected_database_calls=0,
            method="POST",
        )

        import api.osc.hearing_conflict_runtime as hearing_runtime

        def append_hearing_case(case: dict[str, Any], fixture: str) -> None:
            case["side_effect_guard"] = {
                "fixture_database_calls": 0,
                "statement_kinds": [],
                "database_mutations": 0,
                "fixture": fixture,
                "socket.connect": 0,
                "socket.bind": 0,
                "socket.create_connection": 0,
                "socket.getaddrinfo": 0,
                "subprocess.Popen": 0,
            }
            cases.append(case)

        with patch.object(hearing_runtime, "load_existing_schedules", lambda *_args, **_kwargs: []):
            append_hearing_case(
                _invoke_case(
                    app,
                    service="5002",
                    rule="/api/osc/hearing-conflicts",
                    method="GET",
                    endpoint="osc_cases.osc_hearing_conflicts_api",
                    path="/api/osc/hearing-conflicts",
                    expected_status=200,
                    validator=_json_deep_subset(
                        {"ok": True, "candidate_count": 0, "conflict_count": 0, "items": [], "truncated": False}
                    ),
                ),
                "bounded_empty_schedule_projection",
            )

        append_hearing_case(
            _invoke_case(
                app,
                service="5002",
                rule="/api/osc/hearing-conflicts/check",
                method="POST",
                endpoint="osc_cases.osc_hearing_conflicts_check_api",
                path="/api/osc/hearing-conflicts/check",
                expected_status=200,
                json_body={
                    "candidate": {
                        "case_number": "OFF-001",
                        "title": "（待確認）臺灣高等法院刑事開庭",
                        "start_date": "2026-08-05T10:00",
                        "end_date": "2026-08-05T11:00",
                    }
                },
                validator=_json_deep_subset(
                    {"ok": True, "excluded": True, "conflict_count": 0, "items": []}
                ),
            ),
            "tentative_candidate_fail_closed",
        )

        generated_fixture = {
            "ok": True,
            "created": True,
            "path": str(sandbox / "hearing-draft.docx"),
            "document_id": "offline-hearing-doc",
            "file_name": "hearing-draft.docx",
            "case_number": "OFF-001",
            "generation_mode": "manual",
        }
        with patch.object(
            hearing_runtime,
            "load_case",
            lambda **_kwargs: {"id": "offline-case", "case_number": "OFF-001", "client_name": "Fixture Client"},
        ), patch.object(
            hearing_runtime,
            "generate_manual",
            lambda *_args, **_kwargs: dict(generated_fixture),
        ), patch.object(osc_cases, "_osc_log_activity", lambda *_args, **_kwargs: None):
            append_hearing_case(
                _invoke_case(
                    app,
                    service="5002",
                    rule="/api/osc/hearing-conflicts/generate",
                    method="POST",
                    endpoint="osc_cases.osc_hearing_conflicts_generate_api",
                    path="/api/osc/hearing-conflicts/generate",
                    expected_status=200,
                    json_body={
                        "mode": "manual",
                        "case_number": "OFF-001",
                        "target_start": "2026-08-05T10:00",
                        "prior_start": "2026-08-05T10:30",
                        "prior_court_name": "Fixture Court",
                    },
                    validator=_json_deep_subset(
                        {
                            "ok": True,
                            "document_id": "offline-hearing-doc",
                            "generation_mode": "manual",
                            "download_url": "/api/osc/hearing-conflicts/download?document_id=offline-hearing-doc",
                        }
                    ),
                ),
                "sandboxed_manual_generation_adapter",
            )

        download_docx = sandbox / "hearing-download.docx"
        download_body = b"offline hearing DOCX fixture\n"
        download_docx.write_bytes(download_body)
        with patch.object(
            osc_cases,
            "_osc_exec",
            lambda *_args, **_kwargs: (
                {"id": "offline-hearing-doc", "file_name": "hearing-download.docx", "file_path": str(download_docx)},
                {},
            ),
        ), patch.object(
            osc_cases,
            "_osc_resolve_existing_local_path",
            lambda *_args, **_kwargs: str(download_docx),
        ):
            append_hearing_case(
                _invoke_case(
                    app,
                    service="5002",
                    rule="/api/osc/hearing-conflicts/download",
                    method="GET",
                    endpoint="osc_cases.osc_hearing_conflicts_download_api",
                    path="/api/osc/hearing-conflicts/download?document_id=offline-hearing-doc",
                    expected_status=200,
                    validator=lambda response: (
                        response.get_data() == download_body,
                        {
                            "kind": "body_exact_sha256",
                            "expected_sha256": _digest(download_body),
                            "observed_sha256": _digest(response.get_data()),
                        },
                    ),
                ),
                "tenant_bound_document_id_download",
            )
    write_db.close()
    return cases


def _worker_report(sandbox: Path, live_root: Path) -> dict[str, Any]:
    sandbox = sandbox.resolve(strict=True)
    live_root = live_root.expanduser().resolve()
    # The immutable external runtime intentionally contains a .pth entry for
    # the active V2 checkout.  Python can therefore pre-import ``api`` from
    # LIVE before this harness runs.  Drop that namespace in the isolated
    # worker so the release/source root at sys.path[0] is authoritative.
    for module_name in tuple(sys.modules):
        if module_name == "api" or module_name.startswith("api."):
            sys.modules.pop(module_name, None)
    importlib.invalidate_caches()
    _worker_python, worker_roots = _worker_runtime()
    audit_attempts = _install_worker_audit_guard(
        sandbox,
        live_root,
        allowed_live_read_roots=_worker_live_read_roots(worker_roots),
    )
    blocked_attempts: Counter[str] = Counter()
    suppressed_stdout = io.StringIO()

    with redirect_stdout(suppressed_stdout), ExitStack() as stack:
        stack.enter_context(patch.object(socket.socket, "connect", _blocked_operation("socket.connect", blocked_attempts)))
        stack.enter_context(patch.object(socket.socket, "bind", _blocked_operation("socket.bind", blocked_attempts)))
        stack.enter_context(patch.object(socket, "create_connection", _blocked_operation("socket.create_connection", blocked_attempts)))
        stack.enter_context(patch.object(socket, "getaddrinfo", _blocked_operation("socket.getaddrinfo", blocked_attempts)))
        stack.enter_context(patch.object(subprocess, "Popen", _blocked_operation("subprocess.Popen", blocked_attempts)))

        class _OfflineInferenceGateway:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

        class _OfflineServerOrchestrator:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

        class _OfflineAutoSkill:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                pass

            def stats(self) -> dict[str, Any]:
                return {"documents": 0, "fixture": "in-memory"}

        from flask import Blueprint
        import skills.ops.iron_dome_sync as iron_dome_sync

        offline_telegram_blueprint = Blueprint("telegram", "offline-telegram")

        import_stubs = {
            "skills.research.web_research": _fake_module(
                "skills.research.web_research",
                search_web=lambda *_args, **_kwargs: [],
                research_topic=lambda *_args, **_kwargs: {},
                fetch_url_content=lambda *_args, **_kwargs: {},
            ),
            "skills.evolution.skill_genesis": _fake_module(
                "skills.evolution.skill_genesis",
                list_skills=lambda: ["offline-skill"],
                generate_skill=lambda *_args, **_kwargs: {"success": False, "offline": True},
                list_skill_versions=lambda _skill: {
                    "success": True,
                    "versions": [{"id": "offline-v1", "fixture": True}],
                },
                get_skill_release_state=lambda _skill: {"success": False},
                get_skill_runtime_stats=lambda limit=200: {"success": True, "events": [], "limit": limit},
                list_iron_dome_patterns=lambda **kwargs: {
                    "success": True,
                    "patterns": [],
                    "options": kwargs,
                },
            ),
            "skills.bridge.melchior_bridge": _fake_module(
                "skills.bridge.melchior_bridge", analyze_image=lambda *_args, **_kwargs: {}
            ),
            "skills.bridge.inference_gateway": _fake_module(
                "skills.bridge.inference_gateway", InferenceGateway=_OfflineInferenceGateway
            ),
            "api.orchestrator": _fake_module(
                "api.orchestrator",
                Orchestrator=_OfflineServerOrchestrator,
            ),
            "api.admin_allowlist": _fake_module(
                "api.admin_allowlist",
                get_line_admin_user_ids=lambda: set(),
                get_discord_admin_ids=lambda: set(),
            ),
            "api.webhooks.line": _fake_module(
                "api.webhooks.line",
                init_line_module=lambda **_kwargs: None,
                _read_attachment_job=lambda *_args, **_kwargs: None,
                _list_attachment_job_ids=lambda: [],
            ),
            "api.webhooks.telegram": _fake_module(
                "api.webhooks.telegram",
                telegram_bp=offline_telegram_blueprint,
            ),
            "skills.ops.config": _fake_module(
                "skills.ops.config",
                validate_config=lambda: None,
            ),
            "skills.ops.iron_dome_sync": _fake_module(
                "skills.ops.iron_dome_sync",
                register_iron_dome_routes=lambda _app: None,
            ),
            "skills.management.auto_skill": _fake_module(
                "skills.management.auto_skill",
                AutoSkill=_OfflineAutoSkill,
            ),
            "skills.memory.job_queue": _fake_module(
                "skills.memory.job_queue",
                read=lambda _job_id: None,
            ),
            "skills.bridge.balthasar_bridge": _fake_module(
                "skills.bridge.balthasar_bridge",
                summarize_text=lambda *_args, **_kwargs: "",
                check_health=lambda: (False, "offline"),
            ),
            "skills.bridge.melchior_manager": _fake_module(
                "skills.bridge.melchior_manager",
                sync_skills_to_melchior=lambda *_args, **_kwargs: {},
                melchior_health=lambda: {"ok": False, "offline": True},
            ),
            "skills.management.skill_interview": _fake_module(
                "skills.management.skill_interview",
                list_interview_history=lambda limit=10: [],
            ),
        }
        stack.enter_context(patch.dict(sys.modules, import_stubs))

        import api.authz as authz
        import api.tools_api as tools_api
        import api.blueprints.admin_runtime as admin_runtime
        import api.blueprints.dashboard_pages as dashboard_pages
        import api.blueprints.golem_console as golem_console
        import api.blueprints.osc_accounting as osc_accounting
        import api.blueprints.osc_cases as osc_cases
        import api.blueprints.osc_debt as osc_debt
        import api.blueprints.osc_files as osc_files
        import api.blueprints.osc_gcal as osc_gcal
        import api.blueprints.osc_pdf as osc_pdf
        import api.blueprints.raziel as raziel
        import api.blueprints.osc_settings as osc_settings
        import api.blueprints.web_runtime as web_runtime
        import api.blueprints.lottery as lottery
        import api.debt_document_generator as debt_generator
        import api.osc.utils as osc_utils
        import api.server as server
        # server imports an empty Telegram blueprint so its broad startup stays
        # isolated.  Load the real extracted blueprint only after server is
        # stable, and suppress its import-time already-exists directory call.
        sys.modules.pop("api.webhooks.telegram", None)
        with patch.object(os, "makedirs", lambda *_args, **_kwargs: None):
            telegram = importlib.import_module("api.webhooks.telegram")
        skill_genesis = sys.modules["skills.evolution.skill_genesis"]

        actual_modules = {
            module.__name__: module
            for module in (
                authz,
                tools_api,
                admin_runtime,
                dashboard_pages,
                golem_console,
                osc_accounting,
                osc_cases,
                osc_debt,
                osc_files,
                osc_gcal,
                osc_pdf,
                raziel,
                osc_settings,
                web_runtime,
                lottery,
                debt_generator,
                osc_utils,
                server,
                telegram,
                iron_dome_sync,
            )
        }
        module_origins: dict[str, str] = {}
        repo_root = REPO_ROOT.resolve(strict=True)
        for module_name, module in actual_modules.items():
            raw_origin = str(getattr(module, "__file__", "") or "").strip()
            if not raw_origin:
                raise ContractValidationError(f"actual handler module has no origin: {module_name}")
            origin = Path(raw_origin).resolve(strict=True)
            if not _is_within(origin, repo_root):
                raise ContractValidationError(
                    f"actual handler module escaped candidate/repo: {module_name}={origin}"
                )
            if _is_within(origin, live_root) and not _is_installed_release_root(
                repo_root, live_root
            ):
                raise ContractValidationError(
                    f"actual handler module resolved from live V2: {module_name}={origin}"
                )
            module_origins[module_name] = str(origin.relative_to(repo_root))

        inventory = RouteInventory.load()
        verify_loaded_surface(tools_api.app, "5003", inventory)

        exports = sandbox / "exports"
        exports.mkdir(exist_ok=True)
        proof_body = b"synthetic non-sensitive export fixture\n"
        (exports / "proof.txt").write_bytes(proof_body)
        tools_api.EXPORTS_DIR = str(exports)
        server.EXPORTS_DIR = str(exports)

        document_reads: list[str] = []

        def read_offline_document(path: str, **kwargs: Any) -> Any:
            candidate = Path(path).resolve(strict=True)
            if not _is_within(candidate, sandbox / "tmp"):
                raise ReplayIsolationError("PDF text fixture escaped the worker temp directory")
            if not candidate.read_bytes().startswith(b"%PDF"):
                raise ContractValidationError("PDF text fixture lost its PDF signature")
            if kwargs != {"mode": "auto", "ocr_fallback": True, "max_chars": 500_000}:
                raise ContractValidationError("PDF text document-reader contract drifted")
            document_reads.append(candidate.name)
            return type(
                "OfflineDocumentResult",
                (),
                {
                    "success": True,
                    "text": "offline pdf text fixture",
                    "error": "",
                },
            )()

        fake_modules = {
            "api.admin_allowlist": _fake_module(
                "api.admin_allowlist",
                get_line_admin_user_ids=lambda: set(),
                get_discord_admin_ids=lambda: set(),
            ),
            "skills.memory.job_queue": _fake_module("skills.memory.job_queue", read=lambda _job_id: None),
            "skills.magi.council_approval": _fake_module(
                "skills.magi.council_approval",
                list_pending_core_changes=lambda limit=20: {"success": True, "pending": [], "limit": limit},
            ),
            "skills.law_firm.manage_clients": _fake_module(
                "skills.law_firm.manage_clients",
                query_clients=lambda keyword: [{"code": "OFFLINE", "keyword": keyword}],
            ),
            "skills.law_firm.manage_meetings": _fake_module(
                "skills.law_firm.manage_meetings",
                list_meetings=lambda date=None: [{"title": "offline", "date": date}],
            ),
            "skills.bridge.legal_bridge": _fake_module(
                "skills.bridge.legal_bridge",
                SCRIPTS={"offline_contract": "/synthetic/not-executed"},
            ),
            "api.db_helper": _fake_module("api.db_helper", get_cursor=_fake_cursor),
            "api.routing.node_registry": _fake_module(
                "api.routing.node_registry",
                get_node_ip=lambda _name: "",
            ),
            "skills.bridge.http_pool": _fake_module(
                "skills.bridge.http_pool",
                get_session=lambda: object(),
            ),
            "skills.management.auto_skill": _fake_module(
                "skills.management.auto_skill",
                AutoSkill=type(
                    "OfflineAutoSkill",
                    (),
                    {"stats": lambda self: {"documents": 0, "fixture": "in-memory"}},
                ),
            ),
            "skills.engine.document_reader": _fake_module(
                "skills.engine.document_reader",
                read_document=read_offline_document,
            ),
        }
        stack.enter_context(patch.dict(sys.modules, fake_modules))
        stack.enter_context(patch.object(authz, "_check_api_key", lambda _value: True))
        stack.enter_context(patch.object(authz, "_log_access", lambda *_args, **_kwargs: None))
        stack.enter_context(patch.object(tools_api, "list_skills", lambda: ["offline-skill"]))
        stack.enter_context(patch.object(tools_api, "_check_external_api_key", lambda: (True, "")))
        stack.enter_context(patch.object(tools_api, "_resolve_external_api_key", lambda: "offline-key"))
        stack.enter_context(
            patch.object(
                tools_api,
                "_tools_health_snapshot",
                lambda fresh=False: {"ok": True, "status": "ready", "fresh": bool(fresh)},
            )
        )
        stack.enter_context(
            patch.object(
                skill_genesis,
                "get_skill_runtime_stats",
                lambda limit=200: {"success": True, "events": [], "limit": limit},
            )
        )
        stack.enter_context(
            patch.object(
                skill_genesis,
                "list_iron_dome_patterns",
                lambda **kwargs: {"success": True, "patterns": [], "options": kwargs},
            )
        )

        auth = {"X-API-Key": "offline-contract-key"}
        cases = [
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/static/exports/<path:filename>",
                method="GET",
                endpoint="static_exports",
                path="/static/exports/proof.txt",
                expected_status=200,
                validator=lambda response: (
                    response.get_data() == proof_body,
                    {
                        "kind": "body_exact_sha256",
                        "expected_sha256": _digest(proof_body),
                        "observed_sha256": _digest(response.get_data()),
                    },
                ),
                headers=auth,
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/livez",
                method="GET",
                endpoint="livez",
                path="/livez",
                expected_status=200,
                validator=_json_subset({"ok": True, "status": "live", "service": "MAGI Tools API", "probe": "liveness"}),
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/osc/external/health",
                method="GET",
                endpoint="external_osc_health",
                path="/osc/external/health",
                expected_status=200,
                validator=_json_exact(
                    {
                        "success": True,
                        "service": "OSC/CASPER external gateway",
                        "orchestrator_ready": True,
                        "api_key_required": True,
                        "status": "ready",
                    }
                ),
                headers=auth,
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/osc/external/ui",
                method="GET",
                endpoint="external_osc_ui",
                path="/osc/external/ui",
                expected_status=200,
                validator=_body_contains("CASPER OSC 外部對話介面", "/osc/external/chat"),
                headers=auth,
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/connections",
                method="GET",
                endpoint="connections_status",
                path="/connections",
                expected_status=200,
                validator=_json_subset(
                    {
                        "policy": {"internet_allowed": False, "cloud_models_allowed": False},
                    }
                ),
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/skills",
                method="GET",
                endpoint="api_list_skills",
                path="/skills",
                expected_status=200,
                validator=_json_exact({"skills": ["offline-skill"]}),
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/jobs/<job_id>",
                method="GET",
                endpoint="api_get_job",
                path="/jobs/bad",
                expected_status=400,
                validator=_json_exact({"error": "invalid_job_id", "job_id": "bad"}),
                headers=auth,
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/skills/versions",
                method="POST",
                endpoint="api_skill_versions",
                path="/skills/versions",
                expected_status=400,
                validator=_json_exact({"error": "Missing 'skill' parameter"}),
                headers=auth,
                json_body={},
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/skills/release",
                method="GET",
                endpoint="api_skill_release_state",
                path="/skills/release",
                expected_status=400,
                validator=_json_exact({"error": "Missing 'skill' query parameter"}),
                headers=auth,
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/skills/events",
                method="GET",
                endpoint="api_skill_events",
                path="/skills/events?limit=3",
                expected_status=200,
                validator=_json_exact({"success": True, "events": [], "limit": 3}),
                headers=auth,
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/iron-dome/patterns",
                method="GET",
                endpoint="api_iron_dome_patterns_list",
                path="/iron-dome/patterns?limit=2",
                expected_status=200,
                validator=_json_subset({"success": True, "patterns": []}),
                headers=auth,
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/council/core/pending",
                method="GET",
                endpoint="api_council_core_pending",
                path="/council/core/pending?limit=2",
                expected_status=200,
                validator=_json_exact({"success": True, "pending": [], "limit": 2}),
                headers=auth,
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/clients",
                method="GET",
                endpoint="api_query_clients",
                path="/clients?q=offline",
                expected_status=200,
                validator=_json_exact([{"code": "OFFLINE", "keyword": "offline"}]),
                headers=auth,
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/meetings",
                method="GET",
                endpoint="api_list_meetings",
                path="/meetings?date=2026-07-14",
                expected_status=200,
                validator=_json_exact([{"title": "offline", "date": "2026-07-14"}]),
                headers=auth,
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/legal",
                method="GET",
                endpoint="api_legal_skills_list",
                path="/legal",
                expected_status=200,
                validator=_json_exact({"skills": ["offline_contract"]}),
                headers=auth,
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/definitions",
                method="GET",
                endpoint="api_definitions",
                path="/definitions",
                expected_status=200,
                validator=_definitions_contract,
                headers=auth,
            ),
            _invoke_case(
                tools_api.app,
                service="5003",
                rule="/api/audit_log",
                method="GET",
                endpoint="api_list_audit_log",
                path="/api/audit_log?limit=2&days=1",
                expected_status=200,
                validator=_json_exact(
                    {"entries": [], "count": 0, "filters": {"limit": 2, "days": 1}}
                ),
                headers=auth,
            ),
        ]
        pdf_temp_before = {
            path.name for path in (sandbox / "tmp").glob("shortcut_*.pdf")
        }
        isolation_before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
        pdf_case = _invoke_case(
            tools_api.app,
            service="5003",
            rule="/shortcut/pdf_text",
            method="POST",
            endpoint="api_shortcut_pdf_text",
            path="/shortcut/pdf_text",
            expected_status=200,
            validator=_body_contains("offline pdf text fixture"),
            headers=auth,
            data=b"%PDF-1.4\n% synthetic non-sensitive fixture\n",
            content_type="application/pdf",
            branch_class="representative_success_path",
        )
        isolation_after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
        isolation_delta = {
            key: isolation_after[key] - isolation_before[key]
            for key in isolation_before
        }
        pdf_temp_after = {
            path.name for path in (sandbox / "tmp").glob("shortcut_*.pdf")
        }
        if any(isolation_delta.values()) or pdf_temp_after != pdf_temp_before:
            raise ReplayIsolationError(
                "PDF text fixture crossed an isolation boundary or leaked temp files: "
                f"delta={isolation_delta} before={sorted(pdf_temp_before)} "
                f"after={sorted(pdf_temp_after)}"
            )
        if len(document_reads) != 1:
            raise ContractValidationError("PDF text fixture did not dispatch the document reader once")
        pdf_case["side_effect_guard"] = {
            "fixture_database_calls": 0,
            "statement_kinds": [],
            "database_mutations": 0,
            "fixture": "temporary_pdf_in_memory_reader",
            "document_reader_calls": len(document_reads),
            "temporary_files_remaining": len(pdf_temp_after),
            **isolation_delta,
        }
        cases.append(pdf_case)
        sixth_tools_specs = (
            (
                "/health",
                "health",
                "/health",
                _json_exact({"ok": True, "status": "ready", "fresh": False}),
            ),
            (
                "/melchior/health",
                "api_melchior_health",
                "/melchior/health",
                _json_exact({"ok": False, "offline": True}),
            ),
            (
                "/sages",
                "sages_status",
                "/sages",
                _json_exact(
                    {
                        "casper": {"online": True, "status": "ready", "role": "Decision & Governor"},
                        "melchior": {
                            "online": False,
                            "role": "Scientist (Vision/Code)",
                            "gpu": "RTX 3060",
                        },
                        "balthasar": {
                            "online": False,
                            "role": "Council (Review Only)",
                            "council_only": True,
                            "remote_enabled": False,
                            "message": "offline",
                            "proxy_on_casper": {"summarize": True, "transcribe": True},
                        },
                    }
                ),
            ),
            (
                "/skills/knowledge/stats",
                "api_skill_knowledge_stats",
                "/skills/knowledge/stats",
                _json_exact({"documents": 0, "fixture": "in-memory"}),
            ),
            (
                "/summarize/health",
                "api_summarize_health",
                "/summarize/health",
                _json_subset({"success": True}),
            ),
        )
        for rule, endpoint, path, validator in sixth_tools_specs:
            before = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            case = _invoke_case(
                tools_api.app,
                service="5003",
                rule=rule,
                method="GET",
                endpoint=endpoint,
                path=path,
                expected_status=200,
                validator=validator,
                headers=auth,
            )
            after = _isolation_attempt_snapshot(audit_attempts, blocked_attempts)
            delta = {key: after[key] - before[key] for key in before}
            if any(delta.values()):
                raise ReplayIsolationError(
                    f"tools in-memory replay crossed an isolation boundary for {endpoint}: {delta}"
                )
            case["side_effect_guard"] = {
                "fixture_database_calls": 0,
                "statement_kinds": [],
                "database_mutations": 0,
                "fixture": "in_memory_dependencies",
                **delta,
            }
            cases.append(case)
        cases.append(_admin_livez_case(sandbox))
        cases.extend(
            _admin_in_memory_cases(
                sandbox,
                audit_attempts=audit_attempts,
                blocked_attempts=blocked_attempts,
            )
        )
        cases.extend(
            _dashboard_projection_cases(
                dashboard_pages,
                audit_attempts=audit_attempts,
                blocked_attempts=blocked_attempts,
            )
        )
        cases.extend(
            _golem_in_memory_cases(
                golem_console,
                sandbox=sandbox,
                audit_attempts=audit_attempts,
                blocked_attempts=blocked_attempts,
            )
        )
        cases.extend(
            _server_projection_cases(
                server,
                lottery,
                sandbox,
                audit_attempts=audit_attempts,
                blocked_attempts=blocked_attempts,
            )
        )
        cases.extend(
            _toolsapi_proxy_cases(
                server,
                audit_attempts=audit_attempts,
                blocked_attempts=blocked_attempts,
            )
        )
        cases.extend(
            _web_runtime_isolated_cases(
                web_runtime,
                sandbox,
                audit_attempts=audit_attempts,
                blocked_attempts=blocked_attempts,
            )
        )
        cases.extend(
            _iron_dome_read_only_cases(
                iron_dome_sync,
                audit_attempts=audit_attempts,
                blocked_attempts=blocked_attempts,
            )
        )
        cases.extend(
            _osc_file_root_cases(
                osc_files,
                sandbox,
                audit_attempts=audit_attempts,
                blocked_attempts=blocked_attempts,
            )
        )
        cases.extend(
            _osc_public_share_case(
                osc_files,
                sandbox,
                audit_attempts=audit_attempts,
                blocked_attempts=blocked_attempts,
            )
        )
        cases.extend(
            _raziel_read_only_cases(
                raziel,
                sandbox,
                audit_attempts=audit_attempts,
                blocked_attempts=blocked_attempts,
            )
        )
        cases.extend(
            _telegram_get_probe_case(
                telegram,
                audit_attempts=audit_attempts,
                blocked_attempts=blocked_attempts,
            )
        )
        cases.extend(
            _osc_read_only_cases(
                osc_cases,
                osc_settings,
                osc_accounting,
                osc_debt,
                osc_gcal,
                osc_pdf,
                debt_generator,
                osc_utils,
                sandbox=sandbox,
                audit_attempts=audit_attempts,
                blocked_attempts=blocked_attempts,
            )
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "worker": "actual_handlers_in_isolated_process",
        "module_origins": module_origins,
        "handler_source_sha256": {
            "api/tools_api.py": _digest(Path(tools_api.__file__).resolve().read_bytes()),
            "api/blueprints/admin_runtime.py": _digest(Path(admin_runtime.__file__).resolve().read_bytes()),
            "api/blueprints/dashboard_pages.py": _digest(Path(dashboard_pages.__file__).resolve().read_bytes()),
            "api/blueprints/golem_console.py": _digest(Path(golem_console.__file__).resolve().read_bytes()),
            "api/blueprints/lottery.py": _digest(Path(lottery.__file__).resolve().read_bytes()),
            "api/blueprints/osc_accounting.py": _digest(Path(osc_accounting.__file__).resolve().read_bytes()),
            "api/blueprints/osc_cases.py": _digest(Path(osc_cases.__file__).resolve().read_bytes()),
            "api/blueprints/osc_debt.py": _digest(Path(osc_debt.__file__).resolve().read_bytes()),
            "api/blueprints/osc_gcal.py": _digest(Path(osc_gcal.__file__).resolve().read_bytes()),
            "api/blueprints/osc_pdf.py": _digest(Path(osc_pdf.__file__).resolve().read_bytes()),
            "api/blueprints/osc_settings.py": _digest(Path(osc_settings.__file__).resolve().read_bytes()),
            "api/blueprints/raziel.py": _digest(Path(raziel.__file__).resolve().read_bytes()),
            "api/blueprints/web_runtime.py": _digest(Path(web_runtime.__file__).resolve().read_bytes()),
            "api/webhooks/telegram.py": _digest(Path(telegram.__file__).resolve().read_bytes()),
            "api/debt_document_generator.py": _digest(Path(debt_generator.__file__).resolve().read_bytes()),
            "api/osc/utils.py": _digest(Path(osc_utils.__file__).resolve().read_bytes()),
            "api/server.py": _digest(Path(server.__file__).resolve().read_bytes()),
            "skills/ops/iron_dome_sync.py": _digest(
                Path(iron_dome_sync.__file__).resolve().read_bytes()
            ),
        },
        "tools_surface": {"service": "5003", "expected": 67, "actual": 67, "exact": True},
        "cases": cases,
        "case_count": len(cases),
        "all_cases_passed": all(case["passed"] for case in cases),
        "safety": {
            "listener_started": False,
            "network_connections_performed": 0,
            "blocked_network_attempts": sum(
                count for name, count in blocked_attempts.items() if name.startswith("socket.")
            ) + audit_attempts["socket.connect"] + audit_attempts["socket.bind"],
            "subprocess_attempts": blocked_attempts["subprocess.Popen"] + audit_attempts["subprocess.Popen"],
            "live_state_attempts": audit_attempts["live_read_or_write"],
            "writes_outside_sandbox": audit_attempts["write_outside_sandbox"],
            "mutations_outside_sandbox": audit_attempts["mutation_outside_sandbox"],
            "external_storage_roots": [
                str(root) for root in _external_storage_roots()
            ],
            "external_storage_access_attempts": audit_attempts[
                EXTERNAL_STORAGE_ACCESS_EVENT
            ],
            "sandbox_only": (
                audit_attempts["live_read_or_write"] == 0
                and audit_attempts["write_outside_sandbox"] == 0
                and audit_attempts["mutation_outside_sandbox"] == 0
                and audit_attempts[EXTERNAL_STORAGE_ACCESS_EVENT] == 0
                and blocked_attempts["subprocess.Popen"] == 0
                and audit_attempts["subprocess.Popen"] == 0
            ),
        },
        "suppressed_stdout_sha256": _digest(suppressed_stdout.getvalue().encode("utf-8")),
    }


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _manifest_worker_runtime() -> tuple[Path, tuple[Path, ...]]:
    """Return the already hash-bound candidate runtime used for certification."""

    try:
        runtime, manifest, _report = _verify_formal_runtime_binding()
    except _FormalRuntimeBindingError as exc:
        raise ContractValidationError(str(exc)) from exc

    allowed_roots = {REPO_ROOT.resolve(strict=True)}
    for root_key, rows_key in (
        ("runtime_root", "directories"),
        ("base_runtime_root", "base_directories"),
    ):
        root_text = manifest.get(root_key)
        rows = manifest.get(rows_key)
        if not isinstance(root_text, str) or not isinstance(rows, list):
            raise ContractValidationError("certifying replay runtime manifest is incomplete")
        root = Path(root_text).resolve(strict=True)
        for row in rows:
            relative = row.get("path") if isinstance(row, dict) else None
            if not isinstance(relative, str):
                continue
            parts = Path(relative).parts
            if (
                not parts
                or Path(relative).is_absolute()
                or ".." in parts
                or parts[-1] != "site-packages"
            ):
                continue
            candidate = (root / relative).resolve(strict=True)
            if not _inside(candidate, root) or not candidate.is_dir():
                raise ContractValidationError(
                    "certifying replay site-packages escapes its manifest root"
                )
            allowed_roots.add(candidate)
    if len(allowed_roots) == 1:
        raise ContractValidationError(
            "certifying replay runtime manifest has no site-packages"
        )

    raw_pythonpath = os.environ.get("PYTHONPATH", "")
    roots: list[Path] = []
    for value in raw_pythonpath.split(os.pathsep):
        candidate = Path(value)
        if not value or not candidate.is_absolute() or not candidate.is_dir():
            raise ContractValidationError("actual-handler worker PYTHONPATH is unbound")
        resolved = candidate.resolve(strict=True)
        if resolved not in allowed_roots or resolved in roots:
            raise ContractValidationError(
                "actual-handler worker PYTHONPATH is outside the candidate manifest"
            )
        roots.append(resolved)
    if not roots or roots[0] != REPO_ROOT.resolve(strict=True):
        raise ContractValidationError(
            "actual-handler worker release root is not first on PYTHONPATH"
        )
    return runtime, tuple(roots)


def _source_worker_runtime() -> tuple[Path, tuple[Path, ...]]:
    """Bind source diagnostics to an explicitly isolated virtualenv.

    The source tree no longer owns a copied ``venv`` after the V3 runtime was
    centralized.  Local diagnostics may therefore use the immutable runtime
    named by ``MAGI_V3_PYTHON_RUNTIME``.  Formal certification still goes
    through :func:`_manifest_worker_runtime` and re-attests the full manifest.
    """

    declared_runtime = os.environ.get("MAGI_V3_PYTHON_RUNTIME", "").strip()
    runtime = (
        Path(declared_runtime).expanduser().absolute()
        if declared_runtime
        else (REPO_ROOT / "venv" / "bin" / "python3").absolute()
    )
    if not runtime.is_absolute() or runtime.parent.name not in {"bin", "Scripts"}:
        raise ContractValidationError(
            "actual-handler source replay runtime must be an absolute virtualenv executable"
        )
    venv_root = runtime.parent.parent
    config_path = venv_root / "pyvenv.cfg"
    try:
        values = {}
        for raw in config_path.read_text(encoding="utf-8").splitlines():
            if "=" in raw:
                key, value = raw.split("=", 1)
                values[key.strip().lower()] = value.strip()
    except (OSError, UnicodeError) as exc:
        raise ContractValidationError(
            "actual-handler source replay requires the candidate virtualenv"
        ) from exc
    if values.get("include-system-site-packages", "").lower() != "false":
        raise ContractValidationError(
            "actual-handler candidate virtualenv enables system site-packages"
        )
    version = values.get("version", "").split(".")
    if len(version) < 2 or not all(part.isdigit() for part in version[:2]):
        raise ContractValidationError("actual-handler candidate Python version is invalid")

    if os.name == "nt":
        if not declared_runtime:
            runtime = venv_root / "Scripts" / "python.exe"
        site_packages = venv_root / "Lib" / "site-packages"
    else:
        if not declared_runtime:
            runtime = venv_root / "bin" / "python3"
        site_packages = (
            venv_root
            / "lib"
            / f"python{version[0]}.{version[1]}"
            / "site-packages"
        )
    if not runtime.is_file() or not site_packages.is_dir():
        raise ContractValidationError(
            "actual-handler candidate virtualenv runtime is incomplete"
        )
    resolved_site = site_packages.resolve(strict=True)
    if not _inside(resolved_site, venv_root):
        raise ContractValidationError(
            "actual-handler candidate site-packages escapes the virtualenv"
        )
    for module_name in ("flask", "flask_login", "jsonschema"):
        spec = PathFinder.find_spec(module_name, [str(resolved_site)])
        if spec is None or not spec.origin or not _inside(Path(spec.origin), resolved_site):
            raise ContractValidationError(
                f"actual-handler candidate virtualenv lacks {module_name}"
            )
    return runtime, (REPO_ROOT.resolve(strict=True), resolved_site)


def _worker_runtime() -> tuple[Path, tuple[Path, ...]]:
    if os.environ.get("MAGI_V3_ROUTE_CERTIFYING", "").strip() == "1":
        return _manifest_worker_runtime()
    return _source_worker_runtime()


def _worker_live_read_roots(worker_roots: Sequence[Path]) -> tuple[Path, ...]:
    """Allow immutable candidate code and its bound runtime to be read.

    Installed releases live below the MAGI application-support root. Omitting
    the first worker root therefore made the audit hook mistake reads of the
    candidate itself for reads of mutable LIVE state. This allowlist remains
    read-only: the audit hook still rejects every write below these paths.
    """

    roots = tuple(Path(root).resolve(strict=True) for root in worker_roots)
    if not roots or roots[0] != REPO_ROOT.resolve(strict=True):
        raise ContractValidationError(
            "actual-handler worker release root is not first on its read allowlist"
        )
    return roots


def _is_installed_release_root(repo_root: Path, live_root: Path) -> bool:
    """Return whether *repo_root* is one immutable installed release.

    Installed candidates intentionally live below ``MAGI/releases``.  They are
    code inputs bound by the candidate manifest, not mutable LIVE runtime
    state.  Requiring the repository to be a direct child also prevents an
    arbitrary nested path elsewhere below the MAGI application-support tree
    from gaining the same exception.
    """

    resolved_repo = repo_root.resolve(strict=True)
    releases_root = (live_root.resolve(strict=True) / "releases").resolve(
        strict=True
    )
    return resolved_repo.parent == releases_root and resolved_repo.is_dir()


def _worker_environment(
    sandbox: Path,
    *,
    worker_python: Path | None = None,
    python_roots: Sequence[Path] | None = None,
) -> dict[str, str]:
    # Start from an explicit allowlist.  Inheriting the parent test/campaign
    # environment can leak MAGI_RUNTIME_ROOT or other live-path selectors into
    # this worker and either touch production state or create order-dependent
    # full-suite failures.
    if worker_python is None or python_roots is None:
        bound_python, bound_roots = _worker_runtime()
        worker_python = worker_python or bound_python
        python_roots = python_roots or bound_roots
    roots = tuple(Path(root).resolve(strict=True) for root in python_roots)
    if not roots or roots[0] != REPO_ROOT.resolve(strict=True):
        raise ContractValidationError(
            "actual-handler worker release root is not first on PYTHONPATH"
        )
    live_root = (
        account_home() / "Library" / "Application Support" / "MAGI" / "runtime"
    ).resolve(strict=False)
    user_python = (account_home() / "Library" / "Python").resolve(strict=False)
    source_runtime = os.environ.get("MAGI_V3_PYTHON_RUNTIME", "").strip()
    allow_bound_source_runtime = (
        os.environ.get("MAGI_V3_ROUTE_CERTIFYING", "").strip() != "1"
        and bool(source_runtime)
        and Path(source_runtime).expanduser().is_absolute()
    )
    for root in roots[1:]:
        if (
            (_is_within(root, live_root) and not allow_bound_source_runtime)
            or _is_within(root, user_python)
        ):
            raise ContractValidationError(
                "actual-handler worker cannot load live or user site-packages"
            )
    environment = {
        "HOME": str(sandbox / "home"),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": str(sandbox / "tmp"),
        "XDG_CACHE_HOME": str(sandbox / "xdg-cache"),
        "XDG_CONFIG_HOME": str(sandbox / "xdg-config"),
        "XDG_DATA_HOME": str(sandbox / "xdg-data"),
        "MAGI_ROOT_DIR": str(sandbox / "magi-root"),
        "MAGI_AGENT_DIR": str(sandbox / "agent"),
        "MAGI_METRICS_DIR": str(sandbox / "metrics"),
        "MAGI_ORCH_DIR": str(REPO_ROOT / "casper_ecosystem" / "law_firm_orchestrators"),
        "MAGI_CODE_DIR": str(REPO_ROOT / "casper_ecosystem" / "law_firm_orchestrators"),
        "MAGI_JSON_DIR": str(REPO_ROOT / "json"),
        "MAGI_SKILL_PYTHON": str(Path(worker_python).absolute()),
        "MAGI_ENV_FILE": str(sandbox / "missing.env"),
        "MAGI_EXPORTS_DIR": str(sandbox / "exports"),
        "MAGI_OSC_FILE_SHARE_STORE": str(sandbox / "osc-file-shares.json"),
        "MAGI_OSC_FILE_SHARE_CACHE_DIR": str(sandbox / "osc-share-cache"),
        "MAGI_LINE_LAST_SENDER_FILE": str(sandbox / "line-last.json"),
        "MAGI_DISCORD_LAST_CHANNEL_FILE": str(sandbox / "discord-last.json"),
        "MAGI_ALLOW_INTERNET": "0",
        "MAGI_ALLOW_CLOUD_MODELS": "0",
        "MAGI_DISABLE_SERVER_STARTUP_HOOKS": "1",
        "MAGI_SKIP_IMPORT_PROBES": "1",
        "FLASK_SECRET_KEY": "offline-route-replay",
        "DB_PASSWORD": "offline-route-replay",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
        "PYTHONPATH": os.pathsep.join(str(root) for root in roots),
        "PYTHONHASHSEED": "0",
    }
    if allow_bound_source_runtime:
        # The isolated worker re-validates its own runtime before importing
        # handlers. Forward only the already validated executable path; no
        # parent credentials or mutable live paths are inherited.
        environment["MAGI_V3_PYTHON_RUNTIME"] = str(Path(worker_python).absolute())
    if os.environ.get("MAGI_V3_ROUTE_CERTIFYING", "").strip() == "1":
        # Re-attest before forwarding any formal binding.  The worker performs
        # the same verification again before importing Flask/handlers.
        verified_python, verified_roots = _manifest_worker_runtime()
        if (
            Path(worker_python).resolve(strict=True)
            != verified_python.resolve(strict=True)
            or tuple(roots) != tuple(verified_roots)
        ):
            raise ContractValidationError(
                "actual-handler worker runtime differs from the verified formal binding"
            )
        environment.update(
            {
                name: os.environ[name]
                for name in FORMAL_RUNTIME_ENVIRONMENT_KEYS
            }
        )
    return environment


def _run_worker(sandbox: Path) -> dict[str, Any]:
    for child in (
        sandbox / "tmp",
        sandbox / "home",
        sandbox / "xdg-cache",
        sandbox / "xdg-config",
        sandbox / "xdg-data",
        sandbox / "magi-root",
        sandbox / "agent",
        sandbox / "metrics",
        sandbox / "orch",
    ):
        child.mkdir(parents=True, exist_ok=True)
    # The campaign launcher deliberately replaces HOME with an isolated tree.
    # Resolve the login account independently so the audit guard still protects
    # the real Application Support runtime during certifying execution.
    live_root = account_home() / "Library" / "Application Support" / "MAGI"
    worker_python, python_roots = _worker_runtime()
    result = subprocess.run(
        [
            str(worker_python),
            "-S",
            str(SCRIPT_PATH),
            "--worker",
            "--sandbox",
            str(sandbox),
            "--live-root",
            str(live_root),
        ],
        cwd=REPO_ROOT,
        env=_worker_environment(
            sandbox,
            worker_python=worker_python,
            python_roots=python_roots,
        ),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise ContractValidationError(
            f"actual-handler worker failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ContractValidationError("actual-handler worker emitted invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ContractValidationError("actual-handler worker evidence is not an object")
    return payload


def _build_dispositions(
    *,
    inventory: RouteInventory,
    reviews: Mapping[RouteMethodKey, Any],
    executed: Mapping[RouteMethodKey, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], Counter[str]]:
    dispositions: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    for route in sorted(inventory.routes):
        for method in route.methods:
            key = RouteMethodKey(route.service, route.rule, method, route.endpoint)
            review = reviews.get(key)
            base = _key_dict(key)
            if key in executed:
                case = executed[key]
                branch_class = str(case.get("branch_class") or "")
                if branch_class not in BRANCH_CLASSES:
                    raise ContractValidationError(
                        f"actual-handler replay case lacks a valid branch class: {key}"
                    )
                if not case.get("passed"):
                    disposition = "actual_handler_failed"
                    reason_code = "ACTUAL_HANDLER_CONTRACT_DRIFT"
                elif branch_class == "validation_guard_only":
                    disposition = "validation_guard_only"
                    reason_code = "VALIDATION_GUARD_DISPATCH_ONLY"
                else:
                    disposition = "actual_handler_passed"
                    reason_code = "BOUND_REPRESENTATIVE_SUCCESS_PATH"
                row = {
                    **base,
                    "disposition": disposition,
                    "reason_code": reason_code,
                    "reviewed": True,
                    "side_effect_class": review.side_effect_class if review else None,
                    "branch_class": branch_class,
                    "handler_dispatch_passed": case.get("passed") is True,
                    "representative_success_path_passed": (
                        case.get("passed") is True
                        and branch_class == "representative_success_path"
                    ),
                    "evidence_sha256": _digest(_canonical(case)),
                }
            elif review is None or not review.reviewed:
                row = {
                    **base,
                    "disposition": "blocked_unreviewed",
                    "reason_code": "ROUTE_METHOD_REVIEW_REQUIRED",
                    "reviewed": False,
                    "side_effect_class": None,
                }
            elif review.side_effect_class in {"external_commit", "destructive"}:
                row = {
                    **base,
                    "disposition": "blocked_unsafe_side_effect",
                    "reason_code": "OFFLINE_REPLAY_FORBIDS_" + review.side_effect_class.upper(),
                    "reviewed": True,
                    "side_effect_class": review.side_effect_class,
                }
            elif key in FIXTURE_ONLY_KEYS:
                row = {
                    **base,
                    "disposition": "fixture_contract_only",
                    "reason_code": "ACTUAL_HANDLER_SANDBOX_FIXTURE_REQUIRED",
                    "reviewed": True,
                    "side_effect_class": review.side_effect_class,
                }
            else:
                row = {
                    **base,
                    "disposition": "missing_actual_handler_case",
                    "reason_code": "BOUND_EXPECTED_OUTCOME_REQUIRED",
                    "reviewed": True,
                    "side_effect_class": review.side_effect_class,
                }
            dispositions.append(row)
            counts[row["disposition"]] += 1
    return dispositions, counts


def _route_summary(inventory: RouteInventory, dispositions: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    by_route: dict[tuple[str, str, str], list[str]] = {}
    for row in dispositions:
        by_route.setdefault((str(row["service"]), str(row["rule"]), str(row["endpoint"])), []).append(
            str(row["disposition"])
        )
    fully = sum(all(value == "actual_handler_passed" for value in values) for values in by_route.values())
    partial = sum(
        any(value == "actual_handler_passed" for value in values)
        and not all(value == "actual_handler_passed" for value in values)
        for values in by_route.values()
    )
    dispatched = sum(
        all(value in {"actual_handler_passed", "validation_guard_only"} for value in values)
        for values in by_route.values()
    )
    return {
        "pinned_routes": len(inventory.routes),
        "routes_with_machine_disposition": len(by_route),
        "routes_with_handler_dispatch_evidence": dispatched,
        "fully_actual_handler_replayed_routes": fully,
        "partially_actual_handler_replayed_routes": partial,
        "routes_with_remaining_gap": len(by_route) - fully,
    }


def run_actual_route_replay(workspace: Path | None = None) -> dict[str, Any]:
    owned_temp: tempfile.TemporaryDirectory[str] | None = None
    if workspace is None:
        owned_temp = tempfile.TemporaryDirectory(prefix="magi-v3-actual-route-replay-")
        root = Path(owned_temp.name).resolve()
    else:
        root = workspace.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
    live_root = (
        account_home() / "Library" / "Application Support" / "MAGI"
    ).resolve()
    if _is_within(root, live_root):
        raise ContractValidationError("actual route replay workspace overlaps live MAGI state")

    try:
        validated = load_and_validate_runtime_inventory()
        inventory = RouteInventory.load()
        reviews = load_route_method_reviews(expected_inventory_fingerprint=validated["fingerprint"])
        worker = _run_worker(root / "worker")
        golden = run_osc_file_golden_flow(
            BEHAVIOR_FIXTURE_ROOT / "osc-file-content.json",
            root / "osc-golden",
        )
        operational = run_operational_golden_flows(root / "operational-golden")

        executed: dict[RouteMethodKey, Mapping[str, Any]] = {}
        for case in worker.get("cases", []):
            key = _route_key(case)
            if key in executed:
                raise ContractValidationError(f"duplicate actual-handler replay evidence: {key}")
            executed[key] = case
        for route in golden["reviewed_routes"]:
            key = _route_key(route)
            if key in executed:
                raise ContractValidationError(f"duplicate golden actual-handler replay evidence: {key}")
            executed[key] = {
                **route,
                "passed": golden["passed"],
                "branch_class": "representative_success_path",
                "representative_success_path": True,
                "validation_guard_only": False,
                "flow_id": golden["flow_id"],
                "expected_outcomes_sha256": golden["expected_outcomes_sha256"],
                "observed_outcomes_sha256": golden["observed_outcomes_sha256"],
            }
        for case in operational["cases"]:
            key = _route_key(case)
            if key in executed:
                raise ContractValidationError(f"duplicate operational actual-handler replay evidence: {key}")
            executed[key] = case

        unknown = sorted(set(executed) - set(reviews))
        if unknown:
            raise ContractValidationError(f"actual-handler replay contains unreviewed route methods: {unknown[:3]}")
        if any(case.get("auth_or_not_found_status_used_as_proof") for case in worker["cases"]):
            raise ContractValidationError("actual-handler replay attempted to count 401/404 as dispatch proof")

        dispositions, disposition_counts = _build_dispositions(
            inventory=inventory,
            reviews=reviews,
            executed=executed,
        )
        route_summary = _route_summary(inventory, dispositions)
        method_total = sum(len(route.methods) for route in inventory.routes)
        if len(dispositions) != method_total or route_summary["routes_with_machine_disposition"] != 347:
            raise ContractValidationError("route replay disposition does not account for the full pinned inventory")
        actual_passed = disposition_counts["actual_handler_passed"]
        validation_guard_only = disposition_counts["validation_guard_only"]
        actual_failed = disposition_counts["actual_handler_failed"]
        safe_execution = (
            worker["safety"]["sandbox_only"]
            and golden["passed"]
            and operational["passed"]
            and worker["safety"]["external_storage_access_attempts"] == 0
        )
        covered_domains = [
            "osc_file_preview_download",
            "tools_read_only_operations_and_audit",
            *operational["domains"],
        ]
        missing_domains = [domain for domain in GOLDEN_DOMAINS_REQUIRED if domain not in covered_domains]
        route_blocker_retained = route_summary["routes_with_remaining_gap"] > 0 or actual_failed > 0
        golden_blocker_retained = bool(missing_domains)
        execution_passed = actual_failed == 0 and safe_execution
        report: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "workload": "346_route_contract_replay",
            "script_sha256": _digest(SCRIPT_PATH.read_bytes()),
            "handler_source_sha256": {
                **worker["handler_source_sha256"],
                "api/blueprints/osc_cases.py": _digest((REPO_ROOT / "api" / "blueprints" / "osc_cases.py").read_bytes()),
                "api/blueprints/osc_files.py": _digest((REPO_ROOT / "api" / "blueprints" / "osc_files.py").read_bytes()),
                "api/blueprints/osc_gcal.py": _digest((REPO_ROOT / "api" / "blueprints" / "osc_gcal.py").read_bytes()),
                "skills/labor-law-calculator/action.py": _digest(
                    (REPO_ROOT / "skills" / "labor-law-calculator" / "action.py").read_bytes()
                ),
            },
            "inventory_fingerprint": validated["fingerprint"],
            "inventory_counts": inventory.counts,
            "route_summary": route_summary,
            "route_method_summary": {
                "pinned_route_methods": method_total,
                "reviewed_route_methods": len(reviews),
                "actual_handler_passed": actual_passed,
                "representative_success_path_passed": actual_passed,
                "validation_guard_only": validation_guard_only,
                "handler_dispatch_passed": actual_passed + validation_guard_only,
                "actual_handler_failed": actual_failed,
                "remaining_route_methods": method_total - actual_passed,
                "dispositions": dict(sorted(disposition_counts.items())),
            },
            "surface_verification": {
                "5003": worker["tools_surface"],
                "5002": {
                    "exact_full_surface_loaded": False,
                    "reason_code": "FULL_PRODUCTION_APP_IMPORT_NOT_PERMITTED_IN_OFFLINE_HANDLER_REPLAY",
                    "isolated_actual_handlers_executed": sum(
                        key.service == "5002" for key in executed
                    ),
                },
            },
            "actual_handler_cases": worker["cases"],
            "osc_golden_flow": golden,
            "operational_golden_flow": operational,
            "route_method_dispositions": dispositions,
            "golden_flow_coverage": {
                "required_domains": list(GOLDEN_DOMAINS_REQUIRED),
                "covered_domains": covered_domains,
                "missing_domains": missing_domains,
                "actual_handler_flows": 5,
                "actual_handler_route_methods": actual_passed,
                "complete": not missing_domains,
            },
            "safety": {
                "offline": True,
                "listener_started": False,
                "production_service_started": False,
                "production_database_accessed": False,
                "nas_accessed": (
                    worker["safety"]["external_storage_access_attempts"] != 0
                ),
                "external_storage_roots": worker["safety"][
                    "external_storage_roots"
                ],
                "external_storage_access_attempts": worker["safety"][
                    "external_storage_access_attempts"
                ],
                "isolation_attempts": {
                    EXTERNAL_STORAGE_ACCESS_EVENT: worker["safety"][
                        "external_storage_access_attempts"
                    ]
                },
                "worker": worker["safety"],
                "golden_network_access_performed": golden["network_access_performed"],
                "golden_production_state_accessed": golden["production_state_accessed"],
                "operational_network_access_performed": operational["network_access_performed"],
                "operational_provider_exchange_performed": operational["provider_exchange_performed"],
                "operational_nas_mount_attempted": operational["nas_mount_attempted"],
                "operational_production_state_accessed": operational["production_state_accessed"],
                "safe_execution": safe_execution,
            },
            "blockers": {
                "ROUTE_REPLAY_NOT_IMPLEMENTED": {
                    "retained": route_blocker_retained,
                    "remaining_routes": route_summary["routes_with_remaining_gap"],
                    "remaining_route_methods": method_total - actual_passed,
                    "reason": "not all pinned route-methods have reviewed, bound actual-handler replay cases",
                },
                "GOLDEN_FLOW_COVERAGE_INCOMPLETE": {
                    "retained": golden_blocker_retained,
                    "missing_domains": missing_domains,
                    "reason": (
                        "all required actual-handler golden-flow domains passed in sandbox"
                        if not missing_domains
                        else "one or more required actual-handler golden-flow domains remain absent"
                    ),
                },
            },
            "execution_passed": execution_passed,
            "coverage_complete": not route_blocker_retained and not golden_blocker_retained,
            "passed": execution_passed and not route_blocker_retained and not golden_blocker_retained,
        }
        report["evidence_sha256"] = _digest(_canonical(report))
        return report
    finally:
        if owned_temp is not None:
            owned_temp.cleanup()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--sandbox", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--live-root", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.worker:
            if args.sandbox is None or args.live_root is None:
                raise ContractValidationError("worker requires sandbox and live-root")
            # Re-read immediately before handler execution as a final TOCTOU
            # barrier; formal workers already performed the pre-import check.
            _worker_runtime()
            payload = _worker_report(args.sandbox, args.live_root)
        else:
            payload = run_actual_route_replay(args.workspace)
    except Exception as exc:
        print(json.dumps({"passed": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if payload.get("passed", payload.get("all_cases_passed")) else 2


if __name__ == "__main__":
    raise SystemExit(main())

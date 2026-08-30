from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import urlencode
from unittest.mock import patch

from flask import Flask, session
from flask_login import LoginManager, UserMixin, login_user
from werkzeug.test import Client
from werkzeug.wrappers import Response

from magi_v3.compat.gateway import LazyCompatibilityApp, RouteInventory

from .inventory import load_and_validate_runtime_inventory
from .paths import REPO_ROOT, RUNTIME_ROUTES_PATH
from .route_reviews import load_route_method_reviews, require_reviewed_route_method
from .schema import ContractValidationError, load_json

BEHAVIOR_FIXTURE_ROOT = REPO_ROOT / "tests" / "v3" / "compat" / "behavior_fixtures"
_FLOW_LOCK = threading.RLock()


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


def _fixture(path: Path) -> tuple[dict[str, Any], bytes, str]:
    resolved = path.expanduser().resolve(strict=True)
    try:
        resolved.relative_to(BEHAVIOR_FIXTURE_ROOT.resolve(strict=True))
    except ValueError as exc:
        raise ContractValidationError("golden-flow fixture is outside the behavior allowlist") from exc
    if resolved.is_symlink() or resolved.suffix != ".json":
        raise ContractValidationError("golden-flow fixture must be a regular JSON file")
    raw = resolved.read_bytes()
    fixture = load_json(resolved)
    if fixture.get("schema_version") != 1 or fixture.get("classification") != "synthetic_non_sensitive":
        raise ContractValidationError("golden-flow fixture lacks synthetic non-sensitive classification")
    filename = fixture.get("filename")
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", filename):
        raise ContractValidationError("golden-flow fixture filename is not a safe synthetic basename")
    try:
        payload = base64.b64decode(str(fixture.get("content_base64") or ""), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ContractValidationError("golden-flow fixture payload is not strict Base64") from exc
    if (
        not payload
        or len(payload) != fixture.get("content_length")
        or _digest(payload) != fixture.get("content_sha256")
        or b"nonsensitive fixture" not in payload
    ):
        raise ContractValidationError("golden-flow fixture payload binding is invalid")
    flow = fixture.get("golden_flow")
    if (
        not isinstance(flow, dict)
        or flow.get("flow_id") != "osc_preview_range_download_v1"
        or not isinstance(flow.get("expected_outcomes"), dict)
    ):
        raise ContractValidationError("golden-flow expected outcomes are missing")
    return fixture, payload, _digest(raw)


class _User(UserMixin):
    id = "operator-contract"
    role = "operator"


def _isolated_app(*, include_gcal: bool = False) -> Flask:
    from api.blueprints import osc_cases, osc_files

    app = Flask("v3-offline-osc-golden-flow")
    app.config.update(TESTING=True, SECRET_KEY="offline-contract-only")
    login = LoginManager(app)
    login.login_view = "contract_login_page"

    @login.user_loader
    def load_user(user_id: str):
        return _User() if user_id == _User.id else None

    @app.get("/login")
    def contract_login_page():
        return "offline contract login", 200

    @app.post("/__offline_contract/login")
    def contract_login():
        login_user(_User())
        return {"ok": True}

    @app.post("/__offline_contract/session/gcal-state")
    def contract_gcal_state():
        session["gcal_oauth_state"] = "offline-expected-state"
        return {"ok": True}

    @app.get("/__offline_contract/session/gcal-state")
    def contract_gcal_state_status():
        return {"present": "gcal_oauth_state" in session}

    app.register_blueprint(osc_cases.osc_bp)
    app.register_blueprint(osc_files.osc_files_bp)
    if include_gcal:
        from api.blueprints import osc_gcal

        app.register_blueprint(osc_gcal.osc_gcal_bp)
    return app


def _content_type(response: Response) -> str:
    return str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip()


def run_osc_file_golden_flow(
    fixture_path: Path,
    workspace: Path,
    *,
    inventory_path: Path = RUNTIME_ROUTES_PATH,
) -> dict[str, Any]:
    """Execute one bound OSC browser flow entirely through an in-process V3 compat app."""

    fixture, payload, fixture_sha = _fixture(fixture_path)
    root = workspace.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    live = (Path.home() / "Library" / "Application Support" / "MAGI").resolve()
    try:
        root.relative_to(live)
    except ValueError:
        pass
    else:
        raise ContractValidationError("golden-flow workspace must not overlap live MAGI state")

    inventory = RouteInventory.load(inventory_path)
    validated_inventory = load_and_validate_runtime_inventory(inventory_path)
    route = fixture["route"]
    signatures = {
        (item.rule, item.endpoint, item.methods)
        for item in inventory.for_service(str(route["service"]))
    }
    required = {
        (str(route["preview"]), str(route["preview_endpoint"]), ("GET",)),
        (str(route["content"]), str(route["content_endpoint"]), ("GET",)),
    }
    if not required <= signatures:
        raise ContractValidationError("golden-flow routes are not pinned in the 347-route inventory")
    reviews = load_route_method_reviews(
        expected_inventory_fingerprint=validated_inventory["fingerprint"]
    )
    reviewed_routes = []
    for rule, endpoint, methods in sorted(required):
        review = require_reviewed_route_method(
            service=str(route["service"]),
            rule=rule,
            method=methods[0],
            endpoint=endpoint,
            reviews=reviews,
        )
        if review.side_effect_class not in {"read_only", "reversible_write"}:
            raise ContractValidationError(
                "OSC golden-flow route review permits unsafe external side effects"
            )
        reviewed_routes.append(
            {
                "service": str(route["service"]),
                "rule": rule,
                "method": methods[0],
                "endpoint": endpoint,
                "reviewed_by": review.reviewed_by,
                "side_effect_class": review.side_effect_class,
            }
        )

    allowed = root / "allowed"
    allowed.mkdir(exist_ok=True)
    document = allowed / str(fixture["filename"])
    document.write_bytes(payload)
    outside = root / "outside.pdf"
    outside.write_bytes(b"%PDF-synthetic-outside\n")
    staging = root / "staging"
    staging.mkdir(exist_ok=True)

    from api.blueprints import osc_cases, osc_files

    network_attempts: list[str] = []

    def block_network(*_args: Any, **_kwargs: Any):
        network_attempts.append("blocked")
        raise AssertionError("network access is forbidden in the offline golden flow")

    with _FLOW_LOCK:
        previous_root = os.environ.get("PAPERCLIP_FILEMANAGER_TEST_BASE")
        previous_tempdir = tempfile.tempdir
        os.environ["PAPERCLIP_FILEMANAGER_TEST_BASE"] = str(allowed)
        tempfile.tempdir = str(staging)
        try:
            with (
                patch.object(osc_cases, "_osc_audit_file_event", lambda *_a, **_k: None),
                patch.object(osc_files, "_audit_file_event", lambda *_a, **_k: None),
                patch.object(osc_cases, "urlopen", block_network),
                patch.object(osc_files, "urlopen", block_network),
            ):
                app = _isolated_app()
                compat = LazyCompatibilityApp(
                    "5002",
                    inventory=inventory,
                    loader=lambda _service: app,
                    verifier=lambda _app, _service, _inventory: None,
                )
                anonymous = Client(compat, Response, use_cookies=True)
                preview_query = urlencode({"path": str(document)})
                anonymous_preview = anonymous.get(f"{route['preview']}?{preview_query}")

                client = Client(compat, Response, use_cookies=True)
                login_response = client.post("/__offline_contract/login")
                preview = client.get(f"{route['preview']}?{preview_query}")
                preview_json = preview.get_json() or {}
                range_response = client.get(
                    str(preview_json.get("content_url") or ""),
                    headers={"Range": str(fixture["range"]["header"])},
                )
                full = client.get(f"{route['content']}?{preview_query}")
                missing = client.get(
                    f"{route['preview']}?{urlencode({'path': str(allowed / 'missing.pdf')})}"
                )
                forbidden = client.get(
                    f"{route['content']}?{urlencode({'path': str(outside)})}"
                )
        finally:
            tempfile.tempdir = previous_tempdir
            if previous_root is None:
                os.environ.pop("PAPERCLIP_FILEMANAGER_TEST_BASE", None)
            else:
                os.environ["PAPERCLIP_FILEMANAGER_TEST_BASE"] = previous_root

    range_payload = base64.b64decode(str(fixture["range"]["body_base64"]), validate=True)
    expected = {
        **fixture["golden_flow"]["expected_outcomes"],
        "range_body_sha256": _digest(range_payload),
        "range_content_range": (
            f"bytes {fixture['range']['start']}-{fixture['range']['end']}/{len(payload)}"
        ),
        "full_download_body_sha256": _digest(payload),
    }
    observed = {
        "anonymous_preview_status": anonymous_preview.status_code,
        "login_status": login_response.status_code,
        "preview_status": preview.status_code,
        "preview_kind": preview_json.get("kind"),
        "range_status": range_response.status_code,
        "range_content_type": _content_type(range_response),
        "range_content_disposition_prefix": str(
            range_response.headers.get("Content-Disposition") or ""
        ).split(" ", 1)[0],
        "range_body_sha256": _digest(range_response.data),
        "range_content_range": range_response.headers.get("Content-Range"),
        "full_download_status": full.status_code,
        "full_download_content_type": _content_type(full),
        "full_download_content_disposition_prefix": str(
            full.headers.get("Content-Disposition") or ""
        ).split(" ", 1)[0],
        "full_download_body_sha256": _digest(full.data),
        "missing_status": missing.status_code,
        "forbidden_status": forbidden.status_code,
    }
    if observed != expected:
        raise ContractValidationError(
            f"OSC golden-flow outcome drift: expected={expected!r}, observed={observed!r}"
        )
    if range_response.data != range_payload:
        raise ContractValidationError("OSC golden-flow range bytes do not match the fixture")
    if network_attempts:
        raise ContractValidationError("OSC golden-flow attempted network access")
    staged_files_remaining = sum(1 for path in staging.rglob("*") if path.is_file())
    if staged_files_remaining:
        raise ContractValidationError("OSC golden-flow left staged files behind")

    evidence = {
        "schema_version": 1,
        "flow_id": fixture["golden_flow"]["flow_id"],
        "inventory_counts": inventory.counts,
        "inventory_fingerprint": validated_inventory["fingerprint"],
        "reviewed_routes": reviewed_routes,
        "fixture_sha256": fixture_sha,
        "expected_outcomes_sha256": _digest(_canonical(expected)),
        "observed_outcomes_sha256": _digest(_canonical(observed)),
        "outcomes": observed,
        "transport": "in_process_wsgi_v3_compat",
        "network_access_performed": False,
        "external_writes_performed": False,
        "sandbox_writes_only": True,
        "staged_files_remaining": staged_files_remaining,
        "production_state_accessed": False,
        "service_start_performed": False,
        "passed": True,
    }
    evidence["evidence_sha256"] = _digest(_canonical(evidence))
    return evidence


def run_operational_golden_flows(
    workspace: Path,
    *,
    inventory_path: Path = RUNTIME_ROUTES_PATH,
) -> dict[str, Any]:
    """Exercise NAS, Office, and provider/session branches with synthetic state only."""

    root = workspace.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    live = (Path.home() / "Library" / "Application Support" / "MAGI").resolve()
    try:
        root.relative_to(live)
    except ValueError:
        pass
    else:
        raise ContractValidationError("operational golden-flow workspace overlaps live MAGI state")

    inventory = RouteInventory.load(inventory_path)
    validated_inventory = load_and_validate_runtime_inventory(inventory_path)
    required = (
        ("5002", "/api/osc/folders/browse", "GET", "osc_files.osc_folders_browse_api"),
        ("5002", "/api/osc/folders/tree", "GET", "osc_files.osc_folders_tree_api"),
        ("5002", "/api/osc/files/info", "GET", "osc_files.osc_files_info_api"),
        ("5002", "/api/osc/labor-law/parse-files", "POST", "osc_cases.osc_labor_law_parse_files"),
        ("5002", "/api/osc/gcal/auth/callback", "GET", "osc_gcal.gcal_auth_callback"),
    )
    signatures = {
        (route.service, route.rule, method, route.endpoint)
        for route in inventory.routes
        for method in route.methods
    }
    if not set(required) <= signatures:
        raise ContractValidationError("operational golden-flow routes are not pinned in the inventory")
    reviews = load_route_method_reviews(
        expected_inventory_fingerprint=validated_inventory["fingerprint"]
    )
    reviewed_routes = []
    for service, rule, method, endpoint in required:
        review = require_reviewed_route_method(
            service=service,
            rule=rule,
            method=method,
            endpoint=endpoint,
            reviews=reviews,
        )
        reviewed_routes.append(
            {
                "service": service,
                "rule": rule,
                "method": method,
                "endpoint": endpoint,
                "reviewed_by": review.reviewed_by,
                "side_effect_class": review.side_effect_class,
            }
        )

    allowed = root / "synthetic-nas"
    case_dir = allowed / "Case-A"
    child_dir = case_dir / "Pleadings"
    child_dir.mkdir(parents=True, exist_ok=True)
    (child_dir / "nonsensitive-fixture.txt").write_text(
        "synthetic nonsensitive fixture\n", encoding="utf-8"
    )
    (allowed / ".DS_Store").write_text("synthetic hidden fixture", encoding="utf-8")
    workbook_path = allowed / "attendance-nonsensitive.xlsx"
    staging = root / "staging"
    staging.mkdir(exist_ok=True)

    try:
        import openpyxl
    except ImportError as exc:
        raise ContractValidationError("Office golden flow requires openpyxl") from exc
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(
        [
            "115/07/13(一)",
            "09:00",
            "18:00",
            "09:00",
            "19:30",
            None,
            None,
            None,
            None,
            "0030",
            "0100",
        ]
    )
    workbook.save(workbook_path)
    workbook.close()

    from api.blueprints import osc_cases, osc_files, osc_gcal

    source_paths = {
        "api/blueprints/osc_cases.py": Path(osc_cases.__file__).resolve(),
        "api/blueprints/osc_files.py": Path(osc_files.__file__).resolve(),
        "api/blueprints/osc_gcal.py": Path(osc_gcal.__file__).resolve(),
        "skills/labor-law-calculator/action.py": (
            REPO_ROOT / "skills" / "labor-law-calculator" / "action.py"
        ).resolve(),
    }
    for source in source_paths.values():
        try:
            source.relative_to(REPO_ROOT.resolve())
        except ValueError as exc:
            raise ContractValidationError(
                f"operational golden flow imported handler outside source checkout: {source}"
            ) from exc
    handler_source_sha256 = {
        name: _digest(source.read_bytes()) for name, source in source_paths.items()
    }

    network_attempts: list[str] = []
    provider_attempts: list[str] = []
    mount_attempts: list[str] = []

    def block_network(*_args: Any, **_kwargs: Any):
        network_attempts.append("blocked")
        raise AssertionError("network access is forbidden in operational golden flows")

    def block_provider(*_args: Any, **_kwargs: Any):
        provider_attempts.append("blocked")
        raise AssertionError("provider exchange must not run for a state-mismatch callback")

    def block_mount(*_args: Any, **_kwargs: Any):
        mount_attempts.append("blocked")
        raise AssertionError("NAS mount-on-demand is forbidden in operational golden flows")

    fake_nas_guard = ModuleType("api.nas_mount_guard")
    fake_nas_guard.ensure_nas_mounts = block_mount

    with _FLOW_LOCK:
        previous_root = os.environ.get("PAPERCLIP_FILEMANAGER_TEST_BASE")
        previous_tempdir = tempfile.tempdir
        os.environ["PAPERCLIP_FILEMANAGER_TEST_BASE"] = str(allowed)
        tempfile.tempdir = str(staging)
        try:
            with (
                patch.object(osc_cases, "_osc_audit_file_event", lambda *_a, **_k: None),
                patch.object(osc_files, "_audit_file_event", lambda *_a, **_k: None),
                patch.object(osc_cases, "urlopen", block_network),
                patch.object(osc_files, "urlopen", block_network),
                patch.object(osc_gcal, "_get_setting", block_provider),
                patch.object(osc_gcal.logger, "warning", lambda *_a, **_k: None),
                patch.dict(sys.modules, {"api.nas_mount_guard": fake_nas_guard}),
            ):
                app = _isolated_app(include_gcal=True)
                compat = LazyCompatibilityApp(
                    "5002",
                    inventory=inventory,
                    loader=lambda _service: app,
                    verifier=lambda _app, _service, _inventory: None,
                )
                bound = app.url_map.bind("localhost")
                for _service, rule, method, endpoint in required:
                    concrete = rule
                    matched, _values = bound.match(concrete, method=method)
                    if matched != endpoint:
                        raise ContractValidationError(
                            f"operational route dispatch drift: {method} {rule} -> {matched}"
                        )

                client = Client(compat, Response, use_cookies=True)
                login_response = client.post("/__offline_contract/login")
                browse = client.get(
                    "/api/osc/folders/browse?"
                    + urlencode(
                        {
                            "base_path": str(allowed),
                            "summarize_dirs": "0",
                        }
                    )
                )
                tree = client.get(
                    "/api/osc/folders/tree?" + urlencode({"base_path": str(allowed)})
                )
                info = client.get(
                    "/api/osc/files/info?" + urlencode({"path": str(workbook_path)})
                )
                office = client.post(
                    "/api/osc/labor-law/parse-files",
                    json={"file_paths": [str(workbook_path)], "monthly_wage": 48000},
                )
                state_set = client.post("/__offline_contract/session/gcal-state")
                callback = client.get(
                    "/api/osc/gcal/auth/callback?"
                    + urlencode({"code": "offline-code", "state": "mismatched-state"})
                )
                state_after = client.get("/__offline_contract/session/gcal-state")
        finally:
            tempfile.tempdir = previous_tempdir
            if previous_root is None:
                os.environ.pop("PAPERCLIP_FILEMANAGER_TEST_BASE", None)
            else:
                os.environ["PAPERCLIP_FILEMANAGER_TEST_BASE"] = previous_root

    browse_json = browse.get_json() or {}
    tree_json = tree.get_json() or {}
    info_json = info.get_json() or {}
    office_json = office.get_json() or {}
    state_after_json = state_after.get_json() or {}
    outcomes = {
        "login_status": login_response.status_code,
        "nas_browse_status": browse.status_code,
        "nas_browse_folder_names": [row.get("name") for row in browse_json.get("folders", [])],
        "nas_browse_file_names": [row.get("name") for row in browse_json.get("files", [])],
        "nas_browse_hidden_count": browse_json.get("hidden_count"),
        "nas_tree_status": tree.status_code,
        "nas_tree_children": [
            {"name": row.get("name"), "has_subdirs": row.get("has_subdirs")}
            for row in tree_json.get("children", [])
        ],
        "nas_info_status": info.status_code,
        "nas_info_projection": {
            "ok": info_json.get("ok"),
            "name": info_json.get("name"),
            "ext": info_json.get("ext"),
            "kind": info_json.get("kind"),
            "size_positive": isinstance(info_json.get("size"), int) and info_json["size"] > 0,
        },
        "office_parse_status": office.status_code,
        "office_parse_projection": {
            "ok": office_json.get("ok"),
            "error": office_json.get("error"),
            "total_records": office_json.get("total_records"),
            "total_ot_hours": office_json.get("total_ot_hours"),
            "errors": office_json.get("errors"),
            "record": (office_json.get("records") or [None])[0],
        },
        "provider_state_set_status": state_set.status_code,
        "provider_callback_status": callback.status_code,
        "provider_callback_state_error": "授權狀態驗證失敗" in callback.get_data(as_text=True),
        "provider_session_state_present_after_callback": state_after_json.get("present"),
    }
    expected = {
        "login_status": 200,
        "nas_browse_status": 200,
        "nas_browse_folder_names": ["Case-A"],
        "nas_browse_file_names": ["attendance-nonsensitive.xlsx"],
        "nas_browse_hidden_count": 1,
        "nas_tree_status": 200,
        "nas_tree_children": [{"name": "Case-A", "has_subdirs": True}],
        "nas_info_status": 200,
        "nas_info_projection": {
            "ok": True,
            "name": "attendance-nonsensitive.xlsx",
            "ext": ".xlsx",
            "kind": "office",
            "size_positive": True,
        },
        "office_parse_status": 200,
        "office_parse_projection": {
            "ok": True,
            "error": None,
            "total_records": 1,
            "total_ot_hours": 1.5,
            "errors": [],
            "record": {
                "date": "115/07/13",
                "day_type": "平日",
                "note": "",
                "ot_pay": 400.0,
                "post_ot_min": 60,
                "pre_ot_min": 30,
                "source": "excel",
                "total_ot_min": 90,
                "weekday": "一",
            },
        },
        "provider_state_set_status": 200,
        "provider_callback_status": 400,
        "provider_callback_state_error": True,
        "provider_session_state_present_after_callback": False,
    }
    if outcomes != expected:
        raise ContractValidationError(
            f"operational golden-flow outcome drift: expected={expected!r}, observed={outcomes!r}"
        )
    if network_attempts or provider_attempts or mount_attempts:
        raise ContractValidationError("operational golden flow crossed an external boundary")
    staged_files_remaining = sum(1 for path in staging.rglob("*") if path.is_file())
    if staged_files_remaining:
        raise ContractValidationError("operational golden flow left staged files behind")

    cases = []
    domain_by_rule = {
        "/api/osc/folders/browse": "nas_file_workflows",
        "/api/osc/folders/tree": "nas_file_workflows",
        "/api/osc/files/info": "nas_file_workflows",
        "/api/osc/labor-law/parse-files": "office_document_workflows",
        "/api/osc/gcal/auth/callback": "provider_and_session_integrations",
    }
    branch_by_rule = {
        "/api/osc/folders/browse": "existing_allowlisted_sandbox_directory",
        "/api/osc/folders/tree": "existing_allowlisted_sandbox_tree",
        "/api/osc/files/info": "existing_allowlisted_office_file_metadata",
        "/api/osc/labor-law/parse-files": "synthetic_xlsx_success",
        "/api/osc/gcal/auth/callback": "csrf_state_mismatch_before_provider_exchange",
    }
    for route in reviewed_routes:
        validation_guard_only = route["rule"] == "/api/osc/gcal/auth/callback"
        case = {
            **route,
            "domain": domain_by_rule[route["rule"]],
            "covered_branch": branch_by_rule[route["rule"]],
            "actual_handler_dispatched": True,
            "auth_or_not_found_status_used_as_proof": False,
            "branch_class": (
                "validation_guard_only"
                if validation_guard_only
                else "representative_success_path"
            ),
            "representative_success_path": not validation_guard_only,
            "validation_guard_only": validation_guard_only,
            "expected_outcomes_sha256": _digest(_canonical(expected)),
            "observed_outcomes_sha256": _digest(_canonical(outcomes)),
            "passed": True,
        }
        case["evidence_sha256"] = _digest(_canonical(case))
        cases.append(case)

    evidence = {
        "schema_version": 1,
        "flow_id": "nas_office_provider_session_v1",
        "inventory_counts": inventory.counts,
        "inventory_fingerprint": validated_inventory["fingerprint"],
        "handler_source_sha256": handler_source_sha256,
        "reviewed_routes": reviewed_routes,
        "cases": cases,
        "case_count": len(cases),
        "domains": [
            "nas_file_workflows",
            "office_document_workflows",
            "provider_and_session_integrations",
        ],
        "expected_outcomes_sha256": _digest(_canonical(expected)),
        "observed_outcomes_sha256": _digest(_canonical(outcomes)),
        "outcomes": outcomes,
        "transport": "in_process_wsgi_v3_compat",
        "network_access_performed": False,
        "provider_exchange_performed": False,
        "nas_mount_attempted": False,
        "external_writes_performed": False,
        "sandbox_writes_only": True,
        "staged_files_remaining": staged_files_remaining,
        "production_state_accessed": False,
        "service_start_performed": False,
        "passed": True,
    }
    evidence["evidence_sha256"] = _digest(_canonical(evidence))
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one synthetic OSC golden flow without a listener or external writes."
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=BEHAVIOR_FIXTURE_ROOT / "osc-file-content.json",
    )
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        evidence = run_osc_file_golden_flow(args.fixture, args.workspace)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "passed": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(evidence, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

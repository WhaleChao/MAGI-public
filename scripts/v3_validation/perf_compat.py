#!/usr/bin/env python3
"""Offline matched benchmark for V2, V3 compatibility, and native V3 probes.

This benchmark deliberately measures one narrow claim: the incremental cost of
placing the V3 ``LazyCompatibilityApp`` in front of a V2 handler, or comparing
the actual V2 liveness handler with the actual native V3 gateway probe handler.
It does not start listeners, import either production service, or mutate live
state.  The report separately records the current absence of a native V3
business route; a compatibility factory is never relabelled as a native route.

Correctness and workload identity are gates, not observations.  Results are
only compared after both isolated child processes prove the same runtime,
pinned inventory route, request-plan digest, and per-request response digest.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import inspect
import json
import math
import os
import platform
import resource
import sqlite3
import socket
import statistics
import subprocess
import sys
import time
import tracemalloc
from array import array
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, Mapping, Sequence
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
sys.dont_write_bytecode = True

from flask import Flask, Response, request
from werkzeug.test import Client

from magi_v3.compat.gateway import LazyCompatibilityApp, RouteInventory, RouteSpec
from scripts.v3_validation.inventory import EXPECTED_FINGERPRINT, load_and_validate_runtime_inventory


SCRIPT_PATH = Path(__file__).resolve()
SCHEMA_VERSION = 1
FIXTURE_VERSION = "matched-compat-v1"
BLOCKER_CODE = "MATCHED_PRODUCTION_PERFORMANCE_NOT_IMPLEMENTED"
PINNED_ROUTE = RouteSpec("5002", "/livez", ("GET",), "admin_runtime.livez")
MODES = ("v2_direct_wsgi", "v3_compat_wsgi")
NATIVE_MODES = ("v2_actual_livez_wsgi", "v3_native_gateway_livez_wsgi")
OSC_CASE_MODES = ("v2_actual_osc_cases_wsgi", "v3_native_osc_cases_wsgi")
ALL_MODES = MODES + NATIVE_MODES + OSC_CASE_MODES
WORKLOADS = (
    "fixture_livez",
    "actual_v2_livez",
    "native_gateway_livez",
    "synthetic_osc_cases",
)
DEFAULT_WORKLOAD = "native_gateway_livez"
BUSINESS_ROUTE = RouteSpec(
    "5002", "/api/osc/cases", ("GET", "POST"), "osc_cases.osc_cases_api"
)
REQUEST_CASES: tuple[tuple[str, str], ...] = (
    ("baseline", "trace-00"),
    ("ascii", "trace-01"),
    ("unicode-%E5%8F%B0%E7%81%A3", "trace-02"),
    ("repeat", "trace-03"),
)
SYNTHETIC_BUSINESS_REQUESTS: tuple[dict[str, Any], ...] = (
    {
        "case_id": "case_list_open_filtered",
        "method": "GET",
        "path": "/api/osc/cases?q=SYNTHETIC-PERF-CASE&status_scope=open&limit=25",
        "headers": {"Accept": "application/json", "X-MAGI-Synthetic": "1"},
        "body": None,
    },
    {
        "case_id": "case_upsert_existing",
        "method": "POST",
        "path": "/api/osc/cases",
        "headers": {"Content-Type": "application/json", "X-MAGI-Synthetic": "1"},
        "body": {
            "id": "synthetic-case-000",
            "case_number": "2026-0001",
            "client_name": "SYNTHETIC-PERF-CASE-000",
            "case_category": "一般案件",
            "case_type": "民事",
            "case_reason": "損害賠償",
            "lawyer": "離線合成律師",
            "status": "進行中",
            "notes": "production-shaped-post-fixture",
            "auto_create_folder": False,
        },
    },
)
SYNTHETIC_OSC_ROWS: tuple[dict[str, Any], ...] = tuple(
    {
        "id": f"synthetic-case-{index:03d}",
        "case_number": f"2026-{index + 1:04d}",
        "client_name": f"SYNTHETIC-PERF-CASE-{index:03d}",
        "case_category": "一般案件",
        "case_type": "民事",
        "case_stage": "一審",
        "case_reason": "損害賠償",
        "laf_case_no": None,
        "application_no": None,
        "court_name": "臺灣合成地方法院",
        "court_case_no": f"115年度合字第{index + 1}號",
        "court_division": "合股",
        "legal_aid_status": "",
        "lawyer": "離線合成律師",
        "status": "進行中",
        "manual_status_lock": 0,
        "manual_status_source": None,
        "manual_status_at": None,
        "notes": "synthetic-offline-only",
        "folder_path": f"Z:\\synthetic\\2026-{index + 1:04d}",
        "updated_at": f"2026-07-14 12:{59 - index:02d}:00",
        "created_date": f"2026-07-14 11:{59 - index:02d}:00",
    }
    for index in range(32)
)
SYNTHETIC_OSC_REQUEST_PLAN: tuple[dict[str, Any], ...] = (
    {
        "case_id": "case_list_open_filtered",
        "method": "GET",
        "path": "/api/osc/cases?q=SYNTHETIC-PERF-CASE&status_scope=open&limit=25",
        "headers": {"Accept": "application/json", "X-MAGI-Synthetic": "1"},
        "body": None,
    },
    {
        "case_id": "case_upsert_existing",
        "method": "POST",
        "path": "/api/osc/cases",
        "headers": {"Content-Type": "application/json", "X-MAGI-Synthetic": "1"},
        "body": {
            "id": "synthetic-case-000",
            "case_number": "2026-0001",
            "client_name": "SYNTHETIC-PERF-CASE-000",
            "case_category": "一般案件",
            "case_type": "民事",
            "case_reason": "損害賠償",
            "lawyer": "離線合成律師",
            "status": "進行中",
            "notes": "production-shaped-post-fixture",
            "auto_create_folder": False,
        },
    },
)
SYNTHETIC_OSC_SCHEMA = """
CREATE TABLE cases (
    id TEXT PRIMARY KEY,
    case_number TEXT NOT NULL UNIQUE,
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
    legal_aid_status TEXT DEFAULT '',
    lawyer TEXT,
    status TEXT DEFAULT '進行中',
    manual_status_lock INTEGER NOT NULL DEFAULT 0,
    manual_status_source TEXT,
    manual_status_at TEXT,
    notes TEXT,
    folder_path TEXT,
    updated_at TEXT NOT NULL,
    created_date TEXT NOT NULL
)
"""


class PerfEvidenceError(RuntimeError):
    """Matched evidence is invalid and must not be compared."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_evidence_hash(evidence: Mapping[str, Any]) -> None:
    supplied = evidence.get("evidence_sha256")
    if not isinstance(supplied, str) or len(supplied) != 64:
        raise PerfEvidenceError("evidence_sha256 is missing")
    unhashed = dict(evidence)
    del unhashed["evidence_sha256"]
    if _sha256_bytes(_canonical_json(unhashed)) != supplied:
        raise PerfEvidenceError("evidence_sha256 does not match canonical evidence")


def _request_plan(workload: str = "fixture_livez") -> tuple[dict[str, Any], ...]:
    if workload not in WORKLOADS:
        raise PerfEvidenceError(f"unsupported benchmark workload: {workload!r}")
    if workload == "synthetic_osc_cases":
        return tuple(dict(row) for row in SYNTHETIC_OSC_REQUEST_PLAN)
    return tuple(
        {
            "method": "GET",
            "path": f"/livez?case={case}",
            "headers": {"X-MAGI-Perf-Case": trace},
        }
        for case, trace in REQUEST_CASES
    )


def _request_plan_sha256(workload: str = "fixture_livez") -> str:
    return _sha256_bytes(_canonical_json(_request_plan(workload)))


def _decoded_case(path: str) -> str:
    # Werkzeug performs URL decoding before the Flask view sees ``request.args``.
    from urllib.parse import parse_qs, urlsplit

    return parse_qs(urlsplit(path).query, keep_blank_values=True)["case"][0]


def _expected_body(row: Mapping[str, Any]) -> bytes:
    payload = {
        "case": _decoded_case(str(row["path"])),
        "fixture": FIXTURE_VERSION,
        "method": str(row["method"]),
        "path": "/livez",
        "trace": str(row["headers"]["X-MAGI-Perf-Case"]),
    }
    return _canonical_json(payload) + b"\n"


def build_offline_app() -> Flask:
    """Build a deterministic route fixture with the pinned V2 route identity."""

    app = Flask("magi_v3_matched_perf_fixture")

    def livez() -> Response:
        body = _canonical_json(
            {
                "case": request.args.get("case", ""),
                "fixture": FIXTURE_VERSION,
                "method": request.method,
                "path": request.path,
                "trace": request.headers.get("X-MAGI-Perf-Case", ""),
            }
        ) + b"\n"
        response = Response(body, status=200, content_type="application/json")
        response.headers["X-MAGI-Perf-Fixture"] = FIXTURE_VERSION
        return response

    app.add_url_rule(
        PINNED_ROUTE.rule,
        endpoint=PINNED_ROUTE.endpoint,
        view_func=livez,
        methods=list(PINNED_ROUTE.methods),
    )
    return app


def build_actual_livez_app(sandbox_root: Path) -> Flask:
    """Compose the production V2 blueprint without importing/starting its service."""

    from api.blueprints.admin_runtime import create_admin_runtime_blueprint

    class OfflineDatabase:
        @staticmethod
        def connect(**_kwargs: Any) -> Any:
            raise PerfEvidenceError("actual /livez workload attempted a database connection")

    app = Flask("magi_v3_actual_livez_perf")
    app.config.update(TESTING=True, SECRET_KEY="offline-perf-only")
    blueprint = create_admin_runtime_blueprint(
        logger=app.logger,
        orchestrator=object(),
        require_json_auth=lambda admin=False: None,
        list_skill_docs=lambda: [],
        nerv_skill_interview_user_id=lambda: "offline-perf",
        extract_interview_skill_name=lambda _message: "",
        skill_doc_path=lambda name: sandbox_root / "skills" / name / "SKILL.md",
        skill_action_path=lambda name: sandbox_root / "skills" / name / "action.py",
        skill_summary=lambda content: str(content or "").strip(),
        nerv_product_runtime_payload=lambda: {"ok": True},
        nerv_product_names=(),
        update_product_runtime=lambda product, **updates: updates,
        cloudflared_alive=lambda: False,
        server_start_time=time.time() - 60,
        attachment_job_queue=None,
        list_attachment_job_ids=lambda: [],
        read_attachment_job=lambda _job_id: {},
        expected_magi_api_key="offline-perf-only",
        db_config={},
        mysql_connector=OfflineDatabase,
        safe_remove_tmp=lambda _path: (_ for _ in ()).throw(
            PerfEvidenceError("actual /livez workload attempted a filesystem mutation")
        ),
        magi_root=sandbox_root,
    )
    app.register_blueprint(blueprint)
    return app


def build_native_gateway_livez_app() -> Any:
    """Compose the real native V3 probe handler using only in-memory dependencies."""

    from magi_v3.gateway import Gateway, GatewayConfig, ReleaseOwnership, _ProbeMiddleware
    from magi_v3.health import HealthService
    from magi_v3.service_manifest import ServiceDefinition

    class OfflineRoleGuard:
        acquired = False

        @staticmethod
        def acquire() -> None:
            raise PerfEvidenceError("native probe benchmark attempted to acquire a role lock")

        @staticmethod
        def release() -> None:
            return None

    class OfflineGovernor:
        @staticmethod
        def active_counts() -> dict[str, int]:
            return {"light": 0, "heavy": 0}

    def fallback(_environ: dict[str, Any], _start_response: Any) -> Any:
        raise PerfEvidenceError("native probe benchmark escaped the /livez handler")

    governor = OfflineGovernor()
    health = HealthService(
        ledger=object(),
        governor=governor,
        state_dir=Path("/synthetic/magi-v3-perf"),
        active_guard=OfflineRoleGuard(),
    )
    runtime = SimpleNamespace(health=health, governor=governor)
    services = tuple(
        (
            ServiceDefinition(
                service_id=service_id,
                role="gateway",
                kind="wsgi",
                required=True,
                port=port,
                factory=f"synthetic:{service_id}",
            ),
            lambda: fallback,
        )
        for service_id, port in (("main_http", 5002), ("tools_http", 5003))
    )
    gateway = Gateway(
        runtime=runtime,
        ownership=ReleaseOwnership(
            release_id="synthetic-perf",
            manifest_path=Path("/synthetic/ownership.json"),
            release_manifest_sha256="0" * 64,
            gateway_binding={},
            control_binding={},
        ),
        role_guard=OfflineRoleGuard(),
        control_owner=lambda: False,
        services=services,
        config=GatewayConfig(),
    )
    return _ProbeMiddleware(fallback, gateway)


class SyntheticOscDatabase:
    """Private disposable corpus shared semantically by both OSC benchmark arms."""

    _SEED_COLUMNS = tuple(SYNTHETIC_OSC_ROWS[0])

    def __init__(self) -> None:
        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(SYNTHETIC_OSC_SCHEMA)
        placeholders = ",".join("?" for _ in self._SEED_COLUMNS)
        self.connection.executemany(
            f"INSERT INTO cases ({','.join(self._SEED_COLUMNS)}) VALUES ({placeholders})",
            [tuple(row[column] for column in self._SEED_COLUMNS) for row in SYNTHETIC_OSC_ROWS],
        )
        self.connection.commit()
        self.select_count = 0
        self._transaction_events: list[str] = []
        self.connection.set_trace_callback(self._trace_statement)

    def _trace_statement(self, sql: str) -> None:
        statement = " ".join(str(sql or "").strip().upper().split())
        if statement.startswith("BEGIN"):
            self._transaction_events.append("begin")
        elif statement.startswith("INSERT INTO CASES"):
            self._transaction_events.append("insert")
        elif statement.startswith("UPDATE CASES"):
            self._transaction_events.append("update")
        elif statement == "COMMIT":
            self._transaction_events.append("commit")
        elif statement == "ROLLBACK":
            self._transaction_events.append("rollback")

    @property
    def corpus_sha256(self) -> str:
        return _sha256_bytes(_canonical_json(SYNTHETIC_OSC_ROWS))

    def v2_exec(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
        *,
        fetch: str = "none",
    ) -> tuple[Any, dict[str, str]]:
        statement = str(sql or "").strip()
        upper = statement.upper()
        translated = statement.replace("%s", "?").replace("NOW()", "CURRENT_TIMESTAMP")
        if upper.startswith("SELECT "):
            if fetch not in {"one", "all"}:
                raise PerfEvidenceError("synthetic V2 SELECT requested an invalid fetch mode")
            self.select_count += 1
            cursor = self.connection.execute(translated, tuple(params))
            if fetch == "one":
                row = cursor.fetchone()
                return (dict(row) if row is not None else None), {
                    "backend": "synthetic_sqlite"
                }
            return [dict(row) for row in cursor.fetchall()], {"backend": "synthetic_sqlite"}
        if upper.startswith("INSERT INTO CASES ") and fetch == "none":
            columns_text = statement[statement.index("(") + 1 : statement.index(")")]
            columns = [column.strip() for column in columns_text.split(",")]
            values = dict(zip(columns, params))
            duplicate = self.connection.execute(
                "SELECT 1 FROM cases WHERE id = ? OR case_number = ? LIMIT 1",
                (values.get("id"), values.get("case_number")),
            ).fetchone()
            if duplicate is not None:
                raise RuntimeError("1062 Duplicate entry in disposable OSC fixture")
            cursor = self.connection.execute(translated, tuple(params))
            self.connection.commit()
            return {"rowcount": cursor.rowcount, "lastrowid": cursor.lastrowid}, {
                "backend": "synthetic_sqlite"
            }
        if (
            upper.startswith("UPDATE CASES SET ")
            and " WHERE ID=%S" in upper
            and fetch == "none"
            and params
        ):
            cursor = self.connection.execute(translated, tuple(params))
            self.connection.commit()
            return {"rowcount": cursor.rowcount, "lastrowid": cursor.lastrowid}, {
                "backend": "synthetic_sqlite"
            }
        raise PerfEvidenceError(
            "synthetic V2 OSC request escaped the bounded SELECT/INSERT/UPDATE database boundary"
        )

    def transaction_event_offset(self) -> int:
        return len(self._transaction_events)

    def transaction_events_since(self, offset: int) -> list[str]:
        return list(self._transaction_events[offset:])

    def side_effect_evidence(
        self, post_transaction_transcripts: Sequence[Sequence[str]]
    ) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT id,case_number,client_name,case_category,case_type,case_reason,lawyer,status,notes,folder_path "
            "FROM cases WHERE id = ?",
            ("synthetic-case-000",),
        ).fetchone()
        if row is None:
            raise PerfEvidenceError("synthetic POST target disappeared")
        projection = dict(row)
        flattened_events = [
            event for transcript in post_transaction_transcripts for event in transcript
        ]
        counts = {
            event: flattened_events.count(event)
            for event in ("begin", "insert", "update", "commit", "rollback")
        }
        return {
            "database": "sqlite_memory_disposable",
            "target_state_sha256": _sha256_bytes(_canonical_json(projection)),
            "target_state": projection,
            "transaction_event_counts": counts,
            "post_transaction_count": len(post_transaction_transcripts),
            "post_transaction_transcript_sha256": _sha256_bytes(
                _canonical_json(list(post_transaction_transcripts))
            ),
            "balanced_transactions": (
                counts["begin"] == counts["commit"] == len(post_transaction_transcripts)
                and counts["update"] == len(post_transaction_transcripts)
                and counts["rollback"] == counts["insert"] == 0
                and all(list(transcript) == ["begin", "update", "commit"] for transcript in post_transaction_transcripts)
            ),
            "external_writes": False,
            "production_state_accessed": False,
            "nas_accessed": False,
        }


class _V2SyntheticOscWSGI:
    def __init__(self, app: Flask, database: SyntheticOscDatabase, module: Any) -> None:
        self.app = app
        self.database = database
        self.module = module
        self.post_transaction_transcripts: list[list[str]] = []

    def __call__(self, environ: dict[str, Any], start_response: Any) -> Any:
        offset = self.database.transaction_event_offset()
        def forbidden_setting(*_args: Any, **_kwargs: Any) -> Any:
            raise PerfEvidenceError("synthetic OSC benchmark attempted a settings lookup")

        def forbidden_path(*_args: Any, **_kwargs: Any) -> Any:
            raise PerfEvidenceError("synthetic OSC benchmark attempted NAS/path resolution")

        with patch.object(self.module, "_osc_exec", side_effect=self.database.v2_exec), patch.object(
            self.module,
            "_get_translate_local_path_to_canonical",
            return_value=lambda value: value,
        ), patch.object(self.module, "_CASE_MANUAL_STATUS_SCHEMA_READY", True), patch.object(
            self.module, "_osc_get_setting_value", side_effect=forbidden_setting
        ), patch.object(
            self.module, "_osc_resolve_existing_local_path", side_effect=forbidden_path
        ):
            result = self.app(environ, start_response)
        if str(environ.get("REQUEST_METHOD") or "").upper() == "POST":
            self.post_transaction_transcripts.append(
                self.database.transaction_events_since(offset)
            )
        return result

    def side_effect_evidence(self) -> dict[str, Any]:
        return self.database.side_effect_evidence(self.post_transaction_transcripts)


def build_actual_osc_cases_app() -> Any:
    """Mount the actual V2 OSC blueprint over a disposable synthetic database."""

    from flask_login import LoginManager
    import api.blueprints.osc_cases as osc_module

    app = Flask("magi_v3_actual_osc_cases_perf")
    app.config.update(
        TESTING=True,
        LOGIN_DISABLED=True,
        PROPAGATE_EXCEPTIONS=True,
        SECRET_KEY="synthetic-osc-perf-only",
    )
    LoginManager().init_app(app)
    app.register_blueprint(osc_module.osc_bp)
    return _V2SyntheticOscWSGI(app, SyntheticOscDatabase(), osc_module)


class _OfflineCsrf:
    @staticmethod
    def validate(_environ: Mapping[str, Any]) -> tuple[bool, str]:
        return True, "synthetic_get_only"

    @staticmethod
    def safe_response_cookie(_environ: Mapping[str, Any]) -> None:
        return None


def build_native_osc_cases_app() -> Any:
    """Compose the actual native OSC route over the identical synthetic corpus."""

    from magi_v3.osc_cases import OscCasesApplication, OscCasesService, SQLiteCaseStore
    from magi_v3.osc_main import V2SecurityHeaderPolicy

    database = SyntheticOscDatabase()
    service = OscCasesService(
        SQLiteCaseStore(database.connection),
        id_factory=lambda: (_ for _ in ()).throw(
            PerfEvidenceError("GET-only synthetic benchmark attempted to allocate a case id")
        ),
        lawyer_resolver=lambda current, _case_type, _reason, _category: current,
    )
    application = OscCasesApplication(
        service,
        authorize=lambda _environ: True,
        csrf=_OfflineCsrf(),
        response_security_headers=V2SecurityHeaderPolicy({}),
    )
    return _SyntheticOscWSGI(application, database)


class _SyntheticOscWSGI:
    def __init__(self, application: Any, database: SyntheticOscDatabase) -> None:
        self.application = application
        self.database = database
        self.post_transaction_transcripts: list[list[str]] = []

    def __call__(self, environ: dict[str, Any], start_response: Any) -> Any:
        offset = self.database.transaction_event_offset()
        result = self.application(environ, start_response)
        if str(environ.get("REQUEST_METHOD") or "").upper() == "POST":
            self.post_transaction_transcripts.append(
                self.database.transaction_events_since(offset)
            )
        return result

    def side_effect_evidence(self) -> dict[str, Any]:
        return self.database.side_effect_evidence(self.post_transaction_transcripts)


def _modes_for_workload(workload: str) -> tuple[str, str]:
    if workload == "native_gateway_livez":
        return NATIVE_MODES
    if workload == "synthetic_osc_cases":
        return OSC_CASE_MODES
    return MODES


def _route_for_workload(workload: str) -> RouteSpec:
    return BUSINESS_ROUTE if workload == "synthetic_osc_cases" else PINNED_ROUTE


def _handler_identity(mode: str, workload: str) -> dict[str, Any]:
    if workload == "synthetic_osc_cases":
        if mode == "v2_actual_osc_cases_wsgi":
            from api.blueprints.osc_cases import osc_cases_api as view

            return {
                "implementation": "production_v2",
                "callable": f"{view.__module__}.{view.__qualname__}",
                "source_sha256": _sha256_bytes(inspect.getsource(view).encode("utf-8")),
            }
        if mode == "v3_native_osc_cases_wsgi":
            from magi_v3.osc_cases import (
                OscCasesApplication,
                OscCasesService,
                SQLiteCaseTransaction,
            )

            source = "\n".join(
                (
                    inspect.getsource(OscCasesApplication.__call__),
                    inspect.getsource(OscCasesApplication.response),
                    inspect.getsource(OscCasesService.list_cases),
                    inspect.getsource(OscCasesService.create_case),
                    inspect.getsource(SQLiteCaseTransaction.list_cases),
                    inspect.getsource(SQLiteCaseTransaction.update_case),
                )
            )
            return {
                "implementation": "native_v3",
                "callable": (
                    "magi_v3.osc_cases.OscCasesApplication -> "
                    "OscCasesService.list_cases -> SQLiteCaseTransaction.list_cases"
                ),
                "source_sha256": _sha256_bytes(source.encode("utf-8")),
            }
        raise PerfEvidenceError("synthetic OSC workload received an invalid mode")
    if workload == "native_gateway_livez" and mode == "v3_native_gateway_livez_wsgi":
        from magi_v3.gateway import Gateway, _ProbeMiddleware
        from magi_v3.health import HealthService

        source = "\n".join(
            (
                inspect.getsource(_ProbeMiddleware.__call__),
                inspect.getsource(Gateway.liveness),
                inspect.getsource(HealthService.liveness),
            )
        )
        return {
            "implementation": "native_v3",
            "callable": "magi_v3.gateway._ProbeMiddleware.__call__ -> Gateway.liveness -> HealthService.liveness",
            "source_sha256": _sha256_bytes(source.encode("utf-8")),
        }
    if workload in {"actual_v2_livez", "native_gateway_livez"}:
        app = build_actual_livez_app(Path(os.environ.get("TMPDIR", "/tmp")).resolve())
        view = app.view_functions[PINNED_ROUTE.endpoint]
        return {
            "implementation": "production_v2",
            "callable": f"{view.__module__}.{view.__qualname__}",
            "source_sha256": _sha256_bytes(inspect.getsource(view).encode("utf-8")),
        }
    return {
        "implementation": "fixture",
        "callable": f"{__name__}.build_offline_app",
        "source_sha256": _sha256_bytes(inspect.getsource(build_offline_app).encode("utf-8")),
    }


def _verify_fixture_surface(app: Any, service: str, inventory: RouteInventory) -> None:
    if service != PINNED_ROUTE.service or PINNED_ROUTE not in inventory.routes:
        raise PerfEvidenceError("pinned route identity is absent from the runtime inventory")
    observed = {
        RouteSpec(
            service,
            str(rule.rule),
            tuple(sorted(set(rule.methods or ()) & {"GET", "POST", "PUT", "PATCH", "DELETE"})),
            str(rule.endpoint),
        )
        for rule in app.url_map.iter_rules()
        if rule.endpoint != "static"
    }
    if observed != {PINNED_ROUTE}:
        raise PerfEvidenceError(f"offline fixture surface drifted: {sorted(observed)!r}")


def _verify_actual_livez_surface(app: Any, service: str, inventory: RouteInventory) -> None:
    if service != PINNED_ROUTE.service or PINNED_ROUTE not in inventory.routes:
        raise PerfEvidenceError("pinned actual-handler route is absent from the runtime inventory")
    matches = [
        rule
        for rule in app.url_map.iter_rules()
        if str(rule.rule) == PINNED_ROUTE.rule
        and str(rule.endpoint) == PINNED_ROUTE.endpoint
        and "GET" in set(rule.methods or ())
    ]
    if len(matches) != 1:
        raise PerfEvidenceError("actual production /livez route identity is not unique")
    view = app.view_functions.get(PINNED_ROUTE.endpoint)
    if view is None or view.__module__ != "api.blueprints.admin_runtime" or view.__name__ != "livez":
        raise PerfEvidenceError("actual /livez view is not the production blueprint handler")


def _verify_actual_osc_surface(app: Any) -> None:
    matches = [
        rule
        for rule in app.url_map.iter_rules()
        if str(rule.rule) == BUSINESS_ROUTE.rule
        and str(rule.endpoint) == BUSINESS_ROUTE.endpoint
        and {"GET", "POST"}.issubset(set(rule.methods or ()))
    ]
    if len(matches) != 1:
        raise PerfEvidenceError("actual production OSC route identity is not unique")
    view = app.view_functions.get(BUSINESS_ROUTE.endpoint)
    if (
        view is None
        or view.__module__ != "api.blueprints.osc_cases"
        or view.__name__ != "osc_cases_api"
    ):
        raise PerfEvidenceError("actual OSC view is not the production blueprint handler")


def _make_client(mode: str, workload: str = "fixture_livez") -> Client:
    if mode not in _modes_for_workload(workload):
        raise PerfEvidenceError(f"unsupported benchmark mode: {mode!r}")
    if workload == "native_gateway_livez":
        target = (
            build_actual_livez_app(Path(os.environ.get("TMPDIR", "/tmp")).resolve())
            if mode == "v2_actual_livez_wsgi"
            else build_native_gateway_livez_app()
        )
        return Client(target, Response)
    if workload == "synthetic_osc_cases":
        if mode == "v2_actual_osc_cases_wsgi":
            target = build_actual_osc_cases_app()
            _verify_actual_osc_surface(target.app)
        else:
            target = build_native_osc_cases_app()
        return Client(target, Response)
    if workload == "fixture_livez":
        app = build_offline_app()
        verifier = _verify_fixture_surface
    elif workload == "actual_v2_livez":
        app = build_actual_livez_app(Path(os.environ.get("TMPDIR", "/tmp")).resolve())
        verifier = _verify_actual_livez_surface
    else:
        raise PerfEvidenceError(f"unsupported benchmark workload: {workload!r}")
    if mode == "v2_direct_wsgi":
        target = app
    else:
        inventory = RouteInventory.load()
        target = LazyCompatibilityApp(
            PINNED_ROUTE.service,
            inventory=inventory,
            loader=lambda _service: app,
            verifier=verifier,
        )
    return Client(target, Response)


def _expected_projection(row: Mapping[str, Any], workload: str) -> dict[str, Any]:
    if workload == "synthetic_osc_cases":
        if str(row.get("method")) == "POST":
            body = row.get("body") or {}
            return {
                "http_status": 200,
                "ok": True,
                "id": body.get("id"),
                "case_number": body.get("case_number"),
                "mode": "upsert",
            }
        projected = [_project_osc_item(item) for item in SYNTHETIC_OSC_ROWS[:25]]
        return {
            "http_status": 200,
            "ok": True,
            "item_count": len(projected),
            "items_sha256": _sha256_bytes(_canonical_json(projected)),
        }
    if workload == "native_gateway_livez":
        return {
            "http_status": 200,
            "live": True,
            "probe_kind": "process_liveness",
            "status": "live",
        }
    if workload == "actual_v2_livez":
        return {
            "ok": True,
            "probe": "liveness",
            "readiness_checked": False,
            "status": "live",
            "timestamp_numeric": True,
            "uptime_seconds_numeric": True,
        }
    body = _expected_body(row)
    return {
        "body_sha256": _sha256_bytes(body),
        "content_length": len(body),
        "content_type": "application/json",
        "fixture_header": FIXTURE_VERSION,
        "status_code": 200,
    }


def _project_osc_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "case_number": item.get("case_number"),
        "client_name": item.get("client_name"),
        "case_category": item.get("case_category"),
        "case_type": item.get("case_type"),
        "case_reason": item.get("case_reason"),
        "lawyer": item.get("lawyer"),
        "status": item.get("status"),
        "effective_status": "進行中",
        "status_display": "進行中",
        "case_type_display": "民事",
        "case_reason_display": item.get("case_reason"),
        "folder_path": item.get("folder_path"),
    }


def _response_signature(
    response: Any, row: Mapping[str, Any], workload: str, mode: str
) -> bytes:
    if workload == "synthetic_osc_cases":
        if response.status_code != 200 or response.headers.get("Content-Type", "") != "application/json":
            raise PerfEvidenceError("matched OSC response metadata drifted")
        payload = response.get_json(silent=True)
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise PerfEvidenceError("matched OSC response is not a successful JSON object")
        if str(row.get("method")) == "POST":
            projection = {
                "http_status": response.status_code,
                "ok": payload.get("ok"),
                "id": payload.get("id"),
                "case_number": payload.get("case_number"),
                "mode": payload.get("mode"),
            }
            if projection != _expected_projection(row, workload):
                raise PerfEvidenceError("matched OSC POST semantic projection drifted")
            return _canonical_json(projection) + b"\n"
        items = payload.get("items")
        if not isinstance(items, list):
            raise PerfEvidenceError("matched OSC response items are not a list")
        projection = {
            "http_status": response.status_code,
            "ok": payload.get("ok"),
            "item_count": len(items),
            "items_sha256": _sha256_bytes(
                _canonical_json([_project_osc_item(item) for item in items])
            ),
        }
        if projection != _expected_projection(row, workload):
            raise PerfEvidenceError("matched OSC semantic projection drifted")
        return _canonical_json(projection) + b"\n"
    if workload == "native_gateway_livez":
        if response.status_code != 200 or response.headers.get("Content-Type", "") != "application/json":
            raise PerfEvidenceError("matched native /livez response metadata drifted")
        payload = response.get_json(silent=True)
        if not isinstance(payload, dict):
            raise PerfEvidenceError("matched native /livez response is not a JSON object")
        if mode == "v2_actual_livez_wsgi":
            live = payload.get("ok") is True and payload.get("readiness_checked") is False
            status = payload.get("status")
        elif mode == "v3_native_gateway_livez_wsgi":
            components = payload.get("components")
            live = (
                payload.get("ready") is True
                and payload.get("scope") == "gateway_process"
                and isinstance(components, dict)
                and components.get("process") == "ok"
                and components.get("model_probe_performed") is False
            )
            status = payload.get("status")
        else:
            raise PerfEvidenceError("native workload received an invalid mode")
        projection = {
            "http_status": response.status_code,
            "live": live,
            "probe_kind": "process_liveness",
            "status": status,
        }
        if projection != _expected_projection(row, workload):
            raise PerfEvidenceError("matched native /livez semantic contract drifted")
        return _canonical_json(projection) + b"\n"
    if workload == "actual_v2_livez":
        if response.status_code != 200 or response.headers.get("Content-Type", "") != "application/json":
            raise PerfEvidenceError("actual /livez response metadata drifted")
        payload = response.get_json(silent=True)
        expected_keys = {
            "ok",
            "status",
            "probe",
            "readiness_checked",
            "timestamp",
            "uptime_seconds",
        }
        if not isinstance(payload, dict) or set(payload) != expected_keys:
            raise PerfEvidenceError("actual /livez response keys drifted")
        timestamp = payload.get("timestamp")
        uptime = payload.get("uptime_seconds")
        projection = {
            "ok": payload.get("ok"),
            "probe": payload.get("probe"),
            "readiness_checked": payload.get("readiness_checked"),
            "status": payload.get("status"),
            "timestamp_numeric": not isinstance(timestamp, bool) and isinstance(timestamp, (int, float)),
            "uptime_seconds_numeric": not isinstance(uptime, bool)
            and isinstance(uptime, (int, float))
            and uptime >= 0,
        }
        if projection != _expected_projection(row, workload):
            raise PerfEvidenceError("actual /livez response semantics drifted")
        return _canonical_json(projection) + b"\n"
    body = response.get_data()
    expected = _expected_body(row)
    content_type = response.headers.get("Content-Type", "")
    fixture_header = response.headers.get("X-MAGI-Perf-Fixture", "")
    if response.status_code != 200:
        raise PerfEvidenceError(f"unexpected response status: {response.status_code}")
    if content_type != "application/json":
        raise PerfEvidenceError(f"unexpected response content type: {content_type!r}")
    if fixture_header != FIXTURE_VERSION:
        raise PerfEvidenceError(f"fixture header drifted: {fixture_header!r}")
    if body != expected:
        raise PerfEvidenceError(
            f"response body drifted: expected={_sha256_bytes(expected)} observed={_sha256_bytes(body)}"
        )
    projection = {
        "body_sha256": _sha256_bytes(body),
        "content_length": int(response.headers["Content-Length"]),
        "content_type": content_type,
        "fixture_header": fixture_header,
        "status_code": response.status_code,
    }
    return _canonical_json(projection) + b"\n"


def _current_rss_bytes() -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "rss=", "-p", str(os.getpid())],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return int(result.stdout.strip()) * 1024, "ps_rss"
    except (OSError, ValueError, subprocess.SubprocessError):
        maximum = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        multiplier = 1 if sys.platform == "darwin" else 1024
        return maximum * multiplier, "resource_maxrss_fallback"


def _fd_count() -> int:
    try:
        return len(os.listdir("/dev/fd"))
    except OSError as exc:
        raise PerfEvidenceError(f"file-descriptor sampling failed: {exc}") from exc


@contextmanager
def _network_blocked() -> Iterator[None]:
    def blocked(*_args: Any, **_kwargs: Any) -> None:
        raise PerfEvidenceError("offline benchmark attempted a network connection")

    with patch.object(socket, "create_connection", side_effect=blocked), patch.object(
        socket.socket, "connect", side_effect=blocked
    ):
        yield


def _request(client: Client, row: Mapping[str, Any]) -> Any:
    kwargs: dict[str, Any] = {
        "path": str(row["path"]),
        "method": str(row["method"]),
        "headers": dict(row["headers"]),
    }
    if row.get("body") is not None:
        kwargs["json"] = row["body"]
    return client.open(**kwargs)


def _percentile(values: Sequence[int], percentile: float) -> float:
    if not values:
        raise PerfEvidenceError("latency sample is empty")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _runtime_identity() -> dict[str, str]:
    executable = Path(sys.executable).resolve()
    return {
        "implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "executable": str(executable),
        "executable_sha256": _sha256_file(executable),
    }


def _workload_source_sha256(workload: str) -> str:
    if workload == "fixture_livez":
        source = inspect.getsource(build_offline_app)
    elif workload == "actual_v2_livez":
        from api.blueprints.admin_runtime import create_admin_runtime_blueprint

        source = inspect.getsource(create_admin_runtime_blueprint)
    elif workload == "native_gateway_livez":
        source = inspect.getsource(_request_plan) + inspect.getsource(_expected_projection)
    elif workload == "synthetic_osc_cases":
        source = "\n".join(
            (
                inspect.getsource(build_actual_osc_cases_app),
                inspect.getsource(build_native_osc_cases_app),
                inspect.getsource(SyntheticOscDatabase),
                inspect.getsource(_project_osc_item),
            )
        )
    else:
        raise PerfEvidenceError(f"unsupported benchmark workload: {workload!r}")
    return _sha256_bytes(source.encode("utf-8"))


def measure_mode(
    mode: str,
    *,
    warmup: int,
    iterations: int,
    workload: str = "fixture_livez",
) -> dict[str, Any]:
    """Measure one arm in the current isolated process."""

    _validate_bounds(warmup=warmup, iterations=iterations, repeats=1)
    plan = _request_plan(workload)
    forbidden_service_modules = {"api.server", "api.tools_api"}
    preexisting_forbidden_imports = forbidden_service_modules & set(sys.modules)

    cold_start_started = time.perf_counter_ns()
    with _network_blocked():
        construction_started = time.perf_counter_ns()
        client = _make_client(mode, workload)
        construction_latency_ns = time.perf_counter_ns() - construction_started
        handler_identity = _handler_identity(mode, workload)
        cold_row = plan[0]
        cold_started = time.perf_counter_ns()
        cold_response = _request(client, cold_row)
        cold_latency_ns = time.perf_counter_ns() - cold_started
        cold_signature = _response_signature(cold_response, cold_row, workload, mode)
        cold_response.close()
        cold_start_latency_ns = time.perf_counter_ns() - cold_start_started
        for index in range(warmup):
            row = plan[index % len(plan)]
            response = _request(client, row)
            _response_signature(response, row, workload, mode)
            response.close()

    gc.collect()
    rss_before, rss_source_before = _current_rss_bytes()
    fd_before = _fd_count()
    objects_before = len(gc.get_objects())
    tracemalloc.start()
    traced_before, _ = tracemalloc.get_traced_memory()
    latencies_ns = array("Q")
    observed_digest = hashlib.sha256()
    expected_digest = hashlib.sha256()

    with _network_blocked():
        for index in range(iterations):
            row = plan[index % len(plan)]
            started = time.perf_counter_ns()
            response = _request(client, row)
            signature = _response_signature(response, row, workload, mode)
            elapsed = time.perf_counter_ns() - started
            response.close()
            latencies_ns.append(elapsed)
            observed_digest.update(signature)
            expected_projection = _expected_projection(row, workload)
            expected_digest.update(_canonical_json(expected_projection) + b"\n")

    gc.collect()
    traced_after, traced_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    objects_after = len(gc.get_objects())
    rss_after, rss_source_after = _current_rss_bytes()
    fd_after = _fd_count()
    if rss_source_before != rss_source_after:
        raise PerfEvidenceError("RSS sampling source changed within one benchmark arm")

    observed_sha = observed_digest.hexdigest()
    expected_sha = expected_digest.hexdigest()
    if observed_sha != expected_sha:
        raise PerfEvidenceError("response-sequence digest does not match the independent expectation")
    forbidden_imports = sorted(
        (forbidden_service_modules & set(sys.modules)) - preexisting_forbidden_imports
    )
    if forbidden_imports:
        raise PerfEvidenceError(f"offline worker imported production services: {forbidden_imports}")
    opposite_handler_module_imported = False
    side_effect_transcript: dict[str, Any] | None = None
    if workload == "synthetic_osc_cases":
        opposite_module = (
            "magi_v3.osc_cases"
            if mode == "v2_actual_osc_cases_wsgi"
            else "api.blueprints.osc_cases"
        )
        opposite_handler_module_imported = opposite_module in sys.modules
        if opposite_handler_module_imported and os.environ.get("MAGI_PERF_ISOLATED_WORKER") == "1":
            raise PerfEvidenceError(
                f"synthetic OSC arm imported the opposite handler module: {opposite_module}"
            )
        application = getattr(client, "application", None)
        evidence_factory = getattr(application, "side_effect_evidence", None)
        if not callable(evidence_factory):
            raise PerfEvidenceError("synthetic OSC arm did not expose side-effect evidence")
        side_effect_transcript = evidence_factory()
        event_counts = side_effect_transcript["transaction_event_counts"]
        if side_effect_transcript["balanced_transactions"] is not True:
            raise PerfEvidenceError("synthetic OSC transaction transcript is unbalanced")
        if event_counts["update"] < 1 or event_counts["update"] != event_counts["commit"]:
            raise PerfEvidenceError("synthetic OSC POST did not commit one bounded update per mutation")

    samples = list(latencies_ns)
    latency_digest = hashlib.sha256(latencies_ns.tobytes()).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "workload": workload,
        "scope": (
            "actual_v2_vs_native_v3_gateway_liveness"
            if workload == "native_gateway_livez"
            else (
                "actual_v2_vs_native_v3_synthetic_osc_case_list_and_upsert"
                if workload == "synthetic_osc_cases"
                else (
                    "actual_production_handler_compatibility_overhead_only"
                    if workload == "actual_v2_livez"
                    else "compatibility_wrapper_overhead_only"
                )
            )
        ),
        "runtime": _runtime_identity(),
        "process_identity": {"pid": os.getpid(), "parent_pid": os.getppid()},
        "script_sha256": _sha256_file(SCRIPT_PATH),
        "workload_source_sha256": _workload_source_sha256(workload),
        "handler_identity": handler_identity,
        "inventory_fingerprint": EXPECTED_FINGERPRINT,
        "pinned_route": {
            "service": _route_for_workload(workload).service,
            "rule": _route_for_workload(workload).rule,
            "methods": list(_route_for_workload(workload).methods),
            "endpoint": _route_for_workload(workload).endpoint,
        },
        "request_plan_sha256": _request_plan_sha256(workload),
        "request_case_count": len(plan),
        "warmup_requests": warmup,
        "measured_requests": iterations,
        "cold_request": {
            "latency_us": round(cold_latency_ns / 1_000, 3),
            "response_signature_sha256": _sha256_bytes(cold_signature),
        },
        "cold_start": {
            "definition": "client_construction_plus_handler_identity_plus_first_request",
            "latency_us": round(cold_start_latency_ns / 1_000, 3),
            "client_construction_latency_us": round(construction_latency_ns / 1_000, 3),
        },
        "response_sequence_sha256": observed_sha,
        "expected_response_sequence_sha256": expected_sha,
        "correctness_passed": True,
        "latency": {
            "unit": "microseconds",
            "mean": round(statistics.fmean(samples) / 1_000, 3),
            "p50": round(_percentile(samples, 0.50) / 1_000, 3),
            "p95": round(_percentile(samples, 0.95) / 1_000, 3),
            "p99": round(_percentile(samples, 0.99) / 1_000, 3),
            "min": round(min(samples) / 1_000, 3),
            "max": round(max(samples) / 1_000, 3),
            "sample_count": len(samples),
            "sample_bytes_sha256": latency_digest,
        },
        "memory": {
            "rss_source": rss_source_before,
            "rss_before_bytes": rss_before,
            "rss_after_bytes": rss_after,
            "rss_growth_bytes": rss_after - rss_before,
            "gc_objects_before": objects_before,
            "gc_objects_after": objects_after,
            "gc_object_growth": objects_after - objects_before,
            "tracemalloc_current_growth_bytes": traced_after - traced_before,
            "tracemalloc_peak_bytes": traced_peak,
            "measurement_note": "growth includes the benchmark client and compact latency array",
        },
        "file_descriptors": {
            "source": "/dev/fd",
            "before": fd_before,
            "after": fd_after,
            "drift": fd_after - fd_before,
        },
        "safety": {
            "listener_started": False,
            "production_service_imported": False,
            "production_handler_module_imported": workload in {
                "actual_v2_livez",
                "native_gateway_livez",
            }
            or (workload == "synthetic_osc_cases" and mode == "v2_actual_osc_cases_wsgi"),
            "network_connections_blocked": True,
            "live_state_accessed": False,
            "external_writes": False,
            "production_state_writes": False,
            "production_port_accessed": False,
            "nas_accessed": False,
            "launchctl_invoked": False,
        },
        **(
            {
                "synthetic_corpus": {
                    "database": "sqlite_memory_disposable",
                    "row_count": len(SYNTHETIC_OSC_ROWS),
                    "corpus_sha256": _sha256_bytes(_canonical_json(SYNTHETIC_OSC_ROWS)),
                    "read_only": False,
                    "disposable": True,
                    "measured_methods": ["GET", "POST"],
                    "unmeasured_methods": [],
                    "opposite_handler_module_imported": opposite_handler_module_imported,
                    "side_effect_transcript": side_effect_transcript,
                }
            }
            if workload == "synthetic_osc_cases"
            else {}
        ),
    }


def _validate_bounds(*, warmup: int, iterations: int, repeats: int) -> None:
    if not 0 <= warmup <= 100_000:
        raise PerfEvidenceError("warmup must be between 0 and 100000")
    if not 1 <= iterations <= 1_000_000:
        raise PerfEvidenceError("iterations must be between 1 and 1000000")
    if not 1 <= repeats <= 20:
        raise PerfEvidenceError("repeats must be between 1 and 20")


def _median_metric(runs: Sequence[Mapping[str, Any]], section: str, metric: str) -> float:
    return float(statistics.median(float(row[section][metric]) for row in runs))


def validate_and_compare(
    mode_runs: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    inventory_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    """Reject drift first, then calculate descriptive deltas without a gate."""

    workloads = {
        row.get("workload")
        for rows in mode_runs.values()
        for row in rows
        if isinstance(row, Mapping)
    }
    if len(workloads) != 1:
        raise PerfEvidenceError("matched modes do not identify one workload")
    workload = next(iter(workloads))
    if workload not in WORKLOADS:
        raise PerfEvidenceError("worker workload identity is invalid")
    modes = _modes_for_workload(str(workload))
    if set(mode_runs) != set(modes) or any(not mode_runs[mode] for mode in modes):
        raise PerfEvidenceError("both matched modes require at least one isolated run")
    flattened = [row for mode in modes for row in mode_runs[mode]]
    repeats = {len(mode_runs[mode]) for mode in modes}
    if len(repeats) != 1:
        raise PerfEvidenceError("matched modes have different repeat counts")
    required_equal = (
        "script_sha256",
        "workload",
        "workload_source_sha256",
        "inventory_fingerprint",
        "pinned_route",
        "request_plan_sha256",
        "request_case_count",
        "warmup_requests",
        "measured_requests",
        "response_sequence_sha256",
        "expected_response_sequence_sha256",
    )
    for key in required_equal:
        if len({json.dumps(row[key], sort_keys=True) for row in flattened}) != 1:
            raise PerfEvidenceError(f"matched comparison rejected because {key} differs")
    for mode in modes:
        if any(row["mode"] != mode for row in mode_runs[mode]):
            raise PerfEvidenceError(f"worker report was assigned to the wrong mode: {mode}")
    matched_route = _route_for_workload(str(workload))
    expected_route = {
        "service": matched_route.service,
        "rule": matched_route.rule,
        "methods": list(matched_route.methods),
        "endpoint": matched_route.endpoint,
    }
    if any(row["pinned_route"] != expected_route for row in flattened):
        raise PerfEvidenceError("worker pinned-route identity does not match the benchmark contract")
    if any(row.get("workload") not in WORKLOADS for row in flattened):
        raise PerfEvidenceError("worker workload identity is invalid")
    if any(not row.get("correctness_passed") for row in flattened):
        raise PerfEvidenceError("matched comparison rejected because correctness failed")
    if any(row["response_sequence_sha256"] != row["expected_response_sequence_sha256"] for row in flattened):
        raise PerfEvidenceError("matched comparison rejected because a response digest is unexpected")
    runtime_digests = {row["runtime"]["executable_sha256"] for row in flattened}
    if len(runtime_digests) != 1:
        raise PerfEvidenceError("matched comparison rejected because runtimes differ")
    if inventory_evidence.get("fingerprint") != EXPECTED_FINGERPRINT:
        raise PerfEvidenceError("validated runtime inventory fingerprint is not pinned")
    if any(row["inventory_fingerprint"] != inventory_evidence["fingerprint"] for row in flattened):
        raise PerfEvidenceError("worker inventory fingerprint differs from orchestrator preflight")
    if workload == "synthetic_osc_cases":
        transcripts = {
            json.dumps(
                row["synthetic_corpus"]["side_effect_transcript"],
                ensure_ascii=False,
                sort_keys=True,
            )
            for row in flattened
        }
        if len(transcripts) != 1:
            raise PerfEvidenceError(
                "matched comparison rejected because POST side-effect transcripts differ"
            )

    direct = mode_runs[modes[0]]
    compat = mode_runs[modes[1]]
    direct_p50 = _median_metric(direct, "latency", "p50")
    compat_p50 = _median_metric(compat, "latency", "p50")
    direct_p95 = _median_metric(direct, "latency", "p95")
    compat_p95 = _median_metric(compat, "latency", "p95")
    direct_p99 = _median_metric(direct, "latency", "p99")
    compat_p99 = _median_metric(compat, "latency", "p99")
    direct_cold = _median_metric(direct, "cold_request", "latency_us")
    compat_cold = _median_metric(compat, "cold_request", "latency_us")
    direct_cold_start = _median_metric(direct, "cold_start", "latency_us")
    compat_cold_start = _median_metric(compat, "cold_start", "latency_us")
    comparison = {
        "baseline_mode": modes[0],
        "candidate_mode": modes[1],
        "latency_p50_direct_us": round(direct_p50, 3),
        "latency_p50_compat_us": round(compat_p50, 3),
        "latency_p50_delta_us": round(compat_p50 - direct_p50, 3),
        "latency_p50_ratio": round(compat_p50 / direct_p50, 6),
        "latency_p95_direct_us": round(direct_p95, 3),
        "latency_p95_compat_us": round(compat_p95, 3),
        "latency_p95_delta_us": round(compat_p95 - direct_p95, 3),
        "latency_p95_ratio": round(compat_p95 / direct_p95, 6),
        "latency_p99_direct_us": round(direct_p99, 3),
        "latency_p99_compat_us": round(compat_p99, 3),
        "latency_p99_delta_us": round(compat_p99 - direct_p99, 3),
        "latency_p99_ratio": round(compat_p99 / direct_p99, 6),
        "cold_latency_direct_us": round(direct_cold, 3),
        "cold_latency_compat_us": round(compat_cold, 3),
        "cold_latency_delta_us": round(compat_cold - direct_cold, 3),
        "cold_start_direct_us": round(direct_cold_start, 3),
        "cold_start_compat_us": round(compat_cold_start, 3),
        "cold_start_delta_us": round(compat_cold_start - direct_cold_start, 3),
        "rss_growth_direct_bytes": _median_metric(direct, "memory", "rss_growth_bytes"),
        "rss_growth_compat_bytes": _median_metric(compat, "memory", "rss_growth_bytes"),
        "gc_object_growth_direct": _median_metric(direct, "memory", "gc_object_growth"),
        "gc_object_growth_compat": _median_metric(compat, "memory", "gc_object_growth"),
        "tracemalloc_peak_direct_bytes": _median_metric(direct, "memory", "tracemalloc_peak_bytes"),
        "tracemalloc_peak_compat_bytes": _median_metric(compat, "memory", "tracemalloc_peak_bytes"),
        "fd_drift_direct": _median_metric(direct, "file_descriptors", "drift"),
        "fd_drift_compat": _median_metric(compat, "file_descriptors", "drift"),
    }
    if workload in {"native_gateway_livez", "synthetic_osc_cases"}:
        comparison.update(
            {
                "latency_p50_v2_us": comparison["latency_p50_direct_us"],
                "latency_p50_native_v3_us": comparison["latency_p50_compat_us"],
                "latency_p95_v2_us": comparison["latency_p95_direct_us"],
                "latency_p95_native_v3_us": comparison["latency_p95_compat_us"],
                "latency_p99_v2_us": comparison["latency_p99_direct_us"],
                "latency_p99_native_v3_us": comparison["latency_p99_compat_us"],
                "rss_growth_v2_bytes": comparison["rss_growth_direct_bytes"],
                "rss_growth_native_v3_bytes": comparison["rss_growth_compat_bytes"],
                "fd_drift_v2": comparison["fd_drift_direct"],
                "fd_drift_native_v3": comparison["fd_drift_compat"],
                "cold_start_v2_us": comparison["cold_start_direct_us"],
                "cold_start_native_v3_us": comparison["cold_start_compat_us"],
            }
        )
    return {
        "comparison_valid": True,
        "workload_equivalent": True,
        "responses_correct": True,
        "same_python_runtime": True,
        "same_pinned_route_identity": True,
        "same_request_plan": True,
        "same_semantic_response_contract": True,
        "arm_modes": list(modes),
        "comparison": comparison,
    }


def _run_child(
    mode: str,
    *,
    warmup: int,
    iterations: int,
    workload: str,
) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.update(
        {
            "MAGI_DISABLE_SERVER_STARTUP_HOOKS": "1",
            "MAGI_SKIP_IMPORT_PROBES": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "MAGI_PERF_ISOLATED_WORKER": "1",
        }
    )
    command = [
        sys.executable,
        str(SCRIPT_PATH),
        "--worker-mode",
        mode,
        "--warmup",
        str(warmup),
        "--iterations",
        str(iterations),
        "--workload",
        workload,
    ]
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=max(60, min(600, iterations // 500 + 60)),
    )
    if result.returncode != 0:
        raise PerfEvidenceError(
            f"isolated {mode} worker failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PerfEvidenceError(f"isolated {mode} worker emitted invalid JSON") from exc
    if not isinstance(payload, dict):
        raise PerfEvidenceError(f"isolated {mode} worker did not emit an object")
    process_identity = payload.get("process_identity")
    if (
        not isinstance(process_identity, dict)
        or type(process_identity.get("pid")) is not int
        or process_identity["pid"] <= 0
        or process_identity.get("parent_pid") != os.getpid()
    ):
        raise PerfEvidenceError(f"isolated {mode} worker process identity is invalid")
    return payload


def _gateway_threshold_evaluation(
    comparison: Mapping[str, Any], *, native: bool = False
) -> dict[str, Any]:
    policy_path = REPO_ROOT / "config" / "v3_resource_policy.json"
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        slo = policy["performance_slo"]
        thresholds = {
            "gateway_livez_p95_us": float(slo["gateway_livez_p95_ms"]) * 1_000,
            "gateway_added_overhead_p95_us": float(slo["gateway_added_overhead_p95_ms"])
            * 1_000,
            "gateway_added_overhead_p99_us": float(slo["gateway_added_overhead_p99_ms"])
            * 1_000,
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise PerfEvidenceError(f"gateway performance thresholds are invalid: {exc}") from exc
    observed = {
        "gateway_livez_p95_us": float(comparison["latency_p95_compat_us"]),
        "gateway_added_overhead_p95_us": max(0.0, float(comparison["latency_p95_delta_us"])),
        "gateway_added_overhead_p99_us": max(0.0, float(comparison["latency_p99_delta_us"])),
    }
    checks = {
        key: {
            "observed_us": round(observed[key], 3),
            "maximum_us": round(maximum, 3),
            "passed": observed[key] <= maximum,
        }
        for key, maximum in thresholds.items()
    }
    return {
        "scope": (
            "actual_v2_livez_vs_native_v3_gateway_livez"
            if native
            else "actual_v2_livez_handler_through_v3_compatibility_wrapper"
        ),
        "aggregation": "median_of_three_isolated_run_percentiles",
        "policy_sha256": _sha256_file(policy_path),
        "checks": checks,
        "passed": all(row["passed"] for row in checks.values()),
        "covers_native_v3_gateway_probe": native,
        "does_not_cover_business_workloads": True,
    }


def _business_architecture_gap(
    inventory: RouteInventory, *, measured: bool
) -> dict[str, Any]:
    """Bind the synthetic measurement to remaining production composition gaps."""

    if BUSINESS_ROUTE not in inventory.routes:
        raise PerfEvidenceError("representative V2 business route is absent from pinned inventory")
    manifest_path = REPO_ROOT / "config" / "v3_service_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    factories = {
        row["id"]: row.get("factory")
        for row in manifest.get("services", [])
        if isinstance(row, dict) and row.get("id") in {"main_http", "tools_http"}
    }
    expected_factories = {
        "main_http": "magi_v3.compat:create_main_app",
        "tools_http": "magi_v3.compat:create_tools_app",
    }
    if factories != expected_factories:
        raise PerfEvidenceError("V3 gateway factory architecture changed; re-audit native business routes")
    corpus = [dict(row) for row in SYNTHETIC_BUSINESS_REQUESTS]
    native_source = "\n".join(
        (
            inspect.getsource(build_native_osc_cases_app),
            inspect.getsource(SyntheticOscDatabase),
        )
    )
    return {
        "schema_version": 1,
        "name": "synthetic_osc_case_list_and_create",
        "synthetic_only": True,
        "production_state_accessed": False,
        "request_count": len(corpus),
        "request_plan_sha256": _sha256_bytes(_canonical_json(corpus)),
        "requests": corpus,
        "v2_handler": {
            "route": {
                "service": BUSINESS_ROUTE.service,
                "rule": BUSINESS_ROUTE.rule,
                "methods": list(BUSINESS_ROUTE.methods),
                "endpoint": BUSINESS_ROUTE.endpoint,
            },
            "present_in_pinned_inventory": True,
        },
        "database_corpus": {
            "backend": "sqlite_memory",
            "row_count": len(SYNTHETIC_OSC_ROWS),
            "sha256": _sha256_bytes(_canonical_json(SYNTHETIC_OSC_ROWS)),
            "production_state_accessed": False,
        },
        "v3_native_handler": {
            "callable": "magi_v3.osc_cases.OscCasesApplication",
            "source_sha256": _sha256_bytes(native_source.encode("utf-8")),
            "composed_in_service_manifest": False,
        },
        "matched_measurement_status": (
            "matched_synthetic_get_and_post_measured"
            if measured
            else "available_as_synthetic_osc_cases_workload_not_measured_in_this_run"
        ),
        "measured_methods": ["GET", "POST"] if measured else [],
        "unmeasured_methods": [],
        "architecture_gap": {
            "code": "NATIVE_OSC_CASES_NOT_COMPOSED_IN_SERVICE_MANIFEST",
            "gateway_application_factories": factories,
            "factory_kind": "v2_compatibility",
            "manifest_sha256": _sha256_file(manifest_path),
            "reason": (
                "A native /api/osc/cases callable now exists and GET plus a bounded idempotent POST "
                "upsert can be compared on a disposable synthetic database, but main_http still "
                "declares magi_v3.compat and real MariaDB/session/NAS, folder/archive behavior, plus "
                "production-state measurements remain unimplemented."
            ),
        },
    }


def _collect_workload_children(
    *,
    workload: str,
    warmup: int,
    iterations: int,
    repeats: int,
    inventory_evidence: Mapping[str, Any],
    ordinal_offset: int = 0,
) -> dict[str, Any]:
    modes = _modes_for_workload(workload)
    mode_runs: dict[str, list[dict[str, Any]]] = {mode: [] for mode in modes}
    execution_order: list[str] = []
    children: list[dict[str, Any]] = []
    for repeat in range(repeats):
        order = modes if repeat % 2 == 0 else tuple(reversed(modes))
        for mode in order:
            execution_order.append(mode)
            child = _run_child(mode, warmup=warmup, iterations=iterations, workload=workload)
            mode_runs[mode].append(child)
            children.append(
                {
                    "ordinal": ordinal_offset + len(children),
                    "mode": mode,
                    "workload": workload,
                    "pid": child["process_identity"]["pid"],
                    "parent_pid": child["process_identity"]["parent_pid"],
                }
            )
    validation = validate_and_compare(mode_runs, inventory_evidence=inventory_evidence)
    return {
        "workload": workload,
        "modes": modes,
        "runs": mode_runs,
        "execution_order": execution_order,
        "children": children,
        "validation": validation,
    }


def _synthetic_business_evidence(
    collection: Mapping[str, Any],
    *,
    warmup: int,
    iterations: int,
    repeats: int,
) -> dict[str, Any]:
    modes = tuple(collection["modes"])
    runs = collection["runs"]
    validation = collection["validation"]
    runtime_hashes = {
        row["runtime"]["executable_sha256"]
        for mode in modes
        for row in runs[mode]
    }
    if len(runtime_hashes) != 1:
        raise PerfEvidenceError("synthetic OSC arms did not use one Python runtime")
    return {
        "schema_version": 1,
        "workload": "synthetic_osc_cases",
        "synthetic_only": True,
        "production_business_workload": False,
        "release_thresholds_applied": False,
        "parameters": {"warmup": warmup, "iterations": iterations, "repeats": repeats},
        "execution_order": list(collection["execution_order"]),
        "sequential_process_proof": {
            "maximum_simultaneous_benchmark_children": 1,
            "blocking_subprocess_run_used": True,
            "children": list(collection["children"]),
        },
        "route": {
            "service": BUSINESS_ROUTE.service,
            "rule": BUSINESS_ROUTE.rule,
            "methods": list(BUSINESS_ROUTE.methods),
            "endpoint": BUSINESS_ROUTE.endpoint,
        },
        "measured_methods": ["GET", "POST"],
        "unmeasured_methods": [],
        "database_corpus": {
            "backend": "sqlite_memory",
            "row_count": len(SYNTHETIC_OSC_ROWS),
            "sha256": _sha256_bytes(_canonical_json(SYNTHETIC_OSC_ROWS)),
        },
        "isolation_contract": {
            "actual_v2_blueprint_view_executed": True,
            "actual_native_wsgi_and_service_executed": True,
            "v2_database_override": "_osc_exec_bounded_disposable_in_memory_sqlite",
            "v2_manual_schema_guard": "pre_satisfied_no_ddl",
            "v2_path_mapper": "identity_for_get_and_nas_resolution_forbidden",
            "v2_settings_lookup": "forbidden_rows_have_explicit_lawyer",
            "authentication": "flask_login_disabled_and_native_authorizer_true_synthetic_only",
            "network": "socket_connect_blocked",
        },
        "side_effect_transcript": {
            mode: runs[mode][0]["synthetic_corpus"]["side_effect_transcript"]
            for mode in modes
        },
        "handler_identities": {
            mode: runs[mode][0]["handler_identity"] for mode in modes
        },
        "same_python_runtime": True,
        "same_request_plan": validation["same_request_plan"],
        "same_route_identity": validation["same_pinned_route_identity"],
        "response_projection_equivalent": validation["responses_correct"],
        "runs": runs,
        "comparison": validation["comparison"],
        "limitations": [
            "disposable in-memory SQLite replaces production MariaDB in both isolated arms",
            "POST is limited to an idempotent upsert with auto_create_folder=false",
            "folder creation and archive behavior remain unmeasured",
            "no production session, listener, service lifecycle, network, LIVE state, or NAS access",
        ],
    }


def run_benchmark(
    *,
    warmup: int = 100,
    iterations: int = 1_000,
    repeats: int = 3,
    workload: str = DEFAULT_WORKLOAD,
) -> dict[str, Any]:
    _validate_bounds(warmup=warmup, iterations=iterations, repeats=repeats)
    if workload not in WORKLOADS:
        raise PerfEvidenceError(f"unsupported benchmark workload: {workload!r}")
    inventory_evidence = load_and_validate_runtime_inventory()
    inventory = RouteInventory.load()
    if _route_for_workload(workload) not in inventory.routes:
        raise PerfEvidenceError("pinned benchmark route is absent from the V2 runtime inventory")

    primary = _collect_workload_children(
        workload=workload,
        warmup=warmup,
        iterations=iterations,
        repeats=repeats,
        inventory_evidence=inventory_evidence,
    )
    modes = tuple(primary["modes"])
    mode_runs = primary["runs"]
    validation = primary["validation"]
    execution_order = list(primary["execution_order"])
    sequential_children = list(primary["children"])
    business_collection = primary if workload == "synthetic_osc_cases" else None
    if workload == "native_gateway_livez":
        business_collection = _collect_workload_children(
            workload="synthetic_osc_cases",
            warmup=warmup,
            iterations=iterations,
            repeats=repeats,
            inventory_evidence=inventory_evidence,
            ordinal_offset=len(sequential_children),
        )
        execution_order.extend(business_collection["execution_order"])
        sequential_children.extend(business_collection["children"])
    gateway_thresholds = (
        _gateway_threshold_evaluation(
            validation["comparison"], native=workload == "native_gateway_livez"
        )
        if workload in {"actual_v2_livez", "native_gateway_livez"}
        else {
            "scope": "not_applied_to_fixture",
            "passed": False,
            "does_not_cover_native_v3_or_business_workloads": True,
        }
    )
    if workload in {"actual_v2_livez", "native_gateway_livez"} and gateway_thresholds["passed"] is not True:
        raise PerfEvidenceError("actual-handler gateway performance thresholds failed")
    business_measured = business_collection is not None
    business_gap = _business_architecture_gap(inventory, measured=business_measured)
    synthetic_business = (
        _synthetic_business_evidence(
            business_collection,
            warmup=warmup,
            iterations=iterations,
            repeats=repeats,
        )
        if business_collection is not None
        else None
    )
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": (
            "sequential_actual_v2_livez_vs_native_v3_gateway_livez"
            if workload == "native_gateway_livez"
            else (
                "sequential_actual_v2_vs_native_v3_synthetic_osc_cases_get_and_post"
                if workload == "synthetic_osc_cases"
                else (
                    "sequential_actual_v2_livez_direct_vs_v3_compat_wsgi"
                    if workload == "actual_v2_livez"
                    else "matched_v2_direct_vs_v3_compat_wsgi"
                )
            )
        ),
        "workload": workload,
        "scope": (
            "actual_v2_vs_native_v3_gateway_liveness"
            if workload == "native_gateway_livez"
            else (
                "matched_synthetic_business_handler_get_and_post"
                if workload == "synthetic_osc_cases"
                else (
                    "actual_production_handler_compatibility_overhead_only"
                    if workload == "actual_v2_livez"
                    else "compatibility_wrapper_overhead_only"
                )
            )
        ),
        "offline": True,
        "parameters": {"warmup": warmup, "iterations": iterations, "repeats": repeats},
        "execution_order": execution_order,
        "sequential_process_proof": {
            "maximum_simultaneous_benchmark_children": 1,
            "blocking_subprocess_run_used": True,
            "children": sequential_children,
        },
        "inventory": {
            "counts": inventory.counts,
            "fingerprint": inventory_evidence["fingerprint"],
            "pinned_route_present": True,
        },
        "equivalence_proof": {
            "direct_and_compat_use_same_handler_source": workload
            in {"fixture_livez", "actual_v2_livez"},
            "distinct_actual_handler_sources_expected": workload
            in {"native_gateway_livez", "synthetic_osc_cases"},
            "handler_identities": {
                mode: mode_runs[mode][0]["handler_identity"] for mode in modes
            },
            "workload_source_sha256": mode_runs[modes[0]][0]["workload_source_sha256"],
            "direct_and_compat_use_same_python_executable_sha256": mode_runs[modes[0]][0]["runtime"][
                "executable_sha256"
            ],
            "request_plan_sha256": mode_runs[modes[0]][0]["request_plan_sha256"],
            "response_sequence_sha256": mode_runs[modes[0]][0]["response_sequence_sha256"],
            **{key: validation[key] for key in validation if key != "comparison"},
        },
        "runs": mode_runs,
        "comparison": validation["comparison"],
        "gateway_threshold_evaluation": gateway_thresholds,
        "representative_business_corpus": business_gap,
        "synthetic_business_benchmark": synthetic_business,
        "claim_coverage": {
            "same_python_executable": True,
            "same_host_orchestrator": True,
            "same_request_corpus": True,
            "isolated_process_per_arm": True,
            "warm_latency_p50_p95_p99": True,
            "cold_first_request_latency": True,
            "production_v2_handler": workload
            in {"actual_v2_livez", "native_gateway_livez", "synthetic_osc_cases"},
            "native_v3_handler": workload in {"native_gateway_livez", "synthetic_osc_cases"},
            "native_v3_gateway_probe": workload == "native_gateway_livez",
            "production_business_workload": False,
            "representative_synthetic_business_corpus_defined": True,
            "matched_native_business_handler": business_measured,
            "synthetic_business_workload": business_measured,
            "synthetic_business_get_measured": business_measured,
            "synthetic_business_post_measured": business_measured,
            "rss_evidence": True,
            "file_descriptor_evidence": True,
            "production_machine_state_control": False,
            "same_host_sequential_not_concurrent": True,
            "release_gateway_thresholds_applied": workload in {"actual_v2_livez", "native_gateway_livez"},
        },
        "gate": {
            "blocker_code": BLOCKER_CODE,
            "eligible_to_clear_full_v2_v3_performance_blocker": False,
            "decision": "blocker_retained",
            "reason": (
                "This evidence compares the actual V2 and native V3 /api/osc/cases GET handlers plus "
                "one bounded idempotent POST upsert on identical disposable in-memory SQLite databases "
                "in isolated processes, but it is not a production business workload, does not measure "
                "real MariaDB/session/NAS or folder/archive behavior, and main_http remains compat."
                if workload == "synthetic_osc_cases"
                else (
                    "This evidence measures the actual V2 and native V3 gateway liveness handlers, but not "
                    "a production business workload."
                    if workload == "native_gateway_livez"
                    else (
                        "This evidence sequentially measures the actual production V2 /livez handler directly and through "
                        "the V3 compatibility wrapper; it does not provide a native V3 handler or production business workload."
                        if workload == "actual_v2_livez"
                        else "This evidence isolates compatibility-wrapper overhead on a deterministic pinned-route fixture; "
                        "it does not compare the complete production V2 handlers with a native V3 implementation."
                    )
                )
            ),
            "thresholds_applied": workload in {"actual_v2_livez", "native_gateway_livez"},
            "threshold_scope": (
                "gateway_livez_native_v2_v3_only"
                if workload == "native_gateway_livez"
                else (
                    "gateway_livez_and_compatibility_wrapper_only"
                    if workload == "actual_v2_livez"
                    else (
                        "none_synthetic_business_descriptive_only"
                        if workload == "synthetic_osc_cases"
                        else "none"
                    )
                )
            ),
            "unproven_requirements": [
                "complete production V2 service-stack measurements",
                "native V3 business-handler measurements",
                "matched production business corpus and machine state",
                "business-workload release-SLO evaluation over those production measurements",
                "real MariaDB-backed OSC POST plus explicit session/NAS/folder/archive measurements",
            ],
        },
        "limitations": [
            "No production V2 or V3 service was started.",
            (
                "The actual V2 and native V3 OSC handlers ran over independent disposable in-memory "
                "SQLite copies of the same corpus; this excludes production MariaDB, network, NAS, "
                "session, and service lifecycle costs."
                if workload == "synthetic_osc_cases"
                else (
                "The actual V2 blueprint and native V3 gateway probe handlers ran directly in-process; "
                "no listener, service lifecycle, role lock, or compatibility application factory ran."
                if workload == "native_gateway_livez"
                else (
                "The production V2 /livez blueprint handler ran in-process without its service; "
                "the V3 arm remains a compatibility wrapper, not a native V3 handler."
                if workload == "actual_v2_livez"
                    else "The deterministic route fixture preserves the pinned route identity but is not the production /livez handler."
                )
                )
            ),
            "RSS growth is allocator- and OS-sensitive; isolated repeats reduce, but do not remove, measurement noise.",
            (
                "No release threshold is applied to the synthetic OSC business measurement; it is descriptive only."
                if workload == "synthetic_osc_cases"
                else (
                "Only the three named gateway /livez and wrapper-overhead thresholds are applied; "
                "no native-V3, business, model, quality, or throughput threshold is inferred."
                if workload == "actual_v2_livez"
                else (
                    "Only gateway /livez thresholds are applied; the synthetic business corpus remains "
                    "unmeasured because the native business handler does not exist."
                    if workload == "native_gateway_livez"
                    else "No release threshold is inferred from descriptive latency or memory deltas."
                )
                )
            ),
        ],
        "hash_scheme": "sha256(canonical-json-without-evidence_sha256)",
    }
    evidence_without_hash = _canonical_json(result)
    result["evidence_sha256"] = _sha256_bytes(evidence_without_hash)
    verify_evidence_hash(result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmup", type=int, default=100)
    parser.add_argument("--iterations", type=int, default=1_000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--workload", choices=WORKLOADS, default=DEFAULT_WORKLOAD)
    parser.add_argument("--output", type=Path, help="atomically write the same JSON emitted on stdout")
    parser.add_argument("--worker-mode", choices=ALL_MODES, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.worker_mode:
            payload = measure_mode(
                args.worker_mode,
                warmup=args.warmup,
                iterations=args.iterations,
                workload=args.workload,
            )
        else:
            payload = run_benchmark(
                warmup=args.warmup,
                iterations=args.iterations,
                repeats=args.repeats,
                workload=args.workload,
            )
    except PerfEvidenceError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    encoded = _canonical_json(payload) + b"\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_bytes(encoded)
        os.replace(temporary, args.output)
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

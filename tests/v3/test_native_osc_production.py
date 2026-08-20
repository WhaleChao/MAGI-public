from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from flask import Flask
from werkzeug.test import Client
from werkzeug.wrappers import Response

from magi_v3 import osc_cases as cases_module
from magi_v3.osc_cases import (
    MariaDBCaseStore,
    OscCasesApplication,
    OscCasesService,
    SQLiteCaseStore,
    initialize_sqlite_cases_schema,
)
from magi_v3.osc_main import (
    ConfiguredPathCanonicalizer,
    DoubleSubmitCsrfProtection,
    EnvironmentApiKeyAuthorizer,
    FlaskSessionAuthorizer,
    MariaDBLawyerResolver,
    MariaDBUserLoader,
    V2SecurityHeaderPolicy,
    create_main_app,
)


ROOT = Path(__file__).resolve().parents[2]


def test_path_canonicalizer_maps_nas_strings_without_probing_filesystem(monkeypatch) -> None:
    probes: list[tuple[Any, ...]] = []

    def forbidden_probe(*args: Any, **_kwargs: Any) -> Any:
        probes.append(args)
        raise AssertionError("path canonicalization must not probe a filesystem")

    monkeypatch.setattr(os, "stat", forbidden_probe)
    canonicalize = ConfiguredPathCanonicalizer((("/Volumes/law-firm", "Z:"),))

    assert canonicalize("/Volumes/law-firm/cases/2026-0001") == "Z:\\cases\\2026-0001"
    assert canonicalize("Z:\\cases\\2026-0001") == "Z:\\cases\\2026-0001"
    assert probes == []


class ScriptedCursor:
    def __init__(self, connection: "ScriptedConnection") -> None:
        self.connection = connection
        self.sql = ""
        self.params: tuple[Any, ...] = ()
        self.rowcount = 0
        self.lastrowid: int | None = None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.sql = sql
        self.params = params
        self.connection.events.append(("execute", sql, params))
        self.rowcount = 1 if sql.startswith(("INSERT", "UPDATE")) else 0
        self.lastrowid = 17 if sql.startswith("INSERT") else None

    def fetchone(self) -> dict[str, Any] | None:
        if "GET_LOCK" in self.sql:
            return {"acquired": 1}
        if "RELEASE_LOCK" in self.sql:
            return {"released": 1}
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        if "SELECT case_number" in self.sql:
            return [{"case_number": "2026-0004"}]
        return []

    def close(self) -> None:
        self.connection.events.append(("cursor_close",))


class ScriptedConnection:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []

    def cursor(self, **_kwargs: Any) -> ScriptedCursor:
        return ScriptedCursor(self)

    def start_transaction(self) -> None:
        self.events.append(("start_transaction",))

    def commit(self) -> None:
        self.events.append(("commit",))

    def rollback(self) -> None:
        self.events.append(("rollback",))

    def close(self) -> None:
        self.events.append(("close",))


def test_mariadb_transaction_commits_then_releases_locks_and_closes() -> None:
    connection = ScriptedConnection()
    store = MariaDBCaseStore(lambda: (connection, {"host": "synthetic"}))

    with store.transaction() as transaction:
        assert transaction.next_case_number(2026) == "2026-0005"
        assert transaction.find_existing("2026-0005", "row-1") is None
        assert transaction.insert_case(
            {column: ("row-1" if column == "id" else "2026-0005" if column == "case_number" else None)
             for column in cases_module._WRITE_COLUMNS}
        ) == (1, 17)

    names = [event[0] for event in connection.events]
    assert names[0] == "start_transaction"
    assert "commit" in names
    assert "rollback" not in names
    assert names[-1] == "close"
    commit_index = names.index("commit")
    release_indices = [
        index
        for index, event in enumerate(connection.events)
        if event[0] == "execute" and "RELEASE_LOCK" in event[1]
    ]
    assert len(release_indices) == 3
    assert all(index > commit_index for index in release_indices)
    sql = [event[1] for event in connection.events if event[0] == "execute"]
    assert any("GET_LOCK" in statement for statement in sql)
    assert any("FOR UPDATE" in statement for statement in sql)


def test_mariadb_transaction_rolls_back_then_releases_lock_and_closes() -> None:
    connection = ScriptedConnection()
    store = MariaDBCaseStore(lambda: connection)

    try:
        with store.transaction() as transaction:
            transaction.find_existing("2026-FAIL", "row-fail")
            raise RuntimeError("injected")
    except RuntimeError as exc:
        assert str(exc) == "injected"

    names = [event[0] for event in connection.events]
    assert "rollback" in names
    assert "commit" not in names
    assert names[-1] == "close"
    rollback_index = names.index("rollback")
    release_indices = [
        index
        for index, event in enumerate(connection.events)
        if event[0] == "execute" and "RELEASE_LOCK" in event[1]
    ]
    assert release_indices and all(index > rollback_index for index in release_indices)


def test_mariadb_list_read_uses_one_transaction_and_commits() -> None:
    connection = ScriptedConnection()
    store = MariaDBCaseStore(lambda: connection)
    with store.transaction() as transaction:
        assert transaction.list_cases(cases_module.CaseListQuery(limit=25)) == []
    sql = [event[1] for event in connection.events if event[0] == "execute"]
    assert len([statement for statement in sql if "FROM cases" in statement]) == 1
    assert ("commit",) in connection.events
    assert ("rollback",) not in connection.events
    assert connection.events[-1] == ("close",)


def test_mariadb_closed_predicates_match_actual_v2_helpers_and_generated_sql() -> None:
    from api.blueprints.osc_cases import _osc_final_closed_sql, _osc_status_scope_sql

    native_final = cases_module._final_closed_sql(dialect="mariadb")
    assert native_final == _osc_final_closed_sql()
    assert "REPLACE(COALESCE(folder_path, ''), '\\\\', '/')" in native_final
    assert cases_module._status_scope_sql(
        "working", dialect="mariadb"
    ) == _osc_status_scope_sql("working")
    assert cases_module._status_scope_sql(
        "closed", dialect="mariadb"
    ) == _osc_status_scope_sql("closed")

    for scope, expected in (
        ("all", native_final),
        ("working", _osc_status_scope_sql("working")),
        ("closed", _osc_status_scope_sql("closed")),
    ):
        connection = ScriptedConnection()
        with MariaDBCaseStore(lambda: connection).transaction() as transaction:
            transaction.list_cases(cases_module.CaseListQuery(status_scope=scope))
        generated = next(
            event[1]
            for event in connection.events
            if event[0] == "execute" and "FROM cases" in event[1]
        )
        assert expected in generated


class ScalarCursor:
    def execute(self, _sql: str, _params: tuple[Any, ...] = ()) -> None:
        return None

    def fetchall(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "typed-row",
                "created_date": datetime(2026, 7, 14, 9, 8, 7),
                "hearing_date": date(2026, 7, 15),
                "hearing_time": time(13, 14, 15),
                "duration": timedelta(hours=2, minutes=3, seconds=4),
                "negative_duration": -timedelta(seconds=1),
                "amount": Decimal("123.45"),
            }
        ]

    def close(self) -> None:
        return None


class ScalarConnection:
    def cursor(self, **_kwargs: Any) -> ScalarCursor:
        return ScalarCursor()

    def start_transaction(self) -> None:
        return None

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None


def test_mariadb_rows_normalize_driver_scalars_to_v2_json_contract() -> None:
    with MariaDBCaseStore(lambda: ScalarConnection()).transaction() as transaction:
        row = transaction.list_cases(cases_module.CaseListQuery(limit=1))[0]

    assert row == {
        "id": "typed-row",
        "created_date": "2026-07-14 09:08:07",
        "hearing_date": "2026-07-15",
        "hearing_time": "13:14:15",
        "duration": "02:03:04",
        "negative_duration": "-00:00:01",
        "amount": 123.45,
    }
    assert json.loads(json.dumps(row)) == row


class ConcurrentState:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
        self.guard = threading.Lock()
        self.active_case_locks = 0
        self.max_active_case_locks = 0
        self.connections: list["ConcurrentConnection"] = []


class ConcurrentCursor:
    def __init__(self, connection: "ConcurrentConnection") -> None:
        self.connection = connection
        self.sql = ""
        self.params: tuple[Any, ...] = ()
        self.rowcount = 0
        self.lastrowid: int | None = None
        self.result: Any = None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self.sql, self.params = sql, params
        state = self.connection.state
        if "GET_LOCK" in sql:
            name, timeout = str(params[0]), float(params[1])
            acquired = state.locks[name].acquire(timeout=timeout)
            if acquired:
                self.connection.held.append(name)
                if ":case:" in name:
                    with state.guard:
                        state.active_case_locks += 1
                        state.max_active_case_locks = max(
                            state.max_active_case_locks, state.active_case_locks
                        )
            self.result = {"acquired": 1 if acquired else 0}
            return
        if "RELEASE_LOCK" in sql:
            name = str(params[0])
            if name in self.connection.held:
                self.connection.held.remove(name)
                if ":case:" in name:
                    with state.guard:
                        state.active_case_locks -= 1
                state.locks[name].release()
            self.result = {"released": 1}
            return
        if sql.startswith("SELECT * FROM cases WHERE case_number"):
            self.result = dict(state.rows.get(str(params[0]), {})) or None
            return
        if sql.startswith("SELECT * FROM cases WHERE id"):
            self.result = next(
                (dict(row) for row in state.rows.values() if row.get("id") == params[0]),
                None,
            )
            return
        if sql.startswith("INSERT INTO cases"):
            row = dict(zip(cases_module._WRITE_COLUMNS, params))
            state.rows[str(row["case_number"])] = row | {
                "legal_aid_status": "",
                "manual_status_lock": 0,
            }
            self.rowcount, self.lastrowid = 1, len(state.rows)
            return
        if sql.startswith("UPDATE cases SET"):
            set_clause = sql.split(" SET ", 1)[1].split(", updated_at", 1)[0]
            columns = [part.split(" = ", 1)[0] for part in set_clause.split(",")]
            row_id = str(params[-1])
            target = next(row for row in state.rows.values() if row.get("id") == row_id)
            target.update(dict(zip(columns, params[:-1])))
            self.rowcount = 1
            return
        raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self) -> Any:
        return self.result

    def fetchall(self) -> list[Any]:
        return list(self.result or [])

    def close(self) -> None:
        return None


class ConcurrentConnection:
    def __init__(self, state: ConcurrentState) -> None:
        self.state = state
        self.held: list[str] = []
        self.commits = 0
        self.rollbacks = 0
        state.connections.append(self)

    def cursor(self, **_kwargs: Any) -> ConcurrentCursor:
        return ConcurrentCursor(self)

    def start_transaction(self) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        while self.held:
            name = self.held.pop()
            self.state.locks[name].release()


def test_duplicate_create_is_serialized_to_one_insert_and_one_upsert() -> None:
    state = ConcurrentState()
    barrier = threading.Barrier(2)
    results: list[str] = []
    errors: list[BaseException] = []

    def worker(row_id: str, client_name: str) -> None:
        try:
            service = OscCasesService(
                MariaDBCaseStore(lambda: ConcurrentConnection(state)),
                id_factory=lambda: row_id,
                year_provider=lambda: 2026,
            )
            barrier.wait(timeout=2)
            result = service.create_case(
                {
                    "id": row_id,
                    "case_number": "2026-LOCKED",
                    "client_name": client_name,
                }
            )
            results.append(result.mode)
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("row-a", "A")),
        threading.Thread(target=worker, args=("row-b", "B")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) == ["insert", "upsert"]
    assert list(state.rows) == ["2026-LOCKED"]
    assert state.max_active_case_locks == 1
    assert all(connection.commits == 1 for connection in state.connections)
    assert all(connection.rollbacks == 0 for connection in state.connections)
    assert all(not connection.held for connection in state.connections)


def _signed_session_cookie(secret: str, user_id: str) -> str:
    app = Flask("native-osc-session-contract")
    app.secret_key = secret
    with app.test_client() as client:
        with client.session_transaction() as session:
            session["_user_id"] = user_id
            session["_fresh"] = True
        cookie = client.get_cookie(app.config["SESSION_COOKIE_NAME"])
        assert cookie is not None
        return cookie.value


def test_flask_session_authorizer_accepts_real_flask_login_cookie_and_rejects_tamper() -> None:
    loaded: list[str] = []
    authorizer = FlaskSessionAuthorizer(
        "session-secret",
        lambda user_id: loaded.append(user_id) or {"id": user_id, "is_active": True},
    )
    cookie = _signed_session_cookie("session-secret", "42")
    assert authorizer({"HTTP_COOKIE": f"session={cookie}"}) is True
    assert loaded == ["42"]
    assert authorizer({"HTTP_COOKIE": f"session={cookie}tampered"}) is False
    assert loaded == ["42"]


def test_flask_session_authorizer_rejects_missing_and_inactive_users() -> None:
    cookie = _signed_session_cookie("session-secret", "missing")
    missing = FlaskSessionAuthorizer("session-secret", lambda _user_id: None)
    inactive = FlaskSessionAuthorizer(
        "session-secret", lambda user_id: {"id": user_id, "is_active": False}
    )
    assert missing({"HTTP_COOKIE": f"session={cookie}"}) is False
    assert inactive({"HTTP_COOKIE": f"session={cookie}"}) is False


class UserCursor:
    def __init__(self, connection: "UserConnection") -> None:
        self.connection = connection

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.connection.query = (sql, params)

    def fetchone(self) -> dict[str, Any] | None:
        return self.connection.row

    def close(self) -> None:
        self.connection.cursor_closed += 1


class UserConnection:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.query: tuple[str, tuple[Any, ...]] | None = None
        self.cursor_closed = 0
        self.rollbacks = 0
        self.closed = 0

    def cursor(self, **_kwargs: Any) -> UserCursor:
        return UserCursor(self)

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


def test_mariadb_user_loader_queries_only_identity_fields_and_closes_read_transaction() -> None:
    active = UserConnection({"id": 7, "username": "operator", "role": "admin"})
    missing = UserConnection(None)
    assert MariaDBUserLoader(lambda: active)("7") == {
        "id": 7,
        "username": "operator",
        "role": "admin",
    }
    assert MariaDBUserLoader(lambda: missing)("404") is None
    assert active.query is not None
    sql, params = active.query
    assert sql == "SELECT id, username, role FROM users WHERE id = %s LIMIT 1"
    assert "password" not in sql.lower()
    assert params == ("7",)
    assert active.cursor_closed == active.rollbacks == active.closed == 1
    assert missing.cursor_closed == missing.rollbacks == missing.closed == 1


class SettingsCursor:
    def __init__(self, connection: "SettingsConnection") -> None:
        self.connection = connection

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.connection.queries.append((sql, params))

    def fetchall(self) -> list[dict[str, str]]:
        return [
            {"setting_key": "default_lawyer", "value": self.connection.regular},
            {"setting_key": "default_debt_lawyer", "value": self.connection.debt},
        ]

    def close(self) -> None:
        return None


class SettingsConnection:
    def __init__(self, regular: str, debt: str) -> None:
        self.regular = regular
        self.debt = debt
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.rollbacks = 0
        self.closed = 0

    def cursor(self, **_kwargs: Any) -> SettingsCursor:
        return SettingsCursor(self)

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed += 1


def test_two_hundred_row_list_uses_constant_bounded_ttl_lawyer_lookup() -> None:
    case_connection = sqlite3.connect(":memory:")
    initialize_sqlite_cases_schema(case_connection)
    rows = [
        (
            f"case-{index}",
            f"2026-{index + 1:04d}",
            f"Client {index}",
            "法律扶助案件",
            "消費者債務清理" if index % 2 else "民事",
            "更生" if index % 2 else "損害賠償",
        )
        for index in range(200)
    ]
    case_connection.executemany(
        "INSERT INTO cases (id,case_number,client_name,case_category,case_type,case_reason) "
        "VALUES (?,?,?,?,?,?)",
        rows,
    )
    now = [100.0]
    setting_connections: list[SettingsConnection] = []

    def setting_factory() -> SettingsConnection:
        connection = SettingsConnection("一般律師", "消債律師")
        setting_connections.append(connection)
        return connection

    resolver = MariaDBLawyerResolver(
        setting_factory,
        {},
        cache_ttl_seconds=10,
        clock=lambda: now[0],
    )
    service = OscCasesService(
        SQLiteCaseStore(case_connection),
        id_factory=lambda: "unused",
        lawyer_resolver=resolver,
    )
    try:
        first = service.list_cases(cases_module.CaseListQuery(limit=500))
        second = service.list_cases(cases_module.CaseListQuery(limit=500))
        assert len(first) == len(second) == 200
        assert {row["lawyer"] for row in first} == {"一般律師", "消債律師"}
        assert len(setting_connections) == 1
        assert sum(len(connection.queries) for connection in setting_connections) == 1
        sql, params = setting_connections[0].queries[0]
        assert "WHERE `key` IN" in sql
        assert len(params) == 6
        assert resolver.cache_info()["size"] == 2
        assert resolver.cache_info()["maximum_size"] == 6

        now[0] = 111.0
        third = service.list_cases(cases_module.CaseListQuery(limit=500))
        assert len(third) == 200
        assert len(setting_connections) == 2
        assert sum(len(connection.queries) for connection in setting_connections) == 2
        assert resolver.cache_info()["size"] <= resolver.cache_info()["maximum_size"] == 6
    finally:
        case_connection.close()


def _sqlite_native_app(*, csrf: DoubleSubmitCsrfProtection) -> tuple[Client, sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    initialize_sqlite_cases_schema(connection)
    service = OscCasesService(
        SQLiteCaseStore(connection),
        id_factory=lambda: "csrf-case",
        year_provider=lambda: 2026,
    )
    return (
        Client(
            OscCasesApplication(service, authorize=lambda _environ: True, csrf=csrf),
            Response,
        ),
        connection,
    )


def test_get_head_and_post_match_double_submit_csrf_contract() -> None:
    csrf = DoubleSubmitCsrfProtection(
        api_key_authorizer=lambda _environ: False,
        token_factory=lambda: "fixed-csrf-token",
    )
    client, connection = _sqlite_native_app(csrf=csrf)
    try:
        get_response = client.get("/api/osc/cases")
        assert get_response.status_code == 200
        assert "X-CSRF-Token=fixed-csrf-token" in get_response.headers["Set-Cookie"]
        assert "Max-Age=86400" in get_response.headers["Set-Cookie"]
        assert "Expires=" in get_response.headers["Set-Cookie"]
        assert "SameSite=Lax" in get_response.headers["Set-Cookie"]
        assert "HttpOnly" not in get_response.headers["Set-Cookie"]

        head_response = client.head("/api/osc/cases")
        assert head_response.status_code == 200
        assert head_response.get_data() == b""
        assert int(head_response.headers["Content-Length"]) > 0

        missing = client.post("/api/osc/cases", json={"client_name": "CSRF"})
        assert missing.status_code == 403
        assert missing.get_json()["reason"] == "csrf_token_missing_in_request"
        assert connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0

        accepted = client.post(
            "/api/osc/cases",
            json={"client_name": "CSRF"},
            headers={"X-CSRF-Token": "fixed-csrf-token"},
        )
        assert accepted.status_code == 200
        assert accepted.get_json()["mode"] == "insert"
    finally:
        connection.close()


def _assert_v2_core_security_headers(response: Response) -> None:
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert response.headers["X-XSS-Protection"] == "1; mode=block"
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert response.headers["Content-Security-Policy"] == (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "media-src 'self' blob:; frame-src 'self' blob:; object-src 'self' blob:;"
    )
    assert response.headers["Cache-Control"] == "no-store"


class FailingListService:
    @staticmethod
    def list_cases(_query: Any) -> list[dict[str, Any]]:
        raise RuntimeError("injected list failure")


def test_native_get_post_401_403_and_500_all_apply_v2_security_headers() -> None:
    policy = V2SecurityHeaderPolicy({})
    csrf = DoubleSubmitCsrfProtection(
        api_key_authorizer=lambda _environ: False,
        token_factory=lambda: "security-csrf",
    )
    client, connection = _sqlite_native_app(csrf=csrf)
    client.application.response_security_headers = policy
    try:
        get_response = client.get("/api/osc/cases")
        post_response = client.post(
            "/api/osc/cases",
            json={"client_name": "Security headers"},
            headers={"X-CSRF-Token": "security-csrf"},
        )
        forbidden = Client(
            OscCasesApplication(
                client.application.service,
                authorize=lambda _environ: True,
                csrf=DoubleSubmitCsrfProtection(
                    api_key_authorizer=lambda _environ: False
                ),
                response_security_headers=policy,
            ),
            Response,
        ).post("/api/osc/cases", json={"client_name": "Denied"})
        unauthorized = Client(
            OscCasesApplication(
                client.application.service,
                authorize=lambda _environ: False,
                csrf=csrf,
                response_security_headers=policy,
            ),
            Response,
        ).get("/api/osc/cases")
        internal_error = Client(
            OscCasesApplication(
                FailingListService(),  # type: ignore[arg-type]
                authorize=lambda _environ: True,
                csrf=csrf,
                response_security_headers=policy,
            ),
            Response,
        ).get("/api/osc/cases")

        assert [
            response.status_code
            for response in (get_response, post_response, unauthorized, forbidden, internal_error)
        ] == [200, 200, 401, 403, 500]
        for response in (get_response, post_response, unauthorized, forbidden, internal_error):
            _assert_v2_core_security_headers(response)
            assert "Strict-Transport-Security" not in response.headers
    finally:
        connection.close()


def test_v2_security_header_policy_matches_actual_hsts_environment_gate() -> None:
    positive_environments = (
        {"MAGI_ENABLE_HSTS": "1"},
        {"MAGI_FORCE_HTTPS": "true"},
        {"MAGI_SECURE_COOKIES": "yes", "MAGI_PUBLIC_BASE_URL": "https://magi.test"},
        {"MAGI_DEPLOYMENT_MODE": "prod", "MAGI_BASE_URL": "https://magi.test"},
    )
    for environment in positive_environments:
        headers = V2SecurityHeaderPolicy(environment)({"PATH_INFO": "/api/osc/cases"})
        assert headers["Strict-Transport-Security"] == (
            "max-age=31536000; includeSubDomains"
        )

    for environment in (
        {},
        {"MAGI_SECURE_COOKIES": "1", "MAGI_PUBLIC_BASE_URL": "http://magi.test"},
        {"MAGI_DEPLOYMENT_MODE": "development", "MAGI_BASE_URL": "https://magi.test"},
    ):
        headers = V2SecurityHeaderPolicy(environment)({"PATH_INFO": "/api/osc/cases"})
        assert "Strict-Transport-Security" not in headers


def test_valid_api_key_exempts_csrf_but_does_not_replace_session_authorization() -> None:
    csrf = DoubleSubmitCsrfProtection(
        api_key_authorizer=EnvironmentApiKeyAuthorizer(("expected-key",)),
    )
    client, connection = _sqlite_native_app(csrf=csrf)
    try:
        accepted = client.post(
            "/api/osc/cases",
            json={"client_name": "API key plus session"},
            headers={"X-API-Key": "expected-key"},
        )
        assert accepted.status_code == 200

        service = OscCasesService(
            SQLiteCaseStore(connection),
            id_factory=lambda: "never",
        )
        denied = Client(
            OscCasesApplication(service, authorize=lambda _environ: False, csrf=csrf),
            Response,
        ).post(
            "/api/osc/cases",
            json={"client_name": "API key alone"},
            headers={"X-API-Key": "expected-key"},
        )
        assert denied.status_code == 401
    finally:
        connection.close()


def test_production_composition_is_lazy_and_unauthorized_request_opens_no_db() -> None:
    connection_calls = 0
    fallback_calls: list[str] = []

    def connection_factory() -> Any:
        nonlocal connection_calls
        connection_calls += 1
        raise AssertionError("unauthorized/fallback request must not connect")

    def fallback(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        fallback_calls.append(environ["PATH_INFO"])
        start_response("204 No Content", [])
        return [b""]

    app = create_main_app(
        connection_factory=connection_factory,
        fallback_factory=lambda: fallback,
        environ={"FLASK_SECRET_KEY": "composition-secret"},
        token_factory=lambda: "composition-csrf",
    )
    assert connection_calls == 0
    client = Client(app, Response)
    assert client.get("/api/osc/other").status_code == 204
    assert client.get("/api/osc/cases").status_code == 401
    invalid_csrf = client.post("/api/osc/cases", json={"client_name": "No session"})
    assert invalid_csrf.status_code == 403
    client.set_cookie("X-CSRF-Token", "composition-csrf")
    valid_csrf = client.post(
        "/api/osc/cases",
        json={"client_name": "No session"},
        headers={"X-CSRF-Token": "composition-csrf"},
    )
    assert valid_csrf.status_code == 401
    assert connection_calls == 0
    assert fallback_calls == ["/api/osc/other"]


def test_production_composition_accepts_signed_session_then_lists_in_one_db_transaction() -> None:
    secret = "production-composition-session-secret"
    user_connection = UserConnection({"id": 42, "username": "operator", "role": "admin"})
    case_connection = ScriptedConnection()
    connections: list[Any] = [user_connection, case_connection]

    def connection_factory() -> Any:
        assert connections, "unexpected extra DB connection"
        return connections.pop(0)

    def fallback(_environ: dict[str, Any], start_response: Any) -> list[bytes]:
        start_response("204 No Content", [])
        return [b""]

    app = create_main_app(
        connection_factory=connection_factory,
        fallback_factory=lambda: fallback,
        environ={"FLASK_SECRET_KEY": secret},
        token_factory=lambda: "signed-session-csrf",
    )
    client = Client(app, Response)
    client.set_cookie("session", _signed_session_cookie(secret, "42"))

    response = client.get("/api/osc/cases?limit=25")

    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "items": []}
    _assert_v2_core_security_headers(response)
    assert not connections
    assert user_connection.query == (
        "SELECT id, username, role FROM users WHERE id = %s LIMIT 1",
        ("42",),
    )
    sql = [event[1] for event in case_connection.events if event[0] == "execute"]
    assert len([statement for statement in sql if "FROM cases" in statement]) == 1
    assert ("commit",) in case_connection.events
    assert ("rollback",) not in case_connection.events
    assert case_connection.events[-1] == ("close",)


def test_importing_production_composition_has_no_v2_or_filesystem_side_effect(tmp_path: Path) -> None:
    script = """
import json, pathlib, sys, threading
root = pathlib.Path.cwd()
before_files = sorted(p.name for p in root.iterdir())
before_threads = threading.active_count()
before_api = {name for name in sys.modules if name.startswith('api.')}
import magi_v3.osc_main
print(json.dumps({
    'files': before_files == sorted(p.name for p in root.iterdir()),
    'threads': before_threads == threading.active_count(),
    'new_api': sorted(name for name in sys.modules if name.startswith('api.') and name not in before_api),
}))
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(result.stdout) == {"files": True, "threads": True, "new_api": []}

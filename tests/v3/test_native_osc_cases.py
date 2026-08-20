from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from werkzeug.test import Client
from werkzeug.wrappers import Response

from magi_v3.osc_cases import (
    CaseListQuery,
    OscCasesApplication,
    OscCasesService,
    RequestValidationError,
    SQLiteCaseStore,
    initialize_sqlite_cases_schema,
)


ROOT = Path(__file__).resolve().parents[2]


class AllowCsrf:
    @staticmethod
    def validate(_environ: Any) -> tuple[bool, str]:
        return True, "test"

    @staticmethod
    def safe_response_cookie(_environ: Any) -> None:
        return None


@pytest.fixture
def connection() -> sqlite3.Connection:
    value = sqlite3.connect(":memory:")
    initialize_sqlite_cases_schema(value)
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def service(connection: sqlite3.Connection) -> OscCasesService:
    sequence = iter(("web-native-1", "web-native-2", "web-native-3", "web-native-4"))

    def lawyer(current: str, case_type: str, _reason: str, _category: str) -> str:
        if current:
            return current
        return "消債承辦律師" if case_type == "消費者債務清理" else "正式承辦律師"

    return OscCasesService(
        SQLiteCaseStore(connection),
        id_factory=lambda: next(sequence),
        year_provider=lambda: 2026,
        path_canonicalizer=lambda path: path.replace("K:\\", "Z:\\"),
        lawyer_resolver=lawyer,
    )


@pytest.fixture
def client(service: OscCasesService) -> Client:
    return Client(
        OscCasesApplication(
            service,
            authorize=lambda _environ: True,
            csrf=AllowCsrf(),
        ),
        Response,
    )


def test_native_create_preserves_core_v2_alias_and_response_contract(
    client: Client, connection: sqlite3.Connection
) -> None:
    response = client.post(
        "/api/osc/cases",
        json={
            "name": "測試當事人",
            "category": "法扶",
            "type": "刑事",
            "case_stage": "一審",
            "case_reason": "涉 詐欺、洗錢防制法",
            "legal_aid_number": "1150101-E-001",
            "court": "臺灣花蓮地方法院",
            "court_case_number": "115年度訴字第1號",
            "division": "義股",
            "lawyer": "範例律師",
            "folder_path": "K:\\案件\\測試",
            "auto_create_folder": False,
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "result": {"rowcount": 1, "lastrowid": 1},
        "id": "web-native-1",
        "case_number": "2026-0001",
        "mode": "insert",
    }
    row = connection.execute("SELECT * FROM cases").fetchone()
    assert row is not None
    assert row["client_name"] == "測試當事人"
    assert row["case_category"] == "法律扶助案件"
    assert row["case_reason"] == "詐欺、洗錢防制法"
    assert row["laf_case_no"] == row["application_no"] == "1150101-E-001"
    assert row["court_case_no"] == row["court_case_number"] == "115年度訴字第1號"
    assert row["court_division"] == "義股"
    assert row["lawyer"] == "正式承辦律師"
    assert row["status"] == "進行中"
    assert row["folder_path"] == "Z:\\案件\\測試"


def test_native_list_filters_orders_and_preserves_display_contract(
    service: OscCasesService, client: Client, connection: sqlite3.Connection
) -> None:
    first = service.create_case(
        {
            "case_number": "2026-0007",
            "client_name": "開啟案件",
            "case_category": "法律扶助案件",
            "case_type": "消費者債務清理",
            "case_reason": "更生",
            "lawyer": "範例律師",
        }
    )
    second = service.create_case(
        {
            "case_number": "2026-0008",
            "client_name": "已結案件",
            "case_category": "一般案件",
            "case_type": "民事",
            "case_reason": "法律顧問契約",
        }
    )
    connection.execute(
        "UPDATE cases SET legal_aid_status = '未結案', updated_at = '2026-01-01' WHERE id = ?",
        (first.row_id,),
    )
    connection.execute(
        "UPDATE cases SET status = '已結案', updated_at = '2026-12-31' WHERE id = ?",
        (second.row_id,),
    )
    connection.commit()

    response = client.get(
        "/api/osc/cases?q=開啟&case_type=消費者債務清理&case_kind=法律扶助案件&status_scope=active&limit=5"
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["id"] == first.row_id
    assert item["effective_status"] == item["status_display"] == "進行中"
    assert item["case_type_display"] == "消費者債務清理"
    assert item["case_reason_display"] == "更生"
    assert item["lawyer"] == "消債承辦律師"

    all_items = client.get("/api/osc/cases?limit=5").get_json()["items"]
    assert [row["id"] for row in all_items] == [first.row_id, second.row_id]
    consultant = next(row for row in all_items if row["id"] == second.row_id)
    assert consultant["case_type_display"] == "民事｜法律顧問"


def test_duplicate_post_for_closed_case_fails_archive_closed_and_rolls_back(
    service: OscCasesService, connection: sqlite3.Connection
) -> None:
    created = service.create_case(
        {
            "case_number": "2026-0099",
            "client_name": "原當事人",
            "case_type": "民事",
            "status": "進行中",
            "folder_path": "Y:/lumi/03_工作資料/10_結案/2026-0099",
        }
    )
    connection.execute(
        "UPDATE cases SET legal_aid_status = '已結案', manual_status_lock = 1 WHERE id = ?",
        (created.row_id,),
    )
    connection.commit()

    with pytest.raises(RequestValidationError, match="archive side effects") as failure:
        service.create_case(
            {
                "case_number": "2026-0099",
                "client_name": "更新當事人",
                "case_type": "民事",
                "status": "進行中",
                "folder_path": "Z:/active/2026-0099",
            }
        )

    assert failure.value.status == 501
    row = connection.execute("SELECT * FROM cases WHERE id = ?", (created.row_id,)).fetchone()
    assert row["client_name"] == "原當事人"
    assert row["status"] == "進行中"
    assert row["legal_aid_status"] == "已結案"
    assert row["folder_path"].startswith("Y:/")
    assert connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1


def test_create_rolls_back_insert_when_in_transaction_hook_fails(
    connection: sqlite3.Connection,
) -> None:
    states: list[bool] = []

    def fail_after_insert(_transaction: Any, _result: Any, _payload: Any) -> None:
        states.append(connection.in_transaction)
        assert connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 1
        raise RuntimeError("injected downstream failure")

    service = OscCasesService(
        SQLiteCaseStore(connection),
        id_factory=lambda: "rollback-case",
        year_provider=lambda: 2026,
        post_persist=fail_after_insert,
    )
    app = Client(
        OscCasesApplication(
            service,
            authorize=lambda _environ: True,
            csrf=AllowCsrf(),
        ),
        Response,
    )

    response = app.post("/api/osc/cases", json={"client_name": "應回滾"})

    assert response.status_code == 500
    assert response.get_json() == {"ok": False, "error": "internal_error"}
    assert states == [True]
    assert connection.in_transaction is False
    assert connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "status", "error"),
    [
        ("post", "/api/osc/cases", {"json": {}}, 400, "client_name required"),
        ("get", "/api/osc/cases?limit=NaN", {}, 400, "limit must be an integer"),
        ("post", "/api/osc/cases", {"data": "{" , "content_type": "application/json"}, 400, "invalid JSON"),
        ("post", "/api/osc/cases", {"data": "{}", "content_type": "text/plain"}, 415, "application/json required"),
        ("put", "/api/osc/cases", {"json": {}}, 405, "method not allowed"),
        ("get", "/api/osc/cases/other", {}, 404, "not found"),
    ],
)
def test_wsgi_validation_contract(
    client: Client,
    method: str,
    path: str,
    kwargs: dict[str, Any],
    status: int,
    error: str,
) -> None:
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == status
    assert response.get_json() == {"ok": False, "error": error}


def test_unimplemented_folder_side_effect_fails_explicitly_without_write(
    client: Client, connection: sqlite3.Connection
) -> None:
    response = client.post(
        "/api/osc/cases",
        json={"client_name": "不得靜默少做", "auto_create_folder": True},
    )
    assert response.status_code == 501
    assert "not implemented" in response.get_json()["error"]
    assert connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0


def test_unimplemented_archive_side_effect_fails_explicitly_without_write(
    client: Client, connection: sqlite3.Connection
) -> None:
    response = client.post(
        "/api/osc/cases",
        json={"client_name": "不得漏掉封存", "status": "已結案"},
    )
    assert response.status_code == 501
    assert "archive side effects" in response.get_json()["error"]
    assert connection.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0


def test_authorization_is_injected_and_denies_before_storage(
    service: OscCasesService, connection: sqlite3.Connection
) -> None:
    app = Client(
        OscCasesApplication(
            service,
            authorize=lambda _environ: False,
            csrf=AllowCsrf(),
        ),
        Response,
    )
    response = app.get("/api/osc/cases")
    assert response.status_code == 401
    assert response.get_json() == {"ok": False, "error": "authentication required"}
    assert connection.in_transaction is False


def test_application_can_wrap_the_remaining_gateway_surface_without_delegating_native_route(
    service: OscCasesService,
) -> None:
    seen: list[str] = []

    def fallback(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        seen.append(environ["PATH_INFO"])
        start_response("202 Accepted", [("Content-Type", "text/plain")])
        return [b"fallback"]

    app = Client(
        OscCasesApplication(
            service,
            authorize=lambda _environ: True,
            csrf=AllowCsrf(),
            fallback=fallback,
        ),
        Response,
    )
    native = app.get("/api/osc/cases")
    other = app.get("/api/osc/other")
    assert native.status_code == 200
    assert other.status_code == 202
    assert other.get_data() == b"fallback"
    assert seen == ["/api/osc/other"]


def test_import_has_no_v2_api_or_runtime_side_effects(tmp_path: Path) -> None:
    script = """
import json, pathlib, sys, threading
root = pathlib.Path.cwd()
before = sorted(p.name for p in root.iterdir())
threads = threading.active_count()
v2_before = {name for name in sys.modules if name.startswith('api.')}
import magi_v3.osc_cases
after = sorted(p.name for p in root.iterdir())
print(json.dumps({
    'files_unchanged': before == after,
    'threads_unchanged': threads == threading.active_count(),
    'new_v2_modules': sorted(name for name in sys.modules if name.startswith('api.') and name not in v2_before),
}))
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(result.stdout)
    assert report == {
        "files_unchanged": True,
        "threads_unchanged": True,
        "new_v2_modules": [],
    }


def test_service_api_can_be_used_without_wsgi(connection: sqlite3.Connection) -> None:
    service = OscCasesService(
        SQLiteCaseStore(connection),
        id_factory=lambda: "direct-service",
        year_provider=lambda: 2026,
    )
    created = service.create_case({"client": "Direct"})
    rows = service.list_cases(CaseListQuery(q="Direct", limit=1))
    assert created.case_number == "2026-0001"
    assert [row["id"] for row in rows] == ["direct-service"]

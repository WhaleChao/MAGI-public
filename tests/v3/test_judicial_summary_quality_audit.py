from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ops" / "audit_judicial_api_summary_quality.py"
SPEC = importlib.util.spec_from_file_location("judicial_summary_quality_audit", SCRIPT)
assert SPEC and SPEC.loader
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


class _StartTransactionConnection:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def start_transaction(self) -> None:
        self.calls.append("start_transaction")


class _ActiveTransactionConnection(_StartTransactionConnection):
    in_transaction = True


class _BeginConnection:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def begin(self) -> None:
        self.calls.append("begin")


class _Cursor:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def execute(self, sql: str) -> None:
        self.calls.append(sql)

    def close(self) -> None:
        self.calls.append("close")


class _SqlConnection:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def cursor(self) -> _Cursor:
        return _Cursor(self.calls)


def test_start_transaction_prefers_mysql_connector_api() -> None:
    conn = _StartTransactionConnection()
    audit._start_transaction(conn)
    assert conn.calls == ["start_transaction"]


def test_start_transaction_reuses_implicit_mysql_transaction() -> None:
    conn = _ActiveTransactionConnection()
    audit._start_transaction(conn)
    assert conn.calls == []


def test_start_transaction_supports_begin_api() -> None:
    conn = _BeginConnection()
    audit._start_transaction(conn)
    assert conn.calls == ["begin"]


def test_start_transaction_has_sql_fallback() -> None:
    conn = _SqlConnection()
    audit._start_transaction(conn)
    assert conn.calls == ["START TRANSACTION", "close"]


def test_raw_json_basenames_preserve_download_hash_for_process_state() -> None:
    row = {
        "source_jid": "NTDM,115,訴,1,20260618,2",
        "full_text_path": (
            "/cache/judicial_api/normalized/20260618/"
            "eb42602987fb_NTDM_115_1_20260618_2.txt"
        ),
    }

    names = audit._raw_json_basenames(
        row, lambda jid: jid.replace(",", "_")
    )

    assert names[0] == "eb42602987fb_NTDM_115_1_20260618_2.json"
    assert names[1] == "NTDM_115_訴_1_20260618_2.json"

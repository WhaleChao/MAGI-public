"""Native V3 OSC case-list and case-create WSGI application.

The module has no import-time connection, schema, thread, file, or network
side effects.  Storage, authorization, identifiers, clocks, path translation,
lawyer defaults, and transactional post-create work are explicit dependencies.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as datetime_time, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterable, Iterator, Mapping, Protocol
from urllib.parse import parse_qs

from .case_lifecycle import requires_closed_storage


_TEMPLATE_DISPLAY = "—"
_CASE_NUMBER = re.compile(r"^(?P<year>\d{4})-(?P<sequence>\d{4})$")
_CLOSING_LAF_STATUSES = frozenset({"已結案，待報結", "已結案，待送出"})
_FINAL_LAF_STATUSES = frozenset({"已結案"})
_OPEN_STATUSES = frozenset({"", "進行中", "未結案", "未結案/進行中", "active", "open", "ongoing", "pending"})
_DEMO_LAWYERS = frozenset({"範例律師", "示範律師", "測試律師", "Sample Lawyer", "Demo Lawyer"})
_CANONICAL_ARCHIVE_DRIVE_PREFIX = (
    (os.environ.get("MAGI_CANONICAL_ARCHIVE_DRIVE") or "Y:").rstrip("/\\") + "/"
)
_SELECT_COLUMNS = (
    "id",
    "case_number",
    "client_name",
    "case_category",
    "case_type",
    "case_stage",
    "case_reason",
    "laf_case_no",
    "application_no",
    "court_name",
    "court_case_no",
    "court_division",
    "legal_aid_status",
    "lawyer",
    "status",
    "manual_status_lock",
    "manual_status_source",
    "manual_status_at",
    "notes",
    "folder_path",
    "updated_at",
    "created_date",
)
_WRITE_COLUMNS = (
    "id",
    "case_number",
    "client_name",
    "client_phone",
    "client_email",
    "client_id_number",
    "case_category",
    "case_type",
    "case_stage",
    "case_reason",
    "laf_case_no",
    "application_no",
    "court_name",
    "court_case_no",
    "court_case_number",
    "court_division",
    "lawyer",
    "status",
    "notes",
    "folder_path",
)


class OscCasesError(RuntimeError):
    """Base error for the native OSC cases boundary."""


class RequestValidationError(OscCasesError):
    """The caller supplied an invalid or currently unsupported request."""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class CaseListQuery:
    q: str = ""
    case_type: str = ""
    case_kind: str = ""
    status_scope: str = "all"
    limit: int = 200


@dataclass(frozen=True, slots=True)
class CreateResult:
    row_id: str
    case_number: str
    mode: str
    rowcount: int
    lastrowid: int | None
    effects: Mapping[str, Any] = field(default_factory=dict)


class CaseTransaction(Protocol):
    def list_cases(self, query: CaseListQuery) -> list[dict[str, Any]]: ...

    def next_case_number(self, year: int) -> str: ...

    def find_existing(self, case_number: str, row_id: str) -> dict[str, Any] | None: ...

    def insert_case(self, values: Mapping[str, Any]) -> tuple[int, int | None]: ...

    def update_case(self, row_id: str, values: Mapping[str, Any]) -> int: ...

    def register_rollback_hook(self, hook: Callable[[], None]) -> None: ...


class CaseStore(Protocol):
    def transaction(self) -> Any: ...


class CsrfProtection(Protocol):
    def validate(self, environ: Mapping[str, Any]) -> tuple[bool, str]: ...

    def safe_response_cookie(self, environ: Mapping[str, Any]) -> str | None: ...


LawyerResolver = Callable[[str, str, str, str], str]
PostPersistHook = Callable[
    [CaseTransaction, CreateResult, Mapping[str, Any]],
    Mapping[str, Any] | None,
]
WSGIApp = Callable[[dict[str, Any], Callable[..., Any]], Iterable[bytes]]
ResponseSecurityHeaders = Callable[[Mapping[str, Any]], Mapping[str, str]]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_value(value: Any) -> Any:
    """Normalize MariaDB scalar types with the existing OSC JSON contract."""

    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, datetime_time):
        return value.isoformat()
    if isinstance(value, timedelta):
        total = int(value.total_seconds())
        sign = "-" if total < 0 else ""
        total = abs(total)
        hours = total // 3600
        minutes = (total % 3600) // 60
        seconds = total % 60
        return f"{sign}{hours:02d}:{minutes:02d}:{seconds:02d}"
    if isinstance(value, Decimal):
        return float(value)
    return value


def _json_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _json_value(value) for key, value in dict(row).items()}


def _wsgi_query_text(value: Any) -> str:
    """Recover UTF-8 bytes carried through WSGI's ISO-8859-1 string boundary."""

    text = str(value or "")
    try:
        return text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text


def _category(value: Any) -> str:
    text = _text(value)
    return {
        "法扶案件": "法律扶助案件",
        "法扶": "法律扶助案件",
        "法律扶助": "法律扶助案件",
    }.get(text, text)


def _clean_reason(value: Any) -> str:
    text = re.sub(r"\s+", "", _text(value)).strip("：:，,。；;、（）()[]【】")
    text = re.sub(r"^(涉嫌|涉及|涉犯|涉有|涉(?!外))", "", text)
    text = re.sub(r"(之)?(案件資料|資料)$", "", text)
    return text.strip("：:，,。；;、（）()[]【】")


def _is_template(row: Mapping[str, Any]) -> bool:
    name = _text(row.get("client_name"))
    number = _text(row.get("case_number"))
    folder = _text(row.get("folder_path"))
    return name == "範本" or ("範本" in name and number.startswith("0000")) or "0000-0000-範本" in folder


def _is_archive_path(value: Any) -> bool:
    path = _text(value).replace("\\", "/").lower()
    return (
        path.startswith(_CANONICAL_ARCHIVE_DRIVE_PREFIX.lower())
        or "/03_工作資料/10_結案/" in path
        or "/10_結案/" in path
    )


def _is_closed_status(value: Any) -> bool:
    text = _text(value)
    return "已結案" in text or text.lower() in {"closed", "close", "done"}


def _is_legal_aid(row: Mapping[str, Any]) -> bool:
    text = " ".join(
        _text(row.get(key))
        for key in (
            "case_category",
            "case_reason",
            "case_type",
            "laf_case_no",
            "application_no",
            "legal_aid_status",
        )
    )
    return "法律扶助案件" in text or "法律扶助" in text or "法扶" in text or bool(
        re.search(r"\d{6,8}-[A-Z]-\d{3}", text)
    )


def _effective_status(row: Mapping[str, Any]) -> str:
    laf_status = _text(row.get("legal_aid_status"))
    status = _text(row.get("status"))
    if laf_status in _FINAL_LAF_STATUSES or _is_archive_path(row.get("folder_path")) or _is_closed_status(status):
        return "已結案"
    if laf_status in _CLOSING_LAF_STATUSES:
        return laf_status
    if _is_legal_aid(row):
        return "進行中" if laf_status in {"未結案", "未結案/進行中", "進行中"} else laf_status or "未開辦"
    if status:
        return "進行中" if status in {"未結案", "未結案/進行中"} else status
    return "進行中"


def _display_row(row: Mapping[str, Any], lawyer_resolver: LawyerResolver) -> dict[str, Any]:
    output = dict(row)
    if _is_template(output):
        output.update(
            {
                "case_category": _TEMPLATE_DISPLAY,
                "case_type": _TEMPLATE_DISPLAY,
                "status": _TEMPLATE_DISPLAY,
                "is_template_case": True,
                "lawyer": "",
                "effective_status": _TEMPLATE_DISPLAY,
                "status_display": _TEMPLATE_DISPLAY,
                "case_type_display": _TEMPLATE_DISPLAY,
                "case_reason_display": output.get("case_reason") or _TEMPLATE_DISPLAY,
            }
        )
        return output
    lawyer = _text(output.get("lawyer"))
    if not lawyer or lawyer in _DEMO_LAWYERS:
        lawyer = _text(
            lawyer_resolver(
                lawyer,
                _text(output.get("case_type")),
                _text(output.get("case_reason")),
                _text(output.get("case_category")),
            )
        )
    effective = _effective_status(output)
    output["lawyer"] = lawyer
    output["effective_status"] = effective
    output["status_display"] = effective
    raw_type = _text(output.get("case_type"))
    reason = _text(output.get("case_reason"))
    consultant = "顧問" in " ".join(
        _text(output.get(key)) for key in ("case_reason", "case_stage", "notes", "folder_path")
    )
    if raw_type == "消費者債務清理":
        output["case_type_display"] = raw_type
    elif raw_type == "民事" and consultant:
        output["case_type_display"] = "民事｜法律顧問"
    else:
        output["case_type_display"] = raw_type
    output["case_reason_display"] = reason
    return output


def _default_lawyer_resolver(_current: str, _case_type: str, _reason: str, _category: str) -> str:
    return ""


class OscCasesService:
    """Application service whose create/upsert operation is one transaction."""

    def __init__(
        self,
        store: CaseStore,
        *,
        id_factory: Callable[[], str],
        year_provider: Callable[[], int] = lambda: datetime.now().year,
        path_canonicalizer: Callable[[str], str] = lambda value: value,
        lawyer_resolver: LawyerResolver = _default_lawyer_resolver,
        post_persist: PostPersistHook = lambda _tx, _result, _payload: None,
        side_effects_enabled: bool = False,
    ) -> None:
        self.store = store
        self.id_factory = id_factory
        self.year_provider = year_provider
        self.path_canonicalizer = path_canonicalizer
        self.lawyer_resolver = lawyer_resolver
        self.post_persist = post_persist
        self.side_effects_enabled = side_effects_enabled

    def list_cases(self, query: CaseListQuery) -> list[dict[str, Any]]:
        with self.store.transaction() as transaction:
            rows = transaction.list_cases(query)
        return [_display_row(row, self.lawyer_resolver) for row in rows]

    def create_case(self, payload: Mapping[str, Any]) -> CreateResult:
        if not isinstance(payload, Mapping):
            raise RequestValidationError("JSON object required")
        raw_auto_folder = payload.get("auto_create_folder")
        auto_create_folder = raw_auto_folder is not None and _text(raw_auto_folder).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if auto_create_folder and not self.side_effects_enabled:
            raise RequestValidationError(
                "auto_create_folder is not implemented by the native V3 handler", status=501
            )
        client_name = _text(payload.get("client_name") or payload.get("name") or payload.get("client"))
        if not client_name:
            raise RequestValidationError("client_name required")
        row_id = _text(payload.get("id")) or _text(self.id_factory())
        if not row_id:
            raise RequestValidationError("generated id is empty", status=500)
        case_number = _text(
            payload.get("case_number") or payload.get("case_no") or payload.get("caseNumber")
        )
        template = _is_template(
            {
                "client_name": client_name,
                "case_number": case_number,
                "folder_path": payload.get("folder_path"),
            }
        )
        case_category = _category(payload.get("case_category") or payload.get("category"))
        if template:
            case_category = _TEMPLATE_DISPLAY
        case_type = _text(payload.get("case_type") or payload.get("type")) or None
        case_reason = _clean_reason(payload.get("case_reason")) or None
        status = _text(payload.get("status")) or "進行中"
        archive_requested = requires_closed_storage(
            {"status": status, "legal_aid_status": payload.get("legal_aid_status")}
        )
        if archive_requested and not self.side_effects_enabled:
            raise RequestValidationError(
                "closed-case archive side effects are not implemented by the native V3 handler",
                status=501,
            )
        if template:
            case_type = _TEMPLATE_DISPLAY
            case_reason = _TEMPLATE_DISPLAY
            status = _TEMPLATE_DISPLAY
        laf_number = _text(
            payload.get("laf_case_no")
            or payload.get("legal_aid_number")
            or payload.get("application_no")
        )
        court_case_number = _text(
            payload.get("court_case_no") or payload.get("court_case_number")
        )
        lawyer = _text(
            payload.get("lawyer")
            or payload.get("case_lawyer")
            or payload.get("assigned_lawyer")
            or payload.get("responsible_lawyer")
        )
        if lawyer in _DEMO_LAWYERS:
            lawyer = ""
        if not lawyer and not template:
            lawyer = _text(
                self.lawyer_resolver(
                    lawyer, _text(case_type), _text(case_reason), case_category
                )
            )
        values: dict[str, Any] = {
            "id": row_id,
            "case_number": case_number,
            "client_name": client_name,
            "client_phone": _text(payload.get("client_phone")) or None,
            "client_email": _text(payload.get("client_email")) or None,
            "client_id_number": _text(payload.get("client_id_number")) or None,
            "case_category": case_category or None,
            "case_type": case_type,
            "case_stage": _text(payload.get("case_stage")) or None,
            "case_reason": case_reason,
            "laf_case_no": laf_number or None,
            "application_no": laf_number or None,
            "court_name": _text(payload.get("court_name") or payload.get("court")) or None,
            "court_case_no": court_case_number or None,
            "court_case_number": court_case_number or None,
            "court_division": _text(payload.get("court_division") or payload.get("division")) or None,
            "lawyer": lawyer or None,
            "status": status,
            "notes": _text(payload.get("notes")) or None,
            "folder_path": self.path_canonicalizer(_text(payload.get("folder_path"))) or None,
        }
        with self.store.transaction() as transaction:
            if not values["case_number"] and not template:
                values["case_number"] = transaction.next_case_number(int(self.year_provider()))
            existing = transaction.find_existing(_text(values["case_number"]), row_id)
            if existing:
                result = self._upsert(transaction, existing, values, payload)
            else:
                rowcount, lastrowid = transaction.insert_case(values)
                result = CreateResult(row_id, _text(values["case_number"]), "insert", rowcount, lastrowid)
            effects = dict(self.post_persist(transaction, result, payload) or {})
            if auto_create_folder and not isinstance(effects.get("folder"), Mapping):
                raise OscCasesError("native folder side effect did not produce a result")
            if archive_requested and not isinstance(effects.get("archive"), Mapping):
                raise OscCasesError("native archive side effect did not produce a result")
            return replace(result, effects=effects)

    def _upsert(
        self,
        transaction: CaseTransaction,
        existing: Mapping[str, Any],
        values: Mapping[str, Any],
        payload: Mapping[str, Any],
    ) -> CreateResult:
        target_id = _text(existing.get("id"))
        target_status = _text(existing.get("status"))
        target_laf_status = _text(existing.get("legal_aid_status"))
        final_closed = (
            target_laf_status in _FINAL_LAF_STATUSES
            or _is_closed_status(target_status)
            or _is_archive_path(existing.get("folder_path"))
        )
        closing = target_laf_status in _CLOSING_LAF_STATUSES
        if final_closed and not self.side_effects_enabled:
            raise RequestValidationError(
                "closed-case archive side effects are not implemented by the native V3 handler",
                status=501,
            )
        update = {
            key: value
            for key, value in values.items()
            if key not in {"id", "status", "folder_path", "lawyer"}
        }
        incoming_lawyer = _text(values.get("lawyer"))
        if incoming_lawyer:
            update["lawyer"] = incoming_lawyer
        if closing and _text(values.get("status")).lower() in _OPEN_STATUSES:
            update["status"] = "結案中"
        elif "status" in payload and not bool(int(existing.get("manual_status_lock") or 0)):
            update["status"] = values.get("status")
        incoming_folder = _text(values.get("folder_path"))
        if incoming_folder and not (
            (final_closed or closing) and not _is_archive_path(incoming_folder)
        ):
            update["folder_path"] = incoming_folder
        rowcount = transaction.update_case(target_id, update)
        return CreateResult(target_id, _text(values.get("case_number")), "upsert", rowcount, None)


def _final_closed_sql(*, dialect: str = "sqlite") -> str:
    """Return the V2 final-closed predicate with a dialect-safe path literal."""

    if dialect not in {"sqlite", "mariadb"}:
        raise ValueError(f"unsupported SQL dialect: {dialect}")
    status = "COALESCE(status, '')"
    laf = "COALESCE(legal_aid_status, '')"
    path_separator = "\\\\" if dialect == "mariadb" else "\\"
    folder = f"REPLACE(COALESCE(folder_path, ''), '{path_separator}', '/')"
    laf_closed = f"{laf} = '已結案'"
    case_closed = f"({status} LIKE '%已結案%' OR LOWER({status}) IN ('closed', 'close', 'done'))"
    archive_prefix = _CANONICAL_ARCHIVE_DRIVE_PREFIX.replace("'", "''")
    folder_closed = f"({folder} LIKE '{archive_prefix}%' OR {folder} LIKE '%/03_工作資料/10_結案/%' OR {folder} LIKE '%/10_結案/%')"
    laf_not_closing = f"{laf} NOT IN ('已結案，待報結', '已結案，待送出')"
    status_not_closing = f"{status} NOT LIKE '%結案中%' AND {status} NOT LIKE '%待報結%' AND {status} NOT LIKE '%待送出%'"
    return f"({laf_closed} OR {folder_closed} OR ({case_closed} AND {laf_not_closing} AND {status_not_closing}))"


def _status_scope_sql(scope: str, *, dialect: str = "sqlite") -> str:
    status = "COALESCE(status, '')"
    laf = "COALESCE(legal_aid_status, '')"
    pending_report = f"({laf} = '已結案，待報結' OR {status} LIKE '%待報結%')"
    pending_submit = f"({laf} = '已結案，待送出' OR {status} LIKE '%待送出%')"
    laf_closing = f"{laf} IN ('已結案，待報結', '已結案，待送出')"
    case_closing = f"({status} LIKE '%結案中%' OR {status} LIKE '%待報結%' OR {status} LIKE '%待送出%')"
    final = _final_closed_sql(dialect=dialect)
    closing = f"({laf_closing} OR {case_closing})"
    active = (
        f"({status} = '' OR {status} LIKE '%進行%' "
        f"OR {status} IN ('未結案', '未結案/進行中') "
        f"OR {laf} IN ('未結案', '未結案/進行中') "
        f"OR LOWER({status}) IN ('active', 'open', 'ongoing', 'pending'))"
    )
    if scope in {"working", "default", "open"}:
        return f"(NOT {final})"
    if scope in {"active", "ongoing"}:
        return f"({active} AND NOT {final} AND NOT {closing})"
    if scope in {"pending_report", "report_pending"}:
        return pending_report
    if scope in {"pending_submit", "submit_pending"}:
        return pending_submit
    if scope in {"closing", "closing_case"}:
        return closing
    if scope in {"closed", "archived"}:
        return final
    return ""


class SQLiteCaseStore:
    """Explicit SQLite adapter used for native tests and local embedding."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.connection.isolation_level = None

    @contextmanager
    def transaction(self) -> Iterator["SQLiteCaseTransaction"]:
        transaction = SQLiteCaseTransaction(self.connection)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield transaction
        except BaseException:
            self.connection.rollback()
            transaction.rollback_external()
            raise
        else:
            try:
                self.connection.commit()
            except BaseException:
                self.connection.rollback()
                transaction.rollback_external()
                raise
            transaction.clear_rollback_hooks()


class SQLiteCaseTransaction:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._rollback_hooks: list[Callable[[], None]] = []

    def register_rollback_hook(self, hook: Callable[[], None]) -> None:
        if not callable(hook):
            raise TypeError("rollback hook must be callable")
        self._rollback_hooks.append(hook)

    def rollback_external(self) -> None:
        errors: list[BaseException] = []
        while self._rollback_hooks:
            hook = self._rollback_hooks.pop()
            try:
                hook()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise OscCasesError(f"external side-effect rollback failed: {errors[0]}")

    def clear_rollback_hooks(self) -> None:
        self._rollback_hooks.clear()

    def list_cases(self, query: CaseListQuery) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if query.q:
            where.append("(" + " OR ".join(f"{column} LIKE ?" for column in ("case_number", "client_name", "court_name", "court_case_no", "laf_case_no", "application_no")) + ")")
            params.extend([f"%{query.q}%"] * 6)
        if query.case_type and query.case_type not in {"全部", "all", "ALL"}:
            if query.case_type == "消費者債務清理":
                where.append("(case_reason LIKE ? OR case_type = ?)")
                params.extend(["%消費者債務清理%", query.case_type])
            elif query.case_type == "法律顧問":
                where.append("(case_type = ? OR (case_type = ? AND (case_reason LIKE ? OR case_stage LIKE ? OR notes LIKE ? OR folder_path LIKE ?)))")
                params.extend([query.case_type, "民事", "%顧問%", "%顧問%", "%顧問%", "%顧問%"])
            else:
                where.append("case_type = ?")
                params.append(query.case_type)
        if query.case_kind and query.case_kind not in {"全部", "all", "ALL"}:
            normalized = {"一般": "一般案件", "法扶": "法律扶助案件", "指定辯護": "指定辯護案件", "無償": "無償案件"}.get(query.case_kind, query.case_kind)
            if normalized == "消費者債務清理":
                where.append("(case_reason LIKE ? OR case_type = ?)")
                params.extend(["%消費者債務清理%", normalized])
            elif normalized == "法律扶助案件":
                where.append("(case_category = ? OR case_reason LIKE ? OR case_reason LIKE ?)")
                params.extend([normalized, "%法扶%", "%法律扶助%"])
            else:
                where.append("case_category = ?")
                params.append(normalized)
        status_clause = _status_scope_sql(query.status_scope, dialect="sqlite")
        if status_clause:
            where.append(status_clause)
        sql = f"SELECT {','.join(_SELECT_COLUMNS)} FROM cases"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if query.status_scope in {"", "all", "ALL"}:
            sql += f" ORDER BY CASE WHEN {_final_closed_sql()} THEN 1 ELSE 0 END, updated_at DESC, created_date DESC LIMIT ?"
        else:
            sql += " ORDER BY updated_at DESC, created_date DESC LIMIT ?"
        params.append(query.limit)
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def next_case_number(self, year: int) -> str:
        maximum = 0
        for row in self.connection.execute(
            "SELECT case_number FROM cases WHERE case_number LIKE ?", (f"{year}-%",)
        ):
            match = _CASE_NUMBER.match(_text(row["case_number"]))
            if match and int(match.group("year")) == year:
                maximum = max(maximum, int(match.group("sequence")))
        return f"{year}-{maximum + 1:04d}"

    def find_existing(self, case_number: str, row_id: str) -> dict[str, Any] | None:
        row = None
        if case_number:
            row = self.connection.execute(
                "SELECT * FROM cases WHERE case_number = ? LIMIT 1", (case_number,)
            ).fetchone()
        if row is None and row_id:
            row = self.connection.execute(
                "SELECT * FROM cases WHERE id = ? LIMIT 1", (row_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def insert_case(self, values: Mapping[str, Any]) -> tuple[int, int | None]:
        cursor = self.connection.execute(
            f"INSERT INTO cases ({','.join(_WRITE_COLUMNS)}) VALUES ({','.join('?' for _ in _WRITE_COLUMNS)})",
            tuple(values.get(column) for column in _WRITE_COLUMNS),
        )
        return cursor.rowcount, cursor.lastrowid

    def update_case(self, row_id: str, values: Mapping[str, Any]) -> int:
        allowed = [column for column in _WRITE_COLUMNS if column != "id"]
        columns = [column for column in allowed if column in values]
        if not columns:
            return 0
        cursor = self.connection.execute(
            f"UPDATE cases SET {','.join(f'{column} = ?' for column in columns)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            tuple(values[column] for column in columns) + (row_id,),
        )
        return cursor.rowcount


class MariaDBCaseStore:
    """Production DB-API adapter with one connection per service transaction.

    ``connection_factory`` may return either a connection or the
    ``(connection, config)`` pair returned by ``api.osc.utils._osc_web_connect``.
    No connection is created until :meth:`transaction` is entered.
    """

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        lock_timeout_seconds: int = 5,
    ) -> None:
        self.connection_factory = connection_factory
        self.lock_timeout_seconds = lock_timeout_seconds

    @contextmanager
    def transaction(self) -> Iterator["MariaDBCaseTransaction"]:
        supplied = self.connection_factory()
        connection = supplied[0] if isinstance(supplied, tuple) else supplied
        transaction = MariaDBCaseTransaction(
            connection, lock_timeout_seconds=self.lock_timeout_seconds
        )
        try:
            starter = getattr(connection, "start_transaction", None)
            if callable(starter):
                starter()
            else:
                transaction._execute("START TRANSACTION")
            yield transaction
        except BaseException:
            connection.rollback()
            transaction.rollback_external()
            raise
        else:
            try:
                connection.commit()
            except BaseException:
                connection.rollback()
                transaction.rollback_external()
                raise
            transaction.clear_rollback_hooks()
        finally:
            transaction.release_locks()
            connection.close()


class MariaDBCaseTransaction:
    """MariaDB statements for one native OSC logical transaction."""

    def __init__(self, connection: Any, *, lock_timeout_seconds: int = 5) -> None:
        self.connection = connection
        self.lock_timeout_seconds = lock_timeout_seconds
        self._locks: list[str] = []
        self._rollback_hooks: list[Callable[[], None]] = []

    def register_rollback_hook(self, hook: Callable[[], None]) -> None:
        if not callable(hook):
            raise TypeError("rollback hook must be callable")
        self._rollback_hooks.append(hook)

    def rollback_external(self) -> None:
        errors: list[BaseException] = []
        while self._rollback_hooks:
            hook = self._rollback_hooks.pop()
            try:
                hook()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise OscCasesError(f"external side-effect rollback failed: {errors[0]}")

    def clear_rollback_hooks(self) -> None:
        self._rollback_hooks.clear()

    def _cursor(self) -> Any:
        try:
            return self.connection.cursor(dictionary=True)
        except TypeError:
            return self.connection.cursor()

    def _execute(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
        *,
        fetch: str = "none",
    ) -> Any:
        cursor = self._cursor()
        try:
            cursor.execute(sql, params)
            if fetch == "one":
                row = cursor.fetchone()
                return _json_row(row) if row is not None else None
            if fetch == "all":
                return [_json_row(row) for row in (cursor.fetchall() or [])]
            return cursor.rowcount, getattr(cursor, "lastrowid", None)
        finally:
            cursor.close()

    def _acquire_lock(self, identity: str) -> None:
        lock_name = f"magi:v3:osc-cases:{identity}"[:64]
        if lock_name in self._locks:
            return
        row = self._execute(
            "SELECT GET_LOCK(%s, %s) AS acquired",
            (lock_name, self.lock_timeout_seconds),
            fetch="one",
        )
        if not row or int(row.get("acquired") or 0) != 1:
            raise OscCasesError(f"could not acquire OSC case transaction lock: {identity}")
        self._locks.append(lock_name)

    def release_locks(self) -> None:
        while self._locks:
            lock_name = self._locks.pop()
            try:
                self._execute("SELECT RELEASE_LOCK(%s) AS released", (lock_name,), fetch="one")
            except Exception:
                # Closing a MariaDB connection also releases its advisory locks.
                continue

    def list_cases(self, query: CaseListQuery) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if query.q:
            where.append(
                "("
                + " OR ".join(
                    f"{column} LIKE %s"
                    for column in (
                        "case_number",
                        "client_name",
                        "court_name",
                        "court_case_no",
                        "laf_case_no",
                        "application_no",
                    )
                )
                + ")"
            )
            params.extend([f"%{query.q}%"] * 6)
        if query.case_type and query.case_type not in {"全部", "all", "ALL"}:
            if query.case_type == "消費者債務清理":
                where.append("(case_reason LIKE %s OR case_type = %s)")
                params.extend(["%消費者債務清理%", query.case_type])
            elif query.case_type == "法律顧問":
                where.append(
                    "(case_type = %s OR (case_type = %s AND (case_reason LIKE %s OR "
                    "case_stage LIKE %s OR notes LIKE %s OR folder_path LIKE %s)))"
                )
                params.extend(
                    [query.case_type, "民事", "%顧問%", "%顧問%", "%顧問%", "%顧問%"]
                )
            else:
                where.append("case_type = %s")
                params.append(query.case_type)
        if query.case_kind and query.case_kind not in {"全部", "all", "ALL"}:
            normalized = {
                "一般": "一般案件",
                "法扶": "法律扶助案件",
                "指定辯護": "指定辯護案件",
                "無償": "無償案件",
            }.get(query.case_kind, query.case_kind)
            if normalized == "消費者債務清理":
                where.append("(case_reason LIKE %s OR case_type = %s)")
                params.extend(["%消費者債務清理%", normalized])
            elif normalized == "法律扶助案件":
                where.append(
                    "(case_category = %s OR case_reason LIKE %s OR case_reason LIKE %s)"
                )
                params.extend([normalized, "%法扶%", "%法律扶助%"])
            else:
                where.append("case_category = %s")
                params.append(normalized)
        status_clause = _status_scope_sql(query.status_scope, dialect="mariadb")
        if status_clause:
            where.append(status_clause)
        sql = f"SELECT {','.join(_SELECT_COLUMNS)} FROM cases"
        if where:
            sql += " WHERE " + " AND ".join(where)
        if query.status_scope in {"", "all", "ALL"}:
            sql += (
                f" ORDER BY CASE WHEN {_final_closed_sql(dialect='mariadb')} THEN 1 ELSE 0 END, "
                "updated_at DESC, created_date DESC LIMIT %s"
            )
        else:
            sql += " ORDER BY updated_at DESC, created_date DESC LIMIT %s"
        params.append(query.limit)
        return self._execute(sql, tuple(params), fetch="all")

    def next_case_number(self, year: int) -> str:
        self._acquire_lock(f"year:{year}")
        rows = self._execute(
            "SELECT case_number FROM cases WHERE case_number LIKE %s FOR UPDATE",
            (f"{year}-%",),
            fetch="all",
        )
        maximum = 0
        for row in rows:
            match = _CASE_NUMBER.match(_text(row.get("case_number")))
            if match and int(match.group("year")) == year:
                maximum = max(maximum, int(match.group("sequence")))
        return f"{year}-{maximum + 1:04d}"

    def find_existing(self, case_number: str, row_id: str) -> dict[str, Any] | None:
        if case_number:
            self._acquire_lock(f"case:{case_number}")
            row = self._execute(
                "SELECT * FROM cases WHERE case_number = %s LIMIT 1 FOR UPDATE",
                (case_number,),
                fetch="one",
            )
            if row:
                return row
        if row_id:
            self._acquire_lock(f"id:{row_id}")
            return self._execute(
                "SELECT * FROM cases WHERE id = %s LIMIT 1 FOR UPDATE",
                (row_id,),
                fetch="one",
            )
        return None

    def insert_case(self, values: Mapping[str, Any]) -> tuple[int, int | None]:
        return self._execute(
            f"INSERT INTO cases ({','.join(_WRITE_COLUMNS)}) VALUES ({','.join('%s' for _ in _WRITE_COLUMNS)})",
            tuple(values.get(column) for column in _WRITE_COLUMNS),
        )

    def update_case(self, row_id: str, values: Mapping[str, Any]) -> int:
        allowed = [column for column in _WRITE_COLUMNS if column != "id"]
        columns = [column for column in allowed if column in values]
        if not columns:
            return 0
        rowcount, _ = self._execute(
            f"UPDATE cases SET {','.join(f'{column} = %s' for column in columns)}, "
            "updated_at = NOW() WHERE id = %s",
            tuple(values[column] for column in columns) + (row_id,),
        )
        return int(rowcount)


def initialize_sqlite_cases_schema(connection: sqlite3.Connection) -> None:
    """Explicitly create the portable subset needed by this native handler."""

    connection.executescript(
        """
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
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            created_date TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_cases_case_number ON cases(case_number);
        """
    )


class OscCasesApplication:
    """Small native WSGI facade for exactly ``/api/osc/cases``."""

    def __init__(
        self,
        service: OscCasesService,
        *,
        authorize: Callable[[Mapping[str, Any]], bool],
        csrf: CsrfProtection,
        response_security_headers: ResponseSecurityHeaders = lambda _environ: {},
        max_body_bytes: int = 1_048_576,
        fallback: WSGIApp | None = None,
    ) -> None:
        self.service = service
        self.authorize = authorize
        self.csrf = csrf
        self.response_security_headers = response_security_headers
        self.max_body_bytes = max_body_bytes
        self.fallback = fallback

    def __call__(self, environ: dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        if _text(environ.get("PATH_INFO")) != "/api/osc/cases" and self.fallback is not None:
            return self.fallback(environ, start_response)
        status, payload = self.response(environ)
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body)))]
        if _text(environ.get("REQUEST_METHOD")).upper() in {"GET", "HEAD"}:
            cookie = self.csrf.safe_response_cookie(environ)
            if cookie:
                headers.append(("Set-Cookie", cookie))
        existing_headers = {name.lower() for name, _value in headers}
        for name, value in self.response_security_headers(environ).items():
            if name.lower() not in existing_headers:
                headers.append((name, value))
                existing_headers.add(name.lower())
        start_response(f"{status} {_status_reason(status)}", headers)
        return [b"" if _text(environ.get("REQUEST_METHOD")).upper() == "HEAD" else body]

    def response(self, environ: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if _text(environ.get("PATH_INFO")) != "/api/osc/cases":
            return 404, {"ok": False, "error": "not found"}
        method = _text(environ.get("REQUEST_METHOD")).upper()
        try:
            if method == "POST":
                csrf_valid, csrf_reason = self.csrf.validate(environ)
                if not csrf_valid:
                    return 403, {
                        "error": "forbidden: invalid CSRF token",
                        "code": "csrf_validation_failed",
                        "reason": csrf_reason,
                    }
            if not self.authorize(environ):
                return 401, {"ok": False, "error": "authentication required"}
            if method in {"GET", "HEAD"}:
                query = _parse_list_query(_wsgi_query_text(environ.get("QUERY_STRING")))
                return 200, {"ok": True, "items": self.service.list_cases(query)}
            if method == "POST":
                payload = self._json_body(environ)
                result = self.service.create_case(payload)
                return 200, {
                    "ok": True,
                    "result": {"rowcount": result.rowcount, "lastrowid": result.lastrowid},
                    "id": result.row_id,
                    "case_number": result.case_number,
                    "mode": result.mode,
                    **dict(result.effects),
                }
            return 405, {"ok": False, "error": "method not allowed"}
        except RequestValidationError as exc:
            return exc.status, {"ok": False, "error": str(exc)}
        except Exception:
            return 500, {"ok": False, "error": "internal_error"}

    def _json_body(self, environ: Mapping[str, Any]) -> Mapping[str, Any]:
        content_type = _text(environ.get("CONTENT_TYPE")).lower()
        if not content_type.startswith("application/json"):
            raise RequestValidationError("application/json required", status=415)
        raw_length = _text(environ.get("CONTENT_LENGTH"))
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise RequestValidationError("invalid Content-Length") from exc
        if length < 0 or length > self.max_body_bytes:
            raise RequestValidationError("request body too large", status=413)
        stream = environ.get("wsgi.input")
        body = stream.read(length) if stream is not None and length else b""
        try:
            payload = json.loads(body or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestValidationError("invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RequestValidationError("JSON object required")
        return payload


def _parse_list_query(query_string: str) -> CaseListQuery:
    values = parse_qs(query_string, keep_blank_values=True)

    def first(name: str, default: str = "") -> str:
        return _text((values.get(name) or [default])[0])

    try:
        limit = int(first("limit", "200") or "200")
    except ValueError as exc:
        raise RequestValidationError("limit must be an integer") from exc
    limit = max(1, min(500, limit))
    case_type = first("case_type")
    case_kind = first("case_kind")
    category = first("category")
    if category and category not in {"全部", "all", "ALL"} and not case_type and not case_kind:
        if category in {"刑事", "民事", "法律顧問", "消費者債務清理", "行政", "非訟"}:
            case_type = category
        else:
            case_kind = category
    return CaseListQuery(
        q=first("q"),
        case_type=case_type,
        case_kind=case_kind,
        status_scope=first("status_scope", "all").lower() or "all",
        limit=limit,
    )


def _status_reason(status: int) -> str:
    return {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Content Too Large",
        415: "Unsupported Media Type",
        500: "Internal Server Error",
        501: "Not Implemented",
    }.get(status, "Unknown")

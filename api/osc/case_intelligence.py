"""Read-only OSC case intelligence snapshots.

This module deliberately keeps DB access injectable so tests can use mock rows
and callers can reuse the existing OSC connection helper without adding a new
write path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from api.laf_case_classifier import clean_laf_case_reason
from api.osc.case_folder_schema import (
    canonicalize_case_subfolder_name,
    case_subfolders,
    strip_number_prefix,
)

ExecFn = Callable[..., tuple[Any, Any]]
PathResolver = Callable[[str], str]


def build_case_intelligence_snapshot(
    exec_fn: ExecFn,
    *,
    row_id: str = "",
    case_number: str = "",
    query: str = "",
    limit: int = 20,
    document_limit: int = 8,
    calendar_limit: int = 8,
    folder_resolver: PathResolver | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a read-only JSON-ready case status snapshot.

    The output is intentionally compact and stable for agents: case identity,
    folder topology, recent documents, calendar/todo references, and a small
    graph projection derived from the same records.
    """

    limit = _bounded_int(limit, default=20, minimum=1, maximum=100)
    document_limit = _bounded_int(document_limit, default=8, minimum=1, maximum=50)
    calendar_limit = _bounded_int(calendar_limit, default=8, minimum=1, maximum=50)
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    warnings: list[str] = []

    case_rows, case_warning = _fetch_cases(
        exec_fn,
        row_id=_clean_text(row_id),
        case_number=_clean_text(case_number),
        query=_clean_text(query),
        limit=limit,
    )
    if case_warning:
        return {
            "ok": False,
            "generated_at": generated_at,
            "query": {"row_id": row_id, "case_number": case_number, "q": query, "limit": limit},
            "cases": [],
            "graph": {"nodes": [], "edges": []},
            "sources": ["cases"],
            "warnings": [case_warning],
        }

    items: list[dict[str, Any]] = []
    for row in case_rows:
        case = _case_snapshot_base(row, folder_resolver=folder_resolver)
        docs, doc_warnings = _fetch_recent_documents(exec_fn, row, limit=document_limit)
        refs, ref_warnings = _fetch_calendar_refs(exec_fn, row, limit=calendar_limit)
        case["recent_docs"] = docs
        case["calendar_refs"] = refs
        case["counts"] = {
            "known_subfolders": len(case.get("known_subfolders") or []),
            "recent_docs": len(docs),
            "calendar_refs": len(refs),
        }
        items.append(case)
        warnings.extend(doc_warnings)
        warnings.extend(ref_warnings)

    return {
        "ok": True,
        "generated_at": generated_at,
        "query": {"row_id": row_id, "case_number": case_number, "q": query, "limit": limit},
        "cases": items,
        "graph": _build_graph(items),
        "sources": [
            "cases",
            "document_index",
            "case_documents",
            "case_todos",
            "calendar_events",
            "case_folder",
        ],
        "warnings": list(dict.fromkeys(warnings)),
    }


def _fetch_cases(
    exec_fn: ExecFn,
    *,
    row_id: str,
    case_number: str,
    query: str,
    limit: int,
) -> tuple[list[dict[str, Any]], str]:
    where: list[str] = []
    params: list[Any] = []
    if row_id:
        where.append("id=%s")
        params.append(row_id)
    if case_number:
        where.append(
            """
            (
                case_number=%s
                OR court_case_no=%s
                OR court_case_number=%s
                OR laf_case_no=%s
                OR application_no=%s
            )
            """
        )
        params.extend([case_number, case_number, case_number, case_number, case_number])
    if query:
        like = f"%{query}%"
        where.append(
            """
            (
                case_number LIKE %s
                OR client_name LIKE %s
                OR court_name LIKE %s
                OR court_case_no LIKE %s
                OR court_case_number LIKE %s
                OR laf_case_no LIKE %s
                OR application_no LIKE %s
                OR case_reason LIKE %s
            )
            """
        )
        params.extend([like, like, like, like, like, like, like, like])

    sql = """
        SELECT id, case_number, client_name, case_category, case_type, case_stage,
               case_reason, court_name, court_case_no, court_case_number,
               court_division, laf_case_no, application_no, legal_aid_status,
               status, folder_path, updated_at, created_date
        FROM cases
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY updated_at DESC, created_date DESC LIMIT %s"
    params.append(limit)
    return _select_rows(exec_fn, sql, tuple(params), "cases")


def _fetch_recent_documents(
    exec_fn: ExecFn,
    case_row: dict[str, Any],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    docs: list[dict[str, Any]] = []
    case_number = _clean_text(case_row.get("case_number"))
    case_id = _clean_text(case_row.get("id"))
    folder_path = _clean_text(case_row.get("folder_path"))
    folder_like = f"%{folder_path}%" if folder_path else ""

    di_where: list[str] = []
    di_params: list[Any] = []
    if case_number:
        di_where.append("case_number=%s")
        di_params.append(case_number)
    if folder_like:
        di_where.append("file_path LIKE %s")
        di_params.append(folder_like)
    if di_where:
        rows, warning = _select_rows(
            exec_fn,
            f"""
            SELECT id, case_number, file_name, file_path, subfolder_name, reason, party, modified_date
            FROM document_index
            WHERE {' OR '.join(di_where)}
            ORDER BY modified_date DESC, id DESC
            LIMIT %s
            """,
            tuple(di_params + [limit]),
            "document_index",
        )
        warnings.extend(_warning_list(warning))
        docs.extend(_doc_from_document_index(row) for row in rows)

    cd_where: list[str] = []
    cd_params: list[Any] = []
    if case_id:
        cd_where.append("cd.case_id=%s")
        cd_params.append(case_id)
    if case_number:
        cd_where.append("cd.case_id=%s")
        cd_params.append(case_number)
        cd_where.append("c.case_number=%s")
        cd_params.append(case_number)
    if folder_like:
        cd_where.append("cd.file_path LIKE %s")
        cd_params.append(folder_like)
    if cd_where:
        rows, warning = _select_rows(
            exec_fn,
            f"""
            SELECT cd.id, cd.case_id, c.case_number AS case_number_ref,
                   cd.document_type, cd.file_name, cd.file_path, cd.description, cd.upload_date
            FROM case_documents cd
            LEFT JOIN cases c ON c.id = cd.case_id
            WHERE {' OR '.join(cd_where)}
            ORDER BY cd.upload_date DESC, cd.id DESC
            LIMIT %s
            """,
            tuple(cd_params + [limit]),
            "case_documents",
        )
        warnings.extend(_warning_list(warning))
        docs.extend(_doc_from_case_documents(row) for row in rows)

    docs = _dedupe_by(docs, ("file_path", "file_name", "source"))
    docs.sort(key=lambda item: _sort_text(item.get("timestamp")), reverse=True)
    return docs[:limit], warnings


def _fetch_calendar_refs(
    exec_fn: ExecFn,
    case_row: dict[str, Any],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    refs: list[dict[str, Any]] = []
    case_number = _clean_text(case_row.get("case_number"))
    client_name = _clean_text(case_row.get("client_name"))
    case_like = f"%{case_number}%" if case_number else ""
    client_like = f"%{client_name}%" if client_name else ""

    todo_where: list[str] = []
    todo_params: list[Any] = []
    if case_number:
        todo_where.extend(["case_number=%s", "description LIKE %s", "source_file LIKE %s"])
        todo_params.extend([case_number, case_like, case_like])
    if client_name:
        todo_where.extend(["client_name LIKE %s", "description LIKE %s"])
        todo_params.extend([client_like, client_like])
    if todo_where:
        rows, warning = _select_rows(
            exec_fn,
            f"""
            SELECT id, case_number, client_name, todo_type, todo_date, todo_time,
                   description, status, source_file, created_date, completed_date
            FROM case_todos
            WHERE {' OR '.join(todo_where)}
            ORDER BY todo_date DESC, id DESC
            LIMIT %s
            """,
            tuple(todo_params + [limit]),
            "case_todos",
        )
        warnings.extend(_warning_list(warning))
        refs.extend(_ref_from_todo(row) for row in rows)

    event_where: list[str] = []
    event_params: list[Any] = []
    if case_number:
        event_where.extend(
            [
                "case_number=%s",
                "title LIKE %s",
                "summary LIKE %s",
                "description LIKE %s",
            ]
        )
        event_params.extend([case_number, case_like, case_like, case_like])
    if client_name:
        event_where.extend(["title LIKE %s", "summary LIKE %s", "description LIKE %s", "location LIKE %s"])
        event_params.extend([client_like, client_like, client_like, client_like])
    if event_where:
        rows, warning = _select_rows(
            exec_fn,
            f"""
            SELECT id, event_id, title, summary, description, start_date, end_date,
                   location, is_all_day, case_number
            FROM calendar_events
            WHERE {' OR '.join(event_where)}
            ORDER BY start_date DESC, id DESC
            LIMIT %s
            """,
            tuple(event_params + [limit]),
            "calendar_events",
        )
        warnings.extend(_warning_list(warning))
        refs.extend(_ref_from_calendar_event(row) for row in rows)

    refs = _dedupe_by(refs, ("source", "id", "title", "date"))
    refs.sort(key=lambda item: _sort_text(item.get("date")), reverse=True)
    return refs[:limit], warnings


def _case_snapshot_base(
    row: dict[str, Any],
    *,
    folder_resolver: PathResolver | None,
) -> dict[str, Any]:
    case_number = _clean_text(row.get("case_number"))
    court_case_no = _clean_text(row.get("court_case_no")) or _clean_text(row.get("court_case_number"))
    case_reason = clean_laf_case_reason(_clean_text(row.get("case_reason")))
    folder_path = _clean_text(row.get("folder_path"))
    key = _clean_text(row.get("id")) or case_number or court_case_no or _clean_text(row.get("client_name"))
    local_folder_path = _resolve_folder_path(folder_path, folder_resolver)

    return {
        "key": key,
        "id": _clean_text(row.get("id")),
        "name": _clean_text(row.get("client_name")),
        "case_number": case_number,
        "case_type": _clean_text(row.get("case_type")),
        "case_stage": _clean_text(row.get("case_stage")),
        "case_reason": case_reason,
        "case_category": _clean_text(row.get("case_category")),
        "status": _clean_text(row.get("status")),
        "legal_aid_status": _clean_text(row.get("legal_aid_status")),
        "laf_case_no": _clean_text(row.get("laf_case_no")) or _clean_text(row.get("application_no")),
        "court": {
            "name": _clean_text(row.get("court_name")),
            "case_number": court_case_no,
            "division": _clean_text(row.get("court_division")),
        },
        "folder_path": folder_path,
        "local_folder_path": local_folder_path,
        "known_subfolders": _scan_known_subfolders(
            folder_path=folder_path,
            local_folder_path=local_folder_path,
            case_category=_clean_text(row.get("case_category")),
        ),
        "updated_at": _json_value(row.get("updated_at")),
        "created_date": _json_value(row.get("created_date")),
    }


def _scan_known_subfolders(*, folder_path: str, local_folder_path: str, case_category: str) -> list[dict[str, Any]]:
    expected = list(case_subfolders(case_category or "一般案件"))
    known: dict[str, dict[str, Any]] = {}
    for name in expected:
        canonical = canonicalize_case_subfolder_name(name)
        known[canonical] = {
            "name": canonical,
            "actual_name": "",
            "label": strip_number_prefix(canonical),
            "path": _display_join(folder_path, canonical),
            "local_path": _display_join(local_folder_path, canonical) if local_folder_path else "",
            "exists": False,
            "source": "schema",
        }

    base = Path(local_folder_path) if local_folder_path else None
    if base and base.is_dir():
        try:
            children = sorted(
                (p for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")),
                key=lambda p: p.name,
            )
        except OSError:
            children = []
        for child in children:
            canonical = canonicalize_case_subfolder_name(child.name)
            item = known.get(canonical)
            if item:
                item["actual_name"] = child.name
                item["path"] = _display_join(folder_path, child.name)
                item["local_path"] = str(child)
                item["exists"] = True
                item["source"] = "schema+filesystem"
            else:
                known[canonical] = {
                    "name": canonical,
                    "actual_name": child.name,
                    "label": strip_number_prefix(canonical),
                    "path": _display_join(folder_path, child.name),
                    "local_path": str(child),
                    "exists": True,
                    "source": "filesystem",
                }

    return list(known.values())


def _doc_from_document_index(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"di-{_clean_text(row.get('id'))}",
        "source": "document_index",
        "case_number": _clean_text(row.get("case_number")),
        "file_name": _clean_text(row.get("file_name")),
        "file_path": _clean_text(row.get("file_path")),
        "subfolder_name": _clean_text(row.get("subfolder_name")),
        "reason": _clean_text(row.get("reason")),
        "party": _clean_text(row.get("party")),
        "timestamp": _json_value(row.get("modified_date")),
    }


def _doc_from_case_documents(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"cd-{_clean_text(row.get('id'))}",
        "source": "case_documents",
        "case_number": _clean_text(row.get("case_number_ref")) or _clean_text(row.get("case_id")),
        "file_name": _clean_text(row.get("file_name")),
        "file_path": _clean_text(row.get("file_path")),
        "subfolder_name": _clean_text(row.get("document_type")),
        "reason": _clean_text(row.get("description")),
        "party": "",
        "timestamp": _json_value(row.get("upload_date")),
    }


def _ref_from_todo(row: dict[str, Any]) -> dict[str, Any]:
    date_text = _json_value(row.get("todo_date"))
    time_text = _json_value(row.get("todo_time"))
    return {
        "id": f"todo-{_clean_text(row.get('id'))}",
        "source": "case_todos",
        "case_number": _clean_text(row.get("case_number")),
        "title": _clean_text(row.get("todo_type")),
        "description": _clean_text(row.get("description")),
        "date": " ".join(x for x in [date_text, time_text] if x).strip(),
        "status": _clean_text(row.get("status")),
        "source_file": _clean_text(row.get("source_file")),
        "created_date": _json_value(row.get("created_date")),
        "completed_date": _json_value(row.get("completed_date")),
    }


def _ref_from_calendar_event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"event-{_clean_text(row.get('id')) or _clean_text(row.get('event_id'))}",
        "source": "calendar_events",
        "case_number": _clean_text(row.get("case_number")),
        "title": _clean_text(row.get("title")) or _clean_text(row.get("summary")),
        "description": _clean_text(row.get("description")),
        "date": _json_value(row.get("start_date")),
        "end_date": _json_value(row.get("end_date")),
        "location": _clean_text(row.get("location")),
        "is_all_day": bool(row.get("is_all_day")),
        "event_id": _clean_text(row.get("event_id")),
    }


def _build_graph(cases: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()

    def add_node(node_id: str, node_type: str, label: str, **extra: Any) -> None:
        if not node_id or node_id in seen_nodes:
            return
        seen_nodes.add(node_id)
        nodes.append({"id": node_id, "type": node_type, "label": label, **extra})

    for case in cases:
        case_id = f"case:{case.get('key')}"
        add_node(case_id, "case", case.get("case_number") or case.get("name") or case.get("key") or "")
        for folder in case.get("known_subfolders") or []:
            if not folder.get("exists"):
                continue
            node_id = f"folder:{case.get('key')}:{folder.get('name')}"
            add_node(node_id, "folder", folder.get("label") or folder.get("name") or "")
            edges.append({"from": case_id, "to": node_id, "type": "has_subfolder"})
        for doc in case.get("recent_docs") or []:
            node_id = f"doc:{doc.get('source')}:{doc.get('id') or doc.get('file_path')}"
            add_node(node_id, "document", doc.get("file_name") or doc.get("id") or "")
            edges.append({"from": case_id, "to": node_id, "type": "has_recent_document"})
        for ref in case.get("calendar_refs") or []:
            node_id = f"calendar:{ref.get('source')}:{ref.get('id')}"
            add_node(node_id, "calendar_ref", ref.get("title") or ref.get("date") or ref.get("id") or "")
            edges.append({"from": case_id, "to": node_id, "type": "has_calendar_ref"})

    return {"nodes": nodes, "edges": edges}


def _select_rows(exec_fn: ExecFn, sql: str, params: tuple[Any, ...], source: str) -> tuple[list[dict[str, Any]], str]:
    try:
        rows, _err = exec_fn(sql, params, fetch="all")
    except Exception as exc:
        return [], f"{source}: {type(exc).__name__}: {exc}"
    if rows is None:
        return [], ""
    if isinstance(rows, dict):
        return [rows], ""
    try:
        return [dict(row) for row in rows], ""
    except Exception as exc:
        return [], f"{source}: invalid_rows: {exc}"


def _resolve_folder_path(folder_path: str, folder_resolver: PathResolver | None) -> str:
    if not folder_path:
        return ""
    if folder_resolver is None:
        return folder_path if Path(folder_path).is_dir() else ""
    try:
        return _clean_text(folder_resolver(folder_path))
    except Exception:
        return ""


def _bounded_int(value: int | str | None, *, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value if value is not None else default)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _json_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _sort_text(value: Any) -> str:
    return _json_value(value).replace("/", "-")


def _display_join(base: str, name: str) -> str:
    base = _clean_text(base).rstrip("/\\")
    name = _clean_text(name)
    if not base:
        return name
    separator = "\\" if "\\" in base and "/" not in base else "/"
    return f"{base}{separator}{name}"


def _dedupe_by(items: Iterable[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    seen: set[tuple[str, ...]] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        key = tuple(_clean_text(item.get(field)) for field in fields)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _warning_list(value: str) -> list[str]:
    return [value] if value else []

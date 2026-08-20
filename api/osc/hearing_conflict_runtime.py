"""Database, NAS, notification, and audit adapter for hearing conflicts."""

from __future__ import annotations

import logging
import os
import time
import hashlib
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any, Mapping

from api.osc.hearing_conflicts import (
    ConflictDecision,
    build_leave_request_docx,
    conflict_notification_text,
    find_conflict_decisions,
    leave_request_filename,
    leave_request_payload,
    manual_leave_request_payload,
    normalize_schedule,
    pick_leave_request_conflict,
    resolve_pleading_output_dir,
)
from api.osc.utils import (
    _osc_exec,
    _osc_guess_case_folder,
    _osc_resolve_existing_local_path,
)
from api.saas_schema import tenant_id_from_env


logger = logging.getLogger(__name__)
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="osc-hearing-conflict")
_QUEUE_SLOTS = BoundedSemaphore(value=64)
_INFLIGHT_LOCK = Lock()
_INFLIGHT_KEYS: set[tuple[str, str, str]] = set()
_DOCUMENT_TYPE = "聲請改期／請假狀草稿"


@dataclass(frozen=True, slots=True)
class EnqueueAdmission:
    accepted: bool
    reason: str
    future: Future[dict[str, Any]]


def load_case(*, case_number: str = "", case_id: str = "") -> dict[str, Any] | None:
    if case_id:
        row, _ = _osc_exec("SELECT * FROM cases WHERE id=%s LIMIT 1", (str(case_id),), fetch="one")
        if row:
            item = dict(row)
            if case_number and str(item.get("case_number") or "").strip() != str(case_number).strip():
                return None
            return item
    if case_number:
        row, _ = _osc_exec(
            "SELECT * FROM cases WHERE case_number=%s ORDER BY updated_at DESC, created_date DESC LIMIT 1",
            (str(case_number).strip(),),
            fetch="one",
        )
        if row:
            return dict(row)
    return None


def _open_status_sql(column: str = "status") -> str:
    return (
        f"(COALESCE({column}, '') NOT IN "
        "('deleted','cancelled','canceled','取消','已取消','completed','done','已完成','完成'))"
    )


def load_existing_schedules(start: datetime, end: datetime, *, limit: int = 500) -> list[dict[str, Any]]:
    """Load all prior business appointments that can compete with a hearing."""

    start_day = (start - timedelta(days=1)).date().isoformat()
    end_day = (end + timedelta(days=1)).date().isoformat()
    calendar_rows, _ = _osc_exec(
        f"""
        SELECT ce.id, ce.event_id, ce.title, ce.summary, ce.description, ce.start_date, ce.end_date,
               ce.location, ce.is_all_day, ce.case_number, ce.created_date, ce.updated_date,
               COALESCE((
                   SELECT c.lawyer FROM cases c
                    WHERE c.case_number=ce.case_number
                    ORDER BY c.updated_at DESC, c.created_date DESC LIMIT 1
               ), '') AS lawyer,
               'calendar_events' AS source_table, 'calendar_events' AS source_kind,
               '' AS status
          FROM calendar_events ce
         WHERE ce.start_date < %s
           AND COALESCE(ce.end_date, DATE_ADD(ce.start_date, INTERVAL 1 HOUR)) > %s
         ORDER BY ce.start_date, ce.id
         LIMIT %s
        """,
        (end.isoformat(sep=" "), start.isoformat(sep=" "), int(limit)),
        fetch="all",
    )
    todo_rows, _ = _osc_exec(
        f"""
        SELECT t.id, t.case_number, t.client_name, t.todo_type, t.todo_date, t.todo_time,
               t.description, t.status, t.source_file, t.created_date, t.completed_date,
               COALESCE((
                   SELECT c.lawyer FROM cases c
                    WHERE c.case_number=t.case_number
                    ORDER BY c.updated_at DESC, c.created_date DESC LIMIT 1
               ), '') AS lawyer,
               CONCAT('todo:', t.id) AS event_id,
               'case_todos' AS source_table
          FROM case_todos t
         WHERE t.todo_date >= %s AND t.todo_date <= %s
           AND {_open_status_sql('t.status')}
         ORDER BY t.todo_date, COALESCE(t.todo_time, '00:00:00'), t.id
         LIMIT %s
        """,
        (start_day, end_day, int(limit)),
        fetch="all",
    )
    values: list[dict[str, Any]] = [dict(row) for row in (calendar_rows or [])]
    for row in todo_rows or []:
        item = dict(row)
        item["title"] = item.get("todo_type") or item.get("description") or "行程"
        item["summary"] = item.get("description") or ""
        item["start_date"] = _todo_start(item)
        item["end_date"] = _todo_end(item)
        item["is_all_day"] = 0 if str(item.get("todo_time") or "").strip() else 1
        item["source_kind"] = "case_todos"
        values.append(item)
    return _dedupe_schedules(values)


def _todo_start(row: Mapping[str, Any]) -> str:
    day = str(row.get("todo_date") or "").strip()
    clock = str(row.get("todo_time") or "").strip()
    return f"{day} {clock}".strip()


def _todo_end(row: Mapping[str, Any]) -> str:
    try:
        start = datetime.fromisoformat(_todo_start(row).replace(" ", "T"))
    except ValueError:
        return _todo_start(row)
    if not str(row.get("todo_time") or "").strip():
        return (start + timedelta(days=1)).isoformat(sep=" ")
    return (start + timedelta(hours=1)).isoformat(sep=" ")


def _dedupe_schedules(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in values:
        try:
            normalized = normalize_schedule(item)
        except ValueError:
            continue
        key = (
            normalized.case_number,
            normalized.start.isoformat(timespec="minutes"),
            "".join(normalized.title.split()),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def check_candidate(candidate: Mapping[str, Any]) -> list[ConflictDecision]:
    enriched = dict(candidate)
    case_number = str(enriched.get("case_number") or "").strip()
    if case_number and not str(enriched.get("lawyer") or "").strip():
        case = load_case(case_number=case_number)
        if case:
            enriched["lawyer"] = case.get("lawyer") or ""
    normalized = normalize_schedule(enriched)
    existing = load_existing_schedules(normalized.start, normalized.end)
    return find_conflict_decisions(enriched, existing)


def _case_folder(case: Mapping[str, Any]) -> Path:
    raw = str(case.get("folder_path") or "").strip()
    local = _osc_resolve_existing_local_path(raw, prefer_dir=True) if raw else ""
    if not local:
        guessed = _osc_guess_case_folder(str(case.get("case_number") or ""))
        local = _osc_resolve_existing_local_path(guessed, prefer_dir=True) if guessed else ""
    if not local or not Path(local).is_dir():
        raise FileNotFoundError("case_folder_not_available")
    return Path(local)


def _enrich_prior(decision: ConflictDecision) -> ConflictDecision:
    prior_case = load_case(case_number=decision.existing.case_number) if decision.existing.case_number else None
    if not prior_case:
        return decision
    raw = {**dict(decision.existing.raw), **{
        "court_name": prior_case.get("court_name") or decision.existing.raw.get("court_name"),
        "court_case_no": prior_case.get("court_case_no") or prior_case.get("court_case_number"),
    }}
    enriched = normalize_schedule(raw)
    return ConflictDecision(decision.candidate, enriched, decision.action, decision.reason)


def _register_document(case: Mapping[str, Any], path: Path, *, description: str) -> str:
    case_id = str(case.get("id") or "").strip()
    if not case_id:
        raise ValueError("case_id_required_for_document_registration")
    tenant_id = tenant_id_from_env()
    idempotency_key = hashlib.sha256(
        f"{tenant_id}\0{case_id}\0{path}".encode("utf-8")
    ).hexdigest()
    _osc_exec(
        """
        INSERT INTO case_documents
          (case_id, document_type, file_name, file_path, description, upload_date,
           tenant_id, idempotency_key)
        VALUES (%s,%s,%s,%s,%s,NOW(),%s,%s)
        ON DUPLICATE KEY UPDATE
          id=LAST_INSERT_ID(id),
          file_name=VALUES(file_name),
          description=VALUES(description)
        """,
        (case_id, _DOCUMENT_TYPE, path.name, str(path), description, tenant_id, idempotency_key),
        fetch="none",
    )
    registered, _ = _osc_exec(
        "SELECT id FROM case_documents WHERE idempotency_key=%s AND tenant_id=%s LIMIT 1",
        (idempotency_key, tenant_id),
        fetch="one",
    )
    document_id = str((registered or {}).get("id") or "").strip()
    if not document_id:
        raise RuntimeError("case_document_registration_failed")
    return document_id


@contextmanager
def _artifact_lock(path: Path, *, timeout_seconds: float = 5.0):
    """Cross-process lock covering DOCX creation and DB registration."""

    lock_path = path.with_name(f".{path.name}.magi.lock")
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    fd: int | None = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.write(fd, f"pid={os.getpid()} created={datetime.now().isoformat()}\n".encode("utf-8"))
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > 120:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                raise TimeoutError("hearing_leave_request_generation_busy")
            time.sleep(0.1)
    try:
        yield
    finally:
        try:
            if fd is not None:
                os.close(fd)
        finally:
            lock_path.unlink(missing_ok=True)


def generate_from_decision(
    decision: ConflictDecision,
    *,
    lawyer_name: str = "",
    party_role: str = "當事人",
    output_dir: str = "",
) -> dict[str, Any]:
    decision = _enrich_prior(decision)
    case = load_case(case_number=decision.candidate.case_number)
    if not case:
        raise LookupError("target_case_not_found")
    payload = leave_request_payload(case, decision, lawyer_name=lawyer_name, party_role=party_role)
    directory = Path(output_dir).expanduser() if output_dir else resolve_pleading_output_dir(
        _case_folder(case),
        title=str(payload.get("document_title") or "聲請改期狀"),
        case_category=str(case.get("case_category") or "一般案件"),
    )
    path = directory / leave_request_filename(payload)
    with _artifact_lock(path):
        created = not path.exists()
        if created:
            build_leave_request_docx(payload, path)
        try:
            document_id = _register_document(case, path, description="MAGI 衝庭檢查產生；送出前須由律師人工確認")
        except Exception:
            if created:
                path.unlink(missing_ok=True)
            raise
    return {
        "ok": True,
        "created": created,
        "path": str(path),
        "document_id": document_id,
        "file_name": path.name,
        "case_number": str(case.get("case_number") or ""),
        "case_id": str(case.get("id") or ""),
        "is_legal_aid": bool(payload.get("is_legal_aid")),
        "generation_mode": "automatic_conflict",
        "payload": _json_payload(payload),
    }


def generate_manual(
    case: Mapping[str, Any],
    *,
    target_start: datetime,
    prior_start: datetime,
    prior_court_name: str,
    prior_hearing_label: str = "開庭",
    prior_court_case_no: str = "",
    lawyer_name: str = "",
    party_role: str = "當事人",
    target_hearing_label: str = "開庭",
    conflict_statement: str = "",
    output_dir: str = "",
) -> dict[str, Any]:
    payload = manual_leave_request_payload(
        case,
        target_start=target_start,
        prior_start=prior_start,
        prior_court_name=prior_court_name,
        prior_hearing_label=prior_hearing_label,
        prior_court_case_no=prior_court_case_no,
        lawyer_name=lawyer_name,
        party_role=party_role,
        target_hearing_label=target_hearing_label,
        conflict_statement=conflict_statement,
    )
    directory = Path(output_dir).expanduser() if output_dir else resolve_pleading_output_dir(
        _case_folder(case),
        title=str(payload.get("document_title") or "聲請改期狀"),
        case_category=str(case.get("case_category") or "一般案件"),
    )
    path = directory / leave_request_filename(payload)
    with _artifact_lock(path):
        created = not path.exists()
        if created:
            build_leave_request_docx(payload, path)
        try:
            document_id = _register_document(case, path, description="人工由 OSC 產生的請假狀草稿；送出前須由律師人工確認")
        except Exception:
            if created:
                path.unlink(missing_ok=True)
            raise
    return {
        "ok": True,
        "created": created,
        "path": str(path),
        "document_id": document_id,
        "file_name": path.name,
        "case_number": str(case.get("case_number") or ""),
        "case_id": str(case.get("id") or ""),
        "is_legal_aid": bool(payload.get("is_legal_aid")),
        "generation_mode": "manual",
        "payload": _json_payload(payload),
    }


def _json_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, datetime):
            result[key] = value.isoformat(sep=" ")
        elif isinstance(value, date):
            result[key] = value.isoformat()
        else:
            result[key] = value
    return result


def _notify(decision: ConflictDecision, *, generated_path: str = "") -> dict[str, Any]:
    from skills.ops.red_phone import send_telegram_push_with_status

    return send_telegram_push_with_status(
        conflict_notification_text(decision, generated_path=generated_path),
        severity="warning",
        source="hearing_conflict",
        topic_key="case_schedule",
        queue_on_fail=True,
        mirror_to_discord=True,
    )


def process_candidate(
    candidate: Mapping[str, Any],
    *,
    auto_generate: bool = True,
    notify: bool = True,
) -> dict[str, Any]:
    """Evaluate one newly ingested hearing without breaking its parent flow."""

    try:
        decisions = check_candidate(candidate)
        generated: dict[str, Any] | None = None
        leave_decision = pick_leave_request_conflict(decisions)
        if leave_decision and auto_generate:
            generated = generate_from_decision(leave_decision)
            try:
                from api.osc.utils import _osc_log_activity

                _osc_log_activity(
                    "hearing_leave_request_generated",
                    "case",
                    str(generated.get("case_id") or generated.get("case_number") or ""),
                    {
                        "mode": "automatic_conflict_background",
                        "file_name": generated.get("file_name"),
                        "created": generated.get("created"),
                    },
                )
            except Exception:
                logger.warning("hearing conflict background audit failed", exc_info=True)
        notifications: list[dict[str, Any]] = []
        if notify:
            for decision in decisions:
                # One generated document can cite the earliest pre-existing
                # hearing; every non-hearing clash still receives a warning.
                if decision.action == "generate_leave_request" and decision is not leave_decision:
                    continue
                notifications.append(
                    _notify(
                        decision,
                        generated_path=str((generated or {}).get("path") or "")
                        if decision.action == "generate_leave_request"
                        else "",
                    )
                )
        return {
            "ok": True,
            "conflict_count": len(decisions),
            "decisions": [item.as_dict() for item in decisions],
            "generated": generated,
            "notifications": notifications,
        }
    except Exception as exc:
        logger.warning("hearing conflict processing failed: %s", exc, exc_info=True)
        return {"ok": False, "error": str(exc), "conflict_count": 0, "decisions": []}


def enqueue_candidate(
    candidate: Mapping[str, Any],
    *,
    auto_generate: bool = True,
    notify: bool = True,
) -> EnqueueAdmission:
    """Serialize NAS writes and notifications away from an OSC request."""

    frozen = dict(candidate)
    key = (
        str(frozen.get("event_id") or frozen.get("id") or "").strip(),
        str(frozen.get("case_number") or "").strip(),
        str(frozen.get("start_date") or frozen.get("start") or "").strip(),
    )
    with _INFLIGHT_LOCK:
        if key in _INFLIGHT_KEYS:
            duplicate: Future[dict[str, Any]] = Future()
            duplicate.set_result({"ok": True, "skipped": True, "reason": "already_queued"})
            return EnqueueAdmission(False, "already_queued", duplicate)
        if not _QUEUE_SLOTS.acquire(blocking=False):
            rejected: Future[dict[str, Any]] = Future()
            rejected.set_result({"ok": False, "skipped": True, "error": "hearing_conflict_queue_full"})
            logger.warning("hearing conflict queue is full; rejected candidate %s", key)
            return EnqueueAdmission(False, "queue_full", rejected)
        _INFLIGHT_KEYS.add(key)

    def run() -> dict[str, Any]:
        try:
            return process_candidate(frozen, auto_generate=auto_generate, notify=notify)
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT_KEYS.discard(key)
            _QUEUE_SLOTS.release()

    return EnqueueAdmission(True, "accepted", _EXECUTOR.submit(run))

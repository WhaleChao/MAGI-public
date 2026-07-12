# -*- coding: utf-8 -*-
"""Parse and apply LAF closing-transfer confirmation emails.

These notices confirm that LAF has accepted a closing report.  MAGI only keeps
its own case lifecycle, so the local outcome is simply "已結案" without
recording LAF's internal transfer/review state.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_LAF_NO_RE = re.compile(r"\b\d{7}-[A-Z]-\d{3}\b")
_TRANSFER_RE = re.compile(r"(分會)?業[已經]?(?:轉入本會系統|分會轉入系統|轉入系統)")
_CLOSING_REPORT_RE = re.compile(r"回報[（(]\s*結案\s*[)）]|回報類型\s*[：:]\s*[^。\n\r]*結案")
_FIELD_LINE_RE = re.compile(r"^[\s※＊*　]*(?P<label>[^：:\n\r]{2,20})\s*[：:]\s*(?P<value>.*)$")
_DEFAULT_ARCHIVE_PENDING_PATH = Path(__file__).resolve().parents[1] / ".runtime" / "laf_closing_archive_pending.json"


@dataclass(frozen=True)
class LAFClosingTransferNotice:
    laf_case_number: str
    client_name: str = ""
    lawyer_name: str = ""
    lawyer_id: str = ""
    report_type: str = ""
    staff_name: str = ""
    staff_phone: str = ""
    staff_email: str = ""
    subject: str = ""

    def safe_dict(self) -> dict[str, str]:
        data = asdict(self)
        if data.get("lawyer_id"):
            data["lawyer_id"] = "<REDACTED>"
        return data


def _clean_mail_text(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?is)<br\s*/?>", "\n", text)
    text = re.sub(r"(?is)</p\s*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\xa0", " ").replace("\u3000", " ")
    text = text.replace("﹕", ":").replace("：", ":")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    return text.strip()


def _field_map(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _FIELD_LINE_RE.match(line)
        if not match:
            continue
        label = re.sub(r"\s+", "", match.group("label") or "")
        value = (match.group("value") or "").strip()
        if label and value:
            out[label] = value
    return out


def _pick(fields: dict[str, str], *labels: str) -> str:
    for label in labels:
        value = fields.get(label)
        if value:
            return value.strip()
    return ""


def _parse_staff(value: str) -> tuple[str, str, str]:
    text = str(value or "").strip()
    if not text:
        return "", "", ""
    email_match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    phone_match = re.search(r"(?:電話|Tel|TEL)\s*[:：]?\s*([0-9#()\-+\s]{6,})", text)
    email = email_match.group(0).strip() if email_match else ""
    phone = phone_match.group(1).strip() if phone_match else ""
    name = text
    name = re.sub(r"\s*(?:電話|Tel|TEL)\s*[:：]?.*$", "", name).strip()
    if " " in name:
        name = name.split()[0].strip()
    return name, phone, email


def parse_laf_closing_transfer_notice(subject: Any = "", body: Any = "") -> LAFClosingTransferNotice | None:
    """Return a closing-transfer notice when the email is a LAF closing finalization."""
    clean_subject = _clean_mail_text(subject)
    clean_body = _clean_mail_text(body)
    haystack = f"{clean_subject}\n{clean_body}".strip()
    if not haystack:
        return None

    if not _TRANSFER_RE.search(haystack):
        return None
    if not _CLOSING_REPORT_RE.search(haystack):
        return None

    laf_match = _LAF_NO_RE.search(haystack)
    if not laf_match:
        return None

    fields = _field_map(haystack)
    report_type = _pick(fields, "回報類型")
    if report_type and "結案" not in report_type:
        return None

    staff_name, staff_phone, staff_email = _parse_staff(_pick(fields, "派案分會承辦人", "分會承辦人", "承辦人"))
    return LAFClosingTransferNotice(
        laf_case_number=laf_match.group(0).strip(),
        client_name=_pick(fields, "受扶助人姓名", "當事人姓名", "申請人姓名"),
        lawyer_name=_pick(fields, "律師姓名"),
        lawyer_id=_pick(fields, "身分證字號", "律師身分證字號"),
        report_type=report_type or "結案",
        staff_name=staff_name,
        staff_phone=staff_phone,
        staff_email=staff_email,
        subject=clean_subject,
    )


def _db_fetch_one(db: Any, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    if db is None:
        return None
    if hasattr(db, "fetch_one"):
        try:
            row = db.fetch_one(sql, params, as_dict=True)
            return row if isinstance(row, dict) else (dict(row) if row else None)
        except TypeError:
            row = db.fetch_one(sql, params)
            return row if isinstance(row, dict) else (dict(row) if row else None)
    if hasattr(db, "execute"):
        try:
            row = db.execute(sql, params, fetch="one")
        except TypeError:
            row = db.execute(sql, params)
        return row if isinstance(row, dict) else row
    return None


def _query_case_by_laf_number(db: Any, laf_case_number: str) -> dict[str, Any] | None:
    laf_no = str(laf_case_number or "").strip()
    if not laf_no:
        return None

    queries = [
        (
            """
            SELECT `id`, `case_number`, `client_name`, `status`, `legal_aid_status`,
                   `legal_aid_approval_status`, `manual_status_lock`, `manual_laf_status_lock`,
                   `legal_aid_number`, `laf_case_no`, `application_no`
            FROM `cases`
            WHERE `legal_aid_number` = %s OR `laf_case_no` = %s OR `application_no` = %s OR `case_number` = %s
            ORDER BY `id` DESC
            LIMIT 1
            """,
            (laf_no, laf_no, laf_no, laf_no),
        ),
        (
            """
            SELECT `id`, `case_number`, `client_name`, `status`, `legal_aid_status`,
                   `legal_aid_approval_status`, `manual_status_lock`, `legal_aid_number`, `laf_case_no`, `application_no`
            FROM `cases`
            WHERE `legal_aid_number` = %s OR `laf_case_no` = %s OR `application_no` = %s OR `case_number` = %s
            ORDER BY `id` DESC
            LIMIT 1
            """,
            (laf_no, laf_no, laf_no, laf_no),
        ),
        (
            """
            SELECT `id`, `case_number`, `client_name`, `status`, `legal_aid_status`,
                   `legal_aid_number`
            FROM `cases`
            WHERE `legal_aid_number` = %s OR `case_number` = %s
            ORDER BY `id` DESC
            LIMIT 1
            """,
            (laf_no, laf_no),
        ),
    ]
    for sql, params in queries:
        try:
            row = _db_fetch_one(db, sql, params)
            if row:
                return row
        except Exception as exc:
            message = str(exc)
            if "Unknown column" not in message and "no such column" not in message:
                raise

    if hasattr(db, "check_laf_case_exists"):
        try:
            row = db.check_laf_case_exists(laf_case_number=laf_no)
            return dict(row) if row else None
        except TypeError:
            row = db.check_laf_case_exists(laf_no, "", "", "")
            return dict(row) if row else None
    return None


def _normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    return re.sub(r"[\s·・•‧∙．｡。、，,()（）-]+", "", text)


def _normalize_status(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return text.replace("、", "，").replace(",", "，")


def _case_is_pending_closing(row: dict[str, Any]) -> bool:
    main = _normalize_status(row.get("legal_aid_status"))
    generic = _normalize_status(row.get("status"))
    if main == "已結案":
        return True
    pending_exact = {
        "已結案，待送出",
        "已結案，待報結",
        "已報結（待轉入）",
        "已報結(待轉入)",
        "待轉入",
        "待報結",
        "暫存",
        "已報結",
    }
    if main in pending_exact:
        return True
    if any(marker in main for marker in ("待送出", "待報結", "待轉入")):
        return True
    return generic in {"結案中", "已結案，待送出", "已結案，待報結"}


def resolve_laf_closing_transfer_case(db: Any, notice: LAFClosingTransferNotice) -> dict[str, Any]:
    row = _query_case_by_laf_number(db, notice.laf_case_number)
    if not row:
        return {"ok": False, "status": "case_not_found", "case": None}
    db_name = _normalize_name(row.get("client_name"))
    mail_name = _normalize_name(notice.client_name)
    if db_name and mail_name and db_name != mail_name:
        return {"ok": False, "status": "client_name_mismatch", "case": row}
    return {"ok": True, "status": "matched", "case": row}


def apply_laf_closing_transfer_notice(
    db: Any,
    notice: LAFClosingTransferNotice,
    *,
    source_message_id: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Apply a parsed closing-transfer notice to the local cases table."""
    if db is None:
        return {"ok": False, "updated": False, "status": "db_unavailable", "notice": notice.safe_dict()}

    resolved = resolve_laf_closing_transfer_case(db, notice)
    row = resolved.get("case")
    if not resolved.get("ok"):
        return {
            "ok": False,
            "updated": False,
            "status": resolved.get("status") or "case_not_matched",
            "notice": notice.safe_dict(),
            "case_number": (row or {}).get("case_number") if isinstance(row, dict) else "",
            "source_message_id": source_message_id,
        }

    assert isinstance(row, dict)
    main = _normalize_status(row.get("legal_aid_status"))
    approval = _normalize_status(row.get("legal_aid_approval_status"))
    generic = _normalize_status(row.get("status"))
    manual_locked = bool(int(row.get("manual_status_lock") or 0))
    if main == "已結案" and (generic == "已結案" or manual_locked) and not approval:
        return {
            "ok": True,
            "updated": False,
            "status": "already_final",
            "case_number": row.get("case_number") or "",
            "laf_case_number": notice.laf_case_number,
            "source_message_id": source_message_id,
        }

    if not _case_is_pending_closing(row):
        return {
            "ok": False,
            "updated": False,
            "status": "not_pending_closing",
            "case_number": row.get("case_number") or "",
            "laf_case_number": notice.laf_case_number,
            "current_legal_aid_status": row.get("legal_aid_status") or "",
            "source_message_id": source_message_id,
        }

    if dry_run:
        return {
            "ok": True,
            "updated": False,
            "status": "would_update",
            "case_number": row.get("case_number") or "",
            "laf_case_number": notice.laf_case_number,
            "source_message_id": source_message_id,
        }

    from casper_ecosystem.law_firm_orchestrators.laf_nightly_audit import _update_laf_status

    _update_laf_status(db, row, "已結案")
    _clear_laf_approval_status(db, row)
    archive_pending = record_laf_closing_archive_pending(
        row,
        notice,
        source_message_id=source_message_id,
        reason="laf_closing_transfer_updated",
    )
    return {
        "ok": bool(archive_pending.get("ok")),
        "updated": True,
        "status": "updated" if archive_pending.get("ok") else "updated_archive_pending_failed",
        "case_number": row.get("case_number") or "",
        "laf_case_number": notice.laf_case_number,
        "source_message_id": source_message_id,
        "archive_pending": archive_pending,
    }


def _clear_laf_approval_status(db: Any, row: dict[str, Any]) -> None:
    case_id = row.get("id")
    if not case_id:
        return
    if not _normalize_status(row.get("legal_aid_approval_status")):
        return
    writer = getattr(db, "execute_write", None) or getattr(db, "execute", None)
    if not writer:
        return
    try:
        writer(
            "UPDATE `cases` SET `legal_aid_approval_status` = NULL, "
            "`legal_aid_approval_checked_at` = NULL WHERE `id` = %s",
            (case_id,),
        )
        row["legal_aid_approval_status"] = None
    except Exception as exc:
        message = str(exc)
        if "legal_aid_approval_status" not in message and "Unknown column" not in message and "no such column" not in message:
            raise


def _archive_pending_path() -> Path:
    raw = str(os.environ.get("MAGI_LAF_CLOSING_ARCHIVE_PENDING_PATH") or "").strip()
    return Path(raw).expanduser() if raw else _DEFAULT_ARCHIVE_PENDING_PATH


def _load_archive_pending(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception:
        logger.warning("failed to load LAF closing archive pending file: %s", path, exc_info=True)
    return {}


def _write_archive_pending(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{int(time.time() * 1000)}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    tmp.replace(path)


def record_laf_closing_archive_pending(
    row: dict[str, Any],
    notice: LAFClosingTransferNotice,
    *,
    source_message_id: str = "",
    reason: str = "",
) -> dict[str, Any]:
    """Durably remember that an updated closing-transfer case still needs archive handling."""
    path = _archive_pending_path()
    case_id = str((row or {}).get("id") or "").strip()
    case_number = str((row or {}).get("case_number") or "").strip()
    key = f"case-id:{case_id}" if case_id else (case_number or str(notice.laf_case_number or "").strip())
    if not key:
        return {"ok": False, "status": "archive_pending_key_missing", "path": str(path)}
    now = time.time()
    try:
        payload = _load_archive_pending(path)
        items = payload.get("items") if isinstance(payload.get("items"), dict) else {}
        old = items.get(key) if isinstance(items.get(key), dict) else {}
        first_seen = old.get("first_seen_at") or now
        attempts = int(old.get("attempts") or 0)
        items[key] = {
            **old,
            "status": "pending_archive",
            "reason": reason or old.get("reason") or "laf_closing_transfer",
            "case_id": case_id,
            "case_number": case_number,
            "client_name": str((row or {}).get("client_name") or "").strip(),
            "laf_case_number": str(notice.laf_case_number or "").strip(),
            "source_message_id": str(source_message_id or "").strip(),
            "first_seen_at": first_seen,
            "updated_at": now,
            "attempts": attempts,
        }
        out = {
            "ok": True,
            "source": "laf_closing_transfer",
            "updated_at": now,
            "pending_count": len(items),
            "items": items,
        }
        _write_archive_pending(path, out)
        return {
            "ok": True,
            "status": "pending_archive_recorded",
            "path": str(path),
            "key": key,
            "pending_count": len(items),
        }
    except Exception as exc:
        logger.error("failed to record LAF closing archive pending item: %s", exc)
        return {
            "ok": False,
            "status": "archive_pending_record_failed",
            "path": str(path),
            "key": key,
            "error": f"{type(exc).__name__}: {exc}",
        }


def laf_closing_transfer_record_status(result: dict[str, Any]) -> str:
    status = str((result or {}).get("status") or "")
    if status == "updated":
        return "closing_transfer_updated"
    if status == "updated_archive_pending_failed":
        return "closing_transfer_archive_pending_failed"
    if status == "already_final":
        return "closing_transfer_already_final"
    if status == "would_update":
        return "closing_transfer_dry_run"
    if status:
        return f"closing_transfer_{status}"[:64]
    return "closing_transfer_seen"

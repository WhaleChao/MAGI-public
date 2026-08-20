"""PII-free receipts for correlating file-review portal snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable


PORTAL_DOWNLOAD_RECEIPT_SCHEMA = "magi.file-review.portal-download-receipt/v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _first(row: dict[str, Any], *names: str) -> str:
    for name in names:
        value = str(row.get(name) or "").strip()
        if value:
            return value
    return ""


def _first_upper(row: dict[str, Any], *names: str) -> str:
    """Read one alias set using the portal's case-insensitive status semantics."""
    return _first(row, *names).upper()


def canonical_portal_download_signature(row: dict[str, Any]) -> str:
    """Hash one actionable OLA row without publishing its case/person data.

    Portal ``rowid`` is the preferred opaque identity.  Case/court fields are
    used only as an in-memory fallback when an older row has no opaque ID; the
    receipt exposes only the final SHA-256 digest.  Revision/status fields make
    a later upload batch on the same row a different receipt.
    """
    if not isinstance(row, dict):
        return ""
    row_id = _first(row, "rowid", "no")
    identity = {
        "row_id": row_id,
        "fallback_court": "" if row_id else _first(row, "court", "crtid"),
        "fallback_case": "" if row_id else _first(
            row, "case_number", "yyidno", "court_case_no", "showyyidno", "c60yyidno"
        ),
        "apply_at": _first(row, "applydt"),
    }
    revision = {
        "semantic_status": "downloadable",
        "status_code": _first(row, "status_code", "status"),
        "portal_status": _first_upper(row, "p_status"),
        "payment_status": _first(row, "paystatus"),
        "payment_flag": _first_upper(row, "payment_flag", "payment"),
        "is_downloaded": _first_upper(row, "isdown"),
        "download_date": _first(row, "downdt"),
        "download_time": _first(row, "downtm"),
        "download_deadline": _first(
            row, "deadline", "downlimit", "dlmdate", "payedate"
        ),
        "payment_deadline": _first(row, "pay_deadline", "paylimitdt", "limitdt"),
        "updated_at": _first(row, "upddt", "updated_at", "updtime"),
        "content_marker": hashlib.sha256(
            re.sub(r"\s+", " ", _first(row, "result_text", "result", "row_text"))[:1000]
            .strip()
            .encode("utf-8")
        ).hexdigest(),
    }
    if not any(identity.values()):
        return ""
    material = json.dumps(
        {
            "schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
            "identity": identity,
            "revision": revision,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def normalize_signature_hashes(values: Iterable[Any] | None) -> list[str]:
    return sorted(
        {
            str(value or "").strip().lower()
            for value in (values or [])
            if _SHA256_RE.fullmatch(str(value or "").strip().lower())
        }
    )


def signature_set_hash(values: Iterable[Any] | None) -> str:
    normalized = normalize_signature_hashes(values)
    material = json.dumps(
        {"schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA, "signatures": normalized},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def portal_snapshot_fingerprint(values: Iterable[Any] | None) -> str:
    signatures = normalize_signature_hashes(values)
    fingerprint_material = json.dumps(
        {
            "schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
            "signature_set_hash": signature_set_hash(signatures),
            "signature_count": len(signatures),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(fingerprint_material.encode("utf-8")).hexdigest()


def portal_observed_epoch(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def portal_download_snapshot(
    items: Iterable[dict[str, Any]] | None,
    *,
    observed_at: str = "",
) -> dict[str, Any]:
    signatures = normalize_signature_hashes(
        canonical_portal_download_signature(item)
        for item in (items or [])
        if isinstance(item, dict)
        and str(item.get("status") or "").strip().lower() == "downloadable"
    )
    observed = str(observed_at or "").strip() or datetime.now().astimezone().isoformat(
        timespec="seconds"
    )
    set_hash = signature_set_hash(signatures)
    return {
        "portal_download_receipt_schema": PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
        "portal_download_signature_hashes": signatures,
        "portal_download_signature_set_hash": set_hash,
        "portal_probe_snapshot_fingerprint": portal_snapshot_fingerprint(signatures),
        "portal_probe_observed_at": observed,
    }

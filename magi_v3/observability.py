"""Bounded, de-identified operations read models for the V3 control core."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .ledger import JobRecord


_SECRET = re.compile(r"(?i)\b(?:token|secret|password|api[_-]?key|authorization)\b\s*[:=]\s*\S+")
_POSIX_HOME_ROOT = "/" + "Users" + "/"
_POSIX_VOLUME_ROOT = "/" + "Volumes" + "/"
_PATH = re.compile(
    rf"(?:{re.escape(_POSIX_HOME_ROOT)}|{re.escape(_POSIX_VOLUME_ROOT)}|[A-Za-z]:\\\\)[^\s\"']+"
)
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")
_TERMINAL_FAILURES = frozenset({"failed", "timed_out", "cancelled"})


def task_trace(record: JobRecord, *, pseudonym_key: bytes | None = None) -> dict[str, Any]:
    """Project one canonical ledger record into the user-safe task trace.

    Inputs/results are intentionally omitted: they commonly contain legal
    material.  The trace still links intent, model/provider metrics, artifacts,
    side effects and their receipts, plus a bounded actionable error.
    """
    metrics = dict(record.metrics)
    key = pseudonym_key or secrets.token_bytes(32)
    return {
        "trace_id": _digest(record.job_id, key),
        "intent": {
            "capability": _safe_label(record.capability),
            "operation": _safe_label(record.operation),
        },
        "model": {
            "provider": _safe_label(metrics.get("provider")),
            "name": _safe_label(metrics.get("model")),
        },
        "tools": sorted(
            {_safe_label(item.get("kind")) for item in record.artifacts if _safe_label(item.get("kind"))}
        ),
        "side_effect": {"class": record.side_effect_class, "phase": record.commit_phase},
        "receipts": [
            {
                "kind": _safe_label(item["kind"]) or "receipt",
                "reference": _digest(str(item["reference"]), key),
                "committed_at": item["committed_at"],
            }
            for item in record.side_effect_receipts
        ],
        "outcome": record.status.value,
        "business_completed": record.business_completed,
        "timing": {"created_at": record.created_at, "started_at": record.started_at, "finished_at": record.finished_at, "duration_ms": metrics.get("duration_ms")},
        "error": actionable_error(record.error),
    }


def outcome_slo(records: Iterable[JobRecord], *, pseudonym_key: bytes | None = None) -> dict[str, Any]:
    """Aggregate real task outcomes without treating deferred work as success."""
    rows = list(records)
    key = pseudonym_key or secrets.token_bytes(32)
    outcomes = Counter(row.status.value for row in rows)
    terminal = sum(outcomes[name] for name in _TERMINAL_FAILURES)
    completed = outcomes["succeeded"] + outcomes["degraded"] + outcomes["deferred"] + terminal
    receipt_required = [row for row in rows if row.side_effect_class in {"external_commit", "destructive"} and row.status.value == "succeeded"]
    receipt_missing = [row for row in receipt_required if not row.side_effect_receipts]
    return {
        "sample_size": len(rows),
        "completed": completed,
        "outcomes": dict(sorted(outcomes.items())),
        "terminal_failure_count": terminal,
        "deferred_count": outcomes["deferred"],
        "success_rate": round(outcomes["succeeded"] / completed, 4) if completed else None,
        "dangerous_success_receipt_coverage": round((len(receipt_required) - len(receipt_missing)) / len(receipt_required), 4) if receipt_required else None,
        "receipt_missing_trace_ids": [_digest(row.job_id, key) for row in receipt_missing],
    }


def actionable_error(error: Mapping[str, Any] | None) -> dict[str, str] | None:
    if not error:
        return None
    code = _safe_label(error.get("code")) or "operation_failed"
    message = _safe_text(str(error.get("message") or code))[:300]
    return {"code": code, "message": message or code, "next_step": _next_step(code)}


def support_bundle(records: Iterable[JobRecord], *, max_records: int = 100) -> dict[str, Any]:
    rows = list(records)
    if len(rows) > max_records:
        rows = rows[:max_records]
    key = secrets.token_bytes(32)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "deidentified": True,
        "slo": outcome_slo(rows, pseudonym_key=key),
        "traces": [task_trace(row, pseudonym_key=key) for row in rows],
    }


def verify_dr_report(report: Mapping[str, Any], *, max_rpo_seconds: int, max_rto_seconds: int) -> dict[str, Any]:
    """Validate a real restore-verification receipt; never infer DR readiness."""
    if max_rpo_seconds < 1 or max_rto_seconds < 1:
        raise ValueError("RPO/RTO targets must be positive")
    elapsed = report.get("restore_elapsed_seconds")
    age = report.get("backup_age_seconds")
    passed = report.get("status") == "passed" and report.get("actual_restore_performed") is True
    rpo_ok = isinstance(age, (int, float)) and not isinstance(age, bool) and 0 <= age <= max_rpo_seconds
    rto_ok = isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool) and 0 <= elapsed <= max_rto_seconds
    return {"verified": bool(passed and rpo_ok and rto_ok), "restore_receipt_valid": bool(passed), "rpo_ok": rpo_ok, "rto_ok": rto_ok, "rpo_target_seconds": max_rpo_seconds, "rto_target_seconds": max_rto_seconds}


def _digest(value: str, key: bytes) -> str:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()[:16]


def _safe_label(value: Any) -> str | None:
    text = str(value or "").strip()
    return text if _SAFE_LABEL.fullmatch(text) else None


def _safe_text(value: str) -> str:
    return _PATH.sub("<local-path>", _SECRET.sub("<redacted>", value))


def _next_step(code: str) -> str:
    lowered = code.lower()
    if "timeout" in lowered:
        return "稍後重試；若持續發生，請提供支援包。"
    if any(token in lowered for token in ("storage", "drive", "nas")):
        return "確認儲存連線後重試；不要刪除既有證據。"
    if "receipt" in lowered:
        return "保留作業紀錄，確認外部系統結果後再重試。"
    return "請提供去識別支援包，供維運人員判讀。"

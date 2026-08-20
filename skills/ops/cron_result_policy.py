# -*- coding: utf-8 -*-
"""Cron result classification helpers.

Classify the rc/stdout/stderr from one invocation as a single snapshot.  This
keeps structured failures, deferred resource-guard runs, and no-op ``@MAGI``
responses from being mistaken for completed work while retaining the narrower
issue-agenda suppression policy for known wrapper return-code quirks.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict


_SUCCESS_MARKERS = (
    "✅ 未發現角色幻覺污染記憶",
    "✅ 報告已發送",
    "✅ Shell job",
)

_FAILURE_MARKERS = (
    "Traceback",
    "FileExistsError",
    "Exception",
    "ERROR",
    "Error:",
    "❌",
)

_HARD_FAILURE_MARKERS = (
    "Traceback",
    "FileExistsError",
    "Unhandled exception",
    "Fatal error",
)

_RESOURCE_GUARD_SKIP_RE = re.compile(
    r"(?:MAGI\s+resource\s+guard\s+skipped|resource_guard_skipped)",
    re.IGNORECASE,
)
_UNKNOWN_TASK_RE = re.compile(r"\bunknown\s+(?:task|command|action)\b", re.IGNORECASE)
_NO_ACTION_RESPONSE_RE = re.compile(
    "|".join(
        (
            r"抱歉[，,]?我目前無法提供有意義的回答",
            r"請換個方式再問一次",
            r"請問.{0,160}(?:請提供|以便我|需要我)",
            r"請提供.{0,160}(?:資訊|需求|上下文|文件|設定)",
            r"could\s+you\s+clarify",
            r"please\s+provide\s+(?:more\s+)?(?:information|details|context)",
            r"need\s+more\s+(?:information|details|context)",
        )
    ),
    re.DOTALL | re.IGNORECASE,
)
_DEFERRED_STATUSES = {"deferred"}
_TERMINAL_SCHEDULE_DEFER_REASONS = {
    "large_files_waiting_for_offpeak_window",
    "repair_budget_reserved",
    "resource_guard_skipped",
}


@dataclass(frozen=True)
class CronResultClassification:
    """Semantic result for one completed cron invocation.

    ``returncode`` remains the child process return code.  A process may exit
    zero while its structured contract says it failed or deferred work, so
    callers must use ``success`` rather than deriving success from rc alone.
    """

    success: bool
    status: str
    returncode: int
    error: str = ""


def _last_json_object(text: str) -> Dict[str, Any] | None:
    """Best-effort parse of the last JSON object printed by a cron script."""
    if not text:
        return None
    stripped = text.strip()
    decoder = json.JSONDecoder()
    candidates = [idx for idx, char in enumerate(stripped) if char == "{"]
    for start in reversed(candidates):
        try:
            obj, end = decoder.raw_decode(stripped[start:])
        except Exception:
            continue
        trailing = stripped[start + end :].strip()
        if isinstance(obj, dict) and trailing in {"", "```"}:
            return obj
    return None


def _contract_error(obj: Dict[str, Any]) -> str:
    for key in ("error", "message", "reason", "status"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    return "structured_result_reported_failure"


def terminal_schedule_deferral_reason(stdout: str, error: str = "") -> str:
    """Return a bounded non-retry wait reason, or an empty string.

    Some maintained batch owners deliberately stop at an off-peak or per-run
    work budget and leave a durable checkpoint for their next ordinary cron
    occurrence.  Retrying those receipts immediately wastes resources and can
    turn an old timeout retry into a false failure after exhaustion.  Only
    explicit, fail-closed structured deferrals (plus the resource guard's
    established text contract) are accepted here; storage/upstream/portal
    waits remain recoverable retries.
    """

    obj = _last_json_object((stdout or "").strip())
    if obj is not None:
        status = str(obj.get("status") or "").strip().lower()
        strict_deferred = bool(
            status in _DEFERRED_STATUSES
            and obj.get("success") is False
            and obj.get("deferred") is True
            and obj.get("partial") is not True
            and obj.get("retryable") is not True
        )
        if strict_deferred:
            reason = str(obj.get("reason") or obj.get("error") or "").strip().lower()
            if (
                reason in _TERMINAL_SCHEDULE_DEFER_REASONS
                or reason.endswith("_budget_exhausted")
                or reason.endswith("_waiting_for_offpeak_window")
            ):
                return reason
    normalized_error = str(error or "").strip().lower()
    if normalized_error == "resource_guard_skipped":
        return normalized_error
    # Compatibility for a completed pre-contract weekend batch.  The
    # scheduler only calls this migration for rc 0/75, non-timeout failures;
    # preserving the provider's explicit quota marker lets the new checkpoint
    # semantics repair the old false red light without deleting evidence.
    if "nim_daily_budget_exceeded:" in normalized_error:
        return "nim_daily_budget_exhausted"
    return ""


def _is_resource_guard_skip(text: str, obj: Dict[str, Any] | None) -> bool:
    if _RESOURCE_GUARD_SKIP_RE.search(text or ""):
        return True
    if not obj:
        return False
    status = str(obj.get("status") or "").strip().lower()
    return bool(
        status == "resource_guard_skipped"
        or (obj.get("resource_guarded") is True and obj.get("skipped") is True)
    )


def classify_cron_result(
    returncode: int | None,
    stdout: str,
    stderr: str,
    *,
    timed_out: bool = False,
    macro_response: bool = False,
) -> CronResultClassification:
    """Classify a single invocation from its own rc/stdout/stderr snapshot."""

    rc = int(returncode or 0)
    clean_stdout = (stdout or "").strip()
    clean_stderr = (stderr or "").strip()
    combined = f"{clean_stdout}\n{clean_stderr}".strip()
    obj = _last_json_object(clean_stdout)

    if timed_out:
        return CronResultClassification(False, "failed", rc, "cron_job_timed_out")

    hard_stderr = any(
        marker.lower() in clean_stderr.lower()
        for marker in _HARD_FAILURE_MARKERS
    )
    if hard_stderr:
        return CronResultClassification(False, "failed", rc, clean_stderr[-500:])

    structured_status = str((obj or {}).get("status") or "").strip().lower()
    strict_structured_deferred = bool(
        obj is not None
        and structured_status in _DEFERRED_STATUSES
        and obj.get("success") is False
        and obj.get("deferred") is True
        and obj.get("partial") is not True
    )
    if strict_structured_deferred and rc in {0, 75}:
        return CronResultClassification(
            False,
            "deferred",
            rc,
            _contract_error(obj),
        )

    # A real child failure outranks wrapper text.  This prevents a stale or
    # mixed "resource guard skipped" line from hiding the rc/stderr emitted by
    # a child that actually ran.
    if rc != 0:
        error = clean_stderr or clean_stdout or f"process_exit_{rc}"
        return CronResultClassification(False, "failed", rc, error[-500:])

    if _is_resource_guard_skip(combined, obj):
        return CronResultClassification(False, "deferred", rc, "resource_guard_skipped")

    if obj is not None:
        contract_value = obj.get("success", obj.get("ok"))
        if contract_value is False:
            return CronResultClassification(False, "failed", rc, _contract_error(obj))
        if contract_value is True:
            # A zero-exit, explicit top-level contract is authoritative.  Many
            # maintained jobs intentionally log recoverable sub-step errors or
            # optional-node warnings to stderr before returning ``ok: true``.
            # Hard interpreter failures remain failures even with stale JSON.
            if any(marker.lower() in clean_stderr.lower() for marker in _HARD_FAILURE_MARKERS):
                return CronResultClassification(False, "failed", rc, clean_stderr[-500:])
            return CronResultClassification(True, "success", rc)

    if any(marker in clean_stderr for marker in _FAILURE_MARKERS):
        return CronResultClassification(False, "failed", rc, clean_stderr[-500:])

    if _UNKNOWN_TASK_RE.search(combined):
        return CronResultClassification(False, "failed", rc, "unknown_task")

    if macro_response:
        if not clean_stdout:
            return CronResultClassification(False, "failed", rc, "empty_magi_response")
        if _NO_ACTION_RESPONSE_RE.search(clean_stdout):
            return CronResultClassification(False, "failed", rc, "clarification_without_action")

    return CronResultClassification(True, "success", rc)


def looks_successful_despite_returncode(stdout: str, stderr: str) -> bool:
    """Return True when output is strong evidence to suppress an issue item."""
    clean_stdout = (stdout or "").strip()
    clean_stderr = (stderr or "").strip()
    combined = f"{clean_stdout}\n{clean_stderr}"
    if clean_stderr:
        return False
    if any(marker in combined for marker in _FAILURE_MARKERS):
        return False
    obj = _last_json_object(stdout)
    if obj:
        success = obj.get("success")
        ok = obj.get("ok")
        if success is True or ok is True:
            severity = str(obj.get("severity") or "").upper()
            alarm_triggered = obj.get("alarm_triggered")
            if severity in {"", "OK", "INFO"} and alarm_triggered in {None, False}:
                return True
    if clean_stdout and not clean_stderr:
        if any(marker in clean_stdout for marker in _SUCCESS_MARKERS):
            return True
    return False


def should_log_cron_issue(returncode: int, stdout: str, stderr: str) -> bool:
    """Decide whether a non-zero cron result should become an issue agenda item."""
    classification = classify_cron_result(returncode, stdout, stderr)
    if classification.status == "deferred":
        return False
    if int(returncode or 0) == 0:
        return not classification.success
    return not looks_successful_despite_returncode(stdout or "", stderr or "")

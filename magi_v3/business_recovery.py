"""Shared recovery policy for MAGI business work.

Business modules may have their own fine-grained queues, but every scheduled
owner still crosses this boundary.  The policy deliberately separates a
recoverable machine condition from a condition that genuinely requires a
person.  Recoverable failures are durably retried and must not be presented as
an unexplained red light on their first occurrence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping


_TRANSIENT_MARKERS = (
    "timeout",
    "timed out",
    "temporarily unavailable",
    "temporary failure",
    "connection refused",
    "connection reset",
    "connection closed",
    "network is unreachable",
    "service unavailable",
    "upstream",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "resource_guard_skipped",
    "storage_unavailable",
    "device not configured",
    "socket is not connected",
    "stale file handle",
    "portal_busy",
    "portal_probe_deferred",
    "popup_processing_timeout",
    "direct_download_incomplete",
    "case_identity_mismatch",
    "invalid csrf token",
    "csrf token expired",
    "session expired",
    "login session expired",
    "singleton",
    "lock busy",
    "already running",
)

_HUMAN_MARKERS = (
    "manual_action_required",
    "manual_required",
    "needs_human",
    "human_required",
    "invalid credentials",
    "invalid_grant",
    "reauthorization_required",
    "interactive_login_required",
    "permission denied",
    "missing required document",
    "missing source document",
    "ambiguous identity",
    "semantic_path_collision_requires_human_review",
)

# These jobs change the host execution topology.  Their own sealed transition
# scripts already have rollback contracts and should never be relaunched by a
# generic business retry policy.
_GENERIC_RETRY_EXCLUDED_JOB_IDS = frozenset(
    {
        "job_reboot_before_day_model_switch",
        "job_reboot_before_night_model_switch",
        "job_omlx_switch_day",
        "job_omlx_switch_night",
    }
)

_REASON_LABELS = {
    "portal_busy": "外部入口正由另一項作業使用",
    "storage_unavailable": "網路儲存裝置暫時無法使用",
    "upstream_unavailable": "外部服務暫時無法使用",
    "timeout": "作業未在本輪時限內完成",
    "identity_guard": "資料身分檢查已阻擋不安全寫入",
    "process_interrupted": "作業程序中斷",
    "transient_failure": "暫時性執行問題",
    "business_failure": "業務作業未完成",
    "human_input_required": "需要人類提供資料、登入或作決定",
}


@dataclass(frozen=True, slots=True)
class RecoveryDecision:
    retryable: bool
    human_required: bool
    reason_code: str
    public_reason: str
    max_attempts: int
    retry_delays_seconds: tuple[int, ...]
    business_domain: str = ""

    def delay_for_attempt(self, attempt: int) -> int:
        if not self.retry_delays_seconds:
            return 60
        index = max(0, min(len(self.retry_delays_seconds) - 1, int(attempt) - 1))
        return int(self.retry_delays_seconds[index])


def _last_json_object(text: str) -> dict[str, Any] | None:
    stripped = str(text or "").strip()
    if not stripped:
        return None
    decoder = json.JSONDecoder()
    for start in reversed([idx for idx, char in enumerate(stripped) if char == "{"]):
        try:
            value, end = decoder.raw_decode(stripped[start:])
        except Exception:
            continue
        if isinstance(value, dict) and stripped[start + end :].strip() in {"", "```"}:
            return value
    return None


def _bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


_CATALOG_PATH = Path(__file__).resolve().parents[1] / "config" / "business_recovery_contracts.json"


@lru_cache(maxsize=4)
def load_recovery_catalog(path: str = "") -> dict[str, Any]:
    target = Path(path).expanduser() if path else _CATALOG_PATH
    payload = json.loads(target.read_text(encoding="utf-8"))
    if payload.get("schema") != "magi.business-recovery-contracts/v1":
        raise ValueError("business recovery catalog schema is invalid")
    if not isinstance(payload.get("defaults"), dict) or not isinstance(payload.get("domains"), dict):
        raise ValueError("business recovery catalog structure is invalid")
    return payload


def contract_for_job(job_id: str, *, catalog_path: str = "") -> tuple[str, dict[str, Any]]:
    jid = str(job_id or "").strip()
    if not jid:
        return "", {}
    try:
        catalog = load_recovery_catalog(catalog_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return "", {}
    for domain, raw in catalog["domains"].items():
        contract = raw if isinstance(raw, dict) else {}
        owners = contract.get("owner_job_ids")
        if isinstance(owners, list) and jid in owners:
            merged = dict(catalog["defaults"])
            merged.update(contract)
            return str(domain), merged
    return "", {}


def audit_recovery_catalog(
    jobs: list[Mapping[str, Any]], *, catalog_path: str = ""
) -> dict[str, Any]:
    """Prove that every declared business owner and verifier is deployable."""

    try:
        catalog = load_recovery_catalog(catalog_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return {
            "ok": False,
            "success": False,
            "status": "failed",
            "action_required": True,
            "error": f"recovery_catalog_unreadable:{type(exc).__name__}",
        }
    indexed = {
        str(job.get("id") or "").strip(): job
        for job in jobs
        if isinstance(job, Mapping) and str(job.get("id") or "").strip()
    }
    issues: list[dict[str, str]] = []
    seen_owners: dict[str, str] = {}
    owner_count = 0
    verifier_count = 0
    for domain, raw in catalog["domains"].items():
        contract = raw if isinstance(raw, dict) else {}
        owners = contract.get("owner_job_ids")
        verifiers = contract.get("verification_job_ids")
        if not isinstance(owners, list) or not owners:
            issues.append({"domain": str(domain), "issue": "missing_owner_jobs"})
            continue
        if not isinstance(verifiers, list) or not verifiers:
            issues.append({"domain": str(domain), "issue": "missing_verification_jobs"})
        for job_id in owners:
            jid = str(job_id or "").strip()
            owner_count += 1
            previous = seen_owners.setdefault(jid, str(domain))
            if previous != str(domain):
                issues.append({"domain": str(domain), "job_id": jid, "issue": "duplicate_owner_domain"})
            job = indexed.get(jid)
            if job is None:
                issues.append({"domain": str(domain), "job_id": jid, "issue": "owner_missing"})
            elif job.get("enabled") is not True:
                issues.append({"domain": str(domain), "job_id": jid, "issue": "owner_disabled"})
            elif not str(job.get("command") or "").strip():
                issues.append({"domain": str(domain), "job_id": jid, "issue": "owner_command_missing"})
            if jid in _GENERIC_RETRY_EXCLUDED_JOB_IDS:
                issues.append({"domain": str(domain), "job_id": jid, "issue": "unsafe_topology_owner"})
        for job_id in verifiers if isinstance(verifiers, list) else []:
            jid = str(job_id or "").strip()
            verifier_count += 1
            job = indexed.get(jid)
            if job is None:
                issues.append({"domain": str(domain), "job_id": jid, "issue": "verifier_missing"})
            elif job.get("enabled") is not True:
                issues.append({"domain": str(domain), "job_id": jid, "issue": "verifier_disabled"})
    ok = not issues
    return {
        "ok": ok,
        "success": ok,
        "status": "passed" if ok else "failed",
        "action_required": not ok,
        "domain_count": len(catalog["domains"]),
        "owner_count": owner_count,
        "verifier_count": verifier_count,
        "issues": issues,
    }


def _retry_delays(job: Mapping[str, Any], contract: Mapping[str, Any]) -> tuple[int, ...]:
    raw = job.get("auto_retry_delays_seconds")
    if not isinstance(raw, (list, tuple)):
        raw = contract.get("retry_delays_seconds")
    if isinstance(raw, (list, tuple)):
        values = tuple(
            _bounded_int(item, 60, 15, 3600)
            for item in raw
            if not isinstance(item, bool)
        )
        if values:
            return values[:6]
    return (60, 300, 900)


def _reason_code(text: str, *, timed_out: bool) -> str:
    lowered = text.lower()
    if timed_out or "timeout" in lowered or "timed out" in lowered:
        return "timeout"
    if any(marker in lowered for marker in ("case_identity_mismatch", "identity mismatch")):
        return "identity_guard"
    if any(
        marker in lowered
        for marker in (
            "invalid csrf token",
            "csrf token expired",
            "session expired",
            "login session expired",
        )
    ):
        return "transient_failure"
    if any(marker in lowered for marker in ("portal_busy", "singleton", "lock busy", "already running")):
        return "portal_busy"
    if any(marker in lowered for marker in ("storage_unavailable", "device not configured", "stale file handle")):
        return "storage_unavailable"
    if any(
        marker in lowered
        for marker in (
            "upstream",
            "service unavailable",
            "temporarily unavailable",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "connection refused",
            "connection reset",
            "connection closed",
        )
    ):
        return "upstream_unavailable"
    if any(marker in lowered for marker in ("sigterm", "sigkill", "process_exit", "interrupted")):
        return "process_interrupted"
    if any(marker in lowered for marker in _TRANSIENT_MARKERS):
        return "transient_failure"
    return "business_failure"


def decide_recovery(
    job: Mapping[str, Any],
    *,
    returncode: int | None,
    stdout: str = "",
    stderr: str = "",
    error: str = "",
    status: str = "",
    timed_out: bool = False,
) -> RecoveryDecision:
    """Return the bounded recovery action for one failed business occurrence."""

    payload = _last_json_object(stdout) or {}
    # Classification is determined by explicit top-level semantic values.
    # Scanning the complete JSON stdout also scans harmless field names such
    # as ``semantic_collision_files: 0`` and ``timed_out: false``; those used
    # to create false human-required and timeout states.
    semantic_text = "\n".join(
        str(value or "")
        for value in (
            status,
            error,
            stderr,
            payload.get("reason"),
            payload.get("error"),
            payload.get("message"),
            stdout if not payload else "",
        )
    ).lower()
    job_id = str(job.get("id") or "").strip()
    business_domain, contract = contract_for_job(job_id)
    max_attempts = _bounded_int(
        job.get("auto_retry_max_attempts", contract.get("max_attempts")), 3, 1, 6
    )
    delays = _retry_delays(job, contract)

    explicit_human = any(
        _bool(payload.get(key)) is True
        for key in ("action_required", "manual_required", "human_required", "needs_human")
    )
    if explicit_human or any(marker in semantic_text for marker in _HUMAN_MARKERS):
        return RecoveryDecision(
            retryable=False,
            human_required=True,
            reason_code="human_input_required",
            public_reason=_REASON_LABELS["human_input_required"],
            max_attempts=max_attempts,
            retry_delays_seconds=delays,
            business_domain=business_domain,
        )

    explicit_retryable = _bool(payload.get("retryable"))
    disabled = _bool(job.get("auto_recover")) is False
    topology_job = job_id in _GENERIC_RETRY_EXCLUDED_JOB_IDS
    if disabled or topology_job or explicit_retryable is False:
        reason = _reason_code(semantic_text, timed_out=timed_out)
        return RecoveryDecision(
            retryable=False,
            human_required=False,
            reason_code=reason,
            public_reason=_REASON_LABELS[reason],
            max_attempts=max_attempts,
            retry_delays_seconds=delays,
            business_domain=business_domain,
        )

    structured_status = str(payload.get("status") or status or "").strip().lower()
    retryable = bool(
        explicit_retryable is True
        or timed_out
        or structured_status in {"deferred", "retrying", "retry_pending", "partial"}
        or int(returncode or 0) in {75, 124, 130}
        or any(marker in semantic_text for marker in _TRANSIENT_MARKERS)
        # Declared business owners are required to be repeat-safe at the
        # scheduler boundary.  Unknown failures are retried only for those
        # owners (or an explicit opt-in), never for arbitrary maintenance or
        # topology commands.
        or bool(business_domain)
        or _bool(job.get("auto_recover")) is True
    )
    reason = _reason_code(semantic_text, timed_out=timed_out)
    return RecoveryDecision(
        retryable=retryable,
        human_required=False,
        reason_code=reason,
        public_reason=_REASON_LABELS[reason],
        max_attempts=max_attempts,
        retry_delays_seconds=delays,
        business_domain=business_domain,
    )


def retry_status_message(decision: RecoveryDecision, *, attempt: int, retry_at: str) -> str:
    return (
        f"MAGI 已自動接手：{decision.public_reason}；"
        f"已安全保留本輪狀態，將進行第 {attempt}/{decision.max_attempts} 次重試"
        f"（{retry_at}）。"
    )


def exhausted_status_message(decision: RecoveryDecision) -> str:
    return (
        f"MAGI 已完成 {decision.max_attempts} 次自動修復但仍未恢復。"
        f"目前已停止可能重複寫入並保留證據；需要處理：{decision.public_reason}。"
    )


def human_status_message(decision: RecoveryDecision) -> str:
    return (
        "MAGI 已完成可安全執行的檢查並保留現況；"
        f"此項無法由系統自行決定：{decision.public_reason}。"
    )

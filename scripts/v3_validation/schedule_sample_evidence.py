"""Canonical, non-sensitive evidence for one bounded schedule-body execution.

The schedule registry keeps this ledger in the release evidence so G11 can
recompute the three-sample commitment.  It intentionally contains only
booleans, counts, return/duration values, controlled relative paths, and hashes;
raw provider output, absolute paths, SQL, and document contents stay in the
disposable fixture.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any


SCHEMA = "magi.v3.schedule-body-sample-evidence/v2"
CONTRACT_DIAGNOSTIC_SCHEMA = "magi.v3.schedule-contract-diagnostic/v1"
GENERIC_CONTRACT_DIAGNOSTIC_KIND = "generic_success_contract"
SYSTEM_DIAGNOSTIC_KIND = "system_diagnostic_terminal"
SYSTEM_DIAGNOSTIC_JOB_ID = "job_1770699415"
SYSTEM_DIAGNOSTIC_JOB_IDS = frozenset(
    {
        SYSTEM_DIAGNOSTIC_JOB_ID,
        "job_optimize_report",
    }
)
SYSTEM_DIAGNOSTIC_RESOURCE_WARNING_CODES = frozenset(
    {
        "disk_free_below_10_gib",
        "memory_free_below_15_percent",
    }
)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
EXECUTION_KINDS = {
    "inherited_real_entrypoint_dry_run_v1",
    "reviewed_real_entrypoint_fixture_v1",
}


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _collect_hashes(value: Any, *, receipt_only: bool = False, path: str = "") -> list[str]:
    hashes: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            hashes.extend(
                _collect_hashes(
                    child,
                    receipt_only=receipt_only,
                    path=child_path,
                )
            )
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, child in enumerate(value):
            hashes.extend(
                _collect_hashes(
                    child,
                    receipt_only=receipt_only,
                    path=f"{path}[{index}]",
                )
            )
    elif isinstance(value, str) and HEX64.fullmatch(value):
        if not receipt_only or "receipt" in path.lower():
            hashes.append(value)
    return sorted(hashes)


def _contract_summary(raw: Any, *, semantic_success: bool) -> dict[str, Any]:
    diagnostic_schema = (
        raw.get("diagnostic_schema")
        if isinstance(raw, Mapping)
        else None
    )
    diagnostic_kind = (
        raw.get("diagnostic_kind")
        if isinstance(raw, Mapping)
        else None
    )
    if diagnostic_schema is None:
        diagnostic_schema = CONTRACT_DIAGNOSTIC_SCHEMA
    if diagnostic_kind is None:
        diagnostic_kind = GENERIC_CONTRACT_DIAGNOSTIC_KIND
    checks_raw = raw.get("checks") if isinstance(raw, Mapping) else None
    checks = (
        {str(key): value for key, value in checks_raw.items() if type(value) is bool}
        if isinstance(checks_raw, Mapping)
        else {"semantic_success": semantic_success}
    )
    artifact_hashes = _collect_hashes(raw)
    receipt_hashes = _collect_hashes(raw, receipt_only=True)
    summary: dict[str, Any] = {
        "diagnostic_schema": diagnostic_schema,
        "diagnostic_kind": diagnostic_kind,
        "checks": dict(sorted(checks.items())),
        "check_count": len(checks),
        "passed_check_count": sum(value is True for value in checks.values()),
        "artifact_sha256s": artifact_hashes,
        "artifact_sha256_count": len(artifact_hashes),
        "receipt_sha256s": receipt_hashes,
        "receipt_sha256_count": len(receipt_hashes),
    }
    observed_status = raw.get("observed_status") if isinstance(raw, Mapping) else None
    warning_codes = raw.get("warnings") if isinstance(raw, Mapping) else None
    if (
        diagnostic_kind == SYSTEM_DIAGNOSTIC_KIND
        or observed_status is not None
        or warning_codes is not None
    ):
        accepted_warning_codes = (
            raw.get("accepted_warning_codes")
            if isinstance(raw, Mapping)
            else None
        )
        summary.update(
            {
                "observed_status": (
                    observed_status if isinstance(observed_status, str) else None
                ),
                "warning_codes": (
                    list(warning_codes)
                    if isinstance(warning_codes, list)
                    and all(isinstance(value, str) and value for value in warning_codes)
                    else None
                ),
                "warning_count": (
                    len(warning_codes)
                    if isinstance(warning_codes, list)
                    and all(isinstance(value, str) and value for value in warning_codes)
                    else None
                ),
                "accepted_warning_codes": (
                    list(accepted_warning_codes)
                    if isinstance(accepted_warning_codes, list)
                    and all(
                        isinstance(value, str) and value
                        for value in accepted_warning_codes
                    )
                    else None
                ),
            }
        )
    summary["evidence_sha256"] = canonical_sha256(summary)
    return summary


def _dependency_summary(raw: Any) -> dict[str, Any]:
    dependency = raw if isinstance(raw, Mapping) else {}
    kind = str(dependency.get("kind") or "none")
    request_count = dependency.get("request_count", 0)
    postcondition_count = dependency.get("postcondition_count", 0)
    passed_postcondition_count = dependency.get("passed_postcondition_count", 0)
    summary: dict[str, Any] = {
        "dependency_present": kind != "none",
        "request_count": int(request_count) if type(request_count) is int else -1,
        "expected_request_kind_count": (
            len(dependency.get("request_counts") or {})
            if isinstance(dependency.get("request_counts"), Mapping)
            else 0
        ),
        "expected_requests_satisfied": dependency.get(
            "expected_requests_satisfied", True
        )
        is True,
        "postcondition_count": (
            int(postcondition_count) if type(postcondition_count) is int else -1
        ),
        "passed_postcondition_count": (
            int(passed_postcondition_count)
            if type(passed_postcondition_count) is int
            else -1
        ),
        "postconditions_passed": dependency.get("postconditions_passed", True)
        is True,
        "transcript_sha256": dependency.get("transcript_sha256"),
        "postconditions_sha256": dependency.get("postconditions_sha256"),
    }
    summary["evidence_sha256"] = canonical_sha256(summary)
    return summary


def build_sample_evidence(
    raw: Mapping[str, Any],
    *,
    sample_index: int,
    execution_kind: str,
    entrypoint_sha256: str,
) -> dict[str, Any]:
    """Reduce a raw disposable execution to a canonical safe evidence row."""

    if execution_kind not in EXECUTION_KINDS:
        raise ValueError("unknown schedule sample execution kind")
    semantic_success = raw.get("semantic_success") is True
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "sample_index": sample_index,
        "execution_kind": execution_kind,
        "execution_nonce_sha256": raw.get("execution_nonce_sha256"),
        "status": str(raw.get("status") or ""),
        "executed": raw.get("executed") is True,
        "returncode": raw.get("returncode"),
        "duration_seconds": raw.get("duration_seconds"),
        "semantic_success": semantic_success,
        "entrypoint_sha256": entrypoint_sha256,
        "sandbox_profile_sha256": raw.get("sandbox_profile_sha256"),
        "stdout_sha256": raw.get("stdout_sha256"),
        "stderr_sha256": raw.get("stderr_sha256"),
        "diagnostic_evidence_relative_path": raw.get(
            "diagnostic_evidence_relative_path"
        ),
        "diagnostic_evidence_sha256": raw.get("diagnostic_evidence_sha256"),
        "fixture_binding_sha256": raw.get("fixture_binding_sha256"),
        "fixture_initial_inventory_sha256": raw.get(
            "fixture_initial_inventory_sha256"
        ),
        "fixture_final_inventory_sha256": raw.get(
            "fixture_final_inventory_sha256"
        ),
        "fixture_final_file_count": raw.get("fixture_final_file_count"),
        "no_fixture_symlinks": raw.get("no_fixture_symlinks"),
        "success_contract_evidence": _contract_summary(
            raw.get("success_contract_evidence"),
            semantic_success=semantic_success,
        ),
        "dependency_evidence": _dependency_summary(raw.get("dependency_evidence")),
        "adapter_mode": str(raw.get("adapter_mode") or ""),
        "network_denied_by_seatbelt": raw.get("network_denied_by_seatbelt")
        is True,
        "notifications_disabled": raw.get("notifications_disabled") is True,
    }
    record["evidence_sha256"] = canonical_sha256(record)
    return record


def _valid_summary_hash(summary: Any) -> bool:
    if not isinstance(summary, Mapping):
        return False
    unsigned = dict(summary)
    supplied = str(unsigned.pop("evidence_sha256", ""))
    return bool(HEX64.fullmatch(supplied) and canonical_sha256(unsigned) == supplied)


def verify_sample_evidence_ledger(
    result: Mapping[str, Any],
    *,
    minimum_samples: int,
) -> bool:
    """Recompute and validate the per-sample ledger carried by one job result."""

    samples = result.get("sample_evidence")
    durations = result.get("duration_samples_seconds")
    if (
        not isinstance(samples, list)
        or len(samples) != minimum_samples
        or not isinstance(durations, list)
        or len(durations) != minimum_samples
        or result.get("sample_evidence_sha256") != canonical_sha256(samples)
    ):
        return False
    expected_entrypoint = str(result.get("entrypoint_sha256") or "")
    expected_adapter_mode = str(result.get("adapter_mode") or "")
    nonces: set[str] = set()
    for expected_index, (sample, duration) in enumerate(
        zip(samples, durations, strict=True), 1
    ):
        if not isinstance(sample, Mapping):
            return False
        unsigned = dict(sample)
        supplied = str(unsigned.pop("evidence_sha256", ""))
        contract = sample.get("success_contract_evidence")
        dependency = sample.get("dependency_evidence")
        nonce = str(sample.get("execution_nonce_sha256") or "")
        if (
            not HEX64.fullmatch(supplied)
            or canonical_sha256(unsigned) != supplied
            or sample.get("schema") != SCHEMA
            or sample.get("sample_index") != expected_index
            or sample.get("execution_kind") not in EXECUTION_KINDS
            or not HEX64.fullmatch(nonce)
            or nonce in nonces
            or sample.get("status") != "passed"
            or sample.get("executed") is not True
            or sample.get("returncode") != 0
            or sample.get("semantic_success") is not True
            or sample.get("duration_seconds") != duration
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or duration <= 0
            or sample.get("entrypoint_sha256") != expected_entrypoint
            or not HEX64.fullmatch(expected_entrypoint)
            or sample.get("adapter_mode") != expected_adapter_mode
            or any(
                not HEX64.fullmatch(str(sample.get(field) or ""))
                for field in (
                    "sandbox_profile_sha256",
                    "stdout_sha256",
                    "stderr_sha256",
                    "diagnostic_evidence_sha256",
                    "fixture_binding_sha256",
                )
            )
            or sample.get("diagnostic_evidence_relative_path")
            != "diagnostics/execution.json"
            or sample.get("network_denied_by_seatbelt") is not True
            or sample.get("notifications_disabled") is not True
            or not _valid_summary_hash(contract)
            or not _valid_summary_hash(dependency)
        ):
            return False
        diagnostic_schema = contract.get("diagnostic_schema")
        diagnostic_kind = contract.get("diagnostic_kind")
        system_diagnostic_job = (
            str(result.get("job_id") or "") in SYSTEM_DIAGNOSTIC_JOB_IDS
        )
        if (
            diagnostic_schema != CONTRACT_DIAGNOSTIC_SCHEMA
            or diagnostic_kind
            not in {
                GENERIC_CONTRACT_DIAGNOSTIC_KIND,
                SYSTEM_DIAGNOSTIC_KIND,
            }
            or (
                system_diagnostic_job
                and diagnostic_kind != SYSTEM_DIAGNOSTIC_KIND
            )
            or (
                not system_diagnostic_job
                and diagnostic_kind == SYSTEM_DIAGNOSTIC_KIND
            )
        ):
            return False
        if system_diagnostic_job:
            observed_status = contract.get("observed_status")
            warning_codes = contract.get("warning_codes")
            accepted_warning_codes = contract.get("accepted_warning_codes")
            if (
                "observed_status" not in contract
                or "warning_codes" not in contract
                or observed_status not in {"healthy", "warning"}
                or not isinstance(warning_codes, list)
                or any(not isinstance(value, str) or not value for value in warning_codes)
                or len(set(warning_codes)) != len(warning_codes)
                or set(warning_codes)
                - SYSTEM_DIAGNOSTIC_RESOURCE_WARNING_CODES
                or accepted_warning_codes
                != sorted(SYSTEM_DIAGNOSTIC_RESOURCE_WARNING_CODES)
                or contract.get("warning_count") != len(warning_codes)
                or (observed_status == "healthy" and warning_codes)
                or (observed_status == "warning" and not warning_codes)
            ):
                return False
        elif "observed_status" in contract or "warning_codes" in contract:
            return False
        nonces.add(nonce)
        checks = contract.get("checks") if isinstance(contract, Mapping) else None
        artifacts = (
            contract.get("artifact_sha256s") if isinstance(contract, Mapping) else None
        )
        receipts = (
            contract.get("receipt_sha256s") if isinstance(contract, Mapping) else None
        )
        if (
            not isinstance(checks, Mapping)
            or not checks
            or any(type(value) is not bool or value is not True for value in checks.values())
            or contract.get("check_count") != len(checks)
            or contract.get("passed_check_count") != len(checks)
            or not isinstance(artifacts, list)
            or contract.get("artifact_sha256_count") != len(artifacts)
            or any(not HEX64.fullmatch(str(value or "")) for value in artifacts)
            or not isinstance(receipts, list)
            or contract.get("receipt_sha256_count") != len(receipts)
            or any(not HEX64.fullmatch(str(value or "")) for value in receipts)
        ):
            return False
        post_count = dependency.get("postcondition_count")
        passed_post_count = dependency.get("passed_postcondition_count")
        if (
            dependency.get("expected_requests_satisfied") is not True
            or dependency.get("postconditions_passed") is not True
            or type(dependency.get("request_count")) is not int
            or dependency.get("request_count") < 0
            or type(dependency.get("expected_request_kind_count")) is not int
            or dependency.get("expected_request_kind_count") < 0
            or type(post_count) is not int
            or post_count < 0
            or passed_post_count != post_count
            or not HEX64.fullmatch(str(dependency.get("transcript_sha256") or ""))
            or not HEX64.fullmatch(
                str(dependency.get("postconditions_sha256") or "")
            )
        ):
            return False
        if sample.get("execution_kind") == "reviewed_real_entrypoint_fixture_v1":
            if (
                any(
                    not HEX64.fullmatch(str(sample.get(field) or ""))
                    for field in (
                        "fixture_initial_inventory_sha256",
                        "fixture_final_inventory_sha256",
                    )
                )
                or type(sample.get("fixture_final_file_count")) is not int
                or sample.get("fixture_final_file_count") < 1
                or sample.get("no_fixture_symlinks") is not True
            ):
                return False
    return (
        result.get("sample_statuses") == ["passed"] * minimum_samples
        and result.get("sandbox_profile_sha256_samples")
        == [row["sandbox_profile_sha256"] for row in samples]
        and result.get("stdout_sha256_samples")
        == [row["stdout_sha256"] for row in samples]
        and result.get("stderr_sha256_samples")
        == [row["stderr_sha256"] for row in samples]
    )

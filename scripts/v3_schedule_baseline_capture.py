"""Capture a redacted, hash-bound scheduler-duration baseline.

The capture reads production scheduler state but never copies commands,
stdout/stderr, paths, or arbitrary state fields into the release evidence.
Each job contributes only its id and a rolling, de-duplicated ledger of at
most 30 successful durations/timestamps.  A percentile is emitted only after
at least three independent observations exist for that job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SCHEDULER_TIMEZONE = ZoneInfo("Asia/Taipei")
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.v3_campaign.schedule_realism import (
    BASELINE_SCHEMA_VERSION,
    COMMAND_CHANGE_INVALIDATION_REASON,
    OBSERVATION_COMMAND_BINDING,
    SOURCE_EVIDENCE_RECEIPT_FIELD,
    _command_definition_sha256,
    _logical_definition_sha256,
    _source_evidence_receipt_sha256,
)


class BaselineCaptureError(ValueError):
    """Raised when scheduler evidence cannot be captured safely."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BaselineCaptureError(f"unreadable JSON evidence: {path}") from exc


def _normalized_timestamp(value: Any, *, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise BaselineCaptureError(f"{field} is missing")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BaselineCaptureError(f"{field} is not valid ISO-8601") from exc
    # The production V2 scheduler persists ``datetime.now().isoformat()`` and
    # therefore emits naive local timestamps.  Its deployment timezone is
    # fixed to Asia/Taipei; make that legacy contract explicit before UTC
    # normalization so equivalent offsets cannot be counted twice.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_SCHEDULER_TIMEZONE)
    return parsed.astimezone(timezone.utc).isoformat()


def _successful_sample(
    raw: Mapping[str, Any],
    *,
    command_sha256: str,
) -> dict[str, Any] | None:
    duration = raw.get("last_duration_sec")
    if (
        raw.get("last_success") is not True
        or raw.get("last_timed_out") is True
        or raw.get("last_returncode") not in {None, 0}
        or not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or duration <= 0
        or raw.get("command_sha256") != command_sha256
    ):
        return None
    observed_at_raw = (
        raw.get("last_success_at")
        or raw.get("last_result_at")
        or raw.get("last_complete_at")
        or ""
    )
    observed_at = _normalized_timestamp(
        observed_at_raw,
        field="successful scheduler observation timestamp",
    )
    return {
        "duration_seconds": round(float(duration), 6),
        "observed_at": observed_at,
    }


def _prior_samples(
    previous: Mapping[str, Any],
    enabled_jobs: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]]]:
    collected: dict[str, list[dict[str, Any]]] = {}
    invalidated: dict[str, dict[str, Any]] = {}
    enabled_ids = set(enabled_jobs)
    previous_invalidated = previous.get("invalidated_observations")
    if isinstance(previous_invalidated, list):
        for raw in previous_invalidated:
            if not isinstance(raw, Mapping):
                continue
            job_id = str(raw.get("job_id") or "")
            if job_id not in enabled_ids:
                continue
            current_command_sha256 = _command_definition_sha256(
                enabled_jobs[job_id]
            )
            observed_command_sha256 = str(
                raw.get("observed_command_sha256") or ""
            )
            if observed_command_sha256 and observed_command_sha256 != current_command_sha256:
                invalidated[job_id] = {
                    "job_id": job_id,
                    "reason": COMMAND_CHANGE_INVALIDATION_REASON,
                    "observed_command_sha256": observed_command_sha256,
                    "current_command_sha256": current_command_sha256,
                    "invalidated_sample_count": int(
                        raw.get("invalidated_sample_count") or 0
                    ),
                    "last_observed_at": str(raw.get("last_observed_at") or ""),
                }
    observations = previous.get("observations")
    if not isinstance(observations, list):
        return collected, invalidated
    for raw in observations:
        if not isinstance(raw, Mapping):
            continue
        job_id = str(raw.get("job_id") or "")
        if job_id not in enabled_ids:
            continue
        observed_command_sha256 = str(raw.get("command_sha256") or "")
        current_command_sha256 = _command_definition_sha256(enabled_jobs[job_id])
        if observed_command_sha256 != current_command_sha256:
            if observed_command_sha256:
                invalidated[job_id] = {
                    "job_id": job_id,
                    "reason": COMMAND_CHANGE_INVALIDATION_REASON,
                    "observed_command_sha256": observed_command_sha256,
                    "current_command_sha256": current_command_sha256,
                    "invalidated_sample_count": int(raw.get("sample_count") or 0),
                    "last_observed_at": str(raw.get("observed_at") or ""),
                }
            continue
        raw_samples = raw.get("samples")
        if isinstance(raw_samples, list):
            candidates = raw_samples
        else:
            candidates = [
                {
                    "duration_seconds": raw.get("duration_seconds"),
                    "observed_at": raw.get("observed_at"),
                }
            ]
        valid_by_time: dict[str, float] = {}
        for sample in candidates:
            if not isinstance(sample, Mapping):
                continue
            duration = sample.get("duration_seconds")
            observed_at_raw = sample.get("observed_at")
            if (
                not isinstance(duration, (int, float))
                or isinstance(duration, bool)
                or duration <= 0
            ):
                continue
            observed_at = _normalized_timestamp(
                observed_at_raw,
                field=f"prior scheduler observation timestamp for {job_id}",
            )
            rounded = round(float(duration), 6)
            existing = valid_by_time.get(observed_at)
            if existing is not None and existing != rounded:
                raise BaselineCaptureError(
                    f"conflicting durations share one observation timestamp for {job_id}"
                )
            valid_by_time[observed_at] = rounded
        valid = [
            {"duration_seconds": duration, "observed_at": observed_at}
            for observed_at, duration in sorted(valid_by_time.items())
        ]
        if valid:
            collected[job_id] = valid[-30:]
            invalidated.pop(job_id, None)
    return collected, invalidated


def _observation(
    job_id: str,
    samples: list[dict[str, Any]],
    *,
    command_sha256: str,
) -> dict[str, Any]:
    latest = samples[-1]
    durations = sorted(float(item["duration_seconds"]) for item in samples)
    payload: dict[str, Any] = {
        "job_id": job_id,
        "command_sha256": command_sha256,
        "duration_seconds": latest["duration_seconds"],
        "sample_count": len(samples),
        "successful": True,
        "observed_at": latest["observed_at"],
        "baseline_kind": (
            "single_latest_production_observation"
            if len(samples) == 1
            else "rolling_successful_production_observations"
        ),
        "samples": samples,
    }
    if len(samples) >= 3:
        payload["duration_p95_seconds"] = round(
            durations[max(0, math.ceil(0.95 * len(durations)) - 1)],
            6,
        )
    return payload


def capture_baseline(
    *,
    cron_jobs_path: Path,
    runtime_state_path: Path,
    previous_baseline_path: Path,
    captured_at: str | None = None,
) -> dict[str, Any]:
    jobs = _load_json(cron_jobs_path)
    state = _load_json(runtime_state_path)
    previous = _load_json(previous_baseline_path)
    if not isinstance(jobs, list) or not all(isinstance(job, dict) for job in jobs):
        raise BaselineCaptureError("cron jobs must be a list of objects")
    if not isinstance(state, dict):
        raise BaselineCaptureError("scheduler state must be an object")
    if (
        not isinstance(previous, dict)
        or previous.get("schema_version") != BASELINE_SCHEMA_VERSION
    ):
        raise BaselineCaptureError("previous baseline schema is invalid")

    enabled_ids = sorted(
        str(job.get("id") or "") for job in jobs if job.get("enabled") is True
    )
    if not enabled_ids or "" in enabled_ids or len(enabled_ids) != len(set(enabled_ids)):
        raise BaselineCaptureError("enabled cron job ids are missing or duplicated")

    enabled_jobs = {
        str(job["id"]): job
        for job in jobs
        if job.get("enabled") is True
    }
    samples_by_id, invalidated_by_id = _prior_samples(previous, enabled_jobs)
    for job_id in enabled_ids:
        raw = state.get(job_id)
        if isinstance(raw, Mapping):
            command_sha256 = _command_definition_sha256(enabled_jobs[job_id])
            sample = _successful_sample(
                raw,
                command_sha256=command_sha256,
            )
            if sample is not None:
                samples = samples_by_id.setdefault(job_id, [])
                matching = [
                    item for item in samples if item["observed_at"] == sample["observed_at"]
                ]
                if matching and matching[0]["duration_seconds"] != sample["duration_seconds"]:
                    raise BaselineCaptureError(
                        f"runtime and prior baseline disagree at one timestamp for {job_id}"
                    )
                if not matching:
                    samples.append(sample)
                    samples.sort(key=lambda item: item["observed_at"])
                    del samples[:-30]
                invalidated_by_id.pop(job_id, None)
    observations = [
        _observation(
            job_id,
            samples_by_id[job_id],
            command_sha256=_command_definition_sha256(enabled_jobs[job_id]),
        )
        for job_id in enabled_ids
        if samples_by_id.get(job_id)
    ]
    observed_ids = {item["job_id"] for item in observations}
    missing_ids = sorted(set(enabled_ids) - observed_ids)
    deep_sample_ids = {
        item["job_id"] for item in observations if int(item["sample_count"]) >= 3
    }
    shallow_sample_jobs = len(enabled_ids) - len(deep_sample_ids)

    allowlist = previous.get("representative_body_allowlist")
    duration_policy = previous.get("duration_policy")
    if not isinstance(allowlist, list) or not isinstance(duration_policy, dict):
        raise BaselineCaptureError("previous baseline policy/allowlist is malformed")
    allowed_ids = {str(item.get("job_id") or "") for item in allowlist if isinstance(item, dict)}
    if not allowed_ids or not allowed_ids.issubset(enabled_ids):
        raise BaselineCaptureError("representative body allowlist references an invalid job")

    known_gaps: list[dict[str, Any]] = []
    if missing_ids:
        known_gaps.append(
            {
                "code": "DURATION_COVERAGE_INCOMPLETE",
                "affected_enabled_jobs": len(missing_ids),
                "detail": "No successful last_duration_sec observation exists in the captured scheduler state.",
            }
        )
    if shallow_sample_jobs:
        known_gaps.append(
            {
                "code": "DURATION_SAMPLE_DEPTH_INCOMPLETE",
                "affected_enabled_jobs": shallow_sample_jobs,
                "detail": "Fewer than three successful duration samples exist for these jobs, so their production p95 cannot be claimed.",
            }
        )
    body_gaps = len(enabled_ids) - len(allowed_ids)
    if body_gaps:
        known_gaps.append(
            {
                "code": "REPRESENTATIVE_BODY_COVERAGE_INCOMPLETE",
                "affected_enabled_jobs": body_gaps,
                "detail": f"Only {len(allowed_ids)} exact, side-effect-free commands are approved for the offline sandbox.",
            }
        )

    source_evidence = {
        "job_definitions_path": cron_jobs_path.name,
        "job_definitions_sha256": _sha256_file(cron_jobs_path),
        "logical_definition_sha256": _logical_definition_sha256(jobs),
        "runtime_state_source": runtime_state_path.name,
        "runtime_state_sha256": _sha256_file(runtime_state_path),
        "runtime_state_read_only_snapshot": True,
        "raw_runtime_state_included": False,
        "legacy_naive_timestamp_timezone": "Asia/Taipei",
        "observation_timestamps_normalized_to_utc": True,
        "observation_command_binding": OBSERVATION_COMMAND_BINDING,
    }
    source_evidence[SOURCE_EVIDENCE_RECEIPT_FIELD] = (
        _source_evidence_receipt_sha256(source_evidence)
    )

    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "status": "incomplete",
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(),
        "source_evidence": source_evidence,
        "coverage": {
            "job_definitions": len(jobs),
            "enabled_job_definitions": len(enabled_ids),
            "enabled_jobs_with_successful_duration": len(observations),
            "enabled_jobs_without_successful_duration": len(missing_ids),
            "minimum_samples_per_job_for_percentile": 3,
            "jobs_meeting_minimum_samples": len(deep_sample_ids),
            "global_duration_percentile_available": len(deep_sample_ids) == len(enabled_ids),
        },
        "observations": observations,
        "invalidated_observations": [
            invalidated_by_id[job_id]
            for job_id in sorted(invalidated_by_id)
            if job_id in missing_ids
        ],
        "duration_policy": duration_policy,
        "representative_body_allowlist": allowlist,
        "known_gaps": known_gaps,
    }


def _write_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cron-jobs", type=Path, required=True)
    parser.add_argument("--runtime-state", type=Path, required=True)
    parser.add_argument("--previous-baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        payload = capture_baseline(
            cron_jobs_path=args.cron_jobs.resolve(strict=True),
            runtime_state_path=args.runtime_state.resolve(strict=True),
            previous_baseline_path=args.previous_baseline.resolve(strict=True),
        )
        _write_atomic(args.output.resolve(), payload)
    except (OSError, BaselineCaptureError) as exc:
        parser.exit(2, f"schedule baseline capture failed: {exc}\n")
    print(json.dumps({"output": str(args.output), "coverage": payload["coverage"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

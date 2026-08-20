"""Fail-closed recovery evidence for oMLX profile-switch jobs.

Cron history remains immutable: a later live gate can prove that service has
recovered, but it never rewrites or erases the original failed occurrence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_PROFILES: dict[str, tuple[str, str]] = {
    "day": ("day", "e4b"),
    "day-e4b-degraded": ("day", "e4b"),
    "night": ("night", "26b"),
    "night-12b-degraded": ("night", "12b"),
    "night-e4b-degraded": ("night", "e4b"),
}


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def assess_omlx_recovery(
    runtime_dir: Path,
    *,
    job_id: str,
    failed_at: Any,
) -> dict[str, Any]:
    """Return explicit recovery evidence newer than one failed occurrence."""

    if job_id not in {
        "job_omlx_switch_day",
        "job_omlx_switch_night",
        "job_omlx_profile_guard",
    }:
        return {"recovered": False, "reason": "not_an_omlx_job"}
    failure_epoch = _epoch(failed_at)
    if failure_epoch <= 0:
        return {"recovered": False, "reason": "missing_failure_timestamp"}

    artifact = Path(runtime_dir) / "model_live_gate_latest.json"
    try:
        artifact_epoch = artifact.stat().st_mtime
        payload = json.loads(artifact.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"recovered": False, "reason": "live_gate_unavailable"}
    if artifact_epoch <= failure_epoch:
        return {"recovered": False, "reason": "live_gate_not_newer"}
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return {"recovered": False, "reason": "live_gate_not_green"}
    if payload.get("failures") not in (None, []):
        return {"recovered": False, "reason": "live_gate_has_failures"}

    active_profile = str(payload.get("active_profile") or "").strip().lower()
    profile = _PROFILES.get(active_profile)
    if profile is None:
        return {"recovered": False, "reason": "unknown_active_profile"}
    declared_expected, keyword = profile
    expected = str(payload.get("expected_profile") or "").strip().lower()
    if expected != declared_expected:
        return {"recovered": False, "reason": "profile_expectation_mismatch"}
    if job_id == "job_omlx_switch_day" and expected != "day":
        return {"recovered": False, "reason": "wrong_day_night_profile"}
    if job_id == "job_omlx_switch_night" and expected != "night":
        return {"recovered": False, "reason": "wrong_day_night_profile"}

    endpoints = payload.get("endpoints")
    main = next(
        (
            row
            for row in endpoints or []
            if isinstance(row, dict) and int(row.get("port") or 0) == 8080
        ),
        None,
    )
    if not isinstance(main, dict) or main.get("ok") is not True:
        return {"recovered": False, "reason": "main_model_not_live"}
    model_id = str(main.get("model_id") or "").strip().lower()
    if keyword not in model_id:
        return {"recovered": False, "reason": "model_profile_mismatch"}

    return {
        "recovered": True,
        "reason": f"newer_live_gate:{active_profile}:{model_id}",
        "active_profile": active_profile,
        "model_id": model_id,
        "artifact_mtime": artifact_epoch,
    }

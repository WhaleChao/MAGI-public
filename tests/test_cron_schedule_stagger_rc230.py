from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
from typing import Any

from scripts.seed_cron_jobs import business_jobs, operational_jobs
from scripts.v3_campaign.schedule_realism import (
    _logical_definition_sha256,
    _source_evidence_receipt_sha256,
)


ROOT = Path(__file__).resolve().parents[1]
PROFILE_GUARD = "job_omlx_profile_guard"
JUDGMENT_RESUMMARY = "job_legacy_judgment_resummary_quality"
PROFILE_GUARD_CRON = "3,18,33,48 * * * *"


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {str(row.get("id")): row for row in rows}


def _generated_jobs() -> list[dict[str, Any]]:
    """Return the source-owned V3 schedule definitions without mutable state.

    ``cron_jobs.json`` is deliberately excluded from clean source trees.  It is
    materialized as a hash-bound runtime input during deployment, so a public
    CI checkout must never require a repository-root copy of that mutable file.
    """

    generated = [
        *business_jobs(ROOT, ROOT / "venv/bin/python3"),
        *operational_jobs(ROOT, ROOT / "venv/bin/python3"),
    ]
    assert len({str(row.get("id") or "") for row in generated}) == len(generated)
    return generated


def _bound_runtime_jobs() -> list[dict[str, Any]] | None:
    """Load a formally bound V3 runtime snapshot when the campaign supplies it."""

    names = (
        "MAGI_CRON_JOBS_FILE",
        "MAGI_CRON_JOBS_SHA256",
        "MAGI_CRON_JOBS_SOURCE_SHA256",
    )
    values = tuple(str(os.environ.get(name) or "").strip() for name in names)
    if not any(values):
        return None
    assert all(values), "V3 cron snapshot binding must be complete"
    path = Path(values[0])
    assert path.is_absolute() and path.is_file() and not path.is_symlink()
    payload = path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == values[1]
    policy = json.loads(
        (ROOT / "config/v3_schedule_dispatch_policy.json").read_text(encoding="utf-8")
    )
    assert policy["cron_jobs_sha256"] == values[2]
    rows = json.loads(payload.decode("utf-8"))
    assert isinstance(rows, list) and rows
    return rows


def test_profile_guard_stays_every_fifteen_minutes_without_colliding_with_resummary() -> None:
    generated = _by_id(_generated_jobs())
    bound = _bound_runtime_jobs()
    sources = [generated]
    if bound is not None:
        sources.append(_by_id(bound))

    for source in sources:
        assert source[PROFILE_GUARD]["cron"] == PROFILE_GUARD_CRON
        assert source[PROFILE_GUARD]["cron"] != source[JUDGMENT_RESUMMARY]["cron"]

    enabled_process_jobs = [
        row
        for row in generated.values()
        if row.get("enabled", True)
        and not str(row.get("command") or "").strip().startswith("@MAGI")
    ]
    duplicate_schedules = {
        str(row.get("cron") or "")
        for row in enabled_process_jobs
        if sum(other.get("cron") == row.get("cron") for other in enabled_process_jobs) > 1
    }
    assert duplicate_schedules == set()


def test_schedule_evidence_bindings_follow_the_staggered_cron_snapshot() -> None:
    baseline_path = ROOT / "config/v3_schedule_realism_baseline.json"
    policy = json.loads(
        (ROOT / "config/v3_schedule_dispatch_policy.json").read_text(encoding="utf-8")
    )
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    registry = json.loads(
        (ROOT / "config/v3_schedule_body_adapter_registry.json").read_text(
            encoding="utf-8"
        )
    )

    source_sha = policy["cron_jobs_sha256"]
    assert isinstance(source_sha, str) and len(source_sha) == 64
    assert baseline["source_evidence"]["job_definitions_sha256"] == source_sha
    assert baseline["source_evidence"]["runtime_source_evidence_receipt_sha256"] == (
        _source_evidence_receipt_sha256(baseline["source_evidence"])
    )
    assert registry["release_binding"]["cron_jobs_source_sha256"] == source_sha
    assert registry["release_binding"]["logical_definition_sha256"] == (
        baseline["source_evidence"]["logical_definition_sha256"]
    )
    assert registry["release_binding"]["inherited_baseline_sha256"] == hashlib.sha256(
        baseline_path.read_bytes()
    ).hexdigest()

    bound = _bound_runtime_jobs()
    if bound is not None:
        snapshot_path = Path(os.environ["MAGI_CRON_JOBS_FILE"])
        snapshot_sha = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        assert snapshot_sha == os.environ["MAGI_CRON_JOBS_SHA256"]
        assert source_sha == os.environ["MAGI_CRON_JOBS_SOURCE_SHA256"]
        # The source hash remains stable after release-path rebasing; the
        # runtime logical hash is still computed to prove a parseable snapshot.
        assert len(_logical_definition_sha256(bound)) == 64


def test_drive_all_files_scan_does_not_reintroduce_the_five_megabyte_ceiling() -> None:
    """Ordinary PDFs must remain hash-verifiable instead of looping forever."""

    candidate_sources = [_by_id(_generated_jobs())]
    bound = _bound_runtime_jobs()
    if bound is not None:
        candidate_sources.append(_by_id(bound))

    for source in candidate_sources:
        command = source["job_drive_case_sync_all_files"]["command"]
        assert "MAGI_DRIVE_SYNC_LOCAL_HASH_MAX_BYTES=3000000000" in command
        assert "MAGI_DRIVE_SYNC_MAX_SINGLE_DOWNLOAD_BYTES=1500000000" in command
        assert "MAGI_DRIVE_SYNC_MAX_SINGLE_UPLOAD_BYTES=3000000000" in command
        assert "--max-upload-bytes 3000000000" in command
        assert "MAGI_DRIVE_SYNC_LOCAL_HASH_MAX_BYTES=5000000" not in command
        assert "MAGI_DRIVE_SYNC_LOCAL_SCAN_TIMEOUT_SEC=300" in command
        assert "MAGI_DRIVE_SYNC_LOCAL_HASH_TIMEOUT_SEC=900" in command
        assert "--inventory-timeout-sec 5400" in command
        assert "--timeout-sec 6000" in command
        assert "--direct-all-case-limit 1" in command
        assert "--all-case-chunk-size 1" in command
        assert "--terminal-headroom-sec 300" in command
        assert source["job_drive_case_sync_all_files"]["timeout_sec"] == 6300


def test_laf_nightly_audit_overwrites_stale_portal_timeout() -> None:
    """A slow authenticated portal scan must not inherit an old 960s limit."""

    sources = [_by_id(_generated_jobs())]
    bound = _bound_runtime_jobs()
    if bound is not None:
        sources.append(_by_id(bound))

    for source in sources:
        assert source["job_laf_nightly_audit"]["timeout_sec"] == 1800

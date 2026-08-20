from __future__ import annotations

import json
import hashlib
from pathlib import Path

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


def test_profile_guard_stays_every_fifteen_minutes_without_colliding_with_resummary() -> None:
    generated = _by_id(operational_jobs(ROOT, ROOT / "venv/bin/python3"))
    configured = _by_id(json.loads((ROOT / "cron_jobs.json").read_text(encoding="utf-8")))
    inventory = _by_id(
        json.loads(
            (ROOT / "docs/architecture/v3/generated/v2_inventory.json").read_text(
                encoding="utf-8"
            )
        )["cron_jobs"]
    )

    for source in (generated, configured, inventory):
        assert source[PROFILE_GUARD]["cron"] == PROFILE_GUARD_CRON
        assert source[PROFILE_GUARD]["cron"] != source[JUDGMENT_RESUMMARY]["cron"]

    enabled_process_jobs = [
        row
        for row in configured.values()
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
    cron_path = ROOT / "cron_jobs.json"
    baseline_path = ROOT / "config/v3_schedule_realism_baseline.json"
    jobs = json.loads(cron_path.read_text(encoding="utf-8"))
    cron_sha = hashlib.sha256(cron_path.read_bytes()).hexdigest()
    logical_sha = _logical_definition_sha256(jobs)
    policy = json.loads(
        (ROOT / "config/v3_schedule_dispatch_policy.json").read_text(encoding="utf-8")
    )
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    registry = json.loads(
        (ROOT / "config/v3_schedule_body_adapter_registry.json").read_text(
            encoding="utf-8"
        )
    )

    assert policy["cron_jobs_sha256"] == cron_sha
    assert baseline["source_evidence"]["job_definitions_sha256"] == cron_sha
    assert baseline["source_evidence"]["logical_definition_sha256"] == logical_sha
    assert baseline["source_evidence"]["runtime_source_evidence_receipt_sha256"] == (
        _source_evidence_receipt_sha256(baseline["source_evidence"])
    )
    assert registry["release_binding"]["cron_jobs_source_sha256"] == cron_sha
    assert registry["release_binding"]["logical_definition_sha256"] == logical_sha
    assert registry["release_binding"]["inherited_baseline_sha256"] == hashlib.sha256(
        baseline_path.read_bytes()
    ).hexdigest()


def test_drive_all_files_scan_does_not_reintroduce_the_five_megabyte_ceiling() -> None:
    """Ordinary PDFs must remain hash-verifiable instead of looping forever."""

    generated = _by_id(business_jobs(ROOT, ROOT / "venv/bin/python3"))
    configured = _by_id(json.loads((ROOT / "cron_jobs.json").read_text(encoding="utf-8")))
    inventory = _by_id(
        json.loads(
            (ROOT / "docs/architecture/v3/generated/v2_inventory.json").read_text(
                encoding="utf-8"
            )
        )["cron_jobs"]
    )

    for source in (generated, configured, inventory):
        command = source["job_drive_case_sync_all_files"]["command"]
        assert "MAGI_DRIVE_SYNC_LOCAL_HASH_MAX_BYTES=1500000000" in command
        assert "MAGI_DRIVE_SYNC_MAX_SINGLE_DOWNLOAD_BYTES=1500000000" in command
        assert "MAGI_DRIVE_SYNC_MAX_SINGLE_UPLOAD_BYTES=1500000000" in command
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

    generated = _by_id(business_jobs(ROOT, ROOT / "venv/bin/python3"))
    configured = _by_id(json.loads((ROOT / "cron_jobs.json").read_text(encoding="utf-8")))
    inventory = _by_id(
        json.loads(
            (ROOT / "docs/architecture/v3/generated/v2_inventory.json").read_text(
                encoding="utf-8"
            )
        )["cron_jobs"]
    )

    for source in (generated, configured, inventory):
        assert source["job_laf_nightly_audit"]["timeout_sec"] == 1800

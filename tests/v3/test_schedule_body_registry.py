from __future__ import annotations

import copy
import json
import hashlib
import os
import sys
from datetime import datetime
from pathlib import Path

import pytest

from scripts.v3_campaign.offline_probes import bound_cron_jobs
from scripts.v3_validation.schedule_body_registry import (
    REGISTRY_PATH,
    ScheduleBodyRegistryError,
    _bind_fixture_v3_shared_state,
    _contract_ok,
    _execute_new_sample,
    _execute_new_samples,
    _load_registry,
    _osc_events_fixture_expectation,
    _prepare_fixture,
    _validate_new_adapter,
    TAIPEI_TIMEZONE,
    actual_entrypoint,
    resolve_registry,
    run_sandbox_escape_probes,
)
from scripts.v3_campaign.schedule_realism import _logical_definition_sha256
from scripts.v3_validation.schedule_product_fixture_matrix import adapter_proposals
from scripts.v3_validation.schedule_nonstorage_fixture_matrix import (
    adapter_proposals as nonstorage_adapter_proposals,
)
from scripts.v3_validation.schedule_sample_evidence import (
    CONTRACT_DIAGNOSTIC_SCHEMA,
    SYSTEM_DIAGNOSTIC_KIND,
    SYSTEM_DIAGNOSTIC_JOB_IDS,
    SYSTEM_DIAGNOSTIC_RESOURCE_WARNING_CODES,
    build_sample_evidence,
    canonical_sha256,
    verify_sample_evidence_ledger,
)


ROOT = Path(__file__).resolve().parents[2]


def _inputs():
    jobs, digest = bound_cron_jobs(ROOT)
    registry = _load_registry(ROOT, jobs, digest)
    return jobs, registry


def _bound_cron_snapshot_bytes() -> bytes:
    """Read the same hash-bound cron input used by sealed candidates."""

    _jobs, digest = bound_cron_jobs(ROOT)
    declared = str(os.environ.get("MAGI_CRON_JOBS_FILE") or "").strip()
    path = Path(declared) if declared else ROOT / "cron_jobs.json"
    payload = path.read_bytes()
    assert hashlib.sha256(payload).hexdigest() == digest
    return payload


def _system_diagnostic_adapter(
    job_id: str = "job_1770699415",
) -> dict[str, object]:
    registry = json.loads(
        (ROOT / REGISTRY_PATH).read_text(encoding="utf-8")
    )
    return next(
        row
        for row in registry["new_safe_adapters"]
        if row["job_id"] == job_id
    )


def test_registry_resolves_every_enabled_job_exactly_once() -> None:
    jobs, registry = _inputs()
    entries, inherited, new = resolve_registry(ROOT, registry, jobs)
    enabled = {str(job["id"]) for job in jobs if job.get("enabled") is True}

    assert len(enabled) == 96
    assert len(entries) == len(enabled)
    assert {row["job_id"] for row in entries} == enabled
    assert len(inherited) == 8
    assert len(new) == 88
    contract_types = [
        str(adapter["success_contract"]["type"])
        for adapter in new.values()
    ]
    assert contract_types.count("autopilot_terminal_fixture") == 2
    assert contract_types.count("operational_hardening_formal_fixture") == 1
    assert contract_types.count("file_review_formal_child_terminal") == 1
    assert contract_types.count("insight_sync_embedding_database_terminal") == 1
    assert contract_types.count("reprocess_insights_api_model_database_terminal") == 1
    assert contract_types.count("system_diagnostic_terminal") == len(
        SYSTEM_DIAGNOSTIC_JOB_IDS
    )
    assert sum(row["classification"] == "safe_adapter" for row in entries) == 96
    assert sum(row["classification"] == "blocked" for row in entries) == 0
    assert all(
        (row["classification"] == "blocked") == bool(row["blockers"])
        for row in entries
    )
    assert not any(
        "LOCAL_BODY_NOT_YET_REVIEWED_FOR_FIXTURE_ISOLATION" in row["blockers"]
        for row in entries
    )


def test_osc_events_fixture_expectation_rolls_forward_across_month_end() -> None:
    expectation = _osc_events_fixture_expectation(
        now=datetime(2026, 7, 31, 23, 59, tzinfo=TAIPEI_TIMEZONE)
    )

    assert expectation["hearing_date"] == "2026-08-30"
    assert expectation["hearing_time"] == "10:00"
    assert "訂115年8月30日上午10時開庭" in expectation["file_name"]


def test_osc_events_contract_uses_bound_future_date_not_a_fixed_calendar_day(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    _prepare_fixture("osc_events_pdf_scan", fixture, "job_osc_events_refresh")
    expectation_path = fixture / "osc-events-fixture.json"
    expectation = json.loads(expectation_path.read_text(encoding="utf-8"))
    todo = {
        "type": "開庭",
        "date": expectation["hearing_date"],
        "time": expectation["hearing_time"],
    }
    scan = {
        "scanned": 1,
        "todo_count": 1,
        "sample_items": [
            {
                "file_name": expectation["file_name"],
                "todos": [todo],
                "write_result": {"inserted": 0},
            }
        ],
    }
    payload = {
        "ok": True,
        "dry_run": True,
        "pdf_calendar_scan": scan,
        "historical_todo_completion": {"matched": 1, "updated": 0},
    }
    (fixture / "runtime" / "osc-events-latest.json").write_text(
        json.dumps({"pdf_calendar_scan": scan}), encoding="utf-8"
    )
    (fixture / "runtime" / "pdf_calendar_scan_cache.json").write_text(
        json.dumps({"files": {expectation["file_name"]: {"ok": True}}}),
        encoding="utf-8",
    )

    contract_ok, evidence = _contract_ok(
        {"type": "osc_events_pdf_scan_fixture"},
        fixture,
        json.dumps(payload),
        "",
    )

    assert contract_ok is True
    assert evidence["checks"]["fixture_hearing_is_future"] is True
    assert evidence["expected_hearing_date"] == expectation["hearing_date"]

    expectation["hearing_date"] = "2000-01-01"
    expectation_path.write_text(json.dumps(expectation), encoding="utf-8")
    contract_ok, evidence = _contract_ok(
        {"type": "osc_events_pdf_scan_fixture"},
        fixture,
        json.dumps(payload),
        "",
    )
    assert contract_ok is False
    assert evidence["checks"]["fixture_hearing_is_future"] is False


def test_osc_events_database_fixture_covers_document_index_fast_path() -> None:
    _jobs, registry = _inputs()
    adapters = {
        row["job_id"]: row
        for row in registry["new_safe_adapters"]
        if row["job_id"] in {"job_osc_events_refresh", "job_osc_todo_governance"}
    }

    assert set(adapters) == {"job_osc_events_refresh", "job_osc_todo_governance"}
    for adapter in adapters.values():
        dependency = adapter["dependency"]
        assert any(
            "CREATE TABLE document_index" in statement
            for statement in dependency["seed_sql"]
        )
        assert dependency["expected_requests"]["FROM document_index"] == 1
        assert any(
            row["sql"] == "SELECT COUNT(*) FROM law_firm_data.document_index"
            and row["equals"] == 1
            for row in dependency["postconditions"]
        )


@pytest.mark.parametrize(
    "resource_warnings",
    [
        ["memory_free_below_15_percent"],
        ["disk_free_below_10_gib"],
        ["memory_free_below_15_percent", "disk_free_below_10_gib"],
    ],
)
def test_system_diagnostic_terminal_accepts_only_resource_warnings_and_preserves_them(
    tmp_path: Path,
    resource_warnings: list[str],
) -> None:
    adapter = _system_diagnostic_adapter()
    payload = {
        "success": True,
        "status": "warning",
        "network_scope": "localhost_only",
        "production_write_performed": False,
        "schedule": {"enabled": 1, "failed": 0},
        "localhost_health": [{"ok": True}] * 4,
        "warnings": resource_warnings,
    }

    contract_ok, evidence = _contract_ok(
        adapter["success_contract"], tmp_path, json.dumps(payload), ""
    )

    assert contract_ok is True
    assert evidence["observed_status"] == "warning"
    assert evidence["warnings"] == resource_warnings
    assert evidence["warning_count"] == len(resource_warnings)
    assert evidence["diagnostic_schema"] == CONTRACT_DIAGNOSTIC_SCHEMA
    assert evidence["diagnostic_kind"] == SYSTEM_DIAGNOSTIC_KIND
    assert evidence["accepted_warning_codes"] == sorted(
        SYSTEM_DIAGNOSTIC_RESOURCE_WARNING_CODES
    )
    assert evidence["checks"]["warning_codes_allowlisted"] is True
    assert evidence["warnings_consistent_with_status"] is True

    payload["success"] = False
    payload["status"] = "failed"
    contract_ok, evidence = _contract_ok(
        adapter["success_contract"], tmp_path, json.dumps(payload), ""
    )
    assert contract_ok is False
    assert evidence["status_accepted"] is False


@pytest.mark.parametrize(
    "warning_code",
    [
        "enabled_schedule_failures_present",
        "enabled_schedule_deferred_present",
        "unknown_future_warning",
    ],
)
def test_system_diagnostic_terminal_rejects_nonresource_warning_codes(
    tmp_path: Path,
    warning_code: str,
) -> None:
    adapter = _system_diagnostic_adapter()
    payload = {
        "success": True,
        "status": "warning",
        "network_scope": "localhost_only",
        "production_write_performed": False,
        "schedule": {"enabled": 1, "failed": 0},
        "localhost_health": [{"ok": True}] * 4,
        "warnings": [warning_code],
    }

    contract_ok, evidence = _contract_ok(
        adapter["success_contract"], tmp_path, json.dumps(payload), ""
    )

    assert contract_ok is False
    assert evidence["checks"]["warning_codes_allowlisted"] is False


@pytest.mark.parametrize("diagnostic_status", ["deferred", "unknown"])
def test_system_diagnostic_terminal_rejects_nonterminal_statuses(
    tmp_path: Path,
    diagnostic_status: str,
) -> None:
    adapter = _system_diagnostic_adapter()
    payload = {
        "success": True,
        "status": diagnostic_status,
        "network_scope": "localhost_only",
        "production_write_performed": False,
        "schedule": {"enabled": 1, "failed": 0},
        "localhost_health": [{"ok": True}] * 4,
        "warnings": [],
    }

    contract_ok, evidence = _contract_ok(
        adapter["success_contract"], tmp_path, json.dumps(payload), ""
    )

    assert contract_ok is False
    assert evidence["checks"]["status_accepted"] is False


@pytest.mark.parametrize("job_id", sorted(SYSTEM_DIAGNOSTIC_JOB_IDS))
def test_system_diagnostic_registry_rejects_widened_warning_allowlist(
    job_id: str,
) -> None:
    adapter = copy.deepcopy(_system_diagnostic_adapter(job_id))
    _validate_new_adapter(adapter)

    adapter["success_contract"]["warning_allowlist"].append(
        "enabled_schedule_deferred_present"
    )

    with pytest.raises(
        ScheduleBodyRegistryError,
        match="invalid system diagnostic warning allowlist",
    ):
        _validate_new_adapter(adapter)


@pytest.mark.parametrize("job_id", sorted(SYSTEM_DIAGNOSTIC_JOB_IDS))
def test_system_diagnostic_registry_rejects_generic_contract_downgrade(
    job_id: str,
) -> None:
    adapter = copy.deepcopy(_system_diagnostic_adapter(job_id))
    adapter["success_contract"]["type"] = "stdout_json"
    adapter["success_contract"].pop("warning_allowlist")

    with pytest.raises(
        ScheduleBodyRegistryError,
        match="invalid system diagnostic contract type",
    ):
        _validate_new_adapter(adapter)


def test_optimize_report_uses_strict_system_diagnostic_contract(
    tmp_path: Path,
) -> None:
    adapter = _system_diagnostic_adapter("job_optimize_report")
    base_payload = {
        "success": True,
        "network_scope": "localhost_only",
        "production_write_performed": False,
        "schedule": {"enabled": 1, "failed": 0},
        "localhost_health": [{"ok": True}] * 4,
    }

    resource_warning = {
        **base_payload,
        "status": "warning",
        "warnings": ["memory_free_below_15_percent"],
    }
    contract_ok, evidence = _contract_ok(
        adapter["success_contract"],
        tmp_path,
        json.dumps(resource_warning),
        "",
    )
    assert contract_ok is True
    assert evidence["diagnostic_kind"] == SYSTEM_DIAGNOSTIC_KIND
    assert evidence["warnings"] == ["memory_free_below_15_percent"]

    for warning_code in (
        "enabled_schedule_deferred_present",
        "unknown_future_warning",
    ):
        rejected = {
            **base_payload,
            "status": "warning",
            "warnings": [warning_code],
        }
        contract_ok, evidence = _contract_ok(
            adapter["success_contract"],
            tmp_path,
            json.dumps(rejected),
            "",
        )
        assert contract_ok is False
        assert evidence["checks"]["warning_codes_allowlisted"] is False


def _synthetic_system_diagnostic_ledger(
    contract_evidence: dict[str, object],
    *,
    job_id: str = "job_1770699415",
) -> dict[str, object]:
    entrypoint_sha256 = hashlib.sha256(b"system-diagnostic-entrypoint").hexdigest()
    adapter_mode = "real_entrypoint_fixture_v1"
    durations = [1.0, 1.25, 1.5]
    samples = []
    for sample_index, duration in enumerate(durations, 1):
        nonce = hashlib.sha256(f"diagnostic-{sample_index}".encode()).hexdigest()
        raw = {
            "status": "passed",
            "executed": True,
            "returncode": 0,
            "duration_seconds": duration,
            "semantic_success": True,
            "execution_nonce_sha256": nonce,
            "sandbox_profile_sha256": hashlib.sha256(b"sandbox").hexdigest(),
            "stdout_sha256": hashlib.sha256(b"stdout").hexdigest(),
            "stderr_sha256": hashlib.sha256(b"stderr").hexdigest(),
            "diagnostic_evidence_relative_path": "diagnostics/execution.json",
            "diagnostic_evidence_sha256": hashlib.sha256(
                b"execution-diagnostic"
            ).hexdigest(),
            "fixture_binding_sha256": hashlib.sha256(b"fixture-binding").hexdigest(),
            "fixture_initial_inventory_sha256": hashlib.sha256(b"initial").hexdigest(),
            "fixture_final_inventory_sha256": hashlib.sha256(b"final").hexdigest(),
            "fixture_final_file_count": 1,
            "no_fixture_symlinks": True,
            "success_contract_evidence": contract_evidence,
            "dependency_evidence": {
                "kind": "localhost_http",
                "request_count": 4,
                "request_counts": {"/health": 4},
                "expected_requests_satisfied": True,
                "postcondition_count": 0,
                "passed_postcondition_count": 0,
                "postconditions_passed": True,
                "transcript_sha256": hashlib.sha256(b"transcript").hexdigest(),
                "postconditions_sha256": hashlib.sha256(
                    b"postconditions"
                ).hexdigest(),
            },
            "adapter_mode": adapter_mode,
            "network_denied_by_seatbelt": True,
            "notifications_disabled": True,
        }
        samples.append(
            build_sample_evidence(
                raw,
                sample_index=sample_index,
                execution_kind="reviewed_real_entrypoint_fixture_v1",
                entrypoint_sha256=entrypoint_sha256,
            )
        )
    return {
        "job_id": job_id,
        "entrypoint_sha256": entrypoint_sha256,
        "adapter_mode": adapter_mode,
        "duration_samples_seconds": durations,
        "sample_statuses": ["passed", "passed", "passed"],
        "sample_evidence": samples,
        "sample_evidence_sha256": canonical_sha256(samples),
        "sandbox_profile_sha256_samples": [
            sample["sandbox_profile_sha256"] for sample in samples
        ],
        "stdout_sha256_samples": [sample["stdout_sha256"] for sample in samples],
        "stderr_sha256_samples": [sample["stderr_sha256"] for sample in samples],
    }


def _rehash_tampered_sample(result: dict[str, object], sample_index: int) -> None:
    samples = result["sample_evidence"]
    assert isinstance(samples, list)
    sample = samples[sample_index]
    assert isinstance(sample, dict)
    contract = sample["success_contract_evidence"]
    assert isinstance(contract, dict)
    unsigned_contract = dict(contract)
    unsigned_contract.pop("evidence_sha256", None)
    contract["evidence_sha256"] = canonical_sha256(unsigned_contract)
    unsigned_sample = dict(sample)
    unsigned_sample.pop("evidence_sha256", None)
    sample["evidence_sha256"] = canonical_sha256(unsigned_sample)
    result["sample_evidence_sha256"] = canonical_sha256(samples)


@pytest.mark.parametrize("job_id", sorted(SYSTEM_DIAGNOSTIC_JOB_IDS))
@pytest.mark.parametrize("removed_field", ["observed_status", "warning_codes"])
def test_system_diagnostic_ledger_rejects_deleted_required_field_after_rehash(
    tmp_path: Path,
    removed_field: str,
    job_id: str,
) -> None:
    adapter = _system_diagnostic_adapter(job_id)
    payload = {
        "success": True,
        "status": "healthy",
        "network_scope": "localhost_only",
        "production_write_performed": False,
        "schedule": {"enabled": 1, "failed": 0},
        "localhost_health": [{"ok": True}] * 4,
        "warnings": [],
    }
    contract_ok, contract_evidence = _contract_ok(
        adapter["success_contract"], tmp_path, json.dumps(payload), ""
    )
    assert contract_ok is True
    result = _synthetic_system_diagnostic_ledger(
        contract_evidence,
        job_id=job_id,
    )
    assert verify_sample_evidence_ledger(result, minimum_samples=3) is True

    tampered = copy.deepcopy(result)
    first_sample = tampered["sample_evidence"][0]
    del first_sample["success_contract_evidence"][removed_field]
    _rehash_tampered_sample(tampered, 0)

    assert verify_sample_evidence_ledger(tampered, minimum_samples=3) is False


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS release control")
def test_system_diagnostic_terminal_three_samples_preserve_resource_state(
    tmp_path: Path,
) -> None:
    adapter = _system_diagnostic_adapter()

    result = _execute_new_samples(ROOT, tmp_path / "samples", adapter)

    assert result["status"] == "passed", result
    assert result["successful_samples"] == 3
    assert verify_sample_evidence_ledger(result, minimum_samples=3) is True
    for sample in result["sample_evidence"]:
        contract = sample["success_contract_evidence"]
        assert contract["diagnostic_schema"] == CONTRACT_DIAGNOSTIC_SCHEMA
        assert contract["diagnostic_kind"] == SYSTEM_DIAGNOSTIC_KIND
        assert contract["observed_status"] in {"healthy", "warning"}
        assert isinstance(contract["warning_codes"], list)
        assert contract["warning_count"] == len(contract["warning_codes"])
        assert (
            contract["observed_status"] == "warning"
        ) == bool(contract["warning_codes"])


def test_registry_is_bound_to_actual_command_entrypoints_and_forbids_standins() -> None:
    jobs, registry = _inputs()
    entries, _inherited, new = resolve_registry(ROOT, registry, jobs)
    by_id = {str(job["id"]): job for job in jobs}

    for job_id, adapter in new.items():
        entrypoint, kind = actual_entrypoint(ROOT, by_id[job_id])
        assert entrypoint == adapter["production_entrypoint"]
        assert kind in {"py", "sh", "reviewed_cron_macro"}
        argv = [str(value).lower() for value in adapter["argv"]]
        assert "--help" not in argv
        assert "-h" not in argv
        assert "-c" not in argv
        joined = " ".join(argv)
        assert "dispatcher" not in joined
        assert "time.sleep" not in joined
        assert "fake_handler" not in joined

    safe_ids = {row["job_id"] for row in entries if row["classification"] == "safe_adapter"}
    assert "job_laf_gmail_dispatch_scan" in safe_ids
    assert "job_laf_portal_new_files_scan" in safe_ids
    assert "job_laf_condition_draft" in safe_ids
    assert "job_laf_nightly_audit" in safe_ids
    assert "job_drive_case_sync_bidirectional" in safe_ids
    assert "job_drive_case_sync_all_files" in safe_ids
    assert "job_file_review_check" in safe_ids
    assert "job_slow_archive_closed_cases" in safe_ids
    assert "job_purge_persona" in safe_ids
    assert "job_osc_auto_backup" in safe_ids
    assert "job_knowledge_lint" in safe_ids
    assert "job_function_health_index" in safe_ids
    assert "job_obsidian_repair_notes" in safe_ids
    assert "job_laf_condition_dedup_scan" in safe_ids
    assert "job_case_index_sync" in safe_ids
    assert "job_magi_self_repair_guardian" in safe_ids
    assert "job_transcript_indexer" in safe_ids
    assert "job_legacy_judgment_resummary_quality" in safe_ids
    assert "job_osc_index_cases" in safe_ids
    assert "job_laf_pending_scan" in safe_ids
    assert "job_nas_pdf_ocr_worker_offpeak" in safe_ids
    assert "job_api_token_health_check" in safe_ids
    assert "job_tailscale_funnel_healthcheck" in safe_ids
    assert "job_1770949442096_9e8adf" in safe_ids
    assert "job_smoke_external_chat" in safe_ids
    assert "job_judicial_api_night_pull" in safe_ids
    assert "job_judicial_api_morning" in safe_ids
    assert "job_judicial_api_noon" in safe_ids
    assert "job_judicial_api_afternoon" in safe_ids
    assert "job_judicial_api_evening" in safe_ids
    assert "job_judicial_api_backlog_clear" in safe_ids
    assert "job_1770705679" in safe_ids
    assert "job_weekly_legal_crawl" in safe_ids
    assert "job_judgment_retry_evening" in safe_ids
    assert "job_worldmonitor_intel" in safe_ids
    assert "job_file_review_downloadable_probe_dense" in safe_ids
    assert "job_transcript_sync" in safe_ids
    assert "job_research_brief_daily" in safe_ids
    assert "job_market_briefing_script" in safe_ids
    assert "job_omlx_switch_day" in safe_ids
    assert "job_omlx_switch_night" in safe_ids
    assert "job_omlx_profile_guard" in safe_ids
    assert "job_osc_events_refresh" in safe_ids
    assert "job_osc_todo_governance" in safe_ids
    assert "job_1770699415" in safe_ids
    assert "job_1770948489644_c5a469" in safe_ids
    assert "job_obsidian_ingest" in safe_ids
    assert "job_obsidian_vector_reindex_notes" in safe_ids
    assert "job_obsidian_vector_reindex_wiki" in safe_ids
    assert "job_accounting_sheet_import" in safe_ids
    assert "job_accounting_monthly_bonus" in safe_ids
    assert "job_benchmark_pdf_namer" in safe_ids
    assert "job_translator_ape_regression" in safe_ids
    assert "job_nightly_regression" in safe_ids
    assert "job_business_module_live_check" in safe_ids
    assert "job_heavy_translation_quality_live" in safe_ids
    assert "job_distill_train_gemma" in safe_ids
    assert "job_insight_sync" in safe_ids
    assert "job_reprocess_insights" in safe_ids
    assert "job_1770948489644_0726cf" in safe_ids
    assert "job_nightly_autopilot" in safe_ids
    assert "job_operational_hardening_audit" in safe_ids
    assert "job_pdf_namer_nightly" in safe_ids
    assert "pdfnamer_docling_layout" in safe_ids


def test_product_registry_entries_exactly_match_reviewed_adapter_proposals() -> None:
    _jobs, registry = _inputs()
    registered = {
        row["job_id"]: row for row in registry["new_safe_adapters"]
    }

    for proposal in adapter_proposals():
        assert registered[proposal["job_id"]] == proposal

    for proposal in nonstorage_adapter_proposals():
        assert registered[proposal["job_id"]] == proposal


def test_registry_rejects_entrypoint_drift() -> None:
    jobs, registry = _inputs()
    changed = [dict(job) for job in jobs]
    target = next(job for job in changed if job.get("id") == "job_backup_market_watchlist")
    target["command"] = str(target["command"]).replace(
        "backup_market_watchlist.py", "different_body.py"
    )

    with pytest.raises(ScheduleBodyRegistryError, match="entrypoint drifted"):
        resolve_registry(ROOT, registry, changed)


def test_fixture_preparation_rejects_unknown_kind_and_has_no_symlink(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    meta = _prepare_fixture("market_watchlist_backup", fixture, "job_fixture")
    assert len(meta["initial_inventory_sha256"]) == 64
    assert not any(path.is_symlink() for path in fixture.rglob("*"))

    with pytest.raises(ScheduleBodyRegistryError, match="unknown fixture kind"):
        _prepare_fixture("not-a-real-kind", tmp_path / "unknown", "job_fixture")


def test_fixture_v3_bindings_are_complete_canonical_and_disposable(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    environment = {
        "MAGI_RUNTIME_DIR": str(fixture / "workspace" / "runtime"),
        "MAGI_EXPORTS_DIR": str(fixture / "workspace" / "exports"),
    }

    receipt = _bind_fixture_v3_shared_state(fixture, environment)

    shared = fixture / "workspace"
    assert receipt["schema"] == "magi.v3.schedule-fixture-bindings/v1"
    assert receipt["shared_relative"] == "workspace"
    assert len(receipt["sha256"]) == 64
    assert environment["MAGI_V3_SHARED_STATE_DIR"] == str(shared)
    assert environment["MAGI_SHARED_STATE_DIR"] == str(shared)
    assert environment["MAGI_AGENT_DIR"] == str(shared / "agent")
    assert environment["MAGI_RUNTIME_DIR"] == str(shared / "runtime")
    assert environment["MAGI_MUTABLE_STATIC_DIR"] == str(shared / "static")
    assert environment["MAGI_EXPORTS_DIR"] == str(shared / "exports")
    assert environment["MAGI_LAF_PROCESSED_EMAILS_PATH"] == str(
        shared / "agent/laf-orchestrator/processed_laf_emails.json"
    )
    assert environment["MAGI_JUDGMENTS_JSON_PATH"] == str(
        shared / "agent/judgment-collector/judgments.json"
    )
    assert environment["MAGI_CORTEX_SYNC_STATE_PATH"] == str(
        shared / "runtime/cortex_sync_state.json"
    )
    assert environment["MAGI_PDF_NAMER_CASE_INDEX"] == str(
        shared / "pdf-namer/_case_index.json"
    )
    assert all(
        Path(value).resolve(strict=False).is_relative_to(fixture)
        for name, value in environment.items()
        if name.startswith("MAGI_") or name == "FAISS_INDEX_DIR"
    )


def test_fixture_v3_bindings_reject_split_mutable_roots(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    environment = {
        "MAGI_AGENT_DIR": str(fixture / "shared-a" / "agent"),
        "MAGI_RUNTIME_DIR": str(fixture / "shared-b" / "runtime"),
    }

    with pytest.raises(
        ScheduleBodyRegistryError,
        match="do not share one canonical root",
    ):
        _bind_fixture_v3_shared_state(fixture, environment)


def test_fixture_v3_bindings_derive_file_review_children_from_explicit_state(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    state = fixture / "state"
    environment = {"MAGI_FILE_REVIEW_STATE_DIR": str(state)}

    receipt = _bind_fixture_v3_shared_state(fixture, environment)

    assert environment["MAGI_FILE_REVIEW_STATE_DIR"] == str(state)
    assert environment["MAGI_FILE_REVIEW_BG_JOB_DIR"] == str(state / "bg-jobs")
    assert environment["MAGI_EEFILE_DOWNLOAD_FOLDER"] == str(state / "downloads")
    assert receipt["resolved_bindings"]["MAGI_FILE_REVIEW_BG_JOB_DIR"] == (
        "state/bg-jobs"
    )


@pytest.mark.parametrize(
    "name,value_kind",
    [
        ("MAGI_FILE_REVIEW_BG_JOB_DIR", "outside"),
        ("MAGI_PDF_NAMER_CASE_INDEX", "outside"),
        ("MAGI_FILE_REVIEW_STATE_DIR", "symlink"),
    ],
)
def test_fixture_v3_bindings_reject_explicit_optional_or_named_escape(
    tmp_path: Path,
    name: str,
    value_kind: str,
) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    value = outside
    if value_kind == "symlink":
        value = fixture / "linked-state"
        value.symlink_to(outside, target_is_directory=True)
    environment = {name: str(value)}

    with pytest.raises(ScheduleBodyRegistryError, match="fixture"):
        _bind_fixture_v3_shared_state(fixture, environment)


def test_function_health_fixture_uses_only_hash_bound_cron_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cron = tmp_path / "candidate-cron.json"
    jobs = json.loads(_bound_cron_snapshot_bytes())
    # The hash-binding contract does not depend on a particular checkout name.
    # Change one candidate definition deterministically so this test remains
    # valid in worktrees, installed releases, and deployment-rebased snapshots.
    rebased = next(job for job in jobs if str(job.get("command") or "").strip())
    rebased["command"] = (
        str(rebased["command"]) + " --candidate-snapshot-binding-probe"
    )
    payload = (json.dumps(jobs, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    cron.write_bytes(payload)
    source_sha = json.loads(
        (ROOT / "config/v3_schedule_dispatch_policy.json").read_text(encoding="utf-8")
    )["cron_jobs_sha256"]
    assert hashlib.sha256(payload).hexdigest() != source_sha
    monkeypatch.setenv("MAGI_CRON_JOBS_FILE", str(cron))
    monkeypatch.setenv("MAGI_CRON_JOBS_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setenv("MAGI_CRON_JOBS_SOURCE_SHA256", source_sha)
    fixture = tmp_path / "valid"
    _prepare_fixture(
        "function_health_index", fixture, "job_function_health_index", source_root=ROOT
    )
    assert (fixture / "runtime" / "cron_jobs.json").read_bytes() == payload

    monkeypatch.setenv("MAGI_CRON_JOBS_SHA256", "0" * 64)
    with pytest.raises(ScheduleBodyRegistryError, match="SHA-256 mismatch"):
        _prepare_fixture(
            "function_health_index",
            tmp_path / "tampered",
            "job_function_health_index",
            source_root=ROOT,
        )


@pytest.mark.parametrize(
    "declared",
    [
        ("MAGI_CRON_JOBS_FILE",),
        ("MAGI_CRON_JOBS_FILE", "MAGI_CRON_JOBS_SHA256"),
        ("MAGI_CRON_JOBS_SOURCE_SHA256",),
    ],
)
def test_function_health_fixture_rejects_partial_cron_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared: tuple[str, ...],
) -> None:
    cron = tmp_path / "candidate-cron.json"
    cron.write_bytes(_bound_cron_snapshot_bytes())
    values = {
        "MAGI_CRON_JOBS_FILE": str(cron),
        "MAGI_CRON_JOBS_SHA256": hashlib.sha256(cron.read_bytes()).hexdigest(),
        "MAGI_CRON_JOBS_SOURCE_SHA256": "0" * 64,
    }
    for name in values:
        monkeypatch.delenv(name, raising=False)
    for name in declared:
        monkeypatch.setenv(name, values[name])

    with pytest.raises(ScheduleBodyRegistryError, match="binding is incomplete"):
        _prepare_fixture(
            "function_health_index",
            tmp_path / "partial",
            "job_function_health_index",
            source_root=ROOT,
        )


def test_function_health_fixture_rejects_wrong_source_policy_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cron = tmp_path / "candidate-cron.json"
    cron.write_bytes(_bound_cron_snapshot_bytes())
    monkeypatch.setenv("MAGI_CRON_JOBS_FILE", str(cron))
    monkeypatch.setenv(
        "MAGI_CRON_JOBS_SHA256", hashlib.sha256(cron.read_bytes()).hexdigest()
    )
    monkeypatch.setenv("MAGI_CRON_JOBS_SOURCE_SHA256", "0" * 64)

    with pytest.raises(ScheduleBodyRegistryError, match="source/policy binding mismatched"):
        _prepare_fixture(
            "function_health_index",
            tmp_path / "wrong-source",
            "job_function_health_index",
            source_root=ROOT,
        )


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS release control")
def test_runner_rejects_adapter_declared_partial_cron_hash(tmp_path: Path) -> None:
    _jobs, registry = _inputs()
    original = next(
        row
        for row in registry["new_safe_adapters"]
        if row["job_id"] == "job_function_health_index"
    )
    adapter = dict(original)
    adapter["environment"] = dict(original["environment"])
    adapter["environment"]["MAGI_CRON_JOBS_SHA256"] = "0" * 64

    with pytest.raises(
        ScheduleBodyRegistryError, match="fixture cron hashes must be generated by the runner"
    ):
        _execute_new_sample(ROOT, tmp_path / "sample", adapter)


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS release control")
def test_function_health_adapter_uses_fixture_cron_for_three_samples(
    tmp_path: Path,
) -> None:
    _jobs, registry = _inputs()
    adapter = next(
        row
        for row in registry["new_safe_adapters"]
        if row["job_id"] == "job_function_health_index"
    )

    assert adapter["environment"]["MAGI_CRON_JOBS_FILE"] == (
        "<FIXTURE>/runtime/cron_jobs.json"
    )
    result = _execute_new_samples(ROOT, tmp_path / "samples", adapter)

    assert result["status"] == "passed", result
    assert result["successful_samples"] == 3
    assert result["sample_statuses"] == ["passed", "passed", "passed"]
    assert result["network_denied_by_seatbelt"] is True
    assert result["notifications_disabled"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS release control")
def test_seatbelt_denies_direct_symlink_and_network_escape(tmp_path: Path) -> None:
    evidence = run_sandbox_escape_probes(ROOT, tmp_path / "escape")

    assert evidence["status"] == "passed"
    assert evidence["direct_write_escape_denied"] is True
    assert evidence["symlink_write_escape_denied"] is True
    assert evidence["network_escape_denied"] is True
    assert evidence["outside_file_absent"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS release control")
def test_new_real_body_adapter_requires_three_successful_samples(tmp_path: Path) -> None:
    _jobs, registry = _inputs()
    adapter = next(
        row
        for row in registry["new_safe_adapters"]
        if row["job_id"] == "job_backup_market_watchlist"
    )
    result = _execute_new_samples(ROOT, tmp_path / "samples", adapter)

    assert result["status"] == "passed"
    assert result["executed"] is True
    assert result["semantic_success"] is True
    assert result["samples_requested"] == 3
    assert result["successful_samples"] == 3
    assert result["duration_sample_count"] == 3
    assert len(result["duration_samples_seconds"]) == 3
    assert result["duration_p95_seconds"] > 0
    assert result["adapter_mode"] == "real_entrypoint_fixture_v1"
    assert result["network_denied_by_seatbelt"] is True
    assert result["notifications_disabled"] is True
    for index, evidence in enumerate(result["sample_evidence"], 1):
        diagnostic = (
            tmp_path
            / "samples"
            / f"sample-{index:03d}"
            / evidence["diagnostic_evidence_relative_path"]
        )
        assert diagnostic.is_file()
        assert hashlib.sha256(diagnostic.read_bytes()).hexdigest() == evidence[
            "diagnostic_evidence_sha256"
        ]
        payload = json.loads(diagnostic.read_text(encoding="utf-8"))
        assert payload["schema"] == "magi.v3.schedule-execution-diagnostic/v1"
        assert payload["job_id"] == "job_backup_market_watchlist"
        assert payload["returncode"] == 0
        assert payload["fixture_binding_sha256"] == evidence[
            "fixture_binding_sha256"
        ]


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS release control")
def test_localhost_dependency_adapter_captures_real_requests(tmp_path: Path) -> None:
    _jobs, registry = _inputs()
    adapter = next(
        row
        for row in registry["new_safe_adapters"]
        if row["job_id"] == "job_optimize_report"
    )
    result = _execute_new_samples(ROOT, tmp_path / "samples", adapter)

    assert result["status"] == "passed"
    assert result["successful_samples"] == 3
    assert result["localhost_dependency_allowlisted"] is True
    assert result["network_denied_by_seatbelt"] is True


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS release control")
def test_disposable_database_adapter_executes_real_mutation_three_times(tmp_path: Path) -> None:
    _jobs, registry = _inputs()
    adapter = next(
        row
        for row in registry["new_safe_adapters"]
        if row["job_id"] == "job_empty_case_shell_cleanup"
    )
    result = _execute_new_samples(ROOT, tmp_path / "samples", adapter)

    assert result["status"] == "passed"
    assert result["successful_samples"] == 3
    assert result["localhost_dependency_allowlisted"] is True
    assert result["network_denied_by_seatbelt"] is True


def test_dependency_resolver_uses_trusted_root_with_sealed_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.v3_validation import schedule_body_registry as registry_module

    trusted = tmp_path / "trusted"
    executable = trusted / "bin" / "mariadbd"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr(
        registry_module,
        "TRUSTED_DEPENDENCY_ROOTS",
        (trusted.resolve(),),
    )

    assert registry_module._trusted_dependency_executable("mariadbd") == str(
        executable.resolve()
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS release control")
@pytest.mark.parametrize(
    "job_id",
    [
        "job_obsidian_repair_notes",
        "job_laf_condition_dedup_scan",
        "job_case_index_sync",
        "job_magi_self_repair_guardian",
        "job_transcript_indexer",
        "job_legacy_judgment_resummary_quality",
        "job_osc_index_cases",
        "job_laf_pending_scan",
        "job_nas_pdf_ocr_worker_offpeak",
        "job_api_token_health_check",
        "job_tailscale_funnel_healthcheck",
        "job_1770949442096_9e8adf",
        "job_smoke_external_chat",
        "job_judicial_api_night_pull",
        "job_judicial_api_morning",
        "job_judicial_api_noon",
        "job_judicial_api_afternoon",
        "job_judicial_api_evening",
        "job_judicial_api_backlog_clear",
        "job_1770705679",
        "job_weekly_legal_crawl",
        "job_judgment_retry_evening",
        "job_worldmonitor_intel",
        "job_file_review_downloadable_probe_dense",
        "job_transcript_sync",
        "job_research_brief_daily",
        "job_market_briefing_script",
        "job_omlx_switch_day",
        "job_omlx_switch_night",
        "job_omlx_profile_guard",
        "job_osc_events_refresh",
        "job_osc_todo_governance",
        "job_1770699415",
        "job_obsidian_ingest",
        "job_obsidian_vector_reindex_notes",
        "job_obsidian_vector_reindex_wiki",
        "job_accounting_sheet_import",
        "job_accounting_monthly_bonus",
        "job_benchmark_pdf_namer",
        "job_translator_ape_regression",
        "job_nightly_regression",
        "job_laf_gmail_dispatch_scan",
        "job_laf_portal_new_files_scan",
        "job_laf_condition_draft",
            "job_laf_nightly_audit",
            "job_business_module_live_check",
            "job_commercial_readiness_live",
            "job_heavy_translation_quality_live",
        "job_distill_train_gemma",
        "job_insight_sync",
        "job_reprocess_insights",
        "job_1770948489644_0726cf",
        "job_1770948489644_c5a469",
        "job_nightly_autopilot",
        "job_operational_hardening_audit",
        "job_pdf_namer_nightly",
        "pdfnamer_docling_layout",
        "job_drive_case_sync_bidirectional",
        "job_drive_case_sync_all_files",
        "job_file_review_check",
        "job_slow_archive_closed_cases",
        "job_exam_tutor_yearly_sync",
    ],
)
def test_newly_reviewed_local_bodies_require_true_postconditions(
    tmp_path: Path, job_id: str
) -> None:
    _jobs, registry = _inputs()
    adapter = next(
        row for row in registry["new_safe_adapters"] if row["job_id"] == job_id
    )
    result = _execute_new_samples(ROOT, tmp_path / job_id, adapter)

    assert result["status"] == "passed"
    assert result["successful_samples"] == 3
    assert result["network_denied_by_seatbelt"] is True
    assert result["notifications_disabled"] is True
    assert len(result["sample_evidence"]) == 3
    assert result["sample_evidence_sha256"] == canonical_sha256(
        result["sample_evidence"]
    )
    assert verify_sample_evidence_ledger(result, minimum_samples=3) is True


def _file_review_terminal_stdout(fixture: Path) -> str:
    receipts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((fixture / "state/formal-receipts").glob("*.json"))
    ]
    by_step = {str(row["step"]): row for row in receipts}
    states = sorted((fixture / "state/bg-jobs").glob("download_*.json"))
    assert len(states) == 1
    state = json.loads(states[0].read_text(encoding="utf-8"))
    download = {
        **dict(state["result"]),
        "success": bool(state["success"]),
        "queued": True,
        "job_id": state["job_id"],
        "pid": int(state["pid"]),
        "child_terminal": True,
        "child_status": state["status"],
        "child_finished_at": state["finished_at"],
        "queue_receipt": by_step["download_queue"],
        "terminal_state_sha256": hashlib.sha256(
            json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
    }
    payload = {
        "success": True,
        "status": "done",
        "deferred": False,
        "failed_steps": [],
        "steps": {
            "check_emails": {
                "success": True,
                "matched": 1,
                "ignored": 1,
                "downloadable_case_numbers": ["2026-0001"],
                "ignored_kinds": ["willingness_inquiry"],
                "willingness_inquiries_excluded": 1,
            },
            "download_payment_slips": {
                "success": True,
                "count": 0,
                "provider": "fixture_payment_portal_provider",
            },
            "download": download,
        },
        "provider_quality_certified": False,
        "provider_role": "bounded_email_and_portal_fixture",
    }
    return json.dumps(payload, ensure_ascii=False)


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS release control")
@pytest.mark.parametrize(
    "job_id",
    ["job_file_review_check", "job_insight_sync", "job_reprocess_insights"],
)
def test_formal_provider_contracts_reject_forged_handler_receipts(
    tmp_path: Path, job_id: str
) -> None:
    _jobs, registry = _inputs()
    adapter = next(
        row for row in registry["new_safe_adapters"] if row["job_id"] == job_id
    )
    sample_root = tmp_path / job_id / "sample-001"
    result = _execute_new_sample(ROOT, sample_root, adapter)
    assert result["status"] == "passed", result
    if job_id in {"job_insight_sync", "job_reprocess_insights"}:
        assert not (sample_root / "mariadb-data").exists()
    fixture = sample_root / "fixture"
    stdout = ""
    if job_id == "job_file_review_check":
        stdout = _file_review_terminal_stdout(fixture)
        receipt_path = next(
            path
            for path in (fixture / "state/formal-receipts").glob("*.json")
            if json.loads(path.read_text(encoding="utf-8"))["step"] == "download"
        )
        forged = json.loads(receipt_path.read_text(encoding="utf-8"))
        forged["handler"] = "fake_handler"
        receipt_path.write_text(json.dumps(forged), encoding="utf-8")
    else:
        relative = (
            "workspace/embedding-receipts.jsonl"
            if job_id == "job_insight_sync"
            else "workspace/reprocess-provider-receipts.jsonl"
        )
        receipt_path = fixture / relative
        rows = [
            json.loads(line)
            for line in receipt_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        rows[0]["handler"] = "fake_handler"
        receipt_path.write_text(
            "\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n",
            encoding="utf-8",
        )

    contract_ok, evidence = _contract_ok(
        adapter["success_contract"], fixture, stdout, ""
    )
    assert contract_ok is False
    assert any(value is False for value in evidence["checks"].values())


@pytest.mark.parametrize(
    ("kind", "job_id", "evidence"),
    [
        (
            "product_business_module_live",
            "job_business_module_live_check",
            [[11, 1], [12, 2], [13, 3]],
        ),
        ("product_heavy_translation_quality", "job_heavy_translation_quality_live", ["source-1.pdf", "source-2.pdf", "source-3.pdf"]),
        ("product_distill_train_gemma", "job_distill_train_gemma", [[4, 3, 1, 3], [5, 4, 1, 4], [6, 4, 2, 5]]),
        ("product_insight_sync", "job_insight_sync", [[1], [10, 11], [21]]),
        ("product_reprocess_insights", "job_reprocess_insights", [[1], [10, 11], [20, 21]]),
    ],
)
def test_product_fixture_kinds_bind_three_distinct_semantic_samples(
    tmp_path: Path, kind: str, job_id: str, evidence: list[object]
) -> None:
    observed = []
    for sample_id in (1, 2, 3):
        fixture = tmp_path / f"sample-{sample_id:03d}" / "fixture"
        _prepare_fixture(kind, fixture, job_id, source_root=ROOT)
        product = json.loads((fixture / "fixture.json").read_text(encoding="utf-8"))["product_input"]
        assert product["sample_id"] == sample_id
        if kind == "product_business_module_live":
            assert "results" not in product
            observed.append(
                [
                    product["expected_drive_matches"],
                    product["expected_calendar_imported"],
                ]
            )
        elif kind == "product_heavy_translation_quality":
            observed.append(product["pdf"])
        elif kind == "product_distill_train_gemma":
            counts = product["expected_counts"]
            profile = json.loads(
                (fixture / "inputs/training-profile.json").read_text(encoding="utf-8")
            )
            observed.append(
                [
                    counts["raw"],
                    counts["usable"],
                    counts["skipped"],
                    profile["optimizer_steps"],
                ]
            )
        elif kind == "product_insight_sync":
            observed.append(product["expected_new_ids"])
        else:
            observed.append(product["expected_selected_ids"])
    assert observed == evidence
    assert len({json.dumps(value, sort_keys=True) for value in observed}) == 3


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS release control")
@pytest.mark.parametrize(
    "job_id",
    [
        "job_business_module_live_check",
        "job_heavy_translation_quality_live",
        "job_distill_train_gemma",
    ],
)
def test_true_orchestration_contracts_reject_component_only_or_tampered_terminals(
    tmp_path: Path, job_id: str
) -> None:
    _jobs, registry = _inputs()
    adapter = next(
        row for row in registry["new_safe_adapters"] if row["job_id"] == job_id
    )
    sample_root = tmp_path / job_id / "sample-001"
    result = _execute_new_sample(ROOT, sample_root, adapter)
    assert result["status"] == "passed"
    fixture = sample_root / "fixture"

    if job_id == "job_business_module_live_check":
        report_path = fixture / "outputs/result.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["results"] = []
        report["result_count"] = 0
        report["checks"] = {
            "fixture_sample_bound": True,
            "typed_product_results": True,
            "required_modules_present": True,
            "all_fixture_modules_healthy": True,
            "summary_contains_each_module": True,
            "sensitive_fields_redacted": True,
        }
        report_path.write_text(json.dumps(report), encoding="utf-8")
    elif job_id == "job_heavy_translation_quality_live":
        transcript_path = fixture / "heavy_provider_transcript.json"
        transcript = json.loads(transcript_path.read_text(encoding="utf-8"))
        transcript_path.write_text(json.dumps(transcript[:-1]), encoding="utf-8")
    else:
        checkpoint = next(
            (fixture / "workspace/gemma-distill/adapters").glob(
                "adapter_bounded-sample-001/checkpoints/step-*.json"
            )
        )
        checkpoint.write_text('{"forged":true}\n', encoding="utf-8")

    contract_ok, _evidence = _contract_ok(
        adapter["success_contract"], fixture, "", ""
    )
    assert contract_ok is False


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS release control")
@pytest.mark.parametrize(
    "job_id",
    [
        "job_1770948489644_c5a469",
        "job_nightly_autopilot",
        "job_operational_hardening_audit",
    ],
)
def test_dynamic_terminal_contracts_reject_child_or_audit_tampering(
    tmp_path: Path, job_id: str
) -> None:
    _jobs, registry = _inputs()
    adapter = next(
        row for row in registry["new_safe_adapters"] if row["job_id"] == job_id
    )
    sample_root = tmp_path / job_id / "sample-001"
    result = _execute_new_sample(ROOT, sample_root, adapter)
    assert result["status"] == "passed", result
    fixture = sample_root / "fixture"
    report_path = fixture / "outputs/result.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))

    if "autopilot" in str(adapter["success_contract"]["type"]):
        report["process_observation"]["children"][0][
            "daemon_waiter_terminal"
        ] = False
        report["safety"]["subprocess_spawn_count"] += 1
    else:
        report["audit"]["final"]["report"].pop("osc_route_integrity")
        report["provider_observation"]["calls"][0]["terminal"] = False
    report_path.write_text(json.dumps(report), encoding="utf-8")

    contract_ok, evidence = _contract_ok(
        adapter["success_contract"], fixture, "", ""
    )
    assert contract_ok is False
    assert any(value is False for value in evidence["checks"].values())


@pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is a macOS release control")
@pytest.mark.parametrize(
    "job_id",
    [
        "job_1770948489644_c5a469",
        "job_nightly_autopilot",
        "job_operational_hardening_audit",
    ],
)
def test_dynamic_terminal_jobs_each_require_three_successful_samples(
    tmp_path: Path, job_id: str
) -> None:
    _jobs, registry = _inputs()
    adapter = next(
        row for row in registry["new_safe_adapters"] if row["job_id"] == job_id
    )
    result = _execute_new_samples(ROOT, tmp_path / job_id, adapter)

    assert result["status"] == "passed", result
    assert result["semantic_success"] is True
    assert result["successful_samples"] == 3
    assert result["duration_sample_count"] == 3
    assert result["sample_statuses"] == ["passed", "passed", "passed"]


@pytest.mark.parametrize(
    ("kind", "job_id"),
    [
        ("product_cortex_sync_terminal", "job_1770948489644_0726cf"),
        ("product_autopilot_tick_terminal", "job_1770948489644_c5a469"),
        ("product_autopilot_nightly_terminal", "job_nightly_autopilot"),
        ("product_operational_hardening_terminal", "job_operational_hardening_audit"),
    ],
)
def test_nonstorage_fixture_kinds_bind_three_distinct_terminal_semantics(
    tmp_path: Path, kind: str, job_id: str
) -> None:
    observed = []
    for sample_id in (1, 2, 3):
        fixture = tmp_path / f"sample-{sample_id:03d}" / "fixture"
        _prepare_fixture(kind, fixture, job_id, source_root=ROOT)
        product = json.loads((fixture / "fixture.json").read_text(encoding="utf-8"))["product_input"]
        assert product["sample_id"] == sample_id
        if kind == "product_cortex_sync_terminal":
            observed.append([product["expected_added"], product["expected_final_state"]])
        elif "autopilot" in kind:
            observed.append([product["expected_terminal_states"], product["expected_repairs"]])
        else:
            observed.append(product["expected"])
    assert len({json.dumps(value, sort_keys=True) for value in observed}) == 3


def test_registry_source_declares_formal_release_binding() -> None:
    registry = json.loads((ROOT / REGISTRY_PATH).read_text(encoding="utf-8"))
    binding = registry["release_binding"]
    contract = registry["execution_contract"]

    assert binding["formal_run_requires_release_id"] is True
    assert binding["formal_run_requires_manifest_sha256"] is True
    assert len(binding["cron_jobs_source_sha256"]) == 64
    assert contract["minimum_successful_samples"] == 3
    assert contract["real_job_body_required"] is True
    assert contract["help_is_forbidden"] is True
    assert contract["dispatcher_latency_is_forbidden"] is True
    assert contract["sleep_probe_is_forbidden"] is True
    assert contract["fake_handler_is_forbidden"] is True


def test_registry_and_baseline_share_the_current_cron_logical_binding() -> None:
    jobs, cron_digest = bound_cron_jobs(ROOT)
    registry = json.loads((ROOT / REGISTRY_PATH).read_text(encoding="utf-8"))
    baseline = json.loads((ROOT / "config/v3_schedule_realism_baseline.json").read_text(encoding="utf-8"))
    logical_digest = _logical_definition_sha256(jobs)

    assert registry["release_binding"]["cron_jobs_source_sha256"] == cron_digest
    assert registry["release_binding"]["logical_definition_sha256"] == logical_digest
    assert baseline["source_evidence"]["job_definitions_sha256"] == cron_digest
    assert baseline["source_evidence"]["logical_definition_sha256"] == logical_digest
    assert baseline["coverage"]["job_definitions"] == len(jobs)
    assert baseline["coverage"]["enabled_job_definitions"] == sum(
        job.get("enabled") is True for job in jobs
    )


def test_registry_accepts_only_logically_identical_candidate_snapshot_digest() -> None:
    jobs, source_digest = bound_cron_jobs(ROOT)
    candidate_digest = "f" * 64
    assert candidate_digest != source_digest
    _load_registry(ROOT, jobs, candidate_digest)

    tampered = json.loads(json.dumps(jobs))
    tampered[0]["cron"] = "59 23 31 12 *"
    with pytest.raises(ScheduleBodyRegistryError, match="logical cron binding drifted"):
        _load_registry(ROOT, tampered, candidate_digest)

#!/usr/bin/env python3
"""Build an immutable MAGI V3 release bundle in a caller-owned staging directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from magi_v3.supply_chain import SupplyChainError, validate_release_supply_chain_binding

CONFIG_FILES = (
    "config/v3_capability_manifest.json",
    "config/v3_cutover_gates.json",
    "config/v3_launchagent_roles.json",
    "config/v3_pre_cutover_readiness.json",
    "config/v3_resource_policy.json",
    "config/v3_schedule_realism_baseline.json",
    "config/v3_live_validation_service_manifest.json",
    "config/v3_service_manifest.json",
    "config/v3_transcription_backends.json",
    "config/v3_validation_campaign.json",
)
SOURCE_DIRECTORIES = (
    "magi_v3",
    "api",
    "integrations/debt_robot",
    "src/supplement_core",
    "skills",
    "casper_ecosystem",
    "gui",
    "mobile_app",
    "migrations",
    "scripts",
    "config",
    "templates",
    "static",
    "tests/v3",
    "tests/support",
    "docs/architecture/v3",
)
REQUIRED_PACKAGE_FILES = (
    ".env.example",
    "daemon.py",
    "osc.py",
    "bin/magi-v3-python",
    "bin/agent_mcp.py",
    "requirements.txt",
    "requirements-selfhost.txt",
    "requirements-optional.txt",
    "pyproject.toml",
    "install-magi.cmd",
    "install-magi.command",
    "install-magi.ps1",
    "config/selfhost.example.json",
    "config/selfhost.schema.json",
    "docs/SELFHOST_DEPLOYMENT.md",
    "json/datastores.json",
    "json/holidays_config.json",
    "json/models.json",
    "json/nodes.json",
    "json/services.json",
    "resources/osc/photo/lawyer_stamp.png",
    "resources/osc/photo/logo.png",
    "resources/osc/photo/namecard.png",
    # Reviewed, minimal MIT snapshot used by the public local-only video
    # renderer.  Pin individual files so an unrelated upstream tool can never
    # enter the immutable release by merely appearing under third_party.
    "third_party/video_autopilot_kit/LICENSE",
    "third_party/video_autopilot_kit/MAGI_INTEGRATION.json",
    "third_party/video_autopilot_kit/runtime/__init__.py",
    "third_party/video_autopilot_kit/runtime/portrait_normalizer.py",
    "data/templates/D_supplement.docx",
    "integrations/debt_robot/document/A.docx",
    "integrations/debt_robot/document/B.docx",
    "integrations/debt_robot/document/C.docx",
    "integrations/debt_robot/document/D.docx",
    "mobile_app/capacitor.config.json",
    "mobile_app/package.json",
    "mobile_app/package-lock.json",
    "mobile_app/www/index.html",
    "mobile_app/android/app/src/main/assets/capacitor.config.json",
    "mobile_app/android/app/src/main/AndroidManifest.xml",
    "casper_ecosystem/law_firm_orchestrators/_browser_helpers.py",
    "casper_ecosystem/law_firm_orchestrators/legalbridge_core.py",
    "casper_ecosystem/law_firm_orchestrators/open_case_vision.py",
    "casper_ecosystem/law_firm_orchestrators/simulated_line.py",
    "scripts/v3_validation/offline_machine_gate_builder.py",
    "scripts/v3_validation/isolated_live_plan_builder.py",
    "scripts/v3_validation/isolated_live_evidence.py",
    "scripts/v3_validation/schemas/isolated-live-execution-plan.schema.json",
    "scripts/ops/active_release_service_launcher.py",
    "scripts/ops/v3_host_singleton_migration.py",
    "config/launchagents/com.magi.memory-watchdog.plist",
    "config/launchagents/com.magi.mlx-mtp.plist",
    "config/launchagents/com.magi.paperclip-share-gateway.plist",
    "config/launchagents/com.magi.paperclip-share-tunnel.plist",
    # Required by tests/v3 modules that import shared helpers through the
    # ``tests.v3`` package.  Without this marker, an installed third-party
    # ``tests`` package can shadow the sealed candidate's test tree.
    "tests/__init__.py",
)
REQUIRED_TEST_TARGETS = (
    "tests/conftest.py",
    "tests/test_admin_runtime_blueprint.py",
    "tests/test_ai_draft_dispatch_quality.py",
    "tests/test_agent_readiness_gate.py",
    "tests/test_agentic_bridges.py",
    "tests/test_agentic_contracts.py",
    "tests/test_agentic_planner.py",
    "tests/test_answer_verifier.py",
    "tests/test_autopilot_child_binding.py",
    "tests/test_archive_wizard_execute.py",
    "tests/test_balthasar_local_empty.py",
    "tests/test_browser_security_policy_rc643.py",
    "tests/test_business_module_live_check.py",
    "tests/test_case_display_authoritative_name.py",
    "tests/test_case_statistics_tool.py",
    "tests/test_clarification_gate.py",
    "tests/test_content_quality_hardening_rc390.py",
    "tests/test_controlled_autonomy_policy.py",
    "tests/test_controlled_autonomy_runtime.py",
    "tests/test_cookie_cutter_blueprint.py",
    "tests/test_cookie_stl.py",
    "tests/test_dashboard_pages_blueprint.py",
    "tests/test_deadline_reminder_insight_gate.py",
    "tests/test_daily_self_evolution.py",
    "tests/test_context_labels.py",
    "tests/test_cron_schedule_stagger_rc230.py",
    "tests/test_deep_task_control_rc568.py",
    "tests/test_debt_lawyer_contact.py",
    "tests/test_debt_robot_source_modules.py",
    "tests/test_document_reader.py",
    "tests/test_drive_case_sync_hash_timeout.py",
    "tests/test_drive_sync_identical_aliases_rc245.py",
    "tests/test_export_public_urls.py",
    "tests/test_export_docx_security.py",
    "tests/test_file_review_notifications.py",
    "tests/test_forensic_transcript_live_command.py",
    "tests/test_forensic_transcript_verifier.py",
    "tests/test_generative_quality_live.py",
    "tests/test_gemma_distill_schedule_semantics.py",
    "tests/test_golem_console.py",
    "tests/test_grounded_ai_heavy_fail_closed.py",
    "tests/test_heavy_translation_quality_live.py",
    "tests/test_input_method_watchdog.py",
    "tests/test_intent_tool_grounding_rc555.py",
    "tests/test_intent_tool_adversarial_rc556.py",
    "tests/test_install_omlx_text.py",
    "tests/test_judgment_nvidia_source_bound_rc194.py",
    "tests/test_judgment_summary_quality_rc170.py",
    "tests/test_judicial_summary_quality.py",
    "tests/test_judgment_staged_backfill_rc223.py",
    "tests/test_laf_case_classifier.py",
    "tests/test_laf_case_storage_authority.py",
    "tests/test_laf_gmail_dispatch_scan.py",
    "tests/test_laf_gmail_spam_restore.py",
    "tests/test_laf_portal_new_files_scan.py",
    "tests/test_laf_portal_retry_decoupling.py",
    "tests/test_laf_portal_retry_heartbeat.py",
    "tests/test_laf_portal_retry_reconciliation.py",
    "tests/test_legaltech_taiwan_law_mcp_rc512.py",
    "tests/test_lottery_blueprint.py",
    "tests/test_live_gate_deployment_paths.py",
    "tests/test_local_deep_queue_worker_rc568.py",
    "tests/test_local_model_champion_eval_rc568.py",
    "tests/test_mobile_auth_routes.py",
    "tests/test_memory_grounding.py",
    "tests/test_memory_policy.py",
    "tests/test_message_intent_boundaries.py",
    "tests/test_natural_language_agent_quality_rc549.py",
    "tests/test_osc_events_refresh_outcome_semantics.py",
    "tests/test_market_briefing_quality_gate.py",
    "tests/test_saas_readiness_migration.py",
    "tests/test_saas_commercial_foundations.py",
    "tests/test_selfhost_portability.py",
    "tests/test_selfhost_release_smoke.py",
    "tests/test_sentencing_trend_chat.py",
    "tests/test_sentencing_trends.py",
    "tests/test_nas_pdf_ocr_worker_lock.py",
    "tests/test_nightly_regression_production_suites.py",
    "tests/test_omlx_watchdog_switch_lock.py",
    "tests/test_optional_line_health.py",
    "tests/test_rc600_runtime_bootstrap_and_reporting.py",
    "tests/test_model_live_gate_degraded_profiles.py",
    "tests/test_model_router_deep_queue_rc568.py",
    "tests/test_osc_address_label.py",
    "tests/test_osc_backup_endpoints.py",
    "tests/test_osc_checklists_endpoints.py",
    "tests/test_osc_closed_case_archive.py",
    "tests/test_osc_csv_import_export.py",
    "tests/test_osc_document_reuse_api.py",
    "tests/test_osc_documents_stamp_endpoint.py",
    "tests/test_osc_file_frontend_runtime.py",
    "tests/test_osc_files_move.py",
    "tests/test_osc_folder_rename.py",
    "tests/test_osc_laf_debt_required_checklist.py",
    "tests/test_osc_p2_discord_and_theme.py",
    "tests/test_osc_pdf_blueprint.py",
    "tests/test_pdf_cross_case_identity_confirmation.py",
    "tests/test_case_number_auto_reconcile.py",
    "tests/test_osc_saas_workbench.py",
    "tests/test_osc_todos_bulk_complete.py",
    "tests/test_overdue_confirmation_calendar_policy.py",
    "tests/test_osc_web_smoke.py",
    "tests/test_pdf_namer_nightly_process_isolation.py",
    "tests/test_pdf_namer_state_isolation.py",
    "tests/test_safe_process.py",
    "tests/test_obsidian_ingest_checkpoint.py",
    "tests/v3/test_legacy_mutable_state_routing.py",
    "tests/v3/test_core_mutable_state_isolation.py",
    "tests/v3/test_manual_skill_mutable_state_isolation.py",
    "tests/v3/test_pdf_namer_handoff.py",
    "tests/v3/test_scheduled_mutable_state_routing.py",
    "tests/v3/test_skill_overlay_isolation.py",
    "tests/v3/test_runtime_isolation_regressions.py",
    "tests/v3/test_offline_machine_gate_builder.py",
    "tests/v3/test_isolated_live_plan_builder.py",
    "tests/v3/test_isolated_live_evidence.py",
    "tests/v3/test_operational_hardening_fixture_paths.py",
    "tests/test_startup_resource_policy.py",
    "tests/test_tools_api_async_jobs.py",
    "tests/test_tools_api_runtime.py",
    "tests/test_tools_api_shortcut_endpoints.py",
    "tests/test_telegram_history.py",
    "tests/test_tool_registry_contracts.py",
    "tests/test_transcribe_runtime.py",
    "tests/test_tailscale_funnel_healthcheck.py",
    "tests/test_translation_strict_nim_provenance.py",
    "tests/test_video_studio_blueprint.py",
    "tests/test_durable_deep_delivery_rc568.py",
    "tests/test_generation_quality_failclosed_rc568.py",
    "tests/test_transcript_filename_repair.py",
    "tests/test_transcript_partial_retry_rc239.py",
    "tests/test_transcript_portal_empty_failclosed_rc223.py",
    "tests/test_tw_output_guard_fidelity.py",
    "tests/test_weekend_resummary_budget_semantics.py",
    "tests/test_rc241_runtime_and_batch_regressions.py",
    "tests/test_reconcile_overdue_todos.py",
    "tests/test_v3_laf_dedup_compat.py",
    "tests/test_web_runtime_blueprint.py",
    "tests/test_web_information_architecture.py",
    "tests/v3/test_active_release_service_launcher.py",
    "tests/v3/test_host_service_process_identity.py",
    "tests/v3/test_host_singleton_migration.py",
)
# Packaging presence and test execution are deliberately separate contracts.
# The bundle builder content-binds these targets but never claims they passed;
# cutover validation must attach real pytest evidence independently.
REQUIRED_FILES = REQUIRED_PACKAGE_FILES + REQUIRED_TEST_TARGETS
EXCLUDED_COMPONENTS = frozenset(
    {
        ".agent",
        ".cache",
        ".embed_cache",
        ".gradle",
        ".runtime",
        ".venv",
        ".versions",
        ".pytest_cache",
        "__pycache__",
        "_bg_jobs",
        "browsers",
        "build",
        "cache",
        "caches",
        "capacitor-cordova-android-plugins",
        "downloads",
        "exports",
        "index_cache",
        "laf_downloads",
        "log",
        "logs",
        "node_modules",
        "output",
        "outputs",
        "queue",
        "queues",
        "run",
        "runs",
        "state",
        "states",
        "temp",
        "tmp",
        "venv",
    }
)
EXCLUDED_MUTABLE_FILES = frozenset(
    {
        ".ds_store",
        ".swo",
        ".swp",
        ".swx",
        ".review_submit_pending.json",
        "_pending_todos.deadletter.jsonl",
        "_pending_todos.jsonl",
        "review_cache.json",
    }
)
PDF_NAMER_MUTABLE_FILES = frozenset(
    {
        "_case_index.json",
        "_corrections.json",
        "_filing_log.json",
        "_learned_filename_rules.json",
        "_nightly_report.json",
        "_nightly_train.log",
        "_threshold_state.json",
        "db_rules_cache.json",
        "training_data.json",
    }
)
DEBT_ADDRESS_MUTABLE_FILES = frozenset(
    {
        "integrations/debt_robot/document/all adress - bank.csv",
        "integrations/debt_robot/document/all adress - bank.json",
        "integrations/debt_robot/document/all adress - company.csv",
        "integrations/debt_robot/document/all adress - company.json",
    }
)
MOBILE_APP_GENERATED_FILES = frozenset(
    {
        "mobile_app/android/app/src/main/assets/capacitor.plugins.json",
        "mobile_app/android/app/src/main/res/xml/config.xml",
    }
)
# These are audited workstation-only helpers/configuration.  Their names are
# intentionally exact: an unfamiliar ignored ``.py``/plist under an allowlisted
# directory must remain visible to the git snapshot equality gate and fail the
# build instead of being silently packaged or broadly discarded.
LOCAL_ONLY_RELEASE_FILES = frozenset(
    {
        "casper_ecosystem/law_firm_orchestrators/file_review_flow.py",
        "casper_ecosystem/law_firm_orchestrators/laf_capture_all_workflows.py",
        "casper_ecosystem/law_firm_orchestrators/laf_html_capture.py",
        "casper_ecosystem/law_firm_orchestrators/legalbridge_config.json",
        "casper_ecosystem/law_firm_orchestrators/osc/__init__.py",
        "config/launchagents/com.magi.omlx-nemotron-parse.plist",
        "config/launchagents/com.magi.osc-folder-helper.plist",
    }
)
ORCHESTRATOR_MUTABLE_FILES = frozenset(
    {
        ".draft_processed_emails.json",
        "_laf_condition_manual_done.json",
        "tools_runtime_events.jsonl",
    }
)

# Exact, reviewed V2 compatibility surfaces that contain literal local/NAS
# fallback examples.  V3-native modules are never eligible for this exception.
# Adding a new file with a literal absolute workstation path therefore fails
# closed until it is either made environment-driven or explicitly reviewed.
AUDITED_V2_ABSOLUTE_PATH_FILES = frozenset(
    {
        "api/blueprints/osc_cases.py",
        "api/blueprints/web_runtime.py",
        "api/case_path_mapper.py",
        "api/commands/apple_commands.py",
        "api/domains/judicial_api_cache.py",
        "api/handlers/document_handler.py",
        "api/laf_poa_docx.py",
        "api/nas_mount_guard.py",
        "api/osc/drafts.py",
        "api/osc/drive_case_sync.py",
        "api/osc/folder_utils.py",
        "api/osc/utils.py",
        "api/platforms/safe_process.py",
        "api/tw_output_guard.py",
        "config/launchagents/com.magi.mlx-mtp.plist",
        "config/launchagents/com.magi.memory-watchdog.plist",
        "config/launchagents/com.magi.omlx-restore.plist",
        "config/v3_cutover_gates.json",
        "config/bin/omlx_switch_model.sh",
        "casper_ecosystem/law_firm_orchestrators/file_review_automation.py",
        "casper_ecosystem/law_firm_orchestrators/judicial_automation_v2.py",
        "casper_ecosystem/law_firm_orchestrators/laf_automation_v2.py",
        "casper_ecosystem/law_firm_orchestrators/laf_deep_extract_backfill.py",
        "casper_ecosystem/law_firm_orchestrators/laf_flow.py",
        "casper_ecosystem/law_firm_orchestrators/laf_folder_builder.py",
        "casper_ecosystem/law_firm_orchestrators/laf_handler.py",
        "casper_ecosystem/law_firm_orchestrators/laf_nightly_audit.py",
        "casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py",
        "casper_ecosystem/law_firm_orchestrators/laf_orchestrator_docmixins.py",
        "casper_ecosystem/law_firm_orchestrators/laf_progress_helper.py",
        "casper_ecosystem/law_firm_orchestrators/laf_vision.py",
        "casper_ecosystem/law_firm_orchestrators/legalbridge_core.py",
        "casper_ecosystem/law_firm_orchestrators/line_notifier.py",
        "casper_ecosystem/law_firm_orchestrators/osc/folder_utils.py",
        "casper_ecosystem/law_firm_orchestrators/patch_file_review.py",
        "daemon.py",
        "docs/architecture/v3/generated/v2_inventory.json",
        "gui/magi_menubar.py",
        "scripts/magi_cli.sh",
        "scripts/magi_doctor.py",
        "scripts/nightly_council.py",
        "scripts/omlx_patch_and_start.sh",
        "scripts/generate_detailed_user_manual.py",
        "scripts/packaging/runtime_bootstrap.py",
        "scripts/ops/benchmark_pdf_bookmarker.py",
        "scripts/ops/benchmark_pdf_namer.py",
        "scripts/ops/clean_closed_case_residue.py",
        "scripts/ops/cleanup_synology_empty_case_shells.py",
        "scripts/ops/cleanup_judgments_leaks.py",
        "scripts/ops/disk_cleanup_healthcheck.py",
        "scripts/ops/osc_draft_live_compare.py",
        "scripts/ops/osc_shell_nas_helper.py",
        "scripts/ops/repair_transcript_filenames.py",
        "scripts/ops/slow_archive_closed_cases.py",
        "scripts/ops/smoke_test_full.py",
        "scripts/ops/triage_transcript_duplicates.py",
        "skills/apple/contacts_bridge.py",
        "skills/apple/eventkit_bridge.py",
        "skills/bilingual-docx/scripts/normalize.js",
        "skills/documents/nas_pdf_ocr_worker.py",
        "skills/documents/pdf_bridge.py",
        "skills/docx-editor/lib/anchor_matcher.py",
        "skills/iron-dome/core.py",
        "skills/legal/laf.py",
        "skills/legal_attest/action.py",
        "skills/management/auto_skill.py",
        "skills/obsidian/SKILL.md",
        "skills/obsidian/action.py",
        "skills/obsidian/bootstrap_synology_vault.py",
        "skills/ops/spotlight_search.py",
        "skills/ops/finder_ops.py",
        "skills/ops/platform_utils.py",
        "skills/osc-orchestrator/action.py",
        "skills/pdf-namer/action.py",
        "skills/pdf-namer/rename_watcher.py",
        "skills/screenshot-sorter-tw/SKILL.md",
        "skills/research/web_research.py",
        "static/osc/tabs/cases.js",
        "static/osc/tabs/file_manager.js",
        "templates/dashboard_nerv.html",
        "templates/partials/osc/cases.html",
        "templates/partials/osc/fileManager.html",
        "tests/support/side_effect_guard.py",
        "tests/v3/test_native_osc_production.py",
        "tests/v3/test_native_osc_cases.py",
    }
)
SYNTHETIC_ABSOLUTE_PATH_FILES = frozenset(
    {
        "scripts/ops/continuous_longfile_3ch_stress.py",
        "scripts/ops/heavy_translation_quality_live.py",
        "scripts/ops/magi_acceptance_gate.py",
        "scripts/ops/nemotron_parse_hf_baseline.py",
        "scripts/ops/nemotron_phase1b_compare.py",
        "scripts/ops/paperclip_deep_verify_v6.py",
        "scripts/ops/paperclip_filemanager_deep_verify.py",
        "scripts/ops/smoke_all_desktop_pdfs_3tasks.py",
        "scripts/ops/smoke_judgment_translation_3ch.py",
        "scripts/ops/smoke_test_full.py",
        "scripts/v3_release_bundle.py",
        "scripts/v3_validation/schedule_body_registry.py",
        "scripts/v3_validation/perf_compat.py",
        "skills/laf-portal-automation/references/snapshot_training.json",
        "skills/market-briefing/test_agent_live.py",
        "skills/market-briefing/test_committee_logic.py",
        "tests/test_admin_runtime_blueprint.py",
        "tests/test_laf_case_storage_authority.py",
        "tests/test_osc_closed_case_archive.py",
        "tests/test_osc_folder_rename.py",
        "tests/test_osc_pdf_blueprint.py",
        "tests/test_osc_web_smoke.py",
        "tests/v3/test_compat_replay.py",
        "tests/v3/test_core_mutable_state_isolation.py",
        "tests/v3/test_cron_snapshot.py",
        "tests/v3/test_fault_certification.py",
        "tests/v3/test_native_osc_cases.py",
        "tests/v3/test_native_osc_production.py",
        "tests/v3/test_physical_fault_drill.py",
        "tests/v3/test_provisional_resource_window_execute.py",
        "tests/v3/test_release_bundle.py",
    }
)
PRIVACY_AUDIT_TEXT_SUFFIXES = frozenset(
    {".css", ".html", ".js", ".json", ".md", ".plist", ".py", ".sh", ".txt"}
)
ABSOLUTE_LOCAL_LITERAL_RE = re.compile(
    rb"(?P<quote>['\"])(?P<literal>"
    rb"/Users/[A-Za-z0-9._<>-]+(?:/[^'\"\r\n]*)?|"
    rb"/Volumes/[A-Za-z0-9._<>-]+(?:/[^'\"\r\n]*)?|"
    rb"[A-Za-z]:[\\/][^'\"\r\n]*|"
    rb"\\\\[A-Za-z0-9._<>-]+(?:[\\/][^'\"\r\n]*)+"
    rb")"
    rb"(?P=quote)"
)
_GENERIC_PATH_COMPONENTS = frozenset(
    {
        b"...",
        b"%",
        b"<share>",
        b"<user>",
        b"active-share",
        b"archive",
        b"case_folder",
        b"homes",
        b"homes-",
        b"home",
        b"program files",
        b"program files (x86)",
        b"public",
        b"share",
        b"synologydrive",
        b"temp",
        b"tmp",
        b"user",
        b"users",
        b"username",
        b"windows",
    }
)
FORBIDDEN_SECRET_SUFFIXES = (
    ".key",
    ".p12",
    ".pem",
    ".pfx",
)
RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
FUNCTIONAL_RELEASE_SUFFIXES = frozenset({".plist", ".py", ".sh"})
COMPLETION_MARKER = "RELEASE_COMPLETE.json"
MANIFEST_NAME = "release-manifest.json"


class ReleaseBundleError(ValueError):
    """Raised before a bundle can be considered complete."""


@dataclass(frozen=True, order=True)
class SourceEntry:
    path: str
    sha256: str
    size: int
    mode: int
    source_mode: int
    device: int
    inode: int
    mtime_ns: int

    def manifest_entry(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "mode": f"{self.mode:04o}",
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _excluded(relative: Path, *, is_directory: bool = False) -> bool:
    lower_parts = tuple(part.lower() for part in relative.parts)
    name = lower_parts[-1] if lower_parts else ""
    exam_tutor_immutable_asset = (
        not is_directory
        and len(lower_parts) >= 3
        and lower_parts[:2] == ("static", "exam_tutor")
        and (
            relative.as_posix() in {
                "static/exam_tutor/choice_bank.json",
                "static/exam_tutor/essay_bank.json",
                "static/exam_tutor/curated_practice_weights.json",
                "static/exam_tutor/extended_source_catalog.json",
                "static/exam_tutor/trend_analysis.json",
            }
            or (
                lower_parts[2] in {"source-pdfs", "essay-source-pdfs"}
                and Path(name).suffix == ".pdf"
            )
        )
    )
    static_not_asset = (
        not is_directory
        and
        bool(lower_parts)
        and lower_parts[0] == "static"
        and not exam_tutor_immutable_asset
        and Path(name).suffix
        not in {".css", ".gif", ".ico", ".jpeg", ".jpg", ".js", ".png", ".svg", ".ttf", ".webp", ".woff", ".woff2"}
        and relative.as_posix() != "static/iron_dome_patterns.json"
    )
    mobile_generated_web_assets = (
        len(lower_parts) >= 7
        and lower_parts[:7]
        == ("mobile_app", "android", "app", "src", "main", "assets", "public")
    )
    mutable_named_data = (
        Path(name).suffix in {".json", ".jsonl", ".lock"}
        and any(token in name for token in ("_cache", "_latest", "_lock", "_pending", "_state", "_status"))
    )
    pdf_namer_mutable = (
        len(lower_parts) >= 3
        and lower_parts[:2] == ("skills", "pdf-namer")
        and name in PDF_NAMER_MUTABLE_FILES
    )
    posix = relative.as_posix()
    judgment_mutable = (
        len(lower_parts) >= 3
        and lower_parts[:2] == ("skills", "judgment-collector")
        and (name == "judgments.json" or name.startswith("judgments.json.bak."))
    )
    orchestrator_mutable = (
        len(lower_parts) == 3
        and lower_parts[:2] == ("casper_ecosystem", "law_firm_orchestrators")
        and name in ORCHESTRATOR_MUTABLE_FILES
    )
    external_hearing_template = (
        len(lower_parts) >= 3
        and lower_parts[:2] == ("templates", "legal")
        and Path(name).suffix == ".docx"
    )
    return (
        any(part in EXCLUDED_COMPONENTS for part in lower_parts)
        or mobile_generated_web_assets
        or static_not_asset
        or mutable_named_data
        or pdf_namer_mutable
        or judgment_mutable
        or orchestrator_mutable
        or external_hearing_template
        or posix in DEBT_ADDRESS_MUTABLE_FILES
        or posix in MOBILE_APP_GENERATED_FILES
        or posix in LOCAL_ONLY_RELEASE_FILES
        or name in EXCLUDED_MUTABLE_FILES
        or name == ".env"
        or (name.startswith(".env.") and name != ".env.example")
        or name.endswith((".db", ".jsonl", ".log", ".pyc", ".pyo", ".sqlite", ".sqlite3"))
    )


def _release_privacy_audit(
    source_root: Path,
    entries: Iterable[SourceEntry],
) -> dict[str, Any]:
    """Fail closed on new absolute workstation literals and emit no contents.

    The evidence contains aggregate counts and a digest of path/line/category
    metadata only.  It never serializes the matched path literal, surrounding
    source line, client data, or document contents.
    """

    findings: list[tuple[str, int, str]] = []
    inspected = 0
    reviewed_files: set[str] = set()
    reviewed_hits = 0
    synthetic_hits = 0
    for entry in entries:
        relative = Path(entry.path)
        if relative.suffix.lower() not in PRIVACY_AUDIT_TEXT_SUFFIXES:
            continue
        inspected += 1
        content = (source_root / relative).read_bytes()
        for match in ABSOLUTE_LOCAL_LITERAL_RE.finditer(content):
            line = content.count(b"\n", 0, match.start()) + 1
            literal = match.group("literal")
            category = "v3_native" if relative.parts and relative.parts[0] == "magi_v3" else "legacy_v2"
            if entry.path in SYNTHETIC_ABSOLUTE_PATH_FILES:
                category = "synthetic_fixture"
            findings.append((entry.path, line, category))
            if category == "v3_native":
                raise ReleaseBundleError(
                    f"V3-native source contains an absolute workstation path literal: {entry.path}:{line}"
                )
            if category == "synthetic_fixture":
                synthetic_hits += 1
                continue
            normalized = literal.replace(b"\\", b"/")
            lowered = normalized.lower()
            parts = [part for part in normalized.split(b"/") if part]
            personal_user_path = False
            private_share_path = False
            if lowered.startswith(b"/users/") and len(parts) >= 2:
                personal_user_path = parts[1].lower() not in _GENERIC_PATH_COMPONENTS
            elif lowered.startswith(b"/volumes/") and len(parts) >= 2:
                private_share_path = parts[1].lower() not in _GENERIC_PATH_COMPONENTS
            elif re.match(rb"^[a-z]:/", lowered):
                remainder = parts[1:] if parts and parts[0].endswith(b":") else parts
                first_component = remainder[0].lower().rstrip(b".%") if remainder else b""
                if first_component == b"users":
                    user_component = remainder[1].lower() if len(remainder) >= 2 else b""
                    personal_user_path = (
                        user_component not in _GENERIC_PATH_COMPONENTS
                        and not user_component.startswith((b"{", b"<"))
                    )
                else:
                    private_share_path = bool(
                        remainder
                        and remainder[0].lower() not in _GENERIC_PATH_COMPONENTS
                        and first_component not in _GENERIC_PATH_COMPONENTS
                        and not remainder[0].startswith((b"{", b"<"))
                        and remainder[0] not in {"01_案件".encode(), "03_工作資料".encode()}
                    )
            elif lowered.startswith(b"//"):
                private_share_path = not any(
                    component.lower() in _GENERIC_PATH_COMPONENTS for component in parts[:2]
                )
            if personal_user_path or private_share_path:
                raise ReleaseBundleError(
                    f"release source contains a private workstation path literal: {entry.path}:{line}"
                )
            if entry.path not in AUDITED_V2_ABSOLUTE_PATH_FILES:
                raise ReleaseBundleError(
                    f"release source contains an unaudited absolute workstation path literal: {entry.path}:{line}"
                )
            reviewed_files.add(entry.path)
            reviewed_hits += 1
    encoded = json.dumps(findings, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    policy = json.dumps(
        sorted(AUDITED_V2_ABSOLUTE_PATH_FILES), separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return {
        "schema_version": 1,
        "status": "passed",
        "inspected_text_file_count": inspected,
        "raw_hits": len(findings),
        "reviewed_non_sensitive_compat_hits": reviewed_hits,
        "synthetic_fixture_hits": synthetic_hits,
        "reviewed_non_sensitive_compat_file_count": len(reviewed_files),
        "violations": 0,
        "finding_metadata_sha256": hashlib.sha256(encoded).hexdigest(),
        "policy_sha256": hashlib.sha256(policy).hexdigest(),
        "content_in_evidence": False,
    }


def _forbidden_secret(relative: Path) -> bool:
    name = relative.name.lower()
    return name.endswith(FORBIDDEN_SECRET_SUFFIXES)


def _entry_from_file(source_root: Path, path: Path, relative: Path) -> SourceEntry:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ReleaseBundleError(f"source is not a regular file: {relative.as_posix()}")
    digest = _sha256_file(path)
    after = path.lstat()
    signature_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        stat.S_IMODE(before.st_mode),
    )
    signature_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        stat.S_IMODE(after.st_mode),
    )
    if signature_before != signature_after:
        raise ReleaseBundleError(f"source changed while hashing: {relative.as_posix()}")
    try:
        path.resolve(strict=True).relative_to(source_root)
    except ValueError as exc:
        raise ReleaseBundleError(f"source escapes repository root: {relative.as_posix()}") from exc
    return SourceEntry(
        path=relative.as_posix(),
        sha256=digest,
        size=before.st_size,
        # A completed release is executable but never writable.  Preserve only
        # executable intent from git/source mode; all other permission bits are
        # canonical release policy rather than checkout accidents.
        mode=0o555 if stat.S_IMODE(before.st_mode) & 0o111 else 0o444,
        source_mode=stat.S_IMODE(before.st_mode),
        device=before.st_dev,
        inode=before.st_ino,
        mtime_ns=before.st_mtime_ns,
    )


def _scan_directory(source_root: Path, directory: Path, relative: Path) -> Iterable[SourceEntry]:
    if directory.is_symlink():
        raise ReleaseBundleError(f"symlinks are forbidden in release sources: {relative.as_posix()}")
    if not directory.is_dir():
        raise ReleaseBundleError(f"required source directory is missing: {relative.as_posix()}")
    with os.scandir(directory) as iterator:
        children = sorted(iterator, key=lambda item: item.name)
    for child in children:
        child_relative = relative / child.name
        child_path = Path(child.path)
        if child.is_symlink():
            raise ReleaseBundleError(
                f"symlinks are forbidden in release sources: {child_relative.as_posix()}"
            )
        if child.is_dir(follow_symlinks=False):
            if not _excluded(child_relative, is_directory=True):
                yield from _scan_directory(source_root, child_path, child_relative)
            continue
        if not child.is_file(follow_symlinks=False):
            raise ReleaseBundleError(
                f"special files are forbidden in release sources: {child_relative.as_posix()}"
            )
        if _forbidden_secret(child_relative):
            raise ReleaseBundleError(
                f"secret-bearing file type is forbidden in release sources: {child_relative.as_posix()}"
            )
        if not _excluded(child_relative):
            yield _entry_from_file(source_root, child_path, child_relative)


def snapshot_sources(source_root: Path) -> tuple[SourceEntry, ...]:
    root = source_root.resolve(strict=True)
    entries: list[SourceEntry] = []
    for relative_text in SOURCE_DIRECTORIES:
        relative = Path(relative_text)
        entries.extend(_scan_directory(root, root / relative, relative))
    for relative_text in REQUIRED_FILES:
        relative = Path(relative_text)
        path = root / relative
        if path.is_symlink():
            raise ReleaseBundleError(f"symlinks are forbidden in release sources: {relative_text}")
        if not path.is_file():
            raise ReleaseBundleError(f"required source file is missing: {relative_text}")
        if any(
            relative == Path(directory) or Path(directory) in relative.parents
            for directory in SOURCE_DIRECTORIES
        ):
            # The recursive directory scan already content-binds this file.
            # Keep it in REQUIRED_FILES as an explicit release contract without
            # producing a duplicate source entry.
            continue
        entries.append(_entry_from_file(root, path, relative))
    entries.sort()
    paths = [entry.path for entry in entries]
    if len(paths) != len(set(paths)):
        raise ReleaseBundleError("release source allowlist produced duplicate paths")
    if not entries:
        raise ReleaseBundleError("release source allowlist is empty")
    present = set(paths)
    missing_required_configs = sorted(set(CONFIG_FILES) - present)
    if missing_required_configs:
        raise ReleaseBundleError(
            f"required source file is missing: {missing_required_configs[0]}"
        )
    return tuple(entries)


def _snapshot_digest(entries: Iterable[SourceEntry]) -> str:
    payload = [entry.manifest_entry() for entry in entries]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _same_file_signature(file_stat: os.stat_result, entry: SourceEntry) -> bool:
    return (
        file_stat.st_dev == entry.device
        and file_stat.st_ino == entry.inode
        and file_stat.st_size == entry.size
        and file_stat.st_mtime_ns == entry.mtime_ns
        and stat.S_IMODE(file_stat.st_mode) == entry.source_mode
    )


def _copy_entry(source_root: Path, staging_root: Path, entry: SourceEntry) -> None:
    source = source_root / entry.path
    destination = staging_root / entry.path
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    source_fd = os.open(source, flags)
    digest = hashlib.sha256()
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or not _same_file_signature(before, entry):
            raise ReleaseBundleError(f"source changed before copy: {entry.path}")
        with os.fdopen(source_fd, "rb", closefd=False) as source_handle, destination.open("xb") as output:
            for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        after = os.fstat(source_fd)
        if not _same_file_signature(after, entry) or digest.hexdigest() != entry.sha256:
            raise ReleaseBundleError(f"source changed during copy: {entry.path}")
    finally:
        os.close(source_fd)
    destination.chmod(entry.mode)


def _assert_safe_staging(source_root: Path, staging_dir: Path) -> Path:
    if staging_dir.exists() or staging_dir.is_symlink():
        raise ReleaseBundleError("staging directory must not already exist")
    parent = staging_dir.parent.resolve(strict=True)
    if not parent.is_dir():
        raise ReleaseBundleError("staging parent must be a directory")
    staging = parent / staging_dir.name
    source = source_root.resolve(strict=True)
    try:
        staging.relative_to(source)
    except ValueError:
        pass
    else:
        raise ReleaseBundleError("staging directory must be outside the source repository")
    application_support = _application_support_root()
    try:
        staging.relative_to(application_support)
    except ValueError:
        pass
    else:
        raise ReleaseBundleError("release bundles must never target Application Support")
    for variable in ("MAGI_V3_STATE_DIR", "MAGI_STATE_DIR", "MAGI_RUNTIME_DIR"):
        configured = os.environ.get(variable)
        if not configured:
            continue
        runtime = Path(configured).expanduser().resolve()
        if staging == runtime or runtime in staging.parents:
            raise ReleaseBundleError(f"staging directory overlaps live runtime from {variable}")
    return staging


def _application_support_root() -> Path:
    return (Path.home() / "Library" / "Application Support").resolve()


def _write_json_exclusive(path: Path, payload: dict[str, Any]) -> bytes:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    return encoded


def _atomic_completion_marker(staging: Path, payload: dict[str, Any]) -> None:
    temporary = staging / f".{COMPLETION_MARKER}.tmp-{os.getpid()}"
    final = staging / COMPLETION_MARKER
    try:
        _write_json_exclusive(temporary, payload)
        os.replace(temporary, final)
        directory_fd = os.open(staging, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def _seal_release_tree(staging: Path) -> None:
    """Make every completed release member and directory genuinely read-only."""

    for directory, directory_names, file_names in os.walk(staging, topdown=False, followlinks=False):
        base = Path(directory)
        for name in directory_names:
            child = base / name
            if child.is_symlink() or not child.is_dir():
                raise ReleaseBundleError(
                    f"unsafe directory appeared before release sealing: {child.relative_to(staging)}"
                )
        for name in file_names:
            child = base / name
            if child.is_symlink() or not child.is_file():
                raise ReleaseBundleError(
                    f"unsafe file appeared before release sealing: {child.relative_to(staging)}"
                )
            expected_mode = 0o555 if stat.S_IMODE(child.stat().st_mode) & 0o111 else 0o444
            child.chmod(expected_mode)
            if stat.S_IMODE(child.stat().st_mode) != expected_mode:
                raise ReleaseBundleError(
                    f"release file mode could not be sealed: {child.relative_to(staging)}"
                )
        base.chmod(0o555)
        if stat.S_IMODE(base.stat().st_mode) != 0o555:
            raise ReleaseBundleError(
                f"release directory mode could not be sealed: {base.relative_to(staging)}"
            )


def _verify_sealed_release(staging: Path, entries: tuple[SourceEntry, ...]) -> None:
    expected_files = {entry.path: entry for entry in entries}
    expected_files.update({MANIFEST_NAME: None, COMPLETION_MARKER: None})
    actual_files: set[str] = set()
    for directory, directory_names, file_names in os.walk(staging, followlinks=False):
        base = Path(directory)
        if stat.S_IMODE(base.stat().st_mode) != 0o555:
            raise ReleaseBundleError(
                f"completed release directory is writable: {base.relative_to(staging)}"
            )
        for name in directory_names:
            child = base / name
            if child.is_symlink():
                raise ReleaseBundleError(
                    f"completed release contains symlinked directory: {child.relative_to(staging)}"
                )
        for name in file_names:
            child = base / name
            relative = child.relative_to(staging).as_posix()
            if child.is_symlink() or not child.is_file():
                raise ReleaseBundleError(f"completed release contains unsafe file: {relative}")
            actual_files.add(relative)
            expected = expected_files.get(relative)
            expected_mode = expected.mode if expected is not None else 0o444
            if stat.S_IMODE(child.stat().st_mode) != expected_mode:
                raise ReleaseBundleError(f"completed release file mode drifted: {relative}")
    if actual_files != set(expected_files):
        raise ReleaseBundleError("completed release contents changed while sealing")


def _git_output(source_root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(source_root), *arguments],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseBundleError(f"git {' '.join(arguments)} failed: {detail or 'unknown error'}")
    return result.stdout


def _git_identity(source_root: Path) -> tuple[Path, str]:
    top_level_text = _git_output(source_root, "rev-parse", "--show-toplevel").decode().strip()
    top_level = Path(top_level_text).resolve(strict=True)
    if top_level != source_root:
        raise ReleaseBundleError("source_root must be the git repository top-level")
    head = _git_output(source_root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
    if not COMMIT_RE.fullmatch(head):
        raise ReleaseBundleError("git HEAD is not a canonical 40- or 64-character commit digest")
    return top_level, head


def _git_provenance(source_root: Path) -> dict[str, Any]:
    pathspecs = [*SOURCE_DIRECTORIES, *REQUIRED_FILES]
    status = _git_output(
        source_root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        *pathspecs,
    )
    return {
        "head": _git_identity(source_root)[1],
        "dirty": bool(status),
        "status_entry_count": len([row for row in status.split(b"\0") if row]),
        "status_sha256": hashlib.sha256(status).hexdigest(),
    }


def _head_tree(source_root: Path, commit: str) -> dict[str, tuple[str, str]]:
    raw = _git_output(
        source_root,
        "ls-tree",
        "-r",
        "-z",
        commit,
        "--",
        *SOURCE_DIRECTORIES,
        *REQUIRED_FILES,
    )
    entries: dict[str, tuple[str, str]] = {}
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.decode("ascii").split()
        if not separator or len(fields) != 3:
            raise ReleaseBundleError("git tree returned malformed allowlist metadata")
        mode, object_type, object_id = fields
        path = raw_path.decode("utf-8")
        relative = Path(path)
        if _forbidden_secret(relative):
            raise ReleaseBundleError(f"secret-bearing file type is forbidden in git allowlist: {path}")
        if _excluded(relative):
            continue
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ReleaseBundleError(f"git allowlist entry is not a regular file: {path}")
        entries[path] = (mode, object_id)
    return entries


def _head_functional_paths(source_root: Path, commit: str) -> set[str]:
    """Return every tracked executable/configuration source in release scope.

    This inventory intentionally bypasses ``_excluded``.  Exclusion rules are
    permitted to remove mutable data, never tracked Python, shell, or launchd
    functionality without making the release build fail closed.
    """

    raw = _git_output(
        source_root,
        "ls-tree",
        "-r",
        "-z",
        commit,
        "--",
        *SOURCE_DIRECTORIES,
    )
    paths: set[str] = set()
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.decode("ascii").split()
        if not separator or len(fields) != 3:
            raise ReleaseBundleError("git tree returned malformed functional metadata")
        mode, object_type, _object_id = fields
        path = raw_path.decode("utf-8")
        if Path(path).suffix.lower() not in FUNCTIONAL_RELEASE_SUFFIXES:
            continue
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise ReleaseBundleError(f"tracked functional entry is not a regular file: {path}")
        paths.add(path)
    return paths


def _git_diff_is_clean(source_root: Path, *, cached: bool, paths: list[str]) -> bool:
    arguments = ["git", "-C", str(source_root), "diff", "--quiet"]
    if cached:
        arguments.append("--cached")
    arguments.extend(["HEAD", "--", *paths])
    result = subprocess.run(
        arguments,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode not in {0, 1}:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        raise ReleaseBundleError(f"git diff failed: {detail or 'unknown error'}")
    return result.returncode == 0


def _verify_git_snapshot(
    source_root: Path,
    snapshot: tuple[SourceEntry, ...],
    expected_commit: str,
) -> None:
    _top_level, head = _git_identity(source_root)
    if head != expected_commit:
        raise ReleaseBundleError("release commit does not exactly match git HEAD")
    snapshot_by_path = {entry.path: entry for entry in snapshot}
    missing_functional = sorted(_head_functional_paths(source_root, head) - set(snapshot_by_path))
    if missing_functional:
        raise ReleaseBundleError(
            f"release excludes tracked functional source: {missing_functional[0]}"
        )
    tree = _head_tree(source_root, head)
    if set(snapshot_by_path) != set(tree):
        missing = sorted(set(tree) - set(snapshot_by_path))
        extra = sorted(set(snapshot_by_path) - set(tree))
        detail = f"missing={missing[:3]}, untracked_or_ignored={extra[:3]}"
        raise ReleaseBundleError(f"allowlist snapshot does not exactly match tracked HEAD: {detail}")
    paths = sorted(snapshot_by_path)
    if not _git_diff_is_clean(source_root, cached=True, paths=paths):
        raise ReleaseBundleError("allowlist contains staged changes relative to HEAD")
    if not _git_diff_is_clean(source_root, cached=False, paths=paths):
        raise ReleaseBundleError("allowlist contains modified or deleted files relative to HEAD")
    for path, entry in snapshot_by_path.items():
        mode, object_id = tree[path]
        expected_mode = 0o555 if mode == "100755" else 0o444
        if entry.mode != expected_mode:
            raise ReleaseBundleError(f"allowlist file mode differs from HEAD: {path}")
        blob = _git_output(source_root, "cat-file", "blob", object_id)
        if len(blob) != entry.size or hashlib.sha256(blob).hexdigest() != entry.sha256:
            raise ReleaseBundleError(f"allowlist file content differs from HEAD: {path}")


def build_release_bundle(
    source_root: Path,
    staging_dir: Path,
    *,
    release_id: str,
    commit: str | None = None,
    expected_snapshot_sha256: str | None = None,
    now: datetime | None = None,
    require_supply_chain: bool = False,
) -> dict[str, Any]:
    """Copy the V3 allowlist and atomically mark a verified staging bundle complete."""

    if not RELEASE_ID_RE.fullmatch(release_id):
        raise ReleaseBundleError("release_id contains unsupported characters")
    source = source_root.resolve(strict=True)
    try:
        supply_chain = validate_release_supply_chain_binding(source)
    except (OSError, SupplyChainError) as exc:
        if require_supply_chain or (source / "config/v3_supply_chain_binding.json").exists():
            raise ReleaseBundleError(f"release supply-chain binding failed: {exc}") from exc
        supply_chain = {"schema": "magi.supply-chain-binding/v1", "ok": False, "reason": "not_bound"}
    _top_level, head = _git_identity(source)
    resolved_commit = commit or head
    if not COMMIT_RE.fullmatch(resolved_commit):
        raise ReleaseBundleError("commit must be a lowercase 40- or 64-character hex digest")
    if resolved_commit != head:
        raise ReleaseBundleError("release commit does not exactly match git HEAD")
    staging = _assert_safe_staging(source, staging_dir)
    before = snapshot_sources(source)
    _verify_git_snapshot(source, before, resolved_commit)
    privacy_audit = _release_privacy_audit(source, before)
    source_snapshot_sha256 = _snapshot_digest(before)
    if expected_snapshot_sha256 is not None:
        if not SHA256_RE.fullmatch(expected_snapshot_sha256):
            raise ReleaseBundleError("expected_snapshot_sha256 must be a lowercase SHA-256 digest")
        if source_snapshot_sha256 != expected_snapshot_sha256:
            raise ReleaseBundleError("source snapshot does not match expected_snapshot_sha256")
    git_provenance = _git_provenance(source)
    staging.mkdir(mode=0o755)
    for entry in before:
        _copy_entry(source, staging, entry)
    after_copy = snapshot_sources(source)
    if after_copy != before:
        raise ReleaseBundleError("source snapshot changed while building release bundle")
    _verify_git_snapshot(source, after_copy, resolved_commit)
    copied_files = [entry.manifest_entry() for entry in before]
    for entry in before:
        destination = staging / entry.path
        if destination.is_symlink() or not destination.is_file():
            raise ReleaseBundleError(f"staged file is missing or unsafe: {entry.path}")
        if destination.stat().st_size != entry.size or _sha256_file(destination) != entry.sha256:
            raise ReleaseBundleError(f"staged file verification failed: {entry.path}")
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    manifest = {
        "schema_version": 1,
        "release_id": release_id,
        "commit": resolved_commit,
        "generated_at": generated_at,
        "immutable": True,
        "source_snapshot_sha256": source_snapshot_sha256,
        "release_sha256": source_snapshot_sha256,
        "git_provenance": git_provenance,
        "source_file_count": len(copied_files),
        "source_allowlist": [*SOURCE_DIRECTORIES, *REQUIRED_FILES],
        "required_package_files": list(REQUIRED_PACKAGE_FILES),
        "required_test_targets": list(REQUIRED_TEST_TARGETS),
        "test_execution_evidence": "not_evaluated_by_bundle_builder",
        "privacy_audit": privacy_audit,
        "supply_chain_evidence": supply_chain,
        "excluded_components": sorted(EXCLUDED_COMPONENTS),
        "excluded_mutable_files": sorted(EXCLUDED_MUTABLE_FILES),
        "external_template_contract": {
            "hearing_leave_template_env": "MAGI_HEARING_LEAVE_TEMPLATE_PATH",
            "bundled_local_template": False,
        },
        "files": copied_files,
    }
    manifest_bytes = _write_json_exclusive(staging / MANIFEST_NAME, manifest)
    final_snapshot = snapshot_sources(source)
    if final_snapshot != before:
        raise ReleaseBundleError("source snapshot changed before release completion")
    _verify_git_snapshot(source, final_snapshot, resolved_commit)
    marker = {
        "schema_version": 1,
        "release_id": release_id,
        "commit": resolved_commit,
        "completed_at": generated_at,
        "manifest": MANIFEST_NAME,
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "source_snapshot_sha256": source_snapshot_sha256,
        "release_sha256": source_snapshot_sha256,
        "source_file_count": len(copied_files),
        "privacy_audit_sha256": hashlib.sha256(
            json.dumps(privacy_audit, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }
    _atomic_completion_marker(staging, marker)
    _seal_release_tree(staging)
    _verify_sealed_release(staging, before)
    return marker


def main() -> int:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=repository)
    parser.add_argument("--staging-dir", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--commit")
    parser.add_argument("--expected-snapshot-sha256")
    args = parser.parse_args()
    try:
        marker = build_release_bundle(
            args.source_root,
            args.staging_dir,
            release_id=args.release_id,
            commit=args.commit,
            expected_snapshot_sha256=args.expected_snapshot_sha256,
            require_supply_chain=True,
        )
    except (OSError, ReleaseBundleError) as exc:
        parser.exit(2, f"release bundle failed: {exc}\n")
    print(json.dumps(marker, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

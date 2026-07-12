from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_go_live_nightly_does_not_update_db_on_generic_portal_draft_failed():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_nightly_audit.py")
    assert 'elif err == "portal_draft_failed" and db and case.get("id")' not in src
    assert "不自動更新 DB" in src
    assert "MAGI_LAF_AUTO_GO_LIVE_PREFILL" in src
    assert "go_live_has_no_draft" in src


def test_laf_closing_nightly_auto_draft_is_opt_in_only():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_nightly_audit.py")
    assert "MAGI_LAF_AUTO_CLOSING_DRAFT" in src
    assert "auto_closing_draft_disabled" in src
    assert "報結自動暫存預設關閉" in src


def test_go_live_never_uses_draft_failure_wording():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py")
    assert "開辦預填失敗" in src
    assert "❌ {wf} 暫存失敗" in src
    assert "portal {wf} draft save failed" not in src


def test_laf_duplicate_check_uses_all_laf_number_columns():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py")
    block = src.split("def _check_duplicate", 1)[1].split("def _update_legal_aid_number", 1)[0]
    assert "`legal_aid_number`" in block
    assert "`laf_case_no`" in block
    assert "`application_no`" in block
    assert "Duplicate matched by=laf_number_columns" in block


def test_condition_batch_does_not_auto_mark_manual_done_after_failures():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py")
    batch = src.split("def run_condition_drafts", 1)[1].split("def _was_closing_drafted_recently", 1)[0]
    assert "condition_manual_done" not in batch
    assert "portal condition draft save failed >= 2 times" not in batch
    assert "suppress_notify=suppress_notify" in batch


def test_existing_portal_draft_is_reported_as_noop_status():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_automation_v2.py")
    assert '"status": "already_in_progress"' in src
    orch = _read("casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py")
    assert 'result["portal_status"] = upload_status' in orch
    assert 'result["noop"] = True' in orch


def test_autopilot_zero_max_cases_remains_unlimited():
    src = _read("skills/magi-autopilot/action.py")
    assert "run_condition_drafts(max_cases=int(max_cases), suppress_notify=True)" in src
    assert "run_condition_drafts(max_cases=int(max_cases or 2))" not in src


def test_production_laf_nightly_scans_case_status_drafts():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_nightly_audit.py")
    assert '"case_status_drafts": []' in src
    assert 'portal.get("case_status", [])' in src
    assert "portal_pending_case_status_drafts" in src


def test_closing_batch_uses_permanent_dedup_after_draft():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py")
    block = src.split("def _was_closing_drafted_recently", 1)[1].split("def _get_pending_closing_draft_cases", 1)[0]
    assert "permanent dedup signals" in block
    assert "DATE_SUB(NOW()" not in block


def test_auto_closing_candidates_do_not_include_in_progress_statuses():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py")
    block = src.split("def _get_pending_closing_draft_cases", 1)[1].split("def run_closing_drafts", 1)[0]
    assert "'進行中', '已開辦'" not in block
    assert "'待報結', '已結案，待報結'" in block


def test_auto_closing_status_write_has_current_status_guard():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py")
    block = src.split("if fields.get(\"_auto_closing_draft\")", 1)[1].split("try:", 1)[0]
    assert "SELECT legal_aid_status FROM cases" in block
    assert '"待報結", "已結案，待報結"' in block
    assert "Auto closing draft skipped DB status write" in block


def test_condition_batch_uses_permanent_dedup_after_draft():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py")
    block = src.split("def _was_condition_drafted_recently", 1)[1].split("def _get_pending_condition_cases", 1)[0]
    assert "永久 dedup" in block
    assert "DATE_SUB(NOW()" not in block


def test_closing_failure_returns_structured_portal_error():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py")
    block = src.split("# closing", 1)[1].split("def execute_portal_action_submit", 1)[0]
    assert 'result["error"] = "portal_draft_failed"' in block
    assert 'result["detail"] = str(getattr(self, "_last_portal_error"' in block
    assert '"closing_portal_save_failed"' in block


def test_closing_automation_records_failure_diagnostics():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_automation_v2.py")
    assert "self.last_portal_error = \"\"" in src
    assert "def _set_portal_error" in src
    assert "closing_page1_ajax_save_failed" in src
    assert "closing_save_unclear" in src
    assert "responseText" in src


def test_portal_workflows_use_laf_automation_v2_not_legacy_downloader():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py")
    block = src.split("def _get_automation", 1)[1].split("def close", 1)[0]
    assert "laf_automation_v2 import LAFWebAutomation" in block
    assert "skills.legal.laf import LAFWebAutomation" not in block


def test_legacy_laf_import_path_points_to_unified_v2_class():
    from skills.legal.laf import LAFWebAutomation

    assert LAFWebAutomation.__module__ == "casper_ecosystem.law_firm_orchestrators.laf_automation_v2"
    assert hasattr(LAFWebAutomation, "save_closing_report_draft")
    assert hasattr(LAFWebAutomation, "download_case_files")


def test_casper_laf_handler_is_compat_wrapper_for_api_rules():
    from api.handlers import laf_handler as api_handler
    from casper_ecosystem.law_firm_orchestrators import laf_handler as compat_handler

    assert compat_handler.parse_laf_report_payload is api_handler.parse_laf_report_payload
    assert compat_handler._STATUS_MAP is api_handler._STATUS_MAP
    assert compat_handler._STATUS_MAP["報結"] == "已結案"


def test_laf_dispatch_never_reports_bare_unknown_for_portal_failures():
    from api.pipelines.command_dispatch import _laf_failure_code_and_detail

    err, detail = _laf_failure_code_and_detail(
        {"ok": False, "action": "closing", "preview": {"png": "/tmp/closing.png"}},
        action="closing",
    )
    assert err == "portal_draft_failed"
    assert "closing.png" in detail

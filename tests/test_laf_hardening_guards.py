from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_go_live_nightly_does_not_update_db_on_generic_portal_draft_failed():
    for rel in [
        "scripts/laf_nightly_audit.py",
        "casper_ecosystem/law_firm_orchestrators/laf_nightly_audit.py",
    ]:
        src = _read(rel)
        assert 'elif err == "portal_draft_failed" and db and case.get("id")' not in src
        assert "不自動更新 DB" in src


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
    src = _read("scripts/laf_nightly_audit.py")
    assert '"case_status_drafts": []' in src
    assert 'portal.get("case_status", [])' in src
    assert "portal_pending_case_status_drafts" in src


def test_closing_batch_uses_permanent_dedup_after_draft():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py")
    block = src.split("def _was_closing_drafted_recently", 1)[1].split("def _get_pending_closing_draft_cases", 1)[0]
    assert "permanent dedup signals" in block
    assert "DATE_SUB(NOW()" not in block


def test_condition_batch_uses_permanent_dedup_after_draft():
    src = _read("casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py")
    block = src.split("def _was_condition_drafted_recently", 1)[1].split("def _get_pending_condition_cases", 1)[0]
    assert "永久 dedup" in block
    assert "DATE_SUB(NOW()" not in block

def test_single_source_audit_passes():
    from scripts.ops.audit_single_source import audit

    result = audit()

    assert result["ok"], result["failures"]


def test_laf_canonical_exports_required_legacy_surface():
    from skills.legal.laf import LAFAutomationManager, LAFCaseTypeParser, LAFGmailMonitor, OSCCaseCreator

    assert LAFGmailMonitor
    assert LAFAutomationManager
    assert LAFCaseTypeParser
    assert hasattr(OSCCaseCreator, "dedupe_case_folders_by_laf_marker")


def test_legacy_legalbridge_import_has_no_global_delete_side_effect(tmp_path):
    import os
    import pathlib
    import shutil

    orig_remove = os.remove
    orig_unlink = os.unlink
    orig_path_unlink = pathlib.Path.unlink
    orig_rmtree = shutil.rmtree

    import pytest

    pytest.importorskip("casper_ecosystem.law_firm_orchestrators.legalbridge_core")

    assert os.remove is orig_remove
    assert os.unlink is orig_unlink
    assert pathlib.Path.unlink is orig_path_unlink
    assert shutil.rmtree is orig_rmtree

    protected_like = tmp_path / "SynologyDrive-homes" / "01_案件" / "一般案件" / "民事" / "2026-0099-測試"
    protected_like.mkdir(parents=True)
    marker = protected_like / ".gitkeep"
    marker.write_text("keep", encoding="utf-8")
    marker.unlink()
    shutil.rmtree(protected_like)
    assert not protected_like.exists()

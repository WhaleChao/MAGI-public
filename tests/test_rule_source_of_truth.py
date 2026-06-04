from __future__ import annotations

from pathlib import Path
import importlib.util


ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_laf_nightly_script_is_thin_wrapper():
    import scripts.laf_nightly_audit as script_mod
    import casper_ecosystem.law_firm_orchestrators.laf_nightly_audit as canonical

    assert script_mod.run_audit is canonical.run_audit
    assert script_mod.scan_laf_reporting_status is canonical.scan_laf_reporting_status

    src = _read("scripts/laf_nightly_audit.py")
    assert "source of truth lives" in src.lower()
    assert "01_法扶資料" not in src
    assert "02_開辦資料" not in src


def test_laf_flow_legacy_import_is_thin_wrapper():
    import api.domains.laf_flow as canonical
    import casper_ecosystem.law_firm_orchestrators.laf_flow as legacy

    assert legacy.handle_laf_submit_confirmation_if_any is canonical.handle_laf_submit_confirmation_if_any
    assert legacy.register_laf_progress_submit_pending is canonical.register_laf_progress_submit_pending
    assert legacy.update_laf_status_after_action is canonical.update_laf_status_after_action

    src = _read("casper_ecosystem/law_firm_orchestrators/laf_flow.py")
    assert "source of truth lives" in src.lower()
    assert "_parse_subprocess_result" not in src


def test_laf_open_case_vision_skill_is_thin_wrapper():
    import casper_ecosystem.law_firm_orchestrators.open_case_vision as canonical

    wrapper_path = ROOT / "skills" / "laf-portal-automation" / "open_case_vision.py"
    spec = importlib.util.spec_from_file_location("laf_portal_open_case_vision_wrapper", wrapper_path)
    assert spec and spec.loader
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)

    assert wrapper.extract_open_case_date is canonical.extract_open_case_date
    assert wrapper.build_go_live_remark is canonical.build_go_live_remark

    src = _read("skills/laf-portal-automation/open_case_vision.py")
    assert "source of truth lives" in src.lower()
    assert "def extract_open_case_date" not in src


def test_laf_simulated_line_skill_is_thin_wrapper():
    import casper_ecosystem.law_firm_orchestrators.simulated_line as canonical

    wrapper_path = ROOT / "skills" / "laf-portal-automation" / "simulated_line.py"
    spec = importlib.util.spec_from_file_location("laf_portal_simulated_line_wrapper", wrapper_path)
    assert spec and spec.loader
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)

    assert wrapper.send_line_notify is canonical.send_line_notify

    src = _read("skills/laf-portal-automation/simulated_line.py")
    assert "source of truth lives" in src.lower()
    assert "def send_line_notify" not in src


def test_laf_deep_extract_script_is_thin_wrapper():
    import scripts.ops.laf_deep_extract_backfill as script_mod
    import casper_ecosystem.law_firm_orchestrators.laf_deep_extract_backfill as canonical

    assert script_mod.run is canonical.run
    assert script_mod.main is canonical.main

    src = _read("scripts/ops/laf_deep_extract_backfill.py")
    assert "source of truth lives" in src.lower()
    assert "def _fetch_candidates" not in src


def test_skill_listing_is_single_source_and_openclaw_opt_in(monkeypatch, tmp_path):
    from api.pipelines import skill_listing

    monkeypatch.delenv("MAGI_INCLUDE_RETIRED_OPENCLAW_SKILLS", raising=False)
    roots = skill_listing.iter_skill_roots(str(tmp_path))
    assert roots == [(str(tmp_path / "skills"), "magi")]

    monkeypatch.setenv("MAGI_INCLUDE_RETIRED_OPENCLAW_SKILLS", "1")
    roots = skill_listing.iter_skill_roots(str(tmp_path))
    assert roots[0] == (str(tmp_path / "skills"), "magi")
    assert roots[1][1] == "openclaw-retired"


def test_active_openclaw_defaults_are_retired():
    src = _read("api/pipelines/message_router.py")
    assert "magi-office-ops" not in src
    assert ".openclaw\", \"skills\"" not in src

    from api.pipelines.message_router import read_openclaw_primary_model

    assert "已退役" in read_openclaw_primary_model()


def test_autopilot_comm_health_uses_magi_official_channels_not_openclaw_cli():
    src = _read("skills/magi-autopilot/action.py")
    assert '"openclaw", "channels", "status", "--probe"' not in src
    assert "MAGI_TICK_OPENCLAW_SESSION_SELFHEAL_ENABLE\", False" in src
    assert "official_channel_smoke" in src


def test_file_manager_deep_verify_does_not_default_to_case_nas():
    src = _read("scripts/ops/paperclip_filemanager_deep_verify.py")
    assert "/tmp/paperclip_filemanager_test_base" in src
    assert "PAPERCLIP_FILEMANAGER_TEST_BASE" in src
    assert "/Users/ai/SynologyDrive/homes/01_案件" not in src


def test_file_manager_test_sandbox_is_allowed_but_not_all_tmp(tmp_path):
    from api.osc import utils

    sandbox = Path("/tmp/paperclip_filemanager_test_base")
    sandbox.mkdir(parents=True, exist_ok=True)
    sample = sandbox / "allowed.txt"
    sample.write_text("ok", encoding="utf-8")

    outside = tmp_path / "not-under-file-manager-root.txt"
    outside.write_text("no", encoding="utf-8")

    assert utils._osc_is_safe_local_path(str(sample)) is True
    assert utils._osc_is_safe_local_path(str(outside)) is False

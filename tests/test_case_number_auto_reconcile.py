from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / "skills" / "osc-orchestrator" / "action.py"
SPEC = importlib.util.spec_from_file_location("osc_case_number_reconcile", ACTION)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_compact_portal_case_numbers_are_normalized() -> None:
    samples = {
        "士林刑事 114附民1289_卷1_P1_102.pdf": "114年度附民字第001289號",
        "114東原簡18 卷二.pdf": "114年度東原簡字第000018號",
        "114_偵_005963_閱卷.pdf": "114年度偵字第005963號",
    }
    for filename, expected in samples.items():
        result = MODULE._extract_court_hint_and_case_no_from_filename(filename)
        assert result["court_case_number"] == expected


def test_filename_date_is_not_misread_as_case_number() -> None:
    result = MODULE._extract_court_hint_and_case_no_from_filename("判決_吳某_115年7月27日.pdf")
    assert result["court_case_number"] == ""


def test_review_folder_substantive_number_beats_old_investigation_notice(tmp_path: Path) -> None:
    case = tmp_path / "2025-0079-吳某-一審-侵權行為"
    notices = case / "09_法院通知或程序裁定"
    review = case / "06_閱卷資料" / "20260804"
    notices.mkdir(parents=True)
    review.mkdir(parents=True)
    (notices / "20250122 花蓮地檢署114年度偵字第172號通知.pdf").write_bytes(b"x")
    (review / "士林刑事 114附民1289_卷1.pdf").write_bytes(b"x")

    result = MODULE._discover_case_court_info(str(case), case_type="民事")

    assert result["court_case_number"] == "114年度附民字第001289號"
    assert "閱卷資料" in result["source_path"]


def test_update_decision_auto_upgrades_but_never_downgrades() -> None:
    upgraded = MODULE._court_case_number_update_decision(
        current="114年度偵字第000172號",
        candidate="114年度附民字第001289號",
        source_priority=95,
    )
    downgraded = MODULE._court_case_number_update_decision(
        current="115年度上訴字第000014號",
        candidate="114年度偵字第007543號",
        source_priority=100,
    )
    assert upgraded == {"action": "update", "reason": "trusted_stage_upgrade"}
    assert downgraded == {"action": "keep", "reason": "prevent_older_year_replacement"}


def test_same_stage_or_cross_case_conflict_requires_confirmation() -> None:
    same_stage = MODULE._court_case_number_update_decision(
        current="115年度訴字第000123號",
        candidate="115年度訴字第000456號",
        source_priority=100,
    )
    cross_case = MODULE._court_case_number_update_decision(
        current="",
        candidate="115年度訴字第000456號",
        source_priority=100,
        owned_by_other_case=True,
    )
    assert same_stage["action"] == "confirm"
    assert cross_case == {"action": "confirm", "reason": "candidate_owned_by_other_case"}


def test_padding_does_not_create_false_conflict() -> None:
    decision = MODULE._court_case_number_update_decision(
        current="114年度附民字第1289號",
        candidate="114年度附民字第001289號",
        source_priority=95,
    )
    assert decision == {"action": "keep", "reason": "already_current"}


def test_same_current_number_is_not_escalated_for_duplicate_owner() -> None:
    decision = MODULE._court_case_number_update_decision(
        current="115年度訴字第000551號",
        candidate="115年度訴字第551號",
        source_priority=100,
        owned_by_other_case=True,
        system_case_number="2026-0048",
    )
    assert decision == {"action": "keep", "reason": "already_current"}


def test_remanded_appeal_is_never_replaced_by_older_attached_civil_number() -> None:
    decision = MODULE._court_case_number_update_decision(
        current="114年度重上更二字第000095號",
        candidate="80年度附民字第000398號",
        source_priority=100,
        system_case_number="2025-0077",
    )
    assert MODULE._court_case_number_quality("114年度重上更二字第000095號") > MODULE._court_case_number_quality(
        "114年度附民字第000398號"
    )
    assert decision == {"action": "keep", "reason": "prevent_older_year_replacement"}


def test_historical_initial_candidate_requires_confirmation() -> None:
    decision = MODULE._court_case_number_update_decision(
        current="",
        candidate="80年度附民字第000398號",
        source_priority=100,
        system_case_number="2025-0077",
    )
    assert decision == {"action": "confirm", "reason": "historical_initial_candidate"}


def test_prefixed_appellate_word_is_a_trusted_stage_upgrade() -> None:
    decision = MODULE._court_case_number_update_decision(
        current="114年度花原簡字第000110號",
        candidate="114年度交上易字第000014號",
        source_priority=95,
        system_case_number="2026-0047",
    )
    assert MODULE._court_case_number_quality("114年度交上易字第000014號") == 104
    assert decision == {"action": "update", "reason": "trusted_stage_upgrade"}

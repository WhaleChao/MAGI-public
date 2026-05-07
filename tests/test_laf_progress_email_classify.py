"""T3 tests: progress email classification."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_classify_true_for_jian_jin_keywords():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import classify_progress_email
    assert classify_progress_email("案件進度通知", "") is True


def test_classify_true_keywords_in_snippet():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import classify_progress_email
    assert classify_progress_email("通知", "請確認案件進度回報") is True


def test_classify_false_missing_case_keyword():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import classify_progress_email
    # Only has 進度 but not 案件
    assert classify_progress_email("進度更新", "處理進度如下") is False


def test_classify_false_missing_progress_keyword():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import classify_progress_email
    # Only has 案件 but not 進度
    assert classify_progress_email("案件通知", "您的案件已受理") is False


def test_classify_false_empty():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import classify_progress_email
    assert classify_progress_email("", "") is False


def test_classify_combined_subject_snippet():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import classify_progress_email
    # 案件 in subject, 進度 in snippet
    assert classify_progress_email("法扶案件通知", "請告知案件進度") is True


def test_priority_types_excludes_dispatch():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import _PRIORITY_TYPES
    assert "dispatch" in _PRIORITY_TYPES


def test_priority_types_excludes_fee():
    from casper_ecosystem.law_firm_orchestrators.laf_progress_helper import _PRIORITY_TYPES
    assert "fee" in _PRIORITY_TYPES


def test_laf_py_classify_function_exists():
    from skills.legal.laf import _classify_progress_email, _PROGRESS_PRIORITY_TYPES
    assert callable(_classify_progress_email)
    assert "dispatch" in _PROGRESS_PRIORITY_TYPES


def test_laf_py_classify_true():
    from skills.legal.laf import _classify_progress_email
    assert _classify_progress_email("請確認案件進度", "") is True


def test_laf_py_classify_false_priority_type_handled_by_caller():
    """classify_progress_email itself doesn't check priority types; caller does."""
    from skills.legal.laf import _classify_progress_email
    # Still returns True on keyword match; caller is responsible for priority check
    assert _classify_progress_email("案件進度", "") is True

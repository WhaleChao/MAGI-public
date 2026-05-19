"""T2 tests: _resolve_case_category root fix."""
import pytest
import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _make_builder():
    from casper_ecosystem.law_firm_orchestrators.laf_folder_builder import LAFFolderBuilder
    return LAFFolderBuilder.__new__(LAFFolderBuilder)


@pytest.mark.parametrize("case_type,case_stage,case_reason,expected", [
    # 明確 case_type 優先
    ("民事", "", "", "民事"),
    ("刑事", "", "", "刑事"),
    ("家事", "", "", "家事"),
    ("行政", "", "", "行政"),
    ("消費者債務清理", "", "", "消費者債務清理"),
    # 原 bug 情境：民事 + 「上訴」不應跑到刑事
    ("民事", "二審", "上訴", "民事"),
    # 原 bug 情境：民事 + 「強制執行」不應跑到刑事
    ("民事", "", "強制執行", "民事"),
    # 刑事附帶民事是民事求償／移送民事庭，不能因「刑事」兩字建到刑事根目錄
    ("民事", "一審", "刑事附帶民事", "民事"),
    ("", "一審", "刑事附帶民事", "民事"),
    ("", "", "附民損害賠償", "民事"),
    # 家事推斷（無明確 case_type）
    ("", "", "離婚", "家事"),
    ("", "", "監護", "家事"),
    # 刑事獨有詞推斷
    ("", "偵查", "毒品", "刑事"),
    ("", "", "強盜", "刑事"),
    # 消費者債務清理推斷
    ("", "", "更生", "消費者債務清理"),
    ("民事", "", "消費者債務清理（更生）", "消費者債務清理"),
    # 法扶/社會保險公法爭議：即使 portal 誤標民事，也要放行政根目錄
    ("民事", "一審", "勞工保險爭議", "行政"),
    ("", "一審", "勞保給付爭議", "行政"),
    # 無關鍵字 fallback 民事
    ("", "", "損害賠償", "民事"),
    ("", "", "借貸", "民事"),
])
def test_resolve_case_category(case_type, case_stage, case_reason, expected):
    builder = _make_builder()
    case_info = {"case_type": case_type, "case_stage": case_stage, "case_reason": case_reason}
    assert builder._resolve_case_category(case_info) == expected


def test_civil_with_appeal_keyword_stays_civil():
    builder = _make_builder()
    # 「二審上訴」是民事流程；不含刑事獨有關鍵字
    result = builder._resolve_case_category({
        "case_type": "", "case_stage": "二審", "case_reason": "上訴"
    })
    assert result == "民事"


def test_execution_does_not_force_criminal():
    builder = _make_builder()
    result = builder._resolve_case_category({
        "case_type": "", "case_stage": "", "case_reason": "強制執行"
    })
    assert result == "民事"

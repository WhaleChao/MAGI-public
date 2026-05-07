from casper_ecosystem.law_firm_orchestrators.file_review_automation import (
    _file_review_case_signature_present,
    _file_review_submit_evidence_is_success,
    _file_review_submit_success_from_text,
    _infer_file_review_sys_type,
    _ordered_file_review_sys_candidates,
)


def test_infer_family_case_type_for_file_review():
    assert _infer_file_review_sys_type("HLD", "婚") == "U"
    assert _infer_file_review_sys_type("HLD", "家非") == "U"


def test_infer_criminal_and_admin_case_types_for_file_review():
    assert _infer_file_review_sys_type("HLD", "原訴") == "H"
    assert _infer_file_review_sys_type("TPBA", "訴") == "A"


def test_ordered_sys_candidates_prefers_inferred_then_real_options():
    assert _ordered_file_review_sys_candidates(["H", "V", "U", "I", "A", "K"], "U")[:3] == ["U", "H", "V"]
    assert "V" in _ordered_file_review_sys_candidates(["H", "V"], "")


def test_submit_success_requires_acceptance_signal():
    case_info = {"year": "115", "case_type": "婚", "case_number": "19"}

    assert _file_review_submit_success_from_text(
        "您的閱卷聲請：臺灣花蓮地方法院 家事 115年度婚字第000019號 已受理，請靜待法院回覆結果",
        case_info,
    )
    assert _file_review_submit_success_from_text(
        "已將下列資訊提交至法院 聲請人喬政翔 對象法院臺灣花蓮地方法院 案號家事115.婚.000019 當事人[當事人J]",
        case_info,
    )
    assert not _file_review_submit_success_from_text(
        "閱卷聲請登錄清單 對法院 臺灣花蓮地方法院 案號 家事 115 年度 婚 字第 19 號 確認送出",
        case_info,
    )


def test_submit_evidence_accepts_exact_list_match_but_not_generic_rows():
    case_info = {"year": "115", "case_type": "婚", "case_number": "19"}

    assert _file_review_case_signature_present("HLD 115年度婚字第000019號 [當事人J]", case_info)
    assert _file_review_submit_evidence_is_success({"list_case_verified": True}, "", case_info)
    assert not _file_review_submit_evidence_is_success({"list_row_count": 3}, "查詢結果 查無本案", case_info)

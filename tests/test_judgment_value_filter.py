from __future__ import annotations

from api.domains.judgment_value_filter import KEEP, REVIEW, SKIP_SUMMARY, classify_judgment_record


def test_attached_civil_transfer_order_is_skipped():
    decision = classify_judgment_record(
        jid="SLDM,115,附民,393,20260330,1",
        court_name="SLDM",
        case_number="115年度附民字第393號",
        case_reason="損害賠償",
        full_text=(
            "臺灣士林地方法院刑事附帶民事訴訟裁定。"
            "上列被告因詐欺案件，經原告提起附帶民事訴訟，"
            "因事件繁雜，非經長久時日不能終結其審判，"
            "爰依刑事訴訟法第504條第1項前段，將本件附帶民事訴訟移送本院民事庭。"
        ),
    )
    assert decision.disposition == SKIP_SUMMARY
    assert decision.category == "attached_civil_transfer"


def test_ruling_is_not_skipped_merely_because_it_is_a_ruling():
    decision = classify_judgment_record(
        jid="TPDV,115,全,20,20260401,1",
        court_name="TPDV",
        case_number="115年度全字第20號",
        case_reason="假扣押",
        full_text=(
            "臺灣臺北地方法院民事裁定。"
            "本件爭點為請求原因是否已釋明，及保全必要性是否存在。"
            "本院認為債權人就債權存在及日後不能強制執行之虞已為相當釋明。"
        ),
    )
    assert decision.disposition in {KEEP, REVIEW}
    assert decision.disposition != SKIP_SUMMARY


def test_payment_order_is_skipped():
    decision = classify_judgment_record(
        jid="TNDV,115,司促,3215,20260226,1",
        court_name="TNDV",
        case_number="115年度司促字第3215號",
        case_reason="支付命令",
        full_text="臺灣臺南地方法院支付命令。債務人應向債權人清償新臺幣壹萬捌仟元。",
    )
    assert decision.disposition == SKIP_SUMMARY
    assert decision.category == "payment_order"


def test_payment_order_mentioned_in_procedural_history_is_not_skipped():
    decision = classify_judgment_record(
        jid="TPEV,115,北簡,111,20260305,1",
        court_name="TPEV",
        case_number="115年度北簡字第111號",
        case_reason="給付簽帳卡消費款",
        full_text=(
            "臺灣臺北地方法院民事簡易判決。"
            "上列當事人間請求給付簽帳卡消費款事件，本院判決如下。"
            "原告曾聲請支付命令，惟被告於法定期間內提出異議，應以支付命令之聲請視為起訴。"
            "本院認為依信用卡契約及消費借貸關係，被告應負清償責任。"
        ),
    )
    assert decision.disposition != SKIP_SUMMARY


def test_upper_court_procedural_ruling_goes_to_review_not_skip():
    decision = classify_judgment_record(
        jid="TPHM,115,抗,99,20260401,1",
        court_name="臺灣高等法院",
        case_number="115年度抗字第99號",
        case_reason="移送管轄",
        full_text="臺灣高等法院民事裁定。本件移送管轄。",
    )
    assert decision.disposition == REVIEW


def test_fee_order_is_skipped():
    decision = classify_judgment_record(
        jid="NTDV,115,家補,52,20260330,1",
        court_name="NTDV",
        case_number="115年度家補字第52號",
        case_reason="補繳裁判費",
        full_text="臺灣南投地方法院民事裁定。未據繳納聲請費，命補正，逾期不繳即駁回。",
    )
    assert decision.disposition == SKIP_SUMMARY
    assert decision.category == "fee_order"


def test_administrative_court_ruling_is_review_not_skip():
    decision = classify_judgment_record(
        jid="TPTA,115,交,37,20260415,2",
        court_name="TPTA",
        case_number="115年度交字第37號",
        case_reason="交通裁決",
        full_text="臺北高等行政法院裁定。本件停止訴訟程序。",
    )
    assert decision.disposition == REVIEW


def test_high_court_branch_code_is_review_not_skip():
    decision = classify_judgment_record(
        jid="KSHV,115,金訴,91,20260306,1",
        court_name="KSHV",
        case_number="115年度金訴字第91號",
        case_reason="損害賠償",
        full_text="臺灣高等法院高雄分院民事裁定。提起刑事附帶民事訴訟，經刑事庭裁定移送前來。",
    )
    assert decision.disposition != SKIP_SUMMARY


def test_civil_judgment_after_attached_civil_transfer_is_not_skipped():
    decision = classify_judgment_record(
        jid="STEV,114,店簡,1207,20260316,1",
        court_name="STEV",
        case_number="114年度店簡字第1207號",
        case_reason="損害賠償",
        full_text=(
            "臺灣臺北地方法院民事簡易判決。"
            "上列當事人間請求損害賠償事件，經刑事庭移送審理，本院判決如下。"
            "被告應給付原告三萬元。"
        ),
    )
    assert decision.disposition != SKIP_SUMMARY


def test_substantive_civil_judgment_after_attached_civil_transfer_is_not_skipped():
    decision = classify_judgment_record(
        jid="TNEV,115,南簡,172,20260415,1",
        court_name="TNEV",
        case_number="115年度南簡字第172號",
        case_reason="損害賠償",
        full_text=(
            "臺灣臺南地方法院臺南簡易庭民事判決。"
            "原告提起刑事附帶民事訴訟請求侵權行為損害賠償，經刑事庭裁定移送前來。"
            "本院判決如下，被告應給付原告新臺幣伍萬元。"
            "本院認為被告散布個人資料，構成侵權行為。"
        ),
    )
    assert decision.disposition != SKIP_SUMMARY


def test_jid_encoded_fee_order_is_skipped_even_when_case_number_missing():
    decision = classify_judgment_record(
        jid="PCDV,115,審補,310,20260311,1",
        court_name="PCDV",
        case_number="",
        case_reason="一般",
        full_text="臺灣新北地方法院民事裁定。原告起訴未據繳納裁判費，限期補繳，逾期不繳即駁回其訴。",
    )
    assert decision.disposition == SKIP_SUMMARY
    assert decision.category == "fee_order"


def test_court_branch_fee_order_is_skipped_even_when_case_number_missing():
    decision = classify_judgment_record(
        jid="PCEV,115,板補,743,20260320,1",
        court_name="PCEV",
        case_number="",
        case_reason="一般",
        full_text="臺灣新北地方法院板橋簡易庭民事裁定。請求侵權行為損害賠償事件，原告應補繳裁判費，逾期未補正即駁回。",
    )
    assert decision.disposition == SKIP_SUMMARY
    assert decision.category == "fee_order"


def test_execution_jurisdiction_transfer_is_skipped_despite_case_reason_terms():
    decision = classify_judgment_record(
        jid="TPDV,114,司執,237864,20251222,1",
        court_name="TPDV",
        case_number="",
        case_reason="一般",
        full_text=(
            "臺灣臺北地方法院民事裁定。"
            "上列當事人間侵權行為損害賠償強制執行事件，本院裁定如下。"
            "本件移送臺灣彰化地方法院。理由為本院無管轄權。"
        ),
    )
    assert decision.disposition == SKIP_SUMMARY
    assert decision.category == "execution"


def test_consumer_debt_execution_ruling_is_review_not_skip():
    decision = classify_judgment_record(
        jid="TYDV,114,司執消債更,134,20260316,1",
        court_name="TYDV",
        case_number="114年度司執消債更字第134號",
        case_reason="消費者債務清理",
        full_text="臺灣桃園地方法院民事裁定。債務人執行更生事件，認有保全處分必要，本院裁定如下。",
    )
    assert decision.disposition == REVIEW

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional


KEEP = "KEEP"
REVIEW = "REVIEW"
SKIP_SUMMARY = "SKIP_SUMMARY"


@dataclass
class JudgmentValueDecision:
    disposition: str
    reason: str
    confidence: float
    category: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_UPPER_COURT_PREFIXES = (
    "TPS",
    "TPH",
    "TPHA",
    "TPHM",
    "TPHV",
    "TPBA",
    "TCDA",
    "KSHA",
    "KSHM",
    "KSHV",
    "TNHM",
    "TNHV",
    "TCHM",
    "TCHV",
    "HLHM",
    "HLHV",
    "TPTA",
    "TCTA",
    "KSTA",
)

_SUBSTANTIVE_SIGNALS = re.compile(
    r"爭點|本件爭執|本院認為|本院認|法院認為|應解為|法律上原因|構成要件|"
    r"權利義務|比例原則|信賴保護|裁量|法律見解|實務見解|法理|釋字|憲法|"
    r"不當得利|侵權行為|債務不履行|過失責任|因果關係|舉證責任"
)

_PURE_PROCEDURAL_SIGNALS = re.compile(
    r"補繳裁判費|未據繳納裁判費|逾期不繳|逾期未補正|命補正|"
    r"移送本院民事庭|本件移送於本院民事庭|移送該法院之民事庭|"
    r"支付命令|本票准許強制執行|准予強制執行|強制執行聲請駁回|"
    r"拍賣抵押物|拍賣質物|停止訴訟程序|移送管轄|上訴不合法|抗告不合法"
)


def _s(value: Any) -> str:
    return str(value or "").strip()


def _jid_prefix(jid: str) -> str:
    return _s(jid).split(",", 1)[0].upper()


def _is_upper_court(jid: str, court_name: str) -> bool:
    prefix = _jid_prefix(jid)
    return (
        prefix.startswith(_UPPER_COURT_PREFIXES)
        or prefix.endswith("TA")
        or "最高" in _s(court_name)
        or "高等" in _s(court_name)
        or "行政" in _s(court_name)
    )


def _has_substantive_signal(text: str) -> bool:
    return bool(_SUBSTANTIVE_SIGNALS.search(text or ""))


def _text_head(full_text: str, max_chars: int = 1800) -> str:
    text = _s(full_text)
    return text[:max_chars]


def classify_judgment_record(
    *,
    jid: str = "",
    court_name: str = "",
    case_number: str = "",
    case_reason: str = "",
    title: str = "",
    full_text: str = "",
) -> JudgmentValueDecision:
    """Classify whether a judgment should enter the practical-insight pipeline.

    This intentionally does not exclude every ruling.  It only returns
    SKIP_SUMMARY for high-confidence ministerial/procedural documents.  Ambiguous
    rulings stay REVIEW so they can be summarized or manually triaged later.
    """
    jid = _s(jid)
    court_name = _s(court_name)
    case_number = _s(case_number)
    case_reason = _s(case_reason)
    title = _s(title)
    head = _text_head(full_text)
    hay = " ".join([jid, court_name, case_number, case_reason, title, head])

    upper = _is_upper_court(jid, court_name)
    substantive = _has_substantive_signal(hay)

    # High courts and supreme courts often create useful procedural doctrine.
    # Keep obvious ministerial matches in REVIEW instead of skipping them.
    if upper:
        if _PURE_PROCEDURAL_SIGNALS.search(hay):
            return JudgmentValueDecision(REVIEW, "upper_court_procedural_ruling_review", 0.65, "upper_court")
        return JudgmentValueDecision(KEEP, "upper_court", 0.85, "authority")

    header_is_payment_order = bool(re.search(r"法院支付命令|支付命令", head[:40]))
    case_is_payment_order = bool(re.search(r"司促|促字", case_number + " " + jid))
    reason_is_payment_order = case_reason in {"支付命令"} or title in {"支付命令"}
    if case_is_payment_order or reason_is_payment_order or header_is_payment_order:
        return JudgmentValueDecision(SKIP_SUMMARY, "payment_order_ministerial", 0.98, "payment_order")

    if re.search(r"司票|票字", case_number + " " + jid) and re.search(r"本票|准許強制執行|強制執行", hay):
        return JudgmentValueDecision(SKIP_SUMMARY, "promissory_note_execution_ministerial", 0.95, "promissory_note")

    case_code_text = case_number + " " + jid

    if re.search(r"審補|板補|補字|家補|重補|司補|司家他|,[^,\s]{0,4}補,", case_code_text) and re.search(
        r"補繳裁判費|未據繳納裁判費|逾期不繳|逾期未補正|命補正|確定訴訟費用額", hay
    ):
        return JudgmentValueDecision(SKIP_SUMMARY, "fee_or_correction_order", 0.95, "fee_order")

    attached_civil = bool(re.search(r"附民", case_number + " " + jid + " " + case_reason) or "刑事附帶民事" in hay or "附帶民事訴訟" in hay)
    attached_civil_original_ruling = bool(
        re.search(r"附民", case_number + " " + jid + " " + case_reason)
        or re.search(r"刑事附帶民事訴訟裁定|附帶民事訴訟裁定", head[:160])
    )
    transfer_to_civil = bool(re.search(r"移送.*民事庭|移送於本院民事庭|移送本院民事庭", hay))
    if attached_civil and attached_civil_original_ruling and transfer_to_civil and not substantive:
        return JudgmentValueDecision(SKIP_SUMMARY, "attached_civil_transfer_order", 0.96, "attached_civil_transfer")

    if (
        attached_civil
        and re.search(r"刑事附帶民事訴訟裁定|附帶民事訴訟裁定", head[:80])
        and transfer_to_civil
        and not substantive
    ):
        return JudgmentValueDecision(SKIP_SUMMARY, "attached_civil_transfer_order", 0.96, "attached_civil_transfer")

    consumer_debt_case = bool(re.search(r"消債|更生|清算", case_number + " " + case_reason + " " + head[:500]))
    protective_order = "保全處分" in hay
    if consumer_debt_case or protective_order:
        if re.search(r"裁定", hay):
            return JudgmentValueDecision(REVIEW, "debt_or_protective_ruling_review", 0.6, "ruling_review")

    if re.search(r"司執|司拍|拍字", case_code_text) and re.search(r"移送管轄|移送.*法院|無管轄權", hay):
        return JudgmentValueDecision(SKIP_SUMMARY, "execution_jurisdiction_transfer", 0.94, "execution")

    if re.search(r"司執|司拍|拍字", case_code_text) and re.search(r"強制執行|拍賣|聲請駁回", hay) and not substantive:
        return JudgmentValueDecision(SKIP_SUMMARY, "execution_or_auction_ministerial", 0.9, "execution")

    if re.search(r"裁定", hay) and _PURE_PROCEDURAL_SIGNALS.search(hay) and not substantive:
        return JudgmentValueDecision(SKIP_SUMMARY, "pure_procedural_ruling", 0.86, "procedural_ruling")

    if re.search(r"裁定", hay):
        return JudgmentValueDecision(REVIEW, "ruling_may_contain_legal_value", 0.55, "ruling_review")

    return JudgmentValueDecision(KEEP, "default_keep", 0.6, "default")


def classify_jdoc_payload(payload: Dict[str, Any], *, court_name: Optional[str] = None) -> JudgmentValueDecision:
    jfullx = payload.get("JFULLX") if isinstance(payload, dict) else {}
    if isinstance(jfullx, list):
        jfullx = jfullx[0] if jfullx else {}
    if not isinstance(jfullx, dict):
        jfullx = {}
    jid = _s(payload.get("JID"))
    case_number = ""
    if payload.get("JYEAR") and payload.get("JCASE") and payload.get("JNO"):
        case_number = "%s年度%s字第%s號" % (_s(payload.get("JYEAR")), _s(payload.get("JCASE")), _s(payload.get("JNO")))
    return classify_judgment_record(
        jid=jid,
        court_name=court_name or _jid_prefix(jid),
        case_number=case_number,
        case_reason=_s(payload.get("JTITLE")),
        title=_s(payload.get("JTITLE")),
        full_text=_s(jfullx.get("JFULLCONTENT")),
    )

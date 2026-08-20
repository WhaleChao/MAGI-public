"""Deterministic, source-bound quality rules for practical judgment summaries.

The practical-insight index must prefer an honest empty result over a fluent
but irrelevant paragraph.  This module therefore selects exact source spans,
penalises generic procedural boilerplate, and requires the selected rule to
match the reported case issue before it can enter search or drafting indexes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Iterable


_PROCEDURAL_REASON_RE = re.compile(
    r"上訴|抗告|再審|訴訟救助|管轄|停止執行|假處分|假扣押|"
    r"迴避|定應執行刑|羈押|保全|補正|裁判費|程序|合法"
)
_GENERIC_REASON_RE = re.compile(r"^(?:裁判書|民事|刑事|行政|一般|其他|待確認)?$")
_GENERIC_PROCEDURE_RE = re.compile(
    r"上訴第三審法院.*非以.*違背法令|"
    r"上訴狀內應記載上訴理由|"
    r"未具體表明上訴理由|"
    r"第三審法院應於上訴聲明之範圍內|"
    r"非以判決違背法令為理由.*不得為之|"
    r"不得任意指摘為違法|"
    r"證據之取捨.*事實之認定.*職權|"
    r"其餘攻擊防禦方法.*不影響.*判決結果|"
    r"訴訟費用由.*負擔"
)
_TEMPLATE_RE = re.compile(
    r"一句話概括本判決|後接法律原則論述|從判決中逐字擷取|"
    r"列出(?:本判決|判決內文).*法條|請填入|範例|"
    r"本件無可擷取之實務見解|無可擷取|^\s*無[。．]?\s*$",
    re.MULTILINE,
)
_FAST_DIGEST_RE = re.compile(
    r"##\s*摘要型別\s*[\s\S]{0,120}?(?:抽取式快篩|preview|快速預覽)|"
    r"抽取式快篩（主文與理由均取自裁判原文",
    re.IGNORECASE,
)
_PROMPT_RE = re.compile(
    r"你是一位精確的法律助理|【嚴格規則】|判決內文：|"
    r"Thought:|Action:|Observation:|WFGY",
    re.IGNORECASE,
)
_RULE_RE = re.compile(
    r"(?:^|[；。])\s*"
    r"(?:[一二三四五六七八九十]+、|[（(]?\d+[）).、]\s*)?"
    r"(?:按|次按|再按|又按|本院按)|"
    r"第\s*\d+(?:-\d+)?\s*條(?:之\s*\d+)?|"
    r"應解為|係指|所謂|構成要件|舉證責任|因果關係|"
    r"法律上|準用|適用|規定|不得|得以|應以|應由"
)
_APPLICATION_RE = re.compile(
    r"本院認為|本院判斷|本院審酌|經查|惟查|準此|是以|"
    r"足認|應認|堪認|尚難|故其|從而|核其所請|衡酌|綜合考量"
)
_PARTY_SUBMISSION_RE = re.compile(
    r"^\s*(?:[一二三四五六七八九十]+、|[（(]?\d+[）).、]\s*)?"
    r"(?:(?:本件)?聲請(?:再審)?意旨(?:略以)?|再審聲請意旨|上訴意旨|抗告意旨|"
    r"原告主張|被告辯稱|聲請人(?:雖)?(?:主張|稱|認為|以)|"
    r"再審聲請人(?:雖)?(?:主張|稱|認為|以)|"
    r"相對人辯稱|檢察官聲請意旨)"
)
_PARTY_FACT_RE = re.compile(
    r"原告主張|被告辯稱|上訴人主張|抗告人主張|聲請人主張|"
    r"相對人辯稱|證人證稱|告訴人指稱|被告於民國|"
    r"住址|身分證|電話|帳戶|匯款|"
    r"上訴意旨|抗告意旨|本件原審審理結果|犯罪事實|"
    r"犯行明確|認被告|核被告|被告所為|被告經本院|"
    r"被告於|被告現|經判處|僅泛謂|卷附"
)
_RULE_LEAD_RE = re.compile(
    r"^\s*(?:[一二三四五六七八九十]+、|[（(]?\d+[）).、]\s*)?"
    r"(?:按|次按|再按|又按|本院按|"
    r"(?:民法|刑法|民事訴訟法|刑事訴訟法|行政訴訟法|家事事件法|"
    r"消費者債務清理條例|洗錢防制法|公司法|勞動基準法|"
    r"強制執行法|非訟事件法|家庭暴力防治法|"
    r"兒童及少年福利與權益保障法)?\s*第\s*\d+(?:-\d+)?\s*條|"
    r"最高(?:法院|行政法院)|憲法法庭|司法院釋字)"
)
_LEGAL_CONCEPT_RE = re.compile(
    r"構成要件|舉證責任|因果關係|違法性|過失|故意|信賴保護|"
    r"比例原則|誠信原則|權利濫用|消滅時效|表見代理|"
    r"不當得利|契約解除|損害賠償|意思表示|裁量權|"
    r"禁止錯誤|期待可能性|相當性|證明力|證據能力|"
    r"主觀犯意|客觀構成要件|罪之成立|成立.*罪|刑罰裁量|量刑|"
    r"責任能力|證明程度|不利益變更|罪刑相當"
)
_AUTHORITY_RE = re.compile(
    r"(?:最高法院|最高行政法院|憲法法庭|司法院釋字)\s*"
    r"\d{1,3}\s*(?:年|年度|字|號|釋字)"
)
_STATUTE_RE = re.compile(
    r"(?:(?:民法|刑法|民事訴訟法|刑事訴訟法|行政訴訟法|家事事件法|"
    r"消費者債務清理條例|洗錢防制法|公司法|勞動基準法|"
    r"強制執行法|非訟事件法|家庭暴力防治法|"
    r"兒童及少年福利與權益保障法)\s*)?"
    r"第\s*\d+(?:-\d+)?\s*條(?:之\s*\d+)?(?:第\s*\d+\s*項)?"
)
_OUTCOME_RE = re.compile(
    r"原告之訴駁回|上訴駁回|抗告駁回|聲請駁回|"
    r"原判決廢棄|原裁定廢棄|撤銷|應給付|准予|無罪|有罪"
)
_NORMATIVE_CONTINUATION_RE = re.compile(
    r"^\s*(?:故意以|數人|前項|但書|其)\S{0,120}"
    r"(?:者亦同|連帶負\S{0,24}責任|負\S{0,24}賠償責任)"
)

_TOPIC_TERMS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"侵權|損害賠償"), ("侵權", "損害", "過失", "因果關係", "民法第184條", "舉證責任")),
    (re.compile(r"詐欺"), ("詐欺", "詐術", "陷於錯誤", "交付財物", "不法得利")),
    (re.compile(r"洗錢"), ("洗錢", "金流", "掩飾", "隱匿", "洗錢防制法", "特定犯罪所得")),
    (re.compile(r"背信"), ("背信", "為他人處理事務", "違背任務", "本人損害")),
    (re.compile(r"傷害"), ("傷害", "身體", "健康", "因果關係", "故意")),
    (re.compile(r"更生|清算|消費者債務"), ("更生", "清算", "不能清償", "無擔保債務", "消費者債務清理條例")),
    (re.compile(r"免責"), ("免責", "不予免責", "終止清算", "消費者債務清理條例")),
    (re.compile(r"本票"), ("本票", "票據", "發票人", "執票人", "強制執行")),
    (re.compile(r"訴訟救助"), ("訴訟救助", "無資力", "釋明", "訴訟費用")),
    (re.compile(r"羈押"), ("羈押", "逃亡", "串證", "反覆實施", "比例原則")),
    (re.compile(r"上訴|第三審"), ("上訴", "第三審", "違背法令", "上訴理由")),
    (re.compile(r"抗告"), ("抗告", "抗告理由", "裁定")),
    (re.compile(r"再審"), ("再審", "再審事由", "確定判決")),
    (re.compile(r"勞動|工資|加班"), ("勞動", "工資", "加班", "從屬性", "勞動基準法")),
    (
        re.compile(r"個人資料|個資"),
        ("個人資料", "個資", "特種個資", "蒐集", "處理", "利用", "個人資料保護法"),
    ),
    (re.compile(r"恐嚇"), ("恐嚇", "惡害通知", "心生畏懼", "安全")),
    (re.compile(r"誹謗|妨害名譽"), ("誹謗", "名譽", "真實惡意", "合理查證")),
    (re.compile(r"毒品"), ("毒品", "持有", "販賣", "轉讓", "施用")),
    (re.compile(r"強盜|搶奪"), ("強盜", "搶奪", "強暴", "脅迫", "不能抗拒")),
    (re.compile(r"殺人"), ("殺人", "死亡", "殺人故意", "因果關係")),
    (
        re.compile(r"公共危險|酒駕|不能安全駕駛"),
        ("公共危險", "酒精濃度", "不能安全駕駛", "刑法第185條之3"),
    ),
    (
        re.compile(r"撤銷緩刑|緩刑"),
        ("撤銷緩刑", "緩刑", "刑法第75條", "刑法第75條之1", "違反法令"),
    ),
    (
        re.compile(r"定應執行刑|應執行之刑|數罪併罰"),
        ("定應執行刑", "定其應執行之刑", "應執行之刑", "數罪併罰", "刑法第51條", "裁量", "罪刑相當"),
    ),
    (
        re.compile(r"沒收"),
        ("沒收", "犯罪所得", "違禁物", "刑法第38條", "刑法第38條之1"),
    ),
    (
        re.compile(r"輔助宣告|監護宣告"),
        ("輔助宣告", "監護宣告", "意思表示", "精神障礙", "民法第14條", "民法第15條之1"),
    ),
    (
        re.compile(r"裁判費|起訴程式|補正"),
        ("裁判費", "起訴不合程式", "補正", "民事訴訟法第77條之13", "民事訴訟法第249條"),
    ),
    (re.compile(r"契約|給付"), ("契約", "給付", "債務不履行", "解除", "同時履行抗辯")),
    (
        re.compile(r"參與審判或追訴|審判或追訴職務|國家賠償"),
        ("審判", "追訴", "公務員", "國家賠償", "職務上之罪", "民法第186條"),
    ),
    (
        re.compile(r"債務人異議|強制執行異議"),
        ("債務人異議之訴", "執行名義", "強制執行", "消滅時效", "權利濫用"),
    ),
    (
        re.compile(r"消滅時效|時效中斷"),
        ("消滅時效", "時效中斷", "重新起算", "請求權", "民法第129條", "民法第137條"),
    ),
)

_PROCEDURAL_PRIMARY_TERMS: tuple[tuple[re.Pattern[str], tuple[str, ...]], ...] = (
    (re.compile(r"撤銷緩刑"), ("撤銷緩刑", "刑法第75條", "刑法第75條之1")),
    (
        re.compile(r"定應執行刑"),
        ("定應執行刑", "定其應執行之刑", "應執行之刑", "數罪併罰", "刑法第51條"),
    ),
    (re.compile(r"停止羈押"), ("停止羈押", "具保", "刑事訴訟法第114條")),
    (re.compile(r"延長羈押"), ("延長羈押", "刑事訴訟法第108條")),
    (re.compile(r"羈押"), ("羈押", "逃亡", "串證", "反覆實施")),
    (re.compile(r"再審"), ("再審", "再審事由", "確定判決")),
    (re.compile(r"沒收"), ("沒收", "犯罪所得", "違禁物", "刑法第38條")),
    (re.compile(r"交付法庭錄音"), ("法庭錄音", "交付法庭錄音")),
)


def _primary_issue_terms(case_reason: str) -> tuple[str, ...]:
    reason = re.sub(r"\s+", "", str(case_reason or ""))
    for pattern, terms in _PROCEDURAL_PRIMARY_TERMS:
        if pattern.search(reason):
            return terms
    return ()


def _normalize_caption_issue(value: str) -> str:
    candidate = re.sub(r"\s+", "", str(value or "")).strip(" ，。、")
    if re.search(r"數罪併罰.*(?:二|2).*裁判以上", candidate):
        return ""
    canonical = (
        (r"(?:債務)?人?聲請更生|聲請更生", "更生"),
        (r"(?:債務)?人?聲請清算|聲請清算", "清算"),
        (r"聲請假扣押|請求假扣押", "假扣押"),
        (r"聲請假處分|請求假處分", "假處分"),
        (r"核發.*保護令", "保護令"),
        (r"聲請給付扶養費|請求給付扶養費", "給付扶養費"),
        (r"死亡宣告", "死亡宣告"),
        (r"選任臨時管理人", "選任臨時管理人"),
        (r"變更.*捐助章程", "變更捐助章程"),
        (r"許可監護人.*行為", "許可監護人行為"),
        (r"撤銷輔助宣告", "撤銷輔助宣告"),
        (r"撤銷監護宣告", "撤銷監護宣告"),
    )
    for pattern, label in canonical:
        if re.search(pattern, candidate):
            return label
    if re.search(
        r"股份有限公司|有限公司|財團法人|委員會|董事長|為相對人|"
        r"本案係|繫屬於|經核於法|尚無不合|判決如下|言詞辯論|"
        r"本院|原審|檢察官|犯罪事實|之本案|依法核准|應予准許",
        candidate,
    ):
        return ""
    return candidate


def infer_case_issue(
    full_text: str,
    case_number: str = "",
    current_reason: str = "",
) -> str:
    """Infer a usable legal issue when legacy rows only say ``一般``.

    The function is intentionally deterministic and source-bound.  It reads
    only the caption/opening paragraph and never invents an offence or claim.
    Procedural applications (retrial, detention, sentence aggregation, etc.)
    take precedence because they are the actual issue decided by the ruling.
    """
    current = re.sub(r"\s+", "", str(current_reason or ""))
    if current and not _GENERIC_REASON_RE.fullmatch(current):
        return current[:80]

    header = re.sub(r"\s+", " ", str(full_text or "")[:2600]).strip()
    number = re.sub(r"\s+", "", str(case_number or ""))
    caption_issue = ""
    caption_patterns = (
        r"上列當事人間(?:因|請求)\s*([^，。；：]{2,48}?)(?:事件|，|於)",
        r"上列(?:聲請人|抗告人|再審聲請人).*?(?:聲請|因)\s*([^，。；：]{2,48}?)(?:事件|案件|，)",
        r"(?:本件|兩造間)(?:係|為)?(?:請求|聲請)\s*([^，。；：]{2,48}?)(?:事件|，|之訴)",
        r"案\s*由\s*[：:]\s*([^，。；：\n]{2,48})",
    )
    for pattern in caption_patterns:
        caption_match = re.search(pattern, header)
        if not caption_match:
            continue
        candidate = _normalize_caption_issue(caption_match.group(1))
        if (
            2 <= len(candidate) <= 40
            and not re.search(r"本院|原審|言詞辯論|判決如下", candidate)
        ):
            caption_issue = candidate
            break
    procedure = ""
    procedure_rules = (
        (r"聲請再審|再審聲請|聲再", "再審"),
        (r"聲請重新審理|重新審理", "重新審理"),
        (r"定其應執行之刑|定應執行刑|數罪併罰", "定應執行刑"),
        (r"具保停止羈押|停止羈押", "停止羈押"),
        (r"延長羈押", "延長羈押"),
        (r"聲請羈押|羈押", "羈押"),
        (r"施以強制治療|強制治療", "強制治療"),
        (r"交付法庭錄音|法庭錄音光碟", "交付法庭錄音"),
        (r"發還扣押物", "發還扣押物"),
        (r"沒收扣押物|單獨宣告沒收", "沒收"),
        (r"聲請保護令|家事保護令", "保護令"),
        (r"訴訟救助", "訴訟救助"),
        (r"撤銷緩刑", "撤銷緩刑"),
        (r"撤銷輔助宣告|輔助宣告", "輔助宣告"),
        (r"撤銷監護宣告|監護宣告", "監護宣告"),
        (r"裁判費|起訴不合程式", "裁判費／起訴程式"),
    )
    procedure_source = f"{number} {header}"
    for pattern, label in procedure_rules:
        if re.search(pattern, procedure_source):
            procedure = label
            break

    underlying = ""
    for match in re.finditer(
        r"因(?:涉嫌|犯|違反)?\s*([^，。；：]{2,48}?)(?:等)?案件",
        header,
    ):
        candidate = re.sub(
            r"^(?:被告|受刑人|受處分人|聲請人|再審聲請人|即受判決人|違反)+",
            "",
            match.group(1).strip(),
        )
        candidate = re.sub(r"(?:之)?案件$", "", candidate).strip(" ，。、")
        # 定應執行刑裁定常把「受刑人數罪併罰有二裁判以上」放在
        # 「因…案件」中；這是程序說明，不是基礎案由，不可寫進分類。
        if re.search(r"數罪併罰.*(?:二|2).*裁判以上", candidate):
            continue
        if (
            2 <= len(candidate) <= 40
            and not re.search(
                r"本院|原審|判決確定|聲請人因受刑人|"
                r"受刑人數罪併罰有二裁判以上",
                candidate,
            )
        ):
            underlying = candidate
            break
    if not underlying:
        underlying = caption_issue

    if procedure and underlying and procedure not in underlying:
        return f"{procedure}（{underlying}）"[:80]
    if procedure:
        return procedure
    if underlying:
        return underlying[:80]
    return "未分類" if not current or _GENERIC_REASON_RE.fullmatch(current) else current


@dataclass(frozen=True)
class SummaryQuality:
    ok: bool
    score: int
    reason: str
    source_supported_spans: int
    rule_spans: int
    application_spans: int
    issue_terms: tuple[str, ...]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PracticeSpan:
    text: str
    score: int
    kind: str
    relevant_terms: tuple[str, ...]
    generic_procedure: bool


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).replace("臺", "台")


def _display_source_span(text: str) -> str:
    """Remove PDF line-wrap noise without changing the source wording."""
    return re.sub(r"\s+", "", str(text or "")).strip()


def _section(summary: str, heading: str) -> str:
    match = re.search(
        rf"##\s*{re.escape(heading)}\s*(.*?)(?=\n##\s*|\Z)",
        str(summary or ""),
        re.DOTALL,
    )
    return match.group(1).strip() if match else ""


def _reason_section(full_text: str) -> str:
    text = re.sub(r"\r\n?", "\n", str(full_text or "")).strip()
    match = re.search(
        r"(?:^|\n)\s*(?:事\s*實\s*及\s*理\s*由|理\s*由)\s*(?:\n|$)",
        text,
    )
    if not match:
        return text
    reason = text[match.end() :]
    end = re.search(r"(?:^|\n)\s*中\s*華\s*民\s*國", reason)
    return reason[: end.start()].strip() if end else reason.strip()


def _source_units(full_text: str) -> list[str]:
    reason = _reason_section(full_text)
    reason = re.sub(r"[ \t]+", " ", reason)
    raw_units = re.split(
        # A semicolon usually connects the elements of one legal rule
        # (statute, elements, burden, exception).  Splitting there produced
        # fluent but legally incomplete fragments.  Preserve the whole
        # sentence and split on semicolons only in the long-paragraph guard.
        r"\n{2,}|(?<=。)\s*",
        reason,
    )
    units: list[str] = []
    for raw in raw_units:
        unit = re.sub(r"\n+", " ", raw).strip()
        if len(_norm(unit)) < 22:
            continue
        # Extremely long OCR paragraphs are divided only at existing sentence
        # boundaries so every emitted span remains an exact source substring.
        if len(unit) > 1100:
            pieces = re.split(r"(?<=[。；])", unit)
            buffer = ""
            for piece in pieces:
                if not piece:
                    continue
                if buffer and len(buffer) + len(piece) > 850:
                    units.append(buffer.strip())
                    buffer = piece
                else:
                    buffer += piece
            if buffer.strip():
                units.append(buffer.strip())
        else:
            units.append(unit)
    return units


def issue_terms(case_reason: str) -> tuple[str, ...]:
    reason = re.sub(r"\s+", "", str(case_reason or ""))
    terms: list[str] = []
    for pattern, mapped in _TOPIC_TERMS:
        if pattern.search(reason):
            terms.extend(mapped)
    if reason and not _GENERIC_REASON_RE.fullmatch(reason):
        terms.append(reason)
        for token in re.findall(r"[\u4e00-\u9fff]{2,8}", reason):
            if token not in {"事件", "案件", "裁定", "判決"}:
                terms.append(token)
    return tuple(dict.fromkeys(terms))


def _score_unit(unit: str, case_reason: str) -> PracticeSpan:
    compact = _norm(unit)
    terms = issue_terms(case_reason)
    matched = tuple(term for term in terms if _norm(term) in compact)
    generic_procedure = bool(_GENERIC_PROCEDURE_RE.search(unit))
    procedural_issue = bool(_PROCEDURAL_REASON_RE.search(case_reason or ""))
    submission = bool(_PARTY_SUBMISSION_RE.search(unit))
    application_lead = bool(
        re.match(
            r"^\s*(?:[一二三四五六七八九十]+、|[（(]?\d+[）).、]\s*)?"
            r"(?:本院認為|本院判斷|本院審酌|本院參酌|經查|惟查|準此|是以|足認|應認|堪認|尚難)",
            unit,
        )
    )
    normative_continuation = bool(
        _NORMATIVE_CONTINUATION_RE.search(re.sub(r"\s+", "", unit))
        and not application_lead
    )
    # Taiwanese judgments frequently omit a paragraph-leading ``按`` after an
    # OCR line break.  A source span that contains an actual statute, a
    # normative marker, and a term tied to the inferred issue is still a legal
    # rule.  Requiring all three signals prevents fact paragraphs that merely
    # mention the offence from being promoted.
    relevant_statutory_rule = bool(
        matched
        and _STATUTE_RE.search(unit)
        and _RULE_RE.search(unit)
        and not submission
        and not _PARTY_FACT_RE.search(unit[:220])
    )
    rule = bool(
        (
            (
                _RULE_RE.search(unit)
                and (
                    _RULE_LEAD_RE.search(unit)
                    or _AUTHORITY_RE.search(unit)
                    or re.search(r"應解為|係指|所謂|構成要件", unit)
                    or (application_lead and _LEGAL_CONCEPT_RE.search(unit))
                )
            )
            or normative_continuation
            or relevant_statutory_rule
        )
        and not submission
    )
    application = bool(_APPLICATION_RE.search(unit))
    score = 0
    if rule:
        score += 22
    if _STATUTE_RE.search(unit):
        score += 14
    if _LEGAL_CONCEPT_RE.search(unit):
        score += 16
    if _AUTHORITY_RE.search(unit):
        score += 9
    if application:
        score += 8
    score += min(36, len(matched) * 12)
    if _PARTY_FACT_RE.search(unit[:220]):
        score -= 48
    if submission:
        score -= 120
    if len(re.findall(r"\d", unit)) >= 20 and not _STATUTE_RE.search(unit):
        score -= 18
    if generic_procedure:
        score -= 6 if procedural_issue else 58
    if len(unit) > 850:
        score -= 10
    kind = "application" if application_lead else ("rule" if rule else ("application" if application else "other"))
    return PracticeSpan(
        text=unit,
        score=score,
        kind=kind,
        relevant_terms=matched,
        generic_procedure=generic_procedure,
    )


def rank_practice_candidates(
    full_text: str,
    case_reason: str = "",
    *,
    max_candidates: int = 14,
) -> list[PracticeSpan]:
    """Return source-exact candidate spans for a guarded external selector.

    The external model is never allowed to author the stored quotation.  It
    may only choose among these deterministic candidates.  Candidate ordering
    deliberately keeps both doctrine and application paragraphs so the final
    summary can explain a reusable rule and how the court applied it.
    """
    scored = [_score_unit(unit, case_reason) for unit in _source_units(full_text)]
    scored.sort(
        key=lambda span: (
            span.kind in {"rule", "application"},
            span.score,
            len(span.relevant_terms),
            -len(span.text),
        ),
        reverse=True,
    )
    substantive_issue = bool(
        case_reason and not _GENERIC_REASON_RE.fullmatch(str(case_reason or ""))
    )
    expected_terms = issue_terms(case_reason)
    primary_terms = _primary_issue_terms(case_reason)
    selected: list[PracticeSpan] = []
    seen: set[str] = set()
    for span in scored:
        key = _norm(span.text)[:120]
        if not key or key in seen or _PARTY_SUBMISSION_RE.search(span.text):
            continue
        if span.kind == "rule":
            minimum = 26
        elif span.kind == "application":
            minimum = 18
        else:
            minimum = 30
        if span.score < minimum:
            continue
        if (
            substantive_issue
            and expected_terms
            and not span.relevant_terms
        ):
            continue
        if primary_terms and not any(
            _norm(term) in _norm(span.text) for term in primary_terms
        ):
            continue
        selected.append(span)
        seen.add(key)
        if len(selected) >= max(1, int(max_candidates)):
            break
    return selected


def select_practice_spans(
    full_text: str,
    case_reason: str = "",
    *,
    rule_limit: int = 2,
    application_limit: int = 1,
) -> tuple[list[PracticeSpan], list[PracticeSpan]]:
    scored = [_score_unit(unit, case_reason) for unit in _source_units(full_text)]
    scored.sort(key=lambda span: (span.score, len(span.relevant_terms), -len(span.text)), reverse=True)
    substantive_issue = bool(case_reason and not _GENERIC_REASON_RE.fullmatch(case_reason))
    issue_has_terms = bool(issue_terms(case_reason))
    primary_terms = _primary_issue_terms(case_reason)

    rules: list[PracticeSpan] = []
    applications: list[PracticeSpan] = []
    seen: set[str] = set()
    for span in scored:
        key = _norm(span.text)[:100]
        if key in seen:
            continue
        if _PARTY_SUBMISSION_RE.search(span.text):
            continue
        if span.kind == "rule" and span.score >= 30:
            if substantive_issue and issue_has_terms and not span.relevant_terms:
                continue
            if primary_terms and not any(
                _norm(term) in _norm(span.text) for term in primary_terms
            ):
                continue
            rules.append(span)
            seen.add(key)
            if len(rules) >= max(1, int(rule_limit)):
                break
    if not rules:
        return [], []

    for span in scored:
        key = _norm(span.text)[:100]
        if key in seen or span.score < 20 or not _APPLICATION_RE.search(span.text):
            continue
        if substantive_issue and issue_has_terms and not span.relevant_terms:
            continue
        applications.append(span)
        seen.add(key)
        if len(applications) >= max(0, int(application_limit)):
            break
    return rules, applications


def _main_outcome(full_text: str) -> str:
    text = re.sub(r"\r\n?", "\n", str(full_text or ""))
    match = re.search(
        r"(?:^|\n)\s*主\s*文\s*(.*?)(?=\n\s*(?:事\s*實|理\s*由|事\s*實\s*及\s*理\s*由)\s*(?:\n|$))",
        text,
        re.DOTALL,
    )
    area = match.group(1).strip() if match else text[:1200]
    hit = _OUTCOME_RE.search(area)
    if not hit:
        return ""
    sentence_start = max(area.rfind("\n", 0, hit.start()), area.rfind("。", 0, hit.start())) + 1
    sentence_end = area.find("。", hit.end())
    sentence_end = len(area) if sentence_end < 0 else sentence_end + 1
    return re.sub(r"\s+", " ", area[sentence_start:sentence_end]).strip()[:260]


def _statutes(text: str) -> list[str]:
    values = [re.sub(r"\s+", "", match.group(0)) for match in _STATUTE_RE.finditer(text)]
    return list(dict.fromkeys(value for value in values if len(value) >= 4))


def build_extractive_practice_summary(
    full_text: str,
    case_reason: str = "",
    *,
    max_chars: int = 1800,
) -> str:
    rules, applications = select_practice_spans(full_text, case_reason)
    if not rules:
        return ""

    def _render(
        current_rules: list[PracticeSpan],
        current_applications: list[PracticeSpan],
    ) -> str:
        matched_terms = tuple(
            dict.fromkeys(
                term
                for span in (*current_rules, *current_applications)
                for term in span.relevant_terms
            )
        )
        lines = [
            "## 法律爭點",
            f"- {case_reason or '未分類'}"
            + (f"；命中：{'、'.join(matched_terms[:6])}" if matched_terms else ""),
            "",
            "## 實務見解",
        ]
        lines.extend(f"- {_display_source_span(span.text)}" for span in current_rules)
        if current_applications:
            lines.extend(["", "## 法院涵攝"])
            lines.extend(
                f"- {_display_source_span(span.text)}"
                for span in current_applications
            )
        outcome = _main_outcome(full_text)
        if outcome:
            lines.extend(["", "## 裁判結果", f"- {outcome}"])
        statutes = _statutes(
            "\n".join(
                span.text for span in (*current_rules, *current_applications)
            )
        )
        if statutes:
            lines.extend(["", "## 適用法條", "、".join(statutes[:12])])
        lines.extend(["", "## 摘要方式", "原文擷取；未以模型改寫（僅正規化空白）"])
        return "\n".join(lines).strip()

    output = _render(rules, applications)
    while len(output) > max_chars and applications:
        applications = applications[:-1]
        output = _render(rules, applications)
    while len(output) > max_chars and len(rules) > 1:
        rules = rules[:-1]
        output = _render(rules, applications)
    # A single exact source span that cannot fit is not safe to truncate.
    return output if len(output) <= max_chars else ""


def _opinion_units(summary: str) -> list[str]:
    opinion = _section(summary, "實務見解")
    units = []
    for raw in re.split(r"\n+|(?<=[。；])\s*", opinion):
        unit = re.sub(r"^\s*(?:[-*•]|[（(]?\d+[）).、])\s*", "", raw).strip()
        if len(_norm(unit)) >= 10:
            units.append(unit)
    return units


def _application_units(summary: str) -> list[str]:
    application = _section(summary, "法院涵攝")
    units = []
    for raw in re.split(r"\n+|(?<=[。；])\s*", application):
        unit = re.sub(r"^\s*(?:[-*•]|[（(]?\d+[）).、])\s*", "", raw).strip()
        if len(_norm(unit)) >= 10:
            units.append(unit)
    return units


def screen_stored_summary(summary: str, case_reason: str = "") -> SummaryQuality:
    text = str(summary or "").strip()
    if not text:
        return SummaryQuality(False, 0, "empty_summary", 0, 0, 0, ())
    if _PROMPT_RE.search(text):
        return SummaryQuality(False, 0, "prompt_or_trace_leak", 0, 0, 0, ())
    if _FAST_DIGEST_RE.search(text):
        return SummaryQuality(False, 8, "fast_digest_preview", 0, 0, 0, ())
    if _TEMPLATE_RE.search(_section(text, "實務見解") or text):
        return SummaryQuality(False, 0, "template_or_placeholder", 0, 0, 0, ())
    units = _opinion_units(text)
    if not units:
        return SummaryQuality(False, 5, "missing_opinion_section", 0, 0, 0, ())
    spans = [_score_unit(unit, case_reason) for unit in units]
    rules = [span for span in spans if span.kind == "rule" and span.score >= 26]
    applications = [
        span
        for span in (
            spans
            + [_score_unit(unit, case_reason) for unit in _application_units(text)]
        )
        if _APPLICATION_RE.search(span.text) and span.score >= 18
    ]
    if not rules:
        return SummaryQuality(False, 15, "missing_substantive_rule", 0, 0, len(applications), ())
    if all(span.generic_procedure for span in rules) and not _PROCEDURAL_REASON_RE.search(case_reason or ""):
        return SummaryQuality(False, 18, "generic_procedure_only", 0, len(rules), len(applications), ())
    terms = tuple(dict.fromkeys(term for span in rules for term in span.relevant_terms))
    if issue_terms(case_reason) and not terms:
        return SummaryQuality(False, 24, "case_issue_mismatch", 0, len(rules), len(applications), ())
    primary_terms = _primary_issue_terms(case_reason)
    if primary_terms and not any(
        _norm(term) in _norm("\n".join(span.text for span in rules))
        for term in primary_terms
    ):
        return SummaryQuality(False, 24, "case_issue_mismatch", 0, len(rules), len(applications), terms)
    score = min(
        100,
        44
        + min(24, max(span.score for span in rules) // 3)
        + min(12, len(rules) * 6)
        + min(10, len(applications) * 5)
        + min(10, len(terms) * 3),
    )
    return SummaryQuality(True, score, "", 0, len(rules), len(applications), terms)


def evaluate_practice_summary(
    summary: str,
    source_text: str,
    case_reason: str = "",
) -> SummaryQuality:
    screened = screen_stored_summary(summary, case_reason)
    if not screened.ok:
        return screened
    source_norm = _norm(source_text)
    supported = 0
    support_units = _opinion_units(summary) + _application_units(summary)
    for unit in support_units:
        unit_norm = _norm(unit)
        if unit_norm in source_norm:
            supported += 1
            continue
        windows = (unit_norm[:36], unit_norm[-36:])
        if any(len(window) >= 24 and window in source_norm for window in windows):
            supported += 1
    if supported == 0:
        return SummaryQuality(
            False,
            min(screened.score, 20),
            "unsupported_opinion",
            0,
            screened.rule_spans,
            screened.application_spans,
            screened.issue_terms,
        )
    opinion_units = _opinion_units(summary)
    if supported < len(support_units):
        return SummaryQuality(
            False,
            min(screened.score, 35),
            "partially_unsupported_opinion",
            supported,
            screened.rule_spans,
            screened.application_spans,
            screened.issue_terms,
        )
    opinion_cjk = len(
        re.findall(r"[\u4e00-\u9fff]", _section(summary, "實務見解"))
    )
    if opinion_cjk < 40:
        return SummaryQuality(
            False,
            min(screened.score, 42),
            "opinion_too_short",
            supported,
            screened.rule_spans,
            screened.application_spans,
            screened.issue_terms,
        )
    # A bare quotation of a statute is rarely a reusable practical insight.
    # Require either an application paragraph or a recognisable doctrinal /
    # precedential signal.  This keeps bulk backfill from turning the library
    # into a collection of copied legislation.
    opinion_text = "\n".join(opinion_units)
    procedural_issue_match = bool(
        _PROCEDURAL_REASON_RE.search(str(case_reason or ""))
        and screened.issue_terms
    )
    if (
        screened.application_spans == 0
        and not procedural_issue_match
        and not _LEGAL_CONCEPT_RE.search(opinion_text)
        and not _AUTHORITY_RE.search(opinion_text)
        and not re.search(r"係指|應解為|所謂|判例|決議|大法庭|統一見解", opinion_text)
    ):
        return SummaryQuality(
            False,
            min(screened.score, 42),
            "statute_only_without_application",
            supported,
            screened.rule_spans,
            screened.application_spans,
            screened.issue_terms,
        )
    return SummaryQuality(
        True,
        screened.score,
        "",
        supported,
        screened.rule_spans,
        screened.application_spans,
        screened.issue_terms,
    )


def evaluate_practice_ready_summary(
    summary: str,
    source_text: str,
    case_reason: str = "",
    court_name: str = "",
    *,
    min_score: int = 80,
) -> SummaryQuality:
    """Apply the stricter contract used by reminders, OSC and pleadings.

    ``evaluate_practice_summary`` proves that the stored propositions are
    source-bound legal rules.  That is necessary for storage, but it is not
    sufficient for material shown to a lawyer as a *usable* practical
    insight: ordinary trial judgments must also show how the court applied
    the rule.  A narrow exception is retained for high-authority doctrinal
    decisions whose proposition is itself the reusable holding.
    """

    quality = evaluate_practice_summary(summary, source_text, case_reason)
    if not quality.ok:
        return quality

    threshold = max(70, min(100, int(min_score)))
    if quality.score < threshold:
        return SummaryQuality(
            False,
            quality.score,
            "below_practice_ready_score",
            quality.source_supported_spans,
            quality.rule_spans,
            quality.application_spans,
            quality.issue_terms,
        )
    if quality.application_spans > 0:
        return quality

    opinion_text = "\n".join(_opinion_units(summary))
    high_authority = bool(
        re.search(r"憲法法庭|大法庭|最高法院|最高行政法院", str(court_name or ""))
    )
    doctrinal_holding = bool(
        _AUTHORITY_RE.search(opinion_text)
        or re.search(r"係指|應解為|所謂|判例|決議|大法庭|統一見解", opinion_text)
    )
    if high_authority and doctrinal_holding and quality.score >= max(84, threshold):
        return quality

    return SummaryQuality(
        False,
        min(quality.score, 69),
        "missing_case_application",
        quality.source_supported_spans,
        quality.rule_spans,
        quality.application_spans,
        quality.issue_terms,
    )


def quality_reason_counts(
    rows: Iterable[tuple[str, str]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for summary, case_reason in rows:
        reason = screen_stored_summary(summary, case_reason).reason or "accepted"
        counts[reason] = counts.get(reason, 0) + 1
    return counts

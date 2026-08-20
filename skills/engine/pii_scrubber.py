"""Fail-closed privacy boundary for text sent to hosted models.

The scrubber is deliberately conservative for a Taiwan law office.  It keeps
the reversible mapping in process memory only, emits a non-sensitive audit
certificate, and performs a second residual scan before a payload may leave
MAGI.  Callers must not log ``mapping`` or the original/scrubbed payload.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

logger = logging.getLogger("PIIScrubber")

PRIVACY_POLICY_VERSION = "tw-law-office-deid-v2"
PRIVACY_PROFILES = frozenset(
    {"office_confidential", "public_judgment", "public_source", "synthetic"}
)

# Direct identifiers and Taiwan-specific office identifiers.
_TW_ID_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z][12]\d{8}(?!\d)", re.IGNORECASE)
_TW_ARC_RE = re.compile(r"(?<![A-Za-z0-9])[A-Z](?:[A-D]|[89])\d{8}(?!\d)", re.IGNORECASE)
_LAF_CASE_RE = re.compile(r"(?<![A-Za-z0-9])\d{7}-[A-Z]-\d{3}(?![A-Za-z0-9])", re.IGNORECASE)
_INTERNAL_CASE_RE = re.compile(r"(?<!\d)20\d{2}-\d{4}(?!\d)")
_COURT_CASE_RE = re.compile(r"\d{2,3}\s*年(?:度)?\s*[\u4e00-\u9fff]{1,12}\s*字\s*第?\s*\d{1,8}\s*號")
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_TW_MOBILE_RE = re.compile(r"(?<!\d)(?:\+?886[-\s]?)?0?9\d{2}[-\s]?\d{3}[-\s]?\d{3}(?!\d)")
_TW_LANDLINE_RE = re.compile(r"(?<!\d)(?:\+?886[-\s]?)?(?:\(?0\d{1,2}\)?[-\s]?)\d{3,4}[-\s]?\d{3,4}(?!\d)")
_IPV4_RE = re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")
_URL_RE = re.compile(r"https?://[^\s<>\]\[\"']+", re.IGNORECASE)
_PASSPORT_RE = re.compile(r"(?:護照(?:號碼|號)?|passport(?:\s*no\.?)?)\s*[:：]?\s*([A-Z0-9]{6,12})", re.IGNORECASE)
_BANK_ACCOUNT_RE = re.compile(
    r"(?:(?:銀行|郵局)?帳號|存摺帳號|匯款帳號|信用卡號)\s*[:：]?\s*([0-9][0-9\s-]{5,24}[0-9])"
)
_MEDICAL_ID_RE = re.compile(
    r"(?:病歷號|就醫序號|健保卡號|診斷書號)\s*[:：]?\s*([A-Za-z0-9-]{4,24})"
)
_VEHICLE_ID_RE = re.compile(r"(?:車牌|牌照)號碼?\s*[:：]?\s*([A-Z0-9-]{4,10})", re.IGNORECASE)
_BIRTH_RE = re.compile(
    r"(?:出生(?:日期|年月日)?|生日|生於)\s*[:：]?\s*"
    r"((?:民國)?\s*\d{2,4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})"
)
_ADDRESS_LABEL_RE = re.compile(
    r"(?:戶籍地|通訊地址|送達處所|住所|居所|住址|地址)\s*[:：]?\s*"
    r"([^\n，,；;。]{5,100})"
)
_TW_ADDRESS_RE = re.compile(
    r"(?:臺|台)(?:北|中|南|東)市[^\n，,；;。]{2,70}(?:路|街|大道|巷|弄|號|樓)"
    r"|(?:新北市|桃園市|高雄市|[一-鿿]{2,3}縣)[^\n，,；;。]{2,70}(?:路|街|大道|巷|弄|號|樓)"
)
_LABELLED_ENTITY_RE = re.compile(
    r"(?:公司名稱|商號名稱|雇主|工作單位|就讀學校)\s*[:：]?\s*([^\n，,；;。]{2,50})"
)

_PERSON_ROLES = (
    "被付懲戒人|法定代理人|選任辯護人|承辦律師|被上訴人|"
    "上訴人|聲請人|相對人|告訴人|被害人|委任人|受扶助人|"
    "債務人|債權人|關係人|具保人|受刑人|原告|被告|證人|"
    "辯護人|代理人|當事人|客戶|申請人|律師|姓名"
)
_ROLE_NAME_RE = re.compile(
    rf"(?P<role>{_PERSON_ROLES})(?P<sep>\s*(?:姓名)?\s*[:：]\s*|\s+|)"
    rf"(?P<name>[\u4e00-\u9fff○◯〇Ｏ某]{{2,5}})"
)
_COMMON_SURNAMES = frozenset(
    "趙錢孫李周吳鄭王馮陳褚衛蔣沈韓楊朱秦尤許何呂施張孔曹嚴華金魏陶姜戚謝鄒喻柏水竇章雲蘇潘葛奚范彭郎魯韋昌馬苗鳳花方俞任袁柳酆鮑史唐費廉岑薛雷賀倪湯滕殷羅畢郝邬安常樂于時傅皮卞齊康伍餘元卜顧孟平黃和穆蕭尹姚邵湛汪祁毛禹狄米貝明臧計伏成戴談宋茅龐熊紀舒屈項祝董梁杜阮藍閵席季麻強賈路婁危江童顏郭梅盛林刁鍾徐邱駱高夏蔡田樊胡凌霍虞萬支柯管盧莫經房裘繆干解應宗丁宣賁鄧郁單杭洪包諸左石崔吉鈕龔程嶇刑滑裴陸榮翁荀羊於惠甄麴家封芊羿儲靡松井段富巫烏焦巴弓牧隗山谷車侯宓蓬全郗班仰秋仲伊宮寧仇栾暴甘旜厲戎祖武符劉景詹束龍葉幸司韶黎薊薄印宿白懷蒲邰從鄂索咸籍賴卓藺屠蒙池喬陰鬱胥能蒼雙聞莘黨翟譚貢勞逖姬申扶堵冉宰酈雍卻璆桑桂濮牛壽通邊扈燕冀浦尚農溫別莊晏柴瞿閻充慕連茹習宦艾魚容向古易慎戈廖庾終暨居衡步都耿滿弘匡國文寇廣祿闕東歐殳沃利蔚越夔隆師鞏厭聶晁勾敖融冷訾辛闞那簡饒空曾毋沙之養鞠須豐巢關蒯相查後荊紅游竺權逑蓋益桓公"
)
_ROLE_FALSE_NAMES = frozenset(
    {
        "主張", "抗辯", "陳稱", "表示", "認為", "辩稱", "辨稱", "又稱", "略以", "未到",
        "到庭", "不服", "提起", "聲明", "請求", "就本案", "依法", "有罪", "無罪",
    }
)
_ROLE_FOLLOW_WORDS = (
    "主張", "陳稱", "辩稱", "辯稱", "認為", "表示", "略以", "請求", "聲明", "提起", "的",
)


@dataclass
class ScrubResult:
    """Scrubbed payload plus an in-memory-only reversible mapping."""

    scrubbed_text: str
    mapping: Dict[str, str] = field(default_factory=dict, repr=False)
    counts: Dict[str, int] = field(default_factory=dict)
    safe_to_send: bool = False
    residual_categories: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    profile: str = "office_confidential"
    policy_version: str = PRIVACY_POLICY_VERSION
    original_sha256: str = ""
    scrubbed_sha256: str = ""
    known_names_verified: bool = False

    def restore(self, text: str) -> str:
        """Restore placeholders locally.  The mapping must never be persisted."""
        out = text or ""
        for placeholder in sorted(self.mapping.keys(), key=len, reverse=True):
            out = out.replace(placeholder, self.mapping[placeholder])
        return out

    def certificate(self) -> dict:
        """Return evidence that contains no source text or reversible values."""
        return {
            "policy_version": self.policy_version,
            "profile": self.profile,
            "safe_to_send": self.safe_to_send,
            "counts": dict(self.counts),
            "residual_categories": list(self.residual_categories),
            "warnings": list(self.warnings),
            "known_names_verified": self.known_names_verified,
            "original_sha256": self.original_sha256,
            "scrubbed_sha256": self.scrubbed_sha256,
        }


class PIIScrubber:
    def __init__(
        self,
        *,
        known_names: Optional[Sequence[str]] = None,
        known_names_verified: Optional[bool] = None,
        source: str = "caller",
    ):
        supplied = known_names is not None
        names = []
        for value in known_names or ():
            name = str(value or "").strip()
            if len(name) >= 2 and name not in names:
                names.append(name)
        names.sort(key=len, reverse=True)
        self.known_names = names
        self.known_names_verified = supplied if known_names_verified is None else bool(known_names_verified)
        self.source = str(source or "caller")

    def scrub(
        self,
        text: str,
        *,
        profile: str = "office_confidential",
        require_known_names: Optional[bool] = None,
    ) -> ScrubResult:
        profile = str(profile or "office_confidential").strip().lower()
        if profile not in PRIVACY_PROFILES:
            profile = "office_confidential"
        original = str(text or "")
        mapping: Dict[str, str] = {}
        counts: Dict[str, int] = {}
        out = original

        def scrub_pattern(pattern: re.Pattern, prefix: str, category: str, *, group: int = 0) -> None:
            nonlocal out
            out = self._scrub_regex(out, pattern, prefix, mapping, counts, category, group=group)

        # Order matters: specific identifiers before broad telephone/number rules.
        scrub_pattern(_TW_ID_RE, "TWID", "tw_id")
        scrub_pattern(_TW_ARC_RE, "ARC", "resident_id")
        scrub_pattern(_PASSPORT_RE, "PASSPORT", "passport", group=1)
        scrub_pattern(_EMAIL_RE, "EMAIL", "email")
        scrub_pattern(_TW_MOBILE_RE, "MOBILE", "mobile")
        scrub_pattern(_TW_LANDLINE_RE, "PHONE", "landline")
        scrub_pattern(_BANK_ACCOUNT_RE, "ACCOUNT", "financial_account", group=1)
        scrub_pattern(_MEDICAL_ID_RE, "MEDICAL", "medical_id", group=1)
        scrub_pattern(_VEHICLE_ID_RE, "VEHICLE", "vehicle_id", group=1)
        scrub_pattern(_BIRTH_RE, "BIRTH", "birth_date", group=1)
        scrub_pattern(_ADDRESS_LABEL_RE, "ADDRESS", "address", group=1)
        scrub_pattern(_TW_ADDRESS_RE, "ADDRESS", "address")
        scrub_pattern(_LABELLED_ENTITY_RE, "ENTITY", "private_entity", group=1)
        scrub_pattern(_LAF_CASE_RE, "LAF", "laf_case")
        scrub_pattern(_INTERNAL_CASE_RE, "MATTER", "internal_case")
        if profile != "public_judgment":
            scrub_pattern(_COURT_CASE_RE, "COURTCASE", "court_case")
        if profile == "office_confidential":
            scrub_pattern(_URL_RE, "URL", "url")
            scrub_pattern(_IPV4_RE, "IP", "ip_address")

        for name in self.known_names:
            if name not in out:
                continue
            out, replacements = self._replace_literal(out, name, "PERSON", mapping)
            counts["known_name"] = counts.get("known_name", 0) + replacements
        out = self._scrub_role_names(out, mapping, counts)

        residuals = self.detect_residuals(out, profile=profile)
        needs_known_names = profile == "office_confidential" if require_known_names is None else bool(require_known_names)
        warnings: list[str] = []
        if needs_known_names and not self.known_names_verified:
            warnings.append("known_name_inventory_unavailable")
        if not original.strip():
            warnings.append("empty_payload")
        safe = bool(original.strip()) and not residuals and not warnings
        result = ScrubResult(
            scrubbed_text=out,
            mapping=mapping,
            counts={key: value for key, value in sorted(counts.items()) if value},
            safe_to_send=safe,
            residual_categories=tuple(residuals),
            warnings=tuple(warnings),
            profile=profile,
            original_sha256=hashlib.sha256(original.encode("utf-8")).hexdigest(),
            scrubbed_sha256=hashlib.sha256(out.encode("utf-8")).hexdigest(),
            known_names_verified=self.known_names_verified,
        )
        logger.info(
            "privacy scrub policy=%s profile=%s safe=%s categories=%s residual=%s source=%s",
            PRIVACY_POLICY_VERSION,
            profile,
            safe,
            sorted(result.counts),
            list(result.residual_categories),
            self.source,
        )
        return result

    def detect_residuals(self, text: str, *, profile: str = "office_confidential") -> list[str]:
        """Second, independent pass.  Only category names leave this function."""
        value = str(text or "")
        checks: list[tuple[str, re.Pattern]] = [
            ("tw_id", _TW_ID_RE),
            ("resident_id", _TW_ARC_RE),
            ("passport", _PASSPORT_RE),
            ("email", _EMAIL_RE),
            ("mobile", _TW_MOBILE_RE),
            ("landline", _TW_LANDLINE_RE),
            ("financial_account", _BANK_ACCOUNT_RE),
            ("medical_id", _MEDICAL_ID_RE),
            ("vehicle_id", _VEHICLE_ID_RE),
            ("birth_date", _BIRTH_RE),
            ("address", _ADDRESS_LABEL_RE),
            ("address", _TW_ADDRESS_RE),
            ("private_entity", _LABELLED_ENTITY_RE),
            ("laf_case", _LAF_CASE_RE),
            ("internal_case", _INTERNAL_CASE_RE),
        ]
        if profile != "public_judgment":
            checks.append(("court_case", _COURT_CASE_RE))
        if profile == "office_confidential":
            checks.extend((("url", _URL_RE), ("ip_address", _IPV4_RE)))
        residuals = [
            category
            for category, pattern in checks
            if self._pattern_has_unmasked_match(pattern, value)
        ]
        for name in self.known_names:
            if name and name in value:
                residuals.append("known_name")
                break
        if self._unmasked_role_name(value):
            residuals.append("labelled_name")
        return list(dict.fromkeys(residuals))

    @staticmethod
    def _pattern_has_unmasked_match(pattern: re.Pattern, text: str) -> bool:
        for match in pattern.finditer(text):
            captured = match.group(match.lastindex) if match.lastindex else match.group(0)
            if re.fullmatch(r"\[[A-Z]+-\d{3}\]", str(captured or "").strip()):
                continue
            return True
        return False

    @staticmethod
    def _placeholder(prefix: str, mapping: Dict[str, str], raw: str) -> str:
        for placeholder, original in mapping.items():
            if original == raw and placeholder.startswith(f"[{prefix}-"):
                return placeholder
        serial = 1 + sum(1 for key in mapping if key.startswith(f"[{prefix}-"))
        placeholder = f"[{prefix}-{serial:03d}]"
        mapping[placeholder] = raw
        return placeholder

    @classmethod
    def _replace_literal(
        cls, text: str, raw: str, prefix: str, mapping: Dict[str, str]
    ) -> tuple[str, int]:
        occurrences = text.count(raw)
        if not occurrences:
            return text, 0
        return text.replace(raw, cls._placeholder(prefix, mapping, raw)), occurrences

    @classmethod
    def _scrub_regex(
        cls,
        text: str,
        pattern: re.Pattern,
        prefix: str,
        mapping: Dict[str, str],
        counts: Dict[str, int],
        category: str,
        *,
        group: int = 0,
    ) -> str:
        def replace(match: re.Match) -> str:
            raw = match.group(group)
            placeholder = cls._placeholder(prefix, mapping, raw)
            counts[category] = counts.get(category, 0) + 1
            if group == 0:
                return placeholder
            start, end = match.span(group)
            rel_start = start - match.start(0)
            rel_end = end - match.start(0)
            whole = match.group(0)
            return whole[:rel_start] + placeholder + whole[rel_end:]

        return pattern.sub(replace, text)

    @classmethod
    def _scrub_role_names(
        cls, text: str, mapping: Dict[str, str], counts: Dict[str, int]
    ) -> str:
        def replace(match: re.Match) -> str:
            candidate = match.group("name")
            person = cls._role_person_part(candidate)
            if not person:
                return match.group(0)
            if not match.group("sep") and person[0] not in _COMMON_SURNAMES and not re.search(r"[○◯〇Ｏ某]", person):
                return match.group(0)
            placeholder = cls._placeholder("PERSON", mapping, person)
            counts["labelled_name"] = counts.get("labelled_name", 0) + 1
            return match.group(0).replace(person, placeholder, 1)

        return _ROLE_NAME_RE.sub(replace, text)

    @staticmethod
    def _unmasked_role_name(text: str) -> bool:
        for match in _ROLE_NAME_RE.finditer(text):
            candidate = match.group("name")
            person = PIIScrubber._role_person_part(candidate)
            if not person or person.startswith("MAGI"):
                continue
            if not match.group("sep") and person[0] not in _COMMON_SURNAMES and not re.search(r"[○◯〇Ｏ某]", person):
                continue
            if "[PERSON-" in match.group(0):
                continue
            return True
        return False

    @staticmethod
    def _role_person_part(candidate: str) -> str:
        value = str(candidate or "")
        if value in _ROLE_FALSE_NAMES:
            return ""
        for marker in _ROLE_FOLLOW_WORDS:
            index = value.find(marker)
            if index >= 2:
                value = value[:index]
                break
        if len(value) < 2 or len(value) > 4:
            return ""
        return value


def build_scrubber_from_magi_db(limit: int = 5000) -> PIIScrubber:
    """Load the local client-name inventory; failure is visible to the gate."""
    import os

    try:
        from api.db_helper import _default_config, get_cursor

        cfg = _default_config()
        cfg["database"] = os.environ.get("MAGI_CASES_DB_NAME", "law_firm_data")
        names: list[str] = []
        with get_cursor(config=cfg, dictionary=True) as (_conn, cur):
            cur.execute(
                "SELECT DISTINCT client_name FROM cases "
                "WHERE client_name IS NOT NULL AND TRIM(client_name) != '' "
                "ORDER BY updated_at DESC LIMIT %s",
                (int(limit),),
            )
            for row in cur.fetchall() or []:
                name = str((row or {}).get("client_name") or "").strip()
                if len(name) >= 2:
                    names.append(name)
        logger.info("privacy name inventory loaded count=%d", len(names))
        return PIIScrubber(
            known_names=names,
            known_names_verified=True,
            source="magi_db",
        )
    except Exception as exc:
        # Never include database configuration or query contents in the warning.
        logger.warning("privacy name inventory unavailable: %s", type(exc).__name__)
        return PIIScrubber(
            known_names=None,
            known_names_verified=False,
            source="magi_db_unavailable",
        )


def combine_privacy_certificates(results: Iterable[ScrubResult]) -> dict:
    """Aggregate non-sensitive evidence for multi-message requests."""
    items = list(results)
    counts: dict[str, int] = {}
    for item in items:
        for key, value in item.counts.items():
            counts[key] = counts.get(key, 0) + int(value)
    return {
        "policy_version": PRIVACY_POLICY_VERSION,
        "safe_to_send": bool(items) and all(item.safe_to_send for item in items),
        "counts": counts,
        "residual_categories": list(
            dict.fromkeys(category for item in items for category in item.residual_categories)
        ),
        "warnings": list(dict.fromkeys(warning for item in items for warning in item.warnings)),
        "payloads": len(items),
    }

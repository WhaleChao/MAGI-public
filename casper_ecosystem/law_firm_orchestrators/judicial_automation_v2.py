import logging
# -*- coding: utf-8 -*-
"""
Judicial Automation Module v2.0.0
司法院相關服務自動化整合模組

網站結構：
1. 律師單一登入 portal.ezlawyer.com.tw - SSO 入口
2. 電子筆錄調閱 www.ezlawyer.com.tw - 筆錄下載
3. 線上閱卷系統 eefile.judicial.gov.tw - 閱卷管理

Author: Claude (Anthropic)
Date: 2025-12
"""

import os
import errno
import re
import sys
import io
import time
import hashlib
import shutil
import json
import pickle
import base64
import tempfile
import threading
import traceback
import ssl
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field

import importlib.util
import urllib.parse
import urllib.request

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.case_path_mapper import local_case_path_candidates, translate_case_path_to_local
from api.runtime_paths import get_env_file, get_transcript_download_dir
from skills.engine.legal_web_adapter import (
    format_legal_web_engine_log,
    legal_web_allowed_hosts,
    preinstalled_selenium_driver_kwargs,
    resolve_legal_web_engine,
)


def _safe_print(*args, **kwargs) -> None:
    try:
        print(*args, **kwargs)
    except BrokenPipeError:
        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 49, exc_info=True)


def _safe_log_callback(callback, message: str) -> None:
    if not callback:
        return
    try:
        callback(message)
    except BrokenPipeError:
        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 58, exc_info=True)


# Shared browser helper (P2-5: consolidate duplicate _dismiss_password_expiry_alert)
try:
    _bh_spec = importlib.util.spec_from_file_location(
        "magi_lf_browser_helpers",
        Path(__file__).parent / "_browser_helpers.py",
    )
    _bh_mod = importlib.util.module_from_spec(_bh_spec)
    _bh_spec.loader.exec_module(_bh_mod)
    _shared_dismiss_alert = _bh_mod.dismiss_password_expiry_alert
except Exception:
    _shared_dismiss_alert = None

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(str(get_env_file()), override=False)
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 44, exc_info=True)

# ==============================================================================
# Safe file operations (never delete Synology Drive data)
# ==============================================================================
safe_remove = None
try:
    from safe_fs import safe_remove  # type: ignore
except Exception:
    # Fallback: import from the current orchestrator directory.
    try:
        _orch_dir = os.path.dirname(os.path.abspath(__file__))
        if _orch_dir and _orch_dir not in sys.path:
            sys.path.insert(0, _orch_dir)
        from safe_fs import safe_remove  # type: ignore
    except Exception:
        safe_remove = None

# ==============================================================================
# Human-in-the-loop CAPTCHA (no auto bypass)
# ==============================================================================

def _is_production_host(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return host not in {"127.0.0.1", "localhost", ""}
    except Exception:
        return True

# =============================================================================
# 依賴項 (Lazy Load Setup)
# =============================================================================

# Check availability
SELENIUM_AVAILABLE = importlib.util.find_spec("selenium") is not None
RAPIDOCR_AVAILABLE = importlib.util.find_spec("rapidocr_onnxruntime") is not None
PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None and importlib.util.find_spec("numpy") is not None
DDDDOCR_AVAILABLE = importlib.util.find_spec("ddddocr") is not None
GMAIL_AVAILABLE = importlib.util.find_spec("googleapiclient") is not None and \
                  importlib.util.find_spec("google_auth_oauthlib") is not None and \
                  importlib.util.find_spec("google.auth") is not None

# Placeholders
webdriver = None
Options = None
Service = None
By = None
WebDriverWait = None
EC = None
Select = None
ActionChains = None
Keys = None
TimeoutException = None
NoSuchElementException = None
ElementClickInterceptedException = None
StaleElementReferenceException = None
NoSuchFrameException = None

RapidOCR = None

Image = None
np = None

Credentials = None
InstalledAppFlow = None
Request = None
build = None

ddddocr = None



# ==============================================================================
# 全域協調機制 - 防止不同類別同時操作同一檔案
# ==============================================================================
_global_transcript_lock = threading.Lock()
_global_transcript_operation_in_progress = False


_TRANSCRIPT_PARSE_EMPTY_MARKERS = {
    "",
    "-",
    "--",
    "none",
    "null",
    "n/a",
    "na",
    "n.a.",
    "unknown",
    "未知",
    "無",
    "無法判讀",
    "無法辨識",
    "未提供",
}

# Judicial records stored with an active matter can legitimately predate the
# current case by many years (for example, a prior-instance or remanded-case
# record).  Requiring 2020 or later made those valid records permanently
# unnameable.  Safety comes from validating a real calendar date and requiring
# the date and transcript marker on the same page, not from an arbitrary recent
# year cutoff.
_TRANSCRIPT_MIN_YEAR = 1912
_TRANSCRIPT_MAX_YEAR = 2200


def _clean_transcript_parse_value(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip()


def _valid_transcript_record_date(value: Any) -> bool:
    text = _clean_transcript_parse_value(value)
    if text.lower() in _TRANSCRIPT_PARSE_EMPTY_MARKERS:
        return False
    if not re.fullmatch(r"\d{8}", text):
        return False
    if text == "00000000":
        return False
    try:
        parsed = datetime.strptime(text, "%Y%m%d")
    except ValueError:
        return False
    return _TRANSCRIPT_MIN_YEAR <= parsed.year <= _TRANSCRIPT_MAX_YEAR


def _valid_transcript_record_type(value: Any) -> bool:
    text = _clean_transcript_parse_value(value)
    if text.lower() in _TRANSCRIPT_PARSE_EMPTY_MARKERS:
        return False
    return "筆錄" in text


def _record_parse_ready_for_filename(parse_result: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(parse_result, dict):
        return False
    return _valid_transcript_record_date(parse_result.get("date")) and _valid_transcript_record_type(parse_result.get("type"))


def _transcript_filename_metadata_category(parse_result: Optional[Dict[str, Any]]) -> str:
    """Return a privacy-safe, actionable reason for an unsafe filename parse.

    The receipt deliberately contains no source name, path, case identifier, or
    extracted text.  Keeping the two required filename fields separate lets an
    operator distinguish parser coverage gaps without weakening the fail-closed
    naming rule.
    """

    result = parse_result if isinstance(parse_result, dict) else {}
    date_ready = _valid_transcript_record_date(result.get("date"))
    type_ready = _valid_transcript_record_type(result.get("type"))
    if not date_ready and not type_ready:
        return "metadata_date_and_type_unresolved"
    if not date_ready:
        return "metadata_date_unresolved"
    if not type_ready:
        return "metadata_type_unresolved"
    return "metadata_unresolved"


_TRANSCRIPT_RECORD_TYPES = (
    "準備程序筆錄",
    "言詞辯論筆錄",
    "審理程序筆錄",
    "宣示判決筆錄",
    "調解程序筆錄",
    "和解程序筆錄",
    "調查程序筆錄",
    "消債調查筆錄",
    "協商會議記錄",
    "審判筆錄",
    "訊問筆錄",
    "勘驗筆錄",
    "移交筆錄",
    "調查筆錄",
)


def _chinese_calendar_number(value: Any) -> Optional[int]:
    """Parse the small Chinese numerals used in ROC court-document dates."""

    text = re.sub(r"\s+", "", str(value or "")).translate(
        str.maketrans({"〇": "零", "○": "零", "Ｏ": "零"})
    )
    digits = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9}
    if not text or any(ch not in digits and ch not in {"十", "百"} for ch in text):
        return None
    if "百" in text:
        head, tail = text.split("百", 1)
        hundreds = digits.get(head, 1) if head else 1
        remainder = _chinese_calendar_number(tail) if tail else 0
        return hundreds * 100 + (remainder or 0)
    if "十" in text:
        head, tail = text.split("十", 1)
        tens = digits.get(head, 1) if head else 1
        ones = digits.get(tail, 0) if tail else 0
        return tens * 10 + ones
    try:
        return int("".join(str(digits[ch]) for ch in text))
    except (KeyError, ValueError):
        return None


def _extract_transcript_metadata_from_text_pages(page_texts: Any) -> Dict[str, Optional[str]]:
    """Extract filename metadata only when a single page proves it is a record.

    Some court PDFs place a cover sheet before the actual transcript.  Looking
    at up to three supplied pages closes that format gap, but the date and
    document type must occur on the same transcript page.  This prevents a date
    from an unrelated attachment from being combined with another page.
    """

    if not isinstance(page_texts, (list, tuple)):
        return {"date": None, "type": None, "period": "", "time": ""}
    numeral = "一二三四五六七八九十百〇零○Ｏ"
    for raw in page_texts[:3]:
        text = str(raw or "")
        compact = re.sub(r"\s+", "", text)
        record_type = next((kind for kind in _TRANSCRIPT_RECORD_TYPES if kind in compact), None)
        if record_type is None and "筆錄" in compact:
            record_type = "筆錄"
        if record_type is None:
            continue

        date_value: Optional[str] = None
        arabic = re.search(
            r"(?:中\s*華\s*)?民\s*國\s*([\d\s]{2,8})年\s*([\d\s]{1,5})月\s*([\d\s]{1,5})日",
            text,
        )
        if arabic:
            try:
                roc_year, month, day = (
                    int(re.sub(r"\s+", "", part)) for part in arabic.groups()
                )
                candidate = datetime(roc_year + 1911, month, day)
                if _TRANSCRIPT_MIN_YEAR <= candidate.year <= _TRANSCRIPT_MAX_YEAR:
                    date_value = candidate.strftime("%Y%m%d")
            except (TypeError, ValueError):
                date_value = None
        if date_value is None:
            chinese = re.search(
                rf"(?:中\s*華\s*)?民\s*國\s*([{numeral}\s]+)年\s*"
                rf"([{numeral}\s]+)月\s*([{numeral}\s]+)日",
                text,
            )
            if chinese:
                roc_year, month, day = (
                    _chinese_calendar_number(part) for part in chinese.groups()
                )
                try:
                    candidate = datetime(int(roc_year) + 1911, int(month), int(day))
                    if _TRANSCRIPT_MIN_YEAR <= candidate.year <= _TRANSCRIPT_MAX_YEAR:
                        date_value = candidate.strftime("%Y%m%d")
                except (TypeError, ValueError):
                    date_value = None
        if date_value is None:
            continue

        period = ""
        time_value = ""
        clock = re.search(
            r"(上\s*午|下\s*午)\s*([\d\s]{1,3})\s*(?:時|:)\s*([\d\s]{1,3})\s*分?",
            text,
        )
        if clock:
            period = re.sub(r"\s+", "", clock.group(1))
            try:
                hour = int(re.sub(r"\s+", "", clock.group(2)))
                minute = int(re.sub(r"\s+", "", clock.group(3)))
                if 1 <= hour <= 12 and 0 <= minute <= 59:
                    time_value = f"{hour:02d}{minute:02d}"
            except ValueError:
                time_value = ""
        else:
            chinese_clock = re.search(
                rf"(上\s*午|下\s*午)\s*([{numeral}\s]+)時\s*"
                rf"([{numeral}\s]+)分",
                text,
            )
            if chinese_clock:
                period = re.sub(r"\s+", "", chinese_clock.group(1))
                hour = _chinese_calendar_number(chinese_clock.group(2))
                minute = _chinese_calendar_number(chinese_clock.group(3))
                if hour is not None and minute is not None and 1 <= hour <= 12 and 0 <= minute <= 59:
                    time_value = f"{hour:02d}{minute:02d}"
        if not period and re.search(r"上\s*午", text):
            period = "上午"
        elif not period and re.search(r"下\s*午", text):
            period = "下午"
        return {
            "date": date_value,
            "type": record_type,
            "period": period,
            "time": time_value,
        }
    return {"date": None, "type": None, "period": "", "time": ""}


def _sanitize_transcript_parse_result(value: Any) -> Dict[str, Optional[str]]:
    """Normalize untrusted OCR/model/cache output before it reaches filenames.

    Old caches can contain plausible-looking but impossible or pre-system dates,
    and some providers return ``period=\"\u4e0a\u53480930\"`` together with
    ``time=\"0930\"``.  Keeping the validation at this shared boundary prevents
    poisoned cache entries from being logged or reused and avoids duplicated
    time fragments in generated filenames.
    """

    result: Dict[str, Optional[str]] = {
        "date": None,
        "type": None,
        "period": "",
        "time": "",
    }
    if not isinstance(value, dict):
        return result

    date_value = _clean_transcript_parse_value(value.get("date"))
    if _valid_transcript_record_date(date_value):
        result["date"] = date_value

    type_value = re.sub(r"\s+", "", str(value.get("type") or "")).strip()
    if _valid_transcript_record_type(type_value):
        result["type"] = type_value

    period_value = _clean_transcript_parse_value(value.get("period"))
    combined = re.fullmatch(r"(\u4e0a\u5348|\u4e0b\u5348)(\d{4})", period_value)
    if combined:
        result["period"] = combined.group(1)
        combined_time = combined.group(2)
        combined_hour = int(combined_time[:2])
        combined_minute = int(combined_time[2:])
        if 1 <= combined_hour <= 12 and 0 <= combined_minute <= 59:
            result["time"] = combined_time
    elif period_value in {"\u4e0a\u5348", "\u4e0b\u5348"}:
        result["period"] = period_value

    time_value = _clean_transcript_parse_value(value.get("time"))
    if re.fullmatch(r"\d{4}", time_value):
        hour = int(time_value[:2])
        minute = int(time_value[2:])
        if 1 <= hour <= 12 and 0 <= minute <= 59:
            if combined and result["time"] and result["time"] != time_value:
                result["time"] = ""
            else:
                result["time"] = time_value

    # A clock without an AM/PM period is not safe enough for a canonical name.
    if not result["period"]:
        result["time"] = ""
    return result


def _find_existing_transcript_folder_path(case_folder_path: str) -> Optional[str]:
    """Find an existing transcript directory without mutating the case tree."""

    if not case_folder_path or not os.path.isdir(case_folder_path):
        return None
    try:
        for item in os.listdir(case_folder_path):
            item_path = os.path.join(case_folder_path, item)
            if os.path.isdir(item_path) and "\u7b46\u9304" in item:
                return item_path
    except OSError:
        logging.getLogger(__name__).debug("transcript folder probe failed", exc_info=True)
    return None


def _is_real_pdf_filename(value: Any) -> bool:
    """Return true only for user-visible PDF files, never macOS sidecars.

    Synology/SMB folders can contain AppleDouble ``._*.pdf`` files.  They are
    metadata containers, not PDFs, even though their suffix is ``.pdf``.  All
    transcript inventory, hashing, parsing and rename entry points must share
    this predicate so a later phase cannot accidentally re-introduce them.
    """

    name = os.path.basename(str(value or ""))
    return bool(name) and not name.startswith(".") and name.lower().endswith(".pdf")


_PORTAL_DOCKET_PATTERN = re.compile(
    r"(?<!\d)(\d{2,3})(?:年度|[.．。])"
    r"([一-鿿A-Za-z]{1,16}?)(?:字|[.．。])"
    r"(?:第)?0*(\d{1,8})(?:號)?"
)


def _portal_docket_identities(text: Any) -> set[Tuple[str, str, int]]:
    """Return stable docket identities from formal or portal table text."""

    compact = re.sub(r"\s+", "", str(text or ""))
    identities: set[Tuple[str, str, int]] = set()
    for match in _PORTAL_DOCKET_PATTERN.finditer(compact):
        try:
            identities.add((str(int(match.group(1))), match.group(2), int(match.group(3))))
        except (TypeError, ValueError):
            continue
    return identities


def _portal_row_matches_case_text(row_text: Any, court_case_number: Any) -> bool:
    """Match a portal table row to one exact docket, including zero padding."""

    targets = _portal_docket_identities(court_case_number)
    return bool(targets and targets.intersection(_portal_docket_identities(row_text)))


def _transcript_text_matches_case(
    text: Any,
    court_case_number: Any,
    client_name: Any = "",
) -> bool:
    """Require an exact docket match before a transcript may be archived.

    Party names are intentionally not a fallback: the same person can have
    multiple cases, and OCR can expose a party name while losing the docket.
    Such files belong in quarantine until another reliable identifier exists.
    """

    body = str(text or "")
    body_dockets = _portal_docket_identities(body)
    target_dockets = _portal_docket_identities(court_case_number)
    return bool(body_dockets and target_dockets and target_dockets.intersection(body_dockets))


def _decode_transcript_model_payload(raw: Any) -> Optional[Dict[str, Any]]:
    """Decode one JSON object without trusting model formatting.

    Local vision/text models sometimes wrap otherwise valid JSON in Markdown or
    prepend a short explanation.  Accept the first real JSON object, but never
    evaluate Python literals or manufacture missing fields.
    """
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1 :]
        closing = text.rfind("```")
        if closing >= 0:
            text = text[:closing]
        text = text.strip()

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except (TypeError, ValueError, json.JSONDecodeError):
        logging.getLogger(__name__).debug(
            "vision payload is not a single JSON object; trying embedded object recovery"
        )

    decoder = json.JSONDecoder()
    for start, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[start:])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None

# ==============================================================================
# 資料結構
# ==============================================================================

@dataclass
class CourtCase:
    """案件資訊"""
    case_id: str = ""
    case_number: str = ""
    court_name: str = ""
    court_case_number: str = ""
    case_type: str = ""
    client_name: str = ""
    folder_path: str = ""


@dataclass
class FileReviewInfo:
    """閱卷資訊"""
    message_id: str = ""
    court: str = ""
    case_number: str = ""
    client_name: str = ""
    status: str = ""
    payment_amount: int = 0
    download_deadline: str = ""
    files: List[str] = field(default_factory=list)
    attachment_path: str = ""


# ==============================================================================
# 法院名稱對應表
# ==============================================================================

class CourtMapping:
    """法院名稱與代碼對應"""
    
    # 法院選項對應 (用於筆錄系統下拉選單)
    COURT_OPTIONS = {
        # 高等法院
        "臺灣高等法院": "TPH",
        "臺灣高等法院臺中分院": "TCH", 
        "臺灣高等法院臺南分院": "TNH",
        "臺灣高等法院高雄分院": "KSH",
        "臺灣高等法院花蓮分院": "HLH",
        
        # 地方法院
        "臺灣臺北地方法院": "TPD",
        "臺灣士林地方法院": "SLD",
        "臺灣新北地方法院": "PCD",
        "臺灣桃園地方法院": "TYD",
        "臺灣新竹地方法院": "SCD",
        "臺灣苗栗地方法院": "MLD",
        "臺灣臺中地方法院": "TCD",
        "臺灣南投地方法院": "NTD",
        "臺灣彰化地方法院": "CHD",
        "臺灣雲林地方法院": "ULD",
        "臺灣嘉義地方法院": "CYD",
        "臺灣臺南地方法院": "TND",
        "臺灣高雄地方法院": "KSD",
        "臺灣橋頭地方法院": "CTD",
        "臺灣屏東地方法院": "PTD",
        "臺灣臺東地方法院": "TTD",
        "臺灣花蓮地方法院": "HLD",
        "臺灣宜蘭地方法院": "ILD",
        "臺灣基隆地方法院": "KLD",
        "臺灣澎湖地方法院": "PHD",
        "福建金門地方法院": "KMD",
        "福建連江地方法院": "LCD",
    }
    
    # 簡易庭對應 (民事簡易案件)
    SIMPLE_COURT_MAPPING = {
        # 宜蘭地院
        "宜簡": ("宜蘭簡易庭", "ILS"),
        "羅簡": ("羅東簡易庭", "LTS"),
        # 新北地院
        "板簡": ("板橋簡易庭", "PCS"),
        "三簡": ("三重簡易庭", "SJS"),
        # 臺北地院  
        "北簡": ("臺北簡易庭", "TPS"),
        # 桃園地院
        "桃簡": ("桃園簡易庭", "TYS"),
        "壢簡": ("中壢簡易庭", "CLS"),
        # 新竹地院
        "竹簡": ("新竹簡易庭", "SCS"),
        "竹北簡": ("竹北簡易庭", "CBS"),
        # 苗栗地院
        "苗簡": ("苗栗簡易庭", "MLS"),
        # 臺中地院
        "中簡": ("臺中簡易庭", "TCS"),
        "沙簡": ("沙鹿簡易庭", "SLS"),
        "豐簡": ("豐原簡易庭", "FYS"),
        # 彰化地院
        "彰簡": ("彰化簡易庭", "CHS"),
        "員簡": ("員林簡易庭", "YLS"),
        # 南投地院
        "投簡": ("南投簡易庭", "NTS"),
        "埔簡": ("埔里簡易庭", "PLS"),
        # 雲林地院
        "雲簡": ("斗六簡易庭", "TLS"),
        "虎簡": ("虎尾簡易庭", "HWS"),
        # 嘉義地院
        "嘉簡": ("嘉義簡易庭", "CYS"),
        "朴簡": ("朴子簡易庭", "PZS"),
        # 臺南地院
        "南簡": ("臺南簡易庭", "TNS"),
        "新簡": ("新營簡易庭", "SYS"),
        "柳簡": ("柳營簡易庭", "LYS"),
        # 高雄地院
        "雄簡": ("高雄簡易庭", "KSS"),
        "鳳簡": ("鳳山簡易庭", "FSS"),
        "岡簡": ("岡山簡易庭", "GSS"),
        # 橋頭地院
        "橋簡": ("橋頭簡易庭", "CTS"),
        "旗簡": ("旗山簡易庭", "CSS"),
        # 屏東地院
        "屏簡": ("屏東簡易庭", "PTS"),
        "潮簡": ("潮州簡易庭", "CZS"),
        # 臺東地院
        "東簡": ("臺東簡易庭", "TTS"),
        # 花蓮地院
        "花簡": ("花蓮簡易庭", "HLS"),
        "玉簡": ("玉里簡易庭", "YUS"),
        # 基隆地院
        "基簡": ("基隆簡易庭", "KLS"),
        # 澎湖地院
        "澎簡": ("澎湖簡易庭", "PHS"),
        # 金門地院
        "金簡": ("金城簡易庭", "KMS"),
        # 連江地院
        "連簡": ("連江簡易庭", "LCS"),
    }
    
    @classmethod
    def get_court_code(cls, court_name: str) -> Optional[str]:
        """取得法院代碼"""
        if court_name in cls.COURT_OPTIONS:
            return cls.COURT_OPTIONS[court_name]
        
        for name, code in cls.COURT_OPTIONS.items():
            if name in court_name or court_name in name:
                return code
        return None
    
    @classmethod
    def get_simple_court(cls, case_number: str) -> Optional[Tuple[str, str]]:
        """根據案號判斷簡易庭"""
        for prefix, (name, code) in cls.SIMPLE_COURT_MAPPING.items():
            if prefix in case_number:
                return (name, code)
        return None
    
    @classmethod
    def is_civil_simple_case(cls, case_number: str) -> bool:
        """判斷是否為民事簡易案件"""
        return cls.get_simple_court(case_number) is not None


# ==============================================================================
# 驗證碼識別器
# ==============================================================================

class CaptchaSolver:
    """驗證碼識別器"""
    
    def __init__(self):
        self.ocr = None
        self.dddd_ocr = None
        
        # ★ 診斷輸出：確認模組可用性
        _safe_print(f"[CaptchaSolver-Judicial] DDDDOCR_AVAILABLE={DDDDOCR_AVAILABLE}, RAPIDOCR_AVAILABLE={RAPIDOCR_AVAILABLE}")
        
        # Lazy Load ddddocr
        if DDDDOCR_AVAILABLE:
            global ddddocr
            if ddddocr is None:
                try:
                    import ddddocr
                except ImportError:
                    logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 336, exc_info=True)

            if ddddocr:
                try:
                    # ★★★ Packaged App Fix: Handle common.onnx path in frozen environment ★★★
                    onnx_kwargs = {'show_ad': False}
                    
                    if getattr(sys, 'frozen', False):
                        # PyInstaller mode
                        base_path = getattr(sys, '_MEIPASS', os.path.dirname(sys.executable))
                        
                        # Try to find common.onnx in probable locations
                        possible_paths = [
                            os.path.join(base_path, 'ddddocr', 'common.onnx'),         # Standard collect
                            os.path.join(base_path, '_internal', 'ddddocr', 'common.onnx'), # _internal folder
                            os.path.join(base_path, 'common.onnx'),                    # Root
                        ]
                        
                        onnx_path = None
                        for p in possible_paths:
                            if os.path.exists(p):
                                onnx_path = p
                                break
                        
                        if onnx_path:
                            _safe_print(f"📦 [ddddocr-judicial] Found frozen model: {onnx_path}")
                            onnx_kwargs['import_onnx_path'] = onnx_path
                        else:
                            _safe_print(f"⚠️ [ddddocr-judicial] frozen model not found in: {possible_paths}")

                    from skills.engine.ocr.shared_runtime import get_shared_ddddocr

                    self.dddd_ocr = get_shared_ddddocr(
                        ddddocr.DdddOcr,
                        kwargs=onnx_kwargs,
                    )
                    # print("✅ ddddocr 初始化成功")
                except Exception as e:
                    logging.getLogger("judicial").warning("ddddocr 初始化失敗: %s", e)

        # Lazy Load RapidOCR (與 ddddocr 並行，雙引擎互補)
        if RAPIDOCR_AVAILABLE:
            try:
                from skills.engine.ocr.shared_runtime import get_shared_rapidocr

                self.ocr = get_shared_rapidocr()
            except Exception as e:
                logging.getLogger("judicial").warning("RapidOCR 初始化失敗: %s", e)
    
    def solve_from_element(self, driver, img_element) -> str:
        """從 Selenium 元素識別驗證碼（ddddocr + RapidOCR 雙引擎並行）"""
        try:
            img_data = img_element.screenshot_as_png
            if not img_data:
                return ""
            candidates = []
            if self.dddd_ocr:
                try:
                    res = re.sub(r'[^A-Za-z0-9]', '', self.dddd_ocr.classification(img_data))
                    if res:
                        candidates.append(res)
                except Exception as e:
                    logging.getLogger("judicial").warning("ddddocr 識別失敗: %s", e)
            if self.ocr:
                try:
                    from PIL import Image as _PIL_Image
                    import numpy as _np
                    img = _PIL_Image.open(io.BytesIO(img_data))
                    img_array = _np.array(img)
                    result, _ = self.ocr(img_array)
                    if result:
                        text = re.sub(r'[^A-Za-z0-9]', '', ''.join([item[1] for item in result]))
                        if text:
                            candidates.append(text)
                except Exception as e:
                    logging.getLogger("judicial").warning("RapidOCR 識別失敗: %s", e)
            if not candidates:
                return ""
            # 回傳字元數最多的結果
            candidates.sort(key=lambda s: len(s), reverse=True)
            return candidates[0]
        except Exception as e:
            logging.getLogger("judicial").warning("驗證碼識別失敗: %s", e)
            return ""
    
    def solve_from_url(self, driver, captcha_url: str) -> str:
        """從 URL 下載並識別驗證碼"""
        try:
            import requests
            
            # 取得 cookies
            cookies = {c['name']: c['value'] for c in driver.get_cookies()}
            
            response = requests.get(captcha_url, cookies=cookies, timeout=10)
            
            if response.status_code != 200:
                return ""
            
            # 1. Try ddddocr
            if self.dddd_ocr:
                try:
                    res = self.dddd_ocr.classification(response.content)
                    res = re.sub(r'[^A-Za-z0-9]', '', res)
                    return res
                except Exception as e:
                    logging.getLogger("judicial").warning("ddddocr 識別失敗: %s", e)

            if not self.ocr:
                return ""
            
            # 2. RapidOCR Fallback
            img = Image.open(io.BytesIO(response.content))
            img_array = np.array(img)
            
            result, _ = self.ocr(img_array)
            
            if result:
                text = ''.join([item[1] for item in result])
                text = re.sub(r'[^A-Za-z0-9]', '', text)
                return text
            
            return ""
            
        except Exception as e:
            logging.getLogger("judicial").warning("驗證碼識別失敗: %s", e)
            return ""


# ==============================================================================
# 律師單一登入 (SSO)
# ==============================================================================

class LawyerSSO:
    """
    律師單一登入系統
    portal.ezlawyer.com.tw
    """
    
    LOGIN_URL = "https://portal.ezlawyer.com.tw/Login.do?gotoLogin=Y"
    
    def __init__(self, username: str, password: str, 
                 headless: bool = True, 
                 log_callback=None):
        self.username = username
        self.password = password
        self.headless = headless
        self.log_callback = log_callback
        
        self.driver = None
        self.logged_in = False
        self.captcha_solver = CaptchaSolver()
        self.web_engine_profile = resolve_legal_web_engine("judicial_sso_v2", interactive_required=True)
        self._engine_logged = False
    
    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{timestamp}] [SSO] {message}"
        _safe_print(full_msg)
        _safe_log_callback(self.log_callback, full_msg)
    
    def _setup_driver(self):
        """設定 WebDriver（Playwright 優先，Selenium 回退）"""
        # global 宣告必須在任何賦值前
        global webdriver, Options, By, WebDriverWait, EC, ActionChains, Select
        global TimeoutException, NoSuchElementException, Keys, StaleElementReferenceException

        if not self._engine_logged:
            self.log(format_legal_web_engine_log(self.web_engine_profile))
            self._engine_logged = True

        use_playwright = (
            os.environ.get("MAGI_TRANSCRIPT_WEB_ENGINE", "playwright").strip().lower() != "selenium"
        )

        if use_playwright:
            try:
                from skills.engine.playwright_wrapper import (
                    create_playwright_driver,
                    By as _By, Keys as _Keys, PlaywrightSelect as _Select,
                    WebDriverWait as _WDW, EC as _EC, PlaywrightActionChains as _AC,
                    TimeoutException as _TE, NoSuchElementException as _NSE,
                    StaleElementReferenceException as _SERE,
                )
                By = _By; Keys = _Keys; Select = _Select
                WebDriverWait = _WDW; EC = _EC; ActionChains = _AC
                TimeoutException = _TE; NoSuchElementException = _NSE
                StaleElementReferenceException = _SERE

                page_timeout = float(os.environ.get("MAGI_WEB_PAGELOAD_TIMEOUT_SEC", "45") or "45")
                dl_dir = os.path.abspath("./downloads")
                self.driver = create_playwright_driver(
                    headless=self.headless,
                    download_dir=dl_dir,
                    page_load_timeout=page_timeout,
                    allowed_navigation_hosts=list(
                        legal_web_allowed_hosts(self.web_engine_profile, extra_urls=(self.LOGIN_URL,))
                    ),
                )
                self.log("✅ Playwright Chromium 初始化成功（筆錄模組）")
                return
            except Exception as _pw_err:
                self.log(f"  ⚠️ Playwright 初始化失敗，回退到 Selenium: {_pw_err}")

        if not SELENIUM_AVAILABLE:
            raise ImportError("Selenium 未安裝，且 Playwright 不可用或已停用")

        if webdriver is None:
            from selenium import webdriver
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.keys import Keys
            from selenium.webdriver.support.ui import WebDriverWait, Select
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.common.action_chains import ActionChains
            from selenium.common.exceptions import TimeoutException, NoSuchElementException

        options = Options()
        options.page_load_strategy = 'eager'
        if self.headless:
            options.add_argument('--headless=new')

        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')

        prefs = {
            "download.default_directory": os.path.abspath("./downloads"),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "plugins.always_open_pdf_externally": True
        }
        options.add_experimental_option("prefs", prefs)

        self.driver = webdriver.Chrome(
            options=options,
            **preinstalled_selenium_driver_kwargs("chrome"),
        )

        try:
            page_timeout = int(os.environ.get("MAGI_SELENIUM_PAGELOAD_TIMEOUT_SEC", "45") or "45")
            script_timeout = int(os.environ.get("MAGI_SELENIUM_SCRIPT_TIMEOUT_SEC", "45") or "45")
            self.driver.set_page_load_timeout(page_timeout)
            self.driver.set_script_timeout(script_timeout)
        except Exception as e:
            self.log(f"  ⚠️ 設定 Selenium timeout 失敗(可忽略): {e}")

        self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': 'Object.defineProperty(navigator, "webdriver", {get: () => undefined})'
        })

        self.driver.implicitly_wait(10)
    
    def login(self, max_retries: int = 3) -> bool:
        """登入律師單一登入系統"""
        # 策略：筆錄調閱站常見情況是「不填驗證碼也能登入」。
        # 預設先不碰驗證碼（避免刷新造成欄位清空/stale），只有在系統明確提示需要驗證碼時才啟用 OCR。
        need_captcha = os.environ.get("MAGI_EZLAWYER_ASSUME_CAPTCHA_REQUIRED", "0").strip().lower() in {"1", "true", "yes", "on"}
        force_solve = os.environ.get("MAGI_EZLAWYER_SOLVE_CAPTCHA", "0").strip().lower() in {"1", "true", "yes", "on"}

        for attempt in range(max_retries):
            try:
                if not self.driver:
                    self._setup_driver()
                
                self.log(f"正在登入 (第 {attempt + 1} 次嘗試)...")
                # Opt-in one-shot flag: page load may trigger password-expiry alert;
                # we read it synchronously via _dismiss_password_expiry_alert().
                self.driver._next_dialog_no_dismiss = True
                try:
                    _page = getattr(self.driver, "_page", None)
                    if _page is not None:
                        _page.goto(self.LOGIN_URL, timeout=15000)
                    else:
                        self.driver.get(self.LOGIN_URL)
                except Exception as _ne:
                    self.log(f"  ⚠️ 登入頁面導航逾時/失敗: {_ne}")
                    continue
                time.sleep(2)

                # 頁面載入後先清一次 alert（密碼到期警告可能在頁面載入時就彈出）
                self._dismiss_password_expiry_alert()

                # 等待頁面載入
                WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "form"))
                )

                # 尋找表單元素（根據實際頁面結構）
                # 帳號是第一個 form-control，密碼是第二個
                form_inputs = self.driver.find_elements(
                    By.CSS_SELECTOR, "input.form-control"
                )

                if len(form_inputs) >= 2:
                    username_field = form_inputs[0]  # 第一個 input
                    password_field = form_inputs[1]  # 第二個 input
                else:
                    # 備用選擇器
                    username_field = self._find_element([
                        (By.CSS_SELECTOR, "input[type='text']"),
                        (By.NAME, "account"),
                        (By.NAME, "userId"),
                    ])
                    password_field = self._find_element([
                        (By.CSS_SELECTOR, "input[type='password']"),
                        (By.NAME, "password"),
                    ])

                if not username_field or not password_field:
                    self.log("找不到帳號或密碼欄位")
                    continue
                
                # 輸入帳號密碼
                username_field.clear()
                username_field.send_keys(self.username)
                time.sleep(0.5)
                
                password_field.clear()
                password_field.send_keys(self.password)
                time.sleep(0.5)
                
                # 處理驗證碼（含重刷機制）
                captcha_field = self._find_element([
                    (By.NAME, "checkCode"),
                    (By.NAME, "captcha"),
                    (By.NAME, "verifyCode"),
                    (By.ID, "checkCode"),
                    (By.CSS_SELECTOR, "input.form-control:nth-of-type(3)"),  # 第三個 form-control
                ])
                
                if captcha_field:
                    # 找到驗證碼圖片
                    captcha_img = self._find_element([
                        (By.ID, "captcha"),
                        (By.CSS_SELECTOR, "img#captcha"),
                        (By.CSS_SELECTOR, "img[src*='Captcha']"),
                        (By.CSS_SELECTOR, "img[src*='captcha']"),
                    ])
                    
                    # 找到重新產生按鈕
                    refresh_btn = self._find_element([
                        (By.XPATH, "//a[contains(text(), '重新產生')]"),
                        (By.XPATH, "//a[contains(@href, '#') and contains(text(), '產生')]"),
                    ])
                    
                    captcha_text = ""
                    max_captcha_retries = 5
                    
                    for captcha_try in range(max_captcha_retries):
                        if captcha_img:
                            # 使用 RapidOCR 識別
                            captcha_text = self.captcha_solver.solve_from_element(self.driver, captcha_img)
                            
                            # 驗證碼應該是6位數字
                            if captcha_text and len(captcha_text) >= 6:
                                # 只取數字
                                captcha_text = re.sub(r'[^0-9]', '', captcha_text)
                                if len(captcha_text) >= 6:
                                    captcha_text = captcha_text[:6]  # 只取前6位
                                    self.log(f"識別驗證碼: {captcha_text}")
                                    break
                        
                        # 識別失敗或不足6位，點刷新
                        self.log(f"  驗證碼不清楚 (第 {captcha_try+1} 次)，重新產生...")
                        if refresh_btn:
                            try:
                                refresh_btn.click()
                                time.sleep(1.5)
                                # 重新取得驗證碼圖片元素
                                captcha_img = self._find_element([
                                    (By.ID, "captcha"),
                                    (By.CSS_SELECTOR, "img#captcha"),
                                ])
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 636, exc_info=True)
                    
                    if captcha_text and len(captcha_text) >= 6:
                        captcha_field.clear()
                        captcha_field.send_keys(captcha_text)
                    else:
                        self.log("⚠️ 驗證碼識別失敗，請手動輸入")
                        if not self.headless:
                            input("請手動輸入驗證碼後按 Enter 繼續...")
                
                import random
                time.sleep(random.uniform(0.5, 1.5))  # 隨機延遲模擬人類
                
                # 點擊登入按鈕
                login_btn = self._find_element([
                    (By.CSS_SELECTOR, "button[title='會員登入']"),  # 優先使用 title
                    (By.CSS_SELECTOR, "button.btn-primary"),
                    (By.XPATH, "//button[contains(text(), '會員登入')]"),
                    (By.XPATH, "//button[contains(text(), '登入')]"),
                    (By.XPATH, "//button[@type='submit']"),
                ])
                
                if login_btn:
                    time.sleep(random.uniform(0.3, 0.8))  # 點擊前再等一下
                    # Opt-in one-shot flag: login submit may raise password-expiry alert.
                    self.driver._next_dialog_no_dismiss = True
                    login_btn.click()
                else:
                    # 嘗試用 Enter 提交
                    self.driver._next_dialog_no_dismiss = True
                    password_field.send_keys(Keys.RETURN)
                
                time.sleep(random.uniform(2.5, 4))

                # 按鈕點擊後再清一次 alert（密碼到期警告也可能在送出後才彈）
                self._dismiss_password_expiry_alert()

                # 檢查登入結果
                if self._check_login_success():
                    self.logged_in = True
                    self.log("✅ 登入成功")
                    return True
                else:
                    error_msg = self._get_error_message()
                    self.log(f"登入失敗: {error_msg}")

                    # 如果是驗證碼錯誤，重試
                    if "驗證碼" in error_msg or "captcha" in error_msg.lower():
                        self.log("驗證碼錯誤，重新嘗試...")
                        continue

            except Exception as e:
                self.log(f"登入異常: {e}")
                traceback.print_exc()
                # driver.get() timeout 後 driver 可能進入不穩狀態；直接重建以提升成功率
                try:
                    if self.driver:
                        self.driver.quit()
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 689, exc_info=True)
                self.driver = None
        
        return False
    
    def _dismiss_password_expiry_alert(self, wait_sec: float = 2.0) -> bool:
        """
        攔截並接受「密碼到期建議更改」alert。
        使用 WebDriverWait 等待 alert 出現（最多 wait_sec 秒）。
        回傳 True 表示有 alert 被處理，False 表示無 alert 或逾時。
        """
        if _shared_dismiss_alert is not None:
            return _shared_dismiss_alert(self.driver, log_fn=self.log, wait_sec=wait_sec)
        # Fallback: inline implementation with WebDriverWait
        try:
            if WebDriverWait and EC:
                try:
                    WebDriverWait(self.driver, wait_sec).until(EC.alert_is_present())
                except Exception:
                    return False
            al = self.driver.switch_to.alert
            alert_text = al.text
            self.log(f"  ⚠️ 發現 Alert: {alert_text}")
            al.accept()
            if ("建議" in alert_text or "未更新" in alert_text or "未變更" in alert_text) and "密碼" in alert_text:
                self.log("  (Alert 為密碼到期警告，已接受，繼續登入)")
            else:
                self.log(f"  (Alert 已接受: {alert_text[:80]})")
            return True
        except Exception:
            return False

    def _find_element(self, selectors: List[Tuple]) -> Optional[Any]:
        """嘗試多種選擇器尋找元素"""
        for by, value in selectors:
            try:
                element = self.driver.find_element(by, value)
                if element and element.is_displayed():
                    return element
            except NoSuchElementException:
                continue
        return None
    
    def _check_login_success(self) -> bool:
        """檢查是否登入成功"""
        try:
            # 檢查 URL 是否變更
            if "Login" not in self.driver.current_url:
                return True
            
            # 檢查是否有登出連結
            logout_link = self._find_element([
                (By.XPATH, "//a[contains(text(), '登出')]"),
                (By.XPATH, "//a[contains(@href, 'logout')]"),
            ])
            if logout_link:
                return True
            
            # 檢查是否有錯誤訊息
            if self._get_error_message():
                return False
            
            return False
            
        except Exception:
            return False
    
    def _get_error_message(self) -> str:
        """取得錯誤訊息"""
        try:
            error_elements = self.driver.find_elements(By.CSS_SELECTOR, ".alert-danger, .error, .text-danger")
            for elem in error_elements:
                text = elem.text.strip()
                if text:
                    return text
            return ""
        except Exception:
            return ""
    
    def navigate_to(self, target: str) -> bool:
        """導航到指定服務"""
        if not self.logged_in:
            self.log("尚未登入")
            return False
        
        try:
            targets = {
                "record": "https://www.ezlawyer.com.tw/",
                "eefile": "https://eefile.judicial.gov.tw/",
            }
            
            if target in targets:
                try:
                    _page = getattr(self.driver, "_page", None)
                    if _page is not None:
                        _page.goto(targets[target], timeout=15000)
                    else:
                        self.driver.get(targets[target])
                except Exception as _ne:
                    self.log(f"  ⚠️ 導航到 {target} 逾時/失敗: {_ne}")
                    return False
                time.sleep(2)
                return True
            
            return False
            
        except Exception as e:
            self.log(f"導航失敗: {e}")
            return False
    
    def close(self):
        """關閉瀏覽器"""
        if self.driver:
            self.driver.quit()
            self.driver = None
            self.logged_in = False


# ==============================================================================
# 電子筆錄調閱
# ==============================================================================

class CourtRecordDownloader:
    """
    電子筆錄調閱服務
    www.ezlawyer.com.tw
    
    工作流程：
    1. 登入 (不需要驗證碼)
    2. 進入「電子筆錄調閱」頁面
    3. 輸入案件資訊（法院、類別、年度、字別、案號）
    4. 點選查詢
    5. 對每個可以「立即調閱」的結果點選進入
    6. 選擇「下載PDF」連結下載
    7. 將筆錄存到案件的「筆錄」資料夾
    """
    
    BASE_URL = "https://www.ezlawyer.com.tw"
    LOGIN_URL = "https://www.ezlawyer.com.tw/eb/login/loginPage"
    SEARCH_URL = "https://www.ezlawyer.com.tw/eb/user/downloadEB"
    
    # 案件類別對應
    CASE_TYPE_MAP = {
        '刑事': '刑事',
        '簡易刑事': '刑事',  # 刑事簡易
        '民事': '民事',
        '簡易民事': '民事',  # 民事簡易
        '家事': '家事',
        '行政': '行政',
        '少年': '少年',
    }
    
    def __init__(self, username: str, password: str,
                 db_manager=None,
                 download_folder: str | None = None,
                 headless: bool = True,
                 log_callback=None):
        self.username = username
        self.password = password
        self.db = db_manager
        resolved_download_folder = (
            Path(download_folder).expanduser().resolve()
            if download_folder
            else get_transcript_download_dir()
        )
        self.download_folder = str(resolved_download_folder)
        self.md5_record_file = os.path.join(self.download_folder, '.downloaded_files.json')
        self.headless = headless
        self.log_callback = log_callback
        
        self.driver = None
        self.logged_in = False
        self.web_engine_profile = resolve_legal_web_engine("judicial_transcript_v2", interactive_required=True)
        self._engine_logged = False
        self._last_pdf_fetch_count = 0
        self._last_pdf_known_duplicate_count = 0
        self._last_no_new_files_reason = ""
        self._last_download_error = ""
        self.last_login_error_code = ""
        self.last_login_error_detail = ""

        # ★ Cookie 持久化：成功登入後存檔，下次直接沿用 session
        _runtime_dir = (
            os.environ.get("MAGI_RUNTIME_DIR", "").strip()
            or os.path.join(
                os.path.dirname(
                    os.path.dirname(
                        os.path.dirname(os.path.abspath(__file__))
                    )
                ),
                ".runtime",
            )
        )
        os.makedirs(_runtime_dir, exist_ok=True)
        self._session_cookie_file = os.path.join(_runtime_dir, "ezlawyer_transcript_session.json")

        # ★ Gemini 解析快取（避免重複調用 API）
        self.gemini_cache_file = os.path.join(self.download_folder, '.gemini_parse_cache.json')

        self.gemini_cache = self._load_gemini_cache()
        os.makedirs(self.download_folder, exist_ok=True)

        import atexit
        atexit.register(self._quit_driver)

    def _quit_driver(self):
        """確保 Chrome driver 在進程結束時一定被清理。"""
        driver = getattr(self, "driver", None)
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 964, exc_info=True)
            self.driver = None

    def __del__(self):
        self._quit_driver()

    def _save_session_cookies(self):
        """登入成功後把 cookies 存到磁碟，供下次直接沿用 session。"""
        try:
            if not self.driver:
                return
            # 嘗試透過 Playwright context 取得 cookies
            _ctx = getattr(self.driver, "_context", None)
            if _ctx is not None:
                cookies = _ctx.cookies()
            else:
                cookies = self.driver.get_cookies()
            if not cookies:
                return
            import json as _json
            with open(self._session_cookie_file, "w", encoding="utf-8") as _f:
                _json.dump({"cookies": cookies, "ts": time.time()}, _f, ensure_ascii=False)
            self.log(f"  ✅ Session cookies 已存檔（{len(cookies)} 筆）")
        except Exception as _e:
            self.log(f"  ⚠️ 存 session cookies 失敗（非致命）: {_e}")

    def _restore_session_cookies(self) -> bool:
        """嘗試從磁碟讀取 cookies 並注入瀏覽器，若 session 仍有效則跳過登入。"""
        try:
            import json as _json
            if not os.path.isfile(self._session_cookie_file):
                return False
            with open(self._session_cookie_file, "r", encoding="utf-8") as _f:
                data = _json.load(_f)
            # Cookie 超過 8 小時視為過期
            if time.time() - data.get("ts", 0) > 8 * 3600:
                self.log("  ℹ️ Session cookies 已過期（>8h），重新登入")
                return False
            cookies = data.get("cookies", [])
            if not cookies:
                return False
            # 先導航到目標 domain 才能注入 cookies
            _ctx = getattr(self.driver, "_context", None)
            if _ctx is not None:
                try:
                    _ctx.add_cookies(cookies)
                except Exception as _ce:
                    self.log(f"  ⚠️ PW context add_cookies 失敗: {_ce}")
                    return False
            else:
                # Fallback: Selenium-style add_cookie
                self.driver.get("https://www.ezlawyer.com.tw/eb/login/loginPage")
                for c in cookies:
                    try:
                        self.driver.add_cookie(c)
                    except Exception:
                        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 1020, exc_info=True)
            self.log(f"  ℹ️ Session cookies 已注入（{len(cookies)} 筆），驗證中...")
            # 導航到已登入頁面確認（15s timeout 避免 stale cookies 造成無限 hang）
            _nav_ok = False
            try:
                _page = getattr(self.driver, "_page", None)
                if _page is not None:
                    _page.goto("https://www.ezlawyer.com.tw/eb/user/userPage", timeout=15000)
                else:
                    self.driver.get("https://www.ezlawyer.com.tw/eb/user/userPage")
                _nav_ok = True
            except Exception as _ne:
                self.log(f"  ⚠️ Session 驗證導航逾時/失敗: {_ne}，將改走完整登入流程")
                _nav_ok = False
            if not _nav_ok:
                try:
                    if _ctx is not None and hasattr(_ctx, "clear_cookies"):
                        _ctx.clear_cookies()
                except Exception:
                    logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 1039, exc_info=True)
                return False
            time.sleep(2)
            if self._has_logout_link():
                self.log("  ✅ Session 有效，跳過登入流程")
                return True
            self.log("  ℹ️ Session 已失效，清除 cookies 並重新登入")
            # 清除失效 cookies 避免殘留狀態干擾登入重試
            try:
                if _ctx is not None and hasattr(_ctx, "clear_cookies"):
                    _ctx.clear_cookies()
                else:
                    try:
                        self.driver.delete_all_cookies()
                    except Exception:
                        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 1054, exc_info=True)
            except Exception:
                logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 1056, exc_info=True)
            return False
        except Exception as _e:
            self.log(f"  ⚠️ 還原 session cookies 失敗（非致命）: {_e}")
            return False

    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{timestamp}] [筆錄] {message}"
        _safe_print(full_msg)
        _safe_log_callback(self.log_callback, full_msg)
    
    def _setup_driver(self):
        """設置 WebDriver（Playwright 優先，Selenium 回退；含反爬蟲措施）"""
        # global 宣告必須在任何賦值前
        global webdriver, Options, By, WebDriverWait, EC, ActionChains, Select
        global TimeoutException, NoSuchElementException, Keys, StaleElementReferenceException

        if not self._engine_logged:
            self.log(format_legal_web_engine_log(self.web_engine_profile))
            self._engine_logged = True
        self.log("  正在設置 WebDriver...")

        use_playwright = (
            os.environ.get("MAGI_TRANSCRIPT_WEB_ENGINE", "playwright").strip().lower() != "selenium"
        )

        try:
            if use_playwright:
                from skills.engine.playwright_wrapper import (
                    create_playwright_driver,
                    By as _By, Keys as _Keys, PlaywrightSelect as _Select,
                    WebDriverWait as _WDW, EC as _EC, PlaywrightActionChains as _AC,
                    TimeoutException as _TE, NoSuchElementException as _NSE,
                    StaleElementReferenceException as _SERE,
                )
                By = _By; Keys = _Keys; Select = _Select
                WebDriverWait = _WDW; EC = _EC; ActionChains = _AC
                TimeoutException = _TE; NoSuchElementException = _NSE
                StaleElementReferenceException = _SERE

                page_timeout = float(os.environ.get("MAGI_WEB_PAGELOAD_TIMEOUT_SEC", "45") or "45")
                self.driver = create_playwright_driver(
                    headless=self.headless,
                    download_dir=self.download_folder,
                    page_load_timeout=page_timeout,
                    allowed_navigation_hosts=list(
                        legal_web_allowed_hosts(
                            self.web_engine_profile,
                            extra_urls=(self.LOGIN_URL, self.SEARCH_URL),
                        )
                    ),
                )
                self.log("✅ Playwright Chromium 初始化成功（筆錄 CourtRecordDownloader）")
                # 初始化驗證碼識別器
                self.captcha_solver = CaptchaSolver()
                return
        except Exception as _pw_err:
            self.log(f"  ⚠️ Playwright 初始化失敗，回退到 Selenium: {_pw_err}")

        # ---- Selenium fallback ----
        if not SELENIUM_AVAILABLE:
            raise ImportError("Selenium 未安裝，且 Playwright 不可用或已停用")

        import random

        if webdriver is None:
            from selenium import webdriver
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.keys import Keys
            from selenium.webdriver.support.ui import WebDriverWait, Select
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.common.action_chains import ActionChains
            from selenium.common.exceptions import TimeoutException, NoSuchElementException

        options = Options()
        options.page_load_strategy = 'eager'

        if self.headless:
            options.add_argument('--headless=new')

        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        ]
        options.add_argument(f'--user-agent={random.choice(user_agents)}')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1280,800')

        prefs = {
            "download.default_directory": self.download_folder,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "plugins.always_open_pdf_externally": True
        }
        options.add_experimental_option("prefs", prefs)

        self.driver = webdriver.Chrome(
            options=options,
            **preinstalled_selenium_driver_kwargs("chrome"),
        )
        self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        try:
            # Headless Chrome sometimes ignores download.default_directory for
            # form/JS-triggered downloads unless CDP download behavior is enabled.
            self.driver.execute_cdp_cmd(
                "Page.setDownloadBehavior",
                {"behavior": "allow", "downloadPath": self.download_folder},
            )
            self.log(f"  ✓ Selenium headless 下載路徑已啟用: {self.download_folder}")
        except Exception as e:
            self.log(f"  ⚠️ 設定 Selenium CDP 下載路徑失敗(可忽略): {e}")

        try:
            page_timeout = int(os.environ.get("MAGI_SELENIUM_PAGELOAD_TIMEOUT_SEC", "45") or "45")
            script_timeout = int(os.environ.get("MAGI_SELENIUM_SCRIPT_TIMEOUT_SEC", "45") or "45")
            self.driver.set_page_load_timeout(page_timeout)
            self.driver.set_script_timeout(script_timeout)
        except Exception as e:
            self.log(f"  ⚠️ 設定 Selenium timeout 失敗(可忽略): {e}")

        self.driver.implicitly_wait(5)

        # 初始化驗證碼識別器
        self.captcha_solver = CaptchaSolver()
        self.log("  ✓ WebDriver 設置完成")

    def _random_delay(self, min_sec: float = 0.5, max_sec: float = 2.0):
        """隨機延遲（模擬人類行為）"""
        import random
        delay = random.uniform(min_sec, max_sec)
        time.sleep(delay)

    def _dismiss_password_expiry_alert(self, wait_sec: float = 2.0) -> bool:
        """接受密碼到期警告 alert，使用 WebDriverWait 等待 alert 出現（最多 wait_sec 秒）。"""
        if _shared_dismiss_alert is not None:
            return _shared_dismiss_alert(self.driver, log_fn=self.log, wait_sec=wait_sec)
        # Fallback inline
        try:
            if WebDriverWait and EC:
                try:
                    WebDriverWait(self.driver, wait_sec).until(EC.alert_is_present())
                except Exception:
                    return False
            al = self.driver.switch_to.alert
            alert_text = al.text
            self.log(f"  ⚠️ 發現 Alert: {alert_text}")
            al.accept()
            if ("建議" in alert_text or "未更新" in alert_text or "未變更" in alert_text) and "密碼" in alert_text:
                self.log("  (Alert 為密碼到期警告，已接受，繼續登入)")
            else:
                self.log(f"  (Alert 已接受: {alert_text[:80]})")
            return True
        except Exception:
            return False

    def login(self, max_retries: int = 3) -> bool:
        """
        登入 ezlawyer.com.tw
        包含驗證碼 OCR 識別和反爬蟲措施
        """
        # ★ 如果已經登入，直接回傳 True，避免重複登入流程
        if self.logged_in:
            return True
        self.last_login_error_code = ""
        self.last_login_error_detail = ""

        # 策略：筆錄調閱站常見情況是「不填驗證碼也能登入」。
        # 預設先不碰驗證碼（避免刷新造成欄位清空/stale），只有在系統明確提示需要驗證碼時才啟用 OCR。
        need_captcha = os.environ.get("MAGI_EZLAWYER_ASSUME_CAPTCHA_REQUIRED", "0").strip().lower() in {"1", "true", "yes", "on"}
        force_solve = os.environ.get("MAGI_EZLAWYER_SOLVE_CAPTCHA", "0").strip().lower() in {"1", "true", "yes", "on"}

        # ★ 先嘗試從磁碟還原 session（避免重複登入觸發 IP 鎖定）
        if not self.driver:
            self._setup_driver()
        if self._restore_session_cookies():
            self.logged_in = True
            return True

        for attempt in range(max_retries):
            try:
                if not self.driver:
                    self._setup_driver()

                self.log(f"正在登入 ezlawyer.com.tw... (第 {attempt + 1} 次嘗試)")

                # 隨機延遲避免被偵測
                self._random_delay(1, 3)

                try:
                    _page = getattr(self.driver, "_page", None)
                    if _page is not None:
                        _page.goto(self.LOGIN_URL, timeout=15000)
                    else:
                        self.driver.get(self.LOGIN_URL)
                except Exception as _ne:
                    self.log(f"  ⚠️ 登入頁面導航逾時/失敗: {_ne}")
                    continue
                self._random_delay(2, 4)

                # ★ 檢查是否已經登入 (可能被重導向到首頁)
                if self._has_logout_link():
                    self.log("  ℹ️ 偵測到已登入狀態")
                    self.logged_in = True
                    self._save_session_cookies()
                    return True

                # ★ 顯式等待 #j_username 出現（最多 15 秒），避免 JS-rendered 表單時序競爭
                if WebDriverWait and EC:
                    try:
                        WebDriverWait(self.driver, 15).until(
                            EC.presence_of_element_located((By.ID, "j_username"))
                        )
                    except Exception as _wait_err:
                        self.log(f"  ⚠️ WebDriverWait #j_username 逾時: {_wait_err}")

                # 找到帳號欄位
                username_field = None
                try:
                    username_field = self.driver.find_element(By.ID, "j_username")
                except NoSuchElementException:
                    try:
                        username_field = self.driver.find_element(By.CSS_SELECTOR, "input[type='text']")
                    except NoSuchElementException:
                        # 診斷：dump URL + HTML snippet + 截圖
                        try:
                            _cur_url = getattr(self.driver, "current_url", "?")
                            self.log(f"❌ 找不到帳號欄位 | URL={_cur_url}")
                        except Exception:
                            self.log("❌ 找不到帳號欄位")
                        try:
                            _html = self.driver.page_source or ""
                            _snippet = _html[:2000].replace("\n", " ")
                            self.log(f"  HTML snippet: {_snippet}")
                        except Exception:
                            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 1286, exc_info=True)
                        try:
                            import time as _time
                            _dbg_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".runtime", "debug_screenshots")
                            os.makedirs(_dbg_dir, exist_ok=True)
                            _dbg_path = os.path.join(_dbg_dir, f"transcript_login_fail_{int(_time.time())}.png")
                            self.driver.save_screenshot(_dbg_path)
                            self.log(f"  已儲存診斷截圖: {_dbg_path}")
                        except Exception as _dbg_err:
                            self.log(f"  截圖失敗: {_dbg_err}")
                        continue
                
                # 找到密碼欄位
                password_field = None
                try:
                    password_field = self.driver.find_element(By.ID, "j_password")
                except NoSuchElementException:
                    try:
                        password_field = self.driver.find_element(By.CSS_SELECTOR, "input[type='password']")
                    except NoSuchElementException:
                        self.log("❌ 找不到密碼欄位")
                        continue
                
                # 模擬人類輸入：清除並逐字填入
                self.log(f"  填入帳號: {self.username[:3]}***")
                username_field.clear()
                self._random_delay(0.3, 0.8)
                for char in self.username:
                    username_field.send_keys(char)
                    time.sleep(0.05 + 0.05 * (time.time() % 1))  # 隨機打字速度
                
                self._random_delay(0.5, 1.5)
                
                self.log(f"  填入密碼: ***")
                password_field.clear()
                self._random_delay(0.3, 0.8)
                for char in self.password:
                    password_field.send_keys(char)
                    time.sleep(0.05 + 0.05 * (time.time() % 1))
                
                self._random_delay(0.5, 1.0)
                
                # 處理驗證碼（如果存在）
                if force_solve or need_captcha:
                    captcha_solved = self._solve_captcha()
                    if not captcha_solved:
                        # 需要驗證碼但 OCR 未解出：避免送出造成 alert/鎖定，直接下一輪重試
                        self.log("  ⚠️ 驗證碼未能自動解出，改由下一輪重新登入重試（不送出）")
                        self._random_delay(2, 4)
                        continue
                else:
                    self.log("  ℹ️ 策略：先不處理驗證碼，直接嘗試登入")
                
                self._random_delay(0.5, 1.5)
                
                # 找到並點擊登入按鈕
                login_btn = None
                btn_selectors = [
                    (By.CSS_SELECTOR, "input.button-style"),
                    (By.CSS_SELECTOR, "input[type='submit']"),
                    (By.CSS_SELECTOR, "button[type='submit']"),
                ]
                
                for selector in btn_selectors:
                    try:
                        login_btn = self.driver.find_element(*selector)
                        if login_btn and login_btn.is_displayed():
                            break
                    except NoSuchElementException:
                        continue
                
                if not login_btn:
                    self.log("❌ 找不到登入按鈕")
                    continue

                # 防呆：若欄位被清空，先補填再送出，避免跳出「請輸入會員帳號！」。
                # ★ 注意：瀏覽器安全限制下 password input 的 getAttribute("value") 永遠回傳 ""，
                #   不能以此判斷密碼是否已填。使用 JS property 讀取實際值；若仍讀不到則
                #   只在未曾解過驗證碼的情況下重填（避免重填密碼時清掉已填的驗證碼）。
                _captcha_was_solved = force_solve or need_captcha
                try:
                    uval = (username_field.get_attribute("value") or "").strip()
                    if not uval:
                        if _captcha_was_solved:
                            # ★ 驗證碼已填時，用 JS 設帳號（不觸發事件，避免清掉驗證碼）
                            try:
                                self.driver.execute_script(
                                    "arguments[0].value = arguments[1]",
                                    username_field, self.username)
                                self.log("  ⚠️ 帳號欄位空值（已解驗證碼），用 JS 直接設值（不觸發 event）")
                            except Exception:
                                self.log("  ⚠️ JS 設帳號失敗，略過（避免清掉驗證碼）")
                        else:
                            self.log("  ⚠️ 帳號欄位疑似被清空，重新填入")
                            username_field.clear()
                            username_field.send_keys(self.username)
                    # 密碼欄位：優先用 JS property 判斷，避免 getAttribute 永遠回空
                    try:
                        pval = (self.driver.execute_script(
                            "return arguments[0].value", password_field) or "").strip()
                    except Exception:
                        pval = (password_field.get_attribute("value") or "").strip()
                    if not pval:
                        if _captcha_was_solved:
                            # ★ 若驗證碼已填，不重填密碼（重填密碼會清掉驗證碼）
                            # 改用 JS 直接設值，不觸發 input event
                            try:
                                self.driver.execute_script(
                                    "arguments[0].value = arguments[1]",
                                    password_field, self.password)
                                self.log("  ⚠️ 密碼欄位空值（已解驗證碼），用 JS 直接設值（不觸發 event）")
                            except Exception:
                                self.log("  ⚠️ JS 設密碼失敗，略過（避免清掉驗證碼）")
                        else:
                            self.log("  ⚠️ 密碼欄位疑似被清空，重新填入")
                            password_field.clear()
                            password_field.send_keys(self.password)
                    # ★★ 驗證碼保護：若驗證碼已填，確認欄位值仍在（帳號/密碼填寫可能觸發 JS 清空）
                    if _captcha_was_solved:
                        try:
                            _chk_val = self.driver.execute_script(
                                "return document.getElementById('chkCode') ? "
                                "document.getElementById('chkCode').value : ''") or ""
                            if not _chk_val.strip():
                                self.log("  ⚠️ 驗證碼欄位被清空（帳號/密碼填寫觸發 JS），重新填入")
                                # 用 JS 直接補填驗證碼，不觸發 focus/blur 事件
                                try:
                                    self.driver.execute_script(
                                        "var el = document.getElementById('chkCode');"
                                        "if(el){ el.value = arguments[0]; }",
                                        getattr(self, '_last_captcha_text', ''))
                                    self.log(f"  ★ 驗證碼已用 JS 補填（len={len(getattr(self,'_last_captcha_text',''))}）")
                                except Exception as _ce:
                                    self.log(f"  ⚠️ JS 補填驗證碼失敗: {_ce}")
                            else:
                                self.log(f"  ✓ 驗證碼欄位確認有值（len={len(_chk_val)}）")
                        except Exception as _ve:
                            self.log(f"  ⚠️ 驗證碼保護讀取失敗: {_ve}")
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1044, exc_info=True)
                
                # ★★ 送出前診斷：列出所有 form 欄位值（含隱藏欄位），確認有沒有漏填
                if _captcha_was_solved:
                    try:
                        _form_diag = self.driver.execute_script("""
                            var f = document.querySelector('form');
                            if (!f) return 'no form';
                            var inputs = [];
                            f.querySelectorAll('input').forEach(function(el){
                                var v = el.value || '';
                                var masked = el.type==='password' ? '***' : (v.length>20 ? v.substring(0,20)+'…' : v);
                                inputs.push(el.name+'['+el.type+']='+masked);
                            });
                            return inputs.join(', ');
                        """) or ""
                        self.log(f"  [diag-form] {_form_diag}")
                    except Exception as _fd:
                        self.log(f"  [diag-form] 讀取失敗: {_fd}")

                self.log("  點擊登入...")
                try:
                    # Opt-in one-shot flag: login may raise password-expiry alert.
                    self.driver._next_dialog_no_dismiss = True
                    login_btn.click()
                except Exception:
                    try:
                        self.driver._next_dialog_no_dismiss = True
                        password_field.send_keys(Keys.RETURN)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1053, exc_info=True)
                self._random_delay(3, 5)
                
                # 檢查是否登入成功
                # 若有 alert（密碼到期警告或欄位缺漏），先關閉再確認是否已登入
                try:
                    current_url = self.driver.current_url
                except Exception as e:
                    # current_url 讀取失敗 → 通常是 unexpected alert open
                    # 密碼到期警告出現在成功登入後的頁面跳轉期間 → 接受後需等待主頁面載入
                    _alert_accepted = self._dismiss_password_expiry_alert()
                    err_str = str(e)
                    if not _alert_accepted:
                        self.log(f"  ⚠️ 登入讀取 current_url 失敗: {e}")
                    # Alert 含「驗證碼」→ 必須填驗證碼才能登入，不等直接下一輪
                    if "驗證碼" in err_str or "captcha" in err_str.lower():
                        need_captcha = True
                        self.log("  ℹ️ Alert 要求驗證碼，下一輪啟用 OCR")
                        self._random_delay(2, 4)
                        continue
                    # 密碼到期警告或其他 alert → 等待主頁面載入，retry 最多 5 次
                    # 每次等 1.5-2.5s，最長等 ~12s；每輪也清掉殘留 alert
                    _login_detected = False
                    for _post_alert_retry in range(5):
                        self._random_delay(1.5, 2.5)
                        self._dismiss_password_expiry_alert()  # 清掉殘留 alert
                        if self._has_logout_link():
                            self.logged_in = True
                            self.log(f"✅ 登入成功（alert 已處理，第 {_post_alert_retry + 1} 次確認）")
                            self._save_session_cookies()
                            _login_detected = True
                            return True
                        try:
                            _url_now = self.driver.current_url
                            if "loginPage" not in _url_now:
                                self.logged_in = True
                                self.log(f"✅ 登入成功（URL 已跳轉: {_url_now[:60]}）")
                                self._save_session_cookies()
                                _login_detected = True
                                return True
                        except Exception:
                            # A modal alert can temporarily block current_url.  This
                            # is recoverable, but must remain diagnosable if login
                            # confirmation later times out.
                            logging.getLogger(__name__).debug(
                                "登入確認無法讀取 current_url；可能仍有 alert，繼續等待",
                                exc_info=True,
                            )
                    if not _login_detected:
                        self.log("  ⚠️ Alert 已接受但未偵測到登入成功狀態，下一輪重試")
                    self._random_delay(2, 4)
                    continue
                if "loginPage" not in current_url or self._has_logout_link():
                    self.logged_in = True
                    self.log("✅ 登入成功")
                    self._save_session_cookies()
                    return True
                else:
                    self.log(f"  ⚠️ 登入失敗，等待後重試... (URL: {current_url[:80]})")
                    # ★ IP 鎖定偵測：errCnt 過高 → 提前放棄，避免繼續累積失敗加重鎖定
                    import re as _re_login
                    _err_cnt_match = _re_login.search(r"errCnt=(\d+)", current_url)
                    if _err_cnt_match:
                        _err_cnt = int(_err_cnt_match.group(1))
                        if _err_cnt >= 30:
                            self.log(f"  🚫 偵測到 errCnt={_err_cnt}（>=30），疑似 IP/帳號被鎖定。"
                                     "請手動登入或等待 15-30 分鐘後重試。")
                            break  # 提前跳出 retry 迴圈，不再繼續嘗試
                    # 抓取頁面錯誤文字以診斷失敗原因
                    _page_requires_captcha = False
                    try:
                        page_text = self.driver.find_element(By.TAG_NAME, "body").text or ""
                        for kw in ["驗證碼", "錯誤", "帳號", "密碼", "Error", "captcha", "locked", "鎖"]:
                            if kw.lower() in page_text.lower():
                                # 取出含關鍵字的那行
                                for line in page_text.splitlines():
                                    if kw.lower() in line.lower() and line.strip():
                                        self.log(f"  📄 頁面訊息: {line.strip()[:120]}")
                                        break
                        # ★ 頁面明確要求驗證碼 → 下一輪啟用 OCR
                        if "驗證碼" in page_text or "captcha" in page_text.lower():
                            _page_requires_captcha = True
                    except Exception:
                        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 1532, exc_info=True)
                    if _page_requires_captcha and not need_captcha:
                        need_captcha = True
                        self.log("  ℹ️ 頁面要求驗證碼，下一輪啟用 OCR")
                    # 失敗後等待更長時間再重試
                    self._random_delay(5 * (attempt + 1), 10 * (attempt + 1))
                    
            except Exception as e:
                self.last_login_error_code = "login_exception"
                self.last_login_error_detail = f"login_exception: {str(e)[:240]}"
                self.log(f"登入異常: {e}")
                traceback.print_exc()
                # driver.get() timeout 後 driver 可能進入不穩狀態；直接重建以提升成功率
                try:
                    if self.driver:
                        self.driver.quit()
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1098, exc_info=True)
                self.driver = None
                self._random_delay(5 * (attempt + 1), 10 * (attempt + 1))
        
        if not self.last_login_error_detail:
            page_text = " ".join((self._current_page_text(max_chars=1200) or "").split())
            if self._page_has_unauthorized_marker():
                self.last_login_error_code = "ezlawyer_not_authorized"
                self.last_login_error_detail = (
                    "ezlawyer_not_authorized: 已登入電子筆錄服務網，但目前頁面顯示未授權；"
                    f"url={self._current_url_safe()[:120]}"
                )
            else:
                self.last_login_error_code = "login_failed"
                self.last_login_error_detail = (
                    f"SSO login failed: url={self._current_url_safe()[:120]}"
                    + (f"; page={page_text[:240]}" if page_text else "")
                )
        self.log(f"❌ 登入失敗 - 已達最大重試次數: {self.last_login_error_detail[:160]}")
        return False
    
    def _solve_captcha(self) -> bool:
        """識別並填入驗證碼"""
        try:
            # 尋找驗證碼輸入框
            captcha_input = None
            try:
                captcha_input = self.driver.find_element(By.ID, "chkCode")
            except NoSuchElementException:
                try:
                    captcha_input = self.driver.find_element(By.CSS_SELECTOR, "input[name*='captcha'], input[name*='chk']")
                except NoSuchElementException:
                    # 找不到就視為不需要驗證碼（符合「可直接登入」的實務）
                    self.log("  ℹ️ 找不到驗證碼輸入框，視為不需要驗證碼")
                    return True

            def _find_captcha_img():
                # 尋找真正的驗證碼圖片，避免誤抓到「重新產生」圖示
                candidates = []
                seen = set()
                selectors = [
                    "img[src*='dynamicImage']",
                    "img[src*='/eb/dynamicImage']",
                    "img[id='captcha']",
                    "img#captcha",
                    "img[src*='captcha']",
                    "img[title*='驗證碼']",
                ]
                for selector in selectors:
                    try:
                        for el in self.driver.find_elements(By.CSS_SELECTOR, selector):
                            key = id(el)
                            if key not in seen:
                                seen.add(key)
                                candidates.append(el)
                    except Exception:
                        continue

                best_score = -10**9
                best = None
                try:
                    input_y = float((captcha_input.location or {}).get("y", 0))
                except Exception:
                    input_y = 0.0
                for el in candidates:
                    try:
                        if not el.is_displayed():
                            continue
                        src = (el.get_attribute("src") or "").lower()
                        title = (el.get_attribute("title") or "").lower()
                        w = float((el.size or {}).get("width", 0))
                        h = float((el.size or {}).get("height", 0))
                        y = float((el.location or {}).get("y", 0))

                        score = 0
                        if "dynamicimage" in src:
                            score += 200
                        if "captcha" in src:
                            score += 80
                        if w >= 50 and h >= 18:
                            score += 30
                        if abs(y - input_y) <= 100:
                            score += 20

                        # Refresh icon signals
                        if "reload" in src or "refresh" in src:
                            score -= 500
                        if "重新產生" in title:
                            score -= 500
                        if w <= 32 and h <= 32:
                            score -= 120

                        if score > best_score:
                            best_score = score
                            best = el
                    except Exception:
                        continue
                return best

            # 尋找真正的驗證碼圖片，避免誤抓到「重新產生」圖示
            captcha_img = _find_captcha_img()

            if not captcha_img:
                self.log("  ℹ️ 沒有發現驗證碼圖片（可能不需要）")
                return True
            
            # 使用 OCR 識別驗證碼
            if not hasattr(self, 'captcha_solver') or not self.captcha_solver:
                self.captcha_solver = CaptchaSolver()

            allow_ocr = os.environ.get("MAGI_ALLOW_CAPTCHA_OCR", "1").strip().lower() in {"1", "true", "yes", "on"}
            allow_human = os.environ.get("MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK", "0").strip().lower() in {"1", "true", "yes", "on"}
            expected_len = int(os.environ.get("MAGI_EZLAWYER_CAPTCHA_LEN", "4") or "4")
            max_retries = int(os.environ.get("MAGI_EZLAWYER_CAPTCHA_RETRIES", "8") or "8")

            def _refresh_captcha() -> bool:
                # 用 find_elements（plural）避免 implicit wait 每個 selector 等 5s
                refresh_selectors = [
                    (By.CSS_SELECTOR, "a[onclick*='reload']"),
                    (By.CSS_SELECTOR, "a[onclick*='refresh']"),
                    (By.CSS_SELECTOR, "a[onclick*='captcha']"),
                    (By.XPATH, "//a[contains(text(),'重新')]"),
                    (By.XPATH, "//a[contains(text(),'刷新')]"),
                    (By.XPATH, "//img[contains(@title,'重新')]"),
                    (By.CSS_SELECTOR, "img[src*='refresh']"),
                    (By.CSS_SELECTOR, "img[alt*='重新']"),
                ]
                for by, value in refresh_selectors:
                    try:
                        els = self.driver.find_elements(by, value)
                        for el in els:
                            if el and el.is_displayed():
                                el.click()
                                self._random_delay(0.5, 1.2)
                                return True
                    except Exception:
                        continue
                try:
                    # 點圖片本身通常也會刷新
                    img2 = _find_captcha_img() or captcha_img
                    img2.click()
                    self._random_delay(0.4, 1.0)
                    return True
                except Exception:
                    return False

            captcha_text = ""
            if allow_ocr:
                for n in range(max_retries):
                    # 每次都重新找元素，避免 stale element reference
                    try:
                        captcha_img = _find_captcha_img() or captcha_img
                    except Exception:
                        captcha_img = None
                    if not captcha_img:
                        break
                    try:
                        raw = self.captcha_solver.solve_from_element(self.driver, captcha_img) or ""
                    except Exception:
                        captcha_img = _find_captcha_img()
                        if not captcha_img:
                            break
                        try:
                            raw = self.captcha_solver.solve_from_element(self.driver, captcha_img) or ""
                        except Exception:
                            break
                    local_text = re.sub(r"[^A-Za-z0-9]", "", raw)[:expected_len]
                    self.log(f"  OCR 第 {n + 1} 次，local_len={len(local_text)} text={local_text!r}")
                    # 儲存驗證碼圖片供人工檢查（first OCR attempt only）
                    if n == 0:
                        try:
                            _debug_path = os.path.join(self.download_folder, "debug_ezlawyer_captcha.png")
                            with open(_debug_path, "wb") as _f:
                                _f.write(captcha_img.screenshot_as_png)
                            self.log(f"  📸 驗證碼圖片已存: {_debug_path}")
                        except Exception:
                            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 1709, exc_info=True)

                    # Melchior 交叉驗證（與 file_review 一致）
                    melchior_text = ""
                    if captcha_img:
                        try:
                            melchior_text = re.sub(
                                r"[^A-Za-z0-9]", "",
                                self._solve_captcha_with_melchior(captcha_img, expected_len) or ""
                            )[:expected_len]
                            if melchior_text:
                                self.log(f"  Melchior 第 {n + 1} 次，melchior_len={len(melchior_text)}")
                        except Exception:
                            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 1722, exc_info=True)

                    if len(local_text) >= expected_len and len(melchior_text) >= expected_len:
                        if local_text == melchior_text:
                            self.log("  ✅ 雙引擎驗證一致")
                            captcha_text = local_text
                            break
                        self.log("  ⚠️ 雙引擎結果不一致，刷新重試")
                    elif len(local_text) >= expected_len and not melchior_text:
                        self.log("  ⚠️ Melchior 無回應，退回信任 local OCR")
                        captcha_text = local_text
                        break
                    elif len(melchior_text) >= expected_len:
                        self.log("  ✅ Melchior 備援辨識成功")
                        captcha_text = melchior_text
                        break

                    self.log(f"  ⚠️ 驗證碼 OCR 不足（第 {n + 1}/{max_retries} 次），自動刷新重試")
                    _refresh_captcha()
                else:
                    captcha_text = ""

            if (not captcha_text) and allow_human:
                try:
                    from magi_human_captcha import request_human_captcha
                    img_path = os.path.join(self.download_folder, "debug_ezlawyer_captcha.png")
                    try:
                        with open(img_path, "wb") as f:
                            f.write(captcha_img.screenshot_as_png)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1248, exc_info=True)
                    captcha_text = request_human_captcha(
                        kind="ezlawyer_record",
                        image_path=Path(img_path),
                        expected_len=expected_len,
                        ttl_seconds=int(os.environ.get("MAGI_CAPTCHA_TTL_SECONDS", "300") or "300"),
                        wait_seconds=int(os.environ.get("MAGI_CAPTCHA_WAIT_SECONDS", "180") or "180"),
                        headless=bool(self.headless),
                        notify=True,
                        log=self.log,
                    )
                except Exception as e:
                    self.log(f"  ⚠️ 人工驗證碼流程失敗: {e}")
                    captcha_text = ""

            captcha_text = re.sub(r"[^A-Za-z0-9]", "", (captcha_text or ""))
            if len(captcha_text) >= expected_len:
                self.log("  ✓ 已取得驗證碼（不顯示）")
                _target = captcha_text[:expected_len]
                _filled = False
                # 診斷：確認 captcha_input 的 id / name / type
                try:
                    _ci_id = captcha_input.get_attribute("id") or ""
                    _ci_name = captcha_input.get_attribute("name") or ""
                    _ci_type = captcha_input.get_attribute("type") or ""
                    self.log(f"  [diag] captcha_input: id={_ci_id!r} name={_ci_name!r} type={_ci_type!r}")
                except Exception:
                    logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 1779, exc_info=True)
                # ★ 優先用 Playwright native fill()（最可靠，直接設 value + 觸發事件）
                try:
                    _pw_el = getattr(captcha_input, "_el", None)
                    if _pw_el is not None:
                        _pw_el.fill(_target)
                        _filled = True
                        # 回讀確認
                        try:
                            _readback = self.driver.execute_script("return arguments[0].value", captcha_input) or ""
                            self.log(f"  (captcha filled via PW native fill, readback_len={len(_readback)})")
                        except Exception:
                            self.log("  (captcha filled via PW native fill)")
                except Exception as _e:
                    self.log(f"  ⚠️ PW native fill 失敗: {_e}")
                if not _filled:
                    # Fallback: click + clear + 逐字 send_keys + dispatchEvent
                    try:
                        captcha_input.click()
                        self._random_delay(0.1, 0.3)
                    except Exception:
                        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 1800, exc_info=True)
                    captcha_input.clear()
                    self._random_delay(0.1, 0.3)
                    for ch in _target:
                        captcha_input.send_keys(ch)
                        time.sleep(0.06)
                    try:
                        self.driver.execute_script(
                            "arguments[0].dispatchEvent(new Event('input',{bubbles:true}));"
                            "arguments[0].dispatchEvent(new Event('change',{bubbles:true}));",
                            captcha_input)
                    except Exception:
                        logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 1812, exc_info=True)
                # ★ 儲存驗證碼文字，供 pre-submit 保護機制補填用
                self._last_captcha_text = _target
                return True

            self.log("  ⚠️ 驗證碼未取得，將由登入流程重新嘗試")
            return False
                
        except Exception as e:
            self.log(f"  驗證碼處理錯誤: {e}")
            return False
    
    def _has_logout_link(self) -> bool:
        """檢查頁面是否有登出連結（表示已登入）"""
        try:
            text = self._current_page_text(max_chars=2000)
            compact = re.sub(r"\s+", "", text or "")
            if "會員姓名：尚未登入" in text or "會員姓名:尚未登入" in text or "會員姓名：尚未登入" in compact or "會員姓名:尚未登入" in compact:
                return False
        except Exception as e:
            self.log(f"  登入狀態文字檢查失敗，改用後續方式判斷: {type(e).__name__}")
        try:
            self.driver.find_element(By.XPATH, "//a[contains(text(), '登出') or contains(text(), 'Logout')]")
            return True
        except Exception as e:
            self.log(f"  登出連結檢查失敗，改用 JS 判斷: {type(e).__name__}")
        try:
            found = self.driver.execute_script("""
                return Array.from(document.querySelectorAll('a')).some(a => {
                    const text = (a.innerText || a.textContent || '').trim();
                    const href = (a.href || a.getAttribute('href') || '').toLowerCase();
                    return text.includes('登出') || text.toLowerCase().includes('logout') || href.includes('logout');
                });
            """)
            if bool(found):
                return True
        except Exception as e:
            self.log(f"  JS 登出連結檢查失敗，改用頁面文字判斷: {type(e).__name__}")
        try:
            text = self._current_page_text(max_chars=2000)
            if "會員姓名" in text and ("會員期限" in text or "電子筆錄調閱" in text):
                return True
        except Exception as e:
            self.log(f"  登入狀態 fallback 文字檢查失敗: {type(e).__name__}")
        return False

    def _current_url_safe(self) -> str:
        try:
            return str(getattr(self.driver, "current_url", "") or "")
        except Exception:
            return ""

    def _current_page_text(self, max_chars: int = 8000) -> str:
        try:
            return (self.driver.find_element(By.TAG_NAME, "body").text or "")[:max_chars]
        except Exception:
            try:
                return (getattr(self.driver, "page_source", "") or "")[:max_chars]
            except Exception:
                return ""

    def _page_has_unauthorized_marker(self) -> bool:
        """ezlawyer may show a logged-in page that is not authorized for a deep link."""
        text = self._current_page_text().lower()
        markers = [
            "not authorized to access this page",
            "you are not authorized",
            "沒有權限",
            "無權限",
            "未授權",
            "未被授權",
            "未獲授權",
        ]
        return any(marker.lower() in text for marker in markers)

    def _search_form_ready(self, timeout_sec: float = 0) -> bool:
        deadline = time.time() + max(0, float(timeout_sec or 0))
        while True:
            try:
                self.driver.find_element(By.ID, "jud_name")
                return True
            except Exception:
                if time.time() >= deadline:
                    return False
                time.sleep(0.25)

    def _wait_for_search_form(self, timeout_ms: int = 15000) -> bool:
        _page = getattr(self.driver, "_page", None)
        if _page is not None:
            try:
                _page.wait_for_selector("#jud_name", timeout=timeout_ms, state="attached")
                return True
            except Exception:
                return self._search_form_ready(timeout_sec=1)
        return self._search_form_ready(timeout_sec=max(1, timeout_ms / 1000))

    def _transcript_menu_href(self) -> str:
        js = """
            const anchors = Array.from(document.querySelectorAll('a'));
            const hit = anchors.find(a => {
                const text = (a.innerText || a.textContent || '').trim();
                const href = a.href || a.getAttribute('href') || '';
                return text.includes('電子筆錄調閱') || href.includes('/eb/user/downloadEB');
            });
            return hit ? (hit.href || hit.getAttribute('href') || '') : '';
        """
        try:
            href = self.driver.execute_script(js)
            return str(href or "").strip()
        except Exception:
            return ""

    def _click_transcript_menu_entry(self, label: str = "側欄點擊") -> bool:
        _page = getattr(self.driver, "_page", None)
        if _page is not None:
            try:
                locator = _page.locator("a", has_text="電子筆錄調閱").first
                locator.click(timeout=5000)
                if self._wait_for_search_form(timeout_ms=20000):
                    self.log(f"  ✓ 搜尋頁就緒（{label}）")
                    return True
            except Exception as _ce:
                self.log(f"  ⚠️ 電子筆錄入口點擊失敗: {str(_ce)[:120]}")
        else:
            try:
                for el in self.driver.find_elements(By.TAG_NAME, "a"):
                    text = (el.text or "").strip()
                    href_attr = (el.get_attribute("href") or "").strip()
                    if "電子筆錄調閱" in text or "/eb/user/downloadEB" in href_attr:
                        el.click()
                        if self._wait_for_search_form(timeout_ms=20000):
                            self.log(f"  ✓ 搜尋頁就緒（{label}）")
                            return True
                        break
            except Exception as _ce:
                self.log(f"  ⚠️ 電子筆錄入口點擊失敗: {str(_ce)[:120]}")
        return False

    def _open_transcript_search_page(self) -> bool:
        """
        Open the transcript search form. The ezlawyer site sometimes rejects a
        direct deep link while the logged-in sidebar still exposes the valid
        transcript entry, so try the official menu entry as a fallback.
        """
        _page = getattr(self.driver, "_page", None)

        def _goto(url: str, label: str) -> bool:
            try:
                if _page is not None:
                    _page.goto(url, timeout=15000, wait_until="commit")
                else:
                    self.driver.get(url)
                if self._wait_for_search_form(timeout_ms=20000):
                    self.log(f"  ✓ 搜尋頁就緒（{label}）")
                    return True
                if self._page_has_unauthorized_marker():
                    self.log(f"  ⚠️ {label} 顯示未授權頁，改試側欄入口")
                else:
                    self.log(f"  ⚠️ {label} 未出現搜尋表單")
                return False
            except Exception as _ne:
                self.log(f"  ⚠️ {label} 導航失敗: {str(_ne)[:120]}")
                return False

        href = self._transcript_menu_href()
        if href and not href.lower().startswith("javascript"):
            self.log(f"  ℹ️ 由目前頁面的電子筆錄入口進入")
            if _goto(href, "側欄入口"):
                return True
        if self._click_transcript_menu_entry("側欄點擊"):
            return True

        if _goto(self.SEARCH_URL, "直接入口"):
            return True

        # Direct deep link may bounce to a bare login/access page. Restore the
        # logged-in user page once and try the sidebar again.
        user_page = f"{self.BASE_URL}/eb/user/userPage"
        try:
            if _page is not None:
                _page.goto(user_page, timeout=15000, wait_until="commit")
            else:
                self.driver.get(user_page)
            time.sleep(1)
        except Exception as _ue:
            self.log(f"  ⚠️ 回到會員頁失敗: {str(_ue)[:120]}")

        href = self._transcript_menu_href()
        if href and not href.lower().startswith("javascript"):
            self.log(f"  ℹ️ 由會員頁電子筆錄入口重新進入")
            if _goto(href, "會員頁側欄入口"):
                return True
        if self._click_transcript_menu_entry("會員頁側欄點擊"):
            return True

        if _page is not None:
            try:
                locator = _page.locator("text=電子筆錄調閱").first
                locator.click(timeout=5000)
                if self._wait_for_search_form(timeout_ms=20000):
                    self.log("  ✓ 搜尋頁就緒（文字點擊）")
                    return True
            except Exception as _ce:
                self.log(f"  ⚠️ 電子筆錄文字點擊失敗: {str(_ce)[:120]}")

        if self._page_has_unauthorized_marker():
            msg = (
                "ezlawyer_not_authorized: 已登入電子筆錄服務網，但目前帳號/入口未授權存取調閱頁"
                f"；url={self._current_url_safe()[:120]}"
            )
        else:
            msg = (
                "transcript_search_page_unavailable: 已登入但找不到電子筆錄搜尋表單"
                f"；url={self._current_url_safe()[:120]}"
            )
        self._last_download_error = msg
        self.last_login_error_code = "ezlawyer_not_authorized" if msg.startswith("ezlawyer_not_authorized") else "search_page_unavailable"
        self.last_login_error_detail = msg
        self.log(f"  ❌ {msg}")
        return False
    
    def _solve_captcha_with_melchior(self, captcha_img, expected_len: int = 6) -> str:
        """InferenceGateway 備援 OCR — 驗證碼交叉驗證（alphanumeric）"""
        if os.environ.get("MAGI_CAPTCHA_USE_MELCHIOR", "1").strip().lower() not in {"1", "true", "yes", "on"}:
            return ""
        tmp_path = None
        try:
            png = captcha_img.screenshot_as_png
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                tf.write(png)
                tmp_path = tf.name
            try:
                from skills.bridge.inference_gateway import InferenceGateway
            except Exception:
                magi_root = os.environ.get("MAGI_ROOT_DIR", str(_MAGI_ROOT)).strip() or str(_MAGI_ROOT)
                if magi_root and magi_root not in sys.path:
                    sys.path.insert(0, magi_root)
                from skills.bridge.inference_gateway import InferenceGateway
            prompt = (
                f"Read this CAPTCHA image and output ONLY the {expected_len} "
                "alphanumeric characters (letters and digits). No spaces, no punctuation."
            )
            gateway = InferenceGateway()
            r = gateway.dispatch(
                prompt=prompt,
                image_path=tmp_path,
                task_type="captcha",
                timeout=max(8, int(os.environ.get("MAGI_CAPTCHA_VISION_TIMEOUT", "12") or "12")),
                cross_validate=True,
                tc_review=False,
            )
            text = ""
            if isinstance(r, dict):
                text = str(r.get("analysis") or r.get("response") or r.get("text") or "")
            cleaned = re.sub(r"[^A-Za-z0-9]", "", text or "")
            if len(cleaned) >= expected_len:
                self.log(f"  🤖 Gateway CAPTCHA route={r.get('route')} degraded={r.get('degraded')}")
                return cleaned[:expected_len]
            return ""
        except Exception as e:
            self.log(f"  ⚠️ Gateway 驗證碼備援失敗: {e}")
            return ""
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except Exception:
                    logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 1878, exc_info=True)

    def get_cases_from_db(self) -> List[CourtCase]:
        """從資料庫取得案件"""
        if not self.db:
            return []
        
        try:
            # 排除地方檢察署案件（檢察署不提供筆錄下載）
            query = """
                SELECT id, case_number, court_name, court_case_number, 
                       case_type, client_name, folder_path
                FROM cases 
                WHERE status IN ('進行中', 'Active', '開辦中')
                  AND court_case_number IS NOT NULL 
                  AND court_case_number != ''
                  AND court_name IS NOT NULL
                  AND TRIM(court_name) != ''
                  AND court_name NOT LIKE '%檢察署%'
                  AND court_name NOT LIKE '%檢察%'
                  AND court_name NOT LIKE '%地檢%'
            """
            results = self.db.execute(query, fetch='all') or []
            
            _field_names = ('id', 'case_number', 'court_name', 'court_case_number',
                           'case_type', 'client_name', 'folder_path')
            cases = []
            for row in results:
                if isinstance(row, (tuple, list)):
                    row = dict(zip(_field_names, row))
                elif hasattr(row, "keys"):
                    # sqlite3.Row / dict-like
                    row = dict(row)
                elif not isinstance(row, dict):
                    self.log(f"  ⚠️ 跳過未知 DB row 型態: {type(row)}")
                    continue
                case = CourtCase(
                    case_id=row.get('id', ''),
                    case_number=row.get('case_number', ''),
                    court_name=row.get('court_name', ''),
                    court_case_number=row.get('court_case_number', ''),
                    case_type=row.get('case_type', ''),
                    client_name=row.get('client_name', ''),
                    folder_path=row.get('folder_path', '')
                )
                cases.append(case)
            
            self.log(f"從資料庫取得 {len(cases)} 筆案件")
            return cases
            
        except Exception as e:
            self.log(f"查詢資料庫失敗: {e}")
            return []
    
    def _parse_case_number(self, case_number: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """
        解析案號
        
        支援格式：
        - 114年度訴字第123號
        - 114年度宜簡字第299號
        - 114.訴.000123
        
        Returns:
            (year, word, number) 或 (None, None, None)
        """
        # 格式1: 114年度訴字第123號 / 114年度宜簡字第299號 / 114年度司消債調第124號 (字可選)
        match = re.search(r'(\d+)年度?(.+?)(?:字|)?第?(\d+)號?', case_number)
        if match:
            return match.groups()
        
        # 格式2: 114.訴.000123
        match = re.search(r'(\d+)[.\-](.+?)[.\-](\d+)', case_number)
        if match:
            year, word, number = match.groups()
            return (year, word, str(int(number)))
        
        return (None, None, None)

    def _case_scoped_result_elements(self, xpath: str, case: Optional[CourtCase]) -> List[Any]:
        """Return result controls only from the row for ``case``.

        The portal may ignore submitted filters and render the entire request
        inventory.  Treating every button/link on that page as belonging to the
        requested case caused cross-case transcript downloads.  A page without
        docket rows is a dedicated detail page and may still use all controls;
        a multi-case table is always scoped to the exact docket row.
        """

        all_elements = list(self.driver.find_elements(By.XPATH, xpath) or [])
        if case is None:
            return all_elements
        try:
            rows = list(self.driver.find_elements(By.XPATH, "//tr") or [])
        except Exception:
            return []
        docket_rows = []
        matching_rows = []
        target = str(getattr(case, "court_case_number", "") or "").strip()
        for row in rows:
            try:
                row_text = str(row.text or "")
            except Exception:
                continue
            if _portal_docket_identities(row_text):
                docket_rows.append(row)
            if _portal_row_matches_case_text(row_text, target):
                matching_rows.append(row)
        if not docket_rows:
            return all_elements
        if not matching_rows:
            self._last_download_error = (
                "result_row_mismatch: 結果頁含其他案件，"
                f"但找不到目標案號 {target} 的可核對列"
            )
            self.log(f"  ❌ {self._last_download_error}")
            return []
        relative_xpath = xpath if xpath.startswith(".") else "." + xpath
        scoped: List[Any] = []
        seen: set[str] = set()
        for row in matching_rows:
            try:
                row_elements = list(row.find_elements(By.XPATH, relative_xpath) or [])
            except Exception:
                continue
            for element in row_elements:
                identity = str(getattr(element, "id", "") or id(element))
                if identity in seen:
                    continue
                seen.add(identity)
                scoped.append(element)
        return scoped
    
    def _determine_case_type(self, case: CourtCase, word: str) -> str:
        """
        根據案件資訊和字別決定類別
        
        簡易案件判斷規則：
        - 字別含「簡」→ 簡易案件
        - 根據 case_type 決定是刑事還是民事
        
        Args:
            case: 案件資訊
            word: 字別（如 訴、宜簡、交上易）
            
        Returns:
            類別名稱（用於下拉選單）
        """
        case_type = case.case_type or ''
        
        # 判斷是否為簡易案件
        is_simple = '簡' in word
        
        # 判斷刑事還是民事
        if '刑' in case_type or case_type in ['刑事', '刑事簡易']:
            return '刑事'
        elif '民' in case_type or case_type in ['民事', '民事簡易']:
            return '民事'
        elif '家' in case_type:
            return '家事'
        elif '行' in case_type:
            return '行政'
        else:
            # 預設根據字別推測
            if is_simple:
                return '民事'  # 預設簡易為民事
            return '民事'
    
    def _execute_search_query(self, case: CourtCase) -> bool:
        """
        執行查詢動作 (導航、填寫表單、點擊查詢)
        回傳是否成功提交查詢
        """
        try:
            # 解析案號
            year, word, number = self._parse_case_number(case.court_case_number)
            
            if not all([year, word, number]):
                self.log(f"  ⚠️ 無法解析案號: {case.court_case_number}")
                return False
            
            # 導航到搜尋頁面。直接 deep link 在 ezlawyer 偶爾會回 logged-in
            # unauthorized 頁，需改由側欄「電子筆錄調閱」入口進入。
            if not self._search_form_ready(timeout_sec=0.5):
                if not self._open_transcript_search_page():
                    return False
            time.sleep(1)
            
            # 選擇法院 (模糊比對，台/臺 視為同義)
            court_name = (case.court_name or "").strip()
            court_found = False

            def _norm_court_name(s: str) -> str:
                return re.sub(r"\s+", "", (s or "").replace("臺", "台"))
            
            try:
                court_select = Select(self.driver.find_element(By.ID, "jud_name"))
                # 優先嘗試完全匹配
                try:
                    court_select.select_by_visible_text(court_name)
                    court_found = True
                except Exception:
                    try:
                        alt_court_name = court_name.replace("臺", "台")
                        court_select.select_by_visible_text(alt_court_name)
                        court_found = True
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1424, exc_info=True)

                if not court_found:
                    # 模糊匹配
                    norm_target = _norm_court_name(court_name)
                    for option in court_select.options:
                        opt_text = (option.text or "").strip()
                        norm_opt = _norm_court_name(opt_text)
                        if norm_target and (norm_target in norm_opt or norm_opt in norm_target):
                            court_select.select_by_visible_text(option.text)
                            court_found = True
                            break
                    
                    # 再試一次去掉 "臺灣" 的
                    if not court_found:
                         short_name = _norm_court_name(court_name.replace("臺灣", "").replace("台灣", ""))
                         for option in court_select.options:
                            opt_text = (option.text or "").strip()
                            norm_opt = _norm_court_name(opt_text)
                            if short_name and short_name in norm_opt:
                                court_select.select_by_visible_text(option.text)
                                court_found = True
                                break
            except Exception as e:
                self.log(f"  ⚠️ 選擇法院失敗: {e}")
                pass # 繼續嘗試
            
            time.sleep(0.5)
            
            # 選擇類別
            case_type = self._determine_case_type(case, word)
            try:
                type_select = Select(self.driver.find_element(By.ID, "sys_id"))
                type_select.select_by_visible_text(case_type)
            except Exception as e:
                self.log(f"  ⚠️ 選擇類別失敗: {e}")
            time.sleep(0.5)
            
            # 填寫案號
            try:
                self.driver.find_element(By.ID, "eb_year").clear()
                self.driver.find_element(By.ID, "eb_year").send_keys(year)
                
                self.driver.find_element(By.ID, "eb_id").clear()
                self.driver.find_element(By.ID, "eb_id").send_keys(word)
                
                self.driver.find_element(By.ID, "eb_num").clear()
                self.driver.find_element(By.ID, "eb_num").send_keys(number)
                
                time.sleep(0.5)
            except Exception as e:
                 self.log(f"  ⚠️ 填寫案號失敗: {e}")
                 return False

            # 點擊查詢
            try:
                search_btn = self.driver.find_element(By.ID, "queryBtn")
                search_btn.click()
                time.sleep(2)
                
                # 處理 Alert
                alert_result = self._handle_alert()
                if alert_result == "no_data":
                    self.log("  ℹ️ 查無筆錄資料")
                    self._last_no_new_files_reason = "portal_confirmed_empty"
                    return True
                    
                return True
                
            except Exception as e:
                self.log(f"  ⚠️ 提交查詢失敗: {e}")
                return False
                
        except Exception as e:
            self.log(f"  ❌ 執行查詢流程失敗: {e}")
            return False

    def download_record(self, case: CourtCase, transcript_folder: str = None) -> List[str]:

        downloaded_files = []
        
        if not self.driver or not self.logged_in:
            return downloaded_files
        
        try:
            self.log(f"下載: {case.court_name} {case.court_case_number}")

            # 執行查詢（失敗視為查詢失敗，不可假成功成 no_new_files）
            if not self._execute_search_query(case):
                # A long batch can outlive the portal session.  Previously the
                # in-memory ``logged_in`` flag stayed true and every remaining
                # case spent another full navigation timeout before failing.
                # Rebuild the browser/session once and retry the same docket.
                # Keep the detailed failure produced by the navigation helper;
                # replacing it with a generic error made a single expired
                # session look like dozens of unrelated case failures.
                first_error = str(getattr(self, "_last_download_error", "") or "").strip()
                recoverable = (
                    not first_error
                    or first_error.startswith("transcript_search_page_unavailable:")
                    or first_error.startswith("search_navigate_failed:")
                )
                recovered = False
                if recoverable:
                    self.log("  🔄 搜尋頁狀態失效，受控重建登入 session 後重試本案")
                    try:
                        self.close()
                        recovered = bool(self.login(max_retries=2))
                        if recovered:
                            self._last_download_error = ""
                            recovered = bool(self._execute_search_query(case))
                    except Exception as recovery_error:
                        self.log(f"  ⚠️ 搜尋 session 受控復原失敗: {str(recovery_error)[:120]}")
                        recovered = False
                if not recovered:
                    detail = str(getattr(self, "_last_download_error", "") or first_error).strip()
                    raise RuntimeError(
                        detail or f"search_navigate_failed: {case.court_case_number}"
                    )

            # Alert 已給予明確空清單證據，不再對空結果頁做連結推測。
            if getattr(self, "_last_no_new_files_reason", "") == "portal_confirmed_empty":
                self.log("  ℹ️ 入口已明確回覆無筆錄資料")
                return downloaded_files

            # 等待結果載入
            time.sleep(2)

            # 尋找「立即調閱」按鈕並點擊
            downloaded_files = list(
                dict.fromkeys(
                    str(path)
                    for path in (
                        self._process_search_results(
                            transcript_folder=transcript_folder,
                            case=case,
                        )
                        or []
                    )
                )
            )
            
            count = len(downloaded_files)
            if count == 0:
                if getattr(self, "_last_no_new_files_reason", "") == "known_duplicates":
                    self.log(
                        f"  ℹ️ 已確認 PDF 可取回，但 {getattr(self, '_last_pdf_known_duplicate_count', 0)} 份皆為已知檔案"
                    )
                else:
                    self.log(f"  ⚠️ 未下載任何檔案 (未偵測到新檔案)")
            else:
                self.log(f"  ✅ 下載完成，本次新增 {count} 個檔案")
            return downloaded_files
            
        except Exception as e:
            self.log(f"  ❌ 下載失敗: {e}")
            traceback.print_exc()
            # 記錄錯誤到 instance attribute 讓 action.py 區分「真失敗」和「no_new_files」
            err_msg = str(e)[:300]
            try:
                self._last_download_error = err_msg
                if not hasattr(self, "_last_download_errors"):
                    self._last_download_errors = []
                self._last_download_errors.append({
                    "case_number": getattr(case, "court_case_number", "") or "",
                    "error": err_msg,
                })
            except Exception:
                logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 2182, exc_info=True)
            return downloaded_files

    def _handle_alert(self) -> str:
        """
        處理 JavaScript alert 對話框
        
        Returns:
            'no_data' - 查無資料
            'alert_handled' - 有 alert 但已處理
            'no_alert' - 沒有 alert
        """
        try:
            from selenium.webdriver.common.alert import Alert
            
            # 嘗試切換到 alert
            alert = Alert(self.driver)
            alert_text = alert.text
            
            self.log(f"  📢 Alert: {alert_text}")
            
            # 判斷 alert 類型
            if '查無' in alert_text or '無符合' in alert_text:
                # 查無資料，點擊「取消」（不要列入追蹤）
                alert.dismiss()
                return 'no_data'
            else:
                # 其他 alert，點擊「確定」
                alert.accept()
                return 'alert_handled'
                
        except Exception:
            # 沒有 alert
            return 'no_alert'

    def _portal_has_explicit_empty_state(self) -> bool:
        """只在入口頁明確告知查無資料時，才允許回報 no_new_files。"""
        empty_markers = (
            "查無資料",
            "查無筆錄資料",
            "查無符合",
            "無符合條件",
            "無可下載資料",
            "無可下載檔案",
            "目前尚無筆錄",
            "尚無筆錄資料",
            "沒有任何資料",
        )
        visible_text = ""
        try:
            visible_text = str(self.driver.find_element(By.TAG_NAME, "body").text or "")
        except Exception:
            # 不以原始 page_source 為證據，避免誤中隱藏 JS/template。
            try:
                source = str(getattr(self.driver, "page_source", "") or "")
                visible_text = re.sub(r"<script\b[^>]*>.*?</script>", " ", source, flags=re.I | re.S)
                visible_text = re.sub(r"<style\b[^>]*>.*?</style>", " ", visible_text, flags=re.I | re.S)
                visible_text = re.sub(r"<[^>]+>", " ", visible_text)
            except Exception:
                visible_text = ""
        normalized = re.sub(r"\s+", "", visible_text)
        return any(re.sub(r"\s+", "", marker) in normalized for marker in empty_markers)

    def _portal_has_explicit_unavailable_state(self, case: Optional[CourtCase]) -> bool:
        """Return True only when the exact docket row proves it is unavailable.

        The transcript portal commonly renders the entire request inventory even
        after a case-specific query.  Therefore page-wide text such as an expired
        download deadline is not evidence for the requested case.  The marker
        must appear inside a row whose normalized docket exactly matches ``case``.
        This keeps the downloader fail-closed while avoiding false failures for
        requests that are visibly pending, expired, cancelled, or not yet reviewed.
        """
        if case is None:
            return False
        target = str(getattr(case, "court_case_number", "") or "").strip()
        if not target:
            return False
        unavailable_markers = (
            "超過下載期限",
            "逾下載期限",
            "下載期限已過",
            "下載期限逾期",
            "尚未核閱無法下載",
            "書記官尚未核閱",
            "待法院回覆",
            "尚未回覆",
            "法院回覆不同意",
            "取消聲請",
            "待確認",
        )
        try:
            rows = list(self.driver.find_elements(By.XPATH, "//tr") or [])
        except Exception:
            return False
        for row in rows:
            try:
                row_text = str(row.text or "")
            except Exception:
                continue
            if not _portal_row_matches_case_text(row_text, target):
                continue
            normalized = re.sub(r"\s+", "", row_text)
            if any(re.sub(r"\s+", "", marker) in normalized for marker in unavailable_markers):
                return True
        return False

    def _extract_pdf_link_datamap_index(self, link, fallback_index: int) -> int:
        """Extract datamap index from ezlawyer's doChkDownloadEB(type,index)."""
        try:
            onclick = link.get_attribute("onclick") or ""
            match = re.search(r"doChkDownloadEB\s*\(\s*\d+\s*,\s*(\d+)\s*\)", onclick)
            if match:
                return int(match.group(1))
        except Exception:
            logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 2225, exc_info=True)
        return fallback_index

    def _download_pdf_via_query_form_post(self, datamap_index: int) -> Optional[str]:
        """Fallback for ezlawyer PDF links that submit queryForm via JavaScript."""
        try:
            payload_json = self.driver.execute_script(
                """
                var idx = arguments[0];
                var form = document.querySelector('#queryForm');
                if (!form) return '';
                var doquery = document.querySelector('#doquery');
                var datamap = document.querySelector('#datamap');
                var dw = document.querySelector('#dw');
                if (doquery) doquery.value = '2';
                if (datamap) datamap.value = String(idx);
                if (dw) dw.value = '';
                return JSON.stringify({
                    action: form.action || '/eb/user/downloadEB3',
                    fields: Array.from(new FormData(form).entries()).map(function(pair) {
                        return [String(pair[0] || ''), String(pair[1] || '')];
                    })
                });
                """,
                datamap_index,
            )
            if not payload_json:
                return None
            payload = json.loads(payload_json)
            action = str(payload.get("action") or "https://www.ezlawyer.com.tw/eb/user/downloadEB3")
            fields = [(str(k), str(v)) for k, v in (payload.get("fields") or []) if str(k)]
            if not fields:
                return None

            cookies = []
            try:
                for cookie in self.driver.get_cookies() or []:
                    name = str(cookie.get("name") or "").strip()
                    value = str(cookie.get("value") or "")
                    if name:
                        cookies.append(f"{name}={value}")
            except Exception:
                cookies = []

            body = urllib.parse.urlencode(fields, doseq=True).encode("utf-8")
            request = urllib.request.Request(
                action,
                data=body,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://www.ezlawyer.com.tw/eb/user/downloadEB2",
                    "Cookie": "; ".join(cookies),
                },
            )
            ssl_context = ssl._create_unverified_context()
            with urllib.request.urlopen(request, timeout=45, context=ssl_context) as response:
                blob = response.read()
                content_type = (response.headers.get("content-type") or "").lower()
                disposition = response.headers.get("content-disposition") or ""

            if not blob or (not blob.startswith(b"%PDF") and "pdf" not in content_type):
                snippet = blob[:120].decode("utf-8", "replace") if blob else ""
                self.log(f"    ⚠️ queryForm POST 未回 PDF (idx={datamap_index}, ct={content_type}, body={snippet[:60]})")
                return None

            filename = ""
            match = re.search(r"filename\\*?=(?:UTF-8''|\"?)([^\";]+)", disposition, re.I)
            if match:
                filename = urllib.parse.unquote(match.group(1).strip().strip('"'))
            if not filename.lower().endswith(".pdf"):
                filename = f"ezlawyer_transcript_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{datamap_index}.pdf"
            filename = re.sub(r'[\\\\/:*?"<>|]+', "_", filename)
            target = os.path.join(self.download_folder, filename)
            if os.path.exists(target):
                base, ext = os.path.splitext(filename)
                target = os.path.join(
                    self.download_folder,
                    f"{base}_{datetime.now().strftime('%H%M%S_%f')}{ext or '.pdf'}",
                )
            with open(target, "wb") as f:
                f.write(blob)
            self.log(f"    ✅ queryForm POST 下載完成: {os.path.basename(target)}")
            return target
        except Exception as e:
            self.log(f"    ⚠️ queryForm POST 下載失敗 (idx={datamap_index}): {e}")
            return None
    
    def _process_search_results(self, transcript_folder: str = None, case: CourtCase = None) -> List[str]:
        """處理搜尋結果並下載筆錄"""

        self.log(f"  進入處理搜尋結果 (process_search_results), target_folder={transcript_folder}")
        
        downloaded_files = []
        
        try:
            # 等待結果載入
            time.sleep(2)
            if (os.environ.get("MAGI_EZLAWYER_DEBUG", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}:
                try:
                    from api.debug_capture import save_debug_screenshot, save_debug_html
                    save_debug_screenshot(self.driver, "debug_search_page", context="筆錄搜尋頁")
                    save_debug_html(self.driver, "debug_search_page", context="筆錄搜尋頁")
                    self.log("  [DEBUG] Saved debug_search_page")
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1581, exc_info=True)
            
            # 記錄下載前的檔案
            existing_files = set(os.listdir(self.download_folder))
            
            # === Smart Skip Logic Start ===
            # 注意：此區塊只用於 Log 分析，不會影響實際下載
            # 實際的過濾已移除，改由 MD5 機制處理重複
            if transcript_folder and os.path.exists(transcript_folder):
                try:
                    # 分析本機已有的筆錄日期 (僅供參考)
                    local_dates = {}  # date -> count
                    for fname in os.listdir(transcript_folder):
                        if not _is_real_pdf_filename(fname):
                            continue
                        match = re.match(r'^(\d{8})', fname)
                        if match:
                            d = match.group(1)
                            y = int(d[:4]) - 1911
                            m = int(d[4:6])
                            day = int(d[6:8])
                            tw_date = f"{y}/{m:02d}/{day:02d}"
                            local_dates[tw_date] = local_dates.get(tw_date, 0) + 1
                    
                    if local_dates:
                        self.log(f"  ℹ️ 本機已有筆錄日期: {local_dates} (將全部下載並由 MD5 過濾重複)")
                    
                except Exception as e:
                    self.log(f"  ⚠️ Smart Skip 分析失敗: {e}")

            # === Smart Skip Logic End ===

            # 第一步：找「立即調閱」按鈕
            # 先全局搜尋按鈕，確認有沒有可點擊的項目
            all_intrace_buttons = self._case_scoped_result_elements(
                "//input[@type='button' and @value='立即調閱']",
                case,
            )
            
            self.log(f"  全局搜尋找到 {len(all_intrace_buttons)} 個「立即調閱」按鈕")
            
            if not all_intrace_buttons:
                self.log("  ℹ️ 沒有找到「立即調閱」按鈕，筆錄已可直接下載")
                # 直接從搜尋結果頁面下載所有 PDF
                # 支援 502 錯誤重試：重新搜尋並繼續下載
                max_502_retries = 5
                retry_count = 0
                total_clicked = 0
                
                while retry_count < max_502_retries:
                    downloaded_files_batch, clicked, total = self._download_pdfs_from_page(
                        existing_files,
                        start_index=total_clicked,
                        case=case,
                    )
                    downloaded_files.extend(downloaded_files_batch)
                    # The next retry must compare against files that have
                    # already landed during this case.  Keeping only the
                    # pre-case snapshot makes an earlier PDF look new again
                    # and duplicates the same path in the result list.
                    existing_files.update(os.listdir(self.download_folder))
                    total_clicked = clicked  # 更新已點擊數量
                    
                    # 檢查是否全部完成
                    if total == 0 or clicked >= total:
                        self.log(f"  ✅ 直接下載完成: {len(downloaded_files)}/{total} 個檔案")
                        break
                    
                    # 如果未完成，可能是 502 錯誤，嘗試重新搜尋
                    retry_count += 1
                    self.log(f"  ⚠️ 下載中斷於 {clicked}/{total}，等待 10 秒後重試 (第 {retry_count}/{max_502_retries} 次)")
                    time.sleep(10)  # 等待伺服器恢復
                    
                    # 重新搜尋
                    if case:
                        self.log(f"  🔄 重新搜尋案件...")
                        try:
                            _page = getattr(self.driver, "_page", None)
                            if _page is not None:
                                _page.goto(self.SEARCH_URL, timeout=15000)
                            else:
                                self.driver.get(self.SEARCH_URL)
                        except Exception as _ne:
                            self.log(f"  ⚠️ 重新搜尋導航逾時: {_ne}")
                            break
                        time.sleep(2)
                        yy, id_word, num = self._parse_case_number(case.court_case_number)
                        if yy and id_word and num:
                            try:
                                select_court = Select(self.driver.find_element(By.ID, "jud_name"))
                                for opt in select_court.options:
                                    if case.court_name in opt.text or opt.text in case.court_name:
                                        select_court.select_by_visible_text(opt.text)
                                        break
                                    
                                Select(self.driver.find_element(By.ID, "sys_id")).select_by_visible_text(case.case_type)
                                self.driver.find_element(By.ID, "eb_year").clear()
                                self.driver.find_element(By.ID, "eb_year").send_keys(yy)
                                self.driver.find_element(By.ID, "eb_id").clear()
                                self.driver.find_element(By.ID, "eb_id").send_keys(id_word)
                                self.driver.find_element(By.ID, "eb_num").clear()
                                self.driver.find_element(By.ID, "eb_num").send_keys(num)
                                self.driver.find_element(By.ID, "queryBtn").click()
                                time.sleep(3)
                                self._handle_alert()
                            except Exception as e:
                                self.log(f"  ❌ 重新搜尋失敗: {e}")
                                break
                    else:
                        # 沒有案件資訊，無法重新搜尋
                        self.log(f"  ❌ 無法重新搜尋（缺少案件資訊）")
                        break
                
                return downloaded_files
            
            # 如果有按鈕，遍歷並嘗試點擊
            intrace_buttons_indices = list(range(len(all_intrace_buttons)))
            
            # ★★★ 修正：移除日期跳過邏輯 ★★★
            # 舊邏輯只根據日期判斷，會錯誤跳過同一天不同類型的筆錄
            # (例如：本機有「審理程序筆錄」，網站上有「準備程序筆錄」，會被錯誤跳過)
            # 現在改為：下載所有項目，讓 MD5 檢查來過濾重複
            # 這樣可以確保不會漏下任何筆錄
            
            # 如果有筆錄資料夾，記錄已有的檔案供參考（但不跳過）
            if transcript_folder and os.path.exists(transcript_folder):
                existing_pdfs = [f for f in os.listdir(transcript_folder) if _is_real_pdf_filename(f)]
                if existing_pdfs:
                    self.log(f"  ℹ️ 本機已有 {len(existing_pdfs)} 個筆錄檔案 (將由 MD5 過濾重複)")
            
            # 不再過濾，全部嘗試下載
            
            if intrace_buttons_indices:
                self.log(f"  找到 {len(intrace_buttons_indices)} 個待下載項目")
            
            # 點擊每個立即調閱按鈕
            for loop_index, btn_index in enumerate(intrace_buttons_indices):
                try:
                    # 如果不是第一次迭代，需要重新搜尋以恢復頁面狀態 (Re-search Strategy)
                    if loop_index > 0:
                        self.log(f"  🔄 重新搜尋以處理下一個項目...")
                        try:
                            _page = getattr(self.driver, "_page", None)
                            if _page is not None:
                                _page.goto(self.SEARCH_URL, timeout=15000)
                            else:
                                self.driver.get(self.SEARCH_URL)
                        except Exception as _ne:
                            self.log(f"  ⚠️ 重新搜尋導航逾時: {_ne}")
                            break
                        time.sleep(1)
                        
                        # 重填表單 (複製自 initial search logic)
                        # 解析案號
                        # 重填表單 (複製自 initial search logic)
                        # 解析案號
                        yy, id_word, num = self._parse_case_number(case.court_case_number)
                        if yy and id_word and num:
                            
                            # 1. 選擇法院 (模糊比對)
                            select_court = Select(self.driver.find_element(By.ID, "jud_name"))
                            court_found = False
                            for opt in select_court.options:
                                if case.court_name.replace("臺灣", "") in opt.text:
                                    select_court.select_by_visible_text(opt.text)
                                    court_found = True
                                    break
                            if not court_found:
                                # Fallback exact match
                                try:
                                    select_court.select_by_visible_text(case.court_name)
                                except Exception:
                                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1728, exc_info=True)
                            time.sleep(0.5)
                            
                            # 2. 選擇類別
                            sys_type = "刑事" if "刑" in case.case_type else "民事"
                            Select(self.driver.find_element(By.ID, "sys_id")).select_by_visible_text(sys_type)
                            time.sleep(0.5)
                            
                            # 3. 填寫案號
                            self.driver.find_element(By.ID, "eb_year").send_keys(yy)
                            self.driver.find_element(By.ID, "eb_id").send_keys(id_word)
                            self.driver.find_element(By.ID, "eb_num").send_keys(num)
                            
                            # 查詢
                            self.driver.find_element(By.ID, "queryBtn").click()
                            time.sleep(3)
                            self._handle_alert()

                    # 重新全局搜尋按鈕
                    current_buttons = self._case_scoped_result_elements(
                        "//input[@type='button' and @value='立即調閱']",
                        case,
                    )
                    
                    if btn_index >= len(current_buttons):
                        self.log(f"  ⚠️ 按鈕索引 {btn_index} 超出範圍")
                        continue
                    
                    btn = current_buttons[btn_index]
                    self.log(f"  🔍 點擊第 {loop_index+1} 個調閱按鈕...")
                    
                    btn.click()
                    time.sleep(3)
                    
                    # 處理 alert
                    self._handle_alert()
                    
                    # ★★★ 深度救援迴圈 ★★★
                    current_start_index = 0
                    max_deep_retries = 3
                    deep_retry_count = 0
                    
                    while True:
                        # 下載 PDF
                        new_files, clicked, total = self._download_pdfs_from_page(
                            existing_files,
                            start_index=current_start_index,
                            case=case,
                        )
                        downloaded_files.extend(new_files)
                        existing_files.update(os.listdir(self.download_folder))
                        
                        # 檢查是否全部下載完成
                        if total == 0 or clicked >= total:
                            break
                        
                        # 如果未完成，且還有重試機會
                        if deep_retry_count < max_deep_retries:
                            deep_retry_count += 1
                            self.log(f"  ⚠️ 下載未完成 (已點擊 {clicked}/{total})，啟動深度救援 (第 {deep_retry_count}/{max_deep_retries} 次)...")
                            
                            try:
                                # 1. 重新執行搜尋
                                self.log("    🔄 [深度救援] 重新執行搜尋...")
                                if not self._execute_search_query(case):
                                    self.log("    ❌ [深度救援] 搜尋失敗，放棄")
                                    break
                                
                                time.sleep(2)
                                
                                # 2. 重新尋找並點擊按鈕
                                current_buttons_retry = self._case_scoped_result_elements(
                                    "//input[@type='button' and @value='立即調閱']",
                                    case,
                                )
                                if btn_index < len(current_buttons_retry):
                                    self.log(f"    🔍 [深度救援] 重新點擊第 {loop_index+1} 個按鈕...")
                                    current_buttons_retry[btn_index].click()
                                    time.sleep(3)
                                    self._handle_alert()
                                    
                                    # 3. 更新起始索引，準備下一輪下載
                                    current_start_index = clicked
                                    continue
                                else:
                                    self.log("    ❌ [深度救援] 找不回按鈕，放棄")
                                    break
                                    
                            except Exception as e:
                                self.log(f"    ❌ [深度救援] 發生錯誤: {e}")
                                break
                        else:
                            self.log("    ❌ 下載不完整，已達最大重試次數")
                            break
                    
                    # 不再使用 back()，下一次循環會觸發 re-search
                        
                except Exception as e:
                    self.log(f"  ⚠️ 處理第 {loop_index+1} 個項目時發生錯誤: {e}")
            
            if downloaded_files:
                self.log(f"  ✅ 共下載 {len(downloaded_files)} 個檔案")
            else:
                pass
            
        except Exception as e:
            self.log(f"  ⚠️ 處理搜尋結果時發生錯誤: {e}")
        
        return list(dict.fromkeys(str(path) for path in downloaded_files))
    
    def _download_pdfs_from_page(
        self,
        existing_files: set,
        start_index: int = 0,
        case: Optional[CourtCase] = None,
    ) -> Tuple[List[str], int, int]:
        """
        處理單一頁面上的 PDF 下載
        
        Args:
            existing_files: 既有檔案列表 (用於排除)
            start_index: 起始索引 (用於斷點續傳)
            
        Returns:
            (downloaded_files, clicked_count, total_pdfs)
        """
        downloaded_files = []
        clicked_count = start_index
        total_pdfs = 0
        self._last_pdf_fetch_count = 0
        self._last_pdf_known_duplicate_count = 0
        self._last_no_new_files_reason = ""
        
        try:
            # 取得 PDF 總數
            # 注意：這裡假設頁面結構是穩定的，總數不會變
            pdf_links = self._case_scoped_result_elements(
                "//a[contains(text(), '下載PDF')] | //a[contains(text(), 'PDF下載')] | //a[contains(text(), '線上下載')]",
                case,
            )
            
            if not pdf_links:
                if self._portal_has_explicit_unavailable_state(case):
                    self._last_no_new_files_reason = "portal_confirmed_unavailable"
                    self.log("    ℹ️ 目標案號列明確顯示目前不可下載")
                elif self._portal_has_explicit_empty_state():
                    self._last_no_new_files_reason = "portal_confirmed_empty"
                    self.log("    ℹ️ 入口明確顯示目前無筆錄資料")
                else:
                    self._last_download_error = (
                        "unverified_no_pdf_links: 結果頁未找到下載 PDF 連結，"
                        "且沒有可核對的空清單證據"
                    )
                    self.log("    ❌ 找不到「下載PDF」連結，且入口未明確回覆無資料")
                return downloaded_files, clicked_count, 0
            
            total_pdfs = len(pdf_links)
            
            if start_index == 0:
                self.log(f"    找到 {total_pdfs} 個 PDF 可下載")
            else:
                self.log(f"    接續下載: 從第 {start_index+1}/{total_pdfs} 個開始...")
            
            # 檢查索引範圍
            if start_index >= total_pdfs:
                self.log(f"    ℹ️ 起始索引 {start_index} 已超過總數 {total_pdfs}")
                return downloaded_files, clicked_count, total_pdfs
            
            # 使用初始總數作為終止條件
            max_stale_retries = 3
            stale_retry_count = 0
            
            # 防止伺服器過載：每次點擊後等待
            CLICK_DELAY = 3  # 秒，避免 502 錯誤
            clicked_datamap_indices: List[int] = []
            
            while clicked_count < total_pdfs:
                try:
                    # 檢查頁面是否出現 502 錯誤
                    page_source = self.driver.page_source
                    if '502' in page_source and ('Proxy Error' in page_source or 'Error' in self.driver.title):
                        self.log(f"    ⚠️ 偵測到 502 伺服器錯誤，需要重新搜尋")
                        # 回傳目前已點擊的數量，讓上層決定是否重試
                        return downloaded_files, clicked_count, total_pdfs
                    
                    # 每次循環都重新獲取連結（避免 stale element）
                    pdf_links = self._case_scoped_result_elements(
                        "//a[contains(text(), '下載PDF')] | //a[contains(text(), 'PDF下載')] | //a[contains(text(), '線上下載')]",
                        case,
                    )
                    
                    current_link_count = len(pdf_links)
                    
                    if not pdf_links:
                        # 連結消失，先檢查是否為 502 錯誤
                        if '502' in self.driver.page_source or 'Error' in self.driver.title:
                            self.log(f"    ⚠️ 伺服器錯誤導致連結消失 (已點擊 {clicked_count}/{total_pdfs})")
                            return downloaded_files, clicked_count, total_pdfs
                        
                        # 嘗試返回上一頁救援
                        if stale_retry_count < max_stale_retries:
                            stale_retry_count += 1
                            self.log(f"    ⚠️ 頁面連結消失，嘗試返回上一頁 ({stale_retry_count}/{max_stale_retries})...")
                            self.driver.back()
                            time.sleep(3)
                            continue
                        else:
                            self.log(f"    ❌ 頁面連結消失無法恢復 (已點擊 {clicked_count}/{total_pdfs})")
                            break
                    
                    # 重置重試計數器（連結存在）
                    stale_retry_count = 0
                    
                    # 計算要點擊的連結索引
                    # 注意：有些網站點完後連結會消失，所以總是從頭開始找
                    link_index = min(clicked_count, current_link_count - 1)
                    
                    # 如果當前連結數少於預期，可能是因為點擊後連結消失
                    if current_link_count < total_pdfs - clicked_count:
                        # 總是嘗試點第一個可用的連結
                        link_index = 0
                    
                    link = pdf_links[link_index]
                    datamap_index = self._extract_pdf_link_datamap_index(link, clicked_count)
                    
                    self.log(f"    📥 下載 PDF #{clicked_count+1}/{total_pdfs} (頁面剩餘 {current_link_count} 個連結)...")
                    
                    # 取得目前視窗數量
                    original_windows = self.driver.window_handles
                    
                    # 使用 JavaScript 點擊（更可靠）
                    try:
                        self.driver.execute_script("arguments[0].click();", link)
                    except Exception:
                        # 備用：直接點擊
                        link.click()
                    
                    # ★★★ 重要：增加延遲以避免 502 錯誤 ★★★
                    time.sleep(CLICK_DELAY)
                    
                    # 檢查是否開了新視窗
                    new_windows = self.driver.window_handles
                    if len(new_windows) > len(original_windows):
                        new_window = [w for w in new_windows if w not in original_windows][0]
                        self.driver.switch_to.window(new_window)
                        time.sleep(1)
                        self.driver.close()
                        self.driver.switch_to.window(original_windows[0])
                        time.sleep(1)
                    
                    # 處理可能出現的 alert
                    self._handle_alert()
                    
                    clicked_datamap_indices.append(datamap_index)
                    clicked_count += 1
                    
                except StaleElementReferenceException:
                    self.log(f"    ⚠️ 元素已過期，重新取得連結...")
                    time.sleep(1)
                    # 不增加 clicked_count，重試本次
                    continue
                    
                except Exception as e:
                    self.log(f"    ⚠️ 下載 PDF #{clicked_count+1} 時發生錯誤: {e}")
                    clicked_count += 1  # 跳過這個繼續下一個
            
            # 只有當真的有進行點擊操作時才等待
            if clicked_count > start_index:
                wait_count = clicked_count - start_index
                self.log(f"    ⏳ 等待 {wait_count} 個新檔案下載完成...")
                max_wait_seconds = max(30, wait_count * 10)
                waited = 0
                
                while waited < max_wait_seconds:
                    temp_files = [f for f in os.listdir(self.download_folder) if f.endswith('.crdownload')]
                    if not temp_files:
                        break
                    time.sleep(2)
                    waited += 2

                # ezlawyer's PDF links are JavaScript form submits. In headless
                # browser contexts they can silently no-op, so verify whether any
                # PDF landed and fall back to the same queryForm POST with the
                # authenticated browser cookies.
                current_pdf_files = {
                    f for f in os.listdir(self.download_folder)
                    if _is_real_pdf_filename(f)
                }
                if not (current_pdf_files - {f for f in existing_files if _is_real_pdf_filename(f)}):
                    for datamap_index in clicked_datamap_indices:
                        if self._download_pdf_via_query_form_post(datamap_index):
                            self._last_pdf_fetch_count += 1
            
            # 檢查新下載的檔案
            current_files = set(os.listdir(self.download_folder))
            new_files = current_files - existing_files
            
            # ★★★ 即時去重：下載完後立即比對 MD5 記錄，避免重複堆積 ★★★
            md5_records = self._load_md5_records()

            for filename in new_files:
                if not _is_real_pdf_filename(filename):
                    continue
                filepath = os.path.join(self.download_folder, filename)
                md5 = self._calculate_file_md5(filepath)
                if md5 and md5 in md5_records:
                    self._last_pdf_known_duplicate_count += 1
                    known = md5_records[md5].get("filename", "?")
                    self.log(f"    ℹ️ 已知檔案（{known}），跳過: {filename}")
                    try:
                        os.remove(filepath)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1991, exc_info=True)
                else:
                    downloaded_files.append(filepath)
                    self.log(f"    ✅ 下載完成: {filename}")

            if not downloaded_files and self._last_pdf_fetch_count and self._last_pdf_known_duplicate_count:
                self._last_no_new_files_reason = "known_duplicates"

        except Exception as e:
            self.log(f"    ⚠️ 下載 PDF 時發生錯誤: {e}")

        return downloaded_files, clicked_count, total_pdfs
    
    def _download_pdfs(self):

        try:
            # 尋找下載 PDF 連結
            pdf_links = self.driver.find_elements(
                By.XPATH, "//a[contains(text(), '下載PDF')] | //a[contains(text(), 'PDF')] | //a[contains(@href, '.pdf')]"
            )
            
            for link in pdf_links:
                try:
                    link.click()
                    time.sleep(2)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2014, exc_info=True)
                    
        except Exception as e:
            self.log(f"  ⚠️ 下載 PDF 時發生錯誤: {e}")
    
    def download_all(self) -> Dict[str, Any]:

        results = {"success": 0, "failed": 0, "cases": [], "files": []}

        # ★ 先清理下載資料夾中的重複檔案，避免再次下載已存在的內容
        try:
            cleanup = self.cleanup_download_folder()
            results["cleanup"] = cleanup
        except Exception as e:
            self.log(f"  ⚠️ 清理下載資料夾失敗: {e}")

        if not self.login():
            return results

        cases = self.get_cases_from_db()

        # 防呆：避免 nightly 因 Selenium/網路卡住而無限跑。
        try:
            max_runtime_sec = int(os.environ.get("MAGI_EZLAWYER_DOWNLOAD_MAX_RUNTIME_SEC", "1800") or "1800")
        except Exception:
            max_runtime_sec = 1800
        started = time.monotonic()

        for idx, case in enumerate(cases, 1):
            if max_runtime_sec > 0 and (time.monotonic() - started) > max_runtime_sec:
                results["timed_out"] = True
                results["elapsed_sec"] = round(time.monotonic() - started, 2)
                self.log(f"⏱️ 已超過最大執行時間 {max_runtime_sec}s，停止後續案件（已處理 {idx-1}/{len(cases)}）。")
                break

            _download_ok = True
            try:
                downloaded_files = self.download_record(case)
            except Exception as _dl_exc:
                self.log(f"  ❌ download_record exception: {_dl_exc}")
                downloaded_files = []
                _download_ok = False

            if downloaded_files:
                results["success"] += 1

                # ★★★ 暴力模式：下載完馬上歸檔移入，不等待
                self.move_to_case_folder(case, downloaded_files)

                results["files"].extend(downloaded_files)
            elif _download_ok:
                # Query succeeded but all files were deduped — count as success (noop)
                results["success"] += 1
            else:
                results["failed"] += 1

            results["cases"].append({
                "case_number": case.case_number,
                "court_case_number": case.court_case_number,
                "client_name": getattr(case, "client_name", ""),
                "files": downloaded_files,
                "success": _download_ok,
            })

            time.sleep(2)

        self.close()
        return results
    
    def _parse_record_pdf(self, filepath: str) -> Dict[str, Optional[str]]:
        """
        解析筆錄 PDF 第一頁，提取日期、類型、開庭時間
        
        Returns:
            dict: {'date': 'YYYYMMDD', 'type': '審理程序筆錄', 'period': '上午', 'time': '0930'}
        """
        result = {'date': None, 'type': None, 'period': None, 'time': None}
        # The rename caller must be able to distinguish an ordinary
        # "metadata not found" result from an unreadable/corrupt PDF.  The
        # former may be retried by OCR/AI; the latter used to be swallowed and
        # the whole rename step was falsely reported as successful.
        self._last_record_parse_error = ""
        doc = None
        
        try:
            # Cloud/File Provider placeholders can look like ordinary, non-zero
            # files to lstat while the first real read raises a timeout.  Do a
            # minimal read before handing the path to PyMuPDF so that an
            # unavailable source is not mislabeled as a corrupt PDF.  The
            # caller keeps these items retryable and never renames them.
            with open(filepath, "rb") as source_file:
                source_file.read(8)

            import fitz  # PyMuPDF
            
            doc = fitz.open(filepath)
            if len(doc) == 0:
                self._last_record_parse_error = "empty_pdf"
                return result
            
            # 主解析仍使用第一頁；另保留前三頁文字作為「封面頁」格式的
            # 嚴格備援。備援必須在同一頁同時找到筆錄類型與作成日。
            page = doc[0]
            page_texts = [
                str(doc[index].get_text() or "")
                for index in range(min(3, len(doc)))
            ]
            
            # ★★★ 改進：裁切左側行號區域 ★★★
            # 法院筆錄的行號通常在左側約 8% 的寬度範圍內
            # 使用 clip 裁切掉左側區域後再提取文字
            page_rect = page.rect  # 頁面完整區域
            page_width = page_rect.width
            
            # 裁切區域：使用頁面寬度的 8% 作為左側裁切量
            # A4 (595 pt): 8% = 47.6 pt
            # Letter (612 pt): 8% = 49 pt
            LEFT_MARGIN_RATIO = 0.08  # 裁切掉左側 8% 的寬度
            TOP_RATIO = 0.30  # ★ 新增：只讀取上方 30% 的高度
            left_crop = page_width * LEFT_MARGIN_RATIO
            page_height = page_rect.height
            
            # ★★★ 裁切區域：左側 8% + 只取上方 30% ★★★
            clip_rect = fitz.Rect(
                page_rect.x0 + left_crop,  # 左邊界往右移（排除行號）
                page_rect.y0,               # 上邊界不變
                page_rect.x1,               # 右邊界不變
                page_rect.y0 + (page_height * TOP_RATIO)  # ★ 下邊界限制在上方 30%
            )
            
            # 使用裁切區域提取文字（排除行號 + 只取上方）
            text = page.get_text(clip=clip_rect)
            
            # 也保留完整原始文字作為備用
            raw_text = page_texts[0]
            doc.close()
            
            # ★★★ 1. 提取開庭日期 - 多階段解析策略 ★★★
            
            # 策略 A：嘗試完整格式（無空格）- 最可靠
            date_found = False
            compact_patterns = [
                r'中華民國(\d{3})年(\d{1,2})月(\d{1,2})日',
                r'民國(\d{3})年(\d{1,2})月(\d{1,2})日',
            ]
            
            for pattern in compact_patterns:
                match = re.search(pattern, text)
                if match:
                    try:
                        roc_year = int(match.group(1))
                        month = int(match.group(2))
                        day = int(match.group(3))
                        year = roc_year + 1911
                        
                        if _TRANSCRIPT_MIN_YEAR <= year <= _TRANSCRIPT_MAX_YEAR and 1 <= month <= 12 and 1 <= day <= 31:
                            result['date'] = f"{year:04d}{month:02d}{day:02d}"
                            self.log(f"  📅 解析日期: {year}/{month}/{day} (來源: {os.path.basename(filepath)})")
                            date_found = True
                            break
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2146, exc_info=True)
            
            # 策略 B：如果策略 A 失敗，嘗試帶空格的格式
            # ★★★ 改進：先將 clipped text 的換行移除再合併數字 ★★★
            if not date_found:
                # 移除所有換行和多餘空格，但保留單一空格
                normalized_text = re.sub(r'\s+', ' ', text)

                # 提取「中華民國...日」或「民國...日」片段，移除空格後解析
                roc_date_patterns = [
                    r'中\s*華\s*民\s*國\s*([\d\s]+)年\s*([\d\s]+)月\s*([\d\s]+)日',
                    r'民\s*國\s*([\d\s]+)年\s*([\d\s]+)月\s*([\d\s]+)日',
                ]
                for roc_pat in roc_date_patterns:
                    roc_fragment = re.search(roc_pat, normalized_text)
                    if roc_fragment:
                        try:
                            roc_year = int(roc_fragment.group(1).replace(' ', ''))
                            month = int(roc_fragment.group(2).replace(' ', ''))
                            day = int(roc_fragment.group(3).replace(' ', ''))
                            year = roc_year + 1911

                            if 1 <= roc_year <= (_TRANSCRIPT_MAX_YEAR - 1911) and 1 <= month <= 12 and 1 <= day <= 31:
                                result['date'] = f"{year:04d}{month:02d}{day:02d}"
                                self.log(f"  📅 解析日期: {year}/{month}/{day} (來源: {os.path.basename(filepath)})")
                                date_found = True
                                break
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2174, exc_info=True)
            
            # 策略 C：使用原始文字（raw_text）— 同樣用 fragment 提取法
            # 先移除行號行，再正規化後提取日期片段
            if not date_found:
                # 移除獨立行號行（1~3 位數字，可能有空格如 "0 4"）
                raw_no_linenum = re.sub(r'(?m)^\s*\d[\s\d]{0,4}\s*$', '', raw_text)
                raw_normalized = re.sub(r'\s+', ' ', raw_no_linenum)
                for roc_pat in [
                    r'中\s*華\s*民\s*國\s*([\d\s]+)年\s*([\d\s]+)月\s*([\d\s]+)日',
                    r'民\s*國\s*([\d\s]+)年\s*([\d\s]+)月\s*([\d\s]+)日',
                ]:
                    roc_fragment = re.search(roc_pat, raw_normalized)
                    if roc_fragment:
                        try:
                            roc_year = int(roc_fragment.group(1).replace(' ', ''))
                            month = int(roc_fragment.group(2).replace(' ', ''))
                            day = int(roc_fragment.group(3).replace(' ', ''))
                            year = roc_year + 1911

                            if 1 <= roc_year <= (_TRANSCRIPT_MAX_YEAR - 1911) and 1 <= month <= 12 and 1 <= day <= 31:
                                result['date'] = f"{year:04d}{month:02d}{day:02d}"
                                self.log(f"  📅 解析日期: {year}/{month}/{day} (來源: {os.path.basename(filepath)})")
                                date_found = True
                                break
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2200, exc_info=True)
            
            # 2. 提取筆錄性質 - 使用原始文字（raw_text）移除空白後比對
            # ★★★ 修正：使用 raw_text 而非 filtered text，因為標題可能有空格如「審 判 筆 錄」★★★
            normalized_text = re.sub(r'\s+', '', raw_text)  # 移除所有空白/換行
            
            type_keywords = [
                '準備程序筆錄',
                '言詞辯論筆錄', 
                '審理程序筆錄',
                '審判筆錄',
                '訊問筆錄',
                '調解程序筆錄',
                '勘驗筆錄',
                '和解程序筆錄',
                '調查程序筆錄',
                '移交筆錄',
                '宣示判決筆錄',  # 新增
                '調查筆錄',      # 新增
                '協商會議記錄',  # 國民法官案件協商程序
                '消債調查筆錄',  # 消費者債務清理
            ]
            
            for keyword in type_keywords:
                if keyword in normalized_text:
                    result['type'] = keyword
                    break
            
            # 如果沒找到特定類型，fallback 到「筆錄」
            if not result['type']:
                if '筆錄' in normalized_text:
                    result['type'] = '筆錄'
                    self.log(f"  ⚠️ 筆錄類型未找到匹配，使用預設【筆錄】")
            
            # 3. 提取開庭時間 - 格式: 上午9時30分 或 下午2時15分 或 上午 9 時 3 0 分
            time_patterns = [
                # 標準格式: 上午9時30分
                r'(上\s*午|下\s*午)\s*([\d\s]+)\s*[時:時]\s*([\d\s]*)\s*分?',
                # 備用格式: 09:30
                r'(上\s*午|下\s*午)\s*([\d]+)[:\s]*([\d]+)',
            ]
            
            for pattern in time_patterns:
                match = re.search(pattern, text)
                if match:
                    period_raw = match.group(1).replace(' ', '').replace('\n', '')
                    hour_str = match.group(2).replace(' ', '').replace('\n', '')
                    minute_str = match.group(3).replace(' ', '').replace('\n', '') if match.group(3) else '00'
                    
                    # 設定時段
                    if '上午' in period_raw:
                        result['period'] = '上午'
                    elif '下午' in period_raw:
                        result['period'] = '下午'
                    
                    # 解析並儲存完整時間
                    try:
                        hour = int(hour_str)
                        minute = int(minute_str) if minute_str else 0
                        
                        # 確保時分合理 (時: 1-12, 分: 0-59)
                        if 1 <= hour <= 12 and 0 <= minute <= 59:
                            result['time'] = f"{hour:02d}{minute:02d}"
                            self.log(f"  🕐 解析時間: {result['period']}{hour}時{minute}分")
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2265, exc_info=True)
                    
                    break
            
            # 4. 封面頁格式與中文數字日期的可核對備援。
            # 只在同頁同時出現筆錄類型與作成日時才補值。
            deterministic_fallback = _extract_transcript_metadata_from_text_pages(page_texts)
            if not result.get('date') and deterministic_fallback.get('date'):
                result['date'] = deterministic_fallback['date']
            if not result.get('type') and deterministic_fallback.get('type'):
                result['type'] = deterministic_fallback['type']
            if not result.get('period') and deterministic_fallback.get('period'):
                result['period'] = deterministic_fallback['period']
            if not result.get('time') and deterministic_fallback.get('time'):
                result['time'] = deterministic_fallback['time']

            # 5. CASPER Fallback：只在確定性解析仍不完整時使用 AI 輔助。
            # 判斷是否需要 Gemini 輔助：
            # 1. 日期或類型缺失
            # 2. period 只有「上午/下午」沒有完整時間（如「上午0930」）
            period_needs_time = result['period'] in ['上午', '下午', None, '']
            needs_gemini = not result['date'] or not result['type'] or period_needs_time
            
            use_casper_assist = os.environ.get("MAGI_RECORD_PARSE_CASPER_ASSIST", "1").strip().lower() in {"1", "true", "yes", "on"}
            if needs_gemini and use_casper_assist:
                # 判斷是否為掃描檔 (文字極少)
                if len(text.strip()) < 50:
                    self.log("  👁️ 偵測到掃描檔，嘗試 Vision API 解析圖片...")
                    gemini_result = self._parse_with_vision(filepath)
                    if not gemini_result:
                        # Fallback to cached gemini if vision fails completely
                        gemini_result = self._parse_with_gemini_cached(text)
                else:
                    self.log("  🤖 正則解析不完整，嘗試 CASPER 輔助...")
                    gemini_result = self._parse_with_gemini_cached(text)
                
                gemini_result = _sanitize_transcript_parse_result(gemini_result)
                if any(gemini_result.values()):
                    # 只填補缺失的欄位
                    if not result['date'] and gemini_result.get('date'):
                        result['date'] = gemini_result['date']
                        self.log(f"  📅 [AI Assist] 解析日期: {result['date']}")
                    if not result['type'] and gemini_result.get('type'):
                        result['type'] = gemini_result['type']
                        self.log(f"  📝 [AI Assist] 解析類型: {result['type']}")
                    # 只補缺漏欄位；若 OCR 與 AI 的上午／下午衝突，保留
                    # OCR 並拒絕混合兩組時間。
                    gemini_period = gemini_result.get('period', '')
                    gemini_time = gemini_result.get('time', '')
                    if not result['period'] and gemini_period:
                        result['period'] = gemini_period
                    if (
                        not result.get('time')
                        and gemini_time
                        and result.get('period') == gemini_period
                    ):
                        result['time'] = gemini_time
                        self.log(f"  🕐 [AI Assist] 解析時間: {gemini_period}{gemini_time}")
            
        except ImportError:
            self._last_record_parse_error = "pdf_parser_unavailable"
            self.log("  ⚠️ 需要安裝 PyMuPDF (fitz) 才能解析 PDF")
        except TimeoutError:
            self._last_record_parse_error = "pdf_source_unavailable"
            self.log("  ⚠️ PDF 來源尚未可讀，保留原檔等待同步後重試")
        except OSError as e:
            transient_errnos = {
                errno.EAGAIN,
                errno.EBUSY,
                errno.EIO,
                errno.ESTALE,
                errno.ETIMEDOUT,
                errno.ENETDOWN,
                errno.ENETUNREACH,
            }
            if getattr(e, "errno", None) in transient_errnos:
                self._last_record_parse_error = "pdf_source_unavailable"
                self.log("  ⚠️ PDF 來源暫時無法取得，保留原檔等待後續重試")
            else:
                self._last_record_parse_error = "pdf_unreadable"
                self.log("  ⚠️ PDF 讀取失敗，保留原檔並等待檢查")
        except Exception as e:
            self._last_record_parse_error = "pdf_unreadable"
            self.log(f"  ⚠️ 解析 PDF 失敗: {e}")
        finally:
            try:
                if doc is not None and not bool(getattr(doc, "is_closed", False)):
                    doc.close()
            except (OSError, RuntimeError, ValueError):
                logging.getLogger(__name__).debug(
                    "failed to close transcript PDF parser handle",
                    exc_info=True,
                )
        
        return result
    
    def _parse_with_gemini(self, text: str) -> Dict[str, Optional[str]]:
        """
        使用 CASPER 解析筆錄日期與類型（保留函式名以相容舊流程）
        
        Args:
            text: PDF 上方 30% 的文字內容
        
        Returns:
            {'date': 'YYYYMMDD', 'type': '審理程序筆錄', 'period': '上午', 'time': 'HHMM'}
        """
        import json as json_lib

        prompt = (
            "分析以下法院筆錄文字，提取資訊並以 JSON 回覆。\n\n"
            "提取規則：\n"
            "1) date: 開庭日期，民國年轉換為西元年，格式 YYYYMMDD（如民國113年12月21日→20241221）\n"
            "2) type: 筆錄類型（如：審理程序筆錄、準備程序筆錄、言詞辯論筆錄等）\n"
            "3) period: 上午 或 下午\n"
            "4) time: 開庭時間 HHMM（如 0930）\n\n"
            "回覆格式（只回覆 JSON，不要其他文字）：\n"
            "{\"date\":\"YYYYMMDD\",\"type\":\"筆錄類型\",\"period\":\"上午\",\"time\":\"HHMM\"}\n\n"
            "文字內容：\n"
            + (text or "")[:1500]
        )

        # 重要：夜間任務不允許在本機工具端點異常時卡住太久。
        # 這裡設較短 timeout，失敗就直接回退到正則解析結果。
        try:
            tsec = int(os.environ.get("MAGI_CASPER_PARSE_TIMEOUT_SEC", "20") or "20")
        except Exception:
            tsec = 20
        try:
            from skills.bridge.inference_gateway import InferenceGateway

            r = InferenceGateway().chat(
                prompt,
                task_type="transcribe",
                timeout=tsec,
                allow_synthetic_fallback=False,
            )
        except Exception as e:
            self.log(f"  ⚠️ [CASPER Gateway] 呼叫異常(略過): {str(e)[:120]}")
            return None
        if not isinstance(r, dict) or not r.get("success"):
            self.log(f"  ⚠️ [CASPER Gateway] 呼叫失敗: {(r.get('error') if isinstance(r, dict) else '')}")
            return None

        response_text = (r.get("response") or "").strip()
        if not response_text:
            return None

        # Strip code fences if present.
        if "```json" in response_text:
            response_text = response_text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```", 1)[1].split("```", 1)[0].strip()

        parsed = _decode_transcript_model_payload(response_text)
        if parsed is None:
            self.log("  ⚠️ [CASPER] 結構化回應無有效 JSON 物件，已安全略過")
            return None

        # Some models may return a list wrapper.
        if isinstance(parsed, list):
            parsed = parsed[0] if (parsed and isinstance(parsed[0], dict)) else None
        if not isinstance(parsed, dict):
            return None
        return parsed

    def _parse_with_vision(self, filepath: str) -> Dict[str, Optional[str]]:
        """
        使用 Vision API 解析圖檔性質的筆錄 PDF（當文字提取失敗時使用）
        """
        import json as json_lib
        import fitz
        
        try:
            doc = fitz.open(filepath)
            if len(doc) == 0:
                return None
            page = doc[0]
            # render top 40% of first page
            rect = page.rect
            clip = fitz.Rect(rect.x0, rect.y0, rect.x1, rect.height * 0.4)
            pix = page.get_pixmap(dpi=150, clip=clip)
            png_bytes = pix.tobytes("png")
            doc.close()
            
            prompt = (
                "分析以下法院筆錄圖片，提取資訊並以 JSON 回覆。\n\n"
                "提取規則：\n"
                "1) date: 開庭日期，民國年轉換為西元年，格式 YYYYMMDD（如民國113年12月21日→20241221）\n"
                "2) type: 筆錄類型（如：審理程序筆錄、準備程序筆錄等）\n"
                "3) period: 上午 或 下午\n"
                "4) time: 開庭時間 HHMM（如 0930）\n\n"
                "回覆格式（只回覆 JSON）：\n"
                "{\"date\":\"YYYYMMDD\",\"type\":\"...\",\"period\":\"...\",\"time\":\"...\"}"
            )
            rendered_path = ""
            try:
                with tempfile.NamedTemporaryFile(prefix="magi-transcript-", suffix=".png", delete=False) as tmp:
                    tmp.write(png_bytes)
                    rendered_path = tmp.name
                if str(os.environ.get("MAGI_CODEX_CONTEXT") or "").strip().lower() != "transcript":
                    os.environ["MAGI_CODEX_CONTEXT"] = "transcript"
                from skills.bridge.inference_gateway import InferenceGateway

                gateway = InferenceGateway()
                gw_result = gateway.vision(
                    image_path=rendered_path,
                    prompt=prompt,
                    timeout=max(20, int(os.environ.get("MAGI_PDF_NAMER_STAMP_VISION_TIMEOUT", 30) or "30")),
                    task_type="ocr",
                )
                gw_text = str(
                    gw_result.get("analysis")
                    or gw_result.get("response")
                    or gw_result.get("text")
                    or ""
                ).strip()
                if gw_result.get("success") and gw_text:
                    parsed = _decode_transcript_model_payload(gw_text)
                    if isinstance(parsed, dict):
                        self.log(
                            f"  👁️ [Vision] InferenceGateway 解析成功 route={gw_result.get('route', '')} "
                            f"model={gw_result.get('model', '')}"
                        )
                        return parsed
                self.log(
                    f"  ⚠️ [Vision] InferenceGateway 失敗: "
                    f"{str(gw_result.get('error') or 'empty_response')[:160]}"
                )
            except Exception as gateway_err:
                self.log(f"  ⚠️ [Vision] InferenceGateway 異常: {gateway_err}")
            finally:
                if rendered_path:
                    try:
                        os.unlink(rendered_path)
                    except OSError:
                        logging.getLogger(__name__).debug(
                            "vision temporary image cleanup failed: %s",
                            rendered_path,
                            exc_info=True,
                        )
            return None
        except Exception as e:
            self.log(f"  ⚠️ [Vision] 呼叫異常: {e}")
            return None
    
    def _load_gemini_cache(self) -> Dict:
        """載入 Gemini 解析快取"""
        if hasattr(self, 'gemini_cache_file') and os.path.exists(self.gemini_cache_file):
            try:
                with open(self.gemini_cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2472, exc_info=True)
        return {}
    
    def _save_gemini_cache(self):
        """儲存 Gemini 解析快取"""
        if not hasattr(self, 'gemini_cache_file'):
            return
        try:
            with open(self.gemini_cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.gemini_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"  ⚠️ 儲存 Gemini 快取失敗: {e}")
    
    def _get_text_hash(self, text: str) -> str:
        """計算文字的 MD5 雜湊"""
        import hashlib
        return hashlib.md5(text.encode('utf-8')).hexdigest()
    
    def _parse_with_gemini_cached(self, text: str, file_md5: str = None) -> Dict[str, Optional[str]]:
        """
        使用 Gemini AI 解析筆錄（含 MD5 快取）
        
        Args:
            text: PDF 上方 30% 的文字內容
            file_md5: 檔案的 MD5（可選，用於快取識別）
        """
        # 計算文字雜湊作為快取鍵（或使用檔案 MD5）
        cache_key = file_md5 if file_md5 else self._get_text_hash(text)
        
        # ★ 檢查快取
        if hasattr(self, 'gemini_cache') and cache_key in self.gemini_cache:
            cached = self.gemini_cache[cache_key]
            sanitized = _sanitize_transcript_parse_result(cached)
            if sanitized != cached:
                self.gemini_cache[cache_key] = sanitized
                self._save_gemini_cache()
                self.log("  🧹 [CASPER] 已隔離快取中的無效日期／時間")
            self.log(f"  💾 [CASPER] 使用快取結果 (命中)")
            return sanitized
        
        # 調用 CASPER (保留函式名以相容舊流程)
        result = _sanitize_transcript_parse_result(self._parse_with_gemini(text))
        
        # ★ 儲存到快取
        if any(result.values()) and hasattr(self, 'gemini_cache'):
            self.gemini_cache[cache_key] = result
            self._save_gemini_cache()
            self.log(f"  💾 [CASPER] 已快取解析結果")
        
        return result


    
    def find_transcript_folder(self, case_folder_path: str) -> Optional[str]:
        if not case_folder_path or not os.path.exists(case_folder_path):
            return None
        
        try:
            # 列出案件資料夾中的所有子資料夾
            for item in os.listdir(case_folder_path):
                item_path = os.path.join(case_folder_path, item)
                
                # 只檢查資料夾
                if not os.path.isdir(item_path):
                    continue
                
                # 檢查資料夾名稱是否包含「筆錄」
                if '筆錄' in item:
                    return item_path
            
            # 找不到，創建「筆錄」資料夾（不硬編碼編號，因為各案件編號不同）
            default_folder = os.path.join(case_folder_path, "筆錄")
            os.makedirs(default_folder, exist_ok=True)
            self.log(f"  📁 建立筆錄資料夾: {default_folder}")
            return default_folder
            
        except Exception as e:
            self.log(f"  ⚠️ 尋找筆錄資料夾失敗: {e}")
            return None

    def _case_local_path_candidates(self, folder_path: str) -> List[str]:
        paths: List[str] = []

        def _add(path_value: str):
            p = str(path_value or "").strip()
            if not p or p in paths:
                return
            paths.append(p)

        try:
            for candidate in local_case_path_candidates(folder_path):
                _add(candidate)
        except Exception:
            logging.getLogger(__name__).debug("transcript path candidate expansion failed", exc_info=True)
        try:
            _add(translate_case_path_to_local(folder_path))
        except Exception:
            logging.getLogger(__name__).debug("transcript primary path translation failed", exc_info=True)
        try:
            if self.db and hasattr(self.db, "translate_path_to_local"):
                _add(self.db.translate_path_to_local(folder_path))
        except Exception:
            logging.getLogger(__name__).debug("transcript db path translation failed", exc_info=True)
        _add(folder_path)
        return paths

    def _find_existing_transcript_folder(self, case_folder_path: str) -> Optional[str]:
        return _find_existing_transcript_folder_path(case_folder_path)

    def _all_existing_transcript_folders(self, case: CourtCase, preferred_folder: str = "") -> List[str]:
        folders: List[str] = []

        def _add(folder_value: str):
            f = str(folder_value or "").strip()
            if f and os.path.isdir(f) and f not in folders:
                folders.append(f)

        _add(preferred_folder)
        if not getattr(case, "folder_path", ""):
            return folders
        for candidate in self._case_local_path_candidates(case.folder_path):
            _add(self._find_existing_transcript_folder(candidate) or "")
        return folders

    def _generate_record_filename(self, parse_result: Dict[str, Optional[str]], original_filename: str) -> str:
        """
        生成筆錄標準檔名
        
        格式優先順序:
        1. 有完整時間: 20251221 審理程序筆錄(下午0230).pdf
        2. 只有時段: 20251221 審理程序筆錄(下午).pdf
        3. 無時間資訊: 20251221 審理程序筆錄.pdf
        """
        date_str = parse_result.get('date')
        record_type = parse_result.get('type', '筆錄')
        period = parse_result.get('period', '')
        time_str = parse_result.get('time', '')  # 新增: 開庭時間 (格式: 0930)

        if not _record_parse_ready_for_filename(parse_result):
            self.log(f"  ⚠️ 筆錄解析結果不足，保留原檔名: {original_filename}")
            return original_filename
        
        # 確保有日期
        if not date_str:
            # ★★★ BUG FIX: 不再使用下載日期！改用辨識標記 ★★★
            date_str = '00000000'
            self.log(f"  ⚠️ 【日期解析失敗】無法從 PDF 提取作成日，標記 00000000")
        
        # 組合檔名 - 優先使用精確時間
        if period and time_str:
            # 完整格式: 日期 筆錄類型(時段+時間) - 如: 20251221 審理程序筆錄(下午0230).pdf
            filename = f"{date_str} {record_type}({period}{time_str}).pdf"
        elif period:
            # 只有時段: 日期 筆錄類型(時段) - 如: 20251221 審理程序筆錄(下午).pdf
            filename = f"{date_str} {record_type}({period}).pdf"
        else:
            # 無時間資訊: 日期 筆錄類型 - 如: 20251221 審理程序筆錄.pdf
            filename = f"{date_str} {record_type}.pdf"
        
        return filename

    def _transcript_pdf_matches_case(self, filepath: str, case: CourtCase) -> bool:
        try:
            import fitz

            document = fitz.open(filepath)
            try:
                text = "\n".join(
                    str(document[index].get_text() or "")
                    for index in range(min(3, len(document)))
                )
            finally:
                document.close()
        except Exception as exc:
            self.log(f"  ❌ 無法取得 PDF 內容作案件核對: {str(exc)[:120]}")
            return False
        return _transcript_text_matches_case(
            text,
            getattr(case, "court_case_number", ""),
            getattr(case, "client_name", ""),
        )

    def _quarantine_unmatched_transcript(self, filepath: str, case: CourtCase) -> str:
        quarantine = (
            Path(get_transcript_download_dir()).parent
            / "transcript-quarantine"
            / datetime.now().strftime("%Y%m%d")
        )
        quarantine.mkdir(parents=True, exist_ok=True)
        source = Path(filepath)
        destination = quarantine / source.name
        if destination.exists():
            destination = quarantine / (
                f"{source.stem}_{datetime.now().strftime('%H%M%S_%f')}{source.suffix}"
            )
        shutil.move(str(source), str(destination))
        self.log(
            "  ⛔ PDF 內容與目標案號無法一致，已隔離："
            f"{getattr(case, 'court_case_number', '')} <- {destination.name}"
        )
        return str(destination)

    
    @staticmethod
    def _transcript_sha256(filepath: str) -> str:
        """Hash an archived transcript without loading a large PDF into RAM."""
        import hashlib

        digest = hashlib.sha256()
        with open(filepath, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _transcript_archive_reference(filepath: str, case_root: str = "") -> str:
        """Return an auditable relative reference, never an absolute NAS path."""
        path = Path(filepath)
        try:
            if case_root:
                relative = path.resolve().relative_to(Path(case_root).resolve())
                return relative.as_posix()
        except (OSError, RuntimeError, ValueError):
            # A disconnected share, a symlink loop, or a path outside the case
            # root must never leak an absolute NAS path into a receipt.  Falling
            # back to the basename is an explicit privacy-safe outcome, not a
            # silently swallowed archive error.
            return path.name
        return path.name

    def _repair_placeholder_transcript_filename(self, filepath: str) -> str:
        """Repair a legacy ``00000000`` name only from validated PDF metadata.

        A content-hash duplicate has already passed the case-identity gate, but
        its existing archive may retain a historical placeholder name.  Never
        derive a date from the download, folder, or case; retain that name when
        the PDF parser cannot prove a usable date and record type.
        """

        source = str(filepath or "")
        filename = os.path.basename(source)
        if not filename.startswith("00000000 "):
            return source
        parse_result = self._parse_record_pdf(source)
        if not _record_parse_ready_for_filename(parse_result):
            self.log("  ⚠️ 既有筆錄缺少可信 metadata，保留 00000000 檔名")
            return source
        target_name = self._generate_record_filename(parse_result, filename)
        target = os.path.join(os.path.dirname(source), target_name)
        if target == source:
            return source
        if os.path.exists(target):
            stem, ext = os.path.splitext(target_name)
            counter = 2
            while os.path.exists(target):
                target = os.path.join(os.path.dirname(source), f"{stem}_{counter}{ext}")
                counter += 1
        try:
            os.rename(source, target)
            self.log("  ✏️ 已由 PDF 可信 metadata 修正既有 00000000 筆錄檔名")
            return target
        except OSError:
            logging.getLogger(__name__).warning("failed to repair placeholder transcript filename", exc_info=True)
            return source

    def move_to_case_folder(self, case: CourtCase, file_paths: List[str] = None):
        """Archive transcripts and return a hash-bound receipt for every input.

        Callers must not equate a successful portal download with a successful
        archive.  Each receipt proves the final relative location, SHA-256,
        readability/case match, or the quarantine/not-archived outcome.
        """
        receipts: List[Dict[str, Any]] = []

        if not file_paths:
            self.log("⚠️ 未提供檔案路徑，無法歸檔")
            return receipts

        # 載入 MD5 記錄
        downloaded_md5s = self._load_md5_records()
        
        # 尋找案件資料夾
        transcript_folder = None
        local_folder_path = ""
        if case.folder_path:
            local_folder_path = translate_case_path_to_local(case.folder_path)

            # ★ Debug Log for Packaged App
            self.log(f"  [DEBUG] 原路徑: {case.folder_path}")
            self.log(f"  [DEBUG] 轉換後路徑: {local_folder_path}")
            self.log(f"  [DEBUG] 路徑存在否: {os.path.exists(local_folder_path)}")
            
            if os.path.exists(local_folder_path):
                transcript_folder = self.find_transcript_folder(local_folder_path)
                self.log(f"  [DEBUG] 筆錄資料夾: {transcript_folder}")
        
        if not transcript_folder:
            self.log(f"  ⚠️ 無法找到案件資料夾，將保留檔案在下載區")
            if case.folder_path:
                self.log(f"  (請確認該路徑於此電腦是否可存取)")
            for filepath in file_paths:
                receipts.append(
                    {
                        "source_name": os.path.basename(str(filepath)),
                        "status": "not_archived",
                        "reason": "case_folder_unavailable",
                        "archive_reference": "",
                        "sha256": "",
                        "case_identity_match": False,
                        "readable": False,
                    }
                )
            return receipts

        transcript_folders = self._all_existing_transcript_folders(case, preferred_folder=transcript_folder)
        if not transcript_folders and transcript_folder:
            transcript_folders = [transcript_folder]
        if len(transcript_folders) > 1:
            self.log(f"  🔁 同步檢查 {len(transcript_folders)} 個本機/NAS 映射筆錄資料夾，避免重複下載")

        # ★★★ 核心改進：掃描所有可用映射路徑內現有檔案的 MD5 ★★★
        existing_folder_md5s = {}
        existing_folder_files = {}  # MD5 -> filename mapping
        for scan_folder in transcript_folders:
            if not os.path.exists(scan_folder):
                continue
            for fname in os.listdir(scan_folder):
                if not _is_real_pdf_filename(fname):
                    continue
                fpath = os.path.join(scan_folder, fname)
                try:
                    file_md5 = self._calculate_file_md5(fpath)
                    if file_md5:
                        existing_folder_md5s[file_md5] = fpath
                        existing_folder_files[file_md5] = fpath
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2624, exc_info=True)
        
        if existing_folder_md5s:
            self.log(f"  📂 案件資料夾已有 {len(existing_folder_md5s)} 個筆錄 (已計算 MD5)")

        for filepath in file_paths:
            if not os.path.exists(filepath):
                receipts.append(
                    {
                        "source_name": os.path.basename(str(filepath)),
                        "status": "not_archived",
                        "reason": "download_source_missing",
                        "archive_reference": "",
                        "sha256": "",
                        "case_identity_match": False,
                        "readable": False,
                    }
                )
                continue
                
            try:
                # Defence in depth: even a future portal layout regression may
                # never archive a PDF under the requested case unless the PDF
                # itself proves the docket (or, when no docket text is legible,
                # the party).  Inconclusive files stay isolated for review.
                if not self._transcript_pdf_matches_case(filepath, case):
                    quarantine_path = self._quarantine_unmatched_transcript(filepath, case)
                    receipts.append(
                        {
                            "source_name": os.path.basename(str(filepath)),
                            "status": "quarantined",
                            "reason": "case_identity_mismatch",
                            "archive_reference": "",
                            "quarantine_reference": self._transcript_archive_reference(quarantine_path),
                            "sha256": self._transcript_sha256(quarantine_path),
                            "case_identity_match": False,
                            "readable": True,
                        }
                    )
                    continue

                # ★★★ MD5 檢查：同時比對 JSON 記錄 + 資料夾現有檔案 ★★★
                md5 = self._calculate_file_md5(filepath)
                
                # 1. 強制覆蓋重複檢查：即使 MD5 相同也繼續處理 -> 改為：若內容相同則跳過不存
                if md5 and md5 in existing_folder_md5s:
                    existing_path = existing_folder_files.get(md5, "") or existing_folder_md5s.get(md5, "")
                    existing_path = self._repair_placeholder_transcript_filename(existing_path)
                    existing_file = os.path.basename(existing_path) if existing_path else ""
                    existing_folder_md5s[md5] = existing_path
                    existing_folder_files[md5] = existing_path
                    self.log(f"  ℹ️ 已在案件筆錄資料夾/其他映射路徑找到相同檔案 ({existing_file})，跳過移入")
                    
                    # 刪除暫存下載檔
                    try:
                        if safe_remove:
                            safe_remove(filepath, reason="download_dup_md5", allow_delete=True, log=self.log)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2647, exc_info=True)
                        
                    # 仍需更新 JSON 記錄，確保下次檢查知道我們有這份檔案
                    if md5:
                        downloaded_md5s[md5] = {
                            'filename': existing_file,  # 使用已存在的檔名
                            'case_number': case.case_number,
                            'court_case_number': case.court_case_number,
                            'downloaded_at': datetime.now().isoformat(),
                            'size': self._get_file_size_safe(existing_path)
                        }
                    receipts.append(
                        {
                            "source_name": os.path.basename(str(filepath)),
                            "status": "duplicate_existing",
                            "reason": "content_hash_already_archived",
                            "archive_reference": self._transcript_archive_reference(existing_path, local_folder_path),
                            "sha256": self._transcript_sha256(existing_path),
                            "case_identity_match": True,
                            "readable": True,
                            "size": self._get_file_size_safe(existing_path),
                        }
                    )
                    continue

                # 2. 忽略 JSON 記錄檢查
                if md5 and md5 in downloaded_md5s:
                    self.log(f"  ℹ️ MD5 記錄已存在，將強制更新")
                
                # ★★★ 先移動檔案到案件資料夾 ★★★
                original_filename = os.path.basename(filepath)
                temp_dest = os.path.join(transcript_folder, original_filename)
                
                # 處理暫存檔名衝突
                if os.path.exists(temp_dest):
                    name, ext = os.path.splitext(original_filename)
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    temp_filename = f"{name}_{timestamp}{ext}"
                    temp_dest = os.path.join(transcript_folder, temp_filename)
                
                shutil.move(filepath, temp_dest)
                self.log(f"  📁 已移動到案件資料夾: {original_filename}")
                
                # ★★★ 解析 PDF 並重新命名 ★★★
                parse_result = self._parse_record_pdf(temp_dest)
                if _record_parse_ready_for_filename(parse_result):
                    new_filename = self._generate_record_filename(parse_result, original_filename)
                else:
                    self.log(f"  ⚠️ 筆錄解析結果不足，保留原檔名: {os.path.basename(temp_dest)}")
                    new_filename = os.path.basename(temp_dest)
                final_dest = os.path.join(transcript_folder, new_filename)
                
                # 處理最終檔名衝突
                if os.path.exists(final_dest) and final_dest != temp_dest:
                    name, ext = os.path.splitext(new_filename)
                    counter = 2
                    while os.path.exists(final_dest):
                        new_filename = f"{name}_{counter}{ext}"
                        final_dest = os.path.join(transcript_folder, new_filename)
                        counter += 1
                
                # 重新命名
                if temp_dest != final_dest:
                    os.rename(temp_dest, final_dest)
                    self.log(f"  ✅ 重新命名: {new_filename}")
                else:
                    self.log(f"  ✅ 歸檔完成: {new_filename}")
                
                final_sha256 = self._transcript_sha256(final_dest)
                final_matches = self._transcript_pdf_matches_case(final_dest, case)
                if not final_matches:
                    quarantine_path = self._quarantine_unmatched_transcript(final_dest, case)
                    receipts.append(
                        {
                            "source_name": original_filename,
                            "status": "quarantined",
                            "reason": "post_archive_case_identity_mismatch",
                            "archive_reference": "",
                            "quarantine_reference": self._transcript_archive_reference(quarantine_path),
                            "sha256": final_sha256,
                            "case_identity_match": False,
                            "readable": True,
                        }
                    )
                    continue
                # Only a readable, identity-matched final file may enter the
                # durable dedup index.  A quarantined file must remain eligible
                # for a later corrected download.
                if md5:
                    downloaded_md5s[md5] = {
                        'filename': new_filename,
                        'case_number': case.case_number,
                        'court_case_number': case.court_case_number,
                        'downloaded_at': datetime.now().isoformat(),
                        'size': os.path.getsize(final_dest),
                    }
                receipts.append(
                    {
                        "source_name": original_filename,
                        "status": "archived",
                        "reason": "",
                        "archive_reference": self._transcript_archive_reference(final_dest, local_folder_path),
                        "sha256": final_sha256,
                        "case_identity_match": True,
                        "readable": True,
                        "size": os.path.getsize(final_dest),
                    }
                )
                    
            except Exception as e:
                self.log(f"  ❌ 歸檔失敗 ({os.path.basename(filepath)}): {e}")
                receipts.append(
                    {
                        "source_name": os.path.basename(str(filepath)),
                        "status": "not_archived",
                        "reason": "archive_operation_failed",
                        "archive_reference": "",
                        "sha256": "",
                        "case_identity_match": False,
                        "readable": False,
                    }
                )
        
        # 保存 MD5 記錄
        self._save_md5_records(downloaded_md5s)
        return receipts
    
    def _get_file_size_safe(self, path):
        try:
            return os.path.getsize(path)
        except Exception:
            return 0

    def _calculate_file_md5(self, filepath: str) -> Optional[str]:
        """計算去除 PDF 變動元資料後的內容 MD5。

        ezlawyer 每次下載同一份筆錄都會改變：
        1. CreationDate / ModDate（下載時間戳）
        2. 字型子集前綴（如 VTWNOS+DFKaiShu → UMGLIP+DFKaiShu）
        3. PDF /ID（文件唯一識別碼）

        全部歸零後再算 MD5，確保相同內容得到相同 hash。
        """
        try:
            import hashlib, re as _re
            with open(filepath, "rb") as _fh:
                data = _fh.read()
            # 1. 歸零時間戳
            data = _re.sub(
                rb"/(?:Creation|Mod)Date\s*\(D:\d{14}[^)]*\)",
                b"/CreationDate (D:00000000000000+00'00')",
                data,
            )
            # 2. 歸零字型子集隨機前綴（6個大寫字母+加號）
            data = _re.sub(
                rb"/BaseFont\s*/([A-Z]{6})\+",
                b"/BaseFont /AAAAAA+",
                data,
            )
            # 3. 歸零 PDF /ID
            data = _re.sub(
                rb"/ID\s*\[<[0-9A-Fa-f]+>\s*<[0-9A-Fa-f]+>\]",
                b"/ID [<00> <00>]",
                data,
            )
            return hashlib.md5(data).hexdigest()
        except Exception as e:
            self.log(f"  ⚠️ 計算 MD5 失敗: {e}")
            return None
    
    def _load_md5_records(self) -> Dict:
        records = {}
        if os.path.exists(self.md5_record_file):
            try:
                with open(self.md5_record_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        records.update(loaded)
            except Exception:
                records = {}
        try:
            from skills.ops.dedup_db import list_done as _dd_list
            for row in _dd_list("transcript_download_md5", limit=10000):
                md5_key = str(row.get("item_key") or "").strip()
                if not md5_key or md5_key in records:
                    continue
                meta = row.get("metadata")
                payload = {}
                if isinstance(meta, dict):
                    payload = meta
                elif isinstance(meta, str) and meta.strip():
                    try:
                        parsed = json.loads(meta)
                        if isinstance(parsed, dict):
                            payload = parsed
                    except Exception:
                        payload = {"raw_metadata": meta[:200]}
                payload.setdefault("synced_from", "dedup_db")
                records[md5_key] = payload
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2786, exc_info=True)
        return records
    
    def _save_md5_records(self, records: Dict):

        try:
            with open(self.md5_record_file, 'w', encoding='utf-8') as f:
                json.dump(records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"  ⚠️ 保存 MD5 記錄失敗: {e}")
        try:
            from skills.ops.dedup_db import mark_done as _dd_mark
            for md5_key, payload in (records or {}).items():
                md5_key = str(md5_key or "").strip()
                if not md5_key or md5_key.startswith("__"):
                    continue
                _dd_mark(
                    "transcript_download_md5",
                    md5_key,
                    metadata=payload if isinstance(payload, dict) else {"value": payload},
                )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2802, exc_info=True)

    def _migrate_md5_records_if_needed(self):
        """一次性遷移：標記 normalized_v1，保留既有記錄不清空。

        舊版本會清空記錄等待 scan 重建，但這導致 scan → download_all 之間
        的 cleanup 步驟把 scan 建好的記錄清掉，造成去重完全失效。
        現在改為：只加上 marker，保留既有記錄。scan 會在下次重建正確 MD5。
        """
        records = self._load_md5_records()
        if not records:
            return
        marker = records.get("__md5_version__")
        if marker == "normalized_v1":
            return  # 已遷移
        self.log("  🔄 標記 MD5 記錄為 normalized_v1（保留既有記錄）...")
        records["__md5_version__"] = "normalized_v1"
        self._save_md5_records(records)
        self.log(f"  ✅ MD5 記錄已標記（{len(records) - 1} 筆記錄已保留）")

    def cleanup_download_folder(self) -> dict:
        """清理下載資料夾中的重複檔案（相同內容但不同 CreationDate 的 PDF）"""
        self._migrate_md5_records_if_needed()
        import re as _re
        stats = {"removed": 0, "kept": 0, "crdownload_removed": 0}
        seen_md5 = {}  # content-md5 -> first filepath

        pdfs = sorted(
            (f for f in os.listdir(self.download_folder)
             if _is_real_pdf_filename(f)),
        )

        for fname in pdfs:
            fpath = os.path.join(self.download_folder, fname)
            md5 = self._calculate_file_md5(fpath)
            if not md5:
                continue
            if md5 in seen_md5:
                try:
                    if safe_remove:
                        safe_remove(fpath, reason="cleanup_dup", allow_delete=True, log=self.log)
                    else:
                        os.remove(fpath)
                    stats["removed"] += 1
                    self.log(f"  🗑️ 移除重複: {fname} (與 {os.path.basename(seen_md5[md5])} 內容相同)")
                except Exception as e:
                    self.log(f"  ⚠️ 移除失敗 ({fname}): {e}")
            else:
                seen_md5[md5] = fpath
                stats["kept"] += 1

        # 清理 .crdownload 殘留
        for fname in os.listdir(self.download_folder):
            if fname.endswith(".crdownload"):
                try:
                    os.remove(os.path.join(self.download_folder, fname))
                    stats["crdownload_removed"] += 1
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2830, exc_info=True)

        self.log(f"  🧹 清理完成: 保留 {stats['kept']} 個, 移除 {stats['removed']} 個重複, "
                 f"清除 {stats['crdownload_removed']} 個不完整下載")
        return stats

    def close(self):

        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2842, exc_info=True)
            self.driver = None
            self.logged_in = False
            self.log("  ✓ 瀏覽器已關閉")

    def scan_case_folders_for_md5(self, rename_files: bool = False):
        """
        掃描本機案件資料夾以建立/更新 MD5 記錄 (增量掃描)
        :param rename_files: 是否順便檢查並修正檔名 (Batch Rename)
        """
        global _global_transcript_operation_in_progress
        import json
        
        # ★ 使用全域鎖定防止並行操作
        with _global_transcript_lock:
            if _global_transcript_operation_in_progress:
                self.log("⚠️ [MD5] 另一個筆錄操作正在進行中，跳過此次掃描")
                return
            _global_transcript_operation_in_progress = True
        
        try:
            self.log(f"🔍 [MD5] 開始增量掃描案件資料夾 (Rename={rename_files})...")
            
            # 1. 取得所有案件
            cases = self.get_cases_from_db()
            total_cases = len(cases)
            self.log(f"📊 [MD5] 共有 {total_cases} 個案件需要掃描")
            
            # 2. 載入現有 MD5 記錄
            current_records = self._load_md5_records()
            
            # cache file for scan speedup
            cache_file = os.path.join(self.download_folder, '.md5_scan_cache.json')
            file_cache = {}
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        file_cache = json.load(f)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2880, exc_info=True)
            
            updated_any = False
            new_file_cache = {}
            total_files_scanned = 0
            total_files_renamed = 0

            # 防呆：夜間任務不要因 Synology/大量檔案掃描拖太久
            try:
                max_runtime_sec = int(os.environ.get("MAGI_EZLAWYER_MD5_SCAN_MAX_RUNTIME_SEC", "300") or "300")
            except Exception:
                max_runtime_sec = 300
            started = time.monotonic()
            
            for case_idx, case in enumerate(cases, 1):
                if max_runtime_sec > 0 and (time.monotonic() - started) > max_runtime_sec:
                    self.log(f"⏱️ [MD5] 已超過最大執行時間 {max_runtime_sec}s，停止後續案件（已處理 {case_idx-1}/{total_cases}）。")
                    break
                # ★ 進度顯示（每10個案件或最後一個）
                if case_idx % 10 == 0 or case_idx == total_cases:
                    progress_pct = (case_idx / total_cases) * 100
                    self.log(f"📊 [MD5] 進度: {case_idx}/{total_cases} ({progress_pct:.1f}%)")
                
                if not case.folder_path:
                    continue
                
                primary_path = translate_case_path_to_local(case.folder_path)
                # Inventory must be read-only: never create an empty transcript
                # directory merely because a case is included in the scan.
                preferred_transcript_folder = self._find_existing_transcript_folder(primary_path)
                transcript_folders = self._all_existing_transcript_folders(
                    case,
                    preferred_folder=preferred_transcript_folder or "",
                )

                if not transcript_folders:
                    continue

                if len(transcript_folders) > 1:
                    self.log(f"  🔁 [{case_idx}/{total_cases}] {case.court_case_number} 檢查 {len(transcript_folders)} 個映射筆錄資料夾")

                for transcript_folder in transcript_folders:
                    pdf_files = [f for f in os.listdir(transcript_folder) if _is_real_pdf_filename(f)]
                    if pdf_files:
                        self.log(f"  🔍 [{case_idx}/{total_cases}] {case.court_name} {case.court_case_number} - {len(pdf_files)} 份筆錄")

                    for fname in pdf_files:
                        if max_runtime_sec > 0 and (time.monotonic() - started) > max_runtime_sec:
                            self.log(f"⏱️ [MD5] 已超過最大執行時間 {max_runtime_sec}s，停止處理此案件之後的檔案。")
                            break
                        full_path = os.path.join(transcript_folder, fname)

                        # --- Batch Rename Logic ---
                        if rename_files:
                            try:
                                # 1. Parse content
                                # ★ OPTIMIZATION: 若檔名已符合格式 (YYYYMMDD Type(Period).pdf)，跳過解析
                                # Regex: 8 digits, space, chars, (, chars, ), .pdf
                                if (
                                    re.match(r'^\d{8}\s.+?\(.+\)\.pdf$', fname)
                                    and not fname.startswith("00000000 ")
                                ):
                                    # self.log(f"    ⏭️ 檔名已標準化，略過解析: {fname}")
                                    continue

                                parse_result = self._parse_record_pdf(full_path)
                                if _record_parse_ready_for_filename(parse_result):
                                    # 2. Generate canonical name
                                    new_name = self._generate_record_filename(parse_result, fname)

                                    if new_name != fname:
                                        new_full_path = os.path.join(transcript_folder, new_name)

                                        # Handle collision
                                        if os.path.exists(new_full_path):
                                            name_part, ext_part = os.path.splitext(new_name)
                                            counter = 2
                                            while os.path.exists(new_full_path):
                                                new_name_idx = f"{name_part}_{counter}{ext_part}"
                                                new_full_path = os.path.join(transcript_folder, new_name_idx)
                                                counter += 1

                                        # Rename
                                        os.rename(full_path, new_full_path)
                                        self.log(f"    ✏️ 更名: {fname} -> {os.path.basename(new_full_path)}")

                                        # Update pointers
                                        full_path = new_full_path
                                        fname = os.path.basename(new_full_path)
                            except Exception as e:
                                self.log(f"    ⚠️ 更名失敗 ({fname}): {e}")
                        # --------------------------

                        try:
                            stat = os.stat(full_path)
                            mtime = stat.st_mtime
                            size = stat.st_size

                            # Check cache
                            cached = file_cache.get(full_path)
                            md5 = None

                            if cached and cached.get('mtime') == mtime and cached.get('size') == size:
                                md5 = cached.get('md5')
                            else:
                                md5 = self._calculate_file_md5(full_path)
                                updated_any = True

                            if md5:
                                # 更新 Cache
                                new_file_cache[full_path] = {
                                    'mtime': mtime, 'size': size, 'md5': md5
                                }

                                # 更新主 MD5 記錄 (如果不存在)
                                if md5 not in current_records:
                                    current_records[md5] = {
                                        'filename': fname,
                                        'case_number': case.case_number,
                                        'court_case_number': case.court_case_number,
                                        'downloaded_at': datetime.now().isoformat(),
                                        'size': size,
                                        'source': 'scan'
                                    }
                                    updated_any = True
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2993, exc_info=True)
            
            # Save records — 加上 version marker 避免 migration 清空
            current_records["__md5_version__"] = "normalized_v1"
            if updated_any:
                self._save_md5_records(current_records)
                self.log(f"✅ [MD5] 增量掃描完成！已掃描 {total_cases} 個案件，更新了記錄")
            else:
                self._save_md5_records(current_records)
                self.log(f"✅ [MD5] 掃描完成 ({total_cases} 個案件，已標記 version）")

            # Save Cache
            try:
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(new_file_cache, f, ensure_ascii=False)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3009, exc_info=True)

        except Exception as e:
            self.log(f"❌ [MD5] 掃描失敗: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # ★ 釋放全域鎖定
            with _global_transcript_lock:
                _global_transcript_operation_in_progress = False

    def run_full_sync(self, rename_existing: bool = False):
        """
        執行全同步：
        1. 掃描本機建立 MD5 索引（不更名）
        2. 下載所有新筆錄
        3. 確認下載完成後，統一更名所有筆錄
        
        :param rename_existing: 是否在最後統一更名所有筆錄
        """
        self.log(f"🚀 啟動全系統同步 [統一更名={rename_existing}]...")
        
        # 步驟 1: 掃描 MD5（不更名）
        self.log("📊 [步驟 1/3] 掃描本機筆錄建立 MD5 索引...")
        self.scan_case_folders_for_md5(rename_files=False)  # ★ 永遠不在這裡更名
        
        # 步驟 2: 下載新筆錄
        self.log("📥 [步驟 2/3] 下載新筆錄...")
        self.run()
        
        # 步驟 3: 統一更名（如果需要）
        if rename_existing:
            self.log("✏️ [步驟 3/3] 統一更名所有筆錄...")
            self.rename_all_transcripts()
        else:
            self.log("✅ [步驟 3/3] 跳過更名（未啟用）")
        
        self.log("🎉 全系統同步完成！")
    
    def _is_original_download_filename(self, filename: str) -> bool:
        """
        判斷檔名是否為原始下載格式（尚未被更名）
        原始下載格式通常是純數字（如 123456.pdf）或不符合日期+筆錄類型格式
        
        已更名格式範例：
        - 20251221 審理程序筆錄.pdf
        - 20251221 審理程序筆錄(下午).pdf
        - 20251221 準備程序筆錄(上午0930).pdf
        """
        import re
        name_without_ext = os.path.splitext(filename)[0]
        
        # 標準命名格式: 日期(8位數字) + 空格 + 筆錄類型
        standard_pattern = r'^\d{8}\s+(審理程序筆錄|準備程序筆錄|言詞辯論筆錄|調解程序筆錄|審判筆錄|訊問筆錄|勘驗筆錄|和解程序筆錄|調查程序筆錄|調查筆錄|宣示判決筆錄|協商會議記錄|消債調查筆錄|筆錄)'
        
        if name_without_ext.startswith("00000000 "):
            return True

        if re.match(standard_pattern, name_without_ext):
            # 已符合標準命名格式，表示已經被改名過，不應再更名
            return False
        
        # 其他格式（純數字、原始下載名等）視為原始下載格式，可以更名
        return True
    
    def rename_all_transcripts(self):
        """
        統一更名所有案件資料夾中的筆錄
        在下載完成後執行，確保不會影響下載流程
        ★ 只更名原始下載格式的檔案，已經被手動更名過的檔案會跳過
        """
        global _global_transcript_operation_in_progress

        result = {
            "ok": True,
            "success": True,
            "status": "success",
            "retryable": False,
            "renamed_count": 0,
            "parse_failed_count": 0,
            "metadata_pending_count": 0,
            "file_operation_failed_count": 0,
            "retry_pending_count": 0,
            "timed_out": False,
            "failure_receipts": [],
        }
        
        # 使用全域鎖定
        with _global_transcript_lock:
            if _global_transcript_operation_in_progress:
                self.log("⚠️ [更名] 另一個操作正在進行中，跳過")
                result.update(
                    {
                        "success": False,
                        "status": "deferred",
                        "deferred": True,
                        "retryable": True,
                        "reason": "transcript_operation_busy",
                    }
                )
                return result
            _global_transcript_operation_in_progress = True
        
        try:
            self.log("✏️ [更名] 開始統一更名所有筆錄...")
            
            cases = self.get_cases_from_db()
            total_cases = len(cases)
            total_renamed = 0
            stop_for_timeout = False

            def _failure_receipt(filename: str, category: str) -> dict:
                # Do not persist a client/file name or an absolute NAS path in
                # operational evidence.  The short token is sufficient to
                # correlate repeated attempts without leaking case data.
                token = hashlib.sha256(str(filename or "").encode("utf-8")).hexdigest()[:16]
                return {"file_token": token, "category": str(category or "unknown")}

            # 防呆：避免更名階段因個別 PDF 解析/工具端點異常而拖太久
            try:
                max_runtime_sec = int(os.environ.get("MAGI_EZLAWYER_RENAME_MAX_RUNTIME_SEC", "900") or "900")
            except Exception:
                max_runtime_sec = 900
            started = time.monotonic()
            
            for case_idx, case in enumerate(cases, 1):
                if max_runtime_sec > 0 and (time.monotonic() - started) > max_runtime_sec:
                    self.log(f"⏱️ [更名] 已超過最大執行時間 {max_runtime_sec}s，停止後續案件（已處理 {case_idx-1}/{total_cases}）。")
                    result["timed_out"] = True
                    break
                if not case.folder_path:
                    continue
                
                local_path = translate_case_path_to_local(case.folder_path)
                transcript_folder = self._find_existing_transcript_folder(local_path)
                
                if not transcript_folder or not os.path.exists(transcript_folder):
                    continue
                
                pdf_files = [f for f in os.listdir(transcript_folder) if _is_real_pdf_filename(f)]
                if not pdf_files:
                    continue
                
                self.log(f"  📁 [{case_idx}/{total_cases}] {case.court_name} {case.court_case_number} - {len(pdf_files)} 份筆錄")
                
                for fname in pdf_files:
                    full_path = os.path.join(transcript_folder, fname)
                    try:
                        # ★ 先判斷是否需要更名：已是標準格式的檔案不必解析 PDF（速度差很多）
                        if not self._is_original_download_filename(fname):
                            continue

                        if max_runtime_sec > 0 and (time.monotonic() - started) > max_runtime_sec:
                            self.log(f"⏱️ [更名] 已超過最大執行時間 {max_runtime_sec}s，停止處理此案件之後的檔案。")
                            result["timed_out"] = True
                            stop_for_timeout = True
                            break

                        # 解析 PDF
                        self._last_record_parse_error = ""
                        parse_result = self._parse_record_pdf(full_path)
                        if not _record_parse_ready_for_filename(parse_result):
                            parse_error = str(getattr(self, "_last_record_parse_error", "") or "").strip()
                            category = parse_error or _transcript_filename_metadata_category(parse_result)
                            if parse_error:
                                result["parse_failed_count"] += 1
                            else:
                                result["metadata_pending_count"] += 1
                            result["failure_receipts"].append(_failure_receipt(fname, category))
                            continue
                        
                        # 生成標準檔名
                        new_name = self._generate_record_filename(parse_result, fname)
                        
                        if new_name != fname:
                            new_full_path = os.path.join(transcript_folder, new_name)
                            
                            # 處理衝突
                            if os.path.exists(new_full_path):
                                name_part, ext_part = os.path.splitext(new_name)
                                counter = 2
                                while os.path.exists(new_full_path):
                                    new_name = f"{name_part}_{counter}{ext_part}"
                                    new_full_path = os.path.join(transcript_folder, new_name)
                                    counter += 1
                            
                            # 執行更名
                            os.rename(full_path, new_full_path)
                            self.log(f"    ✏️ {fname} → {new_name}")
                            total_renamed += 1
                            
                    except Exception:
                        self.log("    ⚠️ 更名檔案操作失敗（已保留供重試）")
                        result["file_operation_failed_count"] += 1
                        result["failure_receipts"].append(
                            _failure_receipt(fname, "file_operation_failed")
                        )

                if stop_for_timeout:
                    break
            
            self.log(f"✅ [更名] 完成！共更名 {total_renamed} 個檔案")
            result["renamed_count"] = total_renamed
            result["retry_pending_count"] = (
                int(result["parse_failed_count"])
                + int(result["metadata_pending_count"])
                + int(result["file_operation_failed_count"])
                + (1 if bool(result["timed_out"]) else 0)
            )
            if int(result["retry_pending_count"]) > 0:
                result.update(
                    {
                        "status": "partial_retry_pending",
                        "retryable": True,
                        "partial": True,
                    }
                )
            
        except Exception:
            # Broad inventory failures can carry raw paths/filenames in both
            # their message and traceback.  Emit only the fixed category.
            self.log("❌ [更名] 清單處理失敗（已安全保留並排入重試）")
            result.update(
                {
                    "ok": False,
                    "success": False,
                    "status": "failed",
                    "reason": "rename_inventory_failed",
                    # Do not expose the exception text: it can contain a case
                    # path or filename.  The fixed category lets the caller
                    # retain a bounded, safe retry contract while preserving
                    # the fail-closed workflow status.
                    "exception_category": "rename_inventory_exception",
                    "retryable": True,
                    "retry_pending_count": 1,
                }
            )
        finally:
            with _global_transcript_lock:
                _global_transcript_operation_in_progress = False

        return result
    
    def run(self):
        """執行自動下載流程"""
        try:
            # login() 會在 download_all() 內部被呼叫，不需要在此重複呼叫
            self.download_all()
        finally:
            self.close()


# ==============================================================================
# 閱卷管理
# ==============================================================================

class FileReviewManager:

    def __new__(cls, *args, **kwargs):
        try:
            from file_review_automation import FileReviewManager as RealManager
            return RealManager(*args, **kwargs)
        except ImportError:
            _safe_print("⚠️ 無法載入 file_review_automation 模組，請確保該檔案存在。")
            return super(FileReviewManager, cls).__new__(cls)
            
    def __init__(self, *args, **kwargs):
        # 如果無法載入新模組，會執行這裡
        raise ImportError("FileReviewManager 已移至 file_review_automation.py，但無法載入該模組。")


# ==============================================================================
# 電子筆錄自動下載管理器
# ==============================================================================

class TranscriptAutoDownloader:

    
    CHECK_INTERVAL = 21600  # 6 小時 = 21600 秒
    
    def __init__(self, config: Dict, db_manager=None, log_callback=None, laf_manager=None):

        self.config = config
        self.db_manager = db_manager
        self.log_callback = log_callback
        self.laf_manager = laf_manager  # 用於等待 LAF 完成
        
        # 從 config 取得設定
        judicial_config = config.get('judicial', {})
        self.username = os.environ.get('MAGI_JUDICIAL_RECORD_USERNAME') or judicial_config.get('record_username', '')
        self.password = os.environ.get('MAGI_JUDICIAL_RECORD_PASSWORD') or judicial_config.get('record_password', '')
        
        raw_download_folder = (
            judicial_config.get('record_download_folder')
            or str(get_transcript_download_dir())
        )
        
        # (MacFix) 強制修正 Windows 路徑
        if sys.platform == 'darwin' and (raw_download_folder.lower().startswith('k:') or '\\' in raw_download_folder):
             raw_download_folder = str(get_transcript_download_dir())
             _safe_print(f"⚠️ [Mac修正] 偵測到 Windows 路徑，已強制重置為: {raw_download_folder}")
             
        self.download_folder = os.path.abspath(raw_download_folder)
        
        self.headless = judicial_config.get('headless', True)
        self.enabled = judicial_config.get('record_enabled', False)
        
        # MD5 記錄檔
        self.md5_record_file = os.path.join(self.download_folder, '.downloaded_files.json')
        
        # 排程控制
        self._running = False
        self._scheduler_thread = None
        
        # ★ 掃描協調機制 - 避免並行掃描
        self._scan_in_progress = False
        self._scan_lock = threading.Lock()
        
        # 下載器實例
        self.downloader = None
        
        # ★ Gemini 解析快取（避免重複調用 API）
        self.gemini_cache_file = os.path.join(self.download_folder, '.gemini_parse_cache.json')
        self.gemini_cache = self._load_gemini_cache()
        
        # 確保下載資料夾存在
        os.makedirs(self.download_folder, exist_ok=True)
        
        # 路徑設定(用於轉換 DB 路徑到本機路徑)
        paths_config = config.get('paths', {})
        self.canonical_windows_base_path = paths_config.get('canonical_windows_base_path', '')
        self.mac_base_path = paths_config.get('mac_base_path', '')
        self.court_docs_folder = paths_config.get('court_docs_folder', '')
    
    def get_path_mappings(self) -> Tuple[Optional[str], Optional[str]]:

        canonical_path = self.canonical_windows_base_path
        
        # 根據作業系統選擇本機路徑
        if sys.platform == 'darwin':  # macOS
            local_path = self.mac_base_path or self.court_docs_folder
        else:  # Windows / Linux
            local_path = self.court_docs_folder
        
        if not canonical_path or not local_path:
            return None, None
            
        return (
            canonical_path.replace("\\", "/"),
            local_path.replace("\\", "/")
        )
    
    def translate_path_to_local(self, db_path_str: str) -> str:

        if not db_path_str:
            return db_path_str

        translated = translate_case_path_to_local(db_path_str)
        if translated and (
            translated.startswith("/Users/")
            or translated.startswith("/Volumes/")
            or translated.replace("\\", "/") != db_path_str.replace("\\", "/")
        ):
            return translated

        return db_path_str
    
    def find_transcript_folder(self, case_folder_path: str) -> Optional[str]:

        if not case_folder_path or not os.path.exists(case_folder_path):
            return None
        
        try:
            # 列出案件資料夾中的所有子資料夾
            for item in os.listdir(case_folder_path):
                item_path = os.path.join(case_folder_path, item)
                
                # 只檢查資料夾
                if not os.path.isdir(item_path):
                    continue
                
                # 檢查資料夾名稱是否包含「筆錄」
                if '筆錄' in item:
                    return item_path
            
            # 找不到，創建「筆錄」資料夾（不硬編碼編號，因為各案件編號不同）
            default_folder = os.path.join(case_folder_path, "筆錄")
            os.makedirs(default_folder, exist_ok=True)
            self.log(f"  📁 建立筆錄資料夾: {default_folder}")
            return default_folder
            
        except Exception as e:
            self.log(f"  ⚠️ 尋找筆錄資料夾失敗: {e}")
            return None

    def _find_existing_transcript_folder(self, case_folder_path: str) -> Optional[str]:
        return _find_existing_transcript_folder_path(case_folder_path)
    
    def _get_processed_log_path(self):
        return os.path.join(os.path.dirname(self.md5_record_file), '.processed_original_files.json')

    def _load_processed_log(self):
        records = {}
        log_path = self._get_processed_log_path()
        if os.path.exists(log_path):
            try:
                with open(log_path, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        records.update(loaded)
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                _safe_print(f"⚠️ Processed log corrupted ({log_path}): {e}", file=sys.stderr)
            except Exception as e:
                _safe_print(f"⚠️ Failed to load processed log ({log_path}): {e}", file=sys.stderr)
        try:
            from skills.ops.dedup_db import list_done as _dd_list
            for row in _dd_list("transcript_original_processed", limit=10000):
                item_key = str(row.get("item_key") or "").strip()
                if "::" not in item_key:
                    continue
                case_number, filename = item_key.split("::", 1)
                if not case_number or not filename:
                    continue
                bucket = records.setdefault(case_number, [])
                if filename not in bucket:
                    bucket.append(filename)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3338, exc_info=True)
        return records

    def _save_processed_log(self, data):
        log_path = self._get_processed_log_path()
        try:
            with open(log_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _safe_print(f"⚠️ Failed to save processed log ({log_path}): {e}", file=sys.stderr)
        try:
            from skills.ops.dedup_db import mark_done as _dd_mark
            for case_number, filenames in (data or {}).items():
                if not isinstance(filenames, list):
                    continue
                for filename in filenames:
                    case_number = str(case_number or "").strip()
                    filename = str(filename or "").strip()
                    if not case_number or not filename:
                        continue
                    _dd_mark(
                        "transcript_original_processed",
                        f"{case_number}::{filename}",
                        metadata={"case_number": case_number, "filename": filename, "source": "processed_log"},
                    )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3358, exc_info=True)

    def _is_original_file_processed(self, case_number, filename):
        log = self._load_processed_log()
        if filename in log.get(case_number, []):
            return True
        try:
            from skills.ops.dedup_db import is_done as _dd_is_done
            return bool(_dd_is_done("transcript_original_processed", f"{case_number}::{filename}"))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3366, exc_info=True)
            return False

    def _mark_original_file_processed(self, case_number, filename):
        log = self._load_processed_log()
        if case_number not in log:
            log[case_number] = []
        if filename not in log[case_number]:
            log[case_number].append(filename)
            self._save_processed_log(log)
        else:
            try:
                from skills.ops.dedup_db import mark_done as _dd_mark
                _dd_mark(
                    "transcript_original_processed",
                    f"{case_number}::{filename}",
                    metadata={"case_number": case_number, "filename": filename, "source": "processed_log"},
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3382, exc_info=True)

    def archive_to_case_folder(self, filepath: str, case: 'CourtCase') -> bool:
        """歸檔到案件資料夾並重命名"""
        # ★ 原始檔名重複檢查 (若已處理過則直接刪除)
        original_filename_check = os.path.basename(filepath)
        if self._is_original_file_processed(case.case_number, original_filename_check):
            self.log(f"    ⏭️ 原始檔名已存在紀錄，視為重複檔案，直接刪除: {original_filename_check}")
            try:
                if safe_remove:
                    safe_remove(filepath, reason="original_name_processed", allow_delete=True, log=self.log)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3362, exc_info=True)
            return True

        if not case.folder_path:
            self.log(f"  ⚠️ 案件 {case.case_number} 沒有設定資料夾路徑")
            return False
        
        try:
            # 1. 轉換路徑到本機路徑
            local_folder_path = self.translate_path_to_local(case.folder_path)
            
            # 2. 檢查資料夾是否存在
            if not os.path.exists(local_folder_path):
                self.log(f"  ⚠️ 案件資料夾不存在: {local_folder_path}")
                return False
            
            # 3. 尋找筆錄資料夾
            transcript_folder = self.find_transcript_folder(local_folder_path)
            if not transcript_folder:
                self.log(f"  ⚠️ 無法找到或建立筆錄資料夾")
                return False
            
            # ★ 新增：檢查移入前是否已存在相同內容的檔案
            # 避免因為檔名衝突產生 _1 副本
            try:
                source_hash = self._calculate_pdf_content_hash(filepath)
                if source_hash:
                    # 掃描目標資料夾
                    for fname in os.listdir(transcript_folder):
                        if not _is_real_pdf_filename(fname):
                            continue
                            
                        target_path = os.path.join(transcript_folder, fname)
                        if self._calculate_pdf_content_hash(target_path) == source_hash:
                            self.log(f"  ⏭️ [重複] 目標資料夾已有相同內容: {fname}，略過移入")
                            try:
                                if safe_remove:
                                    safe_remove(filepath, reason="content_hash_dup", allow_delete=True, log=self.log)
                            except:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3401, exc_info=True)
                            return True
            except Exception as e:
                # 這裡若失敗則繼續執行移入，不阻擋流程
                # self.log(f"  ⚠️ 檢查重複失敗 (跳過): {e}")
                logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 4369, exc_info=True)

            # 4. 先移動檔案到筆錄資料夾 (保持原始檔名)
            import shutil
            original_filename = os.path.basename(filepath)
            temp_dest = os.path.join(transcript_folder, original_filename)
            
            # 處理暫存檔名衝突
            if os.path.exists(temp_dest):
                name, ext = os.path.splitext(original_filename)
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                temp_filename = f"{name}_{timestamp}{ext}"
                temp_dest = os.path.join(transcript_folder, temp_filename)
            
            shutil.move(filepath, temp_dest)
            self.log(f"  📁 已移動到筆錄資料夾: {original_filename}")
            
            # 5. 解析 PDF 並重新命名
            parse_result = self._parse_record_pdf(temp_dest)
            if _record_parse_ready_for_filename(parse_result):
                new_filename = self._generate_record_filename(parse_result, original_filename)
            else:
                self.log(f"  ⚠️ 筆錄解析結果不足，保留原檔名: {os.path.basename(temp_dest)}")
                new_filename = os.path.basename(temp_dest)
            final_dest = os.path.join(transcript_folder, new_filename)
            
            # 處理最終檔名衝突
            if os.path.exists(final_dest) and final_dest != temp_dest:
                name, ext = os.path.splitext(new_filename)
                counter = 2
                while os.path.exists(final_dest):
                    new_filename = f"{name}_{counter}{ext}"
                    final_dest = os.path.join(transcript_folder, new_filename)
                    counter += 1
            
            # 重命名
            if temp_dest != final_dest:
                os.rename(temp_dest, final_dest)
                self.log(f"  ✅ 重新命名: {new_filename}")
            else:
                self.log(f"  ✅ 歸檔完成: {new_filename}")
            
            # ★ 記錄原始檔名
            self._mark_original_file_processed(case.case_number, original_filename)
            
            return True
            
        except Exception as e:
            self.log(f"  ❌ 歸檔失敗: {e}")
            traceback.print_exc()
            return False
    
    def _parse_record_pdf(self, filepath: str) -> Dict[str, Optional[str]]:
        """
        解析 PDF 取得日期、類型、時段
        ★ 增強版：只讀取上方 30% + 裁切左側行號 + Gemini Fallback
        """
        result = {'date': None, 'type': None, 'period': None, 'time': None}
        
        try:
            import fitz  # PyMuPDF
            
            doc = fitz.open(filepath)
            if len(doc) == 0:
                return result
            
            page = doc[0]
            page_rect = page.rect
            page_width = page_rect.width
            page_height = page_rect.height
            
            # ★ 裁切區域：左側 8% + 只取上方 30%
            LEFT_MARGIN_RATIO = 0.08
            TOP_RATIO = 0.30
            left_crop = page_width * LEFT_MARGIN_RATIO
            
            clip_rect = fitz.Rect(
                page_rect.x0 + left_crop,
                page_rect.y0,
                page_rect.x1,
                page_rect.y0 + (page_height * TOP_RATIO)
            )
            
            text = page.get_text(clip=clip_rect)
            raw_text = page.get_text()
            doc.close()
            
            # 1. 提取開庭日期
            date_found = False
            date_patterns = [
                r'中華民國(\d{3})年(\d{1,2})月(\d{1,2})日',
                r'民國(\d{3})年(\d{1,2})月(\d{1,2})日',
            ]
            
            for pattern in date_patterns:
                match = re.search(pattern, text)
                if match:
                    roc_year = int(match.group(1))
                    month = int(match.group(2))
                    day = int(match.group(3))
                    year = roc_year + 1911
                    if _TRANSCRIPT_MIN_YEAR <= year <= _TRANSCRIPT_MAX_YEAR and 1 <= month <= 12 and 1 <= day <= 31:
                        result['date'] = f"{year:04d}{month:02d}{day:02d}"
                        date_found = True
                        break
            
            # 如果上面失敗，嘗試緊湊版
            if not date_found:
                compact_text = re.sub(r'\s+', '', raw_text)
                for pattern in date_patterns:
                    match = re.search(pattern, compact_text)
                    if match:
                        roc_year = int(match.group(1))
                        month = int(match.group(2))
                        day = int(match.group(3))
                        year = roc_year + 1911
                        if _TRANSCRIPT_MIN_YEAR <= year <= _TRANSCRIPT_MAX_YEAR:
                            result['date'] = f"{year:04d}{month:02d}{day:02d}"
                            date_found = True
                            break
            
            # 2. 提取筆錄類型
            normalized_text = re.sub(r'\s+', '', raw_text)
            type_keywords = [
                '準備程序筆錄', '言詞辯論筆錄', '審理程序筆錄',
                '審判筆錄', '訊問筆錄', '調解程序筆錄', '勘驗筆錄',
                '和解程序筆錄', '調查程序筆錄', '宣示判決筆錄',
            ]
            
            for keyword in type_keywords:
                if keyword in normalized_text:
                    result['type'] = keyword
                    break
            
            if not result['type'] and '筆錄' in normalized_text:
                result['type'] = '筆錄'
            
            # 3. 提取時段（包含完整時間）
            # 先嘗試提取完整時間：上午9時30分 或 下午2時45分
            time_match = re.search(r'(上午|下午)\s*(\d{1,2})\s*時\s*(\d{1,2})\s*分', text)
            if time_match:
                period = time_match.group(1)
                hour = time_match.group(2).zfill(2)
                minute = time_match.group(3).zfill(2)
                result['period'] = f"{period}{hour}{minute}"  # 例如：上午0930
                self.log(f"  🕐 解析時間: {period}{hour}時{minute}分")
            elif '上午' in text:
                result['period'] = '上午'
            elif '下午' in text:
                result['period'] = '下午'
            
            # ★ 4. Gemini Fallback
            # 判斷是否需要 Gemini 輔助：
            # 1. 日期或類型缺失
            # 2. period 只有「上午/下午」沒有完整時間（如「上午0930」）
            period_needs_time = result['period'] in ['上午', '下午', None, '']
            needs_gemini = not result['date'] or not result['type'] or period_needs_time
            
            use_casper_assist = os.environ.get("MAGI_RECORD_PARSE_CASPER_ASSIST", "1").strip().lower() in {"1", "true", "yes", "on"}
            if needs_gemini and use_casper_assist:
                self.log("  🤖 正則解析不完整，嘗試 CASPER 輔助...")
                gemini_result = self._parse_with_gemini_cached(text)
                gemini_result = _sanitize_transcript_parse_result(gemini_result)
                if any(gemini_result.values()):
                    if not result['date'] and gemini_result.get('date'):
                        result['date'] = gemini_result['date']
                        self.log(f"  📅 [CASPER] 解析日期: {result['date']}")
                    if not result['type'] and gemini_result.get('type'):
                        result['type'] = gemini_result['type']
                        self.log(f"  📝 [CASPER] 解析類型: {result['type']}")
                    gemini_period = gemini_result.get('period', '')
                    gemini_time = gemini_result.get('time', '')
                    if not result['period'] and gemini_period:
                        result['period'] = gemini_period
                    if (
                        gemini_time
                        and result.get('period') == gemini_period
                        and result.get('period') in {'上午', '下午'}
                    ):
                        result['period'] = f"{gemini_period}{gemini_time}"
                        result['time'] = gemini_time
                        self.log(f"  🕐 [CASPER] 解析時間: {result['period']}")
            
        except ImportError:
            self.log("  ⚠️ 需要安裝 PyMuPDF (fitz) 才能解析 PDF")
        except Exception as e:
            self.log(f"  ⚠️ 解析 PDF 失敗: {e}")
        
        return result
    
    def _parse_with_gemini(self, text: str) -> Dict[str, Optional[str]]:
        """使用 CASPER 解析筆錄日期與類型（保留函式名以相容舊流程）"""
        import json as json_lib

        prompt = (
            "分析法院筆錄文字，提取以下資訊並回覆 JSON：\n"
            "1. 開庭日期：民國年轉西元年（如113年→2024年），格式 YYYYMMDD\n"
            "2. 筆錄類型：如「準備程序筆錄」「言詞辯論筆錄」「審判筆錄」等\n"
            "3. 開庭時段：上午或下午\n"
            "4. 開庭時間：完整時間格式，如「上午0930」「下午0230」（4位數時分）\n\n"
            "回覆格式（只回 JSON，不要其他文字）：\n"
            "{\"date\":\"YYYYMMDD\",\"type\":\"筆錄類型\",\"period\":\"上午0930\",\"time\":\"HHMM\"}\n\n"
            "注意：period 欄位請包含完整時間（如「上午0930」而非只有「上午」）。\n\n"
            "文字：\n"
            + (text or "")[:1500]
        )

        try:
            from skills.bridge.inference_gateway import InferenceGateway

            r = InferenceGateway().chat(
                prompt,
                task_type="transcribe",
                timeout=90,
                allow_synthetic_fallback=False,
            )
        except Exception as e:
            self.log(f"  ⚠️ [CASPER Gateway] 呼叫異常(略過): {str(e)[:120]}")
            return None
        if not isinstance(r, dict) or not r.get("success"):
            self.log(f"  ⚠️ [CASPER Gateway] 呼叫失敗: {(r.get('error') if isinstance(r, dict) else '')}")
            return None

        response_text = (r.get("response") or "").strip()
        if not response_text:
            return None

        if '```json' in response_text:
            response_text = response_text.split('```json')[1].split('```')[0].strip()
        elif '```' in response_text:
            response_text = response_text.split('```')[1].split('```')[0].strip()

        try:
            parsed_result = json_lib.loads(response_text)
        except Exception as e:
            self.log(f"  ⚠️ [CASPER] JSON 解析失敗: {e}")
            return None

        if isinstance(parsed_result, list):
            if len(parsed_result) > 0 and isinstance(parsed_result[0], dict):
                parsed_result = parsed_result[0]
            else:
                return None
        if not isinstance(parsed_result, dict):
            return None
        return parsed_result
    
    def _load_gemini_cache(self) -> Dict:
        """載入 Gemini 解析快取"""
        if hasattr(self, 'gemini_cache_file') and os.path.exists(self.gemini_cache_file):
            try:
                with open(self.gemini_cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3641, exc_info=True)
        return {}
    
    def _save_gemini_cache(self):
        """儲存 Gemini 解析快取"""
        if not hasattr(self, 'gemini_cache_file'):
            return
        try:
            with open(self.gemini_cache_file, 'w', encoding='utf-8') as f:
                json.dump(self.gemini_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"  ⚠️ 儲存 Gemini 快取失敗: {e}")
    
    def _get_text_hash(self, text: str) -> str:
        """計算文字的 MD5 雜湊"""
        import hashlib
        return hashlib.md5(text.encode('utf-8')).hexdigest()
    
    def _parse_with_gemini_cached(self, text: str) -> Dict[str, Optional[str]]:
        """使用 Gemini AI 解析筆錄（含 MD5 快取）"""
        cache_key = self._get_text_hash(text)
        
        # ★ 檢查快取
        if hasattr(self, 'gemini_cache') and cache_key in self.gemini_cache:
            cached = self.gemini_cache[cache_key]
            sanitized = _sanitize_transcript_parse_result(cached)
            if sanitized != cached:
                self.gemini_cache[cache_key] = sanitized
                self._save_gemini_cache()
                self.log("  🧹 [CASPER] 已隔離快取中的無效日期／時間")
            self.log(f"  💾 [CASPER] 使用快取結果 (命中)")
            return sanitized
        
        # 調用 CASPER (保留函式名以相容舊流程)
        result = _sanitize_transcript_parse_result(self._parse_with_gemini(text))
        
        # ★ 儲存到快取
        if any(result.values()) and hasattr(self, 'gemini_cache'):
            self.gemini_cache[cache_key] = result
            self._save_gemini_cache()
            self.log(f"  💾 [CASPER] 已快取解析結果")
        
        return result
    
    def _generate_record_filename(self, parse_result: Dict[str, Optional[str]], original_filename: str) -> str:
        """根據解析結果生成檔名"""
        date_str = parse_result.get('date')
        record_type = parse_result.get('type', '筆錄')
        period = parse_result.get('period', '')
        time_str = parse_result.get('time', '')
        if period in {'上午', '下午'} and re.fullmatch(r'\d{4}', str(time_str or '')):
            period = f"{period}{time_str}"
        
        # DEBUG: Check for double time bug
        self.log(f"    [FilenameGen] Date={date_str}, Type={record_type}, Period={period}")

        if not _record_parse_ready_for_filename(parse_result):
            self.log(f"    ⚠️ 筆錄解析結果不足，保留原檔名: {original_filename}")
            return original_filename
        
        if not date_str:
            # ★★★ BUG FIX: 不再使用下載日期！改用辨識標記 ★★★
            date_str = '00000000'
            self.log(f"    ⚠️ 【日期解析失敗】無法從 PDF 提取作成日，標記 00000000")
        
        # ★ 防呆修正：處理重複的時間字串 (如 上午09300930)
        if period and len(period) >= 10:
            # 檢查是否有重複的 4 位數時間 (例如 09300930)
            dup_match = re.search(r'(上午|下午)(\d{4})\2', period)
            if dup_match:
                period = f"{dup_match.group(1)}{dup_match.group(2)}"
                self.log(f"    🔧 [AutoFix] 修正重複時間: {dup_match.group(0)} -> {period}")

        if period:
            filename = f"{date_str} {record_type}({period}).pdf"
        else:
            filename = f"{date_str} {record_type}.pdf"
        
        return filename

    def _calculate_file_md5(self, filepath: str) -> Optional[str]:
        """計算檔案的完整 MD5 雜湊值（用於重複檔案偵測）"""
        import hashlib
        try:
            hash_md5 = hashlib.md5()
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        except Exception as e:
            self.log(f"  ⚠️ 計算 MD5 失敗: {filepath} - {e}")
            return None

    def log(self, message: str):

        timestamp = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{timestamp}] [筆錄自動] {message}"
        _safe_print(full_msg)
        _safe_log_callback(self.log_callback, full_msg)
    
    def _check_and_run_first_time_setup(self):
        """檢查是否需要執行首次初始化（只執行一次）"""
        first_run_marker = os.path.join(self.download_folder, '.first_run_completed.json')
        
        if os.path.exists(first_run_marker):
            # 已執行過，跳過
            return
        
        self.log("🚀 [首次執行] 偵測到首次啟動，開始清理與改名作業...")
        
        try:
            self.first_run_cleanup_and_rename()
            
            # 標記首次執行已完成
            with open(first_run_marker, 'w', encoding='utf-8') as f:
                json.dump({
                    'completed_at': datetime.now().isoformat(),
                    'version': '1.0.0'
                }, f, ensure_ascii=False, indent=2)
            
            self.log("✅ [首次執行] 初始化作業完成！")
            
        except Exception as e:
            self.log(f"❌ [首次執行] 初始化失敗: {e}")
            traceback.print_exc()
    
    def first_run_cleanup_and_rename(self):
        """
        首次執行清理與改名作業：
        1. 掃描所有筆錄資料夾
        2. 比對 MD5 刪除重複檔案
        3. 統一改名所有筆錄
        """
        self.log("📊 [首次執行] 開始掃描所有案件資料夾...")
        
        # 創建臨時下載器來使用其方法
        temp_downloader = CourtRecordDownloader(
            username=self.username, password=self.password,
            db_manager=self.db_manager, headless=self.headless,
            log_callback=self.log_callback
        )
        
        cases = temp_downloader.get_cases_from_db()
        total_cases = len(cases)
        total_duplicates_removed = 0
        total_renamed = 0
        
        self.log(f"📁 [首次執行] 共有 {total_cases} 個案件需要處理")
        
        folders_found = 0
        folders_with_pdfs = 0
        
        for case_idx, case in enumerate(cases, 1):
            if not case.folder_path:
                continue
            
            local_path = self.translate_path_to_local(case.folder_path)
            
            # ★ DEBUG: 顯示前 3 個案件的路徑轉換結果
            if case_idx <= 3:
                self.log(f"  [DEBUG] 案件 {case_idx}: {case.case_number}")
                self.log(f"    原始路徑: {case.folder_path}")
                self.log(f"    轉換路徑: {local_path}")
                self.log(f"    路徑存在: {os.path.exists(local_path) if local_path else 'N/A'}")
            
            transcript_folder = self._find_existing_transcript_folder(local_path) if local_path else None
            
            if not transcript_folder or not os.path.exists(transcript_folder):
                if case_idx <= 3:
                    self.log(f"    筆錄資料夾: 未找到")
                continue
            
            folders_found += 1
            pdf_files = [f for f in os.listdir(transcript_folder) if _is_real_pdf_filename(f)]
            if not pdf_files:
                continue
            
            folders_with_pdfs += 1
            
            # 每處理 10 個有 PDF 的資料夾顯示一次進度
            if folders_with_pdfs <= 3:
                self.log(f"  📂 [DEBUG] {case.case_number}: 發現 {len(pdf_files)} 個 PDF  ({transcript_folder})")
            
            # ★ 步驟 1：刪除重複檔案（MD5 比對）
            md5_map = {}  # md5 -> (filepath, filename)
            duplicates = []
            
            for fname in pdf_files:
                full_path = os.path.join(transcript_folder, fname)
                try:
                    # 改用內容雜湊 (Content Hash)
                    content_hash = self._calculate_pdf_content_hash(full_path)
                    
                    if content_hash:
                        # DEBUG: Log Hash for first few folders
                        if folders_with_pdfs <= 3:
                            self.log(f"    [Hash] {fname} -> {content_hash[:8]}...")

                        if content_hash in md5_map:
                            # 發現重複！比較檔名，保留資訊較完整的那個
                            existing_path, existing_name = md5_map[content_hash]
                            
                            # 判斷保留誰：
                            # 1. 優先保留有完整時間的 (e.g. "上午0930" > "上午")
                            # 2. 優先保留沒有 "_1" 後綴的
                            # 3. 優先保留檔名較長的 (通常資訊較多)
                            
                            keep_existing = True
                            
                            # 檢查時間資訊完整性 (包含 4 位數時間)
                            current_has_time = bool(re.search(r'\d{4}\)', fname)) or bool(re.search(r'上午\d+', fname)) or bool(re.search(r'下午\d+', fname))
                            existing_has_time = bool(re.search(r'\d{4}\)', existing_name)) or bool(re.search(r'上午\d+', existing_name)) or bool(re.search(r'下午\d+', existing_name))
                            
                            if current_has_time and not existing_has_time:
                                keep_existing = False
                            elif existing_has_time and not current_has_time:
                                keep_existing = True
                            else:
                                # 時間資訊程度相同，比較是否為副本命名 (e.g. _1)
                                is_current_copy = bool(re.search(r'_\d+\.pdf$', fname))
                                is_existing_copy = bool(re.search(r'_\d+\.pdf$', existing_name))
                                
                                if is_current_copy and not is_existing_copy:
                                    keep_existing = True
                                elif is_existing_copy and not is_current_copy:
                                    keep_existing = False
                                else:
                                    # 都不是副本或都是副本，保留檔名較長的
                                    if len(fname) > len(existing_name):
                                        keep_existing = False
                            
                            if keep_existing:
                                # 標記 current (fname) 為要刪除
                                duplicates.append((full_path, fname, existing_name))
                            else:
                                # 標記 existing 為要刪除，並更新 map 指向 current
                                duplicates.append((existing_path, existing_name, fname))
                                md5_map[content_hash] = (full_path, fname)
                                
                        else:
                            md5_map[content_hash] = (full_path, fname)
                except Exception as e:
                    if folders_with_pdfs <= 3:
                        self.log(f"    [MD5] ⚠️ 計算失敗 {fname}: {e}")
                    pass
            
            # 刪除重複檔案
            try:
                for dup_path, dup_name, original_name in duplicates:
                     # 再次確認檔案存在（避免已刪除）
                    if os.path.exists(dup_path):
                        try:
                            # dup_path 常在案件資料夾（Synology Drive）內，禁止刪除；改隔離保留。
                            if safe_remove:
                                safe_remove(dup_path, reason="transcript_dup_hash", allow_delete=False, log=self.log)
                                self.log(f"  📦 已隔離重複 (內容雜湊比對): {dup_name} (與 {original_name} 相同)")
                                total_duplicates_removed += 1
                        except Exception as e:
                            self.log(f"  ⚠️ 隔離失敗: {dup_name} - {e}")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3890, exc_info=True)

            
            # ★ 步驟 2：強制重新判斷所有筆錄（不論目前檔名如何）
            # 首次執行時，所有筆錄都使用 Gemini 重新解析並改名
            # 重新讀取（因為可能刪除了一些）
            pdf_files = [f for f in os.listdir(transcript_folder) if _is_real_pdf_filename(f)]
            
            for fname in pdf_files:
                full_path = os.path.join(transcript_folder, fname)
                try:
                    # ★ 首次執行：不檢查檔名格式，全部強制使用 Gemini 重新判斷
                    # 但若檔名已是標準格式 (YYYYMMDD Type(Period).pdf)，則跳過以節省資源
                    if re.match(r'^\d{8}\s.+?\(.+\)\.pdf$', fname):
                        # self.log(f"    ⏭️ 檔名已標準化，略過解析: {fname}")
                        continue

                    # 直接解析 PDF（會自動使用 Gemini fallback）
                    parse_result = self._parse_record_pdf(full_path)
                    if not _record_parse_ready_for_filename(parse_result):
                        # 正則和 Gemini 都失敗，跳過
                        continue
                    
                    # 生成新檔名
                    new_name = self._generate_record_filename(parse_result, fname)
                    
                    if new_name != fname:
                        new_full_path = os.path.join(transcript_folder, new_name)
                        
                        # 處理衝突
                        if os.path.exists(new_full_path):
                            name_part, ext_part = os.path.splitext(new_name)
                            counter = 2
                            while os.path.exists(new_full_path):
                                new_name = f"{name_part}_{counter}{ext_part}"
                                new_full_path = os.path.join(transcript_folder, new_name)
                                counter += 1
                        
                        os.rename(full_path, new_full_path)
                        self.log(f"  ✏️ 改名: {fname} → {new_name}")
                        total_renamed += 1
                        
                except Exception as e:
                    self.log(f"  ⚠️ 處理失敗 ({fname}): {e}")
            
            # 進度顯示
            if case_idx % 10 == 0 or case_idx == total_cases:
                progress_pct = (case_idx / total_cases) * 100
                self.log(f"📊 [首次執行] 進度: {case_idx}/{total_cases} ({progress_pct:.1f}%)")
        
        self.log(f"✅ [首次執行] 完成！刪除 {total_duplicates_removed} 個重複檔案，改名 {total_renamed} 個筆錄")
    
    def _calculate_file_md5(self, filepath: str) -> Optional[str]:
        """計算檔案 MD5"""
        try:
            import hashlib
            hash_md5 = hashlib.md5()
            with open(filepath, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    hash_md5.update(chunk)
            return hash_md5.hexdigest()
        except Exception:
            return None
    
    def start(self):

        if not self.enabled:
            self.log("電子筆錄自動下載已停用（record_enabled=false）")
            return
        
        if not self.username or not self.password:
            self.log("⚠️ 未設定帳號密碼，無法啟動自動下載")
            return
            
        # ★ 防止重複啟動
        if self._running and self._scheduler_thread and self._scheduler_thread.is_alive():
            self.log("⚠️ 電子筆錄自動下載排程已在執行中，忽略重複啟動請求")
            return
        
        # ★★★ 首次執行檢查：只執行一次的清理與改名 ★★★
        self._check_and_run_first_time_setup()
        
        self._running = True
        
        def periodic_check():
            # 等待 5 分鐘再啟動，讓 LAF 檔案完整性檢查先執行
            # LAF 一般在啟動後 30 秒開始，所以等 5 分鐘確保它有足夠時間完成
            self.log("⏳ 等待 1 分鐘後啟動筆錄檢查（讓 LAF 檔案完整性檢查先完成）...")
            
            wait_time = 60  # 5 分鐘
            elapsed = 0
            while self._running and elapsed < wait_time:
                time.sleep(10)
                elapsed += 10
            
            if not self._running:
                return
            
            while self._running:
                try:
                    self.log("🔍 [排程] 開始執行筆錄下載檢查...")
                    self.check_and_download()
                except Exception as e:
                    self.log(f"❌ [排程] 定期檢查失敗: {e}")
                    traceback.print_exc()
                
                # 等待 6 小時（分段等待以便能優雅退出）
                elapsed = 0
                while self._running and elapsed < self.CHECK_INTERVAL:
                    time.sleep(10)
                    elapsed += 10
        
        self._scheduler_thread = threading.Thread(target=periodic_check, daemon=True)
        self._scheduler_thread.start()
        
        self.log("✅ 電子筆錄自動下載排程已啟動（等待 1 分鐘後首次執行，之後每 6 小時）")
    
    def stop(self):

        self._running = False
        
        if self.downloader:
            self.downloader.close()
        
        self.log("✅ 電子筆錄自動下載排程已停止")
    
    def check_and_download(self):
        """
        排程檢查下載 (暴力歸檔模式)
        完全依賴 CourtRecordDownloader.run() 的邏輯:
        1. 下載所有案件
        2. 下載完一個案件馬上歸檔 (move_to_case_folder)
        3. 即使檔案重複也強制覆蓋
        """
        try:
            self.log("🚀 [排程] 啟動自動下載 (暴力歸檔模式)...")
            
            # 初始化下載器
            self.downloader = CourtRecordDownloader(
                username=self.username,
                password=self.password,
                db_manager=self.db_manager,
                download_folder=self.download_folder,
                headless=self.headless,
                log_callback=self.log_callback
            )
            
            # 直接執行下載流程 (已包含 download_all -> move_to_case_folder 邏輯)
            self.downloader.run()  
            
            self.log("✅ [排程] 檢查完成")
            
        except Exception as e:
            self.log(f"❌ [排程] 執行失敗: {e}")
            import traceback
            traceback.print_exc()
        finally:
            if self.downloader:
                self.downloader.close()
                self.downloader = None
    
    def rename_all_transcripts(self):
        """
        統一更名所有案件資料夾中的筆錄
        在下載完成後執行
        """
        global _global_transcript_operation_in_progress
        
        try:
            self.log("✏️ [更名] 開始統一更名所有筆錄...")
            
            # 創建臨時下載器來使用其解析方法
            temp_downloader = CourtRecordDownloader(
                username=self.username, password=self.password,
                db_manager=self.db_manager, headless=self.headless,
                log_callback=self.log_callback
            )
            
            cases = temp_downloader.get_cases_from_db()
            total_cases = len(cases)
            total_renamed = 0
            
            for case_idx, case in enumerate(cases, 1):
                if not case.folder_path:
                    continue
                
                local_path = self.translate_path_to_local(case.folder_path)
                transcript_folder = self._find_existing_transcript_folder(local_path) if local_path else None
                
                if not transcript_folder or not os.path.exists(transcript_folder):
                    continue
                
                pdf_files = [f for f in os.listdir(transcript_folder) if _is_real_pdf_filename(f)]
                if not pdf_files:
                    continue
                
                # 只在有檔案需要處理時顯示案件資訊
                case_has_rename = False
                
                for fname in pdf_files:
                    full_path = os.path.join(transcript_folder, fname)
                    try:
                        # ★ 檢查是否已經被改名過（非原始下載格式）
                        # 使用相同的檢查邏輯（透過 CourtRecordDownloader 的方法）
                        if hasattr(temp_downloader, '_is_original_download_filename') and not temp_downloader._is_original_download_filename(fname):
                            # 檔案已被改名過，跳過
                            continue

                        # 只有未標準化的真實 PDF 才進行解析，避免對已完成檔案
                        # 重複呼叫文字擷取或模型。
                        parse_result = temp_downloader._parse_record_pdf(full_path)
                        if not _record_parse_ready_for_filename(parse_result):
                            continue
                        
                        # 生成標準檔名
                        new_name = temp_downloader._generate_record_filename(parse_result, fname)
                        
                        if new_name != fname:
                            if not case_has_rename:
                                self.log(f"  📁 [{case_idx}/{total_cases}] {case.court_name} {case.court_case_number}")
                                case_has_rename = True
                            
                            new_full_path = os.path.join(transcript_folder, new_name)
                            
                            # 處理衝突
                            if os.path.exists(new_full_path):
                                name_part, ext_part = os.path.splitext(new_name)
                                counter = 2
                                while os.path.exists(new_full_path):
                                    new_name = f"{name_part}_{counter}{ext_part}"
                                    new_full_path = os.path.join(transcript_folder, new_name)
                                    counter += 1
                            
                            # 執行更名
                            os.rename(full_path, new_full_path)
                            self.log(f"    ✏️ {fname} → {new_name}")
                            total_renamed += 1
                            
                    except Exception as e:
                        self.log(f"    ⚠️ 更名失敗 ({fname}): {e}")
            
            self.log(f"✅ [更名] 完成！共更名 {total_renamed} 個檔案")
            
        except Exception as e:
            self.log(f"❌ [更名] 失敗: {e}")
            import traceback
            traceback.print_exc()

    
    
    def _calculate_pdf_content_hash(self, filepath: str) -> Optional[str]:
        """
        計算 PDF 的內容雜湊（基於提取的文字）
        用於偵測內容相同但下載時間不同（二進位不同）的重複檔案
        """
        import hashlib
        try:
            import fitz
            doc = fitz.open(filepath)
            if len(doc) == 0:
                doc.close()
                return self._calculate_file_md5(filepath)
            
            # 提取所有頁面的文字（或至少前幾頁）
            # 為了效率和準確性，提取全部文字
            full_text = ""
            for page in doc:
                full_text += page.get_text()
            doc.close()
            
            # 正規化：移除所有空白字符
            # 注意：這裡使用簡單的正規化，如果需要更嚴格可以濾除標點
            import re
            normalized_text = re.sub(r'\s+', '', full_text)
            
            # 如果提取不出文字（可能是掃描檔），退回使用檔案 MD5
            if not normalized_text:
                return self._calculate_file_md5(filepath)
            
            # 計算雜湊
            return hashlib.md5(normalized_text.encode('utf-8')).hexdigest()
            
        except Exception as e:
            # self.log(f"  ⚠️ 計算內容雜湊失敗: {e}，退回使用檔案 MD5")
            return self._calculate_file_md5(filepath)

    def _calculate_file_md5(self, filepath: str) -> Optional[str]:
        """計算去除 PDF 變動元資料後的內容 MD5。

        ezlawyer 每次下載同一份筆錄都會改變：
        1. CreationDate / ModDate（下載時間戳）
        2. 字型子集前綴（如 VTWNOS+DFKaiShu → UMGLIP+DFKaiShu）
        3. PDF /ID（文件唯一識別碼）

        全部歸零後再算 MD5，確保相同內容得到相同 hash。
        """
        try:
            import hashlib, re as _re
            with open(filepath, "rb") as _fh:
                data = _fh.read()
            # 1. 歸零時間戳
            data = _re.sub(
                rb"/(?:Creation|Mod)Date\s*\(D:\d{14}[^)]*\)",
                b"/CreationDate (D:00000000000000+00'00')",
                data,
            )
            # 2. 歸零字型子集隨機前綴（6個大寫字母+加號）
            data = _re.sub(
                rb"/BaseFont\s*/([A-Z]{6})\+",
                b"/BaseFont /AAAAAA+",
                data,
            )
            # 3. 歸零 PDF /ID
            data = _re.sub(
                rb"/ID\s*\[<[0-9A-Fa-f]+>\s*<[0-9A-Fa-f]+>\]",
                b"/ID [<00> <00>]",
                data,
            )
            return hashlib.md5(data).hexdigest()
        except Exception as e:
            self.log(f"  ⚠️ 計算 MD5 失敗: {e}")
            return None
    
    def _load_md5_records(self) -> Dict:
        records = {}
        if os.path.exists(self.md5_record_file):
            try:
                with open(self.md5_record_file, 'r', encoding='utf-8') as f:
                    loaded = json.load(f)
                    if isinstance(loaded, dict):
                        records.update(loaded)
            except Exception as e:
                self.log(f"  ⚠️ 載入 MD5 記錄失敗: {e}")
        try:
            from skills.ops.dedup_db import list_done as _dd_list
            for row in _dd_list("transcript_download_md5", limit=10000):
                md5_key = str(row.get("item_key") or "").strip()
                if not md5_key or md5_key in records:
                    continue
                meta = row.get("metadata")
                payload = {}
                if isinstance(meta, dict):
                    payload = meta
                elif isinstance(meta, str) and meta.strip():
                    try:
                        parsed = json.loads(meta)
                        if isinstance(parsed, dict):
                            payload = parsed
                    except Exception:
                        payload = {"raw_metadata": meta[:200]}
                payload.setdefault("synced_from", "dedup_db")
                records[md5_key] = payload
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4237, exc_info=True)
        return records

    def _save_md5_records(self, records: Dict):

        try:
            with open(self.md5_record_file, 'w', encoding='utf-8') as f:
                json.dump(records, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self.log(f"  ⚠️ 保存 MD5 記錄失敗: {e}")
        try:
            from skills.ops.dedup_db import mark_done as _dd_mark
            for md5_key, payload in (records or {}).items():
                md5_key = str(md5_key or "").strip()
                if not md5_key:
                    continue
                _dd_mark(
                    "transcript_download_md5",
                    md5_key,
                    metadata=payload if isinstance(payload, dict) else {"value": payload},
                )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4256, exc_info=True)
    
    def is_file_already_downloaded(self, filepath: str) -> bool:

        md5 = self._calculate_file_md5(filepath)
        if not md5:
            return False
        
        downloaded_md5s = self._load_md5_records()
        return md5 in downloaded_md5s
    
    def scan_and_update_md5_records(self):

        self.log("🔍 掃描下載資料夾，更新 MD5 記錄...")
        
        records = {}
        
        for filename in os.listdir(self.download_folder):
            filepath = os.path.join(self.download_folder, filename)
            
            # 跳過目錄和隱藏檔案
            if not os.path.isfile(filepath) or filename.startswith('.'):
                continue
            
            md5 = self._calculate_file_md5(filepath)
            if md5:
                records[md5] = {
                    'filename': filename,
                    'scanned_at': datetime.now().isoformat(),
                    'size': os.path.getsize(filepath)
                }
        
        self._save_md5_records(records)
        self.log(f"✅ 已更新 {len(records)} 個檔案的 MD5 記錄")
        
        return records

    def scan_case_folders_for_md5(self):
        """
        增量掃描案件資料夾以建立 MD5 記錄
        ★ 包含掃描協調機制：避免並行掃描（本類別 + 跨類別）
        """
        global _global_transcript_operation_in_progress
        
        # ★★★ 全域協調機制：跨類別防止並行 ★★★
        with _global_transcript_lock:
            if _global_transcript_operation_in_progress:
                self.log("⏳ [MD5] 另一個筆錄操作正在進行中，等待完成...")
                # 等待最多 30 秒
                wait_count = 0
                while _global_transcript_operation_in_progress and wait_count < 30:
                    _global_transcript_lock.release()
                    time.sleep(1)
                    _global_transcript_lock.acquire()
                    wait_count += 1
                
                if _global_transcript_operation_in_progress:
                    self.log("⚠️ [MD5] 等待超時，跳過本次掃描")
                    return
            
            _global_transcript_operation_in_progress = True
        
        # 本類別的鎖定
        with self._scan_lock:
            if self._scan_in_progress:
                self.log("⏳ [MD5] 本類別已有掃描正在進行中，跳過")
                with _global_transcript_lock:
                    _global_transcript_operation_in_progress = False
                return
            self._scan_in_progress = True
        
        self.log("🔍 [MD5] 開始增量掃描案件資料夾...")
        
        try:
            # 1. 取得所有案件
            temp_downloader = CourtRecordDownloader(
                username=self.username, password=self.password, 
                db_manager=self.db_manager, headless=self.headless
            )
            cases = temp_downloader.get_cases_from_db()
            total_cases = len(cases)
            self.log(f"📊 [MD5] 共有 {total_cases} 個案件需要掃描")
            
            # 2. 載入現有 MD5 記錄
            current_records = self._load_md5_records()
            # 建立反向索引：filepath -> md5 (為了檢查檔案是否已記錄)
            # 注意：這裡的 filepath 需要是絕對路徑才能比對
            # 但記錄檔裡存的是什麼？ records[md5] = { 'filename': ..., 'case_number': ... }
            # 記錄檔沒有存絕對路徑。我們無法單靠記錄檔得知該 MD5 對應哪個路徑。
            # 因此，增量掃描的策略稍微調整：
            # 我們必須遍歷檔案，計算 MD5 (或檢查大小/時間)，然後看這個 MD5 是否已存在記錄中。
            # 如果已存在，我們就不需要做與「已下載」相關的判斷，而是確認「這個檔案是已知的」。
            
            # 優化策略：
            # 我們建立一個 seen_files 映射: (full_path) -> {mtime, size, md5}
            # 這樣我們下次掃描時，如果 full_path 的 mtime/size 沒變，就直接用 cached md5。
            cache_file = os.path.join(self.download_folder, '.md5_scan_cache.json')
            file_cache = {}
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, 'r', encoding='utf-8') as f:
                        file_cache = json.load(f)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4330, exc_info=True)
            
            updated_any = False
            new_file_cache = {}
            total_files_scanned = 0
            total_files_cached = 0
            
            for case_idx, case in enumerate(cases, 1):
                # ★ 進度顯示（每10個案件或最後一個）
                if case_idx % 10 == 0 or case_idx == total_cases:
                    progress_pct = (case_idx / total_cases) * 100
                    self.log(f"📊 [MD5] 進度: {case_idx}/{total_cases} ({progress_pct:.1f}%)")
                
                if not case.folder_path:
                    continue
                
                local_path = self.translate_path_to_local(case.folder_path)
                
                # 收集要掃描的目標資料夾
                scan_targets = []
                
                # 1. 筆錄資料夾
                transcript_folder = self._find_existing_transcript_folder(local_path)
                if transcript_folder and os.path.exists(transcript_folder):
                    scan_targets.append(transcript_folder)
                    
                # 2. 閱卷資料夾 (新增)
                try:
                    for item in os.listdir(local_path):
                        full_path = os.path.join(local_path, item)
                        # 尋找包含「閱卷」的資料夾 (如 04_閱卷資料)
                        if os.path.isdir(full_path) and '閱卷' in item:
                            scan_targets.append(full_path)
                            break
                except:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4365, exc_info=True)
                
                if not scan_targets:
                    continue
                
                # 遞迴掃描所有 PDF
                pdf_files = []
                for target in scan_targets:
                    try:
                        for root, dirs, files in os.walk(target):
                            for f in files:
                                if _is_real_pdf_filename(f):
                                    # 存絕對路徑，稍後處理只能用相對路徑或檔名的部分會再調整
                                    # 但這裡 pdf_files 用於下方迴圈 os.stat(full_path)，所以這裡要是檔案名稱(與root結合)或是...
                                    # 原代碼: pdf_files = os.listdir... -> fname
                                    # full_path = os.path.join(transcript_folder, fname)
                                    # 為了最小改動下方迴圈，我們這裡收集元組 (root, list_of_files) ?
                                    # 不，下方直接遍歷 pdf_files 列表 (改為絕對路徑列表)
                                    pdf_files.append(os.path.join(root, f))
                    except:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4384, exc_info=True)
                
                if pdf_files:
                    self.log(f"  🔍 [{case_idx}/{total_cases}] {case.court_name} {case.court_case_number} - {len(pdf_files)} 份檔案 (筆錄/閱卷)")
                
                for full_path in pdf_files:
                    # fname 用於記錄檔，取 basename
                    fname = os.path.basename(full_path)
                    
                    try:
                        stat = os.stat(full_path)
                        mtime = stat.st_mtime
                        size = stat.st_size
                        
                        # Check cache
                        cached = file_cache.get(full_path)
                        md5 = None
                        
                        if cached and cached.get('mtime') == mtime and cached.get('size') == size:
                            md5 = cached.get('md5')
                        else:
                            # Recalculate
                            # self.log(f"  計算指紋: {fname}")
                            md5 = self._calculate_file_md5(full_path)
                            updated_any = True
                        
                        if md5:
                            # 更新 Cache
                            new_file_cache[full_path] = {
                                'mtime': mtime, 'size': size, 'md5': md5
                            }
                            
                            # 更新主 MD5 記錄 (如果不存在)
                            if md5 not in current_records:
                                current_records[md5] = {
                                    'filename': fname,
                                    'case_number': case.case_number,
                                    'court_case_number': case.court_case_number,
                                    'downloaded_at': datetime.now().isoformat(),
                                    'size': size,
                                    'source': 'scan'
                                }
                                updated_any = True
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4427, exc_info=True)
            
            # Save records
            if updated_any:
                self._save_md5_records(current_records)
                self.log(f"✅ [MD5] 增量掃描完成！已掃描 {total_cases} 個案件，更新了記錄")
            else:
                self.log(f"✅ [MD5] 掃描完成 ({total_cases} 個案件，無變更)")
            
            # Save Cache
            try:
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(new_file_cache, f, ensure_ascii=False)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4441, exc_info=True)
                
        except Exception as e:
            self.log(f"❌ [MD5] 掃描失敗: {e}")
            traceback.print_exc()
        finally:
            # ★★★ 釋放本類別鎖和全域鎖 ★★★
            self._scan_in_progress = False
            with _global_transcript_lock:
                _global_transcript_operation_in_progress = False
            self.log("🔓 [MD5] 掃描鎖已釋放")



# ==============================================================================
# 測試
# ==============================================================================


if __name__ == '__main__':
    print("=" * 60)
    print("司法院自動化模組測試")
    print("=" * 60)
    
    # 測試法院對應
    print("\n法院代碼測試:")
    test_courts = ["臺灣花蓮地方法院", "臺灣高等法院花蓮分院"]
    for court in test_courts:
        code = CourtMapping.get_court_code(court)
        print(f"  {court} -> {code}")
    
    # 測試簡易庭
    print("\n簡易庭測試:")
    test_cases = ["114年度宜簡字第123號", "114年度羅簡字第456號", "114年度訴字第789號"]
    for case in test_cases:
        simple = CourtMapping.get_simple_court(case)
        if simple:
            print(f"  {case} -> {simple[0]}")
        else:
            print(f"  {case} -> 非簡易案件")

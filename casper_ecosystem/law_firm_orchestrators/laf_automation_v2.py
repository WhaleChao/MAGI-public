import logging
# -*- coding: utf-8 -*-
"""
LAF Automation Module v2.0.0
法律扶助基金會自動化整合模組

整合到 LegalBridge / OSC (Paperclip) 系統

功能：
1. 監控 Gmail 中的法扶派案/審核通知信
2. 解析信件主旨取得案件資訊（案件類型、案由）
3. 自動登入律師線上操作系統 (lawyer.laf.org.tw)
4. 使用 RapidOCR 識別驗證碼
5. 下載各類通知文件 (PDF)
6. 整合 OSC 建立案件（正確選擇案件類型和案由）

Author: Claude (Anthropic)
Date: 2025-12
"""

import os
import queue
import sys
import re
import time
import json
import importlib
import pickle
import base64
import threading
import traceback
import tempfile
import shutil
import subprocess
import urllib.parse
import uuid
import types
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field

import importlib.util

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(_MAGI_ROOT / ".env")
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 50, exc_info=True)

from api.runtime_paths import (
    config_candidates,
    ensure_orch_on_sys_path,
    get_config_path,
    get_magi_root_dir,
)
from api.case_path_mapper import local_case_path_candidates, preferred_case_roots, translate_case_path_to_local
from skills.engine.legal_web_adapter import format_legal_web_engine_log, resolve_legal_web_engine

# ==============================================================================
# Event log (MemBridge / local JSONL) - best-effort
# ==============================================================================
def _eventlog(event: str, *, ok: Optional[bool] = None, payload: Optional[dict] = None, tags: Optional[dict] = None) -> None:
    try:
        ensure_orch_on_sys_path()
        import magi_eventlog  # type: ignore
        magi_eventlog.remember_event(event, ok=ok, payload=payload or {}, tags=tags or {}, source="laf_automation_v2")
    except Exception:
        return

# ==============================================================================
# Platform paths
# ==============================================================================

def _mac_synology_base() -> str:
    """
    取得本機 Synology Drive base（避免硬編使用者路徑）。
    """
    try:
        home = Path.home()
        cands = [
            home / "Library" / "CloudStorage" / "SynologyDrive-homes",
            home / "SynologyDrive",
        ]
        for p in cands:
            if p.exists():
                return str(p)
        return str(home / "SynologyDrive")
    except Exception:
        return "/Users/ai/SynologyDrive"


MAC_SYNO_BASE = _mac_synology_base()
MAC_SYNO_CASE_ROOT = os.path.join(MAC_SYNO_BASE, "01_案件")

# ==============================================================================
# 安全政策：禁止刪除（含下載暫存），避免誤刪 Synology Drive 內容
# ==============================================================================

NO_DELETE = os.environ.get("MAGI_NO_DELETE", "1").strip().lower() in {"1", "true", "yes", "on"}


def _safe_remove(path: str, log=None) -> None:
    if not path:
        return
    # Prefer project-wide safe_fs policy:
    # - Synology Drive: never delete
    # - MAGI_NO_DELETE=1: quarantine/keep (best-effort)
    try:
        ensure_orch_on_sys_path()
        import safe_fs
        safe_fs.safe_remove(path, reason="laf_tmp", allow_delete=True, log=log)
        return
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 115, exc_info=True)

    if NO_DELETE:
        if log:
            log(f"    ⏭️ 依政策不刪檔 (MAGI_NO_DELETE=1): {os.path.basename(path)}")
        return
    try:
        ensure_orch_on_sys_path()
        import safe_fs
        safe_fs.safe_remove(path)
        if log:
            log(f"    🗑️ 已刪除: {os.path.basename(path)}")
    except Exception as e:
        if log:
            log(f"    ⚠️ 刪除失敗: {e}")


def _safe_move(src: str, dst: str, log=None) -> None:
    if not src or not dst:
        return
    try:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 138, exc_info=True)
    if NO_DELETE:
        try:
            shutil.copy2(src, dst)
            if log:
                log(f"    ✓ 已複製(保留原檔): {os.path.basename(src)}")
        except Exception as e:
            if log:
                log(f"    ⚠️ 複製失敗: {e}")
        return
    try:
        shutil.move(src, dst)
        if log:
            log(f"    ✓ 已移動: {os.path.basename(src)}")
    except Exception as e:
        if log:
            log(f"    ⚠️ 移動失敗: {e}")


PDFTOTEXT_BIN = os.environ.get("MAGI_PDFTOTEXT_BIN", "/opt/homebrew/bin/pdftotext").strip()

# ==============================================================================
# 依賴檢查 (Lazy Load Setup)
# ==============================================================================
_dep_logger = logging.getLogger(__name__)

# Selenium
SELENIUM_AVAILABLE = importlib.util.find_spec("selenium") is not None
if not SELENIUM_AVAILABLE:
    _dep_logger.info("Selenium 未安裝，LAF 自動化功能無法使用")

# Playwright (preferred over Selenium since Chrome 147 macOS ARM regression)
PLAYWRIGHT_AVAILABLE = importlib.util.find_spec("playwright") is not None
if PLAYWRIGHT_AVAILABLE:
    _dep_logger.info("Playwright 可用，將優先作為 LAF 瀏覽器驅動（MAGI_LAF_ENGINE=selenium 可強制用 Selenium）")
else:
    _dep_logger.info("Playwright 未安裝，使用 Selenium 作為 LAF 瀏覽器驅動")

# RapidOCR
RAPIDOCR_AVAILABLE = importlib.util.find_spec("rapidocr_onnxruntime") is not None
if not RAPIDOCR_AVAILABLE:
    _dep_logger.debug("RapidOCR 未安裝，驗證碼自動識別功能無法使用")

# ddddocr (Primary)
DDDDOCR_AVAILABLE = importlib.util.find_spec("ddddocr") is not None
if DDDDOCR_AVAILABLE:
    _dep_logger.debug("[Import] ddddocr 模組可用")
else:
    _dep_logger.debug("[Import] ddddocr 模組未安裝")

# Google Gmail API
GMAIL_AVAILABLE = importlib.util.find_spec("googleapiclient") is not None and \
                  importlib.util.find_spec("google_auth_oauthlib") is not None and \
                  importlib.util.find_spec("google.auth") is not None

# PIL/Numpy
PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None and importlib.util.find_spec("numpy") is not None

# Placeholders
webdriver = None
Options = None


class _ByFallback:
    """Selenium By-compatible constants for use when Playwright driver is active
    (i.e. _create_chrome_driver() was never called so the real selenium.By was
    never lazily imported).  Playwright find_elements() accepts these strings."""
    XPATH = "xpath"
    CSS_SELECTOR = "css selector"
    TAG_NAME = "tag name"
    ID = "id"
    NAME = "name"
    CLASS_NAME = "class name"
    LINK_TEXT = "link text"
    PARTIAL_LINK_TEXT = "partial link text"


By = _ByFallback


class _WebDriverWaitFallback:
    """Minimal WebDriverWait shim for Playwright path (no Selenium installed)."""
    def __init__(self, driver, timeout, poll_frequency=0.5, ignored_exceptions=None):
        import time as _time
        self._driver = driver
        self._timeout = timeout
        self._poll = poll_frequency
        self._time = _time

    def until(self, condition, message=""):
        deadline = self._time.time() + self._timeout
        last_exc = None
        while self._time.time() < deadline:
            try:
                result = condition(self._driver)
                if result:
                    return result
            except Exception as exc:
                last_exc = exc
            self._time.sleep(self._poll)
        raise Exception(
            f"Condition not met after {self._timeout}s. {message}"
            + (f" Last exception: {last_exc}" if last_exc else "")
        )

    def until_not(self, condition, message=""):
        deadline = self._time.time() + self._timeout
        while self._time.time() < deadline:
            try:
                result = condition(self._driver)
                if not result:
                    return True
            except Exception:
                return True
            self._time.sleep(self._poll)
        raise Exception(f"Condition still true after {self._timeout}s. {message}")


WebDriverWait = _WebDriverWaitFallback


class _ECFallback:
    """Minimal expected_conditions shim for Playwright path."""
    @staticmethod
    def presence_of_element_located(locator):
        by, value = locator
        def _check(driver):
            try:
                el = driver.find_element(by, value)
                return el
            except Exception:
                return False
        return _check

    @staticmethod
    def frame_to_be_available_and_switch_to_it(locator):
        by, value = locator
        def _check(driver):
            try:
                driver.switch_to.frame(driver.find_element(by, value))
                return True
            except Exception:
                return False
        return _check

    @staticmethod
    def element_to_be_clickable(locator):
        by, value = locator
        def _check(driver):
            try:
                el = driver.find_element(by, value)
                return el if el else False
            except Exception:
                return False
        return _check

    @staticmethod
    def visibility_of_element_located(locator):
        by, value = locator
        def _check(driver):
            try:
                el = driver.find_element(by, value)
                return el if el else False
            except Exception:
                return False
        return _check


EC = _ECFallback
TimeoutException = None
NoSuchElementException = None
ElementClickInterceptedException = None

RapidOCR = None
ddddocr = None

Credentials = None
InstalledAppFlow = None
Request = None
build = None

Image = None
np = None


# ==============================================================================
# Playwright 相容層 — 從共用模組 import（skills/engine/playwright_wrapper.py）
# ==============================================================================
from skills.engine.playwright_wrapper import (
    PlaywrightElementWrapper,
    PlaywrightDriverWrapper,
    PlaywrightActionChains,
    PlaywrightSelect,
    PlaywrightWebDriverWait,
    _convert_script_for_playwright,
    _PlaywrightSwitchTo,
    _PlaywrightAlert,
    create_playwright_driver as _create_pw_driver_shared,
)
# ==============================================================================
# End Playwright 相容層
# ==============================================================================

# Module-level Selenium exception import (consolidated from method-level imports)
try:
    from selenium.common.exceptions import NoAlertPresentException
except Exception:  # pragma: no cover - selenium may not be installed in test env
    class NoAlertPresentException(Exception):
        pass


def _synthetic_ddddocr_package_name(package_dir: str) -> str:
    raw = re.sub(r"[^0-9A-Za-z_]+", "_", str(package_dir or "").strip()) or "default"
    return f"_magi_ddddocr_fallback_{raw}"


def _ensure_synthetic_package(module_name: str, package_dir: str) -> types.ModuleType:
    mod = sys.modules.get(module_name)
    if isinstance(mod, types.ModuleType):
        return mod
    pkg = types.ModuleType(module_name)
    pkg.__path__ = [package_dir]
    pkg.__file__ = os.path.join(package_dir, "__init__.py")
    pkg.__package__ = module_name
    sys.modules[module_name] = pkg
    return pkg


def _load_ddddocr_legacy_class(package_dir: str):
    legacy_py = os.path.join(package_dir, "compat", "legacy.py")
    if not os.path.exists(legacy_py):
        return None

    base_name = _synthetic_ddddocr_package_name(package_dir)
    compat_name = f"{base_name}.compat"
    legacy_name = f"{compat_name}.legacy"

    _ensure_synthetic_package(base_name, package_dir)
    _ensure_synthetic_package(compat_name, os.path.join(package_dir, "compat"))

    spec = importlib.util.spec_from_file_location(legacy_name, legacy_py)
    if spec is None or spec.loader is None:
        return None

    sys.modules.pop(legacy_name, None)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = compat_name
    sys.modules[legacy_name] = mod
    spec.loader.exec_module(mod)  # type: ignore[attr-defined]
    return getattr(mod, "DdddOcr", None)


def _resolve_ddddocr_class(log=None):
    def _emit(message: str) -> None:
        try:
            if log:
                log(message)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "resolve_ddddocr_class_log", exc_info=True)

    if not DDDDOCR_AVAILABLE:
        return None

    try:
        ddddocr_mod = importlib.import_module("ddddocr")
        cls = getattr(ddddocr_mod, "DdddOcr", None)
        if cls is not None:
            return cls
    except Exception as e:
        _emit(f"⚠️ [ddddocr] import 失敗，改用相容載入器: {e}")

    try:
        spec = importlib.util.find_spec("ddddocr")
        pkg_dir = ""
        if spec and spec.submodule_search_locations:
            pkg_dir = list(spec.submodule_search_locations)[0]
        if not pkg_dir:
            return None
        cls = _load_ddddocr_legacy_class(pkg_dir)
        if cls is not None:
            _emit(f"✅ [ddddocr] 使用 compat/legacy fallback 載入: {pkg_dir}")
            return cls
    except Exception as e:
        _emit(f"⚠️ [ddddocr] compat/legacy fallback 載入失敗: {e}")

    return None

# ==============================================================================
# 驗證碼（人工）協作通道：LINE 回傳四碼
# - 目的：正式站 CAPTCHA 屬安全機制；不提供自動破解/繞過
# - 作法：CASPER 抓取 CAPTCHA 截圖 → 匯出到 /static/exports → LINE 通知你點連結看圖並回覆四碼
# - MAGI API server（<MAGI_ROOT>/api/server.py）會攔截四碼回覆並寫入 response 檔案
# ==============================================================================

MAGI_ENV_PATH = Path(os.environ.get("MAGI_ENV_PATH", f"{_MAGI_ROOT}/.env"))
MAGI_AGENT_DIR = Path(os.environ.get("MAGI_AGENT_DIR", f"{_MAGI_ROOT}/.agent"))
MAGI_EXPORTS_DIR = Path(os.environ.get("MAGI_EXPORTS_DIR", f"{_MAGI_ROOT}/static/exports"))

LAF_CAPTCHA_REQUEST_FILE = Path(os.environ.get("MAGI_LAF_CAPTCHA_REQUEST_FILE", str(MAGI_AGENT_DIR / "laf_captcha_request.json")))
LAF_CAPTCHA_RESPONSE_FILE = Path(os.environ.get("MAGI_LAF_CAPTCHA_RESPONSE_FILE", str(MAGI_AGENT_DIR / "laf_captcha_response.json")))
LAF_CAPTCHA_TTL_SECONDS = int(os.environ.get("MAGI_LAF_CAPTCHA_TTL_SECONDS", "300") or "300")


def _load_dotenv_value(key: str, env_path: Path = MAGI_ENV_PATH) -> str:
    try:
        if not env_path.exists():
            return ""
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, _, v = s.partition("=")
                if k.strip() == key:
                    return (v or "").strip()
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 238, exc_info=True)
    return ""


def _get_public_base_url() -> str:
    base = (os.environ.get("MAGI_PUBLIC_BASE_URL") or "").strip()
    if not base:
        base = _load_dotenv_value("MAGI_PUBLIC_BASE_URL")
    return base.rstrip("/")


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _export_file_to_static(file_path: Path, prefix: str = "laf_captcha") -> dict:
    """
    Copy file to MAGI static exports and return public URL (if MAGI_PUBLIC_BASE_URL is available).
    """
    try:
        src = Path(file_path)
        if not src.exists():
            return {"success": False, "error": "file not found"}

        MAGI_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        token = uuid.uuid4().hex[:10]
        ext = src.suffix.lower() or ".bin"
        filename = f"{prefix}_{stamp}_{token}{ext}"
        dst = MAGI_EXPORTS_DIR / filename
        shutil.copy2(src, dst)

        base = _get_public_base_url()
        url = f"{base}/static/exports/{filename}" if base else ""
        return {"success": True, "path": str(dst), "filename": filename, "url": url}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ==============================================================================
# 資料結構
# ==============================================================================

@dataclass
class LAFCaseInfo:
    """
    法扶案件資訊
    
    從信件主旨解析出的完整案件資訊
    """
    # 基本資訊
    message_id: str = ""
    subject: str = ""
    sender: str = ""
    received_at: datetime = field(default_factory=datetime.now)
    body: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    
    # 案件資訊
    laf_case_number: str = ""      # 法扶案號，如 1141121-E-001
    client_name: str = ""          # 當事人姓名
    client_alias: str = ""         # 原名/別名
    branch: str = ""               # 分會名稱 (花蓮/桃園等)
    
    # OSC 案件類型對應
    case_type: str = ""            # OSC 案件類型：刑事、民事、行政、消費者債務清理、法律顧問、非訟
    case_stage: str = ""           # 案件階段：偵查、一審、二審...
    case_reason: str = ""          # 案由
    case_category: str = "法律扶助案件"  # 固定為法律扶助案件
    
    # 原始法扶資訊
    laf_case_type: str = ""        # 法扶原始案件類型文字
    
    # 承辦人資訊
    staff_name: str = ""
    staff_phone: str = ""
    staff_email: str = ""
    
    # 處理狀態
    has_attachment: bool = False   # 是否有《附加檔案》
    needs_download: bool = False   # 是否需要從系統下載
    notification_type: str = ""    # 派案通知 / 審核結果通知


# ==============================================================================
# 法扶案件類型解析器
# ==============================================================================

class LAFCaseTypeParser:
    """
    解析法扶信件主旨，對應到 OSC 案件類型
    
    法扶信件格式範例：
    - 【法扶花蓮分會派案通知】高弘軒-1141121-E-006-消費者債務清理事件-消費者債務清理程序
    - 【法扶台東分會派案通知】曾慧甄-1141106-J-011-民事通常程序第一審-袋地通行權案件之訴訟代理
    - 【法扶基隆分會審核結果通知】陳紫箖-1140502-K-001-刑事偵查中辯護-過失傷害等
    - 《附加檔案》【法扶花蓮分會派案通知】...
    """
    
    # OSC 案件類型對應表
    CASE_TYPE_MAPPING = {
        # 刑事類
        '刑事偵查': ('刑事', '偵查'),
        '刑事一審': ('刑事', '一審'),
        '刑事二審': ('刑事', '二審'),
        '刑事三審': ('刑事', '三審'),
        '刑事再審': ('刑事', '再審'),
        '刑事非常上訴': ('刑事', '非常上訴'),       
        '刑事執行': ('刑事', '執行'),
        '刑事自訴': ('刑事', '一審'),
        
        # 修正漏掉的類型
        '非常上訴': ('刑事', '非常上訴'),
        '再審': ('刑事', '再審'),
        '自訴': ('刑事', '一審'),
        '執行': ('刑事', '執行'),
        
        # 民事類
        '民事通常程序第一審': ('民事', '一審'),
        '民事通常程序第二審': ('民事', '二審'),
        '民事通常程序第三審': ('民事', '三審'),
        '民事簡易程序': ('民事', '一審'),
        '民事小額程序': ('民事', '一審'),
        '民事執行': ('民事', '執行'),
        '民事非訟': ('非訟', '其他'),
        '民事調解': ('民事', '其他'),
        
        # 家事類 (歸類為民事)
        '家事調解': ('民事', '其他'),
        '家事訴訟': ('民事', '一審'),
        '家事非訟': ('非訟', '其他'),
        
        # 行政類
        '行政訴訟': ('行政', '一審'),
        '行政訴訟第一審': ('行政', '一審'),
        '行政訴訟第二審': ('行政', '二審'),
        '訴願': ('行政', '其他'),
        
        # 消費者債務清理
        '消費者債務清理': ('消費者債務清理', '其他'),
        '消費者債務清理事件': ('消費者債務清理', '其他'),
        
        # 其他
        '法律諮詢': ('法律顧問', '其他'),
        '非訟': ('非訟', '其他'),
    }
    
    # 關鍵字對應（用於模糊匹配）
    KEYWORD_PATTERNS = [
        # (關鍵字模式, 案件類型, 案件階段)
        (r'偵查', '刑事', '偵查'),
        (r'刑事.*一審', '刑事', '一審'),
        (r'刑事.*二審', '刑事', '二審'),
        (r'刑事.*三審', '刑事', '三審'),
        (r'刑事.*再審', '刑事', '再審'),
        (r'刑事.*非常上訴|非常上訴', '刑事', '非常上訴'),
        (r'刑事.*執行|執行', '刑事', '執行'),
        (r'刑事.*自訴|自訴', '刑事', '一審'),
        (r'民事通常.*一審|民事通常程序第一審', '民事', '一審'),
        (r'民事通常.*二審|民事通常程序第二審', '民事', '二審'),
        (r'民事通常.*三審|民事通常程序第三審', '民事', '三審'),
        (r'民事簡易', '民事', '一審'),
        (r'民事小額', '民事', '一審'),
        (r'消費者債務清理', '消費者債務清理', '其他'),
        (r'家事', '民事', '一審'),
        (r'行政', '行政', '一審'),
        (r'訴願', '行政', '其他'),
        (r'非訟', '非訟', '其他'),
    ]
    
    # 常見案由清理規則
    REASON_CLEANUP_PATTERNS = [
        (r'^違反', ''),         # 移除開頭的「違反」
        (r'等$', ''),           # 移除結尾的「等」
        (r'案件之訴訟代理$', ''),  # 移除「案件之訴訟代理」
        (r'之訴訟代理$', ''),   # 移除「之訴訟代理」
        (r'案件$', ''),         # 移除結尾的「案件」
        (r'事件$', ''),         # 移除結尾的「事件」（除非是消債）
        (r'案$', ''),
        # 新增：處理再審/抗告等階段性詞彙（這些應該是「階段」而非「案由」的一部分）
        (r'之再審抗告$', ''),   # 移除「之再審抗告」
        (r'之再審$', ''),       # 移除「之再審」
        (r'之抗告$', ''),       # 移除「之抗告」
        (r'之非常上訴$', ''),   # 移除「之非常上訴」
    ]
    
    @classmethod
    def parse_subject(cls, subject: str) -> Optional[LAFCaseInfo]:
        """
        解析信件主旨
        
        Args:
            subject: 信件主旨
            
        Returns:
            LAFCaseInfo 或 None
        """
        info = LAFCaseInfo()
        info.subject = subject
        
        # 非派案類信件直接跳過（回報、結案、撤回等不應觸發開辦流程）
        _non_dispatch_keywords = ['律師回報', '回報(附條件)', '回報（附條件）', '結案回報', '撤回扶助']
        if any(kw in subject for kw in _non_dispatch_keywords):
            return None

        # 檢查是否有附加檔案標記
        if '《附加檔案》' in subject:
            info.has_attachment = True
            subject = subject.replace('《附加檔案》', '').strip()

        # 主旨格式：【法扶XX分會派案通知】當事人-案號-案件類型-案由
        # 或：【法扶XX分會審核結果通知】...
        
        # === 修正部分開始 ===
        # 修正重點：
        # 1. 單位支援「分會」或「中心」(使用非擷取群組 (?:...) 以保持 group index)
        # 2. 通知類型加入「審查結果通知」
        # 3. 支援缺少「法扶」前綴 (如「花蓮分會派案」) 以及簡寫「派案」
        branch_match = re.search(r'【(?:法扶)?(.+?)(?:分會|中心)?(派案通知|審核結果通知|審查結果通知|審查通知|第\d+次通知|派案)】', subject)
        # === 修正部分結束 ===

        if branch_match:
            info.branch = branch_match.group(1) # 這裡會抓到 "宜蘭" 或 "原住民族法律服務"
            info.notification_type = branch_match.group(2)
            
            # 取得主旨後半部分
            remaining = subject[branch_match.end():].strip()
            
            # 解析格式：當事人-案號-案件類型-案由
            parts = remaining.split('-')
            
            if len(parts) >= 5:
                # ... (以下程式碼保持不變) ...
                # 解析當事人
                client_part = parts[0]
                name_match = re.match(r'(.+?)\(原名[:：](.+?)\)', client_part)
                if name_match:
                    info.client_name = name_match.group(1).strip()
                    info.client_alias = name_match.group(2).strip()
                else:
                    info.client_name = client_part.strip()
                
                # 解析法扶案號
                if len(parts) >= 4:
                    info.laf_case_number = f"{parts[1]}-{parts[2]}-{parts[3]}"
                
                # 解析案件類型和案由
                info.laf_case_type = parts[4]
                if len(parts) >= 6:
                    raw_reason = '-'.join(parts[5:])
                    info.case_reason = cls._cleanup_reason(raw_reason)
                
                # 判斷 OSC 案件類型和階段
                info.case_type, info.case_stage = cls._determine_case_type(info.laf_case_type)
                
                # (V3-新增) 從案由中提取階段資訊
                if info.case_reason:
                    original_reason = info.case_reason
                    info.case_reason, extracted_stage = cls._extract_stage_from_reason(
                        info.case_reason, info.case_stage)
                    if extracted_stage in ['再審', '抗告', '非常上訴']:
                        print(f"DEBUG: 從案由提取階段: {original_reason} -> 案由={info.case_reason}, 階段={extracted_stage}")
                        info.case_stage = extracted_stage
                
                # (V-MacFix) 根據案由修正案件類型
                if info.case_reason:
                    # 1. 消費者債務清理 — 案由一律寫「更生」（有程序切換時再手動改）
                    if '消費者債務清理' in info.case_reason or '更生' in info.case_reason or '清算' in info.case_reason:
                        info.case_type = '消費者債務清理'
                        info.case_stage = '其他'
                        info.case_reason = '更生'
                        
                    # 2. 刑事關鍵字
                    criminal_keywords = ['強盜', '殺人', '毒品', '槍砲', '竊盜', '傷害', '詐欺', '侵占', '背信', '貪污', '賄賂', '妨害性自主', '公共危險', '過失致死', '非常上訴']
                    if info.case_type == '民事' and any(k in info.case_reason for k in criminal_keywords):
                        print(f"DEBUG: 修正案件類型 (關鍵字) {info.case_type} -> 刑事, 案由: {info.case_reason}")
                        info.case_type = '刑事'
                        if info.case_stage not in ['再審', '非常上訴']:
                            if '再審' in original_reason or '非常上訴' in original_reason:
                                info.case_stage = '再審' if '再審' in original_reason else '非常上訴'
                            else:
                                info.case_stage = '偵查' 
                            
                    # 3. 再審/非常上訴
                    if info.case_stage not in ['再審', '非常上訴']:
                        if '再審' in info.case_reason and info.case_type == '刑事':
                             info.case_stage = '再審'
                        elif '非常上訴' in info.case_reason and info.case_type == '刑事':
                             info.case_stage = '非常上訴'
                
                if info.case_type == '消費者債務清理' and not info.case_reason:
                    info.case_reason = '更生'
                
                info.needs_download = True
                return info

        # 2. 嘗試解析新格式：[XX分會]檢送1141127-J-001楊志杰之案件資料
        # 格式：[XX分會]檢送(案號)(當事人)之案件資料
        new_format_match = re.search(r'\[(.+?)分會\]檢送([A-Z0-9\-]+)(.+?)之案件資料', subject)
        if new_format_match:
            info.branch = new_format_match.group(1)
            info.notification_type = "派案通知" # 視為派案通知
            info.laf_case_number = new_format_match.group(2)
            info.client_name = new_format_match.group(3)
            
            # 預設值 (因為主旨沒有提供)
            info.case_type = "民事" 
            info.case_stage = "一審"
            info.case_reason = "待確認"
            info.laf_case_type = "一般案件"
            
            info.needs_download = True
            return info
        
        # 3. ★ 原民中心格式：寄送1141216-W-002、003[當事人J]案件資料
        # 格式：寄送(案號)[、XXX](當事人)案件資料
        indigenous_match = re.search(r'寄送(\d{7}-[A-Z]-\d{3})[、\d-]*(.+?)案件資料', subject)
        if indigenous_match:
            info.branch = "原住民族法律服務中心"
            info.notification_type = "派案通知"
            info.laf_case_number = indigenous_match.group(1)
            info.client_name = indigenous_match.group(2).strip()
            
            # 預設值
            info.case_type = "民事"
            info.case_stage = "一審"
            info.case_reason = "待確認"
            info.laf_case_type = "一般案件"
            
            info.has_attachment = True  # 這類信通常有附件
            info.needs_download = False  # 不需從系統下載
            return info

        return None
    
    @classmethod
    def _determine_case_type(cls, laf_case_type: str) -> Tuple[str, str]:
        """
        根據法扶案件類型判斷 OSC 案件類型和階段
        
        Args:
            laf_case_type: 法扶原始案件類型文字
            
        Returns:
            (案件類型, 案件階段)
        """
        # 1. 先嘗試精確匹配
        for key, (case_type, stage) in cls.CASE_TYPE_MAPPING.items():
            if key in laf_case_type:
                return case_type, stage
        
        # 2. 關鍵字模式匹配
        for pattern, case_type, stage in cls.KEYWORD_PATTERNS:
            if re.search(pattern, laf_case_type):
                return case_type, stage
        
        # 3. 預設為民事一審
        return '民事', '一審'
    
    @classmethod
    def _cleanup_reason(cls, raw_reason: str) -> str:
        """
        清理案由文字
        
        Args:
            raw_reason: 原始案由
            
        Returns:
            清理後的案由
        """
        reason = raw_reason.strip()
        
        # 套用清理規則
        for pattern, replacement in cls.REASON_CLEANUP_PATTERNS:
            # 特殊處理：消費者債務清理事件不移除「事件」
            if '消費者債務清理' in reason and '事件' in pattern:
                continue
            reason = re.sub(pattern, replacement, reason)
        
        return reason.strip()
    
    @classmethod
    def _extract_stage_from_reason(cls, raw_reason: str, current_stage: str) -> Tuple[str, str]:
        """
        從案由中提取階段資訊
        
        例如：「強盜殺人之再審抗告」-> (「強盜殺人」, 「再審」)
        
        Args:
            raw_reason: 原始案由
            current_stage: 目前判斷的階段
            
        Returns:
            (清理後案由, 修正後階段)
        """
        reason = raw_reason.strip()
        stage = current_stage
        
        # 定義階段關鍵字對應（順序很重要，優先匹配更具體的模式）
        stage_patterns = [
            (r'之再審抗告$', '再審'),
            (r'之再審$', '再審'),
            (r'之抗告$', '抗告'),
            (r'之非常上訴$', '非常上訴'),
            (r'再審抗告$', '再審'),
            (r'再審$', '再審'),  # 末尾有「再審」
        ]
        
        for pattern, extracted_stage in stage_patterns:
            if re.search(pattern, reason):
                reason = re.sub(pattern, '', reason).strip()
                stage = extracted_stage
                break
        
        return reason, stage
    
    @classmethod
    def check_needs_download(cls, email_body: str) -> bool:
        """
        檢查是否需要從系統下載
        
        Args:
            email_body: 信件內文
            
        Returns:
            是否需要下載
        """
        return '律師線上操作系統下載表單' in email_body or '律師線上操作系統' in email_body


# ==============================================================================
# 驗證碼識別器
# ==============================================================================

class CaptchaSolver:
    """
    LAF 驗證碼識別器
    
    優先使用 ddddocr (辨識率較高)，若無則使用 RapidOCR
    """
    
    MAX_RETRY = 3
    
    def __init__(self, callback_on_fail=None, log_callback=None):
        """
        初始化
        
        Args:
            callback_on_fail: 連續失敗時的回呼函式 callback()
            log_callback: 日誌回呼函式 (用於 UI 顯示)
        """
        self.ocr_engine = None
        self.dddd_ocr = None
        self.callback_on_fail = callback_on_fail
        self.log_callback = log_callback or print
        self._init_ocr()
    
    def log(self, msg):
        """統一輸出日誌"""
        if self.log_callback:
            self.log_callback(msg)
        else:
            print(msg)

    def _init_ocr(self):
        """初始化 OCR 引擎"""
        # 1. ddddocr (Primary)
        if DDDDOCR_AVAILABLE:
            DdddOcrCls = _resolve_ddddocr_class(log=self.log)

            if DdddOcrCls is not None:
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
                            self.log(f"📦 [ddddocr] Found frozen model: {onnx_path}")
                            onnx_kwargs['import_onnx_path'] = onnx_path
                        else:
                            self.log(f"⚠️ [ddddocr] frozen model not found in: {possible_paths}")

                    self.dddd_ocr = DdddOcrCls(**onnx_kwargs)
                    self.log("✅ ddddocr 引擎初始化成功")
                except Exception as e:
                    self.log(f"⚠️ ddddocr 初始化失敗: {e}")

        # 2. RapidOCR (Backup)
        if RAPIDOCR_AVAILABLE:
            global RapidOCR
            if RapidOCR is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                except ImportError:
                    pass

            if RapidOCR:
                try:
                    # rapidocr_onnxruntime 新版以 config.yaml 為主；傳入舊參數可能會 KeyError('model_path')。
                    # 這裡用預設初始化即可（CPU 模式）。
                    self.ocr_engine = RapidOCR()
                    self.log("✅ RapidOCR 引擎初始化成功")
                except Exception as e:
                    self.log(f"❌ RapidOCR 初始化失敗: {e}")
        
        if not self.dddd_ocr and not self.ocr_engine:
            self.log("⚠️ 無可用的 OCR 引擎")

    
    def solve(self, image_source) -> str:
        """
        識別驗證碼
        
        Args:
            image_source: 圖片來源（檔案路徑、PIL Image、numpy array 或 bytes）
            
        Returns:
            識別出的驗證碼文字
        """
        if not self.dddd_ocr and not self.ocr_engine:
            self.log("  ⚠️ OCR 引擎未初始化")
            return ""

        
        try:
            # 處理不同的輸入類型
            img_bytes = None
            img = None
            
            # Lazy Load PIL and Numpy
            global Image, np
            if Image is None:
                from PIL import Image
            if np is None:
                import numpy as np

            if isinstance(image_source, str):
                img = Image.open(image_source)
                with open(image_source, 'rb') as f:
                    img_bytes = f.read()
            elif isinstance(image_source, bytes):
                img_bytes = image_source
                import io
                img = Image.open(io.BytesIO(image_source))
            elif isinstance(image_source, np.ndarray):
                img = Image.fromarray(image_source)
                import io
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                img_bytes = buf.getvalue()
            elif isinstance(image_source, Image.Image):
                img = image_source
                import io
                buf = io.BytesIO()
                img.save(buf, format='PNG')
                img_bytes = buf.getvalue()
            else:
                self.log(f"⚠️ 不支援的圖片類型: {type(image_source)}")
                return ""

            candidates = []

            def _add_candidate(source: str, value: str):
                digits = re.sub(r'[^\d]', '', str(value or ''))
                if len(digits) >= 4:
                    candidates.append((source, digits[:4]))

            # =========================================================
            # ddddocr（搭配增強預處理）
            # =========================================================
            if self.dddd_ocr and img_bytes:
                # 先試原始圖片
                try:
                    res0 = self.dddd_ocr.classification(img_bytes)
                    res0 = re.sub(r'[^A-Za-z0-9]', '', res0)
                    self.log(f"  🔍 [ddddocr] 原始結果: {'可用' if len(res0) >= 4 else '不完整'}")
                    _add_candidate("dddd_raw", res0)
                except Exception as e:
                    self.log(f"  ⚠️ ddddocr 原始識別失敗: {e}")

                # 再試預處理版本（放大 3x + 二值化，去除噪點線條）
                try:
                    import io as _io
                    _img_pre = img.convert('RGB') if img else None
                    if _img_pre:
                        _gray = np.array(_img_pre.convert('L'))
                        # 二值化：深色字（灰階<140）→黑，背景→白
                        _bin = np.where(_gray < 140, 0, 255).astype(np.uint8)
                        _pil_bin = Image.fromarray(_bin).convert('L')
                        # 放大 3x
                        _pil_bin = _pil_bin.resize(
                            (_pil_bin.width * 3, _pil_bin.height * 3),
                            Image.Resampling.LANCZOS
                        )
                        _buf = _io.BytesIO()
                        _pil_bin.save(_buf, format='PNG')
                        _pre_bytes = _buf.getvalue()
                        res_pre = self.dddd_ocr.classification(_pre_bytes)
                        res_pre = re.sub(r'[^A-Za-z0-9]', '', res_pre)
                        self.log(f"  🔍 [ddddocr] 預處理結果: {'可用' if len(res_pre) >= 4 else '不完整'}")
                        _add_candidate("dddd_pre", res_pre)
                except Exception as e:
                    self.log(f"  ⚠️ ddddocr 預處理識別失敗: {e}")
            
            # =========================================================
            # RapidOCR（與 ddddocr 交叉比對，不只是 fallback）
            # =========================================================
            if self.ocr_engine:
                try:
                    import cv2

                    if img.mode != 'RGB':
                        img = img.convert('RGB')

                    rgb = np.array(img)
                    gray = np.array(img.convert('L'))

                    def _rapid_try(source: str, arr):
                        result, _ = self.ocr_engine(arr)
                        if result:
                            all_text = ''.join([line[1] for line in result])
                            _add_candidate(source, all_text)
                            return True
                        return False

                    threshold = 150
                    binary = np.where(gray < threshold, 0, 255).astype(np.uint8)
                    processed_img = Image.fromarray(binary)
                    processed_img = processed_img.resize(
                        (processed_img.width * 2, processed_img.height * 2),
                        Image.Resampling.LANCZOS,
                    )
                    processed_array = np.array(processed_img)

                    if getattr(self, '_debug_capture_enabled', lambda: False)():
                        try:
                            debug_processed = Path(tempfile.gettempdir()) / "debug_captcha_processed.png"
                            processed_img.save(debug_processed)
                            self.log(f"  📷 預處理後圖片: {debug_processed}")
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 895, exc_info=True)

                    _rapid_try("rapid_binary_150", processed_array)

                    for thresh in [120, 180, 100, 200]:
                        binary_test = np.where(gray < thresh, 0, 255).astype(np.uint8)
                        test_img = Image.fromarray(binary_test)
                        test_img = test_img.resize(
                            (test_img.width * 2, test_img.height * 2),
                            Image.Resampling.LANCZOS,
                        )
                        _rapid_try(f"rapid_thresh_{thresh}", np.array(test_img))

                    _rapid_try("rapid_gray", gray)

                    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
                    blue_mask = cv2.inRange(hsv, (75, 35, 20), (150, 255, 255))
                    blue_mask = cv2.GaussianBlur(blue_mask, (3, 3), 0)
                    blue_mask = cv2.threshold(blue_mask, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
                    blue_mask = cv2.resize(
                        blue_mask,
                        (blue_mask.shape[1] * 2, blue_mask.shape[0] * 2),
                        interpolation=cv2.INTER_CUBIC,
                    )
                    _rapid_try("rapid_blue_mask", blue_mask)

                except Exception as e:
                    self.log(f"  ⚠️ RapidOCR 失敗: {e}")

            if candidates:
                from collections import Counter
                dddd_values = {value for source, value in candidates if source.startswith("dddd")}
                rapid_values = [value for source, value in candidates if source.startswith("rapid")]
                rapid_counter = Counter(rapid_values)
                counts = Counter(value for _, value in candidates)
                cross_engine = [value for value in counts if value in dddd_values and value in rapid_counter]

                if cross_engine:
                    best = max(cross_engine, key=lambda value: (counts[value], rapid_counter[value], value))
                    self.log("  ✅ OCR 雙引擎結果一致")
                    return best

                prefer_engine = os.environ.get("LAF_OCR_PREFER_ENGINE", "rapid").strip().lower()
                if prefer_engine == "dddd" and dddd_values:
                    best = counts.most_common(1)[0][0]
                    self.log("  🔍 OCR 採用 ddddocr 候選")
                    return best
                if rapid_counter:
                    best = rapid_counter.most_common(1)[0][0]
                    self.log("  🔍 OCR 採用 RapidOCR 多策略候選")
                    return best

                best = counts.most_common(1)[0][0]
                self.log("  🔍 OCR 採用唯一可用候選")
                return best
            
            return ""
            
        except Exception as e:
            self.log(f"❌ 驗證碼識別失敗: {e}")
            import traceback
            traceback.print_exc()
            return ""
    
    def solve_with_retry(self, get_image_func, max_retry: int = None) -> str:
        """
        帶重試的驗證碼識別
        
        Args:
            get_image_func: 取得圖片的函式，每次呼叫應該重新取得新的驗證碼圖片
            max_retry: 最大重試次數
            
        Returns:
            識別出的驗證碼，如果連續失敗則回傳空字串
        """
        if max_retry is None:
            max_retry = self.MAX_RETRY
        
        for attempt in range(max_retry):
            try:
                image = get_image_func()
                result = self.solve(image)
                
                if result and len(result) >= 4:
                    self.log(f"✅ 驗證碼識別成功 (第 {attempt + 1} 次)")
                    return result
                
                self.log(f"⚠️ 驗證碼識別結果不完整 (第 {attempt + 1} 次)")
                time.sleep(1)  # 等待一秒後重試
                
            except Exception as e:
                self.log(f"❌ 驗證碼識別錯誤 (第 {attempt + 1} 次): {e}")
        
        # 連續失敗，呼叫回呼
        if self.callback_on_fail:
            self.callback_on_fail()
        
        return ""


# ==============================================================================
# LAF 網站自動化
# ==============================================================================

class LAFWebAutomation:
    """法扶律師線上操作系統自動化"""
    
    DEFAULT_BASE_URL = "https://lawyer.laf.org.tw"
    
    def __init__(self, username: str, password: str, download_folder: str,
                 headless: bool = False, on_captcha_fail=None, log_callback=None,
                 base_url: str = "", mock_mode: bool = False, browser_profile_dir: str = ""):
        """
        初始化
        
        Args:
            username: 律師線上操作系統帳號 (身分證字號)
            password: 密碼
            download_folder: 下載資料夾路徑
            headless: 是否使用無頭模式
            on_captcha_fail: 驗證碼連續失敗時的回呼
            log_callback: 日誌回呼函式 log_callback(message)
            base_url: 指定入口網址（預設為正式站）。測試用 Sandbox 可填 http://127.0.0.1:18080
            mock_mode: 測試模式（Sandbox 用），會繞過 OCR 驗證碼，避免卡住等待手動輸入
        """
        self.username = username
        self.password = password
        self.download_folder = Path(download_folder)
        self.headless = headless
        self.on_captcha_fail = on_captcha_fail
        self.log = log_callback or print
        self.mock_mode = bool(mock_mode) or os.environ.get("LAF_MOCK_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}

        # Base URL (production vs sandbox)
        env_base = os.environ.get("LAF_BASE_URL", "").strip()
        self.base_url = (base_url or env_base or self.DEFAULT_BASE_URL).rstrip("/")
        # Derived URLs (keep paths identical to production so the automation stays aligned)
        self.LOGIN_URL = f"{self.base_url}/lafcsp/"
        self.MAIN_URL = f"{self.base_url}/lafcsp/toMainPage"
        self.DOWNLOAD_URL = f"{self.base_url}/lafcsp/toDownloadList"
        
        self.driver = None
        self.web_engine_profile = resolve_legal_web_engine("laf_portal_v2", interactive_required=True)

        self._engine_logged = False
        self.last_debug_artifact = {}
        self.last_upload_result = {}
        # Pass log_callback specifically to capture OCR logs in UI
        self.captcha_solver = CaptchaSolver(callback_on_fail=on_captcha_fail, log_callback=self.log)
        # 驗證碼處理：優先 OCR 自動辨識，失敗才透過 LINE 請求人工輸入。
        self._captcha_override = os.environ.get("LAF_CAPTCHA", "").strip()
        # OCR 驗證碼：預設啟用（ddddocr / RapidOCR）。
        # 設定 LAF_ALLOW_OCR_CAPTCHA=0 可明確關閉 OCR（改回 LINE 人工模式）。
        env_deny_ocr = os.environ.get("LAF_ALLOW_OCR_CAPTCHA", "1").strip().lower() in {"0", "false", "no", "off"}
        self._allow_captcha_ocr = not env_deny_ocr

        # 瀏覽器 profile（用於保留 cookies / session，降低重複登入成本；不保證可永久免驗證碼）
        env_profile = os.environ.get("LAF_BROWSER_PROFILE_DIR", "").strip()
        default_profile = str(get_magi_root_dir() / ".runtime" / "laf_chrome_profile")
        self.browser_profile_dir = (browser_profile_dir or env_profile).strip()
        if not self.browser_profile_dir and not self.mock_mode:
            self.browser_profile_dir = default_profile
        if self.browser_profile_dir:
            try:
                p = Path(self.browser_profile_dir).expanduser()
                p.mkdir(parents=True, exist_ok=True)
                self.browser_profile_dir = str(p)
            except Exception:
                # If profile dir cannot be created, ignore and fall back to ephemeral profile.
                self.browser_profile_dir = ""

        # 確保下載資料夾存在
        self.download_folder.mkdir(parents=True, exist_ok=True)

        import atexit
        atexit.register(self._quit_driver)

    def _quit_driver(self):
        """確保 Chrome driver 在進程結束時一定被清理。"""
        driver = getattr(self, "driver", None)
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
            self.driver = None

    def __del__(self):
        self._quit_driver()

    def _dump_login_dom_summary(self):
        """輸出登入頁 DOM 摘要（避免寫入帳密），用於除錯 selector。"""
        try:
            out = {
                "url": getattr(self.driver, "current_url", ""),
                "title": getattr(self.driver, "title", ""),
                "inputs": [],
                "images": [],
                "links": [],
            }

            try:
                for el in self.driver.find_elements(By.CSS_SELECTOR, "input"):
                    out["inputs"].append({
                        "id": el.get_attribute("id") or "",
                        "name": el.get_attribute("name") or "",
                        "type": el.get_attribute("type") or "",
                        "placeholder": el.get_attribute("placeholder") or "",
                    })
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1086, exc_info=True)

            try:
                for el in self.driver.find_elements(By.TAG_NAME, "img"):
                    out["images"].append({
                        "id": el.get_attribute("id") or "",
                        "src": el.get_attribute("src") or "",
                        "alt": el.get_attribute("alt") or "",
                    })
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1096, exc_info=True)

            try:
                for el in self.driver.find_elements(By.TAG_NAME, "a")[:80]:
                    out["links"].append({
                        "id": el.get_attribute("id") or "",
                        "href": el.get_attribute("href") or "",
                        "onclick": el.get_attribute("onclick") or "",
                        "text": (el.text or "").strip()[:60],
                    })
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1107, exc_info=True)

            dom_path = self.download_folder / "login_dom_summary.json"
            with open(dom_path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            self.log(f"  🧾 DOM 摘要已保存: {dom_path}")
        except Exception as e:
            self.log(f"  ⚠️ 輸出 DOM 摘要失敗 (不影響流程): {e}")
    
    def _create_playwright_driver(self):
        """建立 Playwright Chromium 驅動器（委派給共用 factory）。"""
        dl_path = str(getattr(self, 'download_folder', '/tmp'))
        driver = _create_pw_driver_shared(
            headless=self.headless,
            download_dir=dl_path,
            page_load_timeout=60.0,
        )
        self.log("✅ Playwright Chromium 初始化成功（共用 factory）")
        return driver

    def _create_driver(self):
        """
        建立 WebDriver。
        優先嘗試 Playwright（預設），Selenium 作為 fallback。
        設定 MAGI_LAF_ENGINE=selenium 可強制用 Selenium。
        """
        if not self._engine_logged:
            self.log(format_legal_web_engine_log(self.web_engine_profile))
            self._engine_logged = True

        use_playwright = (
            PLAYWRIGHT_AVAILABLE
            and os.environ.get("MAGI_LAF_ENGINE", "playwright").strip().lower() != "selenium"
        )

        if use_playwright:
            try:
                return self._create_playwright_driver()
            except Exception as _pw_err:
                self.log(f"  ⚠️ Playwright 初始化失敗，回退到 Selenium: {_pw_err}")

        if not SELENIUM_AVAILABLE:
            raise RuntimeError("Selenium 未安裝，且 Playwright 不可用或已停用")

        # 偵測可用的瀏覽器
        browser_type, browser_path = self._detect_browser()

        if browser_type == 'edge':
            return self._create_edge_driver(browser_path)
        else:
            return self._create_chrome_driver(browser_path)
    
    def _detect_browser(self) -> tuple:
        """
        偵測可用的瀏覽器
        
        Returns:
            (browser_type, browser_path) - 'chrome' 或 'edge'
        """
        if sys.platform == 'darwin':
            # macOS 瀏覽器路徑
            browsers = [
                # Chrome 優先
                ('chrome', "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                ('chrome', "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"),
                ('chrome', os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")),
                ('chrome', "/opt/homebrew/bin/chromium"),
                ('chrome', "/usr/local/bin/chromium"),
                # Edge
                ('edge', "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
                # Brave (用 Chrome driver)
                ('chrome', "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser"),
            ]
        elif sys.platform == 'win32':
            # Windows 瀏覽器路徑
            browsers = [
                ('chrome', r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
                ('chrome', r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
                ('edge', r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                ('edge', r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
            ]
        else:
            # Linux
            browsers = [
                ('chrome', "/usr/bin/google-chrome"),
                ('chrome', "/usr/bin/chromium-browser"),
                ('edge', "/usr/bin/microsoft-edge"),
            ]
        
        for browser_type, path in browsers:
            if os.path.exists(path):
                self.log(f"✅ 找到瀏覽器: {browser_type.upper()} @ {path}")
                return (browser_type, path)
        
        # 找不到任何瀏覽器
        self.log("⚠️ 找不到任何支援的瀏覽器，嘗試過以下路徑:")
        for browser_type, path in browsers:
            self.log(f"   - [{browser_type}] {path}")
        
        return ('chrome', None)  # 讓 Selenium 自己嘗試
    
    def _create_chrome_driver(self, binary_path: str = None):
        """建立 Chrome WebDriver (含反偵測措施)"""
        # Lazy Load Selenium
        global webdriver, Options, By, WebDriverWait, EC, ActionChains
        if webdriver is None:
            try:
                from selenium import webdriver
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC
                from selenium.webdriver.chrome.options import Options
                from selenium.webdriver.common.action_chains import ActionChains
            except ImportError:
                return None

        chrome_options = Options()
        # Chrome 147: eager + headless=new 可能導致 session 不穩定，改用 normal
        chrome_options.page_load_strategy = 'normal'

        if self.headless:
            chrome_options.add_argument('--headless=new')

        # Reuse a persistent browser profile when configured (keeps session cookies).
        if getattr(self, "browser_profile_dir", ""):
            chrome_options.add_argument(f"--user-data-dir={self.browser_profile_dir}")
        
        # 下載設定
        prefs = {
            "download.default_directory": str(self.download_folder.absolute()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            "plugins.always_open_pdf_externally": True,
            # 停用自動化相關提示
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        }
        chrome_options.add_experimental_option("prefs", prefs)
        
        # === 反偵測措施 ===
        # 排除自動化標記
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option("useAutomationExtension", False)
        
        # 基本參數（Chrome 147+ session 穩定性修正）
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--window-size=1920,1080')
        chrome_options.add_argument('--disable-popup-blocking')
        chrome_options.add_argument('--remote-allow-origins=*')
        chrome_options.add_argument('--disable-features=RendererCodeIntegrity,IsolateOrigins,site-per-process')
        
        # 反偵測參數
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('--disable-infobars')
        chrome_options.add_argument('--disable-extensions')
        
        # 模擬真實瀏覽器
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        if binary_path:
            chrome_options.binary_location = binary_path
        
        try:
            _FORCE_CD_VERSION = os.environ.get("MAGI_CHROMEDRIVER_VERSION", "147.0.7727.57").strip()
            if importlib.util.find_spec("webdriver_manager") is not None:
                from webdriver_manager.chrome import ChromeDriverManager
                from selenium.webdriver.chrome.service import Service
                try:
                    service = Service(ChromeDriverManager(driver_version=_FORCE_CD_VERSION).install())
                except Exception:
                    service = Service(ChromeDriverManager().install())
                return webdriver.Chrome(service=service, options=chrome_options)
            else:
                 return webdriver.Chrome(options=chrome_options)
        except ImportError:
            return webdriver.Chrome(options=chrome_options)
        except Exception as e:
            self.log(f"⚠️ 建立 Chrome Driver 失敗: {e}")
            return webdriver.Chrome(options=chrome_options)
    
    def _create_edge_driver(self, binary_path: str = None):
        """建立 Edge WebDriver (含反偵測措施)"""
        # Lazy Load Selenium
        global webdriver
        if webdriver is None:
             try:
                from selenium import webdriver
             except ImportError:
                 return None

        try:
            from selenium.webdriver.edge.options import Options as EdgeOptions
        except ImportError:
            return None

        edge_options = EdgeOptions()
        
        if self.headless:
             edge_options.add_argument('--headless=new')

        # Reuse a persistent browser profile when configured (keeps session cookies).
        if getattr(self, "browser_profile_dir", ""):
            edge_options.add_argument(f"--user-data-dir={self.browser_profile_dir}")
        
        # 下載設定
        prefs = {
            "download.default_directory": str(self.download_folder.absolute()),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": True,
            "plugins.always_open_pdf_externally": True,
            # 停用自動化相關提示
            "credentials_enable_service": False,
            "profile.password_manager_enabled": False,
        }
        edge_options.add_experimental_option("prefs", prefs)
        
        # === 反偵測措施 ===
        # 排除自動化標記
        edge_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        edge_options.add_experimental_option("useAutomationExtension", False)
        
        # 基本參數
        edge_options.add_argument('--disable-gpu')
        edge_options.add_argument('--no-sandbox')
        edge_options.add_argument('--disable-dev-shm-usage')
        edge_options.add_argument('--window-size=1920,1080')
        
        # 反偵測參數
        edge_options.add_argument('--disable-blink-features=AutomationControlled')
        edge_options.add_argument('--disable-infobars')
        edge_options.add_argument('--disable-extensions')
        
        # 模擬真實瀏覽器
        edge_options.add_argument('--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0')
        
        if binary_path:
             edge_options.binary_location = binary_path
        
        try:
            if importlib.util.find_spec("webdriver_manager") is not None:
                from webdriver_manager.microsoft import EdgeChromiumDriverManager
                from selenium.webdriver.edge.service import Service as EdgeService
                service = EdgeService(EdgeChromiumDriverManager().install())
                return webdriver.Edge(service=service, options=edge_options)
            else:
                return webdriver.Edge(options=edge_options)
        except ImportError:
            return webdriver.Edge(options=edge_options)
        except Exception as e:
            self.log(f"⚠️ 建立 Edge Driver 失敗: {e}")
            return webdriver.Edge(options=edge_options)
    

    def _get_captcha_image_playwright(self) -> "np.ndarray":
        """Playwright 專用驗證碼圖片取得：優先用同 session HTTP 下載，再退回元素截圖。"""
        global np
        if np is None:
            import numpy as np
        import io
        from PIL import Image

        pw: PlaywrightDriverWrapper = self.driver  # type: ignore

        # 策略一：直接用 Playwright 的 request context 下載 captcha。
        # 法扶站的 captcha 是動態 JPEG；元素截圖在 headless 模式偶爾會拿到
        # 抗鋸齒/縮放後的影像，OCR 穩定度差。request context 與 browser
        # context 共用 cookies，同 session GET 會同步更新伺服器端 captcha。
        try:
            from urllib.parse import urljoin as _urljoin
            try:
                pw._page.wait_for_selector('img#kaptchaImage, img[src*="captcha"]', timeout=8000, state='attached')
            except Exception:
                pass
            candidates = []
            for frame in pw._page.frames:
                try:
                    src = frame.evaluate("""() => {
                        const img = document.querySelector('img#kaptchaImage') ||
                                    document.querySelector('img[src*="captcha-image"]') ||
                                    document.querySelector('img[src*="captcha"]');
                        return img ? new URL(img.getAttribute('src') || img.src, location.href).href : '';
                    }""")
                    if src:
                        candidates.append(str(src))
                except Exception:
                    continue
            candidates.extend([
                _urljoin(self.LOGIN_URL, "/lafcsp/captcha-image"),
                _urljoin(f"{self.base_url.rstrip('/')}/", "lafcsp/captcha-image"),
            ])
            seen = set()
            for captcha_url in candidates:
                if not captcha_url or captcha_url in seen:
                    continue
                seen.add(captcha_url)
                resp = pw._page.request.get(captcha_url)
                if not resp.ok:
                    self.log(f"  ⚠️ Playwright HTTP 驗證碼下載失敗: status={getattr(resp, 'status', '')}")
                    continue
                img_bytes = resp.body()
                img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
                if self._debug_capture_enabled():
                    debug_path = self.download_folder / 'debug_captcha_http.png'
                    img.save(debug_path)
                    self.log(f"  📷 驗證碼 HTTP 下載: {debug_path}")
                self.log("  ✅ Playwright HTTP 驗證碼下載成功")
                return np.array(img)
        except Exception as e:
            self.log(f"  ⚠️ Playwright HTTP 驗證碼下載失敗: {e}")

        # 策略二：等待 img#kaptchaImage 出現（JS 動態注入，最多等 10s）
        captcha_selectors = [
            "img#kaptchaImage",
            "img[src*='captcha-image']",
            "img[src*='captcha']",
        ]
        for sel in captcha_selectors:
            try:
                # 先搜尋所有 frame（含主 page + iframe）
                found_el = None
                for frame in pw._page.frames:
                    try:
                        frame.wait_for_selector(sel, timeout=8000, state='attached')
                        found_el = frame.query_selector(sel)
                        if found_el:
                            self.log(f"  🔍 Playwright 找到驗證碼: {sel} (frame={frame.name or 'main'})")
                            break
                    except Exception:
                        continue
                if found_el:
                    img_bytes = found_el.screenshot()
                    if img_bytes:
                        img = Image.open(io.BytesIO(img_bytes))
                        if self._debug_capture_enabled():
                            debug_path = self.download_folder / 'debug_captcha.png'
                            img.save(debug_path)
                            self.log(f"  📷 驗證碼截圖: {debug_path}")
                        return np.array(img)
            except Exception as e:
                self.log(f"  ⚠️ Playwright captcha selector {sel} 失敗: {e}")
                continue

        # 策略三：JavaScript canvas 截取
        self.log("  🔄 嘗試 JavaScript canvas 截取驗證碼...")
        try:
            img_b64 = pw._page.evaluate("""() => {
                const img = document.querySelector('img#kaptchaImage') ||
                            document.querySelector('img[src*="captcha-image"]') ||
                            document.querySelector('img[src*="captcha"]');
                if (!img) return null;
                const canvas = document.createElement('canvas');
                canvas.width = img.naturalWidth || img.width || 80;
                canvas.height = img.naturalHeight || img.height || 35;
                const ctx = canvas.getContext('2d');
                ctx.drawImage(img, 0, 0);
                return canvas.toDataURL('image/png').split(',')[1];
            }""")
            if img_b64:
                import base64
                img_bytes = base64.b64decode(img_b64)
                img = Image.open(io.BytesIO(img_bytes))
                self.log("  ✅ JS canvas 截取驗證碼成功")
                return np.array(img)
        except Exception as e:
            self.log(f"  ⚠️ JS canvas 截取失敗: {e}")

        raise Exception("Playwright 三種策略均無法取得驗證碼圖片")

    def _get_captcha_image(self) -> "np.ndarray":
        """從網頁取得驗證碼圖片"""
        global np
        if np is None:
            import numpy as np
        # PIL Image 必須在函數開頭 import；line 1854 的 `from PIL import Image`
        # 會把 Image 變成 local，導致 HTTP path 的 `Image.open` 觸發 UnboundLocalError。
        from PIL import Image

        # Playwright 使用專用方法（支援 frame 搜尋 + HTTP 直接下載）
        if isinstance(self.driver, PlaywrightDriverWrapper):
            return self._get_captcha_image_playwright()

        try:
            # Selenium 元素截圖在新版 LAF 登入頁會被 input-group/CSS 裁切，
            # 常只截到 captcha 的一部分。先用同 session cookie 直接抓原始
            # /captcha-image，讓伺服器端 session 與我們要填的四碼一致。
            try:
                import io as _io
                import urllib.request as _urlrequest
                from urllib.parse import urljoin as _urljoin

                candidates = []
                try:
                    src = self.driver.execute_script("""
                        const img = document.querySelector('img#kaptchaImage') ||
                                    document.querySelector('img[src*="captcha-image"]') ||
                                    document.querySelector('img[src*="captcha"]');
                        return img ? new URL(img.getAttribute('src') || img.src, location.href).href : '';
                    """)
                    if src:
                        candidates.append(str(src))
                except Exception:
                    pass
                candidates.extend([
                    _urljoin(self.LOGIN_URL, "/lafcsp/captcha-image"),
                    _urljoin(f"{self.base_url.rstrip('/')}/", "lafcsp/captcha-image"),
                ])
                cookies = []
                try:
                    for c in self.driver.get_cookies() or []:
                        name = str(c.get("name") or "").strip()
                        value = str(c.get("value") or "").strip()
                        if name:
                            cookies.append(f"{name}={value}")
                except Exception:
                    cookies = []
                headers = {"User-Agent": "Mozilla/5.0"}
                if cookies:
                    headers["Cookie"] = "; ".join(cookies)
                last_err = ""
                seen = set()
                # LAF portal TLS 憑證 Missing Subject Key Identifier — Python urllib 嚴格驗證會拒絕。
                # 此處用 unverified context 繞過（只影響本 captcha 圖下載；登入仍走 Playwright）
                # 否則會 fallback 到元素截圖，OCR 品質差導致連續失敗。
                import ssl as _ssl
                _laf_ssl_ctx = _ssl.create_default_context()
                _laf_ssl_ctx.check_hostname = False
                _laf_ssl_ctx.verify_mode = _ssl.CERT_NONE
                for captcha_url in candidates:
                    if not captcha_url or captcha_url in seen:
                        continue
                    seen.add(captcha_url)
                    try:
                        req = _urlrequest.Request(captcha_url, headers=headers)
                        with _urlrequest.urlopen(req, timeout=8, context=_laf_ssl_ctx) as resp:
                            img_bytes = resp.read()
                        img = Image.open(_io.BytesIO(img_bytes)).convert("RGB")
                        if self._debug_capture_enabled():
                            debug_path = self.download_folder / 'debug_captcha_http.png'
                            img.save(debug_path)
                            self.log(f"  📷 驗證碼 HTTP 下載: {debug_path}")
                        self.log("  ✅ HTTP 驗證碼下載成功")
                        return np.array(img)
                    except Exception as _one_err:
                        last_err = str(_one_err)
                if last_err:
                    raise RuntimeError(last_err)
            except Exception as e:
                self.log(f"  ⚠️ HTTP 驗證碼下載失敗，改用元素截圖: {e}")

            selectors = [
                # LAF 正式站：固定為 /lafcsp/captcha-image
                "img#kaptchaImage[src*='captcha-image']",
                "span.code img#kaptchaImage",
                "img#kaptchaImage",
                "span.code img[src*='captcha-image']",
                "span.code img[src*='captcha']",
                "img[src*='captcha-image']",
                "img[src*='captcha']",
                # 其他站點/相容 selector（放後面避免誤抓到圖示或無關圖片）
                "img[alt*='驗證']",
                "img[src*='checkCode']",
                "#captchaImg",
                ".captcha-img img",
            ]

            captcha_img = None
            for selector in selectors:
                try:
                    el = WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                    )
                    if not el:
                        continue
                    # Guard rails: must be an <img> with captcha-ish src/id and reasonable size.
                    try:
                        if (el.tag_name or "").strip().lower() != "img":
                            continue
                        src = (el.get_attribute("src") or "").lower()
                        el_id = (el.get_attribute("id") or "").strip()
                        if ("captcha" not in src) and (el_id != "kaptchaImage"):
                            continue
                        sz = el.size or {}
                        if int(sz.get("width") or 0) < 30 or int(sz.get("height") or 0) < 12:
                            continue
                    except Exception:
                        continue
                    captcha_img = el
                    self.log(f"  🔍 找到驗證碼圖片: {selector}")
                    break
                except Exception:
                    continue

            if not captcha_img:
                all_imgs = self.driver.find_elements(By.TAG_NAME, "img")
                self.log("  ⚠️ 找不到驗證碼，頁面上的圖片:")
                for img in all_imgs[:15]:
                    src = img.get_attribute('src') or '(無 src)'
                    alt = img.get_attribute('alt') or '(無 alt)'
                    self.log(f"     - src={src[:50]}... alt={alt}")
                raise Exception("找不到驗證碼圖片元素")

            img_bytes = captcha_img.screenshot_as_png
            import io
            from PIL import Image

            img = Image.open(io.BytesIO(img_bytes))
            if self._debug_capture_enabled():
                debug_path = self.download_folder / 'debug_captcha.png'
                img.save(debug_path)
                self.log(f"  📷 驗證碼圖片已保存: {debug_path}")
            return np.array(img)

        except Exception as e:
            self.log(f"❌ 取得驗證碼圖片失敗: {e}")
            raise

    def _refresh_captcha(self):
        """重新整理驗證碼"""
        try:
            # Prefer explicit refresh link to avoid accidental navigation / opening image resources.
            selectors = [
                "a[onclick*='changeCode']",
                "a[title*='重整']",
                "a[data-original-title*='重整']",
                "span.input-group-addon a[onclick*='changeCode']",
                "i.icon-sync",
            ]
            for selector in selectors:
                try:
                    el = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if (el.tag_name or "").strip().lower() == "i":
                        try:
                            el = el.find_element(By.XPATH, "./ancestor::a[1]")
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1418, exc_info=True)
                    el.click()
                    time.sleep(0.6)
                    return
                except Exception:
                    continue
            # Last resort: call page JS if present.
            try:
                self.driver.execute_script("if (typeof changeCode === 'function') { changeCode(); }")
                time.sleep(0.6)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1429, exc_info=True)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1431, exc_info=True)

    def _debug_capture_enabled(self) -> bool:
        v = str(os.environ.get("MAGI_LAF_DEBUG_CAPTURE", "0")).strip().lower()
        return v in {"1", "true", "yes", "on"}

    def _save_page_debug_html(self, tag: str, force: bool = False):
        """保存當前頁面 HTML 與截圖供除錯，並回傳檔案資訊。"""
        if not force and not self._debug_capture_enabled():
            return {}
        if not self.driver or not self.download_folder:
            return {}
        artifact = {"tag": tag, "html": "", "png": "", "ts": int(time.time())}
        try:
            ts = artifact["ts"]
            prefix = f"debug_{tag}_{ts}"
            
            # HTML
            try:
                html = self.driver.page_source or ""
                p = self.download_folder / f"{prefix}.html"
                with open(p, "w", encoding="utf-8") as f:
                    f.write(html)
                artifact["html"] = str(p)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1456, exc_info=True)
                
            # Screenshot
            try:
                p = self.download_folder / f"{prefix}.png"
                self.driver.save_screenshot(str(p))
                artifact["png"] = str(p)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1464, exc_info=True)
                
            self.log(f"  📷 已保存除錯截圖與 HTML: {prefix}")
            self.last_debug_artifact = dict(artifact)
        except Exception:
            return {}
        return artifact

    def _workflow_form_modal_selectors(self, workflow: str) -> List[str]:
        wf = (workflow or "").strip().lower()
        preferred = {
            "go_live": ["#dialog-notOpenedReply"],
            "closing": ["#dialog-closingReply"],
            "withdrawal": ["#dialog-closingReply"],
            "condition": ["#dialog-conditionReply"],
            "inquiry": ["#dialog-inquiryReply"],
            "progress": ["#dialog-notClosedReply"],
        }.get(wf, [])
        fallback = ["#dialog-notOpenedReply", "#dialog-closingReply", "#dialog-conditionReply", "#dialog-inquiryReply"]
        out: List[str] = []
        for sel in preferred + fallback:
            if sel and sel not in out:
                out.append(sel)
        return out

    def _restore_workflow_form_modal(self, workflow: str, close_upload_dialog: bool = True) -> Dict[str, Any]:
        """Hide upload/status overlays and restore the workflow form modal for preview/capture."""
        if not self.driver:
            return {}
        try:
            info = self.driver.execute_script(
                """
                const formSelectors = arguments[0] || [];
                const closeUploadDialogFlag = !!arguments[1];

                const uniq = (items) => {
                  const out = [];
                  for (const one of (items || [])) {
                    if (one && !out.includes(one)) out.push(one);
                  }
                  return out;
                };
                const isVisible = (node) => {
                  if (!node) return false;
                  const st = window.getComputedStyle(node);
                  if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
                  if (node.hidden) return false;
                  const rect = node.getBoundingClientRect();
                  return rect.width > 1 && rect.height > 1;
                };
                const forceHide = (sel) => {
                  const node = document.querySelector(sel);
                  if (!node) return false;
                  try {
                    if (typeof jQuery !== 'undefined') jQuery(sel).modal('hide');
                  } catch (e) {}
                  try { node.classList.remove('show'); } catch (e) {}
                  try { node.style.display = 'none'; } catch (e) {}
                  try { node.setAttribute('aria-hidden', 'true'); } catch (e) {}
                  return true;
                };

                const actions = [];
                if (closeUploadDialogFlag && typeof closeUploadDialog === 'function') {
                  try {
                    closeUploadDialog();
                    actions.push('closeUploadDialog');
                  } catch (e) {
                    actions.push('closeUploadDialog:error');
                  }
                }
                if (typeof closeModal1 === 'function') {
                  try {
                    closeModal1();
                    actions.push('closeModal1');
                  } catch (e) {
                    actions.push('closeModal1:error');
                  }
                }
                if (forceHide('#dialog-form')) actions.push('hide:#dialog-form');
                if (forceHide('#dialog-uploading')) actions.push('hide:#dialog-uploading');
                if (forceHide('#dialog-msg')) actions.push('hide:#dialog-msg');

                let removedBackdrops = 0;
                const backdrops = Array.from(document.querySelectorAll('.modal-backdrop'));
                for (const bd of backdrops) {
                  try { bd.remove(); removedBackdrops += 1; } catch (e) {}
                }
                try { document.body.style.removeProperty('padding-right'); } catch (e) {}

                const candidates = uniq(formSelectors.concat([
                  '#dialog-notOpenedReply',
                  '#dialog-closingReply',
                  '#dialog-conditionReply',
                  '#dialog-inquiryReply'
                ]));
                let restored = '';
                let formVisible = false;
                for (const sel of candidates) {
                  const node = document.querySelector(sel);
                  if (!node) continue;
                  restored = sel;
                  try {
                    if (typeof jQuery !== 'undefined') jQuery(sel).modal('show');
                  } catch (e) {}
                  try { node.style.display = 'block'; } catch (e) {}
                  try { node.classList.add('show'); } catch (e) {}
                  try { node.setAttribute('aria-modal', 'true'); } catch (e) {}
                  try { node.removeAttribute('aria-hidden'); } catch (e) {}
                  formVisible = isVisible(node);
                  if (formVisible) break;
                }
                try {
                  if (restored && formVisible) document.body.classList.add('modal-open');
                  else document.body.classList.remove('modal-open');
                } catch (e) {}

                return {
                  actions: actions,
                  restored: restored,
                  formVisible: formVisible,
                  uploadDialogVisible: isVisible(document.querySelector('#dialog-form')),
                  uploadingVisible: isVisible(document.querySelector('#dialog-uploading')),
                  messageVisible: isVisible(document.querySelector('#dialog-msg')),
                  backdropsRemoved: removedBackdrops
                };
                """,
                self._workflow_form_modal_selectors(workflow),
                bool(close_upload_dialog),
            ) or {}
            if isinstance(info, dict) and info:
                return dict(info)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "restore_workflow_form_modal", exc_info=True)
        return {}

    def _wait_workflow_preview_ready(self, workflow: str, fields: Dict[str, Any] = None, timeout_sec: float = 8.0) -> bool:
        """Wait until upload overlays are gone and the workflow form is visible with final values."""
        if not self.driver:
            return False
        data = dict(fields or {})
        expected_result = str(data.get("sel_result") or data.get("result") or "").strip()
        expected_remark = str(data.get("remark") or data.get("desc") or "").strip()
        expect_uploads = len(list(data.get("upload_files") or []))
        deadline = time.time() + max(1.0, float(timeout_sec or 8.0))
        last_state: Dict[str, Any] = {}

        while time.time() < deadline:
            self._restore_workflow_form_modal(workflow, close_upload_dialog=True)
            if data:
                try:
                    self.fill_workflow_fields(workflow, data)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "preview_refill", exc_info=True)
            try:
                state = self.driver.execute_script(
                    """
                    const selectors = arguments[0] || [];
                    const uniq = (items) => {
                      const out = [];
                      for (const one of (items || [])) {
                        if (one && !out.includes(one)) out.push(one);
                      }
                      return out;
                    };
                    const isVisible = (node) => {
                      if (!node) return false;
                      const st = window.getComputedStyle(node);
                      if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
                      if (node.hidden) return false;
                      const rect = node.getBoundingClientRect();
                      return rect.width > 1 && rect.height > 1;
                    };
                    const val = (sel) => {
                      const node = document.querySelector(sel);
                      return node ? String(node.value || '').trim() : '';
                    };
                    const formSelectors = uniq(selectors.concat([
                      '#dialog-notOpenedReply',
                      '#dialog-closingReply',
                      '#dialog-conditionReply',
                      '#dialog-inquiryReply'
                    ]));
                    let formSelector = '';
                    let formVisible = false;
                    for (const sel of formSelectors) {
                      const node = document.querySelector(sel);
                      if (!node) continue;
                      formSelector = sel;
                      formVisible = isVisible(node);
                      if (formVisible) break;
                    }

                    let uploadRows = 0;
                    const uploadTexts = [];
                    const rows = document.querySelectorAll('#uploadDocnms tr, #uploadDocnmsY tr');
                    rows.forEach((row) => {
                      const hasData = row.querySelector("a[href*='downloadFile'], a[href*='viewUploadFileDetail']");
                      if (!hasData) return;
                      uploadRows += 1;
                      const txt = (row.textContent || '').replace(/\\s+/g, ' ').trim();
                      if (txt) uploadTexts.push(txt);
                    });

                    const bodyText = document.body ? String(document.body.innerText || '') : '';
                    const uploadingVisible = isVisible(document.querySelector('#dialog-uploading'));
                    const uploadDialogVisible = isVisible(document.querySelector('#dialog-form'));
                    const messageVisible = isVisible(document.querySelector('#dialog-msg'));
                    const busyText = (
                      bodyText.indexOf('檔案正在上傳中') >= 0 ||
                      bodyText.indexOf('上傳中') >= 0 ||
                      bodyText.indexOf('請稍等') >= 0
                    );

                    return {
                      formSelector: formSelector,
                      formVisible: formVisible,
                      result: val('#selResult'),
                      remark: val('#selRemark'),
                      uploadRows: uploadRows,
                      uploadText: uploadTexts.join(' || '),
                      uploadingVisible: uploadingVisible,
                      uploadDialogVisible: uploadDialogVisible,
                      messageVisible: messageVisible,
                      busy: uploadingVisible || uploadDialogVisible || messageVisible || busyText
                    };
                    """,
                    self._workflow_form_modal_selectors(workflow),
                ) or {}
            except Exception:
                state = {}

            if isinstance(state, dict):
                last_state = dict(state)
                form_visible = bool(state.get("formVisible"))
                busy = bool(state.get("busy"))
                current_result = str(state.get("result") or "").strip()
                current_remark = str(state.get("remark") or "").strip()
                upload_rows = int(state.get("uploadRows") or 0)
                result_ready = (not expected_result) or (current_result == expected_result)
                remark_ready = (not expected_remark) or (current_remark == expected_remark)
                uploads_ready = (expect_uploads == 0) or (upload_rows > 0)
                if form_visible and (not busy) and result_ready and remark_ready and uploads_ready:
                    return True

            time.sleep(0.5)

        if last_state:
            self.log(
                "  ⚠️ 預覽頁面未完全穩定；將以目前畫面繼續。"
                f" formVisible={last_state.get('formVisible')} busy={last_state.get('busy')}"
                f" result={last_state.get('result')} uploads={last_state.get('uploadRows')}"
            )
        return False

    def _dismiss_post_login_popups(self):
        """
        關閉登入後可能出現的彈窗（如密碼變更提醒）。

        Portal 在登入後會顯示 Bootstrap Modal 提醒律師變更密碼，
        內含「關閉」和「前往修改」按鈕。MAGI 需要點擊「關閉」才能繼續操作。
        """
        try:
            # Switch to content frame if in frameset
            self._switch_to_content_frame_if_any()
            time.sleep(1.0)

            # Try to find and close password change reminder modal
            # The modal typically has a "關閉" button or a close (X) button
            dismissed = self.driver.execute_script("""
                // Look for Bootstrap modal close buttons
                var buttons = document.querySelectorAll(
                    '.modal .btn-default, .modal .btn-secondary, .modal [data-dismiss="modal"], '
                    + '.modal .close, .modal button'
                );
                for (var i = 0; i < buttons.length; i++) {
                    var txt = (buttons[i].textContent || '').trim();
                    if (txt === '關閉' || txt === '取消' || txt === 'Close' || txt === '×') {
                        buttons[i].click();
                        return 'dismissed: ' + txt;
                    }
                }
                // Also try clicking overlay/backdrop to close
                var backdrop = document.querySelector('.modal-backdrop');
                if (backdrop) {
                    // Try to hide modal via jQuery
                    if (typeof jQuery !== 'undefined') {
                        jQuery('.modal').modal('hide');
                        return 'dismissed via jQuery';
                    }
                }
                // Check if modal exists but no close button found
                var modal = document.querySelector('.modal.show, .modal.in, .modal[style*="display: block"]');
                if (modal) {
                    // Force hide
                    modal.style.display = 'none';
                    modal.classList.remove('show', 'in');
                    if (backdrop) backdrop.remove();
                    document.body.classList.remove('modal-open');
                    return 'force-hidden';
                }
                return 'no_modal';
            """)
            if dismissed and dismissed != 'no_modal':
                self.log(f"  🔒 Post-login popup: {dismissed}")
                time.sleep(0.5)
        except Exception as e:
            self.log(f"  ⚠️ Post-login popup check: {e}")

        # Also handle JS alert dialogs
        try:
            alert = self.driver.switch_to.alert
            alert_text = (alert.text or "").strip()
            self.log(f"  ⚠️ Post-login alert: {alert_text[:80]}")
            alert.accept()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1534, exc_info=True)

    def _handle_password_reminder_page(self) -> bool:
        """Handle the post-login 90-day password reminder interstitial."""
        if not self.driver:
            return False
        try:
            src = self.driver.page_source or ""
            cur = self.driver.current_url or ""
            if "changePwReminderResult" not in src and "請每90天變更一次使用者密碼" not in src:
                return False

            self.log("  🔐 偵測到密碼變更提醒頁，選擇三個月後再提醒。")
            clicked = self.driver.execute_script("""
                // LAF portal 的密碼提醒頁有 bug：頁面有 2 個 form（login + reminder），
                // 但 button click handler 寫死 document.forms[0].submit()，submit 到 LOGIN form
                // 而不是 reminder form → server 視為非法 → 重導 toLogin → 我們以為登入失敗
                //
                // 正確做法：直接 set reminderResult.value="remindLater" 然後 submit reminder form
                // （action 含 changePwReminderResult），不要透過 button click。

                // Step 1: set hidden reminderResult value to button id (mimic LAF JS behavior)
                var input = document.querySelector('#reminderResult, input[name="reminderResult"]');
                if (input) {
                    input.value = 'remindLater';
                }

                // Step 2: 直接 submit reminder form（找 action 含 changePwReminderResult 的 form）
                var allForms = Array.from(document.querySelectorAll('form'));
                for (var i = 0; i < allForms.length; i++) {
                    var f = allForms[i];
                    var act = (f.action || '') + '';
                    if (act.indexOf('changePwReminderResult') >= 0 || act.indexOf('Reminder') >= 0) {
                        try {
                            f.submit();
                            return 'reminderFormSubmit:' + act.slice(0, 80);
                        } catch(e) {
                            return 'reminderFormSubmitErr:' + e.message;
                        }
                    }
                }

                // Step 3 (兜底): 找 form 含 reminderResult input 的 form 直接 submit
                if (input) {
                    var f2 = input.closest('form');
                    if (f2) {
                        try { f2.submit(); return 'closestFormSubmit:' + (f2.action || ''); } catch(e) {}
                    }
                }

                // Step 4 (最後兜底): 點 button（舊行為，但 LAF JS 會把 reminderResult 寫死成 button id）
                var btn = document.querySelector('#remindLater');
                if (btn) {
                    try { btn.click(); return 'btnClickFallback'; } catch(e) {}
                }

                return '';
            """)
            if not clicked:
                return False

            # 等待頁面變更（30 秒）
            deadline = time.time() + 30
            while time.time() < deadline:
                time.sleep(0.5)
                try:
                    now_url = self.driver.current_url or ""
                    now_src = self.driver.page_source or ""
                    if "toMainPage" in now_url:
                        self.log("  ✅ 密碼提醒頁已略過。")
                        return True
                    if not ("changePwReminderResult" in now_src or "請每90天變更一次使用者密碼" in now_src):
                        # reminder 標記消失但 URL 不一定是 toMainPage（可能是 frameset 或 toLogin）
                        if "toLogin" in now_url:
                            return False  # session invalidated
                        self.log("  ✅ 密碼提醒頁已略過。")
                        return True
                except Exception:
                    pass
            return False
        except Exception as e:
            self.log(f"  ⚠️ 密碼提醒頁處理失敗: {e}")
            return False

    def _current_page_looks_authenticated(self) -> bool:
        """Best-effort detection for an already-authenticated LAF portal page."""
        if not self.driver:
            return False
        try:
            src = self.driver.page_source or ""
            title = (self.driver.title or "").strip()
            cur = self.driver.current_url or ""
            if self.driver.find_elements(By.CSS_SELECTOR, "input[name='user_id'], input[name='user_pass'], #loginLink"):
                return False
            if any(m in src for m in ["自動登出", "案件狀態區", "待處理案件", "追蹤案件", "最新公告"]):
                return True
            if "toMainPage" in cur:
                return True
            if title == "線上回報":
                return True
            if self.driver.find_elements(By.CSS_SELECTOR, "frame[name='contentFrame'], frame[name='footerFrame']"):
                return True
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "current_page_auth_check", exc_info=True)
        return False

    def login(self) -> bool:
        """登入 LAF 律師線上操作系統"""
        retry_count = 0
        ocr_rejected_count = 0
        try:
            max_login_retry = int(os.environ.get('LAF_LOGIN_MAX_RETRY', '8').strip() or '8')
        except Exception:
            max_login_retry = 8
        try:
            captcha_ocr_retry = int(os.environ.get('LAF_CAPTCHA_MAX_RETRY', '5').strip() or '5')
        except Exception:
            captcha_ocr_retry = 5
        captcha_ocr_retry = max(1, captcha_ocr_retry)

        def _mark_ocr_rejected():
            nonlocal ocr_rejected_count
            ocr_rejected_count += 1
            self.log("  🔁 OCR 驗證碼被入口拒絕，刷新驗證碼後自動重試。")

        while retry_count < max_login_retry:
            if not self.driver:
                try:
                    self.driver = self._create_driver()
                except Exception as e:
                    self.log(f"  ⚠️ 初始化瀏覽器失敗: {e}")
                    _eventlog("laf:portal:login", ok=False, payload={"stage": "create_driver", "error": str(e)[:220]}, tags={"base_url": self.base_url})
                    retry_count += 1
                    continue

            if not self.driver:
                retry_count += 1
                continue

            try:
                # 0) 優先嘗試沿用既有 session（有設定 persistent profile 時可大幅降低 CAPTCHA 次數）
                try:
                    self.driver.get(self.MAIN_URL)
                    time.sleep(1.0)
                    src0 = (self.driver.page_source or "")
                    cur0 = (self.driver.current_url or "").lower()
                    if any(k in cur0 for k in ("tologin", "timeout=y", "/logout")):
                        raise RuntimeError("session_not_valid")
                    if self._handle_password_reminder_page() or self._current_page_looks_authenticated():
                        self.log("✅ LAF 已登入（沿用既有 session）")
                        _eventlog("laf:portal:login", ok=True, payload={"method": "session_reuse_page"}, tags={"base_url": self.base_url})
                        self._dismiss_post_login_popups()
                        return True
                    # 嚴格判定：需在主頁面上找到至少 2 個 markers
                    main_markers = ["案件狀態區", "待處理案件", "追蹤案件", "最新公告"]
                    _matched_markers = sum(1 for m in main_markers if m in src0)
                    if _matched_markers >= 2 or ("toMainPage" in (self.driver.current_url or "")):
                        # 二次驗證：嘗試打 AJAX 檢查 session 是否真的有效
                        # (避免導覽到子頁面消耗 CSRF token)
                        _session_ok = False
                        try:
                            _ajax_ok = self.driver.execute_script("""
                                try {
                                    var xhr = new XMLHttpRequest();
                                    xhr.open('GET', '/lafcsp/toClosedReport', false);
                                    xhr.send(null);
                                    // 如果被重導到登入頁，responseText 會包含 login
                                    if (xhr.status === 200 && xhr.responseText.indexOf('applyno') > -1) {
                                        return 'ok';
                                    }
                                    return 'redirect:' + xhr.status;
                                } catch(e) { return 'error:' + e.message; }
                            """)
                            _session_ok = (_ajax_ok == "ok")
                            if not _session_ok:
                                self.log(f"⚠️ Session reuse AJAX verify: {_ajax_ok}")
                        except Exception as _ve:
                            self.log(f"⚠️ Session reuse verify exception: {_ve}")
                        if _session_ok:
                            self.log("✅ LAF 已登入（沿用既有 session，已驗證）")
                            _eventlog("laf:portal:login", ok=True, payload={"method": "session_reuse"}, tags={"base_url": self.base_url})
                            self._dismiss_post_login_popups()
                            return True
                        else:
                            raise RuntimeError("session_verify_failed")
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1600, exc_info=True)

                self.log(f"🔐 正在登入 LAF 律師線上操作系統 (第 {retry_count + 1} 次)...")
                _eventlog("laf:portal:login", ok=None, payload={"method": "full_login", "attempt": retry_count + 1}, tags={"base_url": self.base_url})
                self.driver.get(self.LOGIN_URL)
                time.sleep(2)

                if self._debug_capture_enabled():
                    from api.debug_capture import save_debug_screenshot
                    save_debug_screenshot(self.driver, f"debug_login_page_{retry_count + 1}", context="法扶登入頁")
                    self.log(f"  📷 登入頁面截圖: debug_login_page_{retry_count + 1}")
                self._dump_login_dom_summary()

                if self._handle_password_reminder_page() or self._current_page_looks_authenticated():
                    self.log("✅ LAF 已登入（登入入口已是 portal 頁）")
                    self._dismiss_post_login_popups()
                    return True

                # ── Playwright 原生登入路徑（更可靠，直接使用 page.fill/click）──
                if isinstance(self.driver, PlaywrightDriverWrapper):
                    self.log("  🎭 使用 Playwright 原生登入路徑...")
                    _pw_page = self.driver._page
                    try:
                        # 等待驗證碼圖片出現
                        _pw_page.wait_for_selector('img#kaptchaImage', timeout=10000, state='visible')
                        # 取驗證碼
                        self.log("  🤖 以 OCR 自動辨識驗證碼...")
                        captcha_text = self.captcha_solver.solve_with_retry(
                            get_image_func=lambda: (self._refresh_captcha() or self._get_captcha_image()),
                            max_retry=captcha_ocr_retry,
                        )
                        if not captcha_text:
                            self.log("  ⚠️ OCR 失敗，重試登入")
                            retry_count += 1
                            continue
                        self.log("✅ 驗證碼識別成功 (第 1 次)")
                        self.log('  🔢 已取得驗證碼（不顯示於日誌）')
                        # 填入帳號、密碼、驗證碼（使用 Playwright 原生 fill，保證填入）
                        _pw_page.fill("input[name='user_id']", self.username)
                        _pw_page.fill("input[name='user_pass']", self.password)
                        _pw_page.fill("input[name='capText']", captcha_text)
                        # 驗證填入值
                        _u = _pw_page.input_value("input[name='user_id']")
                        _c = _pw_page.input_value("input[name='capText']")
                        self.log(f"  ✅ 表單填入確認: user_id={'已填' if _u else '空'}, captcha={'已填' if _c else '空'}")
                        if not _u:
                            self.log("  ❌ username 填入失敗，重試")
                            retry_count += 1
                            continue
                        # 提交（先試 checkForm()，失敗則直接 click loginLink）
                        try:
                            _pw_page.evaluate("() => { if(typeof checkForm==='function') checkForm(); }")
                        except Exception:
                            pass
                        time.sleep(0.5)
                        # 如果還在登入頁，改 click loginLink
                        if 'toMainPage' not in (_pw_page.url or '') and 'processLogin' not in (_pw_page.url or ''):
                            try:
                                _pw_page.click('#loginLink, a#loginLink', timeout=3000)
                            except Exception:
                                pass
                        # 等待登入結果（最多 25 秒）
                        _login_ok_pw = False
                        for _wi in range(25):
                            time.sleep(1)
                            _url = _pw_page.url or ''
                            # 成功條件：toMainPage 或離開 processLogin（含 CSRF nonce 的 processLogin 是成功跳轉）
                            if 'toMainPage' in _url:
                                _login_ok_pw = True
                                break
                            # processLogin 完成後會跳到 toMainPage 或帶 CSRF_NONCE → 再等一秒讓它 redirect
                            if 'processLogin' in _url and 'CSRF_NONCE' in _url:
                                # 表示登入正在處理中，繼續等待
                                continue
                            # 主頁內容確認 — 直接 markers 或 frameset 結構
                            try:
                                _src = _pw_page.content()
                                if any(m in _src for m in ["自動登出", "案件狀態區", "待處理案件", "追蹤案件", "最新公告"]):
                                    _login_ok_pw = True
                                    break
                                # ★ frameset 結構偵測：登入成功後 LAF 可能直接給 frameset 主框（URL 仍 processLogin）
                                # frameset 含 contentFrame/footerFrame → 已登入主頁，內容在 sub-frame
                                if ("contentFrame" in _src and "footerFrame" in _src) or ("toPublishmentList" in _src):
                                    _login_ok_pw = True
                                    break
                            except Exception:
                                pass
                        # 若 loop 結束後仍在 processLogin，再做一次內容確認（portal 重定向有時超過 25s）
                        if not _login_ok_pw:
                            try:
                                _url = _pw_page.url or ''
                                if 'processLogin' in _url and 'CSRF_NONCE' in _url:
                                    _src = _pw_page.content()
                                    if any(m in _src for m in ["自動登出", "案件狀態區", "待處理案件", "追蹤案件", "最新公告", "toMainPage"]):
                                        _login_ok_pw = True
                                    elif ("contentFrame" in _src and "footerFrame" in _src) or ("toPublishmentList" in _src):
                                        _login_ok_pw = True
                            except Exception:
                                pass
                        if not _login_ok_pw and self._handle_password_reminder_page():
                            _login_ok_pw = True
                        self.log(f"  🔗 當前 URL: {_pw_page.url}")
                        if _login_ok_pw:
                            self.log('✅ LAF 登入成功！（Playwright 原生路徑）')
                            _eventlog("laf:portal:login", ok=True, payload={"method": "playwright_native"}, tags={"base_url": self.base_url})
                            self._dismiss_post_login_popups()
                            return True
                        else:
                            self.log(f"❌ 登入失敗，可能是驗證碼錯誤 (第 {retry_count + 1} 次)")
                            _mark_ocr_rejected()
                            retry_count += 1
                            continue
                    except Exception as _pw_login_err:
                        self.log(f"  ⚠️ Playwright 原生登入路徑失敗: {_pw_login_err}，fallback 到 Selenium 相容路徑")
                        # fall through to old path

                wait = WebDriverWait(self.driver, 30)

                def _pick_interactable(css: str):
                    def _finder(_):
                        els = self.driver.find_elements(By.CSS_SELECTOR, css)
                        if not els:
                            return False
                        for e in els:
                            try:
                                if e.is_displayed() and e.is_enabled():
                                    return e
                            except Exception:
                                continue
                        return els[0]
                    return wait.until(_finder)

                def _fill_login_input(el, value: str):
                    v = str(value or "")
                    # Playwright 原生 fill()：最可靠，直接用
                    if isinstance(el, PlaywrightElementWrapper):
                        try:
                            el._el.fill(v)
                            # 觸發 input/change event 讓 JS 框架知道值已改
                            el._el.evaluate(
                                "el => { el.dispatchEvent(new Event('input',{bubbles:true})); "
                                "el.dispatchEvent(new Event('change',{bubbles:true})); }"
                            )
                            return
                        except Exception as _pw_fill_err:
                            logging.getLogger(__name__).debug("PW fill err: %s", _pw_fill_err)
                            # fallback to type()
                            try:
                                el._el.clear()
                            except Exception:
                                pass
                            try:
                                el._el.type(v)
                                return
                            except Exception:
                                pass
                    # Selenium 路徑
                    try:
                        self.driver.execute_script(
                            """
                            const el=arguments[0], v=arguments[1];
                            try { el.focus(); } catch(e) {}
                            try { el.value=''; } catch(e) {}
                            try { el.value=v; } catch(e) {}
                            try { el.dispatchEvent(new Event('input',{bubbles:true})); } catch(e) {}
                            try { el.dispatchEvent(new Event('change',{bubbles:true})); } catch(e) {}
                            try { el.blur(); } catch(e) {}
                            """,
                            el,
                            v,
                        )
                        if (el.get_attribute("value") or "") == v:
                            return
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1648, exc_info=True)
                    try:
                        el.clear()
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1652, exc_info=True)
                    el.send_keys(v)

                username_input = _pick_interactable("input[name='user_id'], input#user_id, input[name='userId']")
                _fill_login_input(username_input, self.username)

                password_input = _pick_interactable(
                    "input[name='user_pass'], input#password, input[type='password'], input[name='password']"
                )
                _fill_login_input(password_input, self.password)

                captcha_input = _pick_interactable(
                    "input[name='capText'], #kaptcha, input[name='captcha'], input[placeholder*='驗證'], input[name='checkCode']"
                )

                # 驗證碼處理：環境變數覆寫 → OCR 自動辨識；不走人工回覆，半夜可自動重試。
                captcha_text = ''
                captcha_source = ''
                if self.mock_mode:
                    captcha_text = '0000'
                    captcha_source = 'mock_mode'
                    self.log('  🧪 [MockMode] 使用固定驗證碼 0000')
                    _eventlog("laf:captcha:provided", ok=True, payload={"method": "mock_mode"}, tags={"base_url": self.base_url})
                else:
                    # Step 1: 環境變數覆寫（最高優先權）
                    captcha_text = (self._captcha_override or '').strip()
                    if captcha_text:
                        captcha_source = 'env_override'
                        _eventlog("laf:captcha:provided", ok=True, payload={"method": "env_override"}, tags={"base_url": self.base_url})

                    # Step 2: OCR 自動辨識（ddddocr / RapidOCR）
                    if not captcha_text and self._allow_captcha_ocr:
                        self.log("  🤖 以 OCR 自動辨識驗證碼...")
                        _eventlog("laf:captcha:ocr:start", ok=None, payload={"attempt": retry_count + 1}, tags={"base_url": self.base_url})
                        try:
                            captcha_text = self.captcha_solver.solve_with_retry(
                                get_image_func=lambda: (self._refresh_captcha() or self._get_captcha_image()),
                                max_retry=captcha_ocr_retry,
                            )
                        except Exception as ocr_err:
                            self.log(f"  ⚠️ OCR 辨識異常: {ocr_err}")
                            captcha_text = ""
                        if captcha_text:
                            # Do not log the captcha itself.
                            captcha_source = 'ocr'
                            _eventlog("laf:captcha:ocr:done", ok=True, payload={"result": "ok", "chars": len(captcha_text)}, tags={"base_url": self.base_url})
                        elif self._allow_captcha_ocr:
                            _eventlog("laf:captcha:ocr:done", ok=False, payload={"result": "empty", "headless": bool(self.headless)}, tags={"base_url": self.base_url})

                    if not captcha_text:
                        self.log("  ❌ OCR 未取得四碼，換新驗證碼自動重試。")
                        _eventlog("laf:captcha:ocr:done", ok=False, payload={"result": "empty_auto_retry"}, tags={"base_url": self.base_url})
                        retry_count += 1
                        continue

                if captcha_text:
                    self.log('  🔢 已取得驗證碼（不顯示於日誌）')
                    captcha_input.clear()
                    captcha_input.send_keys(captcha_text)

                try:
                    # Chrome 147+: 直接呼叫 checkForm() 比 click loginLink 更穩定
                    # （法扶 portal 的登入按鈕是 <a onclick="checkForm();">，
                    #   Chrome 147 headless 下 click 可能不正確觸發 onclick）
                    # 守則：絕不在 execute_script/click 前設 _next_dialog_no_dismiss=True。
                    # Playwright sync 在 dialog 未 dismiss 時會無限卡住。
                    # 改為重置 _last_dialog，讓 _on_dialog 自動 dismiss 後讀 _last_dialog.message。
                    try:
                        self.driver._last_dialog = None
                    except Exception:
                        pass
                    self.driver.execute_script("checkForm();")
                except Exception:
                    try:
                        login_btn = self.driver.find_element(By.CSS_SELECTOR, '#loginLink, a#loginLink')
                        try:
                            self.driver._last_dialog = None
                        except Exception:
                            pass
                        login_btn.click()
                    except Exception:
                        try:
                            # Playwright fallback: click loginLink via native Playwright
                            if isinstance(self.driver, PlaywrightDriverWrapper):
                                try:
                                    self.driver._last_dialog = None
                                except Exception:
                                    pass
                                self.driver._page.click('#loginLink, a#loginLink')
                            else:
                                try:
                                    self.driver._last_dialog = None
                                except Exception:
                                    pass
                                password_input.send_keys('\n')
                        except Exception:
                            try:
                                self.driver._last_dialog = None
                            except Exception:
                                pass
                            password_input.send_keys('\n')

                # 等待 URL 離開 processLogin（最多 12 秒）
                _login_ok = False
                for _w in range(12):
                    time.sleep(1)
                    try:
                        _cur = (self.driver.current_url or "")
                        if "processLogin" not in _cur and "toLogin" not in _cur:
                            _login_ok = True
                            break
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1814, exc_info=True)

                if not _login_ok:
                    time.sleep(2)  # 再等一下

                # 驗證登入成功 - 檢查多種可能的成功 URL/頁面特徵
                current_url = self.driver.current_url
                self.log(f"  🔗 當前 URL: {current_url}")

                # 0-1) 登入成功後可能先進「90 天密碼變更提醒」中間頁
                if self._handle_password_reminder_page():
                    self.log("✅ LAF 登入成功！（已略過密碼提醒）")
                    self._dismiss_post_login_popups()
                    return True

                # 1) URL 判斷
                if 'toMainPage' in current_url:
                    self.log('✅ LAF 登入成功！')
                    self._dismiss_post_login_popups()
                    return True

                # 2) 內容判斷（有些情況 URL 仍停在 processLogin，但頁面已是主頁）
                try:
                    src = (self.driver.page_source or "")
                except Exception:
                    src = ""

                main_markers = ["自動登出", "案件狀態區", "待處理案件", "追蹤案件", "最新公告"]
                if any(m in src for m in main_markers):
                    self.log("✅ LAF 登入成功！（以頁面內容判斷）")
                    self._dismiss_post_login_popups()
                    return True

                # 2-0) frameset 結構偵測（登入成功後 LAF 直接給 frameset 主框，content 在 sub-frame）
                # 必須在 #loginLink 檢查之前，避免被誤判為登入失敗
                if ("contentFrame" in src and "footerFrame" in src) or ("toPublishmentList" in src):
                    self.log("✅ LAF 登入成功！（以 frameset 內容判斷）")
                    self._dismiss_post_login_popups()
                    return True

                # 2-1) frameset 判斷：有些登入成功後會導到 frameset（contentFrame/footerFrame）
                try:
                    if self.driver.find_elements(By.CSS_SELECTOR, "frame[name='contentFrame'], frame[name='footerFrame']"):
                        self.log("✅ LAF 登入成功！（以 frameset 判斷）")
                        self._dismiss_post_login_popups()
                        return True
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1848, exc_info=True)

                # 2-2) 進 contentFrame 再判斷一次（主內容可能在 frame 裡）
                try:
                    self.driver.switch_to.default_content()
                    # Wait briefly for the frame + content to load after login submit.
                    WebDriverWait(self.driver, 15).until(
                        EC.frame_to_be_available_and_switch_to_it((By.NAME, "contentFrame"))
                    )
                    src2 = (self.driver.page_source or "")
                    if any(m in src2 for m in main_markers):
                        self.log("✅ LAF 登入成功！（以 contentFrame 內容判斷）")
                        self._dismiss_post_login_popups()
                        return True
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1863, exc_info=True)
                finally:
                    try:
                        self.driver.switch_to.default_content()
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1868, exc_info=True)

                # 3) 仍在登入頁面（通常是驗證碼錯誤或被要求重新登入）
                try:
                    if self.driver.find_elements(By.CSS_SELECTOR, "#loginLink"):
                        self.log(f"❌ 登入失敗，可能是驗證碼錯誤 (第 {retry_count + 1} 次)")
                        if captcha_source == 'ocr':
                            _mark_ocr_rejected()
                        self.close()  # 清理 driver 避免洩漏
                        retry_count += 1
                        continue
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1877, exc_info=True)

                if 'lafcsp' in current_url and 'toMainPage' not in current_url:
                    self.log(f"❌ 登入失敗，可能是驗證碼錯誤 (第 {retry_count + 1} 次)")
                    if captcha_source == 'ocr':
                        _mark_ocr_rejected()
                    self.close()  # 清理 driver 避免洩漏
                    retry_count += 1
                    continue

                self.log('❌ 登入失敗：帳號或密碼錯誤')
                return False

            except Exception as e:
                self.log(f"❌ 登入過程錯誤: {e}")
                traceback.print_exc()
                self.close()
                retry_count += 1

        return False

    def download_case_files(self, case_number: str, row_element=None) -> List[str]:
        """
        下載特定案件的所有文件
        
        Args:
            case_number: 法扶案號 (如 1141121-E-001)
            row_element: 可選，直接傳入表格行元素以避免重新搜尋
            
        Returns:
            已下載的檔案路徑列表
        """
        downloaded = []
        
        if not self.driver:
            self.log("❌ 瀏覽器未初始化")
            return downloaded
        
        try:
            self.log(f"📥 正在下載案件 {case_number} 的文件...")
            _eventlog(
                "laf:portal:download:start",
                ok=None,
                payload={"download_folder": str(self.download_folder), "headless": bool(self.headless)},
                tags={"laf_case_no": case_number, "base_url": self.base_url},
            )
            
            # 進入下載區
            if self.DOWNLOAD_URL not in self.driver.current_url:
                self.driver.get(self.DOWNLOAD_URL)
                wait = WebDriverWait(self.driver, 20)
                wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table")))
                time.sleep(2)
            
            # 記錄下載前的檔案
            files_before = set(os.listdir(self.download_folder))
            
            # 如果沒有傳入 row_element，需要重新找
            if row_element is None:
                rows = self.driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
                for row in rows:
                    if case_number in row.text:
                        row_element = row
                        break
            
            if row_element is None:
                self.log(f"  ⚠️ 找不到案號 {case_number} 的行")
                return downloaded
            
            # 找下載按鈕（圖示）
            # 嘗試多種可能的 selector
            download_btn = None
            selectors_to_try = [
                # SVG 圖示
                "svg[class*='download']",
                "svg[data-icon*='download']",
                # Font Awesome 圖示
                "i[class*='download']",
                "i[class*='fa-download']",
                # 一般按鈕/連結
                "a[title*='下載']",
                "button[title*='下載']",
                "a[class*='download']",
                "button[class*='download']",
                # Material icons
                "span[class*='download']",
                # 最後一欄的第一個可點擊元素（通常是下載按鈕）
                "td:last-child a",
                "td:last-child button",
                "td:last-child svg",
                "td:nth-last-child(2) a",  # 倒數第二欄
                "td:nth-last-child(2) svg",
            ]
            
            for selector in selectors_to_try:
                try:
                    elements = row_element.find_elements(By.CSS_SELECTOR, selector)
                    if elements:
                        download_btn = elements[0]
                        self.log(f"  🔍 找到下載按鈕 (selector: {selector})")
                        break
                except Exception:
                    continue
            
            # 如果還是找不到，嘗試找所有可點擊的元素
            if download_btn is None:
                self.log("  🔍 嘗試找所有可點擊元素...")
                clickable = row_element.find_elements(By.CSS_SELECTOR, "a, button, svg, i[class*='fa']")
                if clickable:
                    # 找最後面的可點擊元素（通常是下載按鈕）
                    for elem in reversed(clickable):
                        # 跳過明顯不是下載的元素
                        elem_class = elem.get_attribute('class') or ''
                        elem_text = elem.text.strip()
                        if 'refresh' in elem_class.lower() or 'reload' in elem_class.lower():
                            continue
                        download_btn = elem
                        self.log(f"  🔍 使用可點擊元素: tag={elem.tag_name}, class={elem_class}")
                        break
            
            if download_btn is None:
                self.log(f"  ⚠️ 找不到下載按鈕")
                # 保存截圖供除錯
                if self._debug_capture_enabled():
                    from api.debug_capture import save_debug_screenshot
                    save_debug_screenshot(self.driver, f"debug_no_download_btn_{case_number}", context="法扶找不到下載按鈕")
                return downloaded
            
            # ── Strategy 0: Extract doDownload() params → direct POST to /lafcsp/mailtoDownload ──
            # doDownload(this, attachFileName, lawyerid, mailSeq, fileSeq) submits a form
            # to /lafcsp/mailtoDownload and the server responds with the ZIP binary.
            is_playwright = hasattr(self.driver, '_context') and hasattr(self.driver, '_all_pages')
            direct_download_url = None
            _zip_downloaded = False

            if is_playwright:
                try:
                    import re as _re
                    # Find the <a> with doDownload in its onclick (search whole row HTML)
                    row_raw = download_btn._el if hasattr(download_btn, '_el') else download_btn
                    js_onclick = """
                    let el = arguments[0];
                    // Walk up to find the row, then search for doDownload <a>
                    let root = el;
                    for (let i = 0; i < 8; i++) {
                        if (!root || root.tagName === 'TR') break;
                        root = root.parentElement;
                    }
                    if (root) {
                        let anchors = root.querySelectorAll('a[onclick*=\"doDownload\"]');
                        if (anchors.length > 0) return anchors[0].getAttribute('onclick');
                    }
                    // fallback: check the element itself
                    let oc = el.getAttribute('onclick') || '';
                    if (oc.includes('doDownload')) return oc;
                    return null;
                    """
                    onclick_text = self.driver.execute_script(js_onclick, row_raw)
                    if onclick_text and 'doDownload' in onclick_text:
                        # Parse: doDownload(this,'ZIP_PATH','LAWYER_ID','MAIL_SEQ','FILE_SEQ')
                        m = _re.search(
                            r"doDownload\s*\(\s*this\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]",
                            onclick_text,
                        )
                        if m:
                            _attach_fname = m.group(1)
                            _lawyerid     = m.group(2)
                            _mail_seq     = m.group(3)
                            _file_seq     = m.group(4)
                            self.log(f"  📋 解析到 doDownload 參數: attachFileName={_attach_fname}, mailSeq={_mail_seq}, fileSeq={_file_seq}")

                            # POST to /lafcsp/mailtoDownload with form data (session cookies carried automatically)
                            _post_url = f"{self.base_url}/lafcsp/mailtoDownload"
                            _form_data = {
                                "attachFileName": _attach_fname,
                                "lawyerid":       _lawyerid,
                                "mailSeq":        _mail_seq,
                                "fileSeq":        _file_seq,
                                "downloadType":   "fromList",
                            }
                            self.log(f"  📤 直接 POST → {_post_url}")
                            try:
                                _resp = self.driver._context.request.post(
                                    _post_url,
                                    form=_form_data,
                                    timeout=60000,
                                )
                                _body = _resp.body()
                                self.log(f"  📥 回應 status={_resp.status}, body={len(_body)} bytes")
                                if _resp.status == 200 and len(_body) > 500:
                                    # ZIP magic bytes: PK (0x50 0x4B)
                                    if _body[:2] == b'PK':
                                        # Use the filename from the zip path (last component)
                                        _zip_fname = os.path.basename(_attach_fname) or f"{case_number}.zip"
                                        _save_path = self.download_folder / _zip_fname
                                        _save_path.write_bytes(_body)
                                        self.log(f"  ✅ Strategy 0 成功: 已下載 {_zip_fname} ({len(_body):,} bytes)")
                                        downloaded.append(str(_save_path))
                                        _zip_downloaded = True
                                    else:
                                        _preview = _body[:200].decode('utf-8', errors='replace')
                                        self.log(f"  ⚠️ Strategy 0: 回應不是 ZIP，前200字元: {_preview}")
                                else:
                                    self.log(f"  ⚠️ Strategy 0: 回應異常 status={_resp.status}, size={len(_body)}")
                            except Exception as _pe:
                                self.log(f"  ⚠️ Strategy 0 POST 失敗: {_pe}")
                        else:
                            self.log(f"  ⚠️ 無法解析 doDownload 參數: {onclick_text[:120]}")
                    else:
                        self.log(f"  ⚠️ 找不到 doDownload onclick 屬性")
                except Exception as _s0e:
                    self.log(f"  ⚠️ Strategy 0 異常: {_s0e}")

            # ── Strategy 1: Click button (fallback if Strategy 0 failed) ──
            original_handles = set(self.driver.window_handles) if is_playwright else set()

            if not _zip_downloaded:
                try:
                    self.log(f"  🖱️ Strategy 1: 點擊下載按鈕...")
                    try:
                        download_btn.click()
                    except Exception:
                        self.driver.execute_script(
                            "arguments[0].click();",
                            download_btn._el if hasattr(download_btn, '_el') else download_btn,
                        )
                    time.sleep(3)
                except Exception as e:
                    self.log(f"  ⚠️ 點擊下載按鈕失敗: {e}")

            # ── Strategy 2: Detect popup / new-page opened by the click (Playwright) ──
            popup_url = None
            if is_playwright and not _zip_downloaded:
                import time as _t2
                deadline = _t2.monotonic() + 8
                while _t2.monotonic() < deadline:
                    try:
                        new_handles = set(self.driver.window_handles) - original_handles
                        if new_handles:
                            for _ph in new_handles:
                                for _pg in self.driver._all_pages():
                                    if str(id(_pg)) == _ph:
                                        popup_url = _pg.url
                                        self.log(f"  🔗 Strategy 2: 偵測到彈窗頁面: {popup_url}")
                                        try:
                                            _pg.close()
                                        except Exception:
                                            pass
                                        break
                            if popup_url:
                                break
                    except Exception:
                        pass
                    _t2.sleep(0.5)

            # ── Strategy 3: HTTP download via Playwright session (no re-auth needed) ──
            def _http_dl(url):
                """Download file via Playwright request context (preserves session cookies)."""
                if not url or not url.startswith('http'):
                    return None
                try:
                    resp = self.driver._context.request.get(url, timeout=30000)
                    if resp.status == 200:
                        body = resp.body()
                        if len(body) > 500:
                            from urllib.parse import urlparse, unquote as _uq
                            _parsed = urlparse(url)
                            fname = os.path.basename(_parsed.path) or f"{case_number}_doc.pdf"
                            if '.' not in fname:
                                fname = f"{case_number}_doc.pdf"
                            fname = _uq(fname)
                            save_path = self.download_folder / fname
                            save_path.write_bytes(body)
                            self.log(f"  ✓ HTTP 直接下載: {fname} ({len(body):,} bytes)")
                            return str(save_path)
                        else:
                            self.log(f"  ⚠️ HTTP 回應體太小 ({len(body)} bytes)，可能是錯誤頁面")
                    else:
                        self.log(f"  ⚠️ HTTP 下載失敗: status={resp.status}")
                except Exception as _he:
                    self.log(f"  ⚠️ HTTP 下載異常: {_he}")
                return None

            if is_playwright and popup_url and not _zip_downloaded:
                _f = _http_dl(popup_url)
                if _f:
                    downloaded.append(_f)

            if not downloaded and is_playwright and direct_download_url:
                _f = _http_dl(direct_download_url)
                if _f:
                    downloaded.append(_f)

            # ── Strategy 4: Poll laf_downloads/ (standard Playwright download event) ──
            if not downloaded:
                max_wait = 30
                for _ in range(max_wait):
                    time.sleep(1)
                    files_after = set(os.listdir(self.download_folder))
                    new_files = files_after - files_before
                    downloading = any(f.endswith(('.crdownload', '.tmp', '.part')) for f in new_files)
                    if new_files and not downloading:
                        break

            # 收集下載的檔案
            files_after = set(os.listdir(self.download_folder))
            new_files = files_after - files_before

            for f in new_files:
                if not f.endswith(('.crdownload', '.tmp', '.part')):
                    full_path = str(self.download_folder / f)
                    if full_path not in downloaded:
                        downloaded.append(full_path)
                        self.log(f"  ✓ 已下載: {f}")
            
            if downloaded:
                self.log(f"✅ 案件 {case_number} 下載完成，共 {len(downloaded)} 個檔案")
                _eventlog(
                    "laf:portal:download:done",
                    ok=True,
                    payload={"count": len(downloaded), "files": [os.path.basename(p) for p in downloaded[:5]]},
                    tags={"laf_case_no": case_number, "base_url": self.base_url},
                )
            else:
                self.log(f"⚠️ 案件 {case_number} 沒有下載到檔案")
                _eventlog(
                    "laf:portal:download:done",
                    ok=True,
                    payload={"count": 0, "files": []},
                    tags={"laf_case_no": case_number, "base_url": self.base_url},
                )
                
        except Exception as e:
            self.log(f"❌ 下載過程錯誤: {e}")
            traceback.print_exc()
            _eventlog(
                "laf:portal:download:done",
                ok=False,
                payload={"error": str(e)[:220]},
                tags={"laf_case_no": case_number, "base_url": self.base_url},
            )
        
        return downloaded
    
    def get_downloadable_cases(self) -> List[Dict[str, Any]]:
        """
        取得下載頁面上所有可下載的案件資訊
        
        從 Mail標題 解析：
        【法扶花蓮分會派案通知】林文忠-1141121-E-005-刑事偵查中辯護-傷害
        
        Returns:
            List[Dict]: [{
                'case_number': '1141121-E-005',
                'client_name': '林文忠',
                'branch': '花蓮',
                'case_type': '刑事',
                'case_stage': '偵查',
                'case_reason': '傷害',
                'file_list': ['扶助律師接案通知書_...', '委任狀_...', ...],
                'row_element': <WebElement>,  # 用於後續點擊下載
            }]
        """
        cases = []
        
        if not self.driver:
            self.log("❌ 瀏覽器未初始化")
            return cases
            
        try:
            self.log("🔍 正在掃描可下載案件...")
            
            # 進入下載區
            if self.DOWNLOAD_URL not in self.driver.current_url:
                self.driver.get(self.DOWNLOAD_URL)
            
            # 嘗試尋找表格 (含重試機制)
            max_retries = 2
            for attempt in range(max_retries):
                try:
                    wait = WebDriverWait(self.driver, 20)
                    wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "table")))
                    break
                except Exception:
                    if attempt < max_retries - 1:
                        self.log(f"⚠️ 找不到表格，嘗試重新整理 ({attempt + 1}/{max_retries})...")
                        self.driver.refresh()
                        time.sleep(5)
                    else:
                        self.log("❌ 找不到案件列表表格 (Timeout)")
                        return cases
            
            time.sleep(2)
            
            # 找到表格主體的所有行
            rows = self.driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            
            for row in rows:
                try:
                    # 取得所有欄位
                    cells = row.find_elements(By.TAG_NAME, "td")
                    if len(cells) < 4:
                        continue
                    
                    # 欄位結構：案號 | Mail標題 | 表單名稱 | 通知下載日期 | 最後下載日期 | 下載按鈕
                    case_number_cell = cells[0].text.strip()
                    mail_title_cell = cells[1].text.strip()
                    file_list_cell = cells[2].text.strip()
                    
                    # 解析案號
                    case_match = re.search(r'(\d{7}-[A-Z]-\d{3})', case_number_cell)
                    if not case_match:
                        continue
                    case_number = case_match.group(1)
                    
                    # 解析 Mail標題：【法扶XX分會派案通知】當事人-案號-案件類型-案由
                    client_name = ""
                    branch = ""
                    case_type = ""
                    case_stage = ""
                    case_reason = ""
                    
                    # 解析分會
                    branch_match = re.search(r'【法扶(.+?)分會', mail_title_cell)
                    if branch_match:
                        branch = branch_match.group(1)
                    
                    # 解析當事人和案件資訊
                    # 格式：】當事人-案號-案件類型-案由
                    info_match = re.search(r'】\s*(.+?)-\d{7}-[A-Z]-\d{3}-(.+?)-(.+?)$', mail_title_cell)
                    if info_match:
                        client_name = info_match.group(1).strip()
                        # 處理原名情況：林鈴洵(原名:林孟潔)
                        name_match = re.match(r'(.+?)\(原名[:：](.+?)\)', client_name)
                        if name_match:
                            client_name = name_match.group(1).strip()
                        
                        laf_case_type = info_match.group(2).strip()
                        case_reason = info_match.group(3).strip()
                        
                        # 對應 OSC 案件類型
                        parsed = LAFCaseTypeParser.parse_subject(mail_title_cell)
                        if parsed:
                            case_type = parsed.case_type
                            case_stage = parsed.case_stage
                            # ★ 修改：使用 Parser 清理過的案由 (這樣才會移除「案」字)
                            case_reason = parsed.case_reason
                        
                        # Fallback: 如果 parse_subject 失敗或沒抓到，嘗試用案由判斷消債
                        if not case_type or case_type == '民事':
                            if '消費者債務清理' in case_reason or '更生' in case_reason or '清算' in case_reason:
                                case_type = '消費者債務清理'
                                case_stage = '其他'
                            elif any(k in case_reason for k in ['強盜', '殺人', '毒品', '槍砲', '竊盜', '傷害', '詐欺', '侵占', '背信', '貪污', '賄賂', '妨害性自主', '公共危險', '過失致死', '非常上訴']):
                                case_type = '刑事'
                                if '再審' in case_reason or '非常上訴' in case_reason:
                                    case_stage = '其他'
                                else:
                                    case_stage = '偵查'
                    
                    # 如果上面沒解析到，嘗試簡單解析
                    if not client_name:
                        # 嘗試從 Mail標題 找中文姓名（2-4個字）
                        name_patterns = re.findall(r'[\u4e00-\u9fff]{2,4}', mail_title_cell)
                        for name in name_patterns:
                            if name not in ['法扶', '分會', '派案', '通知', '審核', '結果', '刑事', '民事', '偵查', '一審', '二審']:
                                client_name = name
                                break
                    
                    # 解析檔案列表
                    file_list = [f.strip() for f in file_list_cell.split('\n') if f.strip()]
                    # 移除序號 "1. ", "2. " 等
                    file_list = [re.sub(r'^\d+\.\s*', '', f) for f in file_list]
                    
                    cases.append({
                        'case_number': case_number,
                        'client_name': client_name,
                        'branch': branch,
                        'case_type': case_type or '民事',
                        'case_stage': case_stage or '一審',
                        'case_reason': case_reason,
                        'file_list': file_list,
                        'row_element': row,
                        'raw_text': row.text
                    })
                    
                except Exception as e:
                    self.log(f"  ⚠️ 解析行失敗: {e}")
                    continue
            
            self.log(f"📊 掃描完成，共發現 {len(cases)} 個可下載案件")
            
            # 顯示解析結果
            for c in cases:
                self.log(f"  📋 {c['case_number']} - {c['client_name']} - {c['case_type']}({c['case_stage']}) - {c['case_reason']}")
            
        except Exception as e:
            self.log(f"❌ 掃描可下載案件失敗: {e}")
            traceback.print_exc()
            
        return cases

    # ==============================================================================
    # 報結（結案回報）自動化：暫存/送出
    # ==============================================================================

    def _switch_to_content_frame_if_any(self):
        """若主頁已有操作控件就維持 default；否則嘗試切到 contentFrame。"""
        if not self.driver:
            return
        try:
            self.driver.switch_to.default_content()
        except Exception:
            return
        try:
            has_controls = bool(
                self.driver.execute_script(
                    "return !!document.querySelector('#applynm, #applyno, #queryBtn, .rim.has-lawyer, .rim.has-table');"
                )
            )
            if has_controls:
                return
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2246, exc_info=True)
        try:
            frames = self.driver.find_elements(By.CSS_SELECTOR, "frame[name='contentFrame'], iframe[name='contentFrame']")
            for fr in frames:
                try:
                    self.driver.switch_to.default_content()
                    self.driver.switch_to.frame(fr)
                    ok = bool(
                        self.driver.execute_script(
                            "return !!document.querySelector('#applynm, #applyno, #queryBtn, .rim.has-lawyer, .rim.has-table');"
                        )
                    )
                    if ok:
                        return
                except Exception:
                    continue
            self.driver.switch_to.default_content()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2264, exc_info=True)

    def _is_login_or_timeout_page(self) -> bool:
        if not self.driver:
            return True
        try:
            cur = (self.driver.current_url or "").lower()
        except Exception:
            cur = ""
        if any(k in cur for k in ("tologin", "timeout=y", "/logout")):
            return True
        try:
            src = (self.driver.page_source or "").lower()
        except Exception:
            src = ""
        if ("請輸入帳號" in src) or ("驗證碼" in src and "登入" in src):
            return True
        return False

    def _wait_query_done(self, timeout_sec: float = 15.0):
        if not self.driver:
            return
        start = time.time()
        js = """
return (() => {
  const q = document.querySelector('#dialog-querying');
  const visible = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
    if (el.getAttribute('aria-hidden') === 'true') return false;
    if (el.classList && el.classList.contains('show')) return true;
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const querying = visible(q);
  const jqIdle = (typeof window.jQuery === 'undefined') ? true : (window.jQuery.active === 0);
  return {querying, jqIdle};
})();
"""
        while time.time() - start < float(timeout_sec):
            try:
                r = self.driver.execute_script(js) or {}
                if (not bool(r.get("querying"))) and bool(r.get("jqIdle")):
                    return
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2310, exc_info=True)
            time.sleep(0.35)

    def _find_clickable_by_onclick(self, contains_text: str):
        """找出 onclick/href 內含指定片段的可點擊元素（button/a/span 等），回傳 WebElement 或 None。"""
        if not self.driver:
            return None
        try:
            xpath = (
                f"//*[( @onclick and contains(@onclick, {json.dumps(contains_text)}) ) "
                f"or ( @href and contains(@href, {json.dumps(contains_text)}) )]"
            )
            els = self.driver.find_elements(By.XPATH, xpath)
            return els[0] if els else None
        except Exception:
            return None

    def _debug_log_clickables(self, limit: int = 12):
        if not self.driver:
            return
        try:
            rows = self.driver.execute_script(
                """
                const out = [];
                const els = document.querySelectorAll('[onclick], a[href^="javascript:"], a[href*="toReport"], button');
                for (const el of els) {
                  const onclick = (el.getAttribute('onclick') || '').trim();
                  const href = (el.getAttribute('href') || '').trim();
                  const txt = (el.textContent || '').trim().replace(/\\s+/g, ' ');
                  if (!onclick && !href) continue;
                  out.push({onclick, href, txt: txt.slice(0, 40)});
                  if (out.length >= arguments[0]) break;
                }
                return out;
                """,
                int(limit),
            )
            if isinstance(rows, list):
                self.log("  🔎 可點擊元素樣本（onclick/href）：")
                for r in rows[: int(limit)]:
                    if not isinstance(r, dict):
                        continue
                    self.log(f"    - txt={r.get('txt','')} | onclick={r.get('onclick','')} | href={r.get('href','')}")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2354, exc_info=True)

    def _set_input_value(self, el, value: str):
        if el is None:
            return
        v = str(value)
        try:
            self.driver.execute_script(
                """
                const el = arguments[0];
                const v = arguments[1];
                try { el.focus(); } catch(e) {}
                try { el.value = ''; } catch(e) {}
                try { el.value = v; } catch(e) {}
                try { el.dispatchEvent(new Event('input', { bubbles: true })); } catch(e) {}
                try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch(e) {}
                try { el.blur(); } catch(e) {}
                """,
                el,
                v,
            )
            got = (el.get_attribute("value") or "")
            if got == v:
                return
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2379, exc_info=True)
        try:
            el.clear()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2383, exc_info=True)
        try:
            el.send_keys(v)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2387, exc_info=True)

    def _set_select_value(self, el, value: str) -> bool:
        if el is None:
            return False
        v = str(value or "").strip()
        if not v:
            return False
        try:
            self.driver.execute_script(
                """
                const el = arguments[0];
                const val = arguments[1];
                let ok = false;
                for (const opt of (el.options || [])) {
                  const ov = (opt.value || '').trim();
                  const ot = (opt.text || '').trim();
                  if (ov === val || ot === val) {
                    el.value = ov || val;
                    ok = true;
                    break;
                  }
                }
                if (!ok) el.value = val;
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('input', { bubbles: true }));
                return true;
                """,
                el,
                v,
            )
            return True
        except Exception:
            return False

    def _set_radio_value(self, name: str, value: str) -> bool:
        if not self.driver:
            return False
        n = (name or "").strip()
        v = (value or "").strip()
        if not n or not v:
            return False
        try:
            xp = f"//input[@type='radio' and @name={json.dumps(n)} and (@value={json.dumps(v)} or @id={json.dumps(v)})]"
            els = self.driver.find_elements(By.XPATH, xp)
            if not els:
                return False
            el = els[0]
            try:
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2438, exc_info=True)
            try:
                el.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", el)
            return True
        except Exception:
            return False

    def _set_field_by_selectors(self, selectors: List[str], value: Any) -> bool:
        if not self.driver:
            return False
        if value is None:
            return False
        v = str(value).strip()
        if not v:
            return False

        # Strategy 0: single JS call covering all selectors (bypasses Playwright timing)
        try:
            sel_json = json.dumps([s for s in (selectors or []) if s])
            v_json = json.dumps(v)
            hit = self.driver.execute_script(
                f"""
                const sels = {sel_json};
                const val = {v_json};
                for (const sel of sels) {{
                    try {{
                        const el = document.querySelector(sel);
                        if (!el || el.disabled || el.type === 'hidden') continue;
                        el.focus();
                        el.value = val;
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                        if (el.value) return sel;
                    }} catch(e) {{}}
                }}
                return null;
                """
            )
            if hit:
                return True
        except Exception:
            pass

        for sel in (selectors or []):
            if not sel:
                continue
            # Strategy 1: trust fill_by_selector without JS readback verification
            try:
                if hasattr(self.driver, "fill_by_selector"):
                    if self.driver.fill_by_selector(sel, v):
                        return True
            except Exception:
                pass
            # Strategy 2: ElementHandle-based (accept any non-empty value)
            try:
                els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                if not els:
                    continue
                candidates = []
                for e in els:
                    try:
                        if not e.is_enabled():
                            continue
                        candidates.append(e)
                    except Exception:
                        continue
                if not candidates:
                    continue
                visible_first = [e for e in candidates if e.is_displayed()]
                ordered = visible_first + [e for e in candidates if e not in visible_first]
                for el in ordered:
                    tag = (el.tag_name or "").lower()
                    if tag == "select":
                        if self._set_select_value(el, v):
                            return True
                    elif tag in ("textarea", "input"):
                        self._set_input_value(el, v)
                        got = (el.get_attribute("value") or "").strip()
                        if got:
                            return True
            except Exception:
                continue
        return False

    def _set_field_by_label(self, label_keywords: List[str], value: str, kind: str = "input") -> bool:
        """
        以「標籤文字」找附近的 input/textarea/select，避免欄位 name 變動造成失效。
        kind: input|textarea
        """
        if not self.driver:
            return False
        kws = [k for k in (label_keywords or []) if k]
        if not kws:
            return False
        tag = "textarea" if kind == "textarea" else "input"
        # 多策略 XPath：同列優先，其次 fallback 到 following。
        xpaths = []
        for k in kws:
            kq = json.dumps(k)
            xpaths.append(f"//tr[.//*[contains(normalize-space(.), {kq})]]//{tag}[not(@type='hidden')][1]")
            xpaths.append(f"//*[contains(normalize-space(.), {kq})]/ancestor::*[self::td or self::th][1]//{tag}[not(@type='hidden')][1]")
            xpaths.append(f"//*[contains(normalize-space(.), {kq})]/following::{tag}[not(@type='hidden')][1]")
        for xp in xpaths:
            try:
                els = self.driver.find_elements(By.XPATH, xp)
                if not els:
                    continue
                self._set_input_value(els[0], value)
                return True
            except Exception:
                continue
        return False

    def _set_field_by_name(self, name: str, value: str, kind: str = "input") -> bool:
        """
        Set a field by its 'name' attribute.
        kind: 'input', 'select', 'textarea'
        """
        if not self.driver:
            return False
        try:
            elms = self.driver.find_elements("name", name)
            if not elms:
                return False
            elm = elms[0]
            if not elm.is_displayed() and kind != "select":
                # Try simple JS set if hidden (except select, which needs interaction often)
                self.driver.execute_script(f"arguments[0].value = '{value}';", elm)
                return True
            
            if kind == "select":
                from selenium.webdriver.support.ui import Select
                try:
                    Select(elm).select_by_visible_text(value)
                except Exception:
                    # Fallback to value
                    Select(elm).select_by_value(value)
            else:
                elm.clear()
                elm.send_keys(value)
            return True
        except Exception:
            return False

    def _get_field_value_by_name(self, name: str) -> str:
        """
        Get value of a field by name.
        """
        if not self.driver:
            return ""
        try:
            elms = self.driver.find_elements("name", name)
            if not elms:
                return ""
            return elms[0].get_attribute("value")
        except Exception:
            return ""

    def _click_button_by_text(self, candidates: List[str]) -> bool:
        if not self.driver:
            return False
        for t in (candidates or []):
            if not t:
                continue
            tq = json.dumps(t)
            xps = [
                f"//button[contains(normalize-space(.), {tq})]",
                f"//a[contains(normalize-space(.), {tq})]",
                f"//input[@value and contains(@value, {tq})]",
            ]
            for xp in xps:
                try:
                    els = self.driver.find_elements(By.XPATH, xp)
                    if not els:
                        continue
                    el = els[0]
                    try:
                        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2584, exc_info=True)
                    try:
                        el.click()
                    except Exception:
                        self.driver.execute_script("arguments[0].click();", el)
                    return True
                except Exception:
                    continue
        return False

    def _upload_supporting_files(self, files: List[str], workflow: str = "") -> Dict[str, Any]:
        """
        Upload PDF files on the current workflow page.
        Best-effort: tries upload modal/button patterns used by LAF pages.
        """
        result = {"ok": True, "workflow": workflow, "requested": 0, "uploaded": [], "failed": []}
        if not self.driver:
            result["ok"] = False
            result["error"] = "driver_not_ready"
            return result

        pdfs: List[str] = []
        for p in (files or []):
            s = (str(p or "")).strip()
            if not s:
                continue
            if (not os.path.isfile(s)) or (not s.lower().endswith(".pdf")):
                result["failed"].append({"path": s, "error": "not_pdf_or_missing"})
                continue
            pdfs.append(s)

        max_files = int(os.environ.get("MAGI_LAF_MAX_UPLOAD_FILES", "250") or "250")
        if len(pdfs) > max_files:
            pdfs = pdfs[:max_files]
        result["requested"] = len(pdfs)
        if not pdfs:
            self.last_upload_result = dict(result)
            return result

        def _upload_panel_is_open() -> bool:
            """Check if upload modal (#dialog-form) is actually VISIBLE (not just in DOM)."""
            try:
                result = self.driver.execute_script(
                    """
                    var modal = document.querySelector('#dialog-form');
                    if (modal) {
                        var style = window.getComputedStyle(modal);
                        if (style.display !== 'none' && modal.offsetHeight > 0) return true;
                        if (modal.classList.contains('in') || modal.classList.contains('show')) return true;
                        if (modal.style.display && modal.style.display !== 'none') return true;
                    }
                    var inputs = Array.prototype.slice.call(
                        document.querySelectorAll("input[type='file'][name='uploadDoc']"));
                    return inputs.some(function(el) { return el.offsetParent !== null; });
                    """
                )
                if bool(result):
                    return True
            except Exception:
                pass
            try:
                pw_page = getattr(self.driver, "_page", None)
                if pw_page is not None:
                    for fr in pw_page.frames:
                        try:
                            r = fr.evaluate(
                                """() => {
                                    var modal = document.querySelector('#dialog-form');
                                    if (modal) {
                                        var s = window.getComputedStyle(modal);
                                        if (s.display !== 'none' && modal.offsetHeight > 0) return true;
                                    }
                                    var inputs = Array.prototype.slice.call(
                                        document.querySelectorAll("input[type='file'][name='uploadDoc']"));
                                    return inputs.some(function(el) { return el.offsetParent !== null; });
                                }"""
                            )
                            if r:
                                return True
                        except Exception:
                            continue
            except Exception:
                pass
            return False

        def _open_upload_panel() -> bool:
            # Fast path: panel already open.
            if _upload_panel_is_open():
                return True
            wf = (workflow or "").strip().lower()
            token = {
                "condition": "CND", "fee": "LGFEE", "inquiry": "RSM",
                "withdrawal": "PB_doc", "closing": "CR_CS", "go_live": "NOT_OPEN",
            }.get(wf, "")
            # 1) Direct JS linkUpload('TOKEN') — portal canonical way
            js_candidates = [
                f"if (typeof linkUpload === 'function') {{ try {{ linkUpload('{token}'); return true; }} catch(e) {{}} }} return false;" if token else "",
                "if (typeof linkUpload === 'function') { try { linkUpload(); return true; } catch(e) {} } return false;",
            ]
            for js in js_candidates:
                if not js:
                    continue
                try:
                    if bool(self.driver.execute_script(js)):
                        for _ in range(15):
                            if _upload_panel_is_open():
                                return True
                            time.sleep(0.2)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2673, exc_info=True)

            # 2) Click explicit upload buttons (fallback if linkUpload not available)
            selectors = [
                "#uploadBtnY", "#uploadBtnN", "#uploadBtn",
                "button#uploadBtn", "a#uploadBtn", "button[name='uploadBtn']",
                "a[onclick*='linkUpload']", "button[onclick*='linkUpload']",
                "button[data-target*='upload']",
            ]
            for sel in selectors:
                try:
                    els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                    if not els:
                        continue
                    el = els[0]
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
                    try:
                        el.click()
                    except Exception:
                        self.driver.execute_script("arguments[0].click();", el)
                    for _ in range(15):
                        if _upload_panel_is_open():
                            return True
                        time.sleep(0.2)
                except Exception:
                    continue

            # 3) Text fallback
            if self._click_button_by_text(["上傳文件", "上傳檔案", "+上傳文件", "上傳"]):
                for _ in range(15):
                    if _upload_panel_is_open():
                        return True
                    time.sleep(0.2)
            return False

        def _find_file_input():
            cands = [
                "input[type='file'][name='uploadDoc']",
                "input[name='uploadDoc']",
                "input[type='file']",
            ]
            # Poll for up to ~4s: upload panel / modal may take a moment to render
            # after _open_upload_panel() click or after _select_upload_doc_type() AJAX.
            for _attempt in range(20):
                for sel in cands:
                    try:
                        els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                        if els:
                            return els[0]
                    except Exception:
                        continue
                # Also try each frame (upload widget may be iframed)
                try:
                    pw_page = getattr(self.driver, "_page", None)
                    if pw_page is not None:
                        for fr in pw_page.frames:
                            for sel in cands:
                                try:
                                    el = fr.query_selector(sel)
                                    if el:
                                        return PlaywrightElementWrapper(el, self.driver)
                                except Exception:
                                    continue
                except Exception:
                    pass
                time.sleep(0.2)
            return None

        def _click_upload_confirm() -> bool:
            # 1) Dedicated upload button inside modal
            for sel in ["#upload-button", "#uploadBtn2", "#btnUpload", "button.upload-btn",
                         "input[type='submit'][value*='上傳']", "button[type='submit']"]:
                try:
                    if bool(self.driver.execute_script(
                        f"const b=document.querySelector('{sel}'); if(b){{b.click(); return true;}} return false;"
                    )):
                        return True
                except Exception:
                    continue
            # 2) JS function calls commonly used by LAF portal
            for js in [
                "if(typeof doUpload==='function'){doUpload(); return true;} return false;",
                "if(typeof startUpload==='function'){startUpload(); return true;} return false;",
                "if(typeof fileUpload==='function'){fileUpload(); return true;} return false;",
                "if(typeof uploadFile==='function'){uploadFile(); return true;} return false;",
                "var f=document.querySelector('form[action*=\"upload\"]'); if(f){f.submit(); return true;} return false;",
            ]:
                try:
                    if bool(self.driver.execute_script(js)):
                        return True
                except Exception:
                    continue
            # 3) Text-based fallback
            return self._click_button_by_text(["上傳", "確認上傳", "開始上傳", "送出"])

        def _refresh_uploaded_view() -> None:
            try:
                self.driver.execute_script(
                    "try { if (typeof doAjaxPost === 'function') { doAjaxPost(); } } catch(e) {}"
                )
            except Exception:
                return

        def _uploaded_snapshot() -> Dict[str, Any]:
            try:
                snap = self.driver.execute_script(
                        """
                        const box = document.querySelector('#uploadDocnmsY') || document.querySelector('#uploadDocnms');
                        if (!box) return {rows: 0, sig: ""};
                        const rows = Array.from(box.querySelectorAll('tr'));
                        if (!rows.length) return {rows: 0, sig: ""};
                        let n = 0;
                        const sigs = [];
                        for (const r of rows) {
                          const dataLink = r.querySelector("a[href*='downloadFile'], a[href*='viewUploadFileDetail']");
                          if (dataLink) {
                            n += 1;
                            const tds = r.querySelectorAll('td');
                            const text = Array.from(tds).map(td => (td.textContent || '').trim()).join('|');
                            if (text) sigs.push(text);
                          }
                        }
                        return {rows: n, sig: sigs.join('||')};
                        """
                    ) or {}
                return {
                    "rows": int((snap or {}).get("rows") or 0),
                    "sig": str((snap or {}).get("sig") or ""),
                }
            except Exception:
                return {"rows": 0, "sig": ""}

        def _select_upload_doc_type(pdf_path: str) -> bool:
            wf = (workflow or "").strip().lower()
            full_path = str(pdf_path or "")
            base = os.path.basename(full_path)
            # 來源資料夾判斷：04_我方歷次書狀 → 一律歸為律師撰擬書狀
            _from_our_pleadings = "04_我方歷次書狀" in full_path
            keywords: List[str] = []
            if wf == "condition" or ("調解不成立" in base):
                keywords = ["調解不成立證明書", "調解不成立", "其他書類"]
            elif wf == "fee" or ("收據" in base) or ("裁判費" in base):
                keywords = ["收據", "裁定", "其他書類"]
            elif wf == "closing":
                # 根據檔名判斷文件類型：書狀 → 律師撰擬書狀、判決/裁定/筆錄 → 法院判決
                _is_pleading = _from_our_pleadings or any(k in base for k in [
                    "書狀", "聲請狀", "答辯狀", "準備狀", "上訴狀", "抗告狀",
                    "告訴狀", "自訴狀", "起訴狀", "陳報狀", "異議狀",
                    "補充理由狀", "辯護狀", "反訴狀", "追加狀",
                ])
                _is_judgment = any(k in base for k in ["判決", "裁定", "筆錄", "調解", "和解"])
                _is_prosecution = any(k in base for k in ["處分書", "檢察"])
                if _is_pleading:
                    keywords = ["律師撰擬書狀", "其他書類"]
                elif _is_prosecution:
                    keywords = ["檢察署處分書", "其他書類"]
                elif _is_judgment:
                    keywords = ["法院判決", "裁定", "其他書類"]
                else:
                    keywords = ["其他書類", "法院判決"]
            else:
                keywords = ["其他書類", "其他"]
            try:
                picked = self.driver.execute_script(
                    """
                    const kws = arguments[0] || [];
                    // 嘗試多個 selector：不同 workflow 頁面的 select id 不同
                    var sel = document.querySelector('#cl_docnm2')
                           || document.querySelector('#select1')
                           || document.querySelector('select[name="cl_docnm2"]');
                    if (!sel) return {"ok": false, "reason": "select_not_found"};
                    const opts = Array.from(sel.options || []);
                    let target = null;
                    const hasText = (s, kw) => (String(s || '').indexOf(String(kw || '')) >= 0);
                    for (const kw of kws) {
                      target = opts.find(o => o && o.value && (hasText(o.text, kw) || hasText(o.value, kw)));
                      if (target) break;
                    }
                    if (!target) {
                      target = opts.find(o => o && o.value) || null;
                    }
                    if (!target) return {"ok": false, "reason": "no_nonempty_option"};
                    sel.value = target.value;
                    try { sel.dispatchEvent(new Event('change', {bubbles: true})); } catch (e) {}
                    return {"ok": true, "value": target.value, "text": (target.text || '').trim()};
                    """,
                    keywords,
                )
                if isinstance(picked, dict) and picked.get("ok"):
                    self.log(f"  🏷️ 附件類型：{picked.get('text', '')}")
                    return True
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2791, exc_info=True)
            return False

        def _wait_upload_settled(before_rows: int, before_sig: str, timeout_sec: float = 12.0) -> Dict[str, Any]:
            """Wait for LAF portal upload to truly complete.

            Strategy: poll DOM for upload-in-progress indicators AND alert messages.
            Phase 1: detect upload started (loading overlay / 上傳中 text).
            Phase 2: wait for loading to disappear AND success signal to appear.
            Only declare success when no loading indicator is active.
            """
            end = time.time() + max(1.0, float(timeout_sec or 12.0))
            last = {"ok": False, "reason": "timeout", "rows": before_rows, "sig": before_sig, "alert": ""}
            _saw_loading = False
            while time.time() < end:
                try:
                    snap = self.driver.execute_script(
                        """
                        const box = document.querySelector('#uploadDocnmsY') || document.querySelector('#uploadDocnms');
                        let rows = 0;
                        const sigs = [];
                        if (box) {
                          const trs = Array.from(box.querySelectorAll('tr'));
                          for (const r of trs) {
                            const dataLink = r.querySelector("a[href*='downloadFile'], a[href*='viewUploadFileDetail']");
                            if (dataLink) {
                              rows += 1;
                              const tds = r.querySelectorAll('td');
                              const text = Array.from(tds).map(td => (td.textContent || '').trim()).join('|');
                              if (text) sigs.push(text);
                            }
                          }
                          // Fee-style: container may show entries as plain rows without download links
                          if (rows === 0) {
                            for (const r of trs) {
                              const txt = (r.innerText || '').trim();
                              if (txt) { rows += 1; sigs.push(txt); }
                            }
                          }
                        }
                        const alertMsg = (document.querySelector('#alertMsg') || {}).textContent || '';
                        const msgPart = (document.querySelector('#msgPart') || {}).textContent || '';
                        const merged = (String(alertMsg || '') + ' ' + String(msgPart || '')).trim();

                        // Detect upload-in-progress indicators:
                        // 1) LAF "檔案正在上傳中" dialog/overlay
                        // 2) Generic loading overlays / spinners
                        // 3) jQuery blockUI / modal loading
                        let uploading = false;
                        const bodyText = document.body ? document.body.innerText : '';
                        if (bodyText.indexOf('上傳中') >= 0 || bodyText.indexOf('請稍等') >= 0) uploading = true;
                        // Check visible modal/overlay with loading text
                        const modals = document.querySelectorAll('.modal.show, .modal[style*="display: block"], .blockUI, .loading-overlay, #loading, .upload-progress');
                        for (const m of modals) {
                            const t = (m.textContent || '').trim();
                            if (t.indexOf('上傳中') >= 0 || t.indexOf('請稍等') >= 0 || t.indexOf('uploading') >= 0) {
                                uploading = true; break;
                            }
                        }
                        // Check for active XHR via jQuery if available
                        try { if (typeof jQuery !== 'undefined' && jQuery.active > 0) uploading = true; } catch(e) {}

                        return {"rows": rows, "sig": sigs.join('||'), "alert": merged, "uploading": uploading};
                        """
                    ) or {}
                    rows = int((snap or {}).get("rows") or 0)
                    sig = str((snap or {}).get("sig") or "")
                    alert_msg = str((snap or {}).get("alert") or "").strip()
                    is_uploading = bool((snap or {}).get("uploading"))
                    if is_uploading:
                        _saw_loading = True
                    last = {"ok": False, "reason": "waiting", "rows": rows, "sig": sig, "alert": alert_msg}

                    # Hard error alerts — return immediately
                    if any(k in alert_msg for k in ["請選擇文件類型", "請選擇要上傳檔案", "請選擇要上傳之檔案", "檔案上傳失敗", "超過上傳檔案大小限制"]):
                        return {"ok": False, "reason": "alert_error", "rows": rows, "sig": sig, "alert": alert_msg}

                    # Success signals — but ONLY if upload is no longer in progress
                    if not is_uploading:
                        if "上傳成功" in alert_msg:
                            return {"ok": True, "reason": "alert_success", "rows": rows, "sig": sig, "alert": alert_msg}
                        if (rows > int(before_rows or 0)) or ((rows > 0) and (sig != str(before_sig or ""))):
                            # Row count changed AND no loading indicator → upload likely done
                            if _saw_loading:
                                # We saw loading start and stop, plus rows changed — high confidence
                                return {"ok": True, "reason": "rows_changed_after_loading", "rows": rows, "sig": sig, "alert": alert_msg}
                            else:
                                # Rows changed but we never saw loading — give a brief grace period
                                # in case AJAX is still in flight
                                time.sleep(1.0)
                                # Re-check loading state
                                still_loading = bool(self.driver.execute_script(
                                    """
                                    try { if (typeof jQuery !== 'undefined' && jQuery.active > 0) return true; } catch(e) {}
                                    const t = document.body ? document.body.innerText : '';
                                    return (t.indexOf('上傳中') >= 0 || t.indexOf('請稍等') >= 0);
                                    """
                                ) or False)
                                if not still_loading:
                                    return {"ok": True, "reason": "rows_changed", "rows": rows, "sig": sig, "alert": alert_msg}
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2833, exc_info=True)
                time.sleep(0.5)
            return last

        def _read_upload_context() -> Dict[str, str]:
            try:
                ctx = self.driver.execute_script(
                    """
                    const rid = (document.querySelector('#uploadReply_id') || {}).value || '';
                    const f1 = (document.querySelector('#uploadAt_type') || {}).value || '';
                    let f2 = '';
                    try { f2 = (typeof setUploadFieldName !== 'undefined') ? String(setUploadFieldName || '') : ''; } catch (e) {}
                    return {reply_id: String(rid || ''), at_type: String(f1 || ''), field_name: String(f2 || '')};
                    """
                ) or {}
                return {
                    "reply_id": str((ctx or {}).get("reply_id") or "").strip(),
                    "at_type": str((ctx or {}).get("at_type") or "").strip(),
                    "field_name": str((ctx or {}).get("field_name") or "").strip(),
                }
            except Exception:
                return {"reply_id": "", "at_type": "", "field_name": ""}

        def _verify_upload_in_server(reply_id: str, field_name: str, file_basename: str) -> bool:
            rid = (reply_id or "").strip()
            fld = (field_name or "").strip()
            if not rid:
                return False
            if not fld:
                fld = "CND" if (workflow or "").strip().lower() == "condition" else "NOT_OPEN"
            try:
                payload = self.driver.execute_async_script(
                    """
                    const cb = arguments[arguments.length - 1];
                    const rid = arguments[0];
                    const fld = arguments[1];
                    const url = '/lafcsp/genUploadFilesView?reply_id=' + encodeURIComponent(rid)
                              + '&proc_status=T&uploadFieldName=' + encodeURIComponent(fld);
                    fetch(url, {credentials: 'include'})
                      .then(r => r.json())
                      .then(j => cb({ok: true, data: j}))
                      .catch(e => cb({ok: false, err: String(e)}));
                    """,
                    rid,
                    fld,
                ) or {}
            except Exception:
                return False
            data = payload.get("data")
            if not isinstance(data, list):
                return False
            b = (file_basename or "").strip()
            for item in data:
                if not isinstance(item, dict):
                    continue
                fns = item.get("filenames")
                if isinstance(fns, list):
                    for one in fns:
                        if isinstance(one, dict):
                            fn = str(one.get("file_name") or "").strip()
                            if fn and ((not b) or (b in fn) or (fn in b)):
                                return True
            return False

        # --- Pre-upload: get existing files on server for dedup ---
        _open_upload_panel()
        _refresh_uploaded_view()
        ctx = _read_upload_context()
        _existing_files: set = set()
        try:
            _existing_json = self.driver.execute_async_script(
                """
                var cb = arguments[arguments.length - 1];
                var rid = arguments[0]; var fld = arguments[1];
                var url = '/lafcsp/genUploadFilesView?reply_id=' + encodeURIComponent(rid)
                        + '&proc_status=T&uploadFieldName=' + encodeURIComponent(fld);
                fetch(url, {credentials: 'include'})
                  .then(function(r) { return r.json(); })
                  .then(function(j) { cb({ok: true, data: j}); })
                  .catch(function(e) { cb({ok: false, err: String(e)}); });
                """,
                ctx.get("reply_id", ""),
                ctx.get("field_name", "") or ({
                    "condition": "CND", "fee": "LGFEE", "inquiry": "RSM",
                    "withdrawal": "PB_doc", "closing": "CR_CS",
                }.get((workflow or "").strip().lower(), "")),
            ) or {}
            if isinstance(_existing_json.get("data"), list):
                for item in _existing_json["data"]:
                    fns = item.get("filenames") if isinstance(item, dict) else []
                    for one in (fns or []):
                        fn = str((one or {}).get("file_name") or "").strip()
                        if fn:
                            _existing_files.add(fn)
            if _existing_files:
                self.log(f"  📋 伺服器已有 {len(_existing_files)} 個附件")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2930, exc_info=True)

        for p in pdfs:
            try:
                basename = os.path.basename(p)
                # --- Dedup: skip if already uploaded ---
                if basename in _existing_files:
                    self.log(f"  ⏭️ 已存在，跳過: {basename}")
                    result["uploaded"].append(p)
                    continue

                # --- Native UI upload: mimic human click flow ---
                # 1) Ensure upload panel is open (may need to re-open for each file)
                _open_upload_panel()
                time.sleep(0.5)

                # 2) Select document type
                _select_upload_doc_type(p)

                # 3) Snapshot current file list (for _wait_upload_settled comparison)
                # LAF portal uses different container IDs depending on workflow:
                #  - #uploadDocnmsY: go_live/condition/closing/etc.
                #  - #uploadDocnms:  fee/other workflows
                # Query both to be safe.
                _before_snap = self.driver.execute_script(
                    """
                    const box = document.querySelector('#uploadDocnmsY') || document.querySelector('#uploadDocnms');
                    let rows = 0; const sigs = [];
                    if (box) {
                        const trs = Array.from(box.querySelectorAll('tr'));
                        for (const r of trs) {
                            const dl = r.querySelector("a[href*='downloadFile'], a[href*='viewUploadFileDetail']");
                            if (dl) { rows++; sigs.push(Array.from(r.querySelectorAll('td')).map(td => (td.textContent||'').trim()).join('|')); }
                        }
                        // Fee-style container may just list text rows without download links;
                        // accept any row with text as a signature so we can detect change.
                        if (rows === 0) {
                            for (const r of trs) {
                                const txt = (r.innerText || '').trim();
                                if (txt) { rows++; sigs.push(txt); }
                            }
                        }
                    }
                    return {"rows": rows, "sig": sigs.join('||')};
                    """
                ) or {}
                _before_rows = int((_before_snap or {}).get("rows") or 0)
                _before_sig = str((_before_snap or {}).get("sig") or "")

                # 4) Find <input type="file"> and send file path via send_keys
                _file_input = _find_file_input()
                if not _file_input:
                    raise RuntimeError("找不到 file input 元素")
                # Make the input visible so send_keys works (some portals hide it)
                self.driver.execute_script(
                    "arguments[0].style.display='block'; arguments[0].style.visibility='visible';"
                    " arguments[0].style.opacity='1'; arguments[0].style.height='auto';"
                    " arguments[0].style.width='auto'; arguments[0].style.position='relative';",
                    _file_input,
                )
                _file_input.send_keys(str(p))
                self.log(f"  📂 已選取檔案: {basename}")
                time.sleep(0.5)

                # 5) Click upload/confirm button
                _confirm_clicked = _click_upload_confirm()
                if not _confirm_clicked:
                    self.log(f"  ⚠️ 找不到上傳確認按鈕，嘗試觸發 file input onchange...")
                    # Some portals auto-upload on file selection via onchange
                    self.driver.execute_script(
                        "arguments[0].dispatchEvent(new Event('change', {bubbles:true}));",
                        _file_input,
                    )
                time.sleep(1.0)

                # 6) Wait for upload to settle (AJAX completion + file list change)
                _settled = _wait_upload_settled(_before_rows, _before_sig, timeout_sec=30.0)
                _ctx = _read_upload_context()
                _rid = _ctx.get("reply_id", "")
                _fld = _ctx.get("field_name", "")

                if _settled.get("ok"):
                    # DOM says success — verify on server for certainty
                    if _rid and _verify_upload_in_server(_rid, _fld, basename):
                        result["uploaded"].append(p)
                        _existing_files.add(basename)
                        self.log(f"  📎 已上傳: {basename} (server確認)")
                    else:
                        # DOM changed but server can't find the file yet — retry server check
                        _server_ok = False
                        for _retry in range(3):
                            time.sleep(2.0)
                            if _rid and _verify_upload_in_server(_rid, _fld, basename):
                                _server_ok = True
                                break
                        if _server_ok:
                            result["uploaded"].append(p)
                            _existing_files.add(basename)
                            self.log(f"  📎 已上傳: {basename} (server延遲確認)")
                        else:
                            # Accept DOM-only confirmation as last resort
                            result["uploaded"].append(p)
                            _existing_files.add(basename)
                            self.log(f"  📎 已上傳: {basename} (DOM確認，server未驗證)")
                else:
                    # 7) DOM didn't confirm — server-side fallback with retries
                    _server_ok = False
                    for _retry in range(4):
                        if _retry > 0:
                            time.sleep(2.0)
                        if _rid and _verify_upload_in_server(_rid, _fld, basename):
                            _server_ok = True
                            break
                    if _server_ok:
                        result["uploaded"].append(p)
                        _existing_files.add(basename)
                        self.log(f"  📎 已上傳(server驗證): {basename}")
                    else:
                        _alert = _settled.get("alert", "")
                        raise RuntimeError(f"上傳未確認: {_settled.get('reason', 'unknown')}"
                                           f"{(' — ' + _alert) if _alert else ''}")

                # 8) Return to the workflow form so the final preview is captured on the filled page,
                #    not on the upload / uploading overlay.
                try:
                    self._restore_workflow_form_modal(workflow, close_upload_dialog=True)
                    time.sleep(0.5)
                except Exception:
                    pass

            except Exception as e:
                result["failed"].append({"path": p, "error": str(e)})
                self.log(f"  ⚠️ 上傳失敗: {os.path.basename(p)} ({e})")

        if result["failed"] and (not result["uploaded"]):
            result["ok"] = False

        # After successful uploads: verify on server + ensure portal flag is set
        if result["uploaded"]:
            # ── Server-side verification: confirm each file exists ──
            _rid = ctx.get("reply_id", "")
            _fld = ctx.get("field_name", "") or ({
                "condition": "CND", "fee": "LGFEE", "inquiry": "RSM",
                "withdrawal": "PB_doc", "closing": "CR_CS", "go_live": "NOT_OPEN",
            }.get((workflow or "").strip().lower(), ""))
            _verified_all = True
            for _up_path in result["uploaded"]:
                _up_base = os.path.basename(_up_path)
                if _verify_upload_in_server(_rid, _fld, _up_base):
                    self.log(f"  ✅ 伺服器驗證通過: {_up_base}")
                else:
                    self.log(f"  ⚠️ 伺服器驗證失敗: {_up_base}")
                    _verified_all = False
            result["server_verified"] = _verified_all

            # ── Set hasUploadFileData flag so portal knows files were added ──
            try:
                self.driver.execute_script(
                    "try { window.hasUploadFileData = true; } catch(e) {}"
                    " try { if (typeof hasUploadFileData !== 'undefined') hasUploadFileData = true; } catch(e) {}"
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "upload_flag", exc_info=True)
            try:
                self._restore_workflow_form_modal(workflow, close_upload_dialog=True)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "upload_restore_final", exc_info=True)

        self.last_upload_result = dict(result)
        return result

    def _find_report_button_for_person(self, person_name: str, onclick_tokens: List[str]):
        """在包含當事人姓名的列內，找『回報/明細』按鈕。"""
        if not self.driver:
            return None
        who = (person_name or "").strip()
        if not who:
            return None
        try:
            js = """
const who = (arguments[0] || '').toString();
const tokens = arguments[1] || [];
const rows = Array.from(document.querySelectorAll('tr'));
const visible = (el) => {
  const st = window.getComputedStyle(el);
  if (!st || st.display === 'none' || st.visibility === 'hidden') return false;
  const r = el.getBoundingClientRect();
  return r.width > 1 && r.height > 1;
};
for (const row of rows) {
  const txt = ((row.innerText || '') + '').replace(/\\s+/g, '');
  if (!txt || !txt.includes(who.replace(/\\s+/g, ''))) continue;
  const cands = Array.from(row.querySelectorAll('a,button,input[type=button],input[type=submit]'));
  for (const el of cands) {
    if (!visible(el) || el.disabled) continue;
    const t = (el.innerText || el.textContent || el.value || '').toString().trim();
    const oc = (el.getAttribute('onclick') || '');
    const hf = (el.getAttribute('href') || '');
    const hasToken = tokens.some(k => (oc.includes(k) || hf.includes(k)));
    const hasText = ['回報', '明細', '填報', '處理', '我要回報'].some(k => t.includes(k));
    if (hasToken || hasText) return el;
  }
}
return null;
"""
            el = self.driver.execute_script(js, who, onclick_tokens or [])
            return el
        except Exception:
            return None

    def open_closing_report_page(self, laf_case_number: str) -> bool:
        """
        進入「結案/報結」清單頁，定位指定法扶案號並點擊回報，進入 toCR 表單。
        注意：不會送出，僅進入表單頁。
        """
        if not self.driver:
            self.log("❌ 瀏覽器未初始化")
            return False
        applyno = (laf_case_number or "").strip()
        if not applyno:
            self.log("❌ 缺少法扶案號")
            return False

        url = f"{self.base_url}/lafcsp/toClosedReport"
        self.log(f"🌐 開啟結案清單頁: {url}")
        self.driver.get(url)
        time.sleep(1.2)
        self._switch_to_content_frame_if_any()

        # 填入申請編號並搜尋
        try:
            inp = None
            # Poll across main page + all frames for up to 15s — avoids
            # Selenium WebDriverWait quirks when driver is PlaywrightDriverWrapper.
            _deadline = time.time() + 15.0
            _selectors = ["#applyno", "input[name='applyno']", "input#applyno"]
            while time.time() < _deadline and not inp:
                for sel in _selectors:
                    try:
                        els = self.driver.find_elements(By.CSS_SELECTOR, sel)
                        if els:
                            inp = els[0]
                            break
                    except Exception:
                        continue
                if inp:
                    break
                # Also try each Playwright frame directly
                try:
                    pw_page = getattr(self.driver, "_page", None)
                    if pw_page is not None:
                        for fr in pw_page.frames:
                            for sel in _selectors:
                                try:
                                    el = fr.query_selector(sel)
                                    if el:
                                        inp = PlaywrightElementWrapper(el, self.driver)
                                        break
                                except Exception:
                                    continue
                            if inp:
                                break
                except Exception:
                    pass
                if inp:
                    break
                time.sleep(0.3)
            if not inp:
                self.log("❌ 找不到結案清單的申請編號輸入框")
                self._save_page_debug_html("closing_list_no_applyno", force=True)
                # 可能被踢回登入或 frameset 未切換，嘗試 default → contentFrame
                try:
                    self.driver.switch_to.default_content()
                    frames = self.driver.find_elements(By.CSS_SELECTOR, "frame, iframe")
                    self.log(f"  🔍 可用 frames: {len(frames)}")
                    for fr in frames:
                        try:
                            fname = fr.get_attribute("name") or fr.get_attribute("id") or "?"
                            self.log(f"  🔍 Frame: {fname}")
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3184, exc_info=True)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3186, exc_info=True)
                return False
            self._set_input_value(inp, applyno)
        except Exception as e:
            self.log(f"❌ 無法輸入申請編號: {e}")
            return False

        # 觸發搜尋：優先呼叫 showList()，不行再按按鈕
        searched = False
        try:
            searched = bool(self.driver.execute_script("if (typeof showList === 'function') { showList(); return true; } return false;"))
        except Exception:
            searched = False
        if not searched:
            searched = self._click_button_by_text(["開始搜尋", "搜尋", "查詢"])
        time.sleep(1.2)

        # 找回報按鈕（onclick 含 toReport 並含案號）
        try:
            # 先用案號縮小範圍
            # Drafts use href="javascript:toReport...", New uses onclick="toReport..."
            q_case = json.dumps(applyno)
            xp = f"//*[(contains(@onclick, 'toReport(') or contains(@href, 'toReport(')) and (contains(@onclick, {q_case}) or contains(@href, {q_case}))]"
            els = self.driver.find_elements(By.XPATH, xp)
            
            # Prioritize Draft (reply_id not empty)
            btn = None
            if els:
                best_btn = els[0] # Default to first
                for el in els:
                    try:
                        # Check both attributes
                        attr_val = (el.get_attribute("onclick") or "") + (el.get_attribute("href") or "")
                        # Parse regex for 4th argument
                        # toReport('1150206-A-042', '蕭仁俊', 'T', '720099', ...
                        import re
                        m = re.search(r"toReport\s*\(([^)]+)\)", attr_val)
                        if m:
                            args_str = m.group(1)
                            # Split by comma, careful with quotes
                            # Simple split by comma might work if no commas in values
                            args = [x.strip().strip("'").strip('"') for x in args_str.split(',')]
                            if len(args) >= 4:
                                reply_id = args[3]
                                if reply_id and reply_id.strip():
                                    self.log(f"  ✨ 發現暫存案件 (reply_id={reply_id})，優先使用。")
                                    best_btn = el
                                    break
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3235, exc_info=True)
                btn = best_btn

            if not btn:
                btn = self._find_clickable_by_onclick("toReport(")
            if not btn:
                self.log("❌ 找不到『回報』按鈕（toReport）")
                # Debug dump
                if self._debug_capture_enabled():
                    try:
                        from api.debug_capture import save_debug_screenshot
                        save_debug_screenshot(self.driver, f"debug_closing_list_{applyno}", context="法扶找不到回報按鈕")
                        self.log(f"  📷 已保存截圖: debug_closing_list_{applyno}")
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3250, exc_info=True)
                return False
            try:
                self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3255, exc_info=True)
            try:
                btn.click()
            except Exception:
                self.driver.execute_script("arguments[0].click();", btn)
        except Exception as e:
            self.log(f"❌ 點擊回報失敗: {e}")
            return False

        # 等待跳轉到 toCR
        try:
            WebDriverWait(self.driver, 25).until(lambda d: "toCR" in (d.current_url or ""))
        except Exception:
            # frameset 可能在 top，嘗試回到 default 後再檢查
            try:
                self.driver.switch_to.default_content()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3272, exc_info=True)
            try:
                WebDriverWait(self.driver, 10).until(lambda d: "toCR" in (d.current_url or "") or "toCR" in (d.page_source or ""))
            except Exception:
                self.log("⚠️ 未偵測到 toCR URL（可能仍在 frame 內），仍嘗試繼續填寫。")

        self._switch_to_content_frame_if_any()
        self.log("✅ 已進入報結表單頁（toCR）")
        return True

        return True

    def _fill_casekd_cascade(self, clcate_path: list) -> bool:
        """
        填寫結案類型級聯選單 (casekd → level1 → level2 → ...)。

        Portal 使用 AJAX 動態載入每層選項：
        GET /lafcsp/getPLS12ByFnode?category=L41A_1&fnode=<parent_value>

        casekd/level selects 在 #dialog-clCate modal 裡面。
        需要先呼叫 doAdd() 打開 modal，填完後按確認（setClcate）關閉。

        Multi-step approach:
        1. 先開 modal（doAdd / btn.click / $.modal('show')）
        2. WebDriverWait 等待 casekd select 出現
        3. 同步 AJAX 填寫每層選單
        4. 呼叫 setClcate() + 關閉 modal

        Args:
            clcate_path: 文字標籤陣列，如 ["扶助種類為訴訟代理或辯護", "民/家/勞案件", ...]
        """
        if not clcate_path or not self.driver:
            return False

        self.log(f"  📋 Filling casekd cascade: {' → '.join(clcate_path)}")
        import json
        _path_json = json.dumps(clcate_path, ensure_ascii=False)

        try:
            # ── Step 1: 開 modal ──
            # casekd select 在 #dialog-clCate modal 裡，需先 show modal
            # doAdd() 會呼叫 iniFormClcate() 初始化選單
            self.driver.execute_script("""
                try {
                    var btn = document.getElementById('clcate_btn');
                    if (btn) {
                        btn.disabled = false;
                        btn.removeAttribute('disabled');
                        btn.click();
                    } else if (typeof doAdd === 'function') {
                        doAdd();
                    }
                } catch(e) {}
                // 備用：直接 show modal
                try {
                    var modal = document.getElementById('dialog-clCate');
                    if (modal && typeof $ !== 'undefined') {
                        $(modal).modal('show');
                    }
                } catch(e) {}
                // 呼叫 iniFormClcate 確保 casekd 已初始化（含既有草稿恢復）
                try {
                    if (typeof iniFormClcate === 'function') { iniFormClcate(); }
                } catch(e) {}
            """)

            # ── Step 2: 等待 casekd select 出現 ──
            casekd_found = False
            for _wait in range(8):  # 最多等 4 秒
                time.sleep(0.5)
                try:
                    found = self.driver.execute_script(
                        "return !!(document.getElementsByName('casekd')[0] || document.getElementById('casekd'));"
                    )
                    if found:
                        casekd_found = True
                        break
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3350, exc_info=True)

            if not casekd_found:
                # Debug: dump DOM info to diagnose
                try:
                    dom_info = self.driver.execute_script("""
                        var info = {};
                        info.url = location.href;
                        info.title = document.title;
                        info.modal_exists = !!document.getElementById('dialog-clCate');
                        info.modal_display = '';
                        try {
                            var m = document.getElementById('dialog-clCate');
                            if (m) info.modal_display = window.getComputedStyle(m).display;
                        } catch(e) {}
                        info.all_selects = [];
                        var sels = document.querySelectorAll('select');
                        for (var i = 0; i < Math.min(sels.length, 30); i++) {
                            info.all_selects.push({
                                name: sels[i].name || '',
                                id: sels[i].id || '',
                                options: sels[i].options.length
                            });
                        }
                        info.frames = document.querySelectorAll('frame, iframe').length;
                        info.body_snippet = document.body ? document.body.innerHTML.substring(0, 500) : '';
                        return info;
                    """)
                    self.log(f"  ❌ casekd NOT_FOUND after wait — DOM info: {dom_info}")
                except Exception as e:
                    self.log(f"  ❌ casekd NOT_FOUND after wait — DOM probe failed: {e}")
                # Save debug HTML (force=True to capture even without env flag)
                self._save_page_debug_html("casekd_not_found", force=True)
                # Fallback: try setting clcate text field directly
                return self._fill_casekd_fallback(clcate_path)

            # ── Step 3: 逐層填寫 cascade ──
            # Portal 的 getNextLevel() 在 AJAX 回應中檢查 is_enode='Y' 來建立
            # endLevelMap，並在選到 terminal node 時設定 endLevel / docnmClcate。
            # 我們的自定 AJAX 也必須模擬這些行為，否則 setClcate() → checkDataClcate()
            # 會因為 docnmClcate 為空而 return false，導致 clcate 和 cl_docnm 都空白。
            result = self.driver.execute_script(f"""
                var path = {_path_json};
                var selectNames = ['casekd', 'level1', 'level2', 'level3', 'level4',
                                   'level5', 'level6', 'level7', 'level8', 'level9'];
                var results = {{}};
                var lastValue = '';
                var lastStep = -1;
                var lastSelName = '';
                var lastOptText = '';

                for (var step = 0; step < path.length && step < selectNames.length; step++) {{
                    var selName = selectNames[step];
                    var targetText = path[step];
                    var sel = document.getElementsByName(selName)[0] ||
                              document.getElementById(selName);
                    if (!sel) {{
                        results[selName] = 'NOT_FOUND';
                        break;
                    }}

                    // Enable the select (portal disables them initially)
                    sel.disabled = false;
                    sel.removeAttribute('disabled');

                    // If this is not casekd (level 0), load options via synchronous AJAX
                    if (step > 0 && lastValue) {{
                        var xhr = new XMLHttpRequest();
                        xhr.open('GET', '/lafcsp/getPLS12ByFnode?category=L41A_1&fnode=' + lastValue, false);
                        xhr.send(null);
                        if (xhr.status === 200) {{
                            try {{
                                var data = JSON.parse(xhr.responseText);
                                sel.innerHTML = '<option value="">請選擇</option>';
                                for (var i = 0; i < data.length; i++) {{
                                    var opt = document.createElement('option');
                                    opt.value = data[i].seq || data[i].value || '';
                                    opt.textContent = data[i].nodenm || data[i].text || '';
                                    sel.appendChild(opt);
                                    // 模擬 portal 的 endLevelMap 建立：
                                    // 若 is_enode='Y'，將此 seq 標記為 terminal，
                                    // 對應的 endLevel 就是當前 select 的 level
                                    if (data[i].is_enode === 'Y' && typeof endLevelMap !== 'undefined') {{
                                        try {{ endLevelMap.put(data[i].seq, selName); }} catch(e2) {{}}
                                    }}
                                }}
                            }} catch(e) {{
                                results[selName] = 'AJAX_PARSE_ERROR: ' + e.message;
                                break;
                            }}
                        }} else {{
                            results[selName] = 'AJAX_FAILED: ' + xhr.status;
                            break;
                        }}
                    }}

                    // Find and select the option matching targetText
                    var matched = false;
                    for (var j = 0; j < sel.options.length; j++) {{
                        var optText = sel.options[j].textContent.trim();
                        if (optText === targetText || optText.indexOf(targetText) >= 0 ||
                            targetText.indexOf(optText) >= 0) {{
                            sel.selectedIndex = j;
                            lastValue = sel.options[j].value;
                            matched = true;
                            lastStep = step;
                            lastSelName = selName;
                            lastOptText = optText;
                            results[selName] = optText + ' (' + lastValue + ')';
                            sel.dispatchEvent(new Event('change', {{bubbles: true}}));
                            break;
                        }}
                    }}
                    if (!matched) {{
                        results[selName] = 'NO_MATCH for: ' + targetText;
                        break;
                    }}
                }}

                // ── Step 3b: 設定 endLevel + docnmClcate ──
                // setClcate() 依賴 endLevel 來決定讀取哪些 level 的文字，
                // checkDataClcate() 依賴 docnmClcate 不為空。
                // 最後填寫的那一層即為 terminal node。
                if (lastStep >= 0) {{
                    try {{
                        endLevel = lastSelName;  // 全域變數
                        results._endLevel = lastSelName;
                    }} catch(e) {{}}
                    try {{
                        $('#docnmClcate').val(lastOptText);
                        $('#docnmClcatePart').html(lastOptText);
                        results._docnmClcate = lastOptText;
                    }} catch(e) {{}}
                    // 也更新 endLevelMap 以防 setClcate 內部再查
                    try {{
                        if (typeof endLevelMap !== 'undefined' && lastValue) {{
                            endLevelMap.put(lastValue, lastSelName);
                        }}
                    }} catch(e) {{}}
                }}

                // ── Step 4: 呼叫 setClcate() 組合結果 + 關閉 modal ──
                try {{
                    if (typeof setClcate === 'function') {{
                        setClcate();
                        results._setClcate = 'called';
                    }}
                }} catch(e) {{
                    results._setClcate_error = e.message || String(e);
                }}

                // 讀回 clcate 值（setClcate 會填入主表單的 clcate 欄位）
                var clcateEl = document.getElementsByName('clcate')[0] ||
                               document.getElementById('clcate');
                if (clcateEl && clcateEl.value) {{
                    results.clcate = clcateEl.value;
                }}
                // 讀回 cl_docnm（setClcate 會從 docnmClcate 複製到 cl_docnm）
                var clDocnmEl = document.getElementById('cl_docnm');
                results._cl_docnm = clDocnmEl ? clDocnmEl.value : '';

                // 若 setClcate 沒有生效，手動組合並設定所有相關欄位
                if (!results.clcate) {{
                    var allText = '';
                    for (var k = 0; k < selectNames.length; k++) {{
                        var s = document.getElementsByName(selectNames[k])[0];
                        if (s && s.selectedIndex > 0) {{
                            var t = s.options[s.selectedIndex].textContent.trim();
                            allText += (allText ? '、' : '') + t;
                        }}
                    }}
                    if (allText && clcateEl) {{
                        clcateEl.readOnly = false;
                        clcateEl.removeAttribute('readonly');
                        clcateEl.value = allText;
                        results.clcate = allText;
                        results._manual_set = true;
                    }}
                    // 手動設定 cl_docnm（setClcate 失敗時不會設定）
                    if (lastOptText) {{
                        try {{
                            $('#cl_docnm').val(lastOptText);
                            $('#cl_docnmPart').html(lastOptText);
                            results._cl_docnm = lastOptText;
                        }} catch(e) {{}}
                    }}
                }}

                // 關閉 modal
                try {{
                    var modal = document.getElementById('dialog-clCate');
                    if (modal && typeof $ !== 'undefined') {{
                        $(modal).modal('hide');
                    }}
                }} catch(e) {{}}

                return results;
            """)
            self.log(f"  📋 Casekd cascade result: {result}")
            return bool(result and result.get("clcate"))
        except Exception as e:
            self.log(f"  ❌ Casekd cascade failed: {e}")
            return False

    def _fill_casekd_fallback(self, clcate_path: list) -> bool:
        """Fallback: 直接設定 clcate text 欄位（casekd select 不可用時）。"""
        if not clcate_path or not self.driver:
            return False
        import json
        _path_json = json.dumps(clcate_path, ensure_ascii=False)
        try:
            result = self.driver.execute_script(f"""
                var path = {_path_json};
                var fallbackText = path.join('、');
                var terminalText = path[path.length - 1] || '';
                var clcateEl = document.getElementsByName('clcate')[0] ||
                               document.getElementById('clcate');
                if (clcateEl) {{
                    clcateEl.readOnly = false;
                    clcateEl.removeAttribute('readonly');
                    clcateEl.value = fallbackText;
                    // 同步設定 docnmClcate / cl_docnm（server 驗證需要）
                    try {{
                        $('#docnmClcate').val(terminalText);
                        $('#docnmClcatePart').html(terminalText);
                        $('#cl_docnm').val(terminalText);
                        $('#cl_docnmPart').html(terminalText);
                    }} catch(e) {{}}
                    return {{clcate: fallbackText, _fallback: true, _docnm: terminalText}};
                }}
                return {{clcate: '', _fallback: true, _no_clcate_field: true}};
            """)
            self.log(f"  📋 Casekd fallback result: {result}")
            return bool(result and result.get("clcate"))
        except Exception as e:
            self.log(f"  ❌ Casekd fallback failed: {e}")
            return False

    @staticmethod
    def _to_roc_date(dt) -> str:
        """將日期轉為民國格式 7 碼（如 1141210 = 2025-12-10）。"""
        from datetime import datetime, date
        if isinstance(dt, str):
            dt = datetime.strptime(dt.split(" ")[0], "%Y-%m-%d").date()
        elif isinstance(dt, datetime):
            dt = dt.date()
        roc_year = dt.year - 1911
        return f"{roc_year:03d}{dt.month:02d}{dt.day:02d}"

    def _add_date_rows(self, dates, add_fn: str, input_name: str,
                       calc_fn: str, label: str) -> int:
        """
        在報結第二頁用 doAdd*Dt() 新增日期列並填入民國日期。
        dates: 日期清單 (date/datetime/str)
        add_fn: JS 函式名 (如 'doAddViewSheetDt')
        input_name: 新增列裡 input 的 name (如 'viewsheetdtAry')
        calc_fn: 計算次數的 JS 函式 (如 'calculateViewsheet_times')
        Returns: 成功新增的筆數
        """
        added = 0
        for dt in dates:
            try:
                roc = self._to_roc_date(dt)
            except Exception:
                self.log(f"  ⚠️ {label}日期格式錯誤：{dt}")
                continue
            try:
                # 呼叫 doAdd*Dt() 新增一列
                self.driver.execute_script(f"{add_fn}();")
                import time as _time
                _time.sleep(0.3)
                # 找到最後一個同名 input 並填入日期
                ok = self.driver.execute_script(f"""
                    var inputs = document.getElementsByName('{input_name}');
                    if (!inputs || inputs.length === 0) return false;
                    var last = inputs[inputs.length - 1];
                    last.value = '{roc}';
                    last.dispatchEvent(new Event('change', {{bubbles: true}}));
                    last.dispatchEvent(new Event('blur', {{bubbles: true}}));
                    // 觸發計算函式
                    if (typeof {calc_fn} === 'function') {calc_fn}(last);
                    return true;
                """)
                if ok:
                    added += 1
                    self.log(f"  📅 {label}日期：{roc}（{dt}）")
                else:
                    self.log(f"  ⚠️ {label}日期列新增失敗：找不到 input[name={input_name}]")
            except Exception as e:
                self.log(f"  ❌ {label}日期 {dt} 新增失敗：{e}")
        return added

    def fill_closing_report(self, counts: Dict[str, Any], zero_reasons: Dict[str, str] = None) -> bool:
        """
        填寫報結次數與理由欄位（暫存/送出前置）。
        Auto-navigates from Page 1 (toCR) to Page 2 (toClosedSummaryLawyer).
        """
        if not self.driver:
            return False
        zero_reasons = zero_reasons or {}
        on_page2 = False

        # 1. Check Page 1 (toCR) and navigate to Page 2
        try:
            # Check if we are on Page 1 (look for 'casekd' or 'toCR' in url)
            if "toCR" in self.driver.current_url or len(self.driver.find_elements("name", "casekd")) > 0:
                self.log("📄 偵測到報結第一頁 (Basic Info)，嘗試前往第二頁...")
                try:
                    applyno = str(
                        self.driver.execute_script(
                            """
                            var el = document.getElementsByName('applyno')[0] || document.getElementById('applyno');
                            return el ? (el.value || '') : '';
                            """
                        )
                        or ""
                    ).strip()
                except Exception:
                    applyno = ""
                
                self.log("  🔧 Phase 0: Setting Page 1 fields via JS...")
                # Use counts to fill Page 1 court info
                import datetime
                _judg_dt_raw = str((counts or {}).get("judg_dt") or "").strip()
                if _judg_dt_raw:
                    # Convert ISO date (2026-03-16) to Taiwan date (1150316)
                    try:
                        _dt = datetime.datetime.strptime(_judg_dt_raw, "%Y-%m-%d").date()
                        _tw_dt = f"{_dt.year - 1911}{_dt.month:02d}{_dt.day:02d}"
                    except Exception:
                        _tw_dt = _judg_dt_raw.replace("/", "").replace("-", "")
                else:
                    _today = datetime.date.today()
                    _tw_dt = f"{_today.year - 1911}{_today.month:02d}{_today.day:02d}"

                _court_kind = str((counts or {}).get("court_kind") or "法院").strip()
                _court_name_raw = str((counts or {}).get("court_name") or "").strip()
                # 憲法法庭不在法院/檢察署清單，用「其他」
                if "憲法法庭" in _court_name_raw:
                    _rel_court1 = "其他"
                else:
                    _rel_court1 = {"法院": "法院", "檢察署": "檢察署"}.get(_court_kind, "法院")
                _court_name = str((counts or {}).get("court_name") or "").strip()
                _judg_eff = str((counts or {}).get("judg_eff") or "").strip()
                # judg_eff 驗證：只允許法扶表單上實際存在的選項
                _valid_judg_eff = ("對受扶助人較有利", "對受扶助人較不利", "其他")
                if _judg_eff not in _valid_judg_eff:
                    _judg_eff = "其他"
                _appellee = str((counts or {}).get("appellee") or "無").strip()
                _court_year = str((counts or {}).get("court_case_year") or "").strip()
                _court_code = str((counts or {}).get("court_case_code") or "").strip()
                _court_no = str((counts or {}).get("court_case_no") or "").strip()
                # 刑期 / 緩刑（刑事案件 Page 1 欄位）
                _sentence_term = str((counts or {}).get("sentence_term") or "").strip()
                _reprieve_term = str((counts or {}).get("reprieve_term") or "").strip()

                js_fill = f"""
                function _fire(el) {{ el.dispatchEvent(new Event('change', {{bubbles:true}})); }}
                function setVal(name, val, forceOverwrite) {{
                    // Set ALL elements with the same name (hidden + visible)
                    // to avoid server picking the empty visible one
                    var els = document.getElementsByName(name);
                    if (!els.length) {{
                        var byId = document.getElementById(name);
                        if (!byId) return false;
                        els = [byId];
                    }}
                    var set = 0;
                    for (var i = 0; i < els.length; i++) {{
                        var el = els[i];
                        if (!forceOverwrite && el.value && el.value.trim()) continue;
                        if (el.readOnly) el.readOnly = false;
                        el.value = val;
                        _fire(el);
                        set++;
                    }}
                    return set > 0;
                }}
                function setSelect(name, val) {{
                    var els = document.getElementsByName(name);
                    if (!els.length) {{
                        var byId = document.getElementById(name);
                        if (!byId) return false;
                        els = [byId];
                    }}
                    var set = 0;
                    for (var j = 0; j < els.length; j++) {{
                        var el = els[j];
                        if (el.tagName === 'SELECT') {{
                            for (var i = 0; i < el.options.length; i++) {{
                                if (el.options[i].value === val || el.options[i].text === val) {{
                                    el.selectedIndex = i;
                                    _fire(el);
                                    set++;
                                    break;
                                }}
                            }}
                            if (set === 0) {{ el.value = val; _fire(el); set++; }}
                        }} else {{
                            el.value = val;
                            _fire(el);
                            set++;
                        }}
                    }}
                    return set > 0;
                }}
                var results = {{}};
                // is_citizen_judge: 直接設值，不 fire change！
                // changeIs_citizen_judge() 會 submit form → 頁面重載，
                // 導致所有 fill 全部遺失。portal $(document).ready 已設 '否'。
                var icjEl = document.getElementById('is_citizen_judge');
                if (icjEl) {{
                    for (var ii = 0; ii < icjEl.options.length; ii++) {{
                        if (icjEl.options[ii].value === '否') {{ icjEl.selectedIndex = ii; break; }}
                    }}
                    results.is_citizen_judge = true;
                }}
                results.judg_eff = setSelect('judg_eff', '{_judg_eff}');
                results.rel_court1 = setSelect('rel_court1', '{_rel_court1}');
                results.rel_court2 = setVal('rel_court2', '{_court_name}', true);
                results.judg_dt = setVal('judg_dt', '{_tw_dt}', true);
                results.appellee = setVal('appellee', '{_appellee}', true);

                // 刑期 / 緩刑（刑事案件才有此欄位，非刑事 Page 1 無此 input）
                if ('{_sentence_term}') results.terms = setVal('terms', '{_sentence_term}', false);
                if ('{_reprieve_term}') results.reprieve = setVal('reprieve', '{_reprieve_term}', false);

                // Fill first case number row
                var years = document.getElementsByName('year');
                var codes = document.getElementsByName('relcode');
                var nos = document.getElementsByName('relno');
                if (years.length > 0) years[0].value = '{_court_year}';
                if (codes.length > 0) codes[0].value = '{_court_code}';
                if (nos.length > 0) nos[0].value = '{_court_no}';
                results.case_no = '{_court_year} {_court_code} {_court_no}';

                // Remove disabled from judg_dt (portal may disable it)
                var jdt = document.getElementsByName('judg_dt');
                for (var i = 0; i < jdt.length; i++) {{
                    jdt[i].disabled = false; jdt[i].readOnly = false;
                    jdt[i].removeAttribute('disabled'); jdt[i].removeAttribute('readonly');
                }}

                return results;
                """
                # Phase 0a: Fill casekd cascade FIRST (結案類型)
                # casekd modal open/close 可能觸發 portal JS 重設部分欄位，
                # 所以先做 casekd，再做其他 Page 1 欄位填寫。
                _clcate_path = list((counts or {}).get("closing_clcate_path") or [])
                if _clcate_path:
                    self._fill_casekd_cascade(_clcate_path)
                else:
                    self.log("  ⚠️ No closing_clcate_path provided, skipping casekd cascade")

                # Phase 0b: Fill Page 1 fields AFTER casekd
                res = self.driver.execute_script(js_fill)
                self.log(f"  🔧 Phase 0: Page 1 JS fill -> {res}")

                # Verify Page 1 values (same script context for reliability)
                try:
                    _verify = self.driver.execute_script("""
                        var r = {};
                        var rc1 = document.getElementsByName('rel_court1');
                        r.rel_court1 = rc1.length > 0 ? rc1[0].value : 'NOT_FOUND(' + rc1.length + ')';
                        var rc2 = document.getElementsByName('rel_court2');
                        r.rel_court2 = rc2.length > 0 ? rc2[0].value : 'NOT_FOUND(' + rc2.length + ')';
                        var jd = document.getElementsByName('judg_dt');
                        r.judg_dt = jd.length > 0 ? jd[0].value : 'NOT_FOUND(' + jd.length + ')';
                        var cc = document.getElementById('clcate');
                        r.clcate = cc ? cc.value.substring(0, 40) : 'NOT_FOUND';
                        var cd = document.getElementById('cl_docnm');
                        r.cl_docnm = cd ? cd.value : 'NOT_FOUND';
                        r.url = location.href.substring(0, 60);
                        return r;
                    """)
                    self.log(f"  📊 Phase 0b verify: {_verify}")
                except Exception as e:
                    self.log(f"  ⚠️ Phase 0b verify failed: {e}")

                # Debug: log key Page 1 form values before save
                try:
                    _p1_vals = self.driver.execute_script("""
                        var vals = {};
                        var ids = {
                            'rel_court1':'rel_court1','rel_court2':'rel_court2',
                            'clcate':'clcate','cl_docnm':'cl_docnm',
                            'docnmClcate':'docnmClcate','is_citizen_judge':'is_citizen_judge',
                            'tableName':'tableName'
                        };
                        for (var k in ids) {
                            var el = document.getElementById(ids[k])
                                  || document.getElementsByName(ids[k])[0];
                            if (el) vals[k] = el.value || '';
                        }
                        var byName = ['judg_dt','judg_eff','year','relcode','relno','appellee'];
                        for (var i = 0; i < byName.length; i++) {
                            var els = document.getElementsByName(byName[i]);
                            if (els.length > 0) vals[byName[i]] = els[0].value || '';
                        }
                        return vals;
                    """)
                    self.log(f"  📊 Page 1 form values before save: {_p1_vals}")
                except Exception as e:
                    self.log(f"  ⚠️ Page 1 form debug failed: {e}")

                # ===== Step 1: Save Page 1 via AJAX (XHR) =====
                # Portal 要求 Page 1 必須先存檔才能正常操作 Page 2。
                # 關鍵：用 AJAX 存檔（不離開頁面），保持 session/CSRF 完整，
                # 之後 toPrevious() 的 form submit 才能正常導航到 Page 2。
                # 如果用 form submit 存檔，回應頁的 session 狀態會失效。
                self.log("  💾 存檔 Page 1 (AJAX) → insertClosedSummaryBasic...")
                try:
                    _ajax_result = self.driver.execute_async_script("""
                        var callback = arguments[arguments.length - 1];
                        var f = document.forms[0];
                        var fd = new FormData(f);
                        fd.set('goNextFlag', 'N');
                        var xhr = new XMLHttpRequest();
                        xhr.open('POST', '/lafcsp/insertClosedSummaryBasic', true);
                        xhr.onload = function() {
                            callback({status: xhr.status, ok: xhr.status === 200});
                        };
                        xhr.onerror = function() {
                            callback({status: -1, ok: false, error: 'xhr_error'});
                        };
                        xhr.timeout = 30000;
                        xhr.ontimeout = function() {
                            callback({status: -2, ok: false, error: 'timeout'});
                        };
                        xhr.send(fd);
                    """)
                    _save_ok = (_ajax_result or {}).get('ok', False)
                    _save_status = (_ajax_result or {}).get('status', -1)
                    self.log(f"  📊 Page 1 AJAX save: HTTP {_save_status}, ok={_save_ok}")
                except Exception as e:
                    self.log(f"  ⚠️ Page 1 AJAX save exception: {e}")
                    _save_ok = False

                if not _save_ok:
                    self.log("  ❌ Page 1 存檔失敗")
                    self._save_page_debug_html("closing_page1_save_failed")
                    return False

                # 頁面仍在 Page 1，toPrevious() 可用
                self.log("  🚀 toPrevious() → Page 2...")
                try:
                    try:
                        self.driver._last_dialog = None
                    except Exception:
                        pass
                    self.driver.execute_script("toPrevious();")
                except Exception as e:
                    self.log(f"  ⚠️ toPrevious() exception: {e}")

                time.sleep(8.0)

                # Handle alert/confirm dialogs
                try:
                    alert = self.driver.switch_to.alert
                    alert_text = (alert.text or "").strip()
                    self.log(f"  ⚠️ Alert: {alert_text}")
                    alert.accept()
                    time.sleep(2.0)
                except NoAlertPresentException:
                    pass
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3909, exc_info=True)

                try:
                    WebDriverWait(self.driver, 15).until(
                        lambda d: d.execute_script("return document.readyState") == "complete"
                    )
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3916, exc_info=True)

                self._switch_to_content_frame_if_any()

                # Wait for Page 2 identifier (meet_times or URL)
                try:
                    WebDriverWait(self.driver, 20).until(
                        lambda d: len(d.find_elements("id", "meet_times")) > 0
                        or "toClosedSummaryLawyer" in d.current_url
                        or "meet_times" in (d.page_source or "")
                    )
                    # Wait for document ready (don't require jQuery — fill code uses vanilla JS)
                    WebDriverWait(self.driver, 15).until(
                        lambda d: d.execute_script("return document.readyState") == "complete"
                    )
                    on_page2 = True
                    self.log("✅ 已進入報結第二頁 (Handling Info)")
                except Exception as e:
                    self.log(f"⚠️ 無法確認是否進入第二頁: {e}")
                    self._save_page_debug_html("closing_page2_wait_failed")
                    if not on_page2:
                        self._save_page_debug_html("closing_page2_open_failed")
                        return False
            else:
                on_page2 = len(self.driver.find_elements("id", "meet_times")) > 0 or "toClosedSummaryLawyer" in self.driver.current_url
        except Exception as e:
            self.log(f"⚠️ 換頁過程發生異常：{e}")
            return False

        if not on_page2:
            try:
                src_now = self.driver.page_source or ""
            except Exception:
                src_now = ""
            on_page2 = len(self.driver.find_elements("id", "meet_times")) > 0 or "meet_times" in src_now
        if not on_page2:
            self.log("❌ 報結第二頁未就緒，停止填寫。")
            self._save_page_debug_html("closing_page2_not_ready")
            return False

        # 2. Wait for page stable
        try:
            WebDriverWait(self.driver, 8).until(lambda d: (d.execute_script("return document.readyState") == "complete"))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3960, exc_info=True)

        # 3. Fill Fields (Page 2) — 全部使用 JS 設值（send_keys 不可靠）
        # meet_times=面談, tel_times=電話討論, inq_times=律見, wc_times=書狀
        # 所有次數欄位 zero-pad 到 2 位數（portal 用 addZero(name,2)）
        _meet = f"{int(counts.get('meeting_count') or 0):02d}"
        _tel = f"{int(counts.get('contact_count') or 0):02d}"
        _inq = f"{int(counts.get('inq_count') or 0):02d}"
        _wc = f"{int(counts.get('document_count') or 0):02d}"
        # med_times: 調解/和解連繫次數 — 有調解成功時至少 1
        _med_raw = int(counts.get('mediation_contact_count') or 0)
        if (counts or {}).get("has_mediation_success") and _med_raw < 1:
            _med_raw = 1
        _med = f"{_med_raw:02d}"
        try:
            _p2_fill = self.driver.execute_script(f"""
                function _setField(id, val) {{
                    var el = document.getElementById(id);
                    if (!el) return null;
                    el.readOnly = false; el.disabled = false;
                    el.removeAttribute('readonly'); el.removeAttribute('disabled');
                    el.value = val;
                    el.dispatchEvent(new Event('change', {{bubbles:true}}));
                    el.dispatchEvent(new Event('input', {{bubbles:true}}));
                    return val;
                }}
                var r = {{}};
                r.meet_times = _setField('meet_times', '{_meet}');
                r.tel_times = _setField('tel_times', '{_tel}');
                r.inq_times = _setField('inq_times', '{_inq}');
                r.wc_times = _setField('wc_times', '{_wc}');

                // Zero-pad to 2 digits (portal expects 2-digit format)
                var ids = ['meet_times','tel_times','inq_times','wc_times'];
                for (var i = 0; i < ids.length; i++) {{
                    var el = document.getElementById(ids[i]);
                    if (el && el.value.length === 1) el.value = '0' + el.value;
                }}

                // Recalculate disc_times = meet + tel + inq
                var disc = parseInt('{_meet}',10) + parseInt('{_tel}',10) + parseInt('{_inq}',10);
                var discStr = disc < 10 ? '0' + disc : '' + disc;
                _setField('disc_times', discStr);
                var discShow = document.getElementById('disc_timesShow');
                if (discShow) discShow.textContent = discStr;
                r.disc_times = discStr;

                // med_times: 與對造因調和解而連繫之次數
                var medVal = '{_med}';
                _setField('med_times', medVal);
                r.med_times = medVal;

                // Also trigger portal's calculateTimes if available
                try {{
                    if (typeof calculateTimes === 'function') {{
                        calculateTimes(document.getElementById('meet_times'));
                    }}
                }} catch(e) {{}}

                return r;
            """)
            self.log(f"  ✍️ Page 2 count fields (JS): {_p2_fill}")
        except Exception as e:
            self.log(f"  ❌ Page 2 count JS fill failed: {e}")

        # ===== 開庭日期列 (Court dates via doAddCourtDt) =====
        # 用 doAddCourtDt() 新增日期列，填入民國日期，網頁自動計算 ap_times。
        # 日期格式：民國年無斜線 7 碼，如 1140217（2025-02-17）
        _court_dates = counts.get("court_dates") or []
        if _court_dates:
            try:
                _added_court = self._add_date_rows(
                    _court_dates, "doAddCourtDt", "courtdtAry",
                    "calculateAp_times", "開庭"
                )
                self.log(f"  📅 開庭日期：新增 {_added_court} 筆")
            except Exception as e:
                self.log(f"  ❌ 新增開庭日期失敗：{e}")
        elif int(counts.get("court_count", 0) or 0) > 0:
            # 有次數但沒有日期 → fallback 直接設數值
            v = f"{int(counts['court_count']):02d}"
            try:
                self.driver.execute_script(f"""
                    var la = document.getElementById('lawyerap_times');
                    if (la) {{ la.value = '{v}'; la.dispatchEvent(new Event('change', {{bubbles:true}})); }}
                    var ia = document.getElementById('isap_times');
                    if (ia && (!ia.value || ia.value === '0')) ia.value = '00';
                    var h = document.getElementById('ap_times');
                    if (h) h.value = '{v}';
                    var s = document.getElementById('ap_timesShow');
                    if (s) {{ var n = s.querySelector('strong.num') || s; n.textContent = '{v}'; }}
                    if (typeof calculateTimes === 'function' && la) calculateTimes(la);
                """)
                self.log(f"  🔧 (JS fallback) court_count -> ap_times={v}")
            except Exception as e:
                self.log(f"  ❌ JS 設定 court_count 失敗：{e}")

        # isap_times 預設 '00'
        try:
            self.driver.execute_script("""
                var ia = document.getElementById('isap_times');
                if (ia && (!ia.value || ia.value === '0')) ia.value = '00';
            """)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4064, exc_info=True)

        # ===== 閱卷日期列 (Review dates via doAddViewSheetDt) =====
        _review_dates = counts.get("review_dates") or []
        if _review_dates:
            try:
                _added_review = self._add_date_rows(
                    _review_dates, "doAddViewSheetDt", "viewsheetdtAry",
                    "calculateViewsheet_times", "閱卷"
                )
                self.log(f"  📅 閱卷日期：新增 {_added_review} 筆")
            except Exception as e:
                self.log(f"  ❌ 新增閱卷日期失敗：{e}")
        elif int(counts.get("review_count", 0) or 0) > 0:
            # 有次數但沒有日期 → fallback 直接設數值
            v = f"{int(counts['review_count']):02d}"
            try:
                self.driver.execute_script(f"""
                    var h = document.getElementById('viewsheet_times');
                    if (h) h.value = '{v}';
                    var s = document.getElementById('viewsheet_timesShow');
                    if (s) {{ var n = s.querySelector('strong.num') || s; n.textContent = '{v}'; }}
                """)
                self.log(f"  🔧 (JS fallback) review_count -> viewsheet_times={v}")
            except Exception as e:
                self.log(f"  ❌ JS 設定 review_count 失敗：{e}")

        # 3b. Select fields: 費用是否已請領完畢 / 是否達成調解和解
        # These are <select> elements that may be disabled — enable via JS first.
        try:
            # islgfee: 費用是否已請領完畢 → 一律選「是」
            self.driver.execute_script("""
                var el = document.getElementById('islgfee') || document.getElementsByName('islgfee')[0];
                if (el) { el.disabled = false; el.value = '是'; el.dispatchEvent(new Event('change', {bubbles: true})); }
            """)
            self.log("  ✍️ islgfee (費用是否已請領完畢) = 是")
        except Exception as e:
            self.log(f"  ⚠️ 設定 islgfee 失敗：{e}")

        try:
            # is_med_by_ly: 是否因扶助律師之協助而達成調解/和解
            # 依 counts['has_mediation_success'] 判斷，預設「否」
            med_val = "是" if (counts or {}).get("has_mediation_success") else "否"
            self.driver.execute_script(f"""
                var el = document.getElementById('is_med_by_ly') || document.getElementsByName('is_med_by_ly')[0];
                if (el) {{ el.disabled = false; el.value = '{med_val}'; el.dispatchEvent(new Event('change', {{bubbles: true}})); }}
            """)
            self.log(f"  ✍️ is_med_by_ly (是否達成調解/和解) = {med_val}")
        except Exception as e:
            self.log(f"  ⚠️ 設定 is_med_by_ly 失敗：{e}")

        # 3b-2. 酌增酬金 checkbox — 調解/和解成功時勾選「0002-0038」
        if (counts or {}).get("has_mediation_success"):
            try:
                checked = self.driver.execute_script("""
                    var cbs = document.querySelectorAll('input[name="opn_rsn1"]');
                    for (var i = 0; i < cbs.length; i++) {
                        cbs[i].disabled = false;
                        if (cbs[i].value === '0002-0038') {
                            cbs[i].checked = true;
                            cbs[i].dispatchEvent(new Event('change', {bubbles: true}));
                            if (cbs[i].onclick) cbs[i].onclick();
                            return true;
                        }
                    }
                    return false;
                """)
                if checked:
                    self.log("  ✍️ 酌增酬金：已勾選「因扶助律師之協助而使雙方達成調解或和解」")
                else:
                    self.log("  ⚠️ 酌增酬金：找不到 opn_rsn1=0002-0038 checkbox")
            except Exception as e:
                self.log(f"  ⚠️ 設定酌增酬金 checkbox 失敗：{e}")

        # 3c. Set hidden fields on Page 2 that were set on Page 1
        # but not carried over (server renders Page 2 with empty values).
        # 伺服器要求這些欄位非空，否則會「存檔錯誤」。
        try:
            self.driver.execute_script("""
                // is_citizen_judge: 強制設 '否'（不管原值為何）
                var icj = document.getElementById('is_citizen_judge');
                if (icj) icj.value = '否';
                // isnativeculture: 是否為原住民族文化 → '否'
                var inc = document.getElementById('isnativeculture');
                if (inc) inc.value = '否';
                // 修復重複的 tempSaveFlag hidden field — 設定所有同名欄位
                var tsfs = document.getElementsByName('tempSaveFlag');
                for (var i = 0; i < tsfs.length; i++) tsfs[i].value = '';
            """)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4154, exc_info=True)

        # 3d. totalworkhours: 律師實際辦理總工時（伺服器可能不接受空值）
        try:
            _meet_i = int(counts.get("meeting_count") or 0)
            _tel_i = int(counts.get("contact_count") or 0)
            _inq_i = int(counts.get("inq_count") or 0)
            _court_i = int(counts.get("court_count") or 0)
            _review_i = int(counts.get("review_count") or 0)
            _wc_i = int(counts.get("document_count") or 0)
            # 合理工時估算：每次活動平均 1.5 小時，最少 1 小時
            _total_hrs = max(1, int((_meet_i + _tel_i + _inq_i + _court_i * 2 + _review_i + _wc_i) * 1.5))
            self.driver.execute_script(f"""
                var el = document.getElementById('totalworkhours');
                if (el && !el.value) el.value = '{_total_hrs}';
            """)
            self.log(f"  🔧 totalworkhours = {_total_hrs}")
        except Exception as e:
            self.log(f"  ⚠️ 設定 totalworkhours 失敗：{e}")

        # 4. noarrivereason: required when any count is 0
        # This is the "扶助律師特別說明" textarea
        self.fill_noarrivereason_textarea(counts=counts or {}, zero_reasons=zero_reasons or {})

        return True

    def fill_noarrivereason_textarea(self, counts, zero_reasons=None):
        """填 portal noarrivereason textarea（共用 helper，closing 與 progress 均呼叫）。

        Args:
            counts: 必須含 noarrivereason 鍵（已是最終文案）或留空讓 zero_reasons 拼接
            zero_reasons: 各零次數欄位的解釋 dict（fallback）

        Returns:
            True if textarea found and filled (or no need), False if fill failed.
        """
        _noarrive_reason = str((counts or {}).get("noarrivereason") or "").strip()
        if not _noarrive_reason:
            # Build from zero_reasons
            parts = [str(v).strip() for v in (zero_reasons or {}).values() if str(v).strip()]
            _noarrive_reason = "; ".join(parts) if parts else ""
        if not _noarrive_reason:
            return True  # 沒有零值或已說明，無需填
        try:
            ta = self.driver.find_elements("id", "noarrivereason")
            if ta:
                ta[0].clear()
                ta[0].send_keys(_noarrive_reason)
                self.log(f"  ✍️ noarrivereason = {_noarrive_reason[:60]}...")
                return True
            else:
                self.log("  ⚠️ 找不到 noarrivereason textarea")
                return False
        except Exception as e:
            self.log(f"  ❌ 填寫 noarrivereason 失敗: {e}")
            return False

    def fill_workflow_closing_summary(
        self,
        workflow: str,
        counts: Dict[str, Any],
        zero_reasons: Dict[str, str] = None,
    ) -> bool:
        """
        從撤回/疑義表單內，導航至結案資料彙整（ClosedSummary）並填寫辦理情形。

        Prerequisites: 必須已在撤回 (toPB) 或疑義 (toRSM) 表單頁。
        流程：
          1) 選擇 lawy_status = P（辦理中）
          2) 呼叫 doFinish() → 導到 ClosedSummaryBasic（Page 1）
          3) 填寫 Page 1 → 導到 Page 2（辦理情形）
          4) 填寫 Page 2 各次數/日期
          5) doTempSave() 暫存結案資料
        """
        if not self.driver:
            return False

        wf = (workflow or "").strip().lower()
        if wf not in ("withdrawal", "inquiry"):
            self.log(f"⚠️ fill_workflow_closing_summary 僅支援 withdrawal/inquiry，收到 {workflow}")
            return False

        # 1. 選擇 lawy_status = P（辦理中）
        self.log("📋 設定扶助律師辦理情形 = 辦理中 (P)")
        try:
            self.driver.execute_script("""
                var radio = document.getElementById('lawyerStatusP');
                if (radio) { radio.checked = true; radio.click(); }
            """)
            time.sleep(0.5)
        except Exception as e:
            self.log(f"  ⚠️ 設定 lawy_status=P 失敗：{e}")

        # 2. 呼叫 doFinish() 進入結案資料彙整
        self.log("🔗 呼叫 doFinish() → 結案資料彙整...")
        try:
            try:
                self.driver._last_dialog = None
            except Exception:
                pass
            self.driver.execute_script("doFinish();")
        except Exception as e:
            self.log(f"  ❌ doFinish() 呼叫失敗：{e}")
            return False

        time.sleep(6.0)

        # Handle alert
        try:
            alert = self.driver.switch_to.alert
            alert_text = (alert.text or "").strip()
            self.log(f"  ⚠️ Alert: {alert_text}")
            alert.accept()
            time.sleep(2.0)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4249, exc_info=True)

        # Wait for page load
        try:
            WebDriverWait(self.driver, 15).until(
                lambda d: d.execute_script("return document.readyState") == "complete"
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4257, exc_info=True)

        self._switch_to_content_frame_if_any()

        # Check if we are on ClosedSummaryBasic (Page 1) or ClosedSummaryLawyer (Page 2)
        cur_url = self.driver.current_url or ""
        self.log(f"  📍 doFinish 後 URL: {cur_url[:80]}")

        # ClosedSummaryBasic = Page 1, toClosedSummaryLawyer = Page 2
        is_cs_page1 = "ClosedSummary" in cur_url or "toCS" in cur_url
        has_casekd = len(self.driver.find_elements("name", "casekd")) > 0
        has_meet_times = len(self.driver.find_elements("id", "meet_times")) > 0

        if has_meet_times:
            self.log("✅ 已直接進入辦理情形頁（Page 2）")
        elif is_cs_page1 or has_casekd:
            self.log("📄 已進入結案資料彙整第一頁（Page 1），使用 fill_closing_report 導航...")
            # fill_closing_report handles Page 1 → Page 2 navigation
        else:
            # 可能停在原頁或登入逾時
            self.log(f"⚠️ doFinish 後未偵測到 ClosedSummary 頁面")
            self._save_page_debug_html(f"{wf}_doFinish_landing")
            # 嘗試繼續 — 也許頁面需要更多時間
            time.sleep(3.0)

        # 3. 用 fill_closing_report 填寫 Page 1 → Page 2
        if not self.fill_closing_report(counts=counts or {}, zero_reasons=zero_reasons or {}):
            self.log("❌ 結案資料彙整填寫失敗")
            self._save_page_debug_html(f"{wf}_cs_fill_failed")
            return False

        self._save_page_debug_html(f"{wf}_cs_page2_filled")

        # 4. TempSave (doTempSave)
        self.log("  💾 暫存結案資料彙整 (doTempSave)...")
        save_attempted = False
        try:
            has_fn = self.driver.execute_script("return typeof doTempSave === 'function'")
            if has_fn:
                self.driver.execute_script("""
                    var tsfs = document.getElementsByName('tempSaveFlag');
                    for (var i = 0; i < tsfs.length; i++) tsfs[i].value = 'Y';
                    var overs = document.getElementsByName('over');
                    for (var i = 0; i < overs.length; i++) overs[i].value = '';
                """)
                try:
                    self.driver._last_dialog = None
                except Exception:
                    pass
                self.driver.execute_script("doTempSave();")
                save_attempted = True
                self.log("  💾 doTempSave() called")
            else:
                try:
                    self.driver._last_dialog = None
                except Exception:
                    pass
                save_attempted = self._click_button_by_text(["存檔", "暫存", "保存", "儲存"])
        except Exception as e:
            self.log(f"  ⚠️ doTempSave 失敗：{e}")

        if save_attempted:
            time.sleep(3.0)
            # Handle post-save alert
            try:
                alert = self.driver.switch_to.alert
                self.log(f"  ℹ️ 暫存後 Alert: {alert.text}")
                alert.accept()
                time.sleep(1.0)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4319, exc_info=True)
            self.log("✅ 結案資料彙整暫存完成")
        else:
            self.log("⚠️ 結案資料彙整暫存未執行（找不到暫存按鈕）")

        return True

    def save_closing_report_draft(
        self,
        laf_case_number: str,
        counts: Dict[str, Any],
        zero_reasons: Dict[str, str] = None,
        upload_files: List[str] = None,
    ) -> bool:
        """
        報結暫存：進入 toCR → 換頁至 toClosedSummaryLawyer → 填寫 → doTempSave()。
        """
        if not self.open_closing_report_page(laf_case_number):
            return False

        try:
            src_probe = (self.driver.page_source or "")
        except Exception:
            src_probe = ""
        if ("目前已有回報資料正在處理中" in src_probe) or ("已有回報資料正在處理中" in src_probe):
            self.log("ℹ️ 報結：系統顯示已有回報資料正在處理中，略過本次存檔。")
            self.last_upload_result = {
                "ok": True,
                "workflow": "closing",
                "requested": 0,
                "uploaded": 0,
                "failed": 0,
                "files": [],
                "skipped_existing": True,
            }
            self._save_page_debug_html("closing_already_in_progress")
            return True
        
        # Capture Page 1 HTML before navigation (Debug)
        self._save_page_debug_html("closing_page1")

        # Upload supporting files on Page 1 BEFORE navigating to Page 2.
        # The upload panel (linkUpload('CR_CS')) only exists on Page 1.
        # After toPrevious() navigates to Page 2, upload is no longer available.
        up_files = list(upload_files or [])
        if up_files:
            self.log(f"📎 報結附件上傳（第一頁）：共 {len(up_files)} 份 PDF")
            up_res = self._upload_supporting_files(up_files, workflow="closing")
            self._save_page_debug_html("closing_page1_uploaded")
            if not up_res.get("ok"):
                self.log("⚠️ 報結附件上傳失敗，繼續暫存（附件可稍後補上傳）")

        if not self.fill_closing_report(counts=counts or {}, zero_reasons=zero_reasons or {}):
            self.log("❌ 報結第二頁填寫前置失敗")
            self._save_page_debug_html("closing_fill_failed")
            return False

        # Capture Page 2 HTML after fill (Debug)
        self._save_page_debug_html("closing_page2_filled")

        # 嘗試暫存 via doTempSave()
        # doTempSave() calls checkData() (validation) then form.submit()
        # Messages appear in Bootstrap modals (#dialog-msg #msgPart), not JS alerts

        # Debug: log key form values before submission
        try:
            _form_vals = self.driver.execute_script("""
                var f = document.forms[0]; if (!f) return {_no_form: true};
                var vals = {};
                var keys = ['applyno','tableName','tempSaveFlag','over','fromPage',
                            'meet_times','tel_times','inq_times','disc_times',
                            'viewsheet_times','ap_times','lawyerap_times','isap_times',
                            'wc_times','med_times','totalworkhours','islgfee',
                            'is_med_by_ly','is_oth_lyer','is_citizen_judge',
                            'isnativeculture','noarrivereason'];
                for (var i = 0; i < keys.length; i++) {
                    var el = f.elements[keys[i]];
                    if (el) vals[keys[i]] = (el.length !== undefined && el.tagName === undefined)
                        ? el[0].value : el.value;
                }
                vals._action = f.action;
                return vals;
            """)
            self.log(f"  📊 Pre-submit form values: {_form_vals}")
        except Exception as e:
            self.log(f"  ⚠️ Form value debug failed: {e}")

        self.log("  💾 Calling doTempSave()...")
        save_attempted = False
        try:
            has_fn = self.driver.execute_script("return typeof doTempSave === 'function'")
            if has_fn:
                # 修復重複 tempSaveFlag：portal HTML 有兩個同名 hidden field，
                # doTempSave() 只設第一個為 'Y'，第二個留空。
                # 先用 JS 將所有同名 tempSaveFlag 設為 'Y'，再呼叫 doTempSave。
                self.driver.execute_script("""
                    var tsfs = document.getElementsByName('tempSaveFlag');
                    for (var i = 0; i < tsfs.length; i++) tsfs[i].value = 'Y';
                    var overs = document.getElementsByName('over');
                    for (var i = 0; i < overs.length; i++) overs[i].value = '';
                    var fps = document.getElementsByName('fromPage');
                    for (var i = 0; i < fps.length; i++) fps[i].value = '';
                """)
                try:
                    self.driver._last_dialog = None
                except Exception:
                    pass
                self.driver.execute_script("doTempSave();")
                save_attempted = True
                self.log("  💾 doTempSave() called (with tempSaveFlag fix)")
            else:
                # Fallback: try save_btn click
                try:
                    btn = self.driver.find_element("id", "save_btn")
                    if btn and btn.is_displayed():
                        try:
                            self.driver._last_dialog = None
                        except Exception:
                            pass
                        btn.click()
                        save_attempted = True
                        self.log("  💾 save_btn clicked")
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4434, exc_info=True)
                if not save_attempted:
                    try:
                        self.driver._last_dialog = None
                    except Exception:
                        pass
                    save_attempted = self._click_button_by_text(["存檔", "暫存", "保存", "儲存"])
        except Exception as e:
            self.log(f"  ⚠️ doTempSave exception: {e}")

        if not save_attempted:
            self.log("❌ 找不到暫存方法")
            self._save_page_debug_html("closing_save_failed")
            return False

        # Wait for form submission and server response
        time.sleep(4.0)

        # Check for validation error in Bootstrap modal (checkData failed)
        # 法扶入口網存檔流程可能出現多個 Modal：
        #   1. 提醒 Modal（如「未閱卷…請填寫原因」）→ 需關閉後繼續等結果
        #   2. 成功 Modal（「存檔成功」）→ 直接 return True
        #   3. 失敗 Modal（「錯誤」）→ 直接 return False
        _modal_rounds = 0
        while _modal_rounds < 3:
            _modal_rounds += 1
            try:
                modal_msg = self.driver.execute_script("""
                    var el = document.getElementById('msgPart');
                    return el ? el.textContent || el.innerText : '';
                """) or ""
                if modal_msg.strip():
                    self.log(f"  ℹ️ Modal message: {modal_msg.strip()[:100]}")
                    if "存檔成功" in modal_msg or "暫存成功" in modal_msg or "儲存成功" in modal_msg:
                        self.log("✅ 報結存檔完成（Modal 確認成功）")
                        self._save_page_debug_html("closing_save_success")
                        return True
                    elif "錯誤" in modal_msg or "失敗" in modal_msg:
                        self.log(f"❌ 報結存檔失敗: {modal_msg.strip()[:200]}")
                        self._save_page_debug_html("closing_save_error")
                        return False
                    else:
                        # 提醒 Modal（如「未閱卷…請填原因」）→ 點「繼續」按鈕提交表單
                        # 法扶 checkData() 在偵測到零值欄位且 noarrivereason 為空時，
                        # 會 showAlertMsg + 顯示 continueSubmitBtn / cancleBtn。
                        # 點 continueSubmitBtn 會重新呼叫 doTempSave()。
                        self.log("  ℹ️ 提醒 Modal，點擊繼續按鈕...")
                        self.driver.execute_script("""
                            var cont = document.getElementById('continueSubmitBtn');
                            if (cont && cont.offsetParent !== null) {
                                cont.click();
                            } else {
                                // fallback: close modal and retry save
                                if (typeof closeModal1 === 'function') { closeModal1(); }
                                var cb = document.getElementById('closeBtn');
                                if (cb && cb.offsetParent !== null) { cb.click(); }
                            }
                        """)
                        time.sleep(4.0)
                        continue
                else:
                    break
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4465, exc_info=True)
                break

        # Check page source for success message (after form submit + response)
        try:
            src = (self.driver.page_source or "")
        except Exception:
            src = ""

        if any(k in src for k in ["存檔成功", "暫存成功"]):
            self.log("✅ 報結存檔完成（偵測到成功訊息）")
            self._save_page_debug_html("closing_save_success")
            return True

        # Check URL for redirect to list page
        try:
            cur_url = self.driver.current_url or ""
        except Exception:
            cur_url = ""
        if "updateClosedSummaryLawyer" in cur_url:
            # Form was submitted — check if response contains success
            if "存檔成功" in src or "暫存成功" in src:
                self.log("✅ 報結存檔完成（更新回應頁確認成功）")
                return True

        # Also check for JS alert (just in case)
        try:
            alert = self.driver.switch_to.alert
            alert_text = (alert.text or "").strip()
            self.log(f"  ℹ️ Alert: {alert_text}")
            alert.accept()
            if any(k in alert_text for k in ["存檔成功", "暫存成功", "儲存成功"]):
                self.log("✅ 報結存檔完成（Alert 確認成功）")
                return True
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4500, exc_info=True)

        # URL 跳回清單頁不代表存檔成功（可能是表單驗證失敗後的導向），
        # 必須透過 query_closing_status() 確認資料庫實際有紀錄。
        if "toClosedReport" in cur_url:
            self.log("  ℹ️ 已返回結案清單頁，但未偵測到明確成功訊息，進行資料庫驗證...")
            try:
                laf_no = laf_case_number
                status = self.query_closing_status(laf_no)
                closing_status = (status.get("closing") or {}).get("status", "")
                if closing_status in ("暫存", "待轉入", "已轉入"):
                    self.log(f"✅ 報結存檔完成（資料庫驗證：{closing_status}）")
                    self._save_page_debug_html("closing_save_verified")
                    return True
                else:
                    self.log(f"❌ 報結存檔失敗（資料庫查無有效紀錄，狀態：'{closing_status}'）")
                    self._save_page_debug_html("closing_save_db_mismatch")
                    return False
            except Exception as e:
                self.log(f"⚠️ 報結資料庫驗證異常: {e}")
                self._save_page_debug_html("closing_save_verify_error")
                return False

        self.log("❌ 報結存檔結果不明確（未偵測到成功訊息或跳轉）")
        self._save_page_debug_html("closing_save_unclear")
        return False

    def final_submit_closing_report(self, laf_case_number: str) -> bool:
        """
        報結送出：進入 toCR → doFinalSave('toCR') 或點擊送出。
        """
        # Safety policy: scheduled runs must never finalize-submit to the real portal.
        # Allow only when explicitly enabled by admin (out-of-band) by turning draft-only off.
        if os.environ.get("MAGI_LAF_DRAFT_ONLY", "1") == "1":
            self.log("🔒 安全政策：MAGI_LAF_DRAFT_ONLY=1，禁止執行『送出』。本次僅允許暫存。")
            return False
        if not self.open_closing_report_page(laf_case_number):
            return False

        clicked = False
        try:
            clicked = bool(self.driver.execute_script("if (typeof doFinalSave === 'function') { doFinalSave('toCR'); return true; } return false;"))
        except Exception:
            clicked = False
        if not clicked:
            clicked = self._click_button_by_text(["送出", "提交", "確定送出"])
        if not clicked:
            self.log("❌ 找不到『送出』按鈕")
            return False

        time.sleep(2.0)
        try:
            src = (self.driver.page_source or "")
        except Exception:
            src = ""
        if any(k in src for k in ["送出成功", "成功", "完成"]):
            self.log("✅ 報結送出完成（偵測到成功訊息）")
            return True


    # ==============================================================================
    # 通用 workflow（開辦/二階段/疑義/撤回）自動化：僅暫存，不送出
    # ==============================================================================

    def _workflow_meta(self, workflow: str) -> Dict[str, Any]:
        wf = (workflow or "").strip().lower()
        table = {
            "go_live": {
                "name": "開辦",
                "url_path": "/lafcsp/toNotOpenedCase",
                "apply_selectors": ["#toNotOpenedCase_applyno", "input[name='applyno']", "#applyno"],
                "name_selectors": ["#applynm", "#toNotOpenedCase_applynm", "input[name='applynm']"],
                "search_js": ["doSearch('toNotOpenedCase')", "showList()"],
                "report_onclick": ["showNotOpenedDialog(", "goReply(", "toReport("],
                "expected_token": "toNotOpenedCase",
                # 注意：開辦 Modal 中只有「確定」（送出），沒有暫存按鈕。因此這裡移除「確定」，避免自動化誤點擊送出。
                # 將改為「僅填寫不送出」。
                "list_url": f"{self.base_url}/lafcsp/toGoLiveQuery",
                "query_funcs": ["toGoLiveQuery", "toGoLive"],
                # doUpdate() 是「確定」送出函數，draft 模式絕不能呼叫！
                # go_live modal 沒有暫存按鈕，draft 只填欄位+上傳+截圖。
                "draft_js": [],
                "draft_buttons": ["存檔", "暫存", "保存", "儲存"],
                "submit_buttons": ["確定", "送出", "提交"],
                "submit_js": ["doUpdate()"],
            },
            # transcript/review removed (moved to judicial_automation_v2.py)
            "condition": {
                "name": "二階段",
                "url_path": "/lafcsp/toCndQuery",
                "apply_selectors": ["#toCndQuery_applyno", "#cnd_applyno", "input[name='applyno']", "#applyno"],
                "name_selectors": ["#applynm", "#toCndQuery_applynm", "#cnd_applynm", "input[name='applynm']"],
                "search_js": ["doSearch('toCndQuery')", "searchCndCases()", "showList()"],
                "report_onclick": ["showCndDetail(", "toReport("],
                "expected_token": "toCnd",
                "draft_js": ["doSave('toCnd')", "doSave('toCND')", "doSave()"],
                "draft_buttons": ["存檔", "暫存", "保存", "儲存"],
            },
            "inquiry": {
                "name": "疑義",
                "url_path": "/lafcsp/toReqSubj1Query",
                "apply_selectors": ["#toReqSubj1Query_applyno", "input[name='applyno']", "#applyno"],
                "name_selectors": ["#applynm", "#toReqSubj1Query_applynm", "input[name='applynm']"],
                "search_js": ["doSearch('toReqSubj1Query')", "showList()"],
                "report_onclick": ["toReport("],
                "expected_token": "toRSM",
                "draft_js": ["doSave('toRSM')", "doSave('toReqSubj1')", "doSave()"],
                "draft_buttons": ["存檔", "暫存", "保存", "儲存"],
            },
            "fee": {
                "name": "費用",
                "url_path": "/lafcsp/toLGFEEQuery",
                "apply_selectors": ["#toLGFEEQuery_applyno", "input[name='applyno']", "#applyno"],
                "name_selectors": ["#applynm", "#toLGFEEQuery_applynm", "input[name='applynm']"],
                "search_js": ["doSearch('toLGFEEQuery')", "showList()"],
                "report_onclick": ["toReport("],
                "expected_token": "toLGFEE",
                "draft_js": ["doSave('toLgfee')", "doSave('toLGFEE')", "doSave()"],
                "draft_buttons": ["存檔", "暫存", "保存", "儲存"],
            },
            "withdrawal": {
                "name": "撤回",
                "url_path": "/lafcsp/toPBQuery",
                "apply_selectors": ["#toPBQuery_applyno", "input[name='applyno']", "#applyno"],
                "name_selectors": ["#applynm", "#toPBQuery_applynm", "input[name='applynm']"],
                "search_js": ["doSearch('toPBQuery')", "showList()"],
                "report_onclick": ["toReport("],
                "expected_token": "toPB",
                "draft_js": ["doSave('toPB')", "doSave('toPb')", "doSave()"],
                "draft_buttons": ["存檔", "暫存", "保存", "儲存"],
            },
            "progress": {
                "name": "進度回報",
                "url_path": "/lafcsp/toNotClosedCase",
                "apply_selectors": ["#applyno", "input[name='applyno']"],
                "name_selectors": ["#applynm", "input[name='applynm']"],
                "search_js": ["showList()"],
                "report_onclick": ["goReply("],
                "expected_token": "dialog-notClosedReply",
                # 無 draft 暫存；與 go_live 相同，填完不按送出
                "draft_js": [],
                "draft_buttons": [],
                "submit_js": ["doUpdate()"],
                "submit_buttons": ["確定"],
                "form_fields": {
                    "result_select": "#selResult",
                    "remark_textarea": "#selRemark",
                    "upload_js": "linkUpload('NOT_CLOSE')",
                },
                "requires_confirm_token": True,
                "confirm_token_ttl_sec_env": "MAGI_LAF_PROGRESS_CONFIRM_TTL_SEC",
            },
        }
        return table.get(wf, {})

    def open_workflow_report_page(self, workflow: str, laf_case_number: str = "", client_name: str = "") -> bool:
        """
        通用 workflow 進頁：
        1) 開啟清單頁
        2) 填案號/姓名搜尋
        3) 點「回報/明細」進入表單或 modal
        """
        if not self.driver:
            self.log("❌ 瀏覽器未初始化")
            return False
        meta = self._workflow_meta(workflow)
        if not meta:
            self.log(f"❌ 不支援的 workflow: {workflow}")
            return False

        applyno = (laf_case_number or "").strip()
        reqnm = (client_name or "").strip()
        if not applyno and not reqnm:
            self.log(f"❌ 缺少查詢條件（workflow={workflow} 需要案號或姓名）")
            return False

        url = f"{self.base_url}{meta.get('url_path', '')}"
        self.log(f"🌐 開啟{meta.get('name','workflow')}清單頁: {url}")
        self.driver.get(url)
        time.sleep(1.2)
        self._switch_to_content_frame_if_any()
        if self._is_login_or_timeout_page():
            self.log("⚠️ 偵測到登入逾時，先重新登入再回到 workflow 頁")
            if not self.login():
                self.log("❌ 重新登入失敗")
                return False
            self.driver.get(url)
            time.sleep(1.5)
            self._switch_to_content_frame_if_any()

        if applyno:
            self._set_field_by_selectors(meta.get("apply_selectors", []), applyno)
        if reqnm:
            self._set_field_by_selectors(meta.get("name_selectors", []), reqnm)

        try:
            _sspath = os.path.join(
                self.download_folder or ".",
                f"debug_{workflow}_before_search_{int(time.time())}.png",
            )
            self.driver.save_screenshot(_sspath)
            self.log(f"📸 診斷截圖: {_sspath}")
        except Exception:
            pass

        searched = False
        # 優先點 queryBtn（showList 會走 form submit）
        try:
            searched = bool(
                self.driver.execute_script(
                    """
                    const q=document.querySelector('#queryBtn');
                    if(!q) return false;
                    q.removeAttribute('disabled');
                    q.click();
                    return true;
                    """
                )
            )
        except Exception:
            searched = False
        for js in (meta.get("search_js") or []):
            if searched:
                break
            if not js:
                continue
            try:
                searched = bool(
                    self.driver.execute_script(
                        f"try {{ if (typeof {js.split('(')[0]} === 'function') {{ {js}; return true; }} }} catch (e) {{}} return false;"
                    )
                )
            except Exception:
                searched = False
            if searched:
                break
        if not searched:
            searched = self._click_button_by_text(["開始搜尋", "搜尋", "查詢"])
        if not searched:
            self.log("⚠️ 未成功觸發搜尋，仍嘗試直接尋找回報按鈕")
        self._wait_query_done(timeout_sec=15.0)
        time.sleep(1.0)

        btn = None
        # Strategy 1: JS-based search (most reliable with Playwright) — iterate
        # DOM directly and return the actual <input type="button"> / <a> element.
        # This avoids Playwright XPath engine quirks with @onclick attribute matching.
        try:
            tokens_all = [t for t in (meta.get("report_onclick") or []) if t]
            # IMPORTANT: execute_handle via evaluate can serialize an Element, but
            # to get a usable ElementHandle we must use evaluate_handle (not evaluate).
            pw_page = getattr(self.driver, "_page", None)
            if pw_page is not None and tokens_all:
                frames_to_try = [pw_page] + [f for f in pw_page.frames if f != pw_page.main_frame]
                for _fr in frames_to_try:
                    try:
                        handle = _fr.evaluate_handle(
                            """(args) => {
                                const tokens = args.tokens || [];
                                const applyno = args.applyno || '';
                                const all = document.querySelectorAll('input[type="button"], input[type="submit"], a, button');
                                for (const el of all) {
                                    const oc = el.getAttribute('onclick') || '';
                                    const hf = el.getAttribute('href') || '';
                                    const attrs = oc + ' ' + hf;
                                    const hit_token = tokens.some(t => attrs.includes(t));
                                    const hit_apply = applyno ? attrs.includes(applyno) : true;
                                    if (hit_token && hit_apply) return el;
                                }
                                return null;
                            }""",
                            {"tokens": tokens_all, "applyno": applyno or ""},
                        )
                        as_el = handle.as_element()
                        if as_el is not None:
                            btn = PlaywrightElementWrapper(as_el, self.driver)
                            break
                    except Exception as _e:
                        logging.getLogger(__name__).debug("js-search frame err: %s", _e)
                        continue
        except Exception as _e:
            logging.getLogger(__name__).debug("js-search outer err: %s", _e)

        # Strategy 2: legacy XPath fallback (kept for Selenium path + defense)
        if not btn:
            for token in (meta.get("report_onclick") or []):
                if not token:
                    continue
                try:
                    if applyno:
                        xp = (
                            f"//*[( @onclick and contains(@onclick, {json.dumps(token)}) and contains(@onclick, {json.dumps(applyno)}) ) "
                            f"or ( @href and contains(@href, {json.dumps(token)}) and contains(@href, {json.dumps(applyno)}) )]"
                        )
                        els = self.driver.find_elements(By.XPATH, xp)
                        if els:
                            btn = els[0]
                    if not btn:
                        btn = self._find_clickable_by_onclick(token)
                except Exception:
                    btn = None
                if btn:
                    break
        if (not btn) and reqnm:
            btn = self._find_report_button_for_person(reqnm, meta.get("report_onclick") or [])
        if not btn:
            # text fallback（避免點到導覽列）
            text_btn = self._find_report_button_for_person(reqnm or applyno, ["toReport(", "goReply(", "showCndDetail("])
            if text_btn:
                btn = text_btn
        if not btn:
            self.log(f"❌ 找不到{meta.get('name','workflow')}的『回報』按鈕")
            self._debug_log_clickables(limit=16)
            return False

        try:
            self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4753, exc_info=True)

        # Record pre-click URL so we can detect navigation properly
        try:
            _pre_click_url = self.driver.current_url or ""
        except Exception:
            _pre_click_url = ""

        # LAF 回報 button is typically <input type="button" onclick="javascript:toReport(...)">.
        # Playwright's ElementHandle.click() dispatches a synthetic click which
        # does NOT fire legacy onclick="javascript:..." handlers reliably. We MUST
        # extract the onclick attribute and execute it directly.
        # LAF 回報 button is typically <input type="button" onclick="javascript:toReport(...)">.
        # Playwright's ElementHandle.click() dispatches a synthetic click which
        # does NOT fire legacy onclick="javascript:..." handlers reliably. Strategy:
        # extract the onclick attribute and execute it directly via evaluate.
        _onclick_js = ""
        try:
            _btn_info = self.driver.execute_script(
                "return {onclick: arguments[0].getAttribute('onclick') || '',"
                " href: arguments[0].getAttribute('href') || ''};",
                btn,
            ) or {}
            _onclick_js = (_btn_info.get("onclick") or _btn_info.get("href") or "").strip()
            if _onclick_js.lower().startswith("javascript:"):
                _onclick_js = _onclick_js[11:].strip()
        except Exception:
            _onclick_js = ""

        _clicked = False
        # Strategy A: execute onclick JS directly (most reliable for legacy onclick handlers)
        if _onclick_js and ("(" in _onclick_js):
            try:
                self.driver.execute_script(_onclick_js)
                _clicked = True
            except Exception:
                pass
        # Strategy B: invoke element.click() via JS (triggers native click with onclick)
        if not _clicked:
            try:
                self.driver.execute_script("arguments[0].click();", btn)
                _clicked = True
            except Exception:
                pass
        # Strategy C: Playwright ElementHandle.click() (unreliable for legacy onclick)
        if not _clicked:
            try:
                btn.click()
            except Exception:
                pass

        token = (meta.get("expected_token") or "").strip()
        if token:
            # Wait for ACTUAL navigation/modal: either URL changed OR a fee-form
            # specific element appears (input[name='applynm'] without 'Query' prefix,
            # or upload panel elements). page_source match alone is unreliable because
            # list pages often embed the expected token in onclick attrs.
            _deadline = time.time() + 20.0
            _detail_markers = [
                "input[name='applynm']", "#applynm",
                "#uploadBtn", "#uploadBtnY", "#uploadBtnN",
                "input[type='file']",
                "select[name='cl_docnm2']", "#cl_docnm2",
            ]
            _entered = False
            while time.time() < _deadline:
                try:
                    cur_url = self.driver.current_url or ""
                except Exception:
                    cur_url = ""
                # URL actually changed (away from list/query page)
                if cur_url and cur_url != _pre_click_url and token in cur_url:
                    _entered = True
                    break
                # Detail-page markers appeared on current page
                try:
                    for _msel in _detail_markers:
                        try:
                            if self.driver.find_elements(By.CSS_SELECTOR, _msel):
                                _entered = True
                                break
                        except Exception:
                            continue
                    if _entered:
                        break
                except Exception:
                    pass
                time.sleep(0.3)
            if not _entered:
                self.log(f"⚠️ 未偵測到 {token} 入口（可能是 modal），改以欄位偵測繼續")

        self._switch_to_content_frame_if_any()
        self.log(f"✅ 已進入{meta.get('name','workflow')}回報頁")
        return True

    def fill_workflow_fields(self, workflow: str, fields: Dict[str, Any]) -> bool:
        if not self.driver:
            return False
        meta = self._workflow_meta(workflow)
        if not meta:
            return False
        data = dict(fields or {})

        def _set_any(sel_list: List[str], val: Any, label_kws: List[str] = None, kind: str = "input") -> bool:
            if val is None or str(val).strip() == "":
                return False
            if self._set_field_by_selectors(sel_list, val):
                return True
            if label_kws:
                return self._set_field_by_label(label_kws, str(val), kind=kind)
            return False

        wf = (workflow or "").strip().lower()
        if wf == "withdrawal":
            # 撤回 — 實際 HTML 欄位名稱：pb_rec_dt（非 pb_date）、rsn_desc（非 pb_desc）
            # 注意：無 pb_reason SELECT，也無 pb_proc_desc textarea（只有一個 rsn_desc）
            _set_any(
                ["#pb_rec_dt", "input[name='pb_rec_dt']", "#pb_date", "input[name='pb_date']"],
                data.get("pb_date") or data.get("pb_rec_dt"),
                ["撤回日期", "收件日期"],
                kind="input",
            )
            _set_any(
                ["#rsn_desc", "textarea[name='rsn_desc']", "#pb_desc", "textarea[name='pb_desc']", "textarea[name='remark']"],
                data.get("desc") or data.get("reason_text"),
                ["說明", "備註", "原因", "案件辦理情形"],
                kind="textarea",
            )
            # Portal actual radio name is "lawy_status" (confirmed from HTML snapshot 20260222)
            _pb_status = str(data.get("pb_lawyer_status") or data.get("lawy_status") or "N")
            if not self._set_radio_value("lawy_status", _pb_status):
                self._set_radio_value("pb_lawyer_status", _pb_status)

        elif wf == "inquiry":
            # 疑義 — 實際 HTML 欄位名稱是通用的 reqsubj1/reqsubj2/reqdesc（無 rsm_ 前綴）
            _set_any(
                ["#reqsubj1", "select[name='reqsubj1']", "#rsm_reqsubj1", "select[name='rsm_reqsubj1']"],
                data.get("rsm_reqsubj1") or "0001",
                ["主旨"],
                kind="input",
            )

            req2 = data.get("rsm_reqsubj2")
            if not req2:
                req2 = "0117"
            _set_any(
                ["select[name='reqsubj2']", "#reqsubj2", "#rsm_reqsubj2", "select[name='rsm_reqsubj2']"],
                req2,
                ["疑義類型", "主旨"],
                kind="input",
            )

            desc = data.get("desc") or data.get("reason_text") or "（律師未提供詳細說明，請補填）"
            _set_any(
                ["#reqdesc", "textarea[name='reqdesc']", "#rsm_desc", "textarea[name='rsm_desc']", "textarea[name='desc']"],
                desc,
                ["問題", "說明", "備註"],
                kind="textarea",
            )

            # 律師意見 (comments) — separate textarea in inquiry form (confirmed from HTML snapshot 20260219)
            _comments = data.get("comments") or data.get("rsm_comments")
            if _comments:
                _set_any(
                    ["#comments", "textarea[name='comments']", "#rsm_comments", "textarea[name='rsm_comments']"],
                    _comments,
                    ["意見", "律師意見"],
                    kind="textarea",
                )

            # Portal actual radio name is "lawy_status" (confirmed from HTML snapshot 20260219)
            status = str(data.get("rsm_lawyer_status") or data.get("lawy_status") or "N")
            if not self._set_radio_value("lawy_status", status):
                if not self._set_radio_value("rsm_lawyer_status", status):
                    _set_any(["#rsm_lawyer_status", "select[name='rsm_lawyer_status']"], status)

        elif wf == "condition":
            # 二階段 — 實際 HTML 中 at_ctype 與 conditionrsn 是 readonly INPUT（非 SELECT/TEXTAREA）
            # 先嘗試解除 readonly 後填值，若失敗則用 JS 強制設定
            try:
                self.driver.execute_script("""
                    var el = document.querySelector('#at_ctype, input[name="at_ctype"]');
                    if (el) { el.removeAttribute('readonly'); el.readOnly = false; }
                    var el2 = document.querySelector('#conditionrsn, input[name="conditionrsn"], textarea[name="conditionrsn"]');
                    if (el2) { el2.removeAttribute('readonly'); el2.readOnly = false; }
                """)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4864, exc_info=True)
            _set_any(
                ["#at_ctype", "input[name='at_ctype']", "select[name='at_ctype']"],
                data.get("at_ctype") or data.get("condition_type"),
                ["啟動", "體現法建議"],
                kind="input",
            )
            _set_any(
                ["#conditionrsn", "input[name='conditionrsn']", "textarea[name='conditionrsn']", "textarea[name='condition_reason']"],
                data.get("conditionrsn") or data.get("condition_reason"),
                ["附條件原因", "原因", "說明"],
                kind="textarea",
            )

        elif wf == "fee":
            # 費用 — 實際 HTML 欄位名稱是通用的 reqsubj1/reqsubj2/reqdesc（無 lgfee_ 前綴）
            _set_any(
                ["#reqsubj1", "select[name='reqsubj1']", "#lgfee_reqsubj1", "select[name='lgfee_reqsubj1']"],
                data.get("lgfee_reqsubj1"),
                ["主旨"],
                kind="input",
            )
            _set_any(
                ["select[name='reqsubj2']", "#reqsubj2", "#lgfee_reqsubj2", "select[name='lgfee_reqsubj2']"],
                data.get("lgfee_reqsubj2"),
                ["主旨", "費用"],
                kind="input",
            )
            _set_any(
                ["#reqdesc", "textarea[name='reqdesc']", "#lgfee_desc", "textarea[name='lgfee_desc']", "textarea[name='desc']"],
                data.get("desc") or data.get("reason_text"),
                ["說明", "費用", "備註"],
                kind="textarea",
            )
            # reqsubj3 — third-level select, shown only when reqsubj2=0120 (支付裁判費)
            _subj3 = data.get("lgfee_reqsubj3") or data.get("reqsubj3")
            if _subj3:
                _set_any(
                    ["select[name='reqsubj3']", "#reqsubj3", "#lgfee_reqsubj3", "select[name='lgfee_reqsubj3']"],
                    _subj3,
                    ["主旨", "費用類型"],
                    kind="input",
                )
            # Note: fee form has NO lawy_status radio (confirmed from HTML snapshot 20260219).
            # Attempt gracefully in case portal adds it later.
            _fee_status = data.get("lgfee_lawyer_status") or data.get("lawy_status")
            if _fee_status:
                if not self._set_radio_value("lawy_status", str(_fee_status)):
                    self._set_radio_value("lgfee_lawyer_status", str(_fee_status))

        elif wf == "go_live":
            # 開辦 — selResult 正確，但 remark 的實際 ID 是 #selRemark（非 #noc_remark）
            # doSave 不存在，實際用 doUpdate()
            _set_any(
                ["#selResult", "select[name='selResult']", "#toNotOpenedCase_selResult", "#noc_selResult", "select[name='result']"],
                data.get("sel_result") or data.get("result") or "1",
                ["回報狀態"],
                kind="input",
            )
            _set_any(
                ["#selRemark", "textarea[name='selRemark']", "#noc_remark", "textarea[name='noc_remark']", "textarea[name='remark']"],
                data.get("remark") or data.get("desc"),
                ["說明", "備註"],
                kind="textarea",
            )

        elif wf == "progress":
            # 進度回報 — dialog-notClosedReply modal: selResult + selRemark
            # goReply() JS pre-fills selResult with replyStatus from the row.
            # Only override if the caller explicitly provides a value.
            _sel_result = data.get("sel_result") or data.get("result")
            if _sel_result:
                _set_any(
                    ["#selResult", "select[name='selResult']", "#noc_selResult", "select[name='result']"],
                    _sel_result,
                    ["回報狀態", "進度"],
                    kind="input",
                )
            _set_any(
                ["#selRemark", "textarea[name='selRemark']", "#noc_remark", "textarea[name='remark']"],
                data.get("remark") or data.get("desc"),
                ["說明", "備註", "進度說明"],
                kind="textarea",
            )
            # 偵測 portal modal 中的零次數欄位，自動填 noarrivereason（同 closing 處理）
            _PROGRESS_COUNT_FIELD_LABELS = {
                "meet_times": "會議次數",
                "tel_times": "電話聯繫次數",
                "inq_times": "詢問次數",
                "disc_times": "討論次數",
                "viewsheet_times": "閱卷次數",
                "ap_times": "開庭次數",
                "lawyerap_times": "律師開庭次數",
                "isap_times": "本人到庭次數",
                "wc_times": "書狀次數",
                "med_times": "調解次數",
            }
            try:
                zero_fields_found = self.driver.execute_script("""
                    var fieldIds = arguments[0];
                    var zeros = [];
                    for (var i = 0; i < fieldIds.length; i++) {
                        var id = fieldIds[i];
                        var el = document.getElementById(id);
                        if (el) {
                            var v = (el.value || '').replace(/^0+/, '') || '0';
                            if (v === '0' || v === '' || parseInt(v, 10) === 0) {
                                zeros.push(id);
                            }
                        }
                    }
                    return zeros;
                """, list(_PROGRESS_COUNT_FIELD_LABELS.keys()))
            except Exception:
                zero_fields_found = []
            self.last_zero_fields = [_PROGRESS_COUNT_FIELD_LABELS.get(f, f) for f in (zero_fields_found or [])]
            if self.last_zero_fields:
                # 建立 noarrivereason（若 caller 已提供則優先用；否則用預設模板）
                _provided = str((data or {}).get("noarrivereason") or "").strip()
                if not _provided:
                    _tmpl = str((data or {}).get("auto_zero_reason_template") or "").strip()
                    if _tmpl and "{ZERO_FIELDS}" in _tmpl:
                        _provided = _tmpl.replace("{ZERO_FIELDS}", "、".join(self.last_zero_fields))
                    else:
                        import datetime as _dt
                        _provided = (
                            f"本回報週期內，以下項目尚未發生：{'、'.join(self.last_zero_fields)}。"
                            f"若有遺漏請律師上 portal 暫存頁面補充。"
                            f"（MAGI 自動填寫，{_dt.date.today().strftime('%Y-%m-%d')}）"
                        )
                _zero_reasons = {f: _provided for f in (zero_fields_found or [])}
                # 傳入已組好的文案（counts dict 含 noarrivereason 鍵）
                self.fill_noarrivereason_textarea(
                    counts={"noarrivereason": _provided},
                    zero_reasons=_zero_reasons,
                )

        else:
            self.log(f"⚠️ 無可填欄位映射（workflow={workflow}）")
            return False

        return True

    def save_workflow_draft(self, workflow: str, laf_case_number: str = "", client_name: str = "", fields: Dict[str, Any] = None) -> bool:
        """
        通用 workflow 暫存（只暫存，不送出）。
        支援：go_live / condition / inquiry / withdrawal / fee
        """
        meta = self._workflow_meta(workflow)
        if not meta:
            self.log(f"❌ 不支援 workflow: {workflow}")
            return False
        if not self.open_workflow_report_page(workflow, laf_case_number=laf_case_number, client_name=client_name):
            return False

        # Portal may reject new draft when an existing one is still in progress.
        try:
            src_probe = (self.driver.page_source or "")
        except Exception:
            src_probe = ""
        if ("目前已有回報資料正在處理中" in src_probe) or ("已有回報資料正在處理中" in src_probe):
            self.log(f"ℹ️ {meta.get('name','workflow')}：系統顯示已有回報資料正在處理中，略過本次存檔。")
            self.last_upload_result = {
                "ok": True,
                "workflow": str(workflow or ""),
                "requested": 0,
                "uploaded": 0,
                "failed": 0,
                "files": [],
                "skipped_existing": True,
            }
            self._save_page_debug_html(f"{workflow}_already_in_progress")
            return True

        wf_fields = dict(fields or {})
        self.fill_workflow_fields(workflow, wf_fields)
        upload_files = list(wf_fields.get("upload_files") or [])
        if upload_files:
            self.log(f"📎 {meta.get('name','workflow')}附件上傳：共 {len(upload_files)} 份 PDF")
            up_res = self._upload_supporting_files(upload_files, workflow=str(workflow or ""))
            self._wait_workflow_preview_ready(workflow, wf_fields, timeout_sec=10.0)
            self._save_page_debug_html(f"{workflow}_uploaded")
            if not up_res.get("ok"):
                self.log(f"❌ {meta.get('name','workflow')}附件上傳失敗（未成功上傳任何檔案）")
                self._save_page_debug_html(f"{workflow}_upload_failed")
                return False

        # ===== 結案資料彙整（撤回/疑義專用） =====
        # 如果 fields 包含 closing_counts，表示需要填寫辦理情形
        _closing_counts = wf_fields.get("closing_counts")
        wf = (workflow or "").strip().lower()
        if _closing_counts and wf in ("withdrawal", "inquiry"):
            self.log(f"📋 {meta.get('name','workflow')}：偵測到 closing_counts，進入結案資料彙整...")
            _zero_reasons = wf_fields.get("closing_zero_reasons") or {}
            cs_ok = self.fill_workflow_closing_summary(
                workflow=workflow,
                counts=_closing_counts,
                zero_reasons=_zero_reasons,
            )
            if cs_ok:
                self.log(f"✅ {meta.get('name','workflow')}結案資料彙整已暫存")
                # doFinish + doTempSave 會離開原表單頁面，
                # 需要重新進入撤回/疑義表單頁才能執行最終 doSave
                self.log(f"  🔄 重新進入{meta.get('name','workflow')}表單...")
                if not self.open_workflow_report_page(workflow, laf_case_number=laf_case_number, client_name=client_name):
                    self.log(f"  ⚠️ 重新進入{meta.get('name','workflow')}表單失敗")
                    return False
                # 重新填寫基本欄位（頁面重新載入後欄位會被清空）
                self.fill_workflow_fields(workflow, wf_fields)
            else:
                self.log(f"⚠️ {meta.get('name','workflow')}結案資料彙整失敗，僅暫存基本欄位")

        clicked = False
        for js in (meta.get("draft_js") or []):
            fn = (js.split("(")[0] or "").strip()
            if not fn:
                continue
            try:
                clicked = bool(
                    self.driver.execute_script(
                        f"try {{ if (typeof {fn} === 'function') {{ {js}; return true; }} }} catch (e) {{}} return false;"
                    )
                )
            except Exception:
                clicked = False
            if clicked:
                break
        if not clicked:
            clicked = self._click_button_by_text(meta.get("draft_buttons") or ["存檔", "暫存", "保存", "儲存"])
        if not clicked:
            self.log(f"⚠️ {meta.get('name','workflow')}未找到『存檔』按鈕（此頁面可能不支援存檔，或僅有送出按鈕）。")
            self.log(f"   動作：已填寫欄位，但不執行點擊。請手動確認。")
            # go_live 等無存檔按鈕的 workflow，強制截圖供預覽確認
            self._wait_workflow_preview_ready(workflow, wf_fields, timeout_sec=10.0)
            self._save_page_debug_html(f"{workflow}_draft_filled", force=True)
            return True # 視為成功（已填寫）

        time.sleep(2.0)
        self.log(f"✅ {meta.get('name','workflow')}存檔完成")
        self._wait_workflow_preview_ready(workflow, wf_fields, timeout_sec=8.0)
        self._save_page_debug_html(f"{workflow}_draft_ok", force=True)
        return True

    def submit_workflow(self, workflow: str, laf_case_number: str = "", client_name: str = "", fields: Dict[str, Any] = None) -> bool:
        """
        正式送出 workflow（目前僅允許 go_live，且需明確環境變數開啟）。
        """
        meta = self._workflow_meta(workflow)
        if not meta:
            self.log(f"❌ 不支援 workflow: {workflow}")
            return False

        wf = (workflow or "").strip().lower()
        if wf not in {"go_live", "progress"}:
            self.log(f"🔒 安全政策：submit_workflow 目前僅允許 go_live / progress（不支援 {wf}）。")
            return False

        if wf == "go_live":
            allow = str(os.environ.get("MAGI_LAF_ALLOW_GO_LIVE_SUBMIT", "0")).strip().lower() in {"1", "true", "yes", "on"}
            if not allow:
                self.log("🔒 安全政策：MAGI_LAF_ALLOW_GO_LIVE_SUBMIT != 1，禁止送出。")
                return False
        elif wf == "progress":
            allow = str(os.environ.get("MAGI_LAF_ALLOW_PROGRESS_SUBMIT", "0")).strip().lower() in {"1", "true", "yes", "on"}
            if not allow:
                self.log(
                    "🔒 安全政策：MAGI_LAF_ALLOW_PROGRESS_SUBMIT != 1，禁止送出。\n"
                    "正確路徑：請透過 Discord/Telegram 回覆「正確送出 <確認碼>」，\n"
                    "由 api/domains/laf_flow.py::_run_progress_submit() 統一帶 env=1 執行。\n"
                    "CLI 直接 submit 為反模式，可能誤送未核對的資料。"
                )
                return False

        if not self.open_workflow_report_page(workflow, laf_case_number=laf_case_number, client_name=client_name):
            return False

        wf_fields = dict(fields or {})
        self.fill_workflow_fields(workflow, wf_fields)

        # Upload files (same as draft flow)
        upload_files = list(wf_fields.get("upload_files") or [])
        if upload_files:
            self.log(f"📎 {meta.get('name','workflow')}送出前附件上傳：共 {len(upload_files)} 份 PDF")
            up_res = self._upload_supporting_files(upload_files, workflow=str(workflow or ""))
            if not up_res.get("ok"):
                self.log(f"❌ {meta.get('name','workflow')}附件上傳失敗")
                self._save_page_debug_html(f"{workflow}_submit_upload_failed")
                return False
            time.sleep(0.5)
            ready = self._wait_workflow_preview_ready(workflow, wf_fields, timeout_sec=10.0)
            if not ready:
                self.log(f"  ⚠️ {meta.get('name','workflow')}送出前畫面未完全穩定，仍繼續送出。")

        # Try submit via JS function first (most reliable), then button ID, then text search
        clicked = False
        # 1) JS functions (e.g. doUpdate() for go_live)
        for js_call in (meta.get("submit_js") or []):
            try:
                self.driver.execute_script(js_call)
                self.log(f"  ✅ 已透過 JS 呼叫送出: {js_call}")
                clicked = True
                break
            except Exception:
                continue
        # 2) Button by ID (#save_btn is the go_live submit button)
        if not clicked:
            for btn_id in ["#save_btn", "#submitBtn", "#sendBtn"]:
                try:
                    ok_js = self.driver.execute_script(
                        f"var b=document.querySelector('{btn_id}'); if(b){{b.click(); return true;}} return false;"
                    )
                    if ok_js:
                        self.log(f"  ✅ 已透過按鈕 ID 送出: {btn_id}")
                        clicked = True
                        break
                except Exception:
                    continue
        # 3) Fall back to text search
        if not clicked:
            clicked = self._click_button_by_text(meta.get("submit_buttons") or ["確定", "送出", "提交"])
        if not clicked:
            self.log("❌ 找不到可送出的『確定/送出/提交』按鈕")
            self._save_page_debug_html(f"{workflow}_submit_failed")
            return False

        time.sleep(2.0)
        artifact = self._save_page_debug_html(f"{workflow}_submit_clicked")
        try:
            src = (self.driver.page_source or "")
        except Exception:
            src = ""
        if any(k in src for k in ["成功", "完成", "已送出", "處理完成"]):
            self.log("✅ workflow 送出完成（偵測到成功訊息）")
        else:
            self.log("⚠️ 已執行送出點擊（未偵測到明確成功訊息，請以平台畫面為準）")
        if artifact:
            self.last_debug_artifact = dict(artifact)
        return True

    # ──────────────────────────────────────────────────────────────
    # 結案/撤回 狀態查詢（不點擊回報，僅讀取清單）
    # ──────────────────────────────────────────────────────────────

    def query_closing_status(self, laf_case_number: str) -> dict:
        """
        查詢指定法扶案號在「結案清單 (toClosedReport)」和「撤回清單 (toPBQuery)」
        上是否有紀錄，以及狀態（暫存 / 已轉入 / 無）。

        Returns:
            {
                "closing": {"found": bool, "status": str, "rows_text": [str]},
                "withdrawal": {"found": bool, "status": str, "rows_text": [str]},
            }
        status 可能為: "待轉入"（已送件法扶審核中）, "已轉入"（法扶通過）, "暫存"（尚未送出）, "有紀錄"（狀態不明）, "" (空=查無)
        """
        result = {
            "closing": {"found": False, "status": "", "rows_text": []},
            "withdrawal": {"found": False, "status": "", "rows_text": []},
        }
        applyno = (laf_case_number or "").strip()
        if not applyno or not self.driver:
            return result

        # --- 結案清單 ---
        result["closing"] = self._query_list_page_status(
            url_path="/lafcsp/toClosedReport",
            label="結案清單",
            applyno=applyno,
        )

        # --- 撤回清單 ---
        result["withdrawal"] = self._query_list_page_status(
            url_path="/lafcsp/toPBQuery",
            label="撤回清單",
            applyno=applyno,
        )

        return result

    def _query_list_page_status(self, url_path: str, label: str, applyno: str) -> dict:
        """
        開啟清單頁 → 搜尋案號 → 讀取結果表格的狀態欄。
        不點擊任何回報/明細按鈕。
        """
        info = {"found": False, "status": "", "rows_text": []}
        try:
            url = f"{self.base_url}{url_path}"
            self.driver.get(url)
            time.sleep(1.2)
            self._switch_to_content_frame_if_any()

            if self._is_login_or_timeout_page():
                if not self.login():
                    return info
                self.driver.get(url)
                time.sleep(1.5)
                self._switch_to_content_frame_if_any()

            # 填入案號
            filled = False
            for sel in ["#applyno", "input[name='applyno']", "input#applyno",
                        "#toPBQuery_applyno"]:
                try:
                    inp = WebDriverWait(self.driver, 8).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, sel))
                    )
                    self._set_input_value(inp, applyno)
                    filled = True
                    break
                except Exception:
                    continue
            if not filled:
                self.log(f"⚠️ {label}：找不到申請編號輸入框")
                return info

            # 搜尋
            searched = False
            for js in ["showList()", "doSearch('toPBQuery')"]:
                try:
                    searched = bool(self.driver.execute_script(
                        f"if (typeof {js.split('(')[0]} === 'function') {{ {js}; return true; }} return false;"))
                    if searched:
                        break
                except Exception:
                    continue
            if not searched:
                # queryBtn fallback
                try:
                    searched = bool(self.driver.execute_script(
                        "const q=document.querySelector('#queryBtn'); if(!q) return false; q.removeAttribute('disabled'); q.click(); return true;"))
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5169, exc_info=True)
            if not searched:
                self._click_button_by_text(["開始搜尋", "搜尋", "查詢"])
            time.sleep(1.5)

            # 讀取結果表格
            rows = self.driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            for row in rows:
                txt = row.text.strip()
                if not txt or "查無" in txt or "無資料" in txt:
                    continue
                if applyno in txt:
                    info["found"] = True
                    info["rows_text"].append(txt)

            if info["found"]:
                all_text = " ".join(info["rows_text"])
                if "待轉入" in all_text:
                    # 已送件，法扶尚在處理中（不需再操作）
                    info["status"] = "待轉入"
                elif "已轉入" in all_text:
                    info["status"] = "已轉入"
                elif "暫存" in all_text:
                    info["status"] = "暫存"
                else:
                    # 有找到紀錄但狀態不明，嘗試從 toReport 按鈕的 reply_id 推斷
                    # 若有 reply_id ≠ '' → 暫存
                    try:
                        q_case = json.dumps(applyno)
                        xp = f"//*[(contains(@onclick, 'toReport(') or contains(@href, 'toReport(')) and (contains(@onclick, {q_case}) or contains(@href, {q_case}))]"
                        els = self.driver.find_elements(By.XPATH, xp)
                        for el in els:
                            attr_val = (el.get_attribute("onclick") or "") + (el.get_attribute("href") or "")
                            m = re.search(r"toReport\s*\(([^)]+)\)", attr_val)
                            if m:
                                args = [x.strip().strip("'").strip('"') for x in m.group(1).split(',')]
                                if len(args) >= 4 and args[3].strip():
                                    info["status"] = "暫存"
                                    break
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5209, exc_info=True)
                    if not info["status"]:
                        info["status"] = "有紀錄"

            self.log(f"🔍 {label} [{applyno}]: found={info['found']}, status={info['status']}")

        except Exception as e:
            self.log(f"⚠️ {label}查詢異常: {e}")

        return info

    # ------------------------------------------------------------------
    # 接案清冊 Excel 匯出（夜巡用）
    # ------------------------------------------------------------------

    def export_case_list_excel(self, start_tw: str = "1130101", end_tw: str = "") -> Optional[str]:
        """
        匯出「律師接案清冊」Excel 檔。

        Args:
            start_tw: 開始日期（民國 YYYMMDD），預設 1130101
            end_tw: 結束日期（民國 YYYMMDD），預設為今日

        Returns:
            下載的 xlsx 檔案路徑，失敗時回傳 None
        """
        if not self.driver:
            self.log("❌ 瀏覽器未初始化")
            return None

        if not end_tw:
            from datetime import date as _date
            today = _date.today()
            tw_year = today.year - 1911
            end_tw = f"{tw_year}{today.month:02d}{today.day:02d}"

        url = f"{self.base_url}/lafcsp/toCaseList"
        self.log(f"🌐 開啟接案清冊: {url}")
        self.driver.get(url)
        time.sleep(1.5)
        self._switch_to_content_frame_if_any()

        if self._is_login_or_timeout_page():
            if not self.login():
                return None
            self.driver.get(url)
            time.sleep(1.5)
            self._switch_to_content_frame_if_any()

        # 填日期 + submit（讓 exportFile 的 form 生效）
        self.driver.execute_script(
            "document.querySelector('#stdt').value=arguments[0];"
            "document.querySelector('#eddt').value=arguments[1];"
            "$('#form1').submit();",
            str(start_tw), str(end_tw),
        )
        time.sleep(3)

        # 匯出 Excel
        # Bug fix: LAF 原生 exportFile() 把 form target 設為 "hideFrame"，但 hideFrame 只存在於
        # frameset 主框（toMainPage 後的外層）。我們直接 GET /toCaseList 是 standalone 頁面 → 沒 hideFrame
        # → form submit 找不到 target → 開新 tab 或 submit 失敗 → 沒下載
        # 修法：建立 hideFrame iframe 後再 submit；確保 download 在當前 context 觸發
        import glob
        dl = str(self.download_folder)
        before = set(glob.glob(os.path.join(dl, "*.xlsx")))
        self.driver.execute_script(
            """
            // 確保 hideFrame iframe 存在（standalone 頁面沒有，建立一個）
            var existing = document.querySelector('iframe[name="hideFrame"]');
            if (!existing) {
                var iframe = document.createElement('iframe');
                iframe.name = 'hideFrame';
                iframe.id = 'hideFrame';
                iframe.style.display = 'none';
                document.body.appendChild(iframe);
            }
            document.downloadForm.stdtForExcel.value = arguments[0];
            document.downloadForm.eddtForExcel.value = arguments[1];
            document.downloadForm.action = '/lafcsp/exportCaseListDatas';
            document.downloadForm.target = 'hideFrame';
            document.downloadForm.submit();
            """,
            str(start_tw), str(end_tw),
        )

        # 等待下載完成（最多 90 秒；LAF 案件多時 Excel 生成需要時間）
        for _ in range(90):
            time.sleep(1)
            after = set(glob.glob(os.path.join(dl, "*.xlsx")))
            new = after - before
            if new:
                path = new.pop()
                self.log(f"✅ 接案清冊已匯出: {os.path.basename(path)}")
                return path

        self.log("⚠️ 接案清冊匯出逾時（90 秒內無新檔案）")
        return None

    # ------------------------------------------------------------------
    # Portal 暫存/待處理全清單查詢（夜巡用）
    # ------------------------------------------------------------------

    def query_pending_drafts_all(self) -> dict:
        """
        不帶案號搜尋三個 workflow 清單頁，找出仍在暫存/待處理的案件。
        用於夜巡確認是否有遺漏未送出的草稿。

        Returns:
            {
                "closing":   [{"applyno": ..., "status": ..., "row_text": ...}, ...],
                "condition": [...],
                "go_live":   [...],
                "case_status": [...],
            }
        """
        result: Dict[str, list] = {"closing": [], "condition": [], "go_live": [], "case_status": []}
        if not self.driver:
            return result

        result["closing"] = self._query_list_page_all_items(
            "/lafcsp/toClosedReport", "結案回報",
        )
        result["condition"] = self._query_list_page_all_items(
            "/lafcsp/toCndQuery", "條件是否成就",
        )
        result["go_live"] = self._query_list_page_all_items(
            "/lafcsp/toNotOpenedCase", "未開辦",
        )
        result["case_status"] = self.query_case_status_drafts(proc_status="T")
        return result

    def _query_list_page_all_items(self, url_path: str, label: str) -> list:
        """開啟清單頁，不帶條件搜尋，回傳所有可見列。"""
        items: list = []
        try:
            url = f"{self.base_url}{url_path}"
            self.driver.get(url)
            time.sleep(1.5)
            self._switch_to_content_frame_if_any()

            if self._is_login_or_timeout_page():
                if not self.login():
                    return items
                self.driver.get(url)
                time.sleep(1.5)
                self._switch_to_content_frame_if_any()

            # 不填任何條件直接搜尋
            searched = False
            try:
                searched = bool(self.driver.execute_script(
                    "const q=document.querySelector('#queryBtn');"
                    "if(!q) return false;"
                    "q.removeAttribute('disabled'); q.click(); return true;"
                ))
            except Exception:
                searched = False
            if not searched:
                self._click_button_by_text(["開始搜尋", "搜尋", "查詢"])
            time.sleep(2.0)

            rows = self.driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
            for row in rows:
                txt = row.text.strip()
                if not txt or "查無" in txt or "無資料" in txt or len(txt) < 5:
                    continue
                applyno = ""
                m = re.search(r"\d{6,8}-[A-Za-z]-\d{3}", txt)
                if m:
                    applyno = m.group(0)
                status = ""
                for kw in ("暫存", "待轉入", "已轉入"):
                    if kw in txt:
                        status = kw
                        break
                items.append({
                    "applyno": applyno,
                    "status": status,
                    "row_text": txt.replace("\n", " | ")[:200],
                })

            self.log(f"🔍 {label}：找到 {len(items)} 筆")
        except Exception as e:
            self.log(f"⚠️ {label}查詢異常: {e}")

        return items

    def query_case_status_drafts(self, proc_status: str = "T", reply_type: str = "") -> list:
        """
        進入「案件狀態區」查詢暫存/待處理資料。

        Args:
            proc_status: 回報狀態代碼，預設 T（暫存）
            reply_type: 回報類型代碼，留空表示全部類型

        Returns:
            [
                {
                    "branch": "台北分會",
                    "applyno": "1150206-A-042",
                    "reply_type": "結案回報",
                    "first_reply_date": "1150402 11:43:26",
                    "latest_reply_date": "1150402 11:43:26",
                    "status": "暫存",
                    "row_text": "...",
                },
            ]
        """
        items: list = []
        if not self.driver:
            return items

        try:
            url = f"{self.base_url}/lafcsp/toCaseStatusList"
            self.driver.get(url)
            time.sleep(1.5)
            self._switch_to_content_frame_if_any()

            if self._is_login_or_timeout_page():
                if not self.login():
                    return items
                self.driver.get(url)
                time.sleep(1.5)
                self._switch_to_content_frame_if_any()

            # 只看暫存草稿；日期區間清空，避免被頁面預設值意外縮小範圍
            self._set_field_by_name("applyno", "", kind="input")
            if reply_type:
                self._set_field_by_name("reply_type", str(reply_type), kind="select")
            if proc_status:
                self._set_field_by_name("proc_status", str(proc_status), kind="select")
            for date_name in ("reply_dateStart", "reply_dateEnd"):
                try:
                    self.driver.execute_script(
                        "const el = document.getElementsByName(arguments[0])[0];"
                        "if (el) { el.value = ''; el.dispatchEvent(new Event('change', {bubbles:true})); }",
                        date_name,
                    )
                except Exception:
                    self._set_field_by_name(date_name, "", kind="input")

            searched = False
            try:
                searched = bool(self.driver.execute_script(
                    "if (typeof showList === 'function') { showList(); return true; } return false;"
                ))
            except Exception:
                searched = False
            if not searched:
                searched = self._click_button_by_text(["開始搜尋", "搜尋", "查詢"])
            if not searched:
                self.log("⚠️ 案件狀態區無法觸發搜尋")
                return items
            time.sleep(2.0)

            rows = self.driver.find_elements(By.CSS_SELECTOR, "#DataTables_Table_0 tbody tr")
            if not rows:
                rows = self.driver.find_elements(By.CSS_SELECTOR, "table tbody tr")

            for row in rows:
                try:
                    cells = row.find_elements(By.CSS_SELECTOR, "td")
                    if len(cells) < 6:
                        txt = row.text.strip()
                        if txt and "查無" not in txt and "無資料" not in txt:
                            items.append({
                                "branch": "",
                                "applyno": "",
                                "reply_type": "",
                                "first_reply_date": "",
                                "latest_reply_date": "",
                                "status": "",
                                "row_text": txt.replace("\n", " | ")[:200],
                            })
                        continue

                    branch = cells[0].text.strip()
                    applyno = cells[1].text.strip()
                    reply_type_text = cells[2].text.strip()
                    first_reply_date = cells[3].text.strip()
                    latest_reply_date = cells[4].text.strip()
                    status_text = cells[5].text.strip()

                    if not applyno or "查無" in applyno or "無資料" in applyno:
                        continue

                    row_text = " | ".join(
                        part for part in [
                            branch,
                            applyno,
                            reply_type_text,
                            first_reply_date,
                            latest_reply_date,
                            status_text,
                        ] if part
                    )[:200]

                    items.append({
                        "branch": branch,
                        "applyno": applyno,
                        "reply_type": reply_type_text,
                        "first_reply_date": first_reply_date,
                        "latest_reply_date": latest_reply_date,
                        "status": status_text,
                        "row_text": row_text,
                    })
                except Exception:
                    continue

            label = f"回報狀態={proc_status or '*'}"
            if reply_type:
                label += f", 回報類型={reply_type}"
            self.log(f"🔍 案件狀態區：{label}，找到 {len(items)} 筆")
        except Exception as e:
            self.log(f"⚠️ 案件狀態區查詢異常: {e}")

        return items

    def close(self):
        """關閉瀏覽器"""
        if self.driver:
            try:
                self.driver.quit()
            except Exception as e:
                self.log(f"  ⚠️ 關閉瀏覽器時發生錯誤 (可忽略): {e}")
            finally:
                # ★ 重點：無論如何都要將 driver 設為 None，
                # 這樣下一次 login 才會重新啟動瀏覽器
                self.driver = None


# ==============================================================================
# Gmail 監控器
# ==============================================================================

@dataclass
class GeneralEmailInfo:
    """一般信件資訊"""
    message_id: str
    subject: str
    sender: str
    received_at: str
    snippet: str = ""
    body: str = ""
    has_attachment: bool = False
    attachments: List[Dict] = field(default_factory=list)
    rule_name: str = ""
    target_subfolder: str = ""

# ==============================================================================
# Gmail 監控器
# ==============================================================================

class LAFGmailMonitor:
    """監控 Gmail 中的法扶信件與一般信件"""
    
    SCOPES = ['https://www.googleapis.com/auth/gmail.readonly']
    
    def __init__(self, credentials_path: str, token_path: str, 
                 callback=None, general_callback=None, log_callback=None,
                 processed_ids_file: str = None):
        """
        初始化
        
        Args:
            credentials_path: Google OAuth credentials.json 路徑
            token_path: Token 儲存路徑
            callback: 發現法扶信件時的回呼函式 callback(case_info: LAFCaseInfo)
            general_callback: 發現一般信件時的回呼函式 general_callback(email_info: GeneralEmailInfo)
            log_callback: 日誌回呼
            processed_ids_file: 已處理信件 ID 的持久化檔案路徑 (JSON)
        """
        self.credentials_path = credentials_path
        self.token_path = token_path
        self.callback = callback
        self.general_callback = general_callback
        self.log = log_callback or print
        
        self.service = None
        self.credentials = None
        
        self._running = False
        self._monitor_thread = None
        
        # ★ 持久化檔案路徑：若未指定，則使用與 token 相同目錄
        if processed_ids_file:
            self._processed_ids_file = processed_ids_file
        else:
            # 預設儲存在 laf_downloads 資料夾
            self._processed_ids_file = os.path.join(
                os.path.dirname(token_path) if os.path.dirname(token_path) else '.',
                'processed_laf_emails.json'
            )
        
        # ★ 從檔案載入已處理的 message ID
        self._processed_ids = self._load_processed_ids()
        self._general_processed_ids = self._load_processed_ids('_general')
    
    def _load_processed_ids(self, suffix: str = '') -> set:
        """載入已處理的 Email ID 記錄"""
        file_path = self._processed_ids_file
        if suffix:
            base, ext = os.path.splitext(file_path)
            file_path = f"{base}{suffix}{ext}"
        
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.log(f"  📂 已載入 {len(data)} 個已處理的信件 ID ({os.path.basename(file_path)})")
                    return set(data)
            except Exception as e:
                self.log(f"  ⚠️ 載入已處理信件記錄失敗: {e}")
        return set()

    def _save_processed_ids(self, suffix: str = ''):
        """儲存已處理的 Email ID 記錄"""
        file_path = self._processed_ids_file
        ids_to_save = self._processed_ids
        
        if suffix:
            base, ext = os.path.splitext(file_path)
            file_path = f"{base}{suffix}{ext}"
            ids_to_save = self._general_processed_ids
        
        try:
            # 確保目錄存在
            os.makedirs(os.path.dirname(file_path) or '.', exist_ok=True)
            # BUG-11: 裁剪到最新 3000 筆，防止長期無限增長
            ids_list = list(ids_to_save)
            if len(ids_list) > 5000:
                ids_list = ids_list[-3000:]
                if suffix:
                    self._general_processed_ids = set(ids_list)
                else:
                    self._processed_ids = set(ids_list)
                self.log(f"  ℹ️ 已處理信件記錄超過 5000，裁剪至 3000 筆")
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(ids_list, f)
        except Exception as e:
            self.log(f"  ⚠️ 儲存已處理信件記錄失敗: {e}")
    
    def authenticate(self) -> bool:
        """進行 Gmail API 認證"""
        if not GMAIL_AVAILABLE:
            self.log("⚠️ Gmail API 相關模組未安裝")
            return False
        
        # Lazy Load Google API
        global Credentials, InstalledAppFlow, Request, build
        if build is None:
            try:
                from google.oauth2.credentials import Credentials
                from google_auth_oauthlib.flow import InstalledAppFlow
                from google.auth.transport.requests import Request
                from googleapiclient.discovery import build
            except ImportError:
                return False

        if os.path.exists(self.token_path):
            with open(self.token_path, 'rb') as token:
                creds = pickle.load(token)
        else:
            creds = None
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                except Exception:
                    creds = None
            
            if not creds:
                if not os.path.exists(self.credentials_path):
                    self.log(f"❌ 找不到 credentials.json: {self.credentials_path}")
                    return False
                
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, self.SCOPES
                )
                creds = flow.run_local_server(port=0)
            
            with open(self.token_path, 'wb') as token:
                pickle.dump(creds, token)
        
        self.credentials = creds
        self.service = build('gmail', 'v1', credentials=creds)
        self.log("✅ Gmail API 認證成功")
        return True
    
    def check_emails(self, max_results: int = 10) -> List[LAFCaseInfo]:
        """檢查新的法扶信件"""
        results = []
        
        if not self.service:
            self.log("❌ Gmail 服務未初始化")
            return results
        
        try:
            self.log("🔍 正在檢查新信件...")
            # 搜尋最近的法扶相關信件（不再限定未讀，由內部 _processed_ids 避免重複）
            query = '(from:@laf.org.tw OR from:laf.server)'
            
            response = self.service.users().messages().list(
                userId='me',
                q=query,
                maxResults=max_results
            ).execute()
            
            messages = response.get('messages', [])
            
            for msg in messages:
                msg_id = msg['id']
                
                if msg_id in self._processed_ids:
                    continue
                
                # 取得完整信件
                full_msg = self.service.users().messages().get(
                    userId='me', id=msg_id
                ).execute()
                
                # 嘗試取得主旨以便顯示
                subject = "未知主旨"
                try:
                    headers = full_msg.get('payload', {}).get('headers', [])
                    for h in headers:
                        if h.get('name', '').lower() == 'subject':
                            subject = h.get('value', '')
                            break
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5424, exc_info=True)
                
                self.log(f"🔍 [掃描] 檢查信件: {subject} (ID: {msg_id[-6:]}...)")
                
                case_info = self._process_message(msg_id, full_msg)

                if case_info:
                    results.append(case_info)
                    self._processed_ids.add(msg_id)
                    self._save_processed_ids()  # ★ 持久化
                else:
                    # 解析失敗：只標記「已忽略」，不加入 _processed_ids
                    # 讓 _process_message 內部的 ⚠️ log 留紀錄即可
                    # 注意：非派案信件已在 parse_subject 前置過濾攔截（回報/撤回等），
                    # 這裡剩餘的失敗可能是新格式，需要人工注意
                    pass

        except Exception as e:
            self.log(f"❌ 檢查信件失敗: {e}")
            traceback.print_exc()

        return results

    def check_general_emails(self, rules: List[Dict], max_results: int = 10) -> List[GeneralEmailInfo]:
        """檢查符合規則的一般信件"""
        results = []
        
        if not self.service:
            return results
            
        for rule in rules:
            try:
                rule_name = rule.get('name', '未命名規則')
                query = rule.get('query', '')
                target_subfolder = rule.get('target_subfolder', '')
                
                if not query:
                    continue
                    
                # self.log(f"🔍 [一般信件] 檢查規則: {rule_name} (Query: {query})")
                
                response = self.service.users().messages().list(
                    userId='me',
                    q=query,
                    maxResults=max_results
                ).execute()
                
                messages = response.get('messages', [])
                
                for msg in messages:
                    msg_id = msg['id']

                    # 若此信件已被「法扶信件」流程成功解析處理，避免在一般信件流程重複處理
                    if msg_id in self._processed_ids:
                        continue
                    
                    # 檢查是否處理過 (使用獨立的 set)
                    if msg_id in self._general_processed_ids:
                        continue
                    
                    # 取得完整信件
                    full_msg = self.service.users().messages().get(
                        userId='me', id=msg_id
                    ).execute()
                    
                    email_info = self._process_general_message(msg_id, full_msg, rule_name, target_subfolder)
                    
                    if email_info:
                        self.log(f"📨 [一般信件] 發現新信件: {email_info.subject} (規則: {rule_name})")
                        results.append(email_info)
                        self._general_processed_ids.add(msg_id)
                        self._save_processed_ids('_general')  # ★ 持久化
                        
                        # 觸發回呼
                        if self.general_callback:
                            self.general_callback(email_info)
                            
            except Exception as e:
                self.log(f"❌ 檢查一般信件失敗 (規則: {rule.get('name')}): {e}")
                
        return results

    def _process_general_message(self, msg_id: str, msg_data: dict, rule_name: str, target_subfolder: str) -> Optional[GeneralEmailInfo]:
        """解析一般信件"""
        try:
            payload = msg_data.get('payload', {})
            headers = payload.get('headers', [])
            snippet = (msg_data.get("snippet") or "").strip()
            
            subject = "無主旨"
            sender = "未知寄件者"
            date_str = ""
            
            for h in headers:
                name = h.get('name', '').lower()
                if name == 'subject':
                    subject = h.get('value', '')
                elif name == 'from':
                    sender = h.get('value', '')
                elif name == 'date':
                    date_str = h.get('value', '')
            
            # 檢查附件
            has_attachment = False
            attachments = []
            
            parts = [payload]
            if 'parts' in payload:
                parts.extend(payload['parts'])
                
            # 遞迴檢查 parts
            def check_parts(parts_list):
                found_atts = []
                for part in parts_list:
                    if part.get('filename') and part.get('body', {}).get('attachmentId'):
                        found_atts.append({
                            'filename': part['filename'],
                            'attachmentId': part['body']['attachmentId'],
                            'size': part['body'].get('size', 0),
                            'mimeType': part.get('mimeType', '')
                        })
                    
                    if 'parts' in part:
                        found_atts.extend(check_parts(part['parts']))
                return found_atts

            attachments = check_parts(parts)
            has_attachment = len(attachments) > 0

            # 一般信件也保留一份可供後續比對/歸檔的文字內文（不印出內容）
            body = ""
            try:
                body = self._get_email_body(msg_data, quiet=True) or ""
                if len(body) > 20000:
                    body = body[:20000]
            except Exception:
                body = ""
            
            return GeneralEmailInfo(
                message_id=msg_id,
                subject=subject,
                sender=sender,
                received_at=date_str,
                snippet=snippet,
                body=body,
                has_attachment=has_attachment,
                attachments=attachments,
                rule_name=rule_name,
                target_subfolder=target_subfolder
            )
            
        except Exception as e:
            self.log(f"⚠️ 解析一般信件失敗: {e}")
            return None

    def download_attachments(self, email_info: GeneralEmailInfo, target_folder: str) -> List[str]:
        """下載一般信件的附件"""
        downloaded_files = []
        
        if not self.service or not email_info.attachments:
            return downloaded_files
            
        try:
            os.makedirs(target_folder, exist_ok=True)
            _eventlog(
                "laf:gmail:attachments:start",
                ok=None,
                payload={"target_folder": target_folder, "attachments": len(email_info.attachments or [])},
                tags={"rule": (email_info.rule_name or "")[:40]},
            )
            
            for att in email_info.attachments:
                try:
                    att_id = att['attachmentId']
                    filename = os.path.basename(att.get('filename') or 'attachment.bin')
                    filename = re.sub(r'[<>:"/\\\\|?*]+', '_', filename).strip() or "attachment.bin"
                    
                    # 取得附件內容
                    att_data = self.service.users().messages().attachments().get(
                        userId='me', messageId=email_info.message_id, id=att_id
                    ).execute()
                    
                    file_data = base64.urlsafe_b64decode(att_data['data'].encode('UTF-8'))
                    
                    base_name, ext = os.path.splitext(filename)
                    file_path = os.path.join(target_folder, filename)
                    # 避免覆蓋既有檔案
                    if os.path.exists(file_path):
                        for i in range(1, 50):
                            cand = os.path.join(target_folder, f"{base_name} ({i}){ext}")
                            if not os.path.exists(cand):
                                file_path = cand
                                break
                    with open(file_path, 'wb') as f:
                        f.write(file_data)
                        
                    downloaded_files.append(file_path)
                    self.log(f"    📥 下載附件成功: {filename}")
                    
                except Exception as e:
                    self.log(f"    ❌ 下載附件失敗 ({att.get('filename')}): {e}")
                    
        except Exception as e:
            self.log(f"❌ 下載附件流程失敗: {e}")
            _eventlog(
                "laf:gmail:attachments:done",
                ok=False,
                payload={"error": str(e)[:220], "downloaded": len(downloaded_files)},
                tags={"rule": (email_info.rule_name or "")[:40]},
            )
            
        _eventlog(
            "laf:gmail:attachments:done",
            ok=True,
            payload={"downloaded": len(downloaded_files), "files": [os.path.basename(p) for p in downloaded_files[:5]]},
            tags={"rule": (email_info.rule_name or "")[:40]},
        )
        return downloaded_files

    def download_attachments_by_msg_id(self, msg_id: str, target_folder: str) -> List[str]:
        """
        透過郵件 ID 下載附件 (用於法扶通知信)
        
        Args:
            msg_id: Gmail 信件 ID
            target_folder: 目標資料夾路徑
            
        Returns:
            已下載的檔案路徑列表
        """
        downloaded_files = []
        
        if not self.service:
            self.log("  ⚠️ Gmail 服務未初始化，無法下載附件")
            return downloaded_files
        
        try:
            os.makedirs(target_folder, exist_ok=True)
            _eventlog(
                "laf:gmail:msg_attachments:start",
                ok=None,
                payload={"target_folder": target_folder},
                tags={"msg_id_tail": (msg_id or "")[-8:]},
            )
            
            # 取得完整信件資料
            message = self.service.users().messages().get(
                userId='me', id=msg_id, format='full'
            ).execute()
            
            payload = message.get('payload', {})
            parts = payload.get('parts', [])
            
            # 如果沒有 parts，可能附件在 payload 本身
            if not parts and payload.get('filename'):
                parts = [payload]
            
            # 遞迴處理 multipart
            def process_parts(parts_list):
                files = []
                for part in parts_list:
                    filename = part.get('filename', '')
                    mime_type = part.get('mimeType', '')
                    
                    # 遞迴處理嵌套的 parts
                    if 'parts' in part:
                        files.extend(process_parts(part['parts']))
                        continue
                    
                    # 只處理有檔名的附件（跳過純文字部分）
                    if not filename:
                        continue

                    filename = os.path.basename(filename or "attachment.bin")
                    filename = re.sub(r'[<>:"/\\\\|?*]+', '_', filename).strip() or "attachment.bin"
                    
                    self.log(f"  📎 發現附件: {filename} ({mime_type})")
                    
                    # 取得附件資料
                    body = part.get('body', {})
                    attachment_id = body.get('attachmentId')
                    
                    try:
                        if attachment_id:
                            att = self.service.users().messages().attachments().get(
                                userId='me', messageId=msg_id, id=attachment_id
                            ).execute()
                            file_data = base64.urlsafe_b64decode(att['data'].encode('UTF-8'))
                        elif body.get('data'):
                            file_data = base64.urlsafe_b64decode(body['data'].encode('UTF-8'))
                        else:
                            self.log(f"  ⚠️ 無法取得附件資料: {filename}")
                            continue
                        
                        # 儲存檔案
                        base_name, ext = os.path.splitext(filename)
                        file_path = os.path.join(target_folder, filename)
                        if os.path.exists(file_path):
                            for i in range(1, 50):
                                cand = os.path.join(target_folder, f"{base_name} ({i}){ext}")
                                if not os.path.exists(cand):
                                    filename = os.path.basename(cand)
                                    file_path = cand
                                    break
                        
                        with open(file_path, 'wb') as f:
                            f.write(file_data)
                        
                        files.append(file_path)
                        self.log(f"  ✅ 已下載附件: {filename}")
                        
                    except Exception as e:
                        self.log(f"  ❌ 下載附件失敗 ({filename}): {e}")
                
                return files
            
            downloaded_files = process_parts(parts)
            
        except Exception as e:
            self.log(f"❌ 下載附件流程失敗: {e}")
            import traceback
            traceback.print_exc()
            _eventlog(
                "laf:gmail:msg_attachments:done",
                ok=False,
                payload={"error": str(e)[:220]},
                tags={"msg_id_tail": (msg_id or "")[-8:]},
            )
        
        _eventlog(
            "laf:gmail:msg_attachments:done",
            ok=True,
            payload={"downloaded": len(downloaded_files or []), "files": [os.path.basename(p) for p in (downloaded_files or [])[:5]]},
            tags={"msg_id_tail": (msg_id or "")[-8:]},
        )
        return downloaded_files

    def scan_emails_in_range(self, start_date: str, end_date: str, check_exists_func=None):
        """
        掃描指定日期區間的法扶信件
        
        Args:
            start_date: 開始日期 (YYYY/MM/DD)
            end_date: 結束日期 (YYYY/MM/DD)
            check_exists_func: 檢查信件是否存在的函式
        """
        if not self.service:
            return
            
        try:
            self.log(f"🔍 正在掃描 {start_date} 至 {end_date} 的法扶信件...")
            
            # Gmail query: after:YYYY/MM/DD before:YYYY/MM/DD
            # 注意: before 是不包含該日期的，所以如果要包含 end_date，通常要加一天，
            # 但這裡我們先假設使用者輸入的是包含的，我們在 query 處理
            
            # 為了確保包含 end_date，我們將 end_date + 1 天
            try:
                end_dt = datetime.strptime(end_date, '%Y/%m/%d')
                next_day = end_dt + timedelta(days=1)
                query_end_date = next_day.strftime('%Y/%m/%d')
            except Exception:
                query_end_date = end_date

            query = f'(from:@laf.org.tw OR from:laf.server) after:{start_date} before:{query_end_date}'
            
            response = self.service.users().messages().list(
                userId='me',
                q=query,
                maxResults=100  # 增加掃描數量
            ).execute()
            
            messages = response.get('messages', [])
            self.log(f"📊 區間內共有 {len(messages)} 封相關信件")
            
            for msg in messages:
                msg_id = msg['id']
                
                # 1. 檢查是否已處理 (DB)
                if check_exists_func and check_exists_func(msg_id):
                    self.log(f"  ⏭️ 信件已處理，跳過 (ID: {msg_id[-6:]}...)")
                    self._processed_ids.add(msg_id)
                    self._save_processed_ids()  # ★ 持久化
                    continue
                
                # 2. 檢查記憶體快取
                if msg_id in self._processed_ids:
                    continue
                
                # 3. 處理信件
                full_msg = self.service.users().messages().get(
                    userId='me', id=msg_id
                ).execute()
                
                # 嘗試取得主旨以便顯示
                subject = "未知主旨"
                try:
                    headers = full_msg.get('payload', {}).get('headers', [])
                    for h in headers:
                        if h.get('name', '').lower() == 'subject':
                            subject = h.get('value', '')
                            break
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5821, exc_info=True)
                
                self.log(f"  ✨ 發現未處理信件: {subject} (ID: {msg_id[-6:]}...)")
                
                case_info = self._process_message(msg_id, full_msg)

                if case_info:
                    self._processed_ids.add(msg_id)
                    self._save_processed_ids()  # ★ 持久化
                    # 觸發回呼
                    if self.callback:
                        self.callback(case_info)

        except Exception as e:
            self.log(f"❌ 掃描區間信件失敗: {e}")

    def scan_today_emails(self, check_exists_func=None):
        """掃描今日所有法扶信件 (啟動時執行)"""
        today_str = datetime.now().strftime('%Y/%m/%d')
        self.scan_emails_in_range(today_str, today_str, check_exists_func)
    
    def _process_message(self, msg_id: str, msg_data: dict) -> Optional[LAFCaseInfo]:
        """處理單封信件"""
        try:
            headers = msg_data.get('payload', {}).get('headers', [])
            
            subject = ''
            sender = ''
            date_str = ''
            
            for header in headers:
                name = header.get('name', '').lower()
                value = header.get('value', '')
                if name == 'subject':
                    subject = value
                elif name == 'from':
                    sender = value
                elif name == 'date':
                    date_str = value
            
            # 解析主旨
            case_info = LAFCaseTypeParser.parse_subject(subject)
            
            if case_info:
                case_info.message_id = msg_id
                case_info.sender = sender
                
                try:
                    from email.utils import parsedate_to_datetime
                    case_info.received_at = parsedate_to_datetime(date_str)
                except Exception:
                    case_info.received_at = datetime.now()
                
                # 解析信件內文，檢查是否需要下載
                body = self._get_email_body(msg_data)
                case_info.needs_download = LAFCaseTypeParser.check_needs_download(body)
                
                # 檢查附件 (類似一般信件)
                payload = msg_data.get('payload', {})
                parts = [payload]
                if 'parts' in payload:
                    parts.extend(payload['parts'])
                    
                def check_parts(parts_list):
                    atts = []
                    for p in parts_list:
                        if p.get('filename'):
                            body_dict = p.get('body', {})
                            att_id = body_dict.get('attachmentId')
                            if att_id:
                                atts.append({
                                    'filename': p['filename'],
                                    'mimeType': p.get('mimeType', ''),
                                    'attachmentId': att_id,
                                    'size': body_dict.get('size', 0)
                                })
                        if 'parts' in p:
                            atts.extend(check_parts(p['parts']))
                    return atts

                case_info.attachments = check_parts(parts)
                case_info.has_attachment = len(case_info.attachments) > 0
                case_info.body = body or ""
                
                self.log(f"  📄 [附件檢查] 是否有附件標記: {'是' if case_info.has_attachment else '否'}")
                self.log(f"  📥 [下載檢查] 是否需從系統下載: {'是' if case_info.needs_download else '否'}")
                
                # 解析承辦人資訊
                self._parse_staff_info(body, case_info)
                
                self.log(f"\n{'='*60}")
                self.log(f"📧 收到法扶{case_info.notification_type}")
                self.log(f"  分會: {case_info.branch}")
                self.log(f"  當事人: {case_info.client_name}")
                self.log(f"  法扶案號: {case_info.laf_case_number}")
                self.log(f"  案件類型: {case_info.case_type} ({case_info.case_stage})")
                self.log(f"  案由: {case_info.case_reason}")
                self.log(f"  需要下載: {'是' if case_info.needs_download else '否 (有附件)'}")
                self.log(f"{'='*60}")
                
                return case_info
            
            else:
                self.log(f"  ⚠️ [忽略] 主旨格式不符，跳過: {subject}")
            
        except Exception as e:
            self.log(f"❌ 處理信件失敗: {e}")
        
        return None
    
    def _get_email_body(self, msg_data: dict, quiet: bool = False) -> str:
        """取得信件內文 (遞迴處理 multipart)"""
        try:
            payload = msg_data.get('payload', {})
            if not quiet:
                self.log(f"  [DEBUG] 解析郵件本體 (MimeType: {payload.get('mimeType')})...")
            
            def extract_text(part):
                mimeType = part.get('mimeType')
                body_data = part.get('body', {}).get('data')
                
                # 1. 優先回傳 text/plain
                if mimeType == 'text/plain' and body_data:
                    return base64.urlsafe_b64decode(body_data).decode('utf-8')
                
                # 2. 或是 multipart，遞迴尋找
                if mimeType.startswith('multipart/') and 'parts' in part:
                    # 遞迴檢查所有子部分
                    best_text = None
                    for sub_part in part['parts']:
                        text = extract_text(sub_part)
                        if text:
                            # 如果找到 plain text，直接回傳
                            if sub_part.get('mimeType') == 'text/plain':
                                return text
                            # 暫存 (可能是 html)
                            if not best_text:
                                best_text = text
                    return best_text
                    
                # 3. 最後回傳 text/html (作為 fallback)
                if mimeType == 'text/html' and body_data:
                    try:
                        decoded = base64.urlsafe_b64decode(body_data).decode('utf-8')
                        clean = re.sub('<[^<]+?>', '', decoded) # 去除 HTML tags
                        return clean
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5968, exc_info=True)
                
                return None

            # 開始遞迴解析
            text = extract_text(payload)
            if text:
                if not quiet:
                    self.log(f"  [DEBUG] 成功取得內文 ({len(text)} chars)")
                return text
            
            if not quiet:
                self.log(f"  ⚠️ [DEBUG] 無法取得有效內文")
            return ""
        except Exception as e:
            if not quiet:
                self.log(f"  ⚠️ [DEBUG] 取得內文發生錯誤: {e}")
            return ""
    
    def _parse_staff_info(self, body: str, case_info: LAFCaseInfo):
        """從信件內文解析承辦人資訊"""
        try:
            staff_match = re.search(
                r'本案承辦人員[：:]\s*(\S+)\s+電話[：:]\s*([\d\-#]+)\s+Email[：:]\s*(\S+@\S+)',
                body
            )
            if staff_match:
                case_info.staff_name = staff_match.group(1)
                case_info.staff_phone = staff_match.group(2)
                case_info.staff_email = staff_match.group(3)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5999, exc_info=True)
    
    def start_monitor(self, interval_seconds: int = 300, check_immediately: bool = True, general_rules: List[Dict] = None):
        """啟動背景監控"""
        if self._running:
            return
        
        if not self.authenticate():
            return
        
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(interval_seconds, check_immediately, general_rules),
            daemon=True
        )
        self._monitor_thread.start()
        self.log(f"✅ Gmail 監控已啟動 (每 {interval_seconds} 秒檢查)")
    
    def stop_monitor(self):
        """停止監控"""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
    
    def _monitor_loop(self, interval: int, check_immediately: bool, general_rules: List[Dict] = None):
        """監控迴圈"""
        if not check_immediately:
            time.sleep(interval)

        while self._running:
            try:
                # 1. 檢查法扶信件
                cases = self.check_emails()
                for case_info in cases:
                    if self.callback:
                        try:
                            self.callback(case_info)
                        except Exception as e:
                            self.log(f"❌ 法扶回呼處理失敗: {e}")
                
                # 2. 檢查一般信件
                if general_rules:
                    self.check_general_emails(general_rules)
                
                time.sleep(interval)
            
            except Exception as e:
                self.log(f"❌ 監控迴圈錯誤: {e}")
                time.sleep(60)


# ==============================================================================
# OSC 整合
# ==============================================================================

def _pdftotext_extract(pdf_path: str, max_pages: int = 2, timeout_sec: int = 40) -> str:
    p = (pdf_path or "").strip()
    if not p or not os.path.exists(p):
        return ""
    if not os.path.exists(PDFTOTEXT_BIN):
        return ""
    try:
        tmp_dir = os.path.join(tempfile.gettempdir(), "magi_laf_pdftotext")
        os.makedirs(tmp_dir, exist_ok=True)
        tmp_txt = os.path.join(tmp_dir, f"pdftotext_{int(time.time()*1000)}.txt")
        cmd = [PDFTOTEXT_BIN, "-enc", "UTF-8", "-f", "1", "-l", str(int(max_pages)), p, tmp_txt]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=int(timeout_sec))
        if r.returncode != 0:
            return ""
        try:
            with open(tmp_txt, "r", encoding="utf-8", errors="replace") as f:
                return (f.read() or "").strip()
        except Exception:
            return ""
    except Exception:
        return ""


def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _phone_digits(s: str) -> str:
    return re.sub(r"\D+", "", (s or ""))


def _parse_client_fields_from_text(
    text: str,
    client_name: str = "",
) -> Dict[str, str]:
    """從法扶申請書文字中擷取當事人聯絡資料（電話/地址/Email/身分證）。"""
    s = (text or "")
    if not s.strip():
        return {}
    out: Dict[str, str] = {}

    # Email：排除法扶網域（常是承辦資訊）
    for em in re.findall(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", s):
        e = (em or "").strip()
        if not e:
            continue
        if e.lower().endswith("@laf.org.tw"):
            continue
        out["email"] = e
        break

    # 身分證/統編
    m = re.search(r"(?:身分證字號|身份證字號|統一編號|統編|身分證號)[：:\s]*([A-Z][12]\d{8}|\d{8})", s, re.IGNORECASE)
    if m:
        out["tax_id"] = m.group(1).strip().upper()

    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    cname = (client_name or "").strip()

    # ── 電話：候選打分（申請人上下文加分；承辦/法扶/分機重扣） ──
    phone_cands: List[Tuple[float, str]] = []
    bad_ctx = [
        "法扶", "分會", "承辦", "專員", "律師", "事務所", "fax", "傳真", "基金會",
        "承辦人", "承辦專員", "聯絡窗口", "服務電話", "總機", "分機", "聯絡人",
        "代理人", "聯絡人姓名",
    ]
    good_ctx = [
        "申請人", "受扶助", "當事人", "本人", "聯絡電話", "電話1", "電話2", "手機",
        "行動電話", "住家電話",
    ]
    for i, ln in enumerate(lines):
        neigh = " ".join(lines[max(0, i - 1): min(len(lines), i + 2)])
        # 只看電話「前方」上下文判斷是否在聯絡人/代理人區段
        backward_ctx = " ".join(lines[max(0, i - 8): i])
        for m in re.finditer(r"(09\d{2}[-\s]?\d{3}[-\s]?\d{3}|0\d{1,2}[-\s]?\d{3,4}[-\s]?\d{3,4}(?:\s*(?:分機|#)\s*\d+)?)", ln):
            raw = (m.group(1) or "").strip()
            dg = _phone_digits(raw)
            if len(dg) < 8:
                continue
            score = 0.0
            if dg.startswith("09") and len(dg) == 10:
                score += 4.0
            if cname and (cname in ln or cname in neigh):
                score += 4.0
            if any(k in neigh for k in good_ctx):
                score += 2.5
            if any(k in ln.lower() for k in bad_ctx):
                score -= 6.0
            if "分機" in ln or "#" in raw:
                score -= 3.0
            if any(k in neigh for k in ["承辦", "專員", "基金會", "分會", "服務電話", "總機"]):
                score -= 4.0
            # 前方偵測「聯絡人/代理人」區段 → 大幅扣分（該號碼屬於聯絡人而非申請人）
            if any(k in backward_ctx for k in ["聯絡人", "聯絡人姓名", "代理人姓名"]):
                score -= 5.0
            if re.search(r"(申請人|受扶助人|當事人).{0,8}(電話|手機|聯絡)", neigh):
                score += 3.0
            phone_cands.append((score, raw))

    if phone_cands:
        phone_cands.sort(key=lambda x: x[0], reverse=True)
        best_score, best_phone = phone_cands[0]
        if best_score >= 1.0:
            out["phone"] = _normalize_spaces(best_phone).replace(" ", "")

    # ── 地址：通訊地址優先；排除「為送達地址...請自負其責」等說明文字 ──
    def _is_plausible_addr(x: str) -> bool:
        t = _normalize_spaces(x or "")
        if not t:
            return False
        bad_fragments = [
            "為送達地址", "虛偽陳報", "影響申請人權益", "請自負其責", "填寫說明",
            "注意事項", "本欄", "請勾選",
        ]
        if any(b in t for b in bad_fragments):
            return False
        if not re.search(r"[縣市鄉鎮區里村路街段巷弄號樓F]", t):
            return False
        if len(t) > 90 and not re.search(r"\d+號", t):
            return False
        return True

    def _extract_addr(marker: str) -> str:
        for i, ln in enumerate(lines):
            if marker not in ln:
                continue
            mm = re.search(rf"{re.escape(marker)}[：: ]*(.+)$", ln)
            addr = (mm.group(1).strip() if mm else ln.split(marker, 1)[-1].strip("：: ").strip())
            # PDF 表格常把標籤和值分到不同行，向下搜尋最多 10 行
            if not addr or addr in {"[ ]", "[]", "／", "/"} or not _is_plausible_addr(addr):
                for j in range(1, min(11, len(lines) - i)):
                    nxt = lines[i + j].strip()
                    if _is_plausible_addr(nxt):
                        addr = nxt
                        break
            if addr and addr not in {"[ ]", "[]"} and _is_plausible_addr(addr):
                return _normalize_spaces(addr)
        return ""

    addr = _extract_addr("通訊地址") or _extract_addr("住所地址") or _extract_addr("住址") or _extract_addr("地址")
    if addr:
        out["address"] = addr
    return out


def _scan_laf_forms_for_client_fields(case_folder: str, max_pdfs: int = 10) -> Dict[str, str]:
    root = (case_folder or "").strip()
    if not root or not os.path.isdir(root):
        return {}
    pdfs: List[str] = []
    preferred: List[str] = []
    try:
        for base, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.lower().endswith(".pdf"):
                    continue
                p = os.path.join(base, fn)
                if "法律扶助申請書" in fn or "法律扶助申請" in fn:
                    preferred.append(p)
                else:
                    norm = p.replace("\\", "/")
                    if ("/01_法扶資料/" in norm) or ("/02_開辦資料/" in norm):
                        pdfs.append(p)
    except Exception:
        return {}
    picked = (preferred + pdfs)[: max(1, int(max_pdfs))]
    merged: Dict[str, str] = {}
    for p in picked:
        txt = _pdftotext_extract(p, max_pages=3)
        fields = _parse_client_fields_from_text(txt)
        for k, v in fields.items():
            if v and not merged.get(k):
                merged[k] = v
    return merged


def _laf_marker_path(case_folder: str) -> str:
    return os.path.join(case_folder, "01_法扶資料", "_laf_case_number.txt")


def _write_laf_case_marker(case_folder: str, laf_case_number: str, log=None) -> None:
    n = (laf_case_number or "").strip()
    if not case_folder or not n:
        return
    try:
        p = _laf_marker_path(case_folder)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        if os.path.exists(p):
            return
        with open(p, "w", encoding="utf-8") as f:
            f.write(n + "\n")
        if log:
            log(f"  🏷️ 已寫入法扶案號標記: {n}")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6249, exc_info=True)


def _read_laf_case_marker(case_folder: str) -> str:
    try:
        p = _laf_marker_path(case_folder)
        if os.path.exists(p):
            return (open(p, "r", encoding="utf-8", errors="replace").read() or "").strip()
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6258, exc_info=True)
    return ""


def _find_duplicate_folders_by_laf_number(type_root: str, laf_case_number: str, max_scan: int = 2500) -> List[str]:
    root = (type_root or "").strip()
    n = (laf_case_number or "").strip()
    if not root or not n or not os.path.isdir(root):
        return []
    out: List[str] = []
    try:
        scanned = 0
        for ent in os.scandir(root):
            if scanned >= int(max_scan):
                break
            scanned += 1
            if not ent.is_dir():
                continue
            v = _read_laf_case_marker(ent.path)
            if v and v == n:
                out.append(ent.path)
    except Exception:
        return out
    return sorted(set(out))


def _mark_duplicate_folder(dup_folder: str, canonical_folder: str, log=None) -> None:
    try:
        d = (dup_folder or "").strip()
        c = (canonical_folder or "").strip()
        if not d or not c or not os.path.isdir(d):
            return
        if os.path.abspath(d) == os.path.abspath(c):
            return
        p = os.path.join(d, "DUPLICATE_OF.txt")
        if os.path.exists(p):
            return
        with open(p, "w", encoding="utf-8") as f:
            f.write("此資料夾疑似為重複案件資料夾（自動標記，未刪除任何檔案）。\n")
            f.write(f"建議使用的主資料夾（canonical）：{c}\n")
            f.write(f"標記時間：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        if log:
            log(f"  🧭 已標記重複資料夾: {d} -> {c}")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6302, exc_info=True)


def _discover_existing_case_folder(final_root: str, client_name: str, case_reason: str, case_stage: str = "") -> Dict[str, str]:
    root = (final_root or "").strip()
    cn = (client_name or "").strip()
    cn_key = re.sub(r"[\s\u3000·・•‧∙．｡。]+", "", cn).lower()
    cr = (case_reason or "").strip()
    cs = (case_stage or "").strip()
    if not root or not cn or not cr or not os.path.isdir(root):
        return {}
    best = {"score": 0.0, "folder": "", "case_number": ""}
    try:
        for ent in os.scandir(root):
            if not ent.is_dir():
                continue
            name = ent.name or ""
            name_key = re.sub(r"[\s\u3000·・•‧∙．｡。]+", "", name).lower()
            score = 0.0
            if cn and (cn in name or (cn_key and cn_key in name_key)):
                score += 2.5
            if cr and cr in name:
                score += 2.0
            if cs and cs in name:
                score += 1.0
            m = re.match(r"^(20\d{2}-\d{4})-", name)
            case_no = m.group(1) if m else ""
            if case_no:
                score += 0.6
            if score > best["score"]:
                best = {"score": score, "folder": ent.path, "case_number": case_no}
    except Exception:
        return {}
    if best["folder"] and best["score"] >= 4.0 and best["case_number"]:
        return {"folder": best["folder"], "case_number": best["case_number"]}
    return {}


_LAF_CASE_NO_RE = re.compile(r"(\d{7}-[A-Z]-\d{3})")


class OSCCaseCreator:
    """
    OSC (Paperclip) 案件建立器
    
    整合 OSC 的 DatabaseManager 建立案件
    """
    
    def __init__(self, db_manager=None, target_folder: str = None, log_callback=None):
        """
        初始化
        
        Args:
            db_manager: OSC 的 DatabaseManager 實例
            target_folder: 法扶資料存放位置
            log_callback: 日誌回呼
        """
        self.db_manager = db_manager
        self.target_folder = target_folder or './法扶資料'
        self.log = log_callback or print
        
        os.makedirs(self.target_folder, exist_ok=True)

    def dedupe_case_folders_by_laf_marker(self, max_scan_per_type: int = 2500) -> dict:
        """
        去重法扶案件資料夾（不刪除任何檔案）：
        - 以 _laf_case_number.txt 為準，同一 laf_case_number 若出現多個資料夾：
          - 選一個 canonical（優先較小的 case_number 前綴，例如 2026-0006）
          - 其他資料夾寫入 DUPLICATE_OF.txt（標記）
          - 若 DB 可用：將該案 `legal_aid_number` 補齊、並把 `folder_path` 指到 canonical（避免後續再用錯資料夾）
        """
        root = (self.target_folder or "").strip()
        if not root or not os.path.isdir(root):
            return {"ok": True, "skipped": True, "reason": "target_folder_missing"}

        def _case_no_from_folder(path: str) -> str:
            bn = os.path.basename(path.rstrip("/")) or ""
            m = re.match(r"^(20\d{2}-\d{4})-", bn)
            return m.group(1) if m else ""

        type_roots = [root]
        for sub in ["刑事", "民事", "行政", "消費者債務清理"]:
            p = os.path.join(root, sub)
            if os.path.isdir(p):
                type_roots.append(p)

        groups: Dict[str, List[dict]] = {}
        scanned = 0
        for tr in type_roots:
            try:
                n = 0
                for ent in os.scandir(tr):
                    if n >= int(max_scan_per_type):
                        break
                    n += 1
                    if not ent.is_dir():
                        continue
                    laf_no = _read_laf_case_marker(ent.path)
                    if not laf_no:
                        continue
                    cn = _case_no_from_folder(ent.path)
                    groups.setdefault(laf_no, []).append({"folder": ent.path, "case_number": cn})
                    scanned += 1
            except Exception:
                continue

        dup_groups = {k: v for k, v in groups.items() if isinstance(v, list) and len(v) > 1}
        if not dup_groups:
            return {"ok": True, "scanned": scanned, "duplicates": 0, "groups": 0}

        fixed = 0
        updated_db = 0
        for laf_no, items in dup_groups.items():
            # canonical: prefer smallest case_number; fallback to first path
            items_sorted = sorted(
                items,
                key=lambda it: (it.get("case_number") or "9999-9999", it.get("folder") or ""),
            )
            canonical = (items_sorted[0].get("folder") or "").strip()
            if not canonical:
                continue

            for it in items_sorted[1:]:
                dp = (it.get("folder") or "").strip()
                if dp and dp != canonical:
                    _mark_duplicate_folder(dp, canonical, log=self.log)
                    fixed += 1

            # Best-effort: patch DB so future flows use canonical.
            try:
                if self.db_manager and hasattr(self.db_manager, "execute_write"):
                    canonical_db = canonical
                    if hasattr(self.db_manager, "translate_path_to_canonical"):
                        try:
                            canonical_db = self.db_manager.translate_path_to_canonical(canonical)
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6438, exc_info=True)
                    # Update by case_number when present.
                    for it in items_sorted:
                        cn = (it.get("case_number") or "").strip()
                        if not cn:
                            continue
                        self.db_manager.execute_write(
                            "UPDATE `cases` SET "
                            "`legal_aid_number` = CASE WHEN `legal_aid_number` IS NULL OR `legal_aid_number` = '' THEN %s ELSE `legal_aid_number` END, "
                            "`folder_path` = %s, "
                            "`folder_name` = %s "
                            "WHERE `case_number` = %s",
                            (laf_no, canonical_db, os.path.basename(canonical.rstrip('/')), cn),
                        )
                        updated_db += 1
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6454, exc_info=True)

            try:
                _eventlog(
                    "laf:folder:dedupe",
                    ok=True,
                    payload={"laf_case_no": laf_no, "count": len(items_sorted), "canonical": os.path.basename(canonical.rstrip('/'))},
                    tags={"laf_case_no": laf_no},
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6464, exc_info=True)

        return {
            "ok": True,
            "scanned": scanned,
            "duplicates": sum(len(v) for v in dup_groups.values()),
            "groups": len(dup_groups),
            "marked": fixed,
            "db_updates": updated_db,
        }
    
    def _archive_files_to_folder(self, files: List[str], case_folder: str):
        """
        將下載的檔案歸檔到案件資料夾
        
        Args:
            files: 已下載的檔案列表
            case_folder: 目標案件資料夾
        """
        result = {
            "ok": True,
            "processed": 0,
            "new_files": [],
            "skipped_existing": [],
            "zip_backups": [],
            "zip_backup_skipped": [],
            "errors": [],
        }
        if not files:
            return result
        
        # 定義檔案分類規則
        def get_target_subfolder(fname):
            # 結案酬金領款單 → 03_結案資料
            if '結案酬金領款單' in fname or '變動審查通知書' in fname:
                return '03_結案資料'
            # (附條件)第二階段預付酬金領款單 → 02_開辦資料 (僅限「附條件」版本)
            if '附條件第二階段預付酬金領款單' in fname:
                return '02_開辦資料'
            # 其他全部 → 01_法扶資料 (包含預付酬金領款單、准予扶助證明書等)
            return '01_法扶資料'
        
        for file_path in files:
            if not os.path.exists(file_path):
                continue
            result["processed"] += 1
                
            filename = os.path.basename(file_path)
            
            # 如果是 ZIP 檔，進行解壓縮並分類
            if filename.lower().endswith('.zip'):
                self.log(f"    📦 解壓縮並分類: {filename}")
                try:
                    import zipfile
                    with zipfile.ZipFile(file_path, 'r') as zip_ref:
                        for member in zip_ref.infolist():
                            orig_filename = member.filename
                            try:
                                new_filename = orig_filename.encode('cp437').decode('big5')
                            except Exception:
                                try:
                                    new_filename = orig_filename.encode('cp437').decode('gbk')
                                except Exception:
                                    new_filename = orig_filename
                            
                            if member.is_dir() or new_filename.endswith('/'):
                                continue
                            
                            base_name = os.path.basename(new_filename)
                            if not base_name or base_name.startswith('.'):
                                continue
                            
                            target_sub = get_target_subfolder(base_name)
                            dest_folder = os.path.join(case_folder, target_sub)
                            os.makedirs(dest_folder, exist_ok=True)
                            target_path = os.path.join(dest_folder, base_name)
                            
                            # 檢查檔案是否已存在
                            if os.path.exists(target_path):
                                self.log(f"    ⏭️ 已存在，跳過: {base_name}")
                                result["skipped_existing"].append(target_path)
                                continue
                            
                            with open(target_path, "wb") as target_file, zip_ref.open(member) as source_file:
                                shutil.copyfileobj(source_file, target_file)
                            
                            self.log(f"    ✓ {base_name} → {target_sub}/")
                            result["new_files"].append(target_path)
                    
                    # 備份 ZIP 檔 (先檢查是否已有同名檔案)
                    laf_folder = os.path.join(case_folder, '01_法扶資料')
                    os.makedirs(laf_folder, exist_ok=True)
                    
                    # 清理檔名中的 Chrome 重複後綴 (N) 來比對
                    import re
                    base_zip_name = re.sub(r'\s*\(\d+\)\.zip$', '.zip', filename, flags=re.IGNORECASE)
                    
                    # 檢查是否已有相同基本名稱的 ZIP
                    existing_zips = [f for f in os.listdir(laf_folder) if f.lower().endswith('.zip')]
                    zip_already_exists = False
                    for existing_zip in existing_zips:
                        existing_base = re.sub(r'\s*\(\d+\)\.zip$', '.zip', existing_zip, flags=re.IGNORECASE)
                        if existing_base == base_zip_name:
                            zip_already_exists = True
                            self.log(f"    ⏭️ ZIP 已存在，跳過備份: {filename}")
                            result["zip_backup_skipped"].append(os.path.join(laf_folder, existing_zip))
                            break
                    
                    if not zip_already_exists:
                        dest = os.path.join(laf_folder, filename)
                        shutil.copy2(file_path, dest)
                        self.log(f"    ✓ 已備份 ZIP: {filename}")
                        result["zip_backups"].append(dest)
                    
                    # 依政策：預設不刪除任何檔案（避免誤刪/方便回溯）
                    _safe_remove(file_path, log=self.log)
                    
                except Exception as e:
                    self.log(f"    ❌ 解壓縮失敗: {e}，改為複製原始檔")
                    result["errors"].append({"file": filename, "error": str(e)})
                    laf_folder = os.path.join(case_folder, '01_法扶資料')
                    os.makedirs(laf_folder, exist_ok=True)
                    dest = os.path.join(laf_folder, filename)
                    if not os.path.exists(dest):
                        shutil.copy2(file_path, dest)
                        result["new_files"].append(dest)
                    else:
                        result["skipped_existing"].append(dest)
            else:
                # 一般檔案，直接分類
                target_sub = get_target_subfolder(filename)
                dest_folder = os.path.join(case_folder, target_sub)
                os.makedirs(dest_folder, exist_ok=True)
                dest_path = os.path.join(dest_folder, filename)
                if not os.path.exists(dest_path):
                    shutil.copy2(file_path, dest_path)
                    self.log(f"    ✓ {filename} → {target_sub}/")
                    result["new_files"].append(dest_path)
                else:
                    self.log(f"    ⏭️ 已存在，跳過: {filename}")
                    result["skipped_existing"].append(dest_path)
        
        self.log(f"  ✅ 檔案歸檔完成")
        return result

    def _staff_opening_folder(self, case_folder: str) -> str:
        return os.path.join(case_folder, "01_法扶資料", "專員來信")

    def postprocess_staff_email_attachments(self, files: List[str], case_folder: str):
        """
        專員來信附件後處理：
        - ZIP 會保留原檔，並解壓縮到 01_法扶資料/專員來信/_解壓/{zip_stem}/
        """
        if not files:
            return
        staff_root = self._staff_opening_folder(case_folder)
        try:
            os.makedirs(staff_root, exist_ok=True)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6623, exc_info=True)

        for fp in files:
            try:
                if not fp or not os.path.exists(fp):
                    continue
                if not str(fp).lower().endswith(".zip"):
                    continue

                import zipfile

                zip_name = os.path.basename(fp)
                zip_stem = os.path.splitext(zip_name)[0]
                out_dir = os.path.join(staff_root, "_解壓", zip_stem)
                os.makedirs(out_dir, exist_ok=True)

                with zipfile.ZipFile(fp, "r") as z:
                    for member in z.infolist():
                        orig_filename = member.filename
                        try:
                            new_filename = orig_filename.encode("cp437").decode("big5")
                        except Exception:
                            try:
                                new_filename = orig_filename.encode("cp437").decode("gbk")
                            except Exception:
                                new_filename = orig_filename

                        if member.is_dir() or new_filename.endswith("/"):
                            continue
                        base_name = os.path.basename(new_filename)
                        if not base_name or base_name.startswith("."):
                            continue
                        base_name = re.sub(r"[<>:\"/\\\\|?*]+", "_", base_name).strip()
                        if not base_name:
                            continue

                        dst = os.path.join(out_dir, base_name)
                        if os.path.exists(dst):
                            continue
                        with open(dst, "wb") as wf, z.open(member) as rf:
                            shutil.copyfileobj(rf, wf)

                self.log(f"    📦 已解壓 ZIP（保留原檔）: {zip_name}")
            except Exception as e:
                self.log(f"    ⚠️ ZIP 解壓縮失敗（可忽略）: {e}")

    def archive_staff_email_attachments(self, files: List[str], case_folder: str) -> List[str]:
        """
        法扶專員來信附件 → 01_法扶資料/專員來信
        - 不覆蓋既有檔案
        - MAGI_NO_DELETE=1 時僅複製，不刪除原檔
        """
        if not files:
            return []

        staff_root = self._staff_opening_folder(case_folder)
        os.makedirs(staff_root, exist_ok=True)

        archived: List[str] = []
        for fp in files:
            if not fp or not os.path.exists(fp):
                continue
            fn = os.path.basename(fp)
            fn = re.sub(r'[<>:"/\\\\|?*]+', "_", fn).strip() or "attachment.bin"
            dst = os.path.join(staff_root, fn)
            if os.path.exists(dst):
                continue
            _safe_move(fp, dst, log=self.log)
            if os.path.exists(dst):
                archived.append(dst)

        try:
            self.postprocess_staff_email_attachments(archived, case_folder)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6697, exc_info=True)

        return archived
    
    def create_case(self, case_info: LAFCaseInfo, files: List[str] = None) -> Optional[str]:
        """
        建立案件
        
        Args:
            case_info: 法扶案件資訊
            files: 已下載的檔案列表
            
        Returns:
            (OSC 案號, 案件資料夾路徑) 或 (None, None)
        """
        if not self.db_manager:
            self.log("❌ OSC DatabaseManager 未設定")
            return None, None
        
        self.log(f"🔨 [OSC] 開始建立案件資料: {case_info.client_name} ({case_info.laf_case_number})")
        
        try:
            # ★ 診斷點 A
            self.log(f"  [A] 開始檢查案件是否存在...")
            
            # ========== 檢查是否已存在相同案件 ==========
            # 優先使用「人名 + 案件種類 + 案由」判斷，法扶案號輔助驗證
            if hasattr(self.db_manager, 'check_laf_case_exists'):
                self.log(f"  [A1] 呼叫 check_laf_case_exists: {case_info.laf_case_number}, {case_info.client_name}, {case_info.case_type}, {case_info.case_reason}")
                existing_case = self.db_manager.check_laf_case_exists(
                    laf_case_number=case_info.laf_case_number,
                    client_name=case_info.client_name,
                    case_type=case_info.case_type,
                    case_reason=case_info.case_reason
                )
                self.log(f"  [A2] check_laf_case_exists 回傳: {existing_case}")
                if existing_case:
                    self.log(f"⚠️ [OSC] 案件已存在，跳過重複建立:")
                    self.log(f"   OSC 案號: {existing_case.get('case_number')}")
                    self.log(f"   當事人: {existing_case.get('client_name')}")
                    self.log(f"   資料夾: {existing_case.get('folder_path')}")
                    
                    # ★ 即使案件已存在，仍需處理下載的檔案 ★
                    if files:
                        existing_folder = existing_case.get('folder_path')
                        # 轉換為本機路徑
                        if hasattr(self.db_manager, 'translate_path_to_local'):
                            existing_folder = self.db_manager.translate_path_to_local(existing_folder)
                        if existing_folder:
                            existing_folder = translate_case_path_to_local(existing_folder)
                            self.log(f"  [PathMapper] 路徑轉換: {existing_folder}")
                        
                        # ★ [Smart Discovery] 智慧資料夾搜尋 ★
                        # 如果 DB 記錄路徑不存在，嘗試搜尋「案號開頭」的資料夾 (應對使用者改名/移動)
                        final_folder_to_use = None
                        
                        if existing_folder and os.path.exists(existing_folder):
                            # 路徑存在，直接使用
                            final_folder_to_use = existing_folder
                        else:
                            # 路徑不存在，嘗試搜尋
                            self.log(f"  ⚠️ DB路徑不存在 ({existing_folder})，嘗試智慧搜尋 (Smart Discovery)...")
                            osc_case_number = existing_case.get('case_number')
                            
                            # 嘗試搜尋的根目錄 (假設 existing_folder 的上一層)
                            search_root = None
                            if existing_folder:
                                search_root = os.path.dirname(existing_folder)
                            
                            # 如果上一層也不存在，嘗試默認路徑
                            if not search_root or not os.path.exists(search_root):
                                roots = preferred_case_roots(include_closed=False)
                                search_root = roots[0] if roots else MAC_SYNO_CASE_ROOT
                            
                            if search_root and os.path.exists(search_root):
                                self.log(f"     🔍 搜尋範圍: {search_root}")
                                # 包含搜尋子目錄 (例如 '刑事', '民事')
                                potential_roots = [search_root]
                                for sub in ['刑事', '民事', '行政', '消費者債務清理']:
                                    sub_path = os.path.join(search_root, sub)
                                    if os.path.exists(sub_path):
                                        potential_roots.append(sub_path)

                                discovered_path = None
                                for root in potential_roots:
                                    try:
                                        for item in os.listdir(root):
                                            # 比對邏輯：資料夾名稱以 "案號" 開頭 (例如 "2025-0120" 或 "2025-0120-林小美")
                                            if item.startswith(osc_case_number):
                                                full_path = os.path.join(root, item)
                                                if os.path.isdir(full_path):
                                                    discovered_path = full_path
                                                    break
                                    except:
                                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6791, exc_info=True)
                                    if discovered_path: break
                                
                                if discovered_path:
                                    self.log(f"     ✅ [SmartDiscovery] 找到替代資料夾: {discovered_path}")
                                    final_folder_to_use = discovered_path
                                else:
                                    self.log(f"     ❌ [SmartDiscovery] 搜尋失敗，無法找到案號 {osc_case_number} 的資料夾")
                            else:
                                self.log(f"     ❌ 搜尋根目錄不存在: {search_root}")

                        # 執行歸檔
                        if final_folder_to_use:
                            self.log(f"  📂 歸檔下載的檔案到: {final_folder_to_use}")
                            self._archive_files_to_folder(files, final_folder_to_use)
                            _write_laf_case_marker(final_folder_to_use, case_info.laf_case_number, log=self.log)
                            fields = _scan_laf_forms_for_client_fields(final_folder_to_use)
                            if fields:
                                try:
                                    self.db_manager.check_and_add_client(
                                        {
                                            "name": case_info.client_name,
                                            "phone": fields.get("phone", ""),
                                            "email": fields.get("email", ""),
                                            "address": fields.get("address", ""),
                                            "tax_id": fields.get("tax_id", ""),
                                        }
                                    )
                                except Exception:
                                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6819, exc_info=True)
                            try:
                                type_root = os.path.dirname(final_folder_to_use.rstrip("/"))
                                dups = _find_duplicate_folders_by_laf_number(type_root, case_info.laf_case_number)
                                if len(dups) > 1:
                                    canonical = final_folder_to_use if final_folder_to_use in dups else dups[0]
                                    for dp in dups:
                                        if dp != canonical:
                                            _mark_duplicate_folder(dp, canonical, log=self.log)
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6829, exc_info=True)
                        else:
                            self.log(f"  ⚠️ 無法找到目標資料夾，跳過檔案歸檔")
                    
                    return existing_case.get('case_number'), existing_case.get('folder_path')
            # ================================================================
            
            # ★ 診斷點 B
            self.log(f"  [B] 開始建立當事人...")
            
            # 1. 建立/取得當事人
            client_data = {"name": case_info.client_name, "phone": "", "email": "", "address": "", "tax_id": ""}
            try:
                if files:
                    for fp in files[:12]:
                        if not fp or not os.path.exists(fp):
                            continue
                        if fp.lower().endswith(".pdf"):
                            txt = _pdftotext_extract(fp, max_pages=2)
                            fields = _parse_client_fields_from_text(txt)
                            for k in ["phone", "email", "address", "tax_id"]:
                                if fields.get(k) and not client_data.get(k):
                                    client_data[k] = fields.get(k)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6853, exc_info=True)
            
            client_id = self.db_manager.check_and_add_client(client_data)
            self.log(f"✅ 當事人已建立/更新: {case_info.client_name} ({client_id})")
            
            # 2. 先決定分類與案由（用於「磁碟去重」），避免同一案件被建立多個資料夾
            type_subfolder_map = {
                '刑事': '刑事',
                '民事': '民事',
                '行政': '行政',
                '消費者債務清理': '消費者債務清理'
            }
            type_subfolder = type_subfolder_map.get(case_info.case_type, '')
            
            # 消費者債務清理案件案由一律為「更生」
            case_reason = case_info.case_reason
            if case_info.case_type == '消費者債務清理':
                case_reason = '更生'

            final_root_guess = os.path.join(self.target_folder, type_subfolder) if type_subfolder else self.target_folder

            # 2.1 先用「法扶案號 marker」做強一致去重（避免因案由/階段差異沒命中而重建資料夾）
            disk_hit = {}
            try:
                if case_info.laf_case_number:
                    hits = _find_duplicate_folders_by_laf_number(final_root_guess, case_info.laf_case_number, max_scan=2500)
                    if hits:
                        reuse_folder = hits[0]
                        m = re.match(r"^(20\d{2}-\d{4})-", os.path.basename(reuse_folder) or "")
                        reuse_case_no = m.group(1) if m else ""
                        if reuse_case_no:
                            disk_hit = {"folder": reuse_folder, "case_number": reuse_case_no}
                            self.log(f"⚠️ [去重] marker 已存在同案資料夾，改沿用：{os.path.basename(reuse_folder)}")
            except Exception:
                disk_hit = {}

            # 2.2 再用檔名模糊判斷（人名+案由+階段）做補強
            if not disk_hit:
                disk_hit = _discover_existing_case_folder(final_root_guess, case_info.client_name, case_reason, case_info.case_stage)
            if disk_hit and disk_hit.get("folder") and disk_hit.get("case_number"):
                reuse_folder = disk_hit["folder"]
                reuse_case_no = disk_hit["case_number"]
                self.log(f"⚠️ [去重] DB 未命中，但磁碟已存在疑似同案資料夾，改沿用：{os.path.basename(reuse_folder)}")
                try:
                    _write_laf_case_marker(reuse_folder, case_info.laf_case_number, log=self.log)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6899, exc_info=True)

                if files:
                    self._archive_files_to_folder(files, reuse_folder)
                    fields2 = _scan_laf_forms_for_client_fields(reuse_folder)
                    if fields2:
                        try:
                            self.db_manager.check_and_add_client(
                                {
                                    "name": case_info.client_name,
                                    "phone": fields2.get("phone", ""),
                                    "email": fields2.get("email", ""),
                                    "address": fields2.get("address", ""),
                                    "tax_id": fields2.get("tax_id", ""),
                                }
                            )
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6916, exc_info=True)

                try:
                    if hasattr(self.db_manager, "fetch_one") and hasattr(self.db_manager, "execute_write"):
                        row = self.db_manager.fetch_one("SELECT id, case_category FROM cases WHERE case_number = %s LIMIT 1", (reuse_case_no,), as_dict=True)
                        if not row:
                            import uuid
                            case_id = str(uuid.uuid4())
                            folder_path_for_db = reuse_folder
                            if hasattr(self.db_manager, 'translate_path_to_canonical'):
                                folder_path_for_db = self.db_manager.translate_path_to_canonical(reuse_folder)
                            notes = f"法扶案號: {case_info.laf_case_number}\n分會: {case_info.branch}\n"
                            q = """
                                INSERT INTO `cases` (
                                    `id`, `case_number`, `client_name`, `client_name_en`, `case_type`,
                                    `case_category`, `case_subject`, `case_reason`, `status`,
                                    `start_date`, `court_date`, `lawyer`, `folder_path`, `case_stage`,
                                    `court_case_number`, `court_division`, `court_name`, `legal_aid_status`,
                                    `legal_aid_number`, `notes`, `created_date`, `updated_date`
                                ) VALUES (
                                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                                )
                            """
                            params = (
                                case_id,
                                reuse_case_no,
                                case_info.client_name,
                                "",
                                case_info.case_type,
                                "法律扶助案件",
                                case_info.laf_case_type,
                                case_reason,
                                "進行中",
                                datetime.now().strftime('%Y-%m-%d'),
                                None,
                                "",
                                folder_path_for_db,
                                case_info.case_stage,
                                "",
                                "",
                                "",
                                "未開辦",
                                case_info.laf_case_number,
                                notes,
                            )
                            self.db_manager.execute_write(q, params)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6964, exc_info=True)

                return reuse_case_no, reuse_folder

            # 3. 生成系統案號（確認無 DB/磁碟重複後才生成）
            osc_case_number = self.db_manager.generate_case_number()
            if not osc_case_number or 'ERROR' in str(osc_case_number):
                self.log(f"❌ 無法生成案號: {osc_case_number}")
                return None, None
            
            self.log(f"📋 生成 OSC 案號: {osc_case_number}")
            
            # 4. 正確的資料夾命名格式
            # 格式: {系統案號}-{當事人}-{階段或類型}-{案由}
            if case_info.case_type == '消費者債務清理':
                folder_name = f"{osc_case_number}-{case_info.client_name}-消費者債務清理-更生"
            else:
                stage_or_type = case_info.case_stage or case_info.case_type
                folder_name = f"{osc_case_number}-{case_info.client_name}-{stage_or_type}-{case_reason}"
            
            # 清理非法字元
            illegal_chars = '<>:"|?*\\/'
            for char in illegal_chars:
                folder_name = folder_name.replace(char, '_')
            
            # 6. 【修正】建立在正確的子資料夾下
            if type_subfolder:
                final_root = os.path.join(self.target_folder, type_subfolder)
            else:
                final_root = self.target_folder
            
            case_folder = os.path.join(final_root, folder_name)
            os.makedirs(case_folder, exist_ok=True)
            
            self.log(f"📁 案件資料夾已建立: {folder_name}")
            
            # 7. 【新增】建立子資料夾結構
            subfolders = [
                '01_法扶資料', '02_開辦資料', '03_結案資料',
                '04_我方歷次書狀', '05_對方歷次書狀', '06_閱卷資料',
                '07_證據資料', '08_筆錄', '09_法院通知或程序裁定',
                '10_判決書', '11_回執', '12_信件往返'
            ]
            
            for sf in subfolders:
                subfolder_path = os.path.join(case_folder, sf)
                os.makedirs(subfolder_path, exist_ok=True)
            
            # 8. 處理檔案 (ZIP 解壓縮/分類)
            if files:
                # 定義檔案分類規則
                def get_target_subfolder(fname):
                    rules = {
                        '03_結案資料': ['結案酬金領款單', '變動審查通知書'],
                        '02_開辦資料': ['附條件第二階段預付酬金領款單'],
                        # 01_法扶資料 is default
                    }
                    for folder, keywords in rules.items():
                        if any(kw in fname for kw in keywords):
                            return folder
                    return '01_法扶資料'

                for file_path in files:
                    if not os.path.exists(file_path):
                        continue
                        
                    filename = os.path.basename(file_path)
                    
                    # 如果是 ZIP 檔，進行解壓縮並分類
                    if filename.lower().endswith('.zip'):
                        self.log(f"  📦 解壓縮並分類: {filename}")
                        try:
                            import zipfile
                            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                                # 解決中文檔名亂碼問題 (CP437 -> Big5/GBK)
                                for member in zip_ref.infolist():
                                    # 嘗試修正檔名編碼
                                    orig_filename = member.filename
                                    try:
                                        new_filename = orig_filename.encode('cp437').decode('big5')
                                    except Exception:
                                        try:
                                            new_filename = orig_filename.encode('cp437').decode('gbk')
                                        except Exception:
                                            new_filename = orig_filename
                                    
                                    # 忽略資料夾本身
                                    if member.is_dir() or new_filename.endswith('/'):
                                        continue
                                        
                                    # 確保路徑安全，只取檔名
                                    base_name = os.path.basename(new_filename)
                                    if not base_name or base_name.startswith('.'):
                                        continue
                                        
                                    # 決定目標資料夾
                                    target_sub = get_target_subfolder(base_name)
                                    dest_folder = os.path.join(case_folder, target_sub)
                                    target_path = os.path.join(dest_folder, base_name)
                                    
                                    # 寫入檔案
                                    with open(target_path, "wb") as target_file, zip_ref.open(member) as source_file:
                                        shutil.copyfileobj(source_file, target_file)
                                        
                                    self.log(f"    ✓ {base_name} → {target_sub}/")
                                    
                            # 最後將原始 ZIP 檔也放入 01_法扶資料 (先檢查是否已存在)
                            laf_folder_path = os.path.join(case_folder, '01_法扶資料')
                            
                            # 清理檔名中的 Chrome 重複後綴 (N) 來比對
                            import re
                            base_zip_name = re.sub(r'\s*\(\d+\)\.zip$', '.zip', filename, flags=re.IGNORECASE)
                            
                            # 檢查是否已有相同基本名稱的 ZIP
                            existing_zips = [f for f in os.listdir(laf_folder_path) if f.lower().endswith('.zip')] if os.path.exists(laf_folder_path) else []
                            zip_already_exists = False
                            for existing_zip in existing_zips:
                                existing_base = re.sub(r'\s*\(\d+\)\.zip$', '.zip', existing_zip, flags=re.IGNORECASE)
                                if existing_base == base_zip_name:
                                    zip_already_exists = True
                                    self.log(f"  ⏭️ ZIP 已存在，跳過備份: {filename}")
                                    break
                            
                            if not zip_already_exists:
                                dest = os.path.join(laf_folder_path, filename)
                                shutil.copy2(file_path, dest)
                                self.log(f"  ✓ 已備份 ZIP: {filename}")
                            
                            # 依政策：預設不刪除任何檔案（避免誤刪/方便回溯）
                            _safe_remove(file_path, log=self.log)
                                    
                        except Exception as e:
                            self.log(f"    ❌ 解壓縮失敗: {e}，改為複製原始檔")
                            dest = os.path.join(case_folder, '01_法扶資料', filename)
                            shutil.copy2(file_path, dest)
                    else:
                        # 一般檔案，直接分類
                        target_sub = get_target_subfolder(filename)
                        dest_folder = os.path.join(case_folder, target_sub)
                        dest_path = os.path.join(dest_folder, filename)
                        shutil.copy2(file_path, dest_path)
                        self.log(f"  ✓ {filename} → {target_sub}/")
                
                self.log(f"  ✅ 檔案處理完成")
            
            # 8.5 補齊法扶案號 marker + 當事人基本資料（住址/電話/Email/身分證字號）
            _write_laf_case_marker(case_folder, case_info.laf_case_number, log=self.log)
            fields2 = _scan_laf_forms_for_client_fields(case_folder)
            if fields2:
                try:
                    self.db_manager.check_and_add_client(
                        {
                            "name": case_info.client_name,
                            "phone": fields2.get("phone", ""),
                            "email": fields2.get("email", ""),
                            "address": fields2.get("address", ""),
                            "tax_id": fields2.get("tax_id", ""),
                        }
                    )
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7124, exc_info=True)

            # 8.6 去重：同一法扶案號在同一類型資料夾下若出現多個資料夾，標記非 canonical 的資料夾
            try:
                type_root = os.path.dirname(case_folder.rstrip("/"))
                dups = _find_duplicate_folders_by_laf_number(type_root, case_info.laf_case_number)
                if len(dups) > 1:
                    canonical = case_folder if case_folder in dups else dups[0]
                    for dp in dups:
                        if dp != canonical:
                            _mark_duplicate_folder(dp, canonical, log=self.log)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7136, exc_info=True)

            # 9. 建立案件資料
            notes = f"法扶案號: {case_info.laf_case_number}\n"
            notes += f"分會: {case_info.branch}\n"
            if case_info.staff_name:
                notes += f"承辦: {case_info.staff_name} {case_info.staff_phone}\n"
            if case_info.staff_email:
                notes += f"Email: {case_info.staff_email}\n"
            if case_info.client_alias:
                notes += f"當事人原名: {case_info.client_alias}\n"
            
            # 【修正】轉換為標準路徑格式 (用於跨電腦同步)
            folder_path_for_db = case_folder
            if hasattr(self.db_manager, 'translate_path_to_canonical'):
                folder_path_for_db = self.db_manager.translate_path_to_canonical(case_folder)
                
            # 如果還是原本的 K:/ 或 Mac 路徑，硬核轉換回 Z:\ (避免不同裝置無法讀取)
            if folder_path_for_db.startswith('K:/') or folder_path_for_db.startswith('K:\\'):
                folder_path_for_db = folder_path_for_db.replace('K:/', 'Z:\\').replace('K:\\', 'Z:\\').replace('/', '\\')
            elif sys.platform == 'darwin' and 'SynologyDrive' in folder_path_for_db:
                parts = folder_path_for_db.split('SynologyDrive', 1)
                folder_path_for_db = 'Z:\\lumi63181107' + parts[1]
                folder_path_for_db = folder_path_for_db.replace('/', '\\')
            
            # 10. 【修正】使用 SQL 直接插入 DB 記錄
            import uuid
            case_id = str(uuid.uuid4())
            
            query = """
                INSERT INTO `cases` (
                    `id`, `case_number`, `client_name`, `client_name_en`, `case_type`,
                    `case_category`, `case_subject`, `case_reason`, `status`,
                    `start_date`, `court_date`, `lawyer`, `folder_path`, `case_stage`,
                    `court_case_number`, `court_division`, `court_name`,
                    `folder_name`,
                    `legal_aid_number`, `legal_aid_status`,
                    `notes`, `created_date`, `updated_date`
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s,
                    %s, %s,
                    %s,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
            """
            params = (
                case_id,
                osc_case_number,
                case_info.client_name,
                '',  # client_name_en
                case_info.case_type,
                '法律扶助案件',  # case_category
                case_info.laf_case_type,  # case_subject
                case_reason,
                '進行中',  # status
                datetime.now().strftime('%Y-%m-%d'),  # start_date
                None,  # court_date
                '',  # lawyer
                folder_path_for_db,
                case_info.case_stage,
                '',  # court_case_number
                '',  # court_division
                '',  # court_name
                os.path.basename(case_folder.rstrip("/")),
                case_info.laf_case_number,
                '未開辦',  # legal_aid_status
                notes
            )
            
            # ★ 診斷日誌：檢查 SQL 參數數量
            placeholder_count = query.count('%s')
            param_count = len(params) if params else 0
            self.log(f"  [DEBUG] SQL 診斷: {placeholder_count} 個佔位符, {param_count} 個參數")
            if placeholder_count != param_count:
                self.log(f"  ❌ [ERROR] 參數數量不匹配！")
                for i, p in enumerate(params):
                    self.log(f"    [{i}] {type(p).__name__}: {repr(p)[:50]}")
            
            success = self.db_manager.execute_write(query, params)
            
            if success:
                self.log(f"✅ 案件已建立: {osc_case_number}")
                self.log(f"   類型: {case_info.case_type} ({case_info.case_stage})")
                self.log(f"   案由: {case_reason}")
                self.log(f"   資料夾: {case_folder}")
                return osc_case_number, case_folder
            else:
                self.log(f"❌ DB 記錄建立失敗")
                return osc_case_number, case_folder  # 資料夾已建立，回傳部分結果
            
        except Exception as e:
            self.log(f"❌ 建立案件失敗: {e}")
            traceback.print_exc()
        
        return None, None



# ==============================================================================
# 整合管理器
# ==============================================================================

class LAFAutomationManager:
    """
    法扶自動化整合管理器
    
    整合 Gmail 監控、LAF 網站自動化、OSC 案件建立
    """
    
    def __init__(self, config: Dict[str, Any], db_manager=None, 
                 discord_notifier=None, log_callback=None, gemini_client=None):
        """
        初始化
        
        Args:
            config: 設定字典
            db_manager: OSC 的 DatabaseManager
            discord_notifier: Discord 通知器
            log_callback: 日誌回呼
            gemini_client: Gemini AI 客戶端
        """
        self.config = config
        self.db_manager = db_manager
        self.discord = discord_notifier
        self.log = log_callback or print
        self.gemini_client = gemini_client
        
        # 元件
        self.gmail_monitor = None
        self.web_automation = None
        self.case_creator = None
        
        self._running = False
        self.task_queue = queue.Queue()
        self._worker_thread = None
        
        # ★ 已通知案件追蹤 (避免重複發送 Discord 通知)
        laf_config = config.get('laf', {})
        download_folder = laf_config.get('download_folder', './laf_downloads')
        self._notified_cases_file = os.path.join(download_folder, 'notified_laf_cases.json')
        self._notified_cases = self._load_notified_cases()
    
    def _load_notified_cases(self) -> set:
        """載入已通知的案件記錄"""
        if os.path.exists(self._notified_cases_file):
            try:
                with open(self._notified_cases_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.log(f"  📂 已載入 {len(data)} 個已通知的案件記錄")
                    return set(data)
            except Exception as e:
                self.log(f"  ⚠️ 載入已通知案件記錄失敗: {e}")
        return set()
    
    def _save_notified_cases(self):
        """儲存已通知的案件記錄"""
        try:
            os.makedirs(os.path.dirname(self._notified_cases_file) or '.', exist_ok=True)
            with open(self._notified_cases_file, 'w', encoding='utf-8') as f:
                json.dump(list(self._notified_cases), f)
        except Exception as e:
            self.log(f"  ⚠️ 儲存已通知案件記錄失敗: {e}")
    
    @property
    def is_running(self) -> bool:
        """是否正在運行"""
        return self._running
    
    def setup(self):
        """初始化各元件"""
        self.log("[LAF] 🚀 開始執行 setup()...")
        gmail_config = self.config.get('gmail', {})
        laf_config = self.config.get('laf', {})
        
        # 除錯：顯示收到的設定
        self.log(f"[LAF] 📋 設定檢查:")
        self.log(f"  - laf_config keys: {list(laf_config.keys()) if laf_config else 'None'}")
        
        # 取得 username 和 password — 優先從 env
        username = os.environ.get('MAGI_LAF_USERNAME', '') or laf_config.get('username', '')
        password = os.environ.get('MAGI_LAF_PASSWORD', '') or laf_config.get('password', '')
        
        self.log(f"  - username: {'已設定 (' + username[:3] + '***)' if username else '未設定'}")
        self.log(f"  - password: {'已設定' if password else '未設定'}")
        self.log(f"  - db_manager: {'已設定' if self.db_manager else '未設定'}")
        
        # Gmail 監控器
        try:
            cred_path = (gmail_config.get('credentials_path') or "").strip()
            token_path = (gmail_config.get('token_path') or "").strip()

            if not cred_path:
                cand = str(get_config_path("credentials.json"))
                if os.path.exists(cand):
                    cred_path = cand
            if not token_path:
                cand = str(get_config_path("laf_gmail_token.pickle"))
                if os.path.exists(cand):
                    token_path = cand
            if not token_path:
                token_path = "laf_gmail_token.pickle"

            if cred_path and os.path.exists(cred_path):
                self.gmail_monitor = LAFGmailMonitor(
                    credentials_path=cred_path,
                    token_path=token_path,
                    callback=self._on_new_case,
                    general_callback=self._on_general_email,
                    log_callback=self.log
                )
        except Exception as e:
            self.log(f"[LAF] ❌ Gmail 監控器初始化失敗: {e}")

        # LAF 網站自動化
        try:
            if username and password:
                self.log(f"[LAF] ✅ 建立 LAFWebAutomation (headless={laf_config.get('headless', False)})")
                self.web_automation = LAFWebAutomation(
                    username=username,
                    password=password,
                    download_folder=laf_config.get('download_folder', './laf_downloads'),
                    headless=laf_config.get('headless', False),  # 預設顯示瀏覽器
                    on_captcha_fail=self._on_captcha_fail,
                    log_callback=self.log,
                    base_url=laf_config.get('base_url', ''),
                    mock_mode=laf_config.get('mock_mode', False),
                    browser_profile_dir=laf_config.get('browser_profile_dir', ''),
                )
            else:
                self.log(f"[LAF] ⚠️ 缺少帳號密碼，LAFWebAutomation 未建立")
                self.log(f"[LAF]    提示：請檢查 legalbridge_config.json 中的 laf.username 和 laf.password")
        except Exception as e:
            self.log(f"[LAF] ❌ LAFWebAutomation 初始化失敗: {e}")

        # OSC 案件建立器 (這裡最容易出錯，加入詳細保護)
        self.log("[LAF] ⏳ 準備初始化 OSCCaseCreator...")
        if self.db_manager:
            try:
                # (V-MacFix) 轉換為本機路徑
                target_folder = laf_config.get('target_folder', './法扶資料')
                
                # 嘗試路徑轉換 (最可能卡住的地方)
                if hasattr(self.db_manager, 'translate_path_to_local'):
                    try:
                        self.log(f"  [DEBUG] 正在呼叫 translate_path_to_local, 原始路徑: {target_folder}")
                        translated_folder = self.db_manager.translate_path_to_local(target_folder)
                        self.log(f"  [DEBUG] 路徑轉換成功: {translated_folder}")
                        target_folder = translated_folder
                    except Exception as path_err:
                        self.log(f"  [ERROR] 路徑轉換發生錯誤 (使用原始路徑繼續): {path_err}")
                        
                # (MacFix) 二次檢查：如果還是在 K 槽且是 Mac，強制切換
                if sys.platform == 'darwin' and (target_folder.startswith('K:') or target_folder.startswith('k:') or '\\' in target_folder):
                    self.log(f"  [MacFix] 偵測到 Windows 路徑殘留 ({target_folder})，強制切換...")
                    try:
                        mac_base = self.config.get('paths', {}).get('mac_base_path')
                        # 如果沒有 mac_base_path，因為是在 Mac 上執行，我們不能依賴 court_docs_folder (那是 Windows 的 K:)
                        if not mac_base:
                            mac_base = MAC_SYNO_BASE
                            
                        # 組合路徑 (假設結構是 .../01_案件/法扶案件)
                        if '01_案件' not in mac_base:
                            mac_target = os.path.join(mac_base, '01_案件', '法扶案件')
                        else:
                            mac_target = os.path.join(mac_base, '法扶案件')
                            
                        self.log(f"  [MacFix] 已修正為: {mac_target}")
                        target_folder = mac_target
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7406, exc_info=True)
                
                self.case_creator = OSCCaseCreator(
                    db_manager=self.db_manager,
                    target_folder=target_folder,
                    log_callback=self.log
                )
                self.log("[LAF] ✅ OSCCaseCreator 初始化完成")
                
            except Exception as e:
                self.log(f"[LAF] ❌ OSCCaseCreator 初始化失敗: {e}")
                import traceback
                traceback.print_exc() # 印出完整錯誤堆疊
        else:
            self.log(f"[LAF] ⚠️ 缺少 db_manager，OSCCaseCreator 未建立")
            
        self.log("[LAF] 🎉 setup() 執行完畢")
    
    def start(self):
        """啟動服務"""
        self._running = True
        
        if self.gmail_monitor:
            # 0. 確保已認證
            if not self.gmail_monitor.service:
                self.gmail_monitor.authenticate()

            # 1. 先掃描今日信件
            if self.db_manager:
                # 定義檢查函式
                check_func = lambda mid: self.db_manager.check_laf_email_exists(mid)
                # 在背景執行掃描，避免卡住 GUI
                threading.Thread(
                    target=self.gmail_monitor.scan_today_emails,
                    args=(check_func,),
                    daemon=True
                ).start()
            
            # 2. 啟動定期監控
            interval = self.config.get('gmail', {}).get('check_interval', 300)
            
            # 取得一般信件監控規則
            general_config = self.config.get('general_email_monitor', {})
            general_rules = general_config.get('rules', []) if general_config.get('enabled', False) else [] 
            
            self.gmail_monitor.start_monitor(interval, check_immediately=False, general_rules=general_rules)
        
        # 3. 啟動工作執行緒
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker_thread.start()
        
        # 4. 啟動定期下載驗證 (背景執行)
        # 每 6 小時檢查一次 (21600秒)
        def periodic_verification():
            # 初次延遲 10 秒，避免與啟動時的資源競爭
            time.sleep(10)
            
            while self._running:
                try:
                    self.log("🔍 [排程] 開始執行檔案完整性檢查...")
                    self.check_and_download_missing_files()
                except Exception as e:
                    self.log(f"❌ [排程] 定期驗證失敗: {e}")
                
                # 等待 6 小時 (分段等待以便能優雅退出)
                check_interval = 21600 
                elapsed = 0
                while self._running and elapsed < check_interval:
                    time.sleep(10)
                    elapsed += 10
            
        threading.Thread(target=periodic_verification, daemon=True).start()
        
        self.log("✅ LAF 自動化服務已啟動")
    
    def stop(self):
        """停止服務"""
        self._running = False
        
        if self.gmail_monitor:
            self.gmail_monitor.stop_monitor()
        
        if self.web_automation:
            self.web_automation.close()
        
        # 停止工作執行緒
        if self._worker_thread:
            self.task_queue.put(None) # Sentinel
            self._worker_thread.join(timeout=5)
            
        self.log("✅ LAF 自動化服務已停止")
    
    def _on_captcha_fail(self):
        """驗證碼連續失敗回呼"""
        self.log("⚠️ 驗證碼連續識別失敗，請手動處理")
        
        if self.discord:
            self.discord.send_message(
                "⚠️ LAF 驗證碼識別失敗",
                "LAF 律師線上操作系統的驗證碼連續識別失敗，請手動登入處理。",
                color=0xff9900
            )
    
    def _on_new_case(self, case_info: LAFCaseInfo):
        """處理新案件"""
        try:
            # ★ 生成唯一識別碼 (用於追蹤是否已通知)
            notification_key = case_info.message_id or f"{case_info.laf_case_number}_{case_info.client_name}"
            
            # ★ 檢查是否已通知過 (避免重複發送 Discord)
            if notification_key in self._notified_cases:
                self.log(f"  ⏭️ 案件已通知過，跳過 Discord: {case_info.client_name} ({notification_key[-8:]}...)")
                return
            
            # 0. 記錄到資料庫 (避免重複處理)
            if self.db_manager:
                record_data = {
                    'message_id': case_info.message_id,
                    'subject': f"【{case_info.branch}】{case_info.client_name}-{case_info.case_type}",
                    'sender': case_info.sender,
                    'received_at': case_info.received_at,
                    'status': 'processing',
                    'laf_case_number': case_info.laf_case_number,
                    'created_case_id': None
                }
                self.db_manager.add_laf_email_record(record_data)

            # 發送 Discord 通知
            if self.discord:
                self.discord.send_message(
                    f"📧 法扶{case_info.notification_type}",
                    f"**分會:** {case_info.branch}\n"
                    f"**當事人:** {case_info.client_name}\n"
                    f"**法扶案號:** {case_info.laf_case_number}\n"
                    f"**案件類型:** {case_info.case_type} ({case_info.case_stage})\n"
                    f"**案由:** {case_info.case_reason}",
                    color=0x00ff00
                )
                # ★ 標記為已通知
                self._notified_cases.add(notification_key)
                self._save_notified_cases()
            
            # 如果需要下載且設定了自動處理
            auto_create = self.config.get('laf', {}).get('auto_create_case')
            self.log(f"  DEBUG: needs_download={case_info.needs_download}, auto_create_case={auto_create}")
            
            if case_info.needs_download and auto_create:
                self.log(f"  🤖 [自動化] 偵測到需下載文件，已加入佇列等待處理: {case_info.client_name}")
                self.task_queue.put(case_info)
            elif not case_info.needs_download and case_info.has_attachment:
                self.log(f"  📎 [自動化] 偵測到信件內含附件，嘗試下載並建案...")
                
                # ★ 先下載附件
                downloaded_files = []
                if self.gmail_monitor and case_info.message_id:
                    # 取得目標資料夾路徑
                    target_folder = self.config.get('laf', {}).get('target_folder', './法扶資料')
                    temp_download_folder = os.path.join(target_folder, '_temp_attachments')
                    
                    self.log(f"  📥 正在下載信件附件...")
                    downloaded_files = self.gmail_monitor.download_attachments_by_msg_id(
                        case_info.message_id, temp_download_folder
                    )
                    self.log(f"  📎 共下載 {len(downloaded_files)} 個附件")
                
                # 有附件時建案（附件會自動歸檔）
                if self.case_creator and self.config.get('laf', {}).get('auto_create_case'):
                    osc_case_number = self.case_creator.create_case(case_info, downloaded_files)
                    
                    if osc_case_number and self.discord:
                        self.discord.send_message(
                            "✅ 法扶案件已自動建立",
                            f"**OSC 案號:** {osc_case_number}\n"
                            f"**當事人:** {case_info.client_name}\n"
                            f"**附件:** {len(downloaded_files)} 個檔案已歸檔",
                            color=0x00ff00
                        )
        
        except Exception as e:
            self.log(f"❌ 處理新案件失敗: {e}")

    def _on_general_email(self, email_info: GeneralEmailInfo):
        """處理一般信件"""
        try:
            # 發送 Discord 通知
            if self.discord:
                self.discord.send_message(
                    f"📧 {email_info.rule_name}",
                    f"**主旨:** {email_info.subject}\n"
                    f"**寄件者:** {email_info.sender}\n"
                    f"**附件:** {'有' if email_info.has_attachment else '無'}",
                    color=0x0099ff
                )
            
            # 如果有附件且有設定目標資料夾，則下載
            if email_info.has_attachment and email_info.target_subfolder:
                # 取得一般信件下載根目錄
                general_config = self.config.get('general_email_monitor', {})
                target_root = general_config.get('general_download_folder', '')
                
                # 如果未設定，預設為法扶資料夾下的 "一般信件"
                if not target_root:
                    laf_root = self.config.get('laf', {}).get('target_folder', './法扶資料')
                    target_root = os.path.join(laf_root, '一般信件')
                
                # (V-MacFix) 轉換為本機路徑
                if self.db_manager and hasattr(self.db_manager, 'translate_path_to_local'):
                    target_root = self.db_manager.translate_path_to_local(target_root)
                
                # 如果 target_subfolder 是絕對路徑，直接使用
                if os.path.isabs(email_info.target_subfolder):
                    target_folder = email_info.target_subfolder
                else:
                    target_folder = os.path.join(target_root, email_info.target_subfolder)

                # 處理路徑中的變數 (例如 {Sender})
                sender_name = email_info.sender.split('<')[0].strip().replace('"', '')
                # 移除檔名非法字元
                sender_name = re.sub(r'[<>:"/\\|?*]', '_', sender_name)
                target_folder = target_folder.replace('{Sender}', sender_name)

                # --- 法扶專員來信（不在各類通知下載）→ 直接進 01_法扶資料/專員來信 ---
                # 僅在「可辨識法扶案號」且「可定位案件資料夾」時啟用，避免誤歸檔。
                staff_case_folder = None
                staff_mode = False
                try:
                    sender_l = (email_info.sender or "").lower()
                    if "@laf.org.tw" in sender_l or "laf.org.tw" in sender_l:
                        text = " ".join([email_info.subject or "", email_info.snippet or "", email_info.body or ""])
                        m = _LAF_CASE_NO_RE.search(text)
                        laf_no = m.group(1) if m else ""
                        if laf_no:
                            staff_case_folder = self._resolve_case_folder_by_laf_number(laf_no)
                            if staff_case_folder:
                                target_folder = os.path.join(staff_case_folder, "01_法扶資料", "專員來信")
                                staff_mode = True
                except Exception:
                    staff_case_folder = None
                    staff_mode = False

                self.log(f"  📥 [一般信件] 準備下載附件至: {target_folder}")
                
                downloaded = self.gmail_monitor.download_attachments(email_info, target_folder)

                if downloaded and staff_mode and staff_case_folder and self.case_creator:
                    # 下載位置已在案件資料夾中，做 ZIP 後處理（保留原檔）
                    try:
                        self.case_creator.postprocess_staff_email_attachments(downloaded, staff_case_folder)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7655, exc_info=True)
                
                if downloaded and self.discord:
                    # 嘗試轉換回 Windows 路徑供顯示 (方便使用者複製)
                    display_path = target_folder
                    if self.db_manager and hasattr(self.db_manager, 'translate_path_to_canonical'):
                        display_path = self.db_manager.translate_path_to_canonical(target_folder)
                        
                    self.discord.send_message(
                        "✅ 附件下載完成",
                        f"**來源:** {email_info.subject}\n"
                        f"**下載數量:** {len(downloaded)}\n"
                        f"**儲存位置:** `{display_path}`"
                        + ("\n**歸檔:** 已存入該案 `01_法扶資料/專員來信`" if staff_mode else ""),
                        color=0x00ff00
                    )
                    
        except Exception as e:
            self.log(f"❌ 處理一般信件失敗: {e}")
            traceback.print_exc()

    def _resolve_case_folder_by_laf_number(self, laf_case_number: str) -> Optional[str]:
        """
        透過法扶案號定位案件資料夾（優先 DB，其次用 _laf_case_number.txt marker 掃描）。
        """
        n = (laf_case_number or "").strip()
        if not n or not self.case_creator:
            return None

        # 1) DB lookup
        folder = ""
        if self.db_manager and hasattr(self.db_manager, "check_laf_case_exists"):
            rec = None
            try:
                rec = self.db_manager.check_laf_case_exists(n, "", "", "")
            except Exception:
                rec = None
            if isinstance(rec, dict):
                folder = (rec.get("folder_path") or rec.get("folder") or "").strip()
                if folder and hasattr(self.db_manager, "translate_path_to_local"):
                    try:
                        folder = self.db_manager.translate_path_to_local(folder)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7698, exc_info=True)

        # 二次修正：處理可能殘留的 Windows 路徑（Mac）
        try:
            if folder:
                folder = translate_case_path_to_local(folder)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7705, exc_info=True)

        if folder and os.path.isdir(folder):
            return folder

        # 2) Marker scan (bounded)
        try:
            roots = [self.case_creator.target_folder]
            for sub in ["刑事", "民事", "行政", "消費者債務清理"]:
                p = os.path.join(self.case_creator.target_folder, sub)
                if os.path.isdir(p):
                    roots.append(p)
            for r in roots:
                hits = _find_duplicate_folders_by_laf_number(r, n, max_scan=2500)
                if hits:
                    return hits[0]
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7722, exc_info=True)

        return None
    
    def _worker_loop(self):
        """工作執行緒迴圈 (序列化處理)"""
        while self._running:
            try:
                case_info = self.task_queue.get()
                if case_info is None: # Sentinel
                    break
                
                self.log(f"⚙️ 開始處理佇列任務: {case_info.client_name}")
                self._auto_process(case_info)
                self.task_queue.task_done()
                
            except Exception as e:
                self.log(f"❌ 工作執行緒錯誤: {e}")

    def _auto_process(self, case_info: LAFCaseInfo):
        """自動處理案件"""
        try:
            if not self.web_automation:
                self.log("❌ [自動化] Web Automation 未初始化，無法執行下載")
                return {"success": False, "error": "web_automation_not_ready"}
            
            self.log(f"🚀 [自動化] 啟動瀏覽器抓取流程: {case_info.laf_case_number}")
            
            # 登入
            if not self.web_automation.login():
                if self.discord:
                    self.discord.send_message(
                        "❌ LAF 登入失敗",
                        f"無法自動下載案件 {case_info.laf_case_number} 的文件",
                        color=0xff0000
                    )
                else:
                    # 僅在卡住時通知 LINE（避免正常流程干擾）
                    try:
                        from line_notifier import LAFNotifier
                        LAFNotifier().notify_admin(
                            "❌ LAF 登入失敗（自動化已暫停）\n"
                            f"案件：{getattr(case_info, 'client_name', '')} / {getattr(case_info, 'laf_case_number', '')}\n"
                            "請確認法扶系統是否需要人工處理（含驗證碼/帳密/異常登入）。"
                        )
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7768, exc_info=True)
                return {"success": False, "error": "login_failed"}
            
            # 下載檔案
            files = self.web_automation.download_case_files(case_info.laf_case_number)
            
            # 建立案件
            # 建立案件
            if self.case_creator:
                osc_case_number, case_folder = self.case_creator.create_case(case_info, files)
                
                if osc_case_number and self.discord:
                    # 嘗試轉換回 Windows 路徑供顯示
                    display_path = case_folder
                    if self.db_manager and hasattr(self.db_manager, 'translate_path_to_canonical'):
                        display_path = self.db_manager.translate_path_to_canonical(case_folder)
                        
                    self.discord.send_message(
                        "✅ 法扶案件已自動建立",
                        f"**OSC 案號:** {osc_case_number}\n"
                        f"**當事人:** {case_info.client_name}\n"
                        f"**下載檔案:** {len(files)} 個\n"
                        f"**儲存位置:** `{display_path}`",
                        color=0x00ff00
                    )
                return {
                    "success": True,
                    "laf_case_number": getattr(case_info, "laf_case_number", ""),
                    "client_name": getattr(case_info, "client_name", ""),
                    "downloaded_files": len(files or []),
                    "osc_case_number": osc_case_number,
                    "case_folder": case_folder,
                }
            else:
                # 沒有 case_creator 時仍回傳下載結果，避免整體流程被誤判失敗
                return {
                    "success": True,
                    "laf_case_number": getattr(case_info, "laf_case_number", ""),
                    "client_name": getattr(case_info, "client_name", ""),
                    "downloaded_files": len(files or []),
                    "warning": "case_creator_missing",
                }
        
        except Exception as e:
            self.log(f"❌ 自動處理失敗: {e}")
            return {"success": False, "error": str(e)}
        finally:
            # 保持瀏覽器開啟或關閉視需求而定，這裡選擇關閉以節省資源
            # 但因為是序列化處理，也可以考慮保持 session
            if self.web_automation:
                self.web_automation.close()
    
    def manual_process(self, laf_case_number: str, client_name: str = "") -> Optional[str]:
        """
        手動處理指定案件
        
        Args:
            laf_case_number: 法扶案號
            client_name: 當事人姓名（可選）
            
        Returns:
            OSC 案號
        """
        case_info = LAFCaseInfo()
        case_info.laf_case_number = laf_case_number
        case_info.client_name = client_name or "待確認"
        case_info.case_type = "民事"
        case_info.case_stage = "一審"
        case_info.case_reason = ""
        
        try:
            if self.web_automation:
                if self.web_automation.login():
                    files = self.web_automation.download_case_files(laf_case_number)
                    
                    if self.case_creator:
                        osc_case_number, _ = self.case_creator.create_case(case_info, files)
                        return osc_case_number
        finally:
            if self.web_automation:
                self.web_automation.close()
        
        return None

    def _refine_case_info_with_gemini(self, case_info: Dict[str, Any]) -> Dict[str, Any]:
        """
        使用 CASPER 優化案件資訊判斷 (結合資料庫記錄；保留函式名以相容舊流程)
        
        Args:
            case_info: 原始案件資訊
            
        Returns:
            修正後的案件資訊
        """
        try:
            client_name = case_info.get('client_name', '')
            case_reason = case_info.get('case_reason', '')
            current_type = case_info.get('case_type', '')
            current_stage = case_info.get('case_stage', '')
            
            # 查詢資料庫中的現有案件
            existing_cases_str = "無"
            if self.db_manager:
                try:
                    query = """
                        SELECT case_number, case_type, case_stage, case_reason
                        FROM cases
                        WHERE client_name = %s
                          AND case_category IN ('法律扶助案件', '法扶案件')
                        ORDER BY created_date DESC
                        LIMIT 5
                    """
                    results = self.db_manager.fetch_all(query, (client_name,), as_dict=True)
                    if results:
                        cases_list = []
                        for r in results:
                            cases_list.append(f"- 案號: {r['case_number']}, 類型: {r['case_type']}, 階段: {r['case_stage']}, 案由: {r['case_reason']}")
                        existing_cases_str = "\n".join(cases_list)
                except Exception as e:
                    self.log(f"   ⚠️ 查詢 DB 失敗: {e}")

            prompt = f"""
            請協助判斷以下法律扶助案件的正確「案件類型」與「案件階段」。
            請參考該當事人在資料庫中的現有案件，判斷這是否為同一案件的後續階段，或是新案件。
            
            新案件資訊：
            - 當事人：{client_name}
            - 原始案由：{case_reason}
            - 初步判斷類型：{current_type}
            - 初步判斷階段：{current_stage}
            
            資料庫中現有案件：
            {existing_cases_str}
            
            判斷邏輯：
            1. 若新案件案由包含「再審」、「非常上訴」，且資料庫中有對應的原審案件（例如案由相同或相關），請標記為「刑事/再審」或「刑事/非常上訴」。
            2. 若新案件案由包含「消費者債務清理」、「更生」、「清算」，類型應為「消費者債務清理」。
            3. 若資料庫中有完全相同的案件（案由、階段皆同），請維持與資料庫一致的類型與階段。
            4. 若無法確定，請根據台灣法律實務判斷。
            
            請以 JSON 格式回應，包含兩個欄位：case_type, case_stage。
            範例：{{"case_type": "刑事", "case_stage": "再審"}}
            """
            
            response_text = ""
            if self.gemini_client:
                # 允許注入任何具備 generate_content(prompt)->str 的 client（Gemini/Ollama/CASPER proxy）
                response_text = self.gemini_client.generate_content(prompt) or ""
            else:
                # 預設走 CASPER（三哲人分散式推理）
                try:
                    from casper_tools_client import casper_chat
                    r = casper_chat(prompt, timeout_sec=120)
                    response_text = (r.get("response") or "") if isinstance(r, dict) and r.get("success") else ""
                except Exception as e:
                    self.log(f"   ⚠️ CASPER 判斷失敗: {e}")
                    response_text = ""

            if response_text:
                import json
                # 嘗試解析 JSON
                text = str(response_text).strip()
                if '```json' in text:
                    text = text.split('```json')[1].split('```')[0]
                elif '```' in text:
                    text = text.split('```')[1].split('```')[0]
                
                result = json.loads(text)
                new_type = result.get('case_type')
                new_stage = result.get('case_stage')
                
                if new_type and new_stage:
                    self.log(f"   🤖 CASPER 修正 (參考 DB): {current_type}({current_stage}) -> {new_type}({new_stage})")
                    case_info['case_type'] = new_type
                    case_info['case_stage'] = new_stage
                    
        except Exception as e:
            self.log(f"   ⚠️ CASPER 判斷失敗: {e}")
            
        return case_info

    def _parse_existing_folder_name(self, folder_name: str) -> Dict[str, str]:
        """
        解析現有資料夾名稱，提取案件資訊
        
        資料夾格式: 2025-0119-林文忠-偵查-傷害
        
        Args:
            folder_name: 資料夾名稱
            
        Returns:
            {'case_number': ..., 'client_name': ..., 'case_stage': ..., 'case_reason': ...}
        """
        parts = folder_name.split('-')
        result = {
            'case_number': '',
            'client_name': '',
            'case_stage': '',
            'case_reason': '',
            'case_type': ''
        }
        
        if len(parts) >= 3:
            # 格式: 案號(parts[0])-序號(parts[1] if numeric)-當事人-階段-案由
            # 或: 案號-當事人-類型-案由 (消費者債務清理)
            
            # 找出案號部分 (通常是 YYYY-NNNN)
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                result['case_number'] = f"{parts[0]}-{parts[1]}"
                remaining = parts[2:]
            else:
                result['case_number'] = parts[0]
                remaining = parts[1:]
            
            if len(remaining) >= 1:
                result['client_name'] = remaining[0]
            
            if len(remaining) >= 2:
                # 判斷是否為消費者債務清理
                if remaining[1] == '消費者債務清理':
                    result['case_type'] = '消費者債務清理'
                    result['case_stage'] = '其他'
                    if len(remaining) >= 3:
                        result['case_reason'] = '-'.join(remaining[2:])
                else:
                    result['case_stage'] = remaining[1]
                    if len(remaining) >= 3:
                        result['case_reason'] = '-'.join(remaining[2:])
        
        return result

    def check_and_download_missing_files(self):
        """
        檢查並下載缺失的檔案 (下載驗證功能)
        
        邏輯：
        1. 掃描下載頁面取得所有可下載案件（解析當事人姓名）
        2. 用當事人姓名在目標資料夾中尋找對應的案件資料夾
        3. 若找不到資料夾，查詢 DB 是否有該當事人的案件
        4. 若 DB 也沒有，使用 OSC 邏輯自動建立案件記錄和資料夾
        5. 檢查子資料夾（法扶資料、結案資料、開辦資料）是否有缺失檔案
        6. 若缺失則下載並直接放到正確的子資料夾（不經過歸檔程式）
        
        資料夾命名規則：
        - 一般案件：2025-0119-林文忠-偵查-傷害
        - 消債案件：2025-0087-[當事人L]-消費者債務清理-更生
        
        檔案分類規則：
        - 01_法扶資料/：接案通知書、委任狀、法律扶助申請書、案件概述單、資力詢問表、審查表、准予扶助證明書、預付酬金領款單、結案回報書
        - 03_結案資料/：結案酬金領款單
        - 02_開辦資料/：附條件第二階段預付酬金領款單
        """
        if not self.web_automation:
            self.log("❌ web_automation 未初始化，無法執行下載驗證")
            return

        self.log("🔍 [下載驗證] 開始執行完整檢查...")
        
        try:
            # 1. 登入並掃描
            if not self.web_automation.login():
                self.log("❌ [下載驗證] 登入失敗")
                return
                
            cases = self.web_automation.get_downloadable_cases()
            
            if not cases:
                self.log("📊 [下載驗證] 沒有可下載的案件")
                return
            
            self.log(f"📊 [下載驗證] LAF 網站找到 {len(cases)} 個可下載案件")
            
            # 取得目標資料夾根路徑
            target_root = self.config.get('laf', {}).get('target_folder', '')

            # (MacFix) 強制修正 macOS 路徑
            if sys.platform == 'darwin':
                # 如果 target_root 是 Windows 格式 (K:/ 或包含反斜線)，則重新計算
                if target_root.lower().startswith('k:') or '\\' in target_root:
                    mac_base = self.config.get('paths', {}).get('mac_base_path')
                    if not mac_base:
                        mac_base = MAC_SYNO_CASE_ROOT
                    
                    if mac_base:
                        # 預設把法扶案件放在 '法扶案件' 子目錄
                        target_root = os.path.join(mac_base, '法扶案件')
                        self.log(f"🔧 [Mac修正] 將 target_root 重導向至: {target_root}")

            # (V-MacFix) 轉換為本機路徑 (Fallback)
            if self.db_manager and hasattr(self.db_manager, 'translate_path_to_local'):
                target_root = self.db_manager.translate_path_to_local(target_root)
                
            if not target_root:
                self.log("❌ [下載驗證] 未設定 target_folder")
                return
            
            # 2. 預先掃描目標資料夾，建立當事人姓名 -> 資料夾路徑的對應
            # (V3) 同時記錄案由和階段資訊
            client_folder_map = {}
            if os.path.exists(target_root):
                self.log(f"🔍 [下載驗證] 掃描目標資料夾: {target_root}")
                try:
                    for folder_name in os.listdir(target_root):
                        folder_path = os.path.join(target_root, folder_name)
                        if not os.path.isdir(folder_path):
                            continue
                        
                        # (V3) 使用新的解析函式
                        folder_info = self._parse_existing_folder_name(folder_name)
                        c_name = folder_info.get('client_name', '')
                        
                        if not c_name:
                            continue
                        
                        if c_name not in client_folder_map:
                            client_folder_map[c_name] = []
                        
                        client_folder_map[c_name].append({
                            'folder_name': folder_name,
                            'folder_path': folder_path,
                            'case_stage': folder_info.get('case_stage', ''),
                            'case_reason': folder_info.get('case_reason', ''),
                            'case_type': folder_info.get('case_type', '')
                        })
                except Exception as e:
                    self.log(f"⚠️ 掃描目標資料夾失敗: {e}")
            
            self.log(f"📊 [下載驗證] 找到 {len(client_folder_map)} 位當事人的資料夾")
            
            # =========================================================================
            # 第一階段：快速掃描與資料建立 (Batch Phase 1: Metadata & Folders)
            # =========================================================================
            self.log(f"\n🚀 [第一階段] 開始快速掃描與建立資料夾...")
            
            for i, case_data in enumerate(cases):
                # (V-Gemini 已移除) 先前使用 Gemini 優化案件資訊，但因安全機制頻繁失敗，改為直接使用解析結果
                
                case_number = case_data['case_number']  # 法扶案號
                client_name = case_data['client_name']
                case_type = case_data.get('case_type', '民事')
                case_stage = case_data.get('case_stage', '一審')
                case_reason = case_data.get('case_reason', '')
                file_list = case_data.get('file_list', [])
                
                self.log(f"  👀 [{i+1}/{len(cases)}] 掃描: {client_name} ({case_number}) {case_type}/{case_reason}")
                
                if not client_name or client_name == "未知當事人":
                    self.log(f"     ⚠️ 無法解析當事人姓名，跳過")
                    continue
                
                # 4. 用當事人姓名找對應的資料夾
                matching_folders = client_folder_map.get(client_name, [])
                
                # --- [修改點 1] 步驟 A: 姓名的模糊搜尋 ---
                # 如果精確姓名找不到，才嘗試模糊搜尋，把該當事人所有可能的資料夾都找出來
                if not matching_folders:
                    for existing_name, folders in client_folder_map.items():
                        # 只要名字有包含關係 (例如 "黃日霖" in "黃日霖 HOANG...")
                        if client_name in existing_name or existing_name in client_name:
                            # self.log(f"     ℹ️ 姓名模糊匹配成功: {client_name} <-> {existing_name}")
                            matching_folders.extend(folders)

                target_folder = None

                # --- [修改點 2] 步驟 B: 案由與階段的比對 (Relaxed & Strict) ---
                if matching_folders:
                    # 第一輪: 嚴格匹配 (Strict Match)
                    for folder_info in matching_folders:
                        folder_name = folder_info['folder_name']
                        folder_stage = folder_info.get('case_stage', '')
                        folder_reason = folder_info.get('case_reason', '')
                        folder_type = folder_info.get('case_type', '')

                        # 規則 1: 消費者債務清理
                        if case_type == '消費者債務清理' and folder_type == '消費者債務清理':
                             self.log(f"     ✅ [Strict] 匹配到消債案件: {folder_name}")
                             target_folder = folder_info
                             break

                        # 規則 2: 一般案件
                        stage_match = (folder_stage == case_stage)
                        
                        # 案由必須有包含關係
                        reason_match = False
                        if case_reason and folder_reason:
                            reason_match = (case_reason in folder_reason) or (folder_reason in case_reason)
                        elif not case_reason and not folder_reason:
                            reason_match = True
                            
                        if stage_match and reason_match:
                            self.log(f"     ✅ [Strict] 匹配到現有資料夾: {folder_name}")
                            target_folder = folder_info
                            break
                    
                    # ★ [Relaxed Match] 第二輪: 寬鬆匹配 (修正版)
                    # 修正依據: 使用者回饋「審級不同就是不同案件」，因此必須堅持審級 (Case Stage) 一致。
                    # 但針對「案由」(Case Reason) 可以寬鬆，例如 "勞雇契約" vs "勞雇契約等" 或完全不同的描述但同一案件。
                    
                    if not target_folder:
                        self.log(f"     ⚠️ 嚴格匹配失敗，嘗試同階段寬鬆匹配...")
                        
                        # 篩選出「階段相同」的候選資料夾
                        # 注意：如果 case_stage 是空字串 (有些舊資料可能是空的)，則也要考慮
                        same_stage_candidates = []
                        for f in matching_folders:
                            f_stage = f.get('case_stage', '')
                            # 比對階段：如果任一方為空，或是完全相等
                            if (f_stage == case_stage) or (not f_stage and not case_stage):
                                same_stage_candidates.append(f)
                        
                        if same_stage_candidates:
                            # 找到同階段的資料夾 (忽略案由差異)
                            # 如果只有一個，直接選用
                            if len(same_stage_candidates) == 1:
                                best_candidate = same_stage_candidates[0]
                                self.log(f"     ✅ [Relaxed] 找到同階段資料夾 (忽略案由差異): {best_candidate['folder_name']}")
                                target_folder = best_candidate
                            else:
                                # 如果有多個同階段資料夾 (罕見，除非同階段有多個案子)，選一個案由最像的，或是最新的
                                # 這裡簡單選第一個，並記錄警告
                                best_candidate = same_stage_candidates[0]
                                self.log(f"     ✅ [Relaxed] 找到多個同階段資料夾，選用第一個: {best_candidate['folder_name']}")
                                target_folder = best_candidate
                        else:
                            self.log(f"     ℹ️ [Relaxed] 無同階段資料夾，將視為新案件 (符合審級區分原則)")

                    
                # (V3) 如果沒有 target_folder，查詢 DB 或建立
                if not target_folder:
                    # 查詢 DB
                    db_case = self._find_case_in_db(client_name, case_reason, case_stage, laf_case_number=case_number, case_type=case_type)
                    
                    if db_case:
                        # DB 有記錄，使用其 folder_path
                        # self.log(f"     ✅ DB 找到案件: {db_case.get('case_number')}")
                        original_folder_path = db_case.get('folder_path', '')
                        
                        # ★ V3-FIX: 正規化路徑（統一使用 /，避免混用 / 和 \）
                        def normalize_path(p):
                            if not p: return p
                            return p.replace('\\', '/')
                        
                        original_folder_path_normalized = normalize_path(original_folder_path)
                        
                        # (V3-FIX) 多重路徑轉換與檢查
                        paths_to_check = []
                        
                        # 1. 原始路徑
                        if original_folder_path_normalized:
                            paths_to_check.append(('原始', original_folder_path_normalized))
                        
                        # 2. Translate
                        if self.db_manager and hasattr(self.db_manager, 'translate_path_to_local'):
                            translated = self.db_manager.translate_path_to_local(original_folder_path_normalized)
                            translated = normalize_path(translated)
                            if translated and translated != original_folder_path_normalized:
                                paths_to_check.append(('Translator', translated))
                        
                        # 3. Shared mapper candidates
                        if original_folder_path_normalized:
                            for candidate in local_case_path_candidates(original_folder_path_normalized):
                                candidate = normalize_path(candidate)
                                if candidate and candidate not in [p[1] for p in paths_to_check]:
                                    paths_to_check.append(('PathMapper', candidate))

                        # 檢查路徑
                        found_path = None
                        for source, path in paths_to_check:
                            if os.path.exists(path):
                                found_path = path
                                break
                        
                        if found_path:
                            target_folder = {
                                'folder_name': os.path.basename(found_path),
                                'folder_path': found_path
                            }
                        else:
                             # ★ [Smart Discovery] 智慧資料夾搜尋 (即使 DB 路徑失效也能找到) ★
                            self.log(f"     ⚠️ DB 路徑失效，嘗試 Smart Discovery...")
                            osc_case_number = db_case.get('case_number')
                            discovered_path = None
                            
                            # 搜尋潛在根目錄
                            potential_roots = [target_root]
                            # 加入子目錄
                            for sub in ['刑事', '民事', '行政', '消費者債務清理']:
                                sub_path = os.path.join(target_root, sub)
                                if os.path.exists(sub_path):
                                    potential_roots.append(sub_path)
                            
                            for root in potential_roots:
                                if not os.path.exists(root): continue
                                try:
                                    for item in os.listdir(root):
                                         if item.startswith(osc_case_number): # 只要開頭對了 (案號對了)
                                             full_path = os.path.join(root, item)
                                             if os.path.isdir(full_path):
                                                 discovered_path = full_path
                                                 break
                                except:
                                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8269, exc_info=True)
                                if discovered_path: break
                                
                            if discovered_path:
                                self.log(f"     🔍 [SmartDiscovery] 找到更名後的資料夾: {discovered_path}")
                                target_folder = {
                                    'folder_name': os.path.basename(discovered_path),
                                    'folder_path': discovered_path
                                }
                            else:
                                # 真的找不到才重建
                                self.log(f"     ⚠️ 路徑皆不存在且找不到替代，嘗試重建...")
                                recreated_path = self._recreate_folder_for_existing_case(
                                    db_case=db_case, case_type=case_type, case_stage=case_stage,
                                    case_reason=case_reason, target_root=target_root
                                )
                                if recreated_path:
                                    target_folder = {'folder_name': os.path.basename(recreated_path), 'folder_path': recreated_path}
                                    if client_name not in client_folder_map: client_folder_map[client_name] = []
                                    client_folder_map[client_name].append(target_folder)

                    
                    # 建立新案件
                    if not target_folder and not db_case:
                        self.log(f"     🆕 自動建立新案件...")
                        new_folder_path = self._create_laf_case_auto(
                            client_name=client_name, case_type=case_type, case_stage=case_stage,
                            case_reason=case_reason, laf_case_number=case_number, target_root=target_root
                        )
                        if new_folder_path:
                            target_folder = {'folder_name': os.path.basename(new_folder_path), 'folder_path': new_folder_path}
                            if client_name not in client_folder_map: client_folder_map[client_name] = []
                            client_folder_map[client_name].append(target_folder)
                        else:
                            self.log(f"     ❌ 建立案件失敗")

                # 將結果存回 case_data 供第二階段使用
                if target_folder:
                    case_data['target_folder'] = target_folder
                    self.log(f"     📌 鎖定: {target_folder['folder_name']}")
                else:
                    self.log(f"     ❌ 無法鎖定資料夾，將跳過下載")

            self.log(f"\n✅ [第一階段] 資料檢查與建立完成。")

            # =========================================================================
            # 第二階段：檔案檢查與下載 (Batch Phase 2: Download Missing Files)
            # =========================================================================
            self.log(f"\n🚀 [第二階段] 開始檢查缺漏檔案並下載...")

            for i, case_data in enumerate(cases):
                target_folder = case_data.get('target_folder')
                if not target_folder:
                    continue
                
                client_name = case_data['client_name']
                file_list = case_data.get('file_list', [])
                case_number = case_data['case_number']
                row_element = case_data.get('row_element')
                folder_path = target_folder['folder_path']
                
                # 5. 定義檔案分類規則
                file_category_rules = {
                    '01_法扶資料': ['接案通知書', '委任狀', '法律扶助申請書', '案件概述單', '資力詢問表', '審查表', '准予扶助證明書', '預付酬金領款單', '結案回報書'],
                    '03_結案資料': ['結案酬金領款單'],
                    '02_開辦資料': ['附條件第二階段預付酬金領款單'],
                }
                
                # 檢查缺漏
                missing_files = []
                for subfolder_name, keywords in file_category_rules.items():
                    subfolder_path = os.path.join(folder_path, subfolder_name)
                    # 寬鬆比對預期檔名
                    expected_files = [f for f in file_list if any(kw in f for kw in keywords)]
                    
                    if not expected_files: continue
                    
                    if os.path.exists(subfolder_path):
                        existing_files = os.listdir(subfolder_path)
                        for expected in expected_files:
                            expected_base = re.sub(r'_\d{7}-[A-Z]-\d{3}_\d+\.pdf$', '', expected)
                            found = False
                            for existing in existing_files:
                                if expected_base in existing or any(kw in existing for kw in keywords if kw in expected):
                                    found = True
                                    break
                            if not found:
                                missing_files.append((expected, subfolder_name))
                    else:
                        for expected in expected_files:
                            missing_files.append((expected, subfolder_name))
                
                if not missing_files:
                    # self.log(f"  [{i+1}] {client_name}: ✅ 檔案完整")
                    continue
                
                self.log(f"  📥 [{i+1}/{len(cases)}] {client_name} 缺失 {len(missing_files)} 個檔案，下載中...")
                # for mf, sf in missing_files[:3]:
                #     self.log(f"     - {mf}")
                
                # 6. 下載檔案
                downloaded = self.web_automation.download_case_files(case_number, row_element)
                
                if not downloaded:
                    self.log(f"     ⚠️ 下載失敗")
                    continue
                
                # 7. 歸檔
                self.log(f"     📂 歸檔中...")
                def get_target_subfolder(fname):
                    for subfolder, keywords in file_category_rules.items():
                        if any(kw in fname for kw in keywords): return subfolder
                    return '01_法扶資料'

                for file_path in downloaded:
                    if not os.path.exists(file_path): continue
                    file_name = os.path.basename(file_path)
                    
                    # ZIP 處理
                    if file_name.lower().endswith('.zip'):
                        try:
                            import zipfile
                            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                                for member in zip_ref.infolist():
                                    orig_filename = member.filename
                                    try: new_filename = orig_filename.encode('cp437').decode('big5')
                                    except Exception:
                                        try: new_filename = orig_filename.encode('cp437').decode('gbk')
                                        except: new_filename = orig_filename
                                    
                                    if member.is_dir() or new_filename.endswith('/'): continue
                                    base_name = os.path.basename(new_filename)
                                    if not base_name or base_name.startswith('.'): continue
                                        
                                    target_sub = get_target_subfolder(base_name)
                                    target_sub_path = os.path.join(folder_path, target_sub)
                                    os.makedirs(target_sub_path, exist_ok=True)
                                    
                                    target_path = os.path.join(target_sub_path, base_name)
                                    with open(target_path, "wb") as target_file, zip_ref.open(member) as source_file:
                                        shutil.copyfileobj(source_file, target_file)
                                    # self.log(f"          ✓ {base_name}")
                            
                            # Backup ZIP
                            backup_sub = os.path.join(folder_path, '01_法扶資料')
                            os.makedirs(backup_sub, exist_ok=True)
                            _safe_move(file_path, os.path.join(backup_sub, file_name), log=self.log)
                            
                        except Exception as e:
                            self.log(f"        ❌ 解壓/備份失敗: {e}")
                    else:
                        # 一般檔案
                        target_sub = get_target_subfolder(file_name)
                        target_sub_path = os.path.join(folder_path, target_sub)
                        os.makedirs(target_sub_path, exist_ok=True)
                        dest_path = os.path.join(target_sub_path, file_name)
                        _safe_move(file_path, dest_path, log=self.log)
                        # self.log(f"        ✓ {file_name}")

            self.log("\n✅ [第二階段] 所有檔案檢查與下載完成")

        except Exception as e:
            self.log(f"❌ [下載驗證] 執行失敗: {e}")
            traceback.print_exc()
        finally:
            if self.web_automation:
                self.web_automation.close()
            
            # 清理 debug 截圖檔案
            self._cleanup_debug_screenshots()

    def _cleanup_debug_screenshots(self):
        """清理下載驗證過程中產生的 debug 截圖"""
        if not self.web_automation:
            return
        if NO_DELETE:
            return
            
        try:
            # 新路徑：統一清理 .runtime/debug_screenshots/
            try:
                from api.debug_capture import cleanup_old as _debug_cleanup_old
                _runtime_deleted = _debug_cleanup_old(48)
                if _runtime_deleted > 0:
                    self.log(f"  ✅ 已清理 .runtime/debug_screenshots/ 中 {_runtime_deleted} 個舊 debug 檔")
            except Exception:
                pass

            download_folder = self.web_automation.download_folder
            if not download_folder or not os.path.exists(download_folder):
                return

            # 舊路徑相容清理（captcha 截圖仍寫到 tempdir）
            debug_patterns = [
                "debug_captcha*.png",
                "debug_login_page*.png",
            ]

            import glob
            for pattern in debug_patterns:
                for file_path in glob.glob(str(download_folder / pattern)):
                    _safe_remove(file_path, log=self.log)

        except Exception as e:
            self.log(f"  ⚠️ 清理 debug 截圖時發生錯誤: {e}")

    def _find_case_in_db(self, client_name: str, case_reason: str = "", case_stage: str = "", laf_case_number: str = "", case_type: str = "") -> Optional[Dict]:
        """
        在資料庫中查詢當事人的法扶案件
        
        Args:
            client_name: 當事人姓名
            case_reason: 案由
            case_stage: 案件階段
            laf_case_number: 法扶案號
            case_type: 案件類型 (新增)
            
        Returns:
            案件資料字典，或 None
        """
        if not self.db_manager:
            return None
        normalized_client_name = re.sub(r"[\s\u3000·・•‧∙．｡。]+", "", str(client_name or "").strip()).lower()
        incoming_laf_no = str(laf_case_number or "").strip()

        def _row_matches_laf_number(row: Dict) -> bool:
            if not incoming_laf_no:
                return False
            for key in ("legal_aid_number", "laf_case_no", "application_no"):
                if str((row or {}).get(key) or "").strip() == incoming_laf_no:
                    return True
            return incoming_laf_no in str((row or {}).get("notes") or "")
        
        try:
            # 查詢法扶案件（兼容舊值「法扶案件」）
            # (V2.1) 增加查詢 legal_aid_number
            query = """
                SELECT case_number, client_name, folder_path, case_type, case_stage, case_reason,
                       legal_aid_number, laf_case_no, application_no, notes
                FROM cases
                WHERE LOWER(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(client_name), ' ', ''), '　', ''), '·', ''), '・', ''), '‧', ''), '．', '')) = %s
                  AND case_category IN ('法律扶助案件', '法扶案件')
                ORDER BY created_date DESC
            """
            results = self.db_manager.fetch_all(query, (normalized_client_name,), as_dict=True)
            
            if not results:
                return None
            
            # (V2.1) 策略 0: 如果有提供 laf_case_number，優先三欄精準命中（含舊 notes）
            if incoming_laf_no:
                for r in results:
                    if _row_matches_laf_number(r):
                        self.log(f"     ✅ [DB] 法扶案號完全命中: {incoming_laf_no} -> OSC: {r['case_number']}")
                        return r

            # (V2.3) 調整策略：根據使用者回饋
            
            # 優先策略 0: 消費者債務清理 -> 僅在沒有法扶案號時，允許同名同類型合併
            if case_type == '消費者債務清理':
                if incoming_laf_no:
                    return None
                for r in results:
                     # 只要 DB 裡也是消費者債務清理 (通常 case_type='消費者債務清理' 或 案由含關鍵字)
                     if r.get('case_type') == '消費者債務清理' or '消費者債務清理' in (r.get('case_reason') or ''):
                         self.log(f"     ✅ [DB] 消費者債務清理合併: {r['case_number']}")
                         return r

            # ★★★ 新增策略 (舊案件相容性): 同名 + 同案件類型 + 同案由 = 視為相同案件 ★★★
            # 使用者回饋：「有同名且案件種類、案由相同還會是不同人的狀況幾乎不可能」
            if case_type and case_reason:
                for r in results:
                    if incoming_laf_no and not _row_matches_laf_number(r):
                        continue
                    db_type = r.get('case_type', '') or ''
                    db_reason = r.get('case_reason', '') or ''
                    
                    # 案件類型必須相同
                    if db_type != case_type:
                        continue
                    
                    # 案由必須相符 (包含關係，處理 "傷害" vs "傷害罪" 等情況)
                    if db_reason and (case_reason in db_reason or db_reason in case_reason):
                        self.log(f"     ✅ [DB] 同名+同類型+同案由 (舊案件相容): {case_type}/{case_reason} <-> {db_type}/{db_reason}")
                        return r

            # 策略 A: 嚴格比對 (階段 + 案由) -> 用於「確認資料夾是否已存在」
            # 如果 DB 中已經有一筆資料完全符合 (階段 + 案由)，則必須回傳該筆做為合併對象
            # 使用者需求：「除了系統編號以外，要建立的資料夾名稱是否存在，如果存在就不要建立」
            # 即: 如果 target = Client + Stage + Reason 與 DB existing = Client + Stage + Reason 相同 -> 命中
            
            if incoming_laf_no:
                self.log("     ℹ️ [DB] 同人但法扶案號未命中，視為新案件。")
                return None

            if case_reason and case_stage:
                for r in results:
                    db_reason = r.get('case_reason', '') or ''
                    db_stage = r.get('case_stage', '') or ''
                    
                    # 1. 階段必須相同
                    if db_stage != case_stage:
                        continue
                        
                    # 2. 案由必須相符 (包含關係)
                    # 例如: '竊盜' vs '竊盜' (OK), '竊盜' vs '加重竊盜' (OK if substring match desired?)
                    # 使用者說: "請案由有比對到就可以了" (上一輪回饋)，但這一輪說 "案件階段不同會需要不同的資料夾"
                    # 綜合起來: 階段必須同，案由要比對到。
                    
                    if db_reason and (case_reason in db_reason or db_reason in case_reason):
                         self.log(f"     ✅ [DB] 階段與案由皆相符 (合併案件): {case_stage}/{case_reason} <-> {db_stage}/{db_reason}")
                         return r

            # 策略 B: 如果階段不同 -> 視為新案件 (不回傳)
            # 策略 C: 如果案由不符 -> 視為新案件 (不回傳)
            
            self.log(f"     ℹ️ [DB] 無完全符合案件 (類型/階段不同或案由不同)，將建立新案件。")
            return None
            
            # 多筆記錄，嘗試匹配
            
            # 1. 精確匹配案由
            if case_reason:
                for r in results:
                    if r.get('case_reason') == case_reason:
                        return r
            
            # 2. 關鍵字匹配 (針對再審/非常上訴/更生/清算等特殊情況)
            if case_reason:
                special_keywords = ['再審', '非常上訴', '更生', '清算', '刑事再審', '刑事非常上訴', '刑事自訴', '刑事執行']
                for kw in special_keywords:
                    if kw in case_reason:
                        # 優先找同樣有此關鍵字的案件
                        for r in results:
                            if r.get('case_reason') and kw in r.get('case_reason'):
                                return r
            
            # 3. 模糊匹配 (包含關係)
            if case_reason:
                for r in results:
                    db_reason = r.get('case_reason', '')
                    if db_reason and (case_reason in db_reason or db_reason in case_reason):
                        return r
            
            # 4. 匹配階段
            if case_stage:
                for r in results:
                    if r.get('case_stage') == case_stage:
                        return r
            
            # 4. 如果都沒特別匹配，但有多筆... 回傳最新的一筆 (這是最後手段)
            # 但既然經過了 candidates 過濾，應該比較安全了
            return results[0]
        except Exception as e:
            self.log(f"     ⚠️ 查詢 DB 失敗: {e}")
            return None

    def _create_laf_case_auto(self, client_name: str, case_type: str, case_stage: str, 
                              case_reason: str, laf_case_number: str, target_root: str) -> Optional[str]:
        """
        自動建立法扶案件（DB 記錄 + 資料夾結構）
        
        模擬 CaseDialog 的邏輯，但不需要 GUI
        
        Args:
            client_name: 當事人姓名
            case_type: 案件類型（刑事/民事/消費者債務清理等）
            case_stage: 案件階段（偵查/一審等）
            case_reason: 案由
            laf_case_number: 法扶案號（記錄在 notes）
            target_root: 目標資料夾根路徑
            
        Returns:
            新建立的資料夾路徑，或 None
        """
        if not self.db_manager:
            self.log("     ❌ db_manager 未初始化")
            return None
        
        try:
            # 1. 生成 OSC 案號
            osc_case_number = self.db_manager.generate_case_number()
            if 'ERROR' in osc_case_number:
                self.log(f"     ❌ 無法生成案號: {osc_case_number}")
                return None
            
            self.log(f"     📋 生成 OSC 案號: {osc_case_number}")
            
            # 2. 建立資料夾結構
            # 對於消費者債務清理，案由一律為「更生」，且資料夾名稱不需要案件階段
            if case_type == '消費者債務清理':
                case_reason = '更生'  # 強制設為「更生」
                folder_name_parts = [osc_case_number, client_name, case_type, case_reason]
            else:
                folder_name_parts = [osc_case_number, client_name, case_stage or case_type, case_reason]
            
            folder_name = '-'.join(filter(None, folder_name_parts))
            
            # 清理非法字元
            illegal_chars = '<>:"|?*\\/'
            for char in illegal_chars:
                folder_name = folder_name.replace(char, '_')
            
            # (V2.1) 根據案件類型分類到子資料夾
            type_subfolder = ""
            if case_type == '刑事':
                type_subfolder = "刑事"
            elif case_type == '民事':
                type_subfolder = "民事"
            elif case_type == '行政':
                type_subfolder = "行政"
            elif case_type == '消費者債務清理':
                type_subfolder = "消費者債務清理"
            
            # 如果有分類，加到路徑中
            final_root = target_root
            if type_subfolder:
                final_root = os.path.join(target_root, type_subfolder)
                
            # 完整路徑：target_root/分類/資料夾名稱
            full_path = os.path.join(final_root, folder_name)
            
            # 建立主資料夾
            os.makedirs(full_path, exist_ok=True)
            
            # 建立子資料夾（法扶案件結構）
            subfolders = [
                '01_法扶資料', '02_開辦資料', '03_結案資料', 
                '04_我方歷次書狀', '05_對方歷次書狀', '06_閱卷資料', 
                '07_證據資料', '08_筆錄', '09_法院通知或程序裁定', 
                '10_判決書', '11_回執', '12_信件往返'
            ]
            
            for subfolder in subfolders:
                subfolder_path = os.path.join(full_path, subfolder)
                os.makedirs(subfolder_path, exist_ok=True)
                # 建立 .gitkeep 檔案
                gitkeep_path = os.path.join(subfolder_path, '.gitkeep')
                if not os.path.exists(gitkeep_path):
                    with open(gitkeep_path, 'w') as f:
                        pass
            
            self.log(f"     📁 資料夾已建立: {folder_name}")
            
            
            # (V2.1.1 修正) 強制將 DB 中的記錄路徑轉換為系統標準 Z:\\ 網域路徑
            db_folder_path = self.db_manager.translate_path_to_canonical(full_path) if hasattr(self.db_manager, 'translate_path_to_canonical') else full_path
            
            # 如果還是原本的 K:/ 或 Mac 路徑，硬核轉換回 Z:\\ (避免不同裝置無法讀取)
            if db_folder_path.startswith('K:/') or db_folder_path.startswith('K:\\'):
                db_folder_path = db_folder_path.replace('K:/', 'Z:\\').replace('K:\\', 'Z:\\').replace('/', '\\')
            elif sys.platform == 'darwin' and 'SynologyDrive' in db_folder_path:
                # 把 /Users/xxx/SynologyDrive/... 轉回 Z:\lumi63181107\...
                parts = db_folder_path.split('SynologyDrive', 1)
                db_folder_path = 'Z:\\lumi63181107' + parts[1]
                db_folder_path = db_folder_path.replace('/', '\\')

            self.log(f"     [DEBUG DB_FOLDER] {db_folder_path}")

            case_data = {
                'case_number': osc_case_number,
                'client_name': client_name,
                'client_name_en': '',
                'case_type': case_type,
                'case_category': '法律扶助案件',
                'case_subject': '',
                'case_reason': case_reason,
                'status': '進行中',
                'start_date': datetime.now().strftime('%Y-%m-%d'),
                'court_date': None,
                'lawyer': '',
                'folder_path': db_folder_path,
                'case_stage': case_stage,
                'court_case_number': '',
                'court_division': '',
                'court_name': '',
                'legal_aid_status': '未開辦',
                'legal_aid_number': laf_case_number,  # 新增：用於重複檢測
                'notes': f'法扶案號: {laf_case_number}'
            }
            
            # 使用 insert_case_from_csv 或類似方法插入
            success = self._insert_case_to_db(case_data)
            
            if success:
                self.log(f"     ✅ DB 記錄已建立: {osc_case_number}")
                return full_path
            else:
                self.log(f"     ❌ DB 記錄建立失敗")
                return full_path  # 即使 DB 失敗，資料夾已建立
            
        except Exception as e:
            self.log(f"     ❌ 自動建立案件失敗: {e}")
            traceback.print_exc()
            return None

    def _recreate_folder_for_existing_case(self, db_case: Dict, case_type: str, 
                                            case_stage: str, case_reason: str,
                                            target_root: str) -> Optional[str]:
        """
        為已存在於 DB 的案件重建資料夾（跨電腦/路徑變更時使用）
        
        與 _create_laf_case_auto 不同：
        - 不生成新案號，使用原有案號
        - 更新 DB 中的 folder_path
        
        Args:
            db_case: 資料庫中的案件記錄
            case_type: 案件類型
            case_stage: 案件階段
            case_reason: 案由
            target_root: 目標資料夾根路徑
            
        Returns:
            新建立的資料夾路徑，或 None
        """
        if not self.db_manager:
            self.log("     ❌ db_manager 未初始化")
            return None
        
        try:
            # 使用原有案號
            osc_case_number = db_case.get('case_number')
            client_name = db_case.get('client_name')
            
            if not osc_case_number or not client_name:
                self.log("     ❌ DB 記錄缺少必要欄位")
                return None
            
            self.log(f"     📋 使用原有 OSC 案號: {osc_case_number}")
            
            # 建立資料夾結構 (與 _create_laf_case_auto 相同邏輯)
            if case_type == '消費者債務清理':
                case_reason = '更生'
                folder_name_parts = [osc_case_number, client_name, case_type, case_reason]
            else:
                folder_name_parts = [osc_case_number, client_name, case_stage or case_type, case_reason]
            
            folder_name = '-'.join(filter(None, folder_name_parts))
            
            # 清理非法字元
            illegal_chars = '<>:"|?*\\/'
            for char in illegal_chars:
                folder_name = folder_name.replace(char, '_')
            
            # 根據案件類型分類到子資料夾
            type_subfolder = ""
            if case_type == '刑事':
                type_subfolder = "刑事"
            elif case_type == '民事':
                type_subfolder = "民事"
            elif case_type == '行政':
                type_subfolder = "行政"
            elif case_type == '消費者債務清理':
                type_subfolder = "消費者債務清理"
            
            final_root = target_root
            if type_subfolder:
                final_root = os.path.join(target_root, type_subfolder)
            
            full_path = os.path.join(final_root, folder_name)
            
            # 建立主資料夾和子資料夾
            os.makedirs(full_path, exist_ok=True)
            
            subfolders = [
                '01_法扶資料', '02_開辦資料', '03_結案資料', 
                '04_我方歷次書狀', '05_對方歷次書狀', '06_閱卷資料', 
                '07_證據資料', '08_筆錄', '09_法院通知或程序裁定', 
                '10_判決書', '11_回執', '12_信件往返'
            ]
            
            for subfolder in subfolders:
                subfolder_path = os.path.join(full_path, subfolder)
                os.makedirs(subfolder_path, exist_ok=True)
                gitkeep_path = os.path.join(subfolder_path, '.gitkeep')
                if not os.path.exists(gitkeep_path):
                    with open(gitkeep_path, 'w') as f:
                        pass
            
            self.log(f"     📁 資料夾已重建: {folder_name}")
            
            # ★ 注意：不更新 DB 的 folder_path
            # DB 中保持標準路徑 (Z:)，各電腦透過 translate_path_to_local 轉換
            self.log(f"     ✅ 使用原案號完成: {osc_case_number}")
            
            return full_path
            
        except Exception as e:
            self.log(f"     ❌ 重建資料夾失敗: {e}")
            traceback.print_exc()
            return None

    def _insert_case_to_db(self, case_data: Dict) -> bool:
        """
        插入案件記錄到資料庫
        
        Args:
            case_data: 案件資料字典
            
        Returns:
            是否成功
        """
        if not self.db_manager:
            return False
        
        try:
            # 嘗試使用 insert_case_from_csv 方法
            if hasattr(self.db_manager, 'insert_case_from_csv'):
                return self.db_manager.insert_case_from_csv(case_data)
            
            # 備用：直接 SQL 插入
            import uuid
            
            # 1. 確保當事人存在
            client_name = case_data.get('client_name')
            if client_name:
                check_client_query = "SELECT id FROM clients WHERE name = %s"
                existing_client = self.db_manager.execute(check_client_query, (client_name,), fetch='one')
                
                if not existing_client:
                    new_client_id = str(uuid.uuid4())
                    create_client_query = """
                        INSERT INTO clients (id, name, created_date, status)
                        VALUES (%s, %s, NOW(), '進行中')
                    """
                    self.db_manager.execute_write(create_client_query, (new_client_id, client_name))
                    self.log(f"     👤 自動建立當事人資料: {client_name}")

            case_id = str(uuid.uuid4())
            
            query = """
                INSERT INTO cases (
                    id, case_number, client_name, client_name_en, case_type,
                    case_category, case_subject, case_reason, status,
                    start_date, court_date, lawyer, folder_path, case_stage,
                    court_case_number, court_division, court_name, legal_aid_status, 
                    legal_aid_number, notes
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
            """
            params = (
                case_id,
                case_data.get('case_number'),
                case_data.get('client_name'),
                case_data.get('client_name_en', ''),
                case_data.get('case_type'),
                case_data.get('case_category'),
                case_data.get('case_subject', ''),
                case_data.get('case_reason'),
                case_data.get('status'),
                case_data.get('start_date'),
                case_data.get('court_date'),
                case_data.get('lawyer', ''),
                case_data.get('folder_path'),
                case_data.get('case_stage'),
                case_data.get('court_case_number', ''),
                case_data.get('court_division', ''),
                case_data.get('court_name', ''),
                case_data.get('legal_aid_status', ''),
                case_data.get('legal_aid_number', ''),  # 新增：法扶案號
                case_data.get('notes', '')
            )
            
            return self.db_manager.execute_write(query, params)
            
        except Exception as e:
            self.log(f"     ⚠️ 插入 DB 失敗: {e}")
            return False

    def _extract_and_update_client_info(self, client_name: str, folder_path: str):
        """
        從「法律扶助申請書」PDF 提取當事人資訊並更新到資料庫。
        使用 _scan_laf_forms_for_client_fields() 統一解析邏輯。
        """
        if not self.db_manager:
            return

        try:
            fields = _scan_laf_forms_for_client_fields(folder_path, max_pdfs=5)
            if not fields:
                return

            phone = fields.get("phone", "")
            address = fields.get("address", "")
            tax_id = fields.get("tax_id", "")
            email = fields.get("email", "")

            updates = []
            params = []
            if phone:
                updates.append("phone = %s")
                params.append(phone)
            if address:
                updates.append("address = %s")
                params.append(address)
            if tax_id:
                updates.append("notes = CONCAT(COALESCE(notes, ''), %s)")
                params.append(f"|身分證：{tax_id}")
            if email:
                updates.append("email = %s")
                params.append(email)

            if updates:
                params.append(client_name)
                query = f"UPDATE clients SET {', '.join(updates)} WHERE name = %s"
                self.db_manager.execute_write(query, tuple(params))
                self.log(f"     👤 已更新當事人資訊: 電話={phone}, 地址={address}")

        except Exception as e:
            self.log(f"     ⚠️ 提取當事人資訊失敗: {e}")


# ==============================================================================
# 測試
# ==============================================================================

def smoke_login(headless: bool = True, base_url: str = "", mock_mode: bool = False, timeout_sec: int = 90) -> dict:
    """
    正式站登入冒煙測試：
    - 只做登入驗證（不送出任何回報、不做任何案件操作）
    - 回傳登入是否成功 + 偵錯截圖路徑
    """
    import json as _json
    import time as _time
    from pathlib import Path as _Path

    # Load config (prefer Desktop/code/json/config.json)
    cfg_paths = ["config.json"]
    cfg_paths.extend(str(p) for p in config_candidates("config.json"))
    cfg_paths.extend(str(p) for p in config_candidates("legalbridge_config.json"))
    cfg = {}
    for p in cfg_paths:
        try:
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    cfg = _json.load(f) or {}
                break
        except Exception:
            continue

    laf_cfg = (cfg.get("laf") or {}) if isinstance(cfg, dict) else {}
    username = (os.environ.get("MAGI_LAF_USERNAME") or str(laf_cfg.get("username") or "")).strip()
    password = (os.environ.get("MAGI_LAF_PASSWORD") or str(laf_cfg.get("password") or "")).strip()
    download_folder = str(laf_cfg.get("download_folder") or (get_magi_root_dir() / "_laf_smoke")).strip()

    if not username or not password:
        return {"success": False, "error": "missing laf.username/password in config", "config_used": cfg_paths}

    # Always isolate smoke artifacts.
    stamp = time.strftime("%Y%m%d_%H%M%S")
    out_dir = _Path(download_folder) / f"smoke_login_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    wa = None
    t0 = _time.time()
    try:
        wa = LAFWebAutomation(
            username=username,
            password=password,
            download_folder=str(out_dir),
            headless=bool(headless),
            base_url=base_url or "",
            mock_mode=bool(mock_mode),
            log_callback=lambda s: None,  # keep smoke quiet (avoid leaking anything)
        )
        ok = bool(wa.login())

        snap = out_dir / "after_login.png"
        try:
            if wa.driver:
                wa.driver.save_screenshot(str(snap))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9033, exc_info=True)

        return {
            "success": True,
            "ok": ok,
            "ms": int((_time.time() - t0) * 1000),
            "base_url": (base_url or wa.DEFAULT_BASE_URL),
            "headless": bool(headless),
            "mock_mode": bool(mock_mode),
            "debug_dir": str(out_dir),
            "screenshot": str(snap) if snap.exists() else "",
        }
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:500]}", "ms": int((_time.time() - t0) * 1000), "debug_dir": str(out_dir)}
    finally:
        try:
            if wa:
                wa.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9052, exc_info=True)


if __name__ == '__main__':
    # 測試主旨解析
    test_subjects = [
        "【法扶花蓮分會派案通知】高弘軒-1141121-E-006-消費者債務清理事件-消費者債務清理程序",
        "【法扶台東分會派案通知】曾慧甄-1141106-J-011-民事通常程序第一審-袋地通行權案件之訴訟代理",
        "【法扶基隆分會審核結果通知】陳紫箖-1140502-K-001-刑事偵查中辯護-過失傷害等",
        "《附加檔案》【法扶花蓮分會派案通知】林鈴洵(原名:林孟潔)-1141121-E-001-消費者債務清理事件-消費者債務清理程序",
    ]
    
    print("=" * 70)
    print("法扶信件主旨解析測試")
    print("=" * 70)
    
    for subject in test_subjects:
        print(f"\n原始主旨: {subject}")
        info = LAFCaseTypeParser.parse_subject(subject)
        
        if info:
            print(f"  ✓ 當事人: {info.client_name}")
            if info.client_alias:
                print(f"    原名: {info.client_alias}")
            print(f"  ✓ 分會: {info.branch}")
            print(f"  ✓ 法扶案號: {info.laf_case_number}")
            print(f"  ✓ 案件類型: {info.case_type} ({info.case_stage})")
            print(f"  ✓ 案由: {info.case_reason}")
            print(f"  ✓ 有附件: {info.has_attachment}")
        else:
            print("  ✗ 無法解析")

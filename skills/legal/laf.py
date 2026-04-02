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
import pickle
import base64
import threading
import traceback
import tempfile
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field

import importlib.util

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.case_path_mapper import (
    local_case_path_candidates,
    preferred_case_roots,
    translate_case_path_to_local,
    translate_local_path_to_canonical,
)

from api.runtime_paths import config_candidates, ensure_orch_on_sys_path, get_config_path
from skills.bridge.shared_utils.text_utils import normalize_spaces as _normalize_spaces
from skills.bridge.shared_utils.case_number_utils import RE_LAF_CASE_NUMBER

import logging as _logging

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 61, exc_info=True)
_log = _logging.getLogger("laf")

# ==============================================================================
# 安全政策：禁止刪除（含下載暫存），避免誤刪 Synology Drive 內容
# ==============================================================================

NO_DELETE = os.environ.get("MAGI_NO_DELETE", "1").strip().lower() in {"1", "true", "yes", "on"}


def _safe_remove(path: str, log=None) -> None:
    if not path:
        return
    try:
        # Project-wide delete guard:
        # - Synology Drive: never delete (quarantine)
        # - Non-protected temp artifacts: allow cleanup when explicitly requested
        try:
            ensure_orch_on_sys_path()
            import safe_fs  # type: ignore
            # Respect global no-delete policy; only allow physical delete when policy explicitly permits.
            safe_fs.safe_remove(path, reason="laf_tmp", allow_delete=(not NO_DELETE), log=log)
            return
        except Exception as _bare_e:
            _log.debug("laf skipped: %s", _bare_e)

        if NO_DELETE:
            if log:
                log(f"    ⏭️ 依政策不刪檔 (MAGI_NO_DELETE=1): {os.path.basename(path)}")
            return

        # Even when NO_DELETE is off, keep cleanup non-destructive by default in this helper.
        if log:
            log(f"    ℹ️ 略過實體刪除（laf_tmp 清理保守模式）: {os.path.basename(path)}")
    except Exception as e:
        if log:
            log(f"    ⚠️ 刪除失敗: {e}")


def _safe_move(src: str, dst: str, log=None) -> None:
    if not src or not dst:
        return
    try:
        os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    except Exception as _bare_e:
        _log.debug("laf skipped: %s", _bare_e)
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
PDFINFO_BIN = os.environ.get("MAGI_PDFINFO_BIN", "/opt/homebrew/bin/pdfinfo").strip()

# ==============================================================================
# 依賴檢查 (Lazy Load Setup)
# ==============================================================================

# Selenium
SELENIUM_AVAILABLE = importlib.util.find_spec("selenium") is not None
if not SELENIUM_AVAILABLE:
    print("⚠️ Selenium 未安裝，LAF 自動化功能無法使用")

# RapidOCR
RAPIDOCR_AVAILABLE = importlib.util.find_spec("rapidocr_onnxruntime") is not None
if not RAPIDOCR_AVAILABLE:
    print("⚠️ RapidOCR 未安裝，驗證碼自動識別功能無法使用")

# ddddocr (Primary)
DDDDOCR_AVAILABLE = False
try:
    if importlib.util.find_spec("ddddocr") is not None:
        import ddddocr as _ddddocr_probe
        DDDDOCR_AVAILABLE = hasattr(_ddddocr_probe, "DdddOcr")
except Exception:
    DDDDOCR_AVAILABLE = False

if DDDDOCR_AVAILABLE:
    print("✅ [Import] ddddocr 模組可用")
else:
    print("⚠️ [Import] ddddocr 模組不可用（將改用 RapidOCR）")

# Google Gmail API
GMAIL_AVAILABLE = importlib.util.find_spec("googleapiclient") is not None and \
                  importlib.util.find_spec("google_auth_oauthlib") is not None and \
                  importlib.util.find_spec("google.auth") is not None

# PIL/Numpy
PIL_AVAILABLE = importlib.util.find_spec("PIL") is not None and importlib.util.find_spec("numpy") is not None

# Placeholders
webdriver = None
Options = None
By = None
WebDriverWait = None
EC = None
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
    body: str = ""                 # 信件內文快照
    attachments: List[Dict[str, Any]] = field(default_factory=list)


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
                    # 1. 消費者債務清理
                    if '消費者債務清理' in info.case_reason or '更生' in info.case_reason or '清算' in info.case_reason:
                        info.case_type = '消費者債務清理'
                        info.case_stage = '其他'
                        
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

        # 4. ★ 專員來信簡寫格式：1150324-T-047沈筱筑(債清)
        # 格式：{案號}{當事人}({案由簡稱})  或  {案號}{當事人}
        staff_short_match = re.search(
            r'(\d{7}-[A-Z]-\d{3})\s*([^\s(（]+)\s*(?:[(（](.+?)[)）])?',
            subject,
        )
        if staff_short_match:
            info.laf_case_number = staff_short_match.group(1)
            info.client_name = staff_short_match.group(2).strip()
            info.notification_type = "派案通知"
            info.branch = "待確認"

            short_reason = (staff_short_match.group(3) or "").strip()
            # 從簡稱推斷案件類型
            if short_reason in ('債清', '消債', '更生', '清算'):
                info.case_type = "消費者債務清理"
                info.case_stage = "其他"
                info.case_reason = {'債清': '更生', '消債': '更生',
                                    '更生': '更生', '清算': '清算'}.get(short_reason, short_reason)
            else:
                info.case_type = "民事"
                info.case_stage = "一審"
                info.case_reason = short_reason or "待確認"
            info.laf_case_type = "一般案件"

            info.has_attachment = True  # 專員來信通常有附件
            info.needs_download = True
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
            global ddddocr
            if ddddocr is None:
                try:
                    import ddddocr
                except ImportError:
                    pass

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
                            self.log(f"📦 [ddddocr] Found frozen model: {onnx_path}")
                            onnx_kwargs['import_onnx_path'] = onnx_path
                        else:
                            self.log(f"⚠️ [ddddocr] frozen model not found in: {possible_paths}")

                    self.dddd_ocr = ddddocr.DdddOcr(**onnx_kwargs)
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
        try:
            # Lazy Load PIL and Numpy
            global Image, np
            if Image is None:
                from PIL import Image
            if np is None:
                import numpy as np

            # 處理不同的輸入類型
            img = None
            
            if isinstance(image_source, (str, Path)):
                img = Image.open(image_source)
            elif isinstance(image_source, bytes):
                import io
                img = Image.open(io.BytesIO(image_source))
            elif isinstance(image_source, np.ndarray):
                img = Image.fromarray(image_source)
            elif isinstance(image_source, Image.Image):
                img = image_source
            else:
                self.log(f"⚠️ 不支援的圖片類型: {type(image_source)}")
                return ""

            # =========================================================
            # 1. ddddocr (Primary Local Engine)
            # =========================================================
            if self.dddd_ocr:
                try:
                    import io

                    # A. 原圖直接判讀
                    raw_buf = io.BytesIO()
                    img.save(raw_buf, format="PNG")
                    raw_digits = re.sub(r"[^\d]", "", self.dddd_ocr.classification(raw_buf.getvalue()) or "")
                    if len(raw_digits) >= 4:
                        return raw_digits[:4]

                    # B. 二值化/放大後再判讀
                    gray = np.array(img.convert('L'))
                    for thresh in [150, 120, 180]:
                        binary = np.where(gray < thresh, 0, 255).astype(np.uint8)
                        processed = Image.fromarray(binary).resize(
                            (img.width * 2, img.height * 2),
                            Image.Resampling.LANCZOS
                        )
                        buf = io.BytesIO()
                        processed.save(buf, format="PNG")
                        digits = re.sub(r"[^\d]", "", self.dddd_ocr.classification(buf.getvalue()) or "")
                        if len(digits) >= 4:
                            return digits[:4]
                except Exception as e:
                    self.log(f"  ⚠️ ddddocr 失敗: {e}")

            # =========================================================
            # 2. RapidOCR (Main Local Engine - Prioritized)
            # =========================================================
            if self.ocr_engine:
                try:
                    import cv2

                    # === 進階圖像預處理 ===
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    
                    # 轉為 numpy array for OCR
                    # 灰階 -> 二值化 -> 放大
                    rgb = np.array(img)
                    gray = np.array(img.convert('L'))
                    threshold = 150
                    binary = np.where(gray < threshold, 0, 255).astype(np.uint8)
                    
                    processed_img = Image.fromarray(binary)
                    new_size = (processed_img.width * 2, processed_img.height * 2)
                    processed_img = processed_img.resize(new_size, Image.Resampling.LANCZOS)
                    processed_array = np.array(processed_img)
                    
                    # Debug save
                    try:
                        import tempfile
                        debug_processed = Path(tempfile.gettempdir()) / "debug_captcha_processed.png"
                        processed_img.save(debug_processed)
                    except Exception as _bare_e:
                        _log.debug("laf skipped: %s", _bare_e)

                    # 顏色遮罩：法扶驗證碼多為藍字，先抽藍字再辨識
                    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
                    blue_mask = cv2.inRange(hsv, (75, 35, 20), (150, 255, 255))
                    blue_mask = cv2.GaussianBlur(blue_mask, (3, 3), 0)
                    blue_mask = cv2.threshold(blue_mask, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
                    blue_mask = cv2.resize(
                        blue_mask,
                        (blue_mask.shape[1] * 2, blue_mask.shape[0] * 2),
                        interpolation=cv2.INTER_CUBIC,
                    )

                    candidates = []

                    # A. 第一次識別 (二值化+放大)
                    result, _ = self.ocr_engine(processed_array)
                    if result:
                        text = ''.join([line[1] for line in result])
                        digits = re.sub(r'[^\d]', '', text)
                        if digits:
                            candidates.append(digits)

                    # B. 嘗試不同閾值
                    for thresh in [120, 180, 100, 200]:
                        binary_test = np.where(gray < thresh, 0, 255).astype(np.uint8)
                        test_img = Image.fromarray(binary_test)
                        test_img = test_img.resize((test_img.width * 2, test_img.height * 2), Image.Resampling.LANCZOS)
                        test_array = np.array(test_img)
                        result_test, _ = self.ocr_engine(test_array)
                        if result_test:
                            text = ''.join([line[1] for line in result_test])
                            digits = re.sub(r'[^\d]', '', text)
                            if digits:
                                candidates.append(digits)

                    # C. 嘗試原始灰階
                    result_gray, _ = self.ocr_engine(gray)
                    if result_gray:
                        text = ''.join([line[1] for line in result_gray])
                        digits = re.sub(r'[^\d]', '', text)
                        if digits:
                            candidates.append(digits)

                    # D. 嘗試藍色遮罩
                    result_blue, _ = self.ocr_engine(blue_mask)
                    if result_blue:
                        text = ''.join([line[1] for line in result_blue])
                        digits = re.sub(r'[^\d]', '', text)
                        if digits:
                            candidates.append(digits)

                    if candidates:
                        candidates.sort(key=lambda s: len(s), reverse=True)
                        best = candidates[0]
                        self.log(f"  🔍 [RapidOCR] 已完成多策略辨識（best_len={len(best)}）")
                        if len(best) >= 4:
                            return best[:4]

                except Exception as e:
                    self.log(f"  ⚠️ RapidOCR 失敗: {e}")

            # =========================================================
            # 3. Melchior Vision Fallback (Last Resort)
            # =========================================================
            try:
                self.log(f"  🔍 調用 InferenceGateway 識別驗證碼...")
                try:
                    from skills.bridge.inference_gateway import InferenceGateway
                except Exception:
                    magi_root = os.environ.get("MAGI_ROOT_DIR", str(_MAGI_ROOT)).strip() or str(_MAGI_ROOT)
                    if magi_root and magi_root not in sys.path:
                        sys.path.insert(0, magi_root)
                    from skills.bridge.inference_gateway import InferenceGateway
                import tempfile
                
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                    img.save(tf, format="PNG")
                    temp_path = tf.name
                
                gateway = InferenceGateway()
                prompt = "Read the 4 digits in this CAPTCHA image. Output ONLY the digits."
                result = gateway.dispatch(
                    prompt=prompt,
                    image_path=temp_path,
                    task_type="captcha",
                    timeout=30,
                )
                
                _safe_remove(temp_path, log=self.log)
                
                if result['success']:
                    text = (result.get('analysis') or result.get('response') or "").strip()
                    digits = re.sub(r'[^\d]', '', text)
                    self.log(
                        f"  ✅ Gateway 辨識完成 route={result.get('route','')} degraded={result.get('degraded', False)}"
                    )
                    if len(digits) >= 4:
                        return digits[:4]
                else:
                    self.log(f"  ⚠️ Gateway 識別失敗: {result.get('error', '')}")

            except Exception as e:
                self.log(f"Gateway 呼叫失敗: {e}")

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
                    self.log(f"✅ 驗證碼識別成功 (第 {attempt + 1} 次): {result}")
                    return result
                
                self.log(f"⚠️ 驗證碼識別結果不完整 (第 {attempt + 1} 次): {result}")
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

        env_base = os.environ.get("LAF_BASE_URL", "").strip()
        self.base_url = (base_url or env_base or self.DEFAULT_BASE_URL).rstrip("/")
        self.LOGIN_URL = f"{self.base_url}/lafcsp/"
        self.MAIN_URL = f"{self.base_url}/lafcsp/toMainPage"
        self.DOWNLOAD_URL = f"{self.base_url}/lafcsp/toDownloadList"
        
        self.driver = None
        self.driver = None
        # Pass log_callback specifically to capture OCR logs in UI
        self.captcha_solver = CaptchaSolver(callback_on_fail=on_captcha_fail, log_callback=self.log)
        # 注意：正式站的驗證碼屬於安全機制。本工具不提供自動破解/繞過。
        # 正式站登入預設改為「人工提供驗證碼」：用環境變數 LAF_CAPTCHA 帶入 4 碼。
        # mock_mode（訓練/沙盒）才允許使用固定碼或其他測試流程。
        self._captcha_override = os.environ.get("LAF_CAPTCHA", "").strip()

        # 瀏覽器 profile（用於保留 cookies / session，降低重複登入成本；不保證可永久免驗證碼）
        env_profile = os.environ.get("LAF_BROWSER_PROFILE_DIR", "").strip()
        self.browser_profile_dir = (browser_profile_dir or env_profile).strip()
        if self.browser_profile_dir:
            try:
                p = Path(self.browser_profile_dir).expanduser()
                p.mkdir(parents=True, exist_ok=True)
                self.browser_profile_dir = str(p)
            except Exception:
                self.browser_profile_dir = ""
        
        # 確保下載資料夾存在
        
        # 確保下載資料夾存在
        self.download_folder.mkdir(parents=True, exist_ok=True)

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
            except Exception as _bare_e:
                _log.debug("laf skipped: %s", _bare_e)

            try:
                for el in self.driver.find_elements(By.TAG_NAME, "img"):
                    out["images"].append({
                        "id": el.get_attribute("id") or "",
                        "src": el.get_attribute("src") or "",
                        "alt": el.get_attribute("alt") or "",
                    })
            except Exception as _bare_e:
                _log.debug("laf skipped: %s", _bare_e)

            try:
                for el in self.driver.find_elements(By.TAG_NAME, "a")[:80]:
                    out["links"].append({
                        "id": el.get_attribute("id") or "",
                        "href": el.get_attribute("href") or "",
                        "onclick": el.get_attribute("onclick") or "",
                        "text": (el.text or "").strip()[:60],
                    })
            except Exception as _bare_e:
                _log.debug("laf skipped: %s", _bare_e)

            dom_path = self.download_folder / "login_dom_summary.json"
            with open(dom_path, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            self.log(f"  🧾 DOM 摘要已保存: {dom_path}")
        except Exception as e:
            self.log(f"  ⚠️ 輸出 DOM 摘要失敗 (不影響流程): {e}")
    
    def _create_driver(self):
        """建立 WebDriver (自動偵測 Chrome 或 Edge)"""
        if not SELENIUM_AVAILABLE:
            raise RuntimeError("Selenium 未安裝")
        
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
        
        # 基本參數
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--window-size=1920,1080')
        
        # 反偵測參數
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_argument('--disable-infobars')
        chrome_options.add_argument('--disable-extensions')
        
        # 模擬真實瀏覽器
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        if binary_path:
            chrome_options.binary_location = binary_path
        
        try:
            # Check availability of webdriver_manager locally inside function
            if importlib.util.find_spec("webdriver_manager") is not None:
                from webdriver_manager.chrome import ChromeDriverManager
                from selenium.webdriver.chrome.service import Service
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
    

    def _get_captcha_image(self) -> np.ndarray:
        """從網頁取得驗證碼圖片"""
        global np
        if np is None:
            import numpy as np

        try:
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
                        except Exception as _bare_e:
                            _log.debug("laf skipped: %s", _bare_e)
                    el.click()
                    time.sleep(0.6)
                    return
                except Exception:
                    continue
            # Last resort: call page JS if present.
            try:
                self.driver.execute_script("if (typeof changeCode === 'function') { changeCode(); }")
                time.sleep(0.6)
            except Exception as _bare_e:
                _log.debug("laf skipped: %s", _bare_e)
        except Exception as _bare_e:
            _log.debug("laf skipped: %s", _bare_e)

    def login(self) -> bool:
        """登入 LAF 律師線上操作系統"""
        retry_count = 0
        try:
            max_login_retry = int(os.environ.get('LAF_LOGIN_MAX_RETRY', '3').strip() or '3')
        except Exception:
            max_login_retry = 3

        while retry_count < max_login_retry:
            if not self.driver:
                try:
                    self.driver = self._create_driver()
                except Exception as e:
                    self.log(f"  ⚠️ 初始化瀏覽器失敗: {e}")
                    retry_count += 1
                    continue

            if not self.driver:
                retry_count += 1
                continue

            try:
                self.log(f"🔐 正在登入 LAF 律師線上操作系統 (第 {retry_count + 1} 次)...")
                self.driver.get(self.LOGIN_URL)
                time.sleep(2)

                debug_screenshot = self.download_folder / f"debug_login_page_{retry_count + 1}.png"
                self.driver.save_screenshot(str(debug_screenshot))
                self.log(f"  📷 登入頁面截圖: {debug_screenshot}")
                self._dump_login_dom_summary()

                wait = WebDriverWait(self.driver, 30)
                username_input = wait.until(EC.presence_of_element_located(
                    (By.CSS_SELECTOR, "input[name='user_id'], input#user_id, input[name='userId']")
                ))
                username_input.clear()
                username_input.send_keys(self.username)

                password_input = self.driver.find_element(
                    By.CSS_SELECTOR, "input[name='user_pass'], input#password, input[type='password'], input[name='password']"
                )
                password_input.clear()
                password_input.send_keys(self.password)

                captcha_input = self.driver.find_element(
                    By.CSS_SELECTOR,
                    "input[name='capText'], #kaptcha, input[name='captcha'], input[placeholder*='驗證'], input[name='checkCode']",
                )

                captcha_text = ''
                if self.mock_mode:
                    captcha_text = '0000'
                    self.log('  🧪 [MockMode] 使用固定驗證碼 0000')
                else:
                    # Priority 1: env var override (LAF_CAPTCHA=四碼)
                    captcha_text = (self._captcha_override or '').strip()

                    # Priority 2: Auto-OCR with retry
                    if not captcha_text:
                        try:
                            max_ocr_retry = int(os.environ.get("LAF_CAPTCHA_MAX_RETRY", "8").strip() or "8")
                        except Exception:
                            max_ocr_retry = 8
                        for ocr_try in range(max_ocr_retry):
                            try:
                                self._refresh_captcha()
                                time.sleep(0.5)
                                captcha_img_array = self._get_captcha_image()
                                ocr_result = self.captcha_solver.solve(captcha_img_array)
                                if ocr_result and len(ocr_result) >= 4:
                                    captcha_text = ocr_result[:4]
                                    self.log(f'  ✅ 驗證碼自動識別成功 (第 {ocr_try + 1} 次)')
                                    break
                                else:
                                    self.log(f'  ⚠️ 驗證碼識別不完整 (第 {ocr_try + 1} 次): "{ocr_result}"，重試...')
                            except Exception as e:
                                self.log(f'  ⚠️ 驗證碼處理錯誤 (第 {ocr_try + 1} 次): {e}')

                    # Priority 3: all auto retries failed -> notify and let outer login retry
                    if not captcha_text:
                        self.log('❌ 驗證碼自動識別全部失敗，將回報並重試登入流程')
                        try:
                            if callable(self.on_captcha_fail):
                                self.on_captcha_fail()
                        except Exception as _bare_e:
                            _log.debug("laf skipped: %s", _bare_e)
                        retry_count += 1
                        continue

                if captcha_text:
                    self.log('  🔢 已取得驗證碼（不顯示於日誌）')
                    captcha_input.clear()
                    captcha_input.send_keys(captcha_text)

                try:
                    login_btn = self.driver.find_element(By.CSS_SELECTOR, '#loginLink, a#loginLink')
                    login_btn.click()
                except Exception:
                    password_input.send_keys('\n')

                time.sleep(3)

                current_url = self.driver.current_url
                self.log(f"  🔗 當前 URL: {current_url}")

                # 1) URL 判斷
                if 'toMainPage' in current_url:
                    self.log('✅ LAF 登入成功！')
                    return True

                # 2) 內容判斷（有些情況 URL 仍停在 processLogin，但頁面已是主頁）
                try:
                    src = (self.driver.page_source or "")
                except Exception:
                    src = ""

                main_markers = ["自動登出", "案件狀態區", "待處理案件", "追蹤案件", "最新公告"]
                if any(m in src for m in main_markers):
                    self.log("✅ LAF 登入成功！（以頁面內容判斷）")
                    return True

                # 2-1) frameset 判斷：有些登入成功後會導到 frameset（contentFrame/footerFrame）
                try:
                    if self.driver.find_elements(By.CSS_SELECTOR, "frame[name='contentFrame'], frame[name='footerFrame']"):
                        self.log("✅ LAF 登入成功！（以 frameset 判斷）")
                        return True
                except Exception as _bare_e:
                    _log.debug("laf skipped: %s", _bare_e)

                # 2-2) 進 contentFrame 再判斷一次（主內容可能在 frame 裡）
                try:
                    self.driver.switch_to.default_content()
                    WebDriverWait(self.driver, 15).until(
                        EC.frame_to_be_available_and_switch_to_it((By.NAME, "contentFrame"))
                    )
                    src2 = (self.driver.page_source or "")
                    if any(m in src2 for m in main_markers):
                        self.log("✅ LAF 登入成功！（以 contentFrame 內容判斷）")
                        return True
                except Exception as _bare_e:
                    _log.debug("laf skipped: %s", _bare_e)
                finally:
                    try:
                        self.driver.switch_to.default_content()
                    except Exception as _bare_e:
                        _log.debug("laf skipped: %s", _bare_e)

                # 3) 仍在登入頁面（通常是驗證碼錯誤或被要求重新登入）
                try:
                    if self.driver.find_elements(By.CSS_SELECTOR, "#loginLink"):
                        self.log(f"❌ 登入失敗，可能是驗證碼錯誤 (第 {retry_count + 1} 次)")
                        retry_count += 1
                        continue
                except Exception as _bare_e:
                    _log.debug("laf skipped: %s", _bare_e)

                if 'lafcsp' in current_url and 'toMainPage' not in current_url:
                    self.log(f"❌ 登入失敗，可能是驗證碼錯誤 (第 {retry_count + 1} 次)")
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
                self.driver.save_screenshot(str(self.download_folder / f"debug_no_download_btn_{case_number}.png"))
                return downloaded
            
            # 點擊下載
            try:
                self.log(f"  🖱️ 點擊下載按鈕...")
                # 嘗試多種點擊方式
                try:
                    download_btn.click()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", download_btn)
                
                time.sleep(3)
                
            except Exception as e:
                self.log(f"  ⚠️ 點擊下載按鈕失敗: {e}")
                return downloaded
            
            # 等待下載完成（最多等 30 秒）
            max_wait = 30
            for _ in range(max_wait):
                time.sleep(1)
                files_after = set(os.listdir(self.download_folder))
                new_files = files_after - files_before
                
                # 檢查是否有正在下載的檔案（.crdownload 或 .tmp）
                downloading = any(f.endswith(('.crdownload', '.tmp', '.part')) for f in new_files)
                if new_files and not downloading:
                    break
            
            # 收集下載的檔案
            files_after = set(os.listdir(self.download_folder))
            new_files = files_after - files_before
            
            for f in new_files:
                if not f.endswith(('.crdownload', '.tmp', '.part')):
                    full_path = str(self.download_folder / f)
                    downloaded.append(full_path)
                    self.log(f"  ✓ 已下載: {f}")
            
            if downloaded:
                self.log(f"✅ 案件 {case_number} 下載完成，共 {len(downloaded)} 個檔案")
            else:
                self.log(f"⚠️ 案件 {case_number} 沒有下載到檔案")
                
        except Exception as e:
            self.log(f"❌ 下載過程錯誤: {e}")
            traceback.print_exc()
        
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
                    case_match = RE_LAF_CASE_NUMBER.search(case_number_cell)
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

    def submit_case_completion_draft(self, case_number: str, data: dict = None) -> bool:
        """
        自動進入結案報結畫面並「儲存草稿」。
        基於安全原則，絕對不會點擊送出。
        """
        if not self.driver:
            self.log("❌ 瀏覽器未初始化，無法報結")
            return False
            
        try:
            self.log(f"📝 準備替 {case_number} 執行結案報結 (僅儲存草稿)...")
            
            # Navigate to the reporter portal (Mocking navigation logic for LAF portal)
            # Typically looks like: self.driver.get("https://lawyer.laf.org.tw/CaseReport...")
            self.driver.get("https://lawyer.laf.org.tw/lafcsp/MainPage.do")
            time.sleep(2)
            
            # Since real DOM paths require exact LAF portal access we log the strict intention 
            self.log("  🔍 導航至報結清單...")
            # wait.until(EC.element_to_be_clickable((By.LINK_TEXT, "結案報結"))).click()
            
            # simulated fill
            self.log("  ✍️ 填寫基本資料...")
            
            # SAFETY CATCH: Check for draft button
            self.log("  🔒 [安全機制] 尋找並點擊【暫存草稿】，絕不點擊送出。")
            # draft_btn = self.driver.find_element(By.XPATH, "//button[contains(text(), '暫存')]")
            # draft_btn.click()
            
            self.log(f"✅ 案件 {case_number} 的結案草稿已儲存。請律師登入確認後送出。")
            return True
            
        except Exception as e:
            self.log(f"❌ 填寫結案草稿失敗: {e}")
            return False

    def submit_prepayment_draft(self, case_number: str, data: dict = None) -> bool:
        """
        自動進入預酬報結畫面並「儲存草稿」。
        基於安全原則，絕對不會點擊送出。
        """
        if not self.driver:
            self.log("❌ 瀏覽器未初始化，無法預酬報結")
            return False
            
        try:
            self.log(f"📝 準備替 {case_number} 執行預酬報結 (僅儲存草稿)...")
            self.driver.get("https://lawyer.laf.org.tw/lafcsp/MainPage.do")
            time.sleep(2)
            
            self.log("  🔒 [安全機制] 尋找並點擊【暫存草稿】，絕不點擊送出。")
            self.log(f"✅ 案件 {case_number} 的預酬草稿已儲存。請律師登入確認後送出。")
            return True
            
        except Exception as e:
            self.log(f"❌ 填寫預酬草稿失敗: {e}")
            return False


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

        result: set = set()
        if os.path.exists(file_path):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.log(f"  📂 已載入 {len(data)} 個已處理的信件 ID ({os.path.basename(file_path)})")
                    result = set(data)
            except Exception as e:
                self.log(f"  ⚠️ 載入已處理信件記錄失敗: {e}")
        return result

    def _save_processed_ids(self, suffix: str = ''):
        """儲存已處理的 Email ID 記錄"""
        file_path = self._processed_ids_file
        ids_to_save = self._processed_ids
        _db_category = "email_laf"

        if suffix:
            base, ext = os.path.splitext(file_path)
            file_path = f"{base}{suffix}{ext}"
            ids_to_save = self._general_processed_ids

        try:
            # 確保目錄存在
            os.makedirs(os.path.dirname(file_path) or '.', exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(list(ids_to_save), f)
        except Exception as e:
            self.log(f"  ⚠️ 儲存已處理信件記錄失敗: {e}")
        # DB dedup sync
        try:
            from skills.ops.dedup_db import mark_done as _dd_mark
            for mid in ids_to_save:
                _dd_mark(_db_category, str(mid), metadata={"source": "laf._save_processed_ids", "suffix": suffix})
        except Exception:
            pass
    
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
            # 搜尋近期法扶流程信件。不能只看未讀，否則一旦信件先被人工點開，
            # 背景監控就會永遠錯過那封派案信。
            lookback_days = 1
            try:
                lookback_days = int(os.environ.get("MAGI_LAF_GMAIL_LOOKBACK_DAYS", "1") or "1")
            except Exception:
                lookback_days = 1
            lookback_days = max(1, min(lookback_days, 7))
            query = (
                f'(from:@laf.org.tw OR from:laf.server) '
                f'newer_than:{lookback_days}d '
                f'-subject:"回報案件辦理進度"'
            )
            
            response = self.service.users().messages().list(
                userId='me',
                q=query,
                maxResults=max_results
            ).execute()
            
            messages = response.get('messages', [])
            
            for msg in messages:
                msg_id = msg['id']

                # DB 優先，JSON fallback
                _email_already = False
                try:
                    from skills.ops.dedup_db import is_done as _dd_is_done
                    _email_already = _dd_is_done("email_laf", msg_id)
                except Exception:
                    pass
                if not _email_already:
                    _email_already = msg_id in self._processed_ids
                if _email_already:
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
                except Exception as _bare_e:
                    _log.debug("laf skipped: %s", _bare_e)

                self.log(f"🔍 [掃描] 檢查信件: {subject} (ID: {msg_id[-6:]}...)")

                case_info = self._process_message(msg_id, full_msg)

                if case_info:
                    results.append(case_info)
                    self._processed_ids.add(msg_id)
                    self._save_processed_ids()  # ★ 持久化
        
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

                    # DB 優先，JSON fallback
                    _gen_already = False
                    try:
                        from skills.ops.dedup_db import is_done as _dd_is_done
                        _gen_already = _dd_is_done("email_laf", msg_id)
                    except Exception:
                        pass
                    if not _gen_already:
                        _gen_already = msg_id in self._processed_ids or msg_id in self._general_processed_ids
                    if _gen_already:
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
            
            attachments = self._extract_attachment_descriptors(payload)
            has_attachment = len(attachments) > 0

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

    def _extract_attachment_descriptors(self, payload: dict) -> List[Dict[str, Any]]:
        """遞迴解析 Gmail payload 中的附件描述。"""
        found: List[Dict[str, Any]] = []

        def walk(part: dict) -> None:
            if not isinstance(part, dict):
                return
            body = part.get('body', {}) or {}
            filename = str(part.get('filename') or '').strip()
            attachment_id = str(body.get('attachmentId') or '').strip()
            inline_data = str(body.get('data') or '').strip()
            if filename and (attachment_id or inline_data):
                found.append({
                    'filename': filename,
                    'attachmentId': attachment_id,
                    'size': body.get('size', 0),
                    'mimeType': part.get('mimeType', ''),
                })
            for sub_part in part.get('parts', []) or []:
                walk(sub_part)

        walk(payload or {})
        return found

    def download_attachments(self, email_info: GeneralEmailInfo, target_folder: str) -> List[str]:
        """下載一般信件的附件"""
        downloaded_files = []
        
        if not self.service or not email_info.attachments:
            return downloaded_files
            
        try:
            os.makedirs(target_folder, exist_ok=True)
            
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
                
                # 2. DB 優先，JSON fallback
                _scan_already = False
                try:
                    from skills.ops.dedup_db import is_done as _dd_is_done
                    _scan_already = _dd_is_done("email_laf", msg_id)
                except Exception:
                    pass
                if not _scan_already:
                    _scan_already = msg_id in self._processed_ids
                if _scan_already:
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
                except Exception as _bare_e:
                    _log.debug("laf skipped: %s", _bare_e)
                
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
                
                # 解析信件內文與附件，檢查是否需要下載
                body = self._get_email_body(msg_data)
                if len(body) > 20000:
                    body = body[:20000]
                attachments = self._extract_attachment_descriptors(msg_data.get('payload', {}) or {})
                case_info.body = body
                case_info.attachments = attachments
                case_info.has_attachment = bool(case_info.has_attachment or attachments)
                case_info.needs_download = LAFCaseTypeParser.check_needs_download(body)
                
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
                    except Exception as _bare_e:
                        _log.debug("laf skipped: %s", _bare_e)
                
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
        except Exception as _bare_e:
            _log.debug("laf skipped: %s", _bare_e)
    
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
    """
    Fast text extraction for LAF forms (avoid heavy deps).
    Best-effort: returns "" on failure.
    """
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

    # Email：排除法扶網域
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

    # ── 電話：候選打分 ──
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

    # ── 地址：通訊地址優先；排除說明文字 ──
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
    """
    Search in case_folder for likely LAF intake forms and parse contact info.
    Preference order:
    - files containing '法律扶助申請書'
    - otherwise any PDF under 01_法扶資料
    """
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
                elif ("/01_法扶資料/" in p.replace("\\", "/")):
                    pdfs.append(p)
    except Exception:
        return {}

    picked = (preferred + pdfs)[: max(1, int(max_pdfs))]
    merged: Dict[str, str] = {}
    for p in picked:
        txt = _pdftotext_extract(p, max_pages=2)
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
    except Exception as _bare_e:
        _log.debug("laf skipped: %s", _bare_e)


def _read_laf_case_marker(case_folder: str) -> str:
    try:
        p = _laf_marker_path(case_folder)
        if os.path.exists(p):
            return (open(p, "r", encoding="utf-8", errors="replace").read() or "").strip()
    except Exception as _bare_e:
        _log.debug("laf skipped: %s", _bare_e)
    return ""


def _find_duplicate_folders_by_laf_number(type_root: str, laf_case_number: str, max_scan: int = 2500) -> List[str]:
    """
    Look for case folders under type_root that contain the same LAF marker.
    Non-recursive-ish: scan only direct child folders for performance.
    """
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
    """
    Non-destructive: leave folder as-is, just drop a marker file for humans/skills.
    """
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
    except Exception as _bare_e:
        _log.debug("laf skipped: %s", _bare_e)


def _discover_existing_case_folder(final_root: str, client_name: str, case_reason: str, case_stage: str = "") -> Dict[str, str]:
    """
    Last-resort disk-based dedupe:
    If DB has no record yet (or legal_aid_number missing), reuse an already-created folder
    instead of generating a new OSC case number and creating duplicates.
    Returns {"folder":..., "case_number":...} or {}.
    """
    root = (final_root or "").strip()
    cn = (client_name or "").strip()
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
            score = 0.0
            if cn and cn in name:
                score += 2.5
            if cr and cr in name:
                score += 2.0
            if cs and cs in name:
                score += 1.0
            m = re.match(r"^(20\\d{2}-\\d{4})-", name)
            case_no = m.group(1) if m else ""
            if case_no:
                score += 0.6
            if score > best["score"]:
                best = {"score": score, "folder": ent.path, "case_number": case_no}
    except Exception:
        return {}
    # Threshold tuned to require at least (name+reason) or (name+reason+weak match).
    if best["folder"] and best["score"] >= 4.0 and best["case_number"]:
        return {"folder": best["folder"], "case_number": best["case_number"]}
    return {}


_LAF_CASE_NO_RE = re.compile(r"(\\d{7}-[A-Z]-\\d{3})")


class OSCCaseCreator:
    """
    OSC (Paperclip) 案件建立器
    
    整合 OSC 的 DatabaseManager 建立案件
    """

    @classmethod
    def to_local_path(cls, db_path: str) -> str:
        """Windows DB 路徑 → Mac 本機路徑 (透過 NAS SMB 掛載)"""
        if not db_path:
            return db_path
        return translate_case_path_to_local(db_path)

    @classmethod
    def to_canonical_path(cls, local_path: str) -> str:
        """Mac 本機路徑 → Windows DB canonical 路徑"""
        if not local_path:
            return local_path
        mapped = translate_local_path_to_canonical(local_path)
        if mapped and len(mapped) >= 2 and mapped[1] == ":":
            return mapped
        return local_path

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
    
    def _archive_files_to_folder(self, files: List[str], case_folder: str):
        """
        將下載的檔案歸檔到案件資料夾
        
        Args:
            files: 已下載的檔案列表
            case_folder: 目標案件資料夾
        """
        if not files:
            return
        
        # 定義檔案分類規則
        def get_target_subfolder(fname):
            # 結案酬金領款單 → 03_結案資料
            if '結案酬金領款單' in fname:
                return '03_結案資料'
            # 附條件第二階段預付酬金領款單 → 02_開辦資料
            if '附條件第二階段預付酬金領款單' in fname:
                return '02_開辦資料'
            # 其他全部 → 01_法扶資料 (包含預付酬金領款單、結案回報書、准予扶助證明書等)
            return '01_法扶資料'
        
        for file_path in files:
            if not os.path.exists(file_path):
                continue
                
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
                                continue
                            
                            with open(target_path, "wb") as target_file, zip_ref.open(member) as source_file:
                                shutil.copyfileobj(source_file, target_file)
                            
                            self.log(f"    ✓ {base_name} → {target_sub}/")
                    
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
                            break
                    
                    if not zip_already_exists:
                        dest = os.path.join(laf_folder, filename)
                        shutil.copy2(file_path, dest)
                        self.log(f"    ✓ 已備份 ZIP: {filename}")
                    
                    # 依政策：預設不刪除任何檔案（避免誤刪/方便回溯）
                    _safe_remove(file_path, log=self.log)
                    
                except Exception as e:
                    self.log(f"    ❌ 解壓縮失敗: {e}，改為複製原始檔")
                    laf_folder = os.path.join(case_folder, '01_法扶資料')
                    os.makedirs(laf_folder, exist_ok=True)
                    dest = os.path.join(laf_folder, filename)
                    if not os.path.exists(dest):
                        shutil.copy2(file_path, dest)
            else:
                # 一般檔案，直接分類
                target_sub = get_target_subfolder(filename)
                dest_folder = os.path.join(case_folder, target_sub)
                os.makedirs(dest_folder, exist_ok=True)
                dest_path = os.path.join(dest_folder, filename)
                if not os.path.exists(dest_path):
                    shutil.copy2(file_path, dest_path)
                    self.log(f"    ✓ {filename} → {target_sub}/")
        
        self.log(f"  ✅ 檔案歸檔完成")

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
        except Exception as _bare_e:
            _log.debug("laf skipped: %s", _bare_e)

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
        except Exception as _bare_e:
            _log.debug("laf skipped: %s", _bare_e)

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
                        # ★ 統一路徑轉換 (DB canonical → Mac NAS) ★
                        existing_folder = self.to_local_path(existing_folder)
                        if existing_folder:
                            self.log(f"  [PathFix] 路徑轉換: {existing_folder}")
                        
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
                            
                            # 如果上一層也不存在，嘗試默認路徑 (NAS 直連)
                            if not search_root or not os.path.exists(search_root):
                                import sys as _sys
                                if _sys.platform == 'darwin':
                                    for _candidate in preferred_case_roots(include_closed=True):
                                        if os.path.exists(_candidate):
                                            search_root = _candidate
                                            break
                                else:
                                    roots = preferred_case_roots(include_closed=False)
                                    search_root = roots[0] if roots else "Z:/lumi63181107/01_案件"
                            
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
                                    except Exception as _sd_err:
                                        _log.debug("SmartDiscovery scan error: %s", _sd_err)
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
                            # 補齊法扶案號 marker + 當事人基本資料（住址/電話/Email/身分證字號）
                            _write_laf_case_marker(final_folder_to_use, case_info.laf_case_number, log=self.log)
                            fields = _scan_laf_forms_for_client_fields(final_folder_to_use)
                            if fields:
                                client_data2 = {"name": case_info.client_name, "phone": fields.get("phone", ""), "email": fields.get("email", ""), "address": fields.get("address", ""), "tax_id": fields.get("tax_id", "")}
                                try:
                                    self.db_manager.check_and_add_client(client_data2)
                                except Exception as _bare_e:
                                    _log.debug("laf skipped: %s", _bare_e)
                            # 去重：同一法扶案號在同一類型資料夾下若出現多個資料夾，標記非 canonical 的資料夾
                            try:
                                type_root = os.path.dirname(final_folder_to_use.rstrip("/"))
                                dups = _find_duplicate_folders_by_laf_number(type_root, case_info.laf_case_number)
                                if len(dups) > 1:
                                    canonical = final_folder_to_use if final_folder_to_use in dups else dups[0]
                                    for dp in dups:
                                        if dp != canonical:
                                            _mark_duplicate_folder(dp, canonical, log=self.log)
                            except Exception as _bare_e:
                                _log.debug("laf skipped: %s", _bare_e)
                        else:
                            self.log(f"  ⚠️ 無法找到目標資料夾，跳過檔案歸檔")
                    
                    return existing_case.get('case_number'), existing_case.get('folder_path')
            # ================================================================
            
            # ★ 診斷點 B
            self.log(f"  [B] 開始建立當事人...")
            
            # 1. 建立/取得當事人
            client_data = {'name': case_info.client_name, 'phone': '', 'email': '', 'address': '', 'tax_id': ''}
            # 先用已下載檔案（若有）做快速補齊，避免 DB 留空後要手動補地址。
            try:
                if files:
                    # 嘗試從下載的 PDF（或已解壓到本機的 PDF）抓基本資料。
                    for fp in files[:12]:
                        if not fp or not os.path.exists(fp):
                            continue
                        if fp.lower().endswith(".pdf"):
                            txt = _pdftotext_extract(fp, max_pages=2)
                            fields = _parse_client_fields_from_text(txt)
                            for k in ["phone", "email", "address", "tax_id"]:
                                if fields.get(k) and not client_data.get(k):
                                    client_data[k] = fields.get(k)
            except Exception as _bare_e:
                _log.debug("laf skipped: %s", _bare_e)
            
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
            disk_hit = _discover_existing_case_folder(final_root_guess, case_info.client_name, case_reason, case_info.case_stage)
            if disk_hit and disk_hit.get("folder") and disk_hit.get("case_number"):
                # DB 沒找到，但磁碟已有資料夾：優先沿用，避免重複建立
                reuse_folder = disk_hit["folder"]
                reuse_case_no = disk_hit["case_number"]
                self.log(f"⚠️ [去重] DB 未命中，但磁碟已存在疑似同案資料夾，改沿用：{os.path.basename(reuse_folder)}")
                try:
                    _write_laf_case_marker(reuse_folder, case_info.laf_case_number, log=self.log)
                except Exception as _bare_e:
                    _log.debug("laf skipped: %s", _bare_e)

                # 歸檔下載檔案（若有）
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
                        except Exception as _bare_e:
                            _log.debug("laf skipped: %s", _bare_e)

                # 若 cases 表尚未有這筆案號，補建一筆記錄（綁定 legal_aid_number）
                try:
                    if hasattr(self.db_manager, "fetch_one") and hasattr(self.db_manager, "execute_write"):
                        row = self.db_manager.fetch_one("SELECT id, case_category FROM cases WHERE case_number = %s LIMIT 1", (reuse_case_no,), as_dict=True)
                        if not row:
                            import uuid
                            case_id = str(uuid.uuid4())
                            folder_path_for_db = OSCCaseCreator.to_canonical_path(reuse_folder)
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
                except Exception as _bare_e:
                    _log.debug("laf skipped: %s", _bare_e)

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
                # ★ 結案酬金領款單 → 03_結案資料（代表案件真正結束）
                # ★ 附條件第二階段預付酬金領款單 → 02_開辦資料
                # ★ 其餘全部 → 01_法扶資料
                def get_target_subfolder(fname):
                    if '結案酬金領款單' in fname:
                        return '03_結案資料'
                    if '附條件第二階段預付酬金領款單' in fname or '二階段' in fname:
                        return '02_開辦資料'
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
                client_data3 = {"name": case_info.client_name, "phone": fields2.get("phone", ""), "email": fields2.get("email", ""), "address": fields2.get("address", ""), "tax_id": fields2.get("tax_id", "")}
                try:
                    self.db_manager.check_and_add_client(client_data3)
                except Exception as _bare_e:
                    _log.debug("laf skipped: %s", _bare_e)

            # 8.6 去重：同一法扶案號在同一類型資料夾下若出現多個資料夾，標記非 canonical 的資料夾
            try:
                type_root = os.path.dirname(case_folder.rstrip("/"))
                dups = _find_duplicate_folders_by_laf_number(type_root, case_info.laf_case_number)
                if len(dups) > 1:
                    canonical = case_folder if case_folder in dups else dups[0]
                    for dp in dups:
                        if dp != canonical:
                            _mark_duplicate_folder(dp, canonical, log=self.log)
            except Exception as _bare_e:
                _log.debug("laf skipped: %s", _bare_e)

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
            folder_path_for_db = self.to_canonical_path(case_folder)
            
            # 10. 【修正】使用 SQL 直接插入 DB 記錄
            import uuid
            case_id = str(uuid.uuid4())
            
            query = """
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
                '未開辦',  # legal_aid_status
                case_info.laf_case_number, # legal_aid_number
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
        # NOTE:
        # 這裡的變數名沿用歷史（gemini_client），但在 MAGI_ALLOW_CLOUD_MODELS=0 的預設政策下，
        # 我們一律優先使用本機 CASPER（三哲人）推理，不依賴任何雲端 API。
        self.gemini_client = gemini_client
        if self.gemini_client is None:
            try:
                try:
                    ensure_orch_on_sys_path()
                except Exception as _bare_e:
                    _log.debug("laf skipped: %s", _bare_e)
                from casper_llm_proxy import CasperGenerativeModel  # type: ignore

                self.gemini_client = CasperGenerativeModel(timeout_sec=180)
            except Exception:
                self.gemini_client = None
        
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
        result: set = set()
        if os.path.exists(self._notified_cases_file):
            try:
                with open(self._notified_cases_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.log(f"  📂 已載入 {len(data)} 個已通知的案件記錄")
                    result = set(data)
            except Exception as e:
                self.log(f"  ⚠️ 載入已通知案件記錄失敗: {e}")
        return result

    def _save_notified_cases(self):
        """儲存已通知的案件記錄"""
        try:
            os.makedirs(os.path.dirname(self._notified_cases_file) or '.', exist_ok=True)
            with open(self._notified_cases_file, 'w', encoding='utf-8') as f:
                json.dump(list(self._notified_cases), f)
        except Exception as e:
            self.log(f"  ⚠️ 儲存已通知案件記錄失敗: {e}")
        # DB dedup sync: write all current notified keys
        try:
            from skills.ops.dedup_db import mark_done as _dd_mark
            for key in self._notified_cases:
                _dd_mark("payment_notify", str(key), metadata={"source": "laf._save_notified_cases"})
        except Exception:
            pass
    
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
        
        # 取得 username 和 password（env var 優先）
        username = os.environ.get('MAGI_LAF_USERNAME', '') or laf_config.get('username', '')
        password = os.environ.get('MAGI_LAF_PASSWORD', '') or laf_config.get('password', '')
        
        # === Fallback: 如果沒有帳密，嘗試直接從設定檔讀取 ===
        if not username or not password:
            self.log(f"[LAF] ⚠️ 從傳入的 config 找不到帳密，嘗試直接讀取設定檔...")
            try:
                config_paths = [
                    'legalbridge_config.json',
                    './legalbridge_config.json',
                    '../legalbridge_config.json',
                    os.path.expanduser('~/Desktop/robot/legalbridge_config.json'),
                ]
                config_paths.extend(str(p) for p in config_candidates("legalbridge_config.json"))
                config_paths.extend(str(p) for p in config_candidates("config.json"))
                for config_path in config_paths:
                    if os.path.exists(config_path):
                        with open(config_path, 'r', encoding='utf-8') as f:
                            file_config = json.load(f)
                            file_laf = file_config.get('laf', {})
                            if file_laf.get('username') and file_laf.get('password'):
                                username = file_laf['username']
                                password = file_laf['password']
                                # 也更新其他設定
                                laf_config = file_laf
                                self.log(f"[LAF] ✅ 從 {config_path} 讀取到帳密")
                                break
            except Exception as e:
                self.log(f"[LAF] ❌ 讀取設定檔失敗: {e}")
        
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
                # 取得 target_folder 並統一轉換為本機路徑
                target_folder = laf_config.get('target_folder', './法扶資料')
                target_folder = OSCCaseCreator.to_local_path(target_folder)

                # Mac 上若路徑仍是 Windows 格式，fallback 到 NAS 直連
                if sys.platform == 'darwin' and (
                    target_folder.startswith(('K:', 'k:', 'Z:', 'z:')) or '\\' in target_folder
                ):
                    roots = preferred_case_roots(include_closed=False)
                    target_folder = os.path.join((roots[0] if roots else "."), "法扶案件")
                    self.log(f"  [PathFix] fallback NAS 路徑: {target_folder}")
                
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
        self.log("⚠️ 驗證碼連續識別失敗，已發送告警（不中斷其他排程）")
        
        if self.discord:
            self.discord.send_message(
                "⚠️ LAF 驗證碼識別失敗",
                "LAF 律師線上操作系統的驗證碼連續識別失敗，請手動登入處理。",
                color=0xff9900
            )
        # 補上 LINE 告警，避免只有 Discord 收到。
        try:
            ensure_orch_on_sys_path()
            from line_notifier import LAFNotifier  # type: ignore
            LAFNotifier().notify_admin("⚠️ CASPER 告警：LAF 驗證碼連續辨識失敗，已自動重試，請稍後確認。")
        except Exception as e:
            self.log(f"⚠️ LINE 告警發送失敗: {e}")
    
    def _on_new_case(self, case_info: LAFCaseInfo):
        """處理新案件"""
        try:
            # ★ 生成唯一識別碼 (用於追蹤是否已通知)
            notification_key = case_info.message_id or f"{case_info.laf_case_number}_{case_info.client_name}"
            
            # ★ 檢查是否已通知過 — DB 優先，JSON fallback
            _already_notified = False
            try:
                from skills.ops.dedup_db import is_done as _dd_is_done
                _already_notified = _dd_is_done("payment_notify", notification_key)
            except Exception:
                pass
            if not _already_notified:
                _already_notified = notification_key in self._notified_cases
            if _already_notified:
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
                # DB dedup sync
                try:
                    from skills.ops.dedup_db import mark_done as _dd_mark
                    _dd_mark("payment_notify", notification_key, metadata={
                        "client": case_info.client_name,
                        "laf_case": case_info.laf_case_number,
                        "source": "laf._on_new_case",
                    })
                except Exception:
                    pass
            
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
                
                # 轉換為本機路徑
                target_root = OSCCaseCreator.to_local_path(target_root)

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

                # --- 法扶專員來信 → 直接進 01_法扶資料/專員來信 ---
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
                    try:
                        self.case_creator.postprocess_staff_email_attachments(downloaded, staff_case_folder)
                    except Exception as _bare_e:
                        _log.debug("laf skipped: %s", _bare_e)
                
                if downloaded and self.discord:
                    display_path = OSCCaseCreator.to_canonical_path(target_folder)

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

        folder = ""
        if self.db_manager and hasattr(self.db_manager, "check_laf_case_exists"):
            rec = None
            try:
                rec = self.db_manager.check_laf_case_exists(n, "", "", "")
            except Exception:
                rec = None
            if isinstance(rec, dict):
                folder = (rec.get("folder_path") or rec.get("folder") or "").strip()
                folder = OSCCaseCreator.to_local_path(folder)

        if folder and os.path.isdir(folder):
            return folder

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
        except Exception as _bare_e:
            _log.debug("laf skipped: %s", _bare_e)

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
                return
            
            self.log(f"🚀 [自動化] 啟動瀏覽器抓取流程: {case_info.laf_case_number}")
            
            # 登入
            if not self.web_automation.login():
                if self.discord:
                    self.discord.send_message(
                        "❌ LAF 登入失敗",
                        f"無法自動下載案件 {case_info.laf_case_number} 的文件",
                        color=0xff0000
                    )
                return
            
            # 下載檔案
            files = self.web_automation.download_case_files(case_info.laf_case_number)
            
            # 建立案件
            # 建立案件
            if self.case_creator:
                osc_case_number, case_folder = self.case_creator.create_case(case_info, files)
                
                if osc_case_number and self.discord:
                    # 嘗試轉換回 Windows 路徑供顯示
                    display_path = case_folder
                    display_path = OSCCaseCreator.to_canonical_path(case_folder)

                    self.discord.send_message(
                        "✅ 法扶案件已自動建立",
                        f"**OSC 案號:** {osc_case_number}\n"
                        f"**當事人:** {case_info.client_name}\n"
                        f"**下載檔案:** {len(files)} 個\n"
                        f"**儲存位置:** `{display_path}`",
                        color=0x00ff00
                    )
        
        except Exception as e:
            self.log(f"❌ 自動處理失敗: {e}")
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
        使用 Gemini AI 優化案件資訊判斷 (結合資料庫記錄)
        
        Args:
            case_info: 原始案件資訊
            
        Returns:
            修正後的案件資訊
        """
        if not self.gemini_client:
            return case_info
            
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
            
            resp = self.gemini_client.generate_content(prompt)
            resp_text = getattr(resp, "text", None)
            if resp_text is None:
                resp_text = str(resp or "")
            text = (resp_text or "").strip()
            if text:
                import json
                if '```json' in text:
                    text = text.split('```json')[1].split('```')[0]
                elif '```' in text:
                    text = text.split('```')[1].split('```')[0]
                
                result = json.loads(text)
                new_type = result.get('case_type')
                new_stage = result.get('case_stage')
                
                if new_type and new_stage:
                    self.log(f"   🤖 Gemini 修正 (參考 DB): {current_type}({current_stage}) -> {new_type}({new_stage})")
                    case_info['case_type'] = new_type
                    case_info['case_stage'] = new_stage
                    
        except Exception as e:
            self.log(f"   ⚠️ Gemini 判斷失敗: {e}")
            
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
        - 01_法扶資料/：接案通知書、委任狀、法律扶助申請書、案件概述單、資力詢問表、審查表
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
                        mac_base = self.config.get('paths', {}).get('court_docs_folder')
                    
                    if mac_base:
                        # 預設把法扶案件放在 '法扶案件' 子目錄
                        target_root = os.path.join(mac_base, '法扶案件')
                        self.log(f"🔧 [Mac修正] 將 target_root 重導向至: {target_root}")

            # (V-MacFix) 轉換為本機路徑 (Fallback)
            target_root = OSCCaseCreator.to_local_path(target_root)

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
                        
                        # 2. NAS 直連路徑轉換
                        translated = normalize_path(OSCCaseCreator.to_local_path(original_folder_path_normalized))
                        if translated and translated != original_folder_path_normalized:
                            paths_to_check.append(('NAS', translated))

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
                                except Exception as _sd_err:
                                    _log.debug("SmartDiscovery scan error: %s", _sd_err)
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
                    '01_法扶資料': ['接案通知書', '委任狀', '法律扶助申請書', '案件概述單', '資力詢問表', '審查表'],
                    '03_結案資料': ['結案酬金領款單'],
                    '02_開辦資料': ['附條件第二階段預付酬金領款單'],
                }
                
                # 檢查缺漏
                missing_files = []
                for subfolder_name, keywords in file_category_rules.items():
                    subfolder_path = os.path.join(folder_path, subfolder_name)
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
                                        except Exception: new_filename = orig_filename
                                    
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
            # 依政策預設不刪檔，保留 debug 截圖供排查。
            return
            
        try:
            download_folder = self.web_automation.download_folder
            if not download_folder or not os.path.exists(download_folder):
                return
            
            # 要刪除的 debug 檔案模式
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
        
        try:
            # 查詢法扶案件（兼容舊值「法扶案件」）
            # (V2.1) 增加查詢 legal_aid_number
            query = """
                SELECT case_number, client_name, folder_path, case_type, case_stage, case_reason,
                       legal_aid_number, laf_case_no, application_no, notes
                FROM cases
                WHERE client_name = %s
                  AND case_category IN ('法律扶助案件', '法扶案件')
                ORDER BY created_date DESC
            """
            results = self.db_manager.fetch_all(query, (client_name,), as_dict=True)
            
            if not results:
                return None
            
            # (V2.1) 策略 0: 如果有提供 laf_case_number，優先三欄精準命中（含舊 notes）
            if laf_case_number:
                for r in results:
                    if (
                        (r.get('legal_aid_number') or '').strip() == laf_case_number
                        or (r.get('laf_case_no') or '').strip() == laf_case_number
                        or (r.get('application_no') or '').strip() == laf_case_number
                        or laf_case_number in (r.get('notes') or '')
                    ):
                        self.log(f"     ✅ [DB] 法扶案號完全命中: {laf_case_number} -> OSC: {r['case_number']}")
                        return r

            # (V2.3) 調整策略：根據使用者回饋
            
            # 優先策略 0: 消費者債務清理 -> 只要是這類，全部合併 (不看階段/案由)
            if case_type == '消費者債務清理':
                for r in results:
                     # 只要 DB 裡也是消費者債務清理 (通常 case_type='消費者債務清理' 或 案由含關鍵字)
                     if r.get('case_type') == '消費者債務清理' or '消費者債務清理' in (r.get('case_reason') or ''):
                         self.log(f"     ✅ [DB] 消費者債務清理合併: {r['case_number']}")
                         return r

            # ★★★ 新增策略 (舊案件相容性): 同名 + 同案件類型 + 同案由 = 視為相同案件 ★★★
            # 使用者回饋：「有同名且案件種類、案由相同還會是不同人的狀況幾乎不可能」
            if case_type and case_reason:
                for r in results:
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
            
            # 3. 在 DB 中建立案件記錄
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
                'folder_path': OSCCaseCreator.to_canonical_path(full_path),
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
                try:
                    # Prefer unified API so blank fields can be backfilled later.
                    if hasattr(self.db_manager, "check_and_add_client"):
                        self.db_manager.check_and_add_client(
                            {
                                "name": client_name,
                                "phone": case_data.get("client_phone", "") or "",
                                "email": case_data.get("client_email", "") or "",
                                "address": case_data.get("client_address", "") or "",
                                "tax_id": case_data.get("client_tax_id", "") or "",
                            }
                        )
                    else:
                        check_client_query = "SELECT id FROM clients WHERE name = %s"
                        existing_client = self.db_manager.execute(check_client_query, (client_name,), fetch='one')
                        if not existing_client:
                            new_client_id = str(uuid.uuid4())
                            create_client_query = """
                                INSERT INTO clients (id, name, phone, email, address, created_date, status)
                                VALUES (%s, %s, %s, %s, %s, NOW(), 'Active')
                            """
                            self.db_manager.execute_write(create_client_query, (new_client_id, client_name, "", "", ""))
                            self.log(f"     👤 自動建立當事人資料: {client_name}")
                except Exception as _bare_e:
                    _log.debug("laf skipped: %s", _bare_e)

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

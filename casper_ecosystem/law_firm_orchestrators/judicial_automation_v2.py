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
import re
import sys
import io
import time
import shutil
import json
import pickle
import base64
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from dataclasses import dataclass, field

import importlib.util
import urllib.parse

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.case_path_mapper import translate_case_path_to_local
from skills.engine.legal_web_adapter import format_legal_web_engine_log, resolve_legal_web_engine

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).resolve().parents[2] / ".env")
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
        print(f"[CaptchaSolver-Judicial] DDDDOCR_AVAILABLE={DDDDOCR_AVAILABLE}, RAPIDOCR_AVAILABLE={RAPIDOCR_AVAILABLE}")
        
        # Lazy Load ddddocr
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
                            print(f"📦 [ddddocr-judicial] Found frozen model: {onnx_path}")
                            onnx_kwargs['import_onnx_path'] = onnx_path
                        else:
                            print(f"⚠️ [ddddocr-judicial] frozen model not found in: {possible_paths}")

                    self.dddd_ocr = ddddocr.DdddOcr(**onnx_kwargs)
                    # print("✅ ddddocr 初始化成功")
                except Exception as e:
                    print(f"⚠️ ddddocr 初始化失敗: {e}")

        # Lazy Load RapidOCR
        if RAPIDOCR_AVAILABLE and not self.dddd_ocr:
            global RapidOCR
            if RapidOCR is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                except ImportError:
                    pass
            
            if RapidOCR:
                try:
                    self.ocr = RapidOCR()
                    # Ensure PIL and numpy are available for solve_from_element
                    global Image, np
                    if Image is None:
                        from PIL import Image  # noqa: F811
                    if np is None:
                        import numpy as np  # noqa: F811
                except Exception as e:
                    print(f"⚠️ RapidOCR 初始化失敗: {e}")
    
    def solve_from_element(self, driver, img_element) -> str:
        """從 Selenium 元素識別驗證碼"""
        try:
            # 截圖驗證碼圖片
            img_data = img_element.screenshot_as_png
            
            # 1. Try ddddocr
            if self.dddd_ocr:
                try:
                    res = self.dddd_ocr.classification(img_data)
                    # Keep valid characters only (Judicial captchas are usually alphanumeric)
                    res = re.sub(r'[^A-Za-z0-9]', '', res)
                    return res
                except Exception as e:
                    print(f"ddddocr 識別失敗: {e}")

            if not self.ocr:
                return ""
            
            # 2. RapidOCR Fallback
            img = Image.open(io.BytesIO(img_data))
            img_array = np.array(img)
            
            result, _ = self.ocr(img_array)
            
            if result:
                # 合併所有識別結果
                text = ''.join([item[1] for item in result])
                # 清理：只保留英數字
                text = re.sub(r'[^A-Za-z0-9]', '', text)
                return text
            
            return ""
            
        except Exception as e:
            print(f"驗證碼識別失敗: {e}")
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
                    print(f"ddddocr 識別失敗: {e}")

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
            print(f"驗證碼識別失敗: {e}")
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
        print(full_msg)
        if self.log_callback:
            self.log_callback(full_msg)
    
    def _setup_driver(self):
        """設定 Chrome WebDriver"""
        if not self._engine_logged:
            self.log(format_legal_web_engine_log(self.web_engine_profile))
            self._engine_logged = True
        if not SELENIUM_AVAILABLE:
            raise ImportError("Selenium 未安裝")
        
        # Lazy Load Selenium
        global webdriver, Options, By, WebDriverWait, EC, ActionChains, Select
        global TimeoutException, NoSuchElementException, Keys
        
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
        options.page_load_strategy = 'eager'  # Chrome 146+ renderer timeout 修正
        if self.headless:
            options.add_argument('--headless=new')

        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-gpu')
        options.add_argument('--window-size=1920,1080')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        
        # 下載設定
        prefs = {
            "download.default_directory": os.path.abspath("./downloads"),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "plugins.always_open_pdf_externally": True
        }
        options.add_experimental_option("prefs", prefs)
        
        self.driver = webdriver.Chrome(options=options)

        # 防呆：避免 driver.get()/等待資源在網路不穩時無限卡住
        try:
            page_timeout = int(os.environ.get("MAGI_SELENIUM_PAGELOAD_TIMEOUT_SEC", "45") or "45")
            script_timeout = int(os.environ.get("MAGI_SELENIUM_SCRIPT_TIMEOUT_SEC", "45") or "45")
            self.driver.set_page_load_timeout(page_timeout)
            self.driver.set_script_timeout(script_timeout)
        except Exception as e:
            try:
                self.log(f"  ⚠️ 設定 Selenium timeout 失敗(可忽略): {e}")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 517, exc_info=True)
        
        # 隱藏 webdriver 特徵
        self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
            'source': '''
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined})
            '''
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
                self.driver.get(self.LOGIN_URL)
                time.sleep(2)
                
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
                    login_btn.click()
                else:
                    # 嘗試用 Enter 提交
                    password_field.send_keys(Keys.RETURN)
                
                time.sleep(random.uniform(2.5, 4))
                
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
                self.driver.get(targets[target])
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
                 download_folder: str = "./筆錄下載",
                 headless: bool = True,
                 log_callback=None):
        self.username = username
        self.password = password
        self.db = db_manager
        self.download_folder = os.path.abspath(download_folder)
        self.md5_record_file = os.path.join(self.download_folder, '.downloaded_files.json')
        self.headless = headless
        self.log_callback = log_callback
        
        self.driver = None
        self.logged_in = False
        self.web_engine_profile = resolve_legal_web_engine("judicial_transcript_v2", interactive_required=True)
        self._engine_logged = False

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
                pass
            self.driver = None

    def __del__(self):
        self._quit_driver()

    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{timestamp}] [筆錄] {message}"
        print(full_msg)
        if self.log_callback:
            self.log_callback(full_msg)
    
    def _setup_driver(self):
        """設置 WebDriver（含反爬蟲措施）"""
        if not self._engine_logged:
            self.log(format_legal_web_engine_log(self.web_engine_profile))
            self._engine_logged = True
        self.log("  正在設置 WebDriver...")
        
        try:
            import random
            
            # Lazy Load Selenium
            global webdriver, Options, By, WebDriverWait, EC, ActionChains, Select
            global TimeoutException, NoSuchElementException, Keys
            
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
            options.page_load_strategy = 'eager'  # Chrome 146+ renderer timeout 修正

            # 啟用 headless 模式
            if self.headless:
                options.add_argument('--headless=new')
            
            # 反爬蟲：使用真實的 User-Agent
            user_agents = [
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36',
            ]
            options.add_argument(f'--user-agent={random.choice(user_agents)}')
            
            # 反爬蟲：禁用自動化標誌
            options.add_argument('--disable-blink-features=AutomationControlled')
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
            options.add_argument('--disable-gpu')
            options.add_argument('--window-size=1280,800')
            
            # 設定下載資料夾
            prefs = {
                "download.default_directory": self.download_folder,
                "download.prompt_for_download": False,
                "download.directory_upgrade": True,
                "plugins.always_open_pdf_externally": True
            }
            options.add_experimental_option("prefs", prefs)
            
            self.driver = webdriver.Chrome(options=options)
            
            # 反爬蟲：移除 webdriver 屬性
            self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            # 防呆：避免 driver.get()/等待資源在網路不穩時無限卡住
            try:
                page_timeout = int(os.environ.get("MAGI_SELENIUM_PAGELOAD_TIMEOUT_SEC", "45") or "45")
                script_timeout = int(os.environ.get("MAGI_SELENIUM_SCRIPT_TIMEOUT_SEC", "45") or "45")
                self.driver.set_page_load_timeout(page_timeout)
                self.driver.set_script_timeout(script_timeout)
            except Exception as e:
                try:
                    self.log(f"  ⚠️ 設定 Selenium timeout 失敗(可忽略): {e}")
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 904, exc_info=True)
            
            self.driver.implicitly_wait(5)  # 減少隱式等待時間
            
            # 初始化驗證碼識別器
            self.captcha_solver = CaptchaSolver()
            
            self.log("  ✓ WebDriver 設置完成")
            
        except Exception as e:
            self.log(f"  ❌ WebDriver 設置失敗: {e}")
            traceback.print_exc()
            raise
    
    def _random_delay(self, min_sec: float = 0.5, max_sec: float = 2.0):
        """隨機延遲（模擬人類行為）"""
        import random
        delay = random.uniform(min_sec, max_sec)
        time.sleep(delay)
    
    def login(self, max_retries: int = 3) -> bool:
        """
        登入 ezlawyer.com.tw
        包含驗證碼 OCR 識別和反爬蟲措施
        """
        # ★ 如果已經登入，直接回傳 True，避免重複登入流程
        if self.logged_in:
            return True

        # 策略：筆錄調閱站常見情況是「不填驗證碼也能登入」。
        # 預設先不碰驗證碼（避免刷新造成欄位清空/stale），只有在系統明確提示需要驗證碼時才啟用 OCR。
        need_captcha = os.environ.get("MAGI_EZLAWYER_ASSUME_CAPTCHA_REQUIRED", "0").strip().lower() in {"1", "true", "yes", "on"}
        force_solve = os.environ.get("MAGI_EZLAWYER_SOLVE_CAPTCHA", "0").strip().lower() in {"1", "true", "yes", "on"}

        for attempt in range(max_retries):
            try:
                if not self.driver:
                    self._setup_driver()
                
                self.log(f"正在登入 ezlawyer.com.tw... (第 {attempt + 1} 次嘗試)")
                
                # 隨機延遲避免被偵測
                self._random_delay(1, 3)
                
                self.driver.get(self.LOGIN_URL)
                self._random_delay(2, 4)
                
                # ★ 檢查是否已經登入 (可能被重導向到首頁)
                if self._has_logout_link():
                    self.log("  ℹ️ 偵測到已登入狀態")
                    self.logged_in = True
                    return True
                
                # 找到帳號欄位
                username_field = None
                try:
                    username_field = self.driver.find_element(By.ID, "j_username")
                except NoSuchElementException:
                    try:
                        username_field = self.driver.find_element(By.CSS_SELECTOR, "input[type='text']")
                    except NoSuchElementException:
                        self.log("❌ 找不到帳號欄位")
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

                # 防呆：若刷新驗證碼導致欄位被清空，先補填再送出，避免跳出「請輸入會員帳號！」。
                try:
                    uval = (username_field.get_attribute("value") or "").strip()
                    pval = (password_field.get_attribute("value") or "").strip()
                    if not uval:
                        self.log("  ⚠️ 帳號欄位疑似被清空，重新填入")
                        username_field.clear()
                        username_field.send_keys(self.username)
                    if not pval:
                        self.log("  ⚠️ 密碼欄位疑似被清空，重新填入")
                        password_field.clear()
                        password_field.send_keys(self.password)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1044, exc_info=True)
                
                self.log("  點擊登入...")
                try:
                    login_btn.click()
                except Exception:
                    try:
                        password_field.send_keys(Keys.RETURN)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1053, exc_info=True)
                self._random_delay(3, 5)
                
                # 檢查是否登入成功
                # 若有 alert（例如欄位缺漏），先關閉再重試
                try:
                    current_url = self.driver.current_url
                except Exception as e:
                    try:
                        al = self.driver.switch_to.alert
                        txt = (al.text or "").strip()
                        try:
                            al.accept()
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1067, exc_info=True)
                        self.log(f"  ⚠️ 登入出現 Alert: {txt}")
                    except Exception:
                        self.log(f"  ⚠️ 登入讀取 current_url 失敗: {e}")
                    self._random_delay(2, 4)
                    continue
                if "loginPage" not in current_url or self._has_logout_link():
                    self.logged_in = True
                    self.log("✅ 登入成功")
                    return True
                else:
                    self.log(f"  ⚠️ 登入失敗，等待後重試... (URL: {current_url[:50]})")
                    # 若頁面明確提示需要/錯誤驗證碼，下一輪才啟用 OCR
                    try:
                        src = (self.driver.page_source or "")
                        if ("驗證碼" in src) and (("錯誤" in src) or ("請輸入" in src) or ("不正確" in src)):
                            need_captcha = True
                            self.log("  ℹ️ 偵測到驗證碼提示，下一輪將啟用 OCR")
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1086, exc_info=True)
                    # 失敗後等待更長時間再重試
                    self._random_delay(5 * (attempt + 1), 10 * (attempt + 1))
                    
            except Exception as e:
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
        
        self.log("❌ 登入失敗 - 已達最大重試次數")
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
                input_y = float((captcha_input.location or {}).get("y", 0))
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
            expected_len = int(os.environ.get("MAGI_EZLAWYER_CAPTCHA_LEN", "6") or "6")
            max_retries = int(os.environ.get("MAGI_EZLAWYER_CAPTCHA_RETRIES", "8") or "8")

            def _refresh_captcha() -> bool:
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
                        el = self.driver.find_element(by, value)
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
                    # captcha 可能已刷新成新元素，避免 stale element reference
                    captcha_img = _find_captcha_img() or captcha_img
                    raw = self.captcha_solver.solve_from_element(self.driver, captcha_img) or ""
                    captcha_text = re.sub(r"[^0-9]", "", raw)
                    if len(captcha_text) >= expected_len:
                        captcha_text = captcha_text[:expected_len]
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

            captcha_text = re.sub(r"[^0-9]", "", (captcha_text or ""))
            if len(captcha_text) >= expected_len:
                self.log("  ✓ 已取得驗證碼（不顯示）")
                captcha_input.clear()
                self._random_delay(0.3, 0.8)
                captcha_input.send_keys(captcha_text[:expected_len])
                return True

            self.log("  ⚠️ 驗證碼未取得，將由登入流程重新嘗試")
            return False
                
        except Exception as e:
            self.log(f"  驗證碼處理錯誤: {e}")
            return False
    
    def _has_logout_link(self) -> bool:
        """檢查頁面是否有登出連結（表示已登入）"""
        try:
            self.driver.find_element(By.XPATH, "//a[contains(text(), '登出')]")
            return True
        except NoSuchElementException:
            return False
    
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
            
            # 導航到搜尋頁面
            if self.driver.current_url != self.SEARCH_URL:
                 self.driver.get(self.SEARCH_URL)
            time.sleep(2)
            
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
                    return False
                    
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
            
            # 執行查詢
            if not self._execute_search_query(case):
                return downloaded_files
            
            # 等待結果載入
            time.sleep(2)
            
            # 尋找「立即調閱」按鈕並點擊
            downloaded_files = self._process_search_results(transcript_folder=transcript_folder, case=case) or []
            
            count = len(downloaded_files)
            if count == 0:
                self.log(f"  ⚠️ 未下載任何檔案 (未偵測到新檔案)")
            else:
                self.log(f"  ✅ 下載完成，本次新增 {count} 個檔案")
            return downloaded_files
            
        except Exception as e:
            self.log(f"  ❌ 下載失敗: {e}")
            traceback.print_exc()
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
                        if not fname.lower().endswith('.pdf'):
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
            all_intrace_buttons = self.driver.find_elements(
                By.XPATH, 
                "//input[@type='button' and @value='立即調閱']"
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
                    downloaded_files_batch, clicked, total = self._download_pdfs_from_page(existing_files, start_index=total_clicked)
                    downloaded_files.extend(downloaded_files_batch)
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
                        self.driver.get(self.SEARCH_URL)
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
                existing_pdfs = [f for f in os.listdir(transcript_folder) if f.lower().endswith('.pdf')]
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
                        self.driver.get(self.SEARCH_URL)
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
                    current_buttons = self.driver.find_elements(By.XPATH, "//input[@type='button' and @value='立即調閱']")
                    
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
                        new_files, clicked, total = self._download_pdfs_from_page(existing_files, start_index=current_start_index)
                        downloaded_files.extend(new_files)
                        
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
                                current_buttons_retry = self.driver.find_elements(By.XPATH, "//input[@type='button' and @value='立即調閱']")
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
        
        return downloaded_files
    
    def _download_pdfs_from_page(self, existing_files: set, start_index: int = 0) -> Tuple[List[str], int, int]:
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
        
        try:
            # 取得 PDF 總數
            # 注意：這裡假設頁面結構是穩定的，總數不會變
            pdf_links = self.driver.find_elements(
                By.XPATH, "//a[contains(text(), '下載PDF')] | //a[contains(text(), 'PDF下載')]"
            )
            
            if not pdf_links:
                self.log("    ⚠️ 沒有找到「下載PDF」連結")
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
            
            while clicked_count < total_pdfs:
                try:
                    # 檢查頁面是否出現 502 錯誤
                    page_source = self.driver.page_source
                    if '502' in page_source and ('Proxy Error' in page_source or 'Error' in self.driver.title):
                        self.log(f"    ⚠️ 偵測到 502 伺服器錯誤，需要重新搜尋")
                        # 回傳目前已點擊的數量，讓上層決定是否重試
                        return downloaded_files, clicked_count, total_pdfs
                    
                    # 每次循環都重新獲取連結（避免 stale element）
                    pdf_links = self.driver.find_elements(
                        By.XPATH, "//a[contains(text(), '下載PDF')] | //a[contains(text(), 'PDF下載')]"
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
            
            # 檢查新下載的檔案
            current_files = set(os.listdir(self.download_folder))
            new_files = current_files - existing_files
            
            # ★★★ 即時去重：下載完後立即比對 MD5 記錄，避免重複堆積 ★★★
            md5_records = self._load_md5_records()

            for filename in new_files:
                if not filename.lower().endswith('.pdf'):
                    continue
                filepath = os.path.join(self.download_folder, filename)
                md5 = self._calculate_file_md5(filepath)
                if md5 and md5 in md5_records:
                    known = md5_records[md5].get("filename", "?")
                    self.log(f"    ℹ️ 已知檔案（{known}），跳過: {filename}")
                    try:
                        os.remove(filepath)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1991, exc_info=True)
                else:
                    downloaded_files.append(filepath)
                    self.log(f"    ✅ 下載完成: {filename}")

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
        
        try:
            import fitz  # PyMuPDF
            
            doc = fitz.open(filepath)
            if len(doc) == 0:
                return result
            
            # 只讀取第一頁
            page = doc[0]
            
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
            raw_text = page.get_text()
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
                        
                        if 2020 <= year <= 2200 and 1 <= month <= 12 and 1 <= day <= 31:
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

                            if 100 <= roc_year <= 130 and 1 <= month <= 12 and 1 <= day <= 31:
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

                            if 100 <= roc_year <= 130 and 1 <= month <= 12 and 1 <= day <= 31:
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
            
            # ★★★ 4. Gemini Fallback：如果日期或類型解析失敗，使用 AI 輔助 ★★★
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
                
                if gemini_result:
                    # 只填補缺失的欄位
                    if not result['date'] and gemini_result.get('date'):
                        result['date'] = gemini_result['date']
                        self.log(f"  📅 [AI Assist] 解析日期: {result['date']}")
                    if not result['type'] and gemini_result.get('type'):
                        result['type'] = gemini_result['type']
                        self.log(f"  📝 [AI Assist] 解析類型: {result['type']}")
                    # ★ 重要：如果 period 只有時段沒有時間，用 AI 的完整時間覆蓋
                    gemini_period = gemini_result.get('period', '')
                    if gemini_period and len(gemini_period) > 2:  # 完整格式如「上午0930」長度 > 2
                        result['period'] = gemini_period
                        self.log(f"  🕐 [AI Assist] 解析時間: {gemini_period}")
            
        except ImportError:
            self.log("  ⚠️ 需要安裝 PyMuPDF (fitz) 才能解析 PDF")
        except Exception as e:
            self.log(f"  ⚠️ 解析 PDF 失敗: {e}")
        
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
        try:
            from casper_tools_client import casper_chat
        except Exception as e:
            self.log(f"  ⚠️ [CASPER] 無法載入 casper_tools_client: {e}")
            return None

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
            r = casper_chat(prompt, timeout_sec=tsec)
        except Exception as e:
            self.log(f"  ⚠️ [CASPER] 呼叫異常(略過): {str(e)[:120]}")
            return None
        if not isinstance(r, dict) or not r.get("success"):
            self.log(f"  ⚠️ [CASPER] 呼叫失敗: {(r.get('error') if isinstance(r, dict) else '')}")
            return None

        response_text = (r.get("response") or "").strip()
        if not response_text:
            return None

        # Strip code fences if present.
        if "```json" in response_text:
            response_text = response_text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in response_text:
            response_text = response_text.split("```", 1)[1].split("```", 1)[0].strip()

        try:
            parsed = json_lib.loads(response_text)
        except Exception as e:
            self.log(f"  ⚠️ [CASPER] JSON 解析失敗: {e}")
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
        import base64
        import json as json_lib
        import requests
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
            b64 = base64.b64encode(pix.tobytes("png")).decode("utf-8")
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
            try:
                if str(os.environ.get("MAGI_CODEX_CONTEXT") or "").strip().lower() != "transcript":
                    os.environ["MAGI_CODEX_CONTEXT"] = "transcript"
                from skills.bridge.inference_gateway import InferenceGateway

                gateway = InferenceGateway()
                gw_result = gateway.vision(
                    image_path=filepath,
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
                    parsed = json_lib.loads(gw_text)
                    if isinstance(parsed, dict):
                        self.log(
                            f"  👁️ [Vision] InferenceGateway 解析成功 route={gw_result.get('route', '')} "
                            f"model={gw_result.get('model', '')}"
                        )
                        return parsed
            except Exception as gateway_err:
                self.log(f"  ⚠️ [Vision] InferenceGateway 異常: {gateway_err}")
            model = os.environ.get("MAGI_VISION_MODEL", os.environ.get("MAGI_OMLX_VISION_MODEL", "")) or "GLM-OCR-bf16"
            timeout = int(os.environ.get("MAGI_PDF_NAMER_STAMP_VISION_TIMEOUT", 30))
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                        ]
                    }
                ],
                "temperature": 0.0,
                "response_format": {"type": "json_object"}
            }
            vision_base = os.environ.get("MAGI_OMLX_VISION_URL", "http://127.0.0.1:8082")
            resp = requests.post(f"{vision_base}/v1/chat/completions", json=payload, headers={"Authorization": "Bearer magi-local"}, timeout=timeout)
            resp.raise_for_status()
            
            j = resp.json()
            response_text = j["choices"][0]["message"]["content"]
            parsed = json_lib.loads(response_text)
            self.log(f"  👁️ [Vision] 成功從圖片解析: {parsed}")
            return parsed
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
            self.log(f"  💾 [CASPER] 使用快取結果 (命中)")
            return cached
        
        # 調用 CASPER (保留函式名以相容舊流程)
        result = self._parse_with_gemini(text)
        
        # ★ 儲存到快取
        if result and hasattr(self, 'gemini_cache'):
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

    
    def move_to_case_folder(self, case: CourtCase, file_paths: List[str] = None):

        if not file_paths:
            self.log("⚠️ 未提供檔案路徑，無法歸檔")
            return

        # 載入 MD5 記錄
        downloaded_md5s = self._load_md5_records()
        
        # 尋找案件資料夾
        transcript_folder = None
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
            return

        # ★★★ 核心改進：掃描案件資料夾內現有檔案的 MD5 ★★★
        existing_folder_md5s = {}
        existing_folder_files = {}  # MD5 -> filename mapping
        if os.path.exists(transcript_folder):
            for fname in os.listdir(transcript_folder):
                if not fname.lower().endswith('.pdf'):
                    continue
                fpath = os.path.join(transcript_folder, fname)
                try:
                    file_md5 = self._calculate_file_md5(fpath)
                    if file_md5:
                        existing_folder_md5s[file_md5] = fpath
                        existing_folder_files[file_md5] = fname
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2624, exc_info=True)
        
        if existing_folder_md5s:
            self.log(f"  📂 案件資料夾已有 {len(existing_folder_md5s)} 個筆錄 (已計算 MD5)")

        for filepath in file_paths:
            if not os.path.exists(filepath):
                continue
                
            try:
                # ★★★ MD5 檢查：同時比對 JSON 記錄 + 資料夾現有檔案 ★★★
                md5 = self._calculate_file_md5(filepath)
                
                # 1. 強制覆蓋重複檢查：即使 MD5 相同也繼續處理 -> 改為：若內容相同則跳過不存
                if md5 and md5 in existing_folder_md5s:
                    existing_file = existing_folder_files.get(md5, "")
                    self.log(f"  ℹ️ 資料夾內已存在相同檔案 ({existing_file})，跳過移入")
                    
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
                            'size': self._get_file_size_safe(os.path.join(transcript_folder, existing_file))
                        }
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
                new_filename = self._generate_record_filename(parse_result, original_filename)
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
                
                filename = new_filename
                
                # 更新 MD5 記錄
                if md5:
                    downloaded_md5s[md5] = {
                        'filename': filename,
                        'case_number': case.case_number,
                        'court_case_number': case.court_case_number,
                        'downloaded_at': datetime.now().isoformat(),
                        'size': os.path.getsize(final_dest) if os.path.exists(final_dest) else 0
                    }
                    
            except Exception as e:
                self.log(f"  ❌ 歸檔失敗 ({os.path.basename(filepath)}): {e}")
        
        # 保存 MD5 記錄
        self._save_md5_records(downloaded_md5s)
    
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
             if f.lower().endswith(".pdf") and not f.startswith(".")),
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
                
                local_path = translate_case_path_to_local(case.folder_path)
                transcript_folder = self.find_transcript_folder(local_path)
                
                if not transcript_folder or not os.path.exists(transcript_folder):
                    continue
                
                pdf_files = [f for f in os.listdir(transcript_folder) if f.lower().endswith('.pdf')]
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
                            if re.match(r'^\d{8}\s.+?\(.+\)\.pdf$', fname):
                                # self.log(f"    ⏭️ 檔名已標準化，略過解析: {fname}")
                                continue

                            parse_result = self._parse_record_pdf(full_path)
                            if parse_result.get('date') and parse_result.get('type'):
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
        
        # 使用全域鎖定
        with _global_transcript_lock:
            if _global_transcript_operation_in_progress:
                self.log("⚠️ [更名] 另一個操作正在進行中，跳過")
                return
            _global_transcript_operation_in_progress = True
        
        try:
            self.log("✏️ [更名] 開始統一更名所有筆錄...")
            
            cases = self.get_cases_from_db()
            total_cases = len(cases)
            total_renamed = 0

            # 防呆：避免更名階段因個別 PDF 解析/工具端點異常而拖太久
            try:
                max_runtime_sec = int(os.environ.get("MAGI_EZLAWYER_RENAME_MAX_RUNTIME_SEC", "900") or "900")
            except Exception:
                max_runtime_sec = 900
            started = time.monotonic()
            
            for case_idx, case in enumerate(cases, 1):
                if max_runtime_sec > 0 and (time.monotonic() - started) > max_runtime_sec:
                    self.log(f"⏱️ [更名] 已超過最大執行時間 {max_runtime_sec}s，停止後續案件（已處理 {case_idx-1}/{total_cases}）。")
                    break
                if not case.folder_path:
                    continue
                
                local_path = translate_case_path_to_local(case.folder_path)
                transcript_folder = self.find_transcript_folder(local_path)
                
                if not transcript_folder or not os.path.exists(transcript_folder):
                    continue
                
                pdf_files = [f for f in os.listdir(transcript_folder) if f.lower().endswith('.pdf')]
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
                            break

                        # 解析 PDF
                        parse_result = self._parse_record_pdf(full_path)
                        if not parse_result.get('date') or not parse_result.get('type'):
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
                            
                    except Exception as e:
                        self.log(f"    ⚠️ 更名失敗 ({fname}): {e}")
            
            self.log(f"✅ [更名] 完成！共更名 {total_renamed} 個檔案")
            
        except Exception as e:
            self.log(f"❌ [更名] 失敗: {e}")
            import traceback
            traceback.print_exc()
        finally:
            with _global_transcript_lock:
                _global_transcript_operation_in_progress = False
    
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
            print("⚠️ 無法載入 file_review_automation 模組，請確保該檔案存在。")
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
        
        raw_download_folder = judicial_config.get('record_download_folder', './筆錄下載')
        
        # (MacFix) 強制修正 Windows 路徑
        if sys.platform == 'darwin' and (raw_download_folder.lower().startswith('k:') or '\\' in raw_download_folder):
             raw_download_folder = './筆錄下載'
             print(f"⚠️ [Mac修正] 偵測到 Windows 路徑，已強制重置為: {raw_download_folder}")
             
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
                print(f"⚠️ Processed log corrupted ({log_path}): {e}", file=sys.stderr)
            except Exception as e:
                print(f"⚠️ Failed to load processed log ({log_path}): {e}", file=sys.stderr)
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
            print(f"⚠️ Failed to save processed log ({log_path}): {e}", file=sys.stderr)
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
                        if not fname.lower().endswith('.pdf'):
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
                pass

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
            new_filename = self._generate_record_filename(parse_result, original_filename)
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
                    if 2020 <= year <= 2200 and 1 <= month <= 12 and 1 <= day <= 31:
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
                        if 2020 <= year <= 2200:
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
                if gemini_result:
                    if not result['date'] and gemini_result.get('date'):
                        result['date'] = gemini_result['date']
                        self.log(f"  📅 [CASPER] 解析日期: {result['date']}")
                    if not result['type'] and gemini_result.get('type'):
                        result['type'] = gemini_result['type']
                        self.log(f"  📝 [CASPER] 解析類型: {result['type']}")
                    # ★ 重要：如果 period 只有時段沒有時間，用 CASPER 的完整時間覆蓋
                    gemini_period = gemini_result.get('period', '')
                    if gemini_period and len(gemini_period) > 2:  # 完整格式如「上午0930」長度 > 2
                        result['period'] = gemini_period
                        self.log(f"  🕐 [CASPER] 解析時間: {gemini_period}")
            
        except ImportError:
            self.log("  ⚠️ 需要安裝 PyMuPDF (fitz) 才能解析 PDF")
        except Exception as e:
            self.log(f"  ⚠️ 解析 PDF 失敗: {e}")
        
        return result
    
    def _parse_with_gemini(self, text: str) -> Dict[str, Optional[str]]:
        """使用 CASPER 解析筆錄日期與類型（保留函式名以相容舊流程）"""
        import json as json_lib
        try:
            from casper_tools_client import casper_chat
        except Exception as e:
            self.log(f"  ⚠️ [CASPER] 無法載入 casper_tools_client: {e}")
            return None

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

        r = casper_chat(prompt, timeout_sec=90)
        if not isinstance(r, dict) or not r.get("success"):
            self.log(f"  ⚠️ [CASPER] 呼叫失敗: {(r.get('error') if isinstance(r, dict) else '')}")
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
            self.log(f"  💾 [CASPER] 使用快取結果 (命中)")
            return self.gemini_cache[cache_key]
        
        # 調用 CASPER (保留函式名以相容舊流程)
        result = self._parse_with_gemini(text)
        
        # ★ 儲存到快取
        if result and hasattr(self, 'gemini_cache'):
            self.gemini_cache[cache_key] = result
            self._save_gemini_cache()
            self.log(f"  💾 [CASPER] 已快取解析結果")
        
        return result
    
    def _generate_record_filename(self, parse_result: Dict[str, Optional[str]], original_filename: str) -> str:
        """根據解析結果生成檔名"""
        date_str = parse_result.get('date')
        record_type = parse_result.get('type', '筆錄')
        period = parse_result.get('period', '')
        
        # DEBUG: Check for double time bug
        self.log(f"    [FilenameGen] Date={date_str}, Type={record_type}, Period={period}")
        
        if not date_str:
            # ★★★ BUG FIX: 不再使用下載日期！改用辨識標記 ★★★
            date_str = '00000000'
            self.log(f"    ⚠️ 【日期解析失敗】無法從 PDF 提取作成日，標記 00000000")
        
        # ★ 防呆修正：處理重複的時間字串 (如 上午09300930)
        if period and len(period) >= 10:
            import re
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
        print(full_msg)
        if self.log_callback:
            self.log_callback(full_msg)
    
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
            
            transcript_folder = self.find_transcript_folder(local_path) if local_path else None
            
            if not transcript_folder or not os.path.exists(transcript_folder):
                if case_idx <= 3:
                    self.log(f"    筆錄資料夾: 未找到")
                continue
            
            folders_found += 1
            pdf_files = [f for f in os.listdir(transcript_folder) if f.lower().endswith('.pdf')]
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
            pdf_files = [f for f in os.listdir(transcript_folder) if f.lower().endswith('.pdf')]
            
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
                    if not parse_result.get('date') or not parse_result.get('type'):
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
                transcript_folder = self.find_transcript_folder(local_path) if local_path else None
                
                if not transcript_folder or not os.path.exists(transcript_folder):
                    continue
                
                pdf_files = [f for f in os.listdir(transcript_folder) if f.lower().endswith('.pdf')]
                if not pdf_files:
                    continue
                
                # 只在有檔案需要處理時顯示案件資訊
                case_has_rename = False
                
                for fname in pdf_files:
                    full_path = os.path.join(transcript_folder, fname)
                    try:
                        # 解析 PDF
                        parse_result = temp_downloader._parse_record_pdf(full_path)
                        if not parse_result.get('date') or not parse_result.get('type'):
                            continue
                        
                        # ★ 檢查是否已經被改名過（非原始下載格式）
                        # 使用相同的檢查邏輯（透過 CourtRecordDownloader 的方法）
                        if hasattr(temp_downloader, '_is_original_download_filename') and not temp_downloader._is_original_download_filename(fname):
                            # 檔案已被改名過，跳過
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
                transcript_folder = self.find_transcript_folder(local_path)
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
                                if f.lower().endswith('.pdf'):
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

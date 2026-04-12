# -*- coding: utf-8 -*-
"""
閱卷自動化模組 (File Review Automation)
獨立處理律師單一登入及閱卷系統操作

Author: Claude (Anthropic)
Date: 2025-12
"""

import os
import re
import sys
import io
import time
import json
import logging
import random
import hashlib
import shutil
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field
import pickle
import base64
import email
import html
from email.mime.text import MIMEText
import time

import importlib.util
import urllib.parse
import tempfile

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import ensure_path_on_sys_path, get_config_path, get_json_dir, get_orch_dir
from api.case_path_mapper import preferred_case_roots, translate_case_path_to_local
from skills.engine.legal_web_adapter import format_legal_web_engine_log, resolve_legal_web_engine

logger = logging.getLogger("FileReviewAutomation")

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

# =============================================================================
# Human-in-the-loop CAPTCHA (no auto bypass)
# =============================================================================

def _is_production_host(url: str) -> bool:
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        return host not in {"127.0.0.1", "localhost", ""}
    except Exception:
        return True

# =============================================================================
# 依賴項 (Lazy Load Setup)
# =============================================================================

# Check availability without importing
SELENIUM_AVAILABLE = importlib.util.find_spec("selenium") is not None
RAPIDOCR_AVAILABLE = importlib.util.find_spec("rapidocr_onnxruntime") is not None
DDDDOCR_AVAILABLE = False
try:
    if importlib.util.find_spec("ddddocr") is not None:
        import ddddocr as _ddddocr_probe
        DDDDOCR_AVAILABLE = hasattr(_ddddocr_probe, "DdddOcr")
except Exception:
    DDDDOCR_AVAILABLE = False
google_api_available = importlib.util.find_spec("googleapiclient") is not None and \
                       importlib.util.find_spec("google_auth_oauthlib") is not None and \
                       importlib.util.find_spec("google.auth") is not None
GMAIL_AVAILABLE = google_api_available

# Placeholder for lazy imports
webdriver = None
Options = None
By = None
WebDriverWait = None
EC = None
Keys = None
ActionChains = None
TimeoutException = None
NoSuchElementException = None
StaleElementReferenceException = None

RapidOCR = None
ddddocr = None

build = None
InstalledAppFlow = None
Request = None



# =============================================================================
# 驗證碼識別器
# =============================================================================

class CaptchaSolver:
    """驗證碼識別器 (優先使用 ddddocr)"""
    
    def __init__(self):
        self.ocr = None
        self.dddd_ocr = None
        
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
                    self.dddd_ocr = ddddocr.DdddOcr(show_ad=False)
                    logging.getLogger("file_review").info("ddddocr 初始化成功")
                except Exception as e:
                    logging.getLogger("file_review").warning("ddddocr 初始化失敗: %s", e)

        # Lazy Load RapidOCR (與 ddddocr 並行，作為雙引擎互補)
        if RAPIDOCR_AVAILABLE:
            global RapidOCR
            if RapidOCR is None:
                try:
                    from rapidocr_onnxruntime import RapidOCR
                except ImportError:
                    pass

            if RapidOCR:
                try:
                    self.ocr = RapidOCR()
                    logging.getLogger("file_review").info("RapidOCR (備用) 初始化成功")
                except Exception as e:
                    logging.getLogger("file_review").warning("RapidOCR 初始化失敗: %s", e)
    
    def solve_from_element(self, driver, img_element) -> str:
        """從 Selenium 元素識別驗證碼"""
        try:
            # 截圖驗證碼圖片
            img_data = img_element.screenshot_as_png
            return self.solve_png_bytes(img_data)
        except Exception as e:
            print(f"驗證碼識別失敗: {e}")
            return ""

    def solve_png_base64(self, png_base64: str) -> str:
        """從 base64(可含 data URI) 識別驗證碼，回傳純數字字串。"""
        try:
            s = (png_base64 or "").strip()
            if not s:
                return ""
            # Accept: data:image/png;base64,....
            if "," in s and s.lower().startswith("data:"):
                s = s.split(",", 1)[1].strip()
            img_data = base64.b64decode(s)
            return self.solve_png_bytes(img_data)
        except Exception:
            return ""

    def solve_png_bytes(self, img_data: bytes) -> str:
        """從 PNG bytes 識別驗證碼，回傳純數字字串。"""
        try:
            if not img_data:
                return ""
            
            candidates = []

            # 優先使用 ddddocr
            if self.dddd_ocr:
                result = self.dddd_ocr.classification(img_data)
                digits = re.sub(r'[^0-9]', '', result)
                if digits:
                    candidates.append(digits)
            
            # 備用：RapidOCR
            if self.ocr:
                from PIL import Image
                import numpy as np
                img = Image.open(io.BytesIO(img_data))
                img_array = np.array(img)
                
                ocr_result, _ = self.ocr(img_array)
                
                if ocr_result:
                    text = ''.join([item[1] for item in ocr_result])
                    digits = re.sub(r'[^0-9]', '', text)
                    if digits:
                        candidates.append(digits)

            if candidates:
                # 優先回傳位數最完整者（閱卷驗證碼通常為 6 位）
                candidates.sort(key=lambda s: len(s), reverse=True)
                return candidates[0]
            
            return ""
            
        except Exception as e:
            print(f"驗證碼識別失敗: {e}")
            return ""


# =============================================================================
# 律師單一登入 (專用於閱卷系統)
# =============================================================================

class LawyerPortalSSO:
    """
    律師單一登入系統 (portal.ezlawyer.com.tw)
    專門用於閱卷系統登入，與筆錄調閱分開處理
    """
    
    LOGIN_URL = "https://portal.ezlawyer.com.tw/Login.do?gotoLogin=Y"

    def __init__(self,
                 username: str = "",
                 password: str = "",
                 download_folder: str = "./閱卷下載",
                 headless: bool = True,
                 log_callback=None):

        self.username = username
        self.password = password
        self.download_folder = os.path.abspath(download_folder or "./閱卷下載")
        self.headless = headless
        self.log_callback = log_callback

        # Mock 模式：設定 MAGI_EEFILE_MOCK_URL 時自動切換
        mock_url = os.environ.get("MAGI_EEFILE_MOCK_URL", "").strip().rstrip("/")
        self.mock_mode = bool(mock_url)
        if self.mock_mode:
            self.LOGIN_URL = f"{mock_url}/Login.do?gotoLogin=Y"
            # 安全護欄：Mock 模式禁止寫入正式閱卷下載資料夾
            _prod_markers = ("閱卷下載", "MAGI/閱卷")
            if any(m in self.download_folder for m in _prod_markers):
                raise RuntimeError(
                    f"Mock 模式禁止使用正式下載資料夾: {self.download_folder}\n"
                    f"請設定 MAGI_EEFILE_DOWNLOAD_FOLDER 環境變數指向臨時資料夾。"
                )

        self.driver = None
        self.logged_in = False
        self.web_engine_profile = resolve_legal_web_engine("file_review_portal", interactive_required=True)
        self._engine_logged = False
        self.captcha_solver = CaptchaSolver()
        # 預設啟用自動 OCR（含正式站）；必要時可用環境變數關閉。
        self._allow_captcha_ocr = os.environ.get("MAGI_ALLOW_CAPTCHA_OCR", "1").strip().lower() in {
            "1", "true", "yes", "on"
        }
        # 預設不走人工互動，避免夜間流程卡住；需要時可手動打開。
        self._allow_human_captcha_fallback = os.environ.get("MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK", "0").strip().lower() in {
            "1", "true", "yes", "on"
        }
        self._captcha_double_check = os.environ.get("MAGI_CAPTCHA_DOUBLE_CHECK", "1").strip().lower() in {
            "1", "true", "yes", "on"
        }
    
    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{timestamp}] [閱卷SSO] {message}"
        print(full_msg)
        if self.log_callback:
            self.log_callback(full_msg)
    
    def _setup_driver(self):
        """設定 Chrome WebDriver (含反爬蟲措施)"""
        if not self._engine_logged:
            self.log(format_legal_web_engine_log(self.web_engine_profile))
            self._engine_logged = True
        if not SELENIUM_AVAILABLE:
            raise ImportError("Selenium 未安裝")
        
        # Lazy Load Selenium
        global webdriver, Options, By, WebDriverWait, EC, ActionChains
        global TimeoutException, NoSuchElementException, Keys
        
        if webdriver is None:
            from selenium import webdriver
            from selenium.webdriver.common.by import By
            from selenium.webdriver.common.keys import Keys
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.common.action_chains import ActionChains
            from selenium.common.exceptions import TimeoutException, NoSuchElementException
            
        options = Options()
        
        # Headless 模式設定
        if self.headless:
            options.add_argument('--headless=new')
            # Headless 模式下重要設定，防止崩潰
            options.add_argument('--window-size=1920,1080')
            options.add_argument('--disable-gpu')
            options.add_argument('--no-sandbox')
            options.add_argument('--disable-dev-shm-usage')
        else:
            options.add_argument('--window-size=1280,800')
        
        # 反爬蟲：使用真實 User-Agent
        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
        ]
        options.add_argument(f'--user-agent={random.choice(user_agents)}')
        
        # 反爬蟲：禁用自動化標誌
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option("excludeSwitches", ["enable-automation"])
        options.add_experimental_option('useAutomationExtension', False)
        
        # Chrome 146+ renderer timeout 修正：不等外部資源完全載入
        options.page_load_strategy = 'eager'

        # 穩定性設定 — 防止 renderer timeout ("Timed out receiving message from renderer")
        options.add_argument('--disable-infobars')
        options.add_argument('--disable-notifications')
        options.add_argument('--disable-popup-blocking')
        options.add_argument('--remote-allow-origins=*')
        options.add_argument('--disable-extensions')
        options.add_argument('--disable-renderer-backgrounding')
        options.add_argument('--disable-backgrounding-occluded-windows')
        options.add_argument('--disable-hang-monitor')
        options.add_argument('--disable-ipc-flooding-protection')
        options.add_argument('--memory-pressure-off')
        options.add_argument('--js-flags=--max-old-space-size=512')
        
        # 下載設定
        date_str = datetime.now().strftime("%Y%m%d")
        download_dir = os.path.join(self.download_folder, date_str)
        os.makedirs(download_dir, exist_ok=True)
        
        prefs = {
            "download.default_directory": download_dir,
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "plugins.always_open_pdf_externally": True,
            "profile.default_content_settings.popups": 0
        }
        options.add_experimental_option("prefs", prefs)
        
        # 初始化 WebDriver
        try:
            self.driver = webdriver.Chrome(options=options)
        except Exception as e:
            self.log(f"⚠️ Chrome Driver 初始化失敗，嘗試相容模式: {e}")
            # 備用方案: 可能是 driver 版本問題
            options.add_argument('--ignore-certificate-errors')
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
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 370, exc_info=True)
        
        # ★ Headless 模式下強制設定下載行為 (重要修復)
        if self.headless:
            try:
                # 確保下載路徑是絕對路徑 (使用與 prefs 相同的路徑)
                params = {
                    "behavior": "allow", 
                    "downloadPath": download_dir
                }
                self.driver.execute_cdp_cmd("Page.setDownloadBehavior", params)
                self.log(f"  已設定 Headless 下載路徑: {download_dir}")
            except Exception as e:
                self.log(f"  ⚠️ 設定 Headless 下載行為失敗: {e}")

        # 隱藏 webdriver 特徵
        try:
            self.driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
                'source': '''
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined})
                '''
            })
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 393, exc_info=True)
        
        self.driver.implicitly_wait(10)
        mode = "Headless" if self.headless else "GUI"
        self.log(f"WebDriver 設置完成 ({mode} 模式)")
    
    def login(self, max_retries: int = 3) -> bool:
        """
        登入律師單一登入系統
        
        流程：
        1. 開啟登入頁面
        2. 輸入帳號密碼
        3. 識別並輸入驗證碼 (6位數字)
        4. 點擊登入
        """
        for attempt in range(max_retries):
            try:
                if not self.driver:
                    self._setup_driver()
                
                self.log(f"正在登入 (第 {attempt + 1} 次嘗試)...")
                try:
                    self.driver.get(self.LOGIN_URL)
                except Exception as get_e:
                    self.log(f"⚠️ 無法前往登入頁面: {get_e}")
                    # 遇到網路錯誤，重啟 Driver
                    if "ERR_" in str(get_e) or "disconnected" in str(get_e) or "renderer" in str(get_e).lower():
                        self.log("  🔄 偵測到嚴重網路/驅動/renderer 錯誤，重啟 Driver...")
                        try:
                            self.driver.quit()
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 425, exc_info=True)
                        self.driver = None
                        time.sleep(3)
                        continue
                    raise get_e
                
                # 隨機延遲模擬人類
                time.sleep(random.uniform(1.5, 3))
                
                # 等待頁面載入
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input.form-control"))
                )
                
                # 找到所有 form-control 輸入框
                form_inputs = self.driver.find_elements(By.CSS_SELECTOR, "input.form-control")
                self.log(f"  找到 {len(form_inputs)} 個輸入框")

                # Mock 模式：只有帳號+密碼（2 個），無驗證碼
                if self.mock_mode:
                    if len(form_inputs) < 2:
                        self.log("  ⚠️ Mock 模式找不到足夠的輸入框")
                        continue
                    username_field = form_inputs[0]
                    password_field = form_inputs[1]
                    captcha_field = password_field
                    username_field.clear()
                    username_field.send_keys(self.username)
                    password_field.clear()
                    password_field.send_keys(self.password)
                    self.log(f"  [Mock] 輸入帳號密碼，跳過驗證碼")
                else:
                    if len(form_inputs) < 3:
                        self.log("  ⚠️ 找不到足夠的輸入框")
                        continue

                    username_field = form_inputs[0]  # 第一個 = 帳號
                    password_field = form_inputs[1]  # 第二個 = 密碼
                    captcha_field = form_inputs[2]   # 第三個 = 驗證碼

                    # 清空並輸入帳號 (模擬人類打字)
                    self.log(f"  輸入帳號: {self.username}")
                    username_field.clear()
                    for char in self.username:
                        username_field.send_keys(char)
                        time.sleep(random.uniform(0.05, 0.15))

                    time.sleep(random.uniform(0.3, 0.7))

                    # 清空並輸入密碼
                    self.log(f"  輸入密碼: {'*' * len(self.password)}")
                    password_field.clear()
                    for char in self.password:
                        password_field.send_keys(char)
                        time.sleep(random.uniform(0.05, 0.15))

                    time.sleep(random.uniform(0.3, 0.7))

                    # 處理驗證碼：先自動 OCR；必要時才人工回覆
                    captcha_text = ""
                    if self._allow_captcha_ocr:
                        captcha_text = self._solve_captcha_with_retry()
                    if (not captcha_text) and self._allow_human_captcha_fallback:
                        self.log("  ⚠️ 自動 OCR 失敗，改走人工驗證碼流程")
                        captcha_text = self._request_human_captcha_code(expected_len=6)

                    if captcha_text and len(captcha_text) >= 6:
                        captcha_text = captcha_text[:6]  # 只取前6位
                        self.log("  已自動填入驗證碼")
                        captcha_field.clear()
                        for char in captcha_text:
                            captcha_field.send_keys(char)
                            time.sleep(random.uniform(0.05, 0.15))
                    else:
                        self.log("  ❌ 驗證碼識別失敗，將進入下一輪登入重試")
                        continue
                
                time.sleep(random.uniform(0.3, 1.0))
                
                # 點擊登入按鈕
                login_btn = None
                for selector in [
                    "button[title='會員登入']",
                    "button.btn-primary",
                ]:
                    try:
                        login_btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                        if login_btn and login_btn.is_displayed():
                            break
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 515, exc_info=True)
                
                if login_btn:
                    self.log("  點擊登入按鈕")
                    time.sleep(random.uniform(0.3, 0.8))
                    login_btn.click()
                else:
                    self.log("  ⚠️ 找不到登入按鈕，嘗試 Enter 提交")
                    captcha_field.send_keys(Keys.RETURN)
                
                # 等待登入結果
                time.sleep(random.uniform(1.0, 2.0))
                
                # 檢查是否有 Alert (如驗證碼錯誤)
                try:
                    alert = self.driver.switch_to.alert
                    alert_text = alert.text
                    self.log(f"  ⚠️ 發現 Alert: {alert_text}")
                    alert.accept()
                    
                    if "驗證碼" in alert_text:
                        self.log("  (Alert 指示驗證碼錯誤，重試)")
                        continue
                    if "帳號" in alert_text or "密碼" in alert_text:
                        self.log("  (Alert 指示帳號密碼錯誤)")
                        return False
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 542, exc_info=True)
                
                # 檢查登入結果
                if self._check_login_success():
                    self.logged_in = True
                    self.log("✅ 登入成功")
                    return True
                else:
                    error_msg = self._get_error_message()
                    self.log(f"❌ 登入失敗: {error_msg}")
                    
                    # 驗證碼錯誤，重試
                    if "驗證碼" in error_msg:
                        continue
                    
            except Exception as e:
                self.log(f"⚠️ 登入異常: {e}")
                import traceback
                traceback.print_exc()
                # Chrome session 死亡時，重建 driver 讓下一輪重試有效
                if "invalid session" in str(e).lower() or "disconnected" in str(e).lower():
                    self.log("  🔄 偵測到 session 死亡，重建 Driver...")
                    try:
                        self.driver.quit()
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 567, exc_info=True)
                    self.driver = None
                    time.sleep(2)

        return False

    def _request_human_captcha_code(self, expected_len: int = 6) -> str:
        """
        Human-in-the-loop:
        - save captcha image to local download dir
        - export to MAGI static exports
        - notify admin via LINE to reply digits
        - in headless: wait for reply file
        """
        try:
            captcha_img = None
            for selector in ["#captcha", "img#captcha", "img[src*='captcha']"]:
                try:
                    captcha_img = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if captcha_img:
                        break
                except Exception:
                    continue
            if not captcha_img:
                self.log("  ⚠️ 找不到驗證碼圖片")
                return ""

            # Save captcha snapshot
            try:
                out_dir = Path(os.path.join(self.download_folder, datetime.now().strftime('%Y%m%d')))
                out_dir.mkdir(parents=True, exist_ok=True)
            except Exception:
                out_dir = Path(os.path.abspath("."))
            img_path = out_dir / "debug_portal_captcha.png"
            try:
                png = captcha_img.screenshot_as_png
                with open(img_path, "wb") as f:
                    f.write(png)
                self.log(f"  📷 驗證碼圖片已保存: {img_path}")
            except Exception as e:
                self.log(f"  ⚠️ 無法保存驗證碼圖片: {e}")
                return ""

            from magi_human_captcha import request_human_captcha
            code = request_human_captcha(
                kind="portal_sso",
                image_path=img_path,
                expected_len=int(expected_len or 0),
                ttl_seconds=int(os.environ.get("MAGI_CAPTCHA_TTL_SECONDS", "300") or "300"),
                wait_seconds=int(os.environ.get("MAGI_CAPTCHA_WAIT_SECONDS", "180") or "180"),
                headless=bool(self.headless),
                notify=True,
                log=self.log,
            )
            if code:
                self.log("  ✓ 已收到人工驗證碼回覆（不顯示）")
            return code
        except Exception as e:
            self.log(f"  ⚠️ 人工驗證碼流程失敗: {e}")
            return ""

    def _solve_captcha_with_melchior(self, captcha_img, expected_len: int = 6) -> str:
        """
        視覺備援：本地 OCR 不足時，改用 InferenceGateway 讀取驗證碼。
        """
        if os.environ.get("MAGI_CAPTCHA_USE_MELCHIOR", "1").strip().lower() not in {"1", "true", "yes", "on"}:
            return ""
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

            prompt = f"Read this CAPTCHA image and output ONLY {expected_len} digits."
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
            digits = re.sub(r"[^0-9]", "", text or "")
            if len(digits) >= expected_len:
                self.log(
                    f"  🤖 InferenceGateway CAPTCHA route={r.get('route')} degraded={r.get('degraded')} confidence={r.get('confidence', 'n/a')}"
                )
                return digits[:expected_len]
            return ""
        except Exception as e:
            self.log(f"  ⚠️ Gateway 驗證碼備援失敗: {e}")
            return ""
        finally:
            try:
                if 'tmp_path' in locals() and tmp_path and os.path.exists(tmp_path):
                    if safe_remove:
                        safe_remove(tmp_path, reason="tmp_captcha_snapshot", allow_delete=True)
                    else:
                        pass
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 679, exc_info=True)
    
    def _solve_captcha_with_retry(self, max_retries: int = 8) -> str:
        """
        識別驗證碼，如果不清楚就重新產生
        
        Returns:
            識別出的驗證碼文字 (6位數字)
        """
        def _find_captcha_img():
            for selector in ["#captcha", "img#captcha", "img[src*='captcha']"]:
                try:
                    el = self.driver.find_element(By.CSS_SELECTOR, selector)
                    if el:
                        return el
                except Exception:
                    continue
            return None

        def _refresh():
            selectors = [
                "a[title*='重新產生']",
                "a[onclick*='captcha']",
                "a[onclick*='refresh']",
                "img[src*='refresh']",
            ]
            for selector in selectors:
                try:
                    btn = self.driver.find_element(By.CSS_SELECTOR, selector)
                    btn.click()
                    time.sleep(random.uniform(0.8, 1.4))
                    return True
                except Exception:
                    continue
            # 備援：直接點驗證碼圖片觸發刷新
            try:
                img = _find_captcha_img()
                if img:
                    img.click()
                    time.sleep(random.uniform(0.8, 1.4))
                    return True
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 721, exc_info=True)
            return False

        captcha_text = ""
        for retry in range(max_retries):
            captcha_img = _find_captcha_img()
            if not captcha_img:
                self.log("  ⚠️ 找不到驗證碼圖片")
                return ""

            local_text = re.sub(r"[^0-9]", "", self.captcha_solver.solve_from_element(self.driver, captcha_img) or "")[:6]
            self.log(f"  OCR 嘗試第 {retry + 1} 次，local_len={len(local_text)}")

            melchior_text = ""
            if len(local_text) < 6:
                # 本地 OCR 不足 6 碼才呼叫 Melchior 備援，避免 oMLX 超時導致 Chrome session 過期
                melchior_text = re.sub(r"[^0-9]", "", self._solve_captcha_with_melchior(captcha_img, expected_len=6) or "")[:6]
                if melchior_text:
                    self.log(f"  OCR 嘗試第 {retry + 1} 次，melchior_len={len(melchior_text)}")

            # 雙引擎都拿到 6 碼時，必須一致才採用，降低誤判率
            if len(local_text) >= 6 and len(melchior_text) >= 6:
                if local_text == melchior_text:
                    self.log("  ✅ 雙引擎驗證一致")
                    return local_text
                self.log("  ⚠️ 雙引擎結果不一致，刷新驗證碼重試")
            elif len(local_text) >= 6:
                return local_text
            elif len(melchior_text) >= 6:
                self.log("  ✅ Melchior 備援辨識成功")
                return melchior_text

            if retry < max_retries - 1:
                ok = _refresh()
                if not ok:
                    time.sleep(random.uniform(1.0, 1.8))
        return ""
    
    def _check_login_success(self) -> bool:
        """檢查是否登入成功 (支援 Frames)"""
        try:
            # 等待載入
            time.sleep(3)
            
            # 定義成功特徵 (登入成功後頁面會變成 frameset 包含 mainFrame)
            success_indicators = [
                "律師您好", 
                "會員服務", 
                "登出",
                "聲請閱卷",
                "mainFrame",     # 登入成功後會有 frameset
                "SL1A.do",       # mainFrame 的 src
            ]
            
            # Helper: 檢查當前 Frame 是否有特徵
            def check_current_frame():
                src = self.driver.page_source
                for ind in success_indicators:
                    if ind in src:
                        self.log(f"  ✓ 找到特徵: {ind}")
                        return True
                return False

            # 1. 檢查主頁面
            self.driver.switch_to.default_content()
            if check_current_frame():
                return True
            
            # 2. 遍歷所有 Frame/iFrame
            self.log("  掃描頁面 Frames...")
            frames = self.driver.find_elements(By.TAG_NAME, "frame")
            iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
            all_frames = frames + iframes
            
            for i, frame in enumerate(all_frames):
                try:
                    self.driver.switch_to.default_content()
                    self.driver.switch_to.frame(frame)
                    if check_current_frame():
                        self.log(f"  ✓ 在 Frame[{i}] 中找到特徵")
                        self.driver.switch_to.default_content() # 切回主頁面
                        return True
                except Exception:
                    continue
            
            self.driver.switch_to.default_content()
            self.log("  ⚠️ 在主頁面及 Frames 中皆未發現登入特徵")
            return False
            
        except Exception as e:
            self.log(f"  檢查登入失敗: {e}")
            return False
    
    def _get_error_message(self) -> str:
        """取得錯誤訊息"""
        try:
            page_source = self.driver.page_source
            
            if "驗證碼錯誤" in page_source:
                return "驗證碼錯誤"
            if "帳號或密碼錯誤" in page_source:
                return "帳號或密碼錯誤"
            if "登入失敗" in page_source:
                return "登入失敗"
            
            return "未知錯誤"
            
        except Exception:
            return "無法取得錯誤訊息"
    
    def close(self):
        """關閉瀏覽器"""
        if self.driver:
            try:
                self.driver.quit()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 837, exc_info=True)
            self.driver = None
            self.logged_in = False


# =============================================================================
# 閱卷系統管理器
# =============================================================================

@dataclass
class FileReviewInfo:
    """閱卷通知資訊"""
    court: str = ""
    laf_case_no: str = ""
    application_no: str = ""
    court_case_no: str = ""
    client_name: str = ""
    status: str = ""
    payment_amount: int = 0
    payment_deadline: str = ""   # 繳費期限
    deadline: str = ""
    message_id: str = ""
    download_deadline: str = ""  # 下載期限
    files: List[str] = field(default_factory=list)

    @property
    def case_number(self) -> str:
        """相容舊欄位：優先回傳法院案號。"""
        return self.court_case_no or self.laf_case_no or self.application_no

    @case_number.setter
    def case_number(self, value: str):
        """相容舊欄位寫入：預設寫入法院案號。"""
        self.court_case_no = (value or "").strip()


@dataclass
class FileReviewCase:
    """閱卷案件資料"""
    court: str = ""           # 法院
    case_number: str = ""     # 案號
    case_type: str = ""       # 案類
    client_name: str = ""     # 當事人
    lawyer_id: str = "103台檢11712"  # 律師證號
    folder_path: str = ""     # 案件資料夾路徑
    hearing_date: Optional[str] = None  # 開庭日期
    status: str = ""          # 狀態


class FileReviewManager:
    """
    閱卷系統自動化管理器
    
    功能：
    1. 下載已申請的閱卷資料
    2. 申請新的閱卷
    3. 歸檔到案件資料夾
    4. 檢查待閱卷案件 (90天規則)
    5. 監控 Gmail 通知 (繳費/下載)
    """
    
    EEFILE_URL = "https://eefile.judicial.gov.tw/"
    SCOPES = ['https://www.googleapis.com/auth/gmail.modify']

    EMAIL_SUBJECTS = {
        "payment": "法 院 回 覆 閱 卷 聲 請 結 果 通 知 信",
        "ready": "法 院 已 完 成 線 上 交 付 核 閱 通 知 信"
    }

    def __init__(self,
                 username: str = "",
                 password: str = "",
                 gmail_credentials_path: str = "credentials.json",
                 # 閱卷通知信的 Gmail token 與法扶信箱 token 分開管理，避免互相覆蓋。
                 gmail_token_path: str = "filereview_token.pickle",
                 download_folder: str = "./閱卷下載",
                 db_manager=None,
                 discord_notifier=None,
                 headless: bool = True,
                 log_callback=None):

        self.username = username
        self.password = password
        self.credentials_path = gmail_credentials_path
        self.token_path = self._resolve_gmail_token_path(gmail_token_path, gmail_credentials_path)
        self.download_folder = os.path.abspath(download_folder)
        self.db = db_manager
        self.discord = discord_notifier
        self.headless = headless
        self.log_callback = log_callback

        # Mock 模式：設定 MAGI_EEFILE_MOCK_URL 時覆寫 EEFILE_URL
        mock_url = os.environ.get("MAGI_EEFILE_MOCK_URL", "").strip().rstrip("/")
        self.mock_mode = bool(mock_url)
        if self.mock_mode:
            self.EEFILE_URL = f"{mock_url}/"
            # 安全護欄：Mock 模式禁止寫入正式閱卷下載資料夾
            _prod_markers = ("閱卷下載", "MAGI/閱卷")
            if any(m in self.download_folder for m in _prod_markers):
                raise RuntimeError(
                    f"Mock 模式禁止使用正式下載資料夾: {self.download_folder}\n"
                    f"請設定 MAGI_EEFILE_DOWNLOAD_FOLDER 環境變數指向臨時資料夾。"
                )
        self.no_delete = (os.environ.get("MAGI_NO_DELETE", "1").strip().lower() in {"1", "true", "yes", "on"})
        
        self.sso = None
        self.driver = None
        self.gmail_service = None
        self._last_gmail_error = ""
        self._last_smart_skipped_files = []
        self._last_apply_for_review_uploads = {}

        # 案件資料夾 cache（DB 不可用時用來加速定位）
        self.case_folder_cache_file = os.path.join(self.download_folder, "case_folder_cache.json")
        self.case_folder_cache = self._load_case_folder_cache()
        self.case_folder_cache = self._sanitize_case_folder_cache(self.case_folder_cache)
        self.allow_risky_case_scan = (
            os.environ.get("MAGI_ALLOW_RISKY_CASE_SCAN", "0").strip().lower() in {"1", "true", "yes", "on"}
        )
        self.allow_filename_heuristic_archive = (
            os.environ.get("MAGI_ALLOW_FILENAME_HEURISTIC_ARCHIVE", "0").strip().lower() in {"1", "true", "yes", "on"}
        )
        self.allow_party_archive_map = (
            os.environ.get("MAGI_ALLOW_PARTY_ARCHIVE_MAP", "0").strip().lower() in {"1", "true", "yes", "on"}
        )
        # 預設開啟下載前去重，避免同案卷檔在既有 registry / 案件資料夾已存在時反覆重抓。
        self.enable_case_level_download_skip = (
            os.environ.get("MAGI_ENABLE_CASE_LEVEL_DOWNLOAD_SKIP", "1").strip().lower() in {"1", "true", "yes", "on"}
        )
        self.enable_preclick_smart_skip = (
            os.environ.get("MAGI_ENABLE_PRECLICK_SMART_SKIP", "1").strip().lower() in {"1", "true", "yes", "on"}
        )

        # 處理過的 Email 記錄 (持久化)
        self.processed_emails_file = os.path.join(self.download_folder, "processed_emails.json")
        self.processed_emails = self._load_processed_emails()

        # 已通知案件記錄 (持久化，避免重複發送 Discord)
        self.notified_cases_file = os.path.join(self.download_folder, "notified_cases.json")
        self.notified_cases = self._load_notified_cases()

        # 手動標記已繳費（永久跳過通知）
        self.dismissed_payments_file = os.path.join(self.download_folder, "dismissed_payments.json")
        self.dismissed_payments = self._load_dismissed_payments()

        self.ready_to_download = []  # 待下載清單

        # MD5 記錄
        self.md5_records_file = os.path.join(self.download_folder, "md5_records.json")
        self.md5_records = self._load_md5_records()

        # ★ 已下載檔案 registry（防止跨 run 重複下載）
        self.download_registry_file = os.path.join(self.download_folder, "downloaded_registry.json")
        self._download_registry = self._load_download_registry()

        # 已處理的「待繳費」項目（避免重複點擊/下載同一張繳費單）
        self.payment_registry_file = os.path.join(self.download_folder, "payment_registry.json")
        self.payment_registry = self._load_payment_registry()

        # 聲請紀錄（追蹤每案是否已首次聲請 → 決定是否需上傳收文章委任狀）
        self.apply_registry_file = os.path.join(self.download_folder, "apply_registry.json")
        self._apply_registry = self._load_apply_registry()

        # 手動歸檔記憶（人工指定後，下次可直接歸檔）
        self.manual_archive_map_file = os.path.join(self.download_folder, "manual_archive_mappings.json")
        self.manual_archive_requests_file = os.path.join(self.download_folder, "manual_archive_requests.json")
        self._manual_archive_map = self._load_manual_archive_map()
        self._manual_archive_requests = self._load_manual_archive_requests()

        self.logged_in = False

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

    def _resolve_gmail_token_path(self, token_path: str, credentials_path: str) -> str:
        """
        解析 Gmail token 路徑：
        1) 若已是絕對路徑，直接使用
        2) 若相對路徑在 cwd 存在，使用它
        3) 優先固定存放在 credentials.json 同資料夾（即使尚未存在，之後授權會寫入）

        設計理由：
        - 閱卷 Gmail token 必須與法扶 Gmail token 分開，避免誤用導致 scope 不足 (403 insufficientPermissions)。
        """
        if not token_path:
            token_path = "filereview_token.pickle"

        if os.path.isabs(token_path):
            return token_path

        if os.path.exists(token_path):
            return token_path

        cred_dir = os.path.dirname(os.path.abspath(credentials_path or ""))
        if cred_dir:
            # 即使檔案尚未存在，也固定回傳這個路徑，讓 reauth/授權流程寫入正確位置。
            return os.path.join(cred_dir, token_path)

        return token_path

    def _load_case_folder_cache(self) -> dict:
        try:
            if os.path.exists(self.case_folder_cache_file):
                with open(self.case_folder_cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f) or {}
                if isinstance(data, dict):
                    return data
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1044, exc_info=True)
        return {}

    def _save_case_folder_cache(self):
        try:
            with open(self.case_folder_cache_file, "w", encoding="utf-8") as f:
                json.dump(self.case_folder_cache or {}, f, ensure_ascii=False, indent=2)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1052, exc_info=True)

    def _sanitize_case_folder_cache(self, data: dict) -> dict:
        """
        只保留低風險 cache key，避免舊的 yyidno/party 污染導致誤歸檔。
        目前允許：
        - court_case_no:...
        - laf_case_no:...
        """
        if not isinstance(data, dict):
            return {}

        allowed_prefixes = ("court_case_no:", "laf_case_no:")
        cleaned = {}
        changed = False

        for k, v in data.items():
            if not isinstance(k, str) or not isinstance(v, str):
                changed = True
                continue
            if not k.startswith(allowed_prefixes):
                changed = True
                continue
            if not os.path.exists(v):
                changed = True
                continue
            cleaned[k] = v

        if changed:
            self.case_folder_cache = cleaned
            self._save_case_folder_cache()

        return cleaned

    @staticmethod
    def _looks_like_human_party_name(party: str) -> bool:
        """
        過濾高風險識別字（如 BS000-...），避免用當事人欄位做錯誤匹配。
        """
        p = (party or "").strip()
        if not p:
            return False
        if re.search(r"BS\d{3,}", p, re.IGNORECASE):
            return False
        if re.fullmatch(r"[A-Za-z0-9._\-]+", p):
            return False
        if re.search(r"\d{4,}", p):
            return False
        return True

    def _resolve_case_folder_from_db(self, yyidno: str = "", court_case_no: str = "", party: str = "") -> str:
        """
        使用 DB 做保守且精確的案件資料夾解析。
        只接受「年度/字別/號」完全一致的 court_case_number，避免 83 誤配 838。
        """
        if not self.db:
            return ""

        def _translate(path: str) -> str:
            p = (path or "").strip()
            if not p:
                return ""
            try:
                if hasattr(self.db, "translate_path_to_local"):
                    p = self.db.translate_path_to_local(p)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1118, exc_info=True)
            p = self._to_local_case_path(p)
            return p

        def _dedupe(seq: List[str]) -> List[str]:
            out: List[str] = []
            seen = set()
            for x in seq:
                if not x or x in seen:
                    continue
                seen.add(x)
                out.append(x)
            return out

        def _load_party_rows(party_name: str) -> List[dict]:
            """回傳 [{folder_path, court_case_number}, ...] 方便後續判斷案號是否空白。"""
            if not party_name or not self._looks_like_human_party_name(party_name):
                return []
            try:
                rows = self.db.execute(
                    "SELECT `folder_path`, `court_case_number` FROM `cases` "
                    "WHERE `client_name` = %s "
                    "AND `folder_path` IS NOT NULL AND `folder_path` != '' "
                    "LIMIT 8",
                    (party_name,),
                    fetch='all'
                ) or []
            except Exception:
                rows = []

            if isinstance(rows, dict):
                rows = [rows]

            out = []
            seen = set()
            for r in rows:
                p = _translate(str((r or {}).get("folder_path") or ""))
                if not p or p in seen:
                    continue
                seen.add(p)
                ccn = str((r or {}).get("court_case_number") or "").strip()
                if not os.path.exists(p):
                    continue
                out.append({"folder_path": p, "court_case_number": ccn})
            return out

        def _load_party_paths(party_name: str) -> List[str]:
            return [r["folder_path"] for r in _load_party_rows(party_name)]

        def _pick_party_fallback(party_name: str, case_text: str = "") -> str:
            party_rows = _load_party_rows(party_name)
            party_paths = [r["folder_path"] for r in party_rows]
            if len(party_paths) == 1:
                return party_paths[0]
            if len(party_paths) <= 1:
                return ""

            # 多筆結果：檢查是否有案號空白的 → 提醒補填
            missing_ccn = [r for r in party_rows if not r["court_case_number"]]
            if missing_ccn:
                folders = ", ".join(os.path.basename(r["folder_path"]) for r in missing_ccn)
                self.log(
                    f"  ⚠️ 當事人「{party_name}」有多筆案件，其中以下案件尚未填寫案號：{folders}\n"
                    f"     → 請先到 DB 補填 court_case_number 後重試，以便自動辨識正確資料夾。"
                )
                return ""

            norm_case = self._norm(case_text)
            basename_hits: List[str] = []
            if norm_case and ("上訴" in norm_case or "抗告" in norm_case):
                basename_hits = [
                    p for p in party_paths
                    if any(token in self._norm(os.path.basename(p)) for token in (self._norm("二審"), self._norm("上訴")))
                ]
            elif norm_case:
                basename_hits = [
                    p for p in party_paths
                    if self._norm("一審") in self._norm(os.path.basename(p))
                ]

            if len(basename_hits) == 1:
                return basename_hits[0]
            return ""

        parsed_targets: List[tuple[str, str, str]] = []
        y0 = ct0 = n0 = ""
        try:
            parts = [p.strip() for p in (yyidno or "").split(".") if p.strip()]
            if len(parts) >= 3:
                y0 = parts[0]
                ct0 = self._norm(parts[1])
                n0 = str(int(parts[2]))
        except Exception:
            y0 = ct0 = n0 = ""

        if y0 and ct0 and n0:
            parsed_targets.append((y0, ct0, n0))
        else:
            y1, ct1, n1 = self._parse_court_case_no(court_case_no or "")
            if y1 and ct1 and n1:
                try:
                    parsed_targets.append((str(y1), self._norm(ct1), str(int(n1))))
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1221, exc_info=True)

        candidates: List[str] = []

        for y, ct, num in parsed_targets:
            patterns = [
                f"%{y}%{ct}%{num}號%",
                f"%{y}%{ct}%{num}%",
            ]
            for pattern in patterns:
                try:
                    rows = self.db.execute(
                        "SELECT `court_case_number`, `folder_path` "
                        "FROM `cases` "
                        "WHERE `court_case_number` LIKE %s "
                        "AND `folder_path` IS NOT NULL AND `folder_path` != '' "
                        "LIMIT 40",
                        (pattern,),
                        fetch='all'
                    ) or []
                except Exception:
                    rows = []

                if isinstance(rows, dict):
                    rows = [rows]

                for row in rows:
                    raw_case = str((row or {}).get("court_case_number") or "").strip()
                    raw_path = str((row or {}).get("folder_path") or "").strip()
                    if not raw_case or not raw_path:
                        continue

                    y2, ct2, n2 = self._parse_court_case_no(raw_case)
                    if not (y2 and ct2 and n2):
                        continue

                    try:
                        n2s = str(int(n2))
                    except Exception:
                        continue

                    ct2n = self._norm(ct2)
                    if self._norm(y2) != self._norm(y):
                        continue
                    if n2s != num:
                        continue
                    if not (ct2n == ct or ct2n in ct or ct in ct2n):
                        continue

                    candidates.append(_translate(raw_path))

        uniq = _dedupe(candidates)
        if uniq:
            existing = [p for p in uniq if os.path.exists(p)]
            pool = existing or uniq
            if len(pool) == 1:
                return pool[0]

            # 多個候選時，僅在可明確用 party（真人姓名）辨識時才自動決策
            if party and self._looks_like_human_party_name(party):
                pnorm = self._norm(party)
                party_hits = [p for p in pool if pnorm and pnorm in self._norm(os.path.basename(p))]
                if len(party_hits) == 1:
                    return party_hits[0]

            self.log(f"  ⚠️ DB 精確匹配仍有多個候選（{len(pool)}），為避免誤歸檔改列待歸檔")
            return ""

        party_fallback = _pick_party_fallback(party, court_case_no or yyidno)
        if party_fallback:
            return party_fallback

        return ""
    
    def _load_processed_emails(self) -> set:
        """載入已處理的 Email ID 記錄"""
        if os.path.exists(self.processed_emails_file):
            try:
                with open(self.processed_emails_file, 'r', encoding='utf-8') as f:
                    return set(json.load(f))
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1302, exc_info=True)
        return set()

    def _save_processed_emails(self):
        """儲存已處理的 Email ID 記錄"""
        try:
            with open(self.processed_emails_file, 'w', encoding='utf-8') as f:
                json.dump(list(self.processed_emails), f)
        except Exception as e:
            self.log(f"⚠️ 無法儲存 Email 記錄: {e}")

    # 繳費通知冷卻時間（同案件通知間隔，30 天）
    PAYMENT_NOTIFY_COOLDOWN_HOURS = 720

    def _load_notified_cases(self) -> set:
        """載入已通知的案件記錄（含 TTL 清理，超過冷卻時間自動移除以便重新通知）"""
        if os.path.exists(self.notified_cases_file):
            try:
                with open(self.notified_cases_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # 相容舊格式 (list) — 全數保留但下次存檔會轉為 dict
                if isinstance(data, list):
                    return set(data)
                # 新格式 (dict: {key: ISO_timestamp}) — 清除超過冷卻時間的
                if isinstance(data, dict):
                    cutoff = (datetime.now() - timedelta(hours=self.PAYMENT_NOTIFY_COOLDOWN_HOURS)).isoformat()
                    return {k for k, v in data.items() if str(v) >= cutoff}
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1330, exc_info=True)
        return set()

    def _save_notified_cases(self):
        """儲存已通知的案件記錄（dict 格式含 timestamp，自動 TTL 清理）"""
        try:
            now_iso = datetime.now().isoformat()
            existing = {}
            # 讀取現有記錄（可能是舊 list 或新 dict）
            if os.path.exists(self.notified_cases_file):
                try:
                    with open(self.notified_cases_file, 'r', encoding='utf-8') as f:
                        raw = json.load(f)
                    if isinstance(raw, dict):
                        existing = raw
                    elif isinstance(raw, list):
                        # 舊格式遷移：給所有舊 key 設定當前時間
                        existing = {k: now_iso for k in raw}
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1349, exc_info=True)
            # 新增本次 session 的 key
            for key in self.notified_cases:
                if key not in existing:
                    existing[key] = now_iso
            # 清除超過冷卻時間的記錄
            cutoff = (datetime.now() - timedelta(hours=self.PAYMENT_NOTIFY_COOLDOWN_HOURS)).isoformat()
            existing = {k: v for k, v in existing.items() if str(v) >= cutoff}
            with open(self.notified_cases_file, 'w', encoding='utf-8') as f:
                json.dump(existing, f, ensure_ascii=False)
        except Exception as e:
            self.log(f"⚠️ 無法儲存通知記錄: {e}")

    # =========================================================================
    # 手動標記已繳費（永久跳過通知）
    # =========================================================================

    def _load_dismissed_payments(self) -> dict:
        """載入手動標記已繳費的案件（永久跳過，不受冷卻時間影響）。
        DB 為權威來源，JSON 僅在 DB 不可用時作為 fallback。
        """
        merged: dict = {}
        db_available = False
        # 1. DB (authoritative source: dedup_registry, category='payment_dismissed')
        try:
            from skills.ops.dedup_db import list_done
            for row in list_done("payment_dismissed", limit=500):
                key = row.get("item_key", "")
                if not key:
                    continue
                meta = row.get("metadata")
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
                merged[key] = meta if isinstance(meta, dict) else {"dismissed_at": str(row.get("created_at", "")), "keyword": key, "reason": "DB"}
            db_available = True
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_load_dismissed_payments_db", exc_info=True)
        # 2. JSON fallback — only used when DB is unreachable
        if not db_available and os.path.exists(self.dismissed_payments_file):
            try:
                with open(self.dismissed_payments_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    merged.update(data)
                logging.getLogger(__name__).info("dismissed_payments: DB unavailable, loaded %d entries from JSON fallback", len(merged))
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_load_dismissed_payments_json", exc_info=True)
        return merged

    def _save_dismissed_payments(self):
        """儲存手動標記已繳費的案件 — DB 為權威來源，JSON 為 cache（失敗不阻斷）。"""
        # DB (authoritative): upsert all current entries
        try:
            from skills.ops.dedup_db import mark_done
            for key, val in self.dismissed_payments.items():
                meta = val if isinstance(val, dict) else {"keyword": key, "reason": "手動標記已繳費"}
                ts = meta.get("dismissed_at") if isinstance(meta, dict) else None
                mark_done("payment_dismissed", key, status="done", metadata=meta, notified_at=ts)
        except Exception as e:
            self.log(f"⚠️ 無法儲存手動跳過記錄(DB): {e}")
        # JSON cache (best-effort, failure is non-fatal)
        try:
            with open(self.dismissed_payments_file, 'w', encoding='utf-8') as f:
                json.dump(self.dismissed_payments, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.getLogger(__name__).warning("dismissed_payments JSON cache write failed (non-fatal): %s", e)

    def dismiss_payment(self, case_keyword: str, reason: str = "") -> dict:
        """
        手動標記案件繳費通知為已處理（永久跳過）。
        case_keyword: 案號關鍵字（模糊比對，例如 '114原金訴4' 或 '張裕和'）
        """
        norm = self._normalize_case_keyword_loose(case_keyword) if hasattr(self, '_normalize_case_keyword_loose') else case_keyword.strip()
        # 找出所有匹配的 notified_cases key
        matched_keys = []
        for key in list(self.notified_cases):
            if norm in key or case_keyword in key:
                matched_keys.append(key)
        # 也搜尋已存在的 dismissed 紀錄避免重複
        already = [k for k in self.dismissed_payments if norm in k or case_keyword in k]

        if not matched_keys and not already:
            # 沒有匹配的 notified key，直接用 case_keyword 建立 dismiss 紀錄
            dismiss_key = f"web_payment:dismissed:{norm}"
            matched_keys = [dismiss_key]

        now_iso = datetime.now().isoformat()
        dismissed_count = 0
        for key in matched_keys:
            if key not in self.dismissed_payments:
                self.dismissed_payments[key] = {
                    "dismissed_at": now_iso,
                    "keyword": case_keyword,
                    "reason": reason or "手動標記已繳費",
                }
                dismissed_count += 1
        self._save_dismissed_payments()
        return {
            "success": True,
            "keyword": case_keyword,
            "dismissed_keys": matched_keys,
            "new_dismissals": dismissed_count,
            "already_dismissed": len(already),
        }

    def undismiss_payment(self, case_keyword: str) -> dict:
        """取消手動跳過標記（恢復通知）— 同步移除 DB + JSON"""
        norm = self._normalize_case_keyword_loose(case_keyword) if hasattr(self, '_normalize_case_keyword_loose') else case_keyword.strip()
        removed = []
        for key in list(self.dismissed_payments.keys()):
            if norm in key or case_keyword in key:
                del self.dismissed_payments[key]
                removed.append(key)
        self._save_dismissed_payments()
        # DB 也移除
        try:
            from skills.ops.dedup_db import remove
            for key in removed:
                remove("payment_dismissed", key)
        except Exception:
            pass
        return {"success": True, "keyword": case_keyword, "removed_keys": removed}

    def list_dismissed_payments(self) -> dict:
        """列出所有手動跳過的繳費通知"""
        return {"dismissed": self.dismissed_payments}

    def _is_payment_dismissed(self, notify_key: str, notify_key_case: str = "") -> bool:
        """檢查該案件是否已被手動標記為已繳費（in-memory + DB fallback）"""
        for key in (notify_key, notify_key_case):
            if not key:
                continue
            # 精確匹配
            if key in self.dismissed_payments:
                return True
            # 模糊比對：dismissed 的 keyword 是否出現在 notify_key 中
            for dk, dv in self.dismissed_payments.items():
                kw = dv.get("keyword", "") if isinstance(dv, dict) else ""
                if kw and kw in key:
                    return True
        # DB fallback: 如果 in-memory 沒命中，再查一次 DB（可能其他進程寫入）
        try:
            from skills.ops.dedup_db import is_done
            for key in (notify_key, notify_key_case):
                if key and is_done("payment_dismissed", key):
                    return True
        except Exception:
            pass
        return False

    def _is_proof_uploaded_for_case(self, row_json: dict) -> bool:
        """檢查 payment_proof_registry.json 是否已有該案件的繳費憑證上傳記錄。"""
        proof_registry_path = os.path.join(self.download_folder, "payment_proof_registry.json")
        if not os.path.exists(proof_registry_path):
            return False
        try:
            with open(proof_registry_path, 'r', encoding='utf-8') as f:
                proof_reg = json.load(f)
        except Exception:
            return False
        if not proof_reg:
            return False
        # 從 row_json 取得案號，嘗試組出 proof registry 的 key 格式: "114.原金訴.000166"
        yyidno = str(row_json.get("yyidno") or row_json.get("showyyidno") or "").strip()
        if not yyidno:
            return False
        # 正規化案號：移除年度字第號
        norm = re.sub(r"[年度字第號\s]+", "", yyidno)
        # 嘗試拆分為 年.案由.編號 格式
        m = re.match(r"(\d{2,3})([^\d]+)(\d+)", norm)
        if m:
            year, ctype, cnum = m.group(1), m.group(2), m.group(3).zfill(6)
            proof_key = f"{year}.{ctype}.{cnum}"
            if proof_key in proof_reg:
                return True
        # fallback: 模糊比對 — norm 出現在任一 registry key 中
        for pk in proof_reg:
            pk_norm = re.sub(r"[\.\s]+", "", pk)
            if norm and norm in pk_norm:
                return True
        return False

    # =========================================================================
    # 已下載 Registry（防止重複下載）
    # =========================================================================

    @staticmethod
    def _strip_chrome_suffix(filename: str) -> str:
        """
        去除 Chrome 自動加上的 (N) 後綴。
        例: '113_偵_002746_DOC_001_OCR (3).pdf' → '113_偵_002746_DOC_001_OCR.pdf'
        """
        stem, ext = os.path.splitext(filename)
        # 移除尾部的 " (N)"
        cleaned = re.sub(r'\s*\(\d+\)$', '', stem)
        return cleaned + ext

    def _load_download_registry(self) -> dict:
        """載入已下載檔案 registry"""
        if os.path.exists(self.download_registry_file):
            try:
                with open(self.download_registry_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1507, exc_info=True)
        return {}

    def _load_manual_archive_map(self) -> dict:
        """載入手動歸檔映射"""
        if os.path.exists(self.manual_archive_map_file):
            try:
                with open(self.manual_archive_map_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1517, exc_info=True)
        return {}

    def _load_manual_archive_requests(self) -> list:
        """載入手動歸檔請求"""
        if os.path.exists(self.manual_archive_requests_file):
            try:
                with open(self.manual_archive_requests_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1527, exc_info=True)
        return []

    @staticmethod
    def _manual_key_norm(text: str) -> str:
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").strip().lower())

    def _save_manual_archive_map(self):
        try:
            with open(self.manual_archive_map_file, 'w', encoding='utf-8') as f:
                json.dump(self._manual_archive_map, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"⚠️ 無法儲存手動歸檔映射: {e}")

    def _manual_map_candidate_keys(self, yyidno: str = "", court_case_no: str = "", laf_case_no: str = "", party: str = "") -> List[str]:
        keys: List[str] = []

        def _add(prefix: str, value: str):
            norm = self._manual_key_norm(value)
            if norm:
                keys.append(f"{prefix}:{norm}")

        _add("court_case_no", court_case_no)
        _add("yyidno", yyidno)
        _add("laf_case_no", laf_case_no)
        if self.allow_party_archive_map and party and self._looks_like_human_party_name(party):
            _add("party", party)

        uniq: List[str] = []
        seen = set()
        for k in keys:
            if k in seen:
                continue
            seen.add(k)
            uniq.append(k)
        return uniq

    def _resolve_case_folder_from_manual_map(self, yyidno: str = "", court_case_no: str = "", laf_case_no: str = "", party: str = "") -> str:
        if not isinstance(self._manual_archive_map, dict):
            return ""
        for key in self._manual_map_candidate_keys(yyidno=yyidno, court_case_no=court_case_no, laf_case_no=laf_case_no, party=party):
            try:
                path = self._to_local_case_path(str(self._manual_archive_map.get(key) or "").strip())
            except Exception:
                path = ""
            if path and os.path.isdir(path):
                return path
        return ""

    def _remember_auto_archive_mapping(self, folder_path: str, yyidno: str = "", court_case_no: str = "", laf_case_no: str = "", party: str = "") -> int:
        folder = self._to_local_case_path(folder_path or "")
        if not folder or not os.path.isdir(folder):
            return 0
        if not isinstance(self._manual_archive_map, dict):
            self._manual_archive_map = {}

        changed = 0
        for key in self._manual_map_candidate_keys(yyidno=yyidno, court_case_no=court_case_no, laf_case_no=laf_case_no, party=party):
            old = str(self._manual_archive_map.get(key) or "").strip()
            if old == folder:
                continue
            self._manual_archive_map[key] = folder
            changed += 1

        if changed > 0:
            self._save_manual_archive_map()
        return changed

    def _save_download_registry(self):
        """儲存已下載檔案 registry"""
        try:
            with open(self.download_registry_file, 'w', encoding='utf-8') as f:
                json.dump(self._download_registry, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"⚠️ 無法儲存下載 registry: {e}")

    def _load_apply_registry(self) -> dict:
        """載入聲請紀錄 registry（追蹤每案聲請次數）"""
        if os.path.exists(self.apply_registry_file):
            try:
                with open(self.apply_registry_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1612, exc_info=True)
        return {}

    def _save_apply_registry(self):
        try:
            with open(self.apply_registry_file, "w", encoding="utf-8") as f:
                json.dump(self._apply_registry, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"⚠️ 無法儲存 apply registry: {e}")

    def record_application(self, case_key: str, case_info: dict = None):
        """記錄一次聲請，用於判斷是否為首次聲請。"""
        entry = self._apply_registry.get(case_key, {})
        count = entry.get("count", 0) + 1
        entry["count"] = count
        entry["last_apply"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if case_info:
            entry["court_code"] = case_info.get("court_code", "")
            entry["client_name"] = case_info.get("client_name", "")
        self._apply_registry[case_key] = entry
        self._save_apply_registry()

    def is_first_application(self, case_key: str) -> bool:
        """判斷某案件是否為首次聲請（registry 中沒有紀錄或 count=0）。"""
        entry = self._apply_registry.get(case_key, {})
        return entry.get("count", 0) == 0

    @staticmethod
    def make_apply_registry_key(case_info: dict) -> str:
        """產生案件的聲請 registry key。"""
        court = (case_info.get("court_code") or "").strip()
        year = (case_info.get("year") or "").strip()
        case_type = (case_info.get("case_type") or "").strip()
        case_number = (case_info.get("case_number") or "").strip()
        return f"{court}:{year}.{case_type}.{case_number}"

    def _load_payment_registry(self) -> dict:
        """載入待繳費處理 registry（key: payid/rowid）"""
        if os.path.exists(self.payment_registry_file):
            try:
                with open(self.payment_registry_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1657, exc_info=True)
        return {}

    def _save_payment_registry(self):
        """儲存待繳費處理 registry"""
        try:
            with open(self.payment_registry_file, 'w', encoding='utf-8') as f:
                json.dump(self.payment_registry, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"⚠️ 無法儲存 payment registry: {e}")

    @staticmethod
    def _payment_registry_key(row_json: dict) -> str:
        """產生待繳費列的穩定 key。
        優先級：rowid（DB 穩定主鍵）→ yyidno+party（案號+當事人，正規化）→ p_payid（不穩定，備援）。
        注意：p_payid 在法院端每次查詢可能變化，不適合作主 key。
        """
        if not isinstance(row_json, dict):
            return ""

        def _norm(v: str) -> str:
            """正規化案號：移除年度字第號等結構字、符號，去前導零。"""
            s = re.sub(r"[年度字第號\s\.\-_/／\\]+", "", str(v or ""))
            s = re.sub(r"\d+", lambda m: str(int(m.group(0))), s)
            return s.strip().lower()

        rowid = str(row_json.get("rowid") or "").strip()
        if rowid:
            return f"rowid:{rowid}"
        cno_raw = str(row_json.get("yyidno") or row_json.get("showyyidno") or row_json.get("c60yyidno") or "").strip()
        cno = _norm(cno_raw) if cno_raw else ""
        party = str(row_json.get("clnm") or "").strip()
        if cno and party:
            return f"case:{cno}:{party}"
        if cno:
            return f"case:{cno}"
        p_payid = str(row_json.get("p_payid") or "").strip()
        if p_payid:
            return f"payid:{p_payid}"
        no = str(row_json.get("no") or "").strip()
        if not cno_raw and not no:
            return ""
        return f"fallback:{_norm(cno_raw)}:{no}".strip(":")

    def _is_payment_processed(self, row_json: dict) -> bool:
        key = self._payment_registry_key(row_json)
        if not key:
            return False

        # 查找 entry：先精確匹配新 key，再嘗試舊格式相容（p_payid / rowid 互查）
        entry = (self.payment_registry or {}).get(key) or {}
        if not entry:
            # 相容查詢：用 rowid、p_payid、yyidno+party 任一匹配 registry 中的值
            rowid = str(row_json.get("rowid") or "").strip()
            p_payid = str(row_json.get("p_payid") or "").strip()
            yyidno = str(row_json.get("yyidno") or row_json.get("showyyidno") or "").strip()
            yyidno_norm = self._normalize_case_keyword_loose(yyidno) if yyidno else ""
            party = str(row_json.get("clnm") or "").strip()
            for _k, _v in (self.payment_registry or {}).items():
                if not isinstance(_v, dict):
                    continue
                if rowid and str(_v.get("rowid") or "").strip() == rowid:
                    entry = _v; break
                if p_payid and str(_v.get("p_payid") or "").strip() == p_payid:
                    entry = _v; break
                # 用正規化比對案號，避免 "114年度原訴字第84號" vs "114.原訴.84" 不匹配
                if yyidno_norm:
                    stored_cno = str(_v.get("yyidno") or _v.get("case_number") or "").strip()
                    if stored_cno and self._normalize_case_keyword_loose(stored_cno) == yyidno_norm:
                        if not party or str(_v.get("party") or "").strip() == party:
                            entry = _v; break
        if not entry:
            return False

        # 僅有 key 不足以判定已處理：必須至少有可用檔案紀錄且檔案仍可定位。
        path_hints = [str(x).strip() for x in (entry.get("file_paths") or []) if str(x).strip()]
        for p in path_hints:
            if os.path.isfile(p):
                return True

        name_hints = [str(x).strip() for x in (entry.get("files") or []) if str(x).strip()]
        if not name_hints:
            return False

        return bool(self._resolve_payment_registry_files(row_json))

    def _mark_payment_processed(self, row_json: dict, files: Optional[List[str]] = None, case_info: Optional[dict] = None):
        key = self._payment_registry_key(row_json)
        if not key:
            return
        file_paths: List[str] = []
        file_names: List[str] = []
        for x in (files or []):
            xp = str(x or "").strip()
            if not xp:
                continue
            ap = os.path.realpath(xp)
            file_paths.append(ap)
            file_names.append(os.path.basename(ap))

        # 保持順序並去重
        file_paths = list(dict.fromkeys(file_paths))
        file_names = list(dict.fromkeys(file_names))

        # 從多個來源取得當事人姓名：case_info > row_json(clnm) > 檔名解析
        party = ""
        if case_info and isinstance(case_info, dict):
            party = str(case_info.get("party") or "").strip()
        if not party:
            party = str((row_json or {}).get("clnm") or "").strip()
        if not party:
            # 嘗試從檔名解析（繳費單_[當事人H]_115.原金訴.000044.pdf）
            for fn in file_names:
                if fn.startswith("繳費單_") and "_" in fn[4:]:
                    parts = fn.split("_", 2)
                    if len(parts) >= 2 and parts[1]:
                        party = parts[1]
                        break

        case_number = str((row_json or {}).get("yyidno") or (row_json or {}).get("showyyidno") or "")
        if not case_number and case_info and isinstance(case_info, dict):
            case_number = str(case_info.get("case_number") or case_info.get("yyidno") or "")

        self.payment_registry[key] = {
            "processed_at": datetime.now().isoformat(),
            "yyidno": case_number,
            "p_payid": str((row_json or {}).get("p_payid") or ""),
            "rowid": str((row_json or {}).get("rowid") or ""),
            "party": party,
            "case_number": case_number,
            "files": file_names,
            "file_paths": file_paths,
        }
        self._save_payment_registry()

    def _resolve_payment_registry_files(self, row_json: dict) -> List[str]:
        """
        從 payment_registry 還原該待繳費項目的實際檔案路徑（用於補發通知）。
        """
        key = self._payment_registry_key(row_json)
        if not key:
            return []
        entry = (self.payment_registry or {}).get(key) or {}

        # 相容查詢：key 正規化後可能找不到舊格式的 entry
        if not entry:
            rowid = str(row_json.get("rowid") or "").strip()
            p_payid = str(row_json.get("p_payid") or "").strip()
            yyidno = str(row_json.get("yyidno") or row_json.get("showyyidno") or "").strip()
            yyidno_norm = self._normalize_case_keyword_loose(yyidno) if yyidno else ""
            party = str(row_json.get("clnm") or "").strip()
            for _k, _v in (self.payment_registry or {}).items():
                if not isinstance(_v, dict):
                    continue
                if rowid and str(_v.get("rowid") or "").strip() == rowid:
                    entry = _v; break
                if p_payid and str(_v.get("p_payid") or "").strip() == p_payid:
                    entry = _v; break
                if yyidno_norm:
                    stored_cno = str(_v.get("yyidno") or _v.get("case_number") or "").strip()
                    if stored_cno and self._normalize_case_keyword_loose(stored_cno) == yyidno_norm:
                        if not party or str(_v.get("party") or "").strip() == party:
                            entry = _v; break

        # 優先使用 file_paths（完整路徑），若檔案仍存在就直接回傳
        direct_paths = [str(x).strip() for x in (entry.get("file_paths") or []) if str(x).strip()]
        if direct_paths:
            existing = [p for p in direct_paths if os.path.isfile(p)]
            if existing:
                return existing

        names = [str(x).strip() for x in (entry.get("files") or []) if str(x).strip()]
        # fallback: 從 file_paths 提取檔名
        if not names and direct_paths:
            names = [os.path.basename(p) for p in direct_paths if os.path.basename(p)]
        if not names:
            return []

        name_set = set(names)
        found: List[str] = []
        seen: set[str] = set()

        # 先掃常見日期資料夾（今天 + processed_at 日期）
        date_hints = [datetime.now().strftime("%Y%m%d")]
        try:
            processed_at = str(entry.get("processed_at") or "").strip()
            if processed_at:
                d = processed_at.split("T", 1)[0].replace("-", "")
                if len(d) == 8:
                    date_hints.append(d)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1848, exc_info=True)
        date_hints = list(dict.fromkeys(date_hints))

        for ds in date_hints:
            base = os.path.join(self.download_folder, ds)
            if not os.path.isdir(base):
                continue
            for n in names:
                fp = os.path.join(base, n)
                if os.path.isfile(fp):
                    rp = os.path.realpath(fp)
                    if rp not in seen:
                        found.append(fp)
                        seen.add(rp)

        # 再保底掃描整個 download_folder（避免日期不一致）
        if len(found) < len(name_set):
            try:
                for root, _, files in os.walk(self.download_folder):
                    for fn in files:
                        if fn not in name_set:
                            continue
                        fp = os.path.join(root, fn)
                        if not os.path.isfile(fp):
                            continue
                        rp = os.path.realpath(fp)
                        if rp in seen:
                            continue
                        found.append(fp)
                        seen.add(rp)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1879, exc_info=True)

        return found

    @staticmethod
    def _is_within_days(iso_date: str, days: int = 7) -> bool:
        """判斷 ISO 日期是否在今天起 N 天內（含已逾期但不超過 days 天）。

        - 未來 N 天內到期 → True（提醒即將到期）
        - 已逾期 1~N 天 → True（提醒趕快繳）
        - 已逾期超過 N 天 → False（停止通知，避免永久騷擾）
        """
        try:
            deadline = datetime.strptime(iso_date[:10], "%Y-%m-%d").date()
            today = datetime.now().date()
            diff = (deadline - today).days  # 正值=未來, 負值=已過期
            return -days <= diff <= days
        except Exception:
            return True  # 無法解析時預設需要通知

    def _has_payment_proof_uploaded(self, row_json: dict) -> bool:
        """
        檢查該待繳費案件是否已繳費（繳費憑證已上傳或狀態已更新）。

        判斷依據（任一命中即視為已繳費）：
        - paystatus == '1'  → OLA 列表標記已繳
        - p_status == 'Y'   → OLA 列表繳費完成
        - payment == 'Y'    → OLA 繳費旗標
        - statusnm / result 文字包含「已繳」「繳費完成」「收據」
        - 不再是 pending_payment（statusnm 無「待繳費」且 paystatus != '2'）
          且 status 已推進到後續階段 (status >= '4')
        """
        if not isinstance(row_json, dict):
            return False
        paystatus = str(row_json.get("paystatus") or "").strip()
        p_status = str(row_json.get("p_status") or "").strip().upper()
        payment = str(row_json.get("payment") or "").strip().upper()
        statusnm = str(row_json.get("statusnm") or "").strip()
        result_text = str(row_json.get("result") or "").strip()
        status_code = str(row_json.get("status") or "").strip()
        combined_text = f"{statusnm} {result_text}"

        paid = False
        if paystatus == "1" or p_status == "Y" or payment == "Y":
            paid = True
        elif any(kw in combined_text for kw in ("已繳", "繳費完成", "收據", "繳訖")):
            paid = True
        elif (paystatus not in ("", "2") and status_code >= "4"
              and "待繳費" not in combined_text):
            paid = True

        if paid:
            yyidno = row_json.get("showyyidno") or row_json.get("yyidno") or ""
            self.log(f"  [DEBUG] _has_payment_proof_uploaded=True: {yyidno} "
                     f"(paystatus={paystatus}, p_status={p_status}, payment={payment}, "
                     f"status={status_code}, statusnm={statusnm})")
        return paid

    def _notify_payment_if_needed(self, row_json: dict, case_info: dict = None, file_paths: Optional[List[str]] = None):
        """
        繳費通知邏輯：
        - 7 天內到期（或已逾期）且尚未上傳繳費憑證 → 通知
        - 本次 session 已通知過 → 跳過（避免同一次掃描重複發送）
        """
        case_info = case_info or {}
        reg_key = self._payment_registry_key(row_json)
        notify_key = f"web_payment:{reg_key}" if reg_key else ""
        # 案號級 notify_key 作為第二道去重（避免同案不同 key 重複通知）
        yyidno_raw = str(row_json.get("yyidno") or row_json.get("showyyidno") or case_info.get("case_number") or case_info.get("showyyidno") or "").strip()
        yyidno_norm = self._normalize_case_keyword_loose(yyidno_raw) if yyidno_raw else ""
        party = str(row_json.get("clnm") or case_info.get("party") or "").strip()
        notify_key_case = f"web_payment:case:{yyidno_norm}:{party}" if yyidno_norm else ""
        if not notify_key:
            notify_key = notify_key_case or f"web_payment:{yyidno or 'unknown'}"

        # 手動標記已繳費 → 永久跳過
        if self._is_payment_dismissed(notify_key, notify_key_case):
            self.log(f"  ℹ️ 案件已手動標記為已繳費，跳過通知: {notify_key}")
            return True

        # MAGI 已上傳繳費憑證（payment_proof_registry）→ 跳過
        if self._is_proof_uploaded_for_case(row_json):
            self.log(f"  ℹ️ MAGI 已上傳繳費憑證，跳過通知: {notify_key}")
            return True

        # 同一 session 已通知 → 跳過（兩個 key 任一命中即跳過）
        if notify_key in self.notified_cases:
            return True
        if notify_key_case and notify_key_case in self.notified_cases:
            return True

        if file_paths is None:
            file_paths = self._resolve_payment_registry_files(row_json)
        file_paths = [p for p in (file_paths or []) if os.path.exists(p)]

        deadline_raw = row_json.get("paylimitdt") or row_json.get("limitdt") or ""
        deadline_iso = self._roc_compact_date_to_iso(deadline_raw)

        # 已上傳繳費憑證 → 不再通知
        self.log(f"  [DEBUG] 繳費判斷: key={notify_key}, paystatus={row_json.get('paystatus')}, "
                 f"p_status={row_json.get('p_status')}, payment={row_json.get('payment')}, "
                 f"status={row_json.get('status')}, statusnm={row_json.get('statusnm')}, "
                 f"result={str(row_json.get('result') or '')[:30]}")
        if self._has_payment_proof_uploaded(row_json):
            self.log(f"  ℹ️ 案件已上傳繳費憑證，跳過通知: {notify_key}")
            return True

        # 繳費期限超過 14 天 → 僅當沒有 PDF 需交付時才跳過
        # 有 PDF 附件時必須送出，讓使用者收到繳費單
        if deadline_raw and not self._is_within_days(deadline_iso, days=14) and not file_paths:
            self.log(f"  ℹ️ 繳費期限 {deadline_iso} 超過 14 天且無檔案需交付，暫不通知")
            return True

        info = FileReviewInfo()
        info.court = row_json.get("crtnm") or case_info.get("court", "")
        info.client_name = case_info.get("party") or row_json.get("clnm") or "(未知)"
        info.court_case_no = case_info.get("showyyidno") or case_info.get("case_number") or row_json.get("showyyidno") or row_json.get("yyidno") or "-"
        info.status = "待繳費"
        info.payment_deadline = deadline_iso
        try:
            info.payment_amount = int(str(row_json.get("procfee") or row_json.get("fee") or "0").strip() or "0")
        except Exception:
            info.payment_amount = 0
        info.files = file_paths

        sent_ok = bool(self.notify_payment_needed(info))
        if sent_ok:
            self.notified_cases.add(notify_key)
            if notify_key_case:
                self.notified_cases.add(notify_key_case)
            self._save_notified_cases()
        else:
            self.log(f"  ⚠️ 繳費通知未送達，暫不標記已通知: {notify_key}")
        return sent_ok

    def _register_downloaded(self, filename: str, yyidno: str = "", case_info: dict = None):
        """將檔案登錄到 registry，key 為去除 (N) 後綴的基礎檔名"""
        base_name = self._strip_chrome_suffix(filename)
        yy_norm = self._normalize_download_case_id(yyidno)
        if base_name not in self._download_registry:
            self._download_registry[base_name] = {
                "first_downloaded": datetime.now().isoformat(),
                "yyidno": yyidno,
                "yyidno_norm": yy_norm,
                "case_info": case_info or {},
            }
            return
        # 既有 entry 若沒有規範化欄位則補齊
        try:
            ent = self._download_registry.get(base_name) or {}
            if yy_norm and not ent.get("yyidno_norm"):
                ent["yyidno_norm"] = yy_norm
                self._download_registry[base_name] = ent
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2033, exc_info=True)

    @staticmethod
    def _normalize_download_case_id(v: str) -> str:
        s = (v or "").strip().lower()
        if not s:
            return ""
        return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", s)

    @staticmethod
    def _download_entry_artifact_type(entry: dict) -> str:
        if not isinstance(entry, dict):
            return ""
        case_info = entry.get("case_info")
        if isinstance(case_info, dict):
            return str(case_info.get("artifact_type") or "").strip().lower()
        return ""

    @staticmethod
    def _is_payment_slip_filename(filename: str) -> bool:
        base = os.path.basename(str(filename or "")).strip()
        return bool(base) and base.startswith("繳費單_")

    def _is_already_downloaded(self, filename: str) -> bool:
        """檢查檔案是否已登錄在 registry 中"""
        base_name = self._strip_chrome_suffix(filename)
        return base_name in self._download_registry

    def _is_yyidno_fully_downloaded(self, yyidno: str) -> bool:
        """
        檢查某個 yyidno（案號 identifier）的所有檔案是否都已下載。
        只有 registry 中存在「非繳費單」的紀錄，才視為卷宗已下載。
        """
        if not yyidno:
            return False
        target_norm = self._normalize_download_case_id(yyidno)
        for entry in self._download_registry.values():
            if self._download_entry_artifact_type(entry) == "payment_slip":
                continue
            if entry.get("yyidno") == yyidno:
                return True
            if target_norm:
                e_norm = entry.get("yyidno_norm") or self._normalize_download_case_id(entry.get("yyidno") or "")
                if e_norm and e_norm == target_norm:
                    return True
            ci = entry.get("case_info") if isinstance(entry, dict) else {}
            if isinstance(ci, dict):
                show = (ci.get("showyyidno") or "").strip()
                if show and self._normalize_download_case_id(show) == target_norm:
                    return True
                cno = (ci.get("case_number") or "").strip()
                if cno and self._normalize_download_case_id(cno) == target_norm:
                    return True
        return False

    def _case_review_folder_has_files(self, case_info: dict) -> bool:
        """
        檢查案件的閱卷資料子資料夾是否已有檔案。
        用於可下載案件去重：如果閱卷資料夾已有內容，代表已下載過。
        """
        try:
            showyyidno = (case_info.get("showyyidno") or "").strip()
            yyidno = (case_info.get("case_number") or case_info.get("yyidno") or "").strip()
            party = (case_info.get("party") or case_info.get("clnm") or "").strip()
            if not (showyyidno or yyidno or party):
                return False

            class _Tmp:
                def __init__(self, case_number, client_name, court_case_no=""):
                    self.case_number = case_number
                    self.client_name = client_name
                    self.court_case_no = court_case_no
                    self.laf_case_no = ""

            tmp = _Tmp(case_number=(yyidno or showyyidno), client_name=party, court_case_no=showyyidno)
            folder = self._resolve_case_folder(tmp)
            if not folder or not os.path.isdir(folder):
                return False

            # 尋找閱卷資料子資料夾
            review_folder = None
            for subfolder in os.listdir(folder):
                if '閱卷' in subfolder:
                    review_folder = os.path.join(folder, subfolder)
                    break
            if not review_folder or not os.path.isdir(review_folder):
                return False

            # 計算閱卷資料夾中的檔案數（排除隱藏檔和 .DS_Store）
            file_count = 0
            for root, _dirs, files in os.walk(review_folder):
                for fn in files:
                    if fn.startswith('.') or fn == 'Thumbs.db':
                        continue
                    if self._is_payment_slip_filename(fn):
                        continue
                    file_count += 1
                    if file_count >= 1:
                        return True
            return False
        except Exception:
            return False

    def _is_fee_exempt_case(self, court_case_no: str = "", party: str = "", yyidno: str = "") -> bool:
        """
        判斷案件是否免繳費（僅「指定辯護案件」免繳費）。
        注意：無償案件仍需繳費，只有指定辯護案件不需要按繳費單。
        """
        if not self.db:
            return False
        try:
            # 解析案號
            y, ct, num = "", "", ""
            if yyidno:
                parts = [p.strip() for p in yyidno.split(".") if p.strip()]
                if len(parts) >= 3:
                    y, ct, num = parts[0], self._norm(parts[1]), str(int(parts[2]))
            if not (y and ct and num) and court_case_no:
                y, ct, num = self._parse_court_case_no(court_case_no)
                if y and ct and num:
                    y, ct, num = str(y), self._norm(ct), str(int(num))
            if not (y and ct and num):
                return False

            patterns = [f"%{y}%{ct}%{num}%"]
            for pattern in patterns:
                rows = self.db.execute(
                    "SELECT `case_category` FROM `cases` "
                    "WHERE `court_case_number` LIKE %s "
                    "AND `case_category` IS NOT NULL AND `case_category` != '' "
                    "LIMIT 5",
                    (pattern,),
                    fetch='all'
                ) or []
                if isinstance(rows, dict):
                    rows = [rows]
                for row in rows:
                    cat = str((row or {}).get("case_category") or "").strip()
                    if cat == "指定辯護案件":
                        return True
            return False
        except Exception:
            return False

    @staticmethod
    def _to_local_case_path(path_value: str) -> str:
        return translate_case_path_to_local(path_value)

    @staticmethod
    def _normalize_case_text(text: str) -> str:
        if not text:
            return ""
        try:
            import html
            text = html.unescape(text)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2189, exc_info=True)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = text.replace('&nbsp;', ' ').replace('\u3000', ' ')
        return re.sub(r'\s+', ' ', text).strip()

    @staticmethod
    def _format_court_case_no(raw: str) -> str:
        if not raw:
            return ""
        text = re.sub(r'\s+', '', raw)
        m = re.search(r'(\d{2,4})年?度?(.+?)字第?0*(\d+)號?', text)
        if m:
            year, case_type, number = m.groups()
            return f"{year}年度{case_type}字第{int(number)}號"
        m = re.search(r'(\d{2,4})\.(.+?)\.(\d{1,6})', text)
        if m:
            year, case_type, number = m.groups()
            return f"{year}年度{case_type}字第{int(number)}號"
        return raw.strip()

    def _extract_case_identifiers(self, text: str) -> Dict[str, str]:
        normalized = self._normalize_case_text(text)
        result = {
            "laf_case_no": "",
            "application_no": "",
            "court_case_no": ""
        }

        if not normalized:
            return result

        court_patterns = [
            r'(?:法院案號|裁判案號|法院案件編號)\s*[：:]\s*([^\s，。,;；]+)',
            r'案號\s*[：:]\s*((?:\d{2,4}\s*年?度?\s*[^，。,;\s]{1,20}\s*字第?\s*\d+\s*號?)|(?:\d{2,4}\.[^，。,;\s]{1,20}\.\d{1,6}))',
            r'(\d{2,4}\s*年?度?\s*[^，。,;\s]{1,20}\s*字第?\s*\d+\s*號?)',
            r'(\d{2,4}\.[^，。,;\s]{1,20}\.\d{1,6})',
        ]
        for pattern in court_patterns:
            match = re.search(pattern, normalized)
            if match:
                candidate = self._format_court_case_no(match.group(1))
                if candidate:
                    result["court_case_no"] = candidate
                    break

        application_patterns = [
            r'(?:申請編號|聲請編號|申請案號|聲請案號)\s*[：:]\s*([A-Za-z0-9\-]+)',
            r'\b[0-9]{6}-[A-Za-z]-\d{3}\b'
        ]
        for pattern in application_patterns:
            match = re.search(pattern, normalized)
            if match:
                result["application_no"] = match.group(1) if match.lastindex else match.group(0)
                break

        laf_patterns = [
            r'(?:法扶案號|扶助案號|本會案號|基金會案號)\s*[：:]\s*([A-Za-z0-9\-]+)',
            r'\b\d{6,}-[A-Za-z]-\d{3}\b'
        ]
        for pattern in laf_patterns:
            match = re.search(pattern, normalized)
            if match:
                result["laf_case_no"] = match.group(1) if match.lastindex else match.group(0)
                break

        if not result["laf_case_no"] and result["application_no"]:
            result["laf_case_no"] = result["application_no"]

        return result

    def _apply_case_identifiers(self, info: FileReviewInfo, text: str):
        ids = self._extract_case_identifiers(text)
        info.laf_case_no = ids["laf_case_no"]
        info.application_no = ids["application_no"]
        info.court_case_no = ids["court_case_no"]

    @staticmethod
    def _notification_case_no(info: FileReviewInfo) -> str:
        return info.court_case_no or "(未解析法院案號)"

    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{timestamp}] [閱卷] {message}"
        print(full_msg)
        if self.log_callback:
            self.log_callback(full_msg)
    
    # ---------- Gmail 監控 ----------
    
    def _init_gmail(self, allow_interactive: bool = True) -> bool:
        """初始化 Gmail API（預設允許互動式 OAuth；排程/背景工作可關閉互動）"""
        if not GMAIL_AVAILABLE:
            self.log("Gmail API 未安裝")
            return False
            
        global build, InstalledAppFlow, Request
        if build is None:
            try:
                from googleapiclient.discovery import build
                from google_auth_oauthlib.flow import InstalledAppFlow
                from google.auth.transport.requests import Request
            except ImportError as e:
                self.log(f"Gmail API Import Failed: {e}")
                return False
        
        try:
            creds = None
            
            if os.path.exists(self.token_path):
                with open(self.token_path, 'rb') as token:
                    creds = pickle.load(token)
            
            if not creds or not creds.valid:
                # 1) 先嘗試用 refresh token 自動續期
                if creds and creds.expired and creds.refresh_token:
                    try:
                        creds.refresh(Request())
                    except Exception as refresh_err:
                        # 常見情境：invalid_grant（refresh token 被撤銷/過期）
                        err_s = str(refresh_err or "")
                        if "invalid_grant" in err_s.lower():
                            self.log("⚠️ Gmail token refresh 失敗 (invalid_grant)，需要重新授權。將備份舊 token 後改走重新授權流程。")
                            try:
                                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                                backup = f"{self.token_path}.invalid_{ts}"
                                os.replace(self.token_path, backup)
                                self.log(f"  ℹ️ 已備份舊 token: {backup}")
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2317, exc_info=True)
                            creds = None
                        else:
                            raise

                # 2) 仍無可用憑證 → 走互動式授權
                if (not creds) or (not getattr(creds, "valid", False)):
                    if not os.path.exists(self.credentials_path):
                        self.log(f"找不到憑證: {self.credentials_path}")
                        return False

                    if not allow_interactive:
                        self._last_gmail_error = "NEED_INTERACTIVE_OAUTH"
                        self.log("⚠️ 需要互動式 Gmail OAuth 重新授權，但目前是非互動模式。請改用「重新授權閱卷信箱」流程再試。")
                        return False

                    flow = InstalledAppFlow.from_client_secrets_file(self.credentials_path, self.SCOPES)
                    creds = flow.run_local_server(port=0)
                
                with open(self.token_path, 'wb') as token:
                    pickle.dump(creds, token)
            
            self.gmail_service = build('gmail', 'v1', credentials=creds)
            self.log("Gmail API 初始化成功")
            return True
            
        except Exception as e:
            self._last_gmail_error = str(e)[:300]
            self.log(f"Gmail API 初始化失敗: {e}")
            return False

    def reauth_gmail(self) -> bool:
        """
        強制重新授權 Gmail（互動式）。
        - 會先備份舊 token
        - 會開啟瀏覽器/本機 OAuth 回呼
        """
        try:
            if self.token_path and os.path.exists(self.token_path):
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup = f"{self.token_path}.bak_{ts}"
                try:
                    os.replace(self.token_path, backup)
                    self.log(f"ℹ️ 已備份既有 token: {backup}")
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2362, exc_info=True)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2364, exc_info=True)
        self.gmail_service = None
        return self._init_gmail(allow_interactive=True)
    
    # notify_payment_needed 方法已移至第 2983 行，包含 PDF 上傳邏輯

    def notify_ready_to_download(self, info: FileReviewInfo, webhook_url: str = None):
        """通知閱卷資料可下載"""
        import requests
        
        title = "📥 法院閱卷資料已準備完成"
        court_case_no = self._notification_case_no(info)
        message = (
            f"法院案號：{court_case_no}\n"
            f"法院：{info.court}\n"
            f"檔案數：{len(info.files)}\n"
            f"下載期限：{info.download_deadline}"
        )
        if info.laf_case_no:
            message += f"\n法扶案號：{info.laf_case_no}"
        if info.application_no and info.application_no != info.laf_case_no:
            message += f"\n申請編號：{info.application_no}"
        
        if webhook_url:
            try:
                data = {
                    "embeds": [{
                        "title": title,
                        "description": message,
                        "color": 0x2ECC71  # Green
                    }]
                }
                requests.post(webhook_url, json=data)
                self.log(f"已發送下載通知至指定 Webhook: {court_case_no}")
                return
            except Exception as e:
                self.log(f"⚠️ Webhook 發送失敗，嘗試使用預設 Discord: {e}")
        
        if self.discord:
            self.discord.send_notification(title, message, color=0x2ECC71)
        else:
            self.log("⚠️ 無法發送通知 (未設定 Discord)")
    
    def check_payment_emails(self, start_date: str = None, end_date: str = None) -> List[FileReviewInfo]:
        """檢查繳費單通知
        
        Args:
            start_date: 開始日期 (格式: YYYY/MM/DD)，預設為 7 天前
            end_date: 結束日期 (格式: YYYY/MM/DD)，預設為今天
        """
        if not self.gmail_service and not self._init_gmail(allow_interactive=True):
            return []
        
        try:
            # 使用傳入的日期或預設 7 天
            if not start_date:
                start_date = (datetime.now() - timedelta(days=7)).strftime("%Y/%m/%d")
            
            # 搜尋繳費單通知（只搜尋含繳費單的郵件，排除法扶來源避免與法扶通知重疊）
            query = f'subject:(含繳費單) -from:@laf.org.tw -from:laf.server after:{start_date}'
            if end_date:
                # Gmail 的 before: 是排除當天的，所以需要加一天才能包含 end_date 當天
                try:
                    end_dt = datetime.strptime(end_date, "%Y/%m/%d")
                    end_dt_next = end_dt + timedelta(days=1)
                    query += f' before:{end_dt_next.strftime("%Y/%m/%d")}'
                except Exception:
                    query += f' before:{end_date}'
            self.log(f"  🔍 [DEBUG] 執行搜尋 query: {query}")
            
            results = self.gmail_service.users().messages().list(
                userId='me', q=query, maxResults=20
            ).execute()
            
            messages = results.get('messages', [])
            self.log(f"  📊 [DEBUG] Gmail API 回傳 {len(messages)} 封符合的郵件")
            
            notices = []
            
            for msg_info in messages:
                try:
                    msg_id = msg_info['id']
                    
                    # 檢查是否已處理
                    is_processed = msg_id in self.processed_emails
                    
                    msg = self.gmail_service.users().messages().get(
                        userId='me', id=msg_id, format='full'
                    ).execute()

                    # DEBUG: 打印主旨
                    headers = msg.get('payload', {}).get('headers', [])
                    subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "No Subject")
                    self.log(f"  📨 [DEBUG] 檢查信件 [{msg_id}] 主旨: {subject}")
                    
                    info = self._parse_review_email(msg)
                    
                    if info:
                        info.message_id = msg_id
                        info.status = "待繳費"
                        
                        if is_processed:
                            info.status = "已處理"
                            # 雖然已處理，仍加入列表顯示，但不執行後續動作
                            notices.append(info)
                            continue

                        notices.append(info)
                        
                        # (選擇性) 標記為已讀
                        try:
                            self.gmail_service.users().messages().modify(
                                userId='me', id=msg_id,
                                body={'removeLabelIds': ['UNREAD']}
                            ).execute()
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2480, exc_info=True)
                        
                        # 下載附件 (繳費單)
                        self._download_attachments(msg_id, info)
                        
                        # 發送繳費通知（red_phone: TG + DC mirror）
                        self.notify_payment_needed(info)
                        
                        self.processed_emails.add(msg_id)
                        self._save_processed_emails() # 立即儲存
                
                except Exception as e:
                    self.log(f"  ⚠️ 處理信件 {msg_id if 'msg_id' in locals() else 'unknown'} 失敗: {e}")
                    # 繼續處理下一封
                    continue
            
            if notices:
                self.log(f"發現 {len(notices)} 封繳費單通知")
            
            return notices
            
        except Exception as e:
            self.log(f"檢查繳費單失敗: {e}")
            return []
    
    
    def check_ready_emails(self, start_date: str = None, end_date: str = None) -> List[FileReviewInfo]:
        """檢查交付完成通知
        
        Args:
            start_date: 開始日期 (格式: YYYY/MM/DD)，預設為 7 天前
            end_date: 結束日期 (格式: YYYY/MM/DD)，預設為今天
        """
        if not self.gmail_service and not self._init_gmail(allow_interactive=True):
            return []
        
        try:
            # 使用傳入的日期或預設 7 天
            if not start_date:
                start_date = (datetime.now() - timedelta(days=7)).strftime("%Y/%m/%d")
            
            # 搜尋交付完成通知（排除法扶來源避免與法扶通知重疊）
            query = f'subject:(法院 完成 線上 交付 核閱 通知) -from:@laf.org.tw -from:laf.server after:{start_date}'
            if end_date:
                query += f' before:{end_date}'
            self.log(f"  🔍 [DEBUG] 執行搜尋 query: {query}")
            
            results = self.gmail_service.users().messages().list(
                userId='me', q=query, maxResults=20
            ).execute()
            
            messages = results.get('messages', [])
            self.log(f"  📊 [DEBUG] Gmail API 回傳 {len(messages)} 封符合的郵件")
            
        except Exception as e:
            self.log(f"檢查交付完成列表失敗: {e}")
            return []
            
        notices = []
        
        for msg_info in messages:
            try:
                msg_id = msg_info['id']
                
                # 檢查是否已處理
                is_processed = msg_id in self.processed_emails
                
                msg = self.gmail_service.users().messages().get(
                    userId='me', id=msg_id, format='full'
                ).execute()

                # DEBUG: 打印主旨
                headers = msg.get('payload', {}).get('headers', [])
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), "No Subject")
                self.log(f"  📨 [DEBUG] 檢查信件 [{msg_id}] 主旨: {subject}")
                
                info = self._parse_review_email(msg, parse_files=True)
                
                # 只要析出案號即可，不一定要有檔案連結 (因為是通知信)
                if info and info.case_number: 
                    info.message_id = msg_id
                    info.status = "可下載"
                    
                    if is_processed:
                        info.status = "已處理"
                        notices.append(info)
                        continue

                    notices.append(info)
                    
                    # (選擇性) 標記為已讀
                    try:
                        self.gmail_service.users().messages().modify(
                            userId='me', id=msg_id,
                            body={'removeLabelIds': ['UNREAD']}
                        ).execute()
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2577, exc_info=True)
                    
                    self.processed_emails.add(msg_id)
                    self._save_processed_emails() # 立即儲存
                else:
                    self.log(f"  ⚠️ 信件 [{msg_id}] 解析失敗或無案號")
            
            except Exception as e:
                self.log(f"  ⚠️ 處理信件 {msg_id if 'msg_id' in locals() else 'unknown'} 失敗: {e}")
                # 繼續處理下一封
                continue
        
        if notices:
            self.log(f"發現 {len(notices)} 封交付完成通知")
        
        return notices
    
    def _parse_review_email(self, msg: Dict, parse_files: bool = False) -> Optional[FileReviewInfo]:
        """解析閱卷通知信"""
        try:
            payload = msg.get('payload', {})
            body = self._get_email_body(payload)
            headers = payload.get('headers', [])
            subject = next((h.get('value', '') for h in headers if h.get('name') == 'Subject'), '')
            body = body or ""
            
            if not body and not subject:
                return None
            
            info = FileReviewInfo()
            
            # 解析欄位
            court_match = re.search(r'對象法院\s*([^\n\r]+)', body)
            if court_match:
                info.court = court_match.group(1).strip()

            self._apply_case_identifiers(info, f"{body} {subject}")
            
            client_match = re.search(r'當事人\s*([^\n\r]+)', body)
            if client_match:
                info.client_name = client_match.group(1).strip()
                
            amount_match = re.search(r'應繳金額：\s*(\d+)', body)
            if amount_match:
                info.payment_amount = int(amount_match.group(1))
                
            deadline_match = re.search(r'下載期限：\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})', body)
            if deadline_match:
                info.deadline = deadline_match.group(1)
                info.download_deadline = info.deadline
                
            # 嘗試解析繳費期限 (通常與 下載期限/繳費單期限 類似，這邊先共用邏輯)
            payment_deadline_match = re.search(r'繳費期限：\s*(\d{4}[-/]\d{1,2}[-/]\d{1,2})', body)
            if payment_deadline_match:
                info.payment_deadline = payment_deadline_match.group(1)
            elif info.deadline:
                # Fallback: 如果只有一個期限，假設它也是繳費期限
                info.payment_deadline = info.deadline
            
            if parse_files:
                # 交付完成通知通常沒有附件，而是要去網站下載
                # 這裡保留彈性，若未來有連結可在此解析
                pass
                
            return info
            
        except Exception as e:
            self.log(f"解析郵件失敗: {e}")
            return None
    
    def _get_email_body(self, payload: Dict) -> str:
        """取得郵件內文 (遞迴處理 multipart)"""
        try:
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
                    # 策略: 先找 plain，沒找到再找 html (但這裡我們簡單化，先回傳找到的第一個非空結果)
                    # 更好的策略可能是: 收集所有結果，優先選 plain
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
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2684, exc_info=True)
                
                return None

            # 開始遞迴解析
            text = extract_text(payload)
            if text:
                self.log(f"  [DEBUG] 成功取得內文 ({len(text)} chars)")
                return text
            
            self.log(f"  ⚠️ [DEBUG] 無法取得有效內文")
            return ""
            
        except Exception as e:
            self.log(f"  ⚠️ [DEBUG] 取得內文發生錯誤: {e}")
            return ""
            
    def _extract_attachments_recursive(self, part: dict) -> List[dict]:
        """遞迴提取所有附件資訊 (支援巢狀 multipart 結構)"""
        attachments = []
        
        # 檢查當前 part 是否為附件
        filename = part.get('filename')
        attachment_id = part.get('body', {}).get('attachmentId')
        
        if filename and attachment_id:
            attachments.append({
                'filename': filename,
                'attachmentId': attachment_id,
                'mimeType': part.get('mimeType', '')
            })
        
        # 遞迴處理子 parts
        sub_parts = part.get('parts', [])
        for sub_part in sub_parts:
            attachments.extend(self._extract_attachments_recursive(sub_part))
        
        return attachments

    def _download_attachments(self, msg_id: str, info: FileReviewInfo):
        """下載郵件附件 (如繳費單) - 支援巢狀 multipart 結構"""
        try:
            msg = self.gmail_service.users().messages().get(
                userId='me', id=msg_id
            ).execute()
            
            payload = msg.get('payload', {})
            attachments = self._extract_attachments_recursive(payload)
            
            self.log(f"  [DEBUG] 找到 {len(attachments)} 個附件")
            
            for att_info in attachments:
                att_id = att_info['attachmentId']
                filename = att_info['filename']
                
                att = self.gmail_service.users().messages().attachments().get(
                    userId='me', messageId=msg_id, id=att_id
                ).execute()
                
                data = att['data']
                file_data = base64.urlsafe_b64decode(data.encode('UTF-8'))
                
                # 儲存到暫存資料夾
                date_str = datetime.now().strftime("%Y%m%d")
                save_dir = os.path.join(self.download_folder, date_str, "附件")
                os.makedirs(save_dir, exist_ok=True)
                
                file_path = os.path.join(save_dir, filename)
                with open(file_path, 'wb') as f:
                    f.write(file_data)
                
                self.log(f"  📎 已下載附件: {filename}")
                info.files.append(file_path)
                
                self.archive_to_case_folder(info, file_path)
                    
        except Exception as e:
            self.log(f"下載附件失敗: {e}")
            import traceback
            traceback.print_exc()

    def archive_to_case_folder(self, info: FileReviewInfo, file_path: str = None):
        """
        將檔案歸檔到案件資料夾
        
        Args:
            info: FileReviewInfo 物件
            file_path: 可選，指定單一檔案路徑。若未提供，則從當天下載資料夾歸檔所有檔案。
        """
        # DB 不可用時，改用資料夾掃描/快取降級歸檔（避免檔案卡在下載區）
        # 若仍找不到對應案件，則歸檔到下載資料夾內的 _待歸檔。
            
        try:
            folder_path = None
            if self.db and hasattr(self.db, "find_case"):
                try:
                    case = self.db.find_case(info.case_number)
                    if case and getattr(case, "folder_path", None):
                        folder_path = case.folder_path
                        if hasattr(self.db, 'translate_path_to_local'):
                            folder_path = self.db.translate_path_to_local(folder_path)
                except Exception:
                    folder_path = None

            if not folder_path:
                folder_path = self._resolve_case_folder(info)
            
            # 模糊搜尋「閱卷」資料夾
            target_dir = None
            if folder_path and os.path.exists(folder_path):
                for item in os.listdir(folder_path):
                    if '閱卷' in item and os.path.isdir(os.path.join(folder_path, item)):
                        target_dir = os.path.join(folder_path, item)
                        break
                        
            if not target_dir:
                if folder_path:
                    target_dir = os.path.join(folder_path, "06_閱卷資料")  # Fallback
                else:
                    # 完全找不到案件資料夾 → 先丟到下載資料夾內的待歸檔
                    target_dir = os.path.join(self.download_folder, "_待歸檔", info.case_number or "Unknown")
            
            # 加入日期子資料夾
            date_subfolder = datetime.now().strftime("%Y%m%d")
            target_dir = os.path.join(target_dir, date_subfolder)
            
            os.makedirs(target_dir, exist_ok=True)
            
            # 決定要歸檔的檔案
            files_to_archive = []
            
            if file_path:
                # 指定單一檔案
                if os.path.exists(file_path):
                    files_to_archive.append(file_path)
            else:
                # 從當天下載資料夾取得所有檔案
                today_folder = os.path.join(self.download_folder, datetime.now().strftime("%Y%m%d"))
                if os.path.exists(today_folder):
                    for f in os.listdir(today_folder):
                        if not f.endswith(('.json', '.tmp', '.crdownload')):
                            files_to_archive.append(os.path.join(today_folder, f))
            
            if not files_to_archive:
                self.log("  ⚠️ 無檔案可歸檔")
                return
            
            # 歸檔檔案
            archived_count = 0
            for src_path in files_to_archive:
                try:
                    filename = os.path.basename(src_path)
                    target_path = os.path.join(target_dir, filename)
                    
                    shutil.copy2(src_path, target_path)
                    archived_count += 1
                    self.log(f"  已歸檔: {filename}")
                except Exception as copy_e:
                    self.log(f"  ⚠️ 歸檔失敗 {filename}: {copy_e}")
            
            if archived_count > 0:
                self.log(f"  ✅ 共歸檔 {archived_count} 個檔案至 {target_dir}")
                
        except Exception as e:
            self.log(f"歸檔失敗: {e}")

    def login(self) -> bool:
        """登入閱卷系統"""
        self.sso = LawyerPortalSSO(
            username=self.username,
            password=self.password,
            download_folder=self.download_folder,
            headless=self.headless,
            log_callback=self.log_callback
        )
        
        if self.sso.login():
            self.driver = self.sso.driver
            self.logged_in = True
            return True
        
        return False
    
    def navigate_to_file_review(self) -> bool:
        """
        導航到閱卷系統 (優化極速版)
        
        流程：
        1. 點選「聲請閱卷及複製電子卷證」 (優先嘗試 mainFrame)
        2. 處理新視窗 (智慧等待)
        3. 點選「線上閱卷作業」 (智慧等待 + JS 點擊)
        """
        if not self.driver:
            return False
        
        try:
            # ★ 優化: 設定短暫等待 (3s) 以確保元素偵測穩定
            self.driver.implicitly_wait(3)
            
            self.log("導航到閱卷系統 (極速版)...")
            original_windows = set(self.driver.window_handles)
            
            # Helper: 快速點擊連結
            def quick_click_link():
                xpath = "//a[contains(text(), '聲請閱卷') or contains(text(), '電子卷證')]"
                try:
                    # 使用短等待
                    elem = WebDriverWait(self.driver, 1).until(
                        EC.presence_of_element_located((By.XPATH, xpath))
                    )
                    # JS 點擊最快
                    self.driver.execute_script("arguments[0].click();", elem)
                    return True
                except Exception:
                    return False

            # 1. 嘗試主頁面 & Frame (優先嘗試 mainFrame)
            self.driver.switch_to.default_content()
            clicked = False
            
            # A. 嘗試直接找 (Main Content)
            if quick_click_link():
                clicked = True
            else:
                # B. 嘗試 mainFrame (最常見的情況)
                try:
                    self.driver.switch_to.frame("mainFrame")
                    if quick_click_link():
                        clicked = True
                        self.log("  ✓ 在 mainFrame 找到並點擊連結")
                except Exception:
                    # C. 掃描其他 Frame (最後手段)
                    self.driver.switch_to.default_content()
                    frames = self.driver.find_elements(By.TAG_NAME, "frame") + self.driver.find_elements(By.TAG_NAME, "iframe")
                    for f in frames:
                        try:
                            self.driver.switch_to.frame(f)
                            if quick_click_link():
                                clicked = True
                                self.log("  ✓ 在 Frame 找到連結")
                                break
                            self.driver.switch_to.default_content()
                        except Exception:
                            self.driver.switch_to.default_content()
            
            if not clicked:
                self.log("  ⚠️ 找不到閱卷連結 (已搜尋所有路徑)")
                return False

            # 2. 等待新視窗開啟 (智慧等待)
            old_window = None
            try:
                WebDriverWait(self.driver, 5).until(EC.new_window_is_opened(original_windows))
                new_windows = set(self.driver.window_handles) - original_windows
                if new_windows:
                    new_window = new_windows.pop()
                    
                    # ★★★ 關鍵優化: 記錄舊視窗，稍後關閉 ★★★
                    # 避免之後切換到錯誤的視窗
                    for w in original_windows:
                        try:
                            old_window = w
                            break
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2948, exc_info=True)
                    
                    self.driver.switch_to.window(new_window)
                    self.log(f"  ✓ 切換到新視窗")
                    
                    # ★★★ 關閉舊的 Portal 視窗 ★★★
                    # 用戶建議: 避免切換回錯誤的視窗
                    if old_window:
                        try:
                            self.driver.switch_to.window(old_window)
                            self.driver.close()
                            self.driver.switch_to.window(new_window)
                            self.log(f"  ✓ 已關閉舊視窗 (Portal)")
                        except Exception as close_e:
                            self.log(f"  ⚠️ 關閉舊視窗失敗: {close_e}")
                            # 確保回到新視窗
                            try:
                                self.driver.switch_to.window(new_window)
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2967, exc_info=True)
                else:
                    self.log("  ⚠️ 未偵測到新視窗")
                    return False
            except TimeoutException:
                 self.log("  ⚠️ 等待新視窗逾時")
                 return False

            # ★★★ 新增: 404/網路錯誤偵測與自動重新整理 ★★★
            # ola.judicial.gov.tw 容易斷線或 404，需要自動重試
            max_refresh_retries = 5
            for refresh_attempt in range(max_refresh_retries):
                try:
                    # 等待頁面載入 (極速版:減少等待)
                    WebDriverWait(self.driver, 3).until(
                        lambda d: d.execute_script("return document.readyState") == "complete"
                    )
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2985, exc_info=True)
                
                # 檢查是否為 404 或網路錯誤
                try:
                    page_source = self.driver.page_source.lower()
                    page_title = self.driver.title.lower() if self.driver.title else ""
                    current_url = self.driver.current_url.lower() if self.driver.current_url else ""
                    
                    # 常見的錯誤特徵
                    is_error_page = any([
                        "404" in page_title,
                        "not found" in page_title,
                        "無法連線" in page_source,
                        "連線失敗" in page_source,
                        "err_" in page_source,
                        "this site can't be reached" in page_source,
                        "this page isn't working" in page_source,
                        "neterror" in page_source,
                        "aw, snap!" in page_source,
                        len(page_source) < 500 and ("error" in page_source or "404" in page_source),
                    ])
                    
                    if is_error_page:
                        if refresh_attempt < max_refresh_retries - 1:
                            self.log(f"  ⚠️ 偵測到錯誤頁面 (第 {refresh_attempt + 1}/{max_refresh_retries} 次)，自動重新整理...")
                            time.sleep(1)  # 快速重試
                            self.driver.refresh()
                            time.sleep(2)  # 等待重新載入
                            continue
                        else:
                            self.log(f"  ❌ 重試 {max_refresh_retries} 次後仍為錯誤頁面")
                            return False
                    else:
                        # 頁面正常
                        if refresh_attempt > 0:
                            self.log(f"  ✓ 重新整理後頁面正常載入")
                        break
                        
                except Exception as chk_e:
                    self.log(f"  ⚠️ 檢查頁面狀態失敗: {chk_e}")
                    break
            
            # 3. 等待新視窗頁面載入 (document.readyState) - 備用等待
            try:
                WebDriverWait(self.driver, 2).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
            except Exception:
                self.log("  ⚠️ 頁面載入等待逾時 (繼續嘗試)")

            # 4. 點擊「線上閱卷作業」展開選單
            # Helper: 快速點選單
            def click_menu_item(text):
                # 支援 span.menu-text 及其父元素 a
                xpath = f"//span[contains(@class, 'menu-text') and contains(text(), '{text}')]/parent::a | //a[contains(text(), '{text}')]"
                try:
                    elem = WebDriverWait(self.driver, 2).until(EC.element_to_be_clickable((By.XPATH, xpath)))
                    self.driver.execute_script("arguments[0].click();", elem)
                    return True
                except Exception:
                    return False

            # 在新視窗中尋找選單
            found_menu = False
            
            # A. 嘗試直接點擊
            if click_menu_item("線上閱卷作業"):
                found_menu = True
            else:
                # B. 嘗試 Frame (新視窗通常也有 Frame)
                try:
                    # 快速掃描 Frame
                    frames = self.driver.find_elements(By.TAG_NAME, "frame") + self.driver.find_elements(By.TAG_NAME, "iframe")
                    for f in frames:
                        self.driver.switch_to.default_content()
                        try:
                            self.driver.switch_to.frame(f)
                            if click_menu_item("線上閱卷作業"):
                                found_menu = True
                                self.log("  ✓ 在新視窗 Frame 中點擊選單")
                                break
                            
                            # 巢狀 Frame 支援
                            nested = self.driver.find_elements(By.TAG_NAME, "frame")
                            for nf in nested:
                                self.driver.switch_to.frame(nf)
                                if click_menu_item("線上閱卷作業"):
                                    found_menu = True
                                    break
                                self.driver.switch_to.parent_frame()
                            if found_menu: break
                            
                        except Exception:
                            continue
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3080, exc_info=True)

            if not found_menu:
                self.log("  ⚠️ 在新視窗中找不到「線上閱卷作業」選單，嘗試 JS 展開...")
                # Fallback: use JS to click all menu items matching the text
                try:
                    self.driver.switch_to.default_content()
                    # Try all frames
                    for _fr in [None] + self.driver.find_elements(By.TAG_NAME, "frame") + self.driver.find_elements(By.TAG_NAME, "iframe"):
                        try:
                            if _fr is not None:
                                self.driver.switch_to.default_content()
                                self.driver.switch_to.frame(_fr)
                            js_clicked = self.driver.execute_script("""
                                var links = document.querySelectorAll('a');
                                for (var i = 0; i < links.length; i++) {
                                    if ((links[i].textContent || '').indexOf('線上閱卷作業') >= 0) {
                                        links[i].click();
                                        return true;
                                    }
                                }
                                return false;
                            """)
                            if js_clicked:
                                found_menu = True
                                self.log("  ✓ JS fallback: 選單已展開")
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

            if not found_menu:
                self.log("  ⚠️ 所有方法均無法找到「線上閱卷作業」選單")
                return False

            # 5. 等待子選單出現
            time.sleep(1)  # give menu animation time
            submenu_found = False
            try:
                WebDriverWait(self.driver, 5).until(
                    EC.presence_of_element_located((By.XPATH, "//a[contains(text(), '列表式查看') or contains(text(), '閱卷聲請登錄')]"))
                )
                self.log("  ✓ 選單展開成功")
                submenu_found = True
            except Exception:
                # Try JS fallback to find submenu in all frames
                try:
                    self.driver.switch_to.default_content()
                    for _fr in [None] + self.driver.find_elements(By.TAG_NAME, "frame") + self.driver.find_elements(By.TAG_NAME, "iframe"):
                        try:
                            if _fr is not None:
                                self.driver.switch_to.default_content()
                                self.driver.switch_to.frame(_fr)
                            has_submenu = self.driver.execute_script("""
                                var links = document.querySelectorAll('a');
                                for (var i = 0; i < links.length; i++) {
                                    var t = links[i].textContent || '';
                                    if (t.indexOf('列表式查看') >= 0 || t.indexOf('閱卷聲請登錄') >= 0) return true;
                                }
                                return false;
                            """)
                            if has_submenu:
                                submenu_found = True
                                self.log("  ✓ JS fallback: 子選單已找到")
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

            if not submenu_found:
                self.log("  ⚠️ 子選單展開失敗，無法進入列表頁")
                return False

            return True

        except Exception as e:
            self.log(f"  ⚠️ 導航失敗: {e}")
            import traceback
            traceback.print_exc()
            return False
        
        finally:
            # ★ 恢復正常的隱式等待
            self.driver.implicitly_wait(10)
    
    def _is_popup_open(self):
        """檢查彈窗是否開啟 (呼叫多層級檢查)"""
        return self._is_popup_open_all_frames()

    def _is_popup_open_all_frames(self):
        """檢查所有 Frame 層級是否有開啟的 Colorbox"""
        cbox_visible = False
        
        # 保存當前 Frame Context
        # 但因為我們不知道現在在哪，所以只能盡量恢復
        # 這裡假設呼叫後會需要回到 v1 (因為通常是在 v1 操作)
        
        try:
            # (A) 檢查 Root
            self.driver.switch_to.default_content()
            try:
                cbox = self.driver.find_elements(By.ID, "colorbox")
                if cbox and any(c.is_displayed() and c.size['width'] > 0 for c in cbox):
                    # self.log("  ✓ 在 Root 偵測到彈窗") # 減少 Log
                    return True
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3128, exc_info=True)
            
            # (B) 檢查 main-content
            try:
                main_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']")
                self.driver.switch_to.frame(main_iframe)
                cbox = self.driver.find_elements(By.ID, "colorbox")
                if cbox and any(c.is_displayed() and c.size['width'] > 0 for c in cbox):
                    # self.log("  ✓ 在 main-content 偵測到彈窗")
                    return True
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3139, exc_info=True)
            
            # (C) 檢查 v1
            try:
                # 如果剛才在 main-content，可以直接找 v1
                # 保險起見，重切 main -> v1
                self.driver.switch_to.default_content()
                main_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']")
                self.driver.switch_to.frame(main_iframe)
                v1_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='v1'] | //iframe[@id='v1']")
                self.driver.switch_to.frame(v1_iframe)
                
                cbox = self.driver.find_elements(By.ID, "colorbox")
                if cbox and any(c.is_displayed() and c.size['width'] > 0 for c in cbox):
                    # self.log("  ✓ 在 v1 偵測到彈窗")
                    return True
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3156, exc_info=True)
                
        finally:
            # 總是嘗試切回 v1 (因為這是在 loop 中使用的 check)
            try:
                self.driver.switch_to.default_content()
                main_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']")
                self.driver.switch_to.frame(main_iframe)
                v1_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='v1'] | //iframe[@id='v1']")
                self.driver.switch_to.frame(v1_iframe)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3167, exc_info=True)
                
        return False

    def _switch_to_review_list_v1(self) -> bool:
        """切回閱卷列表頁的 main-content -> v1 frame。"""
        try:
            self.driver.switch_to.default_content()
            main_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']")
            self.driver.switch_to.frame(main_iframe)
            v1_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='v1'] | //iframe[@id='v1']")
            self.driver.switch_to.frame(v1_iframe)
            return True
        except Exception:
            return False

    @staticmethod
    def _normalize_case_keyword(text: str) -> str:
        """案號比對正規化（移除常見格式字與符號）。"""
        s = str(text or "")
        s = re.sub(r"[年度字第號\s\.\-_/／\\]+", "", s)
        return s.strip().lower()

    @classmethod
    def _normalize_case_keyword_loose(cls, text: str) -> str:
        """較寬鬆的案號正規化：額外消除數字段前導零。"""
        base = cls._normalize_case_keyword(text)
        if not base:
            return ""

        def _strip_leading_zeros(match):
            token = match.group(0)
            try:
                return str(int(token))
            except Exception:
                return token.lstrip("0") or "0"

        return re.sub(r"\d+", _strip_leading_zeros, base)

    def _matches_target_case_number(self, target_case_number: str, *probes: str) -> bool:
        """比對目標案號與候選值，容忍符號與零補位差異。"""
        target_strict = self._normalize_case_keyword(target_case_number)
        if not target_strict:
            return False
        target_loose = self._normalize_case_keyword_loose(target_case_number)

        for probe in probes:
            probe_text = str(probe or "")
            if not probe_text:
                continue
            probe_strict = self._normalize_case_keyword(probe_text)
            if probe_strict and (target_strict in probe_strict or probe_strict in target_strict):
                return True
            probe_loose = self._normalize_case_keyword_loose(probe_text)
            if target_loose and probe_loose and (target_loose in probe_loose or probe_loose in target_loose):
                return True
        return False

    def _open_review_list_v1(self) -> bool:
        """
        進入「列表式查看」，並切到 main-content -> v1。
        只做導頁/切 frame，不觸發下載。
        """
        if not self.driver:
            return False
        try:
            self.driver.switch_to.default_content()
            list_view_btn = None
            selectors = [
                "//a[contains(normalize-space(.), '列表式查看')]",
                "//a[contains(., '列表式查看')]",
                "//li//a[contains(., '列表式查看')]",
            ]
            for selector in selectors:
                try:
                    cand = self.driver.find_element(By.XPATH, selector)
                    if cand and cand.is_displayed():
                        list_view_btn = cand
                        break
                except Exception:
                    continue

            if list_view_btn is not None:
                try:
                    ActionChains(self.driver).move_to_element(list_view_btn).click().perform()
                except Exception:
                    try:
                        self.driver.execute_script("arguments[0].click();", list_view_btn)
                    except Exception:
                        list_view_btn.click()
                time.sleep(1)

            self.driver.switch_to.default_content()
            main_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']")
            self.driver.switch_to.frame(main_iframe)
            try:
                v1_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='v1'] | //iframe[@id='v1']")
                self.driver.switch_to.frame(v1_iframe)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3266, exc_info=True)

            return True
        except Exception as e:
            self.log(f"  ⚠️ 無法切到列表式查看頁面: {e}")
            return False

    def probe_downloadable_from_portal(self, target_case_number: str = None, max_rows: int = 300) -> Dict[str, Any]:
        """
        只探測入口列表可下載狀態（不下載、不改資料）。
        回傳每列的可下載/待繳費/其他狀態，避免只靠 Gmail 主旨推測。
        """
        out: Dict[str, Any] = {
            "success": False,
            "count": 0,
            "downloadable_count": 0,
            "pending_payment_count": 0,
            "items": [],
        }
        try:
            if not self.logged_in:
                if not self.login():
                    out["error"] = "sso_login_failed"
                    return out

            if not self.navigate_to_file_review():
                out["error"] = "navigate_failed"
                return out

            if not self._open_review_list_v1():
                out["error"] = "list_view_unavailable"
                return out

            # Post-navigation verification: confirm we're on the list page
            _page_check = self.driver.execute_script("""
                var body = (document.body ? document.body.innerText : '') || '';
                var hasList = body.indexOf('聲請登錄清單') >= 0 || body.indexOf('序次') >= 0
                           || body.indexOf('聲請時間') >= 0 || body.indexOf('對象法院') >= 0;
                var trCount = document.querySelectorAll('tr#trdata, table#tablecontext tbody tr').length;
                return {has_list_markers: hasList, tr_count: trCount};
            """) or {}
            if not _page_check.get("has_list_markers") and _page_check.get("tr_count", 0) == 0:
                self.log("  ⚠️ 列表頁驗證失敗：頁面無列表特徵 (markers=%s, rows=%s)" % (
                    _page_check.get("has_list_markers"), _page_check.get("tr_count")))
                out["error"] = "list_page_verification_failed"
                return out

            rows_data = self.driver.execute_script(
                """
                function hasOnlineDownload(row) {
                  const inputSel = "input[type='button'][title='線上下載'],input[type='button'][value='線上下載'],input[name='btn_pay']";
                  if (row.querySelector(inputSel)) return true;
                  const nodes = Array.from(row.querySelectorAll("button,a,input[type='button'],input[type='submit']"));
                  return nodes.some((n) => {
                    const t = ((n.innerText || n.value || n.title || '') + '').replace(/\\s+/g, '');
                    return t.indexOf('線上下載') >= 0;
                  });
                }

                function getRowJson(row) {
                  try {
                    if (row.dataset && row.dataset.json) {
                      try { return JSON.parse(row.dataset.json); } catch (e) {}
                    }
                  } catch (e) {}
                  try {
                    if (typeof window.$ !== 'undefined') {
                      const d = window.$(row).data('json');
                      if (d && typeof d === 'object') return d;
                    }
                  } catch (e) {}
                  return {};
                }

                const rows = Array.from(document.querySelectorAll("tr#trdata, table#tablecontext tbody tr"));
                return rows.map((row) => {
                  const d = getRowJson(row) || {};
                  return {
                    row_text: ((row.innerText || '') + '').trim(),
                    has_online_download: hasOnlineDownload(row),
                    row_json: d,
                  };
                });
                """
            ) or []

            norm_target = self._normalize_case_keyword(target_case_number or "")
            items: List[Dict[str, Any]] = []
            downloadable_count = 0
            pending_payment_count = 0

            for row_data in rows_data:
                try:
                    if not isinstance(row_data, dict):
                        continue
                    row_json = row_data.get("row_json") if isinstance(row_data.get("row_json"), dict) else {}
                    row_text = str(row_data.get("row_text") or "").strip()

                    yyidno = str(row_json.get("yyidno") or "").strip()
                    showyyidno = str(row_json.get("showyyidno") or "").strip()
                    party = str(row_json.get("clnm") or "").strip()
                    court = str(row_json.get("crtid") or "").strip()
                    deadline = str(
                        row_json.get("downlimit")
                        or row_json.get("dlmdate")
                        or row_json.get("payedate")
                        or ""
                    ).strip()
                    pay_deadline = str(
                        row_json.get("paylimitdt")
                        or row_json.get("limitdt")
                        or ""
                    ).strip()

                    probe_text = "\n".join([yyidno, showyyidno, party, row_text])
                    if norm_target and norm_target not in self._normalize_case_keyword(probe_text):
                        continue

                    pending_payment = self._is_pending_payment_row(row_json, row_text=row_text)
                    has_download = bool(row_data.get("has_online_download"))

                    status = "other"
                    if has_download:
                        status = "downloadable"
                        downloadable_count += 1
                    elif pending_payment:
                        status = "pending_payment"
                        pending_payment_count += 1

                    paystatus = str(row_json.get("paystatus") or "").strip()
                    p_status = str(row_json.get("p_status") or "").strip().upper()
                    status_code = str(row_json.get("status") or "").strip()
                    status_name = str(row_json.get("statusnm") or "").strip()
                    result_text = str(row_json.get("result") or "").strip()
                    payment_flag = str(row_json.get("payment") or "").strip().upper()
                    payid = str(row_json.get("p_payid") or "").strip()
                    rowid = str(row_json.get("rowid") or "").strip()
                    applydt = str(row_json.get("applydt") or "").strip()
                    fee = str(row_json.get("procfee") or row_json.get("fee") or "").strip()
                    item = {
                        "status": status,
                        "court": court,
                        "case_number": yyidno,
                        "court_case_no": showyyidno,
                        "party": party,
                        "deadline": deadline,
                        "pay_deadline": pay_deadline,
                        "paystatus": paystatus,
                        "p_status": p_status,
                        "status_code": status_code,
                        "status_name": status_name,
                        "result_text": result_text,
                        "payment_flag": payment_flag,
                        "payid": payid,
                        "rowid": rowid,
                        "applydt": applydt,
                        "fee": fee,
                    }
                    items.append(item)
                    if len(items) >= max_rows:
                        break
                except Exception:
                    continue

            out.update(
                {
                    "success": True,
                    "count": len(items),
                    "downloadable_count": downloadable_count,
                    "pending_payment_count": pending_payment_count,
                    "items": items,
                }
            )
            return out
        except Exception as e:
            out["error"] = f"{type(e).__name__}: {e}"
            return out
        finally:
            try:
                self.driver.switch_to.default_content()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3433, exc_info=True)

    def _extract_row_json(self, row_elem=None, ref_elem=None) -> dict:
        """從列表列元素擷取 data-json。"""
        row_json = {}
        row = row_elem
        try:
            if row is None and ref_elem is not None:
                row = ref_elem.find_element(By.XPATH, "./ancestor::tr")
        except Exception:
            row = None

        if row is not None:
            try:
                json_data_str = row.get_attribute("data-json")
                if json_data_str:
                    if isinstance(json_data_str, dict):
                        row_json = dict(json_data_str)
                    else:
                        row_json = json.loads(json_data_str)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3454, exc_info=True)

        if not row_json:
            try:
                row_json = self.driver.execute_script(
                    """
                    var row = arguments[0];
                    var ref = arguments[1];
                    try {
                      if (typeof $ !== 'undefined') {
                        if (row) {
                          var v = $(row).data('json');
                          if (v) return v;
                        }
                        if (ref) {
                          var v2 = $(ref).closest('tr').data('json');
                          if (v2) return v2;
                        }
                      }
                    } catch(e) {}
                    return null;
                    """,
                    row,
                    ref_elem,
                ) or {}
            except Exception:
                row_json = {}

        if not isinstance(row_json, dict):
            return {}
        return row_json

    @staticmethod
    def _is_pending_payment_row(row_json: dict, row_text: str = "") -> bool:
        """判斷是否為待繳費列。"""
        if not isinstance(row_json, dict):
            row_json = {}
        paystatus = str(row_json.get("paystatus") or "").strip()
        payment = str(row_json.get("payment") or "").strip().upper()
        status = str(row_json.get("status") or "").strip()
        statusnm = str(row_json.get("statusnm") or "").strip()
        result_text = f"{row_json.get('result') or ''}\n{row_text or ''}"

        if "待繳費" in result_text:
            return True
        if paystatus == "2":
            return True
        if payment == "N" and status in {"3", "6"} and ("同意" in statusnm or "繳費" in result_text):
            return True
        return False

    @staticmethod
    def _is_payment_overdue(row_json: dict, max_days: int = 14) -> bool:
        """判斷繳費是否已逾期超過 max_days 天。"""
        if not isinstance(row_json, dict):
            return False
        dl_raw = str(row_json.get("paylimitdt") or row_json.get("limitdt") or "").strip()
        if not dl_raw or len(dl_raw) != 7:
            return False
        try:
            from datetime import datetime
            y = int(dl_raw[:3]) + 1911
            m = int(dl_raw[3:5])
            d = int(dl_raw[5:7])
            dl_date = datetime(year=y, month=m, day=d).date()
            days_overdue = (datetime.now().date() - dl_date).days
            return days_overdue > max_days
        except Exception:
            return False

    def _find_pending_payment_element(self, row_elem):
        """在列表列中尋找可點擊的「待繳費」元素。"""
        selectors = [
            ".//a[contains(normalize-space(.), '待繳費')]",
            ".//a[contains(., '繳費單')]",
            ".//a[contains(., '繳費')]",
            ".//input[@type='button' and (contains(@value, '待繳費') or contains(@title, '待繳費') or contains(@value, '繳費'))]",
            ".//button[contains(., '待繳費') or contains(., '繳費')]",
            ".//a[contains(translate(@onclick,'PAY','pay'), 'pay') or contains(@onclick, '繳費')]",
        ]
        for sel in selectors:
            try:
                els = row_elem.find_elements(By.XPATH, sel)
                for el in els:
                    try:
                        if el.is_displayed():
                            return el
                    except Exception:
                        continue
            except Exception:
                continue
        return None

    @staticmethod
    def _roc_compact_date_to_iso(value: str) -> str:
        """民國緊湊日期（如 1150312）轉西元 YYYY-MM-DD。"""
        s = re.sub(r"\D", "", str(value or ""))
        if len(s) != 7:
            return str(value or "")
        try:
            y = int(s[:3]) + 1911
            m = int(s[3:5])
            d = int(s[5:7])
            return f"{y:04d}-{m:02d}-{d:02d}"
        except Exception:
            return str(value or "")

    def _collect_new_files_from_folder(self, folder: str, existing_file_mtimes: dict, timeout_sec: int = 10) -> List[str]:
        """等待並收集新下載檔名（僅檔名，不含路徑）。"""
        found = []
        if not os.path.exists(folder):
            return found
        start = time.time()
        while time.time() - start < timeout_sec:
            try:
                for filename in os.listdir(folder):
                    if filename.endswith(('.json', '.tmp', '.crdownload')):
                        continue
                    fpath = os.path.join(folder, filename)
                    if not os.path.isfile(fpath):
                        continue
                    current_mtime = os.path.getmtime(fpath)
                    old_mtime = existing_file_mtimes.get(filename, 0)
                    if filename not in existing_file_mtimes or current_mtime > old_mtime:
                        found.append(filename)
                        existing_file_mtimes[filename] = current_mtime
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3581, exc_info=True)
            time.sleep(0.8)

        out = []
        seen = set()
        for x in found:
            if x in seen:
                continue
            seen.add(x)
            out.append(x)
        return out

    def _select_best_pending_row_for_case(self, base_row_elem, base_row_json: dict):
        """
        在同案號多列中，挑出最適合下載繳費單的列。
        優先順序：p_status=Y > paystatus=2 > 較新 applydt。
        為避免 Selenium 在長列表/異常 DOM 卡住，這裡只用 row_json 快速評分，不做深度元素掃描。
        """
        best_row = base_row_elem
        best_json = base_row_json if isinstance(base_row_json, dict) else {}
        target_case = str((best_json or {}).get("yyidno") or (best_json or {}).get("showyyidno") or "").strip()
        target_rowid = str((best_json or {}).get("rowid") or "").strip()
        target_payid = str((best_json or {}).get("p_payid") or "").strip()

        def _score(row_json_inner: dict, row_text_inner: str):
            s = 0
            p_status = str((row_json_inner or {}).get("p_status") or "").strip().upper()
            paystatus = str((row_json_inner or {}).get("paystatus") or "").strip()
            if p_status == "Y":
                s += 8
            if paystatus == "2":
                s += 2
            if "待繳費" in (row_text_inner or ""):
                s += 1
            applydt = re.sub(r"\D", "", str((row_json_inner or {}).get("applydt") or ""))
            rowid = re.sub(r"\D", "", str((row_json_inner or {}).get("rowid") or ""))
            return (s, applydt, rowid)

        try:
            rows = self.driver.find_elements(By.XPATH, "//tr[@id='trdata'] | //table[@id='tablecontext']//tbody//tr")
        except Exception:
            rows = []

        best_score = None
        deadline = time.time() + 8.0
        for i, row in enumerate(rows):
            if i >= 40 or time.time() > deadline:
                break
            try:
                rj = self._extract_row_json(row, None) or {}
                rt = row.text or ""
                if not self._is_pending_payment_row(rj, row_text=rt):
                    continue

                row_case = str(rj.get("yyidno") or rj.get("showyyidno") or "").strip()
                row_rowid = str(rj.get("rowid") or "").strip()
                row_payid = str(rj.get("p_payid") or "").strip()

                same_case = False
                if target_case and row_case:
                    same_case = (row_case == target_case)
                if not same_case and target_rowid and row_rowid:
                    same_case = (row_rowid == target_rowid)
                if not same_case and target_payid and row_payid:
                    same_case = (row_payid == target_payid)
                if not same_case:
                    continue

                cur_score = _score(rj, rt)
                if best_score is None or cur_score > best_score:
                    best_score = cur_score
                    best_row = row
                    best_json = rj
            except Exception:
                continue

        return best_row, (best_json if isinstance(best_json, dict) else {})

    def _download_payment_slip_from_row(self, row_elem, row_json: dict, today_folder: str, existing_file_mtimes: dict) -> List[str]:
        """
        點擊「待繳費」並嘗試下載繳費單，回傳新增檔名列表。
        """
        self.log("  [待繳費] 開始處理繳費單下載流程")
        new_files: List[str] = []
        main_window = None
        before_handles = set()
        try:
            before_handles = set(self.driver.window_handles)
            main_window = self.driver.current_window_handle
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3671, exc_info=True)

        opened = False
        # 優先走「待繳費」超連結點擊，符合實際頁面操作語意；失敗再 fallback doViewFHD2E。
        try:
            pending_el = self._find_pending_payment_element(row_elem)
            if pending_el is not None:
                try:
                    self.driver.execute_script("arguments[0].scrollIntoView({block:'center'});", pending_el)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3681, exc_info=True)
                clicked = False
                try:
                    ActionChains(self.driver).move_to_element(pending_el).pause(0.05).click(pending_el).perform()
                    clicked = True
                except Exception:
                    try:
                        self.driver.execute_script("arguments[0].click();", pending_el)
                        clicked = True
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3691, exc_info=True)
                if clicked:
                    self.log("  ✓ [待繳費] 已點擊「待繳費」超連結")
                    time.sleep(1.1)
                    try:
                        now_handles = set(self.driver.window_handles)
                    except Exception:
                        now_handles = set()
                    if (now_handles - before_handles) or self._is_popup_open_all_frames() or self._is_popup_open():
                        opened = True
                        self.log("  ✓ [待繳費] 超連結已成功觸發視窗/彈窗")
            else:
                clicked_hint = self.driver.execute_script(
                    """
                    var row = arguments[0];
                    if (!row) return '';
                    function norm(t) { return String(t || '').replace(/\\s+/g, ''); }
                    // 只匹配短文字（<15字元）且包含「待繳費」的可互動元素，避免匹配描述文字
                    var nodes = Array.from(row.querySelectorAll('a,button,input[type="button"]'));
                    for (var i = 0; i < nodes.length; i++) {
                      var n = nodes[i];
                      var t = norm(n.innerText || n.value || n.title || n.getAttribute('aria-label') || '');
                      if (t.length > 15) continue;
                      if (t.indexOf('待繳費') < 0 && t.indexOf('繳費單') < 0) continue;
                      try { n.click(); return t || n.tagName; } catch (e) {}
                    }
                    // 次要：找 span/td 但必須是短文字且嚴格匹配「待繳費」
                    var nodes2 = Array.from(row.querySelectorAll('span,td,div,label'));
                    for (var i = 0; i < nodes2.length; i++) {
                      var n = nodes2[i];
                      var t = norm(n.innerText || '');
                      if (t.length > 10 || t.indexOf('待繳費') < 0) continue;
                      var target = n.closest('a,button,input[type="button"],[onclick]');
                      if (target) {
                        try { target.click(); return t || target.tagName; } catch (e) {}
                      }
                    }
                    return '';
                    """,
                    row_elem,
                ) or ""
                if clicked_hint:
                    self.log(f"  ✓ [待繳費] 已以備援定位點擊元素: {clicked_hint}")
                    time.sleep(1.1)
                    try:
                        now_handles = set(self.driver.window_handles)
                    except Exception:
                        now_handles = set()
                    if (now_handles - before_handles) or self._is_popup_open_all_frames() or self._is_popup_open():
                        opened = True
                        self.log("  ✓ [待繳費] 備援點擊已成功觸發視窗/彈窗")
        except Exception as e:
            self.log(f"  ⚠️ [待繳費] 點擊超連結失敗: {e}")

        opened_by = ""
        if opened:
            opened_by = "pending_link_click"
        try:
            if not opened:
                opened_by = self.driver.execute_script(
                    """
                    var json = arguments[0] || {};
                    var row = arguments[1] || null;
                    var target = row;

                    function same(a, b) { return String(a || '') === String(b || ''); }

                    try {
                      if (!target && typeof $ !== 'undefined') {
                        $('tr#trdata, table#tablecontext tbody tr').each(function(){
                          var d = $(this).data('json') || {};
                          if (
                            (json.p_payid && same(d.p_payid, json.p_payid)) ||
                            (json.rowid && same(d.rowid, json.rowid)) ||
                            (json.yyidno && same(d.yyidno, json.yyidno)) ||
                            (json.showyyidno && same(d.showyyidno, json.showyyidno))
                          ) {
                            target = this;
                            return false;
                          }
                        });
                      }
                    } catch(e) {}

                    if (typeof window.doViewFHD2E === 'function') {
                      try { window.doViewFHD2E(json); return 'doViewFHD2E'; } catch(e) {}
                    }

                    if (target && typeof $ !== 'undefined') {
                      try {
                        var clk = $(target).data('clk');
                        if (typeof clk === 'function') {
                          clk(json);
                          return 'row_clk';
                        }
                      } catch(e) {}
                    }

                    if (target) {
                      try { target.click(); return 'row_click'; } catch(e) {}
                    }
                    return '';
                    """,
                    row_json or {},
                    row_elem,
                ) or ""
            if opened_by:
                opened = True
                self.log(f"  ✓ [待繳費] 已觸發入口: {opened_by}")
        except Exception as e:
            self.log(f"  ⚠️ [待繳費] 觸發入口失敗: {e}")

        if not opened:
            self.log("  ⚠️ [待繳費] 未成功觸發任何繳費單入口")
            return []

        time.sleep(3.0)  # 等待 FHD2E dialog 完全載入

        # 路徑 A：同頁 Dialog -> IDX_* -> v1 -> 列印繳費單
        clicked_print = False
        try:
            self.driver.switch_to.default_content()
            # 記錄觸發前已存在的 IDX iframe names，以便找到新出現的
            pre_idx_names = set()
            try:
                for fr in self.driver.find_elements(By.XPATH, "//iframe[starts-with(@name,'IDX_')]"):
                    pre_idx_names.add(fr.get_attribute("name") or "")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3819, exc_info=True)

            # 等待新的 IDX frame 出現（含 FHD2E 的）
            idx_frames = []
            for _wait in range(8):
                all_idx = self.driver.find_elements(By.XPATH, "//iframe[starts-with(@name,'IDX_')]")
                # 優先找 FHD2E 的 iframe
                for fr in all_idx:
                    src = (fr.get_attribute("src") or "").upper()
                    if "FHD2E" in src:
                        idx_frames = [fr]
                        break
                if idx_frames:
                    break
                # 其次找新出現的 IDX iframe
                new_idx = [fr for fr in all_idx if (fr.get_attribute("name") or "") not in pre_idx_names]
                if new_idx:
                    idx_frames = new_idx
                    break
                # 最後 fallback 到所有 IDX
                if all_idx and _wait >= 4:
                    idx_frames = all_idx
                    break
                time.sleep(1)

            if idx_frames:
                target_idx = idx_frames[0]
                # 優先挑選 FHD2E（多元化繳費）對話框
                for fr in idx_frames:
                    src = (fr.get_attribute("src") or "").upper()
                    if "FHD2E01" in src:
                        target_idx = fr
                        break

                self.driver.switch_to.frame(target_idx)

                # 等待 v1 iframe 載入
                nested_v1 = None
                for _wait in range(5):
                    try:
                        nested_v1 = self.driver.find_element(By.XPATH, "//iframe[@name='v1' or @id='v1']")
                        break
                    except Exception:
                        time.sleep(0.5)

                if nested_v1:
                    self.driver.switch_to.frame(nested_v1)
                    time.sleep(1)  # 等 v1 內容載入

                print_btn_selectors = [
                    "//button[@title='列印繳費單']",
                    "//button[contains(normalize-space(.), '列印繳費單')]",
                    "//input[@type='button' and contains(@value, '列印繳費單')]",
                    "//button[contains(normalize-space(.), '列印')]",
                    "//a[contains(normalize-space(.), '列印繳費單')]",
                ]
                print_btn = None
                for sel in print_btn_selectors:
                    try:
                        cands = self.driver.find_elements(By.XPATH, sel)
                        # 先找 displayed 的，再找全部
                        vis = [x for x in cands if x.is_displayed()]
                        if vis:
                            print_btn = vis[0]
                            break
                        if cands:
                            print_btn = cands[0]
                            break
                    except Exception:
                        continue

                if print_btn is not None:
                    try:
                        self.driver.execute_script("arguments[0].click();", print_btn)
                    except Exception:
                        try:
                            print_btn.click()
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3897, exc_info=True)
                    clicked_print = True
                    self.log("  ✓ [待繳費] 已點擊「列印繳費單」")
                    time.sleep(2.0)  # 等 PDF 生成/下載觸發
                else:
                    # Debug: 檢查 v1 內容
                    try:
                        page_text = self.driver.execute_script("return (document.body.innerText||'').substring(0,200)") or ""
                        self.log(f"  [DEBUG] IDX v1 內容: {page_text[:150]}")
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3907, exc_info=True)
            else:
                self.log("  ⚠️ [待繳費] 未找到 IDX_* iframe")
        except Exception as e:
            self.log(f"  ⚠️ [待繳費] Dialog 內點擊列印失敗: {e}")

        # 路徑 B：新視窗（保底）
        try:
            new_handles = set(self.driver.window_handles) - before_handles
        except Exception:
            new_handles = set()

        if (not clicked_print) and new_handles:
            self.log(f"  偵測到待繳費新視窗: {len(new_handles)} 個")
            for wh in list(new_handles):
                try:
                    self.driver.switch_to.window(wh)
                    time.sleep(0.8)
                    clicked_hint = self.driver.execute_script(
                        """
                        var nodes = Array.from(document.querySelectorAll('a,button,input[type="button"],input[type="submit"]'));
                        for (var i = 0; i < nodes.length; i++) {
                          var n = nodes[i];
                          var t = ((n.innerText || n.value || n.title || '') + '').replace(/\\s+/g, '');
                          if (t.indexOf('列印繳費單') >= 0 || t.indexOf('列印') >= 0 || t.indexOf('下載') >= 0 || t.indexOf('繳費單') >= 0) {
                            try { n.click(); return t; } catch(e) {}
                          }
                        }
                        return '';
                        """
                    ) or ""
                    if clicked_hint:
                        clicked_print = True
                        self.log(f"  ✓ 已於待繳費視窗觸發下載/列印元素: {clicked_hint}")
                    time.sleep(1.2)
                except Exception as e:
                    self.log(f"  ⚠️ 處理待繳費新視窗失敗: {e}")
                finally:
                    try:
                        self.driver.close()
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3948, exc_info=True)

            if main_window:
                try:
                    self.driver.switch_to.window(main_window)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3954, exc_info=True)

        if not clicked_print:
            self.log("  ⚠️ [待繳費] 未找到「列印繳費單」按鈕")

        self._switch_to_review_list_v1()
        self.log("  [待繳費] 已回到列表 frame，開始掃描新下載檔案")
        new_files = self._collect_new_files_from_folder(today_folder, existing_file_mtimes, timeout_sec=15)
        self.log(f"  [待繳費] 本輪偵測到新檔案: {len(new_files)}")
        return new_files

    def _download_payment_slip_direct(self, row_elem, row_json: dict, today_folder: str, existing_file_mtimes: dict) -> List[str]:
        """
        直接使用 doViewFHD2E 開啟繳費單 dialog 並下載。
        比 _download_payment_slip_from_row 更簡潔，避免 backup locator 點錯元素。
        """
        self.log("  [繳費單] 直接觸發 doViewFHD2E...")
        new_files: List[str] = []

        # 1. 關閉任何殘留的 IDX dialog
        try:
            self.driver.switch_to.default_content()
            self.driver.execute_script("""
            var frames = document.querySelectorAll('iframe[name^="IDX_"]');
            for (var i = 0; i < frames.length; i++) {
                try { frames[i].parentElement.removeChild(frames[i]); } catch(e) {}
            }
            """)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3983, exc_info=True)

        # 2. 切回 v1 並呼叫 doViewFHD2E
        self._switch_to_review_list_v1()
        opened = self.driver.execute_script("""
        var json = arguments[0] || {};
        if (typeof window.doViewFHD2E === 'function') {
            try { window.doViewFHD2E(json); return 'ok'; } catch(e) { return 'error:'+e; }
        }
        return '';
        """, row_json or {}) or ""

        if not opened.startswith("ok"):
            self.log(f"  ⚠️ [繳費單] doViewFHD2E 觸發失敗: {opened}")
            return []

        self.log("  ✓ [繳費單] doViewFHD2E 已觸發")
        time.sleep(3.0)  # 等 dialog 載入

        # 3. 找 IDX_0 → v1 → 「列印繳費單」按鈕
        clicked_print = False
        try:
            self.driver.switch_to.default_content()
            for _wait in range(5):
                idx_frames = self.driver.find_elements(By.XPATH, "//iframe[starts-with(@name,'IDX_')]")
                if idx_frames:
                    break
                time.sleep(1)

            if not idx_frames:
                self.log("  ⚠️ [繳費單] 未找到 IDX frame")
            else:
                target_idx = idx_frames[-1]  # 用最新的 IDX frame
                for fr in idx_frames:
                    src = (fr.get_attribute("src") or "").upper()
                    if "FHD2E01" in src:
                        target_idx = fr
                        break

                self.driver.switch_to.frame(target_idx)
                # 等 v1 出現
                nested_v1 = None
                for _w in range(5):
                    try:
                        nested_v1 = self.driver.find_element(By.XPATH, "//iframe[@name='v1' or @id='v1']")
                        break
                    except Exception:
                        time.sleep(0.5)

                if nested_v1:
                    self.driver.switch_to.frame(nested_v1)
                    time.sleep(1)

                # 尋找列印按鈕
                for sel in [
                    "//button[@title='列印繳費單']",
                    "//button[contains(normalize-space(.), '列印繳費單')]",
                    "//input[@type='button' and contains(@value, '列印繳費單')]",
                    "//button[contains(normalize-space(.), '列印')]",
                    "//a[contains(normalize-space(.), '列印繳費單')]",
                ]:
                    try:
                        cands = self.driver.find_elements(By.XPATH, sel)
                        vis = [x for x in cands if x.is_displayed()]
                        btn = vis[0] if vis else (cands[0] if cands else None)
                        if btn:
                            self.driver.execute_script("arguments[0].click();", btn)
                            clicked_print = True
                            self.log("  ✓ [繳費單] 已點擊「列印繳費單」")
                            time.sleep(2.0)
                            break
                    except Exception:
                        continue

                if not clicked_print:
                    page_text = ""
                    try:
                        page_text = self.driver.execute_script("return (document.body.innerText||'').substring(0,200)") or ""
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4062, exc_info=True)
                    self.log(f"  ⚠️ [繳費單] 未找到列印按鈕。頁面內容: {page_text[:150]}")

        except Exception as e:
            self.log(f"  ⚠️ [繳費單] 列印按鈕搜尋失敗: {e}")

        # 4. 收集新檔案
        self._switch_to_review_list_v1()
        new_files = self._collect_new_files_from_folder(today_folder, existing_file_mtimes, timeout_sec=15)
        self.log(f"  [繳費單] 偵測到新檔案: {len(new_files)}")
        return new_files

    def download_all_payment_slips(self, max_days: int = 14) -> List[Dict[str, Any]]:
        """
        專門下載所有待繳費案件的繳費單 PDF。

        流程：
        1. 導航到列表頁面（FHD2C01，有 data-json 的下載列表）
        2. 找到所有待繳費列
        3. 依序點擊下載繳費單
        4. 回傳 [{case_info, pdf_path}, ...]
        """
        self.log("🔍 開始批次下載繳費單 PDF...")
        results: List[Dict[str, Any]] = []

        today_folder = os.path.join(
            self.download_folder, time.strftime("%Y%m%d")
        )
        os.makedirs(today_folder, exist_ok=True)

        # 先收集現有檔案的 mtime（用於偵測新檔案）
        def _snapshot_folder(folder: str) -> dict:
            snap = {}
            if os.path.isdir(folder):
                for fn in os.listdir(folder):
                    fp = os.path.join(folder, fn)
                    if os.path.isfile(fp) and not fn.endswith((".crdownload", ".tmp")):
                        snap[fn] = os.path.getmtime(fp)
            return snap

        # ========= 步驟 1: 切到列表頁 (下載列表 FHD2C) =========
        try:
            self.driver.switch_to.default_content()
            # 點擊側邊欄的列表式查看（會載入 FHD2B 或 FHD2C）
            # 我們需要的是有 data-json 的列表（FHD2C），所以用 check_and_download_available 的方式
            list_view_btn = None
            for selector in [
                "//a[contains(normalize-space(.), '列表式查看')]",
                "//a[contains(., '列表式查看')]",
            ]:
                try:
                    el = self.driver.find_element(By.XPATH, selector)
                    if el and el.is_displayed():
                        list_view_btn = el
                        break
                except Exception:
                    continue

            if list_view_btn:
                try:
                    ActionChains(self.driver).move_to_element(list_view_btn).click().perform()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", list_view_btn)
                self.log("  ✓ 點擊「列表式查看」")
                time.sleep(1.5)

            self.driver.switch_to.default_content()
            main_iframe = self.driver.find_element(
                By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']"
            )
            self.driver.switch_to.frame(main_iframe)
            time.sleep(1)
            try:
                v1_iframe = self.driver.find_element(
                    By.XPATH, "//iframe[@name='v1'] | //iframe[@id='v1']"
                )
                self.driver.switch_to.frame(v1_iframe)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4140, exc_info=True)
            time.sleep(2)

            # 滾動以確保所有內容載入
            try:
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(0.5)
                self.driver.execute_script("window.scrollTo(0, 0);")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4149, exc_info=True)
        except Exception as e:
            self.log(f"  ❌ 導航到列表頁失敗: {e}")
            return results

        # ========= 步驟 2: 收集所有待繳費列 =========
        rows_data = self.driver.execute_script("""
        function getRowJson(row) {
          var s = row.getAttribute('data-json');
          if (s) try { return JSON.parse(s); } catch(e) {}
          if (typeof $ !== 'undefined') try { return $(row).data('json') || {}; } catch(e) {}
          return {};
        }
        var rows = Array.from(document.querySelectorAll("tr#trdata, table#tablecontext tbody tr"));
        return rows.map(function(row, idx) {
          var d = getRowJson(row) || {};
          return {
            idx: idx,
            row_text: (row.innerText || '').trim(),
            row_json: d,
          };
        });
        """) or []

        pending_rows = []
        today_str = time.strftime("%Y-%m-%d")
        for rd in rows_data:
            rj = rd.get("row_json") if isinstance(rd.get("row_json"), dict) else {}
            rt = str(rd.get("row_text") or "")
            if self._is_pending_payment_row(rj, row_text=rt):
                # 檢查是否在期限內
                pay_deadline = str(rj.get("paylimitdt") or rj.get("limitdt") or "").strip()
                if pay_deadline and len(pay_deadline) == 7:
                    try:
                        y = int(pay_deadline[:3]) + 1911
                        m = int(pay_deadline[3:5])
                        d = int(pay_deadline[5:7])
                        deadline_iso = f"{y:04d}-{m:02d}-{d:02d}"
                        from datetime import datetime, timedelta
                        deadline_date = datetime.strptime(deadline_iso, "%Y-%m-%d").date()
                        today_date = datetime.now().date()
                        days_overdue = (today_date - deadline_date).days
                        if days_overdue > max_days:
                            # 已逾期超過 max_days 天，跳過
                            self.log(f"  ⏭️ 已逾期 {days_overdue} 天（超過 {max_days} 天），跳過: {rj.get('clnm') or ''}｜{rj.get('showyyidno') or rj.get('yyidno') or ''}")
                            continue
                        max_future = (today_date + timedelta(days=max_days)).strftime("%Y-%m-%d")
                        if deadline_iso > max_future:
                            continue  # 期限太遠（尚未到期），跳過
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4199, exc_info=True)

                # 檢查是否為免繳費案件（僅指定辯護案件免繳費）
                _show = rj.get("showyyidno") or rj.get("yyidno") or ""
                _party = rj.get("clnm") or ""
                if self._is_fee_exempt_case(court_case_no=_show, party=_party, yyidno=rj.get("yyidno") or ""):
                    self.log(f"  ⏭️ 免繳費案件（指定辯護）: {_party}｜{_show}")
                    continue

                # 檢查是否已處理過（registry 有記錄即跳過，避免重複按繳費）
                # 先精確匹配（正規化後的 key），再 fallback 相容查詢（處理舊格式 registry）
                reg_key = self._payment_registry_key(rj)
                existing = self.payment_registry.get(reg_key, {})
                if existing:
                    _existing_paths = existing.get("file_paths") or []
                    _label = rj.get('showyyidno') or rj.get('yyidno') or '-'
                    if _existing_paths and all(os.path.exists(fp) for fp in _existing_paths):
                        self.log(f"  ⏭️ 已有繳費單: {_label}")
                        results.append({
                            "case_number": rj.get("showyyidno") or rj.get("yyidno") or "",
                            "party": rj.get("clnm") or "",
                            "court": rj.get("crtid") or "",
                            "pdf_path": _existing_paths[0],
                            "already_existed": True,
                        })
                    else:
                        self.log(f"  ⏭️ 已處理過（registry 有記錄）: {_label}")
                    continue
                # fallback：相容查詢（舊 registry 格式用未正規化的 key）
                if self._is_payment_processed(rj):
                    _label = rj.get('showyyidno') or rj.get('yyidno') or '-'
                    self.log(f"  ⏭️ 已處理過（相容比對）: {_label}")
                    continue

                pending_rows.append(rd)

        # 限制最多處理筆數，避免耗時過長
        max_items = int(os.environ.get("MAGI_PAYMENT_SLIP_MAX_ITEMS", "20") or "20")
        if len(pending_rows) > max_items:
            self.log(f"  ⚠️ 待處理 {len(pending_rows)} 筆，僅處理前 {max_items} 筆")
            pending_rows = pending_rows[:max_items]
        self.log(f"  待下載繳費單: {len(pending_rows)} 筆")

        # ========= 步驟 3: 依序下載每筆繳費單 =========
        for i, rd in enumerate(pending_rows):
            rj = rd.get("row_json", {})
            row_idx = rd.get("idx", 0)
            case_id = rj.get("showyyidno") or rj.get("yyidno") or f"row_{row_idx}"
            party = rj.get("clnm") or ""
            self.log(f"  [{i+1}/{len(pending_rows)}] 下載繳費單: {party}｜{case_id}")

            # 確保在 v1 frame
            self._switch_to_review_list_v1()

            # 找到列表中的 row element
            row_elem = self.driver.execute_script("""
            var rows = document.querySelectorAll("tr#trdata, table#tablecontext tbody tr");
            var idx = arguments[0];
            return idx < rows.length ? rows[idx] : null;
            """, row_idx)

            if row_elem is None:
                self.log(f"    ⚠️ 找不到列元素 (idx={row_idx})")
                continue

            # 拍快照
            before_snap = _snapshot_folder(today_folder)

            # 直接使用 doViewFHD2E（不走 backup locator，避免點錯元素）
            new_files = self._download_payment_slip_direct(
                row_elem, rj, today_folder, dict(before_snap)
            )

            # 如果現有方法沒抓到，手動等更久再掃一次
            if not new_files:
                self.log("    ℹ️ 初次掃描未偵測到新檔案，延長等待...")
                time.sleep(5)
                after_snap = _snapshot_folder(today_folder)
                for fn, mt in after_snap.items():
                    if fn not in before_snap or mt > before_snap[fn]:
                        new_files.append(fn)
                if new_files:
                    self.log(f"    ✓ 延長掃描找到 {len(new_files)} 個新檔案")

            # 重新命名 PDF 以包含案件資訊（避免全部叫「繳費單.pdf」無法區分）
            # 只重新命名 Chrome 預設的「繳費單」檔名，避免誤改已命名的檔案
            renamed_files = []
            for fn in new_files:
                src = os.path.join(today_folder, fn)
                # 只重新命名以「繳費單」開頭的 PDF（Chrome 預設名稱）
                is_generic_name = fn.startswith("繳費單") and fn.lower().endswith(".pdf")
                if is_generic_name and party and case_id:
                    # 清理案號中的特殊字符
                    safe_case = re.sub(r'[\\/:*?"<>|]', '', case_id).strip()
                    safe_party = re.sub(r'[\\/:*?"<>|]', '', party).strip()
                    new_name = f"繳費單_{safe_party}_{safe_case}.pdf"
                    dst = os.path.join(today_folder, new_name)
                    # 避免重複檔名
                    if os.path.exists(dst) and dst != src:
                        base, ext = os.path.splitext(new_name)
                        for idx in range(1, 100):
                            dst = os.path.join(today_folder, f"{base}_{idx}{ext}")
                            if not os.path.exists(dst):
                                break
                    try:
                        os.rename(src, dst)
                        renamed_files.append(os.path.basename(dst))
                        self.log(f"    📝 重新命名: {fn} → {os.path.basename(dst)}")
                    except Exception as ren_e:
                        self.log(f"    ⚠️ 重新命名失敗: {ren_e}")
                        renamed_files.append(fn)
                else:
                    renamed_files.append(fn)

            # 記錄結果（統一用 _mark_payment_processed 確保欄位完整）
            pdf_paths = [os.path.join(today_folder, fn) for fn in renamed_files if fn.lower().endswith(".pdf")]
            all_paths = [os.path.join(today_folder, fn) for fn in renamed_files]

            if all_paths:
                case_meta = {
                    "court": rj.get("crtid") or "",
                    "case_number": case_id,
                    "showyyidno": rj.get("showyyidno") or "",
                    "party": party,
                }
                self._mark_payment_processed(rj, files=all_paths, case_info=case_meta)
                results.append({
                    "case_number": case_id,
                    "party": party,
                    "court": rj.get("crtid") or "",
                    "pdf_path": pdf_paths[0] if pdf_paths else all_paths[0],
                    "all_paths": all_paths,
                    "already_existed": False,
                })
                self.log(f"    ✅ 繳費單已下載: {new_files}")
            else:
                # 即使沒抓到檔案也記錄（避免重複嘗試）
                self._mark_payment_processed(rj, files=[], case_info={
                    "case_number": case_id, "party": party,
                })
                self.log(f"    ⚠️ 未偵測到繳費單檔案")

            # 關閉 FHD2E dialog（如果還開著）
            try:
                self.driver.switch_to.default_content()
                self.driver.execute_script("""
                // 關閉 IDX dialog
                var frames = document.querySelectorAll('iframe[name^="IDX_"]');
                for (var i = 0; i < frames.length; i++) {
                    try { frames[i].parentElement.removeChild(frames[i]); } catch(e) {}
                }
                // 關閉 jQuery UI dialog
                try { if (typeof $ !== 'undefined') $('.ui-dialog').remove(); } catch(e) {}
                """)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4354, exc_info=True)
            time.sleep(1.5)

        self.log(f"✅ 繳費單下載完成: {len(results)} 筆（含已有）")
        return results

    def download_case_files(self, info: FileReviewInfo) -> bool:
        """
        下載指定案件的閱卷資料
        (目前實作: 登入並下載所有「列表式查看」中的可下載項目)
        """
        self.log(f"準備下載案件: {info.case_number}")
        
        # 確保已登入
        if not hasattr(self, 'logged_in') or not self.logged_in:
            if not self.login():
                return False
                
        # 導航到閱卷系統
        if not self.navigate_to_file_review():
            return False
            
        # 執行批次下載
        # 優化：傳入案號進行過濾，只下載該案件
        downloaded = self.check_and_download_available(target_case_number=info.case_number)
        
        if downloaded:
            self.log(f"  共下載 {len(downloaded)} 個檔案")
            # 更新 info.files (雖然可能包含其他案件的檔案，但至少有東西)
            info.files.extend(downloaded)
            return True
        else:
            self.log("  未下載任何檔案 (可能已過期或無需下載)")
            return True # 沒檔案也算執行成功，避免報錯

    def check_and_download_available(self, target_case_number: str = None) -> List[str]:
        """
        檢查並下載可下載的閱卷資料
        
        流程：
        1. 點選「列表式查看」
        2. 查詢「線上下載」按鈕
        3. 點選下載 (若有指定 target_case_number，只下載該案號)
        4. 建立日期資料夾並歸檔
        
        Returns:
            下載的檔案路徑列表
        """
        downloaded_files = []
        # 每輪重置，避免跨 run 汙染
        self._last_download_meta_by_file = {}
        self._last_smart_skipped_files = []
        # new_file_path -> case meta (party/showyyidno/yyidno/court). Used for human-readable reporting.
        download_meta_by_file = {}
        start_ts = time.time()
        max_runtime_sec = int(os.environ.get("MAGI_FILE_REVIEW_DOWNLOAD_MAX_RUNTIME_SEC", "900") or "900")

        def _time_exceeded() -> bool:
            """
            Nightly/排程穩定性保護：
            Selenium 有時會遇到頁面結構變更/按鈕點擊失效，導致在多案、多次重試時拖很久。
            這裡用「整個 download 檢查」的總時間上限，確保不會卡死 nightly。
            """
            return (time.time() - start_ts) > max_runtime_sec
    
        try:
            self.log(f"檢查可下載的閱卷資料 (目標: {target_case_number or '全部'})...")
            if _time_exceeded():
                self.log(f"  ⏳ 已達時間上限 {max_runtime_sec}s，結束本輪檢查（避免 nightly 卡死）")
                return downloaded_files
            
            # 重要: 先回到主文件，因為 navigate_to_file_review 可能在巢狀 frame 內
            # 側邊選單 (列表式查看) 在根層級，不在 frame 內
            self.driver.switch_to.default_content()
            
            # 點選「列表式查看」(使用 contains(., text) 匹配 <a><i>icon</i> 文字</a> 結構)
            list_view_btn = None
            try:
                self.log("  尋找「列表式查看」按鈕...")
                selectors = [
                    "//a[contains(normalize-space(.), '列表式查看')]",
                    "//a[contains(., '列表式查看')]",
                    "//li//a[contains(., '列表式查看')]",
                ]
                for selector in selectors:
                    try:
                        list_view_btn = self.driver.find_element(By.XPATH, selector)
                        if list_view_btn and list_view_btn.is_displayed():
                            break
                    except Exception:
                        continue
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4446, exc_info=True)
            
            if list_view_btn:
                self.log("  點擊「列表式查看」")
                ActionChains(self.driver).move_to_element(list_view_btn).click().perform()
                time.sleep(1)  # 等待頁面載入
            else:
                self.log("  未找到「列表式查看」按鈕 (可能已在列表模式)")

            if _time_exceeded():
                self.log(f"  ⏳ 已達時間上限 {max_runtime_sec}s，結束本輪檢查（避免 nightly 卡死）")
                return downloaded_files
            
            # 重要: 切換到 main-content iframe (選單項目的 target="main-content")
            # 下載按鈕和表單欄位都在這個 iframe 裡面，不在儀表板區域
            self.log("  切換到 main-content iframe...")
            try:
                self.driver.switch_to.default_content()  # 先回到主文件
                main_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']")
                self.driver.switch_to.frame(main_iframe)
                self.log("  ✓ 成功切換到 main-content iframe")
                
                # 重要: 列表式查看頁面內還有一個 v1 iframe，實際資料在裡面
                # 結構: main-content iframe -> v1 iframe -> 資料表格
                time.sleep(1)
                try:
                    v1_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='v1'] | //iframe[@id='v1']")
                    self.driver.switch_to.frame(v1_iframe)
                    self.log("  ✓ 成功切換到 v1 iframe")
                except Exception as e:
                    self.log(f"  ⚠️ 未找到 v1 iframe (可能資料直接在 main-content 中): {e}")
            except Exception as e:
                self.log(f"  ⚠️ 無法切換到 main-content iframe: {e}")
            
            # 等待頁面完全載入，並滾動以確保所有元素都載入
            time.sleep(2)
            try:
                # 滾動到頁面底部，確保所有下載按鈕都載入
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(1)
                # 再滾回頂部
                self.driver.execute_script("window.scrollTo(0, 0);")
                time.sleep(1)
                self.log("  已滾動頁面確保所有內容載入")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4491, exc_info=True)
            
            # 查詢「線上下載」按鈕 (綠色按鈕，下方有紅色期限文字)
            # 實際結構: <input type='button' name='btn_pay' title='線上下載' value='線上下載'>
            self.log("  查詢「線上下載」按鈕...")
            download_btns = []
            
            download_selectors = [
                # 1. 最精確: input 按鈕 with title='線上下載'
                "//input[@type='button' and @title='線上下載']",
                "//input[@name='btn_pay']",
                "//input[@value='線上下載']",
                # 2. 備用: 其他可能的按鈕結構
                "//button[contains(., '線上下載')]",
                "//a[contains(., '線上下載')]",
            ]
            for selector in download_selectors:
                try:
                    btns = self.driver.find_elements(By.XPATH, selector)
                    if btns:
                        download_btns.extend(btns)
                        self.log(f"  使用選擇器 '{selector[:40]}...' 找到 {len(btns)} 個按鈕")
                        break
                except Exception as e:
                    continue
            
            # ★★★ 案號過濾：只處理目標案件 ★★★
            if download_btns and target_case_number:
                self.log(f"  🔍 正在過濾案號: {target_case_number} (共 {len(download_btns)} 個按鈕)")
                filtered_btns = []
                
                for btn in download_btns:
                    try:
                        # 找到按鈕所在的列 (tr)
                        row = btn.find_element(By.XPATH, "./ancestor::tr")
                        
                        # 1. 優先從 data-json 取得案號 (最精確)
                        json_str = row.get_attribute("data-json")
                        if json_str:
                            try:
                                import json
                                json_data = json.loads(json_str)
                                row_case_number = json_data.get("yyidno", "")
                                row_show_case_number = json_data.get("showyyidno", "")
                                row_c60_case_number = json_data.get("c60yyidno", "")
                                if self._matches_target_case_number(
                                    target_case_number,
                                    row_case_number,
                                    row_show_case_number,
                                    row_c60_case_number,
                                ):
                                    filtered_btns.append(btn)
                                    self.log(f"    ✓ 匹配: {row_show_case_number or row_case_number or row_c60_case_number}")
                                    continue
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4546, exc_info=True)
                        
                        # 2. 備用：從 row.text 比對
                        row_text = row.text
                        if self._matches_target_case_number(target_case_number, row_text):
                            filtered_btns.append(btn)
                            self.log(f"    ✓ 文字匹配")
                            continue
                            
                    except Exception as e:
                        self.log(f"    ⚠️ 無法檢查按鈕: {e}")
                
                if filtered_btns:
                    self.log(f"  ✅ 過濾後剩 {len(filtered_btns)} 個符合條件的按鈕")
                    download_btns = filtered_btns
                else:
                    self.log(f"  ⚠️ 找不到符合 {target_case_number} 的按鈕；為避免誤歸檔，本輪不下載")
                    return downloaded_files
            
            if not download_btns:
                self.log(f"  找到 {len(download_btns)} 個可下載項目 (沒有資料)")
                self.log("  ℹ️ 目前無可下載資料")
                # Debug: 截圖和 HTML 以便分析
                try:
                    ts = int(datetime.now().timestamp())
                    self.driver.save_screenshot(f"debug_no_download_{ts}.png")
                    with open(f"debug_no_download_{ts}.html", "w", encoding="utf-8") as f:
                        f.write(self.driver.page_source)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4575, exc_info=True)
                
                # Check for "無資料" text
                page_source = self.driver.page_source
                if "無資料" in page_source or "查無資料" in page_source:
                    self.log("  (確認無檔案可下載)")
                    pass

                # 仍可能存在「待繳費」列（沒有線上下載按鈕），這裡補跑繳費單流程。
                try:
                    pending_rows = self.driver.find_elements(By.XPATH, "//tr[@id='trdata'] | //table[@id='tablecontext']//tbody//tr")
                except Exception:
                    pending_rows = []

                if pending_rows:
                    self.log(f"  🔎 改掃描待繳費列（共 {len(pending_rows)} 列）...")
                    today_folder = os.path.join(self.download_folder, datetime.now().strftime("%Y%m%d"))
                    os.makedirs(today_folder, exist_ok=True)
                    existing_file_mtimes = {}
                    try:
                        for fn in os.listdir(today_folder):
                            fp = os.path.join(today_folder, fn)
                            if os.path.isfile(fp):
                                existing_file_mtimes[fn] = os.path.getmtime(fp)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4600, exc_info=True)

                    pending_case_info_list = []
                    for row in pending_rows:
                        try:
                            row_json = self._extract_row_json(row, None)
                            row_text = row.text or ""
                            if not self._is_pending_payment_row(row_json, row_text=row_text):
                                continue

                            if target_case_number:
                                if not self._matches_target_case_number(
                                    target_case_number,
                                    row_json.get("yyidno"),
                                    row_json.get("showyyidno"),
                                    row_json.get("c60yyidno"),
                                    row_text,
                                ):
                                    continue

                            case_info = {
                                "court": row_json.get("crtid", ""),
                                "case_number": row_json.get("yyidno", ""),
                                "showyyidno": row_json.get("showyyidno", ""),
                                "party": row_json.get("clnm", ""),
                            }
                            pending_case_info_list.append(case_info)

                            # 期限過濾：超過14天逾期的跳過
                            _dl_raw = str(row_json.get("paylimitdt") or row_json.get("limitdt") or "").strip()
                            if _dl_raw and len(_dl_raw) == 7:
                                try:
                                    _dy = int(_dl_raw[:3]) + 1911
                                    _dm = int(_dl_raw[3:5])
                                    _dd = int(_dl_raw[5:7])
                                    _dl_date = datetime(year=_dy, month=_dm, day=_dd).date()
                                    _days_over = (datetime.now().date() - _dl_date).days
                                    if _days_over > 14:
                                        self.log(f"  ⏭️ 已逾期 {_days_over} 天，跳過: {case_info.get('party') or ''}｜{case_info.get('case_number') or case_info.get('showyyidno') or ''}")
                                        continue
                                except Exception:
                                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4641, exc_info=True)

                            if self._is_payment_processed(row_json):
                                self.log(f"  ⏭️ [繳費單已處理] 跳過重新下載案號 {case_info.get('case_number') or case_info.get('showyyidno') or '-'}")
                                self._notify_payment_if_needed(row_json, case_info=case_info, file_paths=None)
                                continue

                            payment_new_files = self._download_payment_slip_from_row(
                                row,
                                row_json=row_json,
                                today_folder=today_folder,
                                existing_file_mtimes=existing_file_mtimes,
                            ) or []

                            payment_paths = []
                            if payment_new_files:
                                yyidno_reg = (case_info.get("case_number") or case_info.get("showyyidno") or "").strip()
                                case_meta = {
                                    "court": case_info.get("court", ""),
                                    "case_number": case_info.get("case_number", ""),
                                    "showyyidno": case_info.get("showyyidno", ""),
                                    "party": case_info.get("party", ""),
                                    "artifact_type": "payment_slip",
                                }
                                _pay_party2 = (case_meta.get("party") or "").strip()
                                _pay_case2 = yyidno_reg
                                for nf in payment_new_files:
                                    srcp = os.path.join(today_folder, nf)
                                    # 重新命名繳費單 PDF
                                    if nf.lower().endswith(".pdf") and _pay_party2 and _pay_case2:
                                        safe_case = re.sub(r'[\\/:*?"<>|]', '', _pay_case2).strip()
                                        safe_party = re.sub(r'[\\/:*?"<>|]', '', _pay_party2).strip()
                                        new_name = f"繳費單_{safe_party}_{safe_case}.pdf"
                                        dst = os.path.join(today_folder, new_name)
                                        if os.path.exists(dst) and dst != srcp:
                                            base, ext = os.path.splitext(new_name)
                                            for _ridx in range(1, 100):
                                                dst = os.path.join(today_folder, f"{base}_{_ridx}{ext}")
                                                if not os.path.exists(dst):
                                                    break
                                        try:
                                            os.rename(srcp, dst)
                                            srcp = dst
                                            self.log(f"    📝 重新命名: {nf} → {os.path.basename(dst)}")
                                        except Exception:
                                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4686, exc_info=True)
                                    payment_paths.append(srcp)
                                    downloaded_files.append(srcp)
                                    download_meta_by_file[srcp] = dict(case_meta)
                                    self._register_downloaded(os.path.basename(srcp), yyidno=yyidno_reg, case_info=case_meta)
                                    try:
                                        self._last_download_meta_by_file[srcp] = dict(case_meta)
                                        self._last_download_meta_by_file[os.path.basename(srcp)] = dict(case_meta)
                                    except Exception:
                                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4695, exc_info=True)
                                self._mark_payment_processed(row_json, files=payment_paths, case_info=case_meta)

                            self._notify_payment_if_needed(
                                row_json,
                                case_info=case_info,
                                file_paths=payment_paths,
                            )
                        except Exception as row_e:
                            self.log(f"  ⚠️ 待繳費列處理失敗: {row_e}")

                    self._save_download_registry()
                    if downloaded_files and pending_case_info_list:
                        self._archive_to_case_folders(downloaded_files, pending_case_info_list)
                return downloaded_files
                
            # Step 4: 尋找下載按鈕 (只找單檔下載按鈕，排除批次下載)
            filtered_btns = []
            # 關鍵區別：
            # - 單檔下載按鈕: 在 tr#trdata 內, title="下載", width: 30px
            # - 批次下載按鈕: title="單檔批次下載", 有 <span> 文字
            selectors = [
                # 最精確：表格資料行內 (tr#trdata) 的下載按鈕
                "//tr[@id='trdata']//button[@title='下載']",
                # 備用：tablecontext tbody 內的下載按鈕
                "//table[@id='tablecontext']//tbody//tr//button[@title='下載']",
                # 備用：只有 fa-download 圖示沒有 span 文字的按鈕 (排除批次)
                "//tbody//tr[@id='trdata']//button[.//i[contains(@class, 'fa-download')]]",
            ]
            
            for sel in selectors:
                btns = self.driver.find_elements(By.XPATH, sel)
                visible_btns = [b for b in btns if b.is_displayed()]
                
                if visible_btns:
                    # 如果有指定案號，進行過濾
                    if target_case_number:
                        self.log(f"  正在快篩 {len(visible_btns)} 個按鈕，尋找案號: {target_case_number}...")
                        
                        for btn in visible_btns:
                            try:
                                # 找到按鈕所在的列 (tr)
                                row = btn.find_element(By.XPATH, "./ancestor::tr")
                                row_text = row.text
                                json_str = row.get_attribute("data-json")
                                
                                # 1. 先比較 row 文字
                                if self._matches_target_case_number(target_case_number, row_text):
                                    filtered_btns.append(btn)
                                    self.log(f"  ✓ 找到目標案號 (文字匹配)")
                                    continue
                                    
                                # 2. 再比較 data-json 欄位
                                if json_str:
                                    try:
                                        import json
                                        json_data = json.loads(json_str)
                                        if self._matches_target_case_number(
                                            target_case_number,
                                            json_data.get("yyidno", ""),
                                            json_data.get("showyyidno", ""),
                                            json_data.get("c60yyidno", ""),
                                            json_str,
                                        ):
                                            filtered_btns.append(btn)
                                            self.log(f"  ✓ 找到目標案號 (JSON匹配)")
                                            continue
                                    except Exception:
                                        if self._matches_target_case_number(target_case_number, json_str):
                                            filtered_btns.append(btn)
                                            self.log(f"  ✓ 找到目標案號 (JSON字串匹配)")
                                            continue
                                        
                            except Exception as e:
                                self.log(f"  ⚠️ 無法檢查按鈕對應案號: {e}")
                                
                        if not filtered_btns:
                            self.log(f"  ⚠️ 找不到符合 {target_case_number} 的按鈕；為避免誤歸檔，本輪不下載")
                            return downloaded_files
                    else:
                        filtered_btns = visible_btns
                        
                    if filtered_btns:
                        download_btns = filtered_btns
                        self.log(f"  ✓ 最終鎖定 {len(download_btns)} 個下載按鈕")
                        break
                    else:
                         self.log("  ⚠️ 在此選擇器下未找到符合目標案號的按鈕")
            
            self.log(f"  找到 {len(download_btns)} 個可下載項目")
            
            # 建立今日日期資料夾
            today_folder = os.path.join(
                self.download_folder,
                datetime.now().strftime("%Y%m%d")
            )
            os.makedirs(today_folder, exist_ok=True)
            self.log(f"  建立下載資料夾: {today_folder}")
            
            # ★ 記錄下載前的時間戳（用於偵測新檔案）
            download_start_time = time.time()
            
            # 也記錄現有檔案的修改時間（用於排除）
            existing_file_mtimes = {}
            if os.path.exists(today_folder):
                for f in os.listdir(today_folder):
                    fpath = os.path.join(today_folder, f)
                    if os.path.isfile(fpath):
                        existing_file_mtimes[f] = os.path.getmtime(fpath)
            
            # 點擊每個下載按鈕，同時擷取當事人資料
            case_info_list = []  # 存儲每個案件的資訊
            for i, btn in enumerate(download_btns):
                if _time_exceeded():
                    self.log(f"  ⏳ 已達時間上限 {max_runtime_sec}s，停止後續按鈕處理（已完成 {i}/{len(download_btns)}）")
                    break
                try:
                    # 擷取該下載按鈕所在表格列 (tr) 的案件資訊
                    case_info = {"index": i+1}
                    parent_row = None
                    row_json = {}
                    try:
                        # 向上找到父層 tr 元素
                        parent_row = btn.find_element(By.XPATH, "./ancestor::tr")
                        row_json = self._extract_row_json(parent_row, btn)
                        if row_json:
                            case_info["court"] = row_json.get("crtid", "")
                            case_info["case_number"] = row_json.get("yyidno", "")
                            case_info["showyyidno"] = row_json.get("showyyidno", "")  # ★ 正式案號格式
                            case_info["party"] = row_json.get("clnm", "")
                            case_info["sys"] = row_json.get("sys", "")
                            self.log(f"  擷取案件資料: 法院={case_info.get('court')} 案號={case_info.get('case_number')} 當事人={case_info.get('party')}")
                        
                        # 從 td 元素擷取文字內容作為備用
                        if not case_info.get("party"):
                            try:
                                tds = parent_row.find_elements(By.TAG_NAME, "td")
                                if len(tds) >= 3:
                                    # 第3欄通常是 法院/案號/當事人
                                    cell_text = tds[2].text if len(tds) > 2 else ""
                                    lines = cell_text.split("\n")
                                    if len(lines) >= 3:
                                        case_info["party"] = lines[-1].strip()  # 最後一行是當事人
                                    elif len(lines) >= 1:
                                        case_info["party"] = lines[0].strip()
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4841, exc_info=True)
                    except Exception as e:
                        self.log(f"  ⚠️ 無法擷取案件資料: {e}")
                    
                    case_info_list.append(case_info)

                    # 補抓 row JSON（某些列沒有 data-json 屬性），確保去重 key 可用。
                    if not (case_info.get("case_number") or case_info.get("showyyidno")):
                        try:
                            row_json = row_json or self.driver.execute_script("return $(arguments[0]).closest('tr').data('json');", btn)
                            if isinstance(row_json, dict):
                                case_info["court"] = row_json.get("crtid", "") or case_info.get("court", "")
                                case_info["case_number"] = row_json.get("yyidno", "") or case_info.get("case_number", "")
                                case_info["showyyidno"] = row_json.get("showyyidno", "") or case_info.get("showyyidno", "")
                                case_info["party"] = row_json.get("clnm", "") or case_info.get("party", "")
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4857, exc_info=True)

                    row_text = ""
                    try:
                        row_text = parent_row.text if parent_row else ""
                    except Exception:
                        row_text = ""

                    # 待繳費流程：
                    # - 未處理：下載繳費單並通知，該列不進入卷宗下載
                    # - 已處理：僅補通知（必要時），並繼續嘗試卷宗下載
                    row_has_online_download = False
                    try:
                        row_has_online_download = bool(
                            btn
                            and str(btn.get_attribute("title") or "").strip() == "線上下載"
                        )
                    except Exception:
                        row_has_online_download = False
                    if self._is_pending_payment_row(row_json, row_text=row_text):
                        payment_row = parent_row
                        payment_row_json = row_json if isinstance(row_json, dict) else {}
                        try:
                            if parent_row is not None:
                                picked_row, picked_json = self._select_best_pending_row_for_case(parent_row, payment_row_json)
                                if picked_row is not None:
                                    payment_row = picked_row
                                    if isinstance(picked_json, dict) and picked_json:
                                        payment_row_json = picked_json
                                        if picked_row is not parent_row:
                                            self.log("  ℹ️ 同案有多筆待繳費資料，已改用較佳列（優先可下載繳費單）")
                        except Exception as pick_e:
                            self.log(f"  ⚠️ 待繳費列挑選失敗，沿用當前列: {pick_e}")

                        yyidno_payment = (case_info.get("case_number") or case_info.get("showyyidno") or payment_row_json.get("yyidno") or payment_row_json.get("showyyidno") or "").strip()
                        party_payment = (case_info.get("party") or payment_row_json.get("clnm") or "").strip() or "(未知)"

                        # 指定辯護案件免繳費，跳過
                        if self._is_fee_exempt_case(
                            court_case_no=(case_info.get("showyyidno") or payment_row_json.get("showyyidno") or ""),
                            party=party_payment,
                            yyidno=(case_info.get("case_number") or payment_row_json.get("yyidno") or ""),
                        ):
                            self.log(f"  ⏭️ 免繳費案件（指定辯護）: {party_payment}｜{yyidno_payment or '-'}")
                            if row_has_online_download:
                                self.log("  ℹ️ 同列可見「線上下載」，繼續嘗試卷宗下載。")
                            else:
                                continue

                        # 期限過濾：超過14天逾期的跳過繳費（但仍繼續嘗試卷宗下載）
                        elif self._is_payment_overdue(payment_row_json, max_days=14):
                            _dl_raw = str(payment_row_json.get("paylimitdt") or payment_row_json.get("limitdt") or "").strip()
                            self.log(f"  ⏭️ 繳費已逾期超過14天: {party_payment}｜{yyidno_payment or '-'} (期限:{_dl_raw})")
                            if row_has_online_download:
                                self.log("  ℹ️ 同列可見「線上下載」，繼續嘗試卷宗下載。")
                            else:
                                continue

                        elif self._is_payment_processed(payment_row_json):
                            self.log(f"  ⏭️ [繳費單已處理] 跳過重新下載案號 {yyidno_payment or '-'} (當事人: {party_payment})")
                            self._notify_payment_if_needed(payment_row_json, case_info=case_info, file_paths=None)
                            self.log("  ℹ️ 繳費單已處理，繼續嘗試卷宗檔案下載")
                        else:
                            self.log(f"  💰 偵測待繳費項目，嘗試下載繳費單: {party_payment}｜{yyidno_payment or '-'}")
                            payment_new_files = []
                            try:
                                if payment_row is not None:
                                    payment_new_files = self._download_payment_slip_from_row(
                                        payment_row,
                                        row_json=payment_row_json,
                                        today_folder=today_folder,
                                        existing_file_mtimes=existing_file_mtimes,
                                    ) or []
                            except Exception as pay_e:
                                self.log(f"  ⚠️ 待繳費下載流程失敗: {pay_e}")

                            case_meta = {
                                "court": (case_info.get("court") or payment_row_json.get("crtid") or ""),
                                "case_number": (case_info.get("case_number") or payment_row_json.get("yyidno") or ""),
                                "showyyidno": (case_info.get("showyyidno") or payment_row_json.get("showyyidno") or ""),
                                "party": (case_info.get("party") or payment_row_json.get("clnm") or ""),
                                "artifact_type": "payment_slip",
                            }

                            payment_paths = []
                            if payment_new_files:
                                self.log(f"  ✅ 待繳費下載完成 {len(payment_new_files)} 份")
                                yyidno_reg = (case_meta.get("case_number") or case_meta.get("showyyidno") or "").strip()
                                _pay_party = (case_meta.get("party") or "").strip()
                                _pay_case = yyidno_reg
                                for nf in payment_new_files:
                                    srcp = os.path.join(today_folder, nf)
                                    # 重新命名繳費單 PDF
                                    if nf.lower().endswith(".pdf") and _pay_party and _pay_case:
                                        safe_case = re.sub(r'[\\/:*?"<>|]', '', _pay_case).strip()
                                        safe_party = re.sub(r'[\\/:*?"<>|]', '', _pay_party).strip()
                                        new_name = f"繳費單_{safe_party}_{safe_case}.pdf"
                                        dst = os.path.join(today_folder, new_name)
                                        if os.path.exists(dst) and dst != srcp:
                                            base, ext = os.path.splitext(new_name)
                                            for _ridx in range(1, 100):
                                                dst = os.path.join(today_folder, f"{base}_{_ridx}{ext}")
                                                if not os.path.exists(dst):
                                                    break
                                        try:
                                            os.rename(srcp, dst)
                                            srcp = dst
                                            self.log(f"    📝 重新命名: {nf} → {os.path.basename(dst)}")
                                        except Exception:
                                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4966, exc_info=True)
                                    payment_paths.append(srcp)
                                    download_meta_by_file[srcp] = dict(case_meta)
                                    self._register_downloaded(os.path.basename(srcp), yyidno=yyidno_reg, case_info=case_meta)
                                self._mark_payment_processed(payment_row_json, files=payment_paths, case_info=case_meta)
                            else:
                                self.log("  ⚠️ 未偵測到繳費單檔案（可能尚未觸發下載）")

                            try:
                                self._notify_payment_if_needed(payment_row_json, case_info=case_meta, file_paths=payment_paths)
                            except Exception as n_e:
                                self.log(f"  ⚠️ 待繳費通知失敗: {n_e}")
                            # 這類列在法院端可能同時顯示「待繳費」訊息與「線上下載」按鈕。
                            # 若直接 continue 會錯過可下載卷宗，因此此情況要續跑下載流程。
                            if row_has_online_download:
                                self.log("  ℹ️ 同列可見「線上下載」，繼續嘗試卷宗下載。")
                            else:
                                continue

                    # ★★★ Registry 去重：如果該案號已經下載過，跳過 ★★★
                    yyidno_for_dedup = (case_info.get("case_number") or case_info.get("showyyidno") or "").strip()
                    if self.enable_case_level_download_skip and yyidno_for_dedup and self._is_yyidno_fully_downloaded(yyidno_for_dedup):
                        party_label = (case_info.get("party") or "").strip() or "(未知)"
                        self.log(f"  ⏭️ [已下載] 跳過案號 {yyidno_for_dedup} (當事人: {party_label})——registry 中已有紀錄")
                        continue

                    # ★★★ 案件資料夾去重：如果閱卷資料夾已有檔案，跳過下載 ★★★
                    if self._case_review_folder_has_files(case_info):
                        party_label = (case_info.get("party") or "").strip() or "(未知)"
                        self.log(f"  ⏭️ [閱卷資料已存在] 跳過案號 {yyidno_for_dedup} (當事人: {party_label})——案件資料夾已有閱卷資料")
                        continue
                    
                    # 嘗試點擊並確認彈窗開啟 (最多試 3 次)
                    max_open_retries = 3
                    popup_opened = False
                    
                    for open_try in range(max_open_retries):
                        if _time_exceeded():
                            self.log(f"  ⏳ 已達時間上限 {max_runtime_sec}s，停止下載彈窗重試（第 {i+1} 筆）")
                            break
                        self.log(f"  處理第 {i+1} 個案號，點擊下載按鈕 (第 {open_try+1} 次)...")
                        
                        try:
                            # DEBUG: 印出按鈕資訊
                            try:
                                btn_html = btn.get_attribute("outerHTML")
                                self.log(f"    Button HTML: {btn_html[:200]}...")
                            except Exception:
                                self.log("    無法取得 Button HTML")
                                
                            # 1. 確保按鈕可見
                            self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                            time.sleep(0.5)
                            
                            # DEBUG: 截圖點擊前狀態
                            if open_try == 0:
                                ts = int(datetime.now().timestamp())
                                self.driver.save_screenshot(f"debug_click_before_{ts}.png")
                            
                            # 2. 嘗試不同點擊方式 - 優先使用真實 MouseEvent
                            
                            # 方法 1: 創建並派發真實的 MouseEvent
                            self.log("    嘗試派發真實 MouseEvent...")
                            self.driver.execute_script("""
                                var btn = arguments[0];
                                var rect = btn.getBoundingClientRect();
                                var centerX = rect.left + rect.width / 2;
                                var centerY = rect.top + rect.height / 2;
                                
                                var mouseDownEvt = new MouseEvent('mousedown', {
                                    bubbles: true,
                                    cancelable: true,
                                    view: window,
                                    clientX: centerX,
                                    clientY: centerY
                                });
                                btn.dispatchEvent(mouseDownEvt);
                                
                                var mouseUpEvt = new MouseEvent('mouseup', {
                                    bubbles: true,
                                    cancelable: true,
                                    view: window,
                                    clientX: centerX,
                                    clientY: centerY
                                });
                                btn.dispatchEvent(mouseUpEvt);
                                
                                var clickEvt = new MouseEvent('click', {
                                    bubbles: true,
                                    cancelable: true,
                                    view: window,
                                    clientX: centerX,
                                    clientY: centerY
                                });
                                btn.dispatchEvent(clickEvt);
                            """, btn)
                            
                            time.sleep(1)
                            if self._is_popup_open():
                                popup_opened = True
                                break
                            
                            # 方法 2: 使用 jQuery triggerHandler (模擬 jQuery 綁定的事件)
                            self.log("    MouseEvent 無效，嘗試 jQuery triggerHandler...")
                            self.driver.execute_script("""
                                var $btn = $(arguments[0]);
                                var e = $.Event('click');
                                e.stopPropagation = function() {};
                                $btn.triggerHandler(e);
                            """, btn)
                            
                            time.sleep(1)
                            if self._is_popup_open():
                                popup_opened = True
                                break
                            
                            # 方法 3: 直接呼叫內部函式 (從 tr 取得 json 並呼叫)
                            self.log("    jQuery 無效，嘗試直接呼叫 doViewFile()...")
                            try:
                                row_json = self.driver.execute_script("return $(arguments[0]).closest('tr').data('json');", btn)
                                self.log(f"    取得 Row JSON: {row_json}")
                                
                                if row_json:
                                    # ★ (補)更新案件資料: 確保 showyyidno 被擷取
                                    try:
                                        case_info["court"] = row_json.get("crtid", "")
                                        case_info["case_number"] = row_json.get("yyidno", "") or case_info.get("case_number", "")
                                        case_info["showyyidno"] = row_json.get("showyyidno", "")
                                        case_info["party"] = row_json.get("clnm", "")
                                        self.log(f"    (補)更新案件資料: {case_info.get('showyyidno')}")
                                    except Exception:
                                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5097, exc_info=True)

                                    # 嘗試完整模擬按鈕點擊的邏輯
                                    self.driver.execute_script("""
                                        var json = arguments[0];
                                        if (typeof dateUtil !== 'undefined' && typeof doViewFile === 'function') {
                                            if (dateUtil.getNowCDate() * 1 <= json.limitdt * 1 || json.limitdt == "") {
                                                doViewFile(json);
                                            }
                                        } else if (typeof doViewFile === 'function') {
                                            doViewFile(json);
                                        }
                                    """, row_json)
                                    self.log("    已呼叫 doViewFile(json)")
                            except Exception as e:
                                self.log(f"    直接呼叫失敗: {e}")
                            
                            time.sleep(1) # 等待彈窗動畫
                            
                            # 3. 檢查彈窗是否出現 (jQuery UI Dialog 或 colorbox)
                            cbox_visible = self._is_popup_open_all_frames()
                            
                            # (A) 檢查 Root - jQuery UI Dialog
                            if not cbox_visible:
                                self.driver.switch_to.default_content()
                                try:
                                    dialogs = self.driver.find_elements(By.CSS_SELECTOR, ".ui-dialog[role='dialog'], #colorbox")
                                    if dialogs and any(d.is_displayed() and d.size['width'] > 100 for d in dialogs):
                                        cbox_visible = True
                                        self.log("  ✓ 在 Root 偵測到彈窗")
                                except Exception:
                                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5128, exc_info=True)
                            
                            # (B) 檢查 main-content
                            if not cbox_visible:
                                try:
                                    main_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']")
                                    self.driver.switch_to.frame(main_iframe)
                                    dialogs = self.driver.find_elements(By.CSS_SELECTOR, ".ui-dialog[role='dialog'], #colorbox")
                                    if dialogs and any(d.is_displayed() and d.size['width'] > 100 for d in dialogs):
                                        cbox_visible = True
                                        self.log("  ✓ 在 main-content 偵測到彈窗")
                                except Exception:
                                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5140, exc_info=True)
                            
                            # (C) 檢查 v1
                            if not cbox_visible:
                                try:
                                    self.driver.switch_to.default_content()
                                    main_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']")
                                    self.driver.switch_to.frame(main_iframe)
                                    v1_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='v1'] | //iframe[@id='v1']")
                                    self.driver.switch_to.frame(v1_iframe)
                                    
                                    dialogs = self.driver.find_elements(By.CSS_SELECTOR, ".ui-dialog[role='dialog'], #colorbox")
                                    if dialogs and any(d.is_displayed() and d.size['width'] > 100 for d in dialogs):
                                        cbox_visible = True
                                        self.log("  ✓ 在 v1 偵測到彈窗")
                                except Exception:
                                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5156, exc_info=True)
                                
                            # 切回 frame 以便後續操作 (或是如果沒開成功要重點)
                            try:
                                self.driver.switch_to.default_content()
                                main_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']")
                                self.driver.switch_to.frame(main_iframe)
                                v1_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='v1'] | //iframe[@id='v1']")
                                self.driver.switch_to.frame(v1_iframe)
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5166, exc_info=True)
                            
                            if cbox_visible:
                                self.log("  ✓ 偵測到彈窗已開啟")
                                popup_opened = True
                                break
                            else:
                                self.log("  ⚠️ 點擊後未偵測到彈窗")
                                
                        except Exception as e:
                            self.log(f"  動作執行失敗: {e}")
                            # 嘗試恢復 Context
                            try:
                                self.driver.switch_to.default_content()
                                main_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']")
                                self.driver.switch_to.frame(main_iframe)
                                v1_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='v1'] | //iframe[@id='v1']")
                                self.driver.switch_to.frame(v1_iframe)
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5185, exc_info=True)
                    
                    if not popup_opened:
                        self.log("  ❌ 嘗試多次仍無法開啟彈窗，跳過此項目")
                        continue
                        
                    # 處理彈窗 (傳入記錄的視窗清單以偵測新視窗)
                    self.log("  進入彈窗內容處理...")
                    
                    # (SmartDL) 嘗試解析目標案件資料夾
                    # 使用 case_info 而非未定義的 notice
                    target_case_folder = None
                    if case_info and case_info.get('case_number'):
                        # 建立一個臨時物件供 _resolve_case_folder 使用
                        class TempInfo:
                            def __init__(self, case_number, party):
                                self.case_number = case_number
                                self.client_name = party
                        temp_info = TempInfo(case_info.get('case_number'), case_info.get('party'))
                        target_case_folder = self._resolve_case_folder(temp_info)
                    
                    self._handle_download_popup(target_case_folder=target_case_folder)
                    
                    # 等待下載
                    self.log("  等待檔案下載完成...")
                    time.sleep(1.5)

                    # 以「每筆案號」為單位歸因本次新增檔案：讓上游通知能說清楚是哪位當事人/哪個法院案號。
                    try:
                        case_meta = {
                            "court": (case_info.get("court") or "") if isinstance(case_info, dict) else "",
                            "case_number": (case_info.get("case_number") or "") if isinstance(case_info, dict) else "",
                            "showyyidno": (case_info.get("showyyidno") or "") if isinstance(case_info, dict) else "",
                            "party": (case_info.get("party") or "") if isinstance(case_info, dict) else "",
                        }
                    except Exception:
                        case_meta = {}

                    def _collect_new_files_for_case(timeout_sec: int = 8) -> list:
                        found = []
                        if not os.path.exists(today_folder):
                            return found
                        start = time.time()
                        while time.time() - start < timeout_sec:
                            try:
                                for filename in os.listdir(today_folder):
                                    if filename.endswith(('.json', '.tmp', '.crdownload')):
                                        continue
                                    fpath = os.path.join(today_folder, filename)
                                    if not os.path.isfile(fpath):
                                        continue
                                    current_mtime = os.path.getmtime(fpath)
                                    old_mtime = existing_file_mtimes.get(filename, 0)
                                    if filename not in existing_file_mtimes or current_mtime > old_mtime:
                                        found.append(filename)
                                        # update baseline so subsequent cases won't "claim" the same file
                                        existing_file_mtimes[filename] = current_mtime
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5243, exc_info=True)
                            time.sleep(0.8)
                        # uniq preserve order
                        seen_nf = set()
                        out = []
                        for x in found:
                            if x in seen_nf:
                                continue
                            seen_nf.add(x)
                            out.append(x)
                        return out

                    new_for_case = _collect_new_files_for_case(timeout_sec=8)
                    if new_for_case:
                        try:
                            label = (case_meta.get("party") or "").strip() or "（未填當事人）"
                            cno = (case_meta.get("showyyidno") or case_meta.get("case_number") or "").strip()
                            if cno:
                                label = f"{label}｜{cno}"
                            self.log(f"  🧳 本案新增檔案 {len(new_for_case)} 份: {label}")
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5264, exc_info=True)
                        yyidno_reg = (case_meta.get("case_number") or case_meta.get("showyyidno") or "").strip()
                        for nf in new_for_case:
                            srcp = os.path.join(today_folder, nf)
                            download_meta_by_file[srcp] = dict(case_meta)
                            # ★ 將檔案登錄到 registry
                            self._register_downloaded(nf, yyidno=yyidno_reg, case_info=case_meta)
                    
                    # 重要: 恢復 frame context 以便點擊下一個下載按鈕
                    # 結構: 根頁面 → main-content iframe → v1 iframe
                    try:
                        self.driver.switch_to.default_content()
                        main_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']")
                        self.driver.switch_to.frame(main_iframe)
                        
                        # 重要：等待 v1 iframe 出現，避免切換太快
                        WebDriverWait(self.driver, 10).until(
                            EC.presence_of_element_located((By.XPATH, "//iframe[@name='v1'] | //iframe[@id='v1']"))
                        )
                        v1_iframe = self.driver.find_element(By.XPATH, "//iframe[@name='v1'] | //iframe[@id='v1']")
                        self.driver.switch_to.frame(v1_iframe)
                        self.log("  ✓ 已恢復 frame context (main-content → v1)")
                        
                        # 重新定位按鈕列表 (防止 Stale)
                        if i < len(download_btns) - 1:
                            pass # 略過重新定位邏輯，依賴 Loop index
                            
                    except Exception as e:
                        self.log(f"  ⚠️ 恢復 frame context 失敗: {e}")
                    
                except Exception as e:
                    self.log(f"  ⚠️ 下載第 {i+1} 個項目失敗: {e}")
            
            # 檢查新下載的檔案
            self.log("  檢查新檔案並歸檔...")
            time.sleep(1.5)

            # 最後保險：若某些下載在 per-case window 之外才落地，仍嘗試補進候選（但 meta 可能空白）。
            try:
                if os.path.exists(today_folder):
                    for fn in os.listdir(today_folder):
                        if fn.endswith(('.json', '.tmp', '.crdownload')):
                            continue
                        fp = os.path.join(today_folder, fn)
                        if not os.path.isfile(fp):
                            continue
                        if fp in download_meta_by_file:
                            continue
                        try:
                            if os.path.getmtime(fp) >= (download_start_time - 1):
                                download_meta_by_file[fp] = {}
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5316, exc_info=True)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5318, exc_info=True)

            candidates = []
            try:
                candidates = sorted(
                    [p for p in download_meta_by_file.keys() if os.path.exists(p)],
                    key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0,
                )
            except Exception:
                candidates = [p for p in download_meta_by_file.keys() if os.path.exists(p)]

            # uniq preserve order
            seen_p = set()
            uniq_candidates = []
            for p in candidates:
                if p in seen_p:
                    continue
                seen_p.add(p)
                uniq_candidates.append(p)
            candidates = uniq_candidates

            self.log(f"  📊 偵測到 {len(candidates)} 個新下載的檔案")
            
            for src in candidates:
                filename = os.path.basename(src)

                # ★ ebook 壓縮檔過濾：法院 eFile 回傳的 ebook_ROW*.zip 內無有效內容，直接刪除
                if filename.lower().startswith("ebook") and filename.lower().endswith(".zip"):
                    self.log(f"  🗑️ [ebook zip] 刪除無用壓縮檔: {filename}")
                    if self.no_delete:
                        self.log(f"  🔒 MAGI_NO_DELETE=1，保留來源檔案: {filename}")
                    else:
                        try:
                            if safe_remove:
                                safe_remove(src, reason="ebook_zip_useless", allow_delete=True, log=self.log)
                            else:
                                os.remove(src)
                        except Exception as e:
                            self.log(f"  ⚠️ 刪除失敗: {e}")
                    continue

                # 計算 MD5 檢查重複
                try:
                    md5 = self._calculate_md5(src)
                        
                    # 檢查是否已存在 (根據 MD5)
                    is_duplicate = False
                    for record in self.md5_records.values():
                        if record.get('md5') == md5:
                            is_duplicate = True
                            break
                        
                    if is_duplicate:
                        self.log(f"  ⏭️ 發現重複檔案 (MD5相同)，跳過: {filename}")
                        if self.no_delete:
                            self.log(f"  🔒 MAGI_NO_DELETE=1，保留來源檔案: {filename}")
                        else:
                            try:
                                if safe_remove:
                                    safe_remove(src, reason="download_dup_md5", allow_delete=True, log=self.log)
                                else:
                                    pass  # safe policy: never delete if safe_remove unavailable
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5381, exc_info=True)
                        continue
                            
                    # ★ 檔案已經在 today_folder，不需要移動，直接記錄
                    downloaded_files.append(src)
                    # 保存「檔案 -> 案件」對照，供歸檔/通知使用（即使最後進 _待歸檔 也能回報當事人與法院案號）。
                    try:
                        if not hasattr(self, "_last_download_meta_by_file"):
                            self._last_download_meta_by_file = {}
                        meta = dict(download_meta_by_file.get(src) or {})
                        self._last_download_meta_by_file[src] = meta
                        self._last_download_meta_by_file[filename] = meta
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5394, exc_info=True)
                        
                    self.md5_records[filename] = {
                        'md5': md5,
                        'download_date': datetime.now().isoformat(),
                        'path': src
                    }
                    self.log(f"  ✅ 下載完成: {filename}")
                        
                except Exception as e:
                    self.log(f"  ⚠️ 檔案處理失敗 {filename}: {e}")
            
            self._save_md5_records()
            self._save_download_registry()
            
            # ★★★ 歸檔到案件資料夾 ★★★
            if downloaded_files and case_info_list:
                self._archive_to_case_folders(downloaded_files, case_info_list)
            
        except Exception as e:
            self.log(f"⚠️ 檢查下載失敗: {e}")
        
        return downloaded_files
    
 
    
    def _handle_download_popup(self, original_handles=None, target_case_folder=None):
        """
        處理「線上閱覽」彈窗並下載檔案
        
        重要發現：彈窗是 jQuery UI Dialog，不是 colorbox！
        
        HTML 結構：
        <div class="ui-dialog" role="dialog">
          <div class="ui-dialog-titlebar">線上閱覽</div>
          <div class="ui-dialog-content">
            <iframe name="IDX_0" ...></iframe>  ← 內容在這裡
          </div>
        </div>
        """
        try:
            self.log("  === 處理線上閱覽彈窗 ===")
            
            # ★ 確保回到主頁面層級 - Dialog 在這裡！
            self.driver.switch_to.default_content()
            
            # Step 1: 等待 jQuery UI Dialog 出現
            try:
                self.log("  等待 jQuery UI Dialog (20秒)...")
                # 使用 presence_of_element_located 因為 visibility 檢查有時會失敗
                # (即使 Dialog 可見，overlay 樣式可能導致 visibility check 失敗)
                dialog = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, ".ui-dialog[role='dialog']"))
                )
                # 額外等待確保 Dialog 完全載入
                time.sleep(0.5)
                self.log("  ✓ jQuery UI Dialog 已出現")
            except TimeoutException:
                self.log("  ❌ 等待 Dialog 逾時")
                
                # Debug
                try:
                    from datetime import datetime
                    ts = int(datetime.now().timestamp())
                    self.driver.save_screenshot(f"debug_no_dialog_{ts}.png")
                    self.log(f"  已儲存截圖: debug_no_dialog_{ts}.png")
                    
                    # 檢查是否有任何 dialog 相關元素
                    dialogs = self.driver.find_elements(By.CSS_SELECTOR, ".ui-dialog, [role='dialog']")
                    self.log(f"  dialog 元素數量: {len(dialogs)}")
                    for i, d in enumerate(dialogs):
                        self.log(f"    [{i}] displayed={d.is_displayed()}, class={d.get_attribute('class')[:50]}")
                except Exception as e:
                    self.log(f"  Debug 資訊取得失敗: {e}")
                return
            
            # Step 2: 切換到 Dialog 內的 iframe (IDX_0)
            try:
                self.log("  尋找 Dialog 內的 iframe (10秒)...")
                # iframe 的 name 是動態的 (IDX_0, IDX_1, etc.)
                dialog_iframe = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, 
                        ".ui-dialog-content iframe, "
                        ".ui-dialog iframe, "
                        "div[role='dialog'] iframe"))
                )
                self.driver.switch_to.frame(dialog_iframe)
                self.log("  ✓ 已切換到 Dialog iframe (IDX_0)")
            except TimeoutException:
                self.log("  ⚠️ 未找到 Dialog iframe，嘗試直接操作內容")
            
            # Step 2.5: 切換到嵌套的 v1 iframe (關鍵！下載按鈕在這裡面)
            # 結構: Dialog → iframe IDX_0 → iframe v1 → 下載按鈕
            try:
                self.log("  尋找嵌套的 v1 iframe (10秒)...")
                time.sleep(1)  # 等待內部 iframe 載入
                
                # 先嘗試用 name 找
                v1_iframe = None
                try:
                    v1_iframe = self.driver.find_element(By.NAME, "v1")
                except Exception:
                    # 也試試看 id
                    try:
                        v1_iframe = self.driver.find_element(By.ID, "v1")
                    except Exception:
                        # 找所有 iframe
                        iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
                        if iframes:
                            self.log(f"  發現 {len(iframes)} 個 iframe，切換到第一個")
                            v1_iframe = iframes[0]
                
                if v1_iframe:
                    self.driver.switch_to.frame(v1_iframe)
                    self.log("  ✓ 已切換到嵌套的 v1 iframe")
                    time.sleep(2)  # 等待內容載入
                else:
                    self.log("  ⚠️ 未找到嵌套的 v1 iframe，嘗試在當前層級操作")
            except Exception as nested_e:
                self.log(f"  ⚠️ 切換嵌套 iframe 時錯誤: {nested_e}")
            
            # Step 3: 等待檔案表格載入 (網路不穩定，增加等待時間)
            self.log("  等待檔案列表載入 (30秒)...")
            time.sleep(1)  # 給 AJAX 載入時間
            
            try:
                WebDriverWait(self.driver, 30).until(
                    EC.presence_of_element_located((By.XPATH, 
                        "//i[contains(@class, 'fa-download')] | "
                        "//button[.//i[contains(@class, 'fa-download')]] | "
                        "//table[@id='content']//tr[@class] | "
                        "//table//tbody//tr"))
                )
                self.log("  ✓ 檔案列表已載入")
            except TimeoutException:
                self.log("  ⚠️ 檔案列表載入逾時")
                
                # 檢查是否無資料
                page_source = self.driver.page_source
                if "無資料" in page_source or "查無資料" in page_source:
                    self.log("  (確認無檔案可下載)")
                    self._close_dialog()
                    return
            
            # Step 4: 尋找下載按鈕 (只找單檔下載按鈕，排除批次下載)
            download_btns = []
            # 關鍵區別：
            # - 單檔下載按鈕: 在 tr#trdata 內, title="下載", width: 30px
            # - 批次下載按鈕: title="單檔批次下載", 有 <span> 文字
            selectors = [
                # 最精確：表格資料行內 (tr#trdata) 的下載按鈕
                "//tr[@id='trdata']//button[@title='下載']",
                # 備用：tablecontext tbody 內的下載按鈕
                "//table[@id='tablecontext']//tbody//tr//button[@title='下載']",
                # 備用：只有 fa-download 圖示沒有 span 文字的按鈕 (排除批次)
                "//tbody//tr[@id='trdata']//button[.//i[contains(@class, 'fa-download')]]",
            ]
            
            for sel in selectors:
                btns = self.driver.find_elements(By.XPATH, sel)
                visible_btns = [b for b in btns if b.is_displayed()]
                if visible_btns:
                    download_btns = visible_btns
                    self.log(f"  ✓ 找到 {len(download_btns)} 個單檔下載按鈕 (selector: {sel[:40]}...)")
                    break
            
            # Step 5: 點擊下載按鈕並處理「第二個彈窗」
            if download_btns:
                # (SmartDL) 預先掃描目標資料夾
                review_root_folder = None
                if target_case_folder:
                    review_root_folder = self._find_review_folder(target_case_folder)
                    if review_root_folder:
                        self.log(f"  🔍 智慧檢查啟用: 將掃描 {os.path.basename(review_root_folder)} 避免重複下載")

                # ★ 重要: 在切換到任何 iframe 之前，取得主視窗 handle
                self.driver.switch_to.default_content()
                main_window_handle = self.driver.current_window_handle
                self.log(f"  記錄主視窗 handle: {main_window_handle}")
                
                # 恢復到 Dialog -> v1 iframe
                try:
                    dialog_iframe_elem = self.driver.find_element(By.CSS_SELECTOR, 
                        ".ui-dialog-content iframe, .ui-dialog iframe, div[role='dialog'] iframe")
                    self.driver.switch_to.frame(dialog_iframe_elem)
                    try:
                        self.driver.switch_to.frame("v1")
                    except Exception:
                        iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
                        if iframes:
                            self.driver.switch_to.frame(iframes[0])
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5586, exc_info=True)
                
                # 記錄當前所有視窗
                current_windows = set(self.driver.window_handles)
                
                for idx, btn in enumerate(download_btns):
                    try:
                        # (SmartDL) 檢查檔案是否已存在
                        if review_root_folder and self.enable_preclick_smart_skip:
                            try:
                                # 嘗試從按鈕所在的列提取檔名
                                # 通常結構: tr -> td -> button
                                # 檔名通常在同一 tr 的某個 td 內，或者 button 的 title/onclick 屬性中
                                row = btn.find_element(By.XPATH, "./ancestor::tr")
                                row_text = row.text
                                
                                # 因為檔名可能不完整，我們檢查 row text 是否包含 "pdf"
                                # 這裡做一個簡單的啟發式檢查：如果 row text 切割出的任何字串出現在資料夾中
                                
                                # 更精確的方法：嘗試找到帶有 .pdf 的連結或文字
                                filename_candidates = []
                                try:
                                    # 找同一列的連結
                                    links = row.find_elements(By.TAG_NAME, "a")
                                    for link in links:
                                        txt = link.text.strip()
                                        if txt: filename_candidates.append(txt)
                                except Exception as e: logger.debug("Failed to find filename links in row: %s", e)
                                
                                # 如果沒連結，用 row text 分割
                                if not filename_candidates:
                                    parts = row_text.split()
                                    filename_candidates = [p for p in parts if len(p) > 3]
                                
                                # 檢查候選檔名
                                found_existing = False
                                for candidate in filename_candidates:
                                    # 假如 candidate 是 "筆錄.pdf"，我們就找是否有這個檔案
                                    # 如果 candidate 不含副檔名，我們加上 .pdf 試試
                                    check_names = [candidate]
                                    if not candidate.lower().endswith('.pdf'):
                                        check_names.append(candidate + ".pdf")
                                        
                                    for name in check_names:
                                        existing_path = self._find_file_recursively(review_root_folder, name)
                                        if existing_path:
                                            self.log(f"  ⏭️ [智慧跳過] 檔案已存在: {name} -> {existing_path}")
                                            try:
                                                self._last_smart_skipped_files.append({
                                                    "file": name,
                                                    "existing_path": existing_path,
                                                    "review_root_folder": review_root_folder,
                                                })
                                            except Exception:
                                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5640, exc_info=True)
                                            found_existing = True
                                            break
                                    if found_existing: break
                                
                                if found_existing:
                                    continue # 跳過此按鈕的點擊
                                    
                            except Exception as smart_e:
                                self.log(f"  ⚠️ 智慧檢查失敗 (不影響下載): {smart_e}")

                        self.log(f"  點擊下載按鈕 {idx+1}/{len(download_btns)}...")
                        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                        time.sleep(0.5)
                        self.driver.execute_script("arguments[0].click();", btn)
                        self.log(f"  ✓ 已點擊")
                        
                        # 等待「第二個彈窗」(下載確認對話框)
                        self.log("  等待下載確認...")
                        
                        # 檢查是否有新視窗 (Polling)
                        start_wait = time.time()
                        found_new_window = False
                        
                        while time.time() - start_wait < 5:
                            try:
                                if len(self.driver.window_handles) > len(current_windows):
                                    found_new_window = True
                                    break
                            except Exception:
                                # 如果無法取得 window_handles，可能是視窗已關閉
                                break
                            time.sleep(0.5)
                        
                        try:
                            new_windows = set(self.driver.window_handles) - current_windows
                        except Exception:
                            new_windows = set()
                            
                        if new_windows:
                            self.log(f"  偵測到新視窗: {len(new_windows)} 個")
                            new_window = new_windows.pop()
                            
                            # ★ 新策略：不切換到新視窗，只等待它自動關閉
                            # 這樣可以避免 Chrome 在視窗切換時崩潰
                            
                            # 等待下載觸發和新視窗自動關閉 (最多 30 秒)
                            start_wait = time.time()
                            while time.time() - start_wait < 30:
                                try:
                                    current_handles = self.driver.window_handles
                                    if new_window not in current_handles:
                                        self.log("  新視窗已自動關閉")
                                        break
                                except Exception as handle_e:
                                    # WebDriver 可能崩潰
                                    self.log(f"  ❌ WebDriver 連線錯誤: {handle_e}")
                                    break
                                time.sleep(1)
                            
                            # ★ CRITICAL: 檢查 WebDriver 是否仍然可用
                            try:
                                _ = self.driver.current_window_handle
                            except Exception as session_e:
                                self.log(f"  ❌ WebDriver 會話已遺失: {session_e}")
                                self.log("  停止處理此案件")
                                break  # 退出檔案下載迴圈
                            
                            # 確保我們在主視窗
                            try:
                                self.driver.switch_to.window(main_window_handle)
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5712, exc_info=True)
                            
                            # 恢復 Frame Context: default_content -> Dialog iframe -> v1
                            time.sleep(0.5)
                            try:
                                self.driver.switch_to.default_content()
                                
                                dialog_iframe_elem = self.driver.find_element(By.CSS_SELECTOR, 
                                    ".ui-dialog-content iframe, .ui-dialog iframe, div[role='dialog'] iframe")
                                self.driver.switch_to.frame(dialog_iframe_elem)
                                
                                try:
                                    self.driver.switch_to.frame("v1")
                                except Exception:
                                    iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
                                    if iframes:
                                        self.driver.switch_to.frame(iframes[0])
                                        
                                self.log("  ✓ 已恢復 Frame Context")
                            except Exception as fe:
                                self.log(f"  ⚠️ 恢復 Frame Context 失敗: {fe}")
                                # 彈窗可能已被移除，需要退出迴圈
                                break
                                    
                        else:
                            time.sleep(1.5)  # 沒有新視窗，等待下載觸發
                        
                    except Exception as e:
                        self.log(f"  點擊失敗: {e}")
                        # 嘗試恢復狀態
                        try:
                            self.driver.switch_to.window(main_window_handle)
                            self.driver.switch_to.default_content()
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5746, exc_info=True)
            else:
                self.log("  ⚠️ 未找到下載按鈕")
            
            # Step 6: 關閉彈窗
            time.sleep(2)
            self._close_dialog()
            
        except Exception as e:
            self.log(f"  ❌ 處理彈窗錯誤: {e}")
            import traceback
            traceback.print_exc()

    def _close_dialog(self):
        """關閉 jQuery UI Dialog 並等待確認關閉"""
        try:
            self.log("  關閉彈窗...")
            
            # ★ 關鍵: 先回到主頁面層級 - Dialog 在這裡！
            self.driver.switch_to.default_content()
            
            # 方法 1: 點擊 X 按鈕
            try:
                close_btn = self.driver.find_element(By.CSS_SELECTOR, 
                    ".ui-dialog-titlebar-close, button[title='Close'], .ui-button-icon-only")
                if close_btn.is_displayed():
                    close_btn.click()
                    self.log("  ✓ 彈窗已關閉 (X 按鈕)")
                    time.sleep(1)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5776, exc_info=True)
            
            # 方法 2: jQuery API
            try:
                self.driver.execute_script("""
                    if (typeof $ !== 'undefined') {
                        $('.ui-dialog-content').dialog('close');
                        $('.ui-dialog').remove();
                    }
                """)
                self.log("  ✓ 彈窗已關閉 (JS)")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5788, exc_info=True)
            
            # 方法 3: 按 ESC
            try:
                from selenium.webdriver.common.keys import Keys
                self.driver.find_element(By.TAG_NAME, "body").send_keys(Keys.ESCAPE)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5795, exc_info=True)
            
            # ★ 等待 Dialog 真正消失
            try:
                WebDriverWait(self.driver, 5).until(
                    EC.invisibility_of_element((By.CSS_SELECTOR, ".ui-dialog[role='dialog']"))
                )
                self.log("  ✓ 確認 Dialog 已消失")
            except Exception:
                # 強制移除
                try:
                    self.driver.execute_script("$('.ui-dialog').remove(); $('.ui-widget-overlay').remove();")
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5808, exc_info=True)
            
            # 額外等待確保頁面穩定
            time.sleep(1)
                
        except Exception as e:
            self.log(f"  關閉彈窗失敗: {e}")

    def _close_colorbox_simple(self):
        """保留舊方法作為備用 - 改為呼叫 _close_dialog"""
        self._close_dialog()

    def _archive_to_case_folders(self, downloaded_files: List[str], case_info_list: List[Dict]):
        """
        將下載的檔案歸檔到案件資料夾 (K 槽)
        
        使用資料庫查詢: 用 court_case_number 找到 folder_path
        
        Args:
            downloaded_files: 下載的檔案路徑列表
            case_info_list: 案件資訊列表 (包含 court, case_number, party)
        """
        self.log("📂 開始歸檔到案件資料夾...")

        # 供 orchestrator/排程做「可讀回報」：下載了哪個當事人/案號/歸檔到哪。
        # 不改變既有回傳型態（check_and_download_available 仍回 List[str]），僅在 manager 物件上留下最後一次摘要。
        # 重要：這些資訊僅用於回報，不做任何刪除/重寫。
        self._last_archive_report = {
            "ts": datetime.now().isoformat(),
            "downloaded_files": [str(x) for x in (downloaded_files or [])],
            "items": [],  # list[dict]: {party, court_case_no, folder, file, dst, action}
            "cases": [],  # resolved candidates
            "staged": [],
        }
        # file_path or basename -> case meta (party/showyyidno/yyidno/court)
        meta_by_file = getattr(self, "_last_download_meta_by_file", {}) or {}

        if not downloaded_files:
            self.log("  ℹ️ 無下載檔案，略過歸檔")
            return

        if not case_info_list:
            self.log("  ⚠️ 無案件資訊，改存到 _待歸檔")
            case_info_list = []

        if not self.db:
            self.log("  ⚠️ 資料庫未連接，改用資料夾掃描/快取降級模式歸檔")

        # --- 建立案件 -> folder_path 映射 ---
        # 重要：優先使用「實際下載檔案」對應的 case meta，避免清單污染造成誤歸檔。
        resolved = []

        class _Tmp:
            def __init__(self, case_number: str, client_name: str, court_case_no: str = ""):
                self.case_number = case_number
                self.client_name = client_name
                self.court_case_no = court_case_no
                self.laf_case_no = ""

        case_rows: List[Dict[str, str]] = []
        seen_case_rows = set()

        for fp in (downloaded_files or []):
            fn = os.path.basename(fp)
            m = meta_by_file.get(fp) or meta_by_file.get(fn) or {}
            if not isinstance(m, dict):
                continue
            showyyidno = str(m.get("showyyidno") or "").strip()
            yyidno = str(m.get("case_number") or "").strip()
            party = str(m.get("party") or "").strip()
            key = showyyidno or yyidno or party
            if not key:
                continue
            sig = (showyyidno, yyidno, party)
            if sig in seen_case_rows:
                continue
            seen_case_rows.add(sig)
            case_rows.append({"showyyidno": showyyidno, "yyidno": yyidno, "party": party, "key": key})

        # 只有在檔案級 metadata 完全缺失時，才回退舊清單（風險較高）。
        if not case_rows and case_info_list:
            self.log("  ⚠️ 無檔案級案件資訊，回退使用頁面清單解析（可能較不精確）")
            for ci in (case_info_list or []):
                showyyidno = str(ci.get("showyyidno") or "").strip()
                yyidno = str(ci.get("case_number") or "").strip()
                party = str(ci.get("party") or "").strip()
                key = showyyidno or yyidno or party
                if not key:
                    continue
                sig = (showyyidno, yyidno, party)
                if sig in seen_case_rows:
                    continue
                seen_case_rows.add(sig)
                case_rows.append({"showyyidno": showyyidno, "yyidno": yyidno, "party": party, "key": key})

        for ci in case_rows:
            showyyidno = (ci.get("showyyidno") or "").strip()
            yyidno = (ci.get("yyidno") or "").strip()
            party = (ci.get("party") or "").strip()
            key = (ci.get("key") or "").strip() or (showyyidno or yyidno or party)
            if not key:
                continue
            tmp = _Tmp(case_number=(yyidno or showyyidno), client_name=party, court_case_no=showyyidno)
            folder = self._resolve_case_folder(tmp) or ""
            if folder and os.path.isdir(folder):
                resolved.append({"key": key, "showyyidno": showyyidno, "yyidno": yyidno, "party": party, "folder": folder})

        try:
            self._last_archive_report["cases"] = list(resolved)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5918, exc_info=True)
        try:
            learned = 0
            for r in resolved:
                learned += int(
                    self._remember_auto_archive_mapping(
                        folder_path=(r.get("folder") or ""),
                        yyidno=(r.get("yyidno") or ""),
                        court_case_no=(r.get("showyyidno") or ""),
                        party=(r.get("party") or ""),
                    )
                )
            if learned > 0:
                self.log(f"  🧠 已更新歸檔映射 {learned} 筆（下次可自動歸類）")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5933, exc_info=True)

        # uniq folders
        uniq_folders = []
        seen = set()
        for r in resolved:
            fp = r.get("folder") or ""
            if not fp:
                continue
            if fp in seen:
                continue
            seen.add(fp)
            uniq_folders.append(fp)

        # --- Helper: archive one file into a matched folder ---
        # folder -> best meta (prefer showyyidno present)
        folder_meta = {}
        for r in resolved:
            f = (r.get("folder") or "").strip()
            if not f:
                continue
            if f not in folder_meta:
                folder_meta[f] = r
                continue
            if not (folder_meta[f].get("showyyidno") or "").strip() and (r.get("showyyidno") or "").strip():
                folder_meta[f] = r

        def _archive_one(file_path: str, matched_folder: str) -> dict:
            if not matched_folder or not os.path.isdir(matched_folder):
                return {"ok": False, "dst": "", "action": "no_folder"}
            filename = os.path.basename(file_path)

            # 動態尋找包含「閱卷」的子資料夾
            review_folder = None
            try:
                for subfolder in os.listdir(matched_folder):
                    if '閱卷' in subfolder:
                        review_folder = os.path.join(matched_folder, subfolder)
                        break
            except Exception:
                review_folder = None

            if not review_folder:
                review_folder = os.path.join(matched_folder, "02_閱卷資料")

            os.makedirs(review_folder, exist_ok=True)

            # avoid duplicates
            try:
                for root, _dirs, files in os.walk(review_folder):
                    if filename in files:
                        self.log(f"  ⏭️ 已存在，跳過: {filename}")
                        # 回報用：仍視為 ok，dst 以現有路徑為準（若能找到）。
                        existing = os.path.join(root, filename)
                        return {"ok": True, "dst": existing, "action": "exists_skip"}
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5989, exc_info=True)

            today_str = datetime.now().strftime("%Y%m%d")
            date_folder = os.path.join(review_folder, today_str)
            os.makedirs(date_folder, exist_ok=True)
            dst_path = os.path.join(date_folder, filename)

            try:
                if os.path.exists(dst_path):
                    if self.no_delete:
                        self.log(f"  ⏭️ 目標已存在，保留原檔案: {filename}")
                        return {"ok": True, "dst": dst_path, "action": "target_exists_keep_src"}
                    try:
                        if safe_remove:
                            safe_remove(file_path, reason="archive_target_exists", allow_delete=True, log=self.log)
                            self.log(f"  ⏭️ 目標已存在，已隔離原檔案: {filename}")
                        else:
                            # Safe policy: never delete if safe_remove unavailable.
                            self.log(f"  🔒 目標已存在，但 safe_remove 不可用，保留原檔案: {filename}")
                        return {"ok": True, "dst": dst_path, "action": "target_exists_isolate_src"}
                    except Exception as del_e:
                        self.log(f"  ⚠️ 隔離原檔案失敗: {del_e}")
                        return {"ok": False, "dst": dst_path, "action": "target_exists_isolate_failed"}

                if self.no_delete:
                    shutil.copy2(file_path, dst_path)
                    self.log(f"  📁 歸檔並複製(保留原檔): {filename}")
                else:
                    shutil.move(file_path, dst_path)
                    self.log(f"  📁 歸檔並移動: {filename}")
                return {"ok": True, "dst": dst_path, "action": "copied" if self.no_delete else "moved"}
            except Exception as e:
                self.log(f"  ❌ 歸檔失敗 {filename}: {e}")
                return {"ok": False, "dst": dst_path, "action": "failed"}

        # --- Decide strategy ---
        staging_base = os.path.join(self.download_folder, "_待歸檔", datetime.now().strftime("%Y%m%d"))
        os.makedirs(staging_base, exist_ok=True)

        archived_count = 0
        staged_count = 0

        # 1) no resolved folder → stage all
        if not uniq_folders:
            self.log("  ⚠️ 找不到任何匹配的案件資料夾，全部先放 _待歸檔")
            for fp in downloaded_files:
                if not os.path.exists(fp):
                    continue
                fn = os.path.basename(fp)
                dst = os.path.join(staging_base, fn)
                try:
                    if self.no_delete:
                        shutil.copy2(fp, dst)
                    else:
                        shutil.move(fp, dst)
                    staged_count += 1
                    try:
                        meta = meta_by_file.get(fp) or meta_by_file.get(fn) or {}
                        party = (meta.get("party") or "").strip()
                        court_case_no = (meta.get("showyyidno") or meta.get("case_number") or "").strip()
                        self._last_archive_report["staged"].append({"file": fn, "dst": dst})
                        self._last_archive_report["items"].append(
                            {"party": party, "court_case_no": court_case_no, "folder": "", "file": fn, "dst": dst, "action": "staged_no_case"}
                        )
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6054, exc_info=True)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6056, exc_info=True)
            self.log(f"✅ 降級歸檔完成（待歸檔）: {staged_count}/{len(downloaded_files)}")
            return

        # 2) single folder → archive all
        if len(uniq_folders) == 1:
            matched_folder = uniq_folders[0]
            self.log(f"  ✓ 解析到唯一案件資料夾: .../{os.path.basename(matched_folder)}")
            for fp in downloaded_files:
                if not os.path.exists(fp):
                    continue
                res = _archive_one(fp, matched_folder)
                if res.get("ok"):
                    archived_count += 1
                try:
                    fn = os.path.basename(fp)
                    # Prefer per-file meta (most accurate), fallback to folder meta
                    m1 = meta_by_file.get(fp) or meta_by_file.get(fn) or {}
                    m2 = folder_meta.get(matched_folder, {}) or {}
                    party = (m1.get("party") or m2.get("party") or "").strip()
                    court_case_no = (m1.get("showyyidno") or m1.get("case_number") or m2.get("showyyidno") or m2.get("yyidno") or "").strip()
                    self._last_archive_report["items"].append(
                        {
                            "party": party,
                            "court_case_no": court_case_no,
                            "folder": matched_folder,
                            "file": fn,
                            "dst": res.get("dst") or "",
                            "action": res.get("action") or "",
                        }
                    )
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6088, exc_info=True)
            self.log(f"✅ 歸檔完成: {archived_count}/{len(downloaded_files)} 個檔案")
            return

        # 3) multiple folders → 先精準歸因；是否啟用檔名啟發式由設定決定
        self.log(f"  ⚠️ 解析到多個案件資料夾（{len(uniq_folders)} 個），先以精準歸因處理其餘檔案")

        # 3-A) 先用「每檔案已歸因的案件 meta」做精準歸檔（最可靠），剩下的才走檔名啟發式/待歸檔。
        remaining_files = []
        try:
            class _Tmp2:
                def __init__(self, case_number: str, client_name: str, court_case_no: str = ""):
                    self.case_number = case_number
                    self.client_name = client_name
                    self.court_case_no = court_case_no
                    self.laf_case_no = ""

            for fp in downloaded_files:
                if not os.path.exists(fp):
                    continue
                fn = os.path.basename(fp)
                m1 = meta_by_file.get(fp) or meta_by_file.get(fn) or {}
                yyidno = (m1.get("case_number") or "").strip()
                showyyidno = (m1.get("showyyidno") or "").strip()
                party = (m1.get("party") or "").strip()

                if yyidno or showyyidno or party:
                    tmp = _Tmp2(case_number=yyidno or showyyidno, client_name=party, court_case_no=showyyidno)
                    folder = self._resolve_case_folder(tmp) or ""
                    if folder and os.path.isdir(folder):
                        res = _archive_one(fp, folder)
                        if res.get("ok"):
                            archived_count += 1
                        report_item = {
                            "party": party,
                            "court_case_no": (showyyidno or yyidno).strip(),
                            "folder": folder,
                            "file": fn,
                            "dst": res.get("dst") or "",
                            "action": res.get("action") or "",
                        }
                        try:
                            self._last_archive_report["items"].append(report_item)
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6132, exc_info=True)
                        continue

                remaining_files.append(fp)
        except Exception:
            remaining_files = list(downloaded_files)

        if not remaining_files:
            self.log(f"✅ 依檔案歸因精準歸檔完成: {archived_count}/{len(downloaded_files)} 個檔案")
            return

        if not self.allow_filename_heuristic_archive:
            self.log("  ⚠️ 仍有無法精準歸因檔案；已停用檔名啟發式自動分派，全部改放 _待歸檔")
            staged_now = []
            for fp in remaining_files:
                if not os.path.exists(fp):
                    continue
                fn = os.path.basename(fp)
                dst = os.path.join(staging_base, fn)
                try:
                    if self.no_delete:
                        shutil.copy2(fp, dst)
                    else:
                        shutil.move(fp, dst)
                    staged_count += 1
                    staged_now.append({"file": fn, "dst": dst})
                    try:
                        meta = meta_by_file.get(fp) or meta_by_file.get(fn) or {}
                        self._last_archive_report["staged"].append({"file": fn, "dst": dst})
                        self._last_archive_report["items"].append(
                            {
                                "party": (meta.get("party") or "").strip(),
                                "court_case_no": (meta.get("showyyidno") or meta.get("case_number") or "").strip(),
                                "folder": "",
                                "file": fn,
                                "dst": dst,
                                "action": "staged_no_filename_heuristic",
                                "reason": "multi_case_remaining_files_without_precise_mapping",
                            }
                        )
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6173, exc_info=True)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6175, exc_info=True)

            try:
                rep_path = os.path.join(staging_base, "archive_report.json")
                with open(rep_path, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "cases": resolved,
                            "staged": staged_now,
                            "archived": [],
                        },
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
                self.log(f"  🧾 已輸出歸檔報告: {rep_path}")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6192, exc_info=True)

            self.log(f"✅ 歸檔完成: {archived_count}/{len(downloaded_files)}；待歸檔: {staged_count}")
            return

        # build token map for cases
        case_tokens = []

        def _collect_case_tokens(text: str) -> list[tuple[str, str, str]]:
            out = []
            raw = (text or "").strip()
            if not raw:
                return out
            y, ct, num = self._parse_court_case_no(raw)
            if y and ct and num:
                out.append((self._norm(y), self._norm(ct), self._norm(num)))
            # pattern like: 113_偵_002746 / 113偵2746
            try:
                m = re.search("(\\d{2,3})[_\\-\\s]*([\u4e00-\u9fffA-Za-z]{1,8})[_\\-\\s]*0*(\\d{1,8})", raw)
                if m:
                    out.append((self._norm(m.group(1)), self._norm(m.group(2)), self._norm(m.group(3))))
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6214, exc_info=True)
            uniq = []
            seen = set()
            for t in out:
                if t in seen:
                    continue
                seen.add(t)
                uniq.append(t)
            return uniq

        for r in resolved:
            folder = (r.get("folder") or "").strip()
            if not folder:
                continue
            candidates = []
            candidates.extend(_collect_case_tokens((r.get("showyyidno") or "").strip()))
            candidates.extend(_collect_case_tokens((r.get("yyidno") or "").strip()))
            candidates.extend(_collect_case_tokens(os.path.basename(folder)))
            for y, ct, num in candidates:
                if y and ct and num:
                    case_tokens.append({"folder": folder, "year": y, "ct": ct, "num": num})

        # de-dup token rows
        _seen_tok = set()
        _uniq_tok = []
        for t in case_tokens:
            sig = (t.get("folder"), t.get("year"), t.get("ct"), t.get("num"))
            if sig in _seen_tok:
                continue
            _seen_tok.add(sig)
            _uniq_tok.append(t)
        case_tokens = _uniq_tok

        def _pick_folder_by_filename(filename: str) -> str:
            fn = (filename or "").strip()
            norm_fn = self._norm(fn)
            y0, ct0, num0 = self._parse_court_case_no(fn)
            y0 = self._norm(y0)
            ct0 = self._norm(ct0)
            num0 = self._norm(num0)
            # fallback pattern for OCR-ish filename: 113_偵_002746...
            if not (y0 and ct0 and num0):
                m = re.search("(\\d{2,3})[_\\-\\s]*([\u4e00-\u9fffA-Za-z]{1,8})[_\\-\\s]*0*(\\d{1,8})", fn)
                if m:
                    y0, ct0, num0 = self._norm(m.group(1)), self._norm(m.group(2)), self._norm(m.group(3))

            scores = {}
            for t in case_tokens:
                folder = t.get("folder") or ""
                if not folder:
                    continue
                score = 0
                if num0 and t["num"] == num0:
                    score += 4
                elif t["num"] and t["num"] in norm_fn:
                    score += 2
                if y0 and t["year"] == y0:
                    score += 2
                elif t["year"] and t["year"] in norm_fn:
                    score += 1
                if ct0 and (ct0 == t["ct"] or ct0 in t["ct"] or t["ct"] in ct0):
                    score += 2
                elif t["ct"] and t["ct"] in norm_fn:
                    score += 1
                if score > 0:
                    scores[folder] = max(score, scores.get(folder, 0))

            if not scores:
                return ""
            ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
            if len(ranked) == 1:
                return ranked[0][0]
            # only accept when winner is clearly better than 2nd place
            if ranked[0][1] >= ranked[1][1] + 2:
                return ranked[0][0]
            return ""

        report = {"cases": resolved, "staged": [], "archived": []}

        for fp in remaining_files:
            if not os.path.exists(fp):
                continue
            fn = os.path.basename(fp)
            target = _pick_folder_by_filename(fn)
            if target:
                res = _archive_one(fp, target)
                if res.get("ok"):
                    archived_count += 1
                report["archived"].append({"file": fn, "folder": target, "dst": res.get("dst") or "", "action": res.get("action") or ""})
                try:
                    # Prefer per-file meta (most accurate), fallback to folder meta
                    m1 = meta_by_file.get(fp) or meta_by_file.get(fn) or {}
                    meta = folder_meta.get(target, {}) or {}
                    self._last_archive_report["items"].append(
                        {
                            "party": (m1.get("party") or meta.get("party") or "").strip(),
                            "court_case_no": (m1.get("showyyidno") or m1.get("case_number") or meta.get("showyyidno") or meta.get("yyidno") or "").strip(),
                            "folder": target,
                            "file": fn,
                            "dst": res.get("dst") or "",
                            "action": res.get("action") or "",
                        }
                    )
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6318, exc_info=True)
                    continue

            # stage
            dst = os.path.join(staging_base, fn)
            try:
                if self.no_delete:
                    shutil.copy2(fp, dst)
                else:
                    shutil.move(fp, dst)
                staged_count += 1
                report["staged"].append({"file": fn})
                try:
                    meta = meta_by_file.get(fp) or meta_by_file.get(fn) or {}
                    party = (meta.get("party") or "").strip()
                    court_case_no = (meta.get("showyyidno") or meta.get("case_number") or "").strip()
                    self._last_archive_report["staged"].append({"file": fn, "dst": dst})
                    self._last_archive_report["items"].append(
                        {
                            "party": party,
                            "court_case_no": court_case_no,
                            "folder": "",
                            "file": fn,
                            "dst": dst,
                            "action": "staged_ambiguous",
                            "reason": "case_folder_ambiguous_or_insufficient_tokens",
                        }
                    )
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6347, exc_info=True)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6349, exc_info=True)

        # write report for manual triage
        try:
            rep_path = os.path.join(staging_base, "archive_report.json")
            with open(rep_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            self.log(f"  🧾 已輸出歸檔報告: {rep_path}")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6358, exc_info=True)

        try:
            # 完成後同步最後一份（多案模式）摘要，供上游回報使用
            self._last_archive_report["cases"] = list(resolved)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6364, exc_info=True)

        self.log(f"✅ 歸檔完成: {archived_count}/{len(downloaded_files)}；待歸檔: {staged_count}")

    def _resolve_case_folder(self, info) -> str:
        """根據案件資訊找到對應的案件資料夾路徑"""
        yyidno = getattr(info, "case_number", "") or ""
        party = getattr(info, "client_name", "") or ""
        laf_no = getattr(info, "laf_case_no", "") or ""
        court_case_no = getattr(info, "court_case_no", "") or ""

        if not (yyidno or laf_no or court_case_no or party):
            return None

        # 0) cache
        try:
            if court_case_no:
                p = self.case_folder_cache.get(f"court_case_no:{court_case_no}")
                if p and os.path.exists(p):
                    return p
            if laf_no:
                p = self.case_folder_cache.get(f"laf_case_no:{laf_no}")
                if p and os.path.exists(p):
                    return p
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6389, exc_info=True)

        # 0.5) 既有歸檔映射（由歷次成功歸檔自動累積）
        folder_path = self._resolve_case_folder_from_manual_map(
            yyidno=yyidno,
            court_case_no=court_case_no,
            laf_case_no=laf_no,
            party=party,
        )
        if folder_path:
            try:
                self._sync_court_case_number_to_db_if_safe(
                    folder_path=folder_path,
                    court_case_no=court_case_no,
                    party=party,
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6406, exc_info=True)
            return folder_path

        # 1) DB（精確）查詢
        folder_path = self._resolve_case_folder_from_db(
            yyidno=yyidno,
            court_case_no=court_case_no,
            party=party,
        )
        if folder_path:
            try:
                self._remember_auto_archive_mapping(
                    folder_path=folder_path,
                    yyidno=yyidno,
                    court_case_no=court_case_no,
                    laf_case_no=laf_no,
                    party=party,
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6425, exc_info=True)
            try:
                self._sync_court_case_number_to_db_if_safe(
                    folder_path=folder_path,
                    court_case_no=court_case_no,
                    party=party,
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6433, exc_info=True)
            return folder_path
                
        # 2) DB 不可用或 DB 無結果 → 資料夾掃描降級
        folder_path = self._resolve_case_folder_by_scan(
            court_case_no=court_case_no or yyidno,
            laf_case_no=laf_no,
            party=party,
        )
        if folder_path:
            try:
                if court_case_no:
                    self.case_folder_cache[f"court_case_no:{court_case_no}"] = folder_path
                if laf_no:
                    self.case_folder_cache[f"laf_case_no:{laf_no}"] = folder_path
                self._save_case_folder_cache()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6450, exc_info=True)
            try:
                self._remember_auto_archive_mapping(
                    folder_path=folder_path,
                    yyidno=yyidno,
                    court_case_no=court_case_no,
                    laf_case_no=laf_no,
                    party=party,
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6460, exc_info=True)
            try:
                self._sync_court_case_number_to_db_if_safe(
                    folder_path=folder_path,
                    court_case_no=court_case_no,
                    party=party,
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6468, exc_info=True)
            return folder_path

        return None

    def _court_case_numbers_match(self, left: str, right: str) -> bool:
        lft = str(left or "").strip()
        rgt = str(right or "").strip()
        if not lft or not rgt:
            return False

        y1, ct1, n1 = self._parse_court_case_no(lft)
        y2, ct2, n2 = self._parse_court_case_no(rgt)
        if y1 and ct1 and n1 and y2 and ct2 and n2:
            try:
                return (
                    self._norm(y1) == self._norm(y2)
                    and self._norm(ct1) == self._norm(ct2)
                    and str(int(n1)) == str(int(n2))
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6489, exc_info=True)

        return self._manual_key_norm(lft) == self._manual_key_norm(rgt)

    def _sync_court_case_number_to_db_if_safe(self, folder_path: str = "", court_case_no: str = "", party: str = "") -> int:
        if not self.db:
            return 0
        if str(os.environ.get("MAGI_FILE_REVIEW_SYNC_DB_COURT_CASE_NUMBER", "1")).strip().lower() not in {"1", "true", "yes", "on"}:
            return 0

        target_case_no = str(court_case_no or "").strip()
        target_folder = self._to_local_case_path(folder_path or "")
        target_party = str(party or "").strip()
        if not target_case_no or not target_folder or not target_party:
            return 0
        if not self._looks_like_human_party_name(target_party):
            return 0

        y, ct, num = self._parse_court_case_no(target_case_no)
        if not (y and ct and num):
            return 0

        try:
            rows = self.db.execute(
                "SELECT `id`, `case_number`, `client_name`, `court_case_number`, `folder_path` "
                "FROM `cases` "
                "WHERE `client_name` = %s "
                "AND `folder_path` IS NOT NULL AND `folder_path` != '' "
                "LIMIT 12",
                (target_party,),
                fetch='all'
            ) or []
        except Exception as e:
            self.log(f"  ⚠️ 查詢 DB 法院案號補填候選失敗: {e}")
            return 0

        if isinstance(rows, dict):
            rows = [rows]

        matches = []
        for row in rows:
            raw_path = str((row or {}).get("folder_path") or "").strip()
            if not raw_path:
                continue
            try:
                local_path = self.db.translate_path_to_local(raw_path) if hasattr(self.db, "translate_path_to_local") else raw_path
            except Exception:
                local_path = raw_path
            local_path = self._to_local_case_path(local_path)
            if local_path == target_folder:
                matches.append(row)

        if len(matches) != 1:
            if len(matches) > 1:
                self.log(f"  ⚠️ DB 法院案號補填跳過：同一路徑找到多筆 rows（{len(matches)}）")
            return 0

        row = matches[0] or {}
        current = str(row.get("court_case_number") or "").strip()
        if self._court_case_numbers_match(current, target_case_no):
            return 0
        if current:
            self.log(
                f"  ℹ️ DB 已有不同法院案號，為避免誤改略過自動補填: "
                f"{str(row.get('case_number') or '').strip()} {current} -> {target_case_no}"
            )
            return 0

        try:
            self.db.execute(
                "UPDATE `cases` SET `court_case_number` = %s WHERE `id` = %s LIMIT 1",
                (target_case_no, row.get("id")),
            )
            self.log(
                f"  📝 已自動補填 DB 法院案號: "
                f"{str(row.get('case_number') or '').strip()} -> {target_case_no}"
            )
            return 1
        except Exception as e:
            self.log(f"  ⚠️ 自動補填 DB 法院案號失敗: {e}")
            return 0

    def _resolve_case_folder_by_scan(self, court_case_no: str = "", laf_case_no: str = "", party: str = "") -> str:
        """
        DB 不可用時的降級策略：
        - 優先用法扶案號 marker（_laf_case_number.txt）
        - 其他高風險掃描（法院案號 token / 當事人名稱）預設關閉，
          避免把檔案歸到錯誤案件；必要時可用 MAGI_ALLOW_RISKY_CASE_SCAN=1 開啟。
        """
        roots = self._get_case_root_candidates()
        if not roots:
            return ""

        laf_case_no = (laf_case_no or "").strip()
        court_case_no = (court_case_no or "").strip()
        party = (party or "").strip()

        # A) 法扶 marker
        if laf_case_no:
            for r in roots:
                hit = self._scan_by_laf_marker(r, laf_case_no)
                if hit:
                    return hit

        if not self.allow_risky_case_scan:
            return ""

        # B) 法院案號
        if court_case_no:
            y, ct, num = self._parse_court_case_no(court_case_no)
            if y and ct and num:
                for r in roots:
                    hit = self._scan_by_court_tokens(r, y, ct, num)
                    if hit:
                        return hit

        # C) 當事人（最後手段，容易撞名）
        if party:
            for r in roots:
                hit = self._scan_by_party_name(r, party)
                if hit:
                    return hit

        return ""

    @staticmethod
    def _is_supported_review_upload(path: str) -> bool:
        ext = Path(path).suffix.lower()
        return ext in {".pdf", ".png", ".jpg", ".jpeg"}

    def _score_review_upload_candidate(
        self,
        path: str,
        primary_terms: List[str],
        secondary_terms: Optional[List[str]] = None,
        banned_terms: Optional[List[str]] = None,
    ) -> int:
        text = self._norm(str(path))
        score = 0
        for term in primary_terms or []:
            t = self._norm(term)
            if t and t in text:
                score += 50
        for term in secondary_terms or []:
            t = self._norm(term)
            if t and t in text:
                score += 12
        for term in banned_terms or []:
            t = self._norm(term)
            if t and t in text:
                score -= 40

        parts = list(Path(path).parts)
        if any("01_法扶資料" in p for p in parts):
            score += 20
        if any("02_開辦資料" in p for p in parts):
            score += 12
        if any("專員來信" in p for p in parts):
            score -= 20
        return score

    def _ola_upload_attachment(self, file_path: str, file_remark: str = "") -> bool:
        """
        透過 OLA 官方頁面上傳附件。
        OLA 使用 bootbox dialog + 隱藏 uploadform 的 multipart POST 機制。
        策略：
          方法 A: 觸發 bootbox dialog → 等待 file input 出現 → 填入 → 確定
          方法 B: 直接操作隱藏的 uploadform（不開 dialog）
        """
        # ===== 方法 A: 透過 bootbox dialog =====
        try:
            return self._ola_upload_via_dialog(file_path, file_remark)
        except Exception as e:
            self.log(f"    ⚠️ dialog 上傳路徑失敗: {e}，嘗試直接表單路徑")

        # ===== 方法 B: 直接操作隱藏的 uploadform =====
        return self._ola_upload_via_hidden_form(file_path, file_remark)

    def _ola_upload_via_dialog(self, file_path: str, file_remark: str = "") -> bool:
        """透過 bootbox dialog 上傳。"""
        # 1. 點擊「上傳附件」按鈕
        clicked = self.driver.execute_script("""
        var btns = document.querySelectorAll('button');
        for (var i = 0; i < btns.length; i++) {
            var t = (btns[i].innerText || '').replace(/\\s+/g, '');
            if (t.indexOf('上傳附件') >= 0) { btns[i].click(); return 'btn'; }
        }
        if (typeof doUpDataFile === 'function') { doUpDataFile(); return 'fn'; }
        return '';
        """) or ""
        if not clicked:
            raise RuntimeError("找不到上傳按鈕")
        self.log(f"    ✓ 已觸發上傳 dialog: {clicked}")
        time.sleep(1.5)  # 等 bootbox 動畫

        # 2. 等待 dialog 中的 file input 出現
        file_input = None
        try:
            file_input = WebDriverWait(self.driver, 8).until(
                lambda d: self._find_visible_or_any_file_input(d)
            )
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6691, exc_info=True)

        # Fallback: try JS injection to make file input visible
        if file_input is None:
            try:
                file_input_made = self.driver.execute_script("""
                // The bootbox dialog clones <span id="uploadfile"> content
                // Try to find file input anywhere in dialog
                var selectors = [
                    'div.bootbox-body input[type="file"]',
                    'div.modal-body input[type="file"]',
                    'div.bootbox input[type="file"]',
                    'div.modal input[type="file"]',
                    'input[type="file"]',
                    '#file',
                    'input[name="file"]'
                ];
                for (var s = 0; s < selectors.length; s++) {
                    var el = document.querySelector(selectors[s]);
                    if (el) {
                        el.style.display = 'block';
                        el.style.visibility = 'visible';
                        el.style.opacity = '1';
                        el.style.height = '30px';
                        el.style.width = '300px';
                        el.removeAttribute('hidden');
                        var p = el.parentElement;
                        while (p && p !== document.body) {
                            p.style.display = 'block';
                            p.style.visibility = 'visible';
                            p.style.overflow = 'visible';
                            p = p.parentElement;
                        }
                        return true;
                    }
                }
                return false;
                """)
                if file_input_made:
                    time.sleep(0.5)
                    for sel in ["div.bootbox input[type='file']",
                                "div.modal input[type='file']",
                                "input[type='file']",
                                "#file",
                                "input[name='file']"]:
                        try:
                            el = self.driver.find_element(By.CSS_SELECTOR, sel)
                            if el:
                                file_input = el
                                break
                        except Exception:
                            continue
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6744, exc_info=True)

        if file_input is None:
            raise RuntimeError("dialog 中找不到 file input")

        # 3. 確保 file input 可見並填入路徑
        self.driver.execute_script(
            "var e=arguments[0];"
            "e.style.display='block';e.style.visibility='visible';"
            "e.style.opacity='1';e.style.height='auto';e.style.width='auto';"
            "e.removeAttribute('hidden');",
            file_input,
        )
        file_input.send_keys(file_path)
        self.log(f"    ✓ 已填入檔案: {os.path.basename(file_path)}")

        # 4. 填寫說明
        if file_remark:
            try:
                for sel in ["div.bootbox input[name='filermk']",
                            "div.modal.in input[name='filermk']",
                            "input[name='filermk']"]:
                    try:
                        el = self.driver.find_element(By.CSS_SELECTOR, sel)
                        if el:
                            el.clear()
                            el.send_keys(file_remark)
                            break
                    except Exception:
                        continue
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6775, exc_info=True)

        # 5. 點確定
        confirmed = self.driver.execute_script("""
        var sels = ['div.bootbox', 'div.modal.in', 'div.modal'];
        for (var s = 0; s < sels.length; s++) {
            var dlg = document.querySelector(sels[s]);
            if (!dlg) continue;
            var btns = dlg.querySelectorAll('button');
            for (var i = 0; i < btns.length; i++) {
                var t = (btns[i].title || '').trim();
                var txt = (btns[i].innerText || '').replace(/\\s+/g, '');
                if (t === 'uploadData' || txt === '確定') { btns[i].click(); return 'ok'; }
            }
        }
        return '';
        """) or ""
        if not confirmed:
            raise RuntimeError("找不到確定按鈕")
        self.log("    ✓ 已點擊確定上傳")
        time.sleep(3.0)
        return True

    def _ola_upload_via_hidden_form(self, file_path: str, file_remark: str = "") -> bool:
        """直接操作隱藏的 uploadform 表單上傳（不需 dialog）。"""
        # 找到隱藏的 #file input（在 span#uploadfile 內）
        file_input = None
        for sel in ["#uploadfile input[type='file']",
                     "form#uploadform input[type='file']",
                     "input#file[name='file']"]:
            try:
                el = self.driver.find_element(By.CSS_SELECTOR, sel)
                if el:
                    file_input = el
                    break
            except Exception:
                continue

        if file_input is None:
            # 最後嘗試：找任何 file input
            try:
                all_fi = self.driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
                if all_fi:
                    file_input = all_fi[0]
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6820, exc_info=True)

        if file_input is None:
            raise RuntimeError("頁面上找不到任何 file input")

        # 確保可見
        self.driver.execute_script(
            "var e=arguments[0];"
            "e.style.display='block';e.style.visibility='visible';"
            "e.style.opacity='1';e.style.height='auto';e.style.width='auto';"
            "e.removeAttribute('hidden');"
            "var p=e.closest('span,div,form');"
            "if(p){p.style.display='block';p.style.visibility='visible';}",
            file_input,
        )
        file_input.send_keys(file_path)
        self.log(f"    ✓ [hidden-form] 已填入檔案: {os.path.basename(file_path)}")

        # 填說明
        if file_remark:
            try:
                rmk = self.driver.find_element(By.CSS_SELECTOR, "form#uploadform input[name='filermk']")
                if rmk:
                    self.driver.execute_script(
                        "arguments[0].style.display='block';arguments[0].style.visibility='visible';", rmk)
                    rmk.clear()
                    rmk.send_keys(file_remark)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6848, exc_info=True)

        # 直接 submit uploadform（透過 AJAX 避免頁面跳轉）
        submitted = self.driver.execute_script("""
        var f = document.getElementById('uploadform') || document.querySelector('form[name="uploadform"]');
        if (!f) return '';
        // 使用 FormData + fetch/XMLHttpRequest 避免頁面跳轉
        try {
            var fd = new FormData(f);
            var xhr = new XMLHttpRequest();
            // 決定正確的 upload URL（優先用 form.action，否則用絕對路徑）
            var action = f.action || '';
            if (!action || action.indexOf('DOUPLOAD') < 0) {
                // 從當前 URL 推算，或用已知絕對路徑
                var base = window.location.href.replace(/[^/]*$/, '');
                action = (base && base.indexOf('/judrf/') >= 0)
                    ? base + 'DOUPLOAD.htm'
                    : '/judrf/wkf/FHD2C01/DOUPLOAD.htm';
            }
            xhr.open('POST', action, false);  // synchronous
            xhr.send(fd);
            if (xhr.status >= 200 && xhr.status < 400) {
                return 'xhr_ok:' + xhr.status + ':' + (xhr.responseText || '').substring(0, 200);
            }
            return 'xhr_fail:' + xhr.status + ':' + (xhr.responseText || '').substring(0, 200);
        } catch(e) {
            // fallback: normal submit via hiddeniframe
            f.submit();
            return 'form_submit';
        }
        """) or ""
        if not submitted:
            raise RuntimeError("找不到 uploadform")
        self.log(f"    ✓ [hidden-form] 已送出上傳表單: {submitted}")

        # 檢查 XHR 是否真正成功
        if submitted.startswith("xhr_fail:"):
            self.log(f"    ❌ [hidden-form] XHR 上傳失敗: {submitted}")
            return False

        time.sleep(3.0)

        # 驗證上傳結果：檢查附件列表
        try:
            attach_info = self.driver.execute_script("""
            // 重新載入附件列表
            if (typeof queryFileList === 'function') {
                try { queryFileList(); } catch(e) {}
            }
            // 取得附件列表文字
            var tables = document.querySelectorAll('table');
            var attachText = '';
            for (var i = 0; i < tables.length; i++) {
                var t = (tables[i].innerText || '');
                if (t.indexOf('附件') >= 0 || t.indexOf('檔案') >= 0 || t.indexOf('上傳') >= 0) {
                    attachText += t.substring(0, 300) + '\\n';
                }
            }
            // 也找 id=attachList 或類似
            var al = document.getElementById('attachList') || document.getElementById('fileList');
            if (al) attachText += '|LIST:' + (al.innerText || '').substring(0, 200);
            return attachText.substring(0, 500);
            """) or ""
            if attach_info:
                self.log(f"    [POST-UPLOAD] 附件區域: {attach_info[:300]}")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6914, exc_info=True)

        return True

    @staticmethod
    def _find_visible_or_any_file_input(driver):
        """找到 dialog 中的 file input（優先可見，否則任意）。"""
        # 先找 bootbox/modal 中的
        for sel in ["div.bootbox input[type='file']",
                     "div.modal.in input[type='file']",
                     "div.modal input[type='file']"]:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    try:
                        if el.is_displayed():
                            return el
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 6932, exc_info=True)
                # 如果都不 displayed，回傳第一個
                if els:
                    return els[0]
            except Exception:
                continue
        return None

    def _pick_review_upload_file(
        self,
        candidates: List[str],
        primary_terms: List[str],
        secondary_terms: Optional[List[str]] = None,
        banned_terms: Optional[List[str]] = None,
    ) -> str:
        ranked: List[tuple[int, str]] = []
        for path in candidates:
            score = self._score_review_upload_candidate(
                path,
                primary_terms=primary_terms,
                secondary_terms=secondary_terms,
                banned_terms=banned_terms,
            )
            if score <= 0:
                continue
            ranked.append((score, path))
        if not ranked:
            return ""
        ranked.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
        return ranked[0][1]

    def _has_stamp_on_document(self, file_path: str) -> bool:
        """
        使用視覺模組檢查文件是否有收文章（事務所收文章或法院收文章）。
        支援 PDF 和圖片格式。
        """
        if not file_path or not os.path.exists(file_path):
            return False
        try:
            ext = os.path.splitext(file_path)[1].lower()
            png_bytes = None

            if ext == ".pdf":
                try:
                    import fitz  # PyMuPDF
                    doc = fitz.open(file_path)
                    if doc.page_count > 0:
                        page = doc[0]
                        pix = page.get_pixmap(dpi=200)
                        png_bytes = pix.tobytes("png")
                    doc.close()
                except Exception as e:
                    self.log(f"    ⚠️ PDF 轉圖失敗: {e}")
                    return False
            elif ext in (".png", ".jpg", ".jpeg", ".tiff", ".bmp"):
                with open(file_path, "rb") as f:
                    png_bytes = f.read()
            else:
                return False

            if not png_bytes:
                return False

            # 呼叫 vision_parser 的 extract_info_with_vision（硬性 timeout 防卡）
            try:
                import sys
                pdf_namer_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                               "..", "skills", "pdf-namer")
                if pdf_namer_path not in sys.path:
                    sys.path.insert(0, os.path.normpath(pdf_namer_path))
                from vision_parser import extract_info_with_vision
                from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
                _STAMP_TIMEOUT = 15
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(extract_info_with_vision, png_bytes, timeout_sec=_STAMP_TIMEOUT)
                    result = future.result(timeout=_STAMP_TIMEOUT + 5)
                stamp_type = (result.get("stamp_type") or "").strip()
                has_stamp = stamp_type in ("事務所收文章", "法院收文章")
                self.log(f"    🔍 收文章檢查: {os.path.basename(file_path)} → {stamp_type or '無結果'}")
                return has_stamp
            except (ImportError, FuturesTimeout):
                self.log(f"    ⚠️ 收文章檢查逾時或模組不可用，跳過: {os.path.basename(file_path)}")
                return False
        except Exception as e:
            self.log(f"    ⚠️ 收文章檢查失敗: {e}")
            return False

    def _find_review_upload_files(self, case_info: Dict[str, str], prefer_stamped: bool = False) -> Dict[str, str]:
        """
        依案件資料夾自動挑選閱卷聲請附件。
        - auth_file: 委任狀（若 prefer_stamped=True，優先選有收文章的版本，以視覺模組判斷）
        - laf_file: 法扶接案通知書/開辦通知書/准予扶助證明書
        """
        result = {
            "case_folder": "",
            "auth_file": "",
            "laf_file": "",
        }

        folder_path = (
            case_info.get("folder_path")
            or case_info.get("case_folder")
            or ""
        ).strip()
        if folder_path and os.path.exists(folder_path):
            result["case_folder"] = folder_path
        else:
            derived_court_case_no = (
                case_info.get("court_case_no")
                or case_info.get("showyyidno")
                or ""
            ).strip()
            party_name = (case_info.get("client_name") or case_info.get("party") or "").strip()
            if not derived_court_case_no:
                year = (case_info.get("year") or "").strip()
                case_type = (case_info.get("case_type") or "").strip()
                case_number = (case_info.get("case_number") or "").strip()
                if year and case_type and case_number:
                    try:
                        derived_court_case_no = f"{year}年度{case_type}字第{int(case_number)}號"
                    except Exception:
                        derived_court_case_no = f"{year}年度{case_type}字第{case_number}號"

            info = FileReviewCase(
                case_number=(case_info.get("yyidno") or "").strip(),
                client_name=party_name,
            )
            info.laf_case_no = (case_info.get("laf_case_no") or "").strip()
            info.court_case_no = derived_court_case_no

            folder_path = self._resolve_case_folder(info) or ""
            if (not folder_path or not os.path.exists(folder_path)) and (derived_court_case_no or party_name):
                import time as _time
                _scan_start = _time.monotonic()
                _SCAN_BUDGET = 30  # 秒，NAS 掃描上限
                y, ct, num = self._parse_court_case_no(derived_court_case_no)
                for root in self._get_case_root_candidates():
                    if _time.monotonic() - _scan_start > _SCAN_BUDGET:
                        self.log(f"  ⏱️ 資料夾掃描超過 {_SCAN_BUDGET}s，跳過剩餘搜尋")
                        break
                    root = self._to_local_case_path(root)
                    if not root or not os.path.isdir(root):
                        continue
                    if y and ct and num:
                        hit = self._scan_by_court_tokens(root, str(y), str(ct), str(num))
                        if hit and (not party_name or self._norm(party_name) in self._norm(os.path.basename(hit))):
                            folder_path = hit
                            break
                    if party_name:
                        hit = self._scan_by_party_name(root, party_name, max_depth=4, max_dirs=5000)
                        if hit:
                            folder_path = hit
                            break
            if not folder_path or not os.path.exists(folder_path):
                return result

            result["case_folder"] = folder_path
        all_files: List[str] = []
        scan_budget_sec = int(os.environ.get("MAGI_FILE_REVIEW_UPLOAD_SCAN_BUDGET_SEC", "20") or "20")
        max_candidates = int(os.environ.get("MAGI_FILE_REVIEW_UPLOAD_MAX_CANDIDATES", "600") or "600")
        skip_dir_keywords = tuple(
            k for k in [
                "03_閱卷資料",
                "04_閱卷資料",
                "05_證據資料",
                "06_證據資料",
                "卷宗彙編",
                "OCR",
            ]
            if k
        )

        def _collect_supported_files(base_dir: str, *, budget_sec: int, prefer_small_scope: bool = False) -> List[str]:
            import time as _time

            out: List[str] = []
            start = _time.monotonic()
            for root, dirnames, filenames in os.walk(base_dir):
                if _time.monotonic() - start > budget_sec:
                    self.log(f"  ⏱️ 附件掃描超過 {budget_sec}s，停止擴大搜尋：{os.path.basename(base_dir) or base_dir}")
                    break
                if prefer_small_scope:
                    dirnames[:] = [
                        d for d in dirnames
                        if not any(keyword in d for keyword in skip_dir_keywords)
                    ]
                for name in filenames:
                    full = os.path.join(root, name)
                    if self._is_supported_review_upload(full):
                        out.append(full)
                        if len(out) >= max_candidates:
                            self.log(f"  ⏱️ 附件候選超過 {max_candidates} 筆，停止擴大搜尋")
                            return out
            return out

        if prefer_stamped:
            # 首次聲請：先搜可能存放委任狀/存底的子目錄，避免大型卷證資料夾拖慢。
            focused_dirs = [
                "00_委任狀",
                "01_委任契約書",
                "01_法扶資料",
                "02_開辦資料",
                "02_我方歷次書狀",
            ]
            for subdir in focused_dirs:
                target_dir = os.path.join(folder_path, subdir)
                if os.path.isdir(target_dir):
                    all_files.extend(_collect_supported_files(target_dir, budget_sec=max(4, scan_budget_sec // 2), prefer_small_scope=True))
            if not all_files:
                all_files.extend(_collect_supported_files(folder_path, budget_sec=scan_budget_sec, prefer_small_scope=True))
        else:
            # 非首次：優先搜尋特定子目錄
            preferred_dirs = ["01_法扶資料", "02_開辦資料", "00_委任狀", "01_委任契約書"]
            for subdir in preferred_dirs:
                target_dir = os.path.join(folder_path, subdir)
                if not os.path.isdir(target_dir):
                    continue
                all_files.extend(_collect_supported_files(target_dir, budget_sec=max(4, scan_budget_sec // 2), prefer_small_scope=True))

            if not all_files:
                all_files.extend(_collect_supported_files(folder_path, budget_sec=scan_budget_sec, prefer_small_scope=True))

        deduped = sorted(set(all_files))

        # 判斷是否為法扶案件：只有法扶案才需要上傳法扶通知書
        _is_laf_case = False
        if case_info.get("laf_case_no"):
            _is_laf_case = True
        elif folder_path:
            # 資料夾路徑包含法扶相關關鍵字，或有 01_法扶資料 子目錄
            _fp_lower = folder_path.replace("\\", "/").lower()
            if "法扶" in _fp_lower or "legal_aid" in _fp_lower:
                _is_laf_case = True
            elif os.path.isdir(os.path.join(folder_path, "01_法扶資料")):
                _is_laf_case = True
            # 路徑含「無償案件」→ 明確非法扶
            if "無償案件" in _fp_lower:
                _is_laf_case = False

        if _is_laf_case:
            result["laf_file"] = self._pick_review_upload_file(
                deduped,
                primary_terms=["扶助律師接案通知書", "接案通知書", "開辦通知書", "接案證明", "准予扶助證明書"],
                secondary_terms=["法扶", "扶助", "通知書", "證明書"],
                banned_terms=["審查表", "申請書", "資力詢問表", "預付酬金", "案件概述單"],
            )
        else:
            self.log("  ℹ️ 非法扶案件，跳過法扶通知書搜尋")
        if prefer_stamped:
            # 首次聲請：需要上傳有收文章的委任狀
            # 1. 先用檔名篩選所有可能的委任狀候選
            auth_candidates = []
            for f in deduped:
                bn = os.path.basename(f).lower()
                if "委任" in bn and "解除" not in bn:
                    auth_candidates.append(f)

            # 2. 用視覺模組逐一檢查是否有收文章（stamp），總時間上限 45 秒
            stamped_file = ""
            if auth_candidates:
                import time as _time
                _stamp_start = _time.monotonic()
                _STAMP_CHECK_BUDGET = 45  # 秒
                self.log(f"  🔍 檢查 {len(auth_candidates)} 個委任狀候選的收文章（上限 {_STAMP_CHECK_BUDGET}s）...")
                for candidate in auth_candidates:
                    if _time.monotonic() - _stamp_start > _STAMP_CHECK_BUDGET:
                        self.log("  ⏱️ 收文章檢查超時，跳過剩餘候選")
                        break
                    if self._has_stamp_on_document(candidate):
                        stamped_file = candidate
                        self.log(f"  ✓ 找到有收文章的委任狀: {os.path.basename(candidate)}")
                        break

            if stamped_file:
                result["auth_file"] = stamped_file
            else:
                # 3. 視覺模組找不到收文章，fallback 用檔名評分
                self.log("  ⚠️ 視覺模組未找到有收文章的委任狀，改用檔名評分")
                result["auth_file"] = self._pick_review_upload_file(
                    deduped,
                    primary_terms=["委任狀"],
                    secondary_terms=["收文章", "收執", "收狀", "存底", "花分", "分院", "掛號郵件收件回執"],
                    banned_terms=["解除委任", "申請書", "證明書"],
                )
        else:
            result["auth_file"] = self._pick_review_upload_file(
                deduped,
                primary_terms=["委任狀"],
                secondary_terms=["收執", "收狀", "收文章", "存底", "掛號郵件收件回執"],
                banned_terms=["解除委任", "申請書", "證明書"],
            )
        return result

    def _get_case_root_candidates(self) -> List[str]:
        out: List[str] = []
        # 1) env override
        env = (os.environ.get("MAGI_CASE_ROOTS") or "").strip()
        if env:
            for p in env.split(","):
                p = p.strip()
                if p:
                    out.append(p)

        # 2) legalbridge_config.json
        try:
            json_dir = str(get_json_dir()).strip()
            cfg_path = os.path.join(json_dir, "legalbridge_config.json")
            if os.path.exists(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f) or {}
                p = ((cfg.get("paths") or {}).get("court_docs_folder") or "").strip()
                if p:
                    out.append(p)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7190, exc_info=True)

        # 3) shared MAGI roots
        out.extend(preferred_case_roots(include_closed=False))

        # translate windows-like to local
        translated: List[str] = []
        for p in out:
            lp = self._translate_case_root_to_local(p)
            if lp:
                translated.append(lp)
        # uniq + exists
        uniq = []
        seen = set()
        for p in translated:
            p2 = os.path.abspath(p)
            if p2 in seen:
                continue
            seen.add(p2)
            if os.path.isdir(p2):
                uniq.append(p2)
        return uniq[:6]

    def _translate_case_root_to_local(self, p: str) -> str:
        return translate_case_path_to_local((p or "").replace("\\", "/").strip())

    @staticmethod
    def _parse_court_case_no(text: str) -> Tuple[str, str, str]:
        if not text:
            return "", "", ""
        # Remove common spaces but keep word delimiters
        t = text.strip()
        
        # 1) Full canonical format: 114年度訴字第123號 / 114年度原訴字第000084號
        m = re.search(r"(\d{2,3})\s*年度\s*(.+?)\s*字\s*第\s*0*(\d+)\s*號", t)
        if m:
            return m.group(1), m.group(2), m.group(3)
            
        # 2) Dotted/Underscore/Space format: 114.原上訴.154 / 114_訴_123 / 114 訴 123
        m2 = re.search(r"(\d{2,3})[^0-9A-Za-z\u4e00-\u9fff]+([\u4e00-\u9fffA-Za-z]{1,10})[^0-9A-Za-z\u4e00-\u9fff]+0*(\d+)", t)
        if m2:
            return m2.group(1), m2.group(2), m2.group(3)
            
        # 3) Compact format: 114訴123 / 114原上訴154
        m3 = re.search(r"(\d{2,3})([\u4e00-\u9fffA-Za-z]{1,8})?0*(\d+)", t)
        if m3:
            return m3.group(1), m3.group(2) or "", m3.group(3)
            
        return "", "", ""

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub("[^0-9A-Za-z一-鿿]+", "", (s or ""))

    def _scan_by_court_tokens(self, root: str, year: str, case_type: str, num: str,
                              max_depth: int = 6, max_dirs: int = 25000) -> str:
        year = (year or "").strip()
        case_type = (case_type or "").strip()
        num = (num or "").strip()
        if not (year and case_type and num):
            return ""
        target_year = self._norm(year)
        target_type = self._norm(case_type)
        target_num = self._norm(num)

        def _match(path_text: str) -> bool:
            s = self._norm(path_text)
            return (target_year in s) and (target_type in s) and (target_num in s)

        visited = 0
        stack = [(root, 0)]
        while stack and visited < max_dirs:
            cur, depth = stack.pop()
            visited += 1
            try:
                if depth > 0 and _match(cur):
                    return cur
                if depth >= max_depth:
                    continue
                with os.scandir(cur) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False):
                            stack.append((e.path, depth + 1))
            except Exception:
                continue
        return ""

    def _scan_by_laf_marker(self, root: str, laf_no: str,
                            max_depth: int = 8, max_dirs: int = 35000) -> str:
        laf_no = (laf_no or "").strip()
        if not laf_no:
            return ""
        # prioritize 法扶案件 subtree if present
        laf_root = os.path.join(root, "法扶案件")
        start = laf_root if os.path.isdir(laf_root) else root
        visited = 0
        stack = [(start, 0)]
        while stack and visited < max_dirs:
            cur, depth = stack.pop()
            visited += 1
            try:
                if depth >= max_depth:
                    continue
                # marker usually at 01_法扶資料/_laf_case_number.txt
                marker = os.path.join(cur, "01_法扶資料", "_laf_case_number.txt")
                if os.path.exists(marker):
                    try:
                        txt = open(marker, "r", encoding="utf-8", errors="ignore").read()
                        if laf_no in txt:
                            return cur
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7290, exc_info=True)
                with os.scandir(cur) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False):
                            stack.append((e.path, depth + 1))
            except Exception:
                continue
        return ""

    def _scan_by_party_name(self, root: str, party: str,
                            max_depth: int = 4, max_dirs: int = 15000) -> str:
        party = (party or "").strip()
        if not party:
            return ""
        target = self._norm(party)
        visited = 0
        stack = [(root, 0)]
        while stack and visited < max_dirs:
            cur, depth = stack.pop()
            visited += 1
            try:
                if depth > 0:
                    if target and target in self._norm(os.path.basename(cur)):
                        return cur
                if depth >= max_depth:
                    continue
                with os.scandir(cur) as it:
                    for e in it:
                        if e.is_dir(follow_symlinks=False):
                            stack.append((e.path, depth + 1))
            except Exception:
                continue
        return ""

    def _find_review_folder(self, case_folder: str) -> str:
        """
        在案件資料夾中遞迴尋找包含 '閱卷' 的子資料夾
        支援多層目錄結構 (e.g. 2025/112聲判12/06_閱卷資料)
        """
        if not case_folder or not os.path.exists(case_folder):
            return None

        # 1. 檢查根目錄本身是否就是
        if '閱卷' in os.path.basename(case_folder):
             return case_folder
             
        # 2. 遞迴搜尋所有子目錄
        # 優先尋找 '06_閱卷資料' 或 '閱卷資料'
        try:
            for root, dirs, files in os.walk(case_folder):
                for d in dirs:
                    if '閱卷' in d:
                        return os.path.join(root, d)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7344, exc_info=True)

        return None   

    def _file_exists_recursively(self, root_folder: str, filename: str) -> bool:
        """遞迴檢查檔案是否存在於資料夾中"""
        return bool(self._find_file_recursively(root_folder, filename))

    def _find_file_recursively(self, root_folder: str, filename: str) -> str:
        """遞迴尋找檔案，找到則回傳完整路徑，否則回傳空字串。"""
        try:
            for root, _dirs, files in os.walk(root_folder):
                if filename in files:
                    return os.path.join(root, filename)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7359, exc_info=True)
        return ""

    def upload_to_existing_application(
        self,
        case_info: Dict[str, str],
        file_path: str,
        file_remark: str = "委任狀",
    ) -> str:
        """
        在已存在的閱卷聲請案件上傳附件（如委任狀）。

        1. 導航到列表式查看頁面
        2. 找到目標案件的列
        3. 點擊該列打開詳細頁面
        4. 在詳細頁面中上傳附件

        Returns:
            str: "Uploaded", "NotFound", "Error"
        """
        year = case_info.get("year", "")
        case_type = case_info.get("case_type", "")
        case_number = case_info.get("case_number", "")
        client_name = case_info.get("client_name", "")

        self.log(f"上傳附件到已有聲請: {year}年{case_type}字第{case_number}號 (當事人: {client_name})")
        self.log(f"  附件: {os.path.basename(file_path)} ({file_remark})")

        if not os.path.exists(file_path):
            self.log(f"  ❌ 檔案不存在: {file_path}")
            return "Error"

        try:
            self.driver.implicitly_wait(3)

            # ========= 步驟 1: 點擊「列表式查看」並切到列表頁面 =========
            self.log("  導航到列表頁面...")
            self.driver.switch_to.default_content()

            # 1a. 點擊側邊欄「列表式查看」
            list_view_btn = None
            for selector in [
                "//a[contains(normalize-space(.), '列表式查看')]",
                "//a[contains(., '列表式查看')]",
                "//li//a[contains(., '列表式查看')]",
            ]:
                try:
                    el = self.driver.find_element(By.XPATH, selector)
                    if el and el.is_displayed():
                        list_view_btn = el
                        break
                except Exception:
                    continue

            if list_view_btn:
                try:
                    ActionChains(self.driver).move_to_element(list_view_btn).click().perform()
                except Exception:
                    self.driver.execute_script("arguments[0].click();", list_view_btn)
                self.log("  ✓ 點擊「列表式查看」")
                time.sleep(2)  # 等待頁面載入
            else:
                self.log("  ⚠️ 未找到「列表式查看」按鈕，嘗試直接切 iframe")

            # 1b. Switch to main-content -> v1
            self.driver.switch_to.default_content()
            try:
                main_iframe = self.driver.find_element(
                    By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']"
                )
                self.driver.switch_to.frame(main_iframe)
                time.sleep(1)
                v1_iframe = self.driver.find_element(
                    By.XPATH, "//iframe[@name='v1'] | //iframe[@id='v1']"
                )
                self.driver.switch_to.frame(v1_iframe)
                self.log("  ✓ 切換到 main-content → v1")
            except Exception as e:
                self.log(f"  ⚠️ 切到列表 iframe 失敗: {e}")
                return "Error"

            # 等待表格載入
            time.sleep(2)
            try:
                self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(0.5)
                self.driver.execute_script("window.scrollTo(0, 0);")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7447, exc_info=True)

            # ========= 步驟 2: 找到目標列 =========
            self.log("  搜尋目標案件...")

            target_row_idx = self.driver.execute_script("""
            var rows = document.querySelectorAll('tr');
            var targetYear = arguments[0];
            var targetType = arguments[1];
            var targetNum = arguments[2];
            var targetName = arguments[3];

            // Build multiple matching patterns
            var numPadded = ('000000' + targetNum).slice(-6);
            var patterns = [
                targetYear + '.' + targetType + '.' + numPadded,
                targetYear + '年度' + targetType + '字第' + parseInt(targetNum) + '號',
                targetYear + '.原金訴.' + numPadded,
            ];

            for (var i = 0; i < rows.length; i++) {
                var row = rows[i];
                var rowText = (row.innerText || '').toString();

                // Skip rows that don't have the target name
                if (targetName && rowText.indexOf(targetName) < 0) continue;

                // Match by case number pattern in visible text
                var matchCase = false;
                for (var p = 0; p < patterns.length; p++) {
                    if (rowText.indexOf(patterns[p]) >= 0) {
                        matchCase = true;
                        break;
                    }
                }

                // Also try data-json if available
                if (!matchCase) {
                    var jsonStr = row.getAttribute('data-json');
                    if (jsonStr) {
                        try {
                            var rj = JSON.parse(jsonStr);
                            var yyidno = (rj.yyidno || rj.showyyidno || '').toString();
                            for (var p = 0; p < patterns.length; p++) {
                                if (yyidno.indexOf(patterns[p]) >= 0) {
                                    matchCase = true;
                                    break;
                                }
                            }
                        } catch(e) {}
                    }
                }

                // Also check if row has "取消" status (skip cancelled ones)
                if (matchCase) {
                    if (rowText.indexOf('聲請者取消') >= 0) continue;
                    return i;
                }
            }
            return -1;
            """, year, case_type, case_number, client_name)

            if target_row_idx is None or target_row_idx < 0:
                self.log("  ❌ 找不到目標案件的列")
                return "NotFound"

            self.log(f"  ✓ 找到案件，列索引: {target_row_idx}")

            # ========= 步驟 3: 點擊案號超連結開啟詳細頁面 =========
            # 注意：絕不能點「變更聲請」（會修改申請、可能取消閱卷）
            # FHD2B01 列表中，案號欄位本身就是 <a> 超連結，點擊可開啟詳細頁
            # 待繳費案件在繳費狀態欄也有「待繳費」超連結
            opened = self.driver.execute_script("""
            var targetIdx = arguments[0];
            var targetYear = arguments[1];
            var targetType = arguments[2];
            var targetNum = arguments[3];
            var rows = document.querySelectorAll('tr');
            if (targetIdx >= rows.length) return '';
            var row = rows[targetIdx];

            var numPadded = ('000000' + targetNum).slice(-6);
            var casePatterns = [
                targetYear + '.' + targetType + '.' + numPadded,
                targetYear + '年度' + targetType + '字第'
            ];

            // 黑名單：絕不點擊這些連結
            var blacklist = ['變更聲請', '取消'];

            var links = row.querySelectorAll('a');

            // 1. 優先找案號超連結（文字含案號 pattern 的 <a>）
            for (var i = 0; i < links.length; i++) {
                var t = (links[i].innerText || '').replace(/\\s+/g, '');
                var skip = false;
                for (var b = 0; b < blacklist.length; b++) {
                    if (t.indexOf(blacklist[b]) >= 0) { skip = true; break; }
                }
                if (skip) continue;
                for (var p = 0; p < casePatterns.length; p++) {
                    if (t.indexOf(casePatterns[p]) >= 0) {
                        links[i].click();
                        return 'case_number_link';
                    }
                }
            }

            // 2. 找「待繳費」超連結（繳費狀態欄的連結）
            for (var i = 0; i < links.length; i++) {
                var t = (links[i].innerText || '').replace(/\\s+/g, '');
                if (t.indexOf('待繳費') >= 0) {
                    links[i].click();
                    return 'pending_payment_link';
                }
            }

            // 3. 找其他安全的 <a>（排除黑名單、排除純文字短連結）
            for (var i = 0; i < links.length; i++) {
                var t = (links[i].innerText || '').replace(/\\s+/g, '');
                var skip = false;
                for (var b = 0; b < blacklist.length; b++) {
                    if (t.indexOf(blacklist[b]) >= 0) { skip = true; break; }
                }
                if (skip) continue;
                var href = links[i].getAttribute('href') || '';
                var oc = links[i].getAttribute('onclick') || '';
                if (href && href !== '#' && href !== 'javascript:void(0)') {
                    links[i].click();
                    return 'safe_href_link';
                }
                if (oc) {
                    links[i].click();
                    return 'safe_onclick_link';
                }
            }

            // 4. 嘗試點擊列本身（可能有 onclick）
            var onclick = row.getAttribute('onclick');
            if (onclick) {
                row.click();
                return 'row_onclick';
            }

            // 5. 嘗試點擊列中任何有 onclick 的 td（排除危險按鈕）
            var clickables = row.querySelectorAll('td[onclick]');
            for (var i = 0; i < clickables.length; i++) {
                clickables[i].click();
                return 'td_onclick';
            }

            // 6. 直接點擊列
            row.click();
            return 'row_click';
            """, target_row_idx, year, case_type, case_number) or ""

            if not opened:
                self.log("  ❌ 無法開啟案件詳細頁面")
                return "Error"

            self.log(f"  ✓ 已開啟案件 (方式: {opened})")
            time.sleep(2.0)  # 等待 dialog/頁面載入

            # ========= 步驟 4: 找到上傳區域 =========
            # The dialog/detail page may open in different contexts:
            # - A bootbox/modal dialog in v1
            # - A new iframe in main-content
            # - A jQuery UI dialog
            # We need to search across all possible contexts.

            upload_done = False

            # 方法 A: 在當前 v1 frame 中找 dialog
            try:
                upload_done = self._try_upload_in_current_context(file_path, file_remark)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7623, exc_info=True)

            if not upload_done:
                # 方法 B: 切到 main-content 層級找 dialog
                try:
                    self.driver.switch_to.parent_frame()  # back to main-content
                    upload_done = self._try_upload_in_current_context(file_path, file_remark)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7631, exc_info=True)

            if not upload_done:
                # 方法 C: 檢查是否有新 iframe 出現
                try:
                    self.driver.switch_to.parent_frame()  # back to top of main-content or default
                    iframes = self.driver.find_elements(By.TAG_NAME, "iframe")
                    for iframe in iframes:
                        try:
                            name = iframe.get_attribute("name") or ""
                            if name in ("v1", "main-content"):
                                continue
                            self.driver.switch_to.frame(iframe)
                            upload_done = self._try_upload_in_current_context(file_path, file_remark)
                            if upload_done:
                                break
                            self.driver.switch_to.parent_frame()
                        except Exception:
                            try:
                                self.driver.switch_to.parent_frame()
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7652, exc_info=True)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7654, exc_info=True)

            if not upload_done:
                # 方法 D: 回到 default_content，搜尋所有 frame
                try:
                    self.driver.switch_to.default_content()
                    upload_done = self._search_all_frames_for_upload(
                        self.driver, file_path, file_remark, max_depth=4
                    )
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7664, exc_info=True)

            if not upload_done:
                # 方法 E: 直接在 v1 使用 hidden form (如果 v1 有 uploadform)
                try:
                    self.driver.switch_to.default_content()
                    main_iframe = self.driver.find_element(
                        By.XPATH, "//iframe[@name='main-content'] | //iframe[@id='main-content']"
                    )
                    self.driver.switch_to.frame(main_iframe)
                    v1_iframe = self.driver.find_element(
                        By.XPATH, "//iframe[@name='v1'] | //iframe[@id='v1']"
                    )
                    self.driver.switch_to.frame(v1_iframe)
                    upload_done = self._ola_upload_via_hidden_form(file_path, file_remark)
                except Exception as e:
                    self.log(f"  ⚠️ hidden form 路徑也失敗: {e}")

            if upload_done:
                self.log(f"  ✅ 附件上傳成功: {os.path.basename(file_path)}")
                # 截圖記錄
                try:
                    ss_path = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else "/tmp",
                        f"upload_result_{int(time.time())}.png",
                    )
                    self.driver.save_screenshot(ss_path)
                    self.log(f"  📸 截圖: {ss_path}")
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7693, exc_info=True)

                # ========= 關閉 dialog / 返回列表 =========
                try:
                    self.driver.execute_script("""
                    // 關閉 bootbox modal
                    var modals = document.querySelectorAll('.bootbox, .modal.in, .modal.show');
                    for (var i = 0; i < modals.length; i++) {
                        try { modals[i].style.display = 'none'; } catch(e) {}
                    }
                    // 點擊「關閉」或「回上一頁」按鈕
                    var btns = document.querySelectorAll('a, button, input[type="button"]');
                    for (var i = 0; i < btns.length; i++) {
                        var t = (btns[i].innerText || btns[i].value || '').replace(/\\s+/g, '');
                        if (t === '關閉畫面' || t === '關閉' || t === '回上頁' || t === '返回列表') {
                            btns[i].click();
                            break;
                        }
                    }
                    // 移除 backdrop
                    var bd = document.querySelectorAll('.modal-backdrop');
                    for (var i = 0; i < bd.length; i++) {
                        try { bd[i].parentNode.removeChild(bd[i]); } catch(e) {}
                    }
                    """)
                    self.log("  ✓ 已關閉上傳對話框")
                    time.sleep(1.5)
                except Exception as e:
                    self.log(f"  ⚠️ 關閉 dialog 失敗（不影響上傳結果）: {e}")

                return "Uploaded"
            else:
                self.log("  ❌ 所有上傳路徑都失敗")
                # 截圖 debug
                try:
                    ss_path = os.path.join(
                        os.path.dirname(os.path.abspath(__file__)) if "__file__" in dir() else "/tmp",
                        f"upload_fail_{int(time.time())}.png",
                    )
                    self.driver.save_screenshot(ss_path)
                    self.log(f"  📸 截圖: {ss_path}")
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7735, exc_info=True)
                return "Error"

        except Exception as e:
            self.log(f"  ❌ 上傳附件異常: {e}")
            import traceback
            self.log(traceback.format_exc()[-500:])
            return "Error"

    # ------------------------------------------------------------------
    # 繳費憑證上傳
    # ------------------------------------------------------------------
    def upload_payment_proof(
        self,
        case_info: Dict[str, str],
        file_path: str,
    ) -> str:
        """
        上傳繳費憑證截圖到已存在的閱卷聲請案件。
        包裝 upload_to_existing_application，file_remark 固定為「繳費憑證」。

        Returns:
            str: "Uploaded", "NotFound", "Error"
        """
        return self.upload_to_existing_application(
            case_info, file_path, file_remark="繳費憑證"
        )

    # 法院全名 → court_code 對照表
    _FULL_COURT_MAP = {
        "臺灣臺北地方法院": "TPD", "臺灣新北地方法院": "PCD",
        "臺灣士林地方法院": "SLD", "臺灣桃園地方法院": "TYD",
        "臺灣新竹地方法院": "SCD", "臺灣苗栗地方法院": "MLD",
        "臺灣臺中地方法院": "TCD", "臺灣彰化地方法院": "CHD",
        "臺灣南投地方法院": "NTD", "臺灣雲林地方法院": "ULD",
        "臺灣嘉義地方法院": "CYD", "臺灣臺南地方法院": "TND",
        "臺灣高雄地方法院": "KSD", "臺灣屏東地方法院": "PTD",
        "臺灣花蓮地方法院": "HLD", "臺灣臺東地方法院": "TTD",
        "臺灣宜蘭地方法院": "ILD", "臺灣基隆地方法院": "KLD",
        "臺灣澎湖地方法院": "PHD", "福建金門地方法院": "KMD",
        "福建連江地方法院": "LCD",
        "臺灣高等法院": "TPH",
        "臺灣高等法院臺中分院": "TCH",
        "臺灣高等法院臺南分院": "TNH",
        "臺灣高等法院高雄分院": "KSH",
        "臺灣高等法院花蓮分院": "HLH",
    }

    @classmethod
    def _court_name_to_code(cls, court_name: str) -> str:
        """法院全名 → court_code，支援模糊比對。"""
        if not court_name:
            return ""
        code = cls._FULL_COURT_MAP.get(court_name, "")
        if not code:
            for full_name, c in cls._FULL_COURT_MAP.items():
                if full_name in court_name or court_name in full_name:
                    code = c
                    break
        return code

    @staticmethod
    def parse_payment_screenshot(image_path: str) -> Dict[str, str]:
        """
        解析繳費憑證截圖，萃取案號與法院資訊。

        優先用 tesseract OCR + regex（準確率高），fallback 到 vision API。

        截圖格式（案件繳費狀況查詢清單）:
            案號(遞狀流水號) | 銷帳編號 | 繳款人 | 法院名稱 | ...
            114.原金訴.000166 | ... | ... | 臺灣花蓮地方法院 | ...

        Returns:
            dict with year, case_type, case_number, court_name, court_code,
            raw_case_id, amount, payer.  若無法解析則回傳空 dict。
        """
        import re as _re
        import subprocess as _sp
        import shutil as _shutil

        if not os.path.exists(image_path):
            return {}

        ocr_text = ""

        # ── 方法 1: tesseract OCR（最可靠） ──
        if _shutil.which("tesseract"):
            try:
                r = _sp.run(
                    ["tesseract", image_path, "-", "-l", "chi_tra+eng", "--psm", "6"],
                    capture_output=True, text=True, timeout=15,
                )
                if r.returncode == 0:
                    ocr_text = r.stdout
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7830, exc_info=True)

        # ── 從 OCR 文字用 regex 抽取 ──
        if ocr_text:
            result = FileReviewManager._parse_payment_text(ocr_text)
            if result:
                return result

        # ── 方法 2: vision API fallback（Ollama / OpenAI） ──
        import base64 as _b64
        with open(image_path, "rb") as f:
            img_bytes = f.read()
        b64 = _b64.b64encode(img_bytes).decode("utf-8")
        ext = os.path.splitext(image_path)[1].lower()
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(
            ext.lstrip("."), "image/png"
        )
        prompt = (
            "這是一張台灣司法院閱卷繳費狀況查詢截圖。"
            "請從截圖中提取以下資訊並以 JSON 格式回覆：\n"
            '1. raw_case_id: 案號（如 "114.原金訴.000166"）\n'
            '2. court_name: 法院名稱（如 "臺灣花蓮地方法院"）\n'
            '3. amount: 繳費金額（數字）\n'
            '4. payer: 繳款人姓名\n'
            "只回覆 JSON，不要其他文字。"
            '格式：{"raw_case_id":"...","court_name":"...","amount":"...","payer":"..."}'
        )
        content = None
        # ── Product route: InferenceGateway（可依 file_review profile 切到 Codex） ──
        try:
            if str(os.environ.get("MAGI_CODEX_CONTEXT") or "").strip().lower() != "file_review":
                os.environ["MAGI_CODEX_CONTEXT"] = "file_review"
            from skills.bridge.inference_gateway import InferenceGateway

            gateway = InferenceGateway()
            gw_result = gateway.vision(
                image_path=image_path,
                prompt=prompt,
                timeout=30,
                task_type="ocr",
            )
            gw_text = str(
                gw_result.get("analysis")
                or gw_result.get("response")
                or gw_result.get("text")
                or ""
            ).strip()
            if gw_result.get("success") and gw_text and "raw_case_id" in gw_text:
                content = gw_text
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 7880, exc_info=True)
        # ── Primary: GLM-OCR on port 8082 (vision-dedicated server) ──
        if not content:
            _vision_base = os.environ.get("MAGI_OMLX_VISION_URL", "http://127.0.0.1:8082").rstrip("/")
            _vision_model = os.environ.get("MAGI_OMLX_VISION_MODEL", "GLM-OCR-bf16")
            import requests as _req
            try:
                r = _req.post(f"{_vision_base}/v1/chat/completions", json={
                    "model": _vision_model,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ]}],
                    "stream": False,
                    "temperature": 0.1,
                    "max_tokens": 1024,
                }, timeout=60)
                if r.status_code == 200:
                    choices = r.json().get("choices") or []
                    txt = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
                    if txt and "raw_case_id" in txt:
                        content = txt
            except Exception as _e:
                logging.getLogger(__name__).warning("⚠️ GLM-OCR vision failed: %s", _e, exc_info=True)
        # ── Fallback: oMLX melchior_client ──
        if not content:
            try:
                from skills.bridge import melchior_client as _mc
                _chat_omlx = getattr(_mc, "_chat_omlx", None)
                _omlx_avail = getattr(_mc, "_omlx_available", None)
                if callable(_chat_omlx) and callable(_omlx_avail) and _omlx_avail():
                    r = _chat_omlx(
                        prompt=prompt, model=_vision_model,
                        base_url=_vision_base,
                        timeout=30, temperature=0.1, max_tokens=1024, images=[b64],
                    )
                    if r.get("success") and r.get("response"):
                        txt = r["response"].strip()
                        if txt and "raw_case_id" in txt:
                            content = txt
            except Exception as _e:
                logging.getLogger(__name__).warning("⚠️ melchior vision fallback failed: %s", _e, exc_info=True)
        if not content:
            logging.getLogger(__name__).warning("⚠️ parse_payment_screenshot: ALL vision methods failed, returning empty")
            return {}
        import json as _json
        try:
            m = _re.search(r'\{[^}]+\}', content)
            if not m:
                return {}
            parsed = _json.loads(m.group())
        except Exception:
            return {}
        raw_id = str(parsed.get("raw_case_id", "")).strip()
        if not raw_id:
            return {}
        parts = raw_id.split(".")
        if len(parts) != 3:
            return {}
        court_name = str(parsed.get("court_name", "")).strip()
        return {
            "year": parts[0].strip(),
            "case_type": parts[1].strip(),
            "case_number": str(int(parts[2].strip())),
            "court_name": court_name,
            "court_code": FileReviewManager._court_name_to_code(court_name),
            "raw_case_id": raw_id,
            "amount": str(parsed.get("amount", "")),
            "payer": str(parsed.get("payer", "")),
        }

    @staticmethod
    def _parse_payment_text(text: str) -> Dict[str, str]:
        """
        從 OCR 文字（tesseract 輸出）中用 regex 解析繳費截圖資訊。

        預期文字包含類似:
            114.原金訴.000166  ...  臺灣花蓮地方法院  ...  100  ...
        """
        import re as _re

        # 先確認是繳費相關截圖（含繳費/繳款/入帳/繳費狀況等關鍵字）
        _PAYMENT_KEYWORDS = ("繳費", "繳款", "入帳", "銷帳", "請款", "繳費狀況")
        if not any(kw in text for kw in _PAYMENT_KEYWORDS):
            return {}  # 非繳費截圖（可能是照片或其他文件）

        # 案號: 數字.中文字.數字（如 114.原金訴.000166）
        case_match = _re.search(r'(\d{2,3})\.([\u4e00-\u9fff]+)\.(\d{3,6})', text)
        if not case_match:
            return {}

        year = case_match.group(1)
        case_type = case_match.group(2)
        case_number_raw = case_match.group(3)
        case_number = str(int(case_number_raw))
        raw_case_id = f"{year}.{case_type}.{case_number_raw}"

        # 法院名稱: 臺灣...法院 或 福建...法院（含分院）
        court_match = _re.search(r'[臺台福][灣]?[^\s]*法院(?:[^\s]*分院)?', text)
        court_name = court_match.group(0).rstrip() if court_match else ""
        # 正規化: 台→臺
        court_name = court_name.replace("台灣", "臺灣").replace("台中", "臺中").replace("台南", "臺南").replace("台東", "臺東").replace("台北", "臺北")

        court_code = FileReviewManager._court_name_to_code(court_name)

        # 金額: 在 court_name 後面找純數字（排除銷帳編號等長數字和日期）
        amount = ""
        # 找所有 1-4 位數字（繳費金額通常 50-9999）
        nums = _re.findall(r'(?<!\d)(\d{2,4})(?!\d)', text)
        # 過濾掉年份(114,115)、日期(1150311 的片段)等
        for n in nums:
            val = int(n)
            if 50 <= val <= 9999 and n != year:
                amount = n
                break

        return {
            "year": year,
            "case_type": case_type,
            "case_number": case_number,
            "court_name": court_name,
            "court_code": court_code,
            "raw_case_id": raw_case_id,
            "amount": amount,
            "payer": "",  # tesseract 中文人名容易亂碼，不強求
        }

    def _try_upload_in_current_context(self, file_path: str, file_remark: str = "") -> bool:
        """嘗試在當前 frame context 中找到上傳按鈕並上傳。"""
        # 1. 檢查是否有「附件上傳」或「上傳附件」按鈕
        has_upload = self.driver.execute_script("""
        // Check for upload button
        var btns = document.querySelectorAll('button, input[type="button"], a');
        for (var i = 0; i < btns.length; i++) {
            var t = (btns[i].innerText || btns[i].value || btns[i].title || '').replace(/\\s+/g, '');
            if (t.indexOf('上傳附件') >= 0 || t.indexOf('附件上傳') >= 0 || t.indexOf('上傳') >= 0) {
                return 'found';
            }
        }
        // Check for upload form
        if (document.getElementById('uploadform') || document.querySelector('input[type="file"]')) {
            return 'form';
        }
        // Check for doUpDataFile function
        if (typeof doUpDataFile === 'function') return 'fn';
        return '';
        """) or ""

        if not has_upload:
            return False

        self.log(f"    找到上傳機制: {has_upload}")
        return self._ola_upload_attachment(file_path, file_remark)

    def _search_all_frames_for_upload(
        self, driver, file_path: str, file_remark: str = "", max_depth: int = 4, depth: int = 0
    ) -> bool:
        """遞迴搜尋所有 frame 尋找上傳機制。"""
        if depth >= max_depth:
            return False

        # Try current context
        try:
            if self._try_upload_in_current_context(file_path, file_remark):
                return True
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8069, exc_info=True)

        # Search child frames
        try:
            frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
        except Exception:
            return False

        for f in frames:
            try:
                driver.switch_to.frame(f)
                if self._search_all_frames_for_upload(driver, file_path, file_remark, max_depth, depth + 1):
                    return True
                driver.switch_to.parent_frame()
            except Exception:
                try:
                    driver.switch_to.parent_frame()
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8087, exc_info=True)
                break  # frames list may be stale

        return False

    def apply_for_review(self, case_info: Dict[str, str], auto_submit: bool = True, paper_review: bool = False) -> str:
        """
        閱卷聲請流程

        Args:
            case_info: 案件資訊
                - court_code: 法院代碼 (如 TPD = 台北地方)
                - court_name: 法院名稱 (如 臺灣臺北地方法院)
                - year: 民國年 (如 114)
                - case_type: 字別 (如 訴)
                - case_number: 案號 (如 972)
                - client_name: 當事人姓名
                - appointment_date: (紙本閱卷用) 預約日期 YYYY-MM-DD
                - appointment_time: (紙本閱卷用) 預約時段 "上午"/"下午"
            auto_submit: 是否自動送出
            paper_review: 是否為紙本閱卷（閱紙本卷）

        Returns:
            str: 處理結果 ("Applied", "Ready", "Not Available", "Error")
        """
        try:
            # ★ 優化: 設定短暫的隱式等待而非 0，平衡速度與穩定性
            self.driver.implicitly_wait(3)

            _is_paper = paper_review
            court_code = case_info.get('court_code', 'TPD')
            year = case_info.get('year', '')
            case_type = case_info.get('case_type', '')
            case_number = case_info.get('case_number', '')
            client_name = case_info.get('client_name', '')
            sys_type = case_info.get('sys_type', 'AUTO')
            # When sys_type is "AUTO", we will try multiple system options.
            sys_auto_candidates = []

            _mode_label = "紙本閱卷" if _is_paper else "電子閱卷"
            self.log(f"開始閱卷聲請 ({_mode_label}極速版): {court_code} {year}年 {case_type}字第{case_number}號")

            # Helper: 遞迴尋找表單 Frame (增加深度限制防止無限迴圈)
            def _find_form_frame_recursive(driver, max_depth=5, current_depth=0):
                if current_depth >= max_depth:
                    return False
                    
                driver.implicitly_wait(0) # Speed up probing
                found = False
                try:
                    if driver.find_elements(By.NAME, "ocrtid") or driver.find_elements(By.NAME, "crmyy"):
                        found = True
                except Exception as e: logger.debug("Failed to probe for OLA form elements: %s", e)
                
                if found:
                    driver.implicitly_wait(3)
                    return True

                try:
                    frames = driver.find_elements(By.TAG_NAME, "frame") + driver.find_elements(By.TAG_NAME, "iframe")
                except Exception:
                    driver.implicitly_wait(3)
                    return False
                    
                for f in frames:
                    try:
                        driver.switch_to.frame(f)
                        if _find_form_frame_recursive(driver, max_depth, current_depth + 1):
                            return True
                        driver.switch_to.parent_frame()
                    except Exception as frame_e:
                        # Frame 可能已過期或切換失敗，嘗試回到上層
                        try:
                            driver.switch_to.parent_frame()
                        except Exception:
                            try:
                                driver.switch_to.default_content()
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8160, exc_info=True)
                        # 一旦發生錯誤，停止繼續迭代（frames 列表可能已過期）
                        break
                
                driver.implicitly_wait(3) # Restore
                return False
    
            # ★★★ 確認在正確的視窗 (OLA 系統) ★★★
            # 注意: navigate_to_file_review 現在會關閉 Portal 視窗，所以只有一個視窗
            # 但仍保留此檢查作為安全措施
            try:
                current_title = self.driver.title
                # OLA 系統標題: 司法院資訊業務電子卷證整合管理系統 或含 閱卷
                is_ola_window = any([
                    "閱卷" in current_title,
                    "複製" in current_title,
                    "電子卷證" in current_title,
                    "聲請" in current_title and "卷證" in current_title
                ])
                
                if not is_ola_window:
                    # 不在 OLA 系統視窗，嘗試切換 (備用)
                    self.log(f"  ⚠️ 當前視窗標題不符 ({current_title})，嘗試切換...")
                    all_windows = self.driver.window_handles
                    for w in all_windows:
                        self.driver.switch_to.window(w)
                        try:
                            WebDriverWait(self.driver, 2).until(
                                lambda d: "電子卷證" in d.title or "閱卷" in d.title
                            )
                            self.log(f"  ✓ 切換到閱卷系統視窗: {self.driver.title}")
                            break
                        except Exception:
                            continue
                else:
                    self.log(f"  ✓ 確認在閱卷系統視窗: {current_title[:30]}...")
            except Exception as win_e:
                self.log(f"  ⚠️ 視窗檢查失敗: {win_e}")
    
            # ★ 優化: 狀態預檢 (Pre-check)
            form_ready = False
            already_in_system = False
            
            try:
                self.driver.switch_to.default_content()
                # 1. 檢查是否直接在表單 (Deep Search)
                if _find_form_frame_recursive(self.driver):
                    form_ready = True
                    already_in_system = True
                    self.log("  ✓ 偵測到已在表單頁面，啟動極速填寫模式")
                else:
                     # 2. 檢查是否有側邊欄 (System Check) + 標題確認
                     self.driver.switch_to.default_content()
                     title = self.driver.title
                     found_sidebar = False
                     # OLA 系統標題可能是: 司法院資訊業務電子卷證整合管理系統 或 含 閱卷/複製
                     if ("閱卷" in title or "複製" in title or "電子卷證" in title):
                         # Internal Helper for Sidebar (增加深度限制)
                         def _chk_sidebar(d, max_depth=3, depth=0):
                             if depth >= max_depth:
                                 return False
                             try:
                                 if len(d.find_elements(By.XPATH, "//span[contains(text(), '線上閱卷作業')]")) > 0:
                                     return True
                             except Exception:
                                 return False
                             try:
                                 fr = d.find_elements(By.TAG_NAME, "frame") + d.find_elements(By.TAG_NAME, "iframe")
                             except Exception:
                                 return False
                             for f in fr:
                                 try:
                                     d.switch_to.frame(f)
                                     if _chk_sidebar(d, max_depth, depth + 1): 
                                         return True
                                     d.switch_to.parent_frame()
                                 except Exception:
                                     try:
                                         d.switch_to.default_content()
                                     except Exception:
                                         logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8240, exc_info=True)
                                     # 發生錯誤時停止迭代
                                     break
                             return False

                         self.driver.implicitly_wait(0) # Speed up probing
                         if _chk_sidebar(self.driver):
                             found_sidebar = True
                         self.driver.implicitly_wait(3) # Restore
                         
                         if found_sidebar:
                             already_in_system = True
                             self.log(f"  ✓ 偵測到側邊欄且標題符合 ({title})，跳過開啟步驟")
            except Exception:
                 self.driver.implicitly_wait(3) # Ensure restore
                 pass            
            # ========= 步驟 1: 導航到表單 =========
            clicked = already_in_system
            if not already_in_system:
                self.log("  嘗試載入表單頁面...")
            else:
                self.log("  ✓ 已在系統內，跳過開啟步驟")
            
            try:
                self.driver.switch_to.default_content()
                original_window = self.driver.current_window_handle
                
                # 若已在系統或表單，跳過 Step 1 導航
                if not already_in_system:
                    # 1. 執行 iconMenu_15() (最快路徑)
                    try:
                        # 先切換到 mainFrame
                        try:
                            self.driver.switch_to.frame("mainFrame")
                        except Exception:
                            # 備用：尋找 frame
                            frames = self.driver.find_elements(By.TAG_NAME, "frame")
                            for f in frames:
                                self.driver.switch_to.frame(f)
                                if "iconMenu_15" in self.driver.page_source:
                                    break
                                self.driver.switch_to.parent_frame()
    
                        self.driver.execute_script("if(typeof iconMenu_15 === 'function') iconMenu_15();")
                        self.log("  ✓ 執行 iconMenu_15()")
                        
                         # 智慧等待新視窗
                        try:
                             WebDriverWait(self.driver, 10).until(lambda d: len(d.window_handles) > len([original_window]))
                             
                             new_windows = set(self.driver.window_handles)
                             target_window_found = False
                             
                             for w in new_windows:
                                if w != original_window:
                                    self.driver.switch_to.window(w)
                                    # 等待標題載入，避免切換到空白頁或中間轉址頁
                                    try:
                                        WebDriverWait(self.driver, 15).until(lambda d: "閱卷" in d.title or "複製電子" in d.title)
                                        title = self.driver.title
                                        self.log(f"  ✓ 切換到新視窗: {title}")
                                        target_window_found = True
                                        break
                                    except Exception:
                                        title = self.driver.title
                                        self.log(f"  ⚠️ 視窗標題不符 ({title})，嘗試下一個...")
                             
                             if not target_window_found:
                                 self.log("  ⚠️ 未找到閱卷視窗，嘗試傳統點擊...")
                                 raise Exception("Target window not found")
                                 
                             clicked = True
                        except Exception:
                            self.log("  ⚠️ 未偵測到新視窗或標題不符，嘗試點擊圖示...")
                            raise Exception("Window not opened")
                            
                    except Exception as e:
                        # Fallback: 點擊圖示
                        self.log(f"  ⚠️ JS 執行失敗或無新視窗，嘗試傳統點擊...")
                        try:
                             self.driver.switch_to.default_content()
                             # 嘗試尋找 frame
                             try:
                                 self.driver.switch_to.frame("mainFrame")
                             except Exception:
                                 logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8325, exc_info=True)
                                 
                             # 1. 嘗試點擊 iconMenu_15 對應的圖片或連結
                             # 原 XPATH: //a[contains(text(), '聲請閱卷')]
                             # 但 iconMenu_15 是圖片，可能要找 onclick='iconMenu_15()'
                             try:
                                 btn = WebDriverWait(self.driver, 2).until(
                                    EC.element_to_be_clickable((By.XPATH, "//a[contains(@onclick, 'iconMenu_15')] | //img[contains(@src, 'icon02')]"))
                                 )
                                 self.driver.execute_script("arguments[0].click();", btn)
                                 self.log("  ✓ 點擊圖示 (iconMenu_15)")
                             except Exception:
                                 # 備用: 文字搜尋
                                 btn = WebDriverWait(self.driver, 2).until(
                                    EC.element_to_be_clickable((By.XPATH, "//a[contains(text(), '聲請閱卷')] | //a[.//text()[contains(., '閱卷聲請')]]"))
                                 )
                                 self.driver.execute_script("arguments[0].click();", btn)
                                 self.log("  ✓ 點擊連結 (聲請閱卷)")
    
                             clicked = True
                             
                             # 等待新視窗與標題檢核 (復用邏輯)
                             WebDriverWait(self.driver, 10).until(lambda d: len(d.window_handles) > 1)
                             new_windows = set(self.driver.window_handles)
                             for w in new_windows:
                                if w != original_window:
                                    self.driver.switch_to.window(w)
                                    try:
                                        WebDriverWait(self.driver, 15).until(lambda d: "閱卷" in d.title or "複製電子" in d.title)
                                        self.log(f"  ✓ 切換到新視窗 (Fallback): {self.driver.title}")
                                        break
                                    except Exception:
                                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8357, exc_info=True)
                        except Exception as click_e:
                            self.log(f"  ❌ 點擊失敗: {click_e}")
            
            except Exception as e:
                self.log(f"  ⚠️ 導航異常: {e}")

            if not clicked:
                self.log("  ❌ 無法導航至閱卷聲請頁面")
                return "Error"
            
            # ========= 步驟 2: 點擊側邊欄選單 =========
            if not form_ready:
                # 新視窗載入等待
                try:
                    WebDriverWait(self.driver, 3).until(lambda d: d.execute_script("return document.readyState") == "complete")
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8374, exc_info=True)
                    
                self.driver.switch_to.default_content()
                
                # ★ 優化: 直接載入閱卷聲請登錄頁面 (使用 iframe 導航)
                try:
                    # 方法 1: 直接設定 iframe src (最快)
                    try:
                        main_iframe = self.driver.find_element(By.ID, "main-content")
                        self.driver.execute_script("arguments[0].src = 'wkf/FHD1A01.htm';", main_iframe)
                        self.log("  ✓ 直接載入閱卷聲請登錄頁面 (iframe src)")
                        time.sleep(0.5)  # 快速等待 iframe 載入
                    except Exception as iframe_e:
                        # 方法 2: 點擊側邊欄
                        self.log(f"  ⚠️ Iframe 直接載入失敗，改用點擊: {iframe_e}")
                        
                        # 1. 線上閱卷作業 (展開選單)
                        menu_xpath = "//span[contains(text(), '線上閱卷作業')]/ancestor::a | //a[contains(text(), '線上閱卷作業')]"
                        try:
                            menu_btn = WebDriverWait(self.driver, 2).until(EC.element_to_be_clickable((By.XPATH, menu_xpath)))
                            self.driver.execute_script("arguments[0].click();", menu_btn)
                            time.sleep(0.3)
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8397, exc_info=True)  # 選單可能已展開
                        
                        # 2. 閱卷聲請登錄
                        apply_xpath = "//a[contains(text(), '閱卷聲請登錄')] | //a[@url='wkf/FHD1A01.htm']"
                        apply_btn = WebDriverWait(self.driver, 3).until(EC.element_to_be_clickable((By.XPATH, apply_xpath)))
                        self.driver.execute_script("arguments[0].click();", apply_btn)
                        self.log("  ✓ 點擊閱卷聲請登錄")
                        time.sleep(0.5)
                        
                except Exception as nav_e:
                    self.log(f"  ⚠️ 側邊欄導航失敗 (可能已經在該頁面?): {nav_e}")
            else:
                self.log("  ✓ 跳過側邊欄導航 (表單已就緒)")

            # ========= 步驟 3: 切換到表單 iframe (mainFrame -> v1) =========
            target_frame_found = form_ready
            
            if not form_ready:
                # 這是最耗時的部分，優化 frame 切換
                target_frame_found = False
                
                # A. 嘗試 main-content (OLA 系統使用) 或 mainFrame (Portal 使用)
                try:
                    self.driver.switch_to.default_content()
                    
                    # 優先嘗試 main-content (OLA 系統的 iframe)
                    frame_switched = False
                    for frame_name in ["main-content", "mainFrame"]:
                        try:
                            WebDriverWait(self.driver, 2).until(
                                EC.frame_to_be_available_and_switch_to_it(frame_name))
                            self.log(f"  ✓ 切換到 {frame_name}")
                            frame_switched = True
                            break
                        except Exception:
                            self.driver.switch_to.default_content()
                            continue
                    
                    if frame_switched:
                        # 等待表單欄位出現 (不需要再切 v1，表單直接在 main-content 裡)
                        try:
                            WebDriverWait(self.driver, 3).until(
                                lambda d: len(d.find_elements(By.NAME, "ocrtid")) > 0 or 
                                         len(d.find_elements(By.NAME, "crmyy")) > 0
                            )
                            self.log("  ✓ 偵測到表單欄位")
                            target_frame_found = True
                        except Exception:
                            self.log("  ⚠️ 未偵測到表單欄位，嘗試 v1 frame...")
                            try:
                                WebDriverWait(self.driver, 3).until(
                                    EC.frame_to_be_available_and_switch_to_it("v1"))
                                self.log("  ✓ 切換到 v1 frame")
                                target_frame_found = True
                            except Exception:
                                self.log("  ⚠️ v1 frame 切換失敗")
                except Exception as frame_e:
                    self.log(f"  ⚠️ Frame 切換失敗: {frame_e}")
                
                if not target_frame_found:
                    # B. 遞迴掃描 (更強大的備用方案)
                    self.log("  ⚠️ 啟動深度 Frame 搜尋...")
                    
                    self.driver.switch_to.default_content()
                    if _find_form_frame_recursive(self.driver):
                         target_frame_found = True
                         self.log("  ✓ 找到目標表單 Frame (深度搜尋)")
                    else:
                         # 嘗試找 mainFrame 再搜一次
                         try:
                             self.driver.switch_to.default_content()
                             self.driver.switch_to.frame("mainFrame")
                             if _find_form_frame_recursive(self.driver):
                                 target_frame_found = True
                                 self.log("  ✓ 找到目標表單 Frame (mainFrame 下搜尋)")
                         except Exception:
                             logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8473, exc_info=True)
            
            if not target_frame_found:
                self.log("  ❌ 無法定位表單 Frame")
                try:
                     ts = int(datetime.now().timestamp())
                     self.driver.save_screenshot(f"frame_fail_{ts}.png")
                     with open(f"frame_fail_{ts}.html", 'w', encoding='utf-8') as f:
                         f.write(self.driver.page_source)
                     self.log(f"  📸 已保存失敗現場: frame_fail_{ts}.png/.html")
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8484, exc_info=True)
                return "Error"
            
            # 準備填寫表單... (後續邏輯保持，但已在正確 frame)

            # 保存截圖供調試
            try:
                from datetime import datetime
                ts = int(datetime.now().timestamp())
                self.driver.save_screenshot(f"apply_form_{ts}.png")
                self.log(f"  📸 已保存表單截圖: apply_form_{ts}.png")
                
                # ★★★ DEBUG: 保存 mainFrame 內的 HTML 並總結所有表單欄位 ★★★
                with open(f"apply_form_inside_{ts}.html", 'w', encoding='utf-8') as f:
                    f.write(self.driver.page_source)
                self.log(f"  📄 [DEBUG] 已保存 mainFrame 內 HTML: apply_form_inside_{ts}.html")
                
                # 列出所有 select 和 input 元素
                inputs = self.driver.find_elements(By.TAG_NAME, "input")
                selects = self.driver.find_elements(By.TAG_NAME, "select")
                self.log(f"  🔍 [DEBUG] 找到 {len(inputs)} 個 input, {len(selects)} 個 select")
                for sel in selects[:5]:  # 只顯示前5個
                    name = sel.get_attribute('name') or ''
                    id_attr = sel.get_attribute('id') or ''
                    self.log(f"      SELECT: name='{name}', id='{id_attr}'")
                # ★★★ 顯示所有 text 類型的 input ★★★
                text_inputs = [inp for inp in inputs if inp.get_attribute('type') in ['text', 'tel', '', None]]
                self.log(f"  🔍 [DEBUG] 其中 {len(text_inputs)} 個是 text/tel 類型")
                for inp in text_inputs[:20]:  # 顯示前20個
                    name = inp.get_attribute('name') or ''
                    id_attr = inp.get_attribute('id') or ''
                    placeholder = inp.get_attribute('placeholder') or ''
                    if name or id_attr:
                        self.log(f"      INPUT: name='{name}', id='{id_attr}', placeholder='{placeholder}'")
            except Exception as dbg_e:
                self.log(f"  ⚠️ [DEBUG] 表單診斷失敗: {dbg_e}")
            
            # ========= 步驟 3: 填寫表單欄位 (極速 JS 注入版) =========
            self.log("  填寫案件資料 (JS 注入模式)...")
            
            lawyer_id = case_info.get('lawyer_id', '103台檢11712')
            phone = case_info.get('phone', '0905130216')
            self._last_apply_for_review_uploads = {}
            
            try:
                # ★★★ 優化重點：使用 JavaScript 批次填寫表單 ★★★
                # 比 send_keys 快 30-50%，且更穩定
                
                # 步驟 1: 先選擇下拉選單 (這會觸發 AJAX)
                try:
                    from selenium.webdriver.support.ui import Select
                    
                    # 法院選擇 (ocrtid = 對象法院)
                    court_select = self.driver.find_element(By.NAME, "ocrtid")
                    Select(court_select).select_by_value(court_code)
                    self.log(f"    法院: {court_code}")

                    # ★ 關鍵：等待 AJAX 穩定 (下拉選單變更後會觸發後端呼叫)
                    time.sleep(1.0)

                    # 具體法院/庭選擇 (crtid = 花蓮簡易庭 等)
                    court_division = case_info.get('court_division', '')
                    if court_division:
                        try:
                            crt_select = self.driver.find_element(By.NAME, "crtid")
                            crt_sel = Select(crt_select)
                            # 先嘗試精確 value 匹配，再嘗試文字模糊匹配
                            crt_selected = False
                            try:
                                crt_sel.select_by_value(court_division)
                                crt_selected = True
                            except Exception:
                                pass
                            if not crt_selected:
                                for opt in crt_sel.options:
                                    if court_division in (opt.text or ''):
                                        crt_sel.select_by_visible_text(opt.text)
                                        crt_selected = True
                                        break
                            if crt_selected:
                                self.log(f"    具體法院: {court_division}")
                                time.sleep(0.5)
                            else:
                                self.log(f"    ⚠️ 找不到具體法院選項: {court_division}")
                                # 列出可用選項
                                avail = [f"{o.get_attribute('value')}={o.text}" for o in crt_sel.options[:15]]
                                self.log(f"        可用: {avail}")
                        except Exception as crt_e:
                            self.log(f"    ⚠️ crtid 選擇失敗: {crt_e}")

                    # 系統別選擇
                    sys_select = self.driver.find_element(By.NAME, "sys")
                    sys_sel = Select(sys_select)

                    # sys_type 支援：
                    # - 具體值 (e.g. "H")
                    # - 文字 (e.g. "刑事"/"民事")：嘗試以 visible text 選取
                    # - AUTO：根據頁面 option 值逐一嘗試，直到「檢查案號股別」通過
                    sys_type_raw = (sys_type or "").strip()
                    sys_auto_candidates = []
                    if (not sys_type_raw) or sys_type_raw.upper() == "AUTO":
                        vals = []
                        for opt in (sys_sel.options or []):
                            try:
                                v = (opt.get_attribute("value") or "").strip()
                            except Exception:
                                v = ""
                            if v:
                                vals.append(v)
                        # Prefer common choices first, then the rest.
                        preferred = [v for v in ["H", "C", "A", "F", "M", "S"] if v in vals]
                        sys_auto_candidates = preferred + [v for v in vals if v not in preferred]
                        # Pick the first candidate now; the rest will be tried later if needed.
                        if sys_auto_candidates:
                            sys_type_raw = sys_auto_candidates[0]
                            sys_auto_candidates = sys_auto_candidates[1:]

                    selected_ok = False
                    if sys_type_raw:
                        try:
                            sys_sel.select_by_value(sys_type_raw)
                            selected_ok = True
                            self.log(f"    系統別: {sys_type_raw}")
                        except Exception:
                            selected_ok = False
                    if (not selected_ok) and sys_type_raw:
                        # Try visible text selection for human-friendly labels.
                        try:
                            sys_sel.select_by_visible_text(sys_type_raw)
                            selected_ok = True
                            self.log(f"    系統別(文字): {sys_type_raw}")
                        except Exception:
                            selected_ok = False

                    if not selected_ok:
                        # As a last resort, keep whatever default is selected.
                        try:
                            cur = sys_sel.first_selected_option.get_attribute("value") or ""
                        except Exception:
                            cur = ""
                        self.log(f"    ⚠️ 系統別未指定/無法選取，沿用預設: {cur}")
                        sys_type_raw = cur
                    
                    # ★ 再次等待 AJAX 穩定
                    time.sleep(0.5)
                    
                except Exception as e:
                    self.log(f"    ⚠️ 下拉選單選擇失敗: {e}")
                
                # 步驟 2: 使用 Selenium 直接填寫欄位（更可靠）
                self.log("    填寫文字欄位...")
                
                form_data = [
                    ("crmyy", year, "年度"),
                    ("crmid", case_type, "字別"),
                    ("crmno", case_number, "案號"),
                    ("clnm", client_name, "當事人"),
                    ("law_id", lawyer_id, "律師證號"),
                    ("tel", phone, "電話"),
                ]
                
                filled_count = 0
                for field_name, field_value, field_label in form_data:
                    if not field_value:
                        continue
                    try:
                        el = self.driver.find_element(By.NAME, field_name)
                        el.clear()
                        el.send_keys(str(field_value))
                        filled_count += 1
                        self.log(f"      ✓ {field_label}: {field_value}")
                    except Exception as e:
                        self.log(f"      ❌ {field_label} ({field_name}) 填寫失敗: {e}")
                
                self.log(f"    ✓ 共填寫 {filled_count}/{len(form_data)} 個欄位")
                
            except Exception as e:
                self.log(f"  ❌ 填寫表單失敗: {e}")
                return "Error"
            
            # ========= 步驟 4: 點擊「檢查案號股別」按鈕 =========
            self.log("  點擊「檢查案號股別」按鈕...")
            
            check_btn_selectors = [
                "//input[@name='checkDptBtn']",
                "//input[@value='檢查案號股別']",
                "//input[@type='button' and contains(@value, '檢查')]",
                "//button[contains(., '檢查')]",
            ]
            
            check_clicked = False
            
            # Helper: 嘗試點擊按鈕
            def try_click_check_btn():
                combined_xpath = " | ".join(check_btn_selectors)
                try:
                    btn = WebDriverWait(self.driver, 1).until(EC.element_to_be_clickable((By.XPATH, combined_xpath)))
                    self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                    self.driver.execute_script("arguments[0].click();", btn)
                    return True
                except Exception:
                    return False

            # 1. 在當前 Frame (預期是 v1) 嘗試
            if try_click_check_btn():
                check_clicked = True
                self.log("  ✓ 已點擊「檢查案號股別」按鈕 (Current Frame)")
            
            # 2. 如果失敗，嘗試切換到 Parent Frame (mainFrame)
            if not check_clicked:
                self.log("  ⚠️ 當前 Frame 找不到按鈕，嘗試 Parent Frame...")
                try:
                    self.driver.switch_to.parent_frame()
                    if try_click_check_btn():
                        check_clicked = True
                        self.log("  ✓ 已點擊「檢查案號股別」按鈕 (Parent Frame)")
                except Exception:
                   logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8671, exc_info=True)

            # 3. 如果還失敗，重新從 Root 定位 mainFrame
            if not check_clicked:
                 self.log("  ⚠️ Parent Frame 找不到按鈕，重新定位 mainFrame...")
                 try:
                    self.driver.switch_to.default_content()
                    self.driver.switch_to.frame("mainFrame")
                    if try_click_check_btn():
                        check_clicked = True
                        self.log("  ✓ 已點擊「檢查案號股別」按鈕 (mainFrame)")
                 except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8683, exc_info=True)

            if not check_clicked:
                self.log("  ❌ 無法點擊任何查詢/檢查按鈕")
                return "Error"
            
            # ========= 步驟 5: 處理彈窗 (Alert) =========
            # 這是最容易卡住的地方，使用智慧等待
            self.log("  檢查彈窗回應...")
            
            has_ebook = False
            alert_message = ""
            
            try:
                # 等待 Alert 出現 (最多 5 秒)
                WebDriverWait(self.driver, 5).until(EC.alert_is_present())
                alert = self.driver.switch_to.alert
                alert_message = alert.text
                self.log(f"  偵測到彈窗: {alert_message}")
                alert.accept()
                
                # 判斷是否可繼續聲請
                if _is_paper:
                    # 紙本閱卷：alert 可能顯示案號存在/股別確認等，只要不是明確錯誤就繼續
                    if any(keyword in alert_message for keyword in ["無此案號", "找不到", "不存在", "格式錯誤"]):
                        has_ebook = False
                    else:
                        has_ebook = True  # 紙本不需要有電子卷證，只需案號存在
                else:
                    if any(keyword in alert_message for keyword in ["已有電子卷證", "可供聲請", "電子卷證"]):
                        has_ebook = True
                    elif any(keyword in alert_message for keyword in ["無", "找不到", "不存在", "錯誤"]):
                        has_ebook = False
                    else:
                        has_ebook = True  # 預設可繼續
                    
            except TimeoutException:
                # 沒有 Alert，檢查頁面訊息
                self.log("  ⚠️ 未偵測到彈窗，檢查頁面訊息...")
                page_source = self.driver.page_source
                if "已有電子卷證" in page_source or "可供聲請" in page_source:
                    has_ebook = True
                    self.log("  (頁面顯示有電子卷證)")
                elif _is_paper:
                    # 紙本閱卷：沒有彈窗可能表示案號已通過檢查
                    has_ebook = True
                    self.log("  (紙本閱卷：無彈窗，視為案號檢查通過)")
                # 如果完全沒反應，可能按鈕點擊沒觸發後端?
            
            if not has_ebook:
                # If sys_type was AUTO, try other system options before giving up.
                if sys_auto_candidates:
                    self.log(f"  ⚠️ 案號查詢失敗，嘗試切換系統別重試（剩餘 {len(sys_auto_candidates)} 種）...")

                    def _select_sys_value(v: str) -> bool:
                        try:
                            from selenium.webdriver.support.ui import Select
                            s = Select(self.driver.find_element(By.NAME, "sys"))
                            s.select_by_value(v)
                            return True
                        except Exception:
                            return False

                    def _click_check_anywhere() -> bool:
                        nonlocal check_clicked
                        check_clicked = False
                        try:
                            if try_click_check_btn():
                                check_clicked = True
                                return True
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8743, exc_info=True)
                        try:
                            self.driver.switch_to.parent_frame()
                            if try_click_check_btn():
                                check_clicked = True
                                return True
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8750, exc_info=True)
                        try:
                            self.driver.switch_to.default_content()
                            self.driver.switch_to.frame("mainFrame")
                            if try_click_check_btn():
                                check_clicked = True
                                return True
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8758, exc_info=True)
                        return False

                    for cand in list(sys_auto_candidates)[:6]:
                        try:
                            if not _select_sys_value(cand):
                                continue
                            self.log(f"  🔁 改用系統別={cand} 重試檢查...")
                            time.sleep(0.4)
                            if not _click_check_anywhere():
                                continue

                            # Re-run alert check
                            has_ebook = False
                            alert_message = ""
                            try:
                                WebDriverWait(self.driver, 5).until(EC.alert_is_present())
                                alert = self.driver.switch_to.alert
                                alert_message = alert.text
                                self.log(f"  (重試) 偵測到彈窗: {alert_message}")
                                alert.accept()

                                if any(keyword in alert_message for keyword in ["已有電子卷證", "可供聲請", "電子卷證"]):
                                    has_ebook = True
                                elif any(keyword in alert_message for keyword in ["無", "找不到", "不存在", "錯誤"]):
                                    has_ebook = False
                                else:
                                    has_ebook = True
                            except TimeoutException:
                                page_source = self.driver.page_source
                                if "已有電子卷證" in page_source or "可供聲請" in page_source:
                                    has_ebook = True

                            if has_ebook:
                                self.log(f"  ✅ 系統別={cand} 成功通過檢查")
                                break
                        except Exception as e2:
                            self.log(f"  ⚠️ 系統別={cand} 重試失敗: {e2}")
                            continue

                if not has_ebook:
                    self.log(f"  ❌ 案號查詢失敗: {alert_message}")
                    return "Not Available"

            # 若頁面仍有未處理 alert，先清掉以免後續 execute_script 觸發 unexpected alert open
            for _ in range(3):
                try:
                    WebDriverWait(self.driver, 0.8).until(EC.alert_is_present())
                    pending_alert = self.driver.switch_to.alert
                    pending_text = pending_alert.text
                    pending_alert.accept()
                    self.log(f"  ℹ️ 已清除後續彈窗: {pending_text}")
                    time.sleep(0.2)
                except Exception:
                    break
            
            # ========= 步驟 6-7: 選擇聲請方式/範圍/交付 (極速 JS 批次版) =========
            self.log(f"  選擇聲請選項 (JS 批次模式, {'紙本閱卷' if _is_paper else '電子閱卷'})...")

            # ★★★ 優化：使用 JavaScript 一次選擇所有 Radio 按鈕 ★★★
            # paper_review=True  → applyway=0 (閱紙本卷), 無 getway, applytype=0 (全卷)
            # paper_review=False → applyway=1 (複製電子卷證), getway=線上交付, applytype=2 (合併聲請)
            js_select_radios = """
            (function() {
                var selected = [];
                var isPaper = arguments[1] || false;

                // 1. 聲請方式 (value 是文字: "閱紙本卷" / "複製電子卷證")
                if (isPaper) {
                    var applyway = document.querySelector('input[name="applyway"][value="閱紙本卷"]') ||
                                   document.querySelector('input[name="applyway"][value="0"]');
                    if (!applyway) {
                        var allAW = document.querySelectorAll('input[name="applyway"]');
                        if (allAW.length > 0) applyway = allAW[0];
                    }
                } else {
                    var applyway = document.querySelector('input[name="applyway"][value="複製電子卷證"]') ||
                                   document.querySelector('input[name="applyway"][value="1"]');
                    if (!applyway) {
                        var allAW = document.querySelectorAll('input[name="applyway"]');
                        if (allAW.length > 1) applyway = allAW[1];
                    }
                }
                if (applyway && !applyway.checked) {
                    applyway.checked = true;
                    applyway.click();
                    applyway.dispatchEvent(new Event('change', {bubbles: true}));
                    selected.push('聲請方式=' + (isPaper ? '紙本' : '電子'));
                }

                // 2. 聲請範圍 (applytype — 紙本時可能不存在)
                if (!isPaper) {
                    var applytype = document.querySelector('input[name="applytype"][value="2"]') ||
                                    document.querySelector('input[name="applytype"][value="合併聲請"]');
                    if (applytype && !applytype.checked) {
                        applytype.checked = true;
                        applytype.click();
                        applytype.dispatchEvent(new Event('change', {bubbles: true}));
                        selected.push('聲請範圍');
                    }
                } else {
                    // 紙本: 選第一個 applytype (全卷) 如果存在
                    var applytype = document.querySelector('input[name="applytype"]');
                    if (applytype && !applytype.checked) {
                        applytype.checked = true;
                        applytype.click();
                        applytype.dispatchEvent(new Event('change', {bubbles: true}));
                        selected.push('聲請範圍');
                    }
                }

                // 3. 交付方式 — 紙本閱卷無此選項，僅電子閱卷需選
                if (!isPaper) {
                    var getway = document.querySelector('input[name="getway"][value="線上交付"]');
                    if (getway && !getway.checked) {
                        getway.checked = true;
                        getway.dispatchEvent(new Event('change', {bubbles: true}));
                        selected.push('交付方式');
                    }
                }

                // 4. 是否為義務辯護（指定辯護案件需勾選）
                var isFeeExempt = arguments[0] || false;
                if (isFeeExempt) {
                    var oblEl = document.querySelector('input[name="isobligation"][value="Y"]');
                    if (oblEl && !oblEl.checked) {
                        oblEl.checked = true;
                        oblEl.dispatchEvent(new Event('change', {bubbles: true}));
                        selected.push('義務辯護');
                    }
                }

                return selected;
            })();
            """
            
            # 判斷是否為指定辯護案件
            _is_appointed_defense = False
            try:
                _court_case_no = case_info.get("court_case_no") or case_info.get("showyyidno") or ""
                _yyidno = case_info.get("yyidno") or ""
                _party = case_info.get("client_name") or case_info.get("party") or ""
                if not _court_case_no:
                    _year = case_info.get("year") or ""
                    _ct = case_info.get("case_type") or ""
                    _num = case_info.get("case_number") or ""
                    if _year and _ct and _num:
                        _court_case_no = f"{_year}年度{_ct}字第{_num}號"
                _is_appointed_defense = self._is_fee_exempt_case(
                    court_case_no=_court_case_no, party=_party, yyidno=_yyidno
                )
                if _is_appointed_defense:
                    self.log("  ℹ️ 此案為指定辯護案件，將勾選義務辯護")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8886, exc_info=True)

            try:
                selected_options = self.driver.execute_script(js_select_radios, _is_appointed_defense, _is_paper)
                if selected_options:
                    self.log(f"  ✓ JS 批次選擇: {', '.join(selected_options)}")
                else:
                    self.log("  ℹ️ 選項已預設或不需變更")
            except Exception as js_e:
                self.log(f"  ⚠️ JS 批次選擇失敗，改用傳統方式: {js_e}")
                # 備用：傳統方式
                _aw_val = "閱紙本卷" if _is_paper else "複製電子卷證"
                fallback_items = [
                    ("聲請方式", [f"//input[@name='applyway' and @value='{_aw_val}']"]),
                ]
                if not _is_paper:
                    fallback_items.append(("聲請範圍", ["//input[@name='applytype' and @value='2']"]))
                else:
                    at_el = self.driver.find_elements(By.NAME, "applytype")
                    if at_el:
                        fallback_items.append(("聲請範圍", ["//input[@name='applytype']"]))
                if not _is_paper:
                    fallback_items.append(("交付方式", ["//input[@name='getway' and @value='線上交付']"]))
                for (name, selectors) in fallback_items:
                    for sel in selectors:
                        try:
                            radio = WebDriverWait(self.driver, 1).until(
                                EC.element_to_be_clickable((By.XPATH, sel)))
                            if not radio.is_selected():
                                self.driver.execute_script("arguments[0].click();", radio)
                            break
                        except Exception:
                            continue

            # ========= 步驟 6.5: 紙本閱卷 — 選擇預約時段 =========
            if _is_paper:
                # 支援複數預約時段：
                #   appointment_slots: [{"date":"2026-04-07","time":"下午"}, {"date":"2026-04-08","time":"上午"}]
                #   或單一欄位: appointment_date + appointment_time
                _slots = case_info.get('appointment_slots', [])
                if not _slots:
                    _single_date = case_info.get('appointment_date', '')
                    _single_time = case_info.get('appointment_time', '下午')
                    if _single_date:
                        _slots = [{"date": _single_date, "time": _single_time}]

                if _slots:
                    self.log(f"  選擇紙本閱卷預約時段（共 {len(_slots)} 個）...")
                    try:
                        # 等待 applyway 切換後預約時段區域出現
                        time.sleep(1.5)

                        # 重新確保在正確的 frame（radio click 可能觸發頁面部分重載）
                        try:
                            self.driver.switch_to.default_content()
                            for fn in ["main-content", "mainFrame"]:
                                try:
                                    self.driver.switch_to.frame(fn)
                                    break
                                except Exception:
                                    self.driver.switch_to.default_content()
                            try:
                                self.driver.switch_to.frame("v1")
                            except Exception:
                                pass
                        except Exception:
                            pass

                        _selected_count = 0
                        for _slot in _slots:
                            _appt_date = _slot.get("date", "")
                            _appt_time = _slot.get("time", "下午")
                            if not _appt_date:
                                continue

                            # OLA 預約時段 checkbox value 格式: "1150407PM" (民國年MMDDAMPM)
                            _y, _m, _d = _appt_date.split("-")
                            _roc_year = int(_y) - 1911
                            _roc_date = f"{_roc_year}{_m}{_d}"
                            _am_pm = "AM" if "上午" in _appt_time else "PM"
                            _target_value = f"{_roc_date}{_am_pm}"

                            self.log(f"      目標 checkbox value: {_target_value}")

                            # 使用 Selenium 直接操作 checkbox（比 JS 更可靠）
                            appt_result = {'found': False, 'msg': ''}
                            try:
                                target_cb = self.driver.find_element(
                                    By.CSS_SELECTOR, f'input[type="checkbox"][value="{_target_value}"]')
                                if not target_cb.is_selected():
                                    self.driver.execute_script("arguments[0].click();", target_cb)
                                appt_result = {'found': True, 'msg': f'Selenium click: value={_target_value}'}
                            except Exception:
                                # 備用：遍歷所有 checkbox 找模糊匹配
                                _date_part = _target_value[:-2]
                                all_cbs = self.driver.find_elements(By.CSS_SELECTOR, 'input[type="checkbox"]')
                                for cb in all_cbs:
                                    val = cb.get_attribute('value') or ''
                                    if _date_part in val and _am_pm in val.upper():
                                        if not cb.is_selected():
                                            self.driver.execute_script("arguments[0].click();", cb)
                                        appt_result = {'found': True, 'msg': f'模糊匹配: value={val}'}
                                        break
                                if not appt_result['found']:
                                    avail = [cb.get_attribute('value') for cb in all_cbs[:15]]
                                    appt_result['msg'] = f'NOT_FOUND target={_target_value} available={avail}'

                            if appt_result.get('found'):
                                _selected_count += 1
                                self.log(f"  ✓ 已選擇: {_appt_date} {_appt_time} ({appt_result.get('msg', '')})")
                            else:
                                diag = appt_result.get('msg', '')
                                self.log(f"  ⚠️ 未找到: {_appt_date} {_appt_time}")
                                self.log(f"      診斷: {diag[:500]}")

                        self.log(f"  預約時段選擇完成：成功 {_selected_count}/{len(_slots)} 個")
                    except Exception as appt_e:
                        self.log(f"  ⚠️ 預約時段選擇失敗: {appt_e}")
                else:
                    self.log("  ⚠️ 紙本閱卷未指定預約日期，跳過預約時段選擇")

            # ========= 步驟 7.5: 自動上傳法扶附件/委任狀 =========
            self.log("  檢查閱卷聲請附件...")
            try:
                reg_key = self.make_apply_registry_key(case_info)
                is_first = self.is_first_application(reg_key)

                # ── 閱卷資料夾判斷：如果案件已有閱卷資料，視為非首次聲請 ──
                if is_first:
                    _case_folder = (case_info.get("folder_path") or "").strip()
                    if not _case_folder:
                        try:
                            _case_folder = self._resolve_case_folder_from_db(
                                party=case_info.get("client_name", ""),
                            )
                            if isinstance(_case_folder, list):
                                _case_folder = _case_folder[0] if _case_folder else ""
                        except Exception:
                            _case_folder = ""
                    if _case_folder and os.path.isdir(_case_folder):
                        for _review_sub in ["03_閱卷資料", "04_閱卷資料", "閱卷資料"]:
                            _review_dir = os.path.join(_case_folder, _review_sub)
                            if os.path.isdir(_review_dir) and os.listdir(_review_dir):
                                is_first = False
                                self.log(f"  ℹ️ 偵測到 {_review_sub}/ 已有檔案 → 非首次聲請")
                                break

                if not is_first:
                    self.log("  ℹ️ 非首次聲請，跳過附件上傳")
                    upload_files = {}
                    self._last_apply_for_review_uploads = upload_files
                else:
                    self.log("  ℹ️ 首次聲請 → 應上傳收文章委任狀（02_開辦資料）")
                    upload_files = self._find_review_upload_files(case_info, prefer_stamped=True)
                    self._last_apply_for_review_uploads = upload_files

                if not is_first:
                    pass  # 已跳過
                elif upload_files.get("case_folder"):
                    self.log(f"  ✓ 案件資料夾: {upload_files['case_folder']}")
                else:
                    self.log("  ⚠️ 找不到案件資料夾，略過附件上傳")

                for field_name, label in [
                    ("auth_file", "委任狀"),
                    ("laf_file", "法扶通知書"),
                ]:
                    file_path = (upload_files.get(field_name) or "").strip()
                    if not file_path:
                        self.log(f"  ⚠️ 未找到可上傳的{label}")
                        continue
                    if not os.path.exists(file_path):
                        self.log(f"  ⚠️ {label}檔案不存在: {file_path}")
                        continue
                    try:
                        self._ola_upload_attachment(file_path, file_remark=label)
                        self.log(f"  ✓ 已上傳{label}: {os.path.basename(file_path)}")
                    except Exception as upload_e:
                        self.log(f"  ⚠️ 上傳{label}失敗: {upload_e}")
            except Exception as upload_scan_e:
                self.log(f"  ⚠️ 附件掃描失敗: {upload_scan_e}")
            
            # ========= 步驟 8: 根據模式處理送出 =========
            if auto_submit:
                # === 自動送出模式 ===
                self.log("  尋找送出按鈕...")
                
                submit_selectors = [
                    "//button[@text='確認送出']",
                    "//button[.//span[contains(text(), '確認送出')]]",
                    "//button[contains(@class, 'btn-success') and contains(., '送出')]",
                ]
                
                submit_clicked = False
                for sel in submit_selectors:
                    try:
                        btn = WebDriverWait(self.driver, 1).until(EC.element_to_be_clickable((By.XPATH, sel)))
                        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", btn)
                        self.driver.execute_script("arguments[0].click();", btn)
                        submit_clicked = True
                        self.log("  ✓ 已點擊「確認送出」按鈕")
                        break
                    except Exception:
                        continue
                
                if submit_clicked:
                    # 處理送出後的確認彈窗
                    try:
                        WebDriverWait(self.driver, 3).until(EC.alert_is_present())
                        alert = self.driver.switch_to.alert
                        confirm_msg = alert.text
                        self.log(f"  送出確認: {confirm_msg}")
                        alert.accept()
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 8984, exc_info=True)
                    
                    self.log("  ✅ 閱卷聲請已自動提交")

                    # ========= 送出後成功驗證 =========
                    apply_evidence = {
                        "submitted": True,
                        "timestamp": datetime.now().isoformat(),
                    }

                    # (a) 截圖保存成功頁面
                    try:
                        time.sleep(1)  # 等待頁面刷新至成功狀態
                        screenshots_dir = os.path.join(self.download_folder, "screenshots")
                        os.makedirs(screenshots_dir, exist_ok=True)
                        ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
                        screenshot_path = os.path.join(screenshots_dir, f"apply_{ts_str}.png")
                        self.driver.save_screenshot(screenshot_path)
                        apply_evidence["screenshot"] = screenshot_path
                        self.log(f"  📸 成功頁面截圖已保存: {screenshot_path}")
                    except Exception as ss_e:
                        self.log(f"  ⚠️ 成功頁面截圖失敗: {ss_e}")

                    # (b) 嘗試從頁面抓取聲請編號
                    try:
                        application_number = None
                        page_text = self.driver.page_source or ""
                        # 嘗試多種模式抓取聲請序號
                        import re as _re
                        patterns = [
                            r'聲請序號[：:\s]*([A-Za-z0-9\-]+)',
                            r'聲請編號[：:\s]*([A-Za-z0-9\-]+)',
                            r'申請編號[：:\s]*([A-Za-z0-9\-]+)',
                            r'案件編號[：:\s]*([A-Za-z0-9\-]+)',
                            r'收件編號[：:\s]*([A-Za-z0-9\-]+)',
                        ]
                        for pat in patterns:
                            m = _re.search(pat, page_text)
                            if m:
                                application_number = m.group(1).strip()
                                break

                        # 備用：嘗試用 XPath 取得特定元素的文字
                        if not application_number:
                            try:
                                self.driver.implicitly_wait(1)
                                candidate_xpaths = [
                                    "//td[contains(text(),'聲請序號')]/following-sibling::td",
                                    "//th[contains(text(),'聲請序號')]/following-sibling::td",
                                    "//label[contains(text(),'聲請序號')]/following::span[1]",
                                    "//td[contains(text(),'聲請編號')]/following-sibling::td",
                                ]
                                for xp in candidate_xpaths:
                                    try:
                                        el = self.driver.find_element(By.XPATH, xp)
                                        txt = (el.text or "").strip()
                                        if txt and len(txt) < 50:
                                            application_number = txt
                                            break
                                    except Exception:
                                        continue
                                self.driver.implicitly_wait(3)
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9047, exc_info=True)

                        if application_number:
                            apply_evidence["application_number"] = application_number
                            self.log(f"  ✅ 取得聲請編號: {application_number}")
                        else:
                            self.log("  ⚠️ 未能取得聲請編號，建議人工確認")
                    except Exception as an_e:
                        self.log(f"  ⚠️ 抓取聲請編號失敗: {an_e}")
                        self.log("  ⚠️ 未能取得聲請編號，建議人工確認")

                    # (c) 回到列表頁確認新增記錄
                    try:
                        time.sleep(2)  # 等待系統處理完成
                        # 嘗試導航到聲請列表頁
                        list_found = False
                        try:
                            self.driver.switch_to.default_content()
                            # 嘗試透過 iframe src 導航到列表頁
                            try:
                                main_iframe = self.driver.find_element(By.ID, "main-content")
                                self.driver.execute_script("arguments[0].src = 'wkf/FHD1A02.htm';", main_iframe)
                                time.sleep(1.5)
                                list_found = True
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9072, exc_info=True)

                            if not list_found:
                                # 備用：嘗試點擊側邊欄的「聲請閱卷查詢」
                                try:
                                    query_xpath = "//a[contains(text(), '聲請閱卷查詢')] | //a[contains(text(), '聲請查詢')] | //a[@url='wkf/FHD1A02.htm']"
                                    query_btn = WebDriverWait(self.driver, 2).until(
                                        EC.element_to_be_clickable((By.XPATH, query_xpath)))
                                    self.driver.execute_script("arguments[0].click();", query_btn)
                                    time.sleep(1.5)
                                    list_found = True
                                except Exception:
                                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9084, exc_info=True)
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9086, exc_info=True)

                        if list_found:
                            # 切換到列表的 frame 並檢查是否有新記錄
                            try:
                                self.driver.switch_to.default_content()
                                # 嘗試進入 main-content
                                try:
                                    self.driver.switch_to.frame("main-content")
                                except Exception:
                                    try:
                                        self.driver.switch_to.frame("mainFrame")
                                    except Exception:
                                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9099, exc_info=True)

                                # 搜尋表格中的行數或最新記錄
                                rows = self.driver.find_elements(By.XPATH, "//table//tr")
                                if rows:
                                    apply_evidence["list_row_count"] = len(rows)
                                    self.log(f"  ✅ 列表頁確認：共 {len(rows)} 筆記錄")

                                    # 嘗試截圖列表頁
                                    try:
                                        list_screenshot = os.path.join(screenshots_dir, f"apply_list_{ts_str}.png")
                                        self.driver.save_screenshot(list_screenshot)
                                        apply_evidence["list_screenshot"] = list_screenshot
                                        self.log(f"  📸 列表頁截圖已保存: {list_screenshot}")
                                    except Exception:
                                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9114, exc_info=True)
                                else:
                                    self.log("  ⚠️ 列表頁未找到記錄行，可能頁面結構不同")
                            except Exception as frame_e:
                                self.log(f"  ⚠️ 列表頁 frame 切換失敗: {frame_e}")
                        else:
                            self.log("  ⚠️ 無法導航到列表頁，跳過列表驗證")
                    except Exception as list_e:
                        self.log(f"  ⚠️ 列表頁驗證失敗: {list_e}")

                    # (d) 記錄成功證據到日誌
                    try:
                        evidence_summary = f"成功證據: submitted={apply_evidence.get('submitted')}"
                        if apply_evidence.get("application_number"):
                            evidence_summary += f", 聲請編號={apply_evidence['application_number']}"
                        if apply_evidence.get("screenshot"):
                            evidence_summary += f", 截圖={os.path.basename(apply_evidence['screenshot'])}"
                        if apply_evidence.get("list_row_count"):
                            evidence_summary += f", 列表筆數={apply_evidence['list_row_count']}"
                        self.log(f"  📋 {evidence_summary}")
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9135, exc_info=True)

                    # 記錄聲請（用於追蹤首次聲請）
                    try:
                        reg_key = self.make_apply_registry_key(case_info)
                        self.record_application(reg_key, case_info)
                        self.log(f"  📝 已記錄聲請: {reg_key}")
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9143, exc_info=True)

                    # 將證據附加到回傳值（使用特殊格式以便上層解析）
                    evidence_json = ""
                    try:
                        evidence_json = "|" + json.dumps(apply_evidence, ensure_ascii=False)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9150, exc_info=True)
                    return "Applied" + evidence_json
                else:
                    self.log("  ⚠️ 找不到「確認送出」按鈕")
                    return "Error"
            else:
                # === 預覽確認模式：截圖 + 回傳 evidence 供上層產生確認碼 ===
                ready_evidence = {"submit_ready": True}
                try:
                    import datetime as _dt
                    _ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                    _mode = "paper" if _is_paper else "electronic"
                    _shot_name = f"preview_{_mode}_{_ts}.png"
                    _shot_dir = os.path.join(self.download_folder, "screenshots")
                    os.makedirs(_shot_dir, exist_ok=True)
                    _shot_path = os.path.join(_shot_dir, _shot_name)
                    self.driver.save_screenshot(_shot_path)
                    ready_evidence["screenshot"] = _shot_path
                    self.log(f"  📸 預覽截圖已保存: {_shot_path}")
                except Exception as _ss_e:
                    self.log(f"  ⚠️ 預覽截圖失敗: {_ss_e}")
                ready_evidence["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                self._last_apply_for_review_evidence = ready_evidence
                self.log("")
                self.log("  ★ 表單已填寫完成，等待確認碼確認後送出 ★")
                self.log("")
                evidence_json = ""
                try:
                    evidence_json = "|" + json.dumps(ready_evidence, ensure_ascii=False)
                except Exception:
                    pass
                return "Ready" + evidence_json
            
        except Exception as e:
            self.log(f"  ❌ 聲請流程發生錯誤: {e}")
            import traceback
            traceback.print_exc()
            return "Error"
        finally:
             # ★ 恢復正常的隱式等待 (重要)
             self.driver.implicitly_wait(10)

    def _fill_input(self, label_text: str, value: str):
        """Helper to fill input by label text (fuzzy match)"""
        # Try to find input by preceding label or placeholder
        if not value:
            return
        
        self.log(f"    填寫欄位 [{label_text}]: {value}")
        try:
            target = None
            # 嘗試多種定位策略
            xpath_list = [
                # 1. Label 後的第一個 Input
                f"//label[contains(text(), '{label_text}')]/following::input[1]",
                # 2. 表格佈局: Label 所在 TD 的下一個 TD 中的 Input
                f"//label[contains(text(), '{label_text}')]/ancestor::td/following-sibling::td//input[1]",
                # 3. placeholder or title
                f"//input[@placeholder='{label_text}']",
                f"//input[@title='{label_text}']",
                # 4. 常見 ID 猜測 (如果 label 是 "年度")
                f"//input[contains(@id, 'year')]" if '年度' in label_text else "//nonexistent",
                f"//input[contains(@id, 'court')]" if '法院' in label_text else "//nonexistent",
            ]
            
            for xpath in xpath_list:
                try:
                    target = self.driver.find_element(By.XPATH, xpath)
                    if target:
                        break
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9202, exc_info=True)
            
            if target:
                try:
                    target.clear()
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9208, exc_info=True)
                target.send_keys(value)
            else:
                self.log(f"    ⚠️ 找不到欄位 [{label_text}]，嘗試透過 JS 或 Tab...")
                # Fallback: 如果找不到，嘗試切換焦點 (不建議盲目使用，這裡僅記錄)
                pass
                
        except Exception as e:
            self.log(f"    ⚠️ 填寫 [{label_text}] 失敗: {e}")
    
    def _calculate_md5(self, filepath: str) -> str:
        """計算檔案 MD5"""
        hash_md5 = hashlib.md5()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    
    def _load_md5_records(self) -> dict:
        """載入 MD5 記錄"""
        if os.path.exists(self.md5_records_file):
            try:
                with open(self.md5_records_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9233, exc_info=True)
        return {}
    
    def _save_md5_records(self):
        """儲存 MD5 記錄"""
        try:
            with open(self.md5_records_file, 'w', encoding='utf-8') as f:
                json.dump(self.md5_records, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"⚠️ 儲存 MD5 記錄失敗: {e}")
    
    def check_stale_cases(self, review_folder_path: str, days: int = 90) -> List[Dict]:
        """
        檢查待閱卷案件 (超過指定天數未更新)
        
        Args:
            review_folder_path: 閱卷資料根資料夾路徑
            days: 超過多少天視為待閱卷
            
        Returns:
            待閱卷案件列表
        """
        stale_cases = []
        cutoff_date = datetime.now() - timedelta(days=days)
        
        try:
            if not os.path.exists(review_folder_path):
                return stale_cases
            
            # 遞迴掃描資料夾
            for root, dirs, files in os.walk(review_folder_path):
                if not files:
                    continue
                
                # 取得最新檔案的修改時間
                latest_time = None
                for filename in files:
                    filepath = os.path.join(root, filename)
                    mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                    if latest_time is None or mtime > latest_time:
                        latest_time = mtime
                
                if latest_time and latest_time < cutoff_date:
                    # 超過天數，加入待閱卷列表
                    stale_cases.append({
                        'folder': root,
                        'last_update': latest_time,
                        'days_since_update': (datetime.now() - latest_time).days
                    })
                    
        except Exception as e:
            self.log(f"⚠️ 檢查待閱卷失敗: {e}")
        
        return stale_cases
    
    
    def notify_payment_needed(self, info: FileReviewInfo, webhook_url: str = None):
        """傳送繳費單通知。走 red_phone（TG + DC mirror），附件走 LAFNotifier。"""
        try:
            files = info.files or []
            existing_files = [fp for fp in files if os.path.exists(fp)]
            court_case_no = self._notification_case_no(info)

            # 手動標記已繳費 → 跳過通知（所有路徑統一攔截）
            _ck = court_case_no or info.court_case_no or info.laf_case_no or ""
            _party = info.client_name or ""
            _notify_key = f"web_payment:case:{_ck}:{_party}" if _ck else ""
            if self._is_payment_dismissed(_notify_key, f"web_payment:{_ck}"):
                self.log(f"  ℹ️ 已標記為已繳費，跳過通知: {_ck} {_party}")
                return True  # 回傳 True 讓呼叫端視為「已處理」

            self.log(f"  [DEBUG] notify_payment_needed: existing_files={existing_files}")

            msg = f"💰 繳費單通知\n{info.client_name} - {court_case_no}\n法院: {info.court or '-'}\n繳費期限: {info.payment_deadline or '-'}"
            if info.laf_case_no:
                msg += f"\n法扶案號: {info.laf_case_no}"
            if info.application_no and info.application_no != info.laf_case_no:
                msg += f"\n申請編號: {info.application_no}"

            any_ok = False

            # ── red_phone: TG 推送 + DC mirror ──────────────────
            try:
                from skills.ops.red_phone import send_telegram_push_with_status
                st = send_telegram_push_with_status(
                    msg,
                    severity="info",
                    source="file_review_orchestrator",
                    topic_key="filereview_payment",
                    queue_on_fail=True,
                ) or {}
                if bool(st.get("telegram")) or bool(st.get("queued")):
                    any_ok = True
                    self.log(f"  ✅ red_phone 繳費通知已送達: {court_case_no}")
                else:
                    self.log(f"  ⚠️ red_phone 送達失敗: {st.get('error', '')[:80]}")
            except Exception as rp_e:
                self.log(f"  ⚠️ red_phone import/send 失敗: {rp_e}")

            # ── 附件：TG 走 LAFNotifier，DC 走 red_phone ─────────
            if existing_files:
                file_caption = f"📎 繳費單 PDF — {info.client_name} {court_case_no}"
                # TG 附件
                try:
                    ensure_path_on_sys_path(get_orch_dir())
                    from line_notifier import LAFNotifier
                    notifier = LAFNotifier()
                    tg_file_ok = notifier.notify_admin_with_files(
                        file_caption, existing_files,
                        topic_key="filereview_payment",
                        source="file_review_orchestrator",
                    )
                    if tg_file_ok:
                        any_ok = True
                        self.log(f"  ✅ TG PDF 附件已送出 ({len(existing_files)} 份)")
                    else:
                        self.log(f"  ⚠️ TG PDF 附件送出失敗")
                except Exception as file_e:
                    self.log(f"  ⚠️ LAFNotifier 附件發送失敗: {file_e}")
                # DC 附件
                try:
                    from skills.ops.red_phone import send_discord_bot_file
                    for fp in existing_files:
                        dc_file_ok = send_discord_bot_file(
                            fp, caption=file_caption,
                            topic_key="filereview_payment",
                            source="file_review_orchestrator",
                        )
                        if dc_file_ok:
                            self.log(f"  ✅ DC PDF 已上傳: {os.path.basename(fp)}")
                        else:
                            self.log(f"  ⚠️ DC PDF 上傳失敗: {os.path.basename(fp)}")
                except Exception as dc_e:
                    self.log(f"  ⚠️ DC 檔案上傳失敗: {dc_e}")

            # ── Fallback: red_phone 不可用時嘗試直接 TG ──────────
            if not any_ok:
                try:
                    ensure_path_on_sys_path(get_orch_dir())
                    from line_notifier import LAFNotifier
                    notifier = LAFNotifier()
                    tg_ok = notifier.notify_admin(
                        msg + ("\n(已附上 PDF 繳費單)" if existing_files else "\n(目前無 PDF 附件)"),
                        topic_key="filereview_payment",
                        source="file_review_orchestrator",
                    )
                    if tg_ok:
                        any_ok = True
                        self.log(f"  ✅ LAFNotifier fallback 繳費通知已送達: {court_case_no}")
                except Exception as fb_e:
                    self.log(f"  ⚠️ LAFNotifier fallback 也失敗: {fb_e}")

            if any_ok:
                self.log(f"✅ 繳費通知已送達: {court_case_no}")
            else:
                self.log(f"❌ 繳費通知全部管道失敗: {court_case_no}")

            return any_ok

        except Exception as e:
            self.log(f"❌ 發送繳費通知失敗: {e}")
            import traceback
            traceback.print_exc()
            return False

    def _download_email_attachments(self, msg_id: str, message: Dict = None) -> List[str]:
        """
        下載 Gmail 郵件的附件
        
        Args:
            msg_id: Gmail 信件 ID
            message: 可選，已取得的信件資料 (避免重複 API 請求)
            
        Returns:
            已下載的檔案路徑列表
        """
        downloaded_files = []
        
        if not self.gmail_service:
            return downloaded_files
        
        try:
            # 如果沒有傳入 message，則從 API 取得
            if message is None:
                message = self.gmail_service.users().messages().get(
                    userId='me', id=msg_id, format='full'
                ).execute()
            
            payload = message.get('payload', {})
            parts = payload.get('parts', [])
            
            # 如果沒有 parts，可能附件在 payload 本身
            if not parts and payload.get('filename'):
                parts = [payload]
            
            for part in parts:
                filename = part.get('filename', '')
                mime_type = part.get('mimeType', '')
                
                # 只處理 PDF 或常見附件類型
                if not filename:
                    continue
                    
                # 支援的附件類型
                valid_extensions = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.png', '.jpg', '.jpeg']
                if not any(filename.lower().endswith(ext) for ext in valid_extensions):
                    continue
                
                self.log(f"  📎 發現附件: {filename} ({mime_type})")
                
                # 取得附件資料
                body = part.get('body', {})
                attachment_id = body.get('attachmentId')
                
                if attachment_id:
                    # 需要額外請求取得附件內容
                    att = self.gmail_service.users().messages().attachments().get(
                        userId='me', messageId=msg_id, id=attachment_id
                    ).execute()
                    file_data = base64.urlsafe_b64decode(att['data'].encode('UTF-8'))
                elif body.get('data'):
                    # 附件內嵌在 body 中
                    file_data = base64.urlsafe_b64decode(body['data'].encode('UTF-8'))
                else:
                    self.log(f"  ⚠️ 無法取得附件資料: {filename}")
                    continue
                
                # 儲存到下載資料夾
                file_path = os.path.join(self.download_folder, filename)
                
                # 如果檔案已存在，加上時間戳
                if os.path.exists(file_path):
                    base, ext = os.path.splitext(filename)
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    filename = f"{base}_{timestamp}{ext}"
                    file_path = os.path.join(self.download_folder, filename)
                
                with open(file_path, 'wb') as f:
                    f.write(file_data)
                
                downloaded_files.append(file_path)
                self.log(f"  ✅ 已下載附件: {filename}")
                
        except Exception as e:
            self.log(f"  ❌ 下載附件失敗: {e}")
            import traceback
            traceback.print_exc()
        
        return downloaded_files

    def preview_recent_emails(self, days: int = 7, max_results: int = 20, allow_interactive: bool = False) -> List[Dict[str, Any]]:
        """
        正式信件掃描 + 通知預覽（不下載附件、不寫入 processed_emails、不發通知）

        用途：
        - 先確認 token/credentials 是否可用
        - 先看「有哪些案號/類型」將會被觸發處理
        """
        if not self.gmail_service:
            if not self._init_gmail(allow_interactive=bool(allow_interactive)):
                return []

        from datetime import timedelta
        check_date = (datetime.now() - timedelta(days=max(1, int(days or 7)))).strftime('%Y/%m/%d')

        queries = [
            ("payment", f"(法院 回覆 閱卷 結果 通知 OR 含繳費單 OR 待繳費 OR 繳費期限) after:{check_date}"),
            ("download", f"(法院 完成 線上 交付 核閱 通知 OR 線上下載 OR 交付核閱 OR 核閱通知) after:{check_date}"),
            # 備援：法院主旨格式常變動（含全形空白/不同片語），先撈回來再由內容分類。
            ("auto", f"(閱卷 OR 閱 卷 OR 複製電子卷證 OR 線上聲請閱卷暨聲請複製電子卷證系統) after:{check_date}"),
        ]

        out: List[Dict[str, Any]] = []

        def _has_attachment(payload: Dict[str, Any]) -> bool:
            try:
                parts = (payload or {}).get("parts") or []
                if not parts:
                    return bool((payload or {}).get("filename"))
                stack = list(parts)
                while stack:
                    p = stack.pop()
                    if (p.get("filename") or "").strip():
                        return True
                    stack.extend(p.get("parts") or [])
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9511, exc_info=True)
            return False

        for typ, q in queries:
            try:
                results = self.gmail_service.users().messages().list(userId='me', q=q, maxResults=max_results).execute()
                messages = results.get('messages', []) or []
            except Exception as e:
                self._last_gmail_error = str(e)[:300]
                self.log(f"❌ 預覽 Gmail 搜尋失敗: {e}")
                # 預覽用途是「確認 token/權限可用」，這裡直接中止回傳空結果，讓上層判斷需要重新授權。
                return []

            for msg in messages:
                try:
                    msg_id = msg.get("id") or ""
                    message = self.gmail_service.users().messages().get(userId='me', id=msg_id, format='metadata').execute()
                    headers = (message.get("payload") or {}).get("headers") or []
                    subject = next((h.get('value') for h in headers if (h.get('name') or '') == 'Subject'), '') or ''
                    snippet = (message.get("snippet") or "") or ""

                    text_to_search = self._normalize_case_text(f"{subject} {snippet}")
                    ids = self._extract_case_identifiers(text_to_search)
                    resolved = ids.get("court_case_no") or ids.get("laf_case_no") or ids.get("application_no") or ""

                    msg_type = typ
                    if typ == "auto":
                        t = self._normalize_case_text(f"{subject} {snippet}")
                        if any(k in t for k in ["繳費單", "待繳費", "繳費期限", "含繳費單"]):
                            msg_type = "payment"
                        elif any(k in t for k in ["線上下載", "交付核閱", "核閱通知", "下載期限"]):
                            msg_type = "download"
                        else:
                            msg_type = "other"

                    out.append({
                        "type": msg_type,
                        "message_id": msg_id,
                        "subject": subject,
                        "resolved_case_no": resolved,
                        "court_case_no": ids.get("court_case_no", ""),
                        "laf_case_no": ids.get("laf_case_no", ""),
                        "application_no": ids.get("application_no", ""),
                        "has_attachment": _has_attachment(message.get("payload") or {}),
                        "already_processed": (msg_id in self.processed_emails),
                    })
                except Exception:
                    continue

        return out[: max_results * 2]

    def process_emails(self) -> dict:
        """處理所有相關郵件 (繳費單 + 下載通知)，回傳統計摘要。"""
        summary = {
            "payment_hits": 0,
            "payment_notified": 0,
            "download_hits": 0,
            "ready_to_download_count": 0,
            "errors": [],
        }
        if not self.gmail_service:
            # 排程/自動巡檢預設不做互動式 OAuth，避免卡住流程。
            if not self._init_gmail(allow_interactive=False):
                return summary

        try:
            self.log("正在檢查繳費單與下載通知信件...")
            # 重置待下載清單
            self.ready_to_download = []

            from datetime import timedelta
            check_date = (datetime.now() - timedelta(days=7)).strftime('%Y/%m/%d')

            # 排除法扶來源信件，避免與法扶通知系統重複處理
            _excl_laf = " -from:@laf.org.tw -from:laf.server"

            # A. 繳費單通知
            query_payment = f"(法院 回覆 閱卷 結果 通知 OR 含繳費單 OR 待繳費 OR 繳費期限) after:{check_date}{_excl_laf}"
            r_pay = self._scan_and_process_emails(query_payment, "payment")
            summary["payment_hits"] += r_pay.get("hits", 0)
            summary["payment_notified"] += r_pay.get("notified", 0)
            summary["errors"].extend(r_pay.get("errors", []))

            # B. 下載通知
            query_download = f"(法院 完成 線上 交付 核閱 通知 OR 線上下載 OR 交付核閱 OR 核閱通知) after:{check_date}{_excl_laf}"
            r_dl = self._scan_and_process_emails(query_download, "download")
            summary["download_hits"] += r_dl.get("hits", 0)
            summary["errors"].extend(r_dl.get("errors", []))

            # C. 備援掃描（主旨格式改版/插空白時）
            query_auto = f"(閱卷 OR 閱 卷 OR 複製電子卷證 OR 線上聲請閱卷暨聲請複製電子卷證系統) after:{check_date}{_excl_laf}"
            r_auto = self._scan_and_process_emails(query_auto, "auto")
            summary["payment_hits"] += r_auto.get("payment_hits", 0)
            summary["payment_notified"] += r_auto.get("payment_notified", 0)
            summary["download_hits"] += r_auto.get("download_hits", 0)
            summary["errors"].extend(r_auto.get("errors", []))

            summary["ready_to_download_count"] = len(self.ready_to_download)

        except Exception as e:
            self.log(f"❌ 處理郵件失敗: {e}")
            summary["errors"].append(str(e)[:200])

        return summary

    def _scan_and_process_emails(self, query: str, type: str) -> dict:
        """掃描並處理特定類型的郵件，回傳統計。"""
        # 統計：hits=命中數, notified=成功通知數
        # auto 模式可能同時產出 payment 和 download，用複合 key 回傳
        stats = {"hits": 0, "notified": 0, "download_hits": 0,
                 "payment_hits": 0, "payment_notified": 0, "errors": []}
        try:
            self.log(f"  🔍 [DEBUG] 執行搜尋 query: {query}")
            results = self.gmail_service.users().messages().list(userId='me', q=query).execute()
            messages = results.get('messages', [])
            self.log(f"  📊 [DEBUG] Gmail API 回傳 {len(messages)} 封符合的郵件")

            for msg in messages:
                msg_id = msg['id']
                if msg_id in self.processed_emails:
                    continue

                # 取得信件詳細內容
                message = self.gmail_service.users().messages().get(userId='me', id=msg_id).execute()
                headers = message.get('payload', {}).get('headers', [])
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                sender = next((h['value'] for h in headers if h['name'] == 'From'), '')

                # 排除法扶來源信件（避免與法扶通知系統重疊）
                sender_lower = sender.lower()
                if '@laf.org.tw' in sender_lower or 'laf.server' in sender_lower:
                    self.processed_emails.add(msg_id)
                    continue

                self.log(f"  📨 [DEBUG] 檢查信件 [{msg_id}] 主旨: {subject}")

                # 解析內文
                body = ""
                if 'data' in message['payload']['body']:
                    import base64
                    try:
                        body = base64.urlsafe_b64decode(message['payload']['body']['data']).decode('utf-8')
                    except Exception as e: logger.debug("Failed to decode email body data: %s", e)
                elif 'parts' in message['payload']:
                    parts = message['payload']['parts']
                    for part in parts:
                        if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                            import base64
                            try:
                                body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                            except Exception as e: logger.debug("Failed to decode text/plain part data: %s", e)
                            break
                        # 如果沒有 text/plain，嘗試找 text/html
                        elif part['mimeType'] == 'text/html' and 'data' in part['body'] and not body:
                            import base64
                            try:
                                body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                            except Exception as e: logger.debug("Failed to decode text/html part data: %s", e)

                text_to_search = self._normalize_case_text(f"{body} {subject}")
                ids = self._extract_case_identifiers(text_to_search)
                court_case_no = ids["court_case_no"]
                fallback_case_no = ids["laf_case_no"] or ids["application_no"]
                resolved_case_no = court_case_no or fallback_case_no or "Unknown"

                if resolved_case_no == "Unknown":
                    self.log(f"  ⚠️ [DEBUG] 無法提取案號，內文前 200 字: {text_to_search[:200]}...")

                # 根據類型處理（auto 模式先由內容自動分類）
                msg_type = type
                if type == "auto":
                    t = self._normalize_case_text(f"{subject} {body}")
                    if any(k in t for k in ["繳費單", "待繳費", "繳費期限", "含繳費單"]):
                        msg_type = "payment"
                    elif any(k in t for k in ["線上下載", "交付核閱", "核閱通知", "下載期限"]):
                        msg_type = "download"
                    else:
                        msg_type = "other"

                if msg_type == "payment":
                    stats["hits"] += 1
                    if type == "auto":
                        stats["payment_hits"] += 1
                    if "繳費" in body or "附件" in body: # 簡單判斷
                         # 建立 Info 物件
                         info = FileReviewInfo()
                         info.court_case_no = court_case_no
                         info.laf_case_no = ids["laf_case_no"]
                         info.application_no = ids["application_no"]
                         info.message_id = msg_id

                         # ★ 下載附件 (繳費單 PDF)
                         self.log(f"  📥 正在下載繳費通知附件...")
                         downloaded_files = self._download_email_attachments(msg_id, message)
                         info.files = downloaded_files
                         self.log(f"  📎 共下載 {len(downloaded_files)} 個附件")

                         # 標記已處理 (processed_emails 是 set，只記錄 msg_id)
                         self.processed_emails.add(msg_id)
                         self._save_processed_emails()

                         # 通知 (含附件) — key 統一加 web_payment: 前綴避免與 web 掃描重複
                         notify_key = f"web_payment:{info.court_case_no or info.laf_case_no or info.application_no or msg_id}"
                         if notify_key not in self.notified_cases:
                             sent_ok = bool(self.notify_payment_needed(info))
                             if sent_ok:
                                 self.notified_cases.add(notify_key)
                                 self._save_notified_cases()  # 持久化，避免重複通知
                                 stats["notified"] += 1
                                 if type == "auto":
                                     stats["payment_notified"] += 1
                             else:
                                 self.log(f"  ⚠️ 繳費通知未送達，暫不標記已通知: {notify_key}")

                elif msg_type == "download":
                    stats["hits"] += 1
                    if type == "auto":
                        stats["download_hits"] += 1
                    # 下載通知
                    self.log(f"  發現可下載案件: {resolved_case_no}")

                    # 加入待下載清單
                    info = FileReviewInfo()
                    info.court_case_no = court_case_no
                    info.laf_case_no = ids["laf_case_no"]
                    info.application_no = ids["application_no"]
                    info.message_id = msg_id
                    self.ready_to_download.append(info)

                    # 標記已處理 (下載成功後再標記可能更好，但在這裡標記代表「已讀」)
                    # processed_emails 是 set，只記錄 msg_id
                    self.processed_emails.add(msg_id)
                    self._save_processed_emails()

        except Exception as e:
            self.log(f"  ⚠️ 掃描郵件失敗: {e}")
            stats["errors"].append(str(e)[:200])

        return stats

    def process_auto_drafts(self, days: int = 2, max_results: int = 15):
        """自動掃描近期信件，排除法院與法扶信件後，判斷是否需要自動草擬回信"""
        if not self.gmail_service:
            if not self._init_gmail(allow_interactive=False):
                return
        
        try:
            self.log("正在檢查是否有需要自動草擬回覆的信件 (Non-LAF/Non-Judicial)...")
            from datetime import timedelta
            check_date = (datetime.now() - timedelta(days=days)).strftime('%Y/%m/%d')
            
            # Search for emails in Inbox that don't have "法院", "司法院", "法扶", "法律扶助"
            query = f"in:inbox -subject:(法院 OR 司法院 OR 法扶 OR 法律扶助 OR 閱卷 OR 案件進度) after:{check_date}"
            
            try:
                results = self.gmail_service.users().messages().list(userId='me', q=query, maxResults=max_results).execute()
                messages = results.get('messages', [])
            except Exception as e:
                self.log(f"⚠️ 搜尋自動草擬信件失敗: {e}")
                return
            
            if not messages:
                self.log("  ℹ️ 無需要處理的信件。")
                return
            draft_processed_file = os.path.join(str(get_orch_dir()), ".draft_processed_emails.json")
            processed = set()
            try:
                if os.path.exists(draft_processed_file):
                    with open(draft_processed_file, "r") as f:
                        processed = set(json.load(f))
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 9782, exc_info=True)
                
            for msg in messages:
                msg_id = msg['id']
                if msg_id in processed:
                    continue
                    
                message = self.gmail_service.users().messages().get(userId='me', id=msg_id).execute()
                headers = message['payload']['headers']
                subject = next((h['value'] for h in headers if h['name'] == 'Subject'), 'No Subject')
                sender = next((h['value'] for h in headers if h['name'] == 'From'), 'Unknown')
                
                # Skip automated or no-reply emails
                if "no-reply" in sender.lower() or "noreply" in sender.lower() or "postmaster" in sender.lower() or "system" in sender.lower():
                    processed.add(msg_id)
                    continue
                    
                body = ""
                if 'data' in message['payload']['body']:
                    import base64
                    try:
                        body = base64.urlsafe_b64decode(message['payload']['body']['data']).decode('utf-8')
                    except Exception as e: logger.debug("Failed to decode draft body data: %s", e)
                elif 'parts' in message['payload']:
                    for part in message['payload']['parts']:
                        if part['mimeType'] == 'text/plain' and 'data' in part['body']:
                            import base64
                            try:
                                body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                            except Exception as e: logger.debug("Failed to decode draft part data: %s", e)
                            break

                if not body:
                    processed.add(msg_id)
                    continue
                    
                self.log(f"  📧 分析信件: {subject[:30]}... (from {sender})")
                
                # Use CASPER LLM to determine if reply needed
                try:
                    import sys
                    ensure_path_on_sys_path(get_orch_dir())
                    from casper_tools_client import casper_chat
                    
                    prompt = (
                        "你是一個律師事務所助理。請閱讀以下來信，決定是否需要回覆。\n"
                        "如果只是通知信、廣告信、或是無需回覆的確認信，請回答 'NO'。\n"
                        "如果是當事人詢問案件、預約諮詢、或是請求提供資訊，請回答 'YES'，並且直接給出一段草擬的回覆（繁體中文），語氣專業禮貌。\n"
                        "如果需要回覆，格式請嚴格使用:\n"
                        "YES\n"
                        "[草擬內容]\n\n"
                        f"主旨: {subject}\n"
                        f"寄件者: {sender}\n"
                        f"內文:\n{body[:1000]}"
                    )
                    
                    r = casper_chat(prompt, timeout_sec=60)
                    if r.get("success"):
                        response = r.get("response", "").strip()
                        if response.startswith("YES"):
                            draft_body = response[3:].strip()
                            self.log(f"    📝 判斷需要回覆，正在草擬...")
                            
                            draft_py = os.path.join(code_dir, "MAGI", "skills", "gmail-drafts", "action.py")
                            if os.path.exists(draft_py):
                                import subprocess
                                payload = {
                                    "to": sender,
                                    "subject": f"Re: {subject}",
                                    "body": draft_body,
                                    "interactive": False
                                }
                                task_arg = "create " + json.dumps(payload, ensure_ascii=False)
                                venv_py = os.path.join(code_dir, ".venv", "bin", "python")
                                subprocess.run([venv_py, draft_py, "--task", task_arg], capture_output=True, timeout=60)
                                self.log("    ✅ 已通過 gmail-drafts 建立草稿。")
                            else:
                                self.log("    ⚠️ 找不到 gmail-drafts/action.py")
                        else:
                            self.log("    ⏭️ 無需回覆。")
                except Exception as e:
                    self.log(f"    ⚠️ LLM 判斷失敗: {e}")
                    
                processed.add(msg_id)
                
            try:
                with open(draft_processed_file, "w") as f:
                    json.dump(list(processed), f)
            except Exception as e: logger.debug("Failed to save draft processed file: %s", e)
            
        except Exception as e:
            self.log(f"❌ 處理自動草擬失敗: {e}")

    def close(self):
        """關閉並清理 debug 截圖"""
        # 清理 debug 截圖
        self._cleanup_debug_files()
        
        # 確保瀏覽器被正確關閉
        try:
            # 如果 driver 存在且與 sso.driver 不同，先關閉它
            if self.driver and (not self.sso or self.driver is not self.sso.driver):
                try:
                    self.driver.quit()
                    self.log("  🔒 已關閉獨立 Chrome 實例")
                except Exception as e:
                    self.log(f"  ⚠️ 關閉 driver 時發生錯誤 (可忽略): {e}")
            
            # 關閉 SSO 的瀏覽器
            if self.sso:
                try:
                    self.sso.close()
                except Exception as e:
                    self.log(f"  ⚠️ 關閉 SSO 時發生錯誤 (可忽略): {e}")
        finally:
            # 無論如何都要清空參考
            self.driver = None
            self.sso = None
            self.logged_in = False
    
    def _cleanup_debug_files(self):
        """清理所有 debug 截圖和 HTML 檔案"""
        try:
            if self.no_delete:
                self.log("🔒 MAGI_NO_DELETE=1，略過 debug 檔案清理")
                return

            import glob
            
            # Debug 檔案模式
            debug_patterns = [
                "debug_*.png",
                "debug_*.html",
                "apply_form_*.png",
                "apply_after_check_*.png",
                "apply_final_*.png",
            ]
            
            # 在當前目錄和下載資料夾中搜尋
            search_dirs = ['.']
            if hasattr(self, 'download_folder') and self.download_folder:
                search_dirs.append(self.download_folder)
            
            deleted_count = 0
            for search_dir in search_dirs:
                for pattern in debug_patterns:
                    full_pattern = os.path.join(search_dir, pattern)
                    for file_path in glob.glob(full_pattern):
                        try:
                            if safe_remove:
                                safe_remove(file_path, reason="cleanup_debug", allow_delete=True, log=self.log)
                                self.log(f"📦 已隔離 debug 檔案: {os.path.basename(file_path)}")
                                deleted_count += 1
                            else:
                                self.log(f"🔒 safe_remove 不可用，略過 debug 檔案清理: {os.path.basename(file_path)}")
                        except Exception as e:
                            self.log(f"⚠️ 刪除失敗: {e}")
            
            if deleted_count > 0:
                self.log(f"✅ 共清理 {deleted_count} 個 debug 檔案")
                        
        except Exception as e:
            self.log(f"⚠️ 清理 debug 檔案時發生錯誤: {e}")


# =============================================================================
# 測試
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("  測試閱卷自動化模組")
    print("=" * 60)
    
    # 安全：不在程式碼內硬編帳號密碼。請用環境變數提供。
    username = (os.environ.get("FILE_REVIEW_USERNAME") or "").strip()
    password = (os.environ.get("FILE_REVIEW_PASSWORD") or "").strip()
    headless = (os.environ.get("FILE_REVIEW_HEADLESS") or "1").strip() != "0"

    if not username or not password:
        print("請先設定環境變數 FILE_REVIEW_USERNAME / FILE_REVIEW_PASSWORD 再進行手動測試。")
        raise SystemExit(2)

    manager = FileReviewManager(
        username=username,
        password=password,
        headless=headless,
        log_callback=lambda msg: print(msg),
    )
    
    if manager.login():
        print("\n登入成功！")
        
        # 導航到閱卷系統
        if manager.navigate_to_file_review():
            print("進入閱卷系統成功！")
            
            # 檢查可下載項目
            files = manager.check_and_download_available()
            print(f"下載了 {len(files)} 個檔案")
            
            # 注意：避免在預設手動測試流程中執行「閱卷聲請」以免誤送出。
            # 若需測試 apply_for_review，請在你確認環境與測試案件後自行呼叫。
        
        # input("按 Enter 關閉...")
        manager.close()
    else:
        print("\n登入失敗")
        manager.close()

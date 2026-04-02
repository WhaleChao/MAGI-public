# Web Automation & Scraping 實戰指南

> **對象**：MAGI 系統內任何需要「自動化操作網站 / 爬取 HTML / 建構模擬站」的開發工作。
> 本文件濃縮自法扶 (LAF)、司法院 OLA 閱卷系統等實戰經驗。

---

## 目錄

1. [偵察階段：先搞懂網站結構](#1-偵察階段)
2. [選擇工具鏈](#2-選擇工具鏈)
3. [反偵測與偽裝](#3-反偵測與偽裝)
4. [登入與驗證碼](#4-登入與驗證碼)
5. [頁面導航與等待策略](#5-頁面導航與等待策略)
6. [iframe / frameset 處理](#6-iframe--frameset-處理)
7. [表單自動填寫](#7-表單自動填寫)
8. [檔案上傳](#8-檔案上傳)
9. [資料擷取](#9-資料擷取)
10. [重試、容錯與降級](#10-重試容錯與降級)
11. [Session 與 Cookie 管理](#11-session-與-cookie-管理)
12. [建構模擬 / 訓練站](#12-建構模擬站)
13. [除錯工具箱](#13-除錯工具箱)
14. [安全守則](#14-安全守則)
15. [Checklist：新網站自動化啟動清單](#15-checklist)

---

## 1. 偵察階段

在寫任何程式碼之前，先手動操作目標網站並記錄以下資訊：

### 1.1 DOM 結構快照

```python
# 儲存當前頁面完整 HTML（含動態渲染後的 DOM）
with open("snapshot.html", "w", encoding="utf-8") as f:
    f.write(driver.page_source)
```

### 1.2 必須記錄的資訊

| 項目 | 為什麼重要 | 記錄方式 |
|------|-----------|---------|
| **URL 路由模式** | 判斷是 SPA 還是多頁式 | 記錄每步操作的 URL 變化 |
| **iframe / frameset** | 不切 frame 就找不到元素 | `document.querySelectorAll('iframe, frame')` |
| **表單 action URL** | 手動 POST 時需要完整路徑 | DevTools → Network → 攔截 submit |
| **AJAX 端點** | 很多操作不是 form submit 而是 XHR | DevTools → Network → XHR filter |
| **動態載入時機** | 元素什麼時候才會出現 | 觀察 DOMContentLoaded vs onload vs AJAX callback |
| **驗證碼機制** | 圖片? reCAPTCHA? 簡訊? | 登入頁截圖 |
| **Cookie / Token** | Session 維持方式 | DevTools → Application → Cookies |

### 1.3 偵察腳本範本

```python
def reconnaissance(driver, url):
    """偵察目標頁面結構，產出報告。"""
    driver.get(url)
    time.sleep(3)

    report = {
        "url": driver.current_url,
        "title": driver.title,
        "iframes": [],
        "forms": [],
        "inputs": [],
        "buttons": [],
        "links_count": 0,
    }

    # 找所有 iframe
    iframes = driver.find_elements(By.TAG_NAME, "iframe")
    for i, frame in enumerate(iframes):
        report["iframes"].append({
            "index": i,
            "id": frame.get_attribute("id"),
            "name": frame.get_attribute("name"),
            "src": frame.get_attribute("src"),
        })

    # 找所有 form
    forms = driver.find_elements(By.TAG_NAME, "form")
    for form in forms:
        report["forms"].append({
            "id": form.get_attribute("id"),
            "action": form.get_attribute("action"),
            "method": form.get_attribute("method"),
            "enctype": form.get_attribute("enctype"),
        })

    # 找所有 input（含 hidden）
    inputs = driver.find_elements(By.TAG_NAME, "input")
    for inp in inputs:
        report["inputs"].append({
            "type": inp.get_attribute("type"),
            "name": inp.get_attribute("name"),
            "id": inp.get_attribute("id"),
            "value": inp.get_attribute("value")[:50] if inp.get_attribute("value") else "",
        })

    return report
```

---

## 2. 選擇工具鏈

### 決策流程

```
需要 JS 渲染？ ──No──→ requests + BeautifulSoup（最輕量）
     │Yes
     ↓
需要複雜互動（拖拉、多 iframe）？ ──No──→ Playwright（推薦預設）
     │Yes
     ↓
需要保留 browser profile / 插件？ ──No──→ Playwright
     │Yes
     ↓
Selenium + Chrome（最靈活但最笨重）
```

### 工具比較

| 特性 | requests+BS4 | Playwright | Selenium |
|------|-------------|-----------|---------|
| 速度 | 最快 | 快 | 慢 |
| JS 渲染 | 不支援 | 完整支援 | 完整支援 |
| iframe 處理 | 不適用 | 簡潔 API | 需手動切換 |
| 反偵測 | N/A | 較好 | 需額外設定 |
| 檔案上傳 | 不適用 | 原生支援 | 需 send_keys |
| 瀏覽器 Profile | 不適用 | 支援 | 最佳支援 |
| Headless | N/A | 預設 | 需設定 |

### MAGI 慣例

- **輕量爬取**（RSS、API、靜態頁）：`requests` + `BeautifulSoup`
- **政府入口網站**（OLA、法扶）：`Selenium`（因需 profile 持久化 + 複雜 iframe）
- **一般動態網站**：`Playwright`（推薦）

---

## 3. 反偵測與偽裝

### 3.1 Selenium 反偵測標準設定

```python
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

def create_stealth_driver(headless=True, download_dir=None):
    opts = Options()

    # ── 核心反偵測 ──
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    # ── User-Agent 偽裝 ──
    ua = random.choice([
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    ])
    opts.add_argument(f"--user-agent={ua}")

    if headless:
        opts.add_argument("--headless=new")
        opts.add_argument("--window-size=1920,1080")

    # ── 穩定性 ──
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")

    driver = webdriver.Chrome(options=opts)

    # 移除 webdriver 屬性
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })

    # Headless 下載設定（Chrome DevTools Protocol）
    if headless and download_dir:
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": download_dir,
        })

    driver.implicitly_wait(10)
    driver.set_page_load_timeout(45)

    return driver
```

### 3.2 自然行為模擬

```python
import random, time

def human_delay(min_s=0.5, max_s=2.0):
    """模擬人類操作間隔。"""
    time.sleep(random.uniform(min_s, max_s))

def human_type(element, text, min_delay=0.03, max_delay=0.12):
    """模擬人類打字速度。"""
    element.clear()
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(min_delay, max_delay))
```

---

## 4. 登入與驗證碼

### 4.1 驗證碼處理策略（優先順序）

```
1. 環境變數覆蓋 → 測試 / 已知固定碼
2. OCR 自動辨識 → ddddocr（數字+英文）或 RapidOCR（中文）
3. 人工介入 → LINE/Discord webhook 把圖片送給人看
```

### 4.2 OCR 驗證碼範本

```python
def solve_captcha(driver, captcha_img_selector, max_attempts=5):
    """多引擎驗證碼辨識，含重試。"""
    for attempt in range(1, max_attempts + 1):
        # 截取驗證碼圖片
        img_el = driver.find_element(By.CSS_SELECTOR, captcha_img_selector)
        img_bytes = img_el.screenshot_as_png

        # 方法 1: ddddocr
        code = ""
        try:
            import ddddocr
            ocr = ddddocr.DdddOcr(show_ad=False)
            code = ocr.classification(img_bytes).strip()
        except Exception:
            pass

        # 方法 2: RapidOCR fallback
        if not code or len(code) < 4:
            try:
                from rapidocr_onnxruntime import RapidOCR
                rapid = RapidOCR()
                # 需要先存成臨時檔
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                    f.write(img_bytes)
                    tmp_path = f.name
                result, _ = rapid(tmp_path)
                if result:
                    code = "".join(r[1] for r in result).strip()
            except Exception:
                pass

        if code and len(code) >= 4:
            return code

        # 重新整理驗證碼圖片
        try:
            img_el.click()  # 很多網站點圖片會換一張
            time.sleep(1)
        except Exception:
            driver.refresh()
            time.sleep(2)

    return ""  # 全部失敗，需要人工介入
```

### 4.3 登入流程範本

```python
def login_with_retry(driver, url, username, password, max_retries=3):
    for attempt in range(1, max_retries + 1):
        try:
            driver.get(url)
            human_delay(1, 2)

            driver.find_element(By.ID, "user_id").send_keys(username)
            human_delay(0.3, 0.8)
            driver.find_element(By.ID, "user_pass").send_keys(password)
            human_delay(0.3, 0.8)

            # 驗證碼
            captcha = solve_captcha(driver, "#captcha_img")
            if captcha:
                driver.find_element(By.ID, "capText").send_keys(captcha)

            driver.find_element(By.CSS_SELECTOR, ".btn-login").click()
            human_delay(2, 4)

            # 驗證登入成功
            if _is_logged_in(driver):
                return True

        except Exception as e:
            logger.warning("Login attempt %d/%d failed: %s", attempt, max_retries, e)
            if attempt < max_retries:
                human_delay(2, 5)

    return False

def _is_logged_in(driver):
    """檢查是否真的登入成功（依網站調整）。"""
    try:
        # 方法 1: 檢查 URL 是否跳轉
        if "login" not in driver.current_url.lower():
            return True
        # 方法 2: 檢查特定元素是否出現
        driver.find_element(By.ID, "user_menu")
        return True
    except Exception:
        return False
```

---

## 5. 頁面導航與等待策略

### 5.1 等待原則

> **黃金法則**：永遠不要用 `time.sleep()` 作為主要等待手段。
> 用 `WebDriverWait` + Expected Conditions，`sleep` 只作為補充。

```python
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

def wait_and_find(driver, by, value, timeout=15):
    """等待元素出現並回傳。"""
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((by, value))
    )

def wait_and_click(driver, by, value, timeout=15):
    """等待元素可點擊並點擊。"""
    el = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, value))
    )
    el.click()
    return el

def wait_for_page_ready(driver, timeout=30):
    """等待頁面完全載入（含 AJAX）。"""
    WebDriverWait(driver, timeout).until(
        lambda d: d.execute_script("return document.readyState") == "complete"
    )
    # 額外等 AJAX（jQuery 網站常見）
    try:
        WebDriverWait(driver, 5).until(
            lambda d: d.execute_script("return typeof jQuery !== 'undefined' ? jQuery.active === 0 : true")
        )
    except Exception:
        pass
```

### 5.2 點擊失敗的備援策略

```python
def robust_click(driver, element):
    """多策略點擊，應對各種遮擋情況。"""
    # 策略 1: 直接點
    try:
        element.click()
        return True
    except Exception:
        pass

    # 策略 2: JS 點擊（繞過遮擋）
    try:
        driver.execute_script("arguments[0].click();", element)
        return True
    except Exception:
        pass

    # 策略 3: ActionChains（滾動到元素 → 點擊）
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        ActionChains(driver).move_to_element(element).click().perform()
        return True
    except Exception:
        pass

    # 策略 4: 滾動到可見再點
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        time.sleep(0.5)
        element.click()
        return True
    except Exception:
        pass

    return False
```

---

## 6. iframe / frameset 處理

> **經驗教訓**：台灣政府網站特愛用多層 iframe（OLA 用 frameset → main-content → v1/v2）。
> 忘記切 frame 是最常見的 `NoSuchElementException` 原因。

### 6.1 探測 iframe 結構

```python
def map_frame_tree(driver, depth=0, max_depth=5):
    """遞迴探測所有 iframe 結構。"""
    if depth > max_depth:
        return []

    tree = []
    frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
    for i, frame in enumerate(frames):
        info = {
            "depth": depth,
            "index": i,
            "id": frame.get_attribute("id") or "",
            "name": frame.get_attribute("name") or "",
            "src": frame.get_attribute("src") or "",
            "children": [],
        }
        try:
            driver.switch_to.frame(frame)
            info["children"] = map_frame_tree(driver, depth + 1, max_depth)
            driver.switch_to.parent_frame()
        except Exception:
            pass
        tree.append(info)
    return tree
```

### 6.2 安全切換 frame

```python
def switch_to_nested_frame(driver, frame_path: list[str]):
    """
    依序切入多層 iframe。
    frame_path: ["main-content", "v1"] → 先切 main-content 再切 v1
    """
    driver.switch_to.default_content()
    for frame_id in frame_path:
        try:
            WebDriverWait(driver, 10).until(
                EC.frame_to_be_available_and_switch_to_it(frame_id)
            )
        except Exception:
            # Fallback: 用 index 或其他屬性找
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe, frame")
            matched = [f for f in frames
                       if f.get_attribute("id") == frame_id
                       or f.get_attribute("name") == frame_id]
            if matched:
                driver.switch_to.frame(matched[0])
            else:
                raise RuntimeError(f"Frame not found: {frame_id}")
```

### 6.3 OLA 實戰模式

```python
# OLA 的 frame 結構：
# default_content
#   └─ main-content (iframe)
#       ├─ v1 (frame) ← 主要資料頁面在這
#       └─ v2 (frame)
#
# 重點：某些操作（如 colorbox overlay）的元素在 default_content 層級，
#        不在任何 iframe 裡面！

def ola_switch_to_v1(driver):
    driver.switch_to.default_content()
    WebDriverWait(driver, 10).until(
        EC.frame_to_be_available_and_switch_to_it("main-content")
    )
    WebDriverWait(driver, 10).until(
        EC.frame_to_be_available_and_switch_to_it("v1")
    )

def ola_switch_to_overlay(driver):
    """overlay / colorbox 在最上層。"""
    driver.switch_to.default_content()
```

---

## 7. 表單自動填寫

### 7.1 通用表單填寫

```python
def fill_form(driver, field_map: dict):
    """
    依 field_map 自動填寫表單。
    field_map: {
        "#name": "張裕和",                    # CSS selector → 文字
        "select#city": "option_value",        # select 下拉選單
        "input[name='agree']": True,          # checkbox
        "input[name='gender'][value='M']": True,  # radio
    }
    """
    for selector, value in field_map.items():
        try:
            el = driver.find_element(By.CSS_SELECTOR, selector)
            tag = el.tag_name.lower()
            input_type = (el.get_attribute("type") or "").lower()

            if tag == "select":
                from selenium.webdriver.support.ui import Select
                Select(el).select_by_value(str(value))
            elif input_type in ("checkbox", "radio"):
                if bool(value) != el.is_selected():
                    robust_click(driver, el)
            elif tag in ("input", "textarea"):
                el.clear()
                el.send_keys(str(value))
            else:
                # contenteditable div 等
                el.clear()
                el.send_keys(str(value))

            human_delay(0.2, 0.5)
        except Exception as e:
            logger.warning("fill_form failed for %s: %s", selector, e)
```

### 7.2 下拉選單的坑

```python
# 有些網站用假的下拉選單（div+ul 模擬），不是 <select>
# 這時 Select() 會失敗，需要用 JS 或點擊操作

def select_fake_dropdown(driver, trigger_selector, option_text):
    """處理非原生 select 的下拉選單。"""
    trigger = driver.find_element(By.CSS_SELECTOR, trigger_selector)
    trigger.click()
    human_delay(0.5, 1)

    # 找展開後的選項
    options = driver.find_elements(By.XPATH,
        f"//li[contains(text(), '{option_text}')] | "
        f"//div[contains(@class, 'option') and contains(text(), '{option_text}')]"
    )
    if options:
        options[0].click()
    else:
        # JS fallback
        driver.execute_script(f"""
            document.querySelector('{trigger_selector}').value = '{option_text}';
            document.querySelector('{trigger_selector}').dispatchEvent(new Event('change'));
        """)
```

---

## 8. 檔案上傳

> **最大教訓**：不要依賴 dialog 彈窗上傳。政府網站的 bootbox/modal 在 headless 模式下經常失敗。
> 優先用「hidden form + XHR」方式。

### 8.1 標準 file input 上傳

```python
def upload_file_standard(driver, file_input_selector, file_path):
    """最簡單的方式：直接對 <input type="file"> send_keys。"""
    file_input = driver.find_element(By.CSS_SELECTOR, file_input_selector)

    # 確保 input 可見（有些網站把它藏起來）
    driver.execute_script("""
        var el = arguments[0];
        el.style.display = 'block';
        el.style.visibility = 'visible';
        el.style.opacity = '1';
        el.style.position = 'relative';
        el.style.width = '300px';
        el.style.height = '30px';
    """, file_input)

    file_input.send_keys(os.path.abspath(file_path))
```

### 8.2 XHR 直接上傳（OLA 實戰方案）

```python
def upload_via_xhr(driver, file_path, upload_url_path="DOUPLOAD.htm"):
    """
    繞過 dialog，直接用 JS 建立 FormData 送 XHR。
    適用於：表單在頁面上但被隱藏、或 dialog 在 headless 下不穩定的情況。
    """
    abs_path = os.path.abspath(file_path)
    filename = os.path.basename(file_path)

    # 1. 讓隱藏的 file input 可見並填入檔案
    driver.execute_script("""
        var inputs = document.querySelectorAll('input[type="file"]');
        inputs.forEach(function(el) {
            el.style.display = 'block';
            el.style.visibility = 'visible';
            el.style.opacity = '1';
        });
    """)

    file_inputs = driver.find_elements(By.CSS_SELECTOR, 'input[type="file"]')
    if not file_inputs:
        return {"success": False, "error": "no file input found"}

    file_inputs[0].send_keys(abs_path)
    human_delay(0.5, 1)

    # 2. 用 XHR 送出（而非 form.submit()）
    # 注意：form.submit() 在 iframe 裡可能因 relative URL 失敗
    result = driver.execute_script(f"""
        var fileInput = document.querySelector('input[type="file"]');
        if (!fileInput || !fileInput.files || !fileInput.files.length) {{
            return 'xhr_fail:no_file_selected';
        }}
        var formData = new FormData();
        formData.append('file', fileInput.files[0]);

        // 組合絕對 URL
        var base = window.location.href.split('/').slice(0, -1).join('/');
        var url = base + '/{upload_url_path}';

        var xhr = new XMLHttpRequest();
        xhr.open('POST', url, false);  // 同步
        try {{
            xhr.send(formData);
            if (xhr.status === 200) {{
                return xhr.responseText;
            }} else {{
                return 'xhr_fail:status_' + xhr.status;
            }}
        }} catch(e) {{
            return 'xhr_fail:' + e.message;
        }}
    """)

    if isinstance(result, str) and result.startswith("xhr_fail:"):
        return {"success": False, "error": result}

    return {"success": True, "response": result}
```

### 8.3 上傳後關閉 modal

```python
def close_modal_after_upload(driver):
    """上傳完成後清理 bootbox modal，避免影響後續操作。"""
    driver.execute_script("""
        // 關閉 bootbox
        if (typeof bootbox !== 'undefined') {
            bootbox.hideAll();
        }
        // 移除 backdrop
        var backdrops = document.querySelectorAll('.modal-backdrop');
        backdrops.forEach(function(el) { el.remove(); });
        // 移除 body 的 modal-open class
        document.body.classList.remove('modal-open');
        document.body.style.overflow = '';
        document.body.style.paddingRight = '';
    """)
```

---

## 9. 資料擷取

### 9.1 表格資料擷取

```python
def extract_table_data(driver, table_selector="table"):
    """擷取 HTML 表格為 list of dicts。"""
    table = driver.find_element(By.CSS_SELECTOR, table_selector)

    # 取標題
    headers = []
    for th in table.find_elements(By.CSS_SELECTOR, "thead th, tr:first-child th"):
        headers.append(th.text.strip())

    # 取資料列
    rows = []
    for tr in table.find_elements(By.CSS_SELECTOR, "tbody tr"):
        cells = [td.text.strip() for td in tr.find_elements(By.TAG_NAME, "td")]
        if headers and len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))
        else:
            rows.append(cells)

    return rows
```

### 9.2 文字匹配找列（OLA 實戰）

```python
def find_row_by_text(driver, patterns: list[str], skip_patterns: list[str] = None):
    """
    在表格中用文字匹配找到目標列。
    OLA 的列沒有 data-json，只能靠 innerText 匹配。

    patterns: 所有都必須出現（AND）
    skip_patterns: 任一出現就跳過（如 "聲請者取消"）
    """
    skip_patterns = skip_patterns or []
    rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr, .list-group-item")

    for row in rows:
        text = row.text.strip()

        # 跳過排除項
        if any(sp in text for sp in skip_patterns):
            continue

        # 所有 pattern 都要匹配
        if all(p in text for p in patterns):
            return row

    return None
```

### 9.3 BeautifulSoup 解析（非 Selenium 場景）

```python
import requests
from bs4 import BeautifulSoup

def scrape_static_page(url, headers=None):
    """輕量爬取靜態頁面。"""
    default_headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    resp = requests.get(url, headers=headers or default_headers, timeout=30)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding  # 自動偵測編碼（中文網站常需要）

    soup = BeautifulSoup(resp.text, "html.parser")
    return soup
```

---

## 10. 重試、容錯與降級

### 10.1 通用重試裝飾器

```python
import functools

def retry(max_attempts=3, backoff_base=2, exceptions=(Exception,)):
    """指數退避重試裝飾器。"""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_err = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_err = e
                    if attempt < max_attempts:
                        wait = min(30, backoff_base ** attempt)
                        logger.warning("%s attempt %d/%d failed: %s (retry in %ds)",
                                       func.__name__, attempt, max_attempts, e, wait)
                        time.sleep(wait)
            raise last_err
        return wrapper
    return decorator
```

### 10.2 Driver 崩潰自動重啟

```python
def is_driver_alive(driver):
    """檢查 driver 是否還活著。"""
    try:
        _ = driver.current_url
        return True
    except Exception:
        return False

def ensure_driver(driver_holder: dict, create_fn):
    """確保 driver 可用，壞了就重建。"""
    if driver_holder.get("driver") and is_driver_alive(driver_holder["driver"]):
        return driver_holder["driver"]

    # 嘗試關閉舊的
    try:
        if driver_holder.get("driver"):
            driver_holder["driver"].quit()
    except Exception:
        pass

    driver_holder["driver"] = create_fn()
    return driver_holder["driver"]
```

### 10.3 多選擇器 Fallback

```python
def find_element_multi(driver, selectors: list[tuple], timeout=10):
    """
    嘗試多組選擇器，第一個成功的就回傳。
    selectors: [(By.ID, "btn1"), (By.CSS_SELECTOR, ".submit"), (By.XPATH, "//button[text()='送出']")]
    """
    last_err = None
    for by, value in selectors:
        try:
            el = WebDriverWait(driver, timeout).until(
                EC.presence_of_element_located((by, value))
            )
            return el
        except Exception as e:
            last_err = e
            continue

    raise last_err or RuntimeError(f"None of {len(selectors)} selectors found")
```

---

## 11. Session 與 Cookie 管理

### 11.1 Cookie 持久化

```python
import pickle

def save_cookies(driver, path="cookies.pkl"):
    with open(path, "wb") as f:
        pickle.dump(driver.get_cookies(), f)

def load_cookies(driver, path="cookies.pkl"):
    if not os.path.exists(path):
        return False
    with open(path, "rb") as f:
        cookies = pickle.load(f)
    for cookie in cookies:
        # 移除可能過期的欄位
        cookie.pop("expiry", None)
        cookie.pop("sameSite", None)
        try:
            driver.add_cookie(cookie)
        except Exception:
            pass
    return True
```

### 11.2 Session 驗證

```python
def is_session_valid(driver, check_url, success_indicator):
    """
    載入頁面檢查 session 是否仍有效。
    success_indicator: CSS selector，出現表示已登入。
    """
    try:
        driver.get(check_url)
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, success_indicator))
        )
        return True
    except Exception:
        return False
```

---

## 12. 建構模擬站

> **為什麼需要**：政府網站不提供測試環境，直接對正式站開發太危險。
> 建一個離線模擬站，用真實 HTML 快照驅動。

### 12.1 快照擷取

```python
def capture_page_snapshot(driver, output_dir, page_name):
    """擷取頁面完整快照（HTML + 截圖）。"""
    os.makedirs(output_dir, exist_ok=True)

    # HTML
    html_path = os.path.join(output_dir, f"{page_name}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(driver.page_source)

    # 截圖
    png_path = os.path.join(output_dir, f"{page_name}.png")
    driver.save_screenshot(png_path)

    # 元素清單（供後續 selector 參考）
    meta_path = os.path.join(output_dir, f"{page_name}_meta.json")
    meta = {
        "url": driver.current_url,
        "title": driver.title,
        "forms": [],
        "inputs": [],
    }
    for form in driver.find_elements(By.TAG_NAME, "form"):
        meta["forms"].append({
            "id": form.get_attribute("id"),
            "action": form.get_attribute("action"),
        })
    for inp in driver.find_elements(By.CSS_SELECTOR, "input, select, textarea"):
        meta["inputs"].append({
            "tag": inp.tag_name,
            "type": inp.get_attribute("type"),
            "id": inp.get_attribute("id"),
            "name": inp.get_attribute("name"),
        })

    import json
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    return {"html": html_path, "screenshot": png_path, "meta": meta_path}
```

### 12.2 模擬站架構

```
simulator/
├── index.html          # 首頁（導航到各 workflow）
├── server.py           # 本地 HTTP server（處理 POST/上傳）
├── snapshots/          # 從真實站擷取的 HTML
│   ├── login.html
│   ├── list_view.html
│   └── detail_form.html
├── _qa/                # 自動化測試截圖
└── mock_api/           # 模擬 AJAX 回應
    └── responses.json
```

```python
# server.py 範本
from http.server import HTTPServer, SimpleHTTPRequestHandler
import json

class MockHandler(SimpleHTTPRequestHandler):
    def do_POST(self):
        """攔截 POST 請求，回傳模擬回應。"""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # 模擬上傳成功
        if "upload" in self.path.lower():
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "SUCCESS", "data": "OK"
            }).encode())
            return

        # 其他 POST → 200 OK
        self.send_response(200)
        self.end_headers()

if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8080), MockHandler).serve_forever()
```

---

## 13. 除錯工具箱

### 13.1 失敗時自動截圖 + HTML

```python
def on_failure_snapshot(driver, name="error"):
    """操作失敗時自動存證。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    try:
        driver.save_screenshot(f"/tmp/{name}_{ts}.png")
    except Exception:
        pass
    try:
        with open(f"/tmp/{name}_{ts}.html", "w", encoding="utf-8") as f:
            f.write(driver.page_source)
    except Exception:
        pass
```

### 13.2 目前在哪個 frame？

```python
def get_current_frame_info(driver):
    """回傳目前所在 frame 的資訊。"""
    try:
        url = driver.execute_script("return window.location.href")
        name = driver.execute_script("return window.name || ''")
        frame_el = driver.execute_script("return window.frameElement ? window.frameElement.id : 'TOP'")
        return {"url": url, "name": name, "frame_element_id": frame_el}
    except Exception as e:
        return {"error": str(e)}
```

### 13.3 列出所有可互動元素

```python
def list_interactive_elements(driver):
    """列出當前 frame 內所有可互動元素。"""
    return driver.execute_script("""
        var els = document.querySelectorAll(
            'a, button, input, select, textarea, [onclick], [role="button"]'
        );
        var result = [];
        els.forEach(function(el) {
            var rect = el.getBoundingClientRect();
            result.push({
                tag: el.tagName,
                type: el.type || '',
                id: el.id || '',
                name: el.name || '',
                text: (el.innerText || '').substring(0, 50),
                visible: rect.width > 0 && rect.height > 0,
                onclick: (el.getAttribute('onclick') || '').substring(0, 80),
            });
        });
        return result;
    """)
```

---

## 14. 安全守則

### 14.1 絕對不可

- **不得直接送出**正式表單（「送出 / Submit / doFinalSave」），一律只暫存
- **不得刪除**線上資料或取消他人申請
- **不得存取**非目標網站（URL whitelist）
- **不得硬編碼**帳密在程式碼中（用環境變數或 config.json）

### 14.2 必須做

- 每次重要操作前**截圖存證**
- 操作完成後**通知管理員**（LINE/Discord/TG）
- 用 **registry JSON** 防止重複操作（dedup）
- 敏感操作設 **human-in-the-loop** 確認點

### 14.3 去重 Registry 範本

```python
import json

REGISTRY_PATH = "/path/to/registry.json"

def load_registry():
    if os.path.exists(REGISTRY_PATH):
        with open(REGISTRY_PATH, "r") as f:
            return json.load(f)
    return {}

def is_already_processed(key: str) -> bool:
    reg = load_registry()
    return key in reg

def mark_processed(key: str, metadata: dict = None):
    reg = load_registry()
    reg[key] = {
        "processed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **(metadata or {}),
    }
    with open(REGISTRY_PATH, "w") as f:
        json.dump(reg, f, ensure_ascii=False, indent=2)
```

---

## 15. Checklist

新網站自動化啟動時，依序完成以下項目：

```
□ 偵察
  □ 手動操作一遍，錄製每步 URL 變化
  □ 確認是否有 iframe / frameset
  □ 確認登入機制（帳密? SSO? 驗證碼類型?）
  □ 確認表單 submit 方式（form POST? AJAX? WebSocket?）
  □ 擷取關鍵頁面 HTML 快照
  □ 記錄所有需要的 CSS selector / XPath

□ 建模擬站
  □ 用快照 HTML 建離線測試站
  □ 實作 mock POST handler
  □ 自動化腳本先對模擬站開發、測試

□ 實作
  □ 登入模組（含驗證碼 + 重試）
  □ 導航模組（frame 切換 + 等待策略）
  □ 表單填寫模組
  □ 上傳模組（hidden form XHR 優先）
  □ 資料擷取模組

□ 安全
  □ 去重 registry
  □ 截圖存證
  □ 管理員通知
  □ human-in-the-loop 確認點
  □ 不自動送出（只暫存）

□ 維護
  □ SKILL.md 記錄完整 selector map
  □ 失敗自動截圖 + HTML dump
  □ 定期驗證 selector 是否因網站改版失效
```

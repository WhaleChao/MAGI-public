"""
skills/engine/playwright_wrapper.py

共用 Playwright-Selenium 相容層，供法扶、閱卷、筆錄三模組使用。

設計原則：
- 讓三模組的 Selenium 程式碼無需大幅重寫，只換底層驅動器
- Playwright Chromium 自帶，不依賴系統 Chrome / chromedriver 版本
- fallback env var 讓每個模組獨立回退 Selenium
"""

from __future__ import annotations

import logging
import os
import re as _re
import threading
from typing import List, Optional, Any

_logger = logging.getLogger(__name__)

# ==============================================================================
# Selenium 相容 shim（供三模組在 Playwright 模式下仍能用 By/Keys/Select/EC）
# ==============================================================================

try:
    from selenium.webdriver.common.by import By
    from selenium.webdriver.common.keys import Keys
    from selenium.webdriver.support.ui import WebDriverWait, Select
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.common.exceptions import (
        TimeoutException,
        NoSuchElementException,
        ElementClickInterceptedException,
        StaleElementReferenceException,
        NoSuchFrameException,
    )
    _SELENIUM_SHIMS_AVAILABLE = True
except Exception:
    # 如果 Selenium 未安裝，提供最小 shim 讓 module-level import 不炸
    class _ByShim:
        ID = "id"
        NAME = "name"
        XPATH = "xpath"
        CSS_SELECTOR = "css selector"
        CLASS_NAME = "class name"
        TAG_NAME = "tag name"
        LINK_TEXT = "link text"
        PARTIAL_LINK_TEXT = "partial link text"

    By = _ByShim()

    class Keys:
        ENTER = "\n"
        TAB = "\t"
        BACKSPACE = "\x08"
        DELETE = "\x7f"
        ESCAPE = "\x1b"

    class Select:
        def __init__(self, el): self._el = el
        def select_by_visible_text(self, text): self._el.select_option(label=text)
        def select_by_value(self, value): self._el.select_option(value=value)

    WebDriverWait = None
    EC = None
    ActionChains = None
    TimeoutException = Exception
    NoSuchElementException = Exception
    ElementClickInterceptedException = Exception
    StaleElementReferenceException = Exception
    NoSuchFrameException = Exception
    _SELENIUM_SHIMS_AVAILABLE = False


# ==============================================================================
# 內部工具函式
# ==============================================================================

def _convert_script_for_playwright(script: str, args: list):
    """
    Convert Selenium execute_script(script, *args) to Playwright evaluate format.
    Handles the `arguments[N]` convention from Selenium.
    Returns (fn_str, fn_arg) for page.evaluate(fn_str, fn_arg).

    Uses reserved-looking parameter names (`__pw_a0`, `__pw_a1`, ...) that are
    unlikely to collide with script-internal identifiers like `el`.
    """
    if not args:
        return "() => { %s }" % script, None

    refs_found = sorted(set(int(m) for m in _re.findall(r'arguments\[(\d+)\]', script)))
    if not refs_found:
        return "() => { %s }" % script, None

    fn_body = script
    param_names = ["__pw_a%d" % i for i in refs_found]
    for i, pn in zip(refs_found, param_names):
        fn_body = fn_body.replace("arguments[%d]" % i, pn)

    if len(args) == 1 and refs_found == [0]:
        return "(%s) => { %s }" % (param_names[0], fn_body), args[0]

    param_list = ", ".join(param_names)
    return "([%s]) => { %s }" % (param_list, fn_body), list(args)


# ==============================================================================
# Playwright alert shim
# ==============================================================================

class _PlaywrightAlert(object):
    def __init__(self, dialog, wrapper):
        self._d = dialog
        self._w = wrapper

    @property
    def text(self):
        return self._d.message

    def accept(self):
        try:
            self._d.accept()
        except Exception:
            pass
        self._w._last_dialog = None

    def dismiss(self):
        try:
            self._d.dismiss()
        except Exception:
            pass
        self._w._last_dialog = None


# ==============================================================================
# Playwright switch_to shim
# ==============================================================================

class _PlaywrightSwitchTo(object):
    """Mimics Selenium driver.switch_to for PlaywrightDriverWrapper."""

    def __init__(self, wrapper):
        self._w = wrapper

    def default_content(self):
        self._w._active_frame = None

    def frame(self, frame_ref):
        page = self._w._page
        if isinstance(frame_ref, str):
            f = page.frame(name=frame_ref)
            self._w._active_frame = f
        elif isinstance(frame_ref, int):
            frames = page.frames
            if frame_ref < len(frames):
                self._w._active_frame = frames[frame_ref]
        elif hasattr(frame_ref, "_el"):
            try:
                f = frame_ref._el.content_frame()
                self._w._active_frame = f
            except Exception:
                self._w._active_frame = None
        else:
            self._w._active_frame = None

    def window(self, handle: str):
        """Switch active page by handle (str(id(page)))."""
        try:
            for p in self._w._all_pages():
                if str(id(p)) == str(handle):
                    self._w._page = p
                    self._w.switch_to = _PlaywrightSwitchTo(self._w)
                    self._w._active_frame = None
                    return
        except Exception:
            pass

    @property
    def alert(self):
        dlg = self._w._last_dialog
        if dlg is None:
            raise RuntimeError("NoAlertPresentException: no pending dialog")
        return _PlaywrightAlert(dlg, self._w)


# ==============================================================================
# ActionChains shim（Playwright 版）
# ==============================================================================

class PlaywrightActionChains(object):
    """
    Minimal Selenium ActionChains-compatible shim for PlaywrightDriverWrapper.
    支援三模組用到的 move_to_element / click / perform 組合。
    """

    def __init__(self, driver: "PlaywrightDriverWrapper"):
        self._driver = driver
        self._actions: list = []

    def move_to_element(self, element: "PlaywrightElementWrapper"):
        self._actions.append(("hover", element))
        return self

    def click(self, element: Optional["PlaywrightElementWrapper"] = None):
        if element is not None:
            self._actions.append(("click", element))
        else:
            self._actions.append(("click_last", None))
        return self

    def send_keys(self, text: str):
        self._actions.append(("send_keys", text))
        return self

    def perform(self):
        last_hovered = None
        for action, arg in self._actions:
            try:
                if action == "hover" and isinstance(arg, PlaywrightElementWrapper):
                    arg._el.hover()
                    last_hovered = arg
                elif action == "click" and isinstance(arg, PlaywrightElementWrapper):
                    arg._el.click()
                    last_hovered = arg
                elif action == "click_last":
                    if last_hovered is not None:
                        last_hovered._el.click()
                elif action == "send_keys":
                    page = self._driver._page
                    page.keyboard.type(str(arg))
            except Exception as e:
                _logger.debug("PlaywrightActionChains.perform error: %s", e)
        self._actions.clear()


# ==============================================================================
# Select shim（Playwright 版）
# ==============================================================================

class PlaywrightSelect(object):
    """
    Selenium Select-compatible shim for PlaywrightElementWrapper.
    用法：PlaywrightSelect(element).select_by_visible_text("股別")
    """

    def __init__(self, element: "PlaywrightElementWrapper"):
        self._el = element

    def select_by_visible_text(self, text: str):
        try:
            self._el._el.select_option(label=text)
        except Exception as e:
            _logger.debug("PlaywrightSelect.select_by_visible_text error: %s", e)

    def select_by_value(self, value: str):
        try:
            self._el._el.select_option(value=value)
        except Exception as e:
            _logger.debug("PlaywrightSelect.select_by_value error: %s", e)

    def select_by_index(self, index: int):
        try:
            options = self._el._el.query_selector_all("option")
            if index < len(options):
                val = options[index].get_attribute("value")
                if val is not None:
                    self._el._el.select_option(value=val)
        except Exception as e:
            _logger.debug("PlaywrightSelect.select_by_index error: %s", e)

    @property
    def options(self):
        try:
            handles = self._el._el.query_selector_all("option")
            return [PlaywrightElementWrapper(h, self._el._driver) for h in handles]
        except Exception:
            return []

    @property
    def first_selected_option(self):
        try:
            handle = self._el._el.query_selector("option:checked")
            if handle:
                return PlaywrightElementWrapper(handle, self._el._driver)
        except Exception:
            pass
        return None


# ==============================================================================
# WebDriverWait shim（Playwright 版）
# ==============================================================================

class PlaywrightWebDriverWait(object):
    """
    Minimal Selenium WebDriverWait shim for PlaywrightDriverWrapper.
    三模組只用 until(EC.presence_of_element_located) 與
    until(EC.element_to_be_clickable) 這兩種模式。
    """

    def __init__(self, driver: "PlaywrightDriverWrapper", timeout: float):
        self._driver = driver
        self._timeout = timeout

    def until(self, condition, message: str = ""):
        import time as _time
        end = _time.monotonic() + self._timeout
        last_exc = None
        while _time.monotonic() < end:
            try:
                result = condition(self._driver)
                if result is not None and result is not False:
                    return result
            except Exception as e:
                last_exc = e
            _time.sleep(0.2)
        exc_type = TimeoutException if TimeoutException is not Exception else RuntimeError
        raise exc_type(message or "Condition not met within %.1fs" % self._timeout)

    def until_not(self, condition, message: str = ""):
        import time as _time
        end = _time.monotonic() + self._timeout
        while _time.monotonic() < end:
            try:
                result = condition(self._driver)
                if result is None or result is False:
                    return True
            except Exception:
                return True
            _time.sleep(0.2)
        exc_type = TimeoutException if TimeoutException is not Exception else RuntimeError
        raise exc_type(message or "Condition did not become false within %.1fs" % self._timeout)


# ==============================================================================
# PlaywrightElementWrapper
# ==============================================================================

class PlaywrightElementWrapper(object):
    """Wraps playwright ElementHandle to behave like a Selenium WebElement."""

    def __init__(self, el, driver_wrapper: "PlaywrightDriverWrapper"):
        self._el = el
        self._driver = driver_wrapper

    def click(self):
        try:
            self._el.scroll_into_view_if_needed()
            self._el.click(timeout=5000)
        except Exception:
            try:
                self._el.evaluate("el => el.click()")
            except Exception:
                pass

    def send_keys(self, text: Any):
        t = str(text)
        try:
            tag = self._el.evaluate("el => el.tagName.toLowerCase()")
            inp_type = self._el.evaluate("el => (el.getAttribute('type') || '').toLowerCase()")
            if tag == "input" and inp_type == "file":
                self._el.set_input_files(t)
                return
        except Exception:
            pass
        if t == "\n":
            self._el.press("Enter")
        elif "\n" in t:
            parts = t.split("\n")
            for i, part in enumerate(parts):
                if part:
                    self._el.type(part)
                if i < len(parts) - 1:
                    self._el.press("Enter")
        else:
            self._el.type(t)

    def clear(self):
        try:
            self._el.fill("")
        except Exception:
            try:
                self._el.evaluate(
                    "el => { el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); }"
                )
            except Exception:
                pass

    @property
    def text(self):
        try:
            return self._el.text_content() or ""
        except Exception:
            return ""

    @property
    def tag_name(self):
        try:
            return self._el.evaluate("el => el.tagName.toLowerCase()")
        except Exception:
            return ""

    def get_attribute(self, name: str):
        try:
            if name.lower() == "innerhtml":
                return self._el.inner_html()
            if name.lower() == "innertext":
                return self._el.inner_text()
            return self._el.get_attribute(name)
        except Exception:
            return None

    def get_dom_attribute(self, name: str):
        """Selenium 4 API: get DOM property (not HTML attribute)."""
        try:
            return self._el.evaluate(f"el => el['{name}']")
        except Exception:
            return None

    def value_of_css_property(self, property_name: str) -> str:
        """Selenium API: get computed CSS property value.

        For the three properties Selenium's Select checks (visibility / display /
        opacity) we return sensible visible-defaults without running JavaScript,
        saving ~150 JS evaluations per <select> interaction.  For all other
        properties we fall through to getComputedStyle.
        """
        _visibility_defaults = {
            "visibility": "visible",
            "display": "block",
            "opacity": "1",
        }
        if property_name in _visibility_defaults:
            return _visibility_defaults[property_name]
        try:
            return self._el.evaluate(
                f"el => window.getComputedStyle(el).getPropertyValue('{property_name}')"
            ) or ""
        except Exception:
            return ""

    def is_displayed(self):
        try:
            return self._el.is_visible()
        except Exception:
            return False

    def is_enabled(self):
        try:
            return not self._el.is_disabled()
        except Exception:
            return True

    def is_selected(self):
        try:
            return self._el.is_checked()
        except Exception:
            return False

    @property
    def screenshot_as_png(self):
        try:
            return self._el.screenshot()
        except Exception:
            return None

    @property
    def location(self):
        try:
            bb = self._el.bounding_box()
            if bb:
                return {"x": bb["x"], "y": bb["y"]}
        except Exception:
            pass
        return {"x": 0, "y": 0}

    @property
    def size(self):
        try:
            bb = self._el.bounding_box()
            if bb:
                return {"width": bb["width"], "height": bb["height"]}
        except Exception:
            pass
        return {"width": 0, "height": 0}

    def find_element(self, by, value: str) -> "PlaywrightElementWrapper":
        """Find a single child element (Selenium API compat)."""
        selector = _by_to_selector(by, value)
        try:
            handle = self._el.query_selector(selector)
        except Exception as e:
            raise (NoSuchElementException or Exception)(f"find_element failed: {e}") from e
        if handle is None:
            raise (NoSuchElementException or Exception)(f"No element found: {by}={value}")
        return PlaywrightElementWrapper(handle, self._driver)

    def find_elements(self, by, value: str) -> List["PlaywrightElementWrapper"]:
        """Find all child elements matching selector (Selenium API compat)."""
        selector = _by_to_selector(by, value)
        try:
            handles = self._el.query_selector_all(selector)
            return [PlaywrightElementWrapper(h, self._driver) for h in (handles or [])]
        except Exception:
            return []

    def submit(self):
        try:
            self._el.evaluate("el => el.closest('form') && el.closest('form').submit()")
        except Exception:
            pass

    def screenshot(self, filename: Optional[str] = None) -> Optional[bytes]:
        try:
            if filename:
                return self._el.screenshot(path=filename)
            return self._el.screenshot()
        except Exception:
            return None


# ==============================================================================
# PlaywrightDriverWrapper
# ==============================================================================

class PlaywrightDriverWrapper(object):
    """
    Thin Selenium-compatible wrapper around playwright.sync_api.Page.

    Lets three MAGI modules (法扶/閱卷/筆錄) use Playwright as a drop-in for
    Selenium without rewriting the core portal automation logic.
    """

    def __init__(self, page, context, pw_instance, download_dir: Optional[str] = None):
        self._page = page
        self._context = context
        self._pw = pw_instance
        self._active_frame = None
        self._last_dialog = None
        self._download_dir = download_dir
        self.switch_to = _PlaywrightSwitchTo(self)

        def _on_dialog(dialog):
            # Store the dialog for switch_to.alert compatibility.
            self._last_dialog = dialog
            # Dismiss immediately to avoid blocking the JavaScript execution context.
            # Background threads cannot call Playwright sync API (greenlet cross-thread
            # switch is illegal), so we MUST dismiss within this callback.
            try:
                dialog.dismiss()
            except Exception:
                pass

        page.on("dialog", _on_dialog)

        # ★ Context-level download interceptor — catches downloads from ALL pages/popups.
        # CDP Browser.setDownloadBehavior only applies to its own CDP session (the main
        # page). When a popup triggers a download, Chrome uses the system default folder
        # (~/ Downloads). We wire up Playwright's own context download event so that ALL
        # downloads — including from popups — are saved to self._download_dir.
        def _on_download(download):
            try:
                fname = download.suggested_filename or "download"
                target_dir = self._download_dir or "/tmp"
                os.makedirs(target_dir, exist_ok=True)
                target = os.path.join(target_dir, fname)
                # Avoid overwriting an identical existing file
                if os.path.exists(target):
                    base, ext = os.path.splitext(fname)
                    import time as _time
                    target = os.path.join(target_dir, f"{base}_{int(_time.time())}{ext}")
                download.save_as(target)
                _logger.info("✅ 下載截獲 (context): %s → %s", fname, target)
            except Exception as _de:
                _logger.warning("下載截獲失敗: %s", _de)

        try:
            context.on("download", _on_download)
        except Exception:
            pass

        # Capture popups / new tabs so window_handles stays consistent.
        # Playwright fires 'popup' on the ORIGINATING page, and 'page' on context.
        # We cache them here so polling window_handles sees them immediately.
        self._popup_pages: list = []

        def _on_popup(popup_page):
            if popup_page not in self._popup_pages:
                self._popup_pages.append(popup_page)
                popup_page.on("dialog", _on_dialog)
                # Also wire download interceptor on each popup page (belt-and-suspenders)
                try:
                    popup_page.on("download", _on_download)
                except Exception:
                    pass

        page.on("popup", _on_popup)
        try:
            context.on("page", _on_popup)
        except Exception:
            pass

    # ---- internal helpers ----

    def set_download_dir(self, path: str) -> None:
        """Update the download directory for the context-level download interceptor.

        Call this after creating a date-based subfolder so all subsequent downloads
        (including from popups) land in the new path.
        """
        self._download_dir = path
        os.makedirs(path, exist_ok=True)

    def _active(self):
        return self._active_frame if self._active_frame else self._page

    @staticmethod
    def _to_selector(by, value: str) -> str:
        return _by_to_selector(by, value)

    # ---- WebDriver API ----

    def get(self, url: str):
        self._active_frame = None
        try:
            self._page.goto(url, wait_until="load", timeout=30000)
        except Exception:
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass

    @property
    def current_url(self) -> str:
        try:
            return self._page.url
        except Exception:
            return ""

    @property
    def page_source(self) -> str:
        try:
            return self._active().content()
        except Exception:
            try:
                return self._page.content()
            except Exception:
                return ""

    @property
    def title(self) -> str:
        try:
            return self._page.title()
        except Exception:
            return ""

    def execute_script(self, script: str, *args):
        """Selenium-compatible JS execution."""
        processed = [a._el if isinstance(a, PlaywrightElementWrapper) else a for a in args]
        fn_str, fn_arg = _convert_script_for_playwright(script, processed)
        active = self._active()
        try:
            if fn_arg is None:
                return active.evaluate(fn_str)
            else:
                return active.evaluate(fn_str, fn_arg)
        except Exception as e:
            _logger.debug("PW execute_script err: %s | script=%s", e, script[:80])
            return None

    def execute_async_script(self, script: str, *args):
        """
        Selenium execute_async_script → Playwright evaluate (Promise).
        Selenium pattern: last argument is callback; script calls arguments[N] when done.
        """
        processed = [a._el if isinstance(a, PlaywrightElementWrapper) else a for a in args]
        wrapper = (
            "(argArray) => new Promise(function(resolve, reject) {"
            " var allArgs = Array.from(argArray || []);"
            " allArgs.push(resolve);"
            " (function() { " + script + " }).apply(null, allArgs);"
            "})"
        )
        active = self._active()
        try:
            return active.evaluate(wrapper, processed)
        except Exception as e:
            _logger.debug("PW execute_async_script err: %s", e)
            return None

    def find_element(self, by, value: str) -> PlaywrightElementWrapper:
        selector = _by_to_selector(by, value)
        active = self._active()
        try:
            el = active.query_selector(selector)
        except Exception:
            el = None
        if el is None:
            raise (NoSuchElementException or Exception)("No element: %s" % selector)
        return PlaywrightElementWrapper(el, self)

    def find_elements(self, by, value: str) -> List[PlaywrightElementWrapper]:
        selector = _by_to_selector(by, value)
        active = self._active()
        try:
            els = active.query_selector_all(selector)
            return [PlaywrightElementWrapper(el, self) for el in els]
        except Exception:
            return []

    def save_screenshot(self, path: str) -> bool:
        try:
            self._page.screenshot(path=str(path), full_page=False)
            return True
        except Exception:
            return False

    def get_screenshot_as_png(self) -> bytes:
        try:
            return self._page.screenshot()
        except Exception:
            return b""

    def get_screenshot_as_base64(self) -> str:
        import base64
        return base64.b64encode(self.get_screenshot_as_png()).decode()

    def implicitly_wait(self, timeout: float):
        # Playwright uses per-action timeouts; store as hint
        self._implicit_timeout = int(timeout * 1000)

    def set_page_load_timeout(self, timeout: float):
        try:
            self._page.set_default_navigation_timeout(int(timeout * 1000))
        except Exception:
            pass

    def set_script_timeout(self, timeout: float):
        try:
            self._page.set_default_timeout(int(timeout * 1000))
        except Exception:
            pass

    def maximize_window(self):
        try:
            self._page.set_viewport_size({"width": 1920, "height": 1080})
        except Exception:
            pass

    def refresh(self):
        try:
            self._page.reload()
        except Exception:
            pass

    def back(self):
        try:
            self._page.go_back()
        except Exception:
            pass

    # ---- multi-window support (tabs) ----

    def _all_pages(self) -> list:
        """All known pages: context.pages union popup_pages."""
        seen_ids: set = set()
        pages: list = []
        try:
            for p in self._context.pages:
                if id(p) not in seen_ids:
                    seen_ids.add(id(p))
                    pages.append(p)
        except Exception:
            pass
        for p in getattr(self, "_popup_pages", []):
            if id(p) not in seen_ids:
                seen_ids.add(id(p))
                pages.append(p)
        if not pages:
            pages = [self._page]
        return pages

    @property
    def window_handles(self) -> list:
        """Selenium-compat: return handles for all open pages (tabs/windows)."""
        return [str(id(p)) for p in self._all_pages()]

    @property
    def current_window_handle(self) -> str:
        return str(id(self._page))

    def close(self):
        """Close only the current page/tab — does NOT close the browser context.
        Matches Selenium driver.close() semantics for multi-tab workflows."""
        try:
            self._page.close()
        except Exception:
            pass

    def quit(self):
        """Close the current page, the browser context, and stop Playwright."""
        self.close()
        try:
            self._context.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass

    # ---- Playwright-specific helpers ----

    def wait_for_selector(self, selector: str, timeout: float = 10.0,
                           state: str = "visible") -> Optional[PlaywrightElementWrapper]:
        try:
            el = self._page.wait_for_selector(selector, timeout=int(timeout * 1000), state=state)
            if el:
                return PlaywrightElementWrapper(el, self)
        except Exception:
            pass
        return None

    def wait_for_url(self, pattern: str, timeout: float = 15.0):
        try:
            self._page.wait_for_url(pattern, timeout=int(timeout * 1000))
        except Exception:
            pass

    def frame_locator(self, selector: str):
        """Return Playwright frame_locator for iframe-heavy pages."""
        return self._page.frame_locator(selector)

    def expect_download(self, action_fn, timeout: float = 30.0):
        """Context manager that waits for a file download triggered by action_fn."""
        with self._page.expect_download(timeout=int(timeout * 1000)) as download_info:
            action_fn()
        return download_info.value

    def click_link_and_wait_for_popup(self, element, timeout_ms: int = 10000,
                                       load_state: str = "domcontentloaded",
                                       load_timeout_ms: int = 20000):
        """
        Playwright-native: click element and wait for popup/new-tab to appear.
        Returns the new page's handle string (str(id(page))), or None if no popup.

        Unlike polling window_handles, this uses page.expect_popup() which
        reliably captures the popup event before returning — critical for
        portals that use window.open() or target=_blank link clicks.

        After capture, waits for the popup to reach load_state so callers
        can immediately interact with the new page's DOM.
        """
        try:
            pw_el = element._el if isinstance(element, PlaywrightElementWrapper) else element
            with self._page.expect_popup(timeout=timeout_ms) as popup_info:
                try:
                    pw_el.scroll_into_view_if_needed()
                except Exception:
                    pass
                pw_el.click()
            new_page = popup_info.value
            # Wait for the popup to be interactable before returning
            try:
                new_page.wait_for_load_state(load_state, timeout=load_timeout_ms)
            except Exception:
                pass
            if new_page not in self._popup_pages:
                self._popup_pages.append(new_page)
            return str(id(new_page))
        except Exception as e:
            _logger.debug("click_link_and_wait_for_popup: %s", e)
            return None

    def add_init_script(self, script: str):
        """Run script on every new page/frame for stealth patching."""
        try:
            self._context.add_init_script(script)
        except Exception:
            pass


# ==============================================================================
# Helper: by → CSS selector
# ==============================================================================

def _by_to_selector(by, value: str) -> str:
    by_s = str(by).lower()
    if "css" in by_s:
        return value
    if by_s == "id":
        return "#%s" % value
    if "xpath" in by_s:
        return "xpath=%s" % value
    if "tag" in by_s:           # "tag name" must be before generic "name" check
        return value
    if by_s == "name":
        return '[name="%s"]' % value
    if "class" in by_s:
        return ".%s" % value
    if "link" in by_s:
        return 'a:has-text("%s")' % value
    # fallback: treat as attribute name selector
    if "name" in by_s:
        return '[name="%s"]' % value
    return value


# ==============================================================================
# Factory function
# ==============================================================================

def create_playwright_driver(
    headless: bool = True,
    download_dir: Optional[str] = None,
    page_load_timeout: float = 60.0,
    profile_dir: Optional[str] = None,
    stealth: bool = True,
) -> PlaywrightDriverWrapper:
    """
    建立 Playwright Chromium 驅動器並回傳 PlaywrightDriverWrapper。

    Args:
        headless: True = headless 模式
        download_dir: 下載目錄（None = /tmp）
        page_load_timeout: 頁面載入 timeout（秒）
        profile_dir: Chrome profile 目錄（None = 暫時）
        stealth: 是否注入 stealth script 避免自動化偵測

    Returns:
        PlaywrightDriverWrapper

    Raises:
        RuntimeError: Playwright 未安裝或初始化失敗
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError("Playwright 未安裝，請執行 playwright install chromium") from e

    dl_path = download_dir or "/tmp"
    os.makedirs(dl_path, exist_ok=True)

    pw = sync_playwright().start()
    try:
        launch_args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ]

        browser = pw.chromium.launch(
            headless=headless,
            args=launch_args,
        )

        ctx_kwargs: dict = {
            "accept_downloads": True,
            "viewport": {"width": 1920, "height": 1080},
            "ignore_https_errors": True,
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        }

        context = browser.new_context(**ctx_kwargs)

        if stealth:
            try:
                context.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
                )
            except Exception:
                pass

        page = context.new_page()
        page.set_default_navigation_timeout(int(page_load_timeout * 1000))
        page.set_default_timeout(int(page_load_timeout * 1000))

        # 設定下載路徑（Browser-level CDP）
        try:
            client = context.new_cdp_session(page)
            client.send("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": str(dl_path),
                "eventsEnabled": True,
            })
        except Exception as cdp_err:
            _logger.debug("Playwright CDP download path 設定失敗（通常不影響功能）: %s", cdp_err)

        _logger.info("✅ Playwright Chromium 初始化成功（download_dir=%s）", dl_path)
        return PlaywrightDriverWrapper(page, context, pw, download_dir=dl_path)

    except Exception:
        try:
            pw.stop()
        except Exception:
            pass
        raise

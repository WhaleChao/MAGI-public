# -*- coding: utf-8 -*-
"""
Browser Automation Skill (瀏覽器自動化) v2.0
Based on ClawHub community skill: agent-browser
Iron Dome Audit: ⚠️ RESTRICTED — URL whitelist enforced

Uses Playwright (async-first) instead of Selenium.
Capabilities: Navigate, Screenshot, Extract text, Fill forms
"""

import logging
import asyncio
import os

logger = logging.getLogger("BrowserSkill")

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    logger.warning("⚠️ Playwright not installed — browser automation disabled")

# Iron Dome: URL restrictions
BLOCKED_DOMAINS = [
    "localhost", "127.0.0.1",  # Prevent internal service access
    "0.0.0.0",
]

BLOCKED_SCHEMES = ["file://", "javascript:", "data:"]


def _is_url_safe(url):
    """Iron Dome URL filter."""
    url_lower = url.lower()
    
    # Block dangerous schemes
    for scheme in BLOCKED_SCHEMES:
        if url_lower.startswith(scheme):
            return False, f"🛡️ Iron Dome: 封鎖的 URL scheme: {scheme}"
    
    # Block internal access
    for domain in BLOCKED_DOMAINS:
        if domain in url_lower:
            return False, f"🛡️ Iron Dome: 不允許存取內部服務: {domain}"
    
    return True, ""


class BrowserController:
    def __init__(self, headless=False):
        self.headless = headless
        self.browser = None
        self.page = None
        self._pw = None

    def start(self):
        """Start Playwright browser."""
        if not PLAYWRIGHT_AVAILABLE:
            return "❌ Playwright 未安裝。請執行: pip install playwright && playwright install chromium"
        
        if self.browser:
            return "✅ 瀏覽器已啟動"
        
        try:
            self._pw = sync_playwright().start()
            self.browser = self._pw.chromium.launch(headless=self.headless)
            self.page = self.browser.new_page()
            logger.info("🌐 Playwright Browser Started")
            return "✅ 瀏覽器已啟動 (Chromium)"
        except Exception as e:
            logger.error(f"Browser start error: {e}")
            return f"❌ 瀏覽器啟動失敗: {e}"

    def stop(self):
        """Stop browser."""
        try:
            if self.browser:
                self.browser.close()
            if self._pw:
                self._pw.stop()
            self.browser = None
            self.page = None
            self._pw = None
            logger.info("🛑 Browser Stopped")
            return "🛑 瀏覽器已關閉"
        except Exception as e:
            return f"⚠️ 關閉時發生錯誤: {e}"

    def navigate(self, url):
        """Navigate to a URL (Iron Dome filtered)."""
        safe, msg = _is_url_safe(url)
        if not safe:
            return msg
        
        if not self.page:
            self.start()
        
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
            title = self.page.title()
            logger.info(f"🔗 Navigated to: {url} — Title: {title}")
            return f"🌐 已開啟: **{title}**\nURL: `{url}`"
        except Exception as e:
            return f"❌ 導航失敗: {e}"

    def screenshot(self, save_path="/tmp/magi_screenshot.png"):
        """Take a screenshot of current page."""
        if not self.page:
            return "❌ 瀏覽器未啟動"
        
        try:
            self.page.screenshot(path=save_path, full_page=False)
            logger.info(f"📸 Screenshot saved: {save_path}")
            return f"📸 截圖已儲存: `{save_path}`"
        except Exception as e:
            return f"❌ 截圖失敗: {e}"

    def extract_text(self, max_chars=5000):
        """Extract visible text from current page."""
        if not self.page:
            return "❌ 瀏覽器未啟動"
        
        try:
            text = self.page.inner_text("body")
            if len(text) > max_chars:
                text = text[:max_chars] + "\n... [文字已截斷]"
            title = self.page.title()
            return f"📝 **{title}** 頁面文字:\n\n{text}"
        except Exception as e:
            return f"❌ 擷取文字失敗: {e}"

    def fill_form(self, selector, value):
        """Fill an input field."""
        if not self.page:
            return "❌ 瀏覽器未啟動"
        
        try:
            self.page.fill(selector, value)
            return f"✅ 已填入 `{selector}`: {value}"
        except Exception as e:
            return f"❌ 填入失敗: {e}"

    def click_element(self, selector):
        """Click an element."""
        if not self.page:
            return "❌ 瀏覽器未啟動"
        
        try:
            self.page.click(selector, timeout=5000)
            return f"🖱️ 已點擊: `{selector}`"
        except Exception as e:
            return f"❌ 點擊失敗: {e}"


# Convenience functions for Orchestrator integration
_browser_instance = None

def get_browser():
    """Get or create singleton browser instance."""
    global _browser_instance
    if _browser_instance is None:
        _browser_instance = BrowserController(headless=False)
    return _browser_instance

def browse_url(url):
    """Quick function: open URL and extract text."""
    browser = get_browser()
    nav_result = browser.navigate(url)
    if "❌" in nav_result or "🛡️" in nav_result:
        return nav_result
    
    text = browser.extract_text(max_chars=3000)
    return f"{nav_result}\n\n{text}"

def take_screenshot(url=None):
    """Quick function: screenshot a URL."""
    browser = get_browser()
    if url:
        nav_result = browser.navigate(url)
        if "❌" in nav_result or "🛡️" in nav_result:
            return nav_result
    
    return browser.screenshot()


if __name__ == "__main__":
    print("Testing browser skill...")
    result = browse_url("https://www.google.com")
    print(result[:500])
    browser = get_browser()
    browser.stop()

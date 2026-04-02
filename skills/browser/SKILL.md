---
name: browser
description: Browser automation and web scraping capabilities. Use when needing to interact with web pages, fill forms, or extract dynamic content. See WEB_AUTOMATION_GUIDE.md for comprehensive patterns.
license: MIT
compatibility: Requires Playwright or Selenium
metadata:
  author: MAGI-Federation
  version: "2.0"
  sage: melchior
---

# Browser Skill

Web browser automation for dynamic content and page interactions.

## Capabilities

- **Page Navigation**: Load and navigate web pages (including multi-layer iframe/frameset)
- **Form Filling**: Automate form submission (native select, fake dropdown, radio/checkbox)
- **File Upload**: Hidden form + XHR approach (more reliable than dialog-based in headless)
- **Screenshot**: Capture web page screenshots
- **Content Extraction**: Extract text/tables from JavaScript-rendered pages
- **Anti-Detection**: Stealth driver configuration for protected sites
- **Captcha**: OCR-based solving (ddddocr/RapidOCR) with human-in-the-loop fallback

## Files

- `browser_control.py` - Playwright browser controller
- `WEB_AUTOMATION_GUIDE.md` - **實戰指南**：完整的網站自動化 / 爬蟲開發手冊，涵蓋偵察、反偵測、iframe、上傳、模擬站建構等所有模式

## Quick Reference

新網站自動化開發流程：**偵察 → 建模擬站 → 實作 → 安全檢查**。
詳見 [WEB_AUTOMATION_GUIDE.md](WEB_AUTOMATION_GUIDE.md) §15 Checklist。

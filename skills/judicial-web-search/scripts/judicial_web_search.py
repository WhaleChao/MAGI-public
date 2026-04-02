#!/usr/bin/env python3
import logging
import argparse
import json
import os
import re
import sys
from urllib.parse import urljoin


DEFAULT_CHROME = os.environ.get("JUDICIAL_CHROME_PATH", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").strip()
BASE = "https://judgment.judicial.gov.tw/FJUD/Default_AD.aspx"


def _ok(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("success") else 1


def _load_jsonish(text: str) -> dict:
    t = (text or "").strip()
    if not t:
        return {}
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        return {"value": t}


def _clean_text(s: str) -> str:
    if not s:
        return ""
    # collapse noise whitespace
    s = re.sub(r"\r\n?", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _launch(headless: bool):
    from playwright.sync_api import sync_playwright

    p = sync_playwright().start()
    browser = p.chromium.launch(
        headless=bool(headless),
        executable_path=DEFAULT_CHROME if os.path.exists(DEFAULT_CHROME) else None,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    return p, browser


def search(
    keywords: str,
    max_results: int = 10,
    headless: bool = True,
    timeout_sec: int = 60,
    courts: list[str] | None = None,
    case_year: str = "",
    case_word: str = "",
    case_no: str = "",
) -> dict:
    kw = (keywords or "").strip()
    has_structured_filters = bool((courts or []) or (case_year or "").strip() or (case_word or "").strip() or (case_no or "").strip())
    if (not kw) and (not has_structured_filters):
        return {"success": False, "error": "missing keywords_or_case_filters"}
    max_n = int(max_results) if int(max_results) > 0 else 10
    timeout_ms = int(timeout_sec) * 1000

    p = None
    browser = None
    try:
        p, browser = _launch(headless=headless)
        ctx = browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
        page = ctx.new_page()
        page.goto(BASE, wait_until="domcontentloaded", timeout=timeout_ms)

        # Search input is on the default page.
        page.locator("#jud_kw").wait_for(state="visible", timeout=timeout_ms)
        if courts:
            try:
                page.select_option("#jud_court", label=[c for c in courts if c])
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 81, exc_info=True)
        if (case_year or "").strip():
            try:
                page.locator("#jud_year").fill(str(case_year).strip())
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 86, exc_info=True)
        if (case_word or "").strip():
            try:
                page.locator("#jud_case").fill(str(case_word).strip())
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 91, exc_info=True)
        if (case_no or "").strip():
            try:
                page.locator("#jud_no").fill(str(case_no).strip())
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 96, exc_info=True)
        page.locator("#jud_kw").fill(kw if kw else "")
        page.locator("#btnQry").click()

        # Results are rendered inside iframe-data
        page.locator("#iframe-data").wait_for(timeout=timeout_ms)
        frame = page.frame_locator("#iframe-data")
        frame.locator("table#jud").wait_for(timeout=timeout_ms)

        links = frame.locator("table#jud a[href*='data.aspx']").all()
        items = []
        for a in links[:max_n]:
            try:
                href = a.get_attribute("href") or ""
                title = (a.text_content() or "").strip()
                if not href or not title:
                    continue
                url = urljoin("https://judgment.judicial.gov.tw/FJUD/", href)
                items.append({"title": title, "url": url})
            except Exception:
                continue

        return {
            "success": True,
            "keywords": kw,
            "structured_only": (not kw) and has_structured_filters,
            "count": len(items),
            "results": items,
        }
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:240]}"}
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 132, exc_info=True)
        try:
            if p:
                p.stop()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 137, exc_info=True)


def fetch_text(url: str, headless: bool = True, timeout_sec: int = 45, max_chars: int = 500000) -> dict:
    u = (url or "").strip()
    if not u:
        return {"success": False, "error": "missing url"}
    timeout_ms = int(timeout_sec) * 1000
    max_n = int(max_chars) if int(max_chars) > 0 else 500000

    p = None
    browser = None
    try:
        p, browser = _launch(headless=headless)
        ctx = browser.new_context(user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")
        page = ctx.new_page()
        page.goto(u, wait_until="domcontentloaded", timeout=timeout_ms)

        # Prefer div#jud when present.
        text = ""
        try:
            loc = page.locator("div#jud")
            if loc.count() > 0:
                text = loc.inner_text(timeout=timeout_ms) or ""
        except Exception:
            text = ""

        if not text:
            try:
                text = page.locator("body").inner_text(timeout=timeout_ms) or ""
            except Exception:
                text = ""

        text = _clean_text(text)
        if "查無資料" in text or "系統忙碌" in text:
            return {"success": False, "error": "site returned error page", "hint": text[:200]}
        return {"success": True, "url": u, "text": text[:max_n], "text_chars": min(len(text), max_n)}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:240]}"}
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 181, exc_info=True)
        try:
            if p:
                p.stop()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 186, exc_info=True)


def self_test() -> dict:
    smoke_max = int(os.environ.get("JUDICIAL_SEARCH_SMOKE_MAX_RESULTS", "8") or "8")
    # Try a few common keywords; pass if at least one yields results.
    for kw in ["詐欺", "侵權", "損害賠償"]:
        r = search(kw, max_results=smoke_max, headless=True, timeout_sec=60)
        if r.get("success") and (r.get("count", 0) or 0) > 0:
            # Also try fetching one judgment text (smoke)
            u = (r.get("results") or [{}])[0].get("url") or ""
            t = fetch_text(u, headless=True, timeout_sec=45, max_chars=5000) if u else {"success": False, "error": "missing url from search"}
            return {"success": bool(t.get("success")), "search": r, "fetch_text": t, "chrome_path": DEFAULT_CHROME}
    return {"success": False, "error": "no results for self_test keywords", "chrome_path": DEFAULT_CHROME}


def main() -> int:
    ap = argparse.ArgumentParser(description="judicial web search runner")
    ap.add_argument("--task", default="help")
    args = ap.parse_args()
    task = (args.task or "").strip()

    if task in {"help", "summary", "list"}:
        return _ok({"success": True, "commands": ["self_test", "search {..}", "fetch_text {..}"]})

    if task == "self_test":
        return _ok(self_test())

    if task.startswith("search"):
        payload = _load_jsonish(task[len("search") :].strip())
        return _ok(
            search(
                keywords=(payload.get("keywords") or payload.get("query") or payload.get("value") or "").strip(),
                max_results=int(payload.get("max_results", 10) or 10),
                headless=bool(payload.get("headless", True)),
                timeout_sec=int(payload.get("timeout_sec", 60) or 60),
                courts=(payload.get("courts") or payload.get("court") or None),
                case_year=str(payload.get("case_year") or payload.get("jud_year") or "").strip(),
                case_word=str(payload.get("case_word") or payload.get("jud_case") or "").strip(),
                case_no=str(payload.get("case_no") or payload.get("jud_no") or "").strip(),
            )
        )

    if task.startswith("fetch_text"):
        payload = _load_jsonish(task[len("fetch_text") :].strip())
        return _ok(
            fetch_text(
                url=(payload.get("url") or payload.get("value") or "").strip(),
                headless=bool(payload.get("headless", True)),
                timeout_sec=int(payload.get("timeout_sec", 45) or 45),
                max_chars=int(payload.get("max_chars", 500000) or 500000),
            )
        )

    return _ok({"success": False, "error": f"unknown task: {task}"})


if __name__ == "__main__":
    raise SystemExit(main())

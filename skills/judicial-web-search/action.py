#!/usr/bin/env python3
import logging
import argparse
import json
import os
import re
import subprocess
import sys
import hashlib
import html
from urllib.parse import urljoin
from bs4 import BeautifulSoup
import requests
import urllib3
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import sys as _sys
if _MAGI_ROOT not in _sys.path:
    _sys.path.insert(0, _MAGI_ROOT)
from skills.bridge.shared_utils.text_utils import clean_text as _clean_text


VENV_PY = os.environ.get("JUDICIAL_VENV_PY", f"{_MAGI_ROOT}/.venv_judicial/bin/python").strip()
CHROME_PATH = os.environ.get("JUDICIAL_CHROME_PATH", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").strip()
BASE = "https://judgment.judicial.gov.tw/FJUD/Default_AD.aspx"
CACHE_DIR = os.environ.get("JUDICIAL_CACHE_DIR", f"{_MAGI_ROOT}/.cache/judicial_web_search").strip()
urllib3.disable_warnings()


def _preview_limit() -> int:
    """
    Result preview count returned inline.
    Full results are always written to results_path.
    """
    try:
        n = int(os.environ.get("JUDICIAL_SEARCH_PREVIEW_LIMIT", "10") or "10")
    except Exception:
        n = 10
    return max(1, min(50, n))


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


def _run_venv(task: str, timeout_sec: int = 90) -> dict:
    if not os.path.exists(VENV_PY):
        return {"success": False, "error": f"missing venv python: {VENV_PY}"}
    try:
        r = subprocess.run(
            [VENV_PY, os.path.abspath(__file__), "--task", task],
            capture_output=True,
            text=True,
            timeout=int(timeout_sec),
            env={**os.environ, "JUDICIAL_USE_VENV": "1"},
        )
        out = (r.stdout or "").strip()
        if not out:
            return {"success": False, "error": "empty output", "stderr": (r.stderr or "")[-800:], "rc": r.returncode}
        try:
            obj = json.loads(out.splitlines()[-1])
        except Exception:
            return {"success": False, "error": "non-json output", "stdout": out[-800:], "stderr": (r.stderr or "")[-800:], "rc": r.returncode}
        if isinstance(obj, dict):
            obj.setdefault("rc", r.returncode)
            if r.stderr:
                obj.setdefault("stderr_tail", (r.stderr or "")[-800:])
        return obj if isinstance(obj, dict) else {"success": False, "error": "output is not a json object"}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"timeout after {timeout_sec}s"}
    except Exception as e:
        return {"success": False, "error": str(e)[:240]}



def _launch(headless: bool):
    from playwright.sync_api import sync_playwright

    p = sync_playwright().start()
    browser = p.chromium.launch(
        headless=bool(headless),
        executable_path=CHROME_PATH if os.path.exists(CHROME_PATH) else None,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    )
    return p, browser


def _prefer_http_fetch() -> bool:
    return os.environ.get("MAGI_USE_SCRAPLING", "0").strip().lower() in {"1", "true", "yes", "on"}


def _verify_ssl() -> bool:
    return os.environ.get("MAGI_JUDICIAL_VERIFY_SSL", "0").strip().lower() in {"1", "true", "yes", "on"}


def _requests_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
            "Referer": BASE,
        }
    )
    return session


def _extract_hidden_fields(soup: BeautifulSoup) -> dict:
    payload = {}
    for name in ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION", "__VIEWSTATEENCRYPTED"):
        node = soup.select_one(f'input[name="{name}"]')
        payload[name] = node.get("value", "") if node else ""
    return payload


def _parse_result_items(results_html: str, base_url: str) -> list:
    soup = BeautifulSoup(results_html or "", "html.parser")
    items = []
    seen = set()
    for link in soup.select('a[href*="data.aspx?ty=JD"]'):
        href = str(link.get("href") or "").strip()
        title = " ".join(link.get_text(" ", strip=True).split())
        if not href or not title:
            continue
        full_url = urljoin(base_url, href)
        if full_url in seen:
            continue
        seen.add(full_url)
        items.append({"title": title, "url": full_url})
    return items


def _next_page_href(results_html: str, base_url: str) -> str:
    soup = BeautifulSoup(results_html or "", "html.parser")
    link = soup.find("a", string=lambda text: bool(text and "下一頁" in text))
    if not link:
        return ""
    href = str(link.get("href") or "").strip()
    return urljoin(base_url, html.unescape(href)) if href else ""


def _search_http_impl(
    keywords: str,
    max_results: int = 10,
    timeout_sec: int = 60,
    courts=None,
    case_year: str = "",
    case_word: str = "",
    case_no: str = "",
) -> dict:
    kw = (keywords or "").strip()
    has_structured_filters = bool((courts or []) or (case_year or "").strip() or (case_word or "").strip() or (case_no or "").strip())
    if (not kw) and (not has_structured_filters):
        return {"success": False, "error": "missing keywords_or_case_filters"}

    max_n = max(1, int(max_results or 10))
    timeout = max(10, int(timeout_sec or 60))
    session = _requests_session()
    verify_ssl = _verify_ssl()

    try:
        resp = session.get(BASE, timeout=timeout, verify=verify_ssl)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        payload = _extract_hidden_fields(soup)
        payload.update(
            {
                "jud_kw": kw,
                "jud_year": str(case_year or "").strip(),
                "sel_judword": "",
                "jud_case": str(case_word or "").strip(),
                "jud_no": str(case_no or "").strip(),
                "jud_no_end": "",
                "dy1": "",
                "dm1": "",
                "dd1": "",
                "dy2": "",
                "dm2": "",
                "dd2": "",
                "jud_title": "",
                "jud_jmain": "",
                "KbStart": "",
                "KbEnd": "",
                "judtype": "JUDBOOK",
                "whosub": "1",
                "ctl00$cp_content$btnQry": "送出查詢",
            }
        )
        if courts:
            payload["jud_court"] = [str(c or "").strip() for c in courts if str(c or "").strip()]
        post_resp = session.post(BASE, data=payload, timeout=timeout, verify=verify_ssl)
        post_resp.raise_for_status()
        iframe_match = re.search(r'<iframe[^>]+src="([^"]+)"[^>]+id="iframe-data"', post_resp.text)
        if not iframe_match:
            return {"success": False, "error": "missing_iframe_results"}

        next_url = urljoin(BASE, html.unescape(iframe_match.group(1)))
        items = []
        seen = set()
        for _ in range(10):
            page_resp = session.get(next_url, timeout=timeout, verify=verify_ssl)
            page_resp.raise_for_status()
            page_items = _parse_result_items(page_resp.text, next_url)
            added = 0
            for item in page_items:
                if item["url"] in seen:
                    continue
                seen.add(item["url"])
                items.append(item)
                added += 1
                if len(items) >= max_n:
                    break
            if len(items) >= max_n:
                break
            next_href = _next_page_href(page_resp.text, next_url)
            if not next_href or next_href == next_url or added == 0:
                break
            next_url = next_href

        if not items:
            return {"success": False, "error": "no_results"}

        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            key_seed = json.dumps(
                {
                    "keywords": kw,
                    "courts": courts or [],
                    "case_year": str(case_year or ""),
                    "case_word": str(case_word or ""),
                    "case_no": str(case_no or ""),
                    "engine": "http_form",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            key = hashlib.sha256(("search|" + key_seed).encode("utf-8")).hexdigest()[:16]
            path = os.path.join(CACHE_DIR, f"{key}.results.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "keywords": kw,
                        "courts": courts or [],
                        "case_year": str(case_year or ""),
                        "case_word": str(case_word or ""),
                        "case_no": str(case_no or ""),
                        "results": items,
                        "engine": "http_form",
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            path = ""

        preview_cap = _preview_limit()
        preview = items[: min(preview_cap, len(items))]
        return {
            "success": True,
            "keywords": kw,
            "structured_only": (not kw) and has_structured_filters,
            "count": len(items),
            "results": preview,
            "results_truncated": len(preview) != len(items),
            "results_path": path,
            "engine": "http_form",
        }
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:240]}", "engine": "http_form"}


def _search_impl(
    keywords: str,
    max_results: int = 10,
    headless: bool = True,
    timeout_sec: int = 60,
    courts: list[str] | None = None,
    case_year: str = "",
    case_word: str = "",
    case_no: str = "",
) -> dict:
    if _prefer_http_fetch():
        http_result = _search_http_impl(
            keywords=keywords,
            max_results=max_results,
            timeout_sec=timeout_sec,
            courts=courts,
            case_year=case_year,
            case_word=case_word,
            case_no=case_no,
        )
        if http_result.get("success"):
            return http_result
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
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        )
        page = ctx.new_page()
        # Avoid any implicit 30s default timeout on actions inside the function.
        page.set_default_timeout(timeout_ms)
        page.goto(BASE, wait_until="domcontentloaded", timeout=timeout_ms)

        # Some deployments show the search form only after clicking "裁判書查詢".
        try:
            page.locator("#jud_kw").wait_for(state="visible", timeout=min(8000, timeout_ms))
        except Exception:
            try:
                page.get_by_text("裁判書查詢").first.click(timeout=min(15000, timeout_ms), force=True)
                page.locator("#jud_kw").wait_for(state="visible", timeout=timeout_ms)
            except Exception as e:
                return {"success": False, "error": f"cannot locate search input: {type(e).__name__}: {str(e)[:240]}"}

        # Court filter (multi-select)
        if courts:
            try:
                # Select by visible label; set exactly these options.
                page.select_option("#jud_court", label=[c for c in courts if c])
            except Exception:
                # Best-effort: ignore if UI changes.
                pass

        # Case number fields (optional; more precise than full-text)
        # These fields exist on the page, but may not always be required.
        if (case_year or "").strip():
            try:
                page.locator("#jud_year").fill(str(case_year).strip())
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 146, exc_info=True)
        if (case_word or "").strip():
            try:
                page.locator("#jud_case").fill(str(case_word).strip())
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 151, exc_info=True)
        if (case_no or "").strip():
            try:
                page.locator("#jud_no").fill(str(case_no).strip())
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 156, exc_info=True)

        if kw:
            page.locator("#jud_kw").fill(kw)
        else:
            try:
                page.locator("#jud_kw").fill("")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 164, exc_info=True)

        # Clicking may be blocked by overlays; use force + JS fallback.
        try:
            page.locator("#btnQry").scroll_into_view_if_needed(timeout=min(8000, timeout_ms))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 170, exc_info=True)
        try:
            page.locator("#btnQry").click(timeout=timeout_ms, force=True)
        except Exception:
            try:
                page.evaluate("document.getElementById('btnQry') && document.getElementById('btnQry').click()")
            except Exception as e:
                return {"success": False, "error": f"cannot click submit: {type(e).__name__}: {str(e)[:240]}"}

        # iframe-data may exist but not be "visible" depending on layout; wait for it to be attached.
        # Some runs need a retry because the submit may be ignored under overlays/WAF delays.
        iframe_ok = False
        last_err = ""
        for _ in range(3):
            try:
                page.wait_for_load_state("domcontentloaded", timeout=min(timeout_ms, 30000))
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 187, exc_info=True)
            try:
                page.wait_for_selector("iframe#iframe-data", state="attached", timeout=min(timeout_ms, 20000))
                iframe_ok = True
                break
            except Exception as e:
                last_err = str(e)[:240]
                try:
                    page.evaluate("document.getElementById('btnQry') && document.getElementById('btnQry').click()")
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 197, exc_info=True)
                try:
                    page.wait_for_timeout(1200)
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 201, exc_info=True)
        if not iframe_ok:
            # Return a small HTML hint for debugging (no full dump).
            try:
                html_hint = page.content()
                html_hint = re.sub(r"\s+", " ", html_hint)[:800]
            except Exception:
                html_hint = ""
            return {"success": False, "error": f"iframe-data not found: {last_err}", "html_hint": html_hint}
        frame = page.frame_locator("#iframe-data")
        # The table may exist but not be "visible" depending on layout; wait for attachment.
        try:
            frame.locator("table#jud").wait_for(state="attached", timeout=timeout_ms)
        except Exception:
            # Fallback: wait for at least one result link, which implies table is ready.
            frame.locator("a[href*='data.aspx']").first.wait_for(state="attached", timeout=timeout_ms)

        def _collect_current_page() -> list[dict]:
            # NOTE: locator.all() can hang under some Playwright builds when the
            # underlying frame keeps mutating. Use count()/nth() for stability.
            links = frame.locator("table#jud a[href*='data.aspx']")
            page_items = []
            try:
                n = int(links.count() or 0)
            except Exception:
                n = 0
            for i in range(n):
                try:
                    a = links.nth(i)
                    href = a.get_attribute("href") or ""
                    title = (a.text_content() or "").strip()
                    if not href or not title:
                        continue
                    url = urljoin("https://judgment.judicial.gov.tw/FJUD/", href)
                    page_items.append({"title": title, "url": url})
                except Exception:
                    continue
            return page_items

        items = []
        seen_urls = set()
        for it in _collect_current_page():
            u = (it.get("url") or "").strip()
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            items.append(it)

        # Pagination: click "下一頁" inside the iframe until max_results reached or no progress.
        # This is needed for queries where the desired case is not on the first page.
        while len(items) < max_n:
            try:
                first_href = ""
                try:
                    first_href = (frame.locator("table#jud a[href*='data.aspx']").first.get_attribute("href") or "").strip()
                except Exception:
                    first_href = ""

                next_btn = frame.get_by_text("下一頁", exact=True).first
                if next_btn.count() <= 0:
                    break
                next_btn.click(timeout=min(timeout_ms, 15000), force=True)

                # Wait until the result list changes (first link href changes).
                if first_href:
                    def _changed():
                        try:
                            cur = (frame.locator("table#jud a[href*='data.aspx']").first.get_attribute("href") or "").strip()
                            return bool(cur and cur != first_href)
                        except Exception:
                            return False
                    try:
                        page.wait_for_function("() => true", timeout=500)  # yield to allow frame update
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 275, exc_info=True)
                    for _ in range(40):
                        if _changed():
                            break
                        try:
                            page.wait_for_timeout(250)
                        except Exception:
                            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 282, exc_info=True)
                else:
                    try:
                        page.wait_for_timeout(800)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 287, exc_info=True)

                page_new = _collect_current_page()
                added = 0
                for it in page_new:
                    u = (it.get("url") or "").strip()
                    if not u or u in seen_urls:
                        continue
                    seen_urls.add(u)
                    items.append(it)
                    added += 1
                    if len(items) >= max_n:
                        break
                if added == 0:
                    break
            except Exception:
                break

        items = items[:max_n]

        # Write full results to a cache file to avoid stdout truncation.
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            key_seed = json.dumps(
                {
                    "keywords": kw,
                    "courts": courts or [],
                    "case_year": str(case_year or ""),
                    "case_word": str(case_word or ""),
                    "case_no": str(case_no or ""),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            key = hashlib.sha256(("search|" + key_seed).encode("utf-8")).hexdigest()[:16]
            path = os.path.join(CACHE_DIR, f"{key}.results.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "keywords": kw,
                        "courts": courts or [],
                        "case_year": str(case_year or ""),
                        "case_word": str(case_word or ""),
                        "case_no": str(case_no or ""),
                        "results": items,
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception:
            path = ""

        preview_cap = _preview_limit()
        preview = items[: min(preview_cap, len(items))]
        return {
            "success": True,
            "keywords": kw,
            "structured_only": (not kw) and has_structured_filters,
            "count": len(items),
            "results": preview,
            "results_truncated": (len(preview) != len(items)),
            "results_path": path,
        }
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:240]}"}
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 358, exc_info=True)
        try:
            if p:
                p.stop()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 363, exc_info=True)


def _fetch_text_impl(url: str, headless: bool = True, timeout_sec: int = 45, max_chars: int = 500000) -> dict:
    u = (url or "").strip()
    if not u:
        return {"success": False, "error": "missing url"}
    if _prefer_http_fetch():
        try:
            from skills.research.web_research import fetch_url_content

            fetched = fetch_url_content(u, max_length=max_chars, exempt_iron_dome=True)
            text = _clean_text(str(fetched.get("content") or ""))
            if fetched.get("success") and text:
                os.makedirs(CACHE_DIR, exist_ok=True)
                key = hashlib.sha256(u.encode("utf-8")).hexdigest()[:16]
                path = os.path.join(CACHE_DIR, f"{key}.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text[:max_chars])
                preview = (text[:220] + "...") if len(text) > 220 else text
                return {
                    "success": True,
                    "url": u,
                    "text_preview": preview,
                    "text_path": path,
                    "text_chars": min(len(text), int(max_chars)),
                    "engine": str(fetched.get("engine") or "http_fetch"),
                }
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 239, exc_info=True)
    timeout_ms = int(timeout_sec) * 1000
    max_n = int(max_chars) if int(max_chars) > 0 else 500000

    p = None
    browser = None
    try:
        p, browser = _launch(headless=headless)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
        )
        page = ctx.new_page()
        page.goto(u, wait_until="domcontentloaded", timeout=timeout_ms)

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

        # Write full text to a cache file to avoid JSON stdout truncation by the skill runner.
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            key = hashlib.sha256(u.encode("utf-8")).hexdigest()[:16]
            path = os.path.join(CACHE_DIR, f"{key}.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text[:max_n])
        except Exception as e:
            return {"success": False, "error": f"write cache failed: {type(e).__name__}: {str(e)[:200]}"}

        # Keep stdout small to survive the skill runner's 1200-char truncation.
        preview = (text[:220] + "...") if len(text) > 220 else text
        return {
            "success": True,
            "url": u,
            "text_preview": preview,
            "text_path": path,
            "text_chars": min(len(text), max_n),
        }
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:240]}"}
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 426, exc_info=True)
        try:
            if p:
                p.stop()
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 431, exc_info=True)


def _self_test_impl() -> dict:
    smoke_max = int(os.environ.get("JUDICIAL_SEARCH_SMOKE_MAX_RESULTS", "8") or "8")
    for kw in ["詐欺", "侵權", "損害賠償"]:
        r = _search_impl(kw, max_results=smoke_max, headless=True, timeout_sec=60)
        if r.get("success") and (r.get("count", 0) or 0) > 0:
            u = (r.get("results") or [{}])[0].get("url") or ""
            t = _fetch_text_impl(u, headless=True, timeout_sec=45, max_chars=5000) if u else {"success": False, "error": "missing url from search"}
            return {"success": bool(t.get("success")), "search": r, "fetch_text": t, "chrome_path": CHROME_PATH}
    return {"success": False, "error": "no results for self_test keywords", "chrome_path": CHROME_PATH}


def main() -> int:
    ap = argparse.ArgumentParser(description="judicial-web-search skill")
    ap.add_argument("--task", default="help", help="task text")
    args = ap.parse_args()
    task = (args.task or "").strip()

    if task in {"help", "summary", "list"}:
        return _ok({"success": True, "commands": ["help", "self_test", "search {..json..}", "fetch_text {..json..}"]})

    # If we're running inside the dedicated venv, or if HTTP-form fetching is enabled,
    # execute logic directly in the current interpreter.
    if os.environ.get("JUDICIAL_USE_VENV", "").strip() == "1" or _prefer_http_fetch():
        if task == "self_test":
            return _ok(_self_test_impl())
        if task.startswith("search"):
            payload = _load_jsonish(task[len("search") :].strip())
            return _ok(
                _search_impl(
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
                _fetch_text_impl(
                    url=(payload.get("url") or payload.get("value") or "").strip(),
                    headless=bool(payload.get("headless", True)),
                    timeout_sec=int(payload.get("timeout_sec", 45) or 45),
                    max_chars=int(payload.get("max_chars", 500000) or 500000),
                )
            )
        return _ok({"success": False, "error": f"unknown task: {task}"})

    if task == "self_test":
        # Self-test may take longer depending on site load/WAF delays.
        return _ok(_run_venv("self_test", timeout_sec=240))

    if task.startswith("search") or task.startswith("搜尋判決"):
        key = "search" if task.startswith("search") else "搜尋判決"
        payload = _load_jsonish(task[len(key) :].strip())
        # Allow simple one-liner: "搜尋判決 詐欺" -> treat as keywords.
        if "value" in payload and isinstance(payload.get("value"), str) and not payload.get("keywords"):
            payload["keywords"] = payload.get("value")
        timeout = int(payload.get("timeout_sec", 60) or 60)
        return _ok(_run_venv("search " + json.dumps(payload, ensure_ascii=False), timeout_sec=timeout + 60))

    if task.startswith("fetch_text") or task.startswith("抓全文"):
        key = "fetch_text" if task.startswith("fetch_text") else "抓全文"
        payload = _load_jsonish(task[len(key) :].strip())
        timeout = int(payload.get("timeout_sec", 45) or 45)
        return _ok(_run_venv("fetch_text " + json.dumps(payload, ensure_ascii=False), timeout_sec=timeout + 60))

    return _ok({"success": False, "error": f"unknown task: {task}"})


if __name__ == "__main__":
    raise SystemExit(main())

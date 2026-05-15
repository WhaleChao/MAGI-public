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
import ssl
from datetime import datetime
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from urllib import request as _urlrequest
from urllib import error as _urlerror
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
JDG_API_BASE = os.environ.get("JUDICIAL_API_BASE", "https://data.judicial.gov.tw/jdg/api").rstrip("/")
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


def _search_page_limit(max_results: int) -> int:
    try:
        cap = int(os.environ.get("JUDICIAL_SEARCH_MAX_PAGES", "80") or "80")
    except Exception:
        cap = 80
    try:
        desired = int(max_results or 10)
    except Exception:
        desired = 10
    # Judicial result pages usually hold about 20 items.  Keep the old 10-page
    # floor for normal searches, but allow larger exports to actually reach the
    # requested max_results.
    pages = max(10, (max(1, desired) // 20) + 3)
    return max(1, min(cap, pages))


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


def _http_court_values(soup: BeautifulSoup, courts) -> list[str]:
    wanted = [str(c or "").strip() for c in (courts or []) if str(c or "").strip()]
    if not wanted:
        return []
    select = soup.find(id="jud_court") or soup.find(attrs={"name": "jud_court"})
    if not select:
        return wanted
    by_label = {}
    by_value = {}
    for opt in select.find_all("option"):
        label = " ".join(opt.get_text(" ", strip=True).split())
        value = str(opt.get("value") or "").strip()
        if label:
            by_label[label] = value
        if value:
            by_value[value] = value
    values = []
    for court in wanted:
        resolved = by_value.get(court) or by_label.get(court) or court
        if resolved:
            values.append(resolved)
    return values


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


def _parse_total_count(results_html: str) -> int | None:
    text = BeautifulSoup(results_html or "", "html.parser").get_text(" ", strip=True)
    patterns = [
        r"共\s*([0-9,]+)\s*筆",
        r"總\s*筆\s*數\s*[:：]?\s*([0-9,]+)",
        r"查詢結果\s*[:：]?\s*([0-9,]+)\s*筆",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue
        try:
            return int(m.group(1).replace(",", ""))
        except Exception:
            continue
    return None


def _next_page_href(results_html: str, base_url: str) -> str:
    soup = BeautifulSoup(results_html or "", "html.parser")
    link = soup.find("a", string=lambda text: bool(text and "下一頁" in text))
    if not link:
        return ""
    href = str(link.get("href") or "").strip()
    return urljoin(base_url, html.unescape(href)) if href else ""


_JUDICIAL_UI_NOISE = {
    "去格式引用",
    "分享網址",
    "名詞查詢",
    "名詞收集",
    "裁判易讀小幫手",
    "友善列印",
    "轉存PDF",
    "分享",
    "P",
    "列印歷審清單",
}

_JUDICIAL_BOTTOM_STOPS = {
    "歷審裁判",
    "相關法條",
    "相關判解",
    "相關裁判",
}


def _normalize_judicial_text(text: str) -> str:
    """Clean Judicial Yuan page chrome and rebuild readable judgment paragraphs."""
    raw = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    raw = raw.replace("\xa0", " ").replace("\u3000", " ")
    if not raw.strip():
        return ""

    lines: list[str] = []
    for item in raw.split("\n"):
        line = re.sub(r"\s+", " ", str(item or "")).strip()
        if not line:
            continue
        if line in _JUDICIAL_UI_NOISE:
            continue
        if line.startswith("若您有連結此資料內容之需求"):
            continue
        if line.startswith("請選取上方網址後"):
            continue
        if line.startswith("分享網址："):
            continue
        lines.append(line)

    trimmed: list[str] = []
    body_started = False
    for line in lines:
        compact = re.sub(r"\s+", "", line)
        if compact in {"主文", "理由", "事實及理由"}:
            body_started = True
        if body_started and line in _JUDICIAL_BOTTOM_STOPS:
            break
        trimmed.append(line)
    lines = trimmed

    # Combine common metadata labels split by the website renderer.
    combined: list[str] = []
    i = 0
    metadata_labels = {"裁判字號：", "裁判日期：", "裁判案由："}
    while i < len(lines):
        line = lines[i]
        if line in metadata_labels and i + 1 < len(lines):
            combined.append(f"{line}{lines[i + 1]}")
            i += 2
            continue
        combined.append(line)
        i += 1

    heading_map = {
        "主文": "主文",
        "理由": "理由",
        "事實及理由": "事實及理由",
    }

    def _is_heading(line: str) -> bool:
        compact = re.sub(r"\s+", "", line)
        if compact in heading_map:
            return True
        if re.fullmatch(r"[甲乙丙丁戊己庚辛壬癸]、.+", line):
            return True
        if re.fullmatch(r"(?:據上論結|中華民國).*", compact):
            return True
        return False

    def _starts_new_paragraph(line: str) -> bool:
        if _is_heading(line):
            return True
        if re.match(r"^[一二三四五六七八九十百]+、", line):
            return True
        if re.match(r"^[㈠㈡㈢㈣㈤㈥㈦㈧㈨㈩]", line):
            return True
        if re.match(r"^[⒈⒉⒊⒋⒌⒍⒎⒏⒐⒑]", line):
            return True
        if re.match(r"^\d+[.、]", line):
            return True
        if line.startswith(("裁判字號：", "裁判日期：", "裁判案由：")):
            return True
        return False

    def _display_line(line: str) -> str:
        compact = re.sub(r"\s+", "", line)
        if compact in heading_map:
            return heading_map[compact]
        if compact.startswith("中華民國"):
            return compact
        return line

    def _join_sep(prev: str, nxt: str) -> str:
        if not prev or not nxt:
            return ""
        if prev.endswith(("-", "—", "–", "/", "（", "(")):
            return ""
        if re.search(r"[A-Za-z0-9]$", prev) and re.match(r"^[A-Za-z0-9]", nxt):
            return " "
        return ""

    out: list[str] = []
    buf = ""

    def _flush() -> None:
        nonlocal buf
        value = re.sub(r"\s+", " ", buf).strip()
        if value:
            out.append(value)
        buf = ""

    for original in combined:
        line = _display_line(original)
        compact = re.sub(r"\s+", "", line)
        if not line:
            continue
        if line.startswith(("裁判字號：", "裁判日期：", "裁判案由：")):
            _flush()
            out.append(line)
            continue
        if _is_heading(line):
            _flush()
            out.append(compact if compact in heading_map else line)
            continue
        if _starts_new_paragraph(line):
            _flush()
            buf = line
            continue
        if not buf:
            buf = line
            continue
        buf = f"{buf}{_join_sep(buf, line)}{line}"
    _flush()

    # Drop accidental exact duplicates while preserving order.
    final: list[str] = []
    prev = ""
    for para in out:
        norm = re.sub(r"\s+", " ", para).strip()
        if not norm or norm == prev:
            continue
        final.append(norm)
        prev = norm
    return "\n\n".join(final).strip()


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
            payload["jud_court"] = _http_court_values(soup, courts)
        post_resp = session.post(BASE, data=payload, timeout=timeout, verify=verify_ssl)
        post_resp.raise_for_status()
        iframe_match = re.search(r'<iframe[^>]+src="([^"]+)"[^>]+id="iframe-data"', post_resp.text)
        if not iframe_match:
            return {"success": False, "error": "missing_iframe_results"}

        next_url = urljoin(BASE, html.unescape(iframe_match.group(1)))
        items = []
        seen = set()
        pages_scanned = 0
        total_count = None
        for _ in range(_search_page_limit(max_n)):
            pages_scanned += 1
            page_resp = session.get(next_url, timeout=timeout, verify=verify_ssl)
            page_resp.raise_for_status()
            if total_count is None:
                total_count = _parse_total_count(page_resp.text)
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
                        "pages_scanned": pages_scanned,
                        "total_count": total_count,
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
            "pages_scanned": pages_scanned,
            "total_count": total_count,
            "incomplete": bool(total_count and len(items) < total_count),
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
        total_count = None
        try:
            total_count = _parse_total_count(frame.locator("body").inner_text(timeout=min(timeout_ms, 8000)) or "")
        except Exception:
            total_count = None
        for it in _collect_current_page():
            u = (it.get("url") or "").strip()
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            items.append(it)

        # Pagination: click "下一頁" inside the iframe until max_results reached or no progress.
        # This is needed for queries where the desired case is not on the first page.
        pages_scanned = 1
        page_limit = _search_page_limit(max_n)
        while len(items) < max_n and pages_scanned < page_limit:
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
                pages_scanned += 1
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
                        "engine": "playwright",
                        "pages_scanned": pages_scanned,
                        "total_count": total_count,
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
            "engine": "playwright",
            "pages_scanned": pages_scanned,
            "total_count": total_count,
            "incomplete": bool(total_count and len(items) < total_count),
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


_jdg_ssl_ctx_cache: dict[str, ssl.SSLContext] = {}


def _jid_from_judgment_url(url: str) -> str:
    try:
        qs = parse_qs(urlsplit(url or "").query)
        raw = (qs.get("id") or [""])[0]
        return unquote(str(raw or "")).strip()
    except Exception:
        return ""


def _load_code_config_for_jdg() -> dict:
    paths = [
        os.path.join(_MAGI_ROOT, "code", "json", "config.json"),
        os.path.join(_MAGI_ROOT, "config.json"),
    ]
    for path in paths:
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            continue
    return {}


def _get_jdg_credentials() -> tuple[str, str, str]:
    user = (
        os.environ.get("MAGI_JUDICIAL_API_USER")
        or os.environ.get("JUDICIAL_API_USER")
        or os.environ.get("JDG_API_USER")
        or ""
    ).strip()
    pwd = (
        os.environ.get("MAGI_JUDICIAL_API_PASS")
        or os.environ.get("MAGI_JUDICIAL_API_PASSWORD")
        or os.environ.get("JUDICIAL_API_PASSWORD")
        or os.environ.get("JDG_API_PASSWORD")
        or ""
    ).strip()
    if user and pwd:
        return user, pwd, "env"
    cfg = _load_code_config_for_jdg()
    user = str(cfg.get("judicial_api_user") or "").strip()
    pwd = str(cfg.get("judicial_api_pass") or "").strip()
    if user and pwd:
        return user, pwd, "config.judicial_api_*"
    judicial = cfg.get("judicial")
    if isinstance(judicial, dict):
        user = str(judicial.get("api_user") or "").strip()
        pwd = str(judicial.get("api_password") or "").strip()
        if user and pwd:
            return user, pwd, "config.judicial.api_*"
    return "", "", ""


def _build_jdg_ssl_context() -> ssl.SSLContext:
    cached = _jdg_ssl_ctx_cache.get("ctx")
    if cached is not None:
        return cached
    try:
        import certifi

        ca_bundle = certifi.where()
    except Exception:
        ca_bundle = None
    ctx = ssl.create_default_context(cafile=ca_bundle)
    try:
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    except Exception:
        pass
    _jdg_ssl_ctx_cache["ctx"] = ctx
    return ctx


def _jdg_post_json(path: str, payload: dict, timeout_sec: int = 25) -> dict:
    url = JDG_API_BASE + "/" + path.lstrip("/")
    req = _urlrequest.Request(
        url,
        data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _urlrequest.urlopen(req, timeout=max(5, int(timeout_sec)), context=_build_jdg_ssl_context()) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        obj = json.loads(raw or "{}")
        return obj if isinstance(obj, dict) else {"value": obj}
    except _urlerror.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        return {"error": f"HTTP {getattr(e, 'code', 'ERR')}", "body": body[:500]}
    except Exception as e:
        return {"error": str(e)[:240]}


def _extract_jdoc_text(payload: dict) -> tuple[str, str]:
    if not isinstance(payload, dict):
        return "", ""
    jfullx = payload.get("JFULLX") or {}
    if isinstance(jfullx, list):
        jfullx = jfullx[0] if jfullx else {}
    if not isinstance(jfullx, dict):
        jfullx = {}
    title = str(payload.get("JTITLE") or payload.get("TITLE") or "").strip()
    text = str(jfullx.get("JFULLCONTENT") or payload.get("JFULLCONTENT") or "").strip()
    return title, _normalize_judicial_text(_clean_text(text))


def _fetch_text_from_jdg_api(url: str, timeout_sec: int, max_chars: int) -> dict:
    jid = _jid_from_judgment_url(url)
    if not jid:
        return {"success": False, "error": "missing_jid_from_url", "engine": "judicial_api"}
    user, pwd, cred_source = _get_jdg_credentials()
    if not user or not pwd:
        return {"success": False, "error": "missing_judicial_api_credentials", "engine": "judicial_api"}
    auth = _jdg_post_json("Auth", {"user": user, "password": pwd}, timeout_sec=timeout_sec)
    token = str((auth or {}).get("token") or "").strip() if isinstance(auth, dict) else ""
    if not token:
        err = str((auth or {}).get("error") or (auth or {}).get("message") or "") if isinstance(auth, dict) else ""
        if "非本 API 服務時間" in err:
            return {
                "success": False,
                "error": "judicial_api_outside_service_window",
                "recoverable": True,
                "engine": "judicial_api",
                "jid": jid,
                "credential_source": cred_source,
            }
        return {"success": False, "error": (err or "judicial_api_auth_failed")[:240], "engine": "judicial_api", "jid": jid}
    doc = _jdg_post_json("JDoc", {"token": token, "j": jid}, timeout_sec=timeout_sec)
    title, text = _extract_jdoc_text(doc if isinstance(doc, dict) else {})
    if not text:
        return {"success": False, "error": "judicial_api_empty_jdoc", "engine": "judicial_api", "jid": jid}
    os.makedirs(CACHE_DIR, exist_ok=True)
    key = hashlib.sha256(("jdg|" + jid).encode("utf-8")).hexdigest()[:16]
    path = os.path.join(CACHE_DIR, f"{key}.txt")
    max_n = int(max_chars) if int(max_chars) > 0 else 500000
    with open(path, "w", encoding="utf-8") as f:
        f.write(text[:max_n])
    preview = (text[:220] + "...") if len(text) > 220 else text
    return {
        "success": True,
        "url": url,
        "jid": jid,
        "title": title,
        "text_preview": preview,
        "text_path": path,
        "text_chars": min(len(text), max_n),
        "engine": "judicial_api",
    }


def _write_fetch_cache(url: str, text: str, max_chars: int, engine: str, title: str = "") -> dict:
    os.makedirs(CACHE_DIR, exist_ok=True)
    max_n = int(max_chars) if int(max_chars) > 0 else 500000
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    path = os.path.join(CACHE_DIR, f"{key}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text[:max_n])
    preview = (text[:220] + "...") if len(text) > 220 else text
    return {
        "success": True,
        "url": url,
        "title": title,
        "text_preview": preview,
        "text_path": path,
        "text_chars": min(len(text), max_n),
        "engine": engine,
    }


def _fetch_text_from_html_http(url: str, timeout_sec: int, max_chars: int) -> dict:
    session = _requests_session()
    resp = session.get(url, timeout=max(10, int(timeout_sec or 45)), verify=_verify_ssl())
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text or "", "html.parser")
    title = " ".join((soup.title.get_text(" ", strip=True) if soup.title else "").split())
    node = soup.select_one("div#jud") or soup.select_one("#jud")
    raw = node.get_text("\n", strip=False) if node else soup.get_text("\n", strip=False)
    text = _normalize_judicial_text(_clean_text(raw))
    if "查無資料" in text or "系統忙碌" in text:
        return {"success": False, "error": "site returned error page", "hint": text[:200], "engine": "html_http"}
    if len(text) < 80 or ("裁判字號" not in text and "主文" not in text and "理由" not in text):
        return {"success": False, "error": "html_http_empty_or_unrecognized", "hint": text[:200], "engine": "html_http"}
    return _write_fetch_cache(url, text, max_chars=max_chars, engine="html_http", title=title)


def _fetch_text_impl(url: str, headless: bool = True, timeout_sec: int = 45, max_chars: int = 500000) -> dict:
    u = (url or "").strip()
    if not u:
        return {"success": False, "error": "missing url"}
    try:
        direct_result = _fetch_text_from_html_http(u, timeout_sec=timeout_sec, max_chars=max_chars)
        if direct_result.get("success"):
            return direct_result
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 239, exc_info=True)
    if _prefer_http_fetch():
        try:
            from skills.research.web_research import fetch_url_content

            fetched = fetch_url_content(u, max_length=max_chars, exempt_iron_dome=True)
            text = _clean_text(str(fetched.get("content") or ""))
            text = _normalize_judicial_text(text)
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
        api_result = _fetch_text_from_jdg_api(u, timeout_sec=timeout_sec, max_chars=max_chars)
        if api_result.get("success"):
            return api_result
        if api_result.get("recoverable"):
            # Keep the fallback status when the official site is rate-limiting the
            # HTML page and the API is simply outside its service window.
            pass
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

        text = _normalize_judicial_text(_clean_text(text))
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
        api_result = _fetch_text_from_jdg_api(u, timeout_sec=timeout_sec, max_chars=max_chars)
        if api_result.get("success"):
            return api_result
        if api_result.get("recoverable"):
            return {
                "success": False,
                "error": f"{type(e).__name__}: {str(e)[:160]}",
                "recoverable": True,
                "fallback": api_result,
            }
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:240]}", "fallback": api_result}
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

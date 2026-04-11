#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data/fetcher.py — 市場資料抓取函數
零外部依賴（只用標準庫）
"""
from __future__ import annotations

import json
import logging
import os
import re
import ssl
from typing import Any, Dict, List, Optional, Tuple
from urllib import parse, request

# ── 路徑推導（不 import action.py）────────────────────────────────
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent.parent  # market-briefing/
_MAGI_ROOT = _SKILL_DIR.parents[1]
_AGENT_DIR = _MAGI_ROOT / ".agent"
_CACHE_PATH = _AGENT_DIR / "market_data_cache.json"

# Import only indicators from sibling module
from data.indicators import _pct  # noqa: E402


def _http_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 15, insecure: bool = False) -> Any:
    hdr = {
        "User-Agent": "MAGI-market-briefing/1.0",
        "Accept": "application/json,text/plain,*/*",
    }
    if headers:
        hdr.update(headers)
    req = request.Request(url, headers=hdr)
    kwargs: Dict[str, Any] = {"timeout": timeout}
    if insecure:
        kwargs["context"] = ssl._create_unverified_context()  # noqa: SLF001
    with request.urlopen(req, **kwargs) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    return json.loads(raw)


def _yahoo_history(symbol: str, period: str = "3mo", with_volume: bool = False):
    q = parse.quote(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{q}?range={period}&interval=1d"
    data = _http_json(url, timeout=18)
    chart = (((data or {}).get("chart") or {}).get("result") or [{}])[0]
    ts = chart.get("timestamp") or []
    quote = (((chart.get("indicators") or {}).get("quote") or [{}])[0] or {})
    closes_raw = quote.get("close") or []
    highs_raw = quote.get("high") or []
    lows_raw = quote.get("low") or []
    vols_raw = quote.get("volume") or []
    closes: List[float] = []
    tss: List[int] = []
    highs: List[float] = []
    lows: List[float] = []
    volumes: List[int] = []
    for i, (t, c) in enumerate(zip(ts, closes_raw)):
        if c is None:
            continue
        try:
            cv = float(c)
        except Exception:
            continue
        closes.append(cv)
        tss.append(int(t))
        if with_volume:
            highs.append(float(highs_raw[i]) if i < len(highs_raw) and highs_raw[i] is not None else cv)
            lows.append(float(lows_raw[i]) if i < len(lows_raw) and lows_raw[i] is not None else cv)
            volumes.append(int(vols_raw[i]) if i < len(vols_raw) and vols_raw[i] is not None else 0)
    if with_volume:
        return closes, tss, highs, lows, volumes
    return closes, tss


def _load_cache_fetcher() -> Dict[str, Any]:
    """Load cache directly (no circular import)."""
    try:
        if _CACHE_PATH.exists():
            return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"twse_lookup": {}, "sec_tickers": {}, "updated_at": ""}


def _save_cache_fetcher(cache: Dict[str, Any]) -> None:
    """Save cache directly (no circular import)."""
    _AGENT_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _CACHE_PATH.with_suffix(_CACHE_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_CACHE_PATH)


# ── 常用台股別名（避免 circular import 不從 action.py 讀）─────────
COMMON_TW_ALIASES = {
    "台積電": "2330",
    "聯發科": "2454",
    "鴻海": "2317",
    "廣達": "2382",
    "台達電": "2308",
    "中華電": "2412",
    "兆豐金": "2886",
    "富邦金": "2881",
    "國泰金": "2882",
    "台新金": "2887",
}


def _tz_now_str() -> str:
    """Return current date string YYYY-MM-DD without importing action.py."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _get_twse_lookup(force: bool = False) -> Dict[str, Dict[str, str]]:
    cache = _load_cache_fetcher()
    today = _tz_now_str()
    stored = cache.get("twse_lookup") if isinstance(cache.get("twse_lookup"), dict) else {}
    if stored and not force and str(stored.get("_date") or "") == today:
        return stored

    lookup: Dict[str, Dict[str, str]] = {"_date": today}
    endpoints = [
        "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
        "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_AVG_ALL",
    ]
    rows: List[Dict[str, Any]] = []
    for ep in endpoints:
        try:
            data = _http_json(ep, timeout=20, insecure=True)
            if isinstance(data, list) and data:
                rows = data
                break
        except Exception:
            continue

    for r in rows:
        if not isinstance(r, dict):
            continue
        code = str(r.get("公司代號") or r.get("Code") or r.get("證券代號") or "").strip()
        name = str(r.get("公司簡稱") or r.get("Name") or r.get("證券名稱") or "").strip()
        if not code or not re.fullmatch(r"\d{4,5}", code):
            continue
        if not name:
            name = code
        sym = f"{code}.TW"
        lookup[code] = {"symbol": sym, "label": name, "market": "TW"}
        lookup[name] = {"symbol": sym, "label": name, "market": "TW"}

    for n, c in COMMON_TW_ALIASES.items():
        if c in lookup:
            lookup[n] = lookup[c]
        else:
            lookup[n] = {"symbol": f"{c}.TW", "label": n, "market": "TW"}

    cache["twse_lookup"] = lookup
    _save_cache_fetcher(cache)
    return lookup


def _get_sec_tickers(force: bool = False) -> Dict[str, str]:
    cache = _load_cache_fetcher()
    today = _tz_now_str()
    stored = cache.get("sec_tickers") if isinstance(cache.get("sec_tickers"), dict) else {}
    if stored and not force and str(stored.get("_date") or "") == today:
        return stored

    out: Dict[str, str] = {"_date": today}
    try:
        sec_ua = os.environ.get("SEC_USER_AGENT", "MAGI-market-briefing/1.0 (admin@magi.local)")
        data = _http_json(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": sec_ua},
            timeout=20,
        )
        if isinstance(data, dict):
            for v in data.values():
                if not isinstance(v, dict):
                    continue
                t = str(v.get("ticker") or "").upper().strip()
                cik = str(v.get("cik_str") or "").strip()
                if t and cik:
                    out[t] = cik.zfill(10)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_get_sec_tickers", exc_info=True)

    cache["sec_tickers"] = out
    _save_cache_fetcher(cache)
    return out


def _latest_tw_financials(code4: str) -> Dict[str, str]:
    res = {"rev": "", "eps": ""}
    # monthly revenue
    try:
        rows = _http_json("https://openapi.twse.com.tw/v1/opendata/t187ap05_L", timeout=20, insecure=True)
        if isinstance(rows, list):
            candidates = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                code = str(r.get("公司代號") or "").strip()
                if code != code4:
                    continue
                yyyymm = str(r.get("出表日期") or r.get("資料年月") or r.get("年月") or "").strip()
                yoy = str(r.get("營業收入-去年同月增減(%)") or r.get("去年同月增減") or "").strip()
                candidates.append((yyyymm, yoy))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                ym, yoy = candidates[0]
                if ym or yoy:
                    res["rev"] = f"月營收({ym}) YoY {yoy or 'n/a'}%"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_latest_tw_financials_rev", exc_info=True)

    # quarterly eps
    try:
        rows = _http_json("https://openapi.twse.com.tw/v1/opendata/t187ap14_L", timeout=20, insecure=True)
        if isinstance(rows, list):
            candidates = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                code = str(r.get("公司代號") or "").strip()
                if code != code4:
                    continue
                yr = str(r.get("年度") or "").strip()
                qt = str(r.get("季別") or "").strip()
                q = str(r.get("資料年度季別") or r.get("年度季別") or "").strip()
                if not q and (yr or qt):
                    q = f"{yr}Q{qt}" if yr and qt else (yr or qt)
                eps = str(r.get("基本每股盈餘(元)") or r.get("每股盈餘(元)") or r.get("EPS") or "").strip()
                candidates.append((q, eps))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                q, eps = candidates[0]
                if q or eps:
                    res["eps"] = f"季報({q}) EPS {eps or 'n/a'}"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_latest_tw_financials_eps", exc_info=True)

    return res


def _latest_us_filing(symbol: str) -> str:
    symbol = symbol.upper().strip()
    sec = _get_sec_tickers()
    cik = str(sec.get(symbol) or "").strip()
    if not cik:
        return ""
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        sec_ua = os.environ.get("SEC_USER_AGENT", "MAGI-market-briefing/1.0 (admin@magi.local)")
        data = _http_json(url, headers={"User-Agent": sec_ua}, timeout=20)
        recent = ((data or {}).get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        accs = recent.get("accessionNumber") or []
        for form, date, acc in zip(forms, dates, accs):
            f = str(form or "").strip().upper()
            if f in {"10-K", "10-Q", "8-K"}:
                return f"SEC {f} 申報日 {date} ({str(acc or '')[:18]})"
    except Exception:
        return ""
    return ""

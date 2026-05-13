"""
skills/market-briefing/utils.py
Data gathering utilities for the Hedge Fund Committee.
Extracted and modernized from legacy action.py.
"""
from __future__ import annotations
import os
import sys
import json
import logging
import urllib.request
import ssl
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional, Tuple

logger = logging.getLogger("MarketUtils")

def _http_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 15, insecure: bool = False) -> Any:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        ctx = None
        if insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logger.debug(f"HTTP GET failed: {url} - {e}")
        return None

def fetch_yahoo_history(symbol: str, period: str = "3mo") -> List[float]:
    """Fetch close prices from Yahoo Finance API."""
    try:
        # Standard Yahoo Finance v8 chart API
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={period}&interval=1d"
        data = _http_json(url)
        result = data.get("chart", {}).get("result", [])
        if not result:
            return []
        closes = result[0].get("indicators", {}).get("adjclose", [{}])[0].get("adjclose", [])
        return [float(c) for c in closes if c is not None]
    except Exception as e:
        logger.error(f"Failed to fetch Yahoo history for {symbol}: {e}")
        return []

def get_tw_financials(code4: str) -> Dict[str, str]:
    """Fetch latest financials for TW stock from TWSE OpenAPI."""
    # Simplified version for the committee signals
    res = {"rev": "無資料", "eps": "無資料"}
    try:
        # Monthly Revenue
        rows = _http_json("https://openapi.twse.com.tw/v1/opendata/t187ap05_L", insecure=True)
        if rows:
            for r in rows:
                if r.get("公司代號") == code4:
                    res["rev"] = f"YoY {r.get('營業收入-去年同月增減(%)', 'n/a')}%"
                    break
        # EPS
        rows = _http_json("https://openapi.twse.com.tw/v1/opendata/t187ap14_L", insecure=True)
        if rows:
            for r in rows:
                if r.get("公司代號") == code4:
                    res["eps"] = f"EPS {r.get('基本每股盈餘(元)', 'n/a')}"
                    break
    except Exception:
        pass
    return res

def gather_all_data(ticker: str) -> Dict[str, Any]:
    """Composite data gathering for the committee."""
    is_tw = ticker.isdigit() and len(ticker) >= 4
    yahoo_ticker = f"{ticker}.TW" if is_tw else ticker
    
    closes = fetch_yahoo_history(yahoo_ticker)
    
    fundamentals = {}
    if is_tw:
        fundamentals = get_tw_financials(ticker)
    else:
        fundamentals = {"rev": "See SEC Filings", "eps": "See Analyst Estimates"}
    
    # Placeholder for news (would use web_research in full implementation)
    news = [f"市場關注 {ticker} 近期走勢", f"{ticker} 相關行業動態觀察"]
    
    return {
        "ticker": ticker,
        "closes": closes,
        "fundamentals": fundamentals,
        "news": news,
        "is_tw": is_tw
    }

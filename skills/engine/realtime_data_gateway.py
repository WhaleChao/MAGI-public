"""
skills/engine/realtime_data_gateway.py
=======================================
即時資料閘道（Real-time Data Gateway）

設計原則
--------
1. **LLM 不碰即時數字**：天氣/股價/匯率必須來自 authoritative API，
   raw data 直接格式化後回傳，不讓 LLM 合成/四捨五入。
2. **無 API 時明確拒絕**：沒有即時資料來源時，直接說「我沒有即時資料，
   請查 [authoritative URL]」，不嘗試用搜尋引擎猜測。
3. **不依賴 DuckDuckGo / ReAct**：這類查詢的精確度要求超過搜尋引擎能保證的。

支援類型
--------
- weather  → 中央氣象署（CWA）OpenData API 或網頁抓取
- stock    → 台灣證交所 TWSE 公開 API（不需 key）
- fx_rate  → 僅提示查詢網址（暫未接 API）

環境變數
--------
- MAGI_CWA_API_KEY : CWA OpenData API 授權碼（可到 opendata.cwa.gov.tw 免費申請）
  若未設定，改走網頁抓取 fallback；若抓取也失敗則明確拒絕。

2026-04-20 初始版本
"""

from __future__ import annotations

import os
import re
import json
import logging
import time
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 縣市代碼對照
# ---------------------------------------------------------------------------
_COUNTY_MAP: Dict[str, str] = {
    # 縣市名稱 → CWA 縣市 ID (F-C0032-001 用) & 縣市全名
    "臺北": "63", "台北": "63",
    "新北": "65", "板橋": "65",
    "基隆": "10017",
    "桃園": "68",
    "新竹市": "10018", "新竹縣": "10004",
    "新竹": "10004",
    "苗栗": "10005",
    "臺中": "66", "台中": "66",
    "彰化": "10007",
    "南投": "10008",
    "雲林": "10009",
    "嘉義市": "10020", "嘉義縣": "10010",
    "嘉義": "10010",
    "臺南": "67", "台南": "67",
    "高雄": "64",
    "屏東": "10013",
    "臺東": "10014", "台東": "10014",
    "花蓮": "10015",
    "宜蘭": "10002",
    "澎湖": "10016",
    "金門": "09020",
    "連江": "09007",
}

# CWA OpenData 36小時天氣預報 API
_CWA_API_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001"
_CWA_COUNTY_PAGE = "https://www.cwa.gov.tw/V8/C/W/County/County.html"

# TWSE 即時報價
_TWSE_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"


# ---------------------------------------------------------------------------
# 查詢類型分類
# ---------------------------------------------------------------------------

_WEATHER_KEYWORDS = [
    "天氣", "氣溫", "溫度", "下雨", "下雪", "颱風", "降雨", "天晴",
    "陰天", "晴天", "氣象", "預報", "降雪", "豪雨", "颳風", "大風",
    "濕度", "weather", "forecast", "會下", "會不會下", "明天",
]
_STOCK_KEYWORDS = ["股價", "股票", "台積電", "鴻海", "大盤", "加權", "漲", "跌", "點數",
                   "上市", "上櫃", "TWSE", "TSE", "股", "元/股"]
_FX_KEYWORDS = ["匯率", "美金", "日圓", "歐元", "人民幣", "港幣", "換算", "外幣",
                "exchange rate", "forex"]


def classify_realtime_query(text: str) -> Optional[str]:
    """
    回傳即時資料類型 ("weather" / "stock" / "fx_rate") 或 None（非即時查詢）。
    """
    lowered = (text or "").lower()
    if any(k in lowered for k in _WEATHER_KEYWORDS):
        return "weather"
    if any(k in lowered for k in _STOCK_KEYWORDS):
        return "stock"
    if any(k in lowered for k in _FX_KEYWORDS):
        return "fx_rate"
    return None


def _extract_location(text: str) -> Optional[str]:
    """從查詢文字中抽取縣市地點。"""
    for loc in sorted(_COUNTY_MAP.keys(), key=len, reverse=True):
        if loc in text:
            return loc
    return None


# ---------------------------------------------------------------------------
# CWA 天氣查詢
# ---------------------------------------------------------------------------

def _query_cwa_api(location_name: str) -> Dict[str, Any]:
    """使用 CWA OpenData API 查詢 36h 預報（需 MAGI_CWA_API_KEY）。"""
    api_key = os.environ.get("MAGI_CWA_API_KEY", "").strip()
    if not api_key:
        return {"success": False, "error": "no_api_key"}

    try:
        import urllib.request
        params = f"?Authorization={api_key}&locationName={location_name}&elementName=Wx,PoP,MinT,MaxT"
        url = _CWA_API_URL + params
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("success") != "true":
            return {"success": False, "error": "api_error", "raw": data}

        records = data.get("records", {}).get("location", [])
        if not records:
            return {"success": False, "error": "no_records"}

        loc_data = records[0]
        elements = {e["elementName"]: e["time"] for e in loc_data.get("weatherElement", [])}

        result_lines = []
        wx = elements.get("Wx", [])
        pop = elements.get("PoP", [])
        mint = elements.get("MinT", [])
        maxt = elements.get("MaxT", [])

        for i, period in enumerate(wx[:3]):  # 最多 3 個時段
            start = period["startTime"][:16].replace("T", " ")
            end = period["endTime"][:16].replace("T", " ")
            desc = period["parameter"]["parameterName"]
            _pop = pop[i]["parameter"]["parameterName"] if i < len(pop) else "?"
            _min = mint[i]["parameter"]["parameterName"] if i < len(mint) else "?"
            _max = maxt[i]["parameter"]["parameterName"] if i < len(maxt) else "?"
            result_lines.append(
                f"  {start}～{end}：{desc}，降雨機率 {_pop}%，{_min}～{_max}°C"
            )

        return {
            "success": True,
            "location": location_name,
            "source": "中央氣象署 CWA OpenData",
            "source_url": f"{_CWA_COUNTY_PAGE}?CID={_COUNTY_MAP.get(location_name, '')}",
            "forecast": "\n".join(result_lines),
            "raw_periods": len(result_lines),
        }
    except Exception as e:
        logger.warning("[RDG] CWA API error: %s", e)
        return {"success": False, "error": str(e)}


def _query_cwa_scrape(location_name: str) -> Dict[str, Any]:
    """Fallback：抓 CWA 縣市頁面（不需 API key）。"""
    try:
        county_id = _COUNTY_MAP.get(location_name)
        if not county_id:
            return {"success": False, "error": "unknown_location"}

        url = f"{_CWA_COUNTY_PAGE}?CID={county_id}"
        from skills.research.web_research import fetch_url_content
        result = fetch_url_content(url, max_length=6000)
        if not result.get("success"):
            return {"success": False, "error": result.get("error", "fetch_failed")}

        content = result.get("content", "")
        # 只要能取回 CWA 頁面，就不讓 LLM 合成，直接回「請看這裡」
        if len(content) > 100:
            return {
                "success": True,
                "location": location_name,
                "source": "中央氣象署 CWA（網頁資料，未解析）",
                "source_url": url,
                "forecast": f"已取得 CWA 網頁資料（{len(content)} 字元）。請直接查閱：{url}",
                "raw_content": content[:800],
                "scrape_only": True,  # 標記：只有 scrape，沒有 parsed data
            }
        return {"success": False, "error": "empty_content"}
    except Exception as e:
        logger.warning("[RDG] CWA scrape error: %s", e)
        return {"success": False, "error": str(e)}


def query_weather(query_text: str) -> Dict[str, Any]:
    """
    查詢台灣天氣。

    返回格式（成功）：
        {"success": True, "location": str, "source": str, "source_url": str,
         "forecast": str, "reply": str}

    返回格式（失敗）：
        {"success": False, "refusal": str}  ← 直接回給使用者的文字
    """
    location = _extract_location(query_text)
    if not location:
        # 無法辨識地點，還是拒絕猜測
        return {
            "success": False,
            "refusal": (
                "我無法辨識您要查詢的地點。"
                "請直接查閱中央氣象署網站：https://www.cwa.gov.tw/ "
                "或告訴我具體的縣市名稱（例如「臺東」、「台北」）。"
            ),
        }

    # 1. 先試 API
    api_result = _query_cwa_api(location)
    if api_result.get("success") and not api_result.get("scrape_only"):
        reply = (
            f"以下資料來自{api_result['source']}（{api_result['source_url']}）：\n"
            f"{location} 天氣預報：\n{api_result['forecast']}"
        )
        return {**api_result, "reply": reply}

    # 2. Scrape fallback
    scrape_result = _query_cwa_scrape(location)
    if scrape_result.get("success"):
        reply = (
            f"我從中央氣象署取得了 {location} 的頁面，但無法自動解析詳細數字。"
            f"請直接查閱：{scrape_result['source_url']}"
        )
        return {**scrape_result, "reply": reply}

    # 3. 明確拒絕（比猜測安全得多）
    county_id = _COUNTY_MAP.get(location, "")
    cwa_url = f"{_CWA_COUNTY_PAGE}?CID={county_id}" if county_id else "https://www.cwa.gov.tw/"
    return {
        "success": False,
        "location": location,
        "refusal": (
            f"我目前無法取得 {location} 的即時天氣資料。"
            f"請直接查閱中央氣象署（CWA）官網：{cwa_url}"
        ),
    }


# ---------------------------------------------------------------------------
# TWSE 股價查詢
# ---------------------------------------------------------------------------

def query_twse_stock(ticker_or_name: str) -> Dict[str, Any]:
    """查詢台股即時報價（TWSE 公開 API，不需 key）。"""
    # 簡易名稱→代號對照
    _COMMON = {"台積電": "2330", "鴻海": "2317", "聯發科": "2454",
               "台塑": "1301", "中鋼": "2002"}
    code = _COMMON.get(ticker_or_name, ticker_or_name.strip().upper())
    if not re.match(r"^\d{4,6}$", code):
        return {"success": False, "error": "cannot_resolve_ticker",
                "refusal": f"無法解析 {ticker_or_name} 的股票代碼。請查閱台灣證交所：https://www.twse.com.tw/"}
    try:
        import urllib.request
        url = f"{_TWSE_API}?ex_ch=tse_{code}.tw&json=1&delay=0&_={int(time.time()*1000)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("msgArray", [])
        if not items:
            return {"success": False, "error": "no_data",
                    "refusal": f"{code} 無報價資料（可能休市）。請查閱：https://www.twse.com.tw/"}
        item = items[0]
        price = item.get("z", "N/A")
        change = item.get("y", "N/A")  # yesterday close
        name = item.get("n", code)
        reply = f"{name}（{code}）最新成交價：{price} 元（昨收 {change} 元）\n資料來源：台灣證交所 TWSE"
        return {"success": True, "ticker": code, "name": name,
                "price": price, "prev_close": change,
                "source": "台灣證交所 TWSE", "reply": reply}
    except Exception as e:
        logger.warning("[RDG] TWSE error: %s", e)
        return {"success": False, "error": str(e),
                "refusal": f"無法查詢 {code} 報價。請直接查閱：https://www.twse.com.tw/"}


# ---------------------------------------------------------------------------
# 統一入口：handle_realtime_query
# ---------------------------------------------------------------------------

def handle_realtime_query(query_text: str) -> Optional[Dict[str, Any]]:
    """
    若查詢屬於即時資料類型，直接呼叫 authoritative API 並回傳結果。
    呼叫端應優先使用 result["reply"] 作為回覆文字，不讓 LLM 再合成。

    若非即時查詢或無法辨識，回傳 None（代表走正常 LLM 路徑）。
    """
    qtype = classify_realtime_query(query_text)
    if qtype is None:
        return None

    logger.info("[RDG] Real-time query detected: type=%s", qtype)

    if qtype == "weather":
        return query_weather(query_text)
    elif qtype == "stock":
        # 嘗試從查詢文字中抽出股票代碼/名稱
        for name in ["台積電", "鴻海", "聯發科", "台塑", "中鋼"]:
            if name in query_text:
                return query_twse_stock(name)
        m = re.search(r"\b(\d{4,6})\b", query_text)
        if m:
            return query_twse_stock(m.group(1))
        return {
            "success": False,
            "qtype": "stock",
            "refusal": "請提供股票代碼或公司名稱，例如「台積電（2330）」。查閱：https://www.twse.com.tw/",
        }
    elif qtype == "fx_rate":
        return {
            "success": False,
            "qtype": "fx_rate",
            "refusal": (
                "我目前沒有接入即時匯率 API。"
                "請查閱台灣銀行牌告匯率：https://rate.bot.com.tw/xrt "
                "或中央銀行：https://www.cbc.gov.tw/"
            ),
        }

    return None

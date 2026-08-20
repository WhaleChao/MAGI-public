"""
skills/engine/realtime_data_gateway.py
=======================================
即時資料閘道（Real-time Data Gateway）

設計原則
--------
1. **數字精確類（天氣/股價/匯率）**：必須來自 authoritative API；
   raw data 直接格式化後回傳，不讓 LLM 合成/四捨五入。
   無 API 時明確拒絕，不讓 LLM 合成。
2. **資訊整合類（評價/路線/評論/營業時間/新聞/商品比較）**：允許
   web_search → 抓內文 → LLM 整理摘要 + 引用來源（見 web_research_synthesize）。
   無外部來源時可降階回「我目前沒有這方面的即時資料，建議查 [URL]」，
   但這是 fallback 不是預設。
3. **不依賴 DuckDuckGo / ReAct 處理數字精確類**：天氣/股價等精確數字的
   精確度要求超過搜尋引擎能保證的。非數字類查詢則可以使用 web_research_synthesize。

支援類型
--------
- weather  → 中央氣象署（CWA）OpenData API 或網頁抓取
- stock    → 台灣證交所 TWSE 公開 API（不需 key）
- fx_rate  → 僅提示查詢網址（暫未接 API）
- current_time → 系統時鐘（Asia/Taipei）

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
from urllib.parse import urlencode

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
_CWA_LOCATION_NAMES: Dict[str, str] = {
    "臺北": "臺北市", "台北": "臺北市", "新北": "新北市", "板橋": "新北市",
    "基隆": "基隆市", "桃園": "桃園市", "新竹市": "新竹市", "新竹縣": "新竹縣",
    "新竹": "新竹縣", "苗栗": "苗栗縣", "臺中": "臺中市", "台中": "臺中市",
    "彰化": "彰化縣", "南投": "南投縣", "雲林": "雲林縣", "嘉義市": "嘉義市",
    "嘉義縣": "嘉義縣", "嘉義": "嘉義縣", "臺南": "臺南市", "台南": "臺南市",
    "高雄": "高雄市", "屏東": "屏東縣", "臺東": "臺東縣", "台東": "臺東縣",
    "花蓮": "花蓮縣", "宜蘭": "宜蘭縣", "澎湖": "澎湖縣", "金門": "金門縣",
    "連江": "連江縣",
}
_OPEN_METEO_LOCATION_NAMES: Dict[str, str] = {
    "臺北": "Taipei", "台北": "Taipei", "新北": "New Taipei", "板橋": "New Taipei",
    "基隆": "Keelung", "桃園": "Taoyuan", "新竹市": "Hsinchu City", "新竹縣": "Hsinchu County",
    "新竹": "Hsinchu", "苗栗": "Miaoli", "臺中": "Taichung", "台中": "Taichung",
    "彰化": "Changhua", "南投": "Nantou", "雲林": "Yunlin", "嘉義市": "Chiayi City",
    "嘉義縣": "Chiayi County", "嘉義": "Chiayi", "臺南": "Tainan", "台南": "Tainan",
    "高雄": "Kaohsiung", "屏東": "Pingtung", "臺東": "Taitung", "台東": "Taitung",
    "花蓮": "Hualien", "宜蘭": "Yilan", "澎湖": "Penghu", "金門": "Kinmen",
    "連江": "Lienchiang",
}

# CWA OpenData 36小時天氣預報 API
_CWA_API_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001"
_CWA_COUNTY_PAGE = "https://www.cwa.gov.tw/V8/C/W/County/County.html"

# TWSE 即時報價
_TWSE_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
_OPEN_METEO_GEOCODING_API = "https://geocoding-api.open-meteo.com/v1/search"
_OPEN_METEO_FORECAST_API = "https://api.open-meteo.com/v1/forecast"


# ---------------------------------------------------------------------------
# 查詢類型分類
# ---------------------------------------------------------------------------

_WEATHER_KEYWORDS = [
    "天氣", "氣溫", "溫度", "下雨", "下雪", "颱風", "降雨", "天晴",
    "陰天", "晴天", "氣象", "預報", "降雪", "豪雨", "颳風", "大風",
    "濕度", "weather", "forecast", "會下", "會不會下",
    "幾度", "多熱", "多冷", "熱不熱", "冷不冷", "悶不悶", "體感",
]
_STOCK_KEYWORDS = ["股價", "股票", "台積電", "鴻海", "大盤", "加權指數", "台股", "個股",
                   "TWSE", "TSE", "元/股"]
_FX_KEYWORDS = ["匯率", "美金", "美元", "日圓", "日幣", "歐元", "人民幣", "港幣", "外幣",
                "exchange rate", "forex"]

# 提醒/行程/會議類查詢的負面條件：命中這些關鍵字時不走 weather，
# 即使 message 含「明天」等時間詞也不應誤判為天氣查詢。
_WEATHER_NEGATIVE = re.compile(
    r"提醒|記事|行程|日曆|開會|會議|開庭|庭期|法庭|排庭|期限|待辦|事項|備忘|"
    r"改到|改成|延後|提前|取消|memo|remind|schedule|calendar",
    re.IGNORECASE,
)
_STOCK_MANAGEMENT_NEGATIVE = re.compile(
    r"追蹤|清單|watchlist|晨報|預測|技術分析|macd|rsi|布林|新增|設定|移除|取消",
    re.IGNORECASE,
)
_REALTIME_CAPABILITY_QUESTION = re.compile(
    r"(?:你|magi|MAGI).{0,8}(?:會|可以|能不能|能否|可否|能).{0,24}"
    r"(?:天氣|氣象|股票|股價|追蹤股票|匯率)",
    re.IGNORECASE,
)
_REALTIME_ACTION_VERB = re.compile(
    r"(?:查一下|查詢|幫我查|幫忙查|幫.{0,6}查|看一下|看看|告訴我|請.{0,6}查|麻煩.{0,6}查|"
    r"lookup|search|get)",
    re.IGNORECASE,
)
_REALTIME_TIME_CUES = (
    "現在", "目前", "今天", "明天", "後天", "今晚", "早上", "上午", "中午", "下午", "晚上",
    "today", "tomorrow", "now",
)
_CURRENT_TIME_QUERY = re.compile(
    r"(?:現在|目前|此刻).{0,10}(?:幾點|日期|時間|幾月幾日|上午還下午)|"
    r"(?:幾點|日期|時間|幾月幾日|上午還下午).{0,10}(?:現在|目前|此刻)",
    re.IGNORECASE,
)
_REALTIME_NEGATION = re.compile(
    r"(?:不要|不用|不必|毋須|無須|沒有|沒在|不是).{0,12}"
    r"(?:查|問|告訴|提供|顯示|使用工具)?.{0,12}"
    r"(?:天氣|氣象|氣溫|溫度|下雨|下雪|颱風|股價|股票|匯率|外幣|幾點|現在時間)|"
    r"(?:天氣|氣象|股價|股票|匯率).{0,12}(?:不用查|不必查|不要查|不用查了|取消)",
    re.IGNORECASE,
)
_REALTIME_TRANSFORM_OR_META = re.compile(
    r"(?:翻譯|翻成|譯成|摘要(?:這句|這段|下列|以下)?|改寫|校對|"
    r"這個詞|這句話|疑問句|是什麼意思|API|模組|報錯|錯誤|故障)|"
    r"(?:解釋|說明|為什麼|為何).{0,20}(?:形成|原理|機制|制度|差別|定義|概念)|"
    r"(?:形成|原理|機制|制度|差別|定義|概念).{0,20}(?:解釋|說明|是什麼|為什麼|為何)|"
    r"(?:目前|現在|此刻)?\s*時間管理",
    re.IGNORECASE,
)
_REALTIME_HYPOTHETICAL = re.compile(r"(?:如果|假設|假如|倘若|設若).{0,40}(?:天氣|下雨|下雪|颱風|股價|股票|匯率|美元|日圓|外幣)", re.IGNORECASE)
_HISTORICAL_WEATHER = re.compile(
    r"(?:去年|前年|過去|歷史|紀錄|記錄|20\d{2}\s*年|民國\s*\d{2,3}\s*年).{0,30}"
    r"(?:天氣|氣溫|溫度|降雨|下雨|下雪|颱風)|"
    r"(?:天氣|氣溫|溫度|降雨|下雨|下雪|颱風).{0,30}"
    r"(?:去年|前年|過去|歷史|紀錄|記錄|20\d{2}\s*年|民國\s*\d{2,3}\s*年)",
    re.IGNORECASE,
)
_STOCK_QUOTE_CUES = re.compile(r"(?:目前|現在|今天|即時|多少|多少錢|一股|股價|股票價格|成交價|成交價格|報價|收盤|開盤|指數)", re.IGNORECASE)
_FX_QUOTE_CUES = re.compile(r"(?:目前|現在|今天|即時|多少|換多少|兌換|買入|賣出|報價|USD/TWD|TWD/USD)", re.IGNORECASE)


def _looks_like_realtime_action_request(text: str) -> bool:
    """區分「你可以查天氣嗎」能力詢問與「你能查一下明天台北天氣嗎」實際查詢。"""
    raw = str(text or "")
    lowered = raw.lower()
    has_realtime_topic = (
        any(k in lowered for k in _WEATHER_KEYWORDS)
        or _has_stock_topic(lowered)
        or _has_fx_topic(lowered)
    )
    if not has_realtime_topic:
        return False
    if _REALTIME_ACTION_VERB.search(raw):
        return True
    if any(cue in lowered for cue in _REALTIME_TIME_CUES):
        return True
    return any(loc in raw for loc in _COUNTY_MAP)


def _has_stock_topic(lowered: str) -> bool:
    return bool(
        any(k.lower() in lowered for k in _STOCK_KEYWORDS)
        or re.search(r"(?<!\d)\d{4,6}(?!\d).{0,10}(?:漲|跌|成交|股價)|(?:漲|跌|成交|股價).{0,10}(?<!\d)\d{4,6}(?!\d)", lowered)
    )


def _has_fx_topic(lowered: str) -> bool:
    return any(k.lower() in lowered for k in _FX_KEYWORDS)


def _is_non_realtime_lookup_context(text: str) -> bool:
    """Reject quoted, negated, hypothetical, historical and diagnostic uses.

    Keyword presence alone is not intent.  These contexts describe or transform
    a real-time question; they do not ask MAGI to perform that question.
    """
    raw = str(text or "")
    return bool(
        _REALTIME_NEGATION.search(raw)
        or _REALTIME_TRANSFORM_OR_META.search(raw)
        or _REALTIME_HYPOTHETICAL.search(raw)
        or _HISTORICAL_WEATHER.search(raw)
    )


def classify_realtime_query(text: str) -> Optional[str]:
    """
    回傳即時資料類型 ("weather" / "stock" / "fx_rate" / "current_time") 或 None。

    注意：若 message 含提醒/行程/會議類詞彙，即使有時間詞（「明天」）
    也不走 weather，避免提醒查詢誤進天氣路徑。
    """
    raw = str(text or "")
    lowered = raw.lower()
    if _is_non_realtime_lookup_context(raw):
        return None
    if _CURRENT_TIME_QUERY.search(raw):
        return "current_time"
    if _REALTIME_CAPABILITY_QUESTION.search(raw) and not _looks_like_realtime_action_request(raw):
        return None
    if any(k in lowered for k in _WEATHER_KEYWORDS):
        # 負面條件：提醒/行程類 → 不走 weather
        if _WEATHER_NEGATIVE.search(raw):
            pass  # fall through to other checks or return None
        elif _looks_like_realtime_action_request(raw):
            return "weather"
    if _has_stock_topic(lowered):
        if _STOCK_MANAGEMENT_NEGATIVE.search(raw):
            return None
        if _looks_like_realtime_action_request(raw) or _STOCK_QUOTE_CUES.search(raw):
            return "stock"
    if _has_fx_topic(lowered) and (
        _looks_like_realtime_action_request(raw) or _FX_QUOTE_CUES.search(raw)
    ):
        return "fx_rate"
    return None


def detect_realtime_topics(text: str) -> set[str]:
    """Detect real-time topics without deciding whether to intercept the whole request.

    ``classify_realtime_query`` deliberately leaves compound office requests to
    the agent.  The agent still needs to prove that it used the right source,
    so this topic-only detector does not apply the calendar/business negative
    filters.  A time word such as ``明天`` is never a weather topic by itself.
    """
    raw = str(text or "")
    lowered = raw.lower()
    topics: set[str] = set()
    if _is_non_realtime_lookup_context(raw):
        return topics
    if (
        any(k in lowered for k in _WEATHER_KEYWORDS)
        and _looks_like_realtime_action_request(raw)
    ):
        topics.add("weather")
    if _has_stock_topic(lowered) and (
        _looks_like_realtime_action_request(raw) or _STOCK_QUOTE_CUES.search(raw)
    ):
        topics.add("stock")
    if _has_fx_topic(lowered) and (
        _looks_like_realtime_action_request(raw) or _FX_QUOTE_CUES.search(raw)
    ):
        topics.add("fx_rate")
    if _CURRENT_TIME_QUERY.search(raw):
        topics.add("current_time")
    return topics


def _extract_location(text: str) -> Optional[str]:
    """從查詢文字中抽取縣市地點。"""
    for loc in sorted(_COUNTY_MAP.keys(), key=len, reverse=True):
        if loc in text:
            return loc
    return None


def _extract_global_location(text: str) -> Optional[str]:
    """Extract an explicit non-Taiwan place without guessing from context."""
    raw = str(text or "").strip()
    patterns = (
        r"(?:請|麻煩|幫我|可以|能不能|能否)?(?:查一下|查詢|查|看看|看一下|告訴我)?\s*"
        r"(?P<place>[A-Za-z\u3400-\u9fff·.\-\s]{2,40}?)"
        r"(?:今天|明天|後天|今晚|現在|目前)?(?:的)?(?:天氣|氣溫|溫度|幾度|會不會下雨|會下雨)",
        r"(?:weather|forecast)\s+(?:in|for)\s+(?P<place>[A-Za-z\u3400-\u9fff·.\-\s]{2,40})",
        r"(?P<place>[A-Za-z\u3400-\u9fff·.\-\s]{2,40})\s+(?:weather|forecast)",
    )
    for pattern in patterns:
        match = re.search(pattern, raw, re.IGNORECASE)
        if not match:
            continue
        place = re.sub(
            r"^(?:今天|明天|後天|今晚|現在|目前|想知道|請問|我想知道)+|"
            r"(?:如何|怎樣|怎麼樣|好嗎|嗎|呢|？|\?)+$",
            "",
            match.group("place").strip(),
        ).strip(" ，,。")
        if 2 <= len(place) <= 40:
            return place
    return None


_WMO_WEATHER = {
    0: "晴朗", 1: "大致晴朗", 2: "局部多雲", 3: "陰天",
    45: "霧", 48: "霧淞", 51: "小毛毛雨", 53: "毛毛雨", 55: "較強毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 71: "小雪", 73: "中雪", 75: "大雪",
    80: "短暫小雨", 81: "短暫中雨", 82: "強陣雨", 85: "短暫小雪", 86: "強陣雪",
    95: "雷雨", 96: "雷雨伴小冰雹", 99: "雷雨伴大冰雹",
}


def _query_open_meteo(location_name: str, query_text: str) -> Dict[str, Any]:
    """Query global weather through Open-Meteo's geocoder and forecast APIs."""
    try:
        import urllib.request

        geo_url = _OPEN_METEO_GEOCODING_API + "?" + urlencode(
            {"name": location_name, "count": 3, "language": "zh", "format": "json"}
        )
        with urllib.request.urlopen(urllib.request.Request(geo_url, headers={"Accept": "application/json"}), timeout=8) as response:
            geocoded = json.loads(response.read().decode("utf-8"))
        candidates = list(geocoded.get("results") or [])
        if not candidates:
            return {"success": False, "error": "location_not_found"}

        chosen = candidates[0]
        latitude = chosen.get("latitude")
        longitude = chosen.get("longitude")
        if latitude is None or longitude is None:
            return {"success": False, "error": "coordinates_missing"}
        forecast_url = _OPEN_METEO_FORECAST_API + "?" + urlencode(
            {
                "latitude": latitude,
                "longitude": longitude,
                "current": "temperature_2m,apparent_temperature,precipitation,weather_code",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "auto",
                "forecast_days": 3,
            }
        )
        with urllib.request.urlopen(urllib.request.Request(forecast_url, headers={"Accept": "application/json"}), timeout=8) as response:
            forecast = json.loads(response.read().decode("utf-8"))

        daily = forecast.get("daily") or {}
        index = 2 if "後天" in query_text else 1 if "明天" in query_text or "tomorrow" in query_text.lower() else 0
        dates = list(daily.get("time") or [])
        if index >= len(dates):
            return {"success": False, "error": "forecast_period_missing"}
        code = int((daily.get("weather_code") or [0])[index])
        high = (daily.get("temperature_2m_max") or [None])[index]
        low = (daily.get("temperature_2m_min") or [None])[index]
        rain = (daily.get("precipitation_probability_max") or [None])[index]
        place_parts: list[str] = []
        # Geocoder administrative names may be returned in a non-Taiwan
        # Chinese variant even with language=zh.  City + country is enough to
        # identify the source location and avoids leaking inconsistent wording.
        for item in (chosen.get("name"), chosen.get("country")):
            value = str(item or "").strip()
            for source, target in (
                ("台湾", "臺灣"), ("台北", "臺北"), ("台中", "臺中"),
                ("台南", "臺南"), ("台东", "臺東"),
            ):
                value = value.replace(source, target)
            if value and value not in place_parts:
                place_parts.append(value)
        place = "、".join(place_parts)
        period = "後天" if index == 2 else "明天" if index == 1 else "今天"
        summary = f"{period}（{dates[index]}）：{_WMO_WEATHER.get(code, f'天氣代碼 {code}')}，{low}～{high}°C"
        if rain is not None:
            summary += f"，最高降雨機率 {rain}%"
        current = forecast.get("current") or {}
        current_line = ""
        if index == 0 and current.get("temperature_2m") is not None:
            current_line = (
                f"\n目前 {current.get('temperature_2m')}°C（體感 {current.get('apparent_temperature')}°C），"
                f"降水 {current.get('precipitation')} mm。"
            )
        return {
            "success": True,
            "location": place,
            "source": "Open-Meteo Weather Forecast API",
            "source_url": forecast_url,
            "reply": f"以下是 {place} 的權威即時預報資料：\n{summary}{current_line}\n資料來源：Open-Meteo（{forecast_url}）",
        }
    except Exception as exc:
        logger.warning("[RDG] Open-Meteo error: %s", exc)
        return {"success": False, "error": type(exc).__name__}


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
        official_name = _CWA_LOCATION_NAMES.get(location_name, location_name)
        url = _CWA_API_URL + "?" + urlencode(
            {
                "Authorization": api_key,
                "locationName": official_name,
                "elementName": "Wx,PoP,MinT,MaxT",
            }
        )
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
    """Fallback：直接抓 CWA 公開 JS 資料端點（不需 API key、含結構化溫度/天氣描述）。

    CWA 縣市頁面（County.html）的溫度與天氣是 JS 動態載入，純 HTML 抓不到數字；
    但 CWA 同時把資料以 `Data/js/3hr/ChartData_3hr_T_<CID>.js` 形式公開（前端 SPA 拉取的來源）。
    格式為 `var TempArray_3hr = { '<station_id>': { C: { T:[...], AT:[...] }, Wx: { C: [[code,desc],...] } } };`
    """
    try:
        county_id = _COUNTY_MAP.get(location_name)
        if not county_id:
            return {"success": False, "error": "unknown_location"}

        import requests, re, urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        url = f"https://www.cwa.gov.tw/Data/js/3hr/ChartData_3hr_T_{county_id}.js"
        resp = requests.get(
            url, timeout=8, verify=False,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; MAGI/2.0)",
                "Referer": "https://www.cwa.gov.tw/",
            },
        )
        resp.raise_for_status()
        content = resp.text

        # 解析 var TempArray_3hr = {...}，抓第一個測站的當下值
        # 該檔以時間順序 24+ 小時逐時刻儲存，index 0 = 當下時刻（從檔首 Updated 時間開始）
        m = re.search(r"TempArray_3hr\s*=\s*\{\s*'(\d+)'\s*:\s*\{\s*'C'\s*:\s*\{\s*'T'\s*:\s*\[([\d,\-\.]+)\][^}]*'AT'\s*:\s*\[([\d,\-\.]+)\][^}]*\},\s*'F'\s*:\s*\{[^}]+\},\s*'Wx'\s*:\s*\{\s*'C'\s*:\s*\[(\[[^\]]+\](?:,\[[^\]]+\])*)\]", content, re.S)
        updated_m = re.search(r"Updated:\s*([\d/:\s]+)", content)
        updated = updated_m.group(1).strip() if updated_m else ""

        if m:
            temps = [int(x) for x in m.group(2).split(",")]
            ats = [int(x) for x in m.group(3).split(",")]
            wx_raw = m.group(4)
            wx_pairs = re.findall(r"\['(\d+)','([^']+)'\]", wx_raw)
            now_t = temps[0] if temps else None
            now_at = ats[0] if ats else None
            now_wx = wx_pairs[0][1] if wx_pairs else ""
            # 收集未來 12h 摘要（每 3h 一筆，間隔比 hourly 適合一般使用者）
            forecast_hours = []
            for i in range(0, min(13, len(temps)), 3):
                w = wx_pairs[i][1] if i < len(wx_pairs) else ""
                forecast_hours.append(f"+{i}h: {temps[i]}°C ({w})")
            forecast = (
                f"當下：{now_wx}，氣溫 {now_t}°C（體感 {now_at}°C）\n"
                f"未來 12 小時：" + " / ".join(forecast_hours)
            )
            return {
                "success": True,
                "location": location_name,
                "source": f"中央氣象署 CWA（公開 JS 資料 / 更新時間 {updated}）",
                "source_url": f"{_CWA_COUNTY_PAGE}?CID={county_id}",
                "forecast": forecast,
                "raw_periods": len(forecast_hours),
                "now_temp": now_t,
                "now_apparent_temp": now_at,
                "now_weather": now_wx,
            }

        # 解析失敗（CWA 改格式）：明確回失敗讓上層走降級流程
        county_url = f"{_CWA_COUNTY_PAGE}?CID={county_id}"
        return {
            "success": False,
            "error": "parse_failed",
            "location": location_name,
            "source_url": county_url,
            "raw_size": len(content),
        }
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
        global_location = _extract_global_location(query_text)
        if global_location:
            global_result = _query_open_meteo(global_location, query_text)
            if global_result.get("success"):
                return global_result
            return {
                "success": False,
                "location": global_location,
                "refusal": f"我目前無法從權威天氣來源核對 {global_location} 的預報，因此不會猜測。請稍後再試。",
            }
        return {
            "success": False,
            "refusal": (
                "你想查哪個地點的天氣？請告訴我城市或縣市，例如「臺東」或「東京」。"
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

    # The no-key CWA JS endpoint only covers the current/next 12 hours.  It
    # must not be relabelled as tomorrow or the day after tomorrow; use the
    # global forecast API for those explicit forecast periods instead.
    if any(cue in query_text.lower() for cue in ("明天", "後天", "tomorrow")):
        period_result = _query_open_meteo(_OPEN_METEO_LOCATION_NAMES.get(location, location), query_text)
        if period_result.get("success"):
            return period_result

    # 2. Scrape fallback（已升級為解析公開 JS 端點，會回真實溫度）
    scrape_result = _query_cwa_scrape(location)
    if scrape_result.get("success"):
        reply = (
            f"以下資料來自{scrape_result['source']}：\n"
            f"{location}\n{scrape_result['forecast']}\n"
            f"（完整頁面：{scrape_result['source_url']}）"
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
    elif qtype == "current_time":
        from datetime import datetime
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo("Asia/Taipei"))
        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        return {
            "success": True,
            "qtype": "current_time",
            "source": "MAGI 主機系統時鐘（Asia/Taipei）",
            "reply": f"現在是臺灣時間 {now:%Y-%m-%d} 星期{weekdays[now.weekday()]} {now:%H:%M:%S}。",
        }

    return None

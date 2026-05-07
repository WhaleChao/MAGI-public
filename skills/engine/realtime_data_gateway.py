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
    "幾度", "多熱", "多冷", "熱不熱", "冷不冷", "悶不悶", "體感",
]
_STOCK_KEYWORDS = ["股價", "股票", "台積電", "鴻海", "大盤", "加權", "漲", "跌", "點數",
                   "上市", "上櫃", "TWSE", "TSE", "股", "元/股"]
_FX_KEYWORDS = ["匯率", "美金", "日圓", "歐元", "人民幣", "港幣", "換算", "外幣",
                "exchange rate", "forex"]

# 提醒/行程/會議類查詢的負面條件：命中這些關鍵字時不走 weather，
# 即使 message 含「明天」等時間詞也不應誤判為天氣查詢。
_WEATHER_NEGATIVE = re.compile(
    r"提醒|記事|行程|開會|會議|事項|備忘|memo|remind|schedule",
    re.IGNORECASE,
)


def classify_realtime_query(text: str) -> Optional[str]:
    """
    回傳即時資料類型 ("weather" / "stock" / "fx_rate") 或 None（非即時查詢）。

    注意：若 message 含提醒/行程/會議類詞彙，即使有時間詞（「明天」）
    也不走 weather，避免提醒查詢誤進天氣路徑。
    """
    lowered = (text or "").lower()
    if any(k in lowered for k in _WEATHER_KEYWORDS):
        # 負面條件：提醒/行程類 → 不走 weather
        if _WEATHER_NEGATIVE.search(text):
            pass  # fall through to other checks or return None
        else:
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

    return None

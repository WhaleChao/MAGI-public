# -*- coding: utf-8 -*-
"""
worldmonitor-intel — MAGI 全球情報監控技能

直接從公開 RSS/API 收集全球情報，用 Melchior 分析，存入 MAGI 記憶。
不依賴 worldmonitor edge functions (無需 Vercel 帳號)。

Architecture:
    Public RSS + Finnhub API
        ↓ fetch
    action.py (本模組)
        ↓ reasoning
    Melchior (Ollama)
        ↓ memory
    MAGI mem_bridge / local file
"""

import os
import sys
import json
import csv
import argparse
import logging
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import ensure_orch_on_sys_path, get_magi_root_dir

MAGI_DIR = str(get_magi_root_dir())
ensure_orch_on_sys_path()

logger = logging.getLogger("worldmonitor-intel")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY", "")
if not FINNHUB_KEY:
    logger.info("FINNHUB_API_KEY 未設定，將改用 MAGI 免金鑰公開行情來源。")
FETCH_TIMEOUT = 15
SUMMARY_TRANSLATION_ENABLED = os.environ.get("MAGI_WORLDMONITOR_TRANSLATE_SUMMARY", "1").lower() not in {"0", "false", "no"}
_SUMMARY_TRANSLATION_CACHE: Dict[str, str] = {}

# ---------------------------------------------------------------------------
# Public news RSS feeds (no API key needed)
# ---------------------------------------------------------------------------
RSS_FEEDS = {
    "BBC World":       "https://feeds.bbci.co.uk/news/world/rss.xml",
    "NHK Asia":        "https://www3.nhk.or.jp/rss/news/cat6.xml",
    "Al Jazeera":      "https://www.aljazeera.com/xml/rss/all.xml",
    "Guardian World":  "https://www.theguardian.com/world/rss",
    "DW News":         "https://rss.dw.com/xml/rss-en-all",
    "NYTimes World":   "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "NPR World":       "https://feeds.npr.org/1004/rss.xml",
}

FINNHUB_MARKET_SYMBOLS = ["AAPL", "TSMC", "NVDA", "SPY", "^GSPC"]
STOOQ_MARKET_SYMBOLS = {
    "AAPL": "aapl.us",
    "TSMC": "tsm.us",
    "NVDA": "nvda.us",
    "SPY": "spy.us",
    "SPX": "^spx",
}

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _fetch(url: str, timeout: int = FETCH_TIMEOUT) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "MAGI-worldmonitor-intel/2.0",
            "Accept": "application/rss+xml, application/xml, application/json, text/xml, */*"
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        logger.warning(f"Fetch failed {url}: {e}")
        return None

def _fetch_json(url: str) -> Optional[Dict]:
    data = _fetch(url)
    if data:
        try:
            return json.loads(data.decode("utf-8"))
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 79, exc_info=True)
    return None


def _extract_model_labels(payload) -> List[str]:
    """Normalize oMLX /v1/models payloads across old and new schemas."""
    labels: List[str] = []
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = payload.get("data")
        if not isinstance(items, list):
            items = payload.get("models")
        if not isinstance(items, list):
            items = []
    else:
        items = []

    for item in items:
        if isinstance(item, str):
            label = item.strip()
        elif isinstance(item, dict):
            label = (
                str(item.get("id") or "").strip()
                or str(item.get("name") or "").strip()
                or str(item.get("model") or "").strip()
            )
        else:
            label = str(item or "").strip()
        if label and label not in labels:
            labels.append(label)
    return labels

# ---------------------------------------------------------------------------
# RSS parsing
# ---------------------------------------------------------------------------
def _parse_rss(raw: bytes, max_items: int = 8) -> List[Dict]:
    items = []
    try:
        root = ET.fromstring(raw)
        for item in root.iter("item"):
            title = (item.findtext("title") or "").strip()
            desc = (item.findtext("description") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub = (item.findtext("pubDate") or "").strip()
            if title:
                # Strip HTML tags from description
                import re
                desc_clean = re.sub(r"<[^>]+>", "", desc)[:300]
                items.append({"title": title, "summary": desc_clean, "link": link, "date": pub})
            if len(items) >= max_items:
                break
    except Exception as e:
        logger.warning(f"RSS parse error: {e}")
    return items

# ---------------------------------------------------------------------------
# Data collectors
# ---------------------------------------------------------------------------
def collect_news() -> tuple[List[Dict], List[Dict]]:
    """Collect news from public RSS feeds and track per-source health."""
    all_news = []
    source_statuses = []
    for name, url in RSS_FEEDS.items():
        logger.info(f"📰 Fetching {name}...")
        raw = _fetch(url)
        if raw:
            items = _parse_rss(raw, max_items=5)
            for item in items:
                item["source"] = name
            all_news.extend(items)
            source_statuses.append({
                "source": name,
                "url": url,
                "ok": True,
                "count": len(items),
                "error": "",
            })
            logger.info(f"  ✓ {len(items)} articles from {name}")
        else:
            source_statuses.append({
                "source": name,
                "url": url,
                "ok": False,
                "count": 0,
                "error": "fetch failed",
            })
            logger.warning(f"  ✗ Failed to fetch {name}")
    return all_news, source_statuses

def collect_markets() -> tuple[Dict, Dict]:
    """Collect market data from Finnhub with a light-weight health summary."""
    status = {
        "ok": False,
        "provider": "Finnhub" if FINNHUB_KEY else "MAGI public market feed",
        "quotes_ok": 0,
        "quotes_total": len(FINNHUB_MARKET_SYMBOLS if FINNHUB_KEY else STOOQ_MARKET_SYMBOLS),
        "news_ok": 0,
        "detail": "",
    }
    if not FINNHUB_KEY:
        return _collect_stooq_markets(status)
    
    market_data = {}
    quotes_ok = 0
    for symbol in FINNHUB_MARKET_SYMBOLS:
        url = f"https://finnhub.io/api/v1/quote?symbol={symbol}&token={FINNHUB_KEY}"
        data = _fetch_json(url)
        if data and data.get("c"):
            market_data[symbol] = {
                "price": data.get("c", 0),
                "change": data.get("d", 0),
                "change_pct": data.get("dp", 0),
                "high": data.get("h", 0),
                "low": data.get("l", 0),
            }
            quotes_ok += 1
            logger.info(f"  📊 {symbol}: ${data.get('c', 0):.2f} ({data.get('dp', 0):+.2f}%)")
    
    # Market news
    url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
    news = _fetch_json(url)
    if news and isinstance(news, list):
        market_data["_news"] = [
            {
                "title": n.get("headline", ""),
                "source": n.get("source", ""),
                "summary": n.get("summary", "")[:200],
                "url": n.get("url", ""),
            }
            for n in news[:5]
        ]
        status["news_ok"] = len(market_data["_news"])
        logger.info(f"  📊 {len(market_data['_news'])} market news items")
    status["quotes_ok"] = quotes_ok
    status["ok"] = quotes_ok > 0 or status["news_ok"] > 0
    status["detail"] = f"{quotes_ok}/{len(FINNHUB_MARKET_SYMBOLS)} 檔行情成功"
    if status["news_ok"]:
        status["detail"] += f"，{status['news_ok']} 則市場新聞"
    elif not status["ok"]:
        logger.warning("Finnhub 未取得行情，改用 MAGI 免金鑰公開行情來源")
        fallback_status = {
            "ok": False,
            "provider": "MAGI public market feed",
            "quotes_ok": 0,
            "quotes_total": len(STOOQ_MARKET_SYMBOLS),
            "news_ok": 0,
            "detail": "",
        }
        return _collect_stooq_markets(fallback_status)
    return market_data, status


def _collect_stooq_markets(status: Dict) -> tuple[Dict, Dict]:
    """Collect no-key public delayed quotes as MAGI's fallback market source."""
    import urllib.parse

    market_data: Dict = {}
    quotes_ok = 0
    for display_symbol, stooq_symbol in STOOQ_MARKET_SYMBOLS.items():
        query_symbol = urllib.parse.quote(stooq_symbol, safe="")
        url = f"https://stooq.com/q/l/?s={query_symbol}&f=sd2t2ohlcv&h&e=csv"
        raw = _fetch(url)
        if not raw:
            continue
        try:
            rows = list(csv.DictReader(raw.decode("utf-8", errors="replace").splitlines()))
        except Exception:
            rows = []
        if not rows:
            continue
        row = rows[0]
        close_raw = str(row.get("Close") or "").strip()
        open_raw = str(row.get("Open") or "").strip()
        if close_raw in {"", "N/D"}:
            continue
        try:
            price = float(close_raw)
            open_price = float(open_raw) if open_raw not in {"", "N/D"} else 0.0
            high = float(row.get("High") or 0)
            low = float(row.get("Low") or 0)
        except Exception:
            continue
        change = price - open_price if open_price else 0.0
        change_pct = (change / open_price * 100.0) if open_price else 0.0
        market_data[display_symbol] = {
            "price": price,
            "change": change,
            "change_pct": change_pct,
            "high": high,
            "low": low,
            "date": row.get("Date") or "",
            "time": row.get("Time") or "",
            "provider": "Stooq delayed quote",
        }
        quotes_ok += 1
        logger.info(f"  📊 {display_symbol}: {price:.2f} ({change_pct:+.2f}%) via Stooq")

    status["quotes_ok"] = quotes_ok
    status["quotes_total"] = len(STOOQ_MARKET_SYMBOLS)
    status["ok"] = quotes_ok > 0
    if quotes_ok:
        status["detail"] = f"MAGI 免金鑰公開行情來源：{quotes_ok}/{len(STOOQ_MARKET_SYMBOLS)} 檔成功"
    else:
        status["detail"] = "MAGI 免金鑰公開行情來源未取得行情"
    return market_data, status

# ---------------------------------------------------------------------------
# Melchior reasoning
# ---------------------------------------------------------------------------
def _reason_with_melchior(prompt: str, max_tokens: int = 2048) -> str:
    try:
        import urllib.request
        try:
            from api.routing.service_registry import get_service_url as _gsurl
            _omlx_def = _gsurl("omlx_inference")
        except Exception:
            _omlx_def = "http://127.0.0.1:8080"
        omlx_url = (os.environ.get("OMLX_URL") or os.environ.get("OLLAMA_URL") or _omlx_def).rstrip("/")
        model = (os.environ.get("MELCHIOR_MODEL") or os.environ.get("MAGI_TEXT_PRIMARY_MODEL") or "").strip()
        if not model:
            try:
                from api.model_config import TEXT_PRIMARY_MODEL as _tpm
                model = _tpm or "gemma-4-e4b-it-4bit"
            except Exception:
                model = "gemma-4-e4b-it-4bit"

        payload = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": "你是 MAGI 系統的情報分析員 Melchior。請用繁體中文分析全球情報，提供簡潔摘要和洞察。特別標註與台灣、亞太相關的內容。"},
                {"role": "user", "content": prompt}
            ],
            "stream": False,
            "temperature": 0.3,
            "max_tokens": max_tokens,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{omlx_url}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            choices = result.get("choices") or []
            return ((choices[0].get("message") or {}).get("content") or "") if choices else ""
    except Exception as e:
        logger.error(f"Melchior reasoning failed: {e}")
        return f"[推理失敗] {e}"

# ---------------------------------------------------------------------------
# Memory storage
# ---------------------------------------------------------------------------
def _store_to_memory(content: str, metadata: Dict = None):
    # Always save to file first so /intel page has fresh content
    report_dir = os.path.join(MAGI_DIR, "static", "worldmonitor_reports")
    os.makedirs(report_dir, exist_ok=True)
    filepath = os.path.join(report_dir, f"intel_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        logger.info(f"📁 Saved report to {filepath}")
        if metadata:
            sidecar = {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "news_items": metadata.get("news_items") or [],
                "news_statuses": metadata.get("news_statuses") or [],
                "market_status": metadata.get("market_status") or {},
                "market_news": metadata.get("market_news") or [],
                "market_symbols": metadata.get("market_symbols") or [],
            }
            try:
                with open(filepath[:-3] + ".json", "w", encoding="utf-8") as f:
                    json.dump(sidecar, f, ensure_ascii=False, indent=2)
            except Exception as exc:
                logger.warning(f"Sidecar save failed: {exc}")
        # Prune old reports — keep latest 30
        try:
            existing = sorted(
                [e for e in os.scandir(report_dir) if e.is_file() and e.name.endswith(".md")],
                key=lambda e: e.name, reverse=True
            )
            for old in existing[30:]:
                os.unlink(old.path)
                json_sidecar = old.path[:-3] + ".json"
                if os.path.exists(json_sidecar):
                    os.unlink(json_sidecar)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"File save failed: {e}")

    # Also store in MAGI vector memory
    try:
        from skills.memory import mem_bridge
        memory_content = content
        if len(memory_content) > 4800:
            head, _, _tail = memory_content.partition("\n---\n<details>")
            memory_content = head.strip()[:4800]
        source_bits = ["worldmonitor-intel"]
        if metadata:
            news_count = metadata.get("news_count")
            if news_count is not None:
                source_bits.append(f"news={news_count}")
            market_symbols = metadata.get("market_symbols") or []
            source_bits.append(f"markets={len(market_symbols)}")
        mem_bridge.remember(memory_content, source="|".join(source_bits))
        logger.info(f"✅ Stored to MAGI memory ({len(memory_content)} chars)")
    except Exception as e:
        logger.warning(f"Memory bridge failed: {e}")
    return True


def _render_source_health(news_statuses: List[Dict], market_status: Dict) -> str:
    lines = ["## 🩺 來源健康狀態"]
    total_sources = len(news_statuses)
    healthy_sources = sum(1 for item in news_statuses if item.get("ok"))
    lines.append(f"- 新聞來源：{healthy_sources}/{total_sources} 成功")
    for item in news_statuses:
        state = "OK" if item.get("ok") else "FAIL"
        detail = f"{item.get('count', 0)} 篇" if item.get("ok") else item.get("error") or "fetch failed"
        lines.append(f"- {item.get('source', 'unknown')}: {state} ({detail})")
    lines.append(
        f"- 市場資料：{'OK' if market_status.get('ok') else 'DEGRADED'} ({market_status.get('detail') or '未提供'})"
    )
    return "\n".join(lines)


def _contains_latin_or_kana(text: str) -> bool:
    return bool(re.search(r"[A-Za-z\u3040-\u30ff]", text or ""))


def _translate_to_zh_hant(text: str, timeout_sec: int = 8) -> str:
    """Translate a short source-grounded snippet to Traditional Chinese."""
    source = re.sub(r"\s+", " ", str(text or "")).strip()
    if not source or not SUMMARY_TRANSLATION_ENABLED or not _contains_latin_or_kana(source):
        return source
    cached = _SUMMARY_TRANSLATION_CACHE.get(source)
    if cached:
        return cached

    try:
        q = urllib.parse.quote(source[:650])
        url = (
            "https://translate.googleapis.com/translate_a/single"
            f"?client=gtx&sl=auto&tl=zh-TW&dt=t&q={q}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=max(3, timeout_sec)) as resp:
            raw = resp.read().decode("utf-8", "ignore")
        data = json.loads(raw)
        parts = []
        if isinstance(data, list) and data and isinstance(data[0], list):
            for seg in data[0]:
                if isinstance(seg, list) and seg and seg[0]:
                    parts.append(str(seg[0]))
        translated = "".join(parts).strip()
    except Exception as exc:  # noqa: BLE001
        logger.debug("worldmonitor summary translation failed: %s", exc)
        translated = ""

    result = translated or source
    _SUMMARY_TRANSLATION_CACHE[source] = result
    return result


def _news_digest_zh(item: Dict) -> str:
    title = str(item.get("title") or "").strip()
    summary = str(item.get("summary") or "").strip()
    text = title if not summary else f"{title}：{summary}"
    translated = _translate_to_zh_hant(text)
    return translated[:220].rstrip(" ，、；：")


def _fallback_news_analysis(news: List[Dict], markets: Dict, source_health: str) -> str:
    """Generate a readable Traditional Chinese summary when local LLM inference is unavailable."""
    source_counts: Dict[str, int] = {}
    for item in news:
        source = str(item.get("source") or "未知來源")
        source_counts[source] = source_counts.get(source, 0) + 1

    def _line(item: Dict) -> str:
        source = str(item.get("source") or "來源").strip()
        text = _news_digest_zh(item)
        return f"- {source}：{text}"

    asia_terms = ("Taiwan", "台灣", "亞太", "Asia", "Japan", "日本", "Korea", "韓", "China", "中國", "NHK", "Hormuz", "伊朗", "Ukraine", "Russia", "俄")
    asia_items = [
        item for item in news
        if any(term.lower() in (str(item.get("title", "")) + " " + str(item.get("summary", ""))).lower() for term in asia_terms)
    ]
    top_items = news[:5]
    market_symbols = [k for k in markets if not str(k).startswith("_")]

    lines = [
        "## 重大事件概述",
    ]
    if top_items:
        lines.extend(_line(item) for item in top_items)
    else:
        lines.append("- 本次未取得可摘要的新聞項目。")

    lines.extend([
        "",
        "## 對台灣與亞太的潛在影響",
    ])
    if asia_items:
        lines.extend(_line(item) for item in asia_items[:5])
    else:
        lines.append("- 本次資料未出現明確直接指向台灣或亞太的重大事件，仍建議持續觀察國際衝突與供應鏈風險。")

    lines.extend([
        "",
        "## 值得關注的發展趨勢",
        f"- 本次共取得 {len(news)} 則新聞，來源分布：" + "、".join(f"{k} {v} 則" for k, v in source_counts.items()) + "。",
        "- 若國際衝突、海運航道或能源供應相關新聞持續增加，可能影響亞太市場、航運成本與風險情緒。",
    ])
    if market_symbols:
        lines.append(f"- 市場資料涵蓋 {len(market_symbols)} 檔標的：" + "、".join(market_symbols[:8]) + "。")
    else:
        lines.append("- 市場行情本次未啟用或未取得，股匯市判讀需搭配其他市場報告。")

    failed_sources = [line for line in source_health.splitlines() if "FAIL" in line or "DEGRADED" in line]
    risk = "中"
    if len(failed_sources) >= 3:
        risk = "中偏高"
    elif len(news) >= 10:
        risk = "中"
    lines.extend([
        "",
        "## 風險評估",
        f"- 綜合風險：{risk}。",
        "- 來源可用性會影響判讀完整度；本報告保留來源健康狀態，方便確認資料缺口。",
        "",
        "_註：本段為 MAGI 在本機推理服務不可用時產生的結構式摘要。_",
    ])
    return "\n".join(lines)


def _analysis_needs_grounded_fallback(text: str) -> bool:
    """Reject chatty or poorly structured model output for the public report."""
    cleaned = str(text or "").strip()
    if cleaned.startswith("[推理失敗]") or len(cleaned) < 40:
        return True
    chatty_markers = (
        "我是 MAGI",
        "我是MAGI",
        "情報分析員 Melchior",
        "我已接收",
        "我已審閱",
        "好的，",
        "以下是為您準備",
    )
    if any(marker in cleaned for marker in chatty_markers):
        return True
    required = ("重大事件概述", "對台灣", "風險評估")
    return not all(marker in cleaned for marker in required)


def _normalize_analysis_markdown(text: str) -> str:
    """Trim model prose to the first useful section and keep headings parser-friendly."""
    cleaned = str(text or "").strip()
    match = re.search(r"(?m)^#{1,3}\s*(?:\d+[\.)]\s*)?重大事件概述", cleaned)
    if match:
        cleaned = cleaned[match.start():].strip()
    cleaned = re.sub(r"(?m)^#{1,3}\s*\d+[\.)]\s*", "## ", cleaned)
    cleaned = re.sub(r"(?m)^###\s+", "## ", cleaned)
    return cleaned


def render_plain_text_report(markdown_report: str) -> str:
    """Render a copy-friendly text report while keeping the stored .md report structured."""
    text = str(markdown_report or "")
    text = re.sub(r"<details><summary>原始資料</summary>.*?</details>", "", text, flags=re.S)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?m)^#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^---+$", "", text)
    text = re.sub(r"(?m)^_\s*(.*?)\s*_$", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def collect_and_analyze(use_melchior: bool = True) -> str:
    logger.info("=" * 60)
    logger.info("🌐 MAGI worldmonitor-intel — 全球情報收集")
    logger.info("=" * 60)

    # 1. Collect
    news, news_statuses = collect_news()
    markets, market_status = collect_markets()
    source_health = _render_source_health(news_statuses, market_status)

    if not news and not markets:
        final = f"""# 🌐 MAGI 全球情報摘要
**時間**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**狀態**: 降級模式（未收集到可分析資料）
**分析**: 無

---

{source_health}

---
<details><summary>原始資料</summary>

⚠️ 未收集到任何可用資料，但仍保留來源健康狀態與降級報告。
</details>"""
        _store_to_memory(final, metadata={
            "news_count": 0,
            "market_symbols": [],
            "news_items": [],
            "news_statuses": news_statuses,
            "market_status": market_status,
            "market_news": [],
            "tags": ["daily-intel", "degraded", "news", "markets"]
        })
        return final

    # 2. Format for Melchior
    parts = []
    if news:
        parts.append("## 📰 全球新聞")
        for n in news:
            link = str(n.get("link") or "").strip()
            link_part = f"（[閱讀原文]({link})）" if link else ""
            parts.append(f"- [{n['source']}] **{n['title']}**: {n['summary'][:150]} {link_part}".rstrip())
    
    if markets:
        parts.append("\n## 📊 市場數據")
        for sym, data in markets.items():
            if sym.startswith("_"):
                continue
            parts.append(f"- **{sym}**: ${data['price']:.2f} ({data['change_pct']:+.2f}%)")
        if "_news" in markets:
            parts.append("\n### 市場新聞")
            for n in markets["_news"]:
                link = str(n.get("url") or "").strip()
                link_part = f"（[閱讀原文]({link})）" if link else ""
                parts.append(f"- [{n['source']}] {n['title']} {link_part}".rstrip())

    raw_report = "\n".join(parts)
    if source_health:
        raw_report = f"{raw_report}\n\n{source_health}" if raw_report else source_health

    # 3. Analysis. Cron/web refresh uses grounded fallback to avoid chatty or invented prose.
    if use_melchior:
        logger.info("🧠 Sending to Melchior for analysis...")
        prompt = f"""你是 MAGI 的新聞摘要器。請只根據下方來源內容整理，不要加入來源未明示的事件、日期、病名、地點或推測。
禁止自我介紹、寒暄、說「我已接收」、說「以下是」。
請只輸出下列 Markdown 區塊，每個區塊用 2 到 5 個短項目，項目必須使用 "- " 開頭：

## 重大事件概述
## 對台灣與亞太的潛在影響
## 值得關注的發展趨勢
## 風險評估

收集時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}
來源健康：
{source_health}

{raw_report}"""
        analysis = _reason_with_melchior(prompt)
        if _analysis_needs_grounded_fallback(analysis):
            logger.warning("Melchior output unusable; using grounded structured fallback analysis")
            analysis = _fallback_news_analysis(news, markets, source_health)
            analysis_label = "來源整理"
        else:
            analysis = _normalize_analysis_markdown(analysis)
            analysis_label = "Melchior"
    else:
        analysis = _fallback_news_analysis(news, markets, source_health)
        analysis_label = "來源整理"

    final = f"""# 🌐 MAGI 全球情報摘要
**時間**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**新聞來源**: {len(news)} 篇 | **市場**: {len([k for k in markets if not k.startswith('_')])} 檔
**資料可用性**: {'正常' if news or markets else '降級'}
**分析**: {analysis_label}

---

{analysis}

---
{source_health}

---
<details><summary>原始資料</summary>

{raw_report[:3000]}
</details>"""

    # 4. Store
    _store_to_memory(final, metadata={
        "news_count": len(news),
        "market_symbols": [k for k in markets if not k.startswith("_")],
        "news_items": news,
        "news_statuses": news_statuses,
        "market_status": market_status,
        "market_news": markets.get("_news") or [],
        "tags": ["daily-intel", "news", "markets"]
    })

    return final


def main():
    parser = argparse.ArgumentParser(description="MAGI worldmonitor-intel")
    parser.add_argument("--task", required=True, choices=["collect", "recall", "status", "help"])
    parser.add_argument("--no-reasoning", action="store_true")
    parser.add_argument("--plain-output", action="store_true", help="print a copy-friendly plain text report")
    parser.add_argument("--query", default=None)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    if args.task == "help":
        print(json.dumps({"skill": "worldmonitor-intel", "tasks": ["collect", "recall", "status"], "description": "全球情報收集與分析"}, ensure_ascii=False, indent=2))
        return

    if args.task == "collect":
        report = collect_and_analyze(use_melchior=not args.no_reasoning)
        print("\n" + (render_plain_text_report(report) if args.plain_output else report))
    elif args.task == "recall":
        try:
            from skills.memory import mem_bridge
            results = mem_bridge.recall(query=args.query or "全球情報 最新", top_k=args.top_k)
            if results:
                for i, r in enumerate(results, 1):
                    print(f"--- [{i}] ---\n{r.get('content', str(r))[:500]}\n")
            else:
                print("找不到相關記憶。")
        except Exception as e:
            print(f"⚠️ {e}")
    elif args.task == "status":
        # Check Ollama
        try:
            from api.routing.service_registry import get_service_url as _gsurl2
            _omlx_def2 = _gsurl2("omlx_inference")
        except Exception:
            _omlx_def2 = "http://127.0.0.1:8080"
        data = _fetch_json(os.environ.get("OLLAMA_URL", _omlx_def2) + "/v1/models")
        if data:
            models = _extract_model_labels(data)
            if models:
                print(f"✅ Melchior 可用 ({len(models)} models: {', '.join(models[:3])})")
            else:
                print("✅ Melchior 可用 (models schema returned, but no labels could be extracted)")
        else:
            print("❌ Ollama 不可用")
        print(f"📊 Finnhub key: {'已設定' if FINNHUB_KEY else '未設定（使用 MAGI 免金鑰公開行情來源）'}")
        print(f"📰 RSS feeds: {len(RSS_FEEDS)} sources")

if __name__ == "__main__":
    main()

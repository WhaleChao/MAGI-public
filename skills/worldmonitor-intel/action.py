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
import argparse
import logging
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
    logger.warning("FINNHUB_API_KEY 未設定，Finnhub 行情功能停用。請在 .env 設定：FINNHUB_API_KEY=<your_key>")
FETCH_TIMEOUT = 15

# ---------------------------------------------------------------------------
# Public news RSS feeds (no API key needed)
# ---------------------------------------------------------------------------
RSS_FEEDS = {
    "Reuters World":   "https://feeds.reuters.com/Reuters/worldNews",
    "BBC World":       "https://feeds.bbci.co.uk/news/world/rss.xml",
    "NHK Asia":        "https://www3.nhk.or.jp/rss/news/cat6.xml",
    "Al Jazeera":      "https://www.aljazeera.com/xml/rss/all.xml",
    "AP News":         "https://rsshub.app/apnews/topics/apf-topnews",
}

FINNHUB_MARKET_SYMBOLS = ["AAPL", "TSMC", "NVDA", "SPY", "^GSPC"]

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def _fetch(url: str, timeout: int = FETCH_TIMEOUT) -> Optional[bytes]:
    try:
        import urllib.request
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
def collect_news() -> List[Dict]:
    """Collect news from public RSS feeds."""
    all_news = []
    for name, url in RSS_FEEDS.items():
        logger.info(f"📰 Fetching {name}...")
        raw = _fetch(url)
        if raw:
            items = _parse_rss(raw, max_items=5)
            for item in items:
                item["source"] = name
            all_news.extend(items)
            logger.info(f"  ✓ {len(items)} articles from {name}")
        else:
            logger.warning(f"  ✗ Failed to fetch {name}")
    return all_news

def collect_markets() -> Dict:
    """Collect market data from Finnhub."""
    if not FINNHUB_KEY:
        logger.warning("No Finnhub API key, skipping markets")
        return {}
    
    market_data = {}
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
            logger.info(f"  📊 {symbol}: ${data.get('c', 0):.2f} ({data.get('dp', 0):+.2f}%)")
    
    # Market news
    url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
    news = _fetch_json(url)
    if news and isinstance(news, list):
        market_data["_news"] = [
            {"title": n.get("headline", ""), "source": n.get("source", ""), "summary": n.get("summary", "")[:200]}
            for n in news[:5]
        ]
        logger.info(f"  📊 {len(market_data['_news'])} market news items")
    
    return market_data

# ---------------------------------------------------------------------------
# Melchior reasoning
# ---------------------------------------------------------------------------
def _reason_with_melchior(prompt: str, max_tokens: int = 2048) -> str:
    try:
        import urllib.request
        omlx_url = (os.environ.get("OMLX_URL") or os.environ.get("OLLAMA_URL") or "http://127.0.0.1:8080").rstrip("/")
        model = os.environ.get("MELCHIOR_MODEL", "TAIDE-12b-Chat-mlx-4bit")

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
    try:
        from skills.memory import mem_bridge
        tags = ["worldmonitor", "intel", datetime.now().strftime("%Y-%m-%d")]
        if metadata:
            tags.extend(metadata.get("tags", []))
        mem_bridge.remember(content=content, tags=tags, metadata={
            "source": "worldmonitor-intel",
            "timestamp": datetime.now().isoformat(),
            **(metadata or {})
        })
        logger.info(f"✅ Stored to MAGI memory ({len(content)} chars)")
        return True
    except Exception as e:
        logger.warning(f"Memory bridge failed, saving to file: {e}")
    
    # Fallback: local file
    report_dir = os.path.join(MAGI_DIR, "static", "worldmonitor_reports")
    os.makedirs(report_dir, exist_ok=True)
    filepath = os.path.join(report_dir, f"intel_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info(f"📁 Saved report to {filepath}")
    return True

# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------
def collect_and_analyze(use_melchior: bool = True) -> str:
    logger.info("=" * 60)
    logger.info("🌐 MAGI worldmonitor-intel — 全球情報收集")
    logger.info("=" * 60)

    # 1. Collect
    news = collect_news()
    markets = collect_markets()

    if not news and not markets:
        msg = "⚠️ 未收集到任何資料。"
        logger.warning(msg)
        return msg

    # 2. Format for Melchior
    parts = []
    if news:
        parts.append("## 📰 全球新聞")
        for n in news:
            parts.append(f"- [{n['source']}] **{n['title']}**: {n['summary'][:150]}")
    
    if markets:
        parts.append("\n## 📊 市場數據")
        for sym, data in markets.items():
            if sym.startswith("_"):
                continue
            parts.append(f"- **{sym}**: ${data['price']:.2f} ({data['change_pct']:+.2f}%)")
        if "_news" in markets:
            parts.append("\n### 市場新聞")
            for n in markets["_news"]:
                parts.append(f"- [{n['source']}] {n['title']}")

    raw_report = "\n".join(parts)

    # 3. Melchior analysis
    if use_melchior:
        logger.info("🧠 Sending to Melchior for analysis...")
        prompt = f"""以下是剛收集的全球情報。請分析並產出摘要：
1. 重大事件概述（3-5 條）
2. 對台灣/亞太的潛在影響
3. 值得關注的發展趨勢
4. 風險評估（低/中/高）

收集時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}
{raw_report}"""
        analysis = _reason_with_melchior(prompt)
        final = f"""# 🌐 MAGI 全球情報摘要
**時間**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
**新聞來源**: {len(news)} 篇 | **市場**: {len([k for k in markets if not k.startswith('_')])} 檔
**分析**: Melchior

---

{analysis}

---
<details><summary>原始資料</summary>

{raw_report[:3000]}
</details>"""
    else:
        final = f"# 🌐 原始報告\n**時間**: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n{raw_report}"

    # 4. Store
    _store_to_memory(final, metadata={
        "news_count": len(news),
        "market_symbols": [k for k in markets if not k.startswith("_")],
        "tags": ["daily-intel", "news", "markets"]
    })

    return final


def main():
    parser = argparse.ArgumentParser(description="MAGI worldmonitor-intel")
    parser.add_argument("--task", required=True, choices=["collect", "recall", "status", "help"])
    parser.add_argument("--no-reasoning", action="store_true")
    parser.add_argument("--query", default=None)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    if args.task == "help":
        print(json.dumps({"skill": "worldmonitor-intel", "tasks": ["collect", "recall", "status"], "description": "全球情報收集與分析"}, ensure_ascii=False, indent=2))
        return

    if args.task == "collect":
        report = collect_and_analyze(use_melchior=not args.no_reasoning)
        print("\n" + report)
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
        data = _fetch_json(os.environ.get("OLLAMA_URL", "http://127.0.0.1:8080") + "/v1/models")
        if data:
            models = [m["name"] for m in data.get("models", [])]
            print(f"✅ Melchior 可用 ({len(models)} models: {', '.join(models[:3])})")
        else:
            print("❌ Ollama 不可用")
        print(f"📊 Finnhub key: {'已設定' if FINNHUB_KEY else '未設定'}")
        print(f"📰 RSS feeds: {len(RSS_FEEDS)} sources")

if __name__ == "__main__":
    main()

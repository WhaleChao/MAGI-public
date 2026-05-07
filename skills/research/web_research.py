"""
WEB RESEARCH MODULE (網路研究引擎)
===================================
Enables CASPER to search the web and fetch content from URLs.
This provides autonomous knowledge update capability.

Safety: All fetched content is validated against Iron Dome patterns.
"""
import logging

import os
import sys
import re
import json
import requests
from urllib.parse import quote_plus, urlparse
from datetime import datetime
from bs4 import BeautifulSoup

# Ensure we import MAGI's local `skills.*` package (not an unrelated installed package).
# `__file__` = .../MAGI/skills/research/web_research.py
_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _MAGI_ROOT not in sys.path:
    sys.path.insert(0, _MAGI_ROOT)
from skills.bridge.shared_utils.text_utils import strip_zero_width as _strip_zero_width

# Import Iron Dome safety check (via skill genesis)
try:
    from skills.evolution.skill_genesis import validate_skill_safety
except Exception as e:
    # Fail-closed: if Iron Dome validator can't be imported, web research must not run.
    _VALIDATE_IMPORT_ERR = str(e)

    def validate_skill_safety(content):
        return (False, [f"IRON_DOME_IMPORT_FAILED: {_VALIDATE_IMPORT_ERR}"])

# =============================================================================
# Configuration
# =============================================================================
SEARCH_CACHE_DIR = f"{_MAGI_ROOT}/cache/web_search"
USER_AGENT = "MAGI-CASPER/1.0 (Web Research Module)"
MAX_CONTENT_LENGTH = 50000  # Characters
MAX_SECTIONS = 8
_ZERO_WIDTH_ESCAPED = ("\\u200b", "\\u200c", "\\u200d", "\\ufeff")


def _validate_web_content(content: str) -> tuple[bool, str, list]:
    """
    Validate fetched web content with a pre-normalization pass to avoid
    false positives caused by zero-width/BOM characters in page source.
    Returns: (is_safe, normalized_content, violations)
    """
    normalized = _strip_zero_width(content or "")
    is_safe, violations = validate_skill_safety(normalized)
    if is_safe:
        return True, normalized, []

    violation_text = " | ".join([str(v) for v in (violations or [])]).lower()
    maybe_zero_width_fp = any(tok in violation_text for tok in [
        "\\u200b", "\\u200c", "\\u200d", "\\ufeff", "200b", "200c", "200d", "feff", "zero-width"
    ])
    if maybe_zero_width_fp:
        deescaped = normalized
        for tok in _ZERO_WIDTH_ESCAPED:
            deescaped = deescaped.replace(tok, "")
        is_safe2, violations2 = validate_skill_safety(deescaped)
        if is_safe2:
            return True, deescaped, []
        return False, deescaped, violations2

    return False, normalized, violations


def _internet_enabled() -> bool:
    # Read env dynamically so toggles apply without restart.
    return os.environ.get("MAGI_ALLOW_INTERNET", "0").strip().lower() in {"1", "true", "yes", "on"}


def _is_private_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if not h:
        return False
    if h in {"localhost", "127.0.0.1"}:
        return True
    # Allow tailnet CGNAT and common RFC1918 private ranges.
    m = re.match(r"^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$", h)
    if not m:
        return False
    a, b, c, d = [int(x) for x in m.groups()]
    if any(x < 0 or x > 255 for x in (a, b, c, d)):
        return False
    if a == 10:
        return True
    if a == 192 and b == 168:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    if a == 100 and 64 <= b <= 127:
        return True
    return False


def _internet_guard(url: str = "", allow_private: bool = True) -> tuple[bool, str]:
    """
    Returns (allowed, error_message).
    When MAGI_ALLOW_INTERNET=0, only private/localhost URLs are allowed (if allow_private=True).
    """
    if _internet_enabled():
        return True, ""
    if not url:
        return False, "外網已停用（MAGI_ALLOW_INTERNET=0）。"
    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if allow_private and _is_private_host(host):
            return True, ""
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 116, exc_info=True)
    return False, "外網已停用（MAGI_ALLOW_INTERNET=0）。"

# Ensure cache directory exists
os.makedirs(SEARCH_CACHE_DIR, exist_ok=True)

# =============================================================================
# Search Engines
# =============================================================================

def search_duckduckgo(query: str, num_results: int = 5) -> list[dict]:
    """
    Search using DuckDuckGo HTML (no API key required).
    
    Args:
        query: Search query
        num_results: Number of results to return
    
    Returns:
        List of {"title": str, "url": str, "snippet": str}
    """
    try:
        url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {"User-Agent": USER_AGENT}
        
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, 'html.parser')
        results = []
        
        for result in soup.select('.result')[:num_results]:
            title_elem = result.select_one('.result__title')
            link_elem = result.select_one('.result__url')
            snippet_elem = result.select_one('.result__snippet')
            
            if title_elem and link_elem:
                # Extract actual URL from DuckDuckGo redirect
                href = title_elem.find('a')
                actual_url = href.get('href', '') if href else ''
                
                # DuckDuckGo uses redirect URLs, try to extract real one
                if 'uddg=' in actual_url:
                    import urllib.parse
                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(actual_url).query)
                    actual_url = parsed.get('uddg', [actual_url])[0]
                
                results.append({
                    "title": title_elem.get_text(strip=True),
                    "url": actual_url or link_elem.get_text(strip=True),
                    "snippet": snippet_elem.get_text(strip=True) if snippet_elem else ""
                })
        
        return results
    except Exception as e:
        print(f"[WEB RESEARCH] Search error: {e}")
        return []


def search_web(query: str, num_results: int = 5) -> dict:
    """
    Main search function. Returns formatted results.
    
    Args:
        query: Search query
        num_results: Number of results
    
    Returns:
        {"success": bool, "query": str, "results": list, "error": str}
    """
    ok, err = _internet_guard("", allow_private=False)
    if not ok:
        return {"success": False, "query": query, "results": [], "error": err}

    results = search_duckduckgo(query, num_results)
    
    if results:
        return {
            "success": True,
            "query": query,
            "results": results,
            "error": None
        }
    else:
        return {
            "success": False,
            "query": query,
            "results": [],
            "error": "No results found or search failed"
        }


# =============================================================================
# URL Content Fetching
# =============================================================================

def fetch_url_content(url: str, max_length: int = MAX_CONTENT_LENGTH, exempt_iron_dome: bool = False) -> dict:
    """
    Fetches and extracts main text content from a URL.
    
    Args:
        url: URL to fetch
        max_length: Maximum content length to return
        exempt_iron_dome: Bypass _internet_guard block for explicit user requests
    
    Returns:
        {"success": bool, "url": str, "title": str, "content": str, "error": str}
    """
    try:
        ok, err = _internet_guard(url, allow_private=True)
        if not ok and not exempt_iron_dome:
            return {"success": False, "url": url, "title": "", "content": "", "error": err}

        # Optional Scrapling path. If enabled but unavailable, continue with legacy fetch.
        try:
            from skills.engine.scraping_adapter import fetch_page

            scrapling_result = fetch_page(url, timeout=20)
            if not scrapling_result.get("use_fallback"):
                if not scrapling_result.get("success"):
                    return {
                        "success": False,
                        "url": url,
                        "title": "",
                        "content": "",
                        "error": scrapling_result.get("error") or "scrapling_fetch_failed",
                    }
                text = re.sub(r'\n{3,}', '\n\n', str(scrapling_result.get("content") or ""))[:max_length]
                title = str(scrapling_result.get("title") or "")
                is_safe, text, violations = _validate_web_content(text)
                if not is_safe:
                    return {
                        "success": False,
                        "url": url,
                        "title": title,
                        "content": "",
                        "error": f"IRON DOME: Content blocked - {violations[0]}",
                    }
                return {
                    "success": True,
                    "url": str(scrapling_result.get("url") or url),
                    "title": title,
                    "content": text,
                    "error": None,
                    "engine": "scrapling",
                }
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 238, exc_info=True)
        
        # Validate URL
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return {"success": False, "url": url, "title": "", "content": "", "error": "Invalid URL"}
        
        headers = {"User-Agent": USER_AGENT}
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
        
        # Parse HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # Remove scripts, styles, nav, footer
        for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            element.decompose()
        
        # Get title
        title = soup.title.string if soup.title else ""
        
        # Get main content
        main_content = soup.find('main') or soup.find('article') or soup.find('body')
        
        if main_content:
            text = main_content.get_text(separator='\n', strip=True)
        else:
            text = soup.get_text(separator='\n', strip=True)
        
        # Clean up whitespace
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text[:max_length]
        
        # === IRON DOME CHECK ===
        is_safe, text, violations = _validate_web_content(text)
        if not is_safe:
            return {
                "success": False,
                "url": url,
                "title": title,
                "content": "",
                "error": f"IRON DOME: Content blocked - {violations[0]}"
            }
        
        return {
            "success": True,
            "url": url,
            "title": title,
            "content": text,
            "error": None,
            "engine": "requests",
        }
    except Exception as e:
        return {
            "success": False,
            "url": url,
            "title": "",
            "content": "",
            "error": str(e)
        }


def fetch_url_sections(url: str, max_length: int = MAX_CONTENT_LENGTH, max_sections: int = MAX_SECTIONS, exempt_iron_dome: bool = False) -> dict:
    """
    Fetch a URL and extract tabbed/sectioned content when present.

    Supports the GlobalHealthRights case template which uses internal tab anchors:
    - div#tabberpost ul.tabs a[href="#..."] with corresponding div id panels.

    Returns:
        {
          "success": bool,
          "url": str,
          "title": str,
          "sections": [{"id": str, "title": str, "content": str}],
          "error": str|None
        }
    """
    try:
        ok, err = _internet_guard(url, allow_private=True)
        if not ok and not exempt_iron_dome:
            return {"success": False, "url": url, "title": "", "sections": [], "error": err}

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return {"success": False, "url": url, "title": "", "sections": [], "error": "Invalid URL"}

        soup = None
        title = ""
        engine = "requests"
        try:
            from skills.engine.scraping_adapter import fetch_page

            scrapling_result = fetch_page(url, timeout=25)
            if not scrapling_result.get("use_fallback"):
                if not scrapling_result.get("success"):
                    return {
                        "success": False,
                        "url": url,
                        "title": "",
                        "sections": [],
                        "error": scrapling_result.get("error") or "scrapling_fetch_failed",
                    }
                soup = BeautifulSoup(str(scrapling_result.get("html") or ""), "html.parser")
                title = str(scrapling_result.get("title") or "")
                engine = "scrapling"
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 317, exc_info=True)

        if soup is None:
            headers = {"User-Agent": USER_AGENT}
            response = requests.get(url, headers=headers, timeout=25)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
        for element in soup(["script", "style"]):
            element.decompose()

        if not title:
            title = soup.title.string.strip() if soup.title and soup.title.string else ""

        sections = []
        used_chars = 0

        # 1) Tabbed case template (GlobalHealthRights)
        tab_root = soup.find("div", id="tabberpost")
        tab_list = tab_root.find("ul", class_=re.compile(r"\btabs\b", re.IGNORECASE)) if tab_root else None
        if tab_list:
            anchors = tab_list.find_all("a")
            for a in anchors:
                if len(sections) >= max(1, int(max_sections)):
                    break
                href = (a.get("href") or "").strip()
                label = a.get_text(" ", strip=True) or ""
                if not href.startswith("#"):
                    continue
                sec_id = href.lstrip("#").strip()
                if not sec_id:
                    continue
                panel = soup.find(id=sec_id)
                if not panel:
                    continue
                text = panel.get_text(separator="\n", strip=True)
                text = re.sub(r"\n{3,}", "\n\n", text).strip()
                if not text:
                    continue
                remaining = max(0, int(max_length) - used_chars)
                if remaining <= 0:
                    break
                if len(text) > remaining:
                    text = text[:remaining]
                used_chars += len(text)

                is_safe, text, violations = _validate_web_content(text)
                if not is_safe:
                    sections.append(
                        {
                            "id": sec_id,
                            "title": label or sec_id,
                            "content": "",
                            "error": f"IRON DOME: Content blocked - {violations[0]}",
                        }
                    )
                    continue
                sections.append({"id": sec_id, "title": label or sec_id, "content": text})

        # 2) Fallback: single section from main content
        if not sections:
            main_content = soup.find("main") or soup.find("article") or soup.find("body")
            text = main_content.get_text(separator="\n", strip=True) if main_content else soup.get_text(separator="\n", strip=True)
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            text = text[:max_length]

            is_safe, text, violations = _validate_web_content(text)
            if not is_safe:
                return {
                    "success": False,
                    "url": url,
                    "title": title,
                    "sections": [],
                    "error": f"IRON DOME: Content blocked - {violations[0]}",
                }
            sections = [{"id": "main", "title": title or "Main", "content": text}]

        return {"success": True, "url": url, "title": title, "sections": sections, "error": None, "engine": engine}
    except Exception as e:
        return {"success": False, "url": url, "title": "", "sections": [], "error": str(e)}


def fetch_raw_url(url: str) -> dict:
    """
    Fetches raw content from a URL (for code, JSON, etc.).
    
    Args:
        url: URL to fetch
    
    Returns:
        {"success": bool, "url": str, "content": str, "error": str}
    """
    try:
        ok, err = _internet_guard(url, allow_private=True)
        if not ok:
            return {"success": False, "url": url, "content": "", "error": err}

        # Convert GitHub blob URLs to raw
        if "github.com" in url and "/blob/" in url:
            url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
        
        headers = {"User-Agent": USER_AGENT}
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        content = response.text
        
        # === IRON DOME CHECK ===
        is_safe, content, violations = _validate_web_content(content)
        if not is_safe:
            return {
                "success": False,
                "url": url,
                "content": "",
                "error": f"IRON DOME: Content blocked - {violations[0]}"
            }
        
        return {
            "success": True,
            "url": url,
            "content": content,
            "error": None
        }
    except Exception as e:
        return {
            "success": False,
            "url": url,
            "content": "",
            "error": str(e)
        }


# =============================================================================
# Knowledge Research (Combined)
# =============================================================================

def research_topic(topic: str, depth: int = 3) -> dict:
    """
    Researches a topic by searching and fetching top results.
    
    Args:
        topic: Topic to research
        depth: Number of pages to fetch
    
    Returns:
        {"topic": str, "sources": list, "summary": str}
    """
    ok, err = _internet_guard("", allow_private=False)
    if not ok:
        return {"topic": topic, "sources": [], "summary": "", "combined_content": "", "error": err}

    search_result = search_web(topic, num_results=depth)
    
    if not search_result["success"]:
        return {
            "topic": topic,
            "sources": [],
            "summary": f"Research failed: {search_result['error']}"
        }
    
    sources = []
    all_content = []
    
    for result in search_result["results"]:
        fetch_result = fetch_url_content(result["url"], max_length=10000)
        
        if fetch_result["success"]:
            sources.append({
                "title": result["title"],
                "url": result["url"],
                "content_preview": fetch_result["content"][:500] + "..."
            })
            all_content.append(f"## {result['title']}\n{fetch_result['content'][:2000]}")
    
    # Cache results
    cache_file = os.path.join(SEARCH_CACHE_DIR, f"{quote_plus(topic)[:50]}_{datetime.now().strftime('%Y%m%d%H%M%S')}.json")
    with open(cache_file, 'w') as f:
        json.dump({"topic": topic, "sources": sources, "timestamp": datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)
    
    return {
        "topic": topic,
        "sources": sources,
        "combined_content": "\n\n---\n\n".join(all_content),
        "cache_file": cache_file
    }


# =============================================================================
# Web-Grounded Synthesis (Bug #5: Layer C)
# =============================================================================

# 自然語意即時資訊查詢路由器：命中以下類別 → 走 web_research_synthesize
_WEB_GROUNDED_PATTERNS = [
    # 評價類
    (re.compile(r"評價|好不好|推薦|心得|評論|評分|review|有人去過|有人試過|值不值得", re.IGNORECASE), "review"),
    # 路線類（含地名才觸發）
    (re.compile(r"怎麼去|怎麼走|要多久|路線|開車到|搭車到|捷運|公車|交通|怎麼搭|從.{1,10}到", re.IGNORECASE), "route"),
    # 營業時間類
    (re.compile(r"營業|開門|幾點關|還在開嗎|有開嗎|營業時間|開放時間", re.IGNORECASE), "hours"),
    # 商品比較類
    (re.compile(r"比較|哪個比較|差別|差在哪|和.{1,10}哪個好", re.IGNORECASE), "compare"),
    # 新聞時事類
    (re.compile(r"最近.{0,8}(新聞|消息|發生|怎麼了|怎樣了)|近期.{0,8}消息|最新消息", re.IGNORECASE), "news"),
]

# 純閒聊排除 pattern：命中這些 → 不走 web_grounded（交給 LLM 閒聊）
_WEB_GROUNDED_EXCLUDE = re.compile(
    r"^(?:你好|嗨|哈囉|hello|hi|謝謝|再見|bye|哈哈|好的|好啊|不客氣|可以|恩|嗯)",
    re.IGNORECASE,
)

logger_wrs = logging.getLogger(__name__ + ".synthesize")


def _maybe_route_to_web_grounded(message: str):
    """
    判斷訊息是否屬於「即時資訊類（非數字精確）」查詢。
    命中 → 回傳 category 字串（'review'/'route'/'hours'/'compare'/'news'）
    未命中 → 回傳 None，讓後面 LLM 處理。
    """
    if not message or len(message.strip()) < 4:
        return None
    compact = message.strip()
    if _WEB_GROUNDED_EXCLUDE.match(compact):
        return None
    for pattern, category in _WEB_GROUNDED_PATTERNS:
        if pattern.search(compact):
            return category
    return None


def web_research_synthesize(query: str, max_sources: int = 3) -> str:
    """
    搜尋 + 抓內文 + LLM 整理摘要（Bug #5 Layer C）。

    設計原則：
    - 數字精確類（天氣/股價）由 realtime_data_gateway 處理，不在此函式範圍。
    - 資訊整合類（評價/路線/營業時間/商品比較/新聞）走此路徑。
    - 若搜尋或抓網頁全失敗，回 fallback 文字（比「給網址自己看」更誠實）。

    Returns:
        格式化字串：
        {LLM 整理的 2-4 句答案}

        ── 資料來源 ──
        [1] {title} — {url}
        [2] ...
    """
    ok, err = _internet_guard("", allow_private=False)
    if not ok:
        logger_wrs.warning("web_research_synthesize blocked by internet guard: %s", err)
        return (
            f"我搜尋了但網路存取受限，建議直接查 Google：\n"
            f"https://www.google.com/search?q={quote_plus(query)}"
        )

    # Step 1: DuckDuckGo 搜尋
    try:
        results = search_duckduckgo(query, num_results=5)
    except Exception as e:
        logger_wrs.warning("search_duckduckgo failed: %s", e)
        results = []

    if not results:
        return (
            f"我搜尋了但沒找到可靠來源，建議直接查 Google：\n"
            f"https://www.google.com/search?q={quote_plus(query)}"
        )

    # Step 2: 對前 max_sources 筆結果抓內文
    fetched = []
    for item in results[:max_sources]:
        url = item.get("url", "")
        title = item.get("title", url)
        if not url:
            continue
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=8,
                verify=False,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            # 抽主要文字：優先 article/main，退而求其次 p tags
            body = soup.find("article") or soup.find("main") or soup.body
            if body:
                text = " ".join(p.get_text(" ", strip=True) for p in body.find_all("p"))
            else:
                text = soup.get_text(" ", strip=True)
            text = text[:1500]
            fetched.append({"title": title, "url": url, "text": text})
        except Exception as e:
            logger_wrs.debug("fetch %s failed: %s", url, e)
            # 即使抓網頁失敗，保留 snippet 作為替代
            snippet = item.get("snippet", "")
            if snippet:
                fetched.append({"title": title, "url": url, "text": snippet[:500]})

    if not fetched:
        return (
            f"我搜尋了但無法讀取搜尋結果頁面，建議直接查 Google：\n"
            f"https://www.google.com/search?q={quote_plus(query)}"
        )

    # Step 3: 用本地 LLM 整理摘要（grounded synthesis）
    sources_text = "\n\n".join(
        f"[來源 {i+1}] {s['title']}\n{s['text']}"
        for i, s in enumerate(fetched)
    )
    synthesis_prompt = (
        f"使用者問題：{query}\n\n"
        f"以下是從網路搜集的資料：\n{sources_text}\n\n"
        f"請只根據以上資料，用 2~4 句繁體中文回答使用者問題。"
        f"每個重要事實後面標注 [來源 N]（N 為來源編號）。"
        f"若所有來源都沒提到使用者問的點，請明說「目前搜尋到的資料不足以回答此問題」。"
        f"不要自行補充資料中沒有的資訊。"
    )

    synthesized = ""
    try:
        from skills.bridge.grounded_ai import _generate_local
        synthesized = _generate_local(synthesis_prompt, max_tokens=300)
    except Exception as e:
        logger_wrs.warning("LLM synthesis failed: %s", e)
        # Fallback: 直接列出 snippet
        synthesized = "根據以下搜尋結果整理：\n" + "\n".join(
            f"• {s['title']}：{s['text'][:200]}..." for s in fetched[:2]
        )

    # Step 4: 格式化輸出
    source_lines = "\n".join(
        f"[{i+1}] {s['title']} — {s['url']}"
        for i, s in enumerate(fetched)
    )
    return f"{synthesized.strip()}\n\n── 資料來源 ──\n{source_lines}"


# =============================================================================
# Module Test
# =============================================================================
if __name__ == "__main__":
    print("🌐 WEB RESEARCH MODULE TEST")
    print("=" * 50)
    
    # Test search
    print("\n1. Testing DuckDuckGo Search...")
    results = search_web("Python best practices 2024", num_results=3)
    print(f"   Success: {results['success']}")
    for r in results.get('results', [])[:2]:
        print(f"   - {r['title'][:50]}...")
    
    # Test URL fetch
    print("\n2. Testing URL Fetch...")
    fetch = fetch_url_content("https://www.python.org/")
    print(f"   Success: {fetch['success']}")
    print(f"   Title: {fetch.get('title', 'N/A')}")
    print(f"   Content length: {len(fetch.get('content', ''))}")

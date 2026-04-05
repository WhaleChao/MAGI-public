"""
Tool Registry — 將 MAGI 現有功能包裝為 ReAct 工具
==================================================
每個工具是一個 dict: {"fn": callable, "desc": str, "params": str}
工具函數接受 **kwargs，回傳 str（觀察結果）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any


def _tools_api_url() -> str:
    try:
        from api.routing.service_registry import get_service_url
        return get_service_url("tools_api")
    except Exception:
        return "http://localhost:5003"

logger = logging.getLogger("ToolRegistry")

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2])))


# ── Tool Implementations ─────────────────────────────────

def _search_memory(query: str = "", top_k: int = 5, **_) -> str:
    """搜尋 MAGI 記憶庫。"""
    try:
        from skills.memory.mem_bridge import recall
        results = recall(query, top_k=min(top_k, 10))
        if not results:
            return f"記憶庫中找不到與「{query}」相關的內容。"
        lines = []
        for i, r in enumerate(results, 1):
            content = str(r.get("content", ""))[:200]
            source = r.get("source", "unknown")
            score = r.get("score", 0)
            lines.append(f"{i}. [{source}] (score={score:.2f}) {content}")
        return "\n".join(lines)
    except Exception as e:
        return f"記憶搜尋失敗: {e}"


def _remember(content: str = "", source: str = "react_agent", **_) -> str:
    """將資訊存入長期記憶。"""
    if not content.strip():
        return "錯誤: 沒有提供要記住的內容。"
    try:
        from skills.memory.mem_bridge import remember
        result = remember(content, source=source)
        return f"已存入記憶: {content[:100]}..."
    except Exception as e:
        return f"記憶存入失敗: {e}"


def _web_search(query: str = "", num_results: int = 5, **_) -> str:
    """網路搜尋。"""
    try:
        from skills.bridge.http_pool import get_session
        session = get_session()
        resp = session.post(
            _tools_api_url() + "/search",
            json={"query": query, "num_results": min(num_results, 10)},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            if not results:
                return f"搜尋「{query}」沒有找到結果。"
            lines = []
            for r in results[:5]:
                title = r.get("title", "")
                snippet = r.get("snippet", "")[:150]
                url = r.get("url", "")
                lines.append(f"- {title}: {snippet} ({url})")
            return "\n".join(lines)
        return f"搜尋 API 回傳 {resp.status_code}"
    except Exception as e:
        return f"網路搜尋失敗: {e}"


def _query_cases(query: str = "", **_) -> str:
    """查詢案件資料庫（OSC）。"""
    try:
        from skills.bridge.http_pool import get_session
        session = get_session()
        resp = session.get(
            "http://localhost:5002/api/osc/cases/search",
            params={"q": query, "limit": 5},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            cases = data.get("cases", data.get("results", []))
            if not cases:
                return f"查無與「{query}」相關的案件。"
            lines = []
            for c in cases[:5]:
                name = c.get("client_name", c.get("name", ""))
                case_no = c.get("case_number", c.get("case_no", ""))
                reason = c.get("case_reason", c.get("reason", ""))
                status = c.get("status", "")
                lines.append(f"- {name} | {case_no} | {reason} | 狀態: {status}")
            return "\n".join(lines)
        return f"案件查詢 API 回傳 {resp.status_code}"
    except Exception as e:
        return f"案件查詢失敗: {e}"


def _summarize_text(text: str = "", **_) -> str:
    """摘要一段文字。"""
    if not text.strip():
        return "錯誤: 沒有提供要摘要的文字。"
    try:
        from skills.bridge.llm_direct import chat
        result = chat(prompt=text[:8000], feature="summary", timeout=90)
        return result.get("text", "摘要失敗") if result.get("success") else f"摘要失敗: {result.get('error')}"
    except Exception as e:
        return f"摘要失敗: {e}"


def _translate_text(text: str = "", target_lang: str = "繁體中文", **_) -> str:
    """翻譯文字。"""
    if not text.strip():
        return "錯誤: 沒有提供要翻譯的文字。"
    try:
        from skills.bridge.llm_direct import chat
        prompt = f"將以下文字翻譯為{target_lang}：\n\n{text[:8000]}"
        result = chat(prompt=prompt, feature="translate", timeout=90)
        return result.get("text", "翻譯失敗") if result.get("success") else f"翻譯失敗: {result.get('error')}"
    except Exception as e:
        return f"翻譯失敗: {e}"


def _get_schedule(date: str = "", **_) -> str:
    """查詢行程（Google Calendar）。"""
    try:
        from skills.bridge.http_pool import get_session
        session = get_session()
        params = {"date": date} if date else {}
        resp = session.get(
            "http://localhost:5002/api/schedule",
            params=params,
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            events = data.get("events", [])
            if not events:
                return f"{'日期 ' + date + ' ' if date else '今天'}沒有行程。"
            lines = []
            for e in events[:10]:
                t = e.get("time", e.get("start", ""))
                title = e.get("title", e.get("summary", ""))
                lines.append(f"- {t} {title}")
            return "\n".join(lines)
        return f"行程查詢 API 回傳 {resp.status_code}"
    except Exception as e:
        return f"行程查詢失敗: {e}"


def _read_file(path: str = "", max_chars: int = 3000, **_) -> str:
    """讀取檔案內容（只限 MAGI 工作目錄和案件資料夾）。"""
    if not path:
        return "錯誤: 沒有提供檔案路徑。"

    # Iron Dome: 限制可讀路徑（動態解析 NAS 路徑）
    _extra_roots = []
    try:
        from api.case_path_mapper import default_case_roots
        _extra_roots = default_case_roots(include_closed=True)
    except Exception:
        _extra_roots = ["/Users/ai/Library/CloudStorage/SynologyDrive-homes/01_案件"]
    allowed_prefixes = [
        str(MAGI_ROOT),
        *_extra_roots,
        "/tmp/",
    ]
    resolved = str(Path(path).resolve())
    if not any(resolved.startswith(p) for p in allowed_prefixes):
        return f"⛔ 安全限制: 不允許讀取 {path}"

    try:
        p = Path(path)
        if not p.exists():
            return f"檔案不存在: {path}"
        if p.is_dir():
            files = sorted(p.iterdir())[:20]
            return "目錄內容:\n" + "\n".join(f"- {f.name}" for f in files)
        content = p.read_text(encoding="utf-8", errors="replace")
        if len(content) > max_chars:
            return content[:max_chars] + f"\n...(截斷，共 {len(content)} 字元)"
        return content
    except Exception as e:
        return f"讀取失敗: {e}"


def _run_skill(skill_name: str = "", args: str = "", **_) -> str:
    """執行 MAGI 技能。"""
    if not skill_name:
        return "錯誤: 沒有提供技能名稱。"

    # 安全白名單 — 只有這些技能可以被 ReAct 呼叫
    ALLOWED_SKILLS = {
        "web_search", "deep_research", "fetch_url",
        "analyze_image", "summarize_text",
        "query_clients", "query_cases",
        "run_pdf_namer", "judgment_search",
    }
    if skill_name not in ALLOWED_SKILLS:
        return f"⛔ 技能 '{skill_name}' 不在 ReAct 允許清單中。允許: {sorted(ALLOWED_SKILLS)}"

    try:
        from skills.bridge.http_pool import get_session
        session = get_session()
        resp = session.post(
            f"{_tools_api_url()}/skill/{skill_name}",
            json={"args": args} if args else {},
            timeout=60,
        )
        if resp.status_code == 200:
            return str(resp.json().get("result", resp.text))[:2000]
        return f"技能執行回傳 {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return f"技能執行失敗: {e}"


def _calculate(expression: str = "", **_) -> str:
    """計算數學表達式（安全沙箱）。"""
    if not expression:
        return "錯誤: 沒有提供算式。"
    # 只允許數字和基本運算符
    import re
    safe = re.sub(r'[^0-9+\-*/().,%\s]', '', expression)
    if not safe.strip():
        return "錯誤: 算式包含不允許的字元。"
    try:
        result = eval(safe, {"__builtins__": {}}, {})
        return f"{expression} = {result}"
    except Exception as e:
        return f"計算錯誤: {e}"


def _get_current_time(**_) -> str:
    """取得目前日期時間。"""
    from datetime import datetime
    now = datetime.now()
    weekdays = ["一", "二", "三", "四", "五", "六", "日"]
    return f"現在是 {now.strftime('%Y-%m-%d')} 星期{weekdays[now.weekday()]} {now.strftime('%H:%M:%S')}"


# ── Tool Registry ─────────────────────────────────────────

TOOLS: dict[str, dict[str, Any]] = {
    "search_memory": {
        "fn": _search_memory,
        "desc": "搜尋 MAGI 記憶庫（向量 + 全文檢索）",
        "params": "query: str（搜尋關鍵字）, top_k: int（結果數量，預設 5）",
    },
    "remember": {
        "fn": _remember,
        "desc": "將資訊存入長期記憶",
        "params": "content: str（要記住的內容）",
    },
    "web_search": {
        "fn": _web_search,
        "desc": "網路搜尋最新資訊",
        "params": "query: str（搜尋關鍵字）, num_results: int（結果數量，預設 5）",
    },
    "query_cases": {
        "fn": _query_cases,
        "desc": "查詢事務所案件資料庫（當事人、案號、案由等）",
        "params": "query: str（案件關鍵字或案號）",
    },
    "summarize": {
        "fn": _summarize_text,
        "desc": "摘要一段文字（法律文件、判決書等）",
        "params": "text: str（要摘要的文字）",
    },
    "translate": {
        "fn": _translate_text,
        "desc": "翻譯文字",
        "params": "text: str（要翻譯的文字）, target_lang: str（目標語言，預設繁體中文）",
    },
    "get_schedule": {
        "fn": _get_schedule,
        "desc": "查詢行程（Google Calendar）",
        "params": "date: str（日期 YYYY-MM-DD，空值=今天）",
    },
    "read_file": {
        "fn": _read_file,
        "desc": "讀取檔案內容（限 MAGI 工作目錄和案件資料夾）",
        "params": "path: str（檔案路徑）, max_chars: int（最大字元數，預設 3000）",
    },
    "calculate": {
        "fn": _calculate,
        "desc": "計算數學表達式",
        "params": "expression: str（算式，如 '100*1.05+500'）",
    },
    "current_time": {
        "fn": _get_current_time,
        "desc": "取得目前日期和時間",
        "params": "（無參數）",
    },
}


def get_tools() -> dict[str, dict[str, Any]]:
    """取得所有可用工具。"""
    return TOOLS.copy()


def get_tool_names() -> list[str]:
    """取得所有工具名稱。"""
    return list(TOOLS.keys())

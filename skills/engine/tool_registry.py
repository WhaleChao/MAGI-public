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
    """查詢案件資料庫（OSC）。直接走 DB，不繞 HTTP（避免 login_required 攔截）。"""
    try:
        from api.osc.utils import _osc_exec
        sql = """
            SELECT case_number, client_name, case_reason, court_case_no, status
            FROM cases
        """
        params: tuple = ()
        if query:
            like = f"%{query}%"
            sql += """
                WHERE case_number LIKE %s
                   OR client_name LIKE %s
                   OR court_case_no LIKE %s
                   OR laf_case_no LIKE %s
                   OR application_no LIKE %s
            """
            params = (like, like, like, like, like)
        sql += " ORDER BY updated_at DESC, created_date DESC LIMIT 5"
        rows, _err = _osc_exec(sql, params, fetch="all")
        if not rows:
            return f"查無與「{query}」相關的案件。" if query else "目前沒有任何案件記錄。"
        lines = []
        for c in rows[:5]:
            name = c.get("client_name", "")
            case_no = c.get("case_number", "") or c.get("court_case_no", "")
            reason = c.get("case_reason", "")
            status = c.get("status", "")
            lines.append(f"- {name} | {case_no} | {reason} | 狀態: {status}")
        return "\n".join(lines)
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
        import os as _os_tr2
        _server_port2 = _os_tr2.environ.get("MAGI_SERVER_PORT", "5002")
        resp = session.get(
            f"http://localhost:{_server_port2}/api/schedule",
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


def _search_judgments(keywords: str = "", court: str = "", max_results: int = 3, **_) -> str:
    """搜尋司法院判決全文系統。"""
    if not keywords:
        return "錯誤: 請提供搜尋關鍵字（例如：侵權行為、背信、強制執行）。"
    try:
        from skills.bridge.http_pool import get_session
        session = get_session()
        payload = {"skill": "judicial-web-search", "task": "search", "timeout_sec": 60,
                   "keywords": keywords, "max_results": min(max_results, 5)}
        if court:
            payload["court"] = court
        resp = session.post(f"{_tools_api_url()}/skills/run", json=payload, timeout=70)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                out = data.get("result") or data.get("output", "")
                return str(out)[:2000]
            return f"搜尋失敗: {data.get('error', '未知錯誤')}"
        return f"判決搜尋 API 回傳 {resp.status_code}"
    except Exception as e:
        return f"判決搜尋失敗: {e}"


def _search_statutes(query: str = "", **_) -> str:
    """搜尋台灣法規條文（民法、刑法、訴訟法等）。"""
    if not query:
        return "錯誤: 請提供搜尋關鍵字（例如：民法184條、過失傷害、強制執行法）。"
    try:
        from skills.bridge.http_pool import get_session
        session = get_session()
        resp = session.post(
            f"{_tools_api_url()}/skills/run",
            json={"skill": "statutes-vdb", "task": "search", "query": query, "timeout_sec": 30},
            timeout=40,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                out = data.get("result") or data.get("output", "")
                return str(out)[:2000]
            return f"法規搜尋失敗: {data.get('error', '未知錯誤')}"
        return f"法規搜尋 API 回傳 {resp.status_code}"
    except Exception as e:
        return f"法規搜尋失敗: {e}"


# 真實 skill 名稱對照（skills/ 目錄下的子目錄名，全部唯讀/分析類）
# 安全紅線：敏感 skill（laf-orchestrator / file-review-orchestrator / transcript-downloader /
# brain_manager / magi-autopilot）禁止加入此白名單 — 它們會寫入 runtime 或觸發 portal 操作，
# 只能透過管理員指令或 pipeline 直接 dispatch，不能讓 LLM 自主 run_skill 呼叫。
_ALLOWED_SKILLS: dict[str, str] = {
    # skill 目錄名: 說明
    "judicial-web-search": "搜尋司法院判決（用 task=search，params: keywords, max_results）",
    "statutes-vdb":        "搜尋法規條文（用 task=search，params: query）",
    "labor-law-calculator":"計算勞動法金額（資遣費、加班費等，task=run）",
    "contract-review":     "合約審閱分析（task=review，params: text 或 path）",
    "worldmonitor-intel":  "查詢全球/法律新聞（task=run）",
    "judgment-collector":  "依案由收集判決摘要（task=collect，params: case_reason）",
    # 2026-04-21 新增（6 個真實運作 skill，全部唯讀/分析類）
    "pdf-namer":           "PDF 檔名提案（task=propose，params: path）",
    "pdf-bookmarker":      "PDF 頁籤生成（task=run，params: path）",
    "translator":          "翻譯（task=translate，params: text, target_lang, mode）",
    "market-briefing":     "股市晨報/追蹤清單（task=list|brief，params: symbols）",
    "trial-prep":          "開庭準備摘要（task=prepare，params: case_no）",
    "osc-orchestrator":    "案件/當事人/帳務查詢（task=query，params: type, keyword）",
    "interpreter-empirical-classifier": "最高法院通譯裁判實證分類（task=classify|status|self_test）",
}


def _run_skill(skill_name: str = "", task: str = "run", params: str = "", **_) -> str:
    """執行 MAGI 技能（限白名單，唯讀/分析類）。

    可用技能（skill_name）：
      judicial-web-search  → 搜尋司法院判決 (task=search)
      statutes-vdb         → 搜尋法規條文 (task=search)
      labor-law-calculator → 計算勞動法金額 (task=run)
      contract-review      → 合約審閱 (task=review)
      worldmonitor-intel   → 法律新聞 (task=run)
      judgment-collector   → 依案由收集判決 (task=collect)
    """
    if not skill_name:
        skill_list = "\n".join(f"  {k}: {v}" for k, v in _ALLOWED_SKILLS.items())
        return f"錯誤: 沒有提供技能名稱。\n可用技能：\n{skill_list}"

    if skill_name not in _ALLOWED_SKILLS:
        skill_list = ", ".join(_ALLOWED_SKILLS.keys())
        return f"⛔ 技能 '{skill_name}' 不在允許清單。可用: {skill_list}"

    # 解析 params JSON
    params_dict: dict = {}
    if params:
        try:
            params_dict = json.loads(params) if isinstance(params, str) else params
        except Exception:
            params_dict = {}

    try:
        from skills.bridge.http_pool import get_session
        session = get_session()
        payload = {"skill": skill_name, "task": task, "timeout_sec": 60, **params_dict}
        resp = session.post(f"{_tools_api_url()}/skills/run", json=payload, timeout=70)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                out = data.get("result") or data.get("output", "")
                return str(out)[:2000]
            return f"技能失敗: {data.get('error', '未知錯誤')}"
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
        "desc": "網路搜尋最新資訊（天氣、時事、一般問題）",
        "params": "query: str（搜尋關鍵字）, num_results: int（結果數量，預設 5）",
    },
    "query_cases": {
        "fn": _query_cases,
        "desc": "查詢事務所案件資料庫（當事人、案號、案由、進行中/結案等）",
        "params": "query: str（案件關鍵字或案號）",
    },
    "search_judgments": {
        "fn": _search_judgments,
        "desc": "搜尋司法院判決全文系統（輸入法律關鍵字、罪名或爭點，回傳相關判決摘要）",
        "params": "keywords: str（搜尋詞，如「侵權行為」「背信」）, court: str（法院名稱，可空）, max_results: int（預設 3）",
    },
    "search_statutes": {
        "fn": _search_statutes,
        "desc": "搜尋台灣法規條文（民法、刑法、訴訟法、勞動法等）",
        "params": "query: str（法規關鍵字，如「民法184條」「強制執行法」）",
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
        "desc": "計算數學表達式（算術、利率、金額換算等）",
        "params": "expression: str（算式，如 '100*1.05+500'）",
    },
    "current_time": {
        "fn": _get_current_time,
        "desc": "取得目前日期和時間",
        "params": "（無參數）",
    },
    "run_skill": {
        "fn": _run_skill,
        "desc": (
            "執行 MAGI 技能（白名單保護）。"
            "可用 skill_name: judicial-web-search（判決搜尋）, statutes-vdb（法規）, "
            "labor-law-calculator（勞動計算）, contract-review（合約審閱）, "
            "worldmonitor-intel（法律新聞）, judgment-collector（案由判決）, "
            "interpreter-empirical-classifier（通譯判決實證分類）"
        ),
        "params": "skill_name: str, task: str（如 search/run/review）, params: str（JSON）",
    },
}


def get_tools() -> dict[str, dict[str, Any]]:
    """取得所有可用工具。"""
    return TOOLS.copy()


def get_tool_names() -> list[str]:
    """取得所有工具名稱。"""
    return list(TOOLS.keys())


# ── E4B Ensemble 用精簡工具集 ──
import re as _re

_REMEMBER_GATE_RE = _re.compile(r"(記住|請記得|記下來|幫我記|存起來|備忘|不要忘)")

_E4B_ALWAYS_TOOLS = {
    "search_memory", "web_search", "query_cases", "get_schedule",
    "calculate", "current_time", "summarize", "translate",
    "search_judgments", "search_statutes",  # 直接接 MAGI skill 的法律專用工具
    "run_skill",
}


def get_compact_tools(user_query: str = "") -> dict[str, dict[str, Any]]:
    """E4B ensemble 用工具集。

    常駐工具：search_memory, web_search, query_cases, get_schedule,
              calculate, current_time, summarize, translate,
              search_judgments, search_statutes, run_skill
    條件開啟：remember（使用者明確說「記住」等關鍵字才啟用）
    排除：read_file（路徑安全風險）
    """
    tools = {k: v for k, v in TOOLS.items() if k in _E4B_ALWAYS_TOOLS}
    if user_query and _REMEMBER_GATE_RE.search(user_query):
        tools["remember"] = TOOLS["remember"]
    return tools

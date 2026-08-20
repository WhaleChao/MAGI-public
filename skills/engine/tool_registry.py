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
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def _tools_api_url() -> str:
    try:
        from api.routing.service_registry import get_service_url
        return get_service_url("tools_api")
    except Exception:
        return "http://localhost:5003"


def _internal_api_headers() -> dict[str, str]:
    key = (
        os.environ.get("MAGI_EXTERNAL_API_KEY")
        or os.environ.get("MAGI_API_KEY")
        or ""
    ).strip()
    return {"X-API-Key": key} if key else {}

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


def _realtime_lookup(query: str = "", **_) -> str:
    """Read authoritative real-time weather/stock/fx data without LLM guessing."""
    if not str(query or "").strip():
        return "[REALTIME_UNAVAILABLE] 即時資料查詢需要明確問題。"
    try:
        from skills.engine.realtime_data_gateway import handle_realtime_query

        result = handle_realtime_query(str(query))
    except Exception as exc:
        logger.warning("authoritative realtime lookup failed", exc_info=True)
        return "[REALTIME_UNAVAILABLE] 權威即時資料來源目前無法使用，請稍後再試。"
    if not isinstance(result, dict):
        return "[REALTIME_UNAVAILABLE] 這不是可辨識的即時資料問題；請補充要查的天氣地點、股票代碼或匯率幣別。"
    if result.get("success") is True and result.get("reply"):
        # Successful observations are plain user-safe text.  Only failures
        # carry an internal sentinel so ReAct can stop before a second model
        # call; internal control markers must never leak into the final reply.
        return str(result["reply"])
    detail = str(result.get("refusal") or "權威即時資料來源沒有回傳可核對內容。")
    return "[REALTIME_UNAVAILABLE] " + detail


def _normalize_case_query(query: str = "") -> str:
    """Extract the real case search token from natural chat/tool text."""
    raw = str(query or "").strip()
    compact = re.sub(r"\s+", "", raw)
    magi_case = re.search(r"(20\d{2}-\d{4})", compact)
    if magi_case:
        return magi_case.group(1)

    cleaned = re.sub(
        r"^(?:請|麻煩|幫我|幫忙|協助)?(?:查詢|查一下|查|找|搜尋|案件查詢|查案件|案號)",
        "",
        compact,
    )
    cleaned = re.sub(r"(?:案件|案子|資料|資訊|狀態)$", "", cleaned)
    return cleaned.strip() or raw


_CASE_STATISTICS_HINT_RE = re.compile(r"(?:數量|筆數|多少|幾件|統計|總數)")


def _query_case_statistics(query: str) -> str:
    """Return deterministic office-only case counts for aggregate questions."""
    from api.osc.utils import _osc_exec

    text = re.sub(r"\s+", "", str(query or ""))
    filters: list[str] = []
    labels: list[str] = []
    if "法扶" in text or "法律扶助" in text:
        filters.append(
            "("
            "COALESCE(case_category, '') = '法律扶助案件' "
            "OR COALESCE(legal_aid_number, '') <> '' "
            "OR COALESCE(laf_case_no, '') <> '' "
            "OR COALESCE(application_no, '') <> ''"
            ")"
        )
        labels.append("法扶")
    if "刑事" in text:
        filters.append("(COALESCE(case_type, '') = '刑事' OR COALESCE(case_type, '') LIKE '%刑事%')")
        labels.append("刑事")
    elif "民事" in text:
        filters.append("(COALESCE(case_type, '') = '民事' OR COALESCE(case_type, '') LIKE '%民事%')")
        labels.append("民事")
    elif "行政" in text:
        filters.append("(COALESCE(case_type, '') = '行政' OR COALESCE(case_type, '') LIKE '%行政%')")
        labels.append("行政")

    final_closed = """
    (
        COALESCE(legal_aid_status, '') = '已結案'
        OR REPLACE(COALESCE(folder_path, ''), '\\\\', '/') LIKE '%/03_工作資料/10_結案/%'
        OR REPLACE(COALESCE(folder_path, ''), '\\\\', '/') LIKE '%/10_結案/%'
        OR (
            (COALESCE(status, '') LIKE '%已結案%' OR LOWER(COALESCE(status, '')) IN ('closed', 'close', 'done'))
            AND COALESCE(legal_aid_status, '') NOT IN ('已結案，待報結', '已結案，待送出')
            AND COALESCE(status, '') NOT LIKE '%結案中%'
            AND COALESCE(status, '') NOT LIKE '%待報結%'
            AND COALESCE(status, '') NOT LIKE '%待送出%'
        )
    )
    """
    scope = "all"
    wants_all = any(
        marker in text
        for marker in ("全部", "所有", "歷年", "兩者", "都列", "不分是否結案", "區分尚未最終結案與已最終結案")
    )
    if wants_all:
        scope = "all"
    elif any(marker in text for marker in ("進行中", "未結案", "在辦", "辦理中")):
        filters.append(f"NOT {final_closed}")
        scope = "active"
    elif "已結案" in text or "結案件數" in text:
        filters.append(final_closed)
        scope = "closed"

    # OSC displays template rows as navigation aids, not office cases.  Keep
    # aggregate answers aligned with the case list that staff actually use.
    filters.append(
        "NOT ("
        "COALESCE(client_name, '') = '範本' "
        "OR (COALESCE(client_name, '') LIKE '%範本%' AND COALESCE(case_number, '') LIKE '0000%') "
        "OR COALESCE(folder_path, '') LIKE '%0000-0000-範本%'"
        ")"
    )

    where = " AND ".join(filters) if filters else "1=1"
    row, _cfg = _osc_exec(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN {final_closed} THEN 0 ELSE 1 END) AS active_or_closing,
            SUM(CASE WHEN {final_closed} THEN 1 ELSE 0 END) AS final_closed
        FROM cases
        WHERE {where}
        """,
        fetch="one",
    )
    result = row or {}
    total = int(result.get("total") or 0)
    label = "".join(labels) + "案件" if labels else "案件"
    if scope == "active":
        return f"事務所案件資料庫目前記錄的進行中／尚未最終結案{label}共 {total} 件。（僅查本所資料庫）"
    if scope == "closed":
        return f"事務所案件資料庫目前記錄的已最終結案{label}共 {total} 件。（僅查本所資料庫）"
    active_or_closing = int(result.get("active_or_closing") or 0)
    closed = int(result.get("final_closed") or 0)
    return (
        f"事務所案件資料庫目前記錄的{label}共 {total} 件："
        f"其中 {active_or_closing} 件尚未最終結案，{closed} 件已最終結案。"
        "（僅查本所資料庫，不含判決庫、網路或其他外部資料）"
    )


def _query_cases(query: str = "", **_) -> str:
    """查詢案件資料庫（OSC）。直接走 DB，不繞 HTTP（避免 login_required 攔截）。"""
    try:
        from api.osc.utils import _osc_exec
        if _CASE_STATISTICS_HINT_RE.search(str(query or "")):
            return _query_case_statistics(query)
        normalized_query = _normalize_case_query(query)
        sql = """
            SELECT case_number, client_name, case_reason, court_case_no, status
            FROM cases
        """
        params: tuple = ()
        if normalized_query:
            like = f"%{normalized_query}%"
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
        _extra_roots = [
            str(Path.home() / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件")
        ]
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
        payload = {"skill": "judicial-web-search", "task": "search", "timeout_sec": 25,
                   "keywords": keywords, "max_results": min(max_results, 3)}
        if court:
            payload["court"] = court
        resp = session.post(f"{_tools_api_url()}/skills/run", json=payload, headers=_internal_api_headers(), timeout=35)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("success"):
                out = data.get("result") or data.get("output", "")
                return str(out)[:2000]
            return f"搜尋失敗: {data.get('error', '未知錯誤')}"
        return f"判決搜尋 API 回傳 {resp.status_code}"
    except Exception as e:
        return f"判決搜尋失敗: {e}"


def _statute_query_variants(query: str) -> list[str]:
    raw = str(query or "").strip()
    compact = re.sub(r"\s+", "", raw)
    variants = [raw]
    m = re.search(r"(民法|刑法|民事訴訟法|刑事訴訟法|行政訴訟法|家事事件法|消費者債務清理條例|強制執行法)第?(\d+(?:-\d+)?)(?:條)?(?:之(\d+))?", compact)
    if m:
        law = m.group(1)
        article = f"第 {m.group(2)}"
        if m.group(3):
            article += f" 之 {m.group(3)}"
        article += " 條"
        variants.insert(0, f"{law} {article}")
        variants.append(f"{law}\n{article}")
        if law == "民法" and m.group(2) == "184":
            variants.append("侵權行為 民法 第 184 條")
    seen = set()
    out = []
    for item in variants:
        item = str(item or "").strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _format_statute_items(data: dict, query: str) -> str:
    items = data.get("items") if isinstance(data, dict) else []
    if not isinstance(items, list) or not items:
        return ""
    lines = [f"法規搜尋完成：{query}"]
    for item in items[:5]:
        content = str(item.get("content") or "").strip()
        source = str(item.get("source") or "").strip()
        if not content:
            continue
        lines.append(f"- {content[:650]}" + (f"\n  來源：{source}" if source else ""))
    return "\n".join(lines).strip()


def _search_statutes_local(query: str, top_k: int = 5) -> str:
    script = MAGI_ROOT / "skills" / "statutes-vdb" / "action.py"
    if not script.exists():
        return ""
    for q in _statute_query_variants(query):
        compact_q = re.sub(r"\s+", "", q)
        exact_article = bool(re.search(
            r"(民法|刑法|民事訴訟法|刑事訴訟法|行政訴訟法|家事事件法|消費者債務清理條例|強制執行法)第?(\d+(?:-\d+)?)(?:條)?(?:之\d+)?",
            compact_q,
        ))
        payload = json.dumps({"query": q, "top_k": 1 if exact_article else top_k}, ensure_ascii=False)
        env = os.environ.copy()
        env["MAGI_ROOT_DIR"] = str(MAGI_ROOT)
        env["MAGI_ROOT"] = str(MAGI_ROOT)
        try:
            cp = subprocess.run(
                [sys.executable, str(script), "--task", f"search {payload}"],
                cwd=str(MAGI_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
            )
        except Exception:
            continue
        if cp.returncode != 0 or not (cp.stdout or "").strip():
            continue
        try:
            data = json.loads(cp.stdout)
        except Exception:
            continue
        formatted = _format_statute_items(data, q)
        if formatted:
            return formatted[:2000]
    return ""


def _search_statutes(query: str = "", **_) -> str:
    """搜尋台灣法規條文（民法、刑法、訴訟法等）。"""
    if not query:
        return "錯誤: 請提供搜尋關鍵字（例如：民法184條、過失傷害、強制執行法）。"
    local = _search_statutes_local(query)
    if local:
        return local
    try:
        from skills.bridge.http_pool import get_session
        session = get_session()
        for q in _statute_query_variants(query):
            resp = session.post(
                f"{_tools_api_url()}/skills/run",
                json={"skill": "statutes-vdb", "task": "search", "query": q, "timeout_sec": 30},
                headers=_internal_api_headers(),
                timeout=40,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    out = data.get("result") or data.get("output", "")
                    if out:
                        return str(out)[:2000]
                continue
            if resp.status_code in {401, 403}:
                continue
            return f"法規搜尋 API 回傳 {resp.status_code}"
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
    "interpreter-empirical-classifier": "最高法院通譯裁判實證研究（task=fetch|fetch_and_classify|classify|status|self_test）",
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
      interpreter-empirical-classifier → 通譯判決抓取與實證分類 (task=fetch_and_classify)
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
        timeout_sec = int(params_dict.pop("timeout_sec", 60) or 60) if isinstance(params_dict, dict) else 60
        payload = {"skill": skill_name, "task": task, "timeout_sec": timeout_sec}
        if isinstance(params_dict, dict) and params_dict:
            payload.update(params_dict)
        post_kwargs = {
            "json": payload,
            "headers": _internal_api_headers(),
            "timeout": max(70, timeout_sec + 10),
        }
        try:
            resp = session.post(f"{_tools_api_url()}/skills/run", **post_kwargs)
        except TypeError as exc:
            # Unit-test fakes and a few legacy bridge sessions do not accept
            # custom headers; retry without them while keeping runtime auth for
            # real HTTP sessions.
            if "headers" not in str(exc):
                raise
            post_kwargs.pop("headers", None)
            resp = session.post(f"{_tools_api_url()}/skills/run", **post_kwargs)
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


def _read_runtime_json(name: str) -> dict[str, Any]:
    """Read one fixed aggregate runtime artifact without exposing its path."""
    if Path(name).name != name or name in {"", ".", ".."}:
        return {}
    try:
        from api.runtime_paths import get_runtime_dir

        payload = json.loads((get_runtime_dir() / name).read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _system_health(**_) -> str:
    """Return aggregate, privacy-safe health evidence for conversational use."""
    function_health = _read_runtime_json("function_health_index_latest.json")
    business = _read_runtime_json("business_module_live_check_latest.json")
    if not function_health and not business:
        return "系統健康證據目前不可讀；我不會把缺少證據說成正常。"

    summary = function_health.get("summary") if isinstance(function_health.get("summary"), dict) else {}
    business_results = business.get("results") if isinstance(business.get("results"), list) else []
    failed_modules = [
        str(item.get("name") or "unknown")
        for item in business_results
        if isinstance(item, dict) and item.get("ok") is False
    ]
    payload = {
        "function_health": {
            "ok": function_health.get("ok"),
            "generated_at": function_health.get("generated_at"),
            "failed": int(summary.get("failed_health_count") or 0),
            "stale": int(summary.get("stale_health_count") or 0),
            "missing": int(summary.get("missing_health_count") or 0),
            "pending": int(summary.get("pending_occurrence_count") or 0),
        },
        "business_live": {
            "ok": business.get("ok", business.get("success")),
            "generated_at": business.get("generated_at"),
            "release_id": business.get("release_id"),
            "failed_checks": failed_modules,
        },
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _business_status(module: str = "all", **_) -> str:
    """Return aggregate module checks; never return case rows or raw evidence."""
    payload = _read_runtime_json("business_module_live_check_latest.json")
    results = payload.get("results") if isinstance(payload.get("results"), list) else []
    if not results:
        return "業務模組 LIVE 證據目前不可讀；我不會把缺少證據說成正常。"

    query = str(module or "all").strip().lower()
    aliases = {
        "法扶": ("laf_",),
        "laf": ("laf_",),
        "閱卷": ("file_review",),
        "file_review": ("file_review",),
        "筆錄": ("transcript",),
        "transcript": ("transcript",),
        "通知": ("notification",),
        "notification": ("notification",),
        "日曆": ("calendar",),
        "calendar": ("calendar",),
        "drive": ("drive_",),
    }
    needles = aliases.get(query, ())
    selected = []
    for item in results:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if needles and not any(needle in name.lower() for needle in needles):
            continue
        selected.append({"name": name, "ok": item.get("ok")})
    if needles and not selected:
        return f"目前沒有「{module}」可核對的業務健康證據。"
    return json.dumps(
        {
            "generated_at": payload.get("generated_at"),
            "release_id": payload.get("release_id"),
            "module": query,
            "checks": selected,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _evolution_status(**_) -> str:
    """Return aggregate candidate-only self-evolution backlog evidence."""
    try:
        from api.runtime_paths import get_runtime_dir

        path = get_runtime_dir() / "controlled-evolution" / "evolution.sqlite3"
        if path.is_symlink() or not path.is_file():
            return "目前沒有受控演化提案；這不代表系統完美，只代表尚無正式缺口進入演化帳本。"
        uri = f"file:{path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=3) as connection:
            rows = connection.execute(
                "SELECT component, risk, status, COUNT(*) FROM evolution_proposals "
                "GROUP BY component, risk, status ORDER BY component, risk, status"
            ).fetchall()
        counts = [
            {"component": str(row[0]), "risk": str(row[1]), "status": str(row[2]), "count": int(row[3])}
            for row in rows
        ]
        return json.dumps(
            {
                "candidate_only": True,
                "auto_deploy": False,
                "total": sum(item["count"] for item in counts),
                "groups": counts,
                "next_step": "驗證通過的候選仍須人類審查及不可變 release 程序",
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    except Exception:
        return "受控演化帳本目前不可驗證；我不會把缺少證據說成沒有改善需求。"


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
        # Memory is a persistent write.  The conversational pipeline already
        # owns an explicit, auditable memory flow, so the ReAct agent must not
        # be able to select this tool merely because a model inferred intent.
        "side_effect": "reversible_write",
    },
    "web_search": {
        "fn": _web_search,
        "desc": "網路搜尋最新新聞、時事與一般外部資訊",
        "params": "query: str（搜尋關鍵字）, num_results: int（結果數量，預設 5）",
    },
    "realtime_lookup": {
        "fn": _realtime_lookup,
        "desc": "從權威資料源查詢即時天氣、股價或匯率；來源失敗時不猜測",
        "params": "query: str（完整自然語言即時查詢）",
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
    "system_health": {
        "fn": _system_health,
        "desc": "讀取 MAGI 最新功能健康與業務 LIVE 聚合證據（不含案件或個資）",
        "params": "（無參數）",
    },
    "business_status": {
        "fn": _business_status,
        "desc": "查詢法扶、閱卷、筆錄、通知、日曆或 Drive 的聚合健康狀態",
        "params": "module: str（法扶/閱卷/筆錄/通知/日曆/drive/all）",
    },
    "evolution_status": {
        "fn": _evolution_status,
        "desc": "查看 MAGI 自己發現的模組改善缺口與隔離候選狀態（只回傳聚合資訊）",
        "params": "（無參數）",
    },
    "run_skill": {
        "fn": _run_skill,
        "desc": (
            "執行 MAGI 技能（白名單保護）。"
            "可用 skill_name: judicial-web-search（判決搜尋）, statutes-vdb（法規）, "
            "labor-law-calculator（勞動計算）, contract-review（合約審閱）, "
            "worldmonitor-intel（法律新聞）, judgment-collector（案由判決）, "
            "interpreter-empirical-classifier（通譯判決抓取與實證分類）"
        ),
        "params": "skill_name: str, task: str（如 search/run/review/fetch_and_classify）, params: str（JSON）",
        # Some allow-listed skills are read-only while other tasks fetch,
        # persist, or rewrite artifacts.  ReActEngine applies a task-level
        # allow-list before this dynamic tool may run autonomously.
        "side_effect": "dynamic",
    },
}

# Tools without an explicit declaration are deliberately read-only.  Keeping
# the metadata beside the callable lets the ReAct safety layer enforce the
# boundary even when a future prompt tries to select a persistent operation.
for _tool in TOOLS.values():
    _tool.setdefault("side_effect", "read_only")


_TOOL_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "search_memory": {"type": "object", "properties": {"query": {"type": "string"}, "top_k": {"type": "integer"}}, "required": ["query"], "additionalProperties": False},
    "remember": {"type": "object", "properties": {"content": {"type": "string"}, "source": {"type": "string"}}, "required": ["content"], "additionalProperties": False},
    "web_search": {"type": "object", "properties": {"query": {"type": "string"}, "num_results": {"type": "integer"}}, "required": ["query"], "additionalProperties": False},
    "realtime_lookup": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
    "query_cases": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
    "search_judgments": {"type": "object", "properties": {"keywords": {"type": "string"}, "court": {"type": "string"}, "max_results": {"type": "integer"}}, "required": ["keywords"], "additionalProperties": False},
    "search_statutes": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"], "additionalProperties": False},
    "summarize": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
    "translate": {"type": "object", "properties": {"text": {"type": "string"}, "target_lang": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
    "get_schedule": {"type": "object", "properties": {"date": {"type": "string"}}, "additionalProperties": False},
    "read_file": {"type": "object", "properties": {"path": {"type": "string"}, "max_chars": {"type": "integer"}}, "required": ["path"], "additionalProperties": False},
    "calculate": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"], "additionalProperties": False},
    "current_time": {"type": "object", "properties": {}, "additionalProperties": False},
    "system_health": {"type": "object", "properties": {}, "additionalProperties": False},
    "business_status": {"type": "object", "properties": {"module": {"type": "string"}}, "additionalProperties": False},
    "evolution_status": {"type": "object", "properties": {}, "additionalProperties": False},
    "run_skill": {"type": "object", "properties": {"skill_name": {"type": "string"}, "task": {"type": "string"}, "params": {"type": "string"}}, "required": ["skill_name", "task"], "additionalProperties": False},
}
for _tool_name, _tool_schema in _TOOL_INPUT_SCHEMAS.items():
    if _tool_name in TOOLS:
        TOOLS[_tool_name]["input_schema"] = _tool_schema


def get_tools() -> dict[str, dict[str, Any]]:
    """取得所有可用工具。"""
    return TOOLS.copy()


def get_tool_names() -> list[str]:
    """取得所有工具名稱。"""
    return list(TOOLS.keys())


# ── E4B Ensemble 用精簡工具集 ──
import re as _re

_E4B_ALWAYS_TOOLS = {
    "search_memory", "web_search", "realtime_lookup", "query_cases", "get_schedule",
    "calculate", "current_time", "summarize", "translate",
    "search_judgments", "search_statutes",  # 直接接 MAGI skill 的法律專用工具
    "run_skill", "system_health", "business_status", "evolution_status",
}


def get_compact_tools(user_query: str = "") -> dict[str, dict[str, Any]]:
    """E4B ensemble 用工具集。

    常駐工具：search_memory, web_search, query_cases, get_schedule,
              calculate, current_time, summarize, translate,
              search_judgments, search_statutes, run_skill
    排除：remember（持久寫入由主對話確認流程處理）、
          read_file（路徑安全風險）
    """
    tools = {k: v for k, v in TOOLS.items() if k in _E4B_ALWAYS_TOOLS}
    return tools

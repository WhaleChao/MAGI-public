"""
api/answer_provenance.py
========================
回答溯源 — 顯示答案的資料來源，方便使用者識別並引導修正錯誤資料

功能：
- build_provenance_footer()   → 針對事實性回答，生成簡短來源頁尾
- store_provenance()          → 儲存上一次回答的溯源資訊（doc_id / source）
- get_last_provenance()       → 讀取最近一次的溯源資訊（用於「這條不對」修正流程）

設計原則：
- 只在 COMPLEX tier（法條/案號/事實問題）顯示來源頁尾；SIMPLE 閒聊不加
- 對話記錄（chatlog / assistant_reply）不計入「有依據記憶」
- 無任何 grounded source 時，顯示「AI 訓練知識」警告
- 所有操作有 try/except，不影響主流程
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# ── 來源類型標籤對照表 ────────────────────────────────────────────────────────
# key: 來源字串中的子串（小寫），value: 對使用者顯示的標籤（None = 隱藏）
_SOURCE_LABELS: Dict[str, Optional[str]] = {
    "court_ruling":        "判決記錄",
    "judgment":            "判決記錄",
    "judgment_archive":    "判決記錄",
    "legal_statute":       "法條記錄",
    "statute":             "法條記錄",
    "statutes_vdb":        "法條資料庫",
    "user_confirmed":      "已確認事實",
    "user_stated":         "使用者陳述",
    "verified_fact":       "已驗證事實",
    "obsidian":            "個人筆記",
    "obsidian_note":       "個人筆記",
    "case_note":           "案件筆記",
    "laf":                 "法扶記錄",
    "laf_case":            "法扶記錄",
    "laf_portal":          "法扶記錄",
    "cases_db":            "案件資料庫",
    "web":                 "網路資料",
    "web_research":        "網路搜尋",
    "crawler":             "網路搜尋",
    # 對話記錄不顯示（雜訊）
    "assistant_reply":     None,
    "assistant_generated": None,
    "chatlog":             None,
}

# ── Runtime 路徑 ──────────────────────────────────────────────────────────────
_MAGI_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUNTIME_DIR = os.path.join(_MAGI_ROOT, ".runtime")
_PROVENANCE_FILE = os.path.join(_RUNTIME_DIR, "last_answer_provenance.json")
_PROVENANCE_LOCK = threading.Lock()

# 溯源記錄有效期：30 分鐘
_PROVENANCE_TTL_SEC = 1800


# ═════════════════════════════════════════════════════════════════════════════
# 內部 helpers
# ═════════════════════════════════════════════════════════════════════════════

def _label_source(source: str) -> Optional[str]:
    """
    Convert raw source string to human-readable label.
    Returns None to indicate this source type should be hidden.
    """
    if not source:
        return None
    src_lower = source.lower()
    for key, label in _SOURCE_LABELS.items():
        if key in src_lower:
            return label
    # Generic fallback — show as 記憶庫
    return "記憶庫"


def _extract_web_titles(web_context: str) -> List[str]:
    """
    Parse source titles from the (資料來源：...) prefix injected by web_research.
    Returns up to 2 titles, empty list if web_context has no source or is "無。".
    """
    if not web_context or web_context.strip() in ("無。", "無", ""):
        return []
    m = re.search(r"（資料來源：([^）]{1,300})）", web_context)
    if m:
        raw = m.group(1)
        titles = [t.strip() for t in raw.split(",") if t.strip()]
        return titles[:2]
    return []


def _meaningful_memories(memories: List[dict]) -> List[dict]:
    """
    Filter out chatlog / assistant noise; return knowledge-bearing memories
    with at least minimal confidence.
    """
    result = []
    for m in (memories or []):
        if not isinstance(m, dict):
            continue
        src = str(m.get("source", "")).lower()
        # Skip dialog log — it's not a grounded factual source
        if any(skip in src for skip in ("chatlog", "assistant_reply", "assistant_generated")):
            continue
        # Confidence or score threshold (0.15 = very permissive, just exclude zero/near-zero)
        conf = float(m.get("confidence", 0) or 0)
        score = float(m.get("score", 0) or 0)
        if max(conf, score) < 0.15:
            continue
        result.append(m)
    return result[:3]  # top 3 to keep footer short


# ═════════════════════════════════════════════════════════════════════════════
# Public API
# ═════════════════════════════════════════════════════════════════════════════

def build_provenance_footer(
    memories: List[dict],
    web_context: str,
    tier: str,
    risk_level: str = "SAFE",
) -> str:
    """
    Build a concise source attribution footer for factual answers.

    Returns empty string when:
    - tier is SIMPLE or GREETING (casual chat, no footer needed)
    - COMPLEX tier with no meaningful sources and risk_level is SAFE

    Returns a 2-line footer for COMPLEX tier with grounded sources, or
    a single warning line if the answer has no grounded source but mentions
    legal specifics (HIGH/MEDIUM risk).

    Format example (grounded):
        ─
        來源：記憶庫（判決記錄，信心 0.85）｜網路（司法院）
        有誤？請說「這條不對，正確是⋯⋯」

    Format example (ungrounded, HIGH risk):
        （⚠️ 此回答依 AI 訓練知識，法條號碼建議查閱原始法規）
    """
    if tier not in ("COMPLEX",):
        return ""

    parts: List[str] = []

    # ── 記憶庫來源 ─────────────────────────────────────────────
    good_mems = _meaningful_memories(memories)
    if good_mems:
        seen_labels: List[str] = []
        for m in good_mems:
            label = _label_source(str(m.get("source", "")))
            if label is None:
                continue  # hidden source type
            conf = float(m.get("confidence", 0) or 0)
            if conf >= 0.70:
                entry = f"{label}（信心 {conf:.2f}）"
            else:
                entry = label
            if entry not in seen_labels:
                seen_labels.append(entry)
        if seen_labels:
            parts.append("記憶庫：" + "、".join(seen_labels[:2]))

    # ── 網路來源 ───────────────────────────────────────────────
    web_titles = _extract_web_titles(web_context)
    if web_titles:
        parts.append("網路：" + "、".join(web_titles))

    # ── 無溯源來源 → 依風險等級決定是否顯示警告 ────────────────
    if not parts:
        if risk_level in ("HIGH", "MEDIUM"):
            return "（⚠️ 此回答依 AI 訓練知識，無記憶庫依據，法條號碼建議查閱原始法規）"
        return ""

    # ── 組合頁尾 ───────────────────────────────────────────────
    source_line = "來源：" + "｜".join(parts)
    correction_hint = "有誤？請說「這條不對，正確是⋯⋯」"
    return f"─\n{source_line}\n{correction_hint}"


def store_provenance(
    session_id: str,
    memories: List[dict],
    web_context: str,
    query: str,
) -> None:
    """
    Persist the provenance of the last answer so the correction flow can
    identify which doc_id to update/flag when user says "這條不對".

    Writes to .runtime/last_answer_provenance.json (atomic via tmp file).
    Safe to call from a background thread.
    """
    try:
        os.makedirs(_RUNTIME_DIR, exist_ok=True)
        good_mems = _meaningful_memories(memories)
        record = {
            "ts": time.time(),
            "session_id": session_id or "default",
            "query": (query or "")[:200],
            "memory_doc_ids": [
                m.get("doc_id") for m in good_mems if m.get("doc_id")
            ],
            "memory_sources": [
                str(m.get("source", "")) for m in good_mems
            ],
            "memory_contents": [
                str(m.get("content", ""))[:120] for m in good_mems
            ],
            "web_titles": _extract_web_titles(web_context),
            "has_web": bool(_extract_web_titles(web_context)),
            "has_grounded_memory": bool(good_mems),
        }
        tmp = _PROVENANCE_FILE + ".tmp"
        with _PROVENANCE_LOCK:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _PROVENANCE_FILE)
    except Exception as e:
        logger.debug("[provenance] store failed: %s", e)


def get_last_provenance(session_id: str = "") -> Optional[Dict]:
    """
    Load the most recent provenance record.
    Returns None if no record exists or the record is stale (> 30 min).
    """
    try:
        if not os.path.exists(_PROVENANCE_FILE):
            return None
        with _PROVENANCE_LOCK:
            with open(_PROVENANCE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        if not isinstance(data, dict):
            return None
        # Reject stale records
        if time.time() - float(data.get("ts", 0)) > _PROVENANCE_TTL_SEC:
            return None
        return data
    except Exception:
        return None


def format_correction_context(provenance: Dict) -> str:
    """
    Format a human-readable description of the last answer's sources
    for injection into the correction prompt so MAGI knows what to update.
    """
    if not provenance:
        return ""
    lines = []
    doc_ids = provenance.get("memory_doc_ids") or []
    sources = provenance.get("memory_sources") or []
    contents = provenance.get("memory_contents") or []
    for i, (did, src, cont) in enumerate(zip(doc_ids, sources, contents)):
        label = _label_source(src) or "記憶庫"
        lines.append(f"記憶 {i+1}：{label} / doc_id={did} / 內容摘要：「{cont}」")
    web = provenance.get("web_titles") or []
    if web:
        lines.append(f"網路來源：{', '.join(web)}")
    if not lines:
        return "（上一次回答無溯源記憶庫記錄）"
    return "\n".join(lines)

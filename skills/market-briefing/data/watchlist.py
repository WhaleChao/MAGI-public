#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data/watchlist.py — Watchlist 資料類型與管理函數
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── 路徑推導 ─────────────────────────────────────────────────────
_SKILL_DIR = Path(__file__).resolve().parent.parent  # market-briefing/
_MAGI_ROOT = _SKILL_DIR.parents[1]
_AGENT_DIR = _MAGI_ROOT / ".agent"
STATE_PATH = _AGENT_DIR / "market_watchlist.json"

# ── Stop words (duplicated to avoid circular import) ─────────────
STOP_WORDS = {
    "請", "幫我", "幫", "設定", "新增", "增加", "追蹤", "股票", "名單", "清單", "移除", "刪除",
    "減少", "不要", "再", "報", "預測", "晨報", "今日", "台灣", "美國", "台股", "美股",
    "和", "以及", "還有", "與", "請問", "我要", "可以", "先", "開始", "隔天", "每天",
}

_DEFAULT_STATE = {
    "watchlist": [],
    "first_prompt_date": "",
    "active_from_date": "",
    "last_report_date": "",
    "updated_at": "",
}


def _tz_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Taipei"))
    except Exception:
        return datetime.now()


def _load_json_ws(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s", __name__, exc_info=True)
    return default


def _save_json_ws(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@dataclass
class WatchItem:
    symbol: str
    label: str
    market: str  # TW / US / OTHER
    raw: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "symbol": self.symbol,
            "label": self.label,
            "market": self.market,
            "raw": self.raw,
        }


def _unique(items: List[WatchItem]) -> List[WatchItem]:
    seen = set()
    out: List[WatchItem] = []
    for it in items:
        k = it.symbol.upper()
        if k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out


def _tokenize(text: str) -> List[str]:
    s = str(text or "")
    s = re.sub(
        r"(追蹤股票|追蹤清單|我要追蹤|設定追蹤|更新追蹤|新增追蹤|增加追蹤|移除追蹤|修改追蹤|變更追蹤|調整追蹤)\s*[:：]?",
        " ",
        s,
    )
    for ch in [",", "，", "、", ";", "；", "\n", "\t", "|", "/", "＋", "+"]:
        s = s.replace(ch, " ")
    parts = [p.strip() for p in s.split(" ") if p.strip()]
    out: List[str] = []
    for p in parts:
        p = p.strip("：:()（）[]{}")
        if not p or p in STOP_WORDS:
            continue
        out.append(p)
    return out


def _resolve_tokens(text: str) -> List[WatchItem]:
    from data.fetcher import _get_twse_lookup
    tokens = _tokenize(text)
    tw = _get_twse_lookup()
    out: List[WatchItem] = []

    for tk in tokens:
        t = tk.strip()
        if not t:
            continue

        # explicit TW symbol formats
        if re.fullmatch(r"\d{4,5}", t):
            info = tw.get(t)
            if info:
                out.append(WatchItem(symbol=info["symbol"], label=info["label"], market="TW", raw=tk))
            else:
                out.append(WatchItem(symbol=f"{t}.TW", label=t, market="TW", raw=tk))
            continue

        if re.fullmatch(r"\d{4,5}\.(?i:tw|two)", t):
            code = t.split(".")[0]
            info = tw.get(code) or {}
            out.append(WatchItem(symbol=f"{code}.TW", label=str(info.get("label") or code), market="TW", raw=tk))
            continue

        # Chinese company name => TW
        if re.search(r"[\u4e00-\u9fff]", t):
            info = tw.get(t)
            if info:
                out.append(WatchItem(symbol=info["symbol"], label=info["label"], market="TW", raw=tk))
                continue
            # fallback: partial match in lookup names
            matched = None
            for k, v in tw.items():
                if k.startswith("_") or re.fullmatch(r"\d{4}", k):
                    continue
                if t in k:
                    matched = v
                    break
            if matched:
                out.append(WatchItem(symbol=matched["symbol"], label=matched["label"], market="TW", raw=tk))
                continue

        # US symbol
        if re.fullmatch(r"[A-Za-z]{1,6}", t):
            sym = t.upper()
            out.append(WatchItem(symbol=sym, label=sym, market="US", raw=tk))
            continue

        # generic symbol
        if re.fullmatch(r"[A-Za-z0-9._-]{2,12}", t):
            sym = t.upper()
            market = "US" if re.fullmatch(r"[A-Z]{1,6}", sym) else "OTHER"
            out.append(WatchItem(symbol=sym, label=sym, market=market, raw=tk))

    return _unique(out)


def _load_state() -> Dict[str, Any]:
    state = _load_json_ws(STATE_PATH, dict(_DEFAULT_STATE))
    if not isinstance(state, dict):
        state = dict(_DEFAULT_STATE)
    for k, v in _DEFAULT_STATE.items():
        state.setdefault(k, v)
    if not isinstance(state.get("watchlist"), list):
        state["watchlist"] = []
    return state


def _save_state(state: Dict[str, Any]) -> None:
    state["updated_at"] = _tz_now().isoformat()
    _save_json_ws(STATE_PATH, state)


def _watchlist_from_state(state: Dict[str, Any]) -> List[WatchItem]:
    out: List[WatchItem] = []
    for raw in state.get("watchlist") or []:
        if not isinstance(raw, dict):
            continue
        sym = str(raw.get("symbol") or "").strip()
        if not sym:
            continue
        out.append(
            WatchItem(
                symbol=sym,
                label=str(raw.get("label") or sym).strip(),
                market=str(raw.get("market") or "US").strip().upper(),
                raw=str(raw.get("raw") or "").strip(),
            )
        )
    return _unique(out)


def _first_prompt_message() -> str:
    return (
        "📈 股市晨報已啟用。\n"
        "今天先請你告訴我要追蹤哪些股票（可混合台股/美股），例如：\n"
        "- 追蹤股票：台積電、聯發科、AAPL、MSFT\n"
        "收到清單後，我會從隔天 08:30 開始每日回報預測。"
    )


def _format_watchlist(items: List[WatchItem]) -> str:
    if not items:
        return "（目前尚未設定追蹤股票）"
    tw = [f"{x.label} ({x.symbol})" for x in items if x.market == "TW"]
    us = [f"{x.label} ({x.symbol})" for x in items if x.market == "US"]
    other = [f"{x.label} ({x.symbol})" for x in items if x.market not in {"TW", "US"}]
    lines: List[str] = []
    if tw:
        lines.append("台股：" + "、".join(tw))
    if us:
        lines.append("美股：" + "、".join(us))
    if other:
        lines.append("其他：" + "、".join(other))
    return "\n".join(lines)

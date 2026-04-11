#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cmd/sector_cmd.py — 產業板塊分析指令
"""
from __future__ import annotations

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Tuple

from data.fetcher import _http_json
from data.indicators import _safe_mean
from data.perf_tracker import _DEFAULT_MODEL_PARAMS, _load_perf
from data.watchlist import WatchItem
from predict.predict_engine import _predict_one

# ── Shared fetcher cache helpers ─────────────────────────────────
from data.fetcher import _load_cache_fetcher, _save_cache_fetcher, _tz_now_str


# ── TWSE 產業代碼對照 ─────────────────────────────────────────────
_TWSE_SECTOR_NAMES: Dict[str, str] = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織纖維",
    "05": "電機機械", "06": "電器電纜", "07": "化學生技醫療",
    "21": "化學", "08": "玻璃陶瓷", "09": "造紙", "10": "鋼鐵",
    "11": "橡膠", "12": "汽車", "13": "電子", "14": "建材營造",
    "15": "航運", "16": "觀光餐旅", "17": "金融保險",
    "18": "貿易百貨", "19": "綜合", "20": "其他",
    "22": "生技醫療", "23": "油電燃氣", "24": "半導體",
    "25": "電腦及週邊設備", "26": "光電", "27": "通信網路",
    "28": "電子零組件", "29": "電子通路", "30": "資訊服務",
    "31": "其他電子", "32": "文化創意", "33": "農業科技",
    "34": "電子商務", "35": "綠能環保", "36": "數位雲端",
    "37": "運動休閒", "38": "居家生活",
}


def _resolve_sector_name(code: str) -> str:
    """Resolve numeric sector code to Chinese name."""
    return _TWSE_SECTOR_NAMES.get(code, code)


def _get_twse_sector_map() -> Dict[str, List[Dict[str, str]]]:
    """Return {sector_name: [{code, name, symbol}, ...]} from TWSE stock list."""
    cache = _load_cache_fetcher()
    today = _tz_now_str()
    stored = cache.get("twse_sector_map") if isinstance(cache.get("twse_sector_map"), dict) else {}
    if stored and str(stored.get("_date") or "") == today:
        return stored

    sector_map: Dict[str, List[Dict[str, str]]] = {"_date": today}
    try:
        rows = _http_json("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", timeout=20, insecure=True)
        if isinstance(rows, list):
            for r in rows:
                if not isinstance(r, dict):
                    continue
                code = str(r.get("公司代號") or "").strip()
                name = str(r.get("公司簡稱") or "").strip()
                sector = str(r.get("產業別") or r.get("產業類別") or "").strip()
                if not code or not re.fullmatch(r"\d{4,5}", code) or not sector:
                    continue
                if sector not in sector_map:
                    sector_map[sector] = []
                sector_map[sector].append({
                    "code": code, "name": name or code,
                    "symbol": f"{code}.TW",
                })
    except Exception:
        import logging
        logging.getLogger(__name__).debug("silent-catch at %s", __name__, exc_info=True)

    cache["twse_sector_map"] = sector_map
    _save_cache_fetcher(cache)
    return sector_map


def _find_peers(
    target_code: str,
    sector_map: Dict[str, List[Dict[str, str]]],
    max_peers: int = 8,
) -> Tuple[str, List[Dict[str, str]]]:
    """Find same-sector peers for a TW stock. Returns (sector_display_name, peer_list)."""
    for sector, members in sector_map.items():
        if sector.startswith("_"):
            continue
        if not isinstance(members, list):
            continue
        codes = [m["code"] for m in members if isinstance(m, dict)]
        if target_code in codes:
            peers = [m for m in members if isinstance(m, dict) and m["code"] != target_code]
            display = _resolve_sector_name(sector)
            return display, peers[:max_peers]
    return "", []


def _cmd_sector(text: str, mode: str = "deep") -> str:
    """產業板塊分析。"""
    if not text:
        return "⚠️ 請指定產業名稱，例如：--task sector --text \"半導體\""

    sector_map = _get_twse_sector_map()
    # Build reverse mapping: Chinese name → sector code
    name_to_code: Dict[str, str] = {}
    for code, name in _TWSE_SECTOR_NAMES.items():
        name_to_code[name] = code

    # Find matching sector
    matched_sector = ""
    matched_members: List[Dict[str, str]] = []
    text_clean = text.strip()

    # Try direct match by code
    if text_clean in sector_map and not text_clean.startswith("_"):
        matched_sector = text_clean
        matched_members = sector_map[text_clean]
    else:
        # Try Chinese name → code
        for name, code in name_to_code.items():
            if text_clean in name or name in text_clean:
                if code in sector_map and isinstance(sector_map[code], list):
                    matched_sector = code
                    matched_members = sector_map[code]
                    break

    # Fallback: search sector keys directly
    if not matched_sector:
        for sector, members in sector_map.items():
            if sector.startswith("_") or not isinstance(members, list):
                continue
            if text_clean in sector or sector in text_clean:
                matched_sector = sector
                matched_members = members
                break

    if not matched_sector:
        sectors = []
        for k in sorted(sector_map.keys()):
            if k.startswith("_") or not isinstance(sector_map[k], list):
                continue
            name = _resolve_sector_name(k)
            sectors.append(f"{name}({k})")
        return f"⚠️ 找不到「{text_clean}」產業。可用產業：\n" + "、".join(sectors)

    sector_display = _resolve_sector_name(matched_sector)
    lines = [
        f"📊 產業分析 — {sector_display}（{matched_sector}）",
        f"成分股數量：{len(matched_members)}",
        "",
    ]

    sample_size = min(len(matched_members), 10)
    sample = matched_members[:sample_size]

    perf = _load_perf()
    params = perf.get("model_params") if isinstance(perf.get("model_params"), dict) else dict(_DEFAULT_MODEL_PARAMS)

    stock_data: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(sample_size, 6)) as pool:
        futures = {}
        for m in sample:
            item = WatchItem(symbol=m["symbol"], label=m["name"], market="TW", raw=m["code"])
            futures[pool.submit(_predict_one, item, params, mode)] = m
        for fut in as_completed(futures):
            meta = futures[fut]
            try:
                r = fut.result(timeout=60)
                r["code"] = meta["code"]
                r["display_name"] = meta["name"]
                stock_data.append(r)
            except Exception:
                stock_data.append({"ok": False, "code": meta["code"], "display_name": meta["name"]})

    ok_data = [d for d in stock_data if d.get("ok")]
    if not ok_data:
        return f"⚠️ 無法取得「{matched_sector}」產業的股價數據。"

    ok_data.sort(key=lambda d: float(d.get("last") or 0), reverse=True)

    avg_pred = _safe_mean([float(d.get("pred_pct") or 0) for d in ok_data])
    avg_conf = _safe_mean([float(d.get("confidence") or 0) for d in ok_data])

    lines.append(f"【產業概覽】（取樣 {len(ok_data)} 家）")
    lines.append(f"整體預估方向：{'多方' if avg_pred >= 0 else '空方'}（平均 {avg_pred:+.2f}%）")
    lines.append(f"平均信心度：{avg_conf:.0f}%")
    lines.append("")

    if mode in {"technical", "deep"}:
        rsi_vals = [float(d.get("rsi") or 50) for d in ok_data if d.get("rsi") is not None]
        macd_bulls = sum(1 for d in ok_data if float(d.get("macd_hist") or 0) > 0)
        if rsi_vals:
            avg_rsi = _safe_mean(rsi_vals)
            rsi_label = "超買" if avg_rsi > 70 else ("超賣" if avg_rsi < 30 else "中性")
            lines.append("【技術面共識】")
            lines.append(f"平均 RSI(14)：{avg_rsi:.1f}（{rsi_label}）")
            lines.append(f"MACD 多頭比例：{macd_bulls}/{len(ok_data)}（{macd_bulls/len(ok_data)*100:.0f}%）")
            lines.append("")

    if mode == "deep":
        vol_trends = [str(d.get("volume_trend") or "") for d in ok_data if d.get("volume_trend")]
        if vol_trends:
            vt_counts = Counter(vol_trends)
            dominant = vt_counts.most_common(1)[0] if vt_counts else ("無資料", 0)
            lines.append("【資金流向】")
            lines.append(f"量能共識：{dominant[0]}（{dominant[1]}/{len(vol_trends)} 家）")
            adx_vals = [float(d.get("adx") or 0) for d in ok_data if d.get("adx") is not None]
            if adx_vals:
                avg_adx = _safe_mean(adx_vals)
                strength = "強趨勢" if avg_adx > 25 else ("弱趨勢" if avg_adx > 15 else "盤整")
                lines.append(f"平均 ADX：{avg_adx:.1f}（{strength}）")
            lines.append("")

    lines.append("【成分股表現】")
    for d in ok_data:
        name = str(d.get("display_name") or d.get("label") or "?")[:6]
        code = str(d.get("code") or "")
        last = float(d.get("last") or 0)
        pred_pct = float(d.get("pred_pct") or 0)
        conf = int(d.get("confidence") or 0)
        icon = "↗" if pred_pct >= 0 else "↘"
        line = f"  {name}({code}) {icon} {last:.2f} → {pred_pct:+.2f}% 信心{conf}%"
        if mode in {"technical", "deep"} and d.get("rsi") is not None:
            line += f" RSI={float(d['rsi']):.0f}"
        lines.append(line)

    lines.append("")
    lines.append("註：僅分析部分成分股，完整分析請搭配 export 功能匯出 Excel。")
    return "\n".join(lines)

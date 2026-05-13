#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cmd/comps_cmd.py — 同業比較分析指令
"""
from __future__ import annotations

import math
import re
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from data.fetcher import _latest_tw_financials, _yahoo_history
from data.indicators import _pct
from data.watchlist import _resolve_tokens
from mbcmd.sector_cmd import _find_peers, _get_twse_sector_map


def _fetch_comps_metrics(symbol: str, code4: str, market: str) -> Dict[str, Any]:
    """Fetch price + TWSE financials for one stock (no v10 API needed)."""
    metrics: Dict[str, Any] = {"symbol": symbol, "code": code4, "ok": False}
    try:
        closes, _ = _yahoo_history(symbol, period="3mo")
        if not closes:
            return metrics
        metrics["price"] = closes[-1]
        metrics["name"] = symbol

        if market == "TW":
            twf = _latest_tw_financials(code4)
            rev_str = twf.get("rev") or ""
            yoy_match = re.search(r"YoY\s*([-+]?[\d.]+)", rev_str)
            metrics["rev_yoy"] = float(yoy_match.group(1)) if yoy_match else None
            eps_str = twf.get("eps") or ""
            eps_match = re.search(r"EPS\s*([-+]?[\d.]+)", eps_str)
            metrics["eps_val"] = float(eps_match.group(1)) if eps_match else None
            metrics["rev_raw"] = rev_str
            metrics["eps_raw"] = eps_str
            if metrics.get("eps_val") and metrics["eps_val"] > 0:
                metrics["pe"] = round(closes[-1] / (metrics["eps_val"] * 4), 1)
            if len(closes) >= 20:
                metrics["mom_20d"] = round(_pct(closes[-1], closes[-20]), 2)
            if len(closes) >= 5:
                metrics["mom_5d"] = round(_pct(closes[-1], closes[-5]), 2)

        metrics["ok"] = True
    except Exception:
        import logging
        logging.getLogger(__name__).debug("silent-catch at %s", __name__, exc_info=True)
    return metrics


def _cmd_comps(text: str) -> str:
    """同業比較分析。"""
    if not text:
        return "⚠️ 請指定目標公司，例如：--task comps --text \"台積電\""

    items = _resolve_tokens(text)
    if not items:
        return "⚠️ 無法解析股票代號，請用公司名稱或代號。"

    target = items[0]
    if target.market != "TW":
        return f"⚠️ 同業比較目前僅支援台股（收到：{target.label} {target.symbol}）。美股版本開發中。"

    target_code = target.symbol.split(".")[0]
    sector_map = _get_twse_sector_map()
    sector_name, peers = _find_peers(target_code, sector_map)
    if not sector_name:
        return f"⚠️ 找不到 {target.label}({target_code}) 的產業分類，可能非上市股。"
    if not peers:
        return f"⚠️ {target.label} 所屬產業「{sector_name}」中找不到其他同業公司。"

    lines = [
        f"📊 同業比較分析 — {target.label}({target_code})",
        f"產業分類：{sector_name}｜同業數：{len(peers)}",
        "",
    ]

    all_items = [{"symbol": target.symbol, "code": target_code, "name": target.label, "is_target": True}]
    for p in peers:
        all_items.append({"symbol": p["symbol"], "code": p["code"], "name": p["name"], "is_target": False})

    results: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(len(all_items), 8)) as pool:
        futures = {
            pool.submit(_fetch_comps_metrics, it["symbol"], it["code"], "TW"): it
            for it in all_items
        }
        for fut in as_completed(futures):
            meta = futures[fut]
            try:
                r = fut.result(timeout=30)
                r["is_target"] = meta["is_target"]
                r["display_name"] = meta["name"]
                results.append(r)
            except Exception:
                results.append({"ok": False, "is_target": meta["is_target"], "display_name": meta["name"], "code": meta["code"]})

    ok_results = [r for r in results if r.get("ok")]
    if len(ok_results) < 2:
        return f"⚠️ 取得的財務數據不足（僅 {len(ok_results)} 家），無法做有效比較。"

    def _median(vals: List[float]) -> Optional[float]:
        clean = [v for v in vals if v is not None and not math.isnan(v)]
        if not clean:
            return None
        return statistics.median(clean)

    peer_results = [r for r in ok_results if not r.get("is_target")]
    pe_med = _median([r.get("pe") for r in peer_results if r.get("pe") is not None])
    yoy_med = _median([r.get("rev_yoy") for r in peer_results if r.get("rev_yoy") is not None])
    eps_med = _median([r.get("eps_val") for r in peer_results if r.get("eps_val") is not None])
    mom20_med = _median([r.get("mom_20d") for r in peer_results if r.get("mom_20d") is not None])

    lines.append("【估值比較表】")
    header = f"{'公司':<8} {'股價':>8} {'P/E(估)':>8} {'EPS':>8} {'營收YoY%':>9} {'20日漲跌%':>9}"
    lines.append(header)
    lines.append("-" * len(header))

    def _fmt(v, fmt=".1f"):
        if v is None:
            return "n/a"
        return f"{v:{fmt}}"

    def _tag(v, median_v, higher_is_better=True):
        if v is None or median_v is None:
            return ""
        if higher_is_better:
            return " ▲" if v > median_v else (" ▼" if v < median_v else "")
        else:
            return " ▼" if v > median_v else (" ▲" if v < median_v else "")

    target_r = [r for r in ok_results if r.get("is_target")]
    peer_r = sorted(
        [r for r in ok_results if not r.get("is_target")],
        key=lambda x: float(x.get("price") or 0),
        reverse=True,
    )

    for r in target_r + peer_r:
        name = str(r.get("display_name") or r.get("code") or "?")[:6]
        marker = "★" if r.get("is_target") else " "
        price = r.get("price")
        pe = r.get("pe")
        eps = r.get("eps_val")
        yoy = r.get("rev_yoy")
        mom20 = r.get("mom_20d")
        row = (
            f"{marker}{name:<7} "
            f"{_fmt(price, '.0f'):>8} "
            f"{_fmt(pe):>8}{_tag(pe, pe_med, False)} "
            f"{_fmt(eps, '.2f'):>8}{_tag(eps, eps_med, True)} "
            f"{_fmt(yoy):>9}{_tag(yoy, yoy_med, True)} "
            f"{_fmt(mom20):>9}{_tag(mom20, mom20_med, True)}"
        )
        lines.append(row)

    lines.append("")
    lines.append(f"同業中位數: P/E={_fmt(pe_med)} EPS={_fmt(eps_med, '.2f')} 營收YoY={_fmt(yoy_med)}% 20日漲跌={_fmt(mom20_med)}%")

    target_data = target_r[0] if target_r else {}
    if target_data.get("ok") and pe_med is not None:
        t_pe = target_data.get("pe")
        if t_pe is not None:
            ratio = t_pe / pe_med if pe_med > 0 else 1.0
            if ratio > 1.15:
                verdict = "估值偏高（P/E 高於同業中位數 {:.0f}%）".format((ratio - 1) * 100)
            elif ratio < 0.85:
                verdict = "估值偏低（P/E 低於同業中位數 {:.0f}%）".format((1 - ratio) * 100)
            else:
                verdict = "估值合理（P/E 接近同業中位數）"
            lines.append(f"\n💡 結論：{target.label} {verdict}")

    lines.append("\n註：數據來源為 Yahoo Finance / TWSE OpenAPI，僅供參考。")
    return "\n".join(lines)

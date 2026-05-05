#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
predict/predict_engine.py — 預測引擎
"""
from __future__ import annotations

import logging
import statistics
from typing import Any, Callable, Dict, List, Optional

from data.indicators import (
    _adx_approx,
    _bbands,
    _clamp,
    _ema,
    _macd,
    _pct,
    _rsi,
    _safe_mean,
    _support_resistance,
    _volume_trend,
)
from data.fetcher import (
    _latest_tw_financials,
    _latest_us_filing,
    _yahoo_history,
)
from market_news import fetch_market_news, format_news_for_prompt
from data.perf_tracker import _predict_pct_by_params
from data.watchlist import WatchItem, _format_watchlist


def _predict_one(
    item: WatchItem,
    params: Dict[str, float],
    mode: str = "quick",
    committee_callback: Optional[Callable[[WatchItem, str, Dict[str, Any]], Optional[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "symbol": item.symbol,
        "label": item.label,
        "market": item.market,
        "ok": False,
        "line": "",
        "mode": mode,
    }
    try:
        # technical/deep modes need more history for MACD(26+9) and BBands(20)
        period = "6mo" if mode in {"technical", "deep"} else "3mo"
        highs_data: List[float] = []
        lows_data: List[float] = []
        volumes_data: List[int] = []
        if mode == "deep":
            closes, _, highs_data, lows_data, volumes_data = _yahoo_history(item.symbol, period=period, with_volume=True)
        else:
            closes, _ = _yahoo_history(item.symbol, period=period)
        if len(closes) < 25:
            out["line"] = f"{item.label}({item.symbol})：歷史資料不足，暫時無法估算。"
            return out

        last = closes[-1]
        prev = closes[-2]
        ema5 = _ema(closes[-20:], 5)
        ema20 = _ema(closes[-60:], 20)
        mom1 = _pct(last, prev)
        mom5 = _pct(last, closes[-6]) if len(closes) >= 6 else mom1

        returns = []
        for i in range(1, min(15, len(closes))):
            returns.append(_pct(closes[-i], closes[-i - 1]))
        vol = statistics.pstdev(returns) if len(returns) > 1 else 0.0

        trend = _pct(ema5, ema20)
        signal = _predict_pct_by_params(trend, mom5, vol, params)
        pred_pct = signal
        pred_price = last * (1 + pred_pct / 100.0)
        conf = int(_clamp(78 - (abs(vol) * 4.5) - (abs(pred_pct) * 2.5), 35, 86))

        # fundamentals
        fund = ""
        if item.market == "TW":
            code = item.symbol.split(".")[0]
            twf = _latest_tw_financials(code)
            parts = [x for x in [twf.get("rev") or "", twf.get("eps") or ""] if x]
            fund = "；".join(parts)
        elif item.market == "US":
            fund = _latest_us_filing(item.symbol)

        dir_icon = "↗" if pred_pct >= 0 else "↘"
        core = (
            f"{item.label} ({item.symbol}) {dir_icon} 昨收 {last:.2f}，"
            f"預估次一交易日 {pred_price:.2f} ({pred_pct:+.2f}%)，"
            f"信心 {conf}%"
        )
        if fund:
            core += f"｜財報訊號：{fund}"

        tech_dict: Dict[str, Any] = {}
        if mode in {"technical", "deep"}:
            rsi_val = _rsi(closes)
            rsi_label = "超買" if rsi_val > 70 else ("超賣" if rsi_val < 30 else "中性")
            macd_line, sig_line, macd_hist = _macd(closes)
            macd_label = "多頭" if macd_hist > 0 else "空頭"
            bb_upper, bb_mid, bb_lower = _bbands(closes)
            bb_pct = (
                (last - bb_lower) / (bb_upper - bb_lower) * 100
                if (bb_upper - bb_lower) > 1e-9 else 50.0
            )
            bb_label = "上軌附近" if bb_pct > 80 else ("下軌附近" if bb_pct < 20 else "中軌區間")
            tech_line = (
                f"  RSI(14)={rsi_val:.1f}（{rsi_label}）"
                f"｜MACD柱={macd_hist:+.3f}（{macd_label}）"
                f"｜BBands %B={bb_pct:.0f}%（{bb_label}）"
            )
            core += "\n" + tech_line
            tech_dict = {
                "rsi": round(rsi_val, 2),
                "macd_line": round(macd_line, 4),
                "macd_signal": round(sig_line, 4),
                "macd_hist": round(macd_hist, 4),
                "bb_upper": round(bb_upper, 2),
                "bb_mid": round(bb_mid, 2),
                "bb_lower": round(bb_lower, 2),
                "bb_pct": round(bb_pct, 1),
            }

        # ── deep 模式獨有：成交量、支撐阻力、趨勢強度 ──
        deep_dict: Dict[str, Any] = {}
        if mode == "deep" and highs_data and lows_data:
            support, resistance = _support_resistance(highs_data, lows_data, closes)
            vol_trend = _volume_trend(volumes_data) if volumes_data else "無資料"
            adx = _adx_approx(highs_data, lows_data, closes)
            trend_strength = "強趨勢" if adx > 25 else ("弱趨勢" if adx > 15 else "盤整")

            sup_dist = _pct(last, support) if support > 0 else 0
            res_dist = _pct(resistance, last) if resistance > 0 else 0

            deep_line = (
                f"  支撐={support:.2f}（距 {sup_dist:.1f}%）"
                f"｜阻力={resistance:.2f}（距 {res_dist:.1f}%）"
                f"｜量能={vol_trend}"
                f"｜ADX={adx:.1f}（{trend_strength}）"
            )
            core += "\n" + deep_line
            deep_dict = {
                "support": round(support, 2),
                "resistance": round(resistance, 2),
                "volume_trend": vol_trend,
                "adx": round(adx, 1),
                "trend_strength": trend_strength,
            }

        out.update({
            "ok": True,
            "last": last,
            "pred_price": pred_price,
            "pred_pct": pred_pct,
            "trend": trend,
            "mom5": mom5,
            "vol": vol,
            "signal": signal,
            "confidence": conf,
            "line": core,
            **tech_dict,
            **deep_dict,
        })

        # ── MAGI v2 Upgrade: Multi-Agent Committee Deliberation ──
        if mode == "deep" and committee_callback is not None:
            try:
                news_items = fetch_market_news(
                    item.symbol,
                    label=item.label,
                    market=item.market,
                    max_items=5,
                )
                news_lines = format_news_for_prompt(news_items, max_items=5)
                market_data = {
                    "closes": closes,
                    "vol": vol,
                    "trend": trend,
                    "mom5": mom5,
                    "fundamentals": {
                        "rev": fund.split("｜")[0] if fund else "N/A",
                        "eps": fund.split("｜")[1] if "｜" in fund else "N/A",
                    },
                    "news": news_lines,
                    "news_sources": news_items,
                    "data_quality": {
                        "price_source": "Yahoo Finance chart API",
                        "fundamental_source": "TWSE OpenAPI" if item.market == "TW" else ("SEC submissions" if item.market == "US" else "N/A"),
                        "news_source": "Google News RSS" if news_items else "unavailable",
                    },
                }
                committee_result = committee_callback(item, mode, market_data)
                if committee_result:
                    committee_result = dict(committee_result)
                    committee_line = str(committee_result.pop("line", "") or "").strip()
                    out.update(committee_result)
                    if committee_line:
                        out["committee_line"] = committee_line
                        out["line"] = f"{out['line']}\n  {committee_line}"
            except Exception as ce:
                logging.getLogger(__name__).error(
                    "Committee callback failed for %s: %s", item.symbol, ce
                )

        return out
    except Exception as e:
        out["line"] = f"{item.label}({item.symbol})：資料取得失敗（{type(e).__name__}）"
        return out


def _render_report(items: List[WatchItem], rows: List[Dict[str, Any]], mode: str = "quick") -> str:
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Taipei"))
    except Exception:
        now = datetime.now()

    mode_tag = {"quick": "快速", "technical": "技術分析", "deep": "深度"}.get(mode, mode)
    title = f"📊 MAGI 每日股價預測・{mode_tag}模式（{now.strftime('%Y-%m-%d %H:%M')}）"
    head = [
        title,
        "",
        "追蹤清單：",
        _format_watchlist(items),
        "",
    ]

    tw_lines = [r["line"] for r in rows if r.get("market") == "TW"]
    us_lines = [r["line"] for r in rows if r.get("market") == "US"]
    other_lines = [r["line"] for r in rows if r.get("market") not in {"TW", "US"}]

    body: List[str] = []
    body.extend(["【台股】"])
    if tw_lines:
        body.extend([f"- {x}" for x in tw_lines])
    else:
        body.append("- （今日無台股追蹤標的）")
    body.append("")

    body.extend(["【美股】"])
    if us_lines:
        body.extend([f"- {x}" for x in us_lines])
    else:
        body.append("- （今日無美股追蹤標的）")
    body.append("")

    if other_lines:
        body.extend(["【其他】", *[f"- {x}" for x in other_lines], ""])

    ok_rows = [r for r in rows if r.get("ok")]
    avg_pred = _safe_mean([float(r.get("pred_pct") or 0.0) for r in ok_rows], default=0.0)
    risk = "偏高" if abs(avg_pred) > 2.2 else "中性"
    foot = [
        f"整體偏向：{('多方' if avg_pred >= 0 else '空方')}（平均預估 {avg_pred:+.2f}% / 風險 {risk}）",
        "註：此為統計模型推估，非投資建議。",
    ]
    return "\n".join(head + body + foot).strip()

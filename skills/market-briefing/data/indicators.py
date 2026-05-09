#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data/indicators.py — TA 技術指標計算函數
零外部依賴（只用標準庫）
"""
from __future__ import annotations

import statistics
from typing import Iterable, List, Tuple


def _ema(values: List[float], span: int) -> float:
    if not values:
        return 0.0
    alpha = 2.0 / (span + 1.0)
    e = values[0]
    for v in values[1:]:
        e = alpha * v + (1 - alpha) * e
    return e


def _pct(a: float, b: float) -> float:
    if abs(b) < 1e-9:
        return 0.0
    return (a - b) / b * 100.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _rsi(closes: List[float], period: int = 14) -> float:
    """Relative Strength Index (0–100). >70 overbought, <30 oversold."""
    if len(closes) < period + 1:
        return 50.0
    gains = losses = 0.0
    for i in range(-period, 0):
        d = closes[i] - closes[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    if losses < 1e-9:
        return 100.0
    rs = (gains / period) / (losses / period)
    return 100.0 - 100.0 / (1.0 + rs)


def _macd(closes: List[float], fast: int = 12, slow: int = 26, sig: int = 9) -> Tuple[float, float, float]:
    """Returns (macd_line, signal_line, histogram). Positive histogram = bullish momentum."""
    if len(closes) < slow + sig:
        return 0.0, 0.0, 0.0
    alpha_f = 2.0 / (fast + 1)
    alpha_s = 2.0 / (slow + 1)
    alpha_sig = 2.0 / (sig + 1)
    ema_f = ema_s = closes[0]
    macd_vals: List[float] = []
    for c in closes[1:]:
        ema_f = alpha_f * c + (1 - alpha_f) * ema_f
        ema_s = alpha_s * c + (1 - alpha_s) * ema_s
        macd_vals.append(ema_f - ema_s)
    if not macd_vals:
        return 0.0, 0.0, 0.0
    sig_v = macd_vals[0]
    for m in macd_vals[1:]:
        sig_v = alpha_sig * m + (1 - alpha_sig) * sig_v
    macd_line = macd_vals[-1]
    return macd_line, sig_v, macd_line - sig_v


def _bbands(closes: List[float], period: int = 20, k: float = 2.0) -> Tuple[float, float, float]:
    """Returns (upper, middle, lower) Bollinger Bands."""
    if len(closes) < period:
        mid = closes[-1]
        return mid, mid, mid
    window = closes[-period:]
    mid = sum(window) / period
    std = (sum((c - mid) ** 2 for c in window) / period) ** 0.5
    return mid + k * std, mid, mid - k * std


def _support_resistance(highs: List[float], lows: List[float], closes: List[float], n: int = 20) -> Tuple[float, float]:
    """簡易支撐/阻力：近 N 日最低低點為支撐，最高高點為阻力。"""
    if not highs or not lows:
        last = closes[-1] if closes else 0
        return last, last
    h = highs[-n:] if len(highs) >= n else highs
    lo = lows[-n:] if len(lows) >= n else lows
    return min(lo), max(h)


def _volume_trend(volumes: List[int], span: int = 5) -> str:
    """成交量趨勢：近 span 日均量 vs 前 span 日均量。"""
    if len(volumes) < span * 2:
        return "資料不足"
    recent = sum(volumes[-span:]) / span
    prior = sum(volumes[-span * 2:-span]) / span
    if prior < 1:
        return "無成交"
    ratio = recent / prior
    if ratio > 1.5:
        return "爆量"
    if ratio > 1.15:
        return "增量"
    if ratio < 0.7:
        return "縮量"
    if ratio < 0.85:
        return "略減"
    return "持平"


def _adx_approx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> float:
    """ADX 近似值（趨勢強度 0-100）。"""
    n = len(closes)
    if n < period + 2:
        return 0.0
    plus_dm, minus_dm, tr_list = [], [], []
    for i in range(1, n):
        h_diff = highs[i] - highs[i - 1]
        l_diff = lows[i - 1] - lows[i]
        plus_dm.append(max(h_diff, 0) if h_diff > l_diff else 0)
        minus_dm.append(max(l_diff, 0) if l_diff > h_diff else 0)
        tr_list.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    # Smoothed averages (Wilder's method)
    sm_plus = sum(plus_dm[:period])
    sm_minus = sum(minus_dm[:period])
    sm_tr = sum(tr_list[:period])
    dx_vals = []
    for i in range(period, len(plus_dm)):
        sm_plus = sm_plus - sm_plus / period + plus_dm[i]
        sm_minus = sm_minus - sm_minus / period + minus_dm[i]
        sm_tr = sm_tr - sm_tr / period + tr_list[i]
        if sm_tr < 1e-9:
            continue
        di_plus = sm_plus / sm_tr * 100
        di_minus = sm_minus / sm_tr * 100
        di_sum = di_plus + di_minus
        if di_sum < 1e-9:
            continue
        dx_vals.append(abs(di_plus - di_minus) / di_sum * 100)
    if not dx_vals:
        return 0.0
    adx = dx_vals[0]
    for dx in dx_vals[1:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def _safe_mean(values: Iterable[float], default: float = 0.0) -> float:
    arr = [x for x in values if isinstance(x, (int, float))]
    if not arr:
        return default
    return float(sum(arr) / len(arr))

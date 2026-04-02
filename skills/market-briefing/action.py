#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
market-briefing/action.py

每日台股/美股追蹤與晨報（08:30）
- 第一天先詢問追蹤清單
- 設定追蹤後，隔天開始晨報
- 之後可隨時新增/移除追蹤
- 報告含：近期價格趨勢 + 財報/申報摘要（台股公開資訊、SEC 申報）
"""

from __future__ import annotations
import logging

import argparse
import json
import math
import os
import re
import ssl
import statistics
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import parse, request

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None


def _tz_now() -> datetime:
    if ZoneInfo:
        return datetime.now(ZoneInfo("Asia/Taipei"))
    return datetime.now()


def _skill_python() -> str:
    """Return the interpreter used to run this skill, with a safe fallback."""
    return (os.environ.get("MAGI_SKILL_PYTHON") or sys.executable or "python3").strip() or "python3"


_DEFAULT_MAGI_ROOT = Path(__file__).resolve().parents[2]
MAGI_ROOT = Path(os.environ.get("MAGI_ROOT", str(_DEFAULT_MAGI_ROOT)))
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))
AGENT_DIR = MAGI_ROOT / ".agent"
STATE_PATH = AGENT_DIR / "market_watchlist.json"
CACHE_PATH = AGENT_DIR / "market_data_cache.json"
PERF_PATH = AGENT_DIR / "market_perf_history.json"
NOTIFY_LOG_PATH = MAGI_ROOT / "static" / "market_briefing_notify.log"

_DEFAULT_STATE = {
    "watchlist": [],
    "first_prompt_date": "",
    "active_from_date": "",
    "last_report_date": "",
    "updated_at": "",
}

_DEFAULT_MODEL_PARAMS = {
    "w_trend": 0.55,
    "w_mom": 0.45,
    "w_vol": 0.18,
    "bias": 0.0,
    "updated_at": "",
}

COMMON_TW_ALIASES = {
    "台積電": "2330",
    "聯發科": "2454",
    "鴻海": "2317",
    "廣達": "2382",
    "台達電": "2308",
    "中華電": "2412",
    "兆豐金": "2886",
    "富邦金": "2881",
    "國泰金": "2882",
    "台新金": "2887",
}

STOP_WORDS = {
    "請", "幫我", "幫", "設定", "新增", "增加", "追蹤", "股票", "名單", "清單", "移除", "刪除",
    "減少", "不要", "再", "報", "預測", "晨報", "今日", "台灣", "美國", "台股", "美股",
    "和", "以及", "還有", "與", "請問", "我要", "可以", "先", "開始", "隔天", "每天",
}


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 93, exc_info=True)
    return default


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _notify_log(event: str, detail: str = "") -> None:
    try:
        NOTIFY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        ts = _tz_now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {event}"
        if detail:
            line += f" | {detail}"
        with NOTIFY_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line[:4000] + "\n")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 114, exc_info=True)


def _http_json(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = 15, insecure: bool = False) -> Any:
    hdr = {
        "User-Agent": "MAGI-market-briefing/1.0",
        "Accept": "application/json,text/plain,*/*",
    }
    if headers:
        hdr.update(headers)
    req = request.Request(url, headers=hdr)
    kwargs: Dict[str, Any] = {"timeout": timeout}
    if insecure:
        kwargs["context"] = ssl._create_unverified_context()  # noqa: SLF001
    with request.urlopen(req, **kwargs) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    return json.loads(raw)


def _yahoo_history(symbol: str, period: str = "3mo", with_volume: bool = False):
    q = parse.quote(symbol)
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{q}?range={period}&interval=1d"
    data = _http_json(url, timeout=18)
    chart = (((data or {}).get("chart") or {}).get("result") or [{}])[0]
    ts = chart.get("timestamp") or []
    quote = (((chart.get("indicators") or {}).get("quote") or [{}])[0] or {})
    closes_raw = quote.get("close") or []
    highs_raw = quote.get("high") or []
    lows_raw = quote.get("low") or []
    vols_raw = quote.get("volume") or []
    closes: List[float] = []
    tss: List[int] = []
    highs: List[float] = []
    lows: List[float] = []
    volumes: List[int] = []
    for i, (t, c) in enumerate(zip(ts, closes_raw)):
        if c is None:
            continue
        try:
            cv = float(c)
        except Exception:
            continue
        closes.append(cv)
        tss.append(int(t))
        if with_volume:
            highs.append(float(highs_raw[i]) if i < len(highs_raw) and highs_raw[i] is not None else cv)
            lows.append(float(lows_raw[i]) if i < len(lows_raw) and lows_raw[i] is not None else cv)
            volumes.append(int(vols_raw[i]) if i < len(vols_raw) and vols_raw[i] is not None else 0)
    if with_volume:
        return closes, tss, highs, lows, volumes
    return closes, tss


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


def _load_cache() -> Dict[str, Any]:
    return _load_json(CACHE_PATH, {"twse_lookup": {}, "sec_tickers": {}, "updated_at": ""})


def _save_cache(cache: Dict[str, Any]) -> None:
    cache["updated_at"] = _tz_now().isoformat()
    _save_json(CACHE_PATH, cache)


def _load_perf() -> Dict[str, Any]:
    d = _load_json(PERF_PATH, {})
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get("records"), list):
        d["records"] = []
    if not isinstance(d.get("metrics"), dict):
        d["metrics"] = {}
    if not isinstance(d.get("tuning_log"), list):
        d["tuning_log"] = []
    mp = d.get("model_params")
    if not isinstance(mp, dict):
        mp = dict(_DEFAULT_MODEL_PARAMS)
    for k, v in _DEFAULT_MODEL_PARAMS.items():
        mp.setdefault(k, v)
    d["model_params"] = mp
    d.setdefault("updated_at", "")
    return d


def _save_perf(perf: Dict[str, Any]) -> None:
    perf["updated_at"] = _tz_now().isoformat()
    # keep bounded
    recs = perf.get("records") if isinstance(perf.get("records"), list) else []
    if len(recs) > 5000:
        perf["records"] = recs[-5000:]
    logs = perf.get("tuning_log") if isinstance(perf.get("tuning_log"), list) else []
    if len(logs) > 100:
        perf["tuning_log"] = logs[-100:]
    _save_json(PERF_PATH, perf)


def _parse_ymd(s: str) -> Optional[date]:
    try:
        return datetime.strptime(str(s or "").strip(), "%Y-%m-%d").date()
    except Exception:
        return None


def _next_trade_date(issued: date, market: str) -> date:
    # 簡化版：先以平日做交易日推估
    d = issued + timedelta(days=1)
    while d.weekday() >= 5:  # 5,6 => weekend
        d += timedelta(days=1)
    return d


def _utc_ts_to_date(ts: int) -> date:
    return datetime.fromtimestamp(int(ts), timezone.utc).date()


def _actual_close_on_or_after(symbol: str, target: date) -> Tuple[Optional[float], Optional[str]]:
    try:
        closes, tss = _yahoo_history(symbol, period="6mo")
        for ts, c in zip(tss, closes):
            d = _utc_ts_to_date(ts)
            if d >= target:
                return float(c), d.strftime("%Y-%m-%d")
    except Exception:
        return None, None
    return None, None


def _sign(v: float, eps: float = 0.15) -> int:
    if v > eps:
        return 1
    if v < -eps:
        return -1
    return 0


def _predict_pct_by_params(trend: float, mom5: float, vol: float, params: Dict[str, float]) -> float:
    wt = float(params.get("w_trend", _DEFAULT_MODEL_PARAMS["w_trend"]))
    wm = float(params.get("w_mom", _DEFAULT_MODEL_PARAMS["w_mom"]))
    wv = float(params.get("w_vol", _DEFAULT_MODEL_PARAMS["w_vol"]))
    bias = float(params.get("bias", _DEFAULT_MODEL_PARAMS["bias"]))
    raw = wt * trend + wm * mom5 - wv * vol + bias
    return _clamp(raw, -7.0, 7.0)


def _solve_linear_4x4(a: List[List[float]], b: List[float]) -> Optional[List[float]]:
    # Gaussian elimination (small fixed-size system)
    n = 4
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    try:
        for i in range(n):
            pivot = i
            for r in range(i + 1, n):
                if abs(m[r][i]) > abs(m[pivot][i]):
                    pivot = r
            if abs(m[pivot][i]) < 1e-9:
                return None
            if pivot != i:
                m[i], m[pivot] = m[pivot], m[i]
            pv = m[i][i]
            for c in range(i, n + 1):
                m[i][c] /= pv
            for r in range(n):
                if r == i:
                    continue
                f = m[r][i]
                if abs(f) < 1e-12:
                    continue
                for c in range(i, n + 1):
                    m[r][c] -= f * m[i][c]
        return [m[i][n] for i in range(n)]
    except Exception:
        return None


def _fit_params_from_samples(samples: List[Dict[str, Any]], decay: float = 0.0) -> Optional[Dict[str, float]]:
    """Least-squares fit with optional exponential decay weighting.
    decay=0 means uniform weights; decay>0 gives more weight to recent samples.
    """
    # features: [trend, mom5, -vol, 1]
    if len(samples) < 20:
        return None
    xtx = [[0.0] * 4 for _ in range(4)]
    xty = [0.0] * 4
    lam = 1e-3
    n_samples = len(samples)
    for idx, s in enumerate(samples):
        w = math.exp(decay * (idx - n_samples + 1)) if decay > 0 else 1.0
        x = [
            float(s.get("trend") or 0.0),
            float(s.get("mom5") or 0.0),
            -float(s.get("vol") or 0.0),
            1.0,
        ]
        y = float(s.get("actual_ret_pct") or 0.0)
        for i in range(4):
            xty[i] += w * x[i] * y
            for j in range(4):
                xtx[i][j] += w * x[i] * x[j]
    for i in range(4):
        xtx[i][i] += lam
    solved = _solve_linear_4x4(xtx, xty)
    if not solved:
        return None
    wt, wm, wv_for_neg_vol, bias = solved
    # convert back to -w_vol * vol
    wv = wv_for_neg_vol
    wt = _clamp(wt, -0.20, 1.50)
    wm = _clamp(wm, -0.20, 1.50)
    wv = _clamp(wv, 0.00, 1.20)
    bias = _clamp(bias, -2.00, 2.00)
    return {"w_trend": wt, "w_mom": wm, "w_vol": wv, "bias": bias}


def _mae_for_params(samples: List[Dict[str, Any]], params: Dict[str, float]) -> float:
    errs: List[float] = []
    for s in samples:
        pred = _predict_pct_by_params(
            float(s.get("trend") or 0.0),
            float(s.get("mom5") or 0.0),
            float(s.get("vol") or 0.0),
            params,
        )
        y = float(s.get("actual_ret_pct") or 0.0)
        errs.append(abs(pred - y))
    if not errs:
        return 999.0
    return float(sum(errs) / len(errs))


def _refresh_metrics(perf: Dict[str, Any]) -> Dict[str, Any]:
    recs = perf.get("records") if isinstance(perf.get("records"), list) else []
    solved = [r for r in recs if isinstance(r, dict) and r.get("resolved_date")]
    recent = solved[-180:]
    if not recent:
        perf["metrics"] = {"resolved": 0, "mae_pct_point": 0.0, "hit_rate": 0.0, "last_resolved": ""}
        return perf["metrics"]
    mae = _safe_mean([float(r.get("abs_err_pct") or 0.0) for r in recent], default=0.0)
    hits = [1 if bool(r.get("sign_hit")) else 0 for r in recent]
    hit = _safe_mean(hits, default=0.0) * 100.0
    perf["metrics"] = {
        "resolved": len(solved),
        "window": len(recent),
        "mae_pct_point": round(mae, 3),
        "hit_rate": round(hit, 1),
        "last_resolved": str(recent[-1].get("resolved_date") or ""),
    }
    return perf["metrics"]


def _resolve_records_and_tune(perf: Dict[str, Any]) -> Dict[str, Any]:
    now = _tz_now().date()
    recs = perf.get("records") if isinstance(perf.get("records"), list) else []
    resolved_now = 0
    close_cache: Dict[Tuple[str, str], Tuple[Optional[float], Optional[str]]] = {}
    for r in recs:
        if not isinstance(r, dict):
            continue
        if r.get("resolved_date"):
            continue
        tgt = _parse_ymd(str(r.get("target_date") or ""))
        if not tgt or tgt > now:
            continue
        symbol = str(r.get("symbol") or "").strip()
        if not symbol:
            continue
        k = (symbol.upper(), tgt.strftime("%Y-%m-%d"))
        if k in close_cache:
            actual_close, actual_date = close_cache[k]
        else:
            actual_close, actual_date = _actual_close_on_or_after(symbol, tgt)
            close_cache[k] = (actual_close, actual_date)
        if actual_close is None:
            continue
        issued_last = float(r.get("last_price") or 0.0)
        if abs(issued_last) < 1e-9:
            continue
        actual_ret = _pct(float(actual_close), issued_last)
        pred_ret = float(r.get("pred_pct") or 0.0)
        abs_err = abs(pred_ret - actual_ret)
        r["actual_price"] = round(float(actual_close), 6)
        r["actual_date"] = actual_date or ""
        r["actual_ret_pct"] = round(actual_ret, 6)
        r["abs_err_pct"] = round(abs_err, 6)
        r["sign_hit"] = (_sign(pred_ret) == _sign(actual_ret))
        r["resolved_date"] = now.strftime("%Y-%m-%d")
        resolved_now += 1

    # auto-tune by least squares on latest resolved samples
    tune_applied = False
    tune_msg = ""
    params = perf.get("model_params") if isinstance(perf.get("model_params"), dict) else dict(_DEFAULT_MODEL_PARAMS)
    samples = [
        r for r in recs
        if isinstance(r, dict)
        and r.get("resolved_date")
        and r.get("actual_ret_pct") is not None
        and r.get("trend") is not None
        and r.get("mom5") is not None
        and r.get("vol") is not None
    ][-240:]
    if len(samples) >= 20:
        # Try both uniform and decay-weighted fits, pick the better one
        fitted_uniform = _fit_params_from_samples(samples)
        fitted_decay = _fit_params_from_samples(samples, decay=0.02)
        candidates = [(f, t) for f, t in [(fitted_uniform, "uniform"), (fitted_decay, "decay")] if f]
        fitted = None
        fit_type = ""
        if candidates:
            best_mae = 999.0
            for f, t in candidates:
                m = _mae_for_params(samples, f)
                if m < best_mae:
                    best_mae = m
                    fitted = f
                    fit_type = t
        if fitted:
            old_mae = _mae_for_params(samples, params)
            new_mae = _mae_for_params(samples, fitted)
            if (old_mae - new_mae) >= 0.03:
                lr = 0.35
                merged = {
                    "w_trend": (1 - lr) * float(params.get("w_trend", 0.55)) + lr * float(fitted["w_trend"]),
                    "w_mom": (1 - lr) * float(params.get("w_mom", 0.45)) + lr * float(fitted["w_mom"]),
                    "w_vol": (1 - lr) * float(params.get("w_vol", 0.18)) + lr * float(fitted["w_vol"]),
                    "bias": (1 - lr) * float(params.get("bias", 0.0)) + lr * float(fitted["bias"]),
                    "updated_at": _tz_now().isoformat(),
                }
                perf["model_params"] = merged
                tune_applied = True
                tune_msg = (
                    f"權重已自動校準（MAE {old_mae:.3f} → {new_mae:.3f}）"
                )
                logs = perf.get("tuning_log") if isinstance(perf.get("tuning_log"), list) else []
                logs.append({
                    "ts": _tz_now().isoformat(),
                    "sample_count": len(samples),
                    "old_mae": round(old_mae, 6),
                    "new_mae": round(new_mae, 6),
                    "params": {k: round(float(v), 6) for k, v in merged.items() if k != "updated_at"},
                })
                perf["tuning_log"] = logs[-100:]

    metrics = _refresh_metrics(perf)
    return {
        "resolved_now": resolved_now,
        "tune_applied": tune_applied,
        "tune_msg": tune_msg,
        "metrics": metrics,
    }


def _upsert_prediction_records(perf: Dict[str, Any], rows: List[Dict[str, Any]], issued_ymd: str) -> int:
    recs = perf.get("records") if isinstance(perf.get("records"), list) else []
    issued = _parse_ymd(issued_ymd) or _tz_now().date()
    inserted = 0
    keys = {
        (str(r.get("symbol") or "").upper(), str(r.get("issued_date") or ""))
        for r in recs
        if isinstance(r, dict)
    }

    for r in rows:
        if not isinstance(r, dict) or not r.get("ok"):
            continue
        symbol = str(r.get("symbol") or "").strip()
        if not symbol:
            continue
        key = (symbol.upper(), issued_ymd)
        target = _next_trade_date(issued, str(r.get("market") or "US"))
        item = {
            "symbol": symbol,
            "label": str(r.get("label") or symbol),
            "market": str(r.get("market") or "US"),
            "issued_date": issued_ymd,
            "target_date": target.strftime("%Y-%m-%d"),
            "last_price": round(float(r.get("last") or 0.0), 6),
            "pred_price": round(float(r.get("pred_price") or 0.0), 6),
            "pred_pct": round(float(r.get("pred_pct") or 0.0), 6),
            "trend": round(float(r.get("trend") or 0.0), 6),
            "mom5": round(float(r.get("mom5") or 0.0), 6),
            "vol": round(float(r.get("vol") or 0.0), 6),
            "signal": round(float(r.get("signal") or 0.0), 6),
            "confidence": int(r.get("confidence") or 0),
            "actual_price": None,
            "actual_date": "",
            "actual_ret_pct": None,
            "abs_err_pct": None,
            "sign_hit": None,
            "resolved_date": "",
            "created_at": _tz_now().isoformat(),
        }
        if key in keys:
            for i, old in enumerate(recs):
                if not isinstance(old, dict):
                    continue
                if str(old.get("symbol") or "").upper() == key[0] and str(old.get("issued_date") or "") == key[1]:
                    # 同日同標的重跑時更新預測值，但保留已解算資訊
                    keep_actual = {
                        "actual_price": old.get("actual_price"),
                        "actual_date": old.get("actual_date"),
                        "actual_ret_pct": old.get("actual_ret_pct"),
                        "abs_err_pct": old.get("abs_err_pct"),
                        "sign_hit": old.get("sign_hit"),
                        "resolved_date": old.get("resolved_date"),
                    }
                    item.update(keep_actual)
                    recs[i] = item
                    break
        else:
            recs.append(item)
            keys.add(key)
            inserted += 1

    perf["records"] = recs
    return inserted


def _format_perf_lines(perf: Dict[str, Any], resolve_info: Dict[str, Any]) -> List[str]:
    metrics = resolve_info.get("metrics") if isinstance(resolve_info.get("metrics"), dict) else {}
    model_params = perf.get("model_params") if isinstance(perf.get("model_params"), dict) else {}
    lines = [
        "【模型學習狀態】",
        (
            f"- 近期命中率：{float(metrics.get('hit_rate') or 0.0):.1f}%"
            f"（MAE {float(metrics.get('mae_pct_point') or 0.0):.3f} pct, 視窗 {int(metrics.get('window') or 0)}）"
        ),
        (
            f"- 本次解算：{int(resolve_info.get('resolved_now') or 0)} 筆"
            f"｜自動校準：{'有' if bool(resolve_info.get('tune_applied')) else '無'}"
        ),
        f"- 本次新預測：{int(resolve_info.get('new_count') or 0)} 筆",
        (
            f"- 目前權重：trend={float(model_params.get('w_trend') or _DEFAULT_MODEL_PARAMS['w_trend']):.3f}, "
            f"mom={float(model_params.get('w_mom') or _DEFAULT_MODEL_PARAMS['w_mom']):.3f}, "
            f"vol={float(model_params.get('w_vol') or _DEFAULT_MODEL_PARAMS['w_vol']):.3f}, "
            f"bias={float(model_params.get('bias') or _DEFAULT_MODEL_PARAMS['bias']):+.3f}"
        ),
    ]
    tune_msg = str(resolve_info.get("tune_msg") or "").strip()
    if tune_msg:
        lines.append(f"- 校準結果：{tune_msg}")
    return lines

def _get_twse_lookup(force: bool = False) -> Dict[str, Dict[str, str]]:
    cache = _load_cache()
    today = _tz_now().strftime("%Y-%m-%d")
    stored = cache.get("twse_lookup") if isinstance(cache.get("twse_lookup"), dict) else {}
    if stored and not force and str(stored.get("_date") or "") == today:
        return stored

    lookup: Dict[str, Dict[str, str]] = {"_date": today}
    endpoints = [
        "https://openapi.twse.com.tw/v1/opendata/t187ap03_L",
        "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_AVG_ALL",
    ]
    rows: List[Dict[str, Any]] = []
    for ep in endpoints:
        try:
            data = _http_json(ep, timeout=20, insecure=True)
            if isinstance(data, list) and data:
                rows = data
                break
        except Exception:
            continue

    for r in rows:
        if not isinstance(r, dict):
            continue
        code = str(r.get("公司代號") or r.get("Code") or r.get("證券代號") or "").strip()
        name = str(r.get("公司簡稱") or r.get("Name") or r.get("證券名稱") or "").strip()
        if not code or not re.fullmatch(r"\d{4,5}", code):
            continue
        if not name:
            name = code
        sym = f"{code}.TW"
        lookup[code] = {"symbol": sym, "label": name, "market": "TW"}
        lookup[name] = {"symbol": sym, "label": name, "market": "TW"}

    for n, c in COMMON_TW_ALIASES.items():
        if c in lookup:
            lookup[n] = lookup[c]
        else:
            # fallback even if endpoint data is incomplete at this moment
            lookup[n] = {"symbol": f"{c}.TW", "label": n, "market": "TW"}

    cache["twse_lookup"] = lookup
    _save_cache(cache)
    return lookup


def _get_sec_tickers(force: bool = False) -> Dict[str, str]:
    cache = _load_cache()
    today = _tz_now().strftime("%Y-%m-%d")
    stored = cache.get("sec_tickers") if isinstance(cache.get("sec_tickers"), dict) else {}
    if stored and not force and str(stored.get("_date") or "") == today:
        return stored

    out: Dict[str, str] = {"_date": today}
    try:
        sec_ua = os.environ.get("SEC_USER_AGENT", "MAGI-market-briefing/1.0 (admin@magi.local)")
        data = _http_json(
            "https://www.sec.gov/files/company_tickers.json",
            headers={"User-Agent": sec_ua},
            timeout=20,
        )
        if isinstance(data, dict):
            for v in data.values():
                if not isinstance(v, dict):
                    continue
                t = str(v.get("ticker") or "").upper().strip()
                cik = str(v.get("cik_str") or "").strip()
                if t and cik:
                    out[t] = cik.zfill(10)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 770, exc_info=True)

    cache["sec_tickers"] = out
    _save_cache(cache)
    return out


def _latest_tw_financials(code4: str) -> Dict[str, str]:
    res = {"rev": "", "eps": ""}
    # monthly revenue
    try:
        rows = _http_json("https://openapi.twse.com.tw/v1/opendata/t187ap05_L", timeout=20, insecure=True)
        if isinstance(rows, list):
            candidates = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                code = str(r.get("公司代號") or "").strip()
                if code != code4:
                    continue
                yyyymm = str(r.get("出表日期") or r.get("資料年月") or r.get("年月") or "").strip()
                yoy = str(r.get("營業收入-去年同月增減(%)") or r.get("去年同月增減") or "").strip()
                candidates.append((yyyymm, yoy))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                ym, yoy = candidates[0]
                if ym or yoy:
                    res["rev"] = f"月營收({ym}) YoY {yoy or 'n/a'}%"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 799, exc_info=True)

    # quarterly eps
    try:
        rows = _http_json("https://openapi.twse.com.tw/v1/opendata/t187ap14_L", timeout=20, insecure=True)
        if isinstance(rows, list):
            candidates = []
            for r in rows:
                if not isinstance(r, dict):
                    continue
                code = str(r.get("公司代號") or "").strip()
                if code != code4:
                    continue
                yr = str(r.get("年度") or "").strip()
                qt = str(r.get("季別") or "").strip()
                q = str(r.get("資料年度季別") or r.get("年度季別") or "").strip()
                if not q and (yr or qt):
                    q = f"{yr}Q{qt}" if yr and qt else (yr or qt)
                eps = str(r.get("基本每股盈餘(元)") or r.get("每股盈餘(元)") or r.get("EPS") or "").strip()
                candidates.append((q, eps))
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                q, eps = candidates[0]
                if q or eps:
                    res["eps"] = f"季報({q}) EPS {eps or 'n/a'}"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 825, exc_info=True)

    return res


def _latest_us_filing(symbol: str) -> str:
    symbol = symbol.upper().strip()
    sec = _get_sec_tickers()
    cik = str(sec.get(symbol) or "").strip()
    if not cik:
        return ""
    try:
        url = f"https://data.sec.gov/submissions/CIK{cik}.json"
        sec_ua = os.environ.get("SEC_USER_AGENT", "MAGI-market-briefing/1.0 (admin@magi.local)")
        data = _http_json(url, headers={"User-Agent": sec_ua}, timeout=20)
        recent = ((data or {}).get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        dates = recent.get("filingDate") or []
        accs = recent.get("accessionNumber") or []
        for form, date, acc in zip(forms, dates, accs):
            f = str(form or "").strip().upper()
            if f in {"10-K", "10-Q", "8-K"}:
                return f"SEC {f} 申報日 {date} ({str(acc or '')[:18]})"
    except Exception:
        return ""
    return ""


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
    state = _load_json(STATE_PATH, dict(_DEFAULT_STATE))
    if not isinstance(state, dict):
        state = dict(_DEFAULT_STATE)
    for k, v in _DEFAULT_STATE.items():
        state.setdefault(k, v)
    if not isinstance(state.get("watchlist"), list):
        state["watchlist"] = []
    return state


def _save_state(state: Dict[str, Any]) -> None:
    state["updated_at"] = _tz_now().isoformat()
    _save_json(STATE_PATH, state)


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


def _predict_one(item: WatchItem, params: Dict[str, float], mode: str = "quick") -> Dict[str, Any]:
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

            # 距支撐/阻力的百分比
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
        return out
    except Exception as e:
        out["line"] = f"{item.label}({item.symbol})：資料取得失敗（{type(e).__name__}）"
        return out


def _render_report(items: List[WatchItem], rows: List[Dict[str, Any]], mode: str = "quick") -> str:
    now = _tz_now()
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
    # 固定同時輸出台股/美股區塊，避免格式漂移
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


def _maybe_notify(text: str, notify: bool) -> bool:
    if not notify:
        return False
    for p in (str(MAGI_ROOT), str(MAGI_ROOT.parent)):
        if p and p not in sys.path:
            sys.path.insert(0, p)
    try:
        from skills.ops.red_phone import alert_admin  # type: ignore

        r = alert_admin(
            text,
            severity="info",
            source="market_briefing",
            topic_key="market",
        ) or {}
        ok = bool(r.get("telegram") or r.get("line") or r.get("discord"))
        if not ok:
            _notify_log("notify_failed", json.dumps(r, ensure_ascii=False)[:1200])
        return ok
    except Exception as e:
        _notify_log("notify_exception", f"{type(e).__name__}: {e}; {traceback.format_exc(limit=2)}")
        return False


def _cmd_prompt(state: Dict[str, Any], notify: bool) -> str:
    today = _tz_now().strftime("%Y-%m-%d")
    if not state.get("first_prompt_date"):
        state["first_prompt_date"] = today
        _save_state(state)
    msg = _first_prompt_message()
    _maybe_notify(msg, notify=notify)
    return msg


def _cmd_list(state: Dict[str, Any]) -> str:
    items = _watchlist_from_state(state)
    active_from = str(state.get("active_from_date") or "").strip()
    lines = ["📌 目前追蹤股票：", _format_watchlist(items)]
    if active_from:
        lines.append(f"晨報啟用日：{active_from} 08:30")
    return "\n".join(lines)


def _register_financial_crawl_targets(items: List["WatchItem"]) -> None:
    """Register financial report URLs for watchlist items into the crawler-targets skill."""
    try:
        import subprocess as _sp
        crawler_script = str(MAGI_ROOT / "skills" / "crawler-targets" / "action.py")
        if not os.path.exists(crawler_script):
            return
        py = _skill_python()
        for item in items:
            sym = item.symbol.upper().split(".")[0]
            if item.market == "US":
                # macrotrends financial statements (free, no auth)
                slug_map = {
                    "AAPL": "apple", "TSLA": "tesla", "MSFT": "microsoft",
                    "GOOG": "alphabet", "GOOGL": "alphabet", "AMZN": "amazon",
                    "META": "meta-platforms", "NVDA": "nvidia", "QQQ": "invesco-qqq-trust",
                }
                slug = slug_map.get(sym, sym.lower())
                url = f"https://www.macrotrends.net/stocks/charts/{sym}/{slug}/financial-statements"
                note = f"{sym} 財報"
            elif item.market == "TW":
                # MOPS (公開資訊觀測站) financial summary for TW stocks
                code = sym
                url = f"https://mops.twse.com.tw/mops/web/t05st09_new?id={code}"
                note = f"{item.label}({code}) 重大訊息"
            else:
                continue
            payload = json.dumps({"url": url, "note": note}, ensure_ascii=False)
            _sp.run(
                [py, crawler_script, "--task", f"add {payload}"],
                capture_output=True, timeout=10,
                cwd=str(MAGI_ROOT),
            )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1276, exc_info=True)


def _update_watchlist(state: Dict[str, Any], mode: str, text: str) -> str:
    existing = _watchlist_from_state(state)
    parsed = _resolve_tokens(text)
    if mode in {"set", "add"} and not parsed:
        return (
            "⚠️ 我沒解析到股票代號，請用這種格式：\n"
            "- 追蹤股票：台積電、聯發科、AAPL、MSFT"
        )

    if mode == "set":
        new_items = parsed
    elif mode == "add":
        new_items = _unique(existing + parsed)
    elif mode == "remove":
        if not parsed:
            # 嘗試直接用原字串切 token 移除
            tokens = {x.upper() for x in _tokenize(text)}
        else:
            tokens = {x.symbol.upper() for x in parsed} | {x.label.upper() for x in parsed}
        kept: List[WatchItem] = []
        for it in existing:
            key = it.symbol.upper()
            if key in tokens or it.label.upper() in tokens:
                continue
            # partial match
            rm = False
            for tk in tokens:
                if tk and (tk in key or tk in it.label.upper()):
                    rm = True
                    break
            if not rm:
                kept.append(it)
        new_items = kept
    else:
        return "⚠️ 不支援的模式"

    today = _tz_now().date()
    old_empty = len(existing) == 0

    state["watchlist"] = [x.to_dict() for x in new_items]
    # 第一次建立清單，隔天才開始晨報
    if old_empty and new_items:
        state["active_from_date"] = (today + timedelta(days=1)).strftime("%Y-%m-%d")
    elif not new_items:
        state["active_from_date"] = ""

    _save_state(state)
    _register_financial_crawl_targets(new_items)

    if mode == "set":
        prefix = "✅ 已更新追蹤清單"
    elif mode == "add":
        prefix = "✅ 已新增追蹤股票"
    else:
        prefix = "✅ 已更新（移除）追蹤清單"

    lines = [prefix, _format_watchlist(new_items)]
    if state.get("active_from_date"):
        lines.append(f"我會從 {state['active_from_date']} 08:30 開始回報每日預測。")
    elif not new_items:
        lines.append("目前清單為空，我會先停止晨報。")
    return "\n".join(lines)


def _cmd_briefing(state: Dict[str, Any], notify: bool, force: bool = False, mode: str = "quick") -> str:
    items = _watchlist_from_state(state)
    today = _tz_now().strftime("%Y-%m-%d")

    if not items:
        msg = _cmd_prompt(state, notify=notify)
        return msg

    active_from = str(state.get("active_from_date") or "").strip()
    if active_from and not force:
        try:
            if datetime.strptime(today, "%Y-%m-%d").date() < datetime.strptime(active_from, "%Y-%m-%d").date():
                msg = (
                    f"✅ 已收到追蹤清單，將從 {active_from} 08:30 開始晨報。\n"
                    f"目前清單：\n{_format_watchlist(items)}"
                )
                _maybe_notify(msg, notify=notify)
                return msg
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1362, exc_info=True)

    perf = _load_perf()
    resolve_info = _resolve_records_and_tune(perf)
    params = perf.get("model_params") if isinstance(perf.get("model_params"), dict) else dict(_DEFAULT_MODEL_PARAMS)

    with ThreadPoolExecutor(max_workers=min(len(items), 6)) as pool:
        futures = {pool.submit(_predict_one, it, params, mode): it for it in items}
        rows = []
        for fut in as_completed(futures):
            try:
                rows.append(fut.result(timeout=60))
            except Exception:
                it = futures[fut]
                rows.append({"symbol": it.symbol, "label": it.label, "market": it.market, "ok": False, "line": f"{it.label}({it.symbol})：資料取得逾時", "mode": mode})
    # Preserve original order
    order = {it.symbol: i for i, it in enumerate(items)}
    rows.sort(key=lambda r: order.get(r.get("symbol", ""), 999))
    new_count = _upsert_prediction_records(perf, rows, today)
    _save_perf(perf)

    report = _render_report(items, rows, mode=mode)
    perf_info = dict(resolve_info)
    perf_info["new_count"] = int(new_count)
    report = report + "\n\n" + "\n".join(_format_perf_lines(perf, perf_info))
    state["last_report_date"] = today
    _save_state(state)
    _maybe_notify(report, notify=notify)
    return report


def _cmd_performance() -> str:
    perf = _load_perf()
    resolve_info = _resolve_records_and_tune(perf)
    _save_perf(perf)
    metrics = resolve_info.get("metrics") if isinstance(resolve_info.get("metrics"), dict) else {}
    recs = perf.get("records") if isinstance(perf.get("records"), list) else []
    params = perf.get("model_params") if isinstance(perf.get("model_params"), dict) else dict(_DEFAULT_MODEL_PARAMS)
    logs = perf.get("tuning_log") if isinstance(perf.get("tuning_log"), list) else []
    last_tune = logs[-1] if logs else {}

    lines = [
        "📈 MAGI 股市模型績效",
        f"- 累計預測筆數：{len(recs)}",
        f"- 已解算筆數：{int(metrics.get('resolved') or 0)}（最近視窗 {int(metrics.get('window') or 0)}）",
        f"- 近期方向命中率：{float(metrics.get('hit_rate') or 0.0):.1f}%",
        f"- 近期 MAE：{float(metrics.get('mae_pct_point') or 0.0):.3f} pct",
        f"- 最近解算日：{str(metrics.get('last_resolved') or 'n/a')}",
        (
            f"- 目前權重：trend={float(params.get('w_trend') or _DEFAULT_MODEL_PARAMS['w_trend']):.3f}, "
            f"mom={float(params.get('w_mom') or _DEFAULT_MODEL_PARAMS['w_mom']):.3f}, "
            f"vol={float(params.get('w_vol') or _DEFAULT_MODEL_PARAMS['w_vol']):.3f}, "
            f"bias={float(params.get('bias') or _DEFAULT_MODEL_PARAMS['bias']):+.3f}"
        ),
    ]
    if last_tune:
        lines.append(
            f"- 最近校準：{str(last_tune.get('ts') or '')}｜"
            f"MAE {float(last_tune.get('old_mae') or 0.0):.3f} → {float(last_tune.get('new_mae') or 0.0):.3f}"
        )
    if int(resolve_info.get("resolved_now") or 0) > 0:
        lines.append(f"- 本次補解算：{int(resolve_info.get('resolved_now') or 0)} 筆")
    if bool(resolve_info.get("tune_applied")) and str(resolve_info.get("tune_msg") or "").strip():
        lines.append(f"- 本次校準：{str(resolve_info.get('tune_msg'))}")

    # 個股績效分解
    solved = [r for r in recs if isinstance(r, dict) and r.get("resolved_date")]
    if solved:
        sym_stats: Dict[str, Dict[str, Any]] = {}
        for r in solved[-180:]:
            sym = str(r.get("symbol") or "?")
            if sym not in sym_stats:
                sym_stats[sym] = {"hits": 0, "total": 0, "errs": []}
            sym_stats[sym]["total"] += 1
            if r.get("sign_hit"):
                sym_stats[sym]["hits"] += 1
            sym_stats[sym]["errs"].append(float(r.get("abs_err_pct") or 0))
        if sym_stats:
            lines.append("")
            lines.append("📊 個股績效：")
            for sym, st in sorted(sym_stats.items()):
                hr = st["hits"] / st["total"] * 100 if st["total"] else 0
                mae = sum(st["errs"]) / len(st["errs"]) if st["errs"] else 0
                lines.append(f"  {sym}: 命中率 {hr:.0f}%（{st['hits']}/{st['total']}）MAE {mae:.3f}")

    return "\n".join(lines)


def _cmd_backtest() -> str:
    """回測：用不同參數組合（uniform/decay）對歷史數據做交叉驗證。"""
    perf = _load_perf()
    _resolve_records_and_tune(perf)
    _save_perf(perf)

    recs = perf.get("records") if isinstance(perf.get("records"), list) else []
    samples = [
        r for r in recs
        if isinstance(r, dict)
        and r.get("resolved_date")
        and r.get("actual_ret_pct") is not None
        and r.get("trend") is not None
    ]
    if len(samples) < 30:
        return f"⚠️ 回測需要至少 30 筆已解算紀錄，目前僅 {len(samples)} 筆。"

    params_current = perf.get("model_params") if isinstance(perf.get("model_params"), dict) else dict(_DEFAULT_MODEL_PARAMS)
    params_default = dict(_DEFAULT_MODEL_PARAMS)

    # Split: train on first 70%, test on last 30%
    split = int(len(samples) * 0.7)
    train, test = samples[:split], samples[split:]

    fitted_uni = _fit_params_from_samples(train, decay=0.0)
    fitted_dec = _fit_params_from_samples(train, decay=0.02)

    lines = [
        "📊 MAGI 股市模型回測報告",
        f"總樣本數：{len(samples)}（訓練 {len(train)} / 測試 {len(test)}）",
        "",
        "【測試集績效比較】",
    ]

    candidates = [
        ("目前權重", params_current),
        ("默認權重", params_default),
    ]
    if fitted_uni:
        candidates.append(("均勻擬合", fitted_uni))
    if fitted_dec:
        candidates.append(("衰減擬合", fitted_dec))

    best_mae = 999.0
    best_name = ""
    for name, p in candidates:
        mae = _mae_for_params(test, p)
        hits = 0
        for s in test:
            pred = _predict_pct_by_params(
                float(s.get("trend") or 0), float(s.get("mom5") or 0),
                float(s.get("vol") or 0), p,
            )
            if _sign(pred) == _sign(float(s.get("actual_ret_pct") or 0)):
                hits += 1
        hr = hits / len(test) * 100 if test else 0
        lines.append(f"  {name}: MAE={mae:.3f} 命中率={hr:.1f}%")
        if mae < best_mae:
            best_mae = mae
            best_name = name

    lines.append(f"\n最佳：{best_name}（MAE {best_mae:.3f}）")

    # 如果最佳不是目前權重且改善幅度 ≥ 0.05，自動套用
    best_params = None
    for name, p in candidates:
        if name == best_name:
            best_params = p
            break
    current_mae = _mae_for_params(test, params_current)
    if best_params and best_name != "目前權重" and (current_mae - best_mae) >= 0.05:
        lr = 0.4
        merged = {
            "w_trend": (1 - lr) * float(params_current.get("w_trend", 0.55)) + lr * float(best_params["w_trend"]),
            "w_mom": (1 - lr) * float(params_current.get("w_mom", 0.45)) + lr * float(best_params["w_mom"]),
            "w_vol": (1 - lr) * float(params_current.get("w_vol", 0.18)) + lr * float(best_params["w_vol"]),
            "bias": (1 - lr) * float(params_current.get("bias", 0.0)) + lr * float(best_params["bias"]),
            "updated_at": _tz_now().isoformat(),
        }
        perf["model_params"] = merged
        logs = perf.get("tuning_log") if isinstance(perf.get("tuning_log"), list) else []
        logs.append({
            "ts": _tz_now().isoformat(),
            "source": f"backtest_{best_name}",
            "sample_count": len(samples),
            "old_mae": round(current_mae, 6),
            "new_mae": round(best_mae, 6),
            "params": {k: round(float(v), 6) for k, v in merged.items() if k != "updated_at"},
        })
        perf["tuning_log"] = logs[-100:]
        _save_perf(perf)
        lines.append(f"✅ 已自動套用「{best_name}」權重（MAE {current_mae:.3f} → {best_mae:.3f}，lr=0.4 漸進融合）")
    elif best_name == "目前權重":
        lines.append("✅ 目前權重已是最佳，無需調整。")

    # 時間序列趨勢（近 30 筆的滾動 MAE）
    if len(samples) >= 30:
        window = 10
        lines.append("")
        lines.append("【近期滾動 MAE (10筆窗口)】")
        for i in range(max(len(samples) - 30, 0), len(samples) - window + 1, window):
            chunk = samples[i:i + window]
            m = _mae_for_params(chunk, params_current)
            d = str(chunk[-1].get("resolved_date", ""))
            lines.append(f"  ~{d}: {m:.3f}")

    return "\n".join(lines)


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


# ── 同業比較 (Comps) ──────────────────────────────────────────────


def _get_twse_sector_map() -> Dict[str, List[Dict[str, str]]]:
    """Return {sector_name: [{code, name, symbol}, ...]} from TWSE stock list."""
    cache = _load_cache()
    today = _tz_now().strftime("%Y-%m-%d")
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
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1612, exc_info=True)

    cache["twse_sector_map"] = sector_map
    _save_cache(cache)
    return sector_map


def _find_peers(target_code: str, sector_map: Dict[str, List[Dict[str, str]]], max_peers: int = 8) -> Tuple[str, List[Dict[str, str]]]:
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


def _fetch_comps_metrics(symbol: str, code4: str, market: str) -> Dict[str, Any]:
    """Fetch price + TWSE financials for one stock (no v10 API needed)."""
    metrics: Dict[str, Any] = {"symbol": symbol, "code": code4, "ok": False}
    try:
        # Use v8 chart API (same as _yahoo_history, known to work)
        closes, _ = _yahoo_history(symbol, period="3mo")
        if not closes:
            return metrics
        metrics["price"] = closes[-1]
        metrics["name"] = symbol

        # TW-specific: revenue YoY%, EPS from TWSE OpenAPI
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
            # Approximate P/E from price / EPS (annualized)
            if metrics.get("eps_val") and metrics["eps_val"] > 0:
                metrics["pe"] = round(closes[-1] / (metrics["eps_val"] * 4), 1)  # quarterly → annual
            # Price change metrics
            if len(closes) >= 20:
                metrics["mom_20d"] = round(_pct(closes[-1], closes[-20]), 2)
            if len(closes) >= 5:
                metrics["mom_5d"] = round(_pct(closes[-1], closes[-5]), 2)

        metrics["ok"] = True
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1667, exc_info=True)
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

    # Fetch metrics for target + peers concurrently
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

    # Calculate medians
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

    # Render comparison table
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

    # Sort: target first, then by price descending (proxy for market cap)
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

    # Valuation summary
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


# ── 產業分析 (Sector) ────────────────────────────────────────────


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
        # List available sectors with Chinese names
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

    # Pick top companies by familiarity (first N by code, which roughly correlates to listing age/size)
    sample_size = min(len(matched_members), 10)
    sample = matched_members[:sample_size]

    # Fetch price data concurrently for sampled stocks
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

    # Sort by market cap / price for display (use last price as proxy)
    ok_data.sort(key=lambda d: float(d.get("last") or 0), reverse=True)

    # Sector aggregates
    avg_pred = _safe_mean([float(d.get("pred_pct") or 0) for d in ok_data])
    avg_conf = _safe_mean([float(d.get("confidence") or 0) for d in ok_data])

    lines.append(f"【產業概覽】（取樣 {len(ok_data)} 家）")
    lines.append(f"整體預估方向：{'多方' if avg_pred >= 0 else '空方'}（平均 {avg_pred:+.2f}%）")
    lines.append(f"平均信心度：{avg_conf:.0f}%")
    lines.append("")

    # Technical consensus (if technical/deep mode)
    if mode in {"technical", "deep"}:
        rsi_vals = [float(d.get("rsi") or 50) for d in ok_data if d.get("rsi") is not None]
        macd_bulls = sum(1 for d in ok_data if float(d.get("macd_hist") or 0) > 0)
        if rsi_vals:
            avg_rsi = _safe_mean(rsi_vals)
            rsi_label = "超買" if avg_rsi > 70 else ("超賣" if avg_rsi < 30 else "中性")
            lines.append(f"【技術面共識】")
            lines.append(f"平均 RSI(14)：{avg_rsi:.1f}（{rsi_label}）")
            lines.append(f"MACD 多頭比例：{macd_bulls}/{len(ok_data)}（{macd_bulls/len(ok_data)*100:.0f}%）")
            lines.append("")

    if mode == "deep":
        # Volume trend consensus
        vol_trends = [str(d.get("volume_trend") or "") for d in ok_data if d.get("volume_trend")]
        if vol_trends:
            from collections import Counter
            vt_counts = Counter(vol_trends)
            dominant = vt_counts.most_common(1)[0] if vt_counts else ("無資料", 0)
            lines.append(f"【資金流向】")
            lines.append(f"量能共識：{dominant[0]}（{dominant[1]}/{len(vol_trends)} 家）")
            # ADX trend strength
            adx_vals = [float(d.get("adx") or 0) for d in ok_data if d.get("adx") is not None]
            if adx_vals:
                avg_adx = _safe_mean(adx_vals)
                strength = "強趨勢" if avg_adx > 25 else ("弱趨勢" if avg_adx > 15 else "盤整")
                lines.append(f"平均 ADX：{avg_adx:.1f}（{strength}）")
            lines.append("")

    # Individual stock details
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


# ── Excel 匯出 (Export) ──────────────────────────────────────────


def _cmd_export(state: Dict[str, Any], mode: str = "deep") -> str:
    """匯出追蹤清單分析為 Excel (.xlsx)。"""
    items = _watchlist_from_state(state)
    if not items:
        return "⚠️ 追蹤清單為空，請先設定追蹤股票。"

    perf = _load_perf()
    params = perf.get("model_params") if isinstance(perf.get("model_params"), dict) else dict(_DEFAULT_MODEL_PARAMS)

    # Fetch all predictions
    with ThreadPoolExecutor(max_workers=min(len(items), 6)) as pool:
        futures = {pool.submit(_predict_one, it, params, mode): it for it in items}
        rows: List[Dict[str, Any]] = []
        for fut in as_completed(futures):
            try:
                rows.append(fut.result(timeout=60))
            except Exception:
                it = futures[fut]
                rows.append({"symbol": it.symbol, "label": it.label, "market": it.market, "ok": False})

    order = {it.symbol: i for i, it in enumerate(items)}
    rows.sort(key=lambda r: order.get(r.get("symbol", ""), 999))

    # Build Excel via xlsx skill
    now = _tz_now()
    date_str = now.strftime("%Y%m%d")
    export_dir = MAGI_ROOT / "static" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    xlsx_path = str(export_dir / f"market_briefing_{date_str}.xlsx")

    mode_tag = {"quick": "快速", "technical": "技術分析", "deep": "深度"}.get(mode, mode)

    # Build spreadsheet data rows
    header = ["代號", "名稱", "市場", "昨收", "預估價", "預估漲跌%", "信心%", "趨勢(EMA)", "動量5日%", "波動率"]
    if mode in {"technical", "deep"}:
        header += ["RSI(14)", "MACD柱", "BBands %B"]
    if mode == "deep":
        header += ["支撐", "阻力", "量能", "ADX", "趨勢強度"]

    data_rows: List[List[Any]] = []
    for r in rows:
        if not r.get("ok"):
            data_rows.append([r.get("symbol", ""), r.get("label", ""), r.get("market", ""), "資料取得失敗"] + [""] * (len(header) - 4))
            continue
        row = [
            r.get("symbol", ""),
            r.get("label", ""),
            r.get("market", ""),
            round(float(r.get("last") or 0), 2),
            round(float(r.get("pred_price") or 0), 2),
            round(float(r.get("pred_pct") or 0), 2),
            int(r.get("confidence") or 0),
            round(float(r.get("trend") or 0), 3),
            round(float(r.get("mom5") or 0), 3),
            round(float(r.get("vol") or 0), 3),
        ]
        if mode in {"technical", "deep"}:
            row += [
                round(float(r.get("rsi") or 0), 1) if r.get("rsi") is not None else "",
                round(float(r.get("macd_hist") or 0), 4) if r.get("macd_hist") is not None else "",
                round(float(r.get("bb_pct") or 0), 1) if r.get("bb_pct") is not None else "",
            ]
        if mode == "deep":
            row += [
                round(float(r.get("support") or 0), 2) if r.get("support") is not None else "",
                round(float(r.get("resistance") or 0), 2) if r.get("resistance") is not None else "",
                str(r.get("volume_trend") or ""),
                round(float(r.get("adx") or 0), 1) if r.get("adx") is not None else "",
                str(r.get("trend_strength") or ""),
            ]
        data_rows.append(row)

    # Try using xlsx skill, fallback to direct openpyxl
    try:
        import subprocess as _sp
        xlsx_skill = str(MAGI_ROOT / "skills" / "xlsx" / "action.py")
        py = _skill_python()
        if os.path.exists(xlsx_skill):
            # Build JSON payload for xlsx skill
            payload = {
                "sheets": [{
                    "name": f"股市分析_{mode_tag}",
                    "headers": header,
                    "rows": data_rows,
                }],
                "output_path": xlsx_path,
            }
            payload_str = json.dumps(payload, ensure_ascii=False)
            result = _sp.run(
                [py, xlsx_skill, "--task", "create", "--text", payload_str],
                capture_output=True, timeout=30, text=True,
                cwd=str(MAGI_ROOT),
            )
            if result.returncode == 0 and os.path.exists(xlsx_path):
                return f"✅ Excel 報表已匯出：{xlsx_path}\n共 {len(rows)} 檔標的，{mode_tag}模式。"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2048, exc_info=True)

    # Fallback: direct openpyxl
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = f"股市分析_{mode_tag}"

        # Title row
        ws.append([f"MAGI 股市分析報表 — {mode_tag}模式（{now.strftime('%Y-%m-%d %H:%M')}）"])
        ws.append([])  # blank row
        ws.append(header)

        for dr in data_rows:
            ws.append(dr)

        # Auto-width
        for col_idx, col_name in enumerate(header, 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(col_idx)].width = max(len(str(col_name)) * 1.5, 10)

        wb.save(xlsx_path)
        return f"✅ Excel 報表已匯出：{xlsx_path}\n共 {len(rows)} 檔標的，{mode_tag}模式。"
    except ImportError:
        # Last resort: CSV
        csv_path = xlsx_path.replace(".xlsx", ".csv")
        with open(csv_path, "w", encoding="utf-8-sig") as f:
            f.write(",".join(header) + "\n")
            for dr in data_rows:
                f.write(",".join(str(x) for x in dr) + "\n")
        return f"✅ CSV 報表已匯出（openpyxl 未安裝）：{csv_path}\n共 {len(rows)} 檔標的，{mode_tag}模式。"


def main() -> int:
    ap = argparse.ArgumentParser(description="MAGI 市場晨報技能")
    ap.add_argument("--task", default="briefing", help="prompt|list|set|add|remove|briefing|performance|comps|sector|export")
    ap.add_argument("--text", default="", help="自然語句或股票清單")
    ap.add_argument("--notify", default="0", help="1=同步推播 TG")
    ap.add_argument("--force", default="0", help="1=忽略 active_from_date 直接產生")
    ap.add_argument("--mode", default="deep", help="quick|technical|deep（分析模式）")
    args = ap.parse_args()

    task = str(args.task or "briefing").strip().lower()
    if task == "help":
        import json as _j
        print(_j.dumps({"skill": "market-briefing", "tasks": ["prompt", "list", "set", "add", "remove", "briefing", "performance", "comps", "sector", "export"], "description": "MAGI 市場晨報 — 股市資訊收集與分析"}, ensure_ascii=False, indent=2))
        return 0
    text = str(args.text or "").strip()
    notify = str(args.notify or "0").strip().lower() in {"1", "true", "yes", "on"}
    force = str(args.force or "0").strip().lower() in {"1", "true", "yes", "on"}
    mode = str(args.mode or "deep").strip().lower()
    if mode not in {"quick", "technical", "deep"}:
        mode = "deep"

    state = _load_state()

    if task in {"prompt", "ask"}:
        print(_cmd_prompt(state, notify=notify))
        return 0
    if task == "list":
        print(_cmd_list(state))
        return 0
    if task in {"set", "add", "remove"}:
        print(_update_watchlist(state, task, text))
        return 0
    if task in {"brief", "briefing", "report", "daily"}:
        print(_cmd_briefing(state, notify=notify, force=force, mode=mode))
        return 0
    if task in {"perf", "performance", "metrics"}:
        print(_cmd_performance())
        return 0
    if task in {"backtest", "bt"}:
        print(_cmd_backtest())
        return 0
    if task in {"comps", "comp", "同業比較"}:
        print(_cmd_comps(text))
        return 0
    if task in {"sector", "產業分析", "板塊"}:
        print(_cmd_sector(text, mode=mode))
        return 0
    if task in {"export", "xlsx", "excel"}:
        print(_cmd_export(state, mode=mode))
        return 0

    print("⚠️ 不支援的 task，請使用：prompt|list|set|add|remove|briefing|performance|backtest|comps|sector|export")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

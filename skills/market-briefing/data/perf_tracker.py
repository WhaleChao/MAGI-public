#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data/perf_tracker.py — 績效追蹤與自動調參
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── 路徑推導 ─────────────────────────────────────────────────────
_SKILL_DIR = Path(__file__).resolve().parent.parent  # market-briefing/
_MAGI_ROOT = _SKILL_DIR.parents[1]
_AGENT_DIR = _MAGI_ROOT / ".agent"
PERF_PATH = _AGENT_DIR / "market_perf_history.json"

# Import shared indicators
from data.indicators import _clamp, _pct, _safe_mean


# ── Default model params (duplicated from action.py to avoid circular import) ──
_DEFAULT_MODEL_PARAMS = {
    "w_trend": 0.55,
    "w_mom": 0.45,
    "w_vol": 0.18,
    "bias": 0.0,
    "updated_at": "",
}


def _tz_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Taipei"))
    except Exception:
        return datetime.now()


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s", __name__, exc_info=True)
    return default


def _save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _notify_log(event: str, detail: str = "", notify_log_path: Optional[Path] = None) -> None:
    if notify_log_path is None:
        notify_log_path = _MAGI_ROOT / "static" / "market_briefing_notify.log"
    try:
        notify_log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = _tz_now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {event}"
        if detail:
            line += f" | {detail}"
        with notify_log_path.open("a", encoding="utf-8") as f:
            f.write(line[:4000] + "\n")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s", __name__, exc_info=True)


def _load_cache(cache_path: Path) -> Dict[str, Any]:
    return _load_json(cache_path, {"twse_lookup": {}, "sec_tickers": {}, "updated_at": ""})


def _save_cache(cache: Dict[str, Any], cache_path: Path) -> None:
    cache["updated_at"] = _tz_now().isoformat()
    _save_json(cache_path, cache)


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
    from data.fetcher import _yahoo_history
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
    """Least-squares fit with optional exponential decay weighting."""
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
        fitted_uniform = _fit_params_from_samples(samples)
        fitted_decay = _fit_params_from_samples(samples, decay=0.02)
        candidates = [(f, t) for f, t in [(fitted_uniform, "uniform"), (fitted_decay, "decay")] if f]
        fitted = None
        fit_type = ""
        if candidates:
            best_mae = 999.0
            for f, t in candidates:
                m_val = _mae_for_params(samples, f)
                if m_val < best_mae:
                    best_mae = m_val
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

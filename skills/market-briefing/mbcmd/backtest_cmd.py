#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cmd/backtest_cmd.py — 回測指令
"""
from __future__ import annotations

from typing import Any, Dict

from data.perf_tracker import (
    _DEFAULT_MODEL_PARAMS,
    _fit_params_from_samples,
    _load_perf,
    _mae_for_params,
    _predict_pct_by_params,
    _resolve_records_and_tune,
    _save_perf,
    _sign,
    _tz_now,
)


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

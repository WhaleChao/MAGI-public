from __future__ import annotations

import sys
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "market-briefing"
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

from data.perf_tracker import _market_quality_gate  # noqa: E402
from data.watchlist import WatchItem  # noqa: E402
from predict import predict_engine  # noqa: E402


def _row(index: int, *, actual: float, predicted: float, market: str = "TW") -> dict:
    return {
        "symbol": f"{2300 + index}.TW",
        "market": market,
        "target_date": f"2026-05-{(index % 28) + 1:02d}",
        "resolved_date": "2026-06-01",
        "actual_ret_pct": actual,
        "pred_pct": predicted,
    }


def test_quality_gate_rejects_model_that_does_not_beat_constant_baseline():
    records = [
        _row(index, actual=1.0 if index < 50 else -1.0, predicted=-1.0)
        for index in range(80)
    ]
    quality = _market_quality_gate({"records": records}, "TW")

    assert quality["sample_count"] == 80
    assert quality["baseline_hit_rate"] == 62.5
    assert quality["hit_rate"] == 37.5
    assert quality["verified_edge"] is False
    assert quality["status"] == "no_verified_edge"


def test_quality_gate_accepts_out_of_sample_directional_edge():
    records = [
        _row(
            index,
            actual=1.0 if index < 50 else -1.0,
            predicted=1.0 if index < 50 else -1.0,
        )
        for index in range(80)
    ]
    quality = _market_quality_gate({"records": records}, "TW")

    assert quality["hit_rate"] == 100.0
    assert quality["baseline_hit_rate"] == 62.5
    assert quality["edge_pct_point"] == 37.5
    assert quality["verified_edge"] is True


def test_unverified_market_output_is_watch_not_directional_forecast(monkeypatch):
    closes = [100.0 + index * 0.15 for index in range(70)]
    monkeypatch.setattr(
        predict_engine,
        "_yahoo_history",
        lambda *_args, **_kwargs: (closes, list(range(len(closes)))),
    )
    monkeypatch.setattr(predict_engine, "_latest_tw_financials", lambda _code: {})

    row = predict_engine._predict_one(
        WatchItem(symbol="2330.TW", label="台積電", market="TW"),
        {
            "w_trend": 0.55,
            "w_mom": 0.45,
            "w_vol": 0.18,
            "bias": 0.0,
            "_quality_gates": {
                "TW": {
                    "verified_edge": False,
                    "hit_rate": 43.9,
                    "baseline_hit_rate": 55.3,
                    "sample_count": 180,
                }
            },
        },
        mode="quick",
    )

    assert row["ok"] is True
    assert row["public_action"] == "WATCH"
    assert row["verified_edge"] is False
    assert row["confidence"] == 44
    assert "結論：觀望" in row["line"]
    assert "尚未證明具有方向優勢" in row["line"]

    report = predict_engine._render_report(
        [WatchItem(symbol="2330.TW", label="台積電", market="TW")],
        [row],
        mode="quick",
    )
    assert "整體結論：觀望" in report
    assert "整體偏向" not in report
